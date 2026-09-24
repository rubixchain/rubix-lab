#!/usr/bin/env bash
# Rubix lab — move a pool node from localnet to the custom testnet.
#
# DESTRUCTIVE. Per host, in this order:
#   1. stop the node
#   2. rename localnetswarm.key -> testnetswarm.key (the file the node copies
#      into its IPFS repo in testnet mode; the key itself is unchanged, so the
#      swarm and every peer id stay the same)
#   3. rewrite config.toml: network_mode = "testnet", and move the bootstrap
#      entry from localnet_bootstrap_nodes to testnet_bootstrap_nodes
#   4. wipe Postgres (container + volume) and recreate it empty
#   5. start the node and wait for its API
#
# The DID directory is per network (<profile>/testnet/dids), so the old
# localnet DIDs simply stop being visible - nothing needs deleting for that.
# The database is NOT per network, which is why the wipe is not optional: a
# node would otherwise start testnet holding localnet DIDs and level 1000x
# tokens that testnet validation rejects.
#
# Run the binary deploy FIRST. A node that switches to testnet while still
# running an older build enforces the wrong minter allowlist and rejects every
# token, which is confusing to diagnose - so this refuses to touch a host whose
# binary is not the expected commit.
#
#   cd exec-update && REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh <branch> --all
#   cd .. && ./switch-to-testnet.sh                      # DRY RUN
#   ./switch-to-testnet.sh --yes
#   ./switch-to-testnet.sh --yes --host 192.168.1.104    # one host first
#
# Fixed-role hosts (fullnode, explorer, controller) are skipped by default:
# .101 is the bootstrap seed and is switched by hand, since its own bootstrap
# list must end up EMPTY rather than pointing at itself.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
NODE_DIR='~/Desktop/rubix'          # binary, ipfs and the swarm key live here
PROFILE_DIR='~/Desktop/rubix/node'  # -p argument: config.toml and node data
CONTAINER="node"
VOLUME="pgdata_node"
DB_PORT=5433
API_PORT=20000
EXPECT_COMMIT="658cb33f"            # --expect-commit '' to skip the check
DRY_RUN=1
SINGLE_HOST=""
INCLUDE_FIXED=0
JOBS=0
ATTEMPTS=30                         # cold start after a wipe is slow
POLL_INTERVAL=2

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) DRY_RUN=0; shift ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --node-dir) NODE_DIR="$2"; shift 2 ;;
    --profile-dir) PROFILE_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --volume) VOLUME="$2"; shift 2 ;;
    --expect-commit) EXPECT_COMMIT="$2"; shift 2 ;;
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  if [ "$INCLUDE_FIXED" -eq 1 ]; then
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
  else
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" \
      | awk 'NF && $2 != "fullnode" && $2 != "explorer" && $2 != "controller" {print $1}')
  fi
