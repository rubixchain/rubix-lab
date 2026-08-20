#!/usr/bin/env python3
"""
dids-to-excel.py - Sweep an IP range for Rubix nodes, create+register a DID
on any node that doesn't have one yet, and record the result in an .xlsx.

APIs used (confirmed against server/did.go, core/did.go, did/lite.go and
setup/setup.go in the rubixgoplatform repo):
    GET  /rubix/v1/dids                  -> {"result": [<did>, ...]}
    POST /rubix/v1/dids/create            body {"password": ""}
                                          -> {"result": {"did": "...", "peer_id": "..."}}
    POST /rubix/v1/dids/{did}/register    -> {"result": {"id": "<reqID>"}}  (password challenge)
    POST /rubix/v1/signature               body {"id": "<reqID>", "password": ""}
                                          -> {"status": true, "message": "DID registered successfully"}

Password is sent as an empty string throughout, per lab convention (no
passphrase needed for the DID's private key on this fleet).

Safety rules (do not relax these — CreateDID has zero idempotency in the
product code; calling it on a node that already has a DID silently mints a
second identity with no error):
    - A host with exactly 1 DID already: left alone, no action.
    - A host with 0 DIDs: create + register exactly once.
    - A host with >1 DID (ambiguous): skipped and flagged for a human, never
      auto-created.
    - Unreachable hosts: skipped entirely.

Requires openpyxl (not stdlib):
    pip install openpyxl

Usage:
    python3 dids-to-excel.py                              # the fixed fleet range, port 20000
    python3 dids-to-excel.py --dry-run                    # sweep + report only, no create/register
"""

import argparse
import datetime
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

try:
    from openpyxl import Workbook
except ImportError:
    sys.exit("ERROR: openpyxl is required. Install it with:\n  pip install openpyxl")

# Fixed fleet range. .142 and .143 are other systems, not part of this lab.
FLEET_SUBNET = "192.168.1"
FLEET_START = 101
FLEET_END = 144
FLEET_EXCLUDE = {142, 143}
FLEET_HOSTS = ["{}.{}".format(FLEET_SUBNET, i)
               for i in range(FLEET_START, FLEET_END + 1) if i not in FLEET_EXCLUDE]

DEFAULT_PORT = 20000
DEFAULT_TIMEOUT = 5
REGISTER_TIMEOUT = 15  # register+signature round trip, a bit more generous

EP_DIDS = "/rubix/v1/dids"
EP_CREATE = "/rubix/v1/dids/create"
EP_REGISTER = "/rubix/v1/dids/{did}/register"
EP_SIGNATURE = "/rubix/v1/signature"

DID_PASSWORD = ""  # lab convention: no passphrase


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


def get_dids(host, port, timeout):
    """GET /rubix/v1/dids. Returns (reachable, dids, note)."""
    ok, payload = http_json("GET", "http://{}:{}{}".format(host, port, EP_DIDS), timeout)
    if not ok:
        return False, [], payload
    result = payload.get("result") if isinstance(payload, dict) else None
    dids = result if isinstance(result, list) else []
    return True, dids, ""


def create_and_register_did(host, port, timeout):
    """Create a DID then register it. Returns (did_or_None, note)."""
    base = "http://{}:{}".format(host, port)

    ok, payload = http_json("POST", base + EP_CREATE, timeout, {"password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        return None, "create failed: {}".format(payload if not ok else payload.get("message"))
    did = (payload.get("result") or {}).get("did")
    if not did:
        return None, "create failed: no DID in response"

    register_url = base + EP_REGISTER.format(did=did)
    ok, payload = http_json("POST", register_url, timeout)
    if not ok or not isinstance(payload, dict):
        return did, "created but register(step1) failed: {}".format(payload)
    req_id = (payload.get("result") or {}).get("id")
    if not req_id:
        return did, "created but register(step1) gave no request id: {}".format(payload)

    ok, payload = http_json("POST", base + EP_SIGNATURE, timeout,
                             {"id": req_id, "password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        return did, "created but register(step2/signature) failed: {}".format(
            payload if not ok else payload.get("message"))

    return did, "created and registered"


def sweep(hosts, port, timeout):
    def check(host):
        reachable, dids, note = get_dids(host, port, timeout)
        return {"host": host, "reachable": reachable, "dids": dids, "note": note}

    with ThreadPoolExecutor(max_workers=min(40, len(hosts))) as pool:
        return list(pool.map(check, hosts))


def print_table(results):
    width = max(len(r["host"]) for r in results)
    header = "{:<{w}}  {:<10}  {:>5}  {}".format("HOST", "STATUS", "DIDs", "DID(s) / NOTE", w=width)
    print(header)
    print("-" * len(header))
    for r in results:
        status = "OK" if r["reachable"] else "DOWN"
        detail = r.get("note") or ", ".join(r["dids"])
        print("{:<{w}}  {:<10}  {:>5}  {}".format(r["host"], status, len(r["dids"]), detail, w=width))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--out", default=os.path.join(here, "dids.xlsx"))
    p.add_argument("--dry-run", action="store_true",
                   help="sweep and report only, never create/register")
    args = p.parse_args()

    hosts = FLEET_HOSTS

    print("Sweeping {} host(s) ({}.{}-{}, excluding {}) on port {}...\n".format(
        len(hosts), FLEET_SUBNET, FLEET_START, FLEET_END,
        ", ".join(str(i) for i in sorted(FLEET_EXCLUDE)), args.port))
    results = sweep(hosts, args.port, args.timeout)
    print_table(results)

    todo = [r for r in results if r["reachable"] and len(r["dids"]) == 0]
    ambiguous = [r for r in results if r["reachable"] and len(r["dids"]) > 1]
    if ambiguous:
        print("\n{} host(s) have MORE THAN ONE DID already — left untouched, needs human review:".format(
            len(ambiguous)))
        for r in ambiguous:
            print("  {}: {}".format(r["host"], ", ".join(r["dids"])))

    actions = {}
    if todo and not args.dry_run:
        print("\n{} host(s) have no DID — creating and registering one each...\n".format(len(todo)))
        for r in todo:
            did, note = create_and_register_did(r["host"], args.port, REGISTER_TIMEOUT)
            actions[r["host"]] = note
            print("  {:<15}  {}".format(r["host"], note))
        print("\nRe-checking affected hosts...")
        recheck = sweep([r["host"] for r in todo], args.port, args.timeout)
        by_host = {r["host"]: r for r in recheck}
        for r in results:
            if r["host"] in by_host:
                r["dids"] = by_host[r["host"]]["dids"]
                r["reachable"] = by_host[r["host"]]["reachable"]
    elif todo and args.dry_run:
        print("\n--dry-run: {} host(s) would get a DID created (skipped).".format(len(todo)))

    print("\nFinal state:")
    print_table(results)

    wb = Workbook()
    ws = wb.active
    ws.title = "DIDs"
    ws.append(["Host", "Status", "DID Count", "DIDs", "Action Taken", "Note", "Checked At"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in results:
        note = ""
        if len(r["dids"]) > 1:
            note = "multiple DIDs - needs human review"
        elif not r["reachable"]:
            note = r.get("note", "")
        ws.append([
            r["host"],
            "OK" if r["reachable"] else "DOWN",
            len(r["dids"]),
            ", ".join(r["dids"]),
            actions.get(r["host"], ""),
            note,
            checked_at,
        ])
    for col, width in zip("ABCDEFG", (16, 10, 10, 65, 25, 35, 20)):
        ws.column_dimensions[col].width = width
    wb.save(args.out)
    print("\nSaved to {}".format(args.out))


if __name__ == "__main__":
    main()
