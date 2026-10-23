#!/usr/bin/env python3
"""
custom_cases.py - a run made of scenarios someone describes, not catalogue cases.

The web UI's Run builder ("Custom run") writes a spec and starts

    python3 test_runner.py --cases custom --spec <spec.json> --report-name custom

and the same command works from a terminal with a spec written by hand
(example-spec.json beside this file). Each scenario is one case (CUSTOM-1, ...)
and one unit, so the runner treats it exactly like a catalogue case: it picks
the participants (one DID per node, spread over machines), sets up the quorums
and resets the participant nodes' quorum lists, runs it, and checks every
participant's database rows afterwards. Scenarios whose nodes do not overlap
run side by side, unless one is marked to run alone.

A scenario says:
    asset, operation   RBT transfer
                       FT mint | mint+transfer
                       NFT deploy | deploy+transfer | deploy+execute
                       SC deploy | deploy+execute
    pattern            who sends to whom - PATTERNS below
    senders, receivers, quorums
    amount             RBT per transfer; with amount_max, a random amount
                       (3 decimal places) in [amount, amount_max] each time
    assets             FTs minted / NFTs / contracts per sender
    per_transfer       FTs per FT transfer
    rounds             how many times the transfers repeat - hops for a chain
                       or an NFT transfer, executes for deploy+execute
    concurrency        all at once | batch (N at a time) | sequential
    round_gap          seconds between rounds
    same_node          every DID from one node (the biggest node holds 10)
    rotate_quorum      chain only: a different quorum signs every hop
    alone              run with nothing else beside it
    quorum_fund        RBT each quorum is topped up to (default: worked out)

The lab's rules hold: no DID is ever created; every RBT comes from the faucet
by transfer, and each scenario tops its own participants up to what it needs -
the shortfall only - at the start of every run; a quorum is funded for every
pledge it could hold at once, since a pledge is released only when the
receiver spends onward (core/callback.go:14-19). A scenario fails when an
operation is refused or the end state does not reconcile (RBT balances, FT
counts, NFT owners, contract chains); the runner's database check can fail it
too.

check(spec) normalises a spec and lists its errors and warnings; budget(sc)
works out the operations, the quorum funding and the faucet draw. The web UI's
plan shows both before Start, from this same code.
"""

import json
import math
import os
import random
import string
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(HERE, "..", "master"), os.path.join(HERE, "..", "full-test")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import case_helpers as ch       # noqa: E402
import rubix_client as rc       # noqa: E402

SKIP = "SKIP"
VERSION = 1

ASSETS = ("RBT", "FT", "NFT", "SC")
OPERATIONS = {
    "RBT": ["transfer"],
    "FT": ["mint", "mint+transfer"],
    "NFT": ["deploy", "deploy+transfer", "deploy+execute"],
    "SC": ["deploy", "deploy+execute"],
}
OP_HELP = {
    "RBT:transfer": "RBT transfers by the pattern, repeated for the rounds",
    "FT:mint": "every sender mints an FT series",
    "FT:mint+transfer": "every sender mints an FT series, then sends FTs of it by the pattern",
    "NFT:deploy": "every sender creates and deploys NFTs",
    "NFT:deploy+transfer": "every sender deploys NFTs, then each NFT changes owner hop by hop",
    "NFT:deploy+execute": "every sender deploys NFTs, then executes each of them",
    "SC:deploy": "every sender generates and deploys contracts",
    "SC:deploy+execute": "every sender deploys contracts; receivers subscribe, and the owner "
                         "and the subscribers execute in turn",
}
PATTERNS = {
    "pairs": "sender i pays receiver i (receivers are reused when there are fewer)",
    "fan-out": "every sender pays every receiver",
    "fan-in": "every sender pays the same single receiver",
    "ring": "every sender pays the next sender - no separate receivers",
    "chain": "each sender's amount is passed on hop by hop through the receivers",
}
# What a pattern means where it is not about paying.
PATTERN_HELP = {
    "NFT:deploy+transfer": {
        "pairs": "sender i's NFTs go to receiver i, then on through the receivers",
        "fan-out": "a sender's NFTs are spread over the receivers, then go on through them",
        "fan-in": "every NFT goes to the one receiver",
        "ring": "every NFT goes to the next sender, and on round the senders",
        "chain": "each NFT is passed on hop by hop through the receivers",
    },
    "SC:deploy+execute": {
        "pairs": "receiver i subscribes to sender i's contracts",
        "fan-out": "every receiver subscribes to every contract",
        "fan-in": "one receiver subscribes to every contract",
    },
}
# A new quorum every hop: chains of RBT or FT (each hop resets the holder's list).
ROTATE_KEYS = ("RBT:transfer", "FT:mint+transfer")
# The patterns each operation can use; None = it sends nothing to anyone.
# For SC deploy+execute the pattern says which receivers subscribe to which
# contracts: pairs = receiver i to sender i's, fan-out = every receiver to
# every contract, fan-in = one receiver to all of them.
OP_PATTERNS = {
    "RBT:transfer": ["pairs", "fan-out", "fan-in", "ring", "chain"],
    "FT:mint": None,
    "FT:mint+transfer": ["pairs", "fan-out", "fan-in", "ring", "chain"],
    "NFT:deploy": None,
    "NFT:deploy+transfer": ["pairs", "fan-out", "fan-in", "ring", "chain"],
    "NFT:deploy+execute": None,
    "SC:deploy": None,
    "SC:deploy+execute": ["pairs", "fan-out", "fan-in"],
}
CONCURRENCY = {
    "all": "all at once - every operation of a round waits on one barrier and starts together",
    "batch": "N at a time",
    "sequential": "one at a time",
}
RERUN = {
    "twice": "without delay, then again waiting for the fullnodes before each DID's next "
             "transaction (the lab's default for transacting cases)",
    "once": "once, without delay",
}

DEFAULT_SCENARIO = {
    "name": "", "asset": "RBT", "operation": "transfer", "pattern": "pairs",
    "senders": 2, "receivers": 2, "quorums": 1,
    "amount": 1.0, "amount_max": None, "assets": 1, "per_transfer": 10,
    "rounds": 1, "concurrency": "all", "batch": 10, "round_gap": 0,
    "same_node": False, "rotate_quorum": False, "alone": False, "quorum_fund": None,
}

MAX_SCENARIOS = 20
MAX_DIDS_ON_A_NODE = 10         # systems/node-layout.txt: slot 8 holds 10
MAX_OPS = 20000                 # API operations in one scenario, per run
AT_ONCE_WARN = 300
AT_ONCE_MAX = 1000
VALUE_LIMIT = 5000              # value ladders stop here (lab rule) - a warning
QUORUM_MIN = 50                 # never fund a quorum below this
NFT_VALUE = 1.0                 # the value every NFT / contract operation declares
WAIT_ATTEMPTS = 40              # polls for a credit, an FT count, an owner, a chain
WAIT_DELAY = 1.5

