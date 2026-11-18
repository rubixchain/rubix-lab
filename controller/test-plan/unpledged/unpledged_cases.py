#!/usr/bin/env python3
"""
unpledged_cases.py - one case: a quorum's unpledged whole tokens cannot be
transferred.

    A --(Q signs, Q pledges its 2 tokens T1 and T2)--> B           2 RBT
    B --(Q2 signs, B spends onward: Q's pledge is released)--> C   2 RBT
    Q --(Q2 signs; T1 and T2 Free, "unpledge" last in their chains)--> D
         S4: 1 RBT    one whole token moves
         S5: 1.5 RBT  one whole token moves, the other is split and half moves
         S6: 0.5 RBT  a whole token is split and half moves
    (user, 2026-11-18: "so will get a clarity on the allowed tokens")

A payment that moves a released whole token is refused "failed to get quorum DID
for token T: tokenID T not found in quorum tokens": the check looks the token up
in the CURRENT transaction's pledges instead of the pledge transaction's
(core/consensus/checks.go:309). A split moves a new part, which has no unpledge
entry, so it never meets the check.

    cd ../full-test
    python3 test_runner.py --cases unpledged --collect-versions
    python3 validate_cases.py --cases unpledged      # offline

Q is an EMPTY wallet funded with exactly 2 RBT from the faucet (user,
2026-11-18: "so the observability of the holdings are better"; "I mean 2
tokens"): it holds T1 and T2 and nothing else, so every step can be followed on
them. Q's node is restarted so Q is the only quorum set up there (a release
checks one quorum of the node only). The user repeats the run for more rounds.

Every step is verifiable afterwards: its transaction IDs, and before and after
it each participant's node, DID and holdings (every token Q and B hold, the
tokens that moved, by status) and T1's and T2's chains on Q's node - in the
step's evidence and its notes. When a payment is refused, the payer's, the
signing quorum's and the receiver's node give their log lines for its seconds
(each node's own clock), into the evidence and into
reports/logs/unpledged-node-logs_<time>.txt.
"""

import datetime
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "issues"))

import issue_kit as kit_mod
from issue_kit import ch, db, rc, steps, token_health

CASE = "UNPLEDGED-WHOLE-TOKEN"
TOKENS = 2              # whole tokens Q holds, pledges, gets back and offers to D
RELEASE_WAIT = 120      # seconds for Q's pledge to come back after B pays C
LOG_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "..", "reports", "logs"))
_LOG_FILE = {"path": None}


# ---------------------------------------------------------------------------
# What the participants hold - recorded before and after every step
# ---------------------------------------------------------------------------

def _chain_tail(host, token, n=5):
    """[(position, transaction, role)] - the token's last n chain entries on that node."""
    try:
        rows = db.chain_rows(host, token)
    except Exception:
        return []
    return [(pos, x, token_health.ROLE.get(r, r)) for pos, x, _prev, r in rows[-n:]]


def _rows(e, tokens=None):
    """[(token, value, status)] - every token the DID holds (Free, Locked,
    Committed, Pledged), or only `tokens`, in any status."""
    try:
        rows = db.token_rows(e["host"], e["did"])
    except Exception:
        return None
    return [(t, round(v, 3), db.STATUS_NAME.get(s, s)) for t, v, s, _p in rows
            if (tokens is None and s in kit_mod.HELD) or (tokens and t in tokens)]


def holdings(st, label, parts, follow, q, q_tokens):
    """Record what each participant holds, and the chain of each of Q's
    tokens on Q's node, under `label` in the step's evidence; a line each in
    its notes. parts: [(name, entry, whole)] - whole = list every token it
    holds (Q and the empty wallet B), else only the tokens being followed."""
    snap = {"participants": {}, "chains_on_Q": {}}
    for name, e, whole in parts:
        rows = _rows(e, None if whole else follow)
        snap["participants"][name] = {"node": e["host"], "did": e["did"], "holds": rows}
        st.note("{} - {} ({} on {}): {}".format(
            label, name, e["did"][:16], kit_mod.short(e["host"]),
            "unreadable" if rows is None else
            ", ".join("{} {} {}".format(t, v, s) for t, v, s in rows) or "nothing"))
    for t in q_tokens:
        tail = _chain_tail(q["host"], t)
        snap["chains_on_Q"][t] = tail
        st.note("{} - {}'s chain on Q's node ends: {}".format(
            label, t, "; ".join("{} {} {}".format(pos, x[:16], role) for pos, x, role in tail)
            or "no chain"))
    st.data.setdefault("holdings", {})[label] = snap
    return snap


