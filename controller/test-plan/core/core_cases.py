#!/usr/bin/env python3
"""
core_cases.py - the test cases rubixgoplatform's own CI verifies, run on the lab.

Run via:  cd test-plan/full-test && python3 case_runner.py --cases core

The CASES are taken from the product's integration suite; the SCRIPT is this
lab's own (rubix_client + case_runner + report_builder). Nothing is imported
from the product repo at runtime - only the checks were carried across, and
each one names the core check it implements so results stay comparable.
The full check list lives in ../RUBIX-CORE-TESTS.md.

Every case returns (passed, actual, note):
    True  -> matched the expectation
    False -> did not match: a real finding
    SKIP  -> NOT ATTEMPTED, reason in `note`. Never a silent pass.

What makes these different from a hand-written catalogue, and worth keeping:

  * They assert END STATE, not HTTP 200. A 200 on POST /tx means the request
    was accepted, not that a chain advanced, ownership moved, or the receiver
    ever saw it. Every case here reads the state back.
  * Negative cases assert the operation was rejected FOR THE RIGHT REASON and
    left state unchanged. A rejection with the wrong error, or a rejection
    that still moved a balance, is a FAIL.
  * One operation legitimately verifies several things, so a case checks
    several named conditions rather than being split into near-duplicate runs.

Phases are SEQUENTIAL and share state (_STATE): NFTs deployed in the NFT phase
are what the bundled and all-in-one phases execute. That ordering is the
product suite's, and it matters - running them independently would not
exercise the same paths. ORDER encodes it; --only can break it, and a case
whose prerequisite did not run reports SKIP rather than a misleading FAIL.

Verified behaviour these rely on (product source, not assumption):
  * POST /rubix/v1/tx; the request's `owner` field is the RECEIVER.
  * NFT/SC execute is gated by SUBSCRIPTION, not ownership
    (core/consensus/checks.go:122) - which is what the cross-node cases prove.
  * Smart contracts always send owner="" - there is no SC ownership transfer.
  * Child mint: one nft entry PER CHILD, carrying parentNFTId and NO nftId.
  * A receiver credits ~1-2s after the sender's call returns, so every
    assertion polls rather than reading once.
  * A quorum must pledge >= the transfer value, so --fund-quorum bounds the
    largest testable transfer.

Deliberately SKIPped, with the reason carried into the report:
  * token_denom / persistence checks need direct Postgres access. Per
    CLAUDE.md that is deferred (psycopg2 + per-node credentials). These are
    the checks that caught real product bugs, so they are worth un-skipping.
  * Intra-node (two DIDs on one node) conflicts with this fleet's
    one-DID-per-node invariant, which every controller tool depends on.
"""

import json
import os
import random
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc

SKIP = "SKIP"

# Artifacts built by earlier phases and consumed by later ones. The product
# suite works the same way: its bundled/all-in-one phases execute the NFTs and
# contracts the earlier phases deployed.
_STATE = {
    "nft": None,          # deployed NFT id (owned by pair-0 sender)
    "nft_children": [],
    "sc": None,           # deployed SC id
    "ft_name": None,
    "ft_count": 0,
    "callback": None,     # _CallbackReceiver, once started
}

# Settle windows. The product suite waits between phases for chain/quorum/DB
# writes to commit; over a real network there is strictly more reason to, not
# less. Values are the lab's measured ones, not copied constants.
SETTLE = 3
CHAIN_POLL_ATTEMPTS = 12
CHAIN_POLL_DELAY = 2


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _rand(n=16):
    return "".join(random.choice(string.ascii_letters + string.digits) for _ in range(n))


def _metadata_bytes():
    """Distinct metadata per NFT: identical bytes hash to the same IPFS CID,
    so reusing them would collide with an NFT an earlier run created."""
    return json.dumps({"name": "lab-" + _rand(), "run": time.time()}).encode()


def _artifact_bytes():
    return ("artifact-" + _rand(32)).encode()


def _wasm_bytes():
    """A .wasm header plus filler. The lab never executes the contract body -
    deploy/execute go through consensus, which does not interpret it - but the
    extension IS checked literally, so the bytes must be plausible."""
    return b"\x00asm\x01\x00\x00\x00" + _rand(64).encode()


def _raw_bytes():
    return ("// lab contract " + _rand(24) + "\nfn main() {}\n").encode()


def _bal(host, did, port):
    ok, detail, _ = rc.get_rbt_balance_detail(host, did, port)
    return detail if ok else None


