#!/usr/bin/env python3
"""
basic_cases.py - Rubix's basic operations, on the whole fleet at once.

User, 2026-11-13: "what I want is basic operations in rubix in a large scale"
- and no elaborate set-up that can skip a case. Every operation runs twice: with
one DID on every pool node at the same instant, then with every DID in the pool
at the same instant.

    BASIC-RBT   pay 1 RBT, then 0.37 RBT - each wallet pays the next in a ring
    BASIC-FT    mint 10 FTs (burns 1 RBT), then pass 5 of them to the next wallet
    BASIC-NFT   create and deploy an NFT, execute it, pass it to the next wallet
    BASIC-SC    create and deploy a smart contract, execute it

Run:
    cd ../full-test
    python3 test_runner.py --cases basic --collect-versions
    python3 validate_cases.py --cases basic        # offline

Wallets are used as they are. AUDIT-START runs first and takes corrupt tokens
out of use (full-test/token_health.py); a wallet holding less than it needs is
topped up from the faucet (plumbing). One quorum per machine - the DID there
holding the most Free RBT whose token_denom counters match its tokens - signs
for the nodes of the NEXT machine, so every operation crosses the network; each
quorum is topped up before a round for its share of the pledges. The ring is
interleaved by machine, so every wallet pays a wallet on another machine.

A step PASSES when every operation went through and arrived. Any refusal (with
Rubix's own reason, grouped), anything not delivered, RBT left Locked, a node
that crashed or went deaf makes it FAIL (master/steps.py). Nodes that do not
answer are left out of the round and named; nothing else is skipped.
"""

import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "master"))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import audit_cases
import case_helpers as ch
import db_client as db
import rubix_client as rc
import steps
from master_cases import _merge_registries

SKIP = "SKIP"
PART = 0.37             # the part-RBT amount
FT_MINT, FT_PASS = 10, 5
NEED = 3                # RBT Free a paying wallet holds before a round
SETTLE = 90             # seconds a result gets to show on the node (FT counts, NFT owner)
SCALES = (("N", "every node"), ("D", "every DID"))


# ---------------------------------------------------------------------------
# The fleet: quorums, who signs for whom, the two rings
# ---------------------------------------------------------------------------

def _wallets(ctx):
    """{did: wallet} for every pool DID - Free and Locked RBT, and whether its
    token_denom counters differ from its Free tokens - one read per node."""
    by_host = {}
    for e in ctx.fleet:
        by_host.setdefault(e["host"], []).append(e["did"])

    def one(item):
        host, dids = item
        try:
            status, counted, free = db.query_snapshot(host, [
                ("SELECT did, token_status, COALESCE(SUM(token_value), 0) FROM tokens WHERE "
                 "did = ANY(%s) AND token_type = %s GROUP BY did, token_status", (dids, db.TYPE_RBT)),
                ("SELECT did, denom, count FROM token_denom WHERE did = ANY(%s)", (dids,)),
                ("SELECT did, token_value, COUNT(*) FROM tokens WHERE did = ANY(%s) AND token_type = %s "
                 "AND token_status = %s GROUP BY did, token_value", (dids, db.TYPE_RBT, db.FREE)),
            ])
        except Exception:
            return {}
        out = dict((d, {"host": host, "did": d, "machine": ch.machine(host), "free": 0.0,
                        "locked": 0.0, "drift": False}) for d in dids)
        for d, st, v in status:
            if d in out and int(st) == db.FREE:
                out[d]["free"] = float(v)
            elif d in out and int(st) == db.LOCKED:
                out[d]["locked"] = float(v)
        c = dict(((d, round(float(v), 3)), int(n)) for d, v, n in counted)
        a = dict(((d, round(float(v), 3)), int(n)) for d, v, n in free)
        for k in set(c) | set(a):
            if c.get(k, 0) != a.get(k, 0) and k[0] in out:
                out[k[0]]["drift"] = True
        return out
    merged = {}
    for part in ch.run_parallel(one, list(by_host.items()), workers=32):
        merged.update(part)
    return merged


def _entry(w):
    return {"host": w["host"], "did": w["did"], "machine": w["machine"]}


def _interleave(entries):
    """Round-robin over machines, so neighbours in the ring are on different machines."""
    by_m = {}
    for e in sorted(entries, key=lambda e: (e["host"], e["did"])):
        by_m.setdefault(e["machine"], []).append(e)
    out, lists = [], [by_m[m] for m in sorted(by_m)]
    while any(lists):
        for lst in lists:
            if lst:
                out.append(lst.pop(0))
    return out


