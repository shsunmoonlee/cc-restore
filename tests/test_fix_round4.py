"""Fix round 4: pgrep ancestors, restore --include-hibernated / --max-age-days, the test
log path, doctor's hook count, the API-disabled retry, snapshot blindness, and the
pre-connect liveness mutation gap. One or more tests per item."""
import asyncio
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest.mock

from helpers import REAL_LOG, REPO, TempEnv, load_daemon

from ccsessions import cli, config, ledger, restore

D = load_daemon()


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Patch(object):
    def __init__(self, test, obj, name, value):
        old = getattr(obj, name)
        setattr(obj, name, value)
        test.addCleanup(setattr, obj, name, old)


class RPCException(Exception):
    pass


def fake_iterm2(run_until_complete=None):
    mod = types.ModuleType("iterm2")
    rpc = types.ModuleType("iterm2.rpc")
    rpc.RPCException = RPCException
    mod.rpc = rpc
    mod.run_until_complete = run_until_complete

    async def get_app(connection):
        return types.SimpleNamespace(connection=connection, terminal_windows=[])
    mod.async_get_app = get_app
    return {"iterm2": mod, "iterm2.rpc": rpc}


# ------------------------------------------------------------------ 1. pgrep -a

class Item1Pgrep(TempEnv):
    def test_iterm_guids_asks_pgrep_with_ancestors_included(self):
        calls = []

        def fake_run(args, **kw):
            calls.append(list(args))
            if args[0] == "pgrep":
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="G1\nG2\n", stderr="")
        Patch(self, restore.subprocess, "run", fake_run)
        self.assertEqual(restore.iterm_guids(), {"G1", "G2"})
        self.assertEqual(calls[0], ["pgrep", "-axq", "iTerm2"])

    def test_every_pgrep_for_iterm2_in_the_tree_passes_a(self):
        found = 0
        for sub in ("lib", "bin", "daemon", "hooks"):
            root = os.path.join(REPO, sub)
            for dirpath, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    with open(os.path.join(dirpath, f), errors="replace") as fh:
                        for line in fh:
                            if "pgrep" in line and "iTerm2" in line and not line.lstrip().startswith("#"):
                                found += 1
                                flags = re.findall(r'"(-[A-Za-z]+)"', line)
                                self.assertTrue(any("a" in fl for fl in flags), "%s: %s" % (f, line))
        self.assertGreater(found, 0)


# ------------------------------------------------------------------ 2. restore

BOOT = 1_000_000.0


class SelectBase(TempEnv):
    def rows(self, *specs):
        base = {"source": "hook", "interactive": 1, "state": "idle", "ended_at": None,
                "end_reason": None, "cwd": self.tmp, "pid": 1, "pid_start": "x", "iterm_guid": None,
                "last_event_at": BOOT - 100}
        out = []
        for i, s in enumerate(specs):
            r = dict(base, session_id="s%d" % i)
            r.update(s)
            out.append(r)
        return out

    def sel(self, rows, now=BOOT + 60, **kw):
        kw.setdefault("alive_fn", lambda r: False)
        run, typed, skipped = restore.select(rows, BOOT, now, self.cfg, **kw)
        return ([r["session_id"] for r in run], [r["session_id"] for r in typed],
                {r["session_id"]: why for r, why in skipped})

    def worktree(self, repo_exists=True, git=True):
        repo = os.path.join(self.tmp, "repo")
        if repo_exists:
            os.makedirs(repo, exist_ok=True)
            if git:
                os.makedirs(os.path.join(repo, ".git"), exist_ok=True)
        return repo + self.cfg["worktree_marker"] + "gone-wt"


