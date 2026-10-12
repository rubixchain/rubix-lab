#!/usr/bin/env bash
# Rubix lab - bring up the nodes of ONE machine. Step 1, once per machine.
# controller/install-nodes.sh copies systems/ here and runs this over SSH;
# running it by hand on the machine works the same.
#
# The machine's role (the same roles as controller/hosts.txt) decides what runs:
#   pool           every node in ../node-layout.txt
#   fullnode       one node, run with -fullnode
#   faucet         one node - it will hold the faucet DID
#   faucet-quorum  one node - it will hold the faucet quorum DID
#
# Per node (slot N; node_index from the layout, 0 for the single-node roles):
#   ~/rubix-lab/nodes/node<N>/   binary, ipfs, swarm key, config.toml and data.
#                                The folder is both the working directory and
#                                the -p profile (see rubixgoplatform@.service).
#   Postgres container rubix-db-<N> on port 5433 + node_index, own volume
#   rubix-pgdata-<N>, --restart always
#   systemd unit rubixgoplatform@<N>, Restart=always, enabled at boot
#
# Network: testnet mode with the lab's own swarm key - a custom testnet, which
# checks minters against CustomNetAllowedMinters (core/network_identity.go).
#
# Safe to re-run: config.toml, containers and binaries that exist are kept
# (exec-update swaps binaries in place, and a re-run must not undo that); only
# what is missing is created. Running nodes are left running.
#
# Usage:
#   BOOTSTRAP_NODES='[]' ./setup.sh fullnode         # the FIRST fullnode only
#   BOOTSTRAP_NODES='["/ip4/192.168.1.101/tcp/4002/p2p/12D3KooW..."]' ./setup.sh fullnode
#   BOOTSTRAP_NODES='[<every fullnode>]' ./setup.sh pool
#   SLOTS="0 1" BOOTSTRAP_NODES='[...]' ./setup.sh pool   # just these slots
#
# Does NOT create DIDs, set up quorums or fund anything - that is the
# controller's job (dids-to-excel.py, test_runner.py).

set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in
  pool|fullnode|faucet|faucet-quorum) ;;
  *) echo "Usage: BOOTSTRAP_NODES='[...]' $0 pool|fullnode|faucet|faucet-quorum"; exit 1 ;;
esac

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # systems/install
SYSTEMS_DIR="$(cd "$BUNDLE_DIR/.." && pwd)"                  # systems/
REPO_ROOT="$(cd "$SYSTEMS_DIR/.." && pwd)"                   # ~/rubix-lab
PREREQ_DIR="$SYSTEMS_DIR/prerequisite"
LAYOUT="$SYSTEMS_DIR/node-layout.txt"
# nodes/ stays at the REPO ROOT, not under systems/: it is gitignored runtime
# state (binaries, config with lab IPs, DID keys), and the controller tools
# find every node at ~/rubix-lab/nodes/node<slot>.
NODES_DIR="$REPO_ROOT/nodes"
KUBO_VERSION="${KUBO_VERSION:-v0.19.1}"
API_BASE=20000      # constants.RubixServerPort
SWARM_BASE=4002     # constants.SwarmPort
DB_BASE=5433        # constants.PostgresBasePort
EXTRA_ARGS=""
[ "$ROLE" = "fullnode" ] && EXTRA_ARGS=" -fullnode"

echo "== Rubix lab node setup: $ROLE =="

if [ "$REPO_ROOT" != "$HOME/rubix-lab" ]; then
  echo "ERROR: this copy of the repo is at $REPO_ROOT. Every controller tool looks for"
  echo "       the nodes under ~/rubix-lab/nodes - put the repo at ~/rubix-lab."
  exit 1
fi

# The lab fullnodes this machine joins through. Required on purpose: an empty
# list is right only for the very first fullnode, and a node that joins
# nothing looks healthy locally while the rest of the lab cannot see it.
if [ -z "${BOOTSTRAP_NODES+set}" ]; then
  echo "ERROR: set BOOTSTRAP_NODES - the lab fullnodes this machine joins through, e.g."
  echo "  BOOTSTRAP_NODES='[\"/ip4/192.168.1.101/tcp/4002/p2p/12D3KooW...\"]' $0 $ROLE"
  echo "Only the first fullnode uses '[]'. controller/install-nodes.sh fills it in for you."
  exit 1
fi
case "$BOOTSTRAP_NODES" in
  \[*\]) ;;
  *) echo "ERROR: BOOTSTRAP_NODES must be a list in brackets, e.g. '[\"/ip4/...\"]'"; exit 1 ;;
