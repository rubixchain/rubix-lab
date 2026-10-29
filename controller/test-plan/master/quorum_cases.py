"""quorum_cases.py - what one quorum can carry, and how it says no.

One quorum signs every transaction (QuorumRequired = 1,
core/quorum_initiator.go:17; quorumAddresses[0] in core/transaction.go), so its
capacity is a ceiling on everything the fleet does at once. These cases find
that ceiling and check that Rubix meets it GENTLY: past what a quorum can take,
the rest must be refused with a stated reason in good time, leaving nothing
Locked and the quorum able to sign again (master/steps.py).

What is kept out of the way so the limit found is Rubix's, not the lab's:
senders sit on machines other than the quorum's (they would share its CPU),
one sender per node until every node is used, wallets of whole tokens the
faucet paid (no second-hand part tokens), DIDs that were quorums earlier are
not senders where a clean one is free and their refusals are counted apart,
every sending node is checked to sign with the step's quorum, the quorum holds
Free RBT for every pledge the step can make, every machine is watched for a
CPU / memory / disk ceiling, and nodes that cannot receive are left out of the
run (fleet_health).
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc
import db_client as db
import steps

SKIP = "SKIP"

# Senders the QCAP-01 unit takes; rungs above this use a second DID on the same
# nodes (labelled so). 220 senders + 1 quorum fit the 234-node pool.
QCAP_SENDERS = 220

# Kept for older callers; the markers live in case_helpers.
NOT_THE_QUORUM = ch.SENDERS_OWN_TOKENS

PURPOSE = {
    "QCAP-01": "How many transfers ONE quorum signs at the same moment - whole RBT, and a "
               "fractional amount that makes the quorum split its own tokens for every pledge - "
               "and whether, past that, Rubix refuses the rest gently and recovers.",
    "QCAP-02": "Whether a quorum's pledge boundary is exactly its Free holding: up to it a "
               "transfer is signed and the quorum pledges exactly the value; one unit over is "
               "refused for quorum liquidity, at once, with nothing on either side changed.",
    "QCAP-03a": "Whether a quorum that has pledged RBT can spend its OTHER RBT without touching "
                "what it pledged.",
    "QCAP-03b": "Whether pledged RBT is unspendable until released, how long the release takes "
                "once the receiver spends onward, and what the quorum can spend after it.",
}

STEPS = {
    "QCAP-01": [
        ("S1", "Set-up check",
         "Set the quorum up and fund it for the largest rung; point every sender's node at it "
         "and read the lists back; take senders from machines other than the quorum's",
         "the quorum is set up and answers; every sending node signs with it"),
        ("S2", "Whole-RBT rung (one per rung)",
         "n senders, one per node across the machines, send 1 RBT each at the same instant "
         "through the one quorum - a ring, so each also receives one",
         "all n accepted and each on its receiver's node; past the quorum's limit the rest "
         "refused with a stated reason within 60s, nothing left Locked, no node restarted"),
        ("S3", "Recovery after the whole-RBT ladder",
         "one ordinary 1 RBT transfer through the same quorum",
         "goes through in normal time - the quorum signs again"),
        ("S4", "Fractional rung (one per rung)",
         "the same senders all send one fractional amount at the same instant, so the quorum "
         "splits its own tokens for every pledge at once",
         "as S2; a refusal leaves no token Locked and the quorum's token_denom counter right"),
        ("S5", "Recovery after the fractional ladder",
         "one ordinary fractional transfer through the same quorum",
         "goes through in normal time"),
    ],
    "QCAP-02": [
        ("S1", "Set-up check",
         "Set the five quorums up; give each sender a DID on its node holding no part token "
         "(a fraction is then split from its own whole token), funded for the value",
         "every quorum set up; every sender wired to its quorum and funded"),
        ("S2", "Pay at the boundary (one quorum per offset)",
         "read the quorum's Free holding H, then pay H-0.1, H-0.001, H, H+0.001 or H+0.1 "
         "through it, one transfer",
         "up to H: accepted, and the quorum's Pledged rises by exactly the value; over H: "
         "refused for insufficient quorum liquidity within 60s, the quorum's Free and the "
         "sender's wallet unchanged, nothing Locked"),
    ],
    "QCAP-03a": [
        ("S1", "The quorum pledges",
         "A pays B 20 RBT through Q", "accepted; Q's Pledged rises by 20"),
        ("S2", "The quorum spends its free RBT",
         "while the 20 are pledged, Q pays C 15 of its free RBT through a second quorum Q2",
         "accepted and credited; the tokens Q pledged are untouched (same token_ids, still "
         "Pledged)"),
    ],
    "QCAP-03b": [
        ("S1", "Set-up check",
         "Q and B play from DIDs on their nodes holding no whole token; Q is set up and given "
         "exactly 20 RBT; A signs through Q, Q and B through Q2",
         "set up and wired"),
        ("S2", "The quorum pledges all it has",
         "A pays B 20 RBT through Q", "accepted; B holds exactly the 20 tokens A paid"),
        ("S3", "Spend what is pledged",
         "Q tries to send 20 RBT while all of it is pledged",
         "refused with a stated reason within 60s; the pledge intact, nothing Locked"),
        ("S4", "Release by spending onward",
         "B sends the 20 tokens A paid it on to C; then wait for Q's pledge to be released",
         "accepted, B's paid tokens gone; Q's pledge released within 120s (a quorum releases "
         "when a later transaction spends a token it pledged for, core/callback.go)"),
        ("S5", "Spend a released whole token",
         "Q sends 1 RBT - one of the whole tokens it had pledged",
         "accepted by the product's own rule; the known ex-quorum bug (checks.go:308) refuses "
         "it - counted apart, not as the step's result"),
        ("S6", "Spend a split of a released token",
         "Q sends 0.5 RBT, which splits a released whole token",
         "accepted and credited"),
    ],
}


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def _whole_wallet(entry):
    """`entry` with a DID on its node that holds no Free part token, or None.
    A fractional value is then split from the sender's own whole token, whose
    genesis it holds - paying with a part token it received from someone else
    is refused by the known minter-allowlist bug, which on 2026-10-22 stood in
    for the boundary itself at value == holding."""
    own = entry.get("did")
    for did in [own] + [d for d in (entry.get("dids") or []) if d != own]:
        try:
            rows = db.token_rows(entry["host"], did, status=db.FREE)
        except Exception:
            continue
        if not [v for _t, v, _st, _p in rows if v < 1.0 - 1e-9]:
            return dict(entry, did=did)
    return None


def _top_up(entry, amount, port):
    """Set-up, not a test: make `entry` hold `amount` Free RBT."""
    have = _free(entry["host"], entry["did"]) or 0.0
    if have < amount:
        rc.fund_did(entry["host"], entry["did"], amount - have, port)


def _wire_all(hosts, quorum_did, port):
    """Point every host at the quorum, then read every list back. (ok, why)."""
    bad = []

    def one(h):
        rc.quorum_reset(h, [quorum_did], port)
        return h, steps.wired(h, quorum_did, port)
    for h, (ok, why) in ch.run_parallel(one, sorted(set(hosts)), workers=40):
        if not ok:
            bad.append(why)
    return (not bad), ("{} node(s) do not sign with the step's quorum, e.g. {}".format(
        len(bad), bad[0]) if bad else "")


def _senders(ctx, q, n, port):
    """Up to n senders: one per node on machines other than the quorum's, then -
    for rungs above that - a second DID on the same nodes, each a DID that was
    never a quorum, funded with 3 whole RBT."""
    qm = ch.machine(q["host"])
    first = [s for s in ctx.senders if ch.machine(s["host"]) != qm]
    out = list(first[:n])
    if len(out) < n:
        for s in first:
            for d in (s.get("dids") or []):
                if len(out) >= n:
                    break
                if d == s["did"]:
                    continue
                try:
                    if db.ex_pledged_free_value(s["host"], d) > 0:
                        continue
                except Exception:
                    continue
                e = dict(s, did=d)
                _top_up(e, 3, port)
                out.append(e)
            if len(out) >= n:
                break
    return out


def _climb(run, sid, rec_sid, q, senders, rungs, amount_of, port, label):
    """One ladder. Climbs while every rung PASSes; stops at the first that does
    not. Returns (highest rung in full, the stopping step or None)."""
    limit, stop = 0, None
    per_node = len(set(s["host"] for s in senders))
    for n in rungs:
        amount = amount_of()
        group = senders[:n]
        _top_up(q, n * amount + 5, port)
        jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": amount,
                 "memo": "QCAP-01 {} rung {}".format(label, n)} for a, b in ch.ring(group)]
        st = run.step(sid, lambda st, jobs=jobs: st.add(ch.burst(jobs), jobs, set_aside=ch.ASIDE),
                      nodes=group + [q],
                      label="{} at once, {:.3f} RBT each{}".format(
                          n, amount, " (2 DIDs on some nodes)" if n > per_node else ""),
                      require=[lambda n=n, amount=amount: steps.holds(q, n * amount)])
        if st.outcome == steps.PASS:
            limit = n
            time.sleep(ctx_settle(run))
            continue
        stop = st
        break
    a, b = senders[0], senders[1]
    amount = amount_of()
    run.step(rec_sid, lambda st: ch.send(st, a, b, rbt=amount, memo="QCAP-01 recovery"),
             nodes=[a, b, q], required=False, must_accept=True)
    return limit, stop


def ctx_settle(run):
    return getattr(run.ctx.args, "ladder_settle", 0) or 0


def qcap_01(ctx, ci):
    run = steps.CaseRun(ctx, "QCAP-01", STEPS["QCAP-01"])
    if not ctx.quorum_hosts or len(ctx.senders) < 2:
        return SKIP, "needs a quorum and senders", ""
    q, port = ctx.quorum_hosts[0], ctx.port
    rungs = ch.ladder(ctx.args.quorum_ladder, 2 * len(ctx.senders))
    if not rungs:
        return SKIP, "not enough senders for the ladder", ""
    senders = _senders(ctx, q, rungs[-1], port)
    rungs = [n for n in rungs if n <= len(senders)]

    _top_up(q, rungs[-1] + 10, port)
    s1 = run.step("S1", lambda st: None, nodes=[q],
                  require=[lambda: steps.quorum_ready(q, rungs[-1], port),
                           lambda: _wire_all([s["host"] for s in senders], q["did"], port)])
    if s1.outcome == steps.NOT_RUN:
        return run.verdict()

    whole, w_stop = _climb(run, "S2", "S3", q, senders, rungs, lambda: 1.0, port, "whole")
    frac, f_stop = _climb(run, "S4", "S5", q, senders, rungs, lambda: ch.rand_value(0.101, 0.899),
                          port, "fractional")

    def keep(key, value, stop):
        # A limit is saved only when Rubix set it: a ladder stopped by a machine
        # at a ceiling, or by the lab, says nothing about the quorum.
        if stop is None or stop.outcome in (steps.LIMIT, steps.DAMAGE, steps.WRONG):
            if value:
                ch.save_limit(key, value, {"quorum": q["host"], "stopped_at": stop.sid if stop else "",
                                           "outcome": stop.outcome if stop else "top of ladder"})
    keep("quorum_senders", whole, w_stop)
    keep("quorum_senders_fractional", frac, f_stop)

    def said(n, stop):
        if stop is None:
            return "{} at once in full (the top of the ladder - the real limit is higher)".format(n)
        return "{} at once in full, then {} at {}".format(n, stop.outcome, stop.sid)
    return run.verdict(passed="one quorum signed whole-RBT transfers {}; fractional {}".format(
        said(whole, w_stop), said(frac, f_stop)), limit="the quorum's limit, met gently")


def qcap_02(ctx, ci):
    run = steps.CaseRun(ctx, "QCAP-02", STEPS["QCAP-02"])
    offsets = (-0.1, -0.001, 0.0, 0.001, 0.1)
    quorums, senders, recv = ctx.quorum_hosts, ctx.senders, ctx.receivers
    n = min(len(quorums), len(senders), len(recv), len(offsets))
    if n < 1:
        return SKIP, "needs a quorum, a sender and a receiver on different nodes", ""
    port = ctx.port
    payers = [(_whole_wallet(s) or s) for s in senders[:n]]

    def setup(st):
        for i in range(n):
            ok, msg = rc.quorum_setup(quorums[i]["host"], quorums[i]["did"], port)
            st.check(ok, "quorum {} set up: {}".format(steps._short(quorums[i]["host"]), msg))
    s1 = run.step("S1", setup, nodes=quorums[:n], watch=False,
                  require=[lambda i=i: steps.wired(payers[i]["host"], quorums[i]["did"], port)
                           for i in range(n)])
    if s1.outcome != steps.PASS:
        return run.verdict()

    for i in range(n):
        q, s, r, offset = quorums[i], payers[i], recv[i], offsets[i]
        held = _free(q["host"], q["did"])
        if held is None:
            continue
        value = round(held + offset, 3)
        if value <= 0:
            continue
        over = value > held + 1e-9
        _top_up(s, value + 2.0, port)

        def body(st, q=q, s=s, r=r, value=value, over=over):
            free0, pledged0 = _free(q["host"], q["did"]), db.pledged_value(q["host"], q["did"])
            ok, msg, _r = ch.send(st, s, r, rbt=value, memo="QCAP-02 boundary")
            reason = steps.node_reason(msg)
            if ok and not over:
                got = 0.0
                for _ in range(10):
                    got = db.pledged_value(q["host"], q["did"]) - pledged0
                    if got >= value - steps.TOL:
                        break
                    time.sleep(1)
                st.check(abs(got - value) <= 0.002, "the quorum pledged {:.3f} for a {:.3f} transfer "
                         "- it should pledge exactly the value".format(got, value))
            if not ok and over and not st.ops[-1]["aside"]:
                st.check("insufficient quorum liquidity" in reason,
                         "refused, but not for quorum liquidity: " + reason[:160])
                time.sleep(2)
                free1 = _free(q["host"], q["did"])
                st.check(free0 is None or free1 is None or abs(free1 - free0) <= steps.TOL,
                         "the quorum's Free moved {:+.3f} on a refusal".format((free1 or 0) - (free0 or 0)))
        run.step("S2", body, nodes=[q, s, r], expect_refusal=over, must_accept=not over,
                 label="holding {:.3f}, value {:.3f} ({:+.3f})".format(held, value, offset),
                 require=[lambda s=s, value=value: steps.holds(s, value + 0.1)])
    return run.verdict(passed="the pledge boundary is the quorum's Free holding",
                       limit="a boundary refused gently")


def _as_quorum(entry, floor, port):
    """Activate `entry` as a quorum signer and fund it to `floor`."""
    ok, msg = rc.quorum_setup(entry["host"], entry["did"], port)
    if not ok:
        return False, "quorum setup failed: " + str(msg)
    _top_up(entry, floor, port)
    return True, ""


def qcap_03a(ctx, ci):
    run = steps.CaseRun(ctx, "QCAP-03a", STEPS["QCAP-03a"])
    if len(ctx.senders) < 5:
        return SKIP, "needs five nodes (A, B, C, Q, Q2)", ""
    A, B, C, Q, Q2 = ctx.senders[:5]
    port, V, W = ctx.port, 20.0, 15.0
    ready = [lambda: _as_quorum(Q, 100, port), lambda: _as_quorum(Q2, 100, port),
             lambda: (rc.quorum_reset(A["host"], [Q["did"]], port)[0], "A not wired"),
             lambda: (rc.quorum_reset(Q["host"], [Q2["did"]], port)[0], "Q not wired"),
             lambda: steps.wired(A["host"], Q["did"], port),
             lambda: steps.wired(Q["host"], Q2["did"], port)]
    held = {}

    def pledge(st):
        p0 = db.pledged_value(Q["host"], Q["did"])
        ch.send(st, A, B, rbt=V, memo="QCAP-03a A->B")
        time.sleep(ch.SETTLE)
        held["ids"] = set(t for t, _v, _s, _p in db.token_rows(Q["host"], Q["did"], status=db.PLEDGED))
        got = db.pledged_value(Q["host"], Q["did"]) - p0
        st.check(abs(got - V) <= 0.002, "Q's Pledged rose by {:.3f}, not {:.3f}".format(got, V))
    s1 = run.step("S1", pledge, nodes=[A, B, Q], require=ready, must_accept=True)
    if s1.outcome != steps.PASS:
        return run.verdict()

    def spend(st):
        ch.send(st, Q, C, rbt=W, memo="QCAP-03a Q->C")
        time.sleep(ch.SETTLE)
        still = set(t for t, _v, _s, _p in db.token_rows(Q["host"], Q["did"], status=db.PLEDGED))
        touched = held["ids"] - still
        st.check(not touched, "Q spent {} token(s) it had pledged".format(len(touched)))
    run.step("S2", spend, nodes=[Q, C, Q2], must_accept=True)
    return run.verdict(passed="a quorum spent its free RBT without touching the {:.0f} it had "
                              "pledged".format(V))


def qcap_03b(ctx, ci):
    run = steps.CaseRun(ctx, "QCAP-03b", STEPS["QCAP-03b"])
    if len(ctx.senders) < 5:
        return SKIP, "needs five nodes (A, B, C, Q, Q2)", ""
    A, B, C, Q, Q2 = ctx.senders[:5]
    port, V = ctx.port, 20.0
    # The pledge is released when a LATER transaction spends a token of the
    # pledged one (core/callback.go). B's onward send takes its lowest whole
    # token_ids first (core/wallet/token_lock.go), so a B that already holds
    # tokens spends its own older ones and Q's pledge is never touched. B and Q
    # play from DIDs holding no whole token; Q is given exactly V.
    B = ch.empty_did(B, whole_only=True)
    Q = ch.empty_did(Q, whole_only=True)
    if B is None or Q is None:
        return SKIP, "no DID on {}'s node is free of whole tokens - the case follows the exact " \
            "tokens pledged and paid".format("B" if B is None else "Q"), ""
    state = {}

    def setup(st):
        ok, msg = rc.quorum_setup(Q["host"], Q["did"], port)
        st.check(ok, "Q set up: {}".format(msg))
        for host, qdid in ((A["host"], Q["did"]), (Q["host"], Q2["did"]), (B["host"], Q2["did"])):
            rc.quorum_reset(host, [qdid], port)
        ok, msg = rc.fund_did(Q["host"], Q["did"], V, port)
        st.check(ok, "Q funded with {:.0f}: {}".format(V, msg))
    s1 = run.step("S1", setup, nodes=[Q], watch=False,
                  require=[lambda: _as_quorum(Q2, 100, port)])
    if s1.outcome != steps.PASS or not all(steps.wired(h, d, port)[0] for h, d in (
            (A["host"], Q["did"]), (Q["host"], Q2["did"]), (B["host"], Q2["did"]))):
        return run.verdict()

    def pledge(st):
        ok, _m, _r = ch.send(st, A, B, rbt=V, memo="QCAP-03b A->B")
        paid = set()
        for _ in range(20):
            paid = set(ch.whole_free(B["host"], B["did"]))
            if len(paid) >= int(V):
                break
            time.sleep(2)
        state["paid"] = paid
        if ok:
            st.check(len(paid) == int(V), "B holds {} whole tokens, not the {:.0f} A paid".format(
                len(paid), V))
    s2 = run.step("S2", pledge, nodes=[A, B, Q], must_accept=True)
    if s2.outcome != steps.PASS:
        return run.verdict()

    def spend_pledged(st):
        p0 = db.pledged_value(Q["host"], Q["did"])
        ch.send(st, Q, C, rbt=V, memo="QCAP-03b pledged send")
        st.check(db.pledged_value(Q["host"], Q["did"]) >= p0 - steps.TOL,
                 "Q's pledge shrank when it tried to spend it")
    run.step("S3", spend_pledged, nodes=[Q, C, Q2], expect_refusal=True)

    def release(st):
        ok, _m, _r = ch.send(st, B, C, rbt=V, memo="QCAP-03b release")
        if not ok:
            return
        time.sleep(2)
        left = state["paid"] & set(ch.whole_free(B["host"], B["did"]))
        if left:
            st.skip("B's onward send did not move the {} token(s) A paid it, so it cannot release "
                    "Q's pledge".format(len(left)))
            return
        t0, released = time.time(), False
        for _ in range(60):
            if db.pledged_value(Q["host"], Q["did"]) < V - steps.TOL:
                released = True
                break
            time.sleep(2)
        state["released"] = released
        st.data["release_seconds"] = round(time.time() - t0, 1) if released else None
        st.check(released, "Q's pledge was not released within 120s of the onward spend (if Q's "
                           "node has other quorum DIDs set up, the release checks one of them at "
                           "random - core/fullnode.go:69)")
        if released:
            st.note("released after {:.0f}s".format(time.time() - t0))
    run.step("S4", release, nodes=[B, C, Q, Q2], must_accept=True)
    if not state.get("released"):
        return run.verdict()

    run.step("S5", lambda st: ch.send(st, Q, C, rbt=1.0, memo="QCAP-03b whole after release"),
             nodes=[Q, C], must_accept=True)
    run.step("S6", lambda st: ch.send(st, Q, C, rbt=0.5, memo="QCAP-03b split after release"),
             nodes=[Q, C], must_accept=True)
    return run.verdict(passed="pledged RBT is unspendable until released, and released once the "
                              "receiver spends onward")


CASES = {"QCAP-01": qcap_01, "QCAP-02": qcap_02, "QCAP-03a": qcap_03a, "QCAP-03b": qcap_03b}
ORDER = ["QCAP-01", "QCAP-02", "QCAP-03a", "QCAP-03b"]
NEEDS = {
    # Ring: every sender pays the next, so it also receives - no receivers of
    # its own. The case tops the quorum up before each rung for what the rung
    # can pledge (a pledge is held until the receiver spends that token onward).
    "QCAP-01": {"senders": QCAP_SENDERS, "receivers": 0, "quorums": 1, "fund": 3,
                "quorum_fund": 500, "exclusive": True},
    # Five quorums, each topped to ~123 whole from the faucet (poorest DIDs, so
    # the floor sets the holding rather than a big prior balance); the case
    # funds each sender for the value it pays.
    "QCAP-02": {"senders": 5, "receivers": 5, "quorums": 5, "fund": 5,
                "quorum_fund": 123, "quorum_pick": "poorest"},
    # Five nodes the case wires itself (A, B, C, Q, Q2); no runner quorum.
    "QCAP-03a": {"senders": 5, "receivers": 0, "quorums": 0, "fund": 60},
    "QCAP-03b": {"senders": 5, "receivers": 0, "quorums": 0, "fund": 60},
}
# Raw capacity / boundary / wiring: a delayed rerun would measure the fullnodes
# or re-run the manual wiring, not the product.
NO_DELAY_RERUN = {"QCAP-01", "QCAP-02", "QCAP-03a", "QCAP-03b"}
