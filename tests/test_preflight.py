from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import Settings
from bot.main import main
from bot.trader import BscPreflight, ReadOnlyPancakeSwapTrader, TraderError


def _settings(path: Path) -> Settings:
    return Settings(
        bsc_rpc_url="https://public.example",
        wallet_address="0x1111111111111111111111111111111111111111",
        wallet_private_key="must-not-be-used",
        enable_live_trading=True,
        slippage_bps=100,
        deadline_seconds=120,
        receipt_timeout_seconds=180,
        max_gas_limit=700_000,
        strategy_state_file=str(path),
        live_strategy_mode="grid",
        grid_symbols=("XRP", "BTC"),
        grid_spacing_bps=500,
        grid_levels=5,
        grid_order_usdt=Decimal("50"),
        grid_max_budget_usdt=Decimal("250"),
        grid_take_profit_bps=500,
    )


class _FakeReadOnlyTrader:
    def __init__(self, *, usdt: Decimal = Decimal("500")) -> None:
        self.w3 = SimpleNamespace(
            eth=SimpleNamespace(chain_id=56, gas_price=3_000_000_000)
        )
        self.usdt = usdt
        self.quote_calls: list[tuple[str, ...]] = []

    def native_balance_wei(self) -> int:
        return 10**18

    def usdt_balance(self) -> Decimal:
        return self.usdt

    def quote(self, action: str, symbol: str, amount: str) -> dict[str, str]:
        self.quote_calls.append(("quote", action, symbol, amount))
        return {
            "amountIn": amount,
            "quotedOut": "1",
            "route": f"{symbol}/USDT",
        }

    def quote_pair(
        self, input_symbol: str, output_symbol: str, amount: str
    ) -> dict[str, str]:
        self.quote_calls.append(("pair", input_symbol, output_symbol, amount))
        return {
            "amountIn": amount,
            "quotedOut": "1",
            "route": f"{input_symbol} -> USDT -> {output_symbol}",
        }


def _write_journal(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "base_asset": "USDT",
                "pending": {
                    "kind": "strategy",
                    "action": "buy",
                    "symbol": "XRP",
                    "amount": "50",
                    "tx_hash": "0x" + "ab" * 32,
                    "phase": "swap",
                },
                "positions": {},
                "grid": {
                    "version": 2,
                    "config": None,
                    "assets": {
                        "XRP": {
                            "lots": [
                                {
                                    "status": "open",
                                    "buy_amount_usdt": "50",
                                }
                            ]
                        },
                        "BTC": {"lots": []},
                    },
                },
                "confirmed_notifications": [],
            }
        ),
        encoding="utf-8",
    )


