"""issue_kit.py - what the issue checks share: wallets whose tokens have a long,
proper history, quorums, empty wallets filled with such tokens, and the SSH
reads (a node's log, its memory, its IPFS daemon) the checks need.

Every token a check moves has a proper history (user, 2026-11-13: "don't use any
corrupt tokens, so that we can ensure the issues are genuine ... the uncorrupted
tokens should have a bit lengthy and proper chain"):

  * AUDIT-START runs first and takes every unfit token out of use
    (full-test/token_health.py), as in every catalogue run;
  * a check picks a wallet by the tokens Rubix itself will move next - the same
    query as core/wallet/token_lock.go (Free, token_value = 1, ORDER BY
    token_id) - and only if each of them has at least MIN_CHAIN entries in its
    chain at the owner, the wallet holds no token it pledged as a quorum, and
    its token_denom counters match its Free tokens (Rubix plans which tokens to
    lock from those counters);
  * right before use, those tokens are checked across the fleet the way
    token_health does: one owner, an unbroken chain whose row points at its
    end, the last transfer made to this DID, every other node's copy of the
    chain a prefix of the owner's, and no fullnode disagreeing. A fullnode that
    is only BEHIND the owner is allowed and recorded - that is one of the
    issues checked here;
  * every token a check moves is written to the evidence with its chain length.

A check fills an EMPTY wallet with such tokens when it needs to know exactly
which token moves next (a payment of whole RBT takes the lowest token_id
first), and uses the wallet as found when the next tokens already qualify.
"""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "master"))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))        # controller/, for fleet.py

import case_evidence
import case_helpers as ch
import db_client as db
import fail_cases
import fleet as fleet_inventory
import fleet_health
import rubix_client as rc
import steps
import token_health

MIN_CHAIN = 5           # entries a moved token's chain has at its owner, at least
DRAIN_CAP = ch.DRAIN_CAP
HELD = token_health.HELD
QUARANTINED = token_health.QUARANTINED
NODE_LOG = fail_cases.NODE_LOG
UP_WAIT = 90            # seconds a restarted node gets to answer and receive again


def short(host):
    return steps._short(host)


def _parallel(fn, items, workers=32):
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as ex:
        return list(ex.map(fn, items))


# ---------------------------------------------------------------------------
# The fleet, read for wallets
# ---------------------------------------------------------------------------

def _scan_node(item):
    """Every DID of one node: the k tokens a payment of whole RBT takes next,
    with their chain lengths; Free RBT; RBT held by status; RBT it pledged as a
    quorum; token_denom drift. None when the node cannot be read."""
    host, dids, k = item
    rbt = db.TYPE_RBT
    try:
        nxt, by_status, ex_pledged, counters, free = db.query_snapshot(host, [
            # core/wallet/token_lock.go: Free, value 1, ORDER BY token_id - the
            # same collation, so the same order as Rubix's own pick
            ("SELECT x.did, x.token_id, x.n, (SELECT COUNT(*) FROM tokenchain c "
             "WHERE c.token_id = x.token_id) FROM (SELECT t.did, t.token_id, "
             "COUNT(*) OVER (PARTITION BY t.did) AS n, ROW_NUMBER() OVER "
             "(PARTITION BY t.did ORDER BY t.token_id) AS rn FROM tokens t "
             "WHERE t.did = ANY(%s) AND t.token_type = %s AND t.token_status = %s "
             "AND t.token_value = 1) x WHERE x.rn <= %s ORDER BY x.did, x.rn",
             (dids, rbt, db.FREE, k)),
            ("SELECT did, token_status, COALESCE(SUM(token_value), 0) FROM tokens "
             "WHERE did = ANY(%s) AND token_type = %s GROUP BY did, token_status", (dids, rbt)),
            ("SELECT t.did, COALESCE(SUM(t.token_value), 0) FROM tokens t "
             "JOIN LATERAL (SELECT c.role FROM tokenchain c WHERE c.token_id = t.token_id "
             "ORDER BY c.position DESC LIMIT 1) lc ON TRUE "
             "WHERE t.did = ANY(%s) AND t.token_type = %s AND t.token_status = %s "
             "AND lc.role = COALESCE((SELECT id FROM token_role WHERE name = 'unpledge'), 9) "
             "GROUP BY t.did", (dids, rbt, db.FREE)),
            ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
            ("SELECT did, token_value, COUNT(*) FROM tokens WHERE did = ANY(%s) AND "
             "token_type = %s AND token_status = %s GROUP BY did, token_value",
             (dids, rbt, db.FREE)),
        ])
    except Exception:
        return host, None
    out = dict((d, {"host": host, "did": d, "machine": ch.machine(host), "dids": list(dids),
                    "whole": 0, "next": [], "status": {}, "ex_pledged": 0.0, "drift": {}})
               for d in dids)
    for d, t, n, chain in nxt:
        if d in out:
            out[d]["whole"] = int(n)
            out[d]["next"].append((t, int(chain)))
    for d, st, v in by_status:
        if d in out:
            out[d]["status"][int(st)] = float(v)
    for d, v in ex_pledged:
        if d in out:
            out[d]["ex_pledged"] = float(v)
    counted, actual = {}, {}
    for d, value, n in counters:
        counted.setdefault(d, {})[round(float(value), 3)] = int(n)
    for d, value, n in free:
        actual.setdefault(d, {})[round(float(value), 3)] = int(n)
    for d, w in out.items():
        c, a = counted.get(d, {}), actual.get(d, {})
        w["drift"] = dict((v, (c.get(v, 0), a.get(v, 0))) for v in set(c) | set(a)
                          if c.get(v, 0) != a.get(v, 0))
        w["free"] = round(w["status"].get(db.FREE, 0.0), 3)
        w["pledged"] = round(w["status"].get(db.PLEDGED, 0.0) + w["status"].get(db.QUORUM_PLEDGED, 0.0), 3)
        w["held"] = round(sum(v for s, v in w["status"].items() if s in HELD), 3)
    return host, out


