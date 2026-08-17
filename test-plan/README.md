# Rubix Lab — Test Catalogue

`rubix-lab-test-catalogue.csv` — 259 cases, opens directly in Excel.

Organised by **asset first**, then by operation. Written in plain language so the
row itself explains the test. Each case cites the code it came from, so it can be
re-checked when the code changes.

## Approach

Cases are not just "can the system do X" — they ask **"where does X stop
working"**. Same operation, pushed along three axes until it fails:

- **Value** — 0.001 → 1 → 100 → 1000 → 10000 → 200000.10, recording where it breaks
- **Concurrency** — 5 → 25 → 50 → 100 → 200 at once, recording pass rate at each step
- **Quorum count** — the same load repeated at 1, 3, 5 and 10 quorums

The point is the crossover: 200 parallel transfers through **one** quorum is
expected to fail badly; the test records how many quorums are needed to bring the
pass rate back up. `GEN-B-08` and `GEN-B-09` build that full grid.

Also new: **Wallet Shape** (same 100 RBT sent from a wallet of 100×1.0 vs one
1000-token vs thousands of tiny tokens — very different cost), **Quorum
Liquidity** (a quorum must pledge at least the transfer value, so it runs dry),
and **Precision** (1000 repeated 0.001 transfers, checking for drift).

## Layout

| Asset | Functional | Performance | Total |
|---|---|---|---|
| RBT | 80 | 8 | 88 |
| FT | 50 | 5 | 55 |
| NFT | 32 | 4 | 36 |
| Smart Contract | 15 | 2 | 17 |
| Cross-Asset | 11 | 1 | 12 |
| General | 41 | 10 | 51 |

**Operation groups** differ per asset because the assets genuinely differ:

- **RBT** — Mint, Transfer, Transfer Value, Precision, Wallet Shape, Split,
  Quorum Capacity, Pledging, Concurrency, Failure, Bulk
- **FT** — Mint, Transfer, Transfer Value, Quorum Capacity, Pledging,
  Concurrency, Multi Hop, Bulk
- **NFT** — Create, Deploy, Execute, Child Mint, Transfer, Multi Hop, Bulk
- **SC** — Deploy, Execute, Callback, Bulk
- **Cross-Asset** — Combined, Restriction, Failure, Concurrency, Bulk
- **General** — Quorum, Chain, Integrity, Node, DB Failure, Bulk

## Columns

| Column | Meaning |
|---|---|
| Test ID | `ASSET-GROUP-NN`, e.g. `RBT-V-10` = RBT, Transfer Value, case 10 |
| Asset | RBT / FT / NFT / SC / Cross-Asset / General |
| Type | Functional or Performance |
| Op Group | Operation being tested |
| Test Case | Plain-language description of what to do |
| Setup Method | How to set it up — see below |
| Quorum Setup | Single / Multi / Fresh per hop / N/A |
| Expected Result | What should happen. For ladder tests this is often *"find the limit"* rather than pass/fail |
| Also Check In Same Run | Extra things to verify from that same run — avoids repeat runs |
| Priority | P0 blocking / P1 core / P2 depth |
| Code Ref | Source file:line the rule comes from |

### Setup methods

| Method | Count | Meaning |
|---|---|---|
| `API` | 156 | Normal API call |
| `API-RACE` | 55 | Several calls fired at the same moment |
| `NODE-KILL` | 15 | Stop a node, quorum, or database mid-operation |
| `MULTI-NODE` | 9 | Needs a specific fleet layout (e.g. different quorum per hop) |
| `DB-SEED` | 6 | Edit the database directly with SQL to force a broken state |

**About `DB-SEED`:** these six cases deliberately corrupt data to check the
system catches it. Each needs its SQL written down and a restore step after. Run
them **last in a cycle**, never in the middle of a clean run.

**Double-spend does not need `DB-SEED`.** Sending the same token twice at once is
a real race driven through the API (`RBT-N-01`, `FT-N-01`, `NFT-X-06`).

## Quorum funding is a setup rule, not a test variable

**Every quorum must stay well funded at all times.** Keep a floor of 1000 RBT per
quorum, check before and during each run, and top up anything approaching it.
Localnet minting is free, so there is no reason to ever let a quorum run low.

This is deliberate: a quorum has to pledge at least the value being transferred
(`core/consensus/checks.go:539`), so an underfunded quorum would fail transfers
for lack of money and hide the thing we actually want to measure. **No test
should ever fail for pledge shortage.** If one does, the run is invalid — fund
the quorum and repeat. `RBT-Q-11` and `GEN-Q-08` exist to enforce this.

## The real question: how many nodes can share one quorum

Not "1 quorum or 3 quorums" as a capacity number — the question is **how many
nodes can use one quorum at the same time before transaction time degrades**, and
whether spreading across quorums actually brings that time back down.

