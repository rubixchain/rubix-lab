# Rubix core issues found by the lab

Every problem in Rubix itself (`rubixchain/rubixgoplatform`) the lab has found,
with the code behind it and the fix it needs. Lab-side (script) mistakes are
not listed here.

The plan for each issue, in order, later:
1. a focused lab case that reproduces it on its own ("Confirm with");
2. once confirmed, the fix in Rubix core;
3. the same case again on the fixed build.

Nothing below has been fixed or reported to core yet unless it says so.

**Code references.** "532c413b" = read on `vaishnav/custom-testnet` @ 532c413b,
the fleet's build since 2026-10-29. "1d9b28bb" or "658cb33f" = read on the earlier
build; re-check the lines on the current build before reporting.

**Status.** CONFIRMED = seen on the fleet and the code path read. SUSPECTED =
seen on the fleet, cause not proven. CODE = found by reading, not yet seen
failing on the fleet.

| ID | Issue | Status | Last seen |
|---|---|---|---|
| R-01 | Concurrent pledges/splits on one DID deadlock on `token_denom` | CONFIRMED | 2026-11-05 |
| R-02 | `token_denom` counter left wrong after a failed pledge | CONFIRMED | 2026-11-05 |
| R-03 | Rubix nodes grow with large transactions until the kernel kills them | SUSPECTED cause | 2026-11-05 |
| R-04 | A quorum killed mid-transfer leaves the sender's token stuck | CONFIRMED (three times) | 2026-11-05 |
| R-05 | A quorum holding exactly the value is refused (float compare) | CONFIRMED (twice) | 2026-11-03 |
| R-06 | Transfer reports success, receiver never gets the tokens | CONFIRMED | 2026-11-05 |
| R-07 | NFT and contract chains fork on simultaneous executes | CONFIRMED | 2026-11-05 |
| R-08 | A former quorum cannot spend tokens it pledged | CONFIRMED | 2026-11-05 |
| R-09 | Failures leave tokens Locked | CONFIRMED | 2026-11-05 |
| R-10 | After an IPFS auto-restart the node is deaf | CONFIRMED | 2026-11-05 |
| R-11 | One node's shutdown kills every IPFS on the machine | SUSPECTED | 2026-10-22 |
| R-12 | Peer requests swallow connection failures | CONFIRMED | 2026-11-05 |
| R-13 | Peer-port pool: leak, overlap, listener inside a neighbour's pool | CONFIRMED | 2026-11-02 |
| R-14 | `NodeConfigDir` never assigned | CODE | - |
| R-15 | Quorum setup lives only in memory | CONFIRMED | 2026-11-03 |
| R-16 | Pledge release checks only one quorum DID per node | CONFIRMED | 2026-11-05 |
| R-17 | Pubsub receive loop spins on a receive error | CODE | - |
| R-18 | NFT/SC operation from a node without the token becomes a second genesis | CONFIRMED | 2026-10-21 |
| R-19 | Fullnodes validate out of order and reach different verdicts | CONFIRMED, 16-20% of tokens behind | 2026-11-06 |
| R-20 | Zero clock tolerance on the transaction epoch | CONFIRMED | 2026-11-05 |
| R-21 | False double spend on repeated small splits | SUSPECTED | 2026-10-06 |
| R-22 | Split check and wallet recovery need Rubix's public fullnode list | CODE | - |
| R-23 | Re-splitting a previously split token collides on `tokens_pkey` | CODE | older |
| R-24 | `GetAllQuorums()` has no `ORDER BY` | CODE | - |
| R-25 | `AddQuorum` returns an error on a repeat | CODE | - |
| R-26 | Signatures over unhashed data fail on Go 1.25+ | CONFIRMED | 2026-10-14 |
| R-27 | `CreateDID` has no idempotency | CODE | - |
| R-28 | Quorum nodes keep Free copies of other DIDs' tokens | to check | 2026-11-02 |
| R-29 | Concurrent pledges on one quorum lose their token locks (~400 at once) | SUSPECTED cause | 2026-11-03 |
| R-30 | The pledge lock plans from `token_denom` counters and never checks what it locked | CONFIRMED code path | 2026-11-03 |
| R-31 | A faucet-minted whole token refused by the allowlist, naming another minter | SUSPECTED, lead: chains without their genesis | 2026-11-06 |
| R-32 | A transfer recorded by the receiver's node but never credited to the receiver | SUSPECTED, code path found | 2026-11-05 |
| R-33 | A quorum refuses a token "chain mismatch after sync" with no kill involved | SUSPECTED, 780 tokens measured | 2026-11-06 |
| R-34 | One RBT counted on two wallets after concurrent sends on one machine | SUSPECTED | 2026-11-05 |

---

## A. Failing the suite on the current build

