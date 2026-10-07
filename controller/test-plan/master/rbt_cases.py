"""rbt_cases.py - RBT cases: value and precision ladders, wallet shape,
splits, quorum capacity, pledging, concurrency, failure handling, bulk.

Registered into the suite by master_cases.py. Case wording lives in
master-catalogue.csv; this file is the code.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import re
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
    SKIP,
    TOL,
    node_reason,
    refusal_summary,
)


# =============================================================================
# RBT
# =============================================================================


# -----------------------------------------------------------------------------
# Mint / Transfer / Value / Precision / Wallet shape / Split / Quorum / Pledging / Concurrency / Failure / Bulk
# -----------------------------------------------------------------------------
#
# Run just this asset:  cd test-plan/full-test && python3 test_runner.py --only 'RBT-*'
#
# Every case returns (passed, actual, note):
#     True  -> matched the catalogue's Expected Result
#     False -> did not match: a real finding, investigate
#     SKIP  -> NOT ATTEMPTED, with the reason in `note`. Never a silent pass.
#
# Behaviour asserted here is verified against the product source, not assumed:
#   * Transfer endpoint is /rubix/v1/tx, and the request's `owner` field is the
#     RECEIVER (core/transaction.go: `nextOwnerDID := request.Owner`).
#   * HasRBT() is `Tokens.RBT > 0` (types/models/helpers.go), so amount 0 or
#     negative contributes no tokens, and a token-less transaction is rejected
#     by ValidateTransactionInfoFields ("must contain at least one transfer
#     token") - that is WHY RBT-024/025 reject.
#   * FloatPrecision ROUNDS at 3dp (math/math.go), so 0.0005 -> 0.001 and
#     0.0001 -> 0. Cases probing this RECORD the behaviour rather than assert a
#     guess, matching the catalogue's own "Record the behaviour" wording.
#   * A quorum must pledge >= the transfer value
#     (core/consensus/checks.go:539), so the largest testable transfer is
#     bounded by --fund-quorum, not just the sender's balance.
#   * A receiver credits asynchronously ~1-2s after the sender's call returns,
#     so every balance assertion polls (rc.wait_for_balance) - checking once
#     immediately reports a false zero.
#
# HOW TO READ A CASE
#     Every case docstring has the same parts:
#         WHAT IT CHECKS  - the assertion, in plain words
#         WHY IT MATTERS  - the bug it would catch, and the product code involved
#         PARTICIPANTS    - what the runner gives it (its NEEDS entry)
#         MANUAL STEPS    - how to do it BY HAND with curl/psql, no Python
#         PASS / FAIL     - exactly what makes it pass or fail
#     If the script and the manual steps disagree, the manual steps are the
#     specification.
#
#     On top of its own assertion, every case gets the runner's database check
#     (full-test/case_evidence.py): each transaction it made is in the
#     `transactions` table on both nodes, RBT is conserved across its
#     participants, nothing is left Locked, the rows are consistent. The
#     fullnode's accept/reject of each transaction is reported alongside.
#
# BY HAND - the commands the MANUAL STEPS refer to
#     S / R are the sender and receiver node IPs, SD / RD their DIDs:
#         SD=$(curl -s http://$S:20000/rubix/v1/dids | python3 -c \
#              'import sys,json; print(json.load(sys.stdin)["result"][0])')
#
#     BALANCE      curl -s http://$S:20000/rubix/v1/dids/$SD/balances/rbt
#                  -> {"balance": free, "locked": ..., "pledged": ...}
#     QUORUM       curl -s http://$S:20000/rubix/v1/quorums
#                  (the FIRST entry signs every transfer from this node)
#     SEND <X>     two steps - the first POST does nothing on its own:
#                  curl -s -X POST http://$S:20000/rubix/v1/tx \
#                    -H 'Content-Type: application/json' \
#                    -d '{"initiator":"'$SD'","owner":"'$RD'","memo":"manual",
#                         "tokens":{"rbt":<X>,"transferNftOwnership":false}}'
#                  -> {"result":{"id":"<reqID>"}}, then sign it:
#                  curl -s -X POST http://$S:20000/rubix/v1/signature \
#                    -H 'Content-Type: application/json' \
#                    -d '{"id":"<reqID>","password":"mypassword"}'
#                  -> {"status":true,"result":{"transactionID":"<TX>"}}
#     DB           psql -h $S -p 5433 -U rubix -d rubix     (password rubixpass)
#                  SELECT id FROM transactions WHERE id='<TX>';
#                  SELECT token_status, SUM(token_value), COUNT(*) FROM tokens
#                    WHERE did='<DID>' AND token_type=1 GROUP BY token_status;
#                  (status 0 Free, 1 Locked, 4 Transferred, 5 Committed,
#                   6/7 Pledged, 8 Burnt, 9 BurntForFT)
#
#     The receiver credits 1-2s after the sender's call returns - read its
#     balance again after a few seconds, never once.
#
# Why some cases are SKIP rather than implemented:
#   * NODE-KILL cases need to stop/restart a node mid-transfer. The controller
#     can do that over SSH, but doing it *at the right instant* mid-consensus
#     needs orchestration this runner doesn't have yet.
#
# Funding: every RBT comes from the faucet DID (rc.fund_did), signed by the
# faucet quorum. Nothing here mints RBT. DIDs keep what they hold between
# cycles and are topped up only by the shortfall, so the big-value and
# big-wallet cases pay their setup once, not every run.


FAKE_DID = "bafybmi" + "z" * 52       # 59 chars, right prefix, never created
MALFORMED_DID = "not-a-valid-did"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rbt_bal(host, did, port):
    """FREE balance - what a DID can spend now. Use for funding decisions."""
    ok, b, _ = rc.get_rbt_balance(host, did, port)
    return b if ok and b is not None else 0.0


def _rbt_held(entry, port):
    """Free + locked + pledged, 3dp - use for spent/gained deltas. A former
    quorum's pledges turn free at any moment (rc.get_rbt_held), which breaks a
    free-balance delta but not this one."""
    ok, h, _ = rc.get_rbt_held(entry["host"], entry["did"], port)
    return h if ok and h is not None else 0.0


def _wait_held(entry, target, port, attempts=10, delay=2):
    """Poll until the held total reaches `target` (3dp). Returns (reached, held)."""
    target = round(target, 3)
    held = _rbt_held(entry, port)
    for _ in range(attempts):
        if held >= target:
            return True, held
        time.sleep(delay)
        held = _rbt_held(entry, port)
    return held >= target, held


def _ensure_funded(ctx, entry, need):
    """Top a DID up to `need` RBT. Returns (ok, balance, note)."""
    cur = _rbt_bal(entry["host"], entry["did"], ctx.port)
    if cur >= need:
        return True, cur, ""
    status, msg = rc.fund_did(entry["host"], entry["did"], int(need - cur) + 1, ctx.port)
    if not status:
        return False, cur, "funding failed: {}".format(msg)
    ok, bal = rc.wait_for_balance(entry["host"], entry["did"], need, ctx.port)
    if not ok:
        return False, bal, "funding did not reach {} (got {})".format(need, bal)
    return True, bal, ""


# --- PRECONDITIONS -----------------------------------------------------
# A case must BUILD the state it needs, not assume the common setup left the
# fleet in the right shape. Two conditions are easy to get wrong and produce
# failures that look like product bugs but are pure fixture gaps:
#
#   1. Only SENDERS get a quorum registered during setup. The moment a case
#      makes a RECEIVER send (send-back, send-right-after-receiving, two
#      nodes sending to each other), that host has no quorum and the node
#      answers "No quorums available for transaction".
#   2. Quorum liquidity is CONSUMED as the run proceeds. A quorum must pledge
#      >= the transfer value, so a case late in the run can fail purely
#      because an earlier case drained its quorum - nothing to do with the
#      behaviour under test.
#
# _ensure_can_send() and _ensure_quorum_liquidity() below fix both, and every
# transfer helper calls them.

_QUORUM_REGISTERED = set()   # hosts already given a quorum this run


def _ensure_can_send(ctx, entry):
    """Guarantee this host can initiate a transaction at all.

    Registers a quorum on it if setup didn't (receivers never get one), using
    the same round-robin spread as senders so load isn't all on quorum #1.
    Idempotent - AddQuorum errors on repeat but rubix_client treats "already
    exists" as success. Returns (ok, quorum_entry, note)."""
    host = entry["host"]
    q = ctx.sender_quorum.get(host)
    if q is None:
        # deterministic spread, stable across runs for the same host list
        idx = abs(hash(host)) % len(ctx.quorum_hosts)
        q = ctx.quorum_hosts[idx]
        ctx.sender_quorum[host] = q
    if host in _QUORUM_REGISTERED:
        return True, q, ""
    ok, msg = rc.quorum_add(host, q["did"], ctx.port)
    if not ok:
        return False, q, "could not register a quorum on {}: {}".format(host, msg)
    _QUORUM_REGISTERED.add(host)
    time.sleep(0.5)  # let the node settle before it is asked to transact
    return True, q, ""


