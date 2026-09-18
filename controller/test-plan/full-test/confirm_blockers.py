#!/usr/bin/env python3
"""
confirm_blockers.py - does PR #739 actually block, and on what evidence?

    python3 case_runner.py --suite pr-739-blockers --report-name b-branch
    # deploy the merge-base build (710805f2) to the same hosts
    python3 case_runner.py --suite pr-739-blockers --report-name b-base
    python3 confirm_blockers.py <b-branch.json> <b-base.json>

Change build first, baseline second, so this can gate a merge:

    0   nothing confirmed
    1   at least one blocker CONFIRMED - request changes
    2   nothing decidable, because a deciding case SKIPPED or is missing.
        Deliberately NOT 0. A gate that goes green because nothing looked
        reads exactly like one that goes green because nothing was wrong.


WHY THIS EXISTS AND compare_reports.py DOES NOT REPLACE IT

compare_reports.py answers one question per CASE - does it fail here and pass
there - and for SC-C-27 that is the whole answer. For the other two blockers it
is actively misleading, because they FAIL ON BOTH BUILDS FOR DIFFERENT REASONS:

    SC-C-29   base: ACCEPTED three contracts, charged 3.0 for 0.63
                    (the old no-split bug the PR set out to fix)
              change: REFUSED - "transaction amount exceeds 3 decimal places"

    CRS-C-03  base: the bundled deploy+transfer is REJECTED outright
              change: ACCEPTED, and the receiver is credited +1.206 for a
                      1.0 transfer

Status-only, both read FAIL/FAIL and bucket as PRE-EXISTING. Both are
regressions. The information that separates them lives in the `actual` and
`note` text, so this reads that text.

The second thing it does is group. A blocker is not a case. Blocker 1 is three
cases that only mean something together - a control, an event, and a severity
reading - and answering "is the collateral lost" from SC-C-27 alone is exactly
the inference a reviewer will reject.


HOW TO DISTRUST IT

Every verdict prints the raw `actual` line of every case behind it, from both
builds. If the reasoning and the evidence disagree, the evidence is right and
this script has a bug. It is a reading aid, not an authority.
"""

import argparse
import json
import os
import re
import sys


# ---------------------------------------------------------------------------
# Reading a report
# ---------------------------------------------------------------------------

def load(path):
    """-> (meta, {test_id: row}). Rows carry status / actual / note."""
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("results") or []
    if not rows:
        sys.exit("ERROR: {} contains no results".format(path))
    return payload.get("run") or {}, {r["test_id"]: r for r in rows}


def status(rows, tid):
    """'PASS' / 'FAIL' / 'SKIP', or None when the case is absent entirely.

    Absent and SKIP are deliberately different. A case that did not run proves
    nothing, and collapsing it into a pass is how a gap survives a review.
    """
    row = rows.get(tid)
    return row.get("status") if row else None


def text(rows, tid):
    """actual + note, lowercased - the searchable evidence for one case."""
    row = rows.get(tid) or {}
    return "{} {}".format(row.get("actual") or "", row.get("note") or "").lower()


def says(rows, tid, *needles):
    """True if any needle appears in that case's actual/note text."""
    blob = text(rows, tid)
    return any(n.lower() in blob for n in needles)


def actual(rows, tid):
    row = rows.get(tid)
    if not row:
        return "(case absent from this report)"
    a = row.get("actual") or ""
    return a if a else "(no actual recorded)"


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

CONFIRMED = "CONFIRMED"        # blocks the merge
REFUTED = "REFUTED"            # did not reproduce on this run
DOWNGRADED = "DOWNGRADED"      # real, but not the severity claimed
INCONCLUSIVE = "INCONCLUSIVE"  # a SKIP or a missing case on either side

# Single-build vocabulary, deliberately DIFFERENT words. One build can show
# that a behaviour is real, repeatable and mechanically understood. It cannot
# show that this PR introduced it - only a second build does that. Reusing
# "CONFIRMED" for both would let a one-build run be quoted as if it had
# answered the causation question.
REPRODUCED = "REPRODUCED"
NOT_REPRODUCED = "NOT REPRODUCED"

