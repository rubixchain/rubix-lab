"""proc.py - starting and stopping a run's process, the same way on Linux (the
controller) and Windows (the demo on a laptop).

A run gets its own process group / session, so stopping the UI (Ctrl+C, or a
service restart) does not stop a test run - the UI picks it up again when it
comes back (runs.py). Stop in the UI ends the whole group.
"""

import os
import subprocess
import time

import run_lock


def spawn(argv, cwd, out_path):
    """Start argv with stdout and stderr appended to out_path. Returns the Popen."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    with open(out_path, "ab") as out:
        return subprocess.Popen(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, **kw)


def terminate(pid, popen=None, grace=10):
    """End process `pid` and everything it started. SIGTERM to its group first,
    SIGKILL after `grace` seconds; on Windows the whole tree at once. `popen`,
    when this UI started it, is how its exit is seen (a finished child stays a
    zombie, which still answers to its pid, until it is waited for)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    import signal

    def alive():
        return popen.poll() is None if popen is not None else run_lock.pid_alive(pid)
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.time() + grace
    while time.time() < deadline and alive():
        time.sleep(0.3)
    if alive():
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
