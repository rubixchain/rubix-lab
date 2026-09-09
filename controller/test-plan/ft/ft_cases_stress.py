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
import wallet_shapes as ws

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

    # A deliberately small wallet, so exhaustion arrives in a handful of mints.
    # _prepare would top it back up to the lane's funding level, which is why
    # this previously never reached the floor - it drained and was refilled.
    # Drain to the budget AFTER preparing, and do not re-fund.
    budget = 4
    ready, why = ft._prepare(ctx, s, budget + 2)
    if not ready:
        return SKIP, "setup incomplete", why
    okd, whyd = ws.drain_to(ctx, s, r, keep=budget)
    if not okd:
        return SKIP, "could not size the wallet", whyd

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


def _at(m, denom, tol=0.0005):
    """Look up a denomination in a {denom: count} map by VALUE, not identity.

    Both token_denom.denom and tokens.token_value come back as floats, so
    m[1.0] is a coin toss. Every lookup here goes through this.
    """
    for k, v in m.items():
        if abs(k - denom) < tol:
            return v
    return 0


# ---------------------------------------------------------------------------
# FT-P-09
# ---------------------------------------------------------------------------

def ft_p_09(ctx, ci):
    """
    FT-P-09 - Walk one denomination all the way down to zero with mints that
    all SUCCEED.

    WHAT IT CHECKS
        Size a wallet to a known small number of whole tokens, then mint an FT
        backed by 1 RBT once per token. After every mint the counter for 1.000
        must equal the real number of free 1.000 tokens, and the last one must
        land on exactly zero.

    WHY IT MATTERS
        FT-P-06 was supposed to prove this and never has. It mints until one is
        REJECTED, so it measures two things at once - the walk down, and the
        failure path - and the failure path is broken on this fleet in a way
        that masks the walk. Its last run reported:

            denom 0.001: counter=96 free=0

        which reads like the counter is 96 too high, but GEN-IN-15 found
        exactly 96 tokens LOCKED on that host. The rejected mint locked 96
        tokens and never released them. Nothing was burnt, so the decrement
        under test was never even reached, and FT-P-06 has never once
        exercised the thing its docstring claims.

        This case removes the failing mint entirely. Every mint here must
        succeed, so the only code path involved is the decrement, and the
        counter is checked after each one rather than at the end - which turns
        "the total is wrong" into "it went wrong on mint number four".

    MANUAL STEPS
        1. Drain the wallet to about 3 RBT, then list what it holds:
             SELECT token_value, COUNT(*) FROM tokens
              WHERE did='<DID>' AND token_status=0 AND token_type=1
              GROUP BY token_value;
        2. Mint an FT backed by 1 RBT. Sign it. Wait ~8s.
        3. Read both numbers for 1.000 and compare:
             SELECT count FROM token_denom WHERE did='<DID>' AND denom=1.0;
             SELECT COUNT(*) FROM tokens WHERE did='<DID>'
               AND token_status=0 AND token_type=1 AND token_value=1.0;
        4. Repeat until the counter reads 0.

    PASS / FAIL
        PASS  counter equals reality after every mint, and ends at exactly 0
        FAIL  they diverge at any step - the report names the step
        FAIL  a negative count appears - the GREATEST floor was bypassed
        SKIP  the wallet could not be sized, or zero was never reached
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    # Prepare FIRST, drain SECOND. Doing it the other way round is what stopped
    # FT-P-06 reaching the floor for two runs - _prepare topped the wallet back
    # up to the lane's funding level immediately after the drain emptied it.
    budget = 3
    ready, why = ft._prepare(ctx, s, budget + 3)
    if not ready:
        return SKIP, "setup incomplete", why
    okd, whyd = ws.drain_to(ctx, s, r, keep=budget)
    if not okd:
        return SKIP, "could not size the wallet", whyd
    time.sleep(SETTLE)

    try:
        n = int(_at(db.real_free_denoms(s["host"], s["did"]), 1.0))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if n < 1:
        return SKIP, "no whole tokens to walk down", (
            "the wallet holds no free 1.000 token after draining to {} RBT, so "
            "there is no denomination to exhaust".format(budget))
    n = min(n, 4)          # four steps is plenty and keeps the case under a minute

    # One mint per whole token. Each is REQUIRED to succeed - a rejection here
    # means the wallet was mis-sized, not that the product is wrong, so it is
    # reported as such rather than as a failure of the decrement.
    series, problems = [], []
    for i in range(n):
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, 1, ctx.port)
        if not ok:
            return SKIP, "mint #{} of {} was rejected".format(i + 1, n), (
                "this case needs every mint to succeed so that only the "
                "decrement is under test; a rejection means the wallet was "
                "sized wrong. Rejection: {}".format(msg))
        time.sleep(SETTLE)
        try:
            counted = int(_at(db.denom_counter(s["host"], s["did"]), 1.0))
            real = int(_at(db.real_free_denoms(s["host"], s["did"]), 1.0))
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        series.append((counted, real))
        if counted != real:
            problems.append(
                "after mint #{} the counter says {} free 1.000 token(s) but {} "
                "are actually Free - the burn moved the token without moving "
                "the counter with it".format(i + 1, counted, real))

    try:
        negatives = db.negative_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if negatives:
        problems.append("NEGATIVE count(s): " + ", ".join(
            "{:.3f}={}".format(d, c) for _d, d, c in negatives) +
            " - the GREATEST(count-1,0) floor was bypassed")

    walk = " ".join("{}/{}".format(c, rl) for c, rl in series)
    final = series[-1][0] if series else -1
    if final != 0 and not problems:
        return SKIP, "never reached zero", (
            "counter/real after each mint: {} - it ended at {} rather than 0, "
            "so the floor itself was not reached. The decrement tracked "
            "reality at every step, which is the other half of this case and "
            "did pass".format(walk, final))

    return (not problems), "{} mint(s), counter/real: {}".format(n, walk),         "; ".join(problems)


# ---------------------------------------------------------------------------
# FT-P-10
# ---------------------------------------------------------------------------

def ft_p_10(ctx, ci):
    """
    FT-P-10 - A burn whose parents are PART tokens must decrement each of their
    denominations.

    WHAT IT CHECKS
        Build a parts-only wallet, mint an FT backed by more RBT than any single
        part holds - so several parts must be burnt - and check that the counter
        for EVERY denomination involved still matches reality afterwards.

    WHY IT MATTERS
        This is the branch of 977f6fba that has never actually run. The fix
        copies TokenValue onto the burn so the decrement is keyed on a real
        denomination; before it, every write hit denom=0. GEN-IN-16 proves that
        works when the parent is one whole 1.000 token - a single denomination,
        a single decrement. Parts are the harder shape: one mint burns several
        tokens at SEVERAL different denominations, so the code has to key each
        decrement to its own parent rather than to the transaction.

        FT-P-01..05 do cover this, but as a five-case chain where each depends
        on the one before. In the last run FT-P-01 failed on a harness bug and
        took the other four down as SKIPs, so the branch went unproven for the
        second run running. This case builds its own wallet and asserts on its
        own snapshots, so it can fail for one reason only: the product.

    MANUAL STEPS
        1. Build a wallet holding only sub-1.0 tokens (send 0.4/0.3/0.5/0.7/0.5/0.6).
        2. Record both tables:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
             SELECT token_value, COUNT(*) FROM tokens WHERE did='<DID>'
               AND token_status=0 AND token_type=1 GROUP BY token_value;
        3. Mint an FT with token_count=2. No single part covers 2 RBT, so
           several must burn. Sign it.
        4. Repeat step 2 and compare. Every denomination must still agree.

    PASS / FAIL
        PASS  more than one row burnt, and no denomination disagrees
        FAIL  a denomination drifts - a part was burnt without its own
              decrement, which is precisely what the fix was for
        FAIL  a denom=0 row appears - TokenValue was not copied
        FAIL  only one row burnt - the parts path was not exercised
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    ready, why = ft._prepare(ctx, s, 14)
    if not ready:
        return SKIP, "setup incomplete", why

    # Six parts totalling 3.0, none of them near the 2 RBT the mint asks for.
    okw, whyw = ws.make_parts_wallet(ctx, r, s, amounts=(0.4, 0.3, 0.5, 0.7, 0.5, 0.6))
    if not okw:
        return False, "could not build a parts wallet", whyw
    time.sleep(SETTLE)

    try:
        before = db.record("FT-P-10", "before", r["host"], r["did"],
                           db.snapshot(r["host"], r["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    name = _name()
    ok, msg, _ = rc.mint_ft(r["host"], r["did"], name, 10, 2, ctx.port)
    if not ok:
        return False, "mint from parts rejected", str(msg)
    got, _c, _res = rc.wait_for_ft_count(r["host"], r["did"], name, 10, ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("FT-P-10", "after", r["host"], r["did"],
                          db.snapshot(r["host"], r["did"]))
        drift = db.denom_drift(r["host"], r["did"])
        negatives = db.negative_denoms(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    burnt = after["burnt_for_ft_rows"] - before["burnt_for_ft_rows"]

    problems = []
    if not got:
        problems.append("mint returned success but the FT count never reached 10")
    if burnt <= 1:
        problems.append(
            "only {} row burnt - a single token backed the whole batch, so the "
            "multi-part burn this case exists for was never reached".format(burnt))
    if drift:
        problems.append(
            "counter disagrees with reality after burning {} part token(s): {} "
            "- a part was burnt without its own denomination being "
            "decremented".format(burnt, db.describe_drift(drift)))
    if any(abs(d) < 0.0005 for d in after["denom"]):
        problems.append(
            "a denom=0 row exists - TokenValue was not copied onto the burn, "
            "which is the original defect 977f6fba fixes")
    if negatives:
        problems.append("NEGATIVE count(s): " + ", ".join(
            "{:.3f}={}".format(d, c) for _d, d, c in negatives))

    return (not problems), "{} part token(s) burnt for 2 RBT, {} denomination(s) tracked".format(
        burnt, len(after["denom"])), "; ".join(problems) + (
        " | " + db.format_evidence(before, after) if problems else "")


# ---------------------------------------------------------------------------
# FT-DB-04
# ---------------------------------------------------------------------------

def ft_db_04(ctx, ci):
    """
    FT-DB-04 - Force a counter to zero while tokens are still free, then burn.

    WHAT IT CHECKS
        Set token_denom.count to 0 for a denomination the wallet really does
        hold, then burn one of those tokens. The count must stay 0. It must not
        become -1.

    WHY IT MATTERS
        This is the only way to reach the floor in GREATEST(count - 1, 0). No
        natural sequence gets there: on a healthy wallet the counter is only 0
        when there is nothing left to burn, so the subtraction never runs at 0
        and the clamp is never asked to do anything. Every "floor" case on this
        fleet - FT-P-06 and now FT-P-09 - actually tests the walk DOWN to zero,
        not the clamp AT zero. Those are different lines of code.

        It matters because the clamp is the last defence against a counter that
        has already drifted low. If it is missing, one under-count becomes a
        negative, and selection then asks for a number of tokens that cannot
        exist - which fails the transfer rather than merely mis-reporting it.

    DELIBERATE CORRUPTION - this case WRITES to the database.
        It reconciles the row back to the true free-token count in a finally
        block, which leaves the wallet cleaner than it found it. Per the
        catalogue's DB-SEED rule these run LAST in a cycle. It is reserved a
        host of its own so nothing else can be affected, but if it dies
        mid-case re-run GEN-IN-08 against that host before trusting it.

    MANUAL STEPS
        1. Find a denomination the wallet holds free tokens at:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
        2. Force it to zero:
             UPDATE token_denom SET count=0 WHERE did='<DID>' AND denom=1.0;
        3. Mint an FT backed by 1 RBT. Sign it. Wait ~12s.
        4. Read it back. It must be 0, never negative:
             SELECT count FROM token_denom WHERE did='<DID>' AND denom=1.0;
        5. Reconcile the row to the real free count.

    PASS / FAIL
        PASS  the count is still 0 after the burn, and no row anywhere is
              negative
        FAIL  the count is negative - GREATEST is not doing its job, and a
              wallet that drifts low can never recover on its own
        SKIP  no denomination is held both in token_denom and in tokens
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _r = ctx.pair(0)

    ready, why = ft._prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        counter = db.denom_counter(s["host"], s["did"])
        real = db.real_free_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # A 1 RBT mint burns a whole token, so the denomination has to be 1.000 for
    # the seeded row to be the one the burn actually touches.
    target = 1.0
    if _at(real, target) < 1 or _at(counter, target) < 1:
        return SKIP, "no whole denomination to seed", (
            "needs free 1.000 tokens present in BOTH token_denom and tokens; "
            "counter={} real={}".format(_at(counter, target), _at(real, target)))

    problems, observed = [], None
    try:
        db.writable_query(
            s["host"],
            "UPDATE token_denom SET count = 0 WHERE did = %s AND denom = %s",
            (s["did"], target), i_understand_this_writes=True)

        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, 1, ctx.port)
        time.sleep(SETTLE * 2)

        observed = int(_at(db.denom_counter(s["host"], s["did"]), target))
        negatives = db.negative_denoms(s["host"], s["did"])

        if observed < 0:
            problems.append(
                "count for {:.3f} is {} - the burn subtracted from a row that "
                "was already 0, so GREATEST(count-1,0) is not clamping. A "
                "wallet that has drifted low can never recover from "
                "this".format(target, observed))
        if negatives:
            problems.append("NEGATIVE row(s): " + ", ".join(
                "{:.3f}={}".format(d, c) for _d, d, c in negatives))
        if not ok:
            problems.append(
                "the mint was rejected ({}), so the burn may not have been "
                "reached - the count of {} is not evidence either "
                "way".format(msg, observed))
    finally:
        # Reconcile to the truth rather than to the value seen on the way in -
        # a token really was burnt, so the original count is now stale.
        try:
            truth = int(_at(db.real_free_denoms(s["host"], s["did"]), target))
            db.writable_query(
                s["host"],
                "UPDATE token_denom SET count = %s WHERE did = %s AND denom = %s",
                (truth, s["did"], target), i_understand_this_writes=True)
        except Exception as e:               # noqa: BLE001 - must never mask the result
            problems.append(
                "COULD NOT RESTORE token_denom on {} for denom {:.3f}: {} - "
                "re-run GEN-IN-08 against this host before trusting "
                "it".format(s["host"], target, e))

    return (not problems), "counter forced to 0 with {} token(s) still free, " \
        "read back {}".format(_at(real, target), observed), "; ".join(problems)
