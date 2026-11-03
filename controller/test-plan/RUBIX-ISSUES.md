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
| R-01 | Concurrent pledges/splits on one DID deadlock on `token_denom` | CONFIRMED | 2026-11-03 |
| R-02 | `token_denom` counter left wrong after a failed pledge | CONFIRMED | 2026-11-03 |
| R-03 | Rubix nodes grow with large transactions until the kernel kills them | SUSPECTED cause | 2026-11-03 |
| R-04 | A quorum killed mid-transfer leaves the sender's token stuck | CONFIRMED (twice) | 2026-11-03 |
| R-05 | A quorum holding exactly the value is refused (float compare) | CONFIRMED (twice) | 2026-11-03 |
| R-06 | Transfer reports success, receiver never gets the tokens | CONFIRMED | 2026-11-02 |
| R-07 | NFT and contract chains fork on simultaneous executes | CONFIRMED | 2026-11-03 |
| R-08 | A former quorum cannot spend tokens it pledged | CONFIRMED | 2026-11-03 |
| R-09 | Failures leave tokens Locked | CONFIRMED | 2026-11-03 |
| R-10 | After an IPFS auto-restart the node is deaf | CONFIRMED | 2026-10-29 |
| R-11 | One node's shutdown kills every IPFS on the machine | SUSPECTED | 2026-10-22 |
| R-12 | Peer requests swallow connection failures | CONFIRMED | 2026-10-22 |
| R-13 | Peer-port pool: leak, overlap, listener inside a neighbour's pool | CONFIRMED | 2026-11-02 |
| R-14 | `NodeConfigDir` never assigned | CODE | - |
| R-15 | Quorum setup lives only in memory | CONFIRMED | 2026-11-02 |
| R-16 | Pledge release checks only one quorum DID per node | CONFIRMED | 2026-10-23 |
| R-17 | Pubsub receive loop spins on a receive error | CODE | - |
| R-18 | NFT/SC operation from a node without the token becomes a second genesis | CONFIRMED | 2026-10-21 |
| R-19 | Fullnodes validate out of order and reach different verdicts | CONFIRMED | 2026-11-02 |
| R-20 | Zero clock tolerance on the transaction epoch | CONFIRMED | 2026-10-22 |
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
| R-31 | A faucet-minted whole token refused by the allowlist, naming another minter | SUSPECTED | 2026-11-03 |
| R-32 | A transfer recorded by the receiver's node but never credited to the receiver | SUSPECTED | 2026-11-03 |

---

## A. Failing the suite on the current build

### R-01 Concurrent pledges/splits on one DID deadlock on `token_denom`
- **Seen:** every run since 2026-10-21. On 2026-11-02: QCAP-01 fractional rung
  of 20 at once (8 refusals), WALLET-PREP (8), COMBO-01 (2). Refusals:
  `PersistPreConsensus: failed to update token_denom ... deadlock detected
  (SQLSTATE 40P01)` and `PledgeV2: decrement token_denom ...`. On 2026-11-03:
  21 refusals - QCAP-01 (13), WALLET-PREP (3), FT-01 (3), COMBO-01 (2).
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
  178,620. First seen 2026-10-21 (0.1: counter 111, rows 107).
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
- **Fix needed:** a bounded worker pool (or a channel with fixed workers) for
  pubsub callbacks; a context timeout on the callback's DB work; a cheap
  prefilter before the unpledge queries. A pprof endpoint behind a flag would
  make the next case of this diagnosable.
- **Confirm with:** a quorum node with a large wallet, a steady stream of
  published transactions (some with ~2,000 tokens); read its RSS and goroutine
  count over time. Proof: `kill -QUIT` on its MainPID while it is big - Go
  prints every goroutine's stack to the journal.

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
  nodes made an ordinary transfer before any kill.
- **Cause:** the quorum writes the in-flight transaction into its token chain
  before consensus completes, and nothing undoes it when the transaction never
  completes. Where it writes it has not been located yet.
- **Fix needed:** write the quorum's chain entry only after consensus completes;
  or, on startup and in the sync, drop a latest entry whose transaction the
  initiator does not have. The sync must not skip when its own latest entry is
  not in the initiator's chain.
