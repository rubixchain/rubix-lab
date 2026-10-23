"""chain_cases.py - a token down a long chain, validated by a different quorum
at each hop, beside a control chain validated by one quorum throughout.

Every hop is checked by a quorum that has never seen the token before, so it
must sync the chain from the sender and validate it from scratch
(TokenChainIntegrityCheck, core/consensus/checks.go). A chain that only fails
when the quorum rotates - and not in the single-quorum control beside it -
points the failure at the rotation and the sync, not at chain depth. This is
the per-hop validation path the stale-chain findings live in, run deep.

ONE token travels, and every hop checks by its token_id that it arrived. A
send of whole RBT takes the sender's lowest whole token_id first
(core/wallet/token_lock.go), so until 2026-10-23, with every holder funded to
5 RBT, each hop moved a different token and no token went further than one
hop - and the "credited" check (next holder holds >= 1 RBT) was always true,
so a transfer whose token never arrived still counted. Now each holder plays
from a DID on its node that holds no whole token (case_helpers.empty_did).

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
ARRIVAL_WAIT = 60       # seconds for the token to show at the next holder


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


def _run_chain(holders, quorums, hops, port, label, token):
    """Pass `token` along `holders` (cycling) for `hops` hops. Quorum for hop i
    is quorums[i % len(quorums)] - pass a single-item list for the control.
    Returns (hops done, stop) - stop is None when every hop went through, else
    {hop, src, dst, why, kind}, kind one of wire / refused / lost."""
    holder = holders[0]
    for i in range(hops):
        nxt = holders[(i + 1) % len(holders)]
        q = quorums[i % len(quorums)]
        stop = {"hop": i + 1, "src": holder["host"], "dst": nxt["host"]}
        ok, msg = rc.quorum_reset(holder["host"], [q["did"]], port)
        if not ok:
            return i, dict(stop, kind="wire", why="could not wire the quorum: " + ch.node_reason(msg))
        ok, msg, _r = rc.initiate_transaction(holder["host"], holder["did"], nxt["did"], rbt=1.0,
                                              memo="{} hop {}".format(label, i + 1),
                                              timeout=ch.TRANSFER_TIMEOUT)
        if not ok:
            return i, dict(stop, kind="refused", why=ch.node_reason(msg, 200))
        if not _arrived(nxt, token):
            return i, dict(stop, kind="lost", why="the sender reported success but token {} "
                           "never reached the receiver in {}s".format(token, ARRIVAL_WAIT))
        holder = nxt
    return hops, None


def _said(done, stop, hops):
    if stop is None:
        return "{}/{} hops".format(done, hops)
    return "{}/{} hops - stopped at hop {} ({} -> {}): {}".format(
        done, hops, stop["hop"], stop["src"], stop["dst"], stop["why"])


def chain_01(ctx, ci):
    quorums = ctx.quorum_hosts
    holders = [e for e in (ch.empty_did(h, whole_only=True) for h in ctx.senders) if e]
    if len(holders) < 3 or len(quorums) < 2:
        return SKIP, "needs at least three holders with an empty DID on their node and two " \
            "quorums ({} holders, {} quorums)".format(len(holders), len(quorums)), ""
    hops = max(2, int(getattr(ctx.args, "chain_hops", DEFAULT_HOPS)))
    port = ctx.port

    _fund_quorums(quorums, -(-hops // len(quorums)), port)
    token = _seed(holders[0], port)
    if not token:
        return SKIP, "could not hand the first holder exactly one token to follow", ""
    rot_done, rot_stop = _run_chain(holders, quorums, hops, port, "CHAIN-01 rotating", token)
    time.sleep(ch.SETTLE)

    # Control: every holder empty again, a new token, the same depth through ONE quorum.
    _clear(holders, quorums[0], quorums[-1], port)
    _fund_quorums(quorums[:1], hops, port)
    token2 = _seed(holders[0], port)
    if not token2:
        return SKIP, "could not hand the first holder one token for the control chain", \
            "rotating quorum: " + _said(rot_done, rot_stop, hops)
    ctl_done, ctl_stop = _run_chain(holders, quorums[:1], hops, port, "CHAIN-01 control", token2)

    note = "{} holders, tokens {} and {} | rotating quorum: {} | single-quorum control: {}".format(
        len(holders), token, token2, _said(rot_done, rot_stop, hops), _said(ctl_done, ctl_stop, hops))
    stops = [s for s in (rot_stop, ctl_stop) if s]
    if not stops:
        return True, "a {}-hop chain held with a new quorum at every hop, and in the " \
            "single-quorum control".format(hops), note
    lost = [s for s in stops if s["kind"] == "lost"]
    if lost:
        s = lost[0]
        return False, "a transfer was reported successful but its token never reached the next " \
            "holder (hop {}, {}): held by nobody - the sender's push to the receiver runs once, " \
            "after the transfer returns, and is not retried (core/transaction.go:537, " \
            ":600)".format(s["hop"], s["dst"]), note
    unreachable = [s for s in stops if "did not exist with the peer" in s["why"]
                   or "getPeer" in s["why"]]
    if unreachable:
        s = unreachable[0]
        return False, "hop {} could not reach a holder's node over IPFS ({}: {}) - a node that " \
            "cannot be reached, not chain depth".format(s["hop"], s["src"], s["why"][:120]), note
    if rot_stop and not ctl_stop:
        return False, "the chain fails only when the quorum rotates: {} of {} hops with rotation, " \
            "all {} with one quorum".format(rot_done, hops, hops), note
    if ctl_stop and not rot_stop:
        return False, "the single-quorum control failed ({} of {} hops) while every rotated hop " \
            "went through".format(ctl_done, hops), note
    return False, "the chain fails at depth regardless of the quorum ({} rotating, {} control " \
        "of {} hops)".format(rot_done, ctl_done, hops), note


CASES = {"CHAIN-01": chain_01}
ORDER = ["CHAIN-01"]
NEEDS = {
    # Holders cycle; quorums rotate. Disjoint, so holders stay spendable. The
    # holders play from an empty DID on their nodes, so they are not funded.
    "CHAIN-01": {"senders": 30, "receivers": 0, "quorums": 10, "fund": 0, "quorum_fund": 20},
}
NO_DELAY_RERUN = {"CHAIN-01"}