class Item2Select(SelectBase):
    def test_a_include_hibernated_skips_a_live_tab(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "LIVE-0001"},
                         {"state": "hibernated", "iterm_guid": "GONE-0002"})
        _, typed, skipped = self.sel(rows, include_hibernated=True, live_guids={"LIVE-0001"})
        self.assertEqual(typed, ["s1"])
        self.assertEqual(skipped["s0"], "hibernated (tab still open: focus it or `cc-sessions wake`)")

    def test_a_include_hibernated_without_guids_keeps_old_behaviour(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "LIVE-0001"})
        _, typed, _ = self.sel(rows, include_hibernated=True, live_guids=None)
        self.assertEqual(typed, ["s0"])

    def test_b_missing_worktree_with_live_repo_is_kept_for_typed_only(self):
        rows = self.rows({"cwd": self.worktree()},
                         {"state": "hibernated", "cwd": self.worktree()})
        run, typed, skipped = self.sel(rows, include_hibernated=True)
        self.assertEqual((run, typed), ([], ["s1"]))
        self.assertTrue(skipped["s0"].startswith("cwd gone"))
        self.assertNotIn("s1", skipped)

    def test_b_missing_worktree_with_gone_repo_is_skipped(self):
        rows = self.rows({"cwd": self.worktree(repo_exists=False)})
        run, _, skipped = self.sel(rows)
        self.assertEqual(run, [])
        self.assertTrue(skipped["s0"].startswith("cwd gone"))

    def test_b_missing_plain_cwd_is_still_skipped(self):
        rows = self.rows({"cwd": os.path.join(self.tmp, "nope")})
        run, _, skipped = self.sel(rows)
        self.assertEqual(run, [])
        self.assertTrue(skipped["s0"].startswith("cwd gone"))

    def test_c_max_age_days_overrides_config(self):
        rows = self.rows({"last_event_at": BOOT - 10 * 86400})
        self.assertEqual(self.sel(rows)[0], [])  # default 7 days
        self.assertEqual(self.sel(rows, max_age_days=20)[0], ["s0"])
        self.assertEqual(self.sel(rows, max_age_days=0)[0], ["s0"])  # no limit
        self.assertEqual(self.sel(rows, max_age_days=0, now=BOOT + 1e9)[0], ["s0"])
        self.assertIn("older than 5d", self.sel(rows, max_age_days=5)[2]["s0"])


class RunBase(TempEnv):
    def setUp(self):
        super().setUp()
        self.boot = time.time() - 1000
        Patch(self, restore.procs, "boot_time", lambda: self.boot)
        Patch(self, restore.procs, "identity_alive", lambda pid, start: False)
        self.guid_calls = []

    def guids(self, value):
        def f():
            self.guid_calls.append(1)
            return value
        Patch(self, restore, "iterm_guids", f)

    def run_restore(self, **kw):
        lines = []
        rc = restore.run(self.cfg, self.conn, dry_run=True, out=lines.append, **kw)
        return rc, "\n".join(lines)


