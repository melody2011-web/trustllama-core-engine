from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from bot.config import Settings
from bot.trader import (
    GridSignal,
    GridTakeProfitSignal,
    PancakeSwapTrader,
    StrategyGuardError,
    StrategySignal,
    StrategyStateStore,
    TradeResult,
    TraderError,
)


def settings(path: Path) -> Settings:
    return Settings(
        bsc_rpc_url="http://example.invalid",
        wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="not-used-by-these-tests",
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


class GridStateTests(unittest.TestCase):
    @staticmethod
    def _confirmed_level_one(
        state: StrategyStateStore, config: Settings, *, amount_out: str = "25"
    ) -> GridSignal:
        state.grid_decide("XRP", "100", config)
        signal, _ = state.grid_decide("XRP", "95", config)
        assert signal is not None
        state.claim_grid(signal, config, pre_trade_token_balance=0)
        state.confirm_grid(
            signal,
            TradeResult("buy", "XRP", "50", amount_out, "0xbuy-one", 1),
            config,
        )
        return signal

    def test_direct_settings_default_to_disabled_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            values = dict(settings(Path(directory) / "state.json").__dict__)
            values.pop("live_strategy_mode")
            default = Settings(
                **values
            )
            self.assertEqual(default.live_strategy_mode, "disabled")

    def test_grid_rejects_a_nonpositive_level_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Exception, "GRID_LEVELS must be greater than zero"):
                Settings(
                    **{
                        **settings(Path(directory) / "state.json").__dict__,
                        "grid_levels": 0,
                    }
                )

    def test_funded_grid_rejects_any_unapproved_risk_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Exception, "Funded grid mode is fixed"):
                Settings(
                    **{
                        **settings(Path(directory) / "state.json").__dict__,
                        "grid_order_usdt": Decimal("11"),
                    }
                )

    def test_funded_grid_accepts_the_five_percent_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Settings(
                **{
                    **settings(Path(directory) / "state.json").__dict__,
                    "grid_order_usdt": Decimal("50"),
                    "grid_max_budget_usdt": Decimal("250"),
                }
            )
            self.assertEqual(config.grid_order_usdt, Decimal("50"))
            self.assertEqual(config.grid_max_budget_usdt, Decimal("250"))
            self.assertEqual(config.grid_spacing_bps, 500)
            self.assertEqual(config.grid_take_profit_bps, 500)

    def test_requested_environment_names_load_the_five_percent_profile(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "http://example.invalid",
                "LIVE_STRATEGY_MODE": "grid",
                "TOTAL_GRID_LEVELS": "5",
                "PER_LINE_USDT": "50.0",
                "GRID_STEP_PERCENT": "0.05",
                "BUY_IMMEDIATELY_NOW": "True",
                "PROFIT_TARGET_PERCENT": "0.05",
                "GRID_MAX_BUDGET_USDT": "250",
            },
            clear=True,
        ):
            config = Settings.from_environment(require_wallet=False)

        self.assertEqual(config.grid_levels, 5)
        self.assertEqual(config.grid_order_usdt, Decimal("50.0"))
        self.assertEqual(config.grid_spacing_bps, 500)
        self.assertTrue(config.buy_immediately_now)
        self.assertEqual(config.grid_take_profit_bps, 500)

    def test_requested_environment_names_load_the_one_percent_profile(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "http://example.invalid",
                "LIVE_STRATEGY_MODE": "grid",
                "TOTAL_GRID_LEVELS": "5",
                "PER_LINE_USDT": "50.0",
                "GRID_STEP_PERCENT": "0.01",
                "BUY_IMMEDIATELY_NOW": "True",
                "PROFIT_TARGET_PERCENT": "0.01",
                "GRID_MAX_BUDGET_USDT": "250",
            },
            clear=True,
        ):
            config = Settings.from_environment(require_wallet=False)

        self.assertEqual(config.grid_levels, 5)
        self.assertEqual(config.grid_order_usdt, Decimal("50.0"))
        self.assertEqual(config.grid_spacing_bps, 100)
        self.assertTrue(config.buy_immediately_now)
        self.assertEqual(config.grid_take_profit_bps, 100)

    def test_requested_environment_names_load_the_six_level_one_percent_profile(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "http://example.invalid",
                "LIVE_STRATEGY_MODE": "grid",
                "TOTAL_GRID_LEVELS": "6",
                "PER_LINE_USDT": "50.0",
                "GRID_STEP_PERCENT": "0.01",
                "BUY_IMMEDIATELY_NOW": "True",
                "PROFIT_TARGET_PERCENT": "0.01",
                "GRID_MAX_BUDGET_USDT": "300",
            },
            clear=True,
        ):
            config = Settings.from_environment(require_wallet=False)

        self.assertEqual(config.grid_levels, 6)
        self.assertEqual(config.grid_order_usdt, Decimal("50.0"))
        self.assertEqual(config.grid_spacing_bps, 100)
        self.assertTrue(config.buy_immediately_now)
        self.assertEqual(config.grid_max_budget_usdt, Decimal("300"))
        self.assertEqual(config.grid_take_profit_bps, 100)

    def test_funded_grid_rejects_mismatched_approved_rates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Exception, "matching spacing/take-profit"):
                Settings(
                    **{
                        **settings(Path(directory) / "state.json").__dict__,
                        "grid_spacing_bps": 100,
                        "grid_take_profit_bps": 500,
                    }
                )

    def test_conflicting_spacing_aliases_fail_closed(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "http://example.invalid",
                "LIVE_STRATEGY_MODE": "grid",
                "GRID_STEP_PERCENT": "0.05",
                "GRID_SPACING_BPS": "200",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                Exception, "must describe the same spacing"
            ):
                Settings.from_environment(require_wallet=False)

    def test_empty_grid_migrates_safely_to_immediate_entry_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            old_config = settings(path)
            state = StrategyStateStore(path)
            state.grid_decide("XRP", "100", old_config)
            new_config = replace(old_config, buy_immediately_now=True)

            signal, snapshot = state.grid_decide("XRP", "100", new_config)

            self.assertEqual(snapshot.remaining_usdt, "250")
            self.assertEqual(snapshot.spent_usdt, "0")
            self.assertIsNotNone(signal)
            assert signal is not None
            self.assertTrue(signal.immediate)

    def test_open_grid_lot_stays_exit_only_during_profile_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            old_config = settings(path)
            state = StrategyStateStore(path)
            self._confirmed_level_one(state, old_config)
            new_config = replace(old_config, buy_immediately_now=True)

            buy, snapshot = state.grid_decide("XRP", "100", new_config)
            self.assertIsNone(buy)
            self.assertEqual(snapshot.status, "profile_change_pending")
            sell, _ = state.grid_take_profit_decide("XRP", "2.1", new_config)
            self.assertIsNotNone(sell)

    def test_closed_lot_history_does_not_suppress_new_profile_immediate_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            old_config = settings(path)
            state = StrategyStateStore(path)
            self._confirmed_level_one(state, old_config)
            sell, _ = state.grid_take_profit_decide("XRP", "2.1", old_config)
            assert sell is not None
            state.claim_grid_take_profit(
                sell, old_config, pre_trade_usdt_balance=0
            )
            state.confirm_grid_take_profit(
                sell,
                TradeResult("sell", "XRP", "25", "52.5", "0xclosed", 2),
                old_config,
            )
            new_config = replace(old_config, buy_immediately_now=True)

            signal, snapshot = state.grid_decide("XRP", "100", new_config)

            assert signal is not None
            self.assertTrue(signal.immediate)
            self.assertEqual(snapshot.status, "active")

    def test_grid_anchors_once_and_fills_each_fixed_level_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)

            signal, snapshot = state.grid_decide("XRP", "100", config)
            self.assertIsNone(signal)
            self.assertEqual(snapshot.levels, ("95", "90", "85", "80", "75"))
            self.assertEqual(snapshot.remaining_usdt, "250")

            signal, _ = state.grid_decide("XRP", "95", config)
            self.assertEqual(signal, GridSignal("XRP", 1, "50", "95", "100"))
            state.claim_grid(signal, config, pre_trade_token_balance=0)
            state.confirm_grid(
                signal,
                TradeResult("buy", "XRP", "50", "25", "0xlevel1", 1),
                config,
            )

            snapshot = state.grid_snapshot("XRP", config)
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.filled_levels, (1,))
            self.assertEqual(snapshot.spent_usdt, "50")
            self.assertEqual(snapshot.remaining_usdt, "200")

            next_signal, _ = state.grid_decide("XRP", "90", config)
            self.assertEqual(next_signal.level, 2)

    def test_immediate_entry_consumes_level_one_once_across_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = replace(settings(path), buy_immediately_now=True)
            state = StrategyStateStore(path)

            signal, snapshot = state.grid_decide("XRP", "100", config)

            assert signal is not None
            self.assertEqual(signal, GridSignal("XRP", 1, "50", "100", "100", True))
            self.assertEqual(snapshot.levels, ("95", "90", "85", "80", "75"))
            state.claim_grid(signal, config, pre_trade_token_balance=0)
            state.confirm_grid(
                signal,
                TradeResult("buy", "XRP", "50", "25", "0ximmediate", 1),
                config,
            )

            restarted = StrategyStateStore(path)
            repeated, updated = restarted.grid_decide("XRP", "100", config)
            self.assertIsNone(repeated)
            self.assertEqual(updated.filled_levels, (1,))
            self.assertEqual(updated.spent_usdt, "50")

    def test_unbroadcast_immediate_claim_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = replace(settings(path), buy_immediately_now=True)
            state = StrategyStateStore(path)
            signal, _ = state.grid_decide("BTC", "80000", config)
            assert signal is not None

            state.claim_grid(signal, config, pre_trade_token_balance=0)
            state.release_grid(signal)

            retry, snapshot = StrategyStateStore(path).grid_decide("BTC", "80000", config)
            assert retry is not None
            self.assertTrue(retry.immediate)
            self.assertEqual(snapshot.spent_usdt, "0")

    def test_confirmed_grid_buy_records_verified_fill_and_five_percent_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            self._confirmed_level_one(state, config)

            snapshot = state.grid_snapshot("XRP", config)

            assert snapshot is not None
            self.assertEqual(snapshot.open_lots[0].level, 1)
            self.assertEqual(snapshot.open_lots[0].amount_token, "25")
            self.assertEqual(snapshot.open_lots[0].buy_amount_usdt, "50")
            self.assertEqual(snapshot.open_lots[0].buy_price, "2")
            self.assertEqual(snapshot.open_lots[0].target_price, "2.1")
            self.assertEqual(snapshot.spent_usdt, "50")

    def test_legacy_pending_buy_keeps_its_two_percent_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = settings(path)
            state = StrategyStateStore(path)
            signal, _ = state.grid_decide("XRP", "100", config)
            self.assertIsNone(signal)
            signal, _ = state.grid_decide("XRP", "95", config)
            assert signal is not None
            state.claim_grid(signal, config, pre_trade_token_balance=0)

            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["grid"]["config"].pop("take_profit_bps")
            raw["grid"]["config"]["spacing_bps"] = 200
            raw["pending"].pop("grid_take_profit_bps")
            path.write_text(json.dumps(raw), encoding="utf-8")

            state.confirm_grid(
                signal,
                TradeResult("buy", "XRP", "50", "25", "0xlegacy-pending", 1),
                config,
            )

            snapshot = state.grid_snapshot("XRP", config)
            assert snapshot is not None
            self.assertEqual(snapshot.status, "profile_change_pending")
            self.assertEqual(snapshot.open_lots[0].target_price, "2.04")

    def test_take_profit_closes_exact_lot_releases_exposure_and_reuses_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            self._confirmed_level_one(state, config)

            signal, _ = state.grid_take_profit_decide("XRP", "2.1", config)
            self.assertEqual(
                signal,
                GridTakeProfitSignal(
                    "XRP", 1, "25", "2.1", "2.1", "100", "XRP:1:0xbuy-one"
                ),
            )
            assert signal is not None
            state.claim_grid_take_profit(signal, config, pre_trade_usdt_balance=100)
            state.record_grid_attempt(signal, "0xsell-one")
            state.confirm_grid_take_profit(
                signal,
                TradeResult("sell", "XRP", "25", "52.5", "0xsell-one", 2),
                config,
            )

            snapshot = state.grid_snapshot("XRP", config)
            assert snapshot is not None
            self.assertEqual(snapshot.filled_levels, ())
            self.assertEqual(snapshot.open_lots, ())
            self.assertEqual(snapshot.spent_usdt, "0")
            self.assertEqual(snapshot.remaining_usdt, "250")
            self.assertEqual(snapshot.realized_usdt, "52.5")
            self.assertEqual(snapshot.last_sell_outcome["outcome"], "confirmed")
            recycled, _ = state.grid_decide("XRP", "95", config)
            self.assertEqual(recycled.level, 1)

    def test_reverted_take_profit_preserves_the_open_lot_for_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            self._confirmed_level_one(state, config)
            signal, _ = state.grid_take_profit_decide("XRP", "2.1", config)
            assert signal is not None
            state.claim_grid_take_profit(signal, config, pre_trade_usdt_balance=100)
            state.record_grid_attempt(signal, "0xreverted")

            restarted = StrategyStateStore(config.strategy_state_file)
            self.assertEqual(restarted.pending_trade().action, "sell")
            restarted.reject_grid_take_profit(signal, config)

            snapshot = restarted.grid_snapshot("XRP", config)
            assert snapshot is not None
            self.assertEqual(snapshot.filled_levels, (1,))
            self.assertEqual(snapshot.spent_usdt, "50")
            self.assertEqual(snapshot.last_sell_outcome["outcome"], "reverted")

    def test_legacy_grid_fill_without_lot_data_is_rejected_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            config = settings(path)
            path.write_text(
                """{
  "version": 3,
  "base_asset": "USDT",
  "pending": null,
  "last_trade_at": 0,
  "trades": [],
  "positions": {},
  "grid": {
    "version": 1,
    "config": {
      "symbols": ["XRP", "BTC"],
      "spacing_bps": 200,
      "levels": 5,
      "order_usdt": "10",
      "max_budget_usdt": "50"
    },
    "assets": {
      "XRP": {
        "anchor_price": "100",
        "levels": ["98", "96", "94", "92", "90"],
        "filled_levels": [1],
        "spent_usdt": "10",
        "paused_at_floor": false
      }
    }
  }
}""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StrategyGuardError, "lack take-profit lot details"):
                StrategyStateStore(config.strategy_state_file).grid_snapshot("XRP", config)

    def test_price_below_grid_floor_pauses_without_a_buy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            state.grid_decide("BTC", "100", config)

            signal, snapshot = state.grid_decide("BTC", "74.99", config)

            self.assertIsNone(signal)
            self.assertEqual(snapshot.status, "paused_at_floor")
            self.assertEqual(snapshot.spent_usdt, "0")

    def test_pending_grid_claim_blocks_duplicate_buy_until_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            state.grid_decide("XRP", "100", config)
            signal, _ = state.grid_decide("XRP", "95", config)

            state.claim_grid(signal, config, pre_trade_token_balance=0)
            state.record_grid_attempt(signal, "0xpending", phase="swap")

            with self.assertRaisesRegex(StrategyGuardError, "already pending"):
                state.claim_grid(signal, config, pre_trade_token_balance=0)
            self.assertEqual(state.pending_trade().kind, "grid")

    def test_grid_keeps_its_progress_separate_from_a_paused_ema_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            ema_signal = StrategySignal("buy", "XRP", "10", "1", "test")
            state.claim(
                ema_signal,
                now=1_000,
                cooldown_seconds=1,
                max_trades_per_day=3,
            )
            state.confirm(
                ema_signal,
                TradeResult("buy", "XRP", "10", "10", "0xema", 1),
                now=1_000,
            )

            signal, snapshot = state.grid_decide("XRP", "100", config)

            self.assertIsNone(signal)
            self.assertEqual(snapshot.anchor_price, "100")
            self.assertEqual(state.position("XRP").amount, "10")


class GridExecutionTests(unittest.TestCase):
    @staticmethod
    def _signal(config: Settings) -> GridSignal:
        state = StrategyStateStore(config.strategy_state_file)
        state.grid_decide("XRP", "100", config)
        signal, _ = state.grid_decide("XRP", "95", config)
        assert signal is not None
        return signal

    def test_ema_execution_is_blocked_while_grid_mode_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = settings(Path(directory) / "state.json")

            with self.assertRaisesRegex(StrategyGuardError, "EMA/RSI execution is paused"):
                trader.execute_strategy("XRP", [10, 9, 8, 7, 8, 9], available_usdt="50")

    def test_uncertain_grid_broadcast_keeps_level_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader._broadcast_attempted = False
            signal = self._signal(config)

            class BalanceCall:
                def call(self) -> int:
                    return 0

            class Functions:
                def balanceOf(self, _address: str) -> BalanceCall:
                    return BalanceCall()

            class Token:
                functions = Functions()

            trader._token = lambda _symbol: Token()  # type: ignore[method-assign]
            trader.account = type(
                "Account", (), {"address": "0x0000000000000000000000000000000000000000"}
            )()

            def uncertain_execute(_action: str, _symbol: str, _amount: str) -> TradeResult:
                trader._broadcast_attempted = True
                raise RuntimeError("RPC connection dropped after send attempt")

            trader.execute = uncertain_execute  # type: ignore[method-assign]

            with self.assertRaises(RuntimeError):
                trader.execute_grid(signal, available_usdt="50")
            self.assertEqual(
                StrategyStateStore(config.strategy_state_file).pending_trade().kind, "grid"
            )

    def test_uncertain_immediate_broadcast_cannot_create_a_second_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(Path(directory) / "state.json"),
                buy_immediately_now=True,
            )
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader._broadcast_attempted = False
            state = StrategyStateStore(config.strategy_state_file)
            signal, _ = state.grid_decide("XRP", "100", config)
            assert signal is not None and signal.immediate

            class BalanceCall:
                def call(self) -> int:
                    return 0

            class Functions:
                def balanceOf(self, _address: str) -> BalanceCall:
                    return BalanceCall()

            class Token:
                functions = Functions()

            trader._token = lambda _symbol: Token()  # type: ignore[method-assign]
            trader.account = type(
                "Account", (), {"address": "0x0000000000000000000000000000000000000000"}
            )()

            def uncertain_execute(_action: str, _symbol: str, _amount: str) -> TradeResult:
                trader._broadcast_attempted = True
                raise RuntimeError("RPC connection dropped after send attempt")

            trader.execute = uncertain_execute  # type: ignore[method-assign]

            with self.assertRaises(RuntimeError):
                trader.execute_grid(signal, available_usdt="50")

            repeated, snapshot = StrategyStateStore(
                config.strategy_state_file
            ).grid_decide("XRP", "100", config)
            self.assertIsNone(repeated)
            self.assertEqual(snapshot.spent_usdt, "0")
            self.assertIsNotNone(
                StrategyStateStore(config.strategy_state_file).pending_trade()
            )

    def test_insufficient_take_profit_token_balance_releases_unbroadcast_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            GridStateTests._confirmed_level_one(state, config)
            signal, _ = state.grid_take_profit_decide("XRP", "2.1", config)
            assert signal is not None

            class BalanceCall:
                def call(self) -> int:
                    return 100

            class Functions:
                def balanceOf(self, _address: str) -> BalanceCall:
                    return BalanceCall()

            class Token:
                functions = Functions()

            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader._broadcast_attempted = False
            trader._strategy_broadcast_callback = None
            trader.account = type(
                "Account", (), {"address": "0x0000000000000000000000000000000000000000"}
            )()
            trader._token = lambda _symbol: Token()  # type: ignore[method-assign]

            def insufficient_balance(_action: str, _symbol: str, _amount: str) -> TradeResult:
                raise TraderError("Insufficient XRP balance for the requested trade.")

            trader.execute = insufficient_balance  # type: ignore[method-assign]

            with self.assertRaisesRegex(TraderError, "Insufficient XRP balance"):
                trader.execute_grid_take_profit(signal)
            self.assertIsNone(StrategyStateStore(config.strategy_state_file).pending_trade())

    def test_take_profit_receipt_reconciliation_closes_only_its_saved_lot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            GridStateTests._confirmed_level_one(state, config)
            signal, _ = state.grid_take_profit_decide("XRP", "2.1", config)
            assert signal is not None
            state.claim_grid_take_profit(
                signal, config, pre_trade_usdt_balance=100 * 10**18
            )
            state.record_grid_attempt(signal, "0xreceipt")

            class BalanceCall:
                def call(self) -> int:
                    return 110 * 10**18

            class DecimalsCall:
                def call(self) -> int:
                    return 18

            class Functions:
                def balanceOf(self, _address: str) -> BalanceCall:
                    return BalanceCall()

                def decimals(self) -> DecimalsCall:
                    return DecimalsCall()

            class Token:
                functions = Functions()

            class Eth:
                @staticmethod
                def get_transaction_receipt(_tx_hash: str):
                    return {"status": 1, "blockNumber": 2}

            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader.w3 = type("Web3", (), {"eth": Eth()})()
            trader.account = type(
                "Account", (), {"address": "0x0000000000000000000000000000000000000000"}
            )()
            trader._token = lambda _symbol: Token()  # type: ignore[method-assign]

            result = trader.reconcile_grid()

            self.assertEqual(result, TradeResult("sell", "XRP", "25", "10", "0xreceipt", 2))
            snapshot = StrategyStateStore(config.strategy_state_file).grid_snapshot("XRP", config)
            assert snapshot is not None
            self.assertEqual(snapshot.open_lots, ())
            self.assertEqual(snapshot.last_sell_outcome["tx_hash"], "0xreceipt")

    def test_reverted_take_profit_receipt_keeps_lot_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = settings(Path(directory) / "state.json")
            state = StrategyStateStore(config.strategy_state_file)
            GridStateTests._confirmed_level_one(state, config)
            signal, _ = state.grid_take_profit_decide("XRP", "2.1", config)
            assert signal is not None
            state.claim_grid_take_profit(signal, config, pre_trade_usdt_balance=0)
            state.record_grid_attempt(signal, "0xreverted-receipt")

            class Eth:
                @staticmethod
                def get_transaction_receipt(_tx_hash: str):
                    return {"status": 0, "blockNumber": 2}

            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader.w3 = type("Web3", (), {"eth": Eth()})()

            self.assertIsNone(trader.reconcile_grid())
            snapshot = StrategyStateStore(config.strategy_state_file).grid_snapshot("XRP", config)
            assert snapshot is not None
            self.assertEqual(snapshot.filled_levels, (1,))
            self.assertEqual(snapshot.last_sell_outcome["outcome"], "reverted")


if __name__ == "__main__":
    unittest.main()