Every sender always uses its **primary** quorum — the first one it registered
(`core/transaction.go:171`) — so everyone sharing a primary funnels through one
node regardless of how many quorums exist.

The ladder (`RBT-Q-01` → `RBT-Q-10`):

| Test | Setup | Measures |
|---|---|---|
| `RBT-Q-01` | 1 node, 1 quorum | Baseline time, zero contention |
| `RBT-Q-02`…`Q-06` | 2 → 5 → 10 → 20 → 40 nodes on **one** quorum | Where time starts climbing |
| `RBT-Q-07` | Climb until it degrades | **The headline number: nodes per quorum** |
| `RBT-Q-08`…`Q-10` | Same 40 nodes across 2 / 5 / 10 quorums | Does spreading reduce time, and by how much |

`FT-Q-05` runs the same ladder for FT and compares — does FT cope better or worse
than RBT at the same node count. `GEN-Q-10` is the headline comparison: single
versus multi quorum at identical load.

**One setup trap `GEN-Q-09` guards against:** having 5 quorums registered does
nothing on its own. If every node still has quorum-1 as its *primary*, all five
exist but only one does any work — and the "5 quorum" run is secretly a 1-quorum
run. Verify which quorum actually signed rather than assuming.

## One transaction always uses exactly one quorum

Verified by checking every use of `quorumAddresses` in `core/transaction.go`:
the pledge request, the consensus call, and signature verification are **all**
`quorumAddresses[0]`. There is no loop. The comment at `core/transaction.go:184`
("we can have multiple quorums, we need to loop over them") describes a loop that
was never written.

So "how many quorums does one transfer use" is **not a testable variable** — it
is always 1. What can be varied is *which* quorum is primary (`GEN-Q-14`), how
many nodes share one primary (the `RBT-Q` ladder), and how many quorums are
registered on a sender (`GEN-Q-13` — all are fetched every transfer, all but the
first discarded). `GEN-Q-15` records the single-signature fact so it does not get
re-discovered later.

### Verify the primary quorum is stable before trusting quorum groups

`GetAllQuorums()` runs `SELECT did FROM quorum_manager` with **no `ORDER BY`**
(`core/wallet/quorum.go:68`). Postgres does not guarantee row order without one —
it usually returns insertion order for a small table, but that can change after
updates, vacuum, or a different query plan.

The whole quorum-group strategy ("register quorum-1 first so it becomes your
primary") rests on that undefined behaviour. `GEN-Q-11` checks the same quorum is
picked on repeated calls; `GEN-Q-12` checks it survives a node restart. **Run
both before building group-based tests on top.** If the pick does drift, the fix
is a one-line `ORDER BY` in the query rather than a test workaround.

`RBT-Q-13` measures how quickly a quorum's pledged tokens free up after a
transfer settles, since unpledging is event-driven (`core/callback.go:19`). If
recovery lags behind arrival rate, a quorum can stall under sustained load even
while well funded.

## Self-transfer — which assets actually support it

Traced through the code rather than assumed:

| Asset | Supported? | Evidence |
|---|---|---|
| RBT | **Yes** (`RBT-T-05`) | Handled by the local-DID branch, `core/transaction.go:495` |
| FT | **Yes** (`FT-T-09`) | Same path |
| NFT | **No — rejected** (`NFT-X-03`) | `core/consensus/checks.go:403` — a transfer must change ownership |
| SC | Not applicable | Contracts have no ownership transfer, `core/transaction.go:492` |

## Performance means high value and high speed with correct data

Not throughput for its own sake. Every performance row pairs a speed measurement
with a correctness check — the question is always *does the data stay right when
value is high and traffic is fast*. `GEN-B-07` (1000 decimal transfers, zero
drift) and `GEN-V-06` (reconcile immediately after peak parallel load) are the
clearest examples.

Thresholds are **baseline-relative**. The first good run on a known-working
release becomes the baseline; later runs compare against it. Fixed targets can
only be set once a few baselines show the normal variation.

## Pass / fail

- Every P0 must pass. A P0 failure blocks the release.
- Value must reconcile exactly (`GEN-V-01`, `GEN-V-02`, `GEN-B-07`). Not negotiable.
- No P0 performance more than 20% worse than baseline.
- Ladder tests do not pass or fail on their own — they **record a limit**. The
  limit dropping between releases is the regression signal.
- `RBT-S-02` is a known open bug — it should fail in its usual way. A *different*
  failure there is a new finding.

## Not covered by this fleet

The minter allowlist check is skipped on localnet by design
(`core/consensus/minter_allowlist.go:68`), so it cannot be tested here at all.
Closing that gap needs a testnet or a Go unit test.

Validation of transaction internals — bad epoch, forged signature, malformed
token ID — is also absent, deliberately. The node builds and signs its own
transactions (`core/transaction_builder.go:446`), so a healthy node cannot
produce those; the controller only sends `{initiator, receiver, amount}`. Go unit
tests in `core/consensus/checks_test.go` already cover them.