def _signing_quorum(ctx, entry):
    """The quorum that will actually sign for `entry`: the FIRST one registered
    on its node (quorumAddresses[0], core/transaction.go), which is not
    necessarily the one this run mapped to it - fund_lane registers quorum 1
    on every lane host before a case adds its own. Falls back to the mapping
    when the list cannot be read."""
    ok, listed, _ = rc.get_quorums(entry["host"], ctx.port)
    if ok and listed:
        first = str(listed[0]).split(".")[-1]
        for q in ctx.quorum_hosts:
            if q["did"] == first:
                return q
    return ctx.sender_quorum.get(entry["host"])


def _ensure_quorum_liquidity(ctx, entry, amount):
    """Guarantee the quorum backing `entry` can pledge `amount`.

    A quorum must pledge at least the transfer value
    (core/consensus/checks.go), and its free balance falls as the run
    proceeds, so this tops it up rather than letting a later case fail on
    someone else's spending. Returns (ok, quorum_free_balance, note)."""
    q = _signing_quorum(ctx, entry)
    if q is None:
        return True, 0.0, "no quorum mapped yet"
    free = _rbt_bal(q["host"], q["did"], ctx.port)
    if free >= amount:
        return True, free, ""
    ok, bal, note = _ensure_funded(ctx, q, amount + 50)   # headroom for the next case
    if not ok:
        return False, bal, "quorum {} could not be topped up to {}: {}".format(
            q["host"], amount, note)
    return True, bal, ""


def _prepare_sender(ctx, entry, amount):
    """Everything a host needs before it can send `amount`: a registered
    quorum, its own balance, and quorum pledge capacity."""
    ok, _q, note = _ensure_can_send(ctx, entry)
    if not ok:
        return False, note
    ok, _bal_, note = _ensure_funded(ctx, entry, amount)
    if not ok:
        return False, note
    ok, _qbal, note = _ensure_quorum_liquidity(ctx, entry, amount)
    if not ok:
        return False, note
    return True, ""


def _transfer(ctx, s, r, amount, memo="catalogue"):
    """Fire one transfer. Returns (status, message)."""
    status, msg, _ = rc.initiate_transaction(
        s["host"], s["did"], r["did"], rbt=amount, memo=memo, port=ctx.port)
    return status, msg


def _expect_success(ctx, s, r, amount, memo="catalogue"):
    """Transfer and verify BOTH sides moved by exactly `amount`.

    Builds the preconditions first (quorum registered on the sending host,
    sender funded, quorum able to pledge) so a fixture gap can never be
    mistaken for a product failure. Polls the receiver - the credit is
    asynchronous."""
    ok, note = _prepare_sender(ctx, s, amount)
    if not ok:
        return False, "precondition not met", note
    s0 = _rbt_held(s, ctx.port)
    r0 = _rbt_held(r, ctx.port)
    status, msg = _transfer(ctx, s, r, amount, memo)
    if not status:
        return False, "rejected", msg
    credited, r1 = _wait_held(r, r0 + amount, ctx.port)
    s1 = _rbt_held(s, ctx.port)
    if credited and rc.close_enough(s1, s0 - amount):
        return True, "sender {}->{}  receiver {}->{}".format(s0, s1, r0, r1), ""
    if credited:
        return False, "receiver credited but sender wrong", \
            "sender {}->{} (expected {})".format(s0, s1, round(s0 - amount, 3))
    return False, "receiver never credited", "sender {}->{} receiver {}->{}".format(s0, s1, r0, r1)


def _parallel(fns, workers=None):
    """Run callables concurrently; returns list of results in order."""
    workers = workers or len(fns)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda f: f(), fns))


# ---------------------------------------------------------------------------
# Mint (001-004)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Transfer basics (005-012)
# ---------------------------------------------------------------------------


def _scale(ctx, name, default):
    """A scale knob from the command line, falling back to the catalogue size."""
    return getattr(ctx.args, name, None) or default


def rbt_v_11(ctx, ci):
    """
    RBT-V-11 - Climb the value ladder until a transfer fails.

    WHAT IT CHECKS
        One sender sends 1, 10, 100, 500, 1000, 2500, 5000 RBT (up to
        --value-ceiling) to one receiver, one rung at a time, and records the
        largest value that went through and how long each rung took. Each rung
        must move exactly that value: sender down, receiver up.

    WHY IT MATTERS
        Every whole token is one row, locked, pledged against and persisted
        individually. The quorum must pledge >= the value
        (core/consensus/checks.go) and a transaction is processed in batches of
        TokenBatchSize=100 against 10m/15m quorum timeouts
        (core/quorum_initiator.go). The rung where it stops is the limit; that
        limit dropping between releases is the regression.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum. Before each rung the sender and its
        signing quorum are topped up from the faucet.

    MANUAL STEPS
        For X in 1 10 100 500 1000 2500 5000:
          BALANCE on S and R; SEND X from S to R; wait a few seconds;
          BALANCE on S and R again.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        Records a limit - PASS means the ladder ran. The Actual column gives
        the largest value that worked and the time per rung. A rung that fails
        stops the ladder and its reason is in the note. FAIL if even the first
        rung (1 RBT) fails - that measures no limit, it is a broken transfer.
    """
    s, r = ctx.pair(9)
    ceiling = float(_scale(ctx, "value_ceiling", 5000))
    largest, rungs, failure = 0.0, [], ""
    for amount in (1, 10, 100, 500, 1000, 2500, 5000):
        if amount > ceiling:
            break
        ok, note = _prepare_sender(ctx, s, amount)
        if not ok:
            failure = "stopped at {}: could not prepare ({})".format(amount, note)
            break
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, s, r, amount, "RBT-V-11")
        rungs.append("{}:{}s".format(amount, round(time.time() - t0, 1)))
        if passed is not True:
            failure = "failed at {}: {} {}".format(amount, actual, note)
            break
        largest = amount
    return largest > 0, "largest value that worked: {} RBT".format(largest), \
        "{} | {}".format(", ".join(rungs), failure or "ladder not exhausted "
                         "(--value-ceiling {})".format(int(ceiling)))


# A loop of sends stops once the node has refused this many IN A ROW - by then
# it is refusing for a reason, not by chance, and carrying on only burns time.
# The first refusal's message is kept so the report says WHY.
STOP_AFTER_REFUSALS = 10


def _send_loop(ctx, s, r, amount, n, memo):
    """Send `amount` from s to r up to n times. Returns (attempted, refused,
    first_refusal_message, stopped_early)."""
    refused, in_a_row, first = 0, 0, ""
    for i in range(n):
        status, msg = _transfer(ctx, s, r, amount, memo)
        if status:
            in_a_row = 0
            continue
        refused += 1
        in_a_row += 1
        first = first or (msg or "")[:200]
        if in_a_row >= STOP_AFTER_REFUSALS:
            return i + 1, refused, first, True
    return n, refused, first, False


