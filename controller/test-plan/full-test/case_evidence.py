#!/usr/bin/env python3
"""
case_evidence.py - the database check every case gets, and the evidence it
leaves behind.

A case asserts what it was written to assert, usually through the API. That is
not enough on its own: an API can answer "success" for a transfer whose rows
never landed, or a case can pass while the wallet underneath it is malformed.
So the runner wraps EVERY case the same way:

    begin()   before the case: the wallet rows of every participant (senders,
              receivers AND quorums), and where the operation log stands
    finish()  after the case: the same rows again, plus every transaction and
              FT mint the case made (rubix_client.OPS), then these checks:

      persisted    every successful transaction's ID is in the `transactions`
                   table on the sender's node and on the receiver's node
      conserved    across the participants, RBT held (free + locked + pledged
                   + committed + burnt-for-FT) changed by exactly what came in
                   from outside (the faucet) minus what went out to DIDs
                   outside the case. Burnt is left out: a split burns the parent
                   and creates children worth the same.
      no_locks     no RBT is left Locked that was not Locked before
      rows_valid   every new token has a chain row and a legal value, and
                   token_denom moved with the free rows

A case that passed its own assertion but fails any of these is a FAIL. The
readings behind every check go into the run's evidence file.

One more check is REPORTED but does not decide the verdict, because the
fullnode is an observer, not part of a transfer:

      fullnode     each successful transaction is in fullnode_transactions
                   (accepted) or fullnode_invalid_transactions (rejected, with
                   the reason) on the fullnode. The fullnode validates with
                   several workers in parallel and gives up after 3 attempts
                   ~6s apart (core/fullnode_txn_processor.go), so two
                   transactions on the same token close together can be
                   checked out of order and rejected with a previous-
                   transaction mismatch. That is a fullnode finding, and this
                   shows which case, which transaction and why.
"""

import re
import time

import db_client as db
import rubix_client as rc

SETTLE_SECONDS = 30          # how long to wait for a transaction row to land
FULLNODE_HOST = ""           # set by test_runner from hosts.txt (role fullnode)
FULLNODE_WAIT = 30           # the fullnode retries ~6s, then may be queued
VERDICT_CHECKS = ("persisted", "conserved", "no_locks", "rows_valid")
_HELD = ("free", "locked", "pledged", "committed", "burnt_for_ft")


def participants(unit):
    """[(role, entry)] - each DID once, quorums included."""
    seen, out = set(), []
    for role, entries in (("quorum", unit.quorums), ("sender", unit.senders),
                          ("receiver", unit.receivers)):
        for e in entries:
            if e["did"] not in seen:
                seen.add(e["did"])
                out.append((role, e))
    return out


def _capture(host, did):
    """The row snapshot, or None when the database cannot be read (an
    infrastructure problem, reported as such - never a verdict)."""
    try:
        return db.capture(host, did)
    except Exception:
        return None


def begin(unit):
    state = {"op_index": len(rc.OPS), "before": {}, "started": time.time()}
    for _role, e in participants(unit):
        state["before"][e["did"]] = _capture(e["host"], e["did"])
    return state


def _held(snap):
    return sum(snap["totals"].get(k, 0.0) for k in _HELD)


def _wait_for_rows(ops, host_of):
    """Wait until every successful transaction's row is on each participant
    node it touched. Returns {txid: {host: found}}."""
    want = {}
    for op in ops:
        if op["kind"] != "tx" or not op["status"] or not op["txid"]:
            continue
        hosts = set()
        if op["initiator"] in host_of:
            hosts.add(host_of[op["initiator"]])
        if op["owner"] and op["owner"] != op["initiator"] and op["owner"] in host_of:
            hosts.add(host_of[op["owner"]])
        want[op["txid"]] = dict((h, False) for h in hosts)
    deadline = time.time() + SETTLE_SECONDS
    while True:
        for txid, hosts in want.items():
            for h in hosts:
                if not hosts[h]:
                    try:
                        hosts[h] = db.transaction_exists(h, txid)
                    except Exception:
                        pass
        if all(all(v.values()) for v in want.values()) or time.time() > deadline:
            return want
        time.sleep(2)


