#!/usr/bin/env python3
"""
rubix_client.py - Shared helpers for the test-plan scripts. Not a script to
run directly - imported by preflight.py and each test-plan/<asset>/ runner.

Core primitive (confirmed against server/*.go in the rubixgoplatform repo):
almost every mutating call is a 2-step password challenge:
    POST <action>            -> {"result": {"id": "<reqID>"}}   (password needed)
    POST /rubix/v1/signature  body {"id": "<reqID>", "password": DID_PASSWORD}
                              -> final {"status": bool, "message": ..., "result": ...}
RegisterDID, GenerateLocalRBT and InitiateTransaction (RBT/FT/NFT/SC all go
through the one /rubix/v1/transaction body) all follow this. signed_action()
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
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    report_dir = os.path.join(repo_root, "reports", ext)
    os.makedirs(report_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return os.path.join(report_dir, "{}_{}.{}".format(script_name, timestamp, ext))


def new_report_paths(script_name, exts=("pdf", "json")):
    """Paths for every format of ONE run, sharing a single timestamp so the
    files are matched. Returns {ext: path}."""
    repo_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
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
EP_GENERATE_LOCAL_RBT = "/rubix/v1/tokens/generate_local_rbt"
EP_QUORUM_SETUP = "/rubix/v1/quorums/setup"
EP_QUORUM_ADD = "/rubix/v1/quorums/add"
EP_QUORUM_LIST = "/rubix/v1/quorums"
EP_TRANSACTION = "/rubix/v1/tx"  # confirmed setup.go:69 - NOT /rubix/v1/transaction
EP_FT_MINT = "/rubix/v1/fts/mint"
EP_FT_BALANCE = "/rubix/v1/dids/{did}/balances/ft"
EP_CREATE_NFT = "/rubix/v1/nfts/generate"
EP_GENERATE_SC = "/rubix/v1/smart_contracts/generate"


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


def signed_action(host, action_path, body, port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
    """
    POST an action that needs the password-challenge round trip.
    Returns (status: bool, message: str, result: any).
    """
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


# --- Fleet-wide token index registry -----------------------------------
# generate_local_rbt's token IDs are "<level>_<numberInLevel>", derived from
# a flat integer index (core/token.go GetTokenLevelAndNumberForGlobalIndex).
# start_index=0 asks the SERVER for a safe, atomic, but PER-NODE counter
# that starts at 1 on every node independently - so node A's 12th local
# mint and node B's 12th local mint are both literally called "10001_12".
# That's harmless in isolation, but once a transaction touches a shared
# quorum, TokenChainIntigrityCheck (core/consensus/checks.go) looks the
# token up by that bare ID with NO per-DID scoping
# (GetLatestTransactionIdByTokenId(tokenID, ...)) - if the quorum has ITS
# OWN unrelated local token with the same ID, it finds that instead of the
# sender's, and correctly reports a chain mismatch. Confirmed by evidence:
# every failing transfer's "local latest" hash matched exactly by which
# quorum was involved, not which sender.
#
# Fix: never use start_index=0 for fleet minting. Instead allocate a
# strictly increasing GLOBAL range ourselves, seeded far above anything any
# node could plausibly have minted via the old start_index=0 path, so no
# two nodes anywhere in the fleet - across any script, any run - ever
# produce the same token ID.
#
# The registry file is the only record of "how far allocated so far" -
# unlike dids.xlsx/inventory.json it can't be regenerated from the live
# network, so treat it like DID key material: don't delete it, and if it's
# ever lost, re-seed well above the highest index any node has reached
# rather than restarting at the same seed (that would just reintroduce the
# same collision for everything minted since).
TOKEN_INDEX_REGISTRY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "token_index_registry.json")
TOKEN_INDEX_SEED = 10_000_000  # comfortably above anything minted so far fleet-wide


def allocate_token_index_range(count, registry_path=TOKEN_INDEX_REGISTRY_PATH):
    """Reserve `count` consecutive global indices, atomically-enough for a
    lab that runs one test cycle at a time (simple read-modify-write, no
    file lock - if you ever run two mint-heavy scripts concurrently against
    the same fleet, that assumption breaks). Returns the first index of the
    reserved range - pass it as start_index to generate_local_rbt."""
    if os.path.exists(registry_path):
        with open(registry_path, encoding="utf-8") as fh:
            state = json.load(fh)
        next_index = state.get("next_index", TOKEN_INDEX_SEED)
    else:
        next_index = TOKEN_INDEX_SEED

    start = next_index
    with open(registry_path, "w", encoding="utf-8") as fh:
        json.dump({"next_index": start + count,
                   "last_allocated_at": datetime.datetime.now().isoformat(timespec="seconds")}, fh, indent=2)
    return start


def fund_did(host, did, amount, port=DEFAULT_PORT):
    """Mint local RBT for a DID, using a fleet-wide-unique index range (see
    allocate_token_index_range above - never start_index=0 here). Returns
    (status, message)."""
    start_index = allocate_token_index_range(int(amount))
    body = {"did": did, "number_of_tokens": int(amount), "start_index": start_index}
    status, message, _ = signed_action(host, EP_GENERATE_LOCAL_RBT, body, port)
    return status, message


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
                          memo="", port=DEFAULT_PORT, timeout=SIGNATURE_TIMEOUT):
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

    Returns (status, message, result).
    """
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
