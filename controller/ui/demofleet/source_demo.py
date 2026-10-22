"""source_demo.py - the web UI's view of the fake fleet (fakefleet.py), with
the same interface as labui/source_real.py. It only reads: the demo runner is
what changes the fake fleet, in its own process, and this re-reads the shared
state file when it changes."""

import os
import time

import fleet
import run_lock

import fakefleet


class DemoSource(object):
    demo = True

    def __init__(self, cfg):
        self.cfg = cfg
        self.fake = fakefleet.FakeFleet(os.path.join(cfg.data_dir, "fake-fleet.json"))

    def _state(self):
        self.fake.load()
        return self.fake.state

    def hosts(self):
        return list(self.fake.hosts), list(self.fake.commented), ""

    def nodes(self, hosts):
        return fleet.fleet(hosts, fleet.load_layout(), include_fixed=True)

    def probe(self, node):
        time.sleep(0.002)
        if not self.fake.up(node["host"]):
            return False, None, "unreachable (timed out)"
        return True, self._state()["peers"][node["host"]], ""

    def inventory(self, node):
        st = self._state()
        return True, list(st["dids"].get(node["host"], [])), \
            list(st["quorums"].get(node["host"]) or []), ""

    def faucet(self, hosts):
        st = self._state()
        faucet, quorum = self.fake.faucet_dids()
        out = {"ok": True, "note": "", "faucet_quorums": list(st["quorums"].get(fakefleet.FAUCET) or [])}
        for key, did, host in (("faucet", faucet, fakefleet.FAUCET),
                               ("quorum", quorum, fakefleet.FAUCET_QUORUM)):
            w = st["wallets"].get(did) or fakefleet.new_wallet()
            out[key] = {"host": host, "did": did, "balance": fakefleet.free(w),
                        "locked": round(w["locked"], 3), "pledged": round(w["pledged"], 3)}
        ok = out["faucet_quorums"] == [quorum]
        out["ok"] = ok
        out["note"] = ("faucet holds {:.3f} RBT free".format(out["faucet"]["balance"]) if ok else
                       "faucet node must have exactly one quorum registered, the faucet quorum")
        return out

    def faucet_check(self):
        info = self.faucet(None)
        return info["ok"], info["note"]

    def locked_rbt(self, host, did):
        w = self._state()["wallets"].get(did)
        return round(w["locked"], 3) if w else None

    def fullnode_hosts(self, hosts):
        return list(fakefleet.FULLNODES)

    def db_ready(self):
        return True

    def fullnode_stats(self, fn):
        f = self._state()["fullnode"]
        return {"processed": f["processed"].get(fn, 0),
                "invalid": [tuple(x) for x in f["invalid"].get(fn, [])]}

    def fullnode_accepted(self, fn, ids):
        rejected = set(t for t, _r in self._state()["fullnode"]["invalid"].get(fn, []))
        return set(t for t in ids if t not in rejected)

    def collect_builds(self, nodes):
        time.sleep(1.5)
        return self.fake.versions(nodes)

    def lock_holder(self):
        return run_lock.holder(self.cfg.lock_path)