def scan(ctx, k=1, hosts=None):
    """{did: wallet} for every DID on `hosts` (default: the whole pool)."""
    by_host = nodes(ctx)
    items = [(h, by_host[h], k) for h in (hosts or sorted(by_host)) if h in by_host]
    wallets = {}
    for _h, got in _parallel(_scan_node, items):
        wallets.update(got or {})
    return wallets


def nodes(ctx):
    """{node host: [its DIDs]} for the pool."""
    out = {}
    for e in ctx.fleet:
        out.setdefault(e["host"], []).append(e["did"])
    return out


def clean(w):
    """No token it pledged as a quorum (still pledged, or back and Free - its
    payments would meet that issue instead of the one checked), nothing Locked,
    and its token_denom counters match its Free tokens."""
    return (w["pledged"] <= 0 and w["ex_pledged"] <= 0 and not w["drift"]
            and w["status"].get(db.LOCKED, 0.0) <= 0)


def long_enough(w, k, min_chain=MIN_CHAIN):
    return len(w["next"]) >= k and all(c >= min_chain for _t, c in w["next"][:k])


# ---------------------------------------------------------------------------
# Is a token fit: the fleet-wide check token_health makes, for a few tokens
# ---------------------------------------------------------------------------

def _owner_read(item):
    host, tokens = item
    try:
        rows, chain, last_to = db.query_snapshot(host, [
            ("SELECT token_id, did, token_status, COALESCE(transaction_id, ''), latest_position "
             "FROM tokens WHERE token_type = %s AND token_id = ANY(%s)", (db.TYPE_RBT, tokens)),
            ("SELECT token_id, position, transaction_id, COALESCE(previous_transaction_id, ''), "
             "role FROM tokenchain WHERE token_id = ANY(%s)", (tokens,)),
            ("SELECT DISTINCT ON (c.token_id) c.token_id, x.info->>'owner' FROM tokenchain c "
             "JOIN transactions x ON x.id = c.transaction_id WHERE c.token_id = ANY(%s) "
             "AND c.role = %s ORDER BY c.token_id, c.position DESC",
             (tokens, token_health.TRANSFER)),
        ])
    except Exception as e:
        return host, None, "{}: {}".format(type(e).__name__, e)
    chains = {}
    for t, p, x, prev, role in chain:
        chains.setdefault(t, []).append((int(p), x, prev, int(role)))
    for c in chains.values():
        c.sort()
    return host, {"rows": rows, "chains": chains, "last_to": dict(last_to)}, ""


def _other_read(item):
    host, kind, hosted, tokens = item
    try:
        if kind == "fullnode":
            tips, extra = db.query_snapshot(host, [
                ("SELECT DISTINCT ON (token_id) token_id, position, transaction_id FROM "
                 "fullnode_tokenchain WHERE token_id = ANY(%s) ORDER BY token_id, position DESC",
                 (tokens,)),
                ("SELECT token_id, did FROM fullnode_rbt WHERE token_id = ANY(%s)", (tokens,))])
        else:
            tips, extra = db.query_snapshot(host, [
                ("SELECT DISTINCT ON (token_id) token_id, position, transaction_id FROM "
                 "tokenchain WHERE token_id = ANY(%s) ORDER BY token_id, position DESC", (tokens,)),
                ("SELECT token_id, did, token_status, COALESCE(parent_token_id, '') FROM tokens "
                 "WHERE token_type = %s AND did = ANY(%s) AND token_status = ANY(%s) AND "
                 "(token_id = ANY(%s) OR parent_token_id = ANY(%s))",
                 (db.TYPE_RBT, hosted, list(HELD + QUARANTINED), tokens, tokens))])
    except Exception as e:
        return host, kind, None, "{}: {}".format(type(e).__name__, e)
    return host, kind, {"tips": dict((t, (int(p), x)) for t, p, x in tips), "extra": extra}, ""


def fixed_nodes():
    """{node host: [DID]} for the faucet pair."""
    out = {}
    for did, host, port in ((rc.FAUCET_DID, rc.FAUCET_HOST, rc.FAUCET_PORT),
                            (rc.FAUCET_QUORUM_DID, rc.FAUCET_QUORUM_HOST, rc.FAUCET_QUORUM_PORT)):
        if did and host:
            out.setdefault("{}:{}".format(host, port), []).append(did)
    return out


