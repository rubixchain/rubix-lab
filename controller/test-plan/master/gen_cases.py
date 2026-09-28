"""gen_cases.py - General cases: fleet-wide integrity sweeps, drift over
repeated operations, lock accumulation, quorum pledge accounting.

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
)


# ---------------------------------------------------------------------------
# CRS-C-06
# ---------------------------------------------------------------------------


# CRS-C-01 first: if the bundled call is already wrong when run alone, the
# concurrent case has nothing clean to build on.


# Both cases own their wallet outright - each measures a balance delta and a
# denom delta on one DID, so nothing else may touch it while they run.


# =============================================================================
# GENERAL (integrity, drift, locks)
# =============================================================================


# -----------------------------------------------------------------------------
# Integrity - denomination counter baseline and reconciliation
# -----------------------------------------------------------------------------
#
# Run just this asset:  cd test-plan/full-test && python3 test_runner.py --only 'GEN-*'
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


def _describe_drift(drift):
    return "; ".join(
        "denom {:.3f}: counter says {} but {} are Free".format(d, c, a)
        for d, (c, a) in sorted(drift.items()))


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

    `ctx.fleet` is every DID in the pool, set by test_runner. The fallback exists
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

    `ctx.fleet` is every DID in the pool, set by test_runner. The fallback covers a
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


# --- GENERAL (integrity, drift, locks) ---

CASES = {
    "GEN-IN-08": gen_in_08,
    "GEN-IN-09": gen_in_09,

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
    "GEN-IN-19": gen_in_19,

    # Lock release and pledge decrement. These attribute drift to its own
    # code path, so a drift number is never mistaken for a regression of the
    # change under test.
    "GEN-IN-21": gen_in_21,
    "GEN-IN-22": gen_in_22,
    "GEN-IN-23": gen_in_23,
}

ORDER = ["GEN-IN-08", "GEN-IN-09", "GEN-IN-12", "GEN-IN-13", "GEN-IN-14", "GEN-IN-15",
         "GEN-IN-19",
         "GEN-IN-21", "GEN-IN-22", "GEN-IN-23"]

TIMING_CASES = set()

# What each unit of cases needs - see NEEDS in full-test/test_runner.py.
# receivers 0 = the sender receives too. last = runs after everything else.
NEEDS = {
    # These measure drift and leftover locks against a wallet, so they run
    # LAST, after every other unit, when nothing else is moving value. They
    # still get their own DIDs, which nothing else touches while they run.
    "gen-denom-baseline": {
        "cases": ["GEN-IN-08", "GEN-IN-09"],
        "receivers": 0, "fund": 4, "last": True,
    },
    # GEN-IN-19 isolates ONE operation and reconciles across it.
    "gen-drift-attribution": {
        "cases": ["GEN-IN-19"],
        "receivers": 0, "fund": 40, "last": True,
    },
    # GEN-IN-21 fails operations on purpose and counts what they leave locked;
    # GEN-IN-23 measures the quorum drift its own traffic introduces.
    "gen-lock-release": {
        "cases": ["GEN-IN-21", "GEN-IN-23"],
        "receivers": 1, "fund": 30, "last": True,
    },
    # Read-only sweeps of every DID in the pool: no participants of their own,
    # and they run at the very end with nothing else running, so they see the
    # fleet after everything has finished and nothing moves while they read.
    "gen-fleet-invariants": {
        "cases": ["GEN-IN-12", "GEN-IN-13", "GEN-IN-14", "GEN-IN-15",
                  "GEN-IN-22"],
        "senders": 0, "receivers": 0, "quorums": 0, "exclusive": True, "last": True,
    },
}
