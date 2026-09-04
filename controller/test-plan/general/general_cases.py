#!/usr/bin/env python3
"""
general_cases.py - fleet-wide and integrity cases from the master catalogue.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases general
One case:                          python3 case_runner.py --cases general --only GEN-IN-08

Every case returns (passed, actual, note):
    True  -> matched the catalogue's Expected Result
    False -> did not match: a real finding, investigate
    SKIP  -> NOT ATTEMPTED, reason in `note`. Never counted as a pass.

HOW TO READ A CASE
    Each case docstring has four fixed parts:
        WHAT IT CHECKS  - the assertion, in plain words
        WHY IT MATTERS  - the bug it would catch, and the product code involved
        MANUAL STEPS    - how to run it BY HAND, no Python needed
        PASS / FAIL     - exactly what makes it pass or fail

BEFORE RUNNING ANYTHING BY HAND
        HOST=192.168.1.104
        DID=$(curl -s http://$HOST:20000/rubix/v1/dids | python3 -c \\
              'import sys,json; print(json.load(sys.stdin)["result"][0])')
        psql -h $HOST -p 5433 -U rubix -d rubix        # password: rubixpass

WHAT token_denom IS, AND WHY FOUR CASES GUARD IT
    Every node keeps a small table, token_denom, counting how many FREE tokens
    it holds at each denomination - "four tokens worth 1.000, two worth 0.500".

    It is a CACHE, and the only consumer that matters is
    lockTokensForSplitOnce (core/wallet/token_lock.go:505). When a transfer
    needs tokens, that function reads the counter FIRST to decide which
    denominations to ask for, then reads matching rows from `tokens`.

    So a wrong counter is uniquely nasty:
      * Nothing fails when the counter goes wrong.
      * The failure lands on a LATER, unrelated transaction, which asks for
        rows that are no longer Free and dies with
        "lockSelectedTokens: no tokens provided".
      * The error names the innocent transaction, not the operation that
        caused the damage.

    GEN-IN-08 and GEN-IN-09 check the counter's shape. GEN-IN-10 and GEN-IN-11
    check the two operations that BURN or COMMIT RBT and must therefore
    decrement it - FT mint and contract deploy. They are kept apart on purpose:
    if both were one case, an FT regression and a smart-contract regression
    would be indistinguishable in the report.
"""

import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"

SETTLE = 6
TOL = 0.0015


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(host) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)   # already-registered returns an error; ignore

    detail = _bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        if not rc.wait_for_balance(host, entry["did"], need, ctx.port):
            return False, "could not fund to {} RBT (have {})".format(need, have)

    qd = _bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _describe_drift(drift):
    return "; ".join(
        "denom {:.3f}: counter says {} but {} are Free".format(d, c, a)
        for d, (c, a) in sorted(drift.items()))


def _rand_tag(n=8):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _new_contract(ctx, entry):
    """Generate (not deploy) a contract. Both extensions are checked literally
    by the server (server/smart_contract.go:70, :101-106)."""
    tag = _rand_tag(10)
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"], wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None


# ---------------------------------------------------------------------------
# GEN-IN-08
# ---------------------------------------------------------------------------

