#!/usr/bin/env python3
"""
dids-to-excel.py - bring every node up to the number of DIDs it should hold,
register each new DID, and record the fleet's DIDs in an .xlsx.

How many DIDs a node should hold:
    pool node                the `dids` column of systems/node-layout.txt
    faucet, faucet-quorum    1 - the faucet DID and the faucet quorum DID
    fullnode                 0
(explorer and controller machines run no node.)

APIs used (confirmed against server/did.go, core/did.go, did/lite.go and
setup/setup.go in the rubixgoplatform repo):
    GET  /rubix/v1/dids                  -> {"result": [<did>, ...]}
    POST /rubix/v1/dids/create            body {"password": "<DID_PASSWORD>"}
                                          -> {"result": {"did": "...", "peer_id": "..."}}
    POST /rubix/v1/dids/{did}/register    -> {"result": {"id": "<reqID>"}}  (password challenge)
    POST /rubix/v1/signature               body {"id": "<reqID>", "password": "<DID_PASSWORD>"}
                                          -> {"status": true, "message": "DID registered successfully"}

An empty password is NOT possible here -- core.CreateDID rejects it outright
("password required for creating did", did/did.go:78-79). DID_PASSWORD below
is "mypassword" -- the product's own built-in default (command/command.go:417,
-privPWD flag) and the convention its own integration test suite uses
throughout (test/integration/, tests/node/), not a lab-invented value.

Safety rules (do not relax these — CreateDID has zero idempotency in the
product code: calling it never fails, it just mints one more identity):
    - The DID list is read fresh before EVERY create and again after it. A
      create counts only when the node then holds exactly one more DID.
      Anything else - no change, two more, a list that cannot be read, a
      create that timed out - stops that node and flags it for a human.
      Nothing is ever retried automatically.
    - One create at a time per node (nodes run in parallel).
    - A node at its target is left alone; one ABOVE its target is flagged
      and never touched - extra DIDs are not deleted.
    - Unreachable nodes are skipped.

Requires openpyxl (not stdlib):
    pip install openpyxl

Usage:
    python3 dids-to-excel.py                        # pool nodes
    python3 dids-to-excel.py --include-fixed        # plus the faucet nodes - creates the faucet DIDs
    python3 dids-to-excel.py --host 192.168.1.107   # one machine
    python3 dids-to-excel.py --dry-run              # report what is missing, create nothing
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from openpyxl import Workbook
except ImportError:
    sys.exit("ERROR: openpyxl is required. Install it with:\n  pip install openpyxl")

import fleet

DEFAULT_TIMEOUT = 5
CREATE_TIMEOUT = 60     # key generation + IPFS add; generous so a slow node is not cut off
REGISTER_TIMEOUT = 15   # register+signature round trip
TIMEOUT_SETTLE = 15     # after a create times out, wait this long before re-reading the list

EP_DIDS = "/rubix/v1/dids"
EP_CREATE = "/rubix/v1/dids/create"
EP_REGISTER = "/rubix/v1/dids/{did}/register"
EP_SIGNATURE = "/rubix/v1/signature"

DID_PASSWORD = "mypassword"  # the product's own default (command/command.go -privPWD)


def http_json(method, url, timeout, body=None):
    """Send a GET/POST and return (ok, payload_or_error_string)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def get_dids(address, timeout):
    """GET /rubix/v1/dids. Returns (reachable, dids, note)."""
    ok, payload = http_json("GET", "http://{}{}".format(address, EP_DIDS), timeout)
    if not ok:
        return False, [], payload
    result = payload.get("result") if isinstance(payload, dict) else None
    return True, (result if isinstance(result, list) else []), ""


