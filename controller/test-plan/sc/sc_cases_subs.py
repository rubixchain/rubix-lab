#!/usr/bin/env python3
"""
sc_cases_subs.py - subscription at scale, and execution from parts wallets.

Imported by sc_cases.py.

WHY MORE SUBSCRIPTION CASES
    SC-S-01..05 use one contract and four subscribers. That answered the
    question "do subscribers converge?" for a single small case. These push on
    the parts of it a small case cannot reach:

      * MANY contracts and MANY subscribers at once, which is what a real
        network looks like
      * comparing FULL TOKEN DETAIL, not just chain length. Two nodes can agree
        on how many entries a chain has and disagree about what is in them
      * subscribing DURING an execute, not between them
      * depth 20, where a back-fill that fetches "recent" entries runs out

    The path being probed: SubsribeContractSetup only back-fills when the
    contract folder is absent locally, and syncSmartContractTransaction returns
    silently when the metadata carries no PeerID. Either way subscribe reports
    SUCCESS - so an incomplete subscriber looks exactly like a healthy one.
"""

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015

# Shared by the SC-S-06/07 pair: 06 builds the fixture, 07 inspects it.
_MATRIX = {"contracts": [], "subscribers": []}


def _link():
    import sc_cases
    return sc_cases


def _chain_len(host, sc_id, port):
    ok, chain, _ = rc.get_sc_chain(host, sc_id, port)
    return len(chain) if ok else -1


# ---------------------------------------------------------------------------
# SC-S-06
# ---------------------------------------------------------------------------

