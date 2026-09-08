#!/usr/bin/env python3
"""
sc_cases_stress.py - concurrency and scale against the collateral path.

Imported by sc_cases.py.

WHY THESE EXIST
    Every other collateral case runs one deploy at a time on a quiet wallet.
    PR #739's own code comment names a hazard that only appears when that is
    not true:

        "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
         opens its own connection and upserts the burnt parent row, which
         would deadlock against locks held by that outer transaction until
         lock_timeout fires."

    The ordering was chosen to avoid a deadlock. Nothing currently tries to
    cause one. Concurrent deploys from a single wallet are exactly the shape
    that would - two splits, two genesis persists, one wallet's rows.

    The other target is token_denom itself. Two of the three fixes in this PR
    write that table from DIFFERENT code paths - post_consensus_persistence.go
    for a deploy, token_chain.go for an FT burn - and neither appears to
    coordinate with the other. Running both against one DID at once is the
    obvious race and is covered by CRS-C-02 in the cross-asset module.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _link():
    import sc_cases
    return sc_cases


# ---------------------------------------------------------------------------
# SC-C-12
# ---------------------------------------------------------------------------

def sc_c_12(ctx, ci):
    """
    SC-C-12 - Several deploys fired at once from ONE wallet.

    WHAT IT CHECKS
        Five deploys with values are fired simultaneously from a single wallet.
        All succeed, the total cost equals the sum of the values, and the
        counter is consistent afterwards.

    WHY IT MATTERS
        This is the case PR #739's own comment is defending against. The
        collateral split runs BEFORE the outer transaction opens, because
        PersistGenesisTransaction takes its own connection and would otherwise
        deadlock against locks the outer transaction already holds - waiting
        out lock_timeout before failing.

        One wallet is the point: concurrent deploys from DIFFERENT wallets
        touch different rows. From one wallet they contend for the same tokens,
        the same denom rows, and the same genesis-persist path. If the ordering
        is not sufficient, this is where it surfaces - as a timeout, a
        double-spend of the same parent token, or a counter that no longer
        matches reality.

    MANUAL STEPS
        Generate five contracts from one DID, then POST all five deploys
        without waiting for each to return. Afterwards compare the balance drop
        against the sum of the values, and the denom listing against reality.

    PASS / FAIL
        PASS  all five succeed, cost is exact, counter consistent
        FAIL  a deploy times out or reports a lock error -> the deadlock the
              comment describes
        FAIL  cost does not match -> a parent token was consumed twice
        FAIL  counter drifts -> concurrent decrements on one DID are unsafe
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    n = 5
    values = [sc.rand_value(0.050, 0.300) for _ in range(n)]
    ready, why = sc._prepare(ctx, s, sum(values) + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    prepared = []
    for v in values:
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        prepared.append((sc_id, v))

    try:
        before_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before_snap["denom_drift"]:
        return SKIP, "already drifting", (
            "counter inconsistent before the race, so nothing here is "
            "attributable - see GEN-IN-08")
    before = sc._bal(ctx, s)

    def fire(item):
        sc_id, v = item
        started = time.time()
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="concurrent same-wallet deploy",
                                       port=ctx.port)
        return v, ok, str(msg), round(time.time() - started, 1)

    with ThreadPoolExecutor(max_workers=n) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 2)

    after = sc._bal(ctx, s)
    try:
        after_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before_snap, after_snap)

    failed = [(v, m, secs) for v, ok, m, secs in outcomes if not ok]
    ok_values = [v for v, ok, _m, _s in outcomes if ok]
    spent = (before["balance"] - after["balance"]) if (before and after) else None
    expected = sum(ok_values)

    problems = []
    for v, m, secs in failed:
        low = m.lower()
        if "lock" in low or "timeout" in low or "deadlock" in low:
            problems.append("value {} failed after {}s with a LOCK/TIMEOUT error "
                            "({}) - this is the deadlock the collateral-split "
                            "ordering exists to prevent".format(v, secs, m[:80]))
        else:
            problems.append("value {} rejected after {}s: {}".format(v, secs, m[:80]))
    if spent is not None and ok_values and not rc.close_enough(
            spent, expected, tol=sc.cost_tolerance(expected)):
        problems.append("spent {:.4f} for {} successful deploy(s) totalling "
                        "{:.4f} - a parent token was likely consumed twice".format(
                            spent, len(ok_values), expected))
    if drift:
        problems.append("counter drifted after concurrent deploys on ONE DID: "
                        + db.describe_drift(drift))

    return (not problems), "{}/{} concurrent deploys ok, spent {:.4f}".format(
        len(ok_values), n, spent if spent is not None else -1), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-26