def _answers(host):
    ok, _p = rc.http_json("GET", rc.base_url(host) + rc.EP_PEER_ID, 5)
    return ok


class Fleet(object):
    """One quorum per machine, every node pointed at the quorum of the next
    machine, and the payers at both scales."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.quorums, self.signer, self.payers = [], {}, {}
        self.notes = []

    def ready(self):
        """(ok, why) - quorums set up and every node wired."""
        wallets = _wallets(self.ctx)
        if not wallets:
            return False, "no pool node's database could be read"
        machines = sorted(set(w["machine"] for w in wallets.values()))

        def pick(m):
            cands = sorted((w for w in wallets.values() if w["machine"] == m and not w["drift"]
                            and w["locked"] <= 0), key=lambda w: (-w["free"], w["did"]))
            for w in cands[:3]:
                if steps.quorum_ready(_entry(w), 0)[0]:
                    return _entry(w)
            return None
        self.quorums = [q for q in ch.run_parallel(pick, machines, workers=32) if q]
        if not self.quorums:
            return False, "no DID could be set up as a quorum"
        self.ctx.quorum_hosts = list(self.quorums)
        order = [q["machine"] for q in self.quorums]
        qdids = set(q["did"] for q in self.quorums)
        hosts = sorted(set(w["host"] for w in wallets.values()))
        for h in hosts:
            m = ch.machine(h)
            i = order.index(m) if m in order else -1
            self.signer[h] = self.quorums[(i + 1) % len(self.quorums)]
        results = ch.run_parallel(lambda h: (h, rc.quorum_reset(h, [self.signer[h]["did"]])[0]), hosts,
                                  workers=40)
        unwired = set(h for h, ok in results if not ok)
        if unwired:
            self.notes.append("{} node(s) could not be pointed at their quorum and are left out: {}".format(
                len(unwired), ", ".join(steps._short(h) for h in sorted(unwired)[:8])))
        payers = [w for w in wallets.values() if w["did"] not in qdids and w["host"] not in unwired]
        best = {}
        for w in payers:
            if w["host"] not in best or w["free"] > best[w["host"]]["free"]:
                best[w["host"]] = w
        self.payers = {"N": _interleave([_entry(w) for w in best.values()]),
                       "D": _interleave([_entry(w) for w in payers])}
        self.notes.append("{} quorums, one per machine; {} nodes and {} DIDs pay".format(
            len(self.quorums), len(self.payers["N"]), len(self.payers["D"])))
        return True, ""

    def live(self, st, entries, need=0):
        """The entries whose node answers, topped up to `need` RBT Free (the
        faucet pays the shortfall). Left-out wallets are named on the step."""
        hosts = sorted(set(e["host"] for e in entries))
        up = set(h for h, ok in ch.run_parallel(lambda h: (h, _answers(h)), hosts, workers=40) if ok)
        down = [h for h in hosts if h not in up]
        if down:
            st.note("{} node(s) did not answer and were left out: {}".format(
                len(down), ", ".join(steps._short(h) for h in down[:8])))
        entries = [e for e in entries if e["host"] in up]
        if need:
            def top(e):
                ok, detail, _n = rc.get_rbt_balance_detail(e["host"], e["did"])
                have = float(detail["balance"]) if ok and detail else 0.0
                return e, have + 0.0015 >= need or rc.fund_did(e["host"], e["did"], need - have)[0]
            funded = ch.run_parallel(top, entries, workers=16)
            short = [e for e, ok in funded if not ok]
            if short:
                st.note("{} wallet(s) could not be topped up to {} RBT and were left out".format(
                    len(short), need))
            entries = [e for e, ok in funded if ok]
        return entries

    def room(self, payers, each):
        """Each quorum holds Free RBT for its share of this round's pledges."""
        share = {}
        for e in payers:
            q = self.signer[e["host"]]
            share[q["did"]] = share.get(q["did"], 0.0) + each

        def top(q):
            need = share.get(q["did"], 0.0) + 5
            ok, detail, _n = rc.get_rbt_balance_detail(q["host"], q["did"])
            have = float(detail["balance"]) if ok and detail else 0.0
            if have < need:
                rc.fund_did(q["host"], q["did"], round(need - have + 5, 3))
        ch.run_parallel(top, [q for q in self.quorums if q["did"] in share], workers=8)