def rbt_p_04(ctx, ci):
    """
    RBT-P-04 - Send 0.001 RBT many times (catalogue: 1000).

    WHAT IT CHECKS
        One sender sends 0.001 RBT to one receiver N times (--repeat-count,
        default 1000). Every send must succeed, and the receiver's total gain
        must be exactly N x 0.001 - no rounding drift.

    WHY IT MATTERS
        0.001 is MinDecimalUnit, and FloatPrecision rounds at 3dp
        (math/math.go). Every send splits a token down the parts tree
        (1 -> 0.5 -> 0.1 -> 0.05 -> 0.01 -> 0.005 -> 0.001), so a thousand of
        them exercise splitting, change tokens and the denomination counter far
        more than any single transfer. Drift accumulates here first.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        BALANCE on R. Repeat N times: SEND 0.001 from S to R.
        BALANCE on R: it must have grown by exactly N x 0.001.
        In the DB of S: every new part token has a tokenchain row:
          SELECT t.token_id FROM tokens t WHERE t.did='<SD>' AND t.token_type=1
            AND NOT EXISTS (SELECT 1 FROM tokenchain c WHERE c.token_id=t.token_id);

    PASS / FAIL
        PASS  every send accepted and the receiver gained exactly N x 0.001
        FAIL  any send rejected, or the total is off ("drift detected")
    """
    s, r = ctx.pair(9)
    n = int(_scale(ctx, "repeat_count", 1000))
    ok, _q, note = _ensure_can_send(ctx, s)
    if not ok:
        return False, "precondition not met", note
    ok, bal, note = _ensure_funded(ctx, s, max(1, n * 0.001 + 1))
    if not ok:
        return False, "could not fund sender", note
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    tried, failures, why, stopped = _send_loop(ctx, s, r, 0.001, n, "RBT-P-04")
    time.sleep(3)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    r1 = _rbt_bal(r["host"], r["did"], ctx.port)
    expected = round(0.001 * (tried - failures), 3)
    moved = round(r1 - r0, 3)
    if failures:
        return False, "{} of {} sends of 0.001 refused{}".format(
            failures, tried, " - stopped after {} refusals in a row".format(
                STOP_AFTER_REFUSALS) if stopped else ""), \
            "node's reason: {} | sender {} -> {}, receiver {} -> {}".format(
                why, s0, s1, r0, r1)
    if rc.close_enough(moved, expected):
        return True, "{} x 0.001 moved exactly {} (no drift)".format(n, moved), \
            "ran {} (--repeat-count; catalogue asks 1000)".format(n)
    return False, "drift detected", "expected {} moved {}".format(expected, moved)


# ---------------------------------------------------------------------------
# Wallet shape (034-040)
# ---------------------------------------------------------------------------
# RBT-W-03 and RBT-B-08 share pair 13: B-08 grows that sender's wallet, and
# W-03 pays its receiver. It keeps what it holds for the next cycle.
_BIG_WALLET_PAIR = 13


def rbt_w_03(ctx, ci):
    """
    RBT-W-03 - Send 100 RBT from a wallet made of thousands of small parts.

    WHAT IT CHECKS
        A second DID (the feeder) sends the sender 0.07 RBT N times
        (--tiny-tokens, default 2000), so the sender holds only part tokens
        summing past 100. The sender then sends 100 RBT; it must succeed and
        move exactly 100.

    WHY IT MATTERS
        selectTokensForAmount (core/wallet/token_lock.go) has to assemble 100
        from thousands of small rows, and every one of them has to be locked,
        pledged against and persisted. The parts were split by the feeder, not
        the sender, so the sender spends parts second-hand: the path the
        minter-allowlist genesis lookup gets wrong on builds without the
        part-token fixes (f890aa01, 257a9e9d, 4601dd04). A rejection naming
        ValidateMinterAllowlist is that known bug, not a new one.

    PARTICIPANTS
        2 senders, 2 receivers, 1 quorum. Pair 0 is the sender and the feeder;
        the 100 RBT goes to pair 1's receiver.

    MANUAL STEPS
        From the feeder: SEND 0.07 to S, N times.
        BALANCE on S: >= N x 0.07, all in parts:
          SELECT token_value, COUNT(*) FROM tokens WHERE did='<SD>'
            AND token_type=1 AND token_status=0 GROUP BY token_value;
        SEND 100 from S to the other receiver; BALANCE on both.

    PASS / FAIL
        PASS  the parts wallet was built and the 100 moved exactly
        FAIL  a feeder send was rejected, or the 100 did not move exactly
    """
    s, feeder = ctx.pair(14)
    count = int(_scale(ctx, "tiny_tokens", 2000))
    value = 0.07                           # count x value must clear 100
    need = round(count * value, 3) + 5
    ok, note = _prepare_sender(ctx, feeder, need)
    if not ok:
        return SKIP, "not attempted", "could not fund the feeder: {}".format(note)
    ok, _q, note = _ensure_can_send(ctx, s)
    if not ok:
        return False, "precondition not met", note
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    tried, failed, why, stopped = _send_loop(ctx, feeder, s, value, count, "RBT-W-03-build")
    built, s1 = rc.wait_for_balance(s["host"], s["did"], s0 + value * (tried - failed) - TOL,
                                    ctx.port, attempts=30)
    if failed or not built:
        return False, "could not build the parts wallet: {} of {} sends of {} refused{}".format(
            failed, tried, value, " - stopped after {} refusals in a row".format(
                STOP_AFTER_REFUSALS) if stopped else ""), \
            "node's reason: {} | sender {} -> {}".format(why, s0, s1)
    ok, note = _prepare_sender(ctx, s, 100)
    if not ok:
        return False, "precondition not met", note
    t0 = time.time()
    passed, actual, note = _expect_success(ctx, s, ctx.pair(_BIG_WALLET_PAIR)[1], 100, "RBT-W-03")
    return passed, "100 from {} parts of {} in {}s: {}".format(
        count, value, round(time.time() - t0, 2), actual), note


def rbt_s_02(ctx, ci):
    """
    RBT-S-02 - Split a token that was already split before.

    WHAT IT CHECKS
        Not run yet (SKIP). It re-splits a previously split pledge token.

    WHY IT MATTERS
        Known open bug: the child INSERT in core/wallet/persist_genesis_tx.go
        has no ON CONFLICT, so re-splitting collides on tokens_pkey. When this
        case runs it is expected to fail in exactly that way; a different
        failure is a new finding.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet.

    PASS / FAIL
        SKIP until written.
    """
    return SKIP, "not attempted", (
        "KNOWN OPEN BUG: split-token duplicate key (core/wallet/persist_genesis_tx.go, "
        "child INSERT lacks ON CONFLICT). Reproducing it "
        "requires flipping a parent token's Burnt status directly in Postgres - a "
        "DB-SEED fixture, which is deferred (needs psycopg2 + per-node credentials). "
        "Expected outcome when run: duplicate key on tokens_pkey.")


def rbt_s_04(ctx, ci):
    """
    RBT-S-04 - Fifty splits in a row on one wallet.

    WHAT IT CHECKS
        One sender sends 0.3 RBT to one receiver 50 times. Each send splits
        (0.3 is not a whole token) and each must move exactly 0.3, sender and
        receiver checked after every one.

    WHY IT MATTERS
        Repeated splitting walks the wallet through ever-more-fragmented
        states; the change from one split is the input to the next.
        LockTokensForSplit has a 5-retry / 15s budget
        (core/wallet/token_lock.go), and the denomination counter is read
        before the rows - a counter that drifts shows up as a split that
        cannot find the tokens it was promised.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        50 times: BALANCE on S and R; SEND 0.3 from S to R; BALANCE again -
        S down exactly 0.3, R up exactly 0.3.

    PASS / FAIL
        PASS  all 50 exact
        FAIL  the first split that is rejected or moves the wrong amount
    """
    s, r = ctx.pair(2)
    n = 50
    ok, note = _prepare_sender(ctx, s, n * 0.3 + 2)
    if not ok:
        return False, "precondition not met", note
    for i in range(n):
        passed, actual, note = _expect_success(ctx, s, r, 0.3, "RBT-S-04")
        if passed is not True:
            return False, "split {} of {} failed".format(i + 1, n), "{} {}".format(actual, note)
    return True, "{} consecutive splits, balance exact after each".format(n), ""


