#!/usr/bin/env bash
# Rubix lab — fleet-wide node identity + wallet reset.
#
# DESTRUCTIVE AND IRREVERSIBLE. Two modes:
#
# Default - the nodes setup.sh installs (~/rubix-lab/nodes/node<slot>), per node:
#   1. stop its unit rubixgoplatform@<slot>
#   2. delete its Postgres container rubix-db-<slot> + volume rubix-pgdata-<slot>
#      (wallet, tokens, quorum registrations, transaction history)
#   3. delete <node>/testnet, <node>/localnet, <node>/mainnet - the DID private
#      keys, NFT and contract files live there (core/config/config.go: DidDir =
#      <nodeDir>/<network>/dids), NOT in Postgres. There is no recovery.
#   4. create a fresh, empty Postgres container in its place
#   The nodes are left STOPPED. The binary, config.toml and .ipfs (the node's
#   peer ID) are kept, so `./restart-nodes.sh --attempts 30` brings each one
#   back as a fresh node with no DID.
#
# --legacy - the single node every machine ran before the multi-node layout
#   (hand-built in ~/Desktop/rubix, or an older setup.sh in ~/rubix-lab/nodes/testnode):
#   stops rubixgoplatform.service, deletes Postgres container node / rubix-node-db
#   and its volume, the old node's profile (the unit's -p folder: config, DIDs,
#   IPFS repo), its binaries and swarm key, and ~/rubix-lab/nodes/testnode.
#   ~/Desktop/rubix itself goes only if nothing else is in it - the old
#   controller kept its rubix-lab clone there; anything left is listed.
#   Nothing is recreated: install-nodes.sh comes next. Refuses if another Rubix
#   node is still running on the machine - a second node from that era (the
#   old faucet quorum on port 20010) has to be stopped by hand first, so
#   nothing is deleted from under a live process.
#
# Pool machines by default; --include-fixed adds the fullnode, faucet, explorer
# and controller machines (for --legacy that means every machine in hosts.txt).
#
# Usage:
#   ./wipe-node-db.sh                                   # DRY RUN — prints what would happen
#   ./wipe-node-db.sh --yes                             # pool machines' nodes
#   ./wipe-node-db.sh --yes --host 192.168.1.107        # one machine — DO THIS FIRST
#   ./wipe-node-db.sh --legacy --include-fixed --yes    # the one-time flush before the rebuild

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
DRY_RUN=1
LEGACY=0
SINGLE_HOST=""
INCLUDE_FIXED=0
# Concurrent machines. 0 = all at once (default) - separate machines, one SSH
# connection each. --jobs 1 = serial, easier to watch a partial failure.
JOBS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) DRY_RUN=0; shift ;;
    --legacy) LEGACY=1; shift ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
[ "$INCLUDE_FIXED" -eq 1 ] && SELECT+=(--include-fixed)

# Machines, and for the default mode each machine's slots ("ip slot ..." lines
# from fleet.py). fleet.py stops with a clear message on a bad hosts.txt.
if [ "$LEGACY" -eq 1 ]; then
  listing="$(python3 "$HERE/fleet.py" hosts "${SELECT[@]}")"
else
  listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")"
fi
HOSTS=()
declare -A SLOTS_OF=()
while read -r ip slot _rest; do
  [ -n "$ip" ] || continue
  if [ -z "${SLOTS_OF[$ip]+set}" ]; then HOSTS+=("$ip"); SLOTS_OF[$ip]=""; fi
  [ "$LEGACY" -eq 1 ] || SLOTS_OF[$ip]="${SLOTS_OF[$ip]} $slot"