# Set by configure(): what test_runner drives.
SPEC = None
CASES, ORDER, NEEDS, CASE_INFO = {}, [], {}, {}
NO_DELAY_RERUN = set()
REPORT_TITLE = "Rubix Lab - Custom Run"

# A worked example of every asset, in example-spec.json beside this file: the
# form's "load example", what validate_cases.py checks by default, and a spec
# to run from a terminal.
EXAMPLE_PATH = os.path.join(HERE, "example-spec.json")
try:
    with open(EXAMPLE_PATH, encoding="utf-8") as _fh:
        EXAMPLE_SPEC = json.load(_fh)
except (IOError, OSError, ValueError):
    EXAMPLE_SPEC = {"version": VERSION, "label": "example", "rerun": "once",
                    "scenarios": [dict(DEFAULT_SCENARIO, name="rbt pairs")]}


# ---------------------------------------------------------------------------
# The spec: checking it, describing it, what it costs
# ---------------------------------------------------------------------------

def _key(sc):
    return "{}:{}".format(sc["asset"], sc["operation"])


def _patterns(sc):
    return OP_PATTERNS.get(_key(sc))


def _num(text, digits=3):
    out = "{:,.{d}f}".format(float(text), d=digits)
    return out.rstrip("0").rstrip(".") if "." in out else out


def _plural(n, word, many=None):
    return "{} {}".format(n, word if n == 1 else (many or word + "s"))


def _rounds_word(sc):
    if sc["pattern"] == "chain" or _key(sc) == "NFT:deploy+transfer":
        return "hop"
    if sc["operation"] == "deploy+execute":
        return "execute"
    return "round"


def describe(sc):
    """One line saying what the scenario does, for the plan, the report and the
    case text."""
    n, m, q = sc["senders"], sc["receivers"], sc["quorums"]
    key = _key(sc)
    if sc["asset"] == "RBT":
        amount = (_num(sc["amount"]) if not sc["amount_max"] else
                  "{}-{} (random)".format(_num(sc["amount"]), _num(sc["amount_max"])))
        what = "{} RBT".format(amount)
    elif sc["asset"] == "FT":
        what = "{} FTs minted each".format(_num(sc["assets"], 0))
        if key == "FT:mint+transfer":
            what += ", {} per transfer".format(_num(sc["per_transfer"], 0))
    else:
        what = _plural(sc["assets"], "NFT" if sc["asset"] == "NFT" else "contract") + " each"
    pattern = sc["pattern"]
    if pattern is None:
        who = _plural(n, "sender")
    elif pattern == "ring":
        who = "{} in a ring".format(_plural(n, "sender"))
    elif key == "SC:deploy+execute":
        who = "{}, {} subscribing ({})".format(_plural(n, "owner"), _plural(m, "receiver"), pattern)
    else:
        who = "{} -> {} ({})".format(_plural(n, "sender"), _plural(m, "receiver"), pattern)
    bits = ["{} {}".format(sc["asset"], sc["operation"]), who, what]
    if sc["operation"] not in ("mint", "deploy"):
        bits.append(_plural(sc["rounds"], _rounds_word(sc)))
    if sc["pattern"] is not None or sc["operation"] not in ("mint", "deploy") or \
            sc["senders"] > 1:
        bits.append("one at a time" if sc["concurrency"] == "sequential" else
                    "{} at a time".format(sc["batch"]) if sc["concurrency"] == "batch" else
                    "all at once")
    if sc["round_gap"] and sc["rounds"] > 1 and sc["pattern"] != "chain":
        bits.append("{} s between {}s".format(_num(sc["round_gap"], 1), _rounds_word(sc)))
    bits.append(_plural(q, "quorum") + (", a new one every hop" if sc["rotate_quorum"] else ""))
    if sc["same_node"]:
        bits.append("all on one node")
    if sc["alone"]:
        bits.append("runs alone")
    text = ", ".join(bits)
    return "{}: {}".format(sc["name"], text) if sc["name"] else text


def expected(sc):
    return {
        "RBT:transfer": "every transfer goes through, and every participant's RBT moves by "
                        "exactly what it sent and received",
        "FT:mint": "every mint goes through, and every sender holds the FTs it minted",
        "FT:mint+transfer": "every mint and transfer goes through, and every holder shows "
                            "exactly the FTs it should",
        "NFT:deploy": "every NFT is created and deployed",
        "NFT:deploy+transfer": "every deploy and owner change goes through, and each NFT is "
                               "with its last owner only",
        "NFT:deploy+execute": "every deploy and execute goes through",
        "SC:deploy": "every contract is generated and deployed",
        "SC:deploy+execute": "every deploy, subscribe and execute goes through, and every "
                             "subscriber's chain matches the owner's",
    }[_key(sc)] + "; the database check holds"


def _path(sc, i, j, n, m):
    """Where token j of sender i goes, hop by hop: a list of `rounds` positions
    in senders + receivers (0..n-1 senders, n..n+m-1 receivers). Used for
    chains and NFT owner changes, by the run and by the budget alike."""
    pattern, k = sc["pattern"], sc["assets"] if sc["asset"] == "NFT" else 1
    if pattern == "ring":
        first = (i + 1) % n
    elif pattern == "pairs":
        first = n + i % m
    elif pattern == "fan-in":
        first = n
    else:                               # fan-out and chain spread over the receivers
        first = n + (i * k + j) % m
    out = [first]
    while len(out) < sc["rounds"]:
        prev = out[-1]
        out.append((prev + 1) % n if pattern == "ring" else n + (prev - n + 1) % m)
    return out


def _edges(sc, n, m):
    """[(sender index, receiver position)] for one round of a non-chain
    transfer pattern; positions as in _path."""
    pattern = sc["pattern"]
    if pattern == "pairs":
        return [(i, n + i % m) for i in range(n)]
    if pattern == "fan-out":
        return [(i, n + j) for i in range(n) for j in range(m)]
    if pattern == "fan-in":
        return [(i, n) for i in range(n)]
    if pattern == "ring":
        return [(i, (i + 1) % n) for i in range(n)]
    return []


def _subscribers(sc, i, m):
    """Receiver indexes that subscribe to sender i's contracts."""
    if sc["pattern"] == "fan-out":
        return list(range(m))
    if sc["pattern"] == "fan-in":
        return [0]
    return [i % m]


def _ft_value(sc):
    """RBT one FT is worth: the RBT burnt for the series over its count."""
    tokens = int(math.ceil(sc["assets"] / 1000.0))
    return tokens / float(sc["assets"])


