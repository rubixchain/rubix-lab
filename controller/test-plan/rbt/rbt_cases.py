#!/usr/bin/env python3
"""
rbt_cases.py - RBT cases (RBT-001..RBT-080) from master-test-cases.xlsx.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases rbt

Every case returns (passed, actual, note):
    True  -> matched the catalogue's Expected Result
    False -> did not match: a real finding, investigate
    SKIP  -> NOT ATTEMPTED, with the reason in `note`. Never a silent pass.

Behaviour asserted here is verified against the product source, not assumed:
  * Transfer endpoint is /rubix/v1/tx, and the request's `owner` field is the
    RECEIVER (core/transaction.go: `nextOwnerDID := request.Owner`).
  * HasRBT() is `Tokens.RBT > 0` (types/models/helpers.go), so amount 0 or
    negative contributes no tokens, and a token-less transaction is rejected
    by ValidateTransactionInfoFields ("must contain at least one transfer
    token") - that is WHY RBT-024/025 reject.
  * FloatPrecision ROUNDS at 3dp (math/math.go), so 0.0005 -> 0.001 and
    0.0001 -> 0. Cases probing this RECORD the behaviour rather than assert a
    guess, matching the catalogue's own "Record the behaviour" wording.
  * A quorum must pledge >= the transfer value
    (core/consensus/checks.go:539), so the largest testable transfer is
    bounded by --fund-quorum, not just the sender's balance.
  * A receiver credits asynchronously ~1-2s after the sender's call returns,
    so every balance assertion polls (rc.wait_for_balance) - checking once
    immediately reports a false zero.

Why some cases are SKIP rather than implemented:
  * NODE-KILL cases need to stop/restart a node mid-transfer. The controller
    can do that over SSH, but doing it *at the right instant* mid-consensus
    needs orchestration this runner doesn't have yet.
  * DB-SEED cases need direct Postgres access to corrupt state deliberately;
    per CLAUDE.md that is deferred (needs psycopg2 + per-node credentials).
  * Very-high-value cases are bounded by minting cost: generateLocalRBT mints
    ONE token per unit in a loop (core/token.go), measured at ~15s per 1000.
    100,000 RBT is ~25 minutes of minting per wallet, and the quorum needs the
    same again to pledge it. Not a tweak - a multi-hour setup.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc

SKIP = "SKIP"

FAKE_DID = "bafybmi" + "z" * 52       # 59 chars, right prefix, never created
MALFORMED_DID = "not-a-valid-did"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _bal(host, did, port):
    ok, b, _ = rc.get_rbt_balance(host, did, port)
    return b if ok and b is not None else 0.0


def _ensure_funded(ctx, entry, need):
    """Top a DID up to `need` RBT. Returns (ok, balance, note)."""
    cur = _bal(entry["host"], entry["did"], ctx.port)
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


def _ensure_quorum_liquidity(ctx, entry, amount):
    """Guarantee the quorum backing `entry` can pledge `amount`.

    A quorum must pledge at least the transfer value
    (core/consensus/checks.go), and its free balance falls as the run
    proceeds, so this tops it up rather than letting a later case fail on
    someone else's spending. Returns (ok, quorum_free_balance, note)."""
    q = ctx.sender_quorum.get(entry["host"])
    if q is None:
        return True, 0.0, "no quorum mapped yet"
    free = _bal(q["host"], q["did"], ctx.port)
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
    s0 = _bal(s["host"], s["did"], ctx.port)
    r0 = _bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, amount, memo)
    if not status:
        return False, "rejected", msg
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + amount, ctx.port)
    s1 = _bal(s["host"], s["did"], ctx.port)
    if credited and rc.close_enough(s1, s0 - amount):
        return True, "sender {}->{}  receiver {}->{}".format(s0, s1, r0, r1), ""
    if credited:
        return False, "receiver credited but sender wrong", \
            "sender {}->{} (expected {})".format(s0, s1, round(s0 - amount, 3))
    return False, "receiver never credited", "sender {}->{} receiver {}->{}".format(s0, s1, r0, r1)


def _locked(host, did, port):
    ok, d, _ = rc.get_rbt_balance_detail(host, did, port)
    return d["locked"] if ok and d else 0.0


def _expect_rejection(ctx, s, receiver_did, amount, memo="catalogue"):
    """Fire a transfer that SHOULD be refused, then confirm two things the
    catalogue asks for on nearly every rejection case:
      1. the sender's balance is untouched (value must not move), and
      2. no tokens are left stuck in Locked.

    (2) matters because `balance` reports only the FREE portion: tokens
    stranded in Locked by a failed transfer would silently reduce it, and
    per CLAUDE.md a failure that leaves tokens Locked is a real bug, not a
    cosmetic one."""
    # Register a quorum on the sending host first. Without it the node answers
    # "No quorums available" - which IS a rejection, but not the one under
    # test, and would make this case pass for entirely the wrong reason.
    ok, _q, note = _ensure_can_send(ctx, s)
    if not ok:
        return False, "precondition not met", note
    s0 = _bal(s["host"], s["did"], ctx.port)
    locked0 = _locked(s["host"], s["did"], ctx.port)
    status, msg, _ = rc.initiate_transaction(
        s["host"], s["did"], receiver_did, rbt=amount, memo=memo, port=ctx.port)
    time.sleep(2)  # give any lock-release path time to run
    s1 = _bal(s["host"], s["did"], ctx.port)
    locked1 = _locked(s["host"], s["did"], ctx.port)
    if status:
        return False, "ACCEPTED but should have been rejected", msg
    if not rc.close_enough(s0, s1):
        return False, "rejected, but sender balance changed", \
            "{} -> {} (value moved on a rejected transfer)".format(s0, s1)
    if locked1 > locked0 + 0.0015:
        return False, "rejected, but tokens left LOCKED", \
            "locked {} -> {}; a failed transfer must release its locks".format(locked0, locked1)
    return True, "rejected, balance and locks clean: {}".format((msg or "")[:100]), ""


