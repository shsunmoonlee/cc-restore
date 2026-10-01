"""Fix round 3 (2026-09-28 incident: a dead iTerm2 connection retried in-process forever):
one or more tests per fix."""
import asyncio
import json
import os
import sys
import time
import types
import unittest.mock

from helpers import REPO, TempEnv, load_daemon

from ccsessions import cli, ledger

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


class FakeWS(object):
    closed = False


class FakeConn(object):
    def __init__(self):
        self.websocket = FakeWS()


class FakeApp(object):
    terminal_windows = []

    def __init__(self, connection):
        self.connection = connection


class RPCException(Exception):
    pass


def fake_iterm2(run_until_complete, get_app=None):
    """Just enough of the iterm2 package for main() and focus_snapshot()."""
    mod = types.ModuleType("iterm2")
    rpc = types.ModuleType("iterm2.rpc")
    rpc.RPCException = RPCException
    mod.rpc = rpc
    mod.run_until_complete = run_until_complete

    async def default_get_app(connection):
        return FakeApp(connection)
    mod.async_get_app = get_app or default_get_app
    return {"iterm2": mod, "iterm2.rpc": rpc}


def library_run_until_complete(calls):
    """Like iterm2.connection.Connection.run_until_complete: a coroutine exception is
    printed and turned into sys.exit(1)."""
    def run(coro_fn, retry=False):
        calls.append(retry)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro_fn(FakeConn()))
        except Exception:
            sys.exit(1)
        finally:
            loop.close()
    return run


# ------------------------------------------------------------------ 1. main exits on connection loss

class Fix1Main(TempEnv):
    def setUp(self):
        super().setUp()
        for k in ("ITERM2_COOKIE", "ITERM2_KEY"):
            self.addCleanup(self._restore_var, k, os.environ.get(k))
        self.sleeps, self.cookies = [], []
        Patch(self, D, "setup_logging", lambda cfg, stderr=False: None)
        Patch(self, D, "trim_stdio", lambda *a, **k: 0)
        Patch(self, D, "take_lock", lambda cfg: object())
        Patch(self, D, "request_cookie", lambda: self.cookies.append(1))
        Patch(self, D.time, "sleep", self.sleeps.append)

    def _restore_var(self, k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def meta(self, key):
        return ledger.get_meta(self.conn, key)

    def test_connection_loss_inside_run_exits_nonzero_and_records_why(self):
        Patch(self, D, "iterm_running", lambda: True)

        async def loops(daemon, loops=None):
            raise D.ConnectionLost("iTerm2 did not answer a focus snapshot in 15s")
        Patch(self, D.Daemon, "run_loops", loops)
        calls = []
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(library_run_until_complete(calls))):
            rc = D.main([])
        self.assertNotEqual(rc, 0)
        self.assertEqual(calls, [False])  # entered once, never retried in-process
        self.assertEqual(self.sleeps, [])
        err = self.meta("last_error")
        self.assertIn("library exit 1", err)
        self.assertIn("ConnectionLost: iTerm2 did not answer a focus snapshot", err)
        self.assertIsNotNone(self.meta("last_exit_at"))
        self.assertEqual(self.meta("starts"), "1")
        self.assertIsNotNone(self.meta("liveness"))
        self.assertIsNone(self.meta("heartbeat"))

    def test_normal_return_from_run_also_exits_nonzero(self):
        Patch(self, D, "iterm_running", lambda: True)
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(lambda fn, retry=False: None)):
            rc = D.main([])
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.meta("last_error"), "iTerm2 connection ended")

    def test_plain_exception_from_run_exits_nonzero_and_records_it(self):
        Patch(self, D, "iterm_running", lambda: True)

        def run(fn, retry=False):
            raise RuntimeError("401 Unauthorized")
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(run)):
            rc = D.main([])
        self.assertEqual(rc, 1)
        self.assertEqual(self.meta("last_error"), "iTerm2 connection lost: RuntimeError: 401 Unauthorized")
        self.assertIsNotNone(self.meta("last_exit_at"))

    def test_env_cookie_is_used_first_then_replaced_after_a_failure(self):
        os.environ["ITERM2_COOKIE"], os.environ["ITERM2_KEY"] = "env-cookie", "env-key"
        up = iter([False, True])
        Patch(self, D, "iterm_running", lambda: next(up))
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(lambda fn, retry=False: None)):
            D.main([])
        self.assertEqual(self.sleeps, [5])
        self.assertEqual(len(self.cookies), 1)  # the env cookie is not trusted after a failure
        self.assertNotIn("ITERM2_COOKIE", os.environ)

    def test_env_cookie_is_used_when_the_first_attempt_succeeds(self):
        os.environ["ITERM2_COOKIE"], os.environ["ITERM2_KEY"] = "env-cookie", "env-key"
        Patch(self, D, "iterm_running", lambda: True)
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(lambda fn, retry=False: None)):
            D.main([])
        self.assertEqual(self.cookies, [])
        self.assertEqual(os.environ["ITERM2_COOKIE"], "env-cookie")

    def test_pre_connect_failures_wait_in_process_then_connect_once(self):
        up = iter([False, False, True])
        Patch(self, D, "iterm_running", lambda: next(up))
        calls = []

        def run(fn, retry=False):
            calls.append(1)
            raise SystemExit(1)
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(run)):
            rc = D.main([])
        self.assertEqual(self.sleeps, [5, 10])
        self.assertEqual(calls, [1])
        self.assertEqual(len(self.cookies), 1)
        self.assertNotEqual(rc, 0)
        self.assertIn("library exit 1", self.meta("last_error"))

    def test_cookie_refusal_waits_in_process(self):
        Patch(self, D, "iterm_running", lambda: True)
        tries = []

        def cookie():
            tries.append(1)
            if len(tries) == 1:
                raise RuntimeError("iTerm2 refused a cookie: no")
        Patch(self, D, "request_cookie", cookie)
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(lambda fn, retry=False: None)):
            D.main([])
        self.assertEqual(len(tries), 2)
        self.assertEqual(self.sleeps, [5])

    def test_starts_counter_and_recent_starts(self):
        D.record_start(self.conn, now=100.0)
        D.record_start(self.conn, now=200.0)
        self.assertEqual(self.meta("starts"), "2")
        self.assertEqual(json.loads(self.meta("recent_starts")), [100.0, 200.0])
        self.assertEqual(self.meta("pid"), str(os.getpid()))
        for i in range(D.RECENT_STARTS + 5):
            D.record_start(self.conn, now=300.0 + i)
        self.assertEqual(len(json.loads(self.meta("recent_starts"))), D.RECENT_STARTS)