def sc_s_06(ctx, ci):
    """
    SC-S-06 - Many contracts, many subscribers, joining at many depths.

    WHAT IT CHECKS
        Three contracts are deployed and executed repeatedly. Subscribers join
        at staggered points - before deploy, right after, after 1, 3, 5 and 10
        executes - spread across every spare host in the lane. At the end every
        subscriber's chain length is compared against the owner's.

    WHY IT MATTERS
        SC-S-01..05 proved convergence for one contract and four subscribers.
        This is the same question at the scale the fleet allows, and scale
        changes two things: several contracts are syncing at once (so a
        back-fill can fetch the wrong one), and a subscriber joining at depth
        10 needs far more history than one joining at depth 1.

        A partial back-fill that looked fine at depth 3 has somewhere to hide
        at depth 1 and nowhere at depth 10.

    MANUAL STEPS
        Deploy 3 contracts. Between executes, subscribe a different node to
        each. Then for every (node, contract) pair:
          curl -s http://$NODE:20000/rubix/v1/smart_contracts/<SC>/chain
        and compare lengths against the deployer's.

    PASS / FAIL
        PASS  every subscriber matches the owner on every contract it joined
        FAIL  the report names node, contract and the depth it joined at, which
              together say whether lateness or contract count is the factor
        SKIP  fewer than 4 spare hosts in this lane
    """
    sc = _link()
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 4:
        return SKIP, "not enough hosts", (
            "lane has {} spare host(s); this case needs at least 4 to stagger "
            "subscribers meaningfully".format(len(spare)))

    ready, why = sc._prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    n_contracts = min(3, max(1, len(spare) // 2))
    contracts = []
    for i in range(n_contracts):
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        contracts.append(sc_id)

    # A node that subscribes BEFORE anything is deployed.
    early = spare[0]
    for sc_id in contracts:
        rc.subscribe_smart_contract(early["host"], sc_id, ctx.port)
    _MATRIX["subscribers"] = [("before deploy", early, 0)]
    time.sleep(2)

    for sc_id in contracts:
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=sc.rand_value(0.010, 0.200),
                                       data="matrix deploy", port=ctx.port)
        if not ok:
            return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    # Stagger the rest: subscribe, execute a few times, subscribe the next.
    schedule = [(1, "after deploy"), (1, "after 1 execute"),
                (2, "after 3 executes"), (2, "after 5 executes"),
                (5, "after 10 executes")]
    idx = 1
    for executes, label in schedule:
        for _ in range(executes):
            for sc_id in contracts:
                rc.sc_transaction(s["host"], s["did"], sc_id,
                                  value=sc.rand_value(0.010, 0.100),
                                  data="matrix execute", port=ctx.port)
            time.sleep(1.5)
        if idx >= len(spare):
            break
        node = spare[idx]
        idx += 1
        for sc_id in contracts:
            rc.subscribe_smart_contract(node["host"], sc_id, ctx.port)
        depth = _chain_len(s["host"], contracts[0], ctx.port)
        _MATRIX["subscribers"].append((label, node, depth))
        time.sleep(2)

    time.sleep(SETTLE * 2)
    _MATRIX["contracts"] = contracts
    _MATRIX["owner"] = s

    problems, rows = [], []
    for sc_id in contracts:
        owner_len = _chain_len(s["host"], sc_id, ctx.port)
        for label, node, depth in _MATRIX["subscribers"]:
            got = _chain_len(node["host"], sc_id, ctx.port)
            if got != owner_len:
                problems.append("{} on {} ({}): {} of {} entries".format(
                    sc_id[:10], node["host"], label, got, owner_len))
        rows.append("{}={}".format(sc_id[:8], owner_len))

    return (not problems), "{} contract(s) {} | {} subscriber(s)".format(
        len(contracts), " ".join(rows), len(_MATRIX["subscribers"])), (
        "" if not problems else "; ".join(problems[:6]) +
        (" (+{} more)".format(len(problems) - 6) if len(problems) > 6 else "") +
        " - a subscriber short of the owner joined late and never caught up, "
        "yet is still allowed to execute")


# ---------------------------------------------------------------------------
# SC-S-07
# ---------------------------------------------------------------------------

def sc_s_07(ctx, ci):
    """
    SC-S-07 - Compare full token detail across subscribers, not just chain length.

    WHAT IT CHECKS
        For every contract and every subscriber from SC-S-06, the contract's
        token row is compared field by field: value, status, latest position.

    WHY IT MATTERS
        This is the real answer to "all subscribed nodes should have the same
        data". Chain LENGTH matching is a weaker claim than chain CONTENT
        matching - two nodes can hold the same number of entries and disagree
        about what those entries say, which is precisely what a partial sync
        followed by live pubsub events would produce.

        A node whose token row disagrees is still a legitimate subscriber and
        will still be allowed to execute, because execute is gated on
        subscription rather than on agreement.

    MANUAL STEPS
        On the owner and each subscriber:
          psql -h $NODE -p 5433 -U rubix -d rubix -c \\
            "SELECT token_id, token_value, token_status, latest_position
               FROM tokens WHERE token_id='<SC_ID>';"
        Every node should return identical values.

    PASS / FAIL
        PASS  all subscribers agree with the owner on every field
        FAIL  the report names the node, the contract and the field that differs
        SKIP  SC-S-06 did not run, or psycopg2 unavailable
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    if not _MATRIX.get("contracts"):
        return SKIP, "no subscription fixture", (
            "SC-S-06 did not complete - these two run as a pair in one lane")

    owner = _MATRIX["owner"]

    def detail(host, sc_id):
        rows = db.query(
            host,
            "SELECT token_value, token_status, latest_position FROM tokens "
            "WHERE token_id = %s", (sc_id,))
        if not rows:
            return None
        v, st, pos = rows[0]
        return (round(float(v), 3), int(st), int(pos))

    problems, compared = [], 0
    try:
        for sc_id in _MATRIX["contracts"]:
            ref = detail(owner["host"], sc_id)
            if ref is None:
                problems.append("owner has no token row for {}".format(sc_id[:10]))
                continue
            for label, node, _depth in _MATRIX["subscribers"]:
                got = detail(node["host"], sc_id)
                compared += 1
                if got is None:
                    problems.append("{} ({}) has NO token row for {}".format(
                        node["host"], label, sc_id[:10]))
                elif got != ref:
                    problems.append("{} ({}) {}: value/status/position {} vs owner {}".format(
                        node["host"], label, sc_id[:10], got, ref))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    return (not problems), "{} (node, contract) pair(s) compared".format(compared), (
        "" if not problems else "; ".join(problems[:5]) +
        (" (+{} more)".format(len(problems) - 5) if len(problems) > 5 else "") +
        " - these nodes are subscribed and may execute against a contract they "
        "do not see the same way the owner does")


# ---------------------------------------------------------------------------
# SC-S-08
# ---------------------------------------------------------------------------

def sc_s_08(ctx, ci):
    """
    SC-S-08 - Subscribe while an execute is in flight.

    WHAT IT CHECKS
        A node subscribes at the same moment the owner is executing. Afterwards
        its chain matches the owner's.

    WHY IT MATTERS
        Every other subscription case joins BETWEEN operations, when the
        contract is at rest. Joining mid-execute is the case where back-fill
        and a live pubsub event can both deliver the same entry, or neither
        can: the sync reads a chain that is still being written.

        Duplicate delivery and missed delivery look identical from the API -
        subscribe succeeds either way - so only comparing the end state
        afterwards distinguishes them.

    MANUAL STEPS
        Start an execute and, without waiting for it, immediately subscribe a
        second node. Then compare chains once both have settled.

    PASS / FAIL
        PASS  subscriber's chain matches the owner's
        FAIL  shorter -> the in-flight entry was missed by both paths
        FAIL  longer -> the entry was delivered twice
        SKIP  no spare host
    """
    sc = _link()
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a second node to subscribe"
    other = spare[-1]

    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.200),
                                   data="race deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    # Fire the execute and the subscribe together.
    def do_execute():
        return rc.sc_transaction(s["host"], s["did"], sc_id,
                                 value=sc.rand_value(0.010, 0.100),
                                 data="in-flight execute", port=ctx.port)

    def do_subscribe():
        time.sleep(0.2)   # just after the execute starts, not before it
        return rc.subscribe_smart_contract(other["host"], sc_id, ctx.port)

    with ThreadPoolExecutor(max_workers=2) as pool:
        fx = pool.submit(do_execute)
        fs = pool.submit(do_subscribe)
        ex_ok = fx.result()[0]
        sub_ok = fs.result()[0]

    if not ex_ok:
        return SKIP, "execute failed", "cannot judge the race if the execute did not run"
    if not sub_ok:
        return False, "subscribe failed during an execute", (
            "subscribing while the contract was being written was rejected")

    time.sleep(SETTLE * 2)
    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    sub_len = _chain_len(other["host"], sc_id, ctx.port)

    if sub_len == owner_len:
        return True, "owner={} subscriber={}".format(owner_len, sub_len), ""
    direction = "missed the in-flight entry" if sub_len < owner_len \
        else "received an entry twice"
    return False, "owner={} subscriber={}".format(owner_len, sub_len), (
        "subscribing mid-execute {} - back-fill and the live event did not "
        "combine correctly".format(direction))


# ---------------------------------------------------------------------------
# SC-S-09
# ---------------------------------------------------------------------------

def sc_s_09(ctx, ci):
    """
    SC-S-09 - Subscribe to a contract with twenty entries of history.

    WHAT IT CHECKS
        After twenty executes, a fresh node subscribes and must receive the
        whole chain.

    WHY IT MATTERS
        Depth is the variable that separates "back-fill is missing" from
        "back-fill is incomplete". A sync that fetches only the most recent
        entries passes at depth 1, probably passes at depth 3, and cannot pass
        at depth 20. The SIZE of the gap is the diagnosis: one missing entry is
        a delivery problem, nineteen is no back-fill at all.

    MANUAL STEPS
        Deploy, execute twenty times, then subscribe a fresh node and compare
        chain lengths.

    PASS / FAIL
        PASS  chain matches the owner's
        FAIL  the gap size tells you whether back-fill fetched nothing, a
              window, or everything but the tail
        SKIP  no spare host
    """
    sc = _link()
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a node that has not yet subscribed"
    other = spare[0]

    ready, why = sc._prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.200),
                                   data="deep deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    for i in range(20):
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=sc.rand_value(0.005, 0.050),
                                       data="depth {}".format(i + 1), port=ctx.port)
        if not ok:
            return SKIP, "could not build depth", (
                "execute {} failed: {}".format(i + 1, msg))
        time.sleep(1)
    time.sleep(SETTLE)

    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    ok, msg = rc.subscribe_smart_contract(other["host"], sc_id, ctx.port)
    if not ok:
        return False, "subscribe failed", str(msg)
    time.sleep(SETTLE * 2)
    sub_len = _chain_len(other["host"], sc_id, ctx.port)

    if sub_len == owner_len:
        return True, "owner={} late subscriber={}".format(owner_len, sub_len), ""
    gap = owner_len - sub_len
    if sub_len <= 1:
        why_note = "back-fill fetched nothing at all"
    elif gap <= 2:
        why_note = "back-fill fetched almost everything but missed the tail"
    else:
        why_note = "back-fill fetched only a window of recent entries"
    return False, "owner={} late subscriber={}".format(owner_len, sub_len), (
        "missing {} of {} entries - {}".format(gap, owner_len, why_note))


# ---------------------------------------------------------------------------
# SC-S-10
# ---------------------------------------------------------------------------

def sc_s_10(ctx, ci):
    """
    SC-S-10 - A later subscriber must see a previous subscriber's execute.

    WHAT IT CHECKS
        Node A subscribes and executes. Node B then subscribes and must receive
        A's execute, not only the owner's entries.

    WHY IT MATTERS
        Every other case builds history from the OWNER. This builds it from a
        subscriber. If back-fill syncs from the deploying node's copy of the
        chain, an entry written by a different subscriber could be missing from
        whatever B receives - and nothing about the subscribe call would say
        so.

        It also confirms the chain is genuinely shared state rather than
        owner-authored state that others merely observe.

    MANUAL STEPS
        1. Subscribe node A, execute from A.
        2. Subscribe node B.
        3. Compare B's chain against the owner's and against A's.

    PASS / FAIL
        PASS  all three agree
        FAIL  B is short by exactly A's execute -> back-fill only carries
              owner-authored history
        SKIP  fewer than 2 spare hosts
    """
    sc = _link()
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 2:
        return SKIP, "need 2 spare hosts", (
            "lane has {}; this case needs an executing subscriber and a later "
            "one".format(len(spare)))
    a, b = spare[0], spare[1]

    ready, why = sc._prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.200),
                                   data="shared-history deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    ok, msg = rc.subscribe_smart_contract(a["host"], sc_id, ctx.port)
    if not ok:
        return SKIP, "subscribe A failed", str(msg)
    time.sleep(SETTLE)

    ready, why = sc._prepare(ctx, a, 2)
    if not ready:
        return SKIP, "setup incomplete for A", why
    ok, msg, _ = rc.sc_transaction(a["host"], a["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.100),
                                   data="subscriber execute", port=ctx.port)
    if not ok:
        return SKIP, "A could not execute", str(msg)
    time.sleep(SETTLE)

    owner_len = _chain_len(s["host"], sc_id, ctx.port)
    a_len = _chain_len(a["host"], sc_id, ctx.port)

    ok, msg = rc.subscribe_smart_contract(b["host"], sc_id, ctx.port)
    if not ok:
        return False, "subscribe B failed", str(msg)
    time.sleep(SETTLE * 2)
    b_len = _chain_len(b["host"], sc_id, ctx.port)

    agree = owner_len == a_len == b_len
    return agree, "owner={} executor={} late={}".format(owner_len, a_len, b_len), (
        "" if agree else
        "the later subscriber holds {} entries against the owner's {} - an "
        "execute written by another SUBSCRIBER did not reach it, so back-fill "
        "carries owner-authored history only".format(b_len, owner_len))


# ---------------------------------------------------------------------------
# SC-C-23 / 24 / 25 - executing from parts wallets
# ---------------------------------------------------------------------------

def _parts_wallet(ctx, sc, target, amounts):
    """Leave `target` holding only the given fractional amounts.

    BUILDS the shape rather than requiring it. Earlier this refused whenever the
    target already held whole tokens, which on a funded fleet is always - so
    every case using it skipped and the parts path went untested.
    """
    s = ctx.senders[0]
    ready, why = sc._prepare(ctx, s, sum(amounts) + 12)
    if not ready:
        return False, why
    return ws.make_parts_wallet(ctx, target, s, amounts=tuple(amounts))


def sc_c_23(ctx, ci):
    """
    SC-C-23 - A parts-only wallet executes several contracts in succession.

    WHAT IT CHECKS
        A wallet holding only fractional RBT subscribes to three contracts and
        executes each. All succeed, nothing is charged, the counter stays
        consistent.

    WHY IT MATTERS
        SC-C-09 proves one execute works from parts. Repeating it is what
        catches state that degrades: each execute still has to take part in
        consensus from a wallet with no whole token, and if anything about that
        leaves the wallet slightly worse the third attempt is where it shows.

    MANUAL STEPS
        Build a parts wallet (0.4/0.3/0.5), subscribe it to three deployed
        contracts, execute each in turn, checking the balance and denom
        listing after each.

    PASS / FAIL
        PASS  all three execute, balance unchanged, counter consistent
        FAIL  a LATER execute fails while an earlier one succeeded -> the act
              of executing is degrading the wallet
        SKIP  no parts wallet could be built
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a receiver to hold the parts wallet"
    w = spare[0]

    ready, why = sc._prepare(ctx, s, 12)
    if not ready:
        return SKIP, "setup incomplete", why

    contracts = []
    for i in range(3):
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                       value=sc.rand_value(0.010, 0.200),
                                       data="multi deploy", port=ctx.port)
        if not ok:
            return SKIP, "deploy failed", str(msg)
        contracts.append(sc_id)
    time.sleep(SETTLE)

    ok, why = _parts_wallet(ctx, sc, w, [0.4, 0.3, 0.5])
    if not ok:
        return SKIP, "could not build parts wallet", why

    ready, why = sc._prepare(ctx, w, 0.5)
    if not ready:
        return SKIP, "setup incomplete", why

    before = sc._bal(ctx, w)
    results, problems = [], []
    for i, sc_id in enumerate(contracts, 1):
        sub_ok, msg = rc.subscribe_smart_contract(w["host"], sc_id, ctx.port)
        if not sub_ok:
            problems.append("execute {}: subscribe failed ({})".format(i, msg))
            break
        time.sleep(SETTLE)
        ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id,
                                       value=sc.rand_value(0.010, 0.100),
                                       data="parts execute {}".format(i),
                                       port=ctx.port)
        results.append("{}:{}".format(i, "ok" if ok else "FAIL"))
        if not ok:
            problems.append("execute {} of 3 rejected: {} - earlier executes "
                            "succeeded, so the wallet degraded".format(i, msg))
            break
        time.sleep(2)

    time.sleep(SETTLE)
    after = sc._bal(ctx, w)
    try:
        drift = db.denom_drift(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if before and after and not rc.close_enough(before["balance"], after["balance"],
                                                tol=TOL * 4):
        problems.append("executor balance moved {:.4f} -> {:.4f}; execute takes "
                        "no collateral".format(before["balance"], after["balance"]))
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "executes {}".format(" ".join(results)), "; ".join(problems)


def sc_c_24(ctx, ci):
    """
    SC-C-24 - Execute from a wallet holding only minimum-unit tokens.

    WHAT IT CHECKS
        A wallet whose entire balance is 0.001 tokens can still take part in
        consensus and execute a contract.

    WHY IT MATTERS
        The extreme of the parts case. Selection has to gather many rows to
        reach any threshold, and the denomination is the smallest the network
        allows - so if there is a lower bound on what can be selected, or a cap
        on how many rows a selection will consider, this is where it appears.

        A wallet like this is not artificial: it is what remains after many
        fractional splits.

    MANUAL STEPS
        Send 0.001 twenty times to a fresh node, confirm via the tokens table
        that it holds only 0.001 rows, subscribe it, and execute.

    PASS / FAIL
        PASS  executes successfully
        FAIL  rejected while the balance is adequate -> a floor on selection
        SKIP  no clean wallet available
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 2:
        return SKIP, "need a clean host", "no spare wallet for the minimum-unit case"
    w = spare[1]

    ready, why = sc._prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.200),
                                   data="min-unit deploy", port=ctx.port)
    if not ok:
        return SKIP, "deploy failed", str(msg)
    time.sleep(SETTLE)

    ok, why = _parts_wallet(ctx, sc, w, [0.001] * 20)
    if not ok:
        return SKIP, "could not build minimum-unit wallet", why

    try:
        values = db.free_token_values(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if any(v > 0.0015 for v in values):
        return SKIP, "wallet not minimum-unit only", (
            "holds {} row(s) above 0.001, so this is not the extreme "
            "case".format(sum(1 for v in values if v > 0.0015)))

    ready, why = sc._prepare(ctx, w, 0.002)
    if not ready:
        return SKIP, "setup incomplete", why

    sub_ok, msg = rc.subscribe_smart_contract(w["host"], sc_id, ctx.port)
    if not sub_ok:
        return SKIP, "subscribe failed", str(msg)
    time.sleep(SETTLE)

    ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id, value=0.001,
                                   data="min-unit execute", port=ctx.port)
    return bool(ok), "{} row(s) of 0.001, execute {}".format(
        len(values), "ok" if ok else "rejected"), (
        "" if ok else "a wallet of {} minimum-unit tokens could not execute: {} "
        "- suggests a floor on what selection will assemble".format(len(values), msg))


def sc_c_25(ctx, ci):
    """
    SC-C-25 - A parts-only wallet DEPLOYS, then executes its own contract.

    WHAT IT CHECKS
        A wallet with no whole token deploys a contract with a value (taking
        collateral from parts), then executes it.

    WHY IT MATTERS
        SC-C-15 deploys from parts; SC-C-09 executes from parts. This does both
        in sequence on one wallet, which is the case where the deploy's
        collateral changes what the later execute has to work with.

        Deploying from parts consumes some of them and returns change, so the
        wallet's shape afterwards is different from the one it started with.
        If the change is wrong, the execute is the operation that discovers it.

    MANUAL STEPS
        Build a parts wallet, deploy a small fractional contract from it, then
        execute that same contract from the same wallet.

    PASS / FAIL
        PASS  deploy costs its value, execute then succeeds
        FAIL  deploy succeeds but the execute fails -> the deploy left the
              wallet in a state it cannot transact from
        SKIP  no parts wallet could be built
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if not spare:
        return SKIP, "no spare host", "need a wallet for the parts deploy"
    w = spare[-1]

    ok, why = _parts_wallet(ctx, sc, w, [0.7, 0.6, 0.5, 0.4, 0.3])
    if not ok:
        return SKIP, "could not build parts wallet", why

    ready, why = sc._prepare(ctx, w, 1.5)
    if not ready:
        return SKIP, "setup incomplete", why

    value = sc.rand_value(0.050, 0.300)
    spent, sc_id, err = sc._deploy_and_measure(ctx, w, value)
    if err:
        return False, "deploy from parts failed", err

    exact = rc.close_enough(spent, value, tol=sc.cost_tolerance(value))
    time.sleep(SETTLE)

    ok, msg, _ = rc.sc_transaction(w["host"], w["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.050),
                                   data="execute own contract", port=ctx.port)
    try:
        drift = db.denom_drift(w["host"], w["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not exact:
        problems.append("deploy cost {:.4f}, expected {:.3f}".format(spent, value))
    if not ok:
        problems.append("deploy succeeded but the follow-up execute was "
                        "rejected: {} - the deploy left this wallet unable to "
                        "transact".format(msg))
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "deployed {:.3f} from parts (spent {:.4f}), execute {}".format(
        value, spent, "ok" if ok else "FAILED"), "; ".join(problems)
