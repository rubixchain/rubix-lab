#!/usr/bin/env python3
"""
rebuild_token_registry.py - recover the token index high-water mark from the fleet.

WHY THIS EXISTS
    Local RBT is minted with a global index. The node maps that index to a
    token ID:
        GetTokenLevelAndNumberForGlobalIndex(globalIndex) -> (level, numInLevel)
        tokenID = "<level>_<numInLevel>"          (core/token.go:170)
    Two nodes minting the same index produce the same token ID, and a shared
    quorum then cannot tell the tokens apart. token_index_registry.json is the
    fleet-wide record of which indices have been handed out.

    That file is gitignored and lives beside this script, so it does NOT
    survive a repo move. When the layout changed, the registry was left at the
    old path, allocation silently restarted at 10,000,000, and every quorum
    mint died with:
        PersistGenesisTokenRecord: token "10004_971286" already exists

    CLAUDE.md said the high-water mark could not be reconstructed from the
    network. That is not true: every minted token is still in each node's
    `tokens` table, and the ID mapping is invertible. This script inverts it.

WHAT IT DOES
    Reads every node's tokens table, takes each local-RBT token ID, converts
    "<level>_<num>" back to a global index, and reports the highest found
    fleet-wide. With --write it stores that plus a safety margin as the next
    index to allocate.

    READ-ONLY against the databases. It writes exactly one local file, and only
    when --write is passed.

USAGE
    python3 rebuild_token_registry.py                 # inspect, change nothing
    python3 rebuild_token_registry.py --write         # write the registry
    python3 rebuild_token_registry.py --write --margin 100000
    python3 rebuild_token_registry.py --hosts ../../hosts.txt

WHEN TO RUN IT
    * after moving or re-cloning the repo
    * after a fleet wipe (the wipe clears tokens, so the mark legitimately drops)
    * whenever a mint fails with "already exists"
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import db_client as db
import rubix_client as rc

# token/token_mapping.go - TokenMap. Level capacities, mapLevel -> count.
# Copied verbatim; a mismatch here silently produces wrong global indices, so
# it is checked against the known anchor below rather than trusted.
TOKEN_MAP = {
    1: 4300000, 2: 2425000, 3: 2303750, 4: 2188563, 5: 2079134, 6: 1975178, 7: 1876419, 8: 1782598,
    9: 1693468, 10: 1608795, 11: 1528355, 12: 1451937, 13: 1379340, 14: 1310373, 15: 1244855, 16: 1182612,
    17: 1123481, 18: 1067307, 19: 1013942, 20: 963245, 21: 915082, 22: 869328, 23: 825862, 24: 784569,
    25: 745340, 26: 708073, 27: 672670, 28: 639036, 29: 607084, 30: 576730, 31: 547894, 32: 520499,
    33: 494474, 34: 469750, 35: 446263, 36: 423950, 37: 402752, 38: 382615, 39: 363484, 40: 345310,
    41: 328044, 42: 311642, 43: 296060, 44: 281257, 45: 267194, 46: 253834, 47: 241143, 48: 229085,
    49: 217631, 50: 206750, 51: 196412, 52: 186592, 53: 177262, 54: 168399, 55: 159979, 56: 151980,
    57: 144381, 58: 137162, 59: 130304, 60: 117273, 61: 105546, 62: 94992, 63: 85492, 64: 76943,
    65: 69249, 66: 62324, 67: 56092, 68: 50482, 69: 45434, 70: 40891, 71: 36802, 72: 33121,
    73: 29809, 74: 26828, 75: 24146, 76: 21731, 77: 19558, 78: 17602,
}

LEVEL_OFFSET = 10000          # constants.LocalRBT_Level_Offset
TOTAL_CAPACITY = sum(TOKEN_MAP.values())

# Cumulative count before each map level, so a token ID converts back in O(1).
_CUMULATIVE = {}
_c = 0
for _lvl in sorted(TOKEN_MAP):
    _CUMULATIVE[_lvl] = _c
    _c += TOKEN_MAP[_lvl]

TOKEN_ID_RE = re.compile(r"^(\d+)_(\d+)$")


def global_index(token_id):
    """Invert "<level>_<num>" back to the global index it was minted from.

    Returns None for anything that is not a local-RBT id - mainnet/testnet
    tokens use the same shape with a different level offset, and counting one
    of those as a local index would push the mark absurdly high.
    """
    m = TOKEN_ID_RE.match(token_id or "")
    if not m:
        return None
    level, num = int(m.group(1)), int(m.group(2))
    map_level = level - LEVEL_OFFSET
    if map_level not in TOKEN_MAP:
        return None
    if num < 1 or num > TOKEN_MAP[map_level]:
        return None
    return _CUMULATIVE[map_level] + num


def _self_check():
    """The mapping is load-bearing; verify it against a known anchor.

    Global 10,000,000 must map to 10004_971250 - confirmed against a live node
    during the incident this script was written for.
    """
    idx = global_index("10004_971250")
    if idx != 10_000_000:
        sys.exit("ERROR: TOKEN_MAP self-check failed - 10004_971250 mapped to {}, "
                 "expected 10,000,000. The map is out of sync with the product; "
                 "re-copy it from token/token_mapping.go.".format(idx))


def _scan_table(host, port, sql, params):
    """Run one id-listing query. Returns (max_index, count, skipped) or None if
    the table is absent."""
    try:
        rows = db.query(host, sql, params, port=port)
    except db.DBUnavailable:
        raise
    except Exception as e:
        # UndefinedTable and friends: Postgres is up but this table is not
        # there. Normal on a host where no node has ever run, and on pool nodes
        # for the fullnode_* tables.
        if "does not exist" in str(e).lower():
            return None
        raise

    best, seen, skipped = 0, 0, 0
    for (tid,) in rows:
        gi = global_index(tid)
        if gi is None:
            skipped += 1
            continue
        seen += 1
        best = max(best, gi)
    return best, seen, skipped


def scan_host(host, port):
    """Highest local-RBT global index on one node. Returns (max_index, note).

    max_index is None only when the host could not be scanned at all. A host
    with Postgres up but no Rubix schema returns 0 with a note - that is a
    normal fleet state (the explorer runs Postgres but has never run a node),
    not a failure worth aborting the sweep for.

    Also reads fullnode_rbt where it exists. On the fullnode that table mirrors
    tokens minted across the whole fleet, which makes it a useful backstop: an
    index held only by a node that is currently down is still counted here.
    """
    try:
        own = _scan_table(host, port,
                          "SELECT token_id FROM tokens WHERE token_type = %s",
                          (db.TYPE_RBT,))
    except db.DBUnavailable as e:
        return None, str(e)

    try:
        observed = _scan_table(host, port, "SELECT token_id FROM fullnode_rbt", ())
    except db.DBUnavailable as e:
        return None, str(e)
    except Exception:
        observed = None

    if own is None and observed is None:
        return 0, "no rubix schema (Postgres up, no node has run here)"

    best, parts = 0, []
    if own is not None:
        b, seen, skipped = own
        best = max(best, b)
        parts.append("{} local-RBT tokens".format(seen))
        if skipped:
            parts.append("{} non-local ids ignored".format(skipped))
    if observed is not None:
        b, seen, _ = observed
        if seen:
            best = max(best, b)
            parts.append("{} observed via fullnode_rbt".format(seen))

    return best, ", ".join(parts) or "no tokens"


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=os.path.join(HERE, "..", "..", "hosts.txt"))
    p.add_argument("--port", type=int, default=db.DB_PORT)
    p.add_argument("--margin", type=int, default=50_000,
                   help="gap left above the highest index found (default 50000). "
                        "Covers tokens minted by a run whose client timed out while "
                        "the server kept going.")
    p.add_argument("--write", action="store_true",
                   help="write token_index_registry.json (otherwise only reports)")
    args = p.parse_args()

    _self_check()

    if not db.available():
        sys.exit("ERROR: psycopg2 is not installed, so the fleet cannot be scanned.\n"
                 "       sudo apt install -y python3-psycopg2")

    hosts = rc.load_hosts(os.path.abspath(args.hosts))
    # Every host is scanned, fixed-role ones included: the fullnode holds tokens
    # too, and an index it has seen must never be handed out again.
    print("Scanning {} host(s) for the highest local-RBT index...\n".format(len(hosts)))

    overall, reachable, unreachable, no_schema = 0, 0, [], []
    for h in hosts:
        host = h["host"]
        best, note = scan_host(host, args.port)
        if best is None:
            unreachable.append(host)
            print("  {:<16} UNREACHABLE  {}".format(host, note.split("(")[0].strip()))
            continue
        if "no rubix schema" in note:
            no_schema.append(host)
            print("  {:<16} {}".format(host, note))
            continue
        reachable += 1
        overall = max(overall, best)
        print("  {:<16} max index {:>12,}   {}".format(host, best, note))

    print()
    if not reachable:
        sys.exit("ERROR: no host with a Rubix schema could be scanned - nothing to "
                 "rebuild from. Check the nodes are up and hold tokens before writing "
                 "a registry, or the counter would be set from no evidence at all.")
    if no_schema:
        print("Skipped (Postgres up, no node has ever run there): {}\n".format(
            ", ".join(no_schema)))

    if unreachable:
        print("WARNING: {} host(s) could not be scanned: {}".format(
            len(unreachable), ", ".join(unreachable)))
        print("         A token minted only on an unscanned node would not be counted,")
        print("         so its index could be re-issued. Re-run once they are up, or")
        print("         raise --margin well above the largest mint you have done.\n")

    next_index = max(overall + args.margin, rc.TOKEN_INDEX_SEED)
    print("Highest index found fleet-wide : {:>12,}".format(overall))
    print("Safety margin                  : {:>12,}".format(args.margin))
    print("Next index to allocate         : {:>12,}".format(next_index))
    print("Remaining capacity             : {:>12,}".format(TOTAL_CAPACITY - next_index))

    if next_index >= TOTAL_CAPACITY:
        sys.exit("\nERROR: next index is beyond the total token map capacity ({:,}). "
                 "The fleet needs a wipe.".format(TOTAL_CAPACITY))

    path = rc.TOKEN_INDEX_REGISTRY_PATH
    if not args.write:
        print("\nDry run - nothing written. Re-run with --write to store this at:")
        print("  {}".format(path))
        return

    previous = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                previous = json.load(fh).get("next_index")
        except Exception:
            previous = "unreadable"
    # Never move the counter BACKWARDS. A node that is down at scan time may
    # hold higher indices than anything seen here, and lowering the mark would
    # re-issue them.
    if isinstance(previous, int) and previous > next_index:
        print("\nExisting registry already at {:,}, which is higher than the {:,} "
              "just computed.".format(previous, next_index))
        print("Keeping the higher value - lowering it could re-issue indices held by "
              "a node that was down during this scan.")
        next_index = previous

    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "next_index": next_index,
            "rebuilt_from_fleet_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "highest_index_found": overall,
            "margin": args.margin,
            "hosts_scanned": reachable,
            "hosts_unreachable": unreachable,
        }, fh, indent=2)

    print("\nWritten: {}".format(path))
    if previous is not None:
        print("  previous next_index: {}".format(previous))
    print("  new next_index     : {:,}".format(next_index))
    print("\nMinting can resume. This file is gitignored and does NOT survive a repo")
    print("move or re-clone - re-run this script when either happens.")


if __name__ == "__main__":
    main()
