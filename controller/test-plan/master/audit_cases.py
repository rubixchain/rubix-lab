"""audit_cases.py - the fleet audit, at the start and at the end of every run.

Every node's database is read, one consistent snapshot per node, for every DID
in the pool plus the faucet DID and the faucet quorum. The audit ACCOUNTS for
the RBT the faucet minted (--fleet-total, 400,000); it does not fail on it
(user, 2026-11-05: "even though the total is not 400000, it's fine, just mark
how much locked or missed or double spend... we need the count and
accountability for that"). The cases that move value judge it themselves
(case_evidence: conserved, persisted).

    account    held (Free, Locked, Committed, Pledged, QuorumPledged,
               BurntForFT, by status), not delivered (sent, and held by no node
               for its own DID - each transfer with its time, sender and
               receiver), held on the wrong node, and untraced
               (full-test/rbt_ledger.py). Transferred rows are history and Burnt
               split parents are replaced by children worth the same, so
               neither is counted as held.
    ownership  tokens held under two different DIDs (rbt_ledger "held twice"):
               counted, with the RBT counted twice. Free under both is a double
               spend; Free under one and Locked or Pledged under the other still
               counts the RBT twice (until 2026-11-05 only Free twice was looked
               for, and RACE-02's +1.000 showed up as untraced -1.000).
    tokens     AUDIT-START takes every token unfit for a test out of use before
               the cases run (user, 2026-11-06: "Don't use corrupted tokens for
               testing"): full-test/token_health.py checks each Free token the
               pool holds - one owner, a whole chain at the owner, every other
               copy of the chain agreeing - and marks the ones that fail with
               their reason's code (999-444), and Rubix's stuck states with 333.
               Each marking lowers the DID's token_denom by that token; nothing
               else is rewritten. Out-of-use RBT is its own line in the account.
               Fullnode disagreements and token_denom drift are listed only.
               (Until 2026-11-06 the stuck and chainless tokens were set Locked.)
    locked     RBT already Locked is left parked and counted.
    counters   token_denom drift and negative counters are a KNOWN PRODUCT BUG
               that conserves value: reported, never rewritten.

AUDIT-START takes unfit tokens out of use and re-audits. AUDIT-END changes nothing and says what
moved during the run: the total, the transfers not delivered since AUDIT-START,
tokens newly held twice. Neither fails; a node that cannot be read makes the audit
NOT RUN (its figures would be incomplete).
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import case_evidence
import rbt_ledger
import steps
import token_health

SKIP = "SKIP"
FLEET_TOTAL_DEFAULT = rbt_ledger.MINTED
TOL = 0.01

HELD = rbt_ledger.HELD
STUCK = (2, 3, 13, 14, 15, 99)
NAME = {db.FREE: "free", db.LOCKED: "locked", db.COMMITTED: "committed",
        db.PLEDGED: "pledged", db.QUORUM_PLEDGED: "quorum-pledged",
        db.BURNT_FOR_FT: "burnt-for-FT"}
SHOWN = 5       # undelivered transfers named in a note; the evidence keeps all

_START = {}     # AUDIT-START's reading, for AUDIT-END to compare against


def _members(ctx):
    """{node host: [dids]} - the pool from ctx.fleet plus the faucet pair."""
    by_host = {}
    for e in ctx.fleet:
        by_host.setdefault(e["host"], []).append(e["did"])
    for did, host, port in ((rc.FAUCET_DID, rc.FAUCET_HOST, rc.FAUCET_PORT),
                            (rc.FAUCET_QUORUM_DID, rc.FAUCET_QUORUM_HOST, rc.FAUCET_QUORUM_PORT)):
        if did and host:
            key = "{}:{}".format(host, port)
            if did not in by_host.get(key, []):
                by_host.setdefault(key, []).append(did)
    return by_host


def _read_node(host, dids):
    """One REPEATABLE READ snapshot of everything the audit needs from a node."""
    rbt, free = db.TYPE_RBT, db.FREE
    return db.query_snapshot(host, [
        ("SELECT did, token_status, COALESCE(SUM(token_value), 0) FROM tokens "
         "WHERE token_type = %s AND did = ANY(%s) GROUP BY did, token_status", (rbt, dids)),
        ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
        ("SELECT did, token_value, COUNT(*) FROM tokens WHERE token_type = %s "
         "AND token_status = %s AND did = ANY(%s) GROUP BY did, token_value", (rbt, free, dids)),
        ("SELECT did, denom, count FROM token_denom WHERE count < 0 AND did = ANY(%s)", (dids,)),
        ("SELECT t.token_id FROM tokens t WHERE t.token_type = %s AND t.token_status = %s "
         "AND t.did = ANY(%s) AND NOT EXISTS "
         "(SELECT 1 FROM tokenchain c WHERE c.token_id = t.token_id)", (rbt, free, dids)),
    ])


def _audit(ctx, label):
    """The fleet's reading, or None when there is nothing to audit."""
    members = _members(ctx)
    if not members:
        return None

    def one(item):
        host, dids = item
        try:
            return host, _read_node(host, dids), None
        except Exception as e:
            return host, None, "{}: {}".format(type(e).__name__, e)

    with ThreadPoolExecutor(max_workers=min(32, len(members))) as ex:
        readings = list(ex.map(one, members.items()))

    totals = dict((s, 0.0) for s in HELD)
    stuck, locked, drift, negative, orphans, out_of_use = {}, {}, [], [], [], {}
    unreadable = []
    dids_read = 0
    for host, snap, err in readings:
        if snap is None:
            unreadable.append("{} ({})".format(host, err))
            continue
        status_rows, denom_rows, free_rows, neg_rows, orphan_rows = snap
        dids_read += len(members[host])
        for did, status, value in status_rows:
            status, value = int(status), float(value)
            if status in totals:
                totals[status] += value
            if status in db.QUARANTINE:
                out_of_use[status] = out_of_use.get(status, 0.0) + value
            if status == db.LOCKED and value > 0.0005:
                locked[did] = locked.get(did, 0.0) + value
            if status in STUCK and value > 0.0005:
                key = db.STATUS_NAME.get(status, str(status))
                stuck[key] = stuck.get(key, 0.0) + value
        counted, actual = {}, {}
        for did, denom, count in denom_rows:
            counted.setdefault(did, {})[round(float(denom), 3)] = int(count)
        for did, value, count in free_rows:
            actual.setdefault(did, {})[round(float(value), 3)] = int(count)
        for did in members[host]:
            c, a = counted.get(did, {}), actual.get(did, {})
            bad = dict((d, (c.get(d, 0), a.get(d, 0))) for d in set(c) | set(a)
                       if c.get(d, 0) != a.get(d, 0))
            if bad:
                drift.append((host, did, bad))
        negative += [(host, did, float(d), int(n)) for did, d, n in neg_rows]
        orphans += [(host, t) for (t,) in orphan_rows]

    held = round(sum(totals.values()), 3)
    expected = float(getattr(ctx.args, "fleet_total", 0) or FLEET_TOTAL_DEFAULT)
    reading = {"label": label, "held": held, "expected": expected, "dids": dids_read,
               "nodes": len(members),
               "totals": dict((NAME[s], round(v, 3)) for s, v in totals.items()),
               "out_of_use": dict((c, round(v, 3)) for c, v in sorted(out_of_use.items())),
               "out_of_use_total": round(sum(out_of_use.values()), 3),
               "locked": locked, "stuck": stuck, "doubled": {}, "doubled_value": 0.0,
               "drift": dict(((host, did), bad) for host, did, bad in drift),
               "negative": len(negative), "orphans": len(orphans),
               "unreadable": unreadable, "ledger": None}
    if not unreadable:
        try:
            reading["ledger"] = rbt_ledger.account(members, expected)
        except Exception as e:
            reading["ledger_error"] = "{}: {}".format(type(e).__name__, e)
    if reading["ledger"]:
        # a token held under k DIDs is counted k times in `held`
        reading["doubled"] = reading["ledger"]["twice"]
        reading["doubled_value"] = reading["ledger"]["twice_value"]
    return reading