# Evidence a case adds itself (e.g. the fullnode comparison), keyed by Test ID.
EXTRA = {}


def attach(test_id, key, data):
    """Let a case put its own readings into the evidence file."""
    EXTRA.setdefault(test_id, {})[key] = data


def role_labels(unit):
    """{host: "sender" / "receiver 2" / "quorum" ...} - how the report names
    machines, instead of IP addresses."""
    labels = {}
    for role, entries in (("quorum", unit.quorums), ("sender", unit.senders),
                          ("receiver", unit.receivers)):
        for i, e in enumerate(entries):
            name = role if len(entries) == 1 else "{} {}".format(role, i + 1)
            labels.setdefault(e["host"], name)
    return labels


def plain(text, labels):
    """Replace a participant's IP address in a case's own wording with the role
    it played. Hosts that are not participants keep their address: a fleet-wide
    case (GEN-IN-15 and the like) has no roles, and the host IS the finding."""
    text = text or ""
    for host in sorted(labels, key=len, reverse=True):
        # "quorum 192.168.1.5" -> "the quorum", not "quorum the quorum"
        text = re.sub(r"\b(?:the\s+)?(?:quorum|sender|receiver)s?\s+" + re.escape(host) + r"\b",
                      "the " + labels[host], text)
        text = re.sub(re.escape(host) + r"\b", "the " + labels[host], text)
    return text


# Refusals with a known cause in the product, recognised by their text so a
# case's note says what they are instead of each run rediscovering them.
# (marker in the node's message, short label for the Note column, full
# explanation for the end-of-run summary). Markers are the start of a phrase,
# so a message cut short still counts.
KNOWN_PRODUCT_BUGS = (
    ("failed to get quorum DID for t",
     "ex-quorum cannot spend tokens it pledged (checks.go:308)",
     "the sender was a quorum earlier and is spending a token it pledged then, now "
     "unpledged and Free; core looks that token up in the CURRENT transaction's "
     "pledges instead of the pledge transaction (core/consensus/checks.go:308, "
     "FindDIDByTokenID(txnInfo.Quorums ...)), so it always refuses"),
    ("ValidateMinterAllowlist: whole-token genesis fetch failed",
     "minter allowlist on second-hand part tokens (fix not in lab build)",
     "the quorum asks the sender for the whole token's genesis, which a second-hand "
     "holder of a part token does not have; fixed by f890aa01, 257a9e9d and 4601dd04, "
     "none of which is in the build the lab runs"),
    ("invalid epoch",
     "zero clock tolerance (checks.go:61)",
     "the quorum rejects a transaction stamped even a second ahead of its own clock "
     "(core/consensus/checks.go:61, Epoch > time.Now()), so any clock difference "
     "between sender and quorum refuses transfers"),
    ("deadlock detected (SQLSTATE 40P01)",
     "token_denom deadlock on concurrent deploys",
     "concurrent contract deploys from one wallet deadlock in Postgres while updating "
     "the denomination counter (PersistPreConsensus / post-consensus token_denom upsert)"),
    ("transaction amount exceeds 3 decimal places",
     "float sum of contract values (parts.go:55)",
     "a multi-contract request is checked on the float sum of its values "
     "(core/parts/parts.go:55-58): 0.1 + 0.2 = 0.30000000000000004 fails the 3dp check"),
)
KNOWN_BUG_EXPLANATION = dict((label, words) for _m, label, words in KNOWN_PRODUCT_BUGS)


def known_bug_refusals(ops):
    """{short label: count} for refused operations whose message is a known bug."""
    out = {}
    for o in ops:
        if o.get("kind") != "tx" or o.get("status"):
            continue
        for marker, label, _words in KNOWN_PRODUCT_BUGS:
            if marker in (o.get("message") or ""):
                out[label] = out.get(label, 0) + 1
                break
    return out


