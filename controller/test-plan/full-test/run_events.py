#!/usr/bin/env python3
"""
run_events.py - a run as machine-readable events, for the web UI (controller/ui).

    python3 test_runner.py --events FILE ...

writes one JSON object per line to FILE as the run goes: its start, the faucet
and fleet checks, each unit with its participants, each case run, every
transaction and FT mint with its result, each case's result with its database
check, and the end with the report paths. The web UI reads the file while the
run goes, so it can show the run live and pick it up again after a page reload
or a restart of the UI itself.

Without --events nothing here does anything - every function returns at once -
so a run from a terminal prints and writes exactly what it did before.

Every line has v (format version), run, seq (1, 2, 3 ... in file order), t
(unix time) and type. TYPES lists the types and the fields each one always
carries; check_file() checks a file against them (validate_cases.py --events).

Transactions are read where every case's database check reads them: each one
the client logs into rubix_client.OPS. While a file is open, OPS also writes an
"op" line per entry, and signed_action is timed so the line says how long the
call took (seconds after any wait for the fullnodes).
"""

import atexit
import json
import os
import threading
import time

VERSION = 1

TYPES = {
    "run_started": ("cases", "order", "units", "knobs", "pid", "log"),
    "phase": ("phase", "ok", "detail"),
    "unit_started": ("unit", "cases", "exclusive", "quorums", "senders", "receivers", "dirty"),
    "case_started": ("unit", "case", "mode"),
    "op": ("unit", "case", "mode", "kind", "host", "initiator", "owner", "rbt", "assets",
           "ok", "secs", "waited", "pacing", "txid", "reason", "known_bug", "funding"),
    "case_result": ("unit", "case", "status", "actual", "note", "seconds", "db", "fullnode",
                    "checks", "known_bugs", "fullnode_buckets"),
    "unit_done": ("unit", "seconds", "error"),
    "run_done": ("status", "passed", "failed", "skipped", "duration", "report_json",
                 "report_pdf", "evidence", "log", "ledger", "fullnode_reasons", "known_bugs"),
}
COMMON = ("v", "run", "seq", "t", "type")

# The knobs a run_started line records - the catalogue's scale settings.
KNOBS = ("quorum_floor", "quorum_ladder", "ladder_stop", "ladder_settle", "whole_wallet",
         "parts_target", "asset_hops", "scale", "fleet_total", "version_label",
         "collect_versions")

_LOCK = threading.Lock()
_S = {"fh": None, "seq": 0, "run": "", "done": False}
_WHO = {}           # did -> (unit, case, mode): who an operation belongs to
_UNIT_T0 = {}       # unit name -> when it started
_TL = threading.local()


def active():
    return _S["fh"] is not None


def start(path):
    """Open FILE (replacing it) and start recording. Does nothing without a path."""
    if not path or active():
        return
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    _S["fh"] = open(path, "w", encoding="utf-8")
    _S["run"] = os.path.splitext(os.path.basename(path))[0]
    _install_tap()
    atexit.register(_at_exit)


def close():
    """Stop recording without a run_done line (validate_cases.py)."""
    with _LOCK:
        fh, _S["fh"] = _S["fh"], None
        _S["done"] = True
    if fh:
        fh.close()


def emit(type_, **fields):
    if not active():
        return
    with _LOCK:
        fh = _S["fh"]
        if fh is None:
            return
        _S["seq"] += 1
        line = {"v": VERSION, "run": _S["run"], "seq": _S["seq"], "t": round(time.time(), 3),
                "type": type_}
        line.update(fields)
        fh.write(json.dumps(line, default=str) + "\n")
        fh.flush()


# ---------------------------------------------------------------------------
# What test_runner calls. Each is one line there and returns at once without
# --events.
# ---------------------------------------------------------------------------

def _entry(e):
    return {"host": e.get("host"), "machine": e.get("machine"), "did": e.get("did")}


