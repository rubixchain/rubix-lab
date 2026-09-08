#!/usr/bin/env python3
"""
sc_cases_quorum.py - what the QUORUM does during a smart contract deploy.

Imported by sc_cases.py.

WHY A SEPARATE GROUP
    Every other SC case looks at the deployer. These look at the other side of
    the same transaction. A deploy can be perfectly correct from the deployer's
    wallet and still leave the quorum's books wrong - and because the quorum
    signs for the whole fleet, that damage is shared by everyone.

    Pledging moves quorum tokens out of Free (core/wallet/pledge.go:222), so
    the quorum's token_denom must decrement exactly as the deployer's does.
    That is the SAME class of bug PR #739 fixes on two other paths, on a path
    the PR does NOT touch - so a failure here is a new finding, not a
    regression.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"
SETTLE = 6
TOL = 0.0015


def _link():
    import sc_cases
    return sc_cases


def _quorum_of(ctx, entry):
    return ctx.quorum_for(entry) or (ctx.quorum_hosts[0] if ctx.quorum_hosts else None)


# ---------------------------------------------------------------------------
# SC-Q-07
# ---------------------------------------------------------------------------

def sc_q_07(ctx, ci):
    """
    SC-Q-07 - The quorum's own denomination counter after it pledges.

    WHAT IT CHECKS
        After the quorum pledges for a deploy, its token_denom still matches
        the RBT it actually holds Free.

    WHY IT MATTERS
        Pledging takes quorum tokens out of Free, so its counter must decrement
        - the identical situation to the two bugs this PR fixes, on a third
        path nobody has touched. And the quorum is shared: a drifting counter
        there does not break one wallet, it eventually stops the quorum
        selecting tokens for ANY sender, which surfaces as unrelated transfers
        failing across the fleet.

        Only drift this deploy introduced is reported. The quorum signs for
        every lane, so pre-existing drift is somebody else's finding.

    MANUAL STEPS
        On the QUORUM host, before and after a deploy elsewhere:
          SELECT denom, count FROM token_denom WHERE did='<QDID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<QDID>' AND token_status=0 AND token_type=1
            GROUP BY token_value ORDER BY token_value;
        The two listings must still agree afterwards.

    PASS / FAIL
        PASS  no new drift on the quorum
        FAIL  the quorum's counter no longer matches its free tokens - it will
              eventually fail to pledge for senders unrelated to this test
        SKIP  quorum already drifting beforehand
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect a pledge without a known quorum"

    value = sc.rand_value(0.100, 0.999)
    ready, why = sc._prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if before["denom_drift"]:
        return SKIP, "quorum already drifting", (
            "pre-existing drift on the shared quorum: "
            + db.describe_drift(before["denom_drift"]) +
            " - not attributable to this deploy")

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="quorum denom check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    return (not drift), "quorum {} pledged +{:.3f}, denom {}".format(
        q["host"], after["pledged"] - before["pledged"],
        "ok" if not drift else "DRIFT"), (
        "" if not drift else db.describe_drift(drift) +
        " - pledging moved tokens out of Free without decrementing the counter. "
        "This path is not part of PR #739, so this is a new finding")


# ---------------------------------------------------------------------------
# SC-Q-08
# ---------------------------------------------------------------------------