def budget(sc, n=None, m=None, q=None):
    """What one run of the scenario does and needs, worked out from its spec:
        ops            API operations (transfers, mints, creates, deploys, ...)
        at_once        the most started together
        value          RBT moved by transfers
        sender_need    RBT each sender is topped up to before a run
        receiver_need  RBT each receiver is topped up to
        quorum_need    the most RBT one quorum could have pledged at once, +10%
        quorum_fund    what each quorum is topped up to (quorum_need, or the
                       spec's quorum_fund)
        draw           faucet draw for one run if every participant held nothing
    `n`, `m`, `q` override the counts (the run passes what it was given).

    Which quorum signs what is worked out the way the runner assigns them:
    prepare_unit gives the participant nodes, senders first, quorum i % Q - so
    sender i signs with quorum i % Q and receiver j with quorum (n + j) % Q.
    On one node every DID shares the node's single quorum list."""
    n = sc["senders"] if n is None else n
    m = sc["receivers"] if m is None else m
    q = max(1, sc["quorums"] if q is None else q)
    r, k = sc["rounds"], sc["assets"]
    key, pattern = _key(sc), sc["pattern"]
    amax = sc["amount_max"] or sc["amount"]
    signed = [0.0] * q

    def sign(pos, value, quorum=None):
        idx = 0 if sc["same_node"] else (quorum if quorum is not None else pos % q)
        signed[idx % q] += value

    ops = at_once = 0
    value = 0.0
    sender_need = receiver_need = 0.0
    if key in ("RBT:transfer", "FT:mint+transfer"):
        per = amax if sc["asset"] == "RBT" else sc["per_transfer"] * _ft_value(sc)
        if key == "FT:mint+transfer":
            ops += n
            at_once = n
            sender_need = int(math.ceil(k / 1000.0))
        if pattern == "chain":
            for i in range(n):
                holder = i
                for hop, pos in enumerate(_path(sc, i, 0, n, m)):
                    sign(holder, per, hop if sc["rotate_quorum"] else None)
                    holder = pos
            ops += n * r
            at_once = max(at_once, 1 if sc["rotate_quorum"] else n)
            moved = n * r
            if sc["asset"] == "RBT":
                sender_need = amax
        else:
            edges = _edges(sc, n, m)
            for _round in range(r):
                for i, _pos in edges:
                    sign(i, per)
            ops += len(edges) * r
            at_once = max(at_once, len(edges))
            moved = len(edges) * r
            if sc["asset"] == "RBT":
                per_sender = len(edges) // max(1, n)
                if pattern == "ring":
                    # a ring sends and receives every round; random amounts can
                    # leave a sender short by up to (max - min) per round
                    sender_need = amax + (amax - sc["amount"]) * r
                else:
                    sender_need = amax * per_sender * r
        value = per * moved if sc["asset"] == "RBT" else 0.0
    elif key == "FT:mint":
        ops, at_once = n, n
        sender_need = int(math.ceil(k / 1000.0))
    elif sc["asset"] == "NFT":
        ops = 2 * n * k                         # create + deploy
        at_once = n * k
        for i in range(n):
            sign(i, NFT_VALUE * k)
        if key == "NFT:deploy+transfer":
            ops += n * k * r
            for i in range(n):
                for j in range(k):
                    holder = i
                    for pos in _path(sc, i, j, n, m):
                        sign(holder, NFT_VALUE)
                        holder = pos
            # a receiver passes an NFT on after the first hop
            receiver_need = 2 if r > 1 and pattern != "ring" else 0
        elif key == "NFT:deploy+execute":
            ops += n * k * r
            for i in range(n):
                sign(i, NFT_VALUE * k * r)
        sender_need = 2
    else:                                       # SC
        ops = 2 * n * k                         # generate + deploy
        at_once = n * k
        for i in range(n):
            sign(i, NFT_VALUE * k)
        # a deploy commits its value from the deployer (token status Committed)
        sender_need = NFT_VALUE * k + 2
        if key == "SC:deploy+execute":
            subs_of = [_subscribers(sc, i, m) for i in range(n)]
            per_receiver = [0] * max(1, m)
            for i in range(n):
                ops += k * len(subs_of[i])      # subscribes
                ops += k * r * (1 + len(subs_of[i]))
                sign(i, NFT_VALUE * k * r)
                for j in subs_of[i]:
                    sign(n + j, NFT_VALUE * k * r)
                    per_receiver[j] += k
            sender_need = NFT_VALUE * k * (1 + r) + 2
            receiver_need = NFT_VALUE * max(per_receiver) * r + 2
    quorum_need = max(QUORUM_MIN, int(math.ceil(max(signed) * 1.1)) + 5)
    quorum_fund = quorum_need if sc["quorum_fund"] is None else int(sc["quorum_fund"])
    sender_need = round(sender_need, 3)
    receiver_need = round(receiver_need, 3)
    draw = n * sender_need + m * receiver_need + q * quorum_fund
    return {"ops": ops, "at_once": at_once, "value": round(value, 3),
            "sender_need": sender_need, "receiver_need": receiver_need,
            "quorum_need": quorum_need, "quorum_fund": quorum_fund,
            "signed": [round(s, 3) for s in signed], "draw": round(draw, 3)}


def _int(raw, name, lo, hi, errors):
    try:
        v = int(raw)
        if float(raw) != v:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("{} must be a whole number, not {!r}".format(name, raw))
        return lo
    if v < lo:
        errors.append("{} must be at least {}".format(name, lo))
    elif hi is not None and v > hi:
        errors.append("{} must be at most {}".format(name, hi))
    return v


def _float(raw, name, errors, lo=None):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        errors.append("{} must be a number, not {!r}".format(name, raw))
        return lo or 0.0
    if v != v or v in (float("inf"), float("-inf")):
        errors.append("{} must be a number".format(name))
        return lo or 0.0
    if lo is not None and v < lo:
        errors.append("{} must be at least {}".format(name, lo))
    return v


def _bool(raw):
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return bool(raw)


