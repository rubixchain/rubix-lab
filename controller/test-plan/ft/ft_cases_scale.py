#!/usr/bin/env python3
"""
ft_cases_scale.py - the FT burn path under sustained load.

Imported by ft_cases.py. Run via the pr-739-stress suite, after the functional
suite passes.

The functional FT cases mint twice. 977f6fba changed a statement that runs on
EVERY burn, so the interesting question is what a few hundred of them do to the
counter - and whether the two writers of token_denom stay consistent when both
are busy at once rather than taking turns.
"""

import os
import random
import string
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
    import ft_cases
    return ft_cases


def _name():
    return "sx" + "".join(random.choice(string.ascii_lowercase + string.digits)
                          for _ in range(8))


def _scale(ctx):
    return float(getattr(ctx.args, "scale", 1.0) or 1.0)


def _n(ctx, base, floor=3):
    return max(floor, int(base * _scale(ctx)))


# ---------------------------------------------------------------------------
# FT-X-01
# ---------------------------------------------------------------------------

def ft_x_01(ctx, ci):
    """
    FT-X-01 - Many FT mints with the counter checked at every checkpoint.

    WHAT IT CHECKS
        Mint FT batches continuously - 100 by default - re-checking the
        denomination counter every 10, and report the mint number at which it
        first disagreed with reality.

    WHY IT MATTERS
        977f6fba changed a statement that executes once per burnt RBT. The
        functional cases run it a handful of times; production runs it
        constantly. A decrement that is right 99% of the time still corrupts a
        wallet within a day.

        Reporting the FIRST bad checkpoint gives a number to compare between
        releases. It also distinguishes an immediate bug (fails at mint 10)
        from an accumulating one (fails at mint 80), which need different
        fixes.

    MANUAL STEPS
        Loop the FT mint, and every 10 iterations compare:
          SELECT denom, count FROM token_denom WHERE did='<DID>' ORDER BY denom;
          SELECT token_value, COUNT(*) FROM tokens
            WHERE did='<DID>' AND token_status=0 AND token_type=1
            GROUP BY token_value;

    PASS / FAIL
        PASS  every checkpoint consistent; burnt total matches RBT consumed
        FAIL  reports the first bad checkpoint and how far in it was
        RECORD  mints rejected for lack of backing are counted, not failed
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"
    s, _ = ctx.pair(0)

    total = _n(ctx, 100, floor=8)
    checkpoint = max(4, total // 10)
    backing = 1

    ready, why = ft._prepare(ctx, s, total * backing + 20)
    if not ready:
        return SKIP, "setup incomplete", why

    try:
        start = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    if start["denom_drift"]:
        return SKIP, "already drifting", "see GEN-IN-08"

    done, rejected, first_drift = 0, 0, None
    for i in range(1, total + 1):
        ok, _msg, _ = rc.mint_ft(s["host"], s["did"], _name(), 5, backing, ctx.port)
        if ok:
            done += 1
        else:
            rejected += 1
        if i % checkpoint == 0 and first_drift is None:
            time.sleep(1.5)
            try:
                now = db.snapshot(s["host"], s["did"])
            except db.DBUnavailable:
                continue
            d = db.new_drift(start, now)
            if d:
                first_drift = (i, db.describe_drift(d))
        time.sleep(0.4)

    time.sleep(SETTLE * 2)
    try:
        end = db.snapshot(s["host"], s["did"])
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)
    drift = db.new_drift(start, end)
    d = db.delta(start, end)

    problems = []
    if first_drift:
        problems.append("counter FIRST drifted at mint {} of {}: {}{}".format(
            first_drift[0], total, first_drift[1],
            " - early, so this is an immediate defect rather than accumulation"
            if first_drift[0] <= checkpoint * 2 else
            " - late, so this accumulates rather than failing outright"))
    elif drift:
        problems.append("counter drifted by the end: " + db.describe_drift(drift))

    burnt = d["burnt_for_ft"]
    consumed = -d["free"]
    if done and abs(burnt - consumed) > max(0.05, consumed * 0.01):
        problems.append("free fell {:.3f} but only {:.3f} was recorded as burnt "
                        "across {} mint(s) - {:.3f} unaccounted for".format(
                            consumed, burnt, done, abs(consumed - burnt)))

    note = "; ".join(problems)
    if rejected and not problems:
        note = "{} mint(s) rejected for lack of backing - expected once the " \
               "wallet runs low".format(rejected)

    return (not problems), "{}/{} minted, burnt {:.3f}, counter {}".format(
        done, total, burnt, "ok" if not drift else "DRIFT"), note


# ---------------------------------------------------------------------------
# FT-X-02
# ---------------------------------------------------------------------------

def ft_x_02(ctx, ci):
    """
    FT-X-02 - Sustained concurrent minting from several wallets at once.

    WHAT IT CHECKS
        Every host in the lane mints repeatedly and simultaneously. Afterwards
        every wallet's counter must still match its real free tokens.

    WHY IT MATTERS
        FT-P-07 races a mint against one transfer, once. This runs many mints
        from many DIDs continuously, which is what actually exercises whatever
        serialises the counter update - per-DID locking, transaction isolation,
        or nothing at all.

        Because each wallet is a different DID, a failure here is NOT the
        same-row contention FT-P-07 probes. It would mean the burn path
        interferes across DIDs, which would be a much broader defect.

    MANUAL STEPS
        Start the FT mint loop on several hosts at once, then check each
        wallet's counter against its real free tokens.

    PASS / FAIL
        PASS  every wallet consistent
        FAIL  the report names which wallets drifted - more than one points at
              a shared resource rather than a per-wallet bug
        SKIP  fewer than 2 hosts
    """
    ft = _link()
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    seen, hosts = set(), []
    for e in list(ctx.senders) + list(ctx.receivers):
        if e["host"] not in seen:
            seen.add(e["host"])
            hosts.append(e)
    if len(hosts) < 2:
        return SKIP, "need 2+ hosts", "lane has {}".format(len(hosts))

    per_host = _n(ctx, 25, floor=4)
    for e in hosts:
        ready, why = ft._prepare(ctx, e, per_host + 12)
        if not ready:
            return SKIP, "setup incomplete", "{}: {}".format(e["host"], why)

    try:
        before = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    def worker(e):
        ok_n, bad_n = 0, 0
        for _ in range(per_host):
            ok, _m, _ = rc.mint_ft(e["host"], e["did"], _name(), 5, 1, ctx.port)
            ok_n, bad_n = (ok_n + 1, bad_n) if ok else (ok_n, bad_n + 1)
            time.sleep(0.3)
        return e["host"], ok_n, bad_n

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = list(pool.map(worker, hosts))
    time.sleep(SETTLE * 3)

    try:
        after = {e["host"]: db.snapshot(e["host"], e["did"]) for e in hosts}
    except db.DBUnavailable as e:
        return SKIP, "database unreachable", str(e)

    drifted = []
    for h in before:
        d = db.new_drift(before[h], after[h])
        if d:
            drifted.append("{}: {}".format(h, db.describe_drift(d)))

    ok_total = sum(o for _h, o, _b in results)
    bad_total = sum(b for _h, _o, b in results)

    note = ""
    if drifted:
        note = "; ".join(drifted[:4])
        note += (" - more than one wallet drifted, which points at a shared "
                 "resource rather than a per-wallet bug"
                 if len(drifted) > 1 else
                 " - a single wallet drifted, so this is per-DID rather than shared")

    return (not drifted), "{} wallet(s) x {} mints: {} ok, {} rejected".format(
        len(hosts), per_host, ok_total, bad_total), note
