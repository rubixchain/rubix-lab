#!/usr/bin/env bash
# Build rubixgoplatform from a branch and deploy it to the nodes of one or
# more lab machines. Run this FROM the controller.
#
# Prerequisites (see README.md): a git checkout of rubixgoplatform on the
# controller, Go 1.22 + build-essential installed there, and SSH key access
# from the controller to every machine. The passwordless sudo for stopping
# and starting the nodes comes with systems/install/setup.sh.
#
# Every node has its own copy of the binary, in ~/rubix-lab/nodes/node<slot>/
# on its machine (setup.sh's layout; controller/fleet.py lists the slots), and
# runs as systemd unit rubixgoplatform@<slot>. Per machine this stops every
# node, copies the binary over once, swaps it into each node folder, starts
# the nodes again and waits until each answers on its own port.
#
# Why this is all SSH, not the Rubix API: the units have Restart=always, so
# gracefully shutting a node down via its own API would just have systemd
# relaunch the OLD binary underneath you. Swapping the binary needs systemctl
# stop (tells the supervisor to stop, not just the process to exit).
#
# The fleet machines never need a source checkout or Go. The branch is built
# ONCE here and the compiled binary is copied over with scp. This controller
# must be Linux: the Makefile sets CGO_ENABLED=1, so the build cannot be
# cross-compiled without a C toolchain.
#
# Usage:
#   ./update-exec.sh <branch> <machine-ip> [machine-ip...]   # every node on those machines
#   ./update-exec.sh <branch> --all                          # every pool machine
#   ./update-exec.sh <branch> --all --include-fixed          # plus the fullnode and faucet machines
#
# Environment overrides (or exec-update.env, see the example):
#   PREBUILT_BINARY=/path/to/bin   skip the build and ship this binary instead
#                                  (for a controller with no Go toolchain)
#   REPO_DIR=~/Desktop/rubixgoplatform   the checkout to build from
#   JOBS=1                         deploy one machine at a time (default: all at
#                                  once) - useful when watching a failure
#
# Typical use:
#   ./update-exec.sh my-branch 192.168.1.107                 # one machine first
#   ./update-exec.sh my-branch --all --include-fixed         # then every node
#   cd .. && python3 node-versions.py --include-fixed        # verify the commit landed

set -euo pipefail

if [ "$#" -lt 2 ]; then
  echo "Usage: $0 <branch> <machine-ip> [machine-ip...]"
  echo "       $0 <branch> --all [--include-fixed]"
  exit 1
fi

BRANCH="$1"; shift

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTROLLER_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Settings file. Avoids retyping REPO_DIR / PREBUILT_BINARY on every run.
#
#   cp exec-update.env.example exec-update.env    # then edit
#
# Precedence: an already-exported variable WINS over the file, so a one-off
#   REPO_DIR=other/path ./update-exec.sh ...
# still overrides the saved setting. The file only fills in what is unset.
# Point somewhere else with EXEC_UPDATE_ENV=/path/to/file.
# ---------------------------------------------------------------------------
load_env_file() {
  local f="$1" line key val
  [ -f "$f" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"                                   # strip comments
    line="${line#"${line%%[![:space:]]*}"}"              # ltrim
    line="${line%"${line##*[![:space:]]}"}"              # rtrim
    [ -z "$line" ] && continue
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%\"}"; val="${val#\"}"                     # strip quotes
    val="${val%\'}"; val="${val#\'}"
    case "$val" in "~/"*) val="$HOME/${val#\~/}" ;; esac  # a file cannot expand ~
    if [ -z "${!key:-}" ]; then
      export "$key=$val"
    fi
  done < "$f"
}
ENV_FILE="${EXEC_UPDATE_ENV:-$SCRIPT_DIR/exec-update.env}"
if [ -f "$ENV_FILE" ]; then
  load_env_file "$ENV_FILE"
  echo "Settings loaded from $ENV_FILE"
fi
# The single-node fleet needed REMOTE_BIN_REL to find its binary. Every node
# now lives at ~/rubix-lab/nodes/node<slot>, so a leftover value would only
# mislead - say so instead of silently ignoring it.
if [ -n "${REMOTE_BIN_REL:-}${NODE_NAME:-}" ]; then
  echo "NOTE: REMOTE_BIN_REL / NODE_NAME are no longer used (nodes live in"
  echo "      ~/rubix-lab/nodes/node<slot>); remove them from exec-update.env."
fi
# Product repo clone, for BUILDING (controller-local).
REPO_DIR="${REPO_DIR:-$HOME/Desktop/rubixgoplatform}"
SSH_USER="${SSH_USER:-$(whoami)}"

ALL=0; INCLUDE_FIXED=0; TARGET_ARGS=()
for a in "$@"; do
  case "$a" in
    --all) ALL=1 ;;
    --include-fixed) INCLUDE_FIXED=1 ;;
    -*) echo "Unknown argument: $a"; exit 1 ;;
    *) TARGET_ARGS+=("$a") ;;
  esac
