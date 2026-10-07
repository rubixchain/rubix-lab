#!/usr/bin/env python3
"""
unpledged_spend.py - can a DID spend tokens it pledged as a quorum, once the
pledge has been released?

THE QUESTION
    A quorum pledges its own Free tokens as collateral for a transaction it
    signs. When the transaction settles the pledge is released: the tokens are
    Free again and their latest token-chain entry is "unpledge". They are the
    quorum's own tokens, and core's own comments say the quorum that pledged
    them may spend them (core/consensus/checks.go:304-307).

    On the lab every such spend is refused with
        failed to get quorum DID for token X: tokenID "X" not found in quorum tokens
    because the check looks the token up in the CURRENT transaction's pledges
    (txnInfo.Quorums, checks.go:308) instead of the pledge transaction it has
    just loaded (pledgedTxnInfo).

    This script builds that state from scratch on one node, so the finding does
    not rest on wallets left over from earlier runs.

THE STEPS                                                   correct product
    UPS-01  Q sends 1 RBT while its wallet is clean              accepted
    UPS-02  Q signs A -> B for Q's ENTIRE free balance, so Q
            pledges every Free token it holds                    accepted
    UPS-03  the pledge is released: those tokens are Free
            again, last chain role "unpledge"                    released
    UPS-04  Q sends 1 RBT - it now holds only unpledged tokens   accepted
            (bug: refused). For each refused token the database
            shows the correct lookup would accept it: Free on Q,
            last role unpledge, and the pledge transaction lists
            Q itself as the quorum that pledged it
    UPS-05  after a wait, Q sends 0.5, 1 and everything          accepted
            (bug: refused every time - the value is frozen)
    UPS-06  Q pledges again, for A -> B 1 RBT                    accepted
            (the frozen tokens still serve as collateral, and
            are unpledged - still unspendable - afterwards)
    UPS-07  Q gets 3 fresh RBT from the faucet, then sends
            1 RBT three times                                    all accepted
            (bug: a send is refused exactly when it picks an
            unpledged token - what gives RBT-Q-05/Q-07/B-03/N-15
            their ~70% pass rates)

    FAIL in UPS-04, -05 or -07 means the bug is present in the build under test.

WHAT IT CHANGES ON THE LAB - read before running
    Q's whole wallet ends up as unpledged tokens. While the bug is in the build
    Q cannot spend them, so Q stays a "former quorum" from then on (the runner
    keeps such DIDs out of the sender seat). By default Q is the clean DID
    holding the least RBT, so the least is frozen; choose it with --quorum.
    Every RBT used comes from the faucet, by transfer, as for every case.
    The databases are only read, never written.

RUN (on the controller)
    cd ~/Desktop/rubix/rubix-lab/controller/test-plan/custom
    python3 unpledged_spend.py                     # shows the plan, asks first
    python3 unpledged_spend.py --quorum 192.168.1.141 --yes
"""

import argparse
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", "master"))

import rubix_client as rc
import db_client as db
import test_runner as tr
from case_helpers import node_reason

SETTLE = 6                      # a receiver credits ~1-2s after the call returns
BUG = re.compile(r"failed to get quorum DID for token ([0-9A-Za-z_]+)")
PORT = rc.DEFAULT_PORT

RESULTS = []
EVIDENCE = {"steps": {}}


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def record(cid, title, expected, status, actual, note="", bug=False):
    """bug=True when the failure is the unpledged-token refusal itself, so the
    summary does not blame the bug for a refusal with another cause."""
    RESULTS.append({"id": cid, "case": title, "expected": expected, "status": status,
                    "actual": actual, "note": note, "bug": bug})
    print("  [{:<4}] {:<7} {}".format(status, cid, actual))
    if note:
        print("                 {}".format(note))


# ---------------------------------------------------------------------------
# Database readings (read-only)
# ---------------------------------------------------------------------------

