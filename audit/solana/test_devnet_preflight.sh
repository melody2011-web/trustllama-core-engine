#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$ROOT/scripts/solana-devnet-rehearsal-preflight.sh"

if echo '[1,2,3]' | bash "$script" --payer-keypair x --adapter-program-keypair x --hook-program-keypair x 2>/dev/null; then
  echo "piped input was accepted" >&2; exit 1
fi
if TLAMA_DEVNET_KEYPAIR='[1,2,3]' bash "$script" --payer-keypair x --adapter-program-keypair x --hook-program-keypair x 2>/dev/null; then
  echo "secret environment was accepted" >&2; exit 1
fi
if bash "$script" --unknown 2>/dev/null; then
  echo "unknown argument was accepted" >&2; exit 1
fi
echo "Devnet preflight secret rejection tests passed"