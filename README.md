# cc-restore

Five small tools that give Claude Code CLI sessions a memory: hibernate the idle ones to free RAM, wake them on a click, and rebuild the whole window after a restart. macOS + iTerm2.

## The problem

Every live `claude` process costs roughly 600MB of RAM (node plus its MCP helpers), whether or not it is doing anything. Twenty tabs of long-running conversations is 12GB of a laptop gone to sessions that are mostly sitting idle, and the machine starts swapping. So you close some tabs, and then you need the conversation back and have to find the session id by hand.

Then the machine reboots. Every process dies at once. The conversations all still exist on disk, but the mapping from "tab in this window" to "session id in this directory" is gone, and restoring twenty of them by hand is an afternoon.

These tools treat a session like a browser tab: something you can drop from memory and get back later, without losing the conversation.

## What they are built on

Claude Code (2.1.x) writes one state file per live process to `~/.claude/sessions/<PID>.json`:

```json
{ "pid": 41988, "sessionId": "9aa2bb4f-...", "cwd": "/Users/you/Code/myapp",
  "kind": "interactive", "entrypoint": "cli", "status": "idle" }
```

It removes that file on a clean exit. Two facts fall out of this, and the whole suite rests on them:

- A file whose PID is dead is a session that did **not** exit cleanly, which after a reboot is exactly the set of sessions that were alive at shutdown. That is what `cc-restore` restores.
- A file whose PID is alive tells you the session id, the directory, and whether the session is `idle`, `waiting` or busy. That is what `cc-hibernate` reads to decide what is safe to end.

The other half is `claude --resume <session-id>`, which brings a conversation back with its full transcript. Everything below is bookkeeping around those two things.

## The five tools

### cc-restore

Rebuilds an iTerm2 window of `claude --resume` tabs after a restart or a crash. It reads the stale state files, skips anything already running, and drives iTerm2 via AppleScript to open one tab per session running `cd <cwd> && claude --resume <session-id>`.

```
$ cc-restore
source: ~/.claude/sessions state files (0 live, 23 stale)
  open 9aa2bb4f  ~/Code/myapp/.claude/worktrees/proud-sprouting-cocoa
       "after our recent upgrade, google login stopped working"
  open 267a938f  ~/Code/myapp
       "users gave feedback that the progress bar feels slow"
  ...
opened 23 tabs.
```

If no state files are usable (older Claude Code), it falls back to a heuristic: live sessions touch their transcript file at least hourly, so the newest cluster of transcript mtimes before boot time identifies the shutdown set.

```bash
cc-restore                # restore everything that died recently (default max age 7 days)
cc-restore --dry-run      # print the plan, open nothing
cc-restore --hours 12     # only sessions active in the last 12 hours
cc-restore --max-age-days 3
cc-restore --legacy       # force the transcript-mtime heuristic
cc-restore --auto         # boot-gated mode for launchd, see Install
cc-restore --limit 40     # safety cap on tabs (default 40)
```

Extras: a session that lived in a Claude Code worktree (`<repo>/.claude/worktrees/<name>`) that has since been swept is restored anyway, because the tab re-adds the worktree from its `worktree/<name>` branch before resuming. Dedup is process-aware, so a session already open anywhere (even one started as bare `claude`, with no id in its argv) is never resumed a second time, which would interleave two writers into one transcript.

### cc-hibernate

Frees RAM by ending idle sessions, resumably. It SIGKILLs the sessions it picks, writes a manifest to `~/.claude/state/hibernate/manifest-<ts>.json` (plus a `latest.json` symlink) recording session id, cwd, tty, iTerm2 tab GUID and title, resets the terminal modes Claude Code left on, prints a banner in the tab, renames the tab to `[hibernated] <title>`, and leaves `cd <cwd> && claude --resume <id>` typed but not submitted in the tab. Pressing Enter in that tab is a full resume.

```bash
cc-hibernate --dry-run                      # show what it would take, kill nothing
cc-hibernate --terminal iterm2 --idle-minutes 5 --idle-only --skip-visible
cc-hibernate --except <sid>[,<sid>]         # keep specific sessions
cc-hibernate --idle-hours 2
cc-hibernate --skip-visible                 # never touch the frontmost tab of any window
cc-hibernate --quiet                        # print only when something is hibernated
cc-hibernate --include-busy                 # also busy sessions (not recommended)
```

