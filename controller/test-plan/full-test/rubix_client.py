#!/usr/bin/env python3
"""
rubix_client.py - Shared helpers for the test-plan scripts. Not a script to
run directly - imported by test_runner.py, smoke_test.py and the case modules.

Core primitive (confirmed against server/*.go in the rubixgoplatform repo):
almost every mutating call is a 2-step password challenge:
    POST <action>            -> {"result": {"id": "<reqID>"}}   (password needed)
    POST /rubix/v1/signature  body {"id": "<reqID>", "password": DID_PASSWORD}
                              -> final {"status": bool, "message": ..., "result": ...}
RegisterDID and InitiateTransaction (RBT/FT/NFT/SC all go through the one
/rubix/v1/tx body) follow this. The lab never mints RBT - see fund_did. signed_action()
below drives it generically; a handful of calls (CreateDID, AddQuorum,
GetAllDIDs, balances) are plain single-call GET/POST and use http_json()
directly.

DID_PASSWORD = "mypassword" is the product's own built-in default
(command/command.go -privPWD flag), used throughout its integration test
suite - not a lab-invented value. Same convention used by dids-to-excel.py.
"""

import csv
import datetime
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

DID_PASSWORD = "mypassword"
DEFAULT_PORT = 20000
DEFAULT_TIMEOUT = 8
SIGNATURE_TIMEOUT = 20  # generous: covers pledge/consensus round trips


def new_report_path(script_name, ext="pdf"):
    """One run's report file, under <repo root>/reports/<ext>/.

    Same base filename across formats, so a run's PDF and JSON sit side by
    side and are obviously the same run:
        reports/pdf/catalogue_rbt_2026-09-03_10-15-00.pdf
        reports/json/catalogue_rbt_2026-09-03_10-15-00.json
    Timestamped so re-runs never silently overwrite a previous result.
    """
    # this file is controller/test-plan/full-test/ -> three levels up is the repo root
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    report_dir = os.path.join(repo_root, "reports", ext)
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(report_dir, "{}_{}.{}".format(script_name, timestamp, ext))


def new_report_paths(script_name, exts=("pdf", "json")):
    """Paths for every format of ONE run, sharing a single timestamp so the
    files are matched. Returns {ext: path}."""
    # this file is controller/test-plan/full-test/ -> three levels up is the repo root
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    out = {}
    for ext in exts:
        d = os.path.join(repo_root, "reports", ext)
        os.makedirs(d, exist_ok=True)
        out[ext] = os.path.join(d, "{}_{}.{}".format(script_name, timestamp, ext))
    return out

EP_DIDS = "/rubix/v1/dids"
EP_PEER_ID = "/rubix/v1/node/peer_id"
EP_CREATE_DID = "/rubix/v1/dids/create"
EP_REGISTER_DID = "/rubix/v1/dids/{did}/register"
EP_SIGNATURE = "/rubix/v1/signature"
EP_RBT_BALANCE = "/rubix/v1/dids/{did}/balances/rbt"
EP_QUORUM_SETUP = "/rubix/v1/quorums/setup"
EP_QUORUM_ADD = "/rubix/v1/quorums/add"
EP_QUORUM_LIST = "/rubix/v1/quorums"
EP_QUORUM_REMOVE_ALL = "/rubix/v1/quorums/remove_all"   # GET, truncates quorum_manager
EP_TRANSACTION = "/rubix/v1/tx"  # confirmed setup.go:69 - NOT /rubix/v1/transaction
EP_FT_MINT = "/rubix/v1/fts/mint"
EP_FT_BALANCE = "/rubix/v1/dids/{did}/balances/ft"
EP_CREATE_NFT = "/rubix/v1/nfts/generate"
EP_GENERATE_SC = "/rubix/v1/smart_contracts/generate"

# Read-side + subscription endpoints. Needed by the core-derived cases, which
# assert END STATE (chain length, ownership, cross-node sync) rather than just
# a 200 on the write. Paths taken from the product's own integration client.
EP_NFT_LIST = "/rubix/v1/nfts"
EP_NFT_BALANCE = "/rubix/v1/dids/{did}/balances/nft"
EP_NFT_CHAIN = "/rubix/v1/nfts/{nft}/chain"
EP_NFT_CHILDREN = "/rubix/v1/nfts/{nft}/children"
EP_NFT_PARENT = "/rubix/v1/nfts/{nft}/parent"
EP_NFT_SUBSCRIBE = "/rubix/v1/nfts/subscribe?nft={nft}"
EP_SC_LIST = "/rubix/v1/smart_contracts"
EP_SC_CHAIN = "/rubix/v1/smart_contracts/{sc}/chain"
EP_SC_SUBSCRIBE = "/rubix/v1/smart_contracts/subscribe?smartContractToken={sc}"
EP_SC_REGISTER_CALLBACK = "/rubix/v1/smart_contracts/register_callback"
EP_FT_LIST = "/rubix/v1/fts"
# token_type is one of: rbt, nft, ft, smartContract
EP_TX_BY_DID = "/rubix/v1/tx/{did}/{token_type}"


def base_url(host, port=DEFAULT_PORT):
    return "http://{}:{}".format(host, port)


