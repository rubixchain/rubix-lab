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


CASES = {"QCAP-01": qcap_01}
ORDER = ["QCAP-01"]
NEEDS = {
    # Ring: every sender pays the next, so it also receives - no receivers of
    # its own. The quorum is funded for every pledge of both ladders at once
    # (about 1,700 RBT on the default ladder) in case none is released between
    # rungs: a pledge is held until the receiver spends that token onward.
    "QCAP-01": {"senders": QCAP_SENDERS, "receivers": 0, "quorums": 1, "fund": 3,
                "quorum_fund": 2000, "exclusive": True},
}
# Raw capacity: a rerun waiting on the fullnodes between transfers would
# measure the fullnodes, not the quorum.
NO_DELAY_RERUN = {"QCAP-01"}