What it refuses to kill is the whole safety story, and it has its own section below.

### cc-wake

Brings hibernated sessions back. With no selector it lists what is hibernated, reading every manifest in `~/.claude/state/hibernate/`. Wakes happen one at a time: it types `cd <cwd> && claude --resume <id>` into the original iTerm2 tab when that tab still exists (matched by tty and tab GUID), else a new tab, then waits for the session to register itself in `~/.claude/sessions` before checking free memory and starting the next one.

```bash
cc-wake                       # list hibernated sessions (the default)
cc-wake --tty ttys033         # wake the session that lived in that tab
cc-wake --only <sid>[,<sid>]  # wake by session id, prefixes are fine
cc-wake --all [--limit N]     # wake everything, memory-gated
cc-wake --min-free-pct 20     # refuse to wake below this much free RAM (default 20)
cc-wake --settle-secs 120     # how long to wait for a session to register
cc-wake --dry-run
```

### cc-tabwatch

Makes waking a click instead of a command. It connects to iTerm2's Python API and watches which session has focus. Focus a tab whose tty appears unwoken in a manifest, and if that tab is sitting at a bare shell it types the resume command and hits return, renames the tab back to its real title, and marks the manifest entry woken.

Guards: it only acts on a focused session, at most once per tty per 20 seconds, never when that session id is already running, never when the tab GUID no longer matches the one recorded (the tty number was reused by a different tab), and never when free RAM is below the floor. Below the floor it prints a note in the tab instead of thrashing swap. The floor is an environment variable, not a flag: `CC_TABWATCH_MIN_FREE` (default 18, meaning 18 percent free). There are no command line options.

It needs the `iterm2` pip package. The published shebang is `#!/usr/bin/env python3`, and if that interpreter cannot import `iterm2` the script re-execs itself into one that can: `~/.claude/venvs/iterm2/bin/python` if it exists, else `$CC_ITERM2_PYTHON`. If neither works it exits with instructions rather than failing silently.

### cc-daemon

The supervisor that makes the other two automatic. Every 120 seconds it runs `cc-hibernate --terminal iterm2 --idle-minutes 5 --idle-only --skip-visible --quiet`, and it restarts `cc-tabwatch` whenever that has died.

```bash
cc-daemon             # start in the background if not already running
cc-daemon status      # is the daemon up, is cc-tabwatch up
cc-daemon stop        # stop both
cc-daemon --foreground
```

`CC_IDLE_MINUTES` overrides the 5 minute idle threshold. Logs go to `~/Library/Logs/cc-daemon.log`, `~/Library/Logs/cc-hibernate.log` and `~/Library/Logs/cc-tabwatch.log`.

It must be started from a shell running **inside iTerm2**. Everything here talks to iTerm2 through AppleScript and the iTerm2 Python API, and those calls inherit the macOS Automation grant of the process that started them. A copy spawned by launchd has no such grant and hangs on TCC. That is why cc-daemon is started from `~/.zshrc` rather than from a LaunchAgent, while cc-restore, which runs once at login before you have a terminal open, is the one that gets a LaunchAgent.

## Install

```bash
git clone https://github.com/shsunmoonlee/cc-restore
cd cc-restore
cp cc-restore cc-hibernate cc-wake cc-tabwatch cc-daemon ~/.local/bin/   # or anywhere on PATH
chmod +x ~/.local/bin/cc-*
```

Requirements: macOS, iTerm2, Claude Code 2.1+ (cc-restore also works on older versions via its fallback), Python 3.9+ (the system `python3` is fine for four of the five). `cc-restore`, `cc-hibernate` and `cc-wake` have no dependencies at all.

`cc-tabwatch` is the exception: it needs the `iterm2` package, which is not in the standard library.

```bash
python3 -m venv ~/.claude/venvs/iterm2
~/.claude/venvs/iterm2/bin/pip install iterm2
```

That is the path cc-tabwatch looks for on its own. Any other interpreter works too, as long as you point `CC_ITERM2_PYTHON` at it.

**One caveat about paths.** `cc-daemon` as shipped invokes its two children by absolute path: `$HOME/.claude/bin/cc-hibernate` and `$HOME/.claude/bin/cc-tabwatch`, the latter through `$HOME/.claude/venvs/iterm2/bin/python`. It does not search `PATH`. Either keep copies of the scripts in `~/.claude/bin/` as well, or edit those three paths in your copy of `cc-daemon`. The other four tools are location independent.

