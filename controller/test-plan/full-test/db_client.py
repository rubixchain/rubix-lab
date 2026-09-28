#!/usr/bin/env python3
"""
db_client.py - read a lab node's Postgres directly.

Companion to rubix_client.py. That one talks to the node's HTTP API on :20000;
this one reads the database behind it on :5433. Not a replacement - most cases
should use the API, because that is what a real user sees. This exists for the
handful of things the API genuinely does not expose:

  * token_denom     - the per-denomination counter that decides which tokens a
                      later selection asks for. Not in any API response.
  * per-token status - Free / Locked / Committed / BurntForFT. The API reports
                      totals, so it cannot tell "committed correctly" from
                      "destroyed".
  * transaction rows - whether a given node actually stored a transaction, as
                      opposed to merely agreeing about a balance.

Those three are where the product's own suite found real bugs, which is why
they are worth the extra dependency.

REQUIREMENT
    sudo apt install -y python3-psycopg2      # on the CONTROLLER only

    psycopg2 is the standard PostgreSQL driver for Python - the same role
    `requests` plays for HTTP. Nothing Rubix-specific.

    Nothing is installed on the nodes. Each node already runs Postgres in
    Docker with the port published (docker run ... -p 5433:5432), so the
    controller connects over the lab network.

    If it is missing, every function here raises DBUnavailable with an
    actionable message, and cases turn that into an honest SKIP rather than a
    false pass. Import this module freely - importing never fails.

CONNECTION
    Defaults match what setup.sh and wipe-node-db.sh actually create:
        port 5433, database "rubix", user "rubix", password "rubixpass"

SAFETY - READ-ONLY IS ENFORCED, NOT PROMISED
    Every connection opened by query() is put into
        default_transaction_read_only = on
    which makes POSTGRES ITSELF reject any INSERT / UPDATE / DELETE / DDL on
    that session with "cannot execute ... in a read-only transaction". This is
    not a convention that a future edit could quietly break: a write would have
    to first switch connection function, and that function is named so the
    change is obvious in review.

    The catalogue's DB-SEED cases (RBT-DB-*, FT-DB-*, NFT-DB-*, SC-DB-*,
    CRS-DB-*) do need to corrupt data deliberately. They must call
    writable_query() explicitly, which:
      * opens a separate, clearly-named writable connection,
      * refuses to run unless the caller passes i_understand_this_writes=True,
      * logs every statement it executes to stderr so a run's output shows
        exactly what was changed.
    Nothing else in this file can write, and no read path can be turned into a
    write path by accident.

    For MANUAL psql sessions, get the same protection with:
        export PGOPTIONS='-c default_transaction_read_only=on'
    Every psql started from that shell then refuses writes too. Unset it only
    when deliberately running a DB-SEED case by hand.

TOKEN STATUS VALUES
    Verified against constants/constants.go - the block is an iota run, so the
    numbers are positional and worth pinning:
        0 Free          1 Locked        2 Generated     3 Fetched
        4 Transferred   5 Committed     6 Pledged       7 QuorumPledged
        8 Burnt         9 BurntForFT   10 Deployed     11 Executed
"""

import sys

DB_PORT = 5433
DB_NAME = "rubix"
DB_USER = "rubix"
DB_PASSWORD = "rubixpass"
CONNECT_TIMEOUT = 8

# Token types, read from the live token_type table on the fleet.
#
# THIS FILTER IS NOT OPTIONAL. One `tokens` table holds every asset type, so a
# query that omits token_type silently mixes them. Measured on .104: free rows
# totalled 2016.000, of which 5.000 was FT and only 2011.000 was RBT - an
# unfiltered "RBT balance" would have been wrong by exactly the FT holding, and
# wrong in a way that looks plausible.
TYPE_RBT = 1
TYPE_NFT = 2
TYPE_FT = 3
TYPE_SC = 4

TYPE_NAME = {1: "rbt", 2: "nft", 3: "ft", 4: "smart_contract"}

# Verified against constants/constants.go
FREE = 0
LOCKED = 1
TRANSFERRED = 4
COMMITTED = 5
PLEDGED = 6
QUORUM_PLEDGED = 7
BURNT = 8
BURNT_FOR_FT = 9

