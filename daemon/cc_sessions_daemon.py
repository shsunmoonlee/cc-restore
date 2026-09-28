#!/usr/bin/env python3
"""cc-sessions daemon: pressure-driven, LRU-by-focus hibernation of idle Claude Code
sessions in iTerm2, with resume-on-focus.

    cc_sessions_daemon.py                    run forever (launchd KeepAlive supervises it)
    cc_sessions_daemon.py --once --dry-run   connect, print the candidate table, exit

Needs a python with the `iterm2` package. The policy (`choose_candidates`) and the
eviction sequence (`Evictor`) are plain Python, importable and testable without iterm2.

Loop: every tick_s read free memory; below low_free_pct evict the best candidate, let it
settle, re-read, and continue until high_free_pct or no candidate. Eviction is recorded
first (compare-and-set to 'evicting'), then SIGTERM, then SIGKILL after term_grace_s.
Focusing a hibernated tab types the resume command into it.
"""
import asyncio
import fcntl
import glob
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import time


def _libdir():
    here = os.path.dirname(os.path.realpath(__file__))
    for c in (here, os.path.join(here, "..", "lib")):
        if os.path.isdir(os.path.join(c, "ccsessions")):
            return os.path.normpath(c)
    return None


_LIB = _libdir()
if _LIB and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from ccsessions import (BUSY, EVICTING, HIBERNATED, IDLE, LIVE_STATES,  # noqa: E402
                        RESUMING, config, ledger, pending, procs, resumecmd)

log = logging.getLogger("cc-sessions")

RESUME_DEBOUNCE_S = 20
UNGUIDED_RESUME_MAX_AGE_S = 24 * 3600
SESSION_END_WAIT_S = 5
TAB_SHELL_WAIT_S = 5
REQUEST_POLL_S = 5
REAP_EVERY_S = 3600
PRUNE_EVERY_S = 3600
ON_EVICT_TIMEOUT_S = 30
MAX_ABORTS_PER_PASS = 3
TTY_RESET = ("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1005l\x1b[?1006l\x1b[?1015l"
             "\x1b[?1004l\x1b[?2004l\x1b[?1049l\x1b[?25h\x1b[<u\x1b[?7h\x1b[0m\x1b>")


# ------------------------------------------------------------------ pure policy

def _mins(s):
    return int(max(0, s) / 60)


def _as_analysis(v):
    if isinstance(v, dict):
        return v
    return {"pending": list(v or []), "has_user": True}


def guard_reason(row, now, cfg, focus_state, pending_fn, children_fn, identity_fn=None,
                 activity_fn=None, evictions_today=None, hook_failed=None, explicit=False):
    """Why this row must be kept, or None when it may be evicted. Cheap checks first; the
    transcript, file-activity and process-tree checks run only for rows that got that far."""
    sid = row["session_id"]
    idle_s_min = float(cfg["idle_min"]) * 60
    if row.get("source") != "hook":
        return "source=%s" % row.get("source")
    if row.get("interactive") == 0:
        return "non-interactive"
    if hook_failed and sid in hook_failed:
        return "a hook failed to record (marker)"
    st = row.get("state")
    in_state = now - (row.get("state_since") or now)
    stale_busy = False
    if st == BUSY and in_state > float(cfg["stale_busy_min"]) * 60:
        stale_busy = True  # confirmed below from the transcript (Esc-interrupt, no Stop)
    elif st != IDLE:
        return "state=%s" % st
    if not explicit and not stale_busy and in_state < idle_s_min:
        return "idle %dm" % _mins(in_state)
    if (row.get("subagents") or 0) > 0:
        return "subagents=%d" % row["subagents"]
    tty = row.get("tty")
    if not tty or tty not in (focus_state.get("tty_guid") or {}):
        return "no iTerm2 tab for %s" % (tty or "no tty")
    if tty in (focus_state.get("visible_ttys") or set()):
        return "visible tab"
    lf = row.get("last_focus_at")
    if lf and now - lf < idle_s_min:
        return "focused %dm ago" % _mins(now - lf)
    ra = row.get("resumed_at")
    if ra and now - ra < float(cfg["cooldown_min"]) * 60:
        return "cooldown (resumed %dm ago)" % _mins(now - ra)
    n = (evictions_today or {}).get(sid, 0)
    if n >= int(cfg["max_evictions_per_day"]):
        return "evicted %dx today" % n
    if identity_fn is not None:
        why = identity_fn(row)
        if why:
            return why
    tr = row.get("transcript")
    if not tr or not os.path.isfile(tr):
        return "no transcript"
    if activity_fn is not None:
        newest = activity_fn(row)
        if newest is None:
            return "activity unreadable"
        if now - newest < idle_s_min:
            return "active %dm ago (files)" % _mins(now - newest)
    a = _as_analysis(pending_fn(row))
    if not a.get("has_user"):
        return "no user message"
    if a.get("pending"):
        return "pending: " + ", ".join(a["pending"])[:60]
    if stale_busy and not pending.interrupted_last(a):
        return "state=busy"
    kids = children_fn(row)
    if kids:
        return "running: " + ", ".join(sorted(set(kids))[:3])
    return None


