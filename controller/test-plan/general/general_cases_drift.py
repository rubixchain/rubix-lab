#!/usr/bin/env python3
"""
general_cases_drift.py - characterise the denomination drift, not just detect it.

Imported by general_cases.py.

WHAT WAS OBSERVED
    On a fleet where every token was minted by the FIXED binary - wiped first,
    registry reset, nothing left from 1.0.4 - the counter still diverged:

        GEN-IN-08   denom 0.001: counter 212, free 4
        FT-P-06     denom 0.001: counter 208, free 0
                    denom 0.005: counter  22, free 0

    The direction matters. The counter is HIGHER than reality, which means
    tokens left Free WITHOUT the counter being decremented - the same shape as
    the bug 977f6fba fixes, on some path the fix does not cover. And it is
    concentrated at the SMALLEST denominations, which are the ones a split
    creates rather than the ones anyone mints.

WHAT THESE CASES ADD
    GEN-IN-08 says "the books do not balance". None of these say that. Each
    isolates ONE operation and reconciles per denomination across it, so the
    result names the operation and the denomination rather than the fleet:

      GEN-IN-16  one successful FT mint      - which denominations move wrongly
      GEN-IN-17  one REJECTED FT mint        - the counter must not move at all
      GEN-IN-18  one SC deploy               - same question, the other fix
      GEN-IN-19  repeat and watch it grow    - per-operation, or conditional?

    GEN-IN-17 is the sharpest. FT-P-06 reported "the failing mint decremented
    without burning" but could not prove it, because it had no before-snapshot.
    A rejected operation must leave the counter exactly as it found it; if it
    does not, that alone explains the accumulation.

    GEN-IN-19 answers "in what cases" directly. Drift that grows by a constant
    each round is per-operation. Drift that appears at one particular round is
    conditional on something that round did - the wallet running out of a
    denomination, a split reaching a new level.
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


def _name():
    return "dr" + "".join(random.choice(string.ascii_lowercase + string.digits)
                          for _ in range(8))


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

    ready, why = _prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("GEN-IN-16", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 10, 2, ctx.port)
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

    ready, why = _prepare(ctx, s, 8)
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
    ok, msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 10, doomed, ctx.port)
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

    ready, why = _prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    tag = _name()
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
    ready, why = _prepare(ctx, s, rounds * 2 + 12)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        base = db.record("GEN-IN-19", "before", s["host"], s["did"],
                         db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    series, rejected = [], 0
    for i in range(1, rounds + 1):
        ok, _msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, 1, ctx.port)
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