def _at_once(st, calls):
    """calls: [(fn, args, kwargs, receiver DID or None)], all started at the same
    instant and each recorded on the step. Returns [(ok, msg, result)] in order."""
    if not calls:
        return []
    gate = threading.Barrier(len(calls))

    def one(c):
        fn, a, k, to = c
        try:
            gate.wait(timeout=120)
        except threading.BrokenBarrierError:
            pass
        t0 = time.time()
        try:
            ok, msg, res = fn(*a, **k)
        except Exception as e:
            ok, msg, res = False, "{}: {}".format(type(e).__name__, e), None
        return ok, msg, res, round(time.time() - t0, 2), to
    out = ch.run_parallel(one, calls, workers=len(calls))
    for ok, msg, res, secs, to in out:
        st.op(ok, msg, secs, res.get("transactionID") if isinstance(res, dict) else None, to=to)
    return [(ok, msg, res) for ok, msg, res, _s, _t in out]


def _missing(items, shows, wait=SETTLE):
    """The items `shows(item)` is still False for after `wait` seconds."""
    pending, deadline = list(items), time.time() + wait
    while pending:
        res = ch.run_parallel(lambda i: (i, shows(i)), pending, workers=64)
        pending = [i for i, ok in res if not ok]
        if not pending or time.time() > deadline:
            break
        time.sleep(5)
    return pending


def _set_up(run, fleet):
    def body(st):
        for n in fleet.notes:
            st.note(n)
    return run.step("S1", body, nodes=[], watch=False, deliver=False, require=[fleet.ready])


def _verdict(run, what):
    return run.verdict(passed="every {} went through and arrived, on every node and on every DID".format(what))


T = ch.TRANSFER_TIMEOUT


# ---------------------------------------------------------------------------
# BASIC-RBT
# ---------------------------------------------------------------------------

def basic_rbt(ctx, ci):
    case = "BASIC-RBT"
    run, fleet = steps.CaseRun(ctx, case, STEPS[case]), Fleet(ctx)
    if _set_up(run, fleet).outcome != steps.PASS:
        return _verdict(run, "payment")

    def pay(st, scale, amount):
        payers = fleet.live(st, fleet.payers[scale], NEED)
        fleet.room(payers, amount)
        _at_once(st, [(rc.initiate_transaction, (a["host"], a["did"], b["did"]),
                       {"rbt": amount, "memo": "{} {}".format(case, amount), "timeout": T}, b["did"])
                      for a, b in ch.ring(payers)])
    for sid, scale, amount in (("S2", "N", 1.0), ("S3", "D", 1.0), ("S4", "N", PART), ("S5", "D", PART)):
        run.step(sid, lambda st, s=scale, a=amount: pay(st, s, a),
                 nodes=fleet.payers[scale] + fleet.quorums, must_accept=True)
    return _verdict(run, "payment")


# ---------------------------------------------------------------------------
# BASIC-FT
# ---------------------------------------------------------------------------

def _ft_count(e, name):
    ok, result, _n = rc.get_ft_balance(e["host"], e["did"])
    return rc.ft_count_for(result, name) if ok else None