def _check_scenario(raw):
    """(scenario, errors, warnings) for one scenario of a spec."""
    errors, warnings = [], []
    raw = raw if isinstance(raw, dict) else {}
    ignored = sorted(k for k in raw if k not in DEFAULT_SCENARIO and k not in ("id", "summary"))
    if ignored:
        warnings.append("ignored: " + ", ".join(ignored))
    sc = dict(DEFAULT_SCENARIO)
    sc.update(dict((k, v) for k, v in raw.items() if k in DEFAULT_SCENARIO))
    sc["name"] = str(sc["name"] or "").strip()[:60]

    sc["asset"] = str(sc["asset"] or "").strip().upper()
    if sc["asset"] not in ASSETS:
        errors.append("asset must be one of {}".format(", ".join(ASSETS)))
        sc["asset"] = "RBT"
    sc["operation"] = str(sc["operation"] or "").strip().lower()
    if sc["operation"] not in OPERATIONS[sc["asset"]]:
        errors.append("{} operations are: {}".format(sc["asset"], ", ".join(OPERATIONS[sc["asset"]])))
        sc["operation"] = OPERATIONS[sc["asset"]][0]
    key = _key(sc)

    sc["senders"] = _int(sc["senders"], "senders", 1, 1000, errors)
    sc["receivers"] = _int(sc["receivers"] or 0, "receivers", 0, 1000, errors)
    sc["quorums"] = _int(sc["quorums"], "quorums", 1, 100, errors)
    sc["rounds"] = _int(sc["rounds"], "rounds", 1, 10000, errors)
    sc["assets"] = _int(sc["assets"], "assets per sender", 1, 1000000, errors)
    sc["per_transfer"] = _int(sc["per_transfer"], "FTs per transfer", 1, 1000000, errors)
    sc["batch"] = _int(sc["batch"], "batch size", 1, AT_ONCE_MAX, errors)
    sc["round_gap"] = _float(sc["round_gap"] or 0, "pause between rounds", errors, lo=0)
    for flag in ("same_node", "rotate_quorum", "alone"):
        sc[flag] = _bool(sc[flag])
    sc["concurrency"] = str(sc["concurrency"] or "").strip().lower()
    if sc["concurrency"] not in CONCURRENCY:
        errors.append("concurrency must be one of {}".format(", ".join(CONCURRENCY)))
        sc["concurrency"] = "all"

    # Who takes part. The pattern decides whether there are receivers at all;
    # receivers only warn when the spec itself asked for some.
    allowed = OP_PATTERNS[key]
    pattern = str(sc["pattern"] or "").strip().lower() or None
    if raw.get("receivers") in (None, ""):
        sc["receivers"] = sc["receivers"] if allowed and pattern not in ("ring", "fan-in") else 0
    if allowed is None:
        if sc["receivers"]:
            warnings.append("{} sends nothing to anyone, so its {} - ignored".format(
                key, _plural(sc["receivers"], "receiver")))
        sc["pattern"], sc["receivers"] = None, 0
    elif pattern not in allowed:
        errors.append("{} patterns are: {}".format(key, ", ".join(allowed)))
        sc["pattern"] = allowed[0]
    else:
        sc["pattern"] = pattern
        if pattern == "ring":
            if sc["receivers"]:
                warnings.append("in a ring the senders pay each other - the {} ignored".format(
                    _plural(sc["receivers"], "receiver")))
            sc["receivers"] = 0
            if sc["senders"] < 2:
                errors.append("a ring needs at least 2 senders")
        elif pattern == "fan-in":
            if raw.get("receivers") not in (None, "") and sc["receivers"] != 1:
                warnings.append("fan-in has one receiver - receivers set to 1")
            sc["receivers"] = 1
        elif sc["receivers"] < 1:
            errors.append("{} needs at least one receiver".format(pattern))
    multi_hop = sc["pattern"] == "chain" or key == "NFT:deploy+transfer"
    if multi_hop and sc["pattern"] != "ring" and sc["rounds"] > 1 and sc["receivers"] < 2:
        errors.append("{} hops pass through the receivers, so more than one hop needs at least "
                      "2 receivers".format("chain" if sc["pattern"] == "chain" else "NFT"))

    # Amounts.
    if sc["asset"] == "RBT":
        amount = _float(sc["amount"], "amount", errors, lo=0.001)
        sc["amount"] = round(amount, 3)
        if abs(sc["amount"] - amount) > 1e-9:
            warnings.append("amount {} rounded to {} - 3 decimal places is the network's "
                            "precision (math/math.go)".format(amount, sc["amount"]))
        if sc["amount_max"] not in (None, "", 0):
            hi = _float(sc["amount_max"], "random amount up to", errors, lo=0.001)
            sc["amount_max"] = round(hi, 3)
            if sc["amount_max"] <= sc["amount"]:
                errors.append("random amount: the upper bound must be above the amount")
        else:
            sc["amount_max"] = None
        if (sc["amount_max"] or sc["amount"]) > VALUE_LIMIT:
            warnings.append("{} RBT per transfer is above the lab's usual ceiling of {:,} "
                            "RBT".format(_num(sc["amount_max"] or sc["amount"]), VALUE_LIMIT))
    else:
        sc["amount"], sc["amount_max"] = 1.0, None
    if key == "FT:mint+transfer":
        out = sc["per_transfer"] * (sc["rounds"] * (sc["receivers"] if sc["pattern"] == "fan-out"
                                                     else 1) if sc["pattern"] != "chain" else 1)
        if out > sc["assets"]:
            errors.append("each sender sends {:,} FTs but mints only {:,}".format(out, sc["assets"]))
    if sc["asset"] in ("NFT", "SC") and sc["assets"] > 200:
        warnings.append("{:,} {} per sender is a lot - each is created and deployed one by "
                        "one".format(sc["assets"], "NFTs" if sc["asset"] == "NFT" else "contracts"))

    # Placement and quorums.
    if sc["rotate_quorum"]:
        if sc["pattern"] != "chain" or key not in ROTATE_KEYS:
            warnings.append("a new quorum every hop only applies to an RBT or FT chain - ignored")
            sc["rotate_quorum"] = False
        elif sc["quorums"] < 2:
            warnings.append("a new quorum every hop needs at least 2 quorums - ignored")
            sc["rotate_quorum"] = False
        elif sc["senders"] > 1:
            warnings.append("with a new quorum every hop the chains run one after another: "
                            "each hop resets its holder node's quorum list")
    if sc["same_node"]:
        total = sc["senders"] + sc["receivers"] + sc["quorums"]
        if total > MAX_DIDS_ON_A_NODE:
            errors.append("on one node: {} DIDs needed, the biggest node holds {}".format(
                total, MAX_DIDS_ON_A_NODE))
        if sc["quorums"] > 1:
            warnings.append("on one node every DID shares the node's quorum list, so only the "
                            "first of the {} quorums signs".format(sc["quorums"]))
    if sc["quorum_fund"] in ("", None):
        sc["quorum_fund"] = None
    else:
        sc["quorum_fund"] = _int(sc["quorum_fund"], "quorum funding", 0, None, errors)
    if sc["concurrency"] == "batch" and sc["batch"] == 1:
        sc["concurrency"] = "sequential"

    if not errors:
        b = budget(sc)
        if sc["quorum_fund"] is not None and sc["quorum_fund"] < b["quorum_need"]:
            warnings.append("quorum funding {:,} RBT is below the {:,} its quorum could have "
                            "pledged at once - a refusal for pledge shortage makes the run "
                            "invalid".format(sc["quorum_fund"], b["quorum_need"]))
        if b["ops"] > MAX_OPS:
            errors.append("{:,} operations in one run - keep a scenario under {:,}".format(
                b["ops"], MAX_OPS))
        at_once = b["at_once"] if sc["concurrency"] == "all" else min(b["at_once"], sc["batch"])
        if sc["concurrency"] == "all" and at_once > AT_ONCE_MAX:
            errors.append("{:,} operations at once - the most is {:,}; use N at a time".format(
                at_once, AT_ONCE_MAX))
        elif at_once > AT_ONCE_WARN:
            warnings.append("{:,} operations at once - the controller holds one connection "
                            "for each".format(at_once))
    return sc, errors, warnings