def _drift_finding(drift, known=False):
    """drift: {(host, did): {denom: (counted, actual)}} -> one sentence.

    token_denom counter drift is a KNOWN PRODUCT BUG: a pledge decrements the
    counter in a second DB transaction that can fail, and concurrent splits
    update the rows in Go map order and deadlock (core/pledge_v2.go:287,
    persist_genesis_tx.go:209). It conserves value - the Free tokens are real -
    so `known=True` tags it rather than failing the audit over the same bug
    every run."""
    (host, _did), bad = sorted(drift.items())[0]
    prefix = "KNOWN PRODUCT BUG - " if known else ""
    return "{}{} DID(s) have a denomination counter that does not match their Free tokens " \
           "(e.g. {} on {}, as denomination: (counter, real Free tokens))".format(
               prefix, len(drift), dict(sorted(bad.items())), host)


def _describe(reading):
    t = reading["totals"]
    return ("{:.3f} RBT held across {} DIDs on {} nodes - free {:.3f}, pledged {:.3f} + quorum-"
            "pledged {:.3f}, committed {:.3f}, burnt for FT {:.3f}, locked {:.3f}".format(
                reading["held"], reading["dids"], reading["nodes"], t["free"], t["pledged"],
                t["quorum-pledged"], t["committed"], t["burnt-for-FT"], t["locked"]))


