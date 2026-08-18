# Rubix Lab — Environment Setup Runbook

Follow this in order. Each phase ends with a check — do not move on until it
passes. The goal of this runbook is to get from bare desktops to **"the
controller can reach every node"**, which is the foundation everything else
sits on.

Work with however many systems are available. The process is the same for 5
or 40; more can be added later by repeating Phase B and C for the new machines.

---

## Phase A — Plan roles and record addresses

Before touching any machine.

### A1. Assign roles

| Role | How many | Notes |
|---|---|---|
| Controller | 1 | Runs the test scripts. Can also run a node. Needs to reach all others. |
| Quorum | 3–5 | Sign transactions. Must stay funded at all times. |
| Participant | the rest | Ordinary sender/receiver nodes |
| Fullnode | 1 | Dedicated fullnode for this environment |
| Explorer | 1 | Can share a machine with the fullnode |

Quorum count can start at 3. It is easy to promote more participants to quorum
later; the tests that vary quorum count need at least 3 to be meaningful.

### A2. Fix the addresses

Give every machine a static IP or a DHCP reservation. Addresses must not change
between test cycles — the whole inventory is keyed on them.

Record in a spreadsheet: machine name, IP, assigned role, physical location.

### A3. Standardise access

- Same OS user on every machine: `Rubix`
- Same wallet password on every node (the controller needs it for every call)
- SSH key from the controller to every other machine

**Check:** you have a written list of every machine, its IP, and its role.

---

## Phase B — Install the node (per machine)

Repeat on each desktop. See `install/README.md` for detail.

### B1. Install Docker

**Use Docker Engine from the command line. Do not use Docker Desktop for Linux.**

Docker Desktop runs containers inside a VM and needs a logged-in desktop
session. These nodes must survive reboots with nobody logged in — with Docker
Desktop the database container would not come back until someone signed in.
Docker Engine runs as a system service and starts at boot.

**The whole install is four commands. Nothing else is needed.**

```
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker     # start now AND at every boot
sudo usermod -aG docker $USER          # use docker without sudo
```

Log out and back in so the group change takes effect.

Ubuntu's own `docker.io` package is enough. `setup.sh` uses plain `docker run`
— no Docker Compose — so the extra work of adding Docker's official apt
repository buys nothing here.

**`enable` is the important one.** The database container is created with
`--restart always`, but that only helps if the Docker service itself starts at
boot. Skip it and every reboot leaves the node with no database.

**Check:**
```
docker run --rm hello-world     # works without sudo
systemctl is-enabled docker     # prints: enabled
```
If `hello-world` needs sudo, the group change has not applied yet — log out and
back in.

> **Do not purge and reinstall Docker to "start clean".** Stopping
> `docker.socket` breaks the daemon: Docker runs as `dockerd -H fd://`, which
> means systemd hands it a pre-opened socket from `docker.socket`. With that
> socket stopped, `dockerd` exits immediately with
> `failed to load listeners: no sockets found via socket activation`, retries,
> and then systemd rate-limits it with `Start request repeated too quickly`.
>
> If you hit that state, recover with:
> ```
> sudo systemctl reset-failed docker.service docker.socket
> sudo systemctl start docker.socket
> sudo systemctl start docker
> ```
> `reset-failed` must come first, or systemd refuses to start the unit at all.
>
> Also harmless and safe to ignore: `Deleting nftables IPv4/IPv6 rules
> error="exit status 1"` at startup. Docker logs it at info level and starts
> normally afterwards.

See [LAB-QUICKREF.txt](LAB-QUICKREF.txt) for the copy-pasteable Docker install
and node commands in one place.

### B2. Get the repo onto the machine

```
git clone <rubix-lab repo> ~/rubix-lab
```

Use the **same path on every machine** (`~/rubix-lab`) — `exec-update` depends
on that being consistent.

`prerequisite/` must hold the node binary, the IPFS binary and the swarm key.
The key is already committed. For the two binaries, either commit them once or
let `setup.sh` fetch them on first run — see `prerequisite/README.md`.

### B3. Bring up the quorum machines first

The bootstrap list needs each quorum node's peer ID, which only exists after
that node has started once. So quorum machines go first, with an empty list:

```
cd ~/rubix-lab/install
LOCALNET_BOOTSTRAP_NODES='[]' ./setup.sh
```

Then collect each one's peer ID:

```
curl -s http://localhost:20000/rubix/v1/node/peer_id
```

Build the shared bootstrap list from those:

```
["/ip4/<quorum-ip-1>/tcp/4001/p2p/<peer-id-1>", "/ip4/<quorum-ip-2>/tcp/4001/p2p/<peer-id-2>"]
```

### B4. Bring up everything else

On every remaining machine, using the list from B3:

```
LOCALNET_BOOTSTRAP_NODES='["/ip4/...","/ip4/..."]' ./setup.sh
```

**Check on each machine, locally:**
```
curl -s http://localhost:20000/rubix/v1/node/ping
journalctl -u rubixgoplatform -f
```

---

## Phase C — Prove the controller can reach every node

This is the phase that matters most, and the most common place to get stuck.
A node working locally does **not** mean the controller can reach it.

### C1. Open the firewall on each node