class Item2Run(RunBase):
    def test_a_non_auto_include_hibernated_queries_guids_and_skips_live_tab(self):
        self.guids({"LIVE-0001"})
        self.insert(session_id="h1", state="hibernated", iterm_guid="LIVE-0001", last_event_at=self.boot - 100)
        self.insert(session_id="h2", state="hibernated", iterm_guid="GONE-0002", last_event_at=self.boot - 100)
        rc, text = self.run_restore(include_hibernated=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.guid_calls), 1)
        self.assertIn("skip h1: hibernated (tab still open", text)
        self.assertIn("typed h2", text)
        self.assertNotIn("warning", text)

    def test_a_unlistable_guids_warn_and_keep_behaviour(self):
        self.guids(None)
        self.insert(session_id="h1", state="hibernated", iterm_guid="LIVE-0001", last_event_at=self.boot - 100)
        _, text = self.run_restore(include_hibernated=True)
        self.assertIn("warning: iTerm2 tabs could not be listed", text)
        self.assertIn("typed h1", text)

    def test_a_plain_restore_does_not_query_guids(self):
        self.guids({"X"})
        self.run_restore()
        self.assertEqual(self.guid_calls, [])

    def test_b_rebuilt_worktree_is_annotated(self):
        self.guids(set())
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        wt = repo + self.cfg["worktree_marker"] + "parked"
        self.insert(session_id="w1", state="hibernated", cwd=wt, last_event_at=self.boot - 100)
        self.insert(session_id="p1", state="hibernated", cwd=self.tmp, last_event_at=self.boot - 100)
        _, text = self.run_restore(include_hibernated=True)
        line = [l for l in text.splitlines() if l.startswith("  typed w1")][0]
        self.assertIn("(worktree will be rebuilt)", line)
        other = [l for l in text.splitlines() if l.startswith("  typed p1")][0]
        self.assertNotIn("rebuilt", other)

    def test_c_cli_flag_reaches_restore(self):
        seen = {}

        def fake_run(cfg, conn, **kw):
            seen.update(kw)
            return 0
        Patch(self, restore, "run", fake_run)
        self.assertEqual(cli.main(["restore", "--dry-run", "--max-age-days", "0"]), 0)
        self.assertEqual(seen["max_age_days"], 0)
        cli.main(["restore", "--dry-run"])
        self.assertIsNone(seen["max_age_days"])

    def test_c_old_row_restored_with_flag(self):
        self.guids(set())
        self.insert(session_id="o1", last_event_at=time.time() - 40 * 86400)
        _, text = self.run_restore()
        self.assertIn("skip o1: older than 7d", text)
        _, text = self.run_restore(max_age_days=0)
        self.assertIn("open  o1", text)


# ------------------------------------------------------------------ 3. test log path

class Item3LogPath(TempEnv):
    def test_suite_never_points_at_the_real_log(self):
        self.assertTrue(os.environ.get("CC_SESSIONS_LOG"))
        self.assertNotEqual(os.path.realpath(os.environ["CC_SESSIONS_LOG"]), os.path.realpath(REAL_LOG))
        self.assertNotEqual(os.path.realpath(self.cfg.path("log")), os.path.realpath(REAL_LOG))
        self.assertNotEqual(os.path.realpath(config.log_path()), os.path.realpath(REAL_LOG))

    def test_env_wins_over_a_config_naming_the_real_log(self):
        p = os.path.join(self.tmp, "real.json")
        with open(p, "w") as fh:
            json.dump({"log": "~/Library/Logs/cc-sessions.log"}, fh)
        self.assertEqual(config.load(p).path("log"), os.environ["CC_SESSIONS_LOG"])

    def test_hook_subprocess_without_a_config_logs_to_the_env_path(self):
        log = os.path.join(self.tmp, "sub.log")
        env = dict(os.environ, CC_SESSIONS_CONFIG=os.path.join(self.tmp, "absent.json"),
                   CC_SESSIONS_DB=self.db, CC_SESSIONS_LOG=log)
        r = subprocess.run([sys.executable, os.path.join(REPO, "bin", "cc-sessions"), "hook", "Stop"],
                           input=b"", capture_output=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0)
        with open(log) as fh:
            self.assertIn("[hook] Stop ? failed: JSONDecodeError", fh.read())

    def test_log_line_without_cfg_uses_env_path(self):
        cli.log_line(None, "probe-line")
        with open(os.environ["CC_SESSIONS_LOG"]) as fh:
            self.assertIn("probe-line", fh.read())

    def test_hook_failure_in_process_logs_to_temp(self):
        cli.hook_main("Stop", io.BytesIO(b""), lambda: None)
        with open(self.cfg.path("log")) as fh:
            self.assertIn("Stop ? failed", fh.read())


# ------------------------------------------------------------------ 4. doctor hook count

