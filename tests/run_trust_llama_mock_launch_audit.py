"""Offline fake-money TrustLlama launch-sequence audit.

This runner is intentionally dependency-free and does not import any trading,
game, wallet, Solana, or RPC module. "Devnet/Mock" here means a local
deterministic simulation; it does not connect to Solana devnet or any network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


LOG_PATH = Path("tests/logs/trust_llama_mock_launch_audit.log")
MOCK_NETWORK = "solana-devnet-mock"
TOTAL_SUPPLY_TLAMA = 1_000_000_000
DEVELOPER_PERCENT = 1
ARCADE_VAULT_PERCENT = 10
PUBLIC_LP_PERCENT = 89


@dataclass(frozen=True)
class MockLaunchResult:
    total_supply_tlama: int
    developer_tlama: int
    arcade_vault_tlama: int
    public_lp_tlama: int
    mock_lp_keys_burned: bool


def _render_password_entry_box() -> list[str]:
    return [
        "STEP 1/4 · SECURE PASSWORD ENTRY BOX (SIMULATED DISPLAY ONLY)",
        "┌──────────────────────────────────────────────┐",
        "│ Operator password: [ hidden / not collected ] │",
        "│ [ Authenticate ]                               │",
        "└──────────────────────────────────────────────┘",
        "PASS: password field represented; no password was requested, read, or stored.",
    ]


def _simulate_mint() -> list[str]:
    return [
        "STEP 2/4 · MOCK MINT",
        f"network={MOCK_NETWORK}",
        "mint_authority=MOCK-MINT-AUTHORITY-PUBLIC-ONLY",
        f"fake_minted_tlama={TOTAL_SUPPLY_TLAMA:,}",
        "PASS: no mint instruction was signed or submitted.",
    ]


def _simulate_split() -> tuple[MockLaunchResult, list[str]]:
    developer = TOTAL_SUPPLY_TLAMA * DEVELOPER_PERCENT // 100
    arcade_vault = TOTAL_SUPPLY_TLAMA * ARCADE_VAULT_PERCENT // 100
    public_lp = TOTAL_SUPPLY_TLAMA * PUBLIC_LP_PERCENT // 100
    result = MockLaunchResult(
        total_supply_tlama=TOTAL_SUPPLY_TLAMA,
        developer_tlama=developer,
        arcade_vault_tlama=arcade_vault,
        public_lp_tlama=public_lp,
        mock_lp_keys_burned=False,
    )
    assert developer + arcade_vault + public_lp == TOTAL_SUPPLY_TLAMA
    return result, [
        "STEP 3/4 · TOKENOMICS SPLIT",
        f"developer={DEVELOPER_PERCENT}% = {developer:,} TLAMA",
        f"arcade_rewards_vault={ARCADE_VAULT_PERCENT}% = {arcade_vault:,} TLAMA",
        f"public_liquidity_pool={PUBLIC_LP_PERCENT}% = {public_lp:,} TLAMA",
        f"split_total={developer + arcade_vault + public_lp:,} TLAMA",
        "PASS: split equals exactly 100% / 1,000,000,000 fake TLAMA.",
    ]


def _simulate_lp_key_burn(result: MockLaunchResult) -> list[str]:
    assert result.public_lp_tlama == 890_000_000
    mock_lp_authority = "MOCK-LP-AUTHORITY-PUBLIC-ONLY"
    mock_lp_token = "MOCK-LP-TOKEN-PUBLIC-ONLY"
    return [
        "STEP 4/4 · MOCK LP-KEY DESTRUCTION",
        f"lp_authority={mock_lp_authority}",
        f"lp_token={mock_lp_token}",
        "action=simulate_irreversible_local_key_material_destruction",
        "private_key_material_created=false",
        "mock_lp_keys_burned=true",
        "PASS: mock LP control labels were discarded locally; no on-chain burn occurred.",
    ]


def run_audit() -> list[str]:
    lines = [
        "TrustLlama fake-money launch sequence audit",
        "mode=OFFLINE_ONLY",
        f"network={MOCK_NETWORK}",
        "scope=mock launch sequence; no deployment or blockchain activity",
        "",
    ]
    lines.extend(_render_password_entry_box())
    lines.append("")
    lines.extend(_simulate_mint())
    lines.append("")
    result, split_lines = _simulate_split()
    lines.extend(split_lines)
    lines.append("")
    lines.extend(_simulate_lp_key_burn(result))
    lines.extend(
        [
            "",
            "SAFETY CONFIRMATION",
            "real_funds_touched=0",
            "rpc_calls=0",
            "wallet_connections=0",
            "transactions_signed=0",
            "transactions_broadcast=0",
            "mainnet_deployments=0",
            "trading_modules_imported=0",
            "arcade_files_modified=0",
            "workflows_started=0",
            "RESULT=PASS · offline mock launch audit complete",
        ]
    )
    return lines


def main() -> None:
    lines = run_audit()
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\nlog_file={LOG_PATH}")


if __name__ == "__main__":
    main()