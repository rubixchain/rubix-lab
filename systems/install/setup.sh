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
#   ~/Desktop/rubix/rubix-lab/nodes/node<N>/   binary, ipfs, swarm key,
#                                config.toml and data. The folder is both the
#                                working directory and the -p profile (see
#                                rubixgoplatform@.service).
#   Postgres container rubix-db-<N> on port 5433 + node_index, own volume
#   rubix-pgdata-<N>, --restart always
#   systemd unit rubixgoplatform@<N>, Restart=always, enabled at boot
#
# Network: Rubix's testnet setup on the lab's own swarm key - testnet mode,
# ../prerequisite/testnetswarm.key, and a fixed bootstrap list of the lab's
# fullnodes (../bootstrap-nodes.txt), the way the default testnet ships a fixed
# list. A swarm key Rubix does not operate makes it a custom testnet, which
# checks minters against CustomNetAllowedMinters (core/network_identity.go).
#
# Safe to re-run: config.toml, containers and binaries that exist are kept
# (exec-update swaps binaries in place, and a re-run must not undo that); only
# what is missing is created, and an existing config.toml gets the current
# bootstrap list. Running nodes are left running - restart them to apply it.
#
# Usage:
#   ./setup.sh pool                    # every node in ../node-layout.txt
#   ./setup.sh fullnode
#   SLOTS="0 1" ./setup.sh pool        # just these slots
#   BOOTSTRAP_NODES='[...]' ./setup.sh pool   # override ../bootstrap-nodes.txt
#
# Does NOT create DIDs, set up quorums or fund anything - that is the
# controller's job (dids-to-excel.py, test_runner.py).

set -euo pipefail

ROLE="${1:-}"
case "$ROLE" in
  pool|fullnode|faucet|faucet-quorum) ;;
  *) echo "Usage: $0 pool|fullnode|faucet|faucet-quorum"; exit 1 ;;
esac

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # systems/install
SYSTEMS_DIR="$(cd "$BUNDLE_DIR/.." && pwd)"                  # systems/
REPO_ROOT="$(cd "$SYSTEMS_DIR/.." && pwd)"                   # ~/Desktop/rubix/rubix-lab
PREREQ_DIR="$SYSTEMS_DIR/prerequisite"
LAYOUT="$SYSTEMS_DIR/node-layout.txt"
# nodes/ stays at the REPO ROOT, not under systems/: it is gitignored runtime
# state (binaries, config with lab IPs, DID keys), and the controller tools
# find every node at ~/Desktop/rubix/rubix-lab/nodes/node<slot>.
NODES_DIR="$REPO_ROOT/nodes"
KUBO_VERSION="${KUBO_VERSION:-v0.19.1}"
API_BASE=20000      # constants.RubixServerPort
SWARM_BASE=4002     # constants.SwarmPort
DB_BASE=5433        # constants.PostgresBasePort
EXTRA_ARGS=""
[ "$ROLE" = "fullnode" ] && EXTRA_ARGS=" -fullnode"

echo "== Rubix lab node setup: $ROLE =="

if [ "$REPO_ROOT" != "$HOME/Desktop/rubix/rubix-lab" ]; then
  echo "ERROR: this copy of the repo is at $REPO_ROOT. Every controller tool looks for"
  echo "       the nodes under ~/Desktop/rubix/rubix-lab/nodes - put the repo there."
  exit 1
fi
# Until 2026-10-16 the nodes lived in ~/rubix-lab/nodes. Installing here while
# they are still there would create empty node folders next to their existing
# databases - DIDs in Postgres whose keys stayed behind in the old folder.
if compgen -G "$HOME/rubix-lab/nodes/node[0-9]*" >/dev/null; then
  echo "ERROR: this machine still has its nodes in ~/rubix-lab/nodes. Move them first,"
  echo "       from the controller: ./move-nodes-to-desktop.sh --host <this machine>"
  exit 1
fi

# The bootstrap list: every peer in ../bootstrap-nodes.txt except this
# machine's own (a fullnode does not dial itself), as the JSON list
# config.toml takes. BOOTSTRAP_NODES in the environment overrides the file.
if [ -z "${BOOTSTRAP_NODES+set}" ]; then
  own_ips=" $(hostname -I 2>/dev/null) "
  entries=()
  if [ -f "$SYSTEMS_DIR/bootstrap-nodes.txt" ]; then
    while read -r addr; do
      [ -n "$addr" ] || continue
      ip="$(sed -n 's#^/ip4/\([^/]*\)/.*#\1#p' <<<"$addr")"
      case "$own_ips" in *" $ip "*) continue ;; esac
      entries+=("\"$addr\"")
    done < <(sed 's/#.*//; s/[[:space:]]//g' "$SYSTEMS_DIR/bootstrap-nodes.txt")
  fi
  BOOTSTRAP_NODES="[$(IFS=,; echo "${entries[*]-}")]"
