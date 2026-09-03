# Rubix Lab

Scripts and tools for setting up and managing the Rubix Lab (In Office) testing
environment — a private `localnet` network of office desktops used to test
releases, bug fixes and new features.

**New here? Start with [SETUP-RUNBOOK.md](SETUP-RUNBOOK.md).**

## Layout

Two top-level folders, split by **where the thing runs**: `controller/` is
everything the controller machine drives; `systems/` is everything that goes
onto a lab desktop.

```
controller/              run FROM the controller
  hosts.txt(.example)    the one fleet list every controller tool reads
  check-nodes.py         reachability / DID / balance sweep
  dids-to-excel.py       create + register DIDs, export to Excel
  node-versions.py       per-host build, over SSH
  preflight-check.sh     fleet readiness table (docker, unit, sudo, API)
  setup-ssh.sh           one-time passwordless SSH to every host
  restart-nodes.sh       restart nodes fleet-wide (parallel)
  wipe-node-db.sh        destructive DB + DID reset (parallel)
  exec-update/           build a branch and deploy it to running nodes
  test-plan/             the test catalogue and its runners
    full-test/           master-test-cases.xlsx, case_runner.py, smoke_test.py
    rbt/ ft/ nft/ sc/ cross-asset/ general/   per-asset case modules

systems/                 goes ON each lab machine
  install/               setup.sh, config.toml.template, systemd unit
  prerequisite/          rubixgoplatform, ipfs, localnetswarm.key

nodes/                   gitignored runtime: the running node's own data
reports/                 gitignored output: reports/pdf/ and reports/json/
```

`nodes/` is deliberately **not** part of `systems/`: it holds runtime state
(the live binary, generated `config.toml` with real lab IPs, DID private
keys), not source, and `exec-update` deploys into it by that path.

Every machine clones this whole repo to the same path under its own home
directory (e.g. `~/rubix-lab`). `exec-update` relies on that being consistent.

## Quick start on a new machine

```
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # then log out and back in

git clone <this repo> ~/rubix-lab
cd ~/rubix-lab/systems/install
LOCALNET_BOOTSTRAP_NODES='[...]' ./setup.sh
```

Use **Docker Engine as above, not Docker Desktop** — Desktop needs a logged-in
session, so containers would not restart after an unattended reboot.

The fullnode goes first with `'[]'`, then its peer ID becomes the bootstrap
entry for every other machine. Full detail in the runbook.

## How the repo is used

Normal machines only ever **pull** — they clone the repo to get the binaries,
swarm key and scripts, and never commit anything back. The controller is the
only machine that commits (test results, inventory, config changes).

## What runs where

- **Every desktop** — one Rubix node, port 20000, `localnet` mode, own Postgres
- **3–5 desktops** — also act as quorum: they sign transactions and must stay
  funded above 1000 RBT at all times
- **One desktop** — the controller: drives test operations and produces reports
- **Dedicated** — a fullnode and an explorer for this environment

## Rules that matter

- **Quorums must never run out of funds.** A quorum pledges at least the value
  of every transfer it signs. If one runs dry, transfers fail for the wrong
  reason and the test run is invalid. Minting is free on localnet — keep them
  well above the floor.
- **Never regenerate the swarm key per machine.** It is the shared secret that
  makes all nodes one network.
- **One node per machine, always port 20000.** No per-machine variation.
- **`nodes/` is never committed.** It holds binaries, generated config with real
  lab IPs, and DID private keys.
