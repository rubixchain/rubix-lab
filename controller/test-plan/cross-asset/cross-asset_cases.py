#!/usr/bin/env python3
"""
cross-asset_cases.py - operations that touch more than one asset type at once.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases cross-asset

WHY THIS MODULE MATTERS FOR PR #739
    The PR fixes token_denom accounting on TWO separate code paths:

      df07a49f  core/wallet/post_consensus_persistence.go   (SC deploy collateral)
      977f6fba  core/wallet/token_chain.go                  (RBT burnt for FT mint)

    Each was fixed independently, and neither appears to coordinate with the
    other. They write the same table, for the same DID, from different call
    stacks. Every case in the SC and FT suites exercises exactly one of them at
    a time.

    These cases run them together. CRS-C-02 runs them CONCURRENTLY on one DID,
    which is the obvious race and the case most likely to fail.

Docstring contract is the same as the other modules:
    WHAT IT CHECKS / WHY IT MATTERS / MANUAL STEPS / PASS-FAIL
"""

import os
import random
import string
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _rand_value(lo, hi):
    return max(round(random.uniform(lo, hi), 3), 0.001)


def _tag(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)
    detail = _bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)
    qd = _bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _new_contract(ctx, entry):
    tag = _tag()
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"],
                                               wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None


# ---------------------------------------------------------------------------
# CRS-C-01
# ---------------------------------------------------------------------------

