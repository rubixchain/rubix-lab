#!/usr/bin/env python3
"""
sc_cases_scale.py - the collateral and denomination paths under production-level load.

Imported by sc_cases.py. Run via the pr-739-stress suite, AFTER the functional
suite passes - at this volume a single rejection tells you nothing, because you
cannot separate a real defect from ordinary contention unless you already know
the basics are sound.

WHAT CHANGES AT SCALE
    The functional cases ask "is this correct?". These ask "is it STILL correct
    after five hundred of them?" - which is a different question, because the
    failures that matter here are the ones that accumulate:

      * a counter that drifts by one row per thousand operations
      * a split that loses 0.001 every few hundred deploys
      * a subscriber that falls behind once the chain is deep enough
      * a quorum whose pledges outrun its releases under sustained load

    None of those are visible in a five-operation test, and all of them take a
    fleet down eventually.

HOW THESE ARE WRITTEN DIFFERENTLY
    1. CHECKPOINTS. The invariant is re-checked every N operations, and the
       result reports the operation count at which it FIRST broke. "Drifted at
       operation 340" is a regression signal you can compare between releases;
       "drifted" is not.
    2. Individual failures are counted, not fatal. A pledge shortage at high
       concurrency is a capacity result. A broken invariant is a defect. The
       cases keep those separate and say which they found.
    3. Aggregate reconciliation matters more than any single operation. The
       real question at the end is whether the books still balance.
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

# Scaled by --scale so a smoke run and a full run use the same code path.
# case_runner passes args through; a lane can set its own via ctx.args.
DEFAULT_SCALE = 1.0


def _link():
    import sc_cases
    return sc_cases


def _scale(ctx):
    return float(getattr(ctx.args, "scale", DEFAULT_SCALE) or DEFAULT_SCALE)


def _n(ctx, base, floor=3):
    return max(floor, int(base * _scale(ctx)))


def _hosts(ctx):
    seen, out = set(), []
    for e in list(ctx.senders) + list(ctx.receivers):
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# SC-X-01
# ---------------------------------------------------------------------------

def sc_x_01(ctx, ci):
    """
    SC-X-01 - Sustained deploys with the counter checked at every checkpoint.

    WHAT IT CHECKS
        Deploy contracts continuously from one wallet - 200 by default - and
        re-check the denomination counter every 25. Reports the operation count
        at which the counter FIRST disagreed with reality, and the total value
        drift across the whole run.

    WHY IT MATTERS
        A split that loses 0.001 occasionally is invisible in a five-deploy
        test and fatal over a day of production. So is a counter that drifts by
        one row per few hundred operations: nothing fails at the time, and then
        selection starts failing for reasons that look unrelated.

        The checkpoint is the point. "Drifted at deploy 340" is a number you
        can compare against the next release. "Drifted" is not.

    MANUAL STEPS
        Loop the SC-C-01 deploy, and every 25 iterations run:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;
        Note the iteration number the two first disagree at.

    PASS / FAIL
        PASS  every checkpoint consistent, and total spend matches the sum of
              the values within tolerance
        FAIL  reports the FIRST checkpoint that drifted - the earlier it is,
              the more serious
        RECORD  deploys that failed for capacity reasons are counted separately
              and do not fail the case
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    total = _n(ctx, 200, floor=10)
    checkpoint = max(5, total // 8)
    values = [sc.rand_value(0.010, 0.120) for _ in range(total)]

    ready, why = sc._prepare(ctx, s, sum(values) + 15)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    done, rejected, first_drift = 0, [], None
    for i, v in enumerate(values, 1):
        sc_id, err = sc._new_contract(ctx, s)
        if err:
            rejected.append((i, "generation: " + str(err)[:50]))
            continue
        ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id, value=v,
                                       data="scale deploy {}".format(i), port=ctx.port)
        if ok:
            done += 1
        else:
            rejected.append((i, str(msg)[:60]))
        if i % checkpoint == 0 and first_drift is None:
            time.sleep(2)
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            if db.new_drift(start, now):
                first_drift = (i, db.describe_drift(db.new_drift(start, now)))

    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)
    d = db.delta(start, end)

    spent = -d["free"]
    expected = sum(values[:done]) if done < total else sum(values)
    value_gap = abs(spent - expected)

    problems = []
    if first_drift:
        problems.append("counter FIRST drifted at deploy {} of {}: {}".format(
            first_drift[0], total, first_drift[1]))
    elif drift:
        problems.append("counter drifted by the end of the run: "
                        + db.describe_drift(drift))
    if value_gap > max(0.01, expected * 0.001):
        problems.append("spent {:.4f} for {} deploy(s) worth {:.4f} - {:.4f} "
                        "unaccounted for across the run".format(
                            spent, done, expected, value_gap))

    note = "; ".join(problems)
    if rejected and not problems:
        note = "{} deploy(s) rejected under load (capacity, not correctness); " \
               "first: {}".format(len(rejected), rejected[0][1])

    return (not problems), "{}/{} deployed, spent {:.3f}, counter {}".format(
        done, total, spent, "ok" if not drift else "DRIFT"), note


