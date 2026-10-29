"""steps.py - a case as a list of defined steps, and what each step's outcome
means.

Every case module defines STEPS = {case: [(id, title, what the lab does, what
Rubix must do), ...]} - the case book (case_book.py prints it) - and runs them
through a CaseRun, one step at a time. A step:

  1. checks its preconditions. One that does not hold is the LAB's problem -
     the step is NOT RUN and says why; Rubix is never blamed for a bad set-up.
     A participant node that cannot receive (fleet_health: no IPFS p2p
     listener) is one.
  2. reads the participants' wallets (RBT by status, Locked FT/NFT/SC, the
     token_denom counter) and starts watching their machines.
  3. runs the action, recording every operation: accepted or refused, the
     node's own reason, how long it took.
  4. after the action: waits for every accepted transfer to land on its
     receiver, gives locks time to clear, reads the wallets again, checks the
     nodes still answer, have not restarted and can still receive, and - if
     anything was refused - runs one ordinary operation again (the recovery
     probe).

Its outcome is one of:

  PASS      Rubix did what the step expects and every check after it holds.
  LIMIT     Rubix could not take all of it and REFUSED THE REST GENTLY: every
            refusal named its reason and came back within the budget, and
            afterwards nothing was left Locked, no value moved that should not
            have, no counter drifted, no node restarted or went deaf, and the
            recovery probe went through. This is what the lab wants at the
            software's limit; the step records where the limit is.
  DAMAGE    Rubix failed or refused UNGENTLY: a refusal with no usable reason
            (EOF, a dropped connection, a raw database error, a panic), one
            that came after the budget or never, RBT/FT left Locked, a counter
            left wrong, a node that restarted or went deaf, or no recovery.
  WRONG     Rubix accepted what it should have refused, or the result is
            wrong: value created or lost, an accepted transfer that never
            reached its receiver, two winners of a one-token race.
  HARDWARE  the step failed or hit its limit while a machine it ran on was out
            of CPU, memory, swap or disk (fleet_health.Watch). That limit may
            be the machine's, not Rubix's: inconclusive - run it again with
            less on each machine.
  NOT RUN   a precondition did not hold.

A case FAILS on any WRONG or DAMAGE step, is INCONCLUSIVE (reported as SKIP) on
a HARDWARE step, NOT RUN (SKIP) on a required step that could not run, and
PASSES otherwise, with the limits its LIMIT steps found.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import rubix_client as rc
import db_client as db
import case_evidence
import fleet_health

PASS, LIMIT, DAMAGE, WRONG, HARDWARE, NOT_RUN = "PASS", "LIMIT", "DAMAGE", "WRONG", "HARDWARE", "NOT RUN"
SKIP = "SKIP"

REFUSE_BUDGET = 60      # seconds: a refusal later than this is not gentle
SETTLE_MAX = 45         # seconds locks get to clear after a step
DELIVERY_WAIT = 60      # seconds an accepted transfer gets to land on its receiver
TOL = 0.0015
HELD = (db.FREE, db.LOCKED, db.COMMITTED, db.PLEDGED, db.QUORUM_PLEDGED, db.BURNT_FOR_FT)

# A reason that tells the user nothing about what Rubix decided: the
# connection dropped, the database or the Go runtime failed underneath, or
# nothing came back. Matched in the node's own reason (case_helpers.node_reason).
UNCLEAR = ("EOF", "RemoteDisconnected", "Remote end closed", "connection reset",
           "Connection refused", "Broken pipe", "timed out", "timeout", "request failed:",
           "SQLSTATE", "deadlock detected", "duplicate key", "violates ", "runtime error",
           "nil pointer", "invalid memory address", "panic", "context deadline",
           "context canceled", ": ERRO", "500 Internal Server Error")


def node_reason(msg):
    """The node's own reason, long enough to keep what follows the first
    sentence: a token_denom deadlock names "deadlock detected" late in it."""
    import case_helpers as ch
    return ch.node_reason(msg, 1000)


def reason_kind(reason, secs, budget=REFUSE_BUDGET):
    """stated / unclear / late, for one refusal."""
    if secs is not None and secs >= budget:
        return "late"
    text = (reason or "").strip()
    if not text:
        return "unclear"
    low = text.lower()
    if any(m.lower() in low for m in UNCLEAR):
        return "unclear"
    return "stated"


def known_label(reason):
    for marker, label, _words in case_evidence.KNOWN_PRODUCT_BUGS:
        if marker in (reason or ""):
            return label
    return ""


def _short(host):
    """".130:20400" for 192.168.1.130:20400 - how the notes name a node."""
    return "." + str(host).split(".")[-1]


def _group(texts, top=3):
    counts = {}
    for t in texts:
        t = (t or "(no reason given)")[:140]
        counts[t] = counts.get(t, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return "; ".join("{}x {}".format(n, t) for t, n in ranked[:top])


def _pct(values, p):
    vals = sorted(values)
    if not vals:
        return 0.0
    k = max(0, min(len(vals) - 1, int(round(p / 100.0 * len(vals) + 0.5)) - 1))
    return vals[k]


# ---------------------------------------------------------------------------
# Wallet readings - one connection per NODE for all of its DIDs in the step
# ---------------------------------------------------------------------------

def snapshot(entries):
    """{did: {"held", "locked", "ft_locked", "nft_locked", "drift"}} for every
    entry's DID; a DID whose node could not be read is missing."""
    by_host = {}
    for e in entries:
        by_host.setdefault(e["host"], set()).add(e["did"])

    def one(item):
        host, dids = item
        dids = sorted(dids)
        try:
            bal, counters, frees = db.query_snapshot(host, [
                ("SELECT did, token_type, token_status, COALESCE(SUM(token_value), 0), COUNT(*) "
                 "FROM tokens WHERE did = ANY(%s) GROUP BY did, token_type, token_status", (dids,)),
                ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
                ("SELECT did, token_value, COUNT(*) FROM tokens WHERE did = ANY(%s) AND "
                 "token_type = %s AND token_status = %s GROUP BY did, token_value",
                 (dids, db.TYPE_RBT, db.FREE)),
            ])
        except Exception:
            return {}
        out = dict((d, {"held": 0.0, "locked": 0.0, "ft_locked": 0, "nft_locked": 0,
                        "free": 0.0, "pledged": 0.0, "drift": {}}) for d in dids)
        for did, ttype, status, value, count in bal:
            w = out.get(did)
            if w is None:
                continue
            ttype, status, value, count = int(ttype), int(status), float(value), int(count)
            if ttype == db.TYPE_RBT:
                if status in HELD:
                    w["held"] += value
                if status == db.LOCKED:
                    w["locked"] += value
                if status == db.FREE:
                    w["free"] += value
                if status in (db.PLEDGED, db.QUORUM_PLEDGED):
                    w["pledged"] += value
            elif status == db.LOCKED:
                if ttype == db.TYPE_FT:
                    w["ft_locked"] += count
                else:
                    w["nft_locked"] += count
        counted, actual = {}, {}
        for did, denom, count in counters:
            counted.setdefault(did, {})[round(float(denom), 3)] = int(count)
        for did, value, count in frees:
            actual.setdefault(did, {})[round(float(value), 3)] = int(count)
        for did in dids:
            c, a = counted.get(did, {}), actual.get(did, {})
            out[did]["drift"] = dict((d, (c.get(d, 0), a.get(d, 0))) for d in set(c) | set(a)
                                     if c.get(d, 0) != a.get(d, 0))
        return out

    merged = {}
    items = list(by_host.items())
    if items:
        with ThreadPoolExecutor(max_workers=min(32, len(items))) as ex:
            for part in ex.map(one, items):
                merged.update(part or {})
    return merged