### Starting cc-daemon at login

Add this to `~/.zshrc`. It starts the daemon from the first iTerm2 shell after login, and does nothing in every shell after that, because `cc-daemon` exits immediately when it finds a live pidfile.

```bash
[[ "$TERM_PROGRAM" == "iTerm.app" ]] && cc-daemon >/dev/null 2>&1
```

### Restoring the window at login

iTerm2's own restoration brings back windows and scrollback after a reboot but can never revive processes, so the Chrome-like experience is a login agent that runs `cc-restore --auto`:

```xml
<!-- ~/Library/LaunchAgents/com.yourname.cc-restore.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>com.yourname.cc-restore</string>
	<key>ProgramArguments</key>
	<array>
		<string>/bin/sh</string>
		<string>-c</string>
		<string>sleep 10; exec /Users/YOU/.local/bin/cc-restore --auto</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardOutPath</key>
	<string>/Users/YOU/Library/Logs/cc-restore.log</string>
	<key>StandardErrorPath</key>
	<string>/Users/YOU/Library/Logs/cc-restore.log</string>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.yourname.cc-restore.plist
```

`--auto` is designed for exactly this: it only restores sessions that were killed by the shutdown or a crash (state files predating the current boot), never tabs you closed on purpose (clean exits delete their state file, and anything that died after boot is skipped), it runs at most once per boot (marker in `~/.cache/cc-restore.bootmark`), and it exits quietly when there is nothing to do. The first restore after a reboot may show one macOS dialog asking to allow control of iTerm2. Approve it once.

## How they fit together

```
   you work                cc-daemon every 120s                 you click the tab
      |                            |                                    |
   20 live tabs  --------->  cc-hibernate  --------->  [hibernated] tab -----> cc-tabwatch
   ~600MB each          SIGKILL + manifest entry       resume pre-typed        types it and
                                                                               hits return
                                                                                    |
                                                        cc-wake --all <-------------+
                                                     (the manual, batch path)
                                                                |
   reboot / crash ------> cc-restore ------> one window, one tab per session that was alive
```

One lifecycle, three entry points into the same `claude --resume` call:

1. **cc-daemon** runs in the background and calls **cc-hibernate** every two minutes. Anything idle for five minutes that passes the safety checks is killed and recorded in a manifest. Its tab stays open, renamed and pre-loaded with the exact command that brings it back.
2. When you want that session again you either click its tab, where **cc-tabwatch** notices the focus and submits the resume for you, or press Enter yourself on the pre-typed line, or run **cc-wake** to bring back one session, a tty, or everything at once with a memory gate between each.
3. When the machine restarts, hibernation manifests are not the right source any more because live sessions died too. **cc-restore** reads the state files instead and rebuilds the window.

The three data stores stay separate on purpose: `~/.claude/sessions/*.json` is Claude Code's own registry of what is alive, `~/.claude/state/hibernate/manifest-*.json` is this suite's record of what it killed and whether it has been woken, and the transcripts under `~/.claude/projects/` are the conversations themselves, which none of these tools ever write to.

## What cc-hibernate refuses to kill

Hibernation is only safe because the idle check is conservative. A session is a candidate only if the registry says `idle` (or `waiting`, unless `--idle-only`), and it is kept, with the reason printed, if any of the following is true:

- **It has a running child process.** Anything under the claude PID that is not a known helper (MCP servers, plugin binaries) means work is in flight: a Bash tool call in the foreground or the background, or the `caffeinate` process Claude Code starts as its own busy marker.
- **It has a background worker that outlived its shell.** Double-forked work (`nohup ... &`, `setsid`) hangs off launchd with no parent to find, so cc-hibernate also matches orphan processes running in the session's cwd or naming the session's scratchpad directory.
- **It or a subagent wrote a file recently.** The newest mtime across the session transcript, its subagent transcripts, and its background-task output files must be older than the idle threshold. This is what covers work with no child process at all: Agent and Workflow subagents, Monitor watches, `/loop` wakeups.
- **It has undelivered queued input.** Input that arrived while the session could not run (a task notification, a Monitor event, a message you typed) is logged in the transcript as a `queue-operation` and is not delivered until the next turn. SIGKILL would drop it silently, so a session with a non-empty queue is kept, for up to 24 hours.
- **It is waiting out a usage limit.** When a session hits a rate limit and Claude Code's "Continue automatically at usage limit" is on, the process parks itself until the reset and then carries on with the task, but the registry reads `idle` for the entire wait. cc-hibernate reads the reset timestamp from the transcript and keeps the session until 15 minutes past it. Killing during that wait is the one case where the session genuinely does not resume on its own.

