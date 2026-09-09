#!/usr/bin/env python3
"""
general_cases_locks.py - lock release, pledge decrement, and orphaned collateral.

Imported by general_cases.py.

THESE ARE NOT ABOUT PR #739, AND THE REPORT SHOULD SAY SO.
    The second full run found drift on two hosts. Reading the token statuses
    settled where it came from, and neither answer implicates the PR:

      .107   counter 212, free 4, LOCKED 208   -> drift == locked, exactly
             (and 23/1/22 at 0.005; 208+22 = the 230 GEN-IN-15 reported)
      .104   counter 6078, free 6069, PLEDGED 6356, locked 0
             -> ~0.14% of pledges did not decrement

    So .107 is a LOCK RELEASE failure and .104 is a PLEDGE DECREMENT leak.
    Both live on paths this PR does not modify - core/transaction.go:70/:77/:88
    and core/wallet/pledge.go:222.

    The same reading is positive evidence FOR the PR: .107 held 610 BurntForFT
    tokens at 0.001 and 606 Burnt + 168 BurntForFT at 0.005, and the drift
    equals the LOCKED count, not the burnt count. Roughly 1,384 burns all
    decremented correctly. That is the path 977f6fba fixes.

    These cases exist so those findings are attributed to their own code rather
    than to the change under test. A drift number with no attribution attached
    to a PR report is worse than no number at all.
"""

import os
import random
import string
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _hosts(ctx):
    seen, out = set(), []
    for e in list(ctx.senders) + list(ctx.receivers) + list(ctx.quorum_hosts):
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


def _prepare(ctx, entry, need):
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


def _locked(host, did):
    return db.count_in_status(host, did, db.LOCKED)


# ---------------------------------------------------------------------------
# GEN-IN-20
# ---------------------------------------------------------------------------

def gen_in_20(ctx, ci):
    """
    GEN-IN-20 - A rejected transfer must release every lock it took.

    NOT A PR #739 CASE. Lock release lives in core/transaction.go:70/:77/:88,
    which this PR does not touch.

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

    ready, why = _prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-20", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
        locked_before = _locked(s["host"], s["did"])
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
        locked_after = _locked(s["host"], s["did"])
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

    NOT A PR #739 CASE.

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
    ready, why = _prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        base = _locked(s["host"], s["did"])
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
            series.append(_locked(s["host"], s["did"]) - base)
        except db.DBUnavailable:
            break

    time.sleep(SETTLE * 2)
    try:
        final = _locked(s["host"], s["did"]) - base
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

    NOT A PR #739 CASE in origin, though it is the invariant SC-C-27 violates.

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
    for e in _hosts(ctx):
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
    return (not orphaned), "{} host(s) checked, {} holding committed RBT".format(
        checked, len(rows)), (
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

    NOT A PR #739 CASE. Pledging is core/wallet/pledge.go:222, untouched here.

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
    ready, why = _prepare(ctx, s, rounds + 8)
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
