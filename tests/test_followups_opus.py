"""Follow-ups from the Opus review: an evicting row keeps its tab GUID when its tty is
stamped, a transcript title read that raises never fails a policy pass, only the first
candidate's title is resolved, doctor's busy needs liveness since the pass start, and the
pass meta is written lazily and always cleared."""
import time

from helpers import TempEnv, assistant, load_daemon, user
from test_idle_hibernate import IdleBase, arun

from ccsessions import cli, ledger, pending, procs

D = load_daemon()


def meta(conn, key):
    v = ledger.get_meta(conn, key)
    return float(v) if v not in (None, "") else None


def focus():
    return {"tty_guid": {}, "visible_ttys": set(), "visible_guids": set(), "panes": 0,
            "skipped": 0}


def ai(sid, t):
    return {"type": "ai-title", "aiTitle": t, "sessionId": sid}


# ------------------------------------------------------------------ 1. stamp_visible

class StampEvicting(TempEnv):
    def test_evicting_row_keeps_its_guid_but_gets_last_focus_at(self):
        self.insert(session_id="ev", tty="ttys001", state="evicting", iterm_guid="G-orig",
                    last_focus_at=None)
        self.insert(session_id="lv", tty="ttys002", state="idle", iterm_guid="G-old",
                    last_focus_at=None)
        n = ledger.stamp_visible(self.conn, {"ttys001": "G-other", "ttys002": "G-new"}, 1234.0)
        self.assertEqual(n, 2)
        ev, lv = ledger.get(self.conn, "ev"), ledger.get(self.conn, "lv")
        self.assertEqual(ev["iterm_guid"], "G-orig")
        self.assertEqual(ev["last_focus_at"], 1234.0)
        self.assertEqual(lv["iterm_guid"], "G-new")
        self.assertEqual(lv["last_focus_at"], 1234.0)


# ------------------------------------------------------------------ 2, 3, 5c. titles

class PolicyTitles(TempEnv):
    def setUp(self):
        super().setUp()
        saved, saved_err = D.TITLES, D.TITLE_ERRORS
        D.TITLES, D.TITLE_ERRORS = D.TitleCache(), set()
        self.addCleanup(setattr, D, "TITLES", saved)
        self.addCleanup(setattr, D, "TITLE_ERRORS", saved_err)
        real = D.choose_candidates
        D.choose_candidates = lambda rows, *a, **kw: (list(rows), [])
        self.addCleanup(setattr, D, "choose_candidates", real)

    def patch_scan(self, fn):
        real = pending.transcript_title
        pending.transcript_title = fn
        self.addCleanup(setattr, pending, "transcript_title", real)

    def add(self, sid, title, ledger_title="slug"):
        tr = self.transcript(sid, [user("hi"), assistant("ok"), ai(sid, title)])
        return self.insert(session_id=sid, tty=None, pid=None, transcript=tr, title=ledger_title)

    def test_raising_title_read_falls_back_to_the_ledger_title_and_logs_once(self):
        row = self.add("s1", "never read", ledger_title="Ledger title")

        def boom(path):
            raise RuntimeError("pathological transcript")
        self.patch_scan(boom)
        with self.assertLogs(D.log, "ERROR") as logs:
            self.assertEqual(D.title_for(dict(row)), "Ledger title")
            self.assertEqual(D.title_for(dict(row)), "Ledger title")
        self.assertEqual(len(logs.records), 1)
        cands, _, _, titles = D.compute_policy(self.cfg, focus(), [dict(row)], {})
        self.assertEqual([c["session_id"] for c in cands], ["s1"])
        self.assertEqual(titles, {"s1": "Ledger title"})

    def test_only_the_first_candidate_title_is_scanned(self):
        for i in range(5):
            self.add("s%d" % i, "title %d" % i)
        calls = []
        real = pending.transcript_title

        def counting(path):
            calls.append(path)
            return real(path)
        self.patch_scan(counting)
        cands, _, _, titles = D.compute_policy(self.cfg, focus(), D.live_rows(self.conn), {})
        self.assertEqual(len(cands), 5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(titles.values()), [cands[0]["title"]])

    def test_policy_pass_writes_the_transcript_title_back(self):
        self.add("s1", "Real title", ledger_title="slug")
        D.policy_pass(self.conn, self.cfg, focus())
        self.assertEqual(ledger.get(self.conn, "s1")["title"], "Real title")


