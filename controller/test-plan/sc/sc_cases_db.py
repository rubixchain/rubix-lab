#!/usr/bin/env python3
"""
sc_cases_db.py - row-level database checks around a smart contract deploy.

Imported by sc_cases.py. These go below the totals every other case works
from: which ROWS exist afterwards, not just what they sum to.

WHY ROW-LEVEL AT ALL
    "Committed rose by 0.354" is consistent with several different realities.
    It is consistent with the fix working - a 1.000 token split, 0.354
    committed, 0.646 returned as a change row. It is also consistent with a
    0.354 token that happened to be lying in the wallet being committed whole,
    which proves nothing about splitting. And on a busy wallet it is consistent
    with two unrelated movements cancelling out.

    The split fix's actual claim is that CollectRBTTokens returns
    childTokensKept and those children are persisted. That claim is about ROWS.
    Only reading them settles it.

SCHEMA COUPLING
    These bind to table and column names (tokens.parent_token_id,
    tokenchain.position, transactions.info). That is a real cost: if the
    product reshapes those tables, these break before anything else. They are
    worth it here because this PR's whole subject is what lands in those
    tables - but they are the first cases to revisit after a schema change.
    DB-SCHEMA.md records the shape they were written against.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _link():
    import sc_cases
    return sc_cases


# ---------------------------------------------------------------------------
# SC-C-20
# ---------------------------------------------------------------------------

def sc_c_20(ctx, ci):
    """
    SC-C-20 - The change token exists as a row after a fractional deploy.

    WHAT IT CHECKS
        After deploying 0.354 from a wallet of 1.000 tokens:
          * a Committed row of 0.354 exists that did not exist before
          * a NEW Free row of about 0.646 exists - the change
          * committed + change == the denomination consumed (1.000)

    WHY IT MATTERS
        This is the fix's actual claim, stated as rows rather than inferred
        from a balance. CollectRBTTokens returns childTokensKept and the change
        must be persisted; before the fix the whole token was committed as-is
        and the remainder simply ceased to exist.

        A balance check cannot separate "split correctly" from "there happened
        to be a 0.354 token available". Rows can: a change row of 0.646 can
        only come from splitting a 1.000.

    MANUAL STEPS
        1. Before, list the wallet's RBT rows:
             psql -h $SENDER -p 5433 -U rubix -d rubix -c \\
               "SELECT token_id, token_value, token_status, parent_token_id
                  FROM tokens WHERE did='<DID>' AND token_type=1
                  ORDER BY token_value DESC;"
        2. Deploy with "value":0.354 (see SC-C-01).
        3. Run the same query. Look for:
             - a row with token_status=5 (Committed) and value 0.354
             - a NEW row with token_status=0 (Free) and value ~0.646
             - that new row's parent_token_id naming the 1.000 token consumed

    PASS / FAIL
        PASS  both rows present and they sum to the consumed denomination
        FAIL  committed 1.000 as one row and no change row -> the remainder was
              destroyed, which is exactly the bug
        FAIL  change row present but the sum does not reconcile -> value lost
              in the split
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    value = 0.354
    try:
        free_before = {t for t, _v, _st, _p in
                       db.token_rows(s["host"], s["did"], db.FREE)}
        comm_before = {t for t, _v, _st, _p in
                       db.token_rows(s["host"], s["did"], db.COMMITTED)}
        whole_before = [v for _t, v, _st, _p in
                        db.token_rows(s["host"], s["did"], db.FREE) if v >= 1.0]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not whole_before:
        # The wallet needs a whole token so the change is unambiguous. Fund it
        # rather than skipping - generate_local_rbt mints whole tokens.
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            free_before = {t for t, _v, _st, _p in
                           db.token_rows(s["host"], s["did"], db.FREE)}
            whole_before = [v for _t, v, _st, _p in
                            db.token_rows(s["host"], s["did"], db.FREE) if v >= 1.0]
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        if not whole_before:
            return False, "no whole token available", (
                "funded the wallet but it still holds no 1.000 token - the "
                "change produced by a split would be ambiguous")

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="change-token check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        free_after = db.token_rows(s["host"], s["did"], db.FREE)
        comm_after = db.token_rows(s["host"], s["did"], db.COMMITTED)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    new_committed = [(t, v) for t, v, _st, _p in comm_after if t not in comm_before]
    new_free = [(t, v, p) for t, v, _st, p in free_after if t not in free_before]

    committed_val = sum(v for _t, v in new_committed)
    change_val = sum(v for _t, v, _p in new_free)

    problems = []
    if not rc.close_enough(committed_val, value, tol=sc.cost_tolerance(value)):
        problems.append("committed {:.4f} across {} row(s), expected {:.3f}".format(
            committed_val, len(new_committed), value))
    if not new_free:
        problems.append("NO change row appeared - the remainder of the token "
                        "backing this deploy was destroyed rather than returned")
    else:
        total = committed_val + change_val
        if not rc.close_enough(total, round(total), tol=0.01):
            problems.append("committed {:.4f} + change {:.4f} = {:.4f}, which is "
                            "not a whole denomination - value went missing in the "
                            "split".format(committed_val, change_val, total))

    return (not problems), "committed {} row(s)={:.4f}, change {} row(s)={:.4f}".format(
        len(new_committed), committed_val, len(new_free), change_val), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-21
