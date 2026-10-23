"""runs.py - starting a run, following it, stopping it, and finding it again.

A run is test_runner.py in its own process, started with --events FILE. The
runner takes the fleet's run lock itself (run_lock.py), exactly as it does from
a terminal, so the UI and a terminal can never run two at once. Its output goes
to reports/events/<run>.out and its events to reports/events/<run>.jsonl; this
reads both as they grow. Because everything is in files, reloading the page,
opening it on another machine, or restarting the UI loses nothing: on start the
UI asks the lock who holds the fleet, and if it is one of its own runs it reads
the files again from the top.

A run started from a terminal (test_runner.py, smoke_test.py,
unpledged_spend.py) is shown too, from its log - without events, so as its
console only. The UI recognises when it starts an exclusive unit (the "->
<unit>" line) and stops its own polling, as for its own runs.

Stop ends the runner's process group. Transfers it had already sent keep going
on the nodes, so the participants' Locked RBT is checked twice afterwards.
"""

import collections
import datetime
import json
import os
import re
import threading
import time

import run_lock
import test_runner as tr

from labui import FULL_TEST, proc
from labui import plan as plan_mod

RUNNING = ("starting", "running")
STATS_KEEP = 200000         # op records kept in memory for a live run's figures
EVENTS_KEEP = 20000         # events kept for streaming to pages that reattach
CONSOLE_KEEP = 5000
UNIT_LINE = re.compile(r"^\s+-> (\S+)\s")


def now_iso(t=None):
    return datetime.datetime.fromtimestamp(t or time.time()).isoformat(timespec="seconds")


class RunError(Exception):
    def __init__(self, status, message, plan=None):
        Exception.__init__(self, message)
        self.status, self.message, self.plan = status, message, plan


def percentile(values, p):
    """Nearest-rank, as case_helpers.percentile (the lab's own figure)."""
    try:
        import case_helpers
        return case_helpers.percentile(values, p)
    except Exception:
        vals = sorted(values)
        if not vals:
            return 0.0
        k = max(0, min(len(vals) - 1, int(round(p / 100.0 * len(vals) + 0.5)) - 1))
        return vals[k]


def op_figures(records):
    """Per case and mode: transfers that went through, refusals (known product
    bugs counted apart), p50/p95/max seconds; plus refusals grouped by the
    node's reason. `records` are dicts with case, mode, ok, secs, funding,
    reason, known_bug. Faucet top-ups are counted apart - they are setup."""
    per, refusals, funding = collections.OrderedDict(), {}, {"ok": 0, "refused": 0}
    for o in records:
        if o.get("funding"):
            funding["ok" if o.get("ok") else "refused"] += 1
        key = (o.get("case") or o.get("case_id") or "", o.get("mode") or "")
        if o.get("funding") or not key[0]:
            pass
        else:
            f = per.setdefault(key, {"case": key[0], "mode": key[1], "ok": 0, "refused": 0,
                                     "known": 0, "secs": []})
            if o.get("ok"):
                f["ok"] += 1
                if o.get("secs") is not None:
                    f["secs"].append(o["secs"])
            elif o.get("known_bug"):
                f["known"] += 1
            else:
                f["refused"] += 1
        if not o.get("ok"):
            reason = o.get("reason") or "(no reason given)"
            r = refusals.setdefault(reason, {"reason": reason, "count": 0,
                                             "known_bug": o.get("known_bug") or "", "cases": []})
            r["count"] += 1
            c = o.get("case") or o.get("case_id") or ("faucet top-up" if o.get("funding") else "")
            if c and c not in r["cases"]:
                r["cases"].append(c)
    stats = []
    for f in per.values():
        secs = f.pop("secs")
        f.update({"timed": len(secs),
                  "p50": round(percentile(secs, 50), 2) if secs else None,
                  "p95": round(percentile(secs, 95), 2) if secs else None,
                  "max": round(max(secs), 2) if secs else None})
        stats.append(f)
    return stats, sorted(refusals.values(), key=lambda r: -r["count"]), funding


