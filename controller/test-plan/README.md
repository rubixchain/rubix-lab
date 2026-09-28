# Rubix Lab — Test Cases

The lab runs only cases that need what it has and the product's CI does not:
many nodes, concurrency, high value, large wallets, long chains, node kills and
database corruption. Anything `rubixgoplatform/test/integration` already
covers, and single-call checks, are left to the product.

    cd full-test
    python3 validate_cases.py --cases master     # offline check, no fleet
    python3 test_runner.py                       # every case
    python3 test_runner.py --only 'SC-*'         # one asset

## Layout

| File | What it holds |
|---|---|
| `master/master-catalogue.csv` | One row per case, in run order. Opens in Excel. |
| `master/master_cases.py` | No cases. Loads the asset modules and merges them into one suite. |
| `master/rbt_cases.py` | RBT (33 cases) |
| `master/ft_cases.py` | FT (7) |
| `master/sc_cases.py` | Smart contracts (28) |
| `master/crs_cases.py` | Cross-asset (1) |
| `master/gen_cases.py` | General: fleet-wide integrity, drift, locks (10) |
| `master/case_helpers.py` | Shared by more than one asset module |
| `full-test/test_runner.py` | Runs the suite: picks participants per case, prepares them, runs, reports |
| `full-test/rubix_client.py` | API client, including faucet funding |

Every row in the catalogue has code, and every case has a row. To add a case:
write it in its asset module, add it to that module's `CASES`, `ORDER` and
`NEEDS`, and add a catalogue row.

## How a run works

1. **Load the cases** and each one's `NEEDS`: senders, receivers, quorums, and
   the RBT they should hold. Cases that must share DIDs (SC-S-01..05 all watch
   one contract) are one *unit*.
2. **Find the fleet**: every reachable node in `hosts.txt` and every DID on it.
   DIDs are fixed - a node with none is left out, never given one. A node with
   several DIDs is fine.
3. **Check the faucet**, which runs on the controller: port 20000 is the faucet
   DID, port 20010 the faucet quorum. Both DIDs are read from the nodes; the
   faucet node must have exactly one quorum, the faucet quorum.
4. **Run units as nodes free up.** For each unit, in catalogue order:
   - wait until enough nodes are free (nodes, not DIDs, are what is busy: the
     quorum a transfer uses is set per node);
   - pick DIDs: quorums, then senders holding the most RBT, then receivers
     holding the least;
   - set up the quorums, reset each participant node's quorum list to just
     its quorum (so which quorum signs is certain), and faucet any shortfall;
   - run its cases, checking the wallets' database rows before and after each.

   Units whose nodes don't overlap run at the same time: many small cases run
   side by side, a case needing most of the fleet runs nearly alone. `exclusive`
   units (the fleet-wide sweeps) and `senders: "all"` units run with nothing
   else running. `last` units (DB corruption, drift and lock baselines) start
   after everything else has finished.
5. **Report** in catalogue order, with the participants each unit used, plus
   the fleet ledger (fleet + faucet, compared with the last run).

A case that asks for more nodes than the pool has is skipped with the reason
(`RBT-Q-06` needs 46).

## Columns

| Column | Meaning |
|---|---|
| Test ID | `ASSET-GROUP-NN`, e.g. `RBT-V-11` = RBT, Transfer Value, case 11 |
| Asset | RBT / FT / SC / Cross-Asset / General |
| Op Group | Operation being tested |
| Test Case | Plain-language description of what to do |
| Setup Method | How it is driven — see below |
| Quorum Setup | Single = one quorum throughout; Multi = senders spread over the fleet's quorums; N/A |
| Expected Result | What should happen. For ladders this is *"record the limit"*, not pass/fail |
| Also Check In Same Run | Extra things verified from the same run, so it is not repeated |
| Priority | P0 blocking / P1 core |
| Code Ref | Product source the rule comes from |
| Implemented In | The module holding the case's code |

### Setup methods

| Method | Meaning |
|---|---|
| `API` | Normal API calls |
| `API-RACE` | Several calls fired at the same moment |
| `MULTI-NODE` | Needs several nodes in specific roles (e.g. subscribers joining at different depths) |
| `NODE-KILL` | Stop a node, quorum or database mid-operation |
| `DB-SEED` | Edit the database directly to force a broken state. Runs last |

Double-spend is `API-RACE` (a real race through the API), not `DB-SEED`.

