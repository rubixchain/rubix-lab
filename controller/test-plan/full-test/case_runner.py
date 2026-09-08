#!/usr/bin/env python3
"""
case_runner.py - Generic driver for the master catalogue.

Reuses smoke_test.py's proven common setup (pool -> DID readiness -> role
assignment -> quorum setup+funding -> sender funding) and then runs a
per-asset case module against it. To run a different asset, point --cases at
that module; nothing here changes.

A case module must expose:
    CASES = {"RBT-001": fn, ...}   fn(ctx, ci) -> (passed, actual, note)
    ORDER = ["RBT-001", ...]        execution order

`ci` is a CaseInfo carrying the row from rubix-lab-test-catalogue.csv (test id,
case text, expected result, other checks, notes) so a case can assert
against what the catalogue actually says rather than a hardcoded copy.

Three outcomes, deliberately distinct:
    PASS    - ran and matched the expectation
    FAIL    - ran and did NOT match: a real finding
    SKIP    - not attempted, with a reason. Never silently counted as a pass.
              Used where the case needs machinery this runner doesn't have
              (node kills, DB seeding, a second DID on one node) or where
              setup would be prohibitively expensive (see the minting note in
              rbt_cases.py). A SKIP is an honest gap, not a success.

Usage:
    python3 case_runner.py --cases rbt            # test-plan/rbt/rbt_cases.py
    python3 case_runner.py --cases rbt --only RBT-001,RBT-005
    python3 case_runner.py --cases rbt --quorum-fund 5000
"""

import argparse
import csv
import datetime
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rubix_client as rc
import report_builder
from smoke_test import (
    FIXED_ROLES, DEFAULT_HOSTS, ROLES_PATH,
    sweep_and_prepare, assign_roles, write_roles_file,
    setup_quorums, assign_and_fund_senders,
)

# The catalogue is the source of truth. master-test-cases.xlsx is NOT -
# it stopped being updated and now holds stale IDs.
CATALOGUE_PATH = os.path.join(HERE, "..", "rubix-lab-test-catalogue.csv")

SKIP = "SKIP"


class CaseInfo:
    """One row of rubix-lab-test-catalogue.csv."""

    def __init__(self, test_id, asset="", case="", expected="", checks="", notes=""):
        self.test_id = test_id
        self.asset = asset
        self.case = case
        self.expected = expected
        self.checks = checks
        self.notes = notes

    @property
    def expects_rejection(self):
        """True when the catalogue's Expected Result is a rejection."""
        return self.expected.strip().lower().startswith("reject")

    @property
    def is_record_only(self):
        """True when the catalogue asks to RECORD behaviour rather than
        assert pass/fail - e.g. value ladders ('Record the largest value that
        works') and 'Define what happens'. Forcing these into pass/fail would
        invent an expectation the catalogue deliberately doesn't state."""
        low = self.expected.strip().lower()
        return low.startswith("record") or low.startswith("define") or "find the" in low


class CaseContext:
    """What every case gets. Same shape as smoke_test's SmokeContext plus
    the extras full-catalogue cases need."""

    def __init__(self, port, quorum_hosts, senders, receivers, sender_quorum, args):
        self.port = port
        self.quorum_hosts = quorum_hosts
        self.senders = senders
        self.receivers = receivers
        self.sender_quorum = sender_quorum
        self.pairs = list(zip(senders, receivers))
        self.args = args

    def pair(self, i=0):
        """A (sender, receiver) pair. Cases that need an isolated pair should
        use different indices so they don't disturb each other's balances."""
        return self.pairs[i % len(self.pairs)]

    def quorum_for(self, sender):
        return self.sender_quorum.get(sender["host"])


def load_master(path=None):
    """Load the catalogue, keyed by Test ID.

    Reads rubix-lab-test-catalogue.csv - the single source of truth. It used to
    read master-test-cases.xlsx, which silently went stale: it still holds the
    old 259 rows with pre-rename IDs, so every case added or renamed since
    (SC-C-*, FT-P-*, GEN-IN-*, and the whole cross-cutting matrix) matched
    nothing and reported with EMPTY 'Test Case' and 'Expected Result' columns.
    The run was correct; the report just could not say what it had tested.

    A CSV also avoids needing openpyxl at all.
    """
    path = path or CATALOGUE_PATH
    if not os.path.exists(path):
        sys.exit("ERROR: catalogue not found: {}".format(path))
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            tid = (row.get("Test ID") or "").strip()
            if not tid:
                continue
            out[tid] = CaseInfo(
                tid,
                asset=row.get("Asset") or "",
                case=row.get("Test Case") or "",
                expected=row.get("Expected Result") or "",
                checks=row.get("Also Check In Same Run") or "",
                notes=row.get("Code Ref") or "",
            )
    return out