class RunState(object):
    """One run as it goes, built from its events (or, from a terminal, its log)."""

    def __init__(self, run_id, source, kind="catalogue"):
        self.id, self.source, self.kind = run_id, source, kind
        self.status = "starting"
        self.started, self.ended = time.time(), None
        self.pid, self.popen, self.exit_code = None, None, None
        self.events_path = self.out_path = self.log_path = None
        self.cmdline, self.only, self.cases_module, self.order, self.knobs = "", "", "", [], {}
        self.holder = None
        self.spec = None                    # a custom run's scenarios
        self.events = collections.deque(maxlen=EVENTS_KEEP)
        self.seq = 0
        self.console = collections.deque(maxlen=CONSOLE_KEEP)
        self.console_n = 0
        self.units = collections.OrderedDict()
        self.cases = collections.OrderedDict()
        self.phases = collections.OrderedDict()
        self.records = collections.deque(maxlen=STATS_KEEP)
        self.recent_ops = collections.deque(maxlen=300)
        self.ops_total = 0
        self.run_done = None
        self.stop = None
        self.exclusive_now = set()
        self.note = ""
        self.report = {}
        self.cond = threading.Condition()
        self.pending_ops, self.dirty_units, self.dirty_cases = [], set(), set()
        self.run_fields = {}
        self.exclusive_names = set()        # terminal runs: units that run alone

    @property
    def running(self):
        return self.status in RUNNING

    # -- events --------------------------------------------------------------------

    def apply(self, ev):
        kind = ev.get("type")
        self.seq = max(self.seq, ev.get("seq") or 0)
        self.events.append(ev)
        if kind == "run_started":
            self.status = "running"
            self.order = ev.get("order") or []
            self.cases_module = ev.get("cases") or ""
            self.knobs = ev.get("knobs") or {}
            self.spec = ev.get("spec") or self.spec
            self.log_path = ev.get("log") or self.log_path
            for u in ev.get("units") or []:
                self.units.setdefault(u["name"], dict(u, status="waiting"))
                self.dirty_units.add(u["name"])
            for tid in self.order:
                self.cases.setdefault(tid, {"test_id": tid, "status": "waiting"})
            self.run_fields.update(status="running", log=self.log_path, cases=",".join(self.order))
        elif kind == "phase":
            p = dict(ev)
            for k in ("v", "run", "seq", "type"):
                p.pop(k, None)
            self.phases[ev.get("phase")] = p
        elif kind == "unit_started":
            u = self.units.setdefault(ev["unit"], {"name": ev["unit"]})
            u.update(status="preparing", cases=ev.get("cases"), exclusive=ev.get("exclusive"),
                     quorums=ev.get("quorums"), senders=ev.get("senders"),
                     receivers=ev.get("receivers"), dirty=ev.get("dirty"), started=ev.get("t"))
            if ev.get("exclusive"):
                self.exclusive_now.add(ev["unit"])
            self.dirty_units.add(ev["unit"])
        elif kind == "case_started":
            c = self.cases.setdefault(ev["case"], {"test_id": ev["case"]})
            c.update(unit=ev.get("unit"), status="running", mode=ev.get("mode"))
            u = self.units.get(ev.get("unit"))
            if u is not None:
                u.update(status="running", current=ev["case"], mode=ev.get("mode"))
            self.dirty_cases.add(ev["case"])
        elif kind == "op":
            self.ops_total += 1
            rec = dict((k, ev.get(k)) for k in ("seq", "t", "unit", "case", "mode", "kind", "host",
                                                 "initiator", "owner", "rbt", "assets", "ok",
                                                 "secs", "waited", "pacing", "txid", "reason",
                                                 "known_bug", "funding"))
            self.records.append(dict((k, rec[k]) for k in ("case", "mode", "ok", "secs", "funding",
                                                           "reason", "known_bug")))
            self.recent_ops.append(rec)
            self.pending_ops.append(rec)
        elif kind == "case_result":
            c = self.cases.setdefault(ev["case"], {"test_id": ev["case"]})
            for k in ("unit", "status", "actual", "note", "seconds", "db", "fullnode", "checks",
                      "known_bugs", "fullnode_buckets"):
                c[k] = ev.get(k)
            c.pop("mode", None)
            self.dirty_cases.add(ev["case"])
        elif kind == "unit_done":
            u = self.units.setdefault(ev["unit"], {"name": ev["unit"]})
            u.update(status="error" if ev.get("error") else "done", seconds=ev.get("seconds"),
                     error=ev.get("error"), ended=ev.get("t"), current=None)
            self.exclusive_now.discard(ev["unit"])
            self.dirty_units.add(ev["unit"])
        elif kind == "run_done":
            self.run_done = dict(ev)
            self.exclusive_now.clear()
            self.report = {"json": ev.get("report_json"), "pdf": ev.get("report_pdf"),
                           "evidence": ev.get("evidence"), "log": ev.get("log")}

    def add_console(self, lines):
        for line in lines:
            self.console_n += 1
            self.console.append((self.console_n, line))
            if self.exclusive_names:
                m = UNIT_LINE.match(line)
                if m:
                    self.exclusive_now = {m.group(1)} if m.group(1) in self.exclusive_names else set()
                    self.units.setdefault(m.group(1), {"name": m.group(1)}).update(status="running")


