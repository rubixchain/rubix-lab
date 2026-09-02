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
# Usage:
#   ./restart-nodes.sh                                  # every host in hosts.txt
#   ./restart-nodes.sh --host 192.168.1.104              # just one
#   ./restart-nodes.sh --remote-dir '~/Desktop/rubix' --node-name node

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
REMOTE_DIR='~/Desktop/rubix'
NODE_NAME="node"
READY_TIMEOUT=30
SINGLE_HOST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --node-name) NODE_NAME="$2"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
fi

echo "Restarting ${#HOSTS[@]} host(s), waiting up to ${READY_TIMEOUT}s each for the API to answer..."
echo

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$REMOTE_DIR" "$NODE_NAME" <<'REMOTE_SCRIPT'
set -uo pipefail
REMOTE_DIR="$1"; NODE_NAME="$2"

if systemctl list-unit-files rubixgoplatform.service >/dev/null 2>&1; then
  echo "  systemd unit found, using it"
  sudo systemctl start rubixgoplatform
else
  echo "  no systemd unit, launching detached"
  eval "cd ${REMOTE_DIR}"
  nohup ./rubixgoplatform run -p "$NODE_NAME" > node-restart.log 2>&1 < /dev/null &
  disown
fi
REMOTE_SCRIPT
  then
    echo "  FAILED to launch on $ip"
    FAILED+=("$ip")
    echo
    continue
  fi

  echo "  waiting for the API..."
  UP=0
  for i in $(seq 1 "$READY_TIMEOUT"); do
    if curl -s -o /dev/null --max-time 2 "http://${ip}:20000/rubix/v1/dids" 2>/dev/null; then
      UP=1; break
    fi
    sleep 1
  done
  if [ "$UP" -eq 1 ]; then
    echo "  up and answering"
  else
    echo "  WARNING: not answering after ${READY_TIMEOUT}s — check node-restart.log on that host"
    FAILED+=("$ip")
  fi
  echo
done

echo "----------------------------------------"
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "All ${#HOSTS[@]} host(s) restarted and answering."
else
  echo "${#FAILED[@]} host(s) need attention: ${FAILED[*]}"
  exit 1
fi
