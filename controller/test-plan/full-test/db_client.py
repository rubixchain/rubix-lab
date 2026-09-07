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


def assert_read_only(host, port=DB_PORT):
    """Prove the read path really cannot write. Returns (ok, note).

    Attempts a harmless write on a normal query() connection and expects it to
    be REFUSED. Used by preflight so the guarantee is verified on the actual
    fleet rather than assumed from the code.
    """
    try:
        query(host, "CREATE TEMP TABLE _lab_write_probe (x int)", port=port)
    except DBUnavailable:
        raise
    except Exception as e:
        if "read-only" in str(e).lower():
            return True, "server refused a write on the read path, as intended"
        return False, "write was refused, but not as a read-only error: {}".format(e)
    return False, ("A WRITE SUCCEEDED ON THE READ PATH - default_transaction_read_only "
                   "is not being applied. Do not run DB cases until this is fixed")


def ping(host, port=DB_PORT):
    """Return (ok, note) - a cheap reachability probe used by preflight."""
    try:
        query(host, "SELECT 1", port=port)
        return True, ""
    except DBUnavailable as e:
        return False, str(e)


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
        "WHERE did = %s AND token_status = %s GROUP BY token_value",
        (did, FREE), port)
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

def token_status_summary(host, did, port=DB_PORT):
    """{status_name: (count, total_value)} for one DID - a readable snapshot."""
    rows = query(
        host,
        "SELECT token_status, COUNT(*), COALESCE(SUM(token_value), 0) "
        "FROM tokens WHERE did = %s GROUP BY token_status",
        (did,), port)
    return {STATUS_NAME.get(int(s), "status_{}".format(s)): (int(c), float(v))
            for s, c, v in rows}


def value_in_status(host, did, status, port=DB_PORT):
    """Total token_value held by one DID in one status."""
    rows = query(
        host,
        "SELECT COALESCE(SUM(token_value), 0) FROM tokens "
        "WHERE did = %s AND token_status = %s",
        (did, status), port)
    return float(rows[0][0]) if rows else 0.0


def count_in_status(host, did, status, port=DB_PORT):
    rows = query(
        host,
        "SELECT COUNT(*) FROM tokens WHERE did = %s AND token_status = %s",
        (did, status), port)
    return int(rows[0][0]) if rows else 0


def free_token_values(host, did, port=DB_PORT):
    """Every Free token's value for one DID, largest first.

    Used to prove a wallet holds ONLY fractional tokens - the precondition the
    FT-from-parts cases need and cannot establish through the API, which only
    reports a total.
    """
    rows = query(
        host,
        "SELECT token_value FROM tokens WHERE did = %s AND token_status = %s "
        "ORDER BY token_value DESC",
        (did, FREE), port)
    return [float(v) for (v,) in rows]


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
        "WHERE did = %s AND token_status IN (%s, %s)",
        (did, PLEDGED, QUORUM_PLEDGED), port)
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
