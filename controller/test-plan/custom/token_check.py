#!/usr/bin/env python3
"""
token_check.py - which RBT tokens in circulation are fit for a test to use.

A PRE-CONDITION for the test runs (user, 2026-11-06): only tokens with a proper
history may be used. Every Free token a pool DID holds is checked on its owner's
node and against every other node's copy of its chain
(full-test/token_health.py). One code per reason:

    999  more than one owner              666  token row not at its chain's end
    888  split parent still held          555  last transfer went to another DID
    777  broken chain at the owner        444  another node's copy disagrees
    333  a Rubix stuck state (Orphaned, ChainSyncIssue, BeingDoubleSpent, ...)

Listed, never acted on: each fullnode's chain against the owner's, and every
DID whose token_denom differs from its Free tokens (node and DID).

AUDIT-START runs the same check, and marks, at the start of every test run;
this runs it on its own.

WHAT IT CHANGES ON THE LAB
    Nothing, unless --apply or --restore is given. --apply gives each listed
    token its code and lowers its DID's token_denom by it (one statement per
    token, one database transaction per node); --restore puts tokens back from
    the log, full-test/token-quarantine.jsonl. A reason that would mark more
    than 20% of the tokens checked is not applied without --no-limit: that
    points at the check, not the tokens. It takes the fleet lock, like a run.

RUN (on the controller, with no test run on the fleet)
    python3 controller/test-plan/custom/token_check.py                 # read-only report
    python3 controller/test-plan/custom/token_check.py --apply         # mark every reason
    python3 controller/test-plan/custom/token_check.py --apply 999,888 # only these reasons
    python3 controller/test-plan/custom/token_check.py --restore 444   # put the 444s back

Every token, by reason, goes to reports/json/token-check_<time>.json.
"""

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))         # controller/, for fleet.py

import fleet as fleet_inventory
import rubix_client as rc
import db_client as db
import run_lock
import token_health

REPORTS = os.path.join(HERE, "..", "..", "..", "reports", "json")


def _dids(node):
    """The node's DIDs from its API, as the runner reads them."""
    host = node["host"]
    ok, dids, note = rc.get_dids(host)
    if not ok:
        return host, None, "API: {}".format(note)
    if not dids:
        return host, None, "the node reports no DID"
    return host, list(dids), ""


def _codes(text):
    if text == "all":
        return token_health.CODES + (token_health.STUCK_CODE,)
    try:
        codes = tuple(int(c) for c in text.split(",") if c.strip())
    except ValueError:
        sys.exit("ERROR: codes are numbers separated by commas, e.g. 999,888")
    unknown = [c for c in codes if c not in db.QUARANTINE]
    if unknown or not codes:
        sys.exit("ERROR: unknown code(s) {} - the codes are {}".format(unknown, sorted(db.QUARANTINE)))
    return codes


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", nargs="?", const="all", metavar="CODES",
                   help="take the listed tokens out of use (all reasons, or e.g. 999,888)")
    p.add_argument("--restore", metavar="CODES",
                   help="put back the tokens the log says were marked with these codes ('all' = every code)")
    p.add_argument("--no-limit", action="store_true",
                   help="apply a reason even when it would mark more than 20%% of the tokens checked")
    p.add_argument("--hosts", default=fleet_inventory.HOSTS_PATH, metavar="FILE")
    args = p.parse_args()
    if not db.available():
        sys.exit("ERROR: psycopg2 is needed:  sudo apt install -y python3-psycopg2")
    run_lock.take("token_check.py", by="token_check.py")

    if args.restore:
        done, failed = token_health.restore(_codes(args.restore))
        for code, (n, value) in sorted(done.items()):
            print("restored {} token(s) marked {} ({:.3f} RBT)".format(n, code, value))
        for h, n, why in failed:
            print("could not restore {} token(s) on {}: {}".format(n, h, why))
        if not done and not failed:
            print("nothing to restore: no token in the log is still marked with {}".format(args.restore))
        return

    hosts = fleet_inventory.load_hosts(args.hosts)
    nodes = fleet_inventory.fleet(hosts, include_fixed=True)
    pool_nodes = [n for n in nodes if n["role"] == ""]
    fixed_nodes = [n for n in nodes if n["role"] in ("faucet", "faucet-quorum")]
    fullnodes = [n["host"] for n in nodes if n["role"] == "fullnode"]
    print("Reading {} pool node(s), {} faucet node(s) and {} fullnode(s) - {}.".format(
        len(pool_nodes), len(fixed_nodes), len(fullnodes),
        "then marking" if args.apply else "read-only"))
    with ThreadPoolExecutor(max_workers=32) as ex:
        found = list(ex.map(_dids, pool_nodes + fixed_nodes))
    pool = dict((h, d) for h, d, _w in found[:len(pool_nodes)] if d)
    fixed = dict((h, d) for h, d, _w in found[len(pool_nodes):] if d)

    t0 = time.time()
    result = token_health.survey(pool, fixed, fullnodes)
    applied = None
    if args.apply:
        applied = token_health.apply(result, _codes(args.apply), by="token_check.py",
                                     limit=0 if args.no_limit else token_health.MAX_SHARE)
    print()
    for line in token_health.lines(result, applied):
        print(line)
    for h, d, why in found:
        if not d:
            print("no DIDs read from {}: {} - its tokens are not in these figures".format(h, why))

    os.makedirs(REPORTS, exist_ok=True)
    out = os.path.abspath(os.path.join(REPORTS, "token-check_{}.json".format(
        time.strftime("%Y-%m-%d_%H-%M-%S"))))
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(token_health.evidence(result, applied), fh, indent=1)
    print("\nEvery token, by reason: {}".format(out))
    print("Took {:.0f} s.".format(time.time() - t0))
    if not args.apply and (any(result["found"].values()) or result["stuck"]):
        print("Nothing was changed. To take them out of use: "
              "python3 controller/test-plan/custom/token_check.py --apply")


if __name__ == "__main__":
    main()
