"""importer.py - past runs into the UI's history, from the files they left.

Reads, on start and then every minute:
    reports/json/catalogue_<name>_<time>.json     a catalogue run's report
    reports/json/<name>_db-evidence_<time>.json   its database evidence: the
                                                  participants and every operation
    reports/pdf/...                               its PDF
    reports/logs/test_runner_<time>.log           its console log
    reports/pdf/smoke_test_<time>.pdf + logs      smoke tests
    reports/json/unpledged-spend_<time>.json      the ex-quorum probe (also an older
                                                  copy at the top of reports/)
    full-test/fleet-ledger.json                   fleet totals per run
    full-test/lab-limits.json                     limits QCAP-01 measured
A log with no report left beside it is listed too: a run that ended early.

Importing twice changes nothing: every run has a key (its report's name), and a
file is read again only when its size or time changed. A report written by a
run the UI started belongs to that run and is not imported a second time.
"""

import datetime
import json
import os
import re
import threading
import time

from labui import store as store_mod

TS = r"(?P<ts>\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)"
REPORT_RE = re.compile(r"^catalogue_(?P<name>.+)_" + TS + r"\.json$")
EVIDENCE_RE = re.compile(r"^(?P<name>.+)_db-evidence_" + TS + r"\.json$")
LOG_RE = re.compile(r"^(?P<script>test_runner|smoke_test)_" + TS + r"\.log$")
SMOKE_RE = re.compile(r"^smoke_test_" + TS + r"\.pdf$")
PROBE_RE = re.compile(r"^unpledged-spend_" + TS + r"\.json$")
TS_FMT = "%Y-%m-%d_%H-%M-%S"
QUIET = 20              # a file changed in the last QUIET seconds may still be written


def _ts(text):
    return datetime.datetime.strptime(text, TS_FMT)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _node_reason(msg):
    try:
        from case_helpers import node_reason
        return node_reason(msg)
    except Exception:
        return str(msg or "")[:300]


def _known(msg):
    try:
        import case_evidence
        for marker, label, _w in case_evidence.KNOWN_PRODUCT_BUGS:
            if marker in (msg or ""):
                return label
    except Exception:
        pass
    return ""


