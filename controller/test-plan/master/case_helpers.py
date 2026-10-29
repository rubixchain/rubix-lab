"""case_helpers.py - what more than one case module uses: firing transfers at
the same moment and timing them, summarising a burst, the saved limits, and
the node's own refusal reasons.

Every RBT comes from the faucet (rc.fund_did); nothing here mints RBT.
"""

from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
import random
import string
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = 'SKIP'
SETTLE = 6
TOL = 0.0015


def rand_value(lo, hi):
    """A random amount with 3 decimal places, in [lo, hi].

    Deliberately NOT round numbers like 0.001 / 0.5 / 1.0. Clean denominations
    can take a different path through token selection than an arbitrary value:
    0.5 may split cleanly off a 1.000 token while 0.354 needs several levels
    and leaves awkward change. Testing only tidy values tests the easy path.

    3dp is the network maximum - MinDecimalUnit is 0.001 and FloatPrecision
    ROUNDS at 3dp (math/math.go), so a 4th place would be silently rounded and
    the case would assert against a value the node never saw.

    The value used is reported in every result, so a failure stays reproducible
    even though the input is random.
    """
    v = round(random.uniform(lo, hi), 3)
    return max(v, 0.001)


# Refusals that come from the sender's own tokens and would happen with no load
# at all - both known product bugs (case_evidence.KNOWN_PRODUCT_BUGS): a former
# quorum spending a token it once pledged (core/consensus/checks.go:308), and a
# second-hand part token whose whole-token genesis the sender cannot supply. A
# capacity measure counts them apart, not against what it measures.
SENDERS_OWN_TOKENS = ("failed to get quorum DID for t",
                      "ValidateMinterAllowlist: whole-token genesis fetch failed")
# A sender whose clock is ahead of its quorum's is refused outright
# (core/consensus/checks.go:61): the machines' clocks, not a capacity limit.
CLOCK_AHEAD = ("invalid epoch",)
# What a capacity step counts apart from the limit it measures.
ASIDE = SENDERS_OWN_TOKENS + CLOCK_AHEAD


def send(st, frm, to, set_aside=ASIDE, **kw):
    """One transfer from `frm` to `to`, recorded on step `st` (master/steps.py):
    accepted or refused, the node's reason, how long, the transaction ID.
    Returns (ok, msg, result) like rc.initiate_transaction."""
    kw.setdefault("timeout", TRANSFER_TIMEOUT)
    t0 = time.time()
    ok, msg, res = rc.initiate_transaction(frm["host"], frm["did"], to["did"], **kw)
    txid = res.get("transactionID") if isinstance(res, dict) else None
    st.op(ok, msg, round(time.time() - t0, 2), txid, to=to["did"], set_aside=set_aside)
    return ok, msg, res


def timed(st, fn, *a, **k):
    """An NFT / SC / FT operation (fn returns (ok, msg, result)), recorded on step
    `st`. `to` names the DID whose node must record the transaction."""
    to = k.pop("to", None)
    k.setdefault("timeout", TRANSFER_TIMEOUT)
    t0 = time.time()
    ok, msg, res = fn(*a, **k)
    txid = res.get("transactionID") if isinstance(res, dict) else None
    st.op(ok, msg, round(time.time() - t0, 2), txid, to=to)
    return ok, msg, res


def machine(host):
    return host.rsplit(":", 1)[0]


def whole_free(host, did):
    """token_ids of the DID's Free WHOLE RBT tokens, read from its node's DB."""
    return [t for t, v, _st, _p in db.token_rows(host, did, status=db.FREE) if v >= 1.0 - 1e-9]


def empty_did(entry, whole_only=False):
    """`entry` with its DID swapped for one on the SAME node that holds no Free
    RBT at all (whole_only: no Free whole token; parts are allowed), or None.

    A send of whole RBT takes the lowest whole token_id first
    (core/wallet/token_lock.go), so a wallet that already holds older tokens
    never moves the one it was just handed. Cases that follow ONE token - a
    chain, a one-token race, a pledge released by spending onward - need a
    wallet that holds nothing else. Most 5- and 10-DID nodes have idle DIDs, so
    the node's other DIDs are tried first and the entry's own DID last. The
    node, and so its quorum list, is unchanged."""
    own = entry.get("did")
    for did in [d for d in (entry.get("dids") or []) if d != own] + [own]:
        if not did:
            continue
        try:
            rows = db.token_rows(entry["host"], did, status=db.FREE)
        except Exception:
            continue
        if not [v for _t, v, _st, _p in rows if (v >= 1.0 - 1e-9 or not whole_only)]:
            return dict(entry, did=did)
    return None