def explain_fullnode_reason(reason):
    """A rejection reason in a few plain words."""
    r = reason or ""
    if "chain mismatch" in r or "TokenChainIntigrityCheck" in r:
        return "prev-txn mismatch, fullnode behind"
    if "marked unpledged but not found" in r:
        return "unpledged token not in its pledge txn"
    if "signature" in r.lower():
        return "signature check failed"
    if "minter" in r.lower() or "allowlist" in r.lower():
        return "minter allowlist rejected the token"
    return re.sub(r"\s+", " ", r.replace("failed to validate transaction:", "")).strip()[:120]


_INVARIANT_PLAIN = (
    ("NO chain row", "a new token has no history record (tokenchain row)"),
    ("not a legal denomination", "a token holds a value no real token can have"),
    ("value changed in place", "a token's value was changed in place"),
    ("parent is still Free", "a token was split but its parent is still spendable - the same value exists twice"),
    ("token_denom moved", "the denomination counter (token_denom) disagrees with the actual tokens"),
)


def _plain_invariant(problem):
    for key, words in _INVARIANT_PLAIN:
        if key in problem:
            return words
    return problem[:120]


def finish(tid, unit, state, result):
    """Run the checks. Returns (result, evidence). The note it writes is plain
    language; the exact readings go into the evidence file."""
    parts = participants(unit)
    dids = set(e["did"] for _r, e in parts)
    host_of = dict((e["did"], e["host"]) for _r, e in parts)
    labels = role_labels(unit)
    role_of = dict((d, labels.get(h, h)) for d, h in host_of.items())
    ops = [o for o in rc.OPS[state["op_index"]:]
           if o["initiator"] in dids or o["owner"] in dids]

    evidence = {
        "case": tid, "unit": unit.name,
        "participants": [{"role": labels.get(e["host"], r), "host": e["host"], "did": e["did"]}
                         for r, e in parts],
        "operations": ops, "wallets": {}, "checks": [],
    }
    checks = evidence["checks"]
    passed, actual, note = result
    actual, note = plain(actual, labels), plain(note, labels)

    def done(extra_note=""):
        evidence.update(EXTRA.pop(tid, {}))
        n = note + (" | " if note and extra_note else "") + extra_note
        return (passed, actual, n), evidence

    if not parts:
        checks.append({"check": "db", "ok": None,
                       "detail": "no participants of its own - the case reads the fleet itself"})
        return done()

    found = _wait_for_rows(ops, host_of)
    after = dict((e["did"], _capture(e["host"], e["did"])) for _r, e in parts)

    unreadable = sorted(set(role_of[d] for d in dids
                            if state["before"].get(d) is None or after.get(d) is None))
    if unreadable:
        checks.append({"check": "db", "ok": None,
                       "detail": "could not read the database of the {} - the database "
                                 "checks are incomplete (a lab problem, not a verdict)".format(
                                     ", ".join(unreadable))})

    tx = [o for o in ops if o["kind"] == "tx"]
    tx_ok = [o for o in tx if o["status"]]
    funding = [o for o in tx_ok if o["initiator"] not in dids]
    host_role = dict((h, labels.get(h, h)) for h in set(host_of.values()))
    known = known_bug_refusals(tx)
    if known:
        evidence["known_bug_refusals"] = known

    # persisted
    no_id = [o for o in tx_ok if not o["txid"]]
    missing = [(txid, [host_role.get(h, h) for h, ok in hosts.items() if not ok])
               for txid, hosts in found.items() if not all(hosts.values())]
    if tx_ok:
        if not missing and not no_id:
            detail = "every transaction that went through is recorded by both sides"
        else:
            bits = ["{} transaction(s) that went through are missing from the {}'s records".format(
                        len(missing), " and ".join(sorted(set(r for _t, rs in missing for r in rs))))
                    ] if missing else []
            if no_id:
                bits.append("{} reported success but gave no transaction ID".format(len(no_id)))
            detail = "; ".join(bits)
        checks.append({"check": "persisted", "ok": not missing and not no_id, "detail": detail,
                       "missing": [t for t, _r in missing]})

    readable = [d for d in dids if state["before"].get(d) and after.get(d)]
    for d in readable:
        b, a = state["before"][d], after[d]
        evidence["wallets"][d] = {
            "role": role_of[d], "host": host_of[d],
            "before": dict((k, round(v, 3)) for k, v in b["totals"].items()),
            "after": dict((k, round(v, 3)) for k, v in a["totals"].items()),
            "changes": db.describe_diff(db.diff(b, a)),
        }

    if readable and not unreadable:
        # conserved
        came_in = sum(o["rbt"] for o in tx_ok if o["initiator"] not in dids and o["owner"] in dids)
        went_out = sum(o["rbt"] for o in tx_ok if o["initiator"] in dids
                       and o["owner"] and o["owner"] not in dids)
        moved = sum(_held(after[d]) - _held(state["before"][d]) for d in readable)
        expected = came_in - went_out
        tol = 0.002 + 0.001 * len(tx_ok)
        ok = abs(moved - expected) <= tol
        if ok:
            detail = ("value balanced ({:+.3f} RBT came in from the faucet)".format(came_in)
                      if came_in else "value balanced")
        else:
            detail = ("value does NOT balance: the participants' RBT changed by {:+.3f}, but "
                      "{:+.3f} should have ({:.3f} came in, {:.3f} went out)".format(
                          moved, expected, came_in, went_out))
        checks.append({"check": "conserved", "ok": ok, "detail": detail,
                       "moved": round(moved, 3), "expected": round(expected, 3)})

        # no_locks
        leaked = dict((d, after[d]["totals"].get("locked", 0) - state["before"][d]["totals"].get("locked", 0))
                      for d in readable)
        leaked = dict((d, v) for d, v in leaked.items() if v > 0.0015)
        checks.append({
            "check": "no_locks", "ok": not leaked,
            "detail": "nothing left locked" if not leaked else "; ".join(
                "{:.3f} RBT left LOCKED on the {}".format(v, role_of[d]) for d, v in leaked.items())})

        # rows_valid
        problems = []
        for d in readable:
            for p_ in db.check_invariants(db.diff(state["before"][d], after[d]), after[d]):
                problems.append((role_of[d], p_))
        plain_problems = sorted(set("{} (on the {})".format(_plain_invariant(p_), r)
                                    for r, p_ in problems))
        checks.append({
            "check": "rows_valid", "ok": not problems,
            "detail": "wallet records consistent" if not problems else "; ".join(plain_problems[:3]),
            "raw": [p_ for _r, p_ in problems][:20]})

    fullnode = _fullnode_check(tx_ok)
    if fullnode:
        checks.append(fullnode)

    failed = [c for c in checks if c["ok"] is False and c["check"] in VERDICT_CHECKS]

    # One plain sentence, in the order a reader asks: what happened, did it
    # stick, does the money add up, anything stuck, what the fullnode made of it.
    words = []
    if tx:
        refused = len(tx) - len(tx_ok)
        words.append("{} transaction(s){}: {}".format(
            len(tx),
            " incl. {} faucet top-up(s)".format(len(funding)) if funding else "",
            "all went through" if not refused else
            "{} went through, {} refused".format(len(tx_ok), refused)))
        for why, n in known.items():
            words.append("KNOWN PRODUCT BUG behind {} of the refusals: {}".format(n, why))
    for c in checks:
        if c["check"] in ("persisted", "conserved", "no_locks", "rows_valid", "fullnode", "db"):
            words.append(c["detail"])
    # The full sentence goes to the evidence file; the report shows the short
    # DB / Fullnode columns (short_db, short_fullnode) and keeps Note for the
    # case's own words.
    evidence["db_summary"] = "Database: " + "; ".join(words) + "." if words else ""
    if failed and passed is True:
        passed = False
        actual = (actual or "") + " - but the database check failed: " + \
            "; ".join(c["detail"] for c in failed)
    return done()