BLOCKING = (CONFIRMED, REPRODUCED)


# ---------------------------------------------------------------------------
# Blocker 1 - a rejected deploy commits its collateral permanently
# ---------------------------------------------------------------------------

def blocker_1(chg, base):
    """SC-C-33 (control) + SC-C-27 (event) + SC-C-31 (severity) + GEN-IN-22.

    Causation here IS answerable from status: main genuinely passes SC-C-27
    (wallet untouched after a rejection) and the branch genuinely fails it. No
    text reading needed for the flip itself - only for what it cost.
    """
    lines = []
    c27, b27 = status(chg, "SC-C-27"), status(base, "SC-C-27")
    c31 = status(chg, "SC-C-31")
    c33 = status(chg, "SC-C-33")

    if c27 in (None, "SKIP") or b27 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "SC-C-27 is {} on the change and {} on the baseline.".format(
                c27 or "absent", b27 or "absent"),
            "That case IS the blocker - it has to run on both builds. The "
            "usual cause is the rollback lane failing to fund above the "
            "quorum (it needs ~2120 RBT); the skip reason is in the report's "
            "Note column."]

    if c27 != "FAIL":
        return REFUTED, [
            "SC-C-27 did not fail on the change build (status {}).".format(c27),
            "The rejected deploy left the wallet intact, so the collateral is "
            "not being stranded on this run."]

    if b27 != "PASS":
        lines.append(
            "SC-C-27 fails on BOTH builds - this is not something #739 "
            "introduced. It is still real and still worth filing, but it "
            "belongs in its own report, not on this PR.")
        return DOWNGRADED, lines

    lines.append(
        "SC-C-27: PASS on the baseline, FAIL on the change. Same case, same "
        "fleet - the branch caused it. That is the causation question "
        "answered, and nothing but a two-build run answers it.")

    # Severity: is the value actually gone, or merely slow to come back?
    if c31 == "PASS":
        lines.append(
            "SC-C-31 shows the committed value FALLING over its 120s window, "
            "so a release path exists and the loss is not permanent. This is "
            "a real regression in wallet behaviour but NOT unrecoverable "
            "loss - report it as such rather than as value destruction.")
        return DOWNGRADED, lines
    if c31 in (None, "SKIP"):
        lines.append(
            "SC-C-31 is {} - the wait-and-look-again reading was never "
            "taken, so 'the value is lost' is still an inference. It skips "
            "when the host holds under 1 RBT committed, which means SC-C-27 "
            "did not strand anything on this host and the two cases were not "
            "run in the same lane, in order.".format(c31 or "absent"))
        return INCONCLUSIVE, lines

    lines.append(
        "SC-C-31: the committed value had NOT moved 120s after the "
        "rejection. Committed is terminal - there is no path back to Free - "
        "so this is permanent loss, on a path the user cannot avoid.")

    # The control is what turns this from "collateral was committed, which
    # sounds correct" into a precise, actionable statement.
    if c33 == "PASS":
        lines.append(
            "SC-C-33 (the control) shows a SUCCESSFUL deploy committing "
            "exactly the contract value. So the normal path is well-defined "
            "and the finding is precise: a rejected deploy commits the same "
            "value as a successful one and produces no contract for it. The "
            "pre-pass runs before the consensus outcome is known and never "
            "unwinds on failure.")
    elif c33 == "FAIL":
        lines.append(
            "SC-C-33 (the control) ALSO failed - a successful deploy did not "
            "commit the contract value either. The collateral accounting is "
            "wrong on the normal path as well, which is a LARGER finding "
            "than this blocker. Read SC-C-33's note before writing the "
            "report.")
    else:
        lines.append(
            "SC-C-33 (the control) is {} - without it the finding has to be "
            "stated as 'value moved to Committed', which reads as correct "
            "behaviour, because committing collateral is what collateral "
            "is for.".format(c33 or "absent"))

    if status(chg, "GEN-IN-22") == "FAIL":
        lines.append(
            "GEN-IN-22 corroborates fleet-wide: committed RBT sitting on "
            "hosts that hold no contracts to account for it.")

    return CONFIRMED, lines