def _untraced(r):
    led = r["ledger"] or {}
    return round(r["expected"] - r["held"] - r["out_of_use_total"] + r["doubled_value"]
                 - led.get("not_delivered", 0.0) - led.get("wrong_node", 0.0), 3)


def _placed(r):
    """RBT in the fleet's wallets, held or out of use, each token once."""
    return round(r["held"] + r["out_of_use_total"] - r["doubled_value"], 3)


def _headline(r):
    """Where the minted RBT is, in one sentence - the audit's verdict text."""
    led = r["ledger"]
    if led is None:
        return "{:.3f} minted; {} (where the rest is could not be read: {})".format(
            r["expected"], _describe(r), r.get("ledger_error", "?"))
    both_free = _free_twice(r["doubled"])
    return ("{:.3f} RBT minted: {:.3f} held, {:.3f} out of use, {:.3f} not delivered ({} "
            "transfer(s)), {:.3f} on the wrong node, {:.3f} counted twice, untraced {:+.3f}; locked "
            "{:.3f}; held by two DIDs {}{}".format(
                r["expected"], r["held"], r["out_of_use_total"], led["not_delivered"],
                len(led["lost"]), led["wrong_node"], r["doubled_value"], _untraced(r),
                r["totals"]["locked"], len(r["doubled"]),
                " ({} Free under both - a double spend)".format(len(both_free)) if both_free else ""))


def _free_twice(doubled):
    """The tokens Free under two DIDs or more - a double spend."""
    return sorted(t for t, o in doubled.items() if sum(1 for _h, _d, st in o if st == db.FREE) > 1)


def _short(host):
    return "." + str(host or "?").split(".")[-1]


def _lost_line(g):
    return "{} tx {} {:.3f} RBT from {} to {}{}".format(
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(g["epoch"])) if g["epoch"] else "?",
        g["tx"][:16], g["value"], _short(g["sender_host"]),
        _short(g["owner_host"]) if g["owner_host"] else (g["owner"] or "?")[:16],
        " (copies on {})".format(", ".join(_short(h) for h in g["copies_on"][:4]))
        if g["copies_on"] else "")


def _lost_notes(st, lost, title):
    if lost:
        st.note("{}: {:.3f} RBT in {} transfer(s), newest first: {}{}".format(
            title, sum(g["value"] for g in lost), len(lost),
            "; ".join(_lost_line(g) for g in lost[:SHOWN]),
            "; ... (all in the evidence)" if len(lost) > SHOWN else ""))


def _doubled_text(doubled):
    sample = sorted(doubled.items())[:3]
    free = _free_twice(doubled)
    return "{} token(s) held under two different DIDs, their RBT counted twice{}: {}".format(
        len(doubled), " ({} Free under both - a double spend)".format(len(free)) if free else "",
        "; ".join("{} held by {}".format(t, " and ".join(
            "{} ({}) on {}".format(d[:12], db.STATUS_NAME.get(st, st), h) for h, d, st in o))
            for t, o in sample))


def _attach(case, r, new_lost=None):
    led = r["ledger"] or {}
    case_evidence.attach(case, "accounting", {
        "minted": r["expected"], "held": r["held"], "totals": r["totals"],
        "out_of_use": dict((str(c), v) for c, v in r["out_of_use"].items()),
        "not_delivered": led.get("not_delivered"), "wrong_node": led.get("wrong_node"),
        "untraced": _untraced(r) if led else None, "copies": led.get("copies"),
        "held_twice": dict((t, [{"host": h, "did": d, "status": db.STATUS_NAME.get(st, st)}
                                for h, d, st in o]) for t, o in r["doubled"].items()),
        "counted_twice_value": r["doubled_value"],
        "double_spends": _free_twice(r["doubled"]),
        "locked_by_did": dict((d, round(v, 3)) for d, v in r["locked"].items()),
        "not_delivered_transfers": [dict(g, tokens=g["tokens"][:50]) for g in led.get("lost", [])],
        "wrong_node_tokens": [list(w) for w in led.get("wrong", [])][:500],
        "lost_during_run": new_lost})