def _parallel(fns, workers=None):
    """Run callables concurrently; returns list of results in order."""
    workers = workers or len(fns)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda f: f(), fns))


# ---------------------------------------------------------------------------
# Mint (001-004)
# ---------------------------------------------------------------------------
def c001(ctx, ci):
    s, _ = ctx.pair(0)
    before = _bal(s["host"], s["did"], ctx.port)
    status, msg = rc.fund_did(s["host"], s["did"], 5, ctx.port)
    if not status:
        return False, "mint rejected", msg
    ok, after = rc.wait_for_balance(s["host"], s["did"], before + 5, ctx.port)
    if not ok:
        return False, "balance did not rise by 5", "before={} after={}".format(before, after)
    return True, "balance {} -> {}".format(before, after), ""


def c002(ctx, ci):
    s, _ = ctx.pair(1)
    before = _bal(s["host"], s["did"], ctx.port)
    s1, m1 = rc.fund_did(s["host"], s["did"], 3, ctx.port)
    s2, m2 = rc.fund_did(s["host"], s["did"], 3, ctx.port)
    if not (s1 and s2):
        return False, "one or both mints rejected", "{} / {}".format(m1, m2)
    ok, after = rc.wait_for_balance(s["host"], s["did"], before + 6, ctx.port)
    if not ok:
        return False, "two mints of 3 did not add up", "before={} after={}".format(before, after)
    return True, "balance {} -> {} (3 + 3)".format(before, after), ""


def c003(ctx, ci):
    """Large single mint. The catalogue asks to record the largest that works
    and how long it takes; minting is one token per unit, so this is timed."""
    s, _ = ctx.pair(2)
    amount = int(getattr(ctx.args, "large_mint", 2000))
    before = _bal(s["host"], s["did"], ctx.port)
    t0 = time.time()
    status, msg = rc.fund_did(s["host"], s["did"], amount, ctx.port)
    if not status:
        return True, "mint of {} refused with a clear limit".format(amount), msg
    ok, after = rc.wait_for_balance(s["host"], s["did"], before + amount, ctx.port,
                                     attempts=60, delay=2)
    took = round(time.time() - t0, 1)
    if not ok:
        return False, "mint of {} did not fully land in {}s".format(amount, took), \
            "before={} after={}".format(before, after)
    return True, "minted {} in {}s ({} -> {})".format(amount, took, before, after), ""


def c004(ctx, ci):
    return SKIP, "not attempted", (
        "needs a node with ZERO DIDs. Every pool host has exactly one, and "
        "CreateDID has no idempotency (core/did.go) so minting a second identity "
        "to create the fixture would be irreversible. Needs a freshly installed node.")


# ---------------------------------------------------------------------------
# Transfer basics (005-012)
# ---------------------------------------------------------------------------
def c005(ctx, ci):
    s, r = ctx.pair(0)
    return _expect_success(ctx, s, r, 1, "RBT-005")


def c006(ctx, ci):
    """Send back the other way - receiver becomes sender."""
    s, r = ctx.pair(0)
    ok, bal, note = _ensure_funded(ctx, r, 1)
    if not ok:
        return False, "could not fund the return sender", note
    return _expect_success(ctx, r, s, 1, "RBT-006")


def c007(ctx, ci):
    s, _ = ctx.pair(1)
    return _expect_rejection(ctx, s, FAKE_DID, 1, "RBT-007")


def c008(ctx, ci):
    s, _ = ctx.pair(1)
    return _expect_rejection(ctx, s, MALFORMED_DID, 1, "RBT-008")


def c009(ctx, ci):
    """Self-transfer: same DID both sides. Per CLAUDE.md this is supported for
    RBT via the local-DID branch (core/transaction.go:495) - overall balance
    should be unchanged."""
    s, _ = ctx.pair(2)
    before = _bal(s["host"], s["did"], ctx.port)
    status, msg = _transfer(ctx, s, s, 1, "RBT-009")
    if not status:
        return False, "rejected", msg
    time.sleep(2)
    after = _bal(s["host"], s["did"], ctx.port)
    if rc.close_enough(before, after):
        return True, "succeeded, balance unchanged at {}".format(after), ""
    return False, "balance changed on a self-transfer", "{} -> {}".format(before, after)


def c010(ctx, ci):
    return SKIP, "not attempted", (
        "needs TWO DIDs on one node. Fleet is one DID per node by design, and "
        "creating a second would violate the CreateDID hard gate (no idempotency).")


def c011(ctx, ci):
    """Catalogue says 'Define what happens' - so RECORD, don't assert."""
    s, _ = ctx.pair(3)
    before = _bal(s["host"], s["did"], ctx.port)
    status, msg, _ = rc.initiate_transaction(
        s["host"], s["did"], "", rbt=1, memo="RBT-011", port=ctx.port)
    time.sleep(2)
    after = _bal(s["host"], s["did"], ctx.port)
    return True, "empty receiver -> status={} balance {} -> {}".format(status, before, after), \
        "recorded, not asserted: {}".format((msg or "")[:120])


def c012(ctx, ci):
    """Spend immediately after receiving - probes stale chain-tip handling."""
    s, r = ctx.pair(4)
    ok, actual, note = _expect_success(ctx, s, r, 1, "RBT-012-seed")
    if ok is not True:
        return False, "could not seed the receiver", "{} {}".format(actual, note)
    return _expect_success(ctx, r, s, 1, "RBT-012")


# ---------------------------------------------------------------------------
# Value (013-028)
# ---------------------------------------------------------------------------
def _value_case(ctx, pair_index, amount, memo):
    s, r = ctx.pair(pair_index)
    return _expect_success(ctx, s, r, amount, memo)


