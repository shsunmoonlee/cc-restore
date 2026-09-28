"""Where the installed pieces live, and the one command typed into a tab to resume."""
import os
import shlex

HERE = os.path.dirname(os.path.realpath(__file__))


def _roots():
    # installed: $PREFIX/lib/cc-sessions/ccsessions -> $PREFIX ; checkout: <repo>/lib/ccsessions
    lib_parent = os.path.dirname(HERE)
    if os.path.basename(lib_parent) == "cc-sessions":
        return os.path.dirname(os.path.dirname(lib_parent)), lib_parent
    return os.path.dirname(lib_parent), None


def bin_path(name):
    root, _ = _roots()
    return os.path.join(root, "bin", name)


def daemon_script():
    root, installed_lib = _roots()
    if installed_lib:
        return os.path.join(installed_lib, "cc_sessions_daemon.py")
    return os.path.join(root, "daemon", "cc_sessions_daemon.py")


def tilde(path):
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + "/"):
        rest = path[len(home):]
        if all(c.isalnum() or c in "/._-" for c in rest):
            return "~" + rest
    return shlex.quote(path)


def resume_program(cfg):
    if cfg.get("resume_command"):
        return str(cfg["resume_command"])
    return tilde(bin_path("cc-resume"))


def command(cfg, sid, cwd):
    cmd = "%s %s" % (resume_program(cfg), shlex.quote(sid))
    return cmd + (" --cwd %s" % shlex.quote(cwd) if cwd else "")