def _soft_notes(reading):
    """The known things the audit reports alongside the account: RBT parked in
    Locked, the token_denom drift and negative counters, and any stuck/orphan
    tokens still present."""
    notes = []
    if reading["out_of_use"]:
        notes.append("out of use, by reason: {}".format(", ".join(
            "{} ({}) {:.3f} RBT".format(c, db.QUARANTINE.get(c, "?"), v)
            for c, v in reading["out_of_use"].items())))
    if reading["locked"]:
        notes.append("{:.3f} RBT parked in Locked on {} DID(s) - left as is".format(
            sum(reading["locked"].values()), len(reading["locked"])))
    if reading["stuck"]:
        notes.append("RBT in a stuck state: {}".format(
            ", ".join("{} {:.3f}".format(k, v) for k, v in sorted(reading["stuck"].items()))))
    if reading["orphans"]:
        notes.append("{} Free token(s) with no chain history".format(reading["orphans"]))
    if reading["negative"]:
        notes.append("{} negative token_denom counter(s) (known counter bug)".format(
            reading["negative"]))
    if reading["drift"]:
        notes.append(_drift_finding(reading["drift"], known=True))
    return notes


def _pool_and_fixed(ctx):
    """({node host: [dids]} for the pool, the same for the faucet pair)."""
    members = _members(ctx)
    pool = {}
    for e in ctx.fleet:
        pool.setdefault(e["host"], []).append(e["did"])
    return pool, dict((h, d) for h, d in members.items() if h not in pool)


PURPOSE = {
    "AUDIT-START": "Where every RBT the faucet minted is before the run - held, locked, not "
                   "delivered, on the wrong node, double spent - with every token unfit for a test "
                   "taken out of use first, so every later step's findings are its own.",
    "AUDIT-END": "Where every RBT is after the run, and what the run lost, locked or doubled.",
}

STEPS = {
    "AUDIT-START": [
        ("S1", "Read every node",
         "read every pool node's database and the faucet pair's, one consistent snapshot each",
         "every node readable (one that is not is the lab's problem: the audit is NOT RUN)"),
        ("S2", "Take unfit tokens out of use",
         "check every Free token a pool DID holds - one owner, a whole chain at the owner, every "
         "other node's copy of the chain agreeing (full-test/token_health.py) - and mark each one "
         "that fails with its reason's code (999-444), Rubix's stuck states with 333, lowering the "
         "DID's token_denom for each; list the fullnodes' disagreements and token_denom drift; "
         "read again - the audit's only write",
         "done; how many of each reason is reported"),
        ("S3", "Account for the RBT minted",
         "add up every DID's Free, Locked, Committed, Pledged, QuorumPledged and BurntForFT RBT, "
         "and what is out of use; find the rest - sent and delivered to no node, held on a node "
         "that does not host the DID, untraced",
         "reported, not failed: every RBT of the 400,000 placed, each undelivered transfer named"),
        ("S4", "One owner per token",
         "look for a token held under two DIDs, in any held status (Free under both = a double "
         "spend)",
         "reported, not failed: how many and how much RBT is counted twice"),
        ("S5", "Known issues",
         "list RBT parked in Locked, token_denom drift and negative counters",
         "reported, not failed (known product bugs)"),
    ],
    "AUDIT-END": [
        ("S1", "Read every node", "as AUDIT-START S1", "every node readable"),
        ("S2", "What the run lost or created",
         "account as AUDIT-START S3 and compare with it",
         "reported, not failed: the total's move during the run, each transfer not delivered "
         "since AUDIT-START, any that arrived late"),
        ("S3", "One owner per token", "as AUDIT-START S4",
         "reported, not failed: how many, and which are new since AUDIT-START"),
        ("S4", "Known issues", "as AUDIT-START S5, plus what the run committed and burnt for FT",
         "reported, not failed"),
    ],
}


def _read_step(run, ctx, label, got):
    def read(st):
        reading = _audit(ctx, label)
        if reading is None:
            st.skip("nothing to audit")
            return
        got["r"] = reading
        if reading["unreadable"]:
            st.skip("{} node database(s) could not be read, so the audit is incomplete: {}".format(
                len(reading["unreadable"]), "; ".join(reading["unreadable"][:3])))
    return run.step("S1", read, watch=False)


def _account(st, r):
    st.note(_headline(r))
    if r["ledger"]:
        _lost_notes(st, r["ledger"]["lost"], "not delivered")
        if r["ledger"]["wrong"]:
            st.note("{} token(s) held on a node that does not host their DID, e.g. {} on {}".format(
                len(r["ledger"]["wrong"]), r["ledger"]["wrong"][0][1], r["ledger"]["wrong"][0][0]))