done <<<"$listing"
[ "${#HOSTS[@]}" -gt 0 ] || { echo "No machines selected."; exit 1; }
[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

what_happens() {   # $1 = ip
  if [ "$LEGACY" -eq 1 ]; then
    echo "  would: stop rubixgoplatform.service; docker rm -f node rubix-node-db; docker volume rm"
    echo "         pgdata_node rubix_node_pgdata; delete the old node's profile (the unit's -p folder),"
    echo "         its binaries and swarm key in ~/Desktop/rubix, and ~/rubix-lab/nodes/testnode"
  else
    for s in ${SLOTS_OF[$1]}; do
      echo "  would: node $s - stop rubixgoplatform@$s; docker rm -f rubix-db-$s + volume rubix-pgdata-$s;"
      echo "         rm -rf ~/rubix-lab/nodes/node$s/{testnet,localnet,mainnet}; recreate an empty rubix-db-$s"
    done
  fi
}

if [ "$DRY_RUN" -eq 1 ]; then
  echo "== DRY RUN — nothing will be changed. Pass --yes to actually wipe. =="
  echo
  for ip in "${HOSTS[@]}"; do
    echo "== $ip =="
    what_happens "$ip"
    echo
  done
  echo "----------------------------------------"
  echo "DRY RUN — nothing was changed. Pass --yes to actually wipe."
  exit 0
fi

if [ "$LEGACY" -eq 1 ]; then
  echo "== LIVE RUN — this WILL permanently delete the old single-node install (DID keys, wallet, IPFS repo) on ${#HOSTS[@]} machine(s). =="
else
  echo "== LIVE RUN — this WILL permanently delete DID keys and wallet data of every node on ${#HOSTS[@]} machine(s). =="
fi
echo "Running ${JOBS} machine(s) at a time; output is buffered per machine and printed together at the end."
read -r -p "Type 'wipe' to confirm: " CONFIRM
[ "$CONFIRM" = "wipe" ] || { echo "Not confirmed, aborting."; exit 1; }
echo

# Runs ON the machine. $1 = "legacy" or "nodes", then the slots.
REMOTE_SCRIPT='
set -uo pipefail
MODE="$1"; shift

wait_gone() {   # $1 = pgrep -f pattern; 15s for the process to exit
  for _ in $(seq 1 15); do
    pgrep -f "$1" >/dev/null 2>&1 || return 0
    sleep 1
  done
  ! pgrep -f "$1" >/dev/null 2>&1
}

fresh_db() {   # $1 = container, $2 = volume, $3 = port
  docker run --name "$1" \
    -e POSTGRES_PASSWORD=rubixpass -e POSTGRES_USER=rubix -e POSTGRES_DB=rubix \
    -p "$3:5432" -v "$2:/var/lib/postgresql" --restart always -d postgres:18 >/dev/null || return 1
  # Bounded: an endless wait would hang this machine invisibly in a parallel run.
  for _ in $(seq 1 30); do
    docker exec "$1" pg_isready -U rubix >/dev/null 2>&1 && return 0
    sleep 2
  done
  echo "  ERROR: $1 did not become ready within 60s"
  return 1
}

if [ "$MODE" = "legacy" ]; then
  if [ -e /etc/systemd/system/rubixgoplatform@.service ] \
     || compgen -G "$HOME/rubix-lab/nodes/node[0-9]*" >/dev/null; then
    echo "  ERROR: this machine already has the multi-node layout - --legacy is only for the"
    echo "         old single-node install. Use the default mode for the new nodes."
    exit 1
  fi
  echo "  stopping the old node..."
  # Through systemd where the unit exists: it has Restart=always, so a bare
  # pkill would just make systemd relaunch the node mid-wipe.
  if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
    sudo -n systemctl stop rubixgoplatform || { echo "  ERROR: could not stop rubixgoplatform.service"; exit 1; }
  else
    pkill -f "rubixgoplatform run" >/dev/null 2>&1 || true
  fi
  if ! wait_gone "rubixgoplatform run"; then
    echo "  ERROR: a Rubix node is still running here - refusing to delete anything:"
    pgrep -af "rubixgoplatform run" | sed "s/^/           /"
    echo "         Stop it by hand (a second node from the old layout, e.g. the old faucet"
    echo "         quorum on port 20010), then run this again."
    exit 1
  fi
  for c in node rubix-node-db; do docker rm -f "$c" >/dev/null 2>&1 && echo "  deleted container $c"; done
  for v in pgdata_node rubix_node_pgdata; do docker volume rm "$v" >/dev/null 2>&1 && echo "  deleted volume $v"; done

  # Only the files of the old node. ~/Desktop/rubix can hold other things - the
  # old controller kept its rubix-lab clone in it - so the folder itself goes
  # only when nothing else is left in it. The node profile (config, DIDs, IPFS
  # repo) is wherever -p of the old unit pointed, and is deleted only if it
  # really is a node profile (has a config.toml) and is not the repo.
  # (No apostrophes in this block: it is one single-quoted string.)
  old=~/Desktop/rubix
  profile="$(sed -n "s/^ExecStart=.* -p \([^ ]*\).*/\1/p" /etc/systemd/system/rubixgoplatform.service 2>/dev/null | head -1)"
  [ -n "$profile" ] || profile="$old/node"
  if [ "${profile%/}" = "$HOME" ] || [ -e "$profile/.git" ] || [ -e "$profile/rubix-lab" ]; then
    echo "  NOTE: not deleting $profile - it holds more than a node"
  elif [ -f "$profile/config.toml" ]; then
    rm -rf "$profile" && echo "  deleted $profile (old node: config, DIDs, IPFS repo)"
  fi
  if [ -d "$old" ]; then
    for f in rubixgoplatform ipfs localnetswarm.key testnetswarm.key node-restart.log log.txt .ipfs; do
      [ -e "$old/$f" ] && rm -rf "$old/$f" && echo "  deleted $old/$f"
    done
    rest="$(ls -A "$old" 2>/dev/null)"
    if [ -z "$rest" ]; then
      rmdir "$old" && echo "  deleted the now-empty $old"
    else
      echo "  NOTE: left in $old - not the old node files, remove by hand if unwanted: $(echo $rest)"
    fi
  fi
  [ -e ~/rubix-lab/nodes/testnode ] && rm -rf ~/rubix-lab/nodes/testnode && echo "  deleted ~/rubix-lab/nodes/testnode"
  left="$(docker ps -a --format "{{.Names}} ({{.Image}})" | grep -i postgres || true)"
  [ -z "$left" ] || echo "  NOTE: other Postgres containers are still here (not touched): $left"
  if [ -f /etc/systemd/system/rubixgoplatform.service ]; then
    echo "  The old unit file stays until install-nodes.sh removes it. On a machine that will run"
    echo "  no node (explorer, controller) remove it by hand so it stops retrying at boot:"
    echo "    sudo systemctl disable rubixgoplatform && sudo rm /etc/systemd/system/rubixgoplatform.service"
  fi
  echo "  done — old install gone. Next: install-nodes.sh"
  exit 0
fi

FAILED=0
for s in "$@"; do
  slot="${s%%:*}"; port="${s#*:}"
  echo "  node $slot: stopping..."
  sudo -n systemctl stop "rubixgoplatform@$slot" 2>/dev/null \
    || { echo "  ERROR: could not stop rubixgoplatform@$slot (sudo grant missing? re-run setup.sh)"; FAILED=1; continue; }
  # Verify it is really gone before deleting anything underneath it.
  if ! wait_gone "nodes/node$slot/rubixgoplatform run"; then
    echo "  ERROR: node $slot still running after stop - refusing to wipe data underneath it"
    FAILED=1; continue
  fi
  docker rm -f "rubix-db-$slot" >/dev/null 2>&1 || true
  docker volume rm "rubix-pgdata-$slot" >/dev/null 2>&1 || true
  rm -rf ~/rubix-lab/nodes/node$slot/testnet ~/rubix-lab/nodes/node$slot/localnet ~/rubix-lab/nodes/node$slot/mainnet
  if fresh_db "rubix-db-$slot" "rubix-pgdata-$slot" "$port"; then
    echo "  node $slot: wiped, empty database on $port, node stopped"
  else
    FAILED=1
  fi
done
exit $FAILED
'

# Handles one machine. Runs in a background subshell, so it reports its result
# ONLY via exit code (array appends in a subshell would not propagate).
wipe_one() {
  local ip="$1" args=() s
  if [ "$LEGACY" -eq 1 ]; then
    args=(legacy)
  else
    args=(nodes)
    # "slot:db_port" - the database port follows the node_index (fleet.py)
    while read -r hip slot _index _api db _rest; do
      [ "$hip" = "$ip" ] && args+=("$slot:$db")
    done <<<"$listing"
  fi
  ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "${args[@]}" <<<"$REMOTE_SCRIPT"
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

running=0
for ip in "${HOSTS[@]}"; do
  {
    # set +e inside the subshell: errexit would abort it before the .rc file
    # is written, losing this machine's real result.
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
echo "${#HOSTS[@]} machine(s): $(( ${#HOSTS[@]} - ${#FAILED[@]} )) wiped cleanly, ${#FAILED[@]} need attention."
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Needs attention: ${FAILED[*]}"
  echo "(re-run against just one with: --yes --host <ip>)"
  exit 1
fi
echo
if [ "$LEGACY" -eq 1 ]; then
  echo "Next: ./install-nodes.sh (first fullnode with --first, see SETUP-RUNBOOK.md Phase B)."
else
  echo "Every wiped node is stopped. Bring them back (a cold start after a wipe is slow):"
  echo "  ./restart-nodes.sh --attempts 30"
  echo "Then python3 dids-to-excel.py to create the DIDs again."
fi
