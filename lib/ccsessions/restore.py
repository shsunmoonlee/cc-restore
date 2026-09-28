"""`cc-sessions restore`: reopen the sessions a crash, power loss or shutdown killed.

Candidates come from the ledger, never from Claude Code's own registry:
  - crash: a row whose process is gone but whose SessionEnd never arrived (ended_at NULL);
  - shutdown cluster: rows that ended with reason 'other' (what a logout/shutdown SIGTERM
    produces) together, right before boot. T = the newest such ended_at before boot, used
    only when T is within 5 min of the newest pre-boot activity; the cluster walks back
    from T while consecutive ended_at values are within 30 s of each other.
A deliberate exit (/exit, Ctrl-D: reason prompt_input_exit, clear, logout...) is never
restored. Hibernated rows are left alone unless --include-hibernated, or --auto finds
their tab gone; those tabs get the resume command typed but not run.
"""
import os
import re
import subprocess
import time

from . import PARKED_STATES, SUPERSEDED, procs, resumecmd

CLUSTER_ANCHOR_S = 300
CLUSTER_GAP_S = 30
BLIND = "hibernated (iTerm2 tabs unknown)"


def shutdown_cluster(rows, boot):
    """Session ids of the pre-boot shutdown cluster (possibly empty)."""
    pre = [r["last_event_at"] for r in rows if r.get("last_event_at") and r["last_event_at"] <= boot]
    others = sorted((r for r in rows if r.get("end_reason") == "other" and r.get("ended_at")
                     and r["ended_at"] <= boot), key=lambda r: -r["ended_at"])
    if not pre or not others:
        return set()
    t = others[0]["ended_at"]
    if max(pre) - t > CLUSTER_ANCHOR_S:
        return set()
    out, prev = set(), t
    for r in others:
        if prev - r["ended_at"] > CLUSTER_GAP_S:
            break
        out.add(r["session_id"])
        prev = r["ended_at"]
    return out


def select(rows, boot, now, cfg, alive_fn, auto=False, include_hibernated=False,
           live_guids=None, isdir=os.path.isdir):
    """-> (run, typed, skipped): run/typed are rows, skipped is [(row, reason)]."""
    cluster = shutdown_cluster(rows, boot) if boot else set()
    max_age = float(cfg.get("restore_max_age_days", 7)) * 86400
    if auto:
        max_age = max(max_age, 30 * 86400)
    run, typed, skipped = [], [], []
    for r in sorted(rows, key=lambda r: r.get("last_event_at") or 0):
        st = r.get("state")
        if st == SUPERSEDED:
            continue
        if r.get("source") not in ("hook", "legacy"):
            skipped.append((r, "source %s" % r.get("source")))
            continue
        if r.get("interactive") == 0:
            skipped.append((r, "non-interactive"))
            continue
        if alive_fn(r):
            skipped.append((r, "already running"))
            continue
        bucket = None
        if st in PARKED_STATES:
            if include_hibernated:
                bucket = typed
            elif not auto:
                skipped.append((r, "hibernated"))
                continue
            elif live_guids is None:
                skipped.append((r, BLIND))
                continue
            elif r.get("restored_at") and boot and r["restored_at"] >= boot \
                    and r.get("iterm_guid") in live_guids:
                skipped.append((r, "hibernated (already retyped into a tab this boot)"))
                continue
            elif r.get("iterm_guid") and r["iterm_guid"] not in live_guids:
                bucket = typed
            else:
                skipped.append((r, "hibernated (tab still open)"))
                continue
        elif r.get("ended_at") is None:
            bucket = run
        elif r.get("end_reason") == "other" and r["session_id"] in cluster:
            bucket = run
        else:
            skipped.append((r, "exited (%s)" % r.get("end_reason")))
            continue
        last = r.get("last_event_at") or 0
        if auto and boot and last >= boot:
            skipped.append((r, "active after boot"))
            continue
        if now - last > max_age:
            skipped.append((r, "older than %dd" % (max_age / 86400)))
            continue
        if not r.get("cwd") or not isdir(r["cwd"]):
            skipped.append((r, "cwd gone: %s" % r.get("cwd")))
            continue
        bucket.append(r)
    return run, typed, skipped