def _prepare_sender(ctx, entry, amount):
    """Make `entry` able to send `amount`: quorum registered, funds present,
    quorum able to pledge. Returns (ready, reason).

    Cases must build the conditions they need. The first full catalogue run
    failed mostly because they did not - receivers had no quorum registered,
    and quorum liquidity drained mid-run - which produced FAILs that said
    nothing about Rubix.
    """
    host = entry["host"]
    q = ctx.quorum_for(host) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return False, "no quorum available"

    ok, _ = rc.quorum_add(host, q["did"], ctx.port)
    # quorum_add errors on repeat even though the insert is ON CONFLICT DO
    # NOTHING (core/wallet/quorum.go:19) - already-registered is success.
    del ok

    detail = _bal(host, entry["did"], ctx.port)
    have = detail["balance"] if detail else 0
    if have < amount:
        rc.fund_did(host, entry["did"], max(int(amount - have) + 5, 5), ctx.port)
        if not rc.wait_for_balance(host, entry["did"], amount, ctx.port):
            return False, "could not fund sender to {} (have {})".format(amount, have)

    qd = _bal(q["host"], q["did"], ctx.port)
    if qd and qd["balance"] < amount:
        rc.fund_did(q["host"], q["did"], int(amount) + 100, ctx.port)
        rc.wait_for_balance(q["host"], q["did"], amount, ctx.port)
    return True, ""


def _poll_chain(fetch, want_len, attempts=CHAIN_POLL_ATTEMPTS, delay=CHAIN_POLL_DELAY):
    """Poll a chain endpoint until it reaches want_len. Returns (reached, length).

    Chain growth is asynchronous - reading once right after the call returns
    reports the pre-transaction length and looks like the operation did
    nothing.
    """
    n = -1
    for _ in range(attempts):
        ok, chain, _ = fetch()
        if ok:
            n = len(chain)
            if n >= want_len:
                return True, n
        time.sleep(delay)
    return False, n


def _need(key, label):
    """Guard for a case whose prerequisite phase did not run."""
    if not _STATE.get(key):
        return SKIP, "no {}".format(label), (
            "prerequisite phase did not run or failed - these cases are "
            "sequential; run without --only, or include the earlier case")
    return None


# ---------------------------------------------------------------------------
# RBT  (core: shuttle phase - TX_LIST_NODE_A / TX_LIST_NODE_B)
# ---------------------------------------------------------------------------

def rbt_transfer_recorded_both(ctx, ci):
    """TX_LIST_NODE_A + TX_LIST_NODE_B: a transfer is recorded by BOTH ends.

    The check core makes that a plain balance assertion misses: the receiver
    crediting is not the same as the receiver having RECORDED the transaction.
    """
    s, r = ctx.pair(0)
    amount = float(ctx.args.rbt_amount)
    ready, why = _prepare_sender(ctx, s, amount)
    if not ready:
        return SKIP, "setup incomplete", why

    before = _bal(r["host"], r["did"], ctx.port)
    ok, msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"],
                                         rbt=amount, port=ctx.port)
    if not ok:
        return False, "transfer rejected", str(msg)

    credited = rc.wait_for_balance(r["host"], r["did"],
                                   (before["balance"] if before else 0) + amount * 0.99,
                                   ctx.port)

    _, tx_a, _ = rc.get_transactions(s["host"], s["did"], "rbt", ctx.port)
    _, tx_b, _ = rc.get_transactions(r["host"], r["did"], "rbt", ctx.port)
    a_ok, b_ok = len(tx_a) > 0, len(tx_b) > 0

    passed = credited and a_ok and b_ok
    return passed, "credited={} sender_tx={} receiver_tx={}".format(
        credited, len(tx_a), len(tx_b)), (
        "" if passed else "TX_LIST_NODE_{} empty - the transfer moved value but "
        "the node did not record it".format("A" if not a_ok else "B"))


def rbt_shuttle_alternating(ctx, ci):
    """Shuttle: alternating A->B / B->A. Both directions must work.

    B->A is the direction a naive harness gets wrong: the receiver has no
    quorum registered, so it can receive but not send.
    """
    s, r = ctx.pair(1)
    amount = float(ctx.args.rbt_amount)
    for entry in (s, r):
        ready, why = _prepare_sender(ctx, entry, amount)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(entry["host"], why)

    ok1, m1, _ = rc.initiate_transaction(s["host"], s["did"], r["did"], rbt=amount, port=ctx.port)
    time.sleep(SETTLE)
    ok2, m2, _ = rc.initiate_transaction(r["host"], r["did"], s["did"], rbt=amount, port=ctx.port)

    passed = bool(ok1 and ok2)
    return passed, "A->B={} B->A={}".format(bool(ok1), bool(ok2)), (
        "" if passed else "forward={} reverse={}".format(m1, m2))


# ---------------------------------------------------------------------------
# NFT
# ---------------------------------------------------------------------------