def _new_locks(before, after):
    """[(did, rbt, ft, nft)] Locked that was not Locked before."""
    out = []
    for d, a in after.items():
        b = before.get(d)
        if not b:
            continue
        rbt = a["locked"] - b["locked"]
        ft = a["ft_locked"] - b["ft_locked"]
        nft = a["nft_locked"] - b["nft_locked"]
        if rbt > TOL or ft > 0 or nft > 0:
            out.append((d, round(rbt, 3), ft, nft))
    return out


# ---------------------------------------------------------------------------
# Preconditions a step can require - each returns (ok, why)
# ---------------------------------------------------------------------------

def wired(host, quorum_did, port=None):
    """The node's quorum list is exactly [quorum_did] - a transfer is signed by
    the FIRST quorum in the sending node's list (core/transaction.go)."""
    ok, quorums, note = rc.get_quorums(host, port or rc.DEFAULT_PORT)
    qs = [q.get("address", q) if isinstance(q, dict) else q for q in (quorums or [])]
    qs = [str(q).split(".")[-1] for q in qs]
    if not ok:
        return False, "could not read {}'s quorum list: {}".format(_short(host), note)
    if not qs or qs[0] != quorum_did:
        return False, "{} signs with {} instead of the step's quorum".format(
            _short(host), (qs[0][:12] + "...") if qs else "no quorum")
    return True, ""