def basic_ft(ctx, ci):
    case = "BASIC-FT"
    run, fleet = steps.CaseRun(ctx, case, STEPS[case]), Fleet(ctx)
    if _set_up(run, fleet).outcome != steps.PASS:
        return _verdict(run, "FT mint and transfer")
    stamp = int(time.time())
    minted = {}

    def name(scale, e):
        return "basic-ft-{}-{}-{}".format(stamp, scale, e["did"][-6:])

    def mint(st, scale):
        owners = fleet.live(st, fleet.payers[scale], NEED)
        fleet.room(owners, 1.0)
        res = _at_once(st, [(rc.mint_ft, (e["host"], e["did"], name(scale, e), FT_MINT, 1),
                             {"timeout": T}, None) for e in owners])
        done = [e for e, (ok, _m, _r) in zip(owners, res) if ok]
        minted[scale] = done
        lost = _missing(done, lambda e: (_ft_count(e, name(scale, e)) or 0) >= FT_MINT)
        st.check(not lost, "{} of {} minters do not show their {} FTs after {}s, e.g. {}".format(
            len(lost), len(done), FT_MINT, SETTLE, steps._short(lost[0]["host"]) if lost else ""))

    def pass_on(st, scale):
        owners = fleet.live(st, minted.get(scale, []))
        fleet.room(owners, 1.0)
        pairs = ch.ring(owners)
        res = _at_once(st, [(rc.initiate_transaction, (a["host"], a["did"], b["did"]),
                             {"ft": [{"ftName": name(scale, a), "numberOfFts": FT_PASS, "creatorDID": a["did"]}],
                              "memo": "{} pass".format(case), "timeout": T}, b["did"]) for a, b in pairs])
        sent = [(a, b) for (a, b), (ok, _m, _r) in zip(pairs, res) if ok]
        lost = _missing(sent, lambda p: (_ft_count(p[1], name(scale, p[0])) or 0) >= FT_PASS)
        st.check(not lost, "{} of {} receivers do not show the {} FTs passed to them after {}s".format(
            len(lost), len(sent), FT_PASS, SETTLE))

    for scale, (m_sid, p_sid) in (("N", ("S2", "S3")), ("D", ("S4", "S5"))):
        run.step(m_sid, lambda st, s=scale: mint(st, s), nodes=fleet.payers[scale] + fleet.quorums,
                 must_accept=True)
        run.step(p_sid, lambda st, s=scale: pass_on(st, s), nodes=fleet.payers[scale] + fleet.quorums,
                 must_accept=True, require=[lambda s=scale: (len(minted.get(s, [])) >= 2,
                                                             "fewer than two FT series were minted")])
    return _verdict(run, "FT mint and transfer")


# ---------------------------------------------------------------------------
# BASIC-NFT
# ---------------------------------------------------------------------------

def _chain_len(getter, e, token):
    ok, chain, _n = getter(e["host"], token)
    return len(chain or []) if ok else 0


def _holds_nft(e, nft):
    ok, items, _n = rc.get_nft_balance(e["host"], e["did"])
    return ok and nft in str(items)


def basic_nft(ctx, ci):
    case = "BASIC-NFT"
    run, fleet = steps.CaseRun(ctx, case, STEPS[case]), Fleet(ctx)
    if _set_up(run, fleet).outcome != steps.PASS:
        return _verdict(run, "NFT operation")
    nfts = {}       # scale -> [(owner, nft)]

    def deploy(st, scale):
        owners = fleet.live(st, fleet.payers[scale])
        fleet.room(owners, 1.0)
        made = _at_once(st, [(rc.create_nft, (e["host"], e["did"], os.urandom(64), os.urandom(64)),
                              {"timeout": T}, None) for e in owners])
        created = [(e, r) for e, (ok, _m, r) in zip(owners, made) if ok and isinstance(r, str)]
        res = _at_once(st, [(rc.nft_transaction, (e["host"], e["did"], e["did"], n),
                             {"data": "{} deploy".format(case), "timeout": T}, None) for e, n in created])
        nfts[scale] = [(e, n) for (e, n), (ok, _m, _r) in zip(created, res) if ok]
        lost = _missing(nfts[scale], lambda p: _chain_len(rc.get_nft_chain, p[0], p[1]) >= 1)
        st.check(not lost, "{} of {} deployed NFTs have no chain on their owner after {}s".format(
            len(lost), len(nfts[scale]), SETTLE))

    def execute(st, scale):
        have = nfts.get(scale, [])
        up = set(x["host"] for x in fleet.live(st, [p[0] for p in have]))
        have = [(e, n) for e, n in have if e["host"] in up]
        fleet.room([e for e, _n in have], 1.0)
        res = _at_once(st, [(rc.nft_transaction, (e["host"], e["did"], e["did"], n),
                             {"data": "{} execute".format(case), "timeout": T}, None) for e, n in have])
        done = [(e, n) for (e, n), (ok, _m, _r) in zip(have, res) if ok]
        lost = _missing(done, lambda p: _chain_len(rc.get_nft_chain, p[0], p[1]) >= 2)
        st.check(not lost, "{} of {} executed NFTs show no second chain entry on their owner after "
                           "{}s".format(len(lost), len(done), SETTLE))

    def pass_on(st, scale):
        have = nfts.get(scale, [])
        up = set(x["host"] for x in fleet.live(st, [p[0] for p in have]))
        have = [(e, n) for e, n in have if e["host"] in up]
        fleet.room([e for e, _n in have], 1.0)
        pairs = [((a, n), b) for (a, n), (b, _m) in ch.ring(have)]
        res = _at_once(st, [(rc.nft_transaction, (a["host"], a["did"], b["did"], n),
                             {"data": "{} pass".format(case), "transfer_ownership": True, "timeout": T},
                             b["did"]) for (a, n), b in pairs])
        sent = [(n, b) for ((a, n), b), (ok, _m, _r) in zip(pairs, res) if ok]
        lost = _missing(sent, lambda p: _holds_nft(p[1], p[0]))
        st.check(not lost, "{} of {} NFTs passed on are not with their new owner after {}s".format(
            len(lost), len(sent), SETTLE))

    for scale, sids in (("N", ("S2", "S3", "S4")), ("D", ("S5", "S6", "S7"))):
        nodes = fleet.payers[scale] + fleet.quorums
        run.step(sids[0], lambda st, s=scale: deploy(st, s), nodes=nodes, must_accept=True)
        need = [lambda s=scale: (len(nfts.get(s, [])) >= 2, "fewer than two NFTs were deployed")]
        run.step(sids[1], lambda st, s=scale: execute(st, s), nodes=nodes, must_accept=True, require=need)
        run.step(sids[2], lambda st, s=scale: pass_on(st, s), nodes=nodes, must_accept=True, require=need)
    return _verdict(run, "NFT operation")