SUITES_DIR = os.path.join(HERE, "..", "suites")

# Which module owns each Test ID prefix, so a suite file can list cases without
# also having to name the modules they live in.
PREFIX_MODULE = {
    "RBT": "rbt", "FT": "ft", "NFT": "nft",
    "SC": "sc", "CRS": "cross-asset", "GEN": "general",
}


def load_suite(name):
    """Read suites/<name>.txt -> (patterns, modules).

    A suite is a NAMED, COMMITTED selection of existing cases - typically the
    set that verifies one change, so its author gets a report containing their
    work and nothing else.

    Deliberately NOT a separate copy of the cases. Cases are organised by asset
    because they outlive the change that prompted them: SC-C-01 is a permanent
    regression check, and a file named after a merged PR would become
    archaeology nobody dares delete. The suite names the SELECTION; the cases
    stay where they belong.
    """
    path = name if os.path.exists(name) else os.path.join(SUITES_DIR, name + ".txt")
    if not os.path.exists(path):
        available = []
        if os.path.isdir(SUITES_DIR):
            available = sorted(f[:-4] for f in os.listdir(SUITES_DIR) if f.endswith(".txt"))
        sys.exit("ERROR: no suite {!r} at {}\n       Available: {}".format(
            name, path, ", ".join(available) or "(none)"))

    patterns = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                patterns.append(line)
    if not patterns:
        sys.exit("ERROR: suite {} lists no Test IDs.".format(path))

    modules, seen = [], set()
    for pat in patterns:
        prefix = pat.split("-", 1)[0]
        mod = PREFIX_MODULE.get(prefix)
        if mod is None:
            sys.exit("ERROR: suite {} has {!r}, whose prefix {!r} maps to no "
                     "module. Known: {}".format(path, pat, prefix,
                                                ", ".join(sorted(PREFIX_MODULE))))
        if mod not in seen:
            seen.add(mod)
            modules.append(mod)
    print("Suite {} -> {} pattern(s) across {}".format(
        os.path.basename(path), len(patterns), ", ".join(modules)))
    return patterns, modules


