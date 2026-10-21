#!/usr/bin/env bash
# Rubix lab - put the node console on the machines that run nodes, from the
# controller. No sudo. Per machine, all machines at once:
#   1. copy systems/console/ to ~/Desktop/rubix/rubix-lab/systems/console/
#   2. write the autostart + app-menu entries (rubix-console.sh --install), so
#      the console opens on the machine's screen at every login
#   3. --close-old: close the terminal windows left from the old single-node
#      setup - shells running nothing, or only a journalctl / newgrp from
#      those days. A window running anything else stays open and is listed.
#   4. open the console on the machine's screen now, if someone is logged in
#      there - replacing a console that was already open, so a re-run puts
#      the new version on screen
# The controller and explorer run no node, so they are never touched.
#
# Usage:
#   ./install-console.sh --host 192.168.1.107 --close-old   # one machine first
#   ./install-console.sh --include-fixed --close-old        # every machine with nodes
#   ./install-console.sh                                    # pool machines only

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
SINGLE_HOST=""
INCLUDE_FIXED=0
CLOSE_OLD=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    --close-old) CLOSE_OLD=1; shift ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
[ "$INCLUDE_FIXED" -eq 1 ] && SELECT+=(--include-fixed)
HOSTS=($(python3 "$HERE/fleet.py" hosts --with-nodes "${SELECT[@]}" | awk '{print $1}')) || exit 1
[ "${#HOSTS[@]}" -gt 0 ] || { echo "No machines with nodes selected."; exit 1; }

# Runs ON the machine; $1 = 1 to close the old windows. (No apostrophes in
# this block: it is one single-quoted string.)
REMOTE='
CLOSE="$1"
SELF="$HOME/Desktop/rubix/rubix-lab/systems/console/rubix-console.sh"
chmod +x "$SELF" && "$SELF" --install || exit 1
if [ "$CLOSE" = "1" ]; then
  for p in $(pgrep -u "$USER" -x bash); do
    pp=$(ps -o ppid= -p "$p" | tr -d " ")
    case "$(ps -o comm= -p "$pp" 2>/dev/null)" in gnome-terminal-*|x-terminal-emul*|xterm|konsole|terminator|tilix) ;; *) continue ;; esac
    kids=$(pgrep -P "$p" -a)
    old=1
    while read -r kpid kcmd; do
      [ -n "$kpid" ] || continue
      case "$kcmd" in *journalctl*|newgrp*) ;; *) old=0 ;; esac
    done <<<"$kids"
    where=$(readlink /proc/$p/cwd)
    if [ "$old" = "1" ]; then
      for k in $(pgrep -P "$p"); do kill -HUP "$k" 2>/dev/null; done
      kill -HUP "$p" 2>/dev/null; sleep 1
      kill -0 "$p" 2>/dev/null && kill -KILL "$p" 2>/dev/null
      echo "  closed an old terminal window (in $where${kids:+, it ran: $(echo "$kids" | cut -d" " -f2- | cut -c1-60 | tr "\n" ";")})"
    else
      echo "  KEPT a terminal window (in $where): $(echo "$kids" | cut -c1-70 | tr "\n" ";")"
    fi
  done
fi
uid=$(id -u)
# A console already open runs the previous copy of the script: close it, so
# the one opened below is the new copy.
if pkill -u "$uid" -f "systems/console/rubix-console.sh"; then
  echo "  closed the console that was open (it ran the previous version)"; sleep 1
fi
if [ -S "/run/user/$uid/bus" ] && pgrep -u "$uid" -x gnome-shell >/dev/null; then
  DISPLAY=:0 XDG_RUNTIME_DIR="/run/user/$uid" DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    setsid gnome-terminal --title="Rubix nodes" --maximize -- "$SELF" >/dev/null 2>&1 </dev/null &
  echo "  console opened on the screen"
else
  echo "  nobody is logged in on the screen - the console opens at the next login"
fi
'

one() {   # $1 = ip
  local ip="$1"
  ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" 'mkdir -p ~/Desktop/rubix/rubix-lab/systems' \
    && scp -rq "$REPO/systems/console" "${SSH_USER}@${ip}:Desktop/rubix/rubix-lab/systems/" \
    && ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" bash -s -- "$CLOSE_OLD" <<<"$REMOTE"
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
for ip in "${HOSTS[@]}"; do
  { one "$ip" > "$WORKDIR/$ip.out" 2>&1; echo $? > "$WORKDIR/$ip.rc"; } &
done
wait

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="
  cat "$WORKDIR/$ip.out"
  [ "$(cat "$WORKDIR/$ip.rc" 2>/dev/null)" = "0" ] || { echo "  ERROR: not done (SSH, or the copy failed)"; FAILED+=("$ip"); }
done
echo "----------------------------------------"
echo "${#HOSTS[@]} machine(s): $(( ${#HOSTS[@]} - ${#FAILED[@]} )) done, ${#FAILED[@]} need attention."
[ "${#FAILED[@]}" -eq 0 ] || { echo "Needs attention: ${FAILED[*]}"; exit 1; }
echo "From here:  ./fleet-console.sh   (pick a machine, see its nodes, 0-8 switch logs, q back)"
