"""token_health.py - is every RBT token in circulation fit for a test to use?

A PRE-CONDITION for the test runs, not a test (user, 2026-11-06): "only tokens
with a proper history should be used. The other tokens with a broken history or
multiple ownership or any other token issue is making the case fail and testing
becoming pointless." A case that picks such a token fails on the token, not on
what it was written to test - so these tokens are taken out of use first.

Checked: every Free RBT token a pool DID holds - the tokens that left the faucet
and that a case can pick. Locked and Pledged tokens are never picked; they are
checked once they are Free again. Each token is read on its owner's node and
compared with every other node's copy of its chain. Its code is the first of
these that applies:

  999  more than one owner: another DID holds it too (any held status)
  888  the parent of a split, still held although its parts are held - its value
       exists twice
  777  the owner's chain is broken: none, a gap in its positions, position 0 not
       a mint, or an entry whose previous transaction is not the entry before it
  666  the token row does not point at its chain's last entry (transaction or
       position): the next transfer's continuity check refuses it
       (core/wallet/post_consensus_persistence.go, validateTransferChainContinuity)
  555  the chain's last transfer went to another DID
  444  another node's copy of the chain disagrees with the owner's: another
       transaction at a position, or entries the owner does not have - that node
       refuses the token, "chain mismatch after sync" (RUBIX-ISSUES R-04, R-33).
       An older copy, a prefix of the owner's, is normal: that node synced at an
       earlier hop.
  333  in one of Rubix's own stuck states (Generated, Fetched, Orphaned,
       ChainSyncIssue, BeingDoubleSpent, Seed) - AUDIT-START's quarantine

Listed, not acted on (user, 2026-11-06: "get the list then will see"):
  fullnodes    each fullnode's chain for the token against the owner's: same,
               behind, ahead, another transaction at a position, no record - and
               a fullnode that names another owner (fullnode_rbt.did)
  token_denom  every DID, the faucet pair's included, whose counter differs from
               its Free tokens: node and DID, never corrected

Taking a token out of use (apply): its Free row gets the code, and its DID's
token_denom count for that denomination goes down by one, in ONE statement:
Rubix plans which tokens to lock from that counter (R-30), so a counter still
counting a marked token would make the DID's sends fail. Rubix uses statuses
0-15 and 99 (constants/constants.go:72-102) and selects Free tokens only, so a
marked token is never picked. Every marking is appended to
token-quarantine.jsonl beside this file (the tokens table has no column for the
reason), and restore() puts tokens back from it. A code that would mark more
than MAX_SHARE of the tokens checked is not applied: that is a check misreading
Rubix, not damage.
"""

import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor

import db_client as db

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(HERE, "token-quarantine.jsonl")

HELD = (db.FREE, db.LOCKED, db.COMMITTED, db.PLEDGED, db.QUORUM_PLEDGED, db.BURNT_FOR_FT)
STUCK = (2, 3, 13, 14, 15, 99)          # Generated, Fetched, Orphaned, ChainSyncIssue,
                                        # BeingDoubleSpent, Seed (constants/constants.go)
CODES = (999, 888, 777, 666, 555, 444)  # a token's code: the first of these that applies
STUCK_CODE = 333
QUARANTINED = tuple(db.QUARANTINE)
MINT, TRANSFER = 1, 2                   # token role IDs (types/models/lookup.go:20-31)
ROLE = {1: "mint", 2: "transfer", 3: "execute", 4: "deploy", 5: "burn", 6: "commit",
        7: "uncommit", 8: "pledge", 9: "unpledge"}
MAX_SHARE = 0.2
EXAMPLES = 3


def _parallel(fn, items, workers=32):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


def _short(host):
    return "." + str(host).split(".")[-1]


