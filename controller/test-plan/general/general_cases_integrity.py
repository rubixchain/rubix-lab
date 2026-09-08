#!/usr/bin/env python3
"""
general_cases_integrity.py - fleet-wide database invariants.

Imported by general_cases.py.

These take no action. They read the fleet and assert things that must be true
regardless of what ran before - which makes them the cases that catch damage
nobody attributed to anything. Run them at the END of a cycle: a failure here
means one of the earlier cases broke something quietly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "full-test"))
import rubix_client as rc
import db_client as db

SKIP = "SKIP"


def _hosts(ctx):
    seen, out = set(), []
    for e in list(ctx.senders) + list(ctx.receivers) + list(ctx.quorum_hosts):
        if e["host"] not in seen:
            seen.add(e["host"])
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# GEN-IN-12
# ---------------------------------------------------------------------------

def gen_in_12(ctx, ci):
    """
    GEN-IN-12 - No denomination counter is negative, anywhere.

    WHAT IT CHECKS
        Across every host in this run, no token_denom row has count < 0.

    WHY IT MATTERS
        The FT burn fix floors the decrement deliberately:

            count = GREATEST(count - 1, 0)

        That floor exists because the old code could be called more than once
        for the same burn. A negative count is therefore the specific signature
        of that floor being bypassed or removed - and it is worse than drift,
        because selection would ask for a negative number of tokens and fail in
        a way that reads like corruption rather than a counter bug.

        Cheap to check and covers every DID on every host, not just the wallets
        this run happened to use.

    MANUAL STEPS
        On any node:
          SELECT did, denom, count FROM token_denom WHERE count < 0;
        Should return no rows, on every host.

    PASS / FAIL
        PASS  no negative counts anywhere
        FAIL  the report names host, DID and denomination
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, bad = 0, []
    for e in _hosts(ctx):
        try:
            rows = db.negative_denoms(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        for did, denom, count in rows:
            bad.append("{} did {} denom {:.3f} count {}".format(
                e["host"], did[:12], denom, count))

    if not checked:
        return SKIP, "no host reachable", "could not read token_denom anywhere"
    return (not bad), "{} host(s) checked, {} negative row(s)".format(checked, len(bad)), (
        "" if not bad else "; ".join(bad[:5]) +
        " - the burn path floors at zero deliberately, so a negative count "
        "means that floor was bypassed")


# ---------------------------------------------------------------------------
# GEN-IN-13
# ---------------------------------------------------------------------------

def gen_in_13(ctx, ci):
    """
    GEN-IN-13 - Every free token has a chain row.

    WHAT IT CHECKS
        Across every host, no Free RBT token exists without at least one
        `tokenchain` entry.

    WHY IT MATTERS
        A token with no chain is spendable-looking and unusable: it counts
        toward the balance and toward token_denom, so selection will pick it,
        and then validation fails because there is no history to check.

        The collateral split persists change tokens through
        PersistGenesisTransaction on a SEPARATE connection, deliberately before
        the outer transaction opens. A token row committed while its chain row
        was not is exactly the shape that path could produce if it half
        succeeded.

    MANUAL STEPS
        On any node:
          SELECT t.token_id FROM tokens t
           WHERE t.token_type=1 AND t.token_status=0
             AND NOT EXISTS (SELECT 1 FROM tokenchain c
                              WHERE c.token_id = t.token_id);

    PASS / FAIL
        PASS  no orphans on any host
        FAIL  the report names the host and sample token ids - those tokens
              will fail the next transfer that selects them
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, findings, total = 0, [], 0
    for e in _hosts(ctx):
        try:
            orphans = db.orphan_tokens(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        if orphans:
            total += len(orphans)
            findings.append("{}: {} orphan(s) e.g. {}".format(
                e["host"], len(orphans), ", ".join(t[:16] for t in orphans[:2])))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not findings), "{} host(s) checked, {} orphan token(s)".format(
        checked, total), (
        "" if not findings else "; ".join(findings[:4]) +
        " - these count toward the balance and the denomination counter, so "
        "selection will pick them and then fail validation")


# ---------------------------------------------------------------------------
# GEN-IN-14
# ---------------------------------------------------------------------------

def gen_in_14(ctx, ci):
    """
    GEN-IN-14 - No duplicate token ids on any node.

    WHAT IT CHECKS
        Across every host, no token_id appears more than once in `tokens`.

    WHY IT MATTERS
        A duplicate id within one node means the same token was persisted
        twice. The split path is the plausible source: it burns a parent and
        inserts children, and a retry that re-ran the insert without an
        ON CONFLICT guard would produce exactly this.

        This is also the local half of the collision problem the lab already
        knows about - two NODES minting the same index produce tokens a shared
        quorum cannot tell apart. This case catches the same shape within a
        single node, where it is unambiguously a bug rather than a
        configuration mistake.

    MANUAL STEPS
        On any node:
          SELECT token_id, COUNT(*) FROM tokens
           GROUP BY token_id HAVING COUNT(*) > 1;

    PASS / FAIL
        PASS  no duplicates anywhere
        FAIL  the report names host and token id
    """
    if not db.available():
        return SKIP, "database driver missing", "sudo apt install -y python3-psycopg2"

    checked, findings, total = 0, [], 0
    for e in _hosts(ctx):
        try:
            dupes = db.duplicate_token_ids(e["host"])
        except db.DBUnavailable:
            continue
        checked += 1
        if dupes:
            total += len(dupes)
            findings.append("{}: {} duplicate(s) e.g. {} x{}".format(
                e["host"], len(dupes), dupes[0][0][:20], dupes[0][1]))

    if not checked:
        return SKIP, "no host reachable", "could not read the tokens table anywhere"
    return (not findings), "{} host(s) checked, {} duplicate id(s)".format(
        checked, total), (
        "" if not findings else "; ".join(findings[:4]) +
        " - the same token persisted twice on one node, which the split path "
        "could produce on a retry without an ON CONFLICT guard")
