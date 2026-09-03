# Master test cases

`master-test-cases.xlsx` is the single source of truth for test case data —
250 rows across `RBT` / `FT` / `NFT` / `SC` / `XA` (cross-asset) / `GEN`
(general, quorum/chain/resilience/reconciliation-level cases not tied to one
asset). Columns: `Test ID | Asset | Test Case | Expected Result | Other
Checks In Same Case | Notes`.

The asset-specific folders (`../rbt/`, `../ft/`, `../nft/`, `../sc/`,
`../cross-asset/`, `../general/`) hold scripts only, once those exist — each
filters this file by its `Asset` column rather than keeping its own copy of
the case data. Case data lives here and only here; don't duplicate rows into
a per-asset file, or the two will drift out of sync.

`../rubix-lab-test-catalogue.csv` (259 rows, one folder up) is kept as the
original high-level reference and is untouched — this file is what actually
gets used to build and run the test scripts.

## Where this came from

Built from a pasted test-case document (Aug 2026), reconciled against the
old CSV. Notes worth knowing if you're editing this file later:

- The pasted NFT and Cross-Asset sections were each duplicated by mistake in
  the source paste — used once each here.
- RBT and FT were short a handful of distinct scenarios the old CSV had
  (e.g. self-transfer, resend-right-after-receiving, rounding-boundary
  cases, deep multi-level splits, NODE-KILL under parallel load). Pulled
  those in — tagged `[gap-fill from <old Test ID>]` in the Notes column so
  they're easy to find.
- Several rows had a blank Expected Result in the pasted doc where the old
  CSV had one filled in (mostly the NODE-KILL and pledge-timing cases) —
  filled those from the CSV, tagged `[expected result filled from old
  catalogue <Test ID>]`.
- One real correctness fix, verified against `CLAUDE.md`'s code-verified
  facts: "Transfer an NFT to its current owner" had a blank Expected Result
  in the pasted doc. NFT transfers must change ownership
  (`core/consensus/checks.go:403`), so this is **Rejected**, not blank —
  tagged `[corrected]`.
- Deliberately did *not* mechanically pad every ladder-rung variant the old
  CSV had (e.g. its finer-grained 5/25/50/100/200-concurrent-sender series)
  where the pasted set already covers the same concept at a different
  granularity (e.g. 2/5/10/20-node sharing) — that would bloat the master
  with near-duplicates rather than add real coverage.