# The latest token-chain entry of each token, with its role name.
LATEST = ("JOIN LATERAL (SELECT c.role, c.previous_transaction_id FROM tokenchain c "
          "              WHERE c.token_id = t.token_id ORDER BY c.position DESC LIMIT 1) lc ON TRUE "
          "LEFT JOIN token_role r ON r.id = lc.role ")
# The role's name, falling back to its id when token_role has no row for it.
# Ids follow models.TokenRoleTypes order (types/models/lookup.go): 8 = pledge,
# 9 = unpledge - the same fallback db_client.ex_pledged_free_value uses.
ROLE = "COALESCE(r.name, CASE lc.role WHEN 8 THEN 'pledge' WHEN 9 THEN 'unpledge' ELSE lc.role::text END)"


def free_tokens(e):
    """{token_id: {"value", "role", "prev"}} - e's Free RBT tokens and the role
    of each one's latest chain entry."""
    rows = db.query(
        e["host"],
        "SELECT t.token_id, t.token_value, " + ROLE + ", lc.previous_transaction_id "
        "FROM tokens t " + LATEST +
        "WHERE t.did = %s AND t.token_type = %s AND t.token_status = %s",
        (e["did"], db.TYPE_RBT, db.FREE))
    return dict((t, {"value": float(v), "role": role, "prev": prev}) for t, v, role, prev in rows)


def statuses(host, token_ids):
    """{token_id: status} for these tokens on `host`."""
    if not token_ids:
        return {}
    rows = db.query(host, "SELECT token_id, token_status FROM tokens WHERE token_id = ANY(%s)",
                    (list(token_ids),))
    return dict((t, int(s)) for t, s in rows)


def tx_info(entries, txid, attempts=10):
    """The stored TransactionInfo of `txid` from the first participant that has
    it (the quorum keeps a copy - the unpledge path reads it back)."""
    for _ in range(attempts):
        for e in entries:
            try:
                rows = db.query(e["host"], "SELECT info FROM transactions WHERE id = %s", (txid,))
            except db.DBUnavailable:
                continue
            if rows:
                info = rows[0][0]
                return json.loads(info) if isinstance(info, str) else info
        time.sleep(2)
    return None


def pledged_by(info, did):
    """{token_id: value} the quorum `did` pledged in this transaction."""
    out = {}
    for q in (info or {}).get("quorums") or []:
        if q and q.get("did") == did:
            for t in q.get("tokens") or []:
                if t and t.get("tokenId"):
                    out[t["tokenId"]] = float(t.get("tokenValue") or 0)
    return out


def still_pledged(host, token_ids):
    return [t for t, s in statuses(host, token_ids).items()
            if s in (db.PLEDGED, db.QUORUM_PLEDGED)]


def wait_until(check, timeout, every=5):
    """Poll check() until it returns truthy or `timeout` seconds pass.
    Returns (value, seconds waited)."""
    began = time.time()
    while True:
        value = check()
        if value or time.time() - began >= timeout:
            return value, round(time.time() - began, 1)
        time.sleep(every)


def prove(q, token, others):
    """Read, from the databases, what the ownership check had to work with for
    `token`, and what the CORRECT lookup would have concluded."""
    rows = db.query(
        q["host"],
        "SELECT t.token_status, t.did, " + ROLE + ", lc.previous_transaction_id "
        "FROM tokens t " + LATEST + "WHERE t.token_id = %s", (token,))
    if not rows:
        return False, "{}: not found on the sender's node".format(token), {}
    status, did, role, prev = rows[0]
    info = tx_info([q] + others, prev, attempts=1) if prev else None
    pledger = None
    for qi in (info or {}).get("quorums") or []:
        if any(t and t.get("tokenId") == token for t in qi.get("tokens") or []):
            pledger = qi.get("did")
    facts = {"token": token, "status": int(status), "owner_is_sender": did == q["did"],
             "latest_role": role, "pledge_tx": prev, "pledged_by": pledger}
    if int(status) == db.FREE and did == q["did"] and role == "unpledge" and pledger == q["did"]:
        return True, ("{}: Free on the sender, last chain role 'unpledge', pledged in tx {}... "
                      "by the sender itself - the intended check (initiator == the quorum that "
                      "pledged it) would ACCEPT").format(token, (prev or "?")[:12]), facts
    return False, ("{}: status {}, owned by the sender {}, last role '{}', pledged by {} - does "
                   "not match the bug's pattern, look at this token by hand").format(
                       token, db.STATUS_NAME.get(int(status), status), did == q["did"], role,
                       pledger), facts


