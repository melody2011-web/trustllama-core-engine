#!/usr/bin/env bash
set -euo pipefail

# This script contains no secrets. Copy it to an offline Linux machine before
# running it. It intentionally refuses to create mainnet custody keys on Replit.

umask 077

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

if [[ -n "${REPL_ID:-}" || -n "${REPLIT_DEV_DOMAIN:-}" || -n "${REPL_SLUG:-}" ]]; then
  die "refusing to generate mainnet keypairs on Replit; run this script on an offline machine"
fi

if [[ "$#" -ne 1 ]]; then
  die "usage: $0 /absolute/path/to/new-private-key-directory"
fi

KEYGEN="${SOLANA_KEYGEN:-solana-keygen}"
command -v "$KEYGEN" >/dev/null 2>&1 ||
  die "solana-keygen was not found; install and verify the intended Solana CLI offline"

DESTINATION="$1"
[[ "$DESTINATION" = /* ]] ||
  die "the private-key directory must be an absolute path"
[[ ! -e "$DESTINATION" ]] ||
  die "the destination already exists; refusing to overwrite or mix key material"

PARENT="$(dirname "$DESTINATION")"
[[ -d "$PARENT" ]] ||
  die "the destination parent directory does not exist"
[[ ! -L "$PARENT" ]] ||
  die "the destination parent must not be a symbolic link"

DESTINATION="$(realpath -m "$DESTINATION")"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
case "$DESTINATION/" in
  "$SCRIPT_ROOT/"*)
    die "private keypairs must be stored outside the project workspace"
    ;;
esac

install -d -m 700 "$DESTINATION"
[[ "$(stat -c '%a' "$DESTINATION")" == "700" ]] ||
  die "could not enforce mode 700 on the private-key directory"

declare -a NAMES=(
  "adapter-program"
  "transfer-hook-program"
  "tlama-mint"
  "tlama-vault"
  "wsol-vault"
)

for name in "${NAMES[@]}"; do
  key_file="$DESTINATION/$name.json"
  if ! "$KEYGEN" new \
    --no-bip39-passphrase \
    --silent \
    --force \
    --outfile "$key_file" >/dev/null 2>&1; then
    die "key generation failed; protected partial files may remain in the destination"
  fi
  chmod 600 "$key_file"
  [[ ! -L "$key_file" && "$(stat -c '%a' "$key_file")" == "600" ]] ||
    die "could not enforce mode 600 on a generated keypair"
done

declare -A LABELS=(
  ["adapter-program"]="adapter"
  ["transfer-hook-program"]="hook"
  ["tlama-mint"]="tlamaMint"
  ["tlama-vault"]="tlamaVault"
  ["wsol-vault"]="wsolVault"
)

for name in "${NAMES[@]}"; do
  public_key="$("$KEYGEN" pubkey "$DESTINATION/$name.json" 2>/dev/null)" ||
    die "could not derive a public address from a generated keypair"
  [[ "$public_key" =~ ^[1-9A-HJ-NP-Za-km-z]{32,44}$ ]] ||
    die "solana-keygen returned an invalid public address"
  printf '%s=%s\n' "${LABELS[$name]}" "$public_key"
done