# ---------------------------------------------------------------------------
# Blocker 2 - multi-contract requests stopped working
# ---------------------------------------------------------------------------

# The branch's refusal string, from core/transaction_builder.go's precision
# guard. Matched loosely because the wrapper text around it has changed before.
DECIMAL_REFUSAL = ("3 decimal places", "exceeds 3 decimal", "decimal places")


def blocker_2(chg, base):
    """SC-C-29 (the regression) + SC-C-32 and SC-C-34 (why).

    Status lies here. SC-C-29 fails on both builds, for opposite reasons: the
    baseline ACCEPTS the three contracts and overcharges (the bug #739 fixes),
    the change REFUSES them. Only the text separates those.
    """
    lines = []
    c29, b29 = status(chg, "SC-C-29"), status(base, "SC-C-29")

    if c29 in (None, "SKIP") or b29 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "SC-C-29 is {} on the change and {} on the baseline - the "
            "regression itself was not observed on both builds.".format(
                c29 or "absent", b29 or "absent")]

    refused_now = says(chg, "SC-C-29", *DECIMAL_REFUSAL) or \
        says(chg, "SC-C-29", "refused", "rejected")
    refused_before = says(base, "SC-C-29", *DECIMAL_REFUSAL) or \
        says(base, "SC-C-29", "refused", "rejected")

    if not refused_now:
        return REFUTED, [
            "SC-C-29 on the change build shows no refusal of the "
            "three-contract request. The regression did not reproduce.",
            "change actual: " + actual(chg, "SC-C-29")]

    if refused_before:
        lines.append(
            "Both builds refuse the three-contract request, so this is NOT "
            "something #739 introduced - it is pre-existing and belongs in "
            "its own report. Read both actual lines below before accepting "
            "that; a refusal for a DIFFERENT reason on each side would still "
            "be a regression.")
        return DOWNGRADED, lines

    lines.append(
        "SC-C-29: the baseline ACCEPTS three contracts in one request; the "
        "change REFUSES them. Status alone buckets this PRE-EXISTING because "
        "the case fails on both sides - it fails on the baseline for "
        "overcharging (3.0 for 0.63, the no-split bug this PR fixes) and on "
        "the change for refusing outright. Opposite failures, and only the "
        "second one is a regression.")

    if says(chg, "SC-C-29", *DECIMAL_REFUSAL):
        lines.append(
            "The refusal is the 3-decimal-places guard, for values that are "
            "each legal at 3dp and whose sum is too - so the number the "
            "guard tested was not the one the caller sent. 0.345+0.359+0.317 "
            "accumulates to 1.0209999999999999 in float64.")

    # SC-C-32 and SC-C-34 decide between the three explanations. Without them
    # the finding is "multi-contract broke" and the author has to go hunting.
    if says(chg, "SC-C-34", "accumulation confirmed on the sum"):
        lines.append(
            "SC-C-34 settles the mechanism: a SINGLE contract at 0.345 is "
            "accepted, 0.25+0.5 at N=2 is accepted, and only 0.1+0.2 at N=2 "
            "is refused. So it is not a per-value guard and not the contract "
            "count - it is accumulation on the SUM, and it bites from N=2 "
            "upward, which is nearly every multi-contract request rather "
            "than an edge case at three.")
    elif says(chg, "SC-C-32", "float accumulation confirmed"):
        lines.append(
            "SC-C-32 confirms accumulation: the binary-exact triple "
            "(0.25+0.5+0.125) is accepted and the inexact one is refused. "
            "SC-C-34 would additionally pin the boundary at N=2 - check "
            "whether it ran.")
    elif says(chg, "SC-C-34", "count is the limit") or \
            says(chg, "SC-C-32", "not supported at all"):
        lines.append(
            "The corroborating cases point at the CONTRACT COUNT, not "
            "arithmetic: even binary-exact values are refused at N>=2. That "
            "is a different fix from a decimal-safe sum, so report it that "
            "way. Read SC-C-32 and SC-C-34's notes in full.")
    else:
        lines.append(
            "Neither SC-C-32 nor SC-C-34 returned a mechanism verdict "
            "(SC-C-32 {}, SC-C-34 {}). The regression stands, but the report "
            "cannot yet say WHY, and 'multi-contract broke' is much harder "
            "to act on than 'the sum accumulates'.".format(
                status(chg, "SC-C-32") or "absent",
                status(chg, "SC-C-34") or "absent"))

    return CONFIRMED, lines


