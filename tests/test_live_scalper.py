from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import Event
from threading import Thread
from unittest.mock import patch
from types import SimpleNamespace

from bot.live_scalper import (
    LiveScalper,
    LiveScalperError,
    ScalperAmountConfig,
    ScalperPaths,
    parse_quote_json,
)
from bot.trader import PancakeSwapTrader, TradeResult


@dataclass
class FakeTrader:
    calls: list[tuple[str, str, str]]
    pending: object | None = None
    reconciled: int = 0
    reconciliation_result: TradeResult | None = None

    def inspect_pending(self):
        return self.pending

    def reconcile_manual(self, *, confirmation_callback=None):
        self.reconciled += 1
        self.pending = None
        if self.reconciliation_result is not None and confirmation_callback is not None:
            confirmation_callback(self.reconciliation_result)
        return self.reconciliation_result

    def execute(self, action, symbol, amount, *, confirmation_callback=None):
        self.calls.append((action, symbol, amount))
        result = TradeResult(action, symbol, "1", "2", f"0x{len(self.calls)}", 1)
        if confirmation_callback is not None:
            confirmation_callback(result)
        return result

class IdleConnection:
    def __init__(self, stop: Event):
        self.stop, self.pings, self.closed = stop, 0, False
    def recv(self):
        self.stop.set()
        return json.dumps({"s": "XRPUSDT", "p": "100", "E": 100_000})
    def ping(self):
        self.pings += 1
    def close(self):
        self.closed = True


