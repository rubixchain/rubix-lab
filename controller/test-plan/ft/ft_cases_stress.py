#!/usr/bin/env python3
"""
ft_cases_stress.py - repetition and interleaving against the FT burn path.

Imported by ft_cases.py.

The FT-P-01..05 chain proves one mint from parts behaves, and that a second
mint still works. These push further on the specific mechanism 977f6fba
changed:

    UPDATE token_denom SET count = GREATEST(count - 1, 0) WHERE did=$1 AND denom=$2

Three properties in that one statement worth testing directly: it decrements
(not increments), it stops at zero rather than going negative, and it is keyed
on a real denomination (the bug was that TokenValue was never copied, so every
write hit denom=0).
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


def _link():
    import ft_cases
    return ft_cases


def _name():
    return "st" + "".join(random.choice(string.ascii_lowercase + string.digits)
                          for _ in range(8))


# ---------------------------------------------------------------------------
# FT-P-06
# ---------------------------------------------------------------------------

def ft_p_06(ctx, ci):
    """
    FT-P-06 - Mint repeatedly until a denomination is exhausted.

    WHAT IT CHECKS
        Mint FTs from the same wallet until it runs out of backing. The counter
        must reach zero and stop - never go negative - and the mint that
        finally fails must fail cleanly, leaving the counter matching reality.

    WHY IT MATTERS
        This is the direct test of the GREATEST(count - 1, 0) floor. That floor
        was added because the burn path could be reached more than once for the
        same token; without it the counter goes negative and selection starts
        asking for an impossible number of tokens.

        The last mint is the interesting one. Running out of backing is a
        legitimate outcome, but it must leave the wallet consistent: a failed
        mint that already decremented the counter is worse than one that never
        started, because the wallet then advertises less than it holds and the
        loss is permanent.

    MANUAL STEPS
        On a wallet with a known small balance, mint FTs backed by 1 RBT in a
        loop until a mint is rejected. After each, and after the failure:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;

    PASS / FAIL
        PASS  counter floors at zero, never negative, and matches reality after
              the failing mint
        FAIL  a negative count appears -> the floor was bypassed
        FAIL  the counter disagrees with reality after the failed mint -> the
              failure path decremented without burning
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    # A deliberately small wallet, so exhaustion arrives in a handful of mints
    # rather than hundreds.
    budget = 4
    ready, why = ft._prepare(ctx, s, budget + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    minted, first_failure = 0, None
    for i in range(budget + 3):
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, 1, ctx.port)
        if not ok:
            first_failure = (i + 1, str(msg))
            break
        minted += 1
        time.sleep(2)
    time.sleep(SETTLE)

    try:
        drift = db.denom_drift(s["host"], s["did"])
        negatives = db.negative_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if negatives:
        problems.append("NEGATIVE denomination count(s): " + ", ".join(
            "{:.3f}={}".format(d, c) for _did, d, c in negatives) +
            " - the GREATEST(count-1,0) floor was bypassed")
    if drift:
        problems.append("counter disagrees with reality after {} mint(s){}: {}".format(
            minted,
            " and a rejected mint" if first_failure else "",
            db.describe_drift(drift)) +
            " - the failing mint decremented without burning")
    if first_failure is None:
        return SKIP, "never exhausted", (
            "{} mint(s) all succeeded on a {} RBT budget - the wallet was "
            "larger than intended, so the floor was never reached".format(
                minted, budget))

    return (not problems), "{} mint(s) then rejected at #{}".format(
        minted, first_failure[0]), "; ".join(problems)


# ---------------------------------------------------------------------------
# FT-P-07
# ---------------------------------------------------------------------------

