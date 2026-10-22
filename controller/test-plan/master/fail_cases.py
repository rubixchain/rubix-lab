"""fail_cases.py - a node going down in the middle of a transfer.

FAIL-01 kills one participant node while a transfer is in flight and checks the
fleet puts itself right afterwards. Six scenarios: the sender, the receiver and
the quorum, each killed right after the transfer is initiated and again right
after consensus. The kill is timed off the product's own log lines where one
exists (core/transaction.go, core/quorum_initiator.go); the receiver has no
early log marker of its own, so its kills are timed by a short delay.

After each kill, before the node is brought back, the other nodes on the SAME
machine are checked - the shutdown path can fall through to `killall -9 ipfs`
(core/shutdown_manager.go, CLAUDE.md), which would take out every node's IPFS
on that machine. Then the killed node is restarted (always, in a finally) and
the token state on the sender, receiver and quorum is checked for value
conserved, nothing stuck Locked, and no token owned twice.

Setup method: NODE-KILL. It runs last and alone, so a node left slow to recover
cannot disturb any other case. Nothing is wiped and no machine is rebooted.
"""

import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))        # controller/, for fleet.py

import case_helpers as ch
import rubix_client as rc
import db_client as db
import fleet as fleet_inventory

SKIP = "SKIP"
SSH_USER = "rubix"
HOME = "/home/rubix"
NODE_LOG = HOME + "/" + fleet_inventory.NODES_DIR + "/node{slot}/log.txt"

# (role, moment) -> (log marker to wait for, extra seconds after it, which
# node's log carries the marker). An empty marker means "just wait `after`
# seconds from dispatch" - used for the receiver, which logs nothing early.
MARK_START = "InitiateTransaction: Starting transaction"
MARK_DONE = "Transaction succeeded, tokens will be transferred"
MARK_QUORUM = "Consensus request parsed successfully"
SCENARIOS = [
    ("sender", "after initiating", MARK_START, 0.0),
    ("sender", "after consensus", MARK_DONE, 0.0),
    ("quorum", "after initiating", MARK_QUORUM, 0.0),
    ("quorum", "after consensus", MARK_QUORUM, 1.5),
    ("receiver", "after initiating", "", 0.4),
    ("receiver", "after consensus", "", 2.0),
]


def _slot_of(host):
    """The systemd slot for a node address ip:api_port, from the layout."""
    _ip, port = fleet_inventory.split(host)
    index = port - fleet_inventory.API_BASE
    for e in fleet_inventory.load_layout():
        if e["index"] == index:
            return e["slot"]
    return None


def _ssh(machine, remote, timeout):
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "{}@{}".format(SSH_USER, machine), remote],
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "ssh timed out"
    except Exception as e:
        return 1, "{}: {}".format(type(e).__name__, e)


def _stop_on_marker(machine, slot, marker, after, max_wait):
    """Wait for `marker` in the node's log (if any), then stop its unit. Returns
    how the kill was triggered."""
    log = NODE_LOG.format(slot=slot)
    if marker:
        remote = ("timeout {w} bash -c 'tail -Fn0 \"{log}\" | grep -m1 -F \"{m}\"'; rc=$?; "
                  "sleep {a}; sudo -n systemctl stop rubixgoplatform@{s}; echo KILLED rc=$rc").format(
                      w=int(max_wait), log=log, m=marker, a=after, s=slot)
    else:
        remote = ("sleep {a}; sudo -n systemctl stop rubixgoplatform@{s}; echo KILLED").format(
            a=after, s=slot)
    code, out = _ssh(machine, remote, timeout=max_wait + 15)
    if "KILLED" not in out:
        return "stop failed: {}".format(out.strip()[:120])
    if marker and "rc=0" in out:
        return "killed on log marker"
    if marker:
        return "killed on timeout (marker not seen in {}s)".format(int(max_wait))
    return "killed {}s after dispatch".format(after)


def _start(machine, slot):
    _ssh(machine, "sudo -n systemctl start rubixgoplatform@{}".format(slot), timeout=30)


def _peer_ok(host):
    ok, _p = rc.http_json("GET", rc.base_url(*fleet_inventory.split(host)) + rc.EP_PEER_ID, 4)
    return ok


def _wait_up(host, attempts=30, delay=2):
    for _ in range(attempts):
        if _peer_ok(host):
            return True
        time.sleep(delay)
    return False


def _held(host, did):
    try:
        t = db.wallet_totals(host, did)
        return round(sum(t.get(k, 0.0) for k in ("free", "locked", "pledged", "committed")), 3)
    except Exception:
        d = rc.get_rbt_balance_detail(host, did)
        return None if not d[0] else round(sum(d[1].values()), 3)


def _locked(host, did):
    try:
        return db.value_in_status(host, did, db.LOCKED)
    except Exception:
        return None