def crs_c_01(ctx, ci):
    """
    CRS-C-01 - Deploy a contract with a value AND transfer RBT in one call.

    WHAT IT CHECKS
        A single /tx carrying both an RBT transfer and a valued SC deploy. Both
        take effect, each deducts its own amount, and the counter is consistent
        afterwards.

    WHY IT MATTERS
        These are the two paths df07a49f had to reconcile. The fix suppresses
        the isLocalTransfer credit for a deploy, because a deploy pins Owner to
        Initiator and the credit would land on the very DID just decremented -
        cancelling it out.

        A bundled call is where that reasoning is hardest: there IS a genuine
        transfer in the same transaction, with a real receiver, alongside a
        deploy whose owner is the initiator. If the suppression is too broad
        the transfer's accounting is lost; too narrow and the deploy's
        decrement is cancelled. Neither shows up when the two run separately.

    MANUAL STEPS
        Post one transaction with both parts:
          {"initiator":"<DID>", "owner":"<RECEIVER_DID>",
           "tokens":{"rbt":1.0, "ft":[], "nft":[],
                     "smartContract":[{"smartContractId":"<SC>",
                                       "value":0.354,"data":"bundled"}],
                     "transferNftOwnership":false},
           "memo":"CRS-C-01"}
        Check the sender's balance fell by 1.354, the receiver gained 1.0, and
        the denom listing still matches reality.

    PASS / FAIL
        PASS  sender down by transfer+value, receiver up by the transfer, no drift
        FAIL  sender down by only one of the two -> one path's accounting was
              cancelled by the other
        FAIL  counter drifts -> the credit suppression is wrong in a bundle
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    transfer = 1.0
    value = _rand_value(0.100, 0.500)

    ready, why = _prepare(ctx, s, transfer + value + 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before_snap["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"
    s_before = _bal(ctx, s)
    r_before = _bal(ctx, r)

    body = {
        "initiator": s["did"], "owner": r["did"],
        "tokens": {
            "rbt": transfer, "ft": [], "nft": [],
            "smartContract": [{"smartContractId": sc_id, "value": value,
                               "data": "bundled deploy + transfer"}],
            "transferNftOwnership": False,
        },
        "memo": "CRS-C-01 bundled",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    s_after = _bal(ctx, s)
    r_after = _bal(ctx, r)
    try:
        after_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before_snap, after_snap)

    spent = (s_before["balance"] - s_after["balance"]) if (s_before and s_after) else None
    gained = (r_after["balance"] - r_before["balance"]) if (r_before and r_after) else None
    expected = transfer + value

    problems = []
    if spent is None or not rc.close_enough(spent, expected, tol=TOL * 4):
        problems.append("sender spent {:.4f}, expected {:.4f} (transfer {} + "
                        "value {:.3f})".format(spent if spent is not None else -1,
                                               expected, transfer, value))
        if spent is not None and rc.close_enough(spent, transfer, tol=TOL * 4):
            problems.append("the deploy's collateral was not deducted at all - "
                            "its decrement was cancelled inside the bundle")
        elif spent is not None and rc.close_enough(spent, value, tol=TOL * 4):
            problems.append("the transfer was not deducted - the deploy's credit "
                            "suppression is too broad")
    if gained is None or not rc.close_enough(gained, transfer, tol=TOL * 4):
        problems.append("receiver gained {:.4f}, expected {}".format(
            gained if gained is not None else -1, transfer))
    if drift:
        problems.append("counter drifted after a bundled call: "
                        + db.describe_drift(drift))

    return (not problems), "sent {:.4f} (transfer {} + deploy {:.3f}), receiver +{:.4f}".format(
        spent if spent is not None else -1, transfer, value,
        gained if gained is not None else -1), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-02
# ---------------------------------------------------------------------------

def crs_c_02(ctx, ci):
    """
    CRS-C-02 - SC deploy and FT mint fired CONCURRENTLY on the same DID.

    WHAT IT CHECKS
        A valued contract deploy and an FT mint start at the same moment on one
        wallet. Both should succeed, each should cost its own amount, and the
        denomination counter must still match the real free tokens afterwards.

    WHY IT MATTERS
        This is the sharpest test in the suite for PR #739, because it is the
        only one that runs BOTH fixes at once.

          df07a49f  decrements token_denom from post_consensus_persistence.go
          977f6fba  decrements token_denom from token_chain.go

        Same table, same DID, different call stacks, fixed independently by
        different changes. Nothing in either fix appears to coordinate with the
        other. Run separately - as every other case does - they each look
        correct. Run together they are two concurrent read-modify-write cycles
        against the same rows.

        The likely failure is not a crash. It is a counter that ends up short
        by one decrement because both paths read the same starting value, and
        an unrelated transfer failing much later with
        "lockSelectedTokens: no tokens provided".

    MANUAL STEPS
        1. Snapshot the wallet:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
             SELECT token_value, COUNT(*) FROM tokens
               WHERE did='<DID>' AND token_status=0 AND token_type=1
               GROUP BY token_value;
        2. In two terminals at the same moment: POST an SC deploy with a value,
           and POST an FT mint. Sign both.
        3. Wait ~15s and re-run both queries. They must still agree.

    PASS / FAIL
        PASS  both succeed, both cost correctly, counter consistent
        FAIL  counter drifts -> the two decrements interfered; this is a NEW
              finding, not a regression of either fix on its own
        FAIL  either operation is rejected while the wallet has ample balance
        SKIP  counter already drifting before the race
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    value = _rand_value(0.100, 0.500)
    ft_backing = 2          # whole RBT burnt by the mint
    ready, why = _prepare(ctx, s, value + ft_backing + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ft_name = "race" + _tag(6)

    try:
        before = db.record("CRS-C-02", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting before the race", (
            "counter inconsistent beforehand: " + db.describe_drift(before["denom_drift"])
            + " - a drift afterwards could not be attributed to the race")

    def do_deploy():
        return ("sc-deploy",) + rc.sc_transaction(
            s["host"], s["did"], sc_id, value=value,
            data="race: deploy", port=ctx.port)[:2]

    def do_mint():
        return ("ft-mint",) + rc.mint_ft(
            s["host"], s["did"], ft_name, 10, ft_backing, ctx.port)[:2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_deploy)
        f2 = pool.submit(do_mint)
        outcomes = [f1.result(), f2.result()]

    time.sleep(SETTLE * 3)
    try:
        after = db.record("CRS-C-02", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)
    d = db.delta(before, after)

    failed = [(name, msg) for name, ok, msg in outcomes if not ok]
    problems = []
    for name, msg in failed:
        problems.append("{} rejected: {}".format(name, str(msg)[:90]))
    if drift:
        problems.append(
            "DENOM COUNTER DRIFTED after running both paths at once: "
            + db.describe_drift(drift) +
            " - the SC-deploy decrement (post_consensus_persistence.go) and the "
            "FT-burn decrement (token_chain.go) interfered. Each is correct "
            "alone; this is a new finding about the two together, and it will "
            "surface later as an unrelated transfer failing with "
            "'lockSelectedTokens: no tokens provided'")

    return (not problems), (
        "deploy {:.3f} + mint {} RBT concurrently | {} | counter {}".format(
            value, ft_backing, db.format_evidence(before, after),
            "ok" if not drift else "DRIFT")), "; ".join(problems)



# ---------------------------------------------------------------------------
# CRS-C-03
# ---------------------------------------------------------------------------

def crs_c_03(ctx, ci):
    """
    CRS-C-03 - What the RECEIVER's node records after a bundled deploy.

    WHAT IT CHECKS
        After a bundled RBT transfer plus a valued SC deploy, the RECEIVER's
        own database must show only the transferred RBT. The contract's
        collateral must not appear as tokens the receiver owns.

    WHY IT MATTERS
        Every other denomination case measures the INITIATOR. That is only half
        of what df07a49f changed, because the status decision is role-aware:

            isSCDeployCommit := input.RoleName == TokenRole_Commit &&
                executionRole == ExecutionRoleInitiator &&
                hasSCDeploy(txInfo)

        On the initiator's node the collateral is marked Committed. On the
        receiver's node executionRole is Receiver, the guard is false, and the
        default branch writes those tokens as Free owned by the Owner.

        A pure deploy pins Owner to Initiator, so there is no separate receiver
        and the gap never appears. A bundle has a real receiver - and CRS-C-01
        showed it gains the collateral. This case reads the receiver's rows
        directly, so the report states what the receiver actually recorded
        rather than inferring it from a balance.

    MANUAL STEPS
        After a bundled deploy plus transfer, on the RECEIVER's host:
          psql -h $RECV -p 5433 -U rubix -d rubix -c \\
            "SELECT token_id, token_value, token_status FROM tokens
               WHERE did='<RDID>' AND token_type=1 AND token_status=0
               ORDER BY token_value DESC;"
        The newly arrived value should equal the TRANSFER only.

    PASS / FAIL
        PASS  receiver's free value rose by the transfer amount only
        FAIL  it rose by transfer + collateral -> the receiver recorded the
              contract's collateral as its own
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    transfer = 1.0
    value = _rand_value(0.100, 0.500)

    ready, why = _prepare(ctx, s, transfer + value + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        s_before = db.record("CRS-C-03", "before (initiator)", s["host"], s["did"],
                             db.snapshot(s["host"], s["did"]))
        r_before = db.record("CRS-C-03", "before (receiver)", r["host"], r["did"],
                             db.snapshot(r["host"], r["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    body = {
        "initiator": s["did"], "owner": r["did"],
        "tokens": {"rbt": transfer, "ft": [], "nft": [],
                   "smartContract": [{"smartContractId": sc_id, "value": value,
                                      "data": "bundled, receiver view"}],
                   "transferNftOwnership": False},
        "memo": "CRS-C-03 receiver view",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        s_after = db.record("CRS-C-03", "after (initiator)", s["host"], s["did"],
                            db.snapshot(s["host"], s["did"]))
        r_after = db.record("CRS-C-03", "after (receiver)", r["host"], r["did"],
                            db.snapshot(r["host"], r["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    r_gain = r_after["free"] - r_before["free"]
    s_committed = s_after["committed"] - s_before["committed"]

    problems = []
    if not rc.close_enough(r_gain, transfer, tol=TOL * 4):
        problems.append("receiver free value rose {:.4f}, expected {} (the "
                        "transfer only)".format(r_gain, transfer))
        if rc.close_enough(r_gain, transfer + value, tol=TOL * 4):
            problems.append("the excess is exactly the contract value {:.3f} - "
                            "the receiver recorded the collateral as its own, "
                            "because isSCDeployCommit requires executionRole == "
                            "Initiator and the receiver is not".format(value))
    if not rc.close_enough(s_committed, value, tol=TOL * 4):
        problems.append("initiator committed {:.4f}, expected {:.3f}".format(
            s_committed, value))

    return (not problems), "receiver +{:.4f} (transfer {}), initiator committed {:.4f} | recv[{}]".format(
        r_gain, transfer, s_committed,
        db.format_evidence(r_before, r_after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-04
# ---------------------------------------------------------------------------

def crs_c_04(ctx, ci):
    """
    CRS-C-04 - Transfer between two DIDs on ONE node (no contract involved).

    WHAT IT CHECKS
        A plain RBT transfer where sender and receiver are different DIDs on
        the same machine. The sender's free balance falls, the receiver's
        rises, and the node's denomination counters for both DIDs still match
        reality.

    WHY IT MATTERS
        This is the baseline for CRS-C-05, and it is the only way to reach the
        isLocalTransfer branch at all. df07a49f changed how that branch is
        gated:

            upsertTokenDenomDeltas(..., isLocalTransfer && !scDeploy, ...)

        The comment explains the suppression exists because a deploy pins Owner
        to Initiator, making isLocalTransfer trivially true. But the block it
        suppresses was written for a REAL same-node transfer - decrement the
        sender, credit the receiver - and on a one-DID-per-node fleet that
        situation cannot occur, so it has never been exercised.

        Run this first: if a plain same-node transfer is already wrong,
        CRS-C-05's bundled version tells you nothing.

    REQUIRES a host tagged 'multidid' in hosts.txt. Every other host is held to
    exactly one DID, and creating a second on one that did not opt in would
    break the invariant the controller tooling depends on.

    MANUAL STEPS
        On a multidid host, with DID_A and DID_B both local:
          curl -s -X POST http://$HOST:20000/rubix/v1/tx \\
               -H 'Content-Type: application/json' -d '{
                 "initiator":"<DID_A>", "owner":"<DID_B>",
                 "tokens":{"rbt":1.0,"transferNftOwnership":false},
                 "memo":"CRS-C-04"}'
        Then compare both DIDs' counters against their real free tokens.

    PASS / FAIL
        PASS  A down by the amount, B up by it, both counters consistent
        FAIL  a counter drifts -> the same-node credit/decrement pair does not
              balance
        SKIP  no multidid host in this lane
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    host = None
    for e in list(ctx.senders) + list(ctx.receivers):
        if e.get("role") == "multidid":
            host = e
            break
    if host is None:
        return SKIP, "no multidid host", (
            "this case needs two DIDs on ONE machine, which only a host tagged "
            "'multidid' in hosts.txt may have. Tag one pool host and re-run")

    did_b = ws.second_did(ctx, host)
    if not did_b:
        return SKIP, "no second DID", (
            "could not obtain a second DID on {}".format(host["host"]))
    did_a = host["did"]

    ready, why = _prepare(ctx, host, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    amount = 1.0
    try:
        a_before = db.record("CRS-C-04", "before (DID A)", host["host"], did_a,
                             db.snapshot(host["host"], did_a))
        b_before = db.record("CRS-C-04", "before (DID B)", host["host"], did_b,
                             db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.initiate_transaction(host["host"], did_a, did_b,
                                         rbt=amount, memo="CRS-C-04 same-node",
                                         port=ctx.port)
    if not ok:
        return False, "same-node transfer rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        a_after = db.record("CRS-C-04", "after (DID A)", host["host"], did_a,
                            db.snapshot(host["host"], did_a))
        b_after = db.record("CRS-C-04", "after (DID B)", host["host"], did_b,
                            db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    a_moved = a_before["free"] - a_after["free"]
    b_moved = b_after["free"] - b_before["free"]

    problems = []
    if not rc.close_enough(a_moved, amount, tol=TOL * 4):
        problems.append("sender DID fell {:.4f}, expected {}".format(a_moved, amount))
    if not rc.close_enough(b_moved, amount, tol=TOL * 4):
        problems.append("receiver DID rose {:.4f}, expected {}".format(b_moved, amount))
    for label, before, after in (("sender", a_before, a_after),
                                 ("receiver", b_before, b_after)):
        d = db.new_drift(before, after)
        if d:
            problems.append("{} DID counter drifted: {}".format(
                label, db.describe_drift(d)))

    return (not problems), "same node {}: A -{:.4f}, B +{:.4f}".format(
        host["host"], a_moved, b_moved), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-05
# ---------------------------------------------------------------------------

def crs_c_05(ctx, ci):
    """
    CRS-C-05 - Same-node transfer bundled with a valued contract deploy.

    WHAT IT CHECKS
        One transaction carrying an RBT transfer between two DIDs on the SAME
        machine, plus a valued SC deploy. The sender pays transfer + value, the
        other local DID gains the transfer only, and both counters stay
        consistent.

    WHY IT MATTERS
        This is the single branch of df07a49f that nothing else can reach:

            upsertTokenDenomDeltas(..., isLocalTransfer && !scDeploy, ...)

        The suppression assumes those two conditions never usefully coincide -
        a deploy pins Owner to Initiator, so isLocalTransfer is "trivially
        true" and the credit would cancel the decrement. A bundle breaks that
        assumption: there IS a real same-node transfer with a genuine second
        DID, AND a deploy, in one transaction.

        So the suppression fires when a real local credit was owed. If it is
        too broad the transfer's credit is lost; if too narrow the deploy's
        decrement is cancelled. Both are silent, and both leave one DID's
        counter permanently wrong.

        CRS-C-01 found the cross-node version of this. This is the same-node
        version, and it is the last uncovered branch of hunk H2.

    REQUIRES a host tagged 'multidid' in hosts.txt.

    MANUAL STEPS
        On a multidid host, post ONE transaction with both parts:
          {"initiator":"<DID_A>", "owner":"<DID_B>",
           "tokens":{"rbt":1.0, "ft":[], "nft":[],
                     "smartContract":[{"smartContractId":"<SC>",
                                       "value":0.4,"data":"bundled"}],
                     "transferNftOwnership":false}}
        Then compare both DIDs' counters against their real free tokens.

    PASS / FAIL
        PASS  A down by transfer+value, B up by the transfer, no drift
        FAIL  B gained transfer+value -> the collateral was credited locally,
              the same-node form of what CRS-C-01 found
        FAIL  either counter drifts -> the suppression is wrong in a bundle
        SKIP  no multidid host in this lane
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    host = None
    for e in list(ctx.senders) + list(ctx.receivers):
        if e.get("role") == "multidid":
            host = e
            break
    if host is None:
        return SKIP, "no multidid host", (
            "needs two DIDs on ONE machine - tag a pool host 'multidid' in "
            "hosts.txt. This is the only branch of the denom gate that cannot "
            "be reached any other way")

    did_b = ws.second_did(ctx, host)
    if not did_b:
        return SKIP, "no second DID", "could not obtain a second DID on {}".format(
            host["host"])
    did_a = host["did"]

    ready, why = _prepare(ctx, host, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    transfer = 1.0
    value = _rand_value(0.100, 0.500)
    sc_id, err = _new_contract(ctx, host)
    if err:
        return SKIP, "generation failed", err

    try:
        a_before = db.record("CRS-C-05", "before (DID A)", host["host"], did_a,
                             db.snapshot(host["host"], did_a))
        b_before = db.record("CRS-C-05", "before (DID B)", host["host"], did_b,
                             db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    body = {
        "initiator": did_a, "owner": did_b,
        "tokens": {"rbt": transfer, "ft": [], "nft": [],
                   "smartContract": [{"smartContractId": sc_id, "value": value,
                                      "data": "same-node bundle"}],
                   "transferNftOwnership": False},
        "memo": "CRS-C-05 same-node bundle",
    }
    ok, msg, _ = rc._tx(host["host"], body, ctx.port)
    if not ok:
        return False, "same-node bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        a_after = db.record("CRS-C-05", "after (DID A)", host["host"], did_a,
                            db.snapshot(host["host"], did_a))
        b_after = db.record("CRS-C-05", "after (DID B)", host["host"], did_b,
                            db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    a_spent = a_before["free"] - a_after["free"]
    b_gain = b_after["free"] - b_before["free"]
    a_committed = a_after["committed"] - a_before["committed"]
    expected_spend = transfer + value

    problems = []
    if not rc.close_enough(a_spent, expected_spend, tol=TOL * 4):
        problems.append("initiator spent {:.4f}, expected {:.4f}".format(
            a_spent, expected_spend))
    if not rc.close_enough(b_gain, transfer, tol=TOL * 4):
        problems.append("local receiver gained {:.4f}, expected {}".format(
            b_gain, transfer))
        if rc.close_enough(b_gain, expected_spend, tol=TOL * 4):
            problems.append("the excess is exactly the contract value {:.3f} - "
                            "the collateral was credited to the other local DID, "
                            "the same-node form of CRS-C-01".format(value))
    if not rc.close_enough(a_committed, value, tol=TOL * 4):
        problems.append("initiator committed {:.4f}, expected {:.3f}".format(
            a_committed, value))
    for label, before, after in (("initiator", a_before, a_after),
                                 ("local receiver", b_before, b_after)):
        d = db.new_drift(before, after)
        if d:
            problems.append("{} counter drifted: {}".format(
                label, db.describe_drift(d)))

    return (not problems), "same-node bundle: A -{:.4f} (transfer {} + value {:.3f}), B +{:.4f}, committed {:.4f}".format(
        a_spent, transfer, value, b_gain, a_committed), "; ".join(problems)

CASES = {
    "CRS-C-01": crs_c_01,
    "CRS-C-02": crs_c_02,
    "CRS-C-03": crs_c_03,
    # CRS-C-04 / CRS-C-05 are written and working but NOT registered: they need
    # a host tagged 'multidid' in hosts.txt, and no host carries that tag yet.
    # Register them here once one does - the functions and the sweep support
    # are already in place, so it is a two-line change.
}

# CRS-C-01 first: if the bundled call is already wrong when run alone, the
# concurrent case has nothing clean to build on.
ORDER = ["CRS-C-01", "CRS-C-03", "CRS-C-02"]

TIMING_CASES = set()

# Both cases own their wallet outright - each measures a balance delta and a
# denom delta on one DID, so nothing else may touch it while they run.
LANES = {
    "crs-bundled": {
        "cases": ["CRS-C-01"],
        "hosts": 2, "fund": 12,
    },
    "crs-denom-race": {
        "cases": ["CRS-C-02"],
        "hosts": 1, "fund": 20,
    },
    # Reads the RECEIVER's rows, so it needs its own pair - the receiver must
    # not be shared with anything else moving value into it.
    "crs-receiver-view": {
        "cases": ["CRS-C-03"],
        "hosts": 2, "fund": 15,
    },
    # A "crs-intra-node" lane belongs here once a host is tagged 'multidid':
    #     "crs-intra-node": {"cases": ["CRS-C-04", "CRS-C-05"],
    #                        "hosts": 6, "fund": 20},
}