class Item4DoctorCount(TempEnv):
    def hooks_for(self, events):
        with open(self.cfg["claude_settings"], "w") as fh:
            json.dump({"hooks": {e: [{"hooks": [{"type": "command", "command": "cc-sessions hook %s" % e}]}]
                                 for e in events}}, fh)
        return [c for c in cli.doctor_checks(self.cfg) if c[1] == "hooks"]

    def test_all_expected_events_counted(self):
        expected = cli.REQUIRED_HOOK_EVENTS + cli.OPTIONAL_HOOK_EVENTS
        hooks = self.hooks_for(expected)
        self.assertEqual(hooks, [("PASS", "hooks", "all %d events wired" % len(expected))])
        self.assertEqual(len(expected), 8)

    def test_optional_missing_is_reported_as_a_fraction(self):
        hooks = self.hooks_for(cli.REQUIRED_HOOK_EVENTS)
        n = len(cli.REQUIRED_HOOK_EVENTS + cli.OPTIONAL_HOOK_EVENTS)
        self.assertEqual(hooks[0], ("PASS", "hooks", "%d of %d events wired (all required)" % (n - 1, n)))
        self.assertEqual(hooks[1][0], "WARN")


# ------------------------------------------------------------------ 5 + 7. pre-connect loop

class PreConnect(TempEnv):
    def setUp(self):
        super().setUp()
        for k in ("ITERM2_COOKIE", "ITERM2_KEY"):
            self.addCleanup(self._restore_var, k, os.environ.get(k))
            os.environ.pop(k, None)
        self.sleeps = []
        Patch(self, D, "setup_logging", lambda cfg, stderr=False: None)
        Patch(self, D, "trim_stdio", lambda *a, **k: 0)
        Patch(self, D, "take_lock", lambda cfg: object())
        Patch(self, D.time, "sleep", self.on_sleep)

    def _restore_var(self, k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def on_sleep(self, s):
        self.sleeps.append((s, ledger.get_meta(self.conn, "last_error"),
                            ledger.get_meta(self.conn, "liveness")))

    def main(self):
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(lambda fn, retry=False: None)):
            return D.main([])


class Item5ApiDisabled(PreConnect):
    def test_api_not_enabled_waits_ten_minutes(self):
        Patch(self, D, "iterm_running", lambda: True)
        replies = iter([RuntimeError("iTerm2 refused a cookie: The Python API is NOT ENABLED"), None, None])

        def cookie():
            exc = next(replies)
            if exc:
                raise exc
            os.environ["ITERM2_COOKIE"], os.environ["ITERM2_KEY"] = "c", "k"
        Patch(self, D, "request_cookie", cookie)
        self.main()
        self.assertEqual([s for s, _, _ in self.sleeps], [D.API_DISABLED_CHUNK_S] * 10)
        self.assertEqual(sum(s for s, _, _ in self.sleeps), D.API_DISABLED_RETRY_S)
        self.assertEqual(D.API_DISABLED_RETRY_S, 600)
        self.assertIn("Python API not enabled", self.sleeps[0][1])

    def test_other_cookie_failures_keep_the_ladder(self):
        Patch(self, D, "iterm_running", lambda: True)
        replies = iter([RuntimeError("iTerm2 refused a cookie: empty reply")] * 2 + [None])

        def cookie():
            exc = next(replies)
            if exc:
                raise exc
        Patch(self, D, "request_cookie", cookie)
        self.main()
        self.assertEqual([s for s, _, _ in self.sleeps], [5, 10])
        self.assertTrue(self.sleeps[0][1].startswith("waiting for iTerm2"))

    def test_api_disabled_matcher(self):
        self.assertTrue(D.api_disabled(RuntimeError("Python API not Enabled")))
        self.assertFalse(D.api_disabled(RuntimeError("iTerm2 is not running")))


class Item7Liveness(PreConnect):
    def test_liveness_advances_between_pre_connect_sleeps(self):
        up = iter([False, False, True])
        Patch(self, D, "iterm_running", lambda: next(up))
        Patch(self, D, "request_cookie", lambda: None)
        clock = [time.time()]

        def tick():
            clock[0] += 1.0
            return clock[0]
        Patch(self, D.time, "time", tick)
        self.main()
        self.assertEqual([s for s, _, _ in self.sleeps], [5, 10])
        first, second = float(self.sleeps[0][2]), float(self.sleeps[1][2])
        self.assertGreater(second, first)


