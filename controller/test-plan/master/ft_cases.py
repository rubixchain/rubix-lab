"""ft_cases.py - FT cases: mint from parts, denomination exhaustion,
interleaving with other writers of token_denom, scale mints.

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
    _sc_new_contract,
    rand_value,
)


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


# Cases where the MEASUREMENT is the result, not pass/fail. The catalogue
# asks these to record a limit, a duration, or a curve - a PASS here only
# means "it ran"; the number in the Actual column is the real output, and a
# limit dropping between releases is the regression signal. The report gives
# these their own section.


# =============================================================================
# FT (fungible tokens)
# =============================================================================


# -----------------------------------------------------------------------------
# Parts mint, burn audit, denomination accounting
# -----------------------------------------------------------------------------
#
# Run just this asset:  cd test-plan/full-test && python3 test_runner.py --only 'FT-*'
#
# Every case returns (passed, actual, note):
#     True  -> matched the catalogue's Expected Result
#     False -> did not match: a real finding, investigate
#     SKIP  -> NOT ATTEMPTED, reason in `note`. Never counted as a pass.
#
# HOW TO READ A CASE
#     Each case docstring has four fixed parts:
#         WHAT IT CHECKS  - the assertion, in plain words
#         WHY IT MATTERS  - the bug it would catch, and the product code involved
#         MANUAL STEPS    - how to run it BY HAND, no Python needed
#         PASS / FAIL     - exactly what makes it pass or fail
#     If the script and the manual steps disagree, the manual steps are the
#     specification - they are what a human can verify independently.
#
# BEFORE RUNNING ANYTHING BY HAND
#         SENDER=192.168.1.104
#         DID=$(curl -s http://$SENDER:20000/rubix/v1/dids | python3 -c \
#               'import sys,json; print(json.load(sys.stdin)["result"][0])')
#
#     Every state-changing call is a TWO-STEP password challenge. The first POST
#     returns {"result":{"id":"<reqID>"}} and NOTHING has happened yet:
#
#         curl -s -X POST http://$SENDER:20000/rubix/v1/signature \
#              -H 'Content-Type: application/json' \
#              -d '{"id":"<reqID>","password":"mypassword","signature":""}'
#
#     Some checks read Postgres. From the controller:
#         psql -h $SENDER -p 5433 -U rubix -d rubix       # password: rubixpass
#
# WHY THE PARTS CASES EXIST
#     Minting an FT burns RBT. Every other FT-M-* case mints from a wallet
#     holding hundreds of WHOLE tokens, so exactly one whole parent is burnt per
#     batch and the interesting path never runs.
#
#     A wallet can legitimately hold only FRACTIONAL RBT - receive 0.4 and 0.3
#     and you own parts, not a whole token. Minting from that wallet has to burn
#     SEVERAL part tokens to back one batch, and each burn must decrement the
#     denomination counter for what it consumed.
#
#     If it does not, token_denom keeps advertising tokens that are already
#     burnt. Nothing fails at that moment. The failure lands on a LATER,
#     unrelated transaction, which asks for rows that are no longer Free and dies
#     with "lockSelectedTokens: no tokens provided". That is why FT-P-04 mints a
#     SECOND time and FT-P-05 spends the leftovers - the first mint is rarely
#     where the damage shows.


# Repetition and interleaving cases live alongside; imported into
# CASES/ORDER below so nothing else needs to know they are separate.


# The parts wallet is built by sending several sub-1.0 amounts. These sum to
# 2.4, enough to back a small FT batch while guaranteeing no whole token is
# ever created.
PART_AMOUNTS = [0.4, 0.3, 0.5, 0.7, 0.5]

# Shared across the FT-P chain: they deliberately build on one another, exactly
# as the failure mode does.
_PARTS = {"entry": None, "ft_name": None, "minted_rbt": 0}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _ft_bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _ft_prepare(ctx, entry, need):
    """Register a quorum and ensure free balance. Returns (ok, why)."""
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    # Already-registered errors even though the insert is ON CONFLICT DO
    # NOTHING (core/wallet/quorum.go:19), so the result is ignored.
    rc.quorum_add(host, q["did"], ctx.port)

    detail = _ft_bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)

    qd = _ft_bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Strictly sequential and deliberately so: FT-P-01 builds the parts wallet,
# 02 mints from it, 03 audits that mint, 04 mints AGAIN (where corruption
# surfaces), 05 spends what is left. Running one alone reports SKIP rather
# than a misleading FAIL.


# ---------------------------------------------------------------------------
# Lanes - see sc_cases.py for the reasoning.
#
# FT-P-* is a single chain by design: 01 builds the parts wallet, 02 mints from
# it, 03 audits that mint against 02's own before/after snapshots, 04 mints
# AGAIN (where a missed decrement finally surfaces), 05 spends what is left.
# Splitting them across lanes would break every one of those dependencies, so
# they share one lane and run in order.
#
# Two hosts: a funded sender, and the receiver that becomes the parts wallet.
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Parts - exhaustion, walk-to-zero, concurrency, DB seed
# -----------------------------------------------------------------------------
# ft_cases_stress.py - repetition and interleaving against the FT burn path.
#
# The FT-P-01..05 chain proves one mint from parts behaves, and that a second
# mint still works. These push further on the specific mechanism 977f6fba
# changed:
#
#     UPDATE token_denom SET count = GREATEST(count - 1, 0) WHERE did=$1 AND denom=$2
#
# Three properties in that one statement worth testing directly: it decrements
# (not increments), it stops at zero rather than going negative, and it is keyed
# on a real denomination (the bug was that TokenValue was never copied, so every
# write hit denom=0).


def _ft_stress_name():
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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    # A deliberately small wallet, so exhaustion arrives in a handful of mints.
    # _prepare would top it back up to the lane's funding level, which is why
    # this previously never reached the floor - it drained and was refilled.
    # Drain to the budget AFTER preparing, and do not re-fund.
    budget = 4
    ready, why = _ft_prepare(ctx, s, budget + 2)
    if not ready:
        return SKIP, "setup incomplete", why
    okd, whyd = ws.drain_to(ctx, s, r, keep=budget)
    if not okd:
        return SKIP, "could not size the wallet", whyd

    minted, first_failure = 0, None
    for i in range(budget + 3):
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _ft_stress_name(), 5, 1, ctx.port)
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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    ready, why = _ft_prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting before the race", (
            db.describe_drift(before["denom_drift"]) + " - see GEN-IN-08")

    name = _ft_stress_name()

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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    rounds = 10
    ready, why = _ft_prepare(ctx, s, rounds * 2 + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    problems, done = [], 0
    for i in range(1, rounds + 1):
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _ft_stress_name(), 5, 1, ctx.port)
        if not ok:
            problems.append("round {} mint rejected: {}".format(i, str(msg)[:70]))
            break
        time.sleep(1.5)

        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            problems.append("round {}: contract generation failed".format(i))
            break
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=rand_value(0.050, 0.200),
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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    # Prepare FIRST, drain SECOND. Doing it the other way round is what stopped
    # FT-P-06 reaching the floor for two runs - _prepare topped the wallet back
    # up to the lane's funding level immediately after the drain emptied it.
    budget = 3
    ready, why = _ft_prepare(ctx, s, budget + 3)
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
        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _ft_stress_name(), 5, 1, ctx.port)
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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _r = ctx.pair(0)

    ready, why = _ft_prepare(ctx, s, 10)
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
        # A wallet can hold plenty of RBT and still no whole token - on
        # 2026-10-01 the sender had just split them all in RBT-P-04. The faucet
        # sends whole tokens, so take two from it and look again.
        ok, msg = rc.fund_did(s["host"], s["did"], 2, ctx.port)
        if not ok:
            return SKIP, "no whole denomination to seed", (
                "the wallet holds no free 1.000 token and the faucet top-up that "
                "would add two failed: {}".format(msg))
        try:
            counter = db.denom_counter(s["host"], s["did"])
            real = db.real_free_denoms(s["host"], s["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
    if _at(real, target) < 1 or _at(counter, target) < 1:
        return SKIP, "no whole denomination to seed", (
            "needs free 1.000 tokens present in BOTH token_denom and tokens, even "
            "after a 2 RBT faucet top-up; counter={} real={}".format(
                _at(counter, target), _at(real, target)))

    problems, observed = [], None
    try:
        db.writable_query(
            s["host"],
            "UPDATE token_denom SET count = 0 WHERE did = %s AND denom = %s",
            (s["did"], target), i_understand_this_writes=True)

        ok, msg, _ = rc.mint_ft(s["host"], s["did"], _ft_stress_name(), 5, 1, ctx.port)
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


# -----------------------------------------------------------------------------
# Scale - production-level volume
# -----------------------------------------------------------------------------
# ft_cases_scale.py - the FT burn path under sustained load.
#
# Run as a stress selection, after the functional
# suite passes.
#
# The functional FT cases mint twice. 977f6fba changed a statement that runs on
# EVERY burn, so the interesting question is what a few hundred of them do to the
# counter - and whether the two writers of token_denom stay consistent when both
# are busy at once rather than taking turns.


def _ft_scale_name():
    return "sx" + "".join(random.choice(string.ascii_lowercase + string.digits)
                          for _ in range(8))


def _ft_scale_scale(ctx):
    return float(getattr(ctx.args, "scale", 1.0) or 1.0)


def _ft_scale_n(ctx, base, floor=3):
    return max(floor, int(base * _ft_scale_scale(ctx)))


# ---------------------------------------------------------------------------
# FT-X-01
# ---------------------------------------------------------------------------

def ft_x_01(ctx, ci):
    """
    FT-X-01 - Many FT mints with the counter checked at every checkpoint.

    WHAT IT CHECKS
        Mint FT batches continuously - 100 by default - re-checking the
        denomination counter every 10, and report the mint number at which it
        first disagreed with reality.

    WHY IT MATTERS
        977f6fba changed a statement that executes once per burnt RBT. The
        functional cases run it a handful of times; production runs it
        constantly. A decrement that is right 99% of the time still corrupts a
        wallet within a day.

        Reporting the FIRST bad checkpoint gives a number to compare between
        releases. It also distinguishes an immediate bug (fails at mint 10)
        from an accumulating one (fails at mint 80), which need different
        fixes.

    MANUAL STEPS
        Loop the FT mint, and every 10 iterations compare:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;

    PASS / FAIL
        PASS  every checkpoint consistent; burnt total matches RBT consumed
        FAIL  reports the first bad checkpoint and how far in it was
        RECORD  mints rejected for lack of backing are counted, not failed
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    total = _ft_scale_n(ctx, 100, floor=8)
    checkpoint = max(4, total // 10)
    backing = 1

    ready, why = _ft_prepare(ctx, s, total * backing + 20)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    done, rejected, first_drift = 0, 0, None
    for i in range(1, total + 1):
        ok, _msg, _ = rc.mint_ft(s["host"], s["did"], _ft_scale_name(), 5, backing, ctx.port)
        if ok:
            done += 1
        else:
            rejected += 1
        if i % checkpoint == 0 and first_drift is None:
            time.sleep(1.5)
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            d = db.new_drift(start, now)
            if d:
                first_drift = (i, db.describe_drift(d))
        time.sleep(0.4)

    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)
    d = db.delta(start, end)

    problems = []
    if first_drift:
        problems.append("counter FIRST drifted at mint {} of {}: {}{}".format(
            first_drift[0], total, first_drift[1],
            " - early, so this is an immediate defect rather than accumulation"
            if first_drift[0] <= checkpoint * 2 else
            " - late, so this accumulates rather than failing outright"))
    elif drift:
        problems.append("counter drifted by the end: " + db.describe_drift(drift))

    burnt = d["burnt_for_ft"]
    # Free + pledged: a DID that was a quorum keeps getting old pledges back
    # into Free during the case (21.8 RBT on 2026-10-01), which made free alone
    # look like value had gone missing.
    consumed = -(d["free"] + d["pledged"])
    if done and abs(burnt - consumed) > max(0.05, consumed * 0.01):
        problems.append("free + pledged fell {:.3f} but {:.3f} was recorded as burnt "
                        "across {} mint(s) - {:.3f} unaccounted for".format(
                            consumed, burnt, done, abs(consumed - burnt)))

    note = "; ".join(problems)
    if rejected and not problems:
        note = "{} mint(s) rejected for lack of backing - expected once the " \
               "wallet runs low".format(rejected)

    return (not problems), "{}/{} minted, burnt {:.3f}, counter {}".format(
        done, total, burnt, "ok" if not drift else "DRIFT"), note


