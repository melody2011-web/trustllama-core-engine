from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from web3.middleware import ExtraDataToPOAMiddleware

from bot.config import ConfigurationError, Settings
from bot.main import _prices
from bot.trader import (
    LiveTradingDisabled,
    PancakeSwapTrader,
    PendingTrade,
    StrategyGuardError,
    StrategyPosition,
    StrategyRules,
    StrategySignal,
    StrategyStateStore,
    TraderError,
    TradeResult,
)


def settings(**overrides: object) -> Settings:
    base = Settings(
        bsc_rpc_url="http://example.invalid",
        wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="not-used-by-these-tests",
        enable_live_trading=True,
        slippage_bps=100,
        deadline_seconds=120,
        receipt_timeout_seconds=180,
        max_gas_limit=700_000,
        strategy_fast_window=2,
        strategy_slow_window=3,
        strategy_rsi_window=2,
        strategy_buy_rsi_max=100,
        live_strategy_mode="ema",
    )
    return replace(base, **overrides)


class StrategyRulesTests(unittest.TestCase):
    def test_strategy_position_size_is_locked_to_twenty_percent(self) -> None:
        with self.assertRaisesRegex(ConfigurationError, "must be exactly 2000"):
            settings(strategy_position_bps=1_000)

    def test_rsi_handles_all_losses_without_division_error(self) -> None:
        rules = StrategyRules(settings(strategy_rsi_window=2))

        self.assertEqual(rules._rsi([10, 9, 8]), 0)

    def test_rsi_handles_flat_history_with_neutral_value(self) -> None:
        rules = StrategyRules(settings(strategy_rsi_window=2))

        self.assertEqual(rules._rsi([10, 10, 10]), 50)

    def test_price_parser_rejects_non_finite_values(self) -> None:
        with self.assertRaises(TraderError):
            _prices("1,NaN")

    def test_bullish_crossover_sizes_twenty_percent_of_usdt(self) -> None:
        signal = StrategyRules(settings()).decide(
            "XRP", [10, 9, 8, 7, 8, 9], None, "1"
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "buy")
        self.assertEqual(signal.amount, "0.2")

    def test_bullish_crossover_rounds_usdt_entry_down_to_token_precision(self) -> None:
        signal = StrategyRules(settings()).decide(
            "XRP", [10, 9, 8, 7, 8, 9], None, "0.000000000000000009"
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.amount, "0.000000000000000001")

    def test_stop_loss_sells_the_whole_open_position(self) -> None:
        signal = StrategyRules(settings()).decide(
            "BTC",
            [100, 100, 100, 100, 100, 90],
            StrategyPosition(amount="0.012", entry_price="100"),
            "1",
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.action, "sell")
        self.assertEqual(signal.amount, "0.012")
        self.assertEqual(signal.reason, "stop-loss reached")


