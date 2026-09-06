# cc-restore

Reopen all your Claude Code CLI sessions after a MacBook restart. One iTerm2 window, one tab per session, each resuming with its full transcript.

You had 20 terminal tabs, each running a long-lived `claude` conversation. The machine rebooted (or you closed the window). The processes are gone, but every conversation still exists on disk. `cc-restore` figures out exactly which sessions were alive when the machine went down and brings them all back:

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

## How it works

Claude Code (2.1.x) writes one state file per live process to `~/.claude/sessions/<PID>.json` containing the session id and working directory, and removes it on clean exit. After a reboot or crash, the files whose PID is dead are exactly the sessions that were alive at shutdown. `cc-restore` reads them, skips anything already running, and drives iTerm2 via AppleScript to open a tab per session running `cd <cwd> && claude --resume <session-id>`.

If no state files exist (older Claude Code), it falls back to a heuristic: live sessions touch their transcript file at least hourly, so the newest cluster of transcript mtimes before boot time identifies the shutdown set.

Extras:

- Sessions that lived in a Claude Code worktree (`<repo>/.claude/worktrees/<name>`) that has since been swept are restored anyway: the tab re-adds the worktree from its `worktree/<name>` branch before resuming.
- Dedup is process-aware. A session already open anywhere (even one started as bare `claude`, with no id in its argv) is never resumed a second time, which would interleave two writers into one transcript.

## Install

```bash
git clone https://github.com/shsunmoonlee/cc-restore
cp cc-restore/cc-restore ~/.local/bin/   # or anywhere on PATH
chmod +x ~/.local/bin/cc-restore
```

Requirements: macOS, iTerm2, Claude Code 2.1+ (older versions work via the fallback), Python 3.9+ (system python3 is fine). No dependencies, no daemon, nothing to run before the crash.

## Usage

```bash
cc-restore                # restore everything that died recently (default max age 7 days)
cc-restore --dry-run      # print the plan, open nothing
cc-restore --hours 12     # only sessions active in the last 12 hours
cc-restore --max-age-days 3
cc-restore --legacy       # force the transcript-mtime heuristic
cc-restore --auto         # boot-gated mode for launchd, see below
cc-restore --limit 40     # safety cap on tabs (default 40)
```

Run it soon after boot, ideally before starting new Claude work. Or make it automatic:

## Automatic restore at login (Chrome-style)

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

`--auto` is designed for exactly this: it only restores sessions that were killed by the shutdown or a crash (state files predating the current boot), never tabs you closed on purpose (clean exits delete their state file, and anything that died after boot is skipped), it runs at most once per boot (marker in `~/.cache/cc-restore.bootmark`), and it exits quietly when there is nothing to do. The first restore after a reboot may show one macOS dialog asking to allow control of iTerm2; approve it once.

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

## Caveats

- The state files, the transcript heartbeat, and the resume-prompt flag are internal Claude Code behavior, not documented API. Tested against Claude Code 2.1.251; a future release could change any of them. The fallback heuristic and the explicit `--resume <id>` contract are the stable core.
- Tab order and window layout are not preserved, only the set of sessions.
- iTerm2 only. Terminal.app and other emulators are not supported (PRs welcome).

## Alternatives

- [asadtariq96/cc-session-restore](https://github.com/asadtariq96/cc-session-restore): same idea, but requires a launchd agent snapshotting state before the crash. cc-restore reconstructs after the fact and needs nothing pre-installed.
- [timvw/tmux-assistant-resurrect](https://github.com/timvw/tmux-assistant-resurrect): the mature option if you live in tmux instead of iTerm2 tabs (pair with tmux-resurrect and tmux-continuum).
- iTerm2's native session restoration brings back window layout and scrollback after a reboot, but never the running processes. It composes well with cc-restore.

## License

MIT
