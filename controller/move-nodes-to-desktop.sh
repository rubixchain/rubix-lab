#!/usr/bin/env bash
# Rubix lab - move every machine's lab folder from ~/rubix-lab to
# ~/Desktop/rubix/rubix-lab, where the controller keeps its repo (2026-10-16).
#
# Per machine, one at a time:
#   1. stop its nodes (systemctl stop, through the sudo grant setup.sh
#      installed) and wait until no node or IPFS process runs from the old
#      folder
#   2. mv ~/rubix-lab ~/Desktop/rubix/rubix-lab. Everything a node is moves
#      with it: DID keys (testnet/), peer ID (.ipfs), config.toml, binary. The
#      Postgres containers are not touched (named volumes, no paths).
#   3. install-nodes.sh --host <ip> --force: setup.sh, run from the new folder,
#      points the systemd unit at it and starts the nodes again (asks this
#      machine's sudo password once), then waits for every API.
# Nothing a node stores holds its folder path - the product derives every data
# folder from -p at startup (core/config/config.go:122-140) - so a moved node
# comes back with the same DIDs, tokens and peer ID.
#
# Safe to re-run. A machine already moved is restarted from the new folder
# only if its unit still points at the old one or a node is down; a machine
# with nothing installed is skipped. Refuses a machine that has BOTH folders.
# If a node will not stop, nothing is moved and the nodes are started again.
#
# Usage:
#   ./move-nodes-to-desktop.sh --host 192.168.1.101    # one machine first
#   ./move-nodes-to-desktop.sh --include-fixed         # every machine
#   ./move-nodes-to-desktop.sh                         # pool machines only
#
# The faucet quorum's quorum setup lives in its node's memory: after the
# faucet-quorum machine moves, run  python3 faucet-setup.py  again (once the
# faucet is wired it does not mint, it only redoes the quorum setup).

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
SINGLE_HOST=""
INCLUDE_FIXED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
[ "$INCLUDE_FIXED" -eq 1 ] && SELECT+=(--include-fixed)
hosts_listing="$(python3 "$HERE/fleet.py" hosts --with-nodes "${SELECT[@]}")"
nodes_listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")"
[ -n "$hosts_listing" ] || { echo "No machines selected."; exit 1; }

# Runs ON the machine; arguments are its slots. Last line: STATE=<what it found>.
# (No apostrophes in this block: it is one single-quoted string.)
REMOTE='
set -uo pipefail
OLD="$HOME/rubix-lab"
NEW="$HOME/Desktop/rubix/rubix-lab"
UNIT=/etc/systemd/system/rubixgoplatform@.service
has_nodes() { compgen -G "$1/nodes/node[0-9]*" >/dev/null; }
start_again() { for s in "$@"; do sudo -n systemctl start "rubixgoplatform@$s" 2>/dev/null; done; }

if [ -e "$OLD" ] && [ -e "$NEW" ]; then
  echo "  ERROR: both $OLD and $NEW exist - nothing done. Look at both by hand:"
  echo "         $OLD: $(ls -A "$OLD" | tr "\n" " ")"
  echo "         $NEW: $(ls -A "$NEW" | tr "\n" " ")"
  exit 1
fi
if [ ! -e "$OLD" ]; then
  if has_nodes "$NEW"; then
    if grep -q "$NEW/nodes/" "$UNIT" 2>/dev/null; then echo "STATE=in-place"; else echo "STATE=moved"; fi
  else
    echo "STATE=none"
  fi
  exit 0
fi

