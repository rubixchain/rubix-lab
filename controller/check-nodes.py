#!/usr/bin/env python3
"""
check-nodes.py - Verify the controller can reach every lab node.

Run this from the controller BEFORE building anything else. It answers one
question: which nodes are up, reachable from here, and properly set up.

Uses only the Python standard library - no pip install needed.

Usage:
    python3 check-nodes.py                    # reads hosts.txt in this folder
    python3 check-nodes.py --hosts other.txt
    python3 check-nodes.py --port 20000 --timeout 5
    python3 check-nodes.py --out inventory.json

Exit code is 0 only if every host in the file responded.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_PORT = 20000
DEFAULT_TIMEOUT = 5

# All GET, confirmed against server/server.go route registration.
EP_PING = "/rubix/v1/node/ping"
EP_DIDS = "/rubix/v1/dids"
EP_QUORUMS = "/rubix/v1/quorums"
EP_PEER_ID = "/rubix/v1/node/peer_id"
EP_RBT_BALANCE = "/rubix/v1/dids/{did}/balances/rbt"


def get_json(base_url, path, timeout):
    """GET path and return (ok, payload_or_error_string)."""
    url = base_url + path
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, "HTTP {}".format(resp.status)
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, "HTTP {}".format(e.code)
    except urllib.error.URLError as e:
        return False, "unreachable ({})".format(e.reason)
    except TimeoutError:
        return False, "timeout"
    except Exception as e:  # malformed JSON, connection reset, etc.
        return False, "{}: {}".format(type(e).__name__, e)


def result_of(payload):
    """Pull .result out of the BasicResponse envelope, tolerating shapes."""
    if isinstance(payload, dict):
        return payload.get("result")
    return None


def check_host(entry, port, timeout):
    """Check one node. Returns a dict describing its state."""
    host = entry["host"]
    base = "http://{}:{}".format(host, port)
    info = {
        "host": host,
        "role": entry["role"],
        "api": base,
        "reachable": False,
        "dids": [],
        "did_count": 0,
        "peer_id": None,
        "quorums": [],
        "rbt_balance": None,
        "status": "",
        "notes": [],
    }

    ok, payload = get_json(base, EP_PING, timeout)
    if not ok:
        info["status"] = "DOWN"
        info["notes"].append(str(payload))
        return info

    info["reachable"] = True

    ok, payload = get_json(base, EP_DIDS, timeout)
    if ok:
        res = result_of(payload)
        if isinstance(res, list):
            dids = []
            for item in res:
                if isinstance(item, dict):
                    d = item.get("did") or item.get("DID")
                    if d:
                        dids.append(d)
                elif isinstance(item, str):
                    dids.append(item)
            info["dids"] = dids
            info["did_count"] = len(dids)
    else:
        info["notes"].append("dids: {}".format(payload))

    ok, payload = get_json(base, EP_PEER_ID, timeout)
    if ok:
        res = result_of(payload)
        if isinstance(res, str):
            info["peer_id"] = res
        elif isinstance(res, dict):
            info["peer_id"] = res.get("peerID") or res.get("peer_id")
    else:
        info["notes"].append("peer_id: {}".format(payload))

    ok, payload = get_json(base, EP_QUORUMS, timeout)
    if ok:
        res = result_of(payload)
        if isinstance(res, list):
            quorums = []
            for item in res:
                if isinstance(item, dict):
                    q = item.get("did") or item.get("DID")
                    if q:
                        quorums.append(q)
                elif isinstance(item, str):
                    quorums.append(item)
            info["quorums"] = quorums

    # Balance of the first DID, as a quick funding sanity check.
    if info["dids"]:
        ok, payload = get_json(
            base, EP_RBT_BALANCE.format(did=info["dids"][0]), timeout
        )
        if ok:
            res = result_of(payload)
            if isinstance(res, dict):
                for key in ("rbt_amount", "rbtAmount", "balance", "value"):
                    if key in res:
                        info["rbt_balance"] = res[key]
                        break
            elif isinstance(res, (int, float)):
                info["rbt_balance"] = res

    if info["did_count"] == 0:
        info["status"] = "NO DID"
        info["notes"].append("reachable but no DID yet - needs bootstrap")
    else:
        info["status"] = "OK"

    return info


def load_hosts(path, default_role):
    """Read the hosts file. Format per line:  <host> [role]  ('#' comments ok)."""
    if not os.path.exists(path):
        sys.exit("ERROR: hosts file not found: {}\n"
                 "Create it with one host per line, e.g.\n"
                 "  10.0.0.11  quorum\n"
                 "  10.0.0.20  participant".format(path))
    entries = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            entries.append({
                "host": parts[0],
                "role": parts[1] if len(parts) > 1 else default_role,
            })
    if not entries:
        sys.exit("ERROR: no hosts listed in {}".format(path))
    return entries


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=os.path.join(here, "hosts.txt"),
                   help="hosts file (default: hosts.txt beside this script)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="node API port (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="per-request timeout in seconds (default: %(default)s)")
    p.add_argument("--out", default=os.path.join(here, "inventory.json"),
                   help="where to write the inventory (default: inventory.json)")
    p.add_argument("--default-role", default="pool",
                   help="role label for hosts with none listed (default: %(default)s) — "
                        "untagged hosts are the generic pool, not a fixed role")
    args = p.parse_args()

    entries = load_hosts(args.hosts, args.default_role)
    print("Checking {} host(s) on port {}...\n".format(len(entries), args.port))

    with ThreadPoolExecutor(max_workers=min(20, len(entries))) as pool:
        results = list(pool.map(
            lambda e: check_host(e, args.port, args.timeout), entries))

    width = max(len(r["host"]) for r in results)
    header = "{:<{w}}  {:<12}  {:<8}  {:>5}  {:>8}  {}".format(
        "HOST", "ROLE", "STATUS", "DIDs", "RBT", "NOTES", w=width)
    print(header)
    print("-" * len(header))
    for r in sorted(results, key=lambda x: (x["status"] != "OK", x["host"])):
        bal = "-" if r["rbt_balance"] is None else "{}".format(r["rbt_balance"])
        print("{:<{w}}  {:<12}  {:<8}  {:>5}  {:>8}  {}".format(
            r["host"], r["role"], r["status"], r["did_count"], bal,
            "; ".join(r["notes"]), w=width))

    up = [r for r in results if r["reachable"]]
    ready = [r for r in results if r["status"] == "OK"]
    down = [r for r in results if not r["reachable"]]
    no_did = [r for r in results if r["reachable"] and r["did_count"] == 0]

    print("\nReachable : {}/{}".format(len(up), len(results)))
    print("Ready     : {}/{}   (reachable and has a DID)".format(
        len(ready), len(results)))
    if down:
        print("Down      : {}".format(", ".join(r["host"] for r in down)))
    if no_did:
        print("No DID    : {}".format(", ".join(r["host"] for r in no_did)))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"port": args.port, "nodes": results}, fh, indent=2)
    print("\nInventory written to {}".format(args.out))

    # Non-zero if anything did not respond, so this can gate a later step.
    sys.exit(0 if len(down) == 0 else 1)


if __name__ == "__main__":
    main()