def fullnode_outcome(txids, wait=None):
    """What the fullnode did with these transactions: (accepted set,
    {txid: reason} rejected, [txid] not seen yet). Waits up to `wait` seconds
    (default FULLNODE_WAIT) for every one to be accepted or rejected.
    Raises when the fullnode database cannot be read."""
    ids = [t for t in txids if t]
    accepted, rejected = set(), {}
    deadline = time.time() + (FULLNODE_WAIT if wait is None else wait)
    while True:
        todo = [t for t in ids if t not in accepted and t not in rejected]
        if todo:
            for (tid,) in db.query(FULLNODE_HOST,
                                   "SELECT id FROM fullnode_transactions WHERE id = ANY(%s)",
                                   (todo,)):
                accepted.add(tid)
            for tid, reason in db.query(
                    FULLNODE_HOST,
                    "SELECT transaction->>'ID', reason FROM fullnode_invalid_transactions "
                    "WHERE transaction->>'ID' = ANY(%s)", (todo,)):
                rejected[tid] = reason
        if len(accepted) + len(rejected) >= len(ids) or time.time() > deadline:
            break
        time.sleep(3)
    return accepted, rejected, [t for t in ids if t not in accepted and t not in rejected]


def _fullnode_check(tx_ok):
    """Where each successful transaction ended up on the fullnode."""
    ids = [o["txid"] for o in tx_ok if o["txid"]]
    if not ids or not FULLNODE_HOST:
        return None
    try:
        accepted, rejected, unseen_ids = fullnode_outcome(ids)
    except Exception as e:
        return {"check": "fullnode", "ok": None,
                "detail": "fullnode database not readable: {}: {}".format(type(e).__name__, e)}
    unseen = len(unseen_ids)
    detail = "fullnode accepted {} of {}".format(len(accepted), len(ids))
    if rejected:
        kinds = sorted(set(explain_fullnode_reason(r) for r in rejected.values()))
        detail += ", REJECTED {} ({})".format(len(rejected), "; ".join(kinds))
    if unseen:
        detail += ", {} not yet seen by it after {}s".format(unseen, FULLNODE_WAIT)
    status = dict((t, "accepted") for t in accepted)
    status.update((t, "rejected") for t in rejected)
    status.update((t, "not seen") for t in unseen_ids)
    return {"check": "fullnode", "ok": (not rejected) if not unseen else (False if rejected else None),
            "detail": detail, "rejected": rejected, "status": status}


