import asyncio
import json
import os
import plistlib
import signal
import sys
import time

from helpers import REPO, TempEnv, assistant, load_daemon, user

from ccsessions import ledger, pending

D = load_daemon()
NOW = 2_000_000_000.0


def focus(ttys=("ttys001",), visible=()):
    return {"tty_guid": {t: "G-" + t for t in ttys}, "visible_ttys": set(visible), "visible_guids": set()}


class Policy(TempEnv):
    def setUp(self):
        super().setUp()
        self.tr = self.transcript("s1", [user("hello"), assistant("done")])

    def row(self, **kw):
        r = {"session_id": "s1", "source": "hook", "interactive": 1, "state": "idle",
             "state_since": NOW - 3600, "subagents": 0, "tty": "ttys001", "last_focus_at": None,
             "resumed_at": None, "pid": 10, "pid_start": "x", "transcript": self.tr,
             "last_event_at": NOW - 3600}
        r.update(kw)
        return r

    def guard(self, row, **kw):
        args = dict(now=NOW, cfg=self.cfg, focus_state=focus(), pending_fn=lambda r: {"pending": [], "has_user": True},
                    children_fn=lambda r: [], identity_fn=lambda r: None, activity_fn=lambda r: NOW - 3600,
                    evictions_today={}, hook_failed=set(), explicit=False)
        args.update(kw)
        return D.guard_reason(row, **args)

    def test_happy_path_is_candidate(self):
        self.assertIsNone(self.guard(self.row()))

    def test_each_guard_keeps(self):
        cases = [
            (self.row(source="legacy"), {}, "source=legacy"),
            (self.row(interactive=0), {}, "non-interactive"),
            (self.row(), {"hook_failed": {"s1"}}, "a hook failed"),
            (self.row(state="busy", state_since=NOW - 60), {}, "state=busy"),
            (self.row(state="waiting"), {}, "state=waiting"),
            (self.row(state_since=NOW - 120), {}, "idle 2m"),
            (self.row(subagents=1), {}, "subagents=1"),
            (self.row(tty="ttys009"), {}, "no iTerm2 tab"),
            (self.row(tty=None), {}, "no iTerm2 tab"),
            (self.row(), {"focus_state": focus(visible=("ttys001",))}, "visible tab"),
            (self.row(last_focus_at=NOW - 60), {}, "focused 1m ago"),
            (self.row(resumed_at=NOW - 600), {}, "cooldown"),
            (self.row(), {"evictions_today": {"s1": 3}}, "evicted 3x today"),
            (self.row(), {"identity_fn": lambda r: "process gone or pid reused"}, "process gone"),
            (self.row(transcript=None), {}, "no transcript"),
            (self.row(transcript=os.path.join(self.tmp, "nope.jsonl")), {}, "no transcript"),
            (self.row(), {"activity_fn": lambda r: NOW - 30}, "active 0m ago"),
            (self.row(), {"activity_fn": lambda r: None}, "activity unreadable"),
            (self.row(), {"pending_fn": lambda r: {"pending": [], "has_user": False}}, "no user message"),
            (self.row(), {"pending_fn": lambda r: {"pending": ["1 agent running"], "has_user": True}}, "pending: 1 agent"),
            (self.row(), {"children_fn": lambda r: ["caffeinate"]}, "running: caffeinate"),
        ]
        for row, kw, want in cases:
            got = self.guard(row, **kw)
            self.assertIsNotNone(got, want)
            self.assertTrue(got.startswith(want), "%r vs %r" % (got, want))

    def test_R13_explicit_bypasses_only_idle_min(self):
        self.assertIsNone(self.guard(self.row(state_since=NOW - 5), explicit=True))
        for row, kw in [(self.row(state_since=NOW - 5, last_focus_at=NOW - 5), {}),
                        (self.row(state_since=NOW - 5), {"focus_state": focus(visible=("ttys001",))}),
                        (self.row(state_since=NOW - 5), {"children_fn": lambda r: ["zsh"]}),
                        (self.row(state_since=NOW - 5), {"pending_fn": lambda r: ["queued"]}),
                        (self.row(state_since=NOW - 5), {"activity_fn": lambda r: NOW - 1}),
                        (self.row(state_since=NOW - 5, transcript=None), {}),
                        (self.row(state_since=NOW - 5, resumed_at=NOW - 60), {}),
                        (self.row(state_since=NOW - 5), {"identity_fn": lambda r: "pid reused"})]:
            self.assertIsNotNone(self.guard(row, explicit=True, **kw))

    def test_R6_stale_busy_rescue(self):
        interrupted = {"pending": [], "has_user": True, "last_kind": "user",
                       "last_user_text": "[Request interrupted by user]"}
        busy = self.row(state="busy", state_since=NOW - 31 * 60)
        self.assertIsNone(self.guard(busy, pending_fn=lambda r: interrupted))
        normal = {"pending": [], "has_user": True, "last_kind": "assistant"}
        self.assertEqual(self.guard(busy, pending_fn=lambda r: normal), "state=busy")
        self.assertIsNotNone(self.guard(busy, pending_fn=lambda r: interrupted, activity_fn=lambda r: NOW - 5))
        self.assertEqual(self.guard(self.row(state="busy", state_since=NOW - 29 * 60),
                                    pending_fn=lambda r: interrupted), "state=busy")

    def test_ordering_lru_focus_then_rss(self):
        rows = [self.row(session_id="a", last_focus_at=NOW - 7200),
                self.row(session_id="b", last_focus_at=None),
                self.row(session_id="c", last_focus_at=None),
                self.row(session_id="d", last_focus_at=NOW - 9000)]
        cands, kept = D.choose_candidates(rows, NOW, self.cfg, focus(), {"b": 100, "c": 500},
                                          lambda r: [], lambda r: [], identity_fn=lambda r: None,
                                          activity_fn=lambda r: NOW - 3600)
        self.assertEqual([r["session_id"] for r in cands], ["c", "b", "d", "a"])
        self.assertEqual(kept, [])

    def test_choose_candidates_positional_signature(self):
        cands, kept = D.choose_candidates([self.row(state="busy", state_since=NOW)], NOW, self.cfg, focus(), {},
                                          lambda r: [], lambda r: [])
        self.assertEqual(cands, [])
        self.assertEqual(kept[0][1], "state=busy")

    def test_R7_caffeinate_is_work_mcp_is_helper(self):
        from ccsessions import procs
        table = {10: (1, 100, "claude"),
                 11: (10, 50, "npm exec @scope/some-mcp"),
                 12: (11, 50, "node /x/.bin/server"),
                 13: (10, 1, "caffeinate -i -t 300"),
                 14: (10, 1, "/bin/zsh -c source /x/shell-snapshots/s.sh && make"),
                 15: (14, 1, "make")}
        kids = procs.working_children(10, table, self.cfg["helper_patterns"])
        self.assertIn("caffeinate", kids)
        self.assertIn("shell task", kids)
        self.assertIn("make", kids)
        self.assertNotIn("node", kids)
        self.assertEqual(procs.working_children(10, {10: (1, 1, "claude"), 11: (10, 1, "x-mcp")},
                                                self.cfg["helper_patterns"]), [])
        self.assertNotIn("caffeinate", self.cfg["helper_patterns"])

    def test_R1_identity_checks_registry(self):
        chk = D.default_identity(self.cfg)
        self.assertEqual(chk({"session_id": "s1", "pid": 10, "pid_start": None}), "no recorded process start time")
        orig = D.procs.identity_probe
        try:
            D.procs.identity_probe = lambda pid, start, **kw: D.procs.GONE
            self.assertEqual(chk({"session_id": "s1", "pid": 10, "pid_start": 5}), "process gone or pid reused")
            D.procs.identity_probe = lambda pid, start, **kw: D.procs.UNKNOWN
            self.assertEqual(chk({"session_id": "s1", "pid": 10, "pid_start": 5}), "process identity unknown")
        finally:
            D.procs.identity_probe = orig

    def test_R1_registry_mismatch_keeps(self):
        os.makedirs(self.cfg.path("claude_sessions"))
        with open(os.path.join(self.cfg.path("claude_sessions"), "10.json"), "w") as fh:
            json.dump({"sessionId": "other"}, fh)
        orig = D.procs.identity_probe
        D.procs.identity_probe = lambda pid, start, **kw: D.procs.ALIVE
        try:
            chk = D.default_identity(self.cfg)
            self.assertEqual(chk({"session_id": "s1", "pid": 10, "pid_start": "x"}), "registry names another session")
            with open(os.path.join(self.cfg.path("claude_sessions"), "10.json"), "w") as fh:
                fh.write("{broken")
            self.assertEqual(chk({"session_id": "s1", "pid": 10, "pid_start": "x"}), "registry file unreadable")
            with open(os.path.join(self.cfg.path("claude_sessions"), "10.json"), "w") as fh:
                json.dump({"sessionId": "s1"}, fh)
            self.assertIsNone(chk({"session_id": "s1", "pid": 10, "pid_start": "x"}))
        finally:
            D.procs.identity_probe = orig

    def test_R3_activity_sources(self):
        act = D.default_activity(self.cfg)
        old = time.time() - 7200
        os.utime(self.tr, (old, old))
        self.assertAlmostEqual(act({"session_id": "s1", "transcript": self.tr}), old, delta=1)
        sub = self.tr[:-6] + "/subagents"
        os.makedirs(sub)
        open(os.path.join(sub, "a.jsonl"), "w").close()
        self.assertGreater(act({"session_id": "s1", "transcript": self.tr}), old + 3600)
        os.unlink(os.path.join(sub, "a.jsonl"))
        tasks = os.path.join(self.cfg.path("claude_tmp"), "proj", "s1", "tasks")
        os.makedirs(tasks)
        open(os.path.join(tasks, "t1.output"), "w").close()
        self.assertGreater(act({"session_id": "s1", "transcript": self.tr}), old + 3600)

    def test_R3_hook_failed_markers_read(self):
        d = os.path.join(os.path.dirname(self.db), "hook-failed")
        os.makedirs(d)
        open(os.path.join(d, "s1"), "w").close()
        self.assertEqual(D.hook_failed_sids(self.cfg), {"s1"})

    def test_R9_real_transcript_without_user(self):
        tr = self.transcript("s2", [{"type": "summary", "summary": "x"}])
        a = pending.analyze(tr, NOW)
        self.assertEqual(self.guard(self.row(transcript=tr), pending_fn=lambda r: a), "no user message")


