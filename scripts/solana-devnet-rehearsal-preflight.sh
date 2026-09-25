#!/usr/bin/env bash
# Public, read-only preparation for an exact-artifact Devnet rehearsal.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$ROOT/audit/solana/releases/2026-09-07-production-candidate/manifest.json"
ARTIFACT_DIR="$(dirname "$MANIFEST")/artifacts"
RPC_CHECK=0
PAYER=""
ADAPTER=""
HOOK=""
TRADER=""
MINT=""
TLAMA_VAULT=""
WSOL_VAULT=""

usage() {
  cat <<'USAGE'
Usage: scripts/solana-devnet-rehearsal-preflight.sh \
  --payer-keypair FILE --adapter-program-keypair FILE --hook-program-keypair FILE \
  --trader-keypair FILE \
  --tlama-mint-keypair FILE --tlama-vault-keypair FILE --wsol-vault-keypair FILE \
  [--public-rpc-check]

Default mode is offline preflight. --public-rpc-check makes read-only Devnet RPC
queries; it never signs, funds, deploys, or builds a transaction.
Only keypair file paths may be supplied as arguments. Do not pipe data or place
key material in environment variables.
USAGE
}

reject_secret_environment() {
  local name
  while IFS='=' read -r name _; do
    case "$name" in
      *_KEYPAIR|*_PRIVATE_KEY|*_SECRET|*_MNEMONIC|*_SEED|SOLANA_KEYPAIR)
        echo "refusing possible secret environment variable" >&2
        exit 2
        ;;
    esac
  done < <(env)
}

reject_piped_input() {
  # A pipe is unambiguously an attempted stdin channel. Never consume it.
  if [[ -p /dev/stdin ]]; then
    echo "refusing stdin; supply only keypair file paths as CLI arguments" >&2
    exit 2
  fi
}

require_keypair_path() {
  local path="$1"
  [[ -n "$path" && -f "$path" && -r "$path" ]] || {
    echo "keypair path is not a readable regular file" >&2
    exit 2
  }
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --payer-keypair) PAYER="${2:-}"; shift 2 ;;
    --adapter-program-keypair) ADAPTER="${2:-}"; shift 2 ;;
    --hook-program-keypair) HOOK="${2:-}"; shift 2 ;;
    --trader-keypair) TRADER="${2:-}"; shift 2 ;;
    --tlama-mint-keypair) MINT="${2:-}"; shift 2 ;;
    --tlama-vault-keypair) TLAMA_VAULT="${2:-}"; shift 2 ;;
    --wsol-vault-keypair) WSOL_VAULT="${2:-}"; shift 2 ;;
    --output) shift 2 ;;
    --public-rpc-check) RPC_CHECK=1; shift ;;
    --help) usage; exit 0 ;;
    *) echo "unrecognized argument; only documented flags and file paths are accepted" >&2; usage >&2; exit 2 ;;
  esac
done

reject_secret_environment
reject_piped_input
[[ -n "$PAYER" && -n "$TRADER" && -n "$ADAPTER" && -n "$HOOK" && -n "$MINT" && -n "$TLAMA_VAULT" && -n "$WSOL_VAULT" ]] || { usage >&2; exit 2; }
require_keypair_path "$PAYER"
require_keypair_path "$TRADER"
require_keypair_path "$ADAPTER"
require_keypair_path "$HOOK"
require_keypair_path "$MINT"
require_keypair_path "$TLAMA_VAULT"
require_keypair_path "$WSOL_VAULT"

CLI=""
for candidate in solana agave; do
  if command -v "$candidate" >/dev/null 2>&1; then CLI="$candidate"; break; fi
done
[[ -n "$CLI" ]] || { echo "Solana/Agave CLI is required" >&2; exit 2; }
"$CLI" --version | grep -Eq '(solana-cli|agave-cli)[[:space:]]+2\.2\.1([[:space:]]|$)' || {
  echo "Solana/Agave CLI version 2.2.1 is required" >&2; exit 2;
}

python3 "$ROOT/audit/solana/verify_artifact_manifest.py" "$MANIFEST" "$ARTIFACT_DIR"

# solana-keygen receives paths directly; this script never opens, copies, or
# prints keypair contents. It prints only derived public addresses.
KEYGEN="$(command -v solana-keygen || true)"
[[ -n "$KEYGEN" ]] || { echo "solana-keygen is required" >&2; exit 2; }
payer_pubkey="$("$KEYGEN" pubkey "$PAYER" 2>/dev/null)" || { echo "unable to derive payer public key" >&2; exit 2; }
trader_pubkey="$("$KEYGEN" pubkey "$TRADER" 2>/dev/null)" || { echo "unable to derive trader public key" >&2; exit 2; }
adapter_pubkey="$("$KEYGEN" pubkey "$ADAPTER" 2>/dev/null)" || { echo "unable to derive adapter public key" >&2; exit 2; }
hook_pubkey="$("$KEYGEN" pubkey "$HOOK" 2>/dev/null)" || { echo "unable to derive hook public key" >&2; exit 2; }
mint_pubkey="$("$KEYGEN" pubkey "$MINT" 2>/dev/null)" || { echo "unable to derive mint public key" >&2; exit 2; }
tlama_vault_pubkey="$("$KEYGEN" pubkey "$TLAMA_VAULT" 2>/dev/null)" || { echo "unable to derive TLAMA vault public key" >&2; exit 2; }
wsol_vault_pubkey="$("$KEYGEN" pubkey "$WSOL_VAULT" 2>/dev/null)" || { echo "unable to derive WSOL vault public key" >&2; exit 2; }

read_manifest_id() {
  python3 - "$MANIFEST" "$1" <<'PY'
import json, sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["ids"][sys.argv[2]])
PY
}
[[ "$adapter_pubkey" == "$(read_manifest_id adapter)" ]] || { echo "adapter public key does not match manifest" >&2; exit 2; }
[[ "$hook_pubkey" == "$(read_manifest_id hook)" ]] || { echo "hook public key does not match manifest" >&2; exit 2; }
[[ "$mint_pubkey" == "$(read_manifest_id tlamaMint)" ]] || { echo "mint public key does not match manifest" >&2; exit 2; }
[[ "$tlama_vault_pubkey" == "$(read_manifest_id tlamaVault)" ]] || { echo "TLAMA vault public key does not match manifest" >&2; exit 2; }
[[ "$wsol_vault_pubkey" == "$(read_manifest_id wsolVault)" ]] || { echo "WSOL vault public key does not match manifest" >&2; exit 2; }
[[ "$trader_pubkey" != "$payer_pubkey" ]] || { echo "trader must be separate from the writable fee payer" >&2; exit 2; }

echo "offline preflight passed: artifact hashes and program public keys match"
echo "payer public key: $payer_pubkey"
if [[ "$RPC_CHECK" -eq 0 ]]; then
  echo "dry-run only: no RPC queries, signing, transaction construction, funding, or deployment occurred"
  exit 0
fi

"$CLI" --url https://api.devnet.solana.com cluster-version >/dev/null
echo "Devnet cluster verified"
"$CLI" --url https://api.devnet.solana.com balance "$payer_pubkey"
# Read-only minimum-balance estimates for the exact program ELF byte sizes.
# These are funding inputs, not a transaction plan.
"$CLI" --url https://api.devnet.solana.com rent "$(wc -c < "$ARTIFACT_DIR/tlama_pool_adapter.so")"
"$CLI" --url https://api.devnet.solana.com rent "$(wc -c < "$ARTIFACT_DIR/tlama_transfer_hook.so")"
echo "public RPC checks passed; no transaction was submitted"