#!/usr/bin/env python3
"""
fleet_health.py - what the machines and the nodes on them were doing, read over
SSH. Read-only.

Two uses:

  deaf_nodes()  before a run: which nodes cannot RECEIVE. A node's IPFS carries
                peers' requests to it through `p2p listen /x/<peerID>RubixCore/
                1.0`, which the IPFS auto-restart never registers again
                (core/ipfs_recovery.go restartIPFS / reinitializeIPFS). Such a
                node still answers its API and still sends, so nothing else
                shows it: on 2026-10-22 eight of .130's nine nodes were deaf and
                four cases failed on them. The runner leaves them out.

  clock_skew()  before a run: which machines' clocks are off the rest of the
                fleet's. A quorum refuses a transaction stamped later than its
                own clock (core/consensus/checks.go:62, whole seconds, no
                tolerance), so a sender whose machine runs ahead of its
                quorum's is refused for the lab's clocks. The runner leaves
                such machines out, so an "invalid epoch" refusal it reports is
                Rubix's own, at offsets within CLOCK_TOL.

  Watch         during a case step (master/steps.py): CPU and iowait, memory,
                swap, disk and load on every machine the step runs on, the node
                processes' memory, each node's MainPID (a change = the node
                restarted), and each node's p2p listener at the start and the
                end. A step that fails while one of its machines was at a
                ceiling is reported as a limit of the MACHINE, not of Rubix.

One SSH round trip per machine per sample; the remote script only reads /proc,
ps, df, systemctl and `ipfs p2p ls`.
"""

import os
import statistics
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SSH_USER = "rubix"
PERIOD = 3                  # seconds between samples while a step runs
# Seconds a machine's clock may differ from the fleet's median. Two machines
# within it differ by at most twice it, less than the time from a sender
# stamping a transaction to its quorum checking it (~0.1s on 2026-10-29).
CLOCK_TOL = 0.05

# A step that fails while a machine it runs on crosses one of these is reported
# as HARDWARE. Chosen so that a machine merely busy does not count: Rubix must
# hold up under load; it is only excused when the machine itself ran out.
CPU_BUSY = 95               # % busy over one sampling interval
IOWAIT = 30                 # % of CPU time waiting on disk
MEM_AVAIL = 5               # % of memory still available
SWAP_GROWTH_MB = 200        # swap filled during the step
DISK_FREE_MB = 1024         # free space where the nodes live

REMOTE = r'''
ears="$1"; shift
read -r _ u n s i w q sq st _rest < /proc/stat
echo "cpu=$u,$n,$s,$i,$w,$q,$sq,$st"
awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {sub(":", "", $1); print $1 "=" $2}' /proc/meminfo
read -r l1 _rest < /proc/loadavg; echo "load=$l1"
echo "disk=$(df -Pk ~/Desktop/rubix 2>/dev/null | awk 'NR == 2 {print $4}')"
echo "ntp=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)"
ps -eo comm=,rss= 2>/dev/null | awk '$1 ~ /^rubixgoplatfor/ {r += $2} $1 == "ipfs" {i += $2} $1 == "postgres" {p += $2}
  END {print "rss.node=" r + 0; print "rss.ipfs=" i + 0; print "rss.postgres=" p + 0}'
while [ $# -ge 2 ]; do
  s="$1"; idx="$2"; shift 2
  echo "pid.$s=$(systemctl show -p MainPID --value "rubixgoplatform@$s" 2>/dev/null)"
  if [ "$ears" = 1 ]; then
    d=~/Desktop/rubix/rubix-lab/nodes/node$s
    L=$(( ((22000 + 1000 * idx) % 65536 + 10) % 65536 ))
    echo "ear.$s=$(IPFS_PATH=$d/.ipfs "$d/ipfs" p2p ls 2>/dev/null | awk -v t="/ip4/127.0.0.1/tcp/$L" '$2 ~ "^/p2p/" && $3 == t' | wc -l)"
  fi
done
'''


def _roles():
    """{machine: role} from hosts.txt, or {} where there is none (offline)."""
    import fleet as fleet_inventory
    try:
        return dict((h["host"], h["role"]) for h in fleet_inventory.load_hosts())
    except SystemExit:
        return {}


def machines_of(hosts):
    """{machine: [(slot, node_index), ...]} - EVERY node on each machine the hosts
    are on, not only the hosts: a node that kills its neighbours' IPFS
    (core/shutdown_manager.go) shows on the neighbours. A single-node role
    (faucet, fullnode) runs slot 0, node_index 0 only."""
    import fleet as fleet_inventory
    layout = [(e["slot"], e["index"]) for e in fleet_inventory.load_layout()]
    roles = _roles()
    out = {}
    for h in hosts:
        machine = fleet_inventory.split(h)[0]
        if machine not in out:
            single = roles.get(machine, "") in fleet_inventory.SINGLE_NODE_ROLES
            out[machine] = [(0, 0)] if single else list(layout)
    return out