class ResumeDecision(TempEnv):
    def test_guid_rules(self):
        now = NOW
        alive = lambda r: False
        hib = {"state": "hibernated", "iterm_guid": "G1", "tty": "ttys001", "evicted_at": now - 100}
        self.assertIsNone(D.resume_decision(hib, "G1", "ttys001", now, {}, "-zsh", alive))
        self.assertIn("GUID changed", D.resume_decision(hib, "G2", "ttys001", now, {}, "zsh", alive))
        self.assertEqual(D.resume_decision(hib, "G1", "ttys001", now, {"G1": now - 5}, "zsh", alive), "debounce")
        self.assertIn("running vim", D.resume_decision(hib, "G1", "ttys001", now, {}, "vim", alive))
        self.assertEqual(D.resume_decision(hib, "G1", "ttys001", now, {}, "zsh", lambda r: True), "already running")
        self.assertEqual(D.resume_decision(dict(hib, state="idle"), "G1", "ttys001", now, {}, "zsh", alive), "state=idle")

    def test_fix19_rows_without_guid_never_resume_on_focus(self):
        now, alive = NOW, (lambda r: False)
        imp = {"state": "hibernated", "iterm_guid": None, "tty": "ttys001", "evicted_at": now - 60}
        self.assertIn("cc-sessions wake", D.resume_decision(imp, "G9", "ttys001", now, {}, "zsh", alive))

    def test_fix14_non_interactive_never_resumes(self):
        row = {"state": "hibernated", "iterm_guid": "G1", "tty": "ttys001", "interactive": 0}
        self.assertEqual(D.resume_decision(row, "G1", "ttys001", NOW, {}, "zsh", lambda r: False),
                         "non-interactive")