STATUS_NAME = {
    0: "Free", 1: "Locked", 2: "Generated", 3: "Fetched",
    4: "Transferred", 5: "Committed", 6: "Pledged", 7: "QuorumPledged",
    8: "Burnt", 9: "BurntForFT", 10: "Deployed", 11: "Executed",
    12: "PinnedAsService", 13: "Orphaned", 14: "ChainSyncIssue",
    15: "BeingDoubleSpent", 99: "Seed",
}


class DBUnavailable(Exception):
    """Raised when the database cannot be reached or the driver is missing.

    Cases catch this and report SKIP with the message. It is deliberately NOT
    a silent failure: a DB check that quietly returns 'fine' when it could not
    connect is worse than no check at all.
    """


def _driver():
    try:
        import psycopg2
        return psycopg2
    except ImportError:
        raise DBUnavailable(
            "psycopg2 is not installed on this controller, so database checks "
            "cannot run. Install it with:  sudo apt install -y python3-psycopg2")


def available():
    """True if the driver is importable. Does NOT test any connection."""
    try:
        _driver()
        return True
    except DBUnavailable:
        return False


# Passed as libpq options at connect time. The server then rejects any write
# on this session, so read-only does not depend on this file staying careful.
READ_ONLY_OPTS = "-c default_transaction_read_only=on"


def _connect(host, port=DB_PORT, read_only=True):
    """Open a connection. READ-ONLY unless a caller explicitly asks otherwise.

    read_only=False exists solely for the DB-SEED cases and is reached only
    through writable_query(), never from any read path.
    """
    psycopg2 = _driver()
    kwargs = dict(host=host, port=port, dbname=DB_NAME, user=DB_USER,
                  password=DB_PASSWORD, connect_timeout=CONNECT_TIMEOUT)
    if read_only:
        kwargs["options"] = READ_ONLY_OPTS
    try:
        return psycopg2.connect(**kwargs)
    except Exception as e:
        raise DBUnavailable(
            "cannot reach Postgres at {}:{} ({}). Check the container is up on "
            "that host:  ssh rubix@{} 'docker ps | grep postgres'".format(
                host, port, e, host))


def query(host, sql, params=None, port=DB_PORT):
    """Run one query on a READ-ONLY connection; return rows as a list of tuples.

    The connection is read-only at the server, so a SELECT that was carelessly
    edited into an UPDATE fails loudly instead of changing lab state.
    """
    conn = _connect(host, port, read_only=True)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()
    finally:
        conn.close()


def writable_query(host, sql, params=None, port=DB_PORT,
                   i_understand_this_writes=False):
    """Run a statement that CHANGES DATA. For DB-SEED cases only.

    Deliberately awkward to call. A DB-SEED case corrupts state on purpose to
    prove the product rejects it, so the write is the point - but it must never
    happen by accident, and a reader of the case must see immediately that it
    writes.

    Rules for any case using this:
      1. Record what you changed BEFORE changing it, so it can be restored.
      2. Restore it afterwards, in a finally block.
      3. Run DB-SEED cases LAST in a cycle - they leave the node dirty until
         restored, and a failure mid-case can leave it dirty regardless.

    Every statement is echoed to stderr so the run output shows what was done.
    """
    if not i_understand_this_writes:
        raise DBUnavailable(
            "writable_query() refused: pass i_understand_this_writes=True. "
            "This function CHANGES LAB DATA and is only for DB-SEED cases, "
            "which must record the original values and restore them afterwards")

    sys.stderr.write("[db_client] WRITE on {}: {} params={}\n".format(host, sql, params))
    conn = _connect(host, port, read_only=False)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Denomination counter
# ---------------------------------------------------------------------------

def denom_counter(host, did, port=DB_PORT):
    """token_denom as {denom: count} - what the node BELIEVES it holds.

    This counter is what lockTokensForSplitOnce consults to decide which
    denominations to select from (core/wallet/token_lock.go:505). It is a
    cache, and a wrong one is dangerous: it makes a later selection ask for
    rows that are no longer Free, and the operation dies with
    "lockSelectedTokens: no tokens provided" - blamed on whatever transaction
    ran next, not on the one that corrupted the counter.
    """
    rows = query(host, "SELECT denom, count FROM token_denom WHERE did = %s",
                 (did,), port)
    return {float(d): int(c) for d, c in rows}


