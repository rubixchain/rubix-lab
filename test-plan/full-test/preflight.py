#!/usr/bin/env python3
"""
preflight.py - Stage A of the test flow (see project memory "test-flow-design"
/ the CLAUDE.md-adjacent design discussion this came from). Run once per test
cycle, before any asset runner:

    1. Load hosts.txt, drop down/excluded and fixed-role (fullnode/explorer/
       controller) hosts -> the pool.
    2. Reachability + DID sanity sweep. A pool host without exactly 1 DID is
       excluded and flagged for a human - never auto-fixed here (CreateDID
       has zero idempotency, see CLAUDE.md; this script never calls it).
    3. Reserve a quorum-capable subset, set each up as a quorum
       (POST /rubix/v1/quorums/setup) and fund it well above the floor.
    4. Register every quorum DID on every remaining (participant-capable)
       host, in a stable order (first registered = primary sender quorum -
       not guaranteed stable by the product's own DB query, but stable
       within one preflight run since we control the order here).
    5. Announce pass: re-broadcast every ready host's DID (register +
       signature, not create) so peer/DID knowledge is fresh fleet-wide.
    6. Write a context file the asset runners read to know who's who.

Requires openpyxl only indirectly (not used here); needs rubix_client.py
beside this script.

Usage:
    python3 preflight.py                          # defaults below
    python3 preflight.py --quorum-count 3 --participant-count 10
"""

import argparse
import datetime
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rubix_client as rc

FIXED_ROLES = {"fullnode", "explorer", "controller"}
DEFAULT_HOSTS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "..", "..", "controller", "hosts.txt")


def sweep_pool(pool, port, timeout):
    def check(entry):
        host = entry["host"]
        reachable, dids, note = rc.get_dids(host, port, timeout)
        did = dids[0] if len(dids) == 1 else None
        return {"host": host, "reachable": reachable, "dids": dids, "did": did, "note": note}

    with ThreadPoolExecutor(max_workers=min(40, len(pool))) as ex:
        return list(ex.map(check, pool))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=DEFAULT_HOSTS,
                   help="hosts.txt path (default: controller/hosts.txt)")
    p.add_argument("--port", type=int, default=rc.DEFAULT_PORT)
    p.add_argument("--quorum-count", type=int, default=3,
                   help="quorum-capable hosts to reserve (default: %(default)s, per SETUP-RUNBOOK's minimum)")
    p.add_argument("--participant-count", type=int, default=6,
                   help="participant-capable hosts to reserve for this cycle (default: %(default)s)")
    p.add_argument("--fund-amount", type=int, default=5000,
                   help="RBT to mint per quorum host (default: %(default)s - well above the 1000 floor)")
    p.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "preflight-context.json"))
    args = p.parse_args()

    print("== Preflight ==")
    hosts = rc.load_hosts(args.hosts)
    pool = [h for h in hosts if h["role"] not in FIXED_ROLES]
    print("{} host(s) in hosts.txt, {} in the generic pool (excluding fixed-role hosts)".format(
        len(hosts), len(pool)))

    results = sweep_pool(pool, args.port, rc.DEFAULT_TIMEOUT)
    ready = [r for r in results if r["reachable"] and r["did"]]
    down = [r for r in results if not r["reachable"]]
    ambiguous = [r for r in results if r["reachable"] and not r["did"]]

    print("Reachable: {}/{}  Ready (1 DID): {}".format(
        len(results) - len(down), len(results), len(ready)))
    if down:
        print("DOWN, excluded: {}".format(", ".join(r["host"] for r in down)))
    if ambiguous:
        print("Ambiguous DID count, excluded, needs a human: {}".format(
            ", ".join("{} ({} DIDs)".format(r["host"], len(r["dids"])) for r in ambiguous)))

    need = args.quorum_count + args.participant_count
    if len(ready) < need:
        sys.exit("ERROR: need {} ready hosts ({} quorum + {} participant), only {} available.".format(
            need, args.quorum_count, args.participant_count, len(ready)))

    quorum_hosts = ready[:args.quorum_count]
    participant_hosts = ready[args.quorum_count:args.quorum_count + args.participant_count]

    print("\n-- Setting up {} quorum host(s) --".format(len(quorum_hosts)))
    for r in quorum_hosts:
        ok, msg = rc.quorum_setup(r["host"], r["did"], args.port)
        print("  {:<15} setup: {} ({})".format(r["host"], "OK" if ok else "FAILED", msg))
        if not ok and "already" not in msg.lower():
            sys.exit("ERROR: quorum setup failed on {} - {}".format(r["host"], msg))

    print("\n-- Funding quorum host(s) to >= {} RBT --".format(args.fund_amount))
    for r in quorum_hosts:
        ok, bal, _ = rc.get_rbt_balance(r["host"], r["did"], args.port)
        current = bal if ok and bal is not None else 0
        if current >= args.fund_amount:
            print("  {:<15} already at {} RBT, skipping mint".format(r["host"], current))
            continue
        top_up = args.fund_amount - int(current)
        ok, msg = rc.fund_did(r["host"], r["did"], top_up, args.port)
        if not ok:
            sys.exit("ERROR: funding failed on {} - {}".format(r["host"], msg))
        confirmed, bal = rc.wait_for_balance(r["host"], r["did"], args.fund_amount, args.port)
        print("  {:<15} minted {} -> balance {} ({})".format(
            r["host"], top_up, bal, "confirmed" if confirmed else "NOT CONFIRMED, check manually"))

    print("\n-- Registering quorums on every participant host (stable order) --")
    for p_entry in participant_hosts:
        for q_entry in quorum_hosts:
            ok, msg = rc.quorum_add(p_entry["host"], q_entry["did"], args.port)
            status = "OK" if ok else "FAILED"
            print("  {:<15} += quorum {}  {} ({})".format(
                p_entry["host"], q_entry["did"][:16] + "...", status, msg))
            if not ok:
                sys.exit("ERROR: quorum_add failed on {} for {} - {}".format(
                    p_entry["host"], q_entry["did"], msg))

    print("\n-- Announce pass (re-broadcast DID/peer mapping for every ready host) --")
    for r in ready:
        ok, msg, _ = rc.announce_did(r["host"], r["did"], args.port)
        print("  {:<15} {} ({})".format(r["host"], "OK" if ok else "FAILED", msg))

    context = {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "port": args.port,
        "quorum_hosts": [{"host": r["host"], "did": r["did"]} for r in quorum_hosts],
        "participant_hosts": [{"host": r["host"], "did": r["did"]} for r in participant_hosts],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(context, fh, indent=2)
    print("\nContext written to {}".format(args.out))
    print("Quorum hosts: {}".format(", ".join(r["host"] for r in quorum_hosts)))
    print("Participant hosts: {}".format(", ".join(r["host"] for r in participant_hosts)))


if __name__ == "__main__":
    main()
