#!/usr/bin/env bash
# Rubix lab - pick a machine and open its node console, from the controller.
#
# The console (systems/console/rubix-console.sh, on the machine) shows that
# machine's nodes - service, API, DIDs, peers, last log line - and switches
# between their live logs with 0-8 (j = service events, s = status table).
# q in the console brings you back to this list. Nothing is changed anywhere.
#
# Usage:
#   ./fleet-console.sh                         # list the machines, pick one
#   ./fleet-console.sh --host 192.168.1.107    # straight to one machine
#
# Install the console on the machines first: ./install-console.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
SINGLE_HOST=""
# Single-quoted on purpose: the ~ is expanded on the machine, not here.
CONSOLE='~/Desktop/rubix/rubix-lab/systems/console/rubix-console.sh'

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

open_one() {   # $1 = ip
  if ! ssh -t -o ConnectTimeout=8 "${SSH_USER}@$1" "$CONSOLE"; then
    echo "Could not open the console on $1 - is it installed?  ./install-console.sh --host $1"
    read -rsn1 -t 5 _ || true
  fi
}

if [ -n "$SINGLE_HOST" ]; then open_one "$SINGLE_HOST"; exit 0; fi

mapfile -t MACHINES < <(python3 "$HERE/fleet.py" hosts --include-fixed --with-nodes --hosts "$HOSTS_FILE")
[ "${#MACHINES[@]}" -gt 0 ] || { echo "No machines with nodes in hosts.txt."; exit 1; }

while true; do
  printf '\033[H\033[2J'
  echo "Rubix lab - machines with nodes. Pick one to see its nodes and their live logs."
  echo
  for i in "${!MACHINES[@]}"; do
    printf '  %2d) %s\n' $((i + 1)) "${MACHINES[$i]}"
  done | if command -v column >/dev/null 2>&1; then column -c "$(tput cols 2>/dev/null || echo 120)"; else cat; fi
  echo
  read -rp "Number (q to quit): " n
  case "$n" in q|Q) exit 0 ;; esac
  if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "${#MACHINES[@]}" ]; then
    open_one "${MACHINES[$((n - 1))]%% *}"
  fi
done
