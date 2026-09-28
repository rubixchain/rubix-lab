"""sc_cases.py - Smart contract cases: collateral at scale, subscription
across many nodes, quorum contention, sustained deploy/execute load.

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
    TOL,
    _sc_new_contract,
    rand_value,
)


# =============================================================================
# SMART CONTRACT
# =============================================================================


# -----------------------------------------------------------------------------
# Collateral / Execute / Quorum pledge / Subscription (core cases + shared helpers)
# -----------------------------------------------------------------------------
#
# Run just this asset:  cd test-plan/full-test && python3 test_runner.py --only 'SC-*'
#
# Every case returns (passed, actual, note):
#     True  -> matched the catalogue's Expected Result
#     False -> did not match: a real finding, investigate
#     SKIP  -> NOT ATTEMPTED, reason in `note`. Never counted as a pass.
#
# HOW TO READ A CASE
#     Each case has a docstring with four fixed parts:
#         WHAT IT CHECKS  - the assertion, in plain words
#         WHY IT MATTERS  - the bug it would catch, and the product code involved
#         MANUAL STEPS    - how to run it BY HAND with curl, no Python needed
#         PASS / FAIL     - exactly what makes it pass or fail
#     If the script and the manual steps ever disagree, the manual steps are the
#     specification - they are what a human can verify independently.
#
# BEFORE RUNNING ANYTHING BY HAND
#     Set these once in your shell. Every MANUAL block below uses them.
#
#         SENDER=192.168.1.104          # any pool host with a DID
#         DID=$(curl -s http://$SENDER:20000/rubix/v1/dids | python3 -c \
#               'import sys,json; print(json.load(sys.stdin)["result"][0])')
#
#     Every state-changing call is a TWO-STEP password challenge. The first POST
#     returns {"result":{"id":"<reqID>"}}, and nothing happens until you sign it:
#
#         curl -s -X POST http://$SENDER:20000/rubix/v1/signature \
#              -H 'Content-Type: application/json' \
#              -d '{"id":"<reqID>","password":"mypassword","signature":""}'
#
#     A first POST that "succeeds" has NOT done anything yet. This is the single
#     most common way a manual check reports a false pass.
#
# WHY THE COLLATERAL CASES EXIST
#     None of the other 17 SC cases give a contract a VALUE - they all deploy and
#     execute at the default. That leaves the collateral accounting path entirely
#     untested, and it has a specific, expensive failure mode:
#
#     LockTokensForSplit selects WHOLE denominations (core/wallet/post_consensus_payload_builder.go:226).
#     Backing a 0.001 commitment therefore picks up a whole 1.000 token. If that
#     token is committed as-is instead of being split, the other 0.999 is destroyed
#     - a 0.001 contract silently costs a full RBT. It is invisible at value 1.0,
#     which is exactly where every other SC case sits.


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

def _sc_bal(ctx, entry):
    """Free RBT for a host's DID, or None if unreadable.

    `balance` is the FREE portion only - tokens locked for an in-flight
    transfer or committed as collateral are reported separately
    (types/balance.go). That distinction is the whole point of these cases.
    """
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _sc_prepare(ctx, entry, need):
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

    detail = _sc_bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)

    # The quorum pledges at least the transaction value
    # (core/consensus/checks.go:539), so it needs headroom too.
    qd = _sc_bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _deploy_and_measure(ctx, entry, value):
    """Deploy one contract at `value` and return (spent, sc_id, error).

    `spent` is the drop in FREE balance across the deploy - which is the number
    the collateral cases are actually about.
    """
    sc_id, err = _sc_new_contract(ctx, entry)
    if err:
        return None, None, "contract generation failed: {}".format(err)

    before = _sc_bal(ctx, entry)
    if before is None:
        return None, sc_id, "balance unreadable before deploy"

    ok, msg, _ = rc.sc_transaction(entry["host"], entry["did"], sc_id, value=value,
                                   data="collateral deploy {}".format(value), port=ctx.port)
    if not ok:
        return None, sc_id, "deploy rejected: {}".format(msg)

    time.sleep(SETTLE)
    after = _sc_bal(ctx, entry)
    if after is None:
        return None, sc_id, "balance unreadable after deploy"
    return (before["balance"] - after["balance"]), sc_id, None


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
    ready, why = _sc_prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
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


# SC-C-04 (the whole-value control) runs BEFORE the repeat cases so that if the
# fractional path is corrupting wallet state, the control has already recorded a
# clean result rather than inheriting the damage.
#
# The SC-S block is strictly sequential and shares one contract: 01 deploys it
# with an early subscriber attached, 02-04 subscribe further nodes at
# increasing chain depths, and 05 compares them all. Running one alone reports
# SKIP rather than a misleading FAIL.


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
# `fund` is roughly what the unit's own cases spend; test_runner adds a safety
# margin on top. Only the quorums are funded in bulk - they are shared by every
# lane and carry all of their pledges at once.
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Collateral - value ladder, wallet shapes, sequential deploys
# -----------------------------------------------------------------------------
# sc_cases_extra.py - SC cases added in the collateral review.
#
# Ordinary SC catalogue cases, not a separate suite.
#
# WHAT THESE ADD OVER THE ORIGINAL SC-C-* SET
#     The original cases prove a deploy COSTS the right amount, measured from the
#     free balance. That is the symptom, not the mechanism. Reading the diff of
#     maneesha/fix/denom-array-updation showed three branches nothing asserted:
#
#       * the split CHANGE. CollectRBTTokens returns childTokensKept, and the
#         whole fix is that the remainder comes back. Every existing case infers
#         that from a balance; none checks the change token EXISTS as a row.
#       * `if scInfo.Value <= 0 { continue }` - a zero-value deploy takes no
#         collateral at all.
#       * "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
#         opens its own connection ... which would deadlock against locks held by
#         that outer transaction until lock_timeout fires." A deadlock the code
#         comment names explicitly, with nothing exercising it.
#
#     Also: wallet SHAPE. Selection behaves differently depending on what the
#     wallet holds, and every existing case runs against a wallet of whole
#     tokens.
#
# Each case follows the same docstring contract as sc_cases.py:
#     WHAT IT CHECKS / WHY IT MATTERS / MANUAL STEPS / PASS-FAIL


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
    s, _ = ctx.pair(0)
    ladder = [0.001, 0.002, 0.009, 0.010, 0.099, 0.100,
              0.500, 0.999, 1.000, 1.001, 1.500, 2.999, 10.375]

    ready, why = _sc_prepare(ctx, s, sum(ladder) + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    results, bad = [], []
    for v in ladder:
        spent, _sc_id, err = _deploy_and_measure(ctx, s, v)
        if err:
            bad.append("{}: {}".format(v, err))
            results.append("{}->ERR".format(v))
            continue
        ok = rc.close_enough(spent, v, tol=cost_tolerance(v))
        results.append("{}->{:.4f}{}".format(v, spent, "" if ok else "!"))
        if not ok:
            bad.append("{} cost {:.4f}".format(v, spent))
        time.sleep(2)

    return (not bad), " ".join(results), (
        "" if not bad else "; ".join(bad) +
        "  (a '!' marks a value whose cost did not match)")


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
    s, _ = ctx.pair(0)
    rounds = 20
    values = [rand_value(0.050, 0.400) for _ in range(rounds)]

    ready, why = _sc_prepare(ctx, s, sum(values) + 6)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _sc_bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the total"

    failures = []
    for i, v in enumerate(values, 1):
        sc_id, err = _sc_new_contract(ctx, s)
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
    after = _sc_bal(ctx, s)
    spent = (before["balance"] - after["balance"]) if after else None
    done = rounds - len(failures) if not failures else len(values) - len(failures)
    expected = sum(values[:done]) if failures else sum(values)

    if failures:
        return False, "{}/{} deployed".format(done, rounds), (
            "; ".join(failures) + " - an EARLY failure points at the deploy path, "
            "a LATE one at state the earlier deploys accumulated")

    ok_cost = spent is not None and rc.close_enough(spent, expected,
                                                    tol=cost_tolerance(expected))
    return ok_cost, "{}/{} deployed, spent {:.4f} of {:.4f} expected".format(
        rounds, rounds, spent or -1, expected), (
        "" if ok_cost else "cumulative drift: {:.4f} unaccounted for".format(
            abs((spent or 0) - expected)))


# -----------------------------------------------------------------------------
# Collateral - DB row checks (tokens, chain, transactions)
# -----------------------------------------------------------------------------
# sc_cases_db.py - row-level database checks around a smart contract deploy.
#
# These go below the totals every other case works
# from: which ROWS exist afterwards, not just what they sum to.
#
# WHY ROW-LEVEL AT ALL
#     "Committed rose by 0.354" is consistent with several different realities.
#     It is consistent with the fix working - a 1.000 token split, 0.354
#     committed, 0.646 returned as a change row. It is also consistent with a
#     0.354 token that happened to be lying in the wallet being committed whole,
#     which proves nothing about splitting. And on a busy wallet it is consistent
#     with two unrelated movements cancelling out.
#
#     The split fix's actual claim is that CollectRBTTokens returns
#     childTokensKept and those children are persisted. That claim is about ROWS.
#     Only reading them settles it.
#
# SCHEMA COUPLING
#     These bind to table and column names (tokens.parent_token_id,
#     tokenchain.position, transactions.info). That is a real cost: if the
#     product reshapes those tables, these break before anything else. They are
#     worth it here because the subject is exactly what lands in those
#     tables - but they are the first cases to revisit after a schema change.
#     DB-SCHEMA.md records the shape they were written against.


# -----------------------------------------------------------------------------
# Quorum capacity - per-quorum pledge accounting
# -----------------------------------------------------------------------------
# sc_cases_quorum.py - what the QUORUM does during a smart contract deploy.
#
# WHY A SEPARATE GROUP
#     Every other SC case looks at the deployer. These look at the other side of
#     the same transaction. A deploy can be perfectly correct from the deployer's
#     wallet and still leave the quorum's books wrong - and because the quorum
#     signs for the whole fleet, that damage is shared by everyone.
#
#     Pledging moves quorum tokens out of Free (core/wallet/pledge.go:222), so
#     the quorum's token_denom must decrement exactly as the deployer's does.
#     That is the SAME class of bug as the deploy and FT-burn paths, on a
#     separate path - so a failure here is its own finding.


def _quorum_of(ctx, entry):
    return ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)


# ---------------------------------------------------------------------------
# SC-Q-11
# ---------------------------------------------------------------------------

def sc_q_11(ctx, ci):
    """
    SC-Q-11 - Concurrent deploys from different senders through one quorum.

    WHAT IT CHECKS
        Several senders deploy at the same time through the same quorum. All
        succeed, each costs its own value, and the quorum's counter is still
        consistent afterwards.

    WHY IT MATTERS
        A shared quorum is the one piece of state every lane touches at once,
        which makes it the most likely place for a race. Each deploy decrements
        the quorum's counter; if those decrements are not serialised the
        counter ends up wrong, and it fails later for a sender that had nothing
        to do with this test.

        Sequential deploys through one quorum are already covered. This is the
        same operation with the interleaving that only a real fleet produces.

    MANUAL STEPS
        Point three senders at one quorum, then fire a deploy from each at the
        same moment. Afterwards compare that quorum's denom listing against its
        real free tokens.

    PASS / FAIL
        PASS  all deploys succeed and the quorum counter is consistent
        FAIL  a deploy is rejected for pledge shortage while the quorum clearly
              had capacity -> serialisation problem, not a funding one
        FAIL  the counter drifts -> concurrent decrements are not safe
        SKIP  fewer than 3 hosts in this unit
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    senders = [e for e in (list(ctx.senders) + list(ctx.receivers))][:3]
    if len(senders) < 3:
        return SKIP, "need 3 hosts", (
            "this unit has {} host(s); concurrency needs at least 3 "
            "senders".format(len(senders)))
    q = _quorum_of(ctx, senders[0])
    if q is None:
        return SKIP, "no quorum", "cannot test shared-quorum concurrency"

    values = [rand_value(0.100, 0.500) for _ in senders]
    for e, v in zip(senders, values):
        rc.quorum_add(e["host"], q["did"], ctx.port)
        ready, why = _sc_prepare(ctx, e, v + 4)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    prepared = []
    for e, v in zip(senders, values):
        sc_id, err = _sc_new_contract(ctx, e)
        if err:
            return SKIP, "generation failed", "{}: {}".format(e["host"], err)
        prepared.append((e, sc_id, v))

    try:
        before = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def fire(item):
        e, sc_id, v = item
        return e, v, rc.sc_transaction(e["host"], e["did"], sc_id, value=v,
                                       data="concurrent deploy", port=ctx.port)

    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 2)

    try:
        after = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    failed = [(e["host"], v, res[1]) for e, v, res in outcomes if not res[0]]
    problems = []
    for host, v, msg in failed:
        problems.append("{} (value {}) rejected: {}".format(host, v, msg))
    if drift:
        problems.append("quorum counter drifted after concurrent deploys: "
                        + db.describe_drift(drift) +
                        " - decrements are not safe to interleave")

    return (not problems), "{}/{} concurrent deploys ok, quorum denom {}".format(
        len(outcomes) - len(failed), len(outcomes),
        "ok" if not drift else "DRIFT"), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-Q-12