def real_free_denoms(host, did, port=DB_PORT):
    """{denom: count} computed from the tokens table - what the node ACTUALLY holds.

    Only Free (status 0) tokens count, because token_denom is meant to reflect
    SPENDABLE balance (core/wallet/recovery.go:663).
    """
    rows = query(
        host,
        "SELECT token_value, COUNT(*) FROM tokens "
        "WHERE did = %s AND token_status = %s AND token_type = %s "
        "GROUP BY token_value",
        (did, FREE, TYPE_RBT), port)
    return {float(v): int(c) for v, c in rows}


def denom_drift(host, did, port=DB_PORT):
    """Compare counter against reality. Returns {denom: (counted, actual)} for
    every denomination where they disagree. Empty dict means consistent.

    Denominations absent from one side are treated as 0 there, so a counter row
    that survives after its tokens are gone still shows up - that is precisely
    the drift worth catching.
    """
    counted = denom_counter(host, did, port)
    actual = real_free_denoms(host, did, port)
    out = {}
    for d in set(counted) | set(actual):
        c, a = counted.get(d, 0), actual.get(d, 0)
        if c != a:
            out[d] = (c, a)
    return out


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def token_status_summary(host, did, port=DB_PORT, token_type=TYPE_RBT):
    """{status_name: (count, total_value)} for one DID - a readable snapshot."""
    rows = query(
        host,
        "SELECT token_status, COUNT(*), COALESCE(SUM(token_value), 0) "
        "FROM tokens WHERE did = %s AND token_type = %s GROUP BY token_status",
        (did, token_type), port)
    return {STATUS_NAME.get(int(s), "status_{}".format(s)): (int(c), float(v))
            for s, c, v in rows}


def value_in_status(host, did, status, port=DB_PORT, token_type=TYPE_RBT):
    """Total token_value held by one DID in one status."""
    rows = query(
        host,
        "SELECT COALESCE(SUM(token_value), 0) FROM tokens "
        "WHERE did = %s AND token_status = %s AND token_type = %s",
        (did, status, token_type), port)
    return float(rows[0][0]) if rows else 0.0


def count_in_status(host, did, status, port=DB_PORT, token_type=TYPE_RBT):
    rows = query(
        host,
        "SELECT COUNT(*) FROM tokens "
        "WHERE did = %s AND token_status = %s AND token_type = %s",
        (did, status, token_type), port)
    return int(rows[0][0]) if rows else 0


def free_token_values(host, did, port=DB_PORT):
    """Every Free token's value for one DID, largest first.

    Used to prove a wallet holds ONLY fractional tokens - the precondition the
    FT-from-parts cases need and cannot establish through the API, which only
    reports a total.
    """
    rows = query(
        host,
        "SELECT token_value FROM tokens "
        "WHERE did = %s AND token_status = %s AND token_type = %s "
        "ORDER BY token_value DESC",
        (did, FREE, TYPE_RBT), port)
    return [float(v) for (v,) in rows]


def snapshot(host, did, port=DB_PORT):
    """One before/after picture of a DID's RBT accounting.

    Exists so a case takes the SAME reading before and after, and asserts on the
    DELTA. Reading only afterwards forces a case to assume a starting point -
    FT-P-03 did exactly that, treating a cumulative BurntForFT total as if it
    were this mint's burn, on the strength of a comment saying the wallet was
    fresh. An assumption in a comment is not a measurement.

    Every figure is RBT-only (token_type=1) and scoped to one DID.
    """
    counter = denom_counter(host, did, port)
    actual = real_free_denoms(host, did, port)
    drift = {}
    for d in set(counter) | set(actual):
        c, a = counter.get(d, 0), actual.get(d, 0)
        if c != a:
            drift[d] = (c, a)
    return {
        "free": value_in_status(host, did, FREE, port),
        "free_rows": count_in_status(host, did, FREE, port),
        "committed": value_in_status(host, did, COMMITTED, port),
        "burnt_for_ft": value_in_status(host, did, BURNT_FOR_FT, port),
        "burnt_for_ft_rows": count_in_status(host, did, BURNT_FOR_FT, port),
        "pledged": pledged_value(host, did, port),
        "denom": counter,
        "denom_drift": drift,
    }