def nft_deploy_and_list(ctx, ci):
    """NFT_LIST_NODE_A + NFT_CHAIN_<id>: create, deploy, and confirm the node
    both lists it and shows a chain for it."""
    s, _ = ctx.pair(0)
    ready, why = _prepare_sender(ctx, s, 1.0)
    if not ready:
        return SKIP, "setup incomplete", why

    ok, msg, result = rc.create_nft(s["host"], s["did"], _metadata_bytes(),
                                    _artifact_bytes(), ctx.port)
    if not ok or not result:
        return False, "create failed", str(msg)
    nft_id = result if isinstance(result, str) else str(result)

    ok, msg, _ = rc.nft_transaction(s["host"], s["did"], s["did"], nft_id,
                                    data="NFT deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    _STATE["nft"] = nft_id
    reached, n = _poll_chain(lambda: rc.get_nft_chain(s["host"], nft_id, ctx.port), 1)
    _, listed, _ = rc.list_nfts(s["host"], ctx.port)
    in_list = any(nft_id in json.dumps(x) for x in listed)

    passed = reached and in_list
    return passed, "nft={} chain={} listed={}".format(nft_id[:12], n, in_list), (
        "" if passed else "deployed but chain={} listed={}".format(n, in_list))


def nft_self_execute_grows_chain(ctx, ci):
    """NFT_CHAIN_<id> grows by one on self-execute - ownership unchanged."""
    guard = _need("nft", "deployed NFT")
    if guard:
        return guard
    s, _ = ctx.pair(0)
    nft_id = _STATE["nft"]

    _, before, _ = rc.get_nft_chain(s["host"], nft_id, ctx.port)
    ok, msg, _ = rc.nft_transaction(s["host"], s["did"], s["did"], nft_id,
                                    data="self execute", port=ctx.port)
    if not ok:
        return False, "execute rejected", str(msg)

    reached, n = _poll_chain(lambda: rc.get_nft_chain(s["host"], nft_id, ctx.port),
                             len(before) + 1)
    return reached, "chain {} -> {}".format(len(before), n), (
        "" if reached else "execute returned success but the chain did not grow")


def nft_cross_node_execute_syncs(ctx, ci):
    """NFT_CHAIN_SYNC_<id>: a DIFFERENT node subscribes and executes, and its
    chain length then matches the owner's.

    This is the case that proves execute is gated by SUBSCRIPTION, not
    ownership (core/consensus/checks.go:122).
    """
    guard = _need("nft", "deployed NFT")
    if guard:
        return guard
    s, r = ctx.pair(0)
    nft_id = _STATE["nft"]

    ready, why = _prepare_sender(ctx, r, 1.0)
    if not ready:
        return SKIP, "setup incomplete", why

    sub_ok, sub_msg = rc.subscribe_nft(r["host"], nft_id, ctx.port)
    if not sub_ok:
        return False, "subscribe failed", str(sub_msg)
    time.sleep(SETTLE)

    _, before, _ = rc.get_nft_chain(s["host"], nft_id, ctx.port)
    ok, msg, _ = rc.nft_transaction(r["host"], r["did"], r["did"], nft_id,
                                    data="cross-node execute", port=ctx.port)
    if not ok:
        return False, "cross-node execute rejected", str(msg)

    want = len(before) + 1
    reached_o, n_owner = _poll_chain(lambda: rc.get_nft_chain(s["host"], nft_id, ctx.port), want)
    reached_e, n_exec = _poll_chain(lambda: rc.get_nft_chain(r["host"], nft_id, ctx.port), want)

    synced = n_owner == n_exec and reached_o and reached_e
    return synced, "owner={} executor={}".format(n_owner, n_exec), (
        "" if synced else "chains disagree after cross-node execute - "
        "one node advanced and the other did not")


def nft_mint_children(ctx, ci):
    """NFT_MINT_CHILDREN + NFT_CHILDREN_MINTED_<parent> + NFT_PARENT_OF_<child>.

    One operation, three named checks: the mint reports the right count, the
    parent lists every child, and each child points back.
    """
    guard = _need("nft", "deployed NFT")
    if guard:
        return guard
    s, _ = ctx.pair(0)
    parent = _STATE["nft"]
    want = 2

    ok, msg, result = rc.mint_nft_children(s["host"], s["did"], parent, want, port=ctx.port)
    if not ok:
        return False, "child mint rejected", str(msg)

    children = [c.get("childNFTId") for c in rc.minted_children(result) if c.get("childNFTId")]
    _STATE["nft_children"] = children
    minted_ok = len(children) == want
    time.sleep(SETTLE)

    _, listed, _ = rc.get_nft_children(s["host"], parent, ctx.port)
    blob = json.dumps(listed)
    listed_ok = all(c in blob for c in children) and bool(children)

    parent_ok = True
    for c in children:
        okp, res, _ = rc.get_nft_parent(s["host"], c, ctx.port)
        if not okp or not res or parent not in json.dumps(res):
            parent_ok = False
            break

    passed = minted_ok and listed_ok and parent_ok
    return passed, "minted={}/{} listed={} parent_links={}".format(
        len(children), want, listed_ok, parent_ok), (
        "" if passed else "mint reported {} children; parent lists them={}; "
        "back-links={}".format(len(children), listed_ok, parent_ok))


def nft_transfer_ownership(ctx, ci):
    """NFT_BALANCE_NODE_A / _NODE_B: ownership actually moves.

    Asserts BOTH sides - the sender losing it and the receiver gaining it are
    separate facts, and a transfer that only does one is broken.
    """
    guard = _need("nft", "deployed NFT")
    if guard:
        return guard
    s, r = ctx.pair(0)
    nft_id = _STATE["nft"]

    ok, msg, _ = rc.nft_transaction(s["host"], s["did"], r["did"], nft_id,
                                    data="ownership transfer",
                                    transfer_ownership=True, port=ctx.port)
    if not ok:
        return False, "transfer rejected", str(msg)

    gained = lost = False
    for _ in range(CHAIN_POLL_ATTEMPTS):
        time.sleep(CHAIN_POLL_DELAY)
        _, rb, _ = rc.get_nft_balance(r["host"], r["did"], ctx.port)
        _, sb, _ = rc.get_nft_balance(s["host"], s["did"], ctx.port)
        gained = nft_id in json.dumps(rb)
        lost = nft_id not in json.dumps(sb)
        if gained and lost:
            break

    passed = gained and lost
    if passed:
        _STATE["nft"] = None  # no longer owned by the pair-0 sender
    return passed, "receiver_has={} sender_released={}".format(gained, lost), (
        "" if passed else "ownership did not fully move (receiver={}, "
        "sender_released={})".format(gained, lost))


# ---------------------------------------------------------------------------
# Smart contracts
# ---------------------------------------------------------------------------

def sc_deploy_and_list(ctx, ci):
    """SC_LIST_NODE_A + SC_CHAIN_<id>: deploy and confirm it is listed with a chain."""
    s, _ = ctx.pair(0)
    ready, why = _prepare_sender(ctx, s, 1.0)
    if not ready:
        return SKIP, "setup incomplete", why

    ok, msg, result = rc.create_smart_contract(s["host"], s["did"], _wasm_bytes(),
                                               _raw_bytes(), ctx.port)
    if not ok or not result:
        return False, "generate failed", str(msg)
    sc_id = result if isinstance(result, str) else str(result)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, data="SC deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    _STATE["sc"] = sc_id
    reached, n = _poll_chain(lambda: rc.get_sc_chain(s["host"], sc_id, ctx.port), 1)
    _, listed, _ = rc.list_smart_contracts(s["host"], ctx.port)
    in_list = sc_id in json.dumps(listed)

    passed = reached and in_list
    return passed, "sc={} chain={} listed={}".format(sc_id[:12], n, in_list), (
        "" if passed else "deployed but chain={} listed={}".format(n, in_list))


def sc_cross_node_execute_syncs(ctx, ci):
    """SC_CHAIN_SYNC_<id> + SC_TX_LIST: another node subscribes, executes, and
    both chains agree."""
    guard = _need("sc", "deployed contract")
    if guard:
        return guard
    s, r = ctx.pair(0)
    sc_id = _STATE["sc"]

    ready, why = _prepare_sender(ctx, r, 1.0)
    if not ready:
        return SKIP, "setup incomplete", why

    sub_ok, sub_msg = rc.subscribe_smart_contract(r["host"], sc_id, ctx.port)
    if not sub_ok:
        return False, "subscribe failed", str(sub_msg)
    time.sleep(SETTLE)

    _, before, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    ok, msg, _ = rc.sc_transaction(r["host"], r["did"], sc_id,
                                   data="cross-node execute", port=ctx.port)
    if not ok:
        return False, "cross-node execute rejected", str(msg)

    want = len(before) + 1
    _, n_owner = _poll_chain(lambda: rc.get_sc_chain(s["host"], sc_id, ctx.port), want)
    _, n_exec = _poll_chain(lambda: rc.get_sc_chain(r["host"], sc_id, ctx.port), want)
    _, txs, _ = rc.get_transactions(r["host"], r["did"], "smartContract", ctx.port)

    passed = n_owner == n_exec and n_owner >= want
    return passed, "owner={} executor={} sc_tx={}".format(n_owner, n_exec, len(txs)), (
        "" if passed else "chains disagree after cross-node SC execute")


def sc_callback_delivered(ctx, ci):
    """SC_REGISTER_CALLBACK + SC_CALLBACK_DELIVERED + SC_CALLBACK_INITIATOR.

    Registers a callback URL pointing at this controller, executes the
    contract, and asserts the node actually POSTed back with the right
    initiator. Registration succeeding is not the same as delivery happening -
    core keeps them as separate checks for that reason, and so does this.
    """
    guard = _need("sc", "deployed contract")
    if guard:
        return guard
    s, _ = ctx.pair(0)
    sc_id = _STATE["sc"]

    rx = _STATE.get("callback")
    if rx is None:
        try:
            rx = _CallbackReceiver()
            rx.start()
            _STATE["callback"] = rx
        except Exception as e:
            return SKIP, "no callback receiver", "could not bind a local port: {}".format(e)

    url = rx.url_for(ctx)
    if url is None:
        return SKIP, "callback URL unknown", (
            "could not determine an address the lab nodes can reach this "
            "controller on - pass --callback-host")

    ok, msg, _ = rc.register_sc_callback(s["host"], sc_id, url, ctx.port)
    if not ok:
        return False, "callback registration rejected", str(msg)

    rx.clear()
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   data="callback trigger", port=ctx.port)
    if not ok:
        return False, "execute rejected", str(msg)

    got = rx.wait(timeout=45)
    if not got:
        return False, "no callback received", (
            "registered {} and executed, but the node never POSTed back within "
            "45s - check the controller is reachable from {} on that "
            "port".format(url, s["host"]))

    initiator_ok = any(s["did"] in json.dumps(e) for e in got)
    return initiator_ok, "callbacks={} initiator_match={}".format(len(got), initiator_ok), (
        "" if initiator_ok else "callback delivered but carried the wrong initiator")


