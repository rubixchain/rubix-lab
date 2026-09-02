#!/usr/bin/env bash
# Rubix lab — pre-test readiness check across the whole fleet.
#
# Answers one question: "is every machine actually ready to be tested against?"
# Checks both layers, because either one silently breaks a test run:
#
#   SYSTEM layer (over SSH)
#     ssh      - controller can log in without a password (setup-ssh.sh done)
#     docker   - docker.service enabled AND active (enabled matters: without
#                it the DB container never comes back after a reboot)
#     pgsql    - the Postgres container is running, with restart policy
#                'always' (that's Docker's policy, NOT systemd - the container
#                comes back because the daemon restarts it)
#     unit     - rubixgoplatform.service enabled AND active
#     sudo     - passwordless sudo works for systemctl on that unit, which is
#                what restart-nodes.sh needs (tested with sudo -n, so it fails
#                rather than hanging on a password prompt)
#     binary   - the rubixgoplatform binary is actually where the unit expects
#
#   API layer (over HTTP, from the controller)
#     api      - the node answers on port 20000
#     dids     - how many DIDs it has (expect exactly 1 for a pool host that's
#                been through dids-to-excel.py; 0 right after a wipe)
#
# Read-only: this script changes nothing anywhere. Safe to run any time.
#
# Usage:
#   ./preflight-check.sh                        # all pool hosts (fixed-role ones excluded)
#   ./preflight-check.sh --host 192.168.1.104    # just one
#   ./preflight-check.sh --include-fixed         # include fullnode/explorer/controller

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
BINARY_DIR='~/Desktop/rubix'
CONTAINER="node"
PORT=20000
SINGLE_HOST=""
INCLUDE_FIXED=0
JOBS=0   # 0 = all at once

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --binary-dir) BINARY_DIR="$2"; shift 2 ;;
    --container) CONTAINER="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
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

[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

check_one() {
  local ip="$1"
  local ssh_ok=FAIL docker_ok=- pg_ok=- unit_ok=- sudo_ok=- bin_ok=- api_ok=FAIL dids="-"
  local raw

  # --- system layer, one SSH round trip emitting key=value lines ---
  if raw=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" \
             bash -s -- "$BINARY_DIR" "$CONTAINER" 2>/dev/null <<'REMOTE'
BINARY_DIR_RAW="$1"; CONTAINER="$2"
NODE_DIR="$(eval echo "$BINARY_DIR_RAW")"
echo "docker_enabled=$(systemctl is-enabled docker 2>/dev/null || echo no)"
echo "docker_active=$(systemctl is-active docker 2>/dev/null || echo no)"
echo "pg_running=$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo no)"
echo "pg_policy=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$CONTAINER" 2>/dev/null || echo none)"
echo "unit_enabled=$(systemctl is-enabled rubixgoplatform 2>/dev/null || echo no)"
echo "unit_active=$(systemctl is-active rubixgoplatform 2>/dev/null || echo no)"
# `sudo -n -l <cmd>` asks "am I allowed to run this?" WITHOUT running it, and
# -n means it fails instead of prompting. Must test a command that is actually
# in the grant (start/stop/restart/daemon-reload/enable) - testing something
# like `systemctl is-active` reports a false failure, since that isn't granted
# and doesn't need sudo in the first place.
if sudo -n -l /usr/bin/systemctl restart rubixgoplatform >/dev/null 2>&1; then echo "sudo_ok=yes"; else echo "sudo_ok=no"; fi
echo "systemctl_path=$(command -v systemctl 2>/dev/null || echo none)"
if [ -x "$NODE_DIR/rubixgoplatform" ]; then echo "binary=yes"; else echo "binary=no"; fi
REMOTE
        ); then
    ssh_ok=ok
    local docker_enabled docker_active pg_running pg_policy unit_enabled unit_active sudo_flag binary sysctl_path
    docker_enabled=$(sed -n 's/^docker_enabled=//p' <<<"$raw")
    docker_active=$(sed -n 's/^docker_active=//p' <<<"$raw")
    pg_running=$(sed -n 's/^pg_running=//p' <<<"$raw")
    pg_policy=$(sed -n 's/^pg_policy=//p' <<<"$raw")
    unit_enabled=$(sed -n 's/^unit_enabled=//p' <<<"$raw")
    unit_active=$(sed -n 's/^unit_active=//p' <<<"$raw")
    sudo_flag=$(sed -n 's/^sudo_ok=//p' <<<"$raw")
    binary=$(sed -n 's/^binary=//p' <<<"$raw")
    sysctl_path=$(sed -n 's/^systemctl_path=//p' <<<"$raw")

    [ "$docker_enabled" = "enabled" ] && [ "$docker_active" = "active" ] && docker_ok=ok || docker_ok="FAIL($docker_enabled/$docker_active)"
    [ "$pg_running" = "true" ] && [ "$pg_policy" = "always" ] && pg_ok=ok || pg_ok="FAIL($pg_running/$pg_policy)"
    [ "$unit_enabled" = "enabled" ] && [ "$unit_active" = "active" ] && unit_ok=ok || unit_ok="FAIL($unit_enabled/$unit_active)"
    # The sudoers grant hardcodes /usr/bin/systemctl; if systemctl lives
    # somewhere else on a host, the grant silently never matches.
    if [ "$sudo_flag" = "yes" ]; then
      sudo_ok=ok
    elif [ -n "$sysctl_path" ] && [ "$sysctl_path" != "/usr/bin/systemctl" ]; then
      sudo_ok="FAIL(systemctl at $sysctl_path)"
    else
      sudo_ok=FAIL
    fi
    [ "$binary" = "yes" ] && bin_ok=ok || bin_ok=FAIL
  fi

  # --- API layer, from the controller ---
  local body
  if body=$(curl -s --max-time 5 "http://${ip}:${PORT}/rubix/v1/dids" 2>/dev/null) && [ -n "$body" ]; then
    api_ok=ok
    dids=$(grep -o 'bafybmi[a-zA-Z0-9]*' <<<"$body" | wc -l | tr -d ' ')
  fi

  printf '%-16s %-5s %-18s %-18s %-18s %-5s %-5s %-5s %s\n' \
    "$ip" "$ssh_ok" "$docker_ok" "$pg_ok" "$unit_ok" "$sudo_ok" "$bin_ok" "$api_ok" "$dids"

  # non-zero if anything is not ok
  [[ "$ssh_ok" == ok && "$docker_ok" == ok && "$pg_ok" == ok && "$unit_ok" == ok \
     && "$sudo_ok" == ok && "$bin_ok" == ok && "$api_ok" == ok ]]
}