# ------------------------------------------------------------------ 6. snapshot blindness

class Session(object):
    def __init__(self, sid, tty, fail=False):
        self.session_id, self.tty, self.fail = sid, tty, fail

    async def async_get_variable(self, name):
        if self.fail:
            raise RPCException("SESSION_NOT_FOUND")
        return self.tty


def app_of(sessions):
    tab = types.SimpleNamespace(tab_id="T0", sessions=sessions)
    w = types.SimpleNamespace(tabs=[tab], current_tab=tab)
    return types.SimpleNamespace(terminal_windows=[w])


class FakeConn(object):
    websocket = types.SimpleNamespace(closed=False)


class BlindBase(TempEnv):
    def setUp(self):
        super().setUp()
        Patch(self, D, "_last_skipped", frozenset())
        self.logged = []
        fake_log = types.SimpleNamespace(info=lambda *a: self.logged.append(a[0] % a[1:]),
                                         exception=lambda *a: None, warning=lambda *a: None)
        Patch(self, D, "log", fake_log)
        self.mods = unittest.mock.patch.dict(sys.modules, fake_iterm2())
        self.mods.start()
        self.addCleanup(self.mods.stop)

    def daemon(self, sessions):
        return D.Daemon(self.cfg, self.conn, app_of(sessions), FakeConn(), dry_run=True)

    def meta(self, k):
        return ledger.get_meta(self.conn, k)


class Item6Blindness(BlindBase):
    def test_snapshot_counts_panes_and_skips(self):
        f = arun(D.focus_snapshot(app_of([Session("G1", "ttys001"), Session("G2", "x", fail=True)])))
        self.assertEqual((f["panes"], f["skipped"]), (2, 1))

    def test_skip_is_logged_only_when_the_set_changes(self):
        app = app_of([Session("G1", "ttys001"), Session("G2", "x", fail=True)])
        for _ in range(3):
            arun(D.focus_snapshot(app))
        skips = [l for l in self.logged if "skipping" in l]
        self.assertEqual(len(skips), 1)
        self.assertIn("G2 (SESSION_NOT_FOUND)", skips[0])
        arun(D.focus_snapshot(app_of([Session("G1", "ttys001"), Session("G3", "x", fail=True)])))
        self.assertEqual(len([l for l in self.logged if "skipping" in l]), 2)

    def test_all_skipped_sets_blind_since_once_and_clears_on_recovery(self):
        d = self.daemon([Session("G1", "x", fail=True), Session("G2", "y", fail=True)])
        arun(d.snapshot())
        self.assertEqual(self.meta("skipped_sessions"), "2")
        since = self.meta("blind_since")
        self.assertTrue(float(since) > 0)
        arun(d.snapshot())
        self.assertEqual(self.meta("blind_since"), since)
        d.app = app_of([Session("G1", "ttys001"), Session("G2", "y", fail=True)])
        arun(d.snapshot())
        self.assertEqual((self.meta("skipped_sessions"), self.meta("blind_since")), ("1", ""))

    def test_no_panes_is_not_blind(self):
        d = self.daemon([])
        arun(d.snapshot())
        self.assertEqual((self.meta("skipped_sessions"), self.meta("blind_since")), ("0", ""))

    def test_doctor_warns_on_a_long_blind_spell_only(self):
        now = 2_000_000.0
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "heartbeat", now - 5)
            ledger.set_meta(self.conn, "skipped_sessions", 4)
            ledger.set_meta(self.conn, "blind_since", now - 600)
        checks = cli.daemon_checks(self.conn, now=now, alive=lambda pid: True)
        warn = [c for c in checks if c[1] == "iTerm2 snapshot"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0][0], "WARN")
        self.assertIn("10 min (4 skipped)", warn[0][2])
        for since in (now - 60, ""):
            with ledger.tx(self.conn):
                ledger.set_meta(self.conn, "blind_since", since)
            checks = cli.daemon_checks(self.conn, now=now, alive=lambda pid: True)
            self.assertEqual([c for c in checks if c[1] == "iTerm2 snapshot"], [])