# ---------------------------------------------------------------------------
# SC-X-02
# ---------------------------------------------------------------------------

def sc_x_02(ctx, ci):
    """
    SC-X-02 - One contract executed hundreds of times; chain stays sound.

    WHAT IT CHECKS
        Execute a single contract 200 times by default, checking every 25 that
        the chain length still equals the number of successful executes plus
        the deploy.

    WHY IT MATTERS
        Chain depth is the thing production accumulates that a test never does.
        A duplicate entry, a dropped entry, or a chain whose length stops
        tracking reality are all invisible at depth 3 and unmissable at depth
        200 - and a chain that drifts from the operation count means the
        history is no longer a reliable record of what happened.

        It is also the precondition for SC-X-03: a late subscriber can only be
        tested against a deep chain if a deep chain can be built at all.

    MANUAL STEPS
        Deploy once, then execute in a loop, and every 25 iterations:
          curl -s http://$SENDER:20000/rubix/v1/smart_contracts/<SC>/chain
        The entry count should equal successful executes + 1.

    PASS / FAIL
        PASS  chain length tracks the operation count throughout
        FAIL  reports the checkpoint where they first diverged, and by how much
              - ahead means duplicates, behind means dropped entries
    """
    sc = _link()
    s, _ = ctx.pair(0)
    total = _n(ctx, 200, floor=10)
    checkpoint = max(5, total // 8)

    ready, why = sc._prepare(ctx, s, 20)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.100),
                                   data="scale deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    done, rejected, first_gap = 0, 0, None
    for i in range(1, total + 1):
        ok, _msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                        value=sc.rand_value(0.005, 0.050),
                                        data="scale execute {}".format(i),
                                        port=ctx.port)
        if ok:
            done += 1
        else:
            rejected += 1
        if i % checkpoint == 0 and first_gap is None:
            time.sleep(2)
            okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
            if okc:
                expect = done + 1
                if len(chain) != expect:
                    first_gap = (i, len(chain), expect)

    time.sleep(SETTLE * 2)
    okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    final, expect = (len(chain) if okc else -1), done + 1

    problems = []
    if first_gap:
        i, got, want = first_gap
        problems.append("chain first diverged at execute {}: {} entries for {} "
                        "operations ({})".format(
                            i, got, want,
                            "duplicates" if got > want else "dropped entries"))
    elif final != expect:
        problems.append("chain ended at {} entries for {} operations ({})".format(
            final, expect, "duplicates" if final > expect else "dropped entries"))

    return (not problems), "{}/{} executed, chain {} (expected {})".format(
        done, total, final, expect), (
        "; ".join(problems) if problems else
        ("{} execute(s) rejected under load".format(rejected) if rejected else ""))


# ---------------------------------------------------------------------------
# SC-X-03
# ---------------------------------------------------------------------------

