#!/usr/bin/env python3
"""Offline, review-first generator for TLAMA's production public-ID tuple."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile

from solders.pubkey import Pubkey

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "onchain" / "production-ids.txt"
DEVELOPMENT_IDS_PATH = ROOT / "onchain" / "development-ids.txt"
KEYS = ("adapter", "hook", "tlamaMint", "tlamaVault", "wsolVault", "poolAuthority")


class ValidationError(ValueError):
    pass


def parse_pubkey(label: str, value: str) -> Pubkey:
    value = value.strip()
    if not value:
        raise ValidationError(f"{label} cannot be empty")
    try:
        public_key = Pubkey.from_string(value)
    except ValueError as error:
        raise ValidationError(
            f"{label} must be one Solana 32-byte base58 public address"
        ) from error
    if str(public_key) != value:
        raise ValidationError(f"{label} is not in canonical base58 form")
    return public_key


def load_development_values(path: pathlib.Path = DEVELOPMENT_IDS_PATH) -> set[str]:
    values: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        _, separator, value = line.partition("=")
        if separator:
            values.add(value)
    return values


def validate(values: dict[str, str]) -> dict[str, str]:
    if tuple(values) != KEYS:
        raise ValidationError(f"addresses must be supplied in this order: {', '.join(KEYS)}")

    parsed = {key: parse_pubkey(key, values[key]) for key in KEYS}
    canonical = {key: str(value) for key, value in parsed.items()}

    if len(set(canonical.values())) != len(KEYS):
        raise ValidationError("all six production addresses must be distinct")

    development_values = load_development_values()
    reused = [key for key, value in canonical.items() if value in development_values]
    if reused:
        raise ValidationError(
            "disposable development addresses cannot be used for production: "
            + ", ".join(reused)
        )

    expected_authority, _ = Pubkey.find_program_address(
        [b"pool-authority"], parsed["adapter"]
    )
    if parsed["poolAuthority"] != expected_authority:
        raise ValidationError(
            "poolAuthority does not match the pool-authority PDA derived from adapter; "
            f"expected {expected_authority}"
        )

    return canonical


def render(values: dict[str, str]) -> str:
    return "".join(f"{key}={values[key]}\n" for key in KEYS)


def write_atomically(contents: str, output_path: pathlib.Path = OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".production-ids.", dir=output_path.parent, text=True
    )
    temporary_path = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def collect_interactively() -> dict[str, str]:
    print("Enter PUBLIC Solana addresses only. Never paste seed phrases or private keys.")
    print("Nothing is sent over the network.\n")
    values: dict[str, str] = {}
    for key in KEYS:
        values[key] = input(f"{key}: ").strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate and generate onchain/production-ids.txt offline."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate the existing production-ids.txt without changing it",
    )
    parser.add_argument(
        "--write-json-stdin",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    try:
        if args.check:
            lines = OUTPUT_PATH.read_text(encoding="utf-8").splitlines()
            if len(lines) != len(KEYS):
                raise ValidationError("production-ids.txt must contain exactly six lines")
            raw: dict[str, str] = {}
            for expected, line in zip(KEYS, lines):
                key, separator, value = line.partition("=")
                if separator != "=" or key != expected:
                    raise ValidationError(f"malformed or reordered entry at {expected}")
                raw[key] = value
            validate(raw)
            print(f"VALID: {OUTPUT_PATH.relative_to(ROOT)}")
            return 0

        if args.write_json_stdin:
            if OUTPUT_PATH.exists():
                raise ValidationError(
                    "onchain/production-ids.txt already exists; refusing to overwrite it"
                )
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict) or tuple(payload) != KEYS:
                raise ValidationError(
                    f"JSON input must contain exactly, in order: {', '.join(KEYS)}"
                )
            values = validate(
                {
                    key: value if isinstance(value, str) else ""
                    for key, value in payload.items()
                }
            )
            write_atomically(render(values))
            print("saved")
            return 0

        if OUTPUT_PATH.exists():
            print(
                f"REFUSED: {OUTPUT_PATH.relative_to(ROOT)} already exists. "
                "Review and remove it manually before generating a replacement.",
                file=sys.stderr,
            )
            return 2

        values = validate(collect_interactively())
        contents = render(values)

        print("\nReview the exact file that will be written:\n")
        print(contents, end="")
        print("This does not deploy, sign, fund, build, or submit any transaction.")
        confirmation = input('Type exactly "WRITE PRODUCTION IDS" to save: ').strip()
        if confirmation != "WRITE PRODUCTION IDS":
            print("Cancelled. No file was written.")
            return 1

        write_atomically(contents)
        print(f"\nSaved {OUTPUT_PATH.relative_to(ROOT)} with mode 0600.")
        print("Next: review the diff with `git diff -- onchain/production-ids.txt`.")
        print("Do not build or deploy until the independent release gates are complete.")
        return 0
    except (ValidationError, FileNotFoundError) as error:
        print(f"REJECTED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())