class StrategyStateTests(unittest.TestCase):
    def test_legacy_wbnb_position_blocks_usdt_trading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pending": None,
                        "positions": {"XRP": {"amount": "12", "entry_price": "9"}},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(StrategyGuardError, "Legacy WBNB-denominated"):
                StrategyStateStore(str(path)).position("XRP")

    def test_pending_trade_exposes_saved_manual_transaction_details_without_changing_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StrategyStateStore(str(Path(temporary_directory) / "state.json"))
            state.claim_manual("sell", "XRP", "1.5", now=10_000)
            fingerprint = state._manual_fingerprint("sell", "XRP", "1.5")
            state.record_manual_attempt(fingerprint, "0xhash", phase="approval")

            pending = state.pending_trade()

            self.assertIsNotNone(pending)
            self.assertEqual(pending.kind, "manual")
            self.assertEqual(pending.action, "sell")
            self.assertEqual(pending.symbol, "XRP")
            self.assertEqual(pending.amount, "1.5")
            self.assertEqual(pending.tx_hash, "0xhash")
            self.assertEqual(pending.phase, "approval")
            self.assertIsNotNone(state.pending())

    def test_pending_trade_reports_strategy_claim_without_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StrategyStateStore(str(Path(temporary_directory) / "state.json"))
            signal = StrategySignal("buy", "BTC", "0.1", "50", "test")
            state.claim(signal, now=10_000, cooldown_seconds=900, max_trades_per_day=3)

            pending = state.pending_trade()

            self.assertEqual(
                pending,
                PendingTrade(
                    kind="strategy",
                    action="buy",
                    symbol="BTC",
                    amount="0.1",
                    tx_hash=None,
                    phase=None,
                ),
            )

    def test_pending_claim_blocks_another_strategy_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StrategyStateStore(str(Path(temporary_directory) / "state.json"))
            signal = StrategySignal("buy", "XRP", "0.1", "9", "test")
            state.claim(
                signal,
                now=10_000,
                cooldown_seconds=900,
                max_trades_per_day=3,
            )

            with self.assertRaises(StrategyGuardError):
                state.claim(
                    StrategySignal("buy", "BTC", "0.1", "1", "test"),
                    now=10_001,
                    cooldown_seconds=900,
                    max_trades_per_day=3,
                )
            state.record_broadcast(signal, "0xhash")
            self.assertEqual(state.pending()["tx_hash"], "0xhash")

    def test_confirmed_trade_persists_position_and_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StrategyStateStore(str(Path(temporary_directory) / "state.json"))
            signal = StrategySignal("buy", "XRP", "0.1", "9", "test")
            state.claim(
                signal,
                now=10_000,
                cooldown_seconds=900,
                max_trades_per_day=3,
            )
            state.confirm(
                signal,
                TradeResult("buy", "XRP", "0.1", "12", "0xhash", 1),
                now=10_000,
            )

            self.assertEqual(
                state.position("XRP"), StrategyPosition(amount="12", entry_price="9")
            )
            with self.assertRaises(StrategyGuardError):
                state.claim(
                    StrategySignal("sell", "XRP", "12", "8", "test"),
                    now=10_001,
                    cooldown_seconds=900,
                    max_trades_per_day=3,
                )

    def test_reconciled_approval_keeps_the_open_position(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = StrategyStateStore(str(Path(temporary_directory) / "state.json"))
            buy = StrategySignal("buy", "XRP", "0.1", "9", "test")
            state.claim(
                buy,
                now=10_000,
                cooldown_seconds=900,
                max_trades_per_day=3,
            )
            state.confirm(
                buy,
                TradeResult("buy", "XRP", "0.1", "12", "0xbuy", 1),
                now=10_000,
            )
            sell = StrategySignal("sell", "XRP", "12", "8", "test")
            state.claim(
                sell,
                now=11_000,
                cooldown_seconds=900,
                max_trades_per_day=3,
            )
            state.record_attempt(sell, "0xapproval", phase="approval")
            state.clear_confirmed_approval(sell)

            self.assertIsNone(state.pending())
            self.assertEqual(
                state.position("XRP"), StrategyPosition(amount="12", entry_price="9")
            )


class StrategyExecutionTests(unittest.TestCase):
    @patch("bot.trader.SmartRouterBridge")
    @patch("bot.trader.Account.from_key")
    @patch("bot.trader.Web3")
    def test_trader_initialization_injects_bsc_poa_middleware(
        self, web3_class, account_from_key, _smart_router_bridge
    ) -> None:
        web3 = web3_class.return_value
        web3.is_connected.return_value = True
        web3.eth.chain_id = 56
        account_from_key.return_value.address = settings().wallet_address

        PancakeSwapTrader(settings())

        web3.middleware_onion.inject.assert_called_once_with(
            ExtraDataToPOAMiddleware, layer=0
        )

    @staticmethod
    def _stub_token_balance(trader: PancakeSwapTrader) -> None:
        class Account:
            address = "0x0000000000000000000000000000000000000000"

        class BalanceCall:
            def call(self) -> int:
                return 0

        class Functions:
            def balanceOf(self, address: str) -> BalanceCall:
                return BalanceCall()

        class Token:
            functions = Functions()

        trader._token = lambda symbol: Token()  # type: ignore[method-assign]
        trader.account = Account()

    def test_disabled_live_gate_prevents_strategy_execution(self) -> None:
        trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
        trader.settings = settings(enable_live_trading=False)

        with self.assertRaises(LiveTradingDisabled):
            trader.execute_strategy(
                "XRP", [10, 9, 8, 7, 8, 9], available_usdt="1", now=10_000
            )

    def test_strategy_executes_through_the_existing_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = settings(
                strategy_state_file=str(Path(temporary_directory) / "state.json")
            )
            trader._transaction_broadcasted = False
            self._stub_token_balance(trader)
            calls: list[tuple[str, str, str]] = []

            def guarded_execute(action: str, symbol: str, amount: str) -> TradeResult:
                calls.append((action, symbol, amount))
                return TradeResult(action, symbol, amount, "12", "0xhash", 1)

            trader.execute = guarded_execute  # type: ignore[method-assign]
            result = trader.execute_strategy(
                "XRP", [10, 9, 8, 7, 8, 9], available_usdt="1", now=10_000
            )

            self.assertIsNotNone(result)
            self.assertEqual(calls, [("buy", "XRP", "0.2")])
            state = StrategyStateStore(trader.settings.strategy_state_file)
            self.assertEqual(
                state.position("XRP"), StrategyPosition(amount="12", entry_price="9")
            )

    def test_ambiguous_broadcast_failure_keeps_the_order_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = settings(
                strategy_state_file=str(Path(temporary_directory) / "state.json")
            )
            trader._transaction_broadcasted = False
            trader._broadcast_attempted = False
            self._stub_token_balance(trader)

            def uncertain_execute(action: str, symbol: str, amount: str) -> TradeResult:
                trader._broadcast_attempted = True
                raise RuntimeError("RPC connection dropped after send attempt")

            trader.execute = uncertain_execute  # type: ignore[method-assign]
            with self.assertRaises(RuntimeError):
                trader.execute_strategy(
                    "XRP", [10, 9, 8, 7, 8, 9], available_usdt="1", now=10_000
                )

            state = StrategyStateStore(trader.settings.strategy_state_file)
            self.assertIsNotNone(state.pending())


if __name__ == "__main__":
    unittest.main()
