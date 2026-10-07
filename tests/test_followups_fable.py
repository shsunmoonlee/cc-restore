"""Follow-ups from the final review of the idle-hibernation batch: focus during the last
checks aborts, picks report their own session id, a long pass keeps the daemon healthy
in doctor (liveness + pass_started_at) and is capped, final_check is the last step."""
import asyncio
import contextlib
import io
import json
import os
import signal
import time

from helpers import TempEnv, load_daemon
from test_idle_hibernate import Clock, IdleBase, arun

from ccsessions import cli, config, ledger, procs

D = load_daemon()


# ------------------------------------------------------------------ 1 + 4. final_check

class FinalCheckIsLast(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", tty="ttys001", last_focus_at=None,
                               state_since=self.clock.t - 3600, last_event_at=self.clock.t - 3600)
        self.kills = []
        self.alive = True
        self.sd = self.cfg.path("claude_sessions")
        os.makedirs(self.sd)

    def evictor(self, children):
        def kill(pid, sig):
            self.kills.append((pid, sig))
            self.alive = False
        return D.Evictor(self.conn, self.cfg, None, kill=kill, identity=lambda r: self.alive,
                         clock=self.clock, sleep=self.clock.sleep, children=children,
                         child_probe=lambda p, s: "gone")

    def status(self, status):
        with open(os.path.join(self.sd, "111.json"), "w") as fh:
            json.dump({"pid": 111, "sessionId": "s1", "status": status}, fh)

    def evictions(self):
        return [dict(r) for r in self.conn.execute("SELECT outcome, detail FROM evictions")]

    def test_focus_stamp_after_the_recheck_aborts_before_the_signal(self):
        def children(row):
            # the focus handler's stamp, landing while the row is evicting
            ledger.stamp_visible(self.conn, {"ttys001": "G1"}, self.clock.t)
            return []
        out = arun(self.evictor(children).evict(self.row, 10, 1, recheck=lambda r: None))
        self.assertEqual(out, "aborted-final")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")
        self.assertEqual(self.evictions(), [{"outcome": "aborted", "detail": "focused during the checks"}])

    def test_status_flip_in_the_children_probe_aborts_before_the_signal(self):
        self.status("idle")

        def children(row):
            self.status("busy")
            return []
        out = arun(self.evictor(children).evict(self.row, 10, 1, recheck=lambda r: None))
        self.assertEqual(out, "aborted-final")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")
        self.assertEqual(self.evictions()[0]["detail"], "claude status busy before signal")

    def test_no_focus_change_still_evicts(self):
        ledger.stamp_visible(self.conn, {"ttys001": None}, self.clock.t - 1800)
        row = ledger.get(self.conn, "s1")
        out = arun(self.evictor(lambda r: []).evict(row, 10, 1, recheck=lambda r: None))
        self.assertEqual(out, "term")
        self.assertEqual(self.kills, [(111, signal.SIGTERM)])

    def test_stamp_visible_reaches_evicting_rows_only_among_parked(self):
        self.insert(session_id="ev", tty="ttys002", state="evicting", pid=222)
        self.insert(session_id="hb", tty="ttys003", state="hibernated", pid=333)
        n = ledger.stamp_visible(self.conn, {"ttys002": "G2", "ttys003": "G3"}, 123.0)
        self.assertEqual(n, 1)
        self.assertEqual(ledger.get(self.conn, "ev")["last_focus_at"], 123.0)
        self.assertIsNone(ledger.get(self.conn, "hb")["last_focus_at"])


# ------------------------------------------------------------------ 2. per-call pick sid

class YieldingLock(object):
    """asyncio.Lock whose release yields to the loop, so a waiting pick runs before the
    releasing task reads anything back."""

    def __init__(self):
        self.lock = asyncio.Lock()

    def locked(self):
        return self.lock.locked()

    async def __aenter__(self):
        await self.lock.acquire()

    async def __aexit__(self, *exc):
        self.lock.release()
        await asyncio.sleep(0)


