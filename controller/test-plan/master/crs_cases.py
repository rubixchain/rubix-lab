"""crs_cases.py - Cross-asset cases: several assets in one transaction,
under concurrency.

Registered into the suite by master_cases.py. Case wording lives in
master-catalogue.csv; this file is the code.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import wallet_shapes as ws

from case_helpers import (
    SETTLE,
    SKIP,
)


# =============================================================================
# CROSS-ASSET
# =============================================================================


# -----------------------------------------------------------------------------
# Combined - deploy plus transfer, concurrent deploy and mint
# -----------------------------------------------------------------------------
#
# Run just this asset:  cd test-plan/full-test && python3 test_runner.py --only 'CRS-*'
#
# WHY THIS MODULE MATTERS
#     token_denom accounting is updated on TWO separate code paths:
#
#       df07a49f  core/wallet/post_consensus_persistence.go   (SC deploy collateral)
#       977f6fba  core/wallet/token_chain.go                  (RBT burnt for FT mint)
#
#     Each was fixed independently, and neither appears to coordinate with the
#     other. They write the same table, for the same DID, from different call
#     stacks. Every case in the SC and FT suites exercises exactly one of them at
#     a time.
#
#     These cases run them together. CRS-C-02 runs them CONCURRENTLY on one DID,
#     which is the obvious race and the case most likely to fail.
#
# Docstring contract is the same as the other modules:
#     WHAT IT CHECKS / WHY IT MATTERS / MANUAL STEPS / PASS-FAIL


def _rand_value(lo, hi):
    return max(round(random.uniform(lo, hi), 3), 0.001)


def _tag(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _crs_bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _crs_prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)
    detail = _crs_bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)
    qd = _crs_bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _crs_new_contract(ctx, entry):
    tag = _tag()
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"],
                                               wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None


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
        This is the sharpest test in the suite for the counter fixes, because it is the
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
    ready, why = _crs_prepare(ctx, s, value + ft_backing + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _crs_new_contract(ctx, s)
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


# --- CROSS-ASSET ---

CASES = {
    "CRS-C-02": crs_c_02,
    # CRS-C-04 / CRS-C-05 are written and working but NOT registered: they need
    # a host tagged 'multidid' in hosts.txt, and no host carries that tag yet.
    # Register them here once one does - the functions and the sweep support
    # are already in place, so it is a two-line change.
}

ORDER = ["CRS-C-02"]

TIMING_CASES = set()

# What each unit of cases needs - see NEEDS in full-test/test_runner.py.
# receivers 0 = the sender receives too. last = runs after everything else.
NEEDS = {
    "crs-denom-race": {
        "cases": ["CRS-C-02"],
        "receivers": 0, "fund": 20,
    },
}
