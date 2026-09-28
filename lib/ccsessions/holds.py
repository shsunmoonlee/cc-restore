"""`cc-sessions holds <path>`: does any session still need this directory?

Exit 0 = held, 1 = nobody holds it, 2 = ledger unreadable. Callers (a worktree sweeper)
must treat every exit other than 1 as "held".

A row matches W when its cwd or launch_cwd (both realpath'd) equals W or lies under
W + "/". A session in a PARENT of W does not hold W.
"""
import glob
import os
import re
import time

from . import PARKED_STATES, procs
from .ledger import LedgerError, all_rows, connect

HELD, FREE, UNREADABLE = 0, 1, 2


def encode_project_dir(path):
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def path_matches(row_path, w):
    if not row_path:
        return False
    rp = os.path.realpath(row_path)
    return rp == w or rp.startswith(w.rstrip("/") + "/")


def row_holds(row, now, cfg, alive_fn):
    """Reason string when this row holds its cwd, else None."""
    keep_s = float(cfg["hibernate_keep_days"]) * 86400
    age_s = float(cfg["session_age_days"]) * 86400
    if row.get("ended_at") is None and alive_fn(row):
        return "live session %s" % row["session_id"][:8]
    if row.get("state") in PARKED_STATES:
        ev = row.get("evicted_at")
        if ev is None or now - ev <= keep_s:
            return "hibernated session %s" % row["session_id"][:8]
    le = row.get("last_event_at")
    if le is not None and now - le <= age_s:
        return "session %s active within %sd" % (row["session_id"][:8], cfg["session_age_days"])
    return None


def transcript_recent(path, cfg, now):
    age_s = float(cfg["session_age_days"]) * 86400
    projects = cfg.path("claude_projects")
    for p in {os.path.abspath(path), os.path.realpath(path)}:
        for t in glob.glob(os.path.join(projects, encode_project_dir(p), "*.jsonl")):
            try:
                if now - os.path.getmtime(t) <= age_s:
                    return "transcript active within %sd" % cfg["session_age_days"]
            except OSError:
                continue
    return None


def holds(path, cfg, now=None, alive_fn=None):
    """(code, reason)."""
    now = time.time() if now is None else now
    w = os.path.realpath(path)
    try:
        conn = connect(cfg.path("db"), readonly=True)
        rows = all_rows(conn)
        conn.close()
    except (LedgerError, Exception) as exc:
        return UNREADABLE, "ledger unreadable: %s" % exc
    if alive_fn is None:
        alive_fn = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
    for r in rows:
        if not (path_matches(r.get("cwd"), w) or path_matches(r.get("launch_cwd"), w)):
            continue
        why = row_holds(r, now, cfg, alive_fn)
        if why:
            return HELD, why
    why = transcript_recent(path, cfg, now)
    if why:
        return HELD, why
    return FREE, "no session holds %s" % w
