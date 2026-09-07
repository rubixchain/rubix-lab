# Rubix node database — schema reference

Captured from the live fleet (`192.168.1.104`, 2026-09-07). **Write DB cases
against this file, not from memory.** Every column name here was read off a
running node; guessing one wrong is how a check silently measures the wrong
thing.

Connection: `host=<node> port=5433 dbname=rubix user=rubix password=rubixpass`

```bash
export PGPASSWORD=rubixpass
export PGOPTIONS='-c default_transaction_read_only=on'   # makes psql refuse writes
psql -h 192.168.1.104 -p 5433 -U rubix -d rubix
```

---

## Two traps that produce plausible-looking wrong answers

**1. One `tokens` table holds every asset type.** Filter on `token_type` or RBT
figures silently include NFT, FT and SC rows.

| id | name |
|---|---|
| 1 | `rbt` |
| 2 | `nft` |
| 3 | `ft` |
| 4 | `smart_contract` |

Measured on `.104`: free rows totalled **2016.000**, of which **5.000 was FT**
and only **2011.000 was RBT**. An unfiltered "RBT balance" is wrong by exactly
the FT holding — and looks entirely reasonable.

**2. A node's `tokens` table contains rows for MORE THAN ONE DID.** `.104`
carries six, its own being `bafybmihyf3ko5…` with 2288 rows. Always filter on
`did`. Comparing a per-DID counter against an unfiltered token count produces a
huge fake discrepancy — this cost us a false "product bug" before it was caught.

---

## Token status values

Verified against `constants/constants.go` (an `iota` run, so positional) and
cross-checked against live data.

| id | name | id | name |
|---|---|---|---|
| 0 | Free | 8 | Burnt |
| 1 | Locked | 9 | BurntForFT |
| 2 | Generated | 10 | Deployed |
| 3 | Fetched | 11 | Executed |
| 4 | Transferred | 12 | PinnedAsService |
| 5 | Committed | 13 | Orphaned |
| 6 | Pledged | 14 | ChainSyncIssue |
| 7 | QuorumPledged | 15 | BeingDoubleSpent |
| | | 99 | Seed |

---

## Tables the test cases use

### `tokens` — the one that matters most
```
token_id text, parent_token_id text, token_value numeric, token_status smallint,
did text, transaction_id text, token_state_hash text, token_type smallint,
latest_position bigint, latest_role smallint, lock_reference_id text,
created_at timestamptz, updated_at timestamptz
```
`parent_token_id` links a part token to the whole one it was split from.
`lock_reference_id` ties a Locked token to the operation holding it — useful for
telling a live lock from a stranded one.

### `token_denom` — the denomination counter
```
id bigint, did text, denom numeric, count bigint,
created_at timestamptz, updated_at timestamptz
```
Per `(did, denom)`. Counts **Free RBT only** (`core/wallet/recovery.go:663`),
and only RBT contributes (`core/wallet/post_consensus_persistence.go:702`).
Consumed by `lockTokensForSplitOnce` (`core/wallet/token_lock.go:505`) to decide
which denominations to select before reading `tokens`.

⚠️ **Rows can exist with `count = 0`.** `.104` carries `0.100`, `0.500` and
`1.000` all at zero. Treat a missing row and a zero row as equivalent when
comparing against reality.

### `transactions`
```
id text, info json, signature jsonb,
created_at timestamptz, updated_at timestamptz
```
`info` is JSON — participants come out via `info->>'initiator'` and
`info->>'owner'`. Read them from the row rather than assuming which hosts a test
used.

### `tokenchain` / `tokenchain_index`
```
tokenchain:       id integer, token_id text, transaction_id text,
                  previous_transaction_id text, role smallint, position bigint, ...
tokenchain_index: token_id text, index ARRAY, ...
```

### `unpledge_sequence_info`
```
tx_id text, pledge_tokens ARRAY, epoch integer, quorum_did text,
transaction_tokens ARRAY, created_at timestamptz, updated_at timestamptz
```
A row whose `tx_id` has no matching `transactions.id` is a pledge that can never
be released.