# ---------------------------------------------------------------------------
# BASIC-SC
# ---------------------------------------------------------------------------

def _new_contract(e):
    """Generate (not deploy) a contract: (ok, msg, id). The server checks both
    file names literally: .wasm and .rs (server/smart_contract.go:70, :101-106)."""
    tag = os.urandom(5).hex()
    return rc.create_smart_contract(e["host"], e["did"], b"\x00asm\x01\x00\x00\x00" + tag.encode(),
                                    ("// basic contract {}\nfn main() {{}}\n".format(tag)).encode(),
                                    timeout=T)


def basic_sc(ctx, ci):
    case = "BASIC-SC"
    run, fleet = steps.CaseRun(ctx, case, STEPS[case]), Fleet(ctx)
    if _set_up(run, fleet).outcome != steps.PASS:
        return _verdict(run, "contract operation")
    contracts = {}

    def deploy(st, scale):
        owners = fleet.live(st, fleet.payers[scale])
        fleet.room(owners, 1.0)
        made = _at_once(st, [(_new_contract, (e,), {}, None) for e in owners])
        created = [(e, str(r)) for e, (ok, _m, r) in zip(owners, made) if ok and r]
        res = _at_once(st, [(rc.sc_transaction, (e["host"], e["did"], c),
                             {"data": "{} deploy".format(case), "timeout": T}, None) for e, c in created])
        contracts[scale] = [(e, c) for (e, c), (ok, _m, _r) in zip(created, res) if ok]
        lost = _missing(contracts[scale], lambda p: _chain_len(rc.get_sc_chain, p[0], p[1]) >= 1)
        st.check(not lost, "{} of {} deployed contracts have no chain on their owner after {}s".format(
            len(lost), len(contracts[scale]), SETTLE))

    def execute(st, scale):
        have = contracts.get(scale, [])
        up = set(x["host"] for x in fleet.live(st, [p[0] for p in have]))
        have = [(e, c) for e, c in have if e["host"] in up]
        fleet.room([e for e, _c in have], 1.0)
        res = _at_once(st, [(rc.sc_transaction, (e["host"], e["did"], c),
                             {"data": "{} execute".format(case), "timeout": T}, None) for e, c in have])
        done = [(e, c) for (e, c), (ok, _m, _r) in zip(have, res) if ok]
        lost = _missing(done, lambda p: _chain_len(rc.get_sc_chain, p[0], p[1]) >= 2)
        st.check(not lost, "{} of {} executed contracts show no second chain entry on their owner "
                           "after {}s".format(len(lost), len(done), SETTLE))

    for scale, sids in (("N", ("S2", "S3")), ("D", ("S4", "S5"))):
        nodes = fleet.payers[scale] + fleet.quorums
        run.step(sids[0], lambda st, s=scale: deploy(st, s), nodes=nodes, must_accept=True)
        run.step(sids[1], lambda st, s=scale: execute(st, s), nodes=nodes, must_accept=True,
                 require=[lambda s=scale: (len(contracts.get(s, [])) >= 1, "no contract was deployed")])
    return _verdict(run, "contract operation")


# ---------------------------------------------------------------------------

_SETUP = ("S1", "Set-up",
          "one quorum per machine (the DID there with the most Free RBT and matching token_denom "
          "counters), set up; every node pointed at the quorum of the next machine",
          "quorums set up; nodes that cannot be pointed at theirs are named and left out")


