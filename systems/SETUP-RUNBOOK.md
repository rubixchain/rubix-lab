# Rubix Lab — Environment Setup Runbook

Follow this in order. Each phase ends with a check — do not move on until it
passes. The goal: from machines running the old single-node install (or bare
machines) to **every node reachable, every DID created, the faucet funded and
the smoke test passing**.

Commands run on the **controller** unless a step says otherwise. Every
controller tool reads `controller/hosts.txt` through `controller/fleet.py`,
and machines are added later by repeating Phase E for them.

---

## Phase A — Roles and layout

### A1. Roles (`controller/hosts.txt`)

| Role | Machines | Runs |
|---|---|---|
| `fullnode` | .101 .102 .103 .104 (one keyboard/mouse set) | one node, `-fullnode`, no DID. .101 is the bootstrap seed |
| `faucet` | .105 | one node, the faucet DID |
| `faucet-quorum` | .106 | one node, the faucet quorum DID |
| `controller` | .120 | no node — drives everything |
| `explorer` | .123 | no node |
| pool (no role) | every other machine | the 9 nodes of `systems/node-layout.txt` |

Quorum, sender and receiver are not roles: the test runner picks them per case
from the pool nodes. Down machines stay commented out with a reason and date.

### A2. Nodes on a pool machine (`systems/node-layout.txt`)

| Slot | node_index | API | Swarm | Postgres | DIDs |
|---|---|---|---|---|---|
| 0 | 0 | 20000 | 4002 | 5433 | 1 |
| 1 | 100 | 20100 | 4102 | 5533 | 1 |
| 2 | 200 | 20200 | 4202 | 5633 | 1 |
| 3–7 | 300–700 | 20300–20700 | 4302–4702 | 5733–6133 | 5 each |
| 8 | 800 | 20800 | 4802 | 6233 | 10 |

38 DIDs per machine. With today's 26 pool machines: 234 nodes, 988 DIDs. The
fullnode, faucet and faucet-quorum nodes are slot 0: port 20000.

### A3. Standardise access

- Same OS user on every machine: `rubix`
- Same wallet password on every node (the controller needs it for every call)
- Static IP or DHCP reservation for every machine — everything is keyed on it

**Check:** `python3 controller/fleet.py hosts --include-fixed` lists every
machine with the right role.

---

## Phase B — The controller (.120)

The controller runs no node, so it needs no Docker.

### B1. Packages

```
sudo apt update
sudo apt install -y git python3 python3-psycopg2 python3-reportlab python3-openpyxl \
                    curl rsync build-essential
```
plus **Go 1.22 or newer** for building branches (`exec-update`). Ubuntu 24.04's
`golang-go` is 1.22; on 22.04 use `sudo snap install go --classic`.
Check with `go version`.

### B2. This repo and the product repo

```
git clone <rubix-lab repo> ~/rubix-lab
chmod +x ~/rubix-lab/controller/*.sh ~/rubix-lab/controller/exec-update/*.sh
git clone https://github.com/rubixchain/rubixgoplatform ~/Desktop/rubixgoplatform
```
`controller/hosts.txt` is gitignored (real lab IPs), so a fresh clone does
not have it: copy it from the old controller, or write it from
`hosts.txt.example` with the roles in A1.

### B3. Passwordless SSH to every machine

```
cd ~/rubix-lab/controller
./setup-ssh.sh
```
Asks each machine's login password once. See `controller/VERSION-CHECK.txt`.

**Check:** `setup-ssh.sh` ends with every machine `OK`.

---

## Phase C — Docker Engine on every machine that runs a node

Fullnodes, the faucet pair and every pool machine. (Already done on machines
from the old fleet — Phase E's checks show any that are not.)

**Use Docker Engine from the command line. Do not use Docker Desktop for Linux.**
Docker Desktop runs containers inside a VM and needs a logged-in desktop
session; these nodes must survive reboots with nobody logged in.

```
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker     # start now AND at every boot
sudo usermod -aG docker $USER          # use docker without sudo
```

Log out and back in so the group change takes effect. Ubuntu's own `docker.io`
is enough: `setup.sh` uses plain `docker run`, no Compose.

**`enable` is the important one.** The database containers are created with
`--restart always`, but that only helps if the Docker service itself starts at
boot. Skip it and every reboot leaves the nodes with no database.

**Check (on the machine):** `docker run --rm hello-world` works without sudo,
and `systemctl is-enabled docker` prints `enabled`.

> **Do not purge and reinstall Docker to "start clean".** Stopping
> `docker.socket` breaks the daemon: Docker runs as `dockerd -H fd://`, which
> means systemd hands it a pre-opened socket from `docker.socket`. With that
> socket stopped, `dockerd` exits immediately with
> `failed to load listeners: no sockets found via socket activation`, retries,
> and then systemd rate-limits it with `Start request repeated too quickly`.
>
> Recover with:
> ```
> sudo systemctl reset-failed docker.service docker.socket
> sudo systemctl start docker.socket
> sudo systemctl start docker
> ```
> `reset-failed` must come first, or systemd refuses to start the unit at all.
>
> Also harmless: `Deleting nftables IPv4/IPv6 rules error="exit status 1"` at
> startup — info level, Docker starts normally afterwards.

---

## Phase D — Flush the old single-node install (once)

Decided 2026-10-09: the old tokens are flushed, so the old DIDs go too, and
the faucet gets new DIDs and a new mint (Phase F). This deletes every old
node's DIDs, wallet and IPFS repo — there is no undo. It is the only wipe:
the fresh fleet then runs on without being wiped again.

```
cd ~/rubix-lab/controller
./wipe-node-db.sh --legacy --include-fixed          # dry run: what it would do, per machine
./wipe-node-db.sh --legacy --include-fixed --yes    # type 'wipe' to confirm
```

It stops `rubixgoplatform.service`, deletes the `node` (or `rubix-node-db`)
container and volume, the old node's profile (config, DIDs, IPFS repo) and its
binaries. `~/Desktop/rubix` itself goes only when nothing else is in it — on
.103 the old controller's `rubix-lab` clone lives there and is kept (copy its
`controller/hosts.txt` and anything else you want from it to .120).