# ---------------------------------------------------------------------------
# The same case with and without delay
#
# Every case that makes transfers is run twice by the runner: first WITHOUT
# delay (pacing off for its DIDs - the product's raw behaviour), then WITH
# delay (each DID waits for the fullnode to process its previous transaction).
# The fullnode's verdict on each transfer is compared between the two runs.
# Transfers are matched by who sent to whom, the amount, and the order they
# came in (faucet top-ups are left out - the two runs need different amounts).
# ---------------------------------------------------------------------------

BUCKETS = (
    ("both", "accepted both ways"),
    ("only_with_delay", "accepted only with delay - the fullnode falls behind when the token "
                        "moves again too soon"),
    ("only_without_delay", "accepted only without delay - unexpected"),
    ("neither", "rejected both ways - not a timing problem"),
    ("not_seen", "not seen by the fullnode in one or both runs"),
    ("unmatched", "made in only one of the two runs"),
)


def made_transfers(evidence):
    return any(o["kind"] == "tx" and o["status"] and o["txid"]
               for o in evidence.get("operations", []))


def _keyed_transfers(evidence):
    """{(from-role, to-role, amount, n): op} for the case's own transfers."""
    roles = dict((p_["did"], p_["role"]) for p_ in evidence.get("participants", []))
    seen, out = {}, {}
    for o in evidence.get("operations", []):
        if o["kind"] != "tx" or not o["status"] or not o["txid"] or o["initiator"] not in roles:
            continue
        base = (roles[o["initiator"]], roles.get(o["owner"], "outside DID"), round(o["rbt"], 3))
        seen[base] = seen.get(base, 0) + 1
        out[base + (seen[base],)] = o
    return out


