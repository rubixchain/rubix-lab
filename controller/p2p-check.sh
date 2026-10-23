#!/usr/bin/env bash
# Rubix lab - can every node still RECEIVE from its peers?
#
# A node's API can answer while no peer can reach it. Peers reach a node through
# its IPFS: the node registers `p2p listen /x/<peerID>RubixCore/1.0` pointing at
# its listener port 127.0.0.1:(ReceiverPort+10) (core/core.go:238,
# core/ipfsport/listener.go). Two ways that breaks, both found reading 1d9b28bb
# after the 2026-10-22 run (NFT-01, SC-01, COMBO-01, CHAIN-01 all failed on .130
# slots 4, 6 and 7 with "getPeer failed: did not exist with the peer"):
#
#   DEAF        the IPFS auto-restart starts a new daemon and never registers
#               the p2p listener again (core/ipfs_recovery.go restartIPFS /
#               reinitializeIPFS). A node that is shut down with a stale IPFS
#               PID runs `killall -9 ipfs` (core/shutdown_manager.go), so one
#               restart can deafen every other node on the machine.
#   PORT TAKEN  the listener port is held by another process. With the lab's
#               100-step node_index, slot k+2's listener lies inside slot k's
#               peer-port pool (slot 7's inside slot 5's wrapped pool), and the
#               pool leaks a port on every failed `p2p forward`
#               (core/ipfsport/peer.go OpenPeerConn), so it climbs over time.
#
# Per node: IPFS answering, the p2p listener and its target, who holds the
# listener port, open forwards (peer connections in use right now - a port the
# pool leaked leaves no forward behind, so this does not show the leak), and
# IPFS auto-restarts in the node's log.txt since the node last started.
#
# First run, 2026-10-23: .130 nodes 0-7 DEAF, each with IPFS auto-restarts in
# its log, node 8 fine, no restart on any other machine; a whole-machine
# restart made all 9 receive again.
#
# Read-only: changes nothing anywhere. The fix for a DEAF or PORT TAKEN node
# is restarting its whole machine (restart-nodes.sh --host <ip> --force, which
# stops every node there before starting any), then this check again.
#
# Usage:
#   ./p2p-check.sh                        # pool machines
#   ./p2p-check.sh --host 192.168.1.130   # one machine
#   ./p2p-check.sh --include-fixed        # plus fullnodes and faucet machines

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
# "ip slot node_index api_port db_port swarm_port dids role" per node.
listing="$(python3 "$HERE/fleet.py" nodes "${SELECT[@]}")"
HOSTS=()
while read -r ip _rest; do
  [ -n "$ip" ] || continue
  case " ${HOSTS[*]-} " in *" $ip "*) ;; *) HOSTS+=("$ip") ;; esac
done <<<"$listing"
[ "${#HOSTS[@]}" -gt 0 ] || { echo "No nodes selected."; exit 1; }

