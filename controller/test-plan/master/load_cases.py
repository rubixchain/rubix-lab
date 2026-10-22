"""load_cases.py - the whole fleet transacting at once.

LOAD-01 climbs a ladder of how many DIDs transact at the same instant, up to
every DID in the pool. It is the headline scale test: the product's CI runs
three nodes, this runs hundreds of wallets through tens of quorums at once.

The quorums are sized from QCAP-01 (how many transfers one quorum signs at
once), so the quorum is never the bottleneck - what the ladder finds is where
the NETWORK and the nodes themselves start to drop transfers. The runner's own
database and fullnode checks wrap the case, so a rung that passed the API but
lost rows, or that the fullnodes fell behind on, still shows.

It is exclusive and self-managed: it wires every pool node at one of its
quorums and drives DIDs straight from ctx.fleet, because the ladder's top rung
uses more DIDs than there are nodes (nodes hold several DIDs each).
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

SKIP = "SKIP"
DEFAULT_CAP = 50
DEFAULT_LADDER = "10,20,50,100,200,300,500,750,988"


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def _machine(host):
    return host.rsplit(":", 1)[0]


def load_01(ctx, ci):
    fleet = list(ctx.fleet)
    if len(fleet) < 4:
        return SKIP, "needs the fleet DID list", ""
    port = ctx.args.port
    cap = ch.quorum_capacity(DEFAULT_CAP)
    ladder_text = getattr(ctx.args, "load_ladder", DEFAULT_LADDER)

    # How many DIDs we will ever fire at once decides how many quorums we need.
    rungs = ch.ladder(ladder_text, len(fleet))
    if not rungs:
        return SKIP, "the ladder is empty for {} DIDs".format(len(fleet)), ""
    top = rungs[-1]
    need_q = ch.quorums_for(top, cap)

    # Quorums: one DID each, spread across different machines so signing
    # crosses the network; the rest are senders.
    by_machine = {}
    for e in fleet:
        by_machine.setdefault(_machine(e["host"]), []).append(e)
    quorums, q_machines = [], set()
    for m, entries in by_machine.items():
        if len(quorums) >= need_q:
            break
        quorums.append(entries[0])
        q_machines.add(m)
    q_dids = set(q["did"] for q in quorums)
    senders = [e for e in fleet if e["did"] not in q_dids]

    # Set up and fund the quorums.
    def setup_q(q):
        ok, msg = rc.quorum_setup(q["host"], q["did"], port)
        if ok:
            have = _free(q["host"], q["did"]) or 0.0
            floor = 50 + cap        # room to pledge a ring of 1-RBT sends
            if have < floor:
                ok, msg = rc.fund_did(q["host"], q["did"], floor - have, port)
        return ok, msg
    with ThreadPoolExecutor(max_workers=min(16, len(quorums))) as ex:
        if not all(ok for ok, _m in ex.map(setup_q, quorums)):
            return SKIP, "not every quorum could be set up and funded", ""

    # Point every sending node at one of the quorums, round-robin.
    sender_hosts = []
    for e in senders:
        if e["host"] not in sender_hosts:
            sender_hosts.append(e["host"])
    def wire(i_host):
        i, host = i_host
        return rc.quorum_reset(host, [quorums[i % len(quorums)]["did"]], port)[0]
    with ThreadPoolExecutor(max_workers=min(40, len(sender_hosts))) as ex:
        wired = sum(1 for ok in ex.map(wire, enumerate(sender_hosts)) if ok)
    if wired < len(sender_hosts):
        return False, "only {} of {} nodes accepted a quorum - cannot load them".format(
            wired, len(sender_hosts)), ""

    def fund_group(group):
        def one(e):
            have = _free(e["host"], e["did"]) or 0.0
            if have >= 3:
                return True
            return rc.fund_did(e["host"], e["did"], 3 - have, port)[0]
        with ThreadPoolExecutor(max_workers=min(40, len(group))) as ex:
            return all(ex.map(one, group))

    lines, limit, clean = [], 0, True
    for i, k in enumerate(rungs):
        group = senders[:k]
        if not fund_group(group):
            lines.append("{}: some wallets could not be funded, skipped".format(k))
            continue
        jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": 1.0,
                 "memo": "LOAD-01 rung {}".format(k)} for a, b in ch.ring(group)]
        s = ch.summarize(ch.burst(jobs))
        line = "{}: {}/{} at once, p50 {:.1f}s p95 {:.1f}s max {:.1f}s{}".format(
            k, s["ok"], s["n"], s["p50"], s["p95"], s["max"],
            " - refused: " + s["reasons"] if s["failed"] else "")
        lines.append(line)
        print("    LOAD-01 " + line)
        if s["failed"] == 0 and clean:
            limit = k
        else:
            clean = False
        if s["n"] and 100.0 * s["ok"] / s["n"] < ctx.args.ladder_stop:
            lines.append("stopped: under {:.0f}% went through at {}".format(ctx.args.ladder_stop, k))
            break
        if i < len(rungs) - 1:
            time.sleep(ctx.args.ladder_settle)

    note = "{} quorums for up to {} senders (capacity {}/quorum) | {}".format(
        len(quorums), top, cap, " | ".join(lines))
    if limit:
        ch.save_limit("fleet_concurrent_senders", limit,
                      {"quorums": len(quorums), "capacity": cap})
    if not limit:
        return False, "even {} DIDs transacting at once did not all go through".format(rungs[0]), note
    top_note = " - the top of the ladder, so the real limit is higher" if limit == rungs[-1] else ""
    return True, "{} DIDs transacted at once with every transfer going through{}".format(
        limit, top_note), note


CASES = {"LOAD-01": load_01}
ORDER = ["LOAD-01"]
NEEDS = {
    # Self-managed from ctx.fleet, so it needs the fleet to itself.
    "LOAD-01": {"senders": 2, "receivers": 0, "quorums": 0, "fund": 0, "exclusive": True},
}
NO_DELAY_RERUN = {"LOAD-01"}