def _concurrent_transfers(ctx, n_senders, amount=1, memo="RBT-conc"):
    """Fire `n_senders` transfers at once, each from a different sender.
    Returns (available, ok_count, elapsed, detail)."""
    usable = min(n_senders, len(ctx.pairs))
    pairs = ctx.pairs[:usable]
    for s, _r in pairs:
        ok, note = _prepare_sender(ctx, s, amount + 1)
        if not ok:
            # a lab problem - must not be counted as a refused transfer
            raise RuntimeError("precondition not met for sender {}: {}".format(s["host"], note))
    fns = [(lambda s=s, r=r: _transfer(ctx, s, r, amount, memo)) for s, r in pairs]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok = sum(1 for status, _m in results if status)
    # The node's reasons, grouped. Cutting each message at 60 characters left
    # only the 'peer request failed: status=400 body={...' wrapper, so a 0/4
    # in RBT-N-13 on 2026-10-06 could not say why.
    detail = refusal_summary(m for status, m in results if not status)
    return usable, ok, elapsed, detail


def _capacity_case(ctx, wanted, memo):
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, wanted, memo=memo)
    if usable < wanted:
        return True, "{}/{} succeeded in {}s (only {} senders available, wanted {})".format(
            ok, usable, elapsed, usable, wanted), \
            "fleet has {} sender/receiver pairs; result is for {} not {}".format(
                len(ctx.pairs), usable, wanted)
    if ok == usable:
        return True, "{}/{} succeeded in {}s".format(ok, usable, elapsed), ""
    return False, "{}/{} succeeded in {}s".format(ok, usable, elapsed), detail


def rbt_q_02(ctx, ci):
    """
    RBT-Q-02 - 2 senders sharing ONE quorum, all at the same moment.

    WHAT IT CHECKS
        2 senders each send 1 RBT at once, all signed by the same quorum.
        Every one must succeed.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so concurrent senders queue on it. This rung of
        the ladder shows whether 2 at once still all succeed.

    PARTICIPANTS
        2 senders, 2 receivers (shared when fewer than senders), 1 quorum.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        Fire SEND 1 from all 2 at the same moment (one shell each, started
        together). Count successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        PASS  all 2 succeeded (and the runner found every one in both nodes'
              databases)
        FAIL  any refused, with the refusals in the note
    """
    return _capacity_case(ctx, 2, "RBT-Q-02")


def rbt_q_03(ctx, ci):
    """
    RBT-Q-03 - 5 senders sharing ONE quorum, all at the same moment.

    WHAT IT CHECKS
        5 senders each send 1 RBT at once, all signed by the same quorum.
        Every one must succeed.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so concurrent senders queue on it. This rung of
        the ladder shows whether 5 at once still all succeed.

    PARTICIPANTS
        5 senders, 5 receivers (shared when fewer than senders), 1 quorum.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        Fire SEND 1 from all 5 at the same moment (one shell each, started
        together). Count successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        PASS  all 5 succeeded (and the runner found every one in both nodes'
              databases)
        FAIL  any refused, with the refusals in the note
    """
    return _capacity_case(ctx, 5, "RBT-Q-03")


def rbt_q_04(ctx, ci):
    """
    RBT-Q-04 - 10 senders sharing ONE quorum, all at the same moment.

    WHAT IT CHECKS
        10 senders each send 1 RBT at once, all signed by the same quorum.
        Every one must succeed.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so concurrent senders queue on it. This rung of
        the ladder shows whether 10 at once still all succeed.

    PARTICIPANTS
        10 senders, 5 receivers (shared when fewer than senders), 1 quorum.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        Fire SEND 1 from all 10 at the same moment (one shell each, started
        together). Count successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        PASS  all 10 succeeded (and the runner found every one in both nodes'
              databases)
        FAIL  any refused, with the refusals in the note
    """
    return _capacity_case(ctx, 10, "RBT-Q-04")


def rbt_q_05(ctx, ci):
    """
    RBT-Q-05 - 20 senders sharing ONE quorum, all at the same moment.

    WHAT IT CHECKS
        20 senders each send 1 RBT at once, all signed by the same quorum.
        Records time and pass rate.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so concurrent senders queue on it. This rung of
        the ladder shows whether 20 at once still all succeed.

    PARTICIPANTS
        20 senders, 5 receivers (shared when fewer than senders), 1 quorum.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        Fire SEND 1 from all 20 at the same moment (one shell each, started
        together). Count successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        Records time and pass rate - PASS means it ran. The runner's DB check
        fails it if a "successful" transfer is missing from either node's
        database.
    """
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 20, memo="RBT-Q-05")
    return True, "{}/{} succeeded in {}s (pass rate {:.0f}%)".format(
        ok, usable, elapsed, 100.0 * ok / max(1, usable)), \
        detail or ("fleet provides {} pairs".format(usable))


def rbt_q_06(ctx, ci):
    """
    RBT-Q-06 - 40 senders sharing ONE quorum, all at the same moment.

    WHAT IT CHECKS
        40 senders each send 1 RBT at once, all signed by the same quorum.
        Every one must succeed.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so concurrent senders queue on it. This rung of
        the ladder shows whether 40 at once still all succeed.

    PARTICIPANTS
        40 senders, 5 receivers (shared when fewer than senders), 1 quorum.
        Needs 46 nodes: skipped while the pool is smaller.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        Fire SEND 1 from all 40 at the same moment (one shell each, started
        together). Count successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        Records time and pass rate - PASS means it ran. The runner's DB check
        fails it if a "successful" transfer is missing from either node's
        database.
    """
    if len(ctx.pairs) < 40:
        return SKIP, "not attempted", (
            "needs 40 concurrent senders; fleet currently provides {} sender/receiver "
            "pairs (31 pool hosts, minus quorums, split into pairs).".format(len(ctx.pairs)))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 40, memo="RBT-Q-06")
    return True, "{}/{} succeeded in {}s".format(ok, usable, elapsed), detail