def read_machine(machine, slots, ears=False, timeout=30):
    """One sample of a machine: {key: value} (strings), or None if it could not
    be read. Keys: cpu, MemTotal, MemAvailable, SwapTotal, SwapFree, load,
    disk, ntp, rss.node/ipfs/postgres, pid.<slot>, ear.<slot> (with ears)."""
    args = ["1" if ears else "0"]
    for slot, index in slots:
        args += [str(slot), str(index)]
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                            "{}@{}".format(SSH_USER, machine), "bash", "-s", "--"] + args,
                           input=REMOTE, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    out = {"at": time.time()}
    for line in (p.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out if "cpu" in out else None


def read_controller():
    """The same sample for the controller itself, when it runs Linux: a run that
    saturates the controller times the controller, not the fleet."""
    if not os.path.exists("/proc/stat"):
        return None
    out = {"at": time.time()}
    try:
        with open("/proc/stat") as fh:
            f = fh.readline().split()
        out["cpu"] = ",".join(f[1:9])
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    out[k] = v.split()[0]
        out["load"] = open("/proc/loadavg").read().split()[0]
    except Exception:
        return None
    return out


def _num(s, default=None):
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _cpu(a, b):
    """(busy %, iowait %) between two samples' cpu lines."""
    try:
        x = [float(v) for v in a["cpu"].split(",")]
        y = [float(v) for v in b["cpu"].split(",")]
    except Exception:
        return None, None
    d = [q - p for p, q in zip(x, y)]
    total = sum(d)
    if total <= 0:
        return None, None
    idle, iowait = d[3], d[4]
    return round(100.0 * (total - idle - iowait) / total, 1), round(100.0 * iowait / total, 1)


def summarize(samples, slots):
    """What one machine did over a step: peaks, ceilings crossed, nodes that
    restarted, nodes whose p2p listener came or went."""
    samples = [s for s in samples if s]
    out = {"samples": len(samples), "ceilings": [], "restarted": [], "deafened": [],
           "deaf_before": []}
    if not samples:
        return out
    busy, wait = [], []
    for a, b in zip(samples, samples[1:]):
        c, w = _cpu(a, b)
        if c is not None:
            busy.append(c)
            wait.append(w)
    total = _num(samples[0].get("MemTotal"), 0) or 0
    avail = [_num(s.get("MemAvailable")) for s in samples if _num(s.get("MemAvailable")) is not None]
    swap_used = [(_num(s.get("SwapTotal"), 0) - _num(s.get("SwapFree"), 0)) for s in samples]
    disk = [_num(s.get("disk")) for s in samples if _num(s.get("disk")) is not None]
    out.update({
        "cpu_peak": max(busy) if busy else None,
        "iowait_peak": max(wait) if wait else None,
        "mem_avail_min_pct": round(100.0 * min(avail) / total, 1) if avail and total else None,
        "swap_growth_mb": round((max(swap_used) - swap_used[0]) / 1024.0, 1) if swap_used else None,
        "disk_free_min_mb": round(min(disk) / 1024.0) if disk else None,
        "load_peak": max(_num(s.get("load"), 0) for s in samples),
        "rss_mb": dict((k, round(max(_num(s.get("rss." + k), 0) for s in samples) / 1024.0))
                       for k in ("node", "ipfs", "postgres")),
        "ntp": samples[-1].get("ntp", ""),
    })
    if out["cpu_peak"] is not None and out["cpu_peak"] >= CPU_BUSY:
        out["ceilings"].append("CPU {:.0f}% busy".format(out["cpu_peak"]))
    if out["iowait_peak"] is not None and out["iowait_peak"] >= IOWAIT:
        out["ceilings"].append("{:.0f}% of CPU time waiting on disk".format(out["iowait_peak"]))
    if out["mem_avail_min_pct"] is not None and out["mem_avail_min_pct"] <= MEM_AVAIL:
        out["ceilings"].append("only {:.1f}% of memory available".format(out["mem_avail_min_pct"]))
    if out["swap_growth_mb"] is not None and out["swap_growth_mb"] >= SWAP_GROWTH_MB:
        out["ceilings"].append("{:.0f} MB pushed to swap".format(out["swap_growth_mb"]))
    if out["disk_free_min_mb"] is not None and out["disk_free_min_mb"] <= DISK_FREE_MB:
        out["ceilings"].append("{} MB of disk left".format(out["disk_free_min_mb"]))
    for slot, _index in slots:
        pids = [s.get("pid.{}".format(slot)) for s in samples]
        seen = [p for p in pids if p not in (None, "", "0")]
        if len(set(seen)) > 1 or (seen and any(p in ("", "0") for p in pids)):
            out["restarted"].append(slot)
        ears = [s.get("ear.{}".format(slot)) for s in samples if "ear.{}".format(slot) in s]
        if ears:
            if ears[0] == "0":
                out["deaf_before"].append(slot)
            elif ears[-1] == "0":
                out["deafened"].append(slot)
    return out


class Watch(object):
    """Samples machines in the background between start() and stop()."""

    def __init__(self, hosts, period=PERIOD):
        self.slots = machines_of(hosts)
        self.period = period
        self.samples = dict((m, []) for m in self.slots)
        self.controller = []
        self._stop = threading.Event()
        self._thread = None

    def _round(self, ears=False):
        machines = list(self.slots)
        if machines:
            with ThreadPoolExecutor(max_workers=min(32, len(machines))) as ex:
                for m, s in zip(machines, ex.map(
                        lambda m: read_machine(m, self.slots[m], ears), machines)):
                    if s:
                        self.samples[m].append(s)
        c = read_controller()
        if c:
            self.controller.append(c)

    def start(self):
        self._round(ears=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        while not self._stop.wait(self.period):
            self._round()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=60)
        self._round(ears=True)
        out = dict((m, summarize(self.samples[m], self.slots[m])) for m in self.slots)
        if self.controller:
            out["controller"] = summarize(self.controller, [])
        return out


def clock_offset(machine, samples=4):
    """Seconds this machine's clock is ahead of the controller's (negative:
    behind), or None if it cannot be read. The samples run over one shared SSH
    connection (multiplexed), so each round trip is a few ms; the offset is the
    remote time minus the midpoint of the local times around it, taken from the
    sample with the shortest round trip, as NTP does."""
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlMaster=auto",
            "-o", "ControlPath=" + os.path.join(tempfile.gettempdir(), "rubix-clock-%r@%h:%p"),
            "-o", "ControlPersist=15"]
    target = "{}@{}".format(SSH_USER, machine)
    best = None
    try:
        subprocess.run(["ssh"] + opts + [target, "true"], capture_output=True, timeout=20)
        for _ in range(samples):
            t0 = time.time()
            p = subprocess.run(["ssh"] + opts + [target, "date +%s.%N"],
                               capture_output=True, text=True, timeout=10)
            t1 = time.time()
            remote = float((p.stdout or "").strip())
            if best is None or t1 - t0 < best[1]:
                best = (remote - (t0 + t1) / 2.0, t1 - t0)
    except Exception:
        pass
    return best[0] if best else None


