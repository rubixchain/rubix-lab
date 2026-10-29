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
    them can hit the known minter-allowlist bug; the run counts that apart.
WALLET-01 then finds what one transfer from a big wallet can carry, checks
that asking for more than the wallet holds is refused at once without locking
its thousands of tokens, that concurrent sends from one wallet never reuse a
token, and that fractional amounts are paid from parts.

Kept out of the way: the quorums are funded for the largest transfer (a
pledge is held until the receiver spends onward), the receivers are other
nodes than the wallets', and every machine is watched (master/steps.py).
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
import steps

SKIP = "SKIP"
GIVERS = 30                 # wallets sending change to the fragmented wallet
MAX_ROUNDS = 12
AMOUNTS = (1, 10, 100, 500, 1000, 2000)     # the value ladder stops well under 5,000

PURPOSE = {
    "WALLET-PREP": "What it costs to fill a wallet with thousands of whole tokens, and whether "
                   "30 wallets paying change into ONE wallet at the same moment all land with "
                   "nothing left Locked.",
    "WALLET-01": "How much one transfer from a big wallet can move and how the time grows with "
                 "it; that asking for more than a wallet holds is refused at once without "
                 "locking its tokens; that concurrent sends from one wallet never reuse a token; "
                 "that fractional amounts are paid from part tokens.",
}

STEPS = {
    "WALLET-PREP": [
        ("S1", "Fill the big wallet",
         "top the big wallet up to --whole-wallet whole tokens from the faucet",
         "every faucet transfer accepted and credited; the time per token is recorded"),
        ("S2", "Fragment a wallet (one per round)",
         "30 givers each send a random fraction into the fragmented wallet at the same instant",
         "all 30 accepted and credited; nothing left Locked on any giver or the wallet; the "
         "token_denom counters right"),
    ],
    "WALLET-01": [
        ("S1", "One transfer, rising amount (one per amount)",
         "the big wallet sends 1, 10, 100, 500, 1,000 and 2,000 RBT, one transfer each",
         "accepted and credited, the time per token recorded; where Rubix cannot carry an "
         "amount, a stated refusal within the budget and nothing Locked"),
        ("S2", "More than the wallet holds",
         "the big wallet asks to send 1 RBT more than it holds",
         "refused for insufficient balance at once; none of its tokens left Locked"),
        ("S3", "Ten sends at once from one wallet",
         "the big wallet sends 50 RBT to ten receivers at the same instant",
         "every accepted send moved its own tokens - the wallet down by exactly 50 per accepted "
         "send - and every refusal stated; nothing Locked"),
        ("S4", "Fractional amounts from parts (one per amount)",
         "the fragmented wallet sends 0.999 and 7.777 RBT, paid in part tokens (never more than "
         "half of what its parts hold)",
         "accepted and credited; a refusal by the known second-hand part-token bug is counted "
         "apart"),
    ],
}


def _shape(entry):
    """(whole tokens, part tokens, part value) of a wallet, from its database."""
    denoms = db.real_free_denoms(entry["host"], entry["did"])
    whole = sum(c for v, c in denoms.items() if v >= 1.0)
    parts = sum(c for v, c in denoms.items() if v < 1.0)
    part_value = round(sum(v * c for v, c in denoms.items() if v < 1.0), 3)
    return whole, parts, part_value


def _free(entry):
    ok, detail, _n = rc.get_rbt_balance_detail(entry["host"], entry["did"])
    return float(detail["balance"]) if ok and detail else 0.0


def wallet_prep(ctx, ci):
    run = steps.CaseRun(ctx, "WALLET-PREP", STEPS["WALLET-PREP"])
    if len(ctx.senders) < 2 or len(ctx.receivers) < 2:
        return SKIP, "needs two wallets and change givers", ""
    big, frag = ctx.senders[0], ctx.senders[1]
    givers = ctx.receivers[:GIVERS]
    want_whole, want_parts = ctx.args.whole_wallet, ctx.args.parts_target

    def fill(st):
        whole0, _p, _v = _shape(big)
        t0 = time.time()
        if whole0 < want_whole:
            ok, msg = rc.fund_did(big["host"], big["did"], want_whole - whole0, ctx.port)
            st.check(ok, "the faucet could not fill the big wallet: {}".format(msg))
        secs = time.time() - t0
        whole1, _p, _v = _shape(big)
        moved = whole1 - whole0
        st.data.update({"whole_before": whole0, "whole_after": whole1, "seconds": round(secs, 1)})
        if moved:
            st.note("{} -> {} whole tokens, {:.0f} ms per token".format(whole0, whole1,
                                                                        1000.0 * secs / moved))
        st.check(whole1 >= want_whole, "the big wallet holds {} whole tokens, short of {}".format(
            whole1, want_whole))
    run.step("S1", fill, nodes=[big], conserve=False)

    rounds, still = 0, 0
    _w, parts, _v = _shape(frag)
    while parts < want_parts and rounds < MAX_ROUNDS and still < 2:
        rounds += 1
        jobs = [{"host": g["host"], "did": g["did"], "to": frag["did"],
                 "rbt": ch.rand_value(0.011, 0.989), "memo": "WALLET-PREP change"} for g in givers]
        st = run.step("S2", lambda st, jobs=jobs: st.add(ch.burst(jobs), jobs, set_aside=ch.ASIDE),
                      nodes=givers + [frag], label="round {}".format(rounds))
        _w, now, _v = _shape(frag)
        st.data["parts"] = now
        still = still + 1 if now <= parts else 0
        parts = now
        if st.outcome not in (steps.PASS, steps.LIMIT):
            break
    passed = "wallets ready: big {} whole tokens, fragmented {} part tokens".format(
        _shape(big)[0], parts)
    if parts < want_parts:
        passed += " (short of the {} part tokens asked for)".format(want_parts)
    return run.verdict(passed=passed)