def _owner_side(item):
    """One snapshot of a pool node: its DIDs' held, stuck and quarantined RBT
    rows, the chains of their Free tokens, each Free token's last transfer's
    receiver, and their token_denom counters."""
    host, dids = item
    rbt, free = db.TYPE_RBT, db.FREE
    try:
        rows, chain, last_to, denom = db.query_snapshot(host, [
            ("SELECT token_id, did, token_status, token_value, transaction_id, latest_position, "
             "COALESCE(parent_token_id, '') FROM tokens WHERE token_type = %s AND did = ANY(%s) "
             "AND token_status = ANY(%s)", (rbt, dids, list(HELD + STUCK + QUARANTINED))),
            ("SELECT c.token_id, c.position, c.transaction_id, COALESCE(c.previous_transaction_id, ''), "
             "c.role FROM tokenchain c JOIN tokens t ON t.token_id = c.token_id "
             "WHERE t.token_type = %s AND t.did = ANY(%s) AND t.token_status = %s", (rbt, dids, free)),
            ("SELECT DISTINCT ON (c.token_id) c.token_id, x.info->>'owner' FROM tokenchain c "
             "JOIN tokens t ON t.token_id = c.token_id JOIN transactions x ON x.id = c.transaction_id "
             "WHERE t.token_type = %s AND t.did = ANY(%s) AND t.token_status = %s AND c.role = %s "
             "ORDER BY c.token_id, c.position DESC", (rbt, dids, free, TRANSFER)),
            ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
        ])
    except Exception as e:
        return host, None, "{}: {}".format(type(e).__name__, e)
    chains = {}
    for t, pos, tx, prev, role in chain:
        chains.setdefault(t, []).append((int(pos), tx, prev, int(role)))
    for c in chains.values():
        c.sort()
    return host, {"rows": rows, "chains": chains, "last_to": dict(last_to), "denom": denom}, ""


def _denom_only(item):
    """A faucet node's token_denom counters and Free tokens by value."""
    host, dids = item
    try:
        free, denom = db.query_snapshot(host, [
            ("SELECT did, token_value, COUNT(*) FROM tokens WHERE token_type = %s AND token_status = %s "
             "AND did = ANY(%s) GROUP BY did, token_value", (db.TYPE_RBT, db.FREE, dids)),
            ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
        ])
    except Exception as e:
        return host, None, "{}: {}".format(type(e).__name__, e)
    return host, {"free": free, "denom": denom}, ""


def _other_side(item):
    """Every candidate token's last chain entry on another node, (position,
    transaction); a fullnode's owner for it; a faucet node's own holdings of it."""
    host, kind, tokens, dids = item
    table = "fullnode_tokenchain" if kind == "fullnode" else "tokenchain"
    statements = [("SELECT DISTINCT ON (token_id) token_id, position, transaction_id FROM " + table +
                   " WHERE token_id = ANY(%s) ORDER BY token_id, position DESC", (tokens,))]
    if kind == "fullnode":
        statements.append(("SELECT token_id, did FROM fullnode_rbt WHERE token_id = ANY(%s)", (tokens,)))
    elif kind == "fixed":
        statements.append(("SELECT token_id, did, token_status FROM tokens WHERE token_type = %s "
                           "AND token_id = ANY(%s) AND did = ANY(%s) AND token_status = ANY(%s)",
                           (db.TYPE_RBT, tokens, dids, list(HELD + QUARANTINED))))
    try:
        got = db.query_snapshot(host, statements)
    except Exception as e:
        return host, kind, None, "{}: {}".format(type(e).__name__, e)
    tips = dict((t, (int(p), x)) for t, p, x in got[0])
    return host, kind, {"tips": tips, "extra": got[1] if len(got) > 1 else []}, ""


def _broken(chain):
    """Why the owner's chain is broken, or ""."""
    if not chain:
        return "no chain at all"
    if [p for p, _x, _pr, _r in chain] != list(range(len(chain))):
        return "positions {} - not 0 to {} without gaps".format(
            [p for p, _x, _pr, _r in chain][:8], len(chain) - 1)
    if chain[0][3] != MINT:
        return "position 0 is a {} entry, not a mint".format(ROLE.get(chain[0][3], chain[0][3]))
    for i in range(1, len(chain)):
        if chain[i][2] != chain[i - 1][1]:
            return "entry {} does not follow entry {} (previous {}, entry {} is {})".format(
                i, i - 1, chain[i][2][:16], i - 1, chain[i - 1][1][:16])
    return ""


def _drift(counters, free):
    """{denomination: [counter, Free tokens]} where they differ."""
    return dict((k, [counters.get(k, 0), free.get(k, 0)]) for k in sorted(set(counters) | set(free))
                if counters.get(k, 0) != free.get(k, 0))


