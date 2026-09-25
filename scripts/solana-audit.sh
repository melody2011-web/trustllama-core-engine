#!/usr/bin/env bash
set -u
set -o pipefail

MODE="quick"
DEVNET_EVIDENCE=""
DEVNET_RPC_URL=""
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPORT="$ROOT/audit/solana/READINESS.md"
POOL="$ROOT/onchain/tlama-pool-hook/Cargo.toml"
HOOK="$ROOT/onchain/tlama-transfer-hook/Cargo.toml"
LOG_DIR="$(mktemp -d)"
trap 'rm -rf "$LOG_DIR"' EXIT

select_host_rust() {
  if cargo --version >/dev/null 2>&1 &&
    rustc --version >/dev/null 2>&1 &&
    rustfmt --version >/dev/null 2>&1 &&
    cargo fmt --version >/dev/null 2>&1; then
    return
  fi

  local candidate bin_dir
  while IFS= read -r candidate; do
    [[ "$candidate" == *rustup* ]] && continue
    bin_dir="$(dirname "$candidate")"
    if "$candidate" --version >/dev/null 2>&1 &&
      "$bin_dir/rustc" --version >/dev/null 2>&1 &&
      "$bin_dir/rustfmt" --version >/dev/null 2>&1 &&
      [[ -x "$bin_dir/cargo-fmt" ]]; then
      export PATH="$bin_dir:$PATH"
      return
    fi
  done < <(which -a cargo 2>/dev/null | awk '!seen[$0]++')

  echo "No working host Cargo/Rust compiler is available." >&2
  exit 2
}

select_sbf_host_rust() {
  local replay_toolchain="${TLAMA_SBF_HOST_TOOLCHAIN:-1.93.0}"
  if env -u LD_AUDIT rustup run "$replay_toolchain" cargo --version >/dev/null 2>&1 &&
    env -u LD_AUDIT rustup run "$replay_toolchain" rustc --version >/dev/null 2>&1; then
    local replay_cargo
    replay_cargo="$(env -u LD_AUDIT rustup which --toolchain "$replay_toolchain" cargo)"
    export PATH="$(dirname "$replay_cargo"):$PATH"
    export RUSTUP_TOOLCHAIN="$replay_toolchain"
    unset RUSTC_REAL
  else
    local cargo_bin rustc_bin
    cargo_bin="$(command -v cargo)"
    rustc_bin="$(command -v rustc)"
    if [[ "$cargo_bin" == *rustup* || "$rustc_bin" == *rustup* ]] ||
      ! "$cargo_bin" --version >/dev/null 2>&1 ||
      ! env -u LD_AUDIT "$rustc_bin" --version >/dev/null 2>&1; then
      echo "A managed stable Rust toolchain is required for SBF replay." >&2
      exit 2
    fi
    unset RUSTUP_TOOLCHAIN
    export RUSTC_REAL="$rustc_bin"
  fi
  export RUSTC="$ROOT/scripts/rustc-stable-no-ld-audit.sh"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    quick|sbf) MODE="$1"; shift ;;
    --devnet-evidence)
      DEVNET_EVIDENCE="${2:-}"
      [[ -n "$DEVNET_EVIDENCE" ]] || { echo "--devnet-evidence requires a path" >&2; exit 2; }
      shift 2
      ;;
    --devnet-rpc-url)
      DEVNET_RPC_URL="${2:-}"
      [[ -n "$DEVNET_RPC_URL" ]] || { echo "--devnet-rpc-url requires a URL" >&2; exit 2; }
      shift 2
      ;;
    *)
      echo "Usage: $0 [quick|sbf] [--devnet-evidence FILE --devnet-rpc-url https://api.devnet.solana.com]" >&2
      exit 2
      ;;
  esac
done

select_host_rust

passed=0
failed=0
blocked=0
requested_mode_unavailable=0
production_release_verified=0
production_rebuild_verified=0
rows=()

run_check() {
  local name="$1"
  shift
  local slug
  slug="$(printf '%s' "$name" | tr -cs '[:alnum:]' '-' | tr '[:upper:]' '[:lower:]')"
  if "$@" >"$LOG_DIR/$slug.log" 2>&1; then
    rows+=("| PASS | $name |")
    passed=$((passed + 1))
  else
    rows+=("| FAIL | $name |")
    failed=$((failed + 1))
    echo "FAILED: $name" >&2
    tail -n 30 "$LOG_DIR/$slug.log" >&2
  fi
}

block_check() {
  rows+=("| BLOCKED | $1 |")
  blocked=$((blocked + 1))
}

cd "$ROOT"
run_check "Pool adapter formatting" cargo fmt --check --manifest-path "$POOL"
run_check "Transfer Hook formatting" cargo fmt --check --manifest-path "$HOOK"
run_check "Pool adapter locked unit tests" cargo test --locked --lib --manifest-path "$POOL"
run_check "Transfer Hook locked tests" cargo test --locked --manifest-path "$HOOK"
run_check "Pool adapter release host build" cargo build --locked --release --manifest-path "$POOL"
run_check "Transfer Hook release host build" cargo build --locked --release --manifest-path "$HOOK"