# ---------------------------------------------------------------------------
# Blocker 3 - the receiver is credited the collateral
# ---------------------------------------------------------------------------

def blocker_3(chg, base):
    """CRS-C-03 (observed once) + CRS-C-06 (the proof).

    Status lies here too, and in a way that is easy to miss: on the baseline
    the bundle is REJECTED, so CRS-C-06 correctly SKIPS. A skip on the
    baseline is the expected answer, not a gap - which means this blocker is
    decided almost entirely by the change build alone.
    """
    lines = []
    c03, b03 = status(chg, "CRS-C-03"), status(base, "CRS-C-03")
    c06 = status(chg, "CRS-C-06")

    if c03 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "CRS-C-03 is {} on the change build - the bundled "
            "deploy+transfer was never observed.".format(c03 or "absent")]

    over_credited = says(
        chg, "CRS-C-03",
        "the excess is exactly the contract value",
        "recorded the collateral as its own",
        "receiver free value rose")

    if c03 == "PASS" and c06 in (None, "PASS"):
        return REFUTED, [
            "CRS-C-03 passes on the change build - the receiver gained the "
            "transfer amount only. The over-credit did not reproduce.",
            "change actual: " + actual(chg, "CRS-C-03")]

    # The proof case carries the whole weight: it varies the collateral and
    # requires the excess to track it at two separate points.
    if says(chg, "CRS-C-06", "tracks the collateral",
            "receiver credits the collateral"):
        lines.append(
            "CRS-C-06 PROVES it rather than inferring it: run at two "
            "deliberately different contract values, the receiver's excess "
            "over the transfer equalled the contract value BOTH times, and "
            "the two excesses differ by exactly the difference between the "
            "two values. A coincidental credit does not follow v across "
            "runs; a fixed surcharge does not change with it; a rounding "
            "artefact does not scale. Nothing else is left.")
        if says(chg, "CRS-C-06", "recorded on both nodes", "duplicated, not moved"):
            lines.append(
                "AND the initiator committed the same value for the same "
                "deploys - so the collateral is recorded on BOTH nodes. The "
                "value is duplicated, not misplaced. This is the most "
                "serious reading of the three blockers.")
    elif says(chg, "CRS-C-06", "does not track the collateral"):
        lines.append(
            "CRS-C-06 REFUTES the diagnosis: the receiver gained more than "
            "the transfer, but the excess does not equal the contract value "
            "at both points. Whatever CRS-C-03 saw, it is not simply the "
            "collateral being credited - do NOT report it as such. Re-open "
            "CRS-C-03 with both readings attached.")
        return DOWNGRADED, lines
    elif c06 in (None, "SKIP"):
        lines.append(
            "CRS-C-06 is {} on the change build, so the over-credit rests on "
            "CRS-C-03's single reading. That is an inference, and the first "
            "thing a reviewer will challenge: one observation cannot exclude "
            "a coincidental concurrent credit or a fixed surcharge that "
            "happened to equal the contract value. Get CRS-C-06 to run "
            "before reporting this one.".format(c06 or "absent"))
        if not over_credited:
            return INCONCLUSIVE, lines

    if b03 == "PASS":
        lines.append(
            "CRS-C-03 passes on the baseline and fails on the change - a "
            "clean flip.")
        return CONFIRMED, lines

    # The baseline also failed. There are two very different ways that
    # happens and they lead to opposite verdicts, so the reason has to be
    # read rather than assumed:
    #
    #   main REJECTED the bundle          -> the change accepts it AND
    #                                        mis-credits it. A regression.
    #   main ALSO over-credited           -> pre-existing. Not this PR's.
    #
    # Assuming the first is how a status-only tool gets this wrong in the
    # other direction, and this script exists precisely to not do that.
    base_over_credited = says(
        base, "CRS-C-03",
        "the excess is exactly the contract value",
        "recorded the collateral as its own")
    base_rejected = says(base, "CRS-C-03",
                         "rejected", "refused", "cannot be combined")

    if base_over_credited:
        lines.append(
            "The BASELINE over-credits the receiver in the same way - "
            "CRS-C-03's note there carries the same signature, not a "
            "rejection. So this is PRE-EXISTING and #739 did not introduce "
            "it. It is still a real bug and CRS-C-06 still proves it; file "
            "it separately rather than against this PR.")
        return DOWNGRADED, lines

    if base_rejected:
        lines.append(
            "CRS-C-03 fails on the BASELINE too, for the OPPOSITE reason: "
            "main rejects the bundled deploy+transfer outright rather than "
            "accepting it and over-crediting. Status-only comparison calls "
            "this PRE-EXISTING and is wrong - accepting a request that was "
            "previously rejected, and then mis-crediting it, is a "
            "regression. Note that CRS-C-06 SKIPPING on the baseline is the "
            "CORRECT answer for the same reason, not a coverage gap.")
        return CONFIRMED, lines

    lines.append(
        "CRS-C-03 fails on both builds and the baseline's reason is not "
        "recognisable as either a rejection or the same over-credit. This "
        "script cannot attribute it - read both notes in full and decide by "
        "hand. Baseline actual: " + actual(base, "CRS-C-03"))
    return INCONCLUSIVE, lines


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Single-build mode
#
# Causation for #739 was answered on 2026-09-09 by a paired run on the 31-host
# fleet: SC-C-27 PASS on main @ 710805f2 and FAIL on the branch, and SC-C-29
# accepted on main and refused on the branch. That question does not need
# re-asking, and re-deploying the merge-base costs a full build + fleet deploy.
#
# What that run could NOT answer is mechanism and severity, because the cases
# that supply them were written afterwards. Every one of those carries its own
# control INSIDE a single run - a successful deploy beside the rejected one, a
# binary-exact triple beside the inexact one, two collateral values in one
# pair - which is exactly why one build decides them.
#
# So these judges answer: is the behaviour real on this build, is it
# understood, and how bad is it. They never claim the PR caused it.
# ---------------------------------------------------------------------------

