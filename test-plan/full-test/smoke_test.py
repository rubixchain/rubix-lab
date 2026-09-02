#!/usr/bin/env python3
"""
smoke_test.py - One-shot, whole-fleet smoke test proving the entire flow
works: role assignment -> DID readiness -> quorum setup+funding -> the
actual test cases in test_cases.py.

Deliberately split in two:
    - THIS file is the common part only (pool, DID readiness, role
      assignment, quorum setup, funding). It should rarely need to change.
    - test_cases.py is everything that actually gets tested (RBT/FT/NFT/SC
      right now). To add, remove, or change a case, edit ONLY that file -
      write a function (ctx, report) -> None and add it to its TEST_CASES
      list. This file just loops over whatever's in there.

Single script by design: the point of a smoke test is proving the whole
pipeline end to end in one run. The modular per-asset runners
(test-plan/rbt/, test-plan/ft/, ...) are the separate, later thing for
running the full 250-case catalogue piece by piece - this isn't a
replacement for those, it shares the same rubix_client.py underneath.

Common-part flow (see also project memory "test-flow-design"):
    1. Load hosts.txt -> pool (exclude down + fixed-role hosts).
    2. Reachability + DID sweep. 0 DIDs -> create (hard-gated: only ever on
       a confirmed-empty host). >1 DIDs -> excluded, flagged for a human.
    3. Assign roles: N quorum hosts, remaining split into senders/receivers
       (alternating pairs). Written to smoke-test-roles.txt for visual
       cross-reference against the physical machines.
    4. Announce pass (register+signature) on every involved host.
    5. Quorum setup + funding on the quorum hosts.
    6. Each sender registers exactly ONE quorum (round-robin across the N
       quorums, not all of them) - a sender's first-registered quorum is
       its primary signer, so this is what actually spreads load across
       quorums rather than everyone silently using quorum #1.
    7. Fund each sender.
    8. Hand off to test_cases.TEST_CASES.
    9. Write an informative report: params used, pass/fail, actual result,
       failure reason, timing, per row.

Usage:
    python3 smoke_test.py
    python3 smoke_test.py --quorum-count 3 --fund-quorum 1000 --fund-sender 20
"""

import argparse
import datetime
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rubix_client as rc
from test_cases import SmokeContext, TEST_CASES

HERE = os.path.dirname(os.path.abspath(__file__))
FIXED_ROLES = {"fullnode", "explorer", "controller"}
DEFAULT_HOSTS = os.path.join(HERE, "..", "..", "controller", "hosts.txt")
ROLES_PATH = os.path.join(HERE, "smoke-test-roles.txt")

# Delays at genuine async-propagation points. These do NOT fix the token-ID
# collision bug (that's a product-level issue, see rubix_client.py's
# allocate_token_index_range comment) - they're for the separate, real gaps
# where this script fires the next step before the network has had any
# chance to catch up: pubsub announce propagation, and a stagger so a batch
# of senders doesn't hit the same quorum in the same instant.
ANNOUNCE_SETTLE_SECONDS = 3     # after the DID/peer announce pass
QUORUM_SETTLE_SECONDS = 2       # after quorum setup+funding, before senders register against them
INTER_SENDER_DELAY_SECONDS = 0.3  # between each sender's assign+fund, inside the loop
PRE_TESTCASE_SETTLE_SECONDS = 3   # after all common setup, before the first test case fires


# ---------------------------------------------------------------------------
# Reporting - shared by the common part and every test case
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.rows = []

    def record(self, step, fn, **params):
        """fn() -> (passed: bool, actual: str, failure_reason: str)
        params are whatever's relevant to this step (hosts/DIDs/amounts) -
        stored verbatim so a failure is debuggable without re-running."""
        start = time.time()
        try:
            passed, actual, reason = fn()
        except Exception as e:
            passed, actual, reason = False, "exception", "{}: {}".format(type(e).__name__, e)
        elapsed = round(time.time() - start, 2)
        row = {
            "step": step, "passed": passed, "actual": actual,
            "reason": "" if passed else reason, "seconds": elapsed,
            "params": json.dumps(params, default=str),
        }
        self.rows.append(row)
        print("  [{:<6}] {:<28} {:>6.2f}s  {}{}".format(
            "PASS" if passed else "FAIL", step, elapsed, actual,
            "" if passed else "  -- " + reason))
        return passed

    def save(self, path):
        headers = ["Step", "Pass/Fail", "Actual Result", "Failure Reason",
                   "Seconds", "Params Used"]
        rows = [[r["step"], "PASS" if r["passed"] else "FAIL", r["actual"],
                 r["reason"], r["seconds"], r["params"]] for r in self.rows]
        rc.write_pdf_report(path, "Rubix Lab - Smoke Test Report", headers, rows)
        passed = sum(1 for r in self.rows if r["passed"])
        print("\n{}/{} steps passed. Report: {}".format(passed, len(self.rows), path))


