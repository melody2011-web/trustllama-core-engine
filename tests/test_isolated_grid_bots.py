from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import grid_bot_4
import grid_bot_5
import grid_bot_6
from bot.config import ConfigurationError, Settings
from bot.contracts import PANCAKESWAP_SMART_ROUTER, TOKENS
from bot.isolated_grid import IsolatedGridConfig, IsolatedGridConfigError
from bot.live_runner import LivePriceStore, LiveRunnerError
from bot.smart_router import SmartRouterTrade
from bot.trader import (
    PancakeSwapTrader, StrategyGuardError, StrategyStateStore, GridSignal,
    TradeResult, TraderError, WBNB_WITHDRAWAL_TOPIC,
)


def settings(profile: str | None = None) -> Settings:
    symbol = {"grid_bot_4": "WBNB", "grid_bot_5": "ETH", "grid_bot_6": "SOL"}.get(profile, "XRP")
    return Settings(
        bsc_rpc_url="http://invalid", wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="unused", enable_live_trading=True, slippage_bps=100,
        deadline_seconds=120, receipt_timeout_seconds=120, max_gas_limit=700000,
        live_strategy_mode="grid", grid_symbols=(symbol,), grid_spacing_bps=100 if profile else 500,
        grid_levels=6 if profile else 5, grid_order_usdt=Decimal("50"),
        grid_max_budget_usdt=Decimal("300") if profile else Decimal("250"),
        grid_take_profit_bps=100 if profile else 500, buy_immediately_now=bool(profile),
        isolated_grid_profile=profile,
    )