def rbt_q_07(ctx, ci):
    """
    RBT-Q-07 - Climb the number of senders sharing ONE quorum.

    WHAT IT CHECKS
        With every free node, all sending 1 RBT at the same moment through one
        quorum, in steps of 2, 5, 10, 20 senders. Records the largest step at
        which every transfer succeeded.

    WHY IT MATTERS
        One quorum signs each transaction (quorumAddresses[0],
        core/transaction.go), so everyone sharing it queues on it. This is the
        headline number: how many nodes one quorum can serve at once.

    PARTICIPANTS
        Every free node ("senders": "all"), 5 receivers (shared), 1 quorum.
        Runs with nothing else running.

    MANUAL STEPS
        Point every sender node at the same quorum (QUORUM shows it first).
        For each step, fire SEND 1 from that many senders at the same moment
        (one shell per sender, started together). Count the successes.
          then on BOTH nodes: SELECT id FROM transactions WHERE id='<TX>';

    PASS / FAIL
        Records a limit - PASS means it ran. The Actual column gives the node
        limit and the per-step results; the runner's DB check fails the case if
        any "successful" transfer is not in both nodes' databases.
    """
    results = []
    limit = 0
    for n in (2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-Q-07")
        rate = 100.0 * ok / max(1, usable)
        results.append("{}n:{}/{} {}s".format(usable, ok, usable, elapsed))
        if rate == 100.0:
            limit = usable
    return True, "one-quorum node limit observed: {} (ladder: {})".format(
        limit, ", ".join(results)), \
        "bounded by fleet size ({} pairs), not necessarily by the quorum".format(len(ctx.pairs))


def rbt_q_13(ctx, ci):
    """
    RBT-Q-13 - How fast one quorum frees up: 20 transfers back to back.

    WHAT IT CHECKS
        One sender sends 1 RBT twenty times in a row, each as soon as the
        previous returns, and records the time each took. Records whether the
        quorum ever refused a transfer because it was still busy.

    WHY IT MATTERS
        A quorum's pledge is released by the unpledge path after the
        transaction settles (core/callback.go). If release lags behind arrival,
        a well-funded quorum can still stall under a steady stream.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        20 times, back to back: SEND 1 from S to R; note the time each takes.
        On the quorum's node, pledged value should fall back after each:
          SELECT token_status, SUM(token_value) FROM tokens
            WHERE did='<QUORUM DID>' AND token_type=1 GROUP BY token_status;

    PASS / FAIL
        Records timings - PASS means it ran. A refusal is recorded with the
        count of successes before it. FAIL if the first transfer is refused:
        nothing was back to back yet, so it is not a quorum-busy limit.
    """
    s, r = ctx.pair(4)
    n = 20
    ok, note = _prepare_sender(ctx, s, n + 2)
    if not ok:
        return False, "precondition not met", note
    timings = []
    for _ in range(n):
        t0 = time.time()
        status, msg = _transfer(ctx, s, r, 1, "RBT-Q-13")
        timings.append(round(time.time() - t0, 2))
        if not status:
            # Name the reason in the result itself. On 2026-10-06 this read
            # "quorum refused ... after 19 successes" when the refusal was a
            # clock difference (invalid epoch), not a busy quorum.
            return len(timings) > 1, "transfer {} refused after {} successes: {}".format(
                len(timings), len(timings) - 1, node_reason(msg, 120)), \
                "timings {}s".format(timings)
    return True, "{} back-to-back transfers all accepted; per-transfer {}s".format(n, timings), \
        "no interval found at which the quorum refused"


def rbt_l_02(ctx, ci):
    """
    RBT-L-02 - Pledged tokens cannot be spent while pledged.

    WHAT IT CHECKS
        Not run yet (SKIP). A quorum that is pledging tokens for one
        transaction tries to spend those same tokens.

    WHY IT MATTERS
        ValidatePledgeTransferDisjoint (core/consensus/checks.go) must stop a
        token being both pledged and transferred.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet.

    PASS / FAIL
        SKIP until written.
    """
    return SKIP, "not attempted", (
        "needs to identify a currently-pledged token and attempt to spend it - "
        "requires pledge-table visibility (see RBT-L-01).")


def rbt_l_03(ctx, ci):
    """
    RBT-L-03 - Interrupt a transfer after pledge, before it finishes.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop a node between the pledge and the
        end of consensus, then check what is left Locked or Pledged.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go); a
        failure that leaves tokens stuck is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs a node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return SKIP, "not attempted", (
        "NODE-KILL: must interrupt a transfer between pledge and completion. The "
        "controller can stop a node over SSH, but hitting that window mid-consensus "
        "needs orchestration this runner does not have.")


# ---------------------------------------------------------------------------
# Concurrency (061-067)
# ---------------------------------------------------------------------------
def rbt_n_01(ctx, ci):
    """
    RBT-N-01 - Double spend: the whole balance to two receivers at once.

    WHAT IT CHECKS
        One sender fires two transfers of its ENTIRE free balance at the same
        moment, to two different receivers. Exactly one may succeed.

    WHY IT MATTERS
        Token locking (core/wallet/token_lock.go) and the TOCTOU retry
        (core/transaction.go) must make the second transfer find nothing left.
        Both succeeding means value was created.

    PARTICIPANTS
        2 senders, 2 receivers, 1 quorum (one sender is used; the two
        receivers must differ). The quorum is topped up to pledge the whole
        balance.

    MANUAL STEPS
        BALANCE on S -> B. In two shells started together:
          SEND B from S to R1   and   SEND B from S to R2.
        BALANCE on S, R1, R2.

    PASS / FAIL
        PASS  exactly one succeeded
        FAIL  both succeeded ("DOUBLE SPEND")
        FAIL  both were rejected
    """
    s, r1 = ctx.pair(5)
    _s2, r2 = ctx.pair(6)
    ok, note = _prepare_sender(ctx, s, 2)
    if not ok:
        return False, "precondition not met", note
    held = _rbt_bal(s["host"], s["did"], ctx.port)
    # DIDs keep their balance between cycles, so "the whole balance" can be
    # large - the quorum must be able to pledge all of it for either to win.
    ok, _qbal, note = _ensure_quorum_liquidity(ctx, s, held)
    if not ok:
        return False, "precondition not met", note
    fns = [
        (lambda: _transfer(ctx, s, r1, held, "RBT-061a")),
        (lambda: _transfer(ctx, s, r2, held, "RBT-061b")),
    ]
    results = _parallel(fns)
    wins = sum(1 for status, _m in results if status)
    time.sleep(3)
    final = _rbt_bal(s["host"], s["did"], ctx.port)
    if wins == 1:
        return True, ("exactly one of two competing full-balance transfers succeeded "
                      "(sender {} -> {})".format(held, final)), ""
    if wins == 0:
        # "current balance X" in each refusal is what that request could still
        # lock. If they add up to the whole balance, the two requests split the
        # wallet between them and each came up short - no double spend, but
        # neither could win (seen 2026-09-29: 211.323 + 6.85 = 218.173).
        seen = [float(x) for _s, m in results
                for x in re.findall(r"current balance ([0-9.]+)", m or "")]
        if len(seen) == 2 and abs(sum(seen) - held) <= 0.002:
            return False, "both rejected - the two requests split the wallet", (
                "each locked part of the {} balance ({} + {}) and found too little "
                "for the full amount. No double spend, but neither transfer could "
                "win: the token collection does not let one request take the whole "
                "wallet when another arrives at the same moment".format(
                    held, seen[0], seen[1]))
        return False, "both competing transfers were rejected", \
            "; ".join((m or "")[:200] for _s, m in results)
    return False, "DOUBLE SPEND: both transfers succeeded", \
        "sender held {} and spent it twice; final balance {}".format(held, final)


def rbt_n_10(ctx, ci):
    """
    RBT-N-10 - Fifty transfers from ONE wallet at the same moment.

    WHAT IT CHECKS
        One sender fires 50 transfers of 1 RBT at once to one receiver. The
        sender must end exactly (number that succeeded) lower.

    WHY IT MATTERS
        Fifty operations lock tokens from one wallet concurrently. Two of them
        must never lock the same token, and a rejected one must release what
        it locked (core/wallet/token_lock.go, core/transaction.go).

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        BALANCE on S. Start 50 shells together, each: SEND 1 from S to R.
        BALANCE on S: fell by exactly the number of successes.
        In S's DB, nothing left Locked:
          SELECT COUNT(*) FROM tokens WHERE did='<SD>' AND token_status=1;

    PASS / FAIL
        PASS  the sender spent exactly as much as succeeded
        FAIL  "balance does not match successes"
    """
    s, r = ctx.pair(7)
    n = 50
    ok, note = _prepare_sender(ctx, s, n + 2)
    if not ok:
        return False, "precondition not met", note
    s0 = _rbt_held(s, ctx.port)
    fns = [(lambda: _transfer(ctx, s, r, 1, "RBT-N-10")) for _ in range(n)]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    time.sleep(3)
    s1 = _rbt_held(s, ctx.port)
    spent = round(s0 - s1, 3)
    if rc.close_enough(spent, float(ok_n)):
        return True, "{}/{} succeeded; sender spent exactly {} ({} -> {})".format(
            ok_n, n, spent, s0, s1), ""
    return False, "balance does not match successes", \
        "{} succeeded but sender spent {} ({} -> {})".format(ok_n, spent, s0, s1)


def rbt_n_11(ctx, ci):
    """
    RBT-N-11 - Many senders into ONE receiver at the same moment.

    WHAT IT CHECKS
        Eight senders each send 1 RBT to the same receiver at once. The
        receiver's gain must equal the number that succeeded.

    WHY IT MATTERS
        Concurrent credits to one wallet: the receiver persists each incoming
        transaction (core/transaction.go receiver persistence); a lost or
        doubled credit shows up here.

    PARTICIPANTS
        8 senders, 1 receiver, 1 quorum.

    MANUAL STEPS
        BALANCE on R. From 8 sender nodes at once: SEND 1 to R.
        BALANCE on R after a few seconds: up by exactly the successes.

    PASS / FAIL
        PASS  the receiver gained exactly the number of successes
        FAIL  "receiver total is not the exact sum"
    """
    target = ctx.receivers[0]
    senders = [e for e in ctx.senders + ctx.receivers[1:] if e["did"] != target["did"]]
    for s in senders:
        ok, note = _prepare_sender(ctx, s, 2)
        if not ok:
            return False, "precondition not met", note
    r0 = _rbt_held(target, ctx.port)
    fns = [(lambda s=s: _transfer(ctx, s, target, 1, "RBT-N-11")) for s in senders]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    credited, r1 = _wait_held(target, r0 + ok_n, ctx.port)
    got = round(r1 - r0, 3)
    if credited and rc.close_enough(got, float(ok_n)):
        return True, "{}/{} succeeded; receiver gained exactly {} ({} -> {})".format(
            ok_n, len(senders), got, r0, r1), ""
    return False, "receiver total is not the exact sum", \
        "{} succeeded but receiver gained {} ({} -> {})".format(ok_n, got, r0, r1)


def rbt_n_12(ctx, ci):
    """
    RBT-N-12 - Two nodes sending to each other at the same moment.

    WHAT IT CHECKS
        A sends 1 RBT to B while B sends 1 RBT to A, simultaneously. Both must
        succeed - no deadlock.

    WHY IT MATTERS
        Each side locks its own tokens and credits the other's; lock ordering
        across the two nodes (and the shared quorum's pledge lock, SELECT FOR
        UPDATE ... ORDER BY token_id, core/pledge_v2.go) must not deadlock.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum (the receiver sends too).

    MANUAL STEPS
        In two shells started together: SEND 1 from A to B, SEND 1 from B to A.
        BALANCE on both.

    PASS / FAIL
        PASS  both succeeded
        FAIL  either was rejected or hung
    """
    a, b = ctx.pair(8)
    for e in (a, b):
        ok, note = _prepare_sender(ctx, e, 2)
        if not ok:
            return False, "precondition not met", note
    a0 = _rbt_bal(a["host"], a["did"], ctx.port)
    b0 = _rbt_bal(b["host"], b["did"], ctx.port)
    fns = [
        (lambda: _transfer(ctx, a, b, 1, "RBT-064ab")),
        (lambda: _transfer(ctx, b, a, 1, "RBT-064ba")),
    ]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok_n = sum(1 for status, _m in results if status)
    time.sleep(3)
    a1 = _rbt_bal(a["host"], a["did"], ctx.port)
    b1 = _rbt_bal(b["host"], b["did"], ctx.port)
    if ok_n == 2:
        return True, "both directions succeeded in {}s (A {}->{}, B {}->{})".format(
            elapsed, a0, a1, b0, b1), ""
    return False, "{}/2 succeeded in {}s".format(ok_n, elapsed), \
        "; ".join((m or "")[:80] for _s, m in results if not _s)


def rbt_n_13(ctx, ci):
    """
    RBT-N-13 - Large values in parallel through one quorum.

    WHAT IT CHECKS
        Four senders each send half of the quorum's free balance at the same
        moment. Records how many succeeded; the ones that do not must fail
        cleanly.

    WHY IT MATTERS
        The quorum can pledge for at most two of these at once. The rest must
        be refused for pledge shortage without leaving anything Locked
        (core/consensus/checks.go pledge check).

    PARTICIPANTS
        4 senders, 4 receivers, 1 quorum.

    MANUAL STEPS
        BALANCE on the quorum -> Q. From 4 senders at once: SEND Q/2.
        Count successes; in each sender's DB nothing left Locked.

    PASS / FAIL
        Records the outcome - PASS means it ran; the runner's no_locks and
        conserved checks catch an unclean refusal.
    """
    quorum = ctx.quorum_for(ctx.senders[0])
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    amount = max(1, int(qbal / 2))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 4, amount=amount, memo="RBT-N-13")
    return True, "{}/{} large ({} RBT) transfers succeeded in {}s against a {} RBT quorum".format(
        ok, usable, amount, elapsed, qbal), \
        detail or "no pledge shortage observed at this size"


def rbt_n_14(ctx, ci):
    """
    RBT-N-14 - Tiny and large transfers mixed, in parallel.

    WHAT IT CHECKS
        Six senders fire at once: three send 10 RBT, three send 0.001 RBT.
        All six must succeed - small transfers must not be starved by large.

    WHY IT MATTERS
        The 0.001 transfers split; the 10s do not. Both share one quorum and
        run concurrently, so pledge and lock handling must serve both.

    PARTICIPANTS
        6 senders, 6 receivers, 1 quorum.

    MANUAL STEPS
        From 6 senders at once: three SEND 10, three SEND 0.001.
        Count successes by size.

    PASS / FAIL
        PASS  all six succeeded
        FAIL  "not all settled", with small and large counts
    """
    pairs = ctx.pairs[:6]
    for s, _r in pairs:
        ok, note = _prepare_sender(ctx, s, 12)
        if not ok:
            return False, "precondition not met", note
    fns = []
    for i, (s, r) in enumerate(pairs):
        amt = 10 if i % 2 == 0 else 0.001
        fns.append(lambda s=s, r=r, amt=amt: (amt, _transfer(ctx, s, r, amt, "RBT-N-14")))
    results = _parallel(fns)
    small_ok = sum(1 for amt, (st, _m) in results if amt < 1 and st)
    small_n = sum(1 for amt, _ in results if amt < 1)
    large_ok = sum(1 for amt, (st, _m) in results if amt >= 1 and st)
    large_n = sum(1 for amt, _ in results if amt >= 1)
    if small_ok == small_n and large_ok == large_n:
        return True, "all settled: {}/{} small, {}/{} large".format(
            small_ok, small_n, large_ok, large_n), ""
    # Say why each failed, not a guess: on 2026-09-29 this note claimed the
    # small ones were starved when it was the LARGE ones refused, all by the
    # ex-quorum ownership bug.
    why = sorted(set("{} RBT: {}".format(amt, (m or "")[:160])
                     for amt, (st, m) in results if not st))
    return False, "not all settled: {}/{} small, {}/{} large".format(
        small_ok, small_n, large_ok, large_n), "; ".join(why)


def rbt_n_15(ctx, ci):
    """
    RBT-N-15 - Every node sending at the same moment.

    WHAT IT CHECKS
        Every free node sends 1 RBT at once, through one quorum. The total RBT
        across all participants must be unchanged after (value conserved).

    WHY IT MATTERS
        The fleet-wide burst: the most concurrent load one quorum sees. Value
        created or lost under load is the worst possible bug.

    PARTICIPANTS
        Every free node ("senders": "all"), 5 receivers (shared), 1 quorum.
        Runs with nothing else running.

    MANUAL STEPS
        Sum BALANCE over all participants. Fire SEND 1 from every sender at
        once. Sum BALANCE again after a few seconds.

    PASS / FAIL
        PASS  total conserved
        FAIL  "NOT conserved"
    """
    everyone = ctx.senders + ctx.receivers
    for s, _r in ctx.pairs:
        ok, note = _prepare_sender(ctx, s, 2)
        if not ok:
            return False, "precondition not met", note
    time.sleep(2)
    before = sum(_rbt_held(e, ctx.port) for e in everyone)

    pairs = ctx.pairs
    fns = [(lambda s=s, r=r: _transfer(ctx, s, r, 1, "RBT-N-15")) for s, r in pairs]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok = sum(1 for status, _m in results if status)
    usable = len(pairs)
    detail = "; ".join((m or "")[:60] for status, m in results if not status)[:200]

    time.sleep(5)
    after = sum(_rbt_held(e, ctx.port) for e in everyone)
    conserved = rc.close_enough(round(before, 3), round(after, 3), tol=0.01)
    msg = "{}/{} succeeded in {}s; fleet total {} -> {}".format(
        ok, usable, elapsed, round(before, 3), round(after, 3))
    if conserved:
        return True, msg + " (conserved)", detail
    return False, msg + " (NOT conserved)", \
        "RBT was created or lost across the fleet during parallel load"


# ---------------------------------------------------------------------------
# Failure handling (068-072) - all NODE-KILL
# ---------------------------------------------------------------------------
def _node_kill_skip(what):
    return SKIP, "not attempted", (
        "NODE-KILL: needs to {} at a precise moment mid-transfer. restart-nodes.sh "
        "can stop/start a node over SSH, but coordinating that with an in-flight "
        "consensus round needs orchestration this runner does not have yet.".format(what))


def rbt_f_01(ctx, ci):
    """
    RBT-F-01 - Stop the receiver node mid transfer.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop the receiver's node while a transfer is in
        consensus, then check that nothing is left Locked or Pledged and that
        value is conserved once everything is back.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go) and
        stale NFT/SC locks are released on startup (fbc03fd4); a failure that
        strands tokens is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs the node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return _node_kill_skip("stop the receiver node")