def sc_x_03(ctx, ci):
    """
    SC-X-03 - Subscribers joining continuously throughout a long execute run.

    WHAT IT CHECKS
        While a contract is executed repeatedly, every spare host subscribes at
        a different point in the run. At the end all of them must hold the same
        chain as the owner.

    WHY IT MATTERS
        The functional subscription cases join at depths 0 to 9 with four
        nodes. This joins across the whole fleet at depths spanning hundreds,
        while executes are still arriving - which is what a production network
        actually looks like when a node comes online.

        Two failure modes only appear here: back-fill that works at shallow
        depth but times out at deep, and a subscriber whose live events and
        back-fill overlap while the chain is still being written.

        The report groups any failures by JOIN DEPTH, so the answer to "does
        lateness cause it?" is in the result rather than a follow-up run.

    MANUAL STEPS
        Deploy, then execute in a loop. Every N executes subscribe another
        node, recording the depth at which it joined. At the end compare every
        node's chain against the owner's.

    PASS / FAIL
        PASS  every subscriber matches the owner, whatever depth it joined at
        FAIL  results are grouped by join depth so the pattern is visible - a
              clean cutoff means a back-fill limit, scattered failures mean a
              delivery race
        SKIP  fewer than 3 spare hosts
    """
    sc = _link()
    s = ctx.senders[0]
    spare = [e for e in ctx.receivers if e["host"] != s["host"]]
    if len(spare) < 3:
        return SKIP, "not enough hosts", (
            "lane has {} spare host(s); scaled subscription needs at "
            "least 3".format(len(spare)))

    total = _n(ctx, 150, floor=12)
    ready, why = sc._prepare(ctx, s, 20)
    if not ready:
        return SKIP, "setup incomplete", why

    sc_id, err = sc._new_contract(ctx, s)
    if err:
        return SKIP, "generation failed", err
    ok, msg, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                   value=sc.rand_value(0.010, 0.100),
                                   data="scale subs deploy", port=ctx.port)
    if not ok:
        return False, "deploy rejected", str(msg)
    time.sleep(SETTLE)

    every = max(1, total // (len(spare) + 1))
    joined, done = [], 0
    for i in range(1, total + 1):
        ok, _m, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                      value=sc.rand_value(0.005, 0.040),
                                      data="scale subs exec {}".format(i),
                                      port=ctx.port)
        if ok:
            done += 1
        if i % every == 0 and len(joined) < len(spare):
            node = spare[len(joined)]
            rc.subscribe_smart_contract(node["host"], sc_id, ctx.port)
            joined.append((node, done))

    time.sleep(SETTLE * 3)
    okc, chain, _ = rc.get_sc_chain(s["host"], sc_id, ctx.port)
    owner_len = len(chain) if okc else -1

    behind = []
    for node, depth in joined:
        okn, c, _ = rc.get_sc_chain(node["host"], sc_id, ctx.port)
        got = len(c) if okn else -1
        if got != owner_len:
            behind.append((depth, node["host"], got))

    note = ""
    if behind:
        depths = [d for d, _h, _g in behind]
        ok_depths = [d for _n2, d in joined if d not in depths]
        shallow_ok = ok_depths and max(ok_depths) < min(depths)
        note = ("; ".join("joined at depth {} ({}): {} of {}".format(
            d, h, g, owner_len) for d, h, g in behind[:5]))
        note += (" - every failure is at a GREATER depth than every success, "
                 "which points at a back-fill limit rather than a delivery race"
                 if shallow_ok else
                 " - failures are scattered across depths, which points at a "
                 "delivery race rather than a depth limit")

    return (not behind), "{} execute(s), {} subscriber(s) joined at depths {}".format(
        done, len(joined), ",".join(str(d) for _n3, d in joined)), note


# ---------------------------------------------------------------------------
# SC-X-04
# ---------------------------------------------------------------------------

