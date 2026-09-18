#!/usr/bin/env python3
"""
compare_reports.py - the causation test, with flake control.

FOUR-REPORT MODE (what you want for a PR verdict)

    python3 compare_reports.py --main main-1 main-2 --branch pr739-1 pr739-2

Each build is run TWICE, and the pair is used to decide which cases are
trustworthy at all. A case whose status differs between two runs of the SAME
build is FLAKY: it cannot support any conclusion, and it is excluded from the
verdict rather than quietly counted.

That exclusion is the whole point. Without it, a case that fails on main and
passes on the branch reads as "the PR fixed this" when it may just be noise -
and on an 88-case suite with a double-digit fail count, some of it is noise.
One extra run per build buys the right to believe the other verdicts.

Arguments are report NAMES, not paths: whatever you passed to
--report-name. Each resolves to the NEWEST reports/json/catalogue_<name>_*.json,
so nothing here needs a timestamp typed by hand. An actual file path works too.

TWO-REPORT MODE (kept; no flake control, so it can only be suggestive)

    python3 compare_reports.py <change.json> <baseline.json>

VERDICTS

    INTRODUCED   stable PASS on main, stable FAIL on the branch.
                 The PR caused it. The only verdict that blocks.

    FIXED        stable FAIL on main, stable PASS on the branch.
                 The PR fixed it - the answer to "did this actually solve
                 anything", and the reason both builds get run twice.

    PRE-EXISTING stable FAIL on both. Real, worth filing, NOT this PR's.

    FLAKY        the two runs of one build disagree. Excluded from the
                 verdict. Not a finding - a measurement problem.

    INCONCLUSIVE a SKIP, or the case is missing from a report. A skip is not
                 a pass, and treating it as one is how a gap survives review.

    CLEAN        stable PASS on both.

Exit: 1 if anything is INTRODUCED, 2 if nothing was INTRODUCED but the run is
not trustworthy (flaky or inconclusive cases present), 0 otherwise.
"""

import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(HERE, "..", "..", "..", "reports", "json")

# Cases known to fail on BOTH builds for OPPOSITE reasons. Status-only
# comparison buckets them PRE-EXISTING, which is wrong: on main the
# three-contract request is accepted and overcharged, on the branch it is
# refused outright; on main the bundle is rejected, on the branch it is
# accepted and the receiver over-credited. Both are regressions. The reasons
# live in the actual/note text, so this file flags them rather than judging
# them - confirm_blockers.py reads the text and decides.
TEXT_NOT_STATUS = {
    "SC-C-29": "accepted+overcharged on main vs refused on the branch",
    "CRS-C-03": "bundle rejected on main vs accepted+over-credited on the branch",
}


def resolve(name):
    """A report NAME (--report-name) or a path -> an existing path.

    Names beat paths for this flow: the runner timestamps every report, so
    the path is not knowable in advance and copying it by hand between four
    runs is exactly the manual step this is meant to remove.
    """
    if os.path.exists(name):
        return name
    pattern = os.path.join(REPORTS_DIR, "catalogue_{}_*.json".format(name))
    hits = sorted(glob.glob(pattern))
    if not hits:
        sys.exit("ERROR: no report named {!r}.\n"
                 "       Looked for: {}\n"
                 "       Run with --report-name {} first, or pass a path."
                 .format(name, pattern, name))
    return hits[-1]          # newest by timestamped filename