class RunManager(object):
    def __init__(self, cfg, store, source, catalogue, poller_ref, importer_ref):
        self.cfg, self.store, self.source, self.catalogue = cfg, store, source, catalogue
        self.poller_ref, self.importer_ref = poller_ref, importer_ref
        self.current = None
        self.start_lock = threading.Lock()

    # -- state for the poller and the header ---------------------------------------

    def mode(self):
        st = self.current
        if st is not None and st.running:
            return "exclusive" if st.exclusive_now else "run"
        return "idle"

    def status(self):
        st = self.current
        out = {"mode": self.mode(), "run": None}
        if st is not None:
            out["run"] = {"id": st.id, "status": st.status, "source": st.source,
                          "kind": st.kind, "started": st.started,
                          "exclusive_now": sorted(st.exclusive_now)}
        return out

    # -- startup ---------------------------------------------------------------------

    def recover(self):
        """After a restart: reattach to our own run if it still holds the fleet,
        and settle every run the database still calls running."""
        holder = self.source.lock_holder()
        mine = None
        if holder and holder.get("events") and os.path.exists(holder["events"]):
            run_id = os.path.splitext(os.path.basename(holder["events"]))[0]
            st = RunState(run_id, "ui")
            st.pid = holder.get("pid")
            st.events_path = holder["events"]
            st.out_path = os.path.splitext(holder["events"])[0] + ".out"
            st.holder = holder
            row = self.store.run(run_id) or {}
            st.cmdline, st.only = row.get("cmdline") or "", row.get("only") or ""
            st.kind, st.spec = row.get("kind") or "catalogue", row.get("spec")
            if row.get("started_at"):
                try:
                    st.started = time.mktime(datetime.datetime.fromisoformat(
                        row["started_at"]).timetuple())
                except ValueError:
                    pass
            if not row:
                self.store.put_run({"id": run_id, "source": "ui", "kind": st.kind,
                                    "status": "running", "started_at": holder.get("started"),
                                    "events": st.events_path, "console": st.out_path,
                                    "pid": st.pid, "created": time.time()})
            self.current = mine = st
            threading.Thread(target=self._follow, args=(st,), daemon=True).start()
        for row in self.store.rows("SELECT id, events FROM runs WHERE status IN "
                                   "('starting', 'running')"):
            if mine is not None and row["id"] == mine.id:
                continue
            st = RunState(row["id"], "ui")
            st.events_path = row["events"]
            if st.events_path and os.path.exists(st.events_path):
                with open(st.events_path, "rb") as fh:
                    for raw in fh:
                        try:
                            st.apply(json.loads(raw.decode("utf-8")))
                        except ValueError:
                            pass
            self._finish(st, lost=True)
        threading.Thread(target=self._watch_terminal, daemon=True).start()

    # -- starting --------------------------------------------------------------------

    def start(self, req):
        with self.start_lock:
            st = self.current
            if st is not None and st.running:
                raise RunError(409, "a run is going ({}, {}) - one run on the fleet at a time".format(
                    st.id, st.status))
            holder = self.source.lock_holder()
            if holder:
                raise RunError(409, "the fleet is busy - " + run_lock.describe(holder))
            poller = self.poller_ref()
            plan = self.catalogue.plan(req, poller.pool(), poller.faucet_info)
            if not plan["ok"]:
                raise RunError(400, "; ".join(plan["errors"]), plan)
            ok, note = self.source.faucet_check()
            if not ok:
                raise RunError(409, "faucet not ready - " + note, plan)

            kind = "custom" if plan.get("mode") == "custom" else "catalogue"
            run_id = "ui-" + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            while self.store.run(run_id) or os.path.exists(
                    os.path.join(self.cfg.events_dir, run_id + ".jsonl")):
                run_id += "x"
            st = RunState(run_id, "ui", kind)
            st.events_path = os.path.join(self.cfg.events_dir, run_id + ".jsonl")
            st.out_path = os.path.join(self.cfg.events_dir, run_id + ".out")
            st.only = ",".join(plan["order"]) if "--only" in plan["argv"] else ""
            st.knobs = plan["knobs"]
            run_argv = list(plan["argv"])
            if kind == "custom":
                # The spec the runner reads, kept beside the run's events so the
                # run can be repeated from a terminal with the same command.
                spec_path = os.path.join(self.cfg.events_dir, run_id + ".spec.json")
                with open(spec_path, "w", encoding="utf-8") as fh:
                    json.dump(plan["spec"], fh, indent=2)
                run_argv = [spec_path if a == plan_mod.SPEC_ARG else a for a in run_argv]
                st.spec = plan["spec"]
                st.cmdline = "python3 test_runner.py " + plan_mod._sh(run_argv)
            else:
                st.cmdline = plan["command"]
            argv = list(self.cfg.runner) + run_argv + ["--events", st.events_path]
            st.popen = proc.spawn(argv, FULL_TEST, st.out_path)
            st.pid = st.popen.pid
            self.store.put_run({"id": run_id, "source": "ui", "kind": kind,
                                "name": "custom" if kind == "custom" else self.catalogue.name,
                                "cases": ",".join(plan["order"]),
                                "only": st.only, "status": "starting",
                                "started_at": now_iso(st.started), "pid": st.pid,
                                "knobs": plan["knobs"], "spec": st.spec, "cmdline": st.cmdline,
                                "events": st.events_path, "console": st.out_path,
                                "created": time.time()})
            self.current = st
            threading.Thread(target=self._follow, args=(st,), daemon=True).start()
            return st

    # -- following -------------------------------------------------------------------

    def _alive(self, st):
        if st.popen is not None:
            return st.popen.poll() is None
        return run_lock.pid_alive(st.pid, (st.holder or {}).get("process"))

    def _follow(self, st):
        """Read the run's files as they grow until its process has ended."""
        ev_pos, ev_buf, out_pos, out_buf = 0, b"", 0, b""
        last_seen_alive = True
        while True:
            changed = False
            if st.events_path and os.path.exists(st.events_path):
                with open(st.events_path, "rb") as fh:
                    fh.seek(ev_pos)
                    chunk = fh.read()
                if chunk:
                    ev_pos += len(chunk)
                    ev_buf += chunk
                    *lines, ev_buf = ev_buf.split(b"\n")
                    with st.cond:
                        for raw in lines:
                            if raw.strip():
                                try:
                                    st.apply(json.loads(raw.decode("utf-8")))
                                except ValueError:
                                    pass
                    changed = True
            if st.out_path and os.path.exists(st.out_path):
                with open(st.out_path, "rb") as fh:
                    fh.seek(out_pos)
                    chunk = fh.read()
                if chunk:
                    out_pos += len(chunk)
                    out_buf += chunk
                    *lines, out_buf = out_buf.split(b"\n")
                    with st.cond:
                        st.add_console([l.decode("utf-8", "replace").rstrip("\r") for l in lines])
                    changed = True
            self._flush(st)
            if changed:
                with st.cond:
                    st.cond.notify_all()
            if not last_seen_alive:
                break
            last_seen_alive = self._alive(st)
            time.sleep(0.4)
        if out_buf.strip():
            with st.cond:
                st.add_console([out_buf.decode("utf-8", "replace").rstrip("\r")])
        if st.popen is not None:
            st.exit_code = st.popen.returncode
        self._finish(st)

    def _flush(self, st):
        """Write what changed to the database."""
        try:
            if st.pending_ops:
                ops, st.pending_ops = st.pending_ops, []
                self.store.put_ops(st.id, ops)
            for name in list(st.dirty_units):
                st.dirty_units.discard(name)
                u = st.units.get(name)
                if u:
                    self.store.put_unit(st.id, u)
            for tid in list(st.dirty_cases):
                st.dirty_cases.discard(tid)
                c = st.cases.get(tid)
                if c:
                    pos = st.order.index(tid) if tid in st.order else None
                    self.store.put_case(st.id, c, pos)
            if st.run_fields:
                fields, st.run_fields = st.run_fields, {}
                self.store.update_run(st.id, **fields)
        except Exception:
            pass

    def _finish(self, st, lost=False):
        """The run's process has ended: decide what happened and record it."""
        console = " ".join(line for _n, line in list(st.console)[-30:])
        if st.stop:
            st.status = "stopped"
        elif st.run_done and st.run_done.get("status") == "finished":
            st.status = "finished"
        elif st.run_done:
            st.status = "aborted"
        elif "the fleet is busy" in console:
            st.status = "refused"
        elif lost:
            st.status = "interrupted"
        else:
            st.status = "failed"
        if st.status in ("aborted", "failed", "refused") and not st.note:
            errors = [line for _n, line in st.console if line.startswith("ERROR")]
            st.note = errors[-1] if errors else ("the runner exited with code {}".format(
                st.exit_code) if st.exit_code is not None else "the runner ended without a report")
        if lost and not st.note:
            st.note = ("it finished while the UI was not running" if st.run_done else
                       "the UI was not running when this run ended")
        st.ended = st.ended or time.time()
        done = st.run_done or {}
        fields = {"status": st.status, "ended_at": now_iso(st.ended),
                  "duration": done.get("duration") or round(st.ended - st.started, 1),
                  "exit_code": st.exit_code, "note": st.note,
                  "passed": done.get("passed"), "failed": done.get("failed"),
                  "skipped": done.get("skipped")}
        if done.get("passed") is not None:
            fields["total"] = sum(done.get(k) or 0 for k in ("passed", "failed", "skipped"))
        for key, field in (("report_json", "report_json"), ("report_pdf", "report_pdf"),
                           ("evidence", "evidence"), ("log", "log")):
            if done.get(key):
                fields[field] = done[key]
        report = done.get("report_json")
        if report and os.path.exists(report):
            try:
                with open(report, encoding="utf-8") as fh:
                    meta = json.load(fh).get("run") or {}
                conditions = meta.get("conditions") or []
                fields["conditions"] = conditions
                version = dict((c[0], c[1]) for c in conditions if len(c) == 2).get("Version")
                if version and not str(version).startswith("not recorded"):
                    fields["version"] = version
            except (IOError, OSError, ValueError):
                pass
            key = "report:" + os.path.splitext(os.path.basename(report))[0]
            dup = self.store.one("SELECT id FROM runs WHERE import_key = ? AND id != ?", (key, st.id))
            if dup:
                self.store.delete_run(dup["id"])
            fields["import_key"] = key
        self._flush(st)
        self.store.update_run(st.id, **fields)
        with st.cond:
            st.cond.notify_all()
        importer = self.importer_ref()
        if importer is not None:
            importer.soon()

    # -- runs started from a terminal --------------------------------------------------

    def _watch_terminal(self):
        """Notice a run that holds the fleet but is not one of ours, and follow
        its log; notice when it ends."""
        while True:
            try:
                self._check_terminal()
            except Exception:
                pass
            time.sleep(3)

    def _check_terminal(self):
        st = self.current
        if st is not None and st.running:
            if st.source == "terminal" and not run_lock.pid_alive(st.pid, (st.holder or {}).get("process")):
                st.status, st.ended = "finished", time.time()
                st.exclusive_now = set()
                st.note = "ended (started from {}); its report appears under Reports".format(
                    (st.holder or {}).get("by", "a terminal"))
                with st.cond:
                    st.cond.notify_all()
                importer = self.importer_ref()
                if importer is not None:
                    importer.soon()
            return
        holder = self.source.lock_holder()
        if not holder or (st is not None and st.pid == holder.get("pid")):
            return
        script = holder.get("script", "")
        args = [str(a) for a in holder.get("args") or []]
        kind = {"smoke_test.py": "smoke", "unpledged_spend.py": "probe"}.get(script, "catalogue")
        if kind == "catalogue" and "--cases" in args and \
                args[args.index("--cases") + 1:args.index("--cases") + 2] == ["custom"]:
            kind = "custom"
        t = RunState("terminal-{}".format(holder.get("pid")), "terminal", kind)
        t.status, t.pid, t.holder = "running", holder.get("pid"), holder
        t.cmdline = "{} {}".format(script, " ".join(holder.get("args") or [])).strip()
        try:
            t.started = time.mktime(datetime.datetime.fromisoformat(holder["started"]).timetuple())
        except (KeyError, ValueError):
            pass
        if kind == "catalogue":
            try:
                module = self.catalogue.load()
                t.exclusive_names = set(u.name for u in tr.build_units(module, list(module.ORDER))
                                        if u.exclusive)
            except BaseException:
                t.exclusive_names = set()
        self.current = t
        threading.Thread(target=self._follow_log, args=(t,), daemon=True).start()

    def _follow_log(self, st):
        pos, buf = 0, b""
        while st.running:
            path = (self.source.lock_holder() or st.holder or {}).get("log")
            if path and os.path.exists(path):
                st.log_path = path
                with open(path, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                if chunk:
                    pos += len(chunk)
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    with st.cond:
                        st.add_console([l.decode("utf-8", "replace").rstrip("\r") for l in lines])
                        st.cond.notify_all()
            time.sleep(1)

    # -- stopping ----------------------------------------------------------------------

    def stop(self, run_id):
        st = self.current
        if st is None or st.id != run_id or not st.running:
            raise RunError(409, "this run is not going")
        if st.source != "ui":
            raise RunError(409, "this run was started from {} - stop it there".format(
                (st.holder or {}).get("by", "a terminal")))
        if st.stop:
            raise RunError(409, "already stopping")
        busy = [u for u in st.units.values() if u.get("status") in ("preparing", "running")]
        participants = []
        for u in busy:
            for role in ("quorums", "senders", "receivers"):
                for e in u.get(role) or []:
                    participants.append((u["name"], role[:-1], e.get("host"), e.get("did")))
        st.stop = {"at": time.time(), "units": [u["name"] for u in busy], "checks": [],
                   "pending": True,
                   "message": "Stopped. The runner was ended; transfers it had already sent keep "
                              "going on the nodes until they finish or time out, and the "
                              "participant nodes keep the quorum lists this run set."}
        self.store.update_run(st.id, stop=st.stop)
        threading.Thread(target=proc.terminate, args=(st.pid, st.popen), daemon=True).start()
        threading.Thread(target=self._leftover_locks, args=(st, participants), daemon=True).start()
        return st

    def _leftover_locks(self, st, participants):
        """Look for RBT left Locked on the stopped units' participants, 30 s and
        then 2 min after the stop - an in-flight transfer may still release it."""
        from concurrent.futures import ThreadPoolExecutor
        for wait, label in ((30, "30 s after the stop"), (90, "2 min after the stop")):
            time.sleep(wait)
            if not participants:
                st.stop["checks"].append({"when": label, "checked": 0, "locked": [],
                                          "note": "no unit had started - nothing to check"})
                break

            def one(p):
                try:
                    return p, self.source.locked_rbt(p[2], p[3])
                except Exception:
                    return p, None
            with ThreadPoolExecutor(max_workers=8) as ex:
                found = list(ex.map(one, participants))
            locked = [{"unit": p[0], "role": p[1], "host": p[2], "did": p[3], "locked": v}
                      for p, v in found if v is not None and v > 0.0005]
            unread = sum(1 for _p, v in found if v is None)
            st.stop["checks"].append({"when": label, "checked": len(found), "locked": locked,
                                      "unreadable": unread})
            self.store.update_run(st.id, stop=st.stop, note="stopped; locked RBT {}: {}".format(
                label, ", ".join("{} {:.3f}".format(l["host"], l["locked"]) for l in locked)
                or "none"))
            with st.cond:
                st.cond.notify_all()
        st.stop["pending"] = False
        self.store.update_run(st.id, stop=st.stop)

    # -- what the pages read -------------------------------------------------------------

    def live(self, run_id):
        st = self.current
        return st if st is not None and st.id == run_id else None

    def snapshot(self, run_id):
        st = self.live(run_id)
        if st is not None:
            with st.cond:
                return self._live_snapshot(st)
        return self._db_snapshot(run_id)

    def _live_snapshot(self, st):
        stats, refusals, funding = op_figures(list(st.records))
        return {
            "id": st.id, "live": True, "source": st.source, "kind": st.kind, "status": st.status,
            "started": st.started, "ended": st.ended,
            "elapsed": round((st.ended or time.time()) - st.started, 1),
            "pid": st.pid, "cmdline": st.cmdline, "only": st.only, "cases_module": st.cases_module,
            "knobs": st.knobs, "spec": st.spec, "holder": st.holder, "note": st.note,
            "phases": list(st.phases.values()), "units": list(st.units.values()),
            "cases": list(st.cases.values()), "stats": stats, "refusals": refusals[:50],
            "funding": funding, "ops": list(reversed(st.recent_ops)), "ops_total": st.ops_total,
            "console": [line for _n, line in list(st.console)[-400:]], "console_n": st.console_n,
            "seq": st.seq, "run_done": st.run_done, "stop": st.stop,
            "exclusive_now": sorted(st.exclusive_now),
            "report": st.report or ({"log": st.log_path} if st.log_path else {}),
            "can_stop": st.source == "ui" and st.running and not st.stop,
        }

    def _db_snapshot(self, run_id):
        r = self.store.run(run_id)
        if not r:
            return None
        records = self.store.op_stats(run_id)
        for o in records:
            o["case"] = o.pop("case_id")
        stats, refusals, funding = op_figures(records)
        if isinstance(r.get("stop"), dict):
            r["stop"]["pending"] = False        # its checks ended with the live run
        cases = self.store.cases(run_id)
        for c in cases:
            c["case"] = c.get("case_text")
        return {
            "id": run_id, "live": False, "source": r.get("source"), "kind": r.get("kind"),
            "status": r.get("status"), "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"), "elapsed": r.get("duration"),
            "cmdline": r.get("cmdline"), "only": r.get("only"), "cases_module": r.get("name"),
            "knobs": r.get("knobs"), "spec": r.get("spec") if isinstance(r.get("spec"), dict) else None,
            "note": r.get("note"), "version": r.get("version"),
            "conditions": r.get("conditions"), "phases": [], "units": self.store.units(run_id),
            "cases": cases, "stats": stats, "refusals": refusals[:50], "funding": funding,
            "ops": self.store.ops(run_id, 300), "ops_total": len(records), "console": [],
            "console_n": 0, "seq": 0, "run_done": None, "stop": r.get("stop"), "exclusive_now": [],
            "report": {"json": r.get("report_json"), "pdf": r.get("report_pdf"),
                       "evidence": r.get("evidence"), "log": r.get("log"),
                       "console": r.get("console")},
            "passed": r.get("passed"), "failed": r.get("failed"), "skipped": r.get("skipped"),
            "can_stop": False,
        }

    def stream(self, run_id, since, csince, send):
        """Push a run's new events and console lines to one page as they come.
        `send(event, data)` returns False once the page has gone."""
        st = self.live(run_id)
        if st is None:
            send("end", {"status": "not live"})
            return
        if st.events and since < st.events[0]["seq"] - 1:
            send("reset", {})       # the page is too far behind: reload the snapshot
            return
        pos, cpos, last_beat = since, csince, time.time()
        while True:
            with st.cond:
                evs = [e for e in st.events if e["seq"] > pos]
                cons = [(n, line) for n, line in st.console if n > cpos]
                finished = not st.running
                if not evs and not cons and not finished:
                    st.cond.wait(10)
                    evs = [e for e in st.events if e["seq"] > pos]
                    cons = [(n, line) for n, line in st.console if n > cpos]
                    finished = not st.running
            for e in evs:
                if not send("ev", e):
                    return
                pos = e["seq"]
            if cons:
                if not send("console", {"from": cons[0][0], "lines": [l for _n, l in cons]}):
                    return
                cpos = cons[-1][0]
            if finished and not evs and not cons:
                send("end", {"status": st.status})
                return
            if time.time() - last_beat > 15:
                if not send(None, None):
                    return
                last_beat = time.time()