PRIOR = ("causation was established by the paired run of 2026-09-09 "
         "(main @ 710805f2 vs branch), not by this run")


def single_1(chg):
    """Blocker 1 from the change build alone. SC-C-33 is the control."""
    lines = []
    c27, c31, c33 = (status(chg, "SC-C-27"), status(chg, "SC-C-31"),
                     status(chg, "SC-C-33"))

    if c27 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "SC-C-27 is {} - the rejected deploy never happened, so there is "
            "nothing to observe. It skips when the wallet cannot be funded "
            "above the quorum's free balance (~2120 RBT); the reason is in "
            "the report's Note column.".format(c27 or "absent")]
    if c27 != "FAIL":
        return NOT_REPRODUCED, [
            "SC-C-27 passed: the rejected deploy left the wallet intact. The "
            "collateral is not being stranded on this build.",
            "actual: " + actual(chg, "SC-C-27")]

    lines.append(
        "SC-C-27 reproduces: a deploy was REJECTED and value still left "
        "Free for Committed. " + actual(chg, "SC-C-27"))

    if c33 == "PASS":
        lines.append(
            "SC-C-33 is the control, and it is what makes this statable "
            "without a second build: on THIS SAME build a SUCCESSFUL deploy "
            "commits exactly the contract value. So the normal path is "
            "well-defined, and the defect is precise - a rejected deploy "
            "commits the same value as a successful one and produces no "
            "contract for it.")
    elif c33 == "FAIL":
        lines.append(
            "SC-C-33 (the control) ALSO failed - a successful deploy did not "
            "commit the contract value either, so the collateral accounting "
            "is wrong on the normal path too. That is a LARGER finding than "
            "this blocker; read its note before writing anything up.")
    else:
        lines.append(
            "SC-C-33 (the control) is {} - without it the finding reads as "
            "'value moved to Committed', which sounds like correct "
            "behaviour, because committing collateral is what collateral is "
            "for. Get it to run; it is the cheap half of this "
            "blocker.".format(c33 or "absent"))

    if c31 == "PASS":
        lines.append(
            "SC-C-31 shows the committed value FALLING over its 120s window "
            "- a release path exists, so this is a wallet-behaviour "
            "regression, NOT unrecoverable loss. Report it at that severity.")
        return DOWNGRADED, lines
    if c31 in (None, "SKIP"):
        lines.append(
            "SC-C-31 is {} - nobody waited and looked again, so 'the value "
            "is lost' remains an inference and it is the first thing a "
            "reviewer will challenge. SC-C-27 and SC-C-31 must run in the "
            "same lane, in that order, on one wallet.".format(c31 or "absent"))
        return INCONCLUSIVE, lines

    lines.append(
        "SC-C-31: the committed value had NOT moved 120s after the "
        "rejection. Committed is terminal - no path back to Free - so this "
        "is permanent loss on a path the user cannot avoid. " +
        actual(chg, "SC-C-31"))
    if status(chg, "GEN-IN-22") == "FAIL":
        lines.append(
            "GEN-IN-22 shows committed RBT fleet-wide on hosts holding no "
            "contracts - corroboration only. That sweep reads ABSOLUTE state "
            "and the fleet carries historical committed RBT from earlier "
            "runs, so it cannot attribute anything by itself.")
    return REPRODUCED, lines


