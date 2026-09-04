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

    LockTokensForSplit selects WHOLE denominations (core/consensus/consensus.go:31).
    Backing a 0.001 commitment therefore picks up a whole 1.000 token. If that
    token is committed as-is instead of being split, the other 0.999 is destroyed
    - a 0.001 contract silently costs a full RBT. It is invisible at value 1.0,
    which is exactly where every other SC case sits.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"

# How long to let a deploy settle before reading the balance back. Committed
# tokens and change do not appear the instant the call returns.
SETTLE = 6

# Tolerance for a balance comparison. MinDecimalUnit is 0.001 and FloatPrecision
# rounds at 3dp (math/math.go), so anything tighter than this reports rounding as
# a failure.
TOL = 0.0015


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
    q = ctx.quorum_for(host) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"

    # Already-registered returns an error even though the insert is
    # ON CONFLICT DO NOTHING (core/wallet/quorum.go:19) - so ignore the result.
    rc.quorum_add(host, q["did"], ctx.port)

    detail = _bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        if not rc.wait_for_balance(host, entry["did"], need, ctx.port):
            return False, "could not fund to {} RBT (have {})".format(need, have)

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
        (core/consensus/consensus.go:31), so backing a 0.001 commitment picks
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

    value = 0.001
    spent, sc_id, err = _deploy_and_measure(ctx, s, value)
    if err:
        return False, "deploy failed", err

    exact = rc.close_enough(spent, value, tol=TOL)
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
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    committed = after_committed - before_committed
    exact = rc.close_enough(committed, value, tol=TOL)
    whole = committed >= 0.9

    detail = ", ".join("{}={}x{:.3f}".format(k, v[0], v[1])
                       for k, v in sorted(summary.items()))
    return exact, "committed {:.4f} for a {:.3f} contract".format(committed, value), (
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

    value = 0.001
    rounds = 3
    before = _bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the total cost"

    failures = []
    for i in range(rounds):
        sc_id, err = _new_contract(ctx, s)
        if err:
            failures.append("round {}: generation failed ({})".format(i + 1, err))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                       data="repeat deploy {}".format(i + 1), port=ctx.port)
        if not ok:
            failures.append("round {}: {}".format(i + 1, msg))
        time.sleep(2)

    time.sleep(SETTLE)
    after = _bal(ctx, s)
    spent = (before["balance"] - after["balance"]) if after else None
    expected = value * rounds

    if failures:
        return False, "{}/{} deploys succeeded".format(rounds - len(failures), rounds), (
            "; ".join(failures) + " - a later round failing points at state the "
            "earlier deploy left behind")

    ok_cost = spent is not None and rc.close_enough(spent, expected, tol=TOL * rounds)
    return ok_cost, "{}/{} deployed, spent {:.4f}".format(rounds, rounds, spent or -1), (
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

    exact = rc.close_enough(spent, value, tol=TOL)
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
    values = [0.001, 0.01, 0.1, 0.5]
    ready, why = _prepare(ctx, s, sum(values) + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    results, bad = [], []
    for v in values:
        spent, _sc, err = _deploy_and_measure(ctx, s, v)
        if err:
            bad.append("{}: {}".format(v, err))
            continue
        ok = rc.close_enough(spent, v, tol=TOL)
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
        contract it subscribed to, the cost is exactly the contract value, and
        the tables stay consistent.

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
        PASS  execute succeeds, cost equals the contract value, counter still
              consistent
        FAIL  rejected while the balance shows enough - parts could not be
              assembled for a contract pledge
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
    value = 0.001
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="deploy for parts execute", port=ctx.port)
    if not ok:
        return SKIP, "deploy failed", str(msg)
    time.sleep(SETTLE)

    # Build a parts-only wallet on the executor: several sub-1.0 sends, so no
    # whole token is ever created there.
    try:
        if any(v >= 1.0 for v in db.free_token_values(r["host"], r["did"])):
            return SKIP, "executor holds whole tokens", (
                "{} already has whole tokens, so the parts path cannot be "
                "isolated here without a wipe".format(r["host"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    for amt in (0.4, 0.3, 0.5):
        okp, msgp, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                               rbt=amt, memo="SC-C-09 parts",
                                               port=ctx.port)
        if not okp:
            return SKIP, "could not build parts wallet", str(msgp)
        time.sleep(2)
    time.sleep(SETTLE)

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

    cost_ok = spent is not None and rc.close_enough(spent, value, tol=TOL)
    passed = cost_ok and not drift
    return passed, "executed from parts, spent {:.4f}".format(spent if spent is not None else -1), (
        "" if passed else "; ".join(filter(None, [
            "" if cost_ok else "cost {:.4f}, expected {:.4f}".format(spent or -1, value),
            "" if not drift else "counter drifted after a parts execute"])))


# ---------------------------------------------------------------------------
# SC-Q-06
# ---------------------------------------------------------------------------

def sc_q_06(ctx, ci):
    """
    SC-Q-06 - Check the quorum pledged the correct value for a contract deploy.

    WHAT IT CHECKS
        During a deploy the quorum's pledged value rises to at least the
        contract value, and afterwards the pledge is released.

    WHY IT MATTERS
        Every other quorum case checks whether the deploy SUCCEEDS. This checks
        what the quorum actually did. Two distinct faults hide behind a
        successful deploy: pledging too little (the guarantee is not backed) and
        never releasing (the quorum leaks capacity every transaction until it
        can no longer sign anything, which then looks like an unrelated failure
        much later).

        Release matters as much as the pledge - a quorum that pledges correctly
        but never unpledges will pass every early test and fail the whole run.

    MANUAL STEPS
        1. On the QUORUM host, note pledged value (6 = Pledged, 7 = QuorumPledged):
             psql -h $QUORUM -p 5433 -U rubix -d rubix -c \\
               "SELECT COALESCE(SUM(token_value),0) FROM tokens
                 WHERE did='<QUORUM_DID>' AND token_status IN (6,7);"
        2. Deploy a contract with value 1.0 from the sender.
        3. Re-run the query straight away, then again after ~15 seconds.
        4. Also check nothing is left permanently queued:
             SELECT u.tx_id FROM unpledge_sequence_info u
              WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = u.tx_id);

    PASS / FAIL
        PASS  pledge covered the contract value and returned to its earlier level
        FAIL  pledged less than the value -> the guarantee was not backed
        FAIL  still elevated well after settling -> pledge not released, and the
              quorum will slowly run out of capacity
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    q = ctx.quorum_for(s["host"]) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return SKIP, "no quorum", "cannot check pledging without a known quorum"

    value = 1.0
    ready, why = _prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        pledged_before = db.pledged_value(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="pledge check deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    # Peak pledge is transient - sample promptly, then let it settle.
    peak = pledged_before
    for _ in range(6):
        try:
            peak = max(peak, db.pledged_value(q["host"], q["did"]))
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        time.sleep(1)

    time.sleep(SETTLE * 2)
    try:
        pledged_after = db.pledged_value(q["host"], q["did"])
        stuck = db.open_pledges(q["host"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    covered = (peak - pledged_before) >= (value - TOL)
    released = rc.close_enough(pledged_after, pledged_before, tol=TOL + value * 0.01)

    problems = []
    if not covered:
        problems.append("quorum pledged only {:.4f} for a {:.3f} contract - the "
                        "guarantee was not fully backed".format(peak - pledged_before, value))
    if not released:
        problems.append("pledge not released: {:.4f} -> {:.4f} - the quorum loses "
                        "capacity every transaction".format(pledged_before, pledged_after))
    if stuck:
        problems.append("{} unpledge row(s) reference a transaction that does not "
                        "exist, so they can never be released".format(len(stuck)))

    return (not problems), "pledged +{:.3f}, released={}".format(
        peak - pledged_before, released), "; ".join(problems)


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

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=0.001,
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

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=0.001,
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

    for i in range(3):
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=0.001,
                                       data="hop {}".format(i + 1), port=ctx.port)
        if not ok:
            return SKIP, "execute failed", "could not build a deep chain: {}".format(msg)
        time.sleep(3)
    time.sleep(SETTLE)

    ok, msg, n = _record_sub(ctx, "after 4 executes", other, sc_id)
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
]

TIMING_CASES = set()
