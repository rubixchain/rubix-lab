"""chain_cases.py - a token down a long chain, validated by a different quorum
at each hop, beside a control chain validated by one quorum throughout.

Every hop is checked by a quorum that has never seen the token before, so it
must sync the chain from the sender and validate it from scratch
(TokenChainIntegrityCheck, core/consensus/checks.go). A chain that only fails
when the quorum rotates - and not in the single-quorum control beside it -
points the failure at the rotation and the sync, not at chain depth. This is
the per-hop validation path the stale-chain findings live in, run deep.

Holders and quorums are kept disjoint (a DID that served as a quorum cannot
cleanly spend afterwards - the known ex-quorum bug), so the token can cycle
through the holders as many times as the hop count needs.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc

SKIP = "SKIP"
DEFAULT_HOPS = 200


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def _run_chain(holders, quorums, hops, port, label):
    """Pass 1 RBT along `holders` (cycling) for `hops` hops. Quorum for hop i is
    quorums[i % len(quorums)] - pass a single-item list for the control.
    Returns (done, refusals, reasons)."""
    done = 0
    reasons = {}
    holder = holders[0]
    for i in range(hops):
        nxt = holders[(i + 1) % len(holders)]
        if nxt["did"] == holder["did"]:
            nxt = holders[(i + 2) % len(holders)]
        q = quorums[i % len(quorums)]
        ok, msg = rc.quorum_reset(holder["host"], [q["did"]], port)
        if not ok:
            reasons[("wire", ch.node_reason(msg))] = reasons.get(("wire", ch.node_reason(msg)), 0) + 1
            break
        ok, msg, _r = rc.initiate_transaction(holder["host"], holder["did"], nxt["did"], rbt=1.0,
                                              memo="{} hop {}".format(label, i + 1),
                                              timeout=ch.TRANSFER_TIMEOUT)
        if not ok:
            r = ch.node_reason(msg)
            reasons[r] = reasons.get(r, 0) + 1
            break
        credited, _c = rc.wait_for_balance(nxt["host"], nxt["did"],
                                           (_free(nxt["host"], nxt["did"]) or 0.0), port, attempts=1)
        if not rc.wait_for_balance(nxt["host"], nxt["did"], 1.0 - ch.TOL, port, attempts=20)[0]:
            reasons["receiver not credited"] = reasons.get("receiver not credited", 0) + 1
            break
        done += 1
        holder = nxt
    summary = "; ".join("{}x {}".format(n, r if isinstance(r, str) else r[1])
                        for r, n in sorted(reasons.items(), key=lambda kv: -kv[1])[:3])
    return done, summary


def chain_01(ctx, ci):
    holders, quorums = ctx.senders, ctx.quorum_hosts
    if len(holders) < 3 or len(quorums) < 2:
        return SKIP, "needs at least three holders and two quorums", ""
    hops = max(2, int(getattr(ctx.args, "chain_hops", DEFAULT_HOPS)))
    port = ctx.port

    # Seed the first holder with the travelling token (everyone was funded to
    # `fund` by prepare_unit, so this is usually already covered).
    if (_free(holders[0]["host"], holders[0]["did"]) or 0.0) < 1.0:
        rc.fund_did(holders[0]["host"], holders[0]["did"], 2, port)

    rot_done, rot_why = _run_chain(holders, quorums, hops, port, "CHAIN-01 rotating")
    time.sleep(ch.SETTLE)
    # Control: reseed and run the same depth through ONE quorum.
    if (_free(holders[0]["host"], holders[0]["did"]) or 0.0) < 1.0:
        rc.fund_did(holders[0]["host"], holders[0]["did"], 2, port)
    ctl_done, ctl_why = _run_chain(holders, quorums[:1], hops, port, "CHAIN-01 control")

    note = "rotating quorum: {}/{} hops{} | single-quorum control: {}/{} hops{}".format(
        rot_done, hops, " - " + rot_why if rot_why else "",
        ctl_done, hops, " - " + ctl_why if ctl_why else "")
    if rot_done < hops and ctl_done >= hops:
        return False, "the chain fails only when the quorum rotates: {} of {} hops with rotation, " \
            "all {} with one quorum".format(rot_done, hops, hops), note
    if rot_done < hops and ctl_done < hops:
        return False, "the chain fails at depth regardless of the quorum ({} rotating, {} control " \
            "of {} hops)".format(rot_done, ctl_done, hops), note
    return True, "a {}-hop chain held with a new quorum at every hop, and in the single-quorum " \
        "control".format(hops), note


CASES = {"CHAIN-01": chain_01}
ORDER = ["CHAIN-01"]
NEEDS = {
    # Holders cycle; quorums rotate. Disjoint, so holders stay spendable.
    "CHAIN-01": {"senders": 30, "receivers": 0, "quorums": 10, "fund": 5, "quorum_fund": 20},
}
NO_DELAY_RERUN = {"CHAIN-01"}
