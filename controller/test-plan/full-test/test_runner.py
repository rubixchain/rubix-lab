#!/usr/bin/env python3
"""
test_runner.py - runs the lab suite on the fleet.

The cases live in master/ (one module per stage or group, see
master/master_cases.py); this file is everything around them: which DIDs take
part in each case, getting them ready, running the case, checking the wallets
afterwards, and writing the report.

Flow:
    1. Load the cases (master/master_cases.py) and what each one NEEDS:
       how many senders, receivers and quorums, and how much RBT they hold.
    2. Find every reachable node and every DID on it. A pool machine runs
       several nodes (systems/node-layout.txt), so a node is "ip:api_port"
       (controller/fleet.py). DIDs are fixed - none is ever created here.
    3. Check the faucet: its node must have exactly one quorum, the faucet
       quorum. All RBT comes from the faucet.
    4. For each case (or group of cases that must share DIDs), in order:
         a. wait until enough nodes are free
         b. pick DIDs: quorums, then senders holding the most RBT, then
            receivers holding the least - on different machines where it can
         c. set up the quorums; set every participant node's quorum list to
            that quorum (so the signer is certain); faucet any shortfall
         d. run the case; check the wallets' database rows before and after
       Cases whose nodes do not overlap run at the same time, so how many run
       in parallel depends on the cases, not on a fixed plan.
    5. Report in catalogue order, plus the fleet ledger (fleet + faucet).

Three outcomes:
    PASS    ran and matched the expectation
    FAIL    ran and did NOT match: a finding
    SKIP    not attempted, with the reason. Never counted as a pass.

The suite runs in stages, in catalogue order: the opening fleet audit, then
capacity (QCAP-01 measures how many transfers one quorum signs at once and
saves it to lab-limits.json, which later cases size their quorums by) and the
asset stock, then the limit cases, then failures, then the closing audit.

Usage:
    python3 test_runner.py                                   # every case
    python3 test_runner.py --only 'QCAP-*'                   # one group
    python3 test_runner.py --only AUDIT-START,QCAP-01

One run on the fleet at a time: a second run, from a terminal or the web UI,
refuses to start while one is going (run_lock.py). --events FILE also writes
the run as JSON lines for the web UI (run_events.py); without it nothing
changes.
"""

import argparse
import csv
import re
import datetime
import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# controller/ - fleet.py, the one reader of hosts.txt and node-layout.txt
sys.path.insert(0, os.path.join(HERE, "..", ".."))

import rubix_client as rc
import db_client as db
import case_evidence
import fleet as fleet_inventory
import report_builder
import run_events
import run_lock

FIXED_ROLES = fleet_inventory.FIXED_ROLES
DEFAULT_HOSTS = fleet_inventory.HOSTS_PATH
ANNOUNCE_SETTLE_SECONDS = 3     # after the DID announce pass

# The master catalogue is the source of truth for case wording.
CATALOGUE_PATH = os.path.join(HERE, "..", "master", "master-catalogue.csv")

SKIP = "SKIP"


class CaseInfo:
    """One row of master/master-catalogue.csv."""

    def __init__(self, test_id, asset="", case="", expected="", checks="", notes=""):
        self.test_id = test_id
        self.asset = asset
        self.case = case
        self.expected = expected
        self.checks = checks
        self.notes = notes

    @property
    def expects_rejection(self):
        """True when the catalogue's Expected Result is a rejection."""
        return self.expected.strip().lower().startswith("reject")

    @property
    def is_record_only(self):
        """True when the catalogue asks to RECORD behaviour rather than
        assert pass/fail - e.g. value ladders ('Record the largest value that
        works') and 'Define what happens'. Forcing these into pass/fail would
        invent an expectation the catalogue deliberately doesn't state."""
        low = self.expected.strip().lower()
        return low.startswith("record") or low.startswith("define") or "find the" in low


class CaseContext:
    """What every case gets. Same shape as smoke_test's SmokeContext plus
    the extras full-catalogue cases need."""

    def __init__(self, port, quorum_hosts, senders, receivers, sender_quorum, args,
                 fleet=None):
        self.port = port
        self.quorum_hosts = quorum_hosts
        self.senders = senders
        self.receivers = receivers
        self.sender_quorum = sender_quorum
        # One pair per sender. When a unit has fewer receivers than senders
        # (20 senders sharing 5 receivers to load one quorum), receivers repeat.
        self.pairs = ([(s, receivers[i % len(receivers)]) for i, s in enumerate(senders)]
                      if senders and receivers else [])
        self.args = args

        # EVERY DID in the pool, not just this unit's. A unit's participants
        # are deliberately narrow - that is what makes balance-delta assertions
        # valid - but the fleet-wide sweeps (GEN-IN-12/13/14/15/22) exist to
        # find damage nobody attributed to anything, so they look at all of it.
        self.fleet = list(fleet) if fleet else []

    def pair(self, i=0):
        """A (sender, receiver) pair; the index wraps, so a case asking for
        pair(9) in a one-pair unit gets pair 0. A case that needs two DIFFERENT
        pairs must declare two senders in its NEEDS."""
        return self.pairs[i % len(self.pairs)]

    def quorum_for(self, sender):
        return self.sender_quorum.get(sender["host"])


def load_master(path=None):
    """Load the catalogue, keyed by Test ID.

    Reads master/master-catalogue.csv - the single source of truth. A case
    whose Test ID has no row there still runs, but reports with EMPTY 'Test
    Case' and 'Expected Result' columns, so every case in master_cases.py must
    have a catalogue row.
    """
    path = path or CATALOGUE_PATH
    if not os.path.exists(path):
        sys.exit("ERROR: catalogue not found: {}".format(path))
    out = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            tid = (row.get("Test ID") or "").strip()
            if not tid:
                continue
            out[tid] = CaseInfo(
                tid,
                asset=row.get("Asset") or "",
                case=row.get("Test Case") or "",
                expected=row.get("Expected Result") or "",
                checks=row.get("Also Check In Same Run") or "",
                notes=row.get("Code Ref") or "",
            )
    return out


SUITES_DIR = os.path.join(HERE, "..", "suites")

# Which module owns each Test ID prefix, so a suite file can list cases without
# also having to name the modules they live in. Every asset now lives in the
# master script.
PREFIX_MODULE = dict((p, "master") for p in (
    "AUDIT", "QCAP", "WALLET", "FT", "NFT", "SC", "RACE", "LOAD", "CHAIN", "COMBO", "FAIL"))


def load_suite(name):
    """Read suites/<name>.txt -> (patterns, modules).

    A suite is a NAMED, COMMITTED selection of existing cases - typically the
    set that verifies one change, so its author gets a report containing their
    work and nothing else.

    Deliberately NOT a separate copy of the cases. Cases are organised by asset
    because they outlive the change that prompted them: SC-C-01 is a permanent
    regression check, and a file named after a merged PR would become
    archaeology nobody dares delete. The suite names the SELECTION; the cases
    stay where they belong.
    """
    path = name if os.path.exists(name) else os.path.join(SUITES_DIR, name + ".txt")
    if not os.path.exists(path):
        available = []
        if os.path.isdir(SUITES_DIR):
            available = sorted(f[:-4] for f in os.listdir(SUITES_DIR) if f.endswith(".txt"))
        sys.exit("ERROR: no suite {!r} at {}\n       Available: {}".format(
            name, path, ", ".join(available) or "(none)"))

    patterns = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if line:
                patterns.append(line)
    if not patterns:
        sys.exit("ERROR: suite {} lists no Test IDs.".format(path))

    modules, seen = [], set()
    for pat in patterns:
        prefix = pat.split("-", 1)[0]
        mod = PREFIX_MODULE.get(prefix)
        if mod is None:
            sys.exit("ERROR: suite {} has {!r}, whose prefix {!r} maps to no "
                     "module. Known: {}".format(path, pat, prefix,
                                                ", ".join(sorted(PREFIX_MODULE))))
        if mod not in seen:
            seen.add(mod)
            modules.append(mod)
    print("Suite {} -> {} pattern(s) across {}".format(
        os.path.basename(path), len(patterns), ", ".join(modules)))
    return patterns, modules


