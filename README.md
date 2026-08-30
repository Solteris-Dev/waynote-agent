# waynote-agent

Turn a [waynote](https://github.com/mryll/waynote) sticky note into a live agent
transcript on your desktop.

waynote renders notes as `wlr-layer-shell` surfaces and reconciles external file
edits live. That makes a note a two-way surface: anything that writes to the
`.md` shows up on screen immediately. This daemon is the other half — it watches
the same files, spots a trigger line, asks an agent, and writes the reply back.

No waynote plugin API, no patches to waynote, no Rust.

## Use

Tag a note in waynote's own frontmatter (waynote preserves `tags`):

```yaml
tags: [agent]
```

Then type a line starting with `!` in the note:

```
!what port does the terraria server use?
```

The trigger is replaced in place, so it can never fire twice:

```
> **?** what port does the terraria server use?

198.51.100.7
```

Untagged notes are ignored entirely.

## Making an agentic note

```sh
./waynote_agent.py --new "title"          # prints the path
./waynote_agent.py --new "title" --personal
```

Bound in Hyprland as Super+Shift+N (plain `waynote new` is Super+N).

## Privacy: personal context is opt-in

Claude Code keys its memory and `CLAUDE.md` by working directory. By default the
agent runs in an empty scratch dir (`~/.local/share/waynote-agent/workdir`), so
a sticky note **cannot** read your personal context — a note that asks "name a
file in my memory directory" gets nothing.

To let one note see it, add to that note's frontmatter:

```
personal: true
```

which runs the agent from `$HOME` instead. That's what lets a note answer
questions about your own machine — useful for a scratchpad, wrong for a
shopping list, so it's per note rather than global. `--workdir` overrides the
default location.

## Threads

The note *is* the conversation. Everything above the trigger is sent as context,
so follow-ups resolve — "which of those would you fix first?" works.

Two bangs start fresh, dragging nothing along:

```
!!an unrelated question
```

Because the context is the note rather than a hidden session id, you steer it by
editing: delete an answer you didn't like and it stops influencing the thread.
That also means there is no session to expire and nothing to get out of sync
with what's on screen. Context is capped by `--context-chars` (default 6000,
tail-biased); `--context-chars 0` restores one-shot behaviour.

## Run

```sh
./waynote_agent.py                          # watch forever
./waynote_agent.py --once                   # single pass, useful for testing
./waynote_agent.py --agent 'ollama run x'   # any command; prompt is appended
```

Or as a systemd user unit (`waynote-agent.service`), started from
`hyprland.lua` alongside waybar.

Options: `--notes-dir` (default `~/.local/share/waynote/notes`), `--timeout`
(default 180s), `--agent`, `--system-flag`.

## Per-note personas

Any note can carry its own system prompt, because waynote round-trips unknown
frontmatter keys untouched:

```yaml
tags: [agent]
system: you are a terse sysadmin; answer in one line
```

Passed via `--append-system-prompt`. For agents without such a flag, use
`--system-flag ''` and it gets prepended to the prompt instead.

## Design notes

- **Stdlib only.** Polls mtimes at 0.5s instead of pulling in a watcher dep.
- **No feedback loop.** The trigger line is rewritten *before* the agent runs, so
  the daemon's own writes can't re-fire it.
- **Settle window.** A file untouched for less than 1.5s is skipped, so it won't
  fire mid-typing.
- **Frontmatter is never rewritten** — only the body is touched.
- **Streaming.** If the agent command mentions `stream-json`, `text_delta`
  events are accumulated and the note is rewritten every 350ms with a `▌`
  caret, so you watch the reply type itself on the desktop. The final
  `result` field is authoritative and replaces the accumulated text. Disable
  with `--no-stream`; agents that don't stream fall back automatically.
- **Progressive writes are unambiguous.** The daemon remembers exactly what it
  last wrote and swaps that string, rather than guessing where the answer sits.
  If the text it wrote is gone (you edited the note mid-reply) it stops
  touching the note instead of corrupting it.
- **Structured output is unwrapped.** Agent CLIs log diagnostics to *stdout*
  (claude interleaves MCP warnings), which would otherwise be written into the
  note as if the agent had said them. The default agent uses
  `--output-format json` and only the `result` field is used; anything that
  isn't JSON-with-a-result passes through untouched, so plain-text agents work.
- **Atomic writes** via `os.replace`. waynote makes conflict copies rather than
  overwriting, so a simultaneous edit is recoverable either way.
- Works alongside Obsidian: point waynote's `notes_dir` at a vault folder and
  the same files are editable from both.

## Ideas

- Per-note system prompt via a frontmatter key (waynote passes unknown keys through)
- Stream the reply in progressively instead of one write
- Different trigger per tag (`tags: [agent, shell]`)

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