# ------------------------------------------------------------------ 2. stale App singleton

class Fix2StaleApp(TempEnv):
    def test_app_bound_to_another_connection_is_connection_lost(self):
        old, new = FakeConn(), FakeConn()

        async def get_app(connection):
            return FakeApp(old)  # what App.instance looks like after a dead connection
        failure = {}
        with self.assertRaises(D.ConnectionLost) as cm:
            arun(D.run_connected(new, self.cfg, self.conn, True, get_app, failure))
        self.assertIn("stale App singleton", str(cm.exception))
        self.assertIn("stale App singleton", failure["error"])

    def test_app_on_this_connection_runs_the_loops(self):
        ran = []

        async def loops(daemon, loops=None):
            ran.append(daemon.connection)
        Patch(self, D.Daemon, "run_loops", loops)
        conn = FakeConn()

        async def get_app(connection):
            return FakeApp(connection)
        arun(D.run_connected(conn, self.cfg, self.conn, True, get_app))
        self.assertEqual(ran, [conn])

    def test_unanswered_app_setup_is_connection_lost(self):
        Patch(self, D, "APP_CONNECT_TIMEOUT_S", 0.01)

        async def get_app(connection):
            await asyncio.sleep(5)  # websocket accepted, RPCs never answered
        failure = {}
        with self.assertRaises(D.ConnectionLost) as cm:
            arun(D.run_connected(FakeConn(), self.cfg, self.conn, True, get_app, failure))
        self.assertIn("App setup", str(cm.exception))
        self.assertIn("ConnectionLost", failure["error"])

    def test_good_connect_records_connected_at_and_clears_last_error(self):
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "last_error", "days-old error")
        seen = {}

        async def loops(daemon, loops=None):
            seen["error"] = ledger.get_meta(self.conn, "last_error")
            seen["connected_at"] = ledger.get_meta(self.conn, "connected_at")
        Patch(self, D.Daemon, "run_loops", loops)

        async def get_app(connection):
            return FakeApp(connection)
        arun(D.run_connected(FakeConn(), self.cfg, self.conn, True, get_app))
        self.assertEqual(seen["error"], "")
        self.assertIsNotNone(seen["connected_at"])

    def test_stale_app_does_not_clear_last_error(self):
        with ledger.tx(self.conn):
            ledger.set_meta(self.conn, "last_error", "earlier error")

        async def get_app(connection):
            return FakeApp(FakeConn())
        with self.assertRaises(D.ConnectionLost):
            arun(D.run_connected(FakeConn(), self.cfg, self.conn, True, get_app))
        self.assertEqual(ledger.get_meta(self.conn, "last_error"), "earlier error")
        self.assertIsNone(ledger.get_meta(self.conn, "connected_at"))