# ------------------------------------------------------------------ 4. doctor busy rule

class DoctorBusyRule(TempEnv):
    NOW = 2_000_000_000.0

    def hb(self, **kv):
        D.record_meta(self.conn, heartbeat=self.NOW - 400, pid=4242, **kv)
        return [c for c in cli.daemon_checks(self.conn, now=self.NOW, alive=lambda pid: True)
                if c[1] == "daemon heartbeat"][0][0]

    def test_busy_needs_liveness_since_pass_start_so_a_hung_pass_fails_after_120s(self):
        self.assertEqual(cli.DAEMON_FRESH_S, 120)
        # liveness from before the pass (e.g. record_start) never vouches for it
        self.assertEqual(self.hb(pass_started_at=self.NOW - 10, liveness=self.NOW - 20), "FAIL")
        # written with the pass start: busy while fresh
        self.assertEqual(self.hb(pass_started_at=self.NOW - 100, liveness=self.NOW - 100), "PASS")
        # no liveness progress for 120 s: hung
        self.assertEqual(self.hb(pass_started_at=self.NOW - 121, liveness=self.NOW - 121), "FAIL")
        # progress after the pass start keeps it busy
        self.assertEqual(self.hb(pass_started_at=self.NOW - 300, liveness=self.NOW - 5), "PASS")


# ------------------------------------------------------------------ 4, 5a, 5b. pass meta

class PassMeta(IdleBase):
    def tick_daemon(self, ttys):
        d = self.daemon(ttys)
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
        frees = iter([12, 14, 16])
        d.pressure.free_fn = lambda: next(frees)
        return d

    def test_tick_without_evictions_writes_no_pass_meta(self):
        self.cfg["idle_hibernate_min"] = 0
        d = self.tick_daemon([])
        written = []
        real = D.record_meta

        def spy(conn, **kv):
            written.extend(kv)
            return real(conn, **kv)
        D.record_meta = spy
        self.addCleanup(setattr, D, "record_meta", real)
        arun(d.tick())
        self.assertNotIn("pass_started_at", written)
        self.assertGreater(meta(self.conn, "heartbeat"), 0)

    def test_exception_inside_busy_clears_depth_and_pass_meta(self):
        self.cfg["idle_hibernate_min"] = 0
        self.add("a", "ttys001")
        self.add("b", "ttys002")
        d = self.tick_daemon(["ttys001", "ttys002"])
        calls = []

        async def snap():
            calls.append(1)
            if len(calls) == 3:  # tick, first pick, then the second pick times out
                raise TimeoutError("snapshot")
            return self.focus
        d.snapshot = snap
        with self.assertRaises(TimeoutError):
            arun(d.tick())
        self.assertEqual(len(self.evicted), 1)
        self.assertEqual(d.busy_depth, 0)
        self.assertFalse(d.pass_marked)
        self.assertIsNone(meta(self.conn, "pass_started_at"))

    def test_idle_pass_with_nonzero_minutes_writes_liveness_after_each_eviction(self):
        self.assertEqual(self.cfg["idle_hibernate_min"], 3)
        self.add("a", "ttys001")
        self.add("b", "ttys002")
        d = self.daemon(["ttys001", "ttys002"])
        D.record_meta(self.conn, liveness=0)
        seen = []
        real = d.evictor.evict

        async def evict(row, *a, **kw):
            seen.append(meta(self.conn, "liveness"))
            return await real(row, *a, **kw)
        d.evictor.evict = evict
        self.assertEqual(arun(d.idle_pass(50)), ["term", "term"])
        self.assertEqual(seen[0], 0.0)
        self.assertGreater(seen[1], 0.0)
        self.assertGreaterEqual(meta(self.conn, "liveness"), seen[1])