class FakeTabs(object):
    def __init__(self, jobs, tty="ttys001", visible=False):
        self.jobs = list(jobs)
        self.calls = []
        self.tty_now, self.visible_now = tty, visible

    async def tty(self, guid):
        return "/dev/" + self.tty_now if self.tty_now else None

    async def is_visible(self, guid):
        return self.visible_now

    async def job_name(self, guid):
        self.calls.append(("job", guid))
        return self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]

    async def inject(self, guid, data):
        self.calls.append(("inject", guid, data))

    async def set_name(self, guid, name):
        self.calls.append(("name", guid, name))

    async def send_text(self, guid, text):
        self.calls.append(("send", guid, text))


class Clock(object):
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


class Eviction(TempEnv):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.row = self.insert(session_id="s1", state="idle", last_event_at=NOW - 3600, iterm_guid="G1",
                               cwd="/w/it's here", title="Title")
        self.kills = []
        self.alive = True
        self.kids, self.kid_state = [], {}

    def evictor(self, tabs, on_evict=None, identity=None):
        def kill(pid, sig):
            self.kills.append((pid, sig))
        return D.Evictor(self.conn, self.cfg, tabs, kill=kill, identity=identity or (lambda r: self.alive),
                         clock=self.clock, sleep=self.clock.sleep, on_evict=on_evict,
                         children=lambda r: list(self.kids), child_probe=lambda p, s: self.kid_state.get(p, "gone"))

    def arun(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_term_path_records_first_then_types_resume(self):
        tabs = FakeTabs(["claude", "-zsh"])
        seen_state = []

        def kill(pid, sig):
            seen_state.append(ledger.state_of(self.conn, "s1"))
            self.kills.append((pid, sig))
            self.alive = False
        ev = self.evictor(tabs)
        ev.kill = kill
        hooks = []
        ev.on_evict = lambda exe, env: hooks.append(env)
        self.cfg["on_evict"] = "/bin/true"
        out = self.arun(ev.evict(self.row, 12, 450.0))
        self.assertEqual(out, "term")
        self.assertEqual(seen_state, ["evicting"])  # recorded BEFORE the signal
        self.assertEqual(self.kills, [(111, signal.SIGTERM)])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "hibernated")
        e = dict(self.conn.execute("SELECT * FROM evictions").fetchone())
        self.assertEqual((e["outcome"], e["signal"], e["free_pct"]), ("term", "SIGTERM", 12))
        sent = [c for c in tabs.calls if c[0] == "send"]
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][2].endswith("s1 --cwd '/w/it'\"'\"'s here'"), sent[0][2])
        self.assertFalse(sent[0][2].endswith("\r"))
        self.assertIn(("name", "G1", "[zz] Title"), tabs.calls)
        self.assertEqual(hooks[0]["CC_SESSION_ID"], "s1")
        self.assertEqual(hooks[0]["CC_EVICT_OUTCOME"], "term")

    def test_sigkill_escalation_is_an_incident(self):
        tabs = FakeTabs(["zsh"])
        ev = self.evictor(tabs)

        def kill(pid, sig):
            self.kills.append((pid, sig))
            if sig == signal.SIGKILL and pid == 111:
                self.alive = False
        ev.kill = kill
        self.kids = [(501, 9001), (502, 9002)]
        self.kid_state = {501: "alive", 502: "gone"}
        out = self.arun(ev.evict(self.row, 12, 1.0))
        self.assertEqual(out, "killed")
        # fix 20: only the captured child whose identity is still alive is SIGKILLed
        self.assertEqual(self.kills, [(111, signal.SIGTERM), (111, signal.SIGKILL), (501, signal.SIGKILL)])
        self.assertGreaterEqual(self.clock.t - NOW, self.cfg["term_grace_s"])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "hibernated")
        self.assertEqual(self.conn.execute("SELECT outcome FROM evictions").fetchone()[0], "killed")

    def test_R2_cas_lost_sends_nothing(self):
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET last_event_at=? WHERE session_id='s1'", (NOW,))
        out = self.arun(self.evictor(FakeTabs(["zsh"])).evict(self.row, 12, 1.0))
        self.assertEqual(out, "aborted-cas")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")

    def test_R2_recheck_right_before_signal(self):
        out = self.arun(self.evictor(FakeTabs(["zsh"])).evict(self.row, 12, 1.0, recheck=lambda r: "visible tab"))
        self.assertEqual(out, "aborted-recheck")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")

        async def arecheck(r):
            return "running: make"
        out = self.arun(self.evictor(FakeTabs(["zsh"])).evict(self.row, 12, 1.0, recheck=arecheck))
        self.assertEqual(out, "aborted-recheck")

    def test_R1_identity_change_before_signal_rolls_back(self):
        out = self.arun(self.evictor(FakeTabs(["zsh"]), identity=lambda r: False).evict(self.row, 12, 1.0))
        self.assertEqual(out, "aborted-identity")
        self.assertEqual(self.kills, [])
        self.assertEqual(ledger.state_of(self.conn, "s1"), "idle")
        self.assertEqual(self.conn.execute("SELECT outcome FROM evictions").fetchone()[0], "aborted")

    def test_tab_not_at_shell_types_nothing(self):
        def kill(pid, sig):
            self.alive = False
        ev = self.evictor(FakeTabs(["vim"]))
        ev.kill = kill
        self.assertEqual(self.arun(ev.evict(self.row, 12, 1.0)), "term")
        self.assertFalse([c for c in ev.tabs.calls if c[0] in ("send", "inject")])

    def test_session_end_flip_is_respected(self):
        def kill(pid, sig):
            self.alive = False
            ledger.apply_event(self.conn, "SessionEnd", {"session_id": "s1", "reason": "other"}, None)
        ev = self.evictor(FakeTabs(["zsh"]))
        ev.kill = kill
        self.assertEqual(self.arun(ev.evict(self.row, 12, 1.0)), "term")
        r = ledger.get(self.conn, "s1")
        self.assertEqual((r["state"], r["end_reason"], r["ended_at"]), ("hibernated", "other", None))