# ---------------------------------------------------------------------------
# Lab operations
# ---------------------------------------------------------------------------

def send(src, dst, amount, memo):
    status, msg, result = rc.initiate_transaction(
        src["host"], src["did"], dst["did"], rbt=amount, memo=memo, port=PORT)
    txid = result.get("transactionID") if isinstance(result, dict) else ""
    return bool(status), msg or "", txid or ""


def fund_to(e, target):
    """Top `e` up to `target` free RBT from the faucet (whole tokens)."""
    have = db.value_in_status(e["host"], e["did"], db.FREE)
    if have >= target:
        return True, ""
    return rc.fund_did(e["host"], e["did"], int(math.ceil(target - have)), PORT)


def choose(pool, quorum_host):
    """Q (the DID under test), A (sends through Q), B (receives), C (signs Q's
    own sends). Q and A must be clean - no unpledged tokens, nothing pledged -
    so the starting state is known. B and C are taken from former quorums
    where possible, so the probe does not use up clean DIDs it does not need."""
    entries = [{"host": n["host"], "did": n["dids"][0]} for n in pool]
    info = {}
    for e in entries:
        try:
            info[e["host"]] = {"dirty": db.ex_pledged_free_value(e["host"], e["did"]),
                               "free": db.value_in_status(e["host"], e["did"], db.FREE),
                               "pledged": db.pledged_value(e["host"], e["did"])}
        except db.DBUnavailable:
            pass
    readable = [e for e in entries if e["host"] in info]
    clean = [e for e in readable
             if info[e["host"]]["dirty"] == 0 and info[e["host"]]["pledged"] == 0]
    if quorum_host:
        q = next((e for e in readable if e["host"] == quorum_host), None)
        if q is None:
            sys.exit("ERROR: --quorum {} is not a reachable pool node with a readable "
                     "database".format(quorum_host))
    else:
        if not clean:
            sys.exit("ERROR: no clean DID left (every node holds unpledged tokens) - pass "
                     "--quorum to run on a former quorum, without the clean control")
        q = min(clean, key=lambda e: info[e["host"]]["free"])
    rest = [e for e in readable if e is not q]
    clean_rest = sorted((e for e in rest if e in clean), key=lambda e: -info[e["host"]]["free"])
    if not clean_rest:
        sys.exit("ERROR: needs a second clean DID to send through Q (A); none is left")
    a = clean_rest[0]
    others = sorted((e for e in rest if e is not a),
                    key=lambda e: (info[e["host"]]["dirty"] == 0, -info[e["host"]]["free"]))
    if len(others) < 2:
        sys.exit("ERROR: needs at least 4 reachable nodes")
    c, b = others[0], others[1]
    return q, a, b, c, info


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def run(args):
    if not db.available():
        sys.exit("ERROR: psycopg2 is not installed.\n       sudo apt install -y python3-psycopg2")

    all_hosts = rc.load_hosts(args.hosts)
    if "RUBIX_FAUCET_HOST" not in os.environ:
        controller = [h["host"] for h in all_hosts if h["role"] == "controller"]
        if controller:
            rc.FAUCET_HOST = controller[0]
    ok, note = rc.faucet_ready()
    if not ok:
        sys.exit("ERROR: faucet not ready - {}".format(note))

    hosts = [h for h in all_hosts if h["role"] not in tr.FIXED_ROLES]
    pool, excluded = tr.discover_pool(hosts, PORT, rc.DEFAULT_TIMEOUT)
    for line in excluded:
        print("  excluded: {}".format(line))
    q, a, b, c, info = choose(pool, args.quorum)
    q_dirty = info[q["host"]]["dirty"] > 0 or info[q["host"]]["pledged"] > 0
    others = [a, b, c]
    EVIDENCE["participants"] = {"Q": q, "A": a, "B": b, "C": c}

    print()
    print("Q (under test)        {}  free {:.3f}, unpledged {:.3f}{}".format(
        q["host"], info[q["host"]]["free"], info[q["host"]]["dirty"],
        "  - ALREADY a former quorum: UPS-01 cannot be a clean control" if q_dirty else ""))
    print("A (sends through Q)   {}".format(a["host"]))
    print("B (receives)          {}".format(b["host"]))
    print("C (signs Q's sends)   {}".format(c["host"]))
    print()
    print("Q's whole wallet will end up as unpledged tokens. While the bug is in the")
    print("build, Q cannot spend them afterwards. Nothing is written to any database.")
    if not args.yes:
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Stopped - nothing was changed.")
    print()

    # Quorum wiring: A's transfers are signed by Q, Q's own sends by C.
    for e in (q, c):
        ok, msg = rc.quorum_setup(e["host"], e["did"], PORT)
        if not ok:
            sys.exit("ERROR: quorum setup on {} failed: {}".format(e["host"], msg))
    for node, quorum in ((a, q), (q, c)):
        ok, msg = rc.quorum_reset(node["host"], [quorum["did"]], PORT)
        if not ok:
            sys.exit("ERROR: could not point {} at quorum {}: {}".format(
                node["host"], quorum["host"], msg))
    # 5 RBT: 1 for UPS-01, and enough left that UPS-04 (1) and UPS-05 (0.5, 1,
    # the rest) can all be paid for if the build turns out NOT to have the bug.
    ok, msg = fund_to(q, 5)
    if not ok:
        sys.exit("ERROR: could not fund Q: {}".format(msg))
    time.sleep(SETTLE)

    # UPS-01 ----------------------------------------------------------------
    title01 = "Q sends 1 RBT while its wallet is clean"
    if q_dirty:
        record("UPS-01", title01, "accepted", "SKIP",
               "Q already holds unpledged tokens - no clean control on this node")
    else:
        ok, msg, txid = send(q, b, 1, "UPS-01")
        EVIDENCE["steps"]["UPS-01"] = {"ok": ok, "txid": txid, "message": msg}
        if not ok:
            record("UPS-01", title01, "accepted", "FAIL", "refused: " + node_reason(msg, 200),
                   "Q cannot make a normal send, so the rest would not isolate the bug - stopped")
            return finish(q)
        record("UPS-01", title01, "accepted", "PASS", "accepted (tx {}...)".format(txid[:12]))
    time.sleep(SETTLE)

    # Anything Q has pledged earlier must be back before its balance is read.
    wait_until(lambda: db.pledged_value(q["host"], q["did"]) < 0.0005, args.release_timeout)
    before = free_tokens(q)
    whole = round(sum(t["value"] for t in before.values()), 3)
    EVIDENCE["Q_free_before_pledge"] = before
    if whole < 0.001:
        record("UPS-02", "Q pledges its whole free balance", "accepted", "SKIP",
               "Q holds nothing free to pledge")
        return finish(q)

    # UPS-02 ----------------------------------------------------------------
    for e, target in ((a, whole + 5), (c, whole + 10)):
        ok, msg = fund_to(e, target)
        if not ok:
            sys.exit("ERROR: could not fund {}: {}".format(e["host"], msg))
    time.sleep(SETTLE)
    title02 = "Q signs A -> B for Q's whole free balance, pledging every free token"
    ok, msg, txid = send(a, b, whole, "UPS-02")
    if not ok:
        EVIDENCE["steps"]["UPS-02"] = {"ok": ok, "message": msg}
        record("UPS-02", title02, "accepted", "FAIL", "refused: " + node_reason(msg, 200),
               "without Q's pledge there is nothing to test - stopped")
        return finish(q)
    pledged = pledged_by(tx_info([q, a, b], txid), q["did"])
    EVIDENCE["steps"]["UPS-02"] = {"ok": ok, "txid": txid, "pledged": pledged}
    if not pledged:
        record("UPS-02", title02, "accepted", "FAIL",
               "accepted, but the stored transaction lists no token pledged by Q",
               "tx {} - the pledge cannot be followed - stopped".format(txid))
        return finish(q)
    record("UPS-02", title02, "accepted", "PASS",
           "{:.3f} RBT sent; Q pledged {} token(s) worth {:.3f}".format(
               whole, len(pledged), sum(pledged.values())))

    # UPS-03 ----------------------------------------------------------------
    title03 = "the pledge is released: tokens Free again, last chain role 'unpledge'"
    _, secs = wait_until(lambda: not still_pledged(q["host"], pledged), args.release_timeout)
    still = still_pledged(q["host"], pledged)
    now = free_tokens(q)
    unpledged = dict((t, v) for t, v in now.items() if v["role"] == "unpledge")
    clean_left = dict((t, v) for t, v in now.items() if v["role"] != "unpledge")
    back = [t for t in pledged if t in unpledged]
    EVIDENCE["steps"]["UPS-03"] = {"seconds": secs, "still_pledged": still,
                                   "unpledged": unpledged, "clean_left": clean_left}
    if still:
        record("UPS-03", title03, "released", "FAIL",
               "{} of {} pledged token(s) still pledged after {}s".format(
                   len(still), len(pledged), args.release_timeout),
               "the pledge was never released - a different problem - stopped")
        return finish(q)
    record("UPS-03", title03, "released", "PASS" if len(back) == len(pledged) else "FAIL",
           "released after ~{}s; {} of {} pledged token(s) are Free with role 'unpledge'".format(
               secs, len(back), len(pledged)),
           "Q now holds {:.3f} RBT in unpledged tokens{}".format(
               sum(v["value"] for v in unpledged.values()),
               "" if not clean_left else " and {:.3f} in {} other free token(s), so the next "
               "send may avoid them".format(sum(v["value"] for v in clean_left.values()),
                                           len(clean_left))))

    # UPS-04 ----------------------------------------------------------------
    title04 = "Q spends 1 RBT now that it holds only unpledged tokens"
    ok, msg, txid = send(q, b, 1, "UPS-04")
    time.sleep(SETTLE)
    locked = db.count_in_status(q["host"], q["did"], db.LOCKED)
    EVIDENCE["steps"]["UPS-04"] = {"ok": ok, "txid": txid, "message": msg, "locked_after": locked}
    if ok:
        record("UPS-04", title04, "accepted", "PASS",
               "accepted (tx {}...) - an unpledged token was spent; the bug is not in this "
               "build".format(txid[:12]))
    else:
        m = BUG.search(msg)
        if not m:
            record("UPS-04", title04, "accepted", "FAIL",
                   "refused for a DIFFERENT reason: " + node_reason(msg, 200))
        else:
            _matches, text, facts = prove(q, m.group(1), others)
            EVIDENCE["steps"]["UPS-04"]["proof"] = facts
            record("UPS-04", title04, "accepted", "FAIL",
                   "refused: failed to get quorum DID for token {} (checks.go:308)".format(
                       m.group(1)),
                   text + ("; nothing left Locked" if not locked else
                           "; {} token(s) left LOCKED on Q after the refusal".format(locked)),
                   bug=True)

    # UPS-05 ----------------------------------------------------------------
    title05 = "after a wait, Q spends 0.5, 1 and everything"
    print("  ...      waiting {}s before UPS-05".format(args.wait))
    time.sleep(args.wait)
    outcomes = []
    for amount in (0.5, 1, None):
        if amount is None:
            # "everything" is read now, after the first two, so it is what Q
            # really holds whether or not they went through.
            amount = round(sum(v["value"] for v in free_tokens(q).values()), 3)
            if amount < 0.001:
                continue
        ok, msg, _txid = send(q, b, amount, "UPS-05")
        m = BUG.search(msg or "")
        outcomes.append({"amount": amount, "ok": ok, "token": m.group(1) if m else None,
                         "reason": "" if ok else node_reason(msg, 160)})
        time.sleep(SETTLE)
    EVIDENCE["steps"]["UPS-05"] = outcomes
    bug_refusals = sum(1 for o in outcomes if not o["ok"] and o["token"])
    other_refusals = sum(1 for o in outcomes if not o["ok"] and not o["token"])
    notes = []
    if bug_refusals:
        notes.append("{} refusal(s) name an unpledged token {}s after the release - the value "
                     "stays frozen".format(bug_refusals, args.wait))
    if other_refusals:
        notes.append("{} refusal(s) for another reason - not this bug".format(other_refusals))
    record("UPS-05", title05, "accepted", "PASS" if all(o["ok"] for o in outcomes) else "FAIL",
           ", ".join("{}: {}".format(o["amount"], "accepted" if o["ok"] else
                                     ("refused on {}".format(o["token"]) if o["token"]
                                      else "refused - " + o["reason"][:60]))
                     for o in outcomes),
           "; ".join(notes), bug=bool(bug_refusals))

    # UPS-06 ----------------------------------------------------------------
    title06 = "Q pledges its unpledged tokens again (signs A -> B 1 RBT)"
    frozen_now = dict((t, v) for t, v in free_tokens(q).items() if v["role"] == "unpledge")
    if sum(v["value"] for v in frozen_now.values()) < 1:
        # Only reachable when UPS-04/05 could spend them - i.e. without the bug.
        record("UPS-06", title06, "accepted", "SKIP",
               "Q holds less than 1 RBT in unpledged tokens - they were spent in UPS-04/05, "
               "so there is nothing to re-pledge")
    else:
        ok, msg, txid = send(a, b, 1, "UPS-06")
        if not ok:
            EVIDENCE["steps"]["UPS-06"] = {"ok": ok, "message": msg}
            record("UPS-06", title06, "accepted", "FAIL", "refused: " + node_reason(msg, 200))
        else:
            repledged = pledged_by(tx_info([q, a, b], txid), q["did"])
            wait_until(lambda: not still_pledged(q["host"], repledged), args.release_timeout)
            roles = free_tokens(q)
            reused = [t for t in repledged if t in frozen_now]
            again = [t for t in repledged if roles.get(t, {}).get("role") == "unpledge"]
            EVIDENCE["steps"]["UPS-06"] = {"ok": ok, "txid": txid, "pledged": repledged,
                                           "were_unpledged": reused, "unpledged_again": again}
            record("UPS-06", title06, "accepted", "PASS",
                   "accepted: Q pledged {} token(s), {} of them already unpledged; {} are "
                   "'unpledge' again after release".format(len(repledged), len(reused), len(again)),
                   "unpledged tokens still work as collateral, and come back just as "
                   "unspendable" if reused else
                   "Q pledged other tokens, so this did not exercise the unpledged ones")

    # UPS-07 ----------------------------------------------------------------
    title07 = "Q receives 3 fresh RBT, then sends 1 RBT three times"
    frozen = set(t for t, v in free_tokens(q).items() if v["role"] == "unpledge")
    ok, msg = rc.fund_did(q["host"], q["did"], 3, PORT)
    if not ok:
        record("UPS-07", title07, "all accepted", "SKIP", "faucet top-up failed: " + msg)
        return finish(q)
    time.sleep(SETTLE)
    fresh = set(t for t, v in free_tokens(q).items() if v["role"] != "unpledge")
    rounds = []
    for _ in range(3):
        held_before = set(free_tokens(q))
        ok, msg, _txid = send(q, b, 1, "UPS-07")
        time.sleep(SETTLE)
        gone = held_before - set(free_tokens(q))
        m = BUG.search(msg or "")
        rounds.append({"ok": ok, "used_fresh": sorted(gone & fresh),
                       "used_unpledged": sorted(gone & frozen),
                       "refused_on": m.group(1) if m else None,
                       "reason": "" if ok else node_reason(msg, 160)})
    EVIDENCE["steps"]["UPS-07"] = {"fresh": sorted(fresh), "rounds": rounds}
    words = []
    for i, r in enumerate(rounds, 1):
        if r["ok"]:
            words.append("#{} accepted using {}".format(
                i, "an UNPLEDGED token" if r["used_unpledged"] else
                "fresh token(s)" if r["used_fresh"] else "token(s) not identified"))
        else:
            words.append("#{} refused on {}".format(
                i, "unpledged token " + r["refused_on"] if r["refused_on"] in frozen else
                (r["refused_on"] or r["reason"][:60])))
    spent_frozen = any(r["ok"] and r["used_unpledged"] for r in rounds)
    bug_hits = sum(1 for r in rounds if not r["ok"] and r["refused_on"] in frozen)
    accepted = sum(1 for r in rounds if r["ok"])
    if spent_frozen and bug_hits:
        note = ("an unpledged token was spent in one send and refused in another - the bug "
                "does not cover every path")
    elif bug_hits and accepted:
        note = "sends go through only when they happen to pick fresh tokens"
    elif bug_hits:
        note = "every send picked an unpledged token, even with fresh tokens in the wallet"
    else:
        note = ""
    record("UPS-07", title07, "all accepted", "PASS" if all(r["ok"] for r in rounds) else "FAIL",
           "; ".join(words), note, bug=bool(bug_hits))
    return finish(q)


