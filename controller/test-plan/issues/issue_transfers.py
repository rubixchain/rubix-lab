"""issue_transfers.py - issues in paying RBT from one wallet to another, each
checked on its own with tokens whose history is long and proper (issue_kit.py).

A check FAILS when Rubix behaves as the issue says (the issue is reproduced),
PASSES when it behaves correctly, and is NOT RUN when its set-up could not be
made - never blamed on Rubix.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import issue_kit as kit_mod
from issue_kit import case_evidence, ch, fail_cases, fleet_health, rc, steps

SKIP = "SKIP"
ROUNDS = 5              # RELEASE-ONE-OF-MANY: pledges made and released
SMALL = 40              # SMALL-SPLITS: small payments in a row, then again
CLOCK_PAYMENTS = 150    # CLOCK-EDGE
HOPS = 10               # FULLNODES-ORDER
RELEASE_WAIT = 120      # seconds a pledge gets to come back after the onward payment


def _send(st, frm, to, amount, memo, set_aside=ch.ASIDE, timeout=ch.TRANSFER_TIMEOUT, q=None):
    """One judged payment. `q`, the quorum that signs it, is topped up first
    (kit_mod.afford) - except where what the quorum holds is the point."""
    kit_mod.afford(q, amount)
    ok, msg, res = ch.send(st, frm, to, rbt=amount, memo=memo, set_aside=set_aside, timeout=timeout)
    return ok, msg, kit_mod.txid_of(res)


def _wait_whole(e, k=1, wait=30):
    """The wallet holds k whole tokens Free (a payment it received has landed)."""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            if len(ch.whole_free(e["host"], e["did"])) >= k:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _wait_pledged(e, want, wait):
    """Seconds until the DID's Pledged RBT is within 0.0015 of `want`, or None."""
    t0 = time.time()
    while time.time() - t0 < wait:
        v = kit_mod.pledged(e)
        if v is not None and abs(v - want) < 0.0015:
            return round(time.time() - t0, 1)
        time.sleep(3)
    return None


# ---------------------------------------------------------------------------

PURPOSE = {
    "PAY-TO-NODE-DOWN":
        "A payment to a wallet whose node is down is reported done, and the tokens never reach "
        "it. The sender marks its tokens Transferred, reports success, and only then pushes them "
        "to the receiver, once (core/transaction.go:79, 'Receiver offline, will sync later'); "
        "nothing tries again.",
    "RECEIVE-OLD-COPY":
        "A token paid to a wallet on a node that once checked that token as quorum is refused "
        "there, and lost. The quorum's node keeps a row for the token frozen at the hop it "
        "checked; the receipt is compared with that row ('previous_transaction_id mismatch', "
        "core/wallet/post_consensus_persistence.go:484) while the payer has already marked the "
        "token Transferred.",
    "QUORUM-STOPPED":
        "A quorum stopped in the middle of a payment refuses that token ever after ('chain "
        "mismatch after sync', core/consensus/checks.go:944): its copy of the token's chain keeps "
        "the payment that never completed. It also leaves its own pledge Locked.",
    "EX-QUORUM-SPEND":
        "A wallet that was a quorum cannot spend the token it pledged once the pledge is back: "
        "'failed to get quorum DID for token' - the check looks the token up in the current "
        "transaction's pledges instead of the pledge transaction's (core/consensus/checks.go:309). "
        "A payment that splits the token is not checked and goes through.",
    "RELEASE-ONE-OF-MANY":
        "On a node with several quorum DIDs set up, a pledge comes back only if the node happens "
        "to check the right one: per published transaction it checks ONE of them, picked in Go "
        "map order (core/fullnode.go:66-72), and never tries again.",
    "EXACT-DECIMAL":
        "Paying exactly what a set of part tokens adds up to is refused 'insufficient balance': "
        "Rubix adds the parts' values as binary floats with no rounding (core/parts/parts.go:68-80) "
        "- three 0.01 parts and one 0.005 come to 0.034999999999999996, not 0.035. The same check "
        "refuses a quorum's pledge.",
    "SMALL-SPLITS":
        "One wallet making many small part payments in a row, and splitting returned parts again: "
        "seen once as a false double spend ('ancestor ... already consumed', "
        "core/consensus/checks.go:1119), and in an older build as a duplicate split row "
        "(tokens_pkey).",
    "CLOCK-EDGE":
        "A payer whose machine's clock is a few milliseconds ahead of its quorum's is refused "
        "'invalid epoch': the epoch is whole seconds and may not be later than the quorum's clock "
        "(core/consensus/checks.go:61), so about one payment in 1000/d is refused for a d ms "
        "difference.",
    "FULLNODES-ORDER":
        "A token passed on quickly, hop after hop: the fullnodes check transactions in parallel "
        "with a few retries (core/fullnode.go processTxnWithRetry), so a hop can be checked before "
        "the one before it - rejected 'chain mismatch after sync', or left out, and the four "
        "fullnodes disagree.",
    "QUORUM-SETUP":
        "A quorum forgets its set-up when its node restarts (it lives in memory only, "
        "core/quorum_initiator.go:540-555), and every payment through it is refused 'Quorum is not "
        "setup' until someone sets it up again. Adding a quorum a node already has answers an "
        "error (core/wallet/quorum.go:19).",
}

