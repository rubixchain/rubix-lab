#!/usr/bin/env python3
"""
validate_cases.py - run every case against a FAKE fleet, offline.

WHY THIS EXISTS
    SC-C-01 and SC-C-02 reached the real fleet and died instantly with
        TypeError: string indices must be integers, not 'str'
    because they called ctx.quorum_for(host) when the runner's quorum_for
    expects the entry DICT. A one-line mistake, but it cost a full setup cycle
    (~60s of minting) to discover, and it would have been caught by calling the
    function even once.

    Checking that a module imports and that its docstrings are well-formed
    proves nothing about whether the code runs. This calls every case.

WHAT IT DOES
    Builds a CaseContext with the same shape test_runner builds, stubs every
    rubix_client and db_client function so nothing touches the network or a
    database, then invokes each case and reports anything that raises.

WHAT IT CATCHES
    Wrong argument shapes, misspelled keys, bad unpacking, calls to helpers
    that do not exist, wrong return arity - the whole class of "this was never
    executed" bug.

WHAT IT DOES NOT CATCH
    Whether a case's LOGIC is right. Every stub returns success, so a case that
    asserts the wrong thing still "passes" here. This is a smoke test for
    shape, not a substitute for a real run.

USAGE
    python3 validate_cases.py                   # every case module
    python3 validate_cases.py --cases master    # one module
    python3 validate_cases.py --verbose         # show each case's return value

Exit code is non-zero if any case raised, so this can gate a run:
    python3 validate_cases.py && python3 test_runner.py
"""

import argparse
import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rubix_client as rc
import db_client as db
import wallet_shapes as ws
import test_runner as tr
from test_runner import CaseContext, CaseInfo, load_case_module


class _Args:
    """Stands in for argparse's namespace. Mirrors test_runner's defaults."""
    port = 20000
    rbt_amount = 1
    ft_count = 10
    ft_token_count = 1
    quorum_floor = 100
    # Scale knobs kept tiny so offline validation stays instant; every code
    # path still runs.
    repeat_count = 3
    value_ceiling = 10
    wallet_ceiling = 100
    tiny_tokens = 3
    chain_hops = 3
    burst_count = 3
    callback_host = "192.168.1.103"
    ssh_user = "rubix"
    remote_dir = "~/Desktop/rubix"
    # Stress cases scale their volume by this; 0 keeps the offline
    # validation instant while still executing every code path.
    scale = 0.01


def _entry(n):
    # .121 stands in for a host tagged 'multidid' in hosts.txt, so the
    # intra-node cases execute their body instead of skipping.
    return {"host": "192.168.1.{}".format(n),
            "did": "bafybmi{}".format(str(n) * 7),
            "dids": ["bafybmi{}".format(str(n) * 7)],
            "role": "multidid" if n == 121 else ""}


def build_ctx(n_quorum=3, n_pairs=6):
    """A CaseContext shaped exactly like the one test_runner passes in.

    Six pairs, not two: several cases index ctx.pair(3) or need
    ctx.receivers[3], and a short context would make them SKIP rather than
    exercise the code being validated.
    """
    quorum_hosts = [_entry(200 + i) for i in range(n_quorum)]
    senders = [_entry(104 + i) for i in range(n_pairs)]
    receivers = [_entry(120 + i) for i in range(n_pairs)]
    sender_quorum = {s["host"]: quorum_hosts[i % n_quorum]
                     for i, s in enumerate(senders)}
    # Receivers also send in some cases, so they need a quorum mapping too.
    sender_quorum.update({r["host"]: quorum_hosts[i % n_quorum]
                          for i, r in enumerate(receivers)})
    # `fleet` must be populated or the fleet-wide sweeps take their fallback
    # branch and validate a shape the real runner never passes them.
    return CaseContext(_Args.port, quorum_hosts, senders, receivers,
                       sender_quorum, _Args(),
                       fleet=quorum_hosts + senders + receivers)


# ---------------------------------------------------------------------------
# Stubs. Every one returns the SUCCESS shape, so a case runs its full happy
# path rather than bailing out early - the point is to execute as many lines
# as possible, not to simulate failures.
# ---------------------------------------------------------------------------

