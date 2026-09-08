#!/usr/bin/env python3
"""
sc_cases.py - Smart Contract cases from the master catalogue.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases sc
One case:                          python3 case_runner.py --cases sc --only SC-C-01

Every case returns (passed, actual, note):
    True  -> matched the catalogue's Expected Result
    False -> did not match: a real finding, investigate
    SKIP  -> NOT ATTEMPTED, reason in `note`. Never counted as a pass.

HOW TO READ A CASE
    Each case has a docstring with four fixed parts:
        WHAT IT CHECKS  - the assertion, in plain words
        WHY IT MATTERS  - the bug it would catch, and the product code involved
        MANUAL STEPS    - how to run it BY HAND with curl, no Python needed
        PASS / FAIL     - exactly what makes it pass or fail
    If the script and the manual steps ever disagree, the manual steps are the
    specification - they are what a human can verify independently.

BEFORE RUNNING ANYTHING BY HAND
    Set these once in your shell. Every MANUAL block below uses them.

        SENDER=192.168.1.104          # any pool host with a DID
        DID=$(curl -s http://$SENDER:20000/rubix/v1/dids | python3 -c \\
              'import sys,json; print(json.load(sys.stdin)["result"][0])')

    Every state-changing call is a TWO-STEP password challenge. The first POST
    returns {"result":{"id":"<reqID>"}}, and nothing happens until you sign it:

        curl -s -X POST http://$SENDER:20000/rubix/v1/signature \\
             -H 'Content-Type: application/json' \\
             -d '{"id":"<reqID>","password":"mypassword","signature":""}'

    A first POST that "succeeds" has NOT done anything yet. This is the single
    most common way a manual check reports a false pass.

WHY THE COLLATERAL CASES EXIST
    None of the other 17 SC cases give a contract a VALUE - they all deploy and
    execute at the default. That leaves the collateral accounting path entirely
    untested, and it has a specific, expensive failure mode:

    LockTokensForSplit selects WHOLE denominations (core/wallet/post_consensus_payload_builder.go:226).
    Backing a 0.001 commitment therefore picks up a whole 1.000 token. If that
    token is committed as-is instead of being split, the other 0.999 is destroyed
    - a 0.001 contract silently costs a full RBT. It is invisible at value 1.0,
    which is exactly where every other SC case sits.
"""

import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db
import wallet_shapes as ws

# Cases added while reviewing PR #739 live in sibling files purely to keep
# this one readable. They are ordinary SC catalogue cases and are imported
# into CASES/ORDER below, so nothing else needs to know they are separate.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sc_cases_extra
import sc_cases_db
import sc_cases_quorum
import sc_cases_subs
import sc_cases_stress
import sc_cases_scale

SKIP = "SKIP"

# How long to let a deploy settle before reading the balance back. Committed
# tokens and change do not appear the instant the call returns.
SETTLE = 6

# Tolerance for a balance comparison. MinDecimalUnit is 0.001 and FloatPrecision
# rounds at 3dp (math/math.go), so anything tighter than this reports rounding as
# a failure.
TOL = 0.0015


def rand_value(lo, hi):
    """A random amount with 3 decimal places, in [lo, hi].

    Deliberately NOT round numbers like 0.001 / 0.5 / 1.0. Clean denominations
    can take a different path through token selection than an arbitrary value:
    0.5 may split cleanly off a 1.000 token while 0.354 needs several levels
    and leaves awkward change. Testing only tidy values tests the easy path.

    3dp is the network maximum - MinDecimalUnit is 0.001 and FloatPrecision
    ROUNDS at 3dp (math/math.go), so a 4th place would be silently rounded and
    the case would assert against a value the node never saw.

    The value used is reported in every result, so a failure stays reproducible
    even though the input is random.
    """
    v = round(random.uniform(lo, hi), 3)
    return max(v, 0.001)


def cost_tolerance(value):
    """Tolerance for asserting a deploy cost `value`.

    A FLAT 0.0015 is wrong for small values: at value=0.001 it accepts anything
    from 0 to 0.0025, so a deploy that charged NOTHING passes a check that
    claims to verify it charged 0.001. That is exactly what happened - SC-C-09
    reported "spent 0.0000" and was recorded as a PASS.

    Never allow more than half the expected value, so a zero (or double) charge
    can never sit inside the tolerance. Still floored at the 3dp rounding limit,
    below which the balance API genuinely cannot distinguish values.
    """
    return max(min(TOL, value / 2.0), 1e-9)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _bal(ctx, entry):
    """Free RBT for a host's DID, or None if unreadable.

    `balance` is the FREE portion only - tokens locked for an in-flight
    transfer or committed as collateral are reported separately
    (types/balance.go). That distinction is the whole point of these cases.
    """
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _prepare(ctx, entry, need):
    """Give `entry` a registered quorum and enough free RBT. Returns (ok, why).

    A case must build the conditions it needs. Without this, a case fails
    because the harness left the node unable to transact - which says nothing
    about the product.
    """
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"

    # Already-registered returns an error even though the insert is
    # ON CONFLICT DO NOTHING (core/wallet/quorum.go:19) - so ignore the result.
    rc.quorum_add(host, q["did"], ctx.port)

    detail = _bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)

    # The quorum pledges at least the transaction value
    # (core/consensus/checks.go:539), so it needs headroom too.
    qd = _bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _new_contract(ctx, entry):
    """Generate a contract and return (sc_id, error). Does NOT deploy it.

    Both file extensions are checked literally by the server: the binary must
    end .wasm and the source .rs (server/smart_contract.go:70, :101-106).
    """
    import random
    import string
    tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"], wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None


def _deploy_and_measure(ctx, entry, value):
    """Deploy one contract at `value` and return (spent, sc_id, error).

    `spent` is the drop in FREE balance across the deploy - which is the number
    the collateral cases are actually about.
    """
    sc_id, err = _new_contract(ctx, entry)
    if err:
        return None, None, "contract generation failed: {}".format(err)

    before = _bal(ctx, entry)
    if before is None:
        return None, sc_id, "balance unreadable before deploy"

    ok, msg, _ = rc.sc_transaction(entry["host"], entry["did"], sc_id, value=value,
                                   data="collateral deploy {}".format(value), port=ctx.port)
    if not ok:
        return None, sc_id, "deploy rejected: {}".format(msg)

    time.sleep(SETTLE)
    after = _bal(ctx, entry)
    if after is None:
        return None, sc_id, "balance unreadable after deploy"
    return (before["balance"] - after["balance"]), sc_id, None


# ---------------------------------------------------------------------------
# SC-C-01
# ---------------------------------------------------------------------------

