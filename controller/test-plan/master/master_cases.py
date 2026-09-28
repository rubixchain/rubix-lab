#!/usr/bin/env python3
"""
master_cases.py - the lab suite. Holds no cases itself: it loads one module
per asset and merges what they register into the CASES / ORDER / NEEDS /
TIMING_CASES that full-test/test_runner.py drives.

    master/
        master_cases.py     this file - which asset modules make up the suite
        case_helpers.py     shared by more than one asset module
        rbt_cases.py        RBT
        ft_cases.py         FT
        sc_cases.py         Smart contracts
        crs_cases.py        Cross-asset (several assets in one transaction)
        gen_cases.py        General: fleet-wide integrity, drift, locks
        master-catalogue.csv  the wording of every case (Test Case, Expected ...)

Each asset module defines CASES {id: fn}, ORDER [id, ...], NEEDS (what each
case or group of cases needs: senders, receivers, quorums, RBT) and optionally
TIMING_CASES. To add a case: write fn(ctx, ci) in the right asset module, add
it to that module's CASES, ORDER and NEEDS, and give it a row in
master-catalogue.csv. Nothing in this file changes.

Only cases that need what the lab has and the product's CI does not: many
nodes, concurrency, high value, large wallets, long chains, node kills and DB
corruption. Every RBT comes from the faucet DID; nothing here mints RBT.

Run:
    cd ../full-test
    python3 test_runner.py                                   # everything
    python3 test_runner.py --only 'SC-*'                     # one asset
    python3 validate_cases.py --cases master                 # offline check

Cases, by module and operation group:
    rbt_cases.py  (37 cases)
        Transfer Value   RBT-V-11
        Precision        RBT-P-04
        Wallet Shape     RBT-W-03
        Split            RBT-S-02 RBT-S-04
        Quorum Capacity  RBT-Q-02 RBT-Q-03 RBT-Q-04 RBT-Q-05 RBT-Q-06
                         RBT-Q-07 RBT-Q-13
        Pledging         RBT-L-02 RBT-L-03
        Concurrency      RBT-N-01 RBT-N-10 RBT-N-11 RBT-N-12 RBT-N-13
                         RBT-N-14 RBT-N-15
        Failure          RBT-F-01 RBT-F-02 RBT-F-03 RBT-F-04 RBT-F-05
        Bulk             RBT-B-01 RBT-B-02 RBT-B-03 RBT-B-05
                         RBT-B-06 RBT-B-07 RBT-B-08
    ft_cases.py  (7 cases)
        Parts            FT-P-06 FT-P-09 FT-P-07 FT-P-08
        DB               FT-DB-04
        Scale            FT-X-01 FT-X-02
    sc_cases.py  (28 cases)
        Subscription     SC-S-01 SC-S-02 SC-S-03 SC-S-04 SC-S-05 SC-S-06
                         SC-S-07 SC-S-08 SC-S-09 SC-S-10
        Collateral       SC-C-13 SC-C-19 SC-C-12 SC-C-26 SC-C-29 SC-C-32
                         SC-C-34 SC-C-33 SC-C-27 SC-C-31
        Quorum Capacity  SC-Q-11
        Quorum           SC-Q-12
        Scale            SC-X-01 SC-X-02 SC-X-03 SC-X-04 SC-X-05
        DB Failure       SC-DB-03
    crs_cases.py  (1 cases)
        Combined         CRS-C-02
    gen_cases.py  (10 cases)
        Integrity        GEN-IN-08 GEN-IN-09 GEN-IN-12 GEN-IN-13 GEN-IN-14
                         GEN-IN-15 GEN-IN-19 GEN-IN-21 GEN-IN-22 GEN-IN-23
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rbt_cases
import ft_cases
import sc_cases
import crs_cases
import gen_cases

# Order here is run order across assets. Comment a module out to leave that
# asset out of the suite entirely.
ASSET_MODULES = [
    rbt_cases,
    ft_cases,
    sc_cases,
    crs_cases,
    gen_cases,
]


def _merge_registries(modules):
    """One CASES / ORDER / NEEDS / TIMING_CASES / CASE_INFO across every asset
    module. A Test ID or unit name defined twice would silently shadow the
    other, so either is an import-time error rather than a quiet overwrite.
    """
    cases, order, needs, timing, info = {}, [], {}, set(), {}
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
        info.update(getattr(mod, "CASE_INFO", None) or {})
    return cases, order, needs, timing, info


CASES, ORDER, NEEDS, TIMING_CASES, CASE_INFO = _merge_registries(ASSET_MODULES)
