"""The ONE transcript heuristic: will this session still do work on its own?

No hook covers these, so they are read from the transcript tail (last 2 MB), fail closed:
  - an async Agent (toolUseResult.isAsync + agentId), background Bash (backgroundTaskId) or
    Monitor (taskId + timeoutMs) launched with no matching <task-id>ID</task-id>
    notification or TaskStop yet (launches older than 6 h are ignored);
  - a queue-operation enqueue with no later dequeue / remove (24 h max);
  - a ScheduleWakeup (/loop) whose wake time has not passed;
  - a usage-limit 429 (isApiErrorMessage, error rate_limit, quotaLimits.status rejected,
    quotaLimits.resetsAt) whose reset + 15 min grace has not passed, with no later
    user/assistant record ("Continue automatically at usage limit" waits as idle).
An unreadable or unparseable transcript is pending ["unreadable"].

Output keys match the v1 `cc-pending-work` script: {"pending": [...], "last_text": "..."}.
"""
import calendar
import json
import os
import re
import time

TAIL_BYTES = 2000000
STALE_LAUNCH_S = 6 * 3600
QUEUED_MAX_AGE_S = 24 * 3600
LIMIT_GRACE_S = 15 * 60
WAKE_SLACK_S = 120
TASK_ID = re.compile(rb"<task-id>([^<]+)</task-id>")
INTERRUPTED = "[Request interrupted by user"


def ts_of(d, default):
    t = d.get("timestamp")
    if not isinstance(t, str) or len(t) < 19:
        return default
    try:
        base = calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return default
    frac = re.match(r"\.(\d+)", t[19:])
    return base + (float("0." + frac.group(1)) if frac else 0.0)


def read_tail(path, nbytes=TAIL_BYTES):
    """(lines, truncated). Raises OSError when unreadable."""
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - nbytes))
        if size > nbytes:
            fh.readline()
        return fh.readlines(), size > nbytes


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
                return c["text"]
    return ""


