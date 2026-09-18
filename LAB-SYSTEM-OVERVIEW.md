# Rubix Lab — System Overview (for improving the setup)

Snapshot of the testing setup as it exists on **2026-09-17**, built from the code
in this repo rather than from the existing docs. Several docs are out of date,
and where a doc and the code disagree this file follows the code and lists the
mismatch in §11.

Companion files: `CLAUDE.md` (Rubix behaviour checked against the product
source, and the lab rules), `SETUP-RUNBOOK.md` (setting up the environment),
`controller/test-plan/DB-SCHEMA.md` (node DB columns).

---

## 1. Purpose and hard constraints

- A **private `localnet` Rubix network on ~40 office Ubuntu desktops**, used to
  test `rubixchain/rubixgoplatform` releases, branches and PRs on real hardware
  and a real network.
- **Deliberately not CI.** No GitHub Actions or self-hosted runners. A human
  reaches the controller through TeamViewer and runs the scripts by hand.
- Things the lab can do that the product's own CI (3-node integration suite)
  can't: **fleet scale** (31 nodes), **multiple quorums**, **real concurrency
  across machines**, **mixed-version fleets**, **direct DB checks after long
  or heavy load**, and **comparing two builds on the same fleet**.
- Rules that must hold (see CLAUDE.md for the reasons):
  - Quorums never run out of funds. A pledge-shortage failure makes the run invalid.
  - Never call `CreateDID` on a host that already has a DID. It isn't idempotent.
  - Never regenerate `localnetswarm.key`.
  - Never commit `nodes/`.
  - Use Docker Engine, not Docker Desktop.
  - No Git LFS.
  - Never mint local RBT with `start_index=0`, because token IDs collide across nodes (§6.3).

---

## 2. Topology and current fleet state

