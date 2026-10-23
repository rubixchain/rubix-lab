"""fakefleet.py - a fake Rubix lab fleet, for the web UI's demo (controller/ui/demo.py).

It answers where the real nodes would: the node API (rubix_client.http_json and
the password-challenge step) and the database reads (db_client) - so the real
test_runner, its scheduler, the case modules, the database check, the report
writer, the run lock and the event stream all run unchanged on top of it.
validate_cases.py's stubs fill in every database helper not faked here.

The fleet has the lab's shape: four fullnodes, the faucet pair, and 26 pool
machines running the nine nodes of systems/node-layout.txt (read through
fleet.py), two nodes down and one short of a DID. Wallets hold whole and part
tokens, a quorum pledges what it signs and releases it later, ~3% of DIDs are
ex-quorums that the known checks.go:308 bug stops from spending, a quorum
refuses some transfers past ~210 at once (~120 fractional), and fullnodes
.101/.103 reject some quick re-spends that .102/.104 accept.

State is kept in a JSON file in the demo's data folder, so the UI (which reads
it) and the demo runner (which changes it) share one fleet, and balances carry
over from one demo run to the next like the real fleet's. Nothing here ever
reaches a network.
"""

import atexit
import hashlib
import json
import os
import random
import re
import threading
import time
import urllib.parse

import fleet

POOL = ["192.168.1.{}".format(n) for n in (
    107, 108, 110, 111, 112, 113, 116, 118, 119, 121, 122, 124, 126, 128, 129, 130, 131, 132,
    134, 135, 136, 137, 139, 140, 141, 144)]
COMMENTED = [{"ip": "192.168.1.{}".format(n), "note": "down - unreachable since 2026-08-18"}
             for n in (109, 114, 115, 117, 125, 127, 133, 138)]
FULLNODES = ["192.168.1.{}".format(n) for n in (101, 102, 103, 104)]
FAUCET, FAUCET_QUORUM = "192.168.1.105:20000", "192.168.1.106:20000"
DOWN = ("192.168.1.110:20800", "192.168.1.131:20400")
SHORT = {"192.168.1.118:20800": 9}          # one DID short of the layout
FLEET_TOTAL = 200000                         # each, faucet DID and faucet quorum
BUILD = "1.0.4 @ 1d9b28bb"

MILLI = (500, 100, 50, 10, 5, 1)             # part tokens, in thousandths
SPLIT = {500: (100, 5), 100: (50, 2), 50: (10, 5), 10: (5, 2), 5: (1, 5)}
B32 = "abcdefghijklmnopqrstuvwxyz234567"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def fake_hosts():
    hosts = [{"host": ip, "role": "fullnode"} for ip in FULLNODES]
    hosts += [{"host": "192.168.1.105", "role": "faucet"},
              {"host": "192.168.1.106", "role": "faucet-quorum"}]
    for n in range(107, 145):
        ip = "192.168.1.{}".format(n)
        if ip in POOL:
            hosts.append({"host": ip, "role": ""})
        elif n == 120:
            hosts.append({"host": ip, "role": "controller"})
        elif n == 123:
            hosts.append({"host": ip, "role": "explorer"})
    return hosts


def _digest(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).digest()


def _id(alphabet, n, *parts):
    d, out = _digest(*parts), []
    while len(out) < n:
        for b in d:
            out.append(alphabet[b % len(alphabet)])
        d = hashlib.sha256(d).digest()
    return "".join(out[:n])


def make_did(*parts):
    return "bafybmi" + _id(B32, 52, "did", *parts)


def make_peer(*parts):
    return "12D3KooW" + _id(B58, 44, "peer", *parts)


def make_qm(*parts):
    return "Qm" + _id(B58, 44, "qm", *parts)