# ------------------------------------------------------------------ 3. request_cookie

class Fix3Cookie(TempEnv):
    def setUp(self):
        super().setUp()
        for k in ("ITERM2_COOKIE", "ITERM2_KEY"):
            self.addCleanup(self._restore_var, k, os.environ.get(k))

    def _restore_var(self, k, v):
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def fake_run(self, stdout, rc=0, stderr=""):
        def run(args, **kw):
            return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)
        Patch(self, D.subprocess, "run", run)

    def test_cookie_and_key_land_in_the_right_variables(self):
        self.fake_run("the-cookie-123 the-key-456\n")
        D.request_cookie()
        self.assertEqual(os.environ["ITERM2_COOKIE"], "the-cookie-123")
        self.assertEqual(os.environ["ITERM2_KEY"], "the-key-456")

    def test_refusal_raises(self):
        self.fake_run("", rc=1, stderr="not allowed")
        with self.assertRaises(RuntimeError):
            D.request_cookie()


# ------------------------------------------------------------------ 4. focus_snapshot

class Session(object):
    def __init__(self, sid, tty, fail=False, barrier=None):
        self.session_id, self.tty, self.fail, self.barrier = sid, tty, fail, barrier

    async def async_get_variable(self, name):
        if self.barrier is not None:
            await self.barrier.arrive()
        if self.fail:
            raise RPCException("SESSION_NOT_FOUND")
        return self.tty


class Barrier(object):
    """Every session must be asked before any answers: sequential reads never finish."""

    def __init__(self, n):
        self.n, self.seen, self.ev = n, 0, None

    async def arrive(self):
        if self.ev is None:
            self.ev = asyncio.Event()
        self.seen += 1
        if self.seen >= self.n:
            self.ev.set()
        await self.ev.wait()


def app_of(sessions_by_tab, current=0):
    tabs = [types.SimpleNamespace(tab_id="T%d" % i, sessions=ss) for i, ss in enumerate(sessions_by_tab)]
    w = types.SimpleNamespace(tabs=tabs, current_tab=tabs[current])
    return types.SimpleNamespace(terminal_windows=[w])


class Fix4Snapshot(TempEnv):
    def test_session_that_raises_rpc_exception_is_skipped(self):
        app = app_of([[Session("G1", "/dev/ttys001"), Session("G2", "/dev/ttys002", fail=True)],
                      [Session("G3", "ttys003")]])
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(None)):
            f = arun(D.focus_snapshot(app))
        self.assertEqual(f["tty_guid"], {"ttys001": "G1", "ttys003": "G3"})
        self.assertEqual(f["visible_ttys"], {"ttys001"})
        self.assertEqual(f["visible_guids"], {"G1", "G2"})

    def test_tty_reads_run_concurrently(self):
        b = Barrier(3)
        app = app_of([[Session("G1", "ttys001", barrier=b), Session("G2", "ttys002", barrier=b)],
                      [Session("G3", "ttys003", barrier=b)]])

        async def go():
            return await asyncio.wait_for(D.focus_snapshot(app), 2)
        f = arun(go())
        self.assertEqual(len(f["tty_guid"]), 3)

    def test_other_errors_still_propagate(self):
        class Boom(object):
            session_id = "G9"

            async def async_get_variable(self, name):
                raise ValueError("boom")
        with self.assertRaises(ValueError):
            arun(D.focus_snapshot(app_of([[Boom()]])))

    def test_failure_waits_for_siblings_and_raises_the_first(self):
        done = []

        class Fails(object):
            def __init__(self, sid, delay, exc):
                self.session_id, self.delay, self.exc = sid, delay, exc

            async def async_get_variable(self, name):
                await asyncio.sleep(self.delay)
                done.append(self.session_id)
                raise self.exc
        app = app_of([[Fails("G1", 0, ValueError("first")),
                       Fails("G2", 0.02, KeyError("second")),
                       Session("G3", "ttys003", fail=True)]])
        with unittest.mock.patch.dict(sys.modules, fake_iterm2(None)):
            with self.assertRaises(ValueError) as cm:
                arun(D.focus_snapshot(app))
        self.assertEqual(str(cm.exception), "first")
        self.assertEqual(done, ["G1", "G2"])  # the slower sibling finished, not abandoned


# ------------------------------------------------------------------ 5. snapshot timeout

class SlowSession(object):
    session_id = "G"

    async def async_get_variable(self, name):
        await asyncio.sleep(5)


