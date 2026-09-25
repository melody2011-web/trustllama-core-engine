from __future__ import annotations

import json
import hashlib
import hmac
import os
import socket
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bot.config import ConfigurationError
from bot.live_runner import LivePriceStore, LiveTradingLoop
from bot.smart_router import SmartRouterQuote
from bot.trader import GridLot, GridSignal, GridSnapshot, GridTakeProfitSignal, TradeResult
from tests.test_paper_loop import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _LiveApiServer:
    def __init__(
        self,
        state_path: Path,
        *,
        enabled: bool,
        session_secret: str = "test-reconciliation-secret",
    ) -> None:
        self.state_path = state_path
        self.enabled = enabled
        self.session_secret = session_secret
        self.port = _free_port()
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/live/status"

    def __enter__(self) -> "_LiveApiServer":
        environment = os.environ.copy()
        environment.update(
            {
                "PORT": str(self.port),
                "LIVE_PRICE_HISTORY_FILE": str(self.state_path),
                "LIVE_POLL_INTERVAL_SECONDS": "60",
                "ENABLE_LIVE_TRADING": str(self.enabled).lower(),
                "SESSION_SECRET": self.session_secret,
                "TELEGRAM_SERVICES_ENABLED": "true",
            }
        )
        self.process = subprocess.Popen(
            ["node", "--enable-source-maps", "artifacts/api-server/dist/index.mjs"],
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                self.request()
                return self
            except (HTTPError, URLError, ConnectionError):
                time.sleep(0.05)
        raise RuntimeError("Live API server did not start.")

    def __exit__(self, *_exc: object) -> None:
        if self.process is None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def request(self) -> tuple[int, dict[str, object]]:
        try:
            with urlopen(self.url, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def resolve(
        self, tx_hash: str, resolution: str
    ) -> tuple[int, dict[str, object]]:
        token = hmac.new(
            self.session_secret.encode(),
            b"bnb-defi-bot/telegram-reconciliation/v1",
            hashlib.sha256,
        ).hexdigest()
        request = Request(
            f"http://127.0.0.1:{self.port}/api/notifications/reconciliation/{tx_hash}",
            data=json.dumps({"resolution": resolution}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Internal-Notification-Token": token,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)


class _Bridge:
    def quote(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: int,
        *,
        direct_v3: bool = False,
    ) -> SmartRouterQuote:
        assert input_symbol in {"XRP", "BTC"}
        assert output_symbol == "USDT"
        assert amount_in == 10**18
        assert direct_v3 is True
        return SmartRouterQuote(
            amount_out=10**18,
            route_count=3,
            v2_candidates=1,
            v3_candidates=2,
            stable_candidates=0,
        )


class _TakeProfitBridge:
    def __init__(self) -> None:
        self.amounts: list[int] = []

    def quote(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: int,
        *,
        direct_v3: bool = False,
    ) -> SmartRouterQuote:
        assert input_symbol == "XRP"
        assert output_symbol == "USDT"
        assert direct_v3 is True
        self.amounts.append(amount_in)
        if amount_in == 10**18:
            raise AssertionError("A generic one-token quote must not gate a take-profit sell.")
        assert amount_in == 5 * 10**18
        return SmartRouterQuote(10_200_000_000_000_000_000, 3, 1, 2, 0)


class _FailingBridge:
    def quote(
        self,
        _input_symbol: str,
        _output_symbol: str,
        _amount_in: int,
        *,
        direct_v3: bool = False,
    ) -> SmartRouterQuote:
        assert direct_v3 is True
        raise RuntimeError("RPC token or wallet details must not be journaled")


class _StopAfterWait:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, _timeout: float | None = None) -> bool:
        self.stopped = True
        return True


class _Trader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Decimal, ...]]] = []
        self.pending = None

    def inspect_pending(self):
        return self.pending

    def reconcile_strategy(self):
        return None

    def execute_strategy(self, symbol: str, prices: list[Decimal]):
        self.calls.append((symbol, tuple(prices)))
        return None


