#!/usr/bin/env python3
"""
sc_cases_extra.py - SC cases added while reviewing PR #739's diff.

Imported by sc_cases.py; not run on its own. Split out only because sc_cases.py
was getting long - these are ordinary SC catalogue cases, not a separate suite.

WHAT THESE ADD OVER THE ORIGINAL SC-C-* SET
    The original cases prove a deploy COSTS the right amount, measured from the
    free balance. That is the symptom, not the mechanism. Reading the diff of
    maneesha/fix/denom-array-updation showed three branches nothing asserted:

      * the split CHANGE. CollectRBTTokens returns childTokensKept, and the
        whole fix is that the remainder comes back. Every existing case infers
        that from a balance; none checks the change token EXISTS as a row.
      * `if scInfo.Value <= 0 { continue }` - a zero-value deploy takes no
        collateral at all.
      * "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
        opens its own connection ... which would deadlock against locks held by
        that outer transaction until lock_timeout fires." A deadlock the code
        comment names explicitly, with nothing exercising it.

    Also: wallet SHAPE. Selection behaves differently depending on what the
    wallet holds, and every existing case runs against a wallet of whole
    tokens.

Each case follows the same docstring contract as sc_cases.py:
    WHAT IT CHECKS / WHY IT MATTERS / MANUAL STEPS / PASS-FAIL
"""

import os
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
    """Helpers from sc_cases.py, resolved lazily to avoid a circular import."""
    import sc_cases
    return sc_cases


# ---------------------------------------------------------------------------
# SC-C-13
# ---------------------------------------------------------------------------