def node_reason(msg, limit=300):
    """The node's own reason from a refusal. A quorum refusal arrives wrapped -
    'peer request failed: status=400 body={"referenceId":"",...,"message":"<reason>"}'
    - and cutting that at a fixed length leaves only the wrapper."""
    text = str(msg or "")
    # 400s and 500s carry different JSON bodies ({"referenceId",...,"message",
    # "status"} vs {"status","message","result"}) - parse it when it is whole.
    at = text.find("body=")
    if at >= 0:
        try:
            body = json.loads(text[at + len("body="):].strip())
            if isinstance(body, dict) and body.get("message"):
                return str(body["message"]).strip()[:limit]
        except ValueError:
            pass
    start = text.find('"message":"')
    if start >= 0:
        text = text[start + len('"message":"'):]
        ends = [i for i in (text.find('","status"'), text.find('","result"')) if i >= 0]
        if ends:
            text = text[:min(ends)]
    return text.strip()[:limit]


def refusal_summary(messages, top=3, limit=160):
    """'3x <reason>; 1x <reason>' - refusals grouped by the node's own reason,
    most common first. What a note should say instead of the first N
    characters of each message, which are all the same wrapper."""
    counts = {}
    for m in messages:
        r = node_reason(m, limit) or "(no reason given)"
        counts[r] = counts.get(r, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    return "; ".join("{}x {}".format(n, r) for r, n in ranked[:top])


# ---------------------------------------------------------------------------
# Firing operations at the same moment, and what came back
# ---------------------------------------------------------------------------

# Long enough to see the product's own limits: a quorum gives up on a signer
# after 10 minutes (IndividualQuorumTimeout, core/quorum_initiator.go:44), so a
# shorter client timeout would report our patience, not the product's.
TRANSFER_TIMEOUT = 900


def ensure_quorums(ctx):
    """Re-activate every quorum signer on its own node before a case begins.

    Quorum setup is in memory (core.SetupQuorum loads the signing key into the
    c.qc map, core/quorum_initiator.go) and is lost if the node restarts. The
    runner sets a unit's quorum up once, but FT/NFT/SC share one unit, so a
    quorum that restarts during an earlier, heavy case (e.g. a large FT signing
    load) leaves the next case refused with 'requestPledgeTokenHandler : Quorum
    is not setup'. The call is cheap and harmless when the quorum is already up,
    so each case re-asserts it first. Returns the quorum hosts that could not be
    set up (empty = all good)."""
    failed = []
    for q in (getattr(ctx, "quorum_hosts", None) or []):
        try:
            ok, _msg = rc.quorum_setup(q["host"], q["did"], ctx.port)
        except Exception:
            ok = False
        if not ok:
            failed.append(q["host"])
    return failed


def burst(jobs, timeout=TRANSFER_TIMEOUT):
    """Run every job at the same moment and time each one.

    A job is a dict for rc.initiate_transaction: host, did (initiator), to
    (receiver DID) and any of rbt / ft / nft / smart_contract / memo. All
    threads wait on one barrier and start together, so "50 at once" really is
    50 at once rather than 50 started over the time it takes to spawn them.
    Returns one dict per job, in order: ok, msg, secs, txid.
    """
    if not jobs:
        return []
    gate = threading.Barrier(len(jobs))

    def one(job):
        try:
            gate.wait(timeout=60)
        except threading.BrokenBarrierError:
            pass
        t0 = time.time()
        extra = dict((k, job[k]) for k in ("rbt", "ft", "nft", "smart_contract", "memo") if k in job)
        try:
            ok, msg, result = rc.initiate_transaction(job["host"], job["did"], job["to"],
                                                      timeout=timeout, **extra)
        except Exception as e:
            ok, msg, result = False, "{}: {}".format(type(e).__name__, e), None
        txid = result.get("transactionID") if isinstance(result, dict) else None
        return {"ok": bool(ok), "msg": str(msg or ""), "secs": round(time.time() - t0, 2),
                "txid": txid}

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        return list(ex.map(one, jobs))


def run_parallel(fn, items, workers=None):
    """fn(item) for every item at once; results in order. For the asset
    operations, which are not plain transfers."""
    items = list(items)
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers or min(64, len(items))) as ex:
        return list(ex.map(fn, items))