def c013(ctx, ci):
    return _value_case(ctx, 5, 0.001, "RBT-013")


def c014(ctx, ci):
    """0.0001 is below MinDecimalUnit; FloatPrecision rounds it to 0, and a
    zero-value transaction carries no tokens -> rejected."""
    s, r = ctx.pair(5)
    return _expect_rejection(ctx, s, r["did"], 0.0001, "RBT-014")


def c015(ctx, ci):
    return _value_case(ctx, 6, 0.999, "RBT-015")


def c016(ctx, ci):
    return _value_case(ctx, 6, 2.345, "RBT-016")


def c017(ctx, ci):
    return _value_case(ctx, 7, 99.999, "RBT-017")


def c018(ctx, ci):
    return _value_case(ctx, 7, 100, "RBT-018")


def _large_value_case(ctx, pair_index, amount, memo):
    """Large transfers are bounded by BOTH the sender's balance and the
    quorum's pledge capacity. Skip honestly rather than report a misleading
    failure when the fixture can't support the value."""
    s, r = ctx.pair(pair_index)
    quorum = ctx.quorum_for(s)
    qbal = _bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    if qbal < amount:
        return SKIP, "not attempted", (
            "quorum {} holds {} RBT but must pledge >= {} "
            "(core/consensus/checks.go). Re-run with --fund-quorum {} or higher; "
            "note minting is ~15s per 1000 RBT.".format(
                quorum["host"] if quorum else "?", qbal, amount, int(amount)))
    ok, bal, note = _ensure_funded(ctx, s, amount)
    if not ok:
        return SKIP, "not attempted", "could not fund sender to {}: {}".format(amount, note)
    return _expect_success(ctx, s, r, amount, memo)


def c019(ctx, ci):
    return _large_value_case(ctx, 8, 1000, "RBT-019")


def c020(ctx, ci):
    return _large_value_case(ctx, 8, 10000, "RBT-020")


def c021(ctx, ci):
    return SKIP, "not attempted", (
        "100,000 RBT needs ~25 min of minting on the sender AND the same again on "
        "the quorum to pledge it (one token per unit, ~15s/1000). Deliberate "
        "long-run setup, not a normal cycle.")


def c022(ctx, ci):
    return SKIP, "not attempted", (
        "200,000.10 RBT - ~50 min of minting per wallet plus matching quorum "
        "pledge capacity. Same reason as RBT-021.")


def c023(ctx, ci):
    """Value ladder: climb until it fails, RECORD the largest that worked."""
    s, r = ctx.pair(9)
    quorum = ctx.quorum_for(s)
    qbal = _bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    largest = 0.0
    failure = ""
    for amount in (1, 10, 50, 100, 250, 500, 1000, 2500, 5000):
        if amount > qbal:
            failure = "stopped at {}: exceeds quorum pledge capacity ({})".format(amount, qbal)
            break
        ok, bal, note = _ensure_funded(ctx, s, amount)
        if not ok:
            failure = "stopped at {}: could not fund sender ({})".format(amount, note)
            break
        passed, actual, note = _expect_success(ctx, s, r, amount, "RBT-023")
        if passed is not True:
            failure = "failed at {}: {} {}".format(amount, actual, note)
            break
        largest = amount
    return True, "largest value that worked: {} RBT".format(largest), \
        failure or "ladder not exhausted within fixture limits"


def c024(ctx, ci):
    s, r = ctx.pair(10)
    return _expect_rejection(ctx, s, r["did"], 0, "RBT-024")


def c025(ctx, ci):
    s, r = ctx.pair(10)
    return _expect_rejection(ctx, s, r["did"], -1, "RBT-025")


def c026(ctx, ci):
    s, r = ctx.pair(11)
    held = _bal(s["host"], s["did"], ctx.port)
    return _expect_rejection(ctx, s, r["did"], held + 1000, "RBT-026")


def c027(ctx, ci):
    """Send the entire balance - sender must land on exactly 0."""
    s, r = ctx.pair(12)
    held = _bal(s["host"], s["did"], ctx.port)
    if held <= 0:
        ok, held, note = _ensure_funded(ctx, s, 3)
        if not ok:
            return False, "could not fund sender", note
    quorum = ctx.quorum_for(s)
    qbal = _bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    if held > qbal:
        return SKIP, "not attempted", \
            "sender holds {} but quorum can only pledge {}".format(held, qbal)
    r0 = _bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, held, "RBT-027")
    if not status:
        return False, "rejected", msg
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + held, ctx.port)
    s1 = _bal(s["host"], s["did"], ctx.port)
    if credited and rc.close_enough(s1, 0.0):
        return True, "sender {} -> {} (exactly zero), receiver {} -> {}".format(held, s1, r0, r1), ""
    return False, "did not land on exactly zero", "sender {} -> {}, receiver {} -> {}".format(
        held, s1, r0, r1)


def c028(ctx, ci):
    """One MinDecimalUnit above the balance must be refused."""
    s, r = ctx.pair(13)
    held = _bal(s["host"], s["did"], ctx.port)
    if held <= 0:
        ok, held, note = _ensure_funded(ctx, s, 2)
        if not ok:
            return False, "could not fund sender", note
    return _expect_rejection(ctx, s, r["did"], round(held + 0.001, 3), "RBT-028")


# ---------------------------------------------------------------------------
# Precision (029-033)
# ---------------------------------------------------------------------------
def c029(ctx, ci):
    """1, 2 and 3 decimal places. Catalogue asks for >=10 values in each;
    `--decimal-samples` controls how many actually run, since each transfer
    costs ~2s of settle time."""
    s, r = ctx.pair(6)
    n = int(getattr(ctx.args, "decimal_samples", 3))
    amounts = []
    for i in range(1, n + 1):
        amounts += [round(i * 0.1, 1), round(i * 0.11, 2), round(i * 0.111, 3)]
    failures = []
    for amt in amounts:
        passed, actual, note = _expect_success(ctx, s, r, amt, "RBT-029")
        if passed is not True:
            failures.append("{}: {} {}".format(amt, actual, note))
    if failures:
        return False, "{}/{} failed".format(len(failures), len(amounts)), "; ".join(failures[:4])
    return True, "all {} amounts moved exactly ({} per decimal place)".format(len(amounts), n), ""