def health(ctx, items):
    """{token: {"problems": [...], "chain": n, "fullnodes": {fullnode: view}}}
    for each (host, did, token) - no problems = fit - and the nodes that could
    not be read. Problems, as token_health's codes: another DID holds it too
    (999); its parts are held (888); the owner's chain is broken (777); the
    token row is not at the chain's end (666); the last transfer went to another
    DID (555); another node's copy is ahead of or differs from the owner's
    (444); a fullnode holds another transaction, more entries, or another owner."""
    tokens = sorted(set(t for _h, _d, t in items))
    if not tokens:
        return {}, []
    by_owner = {}
    for h, _d, t in items:
        by_owner.setdefault(h, set()).add(t)
    pool = nodes(ctx)
    jobs = ([(h, "pool", dids, tokens) for h, dids in pool.items()] +
            [(h, "fixed", dids, tokens) for h, dids in fixed_nodes().items() if h not in pool] +
            [(h, "fullnode", [], tokens) for h in case_evidence.FULLNODE_HOSTS])
    unreadable = []
    owner = {}
    for h, got, why in _parallel(_owner_read, [(h, sorted(ts)) for h, ts in by_owner.items()]):
        if got is None:
            unreadable.append("{} ({})".format(short(h), why))
        owner[h] = got
    holders, parents, copies, fn = {}, set(), {}, {}
    wanted = set(tokens)
    for h, kind, got, why in _parallel(_other_read, jobs):
        if got is None:
            unreadable.append("{} ({})".format(short(h), why))
            continue
        if kind == "fullnode":
            fn[h] = (got["tips"], dict(got["extra"]))
            continue
        for t, tip in got["tips"].items():
            copies.setdefault(t, []).append((h, tip))
        for t, d, st, parent in got["extra"]:
            if t in wanted:
                holders.setdefault(t, []).append((h, d, int(st)))
            if parent in wanted and int(st) in HELD:
                parents.add(parent)
    out = {}
    for h, d, t in items:
        mine = owner.get(h)
        if mine is None:
            out[t] = {"problems": ["its owner's node could not be read"], "chain": 0, "fullnodes": {}}
            continue
        chain = mine["chains"].get(t, [])
        at = dict((p, x) for p, x, _pr, _r in chain)
        tip = chain[-1][0] if chain else -1
        problems = []
        others = [o for o in holders.get(t, []) if (o[0], o[1]) != (h, d)]
        if others:
            problems.append("999 also held by {} on {} ({})".format(
                others[0][1][:16], short(others[0][0]), db.STATUS_NAME.get(others[0][2], others[0][2])))
        if t in parents:
            problems.append("888 its parts are held, it should be Burnt")
        why = token_health._broken(chain)
        if why:
            problems.append("777 " + why)
        row = sorted((r for r in mine["rows"] if r[0] == t and r[1] == d), key=lambda r: int(r[2]) != db.FREE)
        if not row:
            problems.append("its owner's node has no row for it under this DID")
        else:
            _t, _d, st, tx, pos = row[0]
            if int(st) != db.FREE:
                problems.append("it is {}, not Free".format(db.STATUS_NAME.get(int(st), st)))
            if chain and (int(pos) != tip or tx != chain[-1][1]):
                problems.append("666 the row says position {}, the chain ends at {}".format(pos, tip))
        to = mine["last_to"].get(t)
        if to and to != d:
            problems.append("555 the last transfer went to {}".format(to[:16]))
        for other, (p, x) in copies.get(t, []):
            if other == h:
                continue
            if p > tip:
                problems.append("444 {} has it to position {}, the owner to {}".format(short(other), p, tip))
                break
            if at.get(p) != x:
                problems.append("444 {} has another transaction at position {}".format(short(other), p))
                break
        views = {}
        for f, (tips, owners) in fn.items():
            if t not in tips:
                views[short(f)] = "no record"
                continue
            p, x = tips[t]
            if p > tip:
                views[short(f)] = "ahead"
            elif at.get(p) != x:
                views[short(f)] = "another transaction"
            elif p < tip:
                views[short(f)] = "behind"
            elif owners.get(t) not in (None, d):
                views[short(f)] = "names another owner"
            else:
                views[short(f)] = "same"
        bad = sorted(set(f for f, v in views.items() if v in ("ahead", "another transaction",
                                                               "names another owner")))
        if bad:
            problems.append("fullnode {} disagrees ({})".format(", ".join(bad), views[bad[0]]))
        out[t] = {"problems": problems, "chain": len(chain), "fullnodes": views}
    return out, unreadable


# ---------------------------------------------------------------------------
# One check's participants
# ---------------------------------------------------------------------------