# ---------------------------------------------------------------------------
# Common part: pool, DID readiness, role assignment
# ---------------------------------------------------------------------------
def sweep_and_prepare(pool, port, timeout):
    """Reachability + DID check; create a DID where genuinely absent
    (hard-gated), announce every resulting host. Returns list of
    {"host","did"} for hosts that ended up ready, and a list of exclusions."""
    def check(entry):
        host = entry["host"]
        reachable, dids, note = rc.get_dids(host, port, timeout)
        return {"host": host, "reachable": reachable, "dids": dids, "note": note}

    with ThreadPoolExecutor(max_workers=min(40, len(pool))) as ex:
        results = list(ex.map(check, pool))

    ready, excluded = [], []
    for r in results:
        if not r["reachable"]:
            excluded.append("{} - unreachable ({})".format(r["host"], r["note"]))
            continue
        n = len(r["dids"])
        if n == 1:
            ready.append({"host": r["host"], "did": r["dids"][0]})
        elif n == 0:
            did, msg = rc.create_did(r["host"], port)
            if not did:
                excluded.append("{} - DID create failed: {}".format(r["host"], msg))
                continue
            ready.append({"host": r["host"], "did": did})
        else:
            excluded.append("{} - {} DIDs, ambiguous, needs a human".format(r["host"], n))

    for entry in ready:
        rc.announce_did(entry["host"], entry["did"], port)

    if ready:
        print("Letting the announce pass propagate ({}s)...".format(ANNOUNCE_SETTLE_SECONDS))
        time.sleep(ANNOUNCE_SETTLE_SECONDS)

    return ready, excluded


def assign_roles(ready, quorum_count):
    if len(ready) < quorum_count + 2:
        sys.exit("ERROR: need at least {} ready hosts ({} quorum + 2), only have {}.".format(
            quorum_count + 2, quorum_count, len(ready)))
    quorum_hosts = ready[:quorum_count]
    rest = ready[quorum_count:]
    senders = rest[0::2]
    receivers = rest[1::2]
    if not senders or not receivers:
        sys.exit("ERROR: not enough non-quorum hosts to form sender/receiver pairs.")
    return quorum_hosts, senders, receivers


def write_roles_file(path, quorum_hosts, senders, receivers):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("Smoke test role assignment - {}\n".format(
            datetime.datetime.now().isoformat(timespec="seconds")))
        fh.write("For visually cross-checking against the physical machines.\n\n")
        for i, e in enumerate(quorum_hosts, 1):
            fh.write("{} - quorum {}\n".format(e["host"], i))
        for i, e in enumerate(senders, 1):
            fh.write("{} - sender {}\n".format(e["host"], i))
        for i, e in enumerate(receivers, 1):
            fh.write("{} - receiver {}\n".format(e["host"], i))
    print("Role assignment written to {}".format(path))


def setup_quorums(quorum_hosts, args, report):
    for i, q in enumerate(quorum_hosts, 1):
        def do_setup(q=q):
            ok, msg = rc.quorum_setup(q["host"], q["did"], args.port)
            return ok, msg, msg
        report.record("quorum-setup-{}".format(i), do_setup, host=q["host"], did=q["did"])

        def do_fund(q=q):
            ok0, bal0, _ = rc.get_rbt_balance(q["host"], q["did"], args.port)
            current = bal0 or 0
            if current >= args.fund_quorum:
                return True, "already at {} RBT".format(current), ""
            status, msg = rc.fund_did(q["host"], q["did"], args.fund_quorum - int(current), args.port)
            if not status:
                return False, "mint rejected", msg
            confirmed, bal1 = rc.wait_for_balance(q["host"], q["did"], args.fund_quorum, args.port)
            if not confirmed:
                return False, "balance did not reach target", "before={} after={}".format(bal0, bal1)
            return True, "{} -> {}".format(bal0, bal1), ""
        report.record("quorum-fund-{}".format(i), do_fund, host=q["host"], did=q["did"],
                       target_rbt=args.fund_quorum)


