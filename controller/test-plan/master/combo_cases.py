"""combo_cases.py - several asset types moving in one transaction, at scale.

The product CI runs its bundled and all-in-one transactions between two nodes.
COMBO-01 runs them from many owners at once across the fleet: every owner first
mints an FT series and deploys one NFT and one smart contract (deploys cannot
be combined - core/transaction_builder.go:516), then fires the combined
transfers in a single /tx each:

    RBT + FT          RBT + NFT (execute)     RBT + SC (execute)
    FT + NFT          FT + SC                 all-in-one (RBT + FT + NFT + SC)

A combined transfer is all or nothing: one that is accepted lands every part on
the receiver's node; one that is refused must have moved NONE of its parts -
the FT count and the NFT and contract chains are read again to prove it. Then
the one combination Rubix must refuse - an NFT deploy bundled with RBT - is
sent, and must be refused without moving or locking anything.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import case_helpers as ch
import rubix_client as rc
import steps

SKIP = "SKIP"
OWNERS = 8

PURPOSE = {
    "COMBO-01": "Whether several asset types in ONE transaction are all or nothing - every part "
                "lands, or none moves - when many owners send them at once, and whether the "
                "combination Rubix forbids is refused cleanly.",
}

STEPS = {
    "COMBO-01": [
        ("S1", "Prepare, every owner at once",
         "each owner mints an FT series (500 from 1 RBT), creates and deploys an NFT, and deploys "
         "a contract",
         "every mint and deploy accepted"),
        ("S2", "Combined transfers, every owner at once",
         "each owner sends RBT+FT, RBT+NFT, RBT+SC, FT+NFT, FT+SC and all four in one, one "
         "transaction each",
         "each accepted with every part on the receiver's node; a refused one moved none of its "
         "parts (the FT count and the NFT and contract chains unchanged)"),
        ("S3", "A combination Rubix must refuse",
         "an owner creates a new NFT and sends it - a deploy - together with 1 RBT in one "
         "transaction (core/transaction_builder.go:516)",
         "refused with a stated reason; no RBT moved, nothing Locked, the NFT not deployed"),
    ],
}


def _rand_bytes(n=64):
    return os.urandom(n)


def _ft_count(entry, name):
    ok, result, _n = rc.get_ft_balance(entry["host"], entry["did"])
    return rc.ft_count_for(result, name) if ok else None


def _len(getter, entry, token_id):
    ok, chain, _n = getter(entry["host"], token_id)
    return len(chain or []) if ok else None


def combo_01(ctx, ci):
    run = steps.CaseRun(ctx, "COMBO-01", STEPS["COMBO-01"])
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 1:
        return SKIP, "needs owners and at least one receiver", ""
    ch.ensure_quorums(ctx)      # re-assert the unit's quorum setup in case a node restarted
    quorums = list(ctx.quorum_hosts or [])
    stamp = int(time.time())
    lock = threading.Lock()
    prepared = []

    def prepare(st):
        def op(*a, **k):
            with lock:
                st.op(*a, **k)

        def one(owner):
            out = {"owner": owner, "ft": None, "nft": None, "sc": None}
            name = "combo-ft-{}-{}".format(stamp, owner["did"][-6:])
            t0 = time.time()
            ok, msg, _r = rc.mint_ft(owner["host"], owner["did"], name, 500, 1, timeout=ch.TRANSFER_TIMEOUT)
            op(ok, msg, round(time.time() - t0, 2))
            if ok and rc.wait_for_ft_count(owner["host"], owner["did"], name, 500, attempts=40, delay=2)[0]:
                out["ft"] = name
            ok, msg, nft_id = rc.create_nft(owner["host"], owner["did"], _rand_bytes(), _rand_bytes())
            op(ok and isinstance(nft_id, str), msg, None)
            if ok and isinstance(nft_id, str):
                t0 = time.time()
                ok, msg, res = rc.nft_transaction(owner["host"], owner["did"], owner["did"], nft_id,
                                                  data="COMBO deploy", timeout=ch.TRANSFER_TIMEOUT)
                op(ok, msg, round(time.time() - t0, 2),
                   res.get("transactionID") if isinstance(res, dict) else None, to=owner["did"])
                if ok:
                    out["nft"] = nft_id
            sc_id, err = ch._sc_new_contract(ctx, owner)
            op(bool(sc_id), "SC generate: {}".format(err), None)
            if sc_id:
                t0 = time.time()
                ok, msg, res = rc.sc_transaction(owner["host"], owner["did"], sc_id, data="COMBO deploy",
                                                 timeout=ch.TRANSFER_TIMEOUT)
                op(ok, msg, round(time.time() - t0, 2),
                   res.get("transactionID") if isinstance(res, dict) else None)
                if ok:
                    out["sc"] = sc_id
            return out
        prepared.extend(ch.run_parallel(one, owners))
    run.step("S1", prepare, nodes=owners + quorums, must_accept=True)

    def combos(st):
        def one(i_state):
            i, p = i_state
            owner, r = p["owner"], recv[i % len(recv)]
            ft1 = [{"ftName": p["ft"], "numberOfFts": 10, "creatorDID": owner["did"]}] if p["ft"] else None
            nft1 = [{"nftId": p["nft"], "value": 1.0, "data": "combo"}] if p["nft"] else None
            sc1 = [{"smartContractId": p["sc"], "value": 1.0, "data": "combo"}] if p["sc"] else None
            plan = []
            if ft1:
                plan.append(("RBT+FT", dict(rbt=1.0, ft=ft1)))
            if nft1:
                plan.append(("RBT+NFT", dict(rbt=1.0, nft=nft1)))
            if sc1:
                plan.append(("RBT+SC", dict(rbt=1.0, smart_contract=sc1)))
            if ft1 and nft1:
                plan.append(("FT+NFT", dict(ft=ft1, nft=nft1)))
            if ft1 and sc1:
                plan.append(("FT+SC", dict(ft=ft1, smart_contract=sc1)))
            if ft1 and nft1 and sc1:
                plan.append(("all-in-one", dict(rbt=1.0, ft=ft1, nft=nft1, smart_contract=sc1)))
            out = []
            for label, kw in plan:
                parts = {"ft": _ft_count(owner, p["ft"]) if "ft" in kw else None,
                         "nft": _len(rc.get_nft_chain, owner, p["nft"]) if "nft" in kw else None,
                         "sc": _len(rc.get_sc_chain, owner, p["sc"]) if "smart_contract" in kw else None}
                t0 = time.time()
                ok, msg, res = rc.initiate_transaction(owner["host"], owner["did"], r["did"],
                                                       memo="COMBO " + label, timeout=ch.TRANSFER_TIMEOUT, **kw)
                out.append((label, ok, msg, round(time.time() - t0, 2),
                            res.get("transactionID") if isinstance(res, dict) else None, r, owner, p, parts))
            return out

        for rows in ch.run_parallel(one, list(enumerate(prepared))):
            for label, ok, msg, secs, txid, r, owner, p, parts in rows:
                st.op(ok, msg, secs, txid, to=r["did"])
                if ok:
                    continue
                time.sleep(2)
                moved = []
                if parts["ft"] is not None and _ft_count(owner, p["ft"]) != parts["ft"]:
                    moved.append("its FTs")
                if parts["nft"] is not None and _len(rc.get_nft_chain, owner, p["nft"]) != parts["nft"]:
                    moved.append("its NFT chain")
                if parts["sc"] is not None and _len(rc.get_sc_chain, owner, p["sc"]) != parts["sc"]:
                    moved.append("its contract chain")
                st.check(not moved, "{} was refused but {} moved anyway - not all or nothing".format(
                    label, " and ".join(moved)))
    run.step("S2", combos, nodes=owners + list(recv[:len(owners)]) + quorums, must_accept=True,
             require=[(any(p["ft"] or p["nft"] or p["sc"] for p in prepared),
                       "no owner had the assets to combine")])

    def forbidden(st):
        owner, r = owners[0], recv[0]
        ok, msg, nft_id = rc.create_nft(owner["host"], owner["did"], _rand_bytes(), _rand_bytes())
        if not ok or not isinstance(nft_id, str):
            st.skip("could not create the NFT to bundle: {}".format(ch.node_reason(msg)))
            return
        ch.send(st, owner, r, rbt=1.0, nft=[{"nftId": nft_id, "value": 1.0, "data": "deploy"}],
                memo="COMBO forbidden", set_aside=())
        time.sleep(ch.SETTLE)
        st.check(not _len(rc.get_nft_chain, owner, nft_id), "the NFT was deployed by a refused "
                                                            "combined transaction")
    run.step("S3", forbidden, nodes=[owners[0], recv[0]] + quorums, expect_refusal=True, budget=30)
    return run.verdict(passed="every combined transfer landed whole across {} owners".format(len(owners)))


CASES = {"COMBO-01": combo_01}
ORDER = ["COMBO-01"]
NEEDS = {
    "COMBO-01": {"senders": OWNERS, "receivers": 8, "quorums": 2, "fund": 30, "quorum_fund": 200},
}
NO_DELAY_RERUN = {"COMBO-01"}
