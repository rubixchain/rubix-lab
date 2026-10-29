"""chain_cases.py - a token down a long chain, validated by a different quorum
at each hop, beside a control chain validated by one quorum throughout.

Every hop is checked by a quorum that has never seen the token before, so it
must sync the chain from the sender and validate it from scratch
(TokenChainIntegrityCheck, core/consensus/checks.go). A chain that only fails
when the quorum rotates - and not in the single-quorum control beside it -
points the failure at the rotation and the sync, not at chain depth. This is
the per-hop validation path the stale-chain findings live in, run deep. The
time per hop is compared between the start and the end of the chain: if
validating a longer history costs more, that is where a depth limit will come
from.

ONE token travels, and every hop checks by its token_id that it arrived. A
send of whole RBT takes the sender's lowest whole token_id first
(core/wallet/token_lock.go), so each holder plays from a DID on its node that
holds no whole token (case_helpers.empty_did). Holders and quorums are kept
disjoint (a DID that served as a quorum cannot cleanly spend afterwards - the
known ex-quorum bug), so the token can cycle through the holders as many times
as the hop count needs.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc
import steps

SKIP = "SKIP"
DEFAULT_HOPS = 200
ARRIVAL_WAIT = 60       # seconds for the token to show at the next holder

PURPOSE = {
    "CHAIN-01": "How deep a token's history can grow while every hop is still validated and "
                "accepted - with a new quorum syncing the whole chain at every hop, and with one "
                "quorum throughout - and whether the time per hop grows with depth.",
}

STEPS = {
    "CHAIN-01": [
        ("S1", "Set-up check",
         "each holder plays from a DID on its node holding no whole token; each quorum is set up "
         "and funded for every pledge it may hold (a pledge is held until spent onward); the first "
         "holder gets exactly one token",
         "the token to follow is known by its token_id"),
        ("S2", "Rotating chain",
         "the token passes along the holders for --chain-hops hops, a different quorum signing "
         "each hop",
         "every hop accepted and the token on the next holder within 60s; the time per hop at the "
         "end of the chain is not much more than at the start"),
        ("S3", "Single-quorum control",
         "a new token, the same depth, one quorum signing every hop",
         "as S2"),
    ],
}


def _free(host, did):
    ok, detail, _n = rc.get_rbt_balance_detail(host, did)
    return float(detail["balance"]) if ok and detail else None


def _arrived(entry, token_id, wait=ARRIVAL_WAIT):
    """True once `token_id` is Free under the entry's DID on its node. The
    receiver credits asynchronously after the sender returns
    (core/transaction.go sendTokensToReceiver), so this polls."""
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            if token_id in ch.whole_free(entry["host"], entry["did"]):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _seed(holder, port):
    """One whole token from the faucet to an empty holder; its token_id, or None."""
    ok, _m = rc.fund_did(holder["host"], holder["did"], 1, port)
    if not ok:
        return None
    tokens = ch.whole_free(holder["host"], holder["did"])
    return tokens[0] if len(tokens) == 1 else None


def _clear(holders, wire_to, sink, port):
    """Hand any whole token a holder still has to `sink`, so the next chain
    starts with every holder empty. The sender is pointed at `wire_to`, never
    at the sink itself."""
    for h in holders:
        n = len(ch.whole_free(h["host"], h["did"]))
        if n:
            rc.quorum_reset(h["host"], [wire_to["did"]], port)
            rc.initiate_transaction(h["host"], h["did"], sink["did"], rbt=float(n),
                                    memo="CHAIN-01 clear", timeout=ch.TRANSFER_TIMEOUT)


def _fund_quorums(quorums, pledges_each, port):
    """Top each quorum up to the pledges it may hold at once: a pledge is held
    until the token is spent onward (core/callback.go), and if the release
    misses (core/fullnode.go:69 checks one quorum DID per node) they pile up."""
    for q in quorums:
        have = _free(q["host"], q["did"]) or 0.0
        if have < pledges_each + 10:
            rc.fund_did(q["host"], q["did"], pledges_each + 10 - have, port)


def _chain(st, holders, quorums, hops, port, label, token):
    """Pass `token` along `holders` (cycling) for `hops` hops, quorum for hop i
    quorums[i % len(quorums)], recording every hop on step `st`. Stops at the
    first hop that is refused or whose token does not arrive."""
    holder, secs = holders[0], []
    for i in range(hops):
        nxt = holders[(i + 1) % len(holders)]
        q = quorums[i % len(quorums)]
        ok, msg = rc.quorum_reset(holder["host"], [q["did"]], port)
        if not ok:
            st.skip("hop {}: could not point {} at its quorum: {}".format(
                i + 1, steps._short(holder["host"]), ch.node_reason(msg)))
            return
        # Nothing set aside: a hop the chain cannot pass, for any reason, ends it.
        ok, _m, _r = ch.send(st, holder, nxt, rbt=1.0, memo="{} hop {}".format(label, i + 1),
                             set_aside=())
        if not ok:
            st.note("stopped at hop {} ({} -> {})".format(i + 1, steps._short(holder["host"]),
                                                          steps._short(nxt["host"])))
            break
        secs.append(st.ops[-1]["secs"] or 0)
        if not st.check(_arrived(nxt, token), "hop {} ({} -> {}): the sender reported success but "
                        "token {} never reached the receiver in {}s".format(
                            i + 1, steps._short(holder["host"]), steps._short(nxt["host"]), token,
                            ARRIVAL_WAIT)):
            break
        holder = nxt
    st.data["hops_done"] = len(secs)
    if len(secs) >= 40:
        head, tail = sum(secs[:20]) / 20.0, sum(secs[-20:]) / 20.0
        st.data["seconds_first_20"], st.data["seconds_last_20"] = round(head, 2), round(tail, 2)
        st.note("a hop took {:.2f}s at the start and {:.2f}s at the end ({:.1f}x)".format(
            head, tail, tail / head if head else 0))


def chain_01(ctx, ci):
    run = steps.CaseRun(ctx, "CHAIN-01", STEPS["CHAIN-01"])
    quorums = ctx.quorum_hosts
    holders = [e for e in (ch.empty_did(h, whole_only=True) for h in ctx.senders) if e]
    hops = max(2, int(getattr(ctx.args, "chain_hops", DEFAULT_HOPS)))
    port = ctx.port
    seeded = {}

    def setup():
        if len(holders) < 3 or len(quorums) < 2:
            return False, "needs at least three holders with an empty DID on their node and two " \
                          "quorums ({} holders, {} quorums)".format(len(holders), len(quorums))
        for q in quorums:
            ok, why = steps.quorum_ready(q, 0, port)
            if not ok:
                return ok, why
        _fund_quorums(quorums, -(-hops // len(quorums)), port)
        seeded["token"] = _seed(holders[0], port)
        return bool(seeded["token"]), "could not hand the first holder exactly one token to follow"
    s1 = run.step("S1", lambda st: None, nodes=quorums, watch=False, require=[setup])
    if s1.outcome != steps.PASS:
        return run.verdict()

    token = seeded["token"]
    rot = run.step("S2", lambda st: _chain(st, holders, quorums, hops, port, "CHAIN-01 rotating", token),
                   nodes=holders + quorums, must_accept=True)

    # Control: every holder empty again, a new token, one quorum.
    def control_setup():
        _clear(holders, quorums[0], quorums[-1], port)
        _fund_quorums(quorums[:1], hops, port)
        seeded["token2"] = _seed(holders[0], port)
        return bool(seeded["token2"]), "could not hand the first holder one token for the control chain"
    ctl = run.step("S3", lambda st: _chain(st, holders, quorums[:1], hops, port, "CHAIN-01 control",
                                           seeded["token2"]),
                   nodes=holders + quorums[:1], must_accept=True, require=[control_setup])

    r_ok, c_ok = rot.outcome == steps.PASS, ctl.outcome == steps.PASS
    if not r_ok and c_ok:
        where = "the chain fails only when the quorum rotates ({} of {} hops with rotation, all {} " \
                "with one quorum)".format(rot.data.get("hops_done", 0), hops, hops)
        rot.summary = where + "; " + rot.summary
    elif not r_ok and not c_ok and rot.outcome != steps.NOT_RUN and ctl.outcome != steps.NOT_RUN:
        rot.summary = "both chains stopped ({} rotating, {} control of {} hops); ".format(
            rot.data.get("hops_done", 0), ctl.data.get("hops_done", 0), hops) + rot.summary
    return run.verdict(passed="a {}-hop chain held with a new quorum at every hop, and in the "
                              "single-quorum control".format(hops))


CASES = {"CHAIN-01": chain_01}
ORDER = ["CHAIN-01"]
NEEDS = {
    # Holders cycle; quorums rotate. Disjoint, so holders stay spendable. The
    # holders play from an empty DID on their nodes, so they are not funded.
    "CHAIN-01": {"senders": 30, "receivers": 0, "quorums": 10, "fund": 0, "quorum_fund": 20},
}
NO_DELAY_RERUN = {"CHAIN-01"}
