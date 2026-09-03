# Rubix integration test catalogue

Every check the integration suite verifies, by subsystem.

A **check** is the unit, not a "test case": a phase performs operations, then
asserts many named checks against the resulting state. One operation
legitimately verifies several things. Check names are stable, so results
compare directly between runs.

Verification asserts the **end state**, not HTTP 200 — chain length, balance,
cross-node sync, callback delivery, DB persistence. A transaction can fail
(e.g. budget starvation) while every check still passes, because checks assert
the end state of what succeeded.

`PASS` / `FAIL` / `WARN`, where WARN is non-blocking.

## Suites

```
--run-all-tests   --nft-only   --sc-only   --ft-only
--ft-parts-tests / --ft-parts-only    --sc-collateral-tests    --negative-tests
```

## Phase order

Strictly sequential, with a settle window between each:

```
MINTING → SHUTTLE → NFT → SMART_CONTRACT → BUNDLED_TX → FT → ALL_IN_ONE
        → INTRA_NODE → NEGATIVE → FINALISE
```

---

## 1. RBT — generation & transfer

| Operation | Detail |
|---|---|
| Minting | Pre-mint localnet RBT on nodeA and quorum via `/rubix/v1/tokens/generate_local_rbt` |
| Shuttle | Alternating A→B / B→A transfers, sequential **and** parallel phases |

| Check | Asserts |
|---|---|
| `TX_LIST_NODE_A` / `TX_LIST_NODE_B` | Each node records the expected transactions |

RBT balances are additionally asserted inside the bundled, all-in-one and
intra-node checks.

## 2. NFT — create, deploy, mint children, execute, transfer

Creates N NFTs, deploys them, mints child NFTs under a parent, self-executes,
cross-node subscribes + executes, and transfers ownership.

**Child-mint mechanics:** one NFT token entry **per child** in
`POST /rubix/v1/tx`, each carrying `parentNFTId`. `nftId` is IGNORED when
`parentNFTId` is set, and there is **no** `numberOfChildren` field — N children
means N entries. Response returns `result.mintedNFTChildren`
(`[{parentNFTId, childNFTId}]`) and `result.transactionID`.

| Check | Asserts |
|---|---|
| `NFT_LIST_NODE_A` / `_NODE_B` | Node lists the NFTs it should own |
| `NFT_CHAIN_<id>_NODE_A` / `_NODE_B` | Chain has expected length (grows per deploy/execute/transfer) |
| `NFT_CHAIN_SYNC_<id>` | After cross-node execute, executor's chain length equals the owner's |
| `NFT_MINT_CHILDREN` | Child-mint tx succeeded and minted the requested count |
| `NFT_CHILDREN_MINTED_<parent>` | Parent's `nfts/{id}/children` lists every minted child |
| `NFT_PARENT_OF_<child>` | Each child's `nfts/{id}/parent` points back |
| `NFT_CHILDREN_QUERY` / `NFT_PARENT_QUERY` | Those endpoints respond for a deployed NFT |
| `NFT_BALANCE_NODE_A` / `_NODE_B` | Ownership counts correct |

## 3. Smart Contract — deploy, execute, callback

Deploys N `.wasm` contracts, self-executes, cross-node subscribes + executes,
and verifies the registered callback URL is actually invoked.

| Check | Asserts |
|---|---|
| `SC_LIST_NODE_A` / `_NODE_B` | Node lists deployed contracts |
| `SC_CHAIN_<id>_NODE_A` / `_NODE_B` | Chain has expected length |
| `SC_CHAIN_SYNC_<id>` | Chain matches across nodes |
| `SC_TX_LIST_NODE_A` / `_NODE_B` | SC transactions recorded per node |
| `SC_REGISTER_CALLBACK_<id>` | Callback URL registration succeeded |
| `SC_CALLBACK_REGISTER_<id>` | Registered in the node's `call_back_urls` |
| `SC_CALLBACK_TRIGGER_EXECUTE_<id>` | Executing triggers the callback |
| `SC_CALLBACK_DELIVERED_<id>` | Node actually POSTed to the receiver |
| `SC_CALLBACK_INITIATOR_<id>` | Payload carries the correct initiator |
| `SC_CALLBACK_DELIVERY` | End-to-end delivery (skipped if no SC deployed) |

## 4. FT — mint & transfer

Mints N FT batches (burning RBT), then transfers a slice of each batch A ↔ B.