**DB-SEED is a failable case, not a cleanup job.** The case corrupts a row and
then runs a real operation on top. If Rubix does not catch the corruption, the
case FAILS and the fix belongs in core. There is no database backup or restore.

## Funding

Every RBT a test DID holds comes from the **faucet DID**, by transfer, signed
by the **faucet quorum**. Nothing mints RBT. The faucet and its quorum hold 2L
RBT each, so no case needs 10,000 RBT or more; the value and wallet ladders stop
at 5,000. FT minting is tested normally.

DIDs are never discarded: each keeps what it holds between cycles and is topped
up only by its shortfall.

A quorum must pledge at least the value it signs (`core/consensus/checks.go`),
so the runner tops up the quorum that will actually sign before a large
transfer. **No case should fail for pledge shortage** — if one does, the run is
invalid.

## One quorum per transaction

Every use of `quorumAddresses` in `core/transaction.go` is `quorumAddresses[0]`:
one quorum signs each transaction, always — the first one registered on the
sender's node. So capacity is measured through a single quorum (`RBT-Q-02` →
`RBT-Q-07`: how many nodes can share one quorum before time degrades). Chains
where the quorum changes at each hop (`*-CH-01`) test a token validated by a
different quorum at every hop.

`GetAllQuorums()` has no `ORDER BY` (`core/wallet/quorum.go`), so "first
registered" is Postgres row order, not a guarantee.

## Database check and evidence - every case

Every case, whatever it asserts itself, is also checked against the nodes'
databases (`full-test/case_evidence.py`). Before the case the runner reads the
wallet rows of all its participants, quorums included; after it:

| Check | What must hold |
|---|---|
| `persisted` | every successful transaction's ID is in `transactions` on the sender's node and the receiver's node |
| `conserved` | the participants' RBT (free + locked + pledged + committed + burnt-for-FT) changed by exactly what came in from the faucet, minus anything sent outside the case |
| `no_locks` | no RBT left Locked that was not Locked before |
| `rows_valid` | every new token has a chain row and a legal value; `token_denom` moved with the free rows |

Also reported for every case, **without deciding the verdict** (the fullnode
observes, it is not part of a transfer): `fullnode` - how many of the case's
transactions the fullnode accepted (`fullnode_transactions`) and rejected
(`fullnode_invalid_transactions`, with the reason). The end of the run lists
every rejection grouped by reason.

The fullnode validates with several workers in parallel and gives a
transaction 3 attempts ~6s apart (`core/fullnode_txn_processor.go`). Two
transactions on the same token close together can be checked out of order and
rejected with a previous-transaction mismatch - that is a fullnode finding.
**Every case that makes transfers runs twice - without delay, then with
delay** - and the fullnode's verdict on each transfer is compared:

- *Without delay*: the case runs as written, the product's raw behaviour.
- *With delay*: before any DID starts a transaction it waits until the
  fullnode has processed that DID's last one (`rubix_client._pace`). Parallel
  bursts stay parallel; chains on one token are paced step by step.

Transfers are matched between the runs by who sent to whom, the amount and the
order (faucet top-ups left out), and each lands in one bucket: **accepted both
ways**; **accepted only with delay** (the fullnode falls behind when a token
moves again too soon); **accepted only without delay** (unexpected);
**rejected both ways** (not a timing problem - the reason is given). The case
passes only if it passes both ways; the comparison is reported in its row and,
per transfer with both transaction IDs, under `fullnode_comparison` in the
evidence file. Cases that make no transfers run once.

The report's notes use roles ("the sender", "receiver 2", "the quorum"), not
IP addresses; the exact rows and IDs are in the evidence file.

A case that passed its own assertion but fails any of the four checks is **FAIL**. A
database that cannot be read is reported as such, never as a verdict. The
runner will not start without `python3-psycopg2`.

**Evidence:** each report row carries a `DB:` summary, and
`reports/json/<run>_db-evidence.json` holds, per case: the participants, every
transaction it made (with the transaction ID the node returned), each wallet's
totals before and after, and each check with its detail.

## Pass / fail

- Every P0 must pass.
- Value must reconcile exactly: per case (above) and across runs in the fleet
  ledger (fleet + faucet).
- Ladders record a limit; the limit dropping between releases is the
  regression signal.
- Thresholds are baseline-relative: the first clean run on a known-good release
  sets the baseline; later runs flag more than 20% worse.
- `RBT-S-02` is a known open bug and should fail in its usual way; a different
  failure is a new finding.