def single_2(chg):
    """Blocker 2 from the change build alone.

    SC-C-32 and SC-C-34 are internally controlled - they compare accepted
    against refused WITHIN one run - so the mechanism needs no baseline at all.
    """
    lines = []
    c29 = status(chg, "SC-C-29")
    if c29 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "SC-C-29 is {} - the three-contract request was never "
            "sent.".format(c29 or "absent")]

    refused = says(chg, "SC-C-29", *DECIMAL_REFUSAL) or \
        says(chg, "SC-C-29", "refused", "rejected")
    if not refused:
        return NOT_REPRODUCED, [
            "SC-C-29 shows no refusal of the three-contract request on this "
            "build.",
            "actual: " + actual(chg, "SC-C-29")]

    lines.append("SC-C-29 reproduces: " + actual(chg, "SC-C-29"))
    if says(chg, "SC-C-29", *DECIMAL_REFUSAL):
        lines.append(
            "The refusal is the 3-decimal-places guard, on values that are "
            "each legal at 3dp and whose sum is too - so the number tested "
            "was not the number sent.")

    if says(chg, "SC-C-34", "accumulation confirmed on the sum"):
        lines.append(
            "SC-C-34 settles the mechanism WITHOUT a baseline, because its "
            "control is inside the same run: 0.345 alone is accepted, "
            "0.25+0.5 at N=2 is accepted, only 0.1+0.2 at N=2 is refused. "
            "Not a per-value guard, not the contract count - accumulation on "
            "the SUM, biting from N=2 upward. 0.1+0.2 is 0.30000000000000004 "
            "in float64, which is the most recognisable example there is.")
    elif says(chg, "SC-C-32", "float accumulation confirmed"):
        lines.append(
            "SC-C-32 confirms accumulation from its own internal control: "
            "the binary-exact triple is accepted and the inexact one "
            "refused, in the same run. SC-C-34 would additionally pin the "
            "boundary at N=2 - check whether it ran.")
    elif says(chg, "SC-C-34", "count is the limit") or \
            says(chg, "SC-C-32", "not supported at all"):
        lines.append(
            "The mechanism cases point at the CONTRACT COUNT, not "
            "arithmetic: even binary-exact values are refused at N>=2. A "
            "different fix from a decimal-safe sum - report it that way.")
        return REPRODUCED, lines
    else:
        lines.append(
            "Neither SC-C-32 ({}) nor SC-C-34 ({}) returned a mechanism "
            "verdict, so the report can say multi-contract is refused but "
            "not why - much harder to act on.".format(
                status(chg, "SC-C-32") or "absent",
                status(chg, "SC-C-34") or "absent"))
        return INCONCLUSIVE, lines
    return REPRODUCED, lines


