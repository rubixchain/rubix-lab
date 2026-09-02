#!/usr/bin/env bash
# Rubix lab — install/enable the systemd unit on every host, fleet-wide,
# from the controller over SSH. install/setup.sh already does this as part
# of a full bring-up; this script is for retrofitting it onto machines that
# were brought up by hand (./rubixgoplatform run -p node in a terminal),
# which is how this fleet actually got started. Once a host is on systemd,
# restart-nodes.sh already prefers `systemctl restart` over the fragile
# nohup/disown path automatically - no changes needed there.
#
# Uses the SAME template as install/setup.sh (install/rubixgoplatform.service)
# via the same sed substitution, copied to each host and rendered there, so
# there's one source of truth for the unit file rather than a second copy
# that can drift.
#
# Per host:
#   1. Stop whatever's running now (systemd or the ad hoc process) so
#      nothing is fighting over the API port when the service starts.
#   2. Confirm the binary actually exists at the expected node dir - refuses
#      to install a unit pointing at a path with nothing there.
#   3. Copy + render the unit template, install it, daemon-reload,
#      enable --now.
#   4. Poll the API to confirm it actually came up.
#
# Usage:
#   ./install-systemd-unit.sh                              # every host in hosts.txt
#   ./install-systemd-unit.sh --host 192.168.1.104          # just one — do this first
#   ./install-systemd-unit.sh --remote-dir '~/Desktop/rubix/node'

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/.." && pwd)"
TEMPLATE="$REPO_ROOT/install/rubixgoplatform.service"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
REMOTE_DIR='~/Desktop/rubix/node'
READY_TIMEOUT=90
SINGLE_HOST=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --ready-timeout) READY_TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

[ -f "$TEMPLATE" ] || { echo "ERROR: $TEMPLATE not found."; exit 1; }

if [ -n "$SINGLE_HOST" ]; then
  HOSTS=("$SINGLE_HOST")
else
  [ -f "$HOSTS_FILE" ] || { echo "ERROR: $HOSTS_FILE not found."; exit 1; }
  mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')
fi

echo "Installing the systemd unit on ${#HOSTS[@]} host(s)..."
echo

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="

  if ! scp -o ConnectTimeout=8 -q "$TEMPLATE" "${SSH_USER}@${ip}:/tmp/rubixgoplatform.service.template"; then
    echo "  FAILED to copy the unit template"
    FAILED+=("$ip")
    echo
    continue
  fi

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$REMOTE_DIR" <<'REMOTE_SCRIPT'
set -euo pipefail
REMOTE_DIR="$1"
NODE_DIR="$(eval echo "$REMOTE_DIR")"

if [ ! -x "$NODE_DIR/rubixgoplatform" ]; then
  echo "  ERROR: no rubixgoplatform binary at $NODE_DIR — refusing to install a unit pointing nowhere"
  exit 1
fi

echo "  stopping whatever's currently running..."
sudo systemctl stop rubixgoplatform >/dev/null 2>&1 || true
pkill -f "rubixgoplatform run" >/dev/null 2>&1 || true
sleep 2

echo "  installing the unit (NODE_DIR=$NODE_DIR, USER=$(whoami))..."
sed -e "s#{{NODE_DIR}}#$NODE_DIR#g" -e "s#{{USER}}#$(whoami)#g" \
  /tmp/rubixgoplatform.service.template | sudo tee /etc/systemd/system/rubixgoplatform.service >/dev/null
rm -f /tmp/rubixgoplatform.service.template

sudo systemctl daemon-reload
sudo systemctl enable --now rubixgoplatform
echo "  enabled and started"
REMOTE_SCRIPT
  then
    echo "  FAILED to install/start the unit"
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
    echo "  WARNING: not answering after ${READY_TIMEOUT}s — check: ssh ${SSH_USER}@${ip} journalctl -u rubixgoplatform -n 100"
    FAILED+=("$ip")
  fi
  echo
done

echo "----------------------------------------"
if [ "${#FAILED[@]}" -eq 0 ]; then
  echo "All ${#HOSTS[@]} host(s) now systemd-managed and answering."
else
  echo "${#FAILED[@]} host(s) need attention: ${FAILED[*]}"
  exit 1
fi
