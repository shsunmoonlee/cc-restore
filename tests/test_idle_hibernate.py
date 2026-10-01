"""Idle-time hibernation (idle_hibernate_min) and the swap pressure trigger (swap_high_pct)."""
import asyncio
import logging
import signal
import time

from helpers import TempEnv, assistant, load_daemon, user

from ccsessions import ledger, procs

D = load_daemon()


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def focus(ttys, visible=()):
    return {"tty_guid": {t: "G-" + t for t in ttys}, "visible_ttys": set(visible),
            "visible_guids": set(), "panes": len(ttys), "skipped": 0}


class FakeWS(object):
    def __init__(self, closed):
        self.closed = closed


class FakeConn(object):
    def __init__(self, closed=False):
        self.websocket = FakeWS(closed)


class Clock(object):
    def __init__(self, t=2_000_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class IdleBase(TempEnv):
    extra_config = {"idle_hibernate_min": 3}

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.pending = {}
        self.children = {}
        self.policy_cfgs = []
        self.evicted = []
        self.outcome = "term"
        self.focus = None

    def add(self, sid, tty, idle_min=None, **kw):
        tr = self.transcript(sid, [user("hi"), assistant("done")])
        since = self.now - (idle_min if idle_min is not None else 30) * 60
        base = dict(session_id=sid, tty=tty, transcript=tr, state_since=since, last_event_at=since)
        base.update(kw)
        return self.insert(**base)

    def daemon(self, ttys, visible=(), connection=None):
        d = D.Daemon(self.cfg, self.conn, None, connection, False)
        self.focus = focus(ttys, visible)

        async def snap():
            return self.focus
        d.snapshot = snap

        async def policy(focus_state, rows=None, explicit=False, exclude=(), cfg=None):
            c = cfg or self.cfg
            self.policy_cfgs.append(c)
            now = time.time()
            rows = D.live_rows(self.conn) if rows is None else rows
            rows = [r for r in rows if r["session_id"] not in exclude]
            cands, kept = D.choose_candidates(
                rows, now, c, focus_state, {},
                lambda r: self.pending.get(r["session_id"], {"pending": [], "has_user": True}),
                lambda r: self.children.get(r["session_id"], []),
                identity_fn=lambda r: None, activity_fn=lambda r: now - 3600,
                evictions_today=ledger.evictions_today(self.conn, now), hook_failed=set())
            self.kept = kept
            return cands, kept, {}, {}
        d.policy = policy

        async def evict(row, free, rss, recheck=None, title=None, reason=None):
            self.evicted.append((row["session_id"], reason))
            if self.outcome in ("term", "killed"):
                with ledger.tx(self.conn):
                    self.conn.execute("UPDATE sessions SET state='hibernated' WHERE session_id=?",
                                      (row["session_id"],))
            return self.outcome
        d.evictor.evict = evict
        return d

    def sids(self):
        return [s for s, _ in self.evicted]


class IdlePass(IdleBase):
    def test_disabled_when_zero(self):
        self.cfg["idle_hibernate_min"] = 0
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"])
        self.assertEqual(arun(d.idle_pass(50)), [])
        self.assertEqual(self.policy_cfgs, [])
        self.assertEqual(self.evicted, [])

    def test_disabled_by_default(self):
        from ccsessions import config
        self.assertEqual(config.DEFAULTS["idle_hibernate_min"], 0)
        self.assertEqual(config.DEFAULTS["idle_hibernate_per_tick"], 3)
        self.assertEqual(config.DEFAULTS["swap_high_pct"], 75)

    def test_picks_only_idle_past_threshold(self):
        ttys = ["ttys%03d" % i for i in range(1, 13)]
        self.add("ok", ttys[0], idle_min=5)
        self.add("young", ttys[1], idle_min=2)
        self.add("busy", ttys[2], state="busy")
        self.add("waiting", ttys[3], state="waiting")
        self.add("resuming", ttys[4], state="resuming")
        self.add("visible", ttys[5])
        self.add("cooldown", ttys[6], resumed_at=self.now - 600)
        self.add("subagent", ttys[7], subagents=1)
        self.add("notab", "ttys099")
        self.add("bgwork", ttys[8])
        self.pending["bgwork"] = {"pending": ["1 agent running"], "has_user": True}
        self.add("child", ttys[9])
        self.children["child"] = ["make"]
        self.add("focused", ttys[10], last_focus_at=self.now - 60)
        # stale busy + Esc-interrupted: the pressure pass may take it, the idle pass never
        self.add("stalebusy", ttys[11], state="busy", idle_min=60)
        self.pending["stalebusy"] = {"pending": [], "has_user": True, "last_kind": "user",
                                     "last_user_text": "[Request interrupted by user]"}
        self.add("maxed", "ttys050")
        with ledger.tx(self.conn):
            for _ in range(3):
                self.conn.execute("INSERT INTO evictions(session_id, ts, pid, free_pct, rss_mb, signal, "
                                  "outcome) VALUES ('maxed', ?, 111, 10, 1, 'SIGTERM', 'term')", (self.now,))
        d = self.daemon(ttys + ["ttys050"], visible=(ttys[5],))
        out = arun(d.idle_pass(50))
        self.assertEqual(out, ["term"])
        self.assertEqual(self.evicted, [("ok", "idle 5m")])
        kept = {r["session_id"]: w for r, w in self.kept}
        self.assertTrue(kept["young"].startswith("idle 2m"), kept["young"])
        self.assertEqual(kept["visible"], "visible tab")
        self.assertTrue(kept["cooldown"].startswith("cooldown"))
        self.assertEqual(kept["subagent"], "subagents=1")
        self.assertTrue(kept["notab"].startswith("no iTerm2 tab"))
        self.assertTrue(kept["bgwork"].startswith("pending"))
        self.assertTrue(kept["child"].startswith("running"))
        self.assertTrue(kept["focused"].startswith("focused 1m"))
        self.assertTrue(kept["maxed"].startswith("evicted 3x today"))
        for sid in ("busy", "waiting", "resuming", "stalebusy"):
            self.assertNotIn(sid, kept)  # never even offered to the guards
        for c in self.policy_cfgs:
            self.assertEqual(c["idle_min"], 3)
        self.assertEqual(self.cfg["idle_min"], 10)  # the daemon's own config is untouched

    def test_focus_threshold_is_idle_hibernate_min(self):
        self.add("a", "ttys001", idle_min=20, last_focus_at=self.now - 5 * 60)
        d = self.daemon(["ttys001"])
        arun(d.idle_pass(50))
        self.assertEqual(self.sids(), ["a"])  # idle_min=10 would keep it ("focused 5m ago")

    def test_per_tick_cap_and_order(self):
        ttys = ["ttys%03d" % i for i in range(1, 6)]
        self.add("f1", ttys[0], last_focus_at=self.now - 600)
        self.add("f2", ttys[1], last_focus_at=self.now - 3000)
        self.add("n1", ttys[2])
        self.add("f3", ttys[3], last_focus_at=self.now - 1200)
        self.add("f4", ttys[4], last_focus_at=self.now - 300)
        d = self.daemon(ttys)
        self.assertEqual(arun(d.idle_pass(50)), ["term"] * 3)
        self.assertEqual(self.sids(), ["n1", "f2", "f3"])  # never focused, then least recent
        self.evicted[:] = []
        self.assertEqual(arun(d.idle_pass(50)), ["term"] * 2)
        self.assertEqual(self.sids(), ["f1", "f4"])

    def test_cap_is_configurable(self):
        self.cfg["idle_hibernate_per_tick"] = 1
        self.add("a", "ttys001")
        self.add("b", "ttys002")
        d = self.daemon(["ttys001", "ttys002"])
        self.assertEqual(arun(d.idle_pass(50)), ["term"])

    def test_aborts_are_not_repicked_and_capped(self):
        for i in range(5):
            self.add("s%d" % i, "ttys%03d" % i)
        d = self.daemon(["ttys%03d" % i for i in range(5)])
        self.outcome = "aborted-cas"
        out = arun(d.idle_pass(50))
        self.assertEqual(out, ["aborted-cas"] * D.MAX_ABORTS_PER_PASS)
        self.assertEqual(len(set(self.sids())), D.MAX_ABORTS_PER_PASS)

    def test_not_blocked_by_pressure_backoff(self):
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"])
        d.pressure.backoff_until = time.time() + 3600
        self.assertEqual(arun(d.idle_pass(50)), ["term"])

    def test_stops_when_disconnected(self):
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"], connection=FakeConn(closed=True))
        with self.assertRaises(D.ConnectionLost):
            arun(d.idle_pass(50))
        self.assertEqual(self.evicted, [])

    def test_waits_for_a_pressure_pass_in_flight(self):
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"])

        async def go():
            d.pass_lock = asyncio.Lock()  # python 3.9 binds a Lock to the loop current at creation
            d.evict_lock = asyncio.Lock()
            await d.pass_lock.acquire()
            task = asyncio.ensure_future(d.idle_pass(50))
            for _ in range(5):
                await asyncio.sleep(0)
            blocked = list(self.evicted)
            d.pass_lock.release()
            return blocked, await task
        blocked, out = arun(go())
        self.assertEqual(blocked, [])
        self.assertEqual(out, ["term"])

    def test_dry_run_never_evicts(self):
        self.add("a", "ttys001")
        d = self.daemon(["ttys001"])
        d.dry_run = True
        self.assertEqual(arun(d.idle_pass(50)), [])
        self.assertEqual(self.evicted, [])


class IdlePassRealEvictor(IdleBase):
    """The idle pass goes through the one Evictor: CAS first, the re-check with the idle
    config, SIGTERM, hibernated, and a log line that names the reason."""

    def test_real_evictor_path(self):
        self.add("a", "ttys001", idle_min=4, pid=4242)
        d = self.daemon(["ttys001"])
        clock = Clock()
        kills, state_at_signal, alive = [], [], [True]

        def kill(pid, sig):
            state_at_signal.append(ledger.state_of(self.conn, "a"))
            kills.append((pid, sig))
            alive[0] = False

        class Tabs(object):
            async def job_name(self, guid):
                return "zsh"

            async def tty(self, guid):
                return "/dev/ttys001"

            async def is_visible(self, guid):
                return False

            async def inject(self, guid, data):
                pass

            async def set_name(self, guid, name):
                pass

            async def send_text(self, guid, text):
                pass
        d.evictor = D.Evictor(self.conn, self.cfg, Tabs(), kill=kill, identity=lambda r: alive[0],
                              clock=clock, sleep=clock.sleep, children=lambda r: [])
        with self.assertLogs("cc-sessions", level="INFO") as logs:
            out = arun(d.idle_pass(27))
        self.assertEqual(out, ["term"])
        self.assertEqual(kills, [(4242, signal.SIGTERM)])
        self.assertEqual(state_at_signal, ["evicting"])
        self.assertEqual(ledger.state_of(self.conn, "a"), "hibernated")
        # pick + post-CAS re-check both ran the guards with idle_hibernate_min
        self.assertGreaterEqual(len(self.policy_cfgs), 2)
        self.assertTrue(all(c["idle_min"] == 3 for c in self.policy_cfgs))
        line = [m for m in logs.output if "evicted a" in m]
        self.assertEqual(len(line), 1, logs.output)
        self.assertIn("(term) reason=idle 4m free=27%", line[0])

    def test_recheck_keeps_a_row_that_got_focused(self):
        self.add("a", "ttys001", idle_min=4, pid=4242)
        d = self.daemon(["ttys001"])
        kills = []
        d.evictor = D.Evictor(self.conn, self.cfg, None, kill=lambda p, s: kills.append(p),
                              identity=lambda r: True, children=lambda r: [])
        real_policy = d.policy
        calls = []

        async def policy(focus_state, rows=None, explicit=False, exclude=(), cfg=None):
            calls.append(1)
            if len(calls) == 2:  # the re-check: the tab is now visible
                focus_state = focus(["ttys001"], visible=("ttys001",))
            return await real_policy(focus_state, rows, explicit, exclude, cfg)
        d.policy = policy
        out = arun(d.idle_pass(50))
        self.assertEqual(out, ["aborted-recheck"])
        self.assertEqual(kills, [])
        self.assertEqual(ledger.state_of(self.conn, "a"), "idle")


class SwapParse(TempEnv):
    def test_normal(self):
        out = "total = 13312.00M  used = 11900.25M  free = 1411.75M  (encrypted)\n"
        self.assertEqual(procs.parse_swapusage(out), (13312.0, 11900.25))

    def test_malformed(self):
        for out in (None, "", "garbage", "total = 13312.00M", "used = 5.00M",
                    "total = 0.00M  used = 0.00M  free = 0.00M", "total = abcM used = 1M",
                    "total = 1.2.3M  used = 1.00M"):
            self.assertIsNone(procs.parse_swapusage(out), out)

    def test_swap_pct(self):
        orig = procs._run
        try:
            procs._run = lambda args, timeout=10: "total = 1000.00M  used = 750.00M  free = 250.00M"
            self.assertEqual(procs.swap_pct(), 75.0)
            procs._run = lambda args, timeout=10: None
            self.assertIsNone(procs.swap_pct())
            procs._run = lambda args, timeout=10: "vm.swapusage: weird"
            self.assertIsNone(procs.swap_pct())
        finally:
            procs._run = orig


class SwapTrigger(TempEnv):
    def pressure(self, frees, swaps):
        clock = Clock()
        frees, swaps = iter(frees), iter(swaps)
        p = D.Pressure(self.cfg, lambda: next(frees), clock=clock, sleep=clock.sleep,
                       swap_fn=lambda: next(swaps))
        calls = []

        async def evict(free):
            calls.append(free)
            return "term"
        return p, evict, calls, clock

    def test_swap_starts_a_pass_and_stops_below_hysteresis(self):
        p, evict, calls, _ = self.pressure([27, 27], [70, 64])
        with self.assertLogs("cc-sessions", level="INFO") as logs:
            self.assertEqual(arun(p.relieve(27, evict, swap=90)), "relieved")
        self.assertEqual(len(calls), 2)  # 70 is still >= 65: one more
        self.assertTrue(any("trigger=swap" in m for m in logs.output), logs.output)
        self.assertEqual(p.trigger, "swap")

    def test_swap_pass_stops_at_high_free(self):
        p, evict, calls, _ = self.pressure([36], [80])
        self.assertEqual(arun(p.relieve(27, evict, swap=90)), "relieved")
        self.assertEqual(len(calls), 1)

    def test_high_swap_with_free_at_or_above_high_free_does_not_evict(self):
        p, evict, calls, _ = self.pressure([], [])
        for free in (35, 36, 80):
            self.assertEqual(arun(p.relieve(free, evict, swap=99)), "comfortable", free)
        self.assertEqual(calls, [])
        self.assertIsNone(p.trigger)

    def test_high_swap_with_free_between_low_and_high_starts_a_swap_pass(self):
        p, evict, calls, _ = self.pressure([30], [50])
        self.assertEqual(arun(p.relieve(34.9, evict, swap=90)), "relieved")
        self.assertEqual(calls, [34.9])
        self.assertEqual(p.trigger, "swap")

    def test_below_threshold_or_unreadable_is_comfortable(self):
        p, evict, calls, _ = self.pressure([], [])
        self.assertEqual(arun(p.relieve(27, evict, swap=74.9)), "comfortable")
        self.assertEqual(arun(p.relieve(27, evict, swap=None)), "comfortable")
        self.assertEqual(arun(p.relieve(27, evict)), "comfortable")
        self.cfg["swap_high_pct"] = 0
        self.assertEqual(arun(p.relieve(27, evict, swap=99)), "comfortable")
        self.assertEqual(calls, [])
        self.assertIsNone(p.trigger)

    def test_free_trigger_wins_and_is_logged(self):
        p, evict, calls, _ = self.pressure([36], [])
        with self.assertLogs("cc-sessions", level="INFO") as logs:
            self.assertEqual(arun(p.relieve(15, evict, swap=90)), "relieved")
        self.assertTrue(any("trigger=free" in m for m in logs.output))

    def test_swap_progress_counts_as_progress(self):
        p, evict, calls, _ = self.pressure([27, 27], [80, 60])
        self.assertEqual(arun(p.relieve(27, evict, swap=90)), "relieved")
        self.assertEqual(p.backoff_until, 0.0)

    def test_no_progress_backs_off(self):
        p, evict, calls, clock = self.pressure([27], [89.5])
        self.assertEqual(arun(p.relieve(27, evict, swap=90)), "backoff")
        self.assertGreater(p.backoff_until, clock())
        self.assertEqual(arun(p.relieve(27, evict, swap=90)), "backoff")
        self.assertEqual(len(calls), 1)

    def test_swap_unreadable_mid_pass(self):
        p, evict, calls, _ = self.pressure([27], [None])
        self.assertEqual(arun(p.relieve(27, evict, swap=90)), "unreadable")


class TickWiring(IdleBase):
    """tick(): swap feeds the pressure pass; the idle pass runs after it, even when the
    pressure pass is backing off."""

    def tick_daemon(self, free, swap):
        d = self.daemon(["ttys001"])
        d.last_prune = d.last_reap = time.time()

        async def off(fn, *a):
            if fn is procs.free_pct:
                return free
            if fn is procs.swap_pct:
                return swap
            return fn(*a)
        d.off = off

        async def nap(s):
            return None
        d.pressure.sleep = nap
        order = []
        relieved = []
        real_relieve = d.pressure.relieve

        async def relieve(f, evict_fn, swap=None):
            order.append("pressure")
            r = await real_relieve(f, evict_fn, swap=swap)
            relieved.append((f, swap, r))
            return r
        d.pressure.relieve = relieve
        real_idle = d.idle_pass

        async def idle(f):
            order.append("idle")
            return await real_idle(f)
        d.idle_pass = idle
        return d, order, relieved

    def test_swap_trigger_starts_the_pressure_pass(self):
        self.cfg["idle_hibernate_min"] = 0
        self.add("a", "ttys001")
        d, order, relieved = self.tick_daemon(30, 90.0)
        d.pressure.free_fn = lambda: 31
        d.pressure.swap_fn = lambda: 50.0
        arun(d.tick())
        self.assertEqual(order, ["pressure", "idle"])
        self.assertEqual(relieved, [(30, 90.0, "relieved")])
        self.assertEqual(self.evicted, [("a", "swap")])

    def test_high_swap_with_plenty_free_evicts_nothing_in_tick(self):
        self.cfg["idle_hibernate_min"] = 0
        self.add("a", "ttys001")
        d, order, relieved = self.tick_daemon(40, 95.0)
        arun(d.tick())
        self.assertEqual(relieved, [(40, 95.0, "comfortable")])
        self.assertEqual(self.evicted, [])

    def test_idle_pass_runs_under_pressure_backoff(self):
        self.add("a", "ttys001")
        d, order, relieved = self.tick_daemon(10, 90.0)
        d.pressure.backoff_until = time.time() + 3600
        arun(d.tick())
        self.assertEqual(relieved[0][2], "backoff")
        self.assertEqual(self.evicted, [("a", "idle 30m")])

    def test_idle_pass_runs_when_free_unreadable(self):
        self.add("a", "ttys001")
        d, order, relieved = self.tick_daemon(None, None)
        arun(d.tick())
        self.assertEqual(order, ["idle"])
        self.assertEqual(self.sids(), ["a"])


class StatusHeader(TempEnv):
    extra_config = {"idle_hibernate_min": 3}

    def test_status_and_doctor_show_the_keys(self):
        import io
        from contextlib import redirect_stdout

        from ccsessions import cli
        orig = (procs.swap_pct, procs.free_pct, procs.swap_used_mb)
        procs.swap_pct, procs.free_pct, procs.swap_used_mb = (lambda: 88.5), (lambda: 30), (lambda: 11900.0)
        try:
            d = cli.status_data(self.cfg, self.conn)
            self.assertEqual(d["idle_hibernate_min"], 3)
            self.assertEqual(d["memory"]["swap_pct"], 88.5)

            class A(object):
                json = False
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.cmd_status(self.cfg, A())
            head = buf.getvalue().splitlines()[:2]
            self.assertIn("idle hibernate: after 3m", head[0])
            self.assertIn("(88.5%, evict at 75%)", head[1])
            checks = {c[1]: c for c in cli.doctor_checks(self.cfg)}
            self.assertIn("88.5%", checks["swap"][2])
            self.assertIn("after 3m", checks["idle hibernate"][2])
        finally:
            procs.swap_pct, procs.free_pct, procs.swap_used_mb = orig


if __name__ == "__main__":
    import unittest
    unittest.main()