- **.103 ran a second node** (the old faucet quorum, port 20010). The wipe
  refuses on a machine where a Rubix node is still running and prints it —
  stop that one by hand, then re-run with `--host 192.168.1.103 --yes`.
- **.102 runs the explorer today; it moves to .123.** Stop it on .102 — the
  fullnode installed there in Phase E needs ports 20000, 4002 and 5433.
- **.120 and .123 will run no node.** Their old unit file would retry at
  every boot; remove it there:
  `sudo systemctl disable rubixgoplatform && sudo rm /etc/systemd/system/rubixgoplatform.service`
  (On every other machine `setup.sh` removes it in Phase E.)

**Check:** every machine reports `done — old install gone`.

---

## Phase E — Install the nodes

`install-nodes.sh` copies `systems/` to each machine and runs `setup.sh` there
over SSH — no git clone needed on the machines. One machine at a time; each
asks for its sudo password once. See `systems/install/README.md`.

### E1. The fullnodes — the first one alone

```
./install-nodes.sh --host 192.168.1.101 --first     # empty bootstrap list: the seed
./install-nodes.sh --role fullnode                  # .102-.104 join through .101
```

### E2. The faucet pair

```
./install-nodes.sh --role faucet --role faucet-quorum
```

Every machine after the first fullnode gets a bootstrap list of every
fullnode that answers, built from their own peer IDs.

### E3. One pool machine first — the pilot

```
./install-nodes.sh --host 192.168.1.107
```

Then check, before installing the rest:

1. **Capacity.** On .107: `free -h`, `uptime`, and
   `systemctl status 'rubixgoplatform@*' --no-pager | grep -E '●|Memory'`
   (each line's memory includes that node's IPFS daemon) and
   `docker stats --no-stream`. Look again during a burst (Phase H's smoke
   test). Nine nodes must leave the machine memory to spare.
2. **One node's shutdown must not take the others' IPFS down.** The product's
   last-resort IPFS kill is `pkill -f "ipfs daemon"` / `killall -9 ipfs`
   (`core/shutdown_manager.go:280`), reached when the node's saved IPFS PID
   is stale — and an IPFS auto-restart never updates it. On .107:
   ```
   pgrep -fc "[i]pfs daemon"                     # 9 (the [i] keeps pgrep from counting this shell)
   for p in $(pgrep -f "[i]pfs daemon"); do echo "$p $(readlink /proc/$p/cwd)"; done
   kill <the PID whose folder is .../nodes/node3>   # node 3's IPFS dies; the node restarts it
   pgrep -fc "[i]pfs daemon"                     # back to 9 once it has (node3/log.txt says when)
   ```
   then, on the controller:
   ```
   ./restart-nodes.sh --host 192.168.1.107 --slot 3 --force
   ssh rubix@192.168.1.107 'pgrep -fc "[i]pfs daemon"'   # still 9?
   ./preflight-check.sh --host 192.168.1.107             # API 9/9?
   ```
   Fewer than 9 means stopping one node killed its neighbours' IPFS - a
   product finding to report to core, and a reason to restart nodes one
   machine at a time until it is fixed.
3. **Reboot.** Reboot .107; after ~2 minutes `./preflight-check.sh --host 192.168.1.107`
   shows `9/9` everywhere.

### E4. The rest of the pool

```
./install-nodes.sh --role pool
```
Machines whose nodes already all answer are skipped (`--force` to re-run).

**Check:**
```
./preflight-check.sh --include-fixed      # every machine "fully ready"
python3 check-nodes.py --include-fixed    # every node reachable (no DIDs yet)
```

---

## Phase F — Faucet DIDs, the allowlist build, faucet tokens

### F1. Create the two faucet DIDs

```
python3 dids-to-excel.py --host 192.168.1.105
python3 dids-to-excel.py --host 192.168.1.106
```
Each prints the DID it created.

### F2. Put them in the minter allowlist