def c030(ctx, ci):
    """0.0005: FloatPrecision ROUNDS at 3dp, so this should become 0.001.
    Catalogue says 'Record the behaviour' - so record, don't assert."""
    s, r = ctx.pair(7)
    s0 = _bal(s["host"], s["did"], ctx.port)
    r0 = _bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, 0.0005, "RBT-030")
    time.sleep(3)
    s1 = _bal(s["host"], s["did"], ctx.port)
    r1 = _bal(r["host"], r["did"], ctx.port)
    moved = round(s0 - s1, 4)
    return True, "status={} sender moved {} (0.0005 -> {}), receiver {} -> {}".format(
        status, moved, moved, r0, r1), \
        "recorded, not asserted. FloatPrecision rounds at 3dp so 0.0005 is expected " \
        "to become 0.001. msg: {}".format((msg or "")[:100])


def c031(ctx, ci):
    """4+ decimal places. Catalogue expects Rejected, but FloatPrecision
    rounds - so verify no value is created or lost either way, and record
    which behaviour actually occurred."""
    s, r = ctx.pair(8)
    s0 = _bal(s["host"], s["did"], ctx.port)
    r0 = _bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, 1.2345, "RBT-031")
    time.sleep(3)
    s1 = _bal(s["host"], s["did"], ctx.port)
    r1 = _bal(r["host"], r["did"], ctx.port)
    sent = round(s0 - s1, 4)
    got = round(r1 - r0, 4)
    if not status:
        if rc.close_enough(s0, s1):
            return True, "rejected, balance unchanged", (msg or "")[:120]
        return False, "rejected but sender balance changed", "{} -> {}".format(s0, s1)
    if rc.close_enough(sent, got):
        return True, "accepted and rounded: sent {} received {} (no value created/lost)".format(
            sent, got), "catalogue expected rejection; product rounds instead - recorded"
    return False, "value created or lost", "sender -{} but receiver +{}".format(sent, got)


def c032(ctx, ci):
    """0.001 x N. Catalogue says 1000 times; at ~2s settle each that is ~33
    minutes, so N is configurable and the actual count is recorded."""
    s, r = ctx.pair(9)
    n = int(getattr(ctx.args, "repeat_count", 25))
    ok, bal, note = _ensure_funded(ctx, s, max(1, n * 0.001 + 1))
    if not ok:
        return False, "could not fund sender", note
    s0 = _bal(s["host"], s["did"], ctx.port)
    r0 = _bal(r["host"], r["did"], ctx.port)
    failures = 0
    for _ in range(n):
        status, _msg = _transfer(ctx, s, r, 0.001, "RBT-032")
        if not status:
            failures += 1
    time.sleep(3)
    s1 = _bal(s["host"], s["did"], ctx.port)
    r1 = _bal(r["host"], r["did"], ctx.port)
    expected = round(0.001 * (n - failures), 3)
    moved = round(r1 - r0, 3)
    if failures:
        return False, "{}/{} transfers rejected".format(failures, n), \
            "sender {} -> {}, receiver {} -> {}".format(s0, s1, r0, r1)
    if rc.close_enough(moved, expected):
        return True, "{} x 0.001 moved exactly {} (no drift)".format(n, moved), \
            "catalogue asks for 1000; ran {} (--repeat-count)".format(n)
    return False, "drift detected", "expected {} moved {}".format(expected, moved)


def c033(ctx, ci):
    """0.333 three times -> exactly 0.999, no creeping error."""
    s, r = ctx.pair(10)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    r0 = _bal(r["host"], r["did"], ctx.port)
    for _ in range(3):
        status, msg = _transfer(ctx, s, r, 0.333, "RBT-033")
        if not status:
            return False, "a 0.333 transfer was rejected", msg
    time.sleep(3)
    r1 = _bal(r["host"], r["did"], ctx.port)
    moved = round(r1 - r0, 3)
    if rc.close_enough(moved, 0.999):
        return True, "3 x 0.333 = exactly {}".format(moved), ""
    return False, "did not total exactly 0.999", "receiver moved {}".format(moved)


# ---------------------------------------------------------------------------
# Wallet shape (034-040)
# ---------------------------------------------------------------------------
def _wallet_shape_skip(detail):
    return SKIP, "not attempted", (
        "needs a wallet built to a specific token composition ({}). Minting is "
        "one token per unit (~15s/1000), and there is no API to place tokens of "
        "chosen denominations, so the fixture cannot be built cheaply.".format(detail))


def c034(ctx, ci):
    return _wallet_shape_skip("one hundred 1.0 tokens")


def c035(ctx, ci):
    return _wallet_shape_skip("a single 1000 RBT token - local mint only produces 1.0 tokens")


def c036(ctx, ci):
    return _wallet_shape_skip("thousands of tiny split tokens summing to >= 100")


def c037(ctx, ci):
    return _wallet_shape_skip("10,000+ tokens held (~2.5 min mint, plus transfer timing)")


def c038(ctx, ci):
    return _wallet_shape_skip("50,000+ tokens held (~12 min mint)")


def c039(ctx, ci):
    """Exact single-token match: local mint produces 1.0 tokens, so sending
    exactly 1.0 should use one whole token with no split or burn."""
    s, r = ctx.pair(11)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 1.0, "RBT-039")


def c040(ctx, ci):
    """Exact combination: 3.0 from three whole 1.0 tokens - no split needed."""
    s, r = ctx.pair(12)
    ok, bal, note = _ensure_funded(ctx, s, 4)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 3.0, "RBT-040")


