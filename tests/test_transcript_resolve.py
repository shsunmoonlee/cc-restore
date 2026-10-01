"""Transcript resolution: a session resumed from another cwd is reported under the new
cwd's project dir, but its transcript keeps growing in the project dir it was created in."""
import asyncio
import io
import json
import os
import time

from helpers import TempEnv, assistant, load_daemon, user

from ccsessions import cli, ledger

D = load_daemon()

SID = "668cfc1f-1111-4222-8333-444455556666"
PROC = {"pid": 4242, "pid_start": "Mon Sep 28 10:00:00 2026", "tty": "ttys004", "interactive": True}


def arun(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def focus(ttys=("ttys001",), visible=()):
    return {"tty_guid": {t: "G-" + t for t in ttys}, "visible_ttys": set(visible),
            "visible_guids": set(), "panes": len(ttys), "skipped": 0}


class Base(TempEnv):
    def real(self, sid=SID, cwd_dir="-orig", mtime=None):
        return self.transcript(sid, [user("hi"), assistant("done")], cwd_dir=cwd_dir, mtime=mtime)

    def phantom(self, sid=SID, cwd_dir="-other-cwd"):
        """What Claude Code reports after a resume from another cwd: the path does not
        exist, only a <sid>/tool-results dir may."""
        d = os.path.join(self.projects, cwd_dir)
        os.makedirs(os.path.join(d, sid, "tool-results"), exist_ok=True)
        return os.path.join(d, "%s.jsonl" % sid)


class Resolver(Base):
    def test_recorded_exists_is_returned_unchanged(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 60)
        self.real(cwd_dir="-b")  # newer elsewhere: a fresh recorded file still wins
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec), rec)

    def test_fresh_recorded_file_does_not_glob(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 60)
        orig = ledger.glob.glob
        calls = []
        ledger.glob.glob = lambda *a, **k: calls.append(a) or orig(*a, **k)
        try:
            self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=600), rec)
        finally:
            ledger.glob.glob = orig
        self.assertEqual(calls, [])

    def test_stale_recorded_file_loses_to_a_newer_one_elsewhere(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 3600)
        new = self.real(cwd_dir="-b", mtime=time.time() - 30)
        self.real(cwd_dir="-c", mtime=time.time() - 7200)
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=600), new)

    def test_stale_recorded_file_that_is_still_newest_is_kept(self):
        self.real(cwd_dir="-b", mtime=time.time() - 7200)
        rec = self.real(cwd_dir="-a", mtime=time.time() - 3600)
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=600), rec)

    def test_stale_threshold_is_the_callers(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 900)
        new = self.real(cwd_dir="-b", mtime=time.time() - 30)
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=1800), rec)
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=600), new)

    def test_stale_recorded_file_with_a_non_uuid_sid_is_kept_without_glob(self):
        rec = self.real(sid="s1", cwd_dir="-a", mtime=time.time() - 3600)
        self.real(sid="s1", cwd_dir="-b")
        self.assertEqual(ledger.resolve_transcript(self.projects, "s1", rec, stale_s=600), rec)

    def test_stale_glob_stays_one_level_deep(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 3600)
        d = os.path.join(self.projects, "-orig", "parent-sid", "subagents")
        os.makedirs(d)
        with open(os.path.join(d, "%s.jsonl" % SID), "w") as fh:
            fh.write("{}\n")
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, rec, stale_s=600), rec)

    def test_recorded_missing_resolves_to_the_real_file(self):
        real = self.real()
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, self.phantom()), real)

    def test_no_recorded_path_still_resolves(self):
        real = self.real()
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, None), real)

    def test_newest_mtime_wins(self):
        self.real(cwd_dir="-old", mtime=time.time() - 3600)
        new = self.real(cwd_dir="-new", mtime=time.time() - 10)
        self.real(cwd_dir="-mid", mtime=time.time() - 600)
        self.assertEqual(ledger.resolve_transcript(self.projects, SID, self.phantom()), new)

    def test_nothing_found_is_none(self):
        self.assertIsNone(ledger.resolve_transcript(self.projects, SID, self.phantom()))
        self.assertIsNone(ledger.resolve_transcript(self.projects, SID, None))
        self.assertIsNone(ledger.resolve_transcript(None, SID, None))

    def test_sid_must_be_a_uuid(self):
        self.real(sid="s1")
        self.real(sid="x" * 36)
        for bad in ("s1", "*", "?" * 36, "../" + SID, SID + "/x", "", None, 7, "x" * 36,
                    SID.replace("-", "*"), "*-*-*-*-*"):
            self.assertIsNone(ledger.resolve_transcript(self.projects, bad, None), bad)

    def test_a_valid_recorded_file_is_kept_for_any_sid(self):
        rec = self.real(sid="s1")
        self.assertEqual(ledger.resolve_transcript(self.projects, "s1", rec), rec)

    def test_subagent_dir_file_is_not_matched(self):
        d = os.path.join(self.projects, "-orig", "parent-sid", "subagents")
        os.makedirs(d)
        with open(os.path.join(d, "%s.jsonl" % SID), "w") as fh:
            fh.write("{}\n")
        self.assertIsNone(ledger.resolve_transcript(self.projects, SID, self.phantom()))

    def test_directory_named_like_the_transcript_is_not_matched(self):
        os.makedirs(os.path.join(self.projects, "-orig", "%s.jsonl" % SID))
        self.assertIsNone(ledger.resolve_transcript(self.projects, SID, None))

    def test_projects_dir_with_glob_characters(self):
        weird = os.path.join(self.tmp, "pro[j]ects*")
        os.makedirs(os.path.join(weird, "-orig"))
        p = os.path.join(weird, "-orig", "%s.jsonl" % SID)
        with open(p, "w") as fh:
            fh.write("{}\n")
        self.assertEqual(ledger.resolve_transcript(weird, SID, None), p)


