# Rubix lab fleet — Step 1: node install

`setup.sh` brings up the Rubix nodes of one Ubuntu machine, each as a standing
systemd service with its own Postgres container. What a machine runs depends
on its role in `controller/hosts.txt`:

| Role | Nodes | Notes |
|---|---|---|
| pool (no role) | every line of [../node-layout.txt](../node-layout.txt) | 9 nodes, holding 1, 1, 1, 5, 5, 5, 5, 5 and 10 DIDs |
| `fullnode` | 1 | run with `-fullnode`, holds no DID |
| `faucet` | 1 | will hold the faucet DID |
| `faucet-quorum` | 1 | will hold the faucet quorum DID |

It is **install only** — it does not create DIDs, set up quorums, peer nodes
or run tests. That comes later, driven from the controller.

Normally you do not run it by hand: `controller/install-nodes.sh` copies
`systems/` to the machine and runs it over SSH with the right role and
bootstrap list. Running it on the machine yourself works the same.

## What one node looks like

Node *N* (its *slot*) on a machine, with `node_index` from the layout (0 for the
single-node roles):

| Thing | Value |
|---|---|
| Folder | `~/rubix-lab/nodes/node<N>/` — binary, `ipfs`, swarm key, `config.toml`, data, `log.txt` |
| systemd unit | `rubixgoplatform@<N>` (template [rubixgoplatform@.service](rubixgoplatform@.service)), `Restart=always`, enabled at boot |
| API port | 20000 + node_index |
| Swarm port | 4002 + node_index |
| Postgres | container `rubix-db-<N>`, volume `rubix-pgdata-<N>`, port 5433 + node_index |

The product derives every port from `node_index` (`core/config/config.go`),
so the layout's spacing of 100 gives node 3 the API port 20300, swarm 4302
and Postgres 5733. The cost of that spacing is written in the layout file.

**The node's folder is both its working directory and its `-p` profile.**
The product runs `./ipfs` from the working directory and, when it restarts
IPFS on its own, points `IPFS_PATH` at `./.ipfs` (`NodeConfigDir` is never
set, `core/ipfs_recovery.go:247`). Nine nodes sharing one working directory
would all restart onto the same IPFS repo.

`nodes/` is gitignored — binaries, generated config with real lab IPs, and
DID private keys do not belong in git. The repo must sit at `~/rubix-lab` on
every machine: the controller tools find every node at
`~/rubix-lab/nodes/node<N>`. `setup.sh` refuses to run from anywhere else.

## Network: testnet mode, the lab's own key

`config.toml` sets `network_mode = "testnet"` and the swarm key is copied in as
`testnetswarm.key` (it is `../prerequisite/localnetswarm.key`, same key). A
testnet node with a swarm key Rubix does not operate is a *custom testnet*
(`core/network_identity.go`): it checks minters against
`CustomNetAllowedMinters` in the build it runs.

## Bootstrap order: the first fullnode, then everything else

`testnet_bootstrap_nodes` needs peer IDs to point at, which exist only once a
node has started. The fullnodes are the seeds:

1. The first fullnode, with an empty list:
   `BOOTSTRAP_NODES='[]' ./setup.sh fullnode`
   (from the controller: `./install-nodes.sh --host 192.168.1.101 --first`)
2. Every other machine points at every fullnode that is already up — what
   `install-nodes.sh` builds from their APIs, e.g.
   `BOOTSTRAP_NODES='["/ip4/192.168.1.101/tcp/4002/p2p/12D3KooW..."]' ./setup.sh fullnode`

## Running it by hand

```
cd ~/rubix-lab/systems/install
BOOTSTRAP_NODES='[...]' ./setup.sh pool
SLOTS="0 1" BOOTSTRAP_NODES='[...]' ./setup.sh pool    # only some slots
```

Requires Docker Engine (user in the `docker` group) and sudo, which it asks
for once: for the systemd unit, the sudoers grant and — only if `ufw` is
active — the firewall rules.

Safe to re-run: `config.toml`, binaries and containers that exist are kept
(re-running never puts the starting binary back over a deployed one), only
what is missing is created, and running nodes are left running.

It refuses while the old single-node install is still on the machine
(container `node`/`rubix-node-db`, or a running `rubixgoplatform.service`) —
wipe that from the controller first: `wipe-node-db.sh --legacy`. Once that is
done it removes the old unit file itself.

## What it does

1. Checks Docker, `envsubst`, the swarm key and the two binaries.
2. Per node: creates the folder, copies the binaries (only if missing) and the
   swarm key, writes `config.toml` (only if missing; an existing one must have
   the expected `node_index` and testnet mode), clears an incomplete `.ipfs/`,
   and starts its Postgres container.
3. Waits for every Postgres to be ready.
4. Installs the template unit and a sudoers grant
   (`/etc/sudoers.d/rubix-lab-nodes`) letting the controller start, stop and
   restart exactly this machine's units without a password.
5. Opens each node's API, swarm and Postgres port if `ufw` is active.
6. Enables and starts every node's unit, then waits (up to ~2.5 minutes) for
   each API port and prints a line per node.

## Verifying

```
curl -s http://localhost:20300/rubix/v1/node/ping      # node 3
journalctl -u rubixgoplatform@3 -f
tail -f ~/rubix-lab/nodes/node3/log.txt
```
From the controller: `./preflight-check.sh` and `python3 check-nodes.py`.

## What's deliberately NOT here

DID creation (`controller/dids-to-excel.py`), quorum setup, peering, minting,
and all test operations — those run from the controller.