class PreflightTests(unittest.TestCase):
    def test_preflight_reports_chain_funding_journal_and_all_direct_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            _write_journal(state_path)
            trader = _FakeReadOnlyTrader()

            report = BscPreflight(_settings(state_path), trader=trader).run()

        self.assertTrue(report["ok"])
        self.assertEqual(report["chain"]["chainId"], 56)  # type: ignore[index]
        self.assertEqual(
            report["funding"]["requiredForTotalGridCap"], "500"  # type: ignore[index]
        )
        self.assertEqual(report["journal"]["openLotCount"], 1)  # type: ignore[index]
        self.assertEqual(report["journal"]["openExposureUsdt"], "50")  # type: ignore[index]
        self.assertEqual(len(trader.quote_calls), 4)
        self.assertNotIn("private", json.dumps(report).lower())

    def test_preflight_fails_when_funding_or_a_quote_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            trader = _FakeReadOnlyTrader(usdt=Decimal("499"))

            original_quote = trader.quote

            def unavailable_quote(
                action: str, symbol: str, amount: str
            ) -> dict[str, str]:
                if action == "buy" and symbol == "BTC":
                    raise TraderError("quote unavailable")
                return original_quote(action, symbol, amount)

            trader.quote = unavailable_quote  # type: ignore[method-assign]
            report = BscPreflight(_settings(state_path), trader=trader).run()

        self.assertFalse(report["ok"])
        self.assertFalse(report["funding"]["sufficient"])  # type: ignore[index]
        self.assertEqual(
            report["quotes"]["USDT/BTC buy"]["status"], "error"  # type: ignore[index]
        )

    @patch("bot.trader.Account.from_key", side_effect=AssertionError("private key accessed"))
    @patch("bot.trader.Web3")
    def test_read_only_client_does_not_load_private_key(self, web3, _from_key) -> None:
        fake_web3 = SimpleNamespace(
            middleware_onion=SimpleNamespace(inject=lambda *_args, **_kwargs: None),
            is_connected=lambda: True,
            eth=SimpleNamespace(
                chain_id=56,
                contract=lambda **_kwargs: object(),
            ),
        )
        web3.return_value = fake_web3

        with tempfile.TemporaryDirectory() as directory:
            client = ReadOnlyPancakeSwapTrader(_settings(Path(directory) / "state.json"))

        self.assertIs(client.w3, fake_web3)
        _from_key.assert_not_called()

    def test_environment_loader_can_require_public_wallet_without_private_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "https://public.example",
                "WALLET_ADDRESS": "0x1111111111111111111111111111111111111111",
                "LIVE_STRATEGY_MODE": "grid",
                "TOTAL_GRID_LEVELS": "5",
                "PER_LINE_USDT": "50",
                "GRID_STEP_PERCENT": "0.05",
                "PROFIT_TARGET_PERCENT": "0.05",
                "GRID_MAX_BUDGET_USDT": "250",
            },
            clear=True,
        ):
            settings = Settings.from_environment(
                require_wallet=True,
                require_private_key=False,
            )

        self.assertEqual(settings.wallet_private_key, "")

    @patch("bot.main.PancakeSwapTrader")
    @patch("bot.main.BscPreflight")
    @patch("bot.main.Settings.from_environment")
    def test_cli_preflight_uses_read_only_settings_and_client(
        self, from_environment, preflight, _trader
    ) -> None:
        settings = _settings(Path("/tmp/preflight-test-state.json"))
        from_environment.return_value = settings
        preflight.return_value.run.return_value = {"ok": True, "profile": {}}

        with patch("bot.main._parser") as parser:
            parser.return_value.parse_args.return_value = SimpleNamespace(
                preflight=True,
                symbol=None,
                amount=None,
                prices=None,
                quote_symbol=None,
                inspect_pending=False,
                reconcile_strategy=False,
                reconcile_manual=False,
                strategy=False,
                action=None,
            )
            with patch("builtins.print"):
                result = main()

        self.assertEqual(result, 0)
        from_environment.assert_called_once_with(
            require_wallet=True,
            require_private_key=False,
        )
        preflight.assert_called_once_with(settings)
        _trader.assert_not_called()

    @patch("bot.main.BscPreflight")
    @patch("bot.main.Settings.from_environment")
    def test_cli_preflight_redacts_unexpected_rpc_errors(
        self, from_environment, preflight
    ) -> None:
        secret_rpc_url = "https://rpc.example/private-token"
        from_environment.return_value = _settings(
            Path("/tmp/preflight-test-state.json")
        )
        preflight.return_value.run.side_effect = ConnectionError(secret_rpc_url)

        with patch("bot.main._parser") as parser:
            parser.return_value.parse_args.return_value = SimpleNamespace(
                preflight=True,
                symbol=None,
                amount=None,
                prices=None,
                quote_symbol=None,
                inspect_pending=False,
                reconcile_strategy=False,
                reconcile_manual=False,
                strategy=False,
                action=None,
            )
            with self.assertLogs("defi-bot", level="ERROR") as logs:
                result = main()

        output = "\n".join(logs.output)
        self.assertEqual(result, 1)
        self.assertIn("ConnectionError", output)
        self.assertNotIn(secret_rpc_url, output)
        self.assertNotIn("private-token", output)


if __name__ == "__main__":
    unittest.main()