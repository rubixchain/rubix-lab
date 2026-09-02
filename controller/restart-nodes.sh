#!/usr/bin/env bash
# Rubix lab — restart nodes fleet-wide, from the controller, over SSH.
#
# rubixgoplatform run is a long-running foreground process. A plain
# `ssh host './rubixgoplatform run ...'` would hang the SSH connection
# forever (the script would never move to the next host) and the node
# would die the moment that SSH session closed. This launches it detached:
# nohup + all three file descriptors redirected away from the SSH channel
# (stdin from /dev/null, stdout/stderr to a log file) + disown, so it
# survives the SSH session ending.
#
# Prefers a systemd-managed restart where one exists (install/setup.sh's
# path) - only falls back to the detached-nohup launch for the ad hoc
# ~/Desktop/rubix/node layout this fleet actually uses.
#
# Default behaviour is idempotent: a host already answering the API is left
# completely alone (not bounced) - only hosts that are actually down get
# touched. Pass --force to bounce everyone regardless (e.g. right after a
# fleet-wide wipe, or to pick up a freshly deployed binary).
#
# Hosts are processed in PARALLEL (they're independent machines with no
# shared state), so a fleet-wide restart takes about as long as the slowest
# single host rather than the sum of all of them. Each host's output is
# buffered to a temp file and printed in host order at the end, so parallel
# runs stay readable instead of interleaving into noise.
#
# Usage:
#   ./restart-nodes.sh                                  # every host in hosts.txt, skip already-up ones
#   ./restart-nodes.sh --host 192.168.1.104              # just one
#   ./restart-nodes.sh --force                           # bounce everyone, even already-healthy hosts
#   ./restart-nodes.sh --attempts 30                     # more patience (post-wipe cold starts)
#   ./restart-nodes.sh --jobs 1                          # serial, if you need to watch one at a time
#   ./restart-nodes.sh --remote-dir '~/Desktop/rubix' --node-name node

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
REMOTE_DIR='~/Desktop/rubix'
NODE_NAME="node"
# Readiness probe: wait INITIAL_WAIT once (no point polling at t=1s when a
# normal start takes ~15s to reach "Server running at 0.0.0.0:20000"), then
# poll every POLL_INTERVAL up to ATTEMPTS times. Breaks the moment it answers.
# Defaults give ~30s worst case, which covers a normal systemd restart.
# A cold start right after a DB/localnet wipe (fresh IPFS init + migrations)
# is slower - use --attempts 30 for those runs.
INITIAL_WAIT=10
POLL_INTERVAL=2
ATTEMPTS=10
SINGLE_HOST=""
FORCE=0
INCLUDE_FIXED=0
# Concurrent hosts. 0 = all at once, which is the default: these are separate
# machines, one SSH connection each, so there's no shared bottleneck to
# throttle for at lab scale. --jobs N to cap it (--jobs 1 = serial).
JOBS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --node-name) NODE_NAME="$2"; shift 2 ;;
    --initial-wait) INITIAL_WAIT="$2"; shift 2 ;;
    --poll-interval) POLL_INTERVAL="$2"; shift 2 ;;
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  # Skip fixed-role hosts by default: the fullnode is the bootstrap seed every
  # other node points at, and the explorer/controller don't run a rubix node at
  # all. Filters on hosts.txt's role column, same as smoke_test.py's FIXED_ROLES.
  if [ "$INCLUDE_FIXED" -eq 1 ]; then
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
  else
    mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" \
      | awk 'NF && $2 != "fullnode" && $2 != "explorer" && $2 != "controller" {print $1}')
  fi
fi

[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

# Without curl the readiness probe silently fails for every host: already-up
# nodes are never skipped, and healthy nodes get reported as "not answering".
command -v curl >/dev/null 2>&1 || {
  echo "ERROR: curl is not installed on this controller, so readiness can't be checked."
  echo "       Install it first:  sudo apt install -y curl"
  exit 1
}

if [ "$FORCE" -eq 1 ]; then
  echo "Checking ${#HOSTS[@]} host(s), ${JOBS} at a time - --force set, bouncing every one regardless of current state..."
else
  echo "Checking ${#HOSTS[@]} host(s), ${JOBS} at a time - already-up ones are left alone, only down ones get restarted..."
fi
echo "(output is buffered per host and printed together when all are done)"
echo

# Handles one host. Runs in a background subshell, so it communicates
# results ONLY via its exit code (array appends in a subshell wouldn't
# propagate back to the parent): 0 = restarted and answering,
# 2 = already up and left alone, 1 = needs attention.
restart_one() {
  local ip="$1"

  if [ "$FORCE" -eq 0 ] && curl -s -o /dev/null --max-time 3 "http://${ip}:20000/rubix/v1/dids" 2>/dev/null; then
    echo "  already up, skipping (use --force to bounce it anyway)"
    return 2
  fi

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$REMOTE_DIR" "$NODE_NAME" <<'REMOTE_SCRIPT'
set -uo pipefail
REMOTE_DIR="$1"; NODE_NAME="$2"

if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
  echo "  systemd unit found, restarting it"
  sudo systemctl restart rubixgoplatform
else
  echo "  no systemd unit - stopping any existing instance first"
  # A real restart, not just start-if-nothing's-running: if a previous
  # attempt (manual, or an earlier run of this script) left a process still
  # bound to the API port - even a half-broken one - a fresh launch fails
  # immediately with "address already in use" and the readiness check below
  # ends up polling a stale process instead of the new one.
  pkill -f "rubixgoplatform run" >/dev/null 2>&1 || true
  sleep 2
  echo "  launching detached"
  eval "cd ${REMOTE_DIR}"
  nohup ./rubixgoplatform run -p "$NODE_NAME" > node-restart.log 2>&1 < /dev/null &
  disown
fi
REMOTE_SCRIPT
  then
    echo "  FAILED to launch"
    return 1
  fi

  echo "  waiting ${INITIAL_WAIT}s, then checking every ${POLL_INTERVAL}s (up to ${ATTEMPTS}x)..."
  local up=0 started_at elapsed attempt
  started_at=$(date +%s)
  sleep "$INITIAL_WAIT"
  for ((attempt = 1; attempt <= ATTEMPTS; attempt++)); do
    if curl -s -o /dev/null --max-time 2 "http://${ip}:20000/rubix/v1/dids" 2>/dev/null; then
      up=1; break
    fi
    [ "$attempt" -lt "$ATTEMPTS" ] && sleep "$POLL_INTERVAL"
  done
  elapsed=$(( $(date +%s) - started_at ))
  if [ "$up" -eq 1 ]; then
    echo "  up and answering (${elapsed}s)"
    return 0
  fi
  echo "  WARNING: not answering after ${elapsed}s — check: ssh ${SSH_USER}@${ip} journalctl -u rubixgoplatform -n 100"
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
    # and the host's real result is lost. Likewise `wait -n` propagates a
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
echo "${#HOSTS[@]} host(s) total: $RESTARTED restarted and answering, ${#SKIPPED[@]} already up (skipped), ${#FAILED[@]} need attention."
if [ "${#FAILED[@]}" -gt 0 ]; then
  echo "Needs attention: ${FAILED[*]}"
  exit 1
fi
