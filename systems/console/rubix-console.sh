#!/usr/bin/env bash
# Rubix lab - this machine's nodes at a glance, and each node's live log.
#
# Starts on a status table: every node's service, API, DIDs, peers, log size
# and last log line, refreshed every 5 seconds. While a test run uses one of
# this machine's nodes, it follows that node's live log by itself - the first
# of them, when the run uses several here - and goes back to the table when
# the run is done with them. (The controller says which in nodes/in-test, a
# line "slot role unit" per node in use: controller/test-plan/full-test/
# node_screens.py.) Keys (they work at any time; a key takes the console out
# of that for 5 minutes, or until "a"):
#   0-8   follow that node's live log
#   j     the same node's service events (journalctl: starts, crashes, restarts)
#   s     back to the status table
#   a     back to following what the test uses
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
IN_TEST="$NODES/in-test"
STALE=600            # an in-test older than this is left by a run that died
HAND=300             # seconds a key keeps the console in the viewer's hands

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

# API, DIDs and peers of every node in one python3 run (python3 is on every
# Ubuntu install; curl is not). One line per node: "slot up|down dids peers".
# node/peer_id is the liveness check: node/ping pings ANOTHER peer and logs
# an ERROR when called bare (server/basic.go:49).
probe_nodes() {
  local args=() s
  for s in "${SLOTS[@]}"; do args+=("$s:${INDEX[$s]}"); done
  python3 - "${args[@]}" <<'PY' 2>/dev/null
import json, sys, urllib.request

def get(url, post=False):
    try:
        req = urllib.request.Request(url, data=b"" if post else None)
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read() or b"null")
    except Exception:
        return None

for arg in sys.argv[1:]:
    slot, index = arg.split(":")
    api = "http://127.0.0.1:%d" % (20000 + int(index))
    if get(api + "/rubix/v1/node/peer_id") is None:
        print(slot, "down", "-", "-")
        continue
    d = get(api + "/rubix/v1/dids")
    p = get("http://127.0.0.1:%d/api/v0/swarm/peers" % (5002 + int(index)), post=True)
    dids = len(d.get("result") or []) if isinstance(d, dict) else "-"
    peers = len(p.get("Peers") or []) if isinstance(p, dict) else "-"
    print(slot, "up", dids, peers)
PY
}

status_table() {
  local cols s i api up dids peers svc size last
  local -A P_UP=() P_DIDS=() P_PEERS=()
  cols="$(tput cols 2>/dev/null || echo 120)"
  while read -r s up dids peers; do
    [ -n "$s" ] || continue
    P_UP[$s]="$up"; P_DIDS[$s]="$dids"; P_PEERS[$s]="$peers"
  done < <(probe_nodes)
  printf 'Rubix nodes on %s (%s)   %s\n\n' "$(hostname)" "$(hostname -I 2>/dev/null | cut -d' ' -f1)" "$(date '+%Y-%m-%d %H:%M:%S')"
  printf '%-5s %-6s %-9s %-4s %-5s %-6s %-6s %s\n' SLOT PORT SERVICE API DIDS PEERS LOG "LAST LOG LINE"
  for s in "${SLOTS[@]}"; do
    i="${INDEX[$s]}"; api=$((20000 + i))
    svc="$(systemctl is-active "rubixgoplatform@$s" 2>/dev/null)"
    if [ "${P_UP[$s]:-down}" = "up" ]; then
      up="${GREEN}up${OFF}  "; dids="${P_DIDS[$s]}"; peers="${P_PEERS[$s]}"
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
follow() {   # $1 = slot, $2 = log | journal, $3 = why it is shown (optional)
  local api=$((20000 + ${INDEX[$1]}))
  stop_follow; clear_screen
  if [ "$2" = "journal" ]; then
    printf '%s== node %s (port %s): service events ==   0-8 node log  s status  a test  q leave%s\n' "$GREEN" "$1" "$api" "$OFF"
    journalctl -u "rubixgoplatform@$1" -n 30 -f --no-pager &
  else
    printf '%s== node %s (port %s): live log ==   0-8 other node  j service events  s status  a test  q leave%s\n' "$GREEN" "$1" "$api" "$OFF"
    [ -n "${3:-}" ] && printf '%s%s%s\n' "$DIM" "$3" "$OFF"
    tail -n 40 -F "$NODES/node$1/log.txt" 2>&1 &
  fi
  FOLLOW=$!
}

in_test() {   # the "slot role unit" lines of the nodes a test run uses now
  local age
  [ -f "$IN_TEST" ] || return 0
  age=$(( $(date +%s) - $(stat -c %Y "$IN_TEST" 2>/dev/null || echo 0) ))
  [ "$age" -le "$STALE" ] && cat "$IN_TEST" 2>/dev/null
  return 0
}

# Follow the node the test uses: the one already followed while it is still in
# use, else the first listed. None in use: back to the table.
AUTO=""
auto_follow() {
  local s role unit first="" keep="" slots=()
  while read -r s role unit; do
    case "$s" in ''|*[!0-9]*) continue ;; esac
    [ -n "${INDEX[$s]+set}" ] || continue
    slots+=("$s")
    [ -z "$first" ] && first="$s $role $unit"
    [ "$s $role $unit" = "$AUTO" ] && keep="$AUTO"
  done <<<"$(in_test)"
  local want="${keep:-$first}"
  if [ -z "$want" ]; then
    if [ -n "$AUTO" ] || [ "$MODE" != "status" ]; then
      AUTO=""; stop_follow; MODE="status"; show_status; tick=0
    fi
    return
  fi
  [ "$want" = "$AUTO" ] && return
  AUTO="$want"
  set -- $want
  local others=() o
  for o in "${slots[@]}"; do [ "$o" = "$1" ] || others+=("$o"); done
  CUR="$1"; MODE="log"
  follow "$1" log "The test is using this node: $2, ${*:3}.${others[*]:+  Also in use here: nodes ${others[*]}.}"
}
show_status() {
  clear_screen
  status_table
  printf '\n%sKeys: 0-8 follow a node'"'"'s log   j its service events   s this table   a follow the test   q leave (the nodes keep running)%s\n' "$DIM" "$OFF"
  printf '%sWhile a test uses a node here, its live log shows by itself.%s\n' "$DIM" "$OFF"
}

trap 'stop_follow; printf "%s\n" "$OFF"; exit 0' INT TERM HUP
MODE="status"; CUR="${SLOTS[0]}"; tick=0; HAND_UNTIL=0
show_status
while true; do
  if read -rsn1 -t 1 key; then
    # A key puts the viewer in charge for HAND seconds; "a" hands back now.
    AUTO=""; HAND_UNTIL=$(( $(date +%s) + HAND ))
    case "$key" in
      [0-9]) if [ -n "${INDEX[$key]+set}" ]; then CUR="$key"; MODE="log"; follow "$CUR" log; fi ;;
      j|J) MODE="journal"; follow "$CUR" journal ;;
      s|S) stop_follow; MODE="status"; show_status; tick=0 ;;
      a|A) HAND_UNTIL=0 ;;
      q|Q) stop_follow; exit 0 ;;
    esac
  fi
  [ "$(date +%s)" -ge "$HAND_UNTIL" ] && auto_follow
  if [ "$MODE" = "status" ]; then
    tick=$((tick + 1))
    [ "$tick" -ge 5 ] && { show_status; tick=0; }
  fi
done
