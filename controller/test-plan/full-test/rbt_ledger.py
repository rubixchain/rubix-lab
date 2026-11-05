"""rbt_ledger.py - where every RBT the faucet minted is, node by node.

Used by the fleet audit (master/audit_cases.py) and by custom/lost_rbt.py.
Read-only: every query runs on a read-only connection (db_client).

The fleet is a test bed and tokens do go astray (user, 2026-11-05: "we need the
count and accountability for that"), so this does not judge - it puts every RBT
in one of these:

    held           a node holds the token for a DID it hosts, in a held status
                   (Free, Locked, Committed, Pledged, QuorumPledged, BurntForFT)
    not delivered  a sender's row says Transferred and no node holds the token
                   for a DID it hosts. The sender reports success before the
                   receiver has the tokens: the push runs once, in a goroutine,
                   and when the receiver cannot be reached it logs "Receiver
                   offline, will sync later" and stops (core/transaction.go:537,
                   :600-606; RUBIX-ISSUES.md R-06). Quorums that validated the
                   transfer keep copy rows of the token under an earlier holder's
                   DID (R-28): listed with the transaction, never counted - until
                   2026-11-05 they hid the token from this bucket and were added
                   once per copy as "wrong node".
    wrong node     no node holds the token for its own DID and no sender marked it
                   Transferred, but a node holds it for a DID it does not host -
                   counted once per token, however many nodes hold a copy.
    untraced       the rest: minted - held - not delivered - wrong node, with a
                   token held twice counted once.

    held twice     one token in a held status under two DIDs, on two nodes - its
                   RBT is in `held` twice. Free under both is a double spend; Free
                   under one and Locked or Pledged under the other is value that
                   one wallet no longer holds and still counts (2026-11-05: until
                   then only Free twice was counted, and RACE-02's +1.000 showed
                   up as untraced -1.000).
"""

from concurrent.futures import ThreadPoolExecutor

import db_client as db

MINTED = 400000.0
HELD = (db.FREE, db.LOCKED, db.COMMITTED, db.PLEDGED, db.QUORUM_PLEDGED, db.BURNT_FOR_FT)


def _read(item):
    """One REPEATABLE READ snapshot of a node: what it holds for its own DIDs,
    every token it still accounts for, its Transferred rows, and what it holds
    for DIDs it does not host."""
    host, dids = item
    try:
        held, own_ids, sent, foreign = db.query_snapshot(host, [
            ("SELECT COALESCE(SUM(token_value), 0) FROM tokens WHERE token_type = %s "
             "AND did = ANY(%s) AND token_status = ANY(%s)", (db.TYPE_RBT, dids, list(HELD))),
            # Any row but Transferred means the token is still accounted for here
            # (held, Burnt as a split parent, or stuck - the audit reports those).
            ("SELECT token_id, did, token_status, token_value FROM tokens WHERE token_type = %s "
             "AND did = ANY(%s) AND token_status <> %s", (db.TYPE_RBT, dids, db.TRANSFERRED)),
            ("SELECT token_id, did, token_value FROM tokens WHERE token_type = %s "
             "AND did = ANY(%s) AND token_status = %s", (db.TYPE_RBT, dids, db.TRANSFERRED)),
            ("SELECT token_id, did, token_status, token_value FROM tokens WHERE token_type = %s "
             "AND NOT (did = ANY(%s)) AND token_status = ANY(%s)", (db.TYPE_RBT, dids, list(HELD))),
        ])
    except Exception as e:
        return host, None, "{}: {}".format(type(e).__name__, e)
    return host, {"held": float(held[0][0]) if held else 0.0, "own": own_ids,
                  "sent": sent, "foreign": foreign}, ""


def _last_moves(host, token_ids):
    """{token_id: (position, transaction_id)} - the latest chain entry for each."""
    rows = db.query(host, "SELECT token_id, position, transaction_id FROM tokenchain "
                          "WHERE token_id = ANY(%s)", (list(token_ids),))
    out = {}
    for t, pos, tx in rows:
        if t not in out or int(pos) > out[t][0]:
            out[t] = (int(pos), tx)
    return out


def _tx_info(host, tx_ids):
    """{tx: (initiator, owner, epoch)} from the transactions' own info JSON;
    epoch is the Unix time the sender built it (core/transaction_builder.go:530)."""
    rows = db.query(host, "SELECT id, info->>'initiator', info->>'owner', info->>'epoch' "
                          "FROM transactions WHERE id = ANY(%s)", (list(tx_ids),))
    out = {}
    for tx, initiator, owner, epoch in rows:
        try:
            epoch = int(epoch)
        except (TypeError, ValueError):
            epoch = None
        out[tx] = (initiator, owner, epoch)
    return out