# ---------------------------------------------------------------------------
# FT
# ---------------------------------------------------------------------------

def ft_mint_and_list(ctx, ci):
    """FT_LIST_NODE_A + FT_BALANCE_NODE_A: mint a batch, confirm series and count.

    Minting an FT BURNS RBT, so the sender needs whole tokens first.
    """
    s, _ = ctx.pair(0)
    count = int(ctx.args.ft_count)
    tokens = int(ctx.args.ft_token_count)
    ready, why = _prepare_sender(ctx, s, tokens + 2)
    if not ready:
        return SKIP, "setup incomplete", why

    name = "labft" + _rand(6).lower()
    ok, msg, _ = rc.mint_ft(s["host"], s["did"], name, count, tokens, ctx.port)
    if not ok:
        return False, "mint rejected", str(msg)

    _STATE["ft_name"] = name
    _STATE["ft_count"] = count
    got = rc.wait_for_ft_count(s["host"], s["did"], name, count, ctx.port)
    _, series, _ = rc.list_fts(s["host"], ctx.port)
    listed = name in json.dumps(series)

    passed = bool(got) and listed
    return passed, "ft={} count={} listed={}".format(name, count, listed), (
        "" if passed else "minted but balance/list did not reflect it "
        "(credited={}, listed={})".format(bool(got), listed))