def check(spec):
    """(normalised spec, errors, warnings). A spec is
    {"version": 1, "label": str, "rerun": "twice"|"once", "scenarios": [...]};
    the scenarios get their Test IDs (CUSTOM-1, ...) and a summary line."""
    errors, warnings = [], []
    if isinstance(spec, str):
        try:
            with open(spec, encoding="utf-8") as fh:
                spec = json.load(fh)
        except (IOError, OSError, ValueError) as e:
            return None, ["cannot read the spec {}: {}".format(spec, e)], []
    if not isinstance(spec, dict):
        return None, ["a spec is a JSON object with a list of scenarios"], []
    rerun = str(spec.get("rerun") or "twice").strip().lower()
    if rerun not in RERUN:
        errors.append("rerun must be 'twice' or 'once'")
        rerun = "twice"
    label = "".join(c for c in str(spec.get("label") or "") if c.isprintable()).strip()[:80]
    raw = spec.get("scenarios")
    if not isinstance(raw, list) or not raw:
        errors.append("add at least one scenario")
        raw = []
    if len(raw) > MAX_SCENARIOS:
        errors.append("at most {} scenarios in one run".format(MAX_SCENARIOS))
        raw = raw[:MAX_SCENARIOS]
    scenarios = []
    for i, item in enumerate(raw, 1):
        sc, e, w = _check_scenario(item)
        sc["id"] = "CUSTOM-{}".format(i)
        sc["summary"] = describe(sc)
        errors += ["{}: {}".format(sc["id"], x) for x in e]
        warnings += ["{}: {}".format(sc["id"], x) for x in w]
        scenarios.append(sc)
    return {"version": VERSION, "label": label, "rerun": rerun, "scenarios": scenarios}, \
        errors, warnings


def configure(spec):
    """Load a spec (a dict, or the path of a JSON file) into CASES / ORDER /
    NEEDS / CASE_INFO / NO_DELAY_RERUN, which test_runner then drives.
    Raises ValueError listing the spec's errors."""
    global SPEC
    norm, errors, _warnings = check(spec)
    if errors:
        raise ValueError("; ".join(errors))
    CASES.clear()
    del ORDER[:]
    NEEDS.clear()
    CASE_INFO.clear()
    NO_DELAY_RERUN.clear()
    for sc in norm["scenarios"]:
        tid = sc["id"]
        b = budget(sc)
        CASES[tid] = _make_case(sc)
        ORDER.append(tid)
        NEEDS[tid] = {"senders": sc["senders"], "receivers": sc["receivers"],
                      "quorums": sc["quorums"], "fund": 0, "quorum_fund": b["quorum_fund"],
                      "same_node": sc["same_node"], "exclusive": sc["alone"]}
        CASE_INFO[tid] = (sc["summary"], expected(sc))
        if norm["rerun"] == "once":
            NO_DELAY_RERUN.add(tid)
    SPEC = norm
    return norm


def report_conditions():
    """Rows for the report's run conditions: what the custom run was."""
    if not SPEC:
        return []
    rows = [("Custom run", "{}{}".format(SPEC["label"] + " - " if SPEC["label"] else "",
                                         "each scenario " + ("once, without delay"
                                                             if SPEC["rerun"] == "once" else
                                                             "without delay, then with delay")))]
    for sc in SPEC["scenarios"]:
        b = budget(sc)
        rows.append((sc["id"], "{} | {:,} operations, quorums topped up to {:,} RBT".format(
            sc["summary"], b["ops"], b["quorum_fund"])))
    return rows


def form():
    """What the web UI's Custom run form offers, from this module, so the page
    and the run agree."""
    return {"version": VERSION, "assets": list(ASSETS), "operations": OPERATIONS,
            "op_help": OP_HELP, "patterns": PATTERNS, "op_patterns": OP_PATTERNS,
            "pattern_help": PATTERN_HELP, "rotate": list(ROTATE_KEYS),
            "concurrency": CONCURRENCY, "rerun": RERUN, "defaults": DEFAULT_SCENARIO,
            "limits": {"scenarios": MAX_SCENARIOS, "dids_on_a_node": MAX_DIDS_ON_A_NODE,
                       "ops": MAX_OPS, "at_once": AT_ONCE_MAX, "value": VALUE_LIMIT,
                       "quorum_min": QUORUM_MIN},
            "example": EXAMPLE_SPEC}


# ---------------------------------------------------------------------------
# Running a scenario
# ---------------------------------------------------------------------------

class _Ops(object):
    """Every operation a scenario made: what, whether it went through, how
    long it took, and the node's reason when it did not."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []

    def add(self, kind, ok, secs, msg=""):
        with self.lock:
            self.rows.append((kind, bool(ok), secs, "" if ok else str(msg or "")))
        return ok

    def counts(self):
        with self.lock:
            rows = list(self.rows)
        return len(rows), sum(1 for r in rows if r[1])

    def refusals(self):
        with self.lock:
            rows = [r for r in self.rows if not r[1]]
        out = {}
        for kind, _ok, _s, msg in rows:
            out.setdefault(kind, []).append(msg)
        return ["{} {} failed: {}".format(len(msgs), kind, ch.refusal_summary(msgs, top=2))
                for kind, msgs in out.items()]

    def timings(self):
        with self.lock:
            rows = list(self.rows)
        kinds, out = [], []
        for r in rows:
            if r[0] not in kinds:
                kinds.append(r[0])
        for kind in kinds:
            mine = [r for r in rows if r[0] == kind]
            went = sum(1 for r in mine if r[1])
            secs = [r[2] for r in mine if r[1] and r[2] is not None]
            if secs:
                out.append("{} x{}: p50 {:.2f}s p95 {:.2f}s max {:.2f}s{}".format(
                    kind, len(mine), ch.percentile(secs, 50), ch.percentile(secs, 95), max(secs),
                    "" if went == len(mine) else " ({} failed)".format(len(mine) - went)))
            else:
                out.append("{} x{}: {} went through".format(kind, len(mine), went))
        return out


def _call(fn, *a, **k):
    """(ok, message, result, seconds) for any client call, which return either
    (ok, msg) or (ok, msg, result)."""
    t0 = time.time()
    try:
        out = fn(*a, **k)
    except Exception as e:
        out = (False, "{}: {}".format(type(e).__name__, e), None)
    secs = round(time.time() - t0, 2)
    ok, msg = out[0], out[1] if len(out) > 1 else ""
    return bool(ok), str(msg or ""), out[2] if len(out) > 2 else None, secs


def _execute(sc, jobs, fn, force_sequential=False):
    """fn(job) for every job, by the scenario's concurrency; results in order."""
    jobs = list(jobs)
    if not jobs:
        return []
    mode = "sequential" if force_sequential else sc["concurrency"]
    if mode == "sequential" or len(jobs) == 1:
        return [fn(j) for j in jobs]
    if mode == "batch":
        with ThreadPoolExecutor(max_workers=max(1, min(sc["batch"], len(jobs)))) as ex:
            return list(ex.map(fn, jobs))
    # All at once: every job waits on one barrier, so "50 at once" really is 50
    # at once rather than 50 started over the time it takes to start threads.
    gate = threading.Barrier(len(jobs))

    def gated(job):
        try:
            gate.wait(timeout=120)
        except threading.BrokenBarrierError:
            pass
        return fn(job)
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        return list(ex.map(gated, jobs))