| Check | Asserts |
|---|---|
| `FT_LIST_NODE_A` / `_NODE_B` | Node lists its FT series |
| `FT_BALANCE_NODE_A` / `_NODE_B` | Per-DID FT counts correct |
| `FT_TX_LIST_NODE_A` / `_NODE_B` | Expected FT transactions recorded |

## 5. Bundled transaction (RBT + NFT + SC in one `/tx`)

A single `/rubix/v1/tx` carrying an RBT transfer + NFT execution + SC execution
atomically, alternating A→B / B→A each round.

| Check | Asserts |
|---|---|
| `BUNDLED_RBT_BALANCE_NODE_A` / `_NODE_B` | RBT moved correctly |
| `BUNDLED_NFT_CHAIN_NODE_A` / `_NODE_B` | NFT chain advanced on both |
| `BUNDLED_SC_CHAIN_NODE_A` / `_NODE_B` | SC chain advanced on both |
| `BUNDLED_SC_CHAIN_SYNC` | SC chain consistent across nodes |
| `BUNDLED_TX_LIST_NODE_A` / `_NODE_B` | Bundled tx recorded |

## 6. All-in-one (RBT + every FT + every NFT + every SC in one `/tx`)

One `/tx` per round carrying RBT + every minted FT batch + every deployed NFT +
every deployed SC. Direction alternates A↔B per round.

| Check | Asserts |
|---|---|
| `ALLINONE_RBT_BALANCE_NODE_A` / `_NODE_B` | RBT moved correctly |
| `ALLINONE_FT_BALANCES_NODE_A` / `_NODE_B` | Every FT batch updated |
| `ALLINONE_NFT_CHAIN_NODE_A_<id>` / `_NODE_B_<id>` | Each NFT chain advanced |
| `ALLINONE_SC_CHAIN_NODE_A_<id>` / `_NODE_B_<id>` | Each SC chain advanced |
| `ALLINONE_SC_CHAIN_SYNC_<id>` | Each SC chain consistent across nodes |
| `ALLINONE_TX_LIST_NODE_A` / `_NODE_B` | All-in-one tx recorded |

## 7. Intra-node (two DIDs on one node)

Creates a secondary DID on nodeA and exercises the full asset matrix against
the primary DID inside one node's wallet boundary — RBT ping-pong, FT
back-and-forth, plus an NFT and SC deployed + self-executed by the secondary.

| Check | Asserts |
|---|---|
| `intra_node.secondary_did_created` | Secondary DID created |
| `intra_node.rbt_balances` | RBT balances readable for both DIDs |
| `intra_node.nft_chain` | Secondary DID's NFT chain advanced |
| `intra_node.sc_chain` | Secondary DID's SC chain advanced |
| `intra_node.ft_balance[<ft_name>]` | Secondary DID holds the funded FTs |

## 8. Negative / failure-path

Inverted assertion: the operation must be **rejected for the right reason**
**and** leave observable state **unchanged**. A rejection for the wrong reason,
or any state change, is a FAIL.

| Check | Scenario | Asserts |
|---|---|---|
| `NEG_RBT_ZERO_BALANCE` | Transfer from a DID with no balance | Rejected; balance unchanged |
| `NEG_RBT_INSUFFICIENT` | Transfer more RBT than owned | Rejected (insufficient); balance unchanged |
| `NEG_RBT_DECIMAL_PLACES` | Transfer `0.00000009` (> 3 dp) | Rejected by precision rule; balance unchanged |
| `NEG_FT_OVER_TRANSFER` | Transfer 1,000,000 FTs not held | Rejected (FT lock fails); FT balance unchanged |
| `NEG_INVALID_RECEIVER_DID` | Transfer to malformed / unknown DID | Rejected |
| `NEG_NON_POSITIVE_AMOUNT` | Transfer a negative amount | Rejected; balance unchanged |

## 9. FT from part RBTs

Minting an FT burns whole RBT — but the tokens burnt need not be whole. A
wallet holding only fractional RBT must mint just the same, burning several
**part** tokens per batch. Funds a DID with sub-1.0 transfers (so it holds
parts and no 1.000 token), mints twice, and audits what the burns did.

The ordinary FT suite cannot catch this: it mints from a DID with hundreds of
whole tokens, so one whole parent is burnt per batch, and it asserts FT counts
— never `token_denom`.

**Why `token_denom` matters:** `lockTokensForSplitOnce` picks *which
denominations* to select from `token_denom`, then reads matching rows from
`tokens`. A counter still advertising burnt tokens makes a later selection ask
for rows that are no longer Free, and the operation dies with
`lockSelectedTokens: no tokens provided` — attributed to whatever transaction
ran next, not to the mint that corrupted the counter.