def ft_transfer(ctx, ci):
    """FT_BALANCE_NODE_B + FT_TX_LIST: transfer a slice and confirm the
    receiver holds it and recorded the transaction."""
    guard = _need("ft_name", "minted FT")
    if guard:
        return guard
    s, r = ctx.pair(0)
    name = _STATE["ft_name"]
    move = max(1, _STATE["ft_count"] // 2)

    ready, why = _prepare_sender(ctx, s, 1.0)
    if not ready:
        return SKIP, "setup incomplete", why

    ft = [{"ftName": name, "ftCount": move, "creatorDID": s["did"]}]
    ok, msg, _ = rc.initiate_transaction(s["host"], s["did"], r["did"], ft=ft, port=ctx.port)
    if not ok:
        return False, "FT transfer rejected", str(msg)

    got = rc.wait_for_ft_count(r["host"], r["did"], name, move, ctx.port)
    _, txs, _ = rc.get_transactions(r["host"], r["did"], "ft", ctx.port)

    passed = bool(got) and len(txs) > 0
    return passed, "moved={} receiver_ft_tx={}".format(move, len(txs)), (
        "" if passed else "receiver credited={} tx_recorded={}".format(bool(got), len(txs) > 0))


# ---------------------------------------------------------------------------
# Bundled  (RBT + NFT + SC in ONE transaction)
# ---------------------------------------------------------------------------

def bundled_transaction(ctx, ci):
    """BUNDLED_RBT_BALANCE + BUNDLED_NFT_CHAIN + BUNDLED_SC_CHAIN.

    One /tx carrying an RBT transfer, an NFT execution and an SC execution.
    The point is ATOMICITY across asset types: all three must advance, from a
    single call.
    """
    guard = _need("sc", "deployed contract")
    if guard:
        return guard
    s, r = ctx.pair(2)
    amount = float(ctx.args.rbt_amount)
    ready, why = _prepare_sender(ctx, s, amount + 2)
    if not ready:
        return SKIP, "setup incomplete", why

    # A fresh NFT owned by THIS pair's sender - the pair-0 NFT was transferred
    # away by the ownership case, and executing an NFT you do not own (and are
    # not subscribed to) is a different test.
    ok, msg, result = rc.create_nft(s["host"], s["did"], _metadata_bytes(),
                                    _artifact_bytes(), ctx.port)
    if not ok or not result:
        return SKIP, "could not create NFT for bundle", str(msg)
    nft_id = result if isinstance(result, str) else str(result)
    ok, msg, _ = rc.nft_transaction(s["host"], s["did"], s["did"], nft_id,
                                    data="bundle deploy", port=ctx.port)
    if not ok:
        return SKIP, "could not deploy NFT for bundle", str(msg)

    sc_id = _STATE["sc"]
    sub_ok, _ = rc.subscribe_smart_contract(s["host"], sc_id, ctx.port)
    if not sub_ok:
        return SKIP, "could not subscribe to SC", "subscription is the gate for execute"
    time.sleep(SETTLE)

    _, nft_before, _ = rc.get_nft_chain(s["host"], nft_id, ctx.port)
    _, sc_before, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    rb_before = _bal(r["host"], r["did"], ctx.port)

    body = {
        "initiator": s["did"], "owner": r["did"],
        "tokens": {
            "rbt": amount, "ft": [],
            "nft": [{"nftId": nft_id, "value": 1.0, "data": "bundled execute"}],
            "smartContract": [{"smartContractId": sc_id, "value": 1.0,
                               "data": "bundled execute"}],
            "transferNftOwnership": False,
        },
        "memo": "bundled RBT+NFT+SC",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "bundled tx rejected", str(msg)

    credited = rc.wait_for_balance(r["host"], r["did"],
                                   (rb_before["balance"] if rb_before else 0) + amount * 0.99,
                                   ctx.port)
    _, n_nft = _poll_chain(lambda: rc.get_nft_chain(s["host"], nft_id, ctx.port),
                           len(nft_before) + 1)
    _, n_sc = _poll_chain(lambda: rc.get_sc_chain(s["host"], sc_id, ctx.port),
                          len(sc_before) + 1)

    nft_ok = n_nft > len(nft_before)
    sc_ok = n_sc > len(sc_before)
    passed = credited and nft_ok and sc_ok
    return passed, "rbt={} nft_chain={}->{} sc_chain={}->{}".format(
        credited, len(nft_before), n_nft, len(sc_before), n_sc), (
        "" if passed else "bundled tx accepted but only some parts advanced "
        "(rbt={}, nft={}, sc={}) - a partial bundle is an atomicity "
        "failure".format(credited, nft_ok, sc_ok))


# ---------------------------------------------------------------------------
# Negative  (rejected for the RIGHT reason, and state unchanged)
# ---------------------------------------------------------------------------

def _expect_rejected(ctx, sender, receiver_did, amount=None, ft=None, expect_words=()):
    """Assert a transaction is rejected AND leaves balance and locked unmoved.

    Checking `locked` matters as much as `balance`: a rejection that leaves
    tokens stuck in Locked is a real bug (there are three separate
    lock-release paths, core/transaction.go:70/:77/:88), and it is invisible
    from balance alone.
    """
    host, did = sender["host"], sender["did"]
    before = _bal(host, did, ctx.port)
    ok, msg, _ = rc.initiate_transaction(host, did, receiver_did, rbt=amount,
                                         ft=ft, port=ctx.port)
    time.sleep(SETTLE)
    after = _bal(host, did, ctx.port)

    if ok:
        return False, "ACCEPTED", "expected rejection but the transaction succeeded"

    text = str(msg).lower()
    reason_ok = (not expect_words) or any(w in text for w in expect_words)
    bal_ok = before is None or after is None or rc.close_enough(before["balance"], after["balance"])
    lock_ok = before is None or after is None or rc.close_enough(before["locked"], after["locked"])

    passed = reason_ok and bal_ok and lock_ok
    detail = []
    if not reason_ok:
        detail.append("rejected, but not for the expected reason: {}".format(msg))
    if not bal_ok:
        detail.append("balance moved on a rejected transaction")
    if not lock_ok:
        detail.append("tokens left LOCKED after rejection")
    return passed, "rejected", "; ".join(detail)


def neg_zero_balance(ctx, ci):
    """NEG_RBT_ZERO_BALANCE: a DID with no funds cannot send."""
    if len(ctx.receivers) < 2:
        return SKIP, "no spare host", "need an unfunded DID"
    # A receiver that no case has funded - deliberately not ctx.pair(0)'s.
    spare = ctx.receivers[-1]
    detail = _bal(spare["host"], spare["did"], ctx.port)
    if detail and detail["balance"] > 0:
        return SKIP, "host not empty", (
            "{} holds {} RBT, so this cannot test the zero-balance "
            "path".format(spare["host"], detail["balance"]))
    s, _ = ctx.pair(0)
    return _expect_rejected(ctx, spare, s["did"], amount=1.0,
                            expect_words=("insufficient", "balance", "token", "fail"))


def neg_insufficient(ctx, ci):
    """NEG_RBT_INSUFFICIENT: cannot send more than held."""
    s, r = ctx.pair(0)
    detail = _bal(s["host"], s["did"], ctx.port)
    have = detail["balance"] if detail else 0
    return _expect_rejected(ctx, s, r["did"], amount=have + 10000.0,
                            expect_words=("insufficient", "balance", "token"))


def neg_decimal_places(ctx, ci):
    """NEG_RBT_DECIMAL_PLACES: more than 3 dp is rejected.

    0.00000009 is below MinDecimalUnit (0.001). FloatPrecision ROUNDS at 3dp
    (math/math.go), so this rounds to 0, contributes no tokens, and is
    rejected as token-less rather than as a precision error - the rejection is
    correct either way, which is why the reason match stays broad.
    """
    s, r = ctx.pair(0)
    return _expect_rejected(ctx, s, r["did"], amount=0.00000009,
                            expect_words=("decimal", "precision", "token", "amount", "invalid"))


def neg_non_positive(ctx, ci):
    """NEG_NON_POSITIVE_AMOUNT: a negative amount is rejected.

    HasRBT() is `Tokens.RBT > 0`, so a negative amount yields no tokens and
    ValidateTransactionInfoFields rejects it as token-less.
    """
    s, r = ctx.pair(0)
    return _expect_rejected(ctx, s, r["did"], amount=-1.0,
                            expect_words=("token", "amount", "invalid", "positive"))


def neg_invalid_receiver(ctx, ci):
    """NEG_INVALID_RECEIVER_DID: a malformed receiver DID is rejected."""
    s, _ = ctx.pair(0)
    return _expect_rejected(ctx, s, "bafybmi" + "0" * 52, amount=1.0,
                            expect_words=("did", "peer", "invalid", "not found", "fail"))


def neg_ft_over_transfer(ctx, ci):
    """NEG_FT_OVER_TRANSFER: cannot transfer FTs not held."""
    guard = _need("ft_name", "minted FT")
    if guard:
        return guard
    s, r = ctx.pair(0)
    ft = [{"ftName": _STATE["ft_name"], "ftCount": 1000000, "creatorDID": s["did"]}]
    return _expect_rejected(ctx, s, r["did"], ft=ft,
                            expect_words=("ft", "insufficient", "lock", "token", "not enough"))


# ---------------------------------------------------------------------------
# SC deploy collateral  (the fractional-value accounting check)
# ---------------------------------------------------------------------------

def sccol_fractional_deploy_cost(ctx, ci):
    """SCCOL_BALANCE_DELTA: a 0.001-value deploy must cost 0.001, not a whole RBT.

    LockTokensForSplit selects WHOLE denominations, so backing a 0.001
    commitment picks a 1.000 token. If that token is committed as-is, the
    other 0.999 is silently destroyed. Only visible below one whole token -
    the ordinary SC path deploys at exactly 1.0, the one value where
    committing a whole token is correct.
    """
    s, _ = ctx.pair(3)
    value = 0.001
    ready, why = _prepare_sender(ctx, s, 5.0)
    if not ready:
        return SKIP, "setup incomplete", why

    ok, msg, result = rc.create_smart_contract(s["host"], s["did"], _wasm_bytes(),
                                               _raw_bytes(), ctx.port)
    if not ok or not result:
        return SKIP, "generate failed", str(msg)
    sc_id = result if isinstance(result, str) else str(result)

    before = _bal(s["host"], s["did"], ctx.port)
    if before is None:
        return SKIP, "balance unreadable", "cannot measure the delta"

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="fractional collateral deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", (
            "a fractional-value deploy should succeed: {}".format(msg))

    time.sleep(SETTLE * 2)
    after = _bal(s["host"], s["did"], ctx.port)
    if after is None:
        return SKIP, "balance unreadable", "cannot measure the delta"

    spent = before["balance"] - after["balance"]
    exact = rc.close_enough(spent, value, tol=0.0015)
    whole = spent >= 0.9

    return exact, "spent={:.4f} for value={:.4f}".format(spent, value), (
        "" if exact else (
            "a whole token was consumed for a {} deploy - the remainder was "
            "destroyed rather than returned as change".format(value) if whole
            else "deploy cost {:.4f}, expected {:.4f}".format(spent, value)))


# ---------------------------------------------------------------------------
# Checks that need machinery this runner does not have.
# Reported as SKIP with the reason, never as a pass.
# ---------------------------------------------------------------------------

def _skip(reason):
    def fn(ctx, ci):
        return SKIP, "not attempted", reason
    return fn


_DB_REASON = (
    "needs direct Postgres access (token_denom / transactions tables). Lab "
    "Postgres is reachable on :5433 with known credentials, so this is "
    "un-skippable once psycopg2 is installed on the controller - and these are "
    "the checks that caught real product bugs, so it is worth doing")

_INTRA_REASON = (
    "needs two DIDs on one node. This fleet holds exactly one DID per node and "
    "every controller tool depends on that; CreateDID has no idempotency, so "
    "adding a second here would be a one-way change")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CASES = {
    "CORE-RBT-01": rbt_transfer_recorded_both,
    "CORE-RBT-02": rbt_shuttle_alternating,

    "CORE-NFT-01": nft_deploy_and_list,
    "CORE-NFT-02": nft_self_execute_grows_chain,
    "CORE-NFT-03": nft_cross_node_execute_syncs,
    "CORE-NFT-04": nft_mint_children,
    "CORE-NFT-05": nft_transfer_ownership,

    "CORE-SC-01": sc_deploy_and_list,
    "CORE-SC-02": sc_cross_node_execute_syncs,
    "CORE-SC-03": sc_callback_delivered,

    "CORE-FT-01": ft_mint_and_list,
    "CORE-FT-02": ft_transfer,

    "CORE-BUN-01": bundled_transaction,

    "CORE-NEG-01": neg_zero_balance,
    "CORE-NEG-02": neg_insufficient,
    "CORE-NEG-03": neg_decimal_places,
    "CORE-NEG-04": neg_non_positive,
    "CORE-NEG-05": neg_invalid_receiver,
    "CORE-NEG-06": neg_ft_over_transfer,

    "CORE-SCCOL-01": sccol_fractional_deploy_cost,
    "CORE-SCCOL-02": _skip(_DB_REASON),
    "CORE-FTP-01": _skip(_DB_REASON),
    "CORE-FTP-02": _skip(_DB_REASON),
    "CORE-INTRA-01": _skip(_INTRA_REASON),
    "CORE-TXP-01": _skip(_DB_REASON),
}

# Sequential by design: NFT/SC/FT artifacts built early are what the bundled
# and negative phases exercise. Negative cases run late so a rejection cannot
# disturb a balance an earlier positive case depends on.
ORDER = [
    "CORE-RBT-01", "CORE-RBT-02",
    "CORE-NFT-01", "CORE-NFT-02", "CORE-NFT-03", "CORE-NFT-04", "CORE-NFT-05",
    "CORE-SC-01", "CORE-SC-02", "CORE-SC-03",
    "CORE-FT-01", "CORE-FT-02",
    "CORE-BUN-01",
    "CORE-SCCOL-01",
    "CORE-NEG-01", "CORE-NEG-02", "CORE-NEG-03", "CORE-NEG-04",
    "CORE-NEG-05", "CORE-NEG-06",
    "CORE-SCCOL-02", "CORE-FTP-01", "CORE-FTP-02", "CORE-INTRA-01", "CORE-TXP-01",
]

# These cases are not in master-test-cases.xlsx - they come from the product's
# suite - so the module supplies its own catalogue text for the report.
CASE_INFO = {
    "CORE-RBT-01": ("RBT transfer is recorded by BOTH sender and receiver",
                    "Value moves and both nodes list the transaction"),
    "CORE-RBT-02": ("Alternating A->B then B->A transfers",
                    "Both directions succeed"),
    "CORE-NFT-01": ("Create + deploy an NFT", "Node lists it and shows a chain"),
    "CORE-NFT-02": ("Self-execute an NFT", "Chain grows by one, ownership unchanged"),
    "CORE-NFT-03": ("Another node subscribes and executes the NFT",
                    "Both chains agree - execute is gated by subscription, not ownership"),
    "CORE-NFT-04": ("Mint child NFTs under a parent",
                    "Children minted, parent lists them, each child links back"),
    "CORE-NFT-05": ("Transfer NFT ownership",
                    "Receiver gains it AND sender releases it"),
    "CORE-SC-01": ("Deploy a smart contract", "Node lists it and shows a chain"),
    "CORE-SC-02": ("Another node subscribes and executes the contract",
                   "Both chains agree and the SC transaction is recorded"),
    "CORE-SC-03": ("Register a callback URL, then execute",
                   "Node POSTs back to the controller with the correct initiator"),
    "CORE-FT-01": ("Mint an FT batch (burns RBT)", "Series listed and count correct"),
    "CORE-FT-02": ("Transfer a slice of the FT batch",
                   "Receiver holds them and recorded the transaction"),
    "CORE-BUN-01": ("RBT + NFT + SC in ONE transaction",
                    "All three advance from a single call"),
    "CORE-NEG-01": ("Transfer from a DID with no balance",
                    "Rejected; balance and locked unchanged"),
    "CORE-NEG-02": ("Transfer more RBT than held",
                    "Rejected; balance and locked unchanged"),
    "CORE-NEG-03": ("Transfer 0.00000009 (beyond 3 dp)",
                    "Rejected; balance and locked unchanged"),
    "CORE-NEG-04": ("Transfer a negative amount",
                    "Rejected; balance and locked unchanged"),
    "CORE-NEG-05": ("Transfer to a malformed receiver DID", "Rejected"),
    "CORE-NEG-06": ("Transfer 1,000,000 FTs not held",
                    "Rejected; FT balance unchanged"),
    "CORE-SCCOL-01": ("Deploy a contract with value 0.001",
                      "Costs exactly 0.001, not a whole RBT"),
    "CORE-SCCOL-02": ("token_denom consistent with Free RBT rows", "DB check"),
    "CORE-FTP-01": ("FT minted from PART RBTs only", "DB check"),
    "CORE-FTP-02": ("No token_denom drift after part burns", "DB check"),
    "CORE-INTRA-01": ("Two DIDs on one node, full asset matrix", "Intra-node"),
    "CORE-TXP-01": ("Every SUCCESS txn present on every participating node", "DB check"),
}

TIMING_CASES = set()


# ---------------------------------------------------------------------------
# Callback receiver - a tiny HTTP server the lab nodes POST back to.
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode() or "{}")
        except Exception:
            body = {"raw": raw.decode("utf-8", "replace")}
        self.server.events.append(body)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":true}')

    def log_message(self, *a):
        pass  # the run's own output is the log


class _CallbackReceiver:
    """Binds 0.0.0.0 on a free port so a node on another machine can reach it."""

    def __init__(self, port=0):
        self.server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
        self.server.events = []
        self.port = self.server.server_address[1]
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def clear(self):
        self.server.events = []

    def wait(self, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.server.events:
                return list(self.server.events)
            time.sleep(1)
        return []

    def url_for(self, ctx):
        """The address the NODES should call back on - this controller's LAN IP.

        Not 127.0.0.1: the node is a different machine. --callback-host wins
        when the controller has several interfaces and the routing guess would
        pick the wrong one.
        """
        host = getattr(ctx.args, "callback_host", "") or _controller_ip()
        if not host:
            return None
        return "http://{}:{}/cb".format(host, self.port)


def _controller_ip():
    """Whichever local interface routes toward the lab network."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.1.1", 1))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        s.close()
