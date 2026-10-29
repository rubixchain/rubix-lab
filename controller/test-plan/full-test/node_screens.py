#!/usr/bin/env python3
"""
node_screens.py - each lab machine's screen follows the node a run is using.

Every machine with nodes shows the node console (systems/console/
rubix-console.sh): a table of its nodes, refreshed every 5 seconds. While a
run uses one of the machine's nodes, the console shows that node's live log
instead - the first of them, where the run uses several there at once - and
goes back to the table when the run is done with them.

The console learns it from one file on the machine,
~/Desktop/rubix/rubix-lab/nodes/in-test: a line "slot role unit" per node in
use, written here over SSH and removed once the machine has none left. The
console reads it every second. A run that dies cannot remove it, so every file
is rewritten each REFRESH seconds and the console ignores one older than 10
minutes.

In use means:
  - the faucet, its quorum and the fullnodes, for the whole run - every
    funding and every transaction goes through them;
  - a unit's quorums, senders and receivers, from when it takes them until it
    gives them back (test_runner.run_units: the span its nodes are busy);
  - for an exclusive unit, also every node it sends a transaction from
    (rubix_client.OPS): LOAD-01, RACE-02 and FAIL-01 drive fleet DIDs beyond
    their participants.

Off until begin(): validate_cases.py drives the real scheduler over a fake
pool and must reach no machine. Nothing here can fail or slow a run - each
write is its own background SSH call, and a machine that cannot be reached
keeps its table.
"""

import atexit
import subprocess
import threading
import time

SSH_USER = "rubix"
FILE = "~/Desktop/rubix/rubix-lab/nodes/in-test"
REFRESH = 120               # seconds between rewrites of every file held
OPS_POLL = 2                # seconds between looks at an exclusive unit's transactions
SSH_TIMEOUT = 20
WHOLE_RUN = "the whole run"

_LOCK = threading.Lock()
_ON = {"on": False}
_IN = {}                    # machine -> {slot: (role, unit)}
_HELD = {}                  # unit -> {(machine, slot)}
_WATCHING = set()           # exclusive units whose transactions add nodes
_TOUCHED = set()            # machines written to this run
_PENDING = []               # writes still running
_MLOCKS = {}                # machine -> lock: one write at a time per machine
_SEM = threading.BoundedSemaphore(16)
_SLOTS = {}                 # api port -> slot (systems/node-layout.txt)
_STOP = threading.Event()


def _where(host):
    """(machine, slot) of a node address ip:port, or of a bare ip (slot 0)."""
    import fleet as fleet_inventory
    if not _SLOTS:
        for e in fleet_inventory.load_layout():
            _SLOTS[fleet_inventory.API_BASE + e["index"]] = e["slot"]
    machine, port = fleet_inventory.split(host)
    return machine, _SLOTS.get(port, 0)


def begin():
    """This run uses the screens from now on: the faucet pair and the fullnodes
    for the whole run."""
    import rubix_client as rc
    with _LOCK:
        if _ON["on"]:
            return
        _ON["on"] = True
    fixed = [(rc.FAUCET_HOST, "faucet"), (rc.FAUCET_QUORUM_HOST, "faucet-quorum")]
    fixed += [(h, "fullnode") for h in rc.FULLNODE_HOSTS]
    _add(WHOLE_RUN, [(h, role) for h, role in fixed if h])
    threading.Thread(target=_refresh, daemon=True).start()
    atexit.register(end)


def use(name, quorums=(), senders=(), receivers=()):
    """`name` uses these nodes (entries with "host") until release(name). A node
    in several roles shows the first: quorum, sender, receiver."""
    _add(name, [(e["host"], "quorum") for e in quorums] +
               [(e["host"], "sender") for e in senders] +
               [(e["host"], "receiver") for e in receivers])


def release(name):
    changed = set()
    with _LOCK:
        _WATCHING.discard(name)
        for m, slot in _HELD.pop(name, ()):
            here = _IN.get(m) or {}
            if slot in here and here[slot][1] == name:
                del here[slot]
                changed.add(m)
    for m in changed:
        _write(m)


def take(unit):
    """test_runner: a unit has taken its nodes."""
    if not _ON["on"]:
        return
    use(unit.name, unit.quorums, unit.senders, unit.receivers)
    if unit.exclusive:
        import rubix_client as rc
        with _LOCK:
            _WATCHING.add(unit.name)
        threading.Thread(target=_follow_ops, args=(unit.name, len(rc.OPS)), daemon=True).start()


def give(unit):
    """test_runner: a unit has given its nodes back."""
    release(unit.name)


def end():
    """The run is over: every screen back to its table. Waits up to
    SSH_TIMEOUT for the last writes."""
    with _LOCK:
        if not _ON["on"]:
            return
        _ON["on"] = False
        _IN.clear()
        _HELD.clear()
        _WATCHING.clear()
        machines = sorted(_TOUCHED)
        pending = list(_PENDING)
    _STOP.set()
    deadline = time.time() + SSH_TIMEOUT
    for t in pending:
        t.join(max(0, deadline - time.time()))
    threads = [threading.Thread(target=_push, args=(m,), daemon=True) for m in machines]
    for t in threads:
        t.start()
    deadline = time.time() + SSH_TIMEOUT + 5
    for t in threads:
        t.join(max(0, deadline - time.time()))


def _add(name, pairs, live=False):
    changed = set()
    with _LOCK:
        if not _ON["on"] or (live and name not in _WATCHING):
            return
        held = _HELD.setdefault(name, set())
        for host, role in pairs:
            m, slot = _where(host)
            here = _IN.setdefault(m, {})
            if slot in here:
                continue
            here[slot] = (role, name)
            held.add((m, slot))
            changed.add(m)
    for m in changed:
        _write(m)


def _follow_ops(name, seen):
    """While exclusive unit `name` runs, the nodes it sends from are in use too."""
    import rubix_client as rc
    while not _STOP.is_set():
        with _LOCK:
            if name not in _WATCHING:
                return
        ops = rc.OPS
        if len(ops) < seen:
            seen = 0
        new = ops[seen:]
        seen += len(new)
        hosts = sorted(set(o.get("host") for o in new if o.get("host")))
        if hosts:
            _add(name, [(h, "sender") for h in hosts], live=True)
        time.sleep(OPS_POLL)


def _refresh():
    while not _STOP.wait(REFRESH):
        with _LOCK:
            machines = [m for m, here in _IN.items() if here]
        for m in machines:
            _write(m)


def _write(machine):
    t = threading.Thread(target=_push, args=(machine,), daemon=True)
    with _LOCK:
        _PENDING[:] = [p for p in _PENDING if p.is_alive()]
        _PENDING.append(t)
        _TOUCHED.add(machine)
    t.start()


def _push(machine):
    """Write the machine's file as it stands NOW - so of several writes queued
    for one machine, the last always leaves the latest state."""
    with _LOCK:
        lock = _MLOCKS.setdefault(machine, threading.Lock())
    with lock, _SEM:
        with _LOCK:
            here = dict(_IN.get(machine) or {})
        text = "".join("{} {} {}\n".format(slot, role, unit)
                       for slot, (role, unit) in sorted(here.items()))
        remote = ("cat > {0}.new && mv {0}.new {0}".format(FILE) if text
                  else "rm -f {}".format(FILE))
        try:
            subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "{}@{}".format(SSH_USER, machine), remote],
                           input=text, capture_output=True, text=True, timeout=SSH_TIMEOUT)
        except Exception:
            pass