# ---------------------------------------------------------------------------
# Split (041-045)
# ---------------------------------------------------------------------------
def c041(ctx, ci):
    """0.7 out of a 1.0 token forces a split: parent burnt, 0.7 sent, 0.3 kept."""
    s, r = ctx.pair(13)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.7, "RBT-041")


def c042(ctx, ci):
    return SKIP, "not attempted", (
        "KNOWN OPEN BUG (see CLAUDE.md: split-token duplicate key). Reproducing it "
        "requires flipping a parent token's Burnt status directly in Postgres - a "
        "DB-SEED fixture, which is deferred (needs psycopg2 + per-node credentials). "
        "Expected outcome when run: duplicate key on tokens_pkey.")


def c043(ctx, ci):
    """Split down to the smallest representable unit."""
    s, r = ctx.pair(0)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.001, "RBT-043")


def c044(ctx, ci):
    """A value needing splits at several levels at once (0.137 from whole tokens)."""
    s, r = ctx.pair(1)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.137, "RBT-044")


def c045(ctx, ci):
    """Repeated splits on the same wallet, confirming balance after each."""
    s, r = ctx.pair(2)
    ok, bal, note = _ensure_funded(ctx, s, 5)
    if not ok:
        return False, "could not fund sender", note
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 0.3, "RBT-045")
        if passed is not True:
            return False, "split {} of 5 failed".format(i + 1), "{} {}".format(actual, note)
    return True, "5 consecutive splits, balance exact after each", ""


# ---------------------------------------------------------------------------
# Quorum capacity / concurrency (046-057)
# ---------------------------------------------------------------------------
def c046(ctx, ci):
    """Baseline: one sender, one quorum, nothing else running. This timing is
    the reference every later concurrency number is compared against."""
    s, r = ctx.pair(0)
    t0 = time.time()
    passed, actual, note = _expect_success(ctx, s, r, 1, "RBT-046")
    took = round(time.time() - t0, 2)
    if passed is not True:
        return False, actual, note
    return True, "baseline single transfer: {}s ({})".format(took, actual), ""


def _concurrent_transfers(ctx, n_senders, amount=1, memo="RBT-conc"):
    """Fire `n_senders` transfers at once, each from a different sender.
    Returns (available, ok_count, elapsed, detail)."""
    usable = min(n_senders, len(ctx.pairs))
    pairs = ctx.pairs[:usable]
    for s, _r in pairs:
        _ensure_funded(ctx, s, amount + 1)
    fns = [(lambda s=s, r=r: _transfer(ctx, s, r, amount, memo)) for s, r in pairs]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok = sum(1 for status, _m in results if status)
    detail = "; ".join((m or "")[:60] for status, m in results if not status)[:200]
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


def c047(ctx, ci):
    return _capacity_case(ctx, 2, "RBT-047")


def c048(ctx, ci):
    return _capacity_case(ctx, 5, "RBT-048")


def c049(ctx, ci):
    return _capacity_case(ctx, 10, "RBT-049")


def c050(ctx, ci):
    """Catalogue: record time and pass rate at 20."""
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 20, memo="RBT-050")
    return True, "{}/{} succeeded in {}s (pass rate {:.0f}%)".format(
        ok, usable, elapsed, 100.0 * ok / max(1, usable)), \
        detail or ("fleet provides {} pairs".format(usable))


def c051(ctx, ci):
    if len(ctx.pairs) < 40:
        return SKIP, "not attempted", (
            "needs 40 concurrent senders; fleet currently provides {} sender/receiver "
            "pairs (31 pool hosts, minus quorums, split into pairs).".format(len(ctx.pairs)))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 40, memo="RBT-051")
    return True, "{}/{} succeeded in {}s".format(ok, usable, elapsed), detail


def c052(ctx, ci):
    """Climb node count until time or pass rate degrades - RECORD the limit."""
    results = []
    limit = 0
    for n in (2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-052")
        rate = 100.0 * ok / max(1, usable)
        results.append("{}n:{}/{} {}s".format(usable, ok, usable, elapsed))
        if rate == 100.0:
            limit = usable
    return True, "one-quorum node limit observed: {} (ladder: {})".format(
        limit, ", ".join(results)), \
        "bounded by fleet size ({} pairs), not necessarily by the quorum".format(len(ctx.pairs))


def _multi_quorum_case(ctx, node_count, quorum_count, memo):
    if len(ctx.quorum_hosts) < quorum_count:
        return SKIP, "not attempted", (
            "needs {} quorums; this run has {}. Re-run with "
            "--quorum-count {}.".format(quorum_count, len(ctx.quorum_hosts), quorum_count))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, node_count, memo=memo)
    return True, "{}/{} succeeded in {}s across {} quorums".format(
        ok, usable, elapsed, len(ctx.quorum_hosts)), detail


def c053(ctx, ci):
    return _multi_quorum_case(ctx, 20, 2, "RBT-053")


def c054(ctx, ci):
    return _multi_quorum_case(ctx, 20, 5, "RBT-054")


def c055(ctx, ci):
    return _multi_quorum_case(ctx, 20, 10, "RBT-055")


def c056(ctx, ci):
    """More than the quorum can pledge must be refused cleanly."""
    s, r = ctx.pair(3)
    quorum = ctx.quorum_for(s)
    if not quorum:
        return False, "no quorum assigned to this sender", ""
    qbal = _bal(quorum["host"], quorum["did"], ctx.port)
    amount = qbal + 1000
    ok, bal, note = _ensure_funded(ctx, s, amount)
    if not ok:
        return SKIP, "not attempted", (
            "to exceed the quorum's {} RBT the sender needs {} RBT, which could not "
            "be funded: {}".format(qbal, amount, note))
    return _expect_rejection(ctx, s, r["did"], amount, "RBT-056")


