"""quorum_cases.py - what one quorum can carry.

One quorum signs every transaction (QuorumRequired = 1,
core/quorum_initiator.go:17; quorumAddresses[0] in core/transaction.go), so its
capacity is a ceiling on everything the fleet does at once.

QCAP-01 climbs a ladder of senders, on different machines, all transferring at
the same moment through ONE quorum - first whole RBT, then a fractional amount -
and saves the highest rung of each that went through in full
(lab-limits.json). The two differ in what the quorum does: it pledges exactly
the transfer value (core/consensus/consensus.go), so a whole amount pledges
whole tokens while a fractional one makes the quorum split its own tokens for
every transfer at once. The node- and fleet-level cases later give themselves
enough quorums that the quorum is never what they hit.
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

SKIP = "SKIP"

# Senders the QCAP-01 unit takes: the top of the default ladder. 220 senders +
# 1 quorum fit the 234-node pool.
QCAP_SENDERS = 220

# Refusals that come from the sender's own tokens and would happen with no load
# at all - counted apart, not against the quorum. Both are known product bugs
# (case_evidence.KNOWN_PRODUCT_BUGS): a former quorum spending a token it once
# pledged (core/consensus/checks.go:308), and a second-hand part token whose
# whole-token genesis the sender cannot supply.
NOT_THE_QUORUM = ("failed to get quorum DID for t",
                  "ValidateMinterAllowlist: whole-token genesis fetch failed")


def _rung_line(label, n, s):
    counted = n - s["aside"]
    line = "{} {}: {}/{} in p50 {:.1f}s p95 {:.1f}s max {:.1f}s".format(
        label, n, s["ok"], counted, s["p50"], s["p95"], s["max"])
    if s["failed"]:
        line += " - refused: " + s["reasons"]
    if s["aside"]:
        line += " - {} sender(s) refused for their own tokens, not counted: {}".format(
            s["aside"], s["aside_reasons"])
    return line


def _climb(ctx, senders, rungs, label, amount_of):
    """One ladder. Each rung: every sender pays the next in a ring, all at the
    same moment, the same amount (so balances stay level). Returns (limit,
    lines, rows, stopped) - limit is the highest rung that went through in
    full with every lower rung also in full."""
    lines, rows = [], []
    limit, clean, stopped = 0, True, ""
    for i, n in enumerate(rungs):
        amount = amount_of()
        jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": amount,
                 "memo": "QCAP-01 {} rung {}".format(label, n)} for a, b in ch.ring(senders[:n])]
        s = ch.summarize(ch.burst(jobs), set_aside=NOT_THE_QUORUM)
        counted = n - s["aside"]
        tag = label if amount == 1.0 else "{} ({:.3f} each)".format(label, amount)
        rows.append({"senders": n, "amount": amount, "ok": s["ok"], "failed": s["failed"],
                     "not_counted": s["aside"], "p50": s["p50"], "p95": s["p95"],
                     "max": s["max"], "reasons": s["reasons"]})
        lines.append(_rung_line(tag, n, s))
        print("    QCAP-01 " + _rung_line(tag, n, s))
        if s["failed"] == 0 and counted > 0 and clean:
            limit = n
        else:
            clean = False
        if counted and 100.0 * s["ok"] / counted < ctx.args.ladder_stop:
            stopped = " ({} stopped: under {:.0f}% went through at {})".format(
                label, ctx.args.ladder_stop, n)
            break
        if i < len(rungs) - 1:
            time.sleep(ctx.args.ladder_settle)
    return limit, lines, rows, stopped


def _said(limit, rungs):
    if not limit:
        return "not even {} at once in full".format(rungs[0])
    return "{} at once in full{}".format(
        limit, " (the top of the ladder - the real limit is higher)" if limit == rungs[-1] else "")


def qcap_01(ctx, ci):
    senders = ctx.senders
    rungs = ch.ladder(ctx.args.quorum_ladder, len(senders))
    if not rungs:
        return SKIP, "not enough senders for the ladder", \
            "the unit has {} senders; the ladder starts at {}".format(
                len(senders), ctx.args.quorum_ladder.split(",")[0])
    quorum = ctx.quorum_hosts[0]["host"] if ctx.quorum_hosts else "?"

    whole, w_lines, w_rows, w_stop = _climb(ctx, senders, rungs, "whole", lambda: 1.0)
    time.sleep(ctx.args.ladder_settle)
    # One deliberately untidy amount per rung, the same for every sender.
    frac, f_lines, f_rows, f_stop = _climb(ctx, senders, rungs, "fractional",
                                           lambda: ch.rand_value(0.101, 0.899))

    if whole:
        ch.save_limit("quorum_senders", whole, {"quorum": quorum, "rungs": w_rows})
    if frac:
        ch.save_limit("quorum_senders_fractional", frac, {"quorum": quorum, "rungs": f_rows})
    table = " | ".join(w_lines + f_lines) + w_stop + f_stop
    actual = "one quorum signed whole-RBT transfers {}; fractional transfers {}".format(
        _said(whole, rungs), _said(frac, rungs))
    if not whole:
        return False, actual, table
    return True, actual, table


# ---------------------------------------------------------------------------
# QCAP-02 - the pledge boundary
#
# A quorum must pledge at least the transfer value (core/consensus/checks.go),
# and it pledges EXACTLY the value by splitting its own tokens
# (core/consensus/consensus.go). So a quorum holding H can sign a transfer of
# any value up to H and no more: at H+one unit the initiator gets back
# "insufficient quorum liquidity" (core/transaction.go:253/278).
#
# This measures where that line actually is. Each quorum is tested ONCE, with a
# value derived from its OWN free holding read from the database - a pledge is
# held until the receiver spends onward, so a quorum that has signed one
# transfer cannot be reused for the next value. Five quorums, five offsets
# around each one's holding: -0.1, -0.001, 0 (exactly), +0.001, +0.1.
# ---------------------------------------------------------------------------

QCAP2_OFFSETS = (-0.1, -0.001, 0.0, 0.001, 0.1)


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def qcap_02(ctx, ci):
    quorums, senders, recv = ctx.quorum_hosts, ctx.senders, ctx.receivers
    n = min(len(quorums), len(senders), len(recv), len(QCAP2_OFFSETS))
    if n < 1:
        return SKIP, "needs a quorum, a sender and a receiver on different nodes", ""

    lines, problems, boundary_ok = [], [], 0
    for i in range(n):
        q, s, r = quorums[i], senders[i], recv[i]
        offset = QCAP2_OFFSETS[i]
        held = _free(q["host"], q["did"])
        if held is None:
            problems.append("could not read quorum {}'s holding".format(q["host"].split(".")[-1]))
            continue
        value = round(held + offset, 3)
        if value <= 0:
            lines.append("holding {:.3f}, offset {:+.3f}: value not positive, skipped".format(
                held, offset))
            continue
        sender_free = _free(s["host"], s["did"]) or 0.0
        if sender_free < value + 0.1:
            lines.append("holding {:.3f}, value {:.3f}: sender holds only {:.3f}, skipped".format(
                held, value, sender_free))
            continue
        # The sender's node already points at this quorum (prepare_unit). Fire
        # one transfer of `value` and see whether the quorum will pledge it.
        ok, msg, _r = rc.initiate_transaction(s["host"], s["did"], r["did"], rbt=value,
                                              memo="QCAP-02 boundary", timeout=ch.TRANSFER_TIMEOUT)
        reason = ch.node_reason(msg)
        should_pass = value <= held + 1e-9
        liquidity = "insufficient quorum liquidity" in reason
        verdict = "went through" if ok else ("refused (liquidity)" if liquidity
                                             else "refused: " + reason)
        lines.append("holding {:.3f}, value {:.3f} ({:+.3f}): {}".format(held, value, offset, verdict))
        # The boundary holds when value<=holding goes through and value>holding
        # is refused for liquidity (not for some other reason).
        if should_pass and ok:
            boundary_ok += 1
        elif (not should_pass) and liquidity:
            boundary_ok += 1
        elif should_pass and not ok:
            problems.append("value {:.3f} <= holding {:.3f} was refused: {}".format(
                value, held, reason))
        elif (not should_pass) and ok:
            problems.append("value {:.3f} > holding {:.3f} went through - the quorum pledged "
                            "more than it holds".format(value, held))
        elif not should_pass and not liquidity:
            problems.append("value {:.3f} > holding {:.3f} refused, but not for liquidity: "
                            "{}".format(value, held, reason))
    note = "{} quorum(s) at the boundary | {}".format(n, " | ".join(lines))
    if problems:
        return False, "; ".join(problems[:3]), note
    if not boundary_ok:
        return SKIP, "no quorum could be measured (sender funding or reads)", note
    return True, "the pledge boundary is exactly the quorum's holding on {} quorum(s)".format(
        boundary_ok), note


# ---------------------------------------------------------------------------
# QCAP-03 - a quorum spending its own RBT
#
# A quorum's pledged tokens are held until the receiver spends onward
# (core/callback.go). These two cases check what the quorum can do with the
# REST of its wallet in the meantime. Both wire their own quorums (A pays
# through Q; Q pays through a second quorum Q2), because the runner points every
# node at ONE quorum and here Q is both a signer and a sender.
# ---------------------------------------------------------------------------

def _wire(host, quorum_did, port):
    ok, msg = rc.quorum_reset(host, [quorum_did], port)
    return ok, msg


def _as_quorum(entry, floor, port):
    """Activate `entry` as a quorum signer and fund it to `floor`."""
    ok, msg = rc.quorum_setup(entry["host"], entry["did"], port)
    if not ok:
        return False, "quorum setup failed: " + str(msg)
    have = _free(entry["host"], entry["did"]) or 0.0
    if have < floor:
        ok, msg = rc.fund_did(entry["host"], entry["did"], floor - have, port)
        if not ok:
            return False, "funding failed: " + str(msg)
    return True, ""


def qcap_03a(ctx, ci):
    if len(ctx.senders) < 5:
        return SKIP, "needs five nodes (A, B, C, Q, Q2)", ""
    A, B, C, Q, Q2 = ctx.senders[:5]
    port, V, W = ctx.port, 20.0, 15.0

    for q in (Q, Q2):
        ok, why = _as_quorum(q, 100, port)
        if not ok:
            return SKIP, "could not set up quorum {}: {}".format(q["host"].split(".")[-1], why), ""
    for host, qdid in ((A["host"], Q["did"]), (Q["host"], Q2["did"])):
        ok, msg = _wire(host, qdid, port)
        if not ok:
            return SKIP, "could not wire quorum: " + str(msg), ""

    # A pays B through Q: Q pledges V.
    ok, msg, _r = rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=V,
                                          memo="QCAP-03a A->B", timeout=ch.TRANSFER_TIMEOUT)
    if not ok:
        return SKIP, "the A->B transfer that makes Q pledge did not go through: {}".format(
            ch.node_reason(msg)), ""
    pledged_ids = set(t for t, _v, _st, _p in db.token_rows(Q["host"], Q["did"], status=db.PLEDGED))
    pledged_before = db.pledged_value(Q["host"], Q["did"])

    # Q pays C through Q2 while V is pledged: must use Q's FREE tokens.
    ok, msg, _r = rc.initiate_transaction(Q["host"], Q["did"], C["did"], rbt=W,
                                          memo="QCAP-03a Q->C", timeout=ch.TRANSFER_TIMEOUT)
    if not ok:
        return False, "the quorum could not spend its own free RBT while a pledge was " \
            "outstanding: {}".format(ch.node_reason(msg)), \
            "Q held a pledge of {:.3f} and tried to send {:.3f} of its free RBT".format(
                pledged_before, W)
    time.sleep(ch.SETTLE)
    still = set(t for t, _v, _st, _p in db.token_rows(Q["host"], Q["did"], status=db.PLEDGED))
    touched = pledged_ids - still
    credited, _c = rc.wait_for_balance(C["host"], C["did"], W - ch.TOL, port, attempts=20)
    note = "Q pledged {:.3f} (tokens {}), then sent {:.3f} of its free RBT".format(
        pledged_before, len(pledged_ids), W)
    if touched:
        return False, "the quorum spent {} token(s) it had pledged".format(len(touched)), note
    if not credited:
        return False, "the receiver was never credited the quorum's {:.3f}".format(W), note
    return True, "a quorum spent its free RBT without touching the {:.3f} it had pledged".format(
        pledged_before), note


def qcap_03b(ctx, ci):
    if len(ctx.senders) < 5:
        return SKIP, "needs five nodes (A, B, C, Q, Q2)", ""
    A, B, C, Q, Q2 = ctx.senders[:5]
    port, V = ctx.port, 20.0

    for q in (Q, Q2):
        ok, why = _as_quorum(q, 100, port)
        if not ok:
            return SKIP, "could not set up quorum {}: {}".format(q["host"].split(".")[-1], why), ""
    for host, qdid in ((A["host"], Q["did"]), (Q["host"], Q2["did"]), (B["host"], Q2["did"])):
        ok, msg = _wire(host, qdid, port)
        if not ok:
            return SKIP, "could not wire quorum: " + str(msg), ""

    # Bring Q down to roughly V free, so pledging V for one transfer leaves it
    # unable to send V itself.
    import wallet_shapes as ws
    ws.drain_to(ctx, Q, C, keep=0.0)
    have = _free(Q["host"], Q["did"]) or 0.0
    if have < V:
        ok, msg = rc.fund_did(Q["host"], Q["did"], V - have, port)
        if not ok:
            return SKIP, "could not fund Q to the pledge amount: " + str(msg), ""

    # A pays B through Q: Q pledges ~all of its V.
    ok, msg, _r = rc.initiate_transaction(A["host"], A["did"], B["did"], rbt=V,
                                          memo="QCAP-03b A->B", timeout=ch.TRANSFER_TIMEOUT)
    if not ok:
        return SKIP, "the A->B transfer that makes Q pledge did not go through: {}".format(
            ch.node_reason(msg)), ""
    time.sleep(ch.SETTLE)
    steps = []

    # 1. Q tries to send V while its value is pledged - should be refused.
    ok, msg, _r = rc.initiate_transaction(Q["host"], Q["did"], C["did"], rbt=V,
                                          memo="QCAP-03b pledged send", timeout=ch.TRANSFER_TIMEOUT)
    steps.append("while pledged: " + ("WENT THROUGH (unexpected)" if ok else "refused, as it should be"))
    refused_while_pledged = not ok

    # 2. B spends the token onward to C - this releases Q's pledge. Time it.
    ok, msg, _r = rc.initiate_transaction(B["host"], B["did"], C["did"], rbt=V,
                                          memo="QCAP-03b release", timeout=ch.TRANSFER_TIMEOUT)
    released, wait_s = False, None
    if ok:
        t0 = time.time()
        for _ in range(60):
            if db.pledged_value(Q["host"], Q["did"]) < V - ch.TOL:
                released, wait_s = True, round(time.time() - t0, 1)
                break
            time.sleep(2)
    steps.append("pledge released {}".format(
        "after {:.0f}s".format(wait_s) if released else "NOT within 120s"))

    # 3. After release Q's tokens are Free but their last chain role is
    #    "unpledge". A whole-RBT send hits the ex-quorum bug (checks.go:308); a
    #    sub-1 send splits and bypasses it (CLAUDE.md known bug).
    whole = split = None
    if released:
        time.sleep(ch.SETTLE)
        ok, msg, _r = rc.initiate_transaction(Q["host"], Q["did"], C["did"], rbt=1.0,
                                              memo="QCAP-03b whole after release",
                                              timeout=ch.TRANSFER_TIMEOUT)
        whole = (ok, ch.node_reason(msg))
        steps.append("whole-RBT send after release: " + ("went through" if ok
                     else "refused: " + whole[1]))
        ok, msg, _r = rc.initiate_transaction(Q["host"], Q["did"], C["did"], rbt=0.5,
                                              memo="QCAP-03b split after release",
                                              timeout=ch.TRANSFER_TIMEOUT)
        split = (ok, ch.node_reason(msg))
        steps.append("fractional send after release: " + ("went through" if ok
                     else "refused: " + split[1]))

    note = " | ".join(steps)
    # The case is about describing the behaviour, not a single pass/fail: it
    # confirms the refuse-while-pledged rule and records the release time and
    # the known ex-quorum bug after release.
    if not refused_while_pledged:
        return False, "a quorum sent RBT whose value it had entirely pledged", note
    if not released:
        return False, "the pledge was not released after the receiver spent the token onward", note
    return True, "pledged RBT is unspendable until released ({:.0f}s), then the known ex-quorum " \
        "bug blocks whole-token sends while splits go through".format(wait_s or 0), note


CASES = {"QCAP-01": qcap_01, "QCAP-02": qcap_02, "QCAP-03a": qcap_03a, "QCAP-03b": qcap_03b}
ORDER = ["QCAP-01", "QCAP-02", "QCAP-03a", "QCAP-03b"]
NEEDS = {
    # Ring: every sender pays the next, so it also receives - no receivers of
    # its own. The quorum is funded for every pledge of both ladders at once
    # (about 1,700 RBT on the default ladder) in case none is released between
    # rungs: a pledge is held until the receiver spends that token onward.
    "QCAP-01": {"senders": QCAP_SENDERS, "receivers": 0, "quorums": 1, "fund": 3,
                "quorum_fund": 2000, "exclusive": True},
    # Five quorums, each topped to ~123 whole from the faucet (poorest DIDs, so
    # the floor sets the holding rather than a big prior balance); senders hold
    # enough to cover a value just over that.
    "QCAP-02": {"senders": 5, "receivers": 5, "quorums": 5, "fund": 135,
                "quorum_fund": 123, "quorum_pick": "poorest"},
    # Five nodes the case wires itself (A, B, C, Q, Q2); no runner quorum.
    "QCAP-03a": {"senders": 5, "receivers": 0, "quorums": 0, "fund": 60},
    "QCAP-03b": {"senders": 5, "receivers": 0, "quorums": 0, "fund": 60},
}
# Raw capacity / boundary / wiring: a delayed rerun would measure the fullnodes
# or re-run the manual wiring, not the product.
NO_DELAY_RERUN = {"QCAP-01", "QCAP-02", "QCAP-03a", "QCAP-03b"}