# Every before/after pair a case records lands here, and test_runner writes it
# into the run's JSON report. A verdict without the readings behind it asks the
# reader to trust the harness; a developer reviewing a fix should be able to see
# the actual rows and check the arithmetic themselves.
EVIDENCE = []


def record(case_id, label, host, did, snap):
    """Keep one snapshot as evidence for the report."""
    EVIDENCE.append({
        "case": case_id,
        "label": label,          # "before" / "after" / "after deploy 3"
        "host": host,
        "did": did,
        "free": round(snap["free"], 4),
        "free_rows": snap["free_rows"],
        "committed": round(snap["committed"], 4),
        "burnt_for_ft": round(snap["burnt_for_ft"], 4),
        "pledged": round(snap["pledged"], 4),
        "denom": {str(k): v for k, v in sorted(snap["denom"].items())},
        "denom_drift": {str(k): list(v) for k, v in sorted(snap["denom_drift"].items())},
    })
    return snap


def evidence_for(case_id):
    return [e for e in EVIDENCE if e["case"] == case_id]


def format_evidence(before, after):
    """One-line before/after summary, for the human-readable report column.

    Deliberately shows the DENOMINATION MAP either side, not just a verdict:
    "counter ok" is a claim, "1.000: 5 -> 4" is the reading it rests on.
    """
    d = delta(before, after)
    denoms = sorted(set(before["denom"]) | set(after["denom"]))
    moved = ["{:.3f}:{}->{}".format(k, before["denom"].get(k, 0),
                                    after["denom"].get(k, 0))
             for k in denoms
             if before["denom"].get(k, 0) != after["denom"].get(k, 0)]
    return "free {:.3f}->{:.3f} committed {:.3f}->{:.3f} burnt {:.3f}->{:.3f}{}".format(
        before["free"], after["free"], before["committed"], after["committed"],
        before["burnt_for_ft"], after["burnt_for_ft"],
        " | denom " + " ".join(moved) if moved else " | denom unchanged")


def delta(before, after):
    """What changed between two snapshots. Scalars only; denom compared separately."""
    return {k: after[k] - before[k] for k in
            ("free", "free_rows", "committed", "burnt_for_ft",
             "burnt_for_ft_rows", "pledged")}


def new_drift(before, after):
    """Drift present AFTER that was not present BEFORE.

    Keeps a case honest about attribution: pre-existing drift is somebody
    else's finding, not this operation's.
    """
    return {d: v for d, v in after["denom_drift"].items()
            if d not in before["denom_drift"]}


def describe_drift(drift):
    return "; ".join("denom {:.3f}: counter={} free={}".format(d, c, a)
                     for d, (c, a) in sorted(drift.items()))


def pledged_value(host, did, port=DB_PORT):
    """Total value this DID currently has pledged, as quorum or otherwise.

    Statuses 6 (Pledged) and 7 (QuorumPledged) together - core's own validator
    treats them as one group for exactly this reason. A quorum must pledge at
    least the transaction value (core/consensus/checks.go:539), and after the
    transaction settles the pledge should be released, so this returning to its
    earlier level is as much the assertion as it rising was.
    """
    rows = query(
        host,
        "SELECT COALESCE(SUM(token_value), 0) FROM tokens "
        "WHERE did = %s AND token_status IN (%s, %s) AND token_type = %s",
        (did, PLEDGED, QUORUM_PLEDGED, TYPE_RBT), port)
    return float(rows[0][0]) if rows else 0.0


def open_pledges(host, port=DB_PORT):
    """tx_ids in unpledge_sequence_info with no matching transactions row.

    An unpledge queued against a transaction that does not exist is a pledge
    that can never be released.
    """
    rows = query(
        host,
        "SELECT u.tx_id FROM unpledge_sequence_info u "
        "WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = u.tx_id)",
        port=port)
    return [t for (t,) in rows]