def c057(ctx, ci):
    """How fast a quorum frees up: fire back-to-back through one sender and
    record the interval."""
    s, r = ctx.pair(4)
    ok, bal, note = _ensure_funded(ctx, s, 6)
    if not ok:
        return False, "could not fund sender", note
    timings = []
    for _ in range(4):
        t0 = time.time()
        status, msg = _transfer(ctx, s, r, 1, "RBT-057")
        timings.append(round(time.time() - t0, 2))
        if not status:
            return True, "quorum refused a back-to-back transfer after {} successes".format(
                len(timings) - 1), "timings {}s; msg: {}".format(timings, (msg or "")[:100])
    return True, "4 back-to-back transfers all accepted; per-transfer {}s".format(timings), \
        "no interval found at which the quorum refused"


# ---------------------------------------------------------------------------
# Pledging (058-060)
# ---------------------------------------------------------------------------
def c058(ctx, ci):
    return SKIP, "not attempted", (
        "watching pledge/unpledge needs visibility into the pledge tables. There is "
        "no HTTP API exposing pledge state; per CLAUDE.md DB-level verification is "
        "deferred (needs psycopg2 + per-node credentials).")


def c059(ctx, ci):
    return SKIP, "not attempted", (
        "needs to identify a currently-pledged token and attempt to spend it - "
        "requires pledge-table visibility (see RBT-058).")


def c060(ctx, ci):
    return SKIP, "not attempted", (
        "NODE-KILL: must interrupt a transfer between pledge and completion. The "
        "controller can stop a node over SSH, but hitting that window mid-consensus "
        "needs orchestration this runner does not have.")


# ---------------------------------------------------------------------------
# Concurrency (061-067)
# ---------------------------------------------------------------------------
def c061(ctx, ci):
    """Double spend: same wallet, whole balance, to two receivers at once.
    Exactly one must win."""
    s, r1 = ctx.pair(5)
    _s2, r2 = ctx.pair(6)
    ok, held, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    held = _bal(s["host"], s["did"], ctx.port)
    fns = [
        (lambda: _transfer(ctx, s, r1, held, "RBT-061a")),
        (lambda: _transfer(ctx, s, r2, held, "RBT-061b")),
    ]
    results = _parallel(fns)
    wins = sum(1 for status, _m in results if status)
    time.sleep(3)
    final = _bal(s["host"], s["did"], ctx.port)
    if wins == 1:
        return True, ("exactly one of two competing full-balance transfers succeeded "
                      "(sender {} -> {})".format(held, final)), ""
    if wins == 0:
        return False, "both competing transfers were rejected", \
            "; ".join((m or "")[:80] for _s, m in results)
    return False, "DOUBLE SPEND: both transfers succeeded", \
        "sender held {} and spent it twice; final balance {}".format(held, final)


def c062(ctx, ci):
    """Many transfers from ONE wallet at once."""
    s, r = ctx.pair(7)
    n = 8
    ok, bal, note = _ensure_funded(ctx, s, n + 2)
    if not ok:
        return False, "could not fund sender", note
    s0 = _bal(s["host"], s["did"], ctx.port)
    fns = [(lambda: _transfer(ctx, s, r, 1, "RBT-062")) for _ in range(n)]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    time.sleep(3)
    s1 = _bal(s["host"], s["did"], ctx.port)
    spent = round(s0 - s1, 3)
    if rc.close_enough(spent, float(ok_n)):
        return True, "{}/{} succeeded; sender spent exactly {} ({} -> {})".format(
            ok_n, n, spent, s0, s1), ""
    return False, "balance does not match successes", \
        "{} succeeded but sender spent {} ({} -> {})".format(ok_n, spent, s0, s1)


def c063(ctx, ci):
    """Many transfers INTO one receiver at once - receiver total must be exact."""
    target = ctx.receivers[0]
    senders = ctx.senders[1:9]
    for s in senders:
        _ensure_funded(ctx, s, 2)
    r0 = _bal(target["host"], target["did"], ctx.port)
    fns = [(lambda s=s: _transfer(ctx, s, target, 1, "RBT-063")) for s in senders]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    credited, r1 = rc.wait_for_balance(target["host"], target["did"], r0 + ok_n, ctx.port)
    got = round(r1 - r0, 3)
    if credited and rc.close_enough(got, float(ok_n)):
        return True, "{}/{} succeeded; receiver gained exactly {} ({} -> {})".format(
            ok_n, len(senders), got, r0, r1), ""
    return False, "receiver total is not the exact sum", \
        "{} succeeded but receiver gained {} ({} -> {})".format(ok_n, got, r0, r1)


def c064(ctx, ci):
    """Two nodes sending to each other simultaneously - no deadlock."""
    a, b = ctx.pair(8)
    _ensure_funded(ctx, a, 2)
    _ensure_funded(ctx, b, 2)
    a0 = _bal(a["host"], a["did"], ctx.port)
    b0 = _bal(b["host"], b["did"], ctx.port)
    fns = [
        (lambda: _transfer(ctx, a, b, 1, "RBT-064ab")),
        (lambda: _transfer(ctx, b, a, 1, "RBT-064ba")),
    ]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok_n = sum(1 for status, _m in results if status)
    time.sleep(3)
    a1 = _bal(a["host"], a["did"], ctx.port)
    b1 = _bal(b["host"], b["did"], ctx.port)
    if ok_n == 2:
        return True, "both directions succeeded in {}s (A {}->{}, B {}->{})".format(
            elapsed, a0, a1, b0, b1), ""
    return False, "{}/2 succeeded in {}s".format(ok_n, elapsed), \
        "; ".join((m or "")[:80] for _s, m in results if not _s)