class PressureLoop(TempEnv):
    def arun(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_hysteresis_stops_at_high(self):
        clock = Clock()
        frees = iter([25, 36])
        p = D.Pressure(self.cfg, lambda: next(frees), clock=clock, sleep=clock.sleep)
        calls = []

        async def evict(free):
            calls.append(free)
            return "term"
        self.assertEqual(self.arun(p.relieve(15, evict)), "relieved")
        self.assertEqual(calls, [15, 25])
        self.assertEqual(self.arun(p.relieve(21, evict)), "comfortable")
        self.assertEqual(self.arun(p.relieve(None, evict)), "comfortable")

    def test_R12_global_backoff_when_free_does_not_rise(self):
        clock = Clock()
        p = D.Pressure(self.cfg, lambda: 15.5, clock=clock, sleep=clock.sleep)
        calls = []

        async def evict(free):
            calls.append(free)
            return "term"
        self.assertEqual(self.arun(p.relieve(15, evict)), "backoff")
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock.t - NOW, self.cfg["settle_s"])
        self.assertEqual(self.arun(p.relieve(10, evict)), "backoff")
        self.assertEqual(len(calls), 1)
        clock.t += self.cfg["backoff_min"] * 60 + 1
        # 10 -> 15.5 rose enough, still under high: evict again; 15.5 -> 15.5: back off
        self.assertEqual(self.arun(p.relieve(10, evict)), "backoff")
        self.assertEqual(calls, [15, 10, 15.5])

    def test_no_candidate_and_abort_cap(self):
        p = D.Pressure(self.cfg, lambda: 10)

        async def none(free):
            return None
        self.assertEqual(self.arun(p.relieve(5, none)), "no-candidate")
        n = []

        async def aborts(free):
            n.append(1)
            return "aborted-cas"
        self.assertEqual(self.arun(p.relieve(5, aborts)), "aborted")
        self.assertEqual(len(n), D.MAX_ABORTS_PER_PASS)


