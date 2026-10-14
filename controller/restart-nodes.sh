#!/usr/bin/env bash
# Rubix lab — restart nodes fleet-wide, from the controller, over SSH.
#
# Every node is its own systemd unit, rubixgoplatform@<slot> (installed by
# systems/install/setup.sh), so a restart is `systemctl restart` of that unit -
# passwordless through the sudo grant setup.sh installs. The unit has
# Restart=always, so a node that crashes comes back on its own; this is for
# the ones that did not, and for picking up a new binary.
#
# Default behaviour is idempotent: a node already answering its API is left
# completely alone (not bounced) - only nodes that are actually down get
# touched. Pass --force to bounce every selected node regardless.
#
# Machines are processed in PARALLEL (independent machines, no shared state).
# Within a machine, its nodes are restarted one after another and then polled
# together. Each machine's output is buffered and printed in hosts.txt order.
#
# Usage:
#   ./restart-nodes.sh                                  # pool machines, only the nodes that are down
#   ./restart-nodes.sh --host 192.168.1.107             # one machine (any role)
#   ./restart-nodes.sh --host 192.168.1.107 --slot 3    # one node
#   ./restart-nodes.sh --include-fixed                  # also the fullnode and faucet machines
#   ./restart-nodes.sh --force                          # bounce every selected node
#   ./restart-nodes.sh --attempts 30                    # more patience (cold start after a wipe)
#   ./restart-nodes.sh --jobs 1                         # one machine at a time

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
# Readiness probe: wait INITIAL_WAIT once (a normal start takes ~15s to reach
# "Server running"), then poll every POLL_INTERVAL up to ATTEMPTS times. Breaks
# the moment every node answers. Defaults give ~30s worst case, which covers a
# normal restart. A cold start right after a wipe (fresh IPFS init +
# migrations) is slower - use --attempts 30 for those runs.
INITIAL_WAIT=10
POLL_INTERVAL=2
ATTEMPTS=10
SINGLE_HOST=""
ONLY_SLOT=""
FORCE=0
INCLUDE_FIXED=0
# Concurrent machines. 0 = all at once, which is the default: separate
# machines, one SSH connection each. --jobs N to cap it (--jobs 1 = serial).
JOBS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --slot) ONLY_SLOT="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --initial-wait) INITIAL_WAIT="$2"; shift 2 ;;
    --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done
[ -z "$ONLY_SLOT" ] || [ -n "$SINGLE_HOST" ] || { echo "ERROR: --slot goes with --host."; exit 1; }

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
# Without curl the readiness probe silently fails for every node: already-up
# nodes are never skipped, and healthy nodes get reported as "not answering".
command -v curl >/dev/null 2>&1 || {
  echo "ERROR: curl is not installed on this controller, so readiness can't be checked."
  echo "       Install it first:  sudo apt install -y curl"
  exit 1
}

SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
[ "$INCLUDE_FIXED" -eq 1 ] && SELECT+=(--include-fixed)
# "ip slot node_index api_port ..." per node; fleet.py stops on a bad hosts.txt.
listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")"
HOSTS=()
while read -r ip _rest; do
  [ -n "$ip" ] || continue
  case " ${HOSTS[*]-} " in *" $ip "*) ;; *) HOSTS+=("$ip") ;; esac