class Kit(object):
    """The participants of one check: who it uses, what it moved."""

    def __init__(self, ctx, case):
        self.ctx, self.case = ctx, case
        self.used = set()           # DIDs this check uses
        self.hosts = set()          # their nodes
        self.machines = set()       # machines it has put a participant on
        self.quorums = []
        self.moved = []             # every token it moved: token, chain, from, to, why
        self.rejected = []          # candidate tokens the fleet-wide check turned down
        self.unreadable = set()
        if getattr(ctx, "quorum_hosts", None) is None:
            ctx.quorum_hosts = []

    # -- naming participants -------------------------------------------------

    def join(self, role, e):
        """Before the first operation on it: the case's database check reads it
        from here on (case_evidence.join)."""
        self.used.add(e["did"])
        self.hosts.add(e["host"])
        self.machines.add(e["machine"])
        case_evidence.join(self.case, role, e)
        return e

    def entry(self, w):
        return {"host": w["host"], "did": w["did"], "machine": w["machine"],
                "dids": list(w["dids"])}

    # -- wallets whose next tokens have a long, proper history ----------------

    def holders(self, n, k=1, min_chain=MIN_CHAIN, avoid_machines=(), on=None, spread=True,
                per_node=1, exclude=()):
        """(up to n wallets, why fewer). Each holds at least k whole tokens and
        the k it pays next each have at least `min_chain` chain entries and pass
        the fleet-wide check. At most `per_node` per node; one per machine
        while machines are left (spread), machines this check already uses last.
        `exclude`: DIDs not to offer again."""
        avoid = set(avoid_machines)
        wallets = scan(self.ctx, k, [on] if on else None)
        cands = [w for w in wallets.values() if w["did"] not in self.used and clean(w)
                 and w["did"] not in exclude
                 and w["whole"] >= k and long_enough(w, k, min_chain)
                 and w["machine"] not in avoid and (on or w["host"] not in self.hosts)]
        cands.sort(key=lambda w: (w["machine"] in self.machines,
                                  -min(c for _t, c in w["next"][:k]), w["host"], w["did"]))
        picked, tried = [], set()

        def full(h, among):
            return sum(1 for x in among if x["host"] == h) >= per_node
        while len(picked) < n:
            taken_m = set(p["machine"] for p in picked)
            left = [w for w in cands if w["did"] not in tried and not full(w["host"], picked)]
            if not left:
                break
            fresh = [w for w in left if w["machine"] not in taken_m]
            batch, bm = [], set()
            for w in (fresh if spread and fresh else left):
                if len(batch) >= 2 * (n - len(picked)) + 2:
                    break
                if (spread and fresh and w["machine"] in bm) or full(w["host"], picked + batch):
                    continue
                batch.append(w)
                bm.add(w["machine"])
            tried |= set(w["did"] for w in batch)
            fit, unreadable = health(self.ctx, [(w["host"], w["did"], t) for w in batch
                                                for t, _c in w["next"][:k]])
            self.unreadable.update(unreadable)
            for w in batch:
                bad = [(t, fit.get(t, {}).get("problems")) for t, _c in w["next"][:k]
                       if fit.get(t, {}).get("problems")]
                if bad:
                    self.rejected.append({"did": w["did"], "host": w["host"], "token": bad[0][0],
                                          "problems": bad[0][1]})
                    continue
                if len(picked) >= n or full(w["host"], picked):
                    continue
                w["tokens"] = [(t, c) for t, c in w["next"][:k]]
                w["fullnodes"] = dict((t, fit.get(t, {}).get("fullnodes")) for t, _c in w["tokens"])
                picked.append(w)
        why = "" if len(picked) >= n else (
            "{} of the {} wallet(s) needed hold {} whole token(s) to pay next with at least {} "
            "chain entries each, a fit history, no pledge, nothing Locked and matching token_denom "
            "counters{}".format(len(picked), n, k, min_chain,
                                "; turned down by the fleet-wide check: {}".format(
                                    "; ".join("{} on {}: {}".format(r["token"], short(r["host"]),
                                                                    r["problems"][0])
                                              for r in self.rejected[:3]))
                                if self.rejected else ""))
        return [dict(self.entry(w), tokens=w["tokens"], fullnodes=w["fullnodes"]) for w in picked], why

    def note_moved(self, frm, to, tokens, why):
        for t, c in tokens:
            self.moved.append({"token": t, "chain": c, "from": frm["did"], "from_host": frm["host"],
                               "to": to["did"], "to_host": to["host"], "why": why})

    # -- quorums ---------------------------------------------------------------

    def quorum(self, need=10, avoid_machines=(), fresh=False, on=None, role="quorum", min_dids=1):
        """(a DID set up to sign, why not). Its token_denom counters match its
        Free tokens (Rubix plans its pledge from them), it holds `need` RBT Free
        (the faucet tops up the rest - lab plumbing), and no other participant
        of this check is on its node. `fresh` restarts its node first, so it is
        the only quorum set up there (a pledge's release looks at one quorum of
        the node only, core/fullnode.go:66-72). `min_dids`: its node hosts at
        least that many DIDs."""
        avoid = set(avoid_machines)
        wallets = scan(self.ctx, 1, [on] if on else None)
        cands = [w for w in wallets.values() if w["did"] not in self.used and not w["drift"]
                 and w["status"].get(db.LOCKED, 0.0) <= 0 and w["machine"] not in avoid
                 and (on or w["host"] not in self.hosts) and len(w["dids"]) >= min_dids]
        if not cands:
            return None, "no DID free to sign: every candidate's token_denom counters differ from " \
                         "its tokens, it holds Locked RBT, or its machine is taken"
        # the poorest that holds enough (rich wallets stay free to pay), else the richest
        cands.sort(key=lambda w: (w["machine"] in self.machines, w["free"] < need,
                                  w["free"] if w["free"] >= need else -w["free"]))
        w = cands[0]
        e = self.join(role, self.entry(w))
        if fresh:
            ok, why = self.restart(e["host"])
            if not ok:
                return None, why
        if w["free"] + 0.0015 < need:
            ok, msg = rc.fund_did(e["host"], e["did"], round(need - w["free"] + 1, 3))
            if not ok:
                return None, "funding the quorum on {} failed: {}".format(short(e["host"]), msg)
        ok, why = steps.quorum_ready(e, need)
        if not ok:
            return None, why
        self.quorums.append(e)
        self.ctx.quorum_hosts.append(e)
        return e, ""

    def restart(self, host):
        """(ok, why): restart one node (module restart()); this check's quorums
        on OTHER nodes of the machine are set up again if the whole machine had
        to be restarted. A quorum on `host` itself is left to the caller."""
        ok, why = restart(host)
        if ok and why:
            for q in self.quorums:
                if q["machine"] == ch.machine(host) and q["host"] != host:
                    rc.quorum_setup(q["host"], q["did"])
        return ok, why

    def as_quorum(self, e, need=0):
        """(ok, why): set up a wallet this check made as a quorum."""
        ok, why = steps.quorum_ready(e, need)
        if not ok:
            return False, why
        self.quorums.append(e)
        self.ctx.quorum_hosts.append(e)
        return True, ""

    def wire(self, entries, quorum):
        """Point each entry's node at `quorum` alone (the first quorum in a
        node's list signs, core/transaction.go)."""
        hosts = sorted(set(e["host"] for e in entries))
        for ok, msg in _parallel(lambda h: rc.quorum_reset(h, [quorum["did"]]), hosts, 16):
            if not ok:
                return False, msg
        return True, ""

    # -- empty wallets ---------------------------------------------------------

    def empty(self, host, quorum, role="wallet"):
        """(a DID on `host` holding no RBT in any held status, why not) - an idle
        one, or the node's spare holding least, emptied into another DID of the
        node through `quorum` (the value stays on the node)."""
        wallets = scan(self.ctx, 1, [host])
        quorum_dids = set(q["did"] for q in self.quorums) | {quorum["did"]}
        mine = [w for w in wallets.values() if w["did"] not in self.used and w["did"] not in quorum_dids]
        idle = [w for w in mine if w["held"] <= 0]
        if idle:
            return self.join(role, self.entry(idle[0])), ""
        # Spares holding whole tokens only first: paying away a balance of parts
        # makes the node look for an exact combination of them.
        spares = sorted((w for w in mine if clean(w) and 0 < w["free"] <= DRAIN_CAP
                         and w["held"] - w["free"] <= 0.0005),
                        key=lambda w: (abs(w["whole"] - w["free"]) > 0.0005, w["free"]))
        # The value goes to another DID of the node - never a quorum: Rubix
        # refuses a payment to the signing quorum (core/transaction.go:80).
        keep = [w for w in wallets.values() if w["did"] not in quorum_dids]
        if not spares or len(keep) < 2:
            return None, "no DID on {} can be emptied: none holds nothing, and none holds only " \
                         "Free RBT (at most {}) with no pledge history".format(short(host), DRAIN_CAP)
        if not self.quorum_can(quorum, max(w["free"] for w in spares[:2])):
            return None, "the quorum on {} cannot pledge enough to empty a wallet".format(
                short(quorum["host"]))
        ok, msg = self.wire([{"host": host}], quorum)
        if not ok:
            return None, msg
        last = ""
        for w in spares[:2]:
            to = next((k for k in keep if k["did"] != w["did"] and k not in spares[:2]),
                      next(k for k in keep if k["did"] != w["did"]))
            e = self.join(role, self.entry(w))
            if to["did"] not in self.used:
                self.join("node's own DID", self.entry(to))
            ok, msg, _r = rc.initiate_transaction(host, w["did"], to["did"], rbt=w["free"],
                                                  memo="{}: empty a wallet".format(self.case),
                                                  timeout=ch.TRANSFER_TIMEOUT)
            if not ok:
                last = "emptying {:.3f} RBT on {} was refused: {}".format(w["free"], short(host),
                                                                         ch.node_reason(msg))
                continue
            for _ in range(30):
                if (scan(self.ctx, 1, [host]).get(w["did"]) or {}).get("held", 1) <= 0:
                    return e, ""
                time.sleep(2)
            last = "a wallet on {} sent its {:.3f} RBT away and still holds RBT".format(
                short(host), w["free"])
        return None, last

    def quorum_can(self, quorum, amount):
        return afford(quorum, amount)

    def fill(self, target, k, quorum, min_chain=MIN_CHAIN, why="set-up", clear_of=()):
        """(target with "tokens" = what it now holds and pays next, why not):
        k whole tokens with long, proper histories into the EMPTY wallet
        `target`, from a wallet whose next k tokens qualify, through `quorum`.
        The tokens must have no row yet on the target's node (nor on the nodes
        in `clear_of`): a node that keeps a copy refuses the receipt
        (RECEIVE-OLD-COPY), which is not what the calling check is about."""
        hosts = [target["host"]] + list(clear_of)
        tried, clear, short_why = set(), [], ""
        for _ in range(4):          # a node that was a quorum keeps copies of many tokens
            cands, short_why = self.holders(8, k=k, min_chain=min_chain, exclude=tried,
                                            avoid_machines={target["machine"]})
            if not cands:
                break
            tried |= set(c["did"] for c in cands)
            clear = [c for c in cands if not copies([(t, h) for t, _c in c["tokens"] for h in hosts])]
            if clear:
                break
        if not clear:
            return None, short_why or "the next tokens of all {} candidate sources already have a row " \
                                      "on {}".format(len(tried), ", ".join(short(h) for h in hosts))
        src = self.join("source", clear[0])
        if not self.quorum_can(quorum, k):
            return None, "the quorum on {} cannot pledge {} RBT".format(short(quorum["host"]), k)
        ok, msg = self.wire([src], quorum)
        if not ok:
            return None, msg
        self.note_moved(src, target, src["tokens"], why)
        ok, msg, res = rc.initiate_transaction(src["host"], src["did"], target["did"], rbt=float(k),
                                               memo="{}: {}".format(self.case, why),
                                               timeout=ch.TRANSFER_TIMEOUT)
        if not ok:
            return None, "the {} payment of {} RBT from {} was refused: {}".format(
                why, k, short(src["host"]), ch.node_reason(msg))
        txid = res.get("transactionID") if isinstance(res, dict) else None
        if not delivered(target, txid, 60 + k):
            return None, "the {} payment {} never reached {}".format(why, (txid or "")[:16],
                                                                     short(target["host"]))
        sent = set(t for t, _c in src["tokens"])
        got = []
        for _ in range(15):
            w = scan(self.ctx, k, [target["host"]]).get(target["did"])
            got = [(t, c) for t, c in (w or {}).get("next", [])]
            if w and w["whole"] == k and w["held"] - k < 0.0005:
                if set(t for t, _c in got) != sent or any(c < min_chain for _t, c in got):
                    return None, "{} was paid other tokens than the source's next {}: {}".format(
                        short(target["host"]), k, ", ".join(t for t, _c in got))
                return dict(target, tokens=got), ""
            time.sleep(2)
        return None, "{} does not show exactly the {} token(s) it was sent".format(
            short(target["host"]), k)

    def one_token_wallet(self, host, quorum, k=1, role="wallet", why="set-up", clear_of=()):
        """(an empty wallet on `host` filled with k long-history tokens, why not)."""
        if not host:
            return None, "no node left for the {}".format(role)
        e, why_not = self.empty(host, quorum, role)
        if not e:
            return None, why_not
        return self.fill(e, k, quorum, why=why, clear_of=clear_of)

    def tokens_of(self, e):
        return [t for t, _c in e.get("tokens", [])]

    def other_hosts(self, avoid_machines=(), min_dids=1):
        """Pool nodes on machines this check does not use yet, then any node it
        does not use."""
        pool = nodes(self.ctx)
        avoid = set(avoid_machines) | self.machines
        first = [h for h in sorted(pool) if ch.machine(h) not in avoid and len(pool[h]) >= min_dids]
        then = [h for h in sorted(pool) if h not in self.hosts and h not in first
                and len(pool[h]) >= min_dids and ch.machine(h) not in set(avoid_machines)]
        return first + then

    def other_host(self, avoid_machines=(), min_dids=1):
        hosts = self.other_hosts(avoid_machines, min_dids)
        return hosts[0] if hosts else None

    def clear_host(self, tokens, avoid_machines=(), min_dids=1):
        """The first of other_hosts() whose node keeps no row for any of
        `tokens` - a payment of them to it is not refused for an old copy."""
        hosts = self.other_hosts(avoid_machines, min_dids)
        if not tokens:
            return hosts[0] if hosts else None
        for i in range(0, len(hosts), 8):
            batch = hosts[i:i + 8]
            held = set(h for _t, h in copies([(t, h) for t in tokens for h in batch]))
            for h in batch:
                if h not in held:
                    return h
        return None

    def ring_clean(self, payers, k):
        """The payers, minus those whose next k tokens already have a row on
        the node of one of the k payers after them in the ring (in k rounds a
        token can travel k hops): such a receipt is refused for the old copy."""
        for _ in range(5):
            n, pairs = len(payers), set()
            for i, e in enumerate(payers):
                for j in range(1, k + 1):
                    nxt = payers[(i + j) % n]
                    if nxt["host"] != e["host"]:
                        pairs |= set((t, nxt["host"]) for t, _c in e["tokens"][:k])
            bad = set(t for t, _h in copies(pairs))
            if not bad:
                return payers
            payers = [e for e in payers if not any(t in bad for t, _c in e["tokens"][:k])]
        return payers

    def wallet_on(self, host=None, role="payee", avoid_machines=(), tokens=()):
        """(a DID to receive on `host` - default: a node on a machine this check
        does not use yet, keeping no row for any of `tokens` - that is not a
        quorum of the check, why not)."""
        host = host or self.clear_host(tokens, avoid_machines)
        if not host:
            return None, "no node left for the {}".format(role)
        quorum_dids = set(q["did"] for q in self.quorums)
        for w in sorted(scan(self.ctx, 1, [host]).values(), key=lambda w: (bool(w["drift"]), w["did"])):
            if w["did"] not in self.used and w["did"] not in quorum_dids:
                return self.join(role, self.entry(w)), ""
        return None, "no DID free on {} for the {}".format(short(host), role)

    def evidence(self):
        case_evidence.attach(self.case, "tokens_moved", self.moved)
        if self.rejected:
            case_evidence.attach(self.case, "tokens_turned_down", self.rejected[:50])
        if self.unreadable:
            case_evidence.attach(self.case, "not_readable", sorted(self.unreadable))

    def chains_note(self):
        cs = [m["chain"] for m in self.moved]
        if not cs:
            return ""
        return "{} token(s) moved, {}-{} chain entries each (at least {} required)".format(
            len(cs), min(cs), max(cs), MIN_CHAIN)


