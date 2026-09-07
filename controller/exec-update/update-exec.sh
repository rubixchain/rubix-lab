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
# The fleet NODES never need a source checkout or Go. The branch is built ONCE
# here and the compiled 27MB binary is copied to each node over scp. Only this
# controller needs the checkout, and it must be Linux: the Makefile sets
# CGO_ENABLED=1, so the build cannot be cross-compiled without a C toolchain.
#
# Usage:
#   ./update-exec.sh <branch> <target-ip> [target-ip...]
#   ./update-exec.sh <branch> --all         # every pool host in controller/hosts.txt
#
# Environment overrides:
#   REMOTE_BIN_REL=Desktop/rubix   where the binary lives on each target,
#                                  relative to its $HOME. REQUIRED for a fleet
#                                  brought up by hand rather than by
#                                  systems/install/setup.sh. Check with:
#                                  systemctl cat rubixgoplatform | grep ExecStart
#   PREBUILT_BINARY=/path/to/bin   skip the build and ship this binary instead
#                                  (for a controller with no Go toolchain)
#   JOBS=1                         deploy one host at a time (default: all at
#                                  once) - useful when watching a failure
#   REPO_DIR=~/rubixgoplatform     the checkout to build from
#
# Typical use on this fleet:
#   REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh my-branch 192.168.1.104   # one host
#   REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh my-branch --all           # then all
#   cd .. && python3 node-versions.py                                       # verify

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
  # controller/hosts.txt is the fleet's single source of truth - the same file
  # every other controller tool reads. Keeping a second list here would drift,
  # and a host commented out as down would silently come back.
  # Fixed-role hosts are skipped: the fullnode is the bootstrap seed every node
  # points at, and the explorer/controller run no participating node.
  MAIN_HOSTS="$(cd "$SCRIPT_DIR/.." && pwd)/hosts.txt"
  if [ -f "$SCRIPT_DIR/hosts.txt" ]; then
    echo "Using $SCRIPT_DIR/hosts.txt (local override)"
    mapfile -t TARGETS < <(grep -vE '^\s*(#|$)' "$SCRIPT_DIR/hosts.txt" | awk '{print $1}')
  elif [ -f "$MAIN_HOSTS" ]; then
    echo "Using $MAIN_HOSTS (fixed-role hosts skipped)"
    mapfile -t TARGETS < <(grep -v '^\s*#' "$MAIN_HOSTS" \
      | awk 'NF && $2 != "fullnode" && $2 != "explorer" && $2 != "controller" {print $1}')
  else
    echo "ERROR: --all needs $MAIN_HOSTS (or a local $SCRIPT_DIR/hosts.txt)."; exit 1
  fi
else
  TARGETS=("$@")
fi

[ "${#TARGETS[@]}" -gt 0 ] || { echo "ERROR: no targets given."; exit 1; }

# ---------------------------------------------------------------------------
# Build FIRST, deploy only if it succeeds — never leave a target stopped
# because a later step failed.
# ---------------------------------------------------------------------------
# PREBUILT_BINARY skips the build entirely and ships a binary someone else
# produced. The build needs CGO_ENABLED=1 (see the Makefile), so it has to
# happen on Linux with a C toolchain - it cannot be cross-compiled from
# Windows or macOS without one. Building on the controller is the normal path;
# this exists for when the controller has no Go, or a release binary was
# handed over directly.
#   PREBUILT_BINARY=/path/to/rubixgoplatform ./update-exec.sh v1.0.5 --all
#
# NOTE: the fleet NODES never need Go or a source checkout. They receive a
# compiled binary over scp and nothing else. Only the build host needs either.
if [ -n "${PREBUILT_BINARY:-}" ]; then
  BINARY="$PREBUILT_BINARY"
  [ -f "$BINARY" ] || { echo "ERROR: PREBUILT_BINARY=$BINARY does not exist."; exit 1; }
  if ! file "$BINARY" 2>/dev/null | grep -q "ELF"; then
    echo "ERROR: $BINARY is not a Linux (ELF) executable."
    echo "       A macOS or Windows build will copy fine and then fail to start,"
    echo "       leaving every node down. Build with 'make compile-linux' on Linux."
    exit 1
  fi
  COMMIT="prebuilt"
  echo "== Using prebuilt binary: $BINARY (skipping build) =="