fi
[ "${#HOSTS[@]}" -gt 0 ] || { echo "ERROR: no hosts selected."; exit 1; }
[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "== DRY RUN — reports what each host WOULD do, changes nothing. Pass --yes to switch. =="
else
  echo "== LIVE — this WILL wipe the database on ${#HOSTS[@]} host(s) and switch them to testnet. =="
  echo "Node dir: $NODE_DIR   Profile: $PROFILE_DIR   Container: $CONTAINER   Volume: $VOLUME"
  [ -n "$EXPECT_COMMIT" ] && echo "Requires binary commit: $EXPECT_COMMIT"
  read -r -p "Type 'switch' to confirm: " CONFIRM
  [ "$CONFIRM" = "switch" ] || { echo "Not confirmed, aborting."; exit 1; }
fi
echo

switch_one() {
  local ip="$1"
  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- \
        "$NODE_DIR" "$PROFILE_DIR" "$CONTAINER" "$VOLUME" "$DB_PORT" \
        "$DRY_RUN" "$EXPECT_COMMIT" "$API_PORT" "$ATTEMPTS" "$POLL_INTERVAL" <<'REMOTE_SCRIPT'
set -euo pipefail
NODE_DIR="$1"; PROFILE_DIR="$2"; CONTAINER="$3"; VOLUME="$4"; DB_PORT="$5"
DRY_RUN="$6"; EXPECT_COMMIT="$7"; API_PORT="$8"; ATTEMPTS="$9"; POLL_INTERVAL="${10}"

eval NODE_DIR="$NODE_DIR"; eval PROFILE_DIR="$PROFILE_DIR"
CFG="$PROFILE_DIR/config.toml"

# --- preconditions, checked before anything is changed ---------------------
[ -f "$CFG" ] || { echo "  ERROR: no config.toml at $CFG"; exit 1; }

if [ -n "$EXPECT_COMMIT" ]; then
  have="$("$NODE_DIR/rubixgoplatform" -v 2>/dev/null | awk -F': *' '/Current Commit/{print $2}' | cut -c1-8)"
  if [ "$have" != "$EXPECT_COMMIT" ]; then
    echo "  ERROR: binary is '$have', expected '$EXPECT_COMMIT' - deploy the build first"
    exit 1
  fi
  echo "  binary $have OK"
fi

mode="$(awk -F'"' '/^network_mode/{print $2}' "$CFG")"
ln_bs="$(sed -n 's/^localnet_bootstrap_nodes = //p' "$CFG")"
tn_bs="$(sed -n 's/^testnet_bootstrap_nodes = //p' "$CFG")"
echo "  config: network_mode=$mode  localnet_bs=${ln_bs:-<none>}  testnet_bs=${tn_bs:-<none>}"

# Idempotency: only move the bootstrap list when there is one to move and the
# target is still empty. Re-running otherwise would blank a good testnet list.
move_bs=0
if [ -n "$ln_bs" ] && [ "$ln_bs" != "[]" ] && { [ -z "$tn_bs" ] || [ "$tn_bs" = "[]" ]; }; then
  move_bs=1
fi

if [ -f "$NODE_DIR/localnetswarm.key" ]; then key_action="rename localnetswarm.key -> testnetswarm.key"
elif [ -f "$NODE_DIR/testnetswarm.key" ]; then key_action="testnetswarm.key already present"
else echo "  ERROR: no swarm key in $NODE_DIR"; exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "  would: stop node; $key_action; set network_mode=testnet;"
  [ "$move_bs" -eq 1 ] && echo "         move bootstrap list to testnet_bootstrap_nodes" \
                       || echo "         leave bootstrap lists as they are"
  echo "         drop container '$CONTAINER' + volume '$VOLUME', recreate empty; start node"
  exit 0
fi

# --- 1. stop ---------------------------------------------------------------
echo "  stopping node..."
if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
  sudo systemctl stop rubixgoplatform
else
  pkill -f "rubixgoplatform run" >/dev/null 2>&1 || true
fi
for i in $(seq 1 15); do
  pgrep -f "rubixgoplatform run" >/dev/null 2>&1 || break
  sleep 1
done
if pgrep -f "rubixgoplatform run" >/dev/null 2>&1; then
  echo "  ERROR: node still running after stop - refusing to change data underneath it"
  exit 1
fi

# --- 2. swarm key ----------------------------------------------------------
if [ -f "$NODE_DIR/localnetswarm.key" ]; then
  if [ -f "$NODE_DIR/testnetswarm.key" ]; then
    rm -f "$NODE_DIR/localnetswarm.key"
    echo "  testnetswarm.key already present, removed the localnet name"
  else
    mv "$NODE_DIR/localnetswarm.key" "$NODE_DIR/testnetswarm.key"
    echo "  swarm key renamed (same key, so the swarm and peer ids are unchanged)"
  fi
fi

# --- 3. config -------------------------------------------------------------
cp "$CFG" "$CFG.localnet.bak"
sed -i 's|^network_mode = .*|network_mode = "testnet"|' "$CFG"
if [ "$move_bs" -eq 1 ]; then
  sed -i "s|^testnet_bootstrap_nodes = .*|testnet_bootstrap_nodes = $ln_bs|" "$CFG"
  sed -i 's|^localnet_bootstrap_nodes = .*|localnet_bootstrap_nodes = []|' "$CFG"
  echo "  config: testnet mode, bootstrap moved (backup at config.toml.localnet.bak)"
else
  echo "  config: testnet mode, bootstrap left as-is (backup at config.toml.localnet.bak)"
fi

# --- 4. database -----------------------------------------------------------
echo "  wiping Postgres..."
docker stop "$CONTAINER" >/dev/null 2>&1 || true
docker rm "$CONTAINER" >/dev/null 2>&1 || true
docker volume rm "$VOLUME" >/dev/null 2>&1 || true
docker run --name "$CONTAINER" \
  -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
  -p "${DB_PORT}:5432" -v "${VOLUME}:/var/lib/postgresql" \
  --restart always -d postgres:18 >/dev/null
for i in $(seq 1 30); do
  docker exec "$CONTAINER" pg_isready -U rubix >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U rubix >/dev/null 2>&1 || {
  echo "  ERROR: Postgres did not become ready"; exit 1; }

# --- 5. start --------------------------------------------------------------
echo "  starting node..."
if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
  sudo systemctl start rubixgoplatform
else
  echo "  WARN: no systemd unit - start the node by hand"
  exit 0
fi
for ((a = 1; a <= ATTEMPTS; a++)); do
  sleep "$POLL_INTERVAL"
  if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:${API_PORT}/rubix/v1/dids" 2>/dev/null; then
    echo "  API answered after $((a * POLL_INTERVAL))s"
    break
  fi
  if [ "$a" -eq "$ATTEMPTS" ]; then
    echo "  ERROR: API did not answer within $((ATTEMPTS * POLL_INTERVAL))s"
    exit 1
  fi
done
echo "  done - switched to testnet"
REMOTE_SCRIPT
  then
    return 1
  fi
  return 0
}

# Each host runs in its own background subshell, so it reports through a file
# rather than a shell variable - an array append inside a subshell does not
# reach the parent, which is why the summary is read back from disk.
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

running=0
for ip in "${HOSTS[@]}"; do
  (
    if out="$(switch_one "$ip" 2>&1)"; then echo "OK" > "$WORK/$ip.status"
    else echo "FAILED" > "$WORK/$ip.status"; fi
    printf '== %s ==\n%s\n\n' "$ip" "$out" > "$WORK/$ip.log"
  ) &
  running=$((running + 1))
  if [ "$running" -ge "$JOBS" ]; then wait -n 2>/dev/null || wait; running=0; fi
done
wait

FAILED=0
for ip in "${HOSTS[@]}"; do
  [ -f "$WORK/$ip.log" ] && cat "$WORK/$ip.log"
  if [ "$(cat "$WORK/$ip.status" 2>/dev/null || echo FAILED)" != "OK" ]; then
    FAILED=$((FAILED + 1))
  fi
done

echo "----------------------------------------"
printf '%d host(s): %d ok, %d need attention.\n' \
  "${#HOSTS[@]}" "$(( ${#HOSTS[@]} - FAILED ))" "$FAILED"
for ip in "${HOSTS[@]}"; do
  s="$(cat "$WORK/$ip.status" 2>/dev/null || echo FAILED)"
  [ "$s" = "OK" ] || echo "  $ip  $s"
done
if [ "$DRY_RUN" -eq 1 ]; then
  echo "Dry run - nothing was changed. Re-run with --yes to switch."
else
  echo "Next: python3 node-versions.py, then check each node's log for"
  echo "      'Minter allowlist: custom testnet: minters=2'."
fi
[ "$FAILED" -eq 0 ] || exit 1