def _poll(check, attempts=WAIT_ATTEMPTS, delay=WAIT_DELAY):
    """check() until it is true, at most `attempts` times: (ok, seconds waited)."""
    t0 = time.time()
    for _ in range(attempts):
        if check():
            return True, round(time.time() - t0, 1)
        time.sleep(delay)
    return bool(check()), round(time.time() - t0, 1)


def _held(entry):
    ok, held, _n = rc.get_rbt_held(entry["host"], entry["did"])
    return held if ok else None


def _free(entry):
    ok, detail, _n = rc.get_rbt_balance_detail(entry["host"], entry["did"])
    return float(detail["balance"]) if ok and detail else None


def _fund(needs, port):
    """Top every (entry, target) up to `target` RBT free from the faucet - the
    shortfall only; a DID keeps what it holds. (ok, why not)."""
    for entry, target in needs:
        if not target:
            continue
        have = _free(entry)
        if have is None:
            return False, "cannot read the balance of {}".format(entry["host"])
        if have + ch.TOL >= target:
            continue
        ok, msg = rc.fund_did(entry["host"], entry["did"], target - have, port)
        if not ok:
            return False, "funding {} to {} RBT: {}".format(entry["host"], _num(target), msg)
    return True, ""


def _stamp():
    return time.strftime("%H%M%S") + "".join(random.choice(string.ascii_lowercase)
                                             for _ in range(3))


def _amount(sc):
    return ch.rand_value(sc["amount"], sc["amount_max"]) if sc["amount_max"] else sc["amount"]


def _reconcile(entries, before, delta, ops_on):
    """Wait until every DID's held RBT is its reading before the transfers plus
    what it sent and received. Problems, as text."""
    want = dict((e["did"], before[e["did"]] + delta.get(e["did"], 0.0)) for e in entries
                if before.get(e["did"]) is not None)
    last = {}

    def settled():
        for e in entries:
            if e["did"] not in want:
                continue
            h = _held(e)
            last[e["did"]] = h
            tol = ch.TOL * (1 + ops_on.get(e["did"], 0))
            if h is None or abs(h - want[e["did"]]) > tol:
                return False
        return True
    ok, _waited = _poll(settled, attempts=20, delay=2)
    if ok:
        return []
    wrong = [(e, last.get(e["did"])) for e in entries if e["did"] in want and
             (last.get(e["did"]) is None or
              abs(last[e["did"]] - want[e["did"]]) > ch.TOL * (1 + ops_on.get(e["did"], 0)))]
    if not wrong:
        return []
    e, h = wrong[0]
    return ["{} participant(s) do not hold what they should, e.g. {} holds {} RBT, expected "
            "{}".format(len(wrong), e["host"], "?" if h is None else _num(h),
                        _num(want[e["did"]]))]


def _run_rbt(sc, ctx, senders, receivers, quorums, ops):
    people = senders + receivers
    pos_entry = dict(enumerate(people))
    lock = threading.Lock()
    before = dict(zip([e["did"] for e in people], ch.run_parallel(_held, people)))
    delta, ops_on, carried = {}, {}, [0.0, 0]

    def moved(frm, to, amount):
        with lock:
            delta[frm["did"]] = delta.get(frm["did"], 0.0) - amount
            delta[to["did"]] = delta.get(to["did"], 0.0) + amount
            for d in (frm["did"], to["did"]):
                ops_on[d] = ops_on.get(d, 0) + 1
            carried[0] += amount
            carried[1] += 1

    n, m = len(senders), len(receivers)
    if sc["pattern"] == "chain":
        def chain(i):
            holder, amount = senders[i], _amount(sc)
            for hop, pos in enumerate(_path(sc, i, 0, n, m)):
                nxt = pos_entry[pos]
                if nxt["did"] == holder["did"]:
                    continue
                if sc["rotate_quorum"]:
                    q = quorums[hop % len(quorums)]
                    ok, msg = rc.quorum_reset(holder["host"], [q["did"]], ctx.port)
                    if not ops.add("quorum switch", ok, None, msg):
                        return
                h0 = _held(nxt)
                ok, msg, _r, secs = _call(rc.initiate_transaction, holder["host"], holder["did"],
                                          nxt["did"], rbt=amount, timeout=ch.TRANSFER_TIMEOUT,
                                          memo="{} chain {} hop {}".format(sc["id"], i + 1, hop + 1))
                if not ops.add("hop", ok, secs, msg):
                    return
                moved(holder, nxt, amount)
                # The next hop spends what this one delivered: wait for it to land.
                if h0 is not None:
                    _poll(lambda: (_held(nxt) or 0.0) >= h0 + amount - ch.TOL, attempts=20, delay=1)
                holder = nxt
        _execute(sc, range(n), chain, force_sequential=sc["rotate_quorum"])
    else:
        edges = _edges(sc, n, m)
        for rnd in range(sc["rounds"]):
            def send(edge):
                i, pos = edge
                frm, to, amount = senders[i], pos_entry[pos], _amount(sc)
                ok, msg, _r, secs = _call(rc.initiate_transaction, frm["host"], frm["did"],
                                          to["did"], rbt=amount, timeout=ch.TRANSFER_TIMEOUT,
                                          memo="{} round {}".format(sc["id"], rnd + 1))
                if ops.add("transfer", ok, secs, msg):
                    moved(frm, to, amount)
            _execute(sc, edges, send)
            if sc["round_gap"] and rnd < sc["rounds"] - 1:
                time.sleep(sc["round_gap"])
    problems = _reconcile(people, before, delta, ops_on)
    return problems, "{} RBT carried by {} transfer(s); {} participants' balances checked".format(
        _num(carried[0]), carried[1], sum(1 for e in people if before.get(e["did"]) is not None))