done
if [ "$ALL" -eq 1 ] && [ "${#TARGET_ARGS[@]}" -gt 0 ]; then
  echo "ERROR: give machine IPs or --all, not both."; exit 1
fi

command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is needed (fleet.py reads hosts.txt)."; exit 1; }
fleet() { python3 "$CONTROLLER_DIR/fleet.py" "$@"; }

# "ip slot node_index api_port ..." for every node being deployed to.
# controller/hosts.txt is the fleet's single source of truth - the same file
# every other controller tool reads; fleet.py stops on a bad entry.
if [ "$ALL" -eq 1 ]; then
  if [ "$INCLUDE_FIXED" -eq 1 ]; then listing="$(fleet nodes --include-fixed)"; else listing="$(fleet nodes)"; fi
else
  [ "${#TARGET_ARGS[@]}" -gt 0 ] || { echo "ERROR: no machines given."; exit 1; }
  listing=""
  for t in "${TARGET_ARGS[@]}"; do
    out="$(fleet nodes --host "$t")"
    [ -n "$out" ] || { echo "ERROR: $t runs no node (explorer/controller)."; exit 1; }
    listing+="$out"$'\n'
  done
fi
TARGETS=()
while read -r ip _rest; do
  [ -n "$ip" ] || continue
  case " ${TARGETS[*]-} " in *" $ip "*) ;; *) TARGETS+=("$ip") ;; esac
done <<<"$listing"
[ "${#TARGETS[@]}" -gt 0 ] || { echo "ERROR: no machines selected."; exit 1; }