def holds(entry, amount):
    ok, detail, _n = rc.get_rbt_balance_detail(entry["host"], entry["did"])
    have = float(detail["balance"]) if ok and detail else 0.0
    if have + TOL < amount:
        return False, "{} holds {:.3f} RBT free, the step needs {:.3f}".format(
            _short(entry["host"]), have, amount)
    return True, ""


def quorum_ready(entry, amount, port=None):
    """Set up as a quorum (in memory, lost on restart: core/quorum_initiator.go)
    and holding enough Free RBT to pledge `amount`."""
    ok, msg = rc.quorum_setup(entry["host"], entry["did"], port or rc.DEFAULT_PORT)
    if not ok:
        return False, "quorum {} could not be set up: {}".format(_short(entry["host"]), msg)
    return holds(entry, amount)


# ---------------------------------------------------------------------------
# A step and a case
# ---------------------------------------------------------------------------

class Step(object):
    """What one step did. Cases add operations and their own checks to it."""

    def __init__(self, case, sid, title, expect):
        self.case, self.sid, self.title, self.expect = case, sid, title, expect
        self.ops = []
        self.checks = []
        self.notes = []
        self.data = {}
        self.not_run = ""
        self.outcome, self.summary = None, ""
        self.record = {}
        self.seconds = 0.0

    def add(self, results, jobs=None, set_aside=()):
        """Burst results (case_helpers.burst), with the jobs they ran."""
        for i, r in enumerate(results or []):
            job = (jobs or [{}] * len(results))[i] if jobs else {}
            self.op(r.get("ok"), r.get("msg", ""), r.get("secs"), r.get("txid"),
                    to=job.get("to"), set_aside=set_aside)
        return results

    def op(self, ok, msg="", secs=None, txid=None, to=None, set_aside=()):
        reason = "" if ok else node_reason(msg)
        self.ops.append({"ok": bool(ok), "reason": reason, "secs": secs, "txid": txid, "to": to,
                         "aside": (not ok) and any(m in reason for m in set_aside)})

    def check(self, ok, detail):
        """A case's own assertion about the result; a False one makes the step WRONG."""
        self.checks.append((bool(ok), detail))
        return ok

    def note(self, text):
        self.notes.append(text)

    def skip(self, why):
        self.not_run = why

    @property
    def accepted(self):
        return [o for o in self.ops if o["ok"]]

    @property
    def refused(self):
        return [o for o in self.ops if not o["ok"] and not o["aside"]]