# ---------------------------------------------------------------------------

def sc_c_21(ctx, ci):
    """
    SC-C-21 - Chain rows exist for the contract and for the split children.

    WHAT IT CHECKS
        After a fractional deploy, `tokenchain` holds an entry for the smart
        contract token, and the change token produced by the split also has
        chain rows.

    WHY IT MATTERS
        A token row without a chain row is a token with no history: it exists
        in the wallet but cannot be validated, and the failure appears much
        later as a chain-mismatch on some unrelated transfer.

        The fix persists the split via PersistGenesisTransaction, and the code
        comment notes this has to happen BEFORE the outer transaction opens or
        it deadlocks. Something persisted through that separate path is exactly
        what could end up with a token row and no chain row.

    MANUAL STEPS
        1. Deploy a fractional value, noting the contract id.
        2. Chain for the contract:
             SELECT position, transaction_id, previous_transaction_id, role
               FROM tokenchain WHERE token_id='<SC_ID>' ORDER BY position;
        3. Find the change token, then run the same query for its token_id.

    PASS / FAIL
        PASS  contract has at least one chain row, and every new Free token has
              at least one
        FAIL  a token exists with no chain row -> unvalidatable, and it will
              fail later somewhere unrelated
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        free_before = {t for t, _v, _st, _p in db.token_rows(s["host"], s["did"], db.FREE)}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    value = sc.rand_value(0.100, 0.999)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="chain row check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        sc_chain = db.chain_rows(s["host"], sc_id)
        free_after = db.token_rows(s["host"], s["did"], db.FREE)
        new_free = [t for t, _v, _st, _p in free_after if t not in free_before]
        orphans = [t for t in new_free if not db.chain_rows(s["host"], t)]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not sc_chain:
        problems.append("the contract token has NO tokenchain row - it was "
                        "deployed but has no history")
    if orphans:
        problems.append("{} new token(s) have no chain row: {} - they cannot be "
                        "validated and will fail a later transfer".format(
                            len(orphans), ", ".join(t[:16] for t in orphans[:3])))

    return (not problems), "sc chain rows={} new tokens={} orphans={}".format(
        len(sc_chain), len(new_free), len(orphans)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-22
# ---------------------------------------------------------------------------

def sc_c_22(ctx, ci):
    """
    SC-C-22 - The deploy is recorded in the transactions table.

    WHAT IT CHECKS
        A row exists in `transactions` for the deploy, and its info names the
        deployer as initiator.

    WHY IT MATTERS
        The API returning success means the request was accepted. A row in
        `transactions` means the node actually recorded it. Those are different
        claims, and the gap between them is where a "successful" operation
        disappears on restart.

        Reading the initiator from the row rather than assuming it also
        confirms the deploy was attributed to the right DID - collateral taken
        from one wallet and recorded against another would balance, and be
        wrong.

    MANUAL STEPS
        After a deploy:
          SELECT id, info->>'initiator', info->>'owner'
            FROM transactions ORDER BY created_at DESC LIMIT 5;
        The newest row should name the deployer as initiator.

    PASS / FAIL
        PASS  a new transaction row exists naming the deployer
        FAIL  no new row -> accepted but not recorded
        FAIL  recorded against a different DID
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = set(t for (t,) in db.query(
            s["host"], "SELECT id FROM transactions"))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    value = sc.rand_value(0.100, 0.999)
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="transaction row check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    try:
        after = [t for (t,) in db.query(s["host"], "SELECT id FROM transactions")]
        new = [t for t in after if t not in before]
        mine = []
        for tx in new:
            init, owner = db.transaction_participants(s["host"], tx)
            if init == s["did"]:
                mine.append(tx)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    if not new:
        problems.append("no new transactions row after a successful deploy - "
                        "accepted but not recorded")
    elif not mine:
        problems.append("{} new row(s), none naming {} as initiator - the deploy "
                        "was recorded against a different DID".format(
                            len(new), s["did"][:16]))

    return (not problems), "{} new transaction row(s), {} from this DID".format(
        len(new), len(mine)), "; ".join(problems)