def _ft_count(entry, name):
    ok, result, _n = rc.get_ft_balance(entry["host"], entry["did"])
    return rc.ft_count_for(result, name) if ok else None


def _run_ft(sc, ctx, senders, receivers, quorums, ops):
    stamp, n, m = _stamp(), len(senders), len(receivers)
    people = senders + receivers
    count, tokens = sc["assets"], int(math.ceil(sc["assets"] / 1000.0))
    lock = threading.Lock()
    expect, series = {}, {}

    def name_of(i):
        return "lab-{}-{}-{}".format(sc["id"].lower(), stamp, senders[i]["did"][-6:])

    def mint(i):
        s = senders[i]
        ok, msg, _r, secs = _call(rc.mint_ft, s["host"], s["did"], name_of(i), count, tokens,
                                  timeout=ch.TRANSFER_TIMEOUT)
        if ops.add("mint", ok, secs, msg):
            with lock:
                series[i] = name_of(i)
                expect[(s["did"], name_of(i))] = count
    _execute(sc, range(n), mint)
    for (did, name), want in list(expect.items()):
        entry = next(e for e in senders if e["did"] == did)
        if not _poll(lambda: _ft_count(entry, name) == want)[0]:
            return ["{} does not show the {:,} FTs it minted".format(entry["host"], want)], \
                "{} of {} series minted".format(len(series), n)

    def move(frm, to, i, amount):
        with lock:
            k_from, k_to = (frm["did"], series[i]), (to["did"], series[i])
            expect[k_from] = expect.get(k_from, 0) - amount
            expect[k_to] = expect.get(k_to, 0) + amount

    def payload(i, amount):
        return [{"ftName": series[i], "numberOfFts": amount, "creatorDID": senders[i]["did"]}]

    per = sc["per_transfer"]
    if sc["operation"] == "mint+transfer":
        pos_entry = dict(enumerate(people))
        if sc["pattern"] == "chain":
            def chain(i):
                if i not in series:
                    return
                holder = senders[i]
                for hop, pos in enumerate(_path(sc, i, 0, n, m)):
                    nxt = pos_entry[pos]
                    if nxt["did"] == holder["did"]:
                        continue
                    if sc["rotate_quorum"]:
                        q = quorums[hop % len(quorums)]
                        ok, msg = rc.quorum_reset(holder["host"], [q["did"]], ctx.port)
                        if not ops.add("quorum switch", ok, None, msg):
                            return
                    have = expect.get((nxt["did"], series[i]), 0)
                    ok, msg, _r, secs = _call(rc.initiate_transaction, holder["host"],
                                              holder["did"], nxt["did"], ft=payload(i, per),
                                              timeout=ch.TRANSFER_TIMEOUT,
                                              memo="{} chain {} hop {}".format(sc["id"], i + 1, hop + 1))
                    if not ops.add("FT hop", ok, secs, msg):
                        return
                    move(holder, nxt, i, per)
                    rc.wait_for_ft_count(nxt["host"], nxt["did"], series[i], have + per,
                                         attempts=WAIT_ATTEMPTS, delay=WAIT_DELAY)
                    holder = nxt
            _execute(sc, range(n), chain, force_sequential=sc["rotate_quorum"])
        else:
            edges = [e for e in _edges(sc, n, m) if e[0] in series]
            for rnd in range(sc["rounds"]):
                def send(edge):
                    i, pos = edge
                    frm, to = senders[i], pos_entry[pos]
                    ok, msg, _r, secs = _call(rc.initiate_transaction, frm["host"], frm["did"],
                                              to["did"], ft=payload(i, per),
                                              timeout=ch.TRANSFER_TIMEOUT,
                                              memo="{} round {}".format(sc["id"], rnd + 1))
                    if ops.add("FT transfer", ok, secs, msg):
                        move(frm, to, i, per)
                _execute(sc, edges, send)
                if sc["round_gap"] and rnd < sc["rounds"] - 1:
                    time.sleep(sc["round_gap"])
    entry_of = dict((e["did"], e) for e in people)
    final = ch.run_parallel(
        lambda kv: (kv[0], kv[1], _poll(lambda: _ft_count(entry_of[kv[0][0]], kv[0][1]) == kv[1])),
        list(expect.items()))
    wrong = [(did, name, want) for (did, name), want, (ok, _w) in final if not ok]
    problems = []
    if wrong:
        did, name, want = wrong[0]
        problems.append("{} holder(s) do not show the FTs they should, e.g. {} should hold "
                        "{:,} of {}".format(len(wrong), entry_of[did]["host"], want, name))
    return problems, "{} series of {:,}; {} holdings checked".format(len(series), count, len(expect))


def _holds_nft(entry, nft_id):
    ok, items, _n = rc.get_nft_balance(entry["host"], entry["did"])
    return ok and nft_id in json.dumps(items)


def _run_nft(sc, ctx, senders, receivers, quorums, ops):
    n, m, k = len(senders), len(receivers), sc["assets"]
    people = senders + receivers
    lock = threading.Lock()
    nfts = []

    def deploy(job):
        i, j = job
        s = senders[i]
        ok, msg, nft_id, secs = _call(rc.create_nft, s["host"], s["did"], os.urandom(64),
                                      os.urandom(64))
        if not ops.add("NFT create", ok and isinstance(nft_id, str), secs, msg):
            return
        ok, msg, _r, secs = _call(rc.nft_transaction, s["host"], s["did"], s["did"], nft_id,
                                  value=NFT_VALUE, data="{} deploy".format(sc["id"]),
                                  timeout=ch.TRANSFER_TIMEOUT)
        if ops.add("NFT deploy", ok, secs, msg):
            with lock:
                nfts.append({"id": nft_id, "i": i, "j": j, "first": s, "holder": s, "moved": 0})
    _execute(sc, [(i, j) for i in range(n) for j in range(k)], deploy)
    nfts.sort(key=lambda t: (t["i"], t["j"]))
    problems = []
    if sc["operation"] == "deploy+execute":
        def execute(t):
            for x in range(sc["rounds"]):
                o = t["first"]
                ok, msg, _r, secs = _call(rc.nft_transaction, o["host"], o["did"], o["did"],
                                          t["id"], value=NFT_VALUE,
                                          data="{} execute {}".format(sc["id"], x + 1),
                                          timeout=ch.TRANSFER_TIMEOUT)
                if not ops.add("NFT execute", ok, secs, msg):
                    return
                if sc["round_gap"] and x < sc["rounds"] - 1:
                    time.sleep(sc["round_gap"])
        _execute(sc, nfts, execute)
    elif sc["operation"] == "deploy+transfer":
        pos_entry = dict(enumerate(people))
        paths = dict((t["id"], _path(sc, t["i"], t["j"], n, m)) for t in nfts)
        live = list(nfts)
        for hop in range(sc["rounds"]):
            def transfer(t):
                holder, nxt = t["holder"], pos_entry[paths[t["id"]][hop]]
                if nxt["did"] == holder["did"]:
                    return True
                ok, msg, _r, secs = _call(rc.nft_transaction, holder["host"], holder["did"],
                                          nxt["did"], t["id"], value=NFT_VALUE,
                                          data="{} hop {}".format(sc["id"], hop + 1),
                                          transfer_ownership=True, timeout=ch.TRANSFER_TIMEOUT)
                if not ops.add("NFT transfer", ok, secs, msg):
                    return False
                # The new owner passes it on only once its own node lists it.
                arrived, waited = _poll(lambda: _holds_nft(nxt, t["id"]))
                ops.add("arrival at new owner", arrived, waited,
                        "not listed by its new owner after {:.0f}s".format(waited))
                t["holder"], t["moved"] = nxt, t["moved"] + 1
                return arrived
            ok_list = _execute(sc, live, transfer)
            live = [t for t, ok in zip(live, ok_list) if ok]
            if sc["round_gap"] and hop < sc["rounds"] - 1:
                time.sleep(sc["round_gap"])
        for t in nfts:
            if not t["moved"]:
                continue
            if not _holds_nft(t["holder"], t["id"]):
                problems.append("NFT {} is not with its last owner {} after {} hop(s)".format(
                    t["id"][:10], t["holder"]["host"], t["moved"]))
            elif t["holder"]["did"] != t["first"]["did"] and _holds_nft(t["first"], t["id"]):
                problems.append("NFT {} still shows with its first owner too".format(t["id"][:10]))
    return problems[:3], "{} of {} NFTs deployed".format(len(nfts), n * k)