def single_3(chg):
    """Blocker 3 from the change build alone. CRS-C-06 carries it entirely."""
    lines = []
    c03, c06 = status(chg, "CRS-C-03"), status(chg, "CRS-C-06")

    if says(chg, "CRS-C-06", "tracks the collateral",
            "receiver credits the collateral"):
        lines.append(
            "CRS-C-06 PROVES it inside one build: run at two deliberately "
            "different contract values, the receiver's excess over the "
            "transfer equalled the contract value BOTH times and the two "
            "excesses differ by exactly the difference between the values. A "
            "coincidental credit does not follow v across runs, a fixed "
            "surcharge does not change with it, a rounding artefact does not "
            "scale. No baseline needed - the control is the second value.")
        if says(chg, "CRS-C-06", "recorded on both nodes", "duplicated, not moved"):
            lines.append(
                "AND the initiator committed the same value for the same "
                "deploys, so the collateral is recorded on BOTH nodes - "
                "duplicated, not misplaced. The most serious reading of the "
                "three.")
        return REPRODUCED, lines

    if says(chg, "CRS-C-06", "does not track the collateral"):
        return DOWNGRADED, [
            "CRS-C-06 REFUTES the diagnosis: the receiver gained more than "
            "the transfer, but the excess does not equal the contract value "
            "at both points. Whatever CRS-C-03 saw is not simply the "
            "collateral being credited - do NOT report it as such."]

    if c03 == "PASS" and c06 in (None, "PASS"):
        return NOT_REPRODUCED, [
            "CRS-C-03 passed - the receiver gained the transfer only.",
            "actual: " + actual(chg, "CRS-C-03")]

    if c03 in (None, "SKIP"):
        return INCONCLUSIVE, [
            "CRS-C-03 is {} - the bundled deploy+transfer was never "
            "observed on this build.".format(c03 or "absent")]

    lines.append("CRS-C-03: " + actual(chg, "CRS-C-03"))
    lines.append(
        "CRS-C-06 is {}, so this rests on CRS-C-03's single reading. That is "
        "an inference and the first thing a reviewer will challenge: one "
        "observation cannot exclude a coincidental concurrent credit or a "
        "fixed surcharge that happened to equal the contract value. Get "
        "CRS-C-06 to run before reporting this one.".format(c06 or "absent"))
    return INCONCLUSIVE, lines


BLOCKERS = [
    ("BLOCKER 1", "A rejected deploy commits its collateral permanently",
     ["SC-C-33", "SC-C-27", "SC-C-31", "GEN-IN-22"], blocker_1, single_1),
    ("BLOCKER 2", "Multi-contract requests stopped working",
     ["SC-C-29", "SC-C-32", "SC-C-34"], blocker_2, single_2),
    ("BLOCKER 3", "The receiver is credited the contract's collateral",
     ["CRS-C-03", "CRS-C-06"], blocker_3, single_3),
]

WIDTH = 78