# ---------------------------------------------------------------------------

def sc_q_12(ctx, ci):
    """
    SC-Q-12 - NEW quorum counter drift caused by one concurrent burst.

    WHAT IT CHECKS
        Measure the quorum's counter-vs-reality gap BEFORE a burst of
        concurrent deploys and again after, and report only the CHANGE.

    WHY IT MATTERS
        Every other drift case reports an ABSOLUTE gap, and on this fleet that
        number is now permanently non-zero. Quorum .104 sits at exactly:

            denom 0.001  counter 5123  free 5120   drift 3
            denom 0.010  counter 5490  free 5488   drift 2      = 0.023 RBT

        That is history. Between the run that created it and the reading above,
        the counters grew by roughly 3,400 and 3,640 - thousands of further
        pledges - and the drift did not move by one. Ordinary operation counts
        correctly; the gap was made once, during a 28-wallet burst, and it
        never self-corrects because nothing re-derives the array.

        So GEN-IN-08 and SC-X-04 will now fail on EVERY future run against a
        number that has nothing to do with that run, and a genuine new leak
        would hide underneath it. A permanently red case has stopped carrying
        information.

        Measuring the delta fixes both halves: it is zero on a clean build
        regardless of accumulated history, and it attributes any new drift to
        the burst that caused it.

    MANUAL STEPS
        1. Record the quorum's array and its real free tokens (see DB-SCHEMA
           "Recipes"), and note the per-denomination gap.
        2. Fire concurrent deploys from several senders through that quorum.
        3. Wait for everything to settle, then record both again.
        4. Compare the GAPS, not the counts.

    PASS / FAIL
        PASS  no denomination's gap widened - concurrent pledging is safe on
              this build, whatever history the wallet carries
        FAIL  a gap widened -> this burst lost that many decrements. Concurrent
              writers to one (did, denom) row are not serialised
        SKIP  fewer than 3 hosts in this unit, or no quorum
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    senders = [e for e in (list(ctx.senders) + list(ctx.receivers))][:4]
    if len(senders) < 3:
        return SKIP, "need 3 hosts", (
            "this unit has {} host(s); a burst needs at least 3".format(len(senders)))
    q = _quorum_of(ctx, senders[0])
    if q is None:
        return SKIP, "no quorum", "cannot measure quorum drift without one"

    def gaps():
        """Per-denomination (counter - real_free), keyed to 3dp."""
        counter = db.denom_counter(q["host"], q["did"])
        real = db.real_free_denoms(q["host"], q["did"])
        keys = set(round(d, 3) for d in counter) | set(round(d, 3) for d in real)
        out = {}
        for k in keys:
            c = sum(v for d, v in counter.items() if round(d, 3) == k)
            r = sum(v for d, v in real.items() if round(d, 3) == k)
            out[k] = c - r
        return out

    values = [rand_value(0.100, 0.500) for _ in senders]
    for e, v in zip(senders, values):
        rc.quorum_add(e["host"], q["did"], ctx.port)
        ready, why = _sc_prepare(ctx, e, v + 4)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    # Several rounds, so the burst is wide AND repeated - one round of three is
    # what SC-Q-11 already does and it has never reproduced the drift.
    rounds = 5
    prepared = []
    for _ in range(rounds):
        for e, v in zip(senders, values):
            sc_id, err = _sc_new_contract(ctx, e)
            if err:
                return SKIP, "generation failed", "{}: {}".format(e["host"], err)
            prepared.append((e, sc_id, v))

    try:
        before = gaps()
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def fire(item):
        e, sc_id, v = item
        return rc.sc_transaction(e["host"], e["did"], sc_id, value=v,
                                 data="SC-Q-12 burst", port=ctx.port)

    with ThreadPoolExecutor(max_workers=len(senders)) as pool:
        outcomes = list(pool.map(fire, prepared))
    ok_count = sum(1 for o in outcomes if o and o[0])

    # Generous settle: a widened gap must not be an artefact of reading while
    # pledges are still open.
    time.sleep(SETTLE * 5)

    try:
        after = gaps()
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    widened = {}
    for k in set(before) | set(after):
        delta = after.get(k, 0) - before.get(k, 0)
        if delta > 0:
            widened[k] = delta

    note = ""
    if widened:
        note = ("this burst of {} concurrent deploy(s) lost {} decrement(s): "
                "{} - concurrent writers to the same (did, denom) row are not "
                "serialised".format(
                    len(prepared), sum(widened.values()),
                    ", ".join("{:.3f}+{}".format(k, v)
                              for k, v in sorted(widened.items()))))

    return (not widened), "{}/{} deploy(s) ok through quorum {}, new drift {}".format(
        ok_count, len(prepared), q["host"], sum(widened.values()) if widened else 0), note


# -----------------------------------------------------------------------------
# Subscription - staggered subscribers, parts execute
# -----------------------------------------------------------------------------
# sc_cases_subs.py - subscription at scale, and execution from parts wallets.
#
# WHY MORE SUBSCRIPTION CASES
#     SC-S-01..05 use one contract and four subscribers. That answered the
#     question "do subscribers converge?" for a single small case. These push on
#     the parts of it a small case cannot reach:
#
#       * MANY contracts and MANY subscribers at once, which is what a real
#         network looks like
#       * comparing FULL TOKEN DETAIL, not just chain length. Two nodes can agree
#         on how many entries a chain has and disagree about what is in them
#       * subscribing DURING an execute, not between them
#       * depth 20, where a back-fill that fetches "recent" entries runs out
#
#     The path being probed: SubsribeContractSetup only back-fills when the
#     contract folder is absent locally, and syncSmartContractTransaction returns
#     silently when the metadata carries no PeerID. Either way subscribe reports
#     SUCCESS - so an incomplete subscriber looks exactly like a healthy one.


# Shared by the SC-S-06/07 pair: 06 builds the fixture, 07 inspects it.
_MATRIX = {"contracts": [], "subscribers": []}


def _chain_len(host, sc_id, port):
    ok, chain, _ = rc.get_sc_chain(host, sc_id, port)
    return len(chain) if ok else -1


# ---------------------------------------------------------------------------
# SC-S-06
# ---------------------------------------------------------------------------

def sc_s_06(ctx, ci):
    """
    SC-S-06 - Many contracts, many subscribers, joining at many depths.

    WHAT IT CHECKS
        Three contracts are deployed and executed repeatedly. Subscribers join
        at staggered points - before deploy, right after, after 1, 3, 5 and 10
        executes - spread across every spare host in the lane. At the end every
        subscriber's chain length is compared against the owner's.

    WHY IT MATTERS
        SC-S-01..05 proved convergence for one contract and four subscribers.
        This is the same question at the scale the fleet allows, and scale
        changes two things: several contracts are syncing at once (so a
        back-fill can fetch the wrong one), and a subscriber joining at depth
        10 needs far more history than one joining at depth 1.

        A partial back-fill that looked fine at depth 3 has somewhere to hide
        at depth 1 and nowhere at depth 10.

    MANUAL STEPS
        Deploy 3 contracts. Between executes, subscribe a different node to
        each. Then for every (node, contract) pair:
          curl -s http://$NODE:20000/rubix/v1/smart_contracts/<SC>/chain
        and compare lengths against the deployer's.

    PASS / FAIL
        PASS  every subscriber matches the owner on every contract it joined
        FAIL  the report names node, contract and the depth it joined at, which
              together say whether lateness or contract count is the factor
        SKIP  fewer than 4 spare hosts in this unit
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 4:
        return SKIP, "not enough hosts", (
            "unit has {} spare host(s); this case needs at least 4 to stagger "
            "subscribers meaningfully".format(len(spare)))

    ready, why = _sc_prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    n_contracts = min(3, max(1, len(spare) // 2))
    contracts = []
    for i in range(n_contracts):
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        contracts.append(sc_id)

    # A node that subscribes BEFORE anything is deployed.
    early = spare[0]
    for sc_id in contracts:
        rc.subscribe_smart_contract(early["host"], sc_id, ctx.port)
    _MATRIX["subscribers"] = [("before deploy", early, 0)]
    time.sleep(2)

    for sc_id in contracts:
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=rand_value(0.010, 0.200),
                                       data="matrix deploy", port=ctx.port)
        if not ok:
            return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    # Stagger the rest: subscribe, execute a few times, subscribe the next.
    schedule = [(1, "after deploy"), (1, "after 1 execute"),
                (2, "after 3 executes"), (2, "after 5 executes"),
                (5, "after 10 executes")]
    idx = 1
    for executes, label in schedule:
        for _ in range(executes):
            for sc_id in contracts:
                rc.sc_transaction(s["host"], s["did"], sc_id,
                                  value=rand_value(0.010, 0.100),
                                  data="matrix execute", port=ctx.port)
            time.sleep(1.5)
        if idx >= len(spare):
            break
        node = spare[idx]
        idx += 1
        for sc_id in contracts:
            rc.subscribe_smart_contract(node["host"], sc_id, ctx.port)
        depth = _chain_len(s["host"], contracts[0], ctx.port)
        _MATRIX["subscribers"].append((label, node, depth))
        time.sleep(2)

    time.sleep(SETTLE * 2)
    _MATRIX["contracts"] = contracts
    _MATRIX["owner"] = s

    problems, rows = [], []
    for sc_id in contracts:
        owner_len = _chain_len(s["host"], sc_id, ctx.port)
        for label, node, depth in _MATRIX["subscribers"]:
            got = _chain_len(node["host"], sc_id, ctx.port)
            if got != owner_len:
                problems.append("{} on {} ({}): {} of {} entries".format(
                    sc_id[:10], node["host"], label, got, owner_len))
        rows.append("{}={}".format(sc_id[:8], owner_len))

    return (not problems), "{} contract(s) {} | {} subscriber(s)".format(
        len(contracts), " ".join(rows), len(_MATRIX["subscribers"])), (
        "" if not problems else "; ".join(problems[:6]) +
        (" (+{} more)".format(len(problems) - 6) if len(problems) > 6 else "") +
        " - a subscriber short of the owner joined late and never caught up, "
        "yet is still allowed to execute")


# ---------------------------------------------------------------------------
# SC-S-07
# ---------------------------------------------------------------------------

def sc_s_07(ctx, ci):
    """
    SC-S-07 - Compare full token detail across subscribers, not just chain length.

    WHAT IT CHECKS
        For every contract and every subscriber from SC-S-06, the contract's
        token row is compared field by field: value, status, latest position.

    WHY IT MATTERS
        This is the real answer to "all subscribed nodes should have the same
        data". Chain LENGTH matching is a weaker claim than chain CONTENT
        matching - two nodes can hold the same number of entries and disagree
        about what those entries say, which is precisely what a partial sync
        followed by live pubsub events would produce.

        A node whose token row disagrees is still a legitimate subscriber and
        will still be allowed to execute, because execute is gated on
        subscription rather than on agreement.

    MANUAL STEPS
        On the owner and each subscriber:
          psql -h $NODE -p 5433 -U rubix -d rubix -c \\
            "SELECT token_id, token_value, token_status, latest_position
               FROM tokens WHERE token_id='<SC_ID>';"
        Every node should return identical values.

    PASS / FAIL
        PASS  all subscribers agree with the owner on every field
        FAIL  the report names the node, the contract and the field that differs
        SKIP  SC-S-06 did not run, or psycopg2 unavailable
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    if not _MATRIX.get("contracts"):
        return SKIP, "no subscription fixture", (
            "SC-S-06 did not complete - these two run as a pair in one lane")

    owner = _MATRIX["owner"]

    def detail(host, sc_id):
        rows = db.query(
            host,
            "SELECT token_value, token_status, latest_position FROM tokens "
            "WHERE token_id = %s", (sc_id,))
        if not rows:
            return None
        v, st, pos = rows[0]
        return (round(float(v), 3), int(st), int(pos))

    problems, compared = [], 0
    try:
        for sc_id in _MATRIX["contracts"]:
            ref = detail(owner["host"], sc_id)
            if ref is None:
                problems.append("owner has no token row for {}".format(sc_id[:10]))
                continue
            for label, node, _depth in _MATRIX["subscribers"]:
                got = detail(node["host"], sc_id)
                compared += 1
                if got is None:
                    problems.append("{} ({}) has NO token row for {}".format(
                        node["host"], label, sc_id[:10]))
                elif got != ref:
                    problems.append("{} ({}) {}: value/status/position {} vs owner {}".format(
                        node["host"], label, sc_id[:10], got, ref))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    return (not problems), "{} (node, contract) pair(s) compared".format(compared), (
        "" if not problems else "; ".join(problems[:5]) +
        (" (+{} more)".format(len(problems) - 5) if len(problems) > 5 else "") +
        " - these nodes are subscribed and may execute against a contract they "
        "do not see the same way the owner does")


# ---------------------------------------------------------------------------
# SC-S-08
# ---------------------------------------------------------------------------

def sc_s_08(ctx, ci):
    """
    SC-S-08 - Subscribe while an execute is in flight.

    WHAT IT CHECKS
        A node subscribes at the same moment the owner is executing. Afterwards
        its chain matches the owner's.

    WHY IT MATTERS
        Every other subscription case joins BETWEEN operations, when the
        contract is at rest. Joining mid-execute is the case where back-fill
        and a live pubsub event can both deliver the same entry, or neither
        can: the sync reads a chain that is still being written.

        Duplicate delivery and missed delivery look identical from the API -
        subscribe succeeds either way - so only comparing the end state
        afterwards distinguishes them.

    MANUAL STEPS
        Start an execute and, without waiting for it, immediately subscribe a
        second node. Then compare chains once both have settled.

    PASS / FAIL
        PASS  subscriber's chain matches the owner's
        FAIL  shorter -> the in-flight entry was missed by both paths
        FAIL  longer -> the entry was delivered twice
        SKIP  no spare host
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a second node to subscribe"
    other = spare[-1]

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="race deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    # Fire the execute and the subscribe together.
    def do_execute():
        return rc.sc_transaction(s["host"], s["did"], sc_id,
                                 value=rand_value(0.010, 0.100),
                                 data="in-flight execute", port=ctx.port)

    def do_subscribe():
        time.sleep(0.2)   # just after the execute starts, not before it
        return rc.subscribe_smart_contract(other["host"], sc_id, ctx.port)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fx = pool.submit(do_execute)
        fs = pool.submit(do_subscribe)
        ex_ok = fx.result()[0]
        sub_ok = fs.result()[0]

    if not ex_ok:
        return SKIP, "execute failed", "cannot judge the race if the execute did not run"
    if not sub_ok:
        return False, "subscribe failed during an execute", (
            "subscribing while the contract was being written was rejected")

    time.sleep(SETTLE * 2)
    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    sub_len = _chain_len(other["host"], sc_id, ctx.port)

    if sub_len == owner_len:
        return True, "owner={} subscriber={}".format(owner_len, sub_len), ""
    direction = "missed the in-flight entry" if sub_len < owner_len \
        else "received an entry twice"
    return False, "owner={} subscriber={}".format(owner_len, sub_len), (
        "subscribing mid-execute {} - back-fill and the live event did not "
        "combine correctly".format(direction))


# ---------------------------------------------------------------------------
# SC-S-09
# ---------------------------------------------------------------------------

def sc_s_09(ctx, ci):
    """
    SC-S-09 - Subscribe to a contract with twenty entries of history.

    WHAT IT CHECKS
        After twenty executes, a fresh node subscribes and must receive the
        whole chain.

    WHY IT MATTERS
        Depth is the variable that separates "back-fill is missing" from
        "back-fill is incomplete". A sync that fetches only the most recent
        entries passes at depth 1, probably passes at depth 3, and cannot pass
        at depth 20. The SIZE of the gap is the diagnosis: one missing entry is
        a delivery problem, nineteen is no back-fill at all.

    MANUAL STEPS
        Deploy, execute twenty times, then subscribe a fresh node and compare
        chain lengths.

    PASS / FAIL
        PASS  chain matches the owner's
        FAIL  the gap size tells you whether back-fill fetched nothing, a
              window, or everything but the tail
        SKIP  no spare host
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a node that has not yet subscribed"
    other = spare[0]

    ready, why = _sc_prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="deep deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    for i in range(20):
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=rand_value(0.005, 0.050),
                                       data="depth {}".format(i + 1), port=ctx.port)
        if not ok:
            return SKIP, "could not build depth", (
                "execute {} failed: {}".format(i + 1, msg))
        time.sleep(1)
    time.sleep(SETTLE)

    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    ok, msg = rc.subscribe_smart_contract(other["host"], sc_id, ctx.port)
    if not ok:
        return False, "subscribe failed", str(msg)
    time.sleep(SETTLE * 2)
    sub_len = _chain_len(other["host"], sc_id, ctx.port)

    if sub_len == owner_len:
        return True, "owner={} late subscriber={}".format(owner_len, sub_len), ""
    gap = owner_len - sub_len
    if sub_len <= 1:
        why_note = "back-fill fetched nothing at all"
    elif gap <= 2:
        why_note = "back-fill fetched almost everything but missed the tail"
    else:
        why_note = "back-fill fetched only a window of recent entries"
    return False, "owner={} late subscriber={}".format(owner_len, sub_len), (
        "missing {} of {} entries - {}".format(gap, owner_len, why_note))


# ---------------------------------------------------------------------------
# SC-S-10
# ---------------------------------------------------------------------------

def sc_s_10(ctx, ci):
    """
    SC-S-10 - A later subscriber must see a previous subscriber's execute.

    WHAT IT CHECKS
        Node A subscribes and executes. Node B then subscribes and must receive
        A's execute, not only the owner's entries.

    WHY IT MATTERS
        Every other case builds history from the OWNER. This builds it from a
        subscriber. If back-fill syncs from the deploying node's copy of the
        chain, an entry written by a different subscriber could be missing from
        whatever B receives - and nothing about the subscribe call would say
        so.

        It also confirms the chain is genuinely shared state rather than
        owner-authored state that others merely observe.

    MANUAL STEPS
        1. Subscribe node A, execute from A.
        2. Subscribe node B.
        3. Compare B's chain against the owner's and against A's.

    PASS / FAIL
        PASS  all three agree
        FAIL  B is short by exactly A's execute -> back-fill only carries
              owner-authored history
        SKIP  fewer than 2 spare hosts
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 2:
        return SKIP, "need 2 spare hosts", (
            "unit has {}; this case needs an executing subscriber and a later "
            "one".format(len(spare)))
    a, b = spare[0], spare[1]

    ready, why = _sc_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="shared-history deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    ok, msg = rc.subscribe_smart_contract(a["host"], sc_id, ctx.port)
    if not ok:
        return SKIP, "subscribe A failed", str(msg)
    time.sleep(SETTLE)

    ready, why = _sc_prepare(ctx, a, 2)
    if not ready:
        return SKIP, "setup incomplete for A", why
    ok, msg, _ = rc.sc_transaction(a["host"], a["did"], sc_id,
                                   value=rand_value(0.010, 0.100),
                                   data="subscriber execute", port=ctx.port)
    if not ok:
        return SKIP, "A could not execute", str(msg)
    time.sleep(SETTLE)

    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    a_len = _chain_len(a["host"], sc_id, ctx.port)

    ok, msg = rc.subscribe_smart_contract(b["host"], sc_id, ctx.port)
    if not ok:
        return False, "subscribe B failed", str(msg)
    time.sleep(SETTLE * 2)
    b_len = _chain_len(b["host"], sc_id, ctx.port)

    agree = owner_len == a_len == b_len
    return agree, "owner={} executor={} late={}".format(owner_len, a_len, b_len), (
        "" if agree else
        "the later subscriber holds {} entries against the owner's {} - an "
        "execute written by another SUBSCRIBER did not reach it, so back-fill "
        "carries owner-authored history only".format(b_len, owner_len))


# -----------------------------------------------------------------------------
# Concurrency - simultaneous deploys
# -----------------------------------------------------------------------------
# sc_cases_stress.py - concurrency and scale against the collateral path.
#
# WHY THESE EXIST
#     Every other collateral case runs one deploy at a time on a quiet wallet.
#     The collateral-split code comment names a hazard that only appears when that is
#     not true:
#
#         "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
#          opens its own connection and upserts the burnt parent row, which
#          would deadlock against locks held by that outer transaction until
#          lock_timeout fires."
#
#     The ordering was chosen to avoid a deadlock. Nothing currently tries to
#     cause one. Concurrent deploys from a single wallet are exactly the shape
#     that would - two splits, two genesis persists, one wallet's rows.
#
#     The other target is token_denom itself. Two counter fixes
#     write that table from DIFFERENT code paths - post_consensus_persistence.go
#     for a deploy, token_chain.go for an FT burn - and neither appears to
#     coordinate with the other. Running both against one DID at once is the
#     obvious race and is covered by CRS-C-02 in the cross-asset module.


# ---------------------------------------------------------------------------
# SC-C-12
# ---------------------------------------------------------------------------

def sc_c_12(ctx, ci):
    """
    SC-C-12 - Several deploys fired at once from ONE wallet.

    WHAT IT CHECKS
        Five deploys with values are fired simultaneously from a single wallet.
        All succeed, the total cost equals the sum of the values, and the
        counter is consistent afterwards.

    WHY IT MATTERS
        This is the case the collateral-split code comment defends against. The
        collateral split runs BEFORE the outer transaction opens, because
        PersistGenesisTransaction takes its own connection and would otherwise
        deadlock against locks the outer transaction already holds - waiting
        out lock_timeout before failing.

        One wallet is the point: concurrent deploys from DIFFERENT wallets
        touch different rows. From one wallet they contend for the same tokens,
        the same denom rows, and the same genesis-persist path. If the ordering
        is not sufficient, this is where it surfaces - as a timeout, a
        double-spend of the same parent token, or a counter that no longer
        matches reality.

    MANUAL STEPS
        Generate five contracts from one DID, then POST all five deploys
        without waiting for each to return. Afterwards compare the balance drop
        against the sum of the values, and the denom listing against reality.

    PASS / FAIL
        PASS  all five succeed, cost is exact, counter consistent
        FAIL  a deploy times out or reports a lock error -> the deadlock the
              comment describes
        FAIL  cost does not match -> a parent token was consumed twice
        FAIL  counter drifts -> concurrent decrements on one DID are unsafe
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    n = 5
    values = [rand_value(0.050, 0.300) for _ in range(n)]
    ready, why = _sc_prepare(ctx, s, sum(values) + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    prepared = []
    for v in values:
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        prepared.append((sc_id, v))

    try:
        before_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before_snap["denom_drift"]:
        return SKIP, "already drifting", (
            "counter inconsistent before the race, so nothing here is "
            "attributable - see GEN-IN-08")
    before = _sc_bal(ctx, s)

    def fire(item):
        sc_id, v = item
        started = time.time()
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="concurrent same-wallet deploy",
                                       port=ctx.port)
        return v, ok, str(msg), round(time.time() - started, 1)

    with ThreadPoolExecutor(max_workers=n) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 2)

    after = _sc_bal(ctx, s)
    try:
        after_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before_snap, after_snap)

    failed = [(v, m, secs) for v, ok, m, secs in outcomes if not ok]
    ok_values = [v for v, ok, _m, _s in outcomes if ok]
    spent = (before["balance"] - after["balance"]) if (before and after) else None
    expected = sum(ok_values)

    problems = []
    for v, m, secs in failed:
        low = m.lower()
        if "lock" in low or "timeout" in low or "deadlock" in low:
            problems.append("value {} failed after {}s with a LOCK/TIMEOUT error "
                            "({}) - this is the deadlock the collateral-split "
                            "ordering exists to prevent".format(v, secs, m[:80]))
        else:
            problems.append("value {} rejected after {}s: {}".format(v, secs, m[:80]))
    if spent is not None and ok_values and not rc.close_enough(
            spent, expected, tol=cost_tolerance(expected)):
        problems.append("spent {:.4f} for {} successful deploy(s) totalling "
                        "{:.4f} - a parent token was likely consumed twice".format(
                            spent, len(ok_values), expected))
    if drift:
        problems.append("counter drifted after concurrent deploys on ONE DID: "
                        + db.describe_drift(drift))

    return (not problems), "{}/{} concurrent deploys ok, spent {:.4f}".format(
        len(ok_values), n, spent if spent is not None else -1), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-26
# ---------------------------------------------------------------------------

def sc_c_26(ctx, ci):
    """
    SC-C-26 - Many nodes deploying at once through the shared quorums.

    WHAT IT CHECKS
        Every host in this unit deploys a valued contract simultaneously. All
        should succeed, each costing its own value, with no quorum's counter
        drifting.

    WHY IT MATTERS
        The fleet-scale version. Each deployer is independent, so this is not
        testing the same contention as SC-C-12 - it is testing whether the
        SHARED quorums hold up when every lane hits them at once. Quorum
        capacity, pledge serialisation and the quorum-side denom decrement all
        come under pressure together.

        Expect some noise here: a rejection for genuine pledge shortage is a
        funding result, not a defect, and the case says so rather than
        reporting it as a failure. What matters is a rejection while the quorum
        clearly had capacity, or a counter that ends up wrong.

    MANUAL STEPS
        Fire a deploy from every available host at the same moment, then check
        each quorum's pledged total and denom listing.

    PASS / FAIL
        PASS  all deploys succeed, no quorum counter drifts
        RECORD  how many succeeded and the failure reasons - a pledge shortage
              is a capacity finding, not a correctness one
        FAIL  a quorum's counter drifts -> concurrent pledging is unsafe
        SKIP  fewer than 3 hosts in this unit
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    hosts = list(ctx.senders) + list(ctx.receivers)
    seen, deployers = set(), []
    for e in hosts:
        if e["host"] not in seen:
            seen.add(e["host"])
            deployers.append(e)
    if len(deployers) < 3:
        return SKIP, "need 3+ hosts", (
            "unit has {} host(s); fleet-scale concurrency needs more".format(
                len(deployers)))

    values = [rand_value(0.050, 0.250) for _ in deployers]
    prepared = []
    for e, v in zip(deployers, values):
        ready, why = _sc_prepare(ctx, e, v + 4)
        if not ready:
            continue
        sc_id, err = _sc_new_contract(ctx, e)
        if err:
            continue
        prepared.append((e, sc_id, v))
    if len(prepared) < 3:
        return SKIP, "could not prepare enough hosts", (
            "only {} of {} hosts ready".format(len(prepared), len(deployers)))

    try:
        q_before = {q["host"]: db.snapshot(q["host"], q["did"])
                    for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def fire(item):
        e, sc_id, v = item
        ok, msg, _ = rc.sc_transaction(e["host"], e["did"], sc_id, value=v,
                                       data="fleet-scale deploy", port=ctx.port)
        return e["host"], v, ok, str(msg)

    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 3)

    try:
        q_after = {q["host"]: db.snapshot(q["host"], q["did"])
                   for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    succeeded = [o for o in outcomes if o[2]]
    pledge_short, other_fail = [], []
    for host, v, ok, msg in outcomes:
        if ok:
            continue
        low = msg.lower()
        (pledge_short if ("pledge" in low or "insufficient" in low)
         else other_fail).append((host, v, msg))

    problems = []
    for qh in q_before:
        drift = db.new_drift(q_before[qh], q_after[qh])
        if drift:
            problems.append("quorum {} counter drifted under concurrent load: {}".format(
                qh, db.describe_drift(drift)))
    for host, v, msg in other_fail:
        problems.append("{} (value {}) rejected for a non-capacity reason: {}".format(
            host, v, msg[:80]))

    note = "; ".join(problems)
    if pledge_short and not problems:
        note = ("{} deploy(s) rejected for pledge shortage - a quorum CAPACITY "
                "result at this concurrency, not a correctness one. Raise "
                "--fund-quorum or lower the concurrency to separate the "
                "two.".format(len(pledge_short)))

    return (not problems), "{}/{} deploys ok across {} host(s); {} pledge-short".format(
        len(succeeded), len(outcomes), len(prepared), len(pledge_short)), note


# -----------------------------------------------------------------------------
# Scale - production-level volume
# -----------------------------------------------------------------------------
# sc_cases_scale.py - the collateral and denomination paths under production-level load.
#
# Run as a stress selection, AFTER the functional
# suite passes - at this volume a single rejection tells you nothing, because you
# cannot separate a real defect from ordinary contention unless you already know
# the basics are sound.
#
# WHAT CHANGES AT SCALE
#     The functional cases ask "is this correct?". These ask "is it STILL correct
#     after five hundred of them?" - which is a different question, because the
#     failures that matter here are the ones that accumulate:
#
#       * a counter that drifts by one row per thousand operations
#       * a split that loses 0.001 every few hundred deploys
#       * a subscriber that falls behind once the chain is deep enough
#       * a quorum whose pledges outrun its releases under sustained load
#
#     None of those are visible in a five-operation test, and all of them take a
#     fleet down eventually.
#
# HOW THESE ARE WRITTEN DIFFERENTLY
#     1. CHECKPOINTS. The invariant is re-checked every N operations, and the
#        result reports the operation count at which it FIRST broke. "Drifted at
#        operation 340" is a regression signal you can compare between releases;
#        "drifted" is not.
#     2. Individual failures are counted, not fatal. A pledge shortage at high
#        concurrency is a capacity result. A broken invariant is a defect. The
#        cases keep those separate and say which they found.
#     3. Aggregate reconciliation matters more than any single operation. The
#        real question at the end is whether the books still balance.


# Scaled by --scale so a smoke run and a full run use the same code path.
# test_runner passes args through; a case reads its own via ctx.args.
DEFAULT_SCALE = 1.0


def _sc_scale_scale(ctx):
    return float(getattr(ctx.args, "scale", DEFAULT_SCALE) or DEFAULT_SCALE)


def _sc_scale_n(ctx, base, floor=3):
    return max(floor, int(base * _sc_scale_scale(ctx)))


def _sc_scale_hosts(ctx):
    seen, out = set(), []
    for e in list(ctx.senders) + list(ctx.receivers):
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# SC-X-01
# ---------------------------------------------------------------------------

def sc_x_01(ctx, ci):
    """
    SC-X-01 - Sustained deploys with the counter checked at every checkpoint.

    WHAT IT CHECKS
        Deploy contracts continuously from one wallet - 200 by default - and
        re-check the denomination counter every 25. Reports the operation count
        at which the counter FIRST disagreed with reality, and the total value
        drift across the whole run.

    WHY IT MATTERS
        A split that loses 0.001 occasionally is invisible in a five-deploy
        test and fatal over a day of production. So is a counter that drifts by
        one row per few hundred operations: nothing fails at the time, and then
        selection starts failing for reasons that look unrelated.

        The checkpoint is the point. "Drifted at deploy 340" is a number you
        can compare against the next release. "Drifted" is not.

    MANUAL STEPS
        Loop the SC-C-01 deploy, and every 25 iterations run:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;
        Note the iteration number the two first disagree at.

    PASS / FAIL
        PASS  every checkpoint consistent, and total spend matches the sum of
              the values within tolerance
        FAIL  reports the FIRST checkpoint that drifted - the earlier it is,
              the more serious
        RECORD  deploys that failed for capacity reasons are counted separately
              and do not fail the case
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    total = _sc_scale_n(ctx, 200, floor=10)
    checkpoint = max(5, total // 8)
    values = [rand_value(0.010, 0.120) for _ in range(total)]

    ready, why = _sc_prepare(ctx, s, sum(values) + 15)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    done, rejected, first_drift = 0, [], None
    for i, v in enumerate(values, 1):
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            rejected.append((i, "generation: " + str(err)[:50]))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="scale deploy {}".format(i), port=ctx.port)
        if ok:
            done += 1
        else:
            rejected.append((i, str(msg)[:60]))
        if i % checkpoint == 0 and first_drift is None:
            time.sleep(2)
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            if db.new_drift(start, now):
                first_drift = (i, db.describe_drift(db.new_drift(start, now)))

    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)
    d = db.delta(start, end)

    spent = -d["free"]
    expected = sum(values[:done]) if done < total else sum(values)
    value_gap = abs(spent - expected)

    problems = []
    if first_drift:
        problems.append("counter FIRST drifted at deploy {} of {}: {}".format(
            first_drift[0], total, first_drift[1]))
    elif drift:
        problems.append("counter drifted by the end of the run: "
                        + db.describe_drift(drift))
    if value_gap > max(0.01, expected * 0.001):
        problems.append("spent {:.4f} for {} deploy(s) worth {:.4f} - {:.4f} "
                        "unaccounted for across the run".format(
                            spent, done, expected, value_gap))

    note = "; ".join(problems)
    if rejected and not problems:
        note = "{} deploy(s) rejected under load (capacity, not correctness); " \
               "first: {}".format(len(rejected), rejected[0][1])

    return (not problems), "{}/{} deployed, spent {:.3f}, counter {}".format(
        done, total, spent, "ok" if not drift else "DRIFT"), note


# ---------------------------------------------------------------------------
# SC-X-02
# ---------------------------------------------------------------------------

def sc_x_02(ctx, ci):
    """
    SC-X-02 - One contract executed hundreds of times; chain stays sound.

    WHAT IT CHECKS
        Execute a single contract 200 times by default, checking every 25 that
        the chain length still equals the number of successful executes plus
        the deploy.

    WHY IT MATTERS
        Chain depth is the thing production accumulates that a test never does.
        A duplicate entry, a dropped entry, or a chain whose length stops
        tracking reality are all invisible at depth 3 and unmissable at depth
        200 - and a chain that drifts from the operation count means the
        history is no longer a reliable record of what happened.

        It is also the precondition for SC-X-03: a late subscriber can only be
        tested against a deep chain if a deep chain can be built at all.

    MANUAL STEPS
        Deploy once, then execute in a loop, and every 25 iterations:
          curl -s http://$SENDER:20000/rubix/v1/smart_contracts/<SC>/chain
        The entry count should equal successful executes + 1.

    PASS / FAIL
        PASS  chain length tracks the operation count throughout
        FAIL  reports the checkpoint where they first diverged, and by how much
              - ahead means duplicates, behind means dropped entries
    """
    s, _ = ctx.pair(0)
    total = _sc_scale_n(ctx, 200, floor=10)
    checkpoint = max(5, total // 8)

    ready, why = _sc_prepare(ctx, s, 20)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.100),
                                   data="scale deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    done, rejected, first_gap = 0, 0, None
    for i in range(1, total + 1):
        ok, _msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                        value=rand_value(0.005, 0.050),
                                        data="scale execute {}".format(i),
                                        port=ctx.port)
        if ok:
            done += 1
        else:
            rejected += 1
        if i % checkpoint == 0 and first_gap is None:
            time.sleep(2)
            okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
            if okc:
                expect = done + 1
                if len(chain) != expect:
                    first_gap = (i, len(chain), expect)

    time.sleep(SETTLE * 2)
    okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    final, expect = (len(chain) if okc else -1), done + 1

    problems = []
    if first_gap:
        i, got, want = first_gap
        problems.append("chain first diverged at execute {}: {} entries for {} "
                        "operations ({})".format(
                            i, got, want,
                            "duplicates" if got > want else "dropped entries"))
    elif final != expect:
        problems.append("chain ended at {} entries for {} operations ({})".format(
            final, expect, "duplicates" if final > expect else "dropped entries"))

    return (not problems), "{}/{} executed, chain {} (expected {})".format(
        done, total, final, expect), (
        "; ".join(problems) if problems else
        ("{} execute(s) rejected under load".format(rejected) if rejected else ""))


# ---------------------------------------------------------------------------
# SC-X-03
# ---------------------------------------------------------------------------

def sc_x_03(ctx, ci):
    """
    SC-X-03 - Subscribers joining continuously throughout a long execute run.

    WHAT IT CHECKS
        While a contract is executed repeatedly, every spare host subscribes at
        a different point in the run. At the end all of them must hold the same
        chain as the owner.

    WHY IT MATTERS
        The functional subscription cases join at depths 0 to 9 with four
        nodes. This joins across the whole fleet at depths spanning hundreds,
        while executes are still arriving - which is what a production network
        actually looks like when a node comes online.

        Two failure modes only appear here: back-fill that works at shallow
        depth but times out at deep, and a subscriber whose live events and
        back-fill overlap while the chain is still being written.

        The report groups any failures by JOIN DEPTH, so the answer to "does
        lateness cause it?" is in the result rather than a follow-up run.

    MANUAL STEPS
        Deploy, then execute in a loop. Every N executes subscribe another
        node, recording the depth at which it joined. At the end compare every
        node's chain against the owner's.

    PASS / FAIL
        PASS  every subscriber matches the owner, whatever depth it joined at
        FAIL  results are grouped by join depth so the pattern is visible - a
              clean cutoff means a back-fill limit, scattered failures mean a
              delivery race
        SKIP  fewer than 3 spare hosts
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 3:
        return SKIP, "not enough hosts", (
            "unit has {} spare host(s); scaled subscription needs at "
            "least 3".format(len(spare)))

    total = _sc_scale_n(ctx, 150, floor=12)
    ready, why = _sc_prepare(ctx, s, 20)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.100),
                                   data="scale subs deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    every = max(1, total // (len(spare) + 1))
    joined, done = [], 0
    for i in range(1, total + 1):
        ok, _m, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                      value=rand_value(0.005, 0.040),
                                      data="scale subs exec {}".format(i),
                                      port=ctx.port)
        if ok:
            done += 1
        if i % every == 0 and len(joined) < len(spare):
            node = spare[len(joined)]
            rc.subscribe_smart_contract(node["host"], sc_id, ctx.port)
            joined.append((node, done))

    time.sleep(SETTLE * 3)
    okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    owner_len = len(chain) if okc else -1

    behind = []
    for node, depth in joined:
        okn, c, _ = rc.get_sc_chain(node["host"], sc_id, ctx.port)
        got = len(c) if okn else -1
        if got != owner_len:
            behind.append((depth, node["host"], got))

    note = ""
    if behind:
        depths = [d for d, _h, _g in behind]
        ok_depths = [d for _n2, d in joined if d not in depths]
        shallow_ok = ok_depths and max(ok_depths) < min(depths)
        note = ("; ".join("joined at depth {} ({}): {} of {}".format(
            d, h, g, owner_len) for d, h, g in behind[:5]))
        note += (" - every failure is at a GREATER depth than every success, "
                 "which points at a back-fill limit rather than a delivery race"
                 if shallow_ok else
                 " - failures are scattered across depths, which points at a "
                 "delivery race rather than a depth limit")

    return (not behind), "{} execute(s), {} subscriber(s) joined at depths {}".format(
        done, len(joined), ",".join(str(d) for _n3, d in joined)), note