def install_stubs():
    calls = []

    def rec(name, ret):
        def fn(*a, **k):
            calls.append(name)
            return ret
        return fn

    balance = {"balance": 500.0, "locked": 0.0, "pledged": 0.0}

    rc.get_rbt_balance_detail = rec("get_rbt_balance_detail", (True, dict(balance), ""))
    rc.get_rbt_balance = rec("get_rbt_balance", (True, 500.0, ""))
    rc.quorum_add = rec("quorum_add", (True, "added"))
    rc.fund_did = rec("fund_did", (True, "from faucet"))
    rc.faucet_ready = rec("faucet_ready", (True, "faucet holds 2150000.000 RBT free"))
    # First entry is quorum .200, so _signing_quorum resolves to a real
    # quorum_hosts entry the way it does on the fleet.
    rc.get_quorums = rec("get_quorums", (True, ["bafybmi" + "200" * 7], ""))
    rc.quorum_setup = rec("quorum_setup", (True, "quorum set up"))
    rc.quorum_reset = rec("quorum_reset", (True, "quorum list reset"))
    # Real signature returns (ok, balance) - a tuple. Stubbing it as a bare
    # bool hid the fact that callers were treating the tuple as truthy.
    rc.wait_for_balance = rec("wait_for_balance", (True, 500.0))
    rc.initiate_transaction = rec("initiate_transaction", (True, "ok", {}))
    rc.create_smart_contract = rec("create_smart_contract", (True, "ok", "SC" + "a" * 44))
    rc.create_nft = rec("create_nft", (True, "ok", "Qm" + "b" * 44))
    rc.sc_transaction = rec("sc_transaction", (True, "ok", {}))
    rc.nft_transaction = rec("nft_transaction", (True, "ok", {}))
    rc.mint_nft_children = rec("mint_nft_children", (True, "ok", {
        "mintedNFTChildren": [{"parentNFTId": "Qm" + "b" * 44,
                               "childNFTId": "Qm" + "c" * 44}]}))
    rc.minted_children = lambda r: (r or {}).get("mintedNFTChildren") or []
    rc.mint_ft = rec("mint_ft", (True, "ok", {}))
    rc.get_ft_balance = rec("get_ft_balance", (True, [{"name": "x", "count": 10}], ""))
    # Real signature returns (ok, count, raw).
    rc.wait_for_ft_count = rec("wait_for_ft_count", (True, 10, []))
    rc.ft_count_for = rec("ft_count_for", 10)
    rc.subscribe_nft = rec("subscribe_nft", (True, "subscribed"))
    rc.subscribe_smart_contract = rec("subscribe_smart_contract", (True, "subscribed"))
    rc.register_sc_callback = rec("register_sc_callback", (True, "ok", {}))
    # Chains return 2 entries so a "did it grow" check has something to compare.
    rc.get_sc_chain = rec("get_sc_chain", (True, [{"transactionId": "t1"},
                                                  {"transactionId": "t2"}], ""))
    rc.get_nft_chain = rec("get_nft_chain", (True, [{"transactionId": "t1"},
                                                    {"transactionId": "t2"}], ""))
    rc.get_nft_children = rec("get_nft_children", (True, [{"childNFTId": "Qm" + "c" * 44}], ""))
    rc.get_nft_parent = rec("get_nft_parent", (True, {"parentNFTId": "Qm" + "b" * 44}, ""))
    rc.get_nft_balance = rec("get_nft_balance", (True, [{"tokenId": "Qm" + "b" * 44}], ""))
    rc.list_nfts = rec("list_nfts", (True, [{"tokenId": "Qm" + "b" * 44}], ""))
    rc.list_smart_contracts = rec("list_smart_contracts", (True, ["SC" + "a" * 44], ""))
    rc.list_fts = rec("list_fts", (True, [{"ft_name": "x"}], ""))
    rc.get_transactions = rec("get_transactions", (True, [{"id": "t1"}], ""))
    rc.get_dids = rec("get_dids", (True, ["bafybmi" + "z" * 52], ""))
    rc._tx = rec("_tx", (True, "ok", {}))

    db.available = lambda: True
    db.denom_counter = rec("denom_counter", {1.0: 5, 0.5: 2})
    db.real_free_denoms = rec("real_free_denoms", {1.0: 5, 0.5: 2})
    db.denom_drift = rec("denom_drift", {})
    db.value_in_status = rec("value_in_status", 1.0)
    db.count_in_status = rec("count_in_status", 3)
    db.free_token_values = rec("free_token_values", [0.5, 0.3, 0.2])
    db.token_status_summary = rec("token_status_summary", {"Free": (5, 5.0)})
    db.pledged_value = rec("pledged_value", 0.0)
    db.open_pledges = rec("open_pledges", [])
    db.transaction_exists = rec("transaction_exists", True)
    db.transaction_participants = rec("transaction_participants", ("did_a", "did_b"))
    _snap = {"free": 10.0, "free_rows": 12, "committed": 0.0,
             "burnt_for_ft": 2.0, "burnt_for_ft_rows": 3, "pledged": 0.0,
             "denom": {1.0: 5}, "denom_drift": {}}
    db.snapshot = rec("snapshot", dict(_snap))
    db.delta = lambda b, a: {k: a[k] - b[k] for k in
        ("free","free_rows","committed","burnt_for_ft","burnt_for_ft_rows","pledged")}
    db.new_drift = lambda b, a: {}
    db.describe_drift = lambda d: ""
    db.duplicate_token_ids = rec("duplicate_token_ids", [])
    db.token_rows = rec("token_rows", [("tok1", 1.0, 0, None)])
    db.children_of = rec("children_of", [])
    db.chain_rows = rec("chain_rows", [(0, "tx1", "", 1)])
    db.unpledge_rows = rec("unpledge_rows", [])
    db.negative_denoms = rec("negative_denoms", [])
    db.orphan_tokens = rec("orphan_tokens", [])
    # Raw-SQL paths used by the fleet-wide sweeps and the DB-SEED case.
    db.query = rec("query", [])
    db.writable_query = rec("writable_query", [])
    db.denom_counter = rec("denom_counter", {1.0: 5, 0.5: 2})
    db.real_free_denoms = rec("real_free_denoms", {1.0: 5, 0.5: 2})
    db.record = lambda *a, **k: (a[-1] if a else None)
    db.format_evidence = lambda b, a: "free 10.000->9.514 | denom unchanged"
    # wallet_shapes performs real transfers; offline it always succeeds so the
    # case body past the precondition still executes.
    ws.make_parts_wallet = rec("make_parts_wallet", (True, "parts wallet ready"))
    ws.make_mixed_wallet = rec("make_mixed_wallet", (True, "mixed wallet ready"))
    ws.make_minimum_unit_wallet = rec("make_minimum_unit_wallet", (True, "ok"))
    ws.drain_whole_tokens = rec("drain_whole_tokens", (True, "drained"))
    ws.drain_to = rec("drain_to", (True, "drained to budget"))
    ws.describe = rec("describe", "5 row(s) totalling 2.400: 0 whole, 5 part")
    ws.second_did = rec("second_did", "bafybmi" + "s" * 52)
    return calls