def ft_p_07(ctx, ci):
    """
    FT-P-07 - FT mint and an RBT transfer fired at the same time.

    WHAT IT CHECKS
        A mint (which burns RBT) and an ordinary transfer (which moves RBT)
        start together on one wallet. Both should complete correctly and the
        counter must still match reality.

    WHY IT MATTERS
        Both operations decrement token_denom for the same DID, by different
        code paths - the mint through token_chain.go (the path 977f6fba fixed),
        the transfer through the normal post-consensus route. Two concurrent
        read-modify-write cycles on the same rows.

        This is the FT-side sibling of CRS-C-02. It is cheaper to run and needs
        no smart contract, so if both fail the common factor is concurrent
        denom writes generally, not anything specific to contracts.

    MANUAL STEPS
        Snapshot the counter, then in two terminals at once: POST an FT mint
        and POST an RBT transfer from the same DID. Sign both. Wait, then
        compare the counter against the real free tokens.

    PASS / FAIL
        PASS  both succeed, counter consistent
        FAIL  counter drifts -> concurrent decrements on one DID are not safe
        SKIP  counter already drifting beforehand
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    ready, why = ft._prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting before the race", (
            db.describe_drift(before["denom_drift"]) + " - see GEN-IN-08")

    name = _name()

    def do_mint():
        return ("ft-mint",) + rc.mint_ft(s["host"], s["did"], name, 10, 2, ctx.port)[:2]

    def do_transfer():
        return ("rbt-transfer",) + rc.initiate_transaction(
            s["host"], s["did"], r["did"], rbt=1.0,
            memo="FT-P-07 race", port=ctx.port)[:2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_mint)
        f2 = pool.submit(do_transfer)
        outcomes = [f1.result(), f2.result()]

    time.sleep(SETTLE * 3)
    try:
        after = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)
    d = db.delta(before, after)

    problems = ["{} rejected: {}".format(n, str(m)[:80])
                for n, ok, m in outcomes if not ok]
    if drift:
        problems.append("counter drifted after a concurrent mint + transfer: "
                        + db.describe_drift(drift) +
                        " - both decrement token_denom for this DID by different "
                        "paths, and they interfered")

    return (not problems), "mint + transfer concurrently | free {:+.3f} burnt {:+.3f} | counter {}".format(
        d["free"], d["burnt_for_ft"], "ok" if not drift else "DRIFT"), "; ".join(problems)


# ---------------------------------------------------------------------------
# FT-P-08
# ---------------------------------------------------------------------------

def ft_p_08(ctx, ci):
    """
    FT-P-08 - Alternate FT mint and SC deploy ten times on one wallet.

    WHAT IT CHECKS
        Ten rounds of "mint an FT, then deploy a valued contract" on the same
        wallet. Every operation succeeds and the counter is still correct at
        the end.

    WHY IT MATTERS
        The sequential version of CRS-C-02. Both fixes write token_denom for
        this DID; here they take turns instead of racing.

        That distinction is the diagnosis. If this passes and CRS-C-02 fails,
        the paths are individually correct and the problem is concurrency. If
        BOTH fail, the two fixes disagree about the counter even when nothing
        is racing - an ordering bug, which is easier to fix and worse to ship.

        Ten rounds because a single alternation is unlikely to accumulate
        enough error to be visible.

    MANUAL STEPS
        Loop ten times: mint an FT backed by 1 RBT, then deploy a contract with
        a small value. Compare the counter against reality at the end.

    PASS / FAIL
        PASS  all twenty operations succeed, counter consistent
        FAIL  counter drifts -> read with CRS-C-02 to tell ordering from racing
        FAIL  a LATER round fails while earlier ones passed -> accumulated state
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    rounds = 10
    ready, why = ft._prepare(ctx, s, rounds * 2 + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    import sc_cases  # for contract generation and value helpers
    problems, done = [], 0
    for i in range(1, rounds + 1):
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, 1, ctx.port)
        if not ok:
            problems.append("round {} mint rejected: {}".format(i, str(msg)[:70]))
            break
        time.sleep(1.5)

        sc_id, err = sc_cases._new_contract(ctx, s)
        if err:
            problems.append("round {}: contract generation failed".format(i))
            break
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=sc_cases.rand_value(0.050, 0.200),
                                       data="alternating round {}".format(i),
                                       port=ctx.port)
        if not ok:
            problems.append("round {} deploy rejected: {}".format(i, str(msg)[:70]))
            break
        done = i
        time.sleep(1.5)

    time.sleep(SETTLE * 2)
    try:
        after = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    if drift:
        problems.append("counter drifted after {} alternating round(s): {} - "
                        "compare with CRS-C-02: a failure HERE too means the two "
                        "denom paths disagree even without concurrency".format(
                            done, db.describe_drift(drift)))
    if problems and done > 0 and done < rounds:
        problems.append("rounds 1-{} succeeded first, so this is accumulated "
                        "state rather than an immediate defect".format(done))

    return (not problems), "{}/{} alternating round(s), counter {}".format(
        done, rounds, "ok" if not drift else "DRIFT"), "; ".join(problems)
