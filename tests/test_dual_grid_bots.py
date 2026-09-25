from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from bot.config import ConfigurationError, Settings
from bot.isolated_grid import (
    IsolatedGridConfigError,
    OperatorActivationStore,
    isolated_profile,
)
from bot.market_data import (
    MarketDataError,
    dynamic_grid_plan,
    validate_market_data,
)
from bot.live_runner import LivePriceStore, LiveTradingLoop
from bot.trader import StrategyStateStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _market_document(symbol: str, now: float) -> dict[str, object]:
    candles = []
    start = now - 15 * 60
    for index in range(15):
        opened = start + index * 60
        candles.append(
            {
                "opened_at": opened,
                "closed_at": opened + 60,
                "open": "100",
                "high": "102",
                "low": "98",
                "close": "100",
                "volume": "20" if index == 14 else "10",
            }
        )
    return {
        "symbol": symbol,
        "base_asset": "USDT",
        "captured_at": now,
        "candles": candles,
        "volume_profile": [
            {"price": "98", "volume": "10"},
            {"price": "100", "volume": "40"},
            {"price": "102", "volume": "10"},
        ],
        "rsi": "50",
    }


def _settings(path: Path, profile: str) -> Settings:
    symbol = isolated_profile(profile).symbol
    return Settings(
        bsc_rpc_url="http://example.invalid",
        wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="unused",
        enable_live_trading=True,
        slippage_bps=100,
        deadline_seconds=120,
        receipt_timeout_seconds=180,
        max_gas_limit=700_000,
        strategy_state_file=str(path),
        live_strategy_mode="grid",
        grid_symbols=(symbol,),
        grid_spacing_bps=100,
        grid_levels=6,
        grid_order_usdt=Decimal("50"),
        grid_max_budget_usdt=Decimal("300"),
        grid_take_profit_bps=100,
        isolated_grid_profile=profile,
    )


class DualGridMarketDataTests(unittest.TestCase):
    def test_dynamic_plan_uses_validated_candles_volume_and_rsi(self) -> None:
        now = 10_000.0
        market = validate_market_data(_market_document("BTC", now), "BTC", now=now)
        plan = dynamic_grid_plan(market)
        self.assertTrue(plan.entry_allowed)
        self.assertEqual(plan.rsi, Decimal("50"))
        self.assertTrue(plan.volume_confirmed)
        self.assertEqual(plan.spacing_bps, 400)
        self.assertEqual(len(plan.levels), 6)
        self.assertTrue(all(left > right for left, right in zip(plan.levels, plan.levels[1:])))

    def test_invalid_stale_cross_symbol_and_contradictory_data_fail_closed(self) -> None:
        now = 10_000.0
        document = _market_document("BTC", now)
        with self.assertRaises(MarketDataError):
            validate_market_data(document, "XRP", now=now)
        stale = _market_document("BTC", now)
        stale["captured_at"] = now - 301
        with self.assertRaises(MarketDataError):
            validate_market_data(stale, "BTC", now=now)
        contradictory = _market_document("BTC", now)
        contradictory["rsi"] = "10"
        with self.assertRaises(MarketDataError):
            validate_market_data(contradictory, "BTC", now=now)

    def test_state_initializes_from_dynamic_plan_not_source_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _settings(Path(directory) / "state.json", "grid_bot_1")
            market = validate_market_data(
                _market_document("BTC", 10_000), "BTC", now=10_000
            )
            plan = dynamic_grid_plan(market)
            signal, snapshot = StrategyStateStore(config.strategy_state_file).grid_decide(
                "BTC",
                "99",
                config,
                anchor_price=plan.anchor_price,
                levels=plan.levels,
            )
            self.assertIsNone(signal)
            self.assertEqual(snapshot.anchor_price, format(plan.anchor_price.normalize(), "f"))
            self.assertEqual(snapshot.levels, tuple(format(value.normalize(), "f") for value in plan.levels))

    def test_invalid_market_data_blocks_before_any_rpc_quote(self) -> None:
        class RejectingProvider:
            def read(self, _symbol: str):
                raise MarketDataError("stale test data")

        class NoNetworkBridge:
            def quote(self, *_args: object, **_kwargs: object):
                raise AssertionError("RPC quote must not run for invalid market data")

        class NoTradeTrader:
            def confirmed_notifications(self):
                return []

            def inspect_pending(self):
                return None

            def reconcile_grid(self):
                return None

            def grid_snapshot(self, _symbol: str):
                return None

            def grid_decide(self, *_args: object, **_kwargs: object):
                raise AssertionError("grid decision must not run for invalid market data")

        with tempfile.TemporaryDirectory() as directory:
            config = _settings(Path(directory) / "state.json", "grid_bot_1")
            prices = LivePriceStore(str(Path(directory) / "prices.json"), ("BTC",))
            loop = LiveTradingLoop(
                config,
                bridge=NoNetworkBridge(),
                trader=NoTradeTrader(),
                prices=prices,
                market_data_provider=RejectingProvider(),
            )
            loop.run_once()
            state = json.loads((Path(directory) / "prices.json").read_text())
            self.assertIsNotNone(state["last_successful_poll_at"])

    def test_revoked_authorization_blocks_before_reconciliation_or_rpc(self) -> None:
        class NoNetworkBridge:
            def quote(self, *_args: object, **_kwargs: object):
                raise AssertionError("RPC must not run after authorization is revoked")

        class NoTradeTrader:
            def confirmed_notifications(self):
                return []

            def inspect_pending(self):
                raise AssertionError("reconciliation must not run after authorization is revoked")

        with tempfile.TemporaryDirectory() as directory:
            config = _settings(Path(directory) / "state.json", "grid_bot_1")
            loop = LiveTradingLoop(
                config,
                bridge=NoNetworkBridge(),
                trader=NoTradeTrader(),
                prices=LivePriceStore(str(Path(directory) / "prices.json"), ("BTC",)),
                market_data_provider=object(),
                execution_authorizer=lambda: (_ for _ in ()).throw(
                    IsolatedGridConfigError("activation revoked")
                ),
            )
            with self.assertRaises(IsolatedGridConfigError):
                loop.run_once()

    def test_dual_profile_rejects_ema_or_disabled_mode_as_defense_in_depth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = _settings(Path(directory) / "state.json", "grid_bot_1")
            for mode in ("ema", "disabled"):
                with self.subTest(mode=mode), self.assertRaises(ConfigurationError):
                    LiveTradingLoop(
                        replace(config, live_strategy_mode=mode),
                        bridge=object(),
                        trader=object(),
                        prices=LivePriceStore(
                            str(Path(directory) / f"{mode}.json"), ("BTC",)
                        ),
                        market_data_provider=object(),
                    )