def token_rows(host, did, status=None, port=DB_PORT, token_type=TYPE_RBT):
    """Individual RBT rows for a DID: [(token_id, value, status, parent_id)].

    Row-level rather than a SUM. A total cannot distinguish "committed 1.000 as
    one whole token" from "committed 0.354 and returned 0.646 as change" when
    the wallet held other tokens - only looking at which rows exist can.
    """
    sql = ("SELECT token_id, token_value, token_status, parent_token_id "
           "FROM tokens WHERE did = %s AND token_type = %s")
    params = [did, token_type]
    if status is not None:
        sql += " AND token_status = %s"
        params.append(status)
    sql += " ORDER BY token_value DESC, token_id"
    return [(t, float(v), int(st), pid)
            for t, v, st, pid in query(host, sql, tuple(params), port)]


def children_of(host, parent_token_id, port=DB_PORT):
    """Rows produced by splitting a parent token: [(token_id, value, status)]."""
    rows = query(
        host,
        "SELECT token_id, token_value, token_status FROM tokens "
        "WHERE parent_token_id = %s ORDER BY token_value DESC",
        (parent_token_id,), port)
    return [(t, float(v), int(st)) for t, v, st in rows]


def chain_rows(host, token_id, port=DB_PORT):
    """tokenchain entries for one token: [(position, transaction_id, prev_id, role)]."""
    rows = query(
        host,
        "SELECT position, transaction_id, previous_transaction_id, role "
        "FROM tokenchain WHERE token_id = %s ORDER BY position",
        (token_id,), port)
    return [(int(p), t, prev, int(r)) for p, t, prev, r in rows]


def unpledge_rows(host, tx_id, port=DB_PORT):
    """unpledge_sequence_info for one transaction: [(quorum_did, pledge_tokens)].

    A pledge with no unpledge row queued can never be released.
    """
    rows = query(
        host,
        "SELECT quorum_did, pledge_tokens FROM unpledge_sequence_info WHERE tx_id = %s",
        (tx_id,), port)
    return [(q, list(pt or [])) for q, pt in rows]


def negative_denoms(host, did=None, port=DB_PORT):
    """token_denom rows with a negative count - should never exist."""
    if did:
        rows = query(host, "SELECT did, denom, count FROM token_denom "
                           "WHERE count < 0 AND did = %s", (did,), port)
    else:
        rows = query(host, "SELECT did, denom, count FROM token_denom WHERE count < 0",
                     port=port)
    return [(d, float(dn), int(c)) for d, dn, c in rows]


def orphan_tokens(host, port=DB_PORT):
    """Free RBT rows with no tokenchain entry at all."""
    rows = query(
        host,
        "SELECT t.token_id FROM tokens t "
        "WHERE t.token_type = %s AND t.token_status = %s "
        "AND NOT EXISTS (SELECT 1 FROM tokenchain c WHERE c.token_id = t.token_id)",
        (TYPE_RBT, FREE), port)
    return [t for (t,) in rows]


def duplicate_token_ids(host, port=DB_PORT):
    """token_ids appearing more than once - should always be empty."""
    rows = query(
        host,
        "SELECT token_id, COUNT(*) c FROM tokens GROUP BY token_id HAVING COUNT(*) > 1",
        port=port)
    return [(t, int(c)) for t, c in rows]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def transaction_exists(host, tx_id, port=DB_PORT):
    rows = query(host, "SELECT 1 FROM transactions WHERE id = %s LIMIT 1",
                 (tx_id,), port)
    return bool(rows)


def transaction_participants(host, tx_id, port=DB_PORT):
    """(initiator, owner) read from the transaction's own info JSON.

    Derived from the row rather than guessed from which hosts the test used, so
    a persistence check asserts against what the transaction actually claims.
    """
    rows = query(
        host,
        "SELECT info->>'initiator', info->>'owner' FROM transactions "
        "WHERE id = %s LIMIT 1",
        (tx_id,), port)
    if not rows:
        return None, None
    return rows[0][0], rows[0][1]


# ---------------------------------------------------------------------------
# Structural capture and diff
#
# A balance says a transfer "worked". It cannot say the split produced the right
# children, that the parent was burnt rather than left Free, that every new
# token carries a chain row, or that the denomination counter moved with
# reality. All of that lives in the ROWS, so a case asserting only on a balance
# passes while the wallet underneath it is wrong - which is precisely the class
# of bug this suite exists to find.
#
# capture() records the rows, diff() says what changed, and check_invariants()
# applies the rules that must hold whatever the case was doing, so every case
# gets them without asking.
# ---------------------------------------------------------------------------