def load_case_module(name):
    """Import test-plan/<name>/<name>_cases.py by path."""
    mod_path = os.path.join(HERE, "..", name, "{}_cases.py".format(name))
    mod_path = os.path.abspath(mod_path)
    if not os.path.exists(mod_path):
        sys.exit("ERROR: no case module at {}".format(mod_path))
    spec = importlib.util.spec_from_file_location("{}_cases".format(name), mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



# ---------------------------------------------------------------------------
# Lanes
#
# A lane owns its own sender/receiver hosts and runs its cases SEQUENTIALLY;
# lanes run in PARALLEL. On a 31-machine fleet that turns a ~40 minute serial
# pass into under ten minutes, with most of the fleet busy instead of idle.
#
# A lane owns its DIDs exclusively because these cases assert on BALANCE
# DELTAS. Two cases sharing a wallet would corrupt each other's measurements -
# the second would see the first's spending and report it as its own. That is
# also why cases which deliberately build on one another (SC-S-*, FT-P-*) stay
# in ONE lane: they are sequential by design, not by accident.
#
# Quorums are SHARED across lanes on purpose - the fleet has three and every
# lane needs one. Lanes therefore contend for pledge capacity, which is why
# quorums are funded in bulk while a lane is funded to roughly what it spends.
#
# A module declares:
#     LANES = {
#         "sc-collateral": {"cases": [...], "hosts": 1, "fund": 6},
#     }
#         hosts - sender/receiver machines this lane needs to itself
#         fund  - RBT per host, before the safety margin
# A module with no LANES runs as a single lane holding every case, which is the
# original behaviour.
# ---------------------------------------------------------------------------

# Added to every lane's funding. Being generous costs seconds of minting;
# being short costs a whole run to a failure that says nothing about the
# product.
FUND_SAFETY_MARGIN = 10


class Lane(object):
    def __init__(self, name, cases, ctx, fund):
        self.name = name
        self.cases = cases
        self.ctx = ctx
        self.fund = fund
        self.rows = []          # (test_id, result_tuple, seconds, CaseInfo)


def resolve_ci(tid, master, case_info):
    ci = master.get(tid)
    if ci is not None:
        return ci
    text = case_info.get(tid)
    return CaseInfo(tid, case=text[0], expected=text[1]) if text else CaseInfo(tid)


def build_lanes(module, order, ready, quorum_hosts, sender_quorum, args):
    """Allocate pool hosts to lanes. Returns (lanes, {case: (actual, note)})."""
    spec_all = getattr(module, "LANES", None)
    qhosts = {q["host"] for q in quorum_hosts}
    pool = [h for h in ready if h["host"] not in qhosts]

    if not spec_all:
        half = max(1, len(pool) // 2)
        ctx = CaseContext(args.port, quorum_hosts, pool[:half], pool[half:],
                          sender_quorum, args)
        return [[Lane("all", list(order), ctx, args.fund_sender)]], {}

    # WAVES. More lanes can be defined than the fleet has hosts. Dropping the
    # overflow would silently skip a third of the suite, so instead the
    # allocator fills the fleet, and anything that does not fit starts a new
    # wave that runs after the first finishes and frees its hosts.
    #
    # Every lane still gets its OWN hosts within its wave - the isolation that
    # makes balance-delta assertions valid is never traded away for speed.
    waves, lanes, skipped, idx = [], [], {}, 0
    for name, spec in spec_all.items():
        cases = [c for c in order if c in spec.get("cases", [])]
        if not cases:
            continue
        need = int(spec.get("hosts", 2))
        if need > len(pool):
            for c in cases:
                skipped[c] = ("lane too large for the fleet",
                              "lane {!r} needs {} host(s); the pool has {}".format(
                                  name, need, len(pool)))
            continue
        if idx + need > len(pool):
            # Fleet full - close this wave and start the next.
            waves.append(lanes)
            lanes, idx = [], 0
        mine = pool[idx:idx + need]
        idx += need
        # One sender, the rest receivers - so a case's ctx.pair(0) and
        # ctx.receivers[n] resolve inside its own lane, and no case has to know
        # that lanes exist at all.
        senders = mine[:1]
        receivers = mine[1:] or mine[:1]
        ctx = CaseContext(args.port, quorum_hosts, senders, receivers,
                          sender_quorum, args)
        lanes.append(Lane(name, cases, ctx,
                          int(spec.get("fund", args.fund_sender)) + FUND_SAFETY_MARGIN))

    if lanes:
        waves.append(lanes)

    total_lanes = sum(len(w) for w in waves)
    total_cases = sum(len(l.cases) for w in waves for l in w)
    print("  {} lane(s), {} case(s), in {} wave(s) over {} pool hosts".format(
        total_lanes, total_cases, len(waves), len(pool)))
    for wi, wave in enumerate(waves, 1):
        used = sum(len({e["host"] for e in l.ctx.senders + l.ctx.receivers})
                   for l in wave)
        print("  -- wave {} ({} lane(s), {} host(s)) --".format(wi, len(wave), used))
        for ln in wave:
            print("    {:<24} {:>2} case(s)  {:>2} host(s)  fund {:>3}  from {}".format(
                ln.name, len(ln.cases),
                len({e["host"] for e in ln.ctx.senders + ln.ctx.receivers}),
                ln.fund, ln.ctx.senders[0]["host"]))
    if skipped:
        print("  {} case(s) cannot run - see the report for why".format(len(skipped)))
    return waves, skipped


def fund_lane(lane, args):
    """Give every host in a lane a registered quorum and its working balance."""
    q = lane.ctx.quorum_hosts[0] if lane.ctx.quorum_hosts else None
    seen = set()
    for e in list(lane.ctx.senders) + list(lane.ctx.receivers):
        if e["host"] in seen:
            continue
        seen.add(e["host"])
        if q is not None:
            # Errors when already registered; that is success, not a failure.
            rc.quorum_add(e["host"], q["did"], args.port)
        ok, detail, _ = rc.get_rbt_balance_detail(e["host"], e["did"], args.port)
        have = detail["balance"] if ok and detail else 0
        if have < lane.fund:
            rc.fund_did(e["host"], e["did"], int(lane.fund - have) + 1, args.port)
            rc.wait_for_balance(e["host"], e["did"], lane.fund, args.port)


def run_lanes(waves, cases_map, master, case_info, args):
    """Fund and run every lane in parallel. Results land on each Lane."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    lock = threading.Lock()

    def run_one(lane):
        try:
            fund_lane(lane, args)
        except Exception as e:
            for tid in lane.cases:
                lane.rows.append((tid, (SKIP, "lane setup failed",
                                        "{}: {}".format(type(e).__name__, e)),
                                  0.0, resolve_ci(tid, master, case_info)))
            return
        for tid in lane.cases:
            ci = resolve_ci(tid, master, case_info)
            started = time.time()
            try:
                result = cases_map[tid](lane.ctx, ci)
            except Exception as e:
                result = (False, "exception", "{}: {}".format(type(e).__name__, e))
            elapsed = round(time.time() - started, 2)
            lane.rows.append((tid, result, elapsed, ci))
            status = ("SKIP" if (isinstance(result[0], str) and result[0] == SKIP)
                      else "PASS" if result[0] is True else "FAIL")
            with lock:
                print("  [{:<4}] {:<11} {:>6.2f}s  {:<18} {}".format(
                    status, tid, elapsed, lane.name, result[1]))

    for wi, wave in enumerate(waves, 1):
        if len(waves) > 1:
            print("\n-- wave {} of {}: {} lane(s) in parallel --".format(
                wi, len(waves), len(wave)))
        else:
            print("\nRunning {} lane(s) in parallel - output is interleaved by "
                  "completion, the report is ordered.".format(len(wave)))
        with ThreadPoolExecutor(max_workers=max(1, len(wave))) as pool:
            list(pool.map(run_one, wave))


def dump_roles(path, quorum_hosts, senders, receivers):
    """Write a pinnable roles file describing this run's assignment."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# Role assignment for a pinned run - edit, then pass with --pin-roles\n")
        fh.write("# One 'IP role' per line. role = quorum | sender | receiver.\n")
        fh.write("#\n")
        fh.write("# For a MIXED-VERSION run, deploy the builds by IP first, e.g.\n")
        fh.write("#   REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh old-branch <receiver IPs>\n")
        fh.write("#   REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh new-branch <sender IPs>\n")
        fh.write("# then pin the same IPs here so the roles cannot drift, and run with\n")
        fh.write("# --collect-versions so the report records the build PER ROLE.\n\n")
        for e in quorum_hosts:
            fh.write("{}  quorum\n".format(e["host"]))
        for e in senders:
            fh.write("{}  sender\n".format(e["host"]))
        for e in receivers:
            fh.write("{}  receiver\n".format(e["host"]))
    print("Roles written to {}".format(path))
    print("Edit it, then re-run with:  --pin-roles {}".format(path))


def load_pinned_roles(path, ready):
    """Read a pinned roles file. Returns (quorum_hosts, senders, receivers).

    Every pinned host must be in `ready` - pinning a host that is down would
    otherwise fail deep inside a case with a confusing error, and in a
    mixed-version run it would quietly change what is being compared.
    """
    if not os.path.exists(path):
        sys.exit("ERROR: --pin-roles file not found: {}\n"
                 "       Generate one with --dump-roles {}".format(path, path))

    by_host = {e["host"]: e for e in ready}
    buckets = {"quorum": [], "sender": [], "receiver": []}
    missing, unknown = [], []

    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2 or parts[1] not in buckets:
                unknown.append("line {}: {!r} (expected '<ip> quorum|sender|receiver')"
                               .format(lineno, raw.strip()))
                continue
            host, role = parts[0], parts[1]
            if host not in by_host:
                missing.append(host)
                continue
            buckets[role].append(by_host[host])

    if unknown:
        sys.exit("ERROR: {} unparseable line(s) in {}:\n  {}".format(
            len(unknown), path, "\n  ".join(unknown)))
    if missing:
        sys.exit(
            "ERROR: {} pinned host(s) are not reachable/ready: {}\n"
            "       A pinned run must use exactly the hosts you pinned - silently\n"
            "       dropping one would change what the run actually compares.\n"
            "       Fix the host, or edit {}.".format(
                len(missing), ", ".join(missing), path))
    if not buckets["quorum"]:
        sys.exit("ERROR: {} pins no quorum host.".format(path))
    if not buckets["sender"] or not buckets["receiver"]:
        sys.exit("ERROR: {} needs at least one sender and one receiver.".format(path))

    print("  quorum   : {}".format(", ".join(e["host"] for e in buckets["quorum"])))
    print("  senders  : {}".format(", ".join(e["host"] for e in buckets["sender"])))
    print("  receivers: {}".format(", ".join(e["host"] for e in buckets["receiver"])))
    return buckets["quorum"], buckets["sender"], buckets["receiver"]


class CatalogueReport:
    """Per-Test-ID results, with SKIP tracked separately from PASS/FAIL."""

    def __init__(self):
        self.rows = []

    def add(self, ci, result, elapsed):
        """Record a result produced by a lane, without re-running it."""
        passed, actual, note = result
        if isinstance(passed, str) and passed == SKIP:
            status = "SKIP"
        elif passed is True:
            status = "PASS"
        else:
            status = "FAIL"
        self.rows.append({
            "test_id": ci.test_id, "case": ci.case, "expected": ci.expected,
            "status": status, "actual": actual, "note": note,
            "seconds": elapsed,
        })
        return status

    def record(self, ci, fn, ctx):
        start = time.time()
        try:
            passed, actual, note = fn(ctx, ci)
        except Exception as e:
            passed, actual, note = False, "exception", "{}: {}".format(type(e).__name__, e)
        elapsed = round(time.time() - start, 2)

        # `==` not `is`: SKIP is defined independently in each case module, and
        # Python does not guarantee identity for equal strings across modules.
        if isinstance(passed, str) and passed == SKIP:
            status = "SKIP"
        elif passed is True:
            status = "PASS"
        else:
            status = "FAIL"

        self.rows.append({
            "test_id": ci.test_id, "case": ci.case, "expected": ci.expected,
            "status": status, "actual": actual, "note": note, "seconds": elapsed,
        })
        print("  [{:<4}] {:<9} {:>6.2f}s  {}{}".format(
            status, ci.test_id, elapsed, actual, ("  -- " + note) if note else ""))
        return status

    def save(self, asset, meta, timing_ids):
        paths = rc.new_report_paths("catalogue_{}".format(asset), ("pdf", "json"))
        report_builder.build_json(paths["json"], meta, self.rows)
        pdf_ok = report_builder.build_pdf(
            paths["pdf"], "Rubix Lab - {} Catalogue Run".format(asset.upper()),
            meta, self.rows, timing_ids)

        p = sum(1 for r in self.rows if r["status"] == "PASS")
        f = sum(1 for r in self.rows if r["status"] == "FAIL")
        s = sum(1 for r in self.rows if r["status"] == "SKIP")
        attempted = p + f
        print("\n{} passed, {} failed, {} skipped (of {}).".format(p, f, s, len(self.rows)))
        print("Pass rate of ATTEMPTED cases: {:.1f}% ({}/{}) - skipped are not counted "
              "as passes.".format((100.0 * p / attempted) if attempted else 0.0, p, attempted))
        if f:
            print("FAILED: {}".format(
                ", ".join(r["test_id"] for r in self.rows if r["status"] == "FAIL")))
        if s:
            print("SKIPPED (not attempted, reason in the report's Note column): {}".format(
                ", ".join(r["test_id"] for r in self.rows if r["status"] == "SKIP")))
        print("\nJSON: {}".format(paths["json"]))
        if pdf_ok:
            print("PDF : {}".format(paths["pdf"]))
        else:
            print("PDF : not written - reportlab missing. Results are safe in the JSON "
                  "above. Install with: sudo apt install -y python3-reportlab")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", default="",
                   help="run a named selection from suites/<name>.txt (e.g. "
                        "pr-739-sc). Sets --cases, --only and --report-name for "
                        "you, so one change's verification is a single reviewed, "
                        "committed file rather than a command someone has to "
                        "remember.")
    p.add_argument("--cases", default="",
                   help="asset folder name, e.g. rbt. Accepts several, comma "
                        "separated (e.g. sc,general) so one report can span the "
                        "modules a single change touches - the denomination "
                        "checks live in general/ but verify fixes made in the SC "
                        "and FT paths, and each author should only see their own.")
    p.add_argument("--only", default="",
                   help="comma-separated Test IDs to run. A trailing * is a "
                        "prefix match, e.g. --only 'SC-C-*,GEN-IN-11'")
    p.add_argument("--report-name", default="",
                   help="label for the report file (default: the --cases value). "
                        "Use it to name a run after what it verifies, e.g. "
                        "--report-name sc-collateral-fix")
    p.add_argument("--hosts", default=DEFAULT_HOSTS)
    p.add_argument("--port", type=int, default=rc.DEFAULT_PORT)
    p.add_argument("--quorum-count", type=int, default=3)
    p.add_argument("--fund-quorum", type=int, default=2000,
                   help="RBT per quorum. A quorum must pledge >= the transfer value "
                        "(core/consensus/checks.go), so this caps testable transfer size.")
    p.add_argument("--fund-sender", type=int, default=200,
                   help="RBT per sender. Minting is one token per unit (~15s per 1000), "
                        "so large values here cost real time.")
    p.add_argument("--rbt-amount", type=float, default=1)
    p.add_argument("--ft-count", type=int, default=10)
    p.add_argument("--ft-token-count", type=int, default=1)
    # Cases whose full catalogue size is impractical in a normal pass. The
    # actual value used is always stated in that case's report row, so a
    # reduced run is never mistaken for the full one.
    p.add_argument("--large-mint", type=int, default=2000,
                   help="RBT-003 single-mint size (~15s per 1000)")
    p.add_argument("--decimal-samples", type=int, default=3,
                   help="RBT-029 values per decimal place (catalogue asks 10)")
    p.add_argument("--repeat-count", type=int, default=25,
                   help="RBT-032 repetitions (catalogue asks 1000, ~33 min)")
    # --- mixed-version runs -------------------------------------------------
    # Roles are normally derived from the REACHABLE host list
    # (quorum = first N, then senders/receivers alternate). That is fine for a
    # uniform fleet, but for a mixed-version test it is a trap: one node
    # dropping out shifts every role after it, so "receivers on the old build"
    # silently becomes a different arrangement and the run answers a question
    # nobody asked. Pinning makes the experiment reproducible.
    p.add_argument("--dump-roles", default="",
                   help="write the roles this run WOULD use to a file, then exit. "
                        "Edit it to taste and feed it back with --pin-roles.")
    p.add_argument("--pin-roles", default="",
                   help="pin roles from a file instead of deriving them from host "
                        "order. REQUIRED for a meaningful mixed-version run. "
                        "Format: one 'IP role' per line (quorum/sender/receiver); "
                        "'#' comments allowed.")
    p.add_argument("--scale", type=float, default=1.0,
                   help="multiplier for the volume of the SC-X-*/FT-X-* stress "
                        "cases (default 1.0 = the documented figures). Use 0.1 "
                        "for a quick shape-check of the stress suite itself, or "
                        "2.0+ to push a fleet that already passes.")
    p.add_argument("--version-label", default="",
                   help="manual fleet build label, e.g. '1.0.4' or a branch name. Only "
                        "used when --collect-versions is off.")
    p.add_argument("--collect-versions", action="store_true",
                   help="SSH each participating host and record its build PER ROLE. "
                        "Required for the mixed-fleet cases, where sender, receiver and "
                        "quorum can legitimately be on different versions and a single "
                        "fleet-wide label would be wrong. Adds a few seconds.")
    p.add_argument("--ssh-user", default="rubix", help="SSH user for --collect-versions")
    p.add_argument("--remote-dir", default="~/Desktop/rubix",
                   help="directory holding the rubixgoplatform binary on each host")
    args = p.parse_args()

    if args.suite:
        patterns, suite_modules = load_suite(args.suite)
        if not args.only:
            args.only = ",".join(patterns)
        if not args.cases:
            args.cases = ",".join(suite_modules)
        if not args.report_name:
            args.report_name = args.suite
    if not args.cases:
        sys.exit("ERROR: pass --cases <module> or --suite <name>.")

    module_names = [m.strip() for m in args.cases.split(",") if m.strip()]
    if not module_names:
        sys.exit("ERROR: --cases needs at least one module name.")

    modules = [(n, load_case_module(n)) for n in module_names]

    # Merge, preserving each module's own ORDER and the order they were listed.
    # A duplicate Test ID across modules is a mistake worth stopping for: the
    # two definitions would silently disagree about what the ID means.
    merged_cases, order, owner = {}, [], {}
    for name, mod in modules:
        for tid in mod.ORDER:
            if tid in merged_cases:
                sys.exit("ERROR: {} is defined in both {} and {} - a Test ID must "
                         "have exactly one definition.".format(tid, owner[tid], name))
            merged_cases[tid] = mod.CASES[tid]
            owner[tid] = name
            order.append(tid)

    class _Merged(object):
        pass
    module = _Merged()
    module.CASES = merged_cases
    module.ORDER = list(order)
    module.CASE_INFO = {}
    module.TIMING_CASES = set()
    for _n, mod in modules:
        module.CASE_INFO.update(getattr(mod, "CASE_INFO", {}) or {})
        module.TIMING_CASES |= set(getattr(mod, "TIMING_CASES", set()) or set())

    if len(modules) > 1:
        print("Running {} modules: {}".format(
            len(modules), ", ".join("{} ({})".format(n, len(m.ORDER)) for n, m in modules)))

    order = list(module.ORDER)
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        exact = {t for t in wanted if not t.endswith("*")}
        prefixes = tuple(t[:-1] for t in wanted if t.endswith("*"))

        def selected(tid):
            return tid in exact or (prefixes and tid.startswith(prefixes))

        order = [t for t in order if selected(t)]
        # Only exact IDs can be "unknown" - a prefix legitimately matches nothing
        # if that group is not in the loaded modules, but it is worth saying so
        # rather than silently running fewer cases than asked for.
        missing = exact - set(module.CASES)
        if missing:
            sys.exit("ERROR: unknown Test ID(s): {}".format(", ".join(sorted(missing))))
        empty = [p + "*" for p in prefixes if not any(t.startswith(p) for t in module.CASES)]
        if empty:
            sys.exit("ERROR: these patterns matched no case in {}: {}".format(
                args.cases, ", ".join(empty)))
    if not order:
        sys.exit("ERROR: nothing to run.")

    # A case module may carry its own catalogue text (CASE_INFO) instead of
    # having rows in master-test-cases.xlsx - the core-derived cases come from
    # the product's suite, not the sheet. Only read the workbook when some case
    # actually needs it, so those runs don't require openpyxl at all.
    case_info = getattr(module, "CASE_INFO", {})
    master = load_master()

    print("== Common: pool + reachability + DID readiness ==")
    hosts = rc.load_hosts(args.hosts)
    pool = [h for h in hosts if h["role"] not in FIXED_ROLES]
    ready, excluded = sweep_and_prepare(pool, args.port, rc.DEFAULT_TIMEOUT)
    print("{} in pool, {} ready, {} excluded".format(len(pool), len(ready), len(excluded)))
    for line in excluded:
        print("  excluded: {}".format(line))

    print("\n== Common: role assignment ==")
    if args.pin_roles:
        quorum_hosts, senders, receivers = load_pinned_roles(args.pin_roles, ready)
        print("Roles PINNED from {} - host order ignored".format(args.pin_roles))
    else:
        quorum_hosts, senders, receivers = assign_roles(ready, args.quorum_count)

    if args.dump_roles:
        dump_roles(args.dump_roles, quorum_hosts, senders, receivers)
        sys.exit(0)

    write_roles_file(ROLES_PATH, quorum_hosts, senders, receivers)
    print("Quorum: {}  Senders: {}  Receivers: {}".format(
        len(quorum_hosts), len(senders), len(receivers)))

    setup_report = _SetupReport()
    print("\n== Common: quorum setup + funding ==")
    setup_quorums(quorum_hosts, args, setup_report)
    time.sleep(2)
    print("\n== Common: assign quorum + fund senders ==")
    # When a module defines LANES, each lane funds its OWN hosts to what its
    # own cases actually spend. Bulk-funding every sender to --fund-sender
    # first would mint thousands of tokens nobody uses: minting is one token
    # per unit server-side (~15s per 1000), so 14 senders x 200 RBT is about
    # 40 seconds of pure waste before a single case runs. Subscribers are the
    # clearest example - they only ever execute, which costs nothing.
    #
    # Quorums are still funded in bulk here: they are shared by every lane and
    # must be able to pledge for all of them at once.
    if getattr(module, "LANES", None):
        print("  lanes present - senders are funded per lane, not in bulk")
        sender_quorum = {}
        for i, entry in enumerate(senders + receivers):
            sender_quorum[entry["host"]] = quorum_hosts[i % len(quorum_hosts)]
    else:
        sender_quorum = assign_and_fund_senders(quorum_hosts, senders, args,
                                                setup_report)
    if setup_report.failures:
        sys.exit("ERROR: common setup failed on: {}\nFix that before running cases - "
                 "results would be meaningless.".format(", ".join(setup_report.failures)))

    print("\n== {} cases ({}) ==".format(
        (args.report_name or args.cases).upper(), len(order)))

    # The script is still sequential WITHIN a lane. Lanes run at the same
    # time simply because the machines are there and idle - a lane is one
    # sender working through its own list, nothing more. Cases that must
    # follow one another (SC-S-*, FT-P-*) sit in a single lane and keep
    # their order.
    waves, unallocated = build_lanes(module, order, ready, quorum_hosts,
                                     sender_quorum, args)
    lanes = [l for w in waves for l in w]
    time.sleep(3)
    started = time.time()
    started_at = datetime.datetime.now()

    run_lanes(waves, module.CASES, master, case_info, args)

    # Merge lane results back into catalogue ORDER. Lanes finish out of
    # order; a report following completion order would be unreadable and
    # would not line up against a previous run.
    report = CatalogueReport()
    by_id = {}
    for ln in lanes:
        for tid, result, elapsed, ci in ln.rows:
            by_id[tid] = (ci, result, elapsed)
    for test_id in order:
        if test_id in by_id:
            ci, result, elapsed = by_id[test_id]
            report.add(ci, result, elapsed)
        elif test_id in unallocated:
            actual, note = unallocated[test_id]
            report.add(resolve_ci(test_id, master, case_info),
                       (SKIP, actual, note), 0.0)
    duration = time.time() - started
    lane_summary = ", ".join("{}({})".format(ln.name, len(ln.cases))
                             for ln in lanes)

    # Versions PER ROLE, not one fleet-wide label. In the mixed-fleet cases a
    # sender, receiver and quorum can each be on a different build, and the
    # result only means something if the report says which build each role
    # was actually running.
    version_rows = []
    if args.collect_versions:
        print("\nCollecting per-host versions over SSH...")
        role_of = {}
        for q in quorum_hosts:
            role_of[q["host"]] = "quorum"
        for s in senders:
            role_of.setdefault(s["host"], "sender")
        for r in receivers:
            role_of.setdefault(r["host"], "receiver")
        versions = rc.collect_versions(list(role_of), args.ssh_user, args.remote_dir)
        by_role = {}
        for host, ver in versions.items():
            by_role.setdefault(role_of[host], []).append((host, ver))
        for role in ("quorum", "sender", "receiver"):
            entries = sorted(by_role.get(role, []))
            if not entries:
                continue
            distinct = sorted({v for _h, v in entries})
            summary = "{}  [{}]".format(
                ", ".join("{}={}".format(h, v) for h, v in entries),
                "uniform: {}".format(distinct[0]) if len(distinct) == 1
                else "MIXED: {}".format(", ".join(distinct)))
            version_rows.append(("Version - {}s".format(role), summary))
        all_versions = sorted({v for v in versions.values()})
        version_rows.append((
            "Version - fleet",
            "uniform across all roles: {}".format(all_versions[0]) if len(all_versions) == 1
            else "MIXED FLEET: {} - per-role breakdown above".format(", ".join(all_versions))))
    else:
        version_rows.append((
            "Version",
            args.version_label or
            "not recorded. Re-run with --collect-versions to capture the build PER ROLE "
            "(needed for mixed-fleet cases), or pass --version-label for a manual note."))

    # Run conditions - without these a report can't be compared against
    # another run, which is the whole point of a regression lab.
    meta = {
        "asset": args.cases,
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_seconds": round(duration, 1),
        "conditions": [
            ("Run started", started_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Duration", "{:.0f}s".format(duration)),
            ("Asset / cases run", "{} - {} of {} in the catalogue".format(
                args.cases.upper(), len(order), len(module.ORDER))),
        ] + version_rows + [
            ("Hosts file", args.hosts),
            ("Pool hosts ready", "{} (of {} in pool); excluded: {}".format(
                len(ready), len(pool), len(excluded) or "none")),
            ("Quorum hosts", ", ".join("{} ({}...)".format(q["host"], q["did"][:12])
                                        for q in quorum_hosts)),
            ("Sender hosts", ", ".join(s["host"] for s in senders)),
            ("Receiver hosts", ", ".join(r["host"] for r in receivers)),
            ("Lanes", "{} lane(s) in {} wave(s): {}".format(
                len(lanes), len(waves), lane_summary)),
            ("Lane funding", "each lane funded for what its own cases spend, "
                             "plus a {} RBT margin; quorums {} RBT because "
                             "they are shared and carry every lane's "
                             "pledges".format(FUND_SAFETY_MARGIN,
                                              args.fund_quorum)),
            ("Quorum funding", "{} RBT each (caps the largest testable transfer - a "
                               "quorum must pledge >= the transfer value)".format(args.fund_quorum)),
            ("Sender funding", "per lane, sized to what that lane spends "
                               "(+{} RBT margin) rather than a flat "
                               "{} for every host".format(FUND_SAFETY_MARGIN,
                                                          args.fund_sender)),
            ("Reduced-scale flags", "--large-mint {} | --decimal-samples {} (catalogue asks 10) "
                                     "| --repeat-count {} (catalogue asks 1000)".format(
                                         args.large_mint, args.decimal_samples, args.repeat_count)),
            ("API port", str(args.port)),
        ],
    }
    if args.only:
        meta["conditions"].insert(3, ("Subset filter", "--only {}".format(args.only)))

    timing_ids = set(getattr(module, "TIMING_CASES", set()))
    print()
    report.save(args.report_name or args.cases.replace(",", "-"), meta, timing_ids)


class _SetupReport:
    """Minimal Report-alike for the common setup phase, which predates any
    Test ID. Tracks failures so cases never run against a broken fleet."""

    def __init__(self):
        self.failures = []

    def record(self, step, fn, **params):
        start = time.time()
        try:
            passed, actual, reason = fn()
        except Exception as e:
            passed, actual, reason = False, "exception", "{}: {}".format(type(e).__name__, e)
        elapsed = round(time.time() - start, 2)
        print("  [{:<4}] {:<24} {:>6.2f}s  {}{}".format(
            "PASS" if passed else "FAIL", step, elapsed, actual,
            "" if passed else "  -- " + reason))
        if not passed:
            self.failures.append(step)
        return passed


if __name__ == "__main__":
    main()