def audit_start(ctx, ci):
    run = steps.CaseRun(ctx, "AUDIT-START", STEPS["AUDIT-START"])
    got = {}
    if _read_step(run, ctx, "start", got).outcome != steps.PASS:
        return run.verdict()

    def take_out(st):
        # A pre-condition for the cases (user, 2026-11-06): no case may pick a
        # token whose history is already broken. AUDIT-START runs first and
        # alone, so no token moves while it is read and marked.
        pool, fixed = _pool_and_fixed(ctx)
        found = token_health.survey(pool, fixed, case_evidence.FULLNODE_HOSTS)
        done = token_health.apply(found, by="AUDIT-START")
        st.data["out_of_use"] = dict((str(c), v) for c, v in done["marked"].items())
        for line in token_health.lines(found, done, denom_rows=5):
            st.note(line)
        case_evidence.attach("AUDIT-START", "token_health", token_health.evidence(found, done, cap=500))
        if done["marked"]:
            got["r"] = _audit(ctx, "start")         # read again, with them out of use
    run.step("S2", take_out, watch=False)
    r = got["r"]
    _START.clear()
    _START.update(r)
    run.step("S3", lambda st: _account(st, r), watch=False)
    run.step("S4", lambda st: st.note(_doubled_text(r["doubled"]) if r["doubled"] else "none"),
             watch=False)

    def known(st):
        for n in _soft_notes(r):
            st.note(n)
    run.step("S5", known, watch=False)
    _attach("AUDIT-START", r)
    return run.verdict(passed=_headline(r))


def audit_end(ctx, ci):
    # AUDIT-END reports the run's true end state and does NOT change the fleet -
    # cleaning happens at the next run's AUDIT-START.
    run = steps.CaseRun(ctx, "AUDIT-END", STEPS["AUDIT-END"])
    got = {}
    if _read_step(run, ctx, "end", got).outcome != steps.PASS:
        return run.verdict()
    r = got["r"]
    start = _START if _START and not _START.get("unreadable") else None
    new_lost = []

    def totals(st):
        _account(st, r)
        if start is None:
            st.note("no AUDIT-START reading to compare with")
            return
        moved = round(_placed(r) - _placed(start), 3)
        st.data["moved"] = moved
        st.note("in the wallets (held or out of use) {:+.3f} RBT during this run ({:.3f} -> "
                "{:.3f})".format(moved, _placed(start), _placed(r)))
        if r["ledger"] and start["ledger"]:
            before = start["ledger"]["lost_tokens"]
            new_lost[:] = [g for g in r["ledger"]["lost"]
                           if any(t not in before for t, _v in g["tokens"])]
            _lost_notes(st, new_lost, "not delivered during this run")
            arrived = before - r["ledger"]["lost_tokens"]
            if arrived:
                st.note("{} token(s) not delivered at AUDIT-START are held by their owner's node "
                        "now".format(len(arrived)))
    run.step("S2", totals, watch=False)

    def owners(st):
        if not r["doubled"]:
            st.note("none")
            return
        st.note(_doubled_text(r["doubled"]))
        if start is not None:
            new = sorted(set(r["doubled"]) - set(start["doubled"]))
            st.note("{} of them new since AUDIT-START{}".format(
                len(new), ": " + ", ".join(new[:5]) if new else ""))
    run.step("S3", owners, watch=False)

    def known(st):
        for n in _soft_notes(r):
            st.note(n)
        if start is not None:
            st.note("committed {:+.3f}, burnt for FT {:+.3f}, locked {:+.3f} since the start".format(
                r["totals"]["committed"] - start["totals"]["committed"],
                r["totals"]["burnt-for-FT"] - start["totals"]["burnt-for-FT"],
                r["totals"]["locked"] - start["totals"]["locked"]))
    run.step("S4", known, watch=False)
    _attach("AUDIT-END", r, [dict(g, tokens=g["tokens"][:50]) for g in new_lost] if start else None)
    return run.verdict(passed=_headline(r))


CASES = {"AUDIT-START": audit_start, "AUDIT-END": audit_end}
ORDER = ["AUDIT-START", "AUDIT-END"]
NEEDS = {
    "AUDIT-START": {"senders": 0, "receivers": 0, "quorums": 0, "first": True},
    "AUDIT-END": {"senders": 0, "receivers": 0, "quorums": 0, "last": True, "exclusive": True},
}
NO_DELAY_RERUN = set(ORDER)
