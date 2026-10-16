#!/usr/bin/env python3
"""
fleet.py - which nodes run where. The one parser for controller/hosts.txt and
systems/node-layout.txt: the Python tools import it, the shell tools read its
command-line output, so every tool agrees on the fleet.

A machine is not a node any more. A pool machine runs one node per line of
systems/node-layout.txt, so the tools address a NODE as "ip:api_port". Its
other ports follow from its node_index, the way the product derives them
(core/config/config.go:141-152):

    API       20000 + node_index
    swarm      4002 + node_index
    Postgres   5433 + node_index      (setup.sh writes it into config.toml)

Roles in hosts.txt, one per machine:
    (none)         pool machine - every node in systems/node-layout.txt
    fullnode       one node (slot 0), run with -fullnode, holds no DID
    faucet         one node (slot 0) holding the faucet DID
    faucet-quorum  one node (slot 0) holding the faucet quorum DID
    explorer       no node
    controller     no node

On a machine, slot N lives in ~/Desktop/rubix/rubix-lab/nodes/node<N>/ (the
lab folder sits where the controller keeps its repo) and runs as systemd
unit rubixgoplatform@<N>, with Postgres container rubix-db-<N> (volume
rubix-pgdata-<N>). systems/install/setup.sh builds exactly that.

Command line, for the shell tools (one line each, space separated):
    python3 fleet.py nodes [--include-fixed] [--host IP]
        -> ip slot node_index api_port db_port swarm_port dids role
    python3 fleet.py hosts [--include-fixed] [--with-nodes] [--host IP]
        -> ip role
Pool machines only unless --include-fixed. --host picks one machine whatever
its role. `hosts --with-nodes` leaves out machines that run no node. A blank
role is printed as "pool".
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOSTS_PATH = os.path.join(HERE, "hosts.txt")
LAYOUT_PATH = os.path.join(HERE, "..", "systems", "node-layout.txt")

API_BASE = 20000        # constants.RubixServerPort
SWARM_BASE = 4002       # constants.SwarmPort
DB_BASE = 5433          # constants.PostgresBasePort

# Where the lab folder and its nodes live on every machine, relative to its
# home directory (until 2026-10-16 it was ~/rubix-lab - move-nodes-to-desktop.sh).
LAB_DIR = "Desktop/rubix/rubix-lab"
NODES_DIR = LAB_DIR + "/nodes"

# Roles that run one node at slot 0, node_index 0 -> how many DIDs it holds.
SINGLE_NODE_ROLES = {"fullnode": 0, "faucet": 1, "faucet-quorum": 1}
NO_NODE_ROLES = ("explorer", "controller")
FIXED_ROLES = set(SINGLE_NODE_ROLES) | set(NO_NODE_ROLES)
ROLES = ("",) + tuple(sorted(FIXED_ROLES))


def load_hosts(path=HOSTS_PATH):
    """[{"host": ip, "role": role}] in file order. One machine per line,
    `<ip> [role]`, '#' starts a comment. An unknown role stops the tool: a typo
    would otherwise quietly turn a fullnode into a pool machine."""
    if not os.path.exists(path):
        sys.exit("ERROR: hosts file not found: {}\n"
                 "Copy hosts.txt.example to hosts.txt and fill in real IPs first.".format(path))
    hosts, seen = [], set()
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            role = parts[1] if len(parts) > 1 else ""
            if role == "multidid":
                sys.exit("ERROR: {}:{} tags {} 'multidid'. That tag is gone - "
                         "systems/node-layout.txt sets how many DIDs every node "
                         "holds. Remove the tag.".format(path, n, parts[0]))
            if len(parts) > 2 or role not in ROLES:
                sys.exit("ERROR: {}:{}: '{}' - expected '<ip> [role]' with role one "
                         "of: {}".format(path, n, line, ", ".join(r for r in ROLES if r)))
            if parts[0] in seen:
                sys.exit("ERROR: {}:{}: {} is listed twice".format(path, n, parts[0]))
            seen.add(parts[0])
            hosts.append({"host": parts[0], "role": role})
    if not hosts:
        sys.exit("ERROR: no hosts listed in {}".format(path))
    return hosts


def load_layout(path=LAYOUT_PATH):
    """[{"slot", "index", "did_target"}] from node-layout.txt, checked: whole
    numbers, slots and node_indexes all different, every API port in range."""
    if not os.path.exists(path):
        sys.exit("ERROR: node layout not found: {}".format(path))
    layout = []
    with open(path, encoding="utf-8") as fh:
        for n, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                slot, index, dids = (int(x) for x in line.split())
            except ValueError:
                sys.exit("ERROR: {}:{}: '{}' - expected 'slot node_index dids', three "
                         "whole numbers".format(path, n, line))
            if min(slot, index, dids) < 0 or API_BASE + index > 65535:
                sys.exit("ERROR: {}:{}: '{}' - numbers must be 0 or more and "
                         "20000 + node_index at most 65535".format(path, n, line))
            layout.append({"slot": slot, "index": index, "did_target": dids})
    if not layout:
        sys.exit("ERROR: {} lists no nodes".format(path))
    for key in ("slot", "index"):
        values = [e[key] for e in layout]
        if len(set(values)) != len(values):
            sys.exit("ERROR: {}: two lines share a {}".format(
                path, "slot" if key == "slot" else "node_index"))
    return layout


def _node(machine, role, slot, index, did_target):
    api = API_BASE + index
    return {
        "machine": machine, "role": role, "slot": slot, "index": index,
        "did_target": did_target,
        "host": "{}:{}".format(machine, api),
        "api_port": api, "db_port": DB_BASE + index, "swarm_port": SWARM_BASE + index,
        "dir": "{}/node{}".format(NODES_DIR, slot),
        "unit": "rubixgoplatform@{}".format(slot),
        "container": "rubix-db-{}".format(slot),
        "volume": "rubix-pgdata-{}".format(slot),
    }


def nodes_of(entry, layout):
    """The nodes one machine runs, from its hosts.txt entry."""
    role = entry["role"]
    if role in NO_NODE_ROLES:
        return []
    if role in SINGLE_NODE_ROLES:
        return [_node(entry["host"], role, 0, 0, SINGLE_NODE_ROLES[role])]
    return [_node(entry["host"], role, e["slot"], e["index"], e["did_target"])
            for e in layout]


def select(hosts, include_fixed=False, host=None):
    """The machines a tool works on: the pool, plus the fixed-role machines with
    include_fixed. `host` picks one machine, whatever its role - it must be in
    hosts.txt, since its role decides which nodes it runs."""
    if host:
        chosen = [h for h in hosts if h["host"] == host]
        if not chosen:
            sys.exit("ERROR: {} is not in hosts.txt (or is commented out). Add it - "
                     "its role there decides which nodes it runs.".format(host))
        return chosen
    return [h for h in hosts if include_fixed or h["role"] not in FIXED_ROLES]


def fleet(hosts=None, layout=None, include_fixed=False, host=None):
    """Every node on the selected machines, in hosts.txt order then slot order."""
    hosts = load_hosts() if hosts is None else hosts
    layout = load_layout() if layout is None else layout
    out = []
    for h in select(hosts, include_fixed, host):
        out.extend(nodes_of(h, layout))
    return out


def role_hosts(hosts, role):
    """IPs of the machines with this role, in hosts.txt order."""
    return [h["host"] for h in hosts if h["role"] == role]


def split(address, default_port=API_BASE):
    """(ip, api_port) from a node address "ip:port", or from a bare ip."""
    address = str(address)
    if ":" in address:
        ip, port = address.rsplit(":", 1)
        return ip, int(port)
    return address, default_port


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("what", choices=("nodes", "hosts"))
    p.add_argument("--include-fixed", action="store_true",
                   help="also the fullnode, faucet, explorer and controller machines")
    p.add_argument("--with-nodes", action="store_true",
                   help="hosts: leave out machines that run no node")
    # --host (one ADDRESS) and --hosts (a FILE) are both spelled out so argparse
    # never takes one for an abbreviation of the other.
    p.add_argument("--host", default="", metavar="ADDRESS", help="just this machine")
    p.add_argument("--hosts", default=HOSTS_PATH, metavar="FILE")
    p.add_argument("--layout", default=LAYOUT_PATH, metavar="FILE")
    args = p.parse_args()

    hosts = load_hosts(args.hosts)
    if args.what == "hosts":
        for h in select(hosts, args.include_fixed, args.host or None):
            if args.with_nodes and h["role"] in NO_NODE_ROLES:
                continue
            print("{} {}".format(h["host"], h["role"] or "pool"))
        return
    for n in fleet(hosts, load_layout(args.layout), args.include_fixed, args.host or None):
        print("{machine} {slot} {index} {api_port} {db_port} {swarm_port} {did_target} {r}".format(
            r=n["role"] or "pool", **n))


if __name__ == "__main__":
    main()