class CaseRun(object):
    """Runs one case's steps and turns them into the case's verdict."""

    def __init__(self, ctx, case, definitions, budget=REFUSE_BUDGET):
        self.ctx, self.case, self.budget = ctx, case, budget
        self.defs = dict((d[0], d) for d in definitions)
        self.steps = []
        self.host_of = {}
        for e in (list(getattr(ctx, "fleet", None) or []) + list(ctx.senders or []) +
                  list(ctx.receivers or []) + list(getattr(ctx, "quorum_hosts", None) or [])):
            self.host_of[e["did"]] = e["host"]
            for d in e.get("dids") or ():
                self.host_of.setdefault(d, e["host"])

    def step(self, sid, body, nodes=(), label="", require=(), probe=None, budget=None,
             expect_refusal=False, must_accept=False, conserve=True, deliver=True, watch=True,
             required=True, restarts=(), probe_always=False):
        """Run one defined step. `body(st)` does the action; `nodes` are the
        entries (host, did) whose wallets and nodes the step checks; `require`
        is a list of (ok, why) pairs or callables returning one; `probe()` is
        an ordinary operation run after refusals, returning (ok, msg, result).

        A capacity step (the default) may meet a limit: a gentle refusal there
        is LIMIT. A step whose operations are all valid and must go through -
        must_accept - has no such excuse: a refusal is WRONG, with its reason.
        expect_refusal is the opposite: an acceptance is WRONG. `restarts` are
        nodes the step itself stops and starts (FAIL-01), not counted as crashes."""
        if sid not in self.defs:
            raise KeyError("{} {} is not in the case's STEPS".format(self.case, sid))
        d = self.defs[sid]
        st = Step(self.case, "{} {}".format(sid, label).strip(), d[1], d[3])
        st.required = required
        budget = budget or self.budget
        for req in require:
            ok, why = req() if callable(req) else req
            if not ok:
                st.not_run = why
                return self._done(st, {})
        entries, seen = [], set()
        for e in nodes:
            if e and e["did"] not in seen:
                seen.add(e["did"])
                entries.append(e)
        hosts = sorted(set(e["host"] for e in entries))
        w = fleet_health.Watch(hosts).start() if (watch and hosts) else None
        deaf = []
        if w:
            for m, rows in w.samples.items():
                if rows:
                    for slot, index in w.slots[m]:
                        h = "{}:{}".format(m, 20000 + index)
                        if h in hosts and rows[0].get("ear.{}".format(slot)) == "0":
                            deaf.append(h)
        if deaf:
            st.not_run = "participant node(s) cannot receive - no IPFS p2p listener: {} " \
                         "(restart that machine with restart-nodes.sh --force)".format(
                             ", ".join(_short(h) for h in deaf))
            w.stop()
            return self._done(st, {})
        before = snapshot(entries)
        op_index = len(rc.OPS)
        t0 = time.time()
        try:
            body(st)
        except Exception as e:
            st.not_run = st.not_run or "the lab's own code failed: {}: {}".format(type(e).__name__, e)
        st.seconds = round(time.time() - t0, 1)
        machines = w.stop() if w else {}
        if st.not_run:
            return self._done(st, machines)
        undelivered = self._undelivered(st) if deliver else []
        after = self._settle(entries, before)
        flows = [o for o in rc.OPS[op_index:] if o.get("kind") == "tx" and o.get("status")]
        silent = self._silent(hosts)
        recovered = None
        if probe and (probe_always or st.refused or any(not ok for ok, _d in st.checks)):
            t1 = time.time()
            try:
                ok, msg, _r = probe()
            except Exception as e:
                ok, msg = False, "{}: {}".format(type(e).__name__, e)
            recovered = (ok, node_reason(msg), round(time.time() - t1, 1))
        for h in restarts:
            m, idx = h.rsplit(":", 1)[0], int(h.rsplit(":", 1)[1]) - 20000
            for s, i in (w.slots.get(m, []) if w else []):
                if i == idx and s in machines.get(m, {}).get("restarted", []):
                    machines[m]["restarted"].remove(s)
        self._classify(st, entries, before, after, flows, undelivered, machines, silent,
                       recovered, budget, expect_refusal, conserve, must_accept)
        return self._done(st, machines)

    def _undelivered(self, st):
        """Accepted operations whose transaction never reached the receiver's node."""
        want = {}
        for o in st.accepted:
            host = self.host_of.get(o["to"]) if o["to"] else None
            if o["txid"] and host:
                want.setdefault(host, set()).add(o["txid"])
        deadline = time.time() + DELIVERY_WAIT
        while want and time.time() < deadline:
            for host in list(want):
                try:
                    want[host] -= set(db.transactions_present(host, list(want[host])))
                except Exception:
                    pass
                if not want[host]:
                    del want[host]
            if want:
                time.sleep(2)
        return [(h, t) for h, ids in want.items() for t in ids]

    def _settle(self, entries, before):
        """The wallets after the step, once locks it made have had time to clear."""
        time.sleep(3)
        deadline = time.time() + SETTLE_MAX
        after = snapshot(entries)
        while _new_locks(before, after) and time.time() < deadline:
            time.sleep(5)
            after = snapshot(entries)
        return after

    def _silent(self, hosts):
        """Participant nodes that no longer answer their API."""
        def answers(h):
            ok, _p = rc.http_json("GET", rc.base_url(h) + rc.EP_PEER_ID, 5)
            return h, ok
        if not hosts:
            return []
        with ThreadPoolExecutor(max_workers=min(32, len(hosts))) as ex:
            return [h for h, ok in ex.map(answers, hosts) if not ok]

    def _classify(self, st, entries, before, after, flows, undelivered, machines, silent,
                  recovered, budget, expect_refusal, conserve, must_accept=False):
        wrong = [d for ok, d in st.checks if not ok]
        if must_accept and st.refused:
            wrong.append("{} valid operation(s) refused: {}".format(
                len(st.refused), _group(o["reason"] for o in st.refused)))
        damage, known = [], {}
        refused = st.refused
        for o in refused:
            o["kind"] = reason_kind(o["reason"], o["secs"], budget)
            lab = known_label(o["reason"])
            if lab:
                known[lab] = known.get(lab, 0) + 1
        unclear = [o for o in refused if o["kind"] == "unclear"]
        late = [o for o in refused if o["kind"] == "late"]
        if unclear:
            damage.append("{} refusal(s) gave no usable reason: {}".format(
                len(unclear), _group(o["reason"] for o in unclear)))
        if late:
            damage.append("{} refusal(s) came only after {}s or more (the longest {:.0f}s)".format(
                len(late), budget, max(o["secs"] or 0 for o in late)))
        if expect_refusal and st.accepted:
            wrong.append("{} operation(s) accepted that should have been refused".format(
                len(st.accepted)))
        if undelivered:
            wrong.append("{} accepted transfer(s) never reached the receiver's node in {}s, e.g. "
                         "tx {} on {}".format(len(undelivered), DELIVERY_WAIT, undelivered[0][1][:16],
                                              _short(undelivered[0][0])))
        host_of = dict((e["did"], e["host"]) for e in entries)
        for did, rbt, ft, nft in _new_locks(before, after):
            bits = [x for x in ("{:.3f} RBT".format(rbt) if rbt > TOL else "",
                                "{} FT".format(ft) if ft else "",
                                "{} NFT/SC".format(nft) if nft else "") if x]
            damage.append("{} left Locked on {}".format(" + ".join(bits), _short(host_of[did])))
        drifted = [d for d in after if before.get(d) is not None
                   and set(after[d]["drift"]) - set(before[d]["drift"])]
        if drifted:
            damage.append("the token_denom counter was left wrong on {} (KNOWN PRODUCT BUG: "
                          "core/pledge_v2.go:287, persist_genesis_tx.go:209)".format(
                              ", ".join(sorted(set(_short(host_of[d]) for d in drifted)))))
        readable = [d for d in host_of if d in before and d in after]
        if conserve and readable and len(readable) == len(host_of):
            tracked = set(host_of)
            came_in = sum(o["rbt"] for o in flows if o["initiator"] not in tracked and o["owner"] in tracked)
            went_out = sum(o["rbt"] for o in flows if o["initiator"] in tracked and o["owner"]
                           and o["owner"] not in tracked)
            moved = sum(after[d]["held"] - before[d]["held"] for d in readable)
            if abs(moved - (came_in - went_out)) > 0.002 + 0.001 * len(flows):
                wrong.append("value not conserved: the participants' RBT changed by {:+.3f}, "
                             "{:+.3f} expected".format(moved, came_in - went_out))
        for m, s in machines.items():
            if m == "controller":
                continue
            for slot in s.get("restarted", []):
                damage.append("a node on {} (slot {}) restarted during the step".format(_short(m), slot))
            for slot in s.get("deafened", []):
                damage.append("a node on {} (slot {}) lost its IPFS p2p listener - it can no "
                              "longer receive".format(_short(m), slot))
        if silent:
            damage.append("{} stopped answering its API".format(", ".join(_short(h) for h in silent)))
        if recovered is not None and not recovered[0]:
            damage.append("an ordinary operation afterwards failed too ({}) - it did not "
                          "recover".format(recovered[1][:120]))
        hardware = ["{}: {}".format("the controller" if m == "controller" else _short(m),
                                    ", ".join(s["ceilings"]))
                    for m, s in machines.items() if s.get("ceilings")]

        n, ok_n = len([o for o in st.ops if not o["aside"]]), len(st.accepted)
        secs = [o["secs"] for o in st.accepted if o["secs"] is not None]
        timing = " in p50 {:.1f}s p95 {:.1f}s max {:.1f}s".format(
            _pct(secs, 50), _pct(secs, 95), max(secs)) if secs else ""
        bits = ["{}/{} accepted{}".format(ok_n, n, timing)] if st.ops else []
        if refused and not unclear and not late and not must_accept:
            bits.append("{} refused {}: {}".format(len(refused), "as expected" if expect_refusal
                                                   else "gently", _group(o["reason"] for o in refused)))
        aside = [o for o in st.ops if o["aside"]]
        if aside:
            bits.append("{} set aside (the sender's own tokens or clock): {}".format(
                len(aside), _group([o["reason"] for o in aside], 2)))
        if known:
            bits.append("KNOWN PRODUCT BUG: " + ", ".join("{} ({})".format(k, v) for k, v in known.items()))
        if recovered and recovered[0]:
            bits.append("an ordinary operation afterwards went through in {:.1f}s".format(recovered[2]))
        if wrong:
            st.outcome = WRONG
            bits = wrong + damage + bits
        elif damage:
            st.outcome = HARDWARE if hardware else DAMAGE
            bits = damage + bits
        elif refused and not expect_refusal:
            st.outcome = HARDWARE if hardware else LIMIT
        else:
            st.outcome = PASS
        if hardware:
            bits.append("machines at a ceiling: " + "; ".join(hardware))
        else:
            busy = [(s.get("cpu_peak") or 0, m) for m, s in machines.items() if m != "controller"]
            if busy:
                peak, m = max(busy)
                bits.append("busiest machine {} at {:.0f}% CPU".format(_short(m), peak))
        st.summary = "; ".join(bits + st.notes) or "nothing to report"
        st.record.update({"wrong": wrong, "damage": damage, "hardware": hardware, "known": known,
                          "recovered": recovered, "undelivered": undelivered,
                          "refusals": dict((k, len([o for o in refused if o.get("kind") == k]))
                                           for k in ("stated", "unclear", "late"))})

    def _done(self, st, machines):
        if st.not_run:
            st.outcome, st.summary = NOT_RUN, st.not_run
        st.record.update({
            "id": st.sid, "title": st.title, "expect": st.expect, "outcome": st.outcome,
            "summary": st.summary, "seconds": st.seconds, "data": st.data,
            "ops": [dict((k, o.get(k)) for k in ("ok", "reason", "secs", "txid", "to", "aside", "kind"))
                    for o in st.ops][:500],
            "checks": [{"ok": ok, "detail": d} for ok, d in st.checks],
            "machines": dict((m, dict((k, v) for k, v in s.items() if k != "samples"))
                             for m, s in machines.items()),
        })
        self.steps.append(st)
        print("    {} {}: {} - {}".format(self.case, st.sid, st.outcome, st.summary[:300]))
        return st

    def note(self):
        return " | ".join("{} {}: {}".format(s.sid, s.outcome, s.summary) for s in self.steps)

    def verdict(self, passed=None, limit=None):
        """(True/False/SKIP, actual, note) from the steps. `passed` is the
        sentence for a case whose steps all held; `limit` describes what a LIMIT
        step means for this case."""
        case_evidence.attach(self.case, "steps", [s.record for s in self.steps])
        note = self.note()
        ran = [s for s in self.steps if s.outcome != NOT_RUN]
        bad = [s for s in self.steps if s.outcome in (WRONG, DAMAGE)]
        if bad:
            return False, "; ".join("{} {}: {}".format(s.sid, s.outcome, s.summary[:220])
                                    for s in bad[:3]), note
        hw = [s for s in self.steps if s.outcome == HARDWARE]
        if hw:
            return SKIP, "INCONCLUSIVE - a machine ran out, not necessarily Rubix: " + "; ".join(
                "{}: {}".format(s.sid, s.summary[:200]) for s in hw[:2]), note
        missing = [s for s in self.steps if s.outcome == NOT_RUN and getattr(s, "required", True)]
        if missing or not ran:
            first = (missing or self.steps or [None])[0]
            return SKIP, "NOT RUN - {}".format(
                "{}: {}".format(first.sid, first.summary) if first else "no step ran"), note
        limits = [s for s in self.steps if s.outcome == LIMIT]
        text = passed or "all {} step(s) held".format(len(ran))
        if limits:
            text += "; {} at {}: {}".format(limit or "Rubix reached its limit and refused gently",
                                            limits[0].sid, limits[0].summary[:200])
        return True, text, note

    def outcome_of(self, sid_prefix):
        return [s for s in self.steps if s.sid.split(" ")[0] == sid_prefix]
