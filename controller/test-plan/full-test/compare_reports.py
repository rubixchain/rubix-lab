#!/usr/bin/env python3
"""
compare_reports.py - the causation test.

A finding is only a blocker if THIS PR caused it. Running the suite once
cannot tell you that: a case that fails on the PR branch may have been failing
for a year. The only thing that answers it is the same suite against two
builds, so this diffs two report JSONs and says, per case, which it is.

    python3 case_runner.py --suite pr-739-verify --report-name pr739
    # deploy the merge-base build to the same hosts
    python3 case_runner.py --suite pr-739-verify --report-name base
    python3 compare_reports.py <...pr739...json> <...base...json>

The first argument is the CHANGE build, the second is the BASELINE.

Verdicts:

    INTRODUCED   fails on the change, passes on the baseline.
                 The PR caused it. This is the only verdict that blocks.

    FIXED        passes on the change, fails on the baseline.
                 The PR fixed it - the reason the fix cases are in the suite.

    PRE-EXISTING fails on both. Real, worth filing, NOT this PR's to answer.

    CLEAN        passes on both.

    INCONCLUSIVE either side skipped. A skip is not a pass, and treating it as
                 one is how a gap survives a review.

Exit code is 1 if anything is INTRODUCED, so this can gate a merge.
"""
import json
import os
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    rows = payload.get("results") or []
    return payload, {r["test_id"]: r for r in rows}


def verdict(change, base):
    """Both arguments are 'PASS' / 'FAIL' / 'SKIP' / None (absent)."""
    if change in (None, "SKIP") or base in (None, "SKIP"):
        return "INCONCLUSIVE"
    if change == "FAIL" and base == "PASS":
        return "INTRODUCED"
    if change == "PASS" and base == "FAIL":
        return "FIXED"
    if change == "FAIL" and base == "FAIL":
        return "PRE-EXISTING"
    return "CLEAN"


ORDER = ["INTRODUCED", "PRE-EXISTING", "INCONCLUSIVE", "FIXED", "CLEAN"]


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip() + "\n\nERROR: need exactly two report paths "
                 "(change first, baseline second).")
    change_path, base_path = sys.argv[1], sys.argv[2]
    for p in (change_path, base_path):
        if not os.path.exists(p):
            sys.exit("ERROR: no such report: {}".format(p))

    change_meta, change = load(change_path)
    base_meta, base = load(base_path)

    ids = sorted(set(change) | set(base))
    if not ids:
        sys.exit("ERROR: neither report contains any results.")

    buckets = {k: [] for k in ORDER}
    for tid in ids:
        c = change.get(tid, {}).get("status")
        b = base.get(tid, {}).get("status")
        buckets[verdict(c, b)].append((tid, c, b,
                                       (change.get(tid) or base.get(tid) or {}).get("case", "")))

    print("=" * 78)
    print("CAUSATION COMPARISON")
    print("  change   : {}".format(change_path))
    print("  baseline : {}".format(base_path))
    print("  {} case(s) compared".format(len(ids)))
    print("=" * 78)

    for key in ORDER:
        rows = buckets[key]
        if not rows:
            continue
        print("\n{}  ({})".format(key, len(rows)))
        print("-" * 78)
        for tid, c, b, case in rows:
            print("  {:<11} change={:<5} base={:<5}  {}".format(
                tid, c or "-", b or "-", (case or "")[:44]))

    intro = buckets["INTRODUCED"]
    incon = buckets["INCONCLUSIVE"]

    print("\n" + "=" * 78)
    if intro:
        print("VERDICT: {} case(s) INTRODUCED by this change.".format(len(intro)))
        print("         {}".format(", ".join(t for t, _c, _b, _n in intro)))
        print("         These block the merge - they pass without the change "
              "and fail with it.")
    else:
        print("VERDICT: nothing introduced by this change.")
        if buckets["PRE-EXISTING"]:
            print("         {} failure(s) are PRE-EXISTING and belong in their "
                  "own report:".format(len(buckets["PRE-EXISTING"])))
            print("         {}".format(
                ", ".join(t for t, _c, _b, _n in buckets["PRE-EXISTING"])))
    if incon:
        print("\nWARNING: {} case(s) INCONCLUSIVE - a SKIP on either side "
              "proves nothing.".format(len(incon)))
        print("         {}".format(", ".join(t for t, _c, _b, _n in incon)))
        print("         Fix the precondition and re-run before trusting this "
              "comparison.")
    print("=" * 78)

    sys.exit(1 if intro else 0)


if __name__ == "__main__":
    main()