def survey(pool, fixed=None, fullnodes=(), workers=32):
    """Read-only. pool / fixed: {node host: [the DIDs it hosts]} - the pool, and
    the faucet pair (holders and counters, not checked as owners); fullnodes:
    [host]. Returns the findings (see the module's docstring)."""
    fixed = fixed or {}
    readings = _parallel(_owner_side, pool.items(), workers)
    unreadable = [(h, why) for h, s, why in readings if s is None]
    snaps = dict((h, s) for h, s, _w in readings if s is not None)

    holders, parents, cands, stuck = {}, set(), [], []
    already, not_free, free_by = {}, {}, {}
    for h, s in snaps.items():
        for t, d, st, v, tx, pos, parent in s["rows"]:
            st, v = int(st), float(v)
            if st in STUCK:
                stuck.append({"host": h, "did": d, "token": t, "value": v, "status": st})
                continue
            holders.setdefault(t, []).append((h, d, st))
            if st in QUARANTINED:
                a = already.setdefault(st, [0, 0.0])
                a[0], a[1] = a[0] + 1, a[1] + v
                continue
            if parent:
                parents.add(parent)
            if st == db.FREE:
                cands.append({"host": h, "did": d, "token": t, "value": v, "tx": tx, "pos": int(pos)})
                f = free_by.setdefault((h, d), {})
                f[round(v, 3)] = f.get(round(v, 3), 0) + 1
            else:
                name = db.STATUS_NAME.get(st, st)
                not_free[name] = not_free.get(name, 0) + 1

    tokens = sorted(set(c["token"] for c in cands))
    items = []
    if tokens:
        items = ([(h, "pool", tokens, None) for h in snaps] +
                 [(h, "fixed", tokens, dids) for h, dids in fixed.items()] +
                 [(h, "fullnode", tokens, None) for h in fullnodes])
    copies, fn_tips, fn_owner = {}, {}, {}      # copies: {token: [(node, (position, tx))]}
    for h, kind, got, why in _parallel(_other_side, items, workers):
        if got is None:
            unreadable.append((h, why))
        elif kind == "fullnode":
            fn_tips[h], fn_owner[h] = got["tips"], dict(got["extra"])
        else:
            for t, tip in got["tips"].items():
                copies.setdefault(t, []).append((h, tip))
            for t, d, st in got["extra"]:
                holders.setdefault(t, []).append((h, d, int(st)))

    found = dict((code, []) for code in CODES)
    fullnode = dict((h, {"same": 0, "names another owner": [], "behind": [], "ahead": [],
                         "another transaction": [], "no record": []}) for h in fn_tips)
    fit, fit_value = 0, 0.0
    for c in cands:
        h, d, t = c["host"], c["did"], c["token"]
        chain = snaps[h]["chains"].get(t, [])
        at = dict((p, x) for p, x, _pr, _r in chain)
        tip = chain[-1][0] if chain else -1
        reasons = []
        others = [o for o in holders.get(t, []) if (o[0], o[1]) != (h, d)]
        if others:
            reasons.append((999, "also held by {} on {} ({})".format(
                others[0][1][:16], others[0][0], db.STATUS_NAME.get(others[0][2], others[0][2]))))
        if t in parents:
            reasons.append((888, "its parts are held, the parent should be Burnt"))
        why = _broken(chain)
        if why:
            reasons.append((777, why))
        if chain and (c["pos"] != tip or c["tx"] != chain[-1][1]):
            reasons.append((666, "the row says position {} / {}, the chain ends at {} / {}".format(
                c["pos"], (c["tx"] or "")[:16], tip, chain[-1][1][:16])))
        to = snaps[h]["last_to"].get(t)
        if to and to != d:
            reasons.append((555, "the last transfer went to {}".format(to[:16])))
        for other, (p, x) in copies.get(t, []):
            if other == h:
                continue
            if p > tip:
                reasons.append((444, "{} has it to position {}, the owner only to {}".format(other, p, tip)))
                break
            if at.get(p) != x:
                reasons.append((444, "{} has another transaction at position {}".format(other, p)))
                break
        for fh, tips in fn_tips.items():
            view = fullnode[fh]
            if t not in tips:
                view["no record"].append(t)
                continue
            p, x = tips[t]
            if p > tip:
                view["ahead"].append(t)
            elif at.get(p) != x:
                view["another transaction"].append(t)
            elif p < tip:
                view["behind"].append(t)
            elif fn_owner[fh].get(t) not in (None, d):
                view["names another owner"].append(t)
            else:
                view["same"] += 1
        if reasons:
            codes = set(code for code, _w in reasons)
            code = next(cd for cd in CODES if cd in codes)
            found[code].append({"host": h, "did": d, "token": t, "value": c["value"],
                                "reasons": ["{} {}".format(cd, w) for cd, w in reasons]})
        else:
            fit, fit_value = fit + 1, fit_value + c["value"]

    denom = []
    for h, s in snaps.items():
        counters = {}
        for d, value, n in s["denom"]:
            counters.setdefault(d, {})[round(float(value), 3)] = int(n)
        for d in pool[h]:
            off = _drift(counters.get(d, {}), free_by.get((h, d), {}))
            if off:
                denom.append({"host": h, "did": d, "denoms": off})
    for h, s, why in _parallel(_denom_only, fixed.items(), workers):
        if s is None:
            unreadable.append((h, why))
            continue
        counters, free = {}, {}
        for d, value, n in s["denom"]:
            counters.setdefault(d, {})[round(float(value), 3)] = int(n)
        for d, value, n in s["free"]:
            free.setdefault(d, {})[round(float(value), 3)] = int(n)
        for d in fixed[h]:
            off = _drift(counters.get(d, {}), free.get(d, {}))
            if off:
                denom.append({"host": h, "did": d, "denoms": off})

    return {"at": datetime.datetime.now().isoformat(timespec="seconds"),
            "checked": len(cands), "checked_value": round(sum(c["value"] for c in cands), 3),
            "fit": fit, "fit_value": round(fit_value, 3), "found": found, "stuck": stuck,
            "already": already, "not_free": not_free, "fullnodes": fullnode,
            "token_denom": denom, "unreadable": unreadable}


