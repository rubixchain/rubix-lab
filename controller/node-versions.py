#!/usr/bin/env python3
"""
node-versions.py - SSH into an IP range and read each node's Rubix version.

No HTTP API returns this (checked server/ routes in rubixgoplatform - nothing
like /rubix/v1/version exists). The only way to get it is `./rubixgoplatform -v`,
which just prints:
    Rubix Core Version  : 1.0.4
    Current Commit      : ...
    Previous Commit     : ...
and exits immediately -- it does not touch the running node or its port, so
it's safe to run over SSH while the systemd service is live.

Requires:
    - SSH key auth already set up from the controller to every target
      (same one-time step exec-update/README.md describes: ssh-copy-id).
    - The `ssh` binary on the controller (stdlib subprocess, no pip installs).

Reads the fleet's host list from hosts.txt (same file check-nodes.py and
setup-ssh.sh use) — one source of truth for who's in the fleet. Comments
and the optional role column are ignored here; only the host column matters.

Usage:
    python3 node-versions.py                              # reads hosts.txt
    python3 node-versions.py --user rubix --remote-dir '~/Desktop/rubix'
    python3 node-versions.py --hosts other-hosts.txt
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

DEFAULT_USER = "rubix"
DEFAULT_REMOTE_DIR = "~/Desktop/rubix"
DEFAULT_TIMEOUT = 8

VERSION_RE = re.compile(r"Rubix Core Version\s*:\s*(\S+)")


def get_version(host, user, remote_dir, timeout):
    """SSH in, run `./rubixgoplatform -v`, parse the version line."""
    remote_cmd = "cd {} && ./rubixgoplatform -v".format(remote_dir)
    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",                    # never prompt for a password
        "-o", "StrictHostKeyChecking=accept-new", # first-time host key, no prompt
        "-o", "ConnectTimeout={}".format(timeout),
        "{}@{}".format(user, host),
        remote_cmd,
    ]
    try:
        proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None, "ssh timeout"
    except FileNotFoundError:
        sys.exit("ERROR: 'ssh' not found on this machine.")

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return None, "ssh failed: {}".format(err[-1] if err else "exit {}".format(proc.returncode))

    m = VERSION_RE.search(proc.stdout)
    if not m:
        return None, "version line not found in output"
    return m.group(1), ""


def check_host(host, user, remote_dir, timeout):
    version, note = get_version(host, user, remote_dir, timeout)
    return {"host": host, "version": version, "note": note}


def load_hosts(path):
    """Read the hosts file. Format per line:  <host> [role]  ('#' comments ok)."""
    if not os.path.exists(path):
        sys.exit("ERROR: hosts file not found: {}\n"
                 "Copy hosts.txt.example to hosts.txt and fill in real IPs first.".format(path))
    hosts = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            hosts.append(line.split()[0])
    if not hosts:
        sys.exit("ERROR: no hosts listed in {}".format(path))
    return hosts


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hosts", default=os.path.join(here, "hosts.txt"),
                   help="hosts file (default: hosts.txt beside this script)")
    p.add_argument("--user", default=DEFAULT_USER, help="SSH user (default: %(default)s)")
    p.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                   help="directory on each host containing the rubixgoplatform binary (default: %(default)s)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                   help="SSH connect timeout in seconds (default: %(default)s)")
    p.add_argument("--out", default=os.path.join(here, "versions.xlsx"))
    args = p.parse_args()

    hosts = load_hosts(args.hosts)
    print("Checking {} host(s) from {} via SSH as {}...\n".format(
        len(hosts), args.hosts, args.user))

    with ThreadPoolExecutor(max_workers=min(20, len(hosts))) as pool:
        results = list(pool.map(
            lambda h: check_host(h, args.user, args.remote_dir, args.timeout), hosts))

    width = max(len(r["host"]) for r in results)
    header = "{:<{w}}  {:<12}  {}".format("HOST", "VERSION", "NOTE", w=width)
    print(header)
    print("-" * len(header))
    for r in results:
        print("{:<{w}}  {:<12}  {}".format(r["host"], r["version"] or "-", r["note"], w=width))

    ok = [r for r in results if r["version"]]
    print("\nGot version : {}/{}".format(len(ok), len(results)))

    wb = Workbook()
    ws = wb.active
    ws.title = "Versions"
    ws.append(["Host", "Version", "Note", "Checked At"])
    checked_at = datetime.datetime.now().isoformat(timespec="seconds")
    for r in results:
        ws.append([r["host"], r["version"] or "", r["note"], checked_at])
    for col, w in zip("ABCD", (16, 12, 35, 20)):
        ws.column_dimensions[col].width = w
    wb.save(args.out)
    print("\nSaved to {}".format(args.out))


if __name__ == "__main__":
    main()
