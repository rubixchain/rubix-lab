#!/usr/bin/env bash
# Rubix lab — pre-test readiness check across the whole fleet.
#
# Answers one question: "is every machine actually ready to be tested against?"
# One row per machine; a machine runs several nodes, so the node columns are
# counts (9/9). Checks both layers, because either one silently breaks a run:
#
#   SYSTEM layer (one SSH round trip per machine)
#     ssh      - controller can log in without a password (setup-ssh.sh done)
#     docker   - docker.service enabled AND active (enabled matters: without
#                it the DB containers never come back after a reboot)
#     clock    - NTP-synchronised. The product allows NO clock difference: a
#                sender a second ahead of its quorum is refused
#                (core/consensus/checks.go:61, Epoch > time.Now()).
#     units    - rubixgoplatform@<slot> enabled AND active, per node
#     pgsql    - Postgres container rubix-db-<slot> running with restart policy
#                'always' (Docker's policy, not systemd's), per node
#     bin      - the node's binary is where its unit expects it
#     sudo     - passwordless sudo for systemctl on this machine's units, which
#                restart-nodes.sh and update-exec.sh need (tested with sudo -n,
#                so it fails rather than hanging on a password prompt)
#
#   API layer (over HTTP, from the controller)
#     api      - each node answers on its own port
#     dids     - DIDs held against the layout's target (0 right after a wipe;
#                dids-to-excel.py creates the rest). Shown, not counted in
#                "ready": a node below target is still usable.
#
# Read-only: this script changes nothing anywhere. Safe to run any time.
#
# Usage:
#   ./preflight-check.sh                        # pool machines
#   ./preflight-check.sh --include-fixed        # plus fullnodes and faucet machines
#   ./preflight-check.sh --host 192.168.1.107   # one machine (any role)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
SINGLE_HOST=""
INCLUDE_FIXED=0
JOBS=0   # 0 = all at once

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
# Without curl every API check silently fails and EVERY node gets reported as
# down - which looks like a fleet-wide outage rather than a missing tool.
command -v curl >/dev/null 2>&1 || {
  echo "ERROR: curl is not installed on this controller, so no API check can run."
  echo "       Install it first:  sudo apt install -y curl"
  exit 1
}

SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
[ "$INCLUDE_FIXED" -eq 1 ] && SELECT+=(--include-fixed)
# "ip slot node_index api_port db_port swarm_port dids role" per node.
listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")"
HOSTS=()
while read -r ip _rest; do
  [ -n "$ip" ] || continue
  case " ${HOSTS[*]-} " in *" $ip "*) ;; *) HOSTS+=("$ip") ;; esac