class IsolatedGridBotsTests(unittest.TestCase):
    def test_canonical_addresses_and_launcher_paths_are_separate(self):
        self.assertEqual(TOKENS["ETH"].lower(), "0x2170ed0880ac9a755fd29b2688956bd959f933f8")
        self.assertEqual(TOKENS["SOL"].lower(), "0x570a5d26f7765ecb712c0924e4de545b89fd43df")
        roots = (grid_bot_4, grid_bot_5, grid_bot_6)
        paths = []
        for root, prefix in zip(roots, ("GRID_BOT_4", "GRID_BOT_5", "GRID_BOT_6")):
            self.assertTrue(hasattr(root, f"{prefix}_PRICE_HISTORY"))
            self.assertTrue(hasattr(root, f"{prefix}_ERRORS"))
            paths.extend(str(getattr(root, f"{prefix}_{kind}_PATH")) for kind in ("STATE", "CONFIG", "LOG", "PRICE"))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertIn("BNB", grid_bot_4.__doc__)

    def test_config_default_mode_corruption_missing_and_stale_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "one.json"
            config = IsolatedGridConfig(path)
            self.assertEqual(config.initialize_default(), Decimal("50"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(IsolatedGridConfigError):
                config.initialize_default()
            path.unlink()
            with self.assertRaises(IsolatedGridConfigError):
                config.read()
            path.write_text(json.dumps({"version": 1, "per_line_usdt": "50", "updated_at": time.time() - 31 * 86400}))
            with self.assertRaises(IsolatedGridConfigError):
                config.read()

    def test_profiles_and_grid1_constraints(self):
        for profile in ("grid_bot_4", "grid_bot_5", "grid_bot_6"):
            config = settings(profile)
            self.assertEqual(config.grid_levels, 6)
            self.assertLessEqual(config.grid_order_usdt * 6, Decimal("300"))
        with self.assertRaises(ConfigurationError):
            replace(settings(), grid_symbols=("ETH",))

    def test_wbnb_sell_is_only_bot4_and_router_flags_are_correct(self):
        trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
        trader.settings = settings("grid_bot_4")
        self.assertEqual(trader._amount_in_wei("sell", "WBNB", "1")[0], 10**18)
        trader.settings = settings("grid_bot_5")
        with self.assertRaises(TraderError):
            trader._amount_in_wei("sell", "WBNB", "1")
        trader.settings = type("ScalperSettings", (), {"isolated_grid_profile": None, "isolated_scalper_profile": "scalper_bot_7"})()
        self.assertEqual(trader._amount_in_wei("sell", "WBNB", "1")[0], 10**18)
        trader.settings = type("ScalperSettings", (), {"isolated_grid_profile": None, "isolated_scalper_profile": "scalper_bot_8"})()
        with self.assertRaises(TraderError):
            trader._amount_in_wei("sell", "WBNB", "1")
        calls = []
        trader.settings = settings("grid_bot_4")
        trader.account = type("A", (), {"address": "0x0000000000000000000000000000000000000000"})()
        trader.smart_router = type("B", (), {"build_trade": lambda _s, *args, **kw: (calls.append(kw), SmartRouterTrade(1, 1, 0, 1, 0, "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4", "0x01", 10 if kw["native_input"] else 0, 1))[1]})()
        trader._build_smart_router_trade("buy", "WBNB", 10, 1000)
        trader._build_smart_router_trade("sell", "WBNB", 10, 1000)
        self.assertEqual((calls[0]["native_input"], calls[0]["native_output"], calls[0]["direct_v3"]), (False, True, False))
        self.assertEqual((calls[1]["native_input"], calls[1]["native_output"], calls[1]["direct_v3"]), (True, False, False))
        trader.settings = settings("grid_bot_5")
        trader._build_smart_router_trade("buy", "ETH", 10, 1000)
        trader.settings = settings("grid_bot_6")
        trader._build_smart_router_trade("sell", "SOL", 10, 1000)
        self.assertEqual((calls[2]["native_input"], calls[2]["native_output"], calls[2]["direct_v3"]), (False, False, False))
        self.assertEqual((calls[3]["native_input"], calls[3]["native_output"], calls[3]["direct_v3"]), (False, False, False))
        # The saved TP proceeds are an additional floor over normal router
        # slippage: one raw USDT wei below is rejected, equality is allowed.
        trader.settings = settings("grid_bot_5")
        trader._required_grid_minimum_out = 2
        with self.assertRaisesRegex(StrategyGuardError, "below the saved grid"):
            trader._build_smart_router_trade("sell", "ETH", 10, 1000)
        trader._required_grid_minimum_out = 1
        trader._build_smart_router_trade("sell", "ETH", 10, 1000)

    def test_price_store_is_strictly_single_symbol(self):
        with tempfile.TemporaryDirectory() as directory:
            store = LivePriceStore(str(Path(directory) / "prices.json"), ("ETH",))
            store.record_price("ETH", Decimal("1"), 5)
            with self.assertRaises(LiveRunnerError):
                store.record_price("SOL", Decimal("1"), 5)

    def test_bot4_reconciliation_requires_router_withdrawal_event(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(settings("grid_bot_4"), strategy_state_file=str(Path(directory) / "state.json"))
            state = StrategyStateStore(config.strategy_state_file)
            state.grid_decide("WBNB", "100", config)
            signal, _ = state.grid_decide("WBNB", "99", config)
            assert signal is not None
            state.claim_grid(signal, config, pre_trade_token_balance=0)
            state.record_grid_attempt(signal, "0xbuy")
            account = "0x0000000000000000000000000000000000000001"
            good_receipt = {
                "status": 1, "blockNumber": 7, "transactionHash": type("H", (), {"hex": lambda self: "0xbuy"})(),
                "logs": [{"address": TOKENS["WBNB"], "topics": [WBNB_WITHDRAWAL_TOPIC, "0x" + "0" * 24 + PANCAKESWAP_SMART_ROUTER[2:]], "data": hex(2 * 10**18)}],
            }
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.settings = config
            trader.account = type("A", (), {"address": account})()
            trader.w3 = type("W", (), {"eth": type("E", (), {"get_transaction_receipt": lambda _s, _h: good_receipt})()})()
            trader._token = lambda _symbol: (_ for _ in ()).throw(AssertionError("native fill must not use ERC20 delta"))
            result = trader.reconcile_grid()
            self.assertEqual(result.amount_out, "2")

            # A confirmed receipt without the trusted-router Withdrawal is not a fill:
            # the pending claim stays durably blocked for operator reconciliation.
            state = StrategyStateStore(config.strategy_state_file)
            signal2, _ = state.grid_decide("WBNB", "98", config)
            assert signal2 is not None
            state.claim_grid(signal2, config, pre_trade_token_balance=0)
            state.record_grid_attempt(signal2, "0xbad")
            trader.w3.eth.get_transaction_receipt = lambda _h: {**good_receipt, "logs": []}
            with self.assertRaises(StrategyGuardError):
                trader.reconcile_grid()
            self.assertIsNotNone(StrategyStateStore(config.strategy_state_file).pending_trade())


if __name__ == "__main__":
    unittest.main()