class Importer(object):
    def __init__(self, cfg, store, holder_fn):
        self.cfg, self.store, self.holder_fn = cfg, store, holder_fn
        self.lock = threading.Lock()
        self.last = {"at": None, "imported": 0, "error": ""}
        self._soon = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, name="importer", daemon=True).start()

    def soon(self):
        """Look again in a few seconds (a run just ended)."""
        self._soon.set()

    def _loop(self):
        while True:
            try:
                n = self.scan()
                self.last.update(at=time.time(), imported=n, error="")
            except Exception as e:
                self.last.update(at=time.time(), error="{}: {}".format(type(e).__name__, e))
            if self._soon.wait(60):
                self._soon.clear()
                time.sleep(5)

    # -- the files -------------------------------------------------------------------

    def _list(self):
        """{folder: [(name, path, mtime, size)]} for json, pdf, logs and the top."""
        out = {}
        for folder in ("json", "pdf", "logs", ""):
            d = os.path.join(self.cfg.reports_dir, folder)
            items = []
            if os.path.isdir(d):
                for name in os.listdir(d):
                    path = os.path.join(d, name)
                    if os.path.isfile(path):
                        st = os.stat(path)
                        items.append((name, path, st.st_mtime, st.st_size))
            out[folder or "root"] = items
        return out

    def _unchanged(self, path, mtime, size):
        r = self.store.one("SELECT mtime, size FROM import_files WHERE path = ?", (path,))
        return r is not None and r["mtime"] == mtime and r["size"] == size

    def _seen(self, path, mtime, size, run_id):
        self.store.execute("INSERT OR REPLACE INTO import_files (path, mtime, size, run_id) "
                           "VALUES (?, ?, ?, ?)", (path, mtime, size, run_id))

    def scan(self):
        with self.lock:
            files = self._list()
            n = self._ledger() + self._limits()
            now = time.time()
            logs = []
            for name, path, mtime, _size in files["logs"]:
                m = LOG_RE.match(name)
                if m:
                    logs.append({"script": m.group("script"), "ts": _ts(m.group("ts")),
                                 "path": path, "mtime": mtime})
            evidence = []
            for name, path, mtime, size in files["json"]:
                m = EVIDENCE_RE.match(name)
                if m:
                    evidence.append({"name": m.group("name"), "ts": _ts(m.group("ts")),
                                     "path": path})
            pdfs = dict((name, path) for name, path, _m, _s in files["pdf"] + files["root"]
                        if name.endswith(".pdf"))
            for name, path, mtime, size in sorted(files["json"]):
                m = REPORT_RE.match(name)
                if m and now - mtime > QUIET and not self._unchanged(path, mtime, size):
                    n += self._report(m, path, mtime, size, evidence, logs, pdfs)
            for name, path, mtime, size in sorted(files["json"] + files["root"]):
                m = PROBE_RE.match(name)
                if m and now - mtime > QUIET and not self._unchanged(path, mtime, size):
                    n += self._probe(m, path, mtime, size, pdfs)
            for name, path, mtime, size in sorted(files["pdf"]):
                m = SMOKE_RE.match(name)
                if m and now - mtime > QUIET and not self._unchanged(path, mtime, size):
                    n += self._smoke(m, path, mtime, size, logs)
            n += self._orphan_logs(logs, now)
            return n

    # -- one file each ---------------------------------------------------------------

    def _claimed_logs(self, except_id=None):
        return set(r["log"] for r in self.store.rows(
            "SELECT id, log FROM runs WHERE log IS NOT NULL AND log != '' AND id != ?",
            (except_id or "",)))

    def _pick_log(self, logs, script, before, run_id, window_hours=12):
        claimed = self._claimed_logs(run_id)
        best = None
        for lg in logs:
            if lg["script"] != script or lg["path"] in claimed:
                continue
            if before - datetime.timedelta(hours=window_hours) <= lg["ts"] <= \
                    before + datetime.timedelta(seconds=5):
                if best is None or lg["ts"] > best["ts"]:
                    best = lg
        return best["path"] if best else None

    def _report(self, m, path, mtime, size, evidence, logs, pdfs):
        base = os.path.splitext(os.path.basename(path))[0]
        key = "report:" + base
        existing = self.store.one("SELECT id, source FROM runs WHERE import_key = ?", (key,))
        if existing and existing["source"] == "ui":
            self._seen(path, mtime, size, existing["id"])
            return 0
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (IOError, OSError, ValueError):
            return 0
        meta = data.get("run") or {}
        conditions = [list(c) for c in meta.get("conditions") or [] if len(c) == 2]
        cond = dict((k, v) for k, v in conditions)
        ended = _ts(m.group("ts"))
        try:
            started = datetime.datetime.fromisoformat(meta.get("started_at"))
        except (TypeError, ValueError):
            started = ended - datetime.timedelta(seconds=meta.get("duration_seconds") or 0)
        run_id = existing["id"] if existing else "imp-" + base

        ev_path, ev_best = None, None
        for e in evidence:
            if e["name"] == m.group("name") and \
                    started - datetime.timedelta(seconds=60) <= e["ts"] <= \
                    ended + datetime.timedelta(seconds=120):
                if ev_best is None or abs((e["ts"] - ended).total_seconds()) < ev_best:
                    ev_path, ev_best = e["path"], abs((e["ts"] - ended).total_seconds())
        log = self._pick_log(logs, "test_runner", started, run_id)
        summary = data.get("summary") or {}
        kind = "custom" if m.group("name").startswith("custom") else "catalogue"
        run = {"id": run_id, "source": "terminal", "kind": kind, "name": m.group("name"),
               "cases": ",".join(r.get("test_id", "") for r in data.get("results") or []),
               "only": (cond.get("Subset filter") or "").replace("--only ", ""),
               "status": "finished", "started_at": _iso(started), "ended_at": _iso(ended),
               "duration": meta.get("duration_seconds"),
               "version": None if str(cond.get("Version") or "").startswith("not recorded")
               else cond.get("Version"),
               "conditions": conditions, "report_json": path,
               "report_pdf": pdfs.get(base + ".pdf"), "evidence": ev_path, "log": log,
               "passed": summary.get("passed"), "failed": summary.get("failed"),
               "skipped": summary.get("skipped"), "total": summary.get("total"),
               "import_key": key, "created": time.time()}

        ev = {}
        if ev_path:
            try:
                with open(ev_path, encoding="utf-8") as fh:
                    ev = json.load(fh)
            except (IOError, OSError, ValueError):
                ev = {}
        by_case = dict((c.get("case"), c) for c in ev.get("cases") or [])
        units, ops, seq = {}, [], 0
        for c in ev.get("cases") or []:
            parts = c.get("participants") or []
            dids = set(p.get("did") for p in parts)
            u = units.setdefault(c.get("unit"), {"name": c.get("unit"), "cases": [],
                                                 "quorums": [], "senders": [], "receivers": []})
            u["cases"].append(c.get("case"))
            for p in parts:
                role = (p.get("role") or "").split(" ")[0]
                bucket = {"quorum": "quorums", "sender": "senders", "receiver": "receivers"}.get(role)
                entry = {"host": p.get("host"), "did": p.get("did"),
                         "machine": (p.get("host") or "").split(":")[0]}
                if bucket and entry not in u[bucket]:
                    u[bucket].append(entry)
            for run_ev in c.get("runs") or [c]:
                mode = run_ev.get("mode") or "without delay"
                for o in run_ev.get("operations") or []:
                    seq += 1
                    ok = bool(o.get("status"))
                    ops.append({"seq": seq, "t": o.get("at"), "unit": c.get("unit"),
                                "case": c.get("case"), "mode": mode, "kind": o.get("kind"),
                                "host": o.get("host"), "initiator": o.get("initiator"),
                                "owner": o.get("owner"), "rbt": o.get("rbt"),
                                "assets": o.get("assets"), "ok": ok, "secs": None,
                                "waited": o.get("waited_for_fullnode"), "pacing": o.get("pacing"),
                                "txid": o.get("txid"),
                                "reason": "" if ok else _node_reason(o.get("message")),
                                "known_bug": "" if ok else _known(o.get("message")),
                                "funding": o.get("initiator") not in dids})
        with self.store.tx():
            self.store.delete_run(run_id)
            self.store.put_run(run)
            for i, r in enumerate(data.get("results") or []):
                e = by_case.get(r.get("test_id")) or {}
                known = {}
                for run_ev in [e] + list(e.get("runs") or []):
                    for label, k in (run_ev.get("known_bug_refusals") or {}).items():
                        known[label] = known.get(label, 0) + k
                self.store.put_case(run_id, {
                    "test_id": r.get("test_id"), "unit": e.get("unit"), "status": r.get("status"),
                    "actual": r.get("actual"), "note": r.get("note"), "db": r.get("db"),
                    "fullnode": r.get("fullnode"), "seconds": r.get("seconds"),
                    "checks": r.get("db_checks") or [], "known_bugs": known,
                    "fullnode_buckets": (e.get("fullnode_comparison") or {}).get("counts"),
                    "case_text": r.get("case"), "expected": r.get("expected")}, i)
            for u in units.values():
                self.store.put_unit(run_id, u)
            if ops:
                self.store.put_ops(run_id, ops)
            if log:
                self.store.execute("DELETE FROM runs WHERE id = ?",
                                   ("log-" + os.path.splitext(os.path.basename(log))[0],))
        self._seen(path, mtime, size, run_id)
        return 1

    def _probe(self, m, path, mtime, size, pdfs):
        base = os.path.splitext(os.path.basename(path))[0]
        key = "probe:" + base
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (IOError, OSError, ValueError):
            return 0
        results = data.get("results") or []
        when = _ts(m.group("ts"))
        run_id = "imp-" + base
        statuses = [r.get("status") for r in results]
        with self.store.tx():
            self.store.delete_run(run_id)
            self.store.put_run({
                "id": run_id, "source": "terminal", "kind": "probe", "name": "unpledged-spend",
                "cases": ",".join(str(r.get("id")) for r in results), "status": "finished",
                "started_at": _iso(when), "ended_at": _iso(when), "report_json": path,
                "report_pdf": pdfs.get(base + ".pdf"),
                "passed": statuses.count("PASS"), "failed": statuses.count("FAIL"),
                "skipped": len(statuses) - statuses.count("PASS") - statuses.count("FAIL"),
                "total": len(statuses), "import_key": key, "created": time.time(),
                "note": "ex-quorum spend probe (custom/unpledged_spend.py)"})
            for i, r in enumerate(results):
                self.store.put_case(run_id, {
                    "test_id": str(r.get("id")), "status": r.get("status"),
                    "actual": r.get("actual"), "note": r.get("note"),
                    "case_text": r.get("case"), "expected": r.get("expected")}, i)
        self._seen(path, mtime, size, run_id)
        return 1

    def _smoke(self, m, path, mtime, size, logs):
        base = os.path.splitext(os.path.basename(path))[0]
        ended = _ts(m.group("ts"))
        run_id = "imp-" + base
        log = self._pick_log(logs, "smoke_test", ended, run_id, window_hours=6)
        started = next((lg["ts"] for lg in logs if lg["path"] == log), ended)
        with self.store.tx():
            self.store.delete_run(run_id)
            self.store.put_run({
                "id": run_id, "source": "terminal", "kind": "smoke", "name": "smoke test",
                "status": "finished", "started_at": _iso(started),
                "ended_at": _iso(ended), "report_pdf": path, "log": log,
                "import_key": "smoke:" + base, "created": time.time(),
                "note": "smoke_test.py - its results are in the PDF"})
            if log:
                self.store.execute("DELETE FROM runs WHERE id = ?",
                                   ("log-" + os.path.splitext(os.path.basename(log))[0],))
        self._seen(path, mtime, size, run_id)
        return 1

    def _orphan_logs(self, logs, now):
        """Logs no report claimed: runs that ended early. Not the running one."""
        holder = self.holder_fn() or {}
        claimed = self._claimed_logs()
        n = 0
        for lg in logs:
            if lg["path"] in claimed or lg["path"] == holder.get("log") or now - lg["mtime"] < 120:
                continue
            run_id = "log-" + os.path.splitext(os.path.basename(lg["path"]))[0]
            if self.store.one("SELECT 1 FROM runs WHERE id = ?", (run_id,)):
                continue
            self.store.put_run({
                "id": run_id, "source": "terminal",
                "kind": "catalogue" if lg["script"] == "test_runner" else "smoke",
                "name": lg["script"], "status": "no report", "started_at": _iso(lg["ts"]),
                "ended_at": _iso(datetime.datetime.fromtimestamp(lg["mtime"])), "log": lg["path"],
                "created": time.time(),
                "note": "a log with no report beside it - the run ended early; the log says why"})
            n += 1
        return n

    # -- ledger and limits ------------------------------------------------------------

    def _ledger(self):
        path = self.cfg.ledger_path
        if not os.path.exists(path):
            return 0
        st = os.stat(path)
        if self._unchanged(path, st.st_mtime, st.st_size):
            return 0
        try:
            with open(path, encoding="utf-8") as fh:
                runs = json.load(fh).get("runs") or []
        except (IOError, OSError, ValueError):
            return 0
        for r in runs:
            self.store.execute("INSERT OR IGNORE INTO ledger_history (label, at, dids, totals) "
                               "VALUES (?, ?, ?, ?)", (r.get("label"), r.get("at"), r.get("dids"),
                                                       store_mod._enc(r.get("totals") or {})))
        self._seen(path, st.st_mtime, st.st_size, None)
        return 0

    def _limits(self):
        path = self.cfg.limits_path
        if not os.path.exists(path):
            return 0
        st = os.stat(path)
        if self._unchanged(path, st.st_mtime, st.st_size):
            return 0
        try:
            with open(path, encoding="utf-8") as fh:
                limits = json.load(fh)
        except (IOError, OSError, ValueError):
            return 0
        for key, entry in (limits or {}).items():
            if isinstance(entry, dict):
                self.store.execute("INSERT OR IGNORE INTO limits_history (key, at, value, detail) "
                                   "VALUES (?, ?, ?, ?)",
                                   (key, entry.get("at"), entry.get("value"),
                                    store_mod._enc(entry.get("detail"))))
        self._seen(path, st.st_mtime, st.st_size, None)
        return 0
