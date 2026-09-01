#!/usr/bin/env python3
"""Answer `!`-prefixed lines inside waynote notes, in place, on your desktop.

waynote already watches its notes directory and re-renders external edits live,
so a note is a two-way surface: write into the file and it appears on screen.
This daemon watches the same files, spots a trigger line, asks an agent, and
writes the reply back — the note becomes a live transcript.

Opt in per note by tagging it in waynote's own frontmatter:

    tags: [agent]

Optionally give that note its own persona, in the same frontmatter:

    system: you are a terse sysadmin; answer in one line

Then type a line starting with `!` in the note:

    !what's the ip of my terraria box?

If the agent speaks `stream-json` the reply is written progressively, so you
watch it type on the desktop.

Stdlib only; polls mtimes rather than pulling in a watcher dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_NOTES_DIR = Path.home() / ".local/share/waynote/notes"
DEFAULT_AGENT = "claude -p --output-format stream-json --include-partial-messages --verbose"
AGENT_TAG = "agent"
# `!question` continues the thread; `!!question` starts fresh. Continuation is
# the default because the alternative — re-establishing context every line — is
# the easy behaviour, not the useful one.
TRIGGER = re.compile(r"^!\s*(\S.*)$")
RESET_TRIGGER = re.compile(r"^!!\s*(\S.*)$")
# Written in place of the trigger while the agent runs, so you get feedback on
# screen immediately and the line can never fire twice.
PENDING = "_…thinking…_"
CARET = "▌"
STREAM_INTERVAL = 0.35  # seconds between note rewrites while streaming
SETTLE_SECONDS = 1.5    # ignore a file still being typed into
POLL_SECONDS = 0.5
DEFAULT_CONTEXT_CHARS = 6000
# Pre-approved tools. A non-interactive agent cannot raise a permission prompt,
# so anything not listed here is simply denied and the note gets "I can't".
#
# Everything here is READ-ONLY, and the filesystem tools are additionally bounded
# by the working directory — which defaults to an empty scratch dir, so a plain
# note can read nothing of yours. A note with `personal: true` runs from $HOME
# and these become genuinely useful. That composition is what makes it safe to
# turn them on by default: the blast radius is set by the workdir, not the tools.
#
# Bash, Write and Edit are deliberately absent. Those need an approval path that
# doesn't exist yet, and defaulting them on would mean a note could act on your
# machine unattended.
DEFAULT_ALLOWED_TOOLS = ["WebSearch", "WebFetch", "Read", "Glob", "Grep"]

CONTEXT_TEMPLATE = """\
You are answering inside a live sticky note on the user's desktop. The note so \
far is below. Lines beginning `> **?**` are the user's earlier questions; the \
text after each one is your own earlier answer. Treat it as an ongoing thread \
and resolve references like "it" or "this" against it.

--- note so far ---
{context}
--- end of note ---

{question}"""


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
    return Path(base) / "waynote-agent"


def new_ulid() -> str:
    """A ULID, so waynote treats the seeded note like one of its own."""
    import secrets
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
    ts, rnd = int(time.time() * 1000), secrets.randbits(80)
    out = ""
    for _ in range(10):
        ts, r = divmod(ts, 32)
        out = alphabet[r] + out
    tail = ""
    for _ in range(16):
        rnd, r = divmod(rnd, 32)
        tail = alphabet[r] + tail
    return out + tail


WELCOME = """# waynote-agent

This note is wired to an agent. Type a line starting with `!` anywhere in it
and the reply is written back here, live.

Try deleting the fence below and asking something, or just type your own line.

```
!what does wlr-layer-shell do?
```

## Making another one

```
waynote_agent.py --new "title"
```

Or bind it: Super+N makes a plain note, Super+Shift+N an agentic one.
Any existing note works too — add `agent` to its tags:

```
tags: [agent]
```

## Privacy

By default the agent runs in an empty scratch directory, so a sticky note
cannot read your Claude Code memory or CLAUDE.md. To let one note see your
personal context, add:

```
personal: true
```

That is what lets it answer questions about your own machine and notes — which
is useful for a scratchpad and wrong for a shopping list, so it is per note.

## Giving a note its own persona