class Lifecycle(TempEnv):
    def test_R2_startup_reconcile(self):
        self.insert(session_id="a", state="evicting")
        self.insert(session_id="b", state="resuming")
        self.insert(session_id="c", state="evicting", pid=222)
        fixed = D.reconcile(self.conn, lambda r: r["pid"] == 222)
        self.assertEqual(sorted(fixed), [("a", "evicting", "hibernated"), ("c", "evicting", "idle")])
        self.assertEqual(ledger.state_of(self.conn, "b"), "resuming")  # expire_resuming owns it

    def test_R10_resuming_times_out(self):
        now = time.time()
        self.insert(session_id="a", state="resuming", state_since=now - 200)
        self.insert(session_id="b", state="resuming", state_since=now - 30)
        self.assertEqual(D.expire_resuming(self.conn, self.cfg, now), 1)
        self.assertEqual(ledger.state_of(self.conn, "a"), "hibernated")
        self.assertEqual(ledger.state_of(self.conn, "b"), "resuming")

    def test_R5_backoff_sequence_caps(self):
        g = D.backoff_delays()
        self.assertEqual([next(g) for _ in range(6)], [5, 10, 20, 40, 60, 60])

    def test_R5_single_instance_lock(self):
        a = D.take_lock(self.cfg)
        self.assertIsNotNone(a)
        self.addCleanup(a.close)
        self.assertIsNone(D.take_lock(self.cfg))

    def test_R5_launchd_template(self):
        with open(os.path.join(REPO, "launchd", "com.cc-sessions.daemon.plist.in"), "rb") as fh:
            raw = fh.read().replace(b"@DAEMON_PYTHON@", b"/p/python").replace(
                b"@DAEMON_SCRIPT@", b"/p/d.py").replace(b"@LOG_DIR@", b"/p/logs")
        pl = plistlib.loads(raw)
        self.assertIs(pl["KeepAlive"], True)
        self.assertIs(pl["RunAtLoad"], True)
        self.assertEqual(pl["ThrottleInterval"], 30)
        self.assertEqual(pl["ProcessType"], "Interactive")
        self.assertEqual(pl["ProgramArguments"], ["/p/python", "/p/d.py"])
        self.assertTrue(pl["StandardErrorPath"].startswith("/p/logs/"))

    def test_daemon_module_imports_without_iterm2(self):
        self.assertNotIn("iterm2", sys.modules)
        self.assertTrue(callable(D.choose_candidates))


