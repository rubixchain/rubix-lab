#!/usr/bin/env python3
"""Rubix lab - mint the faucet's tokens, then wire the faucet quorum.

Run on the controller once the faucet pair (hosts.txt roles faucet and
faucet-quorum) runs the build whose CustomNetAllowedMinters lists their DIDs
(SETUP-RUNBOOK F4):

  1. mint each DID up to --total RBT, numbered inside its allowlist range
     (faucet from 1, faucet quorum from 2150001), both at once, --batch tokens
     per call (POST /rubix/v1/tokens/generate_faucet_test, start_index)
  2. quorum setup for the faucet quorum DID on its own node
  3. the faucet quorum as the ONLY quorum on the faucet node

Safe to re-run. Minting resumes from what each DID holds, which is exact only
while nothing has been paid out - and the faucet cannot pay out until step 3.
So once the faucet node has a quorum registered, minting is over: a re-run
skips it and only repeats steps 2-3. Re-run it after either faucet node
restarts, because quorum setup lives in the node's memory.
"""

import argparse
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "test-plan", "full-test"))

import fleet
import rubix_client as rc

EP_MINT = "/rubix/v1/tokens/generate_faucet_test"
RANGE_START = {"faucet": 1, "faucet quorum": 2150001}   # CustomNetAllowedMinters


def held(host, did):
    ok, value, note = rc.get_rbt_held(host, did)
    if not ok:
        raise RuntimeError("cannot read the balance: {}".format(note))
    return int(round(value))


def settle(host, did):
    """After a failed call the node may still be minting: wait for the count
    to stop moving for a minute, so a re-run continues from the true number."""
    last, same = None, 0
    while same < 6:
        time.sleep(10)
        now = held(host, did)
        same, last = (same + 1, last) if now == last else (0, now)
    return last


def mint(name, host, did, total, batch, results):
    try:
        while True:
            done = held(host, did)
            if done >= total:
                print("{}: {} RBT minted".format(name, done), flush=True)
                results[name] = True
                return
            n = min(batch, total - done)
            start = RANGE_START[name] + done
            t = time.time()
            ok, msg, _ = rc.signed_action(host, EP_MINT, {"did": did, "token_count": n,
                                          "start_index": start}, timeout=7200)
            if not ok:
                count = settle(host, did)
                print("{}: mint of {} from number {} failed: {} - holds {} now; "
                      "run this again to continue".format(name, n, start, msg, count), flush=True)
                results[name] = False
                return
            secs = time.time() - t
            print("{}: +{} in {:.0f}s ({:.0f} per minute), {} of {}".format(
                name, n, secs, n * 60 / max(secs, 1), done + n, total), flush=True)
    except Exception as exc:          # one DID failing must not hide the other's result
        print("{}: {}".format(name, exc), flush=True)
        results[name] = False


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--total", type=int, default=200000,
                   help="RBT each DID should hold (default: the faucet budget, 200000)")
    p.add_argument("--batch", type=int, default=5000, help="tokens per mint call")
    args = p.parse_args()

    rc.use_fleet_roles(fleet.load_hosts())
    pair = {}
    for name, host in (("faucet", rc.FAUCET_HOST), ("faucet quorum", rc.FAUCET_QUORUM_HOST)):
        did, note = rc._only_did(host, 20000, name, rc.DEFAULT_TIMEOUT)
        if not did:
            sys.exit("ERROR: " + note)
        pair[name] = (host, did)
        print("{:<14} {}  {}".format(name, host, did))

    # The quorum list decides whether minting is over, so an error must stop
    # here: an empty list answers status true with [] (core GetAllQuorum).
    ok, payload = rc.http_json("GET", rc.base_url(rc.FAUCET_HOST) + rc.EP_QUORUM_LIST,
                               rc.DEFAULT_TIMEOUT)
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        sys.exit("ERROR: cannot read the faucet node's quorum list: {}".format(payload))
    if payload.get("result"):
        print("The faucet node already has a quorum, so minting is done - not minting.")
    else:
        results = {}
        threads = [threading.Thread(target=mint, args=(name, host, did, args.total,
                                                       args.batch, results))
                   for name, (host, did) in pair.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if not all(results.get(name) for name in pair):
            sys.exit("Minting is not finished - the faucet is left unwired. Run this again.")

    q_host, q_did = pair["faucet quorum"]
    ok, msg = rc.quorum_setup(q_host, q_did)
    print("quorum setup on {}: {}".format(q_host, msg))
    if not ok and "already" not in msg.lower():
        sys.exit("ERROR: quorum setup failed")
    ok, msg = rc.quorum_reset(rc.FAUCET_HOST, [q_did])
    print("faucet node quorum list: {}".format(msg))
    if not ok:
        sys.exit("ERROR: could not set the faucet node's quorum")
    ok, note = rc.faucet_ready()
    print("Faucet: {}".format(note))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
