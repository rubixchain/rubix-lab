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
# were brought up by hand, not via systems/install/setup.sh).
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
INCLUDE_FIXED=0
# Concurrent hosts. 0 = all at once (default) - separate machines, one SSH
# connection each, no shared bottleneck at lab scale. --jobs N to cap it
# (--jobs 1 = serial, easier to watch a partial failure).
JOBS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) DRY_RUN=0; shift ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --volume) VOLUME="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  # NEVER wipe fixed-role hosts by default. The fullnode is the bootstrap seed
  # whose peer ID is baked into every other node's localnet_bootstrap_nodes -
  # wiping it would break the whole swarm's bootstrap path. The explorer and
  # controller don't run a rubix node at all. Filters on hosts.txt's role
  # column, same as smoke_test.py's FIXED_ROLES.
  if [ "$INCLUDE_FIXED" -eq 1 ]; then
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
  else
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" \
      | awk 'NF && $2 != "fullnode" && $2 != "explorer" && $2 != "controller" {print $1}')
  fi
fi

[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "== DRY RUN — nothing will be changed. Pass --yes to actually wipe. =="
else
  echo "== LIVE RUN — this WILL permanently delete DID keys and wallet data on ${#HOSTS[@]} host(s). =="
  echo "Container: $CONTAINER   Volume: $VOLUME   Remote dir: $REMOTE_DIR"
  echo "Running ${JOBS} host(s) at a time; output is buffered per host and printed together at the end."
  read -r -p "Type 'wipe' to confirm: " CONFIRM
  [ "$CONFIRM" = "wipe" ] || { echo "Not confirmed, aborting."; exit 1; }
fi
echo

# Handles one host. Runs in a background subshell, so it reports its result
# ONLY via exit code (array appends in a subshell wouldn't propagate):
# 0 = wiped cleanly, 1 = failed.
wipe_one() {
  local ip="$1"

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$REMOTE_DIR" "$CONTAINER" "$VOLUME" "$DB_PORT" <<'REMOTE_SCRIPT'
set -euo pipefail
REMOTE_DIR="$1"; CONTAINER="$2"; VOLUME="$3"; DB_PORT="$4"

echo "  stopping node process..."
# Must go through systemd where a unit exists. The unit has Restart=always,
# so a bare pkill would just make systemd relaunch the node ~5s later -
# mid-wipe, against a half-deleted localnet/ and a being-recreated DB.
# pkill is only safe as a fallback on hosts with no unit installed.
if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
  sudo systemctl stop rubixgoplatform
else
  pkill -f "rubixgoplatform run" >/dev/null 2>&1 || true
fi

# Verify it's really gone before deleting anything underneath it.
for i in $(seq 1 15); do
  pgrep -f "rubixgoplatform run" >/dev/null 2>&1 || break
  sleep 1
done
if pgrep -f "rubixgoplatform run" >/dev/null 2>&1; then
  echo "  ERROR: node process still running after stop - refusing to wipe data underneath it"
  exit 1
fi

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
# Bounded, not `until ... done` - an unbounded wait would hang this host
# forever if Postgres never comes up, which is invisible during a parallel run.
PG_READY=0
for i in $(seq 1 30); do
  if docker exec "$CONTAINER" pg_isready -U rubix >/dev/null 2>&1; then PG_READY=1; break; fi
  sleep 2
done
if [ "$PG_READY" -ne 1 ]; then
  echo "  ERROR: Postgres did not become ready within 60s"
  exit 1
fi
echo "  done — node stopped, DB fresh, DID/NFT/SC dir gone. Bring the node back up when ready."
REMOTE_SCRIPT
  then
    echo "  FAILED"
    return 1
  fi
  return 0
}

if [ "$DRY_RUN" -eq 1 ]; then
  for ip in "${HOSTS[@]}"; do
    echo "== $ip =="
    echo "  would: stop node process, docker rm -f $CONTAINER, docker volume rm $VOLUME,"
    echo "         rm -rf ${REMOTE_DIR}/localnet, recreate a fresh $CONTAINER Postgres container"
    echo
  done
  echo "----------------------------------------"
  echo "DRY RUN — nothing was changed. Pass --yes to actually wipe."
  exit 0
fi

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

running=0
for ip in "${HOSTS[@]}"; do
  {
    # set +e inside the subshell: errexit would abort it before the .rc file
    # is written, losing this host's real result. Same reason `wait -n` is
    # guarded below.
    set +e
    wipe_one "$ip" > "$WORKDIR/$ip.out" 2>&1
    echo $? > "$WORKDIR/$ip.rc"
  } &
  running=$(( running + 1 ))
  if [ "$running" -ge "$JOBS" ]; then
    wait -n || true
    running=$(( running - 1 ))
  fi
done
wait || true

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="
  cat "$WORKDIR/$ip.out" 2>/dev/null || echo "  (no output captured)"
  rc="$(cat "$WORKDIR/$ip.rc" 2>/dev/null || echo 1)"
  [ "$rc" = "0" ] || FAILED+=("$ip")
  echo
done

echo "----------------------------------------"
WIPED=$(( ${#HOSTS[@]} - ${#FAILED[@]} ))
echo "${#HOSTS[@]} host(s) total: $WIPED wiped cleanly, ${#FAILED[@]} need attention."
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Needs attention: ${FAILED[*]}"
  echo "(re-run against just those with: --yes --host <ip>)"
  exit 1
fi

echo
echo "Every wiped node needs to be brought back up before anything else:"
echo "  ./restart-nodes.sh --attempts 30   # cold start after a wipe is slower than a normal restart"
echo "Then controller/dids-to-excel.py to re-create DIDs (SSH keys weren't touched by this,"
echo "no need to re-run setup-ssh.sh)."