done <<<"$listing"
[ "${#HOSTS[@]}" -gt 0 ] || { echo "No nodes selected."; exit 1; }
[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

if [ "$FORCE" -eq 1 ]; then
  echo "Checking ${#HOSTS[@]} machine(s), ${JOBS} at a time - --force set, bouncing every selected node..."
else
  echo "Checking ${#HOSTS[@]} machine(s), ${JOBS} at a time - nodes already up are left alone, only down ones get restarted..."
fi
echo "(output is buffered per machine and printed together when all are done)"
echo

answers() {   # $1 = ip, $2 = API port: the node serves its DID list (so its database is up too)
  curl -s -o /dev/null --max-time "${3:-2}" "http://$1:$2/rubix/v1/dids" 2>/dev/null
}

# Handles one machine. Runs in a background subshell, so it communicates
# results ONLY via its exit code: 0 = restarted and answering, 2 = everything
# already up and left alone, 1 = needs attention.
restart_one() {
  local ip="$1" todo=() ports=() hip slot _index api _rest
  while read -r hip slot _index api _rest; do
    [ "$hip" = "$ip" ] || continue
    [ -z "$ONLY_SLOT" ] || [ "$slot" = "$ONLY_SLOT" ] || continue
    if [ "$FORCE" -eq 0 ] && answers "$ip" "$api" 3; then
      echo "  node $slot (:$api) already up, skipped"
      continue
    fi
    todo+=("$slot"); ports+=("$api")
  done <<<"$listing"
  if [ "${#todo[@]}" -eq 0 ]; then
    echo "  (use --force to bounce running nodes anyway)"
    return 2
  fi

  if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "${todo[@]}" <<'REMOTE_SCRIPT'
rc=0
for s in "$@"; do
  sudo -n systemctl restart "rubixgoplatform@$s" \
    || { echo "  ERROR: could not restart rubixgoplatform@$s (sudo grant missing? re-run setup.sh there)"; rc=1; }
done
exit $rc
REMOTE_SCRIPT
  then
    echo "  FAILED to restart (see above)"
    return 1
  fi

  echo "  restarted node(s) ${todo[*]}; waiting ${INITIAL_WAIT}s, then checking every ${POLL_INTERVAL}s (up to ${ATTEMPTS}x)..."
  local started_at i attempt still pending=("${!todo[@]}")
  started_at=$(date +%s)
  sleep "$INITIAL_WAIT"
  for ((attempt = 1; attempt <= ATTEMPTS && ${#pending[@]} > 0; attempt++)); do
    still=()
    for i in "${pending[@]}"; do
      answers "$ip" "${ports[$i]}" || still+=("$i")
    done
    pending=("${still[@]}")
    [ "${#pending[@]}" -gt 0 ] && [ "$attempt" -lt "$ATTEMPTS" ] && sleep "$POLL_INTERVAL"
  done
  if [ "${#pending[@]}" -eq 0 ]; then
    echo "  up and answering ($(( $(date +%s) - started_at ))s)"
    return 0
  fi
  for i in "${pending[@]}"; do
    echo "  WARNING: node ${todo[$i]} (:${ports[$i]}) not answering — check: ssh ${SSH_USER}@${ip} journalctl -u rubixgoplatform@${todo[$i]} -n 100"
  done
  echo "           (if it was still starting, retry with: --attempts 30)"
  return 1
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

running=0
for ip in "${HOSTS[@]}"; do
  {
    # set +e inside the subshell: without it, errexit aborts this subshell the
    # moment restart_one returns non-zero, so the .rc file never gets written
    # and the machine's real result is lost. Likewise `wait -n` propagates a
    # failed job's status, which would kill the parent under set -e.
    set +e
    restart_one "$ip" > "$WORKDIR/$ip.out" 2>&1
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
SKIPPED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="
  cat "$WORKDIR/$ip.out" 2>/dev/null || echo "  (no output captured)"
  rc="$(cat "$WORKDIR/$ip.rc" 2>/dev/null || echo 1)"
  case "$rc" in
    0) ;;
    2) SKIPPED+=("$ip") ;;
    *) FAILED+=("$ip") ;;
  esac
  echo
done

echo "----------------------------------------"
RESTARTED=$(( ${#HOSTS[@]} - ${#SKIPPED[@]} - ${#FAILED[@]} ))
echo "${#HOSTS[@]} machine(s): $RESTARTED had nodes restarted and answering, ${#SKIPPED[@]} already fully up, ${#FAILED[@]} need attention."
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Needs attention: ${FAILED[*]}"
  exit 1
fi