# ------------------------------------------------------------------ round 4b review fixes

class R4bWorktreeRebuildScope(SelectBase):
    """A missing worktree is kept only for an interactive --include-hibernated typed row."""

    def test_auto_crashed_row_with_missing_worktree_is_skipped(self):
        rows = self.rows({"cwd": self.worktree()})
        run, typed, skipped = self.sel(rows, auto=True, live_guids=set())
        self.assertEqual((run, typed), ([], []))
        self.assertTrue(skipped["s0"].startswith("cwd gone"))

    def test_auto_include_hibernated_typed_row_with_missing_worktree_is_skipped(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "GONE-0001", "cwd": self.worktree()})
        run, typed, skipped = self.sel(rows, auto=True, include_hibernated=True, live_guids=set())
        self.assertEqual((run, typed), ([], []))
        self.assertTrue(skipped["s0"].startswith("cwd gone"))

    def test_auto_typed_row_tab_gone_with_missing_worktree_is_skipped(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "GONE-0001", "cwd": self.worktree()})
        _, typed, skipped = self.sel(rows, auto=True, live_guids={"OTHER-001"})
        self.assertEqual(typed, [])
        self.assertTrue(skipped["s0"].startswith("cwd gone"))

    def test_non_auto_include_hibernated_typed_row_is_kept(self):
        rows = self.rows({"state": "hibernated", "cwd": self.worktree()})
        _, typed, skipped = self.sel(rows, include_hibernated=True, live_guids=set())
        self.assertEqual((typed, skipped), (["s0"], {}))

    def test_non_git_repo_is_not_rebuildable(self):
        wt = self.worktree(git=False)
        self.assertFalse(restore.rebuildable_worktree(self.cfg, wt))
        rows = self.rows({"state": "hibernated", "cwd": wt})
        _, typed, skipped = self.sel(rows, include_hibernated=True)
        self.assertEqual(typed, [])
        self.assertTrue(skipped["s0"].startswith("cwd gone"))
        os.makedirs(os.path.join(self.tmp, "repo", ".git"))
        self.assertTrue(restore.rebuildable_worktree(self.cfg, wt))


class R4bRunAnnotation(RunBase):
    def make_worktree(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, ".git"))
        return repo + self.cfg["worktree_marker"] + "x"

    def test_non_auto_include_hibernated_typed_row_annotated(self):
        self.guids(set())
        self.insert(session_id="w1", state="hibernated", cwd=self.make_worktree(),
                    last_event_at=self.boot - 100)
        _, text = self.run_restore(include_hibernated=True)
        self.assertIn("(worktree will be rebuilt)", [l for l in text.splitlines() if l.startswith("  typed w1")][0])

    def test_auto_crashed_row_with_missing_worktree_never_opens(self):
        self.guids(set())
        self.insert(session_id="c1", cwd=self.make_worktree(), last_event_at=self.boot - 100)
        lines = []
        restore.run(self.cfg, self.conn, auto=True, dry_run=True, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("skip c1: cwd gone", text)
        self.assertNotIn("open  c1", text)


class R4bMaxAgeAuto(SelectBase):
    def test_max_age_s_semantics(self):
        day = 86400
        inf = float("inf")
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 0}, auto=True), 30 * day)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": -1}, auto=True), 30 * day)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 0}, auto=True, max_age_days=0), inf)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 0}), inf)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 7}, auto=True), 30 * day)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 45}, auto=True), 45 * day)
        self.assertEqual(restore.max_age_s({"restore_max_age_days": 7}, auto=True, max_age_days=3), 30 * day)

    def test_auto_config_zero_keeps_the_30_day_floor(self):
        self.cfg["restore_max_age_days"] = 0
        rows = self.rows({"last_event_at": BOOT - 40 * 86400})
        run, _, skipped = self.sel(rows, auto=True, live_guids=set())
        self.assertEqual(run, [])
        self.assertIn("older than 30d", skipped["s0"])

    def test_auto_explicit_flag_zero_lifts_the_limit(self):
        self.cfg["restore_max_age_days"] = 0
        rows = self.rows({"last_event_at": BOOT - 400 * 86400})
        self.assertEqual(self.sel(rows, auto=True, live_guids=set(), max_age_days=0)[0], ["s0"])