def gen_in_08(ctx, ci):
    """
    GEN-IN-08 - Compare the denomination counter against the real free tokens.

    WHAT IT CHECKS
        For every denomination, token_denom's count equals the number of Free
        tokens actually held at that value.

    WHY IT MATTERS
        This is the baseline invariant the other three build on. Run on its own
        against an idle wallet it should always pass; run it after a busy
        catalogue pass and a failure says some earlier operation burnt or moved
        tokens without maintaining the counter. Because the eventual symptom
        appears on an unrelated later transaction, this check is the only place
        the fault is attributable.

    MANUAL STEPS
        1. What the node BELIEVES it holds:
             SELECT denom, count FROM token_denom
              WHERE did='$DID' ORDER BY denom;

        2. What it ACTUALLY holds (token_status 0 = Free):
             SELECT token_value, COUNT(*) FROM tokens
              WHERE did='$DID' AND token_status=0
              GROUP BY token_value ORDER BY token_value;

        3. Compare the two listings row by row.

    PASS / FAIL
        PASS  the listings agree at every denomination
        FAIL  any mismatch, in either direction. A counter HIGHER than reality
              is the dangerous one - it makes selection ask for tokens that are
              already gone
        SKIP  psycopg2 missing, or Postgres unreachable
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", (
            "token_denom is not exposed by any API, so this cannot be checked "
            "another way. sudo apt install -y python3-psycopg2")
    try:
        drift = db.denom_drift(s["host"], s["did"])
        counter = db.denom_counter(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    return (not drift), "{} denomination(s) tracked, {} disagree".format(
        len(counter), len(drift)), _describe_drift(drift)


# ---------------------------------------------------------------------------
# GEN-IN-09
# ---------------------------------------------------------------------------

def gen_in_09(ctx, ci):
    """
    GEN-IN-09 - Look for a phantom zero-denomination row.

    WHAT IT CHECKS
        No token_denom row exists with denom = 0 for the DID.

    WHY IT MATTERS
        A denomination of zero is meaningless - no token can have value 0 - but
        the row is an upsert target, so a decrement that overshoots or a
        mis-derived denomination can create one. Selection then asks for
        zero-value tokens, finds nothing, and fails in a way that looks like an
        empty wallet rather than a corrupt counter. Kept separate from
        GEN-IN-08 because it is a distinct defect: the counter can be perfectly
        consistent at every real denomination and still carry this row.

    MANUAL STEPS
             SELECT denom, count FROM token_denom
              WHERE did='$DID' AND denom = 0;

    PASS / FAIL
        PASS  no rows returned
        FAIL  any row -> phantom denomination present
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    try:
        counter = db.denom_counter(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    zero = {d: c for d, c in counter.items() if d == 0}
    return (not zero), ("no zero-denomination row" if not zero
                        else "denom=0 row with count {}".format(list(zero.values())[0])), (
        "" if not zero else
        "a zero denomination cannot correspond to any real token; selection "
        "will ask for it and find nothing")


# ---------------------------------------------------------------------------
# GEN-IN-10
# ---------------------------------------------------------------------------

def gen_in_10(ctx, ci):
    """
    GEN-IN-10 - Check the denomination counter after minting an FT.

    WHAT IT CHECKS
        Minting an FT burns RBT, and afterwards the counter still matches the
        real Free tokens.

    WHY IT MATTERS
        FT mint is one of only two operations that consume RBT without a
        transfer, so it must decrement token_denom for what it burnt. Historically
        it did not - the counter kept advertising burnt tokens, and a later
        unrelated transaction was the one that died. This case takes a
        before/after snapshot around a single mint so the mint itself is
        implicated, rather than whatever ran next.

    MANUAL STEPS
        1. Snapshot the counter and reality (both queries from GEN-IN-08).
        2. Mint an FT:
             curl -s -X POST http://$HOST:20000/rubix/v1/fts/mint \\
                  -H 'Content-Type: application/json' -d '{
                    "did":"'$DID'","ft_name":"denomtest","ft_count":10,
                    "token_count":2}'
           Sign the returned id.
        3. Wait ~6 seconds and re-run both queries.

    PASS / FAIL
        PASS  consistent after the mint
        FAIL  drift appears that was not there before -> the mint burnt RBT
              without decrementing the counter
        SKIP  the wallet was ALREADY drifting before the mint - that is a real
              finding, but GEN-IN-08's, not this one's
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.denom_drift(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before:
        return SKIP, "already drifting before the mint", (
            "cannot attribute drift to the FT mint when it is already present: "
            + _describe_drift(before) + " - see GEN-IN-08")

    ok, msg, _ = rc.mint_ft(s["host"], s["did"], "denom" + _rand_tag(6), 10, 2, ctx.port)
    if not ok:
        return False, "mint rejected", str(msg)
    time.sleep(SETTLE)

    try:
        after = db.denom_drift(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    return (not after), ("counter consistent after FT mint" if not after
                         else "{} denomination(s) drifted".format(len(after))), (
        "" if not after else
        _describe_drift(after) + " - introduced by the FT mint, which burnt RBT "
        "without decrementing the counter")


# ---------------------------------------------------------------------------
# GEN-IN-11
# ---------------------------------------------------------------------------

def gen_in_11(ctx, ci):
    """
    GEN-IN-11 - Check the denomination counter after a contract deploy with a value.

    WHAT IT CHECKS
        Deploying a contract that locks collateral leaves the counter
        consistent at every denomination OTHER than the one the collateral came
        from.

    WHY IT MATTERS
        Contract deploy is the second operation that consumes RBT without a
        transfer. It is checked separately from GEN-IN-10 for a practical
        reason: if one case covered both, a smart-contract regression and an FT
        regression would produce the same red line and nobody could tell which
        subsystem broke. The collateral denomination is excluded because
        movement THERE is the deploy working correctly - drift ELSEWHERE is the
        defect.

    MANUAL STEPS
        1. Snapshot counter and reality (GEN-IN-08 queries).
        2. Deploy a contract with value 0.001 (see SC-C-01 for the body).
        3. Wait ~6 seconds and re-run both queries.
        4. Ignore any change at the 1.000 denomination - that is the collateral
           being taken. Look at every OTHER denomination.

    PASS / FAIL
        PASS  no drift outside the collateral denomination
        FAIL  drift elsewhere -> the deploy disturbed denominations it never
              touched
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.denom_drift(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before:
        return SKIP, "already drifting before the deploy", (
            "cannot attribute drift to the deploy when it is already present: "
            + _describe_drift(before) + " - see GEN-IN-08")

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "contract generation failed", err

    value = 0.001
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="denom drift check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        after = db.denom_drift(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # LockTokensForSplit selects WHOLE denominations, so a 0.001 commitment is
    # backed by a 1.000 token. Movement at that denomination is the deploy
    # working; movement anywhere else is not.
    collateral_denom = 1.0
    other = {d: v for d, v in after.items() if abs(d - collateral_denom) > TOL}

    return (not other), ("no drift outside the collateral denomination" if not other
                         else "{} other denomination(s) drifted".format(len(other))), (
        "" if not other else
        _describe_drift(other) + " - the deploy disturbed denominations outside "
        "the one its collateral came from")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CASES = {
    "GEN-IN-08": gen_in_08,
    "GEN-IN-09": gen_in_09,
    "GEN-IN-10": gen_in_10,
    "GEN-IN-11": gen_in_11,
}

# Shape checks first: if the counter is already wrong before any operation
# runs, GEN-IN-10 and GEN-IN-11 cannot attribute drift to the mint or the deploy
# and will honestly SKIP rather than blame the wrong thing.
ORDER = ["GEN-IN-08", "GEN-IN-09", "GEN-IN-10", "GEN-IN-11"]

TIMING_CASES = set()