if has_nodes "$OLD"; then
  echo "  stopping node(s) $*"
  for s in "$@"; do
    [ -d "$OLD/nodes/node$s" ] || continue
    if ! sudo -n systemctl stop "rubixgoplatform@$s"; then
      echo "  ERROR: could not stop rubixgoplatform@$s (sudo grant missing?) - nothing moved"
      start_again "$@"; exit 1
    fi
  done
  # Node processes and their IPFS daemons must be gone before the folder moves.
  left=""
  for _ in $(seq 1 30); do
    left=""
    for p in $(pgrep -f "rubixgoplatform run|ipfs daemon"); do
      case "$(readlink /proc/$p/cwd 2>/dev/null)" in "$OLD"/*) left="$left $p" ;; esac
    done
    [ -z "$left" ] && break
    sleep 1
  done
  if [ -n "$left" ]; then
    echo "  ERROR: still running from $OLD after 30s - nothing moved, nodes started again:"
    for p in $left; do echo "         $p $(tr "\0" " " < /proc/$p/cmdline 2>/dev/null)"; done
    start_again "$@"; exit 1
  fi
fi

if ! { mkdir -p "$HOME/Desktop/rubix" && mv "$OLD" "$NEW"; }; then
  echo "  ERROR: could not move $OLD to $NEW - nodes started again from the old folder"
  start_again "$@"; exit 1
fi
echo "  moved $OLD -> $NEW"
if has_nodes "$NEW"; then echo "STATE=moved"; else echo "STATE=moved-empty"; fi
'

all_up() {   # $1 = ip: every node of this machine answers on its API port
  local ip slot index api rest
  while read -r ip slot index api rest; do
    [ "$ip" = "$1" ] || continue
    curl -s -o /dev/null --max-time 3 "http://${ip}:${api}/rubix/v1/node/ping" || return 1
  done <<<"$nodes_listing"
  return 0
}

MOVED=(); SKIPPED=(); FAILED=(); FAUCET_MOVED=0
while read -r ip role; do
  [ -n "$ip" ] || continue
  slots=()
  while read -r hip slot _rest; do [ "$hip" = "$ip" ] && slots+=("$slot"); done <<<"$nodes_listing"
  echo "== $ip ($role) =="
  if out="$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "${slots[@]}" <<<"$REMOTE" 2>&1)"; then :; fi
  grep -v '^STATE=' <<<"$out" || true
  state="$(sed -n 's/^STATE=//p' <<<"$out")"
  restart=0
  case "$state" in
    none)
      echo "  nothing installed here - skipped (install-nodes.sh installs straight into the new folder)"
      SKIPPED+=("$ip") ;;
    moved-empty)
      echo "  no nodes in the old folder - moved it, nothing to restart"
      MOVED+=("$ip") ;;
    in-place)
      if all_up "$ip"; then
        echo "  already in ~/Desktop/rubix/rubix-lab and every node answers - skipped"
        SKIPPED+=("$ip")
      else
        echo "  already moved, but a node is down - restarting from the new folder"
        restart=1
      fi ;;
    moved)
      restart=1 ;;
    *)
      echo "  (machine left as it was)"
      FAILED+=("$ip") ;;
  esac
  if [ "$restart" -eq 1 ]; then
    echo "  starting the nodes from the new folder (asks this machine's sudo password once)..."
    if "$HERE/install-nodes.sh" --host "$ip" --force --hosts "$HOSTS_FILE" --user "$SSH_USER"; then
      MOVED+=("$ip")
      case "$role" in faucet|faucet-quorum) FAUCET_MOVED=1 ;; esac
    else
      echo "  ERROR: the nodes did not all come back - see the output above. The folder is"
      echo "         already moved; run this again for $ip once the cause is fixed."
      FAILED+=("$ip")
    fi
  fi
  echo
done <<<"$hosts_listing"

echo "----------------------------------------"
echo "moved: ${#MOVED[@]}   skipped: ${#SKIPPED[@]}   need attention: ${#FAILED[@]}"
[ "${#FAILED[@]}" -eq 0 ] || echo "Needs attention: ${FAILED[*]}"
if [ "$FAUCET_MOVED" -eq 1 ]; then
  echo
  echo "A faucet machine moved, so its quorum setup is gone. Wire the faucet again:"
  echo "  cd $HERE && python3 faucet-setup.py"
fi
[ "${#FAILED[@]}" -eq 0 ]