def afford(quorum, amount):
    """The quorum holds `amount` + 1 RBT Free, topped up from the faucet when
    short. A quorum's pledges stay held until each receiver spends onward, so
    within one check the set-up's payments use it up; a judged payment refused
    'insufficient quorum liquidity' would be the lab's set-up, not the issue."""
    if not quorum:
        return True
    ok, detail, _n = rc.get_rbt_balance_detail(quorum["host"], quorum["did"])
    have = float(detail["balance"]) if ok and detail else 0.0
    if have + 0.0015 >= amount + 1:
        return True
    return rc.fund_did(quorum["host"], quorum["did"], round(amount + 5 - have, 3))[0]


# ---------------------------------------------------------------------------
# Reading the nodes
# ---------------------------------------------------------------------------

def delivered(to, txid, wait=60):
    """The receiver's node credited `txid` to its DID within `wait` seconds."""
    if not txid:
        return False
    deadline = time.time() + wait
    while True:
        try:
            if db.received(to["host"], [(txid, to["did"])]):
                return True
        except Exception:
            pass
        if time.time() > deadline:
            return False
        time.sleep(2)


def rows_on(host, token):
    """[(did, status name, position, transaction)] - every row the node keeps for the token."""
    try:
        rows = db.query(host, "SELECT did, token_status, latest_position, COALESCE(transaction_id, '') "
                              "FROM tokens WHERE token_id = %s", (token,))
    except Exception:
        return []
    return [(d, db.STATUS_NAME.get(int(s), s), p, x) for d, s, p, x in rows]