class DualGridIsolationTests(unittest.TestCase):
    def test_profiles_have_disjoint_files_ports_symbols_and_namespaces(self) -> None:
        one = isolated_profile("grid_bot_1")
        two = isolated_profile("grid_bot_2")
        self.assertEqual((one.symbol, two.symbol), ("BTC", "XRP"))
        self.assertNotEqual(one.control_port, two.control_port)
        self.assertNotEqual(one.environment_namespace, two.environment_namespace)
        one_paths = {one.state, one.config, one.prices, one.market_data, one.log}
        two_paths = {two.state, two.config, two.prices, two.market_data, two.log}
        self.assertTrue(one_paths.isdisjoint(two_paths))

    def test_profile_cannot_claim_another_symbol(self) -> None:
        with self.assertRaises(ConfigurationError):
            replace(_settings(Path("/tmp/state.json"), "grid_bot_1"), grid_symbols=("XRP",))

    def test_activation_is_profile_scoped_short_lived_and_revocable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activation.json"
            one = OperatorActivationStore(path, "grid_bot_1")
            one.activate(now=1_000)
            one.require_fresh(now=1_001)
            with self.assertRaises(IsolatedGridConfigError):
                OperatorActivationStore(path, "grid_bot_2").require_fresh(now=1_001)
            with self.assertRaises(IsolatedGridConfigError):
                one.require_fresh(now=1_000 + 86_401)
            one.revoke()
            self.assertFalse(path.exists())

    def test_both_services_pause_without_secrets_and_restart_cleanly(self) -> None:
        for port in (8008, 9000):
            with socket.socket() as probe:
                probe.settimeout(0.1)
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    self.skipTest(
                        "fixed control ports are occupied by managed grid workflows"
                    )
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            environment["HOME"] = directory
            environment["ENABLE_LIVE_TRADING"] = "false"
            environment["LIVE_STRATEGY_MODE"] = "disabled"
            for secret in ("BSC_RPC_URL", "WALLET_ADDRESS", "WALLET_PRIVATE_KEY"):
                environment.pop(secret, None)

            def run_pair() -> None:
                processes = [
                    subprocess.Popen(
                        [sys.executable, str(PROJECT_ROOT / f"{name}.py")],
                        cwd=PROJECT_ROOT,
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    for name in ("grid_bot_1", "grid_bot_2")
                ]
                try:
                    for port, name in ((8008, "grid_bot_1"), (9000, "grid_bot_2")):
                        for _ in range(100):
                            try:
                                with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                                    payload = json.load(response)
                                break
                            except URLError:
                                time.sleep(0.03)
                        else:
                            self.fail(f"{name} health endpoint did not start")
                        self.assertEqual(payload["service"], name)
                        self.assertEqual(payload["state"], "paused")
                        self.assertFalse(payload["liveExecution"])
                finally:
                    for process in processes:
                        process.terminate()
                    for process in processes:
                        process.wait(timeout=5)
                        self.assertEqual(process.returncode, 0)

            run_pair()
            run_pair()
            root = Path(directory) / ".local/state/bnb-defi-bot"
            self.assertFalse((root / "grid_bot_1/state.json").exists())
            self.assertFalse((root / "grid_bot_2/state.json").exists())
            serialized = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("private", serialized.lower())
            self.assertNotIn("example.invalid", serialized)


if __name__ == "__main__":
    unittest.main()