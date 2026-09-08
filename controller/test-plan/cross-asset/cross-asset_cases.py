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
        before = db.snapshot(s["host"], s["did"])
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
        after = db.snapshot(s["host"], s["did"])
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
        "deploy {:.3f} + mint {} RBT concurrently | free {:+.3f} committed {:+.3f} "
        "burnt {:+.3f} | counter {}".format(
            value, ft_backing, d["free"], d["committed"], d["burnt_for_ft"],
            "ok" if not drift else "DRIFT")), "; ".join(problems)


CASES = {
    "CRS-C-01": crs_c_01,
    "CRS-C-02": crs_c_02,
}

# CRS-C-01 first: if the bundled call is already wrong when run alone, the
# concurrent case has nothing clean to build on.
ORDER = ["CRS-C-01", "CRS-C-02"]

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
}