class Fix5Timeout(TempEnv):
    def daemon(self, connection):
        d = D.Daemon(self.cfg, self.conn, app_of([[SlowSession()]]), connection, dry_run=True)
        d.SNAPSHOT_TIMEOUT_S = 0.01
        return d

    def test_slow_snapshot_on_live_connection_is_a_plain_timeout(self):
        with self.assertRaises(TimeoutError) as cm:
            arun(self.daemon(FakeConn()).snapshot())
        self.assertNotIsInstance(cm.exception, D.ConnectionLost)

    def test_slow_snapshot_on_closed_connection_is_connection_lost(self):
        c = FakeConn()
        c.websocket.closed = True
        with self.assertRaises(D.ConnectionLost):
            arun(self.daemon(c).snapshot())

    def test_tick_loop_tolerates_fewer_than_three_snapshot_timeouts(self):
        d = self.daemon(FakeConn())
        self.cfg["tick_s"] = 0
        n = []

        async def tick():
            n.append(1)
            if len(n) < D.Daemon.MAX_TICK_FAILURES:
                raise TimeoutError("slow snapshot")
            raise D.ConnectionLost("stop the test")
        d.tick = tick
        with self.assertRaises(D.ConnectionLost) as cm:
            arun(d.tick_loop())
        self.assertEqual(str(cm.exception), "stop the test")

    def test_recheck_connection_loss_propagates_and_rolls_back(self):
        row = self.insert(session_id="s1", state="idle", iterm_guid="G1", pid=111, pid_start=1000)
        kills = []

        async def recheck(r):
            raise D.ConnectionLost("websocket closed")
        ev = D.Evictor(self.conn, self.cfg, None, kill=lambda p, s: kills.append(s),
                       identity=lambda r: "alive", children=lambda r: [],
                       child_probe=lambda p, s: "gone")
        with self.assertRaises(D.ConnectionLost):
            arun(ev.evict(row, 10, 1, recheck=recheck))
        self.assertEqual(kills, [])
        r = ledger.get(self.conn, "s1")
        self.assertEqual((r["state"], r["evicted_at"]), ("idle", None))
        out = self.conn.execute("SELECT outcome FROM evictions WHERE session_id='s1'").fetchone()[0]
        self.assertEqual(out, "aborted")


# ------------------------------------------------------------------ 6. doctor