class FakeSession(object):
    def __init__(self, guid, tty, job="-zsh"):
        self.session_id, self.tty, self.job = guid, tty, job
        self.sent, self.names = [], []

    async def async_get_variable(self, name):
        return {"tty": "/dev/" + self.tty if self.tty else None, "jobName": self.job}.get(name)

    async def async_send_text(self, text):
        self.sent.append(text)

    async def async_set_name(self, name):
        self.names.append(name)


class FakeTab(object):
    def __init__(self, tab_id, sessions):
        self.tab_id, self.sessions = tab_id, sessions


class FakeWindow(object):
    def __init__(self, tabs, current):
        self.tabs, self.current_tab = tabs, current


class FakeApp(object):
    def __init__(self, windows):
        self.terminal_windows = windows


class ItermSide(TempEnv):
    def arun(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_R8_every_pane_of_the_current_tab_is_visible(self):
        a, b = FakeSession("G1", "ttys001"), FakeSession("G2", "ttys002")
        c = FakeSession("G3", "ttys003")
        t1, t2 = FakeTab("T1", [a, b]), FakeTab("T2", [c])
        snap = self.arun(D.focus_snapshot(FakeApp([FakeWindow([t1, t2], t1)])))
        self.assertEqual(snap["visible_ttys"], {"ttys001", "ttys002"})
        self.assertEqual(snap["tty_guid"], {"ttys001": "G1", "ttys002": "G2", "ttys003": "G3"})

    def daemon(self, dry_run=False):
        d = D.Daemon(self.cfg, self.conn, FakeApp([]), None, dry_run)

        async def ident(r):
            ok = r.get("state") in ("idle", "busy") and r.get("pid") == 111
            return D.procs.ALIVE if ok else D.procs.GONE
        d.identity = ident
        return d

    def test_focus_on_live_session_records_focus(self):
        self.insert(session_id="live", tty="ttys001")
        self.arun(self.daemon().on_focus(FakeSession("G1", "ttys001")))
        r = ledger.get(self.conn, "live")
        self.assertEqual(r["iterm_guid"], "G1")
        self.assertIsNotNone(r["last_focus_at"])

    def test_focus_on_hibernated_tab_resumes_once(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600, pid=222, cwd="/w/x", title="T")
        d = self.daemon()
        s = FakeSession("G2", "ttys002")
        self.arun(d.on_focus(s))
        self.assertEqual(len(s.sent), 1)
        self.assertTrue(s.sent[0].startswith("\x15") and s.sent[0].endswith("hib --cwd /w/x\r"), s.sent)
        self.assertEqual(s.names, ["T"])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "resuming")
        self.arun(d.on_focus(s))
        self.assertEqual(len(s.sent), 1)

    def test_focus_on_reused_tty_in_new_tab_does_not_resume(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600)
        s = FakeSession("G-new", "ttys002")
        self.arun(self.daemon().on_focus(s))
        self.assertEqual(s.sent, [])
        self.assertEqual(ledger.state_of(self.conn, "hib"), "hibernated")

    def test_dry_run_focus_does_not_resume(self):
        self.insert(session_id="hib", tty="ttys002", state="hibernated", iterm_guid="G2",
                    evicted_at=time.time() - 600)
        s = FakeSession("G2", "ttys002")
        self.arun(self.daemon(dry_run=True).on_focus(s))
        self.assertEqual(s.sent, [])
