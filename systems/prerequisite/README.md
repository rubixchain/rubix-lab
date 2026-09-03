# prerequisite/

Everything a machine needs to run a Rubix lab node, in one place. `setup.sh`
copies from here into `nodes/<name>/`.

## Contents

| File | Committed? | Notes |
|---|---|---|
| `localnetswarm.key` | **Yes** | Shared secret. Every node needs the *identical* file to join the same private network. Never regenerate it per machine. |
| `rubixgoplatform` | Your choice | The node binary. See below. |
| `ipfs` | Your choice | IPFS/kubo binary. See below. |
| `kubo_v0.19.1_linux-amd64.tar.gz` | Optional | Committing the tarball instead of the extracted binary saves about 65MB. `setup.sh` extracts it automatically. |

## The two binaries — commit or download?

`setup.sh` handles both. Pick based on whether your lab machines can reach
GitHub.

**Option 1 — commit the binaries (works offline, simplest)**

Put `rubixgoplatform` and `ipfs` in this folder and commit them. Every machine
gets everything from one `git clone`. No network needed during setup.

Cost: git never forgets binaries. Each version you commit stays in history
forever — roughly 150MB per full set. For a repo cloned onto 40 machines a few
times a year that is usually acceptable, but it does grow.

**Option 2 — let setup.sh download (keeps the repo small)**

Leave `rubixgoplatform` out. `setup.sh` fetches the latest release from GitHub
and saves it here on first run. Needs `curl` and GitHub access from each machine.

A middle path that works well: commit the **kubo tarball** (34MB, version rarely
changes) and download `rubixgoplatform` (changes every release, so committing it
repeatedly is what actually causes bloat).

**Do not use Git LFS for these.** It looks like the right tool, but GitHub meters
LFS bandwidth at 1GB/month free while ordinary clone traffic is unmetered. Forty
machines cloning ~105MB would use 4.2GB and blow that quota on the first pass.
Plain commits are the cheaper choice at this fleet size.

The usual argument for LFS — repo growth — barely applies here. `ipfs` is pinned
and essentially never changes, and `rubixgoplatform` only moves when the
*baseline* version changes; branch testing goes through `exec-update/`, which
never touches this folder.

## Getting the files yourself

**rubixgoplatform** — from a published release:
```
VERSION=$(curl -s https://api.github.com/repos/rubixchain/rubixgoplatform/releases/latest | grep -oP '"tag_name": "\K[^"]+')
curl -sL -o rubix.tar.gz "https://github.com/rubixchain/rubixgoplatform/releases/download/${VERSION}/rubixgoplatform-${VERSION}-linux-amd64.tar.gz"
tar -xzf rubix.tar.gz && mv rubixgoplatform prerequisite/ && rm rubix.tar.gz
```

Or build from source on any Ubuntu machine (`make compile-linux` in the
rubixgoplatform repo, then copy `linux/rubixgoplatform` here). Building on
Windows is painful because of CGO — use Linux.

**ipfs** — take the tarball already vendored in the rubixgoplatform repo, so you
get the exact version known to work here:
```
cp <rubixgoplatform>/test/docker/rubix/kubo_v0.19.1_linux-amd64.tar.gz prerequisite/
```
`setup.sh` extracts it on first run.

## Note on the swarm key

This key is what keeps the lab network private and separate from CI, testnet and
mainnet. It is deliberately different from the one in the rubixgoplatform test
harness. Keep it in this repo (which is private) and do not publish it.

Changing it means every node must be rebuilt — they would no longer be able to
find each other.