class Fix6Doctor(TempEnv):
    NOW = 2_000_000_000.0

    def set(self, **kv):
        with ledger.tx(self.conn):
            for k, v in kv.items():
                ledger.set_meta(self.conn, k, v)

    def test_alive_but_disconnected_is_a_fail_with_pid_age_and_error(self):
        n = self.NOW
        self.set(heartbeat=n - 3600, liveness=n - 10, pid=4242, last_exit_at=n - 3000,
                 last_error="iTerm2 connection lost (library exit 1)")
        checks = cli.daemon_checks(self.conn, now=n, alive=lambda pid: pid == 4242)
        hb = [c for c in checks if c[1] == "daemon heartbeat"][0]
        self.assertEqual(hb[0], "FAIL")
        self.assertIn("daemon pid 4242 alive", hb[2])
        self.assertIn("last tick 3600s ago", hb[2])
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n - 3000))
        self.assertIn("disconnected since %s" % since, hb[2])
        self.assertIn("library exit 1", hb[2])

    def test_fresh_heartbeat_passes(self):
        self.set(heartbeat=self.NOW - 5, liveness=self.NOW - 5)
        checks = cli.daemon_checks(self.conn, now=self.NOW, alive=lambda pid: True)
        self.assertEqual([c[0] for c in checks], ["PASS"])

    def test_dead_daemon_reports_stale_heartbeat_and_last_error(self):
        self.set(heartbeat=self.NOW - 900, liveness=self.NOW - 900, last_error="boom")
        hb = cli.daemon_checks(self.conn, now=self.NOW, alive=lambda pid: True)[0]
        self.assertEqual(hb[0], "FAIL")
        self.assertIn("900s ago", hb[2])
        self.assertIn("boom", hb[2])
        self.assertIn("daemon not running", hb[2])

    def test_connected_but_not_ticking_says_connected_not_old_error(self):
        n = self.NOW
        self.set(heartbeat=n - 3600, liveness=n - 10, pid=4242, last_exit_at=n - 3000,
                 connected_at=n - 20, last_error="")
        hb = cli.daemon_checks(self.conn, now=n, alive=lambda pid: True)[0]
        self.assertEqual(hb[0], "FAIL")
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(n - 20))
        self.assertIn("connected since %s" % since, hb[2])
        self.assertNotIn("disconnected", hb[2])
        self.assertIn("no error recorded", hb[2])

    def test_connect_older_than_last_exit_is_disconnected(self):
        n = self.NOW
        self.set(heartbeat=n - 3600, liveness=n - 10, pid=4242, connected_at=n - 4000,
                 last_exit_at=n - 3000, last_error="boom")
        hb = cli.daemon_checks(self.conn, now=n, alive=lambda pid: True)[0]
        self.assertIn("disconnected since", hb[2])
        self.assertIn("boom", hb[2])

    def test_stale_liveness_with_live_pid_is_alive_but_unresponsive(self):
        n = self.NOW
        self.set(heartbeat=n - 900, liveness=n - 600, pid=4242, last_error="boom")
        hb = cli.daemon_checks(self.conn, now=n, alive=lambda pid: pid == 4242)[0]
        self.assertEqual(hb[0], "FAIL")
        self.assertIn("daemon pid 4242 alive but unresponsive: no liveness for 600s", hb[2])
        self.assertIn("last tick 900s ago", hb[2])
        self.assertIn("boom", hb[2])

    def test_stale_liveness_with_dead_pid_is_not_running(self):
        n = self.NOW
        self.set(heartbeat=n - 900, liveness=n - 600, pid=4242, last_error="boom")
        hb = cli.daemon_checks(self.conn, now=n, alive=lambda pid: False)[0]
        self.assertEqual(hb[0], "FAIL")
        self.assertIn("daemon not running (pid 4242)", hb[2])
        self.assertNotIn("alive", hb[2])
        self.assertIn("boom", hb[2])

    def test_restart_loop_warns(self):
        n = self.NOW
        self.set(heartbeat=n - 5, recent_starts=json.dumps([n - 4000, n - 200, n - 120, n - 40]),
                 last_error="stale App singleton")
        checks = cli.daemon_checks(self.conn, now=n, alive=lambda pid: True)
        warn = [c for c in checks if c[1] == "daemon restarts"]
        self.assertEqual(len(warn), 1)
        self.assertEqual(warn[0][0], "WARN")
        self.assertIn("3 starts", warn[0][2])

    def test_spread_out_starts_do_not_warn(self):
        n = self.NOW
        self.set(heartbeat=n - 5, recent_starts=json.dumps([n - 4000, n - 2000, n - 40]))
        checks = cli.daemon_checks(self.conn, now=n, alive=lambda pid: True)
        self.assertEqual([c for c in checks if c[1] == "daemon restarts"], [])


# ------------------------------------------------------------------ 7. plist + out.log

class Fix7LaunchdAndOutLog(TempEnv):
    def test_plist_keeps_alive_and_puts_system_dirs_before_homebrew(self):
        with open(os.path.join(REPO, "launchd", "com.cc-sessions.daemon.plist.in")) as fh:
            text = fh.read()
        self.assertRegex(text, r"<key>KeepAlive</key>\s*<true/>")
        path = text.split("<key>PATH</key>")[1].split("<string>")[1].split("</string>")[0]
        parts = path.split(":")
        system = ["/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        self.assertEqual(parts[:4], system)
        for brew in ("/opt/homebrew/bin", "/usr/local/bin"):
            self.assertIn(brew, parts)
            self.assertGreater(parts.index(brew), max(parts.index(d) for d in system))

    def open_log(self, size):
        p = os.path.join(self.tmp, "out.log")
        with open(p, "wb") as fh:
            fh.write(b"x" * size)
        fd = os.open(p, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(os.close, fd)
        return p, fd

    def test_oversized_output_file_is_truncated_once(self):
        p, fd = self.open_log(4096)
        fd2 = os.dup(fd)  # stdout and stderr share the file, as in the plist
        self.addCleanup(os.close, fd2)
        self.assertEqual(D.trim_stdio(cap=1024, fds=(fd, fd2)), 4096)
        with open(p, "rb") as fh:
            data = fh.read()
        self.assertLess(len(data), 200)
        self.assertIn(b"truncated 4096 bytes", data)
        os.write(fd, b"after\n")
        with open(p, "rb") as fh:
            self.assertTrue(fh.read().endswith(b"after\n"))

    def test_small_file_and_pipes_are_left_alone(self):
        p, fd = self.open_log(100)
        self.assertEqual(D.trim_stdio(cap=1024, fds=(fd,)), 0)
        self.assertEqual(os.path.getsize(p), 100)
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        self.assertEqual(D.trim_stdio(cap=0, fds=(w, 9999)), 0)


if __name__ == "__main__":
    import unittest
    unittest.main()
