#!/usr/bin/env bash
# Rubix lab - this machine's nodes at a glance, and each node's live log.
#
# Starts on a status table: every node's service, API, DIDs, peers, log size
# and last log line, refreshed every 5 seconds. Keys (they work at any time):
#   0-8   follow that node's live log
#   j     the same node's service events (journalctl: starts, crashes, restarts)
#   s     back to the status table
#   q     leave - the nodes keep running (they are services, not this window)
#
# It opens by itself on the machine's screen at login (--install writes the
# autostart entry; it is also in the app menu as "Rubix nodes"). From the
# controller:  ./fleet-console.sh  (pick a machine), or
#   ssh -t rubix@<ip> '~/Desktop/rubix/rubix-lab/systems/console/rubix-console.sh'
#
#   rubix-console.sh             the console
#   rubix-console.sh --status    print the status table once and exit
#   rubix-console.sh --install   write the autostart + app-menu entries (no sudo)

set -uo pipefail

LAB="$HOME/Desktop/rubix/rubix-lab"
NODES="$LAB/nodes"
SELF="$LAB/systems/console/rubix-console.sh"

if [ "${1:-}" = "--install" ]; then
  entry="[Desktop Entry]
Type=Application
Name=Rubix nodes
Comment=This machine's Rubix nodes and their live logs
Exec=gnome-terminal --title=\"Rubix nodes\" --maximize -- $SELF
Terminal=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=15"
  mkdir -p "$HOME/.config/autostart" "$HOME/.local/share/applications"
  printf '%s\n' "$entry" > "$HOME/.config/autostart/rubix-console.desktop"
  printf '%s\n' "$entry" > "$HOME/.local/share/applications/rubix-console.desktop"
  echo "  console: opens at every login, and is in the app menu as \"Rubix nodes\""
  exit 0
fi

# This machine's nodes: every nodes/node<slot> with a config.toml. Ports follow
# its node_index the way the product derives them (core/config/config.go):
# API 20000 + i, IPFS API 5002 + i.
SLOTS=()
INDEX=()
for d in "$NODES"/node[0-9]*; do
  [ -f "$d/config.toml" ] || continue
  s="${d##*/node}"
  i="$(sed -n 's/^node_index *= *\([0-9][0-9]*\).*/\1/p' "$d/config.toml" | head -1)"
  SLOTS+=("$s")
  INDEX[$s]="${i:-0}"
done
if [ "${#SLOTS[@]}" -eq 0 ]; then
  echo "No Rubix nodes in $NODES on this machine."
  [ -t 0 ] && { echo "(press a key to close)"; read -rsn1; }
  exit 1
fi

if [ -t 1 ]; then RED=$'\033[31m'; GREEN=$'\033[32m'; DIM=$'\033[2m'; OFF=$'\033[0m'; else RED=""; GREEN=""; DIM=""; OFF=""; fi

status_table() {
  local cols s i api up dids peers svc size last
  cols="$(tput cols 2>/dev/null || echo 120)"
  printf 'Rubix nodes on %s (%s)   %s\n\n' "$(hostname)" "$(hostname -I 2>/dev/null | cut -d' ' -f1)" "$(date '+%Y-%m-%d %H:%M:%S')"
  printf '%-5s %-6s %-9s %-4s %-5s %-6s %-6s %s\n' SLOT PORT SERVICE API DIDS PEERS LOG "LAST LOG LINE"
  for s in "${SLOTS[@]}"; do
    i="${INDEX[$s]}"; api=$((20000 + i))
    svc="$(systemctl is-active "rubixgoplatform@$s" 2>/dev/null)"
    up="$(curl -s -m 1 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$api/rubix/v1/node/peer_id")"
    if [ "$up" = "200" ]; then
      up="${GREEN}up${OFF}  "
      dids="$(curl -s -m 2 "http://127.0.0.1:$api/rubix/v1/dids" | grep -o '"bafybmi' | wc -l)"
      peers="$(curl -s -m 2 -X POST "http://127.0.0.1:$((5002 + i))/api/v0/swarm/peers" | grep -o '"Peer"' | wc -l)"
    else
      up="${RED}DOWN${OFF}"; dids="-"; peers="-"
    fi
    [ "$svc" = "active" ] && svc="${GREEN}active${OFF}   " || svc="${RED}$(printf '%-9s' "${svc:-unknown}")${OFF}"
    size="$(du -h "$NODES/node$s/log.txt" 2>/dev/null | cut -f1)"
    # Time of day only, so the width goes to the message.
    last="$(tail -n 1 "$NODES/node$s/log.txt" 2>/dev/null \
            | sed -E 's/^[0-9]{4}-[0-9]{2}-[0-9]{2}T([0-9:]{8})[.0-9]*[+-][0-9:]+ +/\1 /; s/  +/ /g' \
            | cut -c1-$(( cols > 70 ? cols - 50 : 20 )))"
    printf '%-5s %-6s %s %s %-5s %-6s %-6s %s\n' "$s" "$api" "$svc" "$up" "$dids" "$peers" "${size:--}" "${DIM}${last}${OFF}"
  done
}

if [ "${1:-}" = "--status" ]; then status_table; exit 0; fi

clear_screen() { printf '\033[H\033[2J'; }
FOLLOW=""
stop_follow() {
  if [ -n "$FOLLOW" ]; then kill "$FOLLOW" 2>/dev/null; wait "$FOLLOW" 2>/dev/null; FOLLOW=""; fi
}
follow() {   # $1 = slot, $2 = log | journal
  local api=$((20000 + ${INDEX[$1]}))
  stop_follow; clear_screen
  if [ "$2" = "journal" ]; then
    printf '%s== node %s (port %s): service events ==   0-8 node log  s status  q leave%s\n' "$GREEN" "$1" "$api" "$OFF"
    journalctl -u "rubixgoplatform@$1" -n 30 -f --no-pager &
  else
    printf '%s== node %s (port %s): live log ==   0-8 other node  j service events  s status  q leave%s\n' "$GREEN" "$1" "$api" "$OFF"
    tail -n 40 -F "$NODES/node$1/log.txt" 2>&1 &
  fi
  FOLLOW=$!
}
show_status() {
  clear_screen
  status_table
  printf '\n%sKeys: 0-8 follow a node'"'"'s log   j its service events   s this table   q leave (the nodes keep running)%s\n' "$DIM" "$OFF"
}

trap 'stop_follow; printf "%s\n" "$OFF"; exit 0' INT TERM HUP
MODE="status"; CUR="${SLOTS[0]}"; tick=0
show_status
while true; do
  if read -rsn1 -t 1 key; then
    case "$key" in
      [0-9]) if [ -n "${INDEX[$key]+set}" ]; then CUR="$key"; MODE="log"; follow "$CUR" log; fi ;;
      j|J) MODE="journal"; follow "$CUR" journal ;;
      s|S) stop_follow; MODE="status"; show_status; tick=0 ;;
      q|Q) stop_follow; exit 0 ;;
    esac
  fi
  if [ "$MODE" = "status" ]; then
    tick=$((tick + 1))
    [ "$tick" -ge 5 ] && { show_status; tick=0; }
  fi
done
