"""issue_assets.py - issues with NFTs and smart contracts: two executes at once,
and an execute from a node that does not hold the token. Every NFT and contract
is given a history first (deployed, then executed several times by its owner),
so no check plays on a token fresh from its deploy. FAIL = the issue reproduced.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import issue_kit as kit_mod
from issue_kit import case_evidence, ch, rc, steps
import asset_cases

HISTORY = 4             # executes by the owner after the deploy, before anything else
RACES = 3               # ASSET-FORK: rounds of two executes at the same instant

NFT = ("NFT", rc.get_nft_chain)
SC = ("contract", rc.get_sc_chain)


def _execute(st, kind, who, token, data):
    """One execute by `who`, recorded on the step."""
    if kind is NFT:
        return ch.timed(st, rc.nft_transaction, who["host"], who["did"], who["did"], token, data=data)
    return ch.timed(st, rc.sc_transaction, who["host"], who["did"], token, data=data)


def _job(kind, who, token, data):
    if kind is NFT:
        return {"host": who["host"], "did": who["did"], "to": who["did"], "memo": data,
                "nft": [{"nftId": token, "value": 1.0, "data": data}]}
    return {"host": who["host"], "did": who["did"], "to": "", "memo": data,
            "smart_contract": [{"smartContractId": token, "value": 1.0, "data": data}]}


def _with_history(st, ctx, kind, owner, case):
    """Create and deploy an NFT or a contract on `owner`, then execute it
    HISTORY times. Returns its ID or None."""
    if kind is NFT:
        ok, msg, token = ch.timed(st, rc.create_nft, owner["host"], owner["did"],
                                  asset_cases._random_bytes(), asset_cases._random_bytes())
        if not ok or not isinstance(token, str):
            return None
        ok, _m, _r = ch.timed(st, rc.nft_transaction, owner["host"], owner["did"], owner["did"], token,
                              data="{} deploy".format(case))
    else:
        token, err = ch._sc_new_contract(ctx, owner)
        if not token:
            st.op(False, "generating the contract failed: {}".format(err))
            return None
        ok, _m, _r = ch.timed(st, rc.sc_transaction, owner["host"], owner["did"], token,
                              data="{} deploy".format(case))
    if not ok:
        return None
    for i in range(HISTORY):
        ok, _m, _r = _execute(st, kind, owner, token, "{} history {}".format(case, i + 1))
        if not ok:
            return None
    n = asset_cases._chain_len(kind[1], owner, token) or 0
    st.check(n >= HISTORY + 1, "the {}'s chain on its owner has {} entries after a deploy and {} "
                               "executes".format(kind[0], n, HISTORY))
    st.note("{} {} has {} chain entries".format(kind[0], token[:12], n))
    return token


PURPOSE = {
    "ASSET-FORK":
        "Two simultaneous executes of one NFT or one contract, signed by two different quorums, are "
        "both accepted and the chain forks: each quorum checks against its own copy and syncs from "
        "the executor, and nothing orders the executes of one token.",
    "FOREIGN-EXECUTE":
        "An execute from a node that does not hold the NFT or contract is built as a NEW deploy - a "
        "second genesis of an existing token (core/transaction_builder.go:369) - and a quorum that "
        "has not seen the token accepts it (core/consensus/checks.go:836); subscribing reports "
        "success even when its chain sync fails (core/nft.go:150).",
}

STEPS = {
    "ASSET-FORK": [
        ("S1", "Set-up",
         "O (owner) and S (subscriber) on two machines; O signs with Q1, S with Q2",
         "the participants exist"),
        ("S2", "An NFT with a history",
         "O creates and deploys an NFT and executes it {} times; S subscribes, waits until its copy "
         "is O's, executes it once".format(HISTORY),
         "all accepted; S's chain is O's"),
        ("S3", "O and S execute the NFT at the same instant",
         "{} times: both copies equal, then O and S execute together".format(RACES),
         "one is refused with a stated reason, or both go through one after the other - the two "
         "chains never differ"),
        ("S4", "A contract with a history", "as S2, with a smart contract", "as S2"),
        ("S5", "O and S execute the contract at the same instant", "as S3", "as S3"),
    ],
    "FOREIGN-EXECUTE": [
        ("S1", "Set-up",
         "O (owner) signs with Q1; F, G = DIDs on other machines that never had the tokens, "
         "signing with Q3 - a quorum that never saw them", "the participants exist"),
        ("S2", "An NFT and a contract with a history",
         "O creates, deploys and executes each {} times".format(HISTORY), "all accepted"),
        ("S3", "Execute the NFT from a node that never had it",
         "F executes the NFT through Q3", "refused: F does not hold it"),
        ("S4", "Subscribe to the NFT and execute at once",
         "G subscribes and executes the NFT straight away",
         "refused, or accepted on G's synced copy - G's chain is O's, never a new genesis"),
        ("S5", "Execute the contract from a node that never had it",
         "F executes the contract through Q3", "refused: F is not subscribed"),
        ("S6", "Subscribe to the contract and execute at once", "as S4, for the contract", "as S4"),
    ],
}

CASE_INFO = {
    "ASSET-FORK": (
        "Two executes of one NFT (or one contract) at the same moment, through two quorums, are both "
        "accepted and leave two different chains.",
        "One of the two is refused, or both are applied one after the other; there is only ever one "
        "chain."),
    "FOREIGN-EXECUTE": (
        "An NFT or contract executed from a node that does not hold it becomes a second genesis of "
        "that token.",
        "The execute is refused until the node holds the token's chain."),
}


# ---------------------------------------------------------------------------
# ASSET-FORK
# ---------------------------------------------------------------------------

def asset_fork(ctx, ci):
    case = "ASSET-FORK"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q1, why = kit.quorum(need=30, role="Q1 (quorum)")
        if not Q1:
            return False, why
        Q2, why = kit.quorum(need=30, role="Q2 (quorum)")
        if not Q2:
            return False, why
        O, why = kit.wallet_on(role="O (owner)")
        if not O:
            return False, why
        S, why = kit.wallet_on(role="S (subscriber)")
        if not S:
            return False, why
        for e, q in ((O, Q1), (S, Q2)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        p.update(O=O, S=S, Q1=Q1, Q2=Q2,
                 notes=[kit_mod.who(e, n) for e, n in ((O, "O"), (S, "S"), (Q1, "Q1"), (Q2, "Q2"))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    O, S, Q1, Q2 = p["O"], p["S"], p["Q1"], p["Q2"]
    everyone = [O, S, Q1, Q2]

    for sid, rid, kind in (("S2", "S3", NFT), ("S4", "S5", SC)):
        def ready(st, kind=kind):
            token = _with_history(st, ctx, kind, O, case)
            if not token:
                return
            ok, msg = (rc.subscribe_nft if kind is NFT else rc.subscribe_smart_contract)(S["host"], token)
            st.op(ok, msg)
            if not ok:
                return
            synced, lag = asset_cases._caught_up(kind[1], S, O, token)
            if not st.check(synced, "S's copy of the {} was still behind after {:.0f}s".format(kind[0], lag)):
                return
            ok, _m, _r = _execute(st, kind, S, token, "{} subscriber".format(case))
            if ok:
                forked, why = asset_cases._fork(kind[1], O, S, token)
                st.check(not forked and not why, "{}: {}".format(kind[0], why or "forked"))
                p[kind[0]] = token
        run.step(sid, ready, nodes=everyone, must_accept=True, deliver=False)

        def race(st, kind=kind):
            token, forks = p[kind[0]], 0
            for r in range(RACES):
                asset_cases._caught_up(kind[1], S, O, token)
                asset_cases._caught_up(kind[1], O, S, token)
                jobs = [_job(kind, O, token, "{} race {}".format(case, r + 1)),
                        _job(kind, S, token, "{} race {}".format(case, r + 1))]
                st.add(ch.burst(jobs), jobs)
                forked, why = asset_cases._fork(kind[1], O, S, token)
                forks += bool(forked)
                if forked:
                    st.note("round {}: {}".format(r + 1, why))
                time.sleep(ch.SETTLE)
            st.data["forks"] = forks
            st.check(not forks, "the {}'s chain FORKED in {} of {} rounds: O and S hold different "
                                "entries at the same position".format(kind[0], forks, RACES))
        run.step(rid, race, nodes=everyone, deliver=False,
                 require=[lambda kind=kind: (bool(p.get(kind[0])), "S2/S4 did not make the {}".format(kind[0]))])
    return kit_mod.verdict(run, kit, "two executes at once forked the chain",
                           "two executes at once never forked the chain")


# ---------------------------------------------------------------------------
# FOREIGN-EXECUTE
# ---------------------------------------------------------------------------

def foreign_execute(ctx, ci):
    case = "FOREIGN-EXECUTE"
    run, kit, p = steps.CaseRun(ctx, case, STEPS[case]), kit_mod.Kit(ctx, case), {}

    def setup():
        Q1, why = kit.quorum(need=30, role="Q1 (quorum)")
        if not Q1:
            return False, why
        Q3, why = kit.quorum(need=30, role="Q3 (quorum that never saw the tokens)")
        if not Q3:
            return False, why
        O, why = kit.wallet_on(role="O (owner)")
        if not O:
            return False, why
        F, why = kit.wallet_on(role="F")
        if not F:
            return False, why
        G, why = kit.wallet_on(role="G")
        if not G:
            return False, why
        for e, q in ((O, Q1), (F, Q3), (G, Q3)):
            ok, why = kit.wire([e], q)
            if not ok:
                return False, why
        p.update(O=O, F=F, G=G, Q1=Q1, Q3=Q3,
                 notes=[kit_mod.who(e, n) for e, n in ((O, "O"), (F, "F"), (G, "G"), (Q1, "Q1"), (Q3, "Q3"))])
        return True, ""
    if kit_mod.set_up(run, p, setup).outcome != steps.PASS:
        return kit_mod.verdict(run, kit, "", "")
    O, F, G, Q1, Q3 = p["O"], p["F"], p["G"], p["Q1"], p["Q3"]

    def make(st):
        for kind in (NFT, SC):
            p[kind[0]] = _with_history(st, ctx, kind, O, case)
    run.step("S2", make, nodes=[O, Q1], must_accept=True, deliver=False)

    def chains(st, kind, who, token, txid):
        mine = asset_cases._chain(kind[1], who, token) or []
        theirs = asset_cases._chain(kind[1], O, token) or []
        st.data.update(chain_len=len(mine), owner_chain_len=len(theirs))
        st.note("{}'s node has {} chain entries for the {}, the owner's {}".format(
            kit_mod.short(who["host"]), len(mine), kind[0], len(theirs)))
        if txid and case_evidence.FULLNODE_HOSTS:
            out = case_evidence.fullnode_outcome([txid], wait=60)
            said = sorted(set(case_evidence.explain_fullnode_reason(r) for _a, rej, _u, _e in out.values()
                              for r in rej.values()))
            if said:
                st.note("fullnodes rejected it: " + "; ".join(said)[:240])
        return mine, theirs

    for never_sid, once_sid, kind in (("S3", "S4", NFT), ("S5", "S6", SC)):
        def never(st, kind=kind):
            ok, _m, res = _execute(st, kind, F, p[kind[0]], "{} from a node without it".format(case))
            if ok:
                chains(st, kind, F, p[kind[0]], kit_mod.txid_of(res))
        run.step(never_sid, never, nodes=[F, Q3], expect_refusal=True, deliver=False,
                 require=[lambda kind=kind: (bool(p.get(kind[0])), "S2 did not make the {}".format(kind[0]))])

        def at_once(st, kind=kind):
            ok, msg = (rc.subscribe_nft if kind is NFT else rc.subscribe_smart_contract)(G["host"], p[kind[0]])
            st.note("subscribe answered: {}".format(msg or ok))
            ok, _m, res = _execute(st, kind, G, p[kind[0]], "{} right after subscribing".format(case))
            if ok:
                mine, theirs = chains(st, kind, G, p[kind[0]], kit_mod.txid_of(res))
                st.check(len(mine) > len(theirs) // 2 and mine[:1] == theirs[:1],
                         "G's {} chain starts with another entry than the owner's ({} entries vs "
                         "{}) - a second genesis".format(kind[0], len(mine), len(theirs)))
        run.step(once_sid, at_once, nodes=[G, Q3], deliver=False,
                 require=[lambda kind=kind: (bool(p.get(kind[0])), "S2 did not make the {}".format(kind[0]))])
    return kit_mod.verdict(run, kit, "an execute from a node without the token was accepted as a new "
                                     "genesis",
                           "executes from a node without the token were refused")


CASES = {"ASSET-FORK": asset_fork, "FOREIGN-EXECUTE": foreign_execute}
ORDER = list(CASES)
NEEDS = dict((t, {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True}) for t in ORDER)
NO_DELAY_RERUN = set(ORDER)
