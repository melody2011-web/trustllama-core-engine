"""Run the isolated TrustLlama Transfer Hook protocol simulation."""

from __future__ import annotations

import contextlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from trust_llama_transfer_hook_simulation import run_validation


LOG_PATH = Path("tests/logs/trust_llama_transfer_hook_simulation.log")


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("w", encoding="utf-8") as log_stream:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(
            log_stream
        ):
            print("TrustLlama Transfer Hook validation")
            print("mode=offline-local-simulation; network=disabled")
            print("note=not an on-chain Solana devnet deployment")
            print("started_utc=" + datetime.now(timezone.utc).isoformat())
            print("wallets=ephemeral public-only manifest; private keys never persisted")
            success = run_validation(log_stream)
            print("validation_status=" + ("PASS" if success else "FAIL"))
            print("teardown=temporary wallet environment removed by test cleanup")
            print("completed_utc=" + datetime.now(timezone.utc).isoformat())
    print(f"Validation {'passed' if success else 'failed'}; log written to {LOG_PATH}")
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())