import time as _time
_real_sleep = _time.sleep


def _patch_sleep():
    """Cases wait for settle windows; offline there is nothing to wait for."""
    _time.sleep = lambda *a, **k: None


def validate(module_name, verbose=False):
    mod = load_case_module(module_name)
    ctx = build_ctx()
    info = getattr(mod, "CASE_INFO", {})
    failures = []

    print("== {} ({} cases) ==".format(module_name, len(mod.ORDER)))
    for tid in mod.ORDER:
        text = info.get(tid)
        ci = (CaseInfo(tid, case=text[0], expected=text[1])
              if text else CaseInfo(tid, case="(from catalogue)", expected="(from catalogue)"))
        try:
            result = mod.CASES[tid](ctx, ci)
        except Exception as e:
            failures.append((tid, e, traceback.format_exc()))
            print("  [RAISED] {:<12} {}: {}".format(tid, type(e).__name__, e))
            continue

        # Contract: every case returns (passed, actual, note)
        if not (isinstance(result, tuple) and len(result) == 3):
            failures.append((tid, ValueError("bad return"), repr(result)))
            print("  [SHAPE ] {:<12} returned {!r}, expected a 3-tuple".format(tid, result))
            continue
        if verbose:
            print("  [ok    ] {:<12} -> {}".format(tid, result[0]))

    if not failures:
        print("  all {} case(s) executed without raising\n".format(len(mod.ORDER)))
    return failures


