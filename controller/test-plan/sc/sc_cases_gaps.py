#!/usr/bin/env python3
"""
sc_cases_gaps.py - paths in PR #739 that the first full run did not reach.

Imported by sc_cases.py. Written after reviewing the first green run against
the diff, where four branches turned out to have no coverage at all.

    1. POST-SPLIT ROLLBACK. The collateral split runs in a pre-pass that
       commits its own genesis transaction on a separate connection, BEFORE the
       outer transaction opens. Nothing tested what happens when the outer
       transaction then fails. SC-C-06 is rejected before the lock, so it never
       reaches this. A failure here strands Locked tokens and orphans a genesis.

    2. NOVEL DENOMINATIONS. The denom write 977f6fba introduced is a bare
       UPDATE:
           UPDATE token_denom SET count = GREATEST(count-1,0)
            WHERE did = $1 AND denom = $2
       With no matching (did, denom) row that affects ZERO rows and returns no
       error. A split creates children at denominations the wallet has never
       held - so if nothing inserts a row for the new denomination, a later
       burn of that child silently does nothing.

    3. MULTI-SC IN ONE REQUEST. The pre-pass loops
       `for _, scInfo := range req.GetAllSmartContracts()`, so two contracts in
       ONE request each run the split, and the second iteration must see what
       the first committed. SC-C-12 fires five separate requests, which is a
       different path.

    4. RECEIVER-ROLE STATE. Every denom case so far measures the initiator.
       CRS-C-01 showed the receiver's node is where the deploy-bundle goes
       wrong, and nothing was looking there.
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _link():
    import sc_cases
    return sc_cases


# ---------------------------------------------------------------------------
# SC-C-27
# ---------------------------------------------------------------------------

def sc_c_27(ctx, ci):
    """
    SC-C-27 - Force a failure AFTER the collateral split has committed.

    WHAT IT CHECKS
        Deploy a value the wallet can cover but the quorum cannot pledge. The
        split runs and commits its genesis; consensus then fails. Afterwards
        the wallet must have nothing left Locked, and the free balance must be
        back where it started.

    WHY IT MATTERS
        This is the one ordering the fix deliberately introduced and nothing
        tested. The comment is explicit:

            "This runs BEFORE the non-RBT tx begins: PersistGenesisTransaction
             opens its own connection and upserts the burnt parent row"

        A separate connection means a separate transaction, which means the
        outer transaction failing does NOT roll the split back. The parent is
        burnt, children exist, tokens are Locked - and the deploy never
        happened.

        SC-C-06 looks like it covers this and does not: an over-balance deploy
        is rejected before the lock, so the pre-pass never runs. The failure has
        to come AFTER a successful split, which means out-pledging the quorum
        rather than out-spending the wallet.

    MANUAL STEPS
        1. Read the quorum's free balance:
             psql -h $QUORUM -p 5433 -U rubix -d rubix -c \\
               "SELECT COALESCE(SUM(token_value),0) FROM tokens
                 WHERE did='<QDID>' AND token_type=1 AND token_status=0;"
        2. Fund the deployer ABOVE that figure.
        3. Deploy with a value between the quorum's balance and the wallet's -
           the split will succeed, the pledge will not.
        4. Afterwards, on the deployer:
             SELECT token_id, token_value FROM tokens
              WHERE did='<DID>' AND token_status=1;    -- 1 = Locked
           This must return NO rows.

    PASS / FAIL
        PASS  deploy rejected, nothing Locked, balance restored
        FAIL  tokens left Locked -> the split was not rolled back and that
              value is stranded permanently
        FAIL  balance did not return -> the burnt parent was not restored
        SKIP  could not fund above the quorum (needs a large mint)
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)
    q = ctx.quorum_for(s) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)
    if q is None:
        return SKIP, "no quorum", "cannot out-pledge a quorum that is not known"

    try:
        q_free = db.value_in_status(q["host"], q["did"], db.FREE)
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # The value must exceed what the quorum can pledge but sit within the
    # wallet, so the split succeeds and only the pledge fails.
    value = round(q_free + 25.0, 3)
    need = value + 10
    ready, why = sc._prepare(ctx, s, need)
    if not ready:
        return SKIP, "could not fund above the quorum", (
            "needs {:.0f} RBT to out-pledge quorum {} ({:.0f} free): {}".format(
                need, q["host"], q_free, why))

    before = sc._bal(ctx, s)
    try:
        locked_before = db.count_in_status(s["host"], s["did"], db.LOCKED)
        snap_before = db.record("SC-C-27", "before", s["host"], s["did"],
                                db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="post-split rollback probe", port=ctx.port)
    time.sleep(SETTLE * 3)

    after = sc._bal(ctx, s)
    try:
        locked_after = db.count_in_status(s["host"], s["did"], db.LOCKED)
        locked_rows = db.token_rows(s["host"], s["did"], db.LOCKED)
        snap_after = db.record("SC-C-27", "after", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if ok:
        return SKIP, "quorum pledged after all", (
            "the {:.3f} deploy succeeded, so the pledge did not fail and the "
            "post-split path was never reached. Quorum had {:.0f} free - it may "
            "have been topped up by another lane".format(value, q_free))

    problems = []
    stranded = locked_after - locked_before
    if stranded > 0:
        problems.append("{} token(s) left LOCKED after a failed deploy ({}) - "
                        "the collateral split was not rolled back and that value "
                        "is stranded".format(
                            stranded,
                            ", ".join("{}={:.3f}".format(t[:12], v)
                                      for t, v, _st, _p in locked_rows[:3])))
    if before and after and not rc.close_enough(before["balance"], after["balance"],
                                                tol=TOL * 4):
        problems.append("free balance did not return: {:.4f} -> {:.4f} - the "
                        "burnt parent was not restored".format(
                            before["balance"], after["balance"]))
    drift = db.new_drift(snap_before, snap_after)
    if drift:
        problems.append("counter drifted after a failed deploy: "
                        + db.describe_drift(drift))

    return (not problems), "rejected at {:.3f} (quorum free {:.0f}), locked {}->{} | {}".format(
        value, q_free, locked_before, locked_after,
        db.format_evidence(snap_before, snap_after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-28
# ---------------------------------------------------------------------------

def sc_c_28(ctx, ci):
    """
    SC-C-28 - A split creates a novel denomination; the counter must track it.

    WHAT IT CHECKS
        Deploy a value that produces change at a denomination the wallet has
        never held. Afterwards a token_denom row must exist for that new
        denomination, and it must match the tokens actually held there.

    WHY IT MATTERS
        The decrement 977f6fba introduced is a bare UPDATE:

            UPDATE token_denom SET count = GREATEST(count-1,0)
             WHERE did = $1 AND denom = $2

        With no matching row that affects ZERO rows and returns no error. It is
        a silent no-op.

        A split is exactly how a wallet acquires a denomination it has never
        held: deploying 0.354 from a 1.000 produces a 0.646 change token, and
        0.646 is a value nothing minted. If the split does not also create the
        token_denom row, the counter never knows about it - and a later burn of
        that child updates nothing, silently.

        This is the reachable path to the missing-row case, without needing a
        recovered node or seeded corruption.

    MANUAL STEPS
        1. Note the wallet's denominations:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
        2. Deploy a value chosen so the change is a denomination NOT in that
           list - e.g. 0.354 from a 1.000 gives 0.646.
        3. List the free tokens and the counter again. The new denomination
           must appear in BOTH, with matching counts.

    PASS / FAIL
        PASS  the change denomination appears in token_denom and matches reality
        FAIL  the change token exists but has no counter row -> a later burn of
              it will silently do nothing, because the UPDATE has no row to hit
        SKIP  no whole token available to split
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = sc._prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.record("SC-C-28", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not any(v >= 1.0 for v in before["denom"]):
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            before = db.record("SC-C-28", "before (refunded)", s["host"], s["did"],
                               db.snapshot(s["host"], s["did"]))
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        if not any(v >= 1.0 for v in before["denom"]):
            return SKIP, "no whole token to split", "cannot create a novel denomination"

    # Deploy an arbitrary fraction. The change does NOT come back as one
    # tidy token - the first run showed it returns as many small ones
    # (0.001:206->207, 0.010:182->186, ...) - so this no longer guesses the
    # shape. It asks the question the bare UPDATE actually depends on:
    # does every denomination holding free tokens have a counter row?
    value = sc.rand_value(0.150, 0.850)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="novel denomination probe", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-28", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        real = db.real_free_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # Any denomination with free tokens but NO counter row is the missing-row
    # condition: the burn path's UPDATE would match nothing and silently
    # succeed.
    counted = set(after["denom"])
    uncounted = {d: c for d, c in real.items()
                 if c > 0 and not any(abs(d - k) < 1e-9 for k in counted)}
    new_denoms = {d for d in real if not any(abs(d - k) < 1e-9 for k in before["denom"])}

    problems = []
    if uncounted:
        problems.append(
            "{} denomination(s) hold free tokens with NO token_denom row: {} - "
            "the burn path is a bare UPDATE, so burning one of these would "
            "match no row, return no error, and leave the counter permanently "
            "wrong".format(
                len(uncounted),
                ", ".join("{:.3f}x{}".format(d, c)
                          for d, c in sorted(uncounted.items())[:4])))
    drift = db.new_drift(before, after)
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift))

    return (not problems), "deployed {:.3f}, {} new denomination(s), {} uncounted | {}".format(
        value, len(new_denoms), len(uncounted),
        db.format_evidence(before, after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-29
# ---------------------------------------------------------------------------

def sc_c_29(ctx, ci):
    """
    SC-C-29 - Three contracts, each with a value, in ONE request.

    WHAT IT CHECKS
        A single /tx carrying three smart contracts with different values. All
        three deploy, the total cost equals the sum of the three values, and
        the counter stays consistent.

    WHY IT MATTERS
        The collateral pre-pass loops over every contract in the request:

            for _, scInfo := range req.GetAllSmartContracts() { ... }

        Each iteration locks tokens, splits, and persists its own genesis. The
        SECOND iteration must see what the first committed - a different
        situation from concurrent separate requests, which touch different
        transactions entirely.

        SC-C-12 fires five requests at once and covers contention. This covers
        sequence within one request, where the failure mode is the opposite:
        the second split reading a wallet state the first has already changed,
        inside a call that has not finished.

    MANUAL STEPS
        Generate three contracts, then post ONE transaction naming all three:
          "smartContract":[{"smartContractId":"<A>","value":0.31,"data":"x"},
                           {"smartContractId":"<B>","value":0.47,"data":"x"},
                           {"smartContractId":"<C>","value":0.22,"data":"x"}]
        Check the balance fell by 1.00 exactly, and all three are listed.

    PASS / FAIL
        PASS  all three deploy, total cost exact, counter consistent
        FAIL  cost matches only the FIRST value -> later iterations did not
              take collateral
        FAIL  cost exceeds the sum -> an iteration double-locked
        FAIL  counter drifts -> the per-iteration decrements do not compose
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    values = [sc.rand_value(0.100, 0.400) for _ in range(3)]
    ready, why = sc._prepare(ctx, s, sum(values) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    entries = []
    for v in values:
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            return SKIP, "generation failed", err
        entries.append({"smartContractId": sc_id, "value": v,
                        "data": "multi-SC single request"})

    try:
        before = db.record("SC-C-29", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    bal_before = sc._bal(ctx, s)

    body = {
        "initiator": s["did"], "owner": "",
        "tokens": {"rbt": 0, "ft": [], "nft": [],
                   "smartContract": entries, "transferNftOwnership": False},
        "memo": "SC-C-29 three contracts in one request",
    }
    ok, msg, _ = rc._tx(s["host"], body, ctx.port)
    if not ok:
        return False, "multi-contract request rejected", (
            "three contracts in one request was refused: {} - if this is not "
            "supported it should be documented, since the builder loops over "
            "them".format(msg))
    time.sleep(SETTLE * 2)

    bal_after = sc._bal(ctx, s)
    try:
        after = db.record("SC-C-29", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
        listed = rc.list_smart_contracts(s["host"], ctx.port)[1]
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    import json as _json
    blob = _json.dumps(listed)
    present = sum(1 for e in entries if e["smartContractId"] in blob)
    spent = (bal_before["balance"] - bal_after["balance"]) if (bal_before and bal_after) else None
    expected = sum(values)

    problems = []
    if present < len(entries):
        problems.append("{} of {} contracts listed after the request".format(
            present, len(entries)))
    if spent is None or not rc.close_enough(spent, expected, tol=sc.cost_tolerance(expected)):
        problems.append("spent {:.4f}, expected {:.4f}".format(
            spent if spent is not None else -1, expected))
        if spent is not None and rc.close_enough(spent, values[0], tol=TOL * 4):
            problems.append("cost equals only the FIRST value - later iterations "
                            "of the pre-pass took no collateral")
    drift = db.new_drift(before, after)
    if drift:
        problems.append("counter drifted: " + db.describe_drift(drift) +
                        " - the per-iteration decrements do not compose")

    return (not problems), "3 contracts in one request ({}), spent {:.4f} of {:.4f} | {}".format(
        "+".join(str(v) for v in values), spent if spent is not None else -1,
        expected, db.format_evidence(before, after)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-11
# ---------------------------------------------------------------------------

def sc_c_11(ctx, ci):
    """
    SC-C-11 - Deploy a contract with no value, or a value of zero.

    WHAT IT CHECKS
        A deploy with value 0 takes NO collateral: the free balance is
        unchanged, nothing is committed, and the denomination counter does not
        move.

    WHY IT MATTERS
        The pre-pass opens with an explicit skip:

            if scInfo.Value <= 0 { continue }

        Nothing exercised that branch. It matters because it is the guard that
        keeps a valueless contract off the collateral path entirely - if it
        stopped working, every zero-value deploy would attempt a split for
        nothing, and LockTokensForSplit called with 0 has no defined useful
        behaviour.

        It is also the boundary either side of SC-C-01: 0 takes nothing, and
        the smallest positive value takes exactly itself.

    MANUAL STEPS
        Deploy with "value":0 and compare the balance and the denom listing
        before and after. Both must be identical.

    PASS / FAIL
        PASS  deploy succeeds and costs nothing, counter untouched
        FAIL  anything was deducted or committed -> the guard did not hold
        FAIL  rejected -> a zero-value contract should still deploy; it simply
              carries no collateral
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = sc._prepare(ctx, s, 6)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before = db.record("SC-C-11", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=0,
                                   data="zero-value deploy", port=ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-11", "after", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    d = db.delta(before, after)
    problems = []
    if not ok:
        problems.append("rejected: {} - a zero-value contract should deploy, it "
                        "simply carries no collateral".format(str(msg)[:80]))
    if abs(d["free"]) > TOL:
        problems.append("free balance moved {:+.4f} for a zero-value deploy - "
                        "the `if scInfo.Value <= 0 continue` guard did not "
                        "hold".format(d["free"]))
    if abs(d["committed"]) > TOL:
        problems.append("committed moved {:+.4f} for a zero-value deploy".format(
            d["committed"]))
    if db.new_drift(before, after):
        problems.append("counter drifted on a deploy that should not have "
                        "touched it: " + db.describe_drift(db.new_drift(before, after)))

    return (not problems), "zero-value deploy {}, free {:+.4f} committed {:+.4f}".format(
        "accepted" if ok else "REJECTED", d["free"], d["committed"]), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-C-30
# ---------------------------------------------------------------------------

def sc_c_30(ctx, ci):
    """
    SC-C-30 - Execute a contract deployed at exactly 1.0, where no split happened.

    WHAT IT CHECKS
        Deploy at exactly 1.0 - a whole token, so no split occurs - then
        EXECUTE that contract. The execute must succeed and the chain advance.

    WHY IT MATTERS
        When the collateral is exactly one whole denomination there is nothing
        to split, so CollectRBTTokens returns no childTokensKept, scGenTX stays
        nil, and PreviousTransactionID is left as the empty string.

        SC-C-04 proves that deploy costs 1.0. It does not prove the contract is
        usable afterwards. A contract whose genesis was written down the
        no-split path carries different chain linkage from one that was split,
        and the execute is the first operation that has to follow that linkage.

        This is the one branch of H1 the ladder touches but does not finish:
        deploy proven, execute-after-no-split not.

    MANUAL STEPS
        1. Deploy with "value":1.0 exactly.
        2. Read the chain:
             curl -s http://$SENDER:20000/rubix/v1/smart_contracts/<SC>/chain
        3. Execute the same contract.
        4. Read the chain again - it must be one entry longer.

    PASS / FAIL
        PASS  execute succeeds and the chain grows
        FAIL  execute rejected -> a contract deployed without a split cannot be
              used, which SC-C-04 would not have revealed
    """
    sc = _link()
    s, _ = ctx.pair(0)

    ready, why = sc._prepare(ctx, s, 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    # Exactly 1.0: a whole denomination, so the split path is skipped entirely.
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=1.0,
                                   data="no-split deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    okc, before_chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    before_len = len(before_chain) if okc else -1

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=1.0,
                                   data="execute after no-split deploy",
                                   port=ctx.port)
    if not ok:
        return False, "execute rejected after a no-split deploy", (
            "the contract deployed at exactly 1.0 (no split, so its genesis "
            "carries an empty PreviousTransactionID) cannot be executed: {} - "
            "SC-C-04 proves the deploy costs correctly but not that the "
            "contract is usable".format(str(msg)[:90]))

    grew, n = False, before_len
    for _ in range(12):
        time.sleep(2)
        okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
        n = len(chain) if okc else n
        if n > before_len:
            grew = True
            break

    return grew, "no-split deploy at 1.0, chain {} -> {}".format(before_len, n), (
        "" if grew else "execute returned success but the chain never advanced")


# ---------------------------------------------------------------------------
# SC-DB-03  (DB-SEED - writes to the database deliberately)
# ---------------------------------------------------------------------------

def sc_db_03(ctx, ci):
    """
    SC-DB-03 - Burn a denomination whose token_denom row has been deleted.

    WHAT IT CHECKS
        Delete the token_denom row for a denomination the wallet holds, then
        burn a token at that denomination via an FT mint. The product should
        either recreate the row or report an error - it must not silently
        proceed leaving the counter permanently wrong.

    WHY IT MATTERS
        The decrement 977f6fba introduced is a bare UPDATE:

            UPDATE token_denom SET count = GREATEST(count-1,0)
             WHERE did = $1 AND denom = $2

        With no matching row that affects ZERO rows and returns no error. The
        burn proceeds, the token leaves Free, and the counter never learns. It
        is the one shape of this bug that produces no drift at the moment it
        happens - the counter simply has no opinion about that denomination -
        and then under-counts forever.

        A missing row is reachable in normal operation: recovery re-derives
        token_denom (core/wallet/recovery.go:657) and a denomination with no
        Free tokens at that instant gets no row. This case creates that state
        directly rather than waiting to encounter it.

    DELIBERATE CORRUPTION - this case WRITES to the database.
        It records the row first and restores it in a finally block. Per the
        catalogue's DB-SEED rule these run LAST in a cycle, and a failure
        mid-case can still leave the node dirty - re-run GEN-IN-08 afterwards
        to confirm the wallet is clean.

    MANUAL STEPS
        1. Pick a denomination the wallet holds:
             SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
        2. Record it, then delete it:
             DELETE FROM token_denom WHERE did='<DID>' AND denom=<D>;
        3. Mint an FT backed by a token of that denomination.
        4. Re-read token_denom. Was the row recreated? Does it match reality?
        5. Restore the row you deleted.

    PASS / FAIL
        PASS  the row is recreated and matches the real free tokens, OR the
              mint is rejected with a clear error
        FAIL  the mint succeeds and the row is still absent -> the decrement
              silently did nothing and the counter is permanently wrong
        SKIP  psycopg2 missing, or no suitable denomination held
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    ready, why = sc._prepare(ctx, s, 10)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        counter = db.denom_counter(s["host"], s["did"])
        real = db.real_free_denoms(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    # A whole denomination the wallet actually holds, so an FT mint will burn it.
    # BUILD the precondition rather than skipping on it. The last run skipped
    # here because the wallet had been drained of whole tokens by an earlier
    # case - a state one round of funding fixes. Skipping on a fixable
    # precondition is how a branch stays unproven run after run while the
    # report shows nothing wrong.
    def _pick():
        for d in sorted(counter, reverse=True):
            if d >= 1.0 and real.get(d, 0) > 0:
                return d
        for d in sorted(counter, reverse=True):
            for rd, rc_ in real.items():
                if abs(rd - d) < 0.0015 and rc_ > 0 and rd >= 1.0:
                    return d
        return None

    target = _pick()
    if target is None:
        rc.fund_did(s["host"], s["did"], 5, ctx.port)
        rc.wait_for_balance(s["host"], s["did"], 5, ctx.port)
        time.sleep(SETTLE)
        try:
            counter = db.denom_counter(s["host"], s["did"])
            real = db.real_free_denoms(s["host"], s["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        target = _pick()
    if target is None:
        return SKIP, "no suitable denomination", (
            "needs a whole denomination present in both token_denom and the "
            "tokens table, and funding 5 RBT did not produce one")

    original = counter[target]
    restored = False
    try:
        db.writable_query(
            s["host"],
            "DELETE FROM token_denom WHERE did = %s AND denom = %s",
            (s["did"], target), i_understand_this_writes=True)

        ok, msg, _ = rc.mint_ft(s["host"], s["did"], "seed" + str(int(time.time()))[-6:],
                                5, 1, ctx.port)
        time.sleep(SETTLE * 2)

        after = db.denom_counter(s["host"], s["did"])
        real_after = db.real_free_denoms(s["host"], s["did"])
        row_back = any(abs(d - target) < 0.0015 for d in after)

        problems = []
        if ok and not row_back:
            problems.append(
                "the mint succeeded but token_denom still has no row for {:.3f}, "
                "while {} token(s) remain Free there - the bare UPDATE matched "
                "no row, returned no error, and the counter is now permanently "
                "wrong for this denomination".format(
                    target, real_after.get(target, 0)))
        elif ok and row_back:
            counted = next(c for d, c in after.items() if abs(d - target) < 0.0015)
            actual = real_after.get(target, 0)
            if counted != actual:
                problems.append("row recreated at {} but {} token(s) are Free "
                                "at {:.3f}".format(counted, actual, target))

        return (not problems), "deleted denom {:.3f} (was {}), mint {}, row {}".format(
            target, original, "ok" if ok else "rejected",
            "recreated" if row_back else "STILL MISSING"), "; ".join(problems)
    finally:
        # Restore whatever the row was, whether or not the case passed.
        try:
            db.writable_query(
                s["host"],
                "INSERT INTO token_denom (did, denom, count, created_at, updated_at) "
                "VALUES (%s, %s, %s, NOW(), NOW()) "
                "ON CONFLICT (did, denom) DO UPDATE SET count = EXCLUDED.count",
                (s["did"], target, original), i_understand_this_writes=True)
            restored = True
        except Exception:
            pass
        if not restored:
            sys.stderr.write(
                "[SC-DB-03] WARNING: could not restore token_denom row "
                "(did={}, denom={}, count={}) on {} - fix by hand before "
                "trusting later results\n".format(
                    s["did"][:16], target, original, s["host"]))