### R-01 Concurrent pledges/splits on one DID deadlock on `token_denom`
- **Seen:** every run since 2026-10-21. On 2026-11-02: QCAP-01 fractional rung
  of 20 at once (8 refusals), WALLET-PREP (8), COMBO-01 (2). Refusals:
  `PersistPreConsensus: failed to update token_denom ... deadlock detected
  (SQLSTATE 40P01)` and `PledgeV2: decrement token_denom ...`. On 2026-11-03:
  21 refusals - QCAP-01 (13), WALLET-PREP (3), FT-01 (3), COMBO-01 (2). Second
  run 2026-11-03 (14:15): 21 again - QCAP-01 (15), WALLET-PREP (5), COMBO-01 (1).
  Third run (18:13): 10 - FT-01 S6 (7: one owner's five 500-FT sends at once,
  on the quorum's pledge), QCAP-01 already at 10 fractional sends at once (1),
  COMBO-01 (1), SC-01 (1, a different table: `post-consensus persistence:
  insert tokenchain rows: deadlock detected`, two executes of one contract).
  2026-11-05: 31 - WALLET-PREP (16, round 2), QCAP-01 (8, already at 10
  fractional sends at once), FT-01 (5, S5 and S6), COMBO-01 (2).
- **Cause:** every `token_denom` writer updates one row per denomination in Go
  map order, which is random, so two transactions on one DID lock the rows in
  different orders (`core/wallet/persist_genesis_tx.go:209`,
  `core/wallet/post_consensus_persistence.go:672, :703, :716`; 1d9b28bb).
- **Fix needed:** update the denominations in one fixed order (sort the keys),
  or in a single statement over all of them.
- **Confirm with:** one quorum, N senders paying fractional amounts through it
  at once; count deadlock refusals per N.

### R-02 `token_denom` counter left wrong after a failed pledge
- **Seen:** AUDIT-START 2026-11-02: 8 DIDs whose counter does not match their
  Free tokens, e.g. the faucet quorum, 1.0 RBT: counter 178,802, Free tokens
  178,620. First seen 2026-10-21 (0.1: counter 111, rows 107). 2026-11-05: 9
  DIDs at both audits (faucet quorum 1.0 RBT: counter 166,792, Free 166,605 at
  the end), and RACE-02 S2 left a new drift on .108:20000.
- **Cause:** `PledgeV2` commits the pledge, then decrements `token_denom` in a
  second DB transaction (`core/pledge_v2.go:287`; 1d9b28bb). When that one fails
  (for example R-01) the counter stays high. `UnpledgeV2` increments it the same
  way, in a separate cleanup transaction (`core/unpledge_v2.go`; 532c413b).
- **Fix needed:** change the counter in the same DB transaction as the token
  status, on every path.
- **Confirm with:** provoke R-01, then compare each denomination's counter with
  its Free rows on the quorum.

### R-03 Rubix nodes grow with large transactions until the kernel kills them
- **Seen 2026-11-03 (memory read after each batch of units):** the faucet
  quorum went 0.0 GB at the start -> 0.2 GB after QCAP-01 (1,600 one-token
  transfers) -> 2.5 -> 13.3 GB during the wallets unit (a 2,421-token faucet
  draw in 1,000-token chunks, then 1,000- and 2,000-token transfers) ->
  restarted -> 5.9 -> 8.0 GB -> back to 0.1 GB once the wallets unit ended, with
  no restart (`journalctl -k` on .106: OOM-killed at 12:29:19, anon-rss
  14.30 GB - the third kill in three runs). The four fullnodes did the same, 0.1 -> ~3 GB -> 0.1 GB, in the
  same window. Pool nodes in FT-01 S5 (8 owners passing 1,300-1,700-FT series
  hop to hop at once): Rubix processes reached 11-13 GB per machine (.112, .139,
  .140), machines ran out of memory and swapped, and 6 nodes restarted (.118
  slot 7, .124 slot 1 - which also went deaf - .136 slot 3, .137 slot 1, .140
  slot 0, .144 slot 3); the senders' API dropped the connection
  (`RemoteDisconnected`). **So the trigger is transactions carrying many tokens,
  not the number of transactions, and the memory is held while they are
  processed and released afterwards** - a backlog, not a slow leak.
- **Second run 2026-11-03 (14:15), the same pattern:** FT-01 S5 again - Rubix
  4-12 GB per machine on 15 machines (.110: 12.0 GB), machines up to 3.5 GB into
  swap, 9 nodes restarted (.110 slot 3, .113 slot 0, .116 slot 2, .121 slot 8,
  .124 slot 1, .126 slot 5, .128 slot 6, .135 slot 0, .144 slot 6), 7 sends
  lost their connection (`RemoteDisconnected`) and all nine of .135's nodes went
  deaf (R-10). WALLET-01's 2,000-RBT sender (.113:20000) took its machine's
  Rubix to 4.5 GB and 1 GB into swap; that transfer took 64 s. The kernel killed
  the faucet quorum again at 14:22:30 (`journalctl -k` on .106: anon-rss 14.26 GB,
  the fourth kill in four runs), in the wallets unit, before the runner's
  memory watcher could act; the runner restarted it on purpose afterwards at
  6.7 and 6.6 GB, and no case lost its funding. Fullnodes 0.1 -> ~2 GB -> 0.1 GB
  in the same window.
- **Measured 2026-11-03 (third run, 18:13): a QUORUM's memory grows with the
  SQUARE of the tokens in the transaction it signs.** WALLET-01, one transfer at
  a time through quorum 110:20000 (Rubix memory of its machine, step by step):
  100 RBT +0.06 GB, 500 RBT +2.2 GB, 1,000 RBT +8.8 GB (machine down to 19.7%
  free memory) - twice the tokens, four times the memory. Before the 2,000-RBT
  step every node on .110 was down (0.13 GB of Rubix left) and the transfer was
  refused "failed to connect swarm peer" for the quorum; .110's nodes came back
  deaf (R-10). The sender (134:20000) grew linearly, ~1.8 MB per token
  (0.45 -> 0.93 -> 1.8 GB). The faucet quorum did the same in the wallets unit,
  where the faucet pays in 1,000-token chunks: restarted on purpose at 4.6 and
  4.6 GB, restarted by itself ~18:21, then restarted on purpose at 13.3 and
  12.8 GB - each reading taken just before a chunk. **Both kills confirmed with
  `journalctl -k`:** .106 at 18:20:33, anon-rss 14.19 GB - 59 s after the
  planned restart at 18:19:34, so ONE faucet transfer of at most 1,000 tokens
  took a fresh process to 14 GB (the fifth faucet-quorum kill in five runs);
  .110 at 18:27:19, anon-rss 12.62 GB - WALLET-01's quorum, after its 1,000-RBT
  transfer. **This points the cause at the quorum's per-transaction work
  (validating and pledging N tokens), not only at the pubsub backlog below.**
  The code doing O(N^2) work is not found yet - PledgeV2's SQL
  (`core/pledge_v2.go`) works per token; the per-token chain sync in
  `TokenChainIntigrityCheck` is the next place to look.
- **2026-11-05 (faucet chunks of 250 tokens):** the faucet quorum still reached
  7.4 GB and 5.0 GB between draws in the wallets unit; the runner restarted it on
  purpose twice (16:16:57, 16:18:06) and the kernel never killed it - the first
  run without a faucet-quorum kill. WALLET-01's quorum 121:20200, which had
  signed all of QCAP-01 earlier in the run, stopped answering its API after the
  500-RBT transfer (13 s); two steps later its machine was at 97% CPU and by
  16:25 its nodes were deaf (R-10; the runner restarted them). FT-01 S5 (8
  owners passing 1,300-1,700-FT series at once): Rubix at 10.4 GB on .107,
  11.4 GB on .110 and 11.5 GB on .134 (0.8% memory left, 2.9 GB into swap),
  nodes restarted on .122 (slot 4), .126 (4), .128 (6), .134 (5) and .136 (4),
  all nine of .134's nodes deaf, 5 sends `RemoteDisconnected`, 5,400 FTs left
  Locked (R-09).
- **Seen before:** 2026-11-02 the faucet quorum (.106) was OOM-killed at 14:00:31,
  anon-rss 14.27 GB, ~14 min into the run and a few minutes after a 2,000-RBT
  transfer. 2026-10-29: the faucet quorum at 14.2 GB, 24 s after a 2,000-FT
  transfer, plus .111 (13.2 GB), .116 (5.6 GB), .121 (five kills, 2.6-5.1 GB).
  A normal node uses ~50 MB to 0.5 GB. The faucet quorum signs no test
  transfer; it grows from the fleet's traffic.
- **Likely cause (not proven, no pprof in the build):** `types/pubsub.go:173`
  starts one goroutine per published message with no limit
  (`go ps.invokeCallback(...)`). On a node with a quorum set up, each one runs
  `CallBackQuorumUnpledge` (`core/fullnode.go:66-77`, `core/callback.go`),
  several DB round trips per transaction (`core/wallet/pledge.go:490`,
  `core/unpledge_v2.go`) with no timeout. Arrivals faster than the DB answers
  pile goroutines up, each holding its whole transaction. The faucet quorum has
  the biggest DB in the fleet (~178k tokens of its own plus 81,651 copies, see
  R-28), so it answers slowest and grows fastest. All 532c413b.
- **Fix needed:** find and remove the O(N^2) in the quorum's handling of one
  transaction; until then, refuse a transaction with more tokens than the node
  can take, with a stated reason, instead of growing until the kernel kills it.
  Also: a bounded worker pool (or a channel with fixed workers) for pubsub
  callbacks; a context timeout on the callback's DB work; a cheap prefilter
  before the unpledge queries. A pprof endpoint behind a flag would make the
  next case of this diagnosable.
- **Confirm with:** one quorum, one transfer at a time of 100, 250, 500, 750,
  1,000 whole RBT; read the quorum's RSS after each (the 2026-11-03 figures
  should repeat, quadratic). Then a heap profile at 1,000 (pprof), or `kill
  -QUIT` on its MainPID while it is big - Go prints every goroutine's stack to
  the journal.

