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


# ---------------------------------------------------------------------------
# SC-C-31
# ---------------------------------------------------------------------------

def sc_c_31(ctx, ci):
    """
    SC-C-31 - Is the collateral committed by a REJECTED deploy ever released?

    RUNS IMMEDIATELY AFTER SC-C-27, ON THE SAME HOST, BY DESIGN.
    SC-C-27 creates the condition; this case decides whether it is permanent.
    Run it alone and it measures a quiet wallet and passes, which is correct
    but tells you nothing - keep them in the same lane and order.

    WHAT IT CHECKS
        Read the host's Committed RBT, wait, read it again. Committed
        (token_status 5) is a TERMINAL state - there is no release path back to
        Free. If the value SC-C-27 stranded is still there after the network has
        had every chance to settle, it is lost, not in flight.

    WHY IT MATTERS
        This is the difference between a bug report and a blocker. SC-C-27
        showed ~1136 RBT moving to Committed for a deploy that was REJECTED:

            free 1150.438 -> 14.496   committed 420.342 -> 1556.284   locked 0->0

        "locked 0->0" already rules out the lock-release leak seen on .107 -
        those tokens are not waiting on anything. But a single reading taken
        seconds after the rejection cannot distinguish "lost" from "not yet
        cleaned up". A reviewer will ask exactly that, and the honest answer
        today is that nobody has waited and looked again.

        If the value does come back, this is a timing artefact and SC-C-27
        should be re-scoped. If it does not, the collateral pre-pass commits
        value before consensus and never unwinds it on failure - which is
        unrecoverable loss on a path the user cannot avoid, and blocks the PR.

    MANUAL STEPS
        1. Right after a rejected deploy, on the deployer host:
             SELECT ROUND(SUM(token_value)::numeric,3) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=5;
        2. Wait two minutes. Run it again.
        3. Also count contracts - a rejected deploy must not have created one:
             SELECT COUNT(*) FROM smart_contracts WHERE deployer_did='<DID>';

    PASS / FAIL
        PASS  committed FELL over the window - the value is being released and
              SC-C-27 is a timing artefact, not a loss
        FAIL  committed is unchanged - it is terminal, and the PR's pre-pass
              loses it on every rejected deploy
        SKIP  the host holds no committed RBT (SC-C-27 did not run first)
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    # Long enough that "still settling" is not a credible explanation. The
    # measured fleet timing is 1-2s for a receiver to credit and ~15s for a
    # node restart, so two minutes is an order of magnitude past anything
    # normal.
    window = 120

    try:
        first = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if first["committed"] < 1.0:
        return SKIP, "no committed RBT to observe", (
            "this host holds {:.3f} committed - SC-C-27 either did not run "
            "first or did not strand anything, so there is nothing to watch "
            "for release".format(first["committed"]))

    time.sleep(window)

    try:
        second = db.record("SC-C-31", "after-wait", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
        # token_type 4 is a smart contract - the same count GEN-IN-22 uses.
        rows = db.query(s["host"],
                        "SELECT COUNT(*) FROM tokens WHERE token_type = %s",
                        (db.TYPE_SC,))
        contracts = int(rows[0][0]) if rows else -1
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    released = first["committed"] - second["committed"]
    note = ""
    if released <= TOL:
        note = ("{:.3f} RBT is still Committed {}s after the rejection and has "
                "not moved. Committed is terminal - there is no path back to "
                "Free - so this value is permanently lost, for a deploy that "
                "was REJECTED. The collateral pre-pass commits before "
                "consensus and does not unwind on failure".format(
                    second["committed"], window))
        if contracts >= 0:
            note += "; the host holds {} contract(s) to account for it".format(contracts)

    return (released > TOL), "committed {:.3f} -> {:.3f} over {}s (released {:.3f})".format(
        first["committed"], second["committed"], window, released), note


# ---------------------------------------------------------------------------
# SC-C-32
# ---------------------------------------------------------------------------

def sc_c_32(ctx, ci):
    """
    SC-C-32 - Is the multi-contract rejection float accumulation, or is
    multi-contract simply unsupported?

    WHAT IT CHECKS
        Sends the SAME three-contract request twice with different values:

          EXACT   0.25, 0.5, 0.125   -> 0.875   every value exact in binary
          INEXACT 0.345, 0.359, 0.317 -> 1.021  none of them exact in binary

        The outcome pair is the answer.

    WHY IT MATTERS
        SC-C-29 failed with:

            "requestPledgeTokenHandler : transaction amount exceeds 3 decimal
             places"

        for 0.345+0.359+0.317. Every one of those is a legal 3dp value, and so
        is their sum. The only way a 3dp check rejects them is if the number it
        actually tested was not 1.021 - and summing those three in float64
        gives 1.0209999999999999, which has sixteen.

        That is a hypothesis, and SC-C-29 alone cannot distinguish it from "the
        API does not support multiple contracts in one request". Those are
        completely different findings: one is a precision bug worth fixing, the
        other is a documentation gap. Choosing between them by reading the
        error string is guessing.

        Binary-exact values settle it. 0.25, 0.5 and 0.125 are all sums of
        powers of two, so they accumulate with NO error at all - 0.875 exactly.
        If that triple is accepted and the inexact one is refused, the
        difference cannot be anything but the accumulation.

    MANUAL STEPS
        Post one /tx naming three contracts valued 0.25 / 0.5 / 0.125.
        Then post another naming three valued 0.345 / 0.359 / 0.317.
        Compare the two responses.

    PASS / FAIL
        PASS  both accepted - not reproducible on this build
        FAIL  exact ACCEPTED, inexact REFUSED -> float accumulation CONFIRMED.
              totalAmount += scInfo.Value needs a decimal-safe sum
        FAIL  both refused -> multi-contract is not supported at all; a
              different finding, and the builder's loop over contracts is
              misleading
    """
    sc = _link()
    s, _ = ctx.pair(0)

    exact = [0.25, 0.5, 0.125]        # 0.875 - representable exactly in float64
    inexact = [0.345, 0.359, 0.317]   # 1.021 - accumulates to 1.0209999999999999

    ready, why = sc._prepare(ctx, s, sum(exact) + sum(inexact) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    def send(values, label):
        entries = []
        for v in values:
            sc_id, err = sc._new_contract(ctx, s)
            if err:
                return None, "generation failed: " + err
            entries.append({"smartContractId": sc_id, "value": v,
                            "data": "SC-C-32 " + label})
        body = {
            "initiator": s["did"], "owner": "",
            "tokens": {"rbt": 0, "ft": [], "nft": [],
                       "smartContract": entries, "transferNftOwnership": False},
            "memo": "SC-C-32 " + label,
        }
        ok, msg, _ = rc._tx(s["host"], body, ctx.port)
        time.sleep(SETTLE)
        return ok, str(msg)

    ok_exact, msg_exact = send(exact, "binary-exact")
    if ok_exact is None:
        return SKIP, "could not build the exact request", msg_exact
    ok_inexact, msg_inexact = send(inexact, "binary-inexact")
    if ok_inexact is None:
        return SKIP, "could not build the inexact request", msg_inexact

    decimals = "3 decimal places" in msg_inexact

    if ok_exact and not ok_inexact:
        return False, "exact 0.875 ACCEPTED, inexact 1.021 REFUSED", (
            "FLOAT ACCUMULATION CONFIRMED. 0.25+0.5+0.125 are exact in binary "
            "and were accepted; 0.345+0.359+0.317 are not and were refused"
            + (" with the 3-decimal-places error" if decimals else "") +
            ". Both sums are legal 3dp values, so the only difference is the "
            "representation - totalAmount += scInfo.Value accumulates error "
            "and the precision guard then rejects a value the caller never "
            "sent. Refusal was: " + msg_inexact)
    if not ok_exact and not ok_inexact:
        return False, "both multi-contract requests refused", (
            "NOT a precision bug - even binary-exact values are refused, so "
            "multiple contracts in one request are not supported at all. That "
            "is a documentation gap rather than an arithmetic one, and the "
            "pre-pass looping over GetAllSmartContracts is misleading. "
            "Exact refusal: " + msg_exact)
    if ok_exact and ok_inexact:
        return True, "both accepted (0.875 and 1.021)", (
            "not reproducible on this build - SC-C-29's failure did not recur")
    return False, "exact REFUSED but inexact accepted", (
        "the opposite of the hypothesis, and not explainable by accumulation. "
        "Exact refusal: " + msg_exact)


# ---------------------------------------------------------------------------
# SC-C-33
# ---------------------------------------------------------------------------

def sc_c_33(ctx, ci):
    """
    SC-C-33 - What does a SUCCESSFUL deploy do to Committed?

    THE CONTROL FOR SC-C-27 / SC-C-31. Cheap, small value, no quorum
    out-pledging. It answers the one question those two cannot: is moving
    collateral into Committed the FAILURE behaviour, or the NORMAL behaviour
    that the failure path wrongly reuses?

    WHAT IT CHECKS
        Deploy a small contract that SUCCEEDS. Then:
          a) did Committed rise by exactly the contract value?
          b) after a wait, is it still there?

    WHY IT MATTERS
        SC-C-27 showed ~1136 RBT going to Committed for a REJECTED deploy, and
        SC-C-31 decides whether it ever comes back. Both measure only the
        failure path, and a reviewer reading them alone can reasonably answer:
        "collateral is supposed to be committed - that is what collateral IS".

        Without this control the finding has to be stated as "value moved to
        Committed", which sounds like correct behaviour. With it the finding
        becomes precise:

            a successful deploy commits the collateral and gets a contract
            for it; a rejected deploy commits the SAME value and gets
            nothing

        That is a one-line statement a developer can act on, and it points
        straight at the pre-pass running before the consensus outcome is known
        rather than after it.

        The second reading matters just as much. If Committed is released on a
        successful deploy, then a release path EXISTS and SC-C-31's failure to
        observe one is a rejection-only defect. If it is terminal on success
        too, then Committed is terminal by design and the loss on rejection is
        unrecoverable by construction. Those are different severities and
        nothing currently distinguishes them.

    MANUAL STEPS
        1. On the deployer, before:
             SELECT ROUND(SUM(token_value)::numeric,3) FROM tokens
              WHERE did='<DID>' AND token_type=1 AND token_status=5;
        2. Deploy one contract at a small value that will succeed.
        3. Read it again immediately, and once more after two minutes.
        4. The rise should equal the contract value, exactly.

    PASS / FAIL
        PASS  deploy succeeded and Committed rose by exactly the value - the
              normal path is well-defined, and SC-C-27 can be reported as the
              failure path copying it
        FAIL  Committed rose by something other than the value -> the
              accounting is wrong even on the success path, which is a larger
              finding than SC-C-27
        FAIL  the deploy was rejected - no control was established
        SKIP  no database, or the wallet could not be funded

        Either way the note records whether the committed value was released,
        because that is what sets SC-C-27's severity.
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    value = sc.rand_value(0.100, 0.500)
    ready, why = sc._prepare(ctx, s, value + 8)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err

    try:
        before = db.record("SC-C-33", "before", s["host"], s["did"],
                           db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="SC-C-33 success control", port=ctx.port)
    time.sleep(SETTLE * 2)

    try:
        after = db.record("SC-C-33", "after deploy", s["host"], s["did"],
                          db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    if not ok:
        return False, "the control deploy at {:.3f} was REJECTED".format(value), (
            "this case exists to establish what a SUCCESSFUL deploy does, and "
            "no success was obtained - so SC-C-27 has no control to be read "
            "against. Check the quorum's free balance before trusting the "
            "rollback lane at all. Rejection was: " + str(msg))

    committed = after["committed"] - before["committed"]

    # The same window SC-C-31 uses, so the two readings are comparable.
    window = 120
    time.sleep(window)
    try:
        settled = db.record("SC-C-33", "after wait", s["host"], s["did"],
                            db.snapshot(s["host"], s["did"]))
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    released = after["committed"] - settled["committed"]

    if released > TOL:
        release = ("{:.3f} of it was released within {}s, so a release path "
                   "DOES exist - which makes SC-C-31 observing none on the "
                   "rejected path a rejection-only defect".format(released, window))
    else:
        release = ("none of it was released within {}s, so Committed is "
                   "terminal on the success path too - the value SC-C-27 "
                   "stranded is unrecoverable by construction, not merely "
                   "uncollected".format(window))

    problems = []
    if not rc.close_enough(committed, value, tol=TOL * 4):
        problems.append(
            "a SUCCESSFUL deploy of {:.3f} committed {:.4f} - the two must be "
            "equal, and they are not. The collateral accounting is wrong on "
            "the normal path, which is a larger finding than the rejected "
            "path".format(value, committed))

    return (not problems), "deploy {:.3f} ok, committed +{:.4f}, {:.4f} still committed after {}s".format(
        value, committed, settled["committed"] - before["committed"], window), (
        "; ".join(problems + [release]))


# ---------------------------------------------------------------------------
# SC-C-34
# ---------------------------------------------------------------------------

def sc_c_34(ctx, ci):
    """
    SC-C-34 - At what N does a multi-contract request start failing, and is the
    guard reading the SUM or the individual values?

    THE LADDER BEHIND SC-C-32. SC-C-32 compares one exact triple against one
    inexact triple and can only say "accumulation or not". This walks the
    count, so the report can name the boundary and rule out the two
    alternative explanations that a single comparison leaves open.

    WHAT IT CHECKS
        Three requests, in this order:

          N=1   0.345                    one inexact value, alone
          N=2   0.1 + 0.2   -> 0.3       the canonical float64 failure:
                                         0.1+0.2 == 0.30000000000000004
          N=2   0.25 + 0.5  -> 0.75      both exact in binary, same count

        Every value and every sum is a legal 3dp amount, so a correct
        implementation accepts all three.

    WHY IT MATTERS
        SC-C-29 failed with "transaction amount exceeds 3 decimal places" for
        three inexact values. Three explanations survive that one observation:

          1. the sum accumulates float error and the guard tests the sum
          2. the guard tests each value and 0.345 alone is already rejected
          3. multiple contracts in one request are simply not supported

        N=1 kills (2): if 0.345 alone is accepted, the individual values are
        not the problem. Holding N at 2 while swapping exact for inexact kills
        (3): if the count were the problem, both N=2 requests would fail
        together. What is left is (1), and it is then confirmed on the single
        most recognisable example in floating point - 0.1 + 0.2 - which is
        worth more in a PR comment than any arbitrary triple.

        It also pins the boundary. If N=2 already fails, the fix is not an edge
        case at three contracts; it is every multi-contract request that does
        not happen to use binary-exact values, which is nearly all of them.

    MANUAL STEPS
        Post three separate /tx calls naming 1, then 2, then 2 contracts with
        the values above, and record each response verbatim. In Go:
            var t float64; for _, v := range []float64{0.1, 0.2} { t += v }
            fmt.Println(t)   // 0.30000000000000004

    PASS / FAIL
        PASS  all three accepted - not reproducible on this build
        FAIL  N=1 ok, 0.1+0.2 REFUSED, 0.25+0.5 accepted -> accumulation
              confirmed at N=2, on the sum, not the values
        FAIL  N=1 ok, both N=2 refused -> the count is the limit, not the
              arithmetic; SC-C-32's reading should be re-stated
        FAIL  N=1 refused -> the guard rejects a single legal 3dp value and
              this is not about multi-contract at all
        SKIP  setup incomplete
    """
    sc = _link()
    s, _ = ctx.pair(0)

    single = [0.345]
    inexact = [0.1, 0.2]     # 0.30000000000000004 accumulated in float64
    exact = [0.25, 0.5]      # 0.75, exact - same count, no accumulation

    ready, why = sc._prepare(ctx, s, sum(single) + sum(inexact) + sum(exact) + 10)
    if not ready:
        return SKIP, "setup incomplete", why

    def send(values, label):
        entries = []
        for v in values:
            sc_id, err = sc._new_contract(ctx, s)
            if err:
                return None, "generation failed: " + err
            entries.append({"smartContractId": sc_id, "value": v,
                            "data": "SC-C-34 " + label})
        body = {
            "initiator": s["did"], "owner": "",
            "tokens": {"rbt": 0, "ft": [], "nft": [],
                       "smartContract": entries, "transferNftOwnership": False},
            "memo": "SC-C-34 " + label,
        }
        ok, msg, _ = rc._tx(s["host"], body, ctx.port)
        time.sleep(SETTLE)
        return ok, str(msg)

    ok_one, msg_one = send(single, "N=1 0.345")
    if ok_one is None:
        return SKIP, "could not build the N=1 request", msg_one
    ok_in, msg_in = send(inexact, "N=2 0.1+0.2")
    if ok_in is None:
        return SKIP, "could not build the inexact N=2 request", msg_in
    ok_ex, msg_ex = send(exact, "N=2 0.25+0.5")
    if ok_ex is None:
        return SKIP, "could not build the exact N=2 request", msg_ex

    summary = "N=1 {}, N=2 inexact {}, N=2 exact {}".format(
        "ok" if ok_one else "REFUSED",
        "ok" if ok_in else "REFUSED",
        "ok" if ok_ex else "REFUSED")

    if not ok_one:
        return False, summary, (
            "a SINGLE contract at 0.345 - a legal 3dp value - was refused, so "
            "this is not a multi-contract or accumulation finding at all. "
            "SC-C-29 and SC-C-32 should both be re-read against this. "
            "Refusal: " + msg_one)

    if ok_one and not ok_in and ok_ex:
        return False, summary, (
            "FLOAT ACCUMULATION CONFIRMED ON THE SUM, AT N=2. 0.345 alone is "
            "accepted, so individual values are not the problem; 0.25+0.5 at "
            "the same count is accepted, so the count is not the problem. "
            "Only 0.1+0.2 fails, and 0.1+0.2 is exactly 0.30000000000000004 "
            "in float64. The guard is testing an accumulated sum the caller "
            "never sent. This is not an edge case at three contracts - it is "
            "every multi-contract request whose values are not binary-exact. "
            "Refusal: " + msg_in)

    if ok_one and not ok_in and not ok_ex:
        return False, summary, (
            "the COUNT is the limit, not the arithmetic: 0.345 alone is "
            "accepted and both two-contract requests are refused, including "
            "the binary-exact one that cannot accumulate any error. "
            "Multi-contract is unsupported from N=2 upward and SC-C-32's "
            "reading should be re-stated. Inexact refusal: " + msg_in +
            " | exact refusal: " + msg_ex)

    if ok_one and ok_in and ok_ex:
        return True, summary, (
            "all three accepted - the multi-contract refusal seen in SC-C-29 "
            "did not reproduce on this build")

    return False, summary, (
        "an outcome no hypothesis predicts - the exact pair was refused while "
        "the inexact pair was accepted. Record both responses verbatim before "
        "drawing any conclusion. Exact refusal: " + msg_ex)