STEPS = {
    "PAY-TO-NODE-DOWN": [
        ("S1", "Set-up",
         "A = an empty wallet given one token with a long history; B = a DID on another machine, "
         "its node restarted first; Q = the quorum, on a third machine; A's node signs with Q",
         "the participants exist"),
        ("S2", "Pay a wallet whose node is down",
         "stop B's node; A pays B 1 RBT through Q; start B's node again and wait until it answers "
         "and can receive",
         "refused with a reason that says B cannot be reached, or accepted and credited to B within "
         "60s of B being back"),
        ("S3", "Where the token is",
         "read the token's rows on A's and B's nodes, and A's log for the payment",
         "the token is B's (or still A's, if refused) - never held by nobody"),
    ],
    "RECEIVE-OLD-COPY": [
        ("S1", "Set-up",
         "Q = a quorum whose node hosts another DID, X, emptied; Q2 = a second quorum; A = an empty "
         "wallet given one token T with a long history (through Q2); B = an empty wallet elsewhere",
         "the participants exist"),
        ("S2", "T passes through Q",
         "A pays B 1 RBT (T) through Q; read Q's node's rows for T",
         "accepted and credited to B; Q's node's rows for T are listed"),
        ("S3", "T paid to a wallet on Q's node",
         "B pays X 1 RBT (T) through Q2",
         "accepted and credited to X"),
        ("S4", "Control: a token Q's node never checked",
         "B is given another long-history token through Q2, then pays it to X through Q2",
         "accepted and credited to X"),
    ],
    "QUORUM-STOPPED": [
        ("S1", "Set-up",
         "Q = a quorum, its node restarted first; Q2 = another quorum; A = an empty wallet given one "
         "token T with a long history (through Q2); B = a DID on another machine",
         "the participants exist"),
        ("S2", "Stop Q when it takes the request",
         "A pays B 1 RBT through Q; Q's node is stopped on its log line 'Consensus request parsed "
         "successfully', then started and set up again",
         "refused with a stated reason; nothing left Locked"),
        ("S3", "T through Q again",
         "read T's chain on A's node and on Q's; A pays B 1 RBT (T) through Q",
         "accepted and credited to B"),
        ("S4", "T through another quorum",
         "A pays B 1 RBT (T) through Q2", "accepted and credited to B"),
    ],
    "EX-QUORUM-SPEND": [
        ("S1", "Set-up",
         "Q = an empty wallet given one token T with a long history, its node restarted so Q is the "
         "only quorum set up there, then set up as quorum; Q2 = another quorum; A = a wallet whose "
         "next token has a long history; B = an empty wallet; C, D = DIDs on other machines",
         "the participants exist"),
        ("S2", "Q pledges T", "A pays B 1 RBT through Q", "accepted; Q's Pledged RBT rises by 1"),
        ("S3", "The pledge comes back",
         "B pays C the token it got, through Q2; wait for Q's Pledged RBT to fall back",
         "accepted; Q's pledge released within {}s".format(RELEASE_WAIT)),
        ("S4", "Q pays with T", "Q pays D 1 RBT through Q2", "accepted and credited to D"),
        ("S5", "Q pays part of T", "only if S4 was refused: Q pays D 0.5 RBT through Q2",
         "recorded: whether a split of the same token goes through"),
    ],
    "RELEASE-ONE-OF-MANY": [
        ("S1", "Set-up",
         "N = a node hosting 5 or more DIDs, restarted, every DID on it set up as quorum; Q = the "
         "one of them that signs; Q2 = another quorum; A = a wallet whose next {} tokens have long "
         "histories; B = an empty wallet; C = a DID on another machine".format(ROUNDS),
         "the participants exist"),
        ("S2", "{} pledges and their release".format(ROUNDS),
         "{} times: A pays B 1 RBT through Q (Q pledges 1 RBT); B pays C that token through Q2; "
         "wait up to {}s for Q's pledge to come back".format(ROUNDS, RELEASE_WAIT),
         "every payment accepted; every pledge released"),
    ],
    "EXACT-DECIMAL": [
        ("S1", "Set-up",
         "S = an empty wallet given 3 long-history tokens; from S, through Q: E1 gets three 0.01 "
         "parts and one 0.005; E2 one whole token, two 0.5 parts, one 0.01 and one 0.005; Qx three "
         "0.01 and one 0.005 and is set up as quorum; A = a wallet whose next token has a long "
         "history; R = a DID to pay",
         "each wallet holds exactly those parts"),
        ("S2", "Pay 0.035 from three 0.01 and one 0.005", "E1 pays R 0.035 through Q",
         "accepted and credited"),
        ("S3", "Pay 2.015 from one whole, two 0.5, one 0.01 and one 0.005",
         "E2 pays R 2.015 through Q", "accepted and credited"),
        ("S4", "A quorum pledges exactly what it holds",
         "A pays R 0.035 through Qx, which holds exactly 0.035", "accepted and credited"),
    ],
    "SMALL-SPLITS": [
        ("S1", "Set-up",
         "E = an empty wallet given 2 long-history tokens; F = an empty wallet on another machine; "
         "E signs with Q, F with Q2", "the participants exist"),
        ("S2", "{} small payments in a row".format(SMALL),
         "E pays F 0.013 RBT {} times, each after the last returned".format(SMALL),
         "every payment accepted and credited"),
        ("S3", "Parts paid back", "F pays E 0.1 RBT 3 times", "accepted and credited"),
        ("S4", "Split the returned parts again",
         "E pays F 0.007 RBT {} times".format(SMALL // 2), "accepted and credited"),
    ],
    "CLOCK-EDGE": [
        ("S1", "Set-up",
         "measure every pool machine's clock (fleet median as reference); Q = a quorum on the "
         "machine furthest behind; A and B = empty wallets on the machine furthest ahead, each given "
         "one long-history token",
         "the two machines differ by at least 5 ms"),
        ("S2", "{} payments, payer ahead of the quorum".format(CLOCK_PAYMENTS),
         "A and B pay each other 1 RBT in turn through Q, {} times".format(CLOCK_PAYMENTS),
         "every payment accepted (Rubix allows a small clock difference)"),
    ],
    "FULLNODES-ORDER": [
        ("S1", "Set-up",
         "E1 = an empty wallet given one long-history token T; E2 = an empty wallet on another "
         "machine; E1 signs with Q, E2 with Q2", "the participants exist"),
        ("S2", "{} quick hops".format(HOPS),
         "E1 and E2 pass T back and forth {} times, each hop as soon as the last one landed".format(HOPS),
         "every hop accepted and credited"),
        ("S3", "What every fullnode made of it",
         "wait up to 180s; read every fullnode's verdict on each hop and its copy of T's chain",
         "every fullnode accepted every hop and holds T's chain to its last entry"),
    ],
    "QUORUM-SETUP": [
        ("S1", "Set-up",
         "Q = a quorum; A = a wallet whose next 2 tokens have long histories; B = a DID on another "
         "machine; N = a node to add a quorum to", "the participants exist"),
        ("S2", "Add the same quorum twice", "N adds Q to its quorum list, then adds it again",
         "both answer success (the second changes nothing)"),
        ("S3", "Pay through a quorum whose node restarted",
         "A pays B 1 RBT through Q; restart Q's node; A pays B 1 RBT through Q again, without "
         "setting Q up again", "both accepted and credited"),
    ],
}

CASE_INFO = {
    "PAY-TO-NODE-DOWN": (
        "A payment to a wallet whose node is down is reported as done, and the tokens never reach "
        "the wallet - not even after its node is back.",
        "Rubix refuses the payment with a reason saying the receiver cannot be reached, or delivers "
        "it once the receiver's node is back."),
    "RECEIVE-OLD-COPY": (
        "A token paid to a wallet on a node that once checked that token as quorum is refused by "
        "that node, while the payer has already given it up - the token is lost.",
        "The wallet on that node receives the token like any other."),
    "QUORUM-STOPPED": (
        "A quorum stopped in the middle of a payment refuses that token from then on, and leaves "
        "its pledge Locked.",
        "After the quorum is back, the same token goes through it; nothing stays Locked."),
    "EX-QUORUM-SPEND": (
        "A wallet that was a quorum cannot spend the token it pledged, once the pledge is back.",
        "The token, Free again, can be spent like any other."),
    "RELEASE-ONE-OF-MANY": (
        "On a node with several quorum DIDs, a pledge often never comes back after the receiver "
        "spends onward.",
        "Every pledge comes back once the token it covered is spent onward."),
    "EXACT-DECIMAL": (
        "Paying exactly what a wallet's part tokens add up to (0.035, 2.015) is refused "
        "'insufficient balance'; a quorum holding exactly the amount is refused the same way.",
        "A wallet can pay exactly what it holds; a quorum can pledge exactly what it holds."),
    "SMALL-SPLITS": (
        "Many small part payments from one wallet in a row are refused as double spends, or fail "
        "on a duplicate split row.",
        "Every small payment goes through."),
    "CLOCK-EDGE": (
        "A payer whose machine's clock is a few milliseconds ahead of its quorum's is refused "
        "'invalid epoch'.",
        "Payments go through when two machines' clocks differ by less than a second."),
    "FULLNODES-ORDER": (
        "A token passed on quickly: the fullnodes reject some hops, fall behind, or disagree with "
        "each other.",
        "Every fullnode accepts every hop and ends with the token's full chain."),
    "QUORUM-SETUP": (
        "A quorum forgets its set-up when its node restarts, and payments through it are refused "
        "until it is set up again; adding a quorum twice answers an error.",
        "A quorum signs again after its node restarts; adding a quorum twice is harmless."),
}


# ---------------------------------------------------------------------------
# PAY-TO-NODE-DOWN
# ---------------------------------------------------------------------------

def pay_to_node_down(ctx, ci):
    case = "PAY-TO-NODE-DOWN"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q, why = kit.quorum(need=5)
        if not Q:
            return False, why
        A, why = kit.one_token_wallet(kit.other_host(), Q, role="A (payer)")
        if not A:
            return False, why
        B, why = kit.wallet_on(role="B (payee)", tokens=kit.tokens_of(A))
        if not B:
            return False, why
        ok, why = kit.restart(B["host"])
        if not ok:
            return False, why
        ok, why = kit.wire([A], Q)
        if not ok:
            return False, why
        p.update(A=A, B=B, Q=Q, notes=[kit_mod.who(A, "A"), kit_mod.who(B, "B"), kit_mod.who(Q, "Q"),
                                       "A's token {} has {} chain entries".format(*A["tokens"][0])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, Q = p["A"], p["B"], p["Q"]
    token = A["tokens"][0][0]

    def pay(st):
        ok, why = kit_mod.stop(B["host"])
        if not ok:
            st.skip("stopping B's node failed: " + why)
            return
        time.sleep(3)
        kit.note_moved(A, B, A["tokens"], "the payment to a node that is down")
        ok, msg, txid = _send(st, A, B, 1.0, case, timeout=fail_cases.CLIENT_WAIT, q=Q)
        p["ok"], p["txid"] = ok, txid
        kit_mod.start(B["host"])
        back, secs = kit_mod.wait_back(B["host"])
        st.check(back, "B's node did not answer and receive again within {}s of its start".format(
            kit_mod.UP_WAIT))
        st.note("the payment was {} while B's node was down{}".format(
            "accepted" if ok else "refused", "" if ok else ": " + ch.node_reason(msg)))
    run.step("S2", pay, nodes=[A, B, Q], restarts=[B["host"]])

    def where(st):
        st.data["payer_rows"] = kit_mod.rows_on(A["host"], token)
        st.data["payee_rows"] = kit_mod.rows_on(B["host"], token)
        st.note("A's node: {}".format(st.data["payer_rows"] or "no row"))
        st.note("B's node: {}".format(st.data["payee_rows"] or "no row"))
        if p.get("txid"):
            lines = kit_mod.log_lines(A["host"], p["txid"], "sendTokensToReceiver")
            st.data["payer_log"] = lines
            if lines:
                st.note("A's log: " + lines[-1][:240])
            mine = [r for r in st.data["payee_rows"] if r[0] == B["did"]]
            gone = [r for r in st.data["payer_rows"] if r[0] == A["did"] and r[1] == "Transferred"]
            st.check(mine or not gone, "token {} is Transferred on A's node and B's node holds no "
                                       "row for it - it is held by nobody".format(token))
    run.step("S3", where, nodes=[], watch=False, deliver=False, required=False)
    return kit_mod.verdict(run, kit, "the payment was reported done and never reached B",
                           "Rubix refused the payment, or delivered it once B was back")


# ---------------------------------------------------------------------------
# RECEIVE-OLD-COPY
# ---------------------------------------------------------------------------

def receive_old_copy(ctx, ci):
    case = "RECEIVE-OLD-COPY"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        Q, why = kit.quorum(need=5, min_dids=2, role="Q (quorum)")
        if not Q:
            return False, why
        X, why = kit.empty(Q["host"], Q2, role="X (payee on Q's node)")
        if not X:
            return False, why
        # T must be new to Q's node: the copy it keeps comes from S2, nothing earlier
        A, why = kit.one_token_wallet(kit.other_host(), Q2, role="A (payer)", clear_of=[Q["host"]])
        if not A:
            return False, why
        B, why = kit.empty(kit.clear_host(kit.tokens_of(A)), Q2, role="B")
        if not B:
            return False, why
        for e, q in ((A, Q), (B, Q2)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        p.update(A=A, B=B, X=X, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((A, "A"), (B, "B"), (X, "X"), (Q, "Q"),
                                                        (Q2, "Q2"))] +
                 ["T = {} ({} chain entries)".format(*A["tokens"][0])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, X, Q, Q2 = p["A"], p["B"], p["X"], p["Q"], p["Q2"]
    T = A["tokens"][0][0]

    def through_q(st):
        kit.note_moved(A, B, A["tokens"], "T through Q")
        ok, msg, txid = _send(st, A, B, 1.0, case + " T through Q", q=Q)
        if ok and kit_mod.delivered(B, txid, 60):
            rows = kit_mod.rows_on(Q["host"], T)
            st.data["q_node_rows"] = rows
            st.note("Q's node keeps {} row(s) for T: {}".format(len(rows), rows or "none"))
    s2 = run.step("S2", through_q, nodes=[A, B, Q], must_accept=True)

    def to_q_node(st):
        kit.note_moved(B, X, [(T, (kit_mod.tip_on(B["host"], T) or (0,))[0] + 1)], "T to Q's node")
        ok, msg, txid = _send(st, B, X, 1.0, case + " T to Q's node", q=Q2)
        p["txid"] = txid
        if ok and not kit_mod.delivered(X, txid, 60):
            lines = kit_mod.log_lines(X["host"], T, "SendTokens")
            st.data["payee_node_log"] = lines
            if lines:
                st.note("X's node: " + lines[-1][:260])
            said = kit_mod.log_lines(B["host"], txid, "sendTokensToReceiver")
            if said:
                st.note("B's log: " + said[-1][:260])
            st.data["x_node_rows"] = kit_mod.rows_on(X["host"], T)
    run.step("S3", to_q_node, nodes=[B, X, Q2], must_accept=True,
             require=[(s2.outcome == steps.PASS, "T did not pass through Q in S2")])

    def control(st):
        got, why = kit.fill(B, 1, Q2, why="the control token", clear_of=[X["host"]])
        if not got:
            st.skip(why)
            return
        kit.note_moved(B, X, got["tokens"], "the control token to Q's node")
        _send(st, B, X, 1.0, case + " control", q=Q2)
    run.step("S4", control, nodes=[B, X, Q2], must_accept=True, required=False)
    return kit_mod.verdict(run, kit, "Q's node refused the token it had checked as quorum, and the "
                                     "payer had already given it up",
                           "the wallet on Q's node received the token")


# ---------------------------------------------------------------------------
# QUORUM-STOPPED
# ---------------------------------------------------------------------------

def quorum_stopped(ctx, ci):
    case = "QUORUM-STOPPED"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        Q, why = kit.quorum(need=5, fresh=True, role="Q (quorum)")
        if not Q:
            return False, why
        A, why = kit.one_token_wallet(kit.other_host(), Q2, role="A (payer)")
        if not A:
            return False, why
        B, why = kit.wallet_on(role="B (payee)", tokens=kit.tokens_of(A))
        if not B:
            return False, why
        ok, why = kit.wire([A], Q)
        if not ok:
            return False, why
        p.update(A=A, B=B, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((A, "A"), (B, "B"), (Q, "Q"), (Q2, "Q2"))] +
                 ["T = {} ({} chain entries)".format(*A["tokens"][0])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, Q, Q2 = p["A"], p["B"], p["Q"], p["Q2"]
    T = A["tokens"][0][0]
    machine, slot = ch.machine(Q["host"]), kit_mod.slot(Q["host"])

    def stop_q(st):
        result, killer = {}, {}

        def pay():
            t0 = time.time()
            ok, msg, res = rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=1.0, memo=case,
                                                   timeout=fail_cases.CLIENT_WAIT)
            result.update(ok=ok, msg=msg, secs=round(time.time() - t0, 2), txid=kit_mod.txid_of(res))

        def kill():
            killer["how"] = fail_cases._stop_on_marker(machine, slot, fail_cases.MARK_QUORUM, 0.0, 40)
        kit.note_moved(A, B, A["tokens"], "the payment Q is stopped in")
        kit_mod.afford(Q, 1.0)
        k, t = threading.Thread(target=kill), threading.Thread(target=pay)
        k.start()
        time.sleep(0.4)             # let the remote tail attach first
        t.start()
        k.join(timeout=70)
        t.join(timeout=fail_cases.CLIENT_WAIT + 20)
        fail_cases._start(machine, slot)
        back, _s = kit_mod.wait_back(Q["host"])
        st.note(killer.get("how") or "the stop did not report")
        st.check(back, "Q's node did not answer and receive again within {}s".format(kit_mod.UP_WAIT))
        if result:
            st.op(result["ok"], result["msg"], result["secs"], result["txid"], to=B["did"])
        else:
            st.op(False, "no answer within {}s".format(fail_cases.CLIENT_WAIT), fail_cases.CLIENT_WAIT)
        p["first"] = bool(result.get("ok"))
        rc.quorum_setup(Q["host"], Q["did"])
    run.step("S2", stop_q, nodes=[A, B, Q], restarts=[Q["host"]], budget=fail_cases.BUDGET)

    def again(st):
        mine, copy = kit_mod.tip_on(A["host"], T), kit_mod.tip_on(Q["host"], T)
        st.data.update(payer_tip=mine, quorum_tip=copy)
        st.note("T's chain ends at position {} on A's node, at {} on Q's".format(
            mine[0] if mine else "-", copy[0] if copy else "-"))
        _send(st, A, B, 1.0, case + " again through Q", q=Q)
    s3 = run.step("S3", again, nodes=[A, B, Q], must_accept=True,
                  require=[lambda: (not p.get("first"), "the payment went through before Q stopped - "
                                                        "nothing is left to send again"),
                           lambda: steps.quorum_ready(Q, 1)])

    def other(st):
        ok, why = kit.wire([A], Q2)
        if not ok:
            st.skip(why)
            return
        _send(st, A, B, 1.0, case + " through another quorum", q=Q2)
    run.step("S4", other, nodes=[A, B, Q2], must_accept=True, required=False,
             require=[(s3.outcome in (steps.WRONG, steps.DAMAGE), "S3 went through")])
    return kit_mod.verdict(run, kit, "after the stop Q kept refusing the token, or left RBT Locked",
                           "Q took the same token again after its restart")


# ---------------------------------------------------------------------------
# EX-QUORUM-SPEND
# ---------------------------------------------------------------------------

def ex_quorum_spend(ctx, ci):
    case = "EX-QUORUM-SPEND"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        host = kit.other_host()
        ok, why = kit.restart(host) if host else (False, "no node left for Q")
        if not ok:
            return False, why
        Q, why = kit.one_token_wallet(host, Q2, role="Q (quorum holding one token)")
        if not Q:
            return False, why
        ok, why = kit.as_quorum(Q, 1)
        if not ok:
            return False, why
        A, why = kit.holders(1)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.empty(kit.clear_host(kit.tokens_of(A)), Q2, role="B")
        if not B:
            return False, why
        C, why = kit.wallet_on(role="C (payee)", tokens=kit.tokens_of(A))
        if not C:
            return False, why
        D, why = kit.wallet_on(role="D (payee)", tokens=kit.tokens_of(Q))
        if not D:
            return False, why
        p.update(A=A, B=B, C=C, D=D, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((Q, "Q"), (A, "A"), (B, "B"), (C, "C"),
                                                        (D, "D"), (Q2, "Q2"))] +
                 ["Q's token {} has {} chain entries".format(*Q["tokens"][0])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, C, D, Q, Q2 = (p[k] for k in ("A", "B", "C", "D", "Q", "Q2"))

    def pledge(st):
        ok, why = kit.wire([A], Q)
        if not ok:
            st.skip(why)
            return
        kit.note_moved(A, B, A["tokens"], "Q pledges")
        ok, _m, _t = _send(st, A, B, 1.0, case + " Q pledges")
        if ok:
            st.check(_wait_pledged(Q, 1.0, 30) is not None, "Q's Pledged RBT did not rise to 1")
    s2 = run.step("S2", pledge, nodes=[A, B, Q], must_accept=True)

    def release(st):
        ok, why = kit.wire([B], Q2)
        if not ok:
            st.skip(why)
            return
        if not _wait_whole(B):
            st.skip("B never showed the token it was paid")
            return
        ok, _m, _t = _send(st, B, C, 1.0, case + " onward", q=Q2)
        if ok:
            secs = _wait_pledged(Q, 0.0, RELEASE_WAIT)
            p["released"] = secs is not None
            st.check(secs is not None, "Q's pledge was not released within {}s of the onward "
                                       "payment".format(RELEASE_WAIT))
            if secs is not None:
                st.note("Q's pledge came back {}s after the onward payment".format(secs))
    run.step("S3", release, nodes=[B, C, Q, Q2], must_accept=True,
             require=[(s2.outcome == steps.PASS, "Q did not pledge in S2")])

    def spend(st):
        ok, why = kit.wire([Q], Q2)
        if not ok:
            st.skip(why)
            return
        kit.note_moved(Q, D, [(Q["tokens"][0][0], (kit_mod.tip_on(Q["host"], Q["tokens"][0][0]) or (0,))[0] + 1)],
                       "the former quorum pays with its token")
        _send(st, Q, D, 1.0, case + " Q pays", set_aside=(), q=Q2)
    s4 = run.step("S4", spend, nodes=[Q, D, Q2], must_accept=True,
                  require=[lambda: (p.get("released"), "Q's pledge never came back in S3")])

    def part(st):
        _send(st, Q, D, 0.5, case + " Q pays a part", set_aside=(), q=Q2)
    run.step("S5", part, nodes=[Q, D, Q2], must_accept=True, required=False,
             require=[(s4.outcome == steps.WRONG, "S4 went through")])
    return kit_mod.verdict(run, kit, "the former quorum could not spend the token it pledged",
                           "the former quorum spent the token it pledged")


# ---------------------------------------------------------------------------
# RELEASE-ONE-OF-MANY
# ---------------------------------------------------------------------------

def release_one_of_many(ctx, ci):
    case = "RELEASE-ONE-OF-MANY"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        host = kit.other_host(min_dids=5)
        if not host:
            return False, "no free node hosts 5 or more DIDs"
        ok, why = kit.restart(host)
        if not ok:
            return False, why
        dids = kit_mod.nodes(ctx)[host]
        armed = [d for d in dids if rc.quorum_setup(host, d)[0]]
        if len(armed) < 2:
            return False, "only {} of {}'s DIDs could be set up as quorum".format(len(armed), kit_mod.short(host))
        Q, why = kit.quorum(need=ROUNDS + 2, on=host, role="Q (quorum)")
        if not Q:
            return False, why
        A, why = kit.holders(1, k=ROUNDS)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.empty(kit.clear_host(kit.tokens_of(A)), Q2, role="B")
        if not B:
            return False, why
        C, why = kit.wallet_on(role="C (payee)", tokens=kit.tokens_of(A))
        if not C:
            return False, why
        p.update(A=A, B=B, C=C, Q=Q, Q2=Q2, armed=len(armed),
                 notes=["{} quorum DIDs set up on {}".format(len(armed), kit_mod.short(host))] +
                 [kit_mod.who(e, n) for e, n in ((Q, "Q"), (A, "A"), (B, "B"), (C, "C"), (Q2, "Q2"))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, C, Q, Q2 = (p[k] for k in ("A", "B", "C", "Q", "Q2"))

    def rounds(st):
        released, rows = 0, []
        for r in range(ROUNDS):
            base = kit_mod.pledged(Q) or 0.0
            ok, why = kit.wire([A], Q)
            if not ok:
                st.note(why)
                break
            kit.note_moved(A, B, A["tokens"][r:r + 1], "pledge {}".format(r + 1))
            ok, _m, _t = _send(st, A, B, 1.0, "{} pledge {}".format(case, r + 1), q=Q)
            if not ok or _wait_pledged(Q, base + 1.0, 30) is None or not _wait_whole(B):
                rows.append("round {}: no pledge to release".format(r + 1))
                continue
            kit.wire([B], Q2)
            ok, _m, _t = _send(st, B, C, 1.0, "{} onward {}".format(case, r + 1), q=Q2)
            if not ok:
                continue
            secs = _wait_pledged(Q, base, RELEASE_WAIT)
            released += secs is not None
            rows.append("round {}: {}".format(r + 1, "released after {}s".format(secs) if secs is not None
                                              else "NOT released in {}s".format(RELEASE_WAIT)))
        st.data["rounds"] = rows
        made = len([r for r in rows if "no pledge" not in r])
        st.note("; ".join(rows))
        st.check(released == made, "{} of {} pledges came back with {} quorum DIDs set up on Q's node "
                                   "(the node checks one of them per transaction, "
                                   "core/fullnode.go:66-72)".format(released, made, p["armed"]))
    run.step("S2", rounds, nodes=[A, B, C, Q, Q2], must_accept=True)
    return kit_mod.verdict(run, kit, "pledges did not come back with several quorums set up on the node",
                           "every pledge came back")


# ---------------------------------------------------------------------------
# EXACT-DECIMAL
# ---------------------------------------------------------------------------

E1_PARTS = (0.01, 0.01, 0.01, 0.005)                   # 0.035
E2_PARTS = (1.0, 0.5, 0.5, 0.01, 0.005)                # 2.015


def exact_decimal(ctx, ci):
    case = "EXACT-DECIMAL"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def give(S, to, parts):
        for v in parts:
            kit_mod.afford(p.get("Q"), v)
            ok, msg, _r = rc.initiate_transaction(S["host"], S["did"], to["did"], rbt=v,
                                                  memo="{}: parts".format(case),
                                                  timeout=ch.TRANSFER_TIMEOUT)
            if not ok:
                return False, "giving {} RBT to {} was refused: {}".format(v, kit_mod.short(to["host"]),
                                                                          ch.node_reason(msg))
        want = sorted(round(v, 3) for v in parts)
        got = []
        for _ in range(30):
            got = sorted(round(v, 3) for _t, v in kit_mod.free_parts(to))
            if got == want:
                return True, ""
            time.sleep(2)
        return False, "{} holds {} instead of {}".format(kit_mod.short(to["host"]), got, want)

    def setup():
        Q, why = kit.quorum(need=5)
        if not Q:
            return False, why
        p["Q"] = Q
        S, why = kit.one_token_wallet(kit.other_host(), Q, k=3, role="S (source of the parts)")
        if not S:
            return False, why
        wallets = {}
        for name, parts in (("E2", E2_PARTS), ("E1", E1_PARTS), ("Qx", E1_PARTS)):
            e, why = kit.empty(kit.other_host(), Q, role=name)
            if not e:
                return False, why
            ok, why = kit.wire([S], Q)
            if not ok:
                return False, why
            ok, why = give(S, e, parts)
            if not ok:
                return False, why
            wallets[name] = e
        kit.note_moved(S, S, S["tokens"], "split into the parts E1, E2 and Qx hold")
        ok, why = kit.as_quorum(wallets["Qx"], 0.035)
        if not ok:
            return False, "Qx could not be set up as quorum: {}".format(why)
        A, why = kit.holders(1)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        # E2's whole token moves to R whole: R's node must keep no copy of it
        whole = [t for t, v in kit_mod.free_parts(wallets["E2"]) if v >= 1.0 - 1e-9]
        R, why = kit.wallet_on(role="R (payee)", tokens=whole)
        if not R:
            return False, why
        ok, why = kit.wire([wallets["E1"], wallets["E2"]], Q)
        if not ok:
            return False, why
        p.update(Q=Q, A=A, R=R, S=S, **wallets)
        p["notes"] = [kit_mod.who(e, n) for n, e in sorted(wallets.items())] + [
            kit_mod.who(R, "R"), kit_mod.who(Q, "Q"),
            "the parts were split from S's tokens {}".format(
                ", ".join("{} ({} chain entries)".format(t, c) for t, c in S["tokens"]))]
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    Q, A, R, E1, E2, Qx = (p[k] for k in ("Q", "A", "R", "E1", "E2", "Qx"))

    run.step("S2", lambda st: _send(st, E1, R, 0.035, case + " 0.035", q=Q), nodes=[E1, R, Q],
             must_accept=True)
    run.step("S3", lambda st: _send(st, E2, R, 2.015, case + " 2.015", q=Q), nodes=[E2, R, Q],
             must_accept=True)

    def pledge(st):
        ok, why = kit.wire([A], Qx)
        if not ok:
            st.skip(why)
            return
        kit.note_moved(A, R, A["tokens"], "Qx pledges exactly what it holds")
        _send(st, A, R, 0.035, case + " Qx pledges 0.035")
    run.step("S4", pledge, nodes=[A, R, Qx], must_accept=True)
    return kit_mod.verdict(run, kit, "an exact amount of parts was refused 'insufficient balance'",
                           "every exact amount went through")


# ---------------------------------------------------------------------------
# SMALL-SPLITS
# ---------------------------------------------------------------------------

def small_splits(ctx, ci):
    case = "SMALL-SPLITS"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q, why = kit.quorum(need=5, role="Q (quorum)")
        if not Q:
            return False, why
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        E, why = kit.one_token_wallet(kit.other_host(), Q, k=2, role="E (payer)")
        if not E:
            return False, why
        F, why = kit.empty(kit.other_host(), Q2, role="F (payee)")
        if not F:
            return False, why
        for e, q in ((E, Q), (F, Q2)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        p.update(E=E, F=F, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((E, "E"), (F, "F"), (Q, "Q"), (Q2, "Q2"))] +
                 ["E's tokens: {}".format(", ".join("{} ({} chain entries)".format(t, c)
                                                    for t, c in E["tokens"]))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    E, F, Q, Q2 = p["E"], p["F"], p["Q"], p["Q2"]
    kit.note_moved(E, F, E["tokens"], "split into small parts")

    def series(st, frm, to, amount, n):
        q = Q if frm is E else Q2
        for i in range(n):
            _send(st, frm, to, amount, "{} {} #{}".format(case, amount, i + 1), q=q)
        refused = [o["reason"] for o in st.refused]
        for marker, words in (("already consumed", "refused as a double spend"),
                              ("tokens_pkey", "failed on a duplicate split row"),
                              ("duplicate key", "failed on a duplicate row")):
            hits = [r for r in refused if marker in r]
            if hits:
                st.note("{} payment(s) {}: {}".format(len(hits), words, hits[0][:200]))

    def first(st):
        series(st, E, F, 0.013, SMALL)
        said = kit_mod.log_lines(Q["host"], "cannot verify", last=3)
        if said:
            st.note("Q's log: a split accepted without the fullnode check - it asks Rubix's public "
                    "fullnode list, which this network cannot reach: " + said[-1][:200])
    run.step("S2", first, nodes=[E, F, Q], must_accept=True)
    run.step("S3", lambda st: series(st, F, E, 0.1, 3), nodes=[E, F, Q2], must_accept=True)
    run.step("S4", lambda st: series(st, E, F, 0.007, SMALL // 2), nodes=[E, F, Q], must_accept=True)
    return kit_mod.verdict(run, kit, "small part payments were refused",
                           "every small part payment went through")


# ---------------------------------------------------------------------------
# CLOCK-EDGE
# ---------------------------------------------------------------------------

def clock_edge(ctx, ci):
    case = "CLOCK-EDGE"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        pool = kit_mod.nodes(ctx)
        machines = sorted(set(ch.machine(h) for h in pool))
        _off, rel = fleet_health.clock_skew(machines)
        if len(rel) < 2:
            return False, "the machines' clocks could not be read"
        ahead = max(rel, key=rel.get)
        behind = min(rel, key=rel.get)
        d = rel[ahead] - rel[behind]
        p["d_ms"] = round(1000 * d, 1)
        if d < 0.005:
            return False, "the pool's clocks agree within {:.1f} ms - nothing to measure".format(1000 * d)
        Q = None
        for h in sorted(x for x in pool if ch.machine(x) == behind):
            Q, why = kit.quorum(need=5, on=h)
            if Q:
                break
        if not Q:
            return False, "no quorum could be set up on {}".format(behind)
        payers = []
        for h in sorted(x for x in pool if ch.machine(x) == ahead):
            if len(payers) == 2:
                break
            # each payer's token goes to the other's node: no old copy there
            if payers and kit_mod.copies([(t, h) for t in kit.tokens_of(payers[0])]):
                continue
            e, why = kit.one_token_wallet(h, Q, role="payer",
                                          clear_of=[x["host"] for x in payers])
            if e:
                payers.append(e)
        if len(payers) < 2:
            return False, "two payers with a long-history token could not be made on {}".format(ahead)
        ok, why = kit.wire(payers, Q)
        if not ok:
            return False, why
        p.update(A=payers[0], B=payers[1], Q=Q,
                 notes=["{} is {:.1f} ms ahead of {} (fleet median: {:+.1f} / {:+.1f} ms)".format(
                     ahead, 1000 * d, behind, 1000 * rel[ahead], 1000 * rel[behind]),
                     kit_mod.who(payers[0], "A"), kit_mod.who(payers[1], "B"), kit_mod.who(Q, "Q")])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, Q = p["A"], p["B"], p["Q"]
    kit.note_moved(A, B, A["tokens"], "passed back and forth")
    kit.note_moved(B, A, B["tokens"], "passed back and forth")

    def pay(st):
        for i in range(CLOCK_PAYMENTS):
            frm, to = (A, B) if i % 2 == 0 else (B, A)
            if not _wait_whole(frm, 1, 60):
                st.note("{} held no token to pay with after 60s".format(kit_mod.short(frm["host"])))
                break
            _send(st, frm, to, 1.0, "{} #{}".format(case, i + 1), set_aside=(), q=Q)
        n = len([o for o in st.ops if "invalid epoch" in (o["reason"] or "")])
        st.data.update(clock_ms=p["d_ms"], epoch_refusals=n)
        st.note("{} of {} refused 'invalid epoch' with the payer {:.1f} ms ahead (about {:.1f} expected "
                "if any difference is refused)".format(n, len(st.ops), p["d_ms"],
                                                       len(st.ops) * p["d_ms"] / 1000.0))
    run.step("S2", pay, nodes=[A, B, Q], must_accept=True)
    return kit_mod.verdict(run, kit, "a payer a few milliseconds ahead of its quorum was refused",
                           "no payment was refused for the clocks")


# ---------------------------------------------------------------------------
# FULLNODES-ORDER
# ---------------------------------------------------------------------------

def fullnodes_order(ctx, ci):
    case = "FULLNODES-ORDER"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        if not case_evidence.FULLNODE_HOSTS:
            return False, "no fullnode in hosts.txt"
        Q, why = kit.quorum(need=5, role="Q (quorum)")
        if not Q:
            return False, why
        Q2, why = kit.quorum(need=5, role="Q2 (quorum)")
        if not Q2:
            return False, why
        E1, why = kit.one_token_wallet(kit.other_host(), Q, role="E1")
        if not E1:
            return False, why
        E2, why = kit.empty(kit.clear_host(kit.tokens_of(E1)), Q2, role="E2")
        if not E2:
            return False, why
        for e, q in ((E1, Q), (E2, Q2)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        p.update(E1=E1, E2=E2, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((E1, "E1"), (E2, "E2"), (Q, "Q"), (Q2, "Q2"))] +
                 ["T = {} ({} chain entries)".format(*E1["tokens"][0])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    E1, E2, Q, Q2 = p["E1"], p["E2"], p["Q"], p["Q2"]
    T = E1["tokens"][0][0]
    kit.note_moved(E1, E2, E1["tokens"], "{} quick hops".format(HOPS))

    def hops(st):
        p["txids"] = []
        for i in range(HOPS):
            frm, to = (E1, E2) if i % 2 == 0 else (E2, E1)
            if not _wait_whole(frm, 1, 60):
                st.note("hop {}: {} never showed T".format(i + 1, kit_mod.short(frm["host"])))
                break
            ok, _m, txid = _send(st, frm, to, 1.0, "{} hop {}".format(case, i + 1),
                                 q=Q if frm is E1 else Q2)
            if not ok:
                break
            p["txids"].append(txid)
            p["holder"] = to
    run.step("S2", hops, nodes=[E1, E2, Q, Q2], must_accept=True)

    def verdicts(st):
        out = case_evidence.fullnode_outcome(p["txids"], wait=180)
        holder = p.get("holder")
        views, _u = kit_mod.health(ctx, [(holder["host"], holder["did"], T)])
        st.data["fullnode_chain"] = views.get(T, {}).get("fullnodes")
        for fn, (acc, rej, unseen, err) in sorted(out.items()):
            name = case_evidence.short_name(fn)
            if err:
                st.note("{} could not be read: {}".format(name, err))
                continue
            st.check(not rej, "{} rejected {} hop(s): {}".format(
                name, len(rej), "; ".join(sorted(set(case_evidence.explain_fullnode_reason(r)
                                                     for r in rej.values())))[:200]))
            st.check(not unseen, "{} has not processed {} of the {} hops after 180s".format(
                name, len(unseen), len(p["txids"])))
            view = (st.data["fullnode_chain"] or {}).get(name)
            st.check(view in (None, "same"), "{}'s copy of T's chain: {}".format(name, view))
    run.step("S3", verdicts, nodes=[], watch=False, deliver=False,
             require=[lambda: (bool(p.get("txids")), "no hop went through in S2")])
    return kit_mod.verdict(run, kit, "fullnodes rejected hops, fell behind or disagree",
                           "every fullnode accepted every hop and holds the whole chain")


# ---------------------------------------------------------------------------
# QUORUM-SETUP
# ---------------------------------------------------------------------------

def quorum_setup(ctx, ci):
    case = "QUORUM-SETUP"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q, why = kit.quorum(need=5)
        if not Q:
            return False, why
        A, why = kit.holders(1, k=2)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.wallet_on(role="B (payee)", tokens=kit.tokens_of(A))
        if not B:
            return False, why
        N = kit.other_host()
        if not N:
            return False, "no node left to add a quorum to"
        p.update(A=A, B=B, Q=Q, N=N, notes=[kit_mod.who(e, n) for e, n in ((A, "A"), (B, "B"), (Q, "Q"))] +
                 ["N = {}".format(kit_mod.short(N))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, Q, N = p["A"], p["B"], p["Q"], p["N"]

    def twice(st):
        url = rc.base_url(N) + rc.EP_QUORUM_ADD
        rc.http_json("GET", rc.base_url(N) + rc.EP_QUORUM_REMOVE_ALL)
        answers = [rc.http_json("POST", url, rc.DEFAULT_TIMEOUT, {"did": Q["did"]}) for _ in range(2)]
        st.data["answers"] = [a[1] for a in answers]
        for i, (ok, body) in enumerate(answers):
            good = ok and isinstance(body, dict) and bool(body.get("status"))
            st.check(good, "adding Q {} answered: {}".format(
                "the first time" if i == 0 else "again", (body.get("message") if isinstance(body, dict)
                                                         else body) or "no message"))
    run.step("S2", twice, nodes=[], watch=False, deliver=False)

    def restarted(st):
        ok, why = kit.wire([A], Q)
        if not ok:
            st.skip(why)
            return
        kit.note_moved(A, B, A["tokens"], "before and after the quorum's restart")
        ok, _m, _t = _send(st, A, B, 1.0, case + " before the restart", q=Q)
        if not ok:
            return
        ok, why = kit.restart(Q["host"])
        if not ok:
            st.skip(why)
            return
        if why:
            st.note(why)
        _send(st, A, B, 1.0, case + " after the restart", q=Q)
        rc.quorum_setup(Q["host"], Q["did"])
    run.step("S3", restarted, nodes=[A, B, Q], restarts=[Q["host"]], must_accept=True)
    return kit_mod.verdict(run, kit, "the quorum did not sign after its restart, or a repeat add "
                                     "answered an error",
                           "the quorum signed after its restart and a repeat add was harmless")


CASES = {
    "QUORUM-SETUP": quorum_setup,
    "EXACT-DECIMAL": exact_decimal,
    "PAY-TO-NODE-DOWN": pay_to_node_down,
    "RECEIVE-OLD-COPY": receive_old_copy,
    "EX-QUORUM-SPEND": ex_quorum_spend,
    "RELEASE-ONE-OF-MANY": release_one_of_many,
    "QUORUM-STOPPED": quorum_stopped,
    "SMALL-SPLITS": small_splits,
    "FULLNODES-ORDER": fullnodes_order,
    "CLOCK-EDGE": clock_edge,
}
ORDER = list(CASES)
NEEDS = dict((t, {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True}) for t in ORDER)
NO_DELAY_RERUN = set(ORDER)