def make_txid(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


# -- wallets ---------------------------------------------------------------------------
# {"w": whole tokens, "p": {"500": n, "100": n, ...} part tokens by thousandths,
#  "locked", "pledged", "committed", "burnt_ft": RBT}

def new_wallet(whole=0):
    return {"w": int(whole), "p": {}, "locked": 0.0, "pledged": 0.0, "committed": 0.0,
            "burnt_ft": 0.0}


def free(w):
    return round(w["w"] + sum(int(k) * n for k, n in w["p"].items()) / 1000.0, 3)


def give(w, amount):
    milli = int(round(amount * 1000))
    w["w"] += milli // 1000
    milli %= 1000
    for d in MILLI:
        n, milli = milli // d, milli % d
        if n:
            w["p"][str(d)] = w["p"].get(str(d), 0) + n


def take(w, amount):
    """Spend `amount` from the free tokens: whole tokens first, then parts,
    splitting a larger token when no exact parts are left. False if short."""
    milli = int(round(amount * 1000))
    if milli > int(round(free(w) * 1000)):
        return False
    whole = min(milli // 1000, w["w"])
    w["w"] -= whole
    milli -= whole * 1000
    while milli > 0:
        for d in MILLI:
            have = w["p"].get(str(d), 0)
            use = min(have, milli // d)
            if use:
                w["p"][str(d)] = have - use
                milli -= use * d
        if milli == 0:
            break
        for d in reversed(MILLI):
            if d > milli and w["p"].get(str(d), 0) > 0:
                w["p"][str(d)] -= 1
                sub, count = SPLIT[d]
                w["p"][str(sub)] = w["p"].get(str(sub), 0) + count
                break
        else:
            if w["w"] <= 0:
                return False
            w["w"] -= 1
            w["p"]["500"] = w["p"].get("500", 0) + 2
    w["p"] = dict((k, n) for k, n in w["p"].items() if n > 0)
    return True


def denoms(w):
    out = {1.0: w["w"]} if w["w"] else {}
    for k, n in w["p"].items():
        if n:
            out[int(k) / 1000.0] = n
    return out


def _wrapped(msg):
    """A refusal as a quorum's reply arrives - the node's words inside a wrapper."""
    return 'peer request failed: status=400 body={{"referenceId":"","status":false,"message":{}}}' \
        .format(json.dumps(msg))


class FakeFleet(object):
    def __init__(self, path, seed="rubix-lab-demo", writer=False):
        self.path = path
        self.seed_text = seed
        self.writer = writer
        self.lock = threading.RLock()
        self.rnd = random.Random()
        self.sleep = time.sleep          # the real one, kept before the runner scales sleeps
        self.hosts = fake_hosts()
        self.commented = list(COMMENTED)
        self.nodes = fleet.fleet(self.hosts, fleet.load_layout(), include_fixed=True)
        self.by_host = dict((n["host"], n) for n in self.nodes)
        self.tx = {}                     # txid -> {ok, verdicts {fullnode: (verdict, reason, ready_at)}}
        self.pledges = []                # [release_at, quorum did, amount]
        self.inflight = {}               # quorum did -> transfers it is signing now
        self.credited = {}               # did -> when it last received
        self.chains = {}                 # token id -> [(at, host)]
        self.mtime = None
        self.state = None
        if not self.load():
            self.seed()
            self.save()

    # -- state file --------------------------------------------------------------------

    def seed(self):
        rnd = random.Random(self.seed_text)
        dids, peers = {}, {}
        for n in self.nodes:
            k = 1 if n["role"] in ("faucet", "faucet-quorum") else SHORT.get(n["host"], n["did_target"])
            dids[n["host"]] = [make_did(self.seed_text, n["host"], i) for i in range(k)]
            peers[n["host"]] = make_peer(self.seed_text, n["host"])
        wallets, pool_dids, total = {}, [], 0
        for n in self.nodes:
            if n["role"]:
                continue
            for d in dids[n["host"]]:
                # A down node's DIDs hold nothing, so the audit's 400,000 adds up
                # without them (on the real fleet, their balance would be missing).
                whole = 0 if n["host"] in DOWN else rnd.randint(0, 120)
                wallets[d] = new_wallet(whole)
                total += whole
                pool_dids.append(d)
        faucet, fquorum = dids[FAUCET][0], dids[FAUCET_QUORUM][0]
        wallets[faucet] = new_wallet(FLEET_TOTAL - total)
        wallets[fquorum] = new_wallet(FLEET_TOTAL)
        dirty = dict((d, round(rnd.uniform(1, 30), 3)) for d in rnd.sample(pool_dids, 30))
        quorums = dict((h, []) for h in dids)
        quorums[FAUCET] = [fquorum]
        self.state = {"seed": self.seed_text, "dids": dids, "peers": peers, "wallets": wallets,
                      "dirty": dirty, "quorums": quorums, "setup": [fquorum], "ft": {},
                      "nft": {}, "sc": {}, "tx_count": 0,
                      "fullnode": {"processed": dict((f, 0) for f in FULLNODES),
                                   "invalid": dict((f, []) for f in FULLNODES)}}

    def load(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return False
        if mtime == self.mtime and self.state is not None:
            return True
        for _ in range(5):
            try:
                with open(self.path, encoding="utf-8") as fh:
                    state = json.load(fh)
                with self.lock:
                    self.state, self.mtime = state, mtime
                return True
            except (IOError, OSError, ValueError):
                self.sleep(0.05)
        return self.state is not None

    def save(self):
        with self.lock:
            data = json.dumps(self.state)
        tmp = self.path + ".tmp"
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        for _ in range(10):
            try:
                os.replace(tmp, self.path)
                self.mtime = os.path.getmtime(self.path)
                return
            except OSError:
                self.sleep(0.05)

    # -- helpers -------------------------------------------------------------------------

    def node_of(self, did):
        for host, dids in self.state["dids"].items():
            if did in dids:
                return host
        return None

    def wallet(self, did):
        return self.state["wallets"].setdefault(did, new_wallet())

    def _addr(self, host, port=20000):
        host = str(host)
        return host if ":" in host else "{}:{}".format(host, port)

    def up(self, host):
        return host in self.by_host and host not in DOWN and \
            host not in (self.state or {}).get("stopped", [])

    def ssh(self, argv):
        """What a case asks a machine to do over SSH, done to the fake fleet:
        stopping or starting a node's service (FAIL-01). Nothing else runs."""
        remote = str(argv[-1]) if argv else ""
        machine = str(argv[-2]).split("@")[-1] if len(argv) > 1 else ""
        m = re.search(r"systemctl (stop|start) rubixgoplatform@(\d+)", remote)
        if not m:
            return ""
        slot = int(m.group(2))
        index = next((e["index"] for e in fleet.load_layout() if e["slot"] == slot), 0)
        host = "{}:{}".format(machine, fleet.API_BASE + index)
        with self.lock:
            stopped = self.state.setdefault("stopped", [])
            if m.group(1) == "stop":
                self.sleep(0.5)                     # a transfer is in flight by now
                if host not in stopped:
                    stopped.append(host)
                return "KILLED rc=0\n" if "grep" in remote else "KILLED\n"
            if host in stopped:
                stopped.remove(host)
        return ""

    def faucet_dids(self):
        return self.state["dids"][FAUCET][0], self.state["dids"][FAUCET_QUORUM][0]

    # -- the node API --------------------------------------------------------------------

    def http(self, method, url, timeout=8, body=None):
        """rubix_client.http_json, answered by the fake: (ok, payload or error)."""
        u = urllib.parse.urlsplit(url)
        host, path = u.netloc, u.path
        query = urllib.parse.parse_qs(u.query)
        if not self.up(host):
            self.sleep(0.02)
            return False, "unreachable (timed out)"
        self.sleep(0.002)
        ok = {"status": True, "message": ""}
        with self.lock:
            st = self.state
            if path == "/rubix/v1/node/peer_id":
                return True, {"status": True, "message": st["peers"][host], "result": None}
            if path == "/rubix/v1/dids":
                return True, dict(ok, result=list(st["dids"][host]))
            if path == "/rubix/v1/quorums":
                return True, dict(ok, result=list(st["quorums"].get(host) or []))
            if path == "/rubix/v1/quorums/remove_all":
                st["quorums"][host] = []
                return True, dict(ok, message="quorum list removed")
            if path == "/rubix/v1/quorums/add":
                did = (body or {}).get("did")
                lst = st["quorums"].setdefault(host, [])
                if did in lst:
                    return True, {"status": False, "message": "quorum already exists"}
                lst.append(did)
                return True, dict(ok, message="quorum added")
            if path == "/rubix/v1/quorums/setup":
                did = (body or {}).get("did")
                if did not in st["dids"][host]:
                    return True, {"status": False, "message": "DID does not exist on this node"}
                if did not in st["setup"]:
                    st["setup"].append(did)
                return True, dict(ok, message="quorum setup done")
            m = re.match(r"^/rubix/v1/dids/([^/]+)/balances/(rbt|ft|nft)$", path)
            if m:
                did, kind = m.groups()
                if kind == "rbt":
                    w = self.wallet(did)
                    return True, dict(ok, result={"balance": free(w), "locked": round(w["locked"], 3),
                                                  "pledged": round(w["pledged"], 3)})
                if kind == "ft":
                    return True, dict(ok, result=[
                        {"name": name, "creator": (st["ft"].get("_series", {}).get(name) or ""),
                         "count": count, "value": round(count / 1000.0, 3)}
                        for name, count in (st["ft"].get(did) or {}).items() if count > 0])
                return True, dict(ok, result=[{"tokenId": t, "did": did}
                                              for t, n in st["nft"].items() if n["owner"] == did])
            m = re.match(r"^/rubix/v1/(nfts|smart_contracts)/([^/]+)/(chain|children|parent)$", path)
            if m:
                group, token, what = m.groups()
                if what == "chain":
                    return True, dict(ok, result=self._chain(host, token))
                n = st["nft"].get(token) or {}
                if what == "children":
                    return True, dict(ok, result=[{"childNFTId": c, "parentNFTId": token}
                                                  for c in n.get("children", [])])
                return True, dict(ok, result={"parentNFTId": n["parent"]} if n.get("parent") else None)
            if path in ("/rubix/v1/nfts/subscribe", "/rubix/v1/smart_contracts/subscribe"):
                return True, dict(ok, message="subscribed")
            if path == "/rubix/v1/nfts":
                return True, dict(ok, result=[{"tokenId": t} for t in st["nft"]])
            if path == "/rubix/v1/smart_contracts":
                return True, dict(ok, result=list(st["sc"]))
            if path == "/rubix/v1/fts":
                return True, dict(ok, result=[{"ft_name": n} for n in st["ft"].get("_series", {})])
            if path.startswith("/rubix/v1/tx/"):
                return True, dict(ok, result=[])
        return True, {"status": False, "message": "no such endpoint on the fake fleet: " + path}

    def _chain(self, host, token):
        """A token's chain as `host` sees it: other nodes see a new entry ~1 s late."""
        now = time.time()
        return [{"transactionId": make_txid(token, i)}
                for i, (at, by) in enumerate(self.chains.get(token, []))
                if by == host or now - at >= 1.0]

    def signed(self, host, action_path, body, port=20000, timeout=20):
        """rubix_client._signed_action: the action and its password step."""
        host = self._addr(host, port)
        if not self.up(host):
            self.sleep(0.02)
            return False, "request failed: unreachable (timed out)", None
        if action_path == "/rubix/v1/tx":
            return self._transaction(host, body or {})
        if action_path == "/rubix/v1/fts/mint":
            return self._mint_ft(host, body or {})
        if action_path.startswith("/rubix/v1/dids/") and action_path.endswith("/register"):
            self.sleep(0.003)
            return True, "DID registered successfully", None
        self.sleep(0.01)
        return True, "done", None

    def multipart(self, host, action_path, fields, files, port=20000, timeout=20):
        """NFT and contract creation."""
        host = self._addr(host, port)
        self.sleep(self.rnd.uniform(0.1, 0.4))
        did = fields.get("did")
        with self.lock:
            if did not in self.state["dids"].get(host, []):
                return False, "DID does not exist on this node", None
            self.state["tx_count"] += 1
            token = make_qm(action_path, did, self.state["tx_count"], time.time())
            if "nfts" in action_path:
                self.state["nft"][token] = {"owner": did, "creator": did, "children": [],
                                            "parent": None}
            else:
                self.state["sc"][token] = {"owner": did}
            self.chains[token] = []
        return True, "created", token

    def versions(self, nodes, ssh_user="rubix", workers=20):
        return dict((n["host"], BUILD) for n in nodes)

    # -- transactions -------------------------------------------------------------------

    def _refusal(self, initiator, rbt, load):
        if self.state["dirty"].get(initiator) and rbt >= 1:
            token = "60001_{}".format(abs(hash(initiator)) % 400000)
            return _wrapped('failed to get quorum DID for token {0}: tokenID "{0}" not found in '
                            'quorum tokens'.format(token))
        fractional = abs(rbt - round(rbt)) > 0.0005
        cap = 120 if fractional else 210
        if load > cap and self.rnd.random() < (load - cap) / float(load):
            return _wrapped("failed to get consensus: quorum did not respond in time "
                            "(IndividualQuorumTimeout)")
        if fractional and load > 40 and self.rnd.random() < 0.02:
            return _wrapped("PersistPreConsensus: failed to update token_denom: ERROR: deadlock "
                            "detected (SQLSTATE 40P01)")
        return None

    def _transaction(self, host, body):
        initiator, owner = body.get("initiator"), body.get("owner") or ""
        tokens = body.get("tokens") or {}
        rbt = round(float(tokens.get("rbt") or 0), 3)
        fts, nfts, scs = tokens.get("ft") or [], tokens.get("nft") or [], tokens.get("smartContract") or []
        with self.lock:
            st = self.state
            if initiator not in st["dids"].get(host, []):
                return False, "initiator DID {} not found on this node".format(initiator[:16]), None
            qlist = st["quorums"].get(host) or []
            if not qlist:
                return False, "no quorum registered on this node", None
            quorum = qlist[0]
            self.inflight[quorum] = self.inflight.get(quorum, 0) + 1
            load = self.inflight[quorum]
            sender = self.wallet(initiator)
            refusal = self._refusal(initiator, rbt, load) if rbt else None
            if not refusal and rbt and free(sender) < rbt:
                refusal = "insufficient balance: {:.3f} free, {:.3f} needed".format(free(sender), rbt)
            if not refusal and rbt and free(self.wallet(quorum)) < rbt:
                refusal = _wrapped("quorum could not pledge: insufficient balance to pledge")
            for f in fts:
                have = (st["ft"].get(initiator) or {}).get(f.get("ftName"), 0)
                if not refusal and have < int(f.get("numberOfFts") or 0):
                    refusal = "insufficient FT balance: {} of {} held".format(have, f.get("ftName"))
            for n in nfts:
                nft = st["nft"].get(n.get("nftId")) if n.get("nftId") else None
                if refusal:
                    break
                if n.get("parentNFTId"):
                    continue
                if nft is None:
                    refusal = "NFT {} not found".format(str(n.get("nftId"))[:12])
                elif tokens.get("transferNftOwnership"):
                    if owner == initiator:
                        refusal = _wrapped("NFT transfer must change ownership")
                    elif nft["owner"] != initiator:
                        refusal = _wrapped("NFT is not owned by the initiator")
            if not refusal and rbt:
                take(sender, rbt)
                sender["locked"] = round(sender["locked"] + rbt, 3)
            if not refusal:
                for f in fts:           # the sender's FTs are spoken for until the end
                    st["ft"][initiator][f.get("ftName")] -= int(f.get("numberOfFts") or 0)
        latency = self.rnd.uniform(0.25, 0.9) + load * 0.008 + (0.6 if fts else 0) + \
            (0.3 if nfts or scs else 0)
        try:
            self.sleep(latency * (0.4 if refusal else 1.0))
            if refusal:
                return False, refusal, None
            with self.lock:
                st = self.state
                st["tx_count"] += 1
                txid = make_txid(self.seed_text, initiator, owner, st["tx_count"], time.time())
                now = time.time()
                if rbt:
                    sender["locked"] = round(max(0.0, sender["locked"] - rbt), 3)
                    give(self.wallet(owner or initiator), rbt)
                    q = self.wallet(quorum)
                    if take(q, rbt):
                        q["pledged"] = round(q["pledged"] + rbt, 3)
                        self.pledges.append([now + self.rnd.uniform(5, 25), quorum, rbt])
                for f in fts:
                    n, name = int(f.get("numberOfFts") or 0), f.get("ftName")
                    held = st["ft"].setdefault(owner, {})
                    held[name] = held.get(name, 0) + n
                children = []
                for n in nfts:
                    if n.get("parentNFTId"):
                        parent = n["parentNFTId"]
                        st["tx_count"] += 1
                        child = make_qm("child", parent, st["tx_count"])
                        st["nft"][child] = {"owner": initiator, "creator": initiator,
                                            "children": [], "parent": parent}
                        st["nft"].setdefault(parent, {"children": []})["children"].append(child)
                        self.chains.setdefault(child, []).append((now, host))
                        children.append({"parentNFTId": parent, "childNFTId": child})
                        continue
                    token = n["nftId"]
                    if tokens.get("transferNftOwnership"):
                        st["nft"][token]["owner"] = owner
                    self.chains.setdefault(token, []).append((now, host))
                for s in scs:
                    self.chains.setdefault(s.get("smartContractId"), []).append((now, host))
                self._verdicts(txid, initiator, owner, now)
                self.credited[owner or initiator] = now
            result = {"transactionID": txid}
            if children:
                result["mintedNFTChildren"] = children
            return True, "Transfer finished successfully in {:.1f} secs with trnxid {}".format(
                latency, txid), result
        finally:
            with self.lock:
                self.inflight[quorum] -= 1

    def _verdicts(self, txid, initiator, owner, now):
        """What each fullnode makes of a transaction, and when it has."""
        quick = now - self.credited.get(initiator, 0) < 2.0
        rejected = quick and self.rnd.random() < 0.15
        verdicts = {}
        fn_state = self.state["fullnode"]
        for fn in FULLNODES:
            ready = now + self.rnd.uniform(0.3, 3.5)
            if rejected and fn in ("192.168.1.101", "192.168.1.103"):
                reason = ("failed to validate transaction: chain mismatch after sync: "
                          "TokenChainIntigrityCheck failed for token 60001_{}: expected prev {}, "
                          "local latest {}".format(self.rnd.randint(1, 400000),
                                                   make_txid(txid, "a")[:16],
                                                   make_txid(txid, "b")[:16]))
                verdicts[fn] = ("rejected", reason, ready)
                fn_state["invalid"][fn].append([txid, reason])
                fn_state["invalid"][fn] = fn_state["invalid"][fn][-300:]
            else:
                verdicts[fn] = ("accepted", "", ready)
            fn_state["processed"][fn] = fn_state["processed"].get(fn, 0) + 1
        self.tx[txid] = {"verdicts": verdicts}

    def _mint_ft(self, host, body):
        did, name = body.get("did"), body.get("ft_name")
        count, tokens = int(body.get("ft_count") or 0), int(body.get("token_count") or 0)
        start = int(body.get("ft_num_start_index") or 0)
        self.sleep(self.rnd.uniform(0.4, 1.5))
        with self.lock:
            st = self.state
            if did not in st["dids"].get(host, []):
                return False, "DID does not exist on this node", None
            if count > tokens * 1000:
                return False, "FT count {} exceeds {} RBT x 1000".format(count, tokens), None
            series = st["ft"].setdefault("_series", {})
            minted = st["ft"].setdefault("_minted", {})
            key = "{}|{}".format(name, did)
            if minted.get(key) and start < minted[key]:
                return False, ("InsertGenesisTokenInfo: duplicate genesis call rejected for FT "
                               "{}".format(name)), None
            w = self.wallet(did)
            if not take(w, tokens):
                return False, "insufficient balance to mint {} FTs".format(count), None
            w["burnt_ft"] = round(w["burnt_ft"] + tokens, 3)
            series[name] = did
            minted[key] = minted.get(key, 0) + count
            held = st["ft"].setdefault(did, {})
            held[name] = held.get(name, 0) + count
            st["tx_count"] += 1
            txid = make_txid("ft", did, name, st["tx_count"])
        return True, "FT minted successfully", {"transactionID": txid}

    def release_pledges(self):
        """A quorum's pledge is released some seconds later (when the token is
        spent onward, on the real fleet)."""
        now = time.time()
        with self.lock:
            due = [p for p in self.pledges if p[0] <= now]
            self.pledges = [p for p in self.pledges if p[0] > now]
            for _at, quorum, amount in due:
                q = self.wallet(quorum)
                q["pledged"] = round(max(0.0, q["pledged"] - amount), 3)
                give(q, amount)

    # -- the database --------------------------------------------------------------------

    def capture(self, host, did, *a, **k):
        with self.lock:
            w = self.wallet(did)
            totals = {"free": free(w), "locked": round(w["locked"], 3),
                      "committed": round(w["committed"], 3), "burnt_for_ft": round(w["burnt_ft"], 3),
                      "burnt": 0.0, "pledged": round(w["pledged"], 3)}
        return {"host": host, "did": did, "tokens": {}, "chain_len": {}, "denom": {},
                "totals": totals}

    def transactions_present(self, host, ids, *a, **k):
        return set(t for t in ids if t in self.tx)

    def _verdict(self, fn, txid):
        v = (self.tx.get(txid) or {}).get("verdicts", {}).get(fn)
        if not v or time.time() < v[2]:
            return None
        return v

    def query(self, host, sql, params=None, *a, **k):
        params = params or ()
        if "fullnode_transactions WHERE id = ANY" in sql:
            return [(t,) for t in params[0] if (self._verdict(host, t) or ("",))[0] == "accepted"]
        if "fullnode_invalid_transactions" in sql and "ANY" in sql:
            return [(t, v[1]) for t in params[0] for v in [self._verdict(host, t)]
                    if v and v[0] == "rejected"]
        if "UNION ALL" in sql and "fullnode_transactions" in sql:
            return [(1,)] if self._verdict(host, params[0]) else []
        if "FROM transactions WHERE id = ANY" in sql:
            return [(t,) for t in params[0] if t in self.tx]
        return []

    def query_snapshot(self, host, statements, *a, **k):
        """The fleet audit's per-node snapshot (audit_cases._read_node): its
        first three statements (status totals, token_denom, free rows) from the
        fake wallets, and an empty result for every statement after them -
        the fake fleet has no duplicate, negative, chainless or stuck tokens.
        One result per statement, however many the audit asks."""
        first = statements[0][0] if statements else ""
        if "GROUP BY did, token_status" not in first:
            return [[] for _ in statements]
        dids = statements[0][1][1]
        status_rows, denom_rows, free_rows = [], [], []
        with self.lock:
            for did in dids:
                w = self.wallet(did)
                for status, value in ((0, free(w)), (1, w["locked"]), (5, w["committed"]),
                                      (6, w["pledged"]), (9, w["burnt_ft"])):
                    if value:
                        status_rows.append((did, status, value))
                for value, count in denoms(w).items():
                    denom_rows.append((did, value, count))
                    free_rows.append((did, value, count))
        return [status_rows, denom_rows, free_rows] + [[] for _ in statements[3:]]

    def ex_pledged(self, host, did, *a, **k):
        return float(self.state["dirty"].get(did, 0.0))

    def real_free_denoms(self, host, did, *a, **k):
        with self.lock:
            return denoms(self.wallet(did))

    def free_token_values(self, host, did, *a, **k):
        with self.lock:
            out = []
            for value, count in sorted(denoms(self.wallet(did)).items(), reverse=True):
                out += [value] * min(count, 5000)
            return out

    def value_in_status(self, host, did, status, *a, **k):
        with self.lock:
            w = self.wallet(did)
            return {0: free(w), 1: w["locked"], 5: w["committed"], 6: w["pledged"],
                    9: w["burnt_ft"]}.get(int(status), 0.0)

    def wallet_totals(self, host, did, *a, **k):
        with self.lock:
            w = self.wallet(did)
            return dict((s, (v, 1)) for s, v in ((0, free(w)), (1, w["locked"]), (5, w["committed"]),
                                                  (6, w["pledged"]), (9, w["burnt_ft"])) if v)


def install_runner(fake, data_dir):
    """Point the real runner at the fake fleet and at the demo's own folder.
    Only demo_runner.py calls this, in its own process."""
    import case_helpers
    import db_client as db
    import rubix_client as rc
    import run_lock
    import test_runner as tr
    import validate_cases

    saved = dict((k, v) for k, v in vars(rc).items() if callable(v))
    validate_cases.install_stubs()          # database and wallet_shapes stubs
    for k, v in saved.items():              # ...but the real API client
        setattr(rc, k, v)
    rc.http_json = fake.http
    rc._signed_action = fake.signed
    rc.signed_multipart_action = fake.multipart
    rc.collect_versions = fake.versions
    db.available = lambda: True
    db.capture = fake.capture
    db.transactions_present = fake.transactions_present
    db.query = fake.query
    db.query_snapshot = fake.query_snapshot
    db.ex_pledged_free_value = fake.ex_pledged
    db.real_free_denoms = fake.real_free_denoms
    db.denom_counter = fake.real_free_denoms
    db.free_token_values = fake.free_token_values
    db.wallet_totals = fake.wallet_totals
    db.value_in_status = fake.value_in_status
    fleet.load_hosts = lambda path=None: fake.hosts

    # The fake fleet uses the lab's own addresses, so nothing in a demo run may
    # reach a real machine: ssh/scp are answered by the fake (FAIL-01 stops and
    # starts nodes that way), and any other way of starting them is refused.
    import subprocess
    real_run, real_popen, real_system = subprocess.run, subprocess.Popen, os.system

    def remote(argv):
        argv = argv if isinstance(argv, (list, tuple)) else str(argv).split()
        return bool(argv) and os.path.basename(str(argv[0])).lower() in (
            "ssh", "scp", "ssh.exe", "scp.exe")

    def run(args, *a, **k):
        if remote(args):
            out = fake.ssh(list(args) if isinstance(args, (list, tuple)) else str(args).split())
            text = k.get("text") or k.get("universal_newlines")
            return subprocess.CompletedProcess(args, 0, out if text else out.encode(),
                                               "" if text else b"")
        return real_run(args, *a, **k)

    class Popen(real_popen):
        def __init__(self, args, *a, **k):
            if remote(args):
                raise OSError("the demo never connects to a machine")
            real_popen.__init__(self, args, *a, **k)

    def system(cmd):
        if "ssh" in str(cmd) or "scp" in str(cmd):
            raise OSError("the demo never connects to a machine")
        return real_system(cmd)
    subprocess.run, subprocess.Popen, os.system = run, Popen, system

    reports = os.path.join(data_dir, "reports")
    tr.LEDGER_PATH = os.path.join(data_dir, "fleet-ledger.json")
    case_helpers.LIMITS_PATH = os.path.join(data_dir, "lab-limits.json")
    run_lock.LOCK_PATH = os.path.join(data_dir, "fleet-run.lock")

    def stamp():
        return time.strftime("%Y-%m-%d_%H-%M-%S")

    def new_report_paths(script_name, exts=("pdf", "json")):
        ts, out = stamp(), {}
        for ext in exts:
            os.makedirs(os.path.join(reports, ext), exist_ok=True)
            out[ext] = os.path.join(reports, ext, "{}_{}.{}".format(script_name, ts, ext))
        return out

    def new_report_path(script_name, ext="pdf"):
        return new_report_paths(script_name, (ext,))[ext]

    def start_log(script_name):
        import sys
        folder = os.path.join(reports, "logs")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, "{}_{}.log".format(script_name, stamp()))
        fh = open(path, "a", encoding="utf-8")
        lock = threading.Lock()
        sys.stdout = rc._Tee(sys.stdout, fh, lock)
        sys.stderr = rc._Tee(sys.stderr, fh, lock)
        print("Log: {}".format(path))
        return path

    rc.new_report_paths, rc.new_report_path, rc.start_log = \
        new_report_paths, new_report_path, start_log

    # Waits in the cases (settle windows, polls) pass 20x faster; the fake's own
    # latency keeps real time, so timings still look like timings.
    real_sleep = time.sleep
    time.sleep = lambda seconds=0: real_sleep(max(0.0, seconds) * 0.05)

    def background():
        while True:
            real_sleep(1.0)
            fake.release_pledges()
            fake.save()
    threading.Thread(target=background, daemon=True).start()
    atexit.register(fake.save)
