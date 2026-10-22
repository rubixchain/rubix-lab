"""source_real.py - the real fleet, read through the lab's own tools.

fleet.py says which nodes exist, rubix_client reads their APIs (liveness is
node/peer_id, never node/ping), db_client reads the fullnodes' databases over
read-only connections. Every database read takes that node's gate first, so the
UI never holds more than one connection to a node at a time (max_connections
is 50 per node, and runs need them).

demo/source_demo.py is the same interface over a fake fleet.
"""

import re
import threading

import db_client as db
import fleet
import rubix_client as rc
import run_lock

_COMMENTED = re.compile(r"^\s*#\s*(\d{1,3}(?:\.\d{1,3}){3})\b\s*(.*)$")


def commented_hosts(path):
    """Machines commented out of hosts.txt, with the reason written beside them
    ("# 192.168.1.109  # down - unreachable since 2026-08-18")."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                m = _COMMENTED.match(line)
                if m:
                    out.append({"ip": m.group(1), "note": m.group(2).lstrip("#").strip()})
    except (IOError, OSError):
        pass
    return out


def did_of(item):
    """A DID from a /dids or /quorums entry - a plain string, or a dict."""
    if isinstance(item, dict):
        return str(item.get("did") or item.get("DID") or "")
    return str(item)


class RealSource(object):
    demo = False

    def __init__(self, cfg):
        self.cfg = cfg
        self._gates = {}
        self._gates_lock = threading.Lock()
        self._faucet_lock = threading.Lock()     # rc.faucet_ready sets rc's globals

    def gate(self, node):
        """One database connection per node at a time, from this UI."""
        key = db.node_db(node)
        with self._gates_lock:
            return self._gates.setdefault(key, threading.Semaphore(1))

    # -- what exists ---------------------------------------------------------

    def hosts(self):
        """(hosts.txt entries, commented-out machines, error text)."""
        try:
            return fleet.load_hosts(), commented_hosts(fleet.HOSTS_PATH), ""
        except SystemExit as e:
            return [], [], str(e)

    def nodes(self, hosts):
        try:
            return fleet.fleet(hosts, include_fixed=True)
        except SystemExit as e:
            raise RuntimeError(str(e))

    # -- node APIs -----------------------------------------------------------

    def probe(self, node):
        """(up, peer ID, note) from node/peer_id - the lab's liveness check."""
        ok, payload = rc.http_json("GET", rc.base_url(node["host"]) + rc.EP_PEER_ID, timeout=4)
        if ok and isinstance(payload, dict):
            return True, payload.get("message") or "", ""
        return False, None, str(payload)

    def inventory(self, node):
        """(ok, DIDs, quorum list, note)."""
        ok, dids, note = rc.get_dids(node["host"], timeout=6)
        if not ok:
            return False, [], [], note
        okq, quorums, noteq = rc.get_quorums(node["host"], timeout=6)
        return (True, [did_of(d) for d in dids if did_of(d)],
                [did_of(q) for q in quorums] if okq else None, "" if okq else noteq)

    def faucet(self, hosts):
        """The faucet pair as the runner sees it: faucet_ready() plus balances."""
        with self._faucet_lock:
            rc.use_fleet_roles(hosts)
            ok, note = rc.faucet_ready()
            out = {"ok": ok, "note": note, "faucet": None, "quorum": None,
                   "faucet_quorums": None}
            for key, did, host, port in (
                    ("faucet", rc.FAUCET_DID, rc.FAUCET_HOST, rc.FAUCET_PORT),
                    ("quorum", rc.FAUCET_QUORUM_DID, rc.FAUCET_QUORUM_HOST, rc.FAUCET_QUORUM_PORT)):
                entry = {"host": "{}:{}".format(host, port) if host else "", "did": did}
                if did and host:
                    okb, detail, bnote = rc.get_rbt_balance_detail(host, did, port)
                    entry.update(detail if okb and detail else {"error": bnote})
                out[key] = entry
            if rc.FAUCET_HOST:
                okq, quorums, _n = rc.get_quorums(rc.FAUCET_HOST, rc.FAUCET_PORT)
                out["faucet_quorums"] = [did_of(q) for q in quorums] if okq else None
            return out

    def faucet_check(self):
        """A fresh faucet_ready(), for Start."""
        hosts, _c, err = self.hosts()
        if err:
            return False, err
        with self._faucet_lock:
            rc.use_fleet_roles(hosts)
            return rc.faucet_ready()

    def locked_rbt(self, host, did):
        ok, detail, note = rc.get_rbt_balance_detail(host, did)
        return float(detail["locked"]) if ok and detail else None

    # -- fullnode databases ---------------------------------------------------

    def fullnode_hosts(self, hosts):
        return [h["host"] for h in hosts if h["role"] == "fullnode"]

    def db_ready(self):
        return db.available()

    def fullnode_stats(self, fn):
        """{processed, invalid: [(txid, reason)]} from one fullnode's database."""
        with self.gate(fn):
            processed, invalid = db.query_snapshot(fn, [
                ("SELECT COUNT(*) FROM fullnode_transactions", None),
                ("SELECT transaction->>'ID', reason FROM fullnode_invalid_transactions "
                 "LIMIT 20000", None)])
        return {"processed": int(processed[0][0]) if processed else 0,
                "invalid": [(t, r) for t, r in invalid]}

    def fullnode_accepted(self, fn, ids):
        with self.gate(fn):
            return set(t for (t,) in db.query(
                fn, "SELECT id FROM fullnode_transactions WHERE id = ANY(%s)", (list(ids),)))

    # -- other ---------------------------------------------------------------

    def collect_builds(self, nodes):
        """{node: "1.0.4 @ 1d9b28bb"} over SSH, one call per machine (node-versions.py)."""
        return rc.collect_versions(nodes)

    def lock_holder(self):
        return run_lock.holder(self.cfg.lock_path)