# One statement per token: the status moves only from the status the survey
# read, and the counter moves only when that happened (m returns the row).
MARK = ("WITH m AS (UPDATE tokens SET token_status = %(new)s, updated_at = NOW() "
        "WHERE token_id = %(token)s AND did = %(did)s AND token_type = %(rbt)s "
        "AND token_status = %(old)s RETURNING token_value), "
        "d AS (UPDATE token_denom SET count = count + %(delta)s, updated_at = NOW() "
        "WHERE %(delta)s <> 0 AND did = %(did)s AND denom = (SELECT token_value FROM m) "
        "RETURNING count) "
        "SELECT (SELECT COUNT(*) FROM m), (SELECT count FROM d)")


def _write(jobs, by, action, log_path):
    """jobs: {host: [(params, entry, code)]}. Runs each node's batch in one DB
    transaction, logs what changed. Returns ({code: [n, RBT]}, failed)."""
    def run(item):
        host, batch = item
        try:
            return host, batch, db.writable_many(host, MARK, [p for p, _e, _c in batch],
                                                 i_understand_this_writes=True), ""
        except Exception as e:
            return host, batch, None, "{}: {}".format(type(e).__name__, e)
    done, failed, lines = {}, [], []
    now = datetime.datetime.now().isoformat(timespec="seconds")
    for host, batch, rows, why in _parallel(run, jobs.items(), 16):
        if rows is None:
            failed.append((host, len(batch), why))
            continue
        for (p, e, code), r in zip(batch, rows):
            changed, after = (r[0] if r else (0, None))
            if not changed:
                continue                        # moved since the survey read it
            n = done.setdefault(code, [0, 0.0])
            n[0], n[1] = n[0] + 1, round(n[1] + e["value"], 3)
            lines.append(json.dumps({"at": now, "action": action, "by": by, "host": host,
                                     "did": e["did"], "token": e["token"], "value": e["value"],
                                     "from": p["old"], "to": p["new"],
                                     "reasons": e.get("reasons", []), "denom_count_after": after}))
    if lines:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return done, failed


def apply(result, codes=CODES + (STUCK_CODE,), by="token_check", limit=MAX_SHARE, log_path=LOG_PATH):
    """Take the survey's tokens out of use - only the codes asked for. Returns
    {"marked": {code: [n, RBT]}, "not_applied": {code: why}, "failed": [...]}."""
    checked = max(1, result["checked"])
    jobs, not_applied = {}, {}
    for code in CODES:
        hits = result["found"].get(code, [])
        if code not in codes or not hits:
            continue
        if limit and len(hits) > limit * checked:
            not_applied[code] = ("{} of the {} tokens checked - more than {:.0%}, which points at the "
                                 "check rather than the tokens; not marked".format(
                                     len(hits), result["checked"], limit))
            continue
        for e in hits:
            jobs.setdefault(e["host"], []).append(
                ({"new": code, "old": db.FREE, "delta": -1, "token": e["token"], "did": e["did"],
                  "rbt": db.TYPE_RBT}, e, code))
    if STUCK_CODE in codes:
        for e in result["stuck"]:
            jobs.setdefault(e["host"], []).append(
                ({"new": STUCK_CODE, "old": e["status"], "delta": 0, "token": e["token"],
                  "did": e["did"], "rbt": db.TYPE_RBT},
                 dict(e, reasons=["333 {}".format(db.STATUS_NAME.get(e["status"], e["status"]))]),
                 STUCK_CODE))
    marked, failed = _write(jobs, by, "mark", log_path)
    return {"marked": marked, "not_applied": not_applied, "failed": failed}