def run_started(args, cases, order, units, log):
    if not active():
        return
    emit("run_started", cases=cases, order=list(order), only=getattr(args, "only", ""),
         report_name=getattr(args, "report_name", ""),
         units=[{"name": u.name, "cases": list(u.cases), "exclusive": u.exclusive,
                 "first": bool(u.spec["first"]), "last": bool(u.spec["last"]),
                 "same_node": bool(u.spec["same_node"]), "senders": u.spec["senders"],
                 "receivers": u.spec["receivers"], "quorums": u.spec["quorums"],
                 "fund": u.spec["fund"], "quorum_fund": u.spec["quorum_fund"]} for u in units],
         knobs=dict((k, getattr(args, k, None)) for k in KNOBS), pid=os.getpid(), log=log)


def phase(name, ok, detail="", **extra):
    emit("phase", phase=name, ok=ok, detail=detail, **extra)


def pool(specs, pool_nodes, fleet, excluded):
    if not active():
        return
    emit("phase", phase="discover", ok=bool(pool_nodes),
         detail="{} node(s) usable with {} DID(s), {} excluded".format(
             len(pool_nodes), len(fleet), len(excluded)),
         nodes=len(pool_nodes), machines=len(set(n["machine"] for n in pool_nodes)),
         dids=len(fleet), listed=len(specs), excluded=list(excluded))


def unit_started(unit):
    if not active():
        return
    with _LOCK:
        _UNIT_T0[unit.name] = time.time()
        for e in unit.quorums + unit.senders + unit.receivers:
            _WHO[e["did"]] = (unit.name, None, "prepare")
    emit("unit_started", unit=unit.name, cases=list(unit.cases), exclusive=unit.exclusive,
         quorums=[_entry(e) for e in unit.quorums], senders=[_entry(e) for e in unit.senders],
         receivers=[_entry(e) for e in unit.receivers], dirty=list(unit.dirty_spenders))


def case_started(unit, tid, paced):
    if not active():
        return
    mode = "with delay" if paced else "without delay"
    with _LOCK:
        for e in unit.quorums + unit.senders + unit.receivers:
            _WHO[e["did"]] = (unit.name, tid, mode)
    emit("case_started", unit=unit.name, case=tid, mode=mode)


def case_result(unit, tid, status, result, elapsed, evidence):
    if not active():
        return
    import case_evidence
    passed, actual, note = result
    evidence = evidence or {}
    checks = [{"check": c.get("check"), "ok": c.get("ok"), "detail": c.get("detail"),
               "mode": c.get("mode")} for c in evidence.get("checks", [])]
    known = {}
    for run in [evidence] + list(evidence.get("runs", [])):
        for label, n in (run.get("known_bug_refusals") or {}).items():
            known[label] = known.get(label, 0) + n
    emit("case_result", unit=unit.name, case=tid, status=status, actual=actual, note=note,
         seconds=elapsed,
         db=case_evidence.short_db(evidence) if evidence else "-",
         fullnode=case_evidence.short_fullnode(evidence) if evidence else "-",
         checks=checks, known_bugs=known,
         fullnode_buckets=(evidence.get("fullnode_comparison") or {}).get("counts"))


def unit_done(unit, error):
    if not active():
        return
    with _LOCK:
        t0 = _UNIT_T0.pop(unit.name, None)
        for e in unit.quorums + unit.senders + unit.receivers:
            _WHO.pop(e["did"], None)
    emit("unit_done", unit=unit.name,
         seconds=round(time.time() - t0, 2) if t0 else None,
         error="{}: {}".format(type(error).__name__, error) if error else None)


def run_done(rows, duration, report_json, report_pdf, evidence, log, ledger, reasons,
             disagreed, known):
    if not active():
        return
    emit("run_done", status="finished",
         passed=sum(1 for r in rows if r["status"] == "PASS"),
         failed=sum(1 for r in rows if r["status"] == "FAIL"),
         skipped=sum(1 for r in rows if r["status"] == "SKIP"),
         duration=round(duration, 1), report_json=report_json, report_pdf=report_pdf,
         evidence=evidence, log=log, ledger=list(ledger or []),
         fullnode_reasons=[{"reason": r, "cases": sorted(cases), "by": sorted(by)}
                           for r, (cases, by) in sorted((reasons or {}).items(),
                                                        key=lambda kv: -len(kv[1][0]))],
         disagreed=sorted(disagreed or []), known_bugs=known or {})
    _S["done"] = True