fi
case "$BOOTSTRAP_NODES" in
  \[*\]) ;;
  *) echo "ERROR: BOOTSTRAP_NODES must be a list in brackets, e.g. '[\"/ip4/...\"]'"; exit 1 ;;
esac
echo "Bootstrap peers: $BOOTSTRAP_NODES"

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

[ -f "$PREREQ_DIR/testnetswarm.key" ] || { echo "ERROR: prerequisite/testnetswarm.key missing. This is the shared key that makes all lab nodes one private network — it must come from the repo, never be regenerated per machine."; exit 1; }

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
  # (core/ipfs.go) - the lab's key, under the name Rubix's testnet uses.
  cp "$PREREQ_DIR/testnetswarm.key" "$NODE_DIR/testnetswarm.key"

  CFG="$NODE_DIR/config.toml"
  if [ -f "$CFG" ]; then
    have_index="$(awk -F= '/^node_index/ {gsub(/[ \t]/, "", $2); print $2; exit}' "$CFG")"
    have_mode="$(awk -F'"' '/^network_mode/ {print $2; exit}' "$CFG")"
    if [ "$have_index" != "$INDEX" ] || [ "$have_mode" != "testnet" ]; then
      echo "ERROR: $CFG has node_index=$have_index network_mode=$have_mode, expected"
      echo "       $INDEX / testnet. Refusing to touch it - check by hand."
      exit 1
    fi
    # The bootstrap list is fleet-wide (../bootstrap-nodes.txt), so an existing
    # config follows it; everything else in the file is left alone.
    want_line="testnet_bootstrap_nodes = $BOOTSTRAP_NODES"
    if [ "$(grep -m1 '^testnet_bootstrap_nodes' "$CFG")" != "$want_line" ]; then
      awk -v line="$want_line" '/^testnet_bootstrap_nodes/ {print line; next} {print}' "$CFG" > "$CFG.new"
      mv "$CFG.new" "$CFG"
      echo "   config.toml present, bootstrap list updated (restart the node to use it)"
    else
      echo "   config.toml present, kept"
    fi
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

  # Another container already publishing this port is an old install the
  # legacy wipe did not know by name (it removes only `node`/`rubix-node-db`).
  holder="$(docker ps --format '{{.Names}} {{.Ports}}' | grep -E ":${DB_PORT}->" | awk '{print $1}' | grep -vx "$CONTAINER" | head -1 || true)"
  if [ -n "$holder" ]; then
    echo "ERROR: port $DB_PORT is already taken by container '$holder' - an old install?"
    echo "       If it is old:  docker rm -f $holder   (and its volume: docker inspect $holder)"
    echo "       Then run this again."
    exit 1
  fi
  if [ -n "$(docker ps -aq -f "name=^${CONTAINER}$")" ]; then
    if ! docker start "$CONTAINER" >/dev/null; then
      echo "ERROR: Postgres container $CONTAINER exists but will not start (message above)."
      echo "       If it was left by a failed run:  docker rm -f $CONTAINER   then run this again."
      exit 1
    fi
    echo "   Postgres container $CONTAINER present, kept"
  else
    if ! docker run --name "$CONTAINER" \
      -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
      -p "${DB_PORT}:5432" \
      -v "rubix-pgdata-${SLOT}:/var/lib/postgresql" \
      --restart always \
      -d postgres:18 >/dev/null; then
      # docker run creates the container before it fails to start it; remove
      # it, or a re-run would find it and wait for a database that never comes.
      docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
      echo "ERROR: could not start $CONTAINER on port $DB_PORT (message above)."
      exit 1
    fi
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

# The node console (../console): this machine's nodes and their live logs,
# opened on its screen at every login. User-level, no sudo.
if [ -f "$SYSTEMS_DIR/console/rubix-console.sh" ]; then
  chmod +x "$SYSTEMS_DIR/console/rubix-console.sh"
  "$SYSTEMS_DIR/console/rubix-console.sh" --install || true
fi

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
  echo "or the node's own log:                    ~/Desktop/rubix/rubix-lab/nodes/node<slot>/log.txt"
  exit 1
fi
echo ""
echo "Peer ID of node 0 (for a bootstrap list):  curl -s http://localhost:$API_BASE/rubix/v1/node/peer_id"
