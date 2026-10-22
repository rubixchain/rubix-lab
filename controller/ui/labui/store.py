"""store.py - the UI's own SQLite file: runs, their units, case results and
operations, the limits and ledger history, and small fleet snapshots.

Real fleet state is never cached here for long - the poller reads it live. This
is history: what ran, what it found, and what the fleet looked like then.
Everything is reachable from the reports on disk too; this makes it quick to
list, compare and chart.
"""

import json
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, source TEXT, kind TEXT, name TEXT, cases TEXT, only TEXT,
    status TEXT, started_at TEXT, ended_at TEXT, duration REAL, pid INTEGER,
    exit_code INTEGER, version TEXT, knobs TEXT, spec TEXT, cmdline TEXT,
    report_json TEXT, report_pdf TEXT, evidence TEXT, log TEXT, events TEXT, console TEXT,
    passed INTEGER, failed INTEGER, skipped INTEGER, total INTEGER,
    note TEXT, conditions TEXT, import_key TEXT UNIQUE, created REAL, stop TEXT);
CREATE TABLE IF NOT EXISTS units (
    run_id TEXT, name TEXT, cases TEXT, exclusive INTEGER, quorums TEXT, senders TEXT,
    receivers TEXT, dirty TEXT, started REAL, ended REAL, seconds REAL, error TEXT,
    PRIMARY KEY (run_id, name));
CREATE TABLE IF NOT EXISTS case_results (
    run_id TEXT, test_id TEXT, unit TEXT, status TEXT, actual TEXT, note TEXT, db TEXT,
    fullnode TEXT, seconds REAL, checks TEXT, known_bugs TEXT, fullnode_buckets TEXT,
    case_text TEXT, expected TEXT, position INTEGER,
    PRIMARY KEY (run_id, test_id));
CREATE TABLE IF NOT EXISTS ops (
    run_id TEXT, seq INTEGER, at REAL, unit TEXT, case_id TEXT, mode TEXT, kind TEXT,
    host TEXT, initiator TEXT, owner TEXT, rbt REAL, assets TEXT, ok INTEGER, secs REAL,
    waited REAL, pacing TEXT, txid TEXT, reason TEXT, known_bug TEXT, funding INTEGER,
    PRIMARY KEY (run_id, seq));
CREATE INDEX IF NOT EXISTS ops_by_case ON ops (run_id, case_id);
CREATE TABLE IF NOT EXISTS import_files (path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
    run_id TEXT);
CREATE TABLE IF NOT EXISTS limits_history (key TEXT, at TEXT, value REAL, detail TEXT,
    PRIMARY KEY (key, at));
CREATE TABLE IF NOT EXISTS ledger_history (label TEXT, at TEXT, dids INTEGER, totals TEXT,
    PRIMARY KEY (label, at));