def order_key(row, rss):
    lf = row.get("last_focus_at")
    return (0 if lf is None else 1, lf or 0, -float((rss or {}).get(row["session_id"], 0)))


def choose_candidates(rows, now, cfg, focus_state, rss, pending_fn, children_fn,
                      identity_fn=None, activity_fn=None, evictions_today=None,
                      hook_failed=None, explicit=False):
    """-> (candidates in eviction order, [(row, keep_reason)])."""
    cands, kept = [], []
    for r in rows:
        why = guard_reason(r, now, cfg, focus_state, pending_fn, children_fn, identity_fn,
                           activity_fn, evictions_today, hook_failed, explicit)
        if why:
            kept.append((r, why))
        else:
            cands.append(r)
    cands.sort(key=lambda r: order_key(r, rss))
    return cands, kept


def resume_decision(row, guid, tty, now, last_fire, job, identity_alive):
    """Why a focused tab must NOT resume this hibernated row, or None to go ahead."""
    if row.get("state") != HIBERNATED:
        return "state=%s" % row.get("state")
    if now - last_fire.get(guid, 0) < RESUME_DEBOUNCE_S:
        return "debounce"
    if row.get("iterm_guid"):
        if row["iterm_guid"] != guid:
            return "tab GUID changed (tty reused by another tab)"
    else:
        if row.get("tty") != tty:
            return "tty mismatch"
        ev = row.get("evicted_at")
        if not ev or now - ev > UNGUIDED_RESUME_MAX_AGE_S:
            return "no tab GUID and hibernated over a day ago; use cc-sessions wake"
    if job is not None and job not in procs.SHELL_JOBS:
        return "tab is running %s" % job
    if identity_alive(row):
        return "already running"
    return None


def is_shell_job(job):
    return job in procs.SHELL_JOBS


# ------------------------------------------------------------------ default fact sources

def default_identity(cfg):
    sessions_dir = cfg.path("claude_sessions")

    def check(row):
        if not row.get("pid_start"):
            return "no recorded process start time"
        if not procs.identity_alive(row.get("pid"), row["pid_start"]):
            return "process gone or pid reused"
        reg = os.path.join(sessions_dir, "%s.json" % row["pid"])
        if os.path.exists(reg):
            try:
                with open(reg) as fh:
                    got = json.load(fh).get("sessionId")
            except Exception:
                return "registry file unreadable"
            if got != row["session_id"]:
                return "registry names another session"
        return None
    return check


def default_activity(cfg):
    tmp = cfg.path("claude_tmp")

    def newest(row):
        sid, tr = row["session_id"], row.get("transcript")
        paths = []
        if tr:
            paths.append(tr)
            if tr.endswith(".jsonl"):
                paths += glob.glob(tr[:-6] + "/subagents/*.jsonl")
        paths += glob.glob(os.path.join(tmp, "*", sid, "tasks", "*"))
        best = None
        for p in paths:
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            best = m if best is None else max(best, m)
        return best
    return newest


def hook_failed_sids(cfg):
    try:
        return set(os.listdir(os.path.join(config.db_dir(cfg), "hook-failed")))
    except OSError:
        return set()