def load_case_module(name):
    """Import test-plan/<name>/<name>_cases.py by path."""
    mod_path = os.path.join(HERE, "..", name, "{}_cases.py".format(name))
    mod_path = os.path.abspath(mod_path)
    if not os.path.exists(mod_path):
        sys.exit("ERROR: no case module at {}".format(mod_path))
    spec = importlib.util.spec_from_file_location("{}_cases".format(name), mod_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_ci(tid, master, case_info):
    ci = master.get(tid)
    if ci is not None:
        return ci
    text = case_info.get(tid)
    return CaseInfo(tid, case=text[0], expected=text[1]) if text else CaseInfo(tid)


# ---------------------------------------------------------------------------
# Units - what a case needs
#
# A UNIT is what gets participants: one case, or several cases that must run
# in order on the same DIDs (SC-S-01..05 all watch one contract). Each asset
# module declares its units in NEEDS:
#
#     NEEDS = {
#         "RBT-N-01": {"senders": 2, "receivers": 2},
#         "sc-subscription": {"cases": ["SC-S-01", ...], "receivers": 5, "fund": 6},
#     }
#
#   cases        Test IDs, run in this order (default: just the key)
#   senders      DIDs that send (default 1). "all" = every free node except the
#                quorums and receivers; such a unit runs with nothing else running
#   receivers    DIDs that receive (default 1). 0 = the senders are also the
#                receivers
#   quorums      quorum DIDs (default 1; 0 for cases that sign nothing)
#   fund         RBT each sender and receiver holds before the unit starts
#   quorum_fund  RBT each quorum holds before the unit starts (default
#                --quorum-floor). Cases top a quorum up further when a transfer
#                needs it.
#   quorum_pick  "richest" (default) or "poorest" - SC-C-27 has to out-pledge
#                its quorum, which is cheapest against the poorest one
#   same_node    every DID from ONE node (sender, receiver and quorum on the
#                same machine); needs a node holding that many DIDs
#   exclusive    run with nothing else running, at its place in the catalogue:
#                units before it finish first, units after it wait for it
#   first        run before everything else, alone (the opening fleet audit)
#   last         run after every other unit has finished
# ---------------------------------------------------------------------------

# Per-case database evidence (case_evidence.finish), written next to the report.
CASE_EVIDENCE = []

# Time-boxed cases (the case module's NO_DELAY_RERUN) run once, without delay:
# a delayed rerun spends its fixed window waiting on the fullnode.
NO_DELAY_RERUN = set()

UNIT_DEFAULTS = {"senders": 1, "receivers": 1, "quorums": 1, "fund": 0,
                 "quorum_fund": None, "quorum_pick": "richest",
                 "same_node": False, "exclusive": False, "first": False, "last": False}

# Added to a unit's `fund`. Being generous costs one faucet transfer; being
# short costs a failure that says nothing about the product.
FUND_SAFETY_MARGIN = 10


def unit_targets(spec, quorum_floor):
    """(RBT each quorum, RBT each sender and receiver) a unit is topped up to
    before it starts; prepare_unit draws only the shortfall from the faucet.
    The web UI shows the same figures as the faucet draw before a run."""
    floor = spec["quorum_fund"] if spec["quorum_fund"] is not None else quorum_floor
    want = spec["fund"] + FUND_SAFETY_MARGIN if spec["fund"] else 0
    return floor, want


class Unit(object):
    def __init__(self, name, cases, spec):
        unknown = set(spec) - set(UNIT_DEFAULTS) - {"cases"}
        if unknown:
            raise ValueError("unit {}: unknown key(s) {}".format(name, sorted(unknown)))
        self.name = name
        self.cases = cases
        self.spec = dict(UNIT_DEFAULTS)
        self.spec.update({k: v for k, v in spec.items() if k != "cases"})
        self.quorums, self.senders, self.receivers = [], [], []
        self.hosts = set()
        self.rows = []              # (test_id, result_tuple, seconds, CaseInfo)
        self.dirty_spenders = []    # senders/receivers holding ex-pledged tokens

    @property
    def wants_all(self):
        return self.spec["senders"] == "all"

    @property
    def exclusive(self):
        return bool(self.spec["exclusive"]) or bool(self.spec["first"]) or self.wants_all

    @property
    def needs_nodes(self):
        return any(self.spec[k] for k in ("senders", "receivers", "quorums"))

    def node_count(self):
        """Nodes this unit occupies, or None for "all"."""
        s = self.spec
        if self.wants_all:
            return None
        n = s["senders"] + s["receivers"] + s["quorums"]
        return (1 if n else 0) if s["same_node"] else n

    def describe(self):
        def octets(entries):
            return ",".join(e["host"].split(".")[-1] for e in entries) or "-"
        return "s {} r {} q {}".format(octets(self.senders), octets(self.receivers),
                                       octets(self.quorums))


def build_units(module, order):
    """Units for the selected cases, in run order. Every selected case must
    belong to exactly one unit - a case nobody declared would otherwise never
    run, silently."""
    needs = getattr(module, "NEEDS", None) or {}
    selected = set(order)
    pos = {t: i for i, t in enumerate(order)}
    owner, units = {}, []
    for name, spec in needs.items():
        cases = list(spec.get("cases", [name]))
        for c in cases:
            if c in owner:
                sys.exit("ERROR: {} is in unit {!r} and unit {!r}".format(c, owner[c], name))
            owner[c] = name
        cases = [c for c in cases if c in selected]
        if cases:
            units.append(Unit(name, cases, spec))
    undeclared = [t for t in order if t not in owner]
    if undeclared:
        sys.exit("ERROR: no NEEDS entry for {} - declare what each case needs in its "
                 "asset module".format(", ".join(undeclared)))
    # "first" units, then catalogue order, then the "last" ones. An exclusive
    # unit keeps its place: the suite runs in stages (capacity measured first,
    # then the cases sized by it), so it waits for the units before it and the
    # units after it wait for it - run_units enforces both.
    units.sort(key=lambda u: (not u.spec["first"], bool(u.spec["last"]), pos[u.cases[0]]))
    return units


# ---------------------------------------------------------------------------
# Choosing participants
#
# The unit of ownership is a NODE, not a DID: the quorum a transfer uses is
# the first entry in the sending node's quorum list, which every DID on that
# node shares. Two units on one node would overwrite each other's quorum.
# ---------------------------------------------------------------------------

def _free_balances(entries, port):
    """{did: free RBT} for these entries, read in parallel."""
    from concurrent.futures import ThreadPoolExecutor

    def one(e):
        ok, detail, _ = rc.get_rbt_balance_detail(e["host"], e["did"], port)
        return e["did"], (float(detail["balance"]) if ok and detail else 0.0)

    if not entries:
        return {}
    with ThreadPoolExecutor(max_workers=min(40, len(entries))) as ex:
        return dict(ex.map(one, entries))


def _ex_pledged(entries):
    """{did: free RBT held in tokens it pledged as a quorum earlier} - see
    db.ex_pledged_free_value. Unreadable counts as 0.001 so a DID we cannot
    check sorts after the clean ones but before the known-bad ones."""
    from concurrent.futures import ThreadPoolExecutor

    def one(e):
        try:
            return e["did"], db.ex_pledged_free_value(e["host"], e["did"])
        except Exception:
            return e["did"], 0.001

    if not entries:
        return {}
    with ThreadPoolExecutor(max_workers=min(40, len(entries))) as ex:
        return dict(ex.map(one, entries))


def _spread(candidates, k, used):
    """The first k of `candidates` (already in preference order), at most one
    per machine while unused machines are left, then whatever is left. `used`
    holds the machines this unit already occupies and is updated.

    Nine nodes share a machine, so without this a unit's sender, receiver and
    quorum would often sit on one desktop: their traffic would never leave it,
    and they would compete for its CPU in the very cases that time them."""
    picked = []
    for e in candidates:
        if len(picked) < k and e["machine"] not in used:
            picked.append(e)
            used.add(e["machine"])
    for e in candidates:
        if len(picked) < k and e not in picked:
            picked.append(e)
            used.add(e["machine"])
    return picked


def fits(unit, free_nodes, pool_nodes):
    """(fits_now, possible_ever, why_not)."""
    s = unit.spec
    if s["same_node"]:
        k = s["senders"] + s["receivers"] + s["quorums"]
        if not any(len(n["dids"]) >= k for n in pool_nodes):
            return False, False, "needs a node holding {} DIDs; none in the pool does".format(k)
        return any(len(n["dids"]) >= k for n in free_nodes), True, ""
    if unit.wants_all:
        least = s["quorums"] + s["receivers"] + 1
        if len(pool_nodes) < least:
            return False, False, "needs at least {} nodes; the pool has {}".format(
                least, len(pool_nodes))
        return len(free_nodes) == len(pool_nodes), True, ""
    n = unit.node_count()
    if n > len(pool_nodes):
        return False, False, "needs {} nodes; the pool has {}".format(n, len(pool_nodes))
    return len(free_nodes) >= n, True, ""


def allocate(unit, free_nodes, port):
    """Pick this unit's DIDs from the free nodes. Quorums first, then senders
    (the DIDs holding the most, so the faucet is drawn on least), then
    receivers (the ones holding least, so value spreads out).

    Ex-pledged tokens decide first. A DID that was a quorum holds tokens whose
    last chain entry is "unpledge", and the product refuses to let it spend
    them (known bug, core/consensus/checks.go:308). Quorums are topped up to
    the quorum floor, so those DIDs are also the RICHEST - and "richest sends"
    put them in the sender seat, which is why 2-node shared-quorum cases failed
    on 2026-09-29 when the same transfers by hand would not. So: DIDs holding
    ex-pledged tokens are preferred as QUORUMS (pledging them is fine), and
    DIDs without them are preferred as senders and receivers (receivers send
    too, in chains and round trips). That also keeps clean DIDs clean."""
    s = unit.spec
    if not unit.needs_nodes:
        return
    # Every entry carries its node's whole DID list, so a case that needs a
    # second DID on the same node (wallet_shapes.second_did) uses one that
    # exists instead of creating it.
    if s["same_node"]:
        k = s["senders"] + s["receivers"] + s["quorums"]
        node = next(n for n in free_nodes if len(n["dids"]) >= k)
        entries = [{"host": node["host"], "machine": node["machine"], "did": d,
                    "dids": list(node["dids"])} for d in node["dids"]]
        bal = _free_balances(entries, port)
        entries.sort(key=lambda e: -bal[e["did"]])
        q = s["quorums"]
        unit.quorums = entries[:q]
        unit.senders = entries[q:q + s["senders"]]
        unit.receivers = entries[q + s["senders"]:k]
    else:
        # One DID per node - the one holding most, if the node has several.
        entries = [{"host": n["host"], "machine": n["machine"], "did": d, "dids": list(n["dids"])}
                   for n in free_nodes for d in n["dids"]]
        bal = _free_balances(entries, port)
        best = {}
        for e in entries:
            if e["host"] not in best or bal[e["did"]] > bal[best[e["host"]]["did"]]:
                best[e["host"]] = e
        taint = _ex_pledged(list(best.values()))
        dirty = lambda e: taint.get(e["did"], 0.0) > 0
        used = set()
        if s["quorum_pick"] == "poorest":
            unit.quorums = _spread(sorted(best.values(), key=lambda e: (not dirty(e), bal[e["did"]])),
                                   s["quorums"], used)
        else:
            unit.quorums = _spread(sorted(best.values(), key=lambda e: (not dirty(e), -bal[e["did"]])),
                                   s["quorums"], used)
        rest = sorted((e for e in best.values() if e not in unit.quorums),
                      key=lambda e: (dirty(e), -bal[e["did"]]))
        if unit.wants_all:
            # every node sends; receivers are the poorest of the clean ones
            unit.receivers = _spread(sorted(rest, key=lambda e: (dirty(e), bal[e["did"]])),
                                     s["receivers"], used)
            unit.senders = [e for e in rest if e not in unit.receivers]
        else:
            unit.senders = _spread(rest, s["senders"], used)
            unit.receivers = _spread(sorted((e for e in rest if e not in unit.senders),
                                            key=lambda e: (dirty(e), bal[e["did"]])),
                                     s["receivers"], used)
        # Said in the run output: a refusal naming "failed to get quorum DID"
        # from these is the known bug, not the case.
        unit.dirty_spenders = ["{} ({:.3f} RBT)".format(e["host"].split(".")[-1], taint[e["did"]])
                               for e in unit.senders + unit.receivers if dirty(e)]
    unit.hosts = {e["host"] for e in unit.quorums + unit.senders + unit.receivers}


def prepare_unit(unit, args):
    """Set up the unit's quorums, point every participant node at them, and
    fund what the unit's cases expect. Returns sender_quorum {host: quorum}.
    Raises RuntimeError with the step that failed."""
    s = unit.spec
    floor, want = unit_targets(s, args.quorum_floor)
    for q in unit.quorums:
        ok, msg = rc.quorum_setup(q["host"], q["did"], args.port)
        if not ok:
            raise RuntimeError("quorum setup on {} failed: {}".format(q["host"], msg))
        _top_up(q, floor, args)

    sender_quorum = {}
    if unit.quorums:
        participants = unit.senders + unit.receivers
        hosts = []
        for e in participants:
            if e["host"] not in hosts:
                hosts.append(e["host"])
        for i, host in enumerate(hosts):
            q = unit.quorums[i % len(unit.quorums)]
            ok, msg = rc.quorum_reset(host, [q["did"]], args.port)
            if not ok:
                raise RuntimeError(msg)
            sender_quorum[host] = q

    seen = set()
    for e in unit.senders + unit.receivers:
        if e["did"] not in seen:
            seen.add(e["did"])
            _top_up(e, want, args)
    return sender_quorum


def _top_up(entry, target, args):
    """Faucet the shortfall only - a DID keeps what it holds between runs."""
    if not target:
        return
    ok, detail, _ = rc.get_rbt_balance_detail(entry["host"], entry["did"], args.port)
    have = float(detail["balance"]) if ok and detail else 0.0
    if have >= target:
        return
    ok, msg = rc.fund_did(entry["host"], entry["did"], target - have, args.port)
    if not ok:
        raise RuntimeError("funding {} to {} RBT failed: {}".format(entry["host"], target, msg))


# ---------------------------------------------------------------------------
# The scheduler
#
# Takes units in order. A unit starts as soon as enough nodes are free; its
# nodes are busy until it finishes, and the next unit that fits in what is
# left starts beside it. So when small cases run, many run at once; when a
# case needs most of the fleet, it runs nearly alone. An "exclusive" or "all"
# unit waits for everything running to finish, and nothing queued behind it
# starts first - otherwise small units could keep it waiting forever.
# ---------------------------------------------------------------------------

def run_units(units, pool_nodes, fleet, cases_map, master, case_info, args):
    import threading
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    print_lock = threading.Lock()
    busy = set()
    pending = list(units)
    running = {}

    def say(line):
        with print_lock:
            print(line)

    def skip_all(unit, actual, note):
        for tid in unit.cases:
            unit.rows.append((tid, (SKIP, actual, note), 0.0, resolve_ci(tid, master, case_info)))
            run_events.case_result(unit, tid, SKIP, (SKIP, actual, note), 0.0, None)
            say("  [SKIP] {:<11} {}: {}".format(tid, actual, note))

    def run_one(unit):
        run_events.unit_started(unit)
        try:
            sender_quorum = prepare_unit(unit, args) if unit.needs_nodes else {}
        except Exception as e:
            skip_all(unit, "participants could not be prepared",
                     "{} ({})".format(e, unit.describe()))
            return
        receivers = unit.receivers or unit.senders
        unit.ctx = CaseContext(args.port, unit.quorums, unit.senders, receivers,
                               sender_quorum, args, fleet=fleet)
        say("  -> {:<22} {}".format(unit.name, unit.describe()))
        if unit.dirty_spenders:
            say("     note: not enough clean nodes free - {} hold(s) tokens pledged as a "
                "quorum earlier; their 'failed to get quorum DID' refusals are the "
                "known product bug".format(", ".join(unit.dirty_spenders)))
        _run_cases(unit)

    def _unpaced_dids(unit):
        """Every DID a case may send from in its WITHOUT-delay run: the
        participants, the other DIDs on their nodes (wallet_shapes.second_did,
        case_helpers.empty_did), and for an exclusive unit the whole fleet -
        LOAD-01, RACE-02 and FAIL-01 drive DIDs straight from ctx.fleet. Until
        2026-10-23 only the participants were unpaced, so LOAD-01's hundreds of
        fleet senders each polled every fullnode's Postgres twice a second on a
        new connection, and the fullnodes ran out of connections ("too many
        clients already") in the middle of the run."""
        entries = unit.quorums + unit.senders + unit.receivers
        dids = set(e["did"] for e in entries)
        for e in entries:
            dids.update(e.get("dids") or ())
        if unit.exclusive:
            dids.update(e["did"] for e in (fleet or ()))
        return sorted(dids)

    def _run_once(unit, tid, ci, paced):
        """One run of a case, wrapped in the database check (case_evidence):
        participants' rows before and after, every transaction found on both
        nodes, value conserved, nothing left locked, rows consistent, and the
        fullnode's verdict on each transaction."""
        run_events.case_started(unit, tid, paced)
        started = time.time()
        state = case_evidence.begin(unit)
        try:
            if paced:
                rc.reset_pacing(*(unit.quorums + unit.senders + unit.receivers))
                result = cases_map[tid](unit.ctx, ci)
            else:
                with rc.unpaced(*_unpaced_dids(unit)):
                    result = cases_map[tid](unit.ctx, ci)
        except Exception as e:
            result = (False, "exception", "{}: {}".format(type(e).__name__, e))
        elapsed = round(time.time() - started, 2)
        try:
            result, evidence = case_evidence.finish(tid, unit, state, result)
        except Exception as e:
            evidence = {"case": tid, "unit": unit.name, "checks": [
                {"check": "db", "ok": None,
                 "detail": "DB check could not run: {}: {}".format(type(e).__name__, e)}]}
        return result, evidence, elapsed

    def _run_cases(unit):
        for tid in unit.cases:
            ci = resolve_ci(tid, master, case_info)
            # The same case WITHOUT delay (the product's raw behaviour), then,
            # if it made transfers, WITH delay - and the fullnode's verdict on
            # each transfer is compared between the two (case_evidence.combine).
            result, evidence, elapsed = _run_once(unit, tid, ci, paced=False)
            if tid in NO_DELAY_RERUN:
                passed, actual, note = result
                result = (passed, actual, ((note or "") + " | " if note else "") +
                          "run once, without delay: time-boxed, so a delayed rerun "
                          "would spend its window waiting for the fullnode")
            elif case_evidence.FULLNODE_HOSTS and case_evidence.made_transfers(evidence):
                r2, e2, t2 = _run_once(unit, tid, ci, paced=True)
                result, evidence = case_evidence.combine(tid, result, evidence, r2, e2)
                elapsed = round(elapsed + t2, 2)
            CASE_EVIDENCE.append(evidence)
            unit.rows.append((tid, result, elapsed, ci))

            status = ("SKIP" if (isinstance(result[0], str) and result[0] == SKIP)
                      else "PASS" if result[0] is True else "FAIL")
            run_events.case_result(unit, tid, status, result, elapsed, evidence)
            say("  [{:<4}] {:<11} {:>7.2f}s  {}".format(status, tid, elapsed, result[1]))

    workers = max(1, len(pool_nodes))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        while pending or running:
            for unit in list(pending):
                if any(u.exclusive for u in running.values()):
                    break               # an exclusive unit runs with nothing beside it
                if unit.spec["last"] and any(not u.spec["last"]
                                             for u in pending + list(running.values())):
                    break               # "last" waits for every ordinary unit
                free = [n for n in pool_nodes if n["host"] not in busy]
                if not unit.needs_nodes:
                    now, ever, why = (not running) if unit.exclusive else True, True, ""
                else:
                    now, ever, why = fits(unit, free, pool_nodes)
                    if unit.exclusive and running:
                        now = False
                if not ever:
                    pending.remove(unit)
                    skip_all(unit, "not enough participants", why)
                    continue
                if now:
                    try:
                        allocate(unit, free, args.port)
                    except Exception as e:
                        pending.remove(unit)
                        skip_all(unit, "participants could not be chosen",
                                 "{}: {}".format(type(e).__name__, e))
                        continue
                    busy |= unit.hosts
                    pending.remove(unit)
                    running[ex.submit(run_one, unit)] = unit
                    continue
                if unit.exclusive:
                    break               # nothing queued behind it may start first
            if not running:
                if pending:             # nothing can ever start - should not happen
                    for unit in pending:
                        skip_all(unit, "not scheduled", "no free participants")
                    pending = []
                break
            done, _ = wait(list(running), return_when=FIRST_COMPLETED)
            for fut in done:
                unit = running.pop(fut)
                busy -= unit.hosts
                exc = fut.exception()
                if exc:
                    skip_all(unit, "runner error", "{}: {}".format(type(exc).__name__, exc))
                run_events.unit_done(unit, exc)


# ---------------------------------------------------------------------------
# Fleet ledger
#
# Tokens are finite and nobody returns them at the end of a cycle, so per-DID
# balances move constantly and mean little on their own. What must hold is the
# total across the fleet AND the faucet: value only leaves through sinks we
# accept - RBT burnt to mint FT, and collateral committed to a contract,
# neither of which has a path back.
#
#     free + locked + pledged  ==  previous total  -  new sinks
#
# The faucet DID and its quorum are counted, so a faucet draw is a move inside
# the ledger, not new value. Anything else is a leak or a creation.
#
# Burnt (8) is deliberately in neither group: a split burns the parent and
# creates children worth the same, so counting it as a sink would report every
# split as new value.
# ---------------------------------------------------------------------------

LEDGER_PATH = os.path.join(HERE, "fleet-ledger.json")

_SPENDABLE = ("free", "locked", "pledged")
_SINKS = ("committed", "burnt_for_ft")


def fleet_ledger(fleet, label):
    """Sum the fleet, compare with the previous run, return (totals, lines)."""
    if not db.available():
        return None, ["fleet ledger skipped: no database driver"]

    status_name = {db.FREE: "free", db.LOCKED: "locked", db.PLEDGED: "pledged",
                   db.QUORUM_PLEDGED: "pledged", db.COMMITTED: "committed",
                   db.BURNT_FOR_FT: "burnt_for_ft"}
    totals = dict((n, 0.0) for n in status_name.values())
    counted, unreachable = 0, []
    fleet = list(fleet)
    # The faucet DID and the faucet quorum, each on its own machine, are read
    # over their APIs. They only ever send, so they hold nothing in the sink
    # statuses.
    for did, host, port in ((rc.FAUCET_DID, rc.FAUCET_HOST, rc.FAUCET_PORT),
                            (rc.FAUCET_QUORUM_DID, rc.FAUCET_QUORUM_HOST, rc.FAUCET_QUORUM_PORT)):
        if did and all(e["did"] != did for e in fleet):
            fleet.append({"host": host, "did": did, "api_port": port})

    def read(e):
        """(per-status totals, None) or (None, why it could not be read)."""
        try:
            if e.get("api_port"):
                ok, d, note = rc.get_rbt_balance_detail(e["host"], e["did"], e["api_port"])
                if not ok or not d:
                    raise RuntimeError(note)
                return {db.FREE: (d["balance"], 0), db.LOCKED: (d["locked"], 0),
                        db.PLEDGED: (d["pledged"], 0)}, None
            return db.wallet_totals(e["host"], e["did"]), None
        except Exception as exc:
            return None, "{} ({})".format(e["host"], type(exc).__name__)

    # ~1,000 DIDs: read in parallel, one database connection each.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(40, max(1, len(fleet)))) as ex:
        readings = list(ex.map(read, fleet))
    for per_status, why in readings:
        if per_status is None:
            unreachable.append(why)
            continue
        counted += 1
        for status, (value, _rows) in per_status.items():
            name = status_name.get(status)
            if name:
                totals[name] += value
    for k in totals:
        totals[k] = round(totals[k], 3)
    totals["spendable"] = round(sum(totals[k] for k in _SPENDABLE), 3)
    totals["sunk"] = round(sum(totals[k] for k in _SINKS), 3)

    lines = ["fleet ledger over {} DID(s): spendable {:.3f} "
             "(free {:.3f}, locked {:.3f}, pledged {:.3f}), "
             "sunk {:.3f} (committed {:.3f}, burnt-for-ft {:.3f})".format(
                 counted, totals["spendable"], totals["free"], totals["locked"],
                 totals["pledged"], totals["sunk"], totals["committed"],
                 totals["burnt_for_ft"])]
    if unreachable:
        lines.append("  {} DID(s) could not be read, so these totals are "
                     "incomplete: {}".format(len(unreachable), ", ".join(unreachable[:5])))

    history = []
    if os.path.exists(LEDGER_PATH):
        try:
            with open(LEDGER_PATH, encoding="utf-8") as fh:
                history = json.load(fh).get("runs", [])
        except Exception:
            history = []

    members = sorted(e["did"] for e in fleet)
    prev = history[-1] if history else None
    if prev and prev.get("members") and prev["members"] != members:
        # A host that dropped out of the pool takes its balance with it, which
        # would read as a leak. Only the same set of DIDs is comparable.
        lines.append("  not compared with {}: the set of DIDs changed ({} -> {})".format(
            prev.get("label", "the last run"), len(prev["members"]), len(members)))
        prev = None
    if prev and not unreachable:
        moved_sink = round(totals["sunk"] - prev["totals"].get("sunk", 0), 3)
        moved_spend = round(totals["spendable"] - prev["totals"].get("spendable", 0), 3)
        # Value that left the spendable pool without landing in a sink has gone
        # somewhere nothing accounts for.
        unexplained = round(moved_spend + moved_sink, 3)
        lines.append("  since {}: spendable {:+.3f}, sinks {:+.3f}".format(
            prev.get("label", "the last run"), moved_spend, moved_sink))
        if unexplained < -0.001:
            lines.append("  UNEXPLAINED {:+.3f} RBT - value left the fleet without being "
                         "burnt or committed. That is a leak, not wear and "
                         "tear.".format(unexplained))
        elif unexplained > 0.001:
            lines.append("  NEW VALUE {:+.3f} RBT appeared. The faucet is inside the "
                         "ledger, so unless the faucet DID minted since the last run, "
                         "tokens are being created somewhere they should not "
                         "be.".format(unexplained))
        else:
            lines.append("  conserved: every RBT that left the spendable pool is "
                         "accounted for by a sink")

    history.append({"label": label,
                    "at": datetime.datetime.now().isoformat(timespec="seconds"),
                    "dids": counted, "members": members, "totals": totals})
    try:
        with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
            json.dump({"runs": history[-50:]}, fh, indent=2)
    except Exception as exc:
        lines.append("  could not write {}: {}".format(LEDGER_PATH, exc))
    return totals, lines


class CatalogueReport:
    """Per-Test-ID results, with SKIP tracked separately from PASS/FAIL."""

    def __init__(self):
        self.rows = []

    def add(self, ci, result, elapsed, evidence=None):
        """Record a result produced by a unit, without re-running it. The DB
        and fullnode readings get their own short columns so Note stays the
        case's own reason; the full readings are in the evidence file."""
        passed, actual, note = result
        if isinstance(passed, str) and passed == SKIP:
            status = "SKIP"
        elif passed is True:
            status = "PASS"
        else:
            status = "FAIL"
        evidence = evidence or {}
        known = case_evidence.short_known_bugs(evidence)
        self.rows.append({
            "test_id": ci.test_id, "case": ci.case, "expected": ci.expected,
            "status": status, "actual": actual,
            "note": (known + " " + (note or "")).strip() if known else (note or ""),
            "db": case_evidence.short_db(evidence) if evidence else "-",
            "fullnode": case_evidence.short_fullnode(evidence) if evidence else "-",
            "seconds": elapsed, "db_checks": evidence.get("checks", []),
        })
        return status

    def record(self, ci, fn, ctx):
        start = time.time()
        try:
            passed, actual, note = fn(ctx, ci)
        except Exception as e:
            passed, actual, note = False, "exception", "{}: {}".format(type(e).__name__, e)
        elapsed = round(time.time() - start, 2)

        # `==` not `is`: SKIP is defined independently in each case module, and
        # Python does not guarantee identity for equal strings across modules.
        if isinstance(passed, str) and passed == SKIP:
            status = "SKIP"
        elif passed is True:
            status = "PASS"
        else:
            status = "FAIL"

        self.rows.append({
            "test_id": ci.test_id, "case": ci.case, "expected": ci.expected,
            "status": status, "actual": actual, "note": note, "seconds": elapsed,
        })
        print("  [{:<4}] {:<9} {:>6.2f}s  {}{}".format(
            status, ci.test_id, elapsed, actual, ("  -- " + note) if note else ""))
        return status

    def save(self, asset, meta, timing_ids, title=None):
        paths = rc.new_report_paths("catalogue_{}".format(asset), ("pdf", "json"))
        report_builder.build_json(paths["json"], meta, self.rows)
        pdf_ok = report_builder.build_pdf(
            paths["pdf"], title or "Rubix Lab - {} Catalogue Run".format(asset.upper()),
            meta, self.rows, timing_ids)

        p = sum(1 for r in self.rows if r["status"] == "PASS")
        f = sum(1 for r in self.rows if r["status"] == "FAIL")
        s = sum(1 for r in self.rows if r["status"] == "SKIP")
        attempted = p + f
        print("\n{} passed, {} failed, {} skipped (of {}).".format(p, f, s, len(self.rows)))
        print("Pass rate of ATTEMPTED cases: {:.1f}% ({}/{}) - skipped are not counted "
              "as passes.".format((100.0 * p / attempted) if attempted else 0.0, p, attempted))
        if f:
            print("FAILED: {}".format(
                ", ".join(r["test_id"] for r in self.rows if r["status"] == "FAIL")))
        if s:
            print("SKIPPED (not attempted, reason in the report's Note column): {}".format(
                ", ".join(r["test_id"] for r in self.rows if r["status"] == "SKIP")))
        print("\nJSON: {}".format(paths["json"]))
        if pdf_ok:
            print("PDF : {}".format(paths["pdf"]))
        else:
            print("PDF : not written - reportlab missing. Results are safe in the JSON "
                  "above. Install with: sudo apt install -y python3-reportlab")
        return paths["json"], (paths["pdf"] if pdf_ok else "")


def load_fleet(hosts_path=DEFAULT_HOSTS):
    """hosts.txt -> (hosts, pool node specs from fleet.py), with the faucet and
    the fullnodes pointed at the machines the roles name. The setup every
    runner shares (test_runner, smoke_test, custom/unpledged_spend)."""
    hosts = fleet_inventory.load_hosts(hosts_path)
    rc.use_fleet_roles(hosts)
    case_evidence.FULLNODE_HOSTS = list(rc.FULLNODE_HOSTS)
    return hosts, fleet_inventory.fleet(hosts)


def discover_pool(specs, timeout):
    """Every reachable pool node and ALL of its DIDs. `specs` are fleet.py
    nodes; each node returned is {"host": "ip:port", "machine", "slot",
    "did_target", "dids": [...]}.

    DIDs are fixed: this never creates one. A node with no DID is left out and
    named, because creating one here would give a node an identity nobody
    set up (CreateDID has no idempotency, core/did.go). A node holding a
    different number than the layout says still takes part, and is named.

    Then every DID is announced (register + signature), so a node that missed
    an announcement learns it now - in parallel across nodes, one DID at a
    time within a node: one after another, ~1,000 of them took tens of minutes.
    """
    from concurrent.futures import ThreadPoolExecutor

    faucet_dids = {rc.FAUCET_DID, rc.FAUCET_QUORUM_DID} - {""}

    def check(spec):
        reachable, dids, note = rc.get_dids(spec["host"], timeout=timeout)
        return spec, reachable, list(dids or []), note

    with ThreadPoolExecutor(max_workers=min(40, max(1, len(specs)))) as ex:
        results = list(ex.map(check, specs))

    nodes, excluded, off_target = [], [], []
    for spec, reachable, dids, note in results:
        host = spec["host"]
        if not reachable:
            excluded.append("{} - unreachable ({})".format(host, note))
        elif not dids:
            excluded.append("{} - no DID (DIDs are created by dids-to-excel.py, never by "
                            "the runner)".format(host))
        elif faucet_dids & set(dids):
            excluded.append("{} - holds the faucet or faucet quorum DID".format(host))
        else:
            if len(dids) != spec["did_target"]:
                off_target.append("{} ({} of {})".format(host, len(dids), spec["did_target"]))
            nodes.append({"host": host, "machine": spec["machine"], "slot": spec["slot"],
                          "did_target": spec["did_target"], "dids": dids})
    if off_target:
        print("  note: {} node(s) hold a different number of DIDs than node-layout.txt "
              "says - used as they are: {}".format(len(off_target), ", ".join(off_target[:10])))

    def announce(n):
        for did in n["dids"]:
            rc.announce_did(n["host"], did)

    if nodes:
        with ThreadPoolExecutor(max_workers=min(40, len(nodes))) as ex:
            list(ex.map(announce, nodes))
        time.sleep(ANNOUNCE_SETTLE_SECONDS)
    return nodes, excluded


def fullnode_reason_key(reason):
    """A fullnode's rejection reason with the IDs in it masked, so the same
    failure groups across transactions. Contract/NFT IDs are base58 "Qm..."
    hashes. The web UI groups the fullnodes' reasons with this too."""
    return re.sub(r"bafy\w+|Qm[1-9A-HJ-NP-Za-km-z]{44}|[0-9a-f]{16,}|\b\d+_\d+(_\d+)?\b",
                  "<id>",
                  (reason or "").replace("failed to validate transaction:", "")).strip()[:160]


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--suite", default="",
                   help="run a named selection from suites/<name>.txt. Sets --cases, "
                        "--only and --report-name for you.")
    p.add_argument("--cases", default="master",
                   help="case module to run (default: master, which loads every asset "
                        "module).")
    p.add_argument("--only", default="",
                   help="comma-separated Test IDs to run. A trailing * is a prefix "
                        "match, e.g. --only 'SC-C-*,GEN-IN-19'")
    p.add_argument("--report-name", default="",
                   help="label for the report file (default: the --cases value)")
    p.add_argument("--hosts", default=DEFAULT_HOSTS)
    # Nodes are addressed "ip:port" (fleet.py), which carries each node's own
    # port; this is only the default for a bare-IP call.
    p.add_argument("--port", type=int, default=rc.DEFAULT_PORT, help=argparse.SUPPRESS)
    p.add_argument("--quorum-floor", type=int, default=100,
                   help="RBT a quorum is topped up to before its unit starts, unless "
                        "the unit says otherwise. Cases top the signing quorum up "
                        "further when a transfer needs more (a quorum must pledge >= "
                        "the transfer value, core/consensus/checks.go).")
    p.add_argument("--rbt-amount", type=float, default=1)
    p.add_argument("--ft-count", type=int, default=10)
    p.add_argument("--ft-token-count", type=int, default=1)
    # Scale knobs. Defaults are the catalogue's sizes; lower one only for a
    # quick pass. The value actually used is stated in the case's report row,
    # so a reduced run is never mistaken for the full one.
    p.add_argument("--quorum-ladder", default="10,20,50,70,90,100,150,200,220",
                   help="QCAP-01: concurrent senders through one quorum, rung by rung "
                        "(climbed twice: whole RBT, then a fractional amount)")
    p.add_argument("--ladder-stop", type=float, default=50.0,
                   help="stop climbing a ladder once a rung's pass rate falls below this "
                        "percent (default 50)")
    p.add_argument("--ladder-settle", type=int, default=15,
                   help="seconds between ladder rungs, so credits and unpledges land")
    p.add_argument("--whole-wallet", type=int, default=3000,
                   help="WALLET-01: whole tokens in the big wallet (default 3000)")
    p.add_argument("--parts-target", type=int, default=600,
                   help="WALLET-01: part tokens the fragmented wallet collects (default 600)")
    p.add_argument("--asset-hops", type=int, default=6,
                   help="FT-01 / NFT-01 / SC-01: hops each asset makes (default 6)")
    p.add_argument("--race-receivers", type=int, default=20,
                   help="RACE-01: simultaneous receivers one token is raced to (default 20)")
    p.add_argument("--load-ladder", default="10,20,50,100,200,300,500,750,988",
                   help="LOAD-01: DIDs transacting at once, rung by rung")
    p.add_argument("--chain-hops", type=int, default=200,
                   help="CHAIN-01: hops in the chain, each a new quorum (default 200)")
    p.add_argument("--scale", type=float, default=1.0,
                   help="multiplier for the asset cases' volume (default 1.0)")
    p.add_argument("--fleet-total", type=float, default=400000.0,
                   help="AUDIT: RBT the faucet minted, which the whole fleet must always "
                        "hold (default 400000 - 200,000 each for the faucet DID and quorum)")
    p.add_argument("--version-label", default="",
                   help="manual fleet build label. Only used without --collect-versions.")
    p.add_argument("--collect-versions", action="store_true",
                   help="SSH each pool machine and record every node's build")
    p.add_argument("--ssh-user", default="rubix", help="SSH user for --collect-versions")
    p.add_argument("--events", default="", metavar="FILE",
                   help="also write the run as JSON lines to FILE as it goes, for the web UI "
                        "(run_events.py). Off by default.")
    p.add_argument("--spec", default="", metavar="FILE",
                   help="--cases custom: the scenarios to run, as JSON (the web UI's Custom "
                        "run writes one; custom/custom_cases.py has an example)")
    return p


