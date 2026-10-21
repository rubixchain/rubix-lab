# Rubix Lab

Scripts and tools for setting up and managing the Rubix Lab (In Office) testing
environment — a private Rubix network of office desktops, nine nodes on each,
used to test releases, bug fixes and new features.

**New here? Start with [systems/SETUP-RUNBOOK.md](systems/SETUP-RUNBOOK.md).**

## Layout

Two top-level folders, split by **where the thing runs**: `controller/` is
everything the controller machine drives; `systems/` is everything that goes
onto a lab machine.

```
controller/              run FROM the controller
  hosts.txt(.example)    the machines and their roles - the one fleet list
  fleet.py               reads hosts.txt + systems/node-layout.txt for every tool
  install-nodes.sh       install the nodes on machines (copies systems/, runs setup.sh)
  check-nodes.py         reachability / DID / balance sweep, one row per node
  dids-to-excel.py       create + register each node's DIDs, export to Excel
  node-versions.py       every node's build and commit, over SSH
  preflight-check.sh     fleet readiness table (docker, clock, units, sudo, API)
  node-logs.sh           follow the live log of one node, some, or the whole fleet
  fleet-console.sh       pick a machine, see its nodes, switch between their live logs
  install-console.sh     put the node console on the machines (no sudo)
  setup-ssh.sh           one-time passwordless SSH to every machine
  restart-nodes.sh       restart nodes fleet-wide (parallel)
  move-nodes-to-desktop.sh  one-time move of a machine's nodes from ~/rubix-lab
  faucet-setup.py        mint the two faucet DIDs' tokens, wire the faucet quorum
  wipe-node-db.sh        destructive DB + DID reset (parallel)
  exec-update/           build a branch and deploy it to every node
  test-plan/             the test catalogue and its runners
    master/              master-catalogue.csv + one case module per asset
    full-test/           test_runner.py, validate_cases.py, smoke_test.py, clients

systems/                 goes ON each lab machine
  SETUP-RUNBOOK.md       phase-by-phase environment setup
  LAB-QUICKREF.txt       the same setup as commands only, in order
  node-layout.txt        the nodes a pool machine runs and their DID counts
  bootstrap-nodes.txt    the fixed bootstrap list: the four fullnodes
  install/               setup.sh, config.toml.template, systemd template unit
  console/               rubix-console.sh - a machine's nodes and their live logs, on its screen
  prerequisite/          rubixgoplatform, ipfs, testnetswarm.key

nodes/                   gitignored runtime: one folder per node (node0..node8)
reports/                 gitignored output: reports/pdf/ and reports/json/
```

`nodes/` is deliberately **not** part of `systems/`: it holds runtime state
(the live binaries, generated `config.toml` with real lab IPs, DID private
keys), not source, and `exec-update` deploys into it by that path.

Every machine has this repo at the same path, `~/Desktop/rubix/rubix-lab` —
the controller keeps its git clone there and copies `systems/` there on every
other machine (`install-nodes.sh`), and every tool finds node *N* at
`~/Desktop/rubix/rubix-lab/nodes/node<N>`. (Until 2026-10-16 it was
`~/rubix-lab`; `controller/move-nodes-to-desktop.sh` moved the fleet.)

## Where a node is, and how to see its live log

A node is a folder, a systemd unit and a database:

| | Where | Example: node 3 on .107 |
|---|---|---|
| Folder | `~/Desktop/rubix/rubix-lab/nodes/node<slot>/` on its machine | `~/Desktop/rubix/rubix-lab/nodes/node3/` |
| Its own log | `log.txt` in that folder | `.../node3/log.txt` |
| Service (start/stop/restart) | `rubixgoplatform@<slot>` | `rubixgoplatform@3` |
| Database | Docker container `rubix-db-<slot>`, port 5433 + node_index | `rubix-db-3`, port 5733 |
| API | port 20000 + node_index | `http://192.168.1.107:20300` |

