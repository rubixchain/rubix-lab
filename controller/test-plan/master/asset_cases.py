"""asset_cases.py - FT, NFT and smart contracts at scale, with real history.

One unit, so the stock each case leaves (FT series passed through several
wallets, NFTs with children and owners along a chain, contracts with
subscribers at different depths) is there for the cases after it. Every owner
works at the same time; every asset makes several hops. Each step checks the
end state on every node involved - balances, owners, chains - not only that
the API answered (master/steps.py).

The steps that matter most for the software's limits:
  FT-01 S2  a mint over the 1,000-per-RBT cap must be refused at once and burn
            nothing (a failed FT mint left its tokens Locked on 2026-10-01);
  FT-01 S4  how many FTs one transfer can carry - on 2026-10-22 1,100-1,300 FTs
            dropped the connection (RemoteDisconnected), which is not a gentle
            refusal unless the machine itself ran out;
  NFT-01 S5 / SC-01 S3  two executes of the same token at the same instant:
            one may lose, but never a fork;
  NFT-01 S7 a former owner sending an NFT it no longer holds must be refused,
            never turned into a second deploy (core/transaction_builder.go:369).

The product's CI does one of each between two nodes (test/integration); these
run many at once across the fleet.
"""

import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import case_helpers as ch
import steps

SKIP = "SKIP"
OWNERS = 8
RECEIVERS = 30
FT_SIZES = (100, 500, 1000, 2000)       # FT-01 S4: FTs in one transfer
FT_BUDGET = 300                         # an FT refusal may take this long and still be gentle

PURPOSE = {
    "FT-01": "Whether FT minting holds its cap (and a refused mint burns nothing), how many FTs "
             "one transfer can carry before Rubix must refuse, and whether concurrent FT sends "
             "from one wallet never move the same FT twice.",
    "NFT-01": "Whether NFTs keep one chain across nodes when owners, subscribers and new owners "
              "act on them at once - including two executes at the same instant - and whether a "
              "node that no longer holds an NFT is refused rather than allowed a second deploy.",
    "SC-01": "Whether a contract keeps one chain across its owner and subscribers who join at "
             "different depths and execute - including two executes at the same instant.",
}

