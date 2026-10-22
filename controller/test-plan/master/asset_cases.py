"""asset_cases.py - FT, NFT and smart contracts at scale, with real history.

One unit, so the stock each case leaves (FT series passed through several
wallets, NFTs with children and owners along a chain, contracts with
subscribers at different depths) is there for the cases after it. Every owner
works at the same time; every asset makes several hops. Each case checks the
end state on every node involved - balances, owners, chains - not only that
the API answered.

The product's CI does one of each between two nodes (test/integration); these
run many at once across the fleet.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import case_helpers as ch

SKIP = "SKIP"
OWNERS = 8
RECEIVERS = 30


def _hops(ctx):
    return max(2, int(ctx.args.asset_hops))


def _times(label, secs):
    if not secs:
        return "{}: none".format(label)
    return "{} x{}: p50 {:.1f}s p95 {:.1f}s".format(label, len(secs), ch.percentile(secs, 50),
                                                    ch.percentile(secs, 95))


def _timed(fn, *a, **k):
    t0 = time.time()
    out = fn(*a, **k)
    return out, round(time.time() - t0, 2)


def _random_bytes(n=64):
    return os.urandom(n)


# ---------------------------------------------------------------------------
# FT-01
# ---------------------------------------------------------------------------

def _ft_count(entry, name):
    ok, result, _n = rc.get_ft_balance(entry["host"], entry["did"])
    return rc.ft_count_for(result, name) if ok else None


def _ft_settle(entry, name, expected, attempts=40, delay=2):
    """Poll until the holder shows exactly `expected` of `name`."""
    count = None
    for _ in range(attempts):
        count = _ft_count(entry, name)
        if count == expected:
            return True, count
        time.sleep(delay)
    return False, count


def ft_01(ctx, ci):
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 5:
        return SKIP, "needs FT owners and at least 5 receivers", ""
    hops = _hops(ctx)
    stamp = int(time.time())
    expect = {}                     # (did, name) -> count
    entry_of = dict((e["did"], e) for e in owners + list(recv))
    mint_secs, send_secs, problems = [], [], []

    def name_of(owner, tag):
        return "lab-ft-{}-{}-{}".format(tag, stamp, owner["did"][-6:])

    # 1. Two series per owner, all owners at once: 1,000 FTs from 1 RBT (the
    #    1,000-per-RBT cap, core/ft.go:70) and 4,000 from 4 RBT. Then 1,000 more
    #    of the first series under the SAME name, numbered on from 1,000
    #    (FTNumStartIndex, core/ft.go:223): the counts add up (ON CONFLICT DO
    #    UPDATE on fts) rather than a second series.
    def mint(owner):
        out = []
        for tag, count, tokens, start in (("A", 1000, 1, 0), ("B", 4000, 4, 0),
                                          ("A", 1000, 1, 1000)):
            (ok, msg, _r), secs = _timed(rc.mint_ft, owner["host"], owner["did"],
                                         name_of(owner, tag), count, tokens,
                                         timeout=ch.TRANSFER_TIMEOUT, start_index=start)
            out.append((tag, count, ok, msg, secs))
        return owner, out

    for owner, out in ch.run_parallel(mint, owners):
        for tag, count, ok, msg, secs in out:
            mint_secs.append(secs)
            if ok:
                key = (owner["did"], name_of(owner, tag))
                expect[key] = expect.get(key, 0) + count
            else:
                problems.append("mint {} of {} refused: {}".format(count, tag, ch.node_reason(msg)))
    settled = ch.run_parallel(lambda kv: (kv[0], _ft_settle(entry_of[kv[0][0]], kv[0][1], kv[1])),
                              list(expect.items()))
    for (did, name), (ok, count) in settled:
        if not ok:
            problems.append("{} shows {} of {} after minting, not {}".format(
                did[-6:], count, name, expect[(did, name)]))

    # 2. Series A travels through `hops` wallets per owner, owners in parallel:
    #    each holder keeps 200 and passes the rest on.
    def travel(i):
        owner = owners[i]
        name = name_of(owner, "A")
        holder, amount = owner, expect.get((owner["did"], name), 0) - 300
        path = [recv[(i * hops + k) % len(recv)] for k in range(hops)]
        moves = []
        for nxt in path:
            if amount <= 0 or nxt["did"] == holder["did"]:
                break
            (ok, msg, _r), secs = _timed(
                rc.initiate_transaction, holder["host"], holder["did"], nxt["did"],
                ft=[{"ftName": name, "numberOfFts": amount, "creatorDID": owner["did"]}],
                memo="FT-01 hop", timeout=ch.TRANSFER_TIMEOUT)
            moves.append((holder["did"], nxt["did"], name, amount, ok, msg, secs))
            if not ok:
                break
            credited, _c, _raw = rc.wait_for_ft_count(nxt["host"], nxt["did"], name, amount,
                                                      attempts=40, delay=2)
            if not credited:
                break
            holder, amount = nxt, amount - 200
        return moves

    hop_count = 0
    for moves in ch.run_parallel(travel, range(len(owners))):
        for frm, to, name, amount, ok, msg, secs in moves:
            send_secs.append(secs)
            if ok:
                hop_count += 1
                expect[(frm, name)] = expect.get((frm, name), 0) - amount
                expect[(to, name)] = expect.get((to, name), 0) + amount
            else:
                problems.append("FT hop of {} refused: {}".format(amount, ch.node_reason(msg)))

    # 3. Fan-out: every owner sends 500 of series B to five wallets at once.
    jobs, meta = [], []
    for i, owner in enumerate(owners):
        name = name_of(owner, "B")
        for k in range(5):
            r = recv[(i * 5 + k + 3) % len(recv)]
            jobs.append({"host": owner["host"], "did": owner["did"], "to": r["did"],
                         "ft": [{"ftName": name, "numberOfFts": 500, "creatorDID": owner["did"]}],
                         "memo": "FT-01 fan-out"})
            meta.append((owner["did"], r["did"], name))
    fan = ch.burst(jobs)
    for (frm, to, name), res in zip(meta, fan):
        send_secs.append(res["secs"])
        if res["ok"]:
            expect[(frm, name)] = expect.get((frm, name), 0) - 500
            expect[(to, name)] = expect.get((to, name), 0) + 500
    fan_s = ch.summarize(fan)
    if fan_s["failed"]:
        problems.append("{} of {} concurrent FT sends from one wallet refused: {}".format(
            fan_s["failed"], fan_s["n"], fan_s["reasons"]))

    # 4. Two series in ONE transaction.
    def both(i):
        owner, r = owners[i], recv[(i + 7) % len(recv)]
        a, b = name_of(owner, "A"), name_of(owner, "B")
        (ok, msg, _r), secs = _timed(
            rc.initiate_transaction, owner["host"], owner["did"], r["did"],
            ft=[{"ftName": a, "numberOfFts": 100, "creatorDID": owner["did"]},
                {"ftName": b, "numberOfFts": 100, "creatorDID": owner["did"]}],
            memo="FT-01 two series", timeout=ch.TRANSFER_TIMEOUT)
        return owner["did"], r["did"], a, b, ok, msg, secs

    for frm, to, a, b, ok, msg, secs in ch.run_parallel(both, range(len(owners))):
        send_secs.append(secs)
        if ok:
            for name in (a, b):
                expect[(frm, name)] = expect.get((frm, name), 0) - 100
                expect[(to, name)] = expect.get((to, name), 0) + 100
        else:
            problems.append("two-series FT transfer refused: {}".format(ch.node_reason(msg)))

    # 5. Every holder of every series shows exactly what it should.
    final = ch.run_parallel(lambda kv: (kv[0], kv[1], _ft_settle(entry_of[kv[0][0]], kv[0][1], kv[1])),
                            list(expect.items()))
    wrong = [(did, name, want, count) for (did, name), want, (ok, count) in final if not ok]
    if wrong:
        problems.append("{} holder(s) do not show what they should, e.g. {} holds {} of {}, "
                        "expected {}".format(len(wrong), wrong[0][0][-6:], wrong[0][3],
                                             wrong[0][1], wrong[0][2]))
    note = "{} series on {} owners, {} hops, fan-out {}/{} | {} | {} | {} holders checked".format(
        len(owners) * 2, len(owners), hop_count, fan_s["ok"], fan_s["n"],
        _times("mint", mint_secs), _times("FT transfer", send_secs), len(expect))
    if problems:
        return False, "; ".join(problems[:3]), note
    return True, "every FT series reconciles across {} holders".format(len(expect)), note


# ---------------------------------------------------------------------------
# NFT-01
# ---------------------------------------------------------------------------

def _holds_nft(entry, nft_id):
    ok, items, _n = rc.get_nft_balance(entry["host"], entry["did"])
    return ok and nft_id in json.dumps(items)


def _chain_len(getter, entry, token_id):
    ok, chain, _n = getter(entry["host"], token_id)
    return len(chain) if ok else None


def _chain_settle(getter, entry, token_id, want, attempts=30, delay=2):
    n = None
    for _ in range(attempts):
        n = _chain_len(getter, entry, token_id)
        if n is not None and n >= want:
            return True, n
        time.sleep(delay)
    return False, n


def _poll(check, timeout=60, delay=0.5):
    """check() until it is true or `timeout` seconds pass: (ok, seconds waited)."""
    t0 = time.time()
    while True:
        if check():
            return True, round(time.time() - t0, 1)
        if time.time() - t0 >= timeout:
            return False, round(time.time() - t0, 1)
        time.sleep(delay)


def _caught_up(getter, entry, ref, token_id, timeout=60):
    """Wait until `entry`'s own copy of a token's chain is as long as `ref`'s.

    A node builds its next NFT/contract operation on its own copy: one that is
    behind is refused by a quorum that has seen more ('chain mismatch after
    sync'), and accepted by one that has not, forking the chain. A node with no
    copy at all sends the operation as a NEW deploy of the token
    (core/transaction_builder.go:369). Waiting first tests the operation, and
    the wait itself is the propagation time. Returns (ok, seconds)."""
    want = _chain_len(getter, ref, token_id) or 0
    return _poll(lambda: (_chain_len(getter, entry, token_id) or 0) >= want, timeout)


def nft_01(ctx, ci):
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 3:
        return SKIP, "needs NFT owners and at least 3 receivers", ""
    hops = _hops(ctx)
    timing = {"create": [], "deploy": [], "children": [], "execute": [], "transfer": [],
              "subscriber catch-up": [], "arrival at new owner": []}

    def one(i):
        owner = owners[i]
        out = {"problems": [], "owner": owner}

        def step(kind, who, fn, *a, **k):
            (ok, msg, result), secs = _timed(fn, *a, **k)
            timing[kind].append(secs)
            if not ok:
                out["problems"].append("{} by {} refused: {}".format(kind, who, ch.node_reason(msg)))
            return ok, result

        ids = []
        for _k in range(2):
            ok, nft_id = step("create", "the owner", rc.create_nft, owner["host"], owner["did"],
                              _random_bytes(), _random_bytes())
            if not ok or not isinstance(nft_id, str):
                return out
            ok, _r = step("deploy", "the owner", rc.nft_transaction, owner["host"], owner["did"],
                          owner["did"], nft_id, data="NFT-01 deploy", timeout=ch.TRANSFER_TIMEOUT)
            if not ok:
                return out
            ids.append(nft_id)
        parent, mover = ids
        out["parent"], out["mover"] = parent, mover

        ok, result = step("children", "the owner", rc.mint_nft_children, owner["host"],
                          owner["did"], parent, 3, timeout=ch.TRANSFER_TIMEOUT)
        out["children_minted"] = len(rc.minted_children(result)) if ok else 0
        for _k in range(3):
            step("execute", "the owner", rc.nft_transaction, owner["host"], owner["did"],
                 owner["did"], parent, data="NFT-01 execute", timeout=ch.TRANSFER_TIMEOUT)

        # A subscriber on another node executes the parent - once its own copy
        # of the chain has caught up with the owner's (see _caught_up).
        sub = recv[(i * hops + hops) % len(recv)]
        ok_sub, msg = rc.subscribe_nft(sub["host"], parent)
        if not ok_sub:
            out["problems"].append("subscribe refused: {}".format(ch.node_reason(msg)))
        else:
            synced, lag = _caught_up(rc.get_nft_chain, sub, owner, parent)
            timing["subscriber catch-up"].append(lag)
            if synced:
                step("execute", "a subscriber", rc.nft_transaction, sub["host"], sub["did"],
                     sub["did"], parent, data="NFT-01 subscriber execute",
                     timeout=ch.TRANSFER_TIMEOUT)
                out["sub"] = sub
            else:
                out["problems"].append("the subscriber's copy of NFT {} was still behind the "
                                       "owner's after {:.0f}s".format(parent[:10], lag))

        # The second NFT changes owner `hops` times; each new owner passes it on
        # once its own node lists it.
        holder, path = owner, [recv[(i * hops + k) % len(recv)] for k in range(hops)]
        moved = 0
        for nxt in path:
            if nxt["did"] == holder["did"]:
                continue
            who = "the first owner" if moved == 0 else "owner {}".format(moved + 1)
            ok, _r = step("transfer", who, rc.nft_transaction, holder["host"], holder["did"],
                          nxt["did"], mover, data="NFT-01 transfer", transfer_ownership=True,
                          timeout=ch.TRANSFER_TIMEOUT)
            if not ok:
                break
            arrived, lag = _poll(lambda: _holds_nft(nxt, mover))
            timing["arrival at new owner"].append(lag)
            holder, moved = nxt, moved + 1
            if not arrived:
                out["problems"].append("NFT {} not listed by its new owner {:.0f}s after the "
                                       "transfer".format(mover[:10], lag))
                break
        out["holder"], out["moved"] = holder, moved
        return out

    results = ch.run_parallel(one, range(len(owners)))
    problems = []
    checked = 0
    for out in results:
        problems += out["problems"]
        owner = out["owner"]
        if "parent" not in out:
            continue
        ok, kids, _n = rc.get_nft_children(owner["host"], out["parent"])
        if out.get("children_minted", 0) and (not ok or len(kids) < out["children_minted"]):
            problems.append("{} lists {} children, {} were minted".format(
                out["parent"][:10], len(kids) if ok else "?", out["children_minted"]))
        owner_len = _chain_len(rc.get_nft_chain, owner, out["parent"])
        if out.get("sub") and owner_len:
            synced, sub_len = _chain_settle(rc.get_nft_chain, out["sub"], out["parent"], owner_len)
            if not synced:
                problems.append("NFT {} chain is {} long on the owner, {} on the subscriber".format(
                    out["parent"][:10], owner_len, sub_len))
        if out.get("moved"):
            if not _holds_nft(out["holder"], out["mover"]):
                problems.append("NFT {} is not with its last owner after {} transfers".format(
                    out["mover"][:10], out["moved"]))
            elif _holds_nft(owner, out["mover"]):
                problems.append("NFT {} still shows with its first owner too".format(out["mover"][:10]))
        checked += 1
    note = "{} owners, 2 NFTs each, 3 children, 4 executes (1 by a subscriber), up to {} owner " \
           "changes | {}".format(len(owners), hops, " | ".join(_times(k, v) for k, v in timing.items()))
    if problems:
        return False, "; ".join(problems[:3]), note
    return True, "{} NFT families consistent: children, chains across nodes, owners".format(checked), note


# ---------------------------------------------------------------------------
# SC-01
# ---------------------------------------------------------------------------

def sc_01(ctx, ci):
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 4:
        return SKIP, "needs contract owners and at least 4 subscribers", ""
    per_owner = max(1, int(round(2 * ctx.args.scale)))
    timing = {"deploy": [], "execute": [], "subscribe": [], "catch-up before execute": []}

    def contract(job):
        i, c = job
        owner = owners[i]
        subs = [recv[((i * per_owner + c) * 4 + j) % len(recv)] for j in range(4)]
        out = {"owner": owner, "subs": [], "problems": [], "executes": 0}
        sc_id, err = ch._sc_new_contract(ctx, owner)
        if not sc_id:
            out["problems"].append("generate failed: {}".format(err))
            return out
        out["sc"] = sc_id
        last = [owner]          # whoever extended the chain last

        def run(entry, kind, data):
            who = "the owner" if entry is owner else "subscriber {}".format(subs.index(entry) + 1)
            # Every executor builds on its own copy of the chain, so it waits
            # until that copy has the previous execute (see _caught_up).
            if kind == "execute" and entry is not last[0]:
                synced, lag = _caught_up(rc.get_sc_chain, entry, last[0], sc_id)
                timing["catch-up before execute"].append(lag)
                if not synced:
                    out["problems"].append("{}'s copy of contract {} was still behind after "
                                           "{:.0f}s".format(who, sc_id[:10], lag))
                    return False
            (ok, msg, _r), secs = _timed(rc.sc_transaction, entry["host"], entry["did"], sc_id,
                                         data=data, timeout=ch.TRANSFER_TIMEOUT)
            timing[kind].append(secs)
            if not ok:
                out["problems"].append("{} by {} refused: {}".format(kind, who, ch.node_reason(msg)))
            else:
                last[0] = entry
                if kind == "execute":
                    out["executes"] += 1
            return ok

        def subscribe(entry):
            (ok, msg), secs = _timed(rc.subscribe_smart_contract, entry["host"], sc_id)
            timing["subscribe"].append(secs)
            if ok:
                out["subs"].append(entry)
            else:
                out["problems"].append("subscribe refused: {}".format(ch.node_reason(msg)))

        if not run(owner, "deploy", "SC-01 deploy"):
            return out
        # Subscribers join at different depths while the chain keeps growing,
        # and two of them execute too.
        subscribe(subs[0])
        run(owner, "execute", "SC-01 owner 1")
        run(owner, "execute", "SC-01 owner 2")
        subscribe(subs[1])
        if subs[0] in out["subs"]:
            run(subs[0], "execute", "SC-01 subscriber 1")
        subscribe(subs[2])
        run(owner, "execute", "SC-01 owner 3")
        if subs[1] in out["subs"]:
            run(subs[1], "execute", "SC-01 subscriber 2")
        subscribe(subs[3])
        return out

    jobs = [(i, c) for i in range(len(owners)) for c in range(per_owner)]
    results = ch.run_parallel(contract, jobs)
    problems, diverged, good = [], [], 0
    for out in results:
        problems += out["problems"]
        if "sc" not in out:
            continue
        owner_len = _chain_len(rc.get_sc_chain, out["owner"], out["sc"])
        if not owner_len:
            problems.append("contract {} has no chain on its owner".format(out["sc"][:10]))
            continue
        bad = []
        for sub in out["subs"]:
            synced, n = _chain_settle(rc.get_sc_chain, sub, out["sc"], owner_len)
            if not synced:
                bad.append("{} has {}".format(sub["did"][-6:], n))
        if bad:
            diverged.append("{} (owner {}): {}".format(out["sc"][:10], owner_len, ", ".join(bad)))
        else:
            good += 1
    if diverged:
        problems.insert(0, "{} contract(s) diverged between owner and subscribers: {}".format(
            len(diverged), "; ".join(diverged[:2])))
    note = "{} contracts, 4 subscribers each joining at depths 1/3/4/6, 5 executes each (2 by " \
           "subscribers) | {}".format(len(jobs), " | ".join(_times(k, v) for k, v in timing.items()))
    if problems:
        return False, "; ".join(problems[:3]), note
    return True, "{} contracts: every subscriber's chain matches the owner's".format(good), note


CASES = {"FT-01": ft_01, "NFT-01": nft_01, "SC-01": sc_01}
ORDER = ["FT-01", "NFT-01", "SC-01"]
NEEDS = {
    "assets": {"cases": ["FT-01", "NFT-01", "SC-01"], "senders": OWNERS,
               "receivers": RECEIVERS, "quorums": 2, "fund": 30, "quorum_fund": 200},
}
# Stock builders and capacity measures: a delayed rerun would build the stock twice.
NO_DELAY_RERUN = set(ORDER)
