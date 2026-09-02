#!/usr/bin/env bash
# Rubix lab — fleet-wide node identity + wallet reset.
#
# DESTRUCTIVE AND IRREVERSIBLE. This deletes, per host:
#   1. The Postgres container + its data volume (wallet, tokens, quorum
#      registrations, transaction history — everything generate_local_rbt
#      and dids-to-excel.py built up).
#   2. <remote-dir>/localnet/ on that node's own filesystem — this is where
#      DID PRIVATE KEYS actually live (confirmed: core/config/config.go
#      DidDir = <nodeDir>/localnet/dids, NOT in Postgres). Deleting this
#      permanently destroys that node's DID identity. There is no recovery.
#
# Why: every local-RBT token minted before today's rubix_client.py fix
# (allocate_token_index_range) has a small, node-independent sequential ID
# that collides with other nodes' tokens of the same fleet the moment a
# shared quorum has to validate them (TokenChainIntigrityCheck, see
# CLAUDE.md's "Known open bug" entry). New tokens minted through the fixed
# fund_did() are safe, but every wallet's EXISTING balance is a mix of old
# (unsafe) and new (safe) tokens, and transfers pick from the whole wallet
# - so the collision keeps recurring until the old backlog is gone. There's
# no way to selectively delete just the bad tokens; a full reset is the
# clean path back to a trustworthy state.
#
# Defaults match what's ACTUALLY running on this fleet (confirmed via
# `docker ps` earlier — container name "node", volume "pgdata_node", NOT
# setup.sh's rubix-node-db/rubix_node_pgdata defaults, since these machines
# were brought up by hand, not via install/setup.sh).
#
# Usage:
#   ./wipe-node-db.sh                  # DRY RUN — prints what would happen, changes nothing
#   ./wipe-node-db.sh --yes            # actually do it, fleet-wide
#   ./wipe-node-db.sh --yes --host 192.168.1.104   # just one host — DO THIS FIRST
#
# Strongly recommended: run against ONE host with --yes first, confirm the
# node comes back up clean and DID/token state is really gone, before
# running fleet-wide.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
REMOTE_DIR='~/Desktop/rubix/node'
CONTAINER="node"
VOLUME="pgdata_node"
DB_PORT=5433
DRY_RUN=1
SINGLE_HOST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) DRY_RUN=0; shift ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --volume) VOLUME="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "== DRY RUN — nothing will be changed. Pass --yes to actually wipe. =="
else
  echo "== LIVE RUN — this WILL permanently delete DID keys and wallet data on ${#HOSTS[@]} host(s). =="
  echo "Container: $CONTAINER   Volume: $VOLUME   Remote dir: $REMOTE_DIR"
  read -r -p "Type 'wipe' to confirm: " CONFIRM
  [ "$CONFIRM" = "wipe" ] || { echo "Not confirmed, aborting."; exit 1; }
fi
echo

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "  would: stop node process, docker rm -f $CONTAINER, docker volume rm $VOLUME,"
    echo "         rm -rf ${REMOTE_DIR}/localnet, recreate a fresh $CONTAINER Postgres container"
    continue
  fi

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$REMOTE_DIR" "$CONTAINER" "$VOLUME" "$DB_PORT" <<'REMOTE_SCRIPT'
set -euo pipefail
REMOTE_DIR="$1"; CONTAINER="$2"; VOLUME="$3"; DB_PORT="$4"

echo "  stopping node process..."
sudo systemctl stop rubixgoplatform >/dev/null 2>&1 || true
pkill -f rubixgoplatform >/dev/null 2>&1 || true
sleep 1

echo "  wiping Postgres (container + volume)..."
docker stop "$CONTAINER" >/dev/null 2>&1 || true
docker rm "$CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$VOLUME" >/dev/null 2>&1 || true

echo "  deleting DID/NFT/SC key material ($REMOTE_DIR/localnet)..."
eval "rm -rf ${REMOTE_DIR}/localnet"

echo "  recreating a fresh Postgres container..."
docker run --name "$CONTAINER" \
  -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
  -p "${DB_PORT}:5432" \
  -v "${VOLUME}:/var/lib/postgresql" \
  --restart always \
  -d postgres:18 >/dev/null

echo "  waiting for Postgres..."
until docker exec "$CONTAINER" pg_isready -U rubix >/dev/null 2>&1; do sleep 2; done
echo "  done — node stopped, DB fresh, DID/NFT/SC dir gone. Bring the node back up when ready."
REMOTE_SCRIPT
  then
    echo "  FAILED on $ip"
    FAILED+=("$ip")
  fi
  echo
done

if [ "$DRY_RUN" -eq 0 ] && [ "${#FAILED[@]}" -gt 0 ]; then
  echo "These hosts failed and need manual attention: ${FAILED[*]}"
  exit 1
fi

if [ "$DRY_RUN" -eq 0 ]; then
  echo "All done. Every wiped node needs to be brought back up before anything else:"
  echo "  ./restart-nodes.sh          # brings every node back up, from the controller, over SSH"
  echo "Then controller/dids-to-excel.py to re-create DIDs (SSH keys weren't touched by this,"
  echo "no need to re-run setup-ssh.sh)."
fi