if [[ "$MODE" == "sbf" ]]; then
  select_sbf_host_rust
  if [[ -z "${SBF_OUT_DIR:-}" ]]; then
    block_check "SBF replay requires SBF_OUT_DIR"
    requested_mode_unavailable=1
  else
    manifest="${SBF_MANIFEST:-$SBF_OUT_DIR/audit-manifest.json}"
    if ! python3 "$ROOT/audit/solana/verify_artifact_manifest.py" \
      "$manifest" "$SBF_OUT_DIR" >"$LOG_DIR/artifact-manifest.log" 2>&1; then
      rows+=("| FAIL | SBF artifact provenance manifest |")
      failed=$((failed + 1))
      requested_mode_unavailable=1
      echo "FAILED: SBF artifact provenance manifest" >&2
      cat "$LOG_DIR/artifact-manifest.log" >&2
    else
      rows+=("| PASS | SBF artifact provenance manifest |")
      passed=$((passed + 1))
      sbf_features="sbf-test"
      if python3 - "$manifest" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
raise SystemExit(
    0
    if manifest.get("schemaVersion") == 3
    and manifest.get("buildKind") == "production"
    else 1
)
PY
      then
        production_release_verified=1
        sbf_features="sbf-test,production-ids,tlama-transfer-hook/production-ids"
        if [[ -n "${TLAMA_AGAVE_ARCHIVE:-}" && -n "${TLAMA_PLATFORM_TOOLS_ARCHIVE:-}" ]] &&
          "$ROOT/scripts/verify-tlama-reproducible-build.sh" \
            "$manifest" "$SBF_OUT_DIR" >"$LOG_DIR/reproducible-build.log" 2>&1; then
          rows+=("| PASS | Two clean pinned-toolchain production rebuilds |")
          passed=$((passed + 1))
          production_rebuild_verified=1
        else
          block_check "Two clean pinned-toolchain production rebuilds"
          requested_mode_unavailable=1
        fi
      fi
      run_check "Four-artifact SBF adversarial campaign" \
        env SBF_OUT_DIR="$SBF_OUT_DIR" BPF_OUT_DIR="$SBF_OUT_DIR" cargo test --locked \
        --features "$sbf_features" --test program_test --manifest-path "$POOL" -- --nocapture
    fi
  fi
else
  block_check "Commit- and hash-bound four-artifact SBF campaign replay"
fi

if [[ "$production_release_verified" -eq 1 && "$production_rebuild_verified" -eq 1 ]]; then
  rows+=("| PASS | Pinned production-ID SBF build and paired release hashes |")
  passed=$((passed + 1))
else
  block_check "Pinned production-ID SBF build and paired release hashes"
fi
block_check "Independent external audit report"
block_check "Release custody and independent-verifier approvals"
if [[ -z "$DEVNET_EVIDENCE" || -z "$DEVNET_RPC_URL" ]]; then
  block_check "Exact-artifact Devnet RPC rehearsal (supply --devnet-evidence)"
else
  release_manifest="${manifest:-$ROOT/audit/solana/releases/2026-09-07-production-candidate/manifest.json}"
  release_artifacts="${SBF_OUT_DIR:-$(dirname "$release_manifest")/artifacts}"
  if python3 "$ROOT/audit/solana/verify_devnet_evidence.py" \
    "$DEVNET_EVIDENCE" "$release_manifest" "$release_artifacts" \
    --rpc-url "$DEVNET_RPC_URL" \
    >"$LOG_DIR/devnet-evidence.log" 2>&1; then
    rows+=("| PASS | Exact-artifact Devnet RPC rehearsal |")
    passed=$((passed + 1))
  else
    rows+=("| FAIL | Exact-artifact Devnet RPC rehearsal evidence |")
    failed=$((failed + 1))
    echo "FAILED: Exact-artifact Devnet RPC rehearsal evidence" >&2
    tail -n 30 "$LOG_DIR/devnet-evidence.log" >&2
  fi
fi
block_check "Mainnet deployment authorization"

commit="$(git rev-parse HEAD)"
timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
cargo_version="$(cargo --version)"
rustc_version="$(env -u LD_AUDIT "${RUSTC:-rustc}" --version)"
overall="NOT READY FOR AUDIT HANDOFF"
if [[ "$failed" -eq 0 ]]; then
  overall="READY FOR EXTERNAL CODE REVIEW; NOT READY FOR DEPLOYMENT"
fi

{
  echo "# TLAMA Solana audit readiness"
  echo
  echo "- Generated: \`$timestamp\`"
  echo "- Commit: \`$commit\`"
  echo "- Mode: \`$MODE\`"
  echo "- Host Cargo: \`$cargo_version\`"
  echo "- Host rustc: \`$rustc_version\`"
  echo "- Status: **$overall**"
  echo "- Local checks: **$passed passed, $failed failed**"
  echo "- Release hold points: **$blocked blocked**"
  echo
  echo "| Result | Gate |"
  echo "|---|---|"
  printf '%s\n' "${rows[@]}"
  echo
  echo "## Interpretation"
  echo
  echo "A PASS records only a local offline check against this commit. BLOCKED is a"
  echo "required hold point, not a test failure. This report does not authorize an"
  echo "RPC call, deployment, keypair use, upgrade-authority change, or mainnet action."
  echo
  echo "The recorded split-program SBF evidence is in"
  echo "\`onchain/tlama-pool-hook/SBF_EVIDENCE.md\`; replay it in \`sbf\` mode before"
  echo "freezing an auditor handoff. Replay requires a manifest whose frozen source"
  echo "commit is an ancestor of the tested commit, unchanged frozen paths, both"
  echo "lockfiles, all four ELF files, and their hashes. Historical combined or Devnet"
  echo "artifacts are not release candidates."
} >"$REPORT"

cat "$REPORT"
[[ "$failed" -eq 0 && "$requested_mode_unavailable" -eq 0 ]]