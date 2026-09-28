"""The session ledger: one SQLite file (WAL) written by hooks and the daemon.

One writer per fact: hooks write session state; the daemon writes focus, evictions,
requests and the heartbeat. Every write is a short BEGIN IMMEDIATE transaction.
"""
import contextlib
import json
import os
import sqlite3
import time

from . import (BUSY, ENDED, EVICTING, HIBERNATED, IDLE, PARKED_STATES, RESUMING,
               SCHEMA_VERSION, SUPERSEDED, WAITING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, pid INTEGER, pid_start TEXT, interactive INTEGER DEFAULT 1,
  tty TEXT, cwd TEXT, launch_cwd TEXT, transcript TEXT, title TEXT,
  state TEXT, state_since REAL, last_event TEXT, last_event_at REAL,
  subagents INTEGER DEFAULT 0, started_at REAL, ended_at REAL, end_reason TEXT,
  source TEXT, iterm_guid TEXT, last_focus_at REAL, resumed_at REAL, evicted_at REAL);
CREATE INDEX IF NOT EXISTS sessions_pid ON sessions(pid, pid_start);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, session_id TEXT,
  event TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS evictions(id INTEGER PRIMARY KEY, session_id TEXT, ts REAL,
  pid INTEGER, free_pct REAL, rss_mb REAL, signal TEXT, outcome TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY, ts REAL, kind TEXT,
  session_id TEXT, done_at REAL, result TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

EVENTS_KEEP_S = 30 * 86400
EVICTIONS_KEEP_S = 90 * 86400
SESSION_END_HIBERNATE_WINDOW_S = 60


class LedgerError(Exception):
    pass


def connect(path, readonly=False):
    if readonly:
        if not os.path.exists(path):
            raise LedgerError("no ledger at %s" % path)
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3.0,
                               isolation_level=None)
    else:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        conn = sqlite3.connect(path, timeout=3.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    if readonly:
        v = conn.execute("PRAGMA user_version").fetchone()[0]
        if v != SCHEMA_VERSION:
            raise LedgerError("schema version %s, expected %s" % (v, SCHEMA_VERSION))
        return conn
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        conn.execute("PRAGMA journal_mode=WAL")
        with tx(conn):
            if conn.execute("PRAGMA user_version").fetchone()[0] == 0:
                for stmt in SCHEMA.strip().split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    return conn


@contextlib.contextmanager
def tx(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def row_dict(r):
    return dict(r) if r is not None else None


def get(conn, sid):
    return row_dict(conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone())


def all_rows(conn, where="1=1", args=()):
    return [dict(r) for r in conn.execute("SELECT * FROM sessions WHERE " + where, args)]


def add_event(conn, sid, event, detail, now):
    conn.execute("INSERT INTO events(ts, session_id, event, detail) VALUES (?,?,?,?)",
                 (now, sid, event, json.dumps(detail, separators=(",", ":")) if detail else None))


def _set_state(fields, row, new_state, now):
    if row is None or row.get("state") != new_state:
        fields["state"] = new_state
        fields["state_since"] = now


def apply_event(conn, event, payload, proc, now=None):
    """Apply one hook event. `proc` is {pid, pid_start, tty, interactive} for the claude
    process that fired the hook, or None when it could not be found. Returns the new row."""
    now = time.time() if now is None else now
    sid = payload.get("session_id")
    if not sid or not isinstance(sid, str):
        raise ValueError("hook payload has no session_id")
    with tx(conn):
        row = get(conn, sid)
        fields = {"last_event": event, "last_event_at": now, "source": "hook"}
        if row is None:
            conn.execute("INSERT INTO sessions(session_id, state, state_since, started_at, "
                         "subagents, launch_cwd) VALUES (?,?,?,?,0,?)",
                         (sid, IDLE, now, now, payload.get("cwd")))
            row = get(conn, sid)
            new = True
        else:
            new = False
        if payload.get("cwd"):
            fields["cwd"] = payload["cwd"]
            if not row.get("launch_cwd"):
                fields["launch_cwd"] = payload["cwd"]
        if payload.get("transcript_path"):
            fields["transcript"] = payload["transcript_path"]
        if proc:
            fields["pid"] = proc.get("pid")
            fields["pid_start"] = proc.get("pid_start")
            if proc.get("tty"):
                fields["tty"] = proc["tty"]
            if proc.get("interactive") is not None:
                fields["interactive"] = 1 if proc["interactive"] else 0
        state = row.get("state")
        detail = {}
        if event == "SessionStart":
            src = payload.get("source") or "startup"
            detail["source"] = src
            fields["ended_at"] = None
            fields["end_reason"] = None
            if src in ("startup", "clear", "resume"):
                fields["subagents"] = 0
            if src in ("startup", "clear"):
                _set_state(fields, row, IDLE, now)
                if not new:
                    fields["started_at"] = now
            elif src == "resume":
                if state in PARKED_STATES:
                    fields["resumed_at"] = now
                _set_state(fields, row, IDLE, now)
            elif src == "compact":
                if state in (ENDED, SUPERSEDED, HIBERNATED, EVICTING, RESUMING, None):
                    _set_state(fields, row, IDLE, now)
            else:
                _set_state(fields, row, IDLE, now)
            if proc and proc.get("pid") and proc.get("pid_start"):
                others = conn.execute(
                    "SELECT session_id FROM sessions WHERE pid=? AND pid_start=? AND "
                    "session_id<>? AND ended_at IS NULL AND state NOT IN (?,?)",
                    (proc["pid"], proc["pid_start"], sid, HIBERNATED, EVICTING)).fetchall()
                for o in others:
                    conn.execute("UPDATE sessions SET state=?, state_since=?, ended_at=?, "
                                 "end_reason='superseded' WHERE session_id=?",
                                 (SUPERSEDED, now, now, o[0]))
                    add_event(conn, o[0], "superseded", {"by": sid}, now)
        elif event == "UserPromptSubmit":
            _set_state(fields, row, BUSY, now)
        elif event in ("Stop", "StopFailure"):
            _set_state(fields, row, IDLE, now)
        elif event == "SubagentStart":
            fields["subagents"] = (row.get("subagents") or 0) + 1
        elif event == "SubagentStop":
            fields["subagents"] = max(0, (row.get("subagents") or 0) - 1)
        elif event == "Notification":
            nt = payload.get("notification_type") or ""
            detail["type"] = nt
            if nt in ("permission_prompt", "elicitation_dialog"):
                _set_state(fields, row, WAITING, now)
            elif nt == "idle_prompt":
                subs = fields.get("subagents", row.get("subagents") or 0)
                if subs == 0 and state in (BUSY, WAITING, IDLE):
                    _set_state(fields, row, IDLE, now)
        elif event == "SessionEnd":
            reason = payload.get("reason") or "other"
            detail["reason"] = reason
            fields["end_reason"] = reason
            ev_at = row.get("evicted_at")
            if state == EVICTING or (ev_at and now - ev_at <= SESSION_END_HIBERNATE_WINDOW_S):
                _set_state(fields, row, HIBERNATED, now)
            else:
                _set_state(fields, row, ENDED, now)
                fields["ended_at"] = now
        cols = sorted(fields)
        conn.execute("UPDATE sessions SET %s WHERE session_id=?" % ", ".join("%s=?" % c for c in cols),
                     [fields[c] for c in cols] + [sid])
        add_event(conn, sid, event, detail, now)
        return get(conn, sid)


def state_of(conn, sid):
    r = conn.execute("SELECT state FROM sessions WHERE session_id=?", (sid,)).fetchone()
    return r[0] if r else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET "
                 "value=excluded.value", (key, str(value)))


def get_meta(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r[0] if r else None


def prune(conn, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("DELETE FROM events WHERE ts < ?", (now - EVENTS_KEEP_S,))
        conn.execute("DELETE FROM evictions WHERE ts < ?", (now - EVICTIONS_KEEP_S,))
        conn.execute("DELETE FROM requests WHERE done_at IS NOT NULL AND done_at < ?",
                     (now - EVENTS_KEEP_S,))


def add_request(conn, kind, sid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute("INSERT INTO requests(ts, kind, session_id) VALUES (?,?,?)",
                           (now, kind, sid))
        return cur.lastrowid


def evictions_today(conn, now=None):
    now = time.time() if now is None else now
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    out = {}
    for r in conn.execute("SELECT session_id, COUNT(*) FROM evictions WHERE ts >= ? AND "
                          "outcome <> 'aborted' GROUP BY session_id", (midnight,)):
        out[r[0]] = r[1]
    return out


def begin_eviction(conn, row, free, rss, now=None):
    """Compare-and-set: flip the row to evicting only if its state and last_event_at are
    exactly what the guards saw. Returns the evictions row id, or None when the row moved."""
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute(
            "UPDATE sessions SET state=?, state_since=?, evicted_at=? WHERE session_id=? AND "
            "state=? AND last_event_at IS ?",
            (EVICTING, now, now, row["session_id"], row["state"], row["last_event_at"]))
        if cur.rowcount != 1:
            return None
        cur = conn.execute("INSERT INTO evictions(session_id, ts, pid, free_pct, rss_mb, signal, "
                           "outcome) VALUES (?,?,?,?,?,?,?)",
                           (row["session_id"], now, row["pid"], free, rss, "SIGTERM", "started"))
        add_event(conn, row["session_id"], "evict", {"free": free}, now)
        return cur.lastrowid


def finish_eviction(conn, eviction_id, outcome, signal=None, detail=None):
    with tx(conn):
        if signal:
            conn.execute("UPDATE evictions SET outcome=?, signal=?, detail=? WHERE id=?",
                         (outcome, signal, detail, eviction_id))
        else:
            conn.execute("UPDATE evictions SET outcome=?, detail=? WHERE id=?",
                         (outcome, detail, eviction_id))


def rollback_eviction(conn, sid, prev_state, now=None):
    """Undo an evicting flip when the signal was never sent."""
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE sessions SET state=?, state_since=?, evicted_at=NULL WHERE "
                     "session_id=? AND state=?", (prev_state, now, sid, EVICTING))


def mark_hibernated(conn, sid, now=None, only_from=(EVICTING,)):
    now = time.time() if now is None else now
    with tx(conn):
        q = ",".join("?" * len(only_from))
        cur = conn.execute("UPDATE sessions SET state=?, state_since=? WHERE session_id=? AND "
                           "state IN (%s)" % q, (HIBERNATED, now, sid) + tuple(only_from))
        return cur.rowcount


def mark_resuming(conn, sid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        cur = conn.execute("UPDATE sessions SET state=?, state_since=? WHERE session_id=? AND "
                           "state IN (?,?,?)", (RESUMING, now, sid, HIBERNATED, EVICTING, RESUMING))
        if cur.rowcount:
            add_event(conn, sid, "resuming", None, now)
        return cur.rowcount


def set_focus(conn, sid, guid, now=None):
    now = time.time() if now is None else now
    with tx(conn):
        conn.execute("UPDATE sessions SET iterm_guid=?, last_focus_at=? WHERE session_id=?",
                     (guid, now, sid))


def set_guid(conn, sid, guid):
    with tx(conn):
        conn.execute("UPDATE sessions SET iterm_guid=? WHERE session_id=? AND "
                     "(iterm_guid IS NULL OR iterm_guid<>?)", (guid, sid, guid))
