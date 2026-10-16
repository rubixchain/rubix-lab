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
