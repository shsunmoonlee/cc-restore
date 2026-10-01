import os
import time

from helpers import TempEnv

from ccsessions import holds, restore

BOOT = 1_000_000.0


class RestoreSelect(TempEnv):
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

    def sel(self, rows, **kw):
        kw.setdefault("alive_fn", lambda r: False)
        run, typed, skipped = restore.select(rows, BOOT, BOOT + 60, self.cfg, **kw)
        return ([r["session_id"] for r in run], [r["session_id"] for r in typed],
                {r["session_id"]: why for r, why in skipped})

    def test_crash_row_restored(self):
        run, typed, skipped = self.sel(self.rows({}))
        self.assertEqual(run, ["s0"])

    def test_running_and_deliberate_exit_excluded(self):
        rows = self.rows({}, {"state": "ended", "ended_at": BOOT - 50, "end_reason": "prompt_input_exit"})
        run, _, skipped = self.sel(rows, alive_fn=lambda r: r["session_id"] == "s0")
        self.assertEqual(run, [])
        self.assertEqual(skipped["s0"], "already running")
        self.assertTrue(skipped["s1"].startswith("exited"))

    def test_R11_shutdown_cluster_by_gaps(self):
        rows = self.rows(
            {"state": "ended", "end_reason": "other", "ended_at": BOOT - 100, "last_event_at": BOOT - 90},
            {"state": "ended", "end_reason": "other", "ended_at": BOOT - 120, "last_event_at": BOOT - 130},
            {"state": "ended", "end_reason": "other", "ended_at": BOOT - 145, "last_event_at": BOOT - 150},
            # 200 s gap: an earlier, unrelated exit with reason other
            {"state": "ended", "end_reason": "other", "ended_at": BOOT - 345, "last_event_at": BOOT - 350},
        )
        run, _, skipped = self.sel(rows)
        self.assertEqual(sorted(run), ["s0", "s1", "s2"])
        self.assertIn("s3", skipped)

    def test_R11_cluster_needs_anchor_near_last_activity(self):
        rows = self.rows(
            {"state": "ended", "end_reason": "other", "ended_at": BOOT - 3600, "last_event_at": BOOT - 3700},
            {"state": "idle", "pid": 9, "last_event_at": BOOT - 60},  # activity long after T
        )
        run, _, skipped = self.sel(rows)
        self.assertEqual(run, ["s1"])
        self.assertIn("s0", skipped)

    def test_hibernated_excluded_unless_flag(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "G1"})
        run, typed, skipped = self.sel(rows)
        self.assertEqual((run, typed), ([], []))
        run, typed, _ = self.sel(rows, include_hibernated=True)
        self.assertEqual(typed, ["s0"])

    def test_R11_auto_types_hibernated_rows_whose_tab_is_gone(self):
        rows = self.rows({"state": "hibernated", "iterm_guid": "G1"}, {"state": "hibernated", "iterm_guid": "G2"})
        run, typed, _ = self.sel(rows, auto=True, live_guids={"G2"})
        self.assertEqual(typed, ["s0"])
        run, typed, _ = self.sel(rows, auto=True, live_guids=None)  # cannot ask iTerm2: leave them
        self.assertEqual(typed, [])

    def test_R11_legacy_rows_included(self):
        run, _, _ = self.sel(self.rows({"source": "legacy"}))
        self.assertEqual(run, ["s0"])

    def test_auto_only_pre_boot_and_cwd_must_exist(self):
        rows = self.rows({"last_event_at": BOOT + 10}, {"cwd": os.path.join(self.tmp, "gone")})
        run, _, skipped = self.sel(rows, auto=True)
        self.assertEqual(run, [])
        self.assertEqual(skipped["s0"], "active after boot")
        self.assertTrue(skipped["s1"].startswith("cwd gone"))

    def test_R14_non_interactive_and_superseded_never_restored(self):
        run, _, _ = self.sel(self.rows({"interactive": 0}, {"state": "superseded"}))
        self.assertEqual(run, [])

    def test_osascript_typed_vs_run(self):
        lines = restore.osascript_for([("cc-resume a --cwd '/x'", True), ('say "q"', False)])
        joined = "\n".join(lines)
        self.assertIn('write text "cc-resume a --cwd \'/x\'"\n', joined + "\n")
        self.assertIn('write text "say \\"q\\"" newline NO', joined)