def assign_and_fund_senders(quorum_hosts, senders, args, report):
    """Returns {sender_host: quorum_entry} - round-robin, one quorum per
    sender (not all of them - see module docstring on why that matters)."""
    sender_quorum = {}
    for i, s in enumerate(senders):
        q = quorum_hosts[i % len(quorum_hosts)]
        sender_quorum[s["host"]] = q

        def do_add(s=s, q=q):
            ok, msg = rc.quorum_add(s["host"], q["did"], args.port)
            return ok, msg, msg
        report.record("assign-quorum-sender-{}".format(i + 1), do_add,
                       sender_host=s["host"], quorum_host=q["host"], quorum_did=q["did"])

        def do_fund(s=s):
            ok0, bal0, _ = rc.get_rbt_balance(s["host"], s["did"], args.port)
            current = bal0 or 0
            need = args.fund_sender
            if current >= need:
                return True, "already at {} RBT".format(current), ""
            status, msg = rc.fund_did(s["host"], s["did"], need - int(current), args.port)
            if not status:
                return False, "mint rejected", msg
            confirmed, bal1 = rc.wait_for_balance(s["host"], s["did"], need, args.port)
            if not confirmed:
                return False, "balance did not reach target", "before={} after={}".format(bal0, bal1)
            return True, "{} -> {}".format(bal0, bal1), ""
        report.record("fund-sender-{}".format(i + 1), do_fund, host=s["host"], target_rbt=args.fund_sender)
        time.sleep(INTER_SENDER_DELAY_SECONDS)
    return sender_quorum


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=DEFAULT_HOSTS)
    p.add_argument("--port", type=int, default=rc.DEFAULT_PORT)
    p.add_argument("--quorum-count", type=int, default=3)
    p.add_argument("--fund-quorum", type=int, default=1000)
    p.add_argument("--fund-sender", type=int, default=20)
    p.add_argument("--rbt-amount", type=float, default=1)
    p.add_argument("--ft-count", type=int, default=10)
    p.add_argument("--ft-token-count", type=int, default=1)
    args = p.parse_args()
    report = Report()

    print("== Common: pool + reachability + DID readiness ==")
    hosts = rc.load_hosts(args.hosts)
    pool = [h for h in hosts if h["role"] not in FIXED_ROLES]
    ready, excluded = sweep_and_prepare(pool, args.port, rc.DEFAULT_TIMEOUT)
    print("{} host(s) in pool, {} ready, {} excluded".format(len(pool), len(ready), len(excluded)))
    for line in excluded:
        print("  excluded: {}".format(line))

    print("\n== Common: role assignment ==")
    quorum_hosts, senders, receivers = assign_roles(ready, args.quorum_count)
    write_roles_file(ROLES_PATH, quorum_hosts, senders, receivers)
    print("Quorum: {}  Senders: {}  Receivers: {}".format(
        len(quorum_hosts), len(senders), len(receivers)))

    print("\n== Common: quorum setup + funding ==")
    setup_quorums(quorum_hosts, args, report)
    print("Letting quorum funding settle ({}s)...".format(QUORUM_SETTLE_SECONDS))
    time.sleep(QUORUM_SETTLE_SECONDS)

    print("\n== Common: assign quorum to each sender (round-robin) + fund senders ==")
    sender_quorum = assign_and_fund_senders(quorum_hosts, senders, args, report)

    print("\n== Test cases (test_cases.py) ==")
    print("Letting everything settle before the first test case fires ({}s)...".format(
        PRE_TESTCASE_SETTLE_SECONDS))
    time.sleep(PRE_TESTCASE_SETTLE_SECONDS)
    ctx = SmokeContext(args.port, quorum_hosts, senders, receivers, sender_quorum, args)
    for case_fn in TEST_CASES:
        case_fn(ctx, report)

    print()
    report.save(rc.new_report_path("smoke_test"))


if __name__ == "__main__":
    main()