done <<<"$listing"
[ "${#HOSTS[@]}" -gt 0 ] || { echo "No nodes selected."; exit 1; }
[ "$JOBS" -le 0 ] && JOBS=${#HOSTS[@]}

check_one() {
  local ip="$1" role="" slots=() apis=() targets=() hip slot _i api _db _sw dids r
  while read -r hip slot _i api _db _sw dids r; do
    [ "$hip" = "$ip" ] || continue
    slots+=("$slot"); apis+=("$api"); targets+=("$dids"); role="$r"
  done <<<"$listing"
  local n=${#slots[@]} ssh_ok=FAIL docker_ok=- clock_ok=- units=- pg=- bin=- sudo_ok=- raw
  local details=()

  # --- system layer, one SSH round trip emitting key=value lines ---
  if raw=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" \
             bash -s -- "${slots[@]}" 2>/dev/null <<'REMOTE'
v() { "$@" 2>/dev/null; }
echo "docker=$(v systemctl is-enabled docker)/$(v systemctl is-active docker)"
echo "clock=$(v timedatectl show -p NTPSynchronized --value)"
for s in "$@"; do
  echo "unit.$s=$(v systemctl is-enabled "rubixgoplatform@$s")/$(v systemctl is-active "rubixgoplatform@$s")"
  echo "pg.$s=$(v docker inspect -f '{{.State.Running}}/{{.HostConfig.RestartPolicy.Name}}' "rubix-db-$s")"
  if [ -x ~/rubix-lab/nodes/node$s/rubixgoplatform ]; then echo "bin.$s=yes"; else echo "bin.$s=no"; fi
done
# `sudo -n -l <cmd>` asks "am I allowed to run this?" WITHOUT running it; -n
# fails instead of prompting. It must be a command the grant covers.
if sudo -n -l "$(command -v systemctl)" restart "rubixgoplatform@$1" >/dev/null 2>&1; then
  echo "sudo=yes"; else echo "sudo=no"; fi
REMOTE
        ); then
    ssh_ok=ok
    val() { sed -n "s/^$1=//p" <<<"$raw"; }
    [ "$(val docker)" = "enabled/active" ] && docker_ok=ok || docker_ok="FAIL"
    case "$(val clock)" in
      yes) clock_ok=ok ;;
      no) clock_ok=FAIL; details+=("clock not NTP-synchronised: sudo timedatectl set-ntp true") ;;
      *) clock_ok="?" ;;
    esac
    [ "$(val sudo)" = "yes" ] && sudo_ok=ok || { sudo_ok=FAIL; details+=("no passwordless sudo for the node units - re-run setup.sh there"); }
    local u=0 p=0 b=0 s
    for s in "${slots[@]}"; do
      if [ "$(val "unit.$s")" = "enabled/active" ]; then u=$((u + 1)); else details+=("node $s unit: $(val "unit.$s")"); fi
      if [ "$(val "pg.$s")" = "true/always" ]; then p=$((p + 1)); else details+=("node $s Postgres rubix-db-$s: $(val "pg.$s" || true)"); fi
      if [ "$(val "bin.$s")" = "yes" ]; then b=$((b + 1)); else details+=("node $s: no binary in ~/rubix-lab/nodes/node$s"); fi
    done
    units="$u/$n"; pg="$p/$n"; bin="$b/$n"
    [ "$docker_ok" = ok ] || details+=("docker not enabled+active: sudo systemctl enable --now docker")
  else
    details+=("no passwordless SSH (run setup-ssh.sh)")
  fi

  # --- API layer, from the controller ---
  local a=0 have=0 want=0 i body c
  for i in "${!slots[@]}"; do
    want=$((want + targets[i]))
    if body=$(curl -s --max-time 5 "http://${ip}:${apis[i]}/rubix/v1/dids" 2>/dev/null) && [ -n "$body" ]; then
      a=$((a + 1))
      c=$(grep -o 'bafybmi[a-zA-Z0-9]*' <<<"$body" | wc -l | tr -d ' ')
      have=$((have + c))
      [ "$c" -le "${targets[i]}" ] || details+=("node ${slots[i]} holds $c DIDs, layout says ${targets[i]} - check by hand")
    else
      details+=("node ${slots[i]} not answering on :${apis[i]}")
    fi
  done

  printf '%-15s %-13s %-4s %-6s %-5s %-5s %-5s %-5s %-4s %-5s %s\n' \
    "$ip" "${role:-pool}" "$ssh_ok" "$docker_ok" "$clock_ok" "$units" "$pg" "$bin" "$sudo_ok" "$a/$n" "$have/$want"
  for d in "${details[@]}"; do echo "      - $d"; done

  # non-zero if anything is not ok (the DID count is shown, not judged)
  [[ "$ssh_ok" == ok && "$docker_ok" == ok && "$clock_ok" != FAIL && "$units" == "$n/$n" \
     && "$pg" == "$n/$n" && "$bin" == "$n/$n" && "$sudo_ok" == ok && "$a" -eq "$n" ]]
}

echo "Checking ${#HOSTS[@]} machine(s), ${JOBS} at a time. Read-only — nothing is changed."
echo
printf '%-15s %-13s %-4s %-6s %-5s %-5s %-5s %-5s %-4s %-5s %s\n' \
  HOST ROLE SSH DOCKER CLOCK UNITS PGSQL BIN SUDO API DIDs
printf '%s\n' "--------------------------------------------------------------------------------------"

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
echo "$(( ${#HOSTS[@]} - ${#NOT_READY[@]} ))/${#HOSTS[@]} machine(s) fully ready."
if [ "${#NOT_READY[@]}" -gt 0 ]; then
  echo "Not ready: ${NOT_READY[*]}"
  echo
  echo "Common fixes:"
  echo "  UNITS/PGSQL/BIN short -> re-run the install there: ./install-nodes.sh --host <ip> --force"
  echo "  API short but UNITS ok -> still starting, or crashed: ssh <host> journalctl -u rubixgoplatform@<slot> -n 100"
  echo "  SUDO fail             -> the grant comes from setup.sh; re-run the install there"
  exit 1
fi