# ---------------------------------------------------------------------------
# FT-X-02
# ---------------------------------------------------------------------------

def ft_x_02(ctx, ci):
    """
    FT-X-02 - Sustained concurrent minting from several wallets at once.

    WHAT IT CHECKS
        Every host in the lane mints repeatedly and simultaneously. Afterwards
        every wallet's counter must still match its real free tokens.

    WHY IT MATTERS
        FT-P-07 races a mint against one transfer, once. This runs many mints
        from many DIDs continuously, which is what actually exercises whatever
        serialises the counter update - per-DID locking, transaction isolation,
        or nothing at all.

        Because each wallet is a different DID, a failure here is NOT the
        same-row contention FT-P-07 probes. It would mean the burn path
        interferes across DIDs, which would be a much broader defect.

    MANUAL STEPS
        Start the FT mint loop on several hosts at once, then check each
        wallet's counter against its real free tokens.

    PASS / FAIL
        PASS  every wallet consistent
        FAIL  the report names which wallets drifted - more than one points at
              a shared resource rather than a per-wallet bug
        SKIP  fewer than 2 hosts
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    seen, hosts = set(), []
    for e in list(ctx.senders) + list(ctx.receivers):
        if e["host"] not in seen:
            seen.add(e["host"])
            hosts.append(e)
    if len(hosts) < 2:
        return SKIP, "need 2+ hosts", "unit has {}".format(len(hosts))

    per_host = _ft_scale_n(ctx, 25, floor=4)
    for e in hosts:
        ready, why = _ft_prepare(ctx, e, per_host + 12)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    try:
        before = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def worker(e):
        ok_n, bad_n = 0, 0
        for _ in range(per_host):
            ok, _m, _ = rc.mint_ft(e["host"], e["did"], _ft_scale_name(), 5, 1, ctx.port)
            ok_n, bad_n = (ok_n + 1, bad_n) if ok else (ok_n, bad_n + 1)
            time.sleep(0.3)
        return e["host"], ok_n, bad_n

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = list(pool.map(worker, hosts))
    time.sleep(SETTLE * 3)

    try:
        after = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    drifted = []
    for h in before:
        d = db.new_drift(before[h], after[h])
        if d:
            drifted.append("{}: {}".format(h, db.describe_drift(d)))

    ok_total = sum(o for _h, o, _b in results)
    bad_total = sum(b for _h, _o, b in results)

    note = ""
    if drifted:
        note = "; ".join(drifted[:4])
        note += (" - more than one wallet drifted, which points at a shared "
                 "resource rather than a per-wallet bug"
                 if len(drifted) > 1 else
                 " - a single wallet drifted, so this is per-DID rather than shared")

    return (not drifted), "{} wallet(s) x {} mints: {} ok, {} rejected".format(
        len(hosts), per_host, ok_total, bad_total), note


# --- FT (fungible tokens) ---

CASES = {

    # Direct tests of the GREATEST(count-1,0) floor, and of the burn
    # path interleaved with the other writers of token_denom.
    "FT-P-06": ft_p_06,
    "FT-P-07": ft_p_07,
    "FT-P-08": ft_p_08,

    # The three H3 branches the runs had never actually reached. FT-P-06 walks
    # into a REJECTED mint, so on this fleet it hits the lock leak before it
    # ever reaches the decrement; FT-P-09 removes the failing mint. FT-P-10 is
    # the multi-part burn, standalone so it cannot be skipped by FT-P-01.
    # FT-DB-04 is the only way to reach the clamp AT zero.
    "FT-P-09": ft_p_09,
    "FT-DB-04": ft_db_04,

    # Production-level volume - see sc_cases_scale.py.
    "FT-X-01": ft_x_01,
    "FT-X-02": ft_x_02,
}

ORDER = ["FT-P-06", "FT-P-09", "FT-P-07", "FT-P-08",
         "FT-DB-04",
         "FT-X-01", "FT-X-02"]

TIMING_CASES = set()

# What each unit of cases needs - see NEEDS in full-test/test_runner.py.
# receivers 0 = the sender receives too. last = runs after everything else.
NEEDS = {

    # FT-P-06 deliberately runs its wallet dry to reach the counter floor, so
    # it cannot share with anything. FT-P-07/08 interleave the burn path with
    # the other writers of token_denom and need room to work.
    "ft-exhaustion": {
        "cases": ["FT-P-06"],
        "receivers": 1, "fund": 4,
    },

    # FT-P-09 sizes its wallet down to a few whole tokens and needs a sink to
    # drain into, so one receiver too. It must not share with anything that funds.
    "ft-floor-walk": {
        "cases": ["FT-P-09"],
        "receivers": 1, "fund": 8,
    },


    # FT-DB-04 WRITES to token_denom, so it runs LAST - after every other
    # unit has finished (the catalogue's DB-SEED rule).
    "ft-seed-floor": {
        "cases": ["FT-DB-04"],
        "receivers": 0, "fund": 12, "last": True,
    },
    "ft-interleave": {
        "cases": ["FT-P-07", "FT-P-08"],
        "receivers": 1, "fund": 35,
    },

    # --- scale (stress) ---------------------------------------------
    # FT-X-01 burns one RBT per mint for 100 mints, so it needs real balance.
    "ft-scale-mints": {
        "cases": ["FT-X-01"],
        "receivers": 0, "fund": 120,
    },
    "ft-scale-concurrent": {
        "cases": ["FT-X-02"],
        "receivers": 3, "fund": 40,
    },
}