def wallet_01(ctx, ci):
    run = steps.CaseRun(ctx, "WALLET-01", STEPS["WALLET-01"])
    if len(ctx.senders) < 2 or len(ctx.receivers) < 12:
        return SKIP, "needs the two prepared wallets and 12 receivers", ""
    big, frag = ctx.senders[0], ctx.senders[1]
    recv = ctx.receivers
    quorums = list(ctx.quorum_hosts or [])

    def top_up(amount):
        # Set-up, not a test: the ladder (3,611 RBT) is more than the big wallet's
        # 3,000 tokens, so each rung starts with enough for its own amount.
        short = amount + 1.0 - _free(big)
        if short > 0:
            rc.fund_did(big["host"], big["did"], short, ctx.port)

    for i, amount in enumerate(AMOUNTS):
        r = recv[i % len(recv)]
        top_up(amount)
        st = run.step("S1", lambda st, r=r, amount=amount: ch.send(st, big, r, rbt=float(amount),
                                                                   memo="WALLET-01 big"),
                      nodes=[big, r] + quorums, label="{} RBT".format(amount),
                      require=[lambda amount=amount: steps.holds(big, amount)])
        if st.accepted:
            secs = st.accepted[0]["secs"] or 0
            st.data["ms_per_token"] = round(1000.0 * secs / amount, 1)
            st.summary += "; {:.1f} ms per token".format(1000.0 * secs / amount)
        if st.outcome not in (steps.PASS,):
            break

    over = round(_free(big) + 1.0, 3)
    run.step("S2", lambda st: ch.send(st, big, recv[6], rbt=over, memo="WALLET-01 over"),
             nodes=[big, recv[6]] + quorums, expect_refusal=True, budget=30,
             label="{:.3f} RBT from a wallet holding {:.3f}".format(over, over - 1.0))

    targets = recv[2:12]
    jobs = [{"host": big["host"], "did": big["did"], "to": r["did"], "rbt": 50.0,
             "memo": "WALLET-01 big x10"} for r in targets]

    top_up(500.0)

    def ten(st):
        before = _free(big)
        st.add(ch.burst(jobs), jobs)
        time.sleep(ch.SETTLE)
        spent = round(before - _free(big), 3)
        st.check(abs(spent - 50.0 * len(st.accepted)) <= 0.01,
                 "the wallet is down {:.3f} RBT for {} accepted 50 RBT sends".format(
                     spent, len(st.accepted)))
    run.step("S3", ten, nodes=[big] + targets + quorums,
             require=[lambda: steps.holds(big, 500.0)])

    _w, _parts, part_value = _shape(frag)
    for k, amount in enumerate((0.999, 7.777)):
        r = recv[(k + 13) % len(recv)]
        run.step("S4", lambda st, r=r, amount=amount: ch.send(st, frag, r, rbt=amount,
                                                              memo="WALLET-01 fragmented"),
                 nodes=[frag, r] + quorums, label="{} RBT".format(amount), required=False,
                 must_accept=True,
                 require=[(amount <= part_value / 2, "the parts hold only {:.3f}".format(part_value))])
    return run.verdict(passed="every amount the wallets could cover went through; wallets now: "
                              "big {}, fragmented {}".format(ws.describe(big["host"], big["did"]),
                                                             ws.describe(frag["host"], frag["did"])),
                       limit="the largest single transfer, refused gently")


CASES = {"WALLET-PREP": wallet_prep, "WALLET-01": wallet_01}
ORDER = ["WALLET-PREP", "WALLET-01"]
NEEDS = {
    # One unit: WALLET-01 spends the wallets WALLET-PREP builds. Quorums carry
    # the 2,000 RBT send, the ten concurrent 50s and the change, with room for
    # pledges that are only released when the receivers spend onward.
    "wallets": {"cases": ["WALLET-PREP", "WALLET-01"], "senders": 2, "receivers": GIVERS,
                "quorums": 2, "fund": 5, "quorum_fund": 4500},
}
NO_DELAY_RERUN = set(ORDER)
# What a case draws from the faucet itself, beyond what NEEDS funds: WALLET-PREP
# tops the big wallet up to --whole-wallet whole tokens. Only the web UI reads
# this, to show the most a run can draw before it starts.
FAUCET_DRAW = {"WALLET-PREP": lambda args: args.whole_wallet}