class Holds(TempEnv):
    def h(self, path, alive=lambda r: False, now=None, guids=lambda: set()):
        return holds.holds(path, self.cfg, now=now, alive_fn=alive, guids_fn=guids)

    def setUp(self):
        super().setUp()
        self.w = os.path.join(self.tmp, "repo", ".claude", "worktrees", "wt1")
        os.makedirs(os.path.join(self.w, "sub"))

    def test_alive_row_holds(self):
        self.insert(cwd=self.w, launch_cwd=self.w, last_event_at=time.time() - 90 * 86400)
        self.assertEqual(self.h(self.w, alive=lambda r: True)[0], holds.HELD)
        self.assertEqual(self.h(self.w)[0], holds.FREE)

    def test_R4_child_holds_parent_does_not(self):
        self.insert(cwd=os.path.join(self.w, "sub"), launch_cwd=os.path.join(self.w, "sub"))
        self.assertEqual(self.h(self.w, alive=lambda r: True)[0], holds.HELD)
        self.conn.execute("DELETE FROM sessions")
        self.insert(cwd=os.path.join(self.tmp, "repo"), launch_cwd=os.path.join(self.tmp, "repo"))
        self.assertEqual(self.h(self.w, alive=lambda r: True)[0], holds.FREE)
        # sibling with a shared prefix does not match
        self.conn.execute("DELETE FROM sessions")
        self.insert(cwd=self.w + "-other", launch_cwd=self.w + "-other")
        self.assertEqual(self.h(self.w, alive=lambda r: True)[0], holds.FREE)

    def test_R4_launch_cwd_matches_and_realpath(self):
        link = os.path.join(self.tmp, "link")
        os.symlink(self.w, link)
        self.insert(cwd="/elsewhere", launch_cwd=link)
        self.assertEqual(self.h(self.w, alive=lambda r: True)[0], holds.HELD)

    def test_hibernated_within_keep(self):
        now = time.time()
        self.insert(cwd=self.w, state="hibernated", evicted_at=now - 50 * 86400,
                    last_event_at=now - 50 * 86400, ended_at=None)
        self.assertEqual(self.h(self.w)[0], holds.HELD)
        self.assertEqual(self.h(self.w, now=now + 20 * 86400)[0], holds.FREE)

    def test_hibernated_open_tab_holds_past_keep(self):
        now = time.time()
        self.insert(cwd=self.w, state="hibernated", evicted_at=now - 400 * 86400,
                    last_event_at=now - 400 * 86400, ended_at=None, iterm_guid="w0t0p0:ABCDEF12")
        self.assertEqual(self.h(self.w, guids=lambda: {"w0t0p0:ABCDEF12"})[0], holds.HELD)
        self.assertEqual(self.h(self.w, guids=lambda: {"w9t9p9:OTHER999"})[0], holds.FREE)
        self.assertEqual(self.h(self.w, guids=lambda: None)[0], holds.HELD)

    def test_hibernated_no_guid_expires(self):
        now = time.time()
        self.insert(cwd=self.w, state="hibernated", evicted_at=now - 400 * 86400,
                    last_event_at=now - 400 * 86400, ended_at=None)
        called = []
        self.assertEqual(self.h(self.w, guids=lambda: called.append(1))[0], holds.FREE)
        self.assertEqual(called, [])

    def test_old_ended_row_does_not_hold(self):
        now = time.time()
        self.insert(cwd=self.w, state="ended", ended_at=now - 40 * 86400, last_event_at=now - 40 * 86400)
        self.assertEqual(self.h(self.w)[0], holds.FREE)
        self.conn.execute("DELETE FROM sessions")
        self.insert(cwd=self.w, state="ended", ended_at=now - 5 * 86400, last_event_at=now - 5 * 86400)
        self.assertEqual(self.h(self.w)[0], holds.HELD)

    def test_R4_transcript_fallback_encoding(self):
        enc = holds.encode_project_dir(os.path.realpath(self.w))
        self.assertNotIn(".", enc)
        self.assertNotIn("_", holds.encode_project_dir("/a_b"))
        self.transcript("x", [{"type": "user"}], cwd_dir=enc)
        self.assertEqual(self.h(self.w)[0], holds.HELD)
        self.assertEqual(self.h(self.w, now=time.time() + 31 * 86400)[0], holds.FREE)

    def test_unreadable_ledger_is_2(self):
        os.environ["CC_SESSIONS_DB"] = os.path.join(self.tmp, "missing.db")
        from ccsessions import config
        self.assertEqual(holds.holds(self.w, config.load(), alive_fn=lambda r: False)[0], holds.UNREADABLE)
        bad = os.path.join(self.tmp, "bad.db")
        with open(bad, "w") as fh:
            fh.write("this is not sqlite")
        os.environ["CC_SESSIONS_DB"] = bad
        self.assertEqual(holds.holds(self.w, config.load(), alive_fn=lambda r: False)[0], holds.UNREADABLE)
