#!/usr/bin/env python3
"""
run_lock.py - only one test run on the fleet at a time.

A run resets the quorum list of every node it uses and keeps those nodes busy,
so two runs at once would sign each other's transfers with the wrong quorum and
measure each other's load. test_runner.py, smoke_test.py and
custom/unpledged_spend.py take this lock before they touch the fleet and refuse
to start while another run holds it - whether that run was started from a
terminal or from the web UI (controller/ui), which starts test_runner.py itself.

The lock is the operating system's own lock on fleet-run.lock (beside this
file), held for as long as the run's process lives. A run that crashes or is
killed loses it with its process, so a stale lock can never block the next run
and there is nothing to clean up by hand. The file also says who holds it -
script, arguments, pid, start time, log - which is what the refusal message and
the web UI show. That text is information only; the OS lock is what decides.

    run_lock.take("test_runner.py")      # first thing in main(); exits if busy
    run_lock.note(log=log_path)          # add details for the web UI
    run_lock.holder()                    # who holds it now, or None

Linux uses flock (fcntl); Windows, where the web UI's demo runs, uses
msvcrt.locking. Both are only imported inside _lock/_unlock.
"""

import atexit
import datetime
import json
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK_PATH = os.path.join(HERE, "fleet-run.lock")

# Windows locks byte ranges and refuses to READ a locked range, so the lock
# sits on one byte far past the text that names the holder.
_WIN_LOCK_OFFSET = 1 << 30

_HELD = {}      # path -> [fd, holder details]


def _lock(fd):
    """Take the OS lock on an open file without waiting. True if taken."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def _write(fd, info):
    data = (json.dumps(info, indent=2) + "\n").encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, data)


def _read(path):
    """The holder details in the lock file, or None if there are none."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.loads(fh.read() or "null")
    except (IOError, OSError, ValueError):
        return None


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def describe(info):
    """One line naming a holder, for the refusal message and the web UI."""
    if not info:
        return "another run holds the fleet"
    args = " ".join(info.get("args") or [])
    return "{}{} started {} from {} (pid {} on {}) is running".format(
        info.get("script", "a test run"), " " + args if args else "",
        (info.get("started") or "?").replace("T", " "), info.get("by", "a terminal"),
        info.get("pid", "?"), info.get("host", "?"))


def take(script, path=None, by="a terminal", **details):
    """Take the fleet for this process, or stop it with a message naming the
    run that holds it. Released when the process ends, however it ends."""
    path = path or LOCK_PATH
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o664)
    if not _lock(fd):
        os.close(fd)
        sys.exit("ERROR: the fleet is busy - {}. Only one test run at a time: wait for it "
                 "to finish, or stop it where it was started.".format(describe(_read(path))))
    info = {"script": script, "args": sys.argv[1:], "pid": os.getpid(),
            "process": os.path.basename(sys.argv[0] or script),
            "host": socket.gethostname(), "started": _now(), "by": by}
    info.update(dict((k, v) for k, v in details.items() if v not in (None, "")))
    _write(fd, info)
    _HELD[path] = [fd, info]
    atexit.register(release, path)
    return info


def note(path=None, **details):
    """Add details (e.g. the log path) to the lock this process holds."""
    held = _HELD.get(path or LOCK_PATH)
    if held:
        held[1].update(dict((k, v) for k, v in details.items() if v not in (None, "")))
        _write(held[0], held[1])


def release(path=None):
    """Give the fleet back. Runs by itself at exit; a killed process releases
    with its process instead, leaving no "ended" mark - which is how the web UI
    tells a run that finished from one that did not."""
    path = path or LOCK_PATH
    held = _HELD.pop(path, None)
    if not held:
        return
    fd, info = held
    try:
        info["ended"] = _now()
        _write(fd, info)
    except OSError:
        pass
    try:
        _unlock(fd)
    finally:
        os.close(fd)


def pid_alive(pid, process=None):
    """True while process `pid` runs. With `process` (a script name) on Linux,
    also that the pid still belongs to it and was not reused."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = k32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
        k32.CloseHandle(handle)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if process:
        try:
            with open("/proc/{}/cmdline".format(pid), "rb") as fh:
                return process.encode("utf-8") in fh.read()
        except (IOError, OSError):
            pass
    return True


def holder(path=None):
    """Who holds the fleet now: the holder's details, or None when it is free.
    Read from what the holder wrote and whether its process still runs - the
    lock itself is not touched, so asking never gets in a run's way."""
    info = _read(path or LOCK_PATH)
    if not isinstance(info, dict) or info.get("ended"):
        return None
    if not pid_alive(info.get("pid"), info.get("process")):
        return None
    return info