### R-04 A quorum killed mid-transfer leaves the sender's token stuck
- **Seen:** FAIL-01 S4-S7, 2026-11-02. The quorum (.110) synced token
  60001_1211 from sender A while checking transfer `f6995f0e` (14:06:37,
  `excludeTxIDs=[f6995f0e...]`) and was killed. After the restart its copy of
  the token's chain ends at `f6995f0e`, which never completed (A still holds
  the token; nothing moved). Every later send declares the correct previous
  transaction `43537334`. The sync skips (`applyTokenChainFromSync: prevTxID
  already in local chain`) and the check refuses: `TokenChainIntigrityCheck:
  token 60001_1211 (rbt) chain mismatch after sync ... local latest f6995f0e !=
  expected 43537334` (`core/consensus/checks.go:944`; 532c413b). It happens on
  every attempt through that quorum. Kills of the sender (S2, S3) recovered.
  **Reproduced 2026-11-03** on a different token (60001_121106: `local latest
  03ac7725 != expected 43537334`), after S1 had checked that the same three
  nodes made an ordinary transfer before any kill. **Third time 2026-11-05:**
  after S4 killed the quorum (.110:20000) on "Consensus request parsed
  successfully", every send through it in S4-S7 was refused for token
  60001_121091, `chain mismatch after sync ... local latest d51ea47c...`; S1's
  ordinary transfer had gone through before the kills.
- **Cause:** the quorum writes the in-flight transaction into its token chain
  before consensus completes, and nothing undoes it when the transaction never
  completes. Where it writes it has not been located yet. The same leftover
  without any kill is R-33: on 2026-11-06, 451 tokens had such an entry on
  .110:20000 alone.
- **Fix needed:** write the quorum's chain entry only after consensus completes;
  or, on startup and in the sync, drop a latest entry whose transaction the
  initiator does not have. The sync must not skip when its own latest entry is
  not in the initiator's chain.
- **Confirm with:** kill the quorum on its "took the request" log line; then
  send the same token through the same quorum, and through a different one.

### R-05 A quorum holding exactly the value is refused (float compare)
- **Seen:** QCAP-02, 2026-11-02: `CollectRBTTokens: insufficient balance:
  current balance 125.92999999999999, transfer amount 125.93`. Again, word for
  word, in both runs on 2026-11-03.
- **Cause:** the quorum first locks the tokens it picks for the amount
  (`ReqPledgeToken` -> `w.LockTokensForSplit(did, value)`,
  `core/consensus/consensus.go:31`), then `CollectRBTTokens`
  (`core/parts/parts.go:68-80`) adds up the values of THOSE locked tokens as
  float64, in Go map order, and compares `totalBalance < transferAmount` with no
  rounding to 3dp. The locked tokens came to exactly 125.930 in decimal; their
  float sum is 125.92999999999999, so it refused. ("current balance" in the
  message is the value of the locked tokens, not the wallet: the wallet held
  125.931, which the balance API reports correctly because `GetRbtByDid`
  rounds after every addition, `core/token.go:101-103`.) All 532c413b.
- **In plain words:** computers store decimals in binary, where 0.1 or 0.7
  cannot be held exactly (like 1/3 = 0.333... in decimal), so adding them leaves
  tiny errors: `0.7 + 0.1 = 0.7999999999999999`. Rubix shows balances rounded to
  3 decimals, but this check compares the raw sum with the amount without
  rounding, so "125.930 vs 125.930" becomes "125.92999999999999 < 125.93 - not
  enough".
- **Not lab-specific - any Rubix network:** a transfer with a decimal amount can
  be refused "insufficient balance" whenever the tokens Rubix picks add up to
  exactly the amount and the binary sum lands a hair below it. A larger wallet
  does not help (only the picked tokens are counted), and it is random (the
  tokens are added in Go map order, so a retry may pass). Whole-RBT amounts are
  safe. Seen on a quorum's pledge; the sender's transfer and FT minting call the
  same function (not yet proven there).
- **Fix needed:** compare in integer thousandths, or round the sum to 3dp
  (`rubixmath.FloatPrecision`) before comparing. Check the other float sums
  that feed a comparison (`core/transaction_builder.go:94, :211`, `core/ft.go:94`
  also call `CollectRBTTokens`), and say "value of the selected tokens" in the
  message.
- **Confirm with:** quorums each holding exactly H (several fractional H), pay H;
  and senders paying fractional amounts that need an exact combination of
  parts - the sender path goes through the same check.

### R-06 Transfer reports success, receiver never gets the tokens
- **Seen:** 2026-11-02 LOAD-01 at 200 senders: tx `f3fb1ceb` accepted, recorded
  on .110:20000 and .110:20800 only, never on the receiver .111:20000 - the
  whole -1.000 RBT AUDIT-END reports. 2026-10-22: CHAIN-01, two hops to .130
  slot 6, 4 RBT. The fleet was 82.206 RBT short of the 400,000 minted before the
  2026-11-02 run. 2026-11-03 third run: FT-01 S8, an FT transfer (no RBT), tx
  `a6abf2dc2ab962c72b21607b5efe10bc03cf1dd560d746a4f5d7b3e87a4b9491`, from
  .107:20200 to `bafybmiazvuhf2...` on .136:20300, accepted after 85 s; read
  2026-11-05 (`lost_rbt.py --tx`): recorded on .107:20200, .110:20000 and
  .121:20200, never on the receiver's node - so FTs too.
