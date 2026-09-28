"""case_helpers.py - what more than one asset module uses: funding and
quorum preconditions, transfer and balance helpers, shared constants.

Every RBT comes from the faucet (rc.fund_did); nothing here mints RBT.
"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import random
import string
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "full-test"))

import rubix_client as rc
import db_client as db
import wallet_shapes as ws

SKIP = 'SKIP'
SETTLE = 6
TOL = 0.0015


def rand_value(lo, hi):
    """A random amount with 3 decimal places, in [lo, hi].

    Deliberately NOT round numbers like 0.001 / 0.5 / 1.0. Clean denominations
    can take a different path through token selection than an arbitrary value:
    0.5 may split cleanly off a 1.000 token while 0.354 needs several levels
    and leaves awkward change. Testing only tidy values tests the easy path.

    3dp is the network maximum - MinDecimalUnit is 0.001 and FloatPrecision
    ROUNDS at 3dp (math/math.go), so a 4th place would be silently rounded and
    the case would assert against a value the node never saw.

    The value used is reported in every result, so a failure stays reproducible
    even though the input is random.
    """
    v = round(random.uniform(lo, hi), 3)
    return max(v, 0.001)


def _sc_new_contract(ctx, entry):
    """Generate a contract and return (sc_id, error). Does NOT deploy it.

    Both file extensions are checked literally by the server: the binary must
    end .wasm and the source .rs (server/smart_contract.go:70, :101-106).
    """
    import random
    import string
    tag = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    wasm = b"\x00asm\x01\x00\x00\x00" + tag.encode()
    raw = ("// lab contract {}\nfn main() {{}}\n".format(tag)).encode()
    ok, msg, result = rc.create_smart_contract(entry["host"], entry["did"], wasm, raw, ctx.port)
    if not ok or not result:
        return None, str(msg)
    return (result if isinstance(result, str) else str(result)), None