esac

# ---------------------------------------------------------------------------
# Which nodes: "slot node_index" per line
# ---------------------------------------------------------------------------
if [ "$ROLE" = "pool" ]; then
  [ -f "$LAYOUT" ] || { echo "ERROR: $LAYOUT is missing."; exit 1; }
  mapfile -t ALL_ROWS < <(sed 's/#.*//' "$LAYOUT" | awk 'NF == 3 {print $1, $2}')
else
  ALL_ROWS=("0 0")
fi
[ "${#ALL_ROWS[@]}" -gt 0 ] || { echo "ERROR: no nodes in $LAYOUT."; exit 1; }

ROWS=()
if [ -n "${SLOTS:-}" ]; then
  for want in $SLOTS; do
    found=""
    for row in "${ALL_ROWS[@]}"; do
      [ "${row%% *}" = "$want" ] && { ROWS+=("$row"); found=1; }
    done
    [ -n "$found" ] || { echo "ERROR: slot $want is not one of this role's nodes."; exit 1; }
  done
else
  ROWS=("${ALL_ROWS[@]}")
fi

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------
command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker is not installed."; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: Docker daemon not reachable (is it running? are you in the docker group?)."; exit 1; }

if ! command -v envsubst >/dev/null 2>&1; then
  echo "envsubst not found, installing gettext-base..."
  sudo apt-get update -y && sudo apt-get install -y gettext-base
fi

[ -f "$PREREQ_DIR/localnetswarm.key" ] || { echo "ERROR: prerequisite/localnetswarm.key missing. This is the shared key that makes all lab nodes one private network — it must come from the repo, never be regenerated per machine."; exit 1; }

# --- rubixgoplatform binary: use the committed one, else download a release ---
# Either way it is only the starting binary: the build under test arrives with
# controller/exec-update/update-exec.sh.
if [ ! -f "$PREREQ_DIR/rubixgoplatform" ]; then
  echo "prerequisite/rubixgoplatform not found — downloading the latest release..."
  command -v curl >/dev/null 2>&1 || { echo "ERROR: curl needed to download the release. Install curl, or commit the binary to prerequisite/."; exit 1; }
  VERSION="$(curl -fsSL https://api.github.com/repos/rubixchain/rubixgoplatform/releases/latest | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)"
  [ -n "$VERSION" ] || { echo "ERROR: could not determine the latest release. Check network access to GitHub, or commit the binary to prerequisite/."; exit 1; }
  echo "Latest release: $VERSION"
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/rubix.tar.gz" \
    "https://github.com/rubixchain/rubixgoplatform/releases/download/${VERSION}/rubixgoplatform-${VERSION}-linux-amd64.tar.gz" \
    || { echo "ERROR: download failed."; rm -rf "$TMP"; exit 1; }
  tar -xzf "$TMP/rubix.tar.gz" -C "$TMP"
  cp "$TMP/rubixgoplatform" "$PREREQ_DIR/rubixgoplatform"
  rm -rf "$TMP"
  echo "Saved to prerequisite/rubixgoplatform ($VERSION)"
fi

# --- ipfs binary: use the committed one, else extract the committed tarball ---
if [ ! -f "$PREREQ_DIR/ipfs" ]; then
  KUBO_TARBALL="$PREREQ_DIR/kubo_${KUBO_VERSION}_linux-amd64.tar.gz"
  if [ -f "$KUBO_TARBALL" ]; then
    echo "Extracting ipfs from $(basename "$KUBO_TARBALL")..."
    TMP="$(mktemp -d)"
    tar -xzf "$KUBO_TARBALL" -C "$TMP"
    cp "$TMP/kubo/ipfs" "$PREREQ_DIR/ipfs"
    rm -rf "$TMP"
  else
    echo "ERROR: neither prerequisite/ipfs nor $(basename "$KUBO_TARBALL") found."
    echo "Add one of them to prerequisite/. See prerequisite/README.md."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# The single-node install this layout replaces (hand-built in ~/Desktop/rubix,
# or an older setup.sh) holds port 20000 and Postgres 5433. Its data has to be
# wiped from the controller first (wipe-node-db.sh --legacy) - a deliberate,
# confirmed step. This script never deletes data; it only removes the old
# unit once nothing is running under it.
# ---------------------------------------------------------------------------
for old in node rubix-node-db; do
  if [ -n "$(docker ps -aq -f "name=^${old}$")" ]; then
    echo "ERROR: the old single-node Postgres container '$old' is still here. Wipe the old"
    echo "       install first, from the controller:"
    echo "         ./wipe-node-db.sh --legacy --host <this machine's IP> --yes"
    exit 1
  fi