class Facts(object):
    """One snapshot of the slow facts (process table, transcripts) for a policy pass."""

    def __init__(self, cfg, now=None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self.cmds = procs.ps_commands()
        self._analysis = {}

    def analysis(self, row):
        sid = row["session_id"]
        if sid not in self._analysis:
            self._analysis[sid] = pending.analyze(row.get("transcript"), self.now)
        return self._analysis[sid]

    def children(self, row):
        return procs.working_children(row["pid"], self.cmds, self.cfg["helper_patterns"])

    def rss(self, rows):
        return {r["session_id"]: procs.tree_rss_mb(r["pid"], self.cmds) for r in rows if r.get("pid")}


def policy_pass(conn, cfg, focus_state, rows=None, explicit=False, now=None):
    now = time.time() if now is None else now
    if rows is None:
        rows = ledger.all_rows(conn, "ended_at IS NULL AND state IN (?,?,?)", LIVE_STATES)
    facts = Facts(cfg, now)
    rss = facts.rss(rows)
    cands, kept = choose_candidates(
        rows, now, cfg, focus_state, rss, facts.analysis, facts.children,
        identity_fn=default_identity(cfg), activity_fn=default_activity(cfg),
        evictions_today=ledger.evictions_today(conn, now), hook_failed=hook_failed_sids(cfg),
        explicit=explicit)
    return cands, kept, rss, facts


def title_for(row, facts=None):
    t = row.get("title")
    if not t and facts is not None:
        t = facts.analysis(row).get("title")
    return t or os.path.basename(row.get("cwd") or "") or row["session_id"][:8]


# ------------------------------------------------------------------ eviction sequence

class Evictor(object):
    """Record first, SIGTERM, SIGKILL after the grace period, then leave the tab ready to
    resume. `tabs` is an object with async job_name(guid), inject(guid, bytes),
    set_name(guid, name), send_text(guid, text). Everything is injectable for tests."""

    def __init__(self, conn, cfg, tabs, kill=os.kill, identity=None, clock=time.time,
                 sleep=asyncio.sleep, on_evict=None):
        self.conn, self.cfg, self.tabs = conn, cfg, tabs
        self.kill, self.clock, self.sleep = kill, clock, sleep
        self.identity = identity or (lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start")))
        self.on_evict = on_evict

    async def evict(self, row, free, rss_mb, recheck=None, title=None):
        sid = row["session_id"]
        if recheck is not None:
            why = recheck(row)
            if asyncio.iscoroutine(why):
                why = await why
            if why:
                log.info("keep %s at re-check: %s", sid[:8], why)
                return "aborted-recheck"
        eid = ledger.begin_eviction(self.conn, row, free, rss_mb, self.clock())
        if eid is None:
            log.info("keep %s: row changed since the guards ran (compare-and-set lost)", sid[:8])
            return "aborted-cas"
        if not self.identity(row):
            ledger.rollback_eviction(self.conn, sid, row["state"], self.clock())
            ledger.finish_eviction(self.conn, eid, "aborted", detail="identity changed before signal")
            return "aborted-identity"
        try:
            self.kill(int(row["pid"]), signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            ledger.rollback_eviction(self.conn, sid, row["state"], self.clock())
            ledger.finish_eviction(self.conn, eid, "aborted", detail="SIGTERM failed: %s" % exc)
            log.warning("evict %s: SIGTERM failed: %s", sid[:8], exc)
            return "aborted-signal"
        deadline = self.clock() + float(self.cfg["term_grace_s"])
        while self.identity(row) and self.clock() < deadline:
            await self.sleep(0.5)
        if self.identity(row):
            try:
                self.kill(int(row["pid"]), signal.SIGKILL)
            except OSError:
                pass
            outcome, sig = "killed", "SIGKILL"
            log.error("INCIDENT evict %s: still alive %ss after SIGTERM, sent SIGKILL "
                      "(SessionEnd hooks did not run)", sid[:8], self.cfg["term_grace_s"])
            ledger.mark_hibernated(self.conn, sid, self.clock())
        else:
            outcome, sig = "term", "SIGTERM"
            waited = 0.0
            while ledger.state_of(self.conn, sid) == EVICTING and waited < SESSION_END_WAIT_S:
                await self.sleep(0.5)
                waited += 0.5
            if ledger.mark_hibernated(self.conn, sid, self.clock()):
                log.info("evict %s: SessionEnd did not flip the row; daemon set hibernated", sid[:8])
        ledger.finish_eviction(self.conn, eid, outcome, signal=sig)
        log.info("evicted %s (%s) free=%s%% rss=%.0fMB", sid[:8], outcome, free, rss_mb or 0)
        title = title or row.get("title") or sid[:8]
        await self.prepare_tab(row, title)
        self.run_on_evict(row, title, outcome)
        return outcome

    async def prepare_tab(self, row, title):
        guid = row.get("iterm_guid_now") or row.get("iterm_guid")
        if not guid:
            log.warning("evict %s: no tab GUID; nothing typed", row["session_id"][:8])
            return False
        job, waited = None, 0.0
        while waited <= TAB_SHELL_WAIT_S:
            job = await self.tabs.job_name(guid)
            if job is None or is_shell_job(job):
                break
            await self.sleep(0.5)
            waited += 0.5
        if job is None or not is_shell_job(job):
            log.warning("evict %s: tab is not at a shell (%s); nothing typed", row["session_id"][:8], job)
            return False
        banner = "%s\r\n\x1b[2m[cc-sessions] %s: hibernated to free memory; transcript intact.\r\n" \
                 "  press Enter to resume here\x1b[0m\r\n" % (TTY_RESET, title)
        await self.tabs.inject(guid, banner.encode("utf-8"))
        await self.tabs.set_name(guid, "[zz] %s" % title)
        await self.tabs.send_text(guid, resumecmd.command(self.cfg, row["session_id"], row.get("cwd")))
        return True

    def run_on_evict(self, row, title, outcome):
        exe = self.cfg.path("on_evict") if self.cfg.get("on_evict") else None
        if not exe:
            return None
        env = dict(os.environ, CC_SESSION_ID=row["session_id"], CC_SESSION_CWD=row.get("cwd") or "",
                   CC_SESSION_TITLE=title or "", CC_EVICT_OUTCOME=outcome)
        if self.on_evict is not None:
            return self.on_evict(exe, env)
        try:
            return asyncio.ensure_future(_run_hook(exe, env))
        except RuntimeError:
            return None


async def _run_hook(exe, env):
    try:
        p = await asyncio.create_subprocess_exec(exe, env=env, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(p.communicate(), ON_EVICT_TIMEOUT_S)
        except asyncio.TimeoutError:
            p.kill()
            log.warning("on_evict %s timed out after %ss", exe, ON_EVICT_TIMEOUT_S)
            return
        log.info("on_evict %s exit %s: %s", exe, p.returncode, (out or b"").decode("utf-8", "replace").strip()[:300])
    except Exception as exc:
        log.warning("on_evict %s failed: %s", exe, exc)


# ------------------------------------------------------------------ pressure loop (pure-ish)

class Pressure(object):
    """Hysteresis + global backoff. evict_fn(free) -> outcome or None (no candidate)."""

    def __init__(self, cfg, free_fn, clock=time.time, sleep=asyncio.sleep):
        self.cfg, self.free_fn, self.clock, self.sleep = cfg, free_fn, clock, sleep
        self.backoff_until = 0.0

    async def relieve(self, free, evict_fn):
        low, high = float(self.cfg["low_free_pct"]), float(self.cfg["high_free_pct"])
        if free is None or free >= low:
            return "comfortable"
        if self.clock() < self.backoff_until:
            return "backoff"
        misses = 0
        while True:
            outcome = await evict_fn(free)
            if outcome is None:
                return "no-candidate"
            if outcome not in ("term", "killed"):
                misses += 1
                if misses >= MAX_ABORTS_PER_PASS:
                    return "aborted"
                continue
            await self.sleep(float(self.cfg["settle_s"]))
            new = self.free_fn()
            if new is None:
                return "unreadable"
            if new - free < 1:
                self.backoff_until = self.clock() + float(self.cfg["backoff_min"]) * 60
                log.warning("free memory rose %s -> %s%% after an eviction: the memory is "
                            "elsewhere; no evictions for %s min", free, new, self.cfg["backoff_min"])
                return "backoff"
            if new >= high:
                return "relieved"
            free = new


def reconcile(conn, identity, now=None):
    """Daemon startup: rows left in evicting/resuming by a crash or restart."""
    now = time.time() if now is None else now
    fixed = []
    for r in ledger.all_rows(conn, "state IN (?,?)", (EVICTING, RESUMING)):
        new = IDLE if identity(r) else HIBERNATED
        with ledger.tx(conn):
            conn.execute("UPDATE sessions SET state=?, state_since=? WHERE session_id=? AND state=?",
                         (new, now, r["session_id"], r["state"]))
        fixed.append((r["session_id"], r["state"], new))
    return fixed


def expire_resuming(conn, cfg, now=None):
    now = time.time() if now is None else now
    cutoff = now - float(cfg["resume_timeout_s"])
    with ledger.tx(conn):
        cur = conn.execute("UPDATE sessions SET state=?, state_since=? WHERE state=? AND "
                           "state_since < ?", (HIBERNATED, now, RESUMING, cutoff))
        return cur.rowcount


def backoff_delays(start=5, cap=60):
    d = start
    while True:
        yield d
        d = min(cap, d * 2)


# ------------------------------------------------------------------ iTerm2 side

def iterm_running():
    try:
        r = subprocess.run(["osascript", "-e", 'application id "com.googlecode.iterm2" is running'],
                           capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError:
        return False
    return r.stdout.strip() == "true"


def request_cookie():
    """ITERM2_COOKIE/ITERM2_KEY via AppleScript, 10 s timeout. Raises on failure."""
    r = subprocess.run(["osascript", "-e", 'tell application id "com.googlecode.iterm2" to '
                        'request cookie and key for app named "cc-sessions"'],
                       capture_output=True, text=True, timeout=10)
    parts = r.stdout.strip().split()
    if r.returncode != 0 or len(parts) != 2:
        raise RuntimeError("iTerm2 refused a cookie: %s" % (r.stderr.strip()[:160] or "empty reply"))
    os.environ["ITERM2_COOKIE"], os.environ["ITERM2_KEY"] = parts


class ItermTabs(object):
    def __init__(self, app, connection):
        self.app, self.connection = app, connection

    def _s(self, guid):
        return self.app.get_session_by_id(guid) if guid else None

    async def job_name(self, guid):
        s = self._s(guid)
        if s is None:
            return None
        return (await s.async_get_variable("jobName")) or ""

    async def inject(self, guid, data):
        s = self._s(guid)
        if s is not None:
            await s.async_inject(data)

    async def set_name(self, guid, name):
        s = self._s(guid)
        if s is not None:
            await s.async_set_name(name)

    async def send_text(self, guid, text):
        s = self._s(guid)
        if s is not None:
            await s.async_send_text(text)

    async def new_tab(self, text):
        import iterm2
        w = self.app.current_terminal_window
        if w is None:
            w = await iterm2.Window.async_create(self.connection)
            s = w.current_tab.current_session
        else:
            t = await w.async_create_tab()
            s = t.current_session
        await s.async_send_text(text)
        return s.session_id


async def focus_snapshot(app):
    """{tty_guid: {tty: guid}, visible_ttys: set, visible_guids: set}."""
    tty_guid, visible, vguids = {}, set(), set()
    for w in app.terminal_windows:
        cur = w.current_tab
        for t in w.tabs:
            for s in t.sessions:
                tty = procs.norm_tty(await s.async_get_variable("tty"))
                if tty:
                    tty_guid[tty] = s.session_id
                if cur is not None and t.tab_id == cur.tab_id:
                    vguids.add(s.session_id)
                    if tty:
                        visible.add(tty)
    return {"tty_guid": tty_guid, "visible_ttys": visible, "visible_guids": vguids}


class Daemon(object):
    def __init__(self, cfg, conn, app, connection, dry_run):
        self.cfg, self.conn, self.app, self.connection = cfg, conn, app, connection
        self.dry_run = dry_run
        self.tabs = ItermTabs(app, connection)
        self.identity = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
        self.evictor = Evictor(conn, cfg, self.tabs, identity=self.identity)
        self.pressure = Pressure(cfg, procs.free_pct)
        self.last_fire = {}
        self.last_reap = self.last_prune = 0.0
        self.last_said = None

    def heartbeat(self):
        now = time.time()
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "heartbeat", now)
            ledger.set_meta(self.conn, "dry_run", int(self.dry_run))
        p = os.path.join(config.db_dir(self.cfg), "cc-sessions.heartbeat")
        with open(p, "a"):
            os.utime(p, None)

    async def pick_and_evict(self, free, rows=None, explicit=False):
        focus = await focus_snapshot(self.app)
        cands, kept, rss, facts = policy_pass(self.conn, self.cfg, focus, rows=rows, explicit=explicit)
        if not cands:
            said = ("none", tuple(sorted((r["session_id"][:8], w) for r, w in kept)))
            if said != self.last_said:
                log.info("free %s%%: no eviction candidate (%s)", free,
                         "; ".join("%s %s" % (r["session_id"][:8], w) for r, w in kept[:12]))
                self.last_said = said
            return None
        row = cands[0]
        row["iterm_guid_now"] = focus["tty_guid"].get(row["tty"])
        title = title_for(row, facts)
        if self.dry_run:
            said = ("would", row["session_id"])
            if said != self.last_said:
                log.info("DRY RUN would evict %s %s (free %s%%, %.0f MB)", row["session_id"][:8],
                         title[:40], free, rss.get(row["session_id"], 0))
                self.last_said = said
            return None

        async def recheck(r):
            # immediately before the signal: fresh row, fresh focus, fresh process tree,
            # fresh transcript and file activity
            fresh = ledger.get(self.conn, r["session_id"])
            if not fresh or fresh.get("last_event_at") != r.get("last_event_at") \
                    or fresh.get("state") != r.get("state"):
                return "row changed"
            focus2 = await focus_snapshot(self.app)
            if focus2["tty_guid"].get(fresh.get("tty")) != r["iterm_guid_now"]:
                return "tab changed"
            _, k2, _, _ = policy_pass(self.conn, self.cfg, focus2, rows=[fresh], explicit=explicit)
            return k2[0][1] if k2 else None

        with ledger.tx(self.conn):
            if row["iterm_guid_now"]:
                self.conn.execute("UPDATE sessions SET iterm_guid=? WHERE session_id=?",
                                  (row["iterm_guid_now"], row["session_id"]))
        return await self.evictor.evict(row, free, rss.get(row["session_id"], 0), recheck=recheck,
                                        title=title)

    async def tick(self):
        now = time.time()
        self.heartbeat()
        if now - self.last_prune > PRUNE_EVERY_S:
            ledger.prune(self.conn, now)
            self.last_prune = now
        n = expire_resuming(self.conn, self.cfg, now)
        if n:
            log.info("%d resuming row(s) timed back to hibernated", n)
        if now - self.last_reap > REAP_EVERY_S:
            from ccsessions import cli
            removed, _ = cli.reap(self.cfg, dry_run=self.dry_run, cmd_table={}, cwds={})
            if removed:
                log.info("reaped %d dead sockets", len(removed))
            self.last_reap = now
        free = await asyncio.get_running_loop().run_in_executor(None, procs.free_pct)
        if free is None:
            log.warning("memory_pressure unreadable; not evicting")
            return
        await self.pressure.relieve(free, lambda f: self.pick_and_evict(f))

    async def tick_loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(float(self.cfg["tick_s"]))

    async def handle_requests(self):
        rows = self.conn.execute("SELECT * FROM requests WHERE done_at IS NULL ORDER BY id").fetchall()
        for req in rows:
            req = dict(req)
            try:
                result = await self.do_request(req)
            except Exception as exc:
                log.exception("request %s failed", req["id"])
                result = "error: %s" % exc
            with ledger.tx(self.conn):
                self.conn.execute("UPDATE requests SET done_at=?, result=? WHERE id=?",
                                  (time.time(), result, req["id"]))

    async def do_request(self, req):
        kind, sid = req["kind"], req.get("session_id")
        free = procs.free_pct()
        if kind == "hibernate":
            row = ledger.get(self.conn, sid)
            if not row:
                return "unknown session"
            out = await self.pick_and_evict(free, rows=[row], explicit=True)
            return out or "kept (see log)"
        if kind == "hibernate-all-idle":
            done = []
            while True:
                out = await self.pick_and_evict(free)
                if out is None:
                    break
                done.append(out)
                if len(done) > 50:
                    break
            return "evicted %d" % len([d for d in done if d in ("term", "killed")])
        if kind == "wake":
            row = ledger.get(self.conn, sid)
            return await self.wake(row) if row else "unknown session"
        if kind == "wake-all":
            res = [await self.wake(r) for r in ledger.all_rows(self.conn, "state=?", (HIBERNATED,))]
            return "woke %d" % res.count("resumed")
        return "unknown request kind"

    async def wake(self, row):
        if row.get("state") != HIBERNATED:
            return "state=%s" % row.get("state")
        if self.identity(row):
            return "already running"
        cmd = resumecmd.command(self.cfg, row["session_id"], row.get("cwd"))
        if self.dry_run:
            log.info("DRY RUN would wake %s", row["session_id"][:8])
            return "dry-run"
        guid = row.get("iterm_guid")
        job = await self.tabs.job_name(guid) if guid else None
        ledger.mark_resuming(self.conn, row["session_id"])
        if job is None:
            await self.tabs.new_tab(cmd + "\r")
            return "resumed"
        if not is_shell_job(job):
            ledger.mark_hibernated(self.conn, row["session_id"], only_from=(RESUMING,))
            return "tab is running %s" % job
        await self.tabs.send_text(guid, "\x15" + cmd + "\r")
        await self.tabs.set_name(guid, row.get("title") or row["session_id"][:8])
        return "resumed"

    async def request_loop(self):
        while True:
            try:
                await self.handle_requests()
            except Exception:
                log.exception("request poll failed")
            await asyncio.sleep(REQUEST_POLL_S)

    async def on_focus(self, session):
        if session is None:
            return
        guid = session.session_id
        tty = procs.norm_tty(await session.async_get_variable("tty"))
        if not tty:
            return
        now = time.time()
        rows = ledger.all_rows(self.conn, "ended_at IS NULL AND (tty=? OR iterm_guid=?) "
                               "ORDER BY last_event_at DESC", (tty, guid))
        live = [r for r in rows if r["state"] in LIVE_STATES and r.get("tty") == tty and self.identity(r)]
        if live:
            ledger.set_focus(self.conn, live[0]["session_id"], guid, now)
            return
        hib = [r for r in rows if r["state"] == HIBERNATED]
        if not hib:
            return
        hib.sort(key=lambda r: (r.get("iterm_guid") == guid, r.get("evicted_at") or 0), reverse=True)
        row = hib[0]
        job = (await session.async_get_variable("jobName")) or ""
        why = resume_decision(row, guid, tty, now, self.last_fire, job, self.identity)
        if why:
            if why != "debounce":
                log.info("focus %s: not resuming %s: %s", tty, row["session_id"][:8], why)
            self.last_fire[guid] = now
            return
        self.last_fire[guid] = now
        if self.dry_run:
            log.info("DRY RUN would resume %s on focus", row["session_id"][:8])
            return
        ledger.mark_resuming(self.conn, row["session_id"], now)
        await session.async_send_text("\x15" + resumecmd.command(self.cfg, row["session_id"], row.get("cwd")) + "\r")
        await session.async_set_name(row.get("title") or row["session_id"][:8])
        log.info("resumed %s on focus (%s)", row["session_id"][:8], tty)

    async def focus_loop(self):
        import iterm2
        app = self.app
        async with iterm2.FocusMonitor(self.connection) as mon:
            while True:
                update = await mon.async_get_next_update()
                try:
                    s = None
                    if update.selected_tab_changed:
                        tab = app.get_tab_by_id(update.selected_tab_changed.tab_id)
                        s = tab.current_session if tab else None
                    elif update.active_session_changed:
                        s = app.get_session_by_id(update.active_session_changed.session_id)
                    elif update.window_changed and update.window_changed.event == \
                            iterm2.FocusUpdateWindowChanged.Reason.TERMINAL_WINDOW_BECAME_KEY:
                        w = app.get_window_by_id(update.window_changed.window_id)
                        s = w.current_tab.current_session if w and w.current_tab else None
                    await self.on_focus(s)
                except Exception:
                    log.exception("focus handler failed")


# ------------------------------------------------------------------ entry points

def setup_logging(cfg, stderr=False):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    if stderr:
        h = logging.StreamHandler(sys.stderr)
        h.setFormatter(fmt)
        log.addHandler(h)
        return
    path = cfg.path("log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.handlers.RotatingFileHandler(path, maxBytes=1000000, backupCount=3)
    h.setFormatter(fmt)
    log.addHandler(h)


def take_lock(cfg):
    d = config.db_dir(cfg)
    os.makedirs(d, exist_ok=True)
    fh = open(os.path.join(d, "cc-sessions.daemon.lock"), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def once_table(cfg, conn, focus, dry_run, out=print):
    now = time.time()
    rows = ledger.all_rows(conn, "ended_at IS NULL AND state IN (?,?,?)", LIVE_STATES)
    cands, kept, rss, facts = policy_pass(conn, cfg, focus, rows=rows, now=now)
    free = procs.free_pct()
    out("free %s%% (evict below %s%%, stop at %s%%)   dry_run=%s   iTerm2 sessions seen: %d" % (
        free, cfg["low_free_pct"], cfg["high_free_pct"], "on" if dry_run else "off",
        len(focus["tty_guid"])))
    out("%-3s %-8s %-8s %-7s %6s  %s" % ("#", "SID", "TTY", "STATE", "RSS", "VERDICT"))
    for i, r in enumerate(cands, 1):
        out("%-3d %-8s %-8s %-7s %5.0fM  CANDIDATE" % (i, r["session_id"][:8], r.get("tty") or "-",
                                                      r["state"], rss.get(r["session_id"], 0)))
    for r, why in kept:
        out("%-3s %-8s %-8s %-7s %5.0fM  keep: %s" % ("-", r["session_id"][:8], r.get("tty") or "-",
                                                     r["state"], rss.get(r["session_id"], 0), why))
    if not rows:
        out("(no live sessions in the ledger at %s)" % cfg.path("db"))
    return cands, kept


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="cc-sessions daemon")
    ap.add_argument("--once", action="store_true", help="print the candidate table and exit")
    ap.add_argument("--dry-run", action="store_true", help="never signal, type or resume")
    a = ap.parse_args(argv)
    cfg = config.load()
    dry_run = bool(a.dry_run or cfg.get("dry_run"))
    setup_logging(cfg, stderr=a.once)
    if cfg.error:
        log.error("config error, dry_run forced: %s", cfg.error)
    try:
        import iterm2
    except ImportError:
        log.error("the iterm2 package is missing from %s (pip install iterm2)", sys.executable)
        if a.once:
            return 1
        time.sleep(60)  # launchd restarts us; do not spin
        return 1

    if a.once:
        if not iterm_running():
            print("iTerm2 is not running", file=sys.stderr)
            return 1
        if not os.environ.get("ITERM2_COOKIE"):
            request_cookie()
        conn = ledger.connect(cfg.path("db"))
        result = {}

        async def once(connection):
            app = await iterm2.async_get_app(connection)
            result["focus"] = await focus_snapshot(app)
        iterm2.run_until_complete(once, retry=False)
        once_table(cfg, conn, result["focus"], dry_run)
        return 0

    lock = take_lock(cfg)
    if lock is None:
        log.info("another daemon holds the lock; exiting")
        return 0
    conn = ledger.connect(cfg.path("db"))
    fixed = reconcile(conn, lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start")))
    for sid, old, new in fixed:
        log.info("startup: %s %s -> %s", sid[:8], old, new)
    log.info("daemon start (dry_run=%s, pid %d)", dry_run, os.getpid())
    delays = backoff_delays()
    first = True
    while True:
        started = time.time()
        try:
            if not iterm_running():
                raise RuntimeError("iTerm2 is not running")
            if not first or not os.environ.get("ITERM2_COOKIE"):
                os.environ.pop("ITERM2_COOKIE", None)
                os.environ.pop("ITERM2_KEY", None)
                request_cookie()
            first = False

            async def run(connection):
                app = await iterm2.async_get_app(connection)
                d = Daemon(cfg, conn, app, connection, dry_run)
                log.info("connected to iTerm2")
                await asyncio.gather(d.focus_loop(), d.tick_loop(), d.request_loop())
            iterm2.run_until_complete(run, retry=False)
            log.warning("iTerm2 connection ended")
        except (Exception, SystemExit) as exc:  # the iterm2 package sys.exit()s on errors
            log.warning("iTerm2 connection failed: %s", exc)
        first = False
        if time.time() - started > 300:
            delays = backoff_delays()
        time.sleep(next(delays))


if __name__ == "__main__":
    sys.exit(main())
