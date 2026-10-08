#!/usr/bin/env python3
"""
unpledged_spend.py - can a DID spend tokens it pledged as a quorum, once the
pledge has been released?

THE QUESTION
    A quorum pledges its own Free tokens as collateral for a transaction it
    signs. When the pledge is released the tokens are Free again and their
    latest token-chain entry is "unpledge". They are the quorum's own tokens,
    and core's own comments say the quorum that pledged them may spend them
    (core/consensus/checks.go:304-307).

    On the lab every such spend is refused with
        failed to get quorum DID for token X: tokenID "X" not found in quorum tokens
    because the check looks the token up in the CURRENT transaction's pledges
    (txnInfo.Quorums, checks.go:308) instead of the pledge transaction it has
    just loaded (pledgedTxnInfo).

HOW THE SCRIPT MAKES EACH SPEND HIT AN UNPLEDGED TOKEN
    A node, not the caller, picks the tokens a transfer spends. It takes whole
    tokens first (largest denomination first) and, within a denomination, the
    lowest token_id first (core/wallet/token_lock.go:411-560). So if every
    whole token Q holds is one it pledged, a whole-RBT send from Q MUST use an
    unpledged token. Each cycle gives Q exactly N whole tokens (10, 50, 100 by
    default) and has Q pledge all N for an N-RBT transfer - small amounts,
    no bulk.

WHEN A PLEDGE IS RELEASED
    Not when the transaction settles: the quorum releases its pledge for
    transaction T when it sees a LATER transaction spend a token T moved
    (core/callback.go:14-19, core/wallet/pledge.go:486-528). So B, who received
    T, passes on just enough whole tokens to include one of T's, using the
    same lowest-token_id order to work out how many.

THE CASES (each cycle, N = 10, 50, 100)               correct product
    UPS-CTRL    Q sends whole RBT while its wallet is clean       accepted
    UPS-N-1     Q pledges its N whole tokens for A -> B N RBT;
                B passes one on; the pledge is released           released
    UPS-N-2     Q sends 1 RBT - one unpledged whole token         accepted
    UPS-N-3     Q sends every unpledged whole token at once       accepted
    UPS-N-4     Q sends 0.5 - splits an unpledged whole token     accepted
                (a split spends a freshly minted part, so this
                shows whether the bug covers that path too)
    after the cycles:
    UPS-WAIT    after a wait, Q sends 1 RBT again                 accepted
    UPS-REPLEDGE  Q pledges an unpledged token again              accepted
                (still usable as collateral; comes back unpledged)
    UPS-LOWEST  Q gets 3 fresh RBT; the script predicts from the
                database which whole token Q will spend (its
                lowest token_id) and whether that send is refused accepted
                - one unpledged token with a low id blocks every
                whole-RBT send, however much fresh RBT Q holds

    the split path (UPS-N-4 showed a split of an unpledged token goes through):
    SPL-0.999 / 0.5 / 0.1 / 0.001  each fraction below 1 splits Q's lowest
                (unpledged) whole token - accepted? and B credited?   accepted
    SPL-1.5     above 1: one unpledged whole token moves UNSPLIT,
                a second is split - is the unsplit one refused?   accepted
    SPL-DRAIN   repeated 0.999 sends: how much stuck value moves
                out this way, and what change is left behind      accepted
    WHY: the ownership check (checks.go:213) examines only the transferred
    tokens' previous transaction. In a split the transferred token is a new
    part whose previous transaction is the split; the unpledged parent is
    listed only in committedTokens, which the check never reads. So whole
    unpledged tokens are refused, while splits of them are never checked.

    A refusal naming an unpledged token is the bug; for each one the
    databases show the token is Free, owned by Q, last role "unpledge", and
    that its pledge transaction names Q as the quorum that pledged it - so
    the check the code intends (initiator == that quorum) would accept.

WHAT IT CHANGES ON THE LAB - read before running
    Q ends with the largest N (100 by default) in unpledged whole tokens, which
    it cannot spend while the bug is in the build. By default Q is the clean
    DID with the fewest whole tokens; choose it with --quorum. Q, A (sends
    through Q) and B (passes tokens on) must be clean DIDs; C signs Q's and
    B's sends. All RBT comes from the faucet, by transfer. Databases are only
    read. Do not run it during a catalogue run - it rewires 4 nodes' quorums.

RUN (on the controller)
    cd ~/Desktop/rubix/rubix-lab/controller/test-plan/custom
    python3 unpledged_spend.py                       # shows the plan, asks first
    python3 unpledged_spend.py --sizes 10,50 --quorum 192.168.1.141 --yes
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
STARTED = time.time()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def record(cid, title, expected, status, actual, note="", bug=False):
    """bug=True when the failure is the unpledged-token refusal itself, so the
    summary does not blame the bug for a refusal with another cause."""
    RESULTS.append({"id": cid, "case": title, "expected": expected, "status": status,
                    "actual": actual, "note": note, "bug": bug})
    print("  [{:<4}] {:<12} {}".format(status, cid, actual))
    if note:
        print("                      {}".format(note))


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


def whole_sorted(e):
    """e's Free whole (1.0) tokens in the order the node locks them: the lock
    query is ORDER BY token_value DESC, token_id ASC (token_lock.go), and this
    asks the node's own database, so the collation is the same."""
    return [t for (t,) in db.query(
        e["host"],
        "SELECT token_id FROM tokens WHERE did = %s AND token_type = %s AND token_status = %s "
        "AND token_value = 1 ORDER BY token_id ASC",
        (e["did"], db.TYPE_RBT, db.FREE))]


