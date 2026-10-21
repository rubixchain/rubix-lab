#!/usr/bin/env bash
# Rubix lab - follow the live log of fleet nodes, from the controller.
#
# Every line is printed after the node it came from (ip:api_port), so one
# terminal can follow one node or the whole fleet. One SSH connection per
# machine, running `tail -F` on its nodes' log.txt. Ctrl+C stops it. Read-only.
#
# A node's own log is ~/Desktop/rubix/rubix-lab/nodes/node<slot>/log.txt on its
# machine. Its service log (start, crash, restart) is on the machine too:
#   journalctl -u rubixgoplatform@<slot> -f
#
# Usage:
#   ./node-logs.sh --host 192.168.1.102              # one machine (here a fullnode)
#   ./node-logs.sh --host 192.168.1.107 --slot 3     # one node of a pool machine
#   ./node-logs.sh --role fullnode                   # the four fullnodes
#   ./node-logs.sh --role faucet --role faucet-quorum
#   ./node-logs.sh                                   # every pool node
#   ./node-logs.sh --include-fixed                   # every node in the fleet
#   ./node-logs.sh --include-fixed --errors          # only [ERROR] and [WARN] lines
#   ./node-logs.sh --role fullnode --grep "rubix_txn"   # only lines matching a pattern
#   ./node-logs.sh --host 192.168.1.105 --lines 200  # start with the last 200 lines
#
# Without --lines it starts with the last 20 lines of each node when following
# up to 9 nodes, and with new lines only when following more.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="rubix"
SINGLE_HOST=""
INCLUDE_FIXED=0
SLOTS=()
ROLES=()
PATTERN=""
LINES=""

while [ $# -gt 0 ]; do
  case "$1" in
    --host) SINGLE_HOST="$2"; shift 2 ;;
    --slot) SLOTS+=("$2"); shift 2 ;;
    --role) ROLES+=("$2"); shift 2 ;;
    --include-fixed) INCLUDE_FIXED=1; shift ;;
    --grep) PATTERN="$2"; shift 2 ;;
    # Bracket expressions, not backslashes: awk -v would eat a backslash.
    --errors) PATTERN="[[]ERROR[]]|[[]WARN[]]"; shift ;;
    --lines) LINES="$2"; shift 2 ;;
    --hosts) HOSTS_FILE="$2"; shift 2 ;;
    --user) SSH_USER="$2"; shift 2 ;;
    *) echo "Unknown argument: $1"; exit 1 ;;
  esac
done

SELECT=(--hosts "$HOSTS_FILE")
[ -n "$SINGLE_HOST" ] && SELECT+=(--host "$SINGLE_HOST")
{ [ "$INCLUDE_FIXED" -eq 1 ] || [ "${#ROLES[@]}" -gt 0 ]; } && SELECT+=(--include-fixed)
listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")" || exit 1

declare -A FILES=() PORTS=()
ORDER=()
count=0
while read -r ip slot _index api _db _swarm _dids role; do
  [ -n "$ip" ] || continue
  if [ "${#ROLES[@]}" -gt 0 ]; then
    keep=""; for r in "${ROLES[@]}"; do [ "$r" = "$role" ] && keep=1; done
    [ -n "$keep" ] || continue
  fi
  if [ "${#SLOTS[@]}" -gt 0 ]; then
    keep=""; for s in "${SLOTS[@]}"; do [ "$s" = "$slot" ] && keep=1; done
    [ -n "$keep" ] || continue
  fi
  [ -n "${FILES[$ip]+set}" ] || ORDER+=("$ip")
  FILES[$ip]="${FILES[$ip]:-} node$slot/log.txt"
  PORTS[$ip]="${PORTS[$ip]:-} $slot=$api"
  count=$((count + 1))
done <<<"$listing"
[ "$count" -gt 0 ] || { echo "No nodes selected."; exit 1; }
if [ -z "$LINES" ]; then
  if [ "$count" -le 9 ]; then LINES=20; else LINES=0; fi
fi

# Ubuntu's awk is mawk, which reads a pipe in large blocks and so would sit on
# a live log's lines; -W interactive makes it read and print line by line.
AWK=(awk)
awk -W version 2>/dev/null | grep -qi mawk && AWK=(awk -W interactive)

trap 'kill $(jobs -p) 2>/dev/null; exit 0' INT TERM
echo "Following $count node(s) on ${#ORDER[@]} machine(s)${PATTERN:+, lines matching: $PATTERN} - Ctrl+C to stop."

for ip in "${ORDER[@]}"; do
  ssh -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=30 "${SSH_USER}@${ip}" \
    "cd ~/Desktop/rubix/rubix-lab/nodes && exec tail -n ${LINES} -F${FILES[$ip]} 2>&1" </dev/null \
  | "${AWK[@]}" -v ip="$ip" -v map="${PORTS[$ip]}" -v pat="$PATTERN" '
      BEGIN {
        n = split(map, kv, " ")
        for (i = 1; i <= n; i++) { split(kv[i], a, "="); port[a[1]] = a[2] }
        cur = ip
        if (n == 1) cur = ip ":" port[a[1]]    # one file: tail prints no headers
      }
      /^==> node[0-9]+\/log\.txt <==$/ {
        s = $2; sub(/^node/, "", s); sub(/\/log\.txt$/, "", s)
        cur = ip ":" port[s]; next
      }
      /^$/ { next }
      {
        if (pat != "" && $0 !~ pat) next
        print cur " | " $0
        fflush()
      }' &
done
wait
