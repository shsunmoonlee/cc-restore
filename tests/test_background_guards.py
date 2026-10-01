import json
import os
import time

from helpers import TempEnv, assistant, iso, load_daemon, user

from ccsessions import pending, procs

D = load_daemon()


def result(r, t, tool_use_id=None):
    d = {"type": "user", "timestamp": iso(t), "toolUseResult": r}
    if tool_use_id:
        d["message"] = {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}]}
    return d


def note(tid, t, summary=None, event=None, status=None):
    body = "<task-notification>\n<task-id>%s</task-id>\n" % tid
    if status:
        body += "<status>%s</status>\n" % status
    if summary:
        body += "<summary>%s</summary>\n" % summary
    if event:
        body += "<event>%s</event>\n" % event
    return user(body + "</task-notification>", t=t)


def queued_note(tid, t, event):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(t),
            "content": "<task-notification>\n<task-id>%s</task-id>\n<summary>Monitor event: "
                       "\"w\"</summary>\n<event>%s</event>\n</task-notification>" % (tid, event)}


class Base(TempEnv):
    def an(self, records, now=None, **kw):
        return pending.analyze(self.transcript("p", records), now or time.time(), **kw)["pending"]


class MonitorGuards(Base):
    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.launch = result({"taskId": "bm1", "timeoutMs": 1800000, "persistent": False}, self.now - 600)
        self.events = [note("bm1", self.now - 500, summary='Monitor event: "watch"', event="#19 OPEN"),
                       note("bm1", self.now - 400, summary='Monitor event: "watch"', event="#19 MERGED")]

    def test_progress_events_keep_monitor_pending(self):
        self.assertEqual(self.an([user("go"), self.launch] + self.events, self.now), ["1 monitor running"])

    def test_timed_out_and_expired_events_finish(self):
        for ev in ("[Monitor timed out after 30m]", "[Monitor expired after 30m with 4 events delivered.]"):
            end = note("bm1", self.now - 300, summary='Monitor event: "watch"', event=ev)
            self.assertEqual(self.an([user("go"), self.launch] + self.events + [end], self.now), [])

    def test_status_completed_finishes(self):
        for st in ("completed", "failed", "killed", "stopped"):
            end = note("bm1", self.now - 300, summary="stream ended", status=st)
            self.assertEqual(self.an([user("go"), self.launch] + self.events + [end], self.now), [])

    def test_status_inside_event_text_is_not_terminal(self):
        ev = note("bm1", self.now - 300, summary='Monitor event: "w"', event="got <status>done</status>")
        self.assertEqual(self.an([user("go"), self.launch, ev], self.now), ["1 monitor running"])

    def test_queue_operation_envelope_progress_vs_end(self):
        prog = queued_note("bm1", self.now - 300, "line")
        self.assertEqual(self.an([user("go"), self.launch, prog, {"type": "queue-operation",
                                  "operation": "dequeue", "timestamp": iso(self.now - 299)}],
                                 self.now), ["1 monitor running"])
        end = queued_note("bm1", self.now - 300, "[Monitor expired after 30m]")
        self.assertEqual(self.an([user("go"), self.launch, end, {"type": "queue-operation",
                                  "operation": "dequeue", "timestamp": iso(self.now - 299)}],
                                 self.now), [])

    def test_timeout_expiry(self):
        launch = result({"taskId": "bm2", "timeoutMs": 60000}, self.now - 1000)
        self.assertEqual(self.an([user("go"), launch], self.now - 1000 + 60 + 119), ["1 monitor running"])
        self.assertEqual(self.an([user("go"), launch], self.now - 1000 + 60 + 121), [])

    def test_persistent_monitor_stays_pending(self):
        for r in ({"taskId": "bm3", "timeoutMs": 0, "persistent": True},
                  {"taskId": "bm3", "timeoutMs": 0}):
            launch = result(r, self.now - 5 * 3600)
            self.assertEqual(self.an([user("go"), launch], self.now), ["1 monitor running"])
            self.assertEqual(self.an([user("go"), launch], self.now + 2 * 3600), [])
            stop = assistant("stop", t=self.now - 60, tools=[
                {"type": "tool_use", "name": "TaskStop", "input": {"task_id": "bm3"}}])
            self.assertEqual(self.an([user("go"), launch, stop], self.now), [])

    def test_bash_and_agent_notifications_with_status_still_finish(self):
        bg = result({"backgroundTaskId": "b9"}, self.now - 60)
        ag = result({"isAsync": True, "agentId": "a9"}, self.now - 60)
        recs = [user("go"), bg, ag, note("b9", self.now - 30, status="completed", summary="done"),
                note("a9", self.now - 30, status="completed")]
        self.assertEqual(self.an(recs, self.now), [])


