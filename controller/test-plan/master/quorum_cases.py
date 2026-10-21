"""quorum_cases.py - what one quorum can carry.

One quorum signs every transaction (QuorumRequired = 1,
core/quorum_initiator.go:17; quorumAddresses[0] in core/transaction.go), so its
capacity is a ceiling on everything the fleet does at once.

QCAP-01 climbs a ladder of senders, on different machines, all transferring at
the same moment through ONE quorum, and saves the highest rung that went
through in full (lab-limits.json). The node- and fleet-level cases later give
themselves enough quorums that the quorum is never what they hit.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch

SKIP = "SKIP"

# Senders the QCAP-01 unit takes: the top of the default ladder with room for
# a longer one. 220 senders + 1 quorum fit the 234-node pool.
QCAP_SENDERS = 220


def _rung_line(n, s):
    line = "{}: {}/{} in p50 {:.1f}s p95 {:.1f}s max {:.1f}s".format(
        n, s["ok"], n, s["p50"], s["p95"], s["max"])
    return line + (" - refused: " + s["reasons"] if s["failed"] else "")


def qcap_01(ctx, ci):
    senders = ctx.senders
    rungs = ch.ladder(ctx.args.quorum_ladder, len(senders))
    if not rungs:
        return SKIP, "not enough senders for the ladder", \
            "the unit has {} senders; the ladder starts at {}".format(
                len(senders), ctx.args.quorum_ladder.split(",")[0])
    quorum = ctx.quorum_hosts[0]["host"] if ctx.quorum_hosts else "?"

    lines, rows = [], []
    limit, clean, stopped = 0, True, ""
    for i, n in enumerate(rungs):
        group = senders[:n]
        jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": 1.0,
                 "memo": "QCAP-01 rung {}".format(n)} for a, b in ch.ring(group)]
        s = ch.summarize(ch.burst(jobs))
        rows.append({"senders": n, "ok": s["ok"], "failed": s["failed"], "p50": s["p50"],
                     "p95": s["p95"], "max": s["max"], "reasons": s["reasons"]})
        lines.append(_rung_line(n, s))
        print("    QCAP-01 " + _rung_line(n, s))
        if s["ok"] == n and clean:
            limit = n
        else:
            clean = False
        if 100.0 * s["ok"] / n < ctx.args.ladder_stop:
            stopped = " (stopped: under {:.0f}% went through at {})".format(ctx.args.ladder_stop, n)
            break
        if i < len(rungs) - 1:
            time.sleep(ctx.args.ladder_settle)

    table = " | ".join(lines) + stopped
    if limit:
        ch.save_limit("quorum_senders", limit, {"quorum": quorum, "rungs": rows})
    if not limit:
        return False, "even {} concurrent senders through one quorum did not all go " \
            "through".format(rungs[0]), table
    top = " - the top of the ladder, so the real limit is higher" if limit == rungs[-1] else ""
    return True, "one quorum signed {} concurrent transfers in full{}".format(limit, top), table


CASES = {"QCAP-01": qcap_01}
ORDER = ["QCAP-01"]
NEEDS = {
    # Ring: every sender pays the next, so it also receives - no receivers of
    # its own. The quorum is funded for the sum of every rung (690 RBT on the
    # default ladder) in case no pledge is released in between.
    "QCAP-01": {"senders": QCAP_SENDERS, "receivers": 0, "quorums": 1, "fund": 3,
                "quorum_fund": 1000, "exclusive": True},
}
# Raw capacity: a rerun waiting on the fullnodes between transfers would
# measure the fullnodes, not the quorum.
NO_DELAY_RERUN = {"QCAP-01"}