- **Confirm with:** kill the quorum on its "took the request" log line; then
  send the same token through the same quorum, and through a different one.

### R-05 A quorum holding exactly the value is refused (float compare)
- **Seen:** QCAP-02, 2026-11-02: `CollectRBTTokens: insufficient balance:
  current balance 125.92999999999999, transfer amount 125.93`. Again, word for
  word, on 2026-11-03.
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
  2026-11-02 run.
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
  and the receiver never records the transaction.

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
- **Cause:** for a token whose last role is `unpledge`, the check looks the
  token up in `txnInfo.Quorums` (the current transaction's pledges) instead of
  `pledgedTxnInfo.Quorums` (`core/consensus/checks.go:309`; `:400` has the same
  mix-up and so checks nothing; 532c413b). A split bypasses it.
- **Fix needed:** pass `pledgedTxnInfo.Quorums` at both places.
- **Confirm with:** `custom/unpledged_spend.py` (exists).

### R-09 Failures leave tokens Locked
- **Seen:** 2026-11-03 FT-01: after the S5 sends failed (R-03), 1,500 FTs stayed
  Locked on .137:20100 and 1,300 on .144:20300, so S8 found those holders
  showing 200 FTs where 1,500 / 1,300 / 1,700 should be.
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
  .116's nodes. The API keeps answering and the node can still send, but no
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
  CHAIN-01).
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
- **Seen:** QCAP-03b, 2026-10-23 (release never came).
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
  transaction", `checks.go:297`), accepted by .102/.104.
- **Cause:** a fullnode validates published transactions with ~NumCPU/2 workers
  in parallel and 3 attempts ~6 s apart (`core/fullnode.go`
  `processTxnWithRetry`), so transactions on one token close together can be
  validated out of order. Several fullnodes each validate the whole stream on
  their own (one topic, `rubix_txn`), so they can disagree.
- **Fix needed:** process transactions of the same token in order (per-token
  queue), and retry an out-of-order one after its predecessor.
- **Confirm with:** a burst of transfers on one token, hop after hop; compare the
  four fullnodes' verdicts.

### R-20 Zero clock tolerance on the transaction epoch
- **Seen:** LOAD-01 2026-10-22 (3 refusals at 300 senders); smoke test
  2026-10-29. Refusal: `invalid epoch ... not after the current time`.
- **Cause:** `core/consensus/checks.go:61` rejects `Epoch > time.Now()`, so a
  sender whose clock is even slightly ahead of its quorum's is refused. The
  message also names the 2026-04-01 floor, which misleads. 1d9b28bb.
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
  2026-11-02, and 298/298 at 300 this run.
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
  `bafybmih6fkweeuo...`, not the minter named.
- **Cause (not known):** the quorum took a different DID as the token's minter -
  perhaps from a copy of the token's chain that does not start at the genesis
  (see R-28), or a holder recorded as the minter. Not the fixed part-token bug
  (that was part tokens; this is a whole token).
- **Fix needed:** to be decided once the quorum's chain for the token is read.
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
  then mis-matched the incoming token.
- **Still to read:** which token the transaction itself carries (a fullnode's
  `transactions.info` for this ID), and the receiver's row for 60001_156063.
- **Fix needed:** once confirmed, the receiver must persist exactly the tokens
  the transaction carries and take over any copy row for them (owner DID,
  status Free).
- **Confirm with:** pass a token through quorum Q, then send that token to Q
  itself; read Q's `tokens` row for it. For this run:
  `lost_rbt.py --tx 04b85c40...` and Q's row for the token.

## E. Not reachable on the lab, or fixed

- **Local RBT token IDs collide across nodes** (`generate_local_rbt`,
  `core/token.go`; `TokenChainIntigrityCheck` looks tokens up by bare ID).
  Not reachable here: the lab never mints RBT outside the faucet's range.
- **Part tokens spent by a second holder failed the minter allowlist** - FIXED
  in 532c413b (f890aa01, 257a9e9d, 4601dd04).