STEPS = {
    "FT-01": [
        ("S1", "Mint at the cap, every owner at once",
         "each owner mints series A, 1,000 FTs from 1 RBT (the 1,000-per-RBT cap, core/ft.go:70), "
         "and series B, 4,000 FTs from 4 RBT",
         "accepted; each owner shows 1,000 A and 4,000 B"),
        ("S2", "Mint over the cap, every owner at once",
         "each owner mints 1,001 FTs from 1 RBT",
         "refused with a stated reason within the budget; no RBT burnt for FT, nothing Locked"),
        ("S3", "Re-mint under the same name",
         "each owner mints 1,000 more of series A, numbered on from 1,000 (core/ft.go:223)",
         "accepted; series A adds up to 2,000 in one series"),
        ("S4", "FTs per transfer (one per size)",
         "one owner sends 100, 500, 1,000 and 2,000 FTs of series B in one transfer each",
         "accepted and credited, the time recorded; where Rubix cannot carry a size, a stated "
         "refusal within the budget - not a dropped connection or a node restart"),
        ("S5", "A series through several wallets",
         "series A passes through --asset-hops wallets per owner, each holder keeping 200",
         "each hop accepted; each new holder shows its count"),
        ("S6", "Five FT sends at once from one wallet",
         "each owner but the first sends 500 FTs of series B to five wallets at the same instant",
         "every accepted send moved its own FTs - the owner down by exactly 500 per accepted "
         "send; refusals stated; no FT left Locked"),
        ("S7", "Two series in one transaction",
         "each owner sends 100 of A and 100 of B in one transaction",
         "accepted; both arrive together"),
        ("S8", "Reconcile",
         "read every holder of every series",
         "every holder shows exactly what it should"),
    ],
    "NFT-01": [
        ("S1", "Create and deploy, every owner at once",
         "each owner creates and deploys two NFTs", "accepted; each listed by its owner"),
        ("S2", "Children", "each owner mints 3 children under its first NFT",
         "the parent lists 3 children"),
        ("S3", "Owner executes", "each owner executes its first NFT 3 times, one after another",
         "each accepted; the owner's chain grows by 3"),
        ("S4", "A subscriber executes",
         "a node of another machine subscribes, waits until its copy of the chain has caught up, "
         "then executes",
         "subscribed, caught up within 60s, accepted; the chains equal on owner and subscriber"),
        ("S5", "Two executes at the same instant",
         "the owner and the subscriber execute the same NFT at the same instant",
         "never a fork: one may be refused with a stated reason, or both land one after the "
         "other; the chains end equal on both nodes"),
        ("S6", "New owners",
         "each owner's second NFT changes owner --asset-hops times",
         "each transfer accepted; the new owner lists it within 60s"),
        ("S7", "A former owner sends it again",
         "the first owner tries to transfer the NFT it passed on",
         "refused with a stated reason; the last owner still holds it - never a second deploy "
         "(core/transaction_builder.go:369)"),
        ("S8", "Reconcile", "read children, chains and owners",
         "children listed; owner's and subscriber's chains equal; each NFT with its last owner only"),
    ],
    "SC-01": [
        ("S1", "Deploy, every owner at once", "each owner deploys --scale x 2 contracts",
         "accepted; each contract has a chain on its owner"),
        ("S2", "Subscribers join while the chain grows",
         "4 subscribers per contract join at depths 1, 3, 4 and 6; the owner executes 3 times and "
         "two subscribers execute once each, each executor first waiting until its own copy has "
         "caught up",
         "every subscribe and execute accepted; every executor's copy caught up within 60s"),
        ("S3", "Two executes at the same instant",
         "two caught-up subscribers execute the same contract at the same instant",
         "never a fork: one may be refused with a stated reason; the chains end equal"),
        ("S4", "Reconcile", "read every subscriber's chain",
         "equal to the owner's on every contract"),
    ],
}


def _hops(ctx):
    return max(2, int(ctx.args.asset_hops))


def _random_bytes(n=64):
    return os.urandom(n)


def _poll(check, timeout=60, delay=0.5):
    """check() until it is true or `timeout` seconds pass: (ok, seconds waited)."""
    t0 = time.time()
    while True:
        if check():
            return True, round(time.time() - t0, 1)
        if time.time() - t0 >= timeout:
            return False, round(time.time() - t0, 1)
        time.sleep(delay)


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


def _ft_send(st, frm, to, items, memo):
    return ch.send(st, frm, to, ft=items, memo=memo, set_aside=())