def sc_x_04(ctx, ci):
    """
    SC-X-04 - Every host deploying repeatedly and concurrently.

    WHAT IT CHECKS
        Every host in the lane runs its own deploy loop at the same time.
        Afterwards every wallet's counter, and every quorum's, must still match
        reality.

    WHY IT MATTERS
        The closest this suite gets to a production moment: many independent
        wallets deploying through a small number of shared quorums, sustained
        rather than a single burst. It puts pressure on the two things that
        only fail under exactly these conditions - quorum pledge capacity, and
        concurrent writes to the denomination counter from many DIDs at once.

        Rejections are expected and counted. A wallet or quorum whose books no
        longer balance is not.

    MANUAL STEPS
        Run the SC-C-01 deploy loop simultaneously on every host, then check
        each wallet's and each quorum's denom listing against its real free
        tokens.

    PASS / FAIL
        PASS  every wallet and quorum consistent afterwards
        RECORD  the rejection count and rate - a capacity result
        FAIL  any counter inconsistent -> concurrent load corrupts the books
        SKIP  fewer than 3 hosts
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    hosts = _hosts(ctx)
    if len(hosts) < 3:
        return SKIP, "need 3+ hosts", "lane has {}".format(len(hosts))

    per_host = _n(ctx, 15, floor=3)
    for e in hosts:
        ready, why = sc._prepare(ctx, e, per_host * 0.2 + 8)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    try:
        w_before = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
        q_before = {q["host"]: db.snapshot(q["host"], q["did"])
                    for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def worker(e):
        ok_n, bad_n = 0, 0
        for i in range(per_host):
            sc_id, err = sc._new_contract(ctx, e)
            if err:
                bad_n += 1
                continue
            ok, _m, _ = rc.sc_transaction(e["host"], e["did"], sc_id,
                                          value=sc.rand_value(0.010, 0.080),
                                          data="fleet scale {}".format(i),
                                          port=ctx.port)
            ok_n, bad_n = (ok_n + 1, bad_n) if ok else (ok_n, bad_n + 1)
        return e["host"], ok_n, bad_n

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = list(pool.map(worker, hosts))
    time.sleep(SETTLE * 3)

    try:
        w_after = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
        q_after = {q["host"]: db.snapshot(q["host"], q["did"])
                   for q in ctx.quorum_hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    problems = []
    for h in w_before:
        drift = db.new_drift(w_before[h], w_after[h])
        if drift:
            problems.append("wallet {} drifted: {}".format(h, db.describe_drift(drift)))
    for h in q_before:
        drift = db.new_drift(q_before[h], q_after[h])
        if drift:
            problems.append("QUORUM {} drifted: {}".format(h, db.describe_drift(drift)))

    ok_total = sum(o for _h, o, _b in results)
    bad_total = sum(b for _h, _o, b in results)
    attempted = ok_total + bad_total

    note = "; ".join(problems)
    if bad_total and not problems:
        note = ("{} of {} deploys rejected ({:.0f}%) at {} concurrent wallets - "
                "a capacity result, not a correctness one".format(
                    bad_total, attempted, 100.0 * bad_total / max(1, attempted),
                    len(hosts)))

    return (not problems), "{} wallet(s) x {} deploys: {} ok, {} rejected".format(
        len(hosts), per_host, ok_total, bad_total), note


# ---------------------------------------------------------------------------
# SC-X-05
# ---------------------------------------------------------------------------

def sc_x_05(ctx, ci):
    """
    SC-X-05 - Deploy and execute alternately for a sustained period.

    WHAT IT CHECKS
        Alternate deploy and execute continuously for several minutes,
        re-checking the counter periodically, and report the elapsed time and
        operation count at which anything first went wrong.

    WHY IT MATTERS
        Everything else here is bounded by a fixed operation count. This is
        bounded by TIME, which is what surfaces problems that depend on
        background work rather than on how many operations ran - unpledging
        falling behind, a queue that never drains, resources that accumulate.

        A fleet that passes every bounded test and degrades after four minutes
        of steady traffic is still broken, and only a duration-bounded case
        finds it.

    MANUAL STEPS
        Run a deploy/execute loop for the target duration, checking the counter
        every 30 seconds and noting when it first disagrees.

    PASS / FAIL
        PASS  counter consistent throughout the whole period
        FAIL  reports the elapsed seconds and operation count at first failure
              - the TIME matters as much as the count, since a time-dependent
              failure points at background work rather than the operations
    """
    sc = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    duration = max(60, int(180 * _scale(ctx)))
    ready, why = sc._prepare(ctx, s, 30)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    began = time.time()
    last_check = began
    ops, failures, first_bad = 0, 0, None
    live_sc = None

    while time.time() - began < duration:
        if live_sc is None or ops % 5 == 0:
            sc_id, err = sc._new_contract(ctx, s)
            if err:
                failures += 1
                time.sleep(1)
                continue
            ok, _m, _ = rc.sc_transaction(s["host"], s["did"], sc_id,
                                          value=sc.rand_value(0.010, 0.060),
                                          data="soak deploy", port=ctx.port)
            if ok:
                live_sc = sc_id
            else:
                failures += 1
        else:
            ok, _m, _ = rc.sc_transaction(s["host"], s["did"], live_sc,
                                          value=sc.rand_value(0.005, 0.030),
                                          data="soak execute", port=ctx.port)
            if not ok:
                failures += 1
        ops += 1

        if time.time() - last_check >= 30 and first_bad is None:
            last_check = time.time()
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            drift = db.new_drift(start, now)
            if drift:
                first_bad = (int(time.time() - began), ops, db.describe_drift(drift))
        time.sleep(0.5)

    elapsed = int(time.time() - began)
    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)

    problems = []
    if first_bad:
        problems.append("counter first drifted after {}s and {} operation(s): "
                        "{} - a time-dependent failure points at background "
                        "work such as unpledging, not at the operations "
                        "themselves".format(*first_bad))
    elif drift:
        problems.append("counter drifted by the end of {}s: {}".format(
            elapsed, db.describe_drift(drift)))

    return (not problems), "{}s sustained, {} operation(s), {} failure(s), counter {}".format(
        elapsed, ops, failures, "ok" if not drift else "DRIFT"), "; ".join(problems)
