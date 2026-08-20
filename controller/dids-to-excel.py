#!/usr/bin/env python3
"""
dids-to-excel.py - Sweep an IP range for Rubix nodes and record their DIDs.

Calls GET /rubix/v1/dids on every host in the range (confirmed against
server/did.go APIGetAllDID in the rubixgoplatform repo: returns
{"status": bool, "message": str, "result": [<did>, ...]}), then writes one
row per host to an .xlsx file next to this script.

Requires openpyxl (not stdlib):
    pip install openpyxl

Usage:
    python3 dids-to-excel.py                              # 192.168.1.101-150, port 20000
    python3 dids-to-excel.py --start 101 --end 150 --subnet 192.168.1
    python3 dids-to-excel.py --port 20000 --timeout 5
    python3 dids-to-excel.py --out dids.xlsx
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

DEFAULT_SUBNET = "192.168.1"
DEFAULT_START = 101
DEFAULT_END = 150
DEFAULT_PORT = 20000
DEFAULT_TIMEOUT = 5
EP_DIDS = "/rubix/v1/dids"


def get_dids(host, port, timeout):
    """GET /rubix/v1/dids on one host. Returns (reachable, dids, note)."""
    url = "http://{}:{}{}".format(host, port, EP_DIDS)
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, [], "HTTP {}".format(resp.status)
            payload = json.loads(resp.read().decode("utf-8"))
            result = payload.get("result") if isinstance(payload, dict) else None
            dids = result if isinstance(result, list) else []
            return True, dids, ""
    except urllib.error.HTTPError as e:
        return False, [], "HTTP {}".format(e.code)
    except urllib.error.URLError as e:
        return False, [], "unreachable ({})".format(e.reason)
    except TimeoutError:
        return False, [], "timeout"
    except Exception as e:  # malformed JSON, connection reset, etc.
        return False, [], "{}: {}".format(type(e).__name__, e)


def check_host(host, port, timeout):
    reachable, dids, note = get_dids(host, port, timeout)
    return {
        "host": host,
        "reachable": reachable,
        "did_count": len(dids),
        "dids": dids,
        "note": note,
    }


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subnet", default=DEFAULT_SUBNET,
                   help="first three octets, e.g. 192.168.1 (default: %(default)s)")
    p.add_argument("--start", type=int, default=DEFAULT_START,
                   help="first host octet (default: %(default)s)")
    p.add_argument("--end", type=int, default=DEFAULT_END,
                   help="last host octet, inclusive (default: %(default)s)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help="node API port (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="per-request timeout in seconds (default: %(default)s)")
    p.add_argument("--out", default=os.path.join(here, "dids.xlsx"),
                   help="output .xlsx path (default: dids.xlsx beside this script)")
    args = p.parse_args()

    hosts = ["{}.{}".format(args.subnet, i) for i in range(args.start, args.end + 1)]
    print("Checking {} host(s) ({}.{}-{}) on port {}...\n".format(
        len(hosts), args.subnet, args.start, args.end, args.port))

    with ThreadPoolExecutor(max_workers=min(40, len(hosts))) as pool:
        results = list(pool.map(
            lambda h: check_host(h, args.port, args.timeout), hosts))

    reachable = [r for r in results if r["reachable"]]
    width = max(len(r["host"]) for r in results)
    header = "{:<{w}}  {:<10}  {:>5}  {}".format("HOST", "STATUS", "DIDs", "DID(s) / NOTE", w=width)
    print(header)
    print("-" * len(header))
    for r in results:
        status = "OK" if r["reachable"] else "DOWN"
        detail = ", ".join(r["dids"]) if r["dids"] else r["note"]
        print("{:<{w}}  {:<10}  {:>5}  {}".format(r["host"], status, r["did_count"], detail, w=width))

    print("\nReachable : {}/{}".format(len(reachable), len(results)))

    wb = Workbook()
    ws = wb.active
    ws.title = "DIDs"
    ws.append(["Host", "Status", "DID Count", "DIDs", "Note", "Checked At"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in results:
        ws.append([
            r["host"],
            "OK" if r["reachable"] else "DOWN",
            r["did_count"],
            ", ".join(r["dids"]),
            r["note"],
            checked_at,
        ])
    for col, width in zip("ABCDEF", (16, 10, 10, 65, 25, 20)):
        ws.column_dimensions[col].width = width
    wb.save(args.out)
    print("\nSaved to {}".format(args.out))


if __name__ == "__main__":
    main()