The node API and the swarm port must accept connections from the rest of the
lab network:

```
sudo ufw allow 20000/tcp
sudo ufw allow 4001/tcp
sudo ufw status
```

If `ufw` is inactive, nothing is blocked and there is nothing to do.

### C2. Check network reachability between machines

Office networks sometimes isolate clients from each other, which silently
blocks machine-to-machine traffic even though everything looks connected.
Test one node from the controller before assuming the whole fleet is fine:

```
curl -s http://<node-ip>:20000/rubix/v1/node/ping
```

If that fails but the same command works on the node itself, the problem is
network isolation or firewall, not Rubix.

### C3. Run the reachability check

On the controller:

```
cd controller
cp hosts.txt.example hosts.txt      # then fill in your real IPs and roles
python3 check-nodes.py
```

Output looks like:

```
HOST         ROLE          STATUS     DIDs       RBT  NOTES
10.0.0.11    quorum        OK            1     1000
10.0.0.12    quorum        OK            1     1000
10.0.0.20    participant   NO DID        0        -  reachable but no DID yet - needs bootstrap
10.0.0.21    participant   DOWN          0        -  unreachable (timed out)

Reachable : 3/4
Ready     : 2/4   (reachable and has a DID)
Down      : 10.0.0.21
No DID    : 10.0.0.20
```

It needs no extra packages — standard library only. It also writes
`inventory.json`, which later scripts will read.

**Check:** every machine shows `Reachable`. `NO DID` is expected at this stage
and is fixed in Phase D. Anything showing `DOWN` must be resolved before
continuing — the node is not running, the firewall is closed, or the network
blocks it.

---

## Phase D — Identity, quorum and peering

### D1. Create a DID on each node

One per node, once ever. A node that already has a DID must be left alone —
creating a second one gives that machine two identities and no error is raised.

Use the same wallet password everywhere.

### D2. Set up the quorum nodes

On each of the 3–5 designated machines, run the quorum setup so they can sign.

### D3. Register quorums on every participant

Every participant must know which DIDs are allowed to sign.

**Important:** a node always uses the **first** quorum it registered. To spread
load, different groups of participants should register a different quorum first.

**Verify this actually held** — the underlying query has no fixed sort order, so
the "first" quorum is not guaranteed stable. Check which quorum really signs a
transfer rather than assuming it followed registration order. If it drifts, the
grouping strategy needs revisiting (see `test-plan/README.md`).

### D4. Fund the quorums

Mint local RBT so every quorum sits **well above 1000 RBT**.

This is not optional. A quorum has to pledge at least the value of every
transfer it signs. An underfunded quorum fails transfers for lack of collateral
and hides the behaviour being tested. Minting is free on localnet — there is no
reason to run close to the line.

### D5. Announce DIDs so nodes learn about each other

With all nodes online together, run the DID registration announcement on each
node so every node learns the others' DID-to-peer mapping.

This only reaches nodes that are online at that moment. If a machine was down,
re-run this after it comes back, or it will not be reachable for transfers.

**Check:** re-run `python3 check-nodes.py`. Every node should now show `OK` with
at least 1 DID, and quorum nodes should show a healthy RBT balance.

---

## Phase E — Smoke test

Before trusting the environment for real test cycles.

1. Mint some RBT on two participant nodes
2. Send 1 RBT from one to the other
3. Confirm both balances changed correctly
4. Confirm the transaction appears on both nodes
5. Confirm the fullnode and explorer show it

If this works, the environment is ready. If it fails, the problem is in setup,
not in the release being tested — fix it here rather than starting a test cycle.

**Check:** one transfer completes end to end and is visible everywhere it should be.

---

## Phase F — Record the baseline

Once the smoke test passes, write down:

- Which nodes exist, their roles, and their DIDs (`inventory.json` has this)
- Which version each node is running
- Quorum funding levels
- Time taken for that first single transfer, with nothing else running

That last one becomes the baseline every later performance number is compared
against.

---

## Routine checks

Run before every test cycle:

```
python3 check-nodes.py
```

- Any node `DOWN` → excluded from that cycle; investigate before the next one
- Any node `NO DID` → needs Phase D before it can take part
- Any quorum low on RBT → top up before starting

Run after any desktop reboot or power cut, since that is the most common cause
of a node dropping out.

---

## Common problems

| Symptom | Likely cause |
|---|---|
| `failed to load listeners: no sockets found via socket activation` | `docker.socket` was stopped. See the box in B1 — `reset-failed`, then start the socket, then the service. |
| `Start request repeated too quickly` | systemd rate-limited the unit after repeated failures. `sudo systemctl reset-failed docker.service` first, then fix the underlying cause. |
| `Deleting nftables IPv4 rules error="exit status 1"` | Harmless. Info-level, Docker starts fine after it. |
| Works locally, not from controller | Firewall on the node, or network client isolation |
| Node not starting after reboot | Service not enabled at boot, or Docker not started |
| Transfers fail with pledge errors | Quorum out of funds — top up, the run is invalid |
| Node cannot find peers | Wrong or missing swarm key, or bootstrap list not set |
| Transfers fail after adding a new node | DID announcement not re-run while all nodes were online |
| Everything slow through one node | All senders sharing the same primary quorum |
