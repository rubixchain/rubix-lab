# Rubix lab fleet — branch-to-exec deploy tool

Give it a branch name and one or more target desktops; it builds
`rubixgoplatform` from that branch and swaps it into place on each target,
restarting the node. Runs from the **controller desktop** (one of the 40,
per your setup — reachable via TeamViewer/remote access for you, and with
SSH access out to the other desktops for this tool).

## Why this is SSH-based, not the Rubix API

`rubixgoplatform.service` (installed by [../install/setup.sh](../install/setup.sh))
has `Restart=always`. Calling the node's own graceful-shutdown API endpoint
would make the process exit — and systemd would immediately relaunch the
*old* binary underneath you, since from the supervisor's point of view
nothing asked it to stop. Actually stopping it needs `systemctl stop`,
which is an SSH operation. So the whole flow — stop, copy, swap, start — is
SSH end to end, not something the HTTP-based controller/operations tooling
can do.

## One-time setup (on the controller)

Two different repos are involved here, don't mix them up: the **product
repo** (`rubixgoplatform`, cloned for *building* a branch) and **this
`rubix-lab` repo** (cloned on every target desktop, containing each node's
actual `nodes/<name>/` folder — see [../install/README.md](../install/README.md)).

1. Clone `rubixgoplatform` somewhere on the controller (default
   `~/rubixgoplatform`, override with `REPO_DIR=/path ./update-exec.sh ...`)
   — this is the *build* source, unrelated to where `rubix-lab` lives.
2. Install `Go 1.22` + `build-essential` on the controller (needed for
   `make compile-linux`).
3. Generate an SSH key on the controller if it doesn't have one
   (`ssh-keygen`), and copy it to every target desktop:
   `ssh-copy-id <user>@<target-ip>` — one time per desktop.
4. On **each target desktop**, install the scoped sudo grant from
   [rubix-exec-update-sudoers.example](rubix-exec-update-sudoers.example) —
   it only allows starting/stopping this one service, nothing broader.
5. Copy [hosts.txt.example](hosts.txt.example) to `hosts.txt` in this folder
   and fill in your fleet's real IPs, if you want to use `--all`.
6. Make sure every target cloned **`rubix-lab`** to the same path relative
   to its own `$HOME` (default assumption: `~/rubix-lab` — override with
   `REMOTE_REPO_REL=path ./update-exec.sh ...` if you used something else).
   The script resolves each target's real `$HOME` over SSH at deploy time,
   then deploys into `<that clone>/nodes/<NODE_NAME>` (`NODE_NAME` defaults
   to `testnode`, same default as `install/setup.sh` — override both
   consistently if you ever rename it).

## Usage

```
./update-exec.sh <branch> <target-ip> [target-ip...]
./update-exec.sh <branch> --all
```

Example — test `fix/pledge-split` on two desktops:
```
./update-exec.sh fix/pledge-split 10.0.0.14 10.0.0.22
```

## What it does, in order

1. **Builds first, deploys only if the build succeeds** — `git fetch` +
   `checkout` + `pull --ff-only` + `make compile-linux` on the controller.
   A bad branch name or a compile error stops here; no target is ever
   touched because of a build failure.
2. Per target, sequentially: `systemctl stop` → copy the new binary to a
   temp name and rename it into place (atomic swap, never a half-written
   binary at the real path) → `systemctl start` → poll `/rubix/v1/dids`
   for up to 30s to confirm it actually came back up.
3. Prints a summary line per target: which branch/commit it's now running,
   or exactly what to check if it didn't come back cleanly.

## What it deliberately does NOT do

Doesn't touch DIDs, quorum wiring, or peering — those survive a binary
swap untouched (they live in the node's data directory, not the binary).
Doesn't shut down or start nodes *before* you decide to — you still choose
which desktops to target and when; this tool doesn't discover or select
targets on its own. Doesn't run in parallel across targets in v1 — simple
sequential loop, easy to parallelize later with `&`/`wait` if 40 restarts
one at a time ever feels slow in practice.
