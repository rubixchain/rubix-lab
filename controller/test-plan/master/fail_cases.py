"""fail_cases.py - a node going down in the middle of a transfer.

FAIL-01 kills one participant node while a transfer is in flight and checks the
fleet puts itself right afterwards. Six scenarios, one step each: the sender,
the quorum and the receiver, each killed right after the transfer starts and
again right after consensus. The kill is timed off the product's own Info-level
log lines where one exists (core/transaction.go, core/quorum_initiator.go,
core/pledge_v2.go); the receiver logs nothing early, so its kills are timed by
a short delay.

What Rubix must do when a node dies under a transfer (master/steps.py): the
transfer either goes through - and then reaches the receiver once it is up - or
is refused with a stated reason within the budget; afterwards value is
conserved across sender, receiver and quorum, nothing stays Locked, the killed
node's neighbours on its machine still answer and can still receive (a
shutdown that falls through to `killall -9 ipfs`, core/shutdown_manager.go,
would take every node's IPFS on the machine with it, and the IPFS auto-restart
never registers their p2p listeners again, core/ipfs_recovery.go), and an
ordinary transfer goes through again. The killed node's own restart is
expected and not counted as a crash; any other restart on its machine is.

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
import steps

SKIP = "SKIP"
SSH_USER = "rubix"
HOME = "/home/rubix"
NODE_LOG = HOME + "/" + fleet_inventory.NODES_DIR + "/node{slot}/log.txt"
BUDGET = 120            # a refusal later than this, with a node down, is not gentle
CLIENT_WAIT = 300       # how long the lab waits for any answer at all

# Markers must be Info-level lines: log.txt does not carry Debug. Until
# 2026-10-23 "after consensus" waited for "Transaction succeeded, tokens will
# be transferred", which is c.log.Debug (core/transaction.go:119), so it always
# timed out and killed an idle node 40s later.
MARK_START = "InitiateTransaction: Starting transaction"                 # core/transaction.go:31
MARK_DONE = "InitiateTransaction: Publishing transaction to network"     # core/transaction.go, after consensus
MARK_QUORUM = "Consensus request parsed successfully"                    # core/quorum_initiator.go:206
MARK_PLEDGED = "PledgeV2 complete"                                       # core/pledge_v2.go:332

# step -> (role, log marker, extra seconds after it). An empty marker means
# "just wait that many seconds from dispatch" - used for the receiver.
SCENARIOS = [
    ("S2", "sender", MARK_START, 0.0),
    ("S3", "sender", MARK_DONE, 0.0),
    ("S4", "quorum", MARK_QUORUM, 0.0),
    ("S5", "quorum", MARK_PLEDGED, 0.0),
    ("S6", "receiver", "", 0.4),
    ("S7", "receiver", "", 2.0),
]

_AFTER = ("the transfer either goes through - and reaches B once B is up - or is refused with a "
          "stated reason within {}s; afterwards value conserved across A, B and Q, nothing Locked, "
          "the machine's other nodes still answering and able to receive, and an ordinary transfer "
          "goes through again".format(BUDGET))

PURPOSE = {
    "FAIL-01": "What a transfer, and the fleet around it, are left with when the sender, the quorum "
               "or the receiver dies in the middle of it - and whether Rubix recovers on its own.",
}

STEPS = {
    "FAIL-01": [
        ("S1", "Set-up check",
         "pick A (sender), B (receiver) and Q (quorum) on three machines with nine nodes each; set Q "
         "up and fund it; point A and B at Q",
         "Q set up and funded; A and B sign with Q"),
        ("S2", "Kill the sender right after it starts",
         "A sends 1 RBT to B; A's unit is stopped on its log line 'Starting transaction', then started "
         "again", _AFTER),
        ("S3", "Kill the sender right after consensus",
         "as S2, stopped on 'Publishing transaction to network'", _AFTER),
        ("S4", "Kill the quorum when it takes the request",
         "as S2, Q stopped on 'Consensus request parsed successfully'", _AFTER),
        ("S5", "Kill the quorum right after it pledges",
         "as S2, Q stopped on 'PledgeV2 complete'", _AFTER),
        ("S6", "Kill the receiver right after the transfer starts",
         "as S2, B stopped 0.4s after the transfer is sent", _AFTER),
        ("S7", "Kill the receiver around consensus",
         "as S2, B stopped 2s after the transfer is sent", _AFTER),
    ],
}


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
        return "killed on its log line"
    if marker:
        return "killed on timeout (the log line did not come in {}s)".format(int(max_wait))
    return "killed {}s after the transfer was sent".format(after)


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


def fail_01(ctx, ci):
    run = steps.CaseRun(ctx, "FAIL-01", STEPS["FAIL-01"], budget=BUDGET)
    if not db.available():
        return SKIP, "needs the node databases (psycopg2) to check token state after a kill", ""
    by_machine = {}
    for e in ctx.fleet:
        by_machine.setdefault(fleet_inventory.split(e["host"])[0], []).append(e)
    machines = sorted(by_machine, key=lambda m: -len(by_machine[m]))
    if len(machines) < 3:
        return SKIP, "needs DIDs on at least three machines", ""
    A, B, Q = by_machine[machines[0]][0], by_machine[machines[1]][0], by_machine[machines[2]][0]
    port = ctx.port

    def setup():
        ok, why = steps.quorum_ready(Q, 0, port)
        if not ok:
            return ok, why
        have = rc.get_rbt_balance_detail(Q["host"], Q["did"])[1] or {}
        if have.get("balance", 0) < 50:
            rc.fund_did(Q["host"], Q["did"], 50, port)
        for e in (A, B):
            rc.quorum_reset(e["host"], [Q["did"]], port)
            if (rc.get_rbt_balance_detail(e["host"], e["did"])[1] or {}).get("balance", 0) < 5:
                rc.fund_did(e["host"], e["did"], 5, port)
        for e in (A, B):
            ok, why = steps.wired(e["host"], Q["did"], port)
            if not ok:
                return ok, why
        return True, ""
    s1 = run.step("S1", lambda st: None, nodes=[A, B, Q], watch=False, require=[setup])
    if s1.outcome != steps.PASS:
        return run.verdict()

    def rearm():
        # A quorum's setup lives in memory and goes with a restart
        # (core/quorum_initiator.go); A must have something to send.
        ok, why = steps.quorum_ready(Q, 0, port)
        if ok and (rc.get_rbt_balance_detail(A["host"], A["did"])[1] or {}).get("balance", 0) < 2:
            rc.fund_did(A["host"], A["did"], 3, port)
        return ok, why

    for sid, role, marker, after in SCENARIOS:
        target = {"sender": A, "receiver": B, "quorum": Q}[role]
        machine, _p = fleet_inventory.split(target["host"])
        slot = _slot_of(target["host"])

        def body(st, target=target, machine=machine, slot=slot, marker=marker, after=after):
            result, killer = {}, {}

            def transfer():
                t0 = time.time()
                ok, msg, res = rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=1.0,
                                                       memo="FAIL-01 {}".format(sid), timeout=CLIENT_WAIT)
                result.update(ok=ok, msg=msg, secs=round(time.time() - t0, 2),
                              txid=res.get("transactionID") if isinstance(res, dict) else None)

            def kill():
                killer["how"] = _stop_on_marker(machine, slot, marker, after, max_wait=40)
            k = threading.Thread(target=kill)
            t = threading.Thread(target=transfer)
            k.start()
            time.sleep(0.4)             # let the remote tail attach before the markers appear
            t.start()
            k.join(timeout=70)
            t.join(timeout=CLIENT_WAIT + 20)
            _start(machine, slot)
            back = _wait_up(target["host"])
            st.note(killer.get("how") or "the kill did not report")
            st.check(back, "the killed node did not answer again within 60s of its restart")
            if result:
                st.op(result["ok"], result["msg"], result["secs"], result["txid"], to=B["did"])
            else:
                st.op(False, "no answer at all within {}s".format(CLIENT_WAIT), CLIENT_WAIT)
            time.sleep(ch.SETTLE)
            rc.quorum_setup(Q["host"], Q["did"], port)

        run.step(sid, body, nodes=[A, B, Q], restarts=[target["host"]], require=[rearm],
                 probe=lambda: rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=1.0,
                                                       memo="FAIL-01 recovery", timeout=ch.TRANSFER_TIMEOUT),
                 probe_always=True)
    return run.verdict(passed="the fleet recovered from all six mid-transfer node kills: value "
                              "conserved, nothing Locked, neighbours unharmed")


CASES = {"FAIL-01": fail_01}
ORDER = ["FAIL-01"]
NEEDS = {
    # Self-managed from the fleet; runs last and alone so a slow recovery
    # cannot disturb another case.
    "FAIL-01": {"senders": 3, "receivers": 0, "quorums": 0, "fund": 0,
                "exclusive": True, "last": True},
}
NO_DELAY_RERUN = {"FAIL-01"}
