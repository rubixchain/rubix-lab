#!/usr/bin/env python3
"""
lost_rbt.py - where is the RBT the fleet audit cannot find?

THE QUESTION
    The audit reads each node's tokens for that node's OWN DIDs only
    (master/audit_cases.py), so value can sit where it never looks. This puts
    every RBT the faucet minted in one bucket (full-test/rbt_ledger.py):

    held           a node holds the token for a DID it hosts
    NOT DELIVERED  the sender's row says Transferred and no node holds the token
                   for a DID it hosts. A transfer returns success BEFORE the
                   receiver has the tokens: the push runs once, in a goroutine,
                   and when the receiver cannot be reached it logs "Receiver
                   offline, will sync later" and stops (core/transaction.go:537,
                   :600-606). Quorums that validated it keep copies of the token
                   under an earlier holder's DID - named, not counted.
    WRONG NODE     a node holds the token for a DID it does not host, and no
                   sender marked it Transferred - once per token.
    untraced       the rest.

    Each undelivered transfer is printed with its time (the transaction's
    epoch), newest first, its sender and receiver and the receiver's node - so a
    loss can be pinned to a run and to the receiver that never got it.

    --tx ID ...  also says which nodes' `transactions` tables hold these IDs -
                 for a case whose DB check reported a transaction missing on a
                 receiver.

WHAT IT CHANGES ON THE LAB
    Nothing. Every query runs on a read-only connection (db_client). Run it
    with no run on the fleet: tokens move while it reads.

RUN (on the controller)
    python3 controller/test-plan/custom/lost_rbt.py
    python3 controller/test-plan/custom/lost_rbt.py --tx <transaction id> ...
"""

import argparse
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
import rbt_ledger

NODE_ROLES = ("", "faucet", "faucet-quorum")     # fullnodes hold no DIDs of their own


def _dids(node):
    """The node's DIDs from its API, as the runner reads them."""
    host = node["host"]
    ok, dids, note = rc.get_dids(host)
    if not ok:
        return host, None, "API: {}".format(note)
    if not dids:
        return host, None, "the node reports no DID"
    return host, list(dids), ""


def _when(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else "time unknown"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tx", nargs="*", default=[], metavar="ID",
                   help="also show which nodes' transactions tables hold these IDs")
    p.add_argument("--hosts", default=fleet_inventory.HOSTS_PATH, metavar="FILE")
    args = p.parse_args()
    if not db.available():
        sys.exit("ERROR: psycopg2 is needed:  sudo apt install -y python3-psycopg2")

    hosts = fleet_inventory.load_hosts(args.hosts)
    nodes = [n for n in fleet_inventory.fleet(hosts, include_fixed=True) if n["role"] in NODE_ROLES]
    print("Reading {} node(s) - read-only.".format(len(nodes)))
    with ThreadPoolExecutor(max_workers=32) as ex:
        found = list(ex.map(_dids, nodes))
    members = dict((h, d) for h, d, _w in found if d)
    no_dids = [(h, why) for h, d, why in found if not d]
    led = rbt_ledger.account(members)
    unreadable = no_dids + led["unreadable"]
    where = led["where"]

    print()
    print("held by its own node      {:>14.3f} RBT   (what the audit counts)".format(led["held"]))
    print("NOT DELIVERED             {:>14.3f} RBT   in {} token(s), {} transaction(s)".format(
        led["not_delivered"], len(led["lost_tokens"]), len(led["lost"])))
    print("on the WRONG node         {:>14.3f} RBT   in {} token(s)".format(
        led["wrong_node"], len(led["wrong"])))
    print("minted by the faucet      {:>14.3f} RBT".format(led["minted"]))
    print("still untraced            {:>14.3f} RBT".format(led["untraced"]))
    print("({} stale copy row(s) of tokens their owner holds - not counted)".format(led["copies"]))
    if unreadable:
        print("\n{} node(s) could not be read, so the figures above are incomplete:".format(len(unreadable)))
        for h, why in unreadable:
            print("  {}  {}".format(h, why))

    if led["lost"]:
        print("\nNot delivered - sent, and held by no node for its own DID (newest first):")
        for g in led["lost"]:
            print("  {}  tx {}  {:.3f} RBT in {} token(s), e.g. {}".format(
                _when(g["epoch"]), g["tx"], g["value"], len(g["tokens"]), g["tokens"][0][0]))
            print("     from {} on {}  ->  {} on {}".format(
                (g["initiator"] or "?")[:24], g["sender_host"], (g["owner"] or "?")[:24],
                g["owner_host"] or "not a fleet DID"))
            if g["copies_on"]:
                print("     copies kept on {}".format(", ".join(g["copies_on"])))
    if led["wrong"]:
        print("\nOn the wrong node - held for a DID that node does not host ({} token(s), first 50):".format(
            len(led["wrong"])))
        for h, t, d, st, v in led["wrong"][:50]:
            print("  {}  token {} {:.3f} RBT, {}, for {} (served by {})".format(
                h, t, v, db.STATUS_NAME.get(st, st), d[:24], where.get(d, "no fleet node")))

    if args.tx:
        print("\nWhich nodes record these transactions:")
        present_on = dict((t, []) for t in args.tx)

        def present(h):
            try:
                return h, db.transactions_present(h, args.tx)
            except Exception:
                return h, set()
        with ThreadPoolExecutor(max_workers=32) as ex:
            for h, have in ex.map(present, list(members)):
                for t in have:
                    present_on[t].append(h)
        for t in args.tx:
            print("  {}  {}".format(t, ", ".join(sorted(present_on[t])) or "on no node"))


if __name__ == "__main__":
    main()