def _fullnode_status(evidence):
    status, reasons = {}, {}
    for c in evidence.get("checks", []):
        if c.get("check") == "fullnode":
            status.update(c.get("status") or {})
            reasons.update(c.get("rejected") or {})
    return status, reasons


def compare_runs(without, with_):
    """Match the two runs' transfers and bucket the fullnode's verdicts.
    Returns (rows, counts, text)."""
    a, b = _keyed_transfers(without), _keyed_transfers(with_)
    sa, ra = _fullnode_status(without)
    sb, rb = _fullnode_status(with_)
    rows, counts = [], dict((k, 0) for k, _w in BUCKETS)
    neither_reasons = set()
    for key in list(a) + [k for k in b if k not in a]:
        oa, ob = a.get(key), b.get(key)
        va = sa.get(oa["txid"], "not seen") if oa else None
        vb = sb.get(ob["txid"], "not seen") if ob else None
        if oa is None or ob is None:
            bucket = "unmatched"
        elif va == "accepted" and vb == "accepted":
            bucket = "both"
        elif vb == "accepted" and va == "rejected":
            bucket = "only_with_delay"
        elif va == "accepted" and vb == "rejected":
            bucket = "only_without_delay"
        elif va == "rejected" and vb == "rejected":
            bucket = "neither"
            neither_reasons.add(explain_fullnode_reason(rb.get(ob["txid"], "")))
        else:
            bucket = "not_seen"
        counts[bucket] += 1
        rows.append({
            "transfer": "{} -> {}, {} RBT, #{}".format(*key),
            "without_delay": {"txid": oa["txid"] if oa else None, "fullnode": va,
                              "reason": ra.get(oa["txid"]) if oa else None},
            "with_delay": {"txid": ob["txid"] if ob else None, "fullnode": vb,
                           "reason": rb.get(ob["txid"]) if ob else None},
            "outcome": dict(BUCKETS)[bucket],
        })
    total = len(rows)
    parts = ["{} {}".format(counts[k], w) for k, w in BUCKETS if counts[k]]
    text = "Fullnode, same case without and with delay ({} transfer(s)): {}".format(
        total, "; ".join(parts) if parts else "nothing to compare")
    if neither_reasons:
        text += " (reason: {})".format("; ".join(sorted(neither_reasons)))
    return rows, counts, text


# ---------------------------------------------------------------------------
# Short report columns
# ---------------------------------------------------------------------------

def _runs(evidence):
    """[(mode label, run evidence)] - one run, or the two of a combined case."""
    runs = evidence.get("runs")
    if runs:
        return [("w/o delay" if r.get("mode") == "without delay" else "with delay", r)
                for r in runs]
    return [("", evidence)]


def short_db(evidence):
    """The DB column: "ok", or which checks failed, per mode when they differ."""
    out = []
    for label, run in _runs(evidence):
        checks = [c for c in run.get("checks", []) if c.get("check") != "fullnode"]
        bad = [c for c in checks if c.get("ok") is False]
        unread = [c for c in checks if c.get("ok") is None and c.get("check") == "db"
                  and "no participants" not in c.get("detail", "")]
        if not checks or all(c.get("check") == "db" and "no participants" in c.get("detail", "")
                             for c in checks):
            text = "-"
        elif bad:
            text = "; ".join("{}: {}".format(c["check"], c["detail"]) for c in bad)
        elif unread:
            text = "incomplete: " + unread[0]["detail"]
        else:
            text = "ok"
        out.append((label, text))
    if len(set(t for _l, t in out)) == 1:
        return out[0][1]
    return " | ".join("{}: {}".format(l, t) for l, t in out)