def account(members, minted=MINTED, workers=32):
    """members: {node host (ip:api_port): [the DIDs it hosts]}. Returns
        held, not_delivered, wrong_node, untraced, minted   (RBT)
        lost        [{tx, sender_host, initiator, owner, owner_host, epoch,
                      tokens: [(token_id, value)], value, copies_on: [hosts]}],
                    newest first
        lost_tokens {token_id} not delivered
        wrong       [(host, token_id, did, status, value)], one per token
        twice       {token_id: [(host, did, status)]} held under two DIDs or more
        twice_value RBT counted more than once in `held`
        copies      how many rows are stale copies of a token its owner holds
        unreadable  [(host, why)] - their tokens show up as not delivered"""
    items = list(members.items())
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(items) or 1))) as ex:
        readings = list(ex.map(_read, items))
    unreadable = [(h, why) for h, s, why in readings if s is None]
    snaps = dict((h, s) for h, s, _w in readings if s is not None)
    where = {}
    for h in snaps:
        for d in members[h]:
            where[d] = h

    own, holders, values = set(), {}, {}
    for h, s in snaps.items():
        for t, d, st, v in s["own"]:
            own.add(t)
            if int(st) in HELD:
                holders.setdefault(t, []).append((h, d, int(st)))
                values[t] = float(v)
    twice = dict((t, sorted(o)) for t, o in holders.items() if len(o) > 1)
    twice_value = round(sum(values[t] * (len(o) - 1) for t, o in twice.items()), 3)
    sent = {}                                   # token_id -> [(sender host, value)]
    for h, s in snaps.items():
        for t, _d, v in s["sent"]:
            if t not in own:
                sent.setdefault(t, []).append((h, float(v)))
    wrong, copies, copies_of_lost = {}, 0, {}
    for h, s in snaps.items():
        for t, d, st, v in s["foreign"]:
            if t in own:
                copies += 1
            elif t in sent:
                copies_of_lost.setdefault(t, set()).add(h)
            elif t not in wrong:
                wrong[t] = (h, t, d, int(st), float(v))

    # The last sender of a lost token is the one whose chain goes furthest.
    by_host = {}
    for t, rows in sent.items():
        for h, _v in rows:
            by_host.setdefault(h, set()).add(t)
    last = {}                                   # token_id -> (position, tx, sender host)
    for h, ids in by_host.items():
        try:
            moves = _last_moves(h, ids)
        except Exception:
            continue
        for t, (pos, tx) in moves.items():
            if t not in last or pos > last[t][0]:
                last[t] = (pos, tx, h)
    groups = {}                                 # (tx, sender host) -> [(token_id, value)]
    for t, rows in sent.items():
        _pos, tx, h = last.get(t, (0, "?", rows[0][0]))
        value = next((v for hh, v in rows if hh == h), rows[0][1])
        groups.setdefault((tx, h), []).append((t, value))
    info = {}
    by_sender = {}
    for tx, h in groups:
        if tx != "?":
            by_sender.setdefault(h, set()).add(tx)
    for h, txs in by_sender.items():
        try:
            for tx, row in _tx_info(h, txs).items():
                info[(tx, h)] = row
        except Exception:
            pass
    lost = []
    for (tx, h), toks in groups.items():
        initiator, owner, epoch = info.get((tx, h), (None, None, None))
        on = set()
        for t, _v in toks:
            on |= copies_of_lost.get(t, set())
        lost.append({"tx": tx, "sender_host": h, "initiator": initiator, "owner": owner,
                     "owner_host": where.get(owner), "epoch": epoch, "tokens": sorted(toks),
                     "value": round(sum(v for _t, v in toks), 3), "copies_on": sorted(on)})
    lost.sort(key=lambda g: (-(g["epoch"] or 0), g["tx"]))

    held = round(sum(s["held"] for s in snaps.values()), 3)
    not_delivered = round(sum(g["value"] for g in lost), 3)
    wrong_node = round(sum(w[4] for w in wrong.values()), 3)
    return {"held": held, "not_delivered": not_delivered, "wrong_node": wrong_node,
            "untraced": round(minted - held + twice_value - not_delivered - wrong_node, 3),
            "minted": minted, "lost": lost, "lost_tokens": set(sent), "wrong": sorted(wrong.values()),
            "twice": twice, "twice_value": twice_value,
            "copies": copies, "unreadable": unreadable, "where": where}