done
if [ -f /etc/systemd/system/rubixgoplatform.service ]; then
  if systemctl is-active --quiet rubixgoplatform; then
    echo "ERROR: the old single-node service (rubixgoplatform.service) is still running."
    echo "       Wipe the old install from the controller first (wipe-node-db.sh --legacy)."
    exit 1
  fi
  echo "Removing the old single-node unit rubixgoplatform.service (its data is already wiped)..."
  sudo systemctl disable rubixgoplatform >/dev/null 2>&1 || true
  sudo rm -f /etc/systemd/system/rubixgoplatform.service
  sudo systemctl daemon-reload
fi

# ---------------------------------------------------------------------------
# Per node: folder, binaries, swarm key, config.toml, Postgres
# ---------------------------------------------------------------------------
for row in "${ROWS[@]}"; do
  read -r SLOT INDEX <<<"$row"
  NODE_DIR="$NODES_DIR/node$SLOT"
  API_PORT=$((API_BASE + INDEX)); DB_PORT=$((DB_BASE + INDEX)); SWARM_PORT=$((SWARM_BASE + INDEX))
  CONTAINER="rubix-db-$SLOT"
  echo "-- node $SLOT: node_index $INDEX, API $API_PORT, swarm $SWARM_PORT, Postgres $DB_PORT"

  mkdir -p "$NODE_DIR"
  # Copied only when missing: after the first install exec-update owns the
  # binary, and a re-run must not put the old one back.
  [ -f "$NODE_DIR/rubixgoplatform" ] || cp "$PREREQ_DIR/rubixgoplatform" "$NODE_DIR/rubixgoplatform"
  [ -f "$NODE_DIR/ipfs" ] || cp "$PREREQ_DIR/ipfs" "$NODE_DIR/ipfs"
  chmod +x "$NODE_DIR/rubixgoplatform" "$NODE_DIR/ipfs"
  # In testnet mode the node copies ./testnetswarm.key into its IPFS repo
  # (core/ipfs.go). Same key as prerequisite/localnetswarm.key - only the name
  # the product looks for differs.
  cp "$PREREQ_DIR/localnetswarm.key" "$NODE_DIR/testnetswarm.key"

  CFG="$NODE_DIR/config.toml"
  if [ -f "$CFG" ]; then
    have_index="$(awk -F= '/^node_index/ {gsub(/[ \t]/, "", $2); print $2; exit}' "$CFG")"
    have_mode="$(awk -F'"' '/^network_mode/ {print $2; exit}' "$CFG")"
    if [ "$have_index" != "$INDEX" ] || [ "$have_mode" != "testnet" ]; then
      echo "ERROR: $CFG has node_index=$have_index network_mode=$have_mode, expected"
      echo "       $INDEX / testnet. Refusing to touch it - check by hand."
      exit 1
    fi
    echo "   config.toml present, kept"
  else
    NODE_INDEX="$INDEX" DB_PORT="$DB_PORT" BOOTSTRAP_NODES="$BOOTSTRAP_NODES" \
      envsubst '${NODE_INDEX} ${DB_PORT} ${BOOTSTRAP_NODES}' \
      < "$BUNDLE_DIR/config.toml.template" > "$CFG"
    echo "   config.toml written"
  fi

  # A .ipfs/ left by a failed first start (no config inside) makes the node
  # skip IPFS init and fail with a confusing "config: no such file".
  if [ -d "$NODE_DIR/.ipfs" ] && [ ! -f "$NODE_DIR/.ipfs/config" ]; then
    echo "   WARNING: incomplete .ipfs/ found, clearing it so the node can re-init cleanly"
    rm -rf "$NODE_DIR/.ipfs"
  fi

  if [ -n "$(docker ps -aq -f "name=^${CONTAINER}$")" ]; then
    docker start "$CONTAINER" >/dev/null 2>&1 || true
    echo "   Postgres container $CONTAINER present, kept"
  else
    docker run --name "$CONTAINER" \
      -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
      -p "${DB_PORT}:5432" \
      -v "rubix-pgdata-${SLOT}:/var/lib/postgresql" \
      --restart always \
      -d postgres:18 >/dev/null
    echo "   Postgres container $CONTAINER started on $DB_PORT"
  fi
done

