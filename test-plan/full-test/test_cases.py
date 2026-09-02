#!/usr/bin/env python3
"""
test_cases.py - The actual smoke-test cases, kept separate from
smoke_test.py's common setup (pool/DID readiness/role assignment/quorum
setup/funding) on purpose: to add, remove, or change a case, edit ONLY this
file. smoke_test.py never needs to change for that.

Each case is a plain function (ctx, report) -> None that calls
report.record(step_name, fn, **params) one or more times. ctx carries
everything the common setup already prepared (see SmokeContext below) so a
case never has to re-derive hosts/roles/quorum assignment itself.

To add a new case: write a function with this signature, add it to
TEST_CASES at the bottom. Order in that list is execution order.
"""

import random
import string
import time

import rubix_client as rc


class SmokeContext:
    """Everything the common preflight in smoke_test.py already resolved,
    handed to every test case so cases never repeat that work."""

    def __init__(self, port, quorum_hosts, senders, receivers, sender_quorum, args):
        self.port = port
        self.quorum_hosts = quorum_hosts
        self.senders = senders
        self.receivers = receivers
        self.sender_quorum = sender_quorum  # {sender_host: quorum_entry}
        self.pairs = list(zip(senders, receivers))
        self.args = args  # argparse.Namespace: rbt_amount, ft_count, ft_token_count, ...


def random_bytes(n=64):
    return "".join(random.choices(string.ascii_letters + string.digits, k=n)).encode("utf-8")


# ---------------------------------------------------------------------------
# RBT
# ---------------------------------------------------------------------------
def run_rbt_transfers(ctx, report):
    """Send ctx.args.rbt_amount from every sender to its paired receiver."""
    for i, (s, r) in enumerate(ctx.pairs, 1):
        def do_rbt(s=s, r=r):
            ok_s0, bal_s0, _ = rc.get_rbt_balance(s["host"], s["did"], ctx.port)
            ok_r0, bal_r0, _ = rc.get_rbt_balance(r["host"], r["did"], ctx.port)
            status, msg, _ = rc.initiate_transaction(
                s["host"], s["did"], r["did"], rbt=ctx.args.rbt_amount,
                memo="smoke-test-rbt", port=ctx.port)
            if not status:
                return False, "rejected", msg
            ok_s1, bal_s1, _ = rc.get_rbt_balance(s["host"], s["did"], ctx.port)
            ok_r1, bal_r1, _ = rc.get_rbt_balance(r["host"], r["did"], ctx.port)
            if rc.close_enough(bal_s1, bal_s0 - ctx.args.rbt_amount) and \
               rc.close_enough(bal_r1, bal_r0 + ctx.args.rbt_amount):
                return True, "sender {}->{}  receiver {}->{}".format(bal_s0, bal_s1, bal_r0, bal_r1), ""
            return False, "balances did not move as expected", "sender {}->{} receiver {}->{}".format(
                bal_s0, bal_s1, bal_r0, bal_r1)
        report.record("rbt-transfer-{}".format(i), do_rbt,
                       sender_host=s["host"], sender_did=s["did"],
                       receiver_host=r["host"], receiver_did=r["did"],
                       quorum_host=ctx.sender_quorum[s["host"]]["host"], amount=ctx.args.rbt_amount)


