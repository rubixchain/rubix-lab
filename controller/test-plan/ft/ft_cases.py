#!/usr/bin/env python3
"""
ft_cases.py - Fungible Token cases from the master catalogue.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases ft
One case:                          python3 case_runner.py --cases ft --only FT-P-02

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
    If the script and the manual steps disagree, the manual steps are the
    specification - they are what a human can verify independently.

BEFORE RUNNING ANYTHING BY HAND
        SENDER=192.168.1.104
        DID=$(curl -s http://$SENDER:20000/rubix/v1/dids | python3 -c \\
              'import sys,json; print(json.load(sys.stdin)["result"][0])')

    Every state-changing call is a TWO-STEP password challenge. The first POST
    returns {"result":{"id":"<reqID>"}} and NOTHING has happened yet:

        curl -s -X POST http://$SENDER:20000/rubix/v1/signature \\
             -H 'Content-Type: application/json' \\
             -d '{"id":"<reqID>","password":"mypassword","signature":""}'

    Some checks read Postgres. From the controller:
        psql -h $SENDER -p 5433 -U rubix -d rubix       # password: rubixpass

WHY THE PARTS CASES EXIST
    Minting an FT burns RBT. Every other FT-M-* case mints from a wallet
    holding hundreds of WHOLE tokens, so exactly one whole parent is burnt per
    batch and the interesting path never runs.

    A wallet can legitimately hold only FRACTIONAL RBT - receive 0.4 and 0.3
    and you own parts, not a whole token. Minting from that wallet has to burn
    SEVERAL part tokens to back one batch, and each burn must decrement the
    denomination counter for what it consumed.

    If it does not, token_denom keeps advertising tokens that are already
    burnt. Nothing fails at that moment. The failure lands on a LATER,
    unrelated transaction, which asks for rows that are no longer Free and dies
    with "lockSelectedTokens: no tokens provided". That is why FT-P-04 mints a
    SECOND time and FT-P-05 spends the leftovers - the first mint is rarely
    where the damage shows.
"""

import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

# Repetition and interleaving cases live alongside; imported into
# CASES/ORDER below so nothing else needs to know they are separate.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "..", "sc"))
import ft_cases_stress
import ft_cases_scale

SKIP = "SKIP"

SETTLE = 6
TOL = 0.0015

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

def _bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _prepare(ctx, entry, need):
    """Register a quorum and ensure free balance. Returns (ok, why)."""
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    # Already-registered errors even though the insert is ON CONFLICT DO
    # NOTHING (core/wallet/quorum.go:19), so the result is ignored.
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


def _ft_name():
    return "part" + "".join(random.choice(string.ascii_lowercase + string.digits)
                            for _ in range(7))


def _parts_wallet(ctx):
    """The receiver being used as the parts wallet, or None if not built yet."""
    return _PARTS["entry"]


# ---------------------------------------------------------------------------
# FT-P-01
# ---------------------------------------------------------------------------

