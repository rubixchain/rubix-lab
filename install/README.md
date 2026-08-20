# Rubix lab fleet — Step 1: node install

Brings up a single Rubix node on one Ubuntu desktop, running in `localnet`
mode, as a standing systemd service on the fixed API port `20000`.

It is **install only** — it does not create a DID, set up quorum, peer this
node with any other, or run test operations. All of that comes later, driven
from the controller.

The same script runs on every desktop unchanged. The only per-fleet decision
is the bootstrap list, covered below.

The running node lives at `../nodes/<name>/` (`testnode` by default — override
with `NODE_NAME=foo ./setup.sh`). `../prerequisite/` is only the staging point;
`setup.sh` copies from there into `../nodes/<name>/` and that is what actually
runs. `../nodes/` is gitignored — binaries, generated config with real lab IPs,
and DID private keys do not belong in git.

## Before the first machine

`setup.sh` needs three files in `../prerequisite/`:

| File | Notes |
|---|---|
| `localnetswarm.key` | Already committed. Never regenerate it per machine. |
| `rubixgoplatform` | Committed, or `setup.sh` downloads the latest release on first run. |
| `ipfs` | Committed, or extracted from a committed `kubo_*.tar.gz`. |

See [../prerequisite/README.md](../prerequisite/README.md) for how to obtain
them and whether to commit them.

The binary used here is **not** about testing a particular branch — any current
release is fine, since this step just gets a node running. Testing a specific
branch is handled separately by [../exec-update/](../exec-update/) on
already-running nodes; you never rebuild or redistribute this to do that.

## Bring-up order matters once: fullnode first

`localnet_bootstrap_nodes` needs a peer ID to point at, which only exists
after that node has been started at least once. The fullnode is the seed —
a single stable, always-on machine makes a better permanent bootstrap point
than juggling several quorum peer IDs. So:

1. On the fullnode desktop only, run setup with an empty bootstrap list:
   ```
   LOCALNET_BOOTSTRAP_NODES='[]' ./setup.sh
   ```
2. Once it's up, get its peer ID:
   ```
   curl -s http://localhost:20000/rubix/v1/node/peer_id
   ```
3. On **every other desktop** — quorum-designated or not, it makes no
   difference to bootstrap order — point at the fullnode:
   ```
   LOCALNET_BOOTSTRAP_NODES='["/ip4/<fullnode-ip>/tcp/4002/p2p/<fullnode-peer-id>"]' ./setup.sh
   ```

After this, every desktop's `config.toml` is byte-for-byte identical except
for that shared bootstrap entry. Quorum selection itself (which pool
machines sign for which senders) is a Step 2 concern, decided per test
case — not a bring-up-time decision. See `../SETUP-RUNBOOK.md` Phase B3 and
`../LAB-QUICKREF.txt` for the exact copy-pasteable sequence.

## Running it

```
cd rubix-lab
chmod +x setup.sh
LOCALNET_BOOTSTRAP_NODES='[...]' ./setup.sh
```

Requires Docker installed and the user in the `docker` group, plus sudo
access for the systemd unit (the node directory itself, `../nodes/<name>/`,
is a plain subfolder of your own repo clone — no sudo needed for that part).
Safe to re-run: every step checks existing state first (config.toml,
the Postgres container, the systemd unit) rather than blindly redoing work.
Useful after a reboot if something didn't come back up on its own — though
normally systemd (`Restart=always`, enabled at boot) should handle that
without needing this script again.

## What it does

1. Checks Docker is installed and running, `envsubst` is available.
2. Copies the binary, `ipfs`, and the swarm key into `../nodes/<name>/`
   (working directory must contain both, per `core/ipfsport` — mirrors
   `test/docker/rubix/entrypoint.sh` in the main repo).
3. Generates `config.toml` from the template (localnet, DB on `localhost:5433`,
   the bootstrap list you passed in) — only if one doesn't already exist.
4. Starts a dedicated `postgres:18` container (own volume, `--restart always`),
   waits for it to be ready.
5. Clears any incomplete `.ipfs/` state left over from a previous failed run.
6. Runs `rubixgoplatform init` (harmless no-op if config.toml already exists).
7. Installs and starts a systemd service (`rubixgoplatform.service`) so the
   node survives reboots and crashes unattended.

## Verifying success

```
curl -s http://localhost:20000/rubix/v1/dids
journalctl -u rubixgoplatform -f
```

## What's deliberately NOT here

DID creation, quorum setup/grouping, the `registerdid` peering pass, token
minting, and all test operations + reporting — that's Step 2, a separate
controller-driven script that starts from "does this node have a DID yet"
and runs from a controller machine, not on the desktops themselves.