# ---------------------------------------------------------------------------
# SC-X-04
# ---------------------------------------------------------------------------

def sc_x_04(ctx, ci):
    """
    SC-X-04 - Every host deploying repeatedly and concurrently.

    WHAT IT CHECKS
        Every host in the lane runs its own deploy loop at the same time.
        Afterwards every wallet's counter, and every quorum's, must still match
        reality.

    WHY IT MATTERS
        The closest this suite gets to a production moment: many independent
        wallets deploying through a small number of shared quorums, sustained
        rather than a single burst. It puts pressure on the two things that
        only fail under exactly these conditions - quorum pledge capacity, and
        concurrent writes to the denomination counter from many DIDs at once.

        Rejections are expected and counted. A wallet or quorum whose books no
        longer balance is not.

    MANUAL STEPS
        Run the SC-C-01 deploy loop simultaneously on every host, then check
        each wallet's and each quorum's denom listing against its real free
        tokens.

    PASS / FAIL
        PASS  every wallet and quorum consistent afterwards
        RECORD  the rejection count and rate - a capacity result
        FAIL  any counter inconsistent -> concurrent load corrupts the books
        SKIP  fewer than 3 hosts
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    hosts = _sc_scale_hosts(ctx)
    if len(hosts) < 3:
        return SKIP, "need 3+ hosts", "unit has {}".format(len(hosts))

    per_host = _sc_scale_n(ctx, 15, floor=3)
    for e in hosts:
        ready, why = _sc_prepare(ctx, e, per_host * 0.2 + 8)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    try:
        w_before = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
        q_before = {q["host"]: db.snapshot(q["host"], q["did"])
                    for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def worker(e):
        ok_n, bad_n = 0, 0
        for i in range(per_host):
            sc_id, err = _sc_new_contract(ctx, e)
            if err:
                bad_n += 1
                continue
            ok, _m, _ = rc.sc_transaction(e["host"], e["did"], sc_id,
                                          value=rand_value(0.010, 0.080),
                                          data="fleet scale {}".format(i),
                                          port=ctx.port)
            ok_n, bad_n = (ok_n + 1, bad_n) if ok else (ok_n, bad_n + 1)
        return e["host"], ok_n, bad_n

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = list(pool.map(worker, hosts))
    time.sleep(SETTLE * 3)

    try:
        w_after = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
        q_after = {q["host"]: db.snapshot(q["host"], q["did"])
                   for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    for h in w_before:
        drift = db.new_drift(w_before[h], w_after[h])
        if drift:
            problems.append("wallet {} drifted: {}".format(h, db.describe_drift(drift)))
    for h in q_before:
        drift = db.new_drift(q_before[h], q_after[h])
        if drift:
            problems.append("QUORUM {} drifted: {}".format(h, db.describe_drift(drift)))

    ok_total = sum(o for _h, o, _b in results)
    bad_total = sum(b for _h, _o, b in results)
    attempted = ok_total + bad_total

    note = "; ".join(problems)
    if bad_total and not problems:
        note = ("{} of {} deploys rejected ({:.0f}%) at {} concurrent wallets - "
                "a capacity result, not a correctness one".format(
                    bad_total, attempted, 100.0 * bad_total / max(1, attempted),
                    len(hosts)))

    return (not problems), "{} wallet(s) x {} deploys: {} ok, {} rejected".format(
        len(hosts), per_host, ok_total, bad_total), note


# ---------------------------------------------------------------------------
# SC-X-05
# ---------------------------------------------------------------------------

def sc_x_05(ctx, ci):
    """
    SC-X-05 - Deploy and execute alternately for a sustained period.

    WHAT IT CHECKS
        Alternate deploy and execute continuously for several minutes,
        re-checking the counter periodically, and report the elapsed time and
        operation count at which anything first went wrong.

    WHY IT MATTERS
        Everything else here is bounded by a fixed operation count. This is
        bounded by TIME, which is what surfaces problems that depend on
        background work rather than on how many operations ran - unpledging
        falling behind, a queue that never drains, resources that accumulate.

        A fleet that passes every bounded test and degrades after four minutes
        of steady traffic is still broken, and only a duration-bounded case
        finds it.

    MANUAL STEPS
        Run a deploy/execute loop for the target duration, checking the counter
        every 30 seconds and noting when it first disagrees.

    PASS / FAIL
        PASS  counter consistent throughout the whole period
        FAIL  reports the elapsed seconds and operation count at first failure
              - the TIME matters as much as the count, since a time-dependent
              failure points at background work rather than the operations
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    duration = max(60, int(180 * _sc_scale_scale(ctx)))
    ready, why = _sc_prepare(ctx, s, 30)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    began = time.time()
    last_check = began
    ops, failures, first_bad = 0, 0, None
    live_sc = None

    while time.time() - began < duration:
        if live_sc is None or ops % 5 == 0:
            sc_id, err = _sc_new_contract(ctx, s)
            if err:
                failures += 1
                time.sleep(1)
                continue
            ok, _m, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                          value=rand_value(0.010, 0.060),
                                          data="soak deploy", port=ctx.port)
            if ok:
                live_sc = sc_id
            else:
                failures += 1
        else:
            ok, _m, _ = rc.sc_transaction(s["host"], s["did"], live_sc,
                                          value=rand_value(0.005, 0.030),
                                          data="soak execute", port=ctx.port)
            if not ok:
                failures += 1
        ops += 1

        if time.time() - last_check >= 30 and first_bad is None:
            last_check = time.time()
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            drift = db.new_drift(start, now)
            if drift:
                first_bad = (int(time.time() - began), ops, db.describe_drift(drift))
        time.sleep(0.5)

    elapsed = int(time.time() - began)
    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)

    problems = []
    if first_bad:
        problems.append("counter first drifted after {}s and {} operation(s): "
                        "{} - a time-dependent failure points at background "
                        "work such as unpledging, not at the operations "
                        "themselves".format(*first_bad))
    elif drift:
        problems.append("counter drifted by the end of {}s: {}".format(
            elapsed, db.describe_drift(drift)))

    return (not problems), "{}s sustained, {} operation(s), {} failure(s), counter {}".format(
        elapsed, ops, failures, "ok" if not drift else "DRIFT"), "; ".join(problems)


