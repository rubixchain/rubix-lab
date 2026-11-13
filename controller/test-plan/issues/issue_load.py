"""issue_load.py - issues that show when many payments meet one quorum at once,
or one payment carries many tokens; checked with tokens whose history is long
and proper (issue_kit.py). FAIL = the issue reproduced.
"""

import math
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import issue_kit as kit_mod
from issue_kit import ch, db, rc, steps

PAYERS = 20                     # MANY-PARTS-AT-ONCE: payers at once, through one quorum
QUORUM_HOLDS = 50               # ... which holds this many whole tokens
RUNGS = (200, 300, 400)         # PLEDGES-AT-ONCE: payments at once through one quorum
LEAST_PAYERS = 100              # ... below this many qualifying payers it is not run
TOKENS = (100, 250, 500, 750)   # BIG-TRANSFER-MEMORY: tokens in one payment
SUPERLINEAR = 1.6               # memory growing as tokens^this or faster is not linear


def _ring(payers, amount):
    """Each payer pays the next, the last the first: every wallet pays once and
    is paid once."""
    return [{"host": a["host"], "did": a["did"], "to": b["did"],
             "rbt": amount() if callable(amount) else amount, "memo": "issue check"}
            for a, b in ch.ring(payers)]


def _refused(results, jobs, payers):
    by_did = dict((e["did"], e) for e in payers)
    return [(by_did[j["did"]], j, r["msg"]) for r, j in zip(results, jobs) if not r["ok"]]


PURPOSE = {
    "MANY-PARTS-AT-ONCE":
        "Part-RBT payments through one quorum at once deadlock on token_denom (every writer "
        "updates one row per denomination in Go map order, core/wallet/persist_genesis_tx.go:209), "
        "leave the quorum's counters wrong (the counter is changed in a second DB transaction, "
        "core/pledge_v2.go:287) and RBT Locked, and leave the refused payment in the quorum's copy "
        "of the payer's chain, so the same token is refused there again. A quorum whose counter "
        "is above its Free tokens is then refused a pledge it can afford (the lock plans from the "
        "counters, core/wallet/token_lock.go).",
    "PLEDGES-AT-ONCE":
        "Hundreds of payments through one quorum at once: some pledges lose their lock between "
        "being locked and being used ('not locked by this referenceID', core/pledge_v2.go:99-135), "
        "and a token can end up pledged by two DIDs at once.",
    "BIG-TRANSFER-MEMORY":
        "A quorum's memory grows with the square of the tokens in one payment (+2.2 GB for 500 "
        "tokens, +8.8 GB for 1,000, 2026-11-03) until the kernel kills it.",
}

STEPS = {
    "MANY-PARTS-AT-ONCE": [
        ("S1", "Set-up",
         "Qw = an empty wallet given {} tokens, set up as quorum; {} payers whose next 2 tokens have "
         "long histories, each pointed at Qw".format(QUORUM_HOLDS, PAYERS),
         "the participants exist; Qw's counters match its tokens"),
        ("S2", "{} part payments at once".format(PAYERS),
         "each payer pays the next a random 0.100-0.900 RBT, all at the same instant, through Qw",
         "every payment accepted and credited; nothing Locked; Qw's counters still match its tokens"),
        ("S3", "Again", "as S2", "as S2"),
        ("S4", "Send the refused ones again",
         "for each payment refused in S2/S3: compare the payer's next token's chain on its node and on "
         "Qw's; pay the same amount again through Qw",
         "accepted and credited"),
        ("S5", "Qw pledges exactly its Free RBT",
         "only if Qw's token_denom counters are now above its Free tokens: a payer pays exactly Qw's "
         "Free RBT through Qw", "accepted and credited"),
    ],
    "PLEDGES-AT-ONCE": [
        ("S1", "Set-up",
         "Q = a quorum, its node restarted, topped up to cover {} pledges at once; up to {} payers whose "
         "next {} tokens have long histories, pointed at Q".format(2 * max(RUNGS), max(RUNGS), len(RUNGS)),
         "at least {} payers (the first round runs with all of them if fewer than {})".format(
             LEAST_PAYERS, RUNGS[0])),
        ("S2", "{} payments at once".format(RUNGS[0]),
         "each payer pays the next 1 RBT, all at the same instant, through Q; then look for any of "
         "the payers' or Q's tokens held by two DIDs",
         "every payment accepted and credited; no token held twice"),
        ("S3", "{} payments at once".format(RUNGS[1]), "as S2", "as S2"),
        ("S4", "{} payments at once".format(RUNGS[2]), "as S2", "as S2"),
    ],
    "BIG-TRANSFER-MEMORY": [
        ("S1", "Set-up",
         "Q = a quorum, its node restarted, holding {} RBT; A = a wallet holding {} fit tokens; B = an "
         "empty wallet; Q2 = the quorum for B's payments back".format(max(TOKENS) + 10, max(TOKENS)),
         "the participants exist"),
    ] + [("S{}".format(i + 2), "One payment of {} tokens".format(n),
          "read Q's Rubix memory; A pays B {} RBT through Q while it is read every 2s; B pays it back "
          "through Q2".format(n),
          "accepted and credited; Q's memory grows roughly in proportion to the tokens")
         for i, n in enumerate(TOKENS)] + [
        ("S{}".format(len(TOKENS) + 2), "How the memory grew",
         "compare the growth between payments", "no faster than in proportion to the tokens"),
    ],
}

