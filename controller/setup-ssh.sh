#!/usr/bin/env bash
# Rubix lab - one-time SSH key setup: controller -> every node.
#
# Generates a key pair on the controller if it doesn't have one yet, then
# copies it to every host listed in controller/hosts.txt (the same file
# check-nodes.py reads). Safe to re-run: ssh-copy-id is a no-op on a host
# that already has the key, and this script re-checks every host at the end
# regardless.
#
# You will still be asked for each machine's normal login password, once
# per machine, the first time this runs against it. There's no way around
# that without storing the password somewhere insecurely — this script just
# saves you from typing the same commands 40 times.
#
# Usage:
#   cd controller
#   cp hosts.txt.example hosts.txt   # if not already done, fill in real IPs
#   ./setup-ssh.sh [ssh-user]        # ssh-user defaults to "rubix"

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOSTS_FILE="$HERE/hosts.txt"
SSH_USER="${1:-rubix}"

[ -f "$HOSTS_FILE" ] || {
  echo "ERROR: $HOSTS_FILE not found."
  echo "Copy hosts.txt.example to hosts.txt and fill in your real node IPs first."
  exit 1
}

# --- Step 1: make a key pair if one doesn't exist yet ---
if [ ! -f "$HOME/.ssh/id_ed25519" ] && [ ! -f "$HOME/.ssh/id_rsa" ]; then
  echo "No SSH key found on this controller — generating one (~/.ssh/id_ed25519)..."
  ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
else
  echo "SSH key already exists on this controller, skipping generation."
fi
echo

# --- Step 2: pull the IP/hostname column out of hosts.txt ---
# (ignores comment lines and the optional role column, same parsing as
# check-nodes.py's load_hosts)
mapfile -t HOSTS < <(grep -v '^\s*#' "$HOSTS_FILE" | awk 'NF {print $1}')

if [ "${#HOSTS[@]}" -eq 0 ]; then
  echo "ERROR: no hosts found in $HOSTS_FILE"
  exit 1
fi

echo "Copying the key to ${#HOSTS[@]} host(s) as user '$SSH_USER'..."
echo "You'll be asked for each machine's normal login password, once."
echo

FAILED=()
for ip in "${HOSTS[@]}"; do
  echo "== $ip =="
  if ssh-copy-id -o ConnectTimeout=8 "$SSH_USER@$ip"; then
    echo "OK"
  else
    echo "FAILED"
    FAILED+=("$ip")
  fi
  echo
done

# --- Step 3: verify passwordless login actually works ---
echo "Verifying passwordless login to every host..."
STILL_FAILED=()
for ip in "${HOSTS[@]}"; do
  if ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_USER@$ip" 'echo ok' >/dev/null 2>&1; then
    echo "$ip  OK"
  else
    echo "$ip  STILL ASKS FOR A PASSWORD / UNREACHABLE"
    STILL_FAILED+=("$ip")
  fi
done

if [ "${#STILL_FAILED[@]}" -gt 0 ]; then
  echo
  echo "These hosts still need attention: ${STILL_FAILED[*]}"
  echo "Re-run this script any time, or run ssh-copy-id by hand for just those IPs."
  exit 1
fi

echo
echo "All hosts set up — SSH is now passwordless from this controller."