- **The whole account, read 2026-11-05** (`lost_rbt.py`, fixed that day): the
  fleet holds 399,913.794 RBT and **86.206 RBT in 678 tokens, 102 transfers, was
  reported sent and is held by no node for its owner** - nothing else is
  missing, nothing is on a wrong node, nothing is untraced. It matches every
  audit since 2026-10-22 to the thousandth (33.529 before that day's full run,
  81.206 before 10-29, 82.206 before 11-02, 84.206 before the 11-03 third run):
  | When | Transfers | RBT | What | Receiver's own node holds a copy |
  |---|---|---|---|---|
  | 2026-10-22 15:57-16:01 | 55 | 33.529 | fractional sends from many givers at once to ONE receiver (.128:20100) - by its shape WALLET-PREP's fragmented wallet | yes |
  | 2026-10-22 17:55-17:57 | 4 | 4.000 | to .130 slots 3 and 6 (faucet, CHAIN-01) - .130 deaf (R-10) | no |
  | 2026-10-23 17:50-17:51 | 12 | 6.004 | fractional fan-in to one receiver (.131:20600) | yes |
  | 2026-10-26 11:33:27 | 6 | 2.673 | fractional fan-in to one receiver (.119:20300) | yes |
  | 2026-10-26 11:40-11:49 | 20 | 35.000 | to .110 slots 1/4/5/6 and .121:20100; the faucet sent 1 RBT every ~30 s to the same three .110 nodes 15 times (before the runner left deaf nodes out, 10-28) | no |
  | 2026-10-30 11:28 | 1 | 1.000 | .118:20300 -> .130:20200 | yes |
  | 2026-11-02 14:03 | 1 | 1.000 | LOAD-01 at 200 (`f3fb1ceb`) | yes |
  | 2026-11-03 13:02 | 1 | 1.000 | CHAIN-01 clean-up (`04b85c40`, R-32) | yes |
  | 2026-11-03 19:00, 19:02 | 2 | 2.000 | FAIL-01's recovery probes A (.107:20200) -> B (.108:20000) after S3 (sender killed) and S6 (receiver killed): "completed successfully", never on B's node - the run's whole AUDIT-END -2.000. Both steps PASSED: the runner judged a probe on the sender's answer only (fixed 2026-11-05) | yes |
  | 2026-11-05 16:13:19 | 1 | 1.000 | QCAP-01 S2, 200 at once: `b4a2b842` .144:20700 -> .110:20000 (a busy quorum node), token 60001_103213 | yes |
  | 2026-11-05 16:56:29 | 1 | 1.000 | FAIL-01 S3, the sender stopped right after consensus: `720b7a37` .107:20200 -> .108:20000, token 60001_12109 | yes |

  AUDIT-END 2026-11-05: 88.206 RBT in 104 transfers. Both new ones went to a
  node that had signed the token's earlier hops as a quorum, and both receivers'
  nodes recorded the transaction - from the pledge of the previous hop - so the
  runner's delivery check passed them and the steps showed only "value not
  conserved" (fixed 2026-11-05: the check now asks for the receiver DID's own
  `transaction_units` row, `db_client.received`). FT-01 S5 lost an FT transfer
  the same day (`a3ccc31b`, to .128:20600).

  Two shapes: **the receiver could not be reached** (24 transfers, 39 RBT: no
  copy on the receiver's node - this issue as described below), and **the
  receiver's node has the token, but under another DID** (78 transfers, 47.206
  RBT - R-32's signature): above all many fractional transfers arriving at one
  DID at once (73 transfers, 42.206 RBT, all on 1d9b28bb; none since the fleet
  moved to 532c413b on 10-29), and transfers right after a node restart.
- **Cause:** the sender marks its tokens Transferred, publishes, returns
  success, and only then pushes the tokens to the receiver in a goroutine
  (`core/transaction.go:537`). If `getPeer(receiver)` fails it logs "Receiver
  offline, will sync later" and stops (`:600-606`). Nothing retries it, and the
  receiver's own sync needs a fullnode (R-22). 1d9b28bb.
- **Fix needed:** deliver to the receiver before reporting success, or keep a
  persistent outbox that retries until the receiver acknowledges. The receiver
  should also be able to pull a missing transfer from the sender.
- **Confirm with:** make the receiver unreachable for a moment after consensus
  (stop it, or drop its p2p listener); check that the sender reports success
  and the receiver never records the transaction. For the second shape: 30
  fractional transfers to one DID at once, and one transfer right after the
  sender's or receiver's restart (FAIL-01 S3/S6); then read the token's rows on
  the receiver's node (which DID, which status, which transaction).

### R-07 NFT and contract chains fork on simultaneous executes
- **Seen:** 2026-10-29 NFT-01 S5 / SC-01 S3: some pairs were refused ("previous
  transaction id mismatch"), but 4 NFTs and 4 contracts accepted BOTH executes.
  The owner and the subscriber hold different 6th blocks, still different at
  the reconcile. 2026-10-21: an execute built on an older tip was accepted by a
  quorum that had not seen the newer one (fullnodes: tx `2a9ca7c3` expects
  prev `d6b6cbb7`, local latest `26c901a0`). 2026-11-03: 2 NFTs and 2 contracts
  forked at position 6; other racing pairs were refused, two of them with a raw
  SQL error from the quorum (`PledgeV2: failed to insert tokenchain records for
  incoming tx: duplicate key value violates unique constraint "tokenchain_pkey"`).
  Second run 2026-11-03: NFT `QmdwHbgjgt...` forked at position 6 (`63ba0373` vs
  `975c8b8e`); SC-01 never reached its race (its subscribers were deaf, R-10).
  Third run: 4 NFTs and 6 contracts forked at position 6; the other racing
  pairs were refused "previous transaction id mismatch", one with a raw
  `tokenchain_pkey` duplicate and one with a `tokenchain` deadlock. 2026-11-05:
  3 NFTs (`QmYxJCGAJa`, `QmStgG1vsq`, `QmV2pxxgex`) and at least 3 contracts
  forked at position 6, still forked at the reconcile.
- **Cause:** each quorum validates against its own view of the chain and syncs
  from the initiator. Two executes signed by two different quorums on the same
  tip never see each other; nothing orders executes per token.
- **Fix needed (design):** one authority that orders the executes of each
  NFT/contract (for example the owner's node, or the deploy quorum), or a check
  against the chain tip that every executor shares before signing.
- **Confirm with:** owner and subscriber execute the same NFT/contract at the
  same instant through different quorums; compare their chains afterwards.

### R-08 A former quorum cannot spend tokens it pledged
- **Seen:** every run; 2026-11-02 QCAP-01 (4), RACE-02 (2-3), LOAD-01 (3).
  Refusal: `failed to get quorum DID for token X: tokenID "X" not found in
  quorum tokens`. Confirmed on its own 2026-10-08 (`custom/unpledged_spend.py`).
  Second run 2026-11-03: QCAP-01 (5), QCAP-03b (1), COMBO-01 (4) - COMBO-01's
  sender had been QCAP-03a's quorum, and its pledge was released during
  COMBO-01, so a DID with no unpledged token at the start of a case can hold one
  minutes later. Third run: QCAP-01 (27, every sending node in the fleet
  takes part), QCAP-03b (1), and once the check's earlier branch
  (`checks.go:297`, 532c413b): `token 60001_116211 is marked unpledged but not
  found in quorum tokens of pledged transaction 515c4e04...` (LOAD-01 at 500) -
  the pledge transaction its unpledge entry points at did not pledge it.
  2026-11-05: QCAP-01 (7), RACE-02 (2: 60001_100127, 60001_105003), LOAD-01
  (60001_103217 on every rung - the same token each time).
- **Cause:** for a token whose last role is `unpledge`, the check looks the
  token up in `txnInfo.Quorums` (the current transaction's pledges) instead of
  `pledgedTxnInfo.Quorums` (`core/consensus/checks.go:309`; `:400` has the same
  mix-up and so checks nothing; 532c413b). A split bypasses it.
- **Fix needed:** pass `pledgedTxnInfo.Quorums` at both places.
- **Confirm with:** `custom/unpledged_spend.py` (exists).

### R-09 Failures leave tokens Locked
- **Seen:** 2026-11-03 FT-01: after the S5 sends failed (R-03), 1,500 FTs stayed
  Locked on .137:20100 and 1,300 on .144:20300, so S8 found those holders
  showing 200 FTs where 1,500 / 1,300 / 1,700 should be. Second run 2026-11-03:
  the same step left 9,500 FTs Locked on six holders (1,300-2,600 each). Third
  run: FAIL-01 S4 (the quorum killed on its log line) left 1.000 RBT Locked on
  the quorum (.110:20000), and the sender's refusal came after 6 s with no
  reason at all. 2026-11-05: FT-01 S5 left 5,400 FTs Locked on four senders
  (1,300-1,500 each) and S8 found exactly those missing; QCAP-01's fractional
  rung left 0.753 RBT Locked on its quorum; FAIL-01 S2 (the sender stopped on
  "Starting transaction") left 1.000 RBT Locked on the quorum (.110:20000).
  The fleet's parked Locked RBT went 24.176 -> 25.929 during the run.
  23.176 RBT parked Locked on 4 DIDs (AUDIT-START 2026-11-02). Failed
  FT sends left 500 FTs Locked on two senders (FT-01, 2026-10-29). A pledge
  that failed mid-way left 0.576 RBT Locked on its quorum (QCAP-01,
  2026-10-22). A refused FT mint left 0.8 RBT Locked (2026-10-01).
- **Cause:** RBT, FT and NFT/SC each have their own lock-release path on
  failure (`core/transaction.go:70, :77, :88`; 1d9b28bb), and the quorum's
  pledge path has its own. Some failure paths return without releasing.
- **Fix needed:** release on every failure path (a deferred release keyed to the
  transaction), plus a startup sweep that frees locks no live transaction holds.
- **Confirm with:** provoke each failure (R-01 deadlock during a pledge, a mint
  refused for low balance, a refused FT transfer); read Locked before and after.

## B. Several nodes on one machine, and the network

### R-10 After an IPFS auto-restart the node is deaf
- **Seen:** 2026-10-22/23 .130 nodes 0-7 (`p2p-check.sh`); 2026-10-29 all nine of
  .116's nodes; 2026-11-03 (second run) all nine of .135's nodes during FT-01
  S5, with the machine 2.8 GB into swap, 81% iowait, and its slot 0 restarted
  (R-03); third run, .110's nodes after its quorum's crash (R-03) - the runner
  restarted them. 2026-11-05: .121 after WALLET-01's 500-RBT transfer, and all
  nine of .134's nodes in FT-01 S5 - both restarted by the runner. The API keeps answering and the node can still send, but no
  peer can reach it until it restarts.
- **Cause:** `restartIPFS` / `reinitializeIPFS` (`core/ipfs_recovery.go`) never
  call `listenIPFSPort` again, so the new daemon has no `p2p listen
  /x/<peerID>RubixCore/1.0`. 658cb33f.
- **Fix needed:** register the p2p listener again after every IPFS restart.
- **Confirm with:** kill a node's IPFS daemon; wait for the auto-restart; check
  its listener (`p2p-check.sh`) and send it a transfer.

### R-11 One node's shutdown kills every IPFS on the machine
- **Seen:** probably .130 on 2026-10-22 (8 of 9 nodes' IPFS auto-restarted);
  FAIL-01 checks the neighbours after each kill.
- **Cause:** only startup records the IPFS PID (`core/ipfs.go:328, :350`); the
  auto-restart never updates it (`core/ipfs_recovery.go:256`). A shutdown that
  finds the stale PID falls through to `pkill -f "ipfs daemon"` and `killall -9
  ipfs` (`core/shutdown_manager.go:280-300`); its `--repo=` pattern never
  matches. 658cb33f.
- **Fix needed:** record the new PID after an auto-restart; kill only this
  node's daemon; never `killall`.
- **Confirm with:** on a 9-node machine, auto-restart one node's IPFS, then stop
  that node; check the other eight IPFS daemons.

### R-12 Peer requests swallow connection failures
- **Seen:** 2026-10-22 "getPeer failed: did not exist with the peer" from every
  consensus that had to reach .130's deaf nodes (NFT-01, SC-01, COMBO-01,
  CHAIN-01). 2026-11-05, FAIL-01 S2: the sender was stopped on "Starting
  transaction" and still answered, after 6 s, `insufficient quorum liquidity` -
  the quorum had liquidity and had locked 1 RBT for the pledge (R-09). The
  sender's pledge request died with the sender's p2p connection, came back with
  no error and no tokens, and `len(pledgeTokens) == 0` is reported as liquidity
  (`core/transaction.go:237-254`; 532c413b): a connection failure read as a
  business refusal.
- **Cause:** `SendJSONRequest` redeclares `err` inside its retry loop
  (`core/ipfsport/peer.go:205, :221`), so after three failed attempts it
  returns the never-set outer `err` (nil) with an empty response (`:257`);
  `getPeer` reads the empty answer as "DID does not exist"
  (`core/peer.go:200-203`). 1d9b28bb.
- **Fix needed:** keep and return the last error; `getPeer` should tell
  "unreachable" from "not found".
- **Confirm with:** a request to a deaf node (R-10); the error must say
  unreachable.

### R-13 Peer-port pool: leak, overlap, listener inside a neighbour's pool
- **Seen:** 2026-11-02 after a full restart, slot 3 on .116 and .137 did not
  answer at first. A node with no listener exits "failed to start core"
  (`server/server.go:76`).
- **Cause:** each node takes local ports from a pool of 5,000 starting at its
  receive port + 11 (`core/ipfsport/peer.go` `getPeerPort`). Nodes on one
  machine overlap even at the product's index step of 1 (pools 1,000 apart,
  5,000 wide). A node's listener 127.0.0.1:(receive + 10) (`core/core.go:238`)
  lies inside a neighbour's pool, so a neighbour can hold it when the node
  starts. The pool also leaks a port on every refused `p2p/forward`
  (`OpenPeerConn` returns on `resp.Error` without `releasePeerPort`), so it
  climbs on a long-running node. 658cb33f.
- **Fix needed:** release the port on every error path; take peer ports from the
  OS (port 0) or from ranges that cannot overlap; never hand out another
  node's listener port.
- **Confirm with:** start all nine nodes of a machine at once, repeatedly; count
  "failed to start core". Count pool ports in use on a node before and after
  refused forwards.

### R-14 `NodeConfigDir` never assigned
- **Cause:** declared but never set (`types/db.go`). The IPFS auto-restart
  therefore uses `IPFS_PATH=".ipfs"` relative to the working directory
  (`core/ipfs_recovery.go:247`), and `unpledge_mismatch.log` lands there too
  (`core/unpledge_v2.go:301`). Two nodes sharing a working directory would
  restart onto one IPFS repo. 658cb33f.
- **Fix needed:** set it from the `-p` profile directory.
- **Confirm with:** run a node with its working directory outside its profile
  and trigger an IPFS auto-restart; see which repo it uses.

### R-15 Quorum setup lives only in memory
- **Seen:** after every restart: 2026-10-29 the faucet quorum's restart left
  five units unfunded and FT-01 S6/S7 refused; 2026-11-02 the same restart
  (R-03). Refusal: `requestPledgeTokenHandler : Quorum is not setup`.
- **Cause:** `SetupQuorum` keeps the signing key in the `c.qc` map
  (`core/quorum_initiator.go:540-555`); nothing restores it at startup.
- **Fix needed (design):** persist the quorum setup (or re-arm configured quorum
  DIDs at startup); at least, a quorum restart should not silently stop signing.
- **Confirm with:** set up a quorum, restart its node, send through it.

### R-16 Pledge release checks only one quorum DID per node
- **Seen:** QCAP-03b, 2026-10-23 (release never came). Again 2026-11-05: Q's
  pledge not released within 120 s of the onward spend (it was released in the
  three 2026-11-03 runs) - random, as the cause says.
- **Cause:** `TxnCallBack` runs the unpledge callback for ONE quorum DID, the
  first one Go's map iteration gives (`core/fullnode.go:66-72`
  `for did := range c.qc { ...; break }`; 532c413b), and the lookup filters on
  that DID (`core/wallet/pledge.go:497`). The callback runs once per published
  transaction and never retries, so with k quorum DIDs set up on a node a pledge
  is released with probability 1/k.
- **Fix needed:** run the unpledge check for every quorum DID in `c.qc`.
- **Confirm with:** set up k quorum DIDs on one node, pledge through one, spend
  onward; repeat and count releases.

### R-17 Pubsub receive loop spins on a receive error
- **Cause:** `receivePub` (`types/pubsub.go:147-157`; 532c413b) does `continue`
  straight after `s.p.Next()` fails, with no back-off: a persistent receive error
  turns into a busy loop.
- **Fix needed:** back off (and give up / resubscribe after repeated errors).
- **Confirm with:** stop a node's IPFS daemon while the node is subscribed;
  watch the node's CPU.

## C. Validation and consensus

### R-18 NFT/SC operation from a node without the token becomes a second genesis
- **Seen:** NFT-01 2026-10-21: 3 NFTs; all four fullnodes rejected them
  ("genesis transaction is not allowed").
- **Cause:** the builder deploys any NFT/SC ID missing from the sending node's
  `tokens` table, with no previous transaction
  (`core/transaction_builder.go:369`). A quorum that has never seen the NFT
  accepts it (`core/consensus/checks.go:836` checks only its own table).
  Subscribing reports success even when the chain sync fails (`core/nft.go:150`,
  "will rely on future pubsub events"), so a subscriber can execute before it
  holds the NFT. 1d9b28bb.
- **Fix needed:** refuse an NFT/SC operation when the node does not hold the
  token's chain, unless it is an explicit deploy by the creator; make
  subscribe fail when its sync fails; quorums must refuse a genesis for an ID
  that already exists.
- **Confirm with:** subscribe on a node whose sync is made to fail, then execute
  at once.

### R-19 Fullnodes validate out of order and reach different verdicts
- **Seen:** 2026-11-02 the four fullnodes reached different verdicts on the same
  transaction in COMBO-01, QCAP-01 and WALLET-01 ("chain mismatch after sync",
  "still not found locally after sync"). 2026-10-21: tx `7eab35e3` rejected by
  .101/.103 ("marked unpledged but not found in quorum tokens of pledged
  transaction", `checks.go:297`), accepted by .102/.104. 2026-11-05: different
  verdicts on the same transaction in CHAIN-01, COMBO-01, FT-01, LOAD-01,
  NFT-01, QCAP-01 and SC-01.
- **Cause:** a fullnode validates published transactions with ~NumCPU/2 workers
  in parallel and 3 attempts ~6 s apart (`core/fullnode.go`
  `processTxnWithRetry`), so transactions on one token close together can be
  validated out of order. Several fullnodes each validate the whole stream on
  their own (one topic, `rubix_txn`), so they can disagree.
- **Measured 2026-11-06** (`custom/token_check.py`, each fullnode's chain for
  the pool's 98,883 Free tokens against the owner's): .101 same 77,303, behind
  19,550, another transaction at a position 344, ahead 1, no record 1,685;
  .102/.103/.104 about 81,600 same, 16,200 behind, 170-240 another transaction,
  8-9 ahead, 790-890 no record. So 16-20% of the tokens in circulation lack
  their latest transfer(s) on each fullnode. Not pledge bookkeeping: a pledge's
  release rewrites the pledge entry's role (8 -> 9) and adds no chain entry
  (`core/unpledge_v2.go:16-21`). A fullnode is therefore no record of current
  ownership - which matters for wallet recovery from a fullnode (R-22).
- **Fix needed:** process transactions of the same token in order (per-token
  queue), and retry an out-of-order one after its predecessor.
- **Confirm with:** a burst of transfers on one token, hop after hop; compare the
  four fullnodes' verdicts.

### R-20 Zero clock tolerance on the transaction epoch
- **Seen:** LOAD-01 2026-10-22 (3 refusals at 300 senders); smoke test
  2026-10-29. Refusal: `invalid epoch ... not after the current time`.
  2026-11-05, CHAIN-01: one refusal in each 200-hop chain ended it, at hop 135
  (.130:20600 -> .141:20400, `invalid epoch 1793877759 ... current time (epoch
  1793877758)`) and at hop 115 (.131:20800 -> .124:20800), with every machine's
  clock within 25 ms of the fleet's median (the runner leaves out any machine
  more than 50 ms off).
- **Cause:** `core/consensus/checks.go:61` rejects `Epoch > time.Now()`, so a
  sender whose clock is even slightly ahead of its quorum's is refused. The
  message also names the 2026-04-01 floor, which misleads. 1d9b28bb. The epoch
  is whole seconds (`int(time.Now().Unix())`, `core/transaction_builder.go:530`;
  532c413b), so a sender d ms ahead is refused whenever the quorum's clock is
  within d ms before a second turns over - about d/1000 of its transfers (2.5%
  at 25 ms): rare per transfer, near-certain over a few hundred hops.
- **Lab side (2026-11-05):** CHAIN-01 sends a hop refused only for this once
  more, 1.5 s later, so one such refusal no longer ends a chain; the refusal
  stays in the step's record.
- **Fix needed:** allow a small skew (seconds); say which bound was broken.
- **Confirm with:** set a sender's clock 0.1 s, 1 s and 5 s ahead of its quorum's.

### R-21 False double spend on repeated small splits
- **Seen:** once, 2026-10-06 (RBT-P-04, sender .107, quorum .139): 76 refusals
  `ValidateSplitParentsAgainstFullnode: ancestor 60001_N_8 already consumed
  (burnt by <other tx>) ... double spend` (`core/consensus/checks.go:1119`),
  one per whole token in a regular rhythm, sender DB consistent. The function's
  own comment warns about over-rejection. Not seen since.
- **Fix needed:** to be decided once confirmed.
- **Confirm with:** one wallet making many small fractional sends in a row.

### R-22 Split check and wallet recovery need Rubix's public fullnode list
- **Cause:** `ValidateSplitParentsAgainstFullnode` tier 2 and `POST
  /rubix/v1/sync` (`RecoverWalletFromFullnode`) pick a fullnode with
  `selectActiveFullnodePeer` from Rubix's public list on GitHub
  (`core/recover_wallet.go:27`). On a private network it is unreachable: the
  split check accepts unverified ("ACCEPTING (cannot verify)") and recovery
  fails. Also blocks the lab's recovery cases.
- **Fix needed:** a configurable fullnode list (config.toml or a local
  fullnodes.json).
- **Confirm with:** a split on the lab (log line) and a wallet recovery call.

### R-23 Re-splitting a previously split token collides on `tokens_pkey`
- **Cause:** the child INSERT in `core/wallet/persist_genesis_tx.go` has no
  `ON CONFLICT`. Found on an older build, not re-checked.
- **Fix needed:** handle the conflict (or never re-create an existing child).
- **Confirm with:** split a token, send the part back, split the same parent
  again.

## D. Smaller

### R-24 `GetAllQuorums()` has no `ORDER BY`
- `core/wallet/quorum.go:68`. A sender signs with its first quorum
  (`core/transaction.go:171`), but Postgres does not guarantee row order, so
  "first registered = signer" is not guaranteed. The lab works around it (one
  quorum per node list). **Fix:** `ORDER BY` the insertion order.

### R-25 `AddQuorum` returns an error on a repeat
- `core/wallet/quorum.go:19`: the DB write is `ON CONFLICT DO NOTHING`, but the
  Go wrapper reports an error. **Fix:** treat a conflict as success.

### R-26 Signatures over unhashed data fail on Go 1.25+
- **Seen:** 2026-10-14, a binary built with newer Go failed every `CreateDID`:
  `ecdsa: hash length does not match hash function`.
- `BIPSign` (`crypto/bip39.go`) calls `Sign(rand.Reader, data, crypto.SHA256)`
  with the raw message (`"test"`, `did/did.go:147`), not a 32-byte hash.
  **Fix:** hash before signing (and verifying). The lab pins Go 1.22.

### R-27 `CreateDID` has no idempotency
- `core/did.go:26` always mints a new keypair; calling it twice gives a node two
  identities with no error. A risk, not a failure. **Fix (optional):** an
  idempotency key or a "DID already exists for this seed" check.

### R-28 Quorum nodes keep Free copies of other DIDs' tokens
- **Seen:** `custom/lost_rbt.py` 2026-11-02: 1,332 tokens held Free on nodes for
  DIDs those nodes do not host, the same token on several quorum nodes (.106,
  .108, .110). The faucet quorum holds 81,651 copies of the faucet's tokens as
  Free under the faucet DID.
- **To check:** whether these rows are meant to be kept (synced chains for
  validation) and why their status is Free. They bloat the quorum's DB (R-03)
  and can mislead anything that counts tokens by DID.
- **Confirm with:** after one transfer, read the quorum's `tokens` rows for the
  sender's DID.

## F. Found 2026-11-03

### R-29 Concurrent pledges on one quorum lose their token locks
- **Seen:** QCAP-01, 400 one-RBT transfers at once through one quorum: 27
  refused `PledgeV2 failed: PledgeV2: 1 token(s) not locked by this referenceID
  "<id>": | [60001_10696]` (one token each). 398/398 went through at 400 on
  2026-11-02, and 298/298 at 300 this run. Second run 2026-11-03: 1 at 400
  (`[60001_129401]`).
- **Cause (not proven):** the quorum locks the pledge's tokens under the
  request's reference (`ReqPledgeToken` -> `LockTokensForSplit`,
  `core/consensus/consensus.go:31`); `PledgeV2` later re-reads them with
  `token_status = Locked AND lock_reference_id = <reference>` and refuses when
  one is missing (`core/pledge_v2.go:99-135`). Between the two, the token's lock
  changed. The path that changes it under load is not found yet - candidates:
  the initiator's retry (`core/transaction.go:113`, TOCTOU `maxRetries = 3`)
  asking again with a new reference, or a release path. All 532c413b.
- **Fix needed:** once found, keep a pledge's lock until PledgeV2 has run (or
  re-lock under the same reference); the refusal should say the quorum was busy.