def short_fullnode(evidence):
    """The Fullnode column: accepted / rejected (why) / not seen, per mode."""
    out = []
    for label, run in _runs(evidence):
        fn = [c for c in run.get("checks", []) if c.get("check") == "fullnode"]
        if not fn:
            continue
        c = fn[0]
        st = c.get("status") or {}
        if not st:
            out.append((label, c.get("detail", "")))
            continue
        acc = sum(1 for v in st.values() if v == "accepted")
        rej = c.get("rejected") or {}
        unseen = sum(1 for v in st.values() if v == "not seen")
        bits = ["{}/{} accepted".format(acc, len(st))]
        if rej:
            bits.append("{} rejected ({})".format(
                len(rej), "; ".join(sorted(set(explain_fullnode_reason(r) for r in rej.values())))))
        if unseen:
            bits.append("{} not seen in {}s".format(unseen, FULLNODE_WAIT))
        out.append((label, ", ".join(bits)))
    if not out:
        return "-"
    if len(out) == 1 or len(set(t for _l, t in out)) == 1:
        text = out[0][1]
    else:
        text = " | ".join("{}: {}".format(l, t) for l, t in out)
    counts = (evidence.get("fullnode_comparison") or {}).get("counts") or {}
    if counts.get("only_with_delay"):
        text += " | {} accepted only WITH delay (fullnode falls behind)".format(
            counts["only_with_delay"])
    if counts.get("only_without_delay"):
        text += " | {} accepted only without delay".format(counts["only_without_delay"])
    # A "with delay" result only means something for transfers that really
    # waited for the fullnode.
    for _label, run in _runs(evidence):
        if run.get("mode") != "with delay":
            continue
        paced_ops = [o for o in run.get("operations", []) if o.get("pacing")]
        unpaced = [o for o in paced_ops if o["pacing"] in ("timed out", "gave up")]
        if unpaced:
            text += " | {} of {} 'with delay' transfers NOT really paced (fullnode >{}s behind)".format(
                len(unpaced), len(paced_ops), rc.PACE_TIMEOUT)
    return text


def short_known_bugs(evidence):
    """One line for refusals with a known product cause, or ""."""
    counts = {}
    for _mode, run in _runs(evidence):
        for label, n in (run.get("known_bug_refusals") or {}).items():
            counts[label] = counts.get(label, 0) + n
    if not counts:
        return ""
    return "KNOWN PRODUCT BUG behind {} refusal(s): {} - not a lab error, not a capacity limit.".format(
        sum(counts.values()),
        "; ".join("{}x {}".format(n, label) for label, n in
                  sorted(counts.items(), key=lambda kv: -kv[1])))


def _status(result):
    passed = result[0]
    if isinstance(passed, str) and passed == "SKIP":
        return "SKIP"
    return "PASS" if passed is True else "FAIL"


def combine(tid, r_without, e_without, r_with, e_with):
    """One report row from the two runs. The case passes only if it passes
    both ways; the fullnode comparison is reported alongside."""
    rows, counts, text = compare_runs(e_without, e_with)
    s1, s2 = _status(r_without), _status(r_with)
    if s1 == "SKIP" and s2 == "SKIP":
        passed = r_without[0]
    else:
        passed = s1 != "FAIL" and s2 != "FAIL"
    actual = "Without delay: {} {}. With delay: {} {}.".format(
        s1, r_without[1] or "", s2, r_with[1] or "")
    # The case's own words only; the fullnode comparison has its own column.
    n1, n2 = (r_without[2] or "").strip(), (r_with[2] or "").strip()
    if n1 == n2:
        note = n1
    else:
        note = " | ".join(p_ for p_ in ("Without delay: " + n1 if n1 else "",
                                         "With delay: " + n2 if n2 else "") if p_)
    for e, mode in ((e_without, "without delay"), (e_with, "with delay")):
        e["mode"] = mode
        for c in e.get("checks", []):
            c["mode"] = mode
    evidence = {
        "case": tid, "unit": e_without.get("unit"),
        "participants": e_without.get("participants", []),
        "fullnode_comparison": {"counts": counts, "transfers": rows, "summary": text},
        "runs": [e_without, e_with],
        "checks": e_without.get("checks", []) + e_with.get("checks", []),
    }
    return (passed, actual, note), evidence
