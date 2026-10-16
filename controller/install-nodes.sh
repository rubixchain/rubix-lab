#!/usr/bin/env bash
# Rubix lab — install the nodes on lab machines, from the controller.
#
# Per machine: copies systems/ (setup.sh, the binaries, the swarm key,
# node-layout.txt) to ~/Desktop/rubix/rubix-lab/systems/ there, then runs setup.sh over
# `ssh -t` for the machine's role in hosts.txt. No git clone is needed on the
# machines. Explorer and controller machines run no node and are skipped.
#
# One machine at a time, on purpose: setup.sh needs sudo (systemd unit,
# sudoers grant), so each machine asks for its password once, here.
#
# The bootstrap list is fixed: setup.sh takes it from systems/bootstrap-nodes.txt
# (the four fullnodes). Re-running with --force applies a changed list to
# machines already installed; restart their nodes afterwards.
#
# Machines where every node already answers are skipped unless --force.
#
# Usage:
#   ./install-nodes.sh --role fullnode
#   ./install-nodes.sh --role faucet --role faucet-quorum
#   ./install-nodes.sh --role pool                       # every pool machine
#   ./install-nodes.sh --host 192.168.1.107              # one machine, whatever its role
#   ./install-nodes.sh --host 192.168.1.107 --slots "0 1"   # only some of its nodes
#   ./install-nodes.sh --role fullnode --force           # e.g. to apply a new bootstrap list

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
# The lab folder on every machine, relative to its home (fleet.py LAB_DIR).
LAB="Desktop/rubix/rubix-lab"
ROLES=()
SINGLE_HOST=""
SLOTS=""
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --role) ROLES+=("$2"); shift 2 ;;
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --slots) SLOTS="$2"; shift 2 ;;
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

peers="$(sed 's/#.*//' "$REPO/systems/bootstrap-nodes.txt" 2>/dev/null | grep -c '/p2p/' || true)"
echo "Bootstrap list: ${peers:-0} peer(s) in systems/bootstrap-nodes.txt"

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

  echo "  copying systems/ to ~/${LAB}/systems/ ..."
  copied=0
  if ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" "mkdir -p ~/${LAB}"; then
    # rsync sends only what changed (the binaries are ~100MB); scp if either
    # side lacks it.
    if command -v rsync >/dev/null 2>&1 \
       && ssh -o BatchMode=yes "${SSH_USER}@${ip}" 'command -v rsync' >/dev/null 2>&1; then
      rsync -a "$REPO/systems/" "${SSH_USER}@${ip}:${LAB}/systems/" && copied=1
    else
      scp -rq "$REPO/systems" "${SSH_USER}@${ip}:${LAB}/" && copied=1
    fi
  fi
  if [ "$copied" -ne 1 ]; then
    echo "  ERROR: could not copy to $ip (SSH keys set up? see setup-ssh.sh)"
    FAILED+=("$ip"); echo; continue
  fi

  if ssh -t -o ConnectTimeout=8 "${SSH_USER}@${ip}" \
       "cd ~/${LAB}/systems/install && chmod +x setup.sh && SLOTS='${SLOTS}' ./setup.sh ${role}"; then
    OK+=("$ip")
  else
    FAILED+=("$ip")
  fi
  echo
done

echo "----------------------------------------"
echo "${#TARGETS[@]} machine(s): ${#OK[@]} installed, ${#SKIPPED[@]} already up, ${#FAILED[@]} need attention."
[ "${#FAILED[@]}" -eq 0 ] || { echo "Needs attention: ${FAILED[*]}"; exit 1; }
# Check only what this run touched: a bare check-nodes.py covers every pool
# machine, and before the pool is installed that is a wall of DOWN.
DONE=("${OK[@]}" "${SKIPPED[@]}")
if [ "${#DONE[@]}" -le 6 ]; then
  echo "Check them:"
  for ip in "${DONE[@]}"; do echo "  ./preflight-check.sh --host $ip"; done
else
  echo "Check them:  ./preflight-check.sh --include-fixed   (machines not installed yet show as not ready)"
fi