def main():
    args = build_parser().parse_args()

    # One run on the fleet at a time: stops here if another run holds it.
    run_lock.take("test_runner.py", by="the web UI" if args.events else "a terminal",
                  events=os.path.abspath(args.events) if args.events else "")
    log_path = rc.start_log("test_runner")
    run_lock.note(log=log_path)
    run_events.start(args.events)

    if args.suite:
        patterns, suite_modules = load_suite(args.suite)
        if not args.only:
            args.only = ",".join(patterns)
        args.cases = ",".join(suite_modules) or args.cases
        if not args.report_name:
            args.report_name = args.suite

    module_names = [m.strip() for m in args.cases.split(",") if m.strip()]
    if len(module_names) != 1:
        sys.exit("ERROR: pass one case module (normally just 'master').")
    module = load_case_module(module_names[0])
    # A module built from a spec (custom/custom_cases.py) has no cases until
    # it is given one.
    if hasattr(module, "configure"):
        if not args.spec:
            sys.exit("ERROR: --cases {} runs the scenarios in a spec - pass --spec FILE "
                     "(the web UI's Custom run writes one).".format(module_names[0]))
        try:
            module.configure(args.spec)
        except ValueError as e:
            sys.exit("ERROR: the spec {} cannot run: {}".format(args.spec, e))

    order = list(module.ORDER)
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        exact = {t for t in wanted if not t.endswith("*")}
        prefixes = tuple(t[:-1] for t in wanted if t.endswith("*"))

        def selected(tid):
            return tid in exact or (prefixes and tid.startswith(prefixes))

        order = [t for t in order if selected(t)]
        missing = exact - set(module.CASES)
        if missing:
            sys.exit("ERROR: unknown Test ID(s): {}".format(", ".join(sorted(missing))))
        empty = [p_ + "*" for p_ in prefixes if not any(t.startswith(p_) for t in module.CASES)]
        if empty:
            sys.exit("ERROR: these patterns matched no case: {}".format(", ".join(empty)))
    if not order:
        sys.exit("ERROR: nothing to run.")

    case_info = getattr(module, "CASE_INFO", {}) or {}
    master = load_master()
    units = build_units(module, order)
    run_events.run_started(args, module_names[0], order, units, log_path,
                           spec=getattr(module, "SPEC", None))

    # Every verdict rests on database readings; without the driver there would
    # be no evidence, so do not run at all.
    if not db.available():
        sys.exit("ERROR: psycopg2 is not installed - every case is checked against the "
                 "nodes' databases.\n       sudo apt install -y python3-psycopg2")

    # hosts.txt roles decide where the faucet, its quorum and the fullnodes are.
    _all_hosts, specs = load_fleet(args.hosts)
    # Every RBT a case spends comes from the faucet, so a misconfigured faucet
    # would fail every case on funding. Stop here instead.
    faucet_ok, faucet_note = rc.faucet_ready()
    run_events.phase("faucet", faucet_ok, faucet_note)
    if not faucet_ok:
        sys.exit("ERROR: faucet not ready - {}".format(faucet_note))
    print("Faucet: {}... on {}:{}, quorum {}... on {}:{} - {}".format(
        rc.FAUCET_DID[:16], rc.FAUCET_HOST, rc.FAUCET_PORT, rc.FAUCET_QUORUM_DID[:16],
        rc.FAUCET_QUORUM_HOST, rc.FAUCET_QUORUM_PORT, faucet_note))
    print("Fullnodes: {}".format(", ".join(case_evidence.FULLNODE_HOSTS) or
                                 "none in hosts.txt - fullnode acceptance not checked"))

    print()
    print("== Fleet: reachability + DIDs ==")
    run_events.phase("discover", None, "reading every node and announcing its DIDs")
    pool_nodes, excluded = discover_pool(specs, rc.DEFAULT_TIMEOUT)
    fleet = [{"host": n["host"], "did": d} for n in pool_nodes for d in n["dids"]]
    run_events.pool(specs, pool_nodes, fleet, excluded)
    print("{} node(s) on {} pool machine(s) in hosts.txt, {} usable with {} DID(s), "
          "{} excluded".format(len(specs), len(set(s["machine"] for s in specs)),
                               len(pool_nodes), len(fleet), len(excluded)))
    for line in excluded:
        print("  excluded: {}".format(line))
    if not pool_nodes:
        sys.exit("ERROR: no usable nodes.")

    print("\n== {} case(s) in {} unit(s) ==".format(len(order), len(units)))
    print("Each unit gets its own participants when enough nodes are free; "
          "output is interleaved, the report is in catalogue order.")
    started = time.time()
    started_at = datetime.datetime.now()
    NO_DELAY_RERUN.update(getattr(module, "NO_DELAY_RERUN", None) or ())
    run_units(units, pool_nodes, fleet, module.CASES, master, case_info, args)
    duration = time.time() - started

    report = CatalogueReport()
    by_id = {}
    for u in units:
        for tid, result, elapsed, ci in u.rows:
            by_id[tid] = (ci, result, elapsed)
    evidence_by_id = dict((e["case"], e) for e in CASE_EVIDENCE)
    for tid in order:
        if tid in by_id:
            ci, result, elapsed = by_id[tid]
            report.add(ci, result, elapsed, evidence_by_id.get(tid))

    # Which DIDs each unit actually used. Without this a finding can only be
    # traced back to a machine while the terminal scrollback is still open.
    participants = "; ".join("{} [{}]: {}".format(u.name, ",".join(u.cases), u.describe())
                             for u in units if u.hosts)

    run_events.phase("report", None, "evidence, fleet ledger and report")
    version_rows = []
    if args.collect_versions:
        print("\nCollecting per-node versions over SSH...")
        versions = rc.collect_versions(pool_nodes, args.ssh_user)
        distinct = sorted(set(versions.values()))
        version_rows.append(("Version", "uniform: {}".format(distinct[0]) if len(distinct) == 1
                             else "MIXED: " + ", ".join("{}={}".format(h, v)
                                                        for h, v in sorted(versions.items()))))
    else:
        version_rows.append(("Version", args.version_label or
                             "not recorded - re-run with --collect-versions or pass "
                             "--version-label"))

    meta = {
        "asset": args.cases,
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_seconds": round(duration, 1),
        "conditions": [
            ("Run started", started_at.strftime("%Y-%m-%d %H:%M:%S")),
            ("Duration", "{:.0f}s".format(duration)),
            ("Cases run", "{} of {} in the catalogue".format(len(order), len(module.ORDER))),
        ] + version_rows + [
            ("Hosts file", args.hosts),
            ("Pool", "{} node(s) on {} machine(s), {} DID(s); excluded: {}".format(
                len(pool_nodes), len(set(n["machine"] for n in pool_nodes)), len(fleet),
                "; ".join(excluded) or "none")),
            ("Participants", "per unit (node = last octet of 192.168.1.x : API port; "
                             "s=senders r=receivers q=quorums) - " + (participants or "none")),
            ("Funding", "faucet {}... on {}:{}, signed by faucet quorum {}... on {}:{}; "
                        "no RBT minted; shortfall only".format(
                            rc.FAUCET_DID[:16], rc.FAUCET_HOST, rc.FAUCET_PORT,
                            rc.FAUCET_QUORUM_DID[:16], rc.FAUCET_QUORUM_HOST,
                            rc.FAUCET_QUORUM_PORT)),
            ("Quorum floor", "{} RBT before a unit starts, topped up further per "
                             "transfer".format(args.quorum_floor)),
            ("Fullnode pacing", "each DID waits for every fullnode to process its last "
                                "transaction before starting the next{}{}".format(
                                    "" if not rc._PACE["off"] else
                                    " - SWITCHED OFF during the run (see output)",
                                    "" if not rc._UNREADABLE else
                                    " - not waited on (database unreadable): " +
                                    ", ".join(sorted(rc._UNREADABLE)))),
            ("Fullnodes", ", ".join(case_evidence.FULLNODE_HOSTS) or "not checked"),
            ("Scale knobs", "quorum ladder {} | stop below {}% | whole wallet {} | "
                            "parts target {} | asset hops {} | scale {}".format(
                                args.quorum_ladder, args.ladder_stop, args.whole_wallet,
                                args.parts_target, args.asset_hops, args.scale)),
        ],
    }
    if args.only:
        meta["conditions"].insert(3, ("Subset filter", "--only {}".format(args.only)))
    # A custom run says what its scenarios were instead of the catalogue's knobs.
    if hasattr(module, "report_conditions"):
        meta["conditions"] = [c for c in meta["conditions"] if c[0] != "Scale knobs"] + \
            list(module.report_conditions())

    # The raw before/after database readings, next to the report, so a reviewer
    # can check the arithmetic rather than trust the verdict.
    ev_path = rc.new_report_paths(
        (args.report_name or args.cases.replace(",", "-")) + "_db-evidence",
        ("json",))["json"]
    by_case = dict((e["case"], e) for e in CASE_EVIDENCE)
    with open(ev_path, "w", encoding="utf-8") as fh:
        json.dump({"cases": [by_case[t] for t in order if t in by_case],
                   "case_readings": db.EVIDENCE}, fh, indent=2, default=str)
    print("DB evidence for {} case(s) -> {}".format(len(by_case), ev_path))

    # Fullnodes: every rejection this run, grouped by reason, with the
    # fullnodes that gave it, so a pattern (e.g. previous-transaction
    # mismatches on one fullnode only) is visible without opening files.
    reasons, disagreed = {}, set()
    for e in CASE_EVIDENCE:
        for c in e.get("checks", []):
            if c.get("disagree"):
                disagreed.add(e["case"])
            for fn, p in (c.get("per_fullnode") or {}).items():
                for _tid, r in (p.get("rejected") or {}).items():
                    # IDs differ per transaction; mask them so the same failure groups.
                    key = fullnode_reason_key(r)
                    entry = reasons.setdefault(key, (set(), set()))
                    entry[0].add(e["case"])
                    entry[1].add(case_evidence.short_name(fn))
    if reasons:
        print()
        print("Fullnode rejections this run:")
        for r, (cases, by) in sorted(reasons.items(), key=lambda kv: -len(kv[1][0])):
            print("  {} case(s), by {}: {}  <- {}".format(
                len(cases), ", ".join(sorted(by)), r, ", ".join(sorted(cases))))
    if disagreed:
        print("Fullnodes reached DIFFERENT verdicts on the same transaction in: {}".format(
            ", ".join(sorted(disagreed))))

    # Refusals with a known product cause (case_evidence.KNOWN_PRODUCT_BUGS):
    # the cases they touched, so a ladder "limit" or a FAIL they caused is not
    # read as a new finding or used as a baseline.
    known = {}
    for e in CASE_EVIDENCE:
        for run in [e] + e.get("runs", []):
            for why, n in (run.get("known_bug_refusals") or {}).items():
                known.setdefault(why, {})
                known[why][e["case"]] = known[why].get(e["case"], 0) + n
    if known:
        print()
        print("Known product bugs behind refusals this run (not new findings - do not baseline):")
        for label, cases in sorted(known.items(), key=lambda kv: -sum(kv[1].values())):
            print("  {}: {} refusal(s) in {}".format(
                label, sum(cases.values()),
                ", ".join("{} ({})".format(c, n) for c, n in sorted(cases.items()))))
            print("    {}".format(case_evidence.KNOWN_BUG_EXPLANATION.get(label, "")))

    timing_ids = set(getattr(module, "TIMING_CASES", set()))
    print()
    _totals, ledger_lines = fleet_ledger(fleet, args.report_name or args.cases)
    print()
    for line in ledger_lines:
        print(line)

    report_json, report_pdf = report.save(args.report_name or args.cases.replace(",", "-"),
                                          meta, timing_ids, getattr(module, "REPORT_TITLE", None))
    print("Log : {}".format(log_path))
    run_events.run_done(report.rows, duration, report_json, report_pdf, ev_path, log_path,
                        ledger_lines, reasons, disagreed, known)


if __name__ == "__main__":
    main()
