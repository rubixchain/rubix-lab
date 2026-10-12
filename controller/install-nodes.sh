#!/usr/bin/env bash
# Rubix lab — install the nodes on lab machines, from the controller.
#
# Per machine: copies systems/ (setup.sh, the binaries, the swarm key,
# node-layout.txt) to ~/rubix-lab/systems/ there, then runs setup.sh over
# `ssh -t` for the machine's role in hosts.txt. No git clone is needed on the
# machines. Explorer and controller machines run no node and are skipped.
#
# One machine at a time, on purpose: setup.sh needs sudo (systemd unit,
# sudoers grant), so each machine asks for its password once, here.
#
# The bootstrap list is built from the fullnodes' own peer IDs, read from
# their APIs, so it is always current. The first fullnode has nothing to join
# yet: install it alone, with --first (empty list).
#
# Machines where every node already answers are skipped unless --force.
#
# Usage:
#   ./install-nodes.sh --host 192.168.1.101 --first      # first fullnode, empty bootstrap
#   ./install-nodes.sh --role fullnode                   # the other fullnodes
#   ./install-nodes.sh --role faucet --role faucet-quorum
#   ./install-nodes.sh --role pool                       # every pool machine
#   ./install-nodes.sh --host 192.168.1.107              # one machine, whatever its role
#   ./install-nodes.sh --host 192.168.1.107 --slots "0 1"   # only some of its nodes

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
ROLES=()
SINGLE_HOST=""
FIRST=0
SLOTS=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLES+=("$2"); shift 2 ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
    --first) FIRST=1; shift ;;
    --force) FORCE=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "ERROR: curl is needed: sudo apt install -y curl"; exit 1; }
if [ -z "$SINGLE_HOST" ] && [ "${#ROLES[@]}" -eq 0 ]; then
  echo "Say which machines: --host <ip>, or --role pool|fullnode|faucet|faucet-quorum (repeatable)."
  exit 1
fi
for r in "${ROLES[@]}"; do
  case "$r" in pool|fullnode|faucet|faucet-quorum) ;; *) echo "ERROR: unknown role '$r'"; exit 1 ;; esac
done
[ "$FIRST" -eq 0 ] || [ -n "$SINGLE_HOST" ] || { echo "ERROR: --first goes with --host <the first fullnode>."; exit 1; }
[ -z "$SLOTS" ] || [ -n "$SINGLE_HOST" ] || { echo "ERROR: --slots goes with --host."; exit 1; }

fleet() { python3 "$HERE/fleet.py" "$@" --hosts "$HOSTS_FILE"; }

# "ip role" per target machine. fleet.py stops with a clear message on a bad
# hosts.txt or a --host that is not in it; the assignment below then stops too.
if [ -n "$SINGLE_HOST" ]; then
  listing="$(fleet hosts --host "$SINGLE_HOST")"
else
  listing="$(fleet hosts --include-fixed --with-nodes)"
fi
TARGETS=()
while read -r ip role; do
  [ -n "$ip" ] || continue
  if [ -z "$SINGLE_HOST" ]; then
    keep=""
    for r in "${ROLES[@]}"; do [ "$r" = "$role" ] && keep=1; done
    [ -n "$keep" ] || continue
  fi
  case "$role" in
    explorer|controller) echo "$ip is the $role - it runs no node, skipped."; continue ;;
  esac
  TARGETS+=("$ip $role")
done <<<"$listing"
[ "${#TARGETS[@]}" -gt 0 ] || { echo "No machines selected."; exit 1; }

mapfile -t FULLNODES < <(fleet hosts --include-fixed | awk '$2 == "fullnode" {print $1}')

peer_id() {   # the node's libp2p peer ID - the API returns it as "message"
  curl -s --max-time 5 "http://$1:20000/rubix/v1/node/peer_id" 2>/dev/null | python3 -c '
import json, sys
try:
    r = json.load(sys.stdin)
except Exception:
    sys.exit(0)
print(r.get("message") or "" if isinstance(r, dict) else "")' 2>/dev/null || true
}