check_one() {
  local ip="$1" args=() hip slot index _api _db _sw _d _r raw
  while read -r hip slot index _api _db _sw _d _r; do
    [ "$hip" = "$ip" ] && args+=("$slot" "$index")
  done <<<"$listing"

  if ! raw=$(ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${ip}" \
               bash -s -- "${args[@]}" 2>/dev/null <<'REMOTE'
while [ $# -ge 2 ]; do
  s="$1"; i="$2"; shift 2
  d=~/Desktop/rubix/rubix-lab/nodes/node$s
  L=$(( ((22000 + 1000 * i) % 65536 + 10) % 65536 ))
  unit=$(systemctl show -p MainPID --value "rubixgoplatform@$s" 2>/dev/null)
  sock=$(ss -ltnpH "sport = :$L" 2>/dev/null | head -1)
  owner=$(grep -o 'pid=[0-9]*' <<<"$sock" | head -1 | cut -d= -f2)
  name=$(grep -o 'users:(("[^"]*"' <<<"$sock" | head -1 | cut -d'"' -f2 | tr -c 'a-zA-Z0-9_.\n-' '_')
  # Which node folder the holding process runs in (a node or its IPFS daemon).
  holder=$( [ -n "$owner" ] && basename "$(readlink "/proc/$owner/cwd" 2>/dev/null)" 2>/dev/null )
  if p2p=$(IPFS_PATH=$d/.ipfs "$d/ipfs" p2p ls 2>/dev/null); then ipfs=up; else ipfs=down; fi
  target=$(awk '$2 ~ "^/p2p/" {print $3; exit}' <<<"$p2p")
  fwd=$(awk '$2 !~ "^/p2p/" && NF >= 3' <<<"$p2p" | wc -l)
  # log.txt is appended across node starts, so count only the IPFS restarts
  # since the node's last start ("Setting up the core", core/core.go:233) -
  # one of those means the node has been deaf since it happened.
  restarts=$(awk '/Setting up the core/ {n = 0} /Restarting IPFS daemon/ {n++} END {print n + 0}' "$d/log.txt" 2>/dev/null)
  echo "s=$s L=$L unit=${unit:-0} sock=$([ -n "$sock" ] && echo yes || echo no) owner=${owner:--} name=${name:--} holder=${holder:--} ipfs=$ipfs target=${target:-none} fwd=$fwd restarts=${restarts:-0}"
done
REMOTE
          ); then
    printf '%-15s %s\n' "$ip" "no SSH (run setup-ssh.sh)"
    return 1
  fi

  local ok=0 n=0 bad=() line kv s L unit sock owner name holder ipfs target fwd restarts verdict fix
  local maxfwd=0 rs=0
  while read -r line; do
    [ -n "$line" ] || continue
    s=?; L=0; unit=0; sock=no; owner=-; name=-; holder=-; ipfs=down; target=none; fwd=0; restarts=0
    for kv in $line; do
      case "${kv%%=*}" in
        s) s="${kv#*=}" ;; L) L="${kv#*=}" ;; unit) unit="${kv#*=}" ;; sock) sock="${kv#*=}" ;;
        owner) owner="${kv#*=}" ;; name) name="${kv#*=}" ;; holder) holder="${kv#*=}" ;;
        ipfs) ipfs="${kv#*=}" ;; target) target="${kv#*=}" ;; fwd) fwd="${kv#*=}" ;;
        restarts) restarts="${kv#*=}" ;;
      esac
    done
    n=$((n + 1))
    [ "$fwd" -gt "$maxfwd" ] && maxfwd=$fwd
    [ "$restarts" -gt 0 ] 2>/dev/null && rs=$((rs + restarts))
    # The whole machine, not the one node: restarting one node whose IPFS had
    # restarted can kill every other node's IPFS there (see restart-nodes.sh).
    fix="./restart-nodes.sh --host $ip --force, then ./p2p-check.sh --host $ip"
    if [ "$ipfs" != up ]; then
      verdict="IPFS DOWN - its IPFS daemon does not answer"
    elif [ "$target" = none ]; then
      verdict="DEAF - its IPFS has no p2p listener, so no peer can reach it (restart the node)"
    elif [ "$target" != "/ip4/127.0.0.1/tcp/$L" ]; then
      verdict="MISROUTED - its p2p listener points at $target, the node listens on $L"
    elif [ "$sock" != yes ]; then
      verdict="NOT LISTENING - nothing holds 127.0.0.1:$L"
    elif [ "$owner" = - ]; then
      verdict="PORT HELD by a process this user cannot see - 127.0.0.1:$L"
    elif [ "$owner" != "$unit" ]; then
      verdict="PORT TAKEN - 127.0.0.1:$L is held by $name pid $owner (in $holder), not the node (pid $unit): peers' requests reach the wrong process"
    else
      ok=$((ok + 1)); continue
    fi
    bad+=("node $s: $verdict (forwards open $fwd, IPFS restarts since it started $restarts). Fix: $fix")
  done < <(grep '^s=' <<<"$raw")

  printf '%-15s %s/%s can receive   most forwards open on one node: %s   IPFS restarts since the nodes started: %s\n' \
    "$ip" "$ok" "$n" "$maxfwd" "$rs"
  for b in "${bad[@]}"; do echo "      - $b"; done
  [ "$ok" -eq "$n" ]
}

echo "Checking ${#HOSTS[@]} machine(s). Read-only - nothing is changed."
echo

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
for ip in "${HOSTS[@]}"; do
  {
    set +e
    check_one "$ip" > "$WORKDIR/$ip.out" 2>&1
    echo $? > "$WORKDIR/$ip.rc"
  } &
done
wait || true

BAD=()
for ip in "${HOSTS[@]}"; do
  cat "$WORKDIR/$ip.out" 2>/dev/null
  [ "$(cat "$WORKDIR/$ip.rc" 2>/dev/null || echo 1)" = "0" ] || BAD+=("$ip")
done
echo
echo "$(( ${#HOSTS[@]} - ${#BAD[@]} ))/${#HOSTS[@]} machine(s) with every node able to receive."
if [ "${#BAD[@]}" -gt 0 ]; then
  echo "Machines with a node that cannot receive: ${BAD[*]}"
  echo "Each line above ends with the restart that fixes it: the whole machine, with --force"
  echo "(a deaf node still answers its API, so restart-nodes.sh would otherwise leave it alone)."
  exit 1
fi