def ft_p_01(ctx, ci):
    """
    FT-P-01 - Fund a wallet using only amounts below 1 so it holds no whole token.

    WHAT IT CHECKS
        After receiving several sub-1.0 transfers, the wallet holds ONLY
        fractional tokens - no 1.000 token anywhere - and the fractions add up
        to the amount sent.

    WHY IT MATTERS
        This is the precondition every other FT-P case depends on. It has to be
        verified rather than assumed: the API only reports a TOTAL, so a wallet
        holding 2.4 as a whole 2.0 plus a 0.4 looks identical to one holding
        five parts. Only the tokens table can tell them apart, and if this
        precondition is wrong the rest of the FT-P chain silently tests the
        ordinary whole-token path instead.

    MANUAL STEPS
        1. Pick a receiver host that has NOT been funded, e.g. RECV=192.168.1.105
           RDID=$(curl -s http://$RECV:20000/rubix/v1/dids | python3 -c \\
                  'import sys,json; print(json.load(sys.stdin)["result"][0])')

        2. From a funded sender, send these amounts one at a time, signing each:
             0.4, 0.3, 0.5, 0.7, 0.5
             curl -s -X POST http://$SENDER:20000/rubix/v1/tx \\
                  -H 'Content-Type: application/json' -d '{
                    "initiator":"'$DID'", "owner":"'$RDID'",
                    "tokens":{"rbt":0.4,"transferNftOwnership":false},
                    "memo":"FT-P-01"}'
           Wait ~2s between sends; a receiver credits 1-2s after the call returns.

        3. List what the receiver now holds (token_status 0 = Free):
             psql -h $RECV -p 5433 -U rubix -d rubix -c \\
               "SELECT token_value, COUNT(*) FROM tokens
                 WHERE did='$RDID' AND token_status=0 GROUP BY token_value;"

    PASS / FAIL
        PASS  no row has token_value >= 1.0, and the values sum to 2.4
        FAIL  a 1.000 token is present -> this is not a parts wallet, and every
              later FT-P case would be testing the wrong path
        SKIP  psycopg2 missing, or Postgres unreachable on the receiver
    """
    s, r = ctx.pair(0)
    total = sum(PART_AMOUNTS)

    ready, why = _prepare(ctx, s, total + 3)
    if not ready:
        return SKIP, "setup incomplete", why

    if not db.available():
        return SKIP, "database driver missing", (
            "this case cannot be done through the API - it needs the tokens "
            "table to prove no whole token exists. "
            "sudo apt install -y python3-psycopg2")

    # Only usable if the receiver starts with no whole tokens of its own.
    try:
        before = db.free_token_values(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if any(v >= 1.0 for v in before):
        return SKIP, "receiver already holds whole tokens", (
            "{} already has {} whole token(s), so a parts-only wallet cannot be "
            "built here without a wipe".format(r["host"], sum(1 for v in before if v >= 1.0)))

    sent = 0.0
    for amt in PART_AMOUNTS:
        ok, msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                             rbt=amt, memo="FT-P-01 parts",
                                             port=ctx.port)
        if not ok:
            return False, "part transfer failed", "sending {} failed: {}".format(amt, msg)
        sent += amt
        time.sleep(2)

    time.sleep(SETTLE)
    try:
        values = db.free_token_values(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    wholes = [v for v in values if v >= 1.0]
    held = sum(values)
    parts_only = not wholes
    adds_up = rc.close_enough(held, sent, tol=TOL * len(PART_AMOUNTS))

    if parts_only and adds_up:
        _PARTS["entry"] = r

    passed = parts_only and adds_up
    return passed, "{} part token(s) totalling {:.3f}".format(len(values), held), (
        "" if passed else (
            "wallet holds {} whole token(s) - not a parts wallet".format(len(wholes))
            if wholes else
            "parts total {:.3f} but {:.3f} was sent".format(held, sent)))


# ---------------------------------------------------------------------------
# FT-P-02
# ---------------------------------------------------------------------------

def ft_p_02(ctx, ci):
    """
    FT-P-02 - Mint an FT from a wallet holding only part tokens.

    WHAT IT CHECKS
        The mint succeeds, the FT count is right, and MORE THAN ONE RBT row was
        burnt to back the batch.

    WHY IT MATTERS
        Backing one batch from parts requires burning several tokens, because
        no single part covers the whole amount. That multi-burn path is what
        the ordinary FT cases never reach - they burn exactly one whole parent.
        Counting burnt rows is what distinguishes "worked" from "worked the way
        this case is about".

    MANUAL STEPS
        1. Count what the parts wallet has burnt so far (status 9 = BurntForFT):
             psql -h $RECV -p 5433 -U rubix -d rubix -c \\
               "SELECT COUNT(*) FROM tokens WHERE did='$RDID' AND token_status=9;"

        2. Mint an FT from that wallet, using 2 RBT of backing:
             curl -s -X POST http://$RECV:20000/rubix/v1/fts/mint \\
                  -H 'Content-Type: application/json' -d '{
                    "did":"'$RDID'","ft_name":"parttest","ft_count":10,
                    "token_count":2}'
           Sign the returned id.

        3. Re-run the count from step 1, and check the FT balance:
             curl -s http://$RECV:20000/rubix/v1/dids/$RDID/balances/ft

    PASS / FAIL
        PASS  mint succeeds, FT count is 10, and the burnt-row count rose by
              MORE THAN ONE
        FAIL  exactly one row burnt -> a whole token was used, so this is not
              actually exercising the parts path
        FAIL  mint rejected
    """
    r = _parts_wallet(ctx)
    if r is None:
        return SKIP, "no parts wallet", (
            "FT-P-01 did not complete, so there is no parts-only wallet to mint "
            "from. These cases are sequential - run without --only")

    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    try:
        snap_before = db.snapshot(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    burnt_before = snap_before["burnt_for_ft_rows"]
    # Handed to FT-P-03 so it measures this mint's burn instead of assuming the
    # wallet started empty.
    _PARTS["snap_before_mint"] = snap_before

    name = _ft_name()
    ft_count, token_count = 10, 2
    ok, msg, _ = rc.mint_ft(r["host"], r["did"], name, ft_count, token_count, ctx.port)
    if not ok:
        return False, "mint rejected", str(msg)

    got, _cnt, _res = rc.wait_for_ft_count(r["host"], r["did"], name, ft_count, ctx.port)
    time.sleep(SETTLE)

    try:
        snap_after = db.snapshot(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    _PARTS["snap_after_mint"] = snap_after

    burnt = snap_after["burnt_for_ft_rows"] - burnt_before
    multi = burnt > 1
    if multi and got:
        _PARTS["ft_name"] = name
        _PARTS["minted_rbt"] = token_count

    passed = bool(got) and multi
    return passed, "{} FTs, {} RBT row(s) burnt".format(ft_count if got else 0, burnt), (
        "" if passed else (
            "only {} row burnt - a whole token backed this batch, so the parts "
            "path was not exercised".format(burnt) if not multi else
            "mint returned success but the FT count never reached {}".format(ft_count)))


# ---------------------------------------------------------------------------
# FT-P-03
# ---------------------------------------------------------------------------

def ft_p_03(ctx, ci):
    """
    FT-P-03 - Check what the part burn actually consumed.

    WHAT IT CHECKS
        Across the FT-P-02 mint: free balance fell by the RBT minted, and the
        SAME value appears as BurntForFT. Both measured as a delta between a
        before and an after snapshot.

    WHY IT MATTERS
        Burning parts means splitting them, and a split is where value goes
        missing: if a 0.7 part is consumed to supply 0.5, the other 0.2 must
        come back as change. The FT count is correct either way, so only
        comparing the free-balance DROP against the recorded BURN shows whether
        a part was silently destroyed.

        Measured as a delta, not a total. An earlier version read the cumulative
        BurntForFT and assumed the wallet was fresh - true when FT-P-01 had just
        built it, and quietly wrong the moment anything else had burnt from that
        DID. An assumption in a comment is not a measurement.

    MANUAL STEPS
        Around the FT-P-02 mint, on the parts wallet (0 = Free, 9 = BurntForFT):
             psql -h $RECV -p 5433 -U rubix -d rubix -c \
               "SELECT token_status, COUNT(*), SUM(token_value)
                  FROM tokens WHERE did='$RDID' AND token_type=1
                  GROUP BY token_status;"
        Compare the two readings.

    PASS / FAIL
        PASS  free fell by the minted RBT, AND BurntForFT rose by the same
        FAIL  free fell by more than was burnt -> the difference was destroyed
        FAIL  BurntForFT rose by less than the RBT minted -> unrecorded burn
        SKIP  FT-P-02 did not complete, so there are no snapshots to compare
    """
    r = _parts_wallet(ctx)
    if r is None or not _PARTS["ft_name"]:
        return SKIP, "no completed parts mint", (
            "FT-P-02 did not complete, so there is nothing to audit")

    before = _PARTS.get("snap_before_mint")
    after = _PARTS.get("snap_after_mint")
    if not before or not after:
        return SKIP, "no snapshots", (
            "FT-P-02 did not record before/after snapshots - it must run in the "
            "same pass as this case")

    minted = _PARTS["minted_rbt"]
    d = db.delta(before, after)
    free_drop = -d["free"]          # free falls, so the delta is negative
    burnt_rise = d["burnt_for_ft"]
    tol = TOL * 4

    problems = []
    if not rc.close_enough(free_drop, minted, tol=tol):
        problems.append("free balance fell by {:.3f}, expected {}".format(free_drop, minted))
    if not rc.close_enough(burnt_rise, minted, tol=tol):
        problems.append("BurntForFT rose by {:.3f}, expected {}".format(burnt_rise, minted))
    if not rc.close_enough(free_drop, burnt_rise, tol=tol):
        problems.append("free fell {:.3f} but only {:.3f} was recorded as burnt - "
                        "the difference was destroyed".format(free_drop, burnt_rise))

    return (not problems), "free -{:.3f}, burnt +{:.3f} for {} RBT minted".format(
        free_drop, burnt_rise, minted), "; ".join(problems)


def ft_p_04(ctx, ci):
    """
    FT-P-04 - Mint a second FT from the same parts wallet.

    WHAT IT CHECKS
        A second mint from the same wallet succeeds, and the denomination
        counter still agrees with the real Free tokens afterwards.

    WHY IT MATTERS
        This is the case that actually catches the bug. If the first mint burnt
        parts without decrementing token_denom, nothing failed at the time -
        the counter simply now advertises tokens that no longer exist. The
        SECOND mint consults that counter, asks for rows that are not Free, and
        dies with "lockSelectedTokens: no tokens provided". Repeating the
        operation is what turns a silent corruption into a visible failure.

    MANUAL STEPS
        1. Mint again from the parts wallet, exactly as in FT-P-02 but with a
           different ft_name.
        2. Compare the counter against reality:
             psql -h $RECV -p 5433 -U rubix -d rubix
             SELECT denom, count FROM token_denom WHERE did='$RDID' ORDER BY denom;
             SELECT token_value, COUNT(*) FROM tokens
               WHERE did='$RDID' AND token_status=0 GROUP BY token_value ORDER BY token_value;
           The two listings must match, denomination for denomination.

    PASS / FAIL
        PASS  second mint succeeds AND the two listings agree
        FAIL  mint rejected with "no tokens provided" -> the first mint
              corrupted the counter
        FAIL  mint succeeds but the listings disagree -> corruption present,
              and the NEXT operation will be the one that breaks
    """
    r = _parts_wallet(ctx)
    if r is None or not _PARTS["ft_name"]:
        return SKIP, "no completed parts mint", "FT-P-02 did not complete"

    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    # Snapshot BEFORE the second mint. Without this, drift left by the FIRST
    # mint would be reported against the second - blaming the wrong operation
    # for damage it merely inherited.
    try:
        before = db.snapshot(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting before the second mint", (
            "the first mint left the counter inconsistent: "
            + db.describe_drift(before["denom_drift"]) +
            " - that is FT-P-02's finding, not this one's")

    name = _ft_name()
    ft_count, token_count = 5, 1
    ok, msg, _ = rc.mint_ft(r["host"], r["did"], name, ft_count, token_count, ctx.port)
    if not ok:
        text = str(msg).lower()
        hint = ("this is the signature of a corrupted denomination counter - the "
                "first mint burnt parts without decrementing token_denom"
                if "no tokens provided" in text or "lockselected" in text else "")
        return False, "second mint rejected", "{} {}".format(msg, hint).strip()

    got, _cnt, _res = rc.wait_for_ft_count(r["host"], r["did"], name, ft_count, ctx.port)
    time.sleep(SETTLE)

    try:
        after = db.snapshot(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    passed = bool(got) and not drift
    desc = "counter consistent" if not drift else db.describe_drift(drift)
    return passed, "second mint ok, {}".format(desc), (
        "" if passed else (
            desc + " - the next operation to select from this wallet is the one "
            "that will fail" if drift else
            "mint returned success but the FT count never reached {}".format(ft_count)))


# ---------------------------------------------------------------------------
# FT-P-05
# ---------------------------------------------------------------------------

def ft_p_05(ctx, ci):
    """
    FT-P-05 - Spend the parts left over after the FT burns.

    WHAT IT CHECKS
        Whatever RBT remains in the parts wallet after both mints can still be
        transferred out.

    WHY IT MATTERS
        The end-to-end proof that nothing was stranded. A wallet can show a
        healthy free balance while those tokens are unselectable - the balance
        is a SUM over rows, but spending needs the denomination counter to
        point at rows that are really Free. If the burns left the counter
        wrong, this transfer is where it finally surfaces, and it surfaces as a
        transfer failing for no visible reason.

    MANUAL STEPS
        1. Read the parts wallet's free balance:
             curl -s http://$RECV:20000/rubix/v1/dids/$RDID/balances/rbt
        2. Send a small amount back to the original sender - and note that the
           parts wallet needs a quorum registered to SEND, which it did not
           need to RECEIVE:
             curl -s -X POST http://$RECV:20000/rubix/v1/quorums/add \\
                  -H 'Content-Type: application/json' -d '{"did":"<QUORUM_DID>"}'
             curl -s -X POST http://$RECV:20000/rubix/v1/tx \\
                  -H 'Content-Type: application/json' -d '{
                    "initiator":"'$RDID'","owner":"'$DID'",
                    "tokens":{"rbt":0.2,"transferNftOwnership":false},
                    "memo":"FT-P-05"}'
           Sign it.

    PASS / FAIL
        PASS  the transfer succeeds
        FAIL  rejected while the balance says the funds are there -> leftover
              parts are stranded and unselectable
        SKIP  nothing left to spend after the mints
    """
    r = _parts_wallet(ctx)
    if r is None:
        return SKIP, "no parts wallet", "FT-P-01 did not complete"

    s, _ = ctx.pair(0)
    detail = _bal(ctx, r)
    if detail is None:
        return SKIP, "balance unreadable", "cannot tell whether anything is left"

    free = detail["balance"]
    amount = 0.2
    if free < amount:
        return SKIP, "nothing left to spend", (
            "parts wallet holds {:.3f} free after the mints, below the {} this "
            "case sends".format(free, amount))

    # The parts wallet has only ever RECEIVED. Receiving needs no quorum;
    # sending does. Registering it here is the difference between testing the
    # product and testing a gap in the harness.
    ready, why = _prepare(ctx, r, amount)
    if not ready:
        return SKIP, "setup incomplete", why

    ok, msg, _ = rc.initiate_transaction(r["host"], r["did"], s["did"],
                                         rbt=amount, memo="FT-P-05 leftover",
                                         port=ctx.port)
    if not ok:
        return False, "leftover transfer rejected", (
            "{:.3f} RBT is reported free but {} could not be sent: {} - this is "
            "what stranded parts look like".format(free, amount, msg))

    return True, "spent {} of {:.3f} leftover".format(amount, free), ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CASES = {
    "FT-P-01": ft_p_01,
    "FT-P-02": ft_p_02,
    "FT-P-03": ft_p_03,
    "FT-P-04": ft_p_04,
    "FT-P-05": ft_p_05,

    # Direct tests of the GREATEST(count-1,0) floor, and of the burn
    # path interleaved with the other writers of token_denom.
    "FT-P-06": ft_cases_stress.ft_p_06,
    "FT-P-07": ft_cases_stress.ft_p_07,
    "FT-P-08": ft_cases_stress.ft_p_08,

    # Production-level volume - see sc_cases_scale.py.
    "FT-X-01": ft_cases_scale.ft_x_01,
    "FT-X-02": ft_cases_scale.ft_x_02,
}

# Strictly sequential and deliberately so: FT-P-01 builds the parts wallet,
# 02 mints from it, 03 audits that mint, 04 mints AGAIN (where corruption
# surfaces), 05 spends what is left. Running one alone reports SKIP rather
# than a misleading FAIL.
ORDER = ["FT-P-01", "FT-P-02", "FT-P-03", "FT-P-04", "FT-P-05",
         "FT-P-06", "FT-P-07", "FT-P-08",
         "FT-X-01", "FT-X-02"]

TIMING_CASES = set()

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

LANES = {
    "ft-parts": {
        "cases": ["FT-P-01", "FT-P-02", "FT-P-03", "FT-P-04", "FT-P-05"],
        "hosts": 2, "fund": 12,
    },

    # FT-P-06 deliberately runs its wallet dry to reach the counter floor, so
    # it cannot share with anything. FT-P-07/08 interleave the burn path with
    # the other writers of token_denom and need room to work.
    "ft-exhaustion": {
        "cases": ["FT-P-06"],
        "hosts": 1, "fund": 4,
    },
    "ft-interleave": {
        "cases": ["FT-P-07", "FT-P-08"],
        "hosts": 2, "fund": 35,
    },

    # --- scale lanes (pr-739-stress) ---------------------------------------
    # FT-X-01 burns one RBT per mint for 100 mints, so it needs real balance.
    "ft-scale-mints": {
        "cases": ["FT-X-01"],
        "hosts": 1, "fund": 120,
    },
    "ft-scale-concurrent": {
        "cases": ["FT-X-02"],
        "hosts": 4, "fund": 40,
    },
}