def finish(q):
    try:
        frozen = sum(v["value"] for v in free_tokens(q).values() if v["role"] == "unpledge")
    except db.DBUnavailable:
        frozen = None
    print()
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    print("{} passed, {} failed, {} skipped.".format(
        passed, failed, len(RESULTS) - passed - failed))
    bug = [r["id"] for r in RESULTS if r["bug"]]
    if bug:
        print("The unpledged-token spend bug is present ({} refused an unpledged "
              "token).".format(", ".join(bug)))
    elif any(r["id"] == "UPS-04" and r["status"] == "PASS" for r in RESULTS):
        print("No unpledged-token refusal: this build lets a former quorum spend them.")
    if frozen is not None:
        print("Q {} now holds {:.3f} RBT in unpledged tokens.".format(q["host"], frozen))
    EVIDENCE["results"] = RESULTS
    EVIDENCE["Q_unpledged_at_end"] = frozen
    path = rc.new_report_paths("unpledged-spend", ("json",))["json"]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, indent=2, default=str)
    print("Evidence: {}".format(path))


def main():
    p = argparse.ArgumentParser(
        description="Can a DID spend tokens it pledged as a quorum, once the pledge is released?")
    p.add_argument("--hosts", default=tr.DEFAULT_HOSTS)
    p.add_argument("--quorum", default="",
                   help="host to put under test (default: the clean DID holding the least RBT)")
    p.add_argument("--release-timeout", type=int, default=900,
                   help="seconds to wait for a pledge to be released (default 900)")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds between UPS-04 and UPS-05 (default 60)")
    p.add_argument("--yes", action="store_true", help="do not ask before starting")
    run(p.parse_args())


if __name__ == "__main__":
    main()