def build_label(meta):
    """Best effort at naming a build from the report's run metadata."""
    if not isinstance(meta, dict):
        return "unknown"
    for key in ("Branch", "branch", "Version", "version", "Build", "build",
                "Fleet version", "Node version"):
        val = meta.get(key)
        if val:
            return str(val)
    return "unrecorded"


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("change", help="report JSON from the PR branch build")
    p.add_argument("baseline", nargs="?", default=None,
                   help="report JSON from the merge-base build. Omit it to "
                        "run single-build mode, which reports whether each "
                        "blocker REPRODUCES and why, and does not claim the "
                        "PR caused it.")
    p.add_argument("--quiet", action="store_true",
                   help="verdicts and reasoning only; omit the raw case table")
    args = p.parse_args()

    single = args.baseline is None
    for path in [args.change] + ([] if single else [args.baseline]):
        if not os.path.exists(path):
            sys.exit("ERROR: no such report: {}".format(path))

    chg_meta, chg = load(args.change)
    base_meta, base = ({}, {}) if single else load(args.baseline)

    print("=" * WIDTH)
    print("PR #739 - BLOCKER {}".format(
        "REPRODUCTION (single build)" if single else "CONFIRMATION"))
    print("  build    : {}  [{}]".format(args.change, build_label(chg_meta)))
    if single:
        for line in _wrap(
                "No baseline given, so this run does NOT answer whether #739 "
                "introduced these - " + PRIOR + ". What it answers is whether "
                "each behaviour is real on this build and whether the "
                "mechanism is understood. Every case below carries its own "
                "control inside the same run, which is why one build is "
                "enough for that much.", WIDTH - 4):
            print("  ! " + line)
    else:
        print("  baseline : {}  [{}]".format(args.baseline, build_label(base_meta)))
    print("=" * WIDTH)

    verdicts = []
    for name, title, cases, judge, judge_single in BLOCKERS:
        verdict, reasoning = judge_single(chg) if single else judge(chg, base)
        verdicts.append((name, title, verdict))

        print("\n{}  {}".format(name, verdict))
        print("  {}".format(title))
        print("-" * WIDTH)
        for line in reasoning:
            for i, wrapped in enumerate(_wrap(line, WIDTH - 4)):
                print("  {}{}".format("- " if i == 0 else "  ", wrapped))

        if not args.quiet:
            print("\n  evidence")
            for tid in cases:
                if single:
                    print("    {:<10} {:<5}  {}".format(
                        tid, status(chg, tid) or "-", _clip(actual(chg, tid))))
                else:
                    print("    {:<10} change {:<5} | base {:<5}".format(
                        tid, status(chg, tid) or "-", status(base, tid) or "-"))
                    print("      change: {}".format(_clip(actual(chg, tid))))
                    print("      base  : {}".format(_clip(actual(base, tid))))

    print("\n" + "=" * WIDTH)
    blocking = [n for n, _t, v in verdicts if v in BLOCKING]
    for name, title, verdict in verdicts:
        print("  {:<10} {:<14} {}".format(name, verdict, title[:48]))
    print("-" * WIDTH)
    if blocking:
        print("VERDICT: REQUEST CHANGES - {} of {} blocker(s) {} ({}).".format(
            len(blocking), len(verdicts),
            "reproduced" if single else "confirmed", ", ".join(blocking)))
        if single:
            for line in _wrap(
                    "Reproduced, not attributed: " + PRIOR + ". Quote that "
                    "run for causation and this one for mechanism and "
                    "severity.", WIDTH - 9):
                print("         " + line)
    else:
        inconclusive = [n for n, _t, v in verdicts if v == INCONCLUSIVE]
        if inconclusive:
            print("VERDICT: NOT DECIDABLE - {} blocker(s) inconclusive ({}).".format(
                len(inconclusive), ", ".join(inconclusive)))
            print("         A SKIP is not a pass. Fix the precondition and "
                  "re-run before approving anything.")
        else:
            print("VERDICT: no blocker confirmed on this run.")
    print("=" * WIDTH)

    # Exit 2, not 0, when nothing could be decided. Returning success for a
    # run where the deciding case never executed is the failure mode this
    # whole suite is built against: a gate that goes green because nothing
    # looked is indistinguishable from one that goes green because nothing
    # was wrong.
    if blocking:
        sys.exit(1)
    sys.exit(2 if any(v == INCONCLUSIVE for _n, _t, v in verdicts) else 0)


def _wrap(s, width):
    words, line, out = str(s).split(), "", []
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w) if line else w
    if line:
        out.append(line)
    return out or [""]


def _clip(s, n=68):
    s = re.sub(r"\s+", " ", str(s)).strip()
    # ASCII "..." rather than an ellipsis character: this prints to whatever
    # console the controller happens to have, and a cp1252 terminal turns a
    # U+2026 into a replacement glyph in the middle of quoted evidence.
    return s if len(s) <= n else s[:n - 3] + "..."


if __name__ == "__main__":
    main()