### FT
```
fts:       id integer, ft_name text, creator_did text, ft_count integer, ...
ft_tokens: token_id text, ft_id integer, ...
```
An FT series is keyed on `(ft_name, creator_did)`; a re-mint accumulates
`ft_count` rather than creating a second series (`core/ft.go:196`).

### Smart contracts
```
call_back_urls: smart_contract_hash text, callback_url text, ...
```

### Other
```
dids            did text, peer_id text, local boolean, algo_id smallint
quorum_manager  did text, ...
requests        id text, transaction_id text, status smallint, ...
token_state_hashes  did, token_state_hash, pledged_token, transaction_id, ...
transaction_units   transaction_id, did, execution_role, status, ...
ipfs_providers      cid, peer_id, did, role, operation, status, ...
token_recovery      transaction_id, recovered_at, recovered_by, token_count, ...
local_test_token_info  attribute text, value integer, ...
```
Lookups: `token_type`, `token_role`, `did_algo` — each `id / name / is_active`.

### `fullnode_*` — ignore on pool nodes
`fullnode_rbt`, `fullnode_nft`, `fullnode_ft`, `fullnode_smart_contract`,
`fullnode_tokenchain`, `fullnode_tokenchain_index`, `fullnode_transactions`,
`fullnode_invalid_transactions`.

These mirror the observer view kept by the fullnode (`.101`). They exist on
every node but are only populated on the fullnode. **Querying them on a pool
node returns empty, which reads as "no data" rather than "wrong table"** — so a
case that uses them by mistake fails in a confusing way. Pool-node cases want
`tokens` / `transactions` / `tokenchain`.

28 tables in total.

---

## Recipes

```sql
-- Free RBT for one DID, by denomination (what token_denom SHOULD match)
SELECT token_value, COUNT(*) FROM tokens
 WHERE did = :did AND token_status = 0 AND token_type = 1
 GROUP BY token_value ORDER BY token_value;

-- The counter's own view
SELECT denom, count FROM token_denom WHERE did = :did ORDER BY denom;

-- Full RBT picture for one DID
SELECT token_status, COUNT(*), SUM(token_value) FROM tokens
 WHERE did = :did AND token_type = 1
 GROUP BY token_status ORDER BY token_status;

-- Collateral committed by contract deploys
SELECT COALESCE(SUM(token_value),0) FROM tokens
 WHERE did = :did AND token_status = 5 AND token_type = 1;

-- What a quorum currently has pledged
SELECT COALESCE(SUM(token_value),0) FROM tokens
 WHERE did = :did AND token_status IN (6,7) AND token_type = 1;

-- Pledges that can never be released
SELECT u.tx_id FROM unpledge_sequence_info u
 WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = u.tx_id);

-- Did both ends store this transaction? (run on each participant)
SELECT info->>'initiator', info->>'owner' FROM transactions WHERE id = :tx_id;
```

---

## Open questions, to settle during real runs

**`token_denom` may be under-counting on `.104`.** Its counter lists
`1.000 → 0` while the node holds ~2007 free 1.000 RBT rows. The per-DID split
has not been checked yet, so this is **not confirmed** — the earlier fleet-wide
comparison was invalid because it mixed DIDs and asset types. `GEN-IN-08`
answers it properly. If it is real, it is a `token_denom` maintenance bug in the
product, and it would explain later transfers failing with
`lockSelectedTokens: no tokens provided`.

**~2022 RBT sits in status 6 (Pledged) on `.104`**, a pool host. Either stale
pledges that were never released, or the node has served as a quorum. `GEN-V-04`
covers it.

**Postgres versions differ across the fleet** — `.104` is 18.4, `.107` is 18.6.
To be levelled to one stable version. Note that `pg_dump` from Ubuntu 24.04
(client 16.x) **refuses** to dump an 18.x server, so schema capture must go
through `information_schema` queries, as this document did.