def http_json(method, url, timeout=DEFAULT_TIMEOUT, body=None):
    """Send a GET/POST and return (ok, payload_or_error_string)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, "HTTP {}".format(resp.status)
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, "HTTP {}".format(e.code)
    except urllib.error.URLError as e:
        return False, "unreachable ({})".format(e.reason)
    except TimeoutError:
        return False, "timeout"
    except Exception as e:  # malformed JSON, connection reset, etc.
        return False, "{}: {}".format(type(e).__name__, e)


# ---------------------------------------------------------------------------
# Operation log
#
# Every transaction and FT mint any case (or the faucet) makes is appended
# here, with the transactionID the node returned. The runner reads the slice a
# case produced and checks each one against the nodes' databases, so a verdict
# rests on rows that exist, not only on what the API answered.
# ---------------------------------------------------------------------------

OPS = []
_OPS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pacing for the fullnode
#
# The fullnode validates published transactions with several workers in
# parallel and gives each 3 attempts ~6s apart (core/fullnode_txn_processor.go).
# If a token is spent again before the fullnode has stored the transaction that
# delivered it, the two can be validated out of order and the later one is
# rejected with a previous-transaction mismatch.
#
# So before any DID starts a transaction, wait until the fullnode has processed
# the last transaction that DID sent or received (accepted or rejected - either
# way it is done with it). It waits on the fullnode's actual state, not a fixed
# sleep, so an already-processed history costs nothing:
#   - a chain on one token (hops, back-to-back sends) is paced step by step
#   - a parallel burst stays parallel: every call in it reads the same, already
#     processed, previous transaction
#   - the next case on the same DIDs waits for the previous case's last one
# If the fullnode database cannot be read, or it keeps not answering, pacing
# switches itself off and says so, rather than slowing every transaction.
# ---------------------------------------------------------------------------

FULLNODE_HOST = ""          # set by test_runner from hosts.txt (role fullnode)
PACE_TIMEOUT = 15           # longest wait for one previous transaction, seconds
_LAST_TX = {}               # did -> txid of the last successful tx it took part in
_IN_FLIGHT = {}             # did -> transactions it has started and not finished
_PACE = {"off": False, "misses": 0}
_UNPACED = set()            # DIDs a case has taken out of pacing (see unpaced())


class unpaced(object):
    """`with rc.unpaced(a, b):` - these DIDs transact without waiting for the
    fullnode. The runner uses it for the WITHOUT-delay run of each case (the
    same case then runs again, paced, to compare what the fullnode did). Per DID, so other units running at the same time
    are unaffected."""

    def __init__(self, *entries_or_dids):
        self.dids = [e["did"] if isinstance(e, dict) else e for e in entries_or_dids]

    def __enter__(self):
        with _OPS_LOCK:
            _UNPACED.update(self.dids)
        return self

    def __exit__(self, *exc):
        with _OPS_LOCK:
            _UNPACED.difference_update(self.dids)
        return False


def _fullnode_has(txid):
    import db_client as db        # imported here: db_client is optional for most tools
    rows = db.query(FULLNODE_HOST,
                    "SELECT 1 FROM fullnode_transactions WHERE id = %s UNION ALL "
                    "SELECT 1 FROM fullnode_invalid_transactions "
                    "WHERE transaction->>'ID' = %s LIMIT 1", (txid, txid))
    return bool(rows)


def _pace(did):
    """Wait until the fullnode has processed `did`'s last transaction.
    Returns the seconds waited."""
    if not FULLNODE_HOST or _PACE["off"] or not did or did in _UNPACED:
        return 0.0
    txid = _LAST_TX.get(did)
    if not txid:
        return 0.0
    started = time.time()
    while time.time() - started < PACE_TIMEOUT:
        try:
            if _fullnode_has(txid):
                _PACE["misses"] = 0
                return round(time.time() - started, 2)
        except Exception as e:
            _PACE["off"] = True
            print("  [pacing] fullnode database not readable ({}: {}) - pacing off".format(
                type(e).__name__, e))
            return round(time.time() - started, 2)
        time.sleep(0.5)
    _PACE["misses"] += 1
    if _PACE["misses"] >= 3:
        _PACE["off"] = True
        print("  [pacing] the fullnode has not recorded 3 transactions in a row within "
              "{}s - pacing off".format(PACE_TIMEOUT))
    return round(time.time() - started, 2)


def _log_op(host, action_path, body, status, message, result, waited=0.0):
    if action_path not in (EP_TRANSACTION, EP_FT_MINT):
        return
    body = body or {}
    tokens = body.get("tokens") or {}
    txid = result.get("transactionID") if isinstance(result, dict) else None
    try:
        rbt = float(tokens.get("rbt") or 0)
    except (TypeError, ValueError):
        rbt = 0.0
    entry = {
        "at": round(time.time(), 3),
        "kind": "tx" if action_path == EP_TRANSACTION else "ft_mint",
        "host": host,
        "initiator": body.get("initiator") or body.get("did"),
        "owner": body.get("owner") or "",
        "rbt": rbt,
        "assets": sorted(k for k in ("ft", "nft", "smartContract") if tokens.get(k)),
        "status": bool(status),
        "message": str(message or "")[:200],
        "txid": txid or "",
        "waited_for_fullnode": waited,
    }
    with _OPS_LOCK:
        OPS.append(entry)
        if entry["kind"] == "tx" and entry["status"] and txid:
            for did in (entry["initiator"], entry["owner"]):
                if did:
                    _LAST_TX[did] = txid


def signed_action(host, action_path, body, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """
    POST an action that needs the password-challenge round trip.
    Returns (status: bool, message: str, result: any).
    """
    waited = 0.0
    initiator = body.get("initiator") if (action_path == EP_TRANSACTION and body) else None
    if initiator:
        # A call from a DID that already has a transaction in flight is part of
        # a parallel burst: it goes now, with the others, rather than waiting.
        with _OPS_LOCK:
            joining = _IN_FLIGHT.get(initiator, 0) > 0
        if not joining:
            waited = _pace(initiator)
        with _OPS_LOCK:
            _IN_FLIGHT[initiator] = _IN_FLIGHT.get(initiator, 0) + 1
    try:
        status, message, result = _signed_action(host, action_path, body, port, timeout)
    finally:
        if initiator:
            with _OPS_LOCK:
                _IN_FLIGHT[initiator] -= 1
    _log_op(host, action_path, body, status, message, result, waited)
    return status, message, result


def _signed_action(host, action_path, body, port, timeout):
    base = base_url(host, port)
    ok, payload = http_json("POST", base + action_path, timeout, body)
    if not ok or not isinstance(payload, dict):
        return False, "request failed: {}".format(payload), None

    # A validation failure (e.g. bad DID, insufficient balance) can be
    # returned directly here, with no challenge step at all.
    result = payload.get("result")
    req_id = result.get("id") if isinstance(result, dict) else None
    if not req_id:
        return bool(payload.get("status")), payload.get("message", ""), result

    ok, payload = http_json("POST", base + EP_SIGNATURE, timeout,
                             {"id": req_id, "password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict):
        return False, "signature step failed: {}".format(payload), None
    return bool(payload.get("status")), payload.get("message", ""), payload.get("result")


def multipart_post(url, fields, files, timeout=SIGNATURE_TIMEOUT):
    """
    POST multipart/form-data (stdlib only, no 'requests' dependency).
    fields: {name: str}. files: {form_field_name: (filename, bytes_content)}.
    Returns (ok, payload_or_error_string) - same shape as http_json.
    """
    boundary = "----rubixlab{}".format(int(time.time() * 1000))
    parts = []
    for name, value in fields.items():
        parts.append(
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(
                boundary, name, value).encode("utf-8"))
    for field_name, (filename, content) in files.items():
        header = (
            "--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".format(boundary, field_name, filename)
        ).encode("utf-8")
        parts.append(header + content + b"\r\n")
    parts.append("--{}--\r\n".format(boundary).encode("utf-8"))
    body = b"".join(parts)

    headers = {"Content-Type": "multipart/form-data; boundary={}".format(boundary)}
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, "HTTP {}".format(resp.status)
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, "HTTP {}".format(e.code)
    except urllib.error.URLError as e:
        return False, "unreachable ({})".format(e.reason)
    except TimeoutError:
        return False, "timeout"
    except Exception as e:
        return False, "{}: {}".format(type(e).__name__, e)


def signed_multipart_action(host, action_path, fields, files, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Same password-challenge pattern as signed_action(), but the first
    call is multipart/form-data (NFT/SC creation both need real file
    uploads, confirmed against server/nft.go and server/smart_contract.go -
    they are NOT plain JSON despite everything else being JSON)."""
    base = base_url(host, port)
    ok, payload = multipart_post(base + action_path, fields, files, timeout)
    if not ok or not isinstance(payload, dict):
        return False, "request failed: {}".format(payload), None

    result = payload.get("result")
    req_id = result.get("id") if isinstance(result, dict) else None
    if not req_id:
        return bool(payload.get("status")), payload.get("message", ""), result

    ok, payload = http_json("POST", base + EP_SIGNATURE, timeout,
                             {"id": req_id, "password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict):
        return False, "signature step failed: {}".format(payload), None
    return bool(payload.get("status")), payload.get("message", ""), payload.get("result")


def create_did(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Create a brand-new DID. Synchronous, no password challenge (confirmed:
    server.APICreateDID calls core.CreateDID directly, not via AddWebReq).
    HARD GATE: only ever call this when a host is confirmed to have ZERO
    DIDs. CreateDID has no idempotency in the product code - calling it on a
    host that already has one silently mints a second identity, no error."""
    ok, payload = http_json("POST", base_url(host, port) + EP_CREATE_DID, timeout,
                             {"password": DID_PASSWORD})
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        return None, "create failed: {}".format(payload if not ok else payload.get("message"))
    did = (payload.get("result") or {}).get("did")
    if not did:
        return None, "create failed: no DID in response"
    return did, "created"


def mint_ft(host, did, ft_name, ft_count, token_count, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Mint a new FT series. token_count is RBT burnt (FTCount <= TokenCount*1000)."""
    body = {"did": did, "ft_name": ft_name, "ft_count": int(ft_count),
            "token_count": int(token_count), "ft_num_start_index": 0}
    return signed_action(host, EP_FT_MINT, body, port, timeout)


def get_ft_balance(host, did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Returns (ok, {ft_name: count} or raw result, note)."""
    url = base_url(host, port) + EP_FT_BALANCE.format(did=did)
    ok, payload = http_json("GET", url, timeout)
    if not ok:
        return False, None, payload
    return True, payload.get("result") if isinstance(payload, dict) else None, ""


def create_nft(host, did, metadata_bytes, artifact_bytes, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """multipart/form-data: did, metadata file, artifact file -> returns the
    new NFT's ID as a plain string in `result` (core/nft.go createNFT)."""
    fields = {"did": did}
    files = {"metadata": ("metadata.json", metadata_bytes),
             "artifact": ("artifact.bin", artifact_bytes)}
    return signed_multipart_action(host, EP_CREATE_NFT, fields, files, port, timeout)


def create_smart_contract(host, did, wasm_bytes, raw_bytes, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """multipart/form-data: did, binaryCodePath (.wasm), rawCodePath (source)
    -> returns the new contract's ID as a plain string in `result`
    (confirmed against server/smart_contract.go - both files required, and
    BOTH extensions are checked literally: binaryCodePath must end '.wasm',
    rawCodePath must end '.rs' - server/smart_contract.go:70 and :101-106)."""
    fields = {"did": did}
    files = {"binaryCodePath": ("contract.wasm", wasm_bytes),
             "rawCodePath": ("contract.rs", raw_bytes)}
    return signed_multipart_action(host, EP_GENERATE_SC, fields, files, port, timeout)


# ---------------------------------------------------------------------------
# NFT / SC / FT read side + subscriptions
#
# The core-derived cases assert end state, so they need the read endpoints as
# much as the write ones. Payload shapes below mirror the product's own
# integration client exactly - where a field looks redundant or oddly named,
# it is reproduced rather than "cleaned up", because the server reads it
# literally and a tidier body is a different test.
# ---------------------------------------------------------------------------

def _get_list(host, path, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """GET a path whose `result` is a list. Returns (ok, list, note).

    A null `result` is normal for 'nothing yet' (no NFTs, empty chain) and
    comes back as [], not an error - distinguishing 'empty' from 'failed'
    matters, since an empty chain is a legitimate assertion target.
    """
    ok, payload = http_json("GET", base_url(host, port) + path, timeout)
    if not ok:
        return False, [], payload
    result = payload.get("result") if isinstance(payload, dict) else None
    return True, (result if isinstance(result, list) else []), ""


def list_nfts(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    return _get_list(host, EP_NFT_LIST, port, timeout)


def get_nft_balance(host, did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """NFTs owned by did (Free status only)."""
    return _get_list(host, EP_NFT_BALANCE.format(did=did), port, timeout)


def get_nft_chain(host, nft_id, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Ordered chain for one NFT. Length grows by one per deploy/execute/transfer,
    which is what the chain-length checks assert."""
    return _get_list(host, EP_NFT_CHAIN.format(nft=nft_id), port, timeout)


def get_nft_children(host, nft_id, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    return _get_list(host, EP_NFT_CHILDREN.format(nft=nft_id), port, timeout)


def get_nft_parent(host, nft_id, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Returns (ok, result, note). `result` is null when the NFT has no parent,
    so this returns the raw value rather than coercing it to a list."""
    ok, payload = http_json("GET", base_url(host, port) + EP_NFT_PARENT.format(nft=nft_id), timeout)
    if not ok:
        return False, None, payload
    return True, (payload.get("result") if isinstance(payload, dict) else None), ""


def subscribe_nft(host, nft_id, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Subscribe so this node can execute an NFT it does not own.

    Subscription is the gate for NFT/SC execute, not ownership
    (core/consensus/checks.go:122) - which is exactly what the cross-node
    execute cases exist to prove.
    """
    ok, payload = http_json("GET", base_url(host, port) + EP_NFT_SUBSCRIBE.format(nft=nft_id), timeout)
    if not ok:
        return False, payload
    return bool(payload.get("status")), payload.get("message", "")


def list_smart_contracts(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    return _get_list(host, EP_SC_LIST, port, timeout)


def get_sc_chain(host, sc_id, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    return _get_list(host, EP_SC_CHAIN.format(sc=sc_id), port, timeout)


def subscribe_smart_contract(host, sc_id, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    ok, payload = http_json("GET", base_url(host, port) + EP_SC_SUBSCRIBE.format(sc=sc_id), timeout)
    if not ok:
        return False, payload
    return bool(payload.get("status")), payload.get("message", "")


def register_sc_callback(host, sc_id, callback_url, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Field names are exact: smartContractToken / callBackURL."""
    return signed_action(host, EP_SC_REGISTER_CALLBACK,
                         {"smartContractToken": sc_id, "callBackURL": callback_url},
                         port, timeout)


def list_fts(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    return _get_list(host, EP_FT_LIST, port, timeout)


def get_transactions(host, did, token_type, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Transaction history for did, filtered. token_type: rbt|nft|ft|smartContract."""
    return _get_list(host, EP_TX_BY_DID.format(did=did, token_type=token_type), port, timeout)


# ---------------------------------------------------------------------------
# NFT / SC transactions
#
# All go through POST /rubix/v1/tx. The bodies differ in ways the server reads
# literally, so each shape is built explicitly instead of through one
# "flexible" helper that would blur them.
# ---------------------------------------------------------------------------

def _tx(host, body, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """POST a fully-formed TransactionRequest and complete the password challenge."""
    return signed_action(host, EP_TRANSACTION, body, port, timeout)


def nft_transaction(host, initiator_did, owner_did, nft_id, value=1.0,
                    data="NFT transaction", transfer_ownership=False,
                    port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Deploy / execute / transfer an NFT - the same call, three meanings.

    transfer_ownership=False with owner==initiator is a self-execute (or the
    initial deploy). transfer_ownership=True with a different owner moves it.
    Ownership only changes when the flag is set, regardless of `owner`.
    """
    body = {
        "initiator": initiator_did,
        "owner": owner_did,
        "tokens": {
            "rbt": 0, "ft": [],
            "nft": [{"nftId": nft_id, "value": value, "data": data}],
            "smartContract": [],
            "transferNftOwnership": transfer_ownership,
        },
        "memo": data,
    }
    return _tx(host, body, port, timeout)


def mint_nft_children(host, initiator_did, parent_nft_id, count,
                      data="child mint", port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Mint `count` children under a parent NFT.

    ONE nft entry PER CHILD - there is no numberOfChildren field. `nftId` is
    omitted deliberately: it is IGNORED when parentNFTId is set, and supplying
    the parent id there makes the server reject the call as "already exists".
    No "owner" key at all, matching the product's own child-mint payload.
    Response carries result.mintedNFTChildren = [{parentNFTId, childNFTId}].
    """
    body = {
        "initiator": initiator_did,
        "tokens": {
            "rbt": 0, "ft": [],
            "nft": [{"value": 0, "data": data, "parentNFTId": parent_nft_id}
                    for _ in range(count)],
            "smartContract": [],
            "transferNftOwnership": False,
        },
        "memo": "mint {} children of {}".format(count, parent_nft_id[:8]),
    }
    return _tx(host, body, port, timeout)


def sc_transaction(host, initiator_did, sc_id, value=1.0, data="SC transaction",
                   port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Deploy or execute a smart contract.

    `owner` is ALWAYS the empty string for smart contracts - there is no
    ownership transfer concept here, and sending a DID instead changes what
    the server does.
    """
    body = {
        "initiator": initiator_did,
        "owner": "",
        "tokens": {
            "rbt": 0, "ft": [], "nft": [],
            "smartContract": [{"smartContractId": sc_id, "value": value, "data": data}],
            "transferNftOwnership": False,
        },
        "memo": data,
    }
    return _tx(host, body, port, timeout)


def minted_children(result):
    """Pull [{parentNFTId, childNFTId}] out of a child-mint result."""
    if not isinstance(result, dict):
        return []
    return result.get("mintedNFTChildren") or []


def get_dids(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Returns (reachable, dids_list, note)."""
    ok, payload = http_json("GET", base_url(host, port) + EP_DIDS, timeout)
    if not ok:
        return False, [], payload
    result = payload.get("result") if isinstance(payload, dict) else None
    return True, (result if isinstance(result, list) else []), ""


def get_rbt_balance_detail(host, did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Full RBT balance breakdown.

    Confirmed against types/balance.go RBTBalance, whose JSON tags are
    exactly {"balance", "pledged", "locked"} - NOT rbt_amount/rbtAmount.
    `balance` is the FREE (spendable) portion only; tokens that are locked
    for an in-flight transfer or pledged as quorum collateral are reported
    separately and are NOT part of it.

    That distinction matters: several catalogue cases assert "tokens released
    if locked" after a rejection, which is invisible if you only read
    `balance`.

    Returns (ok, {"balance": f, "locked": f, "pledged": f}, note).
    """
    url = base_url(host, port) + EP_RBT_BALANCE.format(did=did)
    ok, payload = http_json("GET", url, timeout)
    if not ok:
        return False, None, payload
    result = payload.get("result") if isinstance(payload, dict) else None
    if isinstance(result, dict):
        out = {}
        for key in ("balance", "locked", "pledged"):
            try:
                out[key] = float(result.get(key) or 0)
            except (TypeError, ValueError):
                return False, None, "unparseable {}: {}".format(key, result.get(key))
        return True, out, ""
    # A bare number is still accepted as the free balance, for robustness.
    if isinstance(result, (int, float)):
        return True, {"balance": float(result), "locked": 0.0, "pledged": 0.0}, ""
    return False, None, "no balance fields in response: {}".format(payload)


def get_rbt_balance(host, did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Free (spendable) RBT balance. Returns (ok, balance_or_None, note).
    Use get_rbt_balance_detail() when locked/pledged matter."""
    ok, detail, note = get_rbt_balance_detail(host, did, port, timeout)
    if not ok:
        return False, None, note
    return True, detail["balance"], ""


# ---------------------------------------------------------------------------
# Funding - from the faucet DID only
#
# Nothing in the lab mints RBT. Every token a test DID holds was transferred to
# it from the faucet DID, and every faucet transfer is signed by the faucet
# quorum. So fleet quorums never pledge for funding, and funding never touches
# a wallet some other lane is measuring.
#
# The faucet runs on the CONTROLLER machine as two nodes:
#     port 20000  the faucet DID          - holds the RBT and sends it
#     port 20010  the faucet quorum DID   - signs every faucet transfer
# Both are set up by hand (quorum added and quorum setup done); the lab only
# transfers. Each node's DID is read from the node itself, so nothing needs
# configuring. The faucet node must have EXACTLY ONE quorum registered, the
# faucet quorum: a transfer is signed by the first entry in that list
# (quorumAddresses[0], core/transaction.go) and the list has no ORDER BY, so a
# second entry would make the signer unpredictable. fund_did checks this before
# every draw and refuses rather than fund through the wrong quorum.
#
# DIDs are never discarded. A DID keeps whatever it holds from one cycle to the
# next and is topped up only by its shortfall.
#
# Overrides, only if the setup ever changes:
#   RUBIX_FAUCET_HOST          controller address (test_runner uses the host
#                              tagged 'controller' in hosts.txt; else .103)
#   RUBIX_FAUCET_PORT          faucet DID's node (default 20000)
#   RUBIX_FAUCET_QUORUM_PORT   faucet quorum's node (default 20010)
#   RUBIX_FAUCET_CHUNK         largest single faucet transfer, RBT (default 1000)
# ---------------------------------------------------------------------------

FAUCET_HOST = os.environ.get("RUBIX_FAUCET_HOST", "192.168.1.103")
FAUCET_PORT = int(os.environ.get("RUBIX_FAUCET_PORT", "20000"))
FAUCET_QUORUM_PORT = int(os.environ.get("RUBIX_FAUCET_QUORUM_PORT", "20010"))
# Read from the two faucet nodes by faucet_ready().
FAUCET_DID = ""
FAUCET_QUORUM_DID = ""
# Funding is plumbing, not a test: a large top-up goes out in chunks so it
# never runs into the quorum timeouts the value ladders exist to measure.
FAUCET_CHUNK = int(os.environ.get("RUBIX_FAUCET_CHUNK", "1000"))

# One faucet transfer at a time. Units fund in parallel, and many parallel
# sends from one DID is something the suite TESTS (RBT-N-10) - funding must not
# depend on it working.
_FAUCET_LOCK = threading.Lock()

# Values are rounded to 3dp (math/math.go FloatPrecision).
TOLERANCE = 0.0015


def transfer_timeout(rbt):
    """Client timeout for a transfer of `rbt`. The node keeps working after the
    client gives up, so a timeout that is too short does not stop a transfer -
    it only makes the caller report failure for something that then succeeds.
    Scaled to the token count and capped just above TotalQuorumTimeout (15m)
    plus the adaptive ceiling (30m) in core/quorum_initiator.go."""
    try:
        n = float(rbt or 0)
    except (TypeError, ValueError):
        n = 0
    return int(min(2400, max(SIGNATURE_TIMEOUT, 60 + n * 0.2)))


def _same_did(listed, did):
    # the quorum list holds bare DIDs today; tolerate a "<peer>.<did>" address
    return listed == did or str(listed).split(".")[-1] == did


def _only_did(port, what, timeout):
    ok, dids, note = get_dids(FAUCET_HOST, port, timeout)
    if not ok:
        return None, "{} node {}:{} did not answer: {}".format(what, FAUCET_HOST, port, note)
    if len(dids) != 1:
        return None, "{} node {}:{} holds {} DIDs, expected exactly 1".format(
            what, FAUCET_HOST, port, len(dids))
    return dids[0], ""


def faucet_ready(timeout=DEFAULT_TIMEOUT):
    """(ok, note). Reads both faucet DIDs from their nodes, then checks the
    faucet node has the faucet quorum as its one and only quorum."""
    global FAUCET_DID, FAUCET_QUORUM_DID
    faucet, note = _only_did(FAUCET_PORT, "faucet", timeout)
    if not faucet:
        return False, note
    quorum, note = _only_did(FAUCET_QUORUM_PORT, "faucet quorum", timeout)
    if not quorum:
        return False, note
    FAUCET_DID, FAUCET_QUORUM_DID = faucet, quorum
    ok, quorums, note = get_quorums(FAUCET_HOST, FAUCET_PORT, timeout)
    if not ok:
        return False, "faucet node {}:{} did not answer: {}".format(FAUCET_HOST, FAUCET_PORT, note)
    if len(quorums) != 1 or not _same_did(quorums[0], FAUCET_QUORUM_DID):
        return False, ("faucet node {}:{} must have exactly one quorum registered, the "
                       "faucet quorum {}... (port {}); it has {}".format(
                           FAUCET_HOST, FAUCET_PORT, FAUCET_QUORUM_DID[:16],
                           FAUCET_QUORUM_PORT, [str(q)[-16:] for q in quorums] or "none"))
    ok, detail, note = get_rbt_balance_detail(FAUCET_HOST, FAUCET_DID, FAUCET_PORT, timeout)
    if not ok or not detail:
        return False, "cannot read the faucet balance: {}".format(note)
    return True, "faucet holds {:.3f} RBT free".format(detail["balance"])


def _whole(amount):
    """Funding is always whole RBT.

    A fractional payout forces the faucet to split, and a part token spent by a
    second holder can fail the minter-allowlist genesis lookup (the fix is not
    in the deployed branch). The faucet only ever hands out whole tokens it
    minted itself, so every funded wallet starts first-hand and clean; a case
    that needs parts builds them deliberately.
    """
    n = int(amount)
    return n + 1 if amount > n else max(n, 1)


def fund_did(host, did, amount, port=DEFAULT_PORT, timeout=None):
    """Transfer at least `amount` RBT (whole tokens) from the faucet to `did`.

    Returns (status, message). Waits until the receiver's free balance shows
    the credit, so a caller can assert on the balance straight after.
    """
    n = _whole(amount)
    with _FAUCET_LOCK:
        ok, note = faucet_ready()
        if not ok:
            return False, note
        if did in (FAUCET_DID, FAUCET_QUORUM_DID):
            return False, "refusing to fund the faucet or its quorum from the faucet"
        sent = 0
        while sent < n:
            chunk = min(FAUCET_CHUNK, n - sent)
            ok0, detail0, _ = get_rbt_balance_detail(host, did, port)
            before = detail0["balance"] if ok0 and detail0 else 0.0
            ok, msg, _ = initiate_transaction(
                FAUCET_HOST, FAUCET_DID, did, rbt=float(chunk),
                memo="faucet {} RBT".format(chunk), port=FAUCET_PORT,
                timeout=timeout or transfer_timeout(chunk))
            # The receiver credits 1-2s after the sender returns, longer for a
            # big chunk. Wait on the receiver whatever the faucet answered: a
            # client timeout does not stop the node, and a transfer that went
            # through anyway must not be sent a second time.
            credited, _now = wait_for_balance(host, did, before + chunk - TOLERANCE,
                                              port, attempts=15 + chunk // 100, delay=2)
            if not credited:
                return False, ("faucet transfer of {} RBT to {}... {} (sent {} of {} "
                               "so far)".format(chunk, did[:16],
                                                "rejected: {}".format(msg) if not ok
                                                else "reported success but the "
                                                "receiver was never credited",
                                                sent, n))
            sent += chunk
    return True, "received {} RBT from the faucet".format(n)


def quorum_setup(host, did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Activate a DID as a quorum signer on its own node. Single-call, no
    password challenge (confirmed: core.SetupQuorum returns synchronously)."""
    body = {"did": did, "password": DID_PASSWORD, "priv_password": DID_PASSWORD}
    ok, payload = http_json("POST", base_url(host, port) + EP_QUORUM_SETUP, timeout, body)
    if not ok or not isinstance(payload, dict):
        return False, "request failed: {}".format(payload)
    return bool(payload.get("status")), payload.get("message", "")


def quorum_add(host, quorum_did, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Register a quorum DID as trusted on a participant node. Idempotency
    note: the Go wrapper errors on repeat even though the DB write is
    ON CONFLICT DO NOTHING (core/wallet/quorum.go) - callers must treat an
    'already exists' style message as success, not failure."""
    ok, payload = http_json("POST", base_url(host, port) + EP_QUORUM_ADD, timeout,
                             {"did": quorum_did})
    if not ok or not isinstance(payload, dict):
        return False, "request failed: {}".format(payload)
    status = bool(payload.get("status"))
    message = payload.get("message", "")
    if not status and "already exist" in message.lower():
        return True, message + " (treated as success - idempotent)"
    return status, message


def quorum_reset(host, quorum_dids, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    """Make `quorum_dids` the node's whole quorum list, in that order.

    A transfer is signed by the FIRST quorum in the node's list
    (quorumAddresses[0], core/transaction.go), and the list is read with no
    ORDER BY (core/wallet/quorum.go). Emptying it first (remove_all truncates
    quorum_manager) and adding one quorum makes the signer certain rather than
    whichever row Postgres returns first. The list is per NODE: every DID on
    the node now uses it. Returns (ok, message)."""
    ok, payload = http_json("GET", base_url(host, port) + EP_QUORUM_REMOVE_ALL, timeout)
    if not ok or not isinstance(payload, dict) or not payload.get("status"):
        return False, "remove_all failed on {}: {}".format(host, payload)
    for did in quorum_dids:
        ok, msg = quorum_add(host, did, port, timeout)
        if not ok:
            return False, "add {}... on {} failed: {}".format(did[:16], host, msg)
    ok, listed, note = get_quorums(host, port, timeout)
    if not ok or [str(q).split(".")[-1] for q in listed] != list(quorum_dids):
        return False, "quorum list on {} is {} after reset, expected {}".format(
            host, listed if ok else note, [d[:16] for d in quorum_dids])
    return True, "quorum list set to {}".format([d[:16] for d in quorum_dids])


def get_quorums(host, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
    ok, payload = http_json("GET", base_url(host, port) + EP_QUORUM_LIST, timeout)
    if not ok:
        return False, [], payload
    result = payload.get("result") if isinstance(payload, dict) else None
    return True, (result if isinstance(result, list) else []), ""


def announce_did(host, did, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """Re-broadcast an EXISTING DID's peer mapping (register + signature).
    Safe to repeat, unlike create - never call create here. Used for the
    'everyone online together' announcement pass, not first-time DID setup."""
    url = EP_REGISTER_DID.format(did=did)
    return signed_action(host, url, None, port, timeout)


def initiate_transaction(sender_host, initiator_did, receiver_did, rbt=None, ft=None,
                          nft=None, smart_contract=None, transfer_nft_ownership=False,
                          memo="", port=DEFAULT_PORT, timeout=None):
    """
    Fire a transaction from sender_host. One body shape covers RBT/FT/NFT/SC
    and any combination (types/models/transaction_info.go TransactionRequest).

    CONFIRMED against core/transaction.go:44 (`nextOwnerDID := request.Owner`)
    and core/transaction_builder.go:60-69: the JSON field is called "owner"
    but it means WHO RECEIVES the asset after this transaction, not who it's
    from. initiator_did must be a DID that exists LOCALLY on sender_host
    (SetupDID requires it) - it is NOT the receiver.

    For a real transfer between two different DIDs: receiver_did = the
    OTHER party's DID.
    For NFT/SC deploy or self-execute (no ownership change): receiver_did
    should equal initiator_did - though note the product code pins
    Owner=Initiator for deploys and pins it to the current owner for
    NFT-only execute regardless of what's passed here, so this case is
    forgiving; transfers are not.

    With no timeout given, it is scaled to the RBT value (transfer_timeout).

    Returns (status, message, result).
    """
    if timeout is None:
        timeout = transfer_timeout(rbt)
    tokens = {"rbt": rbt or 0, "transferNftOwnership": transfer_nft_ownership}
    if ft:
        tokens["ft"] = ft
    if nft:
        tokens["nft"] = nft
    if smart_contract:
        tokens["smartContract"] = smart_contract
    body = {"initiator": initiator_did, "owner": receiver_did, "tokens": tokens, "memo": memo}
    return signed_action(sender_host, EP_TRANSACTION, body, port, timeout)


def load_hosts(path):
    """Read hosts.txt. Format per line: <host> [role] ('#' comments ok)."""
    if not os.path.exists(path):
        sys.exit("ERROR: hosts file not found: {}".format(path))
    hosts = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            hosts.append({"host": parts[0], "role": parts[1] if len(parts) > 1 else ""})
    if not hosts:
        sys.exit("ERROR: no hosts listed in {}".format(path))
    return hosts


def write_pdf_report(path, title, headers, rows):
    """
    Generic tabular PDF report writer, shared by every test-plan script.
    rows: list of lists of strings, same column count as headers.
    Requires reportlab (not stdlib): pip install reportlab
    Chosen over .xlsx because the lab machines are headless Ubuntu boxes -
    no spreadsheet app to open .xlsx with, and a PDF can be viewed anywhere
    (browser, any OS) once copied off the box.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import landscape, A3
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    except ImportError:
        # Never lose a completed run's results just because a formatting
        # library is missing - by the time this is called every test has
        # already executed against the real fleet. Fall back to CSV, which
        # needs nothing beyond the stdlib, and say so loudly.
        csv_path = os.path.splitext(path)[0] + ".csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(headers)
            writer.writerows(rows)
        print("\nWARNING: reportlab is not installed, so no PDF was written.")
        print("         Results were NOT lost - saved as CSV instead:")
        print("           {}".format(csv_path))
        print("         For PDFs in future runs:  sudo apt install -y python3-reportlab")
        print("         (plain `pip install` is blocked on Ubuntu 24.04 by PEP 668,")
        print("          and a venv gets lost if the .venv directory is removed)")
        return

    styles = getSampleStyleSheet()
    cell_style = ParagraphStyle("cell", parent=styles["BodyText"], fontSize=7, leading=9)
    header_style = ParagraphStyle("header", parent=styles["BodyText"], fontSize=8,
                                   leading=10, textColor=colors.white, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(path, pagesize=landscape(A3),
                             leftMargin=24, rightMargin=24, topMargin=24, bottomMargin=24)
    elements = [Paragraph(title, styles["Title"]), Spacer(1, 12)]

    table_data = [[Paragraph(str(h), header_style) for h in headers]]
    for row in rows:
        table_data.append([Paragraph(str(c) if c is not None else "", cell_style) for c in row])

    col_width = (landscape(A3)[0] - 48) / len(headers)
    table = Table(table_data, colWidths=[col_width] * len(headers), repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#333333")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
    ]))
    elements.append(table)
    doc.build(elements)


VERSION_RE = None  # compiled lazily; see get_node_version


def get_node_version(host, ssh_user="rubix", remote_dir="~/Desktop/rubix", timeout=8):
    """Read one node's Rubix build over SSH.

    There is NO version API - checked every route in server/server.go. The
    only source is `./rubixgoplatform -v`, which prints and exits without
    touching the running node, so it is safe while the service is live.
    Same mechanism as controller/node-versions.py.

    This matters for the mixed-fleet cases (GEN): sender, receiver and quorum
    can legitimately be on DIFFERENT builds, and a result is meaningless
    unless the report says which build each role was actually running.

    Returns (version_or_None, note).
    """
    global VERSION_RE
    if VERSION_RE is None:
        import re
        VERSION_RE = re.compile(r"Rubix Core Version\s*:\s*(\S+)")
    import subprocess
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout={}".format(timeout),
        "{}@{}".format(ssh_user, host),
        "cd {} && ./rubixgoplatform -v".format(remote_dir),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except subprocess.TimeoutExpired:
        return None, "ssh timeout"
    except FileNotFoundError:
        return None, "ssh not available on this controller"
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        return None, "ssh failed: {}".format(err[-1] if err else proc.returncode)
    m = VERSION_RE.search(proc.stdout)
    return (m.group(1), "") if m else (None, "version line not found")


def collect_versions(hosts, ssh_user="rubix", remote_dir="~/Desktop/rubix", workers=20):
    """Versions for many hosts at once -> {host: version_or_error_string}."""
    from concurrent.futures import ThreadPoolExecutor

    def one(h):
        v, note = get_node_version(h, ssh_user, remote_dir)
        return h, (v or "UNKNOWN ({})".format(note))

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(hosts)))) as ex:
        return dict(ex.map(one, hosts))


def close_enough(a, b, tol=0.0015):
    """3-decimal-place precision (math/math.go FloatPrecision) leaves room
    for float rounding - compare with a small tolerance, never ==."""
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def wait_for_balance(host, did, min_amount, port=DEFAULT_PORT, attempts=10, delay=2):
    """Poll RBT balance until it clears min_amount or attempts run out.
    Minting is asynchronous relative to when the signature call returns."""
    for _ in range(attempts):
        ok, bal, _ = get_rbt_balance(host, did, port)
        if ok and bal is not None and bal >= min_amount:
            return True, bal
        time.sleep(delay)
    ok, bal, _ = get_rbt_balance(host, did, port)
    return False, bal


def ft_count_for(ft_balance_result, ft_name):
    """Pull the count for one FT series out of what get_ft_balance returns
    (a list of {name, creator, value, count} dicts). 0 if absent."""
    if not isinstance(ft_balance_result, list):
        return 0
    for entry in ft_balance_result:
        if isinstance(entry, dict) and entry.get("name") == ft_name:
            try:
                return int(entry.get("count") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def wait_for_ft_count(host, did, ft_name, min_count, port=DEFAULT_PORT, attempts=15, delay=1):
    """Poll a DID's FT balance until `ft_name` reaches min_count.

    A receiver credits incoming tokens asynchronously - the sender's
    transaction can return success well before the receiving node has
    processed them, so checking the receiver once immediately reports a
    false empty. Returns (reached, actual_count, raw_result)."""
    result = None
    for _ in range(attempts):
        ok, result, _ = get_ft_balance(host, did, port)
        count = ft_count_for(result, ft_name)
        if ok and count >= min_count:
            return True, count, result
        time.sleep(delay)
    ok, result, _ = get_ft_balance(host, did, port)
    return False, ft_count_for(result, ft_name), result
