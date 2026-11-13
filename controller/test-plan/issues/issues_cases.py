#!/usr/bin/env python3
"""
issues_cases.py - one check per Rubix issue the lab has found, to show each is
genuine before it goes to the core team. Run by the same runner as the
catalogue, on the same fleet, set up the same way:

    cd ../full-test
    python3 test_runner.py --cases issues --collect-versions
    python3 test_runner.py --cases issues --only 'EXACT-DECIMAL,PAY-TO-NODE-DOWN'
    python3 validate_cases.py --cases issues            # offline

What makes the result trustworthy (user, 2026-11-13):

  * AUDIT-START runs first, as in every catalogue run: every token unfit for a
    test is taken out of use before anything moves (full-test/token_health.py).
  * Every token a check moves has a LONG, proper history - at least
    issue_kit.MIN_CHAIN entries in its chain at its owner - and is checked
    across the fleet right before it moves: one owner, a whole chain whose row
    points at its end, every other node's copy agreeing, no fullnode
    disagreeing. So a refusal is not a fresh token's luck, nor an old token's
    damage. The evidence lists every token moved with its chain length.
  * Each check runs alone (nothing else on the fleet) and once.
  * A check FAILS when Rubix does what the issue says - the issue is
    REPRODUCED; it PASSES when Rubix behaves correctly; it is NOT RUN when the
    lab could not make its set-up, which says nothing about Rubix.
  * AUDIT-END closes the run: what moved, what was lost or held twice.

The checks are named by what goes wrong, in plain words (CASE_INFO), and say
in their notes and evidence which code path is behind it.

Not checked here: the peer-port pool leak and overlap, NodeConfigDir never set
and the pubsub receive loop (found by reading the code - they need an
instrumented build to show); split checks and wallet recovery needing Rubix's
public fullnode list (SMALL-SPLITS notes the quorum's "cannot verify" log
line); quorum list order (GetAllQuorums has no ORDER BY - Postgres usually
returns insertion order, so it cannot be provoked); signatures on Go 1.25+
(a build-time fault, the lab pins Go 1.22); CreateDID has no idempotency (the
lab never creates DIDs); the minter allowlist naming another minter (seen only
on tokens whose chain had lost its genesis - corrupt tokens, which this run
does not use).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "master"))
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import audit_cases
import issue_transfers
import issue_assets
import issue_load
import issue_nodes
from master_cases import _merge_registries

# Run order: the audit first ("first"), the quick, deterministic checks, the
# NFT/contract checks, the load checks, the one that restarts a machine
# ("last"), and the closing audit, always the very last.
CASE_MODULES = [audit_cases, issue_transfers, issue_assets, issue_load, issue_nodes]

CASES, ORDER, NEEDS, TIMING_CASES, CASE_INFO, NO_DELAY_RERUN = _merge_registries(CASE_MODULES)
PURPOSE = {}
STEPS = {}
for _m in CASE_MODULES:
    PURPOSE.update(getattr(_m, "PURPOSE", {}))
    STEPS.update(getattr(_m, "STEPS", {}))