def clock_skew(machines):
    """({machine: seconds off} for the machines more than CLOCK_TOL off,
    {machine: seconds off} for every machine read). Off what: the median clock
    of the machines read, not the controller's - what refuses a transfer is a
    sender's clock ahead of its quorum's, so only the machines against each
    other matter. A machine that cannot be read is in neither."""
    machines = sorted(set(m for m in machines if m))
    if not machines:
        return {}, {}
    with ThreadPoolExecutor(max_workers=min(32, len(machines))) as ex:
        raw = dict((m, o) for m, o in zip(machines, ex.map(clock_offset, machines))
                   if o is not None)
    if not raw:
        return {}, {}
    mid = statistics.median(raw.values())
    rel = dict((m, o - mid) for m, o in raw.items())
    return dict((m, d) for m, d in rel.items() if abs(d) > CLOCK_TOL), rel


def deaf_nodes(hosts):
    """({host: why} for nodes that cannot receive, [machines whose clock is not
    NTP-synchronised]). A machine that cannot be read says nothing - its nodes
    are not called deaf on no evidence."""
    slots = machines_of(hosts)
    wanted = set(hosts)
    deaf, clocks = {}, []
    if not slots:
        return deaf, clocks
    with ThreadPoolExecutor(max_workers=min(32, len(slots))) as ex:
        readings = dict(zip(slots, ex.map(lambda m: read_machine(m, slots[m], ears=True), slots)))
    import fleet as fleet_inventory
    for m, s in readings.items():
        if not s:
            continue
        if s.get("ntp") == "no":
            clocks.append(m)
        for slot, index in slots[m]:
            host = "{}:{}".format(m, fleet_inventory.API_BASE + index)
            if host in wanted and s.get("ear.{}".format(slot)) == "0":
                deaf[host] = "its IPFS has no p2p listener, so no peer can reach it"
    return deaf, sorted(clocks)