def tip_on(host, token):
    """(position, transaction) of the last chain entry the node holds, or None."""
    try:
        rows = db.chain_rows(host, token)
    except Exception:
        return None
    return (rows[-1][0], rows[-1][1]) if rows else None


def free_parts(e):
    """[(token, value)] the DID holds Free, smallest first."""
    try:
        return sorted(((t, v) for t, v, _s, _p in db.token_rows(e["host"], e["did"], status=db.FREE)),
                      key=lambda r: (r[1], r[0]))
    except Exception:
        return []


def pledged(e):
    try:
        return db.pledged_value(e["host"], e["did"])
    except Exception:
        return None


def txid_of(res):
    return res.get("transactionID") if isinstance(res, dict) else None


# ---------------------------------------------------------------------------
# A node over SSH
# ---------------------------------------------------------------------------

def slot(host):
    return fail_cases._slot_of(host)


def ssh(host_or_machine, remote, timeout=60):
    return fail_cases._ssh(ch.machine(host_or_machine), remote, timeout)


def answers(host):
    return fail_cases._peer_ok(host)


def listening(host):
    """The node's IPFS holds its p2p listener (no peer can reach it otherwise)."""
    deaf, _clocks = fleet_health.deaf_nodes([host])
    return host not in deaf


def wait_back(host, wait=UP_WAIT):
    """(answers and can receive, seconds)."""
    t0 = time.time()
    while time.time() - t0 < wait:
        if answers(host) and listening(host):
            return True, round(time.time() - t0, 1)
        time.sleep(3)
    return False, round(time.time() - t0, 1)