def ft_01(ctx, ci):
    run = steps.CaseRun(ctx, "FT-01", STEPS["FT-01"], budget=FT_BUDGET)
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 5:
        return SKIP, "needs FT owners and at least 5 receivers", ""
    hops = _hops(ctx)
    ch.ensure_quorums(ctx)      # a quorum that restarted in an earlier case lost its setup
    quorums = list(ctx.quorum_hosts or [])
    stamp = int(time.time())
    expect = {}                     # (did, name) -> count
    entry_of = dict((e["did"], e) for e in owners + list(recv))

    def name_of(owner, tag):
        return "lab-ft-{}-{}-{}".format(tag, stamp, owner["did"][-6:])

    def ft(owner, tag, n):
        return [{"ftName": name_of(owner, tag), "numberOfFts": n, "creatorDID": owner["did"]}]

    def reconcile(st, keys):
        settled = ch.run_parallel(lambda k: (k, _ft_settle(entry_of[k[0]], k[1], expect[k])), keys)
        for (did, name), (ok, count) in settled:
            st.check(ok, "{} shows {} of {}, expected {}".format(did[-6:], count, name, expect[(did, name)]))

    def mint(st, plan):
        """plan: [(tag, count, rbt, start)] for every owner at once."""
        def one(owner):
            out = []
            for tag, count, tokens, start in plan:
                t0 = time.time()
                ok, msg, _r = rc.mint_ft(owner["host"], owner["did"], name_of(owner, tag), count,
                                         tokens, timeout=ch.TRANSFER_TIMEOUT, start_index=start)
                out.append((owner, tag, count, ok, msg, round(time.time() - t0, 2)))
            return out
        done = []
        for rows in ch.run_parallel(one, owners):
            for owner, tag, count, ok, msg, secs in rows:
                st.op(ok, msg, secs)
                if ok:
                    key = (owner["did"], name_of(owner, tag))
                    expect[key] = expect.get(key, 0) + count
                    done.append(key)
        return done

    s1 = run.step("S1", lambda st: reconcile(st, mint(st, (("A", 1000, 1, 0), ("B", 4000, 4, 0)))),
                  nodes=owners + quorums, must_accept=True)
    if s1.outcome != steps.PASS:
        return run.verdict()

    def over_cap(st):
        burnt0 = dict((o["did"], db.value_in_status(o["host"], o["did"], db.BURNT_FOR_FT)) for o in owners)
        mint(st, (("X", 1001, 1, 0),))
        time.sleep(ch.SETTLE)
        for o in owners:
            burnt = db.value_in_status(o["host"], o["did"], db.BURNT_FOR_FT) - burnt0[o["did"]]
            st.check(burnt <= steps.TOL, "{} burnt {:.3f} RBT for a refused mint".format(o["did"][-6:], burnt))
    run.step("S2", over_cap, nodes=owners + quorums, expect_refusal=True, budget=60)

    run.step("S3", lambda st: reconcile(st, mint(st, (("A", 1000, 1, 1000),))), nodes=owners + quorums,
             must_accept=True)

    # S4 - one owner, one series, growing transfers; stop at the first size
    # that does not go through cleanly.
    first = owners[0]
    for k, size in enumerate(FT_SIZES):
        r = recv[(k + 20) % len(recv)]
        if expect.get((first["did"], name_of(first, "B")), 0) < size:
            break

        def body(st, r=r, size=size):
            ok, _m, _r = _ft_send(st, first, r, ft(first, "B", size), "FT-01 size {}".format(size))
            if ok:
                key_from, key_to = (first["did"], name_of(first, "B")), (r["did"], name_of(first, "B"))
                expect[key_from] -= size
                expect[key_to] = expect.get(key_to, 0) + size
                entry_of[r["did"]] = r
                reconcile(st, [key_from, key_to])
        st = run.step("S4", body, nodes=[first, r] + quorums, label="{} FTs".format(size))
        if st.accepted:
            st.data["seconds"] = st.accepted[0]["secs"]
        if st.outcome != steps.PASS:
            break

    def travel(st):
        def one(i):
            owner = owners[i]
            name = name_of(owner, "A")
            holder, amount = owner, expect.get((owner["did"], name), 0) - 300
            moves = []
            for nxt in [recv[(i * hops + k) % len(recv)] for k in range(hops)]:
                if amount <= 0 or nxt["did"] == holder["did"]:
                    break
                t0 = time.time()
                ok, msg, res = rc.initiate_transaction(holder["host"], holder["did"], nxt["did"],
                                                       ft=[{"ftName": name, "numberOfFts": amount,
                                                            "creatorDID": owner["did"]}],
                                                       memo="FT-01 hop", timeout=ch.TRANSFER_TIMEOUT)
                moves.append((holder, nxt, name, amount, ok, msg, round(time.time() - t0, 2),
                              res.get("transactionID") if isinstance(res, dict) else None))
                if not ok or not rc.wait_for_ft_count(nxt["host"], nxt["did"], name, amount,
                                                      attempts=40, delay=2)[0]:
                    break
                holder, amount = nxt, amount - 200
            return moves
        for moves in ch.run_parallel(one, range(len(owners))):
            for frm, to, name, amount, ok, msg, secs, txid in moves:
                st.op(ok, msg, secs, txid, to=to["did"])
                if ok:
                    expect[(frm["did"], name)] = expect.get((frm["did"], name), 0) - amount
                    expect[(to["did"], name)] = expect.get((to["did"], name), 0) + amount
                    entry_of[to["did"]] = to
    run.step("S5", travel, nodes=owners + list(recv) + quorums, must_accept=True)

    fanners = owners[1:]

    def fan_out(st):
        jobs, meta = [], []
        before = dict((o["did"], _ft_count(o, name_of(o, "B"))) for o in fanners)
        for i, owner in enumerate(fanners):
            for k in range(5):
                r = recv[(i * 5 + k + 3) % len(recv)]
                jobs.append({"host": owner["host"], "did": owner["did"], "to": r["did"],
                             "ft": ft(owner, "B", 500), "memo": "FT-01 fan-out"})
                meta.append((owner, r))
        results = st.add(ch.burst(jobs), jobs)
        sent = {}
        for (owner, r), res in zip(meta, results):
            if res["ok"]:
                sent[owner["did"]] = sent.get(owner["did"], 0) + 500
                key = (r["did"], name_of(owner, "B"))
                expect[key] = expect.get(key, 0) + 500
                entry_of[r["did"]] = r
        for o in fanners:
            key = (o["did"], name_of(o, "B"))
            expect[key] = expect.get(key, 0) - sent.get(o["did"], 0)
            ok, now = _ft_settle(o, name_of(o, "B"), expect[key])
            st.check(ok, "{} is down {} FTs of B for {} accepted 500-FT sends".format(
                o["did"][-6:], (before.get(o["did"]) or 0) - (now or 0), sent.get(o["did"], 0) // 500))
    run.step("S6", fan_out, nodes=fanners + list(recv) + quorums)

    def both(st):
        def one(i):
            owner, r = owners[i], recv[(i + 7) % len(recv)]
            items = ft(owner, "A", 100) + ft(owner, "B", 100)
            t0 = time.time()
            ok, msg, res = rc.initiate_transaction(owner["host"], owner["did"], r["did"], ft=items,
                                                   memo="FT-01 two series", timeout=ch.TRANSFER_TIMEOUT)
            return owner, r, ok, msg, round(time.time() - t0, 2), \
                res.get("transactionID") if isinstance(res, dict) else None
        for owner, r, ok, msg, secs, txid in ch.run_parallel(one, range(len(owners))):
            st.op(ok, msg, secs, txid, to=r["did"])
            if ok:
                entry_of[r["did"]] = r
                for tag in ("A", "B"):
                    expect[(owner["did"], name_of(owner, tag))] -= 100
                    key = (r["did"], name_of(owner, tag))
                    expect[key] = expect.get(key, 0) + 100
    run.step("S7", both, nodes=owners + list(recv) + quorums, must_accept=True)

    run.step("S8", lambda st: reconcile(st, list(expect)), nodes=[], watch=False)
    return run.verdict(passed="every FT series reconciles across {} holders".format(len(expect)),
                       limit="the FTs one transfer can carry, refused gently")


# ---------------------------------------------------------------------------
# NFT-01
# ---------------------------------------------------------------------------

def _holds_nft(entry, nft_id):
    ok, items, _n = rc.get_nft_balance(entry["host"], entry["did"])
    return ok and nft_id in json.dumps(items)


def _chain(getter, entry, token_id):
    """The token's chain on `entry`'s node as a list of transaction IDs, or None."""
    ok, chain, _n = getter(entry["host"], token_id)
    if not ok:
        return None
    out = []
    for c in chain or []:
        if isinstance(c, dict):
            out.append(str(c.get("transactionId") or c.get("TransactionID") or c.get("transaction_id")
                           or json.dumps(c, sort_keys=True)))
        else:
            out.append(str(c))
    return out


def _chain_len(getter, entry, token_id):
    c = _chain(getter, entry, token_id)
    return len(c) if c is not None else None


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


def _fork(getter, a, b, token_id, timeout=60):
    """(forked, detail): wait for two nodes' copies of a chain to agree; they
    have forked if, at some position, they hold different transactions."""
    t0 = time.time()
    while True:
        x, y = _chain(getter, a, token_id) or [], _chain(getter, b, token_id) or []
        n = min(len(x), len(y))
        diff = [i for i in range(n) if x[i] != y[i]]
        if diff:
            return True, "the chains differ at position {}: {} vs {}".format(
                diff[0], x[diff[0]][:16], y[diff[0]][:16])
        if len(x) == len(y) or time.time() - t0 > timeout:
            return False, "" if len(x) == len(y) else "the chains are {} and {} long".format(len(x), len(y))
        time.sleep(2)


def nft_01(ctx, ci):
    run = steps.CaseRun(ctx, "NFT-01", STEPS["NFT-01"])
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 3:
        return SKIP, "needs NFT owners and at least 3 receivers", ""
    hops = _hops(ctx)
    ch.ensure_quorums(ctx)      # FT-01 may have restarted a shared quorum; re-assert its setup
    quorums = list(ctx.quorum_hosts or [])
    fam = dict((o["did"], {"owner": o}) for o in owners)

    def each(st, fn):
        """fn(family, record) for every owner at once; record(ok, msg, secs, txid, to)."""
        lock = threading.Lock()

        def record(*a, **k):
            with lock:
                st.op(*a, **k)
        ch.run_parallel(lambda f: fn(f, record), list(fam.values()))

    def timed(fn, *a, **k):
        t0 = time.time()
        ok, msg, res = fn(*a, **k)
        return ok, msg, res, round(time.time() - t0, 2), \
            (res.get("transactionID") if isinstance(res, dict) else None)

    def create(f, record):
        o, f["ids"] = f["owner"], []
        for _k in range(2):
            ok, msg, nft_id, secs, _t = timed(rc.create_nft, o["host"], o["did"], _random_bytes(),
                                              _random_bytes())
            record(ok and isinstance(nft_id, str), msg, secs)
            if not ok or not isinstance(nft_id, str):
                return
            ok, msg, _r, secs, txid = timed(rc.nft_transaction, o["host"], o["did"], o["did"], nft_id,
                                            data="NFT-01 deploy", timeout=ch.TRANSFER_TIMEOUT)
            record(ok, msg, secs, txid, to=o["did"])
            if ok:
                f["ids"].append(nft_id)
    run.step("S1", lambda st: each(st, create), nodes=owners + quorums, must_accept=True)
    fam = dict((d, f) for d, f in fam.items() if len(f.get("ids", [])) == 2)
    if not fam:
        return run.verdict()

    def children(f, record):
        o, parent = f["owner"], f["ids"][0]
        ok, msg, res, secs, _t = timed(rc.mint_nft_children, o["host"], o["did"], parent, 3,
                                       timeout=ch.TRANSFER_TIMEOUT)
        record(ok, msg, secs)
        f["children"] = len(rc.minted_children(res)) if ok else 0

    def children_listed(st):
        each(st, children)
        for f in fam.values():
            if f.get("children"):
                ok, kids, _n = rc.get_nft_children(f["owner"]["host"], f["ids"][0])
                st.check(ok and len(kids) >= f["children"], "{} lists {} children, {} were minted".format(
                    f["ids"][0][:10], len(kids) if ok else "?", f["children"]))
    run.step("S2", children_listed, nodes=owners + quorums, must_accept=True)

    def executes(f, record):
        o, parent = f["owner"], f["ids"][0]
        f["len0"] = _chain_len(rc.get_nft_chain, o, parent) or 0
        for _k in range(3):
            ok, msg, _r, secs, txid = timed(rc.nft_transaction, o["host"], o["did"], o["did"], parent,
                                            data="NFT-01 execute", timeout=ch.TRANSFER_TIMEOUT)
            record(ok, msg, secs, txid, to=o["did"])

    def owner_executes(st):
        each(st, executes)
        for f in fam.values():
            n = _chain_len(rc.get_nft_chain, f["owner"], f["ids"][0]) or 0
            st.check(n >= f["len0"] + 3, "{}'s chain grew {} for 3 executes".format(f["ids"][0][:10],
                                                                                  n - f["len0"]))
    run.step("S3", owner_executes, nodes=owners + quorums, must_accept=True)

    subs = []
    for i, f in enumerate(fam.values()):
        f["sub"] = next((r for k in range(len(recv))
                         for r in [recv[(i * hops + hops + k) % len(recv)]]
                         if ch.machine(r["host"]) != ch.machine(f["owner"]["host"])), recv[0])
        subs.append(f["sub"])

    def subscribe(f, record):
        sub, o, parent = f["sub"], f["owner"], f["ids"][0]
        ok, msg = rc.subscribe_nft(sub["host"], parent)
        record(ok, msg, None)
        if not ok:
            return
        synced, lag = _caught_up(rc.get_nft_chain, sub, o, parent)
        f["lag"] = lag
        if synced:
            ok, msg, _r, secs, txid = timed(rc.nft_transaction, sub["host"], sub["did"], sub["did"],
                                            parent, data="NFT-01 subscriber execute",
                                            timeout=ch.TRANSFER_TIMEOUT)
            record(ok, msg, secs, txid, to=sub["did"])
            f["subscribed"] = ok
        else:
            f["behind"] = lag

    def subscriber(st):
        each(st, subscribe)
        for f in fam.values():
            st.check("behind" not in f, "the subscriber's copy of {} was still behind after {:.0f}s".format(
                f["ids"][0][:10], f.get("behind", 0)))
            if f.get("subscribed"):
                forked, why = _fork(rc.get_nft_chain, f["owner"], f["sub"], f["ids"][0])
                st.check(not forked and not why, "{}: {}".format(f["ids"][0][:10], why or "forked"))
    run.step("S4", subscriber, nodes=owners + subs + quorums, must_accept=True)

    def race(st):
        racing = [f for f in fam.values() if f.get("subscribed")]
        for f in racing:
            _caught_up(rc.get_nft_chain, f["sub"], f["owner"], f["ids"][0])
        jobs = []
        for f in racing:
            for who in (f["owner"], f["sub"]):
                jobs.append({"host": who["host"], "did": who["did"], "to": who["did"],
                             "nft": [{"nftId": f["ids"][0], "value": 1.0, "data": "NFT-01 race"}],
                             "memo": "NFT-01 race"})
        st.add(ch.burst(jobs), jobs)
        for f in racing:
            forked, why = _fork(rc.get_nft_chain, f["owner"], f["sub"], f["ids"][0])
            st.check(not forked, "NFT {} FORKED: {}".format(f["ids"][0][:10], why))
            if why and not forked:
                st.note("{}: {}".format(f["ids"][0][:10], why))
    run.step("S5", race, nodes=owners + subs + quorums,
             require=[(any(f.get("subscribed") for f in fam.values()), "no subscriber executed in S4")])

    def move(f, record):
        o, mover = f["owner"], f["ids"][1]
        holder, moved = o, 0
        for k in range(hops):
            nxt = recv[(list(fam).index(o["did"]) * hops + k) % len(recv)]
            if nxt["did"] == holder["did"]:
                continue
            ok, msg, _r, secs, txid = timed(rc.nft_transaction, holder["host"], holder["did"], nxt["did"],
                                            mover, data="NFT-01 transfer", transfer_ownership=True,
                                            timeout=ch.TRANSFER_TIMEOUT)
            record(ok, msg, secs, txid, to=nxt["did"])
            if not ok:
                break
            arrived, lag = _poll(lambda: _holds_nft(nxt, mover))
            holder, moved = nxt, moved + 1
            if not arrived:
                f["lost"] = (mover, lag)
                break
        f["holder"], f["moved"] = holder, moved

    def new_owners(st):
        each(st, move)
        for f in fam.values():
            if "lost" in f:
                st.check(False, "NFT {} not listed by its new owner {:.0f}s after the transfer".format(
                    f["lost"][0][:10], f["lost"][1]))
    run.step("S6", new_owners, nodes=owners + list(recv) + quorums, must_accept=True)

    def again(st):
        for f in [f for f in fam.values() if f.get("moved")]:
            o, mover, holder = f["owner"], f["ids"][1], f["holder"]
            other = next(r for r in recv if r["did"] not in (o["did"], holder["did"]))
            t0 = time.time()
            ok, msg, res = rc.nft_transaction(o["host"], o["did"], other["did"], mover,
                                              data="NFT-01 former owner", transfer_ownership=True,
                                              timeout=ch.TRANSFER_TIMEOUT)
            st.op(ok, msg, round(time.time() - t0, 2),
                  res.get("transactionID") if isinstance(res, dict) else None, to=other["did"])
            time.sleep(ch.SETTLE)
            st.check(_holds_nft(holder, mover), "NFT {} is no longer with its last owner after the "
                                                "former owner's attempt".format(mover[:10]))
    run.step("S7", again, nodes=owners + list(recv) + quorums, expect_refusal=True,
             require=[(any(f.get("moved") for f in fam.values()), "no NFT changed owner in S6")])

    def reconcile(st):
        for f in fam.values():
            o = f["owner"]
            if f.get("children"):
                ok, kids, _n = rc.get_nft_children(o["host"], f["ids"][0])
                st.check(ok and len(kids) >= f["children"], "{} lists {} children, {} minted".format(
                    f["ids"][0][:10], len(kids) if ok else "?", f["children"]))
            if f.get("subscribed"):
                forked, why = _fork(rc.get_nft_chain, o, f["sub"], f["ids"][0])
                st.check(not forked and not why, "{}: {}".format(f["ids"][0][:10], why or "forked"))
            if f.get("moved"):
                st.check(_holds_nft(f["holder"], f["ids"][1]), "NFT {} is not with its last owner".format(
                    f["ids"][1][:10]))
                st.check(not _holds_nft(o, f["ids"][1]), "NFT {} still shows with its first owner".format(
                    f["ids"][1][:10]))
    run.step("S8", reconcile, nodes=[], watch=False)
    return run.verdict(passed="{} NFT families consistent: children, one chain across nodes, one "
                              "owner each".format(len(fam)))


# ---------------------------------------------------------------------------
# SC-01
# ---------------------------------------------------------------------------

def sc_01(ctx, ci):
    run = steps.CaseRun(ctx, "SC-01", STEPS["SC-01"])
    owners, recv = ctx.senders[:OWNERS], ctx.receivers
    if not owners or len(recv) < 4:
        return SKIP, "needs contract owners and at least 4 subscribers", ""
    per_owner = max(1, int(round(2 * ctx.args.scale)))
    ch.ensure_quorums(ctx)      # FT-01 may have restarted a shared quorum; re-assert its setup
    quorums = list(ctx.quorum_hosts or [])
    contracts = []
    lock = threading.Lock()

    def record(st):
        def fn(*a, **k):
            with lock:
                st.op(*a, **k)
        return fn

    def deploy(st):
        rec = record(st)

        def one(job):
            i, c = job
            owner = owners[i]
            out = {"owner": owner, "subs": [recv[((i * per_owner + c) * 4 + j) % len(recv)] for j in range(4)],
                   "joined": [], "last": owner}
            sc_id, err = ch._sc_new_contract(ctx, owner)
            if not sc_id:
                rec(False, "generate failed: {}".format(err), None)
                return None
            t0 = time.time()
            ok, msg, res = rc.sc_transaction(owner["host"], owner["did"], sc_id, data="SC-01 deploy",
                                             timeout=ch.TRANSFER_TIMEOUT)
            rec(ok, msg, round(time.time() - t0, 2), res.get("transactionID") if isinstance(res, dict) else None)
            if not ok:
                return None
            out["sc"] = sc_id
            return out
        for out in ch.run_parallel(one, [(i, c) for i in range(len(owners)) for c in range(per_owner)]):
            if out:
                contracts.append(out)
        for c in contracts:
            st.check(bool(_chain_len(rc.get_sc_chain, c["owner"], c["sc"])),
                     "contract {} has no chain on its owner".format(c["sc"][:10]))
    run.step("S1", deploy, nodes=owners + quorums, must_accept=True)
    if not contracts:
        return run.verdict()
    sub_entries = [s for c in contracts for s in c["subs"]]

    def grow(st):
        rec = record(st)

        def execute(c, entry, data):
            if entry is not c["last"]:
                synced, lag = _caught_up(rc.get_sc_chain, entry, c["last"], c["sc"])
                if not synced:
                    with lock:
                        st.check(False, "{}'s copy of contract {} was still behind after {:.0f}s".format(
                            entry["did"][-6:], c["sc"][:10], lag))
                    return
            t0 = time.time()
            ok, msg, res = rc.sc_transaction(entry["host"], entry["did"], c["sc"], data=data,
                                             timeout=ch.TRANSFER_TIMEOUT)
            rec(ok, msg, round(time.time() - t0, 2), res.get("transactionID") if isinstance(res, dict) else None)
            if ok:
                c["last"] = entry

        def join(c, entry):
            ok, msg = rc.subscribe_smart_contract(entry["host"], c["sc"])
            rec(ok, msg, None)
            if ok:
                c["joined"].append(entry)

        def one(c):
            s = c["subs"]
            join(c, s[0])
            execute(c, c["owner"], "SC-01 owner 1")
            execute(c, c["owner"], "SC-01 owner 2")
            join(c, s[1])
            if s[0] in c["joined"]:
                execute(c, s[0], "SC-01 subscriber 1")
            join(c, s[2])
            execute(c, c["owner"], "SC-01 owner 3")
            if s[1] in c["joined"]:
                execute(c, s[1], "SC-01 subscriber 2")
            join(c, s[3])
        ch.run_parallel(one, contracts)
    run.step("S2", grow, nodes=owners + sub_entries + quorums, must_accept=True)

    def race(st):
        jobs, racing = [], []
        for c in contracts:
            pair = [s for s in c["joined"][:2]]
            if len(pair) < 2:
                continue
            if all(_caught_up(rc.get_sc_chain, s, c["owner"], c["sc"])[0] for s in pair):
                racing.append((c, pair))
                for s in pair:
                    jobs.append({"host": s["host"], "did": s["did"], "to": "",
                                 "smart_contract": [{"smartContractId": c["sc"], "value": 1.0,
                                                     "data": "SC-01 race"}], "memo": "SC-01 race"})
        st.add(ch.burst(jobs), jobs)
        for c, pair in racing:
            for s in pair:
                forked, why = _fork(rc.get_sc_chain, c["owner"], s, c["sc"])
                st.check(not forked, "contract {} FORKED: {}".format(c["sc"][:10], why))
    run.step("S3", race, nodes=owners + sub_entries + quorums,
             require=[(any(len(c["joined"]) >= 2 for c in contracts), "no contract has two subscribers")])

    def reconcile(st):
        for c in contracts:
            for s in c["joined"]:
                forked, why = _fork(rc.get_sc_chain, c["owner"], s, c["sc"])
                st.check(not forked and not why, "contract {} on {}: {}".format(
                    c["sc"][:10], s["did"][-6:], why or "forked"))
    run.step("S4", reconcile, nodes=[], watch=False)
    return run.verdict(passed="{} contracts: every subscriber's chain matches the owner's".format(
        len(contracts)))


CASES = {"FT-01": ft_01, "NFT-01": nft_01, "SC-01": sc_01}
ORDER = ["FT-01", "NFT-01", "SC-01"]
NEEDS = {
    "assets": {"cases": ["FT-01", "NFT-01", "SC-01"], "senders": OWNERS,
               "receivers": RECEIVERS, "quorums": 2, "fund": 30, "quorum_fund": 200},
}
# Stock builders and capacity measures: a delayed rerun would build the stock twice.
NO_DELAY_RERUN = set(ORDER)