# ---------------------------------------------------------------------------
# FT
# ---------------------------------------------------------------------------
def run_ft_mint_transfer(ctx, report):
    """Mint a fresh FT series on each sender, transfer half of it to the
    paired receiver."""
    for i, (s, r) in enumerate(ctx.pairs, 1):
        ft_name = "smoketest-{}-{}".format(i, int(time.time()))

        def do_mint(s=s, ft_name=ft_name):
            status, msg, _ = rc.mint_ft(s["host"], s["did"], ft_name, ctx.args.ft_count,
                                         ctx.args.ft_token_count, ctx.port)
            return status, msg, msg
        minted = report.record("ft-mint-{}".format(i), do_mint,
                                host=s["host"], did=s["did"], ft_name=ft_name,
                                ft_count=ctx.args.ft_count, token_count=ctx.args.ft_token_count)
        if not minted:
            continue

        def do_transfer(s=s, r=r, ft_name=ft_name):
            send_count = max(1, ctx.args.ft_count // 2)
            status, msg, _ = rc.initiate_transaction(
                s["host"], s["did"], r["did"],
                ft=[{"ftName": ft_name, "numberOfFts": send_count, "creatorDID": s["did"]}],
                memo="smoke-test-ft", port=ctx.port)
            if not status:
                return False, "rejected", msg
            ok, bal, _ = rc.get_ft_balance(r["host"], r["did"], ctx.port)
            return True, "receiver FT balance now: {}".format(bal), ""
        report.record("ft-transfer-{}".format(i), do_transfer,
                       sender_host=s["host"], receiver_host=r["host"], receiver_did=r["did"],
                       ft_name=ft_name, count=max(1, ctx.args.ft_count // 2))


# ---------------------------------------------------------------------------
# NFT
# ---------------------------------------------------------------------------
def run_nft_flow(ctx, report):
    """Create -> deploy -> execute an NFT on the first sender."""
    host = ctx.senders[0]
    nft_id_holder = {}

    def do_create(s=host):
        status, msg, result = rc.create_nft(s["host"], s["did"], random_bytes(), random_bytes(), ctx.port)
        if status and isinstance(result, str):
            nft_id_holder["id"] = result
        return status, "nft_id={}".format(result), msg
    report.record("nft-create", do_create, host=host["host"], did=host["did"])

    if "id" not in nft_id_holder:
        report.record("nft-deploy", lambda: (False, "skipped", "no NFT ID from create step"), host=host["host"])
        report.record("nft-execute", lambda: (False, "skipped", "no NFT ID from create step"), host=host["host"])
        return

    nft_id = nft_id_holder["id"]

    def do_deploy(s=host, nft_id=nft_id):
        status, msg, _ = rc.initiate_transaction(
            s["host"], s["did"], s["did"],
            nft=[{"nftId": nft_id, "value": 1, "data": "smoke-test-deploy"}],
            memo="smoke-test-nft-deploy", port=ctx.port)
        return status, msg, msg
    deployed = report.record("nft-deploy", do_deploy, host=host["host"], nft_id=nft_id)
    if not deployed:
        report.record("nft-execute", lambda: (False, "skipped", "deploy failed"), host=host["host"], nft_id=nft_id)
        return

    def do_execute(s=host, nft_id=nft_id):
        status, msg, _ = rc.initiate_transaction(
            s["host"], s["did"], s["did"],
            nft=[{"nftId": nft_id, "value": 1, "data": "smoke-test-execute"}],
            memo="smoke-test-nft-execute", port=ctx.port)
        return status, msg, msg
    report.record("nft-execute", do_execute, host=host["host"], nft_id=nft_id)


# ---------------------------------------------------------------------------
# Smart Contract
# ---------------------------------------------------------------------------
def run_sc_flow(ctx, report):
    """Generate -> deploy -> execute a smart contract on the first sender.
    No callback URL registered - out of scope for a smoke test."""
    host = ctx.senders[0]
    sc_id_holder = {}

    def do_generate(s=host):
        status, msg, result = rc.create_smart_contract(s["host"], s["did"], random_bytes(), random_bytes(), ctx.port)
        if status and isinstance(result, str):
            sc_id_holder["id"] = result
        return status, "sc_id={}".format(result), msg
    report.record("sc-generate", do_generate, host=host["host"], did=host["did"])

    if "id" not in sc_id_holder:
        report.record("sc-deploy", lambda: (False, "skipped", "no contract ID from generate step"), host=host["host"])
        report.record("sc-execute", lambda: (False, "skipped", "no contract ID from generate step"), host=host["host"])
        return

    sc_id = sc_id_holder["id"]

    def do_deploy(s=host, sc_id=sc_id):
        status, msg, _ = rc.initiate_transaction(
            s["host"], s["did"], s["did"],
            smart_contract=[{"smartContractId": sc_id, "value": 1, "data": "smoke-test-deploy"}],
            memo="smoke-test-sc-deploy", port=ctx.port)
        return status, msg, msg
    deployed = report.record("sc-deploy", do_deploy, host=host["host"], sc_id=sc_id)
    if not deployed:
        report.record("sc-execute", lambda: (False, "skipped", "deploy failed"), host=host["host"], sc_id=sc_id)
        return

    def do_execute(s=host, sc_id=sc_id):
        status, msg, _ = rc.initiate_transaction(
            s["host"], s["did"], s["did"],
            smart_contract=[{"smartContractId": sc_id, "value": 1, "data": "smoke-test-execute"}],
            memo="smoke-test-sc-execute", port=ctx.port)
        return status, msg, msg
    report.record("sc-execute", do_execute, host=host["host"], sc_id=sc_id)


# Execution order. Add a new case by writing a function above with the
# signature (ctx, report) -> None and appending it here.
TEST_CASES = [
    run_rbt_transfers,
    run_ft_mint_transfer,
    run_nft_flow,
    run_sc_flow,
]