def _scale_steps(first, rows):
    """Steps for both scales from [(title, what, expected)]."""
    out, n = [], first
    for _key, scale in SCALES:
        for title, what, expect in rows:
            out.append(("S{}".format(n), "{}, {}".format(title, scale), what.format(scale=scale), expect))
            n += 1
    return out


PURPOSE = {
    "BASIC-RBT": "RBT payments, whole and part, from one wallet on every node and then from every "
                 "DID in the pool, all at the same instant.",
    "BASIC-FT": "Minting FTs and passing them on, from every node and then from every DID, all at once.",
    "BASIC-NFT": "Creating, deploying, executing and passing on NFTs, from every node and then from "
                 "every DID, all at once.",
    "BASIC-SC": "Creating, deploying and executing smart contracts, from every node and then from "
                "every DID, all at once.",
}

_ARRIVE = "every one accepted and on the receiver's node; nothing Locked; no node crashes or goes deaf"

STEPS = {
    "BASIC-RBT": [_SETUP] + [
        ("S2", "1 RBT, every node", "one DID per node pays the next node's 1 RBT, all at once", _ARRIVE),
        ("S3", "1 RBT, every DID", "every DID pays the next 1 RBT, all at once", _ARRIVE),
        ("S4", "{} RBT, every node".format(PART), "as S2 with {} RBT (a part payment)".format(PART), _ARRIVE),
        ("S5", "{} RBT, every DID".format(PART), "as S3 with {} RBT".format(PART), _ARRIVE),
    ],
    "BASIC-FT": [_SETUP] + _scale_steps(2, [
        ("Mint", "{{scale}}: each wallet mints {} FTs from 1 RBT, all at once".format(FT_MINT),
         "every mint accepted; each wallet shows its {} FTs".format(FT_MINT)),
        ("Pass on", "{{scale}}: each wallet passes {} of its FTs to the next, all at once".format(FT_PASS),
         "every transfer accepted; each receiver shows the {} FTs".format(FT_PASS)),
    ]),
    "BASIC-NFT": [_SETUP] + _scale_steps(2, [
        ("Create and deploy", "{scale}: each wallet creates an NFT and deploys it, all at once",
         "every one accepted; each NFT has a chain on its owner"),
        ("Execute", "{scale}: each owner executes its NFT, all at once",
         "every one accepted; each chain grows by one"),
        ("Pass on", "{scale}: each owner passes its NFT to the next wallet, all at once",
         "every one accepted; each NFT is with its new owner"),
    ]),
    "BASIC-SC": [_SETUP] + _scale_steps(2, [
        ("Create and deploy", "{scale}: each wallet creates a contract and deploys it, all at once",
         "every one accepted; each contract has a chain on its owner"),
        ("Execute", "{scale}: each owner executes its contract, all at once",
         "every one accepted; each chain grows by one"),
    ]),
}

CASE_INFO = {
    "BASIC-RBT": ("Pay 1 RBT and 0.37 RBT - every node at once, then every DID at once.",
                  "Every payment goes through and reaches the next wallet."),
    "BASIC-FT": ("Mint FTs and pass them on - every node at once, then every DID at once.",
                 "Every mint and transfer goes through; every wallet shows the right FT count."),
    "BASIC-NFT": ("Create, deploy, execute and pass on NFTs - every node at once, then every DID at once.",
                  "Every operation goes through; every NFT ends with its new owner."),
    "BASIC-SC": ("Create, deploy and execute smart contracts - every node at once, then every DID at once.",
                 "Every operation goes through; every contract's chain grows."),
}

_CASES = {"BASIC-RBT": basic_rbt, "BASIC-FT": basic_ft, "BASIC-NFT": basic_nft, "BASIC-SC": basic_sc}


class _Basic(object):
    """The four operations as one module for the registry merge."""
    CASES = _CASES
    ORDER = list(_CASES)
    NEEDS = dict((t, {"senders": 0, "receivers": 0, "quorums": 0, "exclusive": True}) for t in _CASES)
    NO_DELAY_RERUN = set(_CASES)
    CASE_INFO = CASE_INFO


# The audit opens and closes the run, as in every catalogue run.
CASES, ORDER, NEEDS, TIMING_CASES, CASE_INFO, NO_DELAY_RERUN = _merge_registries([audit_cases, _Basic])
