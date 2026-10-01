import io
import json
import os
import stat
import subprocess
import sys

from helpers import REPO, TempEnv

from ccsessions import cli, ledger

BIN = os.path.join(REPO, "bin", "cc-sessions")
PROC = {"pid": 4242, "pid_start": "Mon Sep 28 10:00:00 2026", "tty": "ttys004", "interactive": True}


def payload(**kw):
    d = {"session_id": "abc-1", "cwd": "/w", "transcript_path": "/w/t.jsonl"}
    d.update(kw)
    return io.BytesIO(json.dumps(d).encode())


class HookCli(TempEnv):
    def run_bin(self, *args, stdin=b""):
        env = dict(os.environ, CC_SESSIONS_CONFIG=self.cfg_path, CC_SESSIONS_DB=self.db)
        return subprocess.run([sys.executable, BIN] + list(args), input=stdin,
                              capture_output=True, env=env, timeout=30)

    def test_hook_applies_event(self):
        self.assertEqual(cli.hook_main("UserPromptSubmit", payload(), lambda: dict(PROC)), 0)
        self.assertEqual(ledger.state_of(self.conn, "abc-1"), "busy")

    def test_hook_garbage_stdin_exits_zero_silently(self):
        for junk in (b"", b"not json", b"[1,2]", b'{"no_sid": 1}', b"\xff\xfe"):
            r = self.run_bin("hook", "Stop", stdin=junk)
            self.assertEqual(r.returncode, 0, junk)
            self.assertEqual(r.stdout, b"", junk)
            self.assertEqual(r.stderr, b"", junk)

    def test_hook_no_args_exits_zero(self):
        r = self.run_bin("hook")
        self.assertEqual(r.returncode, 0)

    def test_R3_unwritable_db_writes_marker_then_success_clears_it(self):
        self.conn.close()
        bad = os.path.join(self.tmp, "ro")
        os.makedirs(bad)
        os.chmod(bad, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, bad, stat.S_IRWXU)
        os.environ["CC_SESSIONS_DB"] = os.path.join(bad, "sub", "x.db")
        from ccsessions import config
        cfg = config.load()
        self.assertEqual(cli.hook_main("Stop", payload(), lambda: dict(PROC)), 0)
        # db dir unwritable: the marker (also under the db dir) cannot be written either,
        # but the hook still returns 0
        os.environ["CC_SESSIONS_DB"] = self.db
        # force a failure with a writable db dir: make the db path a directory
        os.makedirs(os.path.join(self.tmp, "state2", "cc.db"))
        os.environ["CC_SESSIONS_DB"] = os.path.join(self.tmp, "state2", "cc.db")
        self.assertEqual(cli.hook_main("Stop", payload(), lambda: dict(PROC)), 0)
        marker = os.path.join(self.tmp, "state2", "hook-failed", "abc-1")
        self.assertTrue(os.path.exists(marker))
        with open(os.path.join(self.tmp, "cc-sessions.log")) as fh:
            self.assertIn("Stop abc-1 failed", fh.read())
        # repoint to a good db in the same dir: success removes the marker
        os.environ["CC_SESSIONS_DB"] = os.path.join(self.tmp, "state2", "good.db")
        self.assertEqual(cli.hook_main("Stop", payload(), lambda: dict(PROC)), 0)
        self.assertFalse(os.path.exists(marker))
        del cfg

    def test_marker_rejects_unsafe_sid(self):
        self.assertIsNone(cli.marker_path(self.cfg, "../../etc"))
        self.assertIsNotNone(cli.marker_path(self.cfg, "0f3a-9b"))

    def test_state_command(self):
        cli.hook_main("Stop", payload(), lambda: dict(PROC))
        r = self.run_bin("state", "abc-1")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, b"idle"))
        r = self.run_bin("state", "nope")
        self.assertEqual((r.returncode, r.stdout.strip()), (1, b"unknown"))

    def test_pending_command_unreadable(self):
        r = self.run_bin("pending", os.path.join(self.tmp, "missing.jsonl"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {"pending": ["unreadable"], "last_text": ""})

    def test_hibernate_and_wake_queue_requests(self):
        cli.hook_main("Stop", payload(), lambda: dict(PROC))
        r = self.run_bin("hibernate", "abc")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.run_bin("wake", "--all")
        self.assertEqual(r.returncode, 0)
        kinds = [(k, s) for k, s in self.conn.execute("SELECT kind, session_id FROM requests ORDER BY id")]
        self.assertEqual(kinds, [("hibernate", "abc-1"), ("wake-all", None)])
        self.assertEqual(self.run_bin("hibernate").returncode, 2)

    def test_status_json(self):
        cli.hook_main("UserPromptSubmit", payload(), lambda: dict(PROC))
        r = self.run_bin("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual([s["state"] for s in d["sessions"]], ["busy"])
        self.assertIn("free_pct", d["memory"])

    def test_doctor_reports_missing_hooks(self):
        with open(self.cfg["claude_settings"], "w") as fh:
            json.dump({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "cc-sessions hook Stop"}]}]}}, fh)
        checks = cli.doctor_checks(self.cfg)
        hooks = [c for c in checks if c[1] == "hooks"]
        self.assertEqual(hooks[0][0], "FAIL")
        self.assertIn("SessionStart", hooks[0][2])
        shapes = [c for c in checks if c[1] == "transcript shapes"][0]
        self.assertEqual(shapes[0], "PASS", shapes)

    def test_reap_removes_dead_sockets_only(self):
        sd = self.cfg.path("sock_dir")
        os.makedirs(sd)
        for n in ("111.sock", "222.sock", "notapid.sock"):
            open(os.path.join(sd, n), "w").close()
        alive = lambda pid: pid == 222
        removed, orphans = cli.reap(self.cfg, cmd_table={9: (1, 0, "node x"), 10: (5, 0, "y")},
                                    cwds={9: os.path.join(self.tmp, "gone"), 10: "/"}, alive=alive)
        self.assertEqual(removed, ["111.sock"])
        self.assertEqual(sorted(os.listdir(sd)), ["222.sock", "notapid.sock"])
        self.assertEqual([o[0] for o in orphans], [9])
        removed, _ = cli.reap(self.cfg, dry_run=True, cmd_table={}, cwds={}, alive=lambda p: False)
        self.assertEqual(removed, ["222.sock"])
        self.assertTrue(os.path.exists(os.path.join(sd, "222.sock")))
