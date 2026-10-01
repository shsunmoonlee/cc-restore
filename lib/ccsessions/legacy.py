"""`cc-sessions import-legacy`: one-time import of v1 state into the ledger.

  - v1 hibernate manifests (manifest-*.json): entries not woken, not empty, and whose
    transcript was not written after killedAt + 60 s (a later write = resumed by hand)
    become hibernated rows (source 'legacy').
  - Claude Code registry files (<sessions dir>/*.json) whose process is alive become rows
    with the registry status as state (source 'legacy').
Idempotent: a row written by a hook (source 'hook') is never overwritten.
"""
import glob
import json
import os
import time

from . import BUSY, HIBERNATED, IDLE, WAITING, procs
from .ledger import add_event, get, tx

RESUMED_SLACK_S = 60


def parse_killed_at(s):
    try:
        return time.mktime(time.strptime(s, "%Y%m%d-%H%M%S"))
    except (TypeError, ValueError):
        return None


def find_transcript(projects, sid):
    ts = glob.glob(os.path.join(projects, "*", "%s.jsonl" % sid))
    if not ts:
        return None
    return max(ts, key=lambda p: os.path.getmtime(p))


def _upsert(conn, sid, fields, now, stats):
    row = get(conn, sid)
    if row is not None and row.get("source") != "legacy":
        stats["skipped_hook"] += 1
        return
    if row is None:
        conn.execute("INSERT INTO sessions(session_id, source, subagents) VALUES (?, 'legacy', 0)",
                     (sid,))
        stats["inserted"] += 1
    else:
        stats["updated"] += 1
    cols = sorted(fields)
    conn.execute("UPDATE sessions SET %s WHERE session_id=?" % ", ".join("%s=?" % c for c in cols),
                 [fields[c] for c in cols] + [sid])
    add_event(conn, sid, "import-legacy", {"state": fields.get("state")}, now)


def manifest_entries(manifest_dir, projects):
    """{sid: fields} for the newest un-woken entry per session."""
    best = {}
    for p in sorted(glob.glob(os.path.join(manifest_dir, "manifest-*.json"))):
        try:
            with open(p) as fh:
                entries = json.load(fh).get("entries") or []
        except (OSError, ValueError, AttributeError):
            continue
        for e in entries:
            if not isinstance(e, dict) or e.get("woken") is True or e.get("empty"):
                continue
            sid, killed = e.get("sessionId"), parse_killed_at(e.get("killedAt"))
            if not sid or killed is None:
                continue
            t = find_transcript(projects, sid)
            if t is None:
                continue
            try:
                if os.path.getmtime(t) > killed + RESUMED_SLACK_S:
                    continue
            except OSError:
                continue
            if sid in best and best[sid]["evicted_at"] >= killed:
                continue
            cwd = e.get("cwd")
            best[sid] = {
                "state": HIBERNATED, "state_since": killed, "evicted_at": killed,
                "last_event_at": killed, "last_event": "import-legacy",
                "iterm_guid": e.get("itermId"), "tty": procs.norm_tty(e.get("tty")),
                "cwd": cwd, "launch_cwd": cwd, "title": e.get("title") or e.get("name"),
                # the killed pid is dead and has no recorded start time; keeping it would
                # let a reused pid pass the identity check
                "transcript": t, "pid": None, "pid_start": None, "interactive": 1,
                "ended_at": None,
            }
    return best


def registry_entries(sessions_dir, projects, info_fn):
    """info_fn(pid) -> {pid_start, tty, comm} for a live process, else None."""
    out = {}
    for p in glob.glob(os.path.join(sessions_dir, "*.json")):
        try:
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if not isinstance(pid, int) or not sid:
            continue
        info = info_fn(pid)
        if not info or not procs.is_claude_comm(info.get("comm")):
            continue
        lstart, tty = info["pid_start"], info.get("tty")
        status = d.get("status")
        state = status if status in (BUSY, IDLE, WAITING) else IDLE
        now = time.time()
        out[sid] = {
            "state": state, "state_since": (d.get("statusUpdatedAt") or 0) / 1000.0 or now,
            "last_event_at": (d.get("updatedAt") or 0) / 1000.0 or now,
            "last_event": "import-legacy", "pid": pid, "pid_start": lstart, "tty": tty,
            "cwd": d.get("cwd"), "launch_cwd": d.get("cwd"), "title": d.get("name"),
            "transcript": find_transcript(projects, sid),
            "interactive": 1 if (d.get("kind") == "interactive" and d.get("entrypoint") == "cli") else 0,
            "started_at": (d.get("startedAt") or 0) / 1000.0 or None, "ended_at": None,
        }
    return out


def live_info(pid):
    if not procs.pid_alive(pid):
        return None
    lc = procs.lstart_comm(pid)
    info = procs.proc_info(pid)
    if not lc or not info:
        return None
    return {"pid_start": lc[0], "comm": lc[1], "tty": info["tty"]}


def import_legacy(conn, cfg, manifest_dir=None, sessions_dir=None, info_fn=None, now=None):
    now = time.time() if now is None else now
    projects = cfg.path("claude_projects")
    manifest_dir = manifest_dir or cfg.path("legacy_manifests")
    sessions_dir = sessions_dir or cfg.path("claude_sessions")
    stats = {"inserted": 0, "updated": 0, "skipped_hook": 0}
    hib = manifest_entries(manifest_dir, projects)
    live = registry_entries(sessions_dir, projects, info_fn or live_info)
    with tx(conn):
        for sid, f in hib.items():
            if sid in live:
                continue  # alive again: the registry row wins
            _upsert(conn, sid, f, now, stats)
        for sid, f in live.items():
            _upsert(conn, sid, f, now, stats)
    stats["hibernated"] = len([s for s in hib if s not in live])
    stats["live"] = len(live)
    return stats
