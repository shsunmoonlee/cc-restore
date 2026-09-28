import io
import json
import os
import time
from contextlib import redirect_stderr, redirect_stdout

from helpers import REPO, TempEnv, load_path, user

from ccsessions import config, ledger, legacy, procs, resumecmd


class Procs(TempEnv):
    def test_R15_tty_normalized(self):
        self.assertEqual(procs.norm_tty("/dev/ttys004"), "ttys004")
        self.assertEqual(procs.norm_tty("ttys004"), "ttys004")
        self.assertEqual(procs.norm_tty("s004"), "ttys004")
        self.assertIsNone(procs.norm_tty("??"))
        self.assertIsNone(procs.norm_tty(""))

    def test_R14_pid_walk_exact_comm(self):
        tree = procs.parse_ps_tree("  300   200 python3\n  200   100 /bin/sh\n  100    50 claude\n"
                                   "   50     1 -zsh\n  400   300 claude-helper\n")
        self.assertEqual(procs.find_claude(300, tree), 100)
        tree2 = procs.parse_ps_tree("  300   200 python3\n  200   100 claude-code-thing\n  100   1 /opt/x/claude\n")
        self.assertEqual(procs.find_claude(300, tree2), 100)
        self.assertIsNone(procs.find_claude(300, procs.parse_ps_tree("  300  200 python3\n  200 1 bash\n")))
        deep = "\n".join("%d %d sh" % (i, i - 1) for i in range(20, 10, -1)) + "\n10 1 claude\n"
        self.assertIsNone(procs.find_claude(20, procs.parse_ps_tree(deep), max_steps=6))

    def test_R14_interactive_from_argv(self):
        self.assertTrue(procs.is_interactive_command("claude --resume abc"))
        self.assertTrue(procs.is_interactive_command("claude -w --permission-mode auto"))
        self.assertFalse(procs.is_interactive_command("claude -p hello"))
        self.assertFalse(procs.is_interactive_command("claude --print --output-format json"))
        self.assertFalse(procs.is_interactive_command("claude --print=x"))

    def test_proc_info_parse(self):
        info = procs.parse_proc_info("Mon Sep 28 12:00:16 2026     ttys004  claude --resume x\n")
        self.assertEqual(info, {"pid_start": "Mon Sep 28 12:00:16 2026", "tty": "ttys004",
                                "command": "claude --resume x"})
        self.assertIsNone(procs.parse_proc_info(""))

    def test_R1_identity_uses_start_time(self):
        lk = lambda pid: ("Mon Sep 28 12:00:16 2026", "claude")
        self.assertTrue(procs.identity_alive(5, "Mon Sep 28 12:00:16 2026", lookup=lk))
        self.assertFalse(procs.identity_alive(5, "Sun Sep 27 12:00:16 2026", lookup=lk))
        self.assertFalse(procs.identity_alive(5, None, lookup=lambda pid: ("x", "node")))
        self.assertFalse(procs.identity_alive(5, "x", lookup=lambda pid: None))
        self.assertFalse(procs.identity_alive(None, "x", lookup=lk))

    def test_memory_and_boot_parsers(self):
        self.assertEqual(procs.parse_memory_pressure(
            "The system has 17179869184 (4194304 pages)\nSystem-wide memory free percentage: 23%\n"), 23)
        self.assertIsNone(procs.parse_memory_pressure("garbage"))
        self.assertEqual(procs.parse_boottime("{ sec = 1727000000, usec = 5 } Mon Sep"), 1727000000)

    def test_tree_rss_and_top_apps(self):
        t = {1: (0, 1024, "/Applications/Big Browser.app/Contents/MacOS/x"),
             2: (1, 2048, "/Applications/Big Browser.app/Contents/Frameworks/helper"),
             3: (0, 512, "claude")}
        self.assertEqual(procs.tree_rss_mb(1, t), 3.0)
        self.assertEqual(procs.top_apps(t)[0], ("Big Browser", 3.0))


class Config(TempEnv):
    def test_bad_config_forces_dry_run(self):
        with open(self.cfg_path, "w") as fh:
            fh.write("{not json")
        c = config.load()
        self.assertTrue(c["dry_run"])
        self.assertIsNotNone(c.error)

    def test_db_env_override_and_defaults(self):
        c = config.load()
        self.assertEqual(c.path("db"), self.db)
        self.assertEqual(c["low_free_pct"], 20)
        self.assertEqual(c["high_free_pct"], 35)
        self.assertEqual(c["max_evictions_per_day"], 3)

    def test_resume_command_quoting(self):
        cmd = resumecmd.command(self.cfg, "abc", "/a b/c")
        self.assertTrue(cmd.endswith("abc --cwd '/a b/c'"))
        self.cfg["resume_command"] = "cc-resume"
        self.assertEqual(resumecmd.command(self.cfg, "abc", "/x"), "cc-resume abc --cwd /x")