# ---------------------------------------------------------------------------

def sc_c_26(ctx, ci):
    """
    SC-C-26 - Many nodes deploying at once through the shared quorums.

    WHAT IT CHECKS
        Every host in this lane deploys a valued contract simultaneously. All
        should succeed, each costing its own value, with no quorum's counter
        drifting.

    WHY IT MATTERS
        The fleet-scale version. Each deployer is independent, so this is not
        testing the same contention as SC-C-12 - it is testing whether the
        SHARED quorums hold up when every lane hits them at once. Quorum
        capacity, pledge serialisation and the quorum-side denom decrement all
        come under pressure together.

        Expect some noise here: a rejection for genuine pledge shortage is a
        funding result, not a defect, and the case says so rather than
        reporting it as a failure. What matters is a rejection while the quorum
        clearly had capacity, or a counter that ends up wrong.

    MANUAL STEPS
        Fire a deploy from every available host at the same moment, then check
        each quorum's pledged total and denom listing.

    PASS / FAIL
        PASS  all deploys succeed, no quorum counter drifts
        RECORD  how many succeeded and the failure reasons - a pledge shortage
              is a capacity finding, not a correctness one
        FAIL  a quorum's counter drifts -> concurrent pledging is unsafe
        SKIP  fewer than 3 hosts in this lane
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    hosts = list(ctx.senders) + list(ctx.receivers)
    seen, deployers = set(), []
    for e in hosts:
        if e["host"] not in seen:
            seen.add(e["host"])
            deployers.append(e)
    if len(deployers) < 3:
        return SKIP, "need 3+ hosts", (
            "lane has {} host(s); fleet-scale concurrency needs more".format(
                len(deployers)))

    values = [sc.rand_value(0.050, 0.250) for _ in deployers]
    prepared = []
    for e, v in zip(deployers, values):
        ready, why = sc._prepare(ctx, e, v + 4)
        if not ready:
            continue
        sc_id, err = sc._new_contract(ctx, e)
        if err:
            continue
        prepared.append((e, sc_id, v))
    if len(prepared) < 3:
        return SKIP, "could not prepare enough hosts", (
            "only {} of {} hosts ready".format(len(prepared), len(deployers)))

    try:
        q_before = {q["host"]: db.snapshot(q["host"], q["did"])
                    for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def fire(item):
        e, sc_id, v = item
        ok, msg, _ = rc.sc_transaction(e["host"], e["did"], sc_id, value=v,
                                       data="fleet-scale deploy", port=ctx.port)
        return e["host"], v, ok, str(msg)

    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 3)

    try:
        q_after = {q["host"]: db.snapshot(q["host"], q["did"])
                   for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    succeeded = [o for o in outcomes if o[2]]
    pledge_short, other_fail = [], []
    for host, v, ok, msg in outcomes:
        if ok:
            continue
        low = msg.lower()
        (pledge_short if ("pledge" in low or "insufficient" in low)
         else other_fail).append((host, v, msg))

    problems = []
    for qh in q_before:
        drift = db.new_drift(q_before[qh], q_after[qh])
        if drift:
            problems.append("quorum {} counter drifted under concurrent load: {}".format(
                qh, db.describe_drift(drift)))
    for host, v, msg in other_fail:
        problems.append("{} (value {}) rejected for a non-capacity reason: {}".format(
            host, v, msg[:80]))

    note = "; ".join(problems)
    if pledge_short and not problems:
        note = ("{} deploy(s) rejected for pledge shortage - a quorum CAPACITY "
                "result at this concurrency, not a correctness one. Raise "
                "--fund-quorum or lower the concurrency to separate the "
                "two.".format(len(pledge_short)))

    return (not problems), "{}/{} deploys ok across {} host(s); {} pledge-short".format(
        len(succeeded), len(outcomes), len(prepared), len(pledge_short)), note