class HealWrite(Base):
    def test_heal_is_compare_and_set(self):
        self.insert(session_id=SID, transcript="/old")
        self.assertEqual(ledger.heal_transcript(self.conn, SID, "/other", "/new"), 0)
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], "/old")
        self.assertEqual(ledger.heal_transcript(self.conn, SID, "/old", "/new"), 1)
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], "/new")

    def test_heal_null_recorded(self):
        self.insert(session_id=SID, transcript=None)
        self.assertEqual(ledger.heal_transcript(self.conn, SID, None, "/new"), 1)

    def test_heal_never_raises(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.assertEqual(ledger.heal_transcript(self.conn, SID, None, "/new"), 0)
        finally:
            self.conn.execute("ROLLBACK")


class HookIngestion(Base):
    def hook(self, event, **kw):
        d = {"session_id": SID, "cwd": "/w"}
        d.update(kw)
        return cli.hook_main(event, io.BytesIO(json.dumps(d).encode()), lambda: dict(PROC))

    def test_existing_payload_path_is_kept(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 60)
        self.real(cwd_dir="-b")
        self.hook("SessionStart", transcript_path=rec, source="startup")
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], rec)

    def test_missing_payload_path_stores_the_resolved_one(self):
        real = self.real()
        self.hook("SessionStart", transcript_path=self.phantom(), source="resume")
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], real)
        self.hook("UserPromptSubmit", transcript_path=self.phantom())
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], real)

    def test_brand_new_session_keeps_the_payload_path(self):
        p = os.path.join(self.projects, "-new", "%s.jsonl" % SID)
        self.hook("SessionStart", transcript_path=p, source="startup")
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], p)
        # the file appears later in that dir: the next event still stores it
        self.real(cwd_dir="-new")
        self.hook("UserPromptSubmit", transcript_path=p)
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], p)

    def test_apply_event_defaults_to_the_payload_grandparent(self):
        real = self.real()
        ledger.apply_event(self.conn, "Stop", {"session_id": SID, "transcript_path": self.phantom()},
                           None)
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], real)