# -----------------------------------------------------------------------------
# Collateral - rejection, release, multi-contract, drift probes
# -----------------------------------------------------------------------------
# sc_cases_gaps.py - collateral paths the first full run did not reach.
#
# Written after reviewing the first green run against
# the diff, where four branches turned out to have no coverage at all.
#
#     1. POST-SPLIT ROLLBACK. The collateral split runs in a pre-pass that
#        commits its own genesis transaction on a separate connection, BEFORE the
#        outer transaction opens. Nothing tested what happens when the outer
#        transaction then fails. SC-C-06 is rejected before the lock, so it never
#        reaches this. A failure here strands Locked tokens and orphans a genesis.
#
#     2. NOVEL DENOMINATIONS. The denom write 977f6fba introduced is a bare
#        UPDATE:
#            UPDATE token_denom SET count = GREATEST(count-1,0)
#             WHERE did = $1 AND denom = $2
#        With no matching (did, denom) row that affects ZERO rows and returns no
#        error. A split creates children at denominations the wallet has never
#        held - so if nothing inserts a row for the new denomination, a later
#        burn of that child silently does nothing.
#
#     3. MULTI-SC IN ONE REQUEST. The pre-pass loops
#        `for _, scInfo := range req.GetAllSmartContracts()`, so two contracts in
#        ONE request each run the split, and the second iteration must see what
#        the first committed. SC-C-12 fires five separate requests, which is a
#        different path.
#
#     4. RECEIVER-ROLE STATE. Every denom case so far measures the initiator.
#        CRS-C-01 showed the receiver's node is where the deploy-bundle goes
#        wrong, and nothing was looking there.


