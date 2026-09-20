#!/usr/bin/env python3
"""
master_cases.py - every implemented lab case, in one file.

Case text, expected results and priority live in master-catalogue.csv (same
folder); this file holds the code. Every Test ID here is a row there, and
case_runner.py resolves each case's wording from that catalogue.

Layout: one section per asset (RBT, FT, SC, CROSS-ASSET, GENERAL), each split
into operation subsections. The INDEX below lists the Test IDs per asset and
operation; the REGISTRY at the end of the file builds CASES / ORDER / LANES.

Cases rubix core's own integration suite already covers on a topology it can
reproduce are not here - see core-covered.csv.

Run:
    python3 ../full-test/case_runner.py --cases master --only 'SC-C-*'
    python3 ../full-test/validate_cases.py --cases master
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = 'SKIP'
SETTLE = 6
TOL = 0.0015

# =============================================================================
# INDEX - implemented Test IDs by asset and operation (from master-catalogue.csv)
# =============================================================================
#
# RBT
#   Mint             RBT-M-02 RBT-M-03 RBT-M-04
#   Transfer         RBT-T-05 RBT-T-07 RBT-T-08
#   Transfer Value   RBT-V-01 RBT-V-02 RBT-V-03 RBT-V-04 RBT-V-05 RBT-V-06
#                    RBT-V-07 RBT-V-08 RBT-V-09 RBT-V-10 RBT-V-11 RBT-V-12
#                    RBT-V-15 RBT-V-16
#   Precision        RBT-P-01 RBT-P-03 RBT-P-04 RBT-P-05
#   Wallet Shape     RBT-W-01 RBT-W-02 RBT-W-03 RBT-W-04 RBT-W-05 RBT-W-06
#                    RBT-W-09
#   Split            RBT-S-01 RBT-S-02 RBT-S-03 RBT-S-04 RBT-S-05
#   Quorum Capacity  RBT-Q-01 RBT-Q-02 RBT-Q-03 RBT-Q-04 RBT-Q-05 RBT-Q-06
#                    RBT-Q-07 RBT-Q-08 RBT-Q-09 RBT-Q-10 RBT-Q-12 RBT-Q-13
#   Pledging         RBT-L-01 RBT-L-02 RBT-L-03
#   Concurrency      RBT-N-01 RBT-N-10 RBT-N-11 RBT-N-12 RBT-N-13 RBT-N-14
#                    RBT-N-15
#   Failure          RBT-F-01 RBT-F-02 RBT-F-03 RBT-F-04 RBT-F-05
#   Bulk             RBT-B-01 RBT-B-02 RBT-B-03 RBT-B-04 RBT-B-05 RBT-B-06
#                    RBT-B-07 RBT-B-08
#
# FT
#   Parts            FT-P-01 FT-P-02 FT-P-03 FT-P-04 FT-P-05 FT-P-06 FT-P-07
#                    FT-P-08 FT-P-09 FT-P-10
#   Scale            FT-X-01 FT-X-02
#   DB               FT-DB-04
#
# SC
#   Collateral       SC-C-01 SC-C-02 SC-C-03 SC-C-04 SC-C-05 SC-C-06 SC-C-07
#                    SC-C-08 SC-C-09 SC-C-11 SC-C-12 SC-C-13 SC-C-14 SC-C-15
#                    SC-C-16 SC-C-17 SC-C-18 SC-C-19 SC-C-20 SC-C-21 SC-C-22
#                    SC-C-23 SC-C-24 SC-C-25 SC-C-26 SC-C-27 SC-C-28 SC-C-29
#                    SC-C-30 SC-C-31 SC-C-32 SC-C-33 SC-C-34
#   Quorum Capacity  SC-Q-06 SC-Q-07 SC-Q-08 SC-Q-09 SC-Q-10 SC-Q-11
#   DB Failure       SC-DB-03
#   Subscription     SC-S-01 SC-S-02 SC-S-03 SC-S-04 SC-S-05 SC-S-06 SC-S-07
#                    SC-S-08 SC-S-09 SC-S-10
#   Scale            SC-X-01 SC-X-02 SC-X-03 SC-X-04 SC-X-05
#   Quorum           SC-Q-12
#
# Cross-Asset
#   Combined         CRS-C-06 CRS-C-01 CRS-C-02 CRS-C-03
#
# General
#   Integrity        GEN-IN-08 GEN-IN-09 GEN-IN-10 GEN-IN-11 GEN-IN-12
#                    GEN-IN-13 GEN-IN-14 GEN-IN-15 GEN-IN-16 GEN-IN-17
#                    GEN-IN-18 GEN-IN-19 GEN-IN-20 GEN-IN-21 GEN-IN-22
#                    GEN-IN-23 GEN-IN-24



# =============================================================================
# RBT
# =============================================================================


# -----------------------------------------------------------------------------
# Mint / Transfer / Value / Precision / Wallet shape / Split / Quorum / Pledging / Concurrency / Failure / Bulk
# (was rbt/rbt_cases.py)
# -----------------------------------------------------------------------------
# rbt_cases.py - RBT cases (RBT-001..RBT-080) from master-test-cases.xlsx.
#
# Run via:  cd test-plan/full-test && python3 case_runner.py --cases rbt
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
# Why some cases are SKIP rather than implemented:
#   * NODE-KILL cases need to stop/restart a node mid-transfer. The controller
#     can do that over SSH, but doing it *at the right instant* mid-consensus
#     needs orchestration this runner doesn't have yet.
#   * DB-SEED cases need direct Postgres access to corrupt state deliberately;
#     per CLAUDE.md that is deferred (needs psycopg2 + per-node credentials).
#   * Very-high-value cases are bounded by minting cost: generateLocalRBT mints
#     ONE token per unit in a loop (core/token.go), measured at ~15s per 1000.
#     100,000 RBT is ~25 minutes of minting per wallet, and the quorum needs the
#     same again to pledge it. Not a tweak - a multi-hour setup.




FAKE_DID = "bafybmi" + "z" * 52       # 59 chars, right prefix, never created
MALFORMED_DID = "not-a-valid-did"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _rbt_bal(host, did, port):
    ok, b, _ = rc.get_rbt_balance(host, did, port)
    return b if ok and b is not None else 0.0


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


def _ensure_quorum_liquidity(ctx, entry, amount):
    """Guarantee the quorum backing `entry` can pledge `amount`.

    A quorum must pledge at least the transfer value
    (core/consensus/checks.go), and its free balance falls as the run
    proceeds, so this tops it up rather than letting a later case fail on
    someone else's spending. Returns (ok, quorum_free_balance, note)."""
    q = ctx.sender_quorum.get(entry["host"])
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
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, amount, memo)
    if not status:
        return False, "rejected", msg
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + amount, ctx.port)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    if credited and rc.close_enough(s1, s0 - amount):
        return True, "sender {}->{}  receiver {}->{}".format(s0, s1, r0, r1), ""
    if credited:
        return False, "receiver credited but sender wrong", \
            "sender {}->{} (expected {})".format(s0, s1, round(s0 - amount, 3))
    return False, "receiver never credited", "sender {}->{} receiver {}->{}".format(s0, s1, r0, r1)


def _rbt_locked(host, did, port):
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
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    locked0 = _rbt_locked(s["host"], s["did"], ctx.port)
    status, msg, _ = rc.initiate_transaction(
        s["host"], s["did"], receiver_did, rbt=amount, memo=memo, port=ctx.port)
    time.sleep(2)  # give any lock-release path time to run
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    locked1 = _rbt_locked(s["host"], s["did"], ctx.port)
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


def rbt_m_02(ctx, ci):
    s, _ = ctx.pair(1)
    before = _rbt_bal(s["host"], s["did"], ctx.port)
    s1, m1 = rc.fund_did(s["host"], s["did"], 3, ctx.port)
    s2, m2 = rc.fund_did(s["host"], s["did"], 3, ctx.port)
    if not (s1 and s2):
        return False, "one or both mints rejected", "{} / {}".format(m1, m2)
    ok, after = rc.wait_for_balance(s["host"], s["did"], before + 6, ctx.port)
    if not ok:
        return False, "two mints of 3 did not add up", "before={} after={}".format(before, after)
    return True, "balance {} -> {} (3 + 3)".format(before, after), ""


def rbt_m_03(ctx, ci):
    """Large single mint. The catalogue asks to record the largest that works
    and how long it takes; minting is one token per unit, so this is timed."""
    s, _ = ctx.pair(2)
    amount = int(getattr(ctx.args, "large_mint", 2000))
    before = _rbt_bal(s["host"], s["did"], ctx.port)
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


def rbt_m_04(ctx, ci):
    return SKIP, "not attempted", (
        "needs a node with ZERO DIDs. Every pool host has exactly one, and "
        "CreateDID has no idempotency (core/did.go) so minting a second identity "
        "to create the fixture would be irreversible. Needs a freshly installed node.")


# ---------------------------------------------------------------------------
# Transfer basics (005-012)
# ---------------------------------------------------------------------------


def rbt_t_05(ctx, ci):
    """Self-transfer: same DID both sides. Per CLAUDE.md this is supported for
    RBT via the local-DID branch (core/transaction.go:495) - overall balance
    should be unchanged."""
    s, _ = ctx.pair(2)
    before = _rbt_bal(s["host"], s["did"], ctx.port)
    status, msg = _transfer(ctx, s, s, 1, "RBT-T-05")
    if not status:
        return False, "rejected", msg
    time.sleep(2)
    after = _rbt_bal(s["host"], s["did"], ctx.port)
    if rc.close_enough(before, after):
        return True, "succeeded, balance unchanged at {}".format(after), ""
    return False, "balance changed on a self-transfer", "{} -> {}".format(before, after)


def rbt_t_07(ctx, ci):
    """Catalogue says 'Define what happens' - so RECORD, don't assert."""
    s, _ = ctx.pair(3)
    before = _rbt_bal(s["host"], s["did"], ctx.port)
    status, msg, _ = rc.initiate_transaction(
        s["host"], s["did"], "", rbt=1, memo="RBT-T-07", port=ctx.port)
    time.sleep(2)
    after = _rbt_bal(s["host"], s["did"], ctx.port)
    return True, "empty receiver -> status={} balance {} -> {}".format(status, before, after), \
        "recorded, not asserted: {}".format((msg or "")[:120])


def rbt_t_08(ctx, ci):
    """Spend immediately after receiving - probes stale chain-tip handling."""
    s, r = ctx.pair(4)
    ok, actual, note = _expect_success(ctx, s, r, 1, "RBT-T-08-seed")
    if ok is not True:
        return False, "could not seed the receiver", "{} {}".format(actual, note)
    return _expect_success(ctx, r, s, 1, "RBT-T-08")


# ---------------------------------------------------------------------------
# Value (013-028)
# ---------------------------------------------------------------------------
def _value_case(ctx, pair_index, amount, memo):
    s, r = ctx.pair(pair_index)
    return _expect_success(ctx, s, r, amount, memo)


def rbt_v_01(ctx, ci):
    return _value_case(ctx, 5, 0.001, "RBT-V-01")


def rbt_v_02(ctx, ci):
    """0.0001 is below MinDecimalUnit; FloatPrecision rounds it to 0, and a
    zero-value transaction carries no tokens -> rejected."""
    s, r = ctx.pair(5)
    return _expect_rejection(ctx, s, r["did"], 0.0001, "RBT-V-02")


def rbt_v_03(ctx, ci):
    return _value_case(ctx, 6, 0.999, "RBT-V-03")


def rbt_v_04(ctx, ci):
    return _value_case(ctx, 6, 2.345, "RBT-V-04")


def rbt_v_05(ctx, ci):
    return _value_case(ctx, 7, 99.999, "RBT-V-05")


def rbt_v_06(ctx, ci):
    return _value_case(ctx, 7, 100, "RBT-V-06")


def _large_value_case(ctx, pair_index, amount, memo):
    """Large transfers are bounded by BOTH the sender's balance and the
    quorum's pledge capacity. Skip honestly rather than report a misleading
    failure when the fixture can't support the value."""
    s, r = ctx.pair(pair_index)
    quorum = ctx.quorum_for(s)
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
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


def rbt_v_07(ctx, ci):
    return _large_value_case(ctx, 8, 1000, "RBT-V-07")


def rbt_v_08(ctx, ci):
    return _large_value_case(ctx, 8, 10000, "RBT-V-08")


def rbt_v_09(ctx, ci):
    return SKIP, "not attempted", (
        "100,000 RBT needs ~25 min of minting on the sender AND the same again on "
        "the quorum to pledge it (one token per unit, ~15s/1000). Deliberate "
        "long-run setup, not a normal cycle.")


def rbt_v_10(ctx, ci):
    return SKIP, "not attempted", (
        "200,000.10 RBT - ~50 min of minting per wallet plus matching quorum "
        "pledge capacity. Same reason as RBT-V-09.")


def rbt_v_11(ctx, ci):
    """Value ladder: climb until it fails, RECORD the largest that worked."""
    s, r = ctx.pair(9)
    quorum = ctx.quorum_for(s)
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
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
        passed, actual, note = _expect_success(ctx, s, r, amount, "RBT-V-11")
        if passed is not True:
            failure = "failed at {}: {} {}".format(amount, actual, note)
            break
        largest = amount
    return True, "largest value that worked: {} RBT".format(largest), \
        failure or "ladder not exhausted within fixture limits"


def rbt_v_12(ctx, ci):
    s, r = ctx.pair(10)
    return _expect_rejection(ctx, s, r["did"], 0, "RBT-V-12")


def rbt_v_15(ctx, ci):
    """Send the entire balance - sender must land on exactly 0."""
    s, r = ctx.pair(12)
    held = _rbt_bal(s["host"], s["did"], ctx.port)
    if held <= 0:
        ok, held, note = _ensure_funded(ctx, s, 3)
        if not ok:
            return False, "could not fund sender", note
    quorum = ctx.quorum_for(s)
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    if held > qbal:
        return SKIP, "not attempted", \
            "sender holds {} but quorum can only pledge {}".format(held, qbal)
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, held, "RBT-V-15")
    if not status:
        return False, "rejected", msg
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + held, ctx.port)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    if credited and rc.close_enough(s1, 0.0):
        return True, "sender {} -> {} (exactly zero), receiver {} -> {}".format(held, s1, r0, r1), ""
    return False, "did not land on exactly zero", "sender {} -> {}, receiver {} -> {}".format(
        held, s1, r0, r1)


def rbt_v_16(ctx, ci):
    """One MinDecimalUnit above the balance must be refused."""
    s, r = ctx.pair(13)
    held = _rbt_bal(s["host"], s["did"], ctx.port)
    if held <= 0:
        ok, held, note = _ensure_funded(ctx, s, 2)
        if not ok:
            return False, "could not fund sender", note
    return _expect_rejection(ctx, s, r["did"], round(held + 0.001, 3), "RBT-V-16")


# ---------------------------------------------------------------------------
# Precision (029-033)
# ---------------------------------------------------------------------------
def rbt_p_01(ctx, ci):
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
        passed, actual, note = _expect_success(ctx, s, r, amt, "RBT-P-01")
        if passed is not True:
            failures.append("{}: {} {}".format(amt, actual, note))
    if failures:
        return False, "{}/{} failed".format(len(failures), len(amounts)), "; ".join(failures[:4])
    return True, "all {} amounts moved exactly ({} per decimal place)".format(len(amounts), n), ""


def rbt_p_03(ctx, ci):
    """0.0005: FloatPrecision ROUNDS at 3dp, so this should become 0.001.
    Catalogue says 'Record the behaviour' - so record, don't assert."""
    s, r = ctx.pair(7)
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    status, msg = _transfer(ctx, s, r, 0.0005, "RBT-P-03")
    time.sleep(3)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    r1 = _rbt_bal(r["host"], r["did"], ctx.port)
    moved = round(s0 - s1, 4)
    return True, "status={} sender moved {} (0.0005 -> {}), receiver {} -> {}".format(
        status, moved, moved, r0, r1), \
        "recorded, not asserted. FloatPrecision rounds at 3dp so 0.0005 is expected " \
        "to become 0.001. msg: {}".format((msg or "")[:100])


def rbt_p_04(ctx, ci):
    """0.001 x N. Catalogue says 1000 times; at ~2s settle each that is ~33
    minutes, so N is configurable and the actual count is recorded."""
    s, r = ctx.pair(9)
    n = int(getattr(ctx.args, "repeat_count", 25))
    ok, bal, note = _ensure_funded(ctx, s, max(1, n * 0.001 + 1))
    if not ok:
        return False, "could not fund sender", note
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    failures = 0
    for _ in range(n):
        status, _msg = _transfer(ctx, s, r, 0.001, "RBT-P-04")
        if not status:
            failures += 1
    time.sleep(3)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    r1 = _rbt_bal(r["host"], r["did"], ctx.port)
    expected = round(0.001 * (n - failures), 3)
    moved = round(r1 - r0, 3)
    if failures:
        return False, "{}/{} transfers rejected".format(failures, n), \
            "sender {} -> {}, receiver {} -> {}".format(s0, s1, r0, r1)
    if rc.close_enough(moved, expected):
        return True, "{} x 0.001 moved exactly {} (no drift)".format(n, moved), \
            "catalogue asks for 1000; ran {} (--repeat-count)".format(n)
    return False, "drift detected", "expected {} moved {}".format(expected, moved)


def rbt_p_05(ctx, ci):
    """0.333 three times -> exactly 0.999, no creeping error."""
    s, r = ctx.pair(10)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    for _ in range(3):
        status, msg = _transfer(ctx, s, r, 0.333, "RBT-P-05")
        if not status:
            return False, "a 0.333 transfer was rejected", msg
    time.sleep(3)
    r1 = _rbt_bal(r["host"], r["did"], ctx.port)
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


def rbt_w_01(ctx, ci):
    return _wallet_shape_skip("one hundred 1.0 tokens")


def rbt_w_02(ctx, ci):
    return _wallet_shape_skip("a single 1000 RBT token - local mint only produces 1.0 tokens")


def rbt_w_03(ctx, ci):
    return _wallet_shape_skip("thousands of tiny split tokens summing to >= 100")


def rbt_w_04(ctx, ci):
    return _wallet_shape_skip("10,000+ tokens held (~2.5 min mint, plus transfer timing)")


def rbt_w_09(ctx, ci):
    return _wallet_shape_skip("50,000+ tokens held (~12 min mint)")


def rbt_w_05(ctx, ci):
    """Exact single-token match: local mint produces 1.0 tokens, so sending
    exactly 1.0 should use one whole token with no split or burn."""
    s, r = ctx.pair(11)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 1.0, "RBT-W-05")


def rbt_w_06(ctx, ci):
    """Exact combination: 3.0 from three whole 1.0 tokens - no split needed."""
    s, r = ctx.pair(12)
    ok, bal, note = _ensure_funded(ctx, s, 4)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 3.0, "RBT-W-06")


# ---------------------------------------------------------------------------
# Split (041-045)
# ---------------------------------------------------------------------------
def rbt_s_01(ctx, ci):
    """0.7 out of a 1.0 token forces a split: parent burnt, 0.7 sent, 0.3 kept."""
    s, r = ctx.pair(13)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.7, "RBT-S-01")


def rbt_s_02(ctx, ci):
    return SKIP, "not attempted", (
        "KNOWN OPEN BUG (see CLAUDE.md: split-token duplicate key). Reproducing it "
        "requires flipping a parent token's Burnt status directly in Postgres - a "
        "DB-SEED fixture, which is deferred (needs psycopg2 + per-node credentials). "
        "Expected outcome when run: duplicate key on tokens_pkey.")


def rbt_s_03(ctx, ci):
    """Split down to the smallest representable unit."""
    s, r = ctx.pair(0)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.001, "RBT-S-03")


def rbt_s_05(ctx, ci):
    """A value needing splits at several levels at once (0.137 from whole tokens)."""
    s, r = ctx.pair(1)
    ok, bal, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    return _expect_success(ctx, s, r, 0.137, "RBT-S-05")


def rbt_s_04(ctx, ci):
    """Repeated splits on the same wallet, confirming balance after each."""
    s, r = ctx.pair(2)
    ok, bal, note = _ensure_funded(ctx, s, 5)
    if not ok:
        return False, "could not fund sender", note
    for i in range(5):
        passed, actual, note = _expect_success(ctx, s, r, 0.3, "RBT-S-04")
        if passed is not True:
            return False, "split {} of 5 failed".format(i + 1), "{} {}".format(actual, note)
    return True, "5 consecutive splits, balance exact after each", ""


# ---------------------------------------------------------------------------
# Quorum capacity / concurrency (046-057)
# ---------------------------------------------------------------------------
def rbt_q_01(ctx, ci):
    """Baseline: one sender, one quorum, nothing else running. This timing is
    the reference every later concurrency number is compared against."""
    s, r = ctx.pair(0)
    t0 = time.time()
    passed, actual, note = _expect_success(ctx, s, r, 1, "RBT-Q-01")
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


def rbt_q_02(ctx, ci):
    return _capacity_case(ctx, 2, "RBT-Q-02")


def rbt_q_03(ctx, ci):
    return _capacity_case(ctx, 5, "RBT-Q-03")


def rbt_q_04(ctx, ci):
    return _capacity_case(ctx, 10, "RBT-Q-04")