class WorkflowGuards(Base):
    def test_workflow_launch_and_completion(self):
        now = time.time()
        launch = result({"status": "async_launched", "taskId": "w1", "taskType": "local_workflow",
                         "runId": "wf_1", "transcriptDir": "/x/subagents/workflows/wf_1"}, now - 60)
        self.assertEqual(self.an([user("go"), launch], now), ["1 workflow running"])
        done = note("w1", now - 10, status="completed", summary="Workflow done")
        self.assertEqual(self.an([user("go"), launch, done], now), [])

    def test_either_marker_alone_launches(self):
        now = time.time()
        a = result({"taskId": "w1", "taskType": "local_workflow"}, now - 60)
        b = result({"taskId": "w2", "status": "async_launched"}, now - 60)
        self.assertEqual(self.an([user("go"), a, b], now), ["2 workflows running"])


class StaleWindows(Base):
    def test_per_kind(self):
        now = time.time()
        recs = [user("go"),
                result({"backgroundTaskId": "b1"}, now - 7 * 3600),
                result({"taskId": "m1", "timeoutMs": 0, "persistent": True}, now - 7 * 3600),
                result({"isAsync": True, "agentId": "a1"}, now - 7 * 3600),
                result({"taskId": "w1", "taskType": "local_workflow"}, now - 7 * 3600)]
        self.assertEqual(self.an(recs, now), ["1 agent running", "1 workflow running"])
        self.assertEqual(self.an(recs, now + 18 * 3600), [])
        fresh = [user("go"), result({"backgroundTaskId": "b1"}, now - 5 * 3600)]
        self.assertEqual(self.an(fresh, now), ["1 background command running"])


class WakeupGuards(Base):
    def test_scheduled_for_beats_delay(self):
        now = time.time()
        call = assistant("later", t=now - 600, tools=[
            {"type": "tool_use", "id": "tw1", "name": "ScheduleWakeup", "input": {"delaySeconds": 60}}])
        res = result({"scheduledFor": (now + 1800) * 1000, "clampedDelaySeconds": 2400}, now - 599, "tw1")
        bare = assistant("later", t=now - 600, tools=[
            {"type": "tool_use", "name": "ScheduleWakeup", "input": {"delaySeconds": 60}}])
        self.assertEqual(self.an([user("x"), bare], now), [])
        self.assertEqual(self.an([user("x"), call, res], now), ["loop wakeup scheduled"])
        self.assertEqual(self.an([user("x"), call, res], now + 1800 + 119), ["loop wakeup scheduled"])
        self.assertEqual(self.an([user("x"), call, res], now + 1800 + 121), [])

    def test_unmatched_scheduled_for_is_ignored(self):
        now = time.time()
        res = result({"scheduledFor": (now + 1800) * 1000}, now - 60, "other")
        self.assertEqual(self.an([user("x"), res], now), [])

    def test_stop_after_result_clears(self):
        now = time.time()
        call = assistant("later", t=now - 600, tools=[
            {"type": "tool_use", "id": "tw1", "name": "ScheduleWakeup", "input": {"delaySeconds": 60}}])
        res = result({"scheduledFor": (now + 1800) * 1000}, now - 599, "tw1")
        stop = assistant("stop", t=now - 30, tools=[
            {"type": "tool_use", "name": "ScheduleWakeup", "input": {"stop": True}}])
        self.assertEqual(self.an([user("x"), call, res, stop], now), [])


