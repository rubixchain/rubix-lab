# Rubix Lab — Environment Setup Runbook

Follow this in order. Each phase ends with a check — do not move on until it
passes. The goal: from machines running the old single-node install (or bare
machines) to **every node reachable on the lab's custom testnet, every DID
created, the faucet funded and the smoke test passing**.

What the lab imitates: Rubix's default testnet — testnet mode, a
`testnetswarm.key`, a fixed bootstrap list and an allowlist of minters — on a
network of its own: the lab's swarm key, the lab's four fullnodes as the
bootstrap peers, and the lab's faucet DIDs as the allowed minters, compiled
into the `vaishnav/temp-custom-testnet` build every node runs.

Commands run on the **controller** unless a step says otherwise, from
`~/rubix-lab/controller` (on .120 the repo sits at `~/Desktop/rubix/rubix-lab`
and the product repo at `~/Desktop/rubix/rubixgoplatform` — use those paths
there). Every controller tool reads `controller/hosts.txt` through
`controller/fleet.py`.

---

## Phase A — Roles and layout

### A1. Roles (`controller/hosts.txt`)

| Role | Machines | Runs |
|---|---|---|
| `fullnode` | .101 .102 .103 .104 (one keyboard/mouse set) | one node, `-fullnode`, no DID. Together the bootstrap peers (`systems/bootstrap-nodes.txt`) |
| `faucet` | .105 | one node, the faucet DID |
| `faucet-quorum` | .106 | one node, the faucet quorum DID |
| `controller` | .120 | no node — drives everything |
| `explorer` | .123 | the explorer — no node |
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
fullnode, faucet and faucet-quorum nodes are slot 0: port 20000. Every node
lives in `~/rubix-lab/nodes/node<slot>` (home folder) and runs as systemd unit
`rubixgoplatform@<slot>`.

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
plus **Go 1.22 or newer** for building the product (`go.mod` says 1.22).
Ubuntu 24.04's `golang-go` is 1.22; on 22.04 use `sudo snap install go --classic`.
Check with `go version`.

### B2. This repo and the product repo

```
git clone <rubix-lab repo> ~/rubix-lab
chmod +x ~/rubix-lab/controller/*.sh ~/rubix-lab/controller/exec-update/*.sh
git clone https://github.com/rubixchain/rubixgoplatform ~/Desktop/rubixgoplatform
```
`controller/hosts.txt` is gitignored (real lab IPs), so a fresh clone does
not have it: copy it over, or write it from `hosts.txt.example` with the roles
in A1. If the product repo lives elsewhere, set `REPO_DIR` in
`controller/exec-update/exec-update.env` (copy the `.example`).

### B3. Passwordless SSH to every machine

```
cd ~/rubix-lab/controller
./setup-ssh.sh
```
Asks each machine's login password once. See `controller/VERSION-CHECK.txt`.

**Check:** `setup-ssh.sh` ends with every machine `OK`.

---

## Phase C — Docker Engine on every machine that runs a node

Fullnodes, the faucet pair and every pool machine.

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

## Phase D — Clear out the old installs (once)

Decided 2026-10-09: the old tokens are flushed, so the old DIDs go too, and
the faucet gets new DIDs and a new mint (Phase F). There is no undo. It is the
only wipe: the fresh fleet then runs on without being wiped again.

### D1. The old single node on every machine

```
cd ~/rubix-lab/controller
./wipe-node-db.sh --legacy --include-fixed          # dry run: what it would do, per machine
./wipe-node-db.sh --legacy --include-fixed --yes    # type 'wipe' to confirm
```
It stops `rubixgoplatform.service`, deletes the `node` (or `rubix-node-db`)
container and volume, the old node's profile (config, DIDs, IPFS repo) and its
binaries. It refuses on a machine where a Rubix node is still running.
On .120 and .123 (no node any more) remove the old unit file by hand:
`sudo systemctl disable rubixgoplatform && sudo rm /etc/systemd/system/rubixgoplatform.service`

### D2. Everything else from the old setups

The legacy wipe only knows the standard single-node layout. Old Postgres
containers under other names, old localnet/testnet folders, the old faucet
folder and old swarm keys are found with two scans and removed by name:

- containers and listeners on the node machines:
  ```
  for h in $(python3 fleet.py hosts --include-fixed --with-nodes | awk '{print $1}'); do out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 rubix@$h "docker ps -a --format '{{.Names}}  {{.Image}}  {{.Status}}  {{.Ports}}'; ss -ltn | grep -E ':(20000|4002) '; pgrep -af '[r]ubixgoplatform run'" 2>&1); [ -n "$out" ] && printf '== %s\n%s\n' "$h" "$out"; done
  ```