- **Confirm with:** 300, 400, 500 one-RBT transfers through one quorum; for each
  refusal, follow its reference ID through the quorum's log.

### R-30 The pledge lock plans from `token_denom` counters and never checks what it locked
- **Seen:** QCAP-02 2026-11-03: a quorum (121:20100) holding 184.045 Free,
  asked to pledge 184.045, refused `CollectRBTTokens: insufficient balance:
  current balance 179.6, transfer amount 184.045` - it locked only 179.6 of it.
  The second run that day refused it with the same figures, so it repeats
  every time while the counter drift (R-02) stays.
- **Cause:** `lockTokensForSplitOnce` (`core/wallet/token_lock.go`, after :500;
  532c413b) plans which denominations to take from the `token_denom` COUNTERS
  (`GetTokenDenomArray` -> `SelectTokensFromTokenDenomForLocking`), then for
  each denomination locks up to that many Free rows (`LIMIT count FOR UPDATE
  SKIP LOCKED`), and never checks that it got the planned number. When a
  counter is higher than the real Free rows (R-02), or rows are skipped as
  locked, it locks less than the amount and the pledge is refused although the
  wallet holds enough. R-02 thus turns into refused valid transfers.
- **Fix needed:** plan from the Free rows themselves (or check the locked total
  and re-plan from rows when it falls short); fix R-02 so the counters stop
  drifting.