def _chain_len(entry, sc_id):
    ok, chain, _n = rc.get_sc_chain(entry["host"], sc_id)
    return len(chain) if ok else None


def _run_sc(sc, ctx, senders, receivers, quorums, ops):
    n, m, k = len(senders), len(receivers), sc["assets"]
    lock = threading.Lock()
    contracts = []

    def deploy(job):
        i, j = job
        s = senders[i]
        t0 = time.time()
        sc_id, err = ch._sc_new_contract(ctx, s)
        if not ops.add("SC generate", bool(sc_id), round(time.time() - t0, 2), err):
            return
        ok, msg, _r, secs = _call(rc.sc_transaction, s["host"], s["did"], sc_id, value=NFT_VALUE,
                                  data="{} deploy".format(sc["id"]), timeout=ch.TRANSFER_TIMEOUT)
        if ops.add("SC deploy", ok, secs, msg):
            with lock:
                contracts.append({"id": sc_id, "i": i, "owner": s, "subs": []})
    _execute(sc, [(i, j) for i in range(n) for j in range(k)], deploy)
    contracts.sort(key=lambda c: c["i"])
    problems = []
    if sc["operation"] == "deploy+execute":
        def run_contract(c):
            for j in _subscribers(sc, c["i"], m):
                sub = receivers[j]
                ok, msg, _r, secs = _call(rc.subscribe_smart_contract, sub["host"], c["id"])
                if ops.add("SC subscribe", ok, secs, msg):
                    c["subs"].append(sub)
            last = [c["owner"]]
            for x in range(sc["rounds"]):
                for entry in [c["owner"]] + c["subs"]:
                    # Every executor builds on its own copy of the chain, so it
                    # waits until that copy has the previous execute.
                    if entry is not last[0]:
                        want = _chain_len(last[0], c["id"]) or 0
                        synced, waited = _poll(lambda: (_chain_len(entry, c["id"]) or 0) >= want)
                        if not ops.add("catch-up before execute", synced, waited,
                                       "{}'s copy still behind after {:.0f}s".format(
                                           entry["host"], waited)):
                            continue
                    ok, msg, _r, secs = _call(rc.sc_transaction, entry["host"], entry["did"],
                                              c["id"], value=NFT_VALUE,
                                              data="{} execute {}".format(sc["id"], x + 1),
                                              timeout=ch.TRANSFER_TIMEOUT)
                    if ops.add("SC execute", ok, secs, msg):
                        last[0] = entry
                if sc["round_gap"] and x < sc["rounds"] - 1:
                    time.sleep(sc["round_gap"])
        _execute(sc, contracts, run_contract)
        diverged = []
        for c in contracts:
            owner_len = _chain_len(c["owner"], c["id"])
            for sub in c["subs"]:
                ok, _w = _poll(lambda: (_chain_len(sub, c["id"]) or 0) >= (owner_len or 0),
                               attempts=20, delay=2)
                if not ok:
                    diverged.append("{} on {}".format(c["id"][:10], sub["host"]))
        if diverged:
            problems.append("{} subscriber copies behind the owner's chain, e.g. {}".format(
                len(diverged), diverged[0]))
    return problems, "{} of {} contracts deployed".format(len(contracts), n * k)


RUNNERS = {"RBT": _run_rbt, "FT": _run_ft, "NFT": _run_nft, "SC": _run_sc}


def _run(sc, ctx):
    senders = list(ctx.senders)
    receivers = [] if sc["pattern"] in (None, "ring") else list(ctx.receivers)
    quorums = list(ctx.quorum_hosts)
    if not senders:
        return SKIP, "the scenario got no senders", ""
    if sc["pattern"] not in (None, "ring") and not receivers:
        return SKIP, "the scenario got no receivers", ""
    down = ch.ensure_quorums(ctx)
    b = budget(sc, len(senders), len(receivers), len(quorums))
    # Top up at the start of every run: the second (paced) run spends again.
    needs = ([(e, b["sender_need"]) for e in senders] +
             [(e, b["receiver_need"]) for e in receivers] +
             [(q, b["quorum_fund"]) for q in quorums])
    ok, why = _fund(needs, ctx.port)
    if not ok:
        return SKIP, "the participants could not be funded from the faucet - not a product " \
                     "result", why
    ops = _Ops()
    t0 = time.time()
    problems, outcome = RUNNERS[sc["asset"]](sc, ctx, senders, receivers, quorums, ops)
    took = time.time() - t0
    total, went = ops.counts()
    refused = ops.refusals()
    note = " | ".join([sc["summary"], "took {:.0f}s".format(took)] + ops.timings() +
                      (["quorum setup failed on " + ", ".join(down)] if down else []))
    if not total:
        return False, "nothing went through - no operation was made", note
    actual = "{:,} of {:,} operations went through; {}".format(went, total, outcome)
    if refused or problems:
        return False, "; ".join((refused + problems)[:4]), note
    return True, actual, note


def _make_case(sc):
    def case(ctx, ci):
        return _run(sc, ctx)
    case.__name__ = "custom_" + sc["id"].lower().replace("-", "_")
    case.__doc__ = sc["summary"]
    return case