# ---------------------------------------------------------------------------
# Every participant's log for the seconds of a refused payment
# ---------------------------------------------------------------------------

def _now_on(host):
    """The node's own clock, as its log writes it (2026-11-18T14:02:11)."""
    code, out = kit_mod.ssh(host, "date '+%Y-%m-%dT%H:%M:%S'", timeout=20)
    return out.strip() if code == 0 and out.strip() else None


def _window(host, start, end, limit=400):
    """The node's log lines from `start` to `end` (its own clock)."""
    log = kit_mod.NODE_LOG.format(slot=kit_mod.slot(host))
    cmd = ("awk -v s='{s}' -v e='{e}' 'substr($0,1,19) >= s && substr($0,1,19) <= e' {log} "
           "| tail -n {n}").format(s=start, e=end, log=log, n=int(limit))
    code, out = kit_mod.ssh(host, cmd, timeout=60)
    return [l.rstrip() for l in out.splitlines() if l.strip()] if code == 0 else \
        ["(the log could not be read: {})".format(out.strip()[:160])]


def _write_logs(title, logs):
    os.makedirs(LOG_DIR, exist_ok=True)
    if not _LOG_FILE["path"]:
        _LOG_FILE["path"] = os.path.join(LOG_DIR, "unpledged-node-logs_{}.txt".format(
            datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")))
    with open(_LOG_FILE["path"], "a", encoding="utf-8") as fh:
        fh.write("=" * 100 + "\n{}\n".format(title))
        for role, host, did, start, end, lines in logs:
            fh.write("-" * 100 + "\n{} - node {} - DID {} - log from {} to {} ({} lines)\n".format(
                role, host, did, start, end, len(lines)))
            fh.write("\n".join(lines) + "\n")
    return _LOG_FILE["path"]


def pay(st, frm, to, quorum, amount, label):
    """One payment, recorded on the step with its transaction ID. When Rubix
    refuses it, every participant's log for its seconds goes to the evidence
    and the log file, and the step notes each node's first ERROR line."""
    who = [("payer", frm), ("quorum", quorum), ("receiver", to)]
    starts = dict((e["host"], _now_on(e["host"])) for _r, e in who)
    ok, msg, res = ch.send(st, frm, to, rbt=amount, memo="{} {}".format(CASE, label), set_aside=())
    txid = kit_mod.txid_of(res)
    st.data.setdefault("payments", []).append(
        {"payment": label, "amount": amount, "from": [frm["host"], frm["did"]],
         "to": [to["host"], to["did"]], "quorum": [quorum["host"], quorum["did"]],
         "accepted": bool(ok), "transaction_id": txid,
         "refusal": "" if ok else ch.node_reason(msg, 1000)})
    st.note("{}: {} RBT, {}".format(label, amount, "accepted, transaction " + (txid or "?") if ok
                                    else "REFUSED: " + ch.node_reason(msg, 400)))
    if ok:
        return ok, msg, res
    time.sleep(5)                       # let every node finish writing about it
    logs = []
    for role, e in who:
        start, end = starts.get(e["host"]), _now_on(e["host"])
        lines = _window(e["host"], start, end) if start and end else ["(the node's clock could not be read)"]
        logs.append((role, e["host"], e["did"], start, end, lines))
    path = _write_logs("{} - {} - {} RBT {} -> {} through quorum {} - refused: {}".format(
        CASE, label, amount, frm["did"], to["did"], quorum["did"], ch.node_reason(msg, 400)), logs)
    st.data.setdefault("participant_logs", []).append(
        {"payment": label, "refusal": ch.node_reason(msg, 1000), "file": path,
         "nodes": [{"role": r, "host": h, "did": d, "from": s, "to": e, "lines": l}
                   for r, h, d, s, e, l in logs]})
    for role, host, _d, _s, _e, lines in logs:
        errors = [l for l in lines if "[ERROR]" in l or "[WARN]" in l]
        st.note("{} {}: {} log line(s), {} ERROR/WARN{}".format(
            role, kit_mod.short(host), len(lines), len(errors),
            " - first: " + errors[0][:260] if errors else ""))
    st.note("every participant's log for this payment: " + path)
    return ok, msg, res


def _wait(cond, seconds, every=2):
    """Seconds until cond() held, or None."""
    t0 = time.time()
    while time.time() - t0 < seconds:
        if cond():
            return round(time.time() - t0, 1)
        time.sleep(every)
    return None


# ---------------------------------------------------------------------------

PURPOSE = {
    CASE: "Whether a wallet that signed as quorum can pay with its whole tokens once the pledge on "
          "them is released - and, when Rubix refuses, what every node in that payment wrote in its log.",
}

STEPS = {
    CASE: [
        ("S1", "Set-up",
         "Q2 = a quorum; Q = an empty wallet given exactly {n} RBT by the faucet ({n} tokens, T1 and "
         "T2), its node restarted so Q is the only quorum set up there, then set up as quorum; A = a "
         "wallet whose next {n} tokens are fit; B = an empty wallet; C and D = DIDs to pay. Record "
         "what each holds".format(n=TOKENS),
         "Q holds T1 and T2 alone, Free"),
        ("S2", "A pays B, Q signs and pledges T1 and T2",
         "A pays B {} RBT through Q; record the transaction and what A, B and Q hold".format(TOKENS),
         "accepted and credited to B; T1 and T2 Pledged under Q, each chain ending with a pledge entry"),
        ("S3", "B pays C, Q's pledge is released",
         "B pays C the {} tokens it got, through Q2; wait up to {}s for Q's pledge to be released; "
         "record what B, C and Q hold".format(TOKENS, RELEASE_WAIT),
         "accepted and credited to C; T1 and T2 Free under Q again, each chain's last entry 'unpledge'"),
        ("S4", "Q pays D 1 RBT (one whole token), Q2 signs",
         "Q pays D 1 RBT through Q2 - Rubix moves the whole token T1; on a refusal, the payer's (Q's), "
         "Q2's and D's log for those seconds; record what Q and D hold",
         "accepted and credited to D"),
        ("S5", "Q pays D 1.5 RBT (a whole token and half of another), Q2 signs",
         "Q pays D 1.5 RBT through Q2 - Rubix takes both whole tokens, moves one and splits the "
         "other; logs and holdings as S4",
         "accepted and credited to D"),
        ("S6", "Q pays D 0.5 RBT (half a token), Q2 signs",
         "Q pays D 0.5 RBT through Q2 - Rubix splits a whole token and moves the new half; logs and "
         "holdings as S4",
         "accepted and credited to D"),
    ],
}

# S4-S6: what Q pays D, in this order, once its tokens are back from the pledge
SPENDS = (("S4", 1.0, "one whole token"), ("S5", 1.5, "a whole token and half of another"),
          ("S6", 0.5, "half a token"))

CASE_INFO = {
    CASE: ("A wallet that was a quorum cannot pay with its whole tokens once the pledge on them is "
           "released: Rubix refuses 'failed to get quorum DID for token'. Paid 1, 1.5 and 0.5 RBT from "
           "the released tokens, to see which amounts Rubix allows.",
           "The released tokens are spent like any other, whatever the amount."),
}


def unpledged_whole_token(ctx, ci):
    run, kit, p = steps.CaseRun(ctx, CASE, STEPS[CASE]), kit_mod.Kit(ctx, CASE), {}

    def setup():
        Q2, why = kit.quorum(need=TOKENS + 5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        Q, why = kit.empty(None, Q2, role="Q (quorum, funded with {} RBT by the faucet)".format(TOKENS))
        if not Q:
            return False, why
        ok, msg = rc.fund_did(Q["host"], Q["did"], TOKENS)
        if not ok:
            return False, "the faucet could not give Q {} RBT: {}".format(TOKENS, msg)
        held = _rows(Q) or []
        if len(held) != TOKENS or any(v != 1.0 or s != "Free" for _t, v, s in held):
            return False, "Q should hold {} whole tokens Free after the faucet's {} RBT, it holds {}".format(
                TOKENS, TOKENS, held)
        p["faucet_tx"] = [o.get("txid") for o in rc.OPS if o.get("owner") == Q["did"] and o.get("status")]
        ok, why = kit.restart(Q["host"])
        if not ok:
            return False, why
        ok, why = kit.as_quorum(Q, TOKENS)
        if not ok:
            return False, why
        A, why = kit.holders(1, k=TOKENS)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.empty(None, Q2, role="B", tokens=kit.tokens_of(A))
        if not B:
            return False, why
        C, why = kit.wallet_on(role="C (payee)", tokens=kit.tokens_of(A))
        if not C:
            return False, why
        Ts = sorted(t for t, _v, _s in held)
        D, why = kit.wallet_on(role="D (payee)", tokens=Ts)
        if not D:
            return False, why
        p.update(A=A, B=B, C=C, D=D, Q=Q, Q2=Q2, Ts=Ts, TAs=kit.tokens_of(A),
                 notes=[kit_mod.who(e, n) for e, n in ((Q, "Q"), (Q2, "Q2"), (A, "A"), (B, "B"),
                                                        (C, "C"), (D, "D"))] +
                 ["Q's tokens {}, given by the faucet in transaction(s) {}".format(
                     ", ".join(Ts), ", ".join(t for t in p["faucet_tx"] if t) or "?"),
                  "the tokens A pays next: {}".format(", ".join(kit.tokens_of(A)))])
        return True, ""

    def recorded_setup(st):
        for n in p.get("notes", []):
            st.note(n)
        st.data["faucet_transactions"] = p.get("faucet_tx")
        holdings(st, "after set-up", [("Q", p["Q"], True), ("A", p["A"], False), ("B", p["B"], True)],
                 p["Ts"] + p["TAs"], p["Q"], p["Ts"])
    s1 = run.step("S1", recorded_setup, nodes=[], watch=False, deliver=False, require=[setup])
    if s1.outcome != steps.PASS:
        kit.evidence()
        return run.verdict()
    A, B, C, D, Q, Q2, Ts, TAs = (p[k] for k in ("A", "B", "C", "D", "Q", "Q2", "Ts", "TAs"))
    follow = Ts + TAs

    def q_status(status):
        rows = dict((t, s) for t, _v, s in (_rows(Q) or []))
        return all(rows.get(t) == status for t in Ts)

    def pledge(st):
        ok, why = kit.wire([A], Q)
        if not ok:
            st.skip(why)
            return
        kit.note_moved(A, B, A["tokens"], "A pays B, Q pledges its tokens")
        ok, _m, res = pay(st, A, B, Q, float(TOKENS), "A pays B (Q signs)")
        if not ok:
            return
        secs = _wait(lambda: q_status("Pledged"), 30)
        snap = holdings(st, "after A pays B", [("Q", Q, True), ("A", A, False), ("B", B, True)],
                        follow, Q, Ts)
        st.check(secs is not None, "Q's tokens are not all Pledged 30s after the payment")
        tails = snap["chains_on_Q"]
        st.check(all(tails.get(t) and tails[t][-1][2] == "pledge" for t in Ts),
                 "not every one of Q's tokens has a chain ending with a pledge entry")
        st.note("the payment's transaction is {}; the pledge entries' are {}".format(
            kit_mod.txid_of(res), ", ".join(tails[t][-1][1] for t in Ts if tails.get(t))))
    s2 = run.step("S2", pledge, nodes=[A, B, Q], must_accept=True)

    def release(st):
        ok, why = kit.wire([B], Q2)
        if not ok:
            st.skip(why)
            return
        _wait(lambda: all(t in [x for x, _v, s in (_rows(B) or []) if s == "Free"] for t in TAs), 30)
        kit_mod.afford(Q2, float(TOKENS))
        ok, _m, _r = pay(st, B, C, Q2, float(TOKENS), "B pays C (Q2 signs)")
        if not ok:
            return
        back = _wait(lambda: q_status("Free"), RELEASE_WAIT, 3)
        snap = holdings(st, "after B pays C", [("Q", Q, True), ("B", B, True), ("C", C, False)],
                        follow, Q, Ts)
        tails = snap["chains_on_Q"]
        p["unpledged"] = back is not None and all(tails.get(t) and tails[t][-1][2] == "unpledge" for t in Ts)
        st.data["released_after_s"] = back
        st.check(back is not None, "Q's pledge was not released within {}s of B paying C".format(
            RELEASE_WAIT))
        if back is not None:
            st.note("Q's pledge came back {}s after B paid C".format(back))
            st.check(p["unpledged"], "Q's tokens are Free again but not every chain's last entry is "
                                     "'unpledge'")
    run.step("S3", release, nodes=[B, C, Q, Q2], must_accept=True,
             require=[(s2.outcome == steps.PASS, "Q did not pledge its tokens in S2")])

    def next_are_q_tokens():
        """Rubix's own pick (core/wallet/token_lock.go:545) must be T1 and T2."""
        nxt = (kit_mod.scan(ctx, TOKENS, [Q["host"]]).get(Q["did"]) or {}).get("next") or []
        picked = sorted(t for t, _c in nxt)
        return picked == Ts, "Q's next {} whole tokens are {}, not {}".format(TOKENS, picked, Ts)

    def spend(amount, kind):
        def body(st):
            ok, why = kit.wire([Q], Q2)
            if not ok:
                st.skip(why)
                return
            kit_mod.afford(Q2, amount)
            label = "Q pays D {} RBT, {} (Q2 signs)".format(amount, kind)
            ok, msg, _r = pay(st, Q, D, Q2, amount, label)
            p["spent"][amount] = ("accepted" if ok else "REFUSED: " + ch.node_reason(msg, 300))
            if not ok:
                named = [t for t in Ts if t in ch.node_reason(msg, 1000)]
                st.note("the refusal names {}".format(", ".join(named) if named else
                                                      "none of Q's tokens ({})".format(", ".join(Ts))))
            # Q's tokens in any status (moved, split, still Free), everything Q
            # holds now, and D's share of them
            holdings(st, "after " + label, [("Q's tokens", Q, False), ("Q", Q, True), ("D", D, False)],
                     follow, Q, Ts)
        return body

    def holds_free(amount):
        def check():
            free = sum(v for _t, v, s in (_rows(Q) or []) if s == "Free")
            return free + 0.0015 >= amount, "Q holds {:.3f} RBT Free after the earlier payments, the step " \
                                            "needs {}".format(free, amount)
        return check

    back = lambda: (p.get("unpledged"), "Q's tokens are not Free with 'unpledge' last in their chains "
                                        "- S3 did not get there")
    p["spent"] = {}
    for i, (sid, amount, kind) in enumerate(SPENDS):
        # When the 1 RBT payment goes through (no bug), Q is left with 1 RBT and
        # cannot pay 1.5: that step is then not needed for the answer.
        optional = i > 0 and p["spent"].get(SPENDS[0][1]) == "accepted"
        p.setdefault("spend_steps", []).append((amount, run.step(
            sid, spend(amount, kind), nodes=[Q, D, Q2], must_accept=True, required=not optional,
            require=[back, holds_free(amount)] + ([next_are_q_tokens] if i == 0 else []))))

    # What Rubix allowed, amount by amount. REPRODUCED when a payment of the
    # released tokens was refused; a failure before S4 is reported as what it was.
    kit.evidence()
    passed, actual, note = run.verdict(passed="the released tokens were spent like any other")
    allowed = "; ".join("{} RBT: {}".format(a, p["spent"].get(a) or "not run - " + s.summary[:160])
                        for a, s in p["spend_steps"])
    spends = [s for _a, s in p["spend_steps"]]
    if any(s.outcome == steps.WRONG for s in spends):
        actual = "REPRODUCED - from Q's released tokens: " + allowed
    elif spends[0].outcome == steps.PASS and all(
            s.outcome == steps.PASS or (s.outcome == steps.NOT_RUN and not s.required) for s in spends):
        actual = "NOT REPRODUCED - from Q's released tokens: " + allowed
    return passed, actual, note


CASES = {CASE: unpledged_whole_token}
ORDER = [CASE]
NEEDS = {CASE: {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True}}
NO_DELAY_RERUN = {CASE}