class CronGuards(Base):
    def cron(self, t, cid="c1", sched="17 14 22 8 *", recurring=False, durable=False):
        return result({"id": cid, "humanSchedule": sched, "recurring": recurring, "durable": durable}, t)

    def sched_for(self, fire):
        lt = time.localtime(fire)
        return "%d %d %d %d *" % (lt.tm_min, lt.tm_hour, lt.tm_mday, lt.tm_mon)

    def test_durable_never_blocks(self):
        now = time.time()
        fire = now + 3600
        self.assertEqual(self.an([user("x"), self.cron(now - 60, sched=self.sched_for(fire), durable=True)], now), [])
        self.assertEqual(self.an([user("x"), self.cron(now - 60, recurring=True, sched="*/5 * * * *",
                                                         durable=True)], now), [])

    def test_one_shot_before_and_after_fire(self):
        now = time.time()
        fire = (int(now) // 60) * 60 + 7200
        recs = [user("x"), self.cron(now - 60, sched=self.sched_for(fire))]
        self.assertEqual(self.an(recs, now), ["cron job scheduled"])
        self.assertEqual(self.an(recs, fire + 14 * 60), ["cron job scheduled"])
        self.assertEqual(self.an(recs, fire + 16 * 60), [])

    def test_unparseable_one_shot_blocks_one_hour(self):
        now = time.time()
        recs = [user("x"), self.cron(now - 60, sched="tomorrow afternoon")]
        self.assertEqual(self.an(recs, now), ["cron job scheduled"])
        self.assertEqual(self.an(recs, now + 3600), [])

    def test_recurring_capped(self):
        now = time.time()
        recs = [user("x"), self.cron(now - 60, recurring=True, sched="*/5 * * * *")]
        self.assertEqual(self.an(recs, now), ["recurring cron job"])
        self.assertEqual(self.an(recs, now + 23 * 3600), ["recurring cron job"])
        self.assertEqual(self.an(recs, now + 25 * 3600), [])
        self.assertEqual(self.an(recs, now + 3 * 3600, cron_recurring_block_s=2 * 3600), [])

    def test_cron_delete(self):
        now = time.time()
        rm = assistant("rm", t=now - 30, tools=[{"type": "tool_use", "name": "CronDelete", "input": {"id": "c1"}}])
        recs = [user("x"), self.cron(now - 60, recurring=True, sched="*/5 * * * *"), rm]
        self.assertEqual(self.an(recs, now), [])

    def test_fire_ts_parse(self):
        created = time.mktime((2026, 8, 22, 10, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(pending.cron_fire_ts("17 14 22 8 *", created))[:5], (2026, 8, 22, 14, 17))
        self.assertEqual(time.localtime(pending.cron_fire_ts("0 9 1 1 *", created))[:5], (2027, 1, 1, 9, 0))
        for bad in ("*/5 * * * *", "17 14 31 2 *", "17 14 22 8 1", "", None):
            self.assertIsNone(pending.cron_fire_ts(bad, created))

    def test_daemon_passes_config_window(self):
        self.cfg["cron_recurring_block_h"] = 1
        tr = self.transcript("s1", [user("x"), self.cron(time.time() - 2 * 3600, recurring=True,
                                                           sched="*/5 * * * *")])
        f = D.Facts.__new__(D.Facts)
        f.cfg, f.now, f._analysis = self.cfg, time.time(), {}
        self.assertEqual(f.analysis({"session_id": "s1", "transcript": tr})["pending"], [])
        self.cfg["cron_recurring_block_h"] = 24
        f._analysis = {}
        self.assertEqual(f.analysis({"session_id": "s1", "transcript": tr})["pending"], ["recurring cron job"])


class ChildGuards(Base):
    def test_shell_snapshot_beats_helper_patterns(self):
        snap = "/bin/zsh -c source /opt/u/.claude/shell-snapshots/snapshot-zsh-1-x.sh && npx some-mcp watch"
        table = {10: (1, 1, "claude"), 11: (10, 1, snap), 12: (11, 1, "sleep 30")}
        kids = procs.working_children(10, table, self.cfg["helper_patterns"])
        self.assertIn("shell task", kids)
        self.assertIn("sleep", kids)
        self.assertEqual(procs.working_children(10, table, ["mcp", "zsh", "snapshot"]), ["shell task", "sleep"])

    def test_hook_runners_are_helpers(self):
        hook = "bash " + os.path.join(os.path.expanduser("~"), ".claude", "hooks", "post-tool-use.sh")
        plug = '/bin/sh -c export PATH="/x:$PATH" && "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh"'
        table = {10: (1, 1, "claude"), 11: (10, 1, hook), 12: (11, 1, hook), 13: (10, 1, plug),
                 14: (13, 1, "node run.js"), 15: (10, 1, "caffeinate -i -t 300")}
        self.assertEqual(procs.working_children(10, table, self.cfg["helper_patterns"]), ["caffeinate"])

    def test_hook_runner_shape_is_narrow(self):
        self.assertFalse(procs.is_hook_runner("vim " + os.path.expanduser("~/.claude/hooks/x.sh")))
        self.assertFalse(procs.is_hook_runner("bash /tmp/.claude/hooks/x.sh"))
        self.assertFalse(procs.is_hook_runner("/bin/sh -c export PATH=/x && make"))
        self.assertTrue(procs.is_hook_runner("/bin/bash " + os.path.expanduser("~/.claude/hooks/x.sh")))

    def test_plugin_root_mention_alone_is_not_a_helper(self):
        worker = "node /opt/plugins/foo/worker.js --env CLAUDE_PLUGIN_ROOT=/opt/plugins/foo"
        self.assertFalse(procs.is_hook_runner(worker))
        self.assertFalse(procs.is_hook_runner("CLAUDE_PLUGIN_ROOT=/opt/p /bin/sh -c export PATH=/x"))
        table = {10: (1, 1, "claude"), 11: (10, 1, worker)}
        self.assertEqual(procs.working_children(10, table, self.cfg["helper_patterns"]), ["node"])

    def test_plugin_root_process_matching_helper_patterns_is_a_helper(self):
        server = "node /opt/plugins/foo/mcp-server.js CLAUDE_PLUGIN_ROOT=/opt/plugins/foo"
        table = {10: (1, 1, "claude"), 11: (10, 1, server), 12: (11, 1, "sleep 600")}
        self.assertEqual(procs.working_children(10, table, self.cfg["helper_patterns"]), [])


class ClaudeStatusGuard(Base):
    """Claude Code's own ~/.claude/sessions/<pid>.json status keeps a session that is not
    idle there, whatever the ledger says (a session sat on "waiting" for 40 min after
    /model while the ledger said idle)."""

    SID = "s1"

    def setUp(self):
        super().setUp()
        self.sd = self.cfg.path("claude_sessions")
        os.makedirs(self.sd)
        self.now = time.time()
        self.tr = self.transcript(self.SID, [user("hi"), assistant("done")], mtime=self.now - 3600)

    def write(self, body):
        with open(os.path.join(self.sd, "111.json"), "w") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))

    def status(self, status):
        d = {"pid": 111, "sessionId": self.SID, "statusUpdatedAt": int(self.now * 1000)}
        if status is not None:
            d["status"] = status
        self.write(d)

    def row(self, **kw):
        r = {"session_id": self.SID, "source": "hook", "interactive": 1, "state": "idle",
             "state_since": self.now - 3600, "subagents": 0, "tty": "ttys001",
             "last_focus_at": None, "resumed_at": None, "pid": 111, "pid_start": 1,
             "transcript": self.tr, "last_event_at": self.now - 3600}
        r.update(kw)
        return r

    def guard(self, row=None, cfg=None):
        return D.guard_reason(row or self.row(), now=self.now, cfg=cfg or self.cfg,
                              focus_state={"tty_guid": {"ttys001": "G1"}, "visible_ttys": set()},
                              pending_fn=lambda r: {"pending": [], "has_user": True},
                              children_fn=lambda r: [], identity_fn=lambda r: None,
                              activity_fn=lambda r: self.now - 3600, evictions_today={},
                              hook_failed=set(), status_fn=D.default_status(self.cfg))

    def test_idle_status_allows(self):
        self.status("idle")
        self.assertIsNone(self.guard())

    def test_busy_waiting_shell_keep(self):
        for st in ("busy", "waiting", "shell", "compacting"):
            self.status(st)
            self.assertEqual(self.guard(), "claude status %s" % st, st)

    def test_missing_file_falls_back(self):
        self.assertIsNone(self.guard())
        self.assertIsNone(self.guard(self.row(pid=None)))

    def test_file_without_status_falls_back(self):
        self.status(None)
        self.assertIsNone(self.guard())

    def test_garbage_file_fails_closed(self):
        for body in ("{broken", "", "[1, 2]", "null", '"idle"'):
            self.write(body)
            self.assertEqual(self.guard(), "claude status file unreadable", body)

    def test_stale_busy_row_is_still_kept_by_status(self):
        self.status("busy")
        r = self.row(state="busy", state_since=self.now - 3 * 3600)
        self.assertEqual(self.guard(r), "claude status busy")

    def test_compute_policy_wires_the_status_guard_for_both_passes(self):
        pcfg = D.config.Config(self.cfg)
        pcfg["idle_min"] = 3
        orig = (procs.identity_probe, procs.ps_commands)
        procs.identity_probe = lambda pid, start, **kw: procs.ALIVE
        procs.ps_commands = lambda: {}
        try:
            focus = {"tty_guid": {"ttys001": "G1"}, "visible_ttys": set(), "visible_guids": set()}
            for cfg in (self.cfg, pcfg):
                self.status("waiting")
                cands, kept, _, _ = D.compute_policy(cfg, focus, [self.row()], {}, now=self.now)
                self.assertEqual(cands, [])
                self.assertEqual(kept[0][1], "claude status waiting")
                self.status("idle")
                cands, kept, _, _ = D.compute_policy(cfg, focus, [self.row()], {}, now=self.now)
                self.assertEqual([r["session_id"] for r in cands], [self.SID], kept)
        finally:
            procs.identity_probe, procs.ps_commands = orig

    def test_daemon_pressure_and_idle_passes_keep_a_waiting_session(self):
        import asyncio
        from ccsessions import ledger
        self.cfg["idle_hibernate_min"] = 3
        ledger_row = self.insert(session_id=self.SID, transcript=self.tr, pid=111, tty="ttys001",
                                 state_since=self.now - 3600, last_event_at=self.now - 3600)
        self.assertEqual(ledger_row["state"], "idle")
        d = D.Daemon(self.cfg, self.conn, None, None, False)

        async def snap():
            return {"tty_guid": {"ttys001": "G1"}, "visible_ttys": set(), "visible_guids": set(),
                    "panes": 1, "skipped": 0}
        d.snapshot = snap
        evicted = []

        async def evict(row, free, rss, recheck=None, title=None, reason=None):
            evicted.append(row["session_id"])
            return "aborted-test"
        d.evictor.evict = evict
        orig = (procs.identity_probe, procs.ps_commands)
        procs.identity_probe = lambda pid, start, **kw: procs.ALIVE
        procs.ps_commands = lambda: {}
        loop = asyncio.new_event_loop()
        try:
            self.status("waiting")
            self.assertIsNone(loop.run_until_complete(d.pick_and_evict(10)))
            self.assertEqual(loop.run_until_complete(d.idle_pass(10)), [])
            self.assertEqual(evicted, [])
            self.status("idle")
            self.assertEqual(loop.run_until_complete(d.pick_and_evict(10)), "aborted-test")
            self.assertEqual(evicted, [self.SID])
        finally:
            loop.close()
            procs.identity_probe, procs.ps_commands = orig

    def test_final_check_rereads_the_status_before_signal(self):
        from ccsessions import ledger
        self.insert(session_id=self.SID, state="evicting", pid=111, pid_start="x")
        row = ledger.get(self.conn, self.SID)
        ev = D.Evictor(self.conn, self.cfg, None)
        self.status("idle")
        self.assertIsNone(ev.final_check(row))
        self.status("busy")
        self.assertEqual(ev.final_check(row), "claude status busy before signal")
        self.write("{broken")
        self.assertEqual(ev.final_check(row), "registry file unreadable before signal")
        os.remove(os.path.join(self.sd, "111.json"))
        self.assertIsNone(ev.final_check(row))


class ActivityAndSelfTest(Base):
    def test_workflow_subagent_writes_count(self):
        tr = self.transcript("s1", [user("x")])
        old = time.time() - 7200
        os.utime(tr, (old, old))
        act = D.default_activity(self.cfg)
        self.assertAlmostEqual(act({"session_id": "s1", "transcript": tr}), old, delta=1)
        wf = os.path.join(tr[:-6], "subagents", "workflows", "wf_1")
        os.makedirs(wf)
        open(os.path.join(wf, "agent-a1.jsonl"), "w").close()
        self.assertGreater(act({"session_id": "s1", "transcript": tr}), old + 3600)

    def test_self_test_passes_and_guards_new_shapes(self):
        self.assertEqual(pending.self_test(self.tmp), [])
        orig = pending.notified_ids
        pending.notified_ids = lambda raw: {m.decode() for env in pending.NOTIFICATION.findall(raw)
                                           for m in pending.TASK_ID.findall(env)}
        try:
            self.assertTrue(pending.self_test(self.tmp))
        finally:
            pending.notified_ids = orig