# The parts tree alternates 2 and 5 children per level down from a whole token,
# so these are the only values a legal RBT token can hold (core/parts/parts.go
# MaxPossiblePartsIndexByMaxDecimalPlaces, 3 decimal places = 6 levels).
LEGAL_DENOMS = (1.0, 0.5, 0.1, 0.05, 0.01, 0.005, 0.001)


def _denom_ok(value):
    return any(abs(value - d) < 1e-9 for d in LEGAL_DENOMS)


def capture(host, did, port=DB_PORT, token_type=TYPE_RBT):
    """Every row describing one DID's wallet, plus the chain length per token.

    Three queries, not one per token: a 50,000-token wallet would otherwise
    need 50,000 round trips and the case would time out before asserting
    anything.
    """
    tokens = {}
    for tid, val, status, parent in query(
            host,
            "SELECT token_id, token_value, token_status, parent_token_id "
            "FROM tokens WHERE did = %s AND token_type = %s",
            (did, token_type), port):
        tokens[tid] = {"value": float(val), "status": int(status), "parent": parent}

    chain_len = {}
    for tid, n in query(
            host,
            "SELECT tc.token_id, COUNT(*) FROM tokenchain tc "
            "JOIN tokens t ON t.token_id = tc.token_id "
            "WHERE t.did = %s AND t.token_type = %s GROUP BY tc.token_id",
            (did, token_type), port):
        chain_len[tid] = int(n)

    totals = {}
    for name, st in (("free", FREE), ("locked", LOCKED), ("committed", COMMITTED),
                     ("burnt_for_ft", BURNT_FOR_FT), ("burnt", BURNT)):
        totals[name] = round(sum(t["value"] for t in tokens.values()
                                 if t["status"] == st), 6)
    totals["pledged"] = pledged_value(host, did, port)

    return {"host": host, "did": did, "tokens": tokens, "chain_len": chain_len,
            "denom": denom_counter(host, did, port), "totals": totals}


def diff(before, after):
    """What changed between two captures of the same DID."""
    b, a = before["tokens"], after["tokens"]
    created = dict((t, a[t]) for t in a if t not in b)
    removed = dict((t, b[t]) for t in b if t not in a)
    status_changed = dict((t, (b[t]["status"], a[t]["status"]))
                          for t in a if t in b and a[t]["status"] != b[t]["status"])
    value_changed = dict((t, (b[t]["value"], a[t]["value"]))
                         for t in a if t in b and abs(a[t]["value"] - b[t]["value"]) > 1e-9)
    chain_grew = dict((t, (before["chain_len"].get(t, 0), after["chain_len"].get(t, 0)))
                      for t in after["chain_len"]
                      if after["chain_len"].get(t, 0) != before["chain_len"].get(t, 0))
    denom = {}
    for d_val in set(before["denom"]) | set(after["denom"]):
        moved = after["denom"].get(d_val, 0) - before["denom"].get(d_val, 0)
        if moved:
            denom[round(d_val, 3)] = moved
    totals = dict((k, round(after["totals"].get(k, 0) - before["totals"].get(k, 0), 6))
                  for k in set(before["totals"]) | set(after["totals"]))
    return {"did": after["did"], "host": after["host"],
            "created": created, "removed": removed,
            "status_changed": status_changed, "value_changed": value_changed,
            "chain_grew": chain_grew, "denom": denom,
            "totals": dict((k, v) for k, v in totals.items() if abs(v) > 1e-9)}