CREATE TABLE IF NOT EXISTS fleet_snapshots (at REAL PRIMARY KEY, summary TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""

RUN_FIELDS = ("id", "source", "kind", "name", "cases", "only", "status", "started_at",
              "ended_at", "duration", "pid", "exit_code", "version", "knobs", "spec",
              "cmdline", "report_json", "report_pdf", "evidence", "log", "events", "console",
              "passed", "failed", "skipped", "total", "note", "conditions", "import_key",
              "created", "stop")
# Columns added after the first release, for files made before them.
MIGRATIONS = ("ALTER TABLE runs ADD COLUMN stop TEXT",)
JSON_FIELDS = ("knobs", "spec", "conditions", "cases_json", "quorums", "senders", "stop",
               "receivers", "dirty", "checks", "known_bugs", "fullnode_buckets", "assets")


def _enc(v):
    return json.dumps(v, default=str) if isinstance(v, (dict, list, tuple)) else v


class Store(object):
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            for sql in MIGRATIONS:
                try:
                    self.conn.execute(sql)
                except sqlite3.OperationalError:
                    pass                # already there

    # -- low level ---------------------------------------------------------

    def execute(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params)

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        r = self.rows(sql, params)
        return r[0] if r else None

    class _Tx(object):
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            self.store.lock.acquire()
            self.store.conn.execute("BEGIN")
            return self.store

        def __exit__(self, exc_type, exc, tb):
            try:
                self.store.conn.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.store.lock.release()
            return False

    def tx(self):
        return Store._Tx(self)

    # -- runs ---------------------------------------------------------------

    def put_run(self, run):
        """Insert or replace a whole run row."""
        cols = [k for k in RUN_FIELDS if k in run]
        self.execute("INSERT OR REPLACE INTO runs ({}) VALUES ({})".format(
            ", ".join(cols), ", ".join("?" for _ in cols)), [_enc(run[k]) for k in cols])

    def update_run(self, run_id, **fields):
        fields = dict((k, v) for k, v in fields.items() if k in RUN_FIELDS)
        if not fields:
            return
        self.execute("UPDATE runs SET {} WHERE id = ?".format(
            ", ".join("{} = ?".format(k) for k in fields)),
            [_enc(v) for v in fields.values()] + [run_id])

    def run(self, run_id):
        r = self.one("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _decode(r)

    def runs(self, limit=500):
        return [_decode(r) for r in self.rows(
            "SELECT * FROM runs ORDER BY COALESCE(started_at, '') DESC, created DESC LIMIT ?",
            (limit,))]

    def delete_run(self, run_id):
        for table in ("runs", "units", "case_results", "ops"):
            self.execute("DELETE FROM {} WHERE {} = ?".format(
                table, "id" if table == "runs" else "run_id"), (run_id,))

    # -- parts of a run -------------------------------------------------------

    def put_unit(self, run_id, unit):
        self.execute(
            "INSERT OR REPLACE INTO units (run_id, name, cases, exclusive, quorums, senders, "
            "receivers, dirty, started, ended, seconds, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, unit["name"], _enc(unit.get("cases") or []), int(bool(unit.get("exclusive"))),
             _enc(unit.get("quorums") or []), _enc(unit.get("senders") or []),
             _enc(unit.get("receivers") or []), _enc(unit.get("dirty") or []),
             unit.get("started"), unit.get("ended"), unit.get("seconds"), unit.get("error")))

    def put_case(self, run_id, c, position=None):
        self.execute(
            "INSERT OR REPLACE INTO case_results (run_id, test_id, unit, status, actual, note, "
            "db, fullnode, seconds, checks, known_bugs, fullnode_buckets, case_text, expected, "
            "position) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, c["test_id"], c.get("unit"), c.get("status"), c.get("actual"),
             c.get("note"), c.get("db"), c.get("fullnode"), c.get("seconds"),
             _enc(c.get("checks") or []), _enc(c.get("known_bugs") or {}),
             _enc(c.get("fullnode_buckets")), c.get("case_text"), c.get("expected"), position))

    def put_ops(self, run_id, ops):
        with self.lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO ops (run_id, seq, at, unit, case_id, mode, kind, host, "
                "initiator, owner, rbt, assets, ok, secs, waited, pacing, txid, reason, "
                "known_bug, funding) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, o["seq"], o.get("t"), o.get("unit"), o.get("case"), o.get("mode"),
                  o.get("kind"), o.get("host"), o.get("initiator"), o.get("owner"),
                  o.get("rbt"), _enc(o.get("assets") or []), int(bool(o.get("ok"))),
                  o.get("secs"), o.get("waited"), o.get("pacing"), o.get("txid"),
                  o.get("reason"), o.get("known_bug"), int(bool(o.get("funding"))))
                 for o in ops])

    def units(self, run_id):
        return [_decode(r) for r in self.rows(
            "SELECT * FROM units WHERE run_id = ? ORDER BY COALESCE(started, 0), name", (run_id,))]

    def cases(self, run_id):
        return [_decode(r) for r in self.rows(
            "SELECT * FROM case_results WHERE run_id = ? ORDER BY COALESCE(position, 0), rowid",
            (run_id,))]

    def ops(self, run_id, limit=300):
        return [_decode(r) for r in self.rows(
            "SELECT * FROM ops WHERE run_id = ? ORDER BY seq DESC LIMIT ?", (run_id, limit))]

    def op_stats(self, run_id):
        """[(case, mode, ok, secs, funding, reason, known_bug)] for every op of a run."""
        return self.rows("SELECT case_id, mode, ok, secs, funding, reason, known_bug, unit "
                         "FROM ops WHERE run_id = ?", (run_id,))

    # -- small key/value settings ------------------------------------------------

    def get(self, key, default=None):
        r = self.one("SELECT value FROM kv WHERE key = ?", (key,))
        if not r:
            return default
        try:
            return json.loads(r["value"])
        except ValueError:
            return default

    def set(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
                     (key, json.dumps(value, default=str)))


def _decode(row):
    if not row:
        return row
    for k in JSON_FIELDS + ("cases",):
        v = row.get(k)
        if isinstance(v, str) and v[:1] in ("[", "{"):
            try:
                row[k] = json.loads(v)
            except ValueError:
                pass
    return row