| Check | Asserts |
|---|---|
| `FTPARTS_PRECONDITION` | SKIP-only: node lacks balance to fund a parts wallet |
| `FTPARTS_SETUP` | Parts wallet funded |
| `FTPARTS_WALLET_PARTS_ONLY` | Funded DID holds the full amount as fractional denominations and **no whole token** |
| `FTPARTS_MINT_FROM_PARTS` | FT mint backed by several part tokens succeeds and burnt more than one RBT row |
| `FTPARTS_PARTS_BURNT` | Free balance falls by exactly the RBT minted, and that value is recorded as `BurntForFT` — no part silently destroyed |
| `FTPARTS_FT_BALANCE` | Minting DID holds the FTs, per the API **and** independently per the `tokens` table |
| `FTPARTS_DENOM_CONSISTENT` | `token_denom` matches the real count of Free RBT rows at every denomination |
| `FTPARTS_NO_ZERO_DENOM` | No phantom `denom=0` row for the minting DID |
| `FTPARTS_REPEAT_MINT` | A second mint from the same parts wallet succeeds and costs exactly the RBT requested |
| `FTPARTS_SPEND_AFTER_MINT` | Parts left over after the burns are still spendable |

## 10. SC deploy collateral

Deploying a contract with a value locks RBT as collateral: backing tokens are
marked Committed (terminal) and the remainder must come back as change.

`LockTokensForSplit` selects whole denominations, so backing a 0.001 commitment
picks a whole 1.000 token. If that token is committed as-is, the other 0.999 is
silently destroyed — a 0.001 contract costs a full RBT. Only visible when
`sc_value` < denomination, so every case here uses a **fractional** value. The
ordinary SC suite deploys at exactly 1.0 — the one value where committing a
whole token is correct — and never asserts balance.

| Check | Asserts |
|---|---|
| `SCCOL_PRECONDITION` | SKIP-only: insufficient balance to run the suite |
| `SCCOL_DEPLOY_VALUE` | Fractional-value deploy succeeds |
| `SCCOL_BALANCE_DELTA` | Deployer's free balance drops by **exactly** `sc_value`, not a whole denomination |
| `SCCOL_COMMITTED_SUM` | Committed RBT totals `sc_value`, not a whole denomination |
| `SCCOL_DENOM_CONSISTENT` | `token_denom` agrees with real Free rows at the collateral denomination |
| `SCCOL_EXECUTE_VALUE` | SC execute pledges the contract's value |
| `SCCOL_REPEATED_DEPLOYS` | Three fractional deploys in a row all succeed |
| `SCCOL_REPEATED_COST` | Repeated deploys cost exactly what was asked |
| `SCCOL_FT_DENOM_DRIFT` | No `token_denom` drift at denominations *outside* the SC collateral one. Kept separate from `SCCOL_DENOM_CONSISTENT` so an SC regression is never mistaken for an FT one. **Expect PASS** once the FT paths decrement `token_denom` (they now do); it was red before that fix, so treat a red result as a real regression, not the old known failure |

## 11. Transaction persistence (runs last)

Runs after every subsystem, so all writes have committed across nodes.

| Check | Asserts |
|---|---|
| `TX_PERSISTED_BOTH_NODES` | Every recorded-SUCCESS txn exists in the `transactions` table on **every participating node** (sender AND receiver). Missing on any participant → FAIL |

Participating nodes are derived from the row's own `info->>'initiator'` and
`info->>'owner'`, not guessed — so cross-node transfers require both ends and
single-node operations require one.

Legitimately skipped, never a FAIL:

- `FT_MINT` — `POST /rubix/v1/fts/mint` returns `result: null` and surfaces no
  `transactionID`, so there is nothing to look up. The mint still creates the
  FT genesis transaction in the DB.
- `INTRA_NODE_SETUP` — DID creation, not a token transaction.

---

## Response shapes worth pinning

- `GET /dids/{did}/balances/ft` → `result[]` of `{name, creator, value, count}`.
  Matching on `ft_name`/`FTName` silently counts 0 — those keys do not exist.
- `GET /dids/{did}/balances/rbt` → `{balance, pledged, locked}`. `balance` is
  the **free** portion only; locked and pledged are separate.
- Mint index ranges must be spaced far apart per node so token-number ranges
  can never overlap across nodes.