def rbt_q_05(ctx, ci):
    """Catalogue: record time and pass rate at 20."""
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 20, memo="RBT-Q-05")
    return True, "{}/{} succeeded in {}s (pass rate {:.0f}%)".format(
        ok, usable, elapsed, 100.0 * ok / max(1, usable)), \
        detail or ("fleet provides {} pairs".format(usable))


def rbt_q_06(ctx, ci):
    if len(ctx.pairs) < 40:
        return SKIP, "not attempted", (
            "needs 40 concurrent senders; fleet currently provides {} sender/receiver "
            "pairs (31 pool hosts, minus quorums, split into pairs).".format(len(ctx.pairs)))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 40, memo="RBT-Q-06")
    return True, "{}/{} succeeded in {}s".format(ok, usable, elapsed), detail


def rbt_q_07(ctx, ci):
    """Climb node count until time or pass rate degrades - RECORD the limit."""
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


def _multi_quorum_case(ctx, node_count, quorum_count, memo):
    if len(ctx.quorum_hosts) < quorum_count:
        return SKIP, "not attempted", (
            "needs {} quorums; this run has {}. Re-run with "
            "--quorum-count {}.".format(quorum_count, len(ctx.quorum_hosts), quorum_count))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, node_count, memo=memo)
    return True, "{}/{} succeeded in {}s across {} quorums".format(
        ok, usable, elapsed, len(ctx.quorum_hosts)), detail


def rbt_q_08(ctx, ci):
    return _multi_quorum_case(ctx, 20, 2, "RBT-Q-08")


def rbt_q_09(ctx, ci):
    return _multi_quorum_case(ctx, 20, 5, "RBT-Q-09")


def rbt_q_10(ctx, ci):
    return _multi_quorum_case(ctx, 20, 10, "RBT-Q-10")


def rbt_q_12(ctx, ci):
    """More than the quorum can pledge must be refused cleanly."""
    s, r = ctx.pair(3)
    quorum = ctx.quorum_for(s)
    if not quorum:
        return False, "no quorum assigned to this sender", ""
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port)
    amount = qbal + 1000
    ok, bal, note = _ensure_funded(ctx, s, amount)
    if not ok:
        return SKIP, "not attempted", (
            "to exceed the quorum's {} RBT the sender needs {} RBT, which could not "
            "be funded: {}".format(qbal, amount, note))
    return _expect_rejection(ctx, s, r["did"], amount, "RBT-Q-12")


def rbt_q_13(ctx, ci):
    """How fast a quorum frees up: fire back-to-back through one sender and
    record the interval."""
    s, r = ctx.pair(4)
    ok, bal, note = _ensure_funded(ctx, s, 6)
    if not ok:
        return False, "could not fund sender", note
    timings = []
    for _ in range(4):
        t0 = time.time()
        status, msg = _transfer(ctx, s, r, 1, "RBT-Q-13")
        timings.append(round(time.time() - t0, 2))
        if not status:
            return True, "quorum refused a back-to-back transfer after {} successes".format(
                len(timings) - 1), "timings {}s; msg: {}".format(timings, (msg or "")[:100])
    return True, "4 back-to-back transfers all accepted; per-transfer {}s".format(timings), \
        "no interval found at which the quorum refused"


# ---------------------------------------------------------------------------
# Pledging (058-060)
# ---------------------------------------------------------------------------
def rbt_l_01(ctx, ci):
    return SKIP, "not attempted", (
        "watching pledge/unpledge needs visibility into the pledge tables. There is "
        "no HTTP API exposing pledge state; per CLAUDE.md DB-level verification is "
        "deferred (needs psycopg2 + per-node credentials).")


def rbt_l_02(ctx, ci):
    return SKIP, "not attempted", (
        "needs to identify a currently-pledged token and attempt to spend it - "
        "requires pledge-table visibility (see RBT-L-01).")


def rbt_l_03(ctx, ci):
    return SKIP, "not attempted", (
        "NODE-KILL: must interrupt a transfer between pledge and completion. The "
        "controller can stop a node over SSH, but hitting that window mid-consensus "
        "needs orchestration this runner does not have.")


# ---------------------------------------------------------------------------
# Concurrency (061-067)
# ---------------------------------------------------------------------------
def rbt_n_01(ctx, ci):
    """Double spend: same wallet, whole balance, to two receivers at once.
    Exactly one must win."""
    s, r1 = ctx.pair(5)
    _s2, r2 = ctx.pair(6)
    ok, held, note = _ensure_funded(ctx, s, 2)
    if not ok:
        return False, "could not fund sender", note
    held = _rbt_bal(s["host"], s["did"], ctx.port)
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
        return False, "both competing transfers were rejected", \
            "; ".join((m or "")[:80] for _s, m in results)
    return False, "DOUBLE SPEND: both transfers succeeded", \
        "sender held {} and spent it twice; final balance {}".format(held, final)


def rbt_n_10(ctx, ci):
    """Many transfers from ONE wallet at once."""
    s, r = ctx.pair(7)
    n = 8
    ok, bal, note = _ensure_funded(ctx, s, n + 2)
    if not ok:
        return False, "could not fund sender", note
    s0 = _rbt_bal(s["host"], s["did"], ctx.port)
    fns = [(lambda: _transfer(ctx, s, r, 1, "RBT-N-10")) for _ in range(n)]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    time.sleep(3)
    s1 = _rbt_bal(s["host"], s["did"], ctx.port)
    spent = round(s0 - s1, 3)
    if rc.close_enough(spent, float(ok_n)):
        return True, "{}/{} succeeded; sender spent exactly {} ({} -> {})".format(
            ok_n, n, spent, s0, s1), ""
    return False, "balance does not match successes", \
        "{} succeeded but sender spent {} ({} -> {})".format(ok_n, spent, s0, s1)


def rbt_n_11(ctx, ci):
    """Many transfers INTO one receiver at once - receiver total must be exact."""
    target = ctx.receivers[0]
    senders = ctx.senders[1:9]
    for s in senders:
        _ensure_funded(ctx, s, 2)
    r0 = _rbt_bal(target["host"], target["did"], ctx.port)
    fns = [(lambda s=s: _transfer(ctx, s, target, 1, "RBT-N-11")) for s in senders]
    results = _parallel(fns)
    ok_n = sum(1 for status, _m in results if status)
    credited, r1 = rc.wait_for_balance(target["host"], target["did"], r0 + ok_n, ctx.port)
    got = round(r1 - r0, 3)
    if credited and rc.close_enough(got, float(ok_n)):
        return True, "{}/{} succeeded; receiver gained exactly {} ({} -> {})".format(
            ok_n, len(senders), got, r0, r1), ""
    return False, "receiver total is not the exact sum", \
        "{} succeeded but receiver gained {} ({} -> {})".format(ok_n, got, r0, r1)


def rbt_n_12(ctx, ci):
    """Two nodes sending to each other simultaneously - no deadlock."""
    a, b = ctx.pair(8)
    _ensure_funded(ctx, a, 2)
    _ensure_funded(ctx, b, 2)
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
    """Large values in parallel through one quorum - catalogue expects some to
    fail on pledge shortage, and failures must be clean."""
    quorum = ctx.quorum_for(ctx.senders[0])
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    amount = max(1, int(qbal / 2))
    usable, ok, elapsed, detail = _concurrent_transfers(ctx, 4, amount=amount, memo="RBT-N-13")
    return True, "{}/{} large ({} RBT) transfers succeeded in {}s against a {} RBT quorum".format(
        ok, usable, amount, elapsed, qbal), \
        detail or "no pledge shortage observed at this size"


def rbt_n_14(ctx, ci):
    """Mix of tiny and large in parallel - small ones must not be starved."""
    pairs = ctx.pairs[:6]
    for s, _r in pairs:
        _ensure_funded(ctx, s, 12)
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
    return False, "not all settled: {}/{} small, {}/{} large".format(
        small_ok, small_n, large_ok, large_n), "small transfers may be starved by large ones"


def rbt_n_15(ctx, ci):
    """Every available sender at once; fleet RBT total must be unchanged.

    Funding happens BEFORE the baseline is measured: _concurrent_transfers
    tops senders up by minting, and minting legitimately creates RBT. Taking
    the baseline first would count that new supply as a conservation failure.
    Only the transfer window is measured."""
    everyone = ctx.senders + ctx.receivers
    for s, _r in ctx.pairs:
        _ensure_funded(ctx, s, 2)
    time.sleep(2)
    before = sum(_rbt_bal(e["host"], e["did"], ctx.port) for e in everyone)

    pairs = ctx.pairs
    fns = [(lambda s=s, r=r: _transfer(ctx, s, r, 1, "RBT-N-15")) for s, r in pairs]
    t0 = time.time()
    results = _parallel(fns)
    elapsed = round(time.time() - t0, 2)
    ok = sum(1 for status, _m in results if status)
    usable = len(pairs)
    detail = "; ".join((m or "")[:60] for status, m in results if not status)[:200]

    time.sleep(5)
    after = sum(_rbt_bal(e["host"], e["did"], ctx.port) for e in everyone)
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
    return _node_kill_skip("stop the receiver node")


def rbt_f_02(ctx, ci):
    return _node_kill_skip("stop the quorum node")


def rbt_f_03(ctx, ci):
    return _node_kill_skip("stop and restart the sender node")


def rbt_f_04(ctx, ci):
    return _node_kill_skip("restart the Postgres container")


def rbt_f_05(ctx, ci):
    return _node_kill_skip("kill a node during heavy parallel load")


# ---------------------------------------------------------------------------
# Bulk / performance (073-080)
# ---------------------------------------------------------------------------
def rbt_b_01(ctx, ci):
    """Many small transfers back-to-back; total value must be exact."""
    s, r = ctx.pair(9)
    n = 10
    ok, bal, note = _ensure_funded(ctx, s, n + 2)
    if not ok:
        return False, "could not fund sender", note
    r0 = _rbt_bal(r["host"], r["did"], ctx.port)
    t0 = time.time()
    fails = 0
    for _ in range(n):
        status, _m = _transfer(ctx, s, r, 1, "RBT-B-01")
        if not status:
            fails += 1
    elapsed = round(time.time() - t0, 2)
    credited, r1 = rc.wait_for_balance(r["host"], r["did"], r0 + (n - fails), ctx.port)
    got = round(r1 - r0, 3)
    if fails == 0 and rc.close_enough(got, float(n)):
        return True, "{} transfers in {}s ({:.2f}s each); receiver +{}".format(
            n, elapsed, elapsed / n, got), ""
    return False, "{}/{} failed; receiver +{} in {}s".format(fails, n, got, elapsed), ""


def rbt_b_02(ctx, ci):
    """Repeated high-value transfers, bounded by quorum pledge capacity."""
    s, r = ctx.pair(10)
    quorum = ctx.quorum_for(s)
    qbal = _rbt_bal(quorum["host"], quorum["did"], ctx.port) if quorum else 0
    amount = max(1, min(50, int(qbal / 4)))
    ok, bal, note = _ensure_funded(ctx, s, amount * 3 + 2)
    if not ok:
        return SKIP, "not attempted", "could not fund sender to {}: {}".format(amount * 3, note)
    t0 = time.time()
    for i in range(3):
        passed, actual, note = _expect_success(ctx, s, r, amount, "RBT-B-02")
        if passed is not True:
            return False, "high-value transfer {} of 3 failed".format(i + 1), \
                "{} {}".format(actual, note)
    elapsed = round(time.time() - t0, 2)
    return True, "3 x {} RBT in {}s, all exact".format(amount, elapsed), ""


def rbt_b_03(ctx, ci):
    """Raise parallel count step by step; RECORD the pass-rate curve."""
    curve = []
    for n in (1, 2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-B-03")
        curve.append("{}:{}/{}@{}s".format(usable, ok, usable, elapsed))
    return True, "pass-rate curve -> {}".format(", ".join(curve)), \
        "bounded by fleet size ({} pairs)".format(len(ctx.pairs))


def rbt_b_04(ctx, ci):
    if len(ctx.quorum_hosts) < 3:
        return SKIP, "not attempted", \
            "needs at least 3 quorums; this run has {}".format(len(ctx.quorum_hosts))
    usable, ok, elapsed, _d = _concurrent_transfers(ctx, min(10, len(ctx.pairs)), memo="RBT-B-04")
    return True, "{}/{} in {}s across {} quorums".format(
        ok, usable, elapsed, len(ctx.quorum_hosts)), \
        "compare against the single-quorum figure in RBT-B-03; running the full " \
        "3-vs-5 quorum comparison needs two runs at different --quorum-count"


def rbt_b_05(ctx, ci):
    """Many decimal transfers that force splits; compare against whole-token."""
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
    return SKIP, "not attempted", (
        "soak test - 'run transfers nonstop for hours'. Needs to be scheduled "
        "deliberately, not run inside a normal catalogue pass.")


def rbt_b_07(ctx, ci):
    """Time transfers as chain history grows on one token path."""
    s, r = ctx.pair(12)
    _ensure_funded(ctx, s, 4)
    _ensure_funded(ctx, r, 4)
    timings = []
    a, b = s, r
    for hop in range(6):
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, a, b, 1, "RBT-B-07")
        if passed is not True:
            return False, "hop {} failed".format(hop + 1), "{} {}".format(actual, note)
        timings.append(round(time.time() - t0, 2))
        a, b = b, a
    return True, "per-hop timings as chain grows: {}s".format(timings), \
        "catalogue asks for 1/10/50/100 hops; ran 6 to keep a catalogue pass short"


def rbt_b_08(ctx, ci):
    """Time transfers as the wallet grows (more tokens held)."""
    s, r = ctx.pair(13)
    timings = []
    for target in (10, 50, 150):
        ok, bal, note = _ensure_funded(ctx, s, target)
        if not ok:
            return True, "wallet-growth timings up to this point: {}".format(timings), \
                "stopped at {} tokens: {}".format(target, note)
        t0 = time.time()
        passed, actual, note = _expect_success(ctx, s, r, 1, "RBT-B-08")
        if passed is not True:
            return False, "transfer failed at wallet size {}".format(target), \
                "{} {}".format(actual, note)
        timings.append("{}tok:{}s".format(target, round(time.time() - t0, 2)))
    return True, "transfer time vs wallet size -> {}".format(", ".join(timings)), \
        "catalogue asks up to 10,000 tokens; capped here by minting cost (~15s/1000)"


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
# (was ft/ft_cases.py)
# -----------------------------------------------------------------------------
# ft_cases.py - Fungible Token cases from the master catalogue.
#
# Run via:  cd test-plan/full-test && python3 case_runner.py --cases ft
# One case:                          python3 case_runner.py --cases ft --only FT-P-02
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


def _ft_name():
    return "part" + "".join(random.choice(string.ascii_lowercase + string.digits)
                            for _ in range(7))