def sc_c_13(ctx, ci):
    """
    SC-C-13 - Value ladder: 13 fixed points from 0.001 to 10.375.

    WHAT IT CHECKS
        Every value on a fixed ladder costs exactly itself. The ladder is
        deliberately not random: 0.001, 0.002, 0.009, 0.010, 0.099, 0.100,
        0.500, 0.999, 1.000, 1.001, 1.500, 2.999, 10.375.

    WHY IT MATTERS
        The random cases prove "a fractional value works". This proves WHICH
        values work, and the points are chosen to sit either side of every
        boundary that could plausibly change behaviour:
          * 0.001  the minimum unit
          * 0.009 / 0.010  either side of a denomination step
          * 0.999 / 1.000 / 1.001  either side of a whole token
          * 10.375  needs several whole tokens plus a fraction
        A fix that handles 0.5 but not 0.999 shows up here and nowhere else,
        and the report names the exact value that failed.

    MANUAL STEPS
        For each value in the ladder, on a wallet with at least 30 RBT:
          1. curl -s http://$SENDER:20000/rubix/v1/dids/$DID/balances/rbt
          2. Generate a contract, deploy it with that value (see SC-C-01)
          3. Wait ~6s, read the balance again
          4. The drop must equal that value before moving on

    PASS / FAIL
        PASS  every value costs itself, within half that value
        FAIL  the report lists each value and its actual cost, so the pattern
              of failures identifies which boundary is mishandled
    """
    sc = _link()
    s, _ = ctx.pair(0)
    ladder = [0.001, 0.002, 0.009, 0.010, 0.099, 0.100,
              0.500, 0.999, 1.000, 1.001, 1.500, 2.999, 10.375]

    ready, why = sc._prepare(ctx, s, sum(ladder) + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    results, bad = [], []
    for v in ladder:
        spent, _sc_id, err = sc._deploy_and_measure(ctx, s, v)
        if err:
            bad.append("{}: {}".format(v, err))
            results.append("{}->ERR".format(v))
            continue
        ok = rc.close_enough(spent, v, tol=sc.cost_tolerance(v))
        results.append("{}->{:.4f}{}".format(v, spent, "" if ok else "!"))
        if not ok:
            bad.append("{} cost {:.4f}".format(v, spent))
        time.sleep(2)

    return (not bad), " ".join(results), (
        "" if not bad else "; ".join(bad) +
        "  (a '!' marks a value whose cost did not match)")


# ---------------------------------------------------------------------------
# SC-C-14 / 15 / 16 - wallet shape
# ---------------------------------------------------------------------------

def _shape_case(ctx, ci, shape):
    """Deploy a fractional value from a wallet of a given shape."""
    sc = _link()
    s, r = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    value = sc.rand_value(0.100, 0.999)

    # The wallet shape is BUILT, not looked for. Draining the receiver's whole
    # tokens and sending fractions back makes every shape reachable from any
    # starting state - which is why none of these skip any more.
    if shape == "whole":
        target = s
        ready, why = sc._prepare(ctx, target, 8)
        if not ready:
            return SKIP, "setup incomplete", why
    else:
        ready, why = sc._prepare(ctx, s, 15)
        if not ready:
            return SKIP, "setup incomplete", why
        if shape == "parts":
            okw, whyw = ws.make_parts_wallet(ctx, r, s)
        else:
            okw, whyw = ws.make_mixed_wallet(ctx, r, s)
        if not okw:
            return False, "could not build a {} wallet".format(shape), whyw
        target = r
        ready, why = sc._prepare(ctx, target, value + 0.5)
        if not ready:
            return SKIP, "setup incomplete", why

    try:
        before_rows = db.free_token_values(target["host"], target["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    spent, _sc_id, err = sc._deploy_and_measure(ctx, target, value)
    if err:
        return False, "deploy failed", "from a {} wallet: {}".format(shape, err)

    try:
        after_rows = db.free_token_values(target["host"], target["did"])
        drift = db.denom_drift(target["host"], target["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    exact = rc.close_enough(spent, value, tol=sc.cost_tolerance(value))
    problems = []
    if not exact:
        problems.append("cost {:.4f}, expected {:.3f}".format(spent, value))
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "{} wallet ({} -> {} free rows), value {:.3f}, spent {:.4f}".format(
        shape, len(before_rows), len(after_rows), value, spent), "; ".join(problems)


def sc_c_14(ctx, ci):
    """
    SC-C-14 - Deploy from a wallet holding only whole tokens.

    WHAT IT CHECKS
        A fractional deploy from a wallet of 1.000 tokens costs exactly its
        value, and the counter stays consistent.

    WHY IT MATTERS
        This is the shape the fix was written for: selection picks a whole
        token and must split it. It is also the shape every pre-existing SC
        case happened to use, so it is the one most likely to be right - which
        makes it the control for SC-C-15 and SC-C-16.

    MANUAL STEPS
        Use a freshly funded wallet (generate_local_rbt produces whole tokens),
        then deploy a fractional value as in SC-C-01 and compare the balance.

    PASS / FAIL
        PASS  cost equals the value, no denom drift
        FAIL  a whole token was consumed, or the counter drifted
    """
    return _shape_case(ctx, ci, "whole")


def sc_c_15(ctx, ci):
    """
    SC-C-15 - Deploy from a wallet holding only part tokens.

    WHAT IT CHECKS
        A deploy from a wallet with NO whole token still costs exactly its
        value - the collateral has to be assembled from fractions.

    WHY IT MATTERS
        There is nothing to split here: selection must gather several part
        tokens instead. That is a different path from SC-C-14, and it is the
        one a real wallet reaches after receiving a few fractional transfers.
        If the fix only handles "split one whole token", this is where it
        shows.

    MANUAL STEPS
        Send 0.7, 0.6, 0.5, 0.4 to a fresh receiver (2s apart), confirm via
        the tokens table that it holds no row >= 1.0, then deploy from it.

    PASS / FAIL
        PASS  cost equals the value, no denom drift
        FAIL  rejected while the balance is sufficient -> parts could not back
              a commitment
        SKIP  the receiver already holds whole tokens
    """
    return _shape_case(ctx, ci, "parts")


def sc_c_16(ctx, ci):
    """
    SC-C-16 - Deploy from a wallet holding both whole and part tokens.

    WHAT IT CHECKS
        With both shapes available, the deploy still costs exactly its value.

    WHY IT MATTERS
        The realistic shape, and the one where selection has a CHOICE. Taking a
        whole token when a part would have covered it is not wrong in itself,
        but it is where an over-charge would hide: the wallet has enough of
        everything, so a wrong pick still succeeds and only the arithmetic
        gives it away.

    MANUAL STEPS
        Build a parts wallet as in SC-C-15, then send it an additional 2.0 so
        it holds whole tokens too. Deploy a fractional value from it.

    PASS / FAIL
        PASS  cost equals the value
        FAIL  cost rounds up to a whole token -> selection preferred a whole
              token and did not split it
    """
    return _shape_case(ctx, ci, "mixed")


# ---------------------------------------------------------------------------
# SC-C-17
# ---------------------------------------------------------------------------

def sc_c_17(ctx, ci):
    """
    SC-C-17 - Deploy a value needing a deep split (0.007 from a 1.000 token).

    WHAT IT CHECKS
        A value three orders of magnitude below the available denomination
        still costs exactly itself.

    WHY IT MATTERS
        0.5 splits off a 1.000 token in one step. 0.007 does not - the splitter
        has to work down several levels, and every level is an opportunity to
        lose the remainder. A fix that returns change correctly at one level
        can still sink it at the third, and only a deep value exposes that.

    MANUAL STEPS
        On a wallet holding 1.000 tokens, deploy with "value":0.007, then check
        the balance dropped by 0.007 and NOT by 0.01, 0.1 or 1.0.

    PASS / FAIL
        PASS  cost is 0.007
        FAIL  cost is any larger denomination -> the split stopped early and
              committed more than was asked
    """
    sc = _link()
    s, _ = ctx.pair(0)
    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.007
    spent, _sc_id, err = sc._deploy_and_measure(ctx, s, value)
    if err:
        return False, "deploy failed", err

    exact = rc.close_enough(spent, value, tol=sc.cost_tolerance(value))
    nearest = min([0.01, 0.1, 1.0], key=lambda d: abs(spent - d)) if not exact else None
    return exact, "spent {:.4f} for a {:.3f} deploy".format(spent, value), (
        "" if exact else
        "cost {:.4f}, which is about the {} denomination - the split stopped "
        "short and committed a larger unit than requested".format(spent, nearest))


# ---------------------------------------------------------------------------
# SC-C-18
# ---------------------------------------------------------------------------

def sc_c_18(ctx, ci):
    """
    SC-C-18 - Deploy a value equal to the entire free balance.

    WHAT IT CHECKS
        A deploy that consumes everything either succeeds exactly, or is
        rejected cleanly with the balance untouched. Never partially applied.

    WHY IT MATTERS
        The boundary. Collateral needs the value AND the quorum needs to
        pledge; a wallet with exactly enough has no slack for a rounding error
        in either direction. If the deploy takes slightly more than it should,
        this is the first case where there is nothing left to take it from -
        so an over-charge turns into a failure instead of a silent loss.

    MANUAL STEPS
        Read the exact free balance, then deploy with that value.
        Check the balance afterwards, and check `locked` returned to 0.

    PASS / FAIL
        PASS  succeeds and free balance ends at ~0
        PASS  rejected AND balance unchanged AND nothing left locked
        FAIL  rejected but value went missing, or tokens left Locked
    """
    sc = _link()
    s, _ = ctx.pair(0)
    ready, why = sc._prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    before = sc._bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot determine the exact balance"
    value = round(before["balance"], 3)
    if value <= 0:
        return SKIP, "no balance", "wallet is empty"

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="whole-balance deploy", port=ctx.port)
    time.sleep(SETTLE)
    after = sc._bal(ctx, s)
    if after is None:
        return SKIP, "balance unreadable", "cannot verify the outcome"

    if ok:
        spent = before["balance"] - after["balance"]
        exact = rc.close_enough(spent, value, tol=sc.cost_tolerance(value))
        return exact, "accepted, spent {:.4f} of {:.4f}".format(spent, value), (
            "" if exact else "took {:.4f} for a {:.4f} deploy".format(spent, value))

    problems = []
    if not rc.close_enough(before["balance"], after["balance"]):
        problems.append("rejected but the balance moved: {:.4f} -> {:.4f}".format(
            before["balance"], after["balance"]))
    if not rc.close_enough(before["locked"], after["locked"]):
        problems.append("tokens left LOCKED after rejection")
    return (not problems), "rejected at the exact balance ({:.4f})".format(value), \
        "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-19
# ---------------------------------------------------------------------------

def sc_c_19(ctx, ci):
    """
    SC-C-19 - Twenty sequential deploys from one wallet.

    WHAT IT CHECKS
        Twenty deploys in a row all succeed, and the total spent equals the sum
        of their values.

    WHY IT MATTERS
        Small accounting errors compound. A deploy that loses 0.001 each time
        is invisible once and obvious twenty times, and a counter that drifts
        slightly per deploy eventually stops selection working at all. This is
        also the case most likely to exhaust a denomination and force the
        splitter into a path the short cases never reach.

    MANUAL STEPS
        Loop the SC-C-01 deploy twenty times with a new contract each time,
        recording the balance before the first and after the last. Compare
        against the sum of the values used.

    PASS / FAIL
        PASS  all twenty succeed, total cost equals the sum of the values
        FAIL  the round number of the first failure matters - an early failure
              is a deploy bug, a late one is accumulated state
    """
    sc = _link()
    s, _ = ctx.pair(0)
    rounds = 20
    values = [sc.rand_value(0.050, 0.400) for _ in range(rounds)]

    ready, why = sc._prepare(ctx, s, sum(values) + 6)
    if not ready:
        return SKIP, "setup incomplete", why

    before = sc._bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the total"

    failures = []
    for i, v in enumerate(values, 1):
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            failures.append("round {}: generation failed".format(i))
            break
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="sequential {}".format(i), port=ctx.port)
        if not ok:
            failures.append("round {} (value {}): {}".format(i, v, msg))
            break
        time.sleep(1.5)

    time.sleep(SETTLE)
    after = sc._bal(ctx, s)
    spent = (before["balance"] - after["balance"]) if after else None
    done = rounds - len(failures) if not failures else len(values) - len(failures)
    expected = sum(values[:done]) if failures else sum(values)

    if failures:
        return False, "{}/{} deployed".format(done, rounds), (
            "; ".join(failures) + " - an EARLY failure points at the deploy path, "
            "a LATE one at state the earlier deploys accumulated")

    ok_cost = spent is not None and rc.close_enough(spent, expected,
                                                    tol=sc.cost_tolerance(expected))
    return ok_cost, "{}/{} deployed, spent {:.4f} of {:.4f} expected".format(
        rounds, rounds, spent or -1, expected), (
        "" if ok_cost else "cumulative drift: {:.4f} unaccounted for".format(
            abs((spent or 0) - expected)))
