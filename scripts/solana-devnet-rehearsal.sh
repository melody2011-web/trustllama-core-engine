#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -t 0 ]] || { echo "refusing stdin" >&2; exit 2; }
bash "$ROOT/scripts/solana-devnet-rehearsal-preflight.sh" "$@" --public-rpc-check
pnpm --dir "$ROOT" exec tsx "$ROOT/scripts/src/solana-devnet-rehearsal.ts" "$@"