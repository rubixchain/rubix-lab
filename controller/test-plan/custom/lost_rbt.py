#!/usr/bin/env python3
"""
lost_rbt.py - where is the RBT the fleet audit cannot find?

THE QUESTION
    AUDIT-START on 2026-10-22 counted 399,966.471 RBT against the 400,000 the
    faucet minted, and that run lost 4.000 more. The audit reads each node's
    tokens for that node's OWN DIDs only (master/audit_cases.py), so value can
    sit in two places it never looks:

    NOT DELIVERED  the sender's row says Transferred and no node holds the
                   token at all. A transfer returns success BEFORE the receiver
                   has the tokens: the push runs once, in a goroutine, and when
                   the receiver cannot be reached it logs "Receiver offline,
                   will sync later" and stops (core/transaction.go:537,
                   :600-606). Nothing retries it, and the receiver's own sync
                   needs a fullnode the lab cannot reach.
    WRONG NODE     a node holds the token (Free, Locked, Pledged...) under a
                   DID it does not host.

    For each token it finds, it prints the transaction that last moved it (from
    the sender's token chain), that transaction's initiator and owner, and the
    node the owner lives on - so a loss can be pinned to the receiver that
    never got it.

    --tx ID ...  also says which nodes' `transactions` tables hold these IDs -
                 for a case whose DB check reported a transaction missing on a
                 receiver (FT-01 on 2026-10-22: maybe late, maybe never).

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
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))         # controller/, for fleet.py

import fleet as fleet_inventory
import rubix_client as rc
import db_client as db

MINTED = 400000.0
HELD = (db.FREE, db.LOCKED, db.COMMITTED, db.PLEDGED, db.QUORUM_PLEDGED, db.BURNT_FOR_FT)
NODE_ROLES = ("", "faucet", "faucet-quorum")     # fullnodes hold no DIDs of their own


def _read(node):
    """One read-only snapshot of a node: its DIDs (from its API, as the runner
    reads them), what it holds for them, its Transferred rows, and anything it
    holds for a DID it does not host."""
    host = node["host"]
    ok, dids, note = rc.get_dids(host)
    if not ok:
        return host, None, "API: {}".format(note)
    dids = list(dids or [])
    if not dids:
        return host, None, "the node reports no DID"
    try:
        held, own_ids, sent, foreign = db.query_snapshot(host, [
            ("SELECT COALESCE(SUM(token_value), 0) FROM tokens WHERE token_type = %s "
             "AND did = ANY(%s) AND token_status = ANY(%s)", (db.TYPE_RBT, dids, list(HELD))),
            # Any row but Transferred means the token is still accounted for here
            # (held, Burnt as a split parent, or stuck - the audit reports those).
            ("SELECT token_id FROM tokens WHERE token_type = %s AND did = ANY(%s) "
             "AND token_status <> %s", (db.TYPE_RBT, dids, db.TRANSFERRED)),
            ("SELECT token_id, did, token_value FROM tokens WHERE token_type = %s "
             "AND did = ANY(%s) AND token_status = %s", (db.TYPE_RBT, dids, db.TRANSFERRED)),
            ("SELECT token_id, did, token_status, token_value FROM tokens WHERE token_type = %s "
             "AND NOT (did = ANY(%s)) AND token_status = ANY(%s)", (db.TYPE_RBT, dids, list(HELD))),
        ])
    except Exception as e:
        return host, None, "DB: {}: {}".format(type(e).__name__, e)
    return host, {"dids": dids, "held": float(held[0][0]), "own": set(t for (t,) in own_ids),
                  "sent": sent, "foreign": foreign}, ""


def _last_move(host, token_ids):
    """{token_id: (position, transaction_id)} - the latest chain entry for each."""
    rows = db.query(host, "SELECT token_id, position, transaction_id FROM tokenchain "
                          "WHERE token_id = ANY(%s)", (list(token_ids),))
    out = {}
    for t, pos, tx in rows:
        if t not in out or int(pos) > out[t][0]:
            out[t] = (int(pos), tx)
    return out


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
        readings = list(ex.map(_read, nodes))

    unreadable = [(h, why) for h, r, why in readings if r is None]
    snaps = dict((h, r) for h, r, _w in readings if r is not None)
    where = {}                                  # did -> host that serves it
    for h, r in snaps.items():
        for d in r["dids"]:
            where[d] = h

    accounted = set()
    for r in snaps.values():
        accounted |= r["own"]
    # A token held for a foreign DID is extra value only if no node holds it
    # for its own DID too; otherwise it is a stale copy, listed but not added.
    wrong, copies = [], []                      # (host, token_id, did, status, value)
    for h, r in snaps.items():
        for t, d, st, v in r["foreign"]:
            (copies if t in accounted else wrong).append((h, t, d, int(st), float(v)))
    accounted |= set(t for _h, t, _d, _st, _v in wrong)
    # Not delivered: Transferred somewhere, accounted for nowhere.
    lost = {}                                   # token_id -> [(host, did, value)]
    for h, r in snaps.items():
        for t, d, v in r["sent"]:
            if t not in accounted:
                lost.setdefault(t, []).append((h, d, float(v)))

    # The last sender of each lost token is the one whose chain goes furthest.
    by_host = {}
    for t, rows in lost.items():
        for h, _d, _v in rows:
            by_host.setdefault(h, set()).add(t)
    last = {}                                   # token_id -> (position, tx, host, value)
    for h, ids in by_host.items():
        try:
            moves = _last_move(h, ids)
        except Exception:
            continue
        for t, (pos, tx) in moves.items():
            value = next(v for hh, _d, v in lost[t] if hh == h)
            if t not in last or pos > last[t][0]:
                last[t] = (pos, tx, h, value)

    by_tx = {}                                  # (tx, sender host) -> [values]
    for t in lost:
        pos, tx, h, value = last.get(t, (0, "?", lost[t][0][0], lost[t][0][2]))
        by_tx.setdefault((tx, h), []).append((t, value))

    held = sum(r["held"] for r in snaps.values())
    lost_value = sum(v for rows in by_tx.values() for _t, v in rows)
    wrong_value = sum(v for _h, _t, _d, _st, v in wrong)
    print()
    print("held by its own node      {:>14.3f} RBT   (what the audit counts)".format(held))
    print("NOT DELIVERED             {:>14.3f} RBT   in {} token(s), {} transaction(s)".format(
        lost_value, len(lost), len(by_tx)))
    print("on the WRONG node         {:>14.3f} RBT   in {} token(s)".format(wrong_value, len(wrong)))
    print("minted by the faucet      {:>14.3f} RBT".format(MINTED))
    print("still unexplained         {:>14.3f} RBT".format(MINTED - held - lost_value - wrong_value))
    if unreadable:
        print("\n{} node(s) could not be read, so the figures above are incomplete:".format(len(unreadable)))
        for h, why in unreadable:
            print("  {}  {}".format(h, why))

    if by_tx:
        print("\nNot delivered - sent, and held by nobody (largest first):")
        for (tx, h), rows in sorted(by_tx.items(), key=lambda kv: -sum(v for _t, v in kv[1])):
            initiator = owner = "?"
            if tx != "?":
                try:
                    initiator, owner = db.transaction_participants(h, tx)
                except Exception:
                    pass
            print("  tx {}  {:.3f} RBT in {} token(s), e.g. {}".format(
                tx, sum(v for _t, v in rows), len(rows), rows[0][0]))
            print("     from {} on {}  ->  {} on {}".format(
                (initiator or "?")[:24], h, (owner or "?")[:24], where.get(owner, "not a fleet DID")))
    for title, rows in (("On the wrong node - held for a DID that node does not host", wrong),
                        ("Stale copies - also held by the DID's own node, so not counted twice", copies)):
        if rows:
            print("\n{} ({} token(s), first 50):".format(title, len(rows)))
            for h, t, d, st, v in sorted(rows)[:50]:
                print("  {}  token {} {:.3f} RBT, {}, for {} (served by {})".format(
                    h, t, v, db.STATUS_NAME.get(st, st), d[:24], where.get(d, "no fleet node")))

    if args.tx:
        print("\nWhich nodes record these transactions:")
        found = dict((t, []) for t in args.tx)

        def present(h):
            try:
                return h, db.transactions_present(h, args.tx)
            except Exception:
                return h, set()
        with ThreadPoolExecutor(max_workers=32) as ex:
            for h, have in ex.map(present, list(snaps)):
                for t in have:
                    found[t].append(h)
        for t in args.tx:
            print("  {}  {}".format(t, ", ".join(sorted(found[t])) or "on no node"))


if __name__ == "__main__":
    main()