| Role | Host(s) | Notes |
|---|---|---|
| Fullnode | `192.168.1.101` | Bootstrap seed. Its peer ID is in every node's bootstrap list. **Open issue:** it rejects every pubsub transaction with `initiator signature verification failed`. Leading theory: it was left out of the fleet DB wipe, so it still holds old state. |
| Explorer | `192.168.1.102` | **Not built.** No node runs there. |
| Controller | `192.168.1.103` | Runs all tooling. Talks to nodes over HTTP `:20000`, Postgres `:5433` and SSH. |
| Pool | 31 active hosts in `.104`–`.144` | 10 are commented out in `controller/hosts.txt` (8 down since 2026-08-18, plus `.142`/`.143`, which aren't part of the lab). |

- One node per desktop, API **port 20000**, swarm port 4002, Postgres in Docker on **5433**.
- Every pool host has exactly one DID. Fleet build `1.0.4`, plus whatever branch was last deployed.
- **Quorum, sender and receiver are not stored anywhere.** The runner assigns
  them for each run from host order (or from a pinned roles file). `hosts.txt`
  only records fixed roles, down hosts, and the opt-in `multidid` tag.
- Every node runs under systemd as `rubixgoplatform.service` with
  `Restart=always`. A scoped passwordless-sudo grant covers
  start/stop/restart/daemon-reload/enable of that one unit.

### Two install layouts exist — the fleet uses the hand-built one

| | `systems/install/setup.sh` (as documented) | **Real fleet (hand-built)** |
|---|---|---|
| Binary / workdir | `~/rubix-lab/nodes/testnode/` | `~/Desktop/rubix/` |
| Node data (`-p`) | same dir | `~/Desktop/rubix/node` |
| DB container / volume | `rubix-node-db` / `rubix_node_pgdata` | `node` / `pgdata_node` |

All controller tools default to the **real** layout. `exec-update` needs
`REMOTE_BIN_REL=Desktop/rubix` (set in `exec-update.env`). If that value is
wrong, the binary is copied somewhere systemd never reads, the old binary
restarts, and the script **still reports OK**. Only `node-versions.py` shows the
mistake.

---

## 3. Layer 1 — Node install (manual, once per machine)

`systems/install/setup.sh` + `config.toml.template` + `rubixgoplatform.service`,
using the committed binaries in `systems/prerequisite/` (`rubixgoplatform`,
`ipfs` kubo v0.19.1, `localnetswarm.key`).

Order: install Docker Engine → clone the repo → bring up the **fullnode first**
with `LOCALNET_BOOTSTRAP_NODES='[]'` → read its peer ID → bring up every other
node pointing at it → open ufw 20000/4002 → check with `/node/ping` and
`ipfs swarm peers`.

`LAB-QUICKREF.txt` §6 is the copy-paste block that turned the hand-built nodes
into systemd services and added the sudoers grant.

The install does **not** create DIDs, set up quorums, or fund anything. That
all happens on the controller (layers 2 and 4).

---

## 4. Layer 2 — Fleet operations tooling (`controller/`)

Every tool reads `controller/hosts.txt`, the single source of truth. The shell
tools skip fixed-role hosts by default and run all hosts in parallel.

| Tool | Transport | Effect | Purpose |
|---|---|---|---|
| `check-nodes.py` | HTTP | read-only, writes `inventory.json` | ping / DID count / quorums / RBT balance per host |
| `preflight-check.sh` | SSH + HTTP | read-only | readiness table: ssh, docker enabled+active, pg container + restart policy, unit, sudo -n, binary path, api, DID count |
| `node-versions.py` | SSH | read-only, writes `versions.xlsx` | `./rubixgoplatform -v` per host. **There is no version API.** |
| `dids-to-excel.py` | HTTP | **writes**: creates and registers DIDs only on hosts with 0 DIDs | DID inventory → `dids.xlsx`; flags hosts with more than one DID |
| `setup-ssh.sh` | SSH | writes `authorized_keys` | one-time key distribution |
| `restart-nodes.sh` | SSH | restarts services | systemd restart (nohup fallback). By default skips hosts that are already up; `--force` bounces all |
| `wipe-node-db.sh` | SSH | **destroys** the DB container+volume and `localnet/` (DID private keys) | full identity/wallet reset. Dry run unless `--yes` |

---

## 5. Layer 3 — Build and deploy a branch (`controller/exec-update/update-exec.sh`)

```
REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh <branch> <ip...> | --all
```

1. **Build once on the controller**: git fetch/checkout/`pull --ff-only` in
   `REPO_DIR` (default `~/Desktop/rubixgoplatform`), then `make compile-linux`
   (needs Go 1.22 and CGO, so it must build on Linux). `PREBUILT_BINARY=` skips
   the build. If the build fails, no host is touched.
2. **Per target** (parallel by default, `JOBS=1` for serial): `systemctl stop` →
   scp to a temp name → atomic rename → `systemctl start` → poll
   `/rubix/v1/dids` for up to 30s.
3. Prints one summary line per target (branch/commit, or what to check).

It has to use SSH, not the API: with `Restart=always`, an API shutdown just
brings the old binary back. DIDs, quorum registrations and wallets survive a
binary swap because they live in the data dir and the DB.

**Mixed-version fleets:** deploy different branches to different IPs, pin the
roles to match (§7.2), and run with `--collect-versions` so the report records
the build for each role.

---

## 6. Layer 4 — The test harness (`controller/test-plan/`)

### 6.1 Folder map

```
test-plan/
  rubix-lab-test-catalogue.csv   LIVE catalogue the runner reads (422 rows)
  master/                        UNTRACKED, IN PROGRESS (being written right now, see §11)
    master-catalogue.csv         396 rows, adds Legacy ID / Implemented In / Core Overlap
    core-covered.csv             29 rows dropped because the product's CI covers them
    master_cases.py              ~11.4k lines: every implemented case merged into one module
  full-test/                     the engine
    case_runner.py               MAIN ENTRY POINT: catalogue driver
    smoke_test.py                end-to-end smoke (82 steps), also provides common setup functions
    test_cases.py                the smoke test's 4 flows (RBT, FT, NFT, SC)
    rubix_client.py              HTTP API client + token-index allocator
    db_client.py                 Postgres client, read-only enforced by the server
    wallet_shapes.py             builds wallet preconditions (parts-only, mixed, drained)
    report_builder.py            PDF + JSON report
    compare_reports.py           compares two builds (INTRODUCED/FIXED/...)
    validate_cases.py            offline shape check of every case against stubs
    rebuild_token_registry.py    recovers the token-index high-water mark from fleet DBs
    preflight.py                 LEGACY setup; only rbt/run_rbt.py reads its output
    master-test-cases.xlsx       STALE: old 250-row sheet, not read by the runner
  rbt/ ft/ sc/ general/ cross-asset/ core/   per-asset case modules (<name>_cases.py [+ extra files])
  suites/*.txt                   named, committed case selections (currently only PR #739)
  README.md, RUBIX-CORE-TESTS.md, DB-SCHEMA.md
```

### 6.2 API client (`rubix_client.py`) — the primitive everything uses

- Almost every mutating call is a **two-step password challenge**:
  `POST <action>` → `{"result":{"id":reqID}}` → `POST /rubix/v1/signature
  {"id", "password":"mypassword"}`. A validation error can come back from step
  1 directly. NFT and SC creation use **multipart** uploads, and the
  `.wasm`/`.rs` extensions are checked literally.
- The transaction endpoint is `/rubix/v1/tx`. In the request, `owner` means
  **the receiver**. For SC it is always `""`. Child NFT mint sends one entry per
  child with `parentNFTId` and no `nftId`.
- The RBT balance is `{balance, locked, pledged}`, and `balance` only counts
  **free** tokens.
- Receivers credit **1–2s after** the sender's call returns, so assertions poll
  (`wait_for_balance`, `wait_for_ft_count`).
- `quorum_add` treats "already exists" as success.
- `create_did` must only be called on hosts confirmed to have **zero** DIDs.
- `fund_did` mints local RBT at about **15s per 1000 tokens** (one token per
  unit, server side). Its timeout scales with the amount, because if the client
  times out the server keeps minting and later balances get polluted.
- Versions are collected over SSH only (`collect_versions`).

### 6.3 Token-index registry (fleet-wide mint safety)

Local RBT token IDs come from a global index. With `start_index=0` every node
counts from 1 on its own, and a shared quorum then confuses tokens that share an
ID (`TokenChainIntigrityCheck` looks them up with no DID scope). So `fund_did`
always takes a range from `full-test/token_index_registry.json`: gitignored,
seeded at 10,000,000, plain read-modify-write, **no file lock**, one writer
assumed.

If the file is missing, that's a **hard error**. Recover with
`rebuild_token_registry.py --write`, which reads the high-water mark from every
node's `tokens` table. Any new code that mints directly without going through
`fund_did` brings the collision back.

### 6.4 DB client (`db_client.py`)

- Connects from the controller to each node at `:5433`, db/user `rubix`,
  password `rubixpass`. Needs `python3-psycopg2` on the controller only.
- **Read-only is enforced by Postgres**: the connection sets
  `default_transaction_read_only=on`, and preflight's `assert_read_only`
  confirms it.
- Writes go only through `writable_query(..., i_understand_this_writes=True)`,
  which logs every statement to stderr. Only DB-SEED cases use it, and they must
  record → corrupt → restore in `finally` → run last.
- If the driver or connection is missing it raises `DBUnavailable`, and cases
  turn that into **SKIP**, never PASS.
- Helpers:
  - `snapshot()` before/after: free, committed, burnt_for_ft, pledged, `token_denom` map, drift
  - `denom_drift`, `token_rows`, `children_of`, `chain_rows`, `unpledge_rows`, `open_pledges`
  - `negative_denoms`, `orphan_tokens`, `duplicate_token_ids`, `transaction_exists`/`participants`
- Every `db.record()` lands in a `*_db-evidence.json` file next to the report.
- Traps: always filter on `token_type` (1 rbt, 2 nft, 3 ft, 4 sc) **and** on
  `did`, because one node's `tokens` table holds several DIDs.

### 6.5 Wallet shapes (`wallet_shapes.py`)

Builds the precondition a case needs instead of skipping: drain whole tokens to
a sink in 50-RBT chunks, check the result against the `tokens` table, fund with
fractions only, then check again. Used by the FT-from-parts and SC parts cases.

---

## 7. The run flow end to end (`case_runner.py`)

```
python3 case_runner.py --suite pr-739
python3 case_runner.py --cases sc,general --only 'SC-C-*,GEN-IN-11' --report-name x
```

### 7.1 Steps

1. **Select.**
   - `--suite <name>` reads `suites/<name>.txt` (one ID or `PREFIX*` per line).
     Modules come from the ID prefix: RBT→rbt, FT→ft, NFT→nft, SC→sc,
     CRS→cross-asset, GEN→general.
   - `--cases a,b` merges modules. A duplicate Test ID or lane name is a hard error.
   - `--only` filters. An unknown exact ID, or a prefix that matches nothing, is
     a hard error.
2. **Load catalogue text** from `rubix-lab-test-catalogue.csv` (the case, the
   expected result, "also check", code ref) into a `CaseInfo` for each ID.
3. **Pool + DID readiness** (`smoke_test.sweep_and_prepare`, parallel):
   - Unreachable → excluded.
   - 1 DID → ready.
   - 0 DIDs → `create_did` (the only place DIDs get created automatically).
   - More than 1 → excluded, unless the host is tagged `multidid`.
   - Then an announce pass (register+signature for every ready DID) and a 3s settle.
4. **Roles.**
   - Default: the first `--quorum-count` (3) ready hosts become quorums; the
     rest alternate sender/receiver.
   - `--dump-roles f` writes the assignment and exits.
   - `--pin-roles f` forces an exact assignment, and a pinned host that isn't
     ready is fatal. Use it for mixed-version runs.
5. **Quorum setup + funding.** `quorums/setup` on each quorum, then top up to
   `--fund-quorum` (default **2000**). Any setup failure aborts the run.
6. **Participant funding.**
   - Module has no LANES: every sender registers **one** quorum, round-robin,
     and is funded to `--fund-sender` (200).
   - Module has LANES: funding is deferred to each lane (step 8).
7. **Lane allocation** (`build_lanes`):
   - **Reserved lanes** (`"reserve": True`) take hosts from the end of the pool
     for the whole run and form their own **final wave**. Used for baselines,
     drift attribution, lock counting and fleet sweeps.
   - The other lanes fill the rotating pool in order. When the pool is full, a
     new **wave** starts after the current one finishes.
   - A lane that needs more hosts than the pool has is SKIPped with a reason.
   - Each lane gets 1 sender plus N−1 receivers and a `CaseContext`, where
     `ctx.fleet` is **all ready hosts** so fleet sweeps really cover the fleet.
8. **Execute.**
   - Waves run in order; lanes inside a wave run in parallel (thread pool).
   - Each lane first registers `quorum_hosts[0]` on its hosts and tops them up
     to `fund` + 10 RBT.
   - Cases inside a lane run **one after another**, because they assert on
     balance deltas and some depend on each other (SC-S-*, FT-P-*).
   - An exception becomes a FAIL with `actual="exception"`.
9. **Report.**
   - Rows are merged back into catalogue order.
   - Output: `reports/json/catalogue_<name>_<ts>.json`, `reports/pdf/...pdf`
     (needs reportlab; the JSON is always written), and `*_db-evidence.json`.
   - Run conditions recorded: hosts per role, lane→host mapping, funding,
     reduced-scale flags, versions (per role with `--collect-versions`, else a
     manual `--version-label`).
   - The PDF has: summary → conditions → meaning of SKIP → all rows → failures
     → timing/limit rows (`TIMING_CASES`).

### 7.2 Case contract

```python
def case(ctx, ci) -> (passed, actual, note)   # passed: True | False | "SKIP"
CASES = {"SC-C-01": fn, ...}; ORDER = [...]
LANES = {"lane-name": {"cases": [...], "hosts": N, "fund": RBT, "reserve": bool}}
TIMING_CASES = {...}   # rows whose "actual" is a measurement, not a verdict
CASE_INFO = {id: (case_text, expected)}   # fallback when the ID isn't in the catalogue
```

- `ci.expects_rejection` and `ci.is_record_only` come from the catalogue's
  Expected Result text.
- **SKIP is never a pass.** It needs a reason, and the pass rate is calculated
  over attempted cases only.
- Case idioms:
  - DB `snapshot` before and after, then assert on the **delta**.
  - `new_drift` so earlier drift isn't blamed on this case.
  - Poll for receiver credits.
  - Fleet sweeps print `N host(s) checked (fleet)` or `(LANE ONLY)`.

### 7.3 PR verification workflow (what the lab is currently used for)

```
python3 validate_cases.py --cases sc           # offline: every case runs against stubs (shape only)
REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh <PR branch> --all
python3 case_runner.py --suite <pr> --report-name pr --collect-versions
REMOTE_BIN_REL=Desktop/rubix ./update-exec.sh <merge-base> --all
python3 case_runner.py --suite <pr> --report-name base --collect-versions
python3 compare_reports.py <pr.json> <base.json>
```

`compare_reports.py` labels each case by status only:

| Label | Change build | Baseline build |
|---|---|---|
| **INTRODUCED** (the only blocker) | fail | pass |
| FIXED | pass | fail |
| PRE-EXISTING | fail | fail |
| CLEAN | pass | pass |
| INCONCLUSIVE | a skip on either side | |

It exits 1 if anything is INTRODUCED.

Suites that exist:
- `pr-739.txt`: functional. SC-*, FT-P-*, FT-DB-04, CRS-C-*, GEN-IN-*.
- `pr-739-verify.txt`: 25 cases. Proof of the fixes, the evidence for each blocker, and findings that belong to other code.
- `pr-739-stress.txt`: SC-X-*, FT-X-* and concurrency cases, with `--scale`, then GEN-IN-* last.

PR #739 result so far: request changes. Two regressions were shown against
main (SC-C-27 collateral lost on a rejected deploy; SC-C-29 multi-contract 3dp
float error). Two findings belong to other code (quorum `token_denom` drift;
a rejected FT mint leaves tokens Locked). The `pr-739-verify` confirmation run
has **not been done yet**.

### 7.4 Other entry points

- **`smoke_test.py`**: same common setup, then `test_cases.TEST_CASES`
  (RBT transfers, FT mint+transfer, NFT create/deploy/execute, SC
  generate/deploy/execute). Last result 82/82. Defaults: quorum 1000, sender 20.
  Writes a PDF only.
- **`preflight.py` + `rbt/run_rbt.py`**: the older pilot path. Registers
  *every* quorum on every participant and writes `preflight-context.json`. Only
  `run_rbt.py` reads that file. Nothing current uses it.

---

## 8. What is actually implemented (measured by importing the modules)

| Module | Cases | Lanes | Notes |
|---|---|---|---|
| `sc/` (8 files) | 56 | 28 | SC-C 33, SC-Q 7, SC-S 10, SC-X 5, SC-DB 1. The most developed module (PR #739 work) |
| `general/` (4 files) | 17 | 6 (4 reserved) | GEN-IN integrity: denom baseline/drift, lock release, fleet invariants |
| `ft/` (3 files) | 13 | 8 (1 reserved) | FT-P parts/burn path, FT-X scale, FT-DB-04 |
| `cross-asset/` | 4 | 3 | CRS-C-01/02/03/06 |
| `rbt/` | 80 | **0** | Legacy IDs `RBT-001..080`, which match nothing in the live catalogue. 17 timing cases |
| `core/` | 25 | 0 | `CORE-*` re-implementations of the product's CI checks. Candidate for deletion (the owner says CI already covers them) |
| `nft/` | — | — | **Does not exist** |
| `master/master_cases.py` (untracked) | 161 | 45 (5 reserved) | Merge of rbt (renumbered to catalogue IDs, 71 cases) + ft + sc + cross-asset + general |

Catalogue coverage:
- `master-catalogue.csv` has **161 of 396 rows implemented (41%)**.
- By asset: RBT 91, FT 77, NFT 44, SC 86, Cross-Asset 30, General 68.
- By setup method: API 224, API-RACE 92, NODE-KILL 33, MULTI-NODE 27, DB-SEED 20.
- **Never implemented anywhere**:
  - NFT (0 of 44)
  - NODE-KILL (no orchestration exists)
  - MULTI-NODE layouts beyond what lanes give
  - 18 of 20 DB-SEED rows (only FT-DB-04 and SC-DB-03 exist)
  - Infra-layer rows (41)

Priority: 306 of 396 rows are P0, so priority does not currently separate anything.

---

## 9. Timing and scale facts

- Mint: ~15s per 1000 RBT. This limits value ladders; 100k RBT is about 25 min per wallet, plus the same again for the quorum.
- Node restart to "Server running": ~15s. A cold start after a wipe is slower (`restart-nodes.sh --attempts 30`).
- Receiver credit lag: 1–2s.
- A 78-case run in a single lane took 40 min. With lanes on 31 hosts the design goal is under 10 min.
- Only 3 quorums are used (`--quorum-count`). Every lane shares them, so they compete for pledge capacity. That's why quorums are bulk-funded (2000) and lanes get only what they spend.

---

## 10. Reports and state on the controller (all gitignored)

| Path | Nature |
|---|---|
| `reports/json|pdf/…` | Run results. The PR #739 main baseline `catalogue_pr-739_2026-09-09_17-42-02.json` must be kept; the fleet has since moved off main |
| `full-test/token_index_registry.json` | **Not disposable**: the fleet's mint high-water mark |
| `full-test/smoke-test-roles.txt`, `preflight-context.json` | Snapshots of the last role assignment |
| `controller/inventory.json`, `dids.xlsx`, `versions.xlsx` | Inventory snapshots |
| `controller/exec-update/exec-update.env` | Controller-specific deploy settings |

No run history, trend store or dashboard exists. Runs are compared by hand with
`compare_reports.py`, two at a time.

---

## 11. Known gaps, inconsistencies and defects (improvement targets)

### A. Harness correctness (these can make a run look clean when it isn't)

1. **Cases outside any lane silently disappear.**
   - What happens: when a module defines `LANES`, `build_lanes` only schedules
     cases listed in a lane. The report merge adds rows only for scheduled or
     "unallocated" cases. Any other case is **neither run nor reported**.
   - Impact: in `master_cases.py` all **71 RBT cases have no lane**, so
     `--cases master` drops every RBT case without a word. The report's
     "N of M" line is the only hint.
   - Fix: add those cases as SKIP rows, or put them in a default lane.
2. **Laned runs use the first quorum as primary everywhere.**
   - What happens: `fund_lane` registers `quorum_hosts[0]` on every lane host.
     `sender_quorum` is filled round-robin but never registered. A host that
     registered a different quorum first in an earlier run keeps that one,
     because registrations persist in the DB.
   - Impact: `ctx.quorum_for()` may not name the quorum that actually signs,
     and the "spread across 3 quorums" is mostly quorum 1.
   - Related: GEN-Q-11/12 (is the primary quorum stable?) aren't implemented,
     and `GetAllQuorums()` has no `ORDER BY`.
3. **`compare_reports.py` compares status only.** Cases that fail on both
   builds for *different* reasons (SC-C-29, CRS-C-03) get labelled
   PRE-EXISTING. It also ignores the measurements in timing and limit rows, so
   a ladder limit dropping between builds isn't detected. The README says that
   drop is *the* regression signal.
4. **`validate_cases.py` only proves cases run, not that they're right.** Every
   stub returns success.
5. **Open harness bugs from the 2026-09-09 baseline** (per project notes, not
   re-checked since 2026-09-10):
   - FT-DB-04 and SC-DB-03 raise exceptions.
   - SC-DB-03 may not have restored `token_denom` on `.110`.
   - GEN-IN-20/21/23/24 and FT-P-09 skip.
   - These need clearing before the paired `pr-739-verify` run.
6. **Token registry has no lock.** Two mint-heavy processes at once would hand
   out overlapping ranges.
7. **Deploy can falsely report success** if `REMOTE_BIN_REL` is wrong (§2).
   Nothing checks the version automatically after a deploy.

### B. Structural and source-of-truth drift

8. **Consolidation into `master/` is half done and untracked.**
   - `master_cases.py` duplicates the per-asset modules, which still exist and
     are what suites use (`PREFIX_MODULE` points at `sc`, `ft`, etc.).
   - `case_runner.CATALOGUE_PATH` still reads `rubix-lab-test-catalogue.csv`,
     not `master/master-catalogue.csv`.
   - Fixing a case in one copy leaves the other stale. The files were being
     written at 11:41 on 2026-09-17, so they are still changing.
9. **Four catalogues with different counts.**
   - Docs say 259 (CLAUDE.md, test-plan/README.md).
   - `master-test-cases.xlsx` has 250 rows. `full-test/README.md` calls it the
     source of truth; `case_runner.py` calls it stale.
   - `rubix-lab-test-catalogue.csv` has 422 rows (the live one).
   - `master-catalogue.csv` has 396 rows, plus 29 in `core-covered.csv`.
   - Legacy RBT-001..080 IDs are only mapped in `master-catalogue.csv`'s
     Legacy ID column.
10. **Out-of-date statements in docs:**
    - CLAUDE.md says "Step 2 controller not built". It is built as `case_runner`.
    - CLAUDE.md says "DB verification deferred". `db_client` is live.
    - The `rbt_cases` header says DB-SEED is deferred.
    - CLAUDE.md says the token registry "can't be reconstructed". It can.
11. **Three setup paths with different quorum registration rules**:
    `preflight.py` (all quorums), smoke / non-laned (one each, round-robin),
    laned (quorum #1 everywhere). `preflight.py` + `run_rbt.py` are legacy.
12. **`core/` module**: the owner has decided CI covers it. Delete it, or keep
    it only as `core-covered.csv`.
13. **Install docs describe a layout the fleet doesn't use** (§2).
    `wipe-node-db.sh` and the other tools hard-code the hand-built layout.

### C. Coverage and capability gaps

14. **NFT**: no lab cases at all (0 of 44 catalogue rows).
15. **NODE-KILL** (33 rows): nothing kills a node or quorum at a chosen moment
    in a transaction. SSH and the sudoers grant already allow it.
16. **DB-SEED**: 2 of 20 implemented.
17. **Quorum-scale experiments** (RBT-Q ladder, GEN-Q, 1/3/5/10 quorums): quorum
    count is fixed per run, and there's no reliable way to control or observe
    which quorum signs a transaction (would need DB `unpledge_sequence_info` /
    tx info per hop).
18. **Environment validation is incomplete**: the explorer isn't built, and the
    fullnode signature rejection hasn't been diagnosed. Phase E's "fullnode and
    explorer show the transaction" has never passed.
19. **No baselines or history**: the "baseline-relative, no more than 20%
    regression" rule has no stored baseline, no trend store, and no automatic
    threshold check.
20. **No automation**: every step is a manual command over TeamViewer. There's
    no one-command cycle (preflight-check → deploy → verify versions → suite →
    compare → publish report), no scheduling, and no notifications.
21. **Priority column isn't useful**: 77% of rows are P0.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| **Pool** | Hosts in `hosts.txt` without a fixed role |
| **Lane** | A set of hosts owned exclusively by a sequential list of cases, so balance deltas can't interfere |
| **Wave** | A group of lanes that run in parallel. Waves run one after another; the reserved wave is last |
| **Reserved lane** | Hosts held for the whole run, so nothing else touches that wallet (baselines, drift attribution, lock counts, fleet sweeps) |
| **Suite** | Committed `.txt` list of IDs/prefixes that names a selection of cases, usually one PR's verification set |
| **`token_denom`** | Per-DID, per-denomination counter that token selection relies on. **Drift** is the counter disagreeing with the actual Free rows in `tokens` |
| **Record-only / timing case** | A case whose `actual` is a measured limit or duration rather than a pass/fail verdict |
| **SKIP** | Not attempted, with a reason. Never counted as a pass |
