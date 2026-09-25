#!/usr/bin/env python3
"""Strict verifier for sanitized, public exact-artifact Devnet evidence."""
import argparse
import json
import pathlib
import re
import sys
import base64
import hashlib
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import verify_artifact_manifest as artifacts

SCENARIOS = ("adapterDeployment", "hookDeployment", "initialize", "buy", "sell",
             "capRejection", "vaultHookRejection", "aliasedBuyRejection",
             "adapterFinalization", "hookFinalization")
SNAPSHOTS = ("adapter", "hook", "tlamaMint", "tlamaVault", "wsolVault", "poolAuthority")
SHA = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
FORBIDDEN = re.compile(r"(secret|private|seed|mnemonic|keypair|passphrase|path)", re.I)

def reject(message):
    raise SystemExit(f"Devnet evidence rejected: {message}")

def exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        reject(f"{label} must contain exactly: {', '.join(keys)}")
    return value

def public_key(value):
    return artifacts.valid_pubkey(value)

def no_secret_fields(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN.search(key):
                reject("secret-like field is forbidden")
            no_secret_fields(child)
    elif isinstance(value, list):
        for child in value:
            no_secret_fields(child)

REJECTIONS = ("capRejection", "vaultHookRejection", "aliasedBuyRejection")
EXPECTED_REJECTION_CODES = {
    "capRejection": 5,
    "vaultHookRejection": 8,
    "aliasedBuyRejection": 9,
}
SYSTEM_PROGRAM = "11111111111111111111111111111111"
BPF_LOADER = "BPFLoader2111111111111111111111111111111111"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
INSTRUCTIONS_SYSVAR = "Sysvar1nstructions1111111111111111111111111"

def signature_entry(value, name):
    exact(value, ("signature", "slot", "confirmationStatus", "outcome"), f"signature {name}")
    # Signatures are base58-encoded 64-byte values.
    if not _base58_bytes(value["signature"], 64):
        reject(f"signature is not a canonical 64-byte base58 value: {name}")
    if not isinstance(value["slot"], int) or value["slot"] < 1:
        reject(f"signature slot is invalid: {name}")
    if value["confirmationStatus"] != "finalized":
        reject(f"signature is not finalized: {name}")
    expected = "rejected" if name in REJECTIONS else "successful"
    if value["outcome"] != expected:
        reject(f"signature outcome must be {expected}: {name}")

def _base58_bytes(value, length):
    if not isinstance(value, str) or not value:
        return False
    try:
        number = 0
        for char in value:
            number = number * 58 + artifacts.BASE58_ALPHABET.index(char)
    except ValueError:
        return False
    return len(number.to_bytes((number.bit_length() + 7) // 8, "big")) + len(value) - len(value.lstrip("1")) == length

def _base58_decode(value):
    number = 0
    for char in value:
        number = number * 58 + artifacts.BASE58_ALPHABET.index(char)
    body = number.to_bytes((number.bit_length() + 7) // 8, "big")
    return b"\0" * (len(value) - len(value.lstrip("1"))) + body

def _top_instruction(message, label):
    instructions = message.get("instructions")
    keys = [
        item if isinstance(item, str) else item.get("pubkey")
        for item in message.get("accountKeys", [])
    ]
    if not isinstance(instructions, list) or len(instructions) != 1:
        reject(f"{label} must contain exactly one top-level instruction")
    item = instructions[0]
    try:
        return keys[item["programIdIndex"]], [keys[index] for index in item["accounts"]], _base58_decode(item["data"])
    except (IndexError, KeyError, TypeError, ValueError):
        reject(f"{label} instruction is malformed")

def _require_accounts(actual, expected, label):
    if len(actual) != len(expected):
        reject(f"{label} account layout has the wrong length")
    for index, (got, wanted) in enumerate(zip(actual, expected)):
        if wanted is not None and got != wanted:
            reject(f"{label} account layout mismatch at position {index}")

def _pool_data(tag, first, second):
    return tag + first.to_bytes(8, "little") + second.to_bytes(8, "little")

def validate_transaction(name, message, evidence):
    program, accounts, data = _top_instruction(message, name)
    ids = evidence["accountSnapshots"]
    adapter = evidence["manifest"]["programIds"]["adapter"]
    hook = evidence["manifest"]["programIds"]["hook"]
    if name in ("adapterDeployment", "hookDeployment"):
        target = adapter if name.startswith("adapter") else hook
        if program != SYSTEM_PROGRAM or target not in accounts or data[:4] != b"\0\0\0\0":
            reject(f"{name} is not the exact program-account creation")
        return
    if name in ("adapterFinalization", "hookFinalization"):
        target = adapter if name.startswith("adapter") else hook
        if program != BPF_LOADER or not accounts or accounts[0] != target or data != b"\1\0\0\0":
            reject(f"{name} is not the immutable loader finalization")
        return
    if name == "initialize":
        if program != hook or data != b"TLAMINIT":
            reject("initialize instruction mismatch")
        _require_accounts(accounts, [None, None, ids["tlamaMint"]["address"], SYSTEM_PROGRAM], name)
        return
    if name == "vaultHookRejection":
        if program != hook or data != bytes((105, 37, 101, 197, 75, 251, 102, 26, 1, 0, 0, 0, 0, 0, 0, 0)):
            reject("vault Hook rejection instruction mismatch")
        _require_accounts(accounts, [ids["tlamaVault"]["address"], ids["tlamaMint"]["address"],
                                      None, ids["poolAuthority"]["address"], None,
                                      INSTRUCTIONS_SYSVAR], name)
        return
    expected_data = {
        "buy": _pool_data(b"TLAMABUY", 1_000_000, 1),
        "sell": _pool_data(b"TLAMASEL", 2_000_000_000, 1),
        "capRejection": _pool_data(b"TLAMABUY", 5_000_000 * 1_000_000_000, 1),
        "aliasedBuyRejection": _pool_data(b"TLAMABUY", 1, 1),
    }[name]
    if program != adapter or data != expected_data:
        reject(f"{name} instruction data mismatch")
    if name == "sell":
        roles = [None, ids["tlamaVault"]["address"], ids["wsolVault"]["address"], None]
    else:
        roles = [None, ids["wsolVault"]["address"], ids["tlamaVault"]["address"], None]
    if name == "aliasedBuyRejection":
        roles[1] = None
    expected = [None, *roles, ids["tlamaMint"]["address"], ids["poolAuthority"]["address"],
                None, INSTRUCTIONS_SYSVAR, TOKEN_2022, TOKEN, hook]
    _require_accounts(accounts, expected, name)
    if name == "aliasedBuyRejection" and accounts[1] != accounts[2]:
        reject("aliased buy does not actually alias the source and vault")

def rpc_call(rpc_url, method, params):
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except Exception as error:
        reject(f"Devnet RPC verification failed: {error}")
    if payload.get("error") or "result" not in payload:
        reject(f"Devnet RPC rejected {method}")
    return payload["result"]


def verify_rpc(evidence, rpc_url):
    if rpc_url.rstrip("/") != "https://api.devnet.solana.com":
        reject("only the canonical Solana Devnet RPC is accepted")
    names = list(SCENARIOS)
    signatures = [evidence["signatures"][name]["signature"] for name in names]
    result = rpc_call(
        rpc_url,
        "getSignatureStatuses",
        [signatures, {"searchTransactionHistory": True}],
    )
    statuses = result.get("value", [])
    if len(statuses) != len(signatures):
        reject("Devnet RPC returned an incomplete signature status set")
    for name, expected, status in zip(names, signatures, statuses):
        if (
            status is None
            or status.get("confirmationStatus") != "finalized"
            or status.get("slot") != evidence["signatures"][name]["slot"]
        ):
            reject(f"Devnet RPC did not confirm finalized signature: {name}")
        rejected = name in REJECTIONS
        if (status.get("err") is not None) != rejected:
            reject(f"Devnet RPC outcome does not match expected scenario: {name}")
        if rejected:
            expected_error = {"InstructionError": [0, {"Custom": EXPECTED_REJECTION_CODES[name]}]}
            if status.get("err") != expected_error:
                reject(f"Devnet RPC returned the wrong rejection error: {name}")

        transaction = rpc_call(
            rpc_url,
            "getTransaction",
            [expected, {"encoding": "json", "commitment": "finalized",
                        "maxSupportedTransactionVersion": 0}],
        )
        if transaction is None:
            reject(f"Devnet RPC transaction is absent: {name}")
        message = transaction.get("transaction", {}).get("message", {})
        validate_transaction(name, message, evidence)
        if transaction.get("slot") != evidence["signatures"][name]["slot"]:
            reject(f"transaction slot does not match evidence: {name}")

    final_scenario_slot = evidence["signatures"]["aliasedBuyRejection"]["slot"]
    for name, snapshot in evidence["accountSnapshots"].items():
        if snapshot["slot"] < final_scenario_slot:
            reject(f"account snapshot predates the completed campaign: {name}")
        result = rpc_call(
            rpc_url,
            "getAccountInfo",
            [snapshot["address"], {"encoding": "base64", "commitment": "finalized"}],
        )
        value = result.get("value")
        if value is None or result.get("context", {}).get("slot", 0) < snapshot["slot"]:
            reject(f"Devnet RPC account snapshot is absent or older than evidence: {name}")
        encoded = value.get("data")
        if not isinstance(encoded, list) or len(encoded) < 1:
            reject(f"Devnet RPC account data is malformed: {name}")
        try:
            data_hash = hashlib.sha256(base64.b64decode(encoded[0], validate=True)).hexdigest()
        except (ValueError, TypeError):
            reject(f"Devnet RPC account data is not valid base64: {name}")
        if (
            value.get("owner") != snapshot["owner"]
            or value.get("lamports") != snapshot["lamports"]
            or data_hash != snapshot["dataSha256"]
        ):
            reject(f"Devnet RPC account snapshot does not match evidence: {name}")


def verify(evidence, manifest, artifact_dir, root=artifacts.ROOT, rpc_url=None):
    no_secret_fields(evidence)
    artifacts.verify(manifest, artifact_dir, root)
    exact(evidence, ("schemaVersion", "cluster", "manifest", "signatures",
                     "accountSnapshots", "scenarios"), "evidence")
    if evidence["schemaVersion"] != 1 or evidence["cluster"] != "devnet":
        reject("schemaVersion 1 and cluster devnet are required")
    bound = exact(evidence["manifest"], ("sourceCommit", "artifactHashes", "programIds"),
                  "manifest binding")
    if bound["sourceCommit"] != manifest.get("sourceCommit") or not COMMIT.fullmatch(bound["sourceCommit"]):
        reject("source commit does not match exact artifact manifest")
    if bound["artifactHashes"] != manifest.get("artifacts"):
        reject("artifact hashes do not match exact artifact manifest")
    if bound["programIds"] != {k: manifest["ids"][k] for k in ("adapter", "hook")}:
        reject("program IDs do not match exact artifact manifest")
    signatures = exact(evidence["signatures"], SCENARIOS, "signatures")
    for name in SCENARIOS: signature_entry(signatures[name], name)
    if len({item["signature"] for item in signatures.values()}) != len(SCENARIOS):
        reject("every scenario must have a distinct signature")
    chronology = ("adapterDeployment", "adapterFinalization", "hookDeployment",
                  "hookFinalization", "initialize", "buy", "sell", "capRejection",
                  "vaultHookRejection", "aliasedBuyRejection")
    if any(signatures[a]["slot"] > signatures[b]["slot"]
           for a, b in zip(chronology, chronology[1:])):
        reject("scenario chronology is invalid")
    snapshots = exact(evidence["accountSnapshots"], SNAPSHOTS, "account snapshots")
    expected_owners = {
        "adapter": BPF_LOADER,
        "hook": BPF_LOADER,
        "tlamaMint": TOKEN_2022,
        "tlamaVault": TOKEN_2022,
        "wsolVault": TOKEN,
        "poolAuthority": SYSTEM_PROGRAM,
    }
    for name in SNAPSHOTS:
        item = exact(snapshots[name], ("address", "owner", "lamports", "dataSha256",
                                       "slot", "confirmationStatus"), f"account snapshot {name}")
        if item["address"] != manifest["ids"][name] or not public_key(item["owner"]):
            reject(f"account snapshot public key mismatch: {name}")
        if not isinstance(item["lamports"], int) or item["lamports"] < 0 or not isinstance(item["slot"], int) or item["slot"] < 1:
            reject(f"account snapshot numeric value invalid: {name}")
        if not isinstance(item["dataSha256"], str) or not SHA.fullmatch(item["dataSha256"]):
            reject(f"account snapshot data hash invalid: {name}")
        if item["confirmationStatus"] != "finalized":
            reject(f"account snapshot is not finalized: {name}")
        if item["owner"] != expected_owners[name]:
            reject(f"account snapshot owner mismatch: {name}")
    if snapshots["adapter"]["dataSha256"] != manifest["artifacts"]["tlama_pool_adapter.so"]:
        reject("deployed adapter snapshot does not match the frozen ELF")
    if snapshots["hook"]["dataSha256"] != manifest["artifacts"]["tlama_transfer_hook.so"]:
        reject("deployed hook snapshot does not match the frozen ELF")
    scenarios = exact(evidence["scenarios"], SCENARIOS, "scenarios")
    if any(result is not True for result in scenarios.values()):
        reject("every required scenario must explicitly be true")
    if rpc_url is not None:
        verify_rpc(evidence, rpc_url)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence")
    parser.add_argument("manifest")
    parser.add_argument("artifact_dir")
    parser.add_argument(
        "--rpc-url",
        required=True,
        help="Canonical Devnet RPC; required so documentary evidence cannot clear the gate alone.",
    )
    args = parser.parse_args()
    try:
        evidence = json.loads(pathlib.Path(args.evidence).read_text(encoding="utf-8"))
        manifest = json.loads(pathlib.Path(args.manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        reject(str(error))
    verify(evidence, manifest, pathlib.Path(args.artifact_dir), rpc_url=args.rpc_url)
    print("sanitized Devnet evidence and finalized public ledger state verified")

if __name__ == "__main__":
    main()