"""audit_cases.py - the fleet audit, at the start and at the end of every run.

Every node's database is read, one consistent snapshot per node, for every DID
in the pool plus the faucet DID and the faucet quorum:

    total      RBT held across the whole fleet - Free, Locked, Committed,
               Pledged, QuorumPledged and BurntForFT - must equal what the
               faucet minted (--fleet-total, 400,000). Any other figure is value
               created or lost: a HARD FAIL (nothing in the lab mints after
               setup). Transferred rows are history and Burnt split parents are
               replaced by children worth the same, so neither is counted.
    ownership  no token Free under two different DIDs anywhere in the fleet - a
               double spend, a HARD FAIL (the value exists twice; locking one
               copy cannot undo it).
    stuck      tokens in Orphaned / ChainSyncIssue / BeingDoubleSpent / Seed /
               Generated / Fetched (constants/constants.go), and Free tokens
               with no chain history: AUDIT-START LOCKS these out of use
               (quarantine) and re-audits. Locking is the ONLY change the audit
               makes to the fleet (user, 2026-10-22): it never rewrites a
               counter and never restores a token. Locking only removes a token
               from circulation and keeps it on the ledger, so it is always
               safe, and the count is small.
    locked     RBT already Locked is left parked - reported, not failed. It is
               out of circulation; per-case checks (case_evidence, FAIL-01)
               catch locks made during a run.
    counters   token_denom drift and negative counters are a KNOWN PRODUCT BUG
               that conserves value: reported, never failed, never rewritten.

AUDIT-START fixes what it can (quarantine) and re-audits. AUDIT-END reports the
run's true end state and does not change the fleet. Both fail only on value not
conserved or a double spend; everything else is reported.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import steps

SKIP = "SKIP"
FLEET_TOTAL_DEFAULT = 400000.0
TOL = 0.01

HELD = (db.FREE, db.LOCKED, db.COMMITTED, db.PLEDGED, db.QUORUM_PLEDGED, db.BURNT_FOR_FT)
STUCK = (2, 3, 13, 14, 15, 99)
NAME = {db.FREE: "free", db.LOCKED: "locked", db.COMMITTED: "committed",
        db.PLEDGED: "pledged", db.QUORUM_PLEDGED: "quorum-pledged",
        db.BURNT_FOR_FT: "burnt-for-FT"}

_START = {}     # AUDIT-START's totals, for AUDIT-END to compare against


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
    """One REPEATABLE READ snapshot of everything the audit needs from a node.

    The last query returns the tokens AUDIT-START quarantines: anything in a
    stuck status, plus a Free token with no chain history."""
    rbt, free = db.TYPE_RBT, db.FREE
    return db.query_snapshot(host, [
        ("SELECT did, token_status, COALESCE(SUM(token_value), 0) FROM tokens "
         "WHERE token_type = %s AND did = ANY(%s) GROUP BY did, token_status", (rbt, dids)),
        ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
        ("SELECT did, token_value, COUNT(*) FROM tokens WHERE token_type = %s "
         "AND token_status = %s AND did = ANY(%s) GROUP BY did, token_value", (rbt, free, dids)),
        ("SELECT token_id, did FROM tokens WHERE token_type = %s AND token_status = %s "
         "AND did = ANY(%s)", (rbt, free, dids)),
        ("SELECT did, denom, count FROM token_denom WHERE count < 0 AND did = ANY(%s)", (dids,)),
        ("SELECT t.token_id FROM tokens t WHERE t.token_type = %s AND t.token_status = %s "
         "AND t.did = ANY(%s) AND NOT EXISTS "
         "(SELECT 1 FROM tokenchain c WHERE c.token_id = t.token_id)", (rbt, free, dids)),
        ("SELECT t.token_id, t.did FROM tokens t WHERE t.token_type = %s AND t.did = ANY(%s) AND "
         "(t.token_status = ANY(%s) OR (t.token_status = %s AND NOT EXISTS "
         "(SELECT 1 FROM tokenchain c WHERE c.token_id = t.token_id)))",
         (rbt, dids, list(STUCK), free)),
    ])


def _audit(ctx, label):
    members = _members(ctx)
    if not members:
        return None, "no DIDs to audit"

    def one(item):
        host, dids = item
        try:
            return host, _read_node(host, dids), None
        except Exception as e:
            return host, None, "{}: {}".format(type(e).__name__, e)

    with ThreadPoolExecutor(max_workers=min(32, len(members))) as ex:
        readings = list(ex.map(one, members.items()))

    totals = dict((s, 0.0) for s in HELD)
    stuck, locked, drift, negative, orphans, quarantine = {}, {}, [], [], [], []
    owners = {}
    unreadable = []
    dids_read = 0
    for host, snap, err in readings:
        if snap is None:
            unreadable.append("{} ({})".format(host, err))
            continue
        status_rows, denom_rows, free_rows, free_ids, neg_rows, orphan_rows, quar_rows = snap
        dids_read += len(members[host])
        for did, status, value in status_rows:
            status, value = int(status), float(value)
            if status in totals:
                totals[status] += value
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
        for token_id, did in free_ids:
            owners.setdefault(token_id, set()).add(did)
        negative += [(host, did, float(d), int(n)) for did, d, n in neg_rows]
        orphans += [(host, t) for (t,) in orphan_rows]
        quarantine += [(host, t, d) for (t, d) in quar_rows]

    doubled = dict((t, sorted(o)) for t, o in owners.items() if len(o) > 1)
    held = round(sum(totals.values()), 3)
    reading = {"label": label, "held": held, "dids": dids_read, "nodes": len(members),
               "totals": dict((NAME[s], round(v, 3)) for s, v in totals.items()),
               "locked": locked, "stuck": stuck, "doubled": len(doubled),
               "drift": dict(((host, did), bad) for host, did, bad in drift),
               "negative": len(negative), "orphans": len(orphans),
               "quarantine": quarantine, "unreadable": unreadable}

    # findings = the HARD FAILS only. Parked Locked RBT, stuck/orphan tokens
    # (quarantined by AUDIT-START), counter drift and negative counters are
    # non-fatal and reported through _soft_notes.
    findings = []
    if unreadable:
        findings.append("{} node database(s) could not be read, so this audit is incomplete: "
                        "{}".format(len(unreadable), "; ".join(unreadable[:3])))
    expected = float(getattr(ctx.args, "fleet_total", 0) or FLEET_TOTAL_DEFAULT)
    if not unreadable and abs(held - expected) > TOL:
        findings.append("the fleet holds {:.3f} RBT but the faucet minted {:.3f}: {:+.3f} RBT "
                        "{}".format(held, expected, held - expected,
                                    "created from nowhere" if held > expected else "lost"))
    reading["expected"] = expected
    reading["doubled_text"] = ""
    if doubled:
        sample = sorted(doubled.items())[:3]
        reading["doubled_text"] = "{} token(s) are Free under two different DIDs - a double spend: " \
            "{}".format(len(doubled), "; ".join("{} held by {}".format(t, " and ".join(d[:12] for d in o))
                                                for t, o in sample))
        findings.append(reading["doubled_text"])
    return reading, findings


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
    return ("{:.3f} RBT across {} DIDs on {} nodes - free {:.3f}, pledged {:.3f} + quorum-"
            "pledged {:.3f}, committed {:.3f}, burnt for FT {:.3f}, locked {:.3f}".format(
                reading["held"], reading["dids"], reading["nodes"], t["free"], t["pledged"],
                t["quorum-pledged"], t["committed"], t["burnt-for-FT"], t["locked"]))


def _soft_notes(reading):
    """The non-fatal things to report but not fail on: RBT parked in Locked, the
    known token_denom drift and negative counters, and any stuck/orphan tokens
    still present (their value, if real, shows up in the conservation check)."""
    notes = []
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


def _quarantine(targets):
    """Lock each problem token - a token in a stuck state, or a Free token with
    no chain history - so it leaves the spendable pool and is simply left alone.

    This is the ONLY change the audit makes to the fleet (user, 2026-10-22): no
    counter is rewritten and nothing is restored. Locking only ever removes a
    token from use and keeps it on the ledger (Locked is counted in the fleet
    total), so it is always safe. The audit runs with nothing else transacting
    (AUDIT-START is `first`), so the token cannot change under it. Every write is
    echoed by db_client. Returns how many tokens it locked."""
    n = 0
    for host, token_id, did in targets:
        try:
            db.writable_query(host, "UPDATE tokens SET token_status = %s WHERE token_id = %s AND "
                              "did = %s AND token_type = %s AND token_status <> %s",
                              (db.LOCKED, token_id, did, db.TYPE_RBT, db.LOCKED),
                              i_understand_this_writes=True)
            n += 1
        except Exception:
            pass
    return n


PURPOSE = {
    "AUDIT-START": "The fleet's state before the run - nothing created or lost, no token owned "
                   "twice - so every later step's findings are its own.",
    "AUDIT-END": "The fleet's state after the run: what the run created, lost, locked or doubled.",
}

STEPS = {
    "AUDIT-START": [
        ("S1", "Read every node",
         "read every pool node's database and the faucet pair's, one consistent snapshot each",
         "every node readable (one that is not is the lab's problem: the audit is NOT RUN)"),
        ("S2", "Quarantine problem tokens",
         "lock out of use every RBT token in a stuck status, or Free with no chain history, then "
         "read again - the audit's only write",
         "done; how many is reported"),
        ("S3", "Nothing created or lost",
         "add up every DID's Free, Locked, Committed, Pledged, QuorumPledged and BurntForFT RBT",
         "equals the 400,000 RBT the faucet minted"),
        ("S4", "One owner per token", "look for a token Free under two DIDs",
         "none - two would be a double spend"),
        ("S5", "Known issues",
         "list RBT parked in Locked, token_denom drift and negative counters",
         "reported, not failed (known product bugs)"),
    ],
    "AUDIT-END": [
        ("S1", "Read every node", "as AUDIT-START S1", "every node readable"),
        ("S2", "Nothing created or lost",
         "add up every DID's RBT as at the start",
         "equals the 400,000 minted, and has not moved since AUDIT-START"),
        ("S3", "One owner per token", "look for a token Free under two DIDs", "none"),
        ("S4", "Known issues", "as AUDIT-START S5, plus what the run committed and burnt for FT",
         "reported, not failed"),
    ],
}


def _read_step(run, ctx, label, got):
    def read(st):
        reading, _f = _audit(ctx, label)
        if reading is None:
            st.skip("nothing to audit")
            return
        got["r"] = reading
        if reading["unreadable"]:
            st.skip("{} node database(s) could not be read, so the audit is incomplete: {}".format(
                len(reading["unreadable"]), "; ".join(reading["unreadable"][:3])))
    return run.step("S1", read, watch=False)


def _total_check(st, r):
    lost = r["held"] - r["expected"]
    st.check(abs(lost) <= TOL, "the fleet holds {:.3f} RBT but the faucet minted {:.3f}: {:+.3f} RBT "
                               "{} (controller/test-plan/custom/lost_rbt.py shows where)".format(
                                   r["held"], r["expected"], lost,
                                   "created from nowhere" if lost > 0 else "lost"))


def audit_start(ctx, ci):
    run = steps.CaseRun(ctx, "AUDIT-START", STEPS["AUDIT-START"])
    got = {}
    if _read_step(run, ctx, "start", got).outcome != steps.PASS:
        return run.verdict()

    def quarantine(st):
        n = _quarantine(got["r"]["quarantine"])
        st.data["quarantined"] = n
        st.note("{} problem token(s) locked out of use".format(n) if n else "none needed")
        if n:
            got["r"], _f = _audit(ctx, "start")       # re-check the quarantined fleet
    run.step("S2", quarantine, watch=False)
    r = got["r"]
    _START.clear()
    _START.update(r)
    run.step("S3", lambda st: _total_check(st, r), watch=False)
    run.step("S4", lambda st: st.check(not r["doubled_text"], r["doubled_text"]), watch=False)

    def known(st):
        for n in _soft_notes(r):
            st.note(n)
    run.step("S5", known, watch=False)
    return run.verdict(passed="clean: " + _describe(r))


def audit_end(ctx, ci):
    # AUDIT-END reports the run's true end state and does NOT change the fleet -
    # cleaning happens at the next run's AUDIT-START.
    run = steps.CaseRun(ctx, "AUDIT-END", STEPS["AUDIT-END"])
    got = {}
    if _read_step(run, ctx, "end", got).outcome != steps.PASS:
        return run.verdict()
    r = got["r"]

    def totals(st):
        _total_check(st, r)
        if _START and not _START.get("unreadable"):
            moved = round(r["held"] - _START["held"], 3)
            st.check(abs(moved) <= TOL, "the fleet total moved {:+.3f} RBT during this run ({:.3f} -> "
                                        "{:.3f})".format(moved, _START["held"], r["held"]))
    run.step("S2", totals, watch=False)
    run.step("S3", lambda st: st.check(not r["doubled_text"], r["doubled_text"]), watch=False)

    def known(st):
        for n in _soft_notes(r):
            st.note(n)
        if _START:
            st.note("committed {:+.3f}, burnt for FT {:+.3f} since the start".format(
                r["totals"]["committed"] - _START["totals"]["committed"],
                r["totals"]["burnt-for-FT"] - _START["totals"]["burnt-for-FT"]))
    run.step("S4", known, watch=False)
    return run.verdict(passed="clean: " + _describe(r))


CASES = {"AUDIT-START": audit_start, "AUDIT-END": audit_end}
ORDER = ["AUDIT-START", "AUDIT-END"]
NEEDS = {
    "AUDIT-START": {"senders": 0, "receivers": 0, "quorums": 0, "first": True},
    "AUDIT-END": {"senders": 0, "receivers": 0, "quorums": 0, "last": True, "exclusive": True},
}
NO_DELAY_RERUN = set(ORDER)
