#!/usr/bin/env python3
"""
master_cases.py - the lab suite. Holds no cases itself: it loads the case
modules and merges what they register into the CASES / ORDER / NEEDS /
NO_DELAY_RERUN that full-test/test_runner.py drives.

Only what the lab can find and the product's CI cannot. The CI runs unit
tests and a 3-node localnet integration suite (test/integration: one of each
asset flow between two nodes, six negative cases); the lab runs the same
product on 234 nodes across 26 machines, so its cases are about capacity,
concurrency, state growth and failure at that scale. Every RBT comes from the
faucet DID; nothing here mints RBT.

The suite runs in stages, in this order:

    0  audit_cases.py    AUDIT-START  the fleet is clean, total = 400,000 RBT
    1  quorum_cases.py   QCAP-01      what one quorum signs at once (saved, and
                                      used to size the quorums of later cases)
                         QCAP-02      the pledge boundary is the quorum's holding
                         QCAP-03a/b   a quorum spending its own RBT
       wallet_cases.py   WALLET-PREP  a big and a fragmented wallet, then
                         WALLET-01    sending 1 / 100 / 1,000 RBT from them
       asset_cases.py    FT-01        FT series minted, re-minted, passed on
                         NFT-01       NFTs with children, executes, new owners
                         SC-01        contracts with subscribers at many depths
    2  race_cases.py     RACE-01      one token, many simultaneous sends - one wins
                         RACE-02      node- and machine-level concurrency
       load_cases.py     LOAD-01      the whole fleet transacting at once
       chain_cases.py    CHAIN-01     a deep chain, a new quorum at every hop
       combo_cases.py    COMBO-01     several asset types in one transaction
    3  fail_cases.py     FAIL-01      a node killed mid-transfer, six ways
    4  audit_cases.py    AUDIT-END    nothing lost, created, locked or doubled

Each module defines CASES {id: fn}, ORDER [id, ...] and NEEDS (what each case
or group of cases needs: senders, receivers, quorums, RBT). To add a case:
write fn(ctx, ci) in the right module, add it to CASES, ORDER and NEEDS, and
give it a row in master-catalogue.csv. Nothing in this file changes.

Run:
    cd ../full-test
    python3 test_runner.py                                   # everything
    python3 test_runner.py --only 'QCAP-*'                   # one group
    python3 validate_cases.py --cases master                 # offline check
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import audit_cases
import quorum_cases
import wallet_cases
import asset_cases
import race_cases
import load_cases
import chain_cases
import combo_cases
import fail_cases

# Run order. A unit marked "first" or "last" in its NEEDS runs at the start or
# the end whatever its place here.
CASE_MODULES = [
    audit_cases,
    quorum_cases,
    wallet_cases,
    asset_cases,
    race_cases,
    load_cases,
    chain_cases,
    combo_cases,
    fail_cases,
]


def _merge_registries(modules):
    """One CASES / ORDER / NEEDS / TIMING_CASES / CASE_INFO across every
    module. A Test ID or unit name defined twice would silently shadow the
    other, so either is an import-time error rather than a quiet overwrite.
    """
    cases, order, needs, timing, info, no_rerun = {}, [], {}, set(), {}, set()
    for mod in modules:
        for tid in mod.ORDER:
            if tid in cases:
                raise RuntimeError("Test ID defined twice: {} ({})".format(tid, mod.__name__))
            cases[tid] = mod.CASES[tid]
            order.append(tid)
        missing = set(mod.CASES) - set(mod.ORDER)
        if missing:
            raise RuntimeError("{}: in CASES but not ORDER, so never run: {}".format(
                mod.__name__, ", ".join(sorted(missing))))
        in_unit = set()
        for name, spec in (getattr(mod, "NEEDS", None) or {}).items():
            if name in needs:
                raise RuntimeError("unit defined twice: " + name)
            needs[name] = spec
            in_unit |= set(spec.get("cases", [name]))
        undeclared = [t for t in mod.ORDER if t not in in_unit]
        if undeclared:
            raise RuntimeError("{}: no NEEDS entry for {}".format(
                mod.__name__, ", ".join(undeclared)))
        timing |= set(getattr(mod, "TIMING_CASES", None) or ())
        no_rerun |= set(getattr(mod, "NO_DELAY_RERUN", None) or ())
        info.update(getattr(mod, "CASE_INFO", None) or {})
    # The report follows ORDER, so "last" cases (the closing audit) go last.
    last = set(c for spec in needs.values() if spec.get("last")
               for c in spec.get("cases", []) or [])
    last |= set(name for name, spec in needs.items() if spec.get("last") and "cases" not in spec)
    lasts = [t for t in order if t in last]
    # The closing audit is always the very last case, after any destructive
    # "last" test (FAIL-01 kills nodes), so it audits the fleet's final state.
    lasts.sort(key=lambda t: t == "AUDIT-END")
    order = [t for t in order if t not in last] + lasts
    return cases, order, needs, timing, info, no_rerun


CASES, ORDER, NEEDS, TIMING_CASES, CASE_INFO, NO_DELAY_RERUN = _merge_registries(CASE_MODULES)