- **Confirm with:** a quorum whose counter is above its Free rows (AUDIT lists
  them), asked to pledge exactly its Free holding.

### R-31 A faucet-minted whole token refused by the allowlist, naming another minter
- **Seen:** LOAD-01 at 100 senders, 2026-11-03: `ValidateMinterAllowlist: token
  60001_103300 minter bafybmibumfrj54sor3kmtt6epchysdjqldogc4gki77h4nkhbz7hbdx5ve
  not authorised for level 60001 number 103300`. Token 60001_103300 is in the
  faucet DID's range (1-2,150,000), and the faucet DID is
  `bafybmih6fkweeuo...`, not the minter named. **Again on 2026-11-03 (third
  run, LOAD-01 at 500): the same token, the same minter named** - it repeats,
  so it belongs to that token's history, not to load. 2026-11-05: WALLET-PREP
  round 1 refused `60001_103300_1` - a part of the same token: the history
  follows the token into its parts. QCAP-01 at 200 had one more allowlist
  refusal (set aside by the ladder).
- **Cause (not known):** the quorum took a different DID as the token's minter -
  perhaps from a copy of the token's chain that does not start at the genesis
  (see R-28), or a holder recorded as the minter. Not the fixed part-token bug
  (that was part tokens; this is a whole token).