def check_invariants(d, after):
    """Rules that hold after ANY operation. Returns a list of problems.

    Run automatically after every case, so a case that only asserts a balance
    still catches a malformed wallet underneath it.
    """
    problems = []

    for tid, info in d["created"].items():
        if after["chain_len"].get(tid, 0) < 1:
            problems.append(
                "token {} was created with NO chain row - it counts toward the "
                "balance and the denomination counter, so selection will pick it "
                "and then fail validation".format(tid))
        if not _denom_ok(info["value"]):
            problems.append(
                "token {} has value {} which is not a legal denomination".format(
                    tid, info["value"]))

    if d["value_changed"]:
        problems.append(
            "a token's value changed in place, which nothing should ever do: " +
            "; ".join("{} {} -> {}".format(t, o, n)
                      for t, (o, n) in list(d["value_changed"].items())[:3]))

    # A split must consume its parent: if this wallet still holds the parent
    # of a part token that just appeared, the parent may not be Free, or the
    # same value exists twice. (A RECEIVED part's parent stays with the sender
    # - its absence here is normal, so only a parent held here is checked.)
    for tid, info in d["created"].items():
        parent = info.get("parent")
        if parent and parent in after["tokens"] and after["tokens"][parent]["status"] == FREE:
            problems.append(
                "token {} was split from parent {}, but the parent is still Free - the "
                "same value is spendable twice".format(tid, parent))

    # token_denom must move with the free rows it claims to count.
    moved_rows = {}
    def _bump(value, by):
        key = round(value, 3)
        moved_rows[key] = moved_rows.get(key, 0) + by

    for tid, info in d["created"].items():
        if info["status"] == FREE:
            _bump(info["value"], 1)
    for tid, info in d["removed"].items():
        if info["status"] == FREE:
            _bump(info["value"], -1)
    for tid, (old, new) in d["status_changed"].items():
        value = after["tokens"][tid]["value"]
        if old == FREE and new != FREE:
            _bump(value, -1)
        elif new == FREE and old != FREE:
            _bump(value, 1)

    for denom_v, moved in moved_rows.items():
        counted = d["denom"].get(denom_v, 0)
        if moved != counted:
            problems.append(
                "denomination {:.3f}: free rows moved by {} but token_denom moved "
                "by {} - the counter and the rows disagree".format(
                    denom_v, moved, counted))

    return problems


def describe_diff(d):
    """One line a report row can carry."""
    bits = []
    if d["created"]:
        bits.append("+{} token(s)".format(len(d["created"])))
    if d["removed"]:
        bits.append("-{} token(s)".format(len(d["removed"])))
    if d["status_changed"]:
        bits.append("{} status change(s)".format(len(d["status_changed"])))
    if d["chain_grew"]:
        bits.append("{} chain row(s)".format(
            sum(n - o for o, n in d["chain_grew"].values())))
    for k, v in sorted(d["totals"].items()):
        bits.append("{} {:+.3f}".format(k, v))
    return ", ".join(bits) or "nothing changed"


def expect_spend(before, after, amount, tol=0.0015):
    """Sender side of a transfer, checked on the rows rather than the balance.

    Free value must fall by exactly `amount`, and if a split was needed the
    tokens that appeared must be accounted for by a parent that left.
    Returns (ok, diff, problems).
    """
    d = diff(before, after)
    problems = check_invariants(d, after)
    spent = before["totals"]["free"] - after["totals"]["free"]
    if abs(spent - amount) > tol:
        problems.append("free value fell by {:.4f}, expected {:.4f}".format(spent, amount))
    burnt_parents = [t for t, (o, n) in d["status_changed"].items() if n == BURNT]
    if d["created"] and not burnt_parents and not d["removed"]:
        problems.append(
            "{} new token(s) appeared with no parent burnt or released - a split "
            "must consume what it splits".format(len(d["created"])))
    return (not problems), d, problems


def expect_receive(before, after, amount, tol=0.0015):
    """Receiver side: free value rose by exactly `amount`, in real tokens."""
    d = diff(before, after)
    problems = check_invariants(d, after)
    gained = after["totals"]["free"] - before["totals"]["free"]
    if abs(gained - amount) > tol:
        problems.append("free value rose by {:.4f}, expected {:.4f}".format(gained, amount))
    return (not problems), d, problems


def wallet_totals(host, did, port=DB_PORT, token_type=TYPE_RBT):
    """Value and row count per status for one DID, as SUMs.

    Deliberately not capture(): a fleet-wide ledger over 31 hosts does not need
    every token row shipped back, only the totals.
    """
    out = {}
    for status, value, count in query(
            host,
            "SELECT token_status, COALESCE(SUM(token_value), 0), COUNT(*) "
            "FROM tokens WHERE did = %s AND token_type = %s GROUP BY token_status",
            (did, token_type), port):
        out[int(status)] = (float(value), int(count))
    return out