def analyze(path, now=None):
    """Everything the daemon and CLI need from one pass over the tail:
    pending, last_text, last_kind ('user'|'assistant'|None), last_user_text, has_user,
    title (custom-title beats ai-title)."""
    now = time.time() if now is None else now
    res = {"pending": [], "last_text": "", "last_kind": None, "last_user_text": "",
           "has_user": False, "title": None}
    if not path:
        res["pending"] = ["unreadable"]
        return res
    try:
        lines, truncated = read_tail(path)
    except OSError:
        res["pending"] = ["unreadable"]
        return res
    if truncated:
        res["has_user"] = True  # a 2 MB+ transcript had a first message
    launched, finished = {}, set()
    queued = []
    wake_until = 0.0
    limit_reset = 0.0
    parsed = bad = 0
    title = None
    for i, raw in enumerate(lines):
        if not raw.strip():
            continue
        if b"<task-id>" in raw:
            finished.update(m.decode("utf-8", "replace") for m in TASK_ID.findall(raw))
        try:
            d = json.loads(raw)
        except ValueError:
            if i != len(lines) - 1:  # a half-written last line is normal
                bad += 1
            continue
        if not isinstance(d, dict):
            bad += 1
            continue
        parsed += 1
        typ = d.get("type")
        if typ == "queue-operation":
            op = d.get("operation")
            if op == "enqueue":
                queued.append(ts_of(d, now))
            elif op == "dequeue":
                queued = []
            elif op == "remove" and queued:
                queued.pop(0)
            continue
        if typ in ("custom-title", "ai-title"):
            t = d.get("customTitle") or d.get("aiTitle")
            if t and (typ == "custom-title" or not title or title[0] != "custom-title"):
                title = (typ, t)
            continue
        if d.get("isApiErrorMessage") and d.get("error") == "rate_limit":
            q = d.get("quotaLimits") or {}
            if q.get("status") == "rejected" and q.get("resetsAt"):
                try:
                    limit_reset = float(q["resetsAt"])
                except (TypeError, ValueError):
                    limit_reset = now  # unparseable reset: keep for the grace window
                continue
        r = d.get("toolUseResult")
        if isinstance(r, dict):
            if r.get("isAsync") and r.get("agentId"):
                launched[str(r["agentId"])] = ("agent", ts_of(d, now))
            elif r.get("backgroundTaskId"):
                launched[str(r["backgroundTaskId"])] = ("background command", ts_of(d, now))
            elif r.get("taskId") and "timeoutMs" in r:
                launched[str(r["taskId"])] = ("monitor", ts_of(d, now))
        if typ in ("user", "assistant"):
            limit_reset = 0.0
            msg = d.get("message") or {}
            if typ == "user" and not d.get("isSidechain"):
                res["has_user"] = True
                res["last_kind"] = "user"
                res["last_user_text"] = _text_of(msg.get("content"))
            elif typ == "assistant":
                res["last_kind"] = "assistant"
            for c in (msg.get("content") if isinstance(msg.get("content"), list) else []) or []:
                if not isinstance(c, dict):
                    continue
                if typ == "assistant" and c.get("type") == "text" and (c.get("text") or "").strip():
                    res["last_text"] = c["text"].strip()
                if c.get("type") != "tool_use":
                    continue
                inp = c.get("input") or {}
                if c.get("name") == "ScheduleWakeup":
                    if inp.get("stop"):
                        wake_until = 0.0
                    else:
                        try:
                            delay = float(inp.get("delaySeconds") or 0)
                        except (TypeError, ValueError):
                            delay = 0.0
                        wake_until = ts_of(d, now) + delay + WAKE_SLACK_S
                elif c.get("name") == "TaskStop":
                    for k in ("task_id", "taskId", "id"):
                        if inp.get(k):
                            finished.add(str(inp[k]))
    if title:
        res["title"] = title[1]
    if lines and parsed == 0 and bad:
        res["pending"] = ["unreadable"]
        return res
    pending, counts = [], {}
    for tid, (kind, t0) in launched.items():
        if tid in finished or now - t0 > STALE_LAUNCH_S:
            continue
        counts[kind] = counts.get(kind, 0) + 1
    for kind, n in sorted(counts.items()):
        pending.append("%d %s%s running" % (n, kind, "s" if n > 1 else ""))
    queued = [t for t in queued if now - t < QUEUED_MAX_AGE_S]
    if queued:
        pending.append("queued result not yet delivered")
    if wake_until > now:
        pending.append("loop wakeup scheduled")
    if limit_reset and now < limit_reset + LIMIT_GRACE_S:
        pending.append("usage limit wait until %s" % time.strftime("%H:%M", time.localtime(limit_reset)))
    res["pending"] = pending
    return res


def pending_json(path, now=None):
    a = analyze(path, now)
    return {"pending": a["pending"], "last_text": a["last_text"]}


def interrupted_last(a):
    """True when the last user/assistant record is the user's Esc-interrupt marker."""
    return a.get("last_kind") == "user" and (a.get("last_user_text") or "").startswith(INTERRUPTED)


def self_test(tmpdir):
    """Assert the record shapes this module depends on, on a synthetic transcript.
    Returns a list of failures (empty = pass)."""
    now = time.time()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 60))
    recs = [
        {"type": "user", "timestamp": iso, "message": {"role": "user", "content": "hi"}},
        {"type": "user", "timestamp": iso, "toolUseResult": {"isAsync": True, "agentId": "a1"}},
        {"type": "user", "timestamp": iso, "toolUseResult": {"backgroundTaskId": "b1"}},
        {"type": "user", "timestamp": iso, "toolUseResult": {"taskId": "m1", "timeoutMs": 1}},
        {"type": "assistant", "timestamp": iso, "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "ScheduleWakeup", "input": {"delaySeconds": 3600}},
            {"type": "text", "text": "done"}]}},
        {"type": "queue-operation", "operation": "enqueue", "timestamp": iso},
    ]
    p = os.path.join(tmpdir, "selftest.jsonl")
    with open(p, "w") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    got = analyze(p, now)["pending"]
    fails = []
    for want in ("1 agent running", "1 background command running", "1 monitor running",
                 "queued result not yet delivered", "loop wakeup scheduled"):
        if want not in got:
            fails.append("missing %r in %r" % (want, got))
    return fails
