#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="${1:?usage: verify-tlama-reproducible-build.sh MANIFEST ARTIFACT_DIR [ATTESTATION_OUT]}"
ARTIFACT_DIR="${2:?usage: verify-tlama-reproducible-build.sh MANIFEST ARTIFACT_DIR [ATTESTATION_OUT]}"
ATTESTATION_OUT="${3:-}"
AGAVE_ARCHIVE="${TLAMA_AGAVE_ARCHIVE:?TLAMA_AGAVE_ARCHIVE is required}"
PLATFORM_ARCHIVE="${TLAMA_PLATFORM_TOOLS_ARCHIVE:?TLAMA_PLATFORM_TOOLS_ARCHIVE is required}"
WORK_ROOT="${TLAMA_REPRO_WORK_ROOT:-$ROOT/.local/share}"

readarray -t manifest_values < <(python3 - "$MANIFEST" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["sourceCommit"])
print(manifest["toolchain"]["agave"]["archiveSha256"])
print(manifest["toolchain"]["platformTools"]["archiveSha256"])
for name in ("tlama_pool_adapter.so", "tlama_transfer_hook.so"):
    print(manifest["artifacts"][name])
PY
)
SOURCE_COMMIT="${manifest_values[0]}"
AGAVE_SHA="${manifest_values[1]}"
PLATFORM_SHA="${manifest_values[2]}"
POOL_SHA="${manifest_values[3]}"
HOOK_SHA="${manifest_values[4]}"

check_hash() {
  local expected="$1" path="$2"
  [[ "$(sha256sum "$path" | awk '{print $1}')" == "$expected" ]]
}
check_hash "$AGAVE_SHA" "$AGAVE_ARCHIVE"
check_hash "$PLATFORM_SHA" "$PLATFORM_ARCHIVE"

mkdir -p "$WORK_ROOT"
WORK="$(mktemp -d "$WORK_ROOT/tlama-repro-build.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/agave" "$WORK/platform-tools"
tar -xjf "$AGAVE_ARCHIVE" --strip-components=1 -C "$WORK/agave"
tar -xjf "$PLATFORM_ARCHIVE" -C "$WORK/platform-tools"

for dependency_path in \
  "$WORK/agave/dependencies/platform-tools" \
  "$WORK/agave/bin/platform-tools-sdk/sbf/dependencies/platform-tools"; do
  rm -rf "$dependency_path"
  mkdir -p "$(dirname "$dependency_path")"
  ln -s "$WORK/platform-tools" "$dependency_path"
done
touch "$WORK/agave/bin/platform-tools-sdk/sbf/dependencies/platform-tools-v1.44.md"

[[ "$("$WORK/platform-tools/rust/bin/rustc" --version)" == "rustc 1.89.0-dev" ]]
[[ "$("$WORK/platform-tools/rust/bin/cargo" --version)" == \
  "cargo 1.89.0 (ca74c32f0 2025-10-17)" ]]

export PATH="$WORK/platform-tools/rust/bin:$WORK/agave/bin:$PATH"
export RUSTC="$WORK/platform-tools/rust/bin/rustc"
export CARGO_HOME="${CARGO_HOME:-$ROOT/.local/share/.cargo}"
unset RUSTUP_TOOLCHAIN LD_AUDIT

build_one() {
  local label="$1" checkout="$WORK/source-$1" out="$WORK/build-$1"
  git clone --quiet --no-hardlinks "$ROOT" "$checkout"
  git -C "$checkout" checkout --quiet --detach "$SOURCE_COMMIT"
  test -z "$(git -C "$checkout" status --porcelain=v1 --untracked-files=all)"
  mkdir -p "$out" "$WORK/tmp-$label" "$WORK/rust-cache-$label"
  (
    cd "$checkout"
    export TMPDIR="$WORK/tmp-$label"
    export CARGO_TARGET_DIR="$WORK/target-$label"
    export RUSTC_WRAPPER=
    "$WORK/agave/bin/cargo-build-sbf" \
      --manifest-path onchain/tlama-pool-hook/Cargo.toml \
      --sbf-out-dir "$out" \
      --features production-ids \
      --no-rustup-override \
      --skip-tools-install \
      -- --locked
    "$WORK/agave/bin/cargo-build-sbf" \
      --manifest-path onchain/tlama-transfer-hook/Cargo.toml \
      --sbf-out-dir "$out" \
      --features production-ids \
      --no-rustup-override \
      --skip-tools-install \
      -- --locked
  )
  check_hash "$POOL_SHA" "$out/tlama_pool_adapter.so"
  check_hash "$HOOK_SHA" "$out/tlama_transfer_hook.so"
  cmp "$out/tlama_pool_adapter.so" "$ARTIFACT_DIR/tlama_pool_adapter.so"
  cmp "$out/tlama_transfer_hook.so" "$ARTIFACT_DIR/tlama_transfer_hook.so"
}

build_one a
build_one b
cmp "$WORK/build-a/tlama_pool_adapter.so" "$WORK/build-b/tlama_pool_adapter.so"
cmp "$WORK/build-a/tlama_transfer_hook.so" "$WORK/build-b/tlama_transfer_hook.so"

GENERATED="$WORK/reproducible-build.json"
python3 - "$GENERATED" "$SOURCE_COMMIT" "$AGAVE_SHA" "$PLATFORM_SHA" \
  "$WORK/build-a" "$WORK/build-b" <<'PY'
import hashlib
import json
import pathlib
import sys

out, source, agave, platform, build_a, build_b = sys.argv[1:]
names = ("tlama_pool_adapter.so", "tlama_transfer_hook.so")

def evidence(directory):
    result = {}
    for name in names:
        data = (pathlib.Path(directory) / name).read_bytes()
        result[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    return result

document = {
    "schemaVersion": 1,
    "sourceCommit": source,
    "toolchainArchives": {
        "agave": agave,
        "platformTools": platform,
    },
    "buildA": evidence(build_a),
    "buildB": evidence(build_b),
}
pathlib.Path(out).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
PY

if [[ -n "$ATTESTATION_OUT" ]]; then
  cp "$GENERATED" "$ATTESTATION_OUT"
else
  cmp "$GENERATED" "$ARTIFACT_DIR/reproducible-build.json"
fi