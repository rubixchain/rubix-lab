#!/usr/bin/env python3
"""
wallet_shapes.py - build the wallet a case needs, instead of skipping.

WHY THIS EXISTS
    A run reported eight SKIPs reading "receiver already holds whole tokens".
    Every one of them was the harness giving up on a precondition it was
    perfectly able to create. A parts-only wallet is not a rare state that has
    to be found lying around - it is two transfers away from any wallet.

    Skipping on a buildable precondition is worse than failing: it silently
    removes coverage while the run still looks healthy. The FT-from-parts
    chain - a third of the FT verification for PR #739 - did not execute at
    all, and the summary said "8 skipped" rather than "a third of this suite
    was not tested".

HOW A PARTS WALLET IS BUILT
    1. Drain. Send the wallet's whole-token balance away to a sink DID. Draining
       to a round number leaves only what could not be expressed in whole
       tokens.
    2. Verify the drain, by reading the tokens table - not the balance, which
       cannot distinguish 2.0 held as one token from 2.0 held as four parts.
    3. Fund with fractional amounts only.
    4. Verify again, and report what the wallet actually holds.

    Every step is checked, because a case that believes it is testing the parts
    path while running against whole tokens is worse than one that skips.
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import rubix_client as rc
import db_client as db

SETTLE = 6
DRAIN_CHUNK = 50.0     # transfer at most this much per call, so one drain
                       # cannot exceed what a quorum will pledge


def describe(host, did, api_port=rc.DEFAULT_PORT):
    """Human-readable summary of what a wallet actually holds, from the DB.

    NOTE the DB reads below do NOT take a port. db_client owns the database
    port (5433); the API port (20000) is a different thing entirely, and
    passing one where the other belongs is exactly the mistake that made every
    wallet-shape check time out against the HTTP API.
    """
    try:
        values = db.free_token_values(host, did)
    except db.DBUnavailable as e:
        return "unreadable ({})".format(e)
    if not values:
        return "empty"
    wholes = [v for v in values if v >= 1.0]
    parts = [v for v in values if v < 1.0]
    return "{} row(s) totalling {:.3f}: {} whole, {} part{}".format(
        len(values), sum(values), len(wholes), len(parts),
        " (largest part {:.3f})".format(max(parts)) if parts else "")


def drain_whole_tokens(ctx, target, sink, port=None):
    """Send `target`'s whole-token value to `sink`. Returns (ok, note).

    Drains in chunks: a single transfer of a large balance can exceed what the
    quorum is willing to pledge, and that failure would look like a product
    defect rather than a setup limit.
    """
    api_port = port or ctx.port          # HTTP API - for rc.* calls only
    try:
        values = db.free_token_values(target["host"], target["did"])
    except db.DBUnavailable as e:
        return False, str(e)

    whole_total = sum(v for v in values if v >= 1.0)
    if whole_total < 1.0:
        return True, "nothing to drain"

    # Move whole units only, so whatever fractional dust exists is left behind.
    to_send = int(whole_total)
    sent = 0
    while sent < to_send:
        chunk = min(DRAIN_CHUNK, to_send - sent)
        ok, msg, _ = rc.initiate_transaction(
            target["host"], target["did"], sink["did"], rbt=float(chunk),
            memo="drain whole tokens for a parts wallet", port=api_port)
        if not ok:
            return False, "draining {} RBT failed at {}: {}".format(
                to_send, sent, msg)
        sent += chunk
        time.sleep(2)

    time.sleep(SETTLE)
    try:
        left = db.free_token_values(target["host"], target["did"])
    except db.DBUnavailable as e:
        return False, str(e)
    remaining_whole = [v for v in left if v >= 1.0]
    if remaining_whole:
        return False, ("drained {} RBT but {} whole token(s) remain - selection "
                       "kept them back".format(sent, len(remaining_whole)))
    return True, "drained {} RBT".format(sent)


def make_parts_wallet(ctx, target, sink, amounts=(0.4, 0.3, 0.5, 0.7, 0.5),
                      require_no_whole=True):
    """Leave `target` holding ONLY fractional RBT. Returns (ok, note).

    `sink` receives the drained whole tokens and must be a different, funded
    host. `amounts` are the fractional transfers that build the wallet back up;
    each is sent individually so none can be combined into a whole token.
    """
    api_port = ctx.port
    if not db.available():
        return False, "psycopg2 not installed - cannot verify wallet shape"

    ok, note = drain_whole_tokens(ctx, target, sink, api_port)
    if not ok:
        return False, "drain failed: " + note

    # The sink must be able to pay for what it now sends back.
    need = sum(amounts) + 2
    okb, detail, _ = rc.get_rbt_balance_detail(sink["host"], sink["did"], api_port)
    have = detail["balance"] if okb and detail else 0
    if have < need:
        rc.fund_did(sink["host"], sink["did"], int(need - have) + 2, api_port)
        rc.wait_for_balance(sink["host"], sink["did"], need, api_port)

    for amt in amounts:
        ok, msg, _ = rc.initiate_transaction(
            sink["host"], sink["did"], target["did"], rbt=amt,
            memo="build parts wallet", port=api_port)
        if not ok:
            return False, "sending {} failed: {}".format(amt, msg)
        time.sleep(2)
    time.sleep(SETTLE)

    try:
        values = db.free_token_values(target["host"], target["did"])
    except db.DBUnavailable as e:
        return False, str(e)

    wholes = [v for v in values if v >= 1.0]
    if require_no_whole and wholes:
        return False, ("built the wallet but {} whole token(s) are present - the "
                       "fractional transfers were combined".format(len(wholes)))
    if not values:
        return False, "wallet is empty after building"
    return True, "parts wallet ready: {}".format(
        describe(target["host"], target["did"], api_port))


def make_mixed_wallet(ctx, target, sink, whole=2.0,
                      amounts=(0.4, 0.3, 0.5, 0.7)):
    """Leave `target` holding BOTH whole and fractional tokens."""
    ok, note = make_parts_wallet(ctx, target, sink, amounts,
                                 require_no_whole=True)
    if not ok:
        return False, note
    ok, msg, _ = rc.initiate_transaction(sink["host"], sink["did"],
                                         target["did"], rbt=whole,
                                         memo="add whole tokens", port=ctx.port)
    if not ok:
        return False, "adding whole tokens failed: {}".format(msg)
    time.sleep(SETTLE)
    return True, "mixed wallet ready: {}".format(
        describe(target["host"], target["did"], ctx.port))


def make_minimum_unit_wallet(ctx, target, sink, count=20):
    """Leave `target` holding only tokens at the network minimum (0.001)."""
    return make_parts_wallet(ctx, target, sink, amounts=tuple([0.001] * count),
                             require_no_whole=True)


def drain_to(ctx, target, sink, keep=0.0):
    """Empty `target` down to `keep` RBT. Used where a case needs a wallet with
    a known small balance rather than a specific shape - FT-P-06 needs to reach
    the denomination floor, which it cannot do on a wallet that keeps being
    topped up."""
    api_port = ctx.port
    okb, detail, _ = rc.get_rbt_balance_detail(target["host"], target["did"], api_port)
    if not okb or not detail:
        return False, "balance unreadable"
    surplus = int(detail["balance"] - keep)
    if surplus < 1:
        return True, "already at or below {} RBT".format(keep)
    sent = 0
    while sent < surplus:
        chunk = min(DRAIN_CHUNK, surplus - sent)
        ok, msg, _ = rc.initiate_transaction(target["host"], target["did"],
                                             sink["did"], rbt=float(chunk),
                                             memo="drain to budget", port=api_port)
        if not ok:
            return False, "draining failed at {} of {}: {}".format(sent, surplus, msg)
        sent += chunk
        time.sleep(2)
    time.sleep(SETTLE)
    okb, detail, _ = rc.get_rbt_balance_detail(target["host"], target["did"], api_port)
    now = detail["balance"] if okb and detail else -1
    return True, "drained {} RBT, now holding {:.3f}".format(sent, now)


def second_did(ctx, entry, create=True):
    """Return a SECOND DID on `entry`'s host, or None.

    Idempotent by design: it reuses an existing extra DID and only creates one
    when the host has just the primary. CreateDID has no idempotency of its own
    (core/did.go:26) - calling it blindly each run would leave a machine with a
    growing pile of identities and no error to show for it.

    Returns None unless the host is tagged 'multidid' in hosts.txt. That guard
    is deliberate: a second DID on an untagged host breaks the one-DID-per-node
    invariant every controller tool relies on, and a test should never do that
    to a machine that did not opt in.
    """
    dids = entry.get("dids") or ([entry["did"]] if entry.get("did") else [])
    if len(dids) > 1:
        return dids[1]
    if not create:
        return None
    if entry.get("role") != "multidid":
        return None

    did, msg = rc.create_did(entry["host"], ctx.port)
    if not did:
        return None
    entry.setdefault("dids", list(dids)).append(did)
    rc.announce_did(entry["host"], did, ctx.port)
    time.sleep(SETTLE)
    return did