Add a `system:` line to the frontmatter and that note answers in character.
waynote passes unknown frontmatter keys through untouched, so it lives in the
note itself:

```
system: you are a terse sysadmin; answer in one line
```

## Threads

The note is the conversation. Everything above your question gets sent as
context, so follow-ups work — "what about the other one?" resolves against what
is already written here.

Because the context *is* this note, you steer it by editing: delete an answer
you didn't like and it stops influencing the thread.

To ask something unrelated without dragging the thread along, use two bangs:

```
!!a completely fresh question
```

## Notes

- Lines inside ``` fences never trigger, so examples like the ones above are safe.
- The trigger line is rewritten to a `> **?**` quote before the agent runs, so
  it can never fire twice.
- Delete this note freely — it is seeded once and never comes back.
"""


def recover_interrupted(notes_dir: Path) -> list[Path]:
    """Un-claim questions that were being answered when we died.

    Claiming a trigger rewrites it to a `> **?**` quote before the agent runs, so
    it can never fire twice. The cost is that a crash, reboot or `systemctl
    restart` mid-answer strands the note at `_…thinking…_` with no trigger left
    to retry — stuck forever, silently.

    So on startup, turn any stranded marker back into the question it came from
    and let the normal path answer it again. A partial streamed reply is
    discarded: re-asking is cheap, and half an answer presented as whole is
    worse than none.
    """
    recovered = []
    for path in sorted(notes_dir.glob("*.md")):
        try:
            text = path.read_text()
        except OSError:
            continue
        fm, body = split_frontmatter(text)
        if not has_agent_tag(fm) or (PENDING not in body and CARET not in body):
            continue

        lines, out, i = body.splitlines(), [], 0
        while i < len(lines):
            if lines[i].startswith("> **?**"):
                # Collect the quoted question, then look at what follows it.
                q = [lines[i][len("> **?**"):].strip()]
                j = i + 1
                while j < len(lines) and lines[j].startswith("> "):
                    q.append(lines[j][2:].strip())
                    j += 1
                tail = "\n".join(lines[j:])
                stranded = tail.lstrip().startswith(PENDING) or tail.lstrip().startswith(CARET) \
                    or (tail.lstrip().split("\n", 1)[0].endswith(CARET) if tail.strip() else False)
                if stranded:
                    out.append("!" + q[0])
                    out.extend(q[1:])
                    # Drop the stranded marker/partial and stop rewriting here.
                    rest = [l for l in lines[j:] if PENDING not in l and CARET not in l]
                    out.extend(rest)
                    recovered.append(path)
                    i = len(lines)
                    continue
                out.extend(lines[i:j])
                i = j
                continue
            out.append(lines[i])
            i += 1

        if path in recovered:
            write_atomic(path, fm + "\n".join(out).rstrip() + "\n")
    return recovered


def create_note(notes_dir: Path, body: str, color: str = "yellow",
                personal: bool = False) -> Path:
    """Write a new agent-enabled note and return its path."""
    note_id = new_ulid()
    slug = re.sub(r"[^a-z0-9]+", "-",
                  (body.strip().splitlines() or ["untitled"])[0].lstrip("# ").lower()
                  ).strip("-")[:40] or "untitled"
    path = notes_dir / f"{note_id}-{slug}.md"
    frontmatter = (
        "---\n"
        f"id: {note_id}\n"
        f"color: {color}\n"
        "pinned: false\n"
        "locked: false\n"
        "layer: front\n"
        "tags: [agent]\n"
        + ("personal: true\n" if personal else "")
        + "---\n"
    )
    notes_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(path, frontmatter + body)
    return path


def seed_welcome(notes_dir: Path) -> Path | None:
    """Write the explainer note once, ever. Returns the path if it was created.

    Guarded by a marker in XDG_STATE_HOME rather than by the note's presence, so
    deleting the note keeps it deleted instead of resurrecting it every start.
    """
    marker = _state_dir() / "welcome-seeded"
    if marker.exists():
        return None
    note_id = new_ulid()
    path = notes_dir / f"{note_id}-waynote-agent.md"
    frontmatter = (
        "---\n"
        f"id: {note_id}\n"
        "color: blue\n"
        "pinned: false\n"
        "locked: false\n"
        "layer: front\n"
        "tags: [agent]\n"
        "---\n"
    )
    notes_dir.mkdir(parents=True, exist_ok=True)
    write_atomic(path, frontmatter + WELCOME)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{path.name}\n{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    return path


def split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter_block, body). Frontmatter is returned verbatim."""
    if not text.startswith("---\n"):
        return "", text
    end = text.find("\n---\n", 4)
    if end == -1:
        return "", text
    return text[: end + 5], text[end + 5 :]


def has_agent_tag(frontmatter: str) -> bool:
    m = re.search(r"^tags:\s*\[(.*?)\]\s*$", frontmatter, re.M)
    if m:
        return AGENT_TAG in [t.strip().strip("\"'") for t in m.group(1).split(",")]
    m = re.search(r"^tags:\s*$((?:\n\s*-\s*.+)+)", frontmatter, re.M)
    if m:
        return AGENT_TAG in [
            l.strip().lstrip("-").strip().strip("\"'") for l in m.group(1).splitlines() if l.strip()
        ]
    return False


def wants_personal_context(frontmatter: str) -> bool:
    """`personal: true` opts a note into your Claude Code memory and CLAUDE.md.

    Claude Code keys memory by working directory, so this is off by default:
    the agent runs in a neutral scratch dir and sees nothing about you. Opting
    in runs it from $HOME instead, which is what makes answers able to cite
    your own notes — and what you probably don't want every sticky doing.
    """
    m = re.search(r"^personal:\s*(.+?)\s*$", frontmatter, re.M)
    return bool(m) and m.group(1).strip().strip("\"'").lower() in {"true", "yes", "1", "on"}


def read_system_prompt(frontmatter: str) -> str | None:
    """A per-note persona: `system: ...` (quoted or bare) in the frontmatter.

    waynote round-trips unknown frontmatter keys untouched, so this rides along
    in the note itself rather than in any config of ours.
    """
    m = re.search(r"^system:\s*(.+?)\s*$", frontmatter, re.M)
    if not m:
        return None
    return m.group(1).strip().strip("\"'") or None


def build_argv(cmd: list[str], prompt: str, system: str | None,
               system_flag: str, allowed_tools: list[str]) -> list[str]:
    argv = list(cmd)
    if system:
        if system_flag:
            argv += [system_flag, system]
        else:  # agent has no such flag: fold it into the prompt
            prompt = f"{system}\n\n{prompt}"
    # --allowedTools is variadic, so the prompt must not follow it directly or it
    # is swallowed as another tool name. Passing the prompt first avoids that.
    # Costs nothing: it changes permission decisions, not the cached tool
    # definitions, so the prompt prefix is byte-identical with or without it.
    if allowed_tools and "--allowedTools" not in argv:
        return argv + [prompt, "--allowedTools", *allowed_tools]
    return argv + [prompt]


def unwrap(out: str) -> str:
    """Use the `result` field when the agent emits structured output.

    Agent CLIs also log diagnostics to stdout (claude interleaves MCP warnings),
    which would otherwise be written into the note as if the agent had said
    them. Anything that isn't JSON-with-a-result passes through untouched, so
    plain-text agents keep working.
    """
    try:
        payload = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return out or "_(empty reply)_"
    if isinstance(payload, dict) and "result" in payload:
        if payload.get("is_error"):
            return f"_(agent error: {str(payload['result'])[:200]})_"
        return str(payload["result"]).strip() or "_(empty reply)_"
    return out or "_(empty reply)_"


def ask_once(argv: list[str], timeout: int, cwd: Path) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired:
        return f"_(agent timed out after {timeout}s)_"
    out = (r.stdout or "").strip()
    if r.returncode != 0 and not out:
        return f"_(agent failed: {(r.stderr or '').strip()[:200]})_"
    return unwrap(out)


def ask_streaming(argv: list[str], timeout: int, on_partial, cwd: Path) -> str:
    """Run an agent that speaks stream-json, feeding partial text to on_partial."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, bufsize=1, cwd=cwd)
    acc, final, last = "", None, 0.0
    deadline = time.time() + timeout
    try:
        for line in proc.stdout:
            if time.time() > deadline:
                proc.kill()
                return acc.strip() or f"_(agent timed out after {timeout}s)_"
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # non-JSON diagnostics never reach the note
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "stream_event":
                delta = ev.get("event", {}).get("delta", {})
                if delta.get("type") == "text_delta":
                    acc += delta.get("text", "")
                    now = time.time()
                    if now - last >= STREAM_INTERVAL:
                        on_partial(acc)
                        last = now
            elif isinstance(ev.get("result"), str):
                final = ev["result"]      # authoritative final text
                if ev.get("is_error"):
                    return f"_(agent error: {final[:200]})_"
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return (final if final is not None else acc).strip() or "_(empty reply)_"


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def process(path: Path, cmd: list[str], timeout: int, system_flag: str,
            streaming: bool, context_chars: int, workdir: Path,
            allowed_tools: list[str]) -> bool:
    """Answer the first pending trigger in `path`. True if the file changed."""
    text = path.read_text()
    fm, body = split_frontmatter(text)
    if not has_agent_tag(fm):
        return False

    lines = body.splitlines()
    in_fence = False
    for i, line in enumerate(lines):
        # A fenced block is documentation, not a request — so a note can show
        # `!example` syntax (or hold pasted shell) without firing.
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        reset = RESET_TRIGGER.match(line)
        m = reset or TRIGGER.match(line)
        if not m:
            continue

        # A question can run over several lines. Keep consuming until a blank
        # line, another trigger, or a fence — otherwise only the first line was
        # ever asked, and the rest of what you typed sat orphaned under the
        # answer while the agent replied to half a question.
        question_lines = [m.group(1).strip()]
        end = i + 1
        while end < len(lines):
            nxt = lines[end]
            if not nxt.strip() or TRIGGER.match(nxt) or nxt.lstrip().startswith("```"):
                break
            question_lines.append(nxt.strip())
            end += 1
        question = "\n".join(question_lines).strip()

        # The note above the trigger *is* the conversation history. Using it
        # directly — rather than a hidden session id — means the context is the
        # thing on screen: edit the note and you have edited what the agent
        # sees, delete a bad exchange and it stops poisoning the thread.
        context = ""
        if context_chars > 0 and not reset:
            prior = "\n".join(lines[:i]).strip()
            if len(prior) > context_chars:      # keep the tail; recent > old
                prior = prior[-context_chars:]
                prior = prior[prior.find("\n") + 1 :]  # drop the half-line
            context = prior
        prompt = (CONTEXT_TEMPLATE.format(context=context, question=question)
                  if context else question)

        # 1) claim the whole question immediately so it cannot re-fire, and so
        #    the note shows a marker while the agent thinks.
        quoted = [f"> **?** {question_lines[0]}"] + [f"> {l}" for l in question_lines[1:]]
        lines[i:end] = quoted + ["", PENDING]
        write_atomic(path, fm + "\n".join(lines) + "\n")

        # `prev` tracks exactly what we last wrote, so each progressive update
        # is an unambiguous swap rather than a guess at where the answer sits.
        prev = PENDING

        def on_partial(partial: str) -> None:
            nonlocal prev
            cur = path.read_text()
            fm_now, body_now = split_frontmatter(cur)
            if prev not in body_now:
                return  # edited underneath us; leave it alone
            nxt = (partial + CARET) or PENDING
            write_atomic(path, fm_now + body_now.replace(prev, nxt, 1))
            prev = nxt

        argv = build_argv(cmd, prompt, read_system_prompt(fm), system_flag,
                          allowed_tools)
        cwd = Path.home() if wants_personal_context(fm) else workdir
        answer = (ask_streaming(argv, timeout, on_partial, cwd) if streaming
                  else ask_once(argv, timeout, cwd))

        # 2) swap whatever is on screen for the final answer.
        cur = path.read_text()
        fm_now, body_now = split_frontmatter(cur)
        if prev in body_now:
            write_atomic(path, fm_now + body_now.replace(prev, answer, 1))
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notes-dir", type=Path, default=DEFAULT_NOTES_DIR)
    ap.add_argument("--agent", default=DEFAULT_AGENT,
                    help="agent command; the prompt is appended. If it speaks "
                         "stream-json the reply streams in; JSON output is "
                         "unwrapped; plain text is used as-is")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--system-flag", default="--append-system-prompt",
                    help="flag the agent takes for a system prompt; pass '' to "
                         "prepend it to the prompt instead (default: "
                         "--append-system-prompt)")
    ap.add_argument("--no-stream", action="store_true",
                    help="disable progressive writing even if the agent supports it")
    ap.add_argument("--context-chars", type=int, default=DEFAULT_CONTEXT_CHARS,
                    help="how much of the note above the trigger to send as "
                         "conversation context; 0 disables (default: "
                         f"{DEFAULT_CONTEXT_CHARS})")
    ap.add_argument("--workdir", type=Path, default=None,
                    help="directory the agent runs in. Claude Code keys memory "
                         "and CLAUDE.md by cwd, so the default is a neutral "
                         "scratch dir: notes see nothing personal unless the "
                         "note opts in with `personal: true`")
    ap.add_argument("--new", nargs="?", const="", metavar="TITLE",
                    help="create an agent-enabled note and exit")
    ap.add_argument("--personal", action="store_true",
                    help="with --new: also set `personal: true` on the note")
    ap.add_argument("--allowed-tools", default=",".join(DEFAULT_ALLOWED_TOOLS),
                    help="comma-separated tools pre-approved for the agent. A "
                         "non-interactive agent cannot raise a permission prompt, "
                         "so anything unlisted is denied outright. Read-only by "
                         "default and bounded by --workdir; pass '' to deny all "
                         f"(default: {','.join(DEFAULT_ALLOWED_TOOLS)})")
    ap.add_argument("--no-seed", action="store_true",
                    help="skip the one-time explainer note")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    args = ap.parse_args()

    if args.new is not None:
        title = args.new.strip()
        body = f"# {title}\n\n" if title else ""
        path = create_note(args.notes_dir, body, personal=args.personal)
        print(path)
        return 0

    # Neutral by default: an empty dir has no Claude Code memory or CLAUDE.md,
    # so a sticky note cannot quietly read your personal context.
    workdir = args.workdir or (Path(
        os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share")
    ) / "waynote-agent/workdir")
    workdir.mkdir(parents=True, exist_ok=True)

    cmd = args.agent.split()
    allowed_tools = [t.strip() for t in args.allowed_tools.split(',') if t.strip()]
    streaming = ("stream-json" in " ".join(cmd)) and not args.no_stream
    if not args.notes_dir.is_dir():
        print(f"notes dir not found: {args.notes_dir}", file=sys.stderr)
        return 1
    print(f"watching {args.notes_dir} "
          f"({'streaming' if streaming else 'one-shot'}; agent cwd {workdir}; "
          f"tag a note `tags: [agent]`, then type `!question`)", flush=True)

    try:
        for p in recover_interrupted(args.notes_dir):
            print(f"recovered an interrupted question in {p.name}", flush=True)
    except Exception as e:
        print(f"recovery pass failed: {e}", file=sys.stderr, flush=True)

    if not args.no_seed:
        try:
            seeded = seed_welcome(args.notes_dir)
            if seeded:
                print(f"seeded the explainer note: {seeded.name}", flush=True)
        except Exception as e:
            print(f"could not seed explainer note: {e}", file=sys.stderr, flush=True)

    seen: dict[Path, float] = {}
    while True:
        for path in sorted(args.notes_dir.glob("*.md")):
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue
            # only look at files that have stopped changing
            if time.time() - mtime < SETTLE_SECONDS or seen.get(path) == mtime:
                continue
            try:
                if process(path, cmd, args.timeout, args.system_flag,
                           streaming, args.context_chars, workdir, allowed_tools):
                    print(f"answered a trigger in {path.name}", flush=True)
            except Exception as e:  # a bad note must not kill the daemon
                print(f"error on {path.name}: {e}", file=sys.stderr, flush=True)
            try:
                seen[path] = path.stat().st_mtime
            except FileNotFoundError:
                seen.pop(path, None)
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