def statuses(host, token_ids):
    """{token_id: status} for these tokens on `host`."""
    if not token_ids:
        return {}
    rows = db.query(host, "SELECT token_id, token_status FROM tokens WHERE token_id = ANY(%s)",
                    (list(token_ids),))
    return dict((t, int(s)) for t, s in rows)


def still_pledged(host, token_ids):
    return [t for t, s in statuses(host, token_ids).items()
            if s in (db.PLEDGED, db.QUORUM_PLEDGED)]


def tx_info(entries, txid, attempts=10):
    """The stored TransactionInfo of `txid` from the first participant that has it."""
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


def moved_tokens(info):
    """Token ids the transaction moved (its RBT transaction tokens)."""
    return [t["tokenId"] for t in ((info or {}).get("tokens") or {}).get("rbt") or []
            if t and t.get("tokenId")]


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
        return False, "{}: not found on Q's node".format(token), {}
    status, did, role, prev = rows[0]
    info = tx_info([q] + others, prev, attempts=1) if prev else None
    pledger = None
    for qi in (info or {}).get("quorums") or []:
        if any(t and t.get("tokenId") == token for t in qi.get("tokens") or []):
            pledger = qi.get("did")
    facts = {"token": token, "status": int(status), "owner_is_q": did == q["did"],
             "latest_role": role, "pledge_tx": prev, "pledged_by": pledger}
    if int(status) == db.FREE and did == q["did"] and role == "unpledge" and pledger == q["did"]:
        return True, ("{}: Free on Q, last chain role 'unpledge', pledged in tx {}... by Q "
                      "itself - the intended check (initiator == the quorum that pledged it) "
                      "would ACCEPT").format(token, (prev or "?")[:12]), facts
    return False, ("{}: status {}, owned by Q {}, last role '{}', pledged by {} - does not "
                   "match the bug's pattern, look at this token by hand").format(
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


def fund_whole(e, count):
    """Give `e` at least `count` free whole tokens (the faucet sends whole ones)."""
    have = len(whole_sorted(e))
    if have >= count:
        return True, ""
    ok, msg = rc.fund_did(e["host"], e["did"], count - have, PORT)
    if ok:
        time.sleep(SETTLE)
    return ok, msg


def pass_on(b, a, c, moved, memo):
    """B sends A just enough whole tokens to include one that `moved` brought
    it. Spending any token a pledged transaction moved makes the quorum
    release that pledge (callback.go:14-19, pledge.go:520-526). Returns
    (ok, how many RBT, message)."""
    order = whole_sorted(b)
    moved = set(moved)
    first = next((i for i, t in enumerate(order) if t in moved), None)
    if first is None:
        return False, 0, "B holds none of the tokens the pledged transfer moved"
    k = first + 1
    ok, msg = fund_to(c, k + 10)          # C signs B's send and must pledge it
    if not ok:
        return False, k, "could not fund C: " + msg
    ok, msg, _txid = send(b, a, k, memo)
    return ok, k, msg


def choose(pool, quorum_host):
    """Q (under test), A (sends through Q), B (passes tokens on) - all clean, so
    the starting state is known - and C (signs Q's and B's sends), taken from
    former quorums where possible so clean DIDs are not used up needlessly."""
    entries = [{"host": n["host"], "did": n["dids"][0]} for n in pool]
    info = {}
    for e in entries:
        try:
            info[e["host"]] = {"dirty": db.ex_pledged_free_value(e["host"], e["did"]),
                               "free": db.value_in_status(e["host"], e["did"], db.FREE),
                               "pledged": db.pledged_value(e["host"], e["did"]),
                               "whole": len(whole_sorted(e))}
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
        q = min(clean, key=lambda e: info[e["host"]]["whole"])
    rest = [e for e in readable if e is not q]
    clean_rest = [e for e in rest if e in clean]
    if len(clean_rest) < 2:
        sys.exit("ERROR: needs two more clean DIDs besides Q (A sends through Q, B passes "
                 "tokens on); only {} left".format(len(clean_rest)))
    a = max(clean_rest, key=lambda e: info[e["host"]]["whole"])
    b = min((e for e in clean_rest if e is not a), key=lambda e: info[e["host"]]["whole"])
    others = sorted((e for e in rest if e is not a and e is not b),
                    key=lambda e: (info[e["host"]]["dirty"] == 0, -info[e["host"]]["free"]))
    if not others:
        sys.exit("ERROR: needs at least 4 reachable nodes")
    return q, a, b, others[0], info


# ---------------------------------------------------------------------------
# One spend from Q, judged
# ---------------------------------------------------------------------------

def q_spend(cid, title, q, b, c, others, amount, unpledged, proven):
    """Q sends `amount` to B; PASS if accepted, FAIL if refused. A refusal that
    names an unpledged token is the bug - proved from the databases the first
    time each cycle (`proven` collects the tokens already shown)."""
    # C signs this and must pledge it. Its earlier pledges stay held until the
    # receivers pass those tokens on, so top it up before every send it signs.
    ok, msg = fund_to(c, amount + 10)
    if not ok:
        sys.exit("ERROR: could not fund C: {}".format(msg))
    before = set(free_tokens(q))
    # Count only NEW locks: a DID can already hold stuck Locked tokens from
    # earlier runs (.107 had 3 on 2026-10-08), which this send did not cause.
    locked_before = db.count_in_status(q["host"], q["did"], db.LOCKED)
    ok, msg, txid = send(q, b, amount, cid)
    time.sleep(SETTLE)
    locked = max(0, db.count_in_status(q["host"], q["did"], db.LOCKED) - locked_before)
    used = sorted(before - set(free_tokens(q))) if ok else []
    step = {"amount": amount, "ok": ok, "txid": txid, "used": used,
            "message": "" if ok else msg, "locked_after": locked}
    EVIDENCE["steps"][cid] = step
    if ok:
        hit = [t for t in used if t in unpledged]
        record(cid, title, "accepted", "PASS",
               "accepted - spent {} token(s){}".format(
                   len(used), ", {} of them unpledged".format(len(hit)) if hit else ""),
               "an unpledged token was spent: this path does not hit the bug" if hit else "")
        return
    m = BUG.search(msg)
    if not m:
        record(cid, title, "accepted", "FAIL",
               "refused for a DIFFERENT reason: " + node_reason(msg, 200))
        return
    token = m.group(1)
    note = ""
    if not proven:
        _matches, note, step["proof"] = prove(q, token, others)
        proven.append(token)
    if locked:
        note += ("; " if note else "") + "{} token(s) left LOCKED on Q after the refusal".format(locked)
    record(cid, title, "accepted", "FAIL",
           "refused on unpledged token {} ({})".format(
               token, "one of the N Q pledged" if token in unpledged else "NOT one tracked here"),
           note, bug=True)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run(args):
    if not db.available():
        sys.exit("ERROR: psycopg2 is not installed.\n       sudo apt install -y python3-psycopg2")
    sizes = sorted(set(int(s) for s in args.sizes.split(",") if s.strip()))
    if not sizes or sizes[0] < 2:
        sys.exit("ERROR: --sizes needs whole numbers of 2 or more, e.g. 10,50,100")

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
    EVIDENCE["sizes"] = sizes

    print()
    print("Q (under test)          {}  {} whole token(s), {:.3f} free{}".format(
        q["host"], info[q["host"]]["whole"], info[q["host"]]["free"],
        "  - ALREADY a former quorum: no clean control" if q_dirty else ""))
    print("A (sends through Q)     {}".format(a["host"]))
    print("B (passes tokens on)    {}".format(b["host"]))
    print("C (signs Q's/B's sends) {}".format(c["host"]))
    print("Cycles: N = {} RBT".format(", ".join(str(n) for n in sizes)))
    print()
    print("Q ends with {} RBT in unpledged whole tokens. While the bug is in the build Q".format(
        sizes[-1]))
    print("cannot spend them. Nothing is written to any database.")
    if not args.yes:
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Stopped - nothing was changed.")
    print()

    # Quorum wiring: A's transfers are signed by Q; Q's and B's by C.
    for e in (q, c):
        ok, msg = rc.quorum_setup(e["host"], e["did"], PORT)
        if not ok:
            sys.exit("ERROR: quorum setup on {} failed: {}".format(e["host"], msg))
    for node, quorum in ((a, q), (q, c), (b, c)):
        ok, msg = rc.quorum_reset(node["host"], [quorum["did"]], PORT)
        if not ok:
            sys.exit("ERROR: could not point {} at quorum {}: {}".format(
                node["host"], quorum["host"], msg))
    ok, msg = fund_to(c, sizes[-1] + 20)
    if not ok:
        sys.exit("ERROR: could not fund C: {}".format(msg))

    # UPS-CTRL: a clean Q sends whole RBT, which also trims its whole tokens
    # down to the first cycle's N.
    first = sizes[0]
    title = "Q sends whole RBT while its wallet is clean"
    if q_dirty:
        record("UPS-CTRL", title, "accepted", "SKIP",
               "Q already holds unpledged or pledged tokens - no clean control on this node")
    else:
        ok, msg = fund_whole(q, first + 1)
        if not ok:
            sys.exit("ERROR: could not fund Q: {}".format(msg))
        extra = len(whole_sorted(q)) - first
        ok, msg, txid = send(q, b, extra, "UPS-CTRL")
        EVIDENCE["steps"]["UPS-CTRL"] = {"amount": extra, "ok": ok, "txid": txid, "message": msg}
        if not ok:
            record("UPS-CTRL", title, "accepted", "FAIL", "refused: " + node_reason(msg, 200),
                   "Q cannot make a normal send, so the rest would not isolate the bug - stopped")
            return finish(q)
        record("UPS-CTRL", title, "accepted", "PASS",
               "{} RBT accepted (tx {}...)".format(extra, txid[:12]))
        time.sleep(SETTLE)

    unpledged = set()
    for n in sizes:
        print("  -- cycle N = {} --".format(n))
        if not cycle(n, q, a, b, c, others, unpledged, args):
            return finish(q)

    after_cycles(q, a, b, c, others, unpledged, args)
    print("  -- split path --")
    split_path(q, b, c, others, unpledged, args)
    return finish(q)


def split_path(q, b, c, others, unpledged, args):
    """Where the split path ends. The ownership check (checks.go:213) examines
    only the TRANSFERRED tokens' previous transaction. A send below 1 RBT
    splits Q's lowest whole token: the transferred token is a new part whose
    previous transaction is the split, and the unpledged parent is listed only
    in committedTokens, which the check never looks at - so the unpledge rule
    is never applied. These cases measure what that means in practice."""
    def lowest_is_unpledged():
        order = whole_sorted(q)
        return bool(order) and order[0] in unpledged

    def credited_send(cid, title, amount):
        if not lowest_is_unpledged():
            record(cid, title, "accepted", "SKIP",
                   "Q's lowest whole token is not an unpledged one, so this send would not use one")
            return None
        b_before = db.value_in_status(b["host"], b["did"], db.FREE)
        q_spend(cid, title, q, b, c, others, amount, unpledged, ["(already shown)"])
        step = EVIDENCE["steps"].get(cid, {})
        if step.get("ok"):
            gained = round(db.value_in_status(b["host"], b["did"], db.FREE) - b_before, 3)
            step["b_gained"] = gained
            RESULTS[-1]["note"] = ("B credited {:+.3f}; ".format(gained) +
                                   "the split consumed an unpledged whole token, so its value "
                                   "is no longer stuck").strip()
            print("                      B credited {:+.3f}".format(gained))
        return step.get("ok")

    # SPL-<x>: every fraction below 1 splits one unpledged whole token.
    for amount in (0.999, 0.5, 0.1, 0.001):
        credited_send("SPL-{}".format(amount),
                      "Q sends {} - below 1, splits its lowest (unpledged) whole token".format(amount),
                      amount)

    # SPL-1.5: above 1, one unpledged whole token moves without being split.
    credited_send("SPL-1.5", "Q sends 1.5 - one unpledged whole token moves unsplit, "
                             "a second is split", 1.5)

    # SPL-DRAIN: how much stuck value fractions can move, and what is left.
    title = "Q moves stuck value out with repeated 0.999 sends"
    stuck_before = sum(1 for t in whole_sorted(q) if t in unpledged)
    moved, refused = 0, 0
    for i in range(args.drain):
        if not lowest_is_unpledged():
            break
        ok, msg = fund_to(c, 11)
        if not ok:
            sys.exit("ERROR: could not fund C: {}".format(msg))
        ok, msg, _txid = send(q, b, 0.999, "SPL-DRAIN")
        time.sleep(SETTLE)
        if ok:
            moved += 1
        else:
            refused += 1
    stuck_after = sum(1 for t in whole_sorted(q) if t in unpledged)
    EVIDENCE["steps"]["SPL-DRAIN"] = {"sends": args.drain, "accepted": moved, "refused": refused,
                                      "stuck_before": stuck_before, "stuck_after": stuck_after}
    if not moved and not refused:
        record("SPL-DRAIN", title, "accepted", "SKIP", "no unpledged whole token left to drain")
        return
    record("SPL-DRAIN", title, "accepted", "PASS" if not refused else "FAIL",
           "{} of {} sends accepted; unpledged whole tokens {} -> {}; {:.3f} RBT moved".format(
               moved, moved + refused, stuck_before, stuck_after, 0.999 * moved),
           "each send frees 0.999 and leaves 0.001 change on Q; draining all of Q's stuck "
           "value this way takes one send per unpledged whole token. The change parts can "
           "only be spent once Q holds no whole tokens (larger denominations are always "
           "taken first)")


def cycle(n, q, a, b, c, others, unpledged, args):
    """One N: Q pledges exactly its N whole tokens, the pledge is released,
    then Q tries to spend them three ways. Returns False to stop the run."""
    # Exactly N whole tokens on Q: top up (the faucet sends whole tokens).
    have = len(whole_sorted(q))
    if have > n:
        record("UPS-{}-1".format(n), "Q pledges its N whole tokens", "released", "SKIP",
               "Q holds {} whole tokens, more than {} - sizes must grow".format(have, n))
        return False
    ok, msg = fund_whole(q, n)
    if not ok:
        sys.exit("ERROR: could not fund Q: {}".format(msg))
    q_whole = whole_sorted(q)
    ok, msg = fund_whole(a, n)
    if not ok:
        sys.exit("ERROR: could not fund A: {}".format(msg))

    # UPS-N-1: pledge, pass on, release
    cid = "UPS-{}-1".format(n)
    title = "Q pledges its {} whole tokens for A -> B {} RBT; B passes one on".format(n, n)
    ok, msg, txid = send(a, b, n, cid)
    if not ok:
        EVIDENCE["steps"][cid] = {"ok": ok, "message": msg}
        record(cid, title, "released", "FAIL", "A's transfer refused: " + node_reason(msg, 200),
               "without Q's pledge there is nothing to test - stopped")
        return False
    info = tx_info([q, a, b], txid)
    pledged = pledged_by(info, q["did"])
    moved = moved_tokens(info)
    time.sleep(SETTLE)
    ok_pass, k, msg_pass = pass_on(b, a, c, moved, cid + " pass-on")
    if not ok_pass:
        EVIDENCE["steps"][cid] = {"txid": txid, "pledged": pledged, "pass_on": msg_pass}
        record(cid, title, "released", "SKIP",
               "Q pledged {} token(s), but the release could not be triggered: {}".format(
                   len(pledged), node_reason(msg_pass, 160)))
        return False
    _, secs = wait_until(lambda: not still_pledged(q["host"], pledged), args.release_timeout)
    still = still_pledged(q["host"], pledged)
    roles = free_tokens(q)
    back = set(t for t in pledged if roles.get(t, {}).get("role") == "unpledge")
    all_whole = set(q_whole) <= set(pledged)
    unpledged |= back
    EVIDENCE["steps"][cid] = {"txid": txid, "pledged": pledged, "moved": moved,
                              "passed_on": k, "release_seconds": secs, "still_pledged": still}
    if still:
        record(cid, title, "released", "FAIL",
               "{} of {} pledged token(s) still pledged {}s after B passed one on".format(
                   len(still), len(pledged), args.release_timeout),
               "B spent a token the transfer moved, so the pledge should have been released - "
               "a separate finding; stopped")
        return False
    record(cid, title, "released", "PASS" if len(back) == len(pledged) else "FAIL",
           "Q pledged {} token(s) ({}all of its whole tokens); B passed on {} RBT; released "
           "after ~{}s, {} now Free with role 'unpledge'".format(
               len(pledged), "" if all_whole else "NOT ", k, secs, len(back)))

    proven = []
    q_spend("UPS-{}-2".format(n), "Q sends 1 RBT - one unpledged whole token",
            q, b, c, others, 1, unpledged, proven)
    left = sum(1 for t in whole_sorted(q) if t in unpledged)
    if left:
        q_spend("UPS-{}-3".format(n), "Q sends every unpledged whole token at once",
                q, b, c, others, left, unpledged, proven)
    else:
        record("UPS-{}-3".format(n), "Q sends every unpledged whole token at once", "accepted",
               "SKIP", "no unpledged whole token left - they were spent")
    if any(t in unpledged for t in whole_sorted(q)):
        q_spend("UPS-{}-4".format(n), "Q sends 0.5 - splits an unpledged whole token",
                q, b, c, others, 0.5, unpledged, proven)
    else:
        record("UPS-{}-4".format(n), "Q sends 0.5 - splits an unpledged whole token", "accepted",
               "SKIP", "no unpledged whole token left to split")
    return True


def after_cycles(q, a, b, c, others, unpledged, args):
    # UPS-WAIT
    title = "after a wait, Q sends 1 RBT again"
    if any(t in unpledged for t in whole_sorted(q)):
        print("  ...          waiting {}s".format(args.wait))
        time.sleep(args.wait)
        q_spend("UPS-WAIT", title, q, b, c, others, 1, unpledged, ["(already shown)"])
    else:
        record("UPS-WAIT", title, "accepted", "SKIP", "no unpledged whole token left")

    # UPS-REPLEDGE: Q's lowest whole token is unpledged, so pledging 1 RBT uses it.
    title = "Q pledges an unpledged token again (A -> B 1 RBT), B passes it on"
    lowest = (whole_sorted(q) or [None])[0]
    if lowest not in unpledged:
        record("UPS-REPLEDGE", title, "accepted", "SKIP",
               "Q's lowest whole token is not an unpledged one, so the pledge would not use one")
    else:
        ok, msg = fund_whole(a, 1)
        ok, msg, txid = send(a, b, 1, "UPS-REPLEDGE")
        if not ok:
            record("UPS-REPLEDGE", title, "accepted", "FAIL", "refused: " + node_reason(msg, 200))
        else:
            info = tx_info([q, a, b], txid)
            pledged = pledged_by(info, q["did"])
            time.sleep(SETTLE)
            ok_pass, k, msg_pass = pass_on(b, a, c, moved_tokens(info), "UPS-REPLEDGE pass-on")
            if ok_pass:
                wait_until(lambda: not still_pledged(q["host"], pledged), args.release_timeout)
            roles = free_tokens(q)
            reused = [t for t in pledged if t in unpledged]
            again = [t for t in pledged if roles.get(t, {}).get("role") == "unpledge"]
            EVIDENCE["steps"]["UPS-REPLEDGE"] = {"txid": txid, "pledged": pledged,
                                                 "were_unpledged": reused, "unpledged_again": again}
            record("UPS-REPLEDGE", title, "accepted", "PASS",
                   "accepted: Q pledged {} token(s), {} already unpledged; {} 'unpledge' again "
                   "after release".format(len(pledged), len(reused), len(again)),
                   "unpledged tokens still work as collateral, and come back just as "
                   "unspendable" if reused else
                   "Q pledged other tokens, so this did not exercise the unpledged ones")

    # UPS-LOWEST: predict the token from the lock order, then check.
    title = "Q gets 3 fresh RBT, then sends 1 RBT - its lowest whole token decides"
    ok, msg = rc.fund_did(q["host"], q["did"], 3, PORT)
    if not ok:
        record("UPS-LOWEST", title, "accepted", "SKIP", "faucet top-up failed: " + msg)
        return
    time.sleep(SETTLE)
    ok, msg = fund_to(c, 11)
    if not ok:
        sys.exit("ERROR: could not fund C: {}".format(msg))
    order = whole_sorted(q)
    fresh = [t for t in order if t not in unpledged]
    lowest = order[0] if order else None
    before = set(free_tokens(q))
    ok, msg, txid = send(q, b, 1, "UPS-LOWEST")
    time.sleep(SETTLE)
    m = BUG.search(msg or "")
    # The prediction is WHICH token Q spends - its lowest whole token - and it
    # holds whether the send is accepted (that token left) or refused (named).
    used = sorted(before - set(free_tokens(q))) if ok else []
    held = (lowest in used) if ok else bool(m and m.group(1) == lowest)
    EVIDENCE["steps"]["UPS-LOWEST"] = {"order_first": order[:5], "fresh": fresh, "lowest": lowest,
                                       "ok": ok, "used": used, "message": msg}
    record("UPS-LOWEST", title, "accepted", "PASS" if ok else "FAIL",
           "{} with {} fresh whole token(s) in the wallet; predicted it would spend its lowest "
           "whole token {} ({}) - prediction {}".format(
               "accepted" if ok else "refused on " + (m.group(1) if m else node_reason(msg, 60)),
               len(fresh), lowest, "unpledged" if lowest in unpledged else "fresh",
               "held" if held else "DID NOT hold"),
           "one unpledged token with a low id blocks every whole-RBT send, whatever else the "
           "wallet holds" if not ok and lowest in unpledged else "",
           bug=bool(not ok and m))


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
        print("The unpledged-token spend bug is present: {} refused an unpledged token.".format(
            ", ".join(bug)))
    elif any(r["id"].endswith("-2") and r["status"] == "PASS" for r in RESULTS):
        print("No unpledged-token refusal: this build lets a former quorum spend them.")
    if frozen is not None:
        print("Q {} now holds {:.3f} RBT in unpledged tokens.".format(q["host"], frozen))
    EVIDENCE["results"] = RESULTS
    EVIDENCE["Q_unpledged_at_end"] = frozen
    paths = rc.new_report_paths("unpledged-spend", ("json", "pdf"))
    with open(paths["json"], "w", encoding="utf-8") as fh:
        json.dump(EVIDENCE, fh, indent=2, default=str)
    print("Evidence: {}".format(paths["json"]))

    # The same PDF layout as the catalogue runs (report_builder).
    import report_builder
    parts = EVIDENCE.get("participants") or {}
    meta = {"duration_seconds": time.time() - STARTED, "conditions": [
        ("Run started", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED))),
        ("Question", "Can a DID spend tokens it pledged as a quorum, once the pledge is released?"),
        ("Participants", "; ".join("{} {}".format(k, v["host"]) for k, v in parts.items())),
        ("Cycles (RBT)", ", ".join(str(n) for n in EVIDENCE.get("sizes") or [])),
        ("Q at the end", "{:.3f} RBT in unpledged tokens".format(frozen) if frozen is not None else "unknown"),
        ("Bug present", ", ".join(bug) if bug else "no unpledged-token refusal"),
    ]}
    rows = [{"test_id": r["id"], "case": r["case"], "expected": r["expected"],
             "status": r["status"], "actual": r["actual"], "note": r["note"],
             "db": "-", "fullnode": "-", "seconds": ""} for r in RESULTS]
    if report_builder.build_pdf(paths["pdf"], "Rubix Lab - Unpledged Token Spend", meta, rows, set()):
        print("PDF     : {}".format(paths["pdf"]))
    else:
        print("PDF     : not written (reportlab missing: sudo apt install -y python3-reportlab)")


def main():
    p = argparse.ArgumentParser(
        description="Can a DID spend tokens it pledged as a quorum, once the pledge is released?")
    p.add_argument("--hosts", default=tr.DEFAULT_HOSTS)
    p.add_argument("--quorum", default="",
                   help="host to put under test (default: the clean DID with the fewest whole tokens)")
    p.add_argument("--sizes", default="10,50,100",
                   help="pledge sizes in RBT, one cycle each, ascending (default 10,50,100)")
    p.add_argument("--release-timeout", type=int, default=300,
                   help="seconds to wait for a pledge to be released after B passes a token "
                        "on (default 300)")
    p.add_argument("--wait", type=int, default=60,
                   help="seconds before UPS-WAIT (default 60)")
    p.add_argument("--drain", type=int, default=5,
                   help="0.999 sends in SPL-DRAIN (default 5)")
    p.add_argument("--yes", action="store_true", help="do not ask before starting")
    run(p.parse_args())


if __name__ == "__main__":
    main()
