"""rbt_cases.py - RBT cases: value and precision ladders, wallet shape,
splits, quorum capacity, pledging, concurrency, failure handling, bulk.

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
    SKIP,
    TOL,
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
    """Value ladder: climb until it fails, RECORD the largest that worked and
    how long each rung took. The regression signal is the limit dropping."""
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
    return True, "largest value that worked: {} RBT".format(largest), \
        "{} | {}".format(", ".join(rungs), failure or "ladder not exhausted "
                         "(--value-ceiling {})".format(int(ceiling)))


def rbt_p_04(ctx, ci):
    """0.001 x N (catalogue: 1000). Every send splits, and the receiver's total
    must equal exactly N x 0.001 - rounding drift accumulates here first."""
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
            "ran {} (--repeat-count; catalogue asks 1000)".format(n)
    return False, "drift detected", "expected {} moved {}".format(expected, moved)


# ---------------------------------------------------------------------------
# Wallet shape (034-040)
# ---------------------------------------------------------------------------
# RBT-W-03 and RBT-B-08 share pair 13: B-08 grows that sender's wallet, and
# W-03 pays its receiver. It keeps what it holds for the next cycle.
_BIG_WALLET_PAIR = 13


def rbt_w_03(ctx, ci):
    """Thousands of tiny parts summing past 100, then send 100.

    The parts arrive as fractional transfers from a second DID, so the sender
    spends parts it did not split itself - the path the minter-allowlist
    genesis lookup gets wrong on builds without the part-token fixes. A
    rejection naming ValidateMinterAllowlist is that known bug, not a new one."""
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
    failed = 0
    for _ in range(count):
        status, _m = _transfer(ctx, feeder, s, value, "RBT-W-03-build")
        failed += 0 if status else 1
    built, s1 = rc.wait_for_balance(s["host"], s["did"], s0 + value * (count - failed) - TOL,
                                    ctx.port, attempts=30)
    if failed or not built:
        return False, "could not build the parts wallet", \
            "{}/{} feeder transfers rejected; sender {} -> {}".format(failed, count, s0, s1)
    ok, note = _prepare_sender(ctx, s, 100)
    if not ok:
        return False, "precondition not met", note
    t0 = time.time()
    passed, actual, note = _expect_success(ctx, s, ctx.pair(_BIG_WALLET_PAIR)[1], 100, "RBT-W-03")
    return passed, "100 from {} parts of {} in {}s: {}".format(
        count, value, round(time.time() - t0, 2), actual), note


def rbt_s_02(ctx, ci):
    return SKIP, "not attempted", (
        "KNOWN OPEN BUG (see CLAUDE.md: split-token duplicate key). Reproducing it "
        "requires flipping a parent token's Burnt status directly in Postgres - a "
        "DB-SEED fixture, which is deferred (needs psycopg2 + per-node credentials). "
        "Expected outcome when run: duplicate key on tokens_pkey.")


def rbt_s_04(ctx, ci):
    """Repeated splits on the same wallet, confirming balance after each."""
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
        _prepare_sender(ctx, s, amount + 1)
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


def rbt_q_13(ctx, ci):
    """How fast a quorum frees up: fire back-to-back through one sender and
    record the interval."""
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
            return True, "quorum refused a back-to-back transfer after {} successes".format(
                len(timings) - 1), "timings {}s; msg: {}".format(timings, (msg or "")[:100])
    return True, "{} back-to-back transfers all accepted; per-transfer {}s".format(n, timings), \
        "no interval found at which the quorum refused"


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
        return False, "both competing transfers were rejected", \
            "; ".join((m or "")[:80] for _s, m in results)
    return False, "DOUBLE SPEND: both transfers succeeded", \
        "sender held {} and spent it twice; final balance {}".format(held, final)


def rbt_n_10(ctx, ci):
    """Many transfers from ONE wallet at once."""
    s, r = ctx.pair(7)
    n = 50
    ok, note = _prepare_sender(ctx, s, n + 2)
    if not ok:
        return False, "precondition not met", note
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
    senders = [e for e in ctx.senders + ctx.receivers[1:] if e["did"] != target["did"]]
    for s in senders:
        _prepare_sender(ctx, s, 2)
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
    _prepare_sender(ctx, a, 2)
    _prepare_sender(ctx, b, 2)
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
        _prepare_sender(ctx, s, 12)
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

    Funding happens BEFORE the baseline is measured: a faucet top-up brings
    value in from outside these DIDs, and taking the baseline first would count
    it as a conservation failure. Only the transfer window is measured."""
    everyone = ctx.senders + ctx.receivers
    for s, _r in ctx.pairs:
        _prepare_sender(ctx, s, 2)
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
    n = int(_scale(ctx, "burst_count", 200))
    ok, note = _prepare_sender(ctx, s, n + 2)
    if not ok:
        return False, "precondition not met", note
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
    """1,000 RBT back and forth between one pair, 10 times. Each round the
    quorum pledges 1,000 again, so this is also pledge/unpledge churn."""
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
    """Raise parallel count step by step; RECORD the pass-rate curve."""
    curve = []
    for n in (1, 2, 5, 10, min(20, len(ctx.pairs))):
        if n > len(ctx.pairs):
            break
        usable, ok, elapsed, _d = _concurrent_transfers(ctx, n, memo="RBT-B-03")
        curve.append("{}:{}/{}@{}s".format(usable, ok, usable, elapsed))
    return True, "pass-rate curve -> {}".format(", ".join(curve)), \
        "bounded by fleet size ({} pairs)".format(len(ctx.pairs))


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
    """Time a 1 RBT send as the wallet grows to --wallet-ceiling tokens. The
    curve, not any one point, is the result: it should stay flat."""
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