- **Lead, 2026-11-06** (`custom/token_check.py`): 8 faucet tokens in the same
  range - 60001_102974, _102975, _102977, _102978, _10303, _103026, _103028,
  _103029 - are held by pool DIDs (on .108, .112, .126, .132, .137, .141) whose
  chain starts at position 0 with a TRANSFER, not the mint, while the faucet's own
  chain has the mint at 0 and a different transaction at 1: the holder's copy
  has lost its genesis and is shifted by one. A quorum that takes the minter
  from position 0 of such a chain gets the wrong DID - the likely source of the
  refusals that name another minter. Taken out of use (code 777). A ninth,
  60001_110241 on .108:20000 (a quorum node), has a one-entry chain while
  .107:20000 has the token at position 3: a row the chain sync created for a
  token that had moved on (R-28).
- **Fix needed:** to be decided once the quorum's chain for the token is read;
  first find what writes a chain without its genesis (sync from a peer whose
  chain is partial, `applyTokenChainFromSync`, `core/sync.go`).
- **Confirm with:** read token 60001_103300's chain (height 0) on the quorum that
  refused and on the sender; send the token again through another quorum.

### R-32 A transfer recorded by the receiver's node but never credited to the receiver
- **Seen:** CHAIN-01 2026-11-03, the clean-up between the two chains: a holder
  (124:20800) sent its leftover whole token to quorum 10 (.118:20000), tx
  `04b85c403d62bd12405331212f21fa69374915a1a12645cbe6b414201d44f135`. The sender
  reported success and the receiver's node recorded the transaction
  (`lost_rbt.py --tx`: on .110:20000 - the quorum that signed it - .118:20000
  and .124:20800), but the receiver's Free balance never rose; `lost_rbt.py`
  counts no RBT as undelivered. It is the whole -1.000 RBT of this run
  (AUDIT-END, and the runner's fleet ledger "UNEXPLAINED -1.000").
- **Read on the two nodes (2026-11-03):** the sender (.124:20800) has token
  **60001_156063** with status 4 (Transferred) under this transaction - the
  token CHAIN-01 had just passed along 200 hops. The receiver (.118:20000)
  linked the transaction to a DIFFERENT token, **60001_106727**, Free under quorum
  10's own DID (`...aulyoa`) - a token it already held, so its balance did not
  change. The receiver recorded the transfer against the wrong token and never
  took 60001_156063.
- **Likely cause (not proven):** quorum 10 had validated hops of 60001_156063,
  so its node already held a copy row of it under an earlier holder's DID
  (R-28; the token is on that list on .106, .107 and .108). The receive path
  then refused or mis-matched the incoming token - see "Code path" below.
- **Re-read 2026-11-05 - why the receiver's node "records" the transfer:** a
  node that signed the token's previous hop as quorum holds a pledge for it,
  released when a later transaction spends the token (`core/callback.go`,
  `core/wallet/pledge.go:486-528`), so that node writes the new transaction
  against its OWN pledged token. The "DIFFERENT token, Free under quorum 10's
  own DID" above is most likely quorum 10's released pledge, not the receipt.
  Until 2026-11-05 the runner took such a row for delivery; it now asks for the
  receiver DID's own `transaction_units` row (`db_client.received`).
- **Seen again 2026-11-05 (twice):** QCAP-01 S2, 200 at once: `b4a2b842...`,
  token 60001_103213, .144:20700 -> .110:20000; FAIL-01 S3: `720b7a37...`, token
  60001_12109, .107:20200 -> .108:20000 (the sender was stopped right after
  consensus, so this one can be R-06 instead). Both receivers are busy quorum
  nodes holding a copy of the token under another DID (60001_103213 on .106,
  .107, .108, .110 and .121:20200; 60001_12109 on .106, .108, .110) - as were
  FAIL-01's two lost probes of 2026-11-03, also .107:20200 -> .108:20000.
- **Code path (532c413b, read 2026-11-05, not proven):** the receiver persists
  in `SendTokens` (`core/transaction.go`, after `sendTokensToReceiver`) through
  `PersistPostConsensus` (`core/wallet/post_consensus_persistence.go`), whose
  `validateTransferChainContinuity` locks the local `tokens` row and refuses
  when its `transaction_id` is not the incoming token's previous transaction,
  or its position is not latest + 1. A node that validated an earlier hop has
  such a row: the chain sync creates it, Free, under the first synced
  transaction's owner (`applyTokenChainFromSync`, `core/sync.go:535` - where
  R-28's copies come from), and the sync before the persist returns early when
  the previous transaction is already in the local chain (`core/sync.go:552`).
  If the row is stale the persist fails, `SendTokens` answers an error, and the
  sender - which has already marked the tokens Transferred - only logs
  `sendTokensToReceiver: Receiver rejected the tokens` and returns.
- **Confirm with (read-only, from .120):** the sender's log says whether the
  receiver rejected the tokens (this issue) or could not be reached (R-06; no
  line at all if the sender died first); the receiver's log shows the persist
  error with the token's ID:
  ```
  ssh rubix@192.168.1.144 "grep -n b4a2b84260eef0298be87d1061f4b7cb04d5131da9ac29dd5e87ccf309af496d /home/rubix/Desktop/rubix/rubix-lab/nodes/node7/log.txt | grep -i receiver"
  ssh rubix@192.168.1.110 "grep -n 60001_103213 /home/rubix/Desktop/rubix/rubix-lab/nodes/node0/log.txt | tail -n 20"
  ssh rubix@192.168.1.107 "grep -n 720b7a37fa2b10c89f472bd9139ae44e8142701f5b90da42e8fb6b03e127135c /home/rubix/Desktop/rubix/rubix-lab/nodes/node2/log.txt | grep -i receiver"
  ssh rubix@192.168.1.108 "grep -n 60001_12109 /home/rubix/Desktop/rubix/rubix-lab/nodes/node0/log.txt | tail -n 20"
  ```
- **Still to read:** which token the transaction itself carries (a fullnode's
  `transactions.info` for this ID), and the receiver's row for 60001_156063.
- **Fix needed:** once confirmed, the receiver must bring a copy row up to the
  incoming chain (or take it over: owner DID, status Free) before the
  continuity check, and persist exactly the tokens the transaction carries; the
  sender must not mark its tokens Transferred until the receiver has accepted
  them (R-06).
- **Confirm with:** pass a token through quorum Q, then send that token to Q
  itself; read Q's `tokens` row for it. For this run:
  `lost_rbt.py --tx 04b85c40...` and Q's row for the token.
- **How common (2026-11-05, `lost_rbt.py`):** 78 of the fleet's 102 lost
  transfers (47.206 of 86.206 RBT) have this shape - the receiver's own node
  holds a copy of the token under another DID - most of them fractional
  transfers arriving at one DID at once (see R-06's table). The copy row may
  also be an older quorum copy (R-28) and the delivery simply never came;
  reading one such row is the next step.

### R-33 A quorum refuses a token "chain mismatch after sync" with no kill involved
- **Seen:** second run 2026-11-03, one refusal each, no node of the transfer
  restarted:
  - QCAP-01, 400 at once through quorum 128:20500: `TokenChainIntigrityCheck:
    token 60001_103862 (rbt) chain mismatch after sync from <sender>: local
    latest 8e44107a != expected a2b976ab`;
  - LOAD-01, 200 at once: token 60001_10339, `local latest 8914c1bc != expected
    e4f3f326`;
  - FT-01 S5: an FT, `lab-ft-A-...dtacby_0 (ft)`, `local latest 9d704132 !=
    expected 746f0d69` (the sender, 111:20800, and the step's quorums did not
    restart).
  The first two set the "limit" QCAP-01 (300) and LOAD-01 (100) recorded.
  Third run 2026-11-03: LOAD-01 at 500 (token 60001_108940, `local latest
  3928c543 != expected 1b993472`) and three FT tokens in FT-01 S5. 2026-11-05:
  LOAD-01 at 500 again, one refusal (token 60001_103315) - 491 of 492 went
  through, and the saved limit is 300 because of it.
- **Cause (not known):** the same refusal as R-04 (`core/consensus/checks.go:944`),
  whose sync skips when the prevTxID is "already in local chain". Here no kill
  left a stale entry, so the quorum's copy came from elsewhere - perhaps a copy
  kept from an earlier hop (R-28), or a transaction that failed after the quorum
  had written it.
- **Measured across the fleet, 2026-11-06** (`custom/token_check.py`, read-only
  survey of all 98,883 Free tokens of the pool): **780 tokens (74.48 RBT, 747 of
  them part tokens) have a copy of their chain on another node that is further
  along than the owner's** - 740 by exactly ONE entry, 40 with another
  transaction at a position. Every one of those copies is on one of five nodes:
  .110:20000 (451), .116:20000 (160), .108:20000 (103), .121:20200 (43),
  .128:20500 (23) - the quorums of QCAP-01's fractional ladder and WALLET-PREP,
  and exactly the DIDs whose `token_denom` counters drift (R-02). So the second
  bullet above is the likely cause: the quorum writes the transfer it is
  validating into its copy of the sender's token chain, the transfer then fails
  (an R-01 deadlock during the pledge; a kill, R-04), and nothing removes the
  entry - one failed transfer, one entry ahead, and every later send of that
  token through that quorum is refused. All 780 taken out of use (code 444) on
  2026-11-06, so later runs no longer meet them; new ones will appear wherever
  R-01 fires, and the next AUDIT-START marks them.
- **Fix needed:** as R-04's sync part - when the quorum's latest is not the
  initiator's latest, take the initiator's chain instead of skipping; then find
  what wrote the stale entry.
- **Confirm with:** for each token, read its `tokenchain` rows on the refusing
  quorum and on the sender; find the quorum's latest transaction (which one, did
  it complete, who holds it).

## G. Found 2026-11-05

### R-34 One RBT counted on two wallets after concurrent sends on one machine
- **Seen:** RACE-02 S2, 2026-11-05: nine DIDs, one per node of .108, each sent
  1 RBT round a ring at the same instant, through quorums on .107; 7 accepted,
  2 refused (R-08, tokens 60001_100127 and 60001_105003). The nine wallets
  together rose by +1.000 RBT with nothing coming in from outside, and
  .108:20000's `token_denom` counter was left wrong (R-02). AUDIT-END the same
  run: untraced -1.000 - the same RBT, held by two DIDs at once. The audit
  then counted a token twice only when it was Free under both, so it showed as
  untraced (lab side fixed 2026-11-05: any held status, `rbt_ledger` "held
  twice").
- **Cause:** not known. Candidates: a send whose token reached its receiver
  while the sender's row stayed Free or Locked (a refusal or retry after the
  receiver had persisted - TOCTOU `maxRetries = 3`, `core/transaction.go:113`),
  or the release of a pledge on a node that also holds the token.
- **Fix needed:** to be decided once the token is known.
- **Confirm with:** `controller/test-plan/custom/lost_rbt.py` (from 2026-11-05
  it lists every token held twice, with both holders and statuses) names the
  token; then read its `tokens` and `tokenchain` rows on both nodes and the
  transactions that last moved it. A repeat: the same ring of nine on one
  machine, compared wallet by wallet before and after (the step now records
  which wallet moved by other than its own transfers, `value_off`).

## E. Not reachable on the lab, or fixed

- **Local RBT token IDs collide across nodes** (`generate_local_rbt`,
  `core/token.go`; `TokenChainIntigrityCheck` looks tokens up by bare ID).
  Not reachable here: the lab never mints RBT outside the faucet's range.
- **Part tokens spent by a second holder failed the minter allowlist** - FIXED
  in 532c413b (f890aa01, 257a9e9d, 4601dd04).