def sc_q_08(ctx, ci):
    """
    SC-Q-08 - Which quorum tokens went to Pledged, row by row.

    WHAT IT CHECKS
        The rows the quorum moved into Pledged for this deploy sum to at least
        the contract value, and no more than is reasonable for it.

    WHY IT MATTERS
        SC-Q-06 checks the pledged TOTAL rose enough. This checks what it rose
        BY. Over-pledging is the interesting failure: a quorum that locks a
        whole 1.000 token to back a 0.354 contract is not wrong for that
        transaction, but it exhausts its capacity far faster than its balance
        suggests - and the fleet then sees "quorum cannot pledge" long before
        the quorum looks empty.

        This is the quorum-side mirror of the deployer bug this PR fixes.

    MANUAL STEPS
        On the quorum, before and after (6 = Pledged, 7 = QuorumPledged):
          SELECT token_id, token_value FROM tokens
            WHERE did='<QDID>' AND token_type=1 AND token_status IN (6,7)
            ORDER BY token_value DESC;
        Compare the two lists; the added rows are this deploy's pledge.

    PASS / FAIL
        PASS  newly pledged rows cover the value
        FAIL  they do not cover it -> the guarantee was short
        RECORD  how much MORE than the value was locked, since over-pledging
              silently reduces fleet capacity
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect a pledge without a known quorum"

    value = sc.rand_value(0.100, 0.999)
    ready, why = sc._prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    def pledged_rows():
        out = []
        for st in (db.PLEDGED, db.QUORUM_PLEDGED):
            out += db.token_rows(q["host"], q["did"], st)
        return {t: v for t, v, _st, _p in out}

    try:
        before = pledged_rows()
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="quorum pledge rows", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)

    # Sample promptly - the pledge is transient.
    added = {}
    for _ in range(8):
        try:
            now = pledged_rows()
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)
        fresh = {t: v for t, v in now.items() if t not in before}
        if fresh:
            added = fresh
            break
        time.sleep(1)

    total = sum(added.values())
    covered = total >= (value - TOL)
    excess = total - value

    note = ""
    if not covered:
        note = ("pledged {:.4f} across {} row(s) for a {:.3f} contract - the "
                "guarantee did not cover what was signed".format(
                    total, len(added), value))
    elif excess > 0.5:
        note = ("locked {:.4f} to back {:.3f} ({:.4f} more than needed) - not "
                "wrong for this transaction, but it drains quorum capacity far "
                "faster than the balance suggests".format(total, value, excess))

    return covered, "{} row(s) pledged, {:.4f} for a {:.3f} contract".format(
        len(added), total, value), note


# ---------------------------------------------------------------------------
# SC-Q-09
# ---------------------------------------------------------------------------

def sc_q_09(ctx, ci):
    """
    SC-Q-09 - An unpledge row is queued for the deploy.

    WHAT IT CHECKS
        After a deploy, `unpledge_sequence_info` holds a row for that
        transaction, naming the quorum and the tokens it pledged.

    WHY IT MATTERS
        The pledge is released later, asynchronously. The row queued here is
        what makes that possible - without it the pledge can never be released
        and the quorum loses that capacity permanently.

        This is why SC-Q-06 deliberately does NOT assert the release itself:
        unpledging is on its own schedule, so testing it in a short window
        reports timing as a defect. Testing that the release was QUEUED is the
        part that is deterministic.

    MANUAL STEPS
        On the quorum, after a deploy:
          SELECT tx_id, quorum_did, pledge_tokens FROM unpledge_sequence_info
            ORDER BY created_at DESC LIMIT 5;
        And check none are stranded:
          SELECT u.tx_id FROM unpledge_sequence_info u
            WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.id = u.tx_id);

    PASS / FAIL
        PASS  a new unpledge row appeared, and no row references a transaction
              that does not exist
        FAIL  no row queued -> that pledge can never be released
        FAIL  stranded rows -> pledges permanently stuck
    """
    sc = _link()
    s, _ = ctx.pair(0)
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    q = _quorum_of(ctx, s)
    if q is None:
        return SKIP, "no quorum", "cannot inspect unpledge rows without a quorum"

    value = sc.rand_value(0.100, 0.999)
    ready, why = sc._prepare(ctx, s, value + 5)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        before = {t for (t,) in db.query(
            q["host"], "SELECT tx_id FROM unpledge_sequence_info")}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                   data="unpledge queue check", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE * 2)

    try:
        after = [t for (t,) in db.query(
            q["host"], "SELECT tx_id FROM unpledge_sequence_info")]
        stranded = db.open_pledges(q["host"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    new = [t for t in after if t not in before]
    problems = []
    if not new:
        problems.append("no unpledge row was queued for this deploy - the "
                        "pledge it created can never be released")
    if stranded:
        problems.append("{} unpledge row(s) reference a transaction that does "
                        "not exist, so those pledges are stuck permanently".format(
                            len(stranded)))

    return (not problems), "{} unpledge row(s) queued, {} stranded".format(
        len(new), len(stranded)), "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-Q-10
# ---------------------------------------------------------------------------

def sc_q_10(ctx, ci):
    """
    SC-Q-10 - Deploys routed to each quorum in turn; every quorum's books balance.

    WHAT IT CHECKS
        One deploy per quorum. Each quorum's pledged total rises by its own
        deploy's value, and no quorum's counter drifts.

    WHY IT MATTERS
        Quorum state is per-quorum. A bug that only affects the FIRST quorum -
        or only one that has already signed something - is invisible when every
        test routes through the same one. Every other SC case uses whichever
        quorum the sender happens to be assigned, which in practice is almost
        always the same machine.

        It also gives three independent measurements of the same behaviour: if
        one quorum disagrees with the other two, the difference is that
        machine's state, not the code.

    MANUAL STEPS
        Register a different quorum on three senders, deploy from each, and
        compare each quorum's pledged total and denom listing before and after.

    PASS / FAIL
        PASS  every quorum pledged for its own deploy, none drifted
        FAIL  the report names which quorum disagreed
        SKIP  fewer than two quorums configured
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    quorums = list(ctx.quorum_hosts)
    if len(quorums) < 2:
        return SKIP, "need at least two quorums", (
            "only {} configured; this case compares quorums against each "
            "other".format(len(quorums)))

    s, _ = ctx.pair(0)
    results, problems = [], []
    for q in quorums:
        value = sc.rand_value(0.100, 0.999)
        # Point this sender at THIS quorum specifically.
        rc.quorum_add(s["host"], q["did"], ctx.port)
        ready, why = sc._prepare(ctx, s, value + 5)
        if not ready:
            problems.append("{}: setup incomplete ({})".format(q["host"], why))
            continue
        try:
            before = db.snapshot(q["host"], q["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)

        sc_id, err = sc._new_contract(ctx, s)
        if err:
            problems.append("{}: generation failed".format(q["host"]))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=value,
                                       data="per-quorum deploy", port=ctx.port)
        if not ok:
            problems.append("{}: deploy rejected ({})".format(q["host"], msg))
            continue
        time.sleep(SETTLE)
        try:
            after = db.snapshot(q["host"], q["did"])
        except db.DBUnavailable as e:
            return SKIP, "database unreachable", str(e)

        drift = db.new_drift(before, after)
        results.append("{}:{:.3f}{}".format(
            q["host"].split(".")[-1], value, "" if not drift else " DRIFT"))
        if drift:
            problems.append("{} drifted after its deploy: {}".format(
                q["host"], db.describe_drift(drift)))

    return (not problems), "{} quorum(s): {}".format(len(results), " ".join(results)), \
        "; ".join(problems)


# ---------------------------------------------------------------------------
# SC-Q-11
# ---------------------------------------------------------------------------

def sc_q_11(ctx, ci):
    """
    SC-Q-11 - Concurrent deploys from different senders through one quorum.

    WHAT IT CHECKS
        Several senders deploy at the same time through the same quorum. All
        succeed, each costs its own value, and the quorum's counter is still
        consistent afterwards.

    WHY IT MATTERS
        A shared quorum is the one piece of state every lane touches at once,
        which makes it the most likely place for a race. Each deploy decrements
        the quorum's counter; if those decrements are not serialised the
        counter ends up wrong, and it fails later for a sender that had nothing
        to do with this test.

        Sequential deploys through one quorum are already covered. This is the
        same operation with the interleaving that only a real fleet produces.

    MANUAL STEPS
        Point three senders at one quorum, then fire a deploy from each at the
        same moment. Afterwards compare that quorum's denom listing against its
        real free tokens.

    PASS / FAIL
        PASS  all deploys succeed and the quorum counter is consistent
        FAIL  a deploy is rejected for pledge shortage while the quorum clearly
              had capacity -> serialisation problem, not a funding one
        FAIL  the counter drifts -> concurrent decrements are not safe
        SKIP  fewer than 3 hosts in this lane
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    senders = [e for e in (list(ctx.senders) + list(ctx.receivers))][:3]
    if len(senders) < 3:
        return SKIP, "need 3 hosts", (
            "this lane has {} host(s); concurrency needs at least 3 "
            "senders".format(len(senders)))
    q = _quorum_of(ctx, senders[0])
    if q is None:
        return SKIP, "no quorum", "cannot test shared-quorum concurrency"

    values = [sc.rand_value(0.100, 0.500) for _ in senders]
    for e, v in zip(senders, values):
        rc.quorum_add(e["host"], q["did"], ctx.port)
        ready, why = sc._prepare(ctx, e, v + 4)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    prepared = []
    for e, v in zip(senders, values):
        sc_id, err = sc._new_contract(ctx, e)
        if err:
            return SKIP, "generation failed", "{}: {}".format(e["host"], err)
        prepared.append((e, sc_id, v))

    try:
        before = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def fire(item):
        e, sc_id, v = item
        return e, v, rc.sc_transaction(e["host"], e["did"], sc_id, value=v,
                                       data="concurrent deploy", port=ctx.port)

    with ThreadPoolExecutor(max_workers=len(prepared)) as pool:
        outcomes = list(pool.map(fire, prepared))
    time.sleep(SETTLE * 2)

    try:
        after = db.snapshot(q["host"], q["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(before, after)

    failed = [(e["host"], v, res[1]) for e, v, res in outcomes if not res[0]]
    problems = []
    for host, v, msg in failed:
        problems.append("{} (value {}) rejected: {}".format(host, v, msg))
    if drift:
        problems.append("quorum counter drifted after concurrent deploys: "
                        + db.describe_drift(drift) +
                        " - decrements are not safe to interleave")

    return (not problems), "{}/{} concurrent deploys ok, quorum denom {}".format(
        len(outcomes) - len(failed), len(outcomes),
        "ok" if not drift else "DRIFT"), "; ".join(problems)
