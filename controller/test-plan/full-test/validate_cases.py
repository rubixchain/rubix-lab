#!/usr/bin/env python3
"""
validate_cases.py - run every case against a FAKE fleet, offline.

WHY THIS EXISTS
    SC-C-01 and SC-C-02 reached the real fleet and died instantly with
        TypeError: string indices must be integers, not 'str'
    because they called ctx.quorum_for(host) when case_runner's quorum_for
    expects the entry DICT. A one-line mistake, but it cost a full setup cycle
    (~60s of minting) to discover, and it would have been caught by calling the
    function even once.

    Checking that a module imports and that its docstrings are well-formed
    proves nothing about whether the code runs. This calls every case.

WHAT IT DOES
    Builds a CaseContext with the same shape case_runner builds, stubs every
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
    python3 validate_cases.py                 # every asset module
    python3 validate_cases.py --cases sc      # one module
    python3 validate_cases.py --verbose       # show each case's return value

Exit code is non-zero if any case raised, so this can gate a run:
    python3 validate_cases.py && python3 case_runner.py --cases sc
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
from case_runner import CaseContext, CaseInfo, load_case_module


class _Args:
    """Stands in for argparse's namespace. Mirrors case_runner's defaults."""
    port = 20000
    rbt_amount = 1
    ft_count = 10
    ft_token_count = 1
    fund_quorum = 2000
    fund_sender = 200
    large_mint = 2000
    decimal_samples = 3
    repeat_count = 25
    callback_host = "192.168.1.103"
    ssh_user = "rubix"
    remote_dir = "~/Desktop/rubix"


def _entry(n):
    return {"host": "192.168.1.{}".format(n),
            "did": "bafybmi{}".format(str(n) * 7),
            "role": ""}


def build_ctx(n_quorum=3, n_pairs=6):
    """A CaseContext shaped exactly like the one case_runner passes in.

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
    return CaseContext(_Args.port, quorum_hosts, senders, receivers,
                       sender_quorum, _Args())


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
    rc.fund_did = rec("fund_did", (True, "minted"))
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
    db.duplicate_token_ids = rec("duplicate_token_ids", [])
    return calls


def _patch_sleep():
    """Cases wait for settle windows; offline there is nothing to wait for."""
    import time
    time.sleep = lambda *a, **k: None


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
    for m in modules:
        all_failures += [(m,) + f for f in validate(m, args.verbose)]

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