class R4bMaxAgeFlagType(RunBase):
    def test_flag_is_int(self):
        seen = {}

        def fake_run(cfg, conn, **kw):
            seen.update(kw)
            return 0
        Patch(self, restore, "run", fake_run)
        cli.main(["restore", "--dry-run", "--max-age-days", "5"])
        self.assertIs(type(seen["max_age_days"]), int)
        with unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["restore", "--dry-run", "--max-age-days", "1.5"])


class R4bApiDisabledLiveness(PreConnect):
    def test_liveness_advances_during_the_long_wait(self):
        Patch(self, D, "iterm_running", lambda: True)
        replies = iter([RuntimeError("Python API is NOT ENABLED"), None])

        def cookie():
            exc = next(replies)
            if exc:
                raise exc
        Patch(self, D, "request_cookie", cookie)
        clock = [time.time()]

        def tick():
            clock[0] += 1.0
            return clock[0]
        Patch(self, D.time, "time", tick)
        self.main()
        self.assertEqual(len(self.sleeps), 10)
        self.assertTrue(all(s <= 60 for s, _, _ in self.sleeps))
        lv = [float(l) for _, _, l in self.sleeps]
        self.assertTrue(all(b > a for a, b in zip(lv, lv[1:])), lv)

    def test_sleep_alive_writes_liveness_before_each_chunk(self):
        seen = []
        Patch(self, D, "record_meta", lambda conn, **kw: seen.append(("meta", kw)))
        Patch(self, D.time, "sleep", lambda s: seen.append(("sleep", s)))
        D.sleep_alive(self.conn, 150, 60)
        self.assertEqual([k for k, _ in seen], ["meta", "sleep"] * 3)
        self.assertEqual([v for k, v in seen if k == "sleep"], [60, 60, 30])
        self.assertTrue(all("liveness" in v for k, v in seen if k == "meta"))


class R4bBlindnessMeta(BlindBase):
    def test_record_start_clears_stale_blindness(self):
        now = 2_000_000.0
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "blind_since", now - 3600)
            ledger.set_meta(self.conn, "skipped_sessions", 4)
        D.record_start(self.conn, now=now)
        self.assertEqual((self.meta("blind_since"), self.meta("skipped_sessions")), ("", "0"))
        checks = cli.daemon_checks(self.conn, now=now, alive=lambda pid: True)
        self.assertEqual([c for c in checks if c[1] == "iTerm2 snapshot"], [])

    def test_record_blindness_writes_only_on_change(self):
        d = self.daemon([])
        writes = []
        real = D.ledger.set_meta

        def counting(conn, k, v):
            writes.append(k)
            return real(conn, k, v)
        Patch(self, D.ledger, "set_meta", counting)
        blind = {"panes": 2, "skipped": 2}
        d.record_blindness(blind, now=100.0)
        n = len(writes)
        self.assertGreater(n, 0)
        d.record_blindness(blind, now=200.0)
        self.assertEqual(len(writes), n)
        self.assertEqual(float(self.meta("blind_since")), 100.0)
        d.record_blindness({"panes": 2, "skipped": 1}, now=300.0)
        self.assertEqual(len(writes), 2 * n)
