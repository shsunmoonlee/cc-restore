import json
import os
import time

from helpers import TempEnv, assistant, iso, user

from ccsessions import pending


class Pending(TempEnv):
    def an(self, records, now=None):
        return pending.analyze(self.transcript("p", records), now)

    def test_clean_transcript_has_nothing_pending(self):
        a = self.an([user("hi"), assistant("all done")])
        self.assertEqual(a["pending"], [])
        self.assertEqual(a["last_text"], "all done")
        self.assertTrue(a["has_user"])

    def test_async_agent_until_task_notification(self):
        launch = {"type": "user", "timestamp": iso(time.time() - 60),
                  "toolUseResult": {"isAsync": True, "agentId": "ag1"}}
        self.assertEqual(self.an([user("go"), launch])["pending"], ["1 agent running"])
        note = user("<task-notification><task-id>ag1</task-id>done</task-notification>")
        self.assertEqual(self.an([user("go"), launch, note])["pending"], [])

    def test_background_bash_and_monitor_and_taskstop(self):
        bg = {"type": "user", "timestamp": iso(time.time() - 60), "toolUseResult": {"backgroundTaskId": "b1"}}
        mon = {"type": "user", "timestamp": iso(time.time() - 60), "toolUseResult": {"taskId": "m1", "timeoutMs": 5}}
        self.assertEqual(self.an([user("x"), bg, mon])["pending"],
                         ["1 background command running", "1 monitor running"])
        stop = assistant("stopping", tools=[{"type": "tool_use", "name": "TaskStop", "input": {"task_id": "b1"}}])
        self.assertEqual(self.an([user("x"), bg, mon, stop])["pending"], ["1 monitor running"])

    def test_old_launch_ignored(self):
        bg = {"type": "user", "timestamp": iso(time.time() - 7 * 3600), "toolUseResult": {"backgroundTaskId": "b1"}}
        self.assertEqual(self.an([user("x"), bg])["pending"], [])

    def test_queue_enqueue_without_dequeue(self):
        enq = {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(time.time() - 30)}
        deq = {"type": "queue-operation", "operation": "dequeue", "timestamp": iso(time.time() - 20)}
        rem = {"type": "queue-operation", "operation": "remove", "timestamp": iso(time.time() - 20)}
        self.assertEqual(self.an([user("x"), enq])["pending"], ["queued result not yet delivered"])
        self.assertEqual(self.an([user("x"), enq, deq])["pending"], [])
        self.assertEqual(self.an([user("x"), enq, rem])["pending"], [])
        old = {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(time.time() - 25 * 3600)}
        self.assertEqual(self.an([user("x"), old])["pending"], [])

    def test_schedule_wakeup(self):
        wake = assistant("later", tools=[{"type": "tool_use", "name": "ScheduleWakeup", "input": {"delaySeconds": 900}}])
        self.assertEqual(self.an([user("x"), wake])["pending"], ["loop wakeup scheduled"])
        past = assistant("later", t=time.time() - 7200,
                         tools=[{"type": "tool_use", "name": "ScheduleWakeup", "input": {"delaySeconds": 60}}])
        self.assertEqual(self.an([user("x"), past])["pending"], [])
        stop = assistant("stop", tools=[{"type": "tool_use", "name": "ScheduleWakeup", "input": {"stop": True}}])
        self.assertEqual(self.an([user("x"), wake, stop])["pending"], [])

    def test_usage_limit_wait(self):
        now = time.time()
        err = {"type": "assistant", "timestamp": iso(now - 60), "isApiErrorMessage": True,
               "error": "rate_limit", "quotaLimits": {"status": "rejected", "resetsAt": now + 3600},
               "message": {"role": "assistant", "content": [{"type": "text", "text": "limit"}]}}
        got = self.an([user("x"), err], now)["pending"]
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].startswith("usage limit wait"))
        # reset passed more than 15 min ago: not pending
        err2 = dict(err, quotaLimits={"status": "rejected", "resetsAt": now - 20 * 60})
        self.assertEqual(self.an([user("x"), err2], now)["pending"], [])
        # within the 15 min grace: pending
        err3 = dict(err, quotaLimits={"status": "rejected", "resetsAt": now - 5 * 60})
        self.assertEqual(len(self.an([user("x"), err3], now)["pending"]), 1)
        # the conversation moved on: not pending
        self.assertEqual(self.an([user("x"), err, user("continue")], now)["pending"], [])

    def test_unreadable_and_unparseable_fail_closed(self):
        self.assertEqual(pending.analyze(os.path.join(self.tmp, "nope.jsonl"))["pending"], ["unreadable"])
        self.assertEqual(pending.analyze("")["pending"], ["unreadable"])
        p = self.transcript("bad", ["garbage", "more garbage", "x"])
        self.assertEqual(pending.analyze(p)["pending"], ["unreadable"])

    def test_half_written_last_line_is_fine(self):
        p = self.transcript("half", [user("x"), assistant("y"), '{"type":"assist'])
        self.assertEqual(pending.analyze(p)["pending"], [])

    def test_titles_and_interrupt_marker(self):
        recs = [user("x"), {"type": "ai-title", "aiTitle": "AI"}, {"type": "custom-title", "customTitle": "Mine"},
                {"type": "ai-title", "aiTitle": "AI2"},
                {"type": "user", "timestamp": iso(time.time()), "message": {"role": "user", "content": [
                    {"type": "text", "text": "[Request interrupted by user]"}]}}]
        a = self.an(recs)
        self.assertEqual(a["title"], "Mine")
        self.assertTrue(pending.interrupted_last(a))
        self.assertFalse(pending.interrupted_last(self.an([user("x"), assistant("y")])))

    def test_R9_no_user_message(self):
        a = self.an([{"type": "summary", "summary": "x"}])
        self.assertFalse(a["has_user"])

    def test_output_keys_match_v1(self):
        p = self.transcript("k", [user("x"), assistant("bye")])
        self.assertEqual(sorted(pending.pending_json(p)), ["last_text", "pending"])
        json.dumps(pending.pending_json(p))

    def test_self_test_passes(self):
        self.assertEqual(pending.self_test(self.tmp), [])
