#!/usr/bin/env bash
set -euo pipefail

# Replit's injected runtime loader exhausts static TLS space in the official
# rustup compiler. Remove only that loader for rustc; Cargo and the compiled
# replay harness otherwise run with the normal workspace environment.
unset LD_AUDIT
if [[ -n "${RUSTC_REAL:-}" ]]; then
  exec "$RUSTC_REAL" "$@"
fi
exec rustup run "${RUSTUP_TOOLCHAIN:-stable}" rustc "$@"