"""load_cases.py - the whole fleet transacting at once.

LOAD-01 climbs a ladder of how many DIDs transact at the same instant, up to
every DID in the pool. It is the headline scale test: the product's CI runs
three nodes, this runs hundreds of wallets through tens of quorums at once.

The quorums are sized from QCAP-01 (how many transfers one quorum signs at
once), so the quorum is never the bottleneck - what the ladder finds is where
the NETWORK and the nodes themselves start to drop transfers, and whether Rubix
then refuses gently (master/steps.py). Every machine a rung runs on is watched:
a rung that fails while a machine was at a CPU / memory / disk ceiling is the
machine's limit, not Rubix's, and is not saved as one.

It is exclusive and self-managed: it wires every pool node at one of its
quorums and drives DIDs straight from ctx.fleet, because the ladder's top rung
uses more DIDs than there are nodes (nodes hold several DIDs each).
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc
import steps

SKIP = "SKIP"
DEFAULT_CAP = 50
DEFAULT_LADDER = "10,20,50,100,200,300,500,750,988"

PURPOSE = {
    "LOAD-01": "How many DIDs across the whole fleet can transfer at the same instant with every "
               "transfer correct, and whether Rubix refuses the overflow gently - with enough "
               "quorums that no quorum is the limit, and every machine watched.",
}

STEPS = {
    "LOAD-01": [
        ("S1", "Set-up check",
         "set up quorums sized from QCAP-01, one per machine, funded for every pledge; point "
         "every sending node at one of them and read every list back",
         "every quorum set up; every sending node signs with its quorum"),
        ("S2", "Fleet rung (one per rung)",
         "k DIDs across the fleet send 1 RBT each at the same instant (a ring)",
         "all k accepted and on their receivers' nodes; past the limit, the rest refused with a "
         "stated reason within 60s; no node restarts or goes deaf; nothing Locked"),
        ("S3", "Recovery", "one ordinary transfer after the ladder",
         "goes through in normal time"),
    ],
}


def _free(entry):
    ok, detail, _n = rc.get_rbt_balance_detail(entry["host"], entry["did"])
    return float(detail["balance"]) if ok and detail else None


def load_01(ctx, ci):
    run = steps.CaseRun(ctx, "LOAD-01", STEPS["LOAD-01"])
    fleet = list(ctx.fleet)
    if len(fleet) < 4:
        return SKIP, "needs the fleet DID list", ""
    port = ctx.args.port
    cap = ch.quorum_capacity(DEFAULT_CAP)
    rungs = ch.ladder(getattr(ctx.args, "load_ladder", DEFAULT_LADDER), len(fleet))
    if not rungs:
        return SKIP, "the ladder is empty for {} DIDs".format(len(fleet)), ""
    need_q = ch.quorums_for(rungs[-1], cap)

    # Quorums: one DID each, on different machines; the rest send.
    by_machine = {}
    for e in fleet:
        by_machine.setdefault(ch.machine(e["host"]), []).append(e)
    quorums = []
    for _m, entries in by_machine.items():
        if len(quorums) >= need_q:
            break
        quorums.append(entries[0])
    q_dids = set(q["did"] for q in quorums)
    senders = [e for e in fleet if e["did"] not in q_dids]
    hosts = []
    for e in senders:
        if e["host"] not in hosts:
            hosts.append(e["host"])
    wiring = dict((h, quorums[i % len(quorums)]["did"]) for i, h in enumerate(hosts))

    def quorums_ready():
        for q in quorums:
            ok, why = steps.quorum_ready(q, 0, port)
            if not ok:
                return ok, why
            have = _free(q) or 0.0
            if have < 50 + cap:
                rc.fund_did(q["host"], q["did"], 50 + cap - have, port)
        return True, ""

    def wired():
        ch.run_parallel(lambda h: rc.quorum_reset(h, [wiring[h]], port), hosts, workers=40)
        bad = [why for ok, why in ch.run_parallel(lambda h: steps.wired(h, wiring[h], port), hosts,
                                                   workers=40) if not ok]
        return (not bad), "{} of {} nodes do not sign with their quorum, e.g. {}".format(
            len(bad), len(hosts), bad[0] if bad else "")

    s1 = run.step("S1", lambda st: None, nodes=quorums, watch=False,
                  require=[quorums_ready, wired])
    if s1.outcome != steps.PASS:
        return run.verdict()

    def funded(group):
        def one(e):
            have = _free(e) or 0.0
            return have >= 3 or rc.fund_did(e["host"], e["did"], 3 - have, port)[0]
        return all(ch.run_parallel(one, group, workers=40)), "some wallets could not be funded"

    def pledge_room(k):
        # Each quorum pledges its share of the rung; earlier pledges are held
        # until spent onward and may not be released (core/fullnode.go:69), so
        # every rung starts with room for its own share.
        share = -(-k // len(quorums)) + 10
        for q in quorums:
            have = _free(q) or 0.0
            if have < share:
                rc.fund_did(q["host"], q["did"], share - have, port)
        return all((_free(q) or 0.0) >= share - 10 for q in quorums), \
            "a quorum could not be funded for its share of {} pledges".format(k)

    limit, stop = 0, None
    for i, k in enumerate(rungs):
        group = senders[:k]
        jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": 1.0,
                 "memo": "LOAD-01 rung {}".format(k)} for a, b in ch.ring(group)]
        st = run.step("S2", lambda st, jobs=jobs: st.add(ch.burst(jobs), jobs, set_aside=ch.ASIDE),
                      nodes=group + quorums, label="{} DIDs at once".format(k),
                      require=[lambda group=group: funded(group), lambda k=k: pledge_room(k)])
        if st.outcome != steps.PASS:
            stop = st
            break
        limit = k
        if i < len(rungs) - 1:
            time.sleep(ctx.args.ladder_settle)

    a, b = senders[0], senders[1]
    run.step("S3", lambda st: ch.send(st, a, b, rbt=1.0, memo="LOAD-01 recovery"),
             nodes=[a, b] + quorums, required=False, must_accept=True)

    if limit and (stop is None or stop.outcome in (steps.LIMIT, steps.DAMAGE, steps.WRONG)):
        ch.save_limit("fleet_concurrent_senders", limit,
                      {"quorums": len(quorums), "capacity": cap,
                       "stopped_at": stop.sid if stop else "top of ladder"})
    top = " - the top of the ladder, so the real limit is higher" if stop is None else ""
    return run.verdict(passed="{} DIDs transacted at once with every transfer going through{}".format(
        limit, top), limit="the fleet's limit, met gently")


CASES = {"LOAD-01": load_01}
ORDER = ["LOAD-01"]
NEEDS = {
    # Self-managed from ctx.fleet, so it needs the fleet to itself.
    "LOAD-01": {"senders": 2, "receivers": 0, "quorums": 0, "fund": 0, "exclusive": True},
}
NO_DELAY_RERUN = {"LOAD-01"}