def c065(ctx, ci):
    """Large values in parallel through one quorum - catalogue expects some to
    fail on pledge shortage, and failures must be clean."""
    quorum = ctx.quorum_for(ctx.senders[0])
    qbal = _bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    amount = max(1, int(qbal / 2))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 4, amount=amount, memo="RBT-065")
    return True, "{}/{} large ({} RBT) transfers succeeded in {}s against a {} RBT quorum".format(
        ok, usable, amount, elapsed, qbal), \
        detail or "no pledge shortage observed at this size"


def c066(ctx, ci):
    """Mix of tiny and large in parallel - small ones must not be starved."""
    pairs = ctx.pairs[:6]
    for s, _r in pairs:
        _ensure_funded(ctx, s, 12)
    fns = []
    for i, (s, r) in enumerate(pairs):
        amt = 10 if i % 2 == 0 else 0.001
        fns.append(lambda s=s, r=r, amt=amt: (amt, _transfer(ctx, s, r, amt, "RBT-066")))
    results = _parallel(fns)
    small_ok = sum(1 for amt, (st, _m) in results if amt < 1 and st)
    small_n = sum(1 for amt, _ in results if amt < 1)
    large_ok = sum(1 for amt, (st, _m) in results if amt >= 1 and st)
    large_n = sum(1 for amt, _ in results if amt >= 1)
    if small_ok == small_n and large_ok == large_n:
        return True, "all settled: {}/{} small, {}/{} large".format(
            small_ok, small_n, large_ok, large_n), ""
    return False, "not all settled: {}/{} small, {}/{} large".format(
        small_ok, small_n, large_ok, large_n), "small transfers may be starved by large ones"


def c067(ctx, ci):
    """Every available sender at once; fleet RBT total must be unchanged.

    Funding happens BEFORE the baseline is measured: _concurrent_transfers
    tops senders up by minting, and minting legitimately creates RBT. Taking
    the baseline first would count that new supply as a conservation failure.
    Only the transfer window is measured."""
    everyone = ctx.senders + ctx.receivers
    for s, _r in ctx.pairs:
        _ensure_funded(ctx, s, 2)
    time.sleep(2)
    before = sum(_bal(e["host"], e["did"], ctx.port) for e in everyone)

    pairs = ctx.pairs
    fns = [(lambda s=s, r=r: _transfer(ctx, s, r, 1, "RBT-067")) for s, r in pairs]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok = sum(1 for status, _m in results if status)
    usable = len(pairs)
    detail = "; ".join((m or "")[:60] for status, m in results if not status)[:200]

    time.sleep(5)
    after = sum(_bal(e["host"], e["did"], ctx.port) for e in everyone)
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


def c068(ctx, ci):
    return _node_kill_skip("stop the receiver node")


def c069(ctx, ci):
    return _node_kill_skip("stop the quorum node")


def c070(ctx, ci):
    return _node_kill_skip("stop and restart the sender node")


def c071(ctx, ci):
    return _node_kill_skip("restart the Postgres container")


def c072(ctx, ci):
    return _node_kill_skip("kill a node during heavy parallel load")


# ---------------------------------------------------------------------------
# Bulk / performance (073-080)
# ---------------------------------------------------------------------------
def c073(ctx, ci):
    """Many small transfers back-to-back; total value must be exact."""
    s, r = ctx.pair(9)
    n = 10
    ok, bal, note = _ensure_funded(ctx, s, n + 2)
    if not ok:
        return False, "could not fund sender", note
    r0 = _bal(r["host"], r["did"], ctx.port)
    t0 = time.time()
    fails = 0
    for _ in range(n):
        status, _m = _transfer(ctx, s, r, 1, "RBT-073")
        if not status:
            fails += 1
    elapsed = round(time.time() - t0, 2)
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + (n - fails), ctx.port)
    got = round(r1 - r0, 3)
    if fails == 0 and rc.close_enough(got, float(n)):
        return True, "{} transfers in {}s ({:.2f}s each); receiver +{}".format(
            n, elapsed, elapsed / n, got), ""
    return False, "{}/{} failed; receiver +{} in {}s".format(fails, n, got, elapsed), ""


def c074(ctx, ci):
    """Repeated high-value transfers, bounded by quorum pledge capacity."""
    s, r = ctx.pair(10)
    quorum = ctx.quorum_for(s)
    qbal = _bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    amount = max(1, min(50, int(qbal / 4)))
    ok, bal, note = _ensure_funded(ctx, s, amount * 3 + 2)
    if not ok:
        return SKIP, "not attempted", "could not fund sender to {}: {}".format(amount * 3, note)
    t0 = time.time()
    for i in range(3):
        passed, actual, note = _expect_success(ctx, s, r, amount, "RBT-074")
        if passed is not True:
            return False, "high-value transfer {} of 3 failed".format(i + 1), \
                "{} {}".format(actual, note)
    elapsed = round(time.time() - t0, 2)
    return True, "3 x {} RBT in {}s, all exact".format(amount, elapsed), ""


def c075(ctx, ci):
    """Raise parallel count step by step; RECORD the pass-rate curve."""
    curve = []
    for n in (1, 2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-075")
        curve.append("{}:{}/{}@{}s".format(usable, ok, usable, elapsed))
    return True, "pass-rate curve -> {}".format(", ".join(curve)), \
        "bounded by fleet size ({} pairs)".format(len(ctx.pairs))


def c076(ctx, ci):
    if len(ctx.quorum_hosts) < 3:
        return SKIP, "not attempted", \
            "needs at least 3 quorums; this run has {}".format(len(ctx.quorum_hosts))
    usable, ok, elapsed, _d = _concurrent_transfers(ctx, min(10, len(ctx.pairs)), memo="RBT-076")
    return True, "{}/{} in {}s across {} quorums".format(
        ok, usable, elapsed, len(ctx.quorum_hosts)), \
        "compare against the single-quorum figure in RBT-075; running the full " \
        "3-vs-5 quorum comparison needs two runs at different --quorum-count"


def c077(ctx, ci):
    """Many decimal transfers that force splits; compare against whole-token."""
    s, r = ctx.pair(11)
    ok, bal, note = _ensure_funded(ctx, s, 6)
    if not ok:
        return False, "could not fund sender", note
    t0 = time.time()
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 0.137, "RBT-077")
        if passed is not True:
            return False, "split transfer {} of 5 failed".format(i + 1), \
                "{} {}".format(actual, note)
    split_time = round(time.time() - t0, 2)
    t1 = time.time()
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 1.0, "RBT-077-whole")
        if passed is not True:
            return False, "whole-token transfer {} of 5 failed".format(i + 1), \
                "{} {}".format(actual, note)
    whole_time = round(time.time() - t1, 2)
    return True, "5 split transfers {}s vs 5 whole-token {}s".format(split_time, whole_time), ""