class PickInterleave(IdleBase):
    def test_request_pick_between_idle_picks_does_not_corrupt_aborted(self):
        self.add("a", "ttys001")  # idle 30m: the idle pass takes it
        self.add("r", "ttys002", idle_min=1)  # idle 1m: only the explicit request takes it
        d = self.daemon(["ttys001", "ttys002"])

        async def off(fn, *a):
            return 50 if fn is procs.free_pct else fn(*a)
        d.off = off

        async def policy(focus_state, rows=None, explicit=False, exclude=(), cfg=None):
            now = time.time()
            rows = [r for r in rows if r["session_id"] not in exclude]
            cands, kept = D.choose_candidates(
                rows, now, cfg or self.cfg, focus_state, {},
                lambda r: {"pending": [], "has_user": True}, lambda r: [],
                identity_fn=lambda r: None, activity_fn=lambda r: now - 3600,
                evictions_today={}, hook_failed=set(), explicit=explicit)
            return cands, kept, {}, {}
        d.policy = policy
        picked = []

        async def evict(row, free, rss, recheck=None, title=None, reason=None):
            picked.append(row["session_id"])
            for _ in range(3):
                await asyncio.sleep(0)
            return "aborted-test"
        d.evictor.evict = evict

        async def go():
            d.evict_lock = YieldingLock()
            return await asyncio.gather(d.idle_pass(50),
                                        d.do_request({"kind": "hibernate", "session_id": "r"}))
        idle_out, req_out = arun(go())
        self.assertEqual(req_out, "aborted-test")
        self.assertEqual(idle_out, ["aborted-test"])
        self.assertEqual(picked.count("a"), 1, picked)

    def test_evict_pass_excludes_the_session_its_own_pick_returned(self):
        d = self.daemon([])
        script = iter([("aborted-cas", "A"), ("term", "B"), (None, None)])
        seen = []

        async def pick(free, rows=None, explicit=False, exclude=()):
            seen.append(set(exclude))
            return next(script)
        d.pick = pick
        one, outcomes = arun(d.evict_pass(10))

        async def drain():
            while await one(10) is not None:
                pass
        arun(drain())
        self.assertEqual(seen, [set(), {"A"}, {"A"}])
        self.assertEqual(outcomes, ["aborted-cas", "term"])
        self.assertFalse(hasattr(d, "last_pick"))


# ------------------------------------------------------------------ 3. long pass: liveness, cap

def meta(conn, key):
    v = ledger.get_meta(conn, key)
    return float(v) if v not in (None, "") else None


class LongPass(IdleBase):
    def test_tick_records_pass_and_liveness_per_eviction(self):
        self.cfg["idle_hibernate_min"] = 0
        self.add("a", "ttys001")
        self.add("b", "ttys002")
        d = self.daemon(["ttys001", "ttys002"])
        d.last_prune = d.last_reap = time.time()

        async def off(fn, *a):
            if fn is procs.free_pct:
                return 10
            if fn is procs.swap_pct:
                return None
            return fn(*a)
        d.off = off

        async def nap(s):
            return None
        d.pressure.sleep = nap
        frees = iter([12, 14])
        d.pressure.free_fn = lambda: next(frees)
        D.record_meta(self.conn, liveness=0, heartbeat=0)
        seen = []
        real = d.evictor.evict

        async def evict(row, *a, **kw):
            seen.append((meta(self.conn, "pass_started_at"), meta(self.conn, "liveness")))
            return await real(row, *a, **kw)
        d.evictor.evict = evict
        arun(d.tick())
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(s[0] for s in seen), seen)
        self.assertEqual(seen[0][0], seen[1][0])  # written once, on the first attempt
        self.assertEqual(seen[0][1], seen[0][0])  # liveness written with the pass start
        self.assertGreaterEqual(seen[1][1], seen[0][1])  # and again after the first eviction
        self.assertIsNone(meta(self.conn, "pass_started_at"))
        self.assertGreater(meta(self.conn, "heartbeat"), 0)

    def test_busy_nests_and_clears_once(self):
        d = self.daemon([])
        with d.busy():
            self.assertIsNone(meta(self.conn, "pass_started_at"))  # lazy: no eviction yet
            d.mark_pass()
            first = meta(self.conn, "pass_started_at")
            with d.busy():
                d.mark_pass()
                self.assertEqual(meta(self.conn, "pass_started_at"), first)
            self.assertEqual(meta(self.conn, "pass_started_at"), first)
        self.assertIsNotNone(first)
        self.assertIsNone(meta(self.conn, "pass_started_at"))

    def test_record_start_clears_a_pass_left_by_a_dead_process(self):
        D.record_meta(self.conn, pass_started_at=time.time())
        D.record_start(self.conn)
        self.assertIsNone(meta(self.conn, "pass_started_at"))