# ---------------------------------------------------------------------------
# Build FIRST, deploy only if it succeeds — never leave a node stopped
# because a later step failed.
# ---------------------------------------------------------------------------
# PREBUILT_BINARY skips the build entirely and ships a binary someone else
# produced. The build needs CGO_ENABLED=1 (see the Makefile), so it has to
# happen on Linux with a C toolchain - it cannot be cross-compiled from
# Windows or macOS without one. Building on the controller is the normal path.
#   PREBUILT_BINARY=/path/to/rubixgoplatform ./update-exec.sh v1.0.5 --all
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
# Deploy — machines in PARALLEL (separate machines, one SSH connection each;
# the per-machine cost is the stop/start and the readiness wait, not the copy).
# Same pattern as restart-nodes.sh: each machine runs in a background
# subshell, output is buffered to a file and printed in order at the end, and
# the result travels back through an exit code.
# ---------------------------------------------------------------------------
JOBS="${JOBS:-0}"
[ "$JOBS" -le 0 ] && JOBS=${#TARGETS[@]}

ssh_run() {   # $1 = machine, rest = args for the remote script on stdin
  local target="$1"; shift
  ssh -o BatchMode=yes -o ConnectTimeout=8 "${SSH_USER}@${target}" bash -s -- "$@"
}

# Runs on the machine: `start` or `stop`, then the slots - through the sudo
# grant setup.sh installs, one unit per call.
UNITS_SCRIPT='rc=0; v="$1"; shift
for s in "$@"; do sudo -n systemctl "$v" "rubixgoplatform@$s" || rc=1; done
exit $rc'

deploy_one() {
  local TARGET="$1" slots=() ports=() hip slot _i api _rest missing
  while read -r hip slot _i api _rest; do
    [ "$hip" = "$TARGET" ] && { slots+=("$slot"); ports+=("$api"); }
  done <<<"$listing"

  # Reachable, and every node folder already holds a binary. A missing one
  # means the machine was never installed - copying would put a binary where
  # no unit runs it, and the summary would still say OK.
  if ! missing="$(ssh_run "$TARGET" "${slots[@]}" 2>"$WORKDIR/$TARGET.err" <<'REMOTE'
for s in "$@"; do [ -f ~/rubix-lab/nodes/node$s/rubixgoplatform ] || echo "$s"; done
REMOTE
  )"; then
    echo "-- WARNING: cannot reach ${SSH_USER}@${TARGET} over SSH - nodes untouched"
    echo "            $(tail -1 "$WORKDIR/$TARGET.err" 2>/dev/null || echo no response)"
    return 2
  fi
  if [ -n "$missing" ]; then
    echo "-- WARNING: no binary for node(s) $(echo $missing) in ~/rubix-lab/nodes - refusing to deploy."
    echo "            Install the machine first: ./install-nodes.sh --host ${TARGET}"
    return 3
  fi

  echo "-- stopping ${#slots[@]} node(s)"
  if ! ssh_run "$TARGET" stop "${slots[@]}" <<<"$UNITS_SCRIPT"; then
    echo "-- WARNING: could not stop every node (sudo grant missing? re-run setup.sh there)."
    echo "            Starting them again with the binary unchanged."
    ssh_run "$TARGET" start "${slots[@]}" <<<"$UNITS_SCRIPT" || true
    return 2
  fi

  echo "-- copying binary"
  if ! scp -q "$BINARY" "${SSH_USER}@${TARGET}:rubix-lab/nodes/rubixgoplatform.new"; then
    echo "-- ERROR: copy failed. Nodes are STOPPED with their old binary intact."
    echo "          Start them with: cd .. && ./restart-nodes.sh --host ${TARGET}"
    return 1
  fi
  # Into each node folder under a temp name, then renamed into place - atomic,
  # so no node ever sees a half-written binary.
  if ! ssh_run "$TARGET" "${slots[@]}" <<'REMOTE'
set -e
cd ~/rubix-lab/nodes
for s in "$@"; do
  cp rubixgoplatform.new "node$s/rubixgoplatform.new"
  chmod +x "node$s/rubixgoplatform.new"
  mv "node$s/rubixgoplatform.new" "node$s/rubixgoplatform"
done
rm -f rubixgoplatform.new
REMOTE
  then
    echo "-- ERROR: swapping the binary failed part-way - starting the nodes as they are."
    ssh_run "$TARGET" start "${slots[@]}" <<<"$UNITS_SCRIPT" || true
    return 1
  fi

  echo "-- starting ${#slots[@]} node(s)"
  ssh_run "$TARGET" start "${slots[@]}" <<<"$UNITS_SCRIPT" || echo "-- WARNING: a start command failed"

  # Nine nodes starting at once on one machine take longer than one did.
  echo "-- waiting for the APIs..."
  local i n pending=("${!slots[@]}") still
  for n in $(seq 1 30); do
    still=()
    for i in "${pending[@]}"; do
      curl -s -o /dev/null --max-time 2 "http://${TARGET}:${ports[$i]}/rubix/v1/dids" || still+=("$i")
    done
    pending=("${still[@]}")
    [ "${#pending[@]}" -eq 0 ] && break
    sleep 2
  done
  if [ "${#pending[@]}" -eq 0 ]; then
    echo "-- ${#slots[@]} node(s) up on $BRANCH @ $COMMIT"
    return 0
  fi
  for i in "${pending[@]}"; do
    echo "-- WARNING: node ${slots[$i]} (:${ports[$i]}) did not respond within 60s"
    echo "            check: ssh ${SSH_USER}@${TARGET} journalctl -u rubixgoplatform@${slots[$i]} -n 100"
  done
  return 4
}

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

echo ""
echo "== Deploying to ${#TARGETS[@]} machine(s), ${JOBS} at a time =="
echo "   (output is buffered per machine and printed together when all are done)"

running=0
for TARGET in "${TARGETS[@]}"; do
  {
    # set +e inside the subshell: errexit would abort before the .rc file is
    # written, losing this machine's real result.
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
    2) RESULT[$TARGET]="SKIPPED — unreachable or stop failed, binary unchanged" ;;
    3) RESULT[$TARGET]="SKIPPED — not installed (no binary in a node folder)" ;;
    4) RESULT[$TARGET]="STARTED BUT NOT ALL RESPONDING — check journalctl" ;;
    *) RESULT[$TARGET]="FAILED — nodes may be STOPPED, see output above" ;;
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
echo "  $(( ${#TARGETS[@]} - FAILED )) / ${#TARGETS[@]} machine(s) on $BRANCH @ $COMMIT"
echo ""
echo "  A green summary only means the nodes restarted and answered. Confirm the"
echo "  binary actually changed:   cd .. && python3 node-versions.py --include-fixed"
[ "$FAILED" -eq 0 ] || exit 1