def sc_c_01(ctx, ci):
    """
    SC-C-01 - Deploy a contract with a value of 0.001.

    WHAT IT CHECKS
        Deploying a contract worth 0.001 RBT reduces the deployer's free
        balance by exactly 0.001 - not by a whole 1.000 token.

    WHY IT MATTERS
        LockTokensForSplit selects WHOLE denominations
        (core/wallet/post_consensus_payload_builder.go:226), so backing a 0.001 commitment picks
        up a 1.000 token. If that token is committed without being split, the
        remaining 0.999 is destroyed and the contract silently costs 1000x what
        it should. Invisible at value 1.0, which is where every other SC case
        sits - so only a fractional value can catch it.

    MANUAL STEPS
        1. Note the free balance before:
             curl -s http://$SENDER:20000/rubix/v1/dids/$DID/balances/rbt
           Read the "balance" field - that is the FREE portion only.

        2. Generate a contract (both extensions are checked literally):
             curl -s -X POST http://$SENDER:20000/rubix/v1/smart_contracts/generate \\
                  -F "did=$DID" -F "binaryCodePath=@c.wasm" -F "rawCodePath=@c.rs"
           Sign the returned id. The result is the contract id.

        3. Deploy it with a value of 0.001. Note owner is an EMPTY string -
           contracts have no ownership transfer:
             curl -s -X POST http://$SENDER:20000/rubix/v1/tx \\
                  -H 'Content-Type: application/json' -d '{
                    "initiator":"'$DID'", "owner":"",
                    "tokens":{"rbt":0,"ft":[],"nft":[],
                      "smartContract":[{"smartContractId":"<SC>","value":0.001,
                                        "data":"collateral"}],
                      "transferNftOwnership":false},
                    "memo":"SC-C-01"}'
           Sign the returned id.

        4. Wait ~6 seconds, then read the balance again.

    PASS / FAIL
        PASS  before - after == 0.001 (within 0.0015 for 3dp rounding)
        FAIL  a whole token was consumed -> the remainder was destroyed
        FAIL  the deploy was rejected -> a fractional value should be allowed
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 5.0)
    if not ready:
        return SKIP, "setup incomplete", why

    # Random sub-1.0 value, not 0.001: an arbitrary fraction exercises the
    # splitter properly, where a clean denomination may not.
    value = rand_value(0.100, 0.999)
    spent, sc_id, err = _deploy_and_measure(ctx, s, value)
    if err:
        return False, "deploy failed", err

    exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
    whole = spent >= 0.9
    return exact, "spent {:.4f} for a {:.3f} contract".format(spent, value), (
        "" if exact else (
            "a whole token was consumed for a {} deploy - the remaining {:.3f} "
            "was destroyed instead of returned as change".format(value, spent - value)
            if whole else
            "cost {:.4f}, expected {:.4f}".format(spent, value)))


# ---------------------------------------------------------------------------
# SC-C-02
# ---------------------------------------------------------------------------

def sc_c_02(ctx, ci):
    """
    SC-C-02 - Check what was actually committed for a 0.001 deploy.

    WHAT IT CHECKS
        The tokens backing the contract total exactly 0.001 and are marked
        Committed (a terminal state), with the remainder returned as change.

    WHY IT MATTERS
        SC-C-01 measures the free-balance DROP, which is the symptom. This
        measures where the value WENT, which is the cause. They can disagree:
        a deploy can take the right amount from free balance while committing a
        whole token and losing the change, or commit correctly while the change
        never lands. Separating them says which half is broken.

    MANUAL STEPS
        Reads Postgres directly - the API reports totals, not the per-token
        status this case is about. token_status 5 is Committed
        (constants/constants.go).

        1. Note the committed total BEFORE, from the controller:
             psql -h $SENDER -p 5433 -U rubix -d rubix
             # password: rubixpass
             SELECT COALESCE(SUM(token_value),0) FROM tokens
              WHERE did='<DID>' AND token_status=5;

        2. Deploy a contract with value 0.001 exactly as in SC-C-01.

        3. Wait ~6 seconds, then run the same SELECT again.

        4. Optional, to see the whole picture at once:
             SELECT token_status, COUNT(*), SUM(token_value)
               FROM tokens WHERE did='<DID>' GROUP BY token_status;

    PASS / FAIL
        PASS  the committed total rose by exactly 0.001
        FAIL  it rose by a whole denomination (e.g. 1.0) -> a whole token was
              committed and the change destroyed
        SKIP  psycopg2 not installed, or the node's Postgres is unreachable
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 5.0)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before_committed = db.value_in_status(s["host"], s["did"], db.COMMITTED)
        snap_before = db.record("SC-C-02", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    value = 0.001
    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="committed-sum deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        after_committed = db.value_in_status(s["host"], s["did"], db.COMMITTED)
        summary = db.token_status_summary(s["host"], s["did"])
        snap_after = db.record("SC-C-02", "after", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    committed = after_committed - before_committed
    exact = rc.close_enough(committed, value, tol=cost_tolerance(value))
    whole = committed >= 0.9

    detail = ", ".join("{}={}x{:.3f}".format(k, v[0], v[1])
                       for k, v in sorted(summary.items()))
    evidence = db.format_evidence(snap_before, snap_after)
    return exact, "committed {:.4f} for a {:.3f} contract | {}".format(
        committed, value, evidence), (
        "" if exact else (
            "a whole token was committed for a {} deploy - the remaining {:.3f} "
            "was destroyed rather than returned as change. Token status now: "
            "{}".format(value, committed - value, detail) if whole else
            "committed {:.4f}, expected {:.4f}. Token status now: {}".format(
                committed, value, detail)))


# ---------------------------------------------------------------------------
# SC-C-03
# ---------------------------------------------------------------------------

def sc_c_03(ctx, ci):
    """
    SC-C-03 - Deploy three fractional-value contracts one after another.

    WHAT IT CHECKS
        Three 0.001 deploys in a row all succeed, and together cost about
        0.003 - not 3 whole tokens.

    WHY IT MATTERS
        A single deploy can look correct while leaving the wallet in a state
        that breaks the next one: if the first deploy corrupts the
        denomination counter, the SECOND deploy is the one that fails, and it
        fails somewhere else entirely. Repeating the operation is what turns a
        silent corruption into a visible failure.

    MANUAL STEPS
        1. Note the free balance.
        2. Run the SC-C-01 deploy three times, generating a NEW contract each
           time (a contract cannot be deployed twice).
        3. Wait ~6 seconds after the last one, then read the balance.

    PASS / FAIL
        PASS  all three deploys succeed AND the total cost is about 0.003
        FAIL  any deploy is rejected - especially the 2nd or 3rd, which points
              at state the earlier deploy corrupted rather than at the deploy
              itself
        FAIL  the total is near 3.0 -> a whole token per deploy
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 10.0)
    if not ready:
        return SKIP, "setup incomplete", why

    rounds = 3
    before = _bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the total cost"

    # A different random value each round: repeating one value would only prove
    # that value works three times, not that the path handles varied fractions.
    values = [rand_value(0.100, 0.999) for _ in range(rounds)]
    expected = sum(values)

    failures = []
    for i, value in enumerate(values, 1):
        sc_id, err = _new_contract(ctx, s)
        if err:
            failures.append("round {}: generation failed ({})".format(i, err))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                       data="repeat deploy {}".format(i), port=ctx.port)
        if not ok:
            failures.append("round {} (value {}): {}".format(i, value, msg))
        time.sleep(2)

    time.sleep(SETTLE)
    after = _bal(ctx, s)
    spent = (before["balance"] - after["balance"]) if after else None

    if failures:
        return False, "{}/{} deploys succeeded".format(rounds - len(failures), rounds), (
            "; ".join(failures) + " - a later round failing points at state the "
            "earlier deploy left behind")

    # cost_tolerance(expected), NOT TOL * rounds: the balance is read once before
    # and once after all rounds, so there is a single rounding error, not one per
    # round. Scaling by rounds would have made the tolerance (0.0045) larger than
    # the expected total (0.003) - letting a zero charge pass.
    ok_cost = spent is not None and rc.close_enough(spent, expected,
                                                    tol=cost_tolerance(expected))
    return ok_cost, "{}/{} deployed at {}, spent {:.4f}".format(
        rounds, rounds, "+".join(str(v) for v in values), spent or -1), (
        "" if ok_cost else "expected about {:.3f} total, spent {:.4f}".format(expected, spent or -1))


# ---------------------------------------------------------------------------
# SC-C-04
# ---------------------------------------------------------------------------

def sc_c_04(ctx, ci):
    """
    SC-C-04 - Deploy a contract with a value of exactly 1.0.

    WHAT IT CHECKS
        A whole-value deploy costs exactly 1.0.

    WHY IT MATTERS
        This is the CONTROL for SC-C-01. Value 1.0 is the one case where
        committing a whole token is the correct behaviour, so it should pass
        even when the fractional path is broken. Read the two together:
            SC-C-01 fail + SC-C-04 pass -> fractional handling specifically
            both fail                   -> collateral accounting generally
        Without this, a failing SC-C-01 cannot be narrowed down.

    MANUAL STEPS
        Exactly as SC-C-01, but with "value":1.0 in the deploy body.

    PASS / FAIL
        PASS  cost is 1.0 (within rounding)
        FAIL  anything else
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 6.0)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 1.0
    spent, _sc, err = _deploy_and_measure(ctx, s, value)
    if err:
        return False, "deploy failed", err

    exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
    return exact, "spent {:.4f} for a {:.1f} contract".format(spent, value), (
        "" if exact else "cost {:.4f}, expected {:.1f}. This is the control case - "
        "if it fails too, the problem is collateral accounting generally, not "
        "fractional values specifically".format(spent, value))


# ---------------------------------------------------------------------------
# SC-C-05
# ---------------------------------------------------------------------------

def sc_c_05(ctx, ci):
    """
    SC-C-05 - Execute a contract that carries a value.

    WHAT IT CHECKS
        Executing a contract that has a value succeeds, and the chain advances.

    WHY IT MATTERS
        Execute pledges the contract's value, so it depends on the quorum
        having enough to pledge (core/consensus/checks.go:539). Deploy and
        execute take different paths; a contract that deploys correctly can
        still fail to execute.

    MANUAL STEPS
        1. Deploy a contract with a value as in SC-C-01.
        2. Note the chain length:
             curl -s http://$SENDER:20000/rubix/v1/smart_contracts/<SC>/chain
        3. Execute it - same body as the deploy, same value.
        4. Re-read the chain after a few seconds.

    PASS / FAIL
        PASS  execute succeeds and the chain is one longer
        FAIL  rejected for pledge shortage -> the run is INVALID, not a
              product failure. Top the quorum up and repeat (see CLAUDE.md).
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 6.0)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.001
    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="deploy for execute", port=ctx.port)
    if not ok:
        return SKIP, "deploy failed", "cannot test execute without a deployed contract: {}".format(msg)
    time.sleep(SETTLE)

    _, before, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="valued execute", port=ctx.port)
    if not ok:
        text = str(msg).lower()
        if "pledge" in text or "insufficient" in text:
            return SKIP, "quorum could not pledge", (
                "run is INVALID rather than failed - a quorum must pledge at "
                "least the value. Fund the quorum and repeat: {}".format(msg))
        return False, "execute rejected", str(msg)

    grew = False
    n = len(before)
    for _ in range(12):
        time.sleep(2)
        _, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
        n = len(chain)
        if n > len(before):
            grew = True
            break

    return grew, "chain {} -> {}".format(len(before), n), (
        "" if grew else "execute returned success but the chain never advanced")


# ---------------------------------------------------------------------------
# SC-C-06
# ---------------------------------------------------------------------------

def sc_c_06(ctx, ci):
    """
    SC-C-06 - Deploy with a value larger than the wallet holds.

    WHAT IT CHECKS
        The deploy is rejected, nothing is committed, and no tokens are left
        locked afterwards.

    WHY IT MATTERS
        Checking only that it "fails" is not enough. A rejection that leaves
        tokens stuck in Locked is a real bug - there are three separate
        lock-release paths on failure (core/transaction.go:70, :77, :88) and a
        miss in any of them strands value invisibly. `locked` must return to
        where it started, and that is not visible from `balance` alone.

    MANUAL STEPS
        1. Read the balance and note BOTH "balance" and "locked":
             curl -s http://$SENDER:20000/rubix/v1/dids/$DID/balances/rbt
        2. Deploy with a value far above the free balance (e.g. balance + 1000).
        3. Wait a few seconds, then read the balance again.

    PASS / FAIL
        PASS  rejected, AND balance unchanged, AND locked back to its old value
        FAIL  accepted
        FAIL  rejected but locked is still raised -> tokens stranded
    """
    s, _ = ctx.pair(0)
    ready, why = _prepare(ctx, s, 2.0)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot verify state was left unchanged"

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    too_much = before["balance"] + 1000.0
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=too_much,
                                   data="over-value deploy", port=ctx.port)
    time.sleep(SETTLE)
    after = _bal(ctx, s)

    if ok:
        return False, "ACCEPTED", (
            "deploying {:.3f} against a free balance of {:.3f} should have been "
            "rejected".format(too_much, before["balance"]))

    problems = []
    if after is not None:
        if not rc.close_enough(before["balance"], after["balance"]):
            problems.append("balance moved on a rejected deploy ({:.4f} -> {:.4f})".format(
                before["balance"], after["balance"]))
        if not rc.close_enough(before["locked"], after["locked"]):
            problems.append("tokens left LOCKED after rejection ({:.4f} -> {:.4f})".format(
                before["locked"], after["locked"]))

    return (not problems), "rejected", "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-07
# ---------------------------------------------------------------------------

def sc_c_07(ctx, ci):
    """
    SC-C-07 - Deploy several contracts with different decimal values in one run.

    WHAT IT CHECKS
        Four deploys at 0.001, 0.01, 0.1 and 0.5, each measured on its own. Every
        one must cost exactly its own value.

    WHY IT MATTERS
        SC-C-01 proves ONE fractional value is handled correctly. That does not
        prove the others are. Selection picks whole denominations and splits
        down to the requested value, so different values take different split
        paths - 0.5 may split cleanly from a 1.000 token while 0.001 needs
        several levels. Testing one value and generalising is exactly how the
        0.001 case survived undetected while 1.0 worked.

        Measuring each deploy separately also localises the fault: a report
        saying "0.01 and 0.1 fine, 0.001 wrong" points at deep splits, whereas a
        single combined total would just say "something is off".

    MANUAL STEPS
        For EACH value in 0.001, 0.01, 0.1, 0.5:
          1. Read the free balance:
               curl -s http://$SENDER:20000/rubix/v1/dids/$DID/balances/rbt
          2. Generate a NEW contract (a contract cannot be deployed twice) and
             deploy it with that value - body as in SC-C-01, signing each step.
          3. Wait ~6s and read the balance again.
          4. The drop must equal that value, before moving to the next one.

    PASS / FAIL
        PASS  every value costs exactly itself (within 0.0015)
        FAIL  any single value is wrong - the report names which, and that
              tells you which split depth is broken
    """
    s, _ = ctx.pair(0)
    # Random, and spanning the 1.000 boundary: below a whole token, around it,
    # and above it. 0.001 is kept as the explicit minimum-unit edge case.
    values = [0.001,
              rand_value(0.002, 0.099),
              rand_value(0.100, 0.999),
              rand_value(1.001, 2.999)]
    ready, why = _prepare(ctx, s, sum(values) + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    results, bad = [], []
    for v in values:
        spent, _sc, err = _deploy_and_measure(ctx, s, v)
        if err:
            bad.append("{}: {}".format(v, err))
            continue
        ok = rc.close_enough(spent, v, tol=cost_tolerance(v))
        results.append("{}->{:.4f}{}".format(v, spent, "" if ok else " WRONG"))
        if not ok:
            bad.append("value {} cost {:.4f}".format(v, spent))
        time.sleep(2)

    return (not bad), ", ".join(results) or "no deploys completed", (
        "" if not bad else "; ".join(bad) +
        " - the values that failed indicate which split depth is mishandled")


# ---------------------------------------------------------------------------
# SC-C-08
# ---------------------------------------------------------------------------

def sc_c_08(ctx, ci):
    """
    SC-C-08 - Check the tokens and denomination tables after every deploy.

    WHAT IT CHECKS
        After each of several deploys: Committed rows appear, free rows drop,
        and token_denom still matches the real Free tokens.

    WHY IT MATTERS
        SC-C-01 and SC-C-02 check the value arithmetic. This checks the BOOKS
        stay consistent while that happens - specifically that token_denom is
        maintained as tokens leave Free. A deploy can take exactly the right
        value and still leave the counter advertising a token it just committed.
        Nothing fails then; the next operation to select from this wallet fails
        instead, and it fails somewhere unrelated.

        Checking after EVERY deploy rather than once at the end means the first
        deploy that breaks the invariant is named, instead of the damage being
        attributed to whichever one happened to run last.

    MANUAL STEPS
        Before, and after EACH deploy:
             psql -h $SENDER -p 5433 -U rubix -d rubix
             -- what the node believes it holds
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
             -- what it actually holds free (0 = Free)
             SELECT token_value, COUNT(*) FROM tokens
              WHERE did='<DID>' AND token_status=0 GROUP BY token_value ORDER BY token_value;
             -- and what has been committed (5 = Committed)
             SELECT COUNT(*), SUM(token_value) FROM tokens
              WHERE did='<DID>' AND token_status=5;

    PASS / FAIL
        PASS  after every deploy the two listings agree, and Committed grew
        FAIL  the listings disagree after any deploy - the report names which
              deploy first broke it
        SKIP  psycopg2 missing, or Postgres unreachable
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", (
            "token_denom is not exposed by any API. "
            "sudo apt install -y python3-psycopg2")

    ready, why = _prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        if db.denom_drift(s["host"], s["did"]):
            return SKIP, "already drifting before the run", (
                "the counter is inconsistent before any deploy, so nothing here "
                "could be attributed to a deploy - see GEN-IN-08")
        committed_before = db.value_in_status(s["host"], s["did"], db.COMMITTED)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    values = [0.001, 0.01, 0.1]
    problems, steps = [], []
    for i, v in enumerate(values, 1):
        sc_id, err = _new_contract(ctx, s)
        if err:
            problems.append("deploy {}: generation failed".format(i))
            break
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="table check {}".format(i), port=ctx.port)
        if not ok:
            problems.append("deploy {} ({}) rejected: {}".format(i, v, msg))
            break
        time.sleep(SETTLE)
        try:
            drift = db.denom_drift(s["host"], s["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        steps.append("{}:{}".format(v, "ok" if not drift else "DRIFT"))
        if drift:
            problems.append("after deploy {} (value {}): ".format(i, v) + "; ".join(
                "denom {:.3f} counter={} free={}".format(d, c, a)
                for d, (c, a) in sorted(drift.items())))
            break

    try:
        committed_after = db.value_in_status(s["host"], s["did"], db.COMMITTED)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    grew = committed_after > committed_before

    if not problems and not grew:
        problems.append("deploys succeeded but Committed did not grow "
                        "({:.4f} -> {:.4f}) - collateral was taken from Free "
                        "without being recorded".format(committed_before, committed_after))

    return (not problems), " ".join(steps) or "no deploys completed", "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-09
# ---------------------------------------------------------------------------

def sc_c_09(ctx, ci):
    """
    SC-C-09 - Execute a contract from a subscribed node using part tokens.

    WHAT IT CHECKS
        A node whose wallet holds only fractional RBT can still execute a
        contract it subscribed to; the executor is charged NOTHING (execute
        takes no collateral - only deploy does); and the denomination counter
        stays consistent afterwards.

    WHY IT MATTERS
        Two paths that are each tested separately meet here for the first time.
        Execute pledges the contract's value, and selection has to assemble that
        from parts rather than splitting one whole token. If either the parts
        selection or the collateral accounting is wrong, this is where it shows
        - and neither SC-C-01 (whole-token wallet) nor FT-P-02 (no contract
        involved) would catch it.

    MANUAL STEPS
        1. Build a parts-only wallet on the executor exactly as in FT-P-01 -
           several sub-1.0 transfers, no whole token.
        2. Subscribe that node to a deployed contract:
             curl -s "http://$EXEC:20000/rubix/v1/smart_contracts/subscribe?smartContractToken=<SC>"
        3. Note the free balance and the denom listing (SC-C-08 queries).
        4. Execute the contract from that node with a fractional value.
        5. Re-read balance and denom listing.

    PASS / FAIL
        PASS  execute succeeds, executor charged nothing, counter consistent
        FAIL  rejected while holding enough - a parts-only wallet could not
              take part in consensus
        FAIL  executor was charged - collateral is a deploy-only cost
        SKIP  no parts wallet could be built
    """
    s, r = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    # Well above the 3dp resolution limit: at 0.001 a balance delta cannot be
    # distinguished from zero, which is how this case previously recorded
    # "spent 0.0000" as a PASS.
    value = rand_value(0.050, 0.999)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="deploy for parts execute", port=ctx.port)
    if not ok:
        return SKIP, "deploy failed", str(msg)
    time.sleep(SETTLE)

    # BUILD the parts wallet rather than requiring one to exist. Draining the
    # executor's whole tokens to the sender and sending fractions back makes
    # the precondition reproducible on any wallet, whatever it held before.
    okw, whyw = ws.make_parts_wallet(ctx, r, s, amounts=(0.4, 0.3, 0.5))
    if not okw:
        return SKIP, "could not build parts wallet", whyw
    shape_note = ws.describe(r["host"], r["did"], ctx.port)

    sub_ok, sub_msg = rc.subscribe_smart_contract(r["host"], sc_id, ctx.port)
    if not sub_ok:
        return SKIP, "subscribe failed", str(sub_msg)
    time.sleep(SETTLE)

    ready, why = _prepare(ctx, r, 0.5)
    if not ready:
        return SKIP, "executor setup incomplete", why

    before = _bal(ctx, r)
    ok, msg, _ = rc.sc_transaction(r["host"], r["did"], sc_id, value=value,
                                   data="parts execute", port=ctx.port)
    if not ok:
        return False, "execute rejected", (
            "executor holds {:.3f} in parts but could not execute: {}".format(
                before["balance"] if before else -1, msg))
    time.sleep(SETTLE)

    after = _bal(ctx, r)
    spent = (before["balance"] - after["balance"]) if (before and after) else None
    try:
        drift = db.denom_drift(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # EXECUTE takes no collateral. transaction_builder.go skips the collateral
    # path when the SC token already exists ("Execute-mode SCs reuse their
    # existing value and need no collateral"), so the executor should spend
    # NOTHING. An earlier version of this case asserted a cost equal to the
    # contract value and "passed" on spent=0.0000 only because the tolerance was
    # wider than the value - it was asserting the wrong thing and getting the
    # right answer by accident.
    no_charge = spent is not None and abs(spent) <= TOL
    passed = no_charge and not drift
    return passed, "executed from a parts-only wallet, spent {:.4f} (expected 0)".format(
        spent if spent is not None else -1), (
        "" if passed else "; ".join(filter(None, [
            "" if no_charge else
            "executor was charged {:.4f} - execute should take no collateral, "
            "only deploy does".format(spent or -1),
            "" if not drift else "denom counter drifted after a parts execute"])))


# ---------------------------------------------------------------------------
# SC-Q-06
# ---------------------------------------------------------------------------

def sc_q_06(ctx, ci):
    """
    SC-Q-06 - The deploy value, the quorum pledge and the denom table must agree.

    WHAT IT CHECKS
        For one deploy at value V, three independent records all say V:
          1. the DEPLOYER spent V (free balance drop)
          2. the DEPLOYER committed V (tokens table, status Committed)
          3. the QUORUM pledged at least V (tokens table, status Pledged)
        and afterwards the deployer token_denom still matches its real free
        tokens.

    WHY IT MATTERS
        These are four separate books that must tell the same story. A single
        balance check cannot tell "collateral taken correctly" from "collateral
        taken and mis-recorded" - the deployer balance falls either way. The
        interesting failures are the disagreements:
          * spent > committed  -> value left the wallet without being recorded
          * pledge < value     -> the quorum guaranteed less than it signed for
          * denom drifts       -> the counter still advertises committed tokens,
                                  and the NEXT unrelated transaction fails
        Whether the quorum later RELEASES the pledge is deliberately not
        asserted here - unpledging is asynchronous and on its own schedule, so
        testing it in this window would report timing as a defect.

    MANUAL STEPS
        With DID = deployer, QDID = its quorum DID:

        1. Before, on the deployer (status 5 = Committed) and quorum (6,7 = Pledged):
             psql -h $SENDER -p 5433 -U rubix -d rubix -c \
               "SELECT COALESCE(SUM(token_value),0) FROM tokens
                 WHERE did='<DID>' AND token_type=1 AND token_status=5;"
             psql -h $QUORUM -p 5433 -U rubix -d rubix -c \
               "SELECT COALESCE(SUM(token_value),0) FROM tokens
                 WHERE did='<QDID>' AND token_type=1 AND token_status IN (6,7);"
           And the free balance:
             curl -s http://$SENDER:20000/rubix/v1/dids/<DID>/balances/rbt

        2. Deploy a contract with a fractional value (see SC-C-01).

        3. Re-run all three. Each delta must equal the deploy value.

        4. Then confirm the counter still matches reality:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
             SELECT token_value, COUNT(*) FROM tokens
               WHERE did='<DID>' AND token_status=0 AND token_type=1
               GROUP BY token_value ORDER BY token_value;

    PASS / FAIL
        PASS  spent == committed == value, quorum pledged >= value, no denom drift
        FAIL  any of the four disagree - the report names which
        SKIP  psycopg2 missing, Postgres unreachable, or the counter was
              ALREADY drifting before the deploy (nothing here could then be
              attributed to this deploy - see GEN-IN-08)
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    q = ctx.quorum_for(s) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return SKIP, "no quorum", "cannot compare against a pledge without a known quorum"

    value = rand_value(0.100, 0.999)
    ready, why = _prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        if db.denom_drift(s["host"], s["did"]):
            return SKIP, "already drifting before the deploy", (
                "the counter is inconsistent before this deploy, so a drift "
                "afterwards could not be attributed to it - see GEN-IN-08")
        free_before = _bal(ctx, s)
        committed_before = db.value_in_status(s["host"], s["did"], db.COMMITTED)
        pledged_before = db.pledged_value(q["host"], q["did"])
        # Recorded as evidence: the report should show the readings, not just
        # the verdict drawn from them.
        snap_before = db.record("SC-Q-06", "before (deployer)", s["host"],
                                s["did"], db.snapshot(s["host"], s["did"]))
        qsnap_before = db.record("SC-Q-06", "before (quorum)", q["host"],
                                 q["did"], db.snapshot(q["host"], q["did"]))
        # The quorum's OWN counter matters too. Pledging moves its tokens out of
        # Free (core/wallet/pledge.go:222), so its token_denom must decrement -
        # the same class of bug this PR fixes on the deploy and mint paths, but
        # on a path the PR does NOT touch. A failure here is a new finding.
        q_drift_before = db.denom_drift(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if free_before is None:
        return SKIP, "balance unreadable", "cannot measure what the deployer spent"

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="three-way agreement check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    # Sample the pledge promptly - it is transient - then let everything settle
    # before reading the committed and free figures.
    peak_pledged = pledged_before
    for _ in range(6):
        try:
            peak_pledged = max(peak_pledged, db.pledged_value(q["host"], q["did"]))
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        time.sleep(1)
    time.sleep(SETTLE)

    try:
        free_after = _bal(ctx, s)
        committed_after = db.value_in_status(s["host"], s["did"], db.COMMITTED)
        drift = db.denom_drift(s["host"], s["did"])
        q_drift = db.denom_drift(q["host"], q["did"])
        snap_after = db.record("SC-Q-06", "after (deployer)", s["host"],
                               s["did"], db.snapshot(s["host"], s["did"]))
        qsnap_after = db.record("SC-Q-06", "after (quorum)", q["host"],
                                q["did"], db.snapshot(q["host"], q["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    spent = (free_before["balance"] - free_after["balance"]) if free_after else None
    committed = committed_after - committed_before
    pledged = peak_pledged - pledged_before
    tol = cost_tolerance(value)

    problems = []
    if spent is None or not rc.close_enough(spent, value, tol=tol):
        problems.append("deployer spent {:.4f}, expected {:.3f}".format(
            spent if spent is not None else -1, value))
    if not rc.close_enough(committed, value, tol=tol):
        problems.append("committed {:.4f}, expected {:.3f}".format(committed, value))
    if spent is not None and not rc.close_enough(spent, committed, tol=tol):
        problems.append("spent {:.4f} but only {:.4f} was recorded as committed - "
                        "the difference left the wallet unaccounted for".format(spent, committed))
    if pledged < (value - tol):
        problems.append("quorum pledged {:.4f} for a {:.3f} contract - less than "
                        "it signed for".format(pledged, value))
    if drift:
        problems.append("deployer denom counter drifted: " + "; ".join(
            "denom {:.3f} counter={} free={}".format(d, c, a)
            for d, (c, a) in sorted(drift.items())))
    # Only report quorum drift this deploy INTRODUCED - the quorum signs for the
    # whole fleet, so pre-existing drift there is not attributable to this case.
    new_q_drift = {d: v for d, v in q_drift.items() if d not in q_drift_before}
    if new_q_drift:
        problems.append("QUORUM denom counter drifted after pledging: " + "; ".join(
            "denom {:.3f} counter={} free={}".format(d, c, a)
            for d, (c, a) in sorted(new_q_drift.items())) +
            " - pledging moves tokens out of Free, so the quorum counter must "
            "decrement too. This path is NOT part of the fix under test")

    evidence = "deployer[{}] quorum[{}]".format(
        db.format_evidence(snap_before, snap_after),
        db.format_evidence(qsnap_before, qsnap_after))
    return (not problems), "value={:.3f} spent={:.4f} committed={:.4f} pledged={:.4f} q_denom={}".format(
        value, spent if spent is not None else -1, committed, pledged,
        "ok" if not new_q_drift else "DRIFT"), (
        "; ".join(problems) + (" | " if problems else "") + evidence)


# ---------------------------------------------------------------------------
# Subscription timing - SC-S-01 .. SC-S-05
#
# These run as one sequence and share _SUBS: a contract is deployed, executed
# repeatedly, and different nodes subscribe at different points. SC-S-05 then
# compares what they all ended up with.
#
# Worth knowing before reading them: a late subscriber is NOT guaranteed the
# existing chain. SubsribeContractSetup (core/smart_contract.go:274) only
# fetches when the contract folder is absent locally, and
# syncSmartContractTransaction (:190) returns silently when the metadata has no
# PeerID. So a node can subscribe successfully and still hold a partial chain,
# with nothing reporting it.
# ---------------------------------------------------------------------------

_SUBS = {"sc": None, "subscribers": []}   # [(label, entry, chain_len_at_subscribe)]


def _record_sub(ctx, label, entry, sc_id):
    ok, msg = rc.subscribe_smart_contract(entry["host"], sc_id, ctx.port)
    time.sleep(SETTLE)
    _, chain, _ = rc.get_sc_chain(entry["host"], sc_id, ctx.port)
    _SUBS["subscribers"].append((label, entry, len(chain)))
    return ok, msg, len(chain)


def sc_s_01(ctx, ci):
    """
    SC-S-01 - Subscribe to a contract before it is deployed.

    WHAT IT CHECKS
        What happens when a node subscribes to a contract id that has been
        generated but not yet deployed.

    WHY IT MATTERS
        Subscription and deployment are independent calls, so nothing stops
        them arriving in this order in real use. The interesting question is
        whether the node then receives the deploy event it was waiting for, or
        whether subscribing to a not-yet-existing token leaves it in a state
        that never catches up. Recorded rather than asserted, because either
        outcome is defensible - what matters is knowing which one happens.

    MANUAL STEPS
        1. Generate a contract but DO NOT deploy it. Note the id.
        2. From a second node:
             curl -s "http://$OTHER:20000/rubix/v1/smart_contracts/subscribe?smartContractToken=<SC>"
        3. Now deploy from the first node.
        4. After ~10s, read the chain on the subscriber:
             curl -s http://$OTHER:20000/rubix/v1/smart_contracts/<SC>/chain

    PASS / FAIL
        Records the behaviour. The finding is whether the early subscriber ends
        up with the deploy entry or with an empty chain.
    """
    s, r = ctx.pair(0)
    ready, why = _prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    sub_ok, sub_msg = rc.subscribe_smart_contract(r["host"], sc_id, ctx.port)
    pre_note = "subscribe before deploy {}".format("accepted" if sub_ok else "refused")
    time.sleep(2)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="deploy after early subscribe", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    _STATE_len = 0
    _, chain, _ = rc.get_sc_chain(r["host"], sc_id, ctx.port)
    _STATE_len = len(chain)

    _SUBS["sc"] = sc_id
    _SUBS["subscribers"] = [("before deploy", r, _STATE_len)]

    return True, "{}; subscriber chain={} after deploy".format(pre_note, _STATE_len), (
        "recorded: an early subscriber ended with a chain of {} - "
        "{}".format(_STATE_len,
                    "it received the deploy event" if _STATE_len else
                    "it did NOT receive the deploy event and has nothing"))


def sc_s_02(ctx, ci):
    """
    SC-S-02 - Subscribe immediately after deployment.

    WHAT IT CHECKS
        A node subscribing right after deploy ends up with the full chain.

    WHY IT MATTERS
        The baseline: there is nothing to catch up on, so this is the case that
        SHOULD work even if back-fill is broken. If it fails, the problem is
        subscription itself rather than history sync - which is what separates
        it from SC-S-03 and SC-S-04.

    MANUAL STEPS
        1. Deploy a contract.
        2. Immediately subscribe from a second node (URL as in SC-S-01).
        3. Read the chain on both nodes and compare lengths.

    PASS / FAIL
        PASS  subscriber's chain matches the owner's
        FAIL  shorter - subscription is not delivering current state at all
    """
    guard = _need_sub()
    if guard:
        return guard
    s, _ = ctx.pair(0)
    sc_id = _SUBS["sc"]
    if len(ctx.receivers) < 2:
        return SKIP, "not enough hosts", "need a second subscriber host"
    other = ctx.receivers[1]

    ok, msg, n = _record_sub(ctx, "after deploy", other, sc_id)
    if not ok:
        return False, "subscribe failed", str(msg)
    _, owner_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)

    match = n == len(owner_chain)
    return match, "subscriber={} owner={}".format(n, len(owner_chain)), (
        "" if match else "an immediate subscriber is already behind the owner - "
        "subscription is not delivering current state")


def sc_s_03(ctx, ci):
    """
    SC-S-03 - Subscribe after the contract has already been executed once.

    WHAT IT CHECKS
        A node subscribing after one execution still ends up with the FULL
        chain, including the execution it was not present for.

    WHY IT MATTERS
        This is the first case that needs a back-fill, and back-fill is the weak
        path: SubsribeContractSetup only fetches the contract when its folder is
        absent locally, and the sync it then performs returns silently when the
        metadata carries no PeerID. Either way the subscribe call REPORTS
        SUCCESS. So a node can believe it is subscribed, be allowed to execute,
        and be working from a chain that is missing entries.

    MANUAL STEPS
        1. Execute the contract once from the owner.
        2. Subscribe from a THIRD node.
        3. Compare chains:
             curl -s http://$OWNER:20000/rubix/v1/smart_contracts/<SC>/chain
             curl -s http://$THIRD:20000/rubix/v1/smart_contracts/<SC>/chain

    PASS / FAIL
        PASS  late subscriber's chain matches the owner's
        FAIL  shorter - it subscribed successfully but silently missed history
    """
    guard = _need_sub()
    if guard:
        return guard
    s, _ = ctx.pair(0)
    sc_id = _SUBS["sc"]
    if len(ctx.receivers) < 3:
        return SKIP, "not enough hosts", "need a third subscriber host"
    other = ctx.receivers[2]

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="execute before late subscribe", port=ctx.port)
    if not ok:
        return SKIP, "execute failed", "cannot set up a late subscribe: {}".format(msg)
    time.sleep(SETTLE)

    ok, msg, n = _record_sub(ctx, "after 1 execute", other, sc_id)
    if not ok:
        return False, "subscribe failed", str(msg)
    _, owner_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)

    match = n == len(owner_chain)
    return match, "subscriber={} owner={}".format(n, len(owner_chain)), (
        "" if match else "subscribe reported success but the node is missing {} "
        "chain entr(ies) - it will still be allowed to execute".format(
            len(owner_chain) - n))


def sc_s_04(ctx, ci):
    """
    SC-S-04 - Subscribe after several executions have already happened.

    WHAT IT CHECKS
        Same as SC-S-03 but with a deeper chain - three more executions before
        the node subscribes.

    WHY IT MATTERS
        Chain depth is the variable. A back-fill that silently fetches only the
        most recent entry would pass SC-S-03 (one missing entry, easily masked)
        and fail here. Running both and comparing tells you whether back-fill is
        absent entirely or merely incomplete - a distinction that matters when
        someone has to fix it.

    MANUAL STEPS
        1. Execute the contract three more times from the owner.
        2. Subscribe from a FOURTH node.
        3. Compare chain lengths as in SC-S-03.

    PASS / FAIL
        PASS  chain matches the owner's
        FAIL  shorter - and the SIZE of the gap says whether back-fill fetched
              nothing or only part
    """
    guard = _need_sub()
    if guard:
        return guard
    s, _ = ctx.pair(0)
    sc_id = _SUBS["sc"]
    if len(ctx.receivers) < 4:
        return SKIP, "not enough hosts", "need a fourth subscriber host"
    other = ctx.receivers[3]

    # More hops than before: a back-fill that fetched only the most recent few
    # entries would still pass at depth 3. Depth 8 makes a partial sync visible.
    for i in range(8):
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=rand_value(0.010, 0.200),
                                       data="hop {}".format(i + 1), port=ctx.port)
        if not ok:
            return SKIP, "execute failed", "could not build a deep chain: {}".format(msg)
        time.sleep(3)
    time.sleep(SETTLE)

    ok, msg, n = _record_sub(ctx, "after 9 executes", other, sc_id)
    if not ok:
        return False, "subscribe failed", str(msg)
    _, owner_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)

    match = n == len(owner_chain)
    gap = len(owner_chain) - n
    return match, "subscriber={} owner={}".format(n, len(owner_chain)), (
        "" if match else "missing {} of {} entries - {}".format(
            gap, len(owner_chain),
            "back-fill fetched nothing" if n <= 1 else "back-fill was partial"))


def sc_s_05(ctx, ci):
    """
    SC-S-05 - Compare contract data across nodes that subscribed at different times.

    WHAT IT CHECKS
        Every node that subscribed - before deploy, right after deploy, after
        one execution, after four - holds identical chain data at the end.

    WHY IT MATTERS
        This is the case the whole SC-S sequence exists for. Subscription is
        meant to make when you joined irrelevant; all subscribers should
        converge. If they do not, the node with the shorter chain is still a
        legitimate subscriber and will still be ALLOWED to execute, because
        execute is gated by subscription rather than by chain completeness
        (core/consensus/checks.go:122). It would then be executing against a
        history it cannot see.

        Comparing them together is what makes the failure legible: one table
        showing subscribe-time against final chain length says immediately
        whether lateness is what causes the divergence.

    MANUAL STEPS
        For each node that subscribed, and for the owner:
             curl -s http://$NODE:20000/rubix/v1/smart_contracts/<SC>/chain
        Line up the lengths against when each node subscribed.

    PASS / FAIL
        PASS  every subscriber's chain equals the owner's
        FAIL  any divergence - report which subscribe-times diverged, since
              that identifies whether back-fill or live delivery is at fault
    """
    guard = _need_sub()
    if guard:
        return guard
    s, _ = ctx.pair(0)
    sc_id = _SUBS["sc"]
    if not _SUBS["subscribers"]:
        return SKIP, "no subscribers recorded", "SC-S-01..04 did not run"

    time.sleep(SETTLE)
    _, owner_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    want = len(owner_chain)

    rows, bad = [], []
    for label, entry, at_sub in _SUBS["subscribers"]:
        _, chain, _ = rc.get_sc_chain(entry["host"], sc_id, ctx.port)
        n = len(chain)
        rows.append("{}({})={}".format(label, entry["host"].split(".")[-1], n))
        if n != want:
            bad.append("'{}' on {} has {} of {} entries".format(label, entry["host"], n, want))

    return (not bad), "owner={} | {}".format(want, " ".join(rows)), (
        "" if not bad else "; ".join(bad) +
        " - these nodes are subscribed and may execute against a chain they "
        "cannot fully see")


def _need_sub():
    if not _SUBS.get("sc"):
        return SKIP, "no subscription fixture", (
            "SC-S-01 did not complete, so there is no deployed contract to "
            "subscribe to. The SC-S cases are sequential - run without --only")
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CASES = {
    "SC-C-01": sc_c_01,
    "SC-C-02": sc_c_02,
    "SC-C-03": sc_c_03,
    "SC-C-04": sc_c_04,
    "SC-C-05": sc_c_05,
    "SC-C-06": sc_c_06,
    "SC-C-07": sc_c_07,
    "SC-C-08": sc_c_08,
    "SC-C-09": sc_c_09,
    "SC-Q-06": sc_q_06,
    "SC-S-01": sc_s_01,
    "SC-S-02": sc_s_02,
    "SC-S-03": sc_s_03,
    "SC-S-04": sc_s_04,
    "SC-S-05": sc_s_05,

    # PR #739 review additions - value ladder, wallet shapes, deep split,
    # balance boundary, sustained load, and the row-level DB checks.
    "SC-C-13": sc_cases_extra.sc_c_13,
    "SC-C-14": sc_cases_extra.sc_c_14,
    "SC-C-15": sc_cases_extra.sc_c_15,
    "SC-C-16": sc_cases_extra.sc_c_16,
    "SC-C-17": sc_cases_extra.sc_c_17,
    "SC-C-18": sc_cases_extra.sc_c_18,
    "SC-C-19": sc_cases_extra.sc_c_19,
    "SC-C-20": sc_cases_db.sc_c_20,
    "SC-C-21": sc_cases_db.sc_c_21,
    "SC-C-22": sc_cases_db.sc_c_22,

    # Quorum-side accounting - the other half of every deploy.
    "SC-Q-07": sc_cases_quorum.sc_q_07,
    "SC-Q-08": sc_cases_quorum.sc_q_08,
    "SC-Q-09": sc_cases_quorum.sc_q_09,
    "SC-Q-10": sc_cases_quorum.sc_q_10,
    "SC-Q-11": sc_cases_quorum.sc_q_11,

    # Subscription at fleet scale, and executing from parts wallets.
    "SC-S-06": sc_cases_subs.sc_s_06,
    "SC-S-07": sc_cases_subs.sc_s_07,
    "SC-S-08": sc_cases_subs.sc_s_08,
    "SC-S-09": sc_cases_subs.sc_s_09,
    "SC-S-10": sc_cases_subs.sc_s_10,
    "SC-C-23": sc_cases_subs.sc_c_23,
    "SC-C-24": sc_cases_subs.sc_c_24,
    "SC-C-25": sc_cases_subs.sc_c_25,

    # Concurrency - the deadlock the collateral-split ordering avoids.
    "SC-C-12": sc_cases_stress.sc_c_12,
    "SC-C-26": sc_cases_stress.sc_c_26,

    # Production-level volume. Run via the pr-739-stress suite AFTER
    # the functional suite passes - at this scale a single rejection
    # cannot be told from ordinary contention unless the basics are
    # already known good. Each reports WHERE the invariant first
    # broke, not merely that it did.
    "SC-X-01": sc_cases_scale.sc_x_01,
    "SC-X-02": sc_cases_scale.sc_x_02,
    "SC-X-03": sc_cases_scale.sc_x_03,
    "SC-X-04": sc_cases_scale.sc_x_04,
    "SC-X-05": sc_cases_scale.sc_x_05,
}

# SC-C-04 (the whole-value control) runs BEFORE the repeat cases so that if the
# fractional path is corrupting wallet state, the control has already recorded a
# clean result rather than inheriting the damage.
#
# The SC-S block is strictly sequential and shares one contract: 01 deploys it
# with an early subscriber attached, 02-04 subscribe further nodes at
# increasing chain depths, and 05 compares them all. Running one alone reports
# SKIP rather than a misleading FAIL.
ORDER = [
    "SC-C-01", "SC-C-04", "SC-C-02", "SC-C-03", "SC-C-07", "SC-C-08",
    "SC-C-05", "SC-C-06",
    "SC-Q-06",
    "SC-S-01", "SC-S-02", "SC-S-03", "SC-S-04", "SC-S-05",
    "SC-C-09",
    "SC-C-13", "SC-C-14", "SC-C-15", "SC-C-16", "SC-C-17",
    "SC-C-18", "SC-C-19",
    "SC-C-20", "SC-C-21", "SC-C-22",
    "SC-Q-07", "SC-Q-08", "SC-Q-09", "SC-Q-10", "SC-Q-11",
    "SC-S-06", "SC-S-07", "SC-S-08", "SC-S-09", "SC-S-10",
    "SC-C-23", "SC-C-24", "SC-C-25",
    "SC-C-12", "SC-C-26",
    "SC-X-01", "SC-X-02", "SC-X-03", "SC-X-04", "SC-X-05",
]

TIMING_CASES = set()

# ---------------------------------------------------------------------------
# Lanes - which cases share a wallet, and how much that wallet needs.
#
# The suite is still sequential inside each lane. Lanes run at the same time
# only because the fleet has ~31 machines sitting idle; splitting the work
# across them turns a ~40 minute serial pass into under ten.
#
# Two rules decide the grouping:
#   1. Cases that assert on a BALANCE DELTA must not share a wallet with any
#      other case, or they measure each other's spending.
#   2. Cases that deliberately build on one another stay in ONE lane, in order.
#      SC-S-* share a contract whose chain must deepen between subscribes.
#
# `fund` is roughly what the lane's own cases spend; case_runner adds a safety
# margin on top. Only the quorums are funded in bulk - they are shared by every
# lane and carry all of their pledges at once.
# ---------------------------------------------------------------------------

LANES = {
    # Core collateral arithmetic. Four small deploys.
    "sc-collateral": {
        "cases": ["SC-C-01", "SC-C-04", "SC-C-02", "SC-C-03"],
        "hosts": 1, "fund": 8,
    },
    # The value sweep crosses 1.0, so it needs more than the others.
    "sc-value-sweep": {
        "cases": ["SC-C-07"],
        "hosts": 1, "fund": 12,
    },
    # Reads the tables after every deploy; its own wallet so nothing else can
    # move the counter mid-case.
    "sc-db-tables": {
        "cases": ["SC-C-08"],
        "hosts": 1, "fund": 8,
    },
    # Execute path and the negative case.
    "sc-execute": {
        "cases": ["SC-C-05", "SC-C-06"],
        "hosts": 1, "fund": 8,
    },
    # Three-way agreement between wallet, committed rows and quorum pledge.
    "sc-quorum": {
        "cases": ["SC-Q-06"],
        "hosts": 1, "fund": 8,
    },
    # Subscription timing. One deployer plus subscribers that join at
    # increasing chain depths - SC-S-04 alone needs four spare receivers, and
    # they must all be watching the SAME contract, so this cannot be split.
    "sc-subscription": {
        "cases": ["SC-S-01", "SC-S-02", "SC-S-03", "SC-S-04", "SC-S-05"],
        "hosts": 6, "fund": 6,
    },
    # Builds a parts-only wallet, so it needs a second host to receive the
    # fractional transfers.
    "sc-parts-execute": {
        "cases": ["SC-C-09"],
        "hosts": 2, "fund": 10,
    },

    # --- PR #739 review additions ------------------------------------------
    # The ladder spends the sum of 13 values (~17.5), so it is funded well
    # above every other lane.
    "sc-value-ladder": {
        "cases": ["SC-C-13"],
        "hosts": 1, "fund": 30,
    },
    # Wallet shapes need a second host to receive the fractional transfers that
    # build a parts-only wallet.
    "sc-wallet-shapes": {
        "cases": ["SC-C-14", "SC-C-15", "SC-C-16"],
        "hosts": 2, "fund": 20,
    },
    # Deep split and the exact-balance boundary. SC-C-18 deliberately empties
    # its wallet, so it must not share one with anything else.
    "sc-split-depth": {
        "cases": ["SC-C-17"],
        "hosts": 1, "fund": 8,
    },
    "sc-balance-edge": {
        "cases": ["SC-C-18"],
        "hosts": 1, "fund": 6,
    },
    # Twenty deploys back to back - the slowest lane, so it starts alongside
    # everything else rather than after it.
    "sc-sustained": {
        "cases": ["SC-C-19"],
        "hosts": 1, "fund": 15,
    },
    # Row-level DB checks. Each reads the wallet's rows before and after, so
    # nothing else may touch that wallet while they run.
    "sc-db-rows": {
        "cases": ["SC-C-20", "SC-C-21", "SC-C-22"],
        "hosts": 1, "fund": 12,
    },

    # Quorum-side checks. SC-Q-10 and SC-Q-11 need several senders
    # of their own - 10 routes one deploy per quorum, 11 fires three
    # at one quorum simultaneously.
    "sc-quorum-tables": {
        "cases": ["SC-Q-07", "SC-Q-08", "SC-Q-09"],
        "hosts": 1, "fund": 12,
    },
    "sc-quorum-spread": {
        "cases": ["SC-Q-10", "SC-Q-11"],
        "hosts": 3, "fund": 12,
    },

    # The wide subscription matrix: one deployer plus as many
    # subscribers as the fleet can spare, joining at staggered depths.
    "sc-subs-matrix": {
        "cases": ["SC-S-06", "SC-S-07"],
        "hosts": 7, "fund": 8,
    },
    "sc-subs-timing": {
        "cases": ["SC-S-08", "SC-S-09", "SC-S-10"],
        "hosts": 4, "fund": 12,
    },

    # Parts wallets doing more than one thing.
    "sc-parts-deep": {
        "cases": ["SC-C-23", "SC-C-24", "SC-C-25"],
        "hosts": 4, "fund": 16,
    },

    # Concurrency. SC-C-12 hammers ONE wallet (the deadlock case);
    # SC-C-26 spreads across many to load the shared quorums.
    "sc-race-one-wallet": {
        "cases": ["SC-C-12"],
        "hosts": 1, "fund": 15,
    },
    "sc-race-fleet": {
        "cases": ["SC-C-26"],
        "hosts": 5, "fund": 10,
    },

    # --- scale lanes (pr-739-stress) ---------------------------------------
    # Funded well above the functional lanes: SC-X-01 alone runs 200 deploys,
    # and a lane that runs dry mid-run reports funding as a defect.
    "sc-scale-deploys": {
        "cases": ["SC-X-01"],
        "hosts": 1, "fund": 60,
    },
    "sc-scale-chain": {
        "cases": ["SC-X-02"],
        "hosts": 1, "fund": 40,
    },
    # Takes every host it can: subscribers join across the whole run, so the
    # more nodes, the wider the range of join depths tested.
    "sc-scale-subs": {
        "cases": ["SC-X-03"],
        "hosts": 8, "fund": 30,
    },
    "sc-scale-fleet": {
        "cases": ["SC-X-04"],
        "hosts": 6, "fund": 25,
    },
    # Duration-bounded rather than count-bounded - it finds what depends on
    # background work rather than on operation count.
    "sc-scale-soak": {
        "cases": ["SC-X-05"],
        "hosts": 1, "fund": 50,
    },
}

