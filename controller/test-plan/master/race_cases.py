"""race_cases.py - genuine races the product CI (a 3-node localnet) cannot run.

RACE-01  a wallet holding exactly k tokens fires more sends than it can pay at
         the same instant: never more than it holds may go through, and the
         losers must be refused gently - with whole tokens (k = 1, then 5) and
         with a fraction that makes the one token split.

RACE-02  many DIDs on one node, then many across one machine, all transferring
         at the same instant. The quorums are sized from QCAP-01 so the quorum
         is never the bottleneck - what is measured is node- and machine-level
         contention (the Postgres pool, the peer-port pools, one IPFS per node),
         with the machine watched so a CPU or memory ceiling is not blamed on
         Rubix (master/steps.py).

Both wire their own quorums, because a transfer is signed by the first quorum
in the SENDING node's list and these cases point many nodes at once.
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
DEFAULT_CAP = 50            # one quorum's assumed capacity before QCAP-01 has run

PURPOSE = {
    "RACE-01": "Whether Rubix ever lets a wallet pay more than it holds when sends race, and "
               "whether the losing sends are refused gently - with one token, several tokens, and "
               "a token that must be split.",
    "RACE-02": "How many DIDs on ONE node, and on ONE machine, can transfer at the same instant "
               "before Rubix must refuse, and whether it refuses gently - with the machine watched "
               "so a hardware ceiling is not taken for the software's.",
}

STEPS = {
    "RACE-01": [
        ("S1", "Make a one-token wallet",
         "take a DID on the sender's node that holds no RBT and give it exactly 1 whole token",
         "the wallet holds one whole token, 1.000 RBT"),
        ("S2", "One token, many sends",
         "20 sends of 1 RBT from that wallet to 20 receivers at the same instant",
         "exactly one accepted and on its receiver; the others refused with a stated reason "
         "within 60s; the token not left Locked; the wallet empty"),
        ("S3", "Five tokens, more sends",
         "a wallet with exactly 5 whole tokens sends 1 RBT to 20 receivers at the same instant",
         "no more than 5 accepted, each moving its own token; the rest refused gently; the wallet "
         "down by exactly 1 per accepted send; nothing Locked"),
        ("S4", "One token, fractional sends",
         "a one-token wallet sends 0.3 RBT to 10 receivers at the same instant, so the token must "
         "split",
         "at most 3 accepted (0.9 of the 1.0); the wallet down by exactly 0.3 per accepted send; "
         "the rest refused gently; nothing Locked"),
    ],
    "RACE-02": [
        ("S1", "DIDs on one node at once (one per rung)",
         "2, 5, then 10 DIDs on the one node send 1 RBT each at the same instant, through a quorum "
         "on another machine",
         "all accepted; past the node's limit, the rest refused gently; the node keeps answering, "
         "does not restart and can still receive"),
        ("S2", "DIDs on one machine at once (one per rung)",
         "9 (one per node), 18, then 38 DIDs across the machine's nodes send at the same instant, "
         "through enough quorums on other machines",
         "as S1, for the machine"),
    ],
}


def _free(entry):
    ok, detail, _n = rc.get_rbt_balance_detail(entry["host"], entry["did"])
    return float(detail["balance"]) if ok and detail else None


def _wallet(base, k, port):
    """A wallet on `base`'s node holding exactly k whole tokens and nothing else:
    an empty DID, funded with k. (entry or None, why)."""
    e = ch.empty_did(base)
    if e is None:
        return None, "no DID on the sender's node holds nothing - a {}-token wallet cannot be " \
                     "made without draining, and draining is not reliable".format(k)
    ok, msg = rc.fund_did(e["host"], e["did"], k, port)
    if not ok:
        return None, "the faucet could not fund the wallet: {}".format(msg)
    time.sleep(ch.SETTLE)
    tokens, held = ch.whole_free(e["host"], e["did"]), _free(e) or 0.0
    if len(tokens) != k or abs(held - k) > ch.TOL:
        return None, "the wallet holds {} whole token(s), {:.3f} RBT - not exactly {}".format(
            len(tokens), held, k)
    return e, ""


def race_01(ctx, ci):
    run = steps.CaseRun(ctx, "RACE-01", STEPS["RACE-01"])
    recv = ctx.receivers
    n = min(len(recv), int(getattr(ctx.args, "race_receivers", 20)))
    if not ctx.senders or n < 2:
        return SKIP, "needs one sender and at least two receivers", ""
    port, quorums = ctx.port, list(ctx.quorum_hosts or [])

    w, why = _wallet(ctx.senders[0], 1, port)
    s1 = run.step("S1", lambda st: None, nodes=([w] if w else []) + quorums, watch=False,
                  require=[(w is not None, why)])
    if s1.outcome != steps.PASS:
        return run.verdict()

    def race(st, w, k, amount, count):
        before = _free(w) or 0.0
        jobs = [{"host": w["host"], "did": w["did"], "to": recv[i]["did"], "rbt": amount,
                 "memo": "RACE-01 {} x {}".format(count, amount)} for i in range(count)]
        st.add(ch.burst(jobs), jobs)
        time.sleep(ch.SETTLE)
        after = _free(w) or 0.0
        wins = len(st.accepted)
        st.data.update({"wallet_before": before, "wallet_after": after, "accepted": wins})
        st.check(wins * amount <= k + ch.TOL, "DOUBLE SPEND: {} sends of {} accepted from a wallet "
                                              "holding {}".format(wins, amount, k))
        st.check(abs((before - after) - wins * amount) <= 0.002,
                 "the wallet went {:.3f} -> {:.3f} for {} accepted sends of {}".format(
                     before, after, wins, amount))
        if wins == 0:
            st.note("none went through - the wallet was never spent")

    run.step("S2", lambda st: race(st, w, 1, 1.0, n), nodes=[w] + recv[:n] + quorums)
    # Each later wallet is made before its step, so the step watches it.
    w5, why5 = _wallet(ctx.senders[0], 5, port)
    run.step("S3", lambda st: race(st, w5, 5, 1.0, n), nodes=([w5] if w5 else []) + recv[:n] + quorums,
             require=[(w5 is not None, why5)])
    w1, why1 = _wallet(ctx.senders[0], 1, port)
    m = min(10, n)
    run.step("S4", lambda st: race(st, w1, 1, 0.3, m), nodes=([w1] if w1 else []) + recv[:m] + quorums,
             require=[(w1 is not None, why1)])
    return run.verdict(passed="no wallet paid more than it held",
                       limit="the sends the wallet could not cover were refused gently")


# ---------------------------------------------------------------------------
# RACE-02
# ---------------------------------------------------------------------------

def _by_host(fleet):
    out = {}
    for e in fleet:
        out.setdefault(e["host"], []).append(e["did"])
    return out


def _fund_all(entries, target, port):
    def one(e):
        have = _free(e) or 0.0
        return have >= target or rc.fund_did(e["host"], e["did"], target - have, port)[0]
    return all(ch.run_parallel(one, entries, workers=16)) if entries else True


def race_02(ctx, ci):
    run = steps.CaseRun(ctx, "RACE-02", STEPS["RACE-02"])
    fleet = ctx.fleet
    if not fleet:
        return SKIP, "needs the fleet DID list", ""
    port = ctx.args.port
    cap = ch.quorum_capacity(DEFAULT_CAP)
    by_host = _by_host(fleet)

    def quorum_pool(exclude_machine, how_many):
        picked = []
        for host, dids in by_host.items():
            if ch.machine(host) == exclude_machine or not dids:
                continue
            picked.append({"host": host, "did": dids[0]})
            if len(picked) >= how_many:
                break
        return picked

    def ready(qs, floor):
        def req():
            for q in qs:
                ok, why = steps.quorum_ready(q, 0, port)
                if not ok:
                    return ok, why
                have = _free(q) or 0.0
                if have < floor:
                    rc.fund_did(q["host"], q["did"], floor - have, port)
            return True, ""
        return req

    def wire(hosts, qs):
        def req():
            for i, h in enumerate(hosts):
                rc.quorum_reset(h, [qs[i % len(qs)]["did"]], port)
            for i, h in enumerate(hosts):
                ok, why = steps.wired(h, qs[i % len(qs)]["did"], port)
                if not ok:
                    return ok, why
            return True, ""
        return req

    def ladder(sid, group_of, rungs, qs, hosts):
        for k in rungs:
            group = group_of(k)
            if len(group) < 2:
                break
            jobs = [{"host": a["host"], "did": a["did"], "to": b["did"], "rbt": 1.0,
                     "memo": "RACE-02 {}".format(sid)} for a, b in ch.ring(group)]
            st = run.step(sid, lambda st, jobs=jobs: st.add(ch.burst(jobs), jobs, set_aside=ch.ASIDE),
                          nodes=group + qs, label="{} DIDs at once".format(len(group)),
                          require=[(bool(qs), "no quorum on another machine"),
                                   ready(qs, 50 + len(group)), wire(hosts, qs),
                                   (_fund_all(group, 3, port), "the DIDs could not be funded")])
            if st.outcome != steps.PASS:
                break

    # One node: the node holding the most DIDs, through one quorum elsewhere.
    node_host = max(by_host, key=lambda h: len(by_host[h]))
    node = [{"host": node_host, "did": d} for d in by_host[node_host]]
    qs = quorum_pool(ch.machine(node_host), 1)
    ladder("S1", lambda k: node[:k], [r for r in (2, 5, 10) if r <= len(node)] or [len(node)],
           qs, [node_host])
    time.sleep(ch.SETTLE)

    # One machine: every node on it, one DID each first, then more.
    m = ch.machine(node_host)
    hosts = [h for h in by_host if ch.machine(h) == m]
    rounds = []
    for depth in range(max(len(by_host[h]) for h in hosts)):
        for h in hosts:
            if depth < len(by_host[h]):
                rounds.append({"host": h, "did": by_host[h][depth]})
    qs = quorum_pool(m, ch.quorums_for(len(hosts), cap))
    ladder("S2", lambda k: rounds[:k], [r for r in (len(hosts), 2 * len(hosts), 38) if r <= len(rounds)],
           qs, hosts)
    return run.verdict(passed="node- and machine-level concurrency held",
                       limit="the node's or machine's limit, met gently")


CASES = {"RACE-01": race_01, "RACE-02": race_02}
ORDER = ["RACE-01", "RACE-02"]
NEEDS = {
    "RACE-01": {"senders": 1, "receivers": 24, "quorums": 1, "fund": 0, "quorum_fund": 100},
    # Exclusive: it grabs a whole node and a whole machine from the fleet and
    # wires their quorums, so nothing else may be using those nodes.
    "RACE-02": {"senders": 2, "receivers": 0, "quorums": 0, "fund": 0, "exclusive": True},
}
NO_DELAY_RERUN = {"RACE-01", "RACE-02"}