def load(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("results") or []
    if not rows:
        sys.exit("ERROR: {} contains no results".format(path))
    return payload, {r["test_id"]: r for r in rows}


def status(rows, tid):
    row = rows.get(tid)
    return row.get("status") if row else None


def actual(rows, tid):
    row = rows.get(tid) or {}
    return (row.get("actual") or "").strip()


INTRODUCED = "INTRODUCED"
FLAKY = "FLAKY"
FIXED = "FIXED"
PRE_EXISTING = "PRE-EXISTING"
INCONCLUSIVE = "INCONCLUSIVE"
CLEAN = "CLEAN"

# Order the report prints them in: what blocks first, what invalidates second,
# what the PR achieved third.
ORDER = [INTRODUCED, FLAKY, FIXED, PRE_EXISTING, INCONCLUSIVE, CLEAN]


def verdict_pair(change, base):
    """Two-report mode: no flake control is possible."""
    if change in (None, "SKIP") or base in (None, "SKIP"):
        return INCONCLUSIVE
    if change == "FAIL" and base == "PASS":
        return INTRODUCED
    if change == "PASS" and base == "FAIL":
        return FIXED
    if change == "FAIL" and base == "FAIL":
        return PRE_EXISTING
    return CLEAN


def verdict_quad(m1, m2, b1, b2):
    """Four-report mode. Repeatability is checked BEFORE anything else."""
    if None in (m1, m2, b1, b2):
        return INCONCLUSIVE, "missing from at least one report"
    # SKIP is checked BEFORE disagreement. A case that SKIPPED did not run -
    # its precondition failed - so "PASS then SKIP" is not the build
    # disagreeing with itself, it is one run with nothing to say. Calling that
    # FLAKY hid real flakes among setup failures (32 of 50 on 2026-09-18),
    # and the two have opposite fixes: a flake needs the case stabilised, a
    # skip needs the run's setup fixed.
    runs = (("main-1", m1), ("main-2", m2), ("branch-1", b1), ("branch-2", b2))
    skipped = [name for name, s in runs if s == "SKIP"]
    if skipped:
        return INCONCLUSIVE, "skipped on {}".format(
            "all four runs" if len(skipped) == 4 else ", ".join(skipped))
    if m1 != m2:
        return FLAKY, "main disagrees with itself: {} then {}".format(m1, m2)
    if b1 != b2:
        return FLAKY, "branch disagrees with itself: {} then {}".format(b1, b2)
    if m1 == "PASS" and b1 == "FAIL":
        return INTRODUCED, "passed twice on main, failed twice on the branch"
    if m1 == "FAIL" and b1 == "PASS":
        return FIXED, "failed twice on main, passed twice on the branch"
    if m1 == "FAIL" and b1 == "FAIL":
        return PRE_EXISTING, "failed twice on both"
    return CLEAN, "passed twice on both"


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("reports", nargs="*",
                   help="two-report mode: <change.json> <baseline.json>")
    p.add_argument("--main", nargs=2, metavar=("RUN1", "RUN2"),
                   help="the two baseline runs, by --report-name or path")
    p.add_argument("--branch", nargs=2, metavar=("RUN1", "RUN2"),
                   help="the two PR-branch runs, by --report-name or path")
    p.add_argument("--evidence", action="store_true",
                   help="print the actual/ result line for FIXED and "
                        "INTRODUCED cases - what the change looks like")
    p.add_argument("--detail", default="", metavar="ID[,ID...]",
                   help="print status, actual AND note for these cases from "
                        "every report, then exit - the why behind a verdict, "
                        "including each SKIP's reason")
    args = p.parse_args()

    quad = bool(args.main or args.branch)
    if quad and not (args.main and args.branch):
        sys.exit("ERROR: four-report mode needs BOTH --main and --branch.")
    if not quad and len(args.reports) != 2:
        sys.exit(__doc__.strip() + "\n\nERROR: give either two report paths "
                 "(change first), or --main A B --branch C D.")

    if quad:
        m1p, m2p = (resolve(n) for n in args.main)
        b1p, b2p = (resolve(n) for n in args.branch)
        _, m1 = load(m1p)
        _, m2 = load(m2p)
        _, b1 = load(b1p)
        _, b2 = load(b2p)
        ids = sorted(set(m1) | set(m2) | set(b1) | set(b2))
        sources = [("main  run 1", m1p), ("main  run 2", m2p),
                   ("branch run 1", b1p), ("branch run 2", b2p)]
    else:
        cp, bp = (resolve(n) for n in args.reports)
        _, chg = load(cp)
        _, base = load(bp)
        ids = sorted(set(chg) | set(base))
        sources = [("change  ", cp), ("baseline", bp)]

    runs = ([m1, m2, b1, b2] if quad else [chg, base])

    if args.detail:
        wanted = [t.strip() for t in args.detail.split(",") if t.strip()]
        for tid in wanted:
            print("=" * 78)
            print(tid)
            for (label, path), rows in zip(sources, runs):
                row = rows.get(tid)
                if not row:
                    print("  {:<12} (not in this report)".format(label))
                    continue
                print("  {:<12} {}".format(label, row.get("status")))
                print("      actual : {}".format((row.get("actual") or "-").strip()))
                note = (row.get("note") or "").strip()
                if note:
                    print("      note   : {}".format(note))
        print("=" * 78)
        return

    print("=" * 78)
    print("CAUSATION COMPARISON" + ("  (4 reports, flake-controlled)" if quad
                                    else "  (2 reports, NO flake control)"))
    # Pass/fail/skip per run. A run whose SKIP count stands out from its twin
    # had a setup problem (funding, quorum, reachability) - its verdicts are
    # weaker than the others', and that is visible here before any bucket.
    for (label, path), rows in zip(sources, runs):
        c = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for r in rows.values():
            c[r.get("status")] = c.get(r.get("status"), 0) + 1
        print("  {} : {}   pass {:>2}  fail {:>2}  skip {:>2}".format(
            label, os.path.basename(path), c["PASS"], c["FAIL"], c["SKIP"]))
    print("  {} case(s) compared".format(len(ids)))
    print("=" * 78)

    buckets = {k: [] for k in ORDER}
    for tid in ids:
        if quad:
            v, why = verdict_quad(status(m1, tid), status(m2, tid),
                                  status(b1, tid), status(b2, tid))
            buckets[v].append((tid, why,
                               actual(m1, tid) or actual(m2, tid),
                               actual(b1, tid) or actual(b2, tid)))
        else:
            c, b = status(chg, tid), status(base, tid)
            v = verdict_pair(c, b)
            buckets[v].append((tid, "change={} base={}".format(c or "-", b or "-"),
                               actual(base, tid), actual(chg, tid)))

    for key in ORDER:
        rows = buckets[key]
        if not rows:
            continue
        print("\n{}  ({})".format(key, len(rows)))
        print("-" * 78)
        for tid, why, main_actual, branch_actual in rows:
            print("  {:<11} {}".format(tid, why))
            if args.evidence and key in (FIXED, INTRODUCED):
                print("      main   : {}".format(main_actual[:60] or "-"))
                print("      branch : {}".format(branch_actual[:60] or "-"))

    intro = buckets[INTRODUCED]
    flaky = buckets[FLAKY]
    incon = buckets[INCONCLUSIVE]
    fixed = buckets[FIXED]

    print("\n" + "=" * 78)
    print("  {:>3} FIXED by this PR".format(len(fixed)))
    print("  {:>3} INTRODUCED by this PR".format(len(intro)))
    print("  {:>3} PRE-EXISTING".format(len(buckets[PRE_EXISTING])))
    print("  {:>3} FLAKY - excluded, prove nothing either way".format(len(flaky)))
    print("  {:>3} INCONCLUSIVE".format(len(incon)))
    print("  {:>3} CLEAN".format(len(buckets[CLEAN])))
    print("-" * 78)

    if intro:
        print("VERDICT: REQUEST CHANGES - {} case(s) INTRODUCED.".format(len(intro)))
        print("         {}".format(", ".join(t for t, _w, _m, _b in intro)))
    elif fixed:
        print("VERDICT: nothing introduced; {} case(s) fixed.".format(len(fixed)))
    else:
        print("VERDICT: nothing introduced, and nothing measurably fixed.")

    # Two cases whose STATUS is known to misrepresent them. Named explicitly
    # wherever they land, because the bucket is the misleading part.
    for tid, why in TEXT_NOT_STATUS.items():
        for key in (PRE_EXISTING, CLEAN, FLAKY):
            if any(t == tid for t, _w, _m, _b in buckets[key]):
                print("\nWARNING: {} is bucketed {} and that is misleading -\n"
                      "         {}.\n"
                      "         Read its actual/note text, or use "
                      "confirm_blockers.py.".format(tid, key, why))

    if flaky:
        print("\nWARNING: {} case(s) are FLAKY - a build disagreed with "
              "itself.".format(len(flaky)))
        print("         {}".format(", ".join(t for t, _w, _m, _b in flaky)))
        print("         Nothing about these can be attributed to the PR. If a "
              "case you\n         care about is here, stabilise it before "
              "drawing a conclusion.")
    if not quad:
        print("\nWARNING: two-report mode has NO flake control. A single FIXED "
              "or\n         INTRODUCED row here may be run-to-run noise. Run "
              "each build\n         twice and use --main/--branch to find out.")
    if incon:
        print("\nWARNING: {} case(s) INCONCLUSIVE - a SKIP proves nothing. Fix "
              "the\n         precondition and re-run before trusting "
              "this.".format(len(incon)))
    print("=" * 78)

    if intro:
        sys.exit(1)
    sys.exit(2 if (flaky or incon) else 0)


if __name__ == "__main__":
    main()