echo "Waiting for Postgres..."
for row in "${ROWS[@]}"; do
  read -r SLOT INDEX <<<"$row"
  ready=0
  for _ in $(seq 1 30); do
    if docker exec "rubix-db-$SLOT" pg_isready -U rubix >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  [ "$ready" -eq 1 ] || { echo "ERROR: Postgres rubix-db-$SLOT not ready after 60s."; exit 1; }
done
echo "Postgres ready."

# ---------------------------------------------------------------------------
# systemd: one template unit, one instance per slot
# ---------------------------------------------------------------------------
sed -e "s#{{NODES_DIR}}#$NODES_DIR#g" -e "s#{{USER}}#$(whoami)#g" -e "s#{{EXTRA_ARGS}}#$EXTRA_ARGS#g" \
  "$BUNDLE_DIR/rubixgoplatform@.service" | sudo tee /etc/systemd/system/rubixgoplatform@.service >/dev/null
sudo systemctl daemon-reload

# Passwordless sudo for exactly the start/stop/restart calls the controller
# makes on this machine's units (restart-nodes.sh, update-exec.sh,
# wipe-node-db.sh). One line per unit, no wildcards: in sudoers a * also
# matches extra arguments, so "restart rubixgoplatform@*" would allow
# "restart rubixgoplatform@0 sshd". Covers every slot of the role, not only
# the ones in SLOTS, so a later run for the other slots finds it complete.
SYSTEMCTL="$(command -v systemctl)"
GRANT="$(mktemp)"
{
  echo "# Written by rubix-lab systems/install/setup.sh ($ROLE)."
  for row in "${ALL_ROWS[@]}"; do
    for verb in start stop restart; do
      echo "$(whoami) ALL=(root) NOPASSWD: $SYSTEMCTL $verb rubixgoplatform@${row%% *}"
    done
  done
} > "$GRANT"
if sudo visudo -cf "$GRANT" >/dev/null; then
  sudo install -m 0440 "$GRANT" /etc/sudoers.d/rubix-lab-nodes
else
  echo "WARNING: the sudoers grant did not validate - not installed. The controller's"
  echo "         restart/update tools will need a password on this machine."
fi
rm -f "$GRANT"

# The firewall only matters when ufw is on. The controller needs each node's
# API and Postgres port, every other node needs its swarm port.
if command -v ufw >/dev/null 2>&1 && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "ufw is active - opening each node's API, swarm and Postgres port..."
  for row in "${ROWS[@]}"; do
    read -r SLOT INDEX <<<"$row"
    for port in $((API_BASE + INDEX)) $((SWARM_BASE + INDEX)) $((DB_BASE + INDEX)); do
      sudo ufw allow "$port/tcp" >/dev/null
    done
  done
fi

for row in "${ROWS[@]}"; do
  read -r SLOT INDEX <<<"$row"
  # Starts a node that is not running; leaves a running one alone.
  sudo systemctl enable --now "rubixgoplatform@$SLOT"
done

# ---------------------------------------------------------------------------
# Check: every node's API port accepts connections. A first start initialises
# IPFS and the database, so it is slower than a restart (~15s).
# ---------------------------------------------------------------------------
echo "Waiting for the nodes' APIs (a first start takes longer than a restart)..."
listening() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
deadline=$((SECONDS + 150))
pending=()
for row in "${ROWS[@]}"; do pending+=("$row"); done
while [ "${#pending[@]}" -gt 0 ] && [ "$SECONDS" -lt "$deadline" ]; do
  still=()
  for row in "${pending[@]}"; do
    read -r SLOT INDEX <<<"$row"
    listening $((API_BASE + INDEX)) || still+=("$row")
  done
  pending=("${still[@]}")
  [ "${#pending[@]}" -gt 0 ] && sleep 3
done

echo ""
echo "== Done: $ROLE =="
FAILED=0
for row in "${ROWS[@]}"; do
  read -r SLOT INDEX <<<"$row"
  state="up"
  for p in "${pending[@]}"; do [ "$p" = "$row" ] && state="NOT ANSWERING"; done
  [ "$state" = "up" ] || FAILED=$((FAILED + 1))
  printf '  node %s  port %-5s  %s\n' "$SLOT" "$((API_BASE + INDEX))" "$state"
done
if [ "$FAILED" -gt 0 ]; then
  echo ""
  echo "$FAILED node(s) not answering yet. Logs:  journalctl -u rubixgoplatform@<slot> -n 100"
  echo "or the node's own log:                    ~/rubix-lab/nodes/node<slot>/log.txt"
  exit 1
fi
echo ""
echo "Peer ID of node 0 (for a bootstrap list):  curl -s http://localhost:$API_BASE/rubix/v1/node/peer_id"