Every node checks who minted each token against `CustomNetAllowedMinters` in
the build it runs (`core/minterallowlist/mint_access_control.go`). The list
names the OLD faucet DIDs, so until this step every node would reject tokens
minted by the new ones.

In `~/Desktop/rubixgoplatform`, branch `vaishnav/temp-custom-testnet`, replace
the two DIDs in `CustomNetAllowedMinters` — keep `Level: 60001` and the ranges:
the faucet DID `1`–`2150000`, the faucet quorum `2150001`–`4300000`. Commit and
push the branch.

### F3. Deploy that build to every node — fullnodes and faucet included

```
cd exec-update
./update-exec.sh vaishnav/temp-custom-testnet --all --include-fixed
cd .. && python3 node-versions.py --include-fixed      # "Uniform" on the new commit
```
On any node, `grep "Minter allowlist" ~/rubix-lab/nodes/node0/log.txt` shows
`custom testnet` with `minters=2`.

### F4. Mint and wire the faucet

By hand, as on 2026-09-28: mint the faucet DID's tokens within its range and
the faucet quorum's within its range (level 60001); add the faucet quorum DID
as the **only** quorum on the faucet node, and run quorum setup for it on the
faucet-quorum node. The lab never mints anything else (CLAUDE.md "Funding rule").

**Check:** Phase H's smoke test starts with the faucet line
`Faucet: ... faucet holds N RBT free` — anything else is printed as the reason.

---

## Phase G — The pool's DIDs

```
python3 dids-to-excel.py
```
Creates up to 988 DIDs: one at a time per node, nodes in parallel, re-reading
the node's DID list before and after every create (creating a DID cannot be
safely repeated - `core/did.go`). A node where anything looks off is stopped
and named, never retried; look at it by hand and run the tool again. It writes
`dids.xlsx` (sheet `DIDs`: one row per DID).

**Check:** `python3 check-nodes.py` ends with `DIDs : 988 held, 988 in the layout`.

---

## Phase H — Smoke test

```
cd test-plan/full-test
python3 smoke_test.py          # 3 quorums, 14 sender/receiver pairs, spread over machines
```

Then confirm every fullnode recorded the transactions — on each of .101-.104:
```
psql -h 192.168.1.101 -p 5433 -U rubix -d rubix -c "SELECT count(*) FROM fullnode_transactions"
```
(password `rubixpass`). Then bring up the explorer on .123 (moved from .102)
and confirm it shows them too.

**Check:** the smoke test passes, all four fullnodes have rows, and the
explorer shows the transactions.

---

## Phase I — Record the baseline

The first full run on a known-good build sets the baselines every later run
is compared against:
```
python3 test_runner.py --collect-versions
```
Limits recorded on the old 31-node fleet do not carry over — e.g.
`senders: "all"` cases now run ~230 senders through one quorum.

---

## Routine checks

Before every test cycle, and after any reboot or power cut:
```
./preflight-check.sh --include-fixed
python3 check-nodes.py --include-fixed
```
- A node down → `./restart-nodes.sh` (restarts only nodes that are down)
- DIDs below target → `python3 dids-to-excel.py`
- CLOCK `FAIL` → `sudo timedatectl set-ntp true` on that machine

---

## Common problems

| Symptom | Likely cause |
|---|---|
| `failed to load listeners: no sockets found via socket activation` | `docker.socket` was stopped. See the box in Phase C. |
| `Start request repeated too quickly` | systemd rate-limited the unit. `sudo systemctl reset-failed <unit>` first, then fix the cause. |
| `setup.sh` refuses: old container `node` | The old single-node install is still there — Phase D. |
| A node will not start after a restart: its swarm, IPFS or Postgres port is "already in use" | Another node's peer-port pool holds it — the cost of the 100 spacing, see `systems/node-layout.txt`. `ss -ltnp | grep <port>` names the holder; restart the node that is down. |
| Several nodes on one machine lose IPFS when one node is stopped | The product's last-resort `pkill -f "ipfs daemon"` (Phase E3 check 2). `./restart-nodes.sh --host <ip>`; report it. |
| Transfers refused with `invalid epoch` | Clocks differ — the product allows no difference (`core/consensus/checks.go:61`). Preflight's CLOCK column. |
| Every faucet transfer refused naming the minter allowlist | That node runs a build without the new faucet DIDs — `node-versions.py --include-fixed`, then Phase F3. |
| Works on the machine, not from the controller | Firewall on the machine, or network client isolation. `setup.sh` opens the ports only when `ufw` is active. |
| Nodes not starting after reboot | Docker not enabled at boot (Phase C), or a unit not enabled — preflight shows which. |
| Transfers fail with pledge errors | Quorum out of funds — top up; the run is invalid. |
| Node cannot find peers | Wrong swarm key or bootstrap list — compare `config.toml` with another machine's. |
| Transfers fail on a machine that was down | DID announcements made while it was offline never reached it. The test runner announces every DID at the start of each run; for a hand test, register the DIDs involved again (`POST /rubix/v1/dids/<did>/register`, then sign). |
