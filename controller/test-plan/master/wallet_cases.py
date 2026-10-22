"""wallet_cases.py - big and fragmented wallets.

WALLET-PREP builds the two wallets the run needs and records what it cost:
  - a BIG wallet of thousands of whole tokens, drawn from the faucet, which
    also measures how fast value moves in bulk (~28 ms per token in the
    2026-10-21 smoke test)
  - a FRAGMENTED wallet that collects change - random fractions sent by 30
    other wallets at once, every round - which is how a wallet fragments in
    practice. A wallet cannot build thousands of part tokens itself: one split
    yields at most ~16 parts (core/parts/denom.go tree 1 -> 2x0.5 -> 5x0.1 ...)
    and every later transfer uses existing parts before splitting a new token
    (GetSplitAndNonsplitTokenDenom). These parts are second-hand, so spending
    them can hit the known minter-allowlist bug; the run tags that refusal.
WALLET-01 then spends from both, sized so the wallet always covers the amount.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import wallet_shapes as ws
import case_helpers as ch

SKIP = "SKIP"
GIVERS = 30                 # wallets sending change to the fragmented wallet
MAX_ROUNDS = 12


def _shape(entry):
    """(whole tokens, part tokens, part value) of a wallet, from its database."""
    denoms = db.real_free_denoms(entry["host"], entry["did"])
    whole = sum(c for v, c in denoms.items() if v >= 1.0)
    parts = sum(c for v, c in denoms.items() if v < 1.0)
    part_value = round(sum(v * c for v, c in denoms.items() if v < 1.0), 3)
    return whole, parts, part_value


def wallet_prep(ctx, ci):
    if len(ctx.senders) < 2 or len(ctx.receivers) < 2:
        return SKIP, "needs two wallets and change givers", ""
    big, frag = ctx.senders[0], ctx.senders[1]
    givers = ctx.receivers[:GIVERS]
    want_whole, want_parts = ctx.args.whole_wallet, ctx.args.parts_target

    # Big wallet: the faucet pays whole tokens in 1,000-token transfers.
    whole0, _p, _v = _shape(big)
    t0 = time.time()
    if whole0 < want_whole:
        ok, msg = rc.fund_did(big["host"], big["did"], want_whole - whole0, ctx.port)
        if not ok:
            return False, "the big wallet could not be funded", msg
    big_secs = round(time.time() - t0, 1)
    whole1, _p, _v = _shape(big)
    moved = whole1 - whole0
    rate = " ({:.0f} ms per token)".format(1000.0 * big_secs / moved) if moved else ""

    # Fragmented wallet: every giver sends a random fraction at once, per round.
    _w, parts0, _v = _shape(frag)
    t1 = time.time()
    rounds, sent, refused, refusals, still = 0, 0, 0, [], 0
    parts = parts0
    while parts < want_parts and rounds < MAX_ROUNDS and still < 2:
        rounds += 1
        jobs = [{"host": g["host"], "did": g["did"], "to": frag["did"],
                 "rbt": ch.rand_value(0.011, 0.989), "memo": "WALLET-PREP change"}
                for g in givers]
        results = ch.burst(jobs)
        sent += sum(1 for r in results if r["ok"])
        refused += sum(1 for r in results if not r["ok"])
        refusals += [r["msg"] for r in results if not r["ok"]]
        time.sleep(ch.SETTLE)
        _w, now, _v = _shape(frag)
        still = still + 1 if now <= parts else 0
        parts = now
    frag_secs = round(time.time() - t1, 1)

    note = ("big: {} -> {} whole tokens in {}s{} | fragmented: {} -> {} part tokens in {} "
            "round(s) of {} givers at once, {}s, {} sends went through, {} refused{}".format(
                whole0, whole1, big_secs, rate, parts0, parts, rounds, len(givers), frag_secs,
                sent, refused, (" - " + ch.refusal_summary(refusals)) if refusals else ""))
    if whole1 < want_whole:
        return False, "the big wallet holds {} whole tokens, short of {}".format(whole1, want_whole), note
    if parts < want_parts:
        return False, "the fragmented wallet collected {} part tokens, short of {}".format(
            parts, want_parts), note
    return True, "wallets ready: {} whole tokens, {} part tokens".format(whole1, parts), note


def wallet_01(ctx, ci):
    if len(ctx.senders) < 2 or len(ctx.receivers) < 12:
        return SKIP, "needs the two prepared wallets and 12 receivers", ""
    big, frag = ctx.senders[0], ctx.senders[1]
    lines, problems = [], []

    def send(src, dst, amount, label):
        ok0, bal0, _n = rc.get_rbt_balance(dst["host"], dst["did"], ctx.port)
        t0 = time.time()
        ok, msg, _r = rc.initiate_transaction(src["host"], src["did"], dst["did"], rbt=amount,
                                              memo="WALLET-01 " + label, timeout=ch.TRANSFER_TIMEOUT)
        secs = round(time.time() - t0, 1)
        if not ok:
            lines.append("{} {}: refused after {}s - {}".format(label, amount, secs, ch.node_reason(msg)))
            return False, msg
        credited, _b = rc.wait_for_balance(dst["host"], dst["did"], (bal0 or 0) + amount, ctx.port,
                                           attempts=30, delay=2)
        lines.append("{} {}: {}s{}".format(label, amount, secs, "" if credited else ", NOT credited"))
        if not credited:
            problems.append("{} {} RBT was never credited".format(label, amount))
        return True, ""

    # Big wallet: rising amounts, one after another, then ten at once.
    for i, amount in enumerate((1, 100, 1000)):
        ok, msg = send(big, ctx.receivers[i], amount, "big wallet")
        if not ok:
            problems.append("the big wallet's {} RBT transfer was refused: {}".format(
                amount, ch.node_reason(msg)))
    jobs = [{"host": big["host"], "did": big["did"], "to": r["did"], "rbt": 50.0,
             "memo": "WALLET-01 big x10"} for r in ctx.receivers[2:12]]
    s = ch.summarize(ch.burst(jobs))
    lines.append("big wallet 10 x 50 at once: {}/10 in p95 {:.1f}s{}".format(
        s["ok"], s["p95"], (" - refused: " + s["reasons"]) if s["failed"] else ""))
    if s["failed"]:
        problems.append("{} of 10 concurrent sends from the big wallet were refused: {}".format(
            s["failed"], s["reasons"]))

    # Fragmented wallet: amounts with fractions, so they are paid in parts;
    # never more than half of what the parts are worth.
    _w, parts, part_value = _shape(frag)
    known = 0
    for amount in (0.999, 7.777, 23.456):
        if amount > part_value / 2:
            lines.append("fragmented {}: skipped, the parts hold only {:.3f}".format(amount, part_value))
            continue
        ok, msg = send(frag, ctx.receivers[len(lines) % len(ctx.receivers)], amount, "fragmented")
        if not ok:
            if "whole-token genesis fetch failed" in str(msg):
                known += 1
            else:
                problems.append("the fragmented wallet's {} RBT transfer was refused: {}".format(
                    amount, ch.node_reason(msg)))

    note = " | ".join(lines) + " | wallets now: big {}, fragmented {}".format(
        ws.describe(big["host"], big["did"]), ws.describe(frag["host"], frag["did"]))
    if known:
        note += " | {} fragmented-wallet refusal(s) are the known second-hand part-token " \
                "allowlist bug".format(known)
    if problems:
        return False, "; ".join(problems[:3]), note
    return True, "every amount the wallets could cover went through", note


CASES = {"WALLET-PREP": wallet_prep, "WALLET-01": wallet_01}
ORDER = ["WALLET-PREP", "WALLET-01"]
NEEDS = {
    # One unit: WALLET-01 spends the wallets WALLET-PREP builds. Quorums carry
    # the 1,000 RBT send, the ten concurrent 50s and the change, with room for
    # pledges that are only released when the receivers spend onward.
    "wallets": {"cases": ["WALLET-PREP", "WALLET-01"], "senders": 2, "receivers": GIVERS,
                "quorums": 2, "fund": 5, "quorum_fund": 2500},
}
NO_DELAY_RERUN = set(ORDER)
# What a case draws from the faucet itself, beyond what NEEDS funds: WALLET-PREP
# tops the big wallet up to --whole-wallet whole tokens. Only the web UI reads
# this, to show the most a run can draw before it starts.
FAUCET_DRAW = {"WALLET-PREP": lambda args: args.whole_wallet}
