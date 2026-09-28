# cc-sessions

Session bookkeeping for [Claude Code](https://code.claude.com) on macOS + iTerm2:

- a **ledger** of every session's state, written by Claude Code's own hooks (no scraping);
- a **daemon** that frees memory under pressure by hibernating the least recently used idle
  sessions (graceful SIGTERM, recorded first) and resumes one when you click its tab;
- **restore** after a crash, power loss or shutdown;
- **holds**, so a worktree cleaner can ask "does any session still need this directory?".

Formerly `cc-restore`. See [Migrating from v1](#migrating-from-v1).

## How it works

```
Claude Code hooks (8 events) --> cc-sessions hook <Event> --> ~/.claude/state/cc-sessions.db
                                                                  ^        |
launchd (KeepAlive) --> cc_sessions_daemon.py -------------------+        +--> restore, holds,
   iTerm2 API: focus events, tab control; memory_pressure; evict; resume      status, doctor
```

One writer per fact: hooks write session state (busy, idle, waiting, ended); the daemon
writes focus, evictions, requests and its heartbeat. Everything else only reads.

### Session states

| state | set by |
|---|---|
| `idle` | SessionStart, Stop, StopFailure, Notification `idle_prompt` (when no subagents run) |
| `busy` | UserPromptSubmit |
| `waiting` | Notification `permission_prompt` / `elicitation_dialog` |
| `ended` | SessionEnd |
| `evicting` -> `hibernated` | the daemon, then SessionEnd (or the daemon after the grace period) |
| `resuming` | cc-resume / the daemon, until SessionStart(resume) turns it `idle` |
| `superseded` | SessionStart in the same process under a new id (`/clear`, in-app `/resume`) |

A process is identified by (pid, start time), never by pid alone.

### When does the daemon evict?

Only when free memory (`memory_pressure`) drops below `low_free_pct` (20%); it stops at
`high_free_pct` (35%). If free memory does not rise by at least one point after an eviction,
it stops for `backoff_min` (10 min): the memory is somewhere else, and `cc-sessions status`
shows where.

A session is a candidate only if every guard passes (each failure is logged as a keep
reason): written by hooks, interactive, idle for `idle_min` (10 min), no subagents, its
process identity still matches, its tty is an iTerm2 tab that is not the visible tab of any
window and was not focused in the last `idle_min`, not resumed in the last `cooldown_min`
(45 min), fewer than `max_evictions_per_day` (3) today, it has a transcript with a user
message, no transcript/subagent/task file changed in the last `idle_min`, no background
work in flight (async agents, background shells, monitors, queued input, a scheduled
wakeup, a usage-limit wait), and no child process other than MCP helpers. Candidates go in
order of least recent focus, then largest memory.

Eviction: compare-and-set the row to `evicting`, re-check the guards, SIGTERM, SIGKILL only
after `term_grace_s` (15 s, logged as an INCIDENT). The tab gets a banner, the title
`[zz] <title>`, and the resume command typed but not run. Focusing that tab later runs it.

## Install

Requirements: macOS, iTerm2 with the Python API enabled (Settings > General > Magic), the
system `/usr/bin/python3` (3.9+) for the CLI and hooks, and a python with the `iterm2`
package for the daemon:

```sh
python3 -m venv ~/.claude/venvs/iterm2 && ~/.claude/venvs/iterm2/bin/pip install iterm2
make install                       # PREFIX=~/.claude by default; nothing is started
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cc-sessions.daemon.plist
```

`make install` copies `bin/*` to `$PREFIX/bin`, the library to `$PREFIX/lib/cc-sessions/`,
and renders the launchd agent. `make check` exits 1 when an installed file differs from the
repo. `make uninstall` removes them.

Wire the hooks in `~/.claude/settings.json` (one entry per event):

```json
{ "hooks": {
  "SessionStart":     [{"hooks": [{"type": "command", "command": "cc-sessions hook SessionStart", "timeout": 5}]}],
  "SessionEnd":       [{"hooks": [{"type": "command", "command": "cc-sessions hook SessionEnd", "timeout": 5}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "cc-sessions hook UserPromptSubmit", "timeout": 5}]}],
  "Stop":             [{"hooks": [{"type": "command", "command": "cc-sessions hook Stop", "timeout": 5}]}],
  "StopFailure":      [{"hooks": [{"type": "command", "command": "cc-sessions hook StopFailure", "timeout": 5}]}],
  "SubagentStart":    [{"hooks": [{"type": "command", "command": "cc-sessions hook SubagentStart", "timeout": 5}]}],
  "SubagentStop":     [{"hooks": [{"type": "command", "command": "cc-sessions hook SubagentStop", "timeout": 5}]}],
  "Notification":     [{"hooks": [{"type": "command", "command": "cc-sessions hook Notification", "timeout": 5}]}]
} }
```

The hook always exits 0 and prints nothing; a failure is logged and leaves a marker that
blocks eviction of that session until its next successful hook.

Start with `"dry_run": true` in the config, watch `~/Library/Logs/cc-sessions.log`, then
turn it off. `cc-sessions doctor` checks the whole setup.

## Commands

```
cc-sessions status [--json]          sessions, idle ages, memory, top apps by RSS, heartbeat
cc-sessions state <sid>              the state word (exit 1 + "unknown" if not in the ledger)
cc-sessions hibernate <sid>|--all-idle   ask the daemon (explicit: skips pressure + idle time only)
cc-sessions wake <sid>|--all         resume in the original tab, or a new tab if it is gone
cc-sessions pending <transcript>     {"pending": [...], "last_text": "..."}; unreadable = pending
cc-sessions holds <path> [-v]        exit 0 held, 1 free, 2 ledger unreadable (treat != 1 as held)
cc-sessions restore [--auto] [--dry-run] [--limit N] [--include-hibernated]
cc-sessions import-legacy [--manifests DIR] [--registry DIR]
cc-sessions doctor                   PASS/FAIL checks, exit 1 on any FAIL
cc-sessions reap [--dry-run]         delete dead-pid sockets; list (never kill) orphans
cc-sessions daemon [--once] [--dry-run]   run the daemon in the foreground; --once prints
                                          the candidate table with keep reasons and exits
cc-resume <sid> [--cwd DIR] [--dry-run]   resume a session, unparking its worktree if needed
```

### restore

Restores sessions whose process is gone but whose SessionEnd never arrived (crash, power
loss), plus the shutdown cluster: sessions that ended with reason `other` together, right
before boot. A deliberate exit is never restored. `--auto` (for a login launchd job) runs at
most once per boot, only for sessions last active before boot, and also retypes (without
running) the resume command for hibernated sessions whose tab no longer exists.

### holds

For a worktree sweeper: a session holds `W` when its cwd (or launch cwd) is `W` or under
`W/` (a session in a parent directory does not), and it is alive, or hibernated within
`hibernate_keep_days` (60), or active within `session_age_days` (30), or a transcript for
`W` changed within `session_age_days`.

## Configuration

`~/.config/cc-sessions/config.json`, every key optional (`CC_SESSIONS_CONFIG` overrides the
path, `CC_SESSIONS_DB` the ledger). A config file that does not parse forces `dry_run` on.

| key | default | |
|---|---|---|
| `db` | `~/.claude/state/cc-sessions.db` | the ledger |
| `low_free_pct` / `high_free_pct` | 20 / 35 | evict below, stop at |
| `idle_min` | 10 | minutes idle, unfocused and without file activity |
| `cooldown_min` | 45 | no eviction this soon after a resume |
| `max_evictions_per_day` | 3 | per session |
| `term_grace_s` | 15 | SIGTERM to SIGKILL |
| `tick_s` | 30 | pressure check interval |
| `settle_s` / `backoff_min` | 10 / 10 | global backoff when an eviction frees nothing |
| `stale_busy_min` | 30 | busy this long + Esc-interrupted transcript = idle |
| `helper_patterns` | `["mcp", "npm exec ", "/.bin/", "-mcp"]` | child processes that are not work |
| `on_evict` | null | executable run after an eviction with `CC_SESSION_ID`, `CC_SESSION_CWD`, `CC_SESSION_TITLE`, `CC_EVICT_OUTCOME` |
| `daemon_python` | `~/.claude/venvs/iterm2/bin/python` | python with `iterm2` |
| `resume_command` | installed `cc-resume` | what gets typed into tabs |
| `worktree_marker` | `/.claude/worktrees/` | worktree folder marker (cc-resume) |
| `park_archive_dir` | `~/.claude/repos/{repo_key}/archive` | where a sweeper archived a parked worktree |
| `park_branch_prefix` / `park_commit_subject` | `worktree/` / `wip(auto-park)` | how a parked worktree is recognized |
| `hibernate_keep_days` / `session_age_days` | 60 / 30 | holds windows |
| `log` | `~/Library/Logs/cc-sessions.log` | rotated 1 MB x 3 |
| `dry_run` | false | log "would evict" and never act |

## Development

```sh
make test            # /usr/bin/python3 -m unittest discover -s tests
make release-check   # CC_SESSIONS_PRIVATE_STRINGS=<file with one string per line>
```

Tests use temp ledgers and never signal processes or talk to iTerm2.

## Migrating from v1

v1 was five scripts (`cc-restore`, `cc-hibernate`, `cc-wake`, `cc-tabwatch`, `cc-daemon`)
that inferred session state from Claude Code's internal registry and transcripts, killed
idle sessions with SIGKILL on a timer, and kept hibernation manifests as JSON files.

1. `make install`, wire the hooks, `cc-sessions import-legacy` (imports un-woken
   hibernation manifests and live registry entries; idempotent).
2. Replace a `cc-restore --auto` launchd job with `cc-sessions restore --auto`.
3. Load the daemon agent in dry-run, then live.
4. Stop and remove `cc-daemon`, `cc-hibernate`, `cc-tabwatch`, `cc-wake`, `cc-restore` and
   any shell-profile line that started them.

Dropped: the transcript-mtime `--legacy` restore heuristic, and timer-based eviction.

## License

MIT