- old folders and keys on every machine (current nodes, the controller's
  repos and `~/old_reports` are left out):
  ```
  for h in $(python3 fleet.py hosts --include-fixed | awk '{print $1}'); do out=$(ssh -o BatchMode=yes -o ConnectTimeout=5 rubix@$h 'find ~ -maxdepth 4 \( -name localnet -o -name testnet -o -name .ipfs -o -name "*swarm.key" -o -iname "faucet*" \) -not -path "$HOME/rubix-lab/*" -not -path "$HOME/Desktop/rubix/rubix-lab/*" -not -path "$HOME/Desktop/rubix/rubixgoplatform/*" -not -path "$HOME/old_reports/*" 2>/dev/null' 2>&1); [ -n "$out" ] && printf '== %s\n%s\n' "$h" "$out"; done
  ```
Keep the old controller's reports (.103): copy them to `~/old_reports` before
deleting its old folders.

### D3. Strays on the lab key

Every node on the lab key finds the others on the LAN by itself, so any
machine still running IPFS or a node with that key joins the new network
(on 2026-10-13: a node on .143 and an extra IPFS daemon on .102 port 4001).
Find them in a fullnode's peer list and stop them:
```
ssh rubix@192.168.1.101 'IPFS_PATH=$HOME/rubix-lab/nodes/node0/.ipfs $HOME/rubix-lab/nodes/node0/ipfs swarm peers'
```

**Check:** both scans print nothing, and a fullnode's peer list holds only
the lab machines you installed.

---

## Phase E — The build, the fullnodes, the faucet pair

`install-nodes.sh` copies `systems/` to each machine and runs `setup.sh` there
over SSH — no git clone needed on the machines. One machine at a time; each
asks for its sudo password once. See `systems/install/README.md`.

### E0. Build `vaishnav/temp-custom-testnet` and make it the install binary

`systems/prerequisite/rubixgoplatform` in git is an old release (v1.0.4) with
no custom-testnet code. Build the lab's branch once and put it there on the
controller, so every node starts on it:
```
cd ~/Desktop/rubixgoplatform
git fetch origin
git checkout vaishnav/temp-custom-testnet
git pull --ff-only origin vaishnav/temp-custom-testnet
make compile-linux
cp ~/Desktop/rubixgoplatform/linux/rubixgoplatform ~/rubix-lab/systems/prerequisite/rubixgoplatform
```
Git on the controller then shows that file as modified - expected.

### E1. The fullnodes, then the bootstrap list

```
cd ~/rubix-lab/controller
./install-nodes.sh --role fullnode
```
Then write their peer IDs into `systems/bootstrap-nodes.txt`:
```
cd ~/rubix-lab && { grep '^#' systems/bootstrap-nodes.txt; for h in 101 102 103 104; do p=$(curl -s --max-time 5 http://192.168.1.$h:20000/rubix/v1/node/peer_id | python3 -c 'import json,sys; print(json.load(sys.stdin)["message"])' 2>/dev/null); case "$p" in 12D3Koo*) echo "/ip4/192.168.1.$h/tcp/4002/p2p/$p" ;; *) echo "ERROR: .$h gave no peer ID" >&2 ;; esac; done; } > systems/bootstrap-nodes.new && mv systems/bootstrap-nodes.new systems/bootstrap-nodes.txt && cat systems/bootstrap-nodes.txt
```
Commit and push `systems/bootstrap-nodes.txt`, then give the fullnodes each
other and restart them:
```
cd ~/rubix-lab/controller
./install-nodes.sh --role fullnode --force
for h in 101 102 103 104; do ./restart-nodes.sh --host 192.168.1.$h --force; done
```

**Check:** every fullnode's `config.toml` lists the other three, its log shows
`Minter allowlist: custom testnet` and `Successfully subscribed to topic: rubix_txn`.

### E2. The faucet pair

