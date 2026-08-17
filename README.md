# Rubix Lab

Scripts and tools for setting up and managing the Rubix Lab (In Office) testing
environment — a private `localnet` network of office desktops used to test
releases, bug fixes and new features.

**New here? Start with [SETUP-RUNBOOK.md](SETUP-RUNBOOK.md).**

## Layout

| Folder | Purpose | Goes where |
|---|---|---|
| [prerequisite/](prerequisite/) | Node binary, IPFS binary, swarm key | Every machine |
| [install/](install/) | `setup.sh` — brings up one node on one machine | Every machine |
| [controller/](controller/) | `check-nodes.py` — verifies the controller can reach every node | Controller only |
| [exec-update/](exec-update/) | Build any branch and deploy it to running nodes | Controller only |
| [test-plan/](test-plan/) | 259-case test catalogue (CSV) and how to read it | Reference |
| `nodes/` | Where each machine's running node lives | Created by setup, gitignored |

Every machine clones this whole repo to the same path under its own home
directory (e.g. `~/rubix-lab`). `exec-update` relies on that being consistent.

## Quick start on a new machine

```
sudo apt update && sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # then log out and back in

git clone <this repo> ~/rubix-lab
cd ~/rubix-lab/install
LOCALNET_BOOTSTRAP_NODES='[...]' ./setup.sh
```

Use **Docker Engine as above, not Docker Desktop** — Desktop needs a logged-in
session, so containers would not restart after an unattended reboot.

Quorum machines go first with `'[]'`, then their peer IDs form the bootstrap
list for everything else. Full detail in the runbook.

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