class PassCap(TempEnv):
    def run_pass(self, cap):
        cfg = dict(self.cfg, settle_s=0, max_evictions_per_pass=cap)
        frees = iter(range(11, 100))
        clock = Clock()
        p = D.Pressure(cfg, lambda: next(frees), clock=clock, sleep=clock.sleep)
        calls = []

        async def evict(free):
            calls.append(free)
            return "term"
        return arun(p.relieve(10, evict)), len(calls)

    def test_default_is_ten(self):
        self.assertEqual(config.DEFAULTS["max_evictions_per_pass"], 10)
        self.assertEqual(self.run_pass(self.cfg["max_evictions_per_pass"]), ("capped", 10))

    def test_cap_stops_the_pass(self):
        self.assertEqual(self.run_pass(3), ("capped", 3))

    def test_zero_is_no_cap(self):
        self.assertEqual(self.run_pass(0), ("relieved", 25))


class DoctorBusy(TempEnv):
    NOW = 2_000_000_000.0

    def set(self, **kv):
        D.record_meta(self.conn, **kv)

    def hb(self, alive=lambda pid: True):
        return [c for c in cli.daemon_checks(self.conn, now=self.NOW, alive=alive)
                if c[1] == "daemon heartbeat"][0]

    def test_fresh_heartbeat(self):
        self.set(heartbeat=self.NOW - 5, liveness=self.NOW - 5, pid=4242)
        self.assertEqual(self.hb(), ("PASS", "daemon heartbeat", "5s ago"))

    def test_busy_pass_with_fresh_liveness_passes(self):
        self.set(heartbeat=self.NOW - 400, liveness=self.NOW - 8, pid=4242,
                 pass_started_at=self.NOW - 390)
        st, _, detail = self.hb()
        self.assertEqual(st, "PASS")
        self.assertIn("daemon busy: evicting since %s" % time.strftime(
            "%H:%M:%S", time.localtime(self.NOW - 390)), detail)
        self.assertIn("liveness 8s ago", detail)

    def test_busy_pass_with_stale_liveness_fails(self):
        self.set(heartbeat=self.NOW - 400, liveness=self.NOW - 300, pid=4242,
                 pass_started_at=self.NOW - 390)
        self.assertEqual(self.hb()[0], "FAIL")

    def test_no_pass_with_fresh_liveness_still_fails(self):
        self.set(heartbeat=self.NOW - 400, liveness=self.NOW - 8, pid=4242, pass_started_at="")
        self.assertEqual(self.hb()[0], "FAIL")

    def test_busy_pass_with_dead_pid_fails(self):
        self.set(heartbeat=self.NOW - 400, liveness=self.NOW - 8, pid=4242,
                 pass_started_at=self.NOW - 390)
        self.assertEqual(self.hb(alive=lambda pid: False)[0], "FAIL")

    def request_warning(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli._request(self.cfg, "hibernate-all-idle", None), 0)
        return "stale" in err.getvalue()

    def test_hibernate_warning_uses_the_same_rule(self):
        now = time.time()
        self.set(heartbeat=now - 400, liveness=now - 8, pass_started_at=now - 390)
        self.assertFalse(self.request_warning())
        self.set(pass_started_at="")
        self.assertTrue(self.request_warning())
        self.set(heartbeat=now - 5)
        self.assertFalse(self.request_warning())


# ------------------------------------------------------------------ minor: logs

class MinorLogs(IdleBase):
    def test_idle_pass_disabled_by_per_tick_zero_logs_once(self):
        self.cfg["idle_hibernate_per_tick"] = 0
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"])
        with self.assertLogs("cc-sessions", "INFO") as cm:
            self.assertEqual(arun(d.idle_pass(50)), [])
            self.assertEqual(arun(d.idle_pass(50)), [])
            D.log.info("end")
        hits = [m for m in cm.output if "idle pass disabled (per_tick 0)" in m]
        self.assertEqual(len(hits), 1)
        self.assertEqual(self.evicted, [])

    def test_unreadable_free_logs_a_question_mark(self):
        self.assertEqual(D._pct(None), "?")
        self.assertEqual(D._pct(27), "27%")
        clock = Clock()
        row = self.insert(session_id="s1", state="idle", last_event_at=clock.t - 3600)
        state = {"alive": True}

        def kill(pid, sig):
            state["alive"] = False
        ev = D.Evictor(self.conn, self.cfg, None, kill=kill, identity=lambda r: state["alive"],
                       clock=clock, sleep=clock.sleep, children=lambda r: [],
                       child_probe=lambda p, s: "gone")
        with self.assertLogs("cc-sessions", "INFO") as cm:
            self.assertEqual(arun(ev.evict(row, None, 1, reason="idle 60m")), "term")
        line = [m for m in cm.output if "evicted s1" in m][0]
        self.assertIn("free=? ", line)
        self.assertNotIn("None", line)
