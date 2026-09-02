#!/usr/bin/env bash
# Rubix lab-fleet node bring-up — Step 1, run once per desktop.
#
# Safe to re-run: every step checks for existing state before acting, so
# running this again on an already-initialized desktop (e.g. after a reboot
# where the systemd service failed to come up) is a no-op except for
# whatever actually needs fixing.
#
# Usage:
#   LOCALNET_BOOTSTRAP_NODES='[]' ./setup.sh
#   LOCALNET_BOOTSTRAP_NODES='["/ip4/10.0.0.11/tcp/4002/p2p/12D3Koo...", "/ip4/10.0.0.12/tcp/4002/p2p/12D3Koo..."]' ./setup.sh
#
# See README.md for why LOCALNET_BOOTSTRAP_NODES must be empty on the first
# few (quorum-designated) desktops and filled in on every desktop after that.

set -euo pipefail

BUNDLE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BUNDLE_DIR/.." && pwd)"
PREREQ_DIR="$REPO_ROOT/prerequisite"
# Node name — placeholder "testnode" for now; override with NODE_NAME=foo ./setup.sh
# if you ever need more than one distinguishable node folder.
NODE_NAME="${NODE_NAME:-testnode}"
NODE_DIR="$REPO_ROOT/nodes/$NODE_NAME"
DB_CONTAINER="rubix-node-db"
DB_PORT=5433
KUBO_VERSION="${KUBO_VERSION:-v0.19.1}"

echo "== Rubix lab node setup =="
echo "Prerequisites: $PREREQ_DIR"
echo "Node dir: $NODE_DIR"

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
# Node directory + binaries + swarm key
#
# Lives inside the repo clone (rubix-lab/nodes/<name>), not /opt — no sudo
# needed here, this is just a subfolder of whatever the user already owns.
# prerequisite/ is only ever the staging point for bringing up a NEW node;
# the actual running copy lives under nodes/, and exec-update updates that
# copy in place, never prerequisite/.
# ---------------------------------------------------------------------------
mkdir -p "$NODE_DIR"

cp "$PREREQ_DIR/rubixgoplatform" "$NODE_DIR/rubixgoplatform"
cp "$PREREQ_DIR/ipfs" "$NODE_DIR/ipfs"
# CRITICAL: rubixgoplatform reads ./ipfs and ./localnetswarm.key as relative
# paths from its working directory — both must live in $NODE_DIR, which is
# also the systemd unit's WorkingDirectory. See test/docker/rubix/entrypoint.sh
# in the main repo for the reference implementation this mirrors.
cp "$PREREQ_DIR/localnetswarm.key" "$NODE_DIR/localnetswarm.key"
chmod +x "$NODE_DIR/rubixgoplatform" "$NODE_DIR/ipfs"

# ---------------------------------------------------------------------------
# config.toml — generated once, never overwritten after that
# ---------------------------------------------------------------------------
LOCALNET_BOOTSTRAP_NODES="${LOCALNET_BOOTSTRAP_NODES:-[]}"
CONFIG_FILE="$NODE_DIR/config.toml"

if [ -f "$CONFIG_FILE" ]; then
  EXISTING_MODE=$(grep network_mode "$CONFIG_FILE" | head -1 | awk -F '"' '{print $2}')
  if [ "$EXISTING_MODE" != "localnet" ]; then
    echo "ERROR: existing config.toml has network_mode=$EXISTING_MODE, expected localnet. Refusing to touch it — check by hand."
    exit 1
  fi
  echo "config.toml already present (network_mode=localnet), leaving it as-is."
else
  echo "Generating config.toml with localnet_bootstrap_nodes=$LOCALNET_BOOTSTRAP_NODES"
  export LOCALNET_BOOTSTRAP_NODES
  envsubst < "$BUNDLE_DIR/config.toml.template" > "$CONFIG_FILE"
fi

# ---------------------------------------------------------------------------
# Postgres — one dedicated container + volume per desktop, idempotent
# ---------------------------------------------------------------------------
if [ -n "$(docker ps -aq -f name=^${DB_CONTAINER}$)" ]; then
  echo "Postgres container '$DB_CONTAINER' already exists, leaving it as-is."
  docker start "$DB_CONTAINER" >/dev/null 2>&1 || true
else
  echo "Starting Postgres (postgres:18, pinned)..."
  docker run --name "$DB_CONTAINER" \
    -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
    -p "${DB_PORT}:5432" \
    -v rubix_node_pgdata:/var/lib/postgresql \
    --restart always \
    -d postgres:18
fi

echo "Waiting for Postgres..."
until docker exec "$DB_CONTAINER" pg_isready -U rubix >/dev/null 2>&1; do sleep 2; done
echo "Postgres ready."

# ---------------------------------------------------------------------------
# Stale IPFS state guard — same failure mode entrypoint.sh guards against:
# a .ipfs/ dir left over from a previous failed run (no config inside) makes
# initIPFS skip re-init and fail with a confusing "config: no such file" error.
# ---------------------------------------------------------------------------
IPFS_DIR="$NODE_DIR/.ipfs"
if [ -d "$IPFS_DIR" ] && [ ! -f "$IPFS_DIR/config" ]; then
  echo "WARNING: incomplete .ipfs/ state found, clearing it so Rubix can re-init cleanly."
  rm -rf "$IPFS_DIR"
fi

# ---------------------------------------------------------------------------
# rubix init — no-op if config.toml already exists (see core/config/config.go
# CreateConfigFileFromTemplate), safe to always call.
# ---------------------------------------------------------------------------
(cd "$NODE_DIR" && ./rubixgoplatform init -p "$NODE_DIR")

# ---------------------------------------------------------------------------
# systemd service — always on, restarts on crash and on boot
# ---------------------------------------------------------------------------
sed -e "s#{{NODE_DIR}}#$NODE_DIR#g" -e "s#{{PROFILE}}#$NODE_DIR#g" -e "s#{{USER}}#$(whoami)#g" \
  "$BUNDLE_DIR/rubixgoplatform.service" | sudo tee /etc/systemd/system/rubixgoplatform.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now rubixgoplatform

echo ""
echo "== Done =="
echo "Verify with:  curl -s http://localhost:20000/rubix/v1/dids"
echo "Logs with:    journalctl -u rubixgoplatform -f"