class Daemon(Base):
    def guard(self, row):
        return D.guard_reason(row, now=time.time(), cfg=self.cfg, focus_state=focus(),
                              pending_fn=lambda r: {"pending": [], "has_user": True},
                              children_fn=lambda r: [], identity_fn=lambda r: None,
                              activity_fn=lambda r: time.time() - 3600, evictions_today={},
                              hook_failed=set())

    def row(self, transcript):
        return {"session_id": SID, "source": "hook", "interactive": 1, "state": "idle",
                "state_since": time.time() - 3600, "subagents": 0, "tty": "ttys001",
                "last_focus_at": None, "resumed_at": None, "pid": 10, "pid_start": 1,
                "transcript": transcript, "last_event_at": time.time() - 3600}

    def test_guard_resolves_instead_of_no_transcript(self):
        real = self.real()
        r = self.row(self.phantom())
        self.assertIsNone(self.guard(r))
        self.assertEqual(r["transcript"], real)

    def test_guard_follows_a_newer_file_when_the_recorded_one_is_stale(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 3600)
        new = self.real(cwd_dir="-b", mtime=time.time() - 3000)
        r = self.row(rec)
        self.assertIsNone(self.guard(r))
        self.assertEqual(r["transcript"], new)

    def test_row_transcript_passes_idle_min_as_the_threshold(self):
        rec = self.real(cwd_dir="-a", mtime=time.time() - 15 * 60)
        new = self.real(cwd_dir="-b", mtime=time.time() - 60)
        self.assertEqual(D.row_transcript(self.row(rec), {"claude_projects": self.projects,
                                                          "idle_min": 20}), rec)
        self.assertEqual(D.row_transcript(self.row(rec), {"claude_projects": self.projects,
                                                          "idle_min": 10}), new)

    def test_guard_still_fails_closed(self):
        self.assertEqual(self.guard(self.row(self.phantom())), "no transcript")
        self.assertEqual(self.guard(self.row(None)), "no transcript")

    def test_activity_and_analysis_read_the_resolved_file(self):
        real = self.real(mtime=time.time() - 42)
        r = self.row(self.phantom())
        newest = D.default_activity(self.cfg)(r)
        self.assertAlmostEqual(newest, os.path.getmtime(real), places=3)
        r2 = self.row(self.phantom())
        self.assertTrue(D.Facts(self.cfg).analysis(r2).get("has_user"))

    def test_daemon_policy_heals_the_ledger(self):
        real = self.real()
        ph = self.phantom()
        self.insert(session_id=SID, transcript=ph, pid=None, pid_start=None, tty="ttys001")
        d = D.Daemon(self.cfg, self.conn, None, None, True)
        _, kept, _, _ = arun(d.policy(focus()))
        # kept for an earlier reason (no process), but the row is healed anyway
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], real)
        self.assertNotIn("no transcript", [w for _, w in kept])

    def test_policy_pass_heals_the_ledger(self):
        real = self.real()
        self.insert(session_id=SID, transcript=self.phantom())
        D.policy_pass(self.conn, self.cfg, focus())
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], real)

    def test_unresolvable_row_is_left_alone(self):
        ph = self.phantom()
        self.insert(session_id=SID, transcript=ph)
        _, kept, _, _ = D.policy_pass(self.conn, self.cfg, focus())
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], ph)

    def test_heal_respects_a_concurrent_hook_write(self):
        self.real()
        ph = self.phantom()
        self.insert(session_id=SID, transcript=ph)
        rows = D.live_rows(self.conn)
        before = {r["session_id"]: r.get("transcript") for r in rows}
        D.row_transcript(rows[0], self.cfg)
        with ledger.tx(self.conn):
            self.conn.execute("UPDATE sessions SET transcript='/hook' WHERE session_id=?", (SID,))
        self.assertEqual(D.heal_transcripts(self.conn, rows, before), [])
        self.assertEqual(ledger.get(self.conn, SID)["transcript"], "/hook")