def restore(codes, by="token_check", log_path=LOG_PATH):
    """Put back every token the log says was marked with one of `codes` and is
    still marked: its status before, and its counter +1 if it was Free.
    Returns ({code: [n, RBT]}, failed)."""
    last = {}
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    last[(e["host"], e["did"], e["token"])] = e
    jobs = {}
    for e in last.values():
        if e["action"] == "mark" and e["to"] in codes:
            jobs.setdefault(e["host"], []).append(
                ({"new": e["from"], "old": e["to"], "delta": 1 if e["from"] == db.FREE else 0,
                  "token": e["token"], "did": e["did"], "rbt": db.TYPE_RBT}, e, e["to"]))
    return _write(jobs, by, "restore", log_path)


def lines(result, applied=None, denom_rows=None):
    """The findings in plain lines, for the terminal and the audit's notes.
    denom_rows: how many token_denom rows to list (None = all)."""
    applied = applied or {"marked": {}, "not_applied": {}, "failed": []}
    out = ["checked {} Free token(s), {:.3f} RBT, held by the pool's DIDs: {} fit for testing "
           "({:.3f} RBT)".format(result["checked"], result["checked_value"], result["fit"],
                                 result["fit_value"])]
    for code in CODES + (STUCK_CODE,):
        hits = result["stuck"] if code == STUCK_CODE else result["found"].get(code, [])
        if not hits:
            continue
        done = applied["marked"].get(code)
        e = hits[0]
        out.append("{} {}: {} token(s), {:.3f} RBT{}{}; e.g. {} on {} - {}".format(
            code, db.QUARANTINE[code], len(hits), sum(x["value"] for x in hits),
            ", {} marked".format(done[0]) if done else "",
            " - NOT marked: " + applied["not_applied"][code] if code in applied["not_applied"] else "",
            e["token"], e["host"], "; ".join(e.get("reasons", [])) or
            db.STATUS_NAME.get(e.get("status"), "")))
    if result["already"]:
        out.append("already out of use from earlier checks: {}".format(", ".join(
            "{} x{} ({:.3f} RBT)".format(code, n, v) for code, (n, v) in sorted(result["already"].items()))))
    if result["not_free"]:
        out.append("not checked now, not Free: {}".format(", ".join(
            "{} {}".format(n, k) for k, n in sorted(result["not_free"].items()))))
    for fh, view in sorted(result["fullnodes"].items()):
        out.append("fullnode {} against the owner's chain: same {}, behind {}, ahead {}, another "
                   "transaction {}, no record {}, names another owner {}".format(
                       fh, view["same"], len(view["behind"]), len(view["ahead"]),
                       len(view["another transaction"]), len(view["no record"]),
                       len(view["names another owner"])))
    if result["token_denom"]:
        rows = result["token_denom"] if denom_rows is None else result["token_denom"][:denom_rows]
        out.append("{} DID(s) whose token_denom differs from their Free tokens (not corrected; "
                   "denomination: counter vs Free tokens):".format(len(result["token_denom"])))
        for r in rows:
            out.append("  {} {}: {}".format(r["host"], r["did"], ", ".join(
                "{}: {} vs {}".format(k, c, f) for k, (c, f) in r["denoms"].items())))
        if len(rows) < len(result["token_denom"]):
            out.append("  ... (all in the evidence)")
    for h, why in result["unreadable"]:
        out.append("could not read {}: {} - its tokens are not in these figures".format(h, why))
    for h, n, why in applied["failed"]:
        out.append("could not mark {} token(s) on {}: {}".format(n, h, why))
    return out


def evidence(result, applied=None, cap=None):
    """JSON-able findings; lists cut to `cap` entries each when given."""
    def cut(xs):
        return xs if cap is None else xs[:cap]
    return {"at": result["at"], "checked": result["checked"], "checked_value": result["checked_value"],
            "fit": result["fit"], "fit_value": result["fit_value"],
            "found": dict((str(code), cut(hits)) for code, hits in result["found"].items() if hits),
            "stuck": cut(result["stuck"]),
            "already": dict((str(code), v) for code, v in result["already"].items()),
            "not_free": result["not_free"],
            "fullnodes": dict((h, dict((k, v if isinstance(v, int) else cut(v)) for k, v in view.items()))
                              for h, view in result["fullnodes"].items()),
            "token_denom": result["token_denom"],
            "unreadable": [list(u) for u in result["unreadable"]],
            "applied": applied and {"marked": dict((str(c), v) for c, v in applied["marked"].items()),
                                    "not_applied": dict((str(c), v)
                                                        for c, v in applied["not_applied"].items()),
                                    "failed": [list(f) for f in applied["failed"]]}}