echo "Checking ${#HOSTS[@]} host(s), ${JOBS} at a time. Read-only — nothing is changed."
echo
printf '%-16s %-5s %-18s %-18s %-18s %-5s %-5s %-5s %s\n' \
  HOST SSH DOCKER PGSQL UNIT SUDO BIN API DIDs
printf '%s\n' "-------------------------------------------------------------------------------------------------------"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

running=0
for ip in "${HOSTS[@]}"; do
  {
    set +e
    check_one "$ip" > "$WORKDIR/$ip.out" 2>&1
    echo $? > "$WORKDIR/$ip.rc"
  } &
  running=$(( running + 1 ))
  if [ "$running" -ge "$JOBS" ]; then
    wait -n || true
    running=$(( running - 1 ))
  fi
done
wait || true

NOT_READY=()
for ip in "${HOSTS[@]}"; do
  cat "$WORKDIR/$ip.out" 2>/dev/null
  rc="$(cat "$WORKDIR/$ip.rc" 2>/dev/null || echo 1)"
  [ "$rc" = "0" ] || NOT_READY+=("$ip")
done

echo
echo "----------------------------------------"
READY=$(( ${#HOSTS[@]} - ${#NOT_READY[@]} ))
echo "${READY}/${#HOSTS[@]} host(s) fully ready."
if [ "${#NOT_READY[@]}" -gt 0 ]; then
  echo "Not ready: ${NOT_READY[*]}"
  echo
  echo "Common fixes:"
  echo "  DOCKER not enabled  -> ssh <host> sudo systemctl enable --now docker"
  echo "  PGSQL not running   -> the container is gone; re-create it (see LAB-QUICKREF.txt)"
  echo "  UNIT not installed  -> run the systemd block from LAB-QUICKREF.txt section 6 on that host"
  echo "  SUDO fail           -> the sudoers grant is missing; also in LAB-QUICKREF.txt section 6"
  echo "  API fail but UNIT ok-> still starting, or crashed: ssh <host> journalctl -u rubixgoplatform -n 100"
  exit 1
fi