On top of that, `--skip-visible` never touches the tab you are looking at in each window, `--except` takes explicit session ids, and a session that invokes cc-hibernate from inside itself is excluded automatically via `CLAUDE_SESSION_ID`. A session with no transcript yet (killed before its first message) is recorded as already woken, because there is nothing to resume and `claude --resume` would only say "No conversation found".

`--dry-run` prints the full keep and kill list with the reason for every decision, and is the right way to get comfortable with any of this.

## Bonus: always resume with the full transcript

Claude Code interrupts `--resume` on large or old sessions with a picker ("We recommend resuming from a summary"). If you always want the full conversation back with no prompt, either pick the hidden third option "Don't ask me again" once, or set it directly:

```bash
# writes the same flag the menu option writes
python3 -c "import json,pathlib; p=pathlib.Path.home()/'.claude.json'; d=json.loads(p.read_text()); d['resumeReturnDismissed']=True; p.write_text(json.dumps(d,indent=2))"
```

Belt and braces, since running claude processes can rewrite `~/.claude.json`: raise the trigger thresholds in `~/.claude/settings.json` so the prompt can never fire:

```json
{
  "env": {
    "CLAUDE_CODE_RESUME_THRESHOLD_MINUTES": "525600",
    "CLAUDE_CODE_RESUME_TOKEN_THRESHOLD": "999999999"
  }
}
```

This matters more once cc-tabwatch is running, because it types the resume command and hits return without a human there to answer a picker.

## Caveats

- **macOS and iTerm2 only.** Every tool drives iTerm2, through AppleScript (cc-restore, cc-hibernate, cc-wake) or the iTerm2 Python API (cc-tabwatch). cc-hibernate can identify sessions in Terminal.app, Ghostty and cmux, and cc-wake can open a cmux tab, but the tab banners, pre-typed resume and click-to-wake are iTerm2 features. Terminal.app and tmux are not supported. Ports welcome.
- **SIGKILL is deliberate, not laziness.** A graceful exit runs SessionEnd hooks, deletes the `~/.claude/sessions/<pid>.json` state file that cc-restore depends on to know the session existed, and on a clean worktree session Claude Code may remove the worktree. SIGKILL leaves the transcript, the worktree and the state file untouched, which is exactly what the resume needs. The terminal modes Claude Code leaves on (mouse reporting, kitty keyboard, bracketed paste) are reset on the tty afterwards, so the shell you are dropped back into behaves normally.
- **The formats these tools read are internal.** The session registry, the transcript `jsonl` format, the transcript heartbeat and the resume-prompt flag are all Claude Code implementation details, not a documented API. The tools read them tolerantly, skipping lines they cannot parse and falling back when a file is missing, but a Claude Code update can still change any of them. The stable core is `claude --resume <id>`.
- **Hibernation is safe only because of the keep checks.** See the section above. If you run cc-hibernate with `--include-busy`, none of those checks apply and you will lose queued input and interrupt running work.
- **Tab order and window layout are not preserved by cc-restore**, only the set of sessions. cc-hibernate and cc-wake do keep the tab, because the tab never closes.
- **`cc-daemon` must be started from inside iTerm2**, not from launchd, or its AppleScript calls hang waiting on a macOS Automation permission it cannot be granted.

## Alternatives

- [asadtariq96/cc-session-restore](https://github.com/asadtariq96/cc-session-restore): same idea as cc-restore, but requires a launchd agent snapshotting state before the crash. cc-restore reconstructs after the fact and needs nothing pre-installed.
- [timvw/tmux-assistant-resurrect](https://github.com/timvw/tmux-assistant-resurrect): the mature option if you live in tmux instead of iTerm2 tabs (pair with tmux-resurrect and tmux-continuum).
- iTerm2's native session restoration brings back window layout and scrollback after a reboot, but never the running processes. It composes well with cc-restore.

## License

MIT