CASE_INFO = {
    "MANY-PARTS-AT-ONCE": (
        "Part-RBT payments through one quorum at the same time are refused with a database "
        "deadlock, leave the quorum's denomination counters wrong and RBT Locked, and the same "
        "token is refused through that quorum again afterwards.",
        "Every payment goes through, or is refused with a stated reason and leaves nothing behind."),
    "PLEDGES-AT-ONCE": (
        "Hundreds of payments through one quorum at once: some are refused because the quorum lost "
        "the lock on its pledge, and a token can end up pledged by two wallets.",
        "Every payment goes through; no token is held by two wallets."),
    "BIG-TRANSFER-MEMORY": (
        "A quorum's memory grows with the square of the number of tokens in one payment, until the "
        "machine runs out of memory.",
        "Memory grows no faster than the number of tokens, and comes back afterwards."),
}


# ---------------------------------------------------------------------------
# MANY-PARTS-AT-ONCE
# ---------------------------------------------------------------------------

def many_parts_at_once(ctx, ci):
    case = "MANY-PARTS-AT-ONCE"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {"refused": []}

    def setup():
        Q2, why = kit.quorum(need=QUORUM_HOLDS + 10, role="Q2 (quorum for the set-up)")
        if not Q2:
            return False, why
        Qw, why = kit.empty(kit.other_host(), Q2, role="Qw (quorum)")
        if not Qw:
            return False, why
        Qw, why = kit.fill(Qw, QUORUM_HOLDS, Q2, min_chain=1, why="the quorum's holding")
        if not Qw:
            return False, why
        ok, why = kit.as_quorum(Qw, QUORUM_HOLDS)
        if not ok:
            return False, why
        payers, why = kit.holders(PAYERS, k=2)
        if len(payers) < PAYERS:
            return False, why
        for i, e in enumerate(payers):
            kit.join("payer {}".format(i + 1), e)
        ok, why = kit.wire(payers, Qw)
        if not ok:
            return False, why
        p.update(Qw=Qw, payers=payers,
                 notes=[kit_mod.who(Qw, "Qw"), "{} payers on {} machines".format(
                     len(payers), len(set(e["machine"] for e in payers)))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    Qw, payers = p["Qw"], p["payers"]

    def round_(st):
        jobs = _ring(payers, lambda: ch.rand_value(0.1, 0.9))
        for e in payers:
            kit.note_moved(e, e, e["tokens"][:1], "split for a part payment")
        kit_mod.afford(Qw, sum(j["rbt"] for j in jobs))
        results = st.add(ch.burst(jobs), jobs)
        p["refused"] += _refused(results, jobs, payers)
    run.step("S2", round_, nodes=payers + [Qw], must_accept=True)
    run.step("S3", round_, nodes=payers + [Qw], must_accept=True)

    def again(st):
        by_payer = {}
        for e, job, _msg in p["refused"]:
            by_payer[e["did"]] = (e, job)
        ahead = []
        for e, job in list(by_payer.values())[:10]:
            w = kit_mod.scan(ctx, 1, [e["host"]]).get(e["did"]) or {}
            token = (w.get("next") or [(None, 0)])[0][0]
            if token:
                mine, copy = kit_mod.tip_on(e["host"], token), kit_mod.tip_on(Qw["host"], token)
                if mine and copy and copy[0] > mine[0]:
                    ahead.append("{}: the payer's chain ends at {}, Qw's copy at {}".format(
                        token, mine[0], copy[0]))
            to = next(x for x in payers if x["did"] == job["to"])
            kit_mod.afford(Qw, job["rbt"])
            ch.send(st, e, to, rbt=job["rbt"], memo="{} again".format(case))
        st.data["quorum_copy_ahead"] = ahead
        if ahead:
            st.note("{} refused payer(s)' next token has an entry in Qw's copy the payer does not "
                    "have, e.g. {}".format(len(ahead), ahead[0]))
    run.step("S4", again, nodes=payers + [Qw], must_accept=True, required=False,
             require=[lambda: (bool(p["refused"]), "nothing was refused in S2 or S3")])

    def exact(st):
        w = kit_mod.scan(ctx, 1, [Qw["host"]]).get(Qw["did"]) or {}
        above = dict((v, c) for v, c in (w.get("drift") or {}).items() if c[0] > c[1])
        if not above:
            st.skip("Qw's token_denom counters are not above its tokens - nothing to test")
            return
        amount = round(w["free"], 3)
        st.note("Qw holds {:.3f} RBT Free; its counters say more than it holds for {}".format(
            amount, ", ".join("{} ({} counted, {} held)".format(v, c[0], c[1]) for v, c in sorted(above.items()))))
        P, why = kit.holders(1, k=int(math.ceil(amount)) + 1, min_chain=1)
        if not P:
            st.skip(why)
            return
        R, why = kit.wallet_on(role="R (payee)", tokens=kit.tokens_of(P[0]))
        if not R:
            st.skip(why)
            return
        P = kit.join("P (payer)", P[0])
        kit.wire([P], Qw)
        kit.note_moved(P, R, P["tokens"], "Qw pledges exactly its Free RBT")
        ch.send(st, P, R, rbt=amount, memo="{} exact pledge".format(case))
    run.step("S5", exact, nodes=[Qw], must_accept=True, required=False)
    return kit_mod.verdict(run, kit, "part payments at once were refused or left damage behind",
                           "every part payment went through and left nothing behind")


# ---------------------------------------------------------------------------
# PLEDGES-AT-ONCE
# ---------------------------------------------------------------------------

def _q_tokens(q):
    try:
        return [t for (t,) in db.query(q["host"], "SELECT token_id FROM tokens WHERE did = %s AND "
                                                  "token_type = %s AND token_status = ANY(%s)",
                                       (q["did"], db.TYPE_RBT, [db.FREE, db.PLEDGED, db.QUORUM_PLEDGED]))]
    except Exception:
        return []


def pledges_at_once(ctx, ci):
    case = "PLEDGES-AT-ONCE"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q, why = kit.quorum(need=2 * max(RUNGS) + 20, fresh=True)
        if not Q:
            return False, why
        payers, why = kit.holders(max(RUNGS), k=len(RUNGS), per_node=10, spread=True)
        found = len(payers)
        payers = kit.ring_clean(payers, len(RUNGS))
        if len(payers) < LEAST_PAYERS:
            return False, "{} payer(s) qualified, {} after dropping those whose tokens already have a "\
                          "row on a following payer's node - at least {} needed{}".format(
                              found, len(payers), LEAST_PAYERS, "; " + why if why else "")
        for i, e in enumerate(payers):
            kit.join("payer {}".format(i + 1), e)
        ok, why = kit.wire(payers, Q)
        if not ok:
            return False, why
        p.update(Q=Q, payers=payers,
                 tokens=[t for e in payers for t, _c in e["tokens"]],
                 notes=[kit_mod.who(Q, "Q"), "{} payers on {} nodes".format(
                     len(payers), len(set(e["host"] for e in payers)))])
        p["twice_before"] = set(kit_mod.held_twice(ctx, p["tokens"] + _q_tokens(Q)))
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    Q, payers = p["Q"], p["payers"]

    for i, n in enumerate(RUNGS):
        if i == 0:
            n = min(n, len(payers))     # the first round runs with every payer that qualified
        def rung(st, n=n, i=i):
            who = payers[:n]
            for e in who:
                kit.note_moved(e, e, e["tokens"][i:i + 1], "{} at once".format(n))
            jobs = _ring(who, 1.0)
            kit_mod.afford(Q, n)
            st.add(ch.burst(jobs), jobs)
            twice = kit_mod.held_twice(ctx, p["tokens"] + _q_tokens(Q))
            new = dict((t, h) for t, h in twice.items() if t not in p["twice_before"])
            st.data["held_twice"] = new
            st.check(not new, "{} token(s) held by two DIDs since the payments, e.g. {} by {}".format(
                len(new), next(iter(new), ""), next(iter(new.values()), "")))
        run.step("S{}".format(i + 2), rung, nodes=payers[:n] + [Q], must_accept=True,
                 required=(i == 0),
                 require=[(len(payers) >= n, "only {} payers qualified".format(len(payers)))])
    return kit_mod.verdict(run, kit, "payments at once through one quorum were refused or a token "
                                     "ended up held twice",
                           "every payment went through; no token held twice")


# ---------------------------------------------------------------------------
# BIG-TRANSFER-MEMORY
# ---------------------------------------------------------------------------

class _Sampler(object):
    """The quorum's Rubix memory, read every 2s in the background."""

    def __init__(self, host):
        self.host, self.peak, self._stop = host, None, threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)

    def _read(self):
        v = kit_mod.rss_mb(self.host)
        if v is not None:
            self.peak = v if self.peak is None else max(self.peak, v)

    def _loop(self):
        while not self._stop.is_set():
            self._read()
            self._stop.wait(2)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=30)
        self._read()            # the reading as the payment ends