def osascript_for(tabs):
    """tabs: [(command, run_it)] -> AppleScript opening one window, one tab each."""
    lines = ['set o to ""', 'tell application "iTerm2"', "activate",
             "set w to (create window with default profile)", "set first_done to false"]
    for cmd, run_it in tabs:
        esc = cmd.replace("\\", "\\\\").replace('"', '\\"')
        wt = 'write text "%s"%s' % (esc, "" if run_it else " newline NO")
        lines += ["if first_done then",
                  "tell w to set t to (create tab with default profile)",
                  "set s to current session of t",
                  "else",
                  "set s to current session of w",
                  "set first_done to true",
                  "end if",
                  "tell s to " + wt,
                  "set o to o & (id of s) & linefeed",
                  "delay 0.4"]
    lines += ["end tell", "return o"]
    return lines


GUID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{7,}$")


def valid_guids(stdout, n):
    """Exactly n distinct, well-formed session ids (one per created tab), else None."""
    ids = [l.strip() for l in (stdout or "").splitlines() if l.strip()]
    if len(ids) != n or len(set(ids)) != n or not all(GUID_RE.match(i) for i in ids):
        return None
    return ids


def iterm_guids():
    """Set of live iTerm2 session GUIDs, or None when iTerm2 cannot be asked."""
    if subprocess.run(["pgrep", "-xq", "iTerm2"]).returncode != 0:
        return None
    script = ('tell application "iTerm2"\nset o to ""\nrepeat with w in windows\n'
              'repeat with t in tabs of w\nrepeat with s in sessions of t\n'
              'set o to o & (id of s) & linefeed\nend repeat\nend repeat\nend repeat\n'
              'return o\nend tell')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    return {l.strip() for l in r.stdout.splitlines() if l.strip()}


def run(cfg, conn, auto=False, dry_run=False, limit=None, include_hibernated=False, out=print):
    from .ledger import all_rows
    limit = limit or int(cfg.get("restore_limit", 40))
    boot = procs.boot_time()
    mark = cfg.path("bootmark")
    if auto and not dry_run and boot:
        try:
            with open(mark) as fh:
                if fh.read().strip() == str(boot):
                    out("auto: already restored for this boot")
                    return 0
        except OSError:
            pass

    def write_mark():
        if auto and not dry_run and boot:
            os.makedirs(os.path.dirname(mark), exist_ok=True)
            with open(mark, "w") as fh:
                fh.write(str(boot))

    alive = lambda r: procs.identity_alive(r.get("pid"), r.get("pid_start"))
    guids = iterm_guids() if auto else None
    runs, typed, skipped = select(all_rows(conn), boot, time.time(), cfg, alive, auto=auto,
                                  include_hibernated=include_hibernated, live_guids=guids)
    blind = auto and any(why == BLIND for _, why in skipped)
    if blind:
        out("iTerm2 tabs could not be listed: hibernated sessions skipped; the boot marker "
            "is not written so the next run retries")
    for r, why in skipped:
        if why != "already running":
            out("  skip %s: %s" % (r["session_id"][:8], why))
    tabs = [(resumecmd.command(cfg, r["session_id"], r["cwd"]), True) for r in runs]
    tabs += [(resumecmd.command(cfg, r["session_id"], r["cwd"]), False) for r in typed]
    if not tabs:
        if not blind:
            write_mark()
        out("nothing to restore.")
        return 0
    if len(tabs) > limit:
        out("%d sessions exceeds --limit %d; raise it if intentional." % (len(tabs), limit))
        return 1
    for r in runs:
        out("  open  %s  %s  %s" % (r["session_id"][:8], r["cwd"], (r.get("title") or "")[:50]))
    for r in typed:
        out("  typed %s  %s  %s" % (r["session_id"][:8], r["cwd"], (r.get("title") or "")[:50]))
    if dry_run:
        out("dry-run: would open %d tabs." % len(tabs))
        return 0
    args = ["osascript"]
    for l in osascript_for(tabs):
        args += ["-e", l]
    r = subprocess.run(args, check=True, capture_output=True, text=True)
    guids = valid_guids(r.stdout, len(tabs))
    if guids is None:
        out("iTerm2 did not return one session id per new tab; typed rows stay retryable and "
            "the boot marker is not written")
        return 0
    from .ledger import mark_restored
    for i, row in enumerate(typed):
        mark_restored(conn, row["session_id"], guids[len(runs) + i])
    if not blind:
        write_mark()
    out("opened %d tabs." % len(tabs))
    return 0