def rbt_f_02(ctx, ci):
    """
    RBT-F-02 - Stop the quorum node mid transfer.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop the quorum's node while a transfer is in
        consensus, then check that nothing is left Locked or Pledged and that
        value is conserved once everything is back.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go) and
        stale NFT/SC locks are released on startup (fbc03fd4); a failure that
        strands tokens is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs the node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return _node_kill_skip("stop the quorum node")


def rbt_f_03(ctx, ci):
    """
    RBT-F-03 - Stop and restart the sender node mid transfer.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop the sender's node while a transfer is in
        consensus, then check that nothing is left Locked or Pledged and that
        value is conserved once everything is back.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go) and
        stale NFT/SC locks are released on startup (fbc03fd4); a failure that
        strands tokens is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs the node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return _node_kill_skip("stop and restart the sender node")


def rbt_f_04(ctx, ci):
    """
    RBT-F-04 - Restart the database during a transfer.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop the sender's Postgres container while a transfer is in
        consensus, then check that nothing is left Locked or Pledged and that
        value is conserved once everything is back.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go) and
        stale NFT/SC locks are released on startup (fbc03fd4); a failure that
        strands tokens is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs the node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return _node_kill_skip("restart the Postgres container")


def rbt_f_05(ctx, ci):
    """
    RBT-F-05 - Kill a node during heavy parallel load.

    WHAT IT CHECKS
        Not run yet (SKIP - NODE-KILL). Stop one node under load while a transfer is in
        consensus, then check that nothing is left Locked or Pledged and that
        value is conserved once everything is back.

    WHY IT MATTERS
        Three separate lock-release paths exist (core/transaction.go) and
        stale NFT/SC locks are released on startup (fbc03fd4); a failure that
        strands tokens is a real bug.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet (needs the node stopped at a precise moment).

    PASS / FAIL
        SKIP until the runner can kill a node mid-transfer.
    """
    return _node_kill_skip("kill a node during heavy parallel load")


# ---------------------------------------------------------------------------
# Bulk / performance (073-080)
# ---------------------------------------------------------------------------
def rbt_b_01(ctx, ci):
    """
    RBT-B-01 - Two hundred small transfers back to back.

    WHAT IT CHECKS
        One sender sends 1 RBT to one receiver N times (--burst-count, default
        200), each as soon as the previous returns. Records the time; the
        receiver must gain exactly N.

    WHY IT MATTERS
        Sustained throughput on one pair. Each transfer spends tokens the
        previous one may have just changed - stale chain tips show up here
        (and on the fullnode as previous-transaction mismatches).

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        BALANCE on R. N times back to back: SEND 1 from S to R.
        BALANCE on R: up by exactly N.

    PASS / FAIL
        PASS  none rejected and the receiver gained exactly N
        FAIL  any rejected, or the total is off
    """
    s, r = ctx.pair(9)
    n = int(_scale(ctx, "burst_count", 200))
    ok, note = _prepare_sender(ctx, s, n + 2)
    if not ok:
        return False, "precondition not met", note
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    t0 = time.time()
    tried, fails, why, stopped = _send_loop(ctx, s, r, 1, n, "RBT-B-01")
    elapsed = round(time.time() - t0, 2)
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + (tried - fails), ctx.port)
    got = round(r1 - r0, 3)
    if fails == 0 and rc.close_enough(got, float(n)):
        return True, "{} transfers in {}s ({:.2f}s each); receiver +{}".format(
            n, elapsed, elapsed / n, got), ""
    return False, "{} of {} refused{}; receiver +{} in {}s".format(
        fails, tried, " - stopped after {} refusals in a row".format(STOP_AFTER_REFUSALS)
        if stopped else "", got, elapsed), "node's reason: {}".format(why) if why else ""


def rbt_b_02(ctx, ci):
    """
    RBT-B-02 - 1,000 RBT back and forth, ten times.

    WHAT IT CHECKS
        1,000 RBT goes A -> B, B -> A, and so on, ten transfers. Each must move
        exactly 1,000; each transfer's time is recorded.

    WHY IT MATTERS
        High value repeatedly: the quorum pledges 1,000 each time and must
        release it before the next (pledge/unpledge churn), and the same
        tokens bounce between two wallets, growing their chains.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum (both sides send; both are topped up to
        1,000 first).

    MANUAL STEPS
        Alternate SEND 1000 A -> B and B -> A, ten times, with BALANCE on both
        after each.

    PASS / FAIL
        PASS  all ten exact
        FAIL  the first transfer that is rejected or inexact
    """
    s, r = ctx.pair(10)
    amount, rounds = 1000, 10
    for e in (s, r):
        ok, note = _prepare_sender(ctx, e, amount)
        if not ok:
            return SKIP, "not attempted", "could not prepare {}: {}".format(e["host"], note)
    timings = []
    a, b = s, r
    for i in range(rounds):
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, a, b, amount, "RBT-B-02")
        if passed is not True:
            return False, "high-value transfer {} of {} failed".format(i + 1, rounds), \
                "{} {}".format(actual, note)
        timings.append(round(time.time() - t0, 1))
        a, b = b, a
    return True, "{} x {} RBT, all exact; per-transfer {}s".format(
        rounds, amount, timings), ""


def rbt_b_03(ctx, ci):
    """
    RBT-B-03 - Parallel transfers stepped up, recording the pass rate.

    WHAT IT CHECKS
        Every free node, stepping 1, 2, 5, 10, 20 senders firing 1 RBT at the
        same moment through one quorum. Records successes and time per step.

    WHY IT MATTERS
        The pass-rate curve against concurrency; the point where it bends is
        the regression signal between releases.

    PARTICIPANTS
        Every free node ("senders": "all"), 5 receivers, 1 quorum. Runs with
        nothing else running.

    MANUAL STEPS
        As RBT-Q-07: at each step fire SEND 1 from that many senders at once;
        count successes and time.

    PASS / FAIL
        Records a curve - PASS means it ran.
    """
    curve = []
    for n in (1, 2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-B-03")
        curve.append("{}:{}/{}@{}s".format(usable, ok, usable, elapsed))
    return True, "pass-rate curve -> {}".format(", ".join(curve)), \
        "bounded by fleet size ({} pairs)".format(len(ctx.pairs))


def rbt_b_05(ctx, ci):
    """
    RBT-B-05 - Five splitting transfers versus five whole-token ones.

    WHAT IT CHECKS
        One sender sends 0.137 RBT five times, then 1.0 RBT five times, each
        checked exactly on both sides, and records the time of each group.

    WHY IT MATTERS
        0.137 needs splits at several levels of the parts tree; 1.0 needs none.
        The difference is the cost of splitting.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum.

    MANUAL STEPS
        Five times SEND 0.137, then five times SEND 1, S -> R, with BALANCE on
        both after each; time each group.

    PASS / FAIL
        PASS  all ten exact (the times are recorded)
        FAIL  the first transfer that is rejected or inexact
    """
    s, r = ctx.pair(11)
    ok, bal, note = _ensure_funded(ctx, s, 6)
    if not ok:
        return False, "could not fund sender", note
    t0 = time.time()
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 0.137, "RBT-B-05")
        if passed is not True:
            return False, "split transfer {} of 5 failed".format(i + 1), \
                "{} {}".format(actual, note)
    split_time = round(time.time() - t0, 2)
    t1 = time.time()
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 1.0, "RBT-B-05-whole")
        if passed is not True:
            return False, "whole-token transfer {} of 5 failed".format(i + 1), \
                "{} {}".format(actual, note)
    whole_time = round(time.time() - t1, 2)
    return True, "5 split transfers {}s vs 5 whole-token {}s".format(split_time, whole_time), ""


def rbt_b_06(ctx, ci):
    """
    RBT-B-06 - Transfers non-stop for hours (soak).

    WHAT IT CHECKS
        Not run yet (SKIP). A long soak must be scheduled deliberately, not
        inside a normal catalogue pass.

    PARTICIPANTS
        None - it does nothing yet.

    MANUAL STEPS
        Not defined yet.

    PASS / FAIL
        SKIP until scheduled.
    """
    return SKIP, "not attempted", (
        "soak test - 'run transfers nonstop for hours'. Needs to be scheduled "
        "deliberately, not run inside a normal catalogue pass.")


def rbt_b_07(ctx, ci):
    """
    RBT-B-07 - Transfer time as chain history grows.

    WHAT IT CHECKS
        1 RBT goes A -> B -> A -> ... for N hops (--chain-hops, default 100),
        each checked exactly. Records the time at hops 1, 10, 50, 100.

    WHY IT MATTERS
        Every hop adds a row to the token's chain, and each transfer is
        validated against that chain (TokenChainIntegrityCheck,
        core/consensus/checks.go). Time should stay flat as the chain grows.
        Hops are back to back, so the fullnode sees the same token's
        transactions in quick succession.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum (both sides send).

    MANUAL STEPS
        Alternate SEND 1 A -> B and B -> A, N times, timing each; BALANCE on
        both after each. Chain length of a token:
          SELECT COUNT(*) FROM tokenchain WHERE token_id='<TOKEN>';

    PASS / FAIL
        PASS  every hop exact (the times are recorded)
        FAIL  the first hop that is rejected or inexact
    """
    s, r = ctx.pair(12)
    hops = int(_scale(ctx, "chain_hops", 100))
    timings = []
    a, b = s, r
    for hop in range(hops):
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, a, b, 1, "RBT-B-07")
        if passed is not True:
            return False, "hop {} failed".format(hop + 1), "{} {}".format(actual, note)
        timings.append(round(time.time() - t0, 2))
        a, b = b, a
    marks = [i for i in (1, 10, 50, 100, 250, 500) if i <= len(timings)]
    return True, "per-hop time at hop " + ", ".join(
        "{}: {}s".format(i, timings[i - 1]) for i in marks), \
        "{} hops (--chain-hops)".format(hops)


def rbt_b_08(ctx, ci):
    """
    RBT-B-08 - Transfer time as the wallet grows.

    WHAT IT CHECKS
        The sender is grown to 10, 100, 1000, 2500, 5000 tokens (up to
        --wallet-ceiling) and at each size sends 1 RBT, timed and checked.

    WHY IT MATTERS
        Token selection and the denomination counter scan what the wallet
        holds (core/wallet/token_lock.go); a 1 RBT transfer should not get
        slower because the wallet is big.

    PARTICIPANTS
        1 sender, 1 receiver, 1 quorum. The sender keeps its tokens between
        runs, so the wallet is built once.

    MANUAL STEPS
        Fund S to each size from the faucet; at each, SEND 1 from S to R and
        time it.

    PASS / FAIL
        PASS  every size exact (the curve is recorded)
        FAIL  the transfer failed at a wallet size
    """
    s, r = ctx.pair(_BIG_WALLET_PAIR)
    ceiling = int(_scale(ctx, "wallet_ceiling", 5000))
    timings = []
    for target in (10, 100, 1000, 2500, 5000):
        if target > ceiling:
            break
        ok, note = _prepare_sender(ctx, s, target)
        if not ok:
            return True, "wallet-growth timings up to this point: {}".format(timings), \
                "stopped at {} tokens: {}".format(target, note)
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, s, r, 1, "RBT-B-08")
        if passed is not True:
            return False, "transfer failed at wallet size {}".format(target), \
                "{} {}".format(actual, note)
        timings.append("{}tok:{}s".format(target, round(time.time() - t0, 2)))
    return True, "transfer time vs wallet size -> {}".format(", ".join(timings)), ""


# ---------------------------------------------------------------------------
# GEN-IN-24
# ---------------------------------------------------------------------------


# =============================================================================
# REGISTRY
# =============================================================================


# --- RBT ---

CASES = {
    "RBT-V-11": rbt_v_11,
    "RBT-P-04": rbt_p_04,
    "RBT-W-03": rbt_w_03,
    "RBT-S-02": rbt_s_02,
    "RBT-S-04": rbt_s_04,
    "RBT-Q-02": rbt_q_02,
    "RBT-Q-03": rbt_q_03,
    "RBT-Q-04": rbt_q_04,
    "RBT-Q-05": rbt_q_05,
    "RBT-Q-06": rbt_q_06,
    "RBT-Q-07": rbt_q_07,
    "RBT-Q-13": rbt_q_13,
    "RBT-L-02": rbt_l_02,
    "RBT-L-03": rbt_l_03,
    "RBT-N-01": rbt_n_01,
    "RBT-N-10": rbt_n_10,
    "RBT-N-11": rbt_n_11,
    "RBT-N-12": rbt_n_12,
    "RBT-N-13": rbt_n_13,
    "RBT-N-14": rbt_n_14,
    "RBT-N-15": rbt_n_15,
    "RBT-F-01": rbt_f_01,
    "RBT-F-02": rbt_f_02,
    "RBT-F-03": rbt_f_03,
    "RBT-F-04": rbt_f_04,
    "RBT-F-05": rbt_f_05,
    "RBT-B-01": rbt_b_01,
    "RBT-B-02": rbt_b_02,
    "RBT-B-03": rbt_b_03,
    "RBT-B-05": rbt_b_05,
    "RBT-B-06": rbt_b_06,
    "RBT-B-07": rbt_b_07,
    "RBT-B-08": rbt_b_08,
}

ORDER = list(CASES)


# What each case needs - see NEEDS in full-test/test_runner.py. Every RBT case
# funds its own sender and the signing quorum as it goes (_prepare_sender), so
# none asks for `fund` up front. Skip-only placeholders need nothing.
_NOTHING = {"senders": 0, "receivers": 0, "quorums": 0}
NEEDS = {
    "RBT-V-11": {},                                  # one sender, one receiver
    "RBT-P-04": {},
    # W-03 builds the parts wallet from a second DID (the feeder) and pays a
    # third, so two pairs.
    "RBT-W-03": {"senders": 2, "receivers": 2},
    "RBT-S-02": _NOTHING,
    "RBT-S-04": {},
    # How many senders can share ONE quorum. Receivers repeat when there are
    # fewer of them - the load under test is on the quorum.
    "RBT-Q-02": {"senders": 2, "receivers": 2},
    "RBT-Q-03": {"senders": 5, "receivers": 5},
    "RBT-Q-04": {"senders": 10, "receivers": 5},
    "RBT-Q-05": {"senders": 20, "receivers": 5},
    "RBT-Q-06": {"senders": 40, "receivers": 5},   # skipped until the pool has 46 nodes
    "RBT-Q-07": {"senders": "all", "receivers": 5},
    "RBT-Q-13": {},
    "RBT-L-02": _NOTHING,
    "RBT-L-03": _NOTHING,
    # N-01 sends one balance to two DIFFERENT receivers at once.
    "RBT-N-01": {"senders": 2, "receivers": 2},
    "RBT-N-10": {},
    "RBT-N-11": {"senders": 8, "receivers": 1},     # many into one receiver
    "RBT-N-12": {},
    "RBT-N-13": {"senders": 4, "receivers": 4},
    "RBT-N-14": {"senders": 6, "receivers": 6},
    "RBT-N-15": {"senders": "all", "receivers": 5},
    "RBT-F-01": _NOTHING,
    "RBT-F-02": _NOTHING,
    "RBT-F-03": _NOTHING,
    "RBT-F-04": _NOTHING,
    "RBT-F-05": _NOTHING,
    "RBT-B-01": {},
    "RBT-B-02": {},
    "RBT-B-03": {"senders": "all", "receivers": 5},
    "RBT-B-05": {},
    "RBT-B-06": _NOTHING,
    "RBT-B-07": {},
    "RBT-B-08": {},
}

# Time-boxed: run once, without delay (see NO_DELAY_RERUN in sc_cases.py).
NO_DELAY_RERUN = {"RBT-B-06"}    # the 4-hour soak, once it is written

TIMING_CASES = {
    "RBT-V-11",  # value ladder - largest value that works
    "RBT-Q-05",  # 20-node pass rate + time
    "RBT-Q-07",  # node limit for one quorum
    "RBT-Q-13",  # how fast a quorum frees up
    "RBT-N-13",  # large values in parallel
    "RBT-B-01",  # back-to-back throughput
    "RBT-B-02",  # repeated high-value throughput
    "RBT-B-03",  # parallel pass-rate curve
    "RBT-B-05",  # split vs whole-token timing
    "RBT-B-07",  # transfer time as chain history grows
    "RBT-B-08",  # transfer time as wallet grows
}