def big_transfer_memory(ctx, ci):
    case = "BIG-TRANSFER-MEMORY"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {"growth": {}}

    def setup():
        Q2, why = kit.quorum(need=max(TOKENS) + 10, role="Q2 (quorum for the payments back)")
        if not Q2:
            return False, why
        Q, why = kit.quorum(need=max(TOKENS) + 10, fresh=True, role="Q (quorum measured)")
        if not Q:
            return False, why
        A, why = kit.holders(1, k=max(TOKENS), min_chain=1)
        if not A:
            return False, why
        A = kit.join("A (payer)", A[0])
        B, why = kit.empty(kit.clear_host(kit.tokens_of(A)), Q2, role="B")
        if not B:
            return False, why
        for e, q in ((A, Q), (B, Q2)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        cs = sorted(c for _t, c in A["tokens"])
        p.update(A=A, B=B, Q=Q, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((A, "A"), (B, "B"), (Q, "Q"), (Q2, "Q2"))] +
                 ["A's {} tokens: chains of {}-{} entries, median {}".format(len(cs), cs[0], cs[-1],
                                                                              cs[len(cs) // 2])])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    A, B, Q, Q2 = p["A"], p["B"], p["Q"], p["Q2"]

    for i, n in enumerate(TOKENS):
        def pay(st, n=n):
            avail = kit_mod.mem_available_mb(Q["host"])
            if avail is not None and p["growth"]:
                # if the memory grows with the square of the tokens, this payment
                # needs (n / last)^2 times the last growth - and 2 GB to spare
                last_n = max(p["growth"])
                need = p["growth"][last_n] * (float(n) / last_n) ** 2 + 2048
                if avail < need:
                    st.skip("Q's machine has {} MB available, {:.0f} MB needed if the memory grows "
                            "with the square of the tokens".format(avail, need))
                    return
            kit_mod.afford(Q, n)
            kit_mod.afford(Q2, n)
            base = kit_mod.rss_mb(Q["host"])
            kit.note_moved(A, B, A["tokens"][:n], "{} tokens in one payment".format(n))
            with _Sampler(Q["host"]) as s:
                ok, _m, res = ch.send(st, A, B, rbt=float(n), memo="{} {}".format(case, n),
                                      timeout=rc.transfer_timeout(n))
                time.sleep(10)
            if base is not None and s.peak is not None:
                p["growth"][n] = max(0, s.peak - base)
                st.data.update(rss_before_mb=base, rss_peak_mb=s.peak, growth_mb=p["growth"][n])
                st.note("Q's Rubix: {} MB before, {} MB at its peak (+{} MB)".format(base, s.peak,
                                                                                    p["growth"][n]))
            if ok and kit_mod.delivered(B, kit_mod.txid_of(res), 60 + n // 10):
                # back to A, so the next payment moves the same tokens again
                ch.send(st, B, A, rbt=float(n), memo="{} back".format(case),
                        timeout=rc.transfer_timeout(n))
        run.step("S{}".format(i + 2), pay, nodes=[A, B, Q, Q2], must_accept=True, required=(i == 0))

    def shape(st):
        g = sorted(p["growth"].items())
        st.data["growth_mb"] = dict(g)
        st.note("growth by tokens in one payment: " + ", ".join("{}: +{} MB".format(n, m) for n, m in g))
        for (n1, m1), (n2, m2) in zip(g, g[1:]):
            if m1 < 100 or m2 <= 0:
                continue
            e = math.log(float(m2) / m1) / math.log(float(n2) / n1)
            st.note("{} -> {} tokens: memory x{:.1f} for tokens x{:.1f} (exponent {:.1f})".format(
                n1, n2, float(m2) / m1, float(n2) / n1, e))
            st.check(e < SUPERLINEAR, "from {} to {} tokens Q's memory grew as tokens^{:.1f} "
                                      "(+{} MB -> +{} MB)".format(n1, n2, e, m1, m2))
    run.step("S{}".format(len(TOKENS) + 2), shape, nodes=[], watch=False, deliver=False,
             require=[lambda: (len(p["growth"]) >= 2, "fewer than two payments were measured")])
    return kit_mod.verdict(run, kit, "the quorum's memory grew faster than the tokens it signed for",
                           "the quorum's memory grew no faster than the tokens")


CASES = {
    "MANY-PARTS-AT-ONCE": many_parts_at_once,
    "PLEDGES-AT-ONCE": pledges_at_once,
    "BIG-TRANSFER-MEMORY": big_transfer_memory,
}
ORDER = list(CASES)
NEEDS = dict((t, {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True}) for t in ORDER)
NO_DELAY_RERUN = set(ORDER)