def machine_hosts(machine):
    return ["{}:{}".format(machine, fleet_inventory.API_BASE + index)
            for _s, index in fleet_health.machines_of(["{}:{}".format(machine, fleet_inventory.API_BASE)])[machine]]


def restart(host):
    """(ok, why): restart one node's unit and wait until it answers and receives.
    A node whose IPFS restarted on its own earlier can take every IPFS daemon of
    its machine with it when it stops (core/shutdown_manager.go:280-300): if a
    neighbour cannot receive afterwards, every node of the machine is restarted
    (why then says so - quorums set up there need setting up again)."""
    s = slot(host)
    if s is None:
        return False, "{} is not in the node layout".format(short(host))
    code, out = ssh(host, "sudo -n systemctl restart rubixgoplatform@{}".format(s), timeout=60)
    if code != 0:
        return False, "restarting {} failed: {}".format(short(host), out.strip()[:120])
    ok, secs = wait_back(host)
    if not ok:
        return False, "{} did not answer and receive within {}s of its restart".format(short(host), UP_WAIT)
    deaf, _c = fleet_health.deaf_nodes(machine_hosts(ch.machine(host)))
    if deaf:
        ok, why = restart_machine(ch.machine(host))
        return ok, "the restart left {} neighbour(s) unable to receive - every node of {} restarted{}".format(
            len(deaf), ch.machine(host), "" if ok else ": " + why)
    return True, ""


def stop(host):
    code, out = ssh(host, "sudo -n systemctl stop rubixgoplatform@{}".format(slot(host)), timeout=60)
    return code == 0, out.strip()[:120]


def start(host):
    code, out = ssh(host, "sudo -n systemctl start rubixgoplatform@{}".format(slot(host)), timeout=60)
    return code == 0, out.strip()[:120]


def log_lines(host, *patterns, last=10):
    """The node's log lines containing every one of `patterns` (fixed strings:
    transaction and token IDs, Rubix's own messages), the last `last` of them."""
    pats = [p for p in patterns if p]
    if not pats:
        return []
    cmd = "grep -n -F -- '{}' {}".format(pats[0].replace("'", ""), NODE_LOG.format(slot=slot(host)))
    for p in pats[1:]:
        cmd += " | grep -F -- '{}'".format(p.replace("'", ""))
    code, out = ssh(host, cmd + " | tail -n {}".format(int(last)), timeout=60)
    return [l[:400] for l in out.splitlines() if l.strip()] if code in (0, 1) else []


