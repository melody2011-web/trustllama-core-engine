from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from unittest.mock import patch

from bot.config import Settings
from bot.paper_loop import PaperGuardError, PaperStateStore, PaperTradingLoop
from bot.smart_router import SmartRouterError, SmartRouterQuote
from bot.trader import StrategySignal
from tests.paper_test_safety import paper_test_safety_guard

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PAPER_NODE_NETWORK_GUARD = PROJECT_ROOT / "tests" / "paper_network_guard.cjs"

_paper_safety_guard = None


def setUpModule() -> None:
    global _paper_safety_guard
    _paper_safety_guard = paper_test_safety_guard()
    _paper_safety_guard.__enter__()


def tearDownModule() -> None:
    if _paper_safety_guard is not None:
        _paper_safety_guard.__exit__(None, None, None)


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _PaperApiServer:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.port = _free_port()
        self.process: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/api/paper/status"

    def __enter__(self) -> "_PaperApiServer":
        environment = os.environ.copy()
        environment.update(
            {
                "PORT": str(self.port),
                "PAPER_STATE_FILE": str(self.state_path),
                "STRATEGY_SLOW_WINDOW": "2",
                "STRATEGY_RSI_WINDOW": "2",
                "PAPER_POLL_INTERVAL_SECONDS": "60",
            }
        )
        self.process = subprocess.Popen(
            [
                "node",
                "--require",
                str(PAPER_NODE_NETWORK_GUARD),
                "--enable-source-maps",
                "artifacts/api-server/dist/index.mjs",
            ],
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
        raise RuntimeError("Paper API server did not start.")

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


def settings(**overrides: object) -> Settings:
    base = Settings(
        bsc_rpc_url="https://public.example",
        wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="not-used",
        enable_live_trading=True,
        slippage_bps=100,
        deadline_seconds=120,
        receipt_timeout_seconds=180,
        max_gas_limit=700_000,
        strategy_fast_window=2,
        strategy_slow_window=3,
        strategy_rsi_window=2,
        strategy_buy_rsi_max=100,
        strategy_cooldown_seconds=900,
        paper_poll_interval_seconds=60,
        paper_retry_base_seconds=5,
        paper_retry_max_seconds=20,
        paper_price_history_limit=20,
        paper_starting_bnb=Decimal("1"),
        paper_starting_xrp=Decimal("0"),
        paper_starting_btc=Decimal("0"),
    )
    return replace(base, **overrides)


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def quote(self, input_symbol: str, output_symbol: str, amount_in: int) -> SmartRouterQuote:
        self.calls.append((input_symbol, output_symbol, amount_in))
        values = {
            ("XRP", "WBNB"): 9 * 10**18,
            ("BTC", "WBNB"): 10**18,
            ("WBNB", "XRP"): 12 * 10**18,
            ("WBNB", "BTC"): 10**14,
        }
        return SmartRouterQuote(
            amount_out=values[(input_symbol, output_symbol)],
            route_count=1,
            v2_candidates=2,
            v3_candidates=4,
            stable_candidates=0,
        )


class _StopAfterWait:
    def __init__(self) -> None:
        self.stopped = False
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return self.stopped

    def set(self) -> None:
        self.stopped = True

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.stopped = True
        return True


class PaperStateStoreTests(unittest.TestCase):
    def test_fill_persists_balances_position_and_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = PaperStateStore(
                str(Path(directory) / "paper.json"),
                starting_bnb=Decimal("1"),
                starting_xrp=Decimal("0"),
                starting_btc=Decimal("0"),
            )
            signal = StrategySignal("buy", "XRP", "0.1", "9", "test")
            fill = state.apply_fill(
                signal,
                Decimal("12"),
                now=10_000,
                cooldown_seconds=900,
                max_trades_per_day=3,
            )
            snapshot = state.snapshot()

            self.assertEqual(fill.amount_out, Decimal("12"))
            self.assertEqual(snapshot.balances["BNB"], Decimal("0.9"))
            self.assertEqual(snapshot.balances["XRP"], Decimal("12"))
            self.assertEqual(snapshot.positions["XRP"].amount, "12")
            with self.assertRaises(PaperGuardError):
                state.apply_fill(
                    StrategySignal("sell", "XRP", "12", "8", "test"),
                    Decimal("0.08"),
                    now=10_001,
                    cooldown_seconds=900,
                    max_trades_per_day=3,
                )
            state.apply_fill(
                StrategySignal("buy", "BTC", "0.1", "100", "next day"),
                Decimal("0.001"),
                now=10_000 + 86_401,
                cooldown_seconds=0,
                max_trades_per_day=1,
            )
            self.assertEqual(len(state.snapshot().trades), 2)

            restarted = PaperStateStore(
                str(Path(directory) / "paper.json"),
                starting_bnb=Decimal("99"),
                starting_xrp=Decimal("99"),
                starting_btc=Decimal("99"),
            )
            self.assertEqual(restarted.snapshot().balances["BNB"], Decimal("0.8"))
            self.assertEqual(len(restarted.snapshot().trades), 2)

    def test_quote_and_loop_health_metadata_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = PaperStateStore(
                str(Path(directory) / "paper.json"),
                starting_bnb=Decimal("1"),
                starting_xrp=Decimal("0"),
                starting_btc=Decimal("0"),
            )

            state.record_price("XRP", Decimal("10"), 20, now=100)
            state.record_price("BTC", Decimal("20"), 20, now=101)
            state.record_loop_success(now=102)
            snapshot = state.snapshot()

            self.assertEqual(snapshot.last_successful_quote_at, 101)
            self.assertEqual(
                snapshot.last_successful_quote_at_by_symbol,
                {"XRP": 100, "BTC": 101},
            )
            self.assertEqual(snapshot.last_loop_success_at, 102)
            self.assertIsNone(snapshot.last_loop_error)

            state.record_loop_error("RPC down", now=103)
            errored = state.snapshot()
            self.assertEqual(errored.last_loop_error_at, 103)
            self.assertEqual(errored.last_loop_error, "RPC down")


class PaperStatusApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            ["pnpm", "--filter", "@workspace/api-server", "run", "build"],
            cwd=PROJECT_ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )

    @staticmethod
    def _journal(now: int, marker: str = "primary") -> dict[str, object]:
        if marker == "alternate":
            bnb_balance, xrp_balance, position_amount = "2.20", "4", "4"
        else:
            bnb_balance, xrp_balance, position_amount = "1.10", "2", "2"
        return {
            "version": 1,
            "balances": {"BNB": bnb_balance, "XRP": xrp_balance, "BTC": "0.005"},
            "positions": {"XRP": {"amount": position_amount, "entry_price": "0.08"}},
            "prices": {
                "XRP": ["0.08", "0.081", "0.082"],
                "BTC": ["60000", "60100"],
            },
            "last_trade_at": now - 20,
            "trades": [
                {
                    "at": now - 10,
                    "kind": "paper",
                    "action": "buy",
                    "symbol": "XRP",
                    "amountIn": "0.10",
                    "amountOut": position_amount,
                    "price": "0.08",
                    "reason": "warm-up test",
                    "outcome": "filled",
                }
            ],
            "last_successful_quote_at": now - 1,
            "last_successful_quote_at_by_symbol": {
                "XRP": now - 2,
                "BTC": now - 1,
            },
            "last_loop_success_at": now,
            "last_loop_error_at": 0,
            "last_loop_error": None,
        }

    @staticmethod
    def _write_journal(path: Path, state: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.test.tmp")
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file)
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary, path)

    def test_status_serializes_account_and_health_snapshot(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "paper.json"
            self._write_journal(state_path, self._journal(now))
            os.utime(state_path, (now - 5, now - 5))

            with _PaperApiServer(state_path) as server:
                status, body = server.request()

            self.assertEqual(status, 200)
            self.assertEqual(body["mode"], "paper")
            self.assertFalse(body["liveExecution"])
            self.assertEqual(body["status"], "warming_up")
            self.assertEqual(
                body["balances"],
                {"BNB": "1.10", "XRP": "2", "BTC": "0.005"},
            )
            self.assertEqual(
                body["openPositions"],
                [{"symbol": "XRP", "amount": "2", "entryPrice": "0.08"}],
            )
            self.assertEqual(
                body["recentFills"],
                [
                    {
                        "at": datetime.fromtimestamp(now - 10, timezone.utc)
                        .isoformat(timespec="milliseconds")
                        .replace("+00:00", "Z"),
                        "action": "buy",
                        "symbol": "XRP",
                        "amountIn": "0.10",
                        "amountOut": "2",
                        "price": "0.08",
                        "reason": "warm-up test",
                        "outcome": "filled",
                    }
                ],
            )
            self.assertEqual(
                body["warmup"],
                {
                    "XRP": {"samples": 3, "required": 3, "ready": True},
                    "BTC": {"samples": 2, "required": 3, "ready": False},
                },
            )
            self.assertEqual(
                body["lastSuccessfulQuoteAt"],
                datetime.fromtimestamp(now - 1, timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            )
            self.assertEqual(
                body["lastSuccessfulQuoteAtBySymbol"],
                {
                    "XRP": datetime.fromtimestamp(now - 2, timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                    "BTC": datetime.fromtimestamp(now - 1, timezone.utc)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                },
            )
            self.assertEqual(
                body["lastLoopSuccessAt"],
                datetime.fromtimestamp(now, timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
            )
            self.assertIsNone(body["lastLoopErrorAt"])
            self.assertIsNone(body["lastLoopError"])
            self.assertIsNotNone(body["stateUpdatedAt"])

    def test_replacement_and_malformed_journal_never_return_partial_success(self) -> None:
        now = int(time.time())
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "paper.json"
            primary = self._journal(now)
            alternate = self._journal(now, marker="alternate")
            self._write_journal(state_path, primary)

            with _PaperApiServer(state_path) as server:
                responses: list[tuple[int, dict[str, object]]] = []

                def replace_repeatedly() -> None:
                    for index in range(40):
                        self._write_journal(
                            state_path,
                            alternate if index % 2 else primary,
                        )

                writer = threading.Thread(target=replace_repeatedly)
                writer.start()
                for _ in range(40):
                    responses.append(server.request())
                writer.join()

                valid_markers = {
                    ("1.10", "2", "2"),
                    ("2.20", "4", "4"),
                }
                for status, body in responses:
                    self.assertEqual(status, 200)
                    self.assertIn(
                        (
                            body["balances"]["BNB"],
                            body["balances"]["XRP"],
                            body["openPositions"][0]["amount"],
                        ),
                        valid_markers,
                    )

                state_path.write_text('{"balances":', encoding="utf-8")
                status, body = server.request()
                self.assertEqual(status, 503)
                self.assertFalse(body["ok"])
                self.assertIn("unreadable", body["error"])


class PaperTradingLoopTests(unittest.TestCase):
    def test_live_enabled_setting_still_produces_only_a_paper_fill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = PaperStateStore(
                str(Path(directory) / "paper.json"),
                starting_bnb=Decimal("1"),
                starting_xrp=Decimal("0"),
                starting_btc=Decimal("0"),
            )
            for price in (Decimal("10"), Decimal("9"), Decimal("8"), Decimal("7"), Decimal("8")):
                state.record_price("XRP", price, 20)
            bridge = _Bridge()
            loop = PaperTradingLoop(
                settings(paper_state_file=str(Path(directory) / "paper.json")),
                bridge=bridge,  # type: ignore[arg-type]
                state=state,
            )

            loop.run_once(now=10_000)
            snapshot = state.snapshot()

            self.assertEqual(snapshot.balances["BNB"], Decimal("0.8"))
            self.assertEqual(snapshot.balances["XRP"], Decimal("12"))
            self.assertIn("XRP", snapshot.positions)
            self.assertEqual(snapshot.trades[-1]["kind"], "paper")
            self.assertIn(("WBNB", "XRP", 2 * 10**17), bridge.calls)

    def test_transient_quote_failure_uses_bounded_retry_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loop = PaperTradingLoop(
                settings(paper_state_file=str(Path(directory) / "paper.json")),
                bridge=_Bridge(),  # type: ignore[arg-type]
            )
            loop.run_once = lambda: (_ for _ in ()).throw(SmartRouterError("RPC down"))  # type: ignore[method-assign]
            stopper = _StopAfterWait()
            loop.stop_event = stopper  # type: ignore[assignment]

            loop.run()

            self.assertEqual(stopper.waits, [5])
            self.assertEqual(loop.state.snapshot().last_loop_error, "SmartRouterError")


class PaperConfigurationTests(unittest.TestCase):
    def test_paper_guard_removes_trading_credentials(self) -> None:
        for name in ("BSC_RPC_URL", "WALLET_ADDRESS", "WALLET_PRIVATE_KEY"):
            self.assertNotIn(name, os.environ)

    def test_paper_guard_rejects_external_network_access(self) -> None:
        with self.assertRaisesRegex(
            AssertionError,
            r"Paper safety guard blocked network access.*mock the RPC",
        ):
            with socket.socket() as connection:
                connection.connect(("203.0.113.1", 8545))

    def test_paper_mode_does_not_require_or_load_a_private_key(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BSC_RPC_URL": "https://public.example",
                "PAPER_PRICE_HISTORY_LIMIT": "100",
            },
            clear=True,
        ):
            loaded = Settings.from_environment(require_wallet=False)

        self.assertEqual(loaded.wallet_private_key, "")
        self.assertEqual(
            loaded.wallet_address, "0x0000000000000000000000000000000000000000"
        )


if __name__ == "__main__":
    unittest.main()