```
./install-nodes.sh --role faucet --role faucet-quorum
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
the build it runs (`core/minterallowlist/mint_access_control.go`). Until this
step it names the OLD faucet DIDs, so every node would reject tokens minted by
the new ones.

On branch `vaishnav/temp-custom-testnet`, replace the two DIDs in
`CustomNetAllowedMinters` — keep `Level: 60001` and the ranges: the faucet DID
`1`–`2150000`, the faucet quorum `2150001`–`4300000`. Commit and push.

### F3. Deploy that build to the installed nodes, and make it the install binary

```
cd ~/rubix-lab/controller/exec-update
./update-exec.sh vaishnav/temp-custom-testnet 192.168.1.101 192.168.1.102 192.168.1.103 192.168.1.104 192.168.1.105 192.168.1.106
cp ~/Desktop/rubixgoplatform/linux/rubixgoplatform ~/rubix-lab/systems/prerequisite/rubixgoplatform
cd ~/rubix-lab/controller && python3 node-versions.py --include-fixed
```
The six installed nodes show the new commit (pool rows fail until Phase G).

### F4. Mint and wire the faucet

Mint the faucet DID's tokens within its range and the faucet quorum's within
its range (level 60001) — the faucet quorum generously: every payout stays
pledged on it until the receiver spends it. Add the faucet quorum DID as the
**only** quorum on the faucet node, and run quorum setup for it on the
faucet-quorum node. The lab never mints anything else (CLAUDE.md "Funding rule").

**Check:** Phase H's smoke test starts with the faucet line
`Faucet: ... faucet holds N RBT free` — anything else is printed as the reason.

---

## Phase G — The pool

### G1. One pool machine first — the pilot

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
   pgrep -fc "[i]pfs daemon"
   for p in $(pgrep -f "[i]pfs daemon"); do [ "$(readlink /proc/$p/cwd)" = "$HOME/rubix-lab/nodes/node3" ] && kill "$p" && echo "killed node 3's IPFS (pid $p)"; done
   pgrep -fc "[i]pfs daemon"
   ```
   The first count is 9; after node 3 restarts its IPFS (its `log.txt` says
   when) it is 9 again. Then, on the controller:
   ```
   ./restart-nodes.sh --host 192.168.1.107 --slot 3 --force
   ssh rubix@192.168.1.107 'pgrep -fc "[i]pfs daemon"'
   ./preflight-check.sh --host 192.168.1.107
   ```
   Fewer than 9 means stopping one node killed its neighbours' IPFS - a
   product finding to report to core, and a reason to restart nodes one
   machine at a time until it is fixed.
3. **Reboot.** `ssh -t rubix@192.168.1.107 'sudo reboot'`; after ~3 minutes
   `./preflight-check.sh --host 192.168.1.107` shows `9/9` everywhere.

### G2. The rest of the pool

```
./install-nodes.sh --role pool
./preflight-check.sh --include-fixed
python3 check-nodes.py --include-fixed
```
Machines whose nodes already all answer are skipped (`--force` to re-run).

### G3. The pool's DIDs

```
python3 dids-to-excel.py
python3 check-nodes.py
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
cd ~/rubix-lab/controller/test-plan/full-test
python3 smoke_test.py          # 3 quorums, 14 sender/receiver pairs, spread over machines
```

Then confirm every fullnode recorded the transactions (python3-psycopg2 is
already on the controller; psql is not):
```
for h in 101 102 103 104; do python3 -c "import psycopg2; c = psycopg2.connect(host='192.168.1.$h', port=5433, dbname='rubix', user='rubix', password='rubixpass'); cur = c.cursor(); cur.execute('SELECT count(*) FROM fullnode_transactions'); print('.$h', cur.fetchone()[0])"; done
```
Then bring up the explorer on .123 (moved from .102) and confirm it shows
them too.

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
| `setup.sh` refuses: old container `node`, or a port "already taken by container" | An old install is still there — Phase D. |
| A node will not start after a restart: its swarm, IPFS or Postgres port is "already in use" | Another node's peer-port pool holds it — the cost of the 100 spacing, see `systems/node-layout.txt`. `ss -ltnp | grep <port>` names the holder; restart the node that is down. |
| Several nodes on one machine lose IPFS when one node is stopped | The product's last-resort `pkill -f "ipfs daemon"` (Phase G1 check 2). `./restart-nodes.sh --host <ip>`; report it. |
| Unknown peers in a fullnode's peer list | A machine still running IPFS or a node on the lab key — Phase D3. |
| Transfers refused with `invalid epoch` | Clocks differ — the product allows no difference (`core/consensus/checks.go:61`). Preflight's CLOCK column. |
| Every faucet transfer refused naming the minter allowlist | That node runs a build without the new faucet DIDs — `node-versions.py --include-fixed`, then Phase F3. |
| Works on the machine, not from the controller | Firewall on the machine, or network client isolation. `setup.sh` opens the ports only when `ufw` is active. |
| Nodes not starting after reboot | Docker not enabled at boot (Phase C), or a unit not enabled — preflight shows which. |
| Transfers fail with pledge errors | Quorum out of funds — top up; the run is invalid. |
| Node cannot find peers | Wrong swarm key or bootstrap list — compare `config.toml` with another machine's, and `systems/bootstrap-nodes.txt`. |
| Transfers fail on a machine that was down | DID announcements made while it was offline never reached it. The test runner announces every DID at the start of each run; for a hand test, register the DIDs involved again (`POST /rubix/v1/dids/<did>/register`, then sign). |