def register(address, did):
    """Announce a DID (register + signature). Returns (ok, note)."""
    base = "http://{}".format(address)
    ok, payload = http_json("POST", base + EP_REGISTER.format(did=did), REGISTER_TIMEOUT)
    if not ok or not isinstance(payload, dict):
        return False, "register(step1) failed: {}".format(payload)
    req_id = (payload.get("result") or {}).get("id")
    if not req_id:
        return False, "register(step1) gave no request id: {}".format(payload)
    ok, payload = http_json("POST", base + EP_SIGNATURE, REGISTER_TIMEOUT,
                            {"id": req_id, "password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        return False, "register(step2/signature) failed: {}".format(
            payload if not ok else payload.get("message"))
    return True, ""


def provision(node, timeout, dry_run):
    """Bring one node up to its target. Returns its row for the report."""
    address, target = node["host"], node["did_target"]
    row = {"node": node, "reachable": False, "dids": [], "created": [], "action": "", "note": ""}
    reachable, dids, note = get_dids(address, timeout)
    row["reachable"], row["dids"] = reachable, list(dids)
    if not reachable:
        row["note"] = note
        return row
    if len(dids) > target:
        row["note"] = "holds {} DIDs, target is {} - needs human review".format(len(dids), target)
        return row
    if len(dids) == target:
        return row
    if dry_run:
        row["action"] = "would create {}".format(target - len(dids))
        return row

    problems = []
    while len(row["dids"]) < target:
        before = list(row["dids"])
        ok, payload = http_json("POST", "http://{}{}".format(address, EP_CREATE), CREATE_TIMEOUT,
                                {"password": DID_PASSWORD})
        if not ok and payload == "timeout":
            # The node may still finish the create. Wait, then judge by the list.
            time.sleep(TIMEOUT_SETTLE)
        reachable, after, note = get_dids(address, timeout)
        if not reachable:
            problems.append("DID list unreadable after a create ({}) - stopped".format(note))
            break
        new = [d for d in after if d not in before]
        if len(after) != len(before) + 1 or len(new) != 1:
            answered_ok = ok and isinstance(payload, dict) and payload.get("status")
            answer = payload.get("message") if ok and isinstance(payload, dict) else payload
            problems.append("after a create the node went from {} to {} DIDs{} - stopped, "
                            "check by hand".format(len(before), len(after),
                                                   "" if answered_ok else
                                                   " (create answered: {})".format(answer)))
            row["dids"] = list(after)
            break
        row["dids"] = list(after)
        row["created"].append(new[0])
        ok, note = register(address, new[0])
        if not ok:
            # The DID exists either way; the runner announces every DID before a run.
            problems.append("{}...: {}".format(new[0][:16], note))
    row["action"] = "created {}".format(len(row["created"])) if row["created"] else ""
    row["note"] = "; ".join(problems)
    return row


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=fleet.HOSTS_PATH,
                   help="hosts file (default: hosts.txt beside this script)")
    p.add_argument("--host", default="", metavar="ADDRESS", help="just this machine (any role)")
    p.add_argument("--include-fixed", action="store_true",
                   help="also the fullnode and faucet nodes")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--out", default=os.path.join(here, "dids.xlsx"))
    p.add_argument("--dry-run", action="store_true",
                   help="report what is missing, never create or register")
    args = p.parse_args()

    nodes = fleet.fleet(fleet.load_hosts(args.hosts), include_fixed=args.include_fixed,
                        host=args.host or None)
    if not nodes:
        sys.exit("No nodes selected (explorer and controller machines run none).")
    want = sum(n["did_target"] for n in nodes)
    print("{} node(s) on {} machine(s), {} DID(s) in the layout{}...\n".format(
        len(nodes), len(set(n["machine"] for n in nodes)), want,
        " - dry run" if args.dry_run else ""))

    with ThreadPoolExecutor(max_workers=min(20, len(nodes))) as pool:
        rows = list(pool.map(lambda n: provision(n, args.timeout, args.dry_run), nodes))

    width = max(len(r["node"]["host"]) for r in rows)
    header = "{:<{w}}  {:>4}  {:<13}  {:<6}  {:>7}  {}".format(
        "NODE", "SLOT", "ROLE", "STATUS", "DIDs", "ACTION / NOTE", w=width)
    print(header)
    print("-" * len(header))
    for r in rows:
        n = r["node"]
        detail = "; ".join(x for x in (r["action"], r["note"]) if x)
        print("{:<{w}}  {:>4}  {:<13}  {:<6}  {:>7}  {}".format(
            n["host"], n["slot"], n["role"] or "pool", "OK" if r["reachable"] else "DOWN",
            "{}/{}".format(len(r["dids"]), n["did_target"]), detail, w=width))

    created = sum(len(r["created"]) for r in rows)
    held = sum(len(r["dids"]) for r in rows if r["reachable"])
    flagged = [r for r in rows if r["note"]]
    print("\n{} DID(s) held of {} in the layout; {} created this run.".format(held, want, created))
    if flagged:
        print("{} node(s) need a look - see ACTION / NOTE above.".format(len(flagged)))

    # The faucet DIDs are allowlisted minters, written into the product build.
    for r in rows:
        if r["node"]["role"] in ("faucet", "faucet-quorum") and len(r["dids"]) == 1:
            print("\n{} DID ({}): {}".format(r["node"]["role"].replace("-", " "),
                                             r["node"]["host"], r["dids"][0]))
    if any(r["node"]["role"] == "faucet" and r["created"] for r in rows):
        print("\nNEW faucet DIDs: put both in CustomNetAllowedMinters "
              "(core/minterallowlist/mint_access_control.go) and deploy that build to EVERY "
              "node before minting - every node rejects tokens from a minter not in that list.")

    wb = Workbook()
    ws = wb.active
    ws.title = "Nodes"
    ws.append(["Node", "Machine", "Slot", "Role", "Status", "DIDs Held", "Target",
               "Action Taken", "Note", "Checked At"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in rows:
        n = r["node"]
        ws.append([n["host"], n["machine"], n["slot"], n["role"] or "pool",
                   "OK" if r["reachable"] else "DOWN", len(r["dids"]), n["did_target"],
                   r["action"], r["note"], checked_at])
    for col, width in zip("ABCDEFGHIJ", (22, 16, 6, 14, 8, 10, 8, 16, 60, 20)):
        ws.column_dimensions[col].width = width

    ws2 = wb.create_sheet("DIDs")
    ws2.append(["Node", "Machine", "Slot", "Role", "DID", "Created This Run"])
    for r in rows:
        n = r["node"]
        for d in r["dids"]:
            ws2.append([n["host"], n["machine"], n["slot"], n["role"] or "pool", d,
                        "yes" if d in r["created"] else ""])
    for col, width in zip("ABCDEF", (22, 16, 6, 14, 65, 16)):
        ws2.column_dimensions[col].width = width
    wb.save(args.out)
    print("\nSaved to {}".format(args.out))
    sys.exit(1 if flagged else 0)


if __name__ == "__main__":
    main()