def validate_schedule(module_name, n_nodes=28):
    """Drive the real scheduler (test_runner.run_units) over a fake pool.

    Checks what the per-case run cannot: every case is scheduled exactly once,
    two units running at the same time never share a node, and a unit gets the
    number of senders/receivers/quorums its NEEDS asks for. Node .104 holds
    three DIDs, so same-node units have somewhere to go.
    """
    import threading
    mod = load_case_module(module_name)
    pool = [{"host": "192.168.1.{}".format(104 + i),
             "dids": ["bafybmi{:0>52}".format("{}{}".format(i, k)) for k in range(3 if i == 0 else 1)]}
            for i in range(n_nodes)]
    fleet = [{"host": n["host"], "did": d} for n in pool for d in n["dids"]]
    units = tr.build_units(mod, list(mod.ORDER))

    lock = threading.Lock()
    active, overlaps, seen = {}, [], []
    peak = [0]
    unit_of = {t: u for u in units for t in u.cases}
    ended = set()
    ran_ordinary = set()

    def wrap(tid, fn):
        def run(ctx, ci):
            hosts = {e["host"] for e in ctx.senders + ctx.receivers + ctx.quorum_hosts}
            u = unit_of[tid]
            with lock:
                for other, oh in active.items():
                    if hosts & oh:
                        overlaps.append("{} and {} share {}".format(tid, other, sorted(hosts & oh)))
                if u.exclusive and active:
                    overlaps.append("{} is exclusive but ran beside {}".format(tid, sorted(active)))
                if u.spec["last"] and ran_ordinary - ended:
                    overlaps.append("{} is 'last' but started before {} finished".format(
                        tid, sorted(ran_ordinary - ended)))
                if not u.spec["last"]:
                    ran_ordinary.add(tid)
                active[tid] = hosts
                peak[0] = max(peak[0], len(active))
                seen.append(tid)
            try:
                _real_sleep(0.02)       # long enough for units to overlap
                return fn(ctx, ci)
            finally:
                with lock:
                    del active[tid]
                    ended.add(tid)
        return run

    cases_map = {t: wrap(t, f) for t, f in mod.CASES.items()}
    import io, contextlib
    with contextlib.redirect_stdout(io.StringIO()):
        tr.run_units(units, pool, fleet, cases_map, {}, {}, _Args())

    problems = list(overlaps)
    rows = {}
    for u in units:
        s = u.spec
        if u.hosts and not u.wants_all and not s["same_node"]:
            got = (len(u.senders), len(u.receivers), len(u.quorums))
            want = (s["senders"], s["receivers"], s["quorums"])
            if got != want:
                problems.append("{} got s/r/q {} but NEEDS {}".format(u.name, got, want))
        for tid, result, _e, _ci in u.rows:
            rows.setdefault(tid, []).append(result)
    for tid in mod.ORDER:
        if len(rows.get(tid, [])) != 1:
            problems.append("{} has {} result row(s), expected 1".format(tid, len(rows.get(tid, []))))
    skipped = sorted(t for t, r in rows.items()
                     if r and r[0][0] == "SKIP" and r[0][1] == "not enough participants")
    print("== schedule over a fake pool of {} nodes ==".format(n_nodes))
    print("  {} unit(s); at most {} running at once".format(len(units), peak[0]))
    if skipped:
        print("  not schedulable on {} nodes: {}".format(n_nodes, ", ".join(skipped)))
    for p_ in problems:
        print("  [PROBLEM] " + p_)
    if not problems:
        print("  every case scheduled once; no shared nodes; exclusive units ran alone; "
              "'last' units started after the rest finished")
        print()
    return problems


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases", default="", help="one module, e.g. sc (default: all)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    install_stubs()
    _patch_sleep()

    if args.cases:
        modules = [args.cases]
    else:
        base = os.path.join(HERE, "..")
        modules = sorted(
            d for d in os.listdir(base)
            if os.path.isfile(os.path.join(base, d, "{}_cases.py".format(d))))

    if not modules:
        sys.exit("no case modules found")

    all_failures = []
    schedule_problems = []
    for m in modules:
        all_failures += [(m,) + f for f in validate(m, args.verbose)]
        if hasattr(load_case_module(m), "NEEDS"):
            schedule_problems += validate_schedule(m)
    if schedule_problems:
        sys.exit("{} scheduling problem(s) - see above".format(len(schedule_problems)))

    if all_failures:
        print("=" * 62)
        print("{} case(s) failed to execute:\n".format(len(all_failures)))
        for mod, tid, exc, tb in all_failures:
            print("--- {} / {} ---".format(mod, tid))
            print(tb if isinstance(tb, str) else repr(tb))
        sys.exit(1)

    print("=" * 62)
    print("All cases executed cleanly against the fake fleet.")
    print("NOTE: this proves they RUN, not that their assertions are correct.")


if __name__ == "__main__":
    main()
