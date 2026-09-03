#!/usr/bin/env bash
# Build rubixgoplatform from a branch and deploy it to one or more lab
# desktops. Run this FROM the controller desktop.
#
# Prerequisites (see README.md): a git checkout of rubixgoplatform on the
# controller, Go 1.22 + build-essential installed there, and SSH key access
# (plus a narrow passwordless-sudo grant for systemctl) already set up from
# the controller to every target desktop.
#
# Assumes every target desktop cloned rubix-lab to the same path relative
# to ITS OWN $HOME (default "rubix-lab", i.e. ~/rubix-lab — override with
# REMOTE_REPO_REL=path/to/clone). Resolves each target's actual $HOME over
# SSH at deploy time rather than assuming it, then deploys into
# <that clone>/nodes/<NODE_NAME> — the same folder install/setup.sh created.
#
# Why this is all SSH, not the Rubix API: rubixgoplatform.service has
# Restart=always, so gracefully shutting the process down via its own API
# would just have systemd relaunch the OLD binary underneath you. Swapping
# the binary needs systemctl stop (tells the supervisor to stop, not just
# the process to exit) — that's SSH-only, no way around it.
#
# Usage:
#   ./update-exec.sh <branch> <target-ip> [target-ip...]
#   ./update-exec.sh <branch> --all              # targets read from hosts.txt

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <branch> <target-ip> [target-ip...]"
  echo "       $0 <branch> --all"
  exit 1
fi

BRANCH="$1"; shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$HOME/rubixgoplatform}"     # product repo clone, for BUILDING (controller-local)
REMOTE_REPO_REL="${REMOTE_REPO_REL:-rubix-lab}"   # rubix-lab clone location on EACH TARGET, relative to that target's own $HOME
NODE_NAME="${NODE_NAME:-testnode}"
SSH_USER="${SSH_USER:-$(whoami)}"
# NODE_DIR is resolved per-target inside the deploy loop below (needs each
# target's own $HOME, which the controller can't assume — resolving a
# literal path once per target avoids relying on scp/ssh expanding $HOME or
# ~ remotely, which isn't consistent across implementations).

if [ "$1" == "--all" ]; then
  [ -f "$SCRIPT_DIR/hosts.txt" ] || { echo "ERROR: --all requires $SCRIPT_DIR/hosts.txt (one IP/hostname per line, # comments ok, copy hosts.txt.example)."; exit 1; }
  mapfile -t TARGETS < <(grep -vE '^\s*(#|$)' "$SCRIPT_DIR/hosts.txt")
else
  TARGETS=("$@")
fi

[ "${#TARGETS[@]}" -gt 0 ] || { echo "ERROR: no targets given."; exit 1; }

# ---------------------------------------------------------------------------
# Build FIRST, deploy only if it succeeds — never leave a target stopped
# because a later step failed.
# ---------------------------------------------------------------------------
echo "== Building branch '$BRANCH' =="
[ -d "$REPO_DIR/.git" ] || { echo "ERROR: $REPO_DIR is not a git checkout of rubixgoplatform. Set REPO_DIR or clone it there first."; exit 1; }

(
  cd "$REPO_DIR"
  git fetch origin
  git checkout "$BRANCH"
  git pull --ff-only origin "$BRANCH"
  make compile-linux
)

BINARY="$REPO_DIR/linux/rubixgoplatform"
[ -f "$BINARY" ] || { echo "ERROR: build did not produce $BINARY."; exit 1; }
COMMIT="$(cd "$REPO_DIR" && git rev-parse --short HEAD)"
echo "Built $BRANCH @ $COMMIT -> $BINARY"

# ---------------------------------------------------------------------------
# Deploy — sequential by default (simple, readable output for a lab-sized
# fleet). Easy to parallelize later with `&` + `wait` if 40 sequential
# restarts ever feels slow.
# ---------------------------------------------------------------------------
declare -A RESULT

for TARGET in "${TARGETS[@]}"; do
  echo ""
  echo "== $TARGET =="

  TARGET_HOME="$(ssh "${SSH_USER}@${TARGET}" 'echo $HOME')"
  NODE_DIR="${TARGET_HOME}/${REMOTE_REPO_REL}/nodes/${NODE_NAME}"

  echo "-- stopping service"
  if ssh "${SSH_USER}@${TARGET}" 'sudo systemctl stop rubixgoplatform'; then
    echo "-- copying binary"
    scp -q "$BINARY" "${SSH_USER}@${TARGET}:${NODE_DIR}/rubixgoplatform.new"
    # Copy to a temp name then rename in place — atomic, so a target never
    # sees a half-written binary if the copy above gets interrupted.
    ssh "${SSH_USER}@${TARGET}" "mv ${NODE_DIR}/rubixgoplatform.new ${NODE_DIR}/rubixgoplatform && chmod +x ${NODE_DIR}/rubixgoplatform"
    echo "-- starting service"
    ssh "${SSH_USER}@${TARGET}" 'sudo systemctl start rubixgoplatform'

    echo "-- waiting for API..."
    UP=0
    for i in $(seq 1 15); do
      if curl -s -o /dev/null --max-time 2 "http://${TARGET}:20000/rubix/v1/dids"; then
        UP=1; break
      fi
      sleep 2
    done
    if [ "$UP" -eq 1 ]; then
      echo "-- $TARGET is up on $BRANCH @ $COMMIT"
      RESULT[$TARGET]="OK ($BRANCH @ $COMMIT)"
    else
      echo "-- WARNING: $TARGET did not respond within 30s after restart"
      RESULT[$TARGET]="STARTED BUT NOT RESPONDING — check: ssh ${SSH_USER}@${TARGET} journalctl -u rubixgoplatform -n 100"
    fi
  else
    echo "-- WARNING: could not stop service on $TARGET (SSH/sudo issue?) — skipping, node left as-is"
    RESULT[$TARGET]="SKIPPED — stop failed, node untouched"
  fi
done

echo ""
echo "== Summary: $BRANCH @ $COMMIT =="
for TARGET in "${TARGETS[@]}"; do
  printf "  %-16s %s\n" "$TARGET" "${RESULT[$TARGET]:-UNKNOWN}"
done
