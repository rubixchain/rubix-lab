"""race_cases.py - genuine races the product CI (a 3-node localnet) cannot run.

RACE-01  one sender with funds for exactly one transfer fires the same amount
         to many receivers at the same instant. The product must let exactly
         one through; two would be a double spend.

RACE-02  many DIDs on one node, then many across one machine, all transferring
         at the same instant. The quorums are sized from QCAP-01 so the quorum
         is never the bottleneck - what is measured is node- and machine-level
         contention (the Postgres pool, the peer-port pools, one IPFS per node).

Both wire their own quorums, because a transfer is signed by the first quorum
in the SENDING node's list and these cases point many nodes at once.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc
import db_client as db

SKIP = "SKIP"
DEFAULT_CAP = 50            # one quorum's assumed capacity before QCAP-01 has run


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def _by_host(fleet):
    out = {}
    for e in fleet:
        out.setdefault(e["host"], []).append(e["did"])
    return out


def _machine(host):
    return host.rsplit(":", 1)[0]


# ---------------------------------------------------------------------------
# RACE-01 - one sender, many receivers, one token
# ---------------------------------------------------------------------------

def race_01(ctx, ci):
    recv = ctx.receivers
    n = min(len(recv), int(getattr(ctx.args, "race_receivers", 20)))
    if not ctx.senders or n < 2:
        return SKIP, "needs one sender and at least two receivers", ""
    S = ctx.senders[0]
    port = ctx.port

    # Leave S holding exactly one whole token: drain it, then fund 1 RBT. The
    # sender's node already points at its quorum (prepare_unit).
    import wallet_shapes as ws
    ws.drain_to(ctx, S, recv[0], keep=0.0)
    have = _free(S["host"], S["did"]) or 0.0
    if have < 1.0:
        ok, msg = rc.fund_did(S["host"], S["did"], 1.0 - have, port)
        if not ok:
            return SKIP, "could not fund the sender to one token: " + str(msg), ""
    time.sleep(ch.SETTLE)
    before = _free(S["host"], S["did"]) or 0.0

    # Fire 1 RBT to n receivers at the same instant.
    jobs = [{"host": S["host"], "did": S["did"], "to": recv[i]["did"], "rbt": 1.0,
             "memo": "RACE-01 double-spend attempt"} for i in range(n)]
    results = ch.burst(jobs)
    wins = [i for i, r in enumerate(results) if r["ok"]]
    time.sleep(ch.SETTLE)
    after = _free(S["host"], S["did"]) or 0.0

    credited = 0
    for i in wins:
        ok, _c = rc.wait_for_balance(recv[i]["host"], recv[i]["did"],
                                     (_free(recv[i]["host"], recv[i]["did"]) or 0.0), port, attempts=1)
    # Count receivers that actually show the credit.
    got = sum(1 for i in wins
              if (_free(recv[i]["host"], recv[i]["did"]) or 0.0) >= 1.0 - ch.TOL)
    spent = round(before - after, 3)
    note = "{} simultaneous sends of 1 RBT from a one-token wallet: {} accepted, {} credited; " \
           "wallet {:.3f} -> {:.3f}".format(n, len(wins), got, before, after)
    if len(wins) > 1:
        return False, "DOUBLE SPEND: {} of {} simultaneous sends were accepted from a wallet " \
            "holding one token".format(len(wins), n), note
    if spent > 1.0 + ch.TOL:
        return False, "the one-token wallet lost {:.3f} RBT to {} simultaneous sends".format(
            spent, n), note
    if len(wins) == 0:
        return False, "none of the {} simultaneous sends went through - the single token was " \
            "never spent".format(n), note
    return True, "exactly one of {} simultaneous sends was accepted; no double spend".format(n), note


# ---------------------------------------------------------------------------
# RACE-02 - node-level then machine-level concurrency
# ---------------------------------------------------------------------------

def _setup_quorum(did, host, floor, port):
    ok, msg = rc.quorum_setup(host, did, port)
    if not ok:
        return False, "setup: " + str(msg)
    have = _free(host, did) or 0.0
    if have < floor:
        ok, msg = rc.fund_did(host, did, floor - have, port)
        if not ok:
            return False, "fund: " + str(msg)
    return True, ""


def _fund_all(entries, target, port):
    def one(e):
        have = _free(e["host"], e["did"]) or 0.0
        if have >= target:
            return True
        ok, _m = rc.fund_did(e["host"], e["did"], target - have, port)
        return ok
    with ThreadPoolExecutor(max_workers=min(16, len(entries))) as ex:
        return all(ex.map(one, entries))


def _concurrent_sends(senders, receivers, port):
    """Every sender pays the next in a ring, all at the same instant."""
    pairs = ch.ring(senders) if len(senders) > 1 else []
    jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": 1.0,
             "memo": "RACE-02"} for a, b in pairs]
    return ch.summarize(ch.burst(jobs))


def race_02(ctx, ci):
    fleet = ctx.fleet
    if not fleet:
        return SKIP, "needs the fleet DID list", ""
    port = ctx.args.port
    cap = ch.quorum_capacity(DEFAULT_CAP)
    by_host = _by_host(fleet)
    lines, problems = [], []

    # A quorum DID for signing, taken from a node we are not loading. Several,
    # so no quorum is asked to carry more than its measured capacity.
    def quorum_pool(exclude_machines, how_many):
        picked = []
        for host, dids in by_host.items():
            if _machine(host) in exclude_machines or not dids:
                continue
            picked.append({"host": host, "did": dids[0]})
            if len(picked) >= how_many:
                break
        return picked

    # --- node level: the DIDs on one node, all at once, through ONE quorum ---
    node_host = max(by_host, key=lambda h: len(by_host[h]))
    node_dids = by_host[node_host][:10]
    if len(node_dids) >= 2:
        qs = quorum_pool({_machine(node_host)}, 1)
        if not qs:
            problems.append("no off-machine quorum for the node-level run")
        else:
            q = qs[0]
            ok, why = _setup_quorum(q["did"], q["host"], 200, port)
            if not ok:
                problems.append("node-level quorum setup failed: " + why)
            else:
                rc.quorum_reset(node_host, [q["did"]], port)
                senders = [{"host": node_host, "did": d} for d in node_dids]
                _fund_all(senders, 3, port)
                s = _concurrent_sends(senders, senders, port)
                lines.append("node ({} DIDs, one quorum, capacity {}): {}/{} at once, p95 {:.1f}s{}".format(
                    len(node_dids), cap, s["ok"], s["n"], s["p95"],
                    " - refused: " + s["reasons"] if s["failed"] else ""))
    else:
        lines.append("node level: no node holds two or more DIDs")

    time.sleep(ch.SETTLE)

    # --- machine level: every DID across one machine, through enough quorums --
    target_machine = _machine(node_host)
    machine_hosts = [h for h in by_host if _machine(h) == target_machine]
    machine_dids = [{"host": h, "did": d} for h in machine_hosts for d in by_host[h]][:38]
    if len(machine_dids) >= 2 and len(machine_hosts) >= 1:
        need_q = ch.quorums_for(len(machine_hosts), cap)
        qs = quorum_pool({target_machine}, need_q)
        if not qs:
            problems.append("no off-machine quorums for the machine-level run")
        else:
            ok_q = True
            for q in qs:
                ok, why = _setup_quorum(q["did"], q["host"], 200, port)
                ok_q = ok_q and ok
            if not ok_q:
                problems.append("machine-level quorum setup failed")
            else:
                # Each node on the machine points at one of the quorums.
                for i, h in enumerate(machine_hosts):
                    rc.quorum_reset(h, [qs[i % len(qs)]["did"]], port)
                _fund_all(machine_dids, 3, port)
                s = _concurrent_sends(machine_dids, machine_dids, port)
                lines.append("machine ({} DIDs on {} nodes, {} quorums): {}/{} at once, p95 {:.1f}s{}".format(
                    len(machine_dids), len(machine_hosts), len(qs), s["ok"], s["n"], s["p95"],
                    " - refused: " + s["reasons"] if s["failed"] else ""))
    else:
        lines.append("machine level: not enough DIDs on one machine")

    note = " | ".join(lines)
    if problems:
        return False, "; ".join(problems[:3]), note
    return True, "node- and machine-level concurrency measured (quorums sized from QCAP-01)", note


CASES = {"RACE-01": race_01, "RACE-02": race_02}
ORDER = ["RACE-01", "RACE-02"]
NEEDS = {
    "RACE-01": {"senders": 1, "receivers": 24, "quorums": 1, "fund": 0, "quorum_fund": 100},
    # Exclusive: it grabs a whole node and a whole machine from the fleet and
    # wires their quorums, so nothing else may be using those nodes.
    "RACE-02": {"senders": 2, "receivers": 0, "quorums": 0, "fund": 0, "exclusive": True},
}
NO_DELAY_RERUN = {"RACE-01", "RACE-02"}