The fullnodes and the two faucet machines run one node each: slot 0, port
20000, folder `nodes/node0`. A pool machine runs slots 0–8 (ports 20000,
20100 … 20800). Inside a node folder: `rubixgoplatform` (the binary), `ipfs`,
`testnetswarm.key`, `config.toml`, `log.txt`, `testnet/` (the DID keys) and
`.ipfs/` (the peer ID; hidden, `ls -A`).

**Live logs from the controller** — every line is labelled with the node it
came from (`ip:port | ...`), Ctrl+C stops:
```
cd ~/Desktop/rubix/rubix-lab/controller
./node-logs.sh --host 192.168.1.102                 # one machine (a fullnode)
./node-logs.sh --host 192.168.1.107 --slot 3        # one node of a pool machine
./node-logs.sh --role fullnode                      # the four fullnodes together
./node-logs.sh --role faucet --role faucet-quorum   # the faucet pair
./node-logs.sh --include-fixed                      # every node in the fleet
./node-logs.sh --include-fixed --errors             # only [ERROR] and [WARN] lines, fleet-wide
./node-logs.sh --role fullnode --grep "rubix_txn"   # only lines containing a pattern
./node-logs.sh --host 192.168.1.105 --lines 200     # start with the last 200 lines
```
Without `--lines` it starts with each node's last 20 lines when following up
to 9 nodes, and with new lines only for more.

**On the machine itself** (e.g. sitting at .102):
```
tail -f ~/Desktop/rubix/rubix-lab/nodes/node0/log.txt    # the node's own log
journalctl -u rubixgoplatform@0 -f                       # the service: starts, crashes, restarts
```

## The node console - switching between a machine's nodes

Every machine with nodes has a console: a status table of its nodes, and
each node's live log one key away. Same keys wherever you open it:

| Key | Shows |
|---|---|
| (start) | status table, refreshed every 5s: slot, port, service, API, DIDs, peers, log size, last log line |
| `0`–`8` | that node's live log |
| `j` | the same node's service events (start, crash, restart) |
| `s` | back to the status table |
| `q` | leave - the nodes keep running, they are services, not this window |

**At the machine:** it opens on the screen by itself at login, maximised,
titled "Rubix nodes". Closed it? Open "Rubix nodes" from the app menu, or run
`~/Desktop/rubix/rubix-lab/systems/console/rubix-console.sh`.

**From the controller** - pick any machine from a list, `q` brings you back
to the list:
```
cd ~/Desktop/rubix/rubix-lab/controller
./fleet-console.sh                          # the list of machines
./fleet-console.sh --host 192.168.1.107     # straight to one
ssh rubix@192.168.1.107 '~/Desktop/rubix/rubix-lab/systems/console/rubix-console.sh --status'   # the table once, no keys
```
Use `./node-logs.sh` instead to watch many nodes at once or filter errors
fleet-wide (see above).

**Putting it on the machines** (no sudo; new installs get it from `setup.sh`):
```
./install-console.sh --include-fixed --close-old
```
`--close-old` closes the terminal windows left from the old single-node setup
(idle shells, and `journalctl -u rubixgoplatform.service -f` windows following
a service that no longer exists); a window running anything else is kept and
listed. The controller and explorer are never touched.

## Everyday commands on a fleet machine

Sitting at any lab machine (or `ssh rubix@<ip>` from the controller). The nodes
run as services in the background - they do not need a terminal open, and
closing one does not stop a node. On the fullnodes and the faucet pair only
slot 0 exists.

**Which nodes run here, and are they up**
```
systemctl list-units 'rubixgoplatform@*' --all --no-pager    # one line per node: active / failed / inactive
systemctl status rubixgoplatform@3 --no-pager                 # one node: state, since when, last log lines
for p in 20000 20100 20200 20300 20400 20500 20600 20700 20800; do echo "$p $(curl -s -m 2 -o /dev/null -w '%{http_code}' http://localhost:$p/rubix/v1/node/ping)"; done
docker ps --filter name=rubix-db --format '{{.Names}}  {{.Status}}  {{.Ports}}'   # the nodes' databases
```
In the port loop `200` = answering, `000` = not answering (a fullnode or faucet
machine only has 20000).

