"""app.py - the HTTP side: the JSON API, live updates for a running run
(Server-Sent Events), the report files, and the page itself.

Python's own http.server, a thread per request: a handful of people looking at
one lab, not a public site. No password - the lab network is internal.

    GET  /api/status                     mode, current run, who holds the fleet
    GET  /api/fleet                      machines, nodes, faucet, fullnodes, polling
    POST /api/fleet/refresh              read the fleet again now
    POST /api/fleet/builds               collect every node's build over SSH
    GET  /api/catalogue                  cases, units, knobs
    POST /api/plan                       what a run would do, and whether it may start
    POST /api/runs                       start a run
    GET  /api/runs                       past and current runs
    GET  /api/runs/current               the run on now (or the last one)
    GET  /api/runs/<id>                  one run, live or from history
    GET  /api/runs/<id>/stream           its events and console as they come (SSE)
    POST /api/runs/<id>/stop             stop it
    POST /api/import                     look for new reports now
    GET  /api/files/<folder>/<name>      a report file: json, pdf, logs, events
"""

import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import run_lock

from labui import STATIC
from labui.runs import RunError

FILE_FOLDERS = ("json", "pdf", "logs", "events", "root")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class App(object):
    def __init__(self, cfg, store, poller, runs, catalogue, importer, source):
        self.cfg, self.store, self.poller, self.runs = cfg, store, poller, runs
        self.catalogue, self.importer, self.source = catalogue, importer, source
        self._holder = (0.0, None)
        self._holder_lock = threading.Lock()

    def holder(self):
        with self._holder_lock:
            at, h = self._holder
            if time.time() - at > 2:
                h = self.source.lock_holder()
                self._holder = (time.time(), h)
            return h

    def file_link(self, path):
        """/api/files/<folder>/<name> for a file under reports/, or None."""
        if not path:
            return None
        try:
            rel = os.path.relpath(os.path.abspath(path), os.path.abspath(self.cfg.reports_dir))
        except ValueError:
            return None
        parts = rel.replace("\\", "/").split("/")
        if len(parts) == 1 and SAFE_NAME.match(parts[0]):
            return "/api/files/root/" + parts[0]
        if len(parts) == 2 and parts[0] in FILE_FOLDERS and SAFE_NAME.match(parts[1]):
            return "/api/files/{}/{}".format(parts[0], parts[1])
        return None

    def links(self, report):
        return dict((k, self.file_link(v)) for k, v in (report or {}).items() if v)

    # -- handlers --------------------------------------------------------------------

    def status(self):
        h = self.holder()
        out = self.runs.status()
        out.update({"demo": self.cfg.demo, "now": time.time(),
                    "holder": h, "holder_text": run_lock.describe(h) if h else "",
                    "importer": self.importer.last})
        return out

    def run_list(self, limit):
        out = []
        for r in self.store.runs(limit):
            out.append({k: r.get(k) for k in (
                "id", "source", "kind", "name", "cases", "only", "status", "started_at",
                "ended_at", "duration", "version", "passed", "failed", "skipped", "total",
                "note")})
            out[-1]["files"] = self.links({"json": r.get("report_json"), "pdf": r.get("report_pdf"),
                                           "log": r.get("log"), "evidence": r.get("evidence"),
                                           "console": r.get("console")})
        live = self.runs.current
        if live is not None and live.source == "terminal" and live.running:
            out.insert(0, {"id": live.id, "source": "terminal", "kind": live.kind,
                           "name": live.cmdline, "status": live.status,
                           "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                                       time.localtime(live.started)),
                           "files": self.links({"log": live.log_path}),
                           "note": "started from {}".format((live.holder or {}).get("by", "a terminal"))})
        return out

    def run_snapshot(self, run_id):
        snap = self.runs.snapshot(run_id)
        if snap is None:
            return None
        snap["files"] = self.links(dict(snap.get("report") or {}, console=(
            self.runs.live(run_id).out_path if self.runs.live(run_id) else
            (snap.get("report") or {}).get("console"))))
        return snap


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RubixLabUI/1"

        def log_message(self, fmt, *args):
            pass

        # -- replies ----------------------------------------------------------------

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _json_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                return {}

        def _static(self, rel):
            path = os.path.normpath(os.path.join(STATIC, rel))
            if not path.startswith(os.path.normpath(STATIC)) or not os.path.isfile(path):
                return self._send(404, {"error": "not found"})
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _file(self, folder, name, download):
            if folder not in FILE_FOLDERS or not SAFE_NAME.match(name):
                return self._send(404, {"error": "not found"})
            base = app.cfg.reports_dir if folder == "root" else os.path.join(app.cfg.reports_dir, folder)
            path = os.path.join(base, name)
            if not os.path.isfile(path):
                return self._send(404, {"error": "no such file"})
            ctype = mimetypes.guess_type(path)[0] or "text/plain"
            if name.endswith((".log", ".out", ".jsonl")):
                ctype = "text/plain"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            with open(path, "rb") as fh:
                data = fh.read()
            disp = "attachment" if download else "inline"
            self._send(200, data, ctype, {"Content-Disposition": '{}; filename="{}"'.format(disp, name)})

        def _stream(self, run_id, since, csince):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def send(event, data):
                try:
                    if event is None:
                        self.wfile.write(b": ping\n\n")
                    else:
                        self.wfile.write("event: {}\ndata: {}\n\n".format(
                            event, json.dumps(data, default=str)).encode("utf-8"))
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    return False
            app.runs.stream(run_id, since, csince, send)

        # -- routing ----------------------------------------------------------------

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            path, q = u.path, urllib.parse.parse_qs(u.query)
            try:
                if path in ("/", "/index.html"):
                    return self._static("index.html")
                if path.startswith("/static/"):
                    return self._static(path[len("/static/"):])
                if path == "/api/status":
                    return self._send(200, app.status())
                if path == "/api/fleet":
                    return self._send(200, app.poller.snapshot())
                if path == "/api/catalogue":
                    return self._send(200, app.catalogue.info())
                if path == "/api/runs":
                    return self._send(200, {"runs": app.run_list(int((q.get("limit") or [500])[0]))})
                if path == "/api/runs/current":
                    st = app.runs.current
                    return self._send(200, {"id": st.id if st else None,
                                            "running": bool(st and st.running)})
                m = re.match(r"^/api/runs/([A-Za-z0-9._-]+)(/stream)?$", path)
                if m:
                    if m.group(2):
                        return self._stream(m.group(1), int((q.get("since") or [0])[0]),
                                            int((q.get("csince") or [0])[0]))
                    snap = app.run_snapshot(m.group(1))
                    return self._send(200, snap) if snap else self._send(404, {"error": "no such run"})
                m = re.match(r"^/api/files/([a-z]+)/([^/]+)$", path)
                if m:
                    return self._file(m.group(1), urllib.parse.unquote(m.group(2)),
                                      bool(q.get("download")))
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": "{}: {}".format(type(e).__name__, e)})

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            body = self._json_body()
            try:
                if path == "/api/plan":
                    return self._send(200, app.catalogue.plan(body, app.poller.pool(),
                                                              app.poller.faucet_info))
                if path == "/api/runs":
                    try:
                        st = app.runs.start(body)
                    except RunError as e:
                        return self._send(e.status, {"error": e.message, "plan": e.plan})
                    return self._send(200, {"id": st.id})
                m = re.match(r"^/api/runs/([A-Za-z0-9._-]+)/stop$", path)
                if m:
                    try:
                        app.runs.stop(m.group(1))
                    except RunError as e:
                        return self._send(e.status, {"error": e.message})
                    return self._send(200, {"ok": True})
                if path == "/api/fleet/refresh":
                    ok, why = app.poller.force(body.get("tiers"))
                    return self._send(200 if ok else 409, {"ok": ok, "error": why})
                if path == "/api/fleet/builds":
                    ok, why = app.poller.collect_builds()
                    return self._send(200 if ok else 409, {"ok": ok, "error": why})
                if path == "/api/import":
                    app.importer.soon()
                    return self._send(200, {"ok": True})
                return self._send(404, {"error": "not found"})
            except Exception as e:
                return self._send(500, {"error": "{}: {}".format(type(e).__name__, e)})

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second server take a port already in use.
    allow_reuse_address = os.name != "nt"