class ImportLegacy(TempEnv):
    def manifest(self, entries, name="manifest-20260101-000000.json"):
        d = self.cfg.path("legacy_manifests")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name), "w") as fh:
            json.dump({"entries": entries}, fh)

    def test_manifest_entries_become_hibernated_rows(self):
        killed = time.time() - 3600
        ks = time.strftime("%Y%m%d-%H%M%S", time.localtime(killed))
        self.transcript("h1", [user("x")], mtime=killed - 10)
        self.transcript("h2", [user("x")], mtime=killed + 600)  # resumed by hand later
        self.transcript("h3", [user("x")], mtime=killed - 10)
        self.manifest([
            {"sessionId": "h1", "killedAt": ks, "woken": False, "itermId": "G1", "tty": "ttys003",
             "cwd": "/w/a", "title": "A", "pid": 999},
            {"sessionId": "h2", "killedAt": ks, "woken": False, "cwd": "/w/b"},
            {"sessionId": "h3", "killedAt": ks, "woken": True, "cwd": "/w/c"},
            {"sessionId": "h4", "killedAt": ks, "woken": False, "cwd": "/w/d"},  # no transcript
            {"sessionId": "h5", "killedAt": "garbage", "cwd": "/w/e"},
        ])
        stats = legacy.import_legacy(self.conn, self.cfg, info_fn=lambda pid: None)
        self.assertEqual(stats["inserted"], 1)
        r = ledger.get(self.conn, "h1")
        self.assertEqual((r["state"], r["source"], r["iterm_guid"], r["tty"], r["title"]),
                         ("hibernated", "legacy", "G1", "ttys003", "A"))
        self.assertAlmostEqual(r["evicted_at"], killed, delta=1)
        self.assertIsNone(r["pid"])
        self.assertTrue(r["transcript"].endswith("h1.jsonl"))
        for sid in ("h2", "h3", "h4", "h5"):
            self.assertIsNone(ledger.get(self.conn, sid), sid)
        # idempotent
        stats = legacy.import_legacy(self.conn, self.cfg, info_fn=lambda pid: None)
        self.assertEqual((stats["inserted"], stats["updated"]), (0, 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)

    def test_never_overwrites_hook_rows_and_reads_live_registry(self):
        self.insert(session_id="live1", source="hook", state="busy")
        sd = self.cfg.path("claude_sessions")
        os.makedirs(sd)
        for pid, sid, status in ((501, "live1", "idle"), (502, "live2", "waiting"), (503, "dead", "idle")):
            with open(os.path.join(sd, "%d.json" % pid), "w") as fh:
                json.dump({"pid": pid, "sessionId": sid, "status": status, "cwd": "/w",
                           "kind": "interactive", "entrypoint": "cli", "updatedAt": 1000}, fh)
        info = {501: {"pid_start": "S1", "comm": "claude", "tty": "ttys001"},
                502: {"pid_start": "S2", "comm": "claude", "tty": "ttys002"}}
        stats = legacy.import_legacy(self.conn, self.cfg, info_fn=info.get)
        self.assertEqual(stats["skipped_hook"], 1)
        self.assertEqual(ledger.get(self.conn, "live1")["state"], "busy")
        r = ledger.get(self.conn, "live2")
        self.assertEqual((r["state"], r["source"], r["pid_start"], r["tty"]), ("waiting", "legacy", "S2", "ttys002"))
        self.assertIsNone(ledger.get(self.conn, "dead"))


class CcResume(TempEnv):
    def setUp(self):
        super().setUp()
        self.mod = load_path("cc_resume_bin", os.path.join(REPO, "bin", "cc-resume"))

    def call(self, argv, identity):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.mod.main(argv, identity=identity)
        return rc, out.getvalue(), err.getvalue()

    def test_R10_refuses_when_identity_alive(self):
        self.insert(session_id="r1", state="idle", pid=4321)
        rc, out, err = self.call(["r1", "--dry-run"], identity=lambda r: True)
        self.assertEqual(rc, 1)
        self.assertIn("already running", err)

    def test_dry_run_resolves_cwd_from_ledger(self):
        self.insert(session_id="r2", state="hibernated", pid=None, cwd=self.tmp)
        rc, out, err = self.call(["r2", "--dry-run"], identity=lambda r: False)
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume r2", out)
        self.assertEqual(ledger.state_of(self.conn, "r2"), "hibernated")  # dry run writes nothing

    def test_mark_resuming_tolerates_missing_db(self):
        self.insert(session_id="r3", state="hibernated")
        self.mod.mark_resuming(self.cfg, "r3")
        self.assertEqual(ledger.state_of(self.conn, "r3"), "resuming")
        os.environ["CC_SESSIONS_DB"] = os.path.join(self.tmp, "missing", "x.db")
        self.mod.mark_resuming(config.load(), "r3")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "missing")))

    def test_parked_branch_uses_configured_archive(self):
        cfg = dict(self.cfg)
        cfg["park_archive_dir"] = os.path.join(self.tmp, "arch", "{repo_key}")
        self.assertEqual(self.mod.repo_key("/a/b/c"), "a-b-c")
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        branch, arch = self.mod.parked_branch(cfg, repo, "wt1")
        self.assertIsNone(branch)  # not a git repo: no parked branch
        self.assertEqual(arch, os.path.join(self.tmp, "arch", self.mod.repo_key(repo), "wt1"))
        wt = repo + cfg["worktree_marker"] + "gone"
        self.assertEqual(self.mod.fallback_dir(cfg, wt + "/sub"), repo)
        self.assertEqual(self.mod.fallback_dir(cfg, os.path.join(self.tmp, "x", "y")), self.tmp)