def _at_exit():
    """A run that ends early (sys.exit, an exception) still says so."""
    if active() and not _S["done"]:
        emit("run_done", status="aborted", passed=None, failed=None, skipped=None,
             duration=None, report_json="", report_pdf="", evidence="", log="", ledger=[],
             fullnode_reasons=[], known_bugs={})
    close()


# ---------------------------------------------------------------------------
# Operations, from rubix_client.OPS
# ---------------------------------------------------------------------------

class _Tap(list):
    """rubix_client.OPS, which also writes an op line per entry."""

    def append(self, entry):
        list.append(self, entry)
        try:
            _op(entry)
        except Exception:
            pass


def _install_tap():
    import rubix_client as rc
    if not isinstance(rc.OPS, _Tap):
        rc.OPS = _Tap(rc.OPS)
    if not getattr(rc.signed_action, "_timed", False):
        inner = rc.signed_action

        def signed_action(*a, **k):
            _TL.t0 = time.time()
            try:
                return inner(*a, **k)
            finally:
                _TL.t0 = None
        signed_action._timed = True
        rc.signed_action = signed_action


def _reason(msg):
    try:
        from case_helpers import node_reason
        return node_reason(msg)
    except Exception:
        return str(msg or "")[:300]


def _known(msg):
    try:
        import case_evidence
        for marker, label, _words in case_evidence.KNOWN_PRODUCT_BUGS:
            if marker in (msg or ""):
                return label
    except Exception:
        pass
    return ""


def _op(entry):
    import rubix_client as rc
    with _LOCK:
        who = _WHO.get(entry.get("initiator")) or _WHO.get(entry.get("owner")) or (None, None, None)
    t0 = getattr(_TL, "t0", None)
    waited = entry.get("waited_for_fullnode") or 0.0
    ok = bool(entry.get("status"))
    msg = entry.get("message") or ""
    emit("op", unit=who[0], case=who[1], mode=who[2], kind=entry.get("kind"),
         host=entry.get("host"), initiator=entry.get("initiator"), owner=entry.get("owner"),
         rbt=entry.get("rbt"), assets=entry.get("assets") or [], ok=ok,
         secs=round(entry["at"] - t0 - waited, 2) if t0 else None, waited=waited,
         pacing=entry.get("pacing") or "", txid=entry.get("txid") or "",
         reason="" if ok else _reason(msg), known_bug="" if ok else _known(msg),
         funding=bool(rc.FAUCET_DID) and entry.get("initiator") == rc.FAUCET_DID)


# ---------------------------------------------------------------------------
# Checking a file
# ---------------------------------------------------------------------------

def check_file(path):
    """Problems with an events file, [] when every line is well formed: JSON,
    the common fields, a known type with all its fields, seq 1, 2, 3 ..."""
    problems, seq = [], 0
    try:
        fh = open(path, encoding="utf-8")
    except (IOError, OSError) as e:
        return ["cannot read {}: {}".format(path, e)]
    with fh:
        for n, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                line = json.loads(raw)
            except ValueError as e:
                problems.append("line {}: not JSON ({})".format(n, e))
                continue
            missing = [k for k in COMMON if k not in line]
            if missing:
                problems.append("line {}: missing {}".format(n, ", ".join(missing)))
                continue
            if line["v"] != VERSION:
                problems.append("line {}: version {}".format(n, line["v"]))
            if line["seq"] != seq + 1:
                problems.append("line {}: seq {} after {}".format(n, line["seq"], seq))
            seq = line["seq"]
            fields = TYPES.get(line["type"])
            if fields is None:
                problems.append("line {}: unknown type {!r}".format(n, line["type"]))
                continue
            missing = [k for k in fields if k not in line]
            if missing:
                problems.append("line {} ({}): missing {}".format(n, line["type"], ", ".join(missing)))
    return problems
