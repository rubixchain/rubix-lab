"""poller.py - the one place that reads the fleet in the background.

Browsers never talk to the nodes; they read what this has cached. It reads in
tiers, each at its own pace, and slows down or stops while a run is going so
it never competes with what the run measures:

    tier        what                                   idle    run     exclusive unit
    liveness    GET node/peer_id on every node         30 s    60 s    paused
    inventory   GET dids + quorums on every live node  5 min   off     paused
    faucet      faucet_ready() + the pair's balances   60 s    5 min   paused
    fullnodes   counts + rejection reasons, 4 DBs      2 min   5 min   paused

HTTP goes 16 at a time. A fullnode database is read with one connection at a
time (source.gate), and the pool nodes' databases are not read by the poller
at all. An exclusive unit (QCAP-01 measures one quorum with no other traffic;
the audits read every database) stops everything until it ends; what was read
before stays on screen, marked with its time.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import test_runner as tr

TIERS = (
    ("liveness", {"idle": 30, "run": 60, "exclusive": None}),
    ("inventory", {"idle": 300, "run": None, "exclusive": None}),
    ("faucet", {"idle": 60, "run": 300, "exclusive": None}),
    ("fullnodes", {"idle": 120, "run": 300, "exclusive": None}),
)
HTTP_WORKERS = 16
FORCE_MIN_GAP = 15          # a "refresh now" no more often than this, per tier
DISAGREE_IDS = 5000         # invalid IDs checked on the other fullnodes, per fullnode


class Poller(object):
    def __init__(self, source, store, mode_fn):
        self.source = source
        self.store = store
        self.mode_fn = mode_fn
        self.lock = threading.RLock()
        self.hosts, self.commented, self.hosts_error = [], [], ""
        self.order = []             # node addresses, fleet order
        self.nodes = {}             # address -> node state
        self.faucet_info = None
        self.fullnode_info = None
        self.builds = store.get("builds") or {}
        self.builds_state = {"running": False, "at": store.get("builds_at"), "error": ""}
        self.tiers = dict((name, {"last": None, "took": None, "running": False, "error": "",
                                  "force": True}) for name, _iv in TIERS)
        self.http = ThreadPoolExecutor(max_workers=HTTP_WORKERS)
        self._stop = False
        threading.Thread(target=self._loop, name="poller", daemon=True).start()

    # -- scheduling ------------------------------------------------------------

    def _loop(self):
        while not self._stop:
            mode = self.mode_fn()
            for name, intervals in TIERS:
                st = self.tiers[name]
                every = intervals[mode]
                if st["running"] or every is None:
                    continue
                if name == "inventory" and self.tiers["liveness"]["last"] is None:
                    continue
                if st["force"] or st["last"] is None or time.time() - st["last"] >= every:
                    st["running"], st["force"] = True, False
                    threading.Thread(target=self._run, args=(name,), daemon=True).start()
            time.sleep(1)

    def _run(self, name):
        st = self.tiers[name]
        t0 = time.time()
        try:
            getattr(self, "_" + name)()
            st["error"] = ""
        except Exception as e:
            st["error"] = "{}: {}".format(type(e).__name__, e)
        finally:
            st["last"], st["took"] = time.time(), round(time.time() - t0, 1)
            st["running"] = False

    def force(self, names=None):
        """Read these tiers again now (the page's Refresh). Refused while an
        exclusive unit runs, and no more than once per FORCE_MIN_GAP seconds."""
        if self.mode_fn() == "exclusive":
            return False, "an exclusive unit is running - the fleet is left alone until it ends"
        for name in names or [n for n, _ in TIERS]:
            st = self.tiers.get(name)
            if st and (st["last"] is None or time.time() - st["last"] >= FORCE_MIN_GAP):
                st["force"] = True
        return True, ""

    # -- tiers -----------------------------------------------------------------

    def _liveness(self):
        hosts, commented, err = self.source.hosts()
        with self.lock:
            self.hosts_error = err
        if err:
            return
        nodes = self.source.nodes(hosts)
        results = list(self.http.map(self.source.probe, nodes))
        now = time.time()
        with self.lock:
            self.hosts, self.commented = hosts, commented
            old, fresh, order = self.nodes, {}, []
            for n, (up, peer, note) in zip(nodes, results):
                st = dict(old.get(n["host"], {}))
                st.update({"host": n["host"], "machine": n["machine"], "slot": n["slot"],
                           "role": n["role"] or "pool", "did_target": n["did_target"],
                           "api_port": n["api_port"], "db_port": n["db_port"],
                           "unit": n["unit"], "container": n["container"],
                           "up": up, "peer_id": peer if up else st.get("peer_id"),
                           "note": note, "checked": now})
                fresh[n["host"]] = st
                order.append(n["host"])
            self.nodes, self.order = fresh, order

    def _inventory(self):
        with self.lock:
            up = [dict(n) for n in self.nodes.values() if n.get("up")]
        results = list(self.http.map(self.source.inventory, up))
        now = time.time()
        with self.lock:
            for n, (ok, dids, quorums, note) in zip(up, results):
                st = self.nodes.get(n["host"])
                if st is None:
                    continue
                if ok:
                    st["dids"] = dids
                    if quorums is not None:
                        st["quorums"] = quorums
                    st["inv_checked"] = now
                    st["inv_note"] = note
                else:
                    st["inv_note"] = note
            summary = self._totals()
        try:
            self.store.execute("INSERT OR REPLACE INTO fleet_snapshots (at, summary) VALUES (?, ?)",
                               (now, json.dumps(summary)))
        except Exception:
            pass

    def _faucet(self):
        hosts = self.hosts or self.source.hosts()[0]
        info = self.source.faucet(hosts)
        info["checked"] = time.time()
        with self.lock:
            self.faucet_info = info

    def _fullnodes(self):
        if not self.source.db_ready():
            with self.lock:
                self.fullnode_info = {"error": "no database driver on this controller "
                                               "(sudo apt install -y python3-psycopg2)",
                                      "checked": time.time()}
            return
        hosts = self.hosts or self.source.hosts()[0]
        fns = self.source.fullnode_hosts(hosts)
        stats = {}
        for fn in fns:
            try:
                stats[fn] = self.source.fullnode_stats(fn)
            except Exception as e:
                stats[fn] = {"error": "{}: {}".format(type(e).__name__, e)}
        reasons = {}
        for fn, s in stats.items():
            for _txid, reason in s.get("invalid", []):
                key = tr.fullnode_reason_key(reason)
                entry = reasons.setdefault(key, {"reason": key, "total": 0, "by": {}})
                entry["total"] += 1
                entry["by"][fn] = entry["by"].get(fn, 0) + 1
        # The same transaction accepted by one fullnode and rejected by another.
        disagree = {}
        for fn, s in stats.items():
            ids = [t for t, _r in s.get("invalid", [])][:DISAGREE_IDS]
            if not ids:
                continue
            why = dict(s.get("invalid", []))
            for other in fns:
                if other == fn or "error" in stats.get(other, {}):
                    continue
                try:
                    accepted = self.source.fullnode_accepted(other, ids)
                except Exception:
                    continue
                for t in accepted:
                    d = disagree.setdefault(t, {"txid": t, "rejected_by": [], "accepted_by": [],
                                                "reason": tr.fullnode_reason_key(why.get(t))})
                    if fn not in d["rejected_by"]:
                        d["rejected_by"].append(fn)
                    if other not in d["accepted_by"]:
                        d["accepted_by"].append(other)
        with self.lock:
            self.fullnode_info = {
                "checked": time.time(),
                "fullnodes": [{"host": fn, "processed": s.get("processed"),
                               "invalid": len(s.get("invalid", [])), "error": s.get("error", "")}
                              for fn, s in stats.items()],
                "reasons": sorted(reasons.values(), key=lambda r: -r["total"])[:40],
                "disagreements": {"count": len(disagree),
                                  "sample": sorted(disagree.values(), key=lambda d: d["txid"])[:25]},
            }

    # -- builds over SSH (on demand) ---------------------------------------------

    def collect_builds(self):
        if self.mode_fn() == "exclusive":
            return False, "an exclusive unit is running - try again when it ends"
        if self.builds_state["running"]:
            return False, "already collecting"
        with self.lock:
            nodes = [dict(n) for n in self.nodes.values() if n.get("up")]
        if not nodes:
            return False, "no live nodes known yet"
        self.builds_state.update(running=True, error="")

        def work():
            try:
                found = self.source.collect_builds(nodes)
                with self.lock:
                    self.builds = dict(found)
                self.builds_state["at"] = time.time()
                self.store.set("builds", self.builds)
                self.store.set("builds_at", self.builds_state["at"])
            except Exception as e:
                self.builds_state["error"] = "{}: {}".format(type(e).__name__, e)
            finally:
                self.builds_state["running"] = False
        threading.Thread(target=work, daemon=True).start()
        return True, ""

    # -- what the pages read -----------------------------------------------------

    def faucet_dids(self):
        info = self.faucet_info or {}
        return set(e.get("did") for e in (info.get("faucet") or {}, info.get("quorum") or {})
                   if e.get("did"))

    def pool(self):
        """Usable pool nodes, in the shape test_runner.discover_pool returns -
        what the run builder plans against. None until the fleet has been read."""
        if self.tiers["inventory"]["last"] is None:
            return None
        skip = self.faucet_dids()
        with self.lock:
            return [{"host": n["host"], "machine": n["machine"], "slot": n["slot"],
                     "did_target": n["did_target"], "dids": list(n.get("dids") or [])}
                    for h in self.order for n in [self.nodes[h]]
                    if n["role"] == "pool" and n.get("up") and n.get("dids")
                    and not (skip & set(n["dids"]))]

    def _totals(self):
        nodes = list(self.nodes.values())
        pool = [n for n in nodes if n["role"] == "pool"]
        return {"machines": len(set(n["machine"] for n in nodes)),
                "pool_machines": len(set(n["machine"] for n in pool)),
                "nodes": len(nodes), "up": sum(1 for n in nodes if n.get("up")),
                "pool_nodes": len(pool), "pool_up": sum(1 for n in pool if n.get("up")),
                "dids": sum(len(n.get("dids") or []) for n in pool),
                "did_target": sum(n["did_target"] for n in pool)}

    def snapshot(self):
        mode = self.mode_fn()
        with self.lock:
            machines = []
            by_machine = {}
            for h in self.order:
                by_machine.setdefault(self.nodes[h]["machine"], []).append(self.nodes[h])
            for e in self.hosts:
                ns = by_machine.get(e["host"], [])
                machines.append({"ip": e["host"], "role": e["role"] or "pool",
                                 "nodes": len(ns), "up": sum(1 for n in ns if n.get("up")),
                                 "dids": sum(len(n.get("dids") or []) for n in ns),
                                 "did_target": sum(n["did_target"] for n in ns)})
            nodes = []
            for h in self.order:
                n = dict(self.nodes[h])
                n["build"] = self.builds.get(h)
                nodes.append(n)
            tiers = {}
            for name, intervals in TIERS:
                st = self.tiers[name]
                tiers[name] = {"last": st["last"], "took": st["took"], "running": st["running"],
                               "error": st["error"], "every": intervals[mode],
                               "paused": intervals[mode] is None}
            return {"mode": mode, "hosts_error": self.hosts_error, "machines": machines,
                    "commented": self.commented, "nodes": nodes, "totals": self._totals(),
                    "faucet": self.faucet_info, "fullnodes": self.fullnode_info,
                    "tiers": tiers, "builds": dict(self.builds_state)}