def rss_mb(host):
    """The node's own Rubix process, resident memory in MB, or None."""
    code, out = ssh(host, "ps -o rss= -p $(systemctl show -p MainPID --value rubixgoplatform@{})".format(
        slot(host)), timeout=30)
    try:
        return round(int(out.strip().split()[0]) / 1024.0)
    except (ValueError, IndexError):
        return None


def mem_available_mb(host):
    s = fleet_health.read_machine(ch.machine(host), [])
    try:
        return round(float(s["MemAvailable"]) / 1024.0)
    except (TypeError, KeyError, ValueError):
        return None


# The node's IPFS daemon: the one whose working directory is the node's folder
# (the node starts ./ipfs from it, core/ipfs.go, core/ipfs_recovery.go).
_IPFS = ('for p in $(pgrep -x ipfs); do d=$(readlink /proc/$p/cwd); '
         'echo "$p ${d##*/}"; done')


def ipfs_daemons(machine):
    """{node folder: pid} of every IPFS daemon on the machine, or None."""
    code, out = ssh(machine, _IPFS, timeout=30)
    if code != 0:
        return None
    got = {}
    for line in out.splitlines():
        bits = line.split()
        if len(bits) == 2 and bits[0].isdigit():
            got[bits[1]] = int(bits[0])
    return got


def kill_ipfs(host):
    """(pid killed, why not) - this node's IPFS daemon only."""
    daemons = ipfs_daemons(host) or {}
    pid = daemons.get("node{}".format(slot(host)))
    if not pid:
        return None, "no IPFS daemon found for {} (slot {})".format(short(host), slot(host))
    code, out = ssh(host, "kill {}".format(pid), timeout=30)
    return (pid, "") if code == 0 else (None, "kill failed: {}".format(out.strip()[:120]))


def restart_machine(machine):
    """restart-nodes.sh --host M --force: every node of the machine stopped,
    then started (a shutdown can take the machine's other IPFS daemons with it),
    then waited for until each receives. Returns (ok, why)."""
    script = os.path.join(HERE, "..", "..", "restart-nodes.sh")
    try:
        subprocess.run(["bash", script, "--host", machine, "--force", "--attempts", "20"],
                       capture_output=True, text=True, timeout=300)
    except Exception as e:
        return False, "{}: {}".format(type(e).__name__, e)
    hosts = machine_hosts(machine)
    deadline, still = time.time() + UP_WAIT, hosts
    while still and time.time() < deadline:
        time.sleep(5)
        still = list(fleet_health.deaf_nodes(hosts)[0])
    return (not still), ("" if not still else "{} node(s) on {} still cannot receive".format(
        len(still), machine))


def copies(pairs):
    """{(token, node)} for each (token, node) in `pairs` where the node keeps a
    row for the token - its own, or a copy kept from checking it as quorum."""
    by_host = {}
    for t, h in pairs:
        by_host.setdefault(h, set()).add(t)

    def one(item):
        h, ts = item
        try:
            return h, set(t for (t,) in db.query(h, "SELECT DISTINCT token_id FROM tokens WHERE "
                                                    "token_id = ANY(%s)", (sorted(ts),)))
        except Exception:
            return h, set()
    out = set()
    for h, got in _parallel(one, by_host.items()):
        out |= set((t, h) for t in got)
    return out


def held_twice(ctx, tokens):
    """{token: [(node, DID, status)]} for each of `tokens` held under more than
    one DID across the pool and the faucet pair (a DID counts on the node that
    hosts it; a node's copy of another DID's token does not)."""
    tokens = sorted(set(tokens))
    if not tokens:
        return {}
    pool = nodes(ctx)
    jobs = ([(h, "pool", dids, tokens) for h, dids in pool.items()] +
            [(h, "fixed", dids, tokens) for h, dids in fixed_nodes().items() if h not in pool])
    holders = {}
    for h, _kind, got, _why in _parallel(_other_read, jobs):
        for t, d, st, _parent in (got or {}).get("extra", []):
            if t in tokens:
                holders.setdefault(t, set()).add((h, d, db.STATUS_NAME.get(int(st), st)))
    return dict((t, sorted(hs)) for t, hs in holders.items()
                if len(set((h, d) for h, d, _s in hs)) > 1)


def set_up(run, p, fn):
    """S1: the check's participants, made by fn() -> (ok, why); NOT RUN with
    the reason when they cannot be made. p["notes"] says who they are."""
    def body(st):
        for n in p.get("notes", []):
            st.note(n)
    return run.step("S1", body, nodes=[], watch=False, deliver=False, require=[fn])


def who(e, name):
    return "{} = {} on {}".format(name, e["did"][:16], short(e["host"]))


# ---------------------------------------------------------------------------
# The verdict: FAIL means the issue was reproduced
# ---------------------------------------------------------------------------

def verdict(run, kit, reproduced, not_reproduced):
    """run.verdict() worded for an issue check: FAIL = the issue is real on this
    build, PASS = Rubix behaved correctly, SKIP = the set-up could not be made."""
    kit.evidence()
    passed, actual, note = run.verdict(passed=not_reproduced)
    chains = kit.chains_note()
    if passed is False:
        actual = "REPRODUCED - {}: {}".format(reproduced, actual)
    elif passed is True:
        actual = "NOT REPRODUCED - {}".format(actual)
    if chains:
        note = (note + " | " if note else "") + chains
    return passed, actual, note