bootstrap_for() {   # $1 = target ip -> JSON list of every OTHER fullnode that answers
  local entries=() ip peer
  for ip in "${FULLNODES[@]}"; do
    [ "$ip" = "$1" ] && continue
    peer="$(peer_id "$ip")"
    case "$peer" in
      12D3Koo*|Qm*) entries+=("\"/ip4/${ip}/tcp/4002/p2p/${peer}\"") ;;
      *) echo "  WARNING: fullnode $ip did not give a peer ID - left out of the bootstrap list" >&2 ;;
    esac
  done
  local IFS=,
  echo "[${entries[*]}]"
}

all_up() {   # $1 = ip: every node of this machine answers on its API port
  local ip slot index api rest
  while read -r ip slot index api rest; do
    [ -n "$ip" ] || continue
    curl -s -o /dev/null --max-time 3 "http://${ip}:${api}/rubix/v1/node/ping" || return 1
  done <<<"$(fleet nodes --host "$1")"
  return 0
}

echo "Installing on ${#TARGETS[@]} machine(s), one at a time (each asks for its sudo password once)."
echo

OK=(); FAILED=(); SKIPPED=()
for t in "${TARGETS[@]}"; do
  read -r ip role <<<"$t"
  echo "== $ip ($role) =="
  if [ "$FORCE" -eq 0 ] && [ -z "$SLOTS" ] && all_up "$ip"; then
    echo "  every node already answers - skipped (--force to run setup.sh anyway)"
    SKIPPED+=("$ip"); echo; continue
  fi

  if [ "$FIRST" -eq 1 ]; then
    BOOT='[]'
  else
    BOOT="$(bootstrap_for "$ip")"
    if [ "$BOOT" = "[]" ]; then
      echo "  ERROR: no fullnode answered, so there is nothing to join. Install the first"
      echo "         fullnode with: ./install-nodes.sh --host <its ip> --first"
      FAILED+=("$ip"); echo; continue
    fi
  fi
  echo "  bootstrap: $BOOT"

  echo "  copying systems/ to ~/rubix-lab/systems/ ..."
  copied=0
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" 'mkdir -p ~/rubix-lab'; then
    # rsync sends only what changed (the binaries are ~100MB); scp if either
    # side lacks it.
    if command -v rsync >/dev/null 2>&1 \
       && ssh -o BatchMode=yes "${SSH_USER}@${ip}" 'command -v rsync' >/dev/null 2>&1; then
      rsync -a "$REPO/systems/" "${SSH_USER}@${ip}:rubix-lab/systems/" && copied=1
    else
      scp -rq "$REPO/systems" "${SSH_USER}@${ip}:rubix-lab/" && copied=1
    fi
  fi
  if [ "$copied" -ne 1 ]; then
    echo "  ERROR: could not copy to $ip (SSH keys set up? see setup-ssh.sh)"
    FAILED+=("$ip"); echo; continue
  fi

  # BOOT is JSON with double quotes and no single quotes, so single-quoting it
  # carries it through the remote shell unchanged.
  if ssh -t -o ConnectTimeout=8 "${SSH_USER}@${ip}" \
       "cd ~/rubix-lab/systems/install && chmod +x setup.sh && BOOTSTRAP_NODES='${BOOT}' SLOTS='${SLOTS}' ./setup.sh ${role}"; then
    OK+=("$ip")
  else
    FAILED+=("$ip")
  fi
  echo
done

echo "----------------------------------------"
echo "${#TARGETS[@]} machine(s): ${#OK[@]} installed, ${#SKIPPED[@]} already up, ${#FAILED[@]} need attention."
[ "${#FAILED[@]}" -eq 0 ] || { echo "Needs attention: ${FAILED[*]}"; exit 1; }
echo "Next: python3 check-nodes.py   (then dids-to-excel.py to create the DIDs)"