class LiveScalperTests(unittest.TestCase):
    def quote(self, price: str, event: int = 100_000) -> str:
        return json.dumps({"s": "XRPUSDT", "p": price, "E": event})

    def new_scalper(self, root: Path, trader: FakeTrader) -> LiveScalper:
        root = root / "bot"
        paths = ScalperPaths("test", root, root / "config.json", root / "state.json",
                            root / "trader-state.json",
                            root / "prices.json", root / "notifications.json",
                            root / "errors.json", root / "test.log")
        paths.root.mkdir()
        ScalperAmountConfig(paths.config).save("123.45", now=100)
        return LiveScalper(SimpleNamespace(enable_live_trading=True), paths=paths,
                           market_symbol="XRPUSDT", trade_symbol="XRP", trader=trader,
                           clock=lambda: 100)

    def test_parser_rejects_invalid_stale_and_out_of_order_quotes(self):
        quote = parse_quote_json(self.quote("1.25"), "XRPUSDT", received_at=100)
        self.assertEqual(quote.price, Decimal("1.25"))
        for payload in ('[]', '{"s":"BTCUSDT","p":"1","E":100}', '{"s":"XRPUSDT","p":"0","E":100}'):
            with self.assertRaises(LiveScalperError):
                parse_quote_json(payload, "XRPUSDT", received_at=100)
        with tempfile.TemporaryDirectory() as temporary:
            scalper = self.new_scalper(Path(temporary), FakeTrader([]))
            scalper.process_payload(self.quote("1", 90_000))
            with self.assertRaises(LiveScalperError):
                scalper.process_payload(self.quote("1.1", 90_000))
            with self.assertRaises(LiveScalperError):
                scalper.process_payload(self.quote("1.1", 70_000))

    def test_reversal_signal_uses_dynamic_amount_and_fresh_trader(self):
        with tempfile.TemporaryDirectory() as temporary:
            trader = FakeTrader([])
            scalper = self.new_scalper(Path(temporary), trader)
            scalper.process_payload(self.quote("100", 98_000))
            scalper.process_payload(self.quote("99", 99_000))
            result = scalper.process_payload(self.quote("100", 100_000))
            self.assertEqual(result.action, "buy")
            self.assertEqual(trader.calls, [("buy", "XRP", "123.45")])
            self.assertTrue(scalper.paths.state.exists())
            self.assertTrue(scalper.paths.notifications.exists())

    def test_bad_config_blocks_entry_but_pending_reconciles_and_exit_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            trader = FakeTrader([], pending=object())
            scalper = self.new_scalper(Path(temporary), trader)
            scalper.paths.config.unlink()
            scalper.position = (Decimal("2"), Decimal("100"))
            result = scalper.process_payload(self.quote("102", 100_000))
            self.assertEqual(result.action, "sell")
            self.assertEqual(trader.reconciled, 1)
            self.assertEqual(trader.calls[0][:2], ("sell", "XRP"))

    def test_amount_config_rejects_future_timestamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = ScalperAmountConfig(Path(temporary) / "config.json")
            config.save("999999999", now=200)
            with self.assertRaises(LiveScalperError):
                config.read(now=100)

    def test_five_entrypoints_have_unique_fixed_mappings(self):
        expected = {
            "scalper_bot_7": ("BNBUSDT", "WBNB"), "scalper_bot_8": ("ETHUSDT", "ETH"),
            "scalper_bot_9": ("SOLUSDT", "SOL"), "scalper_bot_10": ("XRPUSDT", "XRP"),
            "scalper_bot_11": ("BTCUSDT", "BTC"),
        }
        roots = []
        for module, mapping in expected.items():
            loaded = importlib.import_module(module)
            self.assertEqual((loaded.MARKET_SYMBOL, loaded.TRADE_SYMBOL), mapping)
            self.assertEqual(loaded.PATHS.bot, module)
            roots.append(loaded.PATHS.root)
        self.assertEqual(len(set(roots)), 5)

    def test_run_heartbeats_injected_connection_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            trader = FakeTrader([])
            scalper = self.new_scalper(Path(temporary), trader)
            stop = Event()
            connection = IdleConnection(stop)
            scalper.stop_event = stop
            scalper.connection_factory = lambda: connection
            scalper.run()
            self.assertEqual(connection.pings, 1)
            self.assertTrue(connection.closed)

    def test_position_state_never_clobbers_trader_journal_and_restart_remembers_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            trader = FakeTrader([])
            scalper = self.new_scalper(Path(temporary), trader)
            scalper.paths.trader_state.write_text('{"pending":{"tx_hash":"0xabc"}}\n')
            scalper.position = (Decimal("2"), Decimal("100"))
            scalper.intent = None
            scalper._save_state()
            self.assertIn("0xabc", scalper.paths.trader_state.read_text())
            scalper._save_prices(parse_quote_json(self.quote("100", 100_000), "XRPUSDT"))
            restarted = LiveScalper(SimpleNamespace(enable_live_trading=True), paths=scalper.paths,
                                    market_symbol="XRPUSDT", trade_symbol="XRP",
                                    trader=FakeTrader([]), clock=lambda: 100)
            with self.assertRaises(LiveScalperError):
                restarted.process_payload(self.quote("101", 100_000))

    def test_all_path_fields_are_unique_within_each_bot(self):
        for number in range(7, 12):
            paths = ScalperPaths.for_bot(f"scalper_bot_{number}")
            values = (paths.config, paths.state, paths.trader_state, paths.prices,
                      paths.notifications, paths.errors, paths.log)
            self.assertEqual(len(values), len(set(values)))

    def test_confirmed_buy_recovery_finalizes_every_asset_before_new_signal(self):
        mappings = (
            ("BNBUSDT", "WBNB"),
            ("ETHUSDT", "ETH"),
            ("SOLUSDT", "SOL"),
            ("XRPUSDT", "XRP"),
            ("BTCUSDT", "BTC"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            for index, (market_symbol, trade_symbol) in enumerate(mappings):
                root = Path(temporary) / str(index)
                root.mkdir()
                paths = ScalperPaths(
                    f"test-{index}", root, root / "config.json",
                    root / "state.json", root / "trader-state.json",
                    root / "prices.json", root / "notifications.json",
                    root / "errors.json", root / "test.log",
                )
                ScalperAmountConfig(paths.config).save("5", now=100)
                initial = LiveScalper(
                    SimpleNamespace(enable_live_trading=True),
                    paths=paths, market_symbol=market_symbol,
                    trade_symbol=trade_symbol, trader=FakeTrader([]),
                    clock=lambda: 100,
                )
                initial._begin_intent("buy", Decimal("100"))
                result = TradeResult(
                    "buy", trade_symbol, "5", "2",
                    f"0x{index + 1}", index + 1,
                )
                trader = FakeTrader(
                    [], pending=object(), reconciliation_result=result
                )
                restarted = LiveScalper(
                    SimpleNamespace(enable_live_trading=True),
                    paths=paths, market_symbol=market_symbol,
                    trade_symbol=trade_symbol, trader=trader,
                    clock=lambda: 100,
                )
                payload = json.dumps(
                    {"s": market_symbol, "p": "100", "E": 100_000}
                )
                self.assertIs(restarted.process_payload(payload), result)
                self.assertEqual(restarted.position, (Decimal("2"), Decimal("100")))
                self.assertIsNone(restarted.intent)
                self.assertEqual(trader.calls, [])

    def test_confirmed_sell_recovery_clears_position_before_new_signal(self):
        with tempfile.TemporaryDirectory() as temporary:
            trader = FakeTrader([])
            initial = self.new_scalper(Path(temporary), trader)
            initial.position = (Decimal("2"), Decimal("100"))
            initial._save_state()
            initial._begin_intent("sell", Decimal("102"))
            result = TradeResult("sell", "XRP", "2", "204", "0xsell", 2)
            recovering = FakeTrader(
                [], pending=object(), reconciliation_result=result
            )
            restarted = LiveScalper(
                SimpleNamespace(enable_live_trading=True),
                paths=initial.paths, market_symbol="XRPUSDT",
                trade_symbol="XRP", trader=recovering, clock=lambda: 100,
            )
            self.assertIs(
                restarted.process_payload(self.quote("102", 100_000)), result
            )
            self.assertIsNone(restarted.position)
            self.assertIsNone(restarted.intent)
            self.assertEqual(recovering.calls, [])

    def test_replayed_buy_and_sell_confirmations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            initial = self.new_scalper(Path(temporary), FakeTrader([]))
            buy = TradeResult("buy", "XRP", "5", "2", "0xbuy", 1)
            initial._begin_intent("buy", Decimal("100"))
            initial._finalize_result(buy)

            buy_recovery = FakeTrader(
                [], pending=object(), reconciliation_result=buy
            )
            after_buy = LiveScalper(
                SimpleNamespace(enable_live_trading=True),
                paths=initial.paths, market_symbol="XRPUSDT",
                trade_symbol="XRP", trader=buy_recovery, clock=lambda: 100,
            )
            self.assertIs(
                after_buy.process_payload(self.quote("100", 99_000)), buy
            )
            self.assertEqual(after_buy.position, (Decimal("2"), Decimal("100")))

            sell = TradeResult("sell", "XRP", "2", "204", "0xsell", 2)
            after_buy._begin_intent("sell", Decimal("102"))
            after_buy._finalize_result(sell)
            sell_recovery = FakeTrader(
                [], pending=object(), reconciliation_result=sell
            )
            after_sell = LiveScalper(
                SimpleNamespace(enable_live_trading=True),
                paths=initial.paths, market_symbol="XRPUSDT",
                trade_symbol="XRP", trader=sell_recovery, clock=lambda: 100,
            )
            self.assertIs(
                after_sell.process_payload(self.quote("102", 100_000)), sell
            )
            self.assertIsNone(after_sell.position)

    def test_notification_write_failure_cannot_fail_confirmed_position(self):
        with tempfile.TemporaryDirectory() as temporary:
            scalper = self.new_scalper(Path(temporary), FakeTrader([]))
            scalper.process_payload(self.quote("100", 98_000))
            scalper.process_payload(self.quote("99", 99_000))
            with patch.object(
                scalper, "_record", side_effect=OSError("disk full")
            ):
                result = scalper.process_payload(self.quote("100", 100_000))
            self.assertEqual(result.action, "buy")
            self.assertEqual(scalper.position, (Decimal("2"), Decimal("100")))
            self.assertIsNone(scalper.intent)

    def test_wallet_nonce_lock_serializes_and_uses_public_address_path(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "bot.trader.Path.home", return_value=Path(temporary)
        ):
            trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
            trader.account = SimpleNamespace(address="0xABcDEF")
            first_acquired, release_first, second_acquired = Event(), Event(), Event()

            def first():
                with trader._nonce_locked():
                    first_acquired.set()
                    release_first.wait(1)

            def second():
                with trader._nonce_locked():
                    second_acquired.set()

            one = Thread(target=first)
            two = Thread(target=second)
            one.start(); self.assertTrue(first_acquired.wait(1))
            two.start()
            self.assertFalse(second_acquired.wait(0.05))
            release_first.set(); one.join(1); two.join(1)
            self.assertTrue(second_acquired.is_set())
            path = Path(temporary) / ".local/state/bnb-defi-bot/nonces/0xabcdef.lock"
            self.assertTrue(path.exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()