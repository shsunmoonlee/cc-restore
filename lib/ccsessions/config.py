"""Configuration: ~/.config/cc-sessions/config.json, every key optional.

CC_SESSIONS_CONFIG overrides the config path, CC_SESSIONS_DB overrides the db path.
A config file that exists but does not parse forces dry_run on (fail closed): a typo in
the file must never turn a dry-run daemon live.
"""
import json
import os

DEFAULT_CONFIG_PATH = "~/.config/cc-sessions/config.json"

DEFAULTS = {
    "db": "~/.claude/state/cc-sessions.db",
    "low_free_pct": 20,
    "high_free_pct": 35,
    "idle_min": 10,
    "cooldown_min": 45,
    "max_evictions_per_day": 3,
    "term_grace_s": 15,
    "tick_s": 30,
    # Child processes that do not count as work (MCP servers and their launchers).
    # `caffeinate` is deliberately NOT here: Claude Code runs it while a turn is busy.
    "helper_patterns": ["mcp", "npm exec ", "/.bin/", "-mcp"],
    "on_evict": None,
    "daemon_python": "~/.claude/venvs/iterm2/bin/python",
    "claude_projects": "~/.claude/projects",
    "claude_sessions": "~/.claude/sessions",
    "claude_settings": "~/.claude/settings.json",
    "claude_tmp": "/private/tmp/claude-{uid}",
    "log": "~/Library/Logs/cc-sessions.log",
    "sock_dir": "/tmp/cc-socks",
    "dry_run": False,
    "hibernate_keep_days": 60,
    "session_age_days": 30,
    "stale_busy_min": 30,
    "resume_timeout_s": 120,
    "settle_s": 10,
    "backoff_min": 10,
    "restore_limit": 40,
    "restore_max_age_days": 7,
    "bootmark": "~/.cache/cc-sessions.bootmark",
    # What gets typed into a tab to resume a session. null = the cc-resume installed
    # next to this package (bin/cc-resume).
    "resume_command": None,
    # Worktree parking (cc-resume). {repo_key} = the repo path with the leading / dropped
    # and every / replaced by -.
    "worktree_marker": "/.claude/worktrees/",
    "park_archive_dir": "~/.claude/repos/{repo_key}/archive",
    "park_branch_prefix": "worktree/",
    "park_commit_subject": "wip(auto-park)",
    # One-time import sources (import-legacy).
    "legacy_manifests": "~/.claude/state/hibernate",
    "launchd_plist": "~/Library/LaunchAgents/com.cc-sessions.daemon.plist",
}

PATH_KEYS = ("db", "daemon_python", "claude_projects", "claude_sessions", "claude_settings",
             "claude_tmp", "log", "sock_dir", "bootmark", "legacy_manifests", "launchd_plist",
             "on_evict")


def expand(p):
    if p is None:
        return None
    return os.path.expanduser(str(p).replace("{uid}", str(os.getuid())))


class Config(dict):
    error = None
    source = None

    def path(self, key):
        return expand(self.get(key))


def config_path():
    return expand(os.environ.get("CC_SESSIONS_CONFIG") or DEFAULT_CONFIG_PATH)


def load(path=None):
    cfg = Config(DEFAULTS)
    path = path or config_path()
    cfg.source = path
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("config root is not an object")
        cfg.update(data)
    except FileNotFoundError:
        pass
    except Exception as exc:  # unreadable or bad JSON: defaults, but never live
        cfg.error = "%s: %s" % (path, exc)
        cfg["dry_run"] = True
    if os.environ.get("CC_SESSIONS_DB"):
        cfg["db"] = os.environ["CC_SESSIONS_DB"]
    return cfg


def db_dir(cfg):
    return os.path.dirname(cfg.path("db")) or "."