**Watch a node live** - Ctrl+C stops watching, the node keeps running
```
tail -f ~/Desktop/rubix/rubix-lab/nodes/node3/log.txt        # node 3's own log
journalctl -u rubixgoplatform@3 -f                            # node 3's service events
journalctl -u 'rubixgoplatform@*' -f                          # service events of every node on this machine
```
To switch to another node, Ctrl+C and run the command again with its slot.

**Start, stop, restart** (no password needed - setup.sh installed a grant for exactly these)
```
sudo systemctl restart rubixgoplatform@3
sudo systemctl stop rubixgoplatform@3
sudo systemctl start rubixgoplatform@3
```
`Restart=always`: a node that crashes comes back by itself within ~5s; only
`systemctl stop` keeps it down.

**A node's identity and peers**
```
curl -s http://localhost:20300/rubix/v1/dids; echo                 # its DIDs
curl -s http://localhost:20300/rubix/v1/node/peer_id; echo         # its peer ID (in "message")
cd ~/Desktop/rubix/rubix-lab/nodes/node3 && IPFS_PATH=.ipfs ./ipfs swarm peers | wc -l   # peers it is connected to
cd ~/Desktop/rubix/rubix-lab/nodes/node3 && ./rubixgoplatform -v | grep Commit           # the build it runs
du -sh ~/Desktop/rubix/rubix-lab/nodes/node*/log.txt                # log sizes (they only grow)
```

**Slot to port** (API / swarm / Postgres): slot 0 = 20000 / 4002 / 5433, then
+100 per slot - slot 3 = 20300 / 4302 / 5733, slot 8 = 20800 / 4802 / 6233.

**From the controller instead** (`cd ~/Desktop/rubix/rubix-lab/controller`):
`./node-logs.sh` (live logs, any nodes - see above), `./preflight-check.sh --host <ip>`
(services, databases, binaries, sudo, API per machine), `python3 check-nodes.py --host <ip>`
(each node's DIDs and RBT), `./restart-nodes.sh --host <ip> --slot 3 --force` (restart one node).

## How the repo is used

Lab machines never commit anything. The controller is the only machine that
commits (test results, inventory, config changes), and it pushes `systems/`
to the machines itself.

## What runs where

| Machine | What |
|---|---|
| Pool machines (no role in `hosts.txt`) | the 9 nodes of `systems/node-layout.txt`: ports 20000, 20100 … 20800, holding 1, 1, 1, 5, 5, 5, 5, 5 and 10 DIDs, each with its own Postgres |
| 4 fullnodes (.101–.104) | one node each, run with `-fullnode` — each receives and validates every published transaction |
| Faucet (.105) / faucet quorum (.106) | one node each: the faucet DID, and the quorum that signs every faucet transfer |
| Controller (.120) | no node — drives test operations and produces reports |
| Explorer (.123) | the explorer |

Quorums, senders and receivers are chosen per test case from the pool nodes.

## Rules that matter

- **Quorums must never run out of funds.** A quorum pledges at least the value
  of every transfer it signs. If one runs dry, transfers fail for the wrong
  reason and the test run is invalid.
- **Never regenerate the swarm key per machine.** It is the shared secret that
  makes all nodes one network.
- **Every node lives in `~/Desktop/rubix/rubix-lab/nodes/node<slot>` and its
  ports follow its `node_index`** (`systems/node-layout.txt`). That folder is
  both the node's working directory and its profile. No per-machine variation.
- **`nodes/` is never committed.** It holds binaries, generated config with real
  lab IPs, and DID private keys.