else
  echo "== Building branch '$BRANCH' =="
  [ -d "$REPO_DIR/.git" ] || {
    echo "ERROR: $REPO_DIR is not a git checkout of rubixgoplatform."
    echo "       Either clone it there:"
    echo "         git clone https://github.com/rubixchain/rubixgoplatform $REPO_DIR"
    echo "       set REPO_DIR to an existing checkout, or pass a binary built"
    echo "       elsewhere with PREBUILT_BINARY=/path/to/rubixgoplatform"
    exit 1; }
  command -v go >/dev/null 2>&1 || {
    echo "ERROR: Go is not installed on this controller, so the branch cannot be built."
    echo "         sudo apt install -y golang-go build-essential      # needs Go 1.22+"
    echo "       Or pass a binary built elsewhere with PREBUILT_BINARY=..."
    exit 1; }

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
fi

# ---------------------------------------------------------------------------
# Deploy — PARALLEL. These are separate machines with one SSH connection each,
# and the per-host cost is dominated by systemctl stop/start plus the API
# readiness wait (up to 30s), not by the 27MB copy (~1s on gigabit). Run
# sequentially, 31 hosts took ~15 minutes; in parallel it finishes in about the
# time of the slowest single host.
#
# Same pattern as restart-nodes.sh and wipe-node-db.sh: each host runs in a
# background subshell, output is buffered to a file and printed in host order
# at the end, and the result travels back through an exit code (a subshell
# cannot append to the parent's arrays).
#
# --jobs N caps concurrency; JOBS=0 (default) means all at once. Drop to
# --jobs 1 to watch one host at a time when something is going wrong.
# ---------------------------------------------------------------------------
JOBS="${JOBS:-0}"
[ "$JOBS" -le 0 ] && JOBS=${#TARGETS[@]}

deploy_one() {
  local TARGET="$1"
  local TARGET_HOME NODE_DIR
  # Where the BINARY lives on the target - which is not always where the
  # node's data dir lives.
  #
  # Default assumes systems/install/setup.sh's layout:
  #   <clone>/nodes/<NODE_NAME>/rubixgoplatform
  # This fleet was brought up BY HAND instead, and its systemd unit runs
  #   $HOME/Desktop/rubix/rubixgoplatform run -p $HOME/Desktop/rubix/node
  # so the binary sits at ~/Desktop/rubix with no "nodes/" segment at all.
  # REMOTE_BIN_REL overrides the whole path (relative to the target's $HOME):
  #   REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh <branch> --all
  # Copying to the wrong directory is silent-ish: the scp fails, or worse
  # succeeds somewhere the service never reads, leaving the OLD binary running
  # while the summary says OK. Check with node-versions.py afterwards.
  if [ -n "${REMOTE_BIN_REL:-}" ]; then
    NODE_DIR="${TARGET_HOME}/${REMOTE_BIN_REL}"
  else
    NODE_DIR="${TARGET_HOME}/${REMOTE_REPO_REL}/nodes/${NODE_NAME}"
  fi

  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${TARGET}" "[ -f ${NODE_DIR}/rubixgoplatform ]"; then
    echo "-- WARNING: no existing binary at ${NODE_DIR}/rubixgoplatform"
    echo "            Refusing to deploy - the service would keep running the old"
    echo "            binary from wherever it actually lives. Set REMOTE_BIN_REL."
    echo "            Find it with:  ssh ${SSH_USER}@${TARGET} 'systemctl cat rubixgoplatform | grep ExecStart'"
    return 3
  fi

  echo "-- stopping service"
  if ! ssh -o ConnectTimeout=8 "${SSH_USER}@${TARGET}" 'sudo systemctl stop rubixgoplatform'; then
    echo "-- WARNING: could not stop service (SSH/sudo issue?) - node left as-is"
    return 2
  fi

  echo "-- copying binary"
  # Copy to a temp name then rename in place — atomic, so a target never sees
  # a half-written binary if the copy gets interrupted.
  if ! scp -q "$BINARY" "${SSH_USER}@${TARGET}:${NODE_DIR}/rubixgoplatform.new"; then
    echo "-- ERROR: copy failed. Node is STOPPED with its old binary intact."
    echo "          Restart it with: ssh ${SSH_USER}@${TARGET} 'sudo systemctl start rubixgoplatform'"
    return 1
  fi
  ssh "${SSH_USER}@${TARGET}" "mv ${NODE_DIR}/rubixgoplatform.new ${NODE_DIR}/rubixgoplatform && chmod +x ${NODE_DIR}/rubixgoplatform"

  echo "-- starting service"
  ssh "${SSH_USER}@${TARGET}" 'sudo systemctl start rubixgoplatform'

  echo "-- waiting for API..."
  local UP=0 i
  for i in $(seq 1 15); do
    if curl -s -o /dev/null --max-time 2 "http://${TARGET}:20000/rubix/v1/dids"; then
      UP=1; break
    fi
    sleep 2
  done
  if [ "$UP" -eq 1 ]; then
    echo "-- up on $BRANCH @ $COMMIT"
    return 0
  fi
  echo "-- WARNING: did not respond within 30s after restart"
  echo "            check: ssh ${SSH_USER}@${TARGET} journalctl -u rubixgoplatform -n 100"
  return 4
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo ""
echo "== Deploying to ${#TARGETS[@]} host(s), ${JOBS} at a time =="
echo "   (output is buffered per host and printed together when all are done)"

running=0
for TARGET in "${TARGETS[@]}"; do
  {
    # set +e inside the subshell: errexit would abort before the .rc file is
    # written, losing this host's real result.
    set +e
    deploy_one "$TARGET" > "$WORKDIR/$TARGET.out" 2>&1
    echo $? > "$WORKDIR/$TARGET.rc"
  } &
  running=$(( running + 1 ))
  if [ "$running" -ge "$JOBS" ]; then
    wait -n || true
    running=$(( running - 1 ))
  fi
done
wait || true

declare -A RESULT
for TARGET in "${TARGETS[@]}"; do
  echo ""
  echo "== $TARGET =="
  cat "$WORKDIR/$TARGET.out" 2>/dev/null || echo "  (no output captured)"
  rc="$(cat "$WORKDIR/$TARGET.rc" 2>/dev/null || echo 1)"
  case "$rc" in
    0) RESULT[$TARGET]="OK ($BRANCH @ $COMMIT)" ;;
    2) RESULT[$TARGET]="SKIPPED — stop failed, node untouched" ;;
    3) RESULT[$TARGET]="SKIPPED — no binary at expected path, node untouched" ;;
    4) RESULT[$TARGET]="STARTED BUT NOT RESPONDING — check journalctl" ;;
    *) RESULT[$TARGET]="FAILED — node may be STOPPED, see output above" ;;
  esac
done

echo ""
echo "== Summary: $BRANCH @ $COMMIT =="
FAILED=0
for TARGET in "${TARGETS[@]}"; do
  printf "  %-16s %s\n" "$TARGET" "${RESULT[$TARGET]:-UNKNOWN}"
  case "${RESULT[$TARGET]}" in OK*) ;; *) FAILED=$(( FAILED + 1 )) ;; esac
done
echo ""
echo "  $(( ${#TARGETS[@]} - FAILED )) / ${#TARGETS[@]} on $BRANCH @ $COMMIT"
echo ""
echo "  A green summary only means the service restarted and answered. Confirm the"
echo "  binary actually changed:   cd .. && python3 node-versions.py"
[ "$FAILED" -eq 0 ] || exit 1