# ---------------------------------------------------------------------------
# SC-C-27
# ---------------------------------------------------------------------------

def sc_c_27(ctx, ci):
    """
    SC-C-27 - Force a failure AFTER the collateral split has committed.

    WHAT IT CHECKS
        Deploy a value the wallet can cover but the quorum cannot pledge. The
        split runs and commits its genesis; consensus then fails. Afterwards
        the wallet must have nothing left Locked, and the free balance must be
        back where it started.

    WHY IT MATTERS
        This is the one ordering the fix deliberately introduced and nothing
        tested. The comment is explicit:

            "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
             opens its own connection and upserts the burnt parent row"

        A separate connection means a separate transaction, which means the
        outer transaction failing does NOT roll the split back. The parent is
        burnt, children exist, tokens are Locked - and the deploy never
        happened.

        SC-C-06 looks like it covers this and does not: an over-balance deploy
        is rejected before the lock, so the pre-pass never runs. The failure has
        to come AFTER a successful split, which means out-pledging the quorum
        rather than out-spending the wallet.

    MANUAL STEPS
        1. Read the quorum's free balance:
             psql -h $QUORUM -p 5433 -U rubix -d rubix -c \\
               "SELECT COALESCE(SUM(token_value),0) FROM tokens
                 WHERE did='<QDID>' AND token_type=1 AND token_status=0;"
        2. Fund the deployer ABOVE that figure.
        3. Deploy with a value between the quorum's balance and the wallet's -
           the split will succeed, the pledge will not.
        4. Afterwards, on the deployer:
             SELECT token_id, token_value FROM tokens
              WHERE did='<DID>' AND token_status=1;    -- 1 = Locked
           This must return NO rows.

    PASS / FAIL
        PASS  deploy rejected, nothing Locked, balance restored
        FAIL  tokens left Locked -> the split was not rolled back and that
              value is stranded permanently
        FAIL  balance did not return -> the burnt parent was not restored
        SKIP  could not fund above the quorum (needs a large mint)
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)
    q = ctx.quorum_for(s) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return SKIP, "no quorum", "cannot out-pledge a quorum that is not known"

    try:
        q_free = db.value_in_status(q["host"], q["did"], db.FREE)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # The value must exceed what the quorum can pledge but sit within the
    # wallet, so the split succeeds and only the pledge fails.
    value = round(q_free + 25.0, 3)
    need = value + 10
    ready, why = _sc_prepare(ctx, s, need)
    if not ready:
        return SKIP, "could not fund above the quorum", (
            "needs {:.0f} RBT to out-pledge quorum {} ({:.0f} free): {}".format(
                need, q["host"], q_free, why))

    before = _sc_bal(ctx, s)
    try:
        locked_before = db.count_in_status(s["host"], s["did"], db.LOCKED)
        snap_before = db.record("SC-C-27", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="post-split rollback probe", port=ctx.port)
    time.sleep(SETTLE * 3)

    after = _sc_bal(ctx, s)
    try:
        locked_after = db.count_in_status(s["host"], s["did"], db.LOCKED)
        locked_rows = db.token_rows(s["host"], s["did"], db.LOCKED)
        snap_after = db.record("SC-C-27", "after", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if ok:
        return SKIP, "quorum pledged after all", (
            "the {:.3f} deploy succeeded, so the pledge did not fail and the "
            "post-split path was never reached. Quorum had {:.0f} free - it may "
            "have been topped up by another lane".format(value, q_free))

    problems = []
    stranded = locked_after - locked_before
    if stranded > 0:
        problems.append("{} token(s) left LOCKED after a failed deploy ({}) - "
                        "the collateral split was not rolled back and that value "
                        "is stranded".format(
                            stranded,
                            ", ".join("{}={:.3f}".format(t[:12], v)
                                      for t, v, _st, _p in locked_rows[:3])))
    if before and after and not rc.close_enough(before["balance"], after["balance"],
                                                tol=TOL * 4):
        problems.append("free balance did not return: {:.4f} -> {:.4f} - the "
                        "burnt parent was not restored".format(
                            before["balance"], after["balance"]))
    drift = db.new_drift(snap_before, snap_after)
    if drift:
        problems.append("counter drifted after a failed deploy: "
                        + db.describe_drift(drift))

    return (not problems), "rejected at {:.3f} (quorum free {:.0f}), locked {}->{} | {}".format(
        value, q_free, locked_before, locked_after,
        db.format_evidence(snap_before, snap_after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-29
# ---------------------------------------------------------------------------

def sc_c_29(ctx, ci):
    """
    SC-C-29 - Three contracts, each with a value, in ONE request.

    WHAT IT CHECKS
        A single /tx carrying three smart contracts with different values. All
        three deploy, the total cost equals the sum of the three values, and
        the counter stays consistent.

    WHY IT MATTERS
        The collateral pre-pass loops over every contract in the request:

            for _, scInfo := range req.GetAllSmartContracts() { ... }

        Each iteration locks tokens, splits, and persists its own genesis. The
        SECOND iteration must see what the first committed - a different
        situation from concurrent separate requests, which touch different
        transactions entirely.

        SC-C-12 fires five requests at once and covers contention. This covers
        sequence within one request, where the failure mode is the opposite:
        the second split reading a wallet state the first has already changed,
        inside a call that has not finished.

    MANUAL STEPS
        Generate three contracts, then post ONE transaction naming all three:
          "smartContract":[{"smartContractId":"<A>","value":0.31,"data":"x"},
                           {"smartContractId":"<B>","value":0.47,"data":"x"},
                           {"smartContractId":"<C>","value":0.22,"data":"x"}]
        Check the balance fell by 1.00 exactly, and all three are listed.

    PASS / FAIL
        PASS  all three deploy, total cost exact, counter consistent
        FAIL  cost matches only the FIRST value -> later iterations did not
              take collateral
        FAIL  cost exceeds the sum -> an iteration double-locked
        FAIL  counter drifts -> the per-iteration decrements do not compose
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    values = [rand_value(0.100, 0.400) for _ in range(3)]
    ready, why = _sc_prepare(ctx, s, sum(values) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    entries = []
    for v in values:
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        entries.append({"smartContractId": sc_id, "value": v,
                        "data": "multi-SC single request"})

    try:
        before = db.record("SC-C-29", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    bal_before = _sc_bal(ctx, s)

    body = {
        "initiator": s["did"], "owner": "",
        "tokens": {"rbt": 0, "ft": [], "nft": [],
                   "smartContract": entries, "transferNftOwnership": False},
        "memo": "SC-C-29 three contracts in one request",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "multi-contract request rejected", (
            "three contracts in one request was refused: {} - if this is not "
            "supported it should be documented, since the builder loops over "
            "them".format(msg))
    time.sleep(SETTLE * 2)

    bal_after = _sc_bal(ctx, s)
    try:
        after = db.record("SC-C-29", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        listed = rc.list_smart_contracts(s["host"], ctx.port)[1]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    import json as _json
    blob = _json.dumps(listed)
    present = sum(1 for e in entries if e["smartContractId"] in blob)
    spent = (bal_before["balance"] - bal_after["balance"]) if (bal_before and bal_after) else None
    expected = sum(values)

    problems = []
    if present < len(entries):
        problems.append("{} of {} contracts listed after the request".format(
            present, len(entries)))
    if spent is None or not rc.close_enough(spent, expected, tol=cost_tolerance(expected)):
        problems.append("spent {:.4f}, expected {:.4f}".format(
            spent if spent is not None else -1, expected))
        if spent is not None and rc.close_enough(spent, values[0], tol=TOL * 4):
            problems.append("cost equals only the FIRST value - later iterations "
                            "of the pre-pass took no collateral")
    drift = db.new_drift(before, after)
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift) +
                        " - the per-iteration decrements do not compose")

    return (not problems), "3 contracts in one request ({}), spent {:.4f} of {:.4f} | {}".format(
        "+".join(str(v) for v in values), spent if spent is not None else -1,
        expected, db.format_evidence(before, after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-DB-03  (DB-SEED - writes to the database deliberately)
# ---------------------------------------------------------------------------

def sc_db_03(ctx, ci):
    """
    SC-DB-03 - Burn a denomination whose token_denom row has been deleted.

    WHAT IT CHECKS
        Delete the token_denom row for a denomination the wallet holds, then
        burn a token at that denomination via an FT mint. The product should
        either recreate the row or report an error - it must not silently
        proceed leaving the counter permanently wrong.

    WHY IT MATTERS
        The decrement 977f6fba introduced is a bare UPDATE:

            UPDATE token_denom SET count = GREATEST(count-1,0)
             WHERE did = $1 AND denom = $2

        With no matching row that affects ZERO rows and returns no error. The
        burn proceeds, the token leaves Free, and the counter never learns. It
        is the one shape of this bug that produces no drift at the moment it
        happens - the counter simply has no opinion about that denomination -
        and then under-counts forever.

        A missing row is reachable in normal operation: recovery re-derives
        token_denom (core/wallet/recovery.go:657) and a denomination with no
        Free tokens at that instant gets no row. This case creates that state
        directly rather than waiting to encounter it.

    DELIBERATE CORRUPTION - this case WRITES to the database.
        It records the row first and restores it in a finally block. Per the
        catalogue's DB-SEED rule these run LAST in a cycle, and a failure
        mid-case can still leave the node dirty - re-run GEN-IN-08 afterwards
        to confirm the wallet is clean.

    MANUAL STEPS
        1. Pick a denomination the wallet holds:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
        2. Record it, then delete it:
             DELETE FROM token_denom WHERE did='<DID>' AND denom=<D>;
        3. Mint an FT backed by a token of that denomination.
        4. Re-read token_denom. Was the row recreated? Does it match reality?
        5. Restore the row you deleted.

    PASS / FAIL
        PASS  the row is recreated and matches the real free tokens, OR the
              mint is rejected with a clear error
        FAIL  the mint succeeds and the row is still absent -> the decrement
              silently did nothing and the counter is permanently wrong
        SKIP  psycopg2 missing, or no suitable denomination held
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _sc_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        counter = db.denom_counter(s["host"], s["did"])
        real = db.real_free_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # A whole denomination the wallet actually holds, so an FT mint will burn it.
    # BUILD the precondition rather than skipping on it. The last run skipped
    # here because the wallet had been drained of whole tokens by an earlier
    # case - a state one round of funding fixes. Skipping on a fixable
    # precondition is how a branch stays unproven run after run while the
    # report shows nothing wrong.
    def _pick():
        for d in sorted(counter, reverse=True):
            if d >= 1.0 and real.get(d, 0) > 0:
                return d
        for d in sorted(counter, reverse=True):
            for rd, rc_ in real.items():
                if abs(rd - d) < 0.0015 and rc_ > 0 and rd >= 1.0:
                    return d
        return None

    target = _pick()
    if target is None:
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            counter = db.denom_counter(s["host"], s["did"])
            real = db.real_free_denoms(s["host"], s["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        target = _pick()
    if target is None:
        return SKIP, "no suitable denomination", (
            "needs a whole denomination present in both token_denom and the "
            "tokens table, and funding 5 RBT did not produce one")

    original = counter[target]
    restored = False
    try:
        db.writable_query(
            s["host"],
            "DELETE FROM token_denom WHERE did = %s AND denom = %s",
            (s["did"], target), i_understand_this_writes=True)

        ok, msg, _ = rc.mint_ft(s["host"], s["did"], "seed" + str(int(time.time()))[-6:],
                                5, 1, ctx.port)
        time.sleep(SETTLE * 2)

        after = db.denom_counter(s["host"], s["did"])
        real_after = db.real_free_denoms(s["host"], s["did"])
        row_back = any(abs(d - target) < 0.0015 for d in after)

        problems = []
        if ok and not row_back:
            problems.append(
                "the mint succeeded but token_denom still has no row for {:.3f}, "
                "while {} token(s) remain Free there - the bare UPDATE matched "
                "no row, returned no error, and the counter is now permanently "
                "wrong for this denomination".format(
                    target, real_after.get(target, 0)))
        elif ok and row_back:
            counted = next(c for d, c in after.items() if abs(d - target) < 0.0015)
            actual = real_after.get(target, 0)
            if counted != actual:
                problems.append("row recreated at {} but {} token(s) are Free "
                                "at {:.3f}".format(counted, actual, target))

        return (not problems), "deleted denom {:.3f} (was {}), mint {}, row {}".format(
            target, original, "ok" if ok else "rejected",
            "recreated" if row_back else "STILL MISSING"), "; ".join(problems)
    finally:
        # Restore whatever the row was, whether or not the case passed.
        try:
            db.writable_query(
                s["host"],
                "INSERT INTO token_denom (did, denom, count, created_at, updated_at) "
                "VALUES (%s, %s, %s, NOW(), NOW()) "
                "ON CONFLICT (did, denom) DO UPDATE SET count = EXCLUDED.count",
                (s["did"], target, original), i_understand_this_writes=True)
            restored = True
        except Exception:
            pass
        if not restored:
            sys.stderr.write(
                "[SC-DB-03] WARNING: could not restore token_denom row "
                "(did={}, denom={}, count={}) on {} - fix by hand before "
                "trusting later results\n".format(
                    s["did"][:16], target, original, s["host"]))


# ---------------------------------------------------------------------------
# SC-C-31
# ---------------------------------------------------------------------------

def sc_c_31(ctx, ci):
    """
    SC-C-31 - Is the collateral committed by a REJECTED deploy ever released?

    RUNS IMMEDIATELY AFTER SC-C-27, ON THE SAME HOST, BY DESIGN.
    SC-C-27 creates the condition; this case decides whether it is permanent.
    Run it alone and it measures a quiet wallet and passes, which is correct
    but tells you nothing - keep them in the same lane and order.

    WHAT IT CHECKS
        Read the host's Committed RBT, wait, read it again. Committed
        (token_status 5) is a TERMINAL state - there is no release path back to
        Free. If the value SC-C-27 stranded is still there after the network has
        had every chance to settle, it is lost, not in flight.

    WHY IT MATTERS
        This is the difference between a bug report and a blocker. SC-C-27
        showed ~1136 RBT moving to Committed for a deploy that was REJECTED:

            free 1150.438 -> 14.496   committed 420.342 -> 1556.284   locked 0->0

        "locked 0->0" already rules out the lock-release leak seen on .107 -
        those tokens are not waiting on anything. But a single reading taken
        seconds after the rejection cannot distinguish "lost" from "not yet
        cleaned up". A reviewer will ask exactly that, and the honest answer
        today is that nobody has waited and looked again.

        If the value does come back, this is a timing artefact and SC-C-27
        should be re-scoped. If it does not, the collateral pre-pass commits
        value before consensus and never unwinds it on failure - which is
        unrecoverable loss on a path the user cannot avoid.

    MANUAL STEPS
        1. Right after a rejected deploy, on the deployer host:
             SELECT ROUND(SUM(token_value)::numeric,3) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=5;
        2. Wait two minutes. Run it again.
        3. Also count contracts - a rejected deploy must not have created one:
             SELECT COUNT(*) FROM smart_contracts WHERE deployer_did='<DID>';

    PASS / FAIL
        PASS  committed FELL over the window - the value is being released and
              SC-C-27 is a timing artefact, not a loss
        FAIL  committed is unchanged - it is terminal, and the pre-pass
              loses it on every rejected deploy
        SKIP  the host holds no committed RBT (SC-C-27 did not run first)
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    # Long enough that "still settling" is not a credible explanation. The
    # measured fleet timing is 1-2s for a receiver to credit and ~15s for a
    # node restart, so two minutes is an order of magnitude past anything
    # normal.
    window = 120

    try:
        first = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if first["committed"] < 1.0:
        return SKIP, "no committed RBT to observe", (
            "this host holds {:.3f} committed - SC-C-27 either did not run "
            "first or did not strand anything, so there is nothing to watch "
            "for release".format(first["committed"]))

    time.sleep(window)

    try:
        second = db.record("SC-C-31", "after-wait", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
        # token_type 4 is a smart contract - the same count GEN-IN-22 uses.
        rows = db.query(s["host"],
                        "SELECT COUNT(*) FROM tokens WHERE token_type = %s",
                        (db.TYPE_SC,))
        contracts = int(rows[0][0]) if rows else -1
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    released = first["committed"] - second["committed"]
    note = ""
    if released <= TOL:
        note = ("{:.3f} RBT is still Committed {}s after the rejection and has "
                "not moved. Committed is terminal - there is no path back to "
                "Free - so this value is permanently lost, for a deploy that "
                "was REJECTED. The collateral pre-pass commits before "
                "consensus and does not unwind on failure".format(
                    second["committed"], window))
        if contracts >= 0:
            note += "; the host holds {} contract(s) to account for it".format(contracts)

    return (released > TOL), "committed {:.3f} -> {:.3f} over {}s (released {:.3f})".format(
        first["committed"], second["committed"], window, released), note


# ---------------------------------------------------------------------------
# SC-C-32
# ---------------------------------------------------------------------------

def sc_c_32(ctx, ci):
    """
    SC-C-32 - Is the multi-contract rejection float accumulation, or is
    multi-contract simply unsupported?

    WHAT IT CHECKS
        Sends the SAME three-contract request twice with different values:

          EXACT   0.25, 0.5, 0.125   -> 0.875   every value exact in binary
          INEXACT 0.345, 0.359, 0.317 -> 1.021  none of them exact in binary

        The outcome pair is the answer.

    WHY IT MATTERS
        SC-C-29 failed with:

            "requestPledgeTokenHandler : transaction amount exceeds 3 decimal
             places"

        for 0.345+0.359+0.317. Every one of those is a legal 3dp value, and so
        is their sum. The only way a 3dp check rejects them is if the number it
        actually tested was not 1.021 - and summing those three in float64
        gives 1.0209999999999999, which has sixteen.

        That is a hypothesis, and SC-C-29 alone cannot distinguish it from "the
        API does not support multiple contracts in one request". Those are
        completely different findings: one is a precision bug worth fixing, the
        other is a documentation gap. Choosing between them by reading the
        error string is guessing.

        Binary-exact values settle it. 0.25, 0.5 and 0.125 are all sums of
        powers of two, so they accumulate with NO error at all - 0.875 exactly.
        If that triple is accepted and the inexact one is refused, the
        difference cannot be anything but the accumulation.

    MANUAL STEPS
        Post one /tx naming three contracts valued 0.25 / 0.5 / 0.125.
        Then post another naming three valued 0.345 / 0.359 / 0.317.
        Compare the two responses.

    PASS / FAIL
        PASS  both accepted - not reproducible on this build
        FAIL  exact ACCEPTED, inexact REFUSED -> float accumulation CONFIRMED.
              totalAmount += scInfo.Value needs a decimal-safe sum
        FAIL  both refused -> multi-contract is not supported at all; a
              different finding, and the builder's loop over contracts is
              misleading
    """
    s, _ = ctx.pair(0)

    exact = [0.25, 0.5, 0.125]        # 0.875 - representable exactly in float64
    inexact = [0.345, 0.359, 0.317]   # 1.021 - accumulates to 1.0209999999999999

    ready, why = _sc_prepare(ctx, s, sum(exact) + sum(inexact) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    def send(values, label):
        entries = []
        for v in values:
            sc_id, err = _sc_new_contract(ctx, s)
            if err:
                return None, "generation failed: " + err
            entries.append({"smartContractId": sc_id, "value": v,
                            "data": "SC-C-32 " + label})
        body = {
            "initiator": s["did"], "owner": "",
            "tokens": {"rbt": 0, "ft": [], "nft": [],
                       "smartContract": entries, "transferNftOwnership": False},
            "memo": "SC-C-32 " + label,
        }
        ok, msg, _ = rc._tx(s["host"], body, ctx.port)
        time.sleep(SETTLE)
        return ok, str(msg)

    ok_exact, msg_exact = send(exact, "binary-exact")
    if ok_exact is None:
        return SKIP, "could not build the exact request", msg_exact
    ok_inexact, msg_inexact = send(inexact, "binary-inexact")
    if ok_inexact is None:
        return SKIP, "could not build the inexact request", msg_inexact

    decimals = "3 decimal places" in msg_inexact

    if ok_exact and not ok_inexact:
        return False, "exact 0.875 ACCEPTED, inexact 1.021 REFUSED", (
            "FLOAT ACCUMULATION CONFIRMED. 0.25+0.5+0.125 are exact in binary "
            "and were accepted; 0.345+0.359+0.317 are not and were refused"
            + (" with the 3-decimal-places error" if decimals else "") +
            ". Both sums are legal 3dp values, so the only difference is the "
            "representation - totalAmount += scInfo.Value accumulates error "
            "and the precision guard then rejects a value the caller never "
            "sent. Refusal was: " + msg_inexact)
    if not ok_exact and not ok_inexact:
        return False, "both multi-contract requests refused", (
            "NOT a precision bug - even binary-exact values are refused, so "
            "multiple contracts in one request are not supported at all. That "
            "is a documentation gap rather than an arithmetic one, and the "
            "pre-pass looping over GetAllSmartContracts is misleading. "
            "Exact refusal: " + msg_exact)
    if ok_exact and ok_inexact:
        return True, "both accepted (0.875 and 1.021)", (
            "not reproducible on this build - SC-C-29's failure did not recur")
    return False, "exact REFUSED but inexact accepted", (
        "the opposite of the hypothesis, and not explainable by accumulation. "
        "Exact refusal: " + msg_exact)


# ---------------------------------------------------------------------------
# SC-C-33
# ---------------------------------------------------------------------------

def sc_c_33(ctx, ci):
    """
    SC-C-33 - What does a SUCCESSFUL deploy do to Committed?

    THE CONTROL FOR SC-C-27 / SC-C-31. Cheap, small value, no quorum
    out-pledging. It answers the one question those two cannot: is moving
    collateral into Committed the FAILURE behaviour, or the NORMAL behaviour
    that the failure path wrongly reuses?

    WHAT IT CHECKS
        Deploy a small contract that SUCCEEDS. Then:
          a) did Committed rise by exactly the contract value?
          b) after a wait, is it still there?

    WHY IT MATTERS
        SC-C-27 showed ~1136 RBT going to Committed for a REJECTED deploy, and
        SC-C-31 decides whether it ever comes back. Both measure only the
        failure path, and a reviewer reading them alone can reasonably answer:
        "collateral is supposed to be committed - that is what collateral IS".

        Without this control the finding has to be stated as "value moved to
        Committed", which sounds like correct behaviour. With it the finding
        becomes precise:

            a successful deploy commits the collateral and gets a contract
            for it; a rejected deploy commits the SAME value and gets
            nothing

        That is a one-line statement a developer can act on, and it points
        straight at the pre-pass running before the consensus outcome is known
        rather than after it.

        The second reading matters just as much. If Committed is released on a
        successful deploy, then a release path EXISTS and SC-C-31's failure to
        observe one is a rejection-only defect. If it is terminal on success
        too, then Committed is terminal by design and the loss on rejection is
        unrecoverable by construction. Those are different severities and
        nothing currently distinguishes them.

    MANUAL STEPS
        1. On the deployer, before:
             SELECT ROUND(SUM(token_value)::numeric,3) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=5;
        2. Deploy one contract at a small value that will succeed.
        3. Read it again immediately, and once more after two minutes.
        4. The rise should equal the contract value, exactly.

    PASS / FAIL
        PASS  deploy succeeded and Committed rose by exactly the value - the
              normal path is well-defined, and SC-C-27 can be reported as the
              failure path copying it
        FAIL  Committed rose by something other than the value -> the
              accounting is wrong even on the success path, which is a larger
              finding than SC-C-27
        FAIL  the deploy was rejected - no control was established
        SKIP  no database, or the wallet could not be funded

        Either way the note records whether the committed value was released,
        because that is what sets SC-C-27's severity.
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    value = rand_value(0.100, 0.500)
    ready, why = _sc_prepare(ctx, s, value + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before = db.record("SC-C-33", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="SC-C-33 success control", port=ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-33", "after deploy", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not ok:
        return False, "the control deploy at {:.3f} was REJECTED".format(value), (
            "this case exists to establish what a SUCCESSFUL deploy does, and "
            "no success was obtained - so SC-C-27 has no control to be read "
            "against. Check the quorum's free balance before trusting the "
            "rollback lane at all. Rejection was: " + str(msg))

    committed = after["committed"] - before["committed"]

    # The same window SC-C-31 uses, so the two readings are comparable.
    window = 120
    time.sleep(window)
    try:
        settled = db.record("SC-C-33", "after wait", s["host"], s["did"],
                            db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    released = after["committed"] - settled["committed"]

    if released > TOL:
        release = ("{:.3f} of it was released within {}s, so a release path "
                   "DOES exist - which makes SC-C-31 observing none on the "
                   "rejected path a rejection-only defect".format(released, window))
    else:
        release = ("none of it was released within {}s, so Committed is "
                   "terminal on the success path too - the value SC-C-27 "
                   "stranded is unrecoverable by construction, not merely "
                   "uncollected".format(window))

    problems = []
    if not rc.close_enough(committed, value, tol=TOL * 4):
        problems.append(
            "a SUCCESSFUL deploy of {:.3f} committed {:.4f} - the two must be "
            "equal, and they are not. The collateral accounting is wrong on "
            "the normal path, which is a larger finding than the rejected "
            "path".format(value, committed))

    return (not problems), "deploy {:.3f} ok, committed +{:.4f}, {:.4f} still committed after {}s".format(
        value, committed, settled["committed"] - before["committed"], window), (
        "; ".join(problems + [release]))


# ---------------------------------------------------------------------------
# SC-C-34
# ---------------------------------------------------------------------------

def sc_c_34(ctx, ci):
    """
    SC-C-34 - At what N does a multi-contract request start failing, and is the
    guard reading the SUM or the individual values?

    THE LADDER BEHIND SC-C-32. SC-C-32 compares one exact triple against one
    inexact triple and can only say "accumulation or not". This walks the
    count, so the report can name the boundary and rule out the two
    alternative explanations that a single comparison leaves open.

    WHAT IT CHECKS
        Three requests, in this order:

          N=1   0.345                    one inexact value, alone
          N=2   0.1 + 0.2   -> 0.3       the canonical float64 failure:
                                         0.1+0.2 == 0.30000000000000004
          N=2   0.25 + 0.5  -> 0.75      both exact in binary, same count

        Every value and every sum is a legal 3dp amount, so a correct
        implementation accepts all three.

    WHY IT MATTERS
        SC-C-29 failed with "transaction amount exceeds 3 decimal places" for
        three inexact values. Three explanations survive that one observation:

          1. the sum accumulates float error and the guard tests the sum
          2. the guard tests each value and 0.345 alone is already rejected
          3. multiple contracts in one request are simply not supported

        N=1 kills (2): if 0.345 alone is accepted, the individual values are
        not the problem. Holding N at 2 while swapping exact for inexact kills
        (3): if the count were the problem, both N=2 requests would fail
        together. What is left is (1), and it is then confirmed on the single
        most recognisable example in floating point - 0.1 + 0.2 - which is
        worth more in a PR comment than any arbitrary triple.

        It also pins the boundary. If N=2 already fails, the fix is not an edge
        case at three contracts; it is every multi-contract request that does
        not happen to use binary-exact values, which is nearly all of them.

    MANUAL STEPS
        Post three separate /tx calls naming 1, then 2, then 2 contracts with
        the values above, and record each response verbatim. In Go:
            var t float64; for _, v := range []float64{0.1, 0.2} { t += v }
            fmt.Println(t)   // 0.30000000000000004

    PASS / FAIL
        PASS  all three accepted - not reproducible on this build
        FAIL  N=1 ok, 0.1+0.2 REFUSED, 0.25+0.5 accepted -> accumulation
              confirmed at N=2, on the sum, not the values
        FAIL  N=1 ok, both N=2 refused -> the count is the limit, not the
              arithmetic; SC-C-32's reading should be re-stated
        FAIL  N=1 refused -> the guard rejects a single legal 3dp value and
              this is not about multi-contract at all
        SKIP  setup incomplete
    """
    s, _ = ctx.pair(0)

    single = [0.345]
    inexact = [0.1, 0.2]     # 0.30000000000000004 accumulated in float64
    exact = [0.25, 0.5]      # 0.75, exact - same count, no accumulation

    ready, why = _sc_prepare(ctx, s, sum(single) + sum(inexact) + sum(exact) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    def send(values, label):
        entries = []
        for v in values:
            sc_id, err = _sc_new_contract(ctx, s)
            if err:
                return None, "generation failed: " + err
            entries.append({"smartContractId": sc_id, "value": v,
                            "data": "SC-C-34 " + label})
        body = {
            "initiator": s["did"], "owner": "",
            "tokens": {"rbt": 0, "ft": [], "nft": [],
                       "smartContract": entries, "transferNftOwnership": False},
            "memo": "SC-C-34 " + label,
        }
        ok, msg, _ = rc._tx(s["host"], body, ctx.port)
        time.sleep(SETTLE)
        return ok, str(msg)

    ok_one, msg_one = send(single, "N=1 0.345")
    if ok_one is None:
        return SKIP, "could not build the N=1 request", msg_one
    ok_in, msg_in = send(inexact, "N=2 0.1+0.2")
    if ok_in is None:
        return SKIP, "could not build the inexact N=2 request", msg_in
    ok_ex, msg_ex = send(exact, "N=2 0.25+0.5")
    if ok_ex is None:
        return SKIP, "could not build the exact N=2 request", msg_ex

    summary = "N=1 {}, N=2 inexact {}, N=2 exact {}".format(
        "ok" if ok_one else "REFUSED",
        "ok" if ok_in else "REFUSED",
        "ok" if ok_ex else "REFUSED")

    if not ok_one:
        return False, summary, (
            "a SINGLE contract at 0.345 - a legal 3dp value - was refused, so "
            "this is not a multi-contract or accumulation finding at all. "
            "SC-C-29 and SC-C-32 should both be re-read against this. "
            "Refusal: " + msg_one)

    if ok_one and not ok_in and ok_ex:
        return False, summary, (
            "FLOAT ACCUMULATION CONFIRMED ON THE SUM, AT N=2. 0.345 alone is "
            "accepted, so individual values are not the problem; 0.25+0.5 at "
            "the same count is accepted, so the count is not the problem. "
            "Only 0.1+0.2 fails, and 0.1+0.2 is exactly 0.30000000000000004 "
            "in float64. The guard is testing an accumulated sum the caller "
            "never sent. This is not an edge case at three contracts - it is "
            "every multi-contract request whose values are not binary-exact. "
            "Refusal: " + msg_in)

    if ok_one and not ok_in and not ok_ex:
        return False, summary, (
            "the COUNT is the limit, not the arithmetic: 0.345 alone is "
            "accepted and both two-contract requests are refused, including "
            "the binary-exact one that cannot accumulate any error. "
            "Multi-contract is unsupported from N=2 upward and SC-C-32's "
            "reading should be re-stated. Inexact refusal: " + msg_in +
            " | exact refusal: " + msg_ex)

    if ok_one and ok_in and ok_ex:
        return True, summary, (
            "all three accepted - the multi-contract refusal seen in SC-C-29 "
            "did not reproduce on this build")

    return False, summary, (
        "an outcome no hypothesis predicts - the exact pair was refused while "
        "the inexact pair was accepted. Record both responses verbatim before "
        "drawing any conclusion. Exact refusal: " + msg_ex)


# --- SMART CONTRACT ---

CASES = {
    "SC-S-01": sc_s_01,
    "SC-S-02": sc_s_02,
    "SC-S-03": sc_s_03,
    "SC-S-04": sc_s_04,
    "SC-S-05": sc_s_05,

    # Collateral review additions - value ladder, wallet shapes, deep split,
    # balance boundary, sustained load, and the row-level DB checks.
    "SC-C-13": sc_c_13,
    "SC-C-19": sc_c_19,

    # Quorum-side accounting - the other half of every deploy.
    "SC-Q-11": sc_q_11,
    "SC-Q-12": sc_q_12,

    # Subscription at fleet scale, and executing from parts wallets.
    "SC-S-06": sc_s_06,
    "SC-S-07": sc_s_07,
    "SC-S-08": sc_s_08,
    "SC-S-09": sc_s_09,
    "SC-S-10": sc_s_10,

    # Concurrency - the deadlock the collateral-split ordering avoids.
    "SC-C-12": sc_c_12,
    "SC-C-26": sc_c_26,

    # Production-level volume. Run as a stress selection AFTER
    # the functional suite passes - at this scale a single rejection
    # cannot be told from ordinary contention unless the basics are
    # already known good. Each reports WHERE the invariant first
    # broke, not merely that it did.
    "SC-X-01": sc_x_01,
    "SC-X-02": sc_x_02,
    "SC-X-03": sc_x_03,
    "SC-X-04": sc_x_04,
    "SC-X-05": sc_x_05,

    # Branches the first full run did not reach, found by scoping coverage
    # to the three changed hunks rather than to the suite as a whole.
    "SC-C-27": sc_c_27,
    "SC-C-29": sc_c_29,
    "SC-DB-03": sc_db_03,

    # Confirmation cases - each one decides whether a
    # finding is real and whose code it belongs to.
    "SC-C-31": sc_c_31,
    "SC-C-32": sc_c_32,
    # Controls for those two - each removes an alternative explanation a
    # reviewer would otherwise raise against the finding it backs.
    "SC-C-33": sc_c_33,
    "SC-C-34": sc_c_34,
}

ORDER = [
    "SC-S-01", "SC-S-02", "SC-S-03", "SC-S-04", "SC-S-05",
    "SC-C-13", "SC-C-19",
    "SC-Q-11", "SC-Q-12",
    "SC-S-06", "SC-S-07", "SC-S-08", "SC-S-09", "SC-S-10",
    "SC-C-12", "SC-C-26",
    "SC-X-01", "SC-X-02", "SC-X-03", "SC-X-04", "SC-X-05",
    "SC-C-29", "SC-C-32", "SC-C-34", # SC-C-33 is the SUCCESS control and must run BEFORE SC-C-27 empties the
    # wallet - afterwards there is nothing left to deploy successfully with.
    "SC-C-33", "SC-C-27", "SC-C-31",
    "SC-DB-03",
]

TIMING_CASES = set()

# What each unit of cases needs - see NEEDS in full-test/test_runner.py.
# receivers 0 = the sender receives too. last = runs after everything else.
NEEDS = {
    # Subscription timing. One deployer plus subscribers that join at
    # increasing chain depths - SC-S-04 alone needs four spare receivers, and
    # they must all be watching the SAME contract, so this cannot be split.
    "sc-subscription": {
        "cases": ["SC-S-01", "SC-S-02", "SC-S-03", "SC-S-04", "SC-S-05"],
        "receivers": 5, "fund": 6,
    },

    # --- Collateral review additions ------------------------------------------
    # The ladder spends the sum of 13 values (~17.5), so it is funded well
    # above the others.
    "sc-value-ladder": {
        "cases": ["SC-C-13"],
        "receivers": 0, "fund": 30,
    },
    # Twenty deploys back to back - the slowest unit.
    "sc-sustained": {
        "cases": ["SC-C-19"],
        "receivers": 0, "fund": 15,
    },

    "sc-quorum-spread": {
        "cases": ["SC-Q-11", "SC-Q-12"],
        "receivers": 2, "fund": 12,
    },

    # The wide subscription matrix: one deployer plus as many
    # subscribers as the fleet can spare, joining at staggered depths.
    "sc-subs-matrix": {
        "cases": ["SC-S-06", "SC-S-07"],
        "receivers": 6, "fund": 8,
    },
    "sc-subs-timing": {
        "cases": ["SC-S-08", "SC-S-09", "SC-S-10"],
        "receivers": 3, "fund": 12,
    },


    # Concurrency. SC-C-12 hammers ONE wallet (the deadlock case);
    # SC-C-26 spreads across many to load the shared quorums.
    "sc-race-one-wallet": {
        "cases": ["SC-C-12"],
        "receivers": 0, "fund": 15,
    },
    "sc-race-fleet": {
        "cases": ["SC-C-26"],
        "receivers": 4, "fund": 10,
    },

    # --- scale (stress) ---------------------------------------------
    # Funded well above the rest: SC-X-01 alone runs 200 deploys, and a
    # unit that runs dry mid-run reports funding as a defect.
    "sc-scale-deploys": {
        "cases": ["SC-X-01"],
        "receivers": 0, "fund": 60,
    },
    "sc-scale-chain": {
        "cases": ["SC-X-02"],
        "receivers": 0, "fund": 40,
    },
    # Subscribers join across the whole run: the more of them, the wider the
    # range of join depths tested.
    "sc-scale-subs": {
        "cases": ["SC-X-03"],
        "receivers": 7, "fund": 30,
    },
    "sc-scale-fleet": {
        "cases": ["SC-X-04"],
        "receivers": 5, "fund": 25,
    },
    # Duration-bounded rather than count-bounded - it finds what depends on
    # background work rather than on operation count.
    "sc-scale-soak": {
        "cases": ["SC-X-05"],
        "receivers": 0, "fund": 50,
    },

    # --- hunk-coverage gaps -------------------------------------------------
    "sc-guard-branches": {
        "cases": ["SC-C-29", "SC-C-32", "SC-C-34"],
        "receivers": 0, "fund": 20,
    },
    # SC-C-27 must out-pledge its quorum, so it needs a wallet larger than
    # the quorum's free balance.
    "sc-rollback": {
        # Order inside this unit is the whole experiment, on ONE wallet:
        #   SC-C-33  a deploy that SUCCEEDS - what Committed does normally
        #   SC-C-27  a deploy that is REJECTED - what Committed does then
        #   SC-C-31  two minutes later - is any of it released
        # 33 must come first; after 27 the wallet is empty and no successful
        # deploy is possible, so the control could never be taken.
        "cases": ["SC-C-33", "SC-C-27", "SC-C-31"],
        "receivers": 0, "fund": 0,
        # SC-C-27 funds itself to just above its quorum's balance, so the
        # poorest quorum makes it cheapest.
        "quorum_pick": "poorest",
    },
    # DB-SEED: writes to the database deliberately. Its own DIDs, and it
    # should run last so a mid-case failure cannot poison anything else.
    "sc-db-seed": {
        "cases": ["SC-DB-03"],
        "receivers": 0, "fund": 15, "last": True,
    },
}