class _GridTrader:
    def __init__(self) -> None:
        self.grid_calls: list[str] = []
        self.ema_calls = 0
        self.executions: list[GridSignal] = []

    def inspect_pending(self):
        return None

    def reconcile_grid(self):
        return None

    def grid_take_profit_decide(self, _symbol: str, _price: Decimal):
        return None, None

    def grid_decide(self, symbol: str, _price: Decimal):
        self.grid_calls.append(symbol)
        snapshot = GridSnapshot(
            symbol=symbol,
            anchor_price="1",
            levels=("0.98", "0.96", "0.94", "0.92", "0.9"),
            filled_levels=(),
            spent_usdt="0",
            remaining_usdt="50",
            status="active",
        )
        if symbol == "XRP":
            return GridSignal("XRP", 1, "10", "0.98", "1"), snapshot
        return None, snapshot

    def grid_snapshot(self, symbol: str):
        return GridSnapshot(
            symbol=symbol,
            anchor_price="1",
            levels=("0.98", "0.96", "0.94", "0.92", "0.9"),
            filled_levels=(1,),
            spent_usdt="10",
            remaining_usdt="40",
            status="active",
        )

    def execute_grid(self, signal: GridSignal):
        self.executions.append(signal)
        return TradeResult("buy", signal.symbol, "10", "10.2", "0xgrid", 1)

    def execute_strategy(self, *_args, **_kwargs):
        self.ema_calls += 1
        raise AssertionError("EMA execution must not run in grid mode")


class _GridTakeProfitTrader:
    def __init__(self) -> None:
        self.take_profit_calls: list[str] = []
        self.buy_decisions = 0
        self.executions: list[GridTakeProfitSignal] = []
        self.closed = False

    @staticmethod
    def _open_snapshot() -> GridSnapshot:
        return GridSnapshot(
            symbol="XRP",
            anchor_price="1",
            levels=("0.98", "0.96", "0.94", "0.92", "0.9"),
            filled_levels=(1,),
            spent_usdt="10",
            remaining_usdt="40",
            status="active",
            open_lots=(GridLot("XRP:1:0xbuy", 1, "5", "10", "2", "2.04", "0xbuy"),),
        )

    def inspect_pending(self):
        return None

    def reconcile_grid(self):
        return None

    def grid_take_profit_decide(
        self, symbol: str, _price: Decimal, *, lot_id: str | None = None
    ):
        self.take_profit_calls.append(symbol)
        if symbol == "XRP":
            assert lot_id == "XRP:1:0xbuy"
            return (
                GridTakeProfitSignal("XRP", 1, "5", "2.04", "2.04", "1", "XRP:1:0xbuy"),
                self._open_snapshot(),
            )
        return None, None

    def execute_grid_take_profit(self, signal: GridTakeProfitSignal):
        self.executions.append(signal)
        self.closed = True
        return TradeResult("sell", signal.symbol, "5", "10.25", "0xsell", 2)

    def grid_decide(self, _symbol: str, _price: Decimal):
        self.buy_decisions += 1
        raise AssertionError("A take-profit sell must be prioritized over a grid buy.")

    def grid_snapshot(self, _symbol: str):
        if not self.closed:
            return self._open_snapshot()
        return GridSnapshot(
            symbol="XRP",
            anchor_price="1",
            levels=("0.98", "0.96", "0.94", "0.92", "0.9"),
            filled_levels=(),
            spent_usdt="0",
            remaining_usdt="50",
            status="active",
            last_sell_outcome={"outcome": "confirmed"},
        )


