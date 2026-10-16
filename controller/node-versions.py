#!/usr/bin/env python3
"""
node-versions.py - read every node's Rubix build over SSH.

No HTTP API returns this (checked server/ routes in rubixgoplatform - nothing
like /rubix/v1/version exists). The only way to get it is `./rubixgoplatform -v`,
which just prints:
    Rubix Core Version  : 1.0.4
    Current Commit      : ...
    Previous Commit     : ...
and exits immediately -- it does not touch the running node or its port, so
it's safe to run over SSH while the node is live.

Every node has its own copy of the binary (~/Desktop/rubix/rubix-lab/nodes/node<slot>/), so
each one is read: after a deploy they should all match, and the commit is
what proves the build that matters (e.g. the one with the new faucet DIDs in
its minter allowlist) actually landed. One SSH call per machine.

Requires:
    - SSH key auth already set up from the controller to every machine
      (setup-ssh.sh).
    - The `ssh` binary on the controller (stdlib subprocess, no pip installs)
      and openpyxl for the .xlsx.

Usage:
    python3 node-versions.py                         # pool nodes, from hosts.txt
    python3 node-versions.py --include-fixed         # plus the fullnode and faucet nodes
    python3 node-versions.py --host 192.168.1.107    # one machine; repeatable
"""

import argparse
import datetime
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

try:
    from openpyxl import Workbook
except ImportError:
    sys.exit("ERROR: openpyxl is required. Install it with:\n  pip install openpyxl")

import fleet

DEFAULT_USER = "rubix"
DEFAULT_TIMEOUT = 8

VERSION_RE = re.compile(r"Rubix Core Version\s*:\s*(\S+)")
COMMIT_RE = re.compile(r"Current Commit\s*:\s*(\S+)")


def machine_versions(machine, slots, user, timeout):
    """{slot: (version, commit, note)} for one machine, in one SSH call."""
    script = "; ".join(
        'echo "== {s}"; (cd ~/{d}/node{s} && ./rubixgoplatform -v) 2>&1'.format(s=s, d=fleet.NODES_DIR)
        for s in slots)
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",                    # never prompt for a password
        "-o", "StrictHostKeyChecking=accept-new", # first-time host key, no prompt
        "-o", "ConnectTimeout={}".format(timeout),
        "{}@{}".format(user, machine),
        script,
    ]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout + 5 * len(slots))
    except subprocess.TimeoutExpired:
        return dict((s, (None, None, "ssh timeout")) for s in slots)
    except FileNotFoundError:
        sys.exit("ERROR: 'ssh' not found on this machine.")
    if proc.returncode == 255:
        err = (proc.stderr or "").strip().splitlines()
        return dict((s, (None, None, "ssh failed: {}".format(err[-1] if err else "exit 255")))
                    for s in slots)

    out, chunks, current = {}, {}, None
    for line in proc.stdout.splitlines():
        m = re.match(r"== (\d+)$", line)
        if m:
            current = int(m.group(1))
            chunks[current] = []
        elif current is not None:
            chunks[current].append(line)
    for s in slots:
        text = "\n".join(chunks.get(s, []))
        v, c = VERSION_RE.search(text), COMMIT_RE.search(text)
        if v:
            out[s] = (v.group(1), c.group(1)[:8] if c else None, "")
        else:
            out[s] = (None, None, (text.strip().splitlines() or ["no output"])[-1][:80])
    return out


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=fleet.HOSTS_PATH,
                   help="hosts file (default: hosts.txt beside this script)")
    p.add_argument("--user", default=DEFAULT_USER, help="SSH user (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="SSH connect timeout in seconds (default: %(default)s)")
    p.add_argument("--include-fixed", action="store_true",
                   help="also the fullnode and faucet nodes")
    # --host (singular) checks ONE machine - the common case after deploying to
    # a single machine: confirm the binary actually changed there before rolling
    # out. Repeatable. --hosts (plural) is the FILE; both are spelled out so
    # argparse never prefix-matches one to the other.
    p.add_argument("--host", action="append", default=[], metavar="ADDRESS",
                   help="check just this machine (repeatable)")
    p.add_argument("--out", default=os.path.join(here, "versions.xlsx"))
    args = p.parse_args()

    hosts = fleet.load_hosts(args.hosts)
    if args.host:
        nodes = [n for h in args.host for n in fleet.fleet(hosts, host=h)]
    else:
        nodes = fleet.fleet(hosts, include_fixed=args.include_fixed)
    if not nodes:
        sys.exit("No nodes selected (explorer and controller machines run none).")
    by_machine = {}
    for n in nodes:
        by_machine.setdefault(n["machine"], []).append(n["slot"])
    print("Reading {} node(s) on {} machine(s) via SSH as {}...\n".format(
        len(nodes), len(by_machine), args.user))

    with ThreadPoolExecutor(max_workers=min(20, len(by_machine))) as pool:
        found = dict(zip(by_machine, pool.map(
            lambda m: machine_versions(m, by_machine[m], args.user, args.timeout), by_machine)))

    rows = []
    for n in nodes:
        version, commit, note = found[n["machine"]][n["slot"]]
        rows.append({"node": n, "version": version, "commit": commit, "note": note})

    width = max(len(r["node"]["host"]) for r in rows)
    header = "{:<{w}}  {:>4}  {:<10}  {:<9}  {}".format("NODE", "SLOT", "VERSION", "COMMIT", "NOTE", w=width)
    print(header)
    print("-" * len(header))
    for r in rows:
        print("{:<{w}}  {:>4}  {:<10}  {:<9}  {}".format(
            r["node"]["host"], r["node"]["slot"], r["version"] or "-", r["commit"] or "-",
            r["note"], w=width))

    ok = [r for r in rows if r["version"]]
    builds = sorted(set("{} @ {}".format(r["version"], r["commit"] or "?") for r in ok))
    print("\nGot version : {}/{}".format(len(ok), len(rows)))
    if len(builds) == 1:
        print("Uniform     : {}".format(builds[0]))
    elif builds:
        print("MIXED       : " + "; ".join(
            "{} on {} node(s)".format(b, sum(1 for r in ok if "{} @ {}".format(
                r["version"], r["commit"] or "?") == b)) for b in builds))

    wb = Workbook()
    ws = wb.active
    ws.title = "Versions"
    ws.append(["Node", "Machine", "Slot", "Role", "Version", "Commit", "Note", "Checked At"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in rows:
        n = r["node"]
        ws.append([n["host"], n["machine"], n["slot"], n["role"] or "pool",
                   r["version"] or "", r["commit"] or "", r["note"], checked_at])
    for col, w in zip("ABCDEFGH", (22, 16, 6, 14, 12, 10, 40, 20)):
        ws.column_dimensions[col].width = w
    wb.save(args.out)
    print("\nSaved to {}".format(args.out))


if __name__ == "__main__":
    main()
