# Rubix lab fleet — branch-to-exec deploy tool

Give it a branch name and the machines to deploy to; it builds
`rubixgoplatform` from that branch once and swaps it into every node on those
machines, restarting them. Runs from the **controller**.

## Why this is SSH-based, not the Rubix API

Every node runs as a systemd unit, `rubixgoplatform@<slot>` (installed by
[../../systems/install/setup.sh](../../systems/install/setup.sh)), with
`Restart=always`. Calling a node's own graceful-shutdown API would make the
process exit — and systemd would immediately relaunch the *old* binary, since
from the supervisor's point of view nothing asked it to stop. Actually
stopping it needs `systemctl stop`, which is an SSH operation. So the whole
flow — stop, copy, swap, start — is SSH end to end.

## One-time setup (on the controller)

Two different repos are involved, don't mix them up: the **product repo**
(`rubixgoplatform`, cloned on the controller for *building*) and **this
`rubix-lab` repo** (on every lab machine at `~/rubix-lab`, holding each node's
folder `nodes/node<slot>/` — see [../../systems/install/README.md](../../systems/install/README.md)).

1. Clone `rubixgoplatform` on the controller (default
   `~/Desktop/rubixgoplatform`; set `REPO_DIR` in `exec-update.env` for
   anywhere else).
2. Install `Go 1.22` + `build-essential` on the controller (needed for
   `make compile-linux`).
3. Passwordless SSH from the controller to every machine:
   `../setup-ssh.sh` (asks each machine's password once).
4. Nothing to install on the machines for sudo: `setup.sh` grants the SSH
   user passwordless `systemctl start|stop|restart rubixgoplatform@<slot>`
   for exactly that machine's nodes (`/etc/sudoers.d/rubix-lab-nodes`).
5. Optional: `cp exec-update.env.example exec-update.env` to keep settings.

## Usage

```
./update-exec.sh <branch> <machine-ip> [machine-ip...]   # every node on those machines
./update-exec.sh <branch> --all                          # every pool machine
./update-exec.sh <branch> --all --include-fixed          # plus the fullnode and faucet machines
```

Which nodes a machine has comes from `../hosts.txt` and
`../../systems/node-layout.txt`, through `../fleet.py`.

Example — try `fix/pledge-split` on one machine, then everywhere:
```
./update-exec.sh fix/pledge-split 192.168.1.107
./update-exec.sh fix/pledge-split --all --include-fixed
cd .. && python3 node-versions.py --include-fixed
```

A build that changes validation — such as a new minter allowlist — has to
reach **every** node, fullnodes and faucet nodes included, so use
`--all --include-fixed` for those.

## What it does, in order

1. **Builds first, deploys only if the build succeeds** — `git fetch` +
   `checkout` + `pull --ff-only` + `make compile-linux` on the controller.
   A bad branch name or a compile error stops here; no node is touched.
2. Per machine, all machines in parallel: check every node folder has a
   binary (an uninstalled machine is refused, not half-deployed) → stop every
   node's unit → copy the binary over once → put it in each node folder under
   a temp name and rename it into place (atomic, never a half-written binary)
   → start the units → poll each node's `/rubix/v1/dids` for up to 60s.
3. Prints a summary line per machine: which branch/commit its nodes now run,
   or exactly what to check if they didn't come back cleanly.

## What it deliberately does NOT do

Doesn't touch DIDs, quorum wiring, or peering — those survive a binary swap
untouched (they live in each node's folder and database, not the binary).
Doesn't decide targets on its own — you choose the machines and when.