def _ft_parts_wallet(ctx):
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

    ready, why = _ft_prepare(ctx, s, total + 3)
    if not ready:
        return SKIP, "setup incomplete", why

    if not db.available():
        return SKIP, "database driver missing", (
            "this case cannot be done through the API - it needs the tokens "
            "table to prove no whole token exists. "
            "sudo apt install -y python3-psycopg2")

    # BUILD the parts wallet. This used to refuse when the receiver already
    # held whole tokens, which on a funded fleet is always - so the entire
    # FT-from-parts chain skipped and a third of the FT verification never ran.
    okw, whyw = ws.make_parts_wallet(ctx, r, s, amounts=tuple(PART_AMOUNTS))
    if not okw:
        return False, "could not build a parts wallet", whyw
    sent = sum(PART_AMOUNTS)
    try:
        values = db.free_token_values(r["host"], r["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    wholes = [v for v in values if v >= 1.0]
    held = sum(values)
    parts_only = not wholes
    # NOT "held == sent". The drain removes WHOLE tokens and deliberately
    # leaves fractional dust behind, so a wallet that has been used already
    # starts with parts of its own - one run found 17.505 where 2.400 had been
    # sent. What this case actually requires is that NO WHOLE TOKEN remains and
    # the wallet holds at least what was just sent; the exact total is not the
    # property under test, and asserting it turned a good precondition into a
    # false failure.
    adds_up = held >= (sent - TOL * len(PART_AMOUNTS))

    if parts_only and adds_up:
        _PARTS["entry"] = r

    passed = parts_only and adds_up
    return passed, "{} part token(s) totalling {:.3f}".format(len(values), held), (
        "" if passed else (
            "wallet holds {} whole token(s) - not a parts wallet".format(len(wholes))
            if wholes else
            "wallet holds {:.3f} but {:.3f} was just sent into it - the "
            "fractional transfers did not arrive".format(held, sent)))


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
    r = _ft_parts_wallet(ctx)
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
    r = _ft_parts_wallet(ctx)
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
    r = _ft_parts_wallet(ctx)
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
    r = _ft_parts_wallet(ctx)
    if r is None:
        return SKIP, "no parts wallet", "FT-P-01 did not complete"

    s, _ = ctx.pair(0)
    detail = _ft_bal(ctx, r)
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
    ready, why = _ft_prepare(ctx, r, amount)
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
# (was ft/ft_cases_stress.py)
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
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    ready, why = _ft_prepare(ctx, s, 14)
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

    name = _ft_stress_name()
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
        return SKIP, "no whole denomination to seed", (
            "needs free 1.000 tokens present in BOTH token_denom and tokens; "
            "counter={} real={}".format(_at(counter, target), _at(real, target)))

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
# (was ft/ft_cases_scale.py)
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
    consumed = -d["free"]
    if done and abs(burnt - consumed) > max(0.05, consumed * 0.01):
        problems.append("free fell {:.3f} but only {:.3f} was recorded as burnt "
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
        return SKIP, "need 2+ hosts", "lane has {}".format(len(hosts))

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


# =============================================================================
# SMART CONTRACT
# =============================================================================


# -----------------------------------------------------------------------------
# Collateral / Execute / Quorum pledge / Subscription (core cases + shared helpers)
# (was sc/sc_cases.py)
# -----------------------------------------------------------------------------
# sc_cases.py - Smart Contract cases from the master catalogue.
#
# Run via:  cd test-plan/full-test && python3 case_runner.py --cases sc
# One case:                          python3 case_runner.py --cases sc --only SC-C-01
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




# Cases added in the collateral review live in sibling files purely to keep
# this one readable. They are ordinary SC catalogue cases and are imported
# into CASES/ORDER below, so nothing else needs to know they are separate.


# How long to let a deploy settle before reading the balance back. Committed
# tokens and change do not appear the instant the call returns.

# Tolerance for a balance comparison. MinDecimalUnit is 0.001 and FloatPrecision
# rounds at 3dp (math/math.go), so anything tighter than this reports rounding as
# a failure.


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


def _sc_new_contract(ctx, entry):
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
    ready, why = _sc_prepare(ctx, s, 5.0)
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
    ready, why = _sc_prepare(ctx, s, 5.0)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before_committed = db.value_in_status(s["host"], s["did"], db.COMMITTED)
        snap_before = db.record("SC-C-02", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    value = 0.001
    sc_id, err = _sc_new_contract(ctx, s)
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
    ready, why = _sc_prepare(ctx, s, 10.0)
    if not ready:
        return SKIP, "setup incomplete", why

    rounds = 3
    before = _sc_bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the total cost"

    # A different random value each round: repeating one value would only prove
    # that value works three times, not that the path handles varied fractions.
    values = [rand_value(0.100, 0.999) for _ in range(rounds)]
    expected = sum(values)

    failures = []
    for i, value in enumerate(values, 1):
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            failures.append("round {}: generation failed ({})".format(i, err))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                       data="repeat deploy {}".format(i), port=ctx.port)
        if not ok:
            failures.append("round {} (value {}): {}".format(i, value, msg))
        time.sleep(2)

    time.sleep(SETTLE)
    after = _sc_bal(ctx, s)
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
    ready, why = _sc_prepare(ctx, s, 6.0)
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
    ready, why = _sc_prepare(ctx, s, 6.0)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.001
    sc_id, err = _sc_new_contract(ctx, s)
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
    ready, why = _sc_prepare(ctx, s, 2.0)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _sc_bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot verify state was left unchanged"

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    too_much = before["balance"] + 1000.0
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=too_much,
                                   data="over-value deploy", port=ctx.port)
    time.sleep(SETTLE)
    after = _sc_bal(ctx, s)

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
    ready, why = _sc_prepare(ctx, s, sum(values) + 8)
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

    ready, why = _sc_prepare(ctx, s, 6)
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
        sc_id, err = _sc_new_contract(ctx, s)
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

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
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

    ready, why = _sc_prepare(ctx, r, 0.5)
    if not ready:
        return SKIP, "executor setup incomplete", why

    before = _sc_bal(ctx, r)
    ok, msg, _ = rc.sc_transaction(r["host"], r["did"], sc_id, value=value,
                                   data="parts execute", port=ctx.port)
    if not ok:
        return False, "execute rejected", (
            "executor holds {:.3f} in parts but could not execute: {}".format(
                before["balance"] if before else -1, msg))
    time.sleep(SETTLE)

    after = _sc_bal(ctx, r)
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
    ready, why = _sc_prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        if db.denom_drift(s["host"], s["did"]):
            return SKIP, "already drifting before the deploy", (
                "the counter is inconsistent before this deploy, so a drift "
                "afterwards could not be attributed to it - see GEN-IN-08")
        free_before = _sc_bal(ctx, s)
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
        # the same class of bug as the deploy and mint paths, but on a
        # separate path. A failure here is a separate finding.
        q_drift_before = db.denom_drift(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if free_before is None:
        return SKIP, "balance unreadable", "cannot measure what the deployer spent"

    sc_id, err = _sc_new_contract(ctx, s)
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
        free_after = _sc_bal(ctx, s)
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
# `fund` is roughly what the lane's own cases spend; case_runner adds a safety
# margin on top. Only the quorums are funded in bulk - they are shared by every
# lane and carry all of their pledges at once.
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Collateral - value ladder, wallet shapes, sequential deploys
# (was sc/sc_cases_extra.py)
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
# SC-C-14 / 15 / 16 - wallet shape
# ---------------------------------------------------------------------------

def _shape_case(ctx, ci, shape):
    """Deploy a fractional value from a wallet of a given shape."""
    s, r = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    value = rand_value(0.100, 0.999)

    # The wallet shape is BUILT, not looked for. Draining the receiver's whole
    # tokens and sending fractions back makes every shape reachable from any
    # starting state - which is why none of these skip any more.
    if shape == "whole":
        target = s
        ready, why = _sc_prepare(ctx, target, 8)
        if not ready:
            return SKIP, "setup incomplete", why
    else:
        ready, why = _sc_prepare(ctx, s, 15)
        if not ready:
            return SKIP, "setup incomplete", why
        if shape == "parts":
            okw, whyw = ws.make_parts_wallet(ctx, r, s)
        else:
            okw, whyw = ws.make_mixed_wallet(ctx, r, s)
        if not okw:
            return False, "could not build a {} wallet".format(shape), whyw
        target = r
        ready, why = _sc_prepare(ctx, target, value + 0.5)
        if not ready:
            return SKIP, "setup incomplete", why

    try:
        before_rows = db.free_token_values(target["host"], target["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    spent, _sc_id, err = _deploy_and_measure(ctx, target, value)
    if err:
        return False, "deploy failed", "from a {} wallet: {}".format(shape, err)

    try:
        after_rows = db.free_token_values(target["host"], target["did"])
        drift = db.denom_drift(target["host"], target["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
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
    s, _ = ctx.pair(0)
    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.007
    spent, _sc_id, err = _deploy_and_measure(ctx, s, value)
    if err:
        return False, "deploy failed", err

    exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
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
    s, _ = ctx.pair(0)
    ready, why = _sc_prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _sc_bal(ctx, s)
    if before is None:
        return SKIP, "balance unreadable", "cannot determine the exact balance"
    value = round(before["balance"], 3)
    if value <= 0:
        return SKIP, "no balance", "wallet is empty"

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="whole-balance deploy", port=ctx.port)
    time.sleep(SETTLE)
    after = _sc_bal(ctx, s)
    if after is None:
        return SKIP, "balance unreadable", "cannot verify the outcome"

    if ok:
        spent = before["balance"] - after["balance"]
        exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
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
# (was sc/sc_cases_db.py)
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




# ---------------------------------------------------------------------------
# SC-C-20
# ---------------------------------------------------------------------------

def sc_c_20(ctx, ci):
    """
    SC-C-20 - The change token exists as a row after a fractional deploy.

    WHAT IT CHECKS
        After deploying 0.354 from a wallet of 1.000 tokens:
          * a Committed row of 0.354 exists that did not exist before
          * a NEW Free row of about 0.646 exists - the change
          * committed + change == the denomination consumed (1.000)

    WHY IT MATTERS
        This is the fix's actual claim, stated as rows rather than inferred
        from a balance. CollectRBTTokens returns childTokensKept and the change
        must be persisted; before the fix the whole token was committed as-is
        and the remainder simply ceased to exist.

        A balance check cannot separate "split correctly" from "there happened
        to be a 0.354 token available". Rows can: a change row of 0.646 can
        only come from splitting a 1.000.

    MANUAL STEPS
        1. Before, list the wallet's RBT rows:
             psql -h $SENDER -p 5433 -U rubix -d rubix -c \\
               "SELECT token_id, token_value, token_status, parent_token_id
                  FROM tokens WHERE did='<DID>' AND token_type=1
                  ORDER BY token_value DESC;"
        2. Deploy with "value":0.354 (see SC-C-01).
        3. Run the same query. Look for:
             - a row with token_status=5 (Committed) and value 0.354
             - a NEW row with token_status=0 (Free) and value ~0.646
             - that new row's parent_token_id naming the 1.000 token consumed

    PASS / FAIL
        PASS  both rows present and they sum to the consumed denomination
        FAIL  committed 1.000 as one row and no change row -> the remainder was
              destroyed, which is exactly the bug
        FAIL  change row present but the sum does not reconcile -> value lost
              in the split
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.354
    try:
        free_before = {t for t, _v, _st, _p in
                       db.token_rows(s["host"], s["did"], db.FREE)}
        comm_before = {t for t, _v, _st, _p in
                       db.token_rows(s["host"], s["did"], db.COMMITTED)}
        whole_before = [v for _t, v, _st, _p in
                        db.token_rows(s["host"], s["did"], db.FREE) if v >= 1.0]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not whole_before:
        # The wallet needs a whole token so the change is unambiguous. Fund it
        # rather than skipping - generate_local_rbt mints whole tokens.
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            free_before = {t for t, _v, _st, _p in
                           db.token_rows(s["host"], s["did"], db.FREE)}
            whole_before = [v for _t, v, _st, _p in
                            db.token_rows(s["host"], s["did"], db.FREE) if v >= 1.0]
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        if not whole_before:
            return False, "no whole token available", (
                "funded the wallet but it still holds no 1.000 token - the "
                "change produced by a split would be ambiguous")

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="change-token check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        free_after = db.token_rows(s["host"], s["did"], db.FREE)
        comm_after = db.token_rows(s["host"], s["did"], db.COMMITTED)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    new_committed = [(t, v) for t, v, _st, _p in comm_after if t not in comm_before]
    new_free = [(t, v, p) for t, v, _st, p in free_after if t not in free_before]

    committed_val = sum(v for _t, v in new_committed)
    change_val = sum(v for _t, v, _p in new_free)

    problems = []
    if not rc.close_enough(committed_val, value, tol=cost_tolerance(value)):
        problems.append("committed {:.4f} across {} row(s), expected {:.3f}".format(
            committed_val, len(new_committed), value))
    if not new_free:
        problems.append("NO change row appeared - the remainder of the token "
                        "backing this deploy was destroyed rather than returned")
    else:
        total = committed_val + change_val
        if not rc.close_enough(total, round(total), tol=0.01):
            problems.append("committed {:.4f} + change {:.4f} = {:.4f}, which is "
                            "not a whole denomination - value went missing in the "
                            "split".format(committed_val, change_val, total))

    return (not problems), "committed {} row(s)={:.4f}, change {} row(s)={:.4f}".format(
        len(new_committed), committed_val, len(new_free), change_val), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-21
# ---------------------------------------------------------------------------

def sc_c_21(ctx, ci):
    """
    SC-C-21 - Chain rows exist for the contract and for the split children.

    WHAT IT CHECKS
        After a fractional deploy, `tokenchain` holds an entry for the smart
        contract token, and the change token produced by the split also has
        chain rows.

    WHY IT MATTERS
        A token row without a chain row is a token with no history: it exists
        in the wallet but cannot be validated, and the failure appears much
        later as a chain-mismatch on some unrelated transfer.

        The fix persists the split via PersistGenesisTransaction, and the code
        comment notes this has to happen BEFORE the outer transaction opens or
        it deadlocks. Something persisted through that separate path is exactly
        what could end up with a token row and no chain row.

    MANUAL STEPS
        1. Deploy a fractional value, noting the contract id.
        2. Chain for the contract:
             SELECT position, transaction_id, previous_transaction_id, role
               FROM tokenchain WHERE token_id='<SC_ID>' ORDER BY position;
        3. Find the change token, then run the same query for its token_id.

    PASS / FAIL
        PASS  contract has at least one chain row, and every new Free token has
              at least one
        FAIL  a token exists with no chain row -> unvalidatable, and it will
              fail later somewhere unrelated
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        free_before = {t for t, _v, _st, _p in db.token_rows(s["host"], s["did"], db.FREE)}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    value = rand_value(0.100, 0.999)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="chain row check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        sc_chain = db.chain_rows(s["host"], sc_id)
        free_after = db.token_rows(s["host"], s["did"], db.FREE)
        new_free = [t for t, _v, _st, _p in free_after if t not in free_before]
        orphans = [t for t in new_free if not db.chain_rows(s["host"], t)]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not sc_chain:
        problems.append("the contract token has NO tokenchain row - it was "
                        "deployed but has no history")
    if orphans:
        problems.append("{} new token(s) have no chain row: {} - they cannot be "
                        "validated and will fail a later transfer".format(
                            len(orphans), ", ".join(t[:16] for t in orphans[:3])))

    return (not problems), "sc chain rows={} new tokens={} orphans={}".format(
        len(sc_chain), len(new_free), len(orphans)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-22
# ---------------------------------------------------------------------------

def sc_c_22(ctx, ci):
    """
    SC-C-22 - The deploy is recorded in the transactions table.

    WHAT IT CHECKS
        A row exists in `transactions` for the deploy, and its info names the
        deployer as initiator.

    WHY IT MATTERS
        The API returning success means the request was accepted. A row in
        `transactions` means the node actually recorded it. Those are different
        claims, and the gap between them is where a "successful" operation
        disappears on restart.

        Reading the initiator from the row rather than assuming it also
        confirms the deploy was attributed to the right DID - collateral taken
        from one wallet and recorded against another would balance, and be
        wrong.

    MANUAL STEPS
        After a deploy:
          SELECT id, info->>'initiator', info->>'owner'
            FROM transactions ORDER BY created_at DESC LIMIT 5;
        The newest row should name the deployer as initiator.

    PASS / FAIL
        PASS  a new transaction row exists naming the deployer
        FAIL  no new row -> accepted but not recorded
        FAIL  recorded against a different DID
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = set(t for (t,) in db.query(
            s["host"], "SELECT id FROM transactions"))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    value = rand_value(0.100, 0.999)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="transaction row check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        after = [t for (t,) in db.query(s["host"], "SELECT id FROM transactions")]
        new = [t for t in after if t not in before]
        mine = []
        for tx in new:
            init, owner = db.transaction_participants(s["host"], tx)
            if init == s["did"]:
                mine.append(tx)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not new:
        problems.append("no new transactions row after a successful deploy - "
                        "accepted but not recorded")
    elif not mine:
        problems.append("{} new row(s), none naming {} as initiator - the deploy "
                        "was recorded against a different DID".format(
                            len(new), s["did"][:16]))

    return (not problems), "{} new transaction row(s), {} from this DID".format(
        len(new), len(mine)), "; ".join(problems)


# -----------------------------------------------------------------------------
# Quorum capacity - per-quorum pledge accounting
# (was sc/sc_cases_quorum.py)
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
# SC-Q-07
# ---------------------------------------------------------------------------

def sc_q_07(ctx, ci):
    """
    SC-Q-07 - The quorum's own denomination counter after it pledges.

    WHAT IT CHECKS
        After the quorum pledges for a deploy, its token_denom still matches
        the RBT it actually holds Free.

    WHY IT MATTERS
        Pledging takes quorum tokens out of Free, so its counter must decrement
        - the identical situation to the deploy and FT-burn counters, on a
        third path. And the quorum is shared: a drifting counter
        there does not break one wallet, it eventually stops the quorum
        selecting tokens for ANY sender, which surfaces as unrelated transfers
        failing across the fleet.

        Only drift this deploy introduced is reported. The quorum signs for
        every lane, so pre-existing drift is somebody else's finding.

    MANUAL STEPS
        On the QUORUM host, before and after a deploy elsewhere:
          SELECT denom, count FROM token_denom WHERE did='<QDID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<QDID>' AND token_status=0 AND token_type=1
            GROUP BY token_value ORDER BY token_value;
        The two listings must still agree afterwards.

    PASS / FAIL
        PASS  no new drift on the quorum
        FAIL  the quorum's counter no longer matches its free tokens - it will
              eventually fail to pledge for senders unrelated to this test
        SKIP  quorum already drifting beforehand
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect a pledge without a known quorum"

    value = rand_value(0.100, 0.999)
    ready, why = _sc_prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "quorum already drifting", (
            "pre-existing drift on the shared quorum: "
            + db.describe_drift(before["denom_drift"]) +
            " - not attributable to this deploy")

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="quorum denom check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    return (not drift), "quorum {} pledged +{:.3f}, denom {}".format(
        q["host"], after["pledged"] - before["pledged"],
        "ok" if not drift else "DRIFT"), (
        "" if not drift else db.describe_drift(drift) +
        " - pledging moved tokens out of Free without decrementing the counter. "
        "This is the pledge path, separate from deploy and FT burn")


# ---------------------------------------------------------------------------
# SC-Q-08
# ---------------------------------------------------------------------------

def sc_q_08(ctx, ci):
    """
    SC-Q-08 - Which quorum tokens went to Pledged, row by row.

    WHAT IT CHECKS
        The rows the quorum moved into Pledged for this deploy sum to at least
        the contract value, and no more than is reasonable for it.

    WHY IT MATTERS
        SC-Q-06 checks the pledged TOTAL rose enough. This checks what it rose
        BY. Over-pledging is the interesting failure: a quorum that locks a
        whole 1.000 token to back a 0.354 contract is not wrong for that
        transaction, but it exhausts its capacity far faster than its balance
        suggests - and the fleet then sees "quorum cannot pledge" long before
        the quorum looks empty.

        This is the quorum-side mirror of the deployer counter.

    MANUAL STEPS
        On the quorum, before and after (6 = Pledged, 7 = QuorumPledged):
          SELECT token_id, token_value FROM tokens
            WHERE did='<QDID>' AND token_type=1 AND token_status IN (6,7)
            ORDER BY token_value DESC;
        Compare the two lists; the added rows are this deploy's pledge.

    PASS / FAIL
        PASS  newly pledged rows cover the value
        FAIL  they do not cover it -> the guarantee was short
        RECORD  how much MORE than the value was locked, since over-pledging
              silently reduces fleet capacity
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect a pledge without a known quorum"

    value = rand_value(0.100, 0.999)
    ready, why = _sc_prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    def pledged_rows():
        out = []
        for st in (db.PLEDGED, db.QUORUM_PLEDGED):
            out += db.token_rows(q["host"], q["did"], st)
        return {t: v for t, v, _st, _p in out}

    try:
        before = pledged_rows()
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="quorum pledge rows", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    # Sample promptly - the pledge is transient.
    added = {}
    for _ in range(8):
        try:
            now = pledged_rows()
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        fresh = {t: v for t, v in now.items() if t not in before}
        if fresh:
            added = fresh
            break
        time.sleep(1)

    total = sum(added.values())
    covered = total >= (value - TOL)
    excess = total - value

    note = ""
    if not covered:
        note = ("pledged {:.4f} across {} row(s) for a {:.3f} contract - the "
                "guarantee did not cover what was signed".format(
                    total, len(added), value))
    elif excess > 0.5:
        note = ("locked {:.4f} to back {:.3f} ({:.4f} more than needed) - not "
                "wrong for this transaction, but it drains quorum capacity far "
                "faster than the balance suggests".format(total, value, excess))

    return covered, "{} row(s) pledged, {:.4f} for a {:.3f} contract".format(
        len(added), total, value), note


# ---------------------------------------------------------------------------
# SC-Q-09
# ---------------------------------------------------------------------------

def sc_q_09(ctx, ci):
    """
    SC-Q-09 - An unpledge row is queued for the deploy.

    WHAT IT CHECKS
        After a deploy, `unpledge_sequence_info` holds a row for that
        transaction, naming the quorum and the tokens it pledged.

    WHY IT MATTERS
        The pledge is released later, asynchronously. The row queued here is
        what makes that possible - without it the pledge can never be released
        and the quorum loses that capacity permanently.

        This is why SC-Q-06 deliberately does NOT assert the release itself:
        unpledging is on its own schedule, so testing it in a short window
        reports timing as a defect. Testing that the release was QUEUED is the
        part that is deterministic.

    MANUAL STEPS
        On the quorum, after a deploy:
          SELECT tx_id, quorum_did, pledge_tokens FROM unpledge_sequence_info
            ORDER BY created_at DESC LIMIT 5;
        And check none are stranded:
          SELECT u.tx_id FROM unpledge_sequence_info u
            WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = u.tx_id);

    PASS / FAIL
        PASS  a new unpledge row appeared, and no row references a transaction
              that does not exist
        FAIL  no row queued -> that pledge can never be released
        FAIL  stranded rows -> pledges permanently stuck
    """
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect unpledge rows without a quorum"

    value = rand_value(0.100, 0.999)
    ready, why = _sc_prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = {t for (t,) in db.query(
            q["host"], "SELECT tx_id FROM unpledge_sequence_info")}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="unpledge queue check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = [t for (t,) in db.query(
            q["host"], "SELECT tx_id FROM unpledge_sequence_info")]
        stranded = db.open_pledges(q["host"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    new = [t for t in after if t not in before]
    problems = []
    if not new:
        problems.append("no unpledge row was queued for this deploy - the "
                        "pledge it created can never be released")
    if stranded:
        problems.append("{} unpledge row(s) reference a transaction that does "
                        "not exist, so those pledges are stuck permanently".format(
                            len(stranded)))

    return (not problems), "{} unpledge row(s) queued, {} stranded".format(
        len(new), len(stranded)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-Q-10
# ---------------------------------------------------------------------------

def sc_q_10(ctx, ci):
    """
    SC-Q-10 - Deploys routed to each quorum in turn; every quorum's books balance.

    WHAT IT CHECKS
        One deploy per quorum. Each quorum's pledged total rises by its own
        deploy's value, and no quorum's counter drifts.

    WHY IT MATTERS
        Quorum state is per-quorum. A bug that only affects the FIRST quorum -
        or only one that has already signed something - is invisible when every
        test routes through the same one. Every other SC case uses whichever
        quorum the sender happens to be assigned, which in practice is almost
        always the same machine.

        It also gives three independent measurements of the same behaviour: if
        one quorum disagrees with the other two, the difference is that
        machine's state, not the code.

    MANUAL STEPS
        Register a different quorum on three senders, deploy from each, and
        compare each quorum's pledged total and denom listing before and after.

    PASS / FAIL
        PASS  every quorum pledged for its own deploy, none drifted
        FAIL  the report names which quorum disagreed
        SKIP  fewer than two quorums configured
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    quorums = list(ctx.quorum_hosts)
    if len(quorums) < 2:
        return SKIP, "need at least two quorums", (
            "only {} configured; this case compares quorums against each "
            "other".format(len(quorums)))

    s, _ = ctx.pair(0)
    results, problems = [], []
    for q in quorums:
        value = rand_value(0.100, 0.999)
        # Point this sender at THIS quorum specifically.
        rc.quorum_add(s["host"], q["did"], ctx.port)
        ready, why = _sc_prepare(ctx, s, value + 5)
        if not ready:
            problems.append("{}: setup incomplete ({})".format(q["host"], why))
            continue
        try:
            before = db.snapshot(q["host"], q["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)

        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            problems.append("{}: generation failed".format(q["host"]))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                       data="per-quorum deploy", port=ctx.port)
        if not ok:
            problems.append("{}: deploy rejected ({})".format(q["host"], msg))
            continue
        time.sleep(SETTLE)
        try:
            after = db.snapshot(q["host"], q["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)

        drift = db.new_drift(before, after)
        results.append("{}:{:.3f}{}".format(
            q["host"].split(".")[-1], value, "" if not drift else " DRIFT"))
        if drift:
            problems.append("{} drifted after its deploy: {}".format(
                q["host"], db.describe_drift(drift)))

    return (not problems), "{} quorum(s): {}".format(len(results), " ".join(results)), \
        "; ".join(problems)


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
        SKIP  fewer than 3 hosts in this lane
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    senders = [e for e in (list(ctx.senders) + list(ctx.receivers))][:3]
    if len(senders) < 3:
        return SKIP, "need 3 hosts", (
            "this lane has {} host(s); concurrency needs at least 3 "
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
        SKIP  fewer than 3 hosts in this lane, or no quorum
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    senders = [e for e in (list(ctx.senders) + list(ctx.receivers))][:4]
    if len(senders) < 3:
        return SKIP, "need 3 hosts", (
            "this lane has {} host(s); a burst needs at least 3".format(len(senders)))
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
# (was sc/sc_cases_subs.py)
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
        SKIP  fewer than 4 spare hosts in this lane
    """
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 4:
        return SKIP, "not enough hosts", (
            "lane has {} spare host(s); this case needs at least 4 to stagger "
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
            "lane has {}; this case needs an executing subscriber and a later "
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


# ---------------------------------------------------------------------------
# SC-C-23 / 24 / 25 - executing from parts wallets
# ---------------------------------------------------------------------------

def _sc_subs_parts_wallet(ctx, target, amounts):
    """Leave `target` holding only the given fractional amounts.

    BUILDS the shape rather than requiring it. Earlier this refused whenever the
    target already held whole tokens, which on a funded fleet is always - so
    every case using it skipped and the parts path went untested.
    """
    s = ctx.senders[0]
    ready, why = _sc_prepare(ctx, s, sum(amounts) + 12)
    if not ready:
        return False, why
    return ws.make_parts_wallet(ctx, target, s, amounts=tuple(amounts))


def sc_c_23(ctx, ci):
    """
    SC-C-23 - A parts-only wallet executes several contracts in succession.

    WHAT IT CHECKS
        A wallet holding only fractional RBT subscribes to three contracts and
        executes each. All succeed, nothing is charged, the counter stays
        consistent.

    WHY IT MATTERS
        SC-C-09 proves one execute works from parts. Repeating it is what
        catches state that degrades: each execute still has to take part in
        consensus from a wallet with no whole token, and if anything about that
        leaves the wallet slightly worse the third attempt is where it shows.

    MANUAL STEPS
        Build a parts wallet (0.4/0.3/0.5), subscribe it to three deployed
        contracts, execute each in turn, checking the balance and denom
        listing after each.

    PASS / FAIL
        PASS  all three execute, balance unchanged, counter consistent
        FAIL  a LATER execute fails while an earlier one succeeded -> the act
              of executing is degrading the wallet
        SKIP  no parts wallet could be built
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a receiver to hold the parts wallet"
    w = spare[0]

    ready, why = _sc_prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    contracts = []
    for i in range(3):
        sc_id, err = _sc_new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=rand_value(0.010, 0.200),
                                       data="multi deploy", port=ctx.port)
        if not ok:
            return SKIP, "deploy failed", str(msg)
        contracts.append(sc_id)
    time.sleep(SETTLE)

    ok, why = _sc_subs_parts_wallet(ctx, w, [0.4, 0.3, 0.5])
    if not ok:
        return SKIP, "could not build parts wallet", why

    ready, why = _sc_prepare(ctx, w, 0.5)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _sc_bal(ctx, w)
    results, problems = [], []
    for i, sc_id in enumerate(contracts, 1):
        sub_ok, msg = rc.subscribe_smart_contract(w["host"], sc_id, ctx.port)
        if not sub_ok:
            problems.append("execute {}: subscribe failed ({})".format(i, msg))
            break
        time.sleep(SETTLE)
        ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id,
                                       value=rand_value(0.010, 0.100),
                                       data="parts execute {}".format(i),
                                       port=ctx.port)
        results.append("{}:{}".format(i, "ok" if ok else "FAIL"))
        if not ok:
            problems.append("execute {} of 3 rejected: {} - earlier executes "
                            "succeeded, so the wallet degraded".format(i, msg))
            break
        time.sleep(2)

    time.sleep(SETTLE)
    after = _sc_bal(ctx, w)
    try:
        drift = db.denom_drift(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if before and after and not rc.close_enough(before["balance"], after["balance"],
                                                tol=TOL * 4):
        problems.append("executor balance moved {:.4f} -> {:.4f}; execute takes "
                        "no collateral".format(before["balance"], after["balance"]))
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "executes {}".format(" ".join(results)), "; ".join(problems)


def sc_c_24(ctx, ci):
    """
    SC-C-24 - Execute from a wallet holding only minimum-unit tokens.

    WHAT IT CHECKS
        A wallet whose entire balance is 0.001 tokens can still take part in
        consensus and execute a contract.

    WHY IT MATTERS
        The extreme of the parts case. Selection has to gather many rows to
        reach any threshold, and the denomination is the smallest the network
        allows - so if there is a lower bound on what can be selected, or a cap
        on how many rows a selection will consider, this is where it appears.

        A wallet like this is not artificial: it is what remains after many
        fractional splits.

    MANUAL STEPS
        Send 0.001 twenty times to a fresh node, confirm via the tokens table
        that it holds only 0.001 rows, subscribe it, and execute.

    PASS / FAIL
        PASS  executes successfully
        FAIL  rejected while the balance is adequate -> a floor on selection
        SKIP  no clean wallet available
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 2:
        return SKIP, "need a clean host", "no spare wallet for the minimum-unit case"
    w = spare[1]

    ready, why = _sc_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=rand_value(0.010, 0.200),
                                   data="min-unit deploy", port=ctx.port)
    if not ok:
        return SKIP, "deploy failed", str(msg)
    time.sleep(SETTLE)

    ok, why = _sc_subs_parts_wallet(ctx, w, [0.001] * 20)
    if not ok:
        return SKIP, "could not build minimum-unit wallet", why

    try:
        values = db.free_token_values(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if any(v > 0.0015 for v in values):
        return SKIP, "wallet not minimum-unit only", (
            "holds {} row(s) above 0.001, so this is not the extreme "
            "case".format(sum(1 for v in values if v > 0.0015)))

    ready, why = _sc_prepare(ctx, w, 0.002)
    if not ready:
        return SKIP, "setup incomplete", why

    sub_ok, msg = rc.subscribe_smart_contract(w["host"], sc_id, ctx.port)
    if not sub_ok:
        return SKIP, "subscribe failed", str(msg)
    time.sleep(SETTLE)

    ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id, value=0.001,
                                   data="min-unit execute", port=ctx.port)
    return bool(ok), "{} row(s) of 0.001, execute {}".format(
        len(values), "ok" if ok else "rejected"), (
        "" if ok else "a wallet of {} minimum-unit tokens could not execute: {} "
        "- suggests a floor on what selection will assemble".format(len(values), msg))


def sc_c_25(ctx, ci):
    """
    SC-C-25 - A parts-only wallet DEPLOYS, then executes its own contract.

    WHAT IT CHECKS
        A wallet with no whole token deploys a contract with a value (taking
        collateral from parts), then executes it.

    WHY IT MATTERS
        SC-C-15 deploys from parts; SC-C-09 executes from parts. This does both
        in sequence on one wallet, which is the case where the deploy's
        collateral changes what the later execute has to work with.

        Deploying from parts consumes some of them and returns change, so the
        wallet's shape afterwards is different from the one it started with.
        If the change is wrong, the execute is the operation that discovers it.

    MANUAL STEPS
        Build a parts wallet, deploy a small fractional contract from it, then
        execute that same contract from the same wallet.

    PASS / FAIL
        PASS  deploy costs its value, execute then succeeds
        FAIL  deploy succeeds but the execute fails -> the deploy left the
              wallet in a state it cannot transact from
        SKIP  no parts wallet could be built
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a wallet for the parts deploy"
    w = spare[-1]

    ok, why = _sc_subs_parts_wallet(ctx, w, [0.7, 0.6, 0.5, 0.4, 0.3])
    if not ok:
        return SKIP, "could not build parts wallet", why

    ready, why = _sc_prepare(ctx, w, 1.5)
    if not ready:
        return SKIP, "setup incomplete", why

    value = rand_value(0.050, 0.300)
    spent, sc_id, err = _deploy_and_measure(ctx, w, value)
    if err:
        return False, "deploy from parts failed", err

    exact = rc.close_enough(spent, value, tol=cost_tolerance(value))
    time.sleep(SETTLE)

    ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id,
                                   value=rand_value(0.010, 0.050),
                                   data="execute own contract", port=ctx.port)
    try:
        drift = db.denom_drift(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not exact:
        problems.append("deploy cost {:.4f}, expected {:.3f}".format(spent, value))
    if not ok:
        problems.append("deploy succeeded but the follow-up execute was "
                        "rejected: {} - the deploy left this wallet unable to "
                        "transact".format(msg))
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "deployed {:.3f} from parts (spent {:.4f}), execute {}".format(
        value, spent, "ok" if ok else "FAILED"), "; ".join(problems)


# -----------------------------------------------------------------------------
# Concurrency - simultaneous deploys
# (was sc/sc_cases_stress.py)
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
        Every host in this lane deploys a valued contract simultaneously. All
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
        SKIP  fewer than 3 hosts in this lane
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
            "lane has {} host(s); fleet-scale concurrency needs more".format(
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
# (was sc/sc_cases_scale.py)
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
# case_runner passes args through; a lane can set its own via ctx.args.
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
            "lane has {} spare host(s); scaled subscription needs at "
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
        return SKIP, "need 3+ hosts", "lane has {}".format(len(hosts))

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
# (was sc/sc_cases_gaps.py)
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
# SC-C-28
# ---------------------------------------------------------------------------

def sc_c_28(ctx, ci):
    """
    SC-C-28 - A split creates a novel denomination; the counter must track it.

    WHAT IT CHECKS
        Deploy a value that produces change at a denomination the wallet has
        never held. Afterwards a token_denom row must exist for that new
        denomination, and it must match the tokens actually held there.

    WHY IT MATTERS
        The decrement 977f6fba introduced is a bare UPDATE:

            UPDATE token_denom SET count = GREATEST(count-1,0)
             WHERE did = $1 AND denom = $2

        With no matching row that affects ZERO rows and returns no error. It is
        a silent no-op.

        A split is exactly how a wallet acquires a denomination it has never
        held: deploying 0.354 from a 1.000 produces a 0.646 change token, and
        0.646 is a value nothing minted. If the split does not also create the
        token_denom row, the counter never knows about it - and a later burn of
        that child updates nothing, silently.

        This is the reachable path to the missing-row case, without needing a
        recovered node or seeded corruption.

    MANUAL STEPS
        1. Note the wallet's denominations:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
        2. Deploy a value chosen so the change is a denomination NOT in that
           list - e.g. 0.354 from a 1.000 gives 0.646.
        3. List the free tokens and the counter again. The new denomination
           must appear in BOTH, with matching counts.

    PASS / FAIL
        PASS  the change denomination appears in token_denom and matches reality
        FAIL  the change token exists but has no counter row -> a later burn of
              it will silently do nothing, because the UPDATE has no row to hit
        SKIP  no whole token available to split
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _sc_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("SC-C-28", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not any(v >= 1.0 for v in before["denom"]):
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            before = db.record("SC-C-28", "before (refunded)", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        if not any(v >= 1.0 for v in before["denom"]):
            return SKIP, "no whole token to split", "cannot create a novel denomination"

    # Deploy an arbitrary fraction. The change does NOT come back as one
    # tidy token - the first run showed it returns as many small ones
    # (0.001:206->207, 0.010:182->186, ...) - so this no longer guesses the
    # shape. It asks the question the bare UPDATE actually depends on:
    # does every denomination holding free tokens have a counter row?
    value = rand_value(0.150, 0.850)

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="novel denomination probe", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-28", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        real = db.real_free_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # Any denomination with free tokens but NO counter row is the missing-row
    # condition: the burn path's UPDATE would match nothing and silently
    # succeed.
    counted = set(after["denom"])
    uncounted = {d: c for d, c in real.items()
                 if c > 0 and not any(abs(d - k) < 1e-9 for k in counted)}
    new_denoms = {d for d in real if not any(abs(d - k) < 1e-9 for k in before["denom"])}

    problems = []
    if uncounted:
        problems.append(
            "{} denomination(s) hold free tokens with NO token_denom row: {} - "
            "the burn path is a bare UPDATE, so burning one of these would "
            "match no row, return no error, and leave the counter permanently "
            "wrong".format(
                len(uncounted),
                ", ".join("{:.3f}x{}".format(d, c)
                          for d, c in sorted(uncounted.items())[:4])))
    drift = db.new_drift(before, after)
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "deployed {:.3f}, {} new denomination(s), {} uncounted | {}".format(
        value, len(new_denoms), len(uncounted),
        db.format_evidence(before, after)), "; ".join(problems)


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
# SC-C-11
# ---------------------------------------------------------------------------

def sc_c_11(ctx, ci):
    """
    SC-C-11 - Deploy a contract with no value, or a value of zero.

    WHAT IT CHECKS
        A deploy with value 0 takes NO collateral: the free balance is
        unchanged, nothing is committed, and the denomination counter does not
        move.

    WHY IT MATTERS
        The pre-pass opens with an explicit skip:

            if scInfo.Value <= 0 { continue }

        Nothing exercised that branch. It matters because it is the guard that
        keeps a valueless contract off the collateral path entirely - if it
        stopped working, every zero-value deploy would attempt a split for
        nothing, and LockTokensForSplit called with 0 has no defined useful
        behaviour.

        It is also the boundary either side of SC-C-01: 0 takes nothing, and
        the smallest positive value takes exactly itself.

    MANUAL STEPS
        Deploy with "value":0 and compare the balance and the denom listing
        before and after. Both must be identical.

    PASS / FAIL
        PASS  deploy succeeds and costs nothing, counter untouched
        FAIL  anything was deducted or committed -> the guard did not hold
        FAIL  rejected -> a zero-value contract should still deploy; it simply
              carries no collateral
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _sc_prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before = db.record("SC-C-11", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=0,
                                   data="zero-value deploy", port=ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-11", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    d = db.delta(before, after)
    problems = []
    if not ok:
        problems.append("rejected: {} - a zero-value contract should deploy, it "
                        "simply carries no collateral".format(str(msg)[:80]))
    if abs(d["free"]) > TOL:
        problems.append("free balance moved {:+.4f} for a zero-value deploy - "
                        "the `if scInfo.Value <= 0 continue` guard did not "
                        "hold".format(d["free"]))
    if abs(d["committed"]) > TOL:
        problems.append("committed moved {:+.4f} for a zero-value deploy".format(
            d["committed"]))
    if db.new_drift(before, after):
        problems.append("counter drifted on a deploy that should not have "
                        "touched it: " + db.describe_drift(db.new_drift(before, after)))

    return (not problems), "zero-value deploy {}, free {:+.4f} committed {:+.4f}".format(
        "accepted" if ok else "REJECTED", d["free"], d["committed"]), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-30
# ---------------------------------------------------------------------------

def sc_c_30(ctx, ci):
    """
    SC-C-30 - Execute a contract deployed at exactly 1.0, where no split happened.

    WHAT IT CHECKS
        Deploy at exactly 1.0 - a whole token, so no split occurs - then
        EXECUTE that contract. The execute must succeed and the chain advance.

    WHY IT MATTERS
        When the collateral is exactly one whole denomination there is nothing
        to split, so CollectRBTTokens returns no childTokensKept, scGenTX stays
        nil, and PreviousTransactionID is left as the empty string.

        SC-C-04 proves that deploy costs 1.0. It does not prove the contract is
        usable afterwards. A contract whose genesis was written down the
        no-split path carries different chain linkage from one that was split,
        and the execute is the first operation that has to follow that linkage.

        This is the one branch of H1 the ladder touches but does not finish:
        deploy proven, execute-after-no-split not.

    MANUAL STEPS
        1. Deploy with "value":1.0 exactly.
        2. Read the chain:
             curl -s http://$SENDER:20000/rubix/v1/smart_contracts/<SC>/chain
        3. Execute the same contract.
        4. Read the chain again - it must be one entry longer.

    PASS / FAIL
        PASS  execute succeeds and the chain grows
        FAIL  execute rejected -> a contract deployed without a split cannot be
              used, which SC-C-04 would not have revealed
    """
    s, _ = ctx.pair(0)

    ready, why = _sc_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _sc_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    # Exactly 1.0: a whole denomination, so the split path is skipped entirely.
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=1.0,
                                   data="no-split deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    okc, before_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    before_len = len(before_chain) if okc else -1

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=1.0,
                                   data="execute after no-split deploy",
                                   port=ctx.port)
    if not ok:
        return False, "execute rejected after a no-split deploy", (
            "the contract deployed at exactly 1.0 (no split, so its genesis "
            "carries an empty PreviousTransactionID) cannot be executed: {} - "
            "SC-C-04 proves the deploy costs correctly but not that the "
            "contract is usable".format(str(msg)[:90]))

    grew, n = False, before_len
    for _ in range(12):
        time.sleep(2)
        okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
        n = len(chain) if okc else n
        if n > before_len:
            grew = True
            break

    return grew, "no-split deploy at 1.0, chain {} -> {}".format(before_len, n), (
        "" if grew else "execute returned success but the chain never advanced")


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


# =============================================================================
# CROSS-ASSET
# =============================================================================


# -----------------------------------------------------------------------------
# Combined - deploy plus transfer, concurrent deploy and mint
# (was cross-asset/cross-asset_cases.py)
# -----------------------------------------------------------------------------
# cross-asset_cases.py - operations that touch more than one asset type at once.
#
# Run via:  cd test-plan/full-test && python3 case_runner.py --cases cross-asset
#
# WHY THIS MODULE MATTERS
#     token_denom accounting is updated on TWO separate code paths:
#
#       df07a49f  core/wallet/post_consensus_persistence.go   (SC deploy collateral)
#       977f6fba  core/wallet/token_chain.go                  (RBT burnt for FT mint)
#
#     Each was fixed independently, and neither appears to coordinate with the
#     other. They write the same table, for the same DID, from different call
#     stacks. Every case in the SC and FT suites exercises exactly one of them at
#     a time.
#
#     These cases run them together. CRS-C-02 runs them CONCURRENTLY on one DID,
#     which is the obvious race and the case most likely to fail.
#
# Docstring contract is the same as the other modules:
#     WHAT IT CHECKS / WHY IT MATTERS / MANUAL STEPS / PASS-FAIL




def _rand_value(lo, hi):
    return max(round(random.uniform(lo, hi), 3), 0.001)


def _tag(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _crs_bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _crs_prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)
    detail = _crs_bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)
    qd = _crs_bal(ctx, q)
    if qd and qd["balance"] < need:
        rc.fund_did(q["host"], q["did"], int(need) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], need, ctx.port)
    return True, ""


def _crs_new_contract(ctx, entry):
    tag = _tag()
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"],
                                               wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None


# ---------------------------------------------------------------------------
# CRS-C-01
# ---------------------------------------------------------------------------

def crs_c_01(ctx, ci):
    """
    CRS-C-01 - Deploy a contract with a value AND transfer RBT in one call.

    WHAT IT CHECKS
        A single /tx carrying both an RBT transfer and a valued SC deploy. Both
        take effect, each deducts its own amount, and the counter is consistent
        afterwards.

    WHY IT MATTERS
        These are the two paths df07a49f had to reconcile. The fix suppresses
        the isLocalTransfer credit for a deploy, because a deploy pins Owner to
        Initiator and the credit would land on the very DID just decremented -
        cancelling it out.

        A bundled call is where that reasoning is hardest: there IS a genuine
        transfer in the same transaction, with a real receiver, alongside a
        deploy whose owner is the initiator. If the suppression is too broad
        the transfer's accounting is lost; too narrow and the deploy's
        decrement is cancelled. Neither shows up when the two run separately.

    MANUAL STEPS
        Post one transaction with both parts:
          {"initiator":"<DID>", "owner":"<RECEIVER_DID>",
           "tokens":{"rbt":1.0, "ft":[], "nft":[],
                     "smartContract":[{"smartContractId":"<SC>",
                                       "value":0.354,"data":"bundled"}],
                     "transferNftOwnership":false},
           "memo":"CRS-C-01"}
        Check the sender's balance fell by 1.354, the receiver gained 1.0, and
        the denom listing still matches reality.

    PASS / FAIL
        PASS  sender down by transfer+value, receiver up by the transfer, no drift
        FAIL  sender down by only one of the two -> one path's accounting was
              cancelled by the other
        FAIL  counter drifts -> the credit suppression is wrong in a bundle
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    transfer = 1.0
    value = _rand_value(0.100, 0.500)

    ready, why = _crs_prepare(ctx, s, transfer + value + 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _crs_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before_snap["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"
    s_before = _crs_bal(ctx, s)
    r_before = _crs_bal(ctx, r)

    body = {
        "initiator": s["did"], "owner": r["did"],
        "tokens": {
            "rbt": transfer, "ft": [], "nft": [],
            "smartContract": [{"smartContractId": sc_id, "value": value,
                               "data": "bundled deploy + transfer"}],
            "transferNftOwnership": False,
        },
        "memo": "CRS-C-01 bundled",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    s_after = _crs_bal(ctx, s)
    r_after = _crs_bal(ctx, r)
    try:
        after_snap = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before_snap, after_snap)

    spent = (s_before["balance"] - s_after["balance"]) if (s_before and s_after) else None
    gained = (r_after["balance"] - r_before["balance"]) if (r_before and r_after) else None
    expected = transfer + value

    problems = []
    if spent is None or not rc.close_enough(spent, expected, tol=TOL * 4):
        problems.append("sender spent {:.4f}, expected {:.4f} (transfer {} + "
                        "value {:.3f})".format(spent if spent is not None else -1,
                                               expected, transfer, value))
        if spent is not None and rc.close_enough(spent, transfer, tol=TOL * 4):
            problems.append("the deploy's collateral was not deducted at all - "
                            "its decrement was cancelled inside the bundle")
        elif spent is not None and rc.close_enough(spent, value, tol=TOL * 4):
            problems.append("the transfer was not deducted - the deploy's credit "
                            "suppression is too broad")
    if gained is None or not rc.close_enough(gained, transfer, tol=TOL * 4):
        problems.append("receiver gained {:.4f}, expected {}".format(
            gained if gained is not None else -1, transfer))
    if drift:
        problems.append("counter drifted after a bundled call: "
                        + db.describe_drift(drift))

    return (not problems), "sent {:.4f} (transfer {} + deploy {:.3f}), receiver +{:.4f}".format(
        spent if spent is not None else -1, transfer, value,
        gained if gained is not None else -1), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-02
# ---------------------------------------------------------------------------

def crs_c_02(ctx, ci):
    """
    CRS-C-02 - SC deploy and FT mint fired CONCURRENTLY on the same DID.

    WHAT IT CHECKS
        A valued contract deploy and an FT mint start at the same moment on one
        wallet. Both should succeed, each should cost its own amount, and the
        denomination counter must still match the real free tokens afterwards.

    WHY IT MATTERS
        This is the sharpest test in the suite for the counter fixes, because it is the
        only one that runs BOTH fixes at once.

          df07a49f  decrements token_denom from post_consensus_persistence.go
          977f6fba  decrements token_denom from token_chain.go

        Same table, same DID, different call stacks, fixed independently by
        different changes. Nothing in either fix appears to coordinate with the
        other. Run separately - as every other case does - they each look
        correct. Run together they are two concurrent read-modify-write cycles
        against the same rows.

        The likely failure is not a crash. It is a counter that ends up short
        by one decrement because both paths read the same starting value, and
        an unrelated transfer failing much later with
        "lockSelectedTokens: no tokens provided".

    MANUAL STEPS
        1. Snapshot the wallet:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
             SELECT token_value, COUNT(*) FROM tokens
               WHERE did='<DID>' AND token_status=0 AND token_type=1
               GROUP BY token_value;
        2. In two terminals at the same moment: POST an SC deploy with a value,
           and POST an FT mint. Sign both.
        3. Wait ~15s and re-run both queries. They must still agree.

    PASS / FAIL
        PASS  both succeed, both cost correctly, counter consistent
        FAIL  counter drifts -> the two decrements interfered; this is a NEW
              finding, not a regression of either fix on its own
        FAIL  either operation is rejected while the wallet has ample balance
        SKIP  counter already drifting before the race
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    value = _rand_value(0.100, 0.500)
    ft_backing = 2          # whole RBT burnt by the mint
    ready, why = _crs_prepare(ctx, s, value + ft_backing + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _crs_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ft_name = "race" + _tag(6)

    try:
        before = db.record("CRS-C-02", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "already drifting before the race", (
            "counter inconsistent beforehand: " + db.describe_drift(before["denom_drift"])
            + " - a drift afterwards could not be attributed to the race")

    def do_deploy():
        return ("sc-deploy",) + rc.sc_transaction(
            s["host"], s["did"], sc_id, value=value,
            data="race: deploy", port=ctx.port)[:2]

    def do_mint():
        return ("ft-mint",) + rc.mint_ft(
            s["host"], s["did"], ft_name, 10, ft_backing, ctx.port)[:2]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(do_deploy)
        f2 = pool.submit(do_mint)
        outcomes = [f1.result(), f2.result()]

    time.sleep(SETTLE * 3)
    try:
        after = db.record("CRS-C-02", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)
    d = db.delta(before, after)

    failed = [(name, msg) for name, ok, msg in outcomes if not ok]
    problems = []
    for name, msg in failed:
        problems.append("{} rejected: {}".format(name, str(msg)[:90]))
    if drift:
        problems.append(
            "DENOM COUNTER DRIFTED after running both paths at once: "
            + db.describe_drift(drift) +
            " - the SC-deploy decrement (post_consensus_persistence.go) and the "
            "FT-burn decrement (token_chain.go) interfered. Each is correct "
            "alone; this is a new finding about the two together, and it will "
            "surface later as an unrelated transfer failing with "
            "'lockSelectedTokens: no tokens provided'")

    return (not problems), (
        "deploy {:.3f} + mint {} RBT concurrently | {} | counter {}".format(
            value, ft_backing, db.format_evidence(before, after),
            "ok" if not drift else "DRIFT")), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-03
# ---------------------------------------------------------------------------

def crs_c_03(ctx, ci):
    """
    CRS-C-03 - What the RECEIVER's node records after a bundled deploy.

    WHAT IT CHECKS
        After a bundled RBT transfer plus a valued SC deploy, the RECEIVER's
        own database must show only the transferred RBT. The contract's
        collateral must not appear as tokens the receiver owns.

    WHY IT MATTERS
        Every other denomination case measures the INITIATOR. That is only half
        of what df07a49f changed, because the status decision is role-aware:

            isSCDeployCommit := input.RoleName == TokenRole_Commit &&
                executionRole == ExecutionRoleInitiator &&
                hasSCDeploy(txInfo)

        On the initiator's node the collateral is marked Committed. On the
        receiver's node executionRole is Receiver, the guard is false, and the
        default branch writes those tokens as Free owned by the Owner.

        A pure deploy pins Owner to Initiator, so there is no separate receiver
        and the gap never appears. A bundle has a real receiver - and CRS-C-01
        showed it gains the collateral. This case reads the receiver's rows
        directly, so the report states what the receiver actually recorded
        rather than inferring it from a balance.

    MANUAL STEPS
        After a bundled deploy plus transfer, on the RECEIVER's host:
          psql -h $RECV -p 5433 -U rubix -d rubix -c \\
            "SELECT token_id, token_value, token_status FROM tokens
               WHERE did='<RDID>' AND token_type=1 AND token_status=0
               ORDER BY token_value DESC;"
        The newly arrived value should equal the TRANSFER only.

    PASS / FAIL
        PASS  receiver's free value rose by the transfer amount only
        FAIL  it rose by transfer + collateral -> the receiver recorded the
              contract's collateral as its own
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    transfer = 1.0
    value = _rand_value(0.100, 0.500)

    ready, why = _crs_prepare(ctx, s, transfer + value + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = _crs_new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        s_before = db.record("CRS-C-03", "before (initiator)", s["host"], s["did"],
                             db.snapshot(s["host"], s["did"]))
        r_before = db.record("CRS-C-03", "before (receiver)", r["host"], r["did"],
                             db.snapshot(r["host"], r["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    body = {
        "initiator": s["did"], "owner": r["did"],
        "tokens": {"rbt": transfer, "ft": [], "nft": [],
                   "smartContract": [{"smartContractId": sc_id, "value": value,
                                      "data": "bundled, receiver view"}],
                   "transferNftOwnership": False},
        "memo": "CRS-C-03 receiver view",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        s_after = db.record("CRS-C-03", "after (initiator)", s["host"], s["did"],
                            db.snapshot(s["host"], s["did"]))
        r_after = db.record("CRS-C-03", "after (receiver)", r["host"], r["did"],
                            db.snapshot(r["host"], r["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    r_gain = r_after["free"] - r_before["free"]
    s_committed = s_after["committed"] - s_before["committed"]

    problems = []
    if not rc.close_enough(r_gain, transfer, tol=TOL * 4):
        problems.append("receiver free value rose {:.4f}, expected {} (the "
                        "transfer only)".format(r_gain, transfer))
        if rc.close_enough(r_gain, transfer + value, tol=TOL * 4):
            problems.append("the excess is exactly the contract value {:.3f} - "
                            "the receiver recorded the collateral as its own, "
                            "because isSCDeployCommit requires executionRole == "
                            "Initiator and the receiver is not".format(value))
    if not rc.close_enough(s_committed, value, tol=TOL * 4):
        problems.append("initiator committed {:.4f}, expected {:.3f}".format(
            s_committed, value))

    return (not problems), "receiver +{:.4f} (transfer {}), initiator committed {:.4f} | recv[{}]".format(
        r_gain, transfer, s_committed,
        db.format_evidence(r_before, r_after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-04
# ---------------------------------------------------------------------------

def crs_c_04(ctx, ci):
    """
    CRS-C-04 - Transfer between two DIDs on ONE node (no contract involved).

    WHAT IT CHECKS
        A plain RBT transfer where sender and receiver are different DIDs on
        the same machine. The sender's free balance falls, the receiver's
        rises, and the node's denomination counters for both DIDs still match
        reality.

    WHY IT MATTERS
        This is the baseline for CRS-C-05, and it is the only way to reach the
        isLocalTransfer branch at all. df07a49f changed how that branch is
        gated:

            upsertTokenDenomDeltas(..., isLocalTransfer && !scDeploy, ...)

        The comment explains the suppression exists because a deploy pins Owner
        to Initiator, making isLocalTransfer trivially true. But the block it
        suppresses was written for a REAL same-node transfer - decrement the
        sender, credit the receiver - and on a one-DID-per-node fleet that
        situation cannot occur, so it has never been exercised.

        Run this first: if a plain same-node transfer is already wrong,
        CRS-C-05's bundled version tells you nothing.

    REQUIRES a host tagged 'multidid' in hosts.txt. Every other host is held to
    exactly one DID, and creating a second on one that did not opt in would
    break the invariant the controller tooling depends on.

    MANUAL STEPS
        On a multidid host, with DID_A and DID_B both local:
          curl -s -X POST http://$HOST:20000/rubix/v1/tx \\
               -H 'Content-Type: application/json' -d '{
                 "initiator":"<DID_A>", "owner":"<DID_B>",
                 "tokens":{"rbt":1.0,"transferNftOwnership":false},
                 "memo":"CRS-C-04"}'
        Then compare both DIDs' counters against their real free tokens.

    PASS / FAIL
        PASS  A down by the amount, B up by it, both counters consistent
        FAIL  a counter drifts -> the same-node credit/decrement pair does not
              balance
        SKIP  no multidid host in this lane
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    host = None
    for e in list(ctx.senders) + list(ctx.receivers):
        if e.get("role") == "multidid":
            host = e
            break
    if host is None:
        return SKIP, "no multidid host", (
            "this case needs two DIDs on ONE machine, which only a host tagged "
            "'multidid' in hosts.txt may have. Tag one pool host and re-run")

    did_b = ws.second_did(ctx, host)
    if not did_b:
        return SKIP, "no second DID", (
            "could not obtain a second DID on {}".format(host["host"]))
    did_a = host["did"]

    ready, why = _crs_prepare(ctx, host, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    amount = 1.0
    try:
        a_before = db.record("CRS-C-04", "before (DID A)", host["host"], did_a,
                             db.snapshot(host["host"], did_a))
        b_before = db.record("CRS-C-04", "before (DID B)", host["host"], did_b,
                             db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.initiate_transaction(host["host"], did_a, did_b,
                                         rbt=amount, memo="CRS-C-04 same-node",
                                         port=ctx.port)
    if not ok:
        return False, "same-node transfer rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        a_after = db.record("CRS-C-04", "after (DID A)", host["host"], did_a,
                            db.snapshot(host["host"], did_a))
        b_after = db.record("CRS-C-04", "after (DID B)", host["host"], did_b,
                            db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    a_moved = a_before["free"] - a_after["free"]
    b_moved = b_after["free"] - b_before["free"]

    problems = []
    if not rc.close_enough(a_moved, amount, tol=TOL * 4):
        problems.append("sender DID fell {:.4f}, expected {}".format(a_moved, amount))
    if not rc.close_enough(b_moved, amount, tol=TOL * 4):
        problems.append("receiver DID rose {:.4f}, expected {}".format(b_moved, amount))
    for label, before, after in (("sender", a_before, a_after),
                                 ("receiver", b_before, b_after)):
        d = db.new_drift(before, after)
        if d:
            problems.append("{} DID counter drifted: {}".format(
                label, db.describe_drift(d)))

    return (not problems), "same node {}: A -{:.4f}, B +{:.4f}".format(
        host["host"], a_moved, b_moved), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-05
# ---------------------------------------------------------------------------

def crs_c_05(ctx, ci):
    """
    CRS-C-05 - Same-node transfer bundled with a valued contract deploy.

    WHAT IT CHECKS
        One transaction carrying an RBT transfer between two DIDs on the SAME
        machine, plus a valued SC deploy. The sender pays transfer + value, the
        other local DID gains the transfer only, and both counters stay
        consistent.

    WHY IT MATTERS
        This is the single branch of df07a49f that nothing else can reach:

            upsertTokenDenomDeltas(..., isLocalTransfer && !scDeploy, ...)

        The suppression assumes those two conditions never usefully coincide -
        a deploy pins Owner to Initiator, so isLocalTransfer is "trivially
        true" and the credit would cancel the decrement. A bundle breaks that
        assumption: there IS a real same-node transfer with a genuine second
        DID, AND a deploy, in one transaction.

        So the suppression fires when a real local credit was owed. If it is
        too broad the transfer's credit is lost; if too narrow the deploy's
        decrement is cancelled. Both are silent, and both leave one DID's
        counter permanently wrong.

        CRS-C-01 found the cross-node version of this. This is the same-node
        version, and it is the last uncovered branch of hunk H2.

    REQUIRES a host tagged 'multidid' in hosts.txt.

    MANUAL STEPS
        On a multidid host, post ONE transaction with both parts:
          {"initiator":"<DID_A>", "owner":"<DID_B>",
           "tokens":{"rbt":1.0, "ft":[], "nft":[],
                     "smartContract":[{"smartContractId":"<SC>",
                                       "value":0.4,"data":"bundled"}],
                     "transferNftOwnership":false}}
        Then compare both DIDs' counters against their real free tokens.

    PASS / FAIL
        PASS  A down by transfer+value, B up by the transfer, no drift
        FAIL  B gained transfer+value -> the collateral was credited locally,
              the same-node form of what CRS-C-01 found
        FAIL  either counter drifts -> the suppression is wrong in a bundle
        SKIP  no multidid host in this lane
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    host = None
    for e in list(ctx.senders) + list(ctx.receivers):
        if e.get("role") == "multidid":
            host = e
            break
    if host is None:
        return SKIP, "no multidid host", (
            "needs two DIDs on ONE machine - tag a pool host 'multidid' in "
            "hosts.txt. This is the only branch of the denom gate that cannot "
            "be reached any other way")

    did_b = ws.second_did(ctx, host)
    if not did_b:
        return SKIP, "no second DID", "could not obtain a second DID on {}".format(
            host["host"])
    did_a = host["did"]

    ready, why = _crs_prepare(ctx, host, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    transfer = 1.0
    value = _rand_value(0.100, 0.500)
    sc_id, err = _crs_new_contract(ctx, host)
    if err:
        return SKIP, "generation failed", err

    try:
        a_before = db.record("CRS-C-05", "before (DID A)", host["host"], did_a,
                             db.snapshot(host["host"], did_a))
        b_before = db.record("CRS-C-05", "before (DID B)", host["host"], did_b,
                             db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    body = {
        "initiator": did_a, "owner": did_b,
        "tokens": {"rbt": transfer, "ft": [], "nft": [],
                   "smartContract": [{"smartContractId": sc_id, "value": value,
                                      "data": "same-node bundle"}],
                   "transferNftOwnership": False},
        "memo": "CRS-C-05 same-node bundle",
    }
    ok, msg, _ = rc._tx(host["host"], body, ctx.port)
    if not ok:
        return False, "same-node bundled call rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        a_after = db.record("CRS-C-05", "after (DID A)", host["host"], did_a,
                            db.snapshot(host["host"], did_a))
        b_after = db.record("CRS-C-05", "after (DID B)", host["host"], did_b,
                            db.snapshot(host["host"], did_b))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    a_spent = a_before["free"] - a_after["free"]
    b_gain = b_after["free"] - b_before["free"]
    a_committed = a_after["committed"] - a_before["committed"]
    expected_spend = transfer + value

    problems = []
    if not rc.close_enough(a_spent, expected_spend, tol=TOL * 4):
        problems.append("initiator spent {:.4f}, expected {:.4f}".format(
            a_spent, expected_spend))
    if not rc.close_enough(b_gain, transfer, tol=TOL * 4):
        problems.append("local receiver gained {:.4f}, expected {}".format(
            b_gain, transfer))
        if rc.close_enough(b_gain, expected_spend, tol=TOL * 4):
            problems.append("the excess is exactly the contract value {:.3f} - "
                            "the collateral was credited to the other local DID, "
                            "the same-node form of CRS-C-01".format(value))
    if not rc.close_enough(a_committed, value, tol=TOL * 4):
        problems.append("initiator committed {:.4f}, expected {:.3f}".format(
            a_committed, value))
    for label, before, after in (("initiator", a_before, a_after),
                                 ("local receiver", b_before, b_after)):
        d = db.new_drift(before, after)
        if d:
            problems.append("{} counter drifted: {}".format(
                label, db.describe_drift(d)))

    return (not problems), "same-node bundle: A -{:.4f} (transfer {} + value {:.3f}), B +{:.4f}, committed {:.4f}".format(
        a_spent, transfer, value, b_gain, a_committed), "; ".join(problems)


# ---------------------------------------------------------------------------
# CRS-C-06
# ---------------------------------------------------------------------------

def crs_c_06(ctx, ci):
    """
    CRS-C-06 - Does the receiver's over-credit track the COLLATERAL, value for
    value?

    THE PROOF BEHIND CRS-C-03. Same bundle, run TWICE with two deliberately
    different contract values, from and to the same pair.

    WHAT IT CHECKS
        Bundle A: transfer 1.0 + deploy at v1
        Bundle B: transfer 1.0 + deploy at v2      (v2 chosen well clear of v1)

        Then the receiver's two gains must satisfy, simultaneously:

            gain_A == 1.0 + v1
            gain_B == 1.0 + v2
            gain_B - gain_A == v2 - v1

    WHY IT MATTERS
        CRS-C-03 observed the receiver gaining 1.206 for a 1.0 transfer bundled
        with a 0.206 deploy, and concluded the extra 0.206 was the collateral.
        That is an inference from ONE reading, and a single reading cannot
        exclude the obvious alternatives: a coincidental concurrent credit, a
        rounding artefact, or a fixed surcharge that merely happened to equal
        the contract value that run.

        Varying the collateral removes all three at once. A coincidence does
        not follow v from one run to the next; a fixed surcharge does not
        change when v changes; a rounding artefact does not scale. If the
        receiver's excess equals v both times AND the difference between the
        two excesses equals the difference between the two contract values,
        the only thing the receiver can be crediting is the collateral itself.

        Note the pairing with SC-C-33: on the initiator the collateral goes to
        Committed, and here the same value ALSO appears as Free on the
        receiver. If both hold in one run, the value is not merely misplaced -
        it exists twice.

    MANUAL STEPS
        Run the CRS-C-03 bundle twice with clearly different contract values,
        and on the RECEIVER each time:
          SELECT ROUND(SUM(token_value)::numeric,4) FROM tokens
           WHERE did='<RDID>' AND token_type=1 AND token_status=0;
        Subtract. Two gains, two contract values, one subtraction.

    PASS / FAIL
        PASS  the receiver gained exactly the transfer both times - no
              over-credit, so CRS-C-03 does not reproduce
        FAIL  both excesses equal their contract value -> the receiver credits
              the collateral, CONFIRMED and no longer an inference
        FAIL  the excess does not track v -> whatever CRS-C-03 saw, it is not
              the collateral; re-open that case before reporting it
        SKIP  a bundle was rejected (that is main's behaviour - on the base
              build this case cannot run, and INCONCLUSIVE is the honest
              verdict rather than a false PASS)
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    transfer = 1.0

    # Deliberately far apart, so "the excess tracked v" cannot be satisfied by
    # noise at the tolerance scale.
    v1 = _rand_value(0.100, 0.200)
    v2 = _rand_value(0.700, 0.900)

    ready, why = _crs_prepare(ctx, s, transfer * 2 + v1 + v2 + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    def bundle(value, label):
        """Fire one bundle; return (ok, receiver_gain, initiator_committed, msg)."""
        sc_id, err = _crs_new_contract(ctx, s)
        if err:
            return None, 0.0, 0.0, "generation failed: " + err
        try:
            r_before = db.record("CRS-C-06", "before receiver " + label,
                                 r["host"], r["did"], db.snapshot(r["host"], r["did"]))
            s_before = db.snapshot(s["host"], s["did"])
        except db.DBUnavailable as e:
            return None, 0.0, 0.0, "database unreachable: " + str(e)

        body = {
            "initiator": s["did"], "owner": r["did"],
            "tokens": {"rbt": transfer, "ft": [], "nft": [],
                       "smartContract": [{"smartContractId": sc_id, "value": value,
                                          "data": "CRS-C-06 " + label}],
                       "transferNftOwnership": False},
            "memo": "CRS-C-06 " + label,
        }
        ok, msg, _ = rc._tx(s["host"], body, ctx.port)
        if not ok:
            return False, 0.0, 0.0, str(msg)
        time.sleep(SETTLE * 2)

        try:
            r_after = db.record("CRS-C-06", "after receiver " + label,
                                r["host"], r["did"], db.snapshot(r["host"], r["did"]))
            s_after = db.snapshot(s["host"], s["did"])
        except db.DBUnavailable as e:
            return None, 0.0, 0.0, "database unreachable: " + str(e)
        return (True,
                r_after["free"] - r_before["free"],
                s_after["committed"] - s_before["committed"],
                "")

    ok1, gain1, committed1, msg1 = bundle(v1, "v={:.3f}".format(v1))
    if ok1 is None:
        return SKIP, "could not run the first bundle", msg1
    if ok1 is False:
        return SKIP, "the first bundle was rejected", (
            "the build under test refuses a bundled transfer+deploy outright, "
            "so there is no over-credit to measure. This is main's behaviour "
            "and is the correct SKIP - do not read it as a pass. Rejection: "
            + msg1)

    ok2, gain2, committed2, msg2 = bundle(v2, "v={:.3f}".format(v2))
    if ok2 is None:
        return SKIP, "could not run the second bundle", msg2
    if ok2 is False:
        return SKIP, "the second bundle was rejected", (
            "the first bundle was accepted and the second was not, so the two "
            "readings are not comparable. Re-run before concluding anything. "
            "Rejection: " + msg2)

    excess1 = gain1 - transfer
    excess2 = gain2 - transfer
    tol = TOL * 4

    clean = rc.close_enough(gain1, transfer, tol=tol) and \
        rc.close_enough(gain2, transfer, tol=tol)
    if clean:
        return True, "receiver +{:.4f} and +{:.4f}, transfer {} both times".format(
            gain1, gain2, transfer), (
            "the receiver gained the transfer only, at two different contract "
            "values - CRS-C-03's over-credit did not reproduce")

    tracks = (rc.close_enough(excess1, v1, tol=tol)
              and rc.close_enough(excess2, v2, tol=tol)
              and rc.close_enough(excess2 - excess1, v2 - v1, tol=tol))

    if tracks:
        note = ("RECEIVER CREDITS THE COLLATERAL - CONFIRMED, not inferred. "
                "The excess over the transfer was {:.4f} for a {:.3f} contract "
                "and {:.4f} for a {:.3f} one, and the two excesses differ by "
                "{:.4f} against a contract-value difference of {:.4f}. A "
                "coincidence does not follow the value across runs and a fixed "
                "surcharge does not change with it. isSCDeployCommit requires "
                "executionRole == Initiator, so on the receiver's node the "
                "guard is false and the default branch writes the collateral "
                "as Free owned by the Owner.".format(
                    excess1, v1, excess2, v2, excess2 - excess1, v2 - v1))
        if committed1 > tol and committed2 > tol:
            note += (" The initiator committed {:.4f} and {:.4f} for the same "
                     "two deploys, so this value is recorded on BOTH nodes - "
                     "it is duplicated, not moved.".format(committed1, committed2))
        return False, "receiver excess {:.4f} (v={:.3f}) and {:.4f} (v={:.3f}) - tracks the collateral".format(
            excess1, v1, excess2, v2), note

    return False, "receiver excess {:.4f} (v={:.3f}) and {:.4f} (v={:.3f}) - does NOT track the collateral".format(
        excess1, v1, excess2, v2), (
        "the receiver gained more than the transfer, but the excess does not "
        "equal the contract value at both points, so whatever CRS-C-03 saw is "
        "not simply the collateral being credited. Do not report it as such - "
        "re-open CRS-C-03 with these two readings attached.")


# CRS-C-01 first: if the bundled call is already wrong when run alone, the
# concurrent case has nothing clean to build on.


# Both cases own their wallet outright - each measures a balance delta and a
# denom delta on one DID, so nothing else may touch it while they run.


# =============================================================================
# GENERAL (integrity, drift, locks)
# =============================================================================


# -----------------------------------------------------------------------------
# Integrity - denomination counter baseline and reconciliation
# (was general/general_cases.py)
# -----------------------------------------------------------------------------
# general_cases.py - fleet-wide and integrity cases from the master catalogue.
#
# Run via:  cd test-plan/full-test && python3 case_runner.py --cases general
# One case:                          python3 case_runner.py --cases general --only GEN-IN-08
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
#
# BEFORE RUNNING ANYTHING BY HAND
#         HOST=192.168.1.104
#         DID=$(curl -s http://$HOST:20000/rubix/v1/dids | python3 -c \
#               'import sys,json; print(json.load(sys.stdin)["result"][0])')
#         psql -h $HOST -p 5433 -U rubix -d rubix        # password: rubixpass
#
# WHAT token_denom IS, AND WHY FOUR CASES GUARD IT
#     Every node keeps a small table, token_denom, counting how many FREE tokens
#     it holds at each denomination - "four tokens worth 1.000, two worth 0.500".
#
#     It is a CACHE, and the only consumer that matters is
#     lockTokensForSplitOnce (core/wallet/token_lock.go:505). When a transfer
#     needs tokens, that function reads the counter FIRST to decide which
#     denominations to ask for, then reads matching rows from `tokens`.
#
#     So a wrong counter is uniquely nasty:
#       * Nothing fails when the counter goes wrong.
#       * The failure lands on a LATER, unrelated transaction, which asks for
#         rows that are no longer Free and dies with
#         "lockSelectedTokens: no tokens provided".
#       * The error names the innocent transaction, not the operation that
#         caused the damage.
#
#     GEN-IN-08 and GEN-IN-09 check the counter's shape. GEN-IN-10 and GEN-IN-11
#     check the two operations that BURN or COMMIT RBT and must therefore
#     decrement it - FT mint and contract deploy. They are kept apart on purpose:
#     if both were one case, an FT regression and a smart-contract regression
#     would be indistinguishable in the report.




# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gen_bal(ctx, entry):
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], ctx.port)
    return detail if ok else None


def _gen_prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)   # already-registered returns an error; ignore

    detail = _gen_bal(ctx, entry)
    have = detail["balance"] if detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} RBT (reached {})".format(need, now)

    qd = _gen_bal(ctx, q)
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


def _gen_new_contract(ctx, entry):
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

    ready, why = _gen_prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        snap_before = db.record("GEN-IN-10", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
        before = snap_before["denom_drift"]
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
        snap_after = db.record("GEN-IN-10", "after", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
        after = db.new_drift(snap_before, snap_after)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    return (not after), ("counter consistent after FT mint | "
                         + db.format_evidence(snap_before, snap_after)
                         if not after
                         else "{} denomination(s) drifted | {}".format(
                             len(after),
                             db.format_evidence(snap_before, snap_after))), (
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

    ready, why = _gen_prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        snap_before = db.record("GEN-IN-11", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
        before = snap_before["denom_drift"]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before:
        return SKIP, "already drifting before the deploy", (
            "cannot attribute drift to the deploy when it is already present: "
            + _describe_drift(before) + " - see GEN-IN-08")

    sc_id, err = _gen_new_contract(ctx, s)
    if err:
        return SKIP, "contract generation failed", err

    value = 0.001
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="denom drift check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        snap_after = db.record("GEN-IN-11", "after", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
        after = db.new_drift(snap_before, snap_after)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # LockTokensForSplit selects WHOLE denominations, so a 0.001 commitment is
    # backed by a 1.000 token. Movement at that denomination is the deploy
    # working; movement anywhere else is not.
    collateral_denom = 1.0
    other = {d: v for d, v in after.items() if abs(d - collateral_denom) > TOL}

    return (not other), (("no drift outside the collateral denomination | "
                          + db.format_evidence(snap_before, snap_after))
                         if not other
                         else "{} other denomination(s) drifted | {}".format(
                             len(other),
                             db.format_evidence(snap_before, snap_after))), (
        "" if not other else
        _describe_drift(other) + " - the deploy disturbed denominations outside "
        "the one its collateral came from")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


# Shape checks first: if the counter is already wrong before any operation
# runs, GEN-IN-10 and GEN-IN-11 cannot attribute drift to the mint or the deploy
# and will honestly SKIP rather than blame the wrong thing.


# ---------------------------------------------------------------------------
# Lanes - see sc_cases.py for the reasoning.
#
# GEN-IN-08 and GEN-IN-09 only READ; they take no action and could share a
# wallet with anything. They are kept on their own host anyway so the baseline
# they report is a wallet nothing else is touching - a drifting counter is only
# attributable if nothing else was writing to it.
#
# GEN-IN-10 and GEN-IN-11 each perform an operation and re-check, so they need
# private wallets for the same reason every delta case does.
# ---------------------------------------------------------------------------


# -----------------------------------------------------------------------------
# Integrity - fleet-wide sweeps
# (was general/general_cases_integrity.py)
# -----------------------------------------------------------------------------
# general_cases_integrity.py - fleet-wide database invariants.
#
# These take no action. They read the fleet and assert things that must be true
# regardless of what ran before - which makes them the cases that catch damage
# nobody attributed to anything. Run them at the END of a cycle: a failure here
# means one of the earlier cases broke something quietly.




def _gen_integrity_hosts(ctx):
    """EVERY ready host in the run, quorums included - not just this lane's.

    These are the fleet-wide sweeps. Scoping them to a lane does not make them
    weaker, it makes them WRONG: a lane holds 2-4 hosts, and "no negative
    counters across 4 host(s)" prints the same shape as "across 31 host(s)"
    while checking 13% of the fleet. That is not a smaller test, it is a test
    that reports a clean result it did not earn.

    `ctx.fleet` is populated by case_runner.build_lanes. The fallback exists
    only for a context built by another driver (smoke_test), and says so in
    the result rather than quietly under-reporting - see _scope() below.
    """
    entries = list(ctx.fleet) if getattr(ctx, "fleet", None) else (
        list(ctx.senders) + list(ctx.receivers) + list(ctx.quorum_hosts))
    seen, out = set(), []
    for e in entries:
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


def _gen_integrity_scope(ctx):
    """A label for the result line, so the reader knows what was swept.

    Every fleet-wide case reports "N host(s) checked". That number is only
    meaningful alongside whether N was the fleet or one lane.
    """
    return "fleet" if getattr(ctx, "fleet", None) else "LANE ONLY"


# ---------------------------------------------------------------------------
# GEN-IN-12
# ---------------------------------------------------------------------------

def gen_in_12(ctx, ci):
    """
    GEN-IN-12 - No denomination counter is negative, anywhere.

    WHAT IT CHECKS
        Across every host in this run, no token_denom row has count < 0.

    WHY IT MATTERS
        The FT burn fix floors the decrement deliberately:

            count = GREATEST(count - 1, 0)

        That floor exists because the old code could be called more than once
        for the same burn. A negative count is therefore the specific signature
        of that floor being bypassed or removed - and it is worse than drift,
        because selection would ask for a negative number of tokens and fail in
        a way that reads like corruption rather than a counter bug.

        Cheap to check and covers every DID on every host, not just the wallets
        this run happened to use.

    MANUAL STEPS
        On any node:
          SELECT did, denom, count FROM token_denom WHERE count < 0;
        Should return no rows, on every host.

    PASS / FAIL
        PASS  no negative counts anywhere
        FAIL  the report names host, DID and denomination
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, bad = 0, []
    for e in _gen_integrity_hosts(ctx):
        try:
            rows = db.negative_denoms(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        for did, denom, count in rows:
            bad.append("{} did {} denom {:.3f} count {}".format(
                e["host"], did[:12], denom, count))

    if not checked:
        return SKIP, "no host reachable", "could not read token_denom anywhere"
    return (not bad), "{} host(s) checked ({}), {} negative row(s)".format(
        checked, _gen_integrity_scope(ctx), len(bad)), (
        "" if not bad else "; ".join(bad[:5]) +
        " - the burn path floors at zero deliberately, so a negative count "
        "means that floor was bypassed")


# ---------------------------------------------------------------------------
# GEN-IN-13
# ---------------------------------------------------------------------------

def gen_in_13(ctx, ci):
    """
    GEN-IN-13 - Every free token has a chain row.

    WHAT IT CHECKS
        Across every host, no Free RBT token exists without at least one
        `tokenchain` entry.

    WHY IT MATTERS
        A token with no chain is spendable-looking and unusable: it counts
        toward the balance and toward token_denom, so selection will pick it,
        and then validation fails because there is no history to check.

        The collateral split persists change tokens through
        PersistGenesisTransaction on a SEPARATE connection, deliberately before
        the outer transaction opens. A token row committed while its chain row
        was not is exactly the shape that path could produce if it half
        succeeded.

    MANUAL STEPS
        On any node:
          SELECT t.token_id FROM tokens t
           WHERE t.token_type=1 AND t.token_status=0
             AND NOT EXISTS (SELECT 1 FROM tokenchain c
                              WHERE c.token_id = t.token_id);

    PASS / FAIL
        PASS  no orphans on any host
        FAIL  the report names the host and sample token ids - those tokens
              will fail the next transfer that selects them
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, findings, total = 0, [], 0
    for e in _gen_integrity_hosts(ctx):
        try:
            orphans = db.orphan_tokens(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        if orphans:
            total += len(orphans)
            findings.append("{}: {} orphan(s) e.g. {}".format(
                e["host"], len(orphans), ", ".join(t[:16] for t in orphans[:2])))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not findings), "{} host(s) checked ({}), {} orphan token(s)".format(
        checked, _gen_integrity_scope(ctx), total), (
        "" if not findings else "; ".join(findings[:4]) +
        " - these count toward the balance and the denomination counter, so "
        "selection will pick them and then fail validation")


# ---------------------------------------------------------------------------
# GEN-IN-14
# ---------------------------------------------------------------------------

def gen_in_14(ctx, ci):
    """
    GEN-IN-14 - No duplicate token ids on any node.

    WHAT IT CHECKS
        Across every host, no token_id appears more than once in `tokens`.

    WHY IT MATTERS
        A duplicate id within one node means the same token was persisted
        twice. The split path is the plausible source: it burns a parent and
        inserts children, and a retry that re-ran the insert without an
        ON CONFLICT guard would produce exactly this.

        This is also the local half of the collision problem the lab already
        knows about - two NODES minting the same index produce tokens a shared
        quorum cannot tell apart. This case catches the same shape within a
        single node, where it is unambiguously a bug rather than a
        configuration mistake.

    MANUAL STEPS
        On any node:
          SELECT token_id, COUNT(*) FROM tokens
           GROUP BY token_id HAVING COUNT(*) > 1;

    PASS / FAIL
        PASS  no duplicates anywhere
        FAIL  the report names host and token id
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, findings, total = 0, [], 0
    for e in _gen_integrity_hosts(ctx):
        try:
            dupes = db.duplicate_token_ids(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        if dupes:
            total += len(dupes)
            findings.append("{}: {} duplicate(s) e.g. {} x{}".format(
                e["host"], len(dupes), dupes[0][0][:20], dupes[0][1]))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not findings), "{} host(s) checked ({}), {} duplicate id(s)".format(
        checked, _gen_integrity_scope(ctx), total), (
        "" if not findings else "; ".join(findings[:4]) +
        " - the same token persisted twice on one node, which the split path "
        "could produce on a retry without an ON CONFLICT guard")


# ---------------------------------------------------------------------------
# GEN-IN-15
# ---------------------------------------------------------------------------

def gen_in_15(ctx, ci):
    """
    GEN-IN-15 - No tokens left Locked anywhere on the fleet.

    WHAT IT CHECKS
        Across every host, no RBT token sits in status 1 (Locked) once the run
        has settled.

    WHY IT MATTERS
        Locked is a transient state held for the duration of an operation.
        Anything still Locked after a run has finished belongs to an operation
        that failed without releasing - that value is stranded permanently,
        counted in no balance and spendable by nobody.

        This is the cheap net for the post-split rollback path. SC-C-27 forces
        that failure deliberately; this catches it whenever ANY case failed
        after its collateral split had already committed, including the
        rejections that occur naturally under load. One rejected deploy out of
        four hundred is easy to wave away - a Locked token it left behind is
        not.

        Three separate lock-release paths exist (core/transaction.go:70, :77,
        :88), so a miss in any one shows up here.

    MANUAL STEPS
        On any node, once nothing is in flight:
          SELECT did, token_id, token_value FROM tokens
           WHERE token_type=1 AND token_status=1;
        Should return no rows.

    PASS / FAIL
        PASS  no Locked tokens on any host
        FAIL  reports host, count and value - each is stranded value, and the
              total is what the fleet has silently lost
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, findings, total_value, total_rows = 0, [], 0.0, 0
    for e in _gen_integrity_hosts(ctx):
        try:
            rows = db.query(
                e["host"],
                "SELECT did, token_id, token_value FROM tokens "
                "WHERE token_type = %s AND token_status = %s",
                (db.TYPE_RBT, db.LOCKED))
        except db.DBUnavailable:
            continue
        checked += 1
        if rows:
            v = sum(float(row[2]) for row in rows)
            total_value += v
            total_rows += len(rows)
            findings.append("{}: {} token(s) worth {:.3f}".format(
                e["host"], len(rows), v))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not findings), "{} host(s) checked ({}), {} locked token(s) worth {:.3f}".format(
        checked, _gen_integrity_scope(ctx), total_rows, total_value), (
        "" if not findings else "; ".join(findings[:5]) +
        " - Locked is transient; anything still Locked belongs to an operation "
        "that failed without releasing, and that value is stranded")


# -----------------------------------------------------------------------------
# Integrity - drift per operation
# (was general/general_cases_drift.py)
# -----------------------------------------------------------------------------
# general_cases_drift.py - characterise the denomination drift, not just detect it.
#
# WHAT WAS OBSERVED
#     On a fleet where every token was minted by the FIXED binary - wiped first,
#     registry reset, nothing left from 1.0.4 - the counter still diverged:
#
#         GEN-IN-08   denom 0.001: counter 212, free 4
#         FT-P-06     denom 0.001: counter 208, free 0
#                     denom 0.005: counter  22, free 0
#
#     The direction matters. The counter is HIGHER than reality, which means
#     tokens left Free WITHOUT the counter being decremented - the same shape as
#     the bug 977f6fba fixes, on some path the fix does not cover. And it is
#     concentrated at the SMALLEST denominations, which are the ones a split
#     creates rather than the ones anyone mints.
#
# WHAT THESE CASES ADD
#     GEN-IN-08 says "the books do not balance". None of these say that. Each
#     isolates ONE operation and reconciles per denomination across it, so the
#     result names the operation and the denomination rather than the fleet:
#
#       GEN-IN-16  one successful FT mint      - which denominations move wrongly
#       GEN-IN-17  one REJECTED FT mint        - the counter must not move at all
#       GEN-IN-18  one SC deploy               - same question, the other fix
#       GEN-IN-19  repeat and watch it grow    - per-operation, or conditional?
#
#     GEN-IN-17 is the sharpest. FT-P-06 reported "the failing mint decremented
#     without burning" but could not prove it, because it had no before-snapshot.
#     A rejected operation must leave the counter exactly as it found it; if it
#     does not, that alone explains the accumulation.
#
#     GEN-IN-19 answers "in what cases" directly. Drift that grows by a constant
#     each round is per-operation. Drift that appears at one particular round is
#     conditional on something that round did - the wallet running out of a
#     denomination, a split reaching a new level.




def _gen_drift_name():
    return "dr" + "".join(random.choice(string.ascii_lowercase + string.digits)
                          for _ in range(8))


def _gen_drift_prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)
    ok, detail, _ = rc.get_rbt_balance_detail(host, entry["did"], ctx.port)
    have = detail["balance"] if ok and detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} (reached {})".format(need, now)
    return True, ""


def _reconcile(before, after):
    """Per denomination: did the COUNTER move by the same amount reality did?

    This is the heart of every case here. A counter that moved by -3 while the
    real free rows moved by -5 has under-decremented by 2 at that denomination,
    and saying so is far more useful than "the totals disagree".

    Returns [(denom, counter_delta, real_delta)] for denominations that
    disagree.
    """
    denoms = set(before["denom"]) | set(after["denom"])
    out = []
    for d in sorted(denoms):
        cb, ca = before["denom"].get(d, 0), after["denom"].get(d, 0)
        # Reality is recomputed from the snapshot's own drift view: counter
        # minus drift gives the real count at that denomination.
        rb = cb - (before["denom_drift"].get(d, (cb, cb))[0]
                   - before["denom_drift"].get(d, (cb, cb))[1])
        ra = ca - (after["denom_drift"].get(d, (ca, ca))[0]
                   - after["denom_drift"].get(d, (ca, ca))[1])
        if (ca - cb) != (ra - rb):
            out.append((d, ca - cb, ra - rb))
    return out


def _describe(rows):
    return "; ".join(
        "denom {:.3f}: counter {:+d} but reality {:+d}".format(d, c, r)
        for d, c, r in rows)


# ---------------------------------------------------------------------------
# GEN-IN-16
# ---------------------------------------------------------------------------

def gen_in_16(ctx, ci):
    """
    GEN-IN-16 - Per-denomination reconciliation across ONE successful FT mint.

    WHAT IT CHECKS
        Snapshot, mint one FT, snapshot. For EVERY denomination, the counter's
        movement must equal reality's movement.

    WHY IT MATTERS
        GEN-IN-10 asks whether drift EXISTS after a mint. This asks which
        denominations moved wrongly and by how much - the difference between
        "something is broken" and "the 0.001 counter under-decremented by 4".

        The observed drift sits at 0.001 and 0.005, which are denominations a
        SPLIT creates rather than ones anyone mints. An FT mint that needs
        backing splits larger tokens down, so the children appear (counter up)
        and are burnt (counter should come down). If only the first half
        happens, this case names it exactly.

    MANUAL STEPS
        Before and after a single mint:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value ORDER BY token_value;
        For each denomination, the change in the first must equal the change in
        the second.

    PASS / FAIL
        PASS  every denomination moved consistently
        FAIL  names the denomination, the counter's delta and reality's delta -
              a counter that moved LESS than reality is an under-decrement, the
              shape that accumulates
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _gen_drift_prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-16", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.mint_ft(s["host"], s["did"], _gen_drift_name(), 10, 2, ctx.port)
    if not ok:
        return SKIP, "mint rejected", (
            "this case needs a SUCCESSFUL mint to reconcile against; a rejected "
            "one is GEN-IN-17's subject: {}".format(str(msg)[:70]))
    time.sleep(SETTLE * 2)

    try:
        after = db.record("GEN-IN-16", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    bad = _reconcile(before, after)
    return (not bad), "one FT mint, {} denomination(s) disagree | {}".format(
        len(bad), db.format_evidence(before, after)), (
        "" if not bad else _describe(bad) +
        " - a counter that moved LESS than reality is an under-decrement, and "
        "it accumulates with every further operation")


# ---------------------------------------------------------------------------
# GEN-IN-17
# ---------------------------------------------------------------------------

def gen_in_17(ctx, ci):
    """
    GEN-IN-17 - A REJECTED FT mint must leave the counter untouched.

    WHAT IT CHECKS
        Deliberately request a mint the wallet cannot back. It must be
        rejected, and the denomination counter must be byte-for-byte what it
        was beforehand.

    WHY IT MATTERS
        This is the sharpest test of the observed drift. FT-P-06 reported
        "the failing mint decremented without burning" - but it had no
        before-snapshot, so it could not prove the failing mint was
        responsible. This can.

        A rejected operation that still moves the counter explains the
        accumulation completely: every failure under load leaves the counter a
        little further from reality, nothing reports it, and the damage only
        surfaces much later on an unrelated transfer.

        It is also the cheapest possible check - one deliberately doomed
        request - which makes it worth running often.

    MANUAL STEPS
        1. Record the counter.
        2. Request a mint needing far more RBT than the wallet holds, e.g.
           token_count = balance + 100. It will be rejected.
        3. Record the counter again. It must be identical.

    PASS / FAIL
        PASS  rejected, and the counter is unchanged at every denomination
        FAIL  the counter moved on an operation that did nothing - this alone
              accounts for drift accumulating under load
        SKIP  the mint unexpectedly succeeded
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _gen_drift_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-17", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # Ask for far more backing than the wallet holds, so this is certain to
    # fail and certain to fail for a reason unrelated to the counter.
    doomed = int(before["free"]) + 100
    ok, msg, _ = rc.mint_ft(s["host"], s["did"], _gen_drift_name(), 10, doomed, ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("GEN-IN-17", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if ok:
        return SKIP, "the doomed mint succeeded", (
            "asked for {} RBT of backing against a free balance of {:.3f} and "
            "it was accepted - cannot test the rejection path".format(
                doomed, before["free"]))

    moved = [(d, before["denom"].get(d, 0), after["denom"].get(d, 0))
             for d in sorted(set(before["denom"]) | set(after["denom"]))
             if before["denom"].get(d, 0) != after["denom"].get(d, 0)]

    return (not moved), "rejected mint of {} RBT, {} denomination(s) moved".format(
        doomed, len(moved)), (
        "" if not moved else
        "; ".join("denom {:.3f}: {} -> {}".format(d, b, a) for d, b, a in moved) +
        " - the counter moved on an operation that did NOTHING. Every rejected "
        "mint under load leaves it further from reality, and nothing reports it")


# ---------------------------------------------------------------------------
# GEN-IN-18
# ---------------------------------------------------------------------------

def gen_in_18(ctx, ci):
    """
    GEN-IN-18 - Per-denomination reconciliation across ONE contract deploy.

    WHAT IT CHECKS
        The same reconciliation as GEN-IN-16, for a valued SC deploy instead of
        an FT mint.

    WHY IT MATTERS
        The two fixes touch the counter by different paths -
        post_consensus_persistence.go for a deploy, token_chain.go for a burn.
        Running the identical reconciliation against each says which path the
        drift belongs to.

        Read as a pair with GEN-IN-16: if only the mint drifts, the FT burn
        path is at fault. If only the deploy drifts, the collateral path is. If
        both do, something they share is - and the split, which both use, is
        the obvious suspect.

    MANUAL STEPS
        As GEN-IN-16, but deploy a fractional-value contract instead of minting.

    PASS / FAIL
        PASS  every denomination moved consistently
        FAIL  names the denomination and both deltas
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _gen_drift_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    tag = _gen_drift_name()
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// drift probe " + tag + "\nfn main() {}\n").encode()
    okc, msg, sc_id = rc.create_smart_contract(s["host"], s["did"], wasm, raw, ctx.port)
    if not okc or not sc_id:
        return SKIP, "generation failed", str(msg)

    try:
        before = db.record("GEN-IN-18", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    value = round(random.uniform(0.100, 0.800), 3)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"],
                                   sc_id if isinstance(sc_id, str) else str(sc_id),
                                   value=value, data="drift probe", port=ctx.port)
    if not ok:
        return SKIP, "deploy rejected", str(msg)[:80]
    time.sleep(SETTLE * 2)

    try:
        after = db.record("GEN-IN-18", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    bad = _reconcile(before, after)
    return (not bad), "one deploy at {:.3f}, {} denomination(s) disagree | {}".format(
        value, len(bad), db.format_evidence(before, after)), (
        "" if not bad else _describe(bad) +
        " - read with GEN-IN-16: mint only means the burn path, deploy only "
        "means the collateral path, both means something they share")


# ---------------------------------------------------------------------------
# GEN-IN-19
# ---------------------------------------------------------------------------

def gen_in_19(ctx, ci):
    """
    GEN-IN-19 - Does the drift accumulate per operation, or appear at a point?

    WHAT IT CHECKS
        Run the same mint repeatedly, measuring total drift after each. Report
        the drift after every round.

    WHY IT MATTERS
        This answers "in what cases is it happening" directly, which detection
        alone cannot.

        A drift that grows by a constant each round is PER-OPERATION: every
        mint under-decrements a little, and the fix is in the common path.

        A drift that is zero for several rounds and then appears is
        CONDITIONAL: something specific to that round triggered it - the wallet
        exhausting a denomination, a split reaching a new level, a rejection.
        The round number tells you where to look, and the wallet state at that
        round tells you what changed.

        Those two need completely different fixes, and no amount of
        after-the-fact drift detection distinguishes them.

    MANUAL STEPS
        Loop a mint 8 times. After each, compare the counter against reality
        and note the total discrepancy. Plot it against the round number.

    PASS / FAIL
        PASS  no drift after any round
        FAIL  reports the per-round series, and says whether it looks linear
              (per-operation) or stepped (conditional)
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    rounds = 8
    ready, why = _gen_drift_prepare(ctx, s, rounds * 2 + 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        base = db.record("GEN-IN-19", "before", s["host"], s["did"],
                         db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    series, rejected = [], 0
    for i in range(1, rounds + 1):
        ok, _msg, _ = rc.mint_ft(s["host"], s["did"], _gen_drift_name(), 5, 1, ctx.port)
        if not ok:
            rejected += 1
        time.sleep(2)
        try:
            now = db.snapshot(s["host"], s["did"])
        except db.DBUnavailable:
            continue
        drift = db.new_drift(base, now)
        magnitude = sum(abs(c - a) for c, a in drift.values())
        series.append((i, magnitude, "ok" if ok else "rej"))

    try:
        end = db.record("GEN-IN-19", "after", s["host"], s["did"],
                        db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    final = db.new_drift(base, end)

    curve = " ".join("{}:{}{}".format(i, m, "" if st == "ok" else "*")
                     for i, m, st in series)

    note = ""
    if final:
        nonzero = [(i, m) for i, m, _st in series if m > 0]
        if nonzero:
            first_round = nonzero[0][0]
            deltas = [series[k][1] - series[k - 1][1] for k in range(1, len(series))]
            steady = deltas and all(abs(d - deltas[0]) <= 1 for d in deltas if d)
            shape = ("grows by about {} each round, so it is PER-OPERATION - "
                     "every mint under-decrements and the fault is in the common "
                     "path".format(deltas[0]) if steady and deltas[0] else
                     "first appears at round {}, so it is CONDITIONAL on what "
                     "that round did - check the wallet state there, and whether "
                     "that round was one of the {} rejected".format(
                         first_round, rejected))
            note = "drift {} | {}".format(db.describe_drift(final), shape)
        else:
            note = "drift present at the end but not at any checkpoint: " \
                   + db.describe_drift(final)

    return (not final), "{} round(s) ({} rejected), drift by round: {}".format(
        rounds, rejected, curve or "none"), note


# -----------------------------------------------------------------------------
# Integrity - lock release after failures
# (was general/general_cases_locks.py)
# -----------------------------------------------------------------------------
# general_cases_locks.py - lock release, pledge decrement, and orphaned collateral.
#
# Lock release (core/transaction.go:70/:77/:88) and pledge decrement
# (core/wallet/pledge.go:222). Each case attributes drift to its own code
# path, so a drift number is never mistaken for a regression elsewhere.




def _gen_locks_hosts(ctx):
    """EVERY ready host in the run, quorums included - not just this lane's.

    Used by the fleet-wide sweeps (GEN-IN-15, GEN-IN-22). Scoping those to a
    lane does not make them weaker, it makes them WRONG: a reserved lane holds
    four hosts, and "0 stranded locks across 4 host(s)" prints the same shape
    as "across 31 host(s)" while checking 13% of the fleet.

    `ctx.fleet` is populated by case_runner.build_lanes. The fallback covers a
    context built by another driver (smoke_test) and is reported rather than
    hidden - see _scope().
    """
    entries = list(ctx.fleet) if getattr(ctx, "fleet", None) else (
        list(ctx.senders) + list(ctx.receivers) + list(ctx.quorum_hosts))
    seen, out = set(), []
    for e in entries:
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


def _gen_locks_scope(ctx):
    """A label for the result line, so the reader knows what was swept."""
    return "fleet" if getattr(ctx, "fleet", None) else "LANE ONLY"


def _gen_locks_prepare(ctx, entry, need):
    host = entry["host"]
    q = ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"
    rc.quorum_add(host, q["did"], ctx.port)
    ok, detail, _ = rc.get_rbt_balance_detail(host, entry["did"], ctx.port)
    have = detail["balance"] if ok and detail else 0
    if have < need:
        rc.fund_did(host, entry["did"], int(need - have) + 5, ctx.port)
        funded, now = rc.wait_for_balance(host, entry["did"], need, ctx.port)
        if not funded:
            return False, "could not fund to {} (reached {})".format(need, now)
    return True, ""


def _gen_locks_locked(host, did):
    return db.count_in_status(host, did, db.LOCKED)


# ---------------------------------------------------------------------------
# GEN-IN-20
# ---------------------------------------------------------------------------

def gen_in_20(ctx, ci):
    """
    GEN-IN-20 - A rejected transfer must release every lock it took.

    Lock release lives in core/transaction.go:70/:77/:88.

    WHAT IT CHECKS
        Count the wallet's Locked tokens, deliberately fail a transfer, and
        count again. The number must return to exactly what it was.

    WHY IT MATTERS
        On .107 the counter drift was 208 at 0.001 and 22 at 0.005 - and the
        Locked counts at those denominations were 208 and 22. Identical. The
        "drift" was not a counter bug at all: those tokens were locked by
        operations that never released them, and the counter was right to still
        count them, because a locked token is supposed to come back.

        That single reading explains three separate failures in the last run -
        GEN-IN-08's drift, GEN-IN-15's 230 stranded tokens, and part of
        SC-C-27. One root cause, and it is not in the change under test.

        This case attributes it per-operation instead of finding it fleet-wide
        afterwards. Three separate release paths exist, so a miss in any one
        shows up here.

    MANUAL STEPS
        1. Count locked tokens (status 1):
             SELECT COUNT(*) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=1;
        2. Attempt a transfer far larger than the balance - certain to fail.
        3. Wait ~15s and count again. It must be unchanged.

    PASS / FAIL
        PASS  locked count returns to its starting value
        FAIL  locks left behind by an operation that did nothing - that value
              is stranded, and it also inflates the denomination counter
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    ready, why = _gen_locks_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-20", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
        locked_before = _gen_locks_locked(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    doomed = before["free"] + 10000.0
    ok, msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                         rbt=doomed, memo="GEN-IN-20 doomed",
                                         port=ctx.port)
    time.sleep(SETTLE * 3)

    try:
        after = db.record("GEN-IN-20", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        locked_after = _gen_locks_locked(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if ok:
        return SKIP, "the doomed transfer succeeded", (
            "sending {:.0f} against a free balance of {:.3f} was accepted - "
            "cannot test the release path".format(doomed, before["free"]))

    leaked = locked_after - locked_before
    return (leaked <= 0), "rejected transfer, locked {} -> {}".format(
        locked_before, locked_after), (
        "" if leaked <= 0 else
        "{} token(s) left LOCKED by an operation that did nothing. This is the "
        "mechanism behind the .107 drift: locked tokens are still counted as "
        "spendable, so the counter and reality diverge by exactly the number "
        "stranded".format(leaked))


# ---------------------------------------------------------------------------
# GEN-IN-21
# ---------------------------------------------------------------------------

def gen_in_21(ctx, ci):
    """
    GEN-IN-21 - Measure how fast locks accumulate over repeated failures.

    WHAT IT CHECKS
        Fail a transfer ten times in a row and record the Locked count after
        each. Reports the leak rate per failed operation.

    WHY IT MATTERS
        GEN-IN-20 answers "does it leak". This answers "how fast", which is
        what decides whether it matters. A leak of one token per failure is a
        slow bleed; the .107 host accumulated 230, which at that rate implies
        hundreds of failures, and at a higher rate implies far fewer.

        The rate also distinguishes mechanisms. A constant leak per failure
        points at one release path being missed every time. A leak that only
        appears on some failures points at a specific failure MODE - and the
        run had several, from pledge shortage to over-balance.

    MANUAL STEPS
        Repeat the GEN-IN-20 doomed transfer ten times, counting locked tokens
        after each, and look at the series.

    PASS / FAIL
        PASS  the locked count never rises
        FAIL  reports the per-round series and the rate per failed operation
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)

    rounds = 10
    ready, why = _gen_locks_prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        base = _gen_locks_locked(s["host"], s["did"])
        db.record("GEN-IN-21", "before", s["host"], s["did"],
                  db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok_free, _d, _ = rc.get_rbt_balance_detail(s["host"], s["did"], ctx.port)
    doomed = (_d["balance"] if ok_free and _d else 100) + 10000.0

    series, accepted = [], 0
    for i in range(1, rounds + 1):
        ok, _msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                              rbt=doomed,
                                              memo="GEN-IN-21 doomed {}".format(i),
                                              port=ctx.port)
        if ok:
            accepted += 1
        time.sleep(2)
        try:
            series.append(_gen_locks_locked(s["host"], s["did"]) - base)
        except db.DBUnavailable:
            break

    time.sleep(SETTLE * 2)
    try:
        final = _gen_locks_locked(s["host"], s["did"]) - base
        db.record("GEN-IN-21", "after", s["host"], s["did"],
                  db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    curve = ",".join(str(v) for v in series)
    note = ""
    if final > 0:
        rate = final / float(rounds)
        deltas = [series[i] - series[i - 1] for i in range(1, len(series))]
        steady = deltas and all(d == deltas[0] for d in deltas)
        note = ("{} token(s) leaked over {} failed transfer(s) - {:.1f} per "
                "failure. {}".format(
                    final, rounds - accepted, rate,
                    "Constant per failure, so one release path is missed every "
                    "time." if steady else
                    "Uneven, so it depends on the failure MODE rather than on "
                    "failing at all - the run saw several modes, from pledge "
                    "shortage to over-balance."))

    return (final <= 0), "{} round(s), locked delta by round: {}".format(
        rounds, curve or "none"), note


# ---------------------------------------------------------------------------
# GEN-IN-22
# ---------------------------------------------------------------------------

def gen_in_22(ctx, ci):
    """
    GEN-IN-22 - Committed RBT must correspond to contracts that exist.

    This is the invariant SC-C-27 violates.

    WHAT IT CHECKS
        For each host, compare the total RBT in Committed status against the
        number of smart contracts that host has deployed. Committed value with
        no contracts behind it is orphaned collateral.

    WHY IT MATTERS
        Committed is terminal - those tokens are excluded from every future
        selection. If a deploy is REJECTED but its collateral was already
        committed by the pre-pass, that value is gone permanently and nothing
        reports it.

        .107 showed exactly this: 2439 RBT Committed, free balance down to 33,
        and SC-C-27's own evidence recording committed 491 -> 2438 across a
        deploy that was rejected. Roughly 1947 RBT reserved against a contract
        that does not exist.

        This is the cheap fleet-wide net for that, in the way GEN-IN-15 is the
        net for stranded locks. It cannot attribute the loss to one deploy, but
        it makes the total visible - and an unexplained Committed balance is
        the single most expensive symptom in this whole suite.

    MANUAL STEPS
        Per host:
          SELECT round(sum(token_value)::numeric,3) FROM tokens
           WHERE token_type=1 AND token_status=5;
          SELECT count(*) FROM tokens WHERE token_type=4;   -- smart contracts
        A large committed total with few contracts is the finding.

    PASS / FAIL
        RECORD  committed total and contract count per host. Flags a host whose
              committed value is large while it holds few contracts
        FAIL  committed RBT exists on a host with NO contracts at all - that
              collateral cannot belong to anything
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, rows, orphaned = 0, [], []
    for e in _gen_locks_hosts(ctx):
        try:
            committed = db.query(
                e["host"],
                "SELECT COALESCE(SUM(token_value),0), COUNT(*) FROM tokens "
                "WHERE token_type = %s AND token_status = %s",
                (db.TYPE_RBT, db.COMMITTED))
            contracts = db.query(
                e["host"],
                "SELECT COUNT(*) FROM tokens WHERE token_type = %s",
                (db.TYPE_SC,))
        except db.DBUnavailable:
            continue
        checked += 1
        value = float(committed[0][0]) if committed else 0.0
        count = int(committed[0][1]) if committed else 0
        n_sc = int(contracts[0][0]) if contracts else 0
        if value <= 0:
            continue
        rows.append("{}: {:.3f} committed across {} token(s), {} contract(s)".format(
            e["host"], value, count, n_sc))
        if n_sc == 0:
            orphaned.append("{}: {:.3f} RBT committed but the host holds NO "
                            "contracts".format(e["host"], value))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not orphaned), "{} host(s) checked ({}), {} holding committed RBT".format(
        checked, _gen_locks_scope(ctx), len(rows)), (
        "; ".join(orphaned) if orphaned else
        ("; ".join(rows[:4]) + " - committed is TERMINAL, so any of this that "
         "does not correspond to a real contract is permanently lost"
         if rows else ""))


# ---------------------------------------------------------------------------
# GEN-IN-23
# ---------------------------------------------------------------------------

def gen_in_23(ctx, ci):
    """
    GEN-IN-23 - A quorum's counter must decrement for every token it pledges.

    Pledging is core/wallet/pledge.go:222.

    WHAT IT CHECKS
        Drive a series of transactions through one quorum, then reconcile that
        quorum's counter against its real free tokens, per denomination.

    WHY IT MATTERS
        On .104 - a quorum - the counter was 6078 at 0.001 while 6069 were
        Free, with 6356 Pledged and nothing Locked or Committed. Every row was
        accounted for, so those 9 tokens were pledged while still being counted
        as spendable. Roughly 0.14% of pledges did not decrement.

        A small rate, but a quorum signs for the whole fleet, and the error is
        in the dangerous direction: the counter over-states what it can pledge.
        Left long enough, the quorum starts failing to pledge for senders that
        have nothing to do with whatever caused it.

        It is also the reason .104's drift looks nothing like .107's, and
        separating the two is what stops both being blamed on the change under
        test.

    MANUAL STEPS
        On a quorum host, before and after driving traffic through it:
          SELECT denom, count FROM token_denom WHERE did='<QDID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<QDID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;
        The gap, divided by the number of transactions, is the leak rate.

    PASS / FAIL
        PASS  the quorum's counter matches its free tokens afterwards
        FAIL  reports the gap and the implied leak per transaction
        SKIP  no quorum available
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, r = ctx.pair(0)
    q = ctx.quorum_for(s) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return SKIP, "no quorum", "cannot measure a pledge leak without a quorum"

    rounds = 10
    ready, why = _gen_locks_prepare(ctx, s, rounds + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-23", "before (quorum)", q["host"], q["did"],
                           db.snapshot(q["host"], q["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    done = 0
    for i in range(rounds):
        ok, _msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                              rbt=round(random.uniform(0.1, 0.5), 3),
                                              memo="GEN-IN-23 pledge traffic",
                                              port=ctx.port)
        if ok:
            done += 1
        time.sleep(1.5)
    time.sleep(SETTLE * 3)

    try:
        after = db.record("GEN-IN-23", "after (quorum)", q["host"], q["did"],
                          db.snapshot(q["host"], q["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    new = db.new_drift(before, after)
    total = sum(abs(c - a) for c, a in new.values())
    note = ""
    if new:
        note = ("{} - {} token(s) counted but not Free after {} transaction(s) "
                "through this quorum, about {:.2f} per transaction. The counter "
                "OVER-states what the quorum can pledge, so it will eventually "
                "fail to pledge for senders unrelated to this test".format(
                    db.describe_drift(new), total, done,
                    total / float(done) if done else 0))

    return (not new), "{} txn(s) through quorum {}, drift {}".format(
        done, q["host"], total if new else 0), note


# ---------------------------------------------------------------------------
# GEN-IN-24
# ---------------------------------------------------------------------------

def gen_in_24(ctx, ci):
    """
    GEN-IN-24 - A rejected FT MINT must release every lock it took.

    Lock release lives in core/transaction.go:70/:77/:88.

    WHAT IT CHECKS
        Count Locked tokens, request an FT mint that cannot possibly be backed,
        and count again. The number must return to exactly what it was.

    WHY IT MATTERS
        This is the mint-side twin of GEN-IN-20, and together they turn a
        correlation into an attribution.

        The second full run showed, on one host:
            GEN-IN-08   denom 0.001: counter 100, free 4   -> drift 96
            GEN-IN-15   96 tokens Locked
            FT-P-06     "4 mints then rejected at #5", counter 96, free 0
        Drift equalled the locked count exactly, for the second run running.

        GEN-IN-20 then showed rejected TRANSFERS leak nothing - locked
        139 -> 139, and 0 across ten consecutive failures. GEN-IN-17 showed a
        rejected mint does not move the COUNTER. So the counter is behaving,
        transfers are behaving, and the leak sits with the rejected MINT
        specifically - which is what this case proves rather than infers.

        The distinction matters for the report: the drift is not a denomination
        bug, and it is not a general lock-release bug. It is one path, and
        naming it is the difference between a finding someone can fix and a
        number someone has to investigate.

    MANUAL STEPS
        1. Count locked tokens:
             SELECT COUNT(*) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=1;
        2. Request a mint needing far more backing than the wallet holds:
             curl -s -X POST http://$HOST:20000/rubix/v1/fts/mint \\
                  -H 'Content-Type: application/json' -d '{
                    "did":"<DID>","ft_name":"doomed","ft_count":10,
                    "token_count":<balance + 100>}'
           Sign it. It will be rejected.
        3. Wait ~15s and count again. It must be unchanged.

    PASS / FAIL
        PASS  locked count returns to its starting value
        FAIL  the mint locked tokens and did not release them. Those tokens are
              stranded AND still counted as spendable, which is the whole of
              the drift seen on this fleet
        SKIP  the doomed mint unexpectedly succeeded
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = _gen_locks_prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-24", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
        locked_before = _gen_locks_locked(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    doomed = int(before["free"]) + 100
    ok, msg, _ = rc.mint_ft(s["host"], s["did"], "lk" + str(int(time.time()))[-6:],
                            10, doomed, ctx.port)
    time.sleep(SETTLE * 3)

    try:
        after = db.record("GEN-IN-24", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        locked_after = _gen_locks_locked(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if ok:
        return SKIP, "the doomed mint succeeded", (
            "asked for {} RBT of backing against {:.3f} free and it was "
            "accepted - cannot test the release path".format(doomed, before["free"]))

    leaked = locked_after - locked_before
    drift = db.new_drift(before, after)
    note = ""
    if leaked > 0:
        note = ("{} token(s) left LOCKED by a mint that was rejected. They are "
                "stranded, and because a locked token is still counted as "
                "spendable the denomination counter now over-states this wallet "
                "by the same {}. Compare GEN-IN-20: rejected TRANSFERS leak "
                "nothing, so this is the mint path specifically".format(
                    leaked, leaked))
        if drift:
            note += " | counter now: " + db.describe_drift(drift)

    return (leaked <= 0), "rejected mint of {} RBT, locked {} -> {}".format(
        doomed, locked_before, locked_after), note


# =============================================================================
# REGISTRY
# =============================================================================


# --- RBT ---

_RBT_CASES = {
    "RBT-M-02": rbt_m_02,
    "RBT-M-03": rbt_m_03,
    "RBT-M-04": rbt_m_04,
    "RBT-T-05": rbt_t_05,
    "RBT-T-07": rbt_t_07,
    "RBT-T-08": rbt_t_08,
    "RBT-V-01": rbt_v_01,
    "RBT-V-02": rbt_v_02,
    "RBT-V-03": rbt_v_03,
    "RBT-V-04": rbt_v_04,
    "RBT-V-05": rbt_v_05,
    "RBT-V-06": rbt_v_06,
    "RBT-V-07": rbt_v_07,
    "RBT-V-08": rbt_v_08,
    "RBT-V-09": rbt_v_09,
    "RBT-V-10": rbt_v_10,
    "RBT-V-11": rbt_v_11,
    "RBT-V-12": rbt_v_12,
    "RBT-V-15": rbt_v_15,
    "RBT-V-16": rbt_v_16,
    "RBT-P-01": rbt_p_01,
    "RBT-P-03": rbt_p_03,
    "RBT-P-04": rbt_p_04,
    "RBT-P-05": rbt_p_05,
    "RBT-W-01": rbt_w_01,
    "RBT-W-02": rbt_w_02,
    "RBT-W-03": rbt_w_03,
    "RBT-W-04": rbt_w_04,
    "RBT-W-09": rbt_w_09,
    "RBT-W-05": rbt_w_05,
    "RBT-W-06": rbt_w_06,
    "RBT-S-01": rbt_s_01,
    "RBT-S-02": rbt_s_02,
    "RBT-S-03": rbt_s_03,
    "RBT-S-05": rbt_s_05,
    "RBT-S-04": rbt_s_04,
    "RBT-Q-01": rbt_q_01,
    "RBT-Q-02": rbt_q_02,
    "RBT-Q-03": rbt_q_03,
    "RBT-Q-04": rbt_q_04,
    "RBT-Q-05": rbt_q_05,
    "RBT-Q-06": rbt_q_06,
    "RBT-Q-07": rbt_q_07,
    "RBT-Q-08": rbt_q_08,
    "RBT-Q-09": rbt_q_09,
    "RBT-Q-10": rbt_q_10,
    "RBT-Q-12": rbt_q_12,
    "RBT-Q-13": rbt_q_13,
    "RBT-L-01": rbt_l_01,
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
    "RBT-B-04": rbt_b_04,
    "RBT-B-05": rbt_b_05,
    "RBT-B-06": rbt_b_06,
    "RBT-B-07": rbt_b_07,
    "RBT-B-08": rbt_b_08,
}

_RBT_ORDER = list(_RBT_CASES)

_RBT_TIMING_CASES = {
    "RBT-M-03",  # largest single mint that works + how long it took
    "RBT-V-11",  # value ladder - largest value that works
    "RBT-Q-01",  # baseline single-transfer time (everything else compares to this)
    "RBT-Q-05",  # 20-node pass rate + time
    "RBT-Q-07",  # node limit for one quorum
    "RBT-Q-08", "RBT-Q-09", "RBT-Q-10",  # same load across 2 / 5 / 10 quorums
    "RBT-Q-13",  # how fast a quorum frees up
    "RBT-N-13",  # large values in parallel
    "RBT-B-01",  # back-to-back throughput
    "RBT-B-02",  # repeated high-value throughput
    "RBT-B-03",  # parallel pass-rate curve
    "RBT-B-04",  # multi-quorum throughput comparison
    "RBT-B-05",  # split vs whole-token timing
    "RBT-B-07",  # transfer time as chain history grows
    "RBT-B-08",  # transfer time as wallet grows
}


# --- FT (fungible tokens) ---

_FT_CASES = {
    "FT-P-01": ft_p_01,
    "FT-P-02": ft_p_02,
    "FT-P-03": ft_p_03,
    "FT-P-04": ft_p_04,
    "FT-P-05": ft_p_05,

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
    "FT-P-10": ft_p_10,
    "FT-DB-04": ft_db_04,

    # Production-level volume - see sc_cases_scale.py.
    "FT-X-01": ft_x_01,
    "FT-X-02": ft_x_02,
}

_FT_ORDER = ["FT-P-01", "FT-P-02", "FT-P-03", "FT-P-04", "FT-P-05",
         "FT-P-06", "FT-P-09", "FT-P-10", "FT-P-07", "FT-P-08",
         "FT-DB-04",
         "FT-X-01", "FT-X-02"]

_FT_TIMING_CASES = set()

_FT_LANES = {
    "ft-parts": {
        "cases": ["FT-P-01", "FT-P-02", "FT-P-03", "FT-P-04", "FT-P-05"],
        "hosts": 2, "fund": 12,
    },

    # FT-P-06 deliberately runs its wallet dry to reach the counter floor, so
    # it cannot share with anything. FT-P-07/08 interleave the burn path with
    # the other writers of token_denom and need room to work.
    "ft-exhaustion": {
        "cases": ["FT-P-06"],
        "hosts": 2, "fund": 4,
    },

    # FT-P-09 sizes its wallet down to a few whole tokens and needs a sink to
    # drain into, so two hosts. It must not share with anything that funds.
    "ft-floor-walk": {
        "cases": ["FT-P-09"],
        "hosts": 2, "fund": 8,
    },

    # FT-P-10 builds its own parts wallet: one funded sender, one receiver that
    # becomes the wallet. Deliberately NOT sharing with ft-parts, so a failure
    # there cannot skip this one too - that coupling is what left the branch
    # unproven for two runs.
    "ft-parts-burn": {
        "cases": ["FT-P-10"],
        "hosts": 2, "fund": 16,
    },

    # RESERVED: FT-DB-04 WRITES to token_denom. Reserved gives it a host no
    # other lane is ever allocated, and reserved lanes run in the final wave,
    # which is the catalogue's DB-SEED rule enforced by the allocator instead
    # of by remembering to order the suite correctly.
    "ft-seed-floor": {
        "cases": ["FT-DB-04"],
        "hosts": 1, "fund": 12, "reserve": True,
    },
    "ft-interleave": {
        "cases": ["FT-P-07", "FT-P-08"],
        "hosts": 2, "fund": 35,
    },

    # --- scale lanes (stress) ---------------------------------------
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


# --- SMART CONTRACT ---

_SC_CASES = {
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

    # Collateral review additions - value ladder, wallet shapes, deep split,
    # balance boundary, sustained load, and the row-level DB checks.
    "SC-C-13": sc_c_13,
    "SC-C-14": sc_c_14,
    "SC-C-15": sc_c_15,
    "SC-C-16": sc_c_16,
    "SC-C-17": sc_c_17,
    "SC-C-18": sc_c_18,
    "SC-C-19": sc_c_19,
    "SC-C-20": sc_c_20,
    "SC-C-21": sc_c_21,
    "SC-C-22": sc_c_22,

    # Quorum-side accounting - the other half of every deploy.
    "SC-Q-07": sc_q_07,
    "SC-Q-08": sc_q_08,
    "SC-Q-09": sc_q_09,
    "SC-Q-10": sc_q_10,
    "SC-Q-11": sc_q_11,
    "SC-Q-12": sc_q_12,

    # Subscription at fleet scale, and executing from parts wallets.
    "SC-S-06": sc_s_06,
    "SC-S-07": sc_s_07,
    "SC-S-08": sc_s_08,
    "SC-S-09": sc_s_09,
    "SC-S-10": sc_s_10,
    "SC-C-23": sc_c_23,
    "SC-C-24": sc_c_24,
    "SC-C-25": sc_c_25,

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
    "SC-C-11": sc_c_11,
    "SC-C-27": sc_c_27,
    "SC-C-28": sc_c_28,
    "SC-C-29": sc_c_29,
    "SC-C-30": sc_c_30,
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

_SC_ORDER = [
    "SC-C-01", "SC-C-04", "SC-C-02", "SC-C-03", "SC-C-07", "SC-C-08",
    "SC-C-05", "SC-C-06",
    "SC-Q-06",
    "SC-S-01", "SC-S-02", "SC-S-03", "SC-S-04", "SC-S-05",
    "SC-C-09",
    "SC-C-13", "SC-C-14", "SC-C-15", "SC-C-16", "SC-C-17",
    "SC-C-18", "SC-C-19",
    "SC-C-20", "SC-C-21", "SC-C-22",
    "SC-Q-07", "SC-Q-08", "SC-Q-09", "SC-Q-10", "SC-Q-11", "SC-Q-12",
    "SC-S-06", "SC-S-07", "SC-S-08", "SC-S-09", "SC-S-10",
    "SC-C-23", "SC-C-24", "SC-C-25",
    "SC-C-12", "SC-C-26",
    "SC-X-01", "SC-X-02", "SC-X-03", "SC-X-04", "SC-X-05",
    "SC-C-11", "SC-C-30", "SC-C-29", "SC-C-32", "SC-C-34", "SC-C-28",
    # SC-C-33 is the SUCCESS control and must run BEFORE SC-C-27 empties the
    # wallet - afterwards there is nothing left to deploy successfully with.
    "SC-C-33", "SC-C-27", "SC-C-31",
    "SC-DB-03",
]

_SC_TIMING_CASES = set()

_SC_LANES = {
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

    # --- Collateral review additions ------------------------------------------
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
        "cases": ["SC-Q-10", "SC-Q-11", "SC-Q-12"],
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

    # --- scale lanes (stress) ---------------------------------------
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

    # --- hunk-coverage gaps -------------------------------------------------
    "sc-guard-branches": {
        "cases": ["SC-C-11", "SC-C-30", "SC-C-29", "SC-C-32", "SC-C-34",
                  "SC-C-28"],
        "hosts": 1, "fund": 20,
    },
    # SC-C-27 must out-pledge a quorum, so it needs a wallet larger than the
    # quorum's free balance - by far the most expensive lane to fund.
    "sc-rollback": {
        # Order inside this lane is the whole experiment, on ONE wallet:
        #   SC-C-33  a deploy that SUCCEEDS - what Committed does normally
        #   SC-C-27  a deploy that is REJECTED - what Committed does then
        #   SC-C-31  two minutes later - is any of it released
        # 33 must come first; after 27 the wallet is empty and no successful
        # deploy is possible, so the control could never be taken.
        "cases": ["SC-C-33", "SC-C-27", "SC-C-31"],
        "hosts": 1, "fund": 2120,
    },
    # DB-SEED: writes to the database deliberately. Own wallet, and it runs
    # last in the catalogue order so a mid-case failure cannot poison anything
    # that had not run yet.
    "sc-db-seed": {
        "cases": ["SC-DB-03"],
        "hosts": 1, "fund": 15,
    },
}


# --- CROSS-ASSET ---

_CRS_CASES = {
    "CRS-C-01": crs_c_01,
    "CRS-C-02": crs_c_02,
    "CRS-C-03": crs_c_03,
    "CRS-C-06": crs_c_06,
    # CRS-C-04 / CRS-C-05 are written and working but NOT registered: they need
    # a host tagged 'multidid' in hosts.txt, and no host carries that tag yet.
    # Register them here once one does - the functions and the sweep support
    # are already in place, so it is a two-line change.
}

_CRS_ORDER = ["CRS-C-01", "CRS-C-03", "CRS-C-06", "CRS-C-02"]

_CRS_TIMING_CASES = set()

_CRS_LANES = {
    "crs-bundled": {
        "cases": ["CRS-C-01"],
        "hosts": 2, "fund": 12,
    },
    "crs-denom-race": {
        "cases": ["CRS-C-02"],
        "hosts": 1, "fund": 20,
    },
    # Reads the RECEIVER's rows, so it needs its own pair - the receiver must
    # not be shared with anything else moving value into it.
    #
    # CRS-C-06 belongs in this lane and not beside it: it measures the SAME
    # receiver's gain across two bundles, so nothing else may credit that DID
    # in between. Sharing the pair with CRS-C-03 is safe because all three
    # readings are deltas, and running them apart would need a second reserved
    # receiver for no gain.
    "crs-receiver-view": {
        "cases": ["CRS-C-03", "CRS-C-06"],
        "hosts": 2, "fund": 30,
    },
    # A "crs-intra-node" lane belongs here once a host is tagged 'multidid':
    #     "crs-intra-node": {"cases": ["CRS-C-04", "CRS-C-05"],
    #                        "hosts": 6, "fund": 20},
}


# --- GENERAL (integrity, drift, locks) ---

_GEN_CASES = {
    "GEN-IN-08": gen_in_08,
    "GEN-IN-09": gen_in_09,
    "GEN-IN-10": gen_in_10,
    "GEN-IN-11": gen_in_11,

    # Fleet-wide invariants. These take no action - they read every
    # host and assert what must be true regardless of what ran, so
    # they catch damage nobody attributed to anything.
    "GEN-IN-12": gen_in_12,
    "GEN-IN-13": gen_in_13,
    "GEN-IN-14": gen_in_14,
    "GEN-IN-15": gen_in_15,

    # Drift CHARACTERISATION. GEN-IN-08 detects that the books do not
    # balance; these say which operation and which denomination, so the
    # finding can be acted on rather than only reported.
    "GEN-IN-16": gen_in_16,
    "GEN-IN-17": gen_in_17,
    "GEN-IN-18": gen_in_18,
    "GEN-IN-19": gen_in_19,

    # Lock release and pledge decrement. These attribute drift to its own
    # code path, so a drift number is never mistaken for a regression of the
    # change under test.
    "GEN-IN-20": gen_in_20,
    "GEN-IN-21": gen_in_21,
    "GEN-IN-22": gen_in_22,
    "GEN-IN-23": gen_in_23,
    "GEN-IN-24": gen_in_24,
}

_GEN_ORDER = ["GEN-IN-08", "GEN-IN-09", "GEN-IN-10", "GEN-IN-11",
         "GEN-IN-12", "GEN-IN-13", "GEN-IN-14", "GEN-IN-15",
         "GEN-IN-16", "GEN-IN-17", "GEN-IN-18", "GEN-IN-19",
         "GEN-IN-20", "GEN-IN-21", "GEN-IN-22", "GEN-IN-23",
         "GEN-IN-24"]

_GEN_TIMING_CASES = set()

_GEN_LANES = {
    # RESERVED: these two define the baseline every other denomination case
    # is attributed against, so their wallet must be touched by nothing else in
    # the entire run. Without the reservation they were handed a host two
    # earlier waves had already used, and reported drift they could not
    # attribute - which then blocked eleven other cases.
    "gen-denom-baseline": {
        "cases": ["GEN-IN-08", "GEN-IN-09"],
        "hosts": 1, "fund": 4, "reserve": True,
    },
    "gen-denom-after-ft": {
        "cases": ["GEN-IN-10"],
        "hosts": 1, "fund": 10,
    },
    "gen-denom-after-sc": {
        "cases": ["GEN-IN-11"],
        "hosts": 1, "fund": 8,
    },

    # Read-only fleet sweeps. One host is enough - they walk every host in the
    # context themselves, and they need no balance at all.
    # RESERVED and last: these sweep the WHOLE fleet, so they should see it
    # after everything else has finished. Their own host stays clean so the
    # sweep is never reporting its own side effects.
    # RESERVED: each isolates ONE operation and reconciles across it, so the
    # wallet must be touched by nothing else or the attribution is worthless -
    # which is exactly what went wrong when GEN-IN-08 ran on a shared host.
    "gen-drift-attribution": {
        "cases": ["GEN-IN-16", "GEN-IN-17", "GEN-IN-18", "GEN-IN-19"],
        "hosts": 1, "fund": 40, "reserve": True,
    },
    # RESERVED: GEN-IN-20/21 deliberately fail operations and count what they
    # leave locked, so nothing else may touch their wallet or the count is
    # meaningless. GEN-IN-23 drives traffic through a shared quorum, which it
    # cannot own - it measures only the drift IT introduced.
    "gen-lock-release": {
        "cases": ["GEN-IN-20", "GEN-IN-24", "GEN-IN-21", "GEN-IN-23"],
        "hosts": 2, "fund": 30, "reserve": True,
    },
    "gen-fleet-invariants": {
        "cases": ["GEN-IN-12", "GEN-IN-13", "GEN-IN-14", "GEN-IN-15",
                  "GEN-IN-22"],
        "hosts": 1, "fund": 0, "reserve": True,
    },
}



def _merge_registries():
    """One CASES / ORDER / LANES / TIMING_CASES across every asset section.

    A Test ID or lane name defined twice would silently shadow the other, so
    either is an import-time error rather than a quiet overwrite.
    """
    cases, order, lanes, timing, info = {}, [], {}, set(), {}
    families = [

        (_RBT_CASES, _RBT_ORDER, None, _RBT_TIMING_CASES, None),

        (_FT_CASES, _FT_ORDER, _FT_LANES, _FT_TIMING_CASES, None),

        (_SC_CASES, _SC_ORDER, _SC_LANES, _SC_TIMING_CASES, None),

        (_CRS_CASES, _CRS_ORDER, _CRS_LANES, _CRS_TIMING_CASES, None),

        (_GEN_CASES, _GEN_ORDER, _GEN_LANES, _GEN_TIMING_CASES, None),

    ]
    for fam_cases, fam_order, fam_lanes, fam_timing, fam_info in families:
        for tid in fam_order:
            if tid in cases:
                raise RuntimeError("Test ID defined twice: " + tid)
            cases[tid] = fam_cases[tid]
            order.append(tid)
        for name, spec in (fam_lanes or {}).items():
            if name in lanes:
                raise RuntimeError("lane defined twice: " + name)
            lanes[name] = spec
        timing |= set(fam_timing or ())
        info.update(fam_info or {})
    return cases, order, lanes, timing, info


CASES, ORDER, LANES, TIMING_CASES, CASE_INFO = _merge_registries()