class LiveTradingLoopTests(unittest.TestCase):
    def test_refuses_to_start_without_explicit_live_flag(self) -> None:
        with self.assertRaises(ConfigurationError):
            LiveTradingLoop(replace(settings(), enable_live_trading=False))

    def test_warms_history_then_delegates_to_guarded_strategy_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(
                    strategy_fast_window=2,
                    strategy_slow_window=3,
                    strategy_rsi_window=2,
                    live_price_history_limit=10,
                    live_strategy_mode="ema",
                ),
                live_price_history_file=str(Path(directory) / "prices.json"),
            )
            trader = _Trader()
            loop = LiveTradingLoop(
                config,
                bridge=_Bridge(),
                trader=trader,
                prices=LivePriceStore(config.live_price_history_file),
            )

            loop.run_once()
            loop.run_once()
            loop.run_once()
            loop.run_once()

            self.assertEqual([symbol for symbol, _history in trader.calls], ["XRP", "BTC"])
            self.assertEqual([len(history) for _symbol, history in trader.calls], [4, 4])

    def test_grid_mode_uses_grid_executor_without_evaluating_ema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(),
                live_strategy_mode="grid",
                live_price_history_file=str(Path(directory) / "prices.json"),
            )
            trader = _GridTrader()
            loop = LiveTradingLoop(
                config,
                bridge=_Bridge(),
                trader=trader,  # type: ignore[arg-type]
                prices=LivePriceStore(config.live_price_history_file),
            )

            loop.run_once()

            self.assertEqual(trader.grid_calls, ["XRP"])
            self.assertEqual(trader.ema_calls, 0)
            self.assertEqual(
                trader.executions, [GridSignal("XRP", 1, "10", "0.98", "1")]
            )
            state = json.loads(Path(config.live_price_history_file).read_text())
            self.assertEqual(state["strategy"]["mode"], "grid")
            self.assertTrue(state["strategy"]["liveExecution"])
            self.assertEqual(state["strategy"]["grid"]["XRP"]["filledLevels"], 1)

    def test_grid_to_ema_transition_discards_grid_history_before_ema_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            prices = LivePriceStore(str(path))
            prices.record_strategy_status("grid", {}, live_execution=True)
            for value in ("10", "9", "8", "7"):
                prices.record_price("XRP", Decimal(value), 10)
                prices.record_price("BTC", Decimal(value), 10)

            config = replace(
                settings(
                    strategy_fast_window=2,
                    strategy_slow_window=3,
                    strategy_rsi_window=2,
                    live_price_history_limit=10,
                    live_strategy_mode="ema",
                ),
                live_price_history_file=str(path),
            )
            trader = _Trader()
            loop = LiveTradingLoop(
                config,
                bridge=_Bridge(),
                trader=trader,
                prices=prices,
            )

            loop.run_once()

            self.assertEqual(trader.calls, [])
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["strategy"]["mode"], "ema")
            self.assertEqual(state["prices"], {"XRP": ["1"], "BTC": ["1"]})

    def test_grid_take_profit_is_checked_before_any_new_buy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = replace(
                settings(),
                live_strategy_mode="grid",
                live_price_history_file=str(Path(directory) / "prices.json"),
            )
            trader = _GridTakeProfitTrader()
            bridge = _TakeProfitBridge()
            loop = LiveTradingLoop(
                config,
                bridge=bridge,  # type: ignore[arg-type]
                trader=trader,  # type: ignore[arg-type]
                prices=LivePriceStore(config.live_price_history_file),
            )

            loop.run_once()

            self.assertEqual(trader.take_profit_calls, ["XRP"])
            self.assertEqual(trader.buy_decisions, 0)
            self.assertEqual(len(trader.executions), 1)
            self.assertEqual(bridge.amounts, [5 * 10**18])
            state = json.loads(Path(config.live_price_history_file).read_text())
            self.assertEqual(state["prices"], {"XRP": [], "BTC": []})
            progress = state["strategy"]["grid"]["XRP"]
            self.assertEqual(progress["activeExposureUsdt"], "0")
            self.assertEqual(progress["openLots"], [])
            self.assertEqual(progress["lastSellOutcome"]["outcome"], "confirmed")

    def test_poll_health_metadata_persists_without_trade_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            store = LivePriceStore(str(path))

            store.record_heartbeat(now=100)
            store.record_poll_success(now=101)
            store.record_poll_error("router unavailable", retry_delay_seconds=5, now=102)

            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["heartbeat_at"], 102)
            self.assertEqual(state["last_successful_poll_at"], 101)
            self.assertEqual(state["last_retry_at"], 102)
            self.assertEqual(state["last_retry_delay_seconds"], 5)
            self.assertEqual(state["retry_count"], 1)
            self.assertEqual(state["last_error_at"], 102)
            self.assertEqual(state["last_error"], "router unavailable")
            self.assertNotIn("wallet_address", state)
            self.assertNotIn("private_key", state)

    def test_confirmed_alert_outbox_is_idempotent_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            store = LivePriceStore(str(path))
            result = TradeResult("buy", "XRP", "50", "51", "0x" + "1" * 64, 123)

            store.queue_notification(result)
            store.queue_notification(result)

            self.assertEqual(store.pending_notifications(), [result])
            store.mark_notification_delivered(result.tx_hash)
            self.assertEqual(store.pending_notifications(), [])
            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(state["notifications"]["delivered"], [result.tx_hash])
            self.assertNotIn("wallet", json.dumps(state).lower())

    def test_runner_records_a_safe_retry_state_after_a_failed_poll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.json"
            config = replace(settings(), live_price_history_file=str(path))
            loop = LiveTradingLoop(
                config,
                bridge=_FailingBridge(),  # type: ignore[arg-type]
                trader=_Trader(),  # type: ignore[arg-type]
                prices=LivePriceStore(str(path)),
                stop_event=_StopAfterWait(),  # type: ignore[arg-type]
            )

            loop.run()

            state = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreater(state["heartbeat_at"], 0)
            self.assertEqual(state["retry_count"], 1)
            self.assertEqual(state["last_retry_delay_seconds"], 5)
            self.assertEqual(state["last_error"], "RuntimeError")
            self.assertNotIn("RPC token", json.dumps(state))


class LiveStatusApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            ["pnpm", "--filter", "@workspace/api-server", "run", "build"],
            cwd=PROJECT_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    def test_status_is_available_when_live_execution_is_disabled_or_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live.json"

            with _LiveApiServer(state_path, enabled=False) as server:
                status, body = server.request()
            self.assertEqual(status, 200)
            self.assertEqual(body["mode"], "live")
            self.assertFalse(body["liveExecution"])
            self.assertEqual(body["status"], "disabled")
            self.assertFalse(body["polling"])
            self.assertIsNone(body["heartbeatAt"])
            self.assertIsNone(body["lastSuccessfulPollAt"])

            with _LiveApiServer(state_path, enabled=True) as server:
                status, body = server.request()
            self.assertEqual(status, 200)
            self.assertTrue(body["liveExecution"])
            self.assertEqual(body["status"], "not_started")
            self.assertFalse(body["polling"])

    def test_status_reports_live_heartbeat_and_retry_without_journal_contents(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "base_asset": "USDT",
                        "prices": {"XRP": ["1"], "BTC": ["2"]},
                        "heartbeat_at": now - 1,
                        "last_successful_poll_at": now - 2,
                        "last_retry_at": now - 1,
                        "last_retry_delay_seconds": 5,
                        "retry_count": 2,
                        "last_error_at": now - 1,
                        "last_error": "SmartRouterError",
                        "strategy": {
                            "mode": "grid",
                            "liveExecution": True,
                            "grid": {
                                "XRP": {
                                    "anchorPrice": "1.5",
                                    "nextLevel": 2,
                                    "filledLevels": 1,
                                    "totalLevels": 5,
                                    "spentUsdt": "10",
                                    "remainingUsdt": "40",
                                    "state": "active",
                                },
                                "BTC": {
                                    "anchorPrice": None,
                                    "nextLevel": None,
                                    "filledLevels": 0,
                                    "totalLevels": 5,
                                    "spentUsdt": "0",
                                    "remainingUsdt": "50",
                                    "state": "not_started",
                                },
                            },
                        },
                    },
                ),
                encoding="utf-8",
            )

            with _LiveApiServer(state_path, enabled=False) as server:
                status, body = server.request()

            self.assertEqual(status, 200)
            self.assertTrue(body["liveExecution"])
            self.assertEqual(body["strategyMode"], "grid")
            self.assertEqual(body["grid"]["XRP"]["anchorPrice"], "1.5")
            self.assertEqual(body["grid"]["XRP"]["nextLevel"], 2)
            self.assertEqual(body["grid"]["XRP"]["remainingUsdt"], "40")
            self.assertEqual(body["grid"]["XRP"]["activeExposureUsdt"], "10")
            self.assertEqual(body["grid"]["XRP"]["realizedUsdt"], "0")
            self.assertEqual(body["grid"]["XRP"]["openLots"], [])
            self.assertIsNone(body["grid"]["XRP"]["lastSellOutcome"])
            self.assertEqual(body["status"], "retrying")
            self.assertTrue(body["polling"])
            self.assertIsNotNone(body["heartbeatAt"])
            self.assertIsNotNone(body["lastSuccessfulPollAt"])
            self.assertEqual(body["retryDelaySeconds"], 5)
            self.assertEqual(body["retryCount"], 2)
            self.assertIsNotNone(body["lastRetryAt"])
            self.assertIsNotNone(body["lastErrorAt"])
            self.assertEqual(body["lastError"], "SmartRouterError")
            self.assertNotIn("notifications", body)
            self.assertNotIn("prices", body)
            self.assertNotIn("0x" + "1" * 64, json.dumps(body))

    def test_operator_can_resolve_uncertain_alert_and_resolution_is_audited(self) -> None:
        tx_hash = "0x" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "base_asset": "USDT",
                        "prices": {"XRP": [], "BTC": []},
                        "notifications": {
                            "pending": [
                                {
                                    "action": "buy",
                                    "symbol": "XRP",
                                    "amount_in": "50",
                                    "amount_out": "51",
                                    "tx_hash": tx_hash,
                                    "block_number": 123,
                                    "confirmed_at": 100,
                                }
                            ],
                            "delivered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            delivery_path = Path(
                f"{state_path}.trade-notification-delivery.json"
            )
            delivery_path.write_text(
                json.dumps(
                    {
                        "deliveredTxHashes": [],
                        "inFlightTxHashes": [tx_hash],
                    }
                ),
                encoding="utf-8",
            )

            with _LiveApiServer(state_path, enabled=True) as server:
                status, body = server.request()
                self.assertEqual(status, 200)
                alert = body["confirmedAlertReconciliation"][0]
                self.assertEqual(alert["txHash"], tx_hash)
                self.assertEqual(alert["symbol"], "XRP")
                self.assertTrue(alert["resendAvailable"])
                self.assertNotIn("amountIn", alert)
                self.assertNotIn("amountOut", alert)
                self.assertNotIn("private", json.dumps(body).lower())

                with ThreadPoolExecutor(max_workers=2) as executor:
                    resolutions = list(
                        executor.map(
                            lambda _index: server.resolve(tx_hash, "delivered"),
                            range(2),
                        )
                    )
                self.assertEqual(
                    sorted(status for status, _body in resolutions), [200, 409]
                )
                successful = next(
                    response for status, response in resolutions if status == 200
                )
                self.assertEqual(successful["outcome"], "delivered")
                self.assertEqual(
                    server.request()[1]["confirmedAlertReconciliation"], []
                )

            delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
            self.assertEqual(delivery["inFlightTxHashes"], [])
            self.assertEqual(delivery["deliveredTxHashes"], [tx_hash])
            audit = {"events": delivery["auditEvents"]}
            self.assertEqual(
                [event["resolution"] for event in audit["events"]],
                ["delivered", "delivered"],
            )
            self.assertEqual(
                [event["outcome"] for event in audit["events"]],
                ["requested", "delivered"],
            )

    def test_controlled_resend_can_only_be_requested_once(self) -> None:
        tx_hash = "0x" + "b" * 64
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "live.json"
            state_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "base_asset": "USDT",
                        "prices": {"XRP": [], "BTC": []},
                        "notifications": {
                            "pending": [
                                {
                                    "action": "sell",
                                    "symbol": "BTC",
                                    "amount_in": "1",
                                    "amount_out": "2",
                                    "tx_hash": tx_hash,
                                    "block_number": 456,
                                }
                            ],
                            "delivered": [],
                        },
                    }
                ),
                encoding="utf-8",
            )
            Path(f"{state_path}.trade-notification-delivery.json").write_text(
                json.dumps(
                    {
                        "deliveredTxHashes": [],
                        "inFlightTxHashes": [tx_hash],
                    }
                ),
                encoding="utf-8",
            )

            with _LiveApiServer(state_path, enabled=True) as server:
                first_status, first = server.resolve(tx_hash, "resend")
                self.assertEqual(first_status, 502)
                self.assertEqual(first["outcome"], "rejected")
                second_status, _second = server.resolve(tx_hash, "resend")
                self.assertEqual(second_status, 409)

            audit = json.loads(
                Path(f"{state_path}.trade-notification-delivery.json").read_text()
            )
            self.assertEqual(
                [event["outcome"] for event in audit["auditEvents"]],
                ["requested", "rejected"],
            )


if __name__ == "__main__":
    unittest.main()