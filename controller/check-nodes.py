#!/usr/bin/env python3
"""
check-nodes.py - Verify the controller can reach every lab node.

Run this from the controller BEFORE building anything else. It answers one
question: which nodes are up, reachable from here, and properly set up. One
row per NODE - a pool machine runs several (systems/node-layout.txt), each on
its own port.

Uses only the Python standard library - no pip install needed.

Usage:
    python3 check-nodes.py                       # pool nodes, from hosts.txt
    python3 check-nodes.py --include-fixed       # plus the fullnode and faucet nodes
    python3 check-nodes.py --host 192.168.1.107  # one machine
    python3 check-nodes.py --timeout 5 --out inventory.json

Exit code is 0 only if every selected node responded.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import fleet

DEFAULT_TIMEOUT = 5

# All GET, confirmed against server/server.go route registration. Liveness is
# read from node/peer_id: node/ping pings ANOTHER peer and logs
# "PeerID cannot be empty" as an ERROR when called bare (server/basic.go:49).
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


def check_node(node, timeout):
    """Check one node. Returns a dict describing its state."""
    base = "http://{}".format(node["host"])
    info = {
        "host": node["host"],
        "machine": node["machine"],
        "slot": node["slot"],
        "role": node["role"] or "pool",
        "api": base,
        "reachable": False,
        "dids": [],
        "did_count": 0,
        "did_target": node["did_target"],
        "peer_id": None,
        "quorums": [],
        "rbt_balance": None,
        "status": "",
        "notes": [],
    }

    ok, payload = get_json(base, EP_PEER_ID, timeout)
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

    # The peer ID comes back in "message", not "result"
    # (server/node.go APIPeerID -> BasicResponse(req, true, GetPeerID(), nil)).
    ok, payload = get_json(base, EP_PEER_ID, timeout)
    if ok and isinstance(payload, dict):
        info["peer_id"] = payload.get("message") or None
    elif not ok:
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
            if isinstance(res, dict) and "balance" in res:
                info["rbt_balance"] = res["balance"]
            elif isinstance(res, (int, float)):
                info["rbt_balance"] = res

    if info["did_count"] == 0 and info["did_target"]:
        info["status"] = "NO DID"
        info["notes"].append("reachable but no DID yet - run dids-to-excel.py")
    else:
        info["status"] = "OK"
    if info["did_count"] != info["did_target"]:
        info["notes"].append("holds {} DID(s), layout says {}".format(
            info["did_count"], info["did_target"]))

    return info


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=fleet.HOSTS_PATH,
                   help="hosts file (default: hosts.txt beside this script)")
    p.add_argument("--host", default="", metavar="ADDRESS",
                   help="just this machine (any role)")
    p.add_argument("--include-fixed", action="store_true",
                   help="also the fullnode and faucet nodes")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="per-request timeout in seconds (default: %(default)s)")
    p.add_argument("--out", default=os.path.join(here, "inventory.json"),
                   help="where to write the inventory (default: inventory.json)")
    args = p.parse_args()

    nodes = fleet.fleet(fleet.load_hosts(args.hosts), include_fixed=args.include_fixed,
                        host=args.host or None)
    if not nodes:
        sys.exit("No nodes selected (explorer and controller machines run none).")
    machines = len(set(n["machine"] for n in nodes))
    print("Checking {} node(s) on {} machine(s)...\n".format(len(nodes), machines))

    with ThreadPoolExecutor(max_workers=min(40, len(nodes))) as pool:
        results = list(pool.map(lambda n: check_node(n, args.timeout), nodes))

    width = max(len(r["host"]) for r in results)
    header = "{:<{w}}  {:>4}  {:<13}  {:<8}  {:>7}  {:>8}  {}".format(
        "NODE", "SLOT", "ROLE", "STATUS", "DIDs", "RBT", "NOTES", w=width)
    print(header)
    print("-" * len(header))
    for r in results:
        bal = "-" if r["rbt_balance"] is None else "{}".format(r["rbt_balance"])
        print("{:<{w}}  {:>4}  {:<13}  {:<8}  {:>7}  {:>8}  {}".format(
            r["host"], r["slot"], r["role"], r["status"],
            "{}/{}".format(r["did_count"], r["did_target"]), bal,
            "; ".join(r["notes"]), w=width))

    up = [r for r in results if r["reachable"]]
    down = [r for r in results if not r["reachable"]]
    no_did = [r for r in results if r["status"] == "NO DID"]
    off_target = [r for r in up if r["did_count"] != r["did_target"]]

    print("\nReachable : {}/{} node(s)".format(len(up), len(results)))
    print("DIDs      : {} held, {} in the layout".format(
        sum(r["did_count"] for r in up), sum(r["did_target"] for r in results)))
    if down:
        print("Down      : {}".format(", ".join(r["host"] for r in down)))
    if no_did:
        print("No DID    : {}".format(", ".join(r["host"] for r in no_did)))
    if off_target:
        print("Off target: {} node(s) - see NOTES".format(len(off_target)))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"nodes": results}, fh, indent=2)
    print("\nInventory written to {}".format(args.out))

    # Non-zero if anything did not respond, so this can gate a later step.
    sys.exit(0 if not down else 1)


if __name__ == "__main__":
    main()