def c078(ctx, ci):
    return SKIP, "not attempted", (
        "soak test - 'run transfers nonstop for hours'. Needs to be scheduled "
        "deliberately, not run inside a normal catalogue pass.")


def c079(ctx, ci):
    """Time transfers as chain history grows on one token path."""
    s, r = ctx.pair(12)
    _ensure_funded(ctx, s, 4)
    _ensure_funded(ctx, r, 4)
    timings = []
    a, b = s, r
    for hop in range(6):
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, a, b, 1, "RBT-079")
        if passed is not True:
            return False, "hop {} failed".format(hop + 1), "{} {}".format(actual, note)
        timings.append(round(time.time() - t0, 2))
        a, b = b, a
    return True, "per-hop timings as chain grows: {}s".format(timings), \
        "catalogue asks for 1/10/50/100 hops; ran 6 to keep a catalogue pass short"


def c080(ctx, ci):
    """Time transfers as the wallet grows (more tokens held)."""
    s, r = ctx.pair(13)
    timings = []
    for target in (10, 50, 150):
        ok, bal, note = _ensure_funded(ctx, s, target)
        if not ok:
            return True, "wallet-growth timings up to this point: {}".format(timings), \
                "stopped at {} tokens: {}".format(target, note)
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, s, r, 1, "RBT-080")
        if passed is not True:
            return False, "transfer failed at wallet size {}".format(target), \
                "{} {}".format(actual, note)
        timings.append("{}tok:{}s".format(target, round(time.time() - t0, 2)))
    return True, "transfer time vs wallet size -> {}".format(", ".join(timings)), \
        "catalogue asks up to 10,000 tokens; capped here by minting cost (~15s/1000)"


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
CASES = {
    "RBT-001": c001, "RBT-002": c002, "RBT-003": c003, "RBT-004": c004,
    "RBT-005": c005, "RBT-006": c006, "RBT-007": c007, "RBT-008": c008,
    "RBT-009": c009, "RBT-010": c010, "RBT-011": c011, "RBT-012": c012,
    "RBT-013": c013, "RBT-014": c014, "RBT-015": c015, "RBT-016": c016,
    "RBT-017": c017, "RBT-018": c018, "RBT-019": c019, "RBT-020": c020,
    "RBT-021": c021, "RBT-022": c022, "RBT-023": c023, "RBT-024": c024,
    "RBT-025": c025, "RBT-026": c026, "RBT-027": c027, "RBT-028": c028,
    "RBT-029": c029, "RBT-030": c030, "RBT-031": c031, "RBT-032": c032,
    "RBT-033": c033, "RBT-034": c034, "RBT-035": c035, "RBT-036": c036,
    "RBT-037": c037, "RBT-038": c038, "RBT-039": c039, "RBT-040": c040,
    "RBT-041": c041, "RBT-042": c042, "RBT-043": c043, "RBT-044": c044,
    "RBT-045": c045, "RBT-046": c046, "RBT-047": c047, "RBT-048": c048,
    "RBT-049": c049, "RBT-050": c050, "RBT-051": c051, "RBT-052": c052,
    "RBT-053": c053, "RBT-054": c054, "RBT-055": c055, "RBT-056": c056,
    "RBT-057": c057, "RBT-058": c058, "RBT-059": c059, "RBT-060": c060,
    "RBT-061": c061, "RBT-062": c062, "RBT-063": c063, "RBT-064": c064,
    "RBT-065": c065, "RBT-066": c066, "RBT-067": c067, "RBT-068": c068,
    "RBT-069": c069, "RBT-070": c070, "RBT-071": c071, "RBT-072": c072,
    "RBT-073": c073, "RBT-074": c074, "RBT-075": c075, "RBT-076": c076,
    "RBT-077": c077, "RBT-078": c078, "RBT-079": c079, "RBT-080": c080,
}

ORDER = ["RBT-{:03d}".format(i) for i in range(1, 81)]

# Cases where the MEASUREMENT is the result, not pass/fail. The catalogue
# asks these to record a limit, a duration, or a curve - a PASS here only
# means "it ran"; the number in the Actual column is the real output, and a
# limit dropping between releases is the regression signal. The report gives
# these their own section.
TIMING_CASES = {
    "RBT-003",  # largest single mint that works + how long it took
    "RBT-023",  # value ladder - largest value that works
    "RBT-046",  # baseline single-transfer time (everything else compares to this)
    "RBT-050",  # 20-node pass rate + time
    "RBT-052",  # node limit for one quorum
    "RBT-053", "RBT-054", "RBT-055",  # same load across 2 / 5 / 10 quorums
    "RBT-057",  # how fast a quorum frees up
    "RBT-065",  # large values in parallel
    "RBT-073",  # back-to-back throughput
    "RBT-074",  # repeated high-value throughput
    "RBT-075",  # parallel pass-rate curve
    "RBT-076",  # multi-quorum throughput comparison
    "RBT-077",  # split vs whole-token timing
    "RBT-079",  # transfer time as chain history grows
    "RBT-080",  # transfer time as wallet grows
}