def percentile(values, p):
    """The p-th percentile (0-100) of `values`, nearest-rank; 0.0 when empty."""
    vals = sorted(values)
    if not vals:
        return 0.0
    k = max(0, min(len(vals) - 1, int(round(p / 100.0 * len(vals) + 0.5)) - 1))
    return vals[k]


def summarize(results, set_aside=()):
    """{n, ok, failed, p50, p95, max, reasons, aside, aside_reasons} for burst()
    results. Times are of the transfers that went through; reasons group the
    refusals by the node's own words.

    Refusals whose message contains a marker in `set_aside` are counted apart
    (aside / aside_reasons) and left out of failed / reasons: a capacity
    measure passes the markers of refusals that come from the sender's own
    tokens, which would happen with no load at all."""
    ok = [r for r in results if r["ok"]]
    refused = [r["msg"] for r in results if not r["ok"]]
    aside = [m for m in refused if any(k in m for k in set_aside)]
    counted = [m for m in refused if m not in aside]
    secs = [r["secs"] for r in ok]
    return {"n": len(results), "ok": len(ok), "failed": len(counted),
            "p50": percentile(secs, 50), "p95": percentile(secs, 95),
            "max": max(secs) if secs else 0.0,
            "reasons": refusal_summary(counted),
            "aside": len(aside), "aside_reasons": refusal_summary(aside)}


def ring(entries):
    """[(entry i, entry i+1)], the last paying the first: every DID sends one
    transfer and receives one, so balances stay level across ladder rungs."""
    n = len(entries)
    return [(entries[i], entries[(i + 1) % n]) for i in range(n)] if n > 1 else []


def ladder(text, ceiling):
    """'10,20,50' -> [10, 20, 50], cut at `ceiling` (what the unit could get)."""
    rungs = sorted(set(int(x) for x in str(text).split(",") if x.strip()))
    return [r for r in rungs if r <= ceiling]


# ---------------------------------------------------------------------------
# Limits one case measures and later cases size themselves by
#
# QCAP-01 finds how many transfers one quorum signs at once; the node- and
# fleet-level cases then give themselves enough quorums that the quorum is
# never what they hit. Kept on the controller between runs, like the ledger.
# ---------------------------------------------------------------------------

LIMITS_PATH = os.path.join(HERE, "..", "full-test", "lab-limits.json")


def load_limits():
    try:
        with open(LIMITS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return {}


def save_limit(key, value, detail=None):
    data = load_limits()
    data[key] = {"value": value, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                 "detail": detail}
    try:
        with open(LIMITS_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except (IOError, OSError):
        pass


def quorum_capacity(default, fractional=False):
    """How many concurrent transfers one quorum signs in full - QCAP-01's last
    measurement, or `default` before it has run. Whole-RBT and fractional
    amounts are measured apart: a fractional pledge makes the quorum split its
    own tokens, which whole amounts never do."""
    key = "quorum_senders_fractional" if fractional else "quorum_senders"
    entry = load_limits().get(key) or {}
    try:
        return max(1, int(entry.get("value") or default))
    except (TypeError, ValueError):
        return default


def quorums_for(n_senders, default_capacity, fractional=False):
    """Quorums needed so that no quorum carries more than its measured capacity."""
    cap = quorum_capacity(default_capacity, fractional)
    return max(1, -(-n_senders // cap))


def _sc_new_contract(ctx, entry):
    """Generate a contract and return (sc_id, error). Does NOT deploy it.

    Both file extensions are checked literally by the server: the binary must
    end .wasm and the source .rs (server/smart_contract.go:70, :101-106).
    """
    import random
    import string
    tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"], wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None
