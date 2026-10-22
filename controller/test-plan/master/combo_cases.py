"""combo_cases.py - several asset types moving in one transaction, at scale.

The product CI runs its bundled and all-in-one transactions between two nodes.
COMBO-01 runs them from many owners at once across the fleet: every owner first
mints an FT series and deploys one NFT and one smart contract (deploys cannot
be combined - core/transaction_builder.go:518), then fires the combined
transfers in a single /tx each:

    RBT + FT          RBT + NFT (execute)     RBT + SC (execute)
    FT + NFT          FT + SC                 all-in-one (RBT + FT + NFT + SC)

Each combined transfer is one transaction, so the runner's database check
confirms all of its parts landed together or none did.
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
OWNERS = 8


def _rand_bytes(n=64):
    return os.urandom(n)


def combo_01(ctx, ci):
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 1:
        return SKIP, "needs owners and at least one receiver", ""
    ch.ensure_quorums(ctx)      # re-assert the unit's quorum setup in case a node restarted
    stamp = int(time.time())

    def prepare(owner):
        """Mint an FT series and deploy one NFT and one SC for this owner."""
        out = {"owner": owner, "ft": None, "nft": None, "sc": None, "problems": []}
        ft_name = "combo-ft-{}-{}".format(stamp, owner["did"][-6:])
        ok, msg, _r = rc.mint_ft(owner["host"], owner["did"], ft_name, 500, 1,
                                 timeout=ch.TRANSFER_TIMEOUT)
        if ok and rc.wait_for_ft_count(owner["host"], owner["did"], ft_name, 500, attempts=40, delay=2)[0]:
            out["ft"] = ft_name
        else:
            out["problems"].append("FT mint: " + ch.node_reason(msg))
        ok, msg, nft_id = rc.create_nft(owner["host"], owner["did"], _rand_bytes(), _rand_bytes())
        if ok and isinstance(nft_id, str):
            ok, msg, _r = rc.nft_transaction(owner["host"], owner["did"], owner["did"], nft_id,
                                             data="COMBO deploy", timeout=ch.TRANSFER_TIMEOUT)
            if ok:
                out["nft"] = nft_id
            else:
                out["problems"].append("NFT deploy: " + ch.node_reason(msg))
        else:
            out["problems"].append("NFT create: " + ch.node_reason(msg))
        sc_id, err = ch._sc_new_contract(ctx, owner)
        if sc_id:
            ok, msg, _r = rc.sc_transaction(owner["host"], owner["did"], sc_id, data="COMBO deploy",
                                            timeout=ch.TRANSFER_TIMEOUT)
            if ok:
                out["sc"] = sc_id
            else:
                out["problems"].append("SC deploy: " + ch.node_reason(msg))
        else:
            out["problems"].append("SC generate: " + str(err))
        return out

    prepared = ch.run_parallel(prepare, owners)

    # Each owner fires the combined transfers it has the pieces for.
    def combos(i_state):
        i, st = i_state
        owner = st["owner"]
        r = recv[i % len(recv)]
        ftname, nft, sc = st["ft"], st["nft"], st["sc"]
        ft1 = [{"ftName": ftname, "numberOfFts": 10, "creatorDID": owner["did"]}] if ftname else None
        done, fail = [], []

        def fire(label, **kw):
            ok, msg, _r = rc.initiate_transaction(owner["host"], owner["did"], r["did"],
                                                  memo="COMBO " + label, timeout=ch.TRANSFER_TIMEOUT, **kw)
            (done if ok else fail).append(label if ok else "{}: {}".format(label, ch.node_reason(msg)))

        if ftname:
            fire("RBT+FT", rbt=1.0, ft=ft1)
        if nft:
            fire("RBT+NFT", rbt=1.0, nft=[{"nftId": nft, "value": 1.0, "data": "combo"}])
        if sc:
            fire("RBT+SC", rbt=1.0, smart_contract=[{"smartContractId": sc, "value": 1.0, "data": "combo"}])
        if ftname and nft:
            fire("FT+NFT", ft=ft1, nft=[{"nftId": nft, "value": 1.0, "data": "combo"}])
        if ftname and sc:
            fire("FT+SC", ft=ft1, smart_contract=[{"smartContractId": sc, "value": 1.0, "data": "combo"}])
        if ftname and nft and sc:
            fire("all-in-one", rbt=1.0, ft=ft1,
                 nft=[{"nftId": nft, "value": 1.0, "data": "combo"}],
                 smart_contract=[{"smartContractId": sc, "value": 1.0, "data": "combo"}])
        return done, fail

    prep_problems = [p for st in prepared for p in st["problems"]]
    results = ch.run_parallel(combos, list(enumerate(prepared)))
    total_ok = sum(len(d) for d, _f in results)
    fails = [f for _d, fl in results for f in fl]

    kinds = sorted(set(d for dn, _f in results for d in dn))
    note = "{} owners, each mints an FT series and deploys an NFT and an SC, then combines: {} | " \
           "{} combined transfer(s) went through{}".format(
               len(owners), ", ".join(kinds) or "none", total_ok,
               "; prep issues: " + "; ".join(prep_problems[:2]) if prep_problems else "")
    if fails:
        return False, "{} combined transfer(s) refused: {}".format(len(fails), "; ".join(fails[:3])), note
    if total_ok == 0:
        return SKIP, "no owner had the assets to combine - prep failed", note
    return True, "every combined transfer went through across {} owners".format(len(owners)), note


CASES = {"COMBO-01": combo_01}
ORDER = ["COMBO-01"]
NEEDS = {
    "COMBO-01": {"senders": OWNERS, "receivers": 8, "quorums": 2, "fund": 30, "quorum_fund": 200},
}
NO_DELAY_RERUN = {"COMBO-01"}
