"""Process, tty and memory facts (macOS `ps`, `memory_pressure`, `sysctl`, `lsof`).

Process identity is (pid, start time): a pid alone is reused by the OS, so every check
that asks "is this row's process still running" compares the stored `pid_start` too.
"""
import os
import re
import subprocess

PS_ENV = dict(os.environ, LC_ALL="C", LANG="C")
CLAUDE_COMM = "claude"
SHELL_JOBS = ("zsh", "-zsh", "bash", "-bash", "fish", "-fish", "sh", "-sh", "login")


def _run(args, timeout=10):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=PS_ENV)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def norm_tty(tty):
    """'/dev/ttys004' | 'ttys004' | 's004' -> 'ttys004'; '??' / '' / None -> None."""
    if not tty:
        return None
    t = str(tty).strip()
    if t.startswith("/dev/"):
        t = t[5:]
    if not t or t.startswith("?") or t == "-":
        return None
    if re.match(r"^s\d+$", t):
        t = "tty" + t
    return t


def parse_ps_tree(out):
    """Parse `ps -axo pid=,ppid=,comm=` into {pid: (ppid, comm)}."""
    tree = {}
    for line in (out or "").splitlines():
        p = line.split(None, 2)
        if len(p) < 3:
            continue
        try:
            tree[int(p[0])] = (int(p[1]), p[2].strip())
        except ValueError:
            continue
    return tree


def ps_tree():
    """One `ps -axo pid=,ppid=,comm=` call (~30 ms; asking for lstart for every process
    costs ~140 ms, so start times are read per pid instead)."""
    return parse_ps_tree(_run(["ps", "-axo", "pid=,ppid=,comm="]))


def is_claude_comm(comm):
    return os.path.basename((comm or "").strip()) == CLAUDE_COMM


def find_claude(start_pid, tree, max_steps=6):
    """Walk ppid from start_pid to the first process whose comm basename is exactly
    'claude'. Returns the pid or None."""
    pid, steps = start_pid, 0
    while pid and pid in tree and steps <= max_steps:
        ppid, comm = tree[pid]
        if is_claude_comm(comm):
            return pid
        pid, steps = ppid, steps + 1
    return None


def parse_proc_info(out):
    """`ps -o lstart=,tty=,command= -p PID` -> {pid_start, tty, command} or None."""
    toks = (out or "").strip().split(None, 6)
    if len(toks) < 6:
        return None
    return {"pid_start": " ".join(toks[:5]), "tty": norm_tty(toks[5]),
            "command": toks[6] if len(toks) > 6 else ""}


def proc_info(pid):
    return parse_proc_info(_run(["ps", "-o", "lstart=,tty=,command=", "-p", str(pid)]))


def lstart_comm(pid):
    """(lstart, comm) for a running pid, or None."""
    out = _run(["ps", "-o", "lstart=,comm=", "-p", str(pid)])
    toks = (out or "").strip().split(None, 5)
    if len(toks) < 6:
        return None
    return " ".join(toks[:5]), toks[5]


def is_interactive_command(cmd):
    """False when the claude argv carries -p / --print (a headless one-shot run)."""
    if not cmd:
        return True
    for tok in cmd.split()[1:]:
        if tok in ("-p", "--print") or tok.startswith("--print="):
            return False
    return True


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, OverflowError):
        return False
    return True


def identity_alive(pid, pid_start, lookup=None):
    """True only when pid runs, is claude, and (when known) started at pid_start.
    `lookup(pid) -> (lstart, comm) | None` is injectable for tests."""
    if not pid:
        return False
    pid = int(pid)
    if lookup is None:
        if not pid_alive(pid):
            return False
        lookup = lstart_comm
    ent = lookup(pid)
    if ent is None:
        return False
    lstart, comm = ent
    if not is_claude_comm(comm):
        return False
    if pid_start and lstart != pid_start:
        return False
    return True


def ps_commands():
    """{pid: (ppid, rss_kb, command)} for every process (one ps call)."""
    out = _run(["ps", "-axo", "pid=,ppid=,rss=,command="])
    table = {}
    for line in (out or "").splitlines():
        p = line.split(None, 3)
        if len(p) < 3:
            continue
        try:
            table[int(p[0])] = (int(p[1]), int(p[2]), p[3] if len(p) > 3 else "")
        except ValueError:
            continue
    return table


def working_children(pid, table, helper_patterns):
    """Descendants of pid that count as work. A helper (MCP server, launcher) and its
    whole subtree are ignored; anything else (a Bash tool shell, caffeinate, a dev
    server) is work."""
    kids = {}
    for cpid, ent in table.items():
        kids.setdefault(ent[0], []).append(cpid)
    out, stack, seen = [], list(kids.get(pid, [])), set()
    while stack:
        c = stack.pop()
        if c in seen:
            continue
        seen.add(c)
        cmd = table[c][-1]
        if any(h in cmd for h in helper_patterns):
            continue
        first = (cmd.split() or ["?"])[0]
        if "shell-snapshots" in cmd or os.path.basename(first).lstrip("-") in ("zsh", "bash", "sh", "fish"):
            out.append("shell task")
        else:
            out.append(os.path.basename(first))
        stack.extend(kids.get(c, []))
    return out


def tree_rss_mb(pid, table):
    """RSS of pid plus all descendants, MB."""
    kids = {}
    for cpid, ent in table.items():
        kids.setdefault(ent[0], []).append(cpid)
    total, stack, seen = 0, [pid], set()
    while stack:
        c = stack.pop()
        if c in seen or c not in table:
            continue
        seen.add(c)
        total += table[c][1]
        stack.extend(kids.get(c, []))
    return total / 1024.0


def app_name(cmd):
    m = re.search(r"/([^/]+)\.app/", cmd or "")
    if m:
        return m.group(1)
    first = (cmd or "?").split() or ["?"]
    return os.path.basename(first[0]) or "?"


def top_apps(table, n=5):
    by = {}
    for _, (ppid, rss, cmd) in table.items():
        k = app_name(cmd)
        by[k] = by.get(k, 0) + rss
    return sorted(((k, v / 1024.0) for k, v in by.items()), key=lambda x: -x[1])[:n]


def parse_memory_pressure(out):
    m = re.search(r"free percentage:\s*(\d+)%", out or "")
    return int(m.group(1)) if m else None


def free_pct():
    """System-wide memory free percentage, or None when it cannot be read."""
    return parse_memory_pressure(_run(["memory_pressure"], timeout=15))


def swap_used_mb():
    out = _run(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"used\s*=\s*([\d.]+)M", out or "")
    return float(m.group(1)) if m else None


def parse_boottime(out):
    m = re.search(r"sec\s*=\s*(\d+)", out or "")
    return int(m.group(1)) if m else None


def boot_time():
    return parse_boottime(_run(["sysctl", "-n", "kern.boottime"]))


def cwd_map():
    """{pid: cwd} for this user's processes (one lsof call); {} when unavailable."""
    out = _run(["lsof", "-n", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"], timeout=25)
    m, pid = {}, None
    for line in (out or "").splitlines():
        if line[:1] == "p":
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line[:1] == "n" and pid:
            m[pid] = line[1:]
    return m