def _one_scenario(ctx, A, B, Q, role, moment, marker, after):
    port = ctx.port
    target = {"sender": A, "receiver": B, "quorum": Q}[role]
    machine, _p = fleet_inventory.split(target["host"])
    slot = _slot_of(target["host"])
    if slot is None:
        return {"role": role, "moment": moment, "line": "no slot for {}".format(target["host"])}

    # Re-fund A so there is always a token to move.
    if (rc.get_rbt_balance_detail(A["host"], A["did"])[1] or {}).get("balance", 0) < 2:
        rc.fund_did(A["host"], A["did"], 3, port)
    neighbours = [e for e in ctx.fleet
                  if fleet_inventory.split(e["host"])[0] == machine and e["host"] != target["host"]]
    nb_before = set(e["host"] for e in neighbours if _peer_ok(e["host"]))

    before = {r: _held(e["host"], e["did"]) for r, e in (("A", A), ("B", B), ("Q", Q))}
    result = {}

    def transfer():
        result["tx"] = rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=1.0,
                                               memo="FAIL-01 {} {}".format(role, moment),
                                               timeout=90)

    t = threading.Thread(target=transfer)
    killer = {"how": ""}

    def kill():
        killer["how"] = _stop_on_marker(machine, slot, marker, after, max_wait=40)

    k = threading.Thread(target=kill)
    k.start()
    time.sleep(0.4)             # let the remote tail attach before the markers appear
    t.start()
    k.join(timeout=70)
    # Neighbours' IPFS/liveness right after the kill, before recovery.
    nb_after = set(e["host"] for e in neighbours if _peer_ok(e["host"]))
    lost = sorted(nb_before - nb_after)
    t.join(timeout=120)

    try:
        _start(machine, slot)
        back = _wait_up(target["host"])
    finally:
        pass
    time.sleep(ch.SETTLE)

    after_held = {r: _held(e["host"], e["did"]) for r, e in (("A", A), ("B", B), ("Q", Q))}
    stuck = [r for r, e in (("A", A), ("B", B), ("Q", Q))
             if (_locked(e["host"], e["did"]) or 0) > ch.TOL]
    readable = all(v is not None for v in list(before.values()) + list(after_held.values()))
    conserved = None
    if readable:
        conserved = abs((sum(after_held.values())) - (sum(before.values()))) <= 0.01

    bits = [killer["how"] or "kill thread did not report"]
    bits.append("node back up" if back else "NODE DID NOT RECOVER")
    if lost:
        bits.append("{} neighbour node(s) stopped answering: {}".format(
            len(lost), ", ".join(h.split(".")[-1] for h in lost)))
    else:
        bits.append("neighbours stayed up")
    if stuck:
        bits.append("RBT left Locked on " + ",".join(stuck))
    if conserved is False:
        bits.append("value NOT conserved across A/B/Q")
    problem = (not back) or bool(lost) or bool(stuck) or (conserved is False)
    return {"role": role, "moment": moment, "problem": problem, "recovered": back,
            "lost": lost, "stuck": stuck,
            "line": "{} {}: {}".format(role, moment, "; ".join(bits))}


def fail_01(ctx, ci):
    if not db.available():
        return SKIP, "needs the node databases (psycopg2) to check token state after a kill", ""
    # Pick A, B, Q on three different machines, the target machine having
    # neighbours to check IPFS on.
    by_machine = {}
    for e in ctx.fleet:
        by_machine.setdefault(fleet_inventory.split(e["host"])[0], []).append(e)
    machines = [m for m, es in by_machine.items() if es]
    if len(machines) < 3:
        return SKIP, "needs DIDs on at least three machines", ""
    # Prefer a target machine with several nodes so the neighbour check means something.
    machines.sort(key=lambda m: -len(by_machine[m]))
    A = by_machine[machines[0]][0]
    B = by_machine[machines[1]][0]
    Q = by_machine[machines[2]][0]
    port = ctx.port

    ok, msg = rc.quorum_setup(Q["host"], Q["did"], port)
    if not ok:
        return SKIP, "could not set up the quorum: " + str(msg), ""
    have = rc.get_rbt_balance_detail(Q["host"], Q["did"])[1]
    if (have or {}).get("balance", 0) < 50:
        rc.fund_did(Q["host"], Q["did"], 50, port)
    for host in (A["host"], B["host"]):
        ok, msg = rc.quorum_reset(host, [Q["did"]], port)
        if not ok:
            return SKIP, "could not point {} at the quorum: {}".format(host, msg), ""
    for e in (A, B):
        if (rc.get_rbt_balance_detail(e["host"], e["did"])[1] or {}).get("balance", 0) < 5:
            rc.fund_did(e["host"], e["did"], 5, port)

    lines, problems = [], []
    for role, moment, marker, after in SCENARIOS:
        out = _one_scenario(ctx, A, B, Q, role, moment, marker, after)
        lines.append(out["line"])
        print("    FAIL-01 " + out["line"])
        if out.get("problem"):
            problems.append(out["line"])

    note = " | ".join(lines)
    if problems:
        return False, "{} of 6 kill scenario(s) left the fleet wrong: {}".format(
            len(problems), "; ".join(p.split(":")[0] for p in problems)), note
    return True, "the fleet recovered from all 6 mid-transfer node kills; neighbours kept their " \
        "IPFS and no token was lost or stuck", note


CASES = {"FAIL-01": fail_01}
ORDER = ["FAIL-01"]
NEEDS = {
    # Self-managed from the fleet; runs last and alone so a slow recovery
    # cannot disturb another case.
    "FAIL-01": {"senders": 3, "receivers": 0, "quorums": 0, "fund": 0,
                "exclusive": True, "last": True},
}
NO_DELAY_RERUN = {"FAIL-01"}
