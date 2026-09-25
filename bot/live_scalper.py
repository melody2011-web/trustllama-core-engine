"""Isolated, live-gated WebSocket scalper primitives.

The quote feed is only a signal source.  ``PancakeSwapTrader.execute`` always
builds a new Smart Router trade immediately before a signed transaction.
"""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event
from typing import Any, Callable, Iterator, Protocol

from .config import ConfigurationError, Settings
from .trader import PancakeSwapTrader, TradeResult

CONFIG_VERSION = 1
MAX_CONFIG_AGE_SECONDS = 30 * 24 * 60 * 60
PRICE_HISTORY_LIMIT = 12
ENTRY_MOVE_BPS = Decimal("10")


class LiveScalperError(RuntimeError):
    pass


@dataclass(frozen=True)
class StreamQuote:
    symbol: str
    price: Decimal
    event_at: float


@dataclass(frozen=True)
class ScalperPaths:
    bot: str
    root: Path
    config: Path
    state: Path
    trader_state: Path
    prices: Path
    notifications: Path
    errors: Path
    log: Path

    @classmethod
    def for_bot(cls, bot: str) -> "ScalperPaths":
        root = Path.home() / ".local" / "state" / "bnb-defi-bot" / bot
        return cls(bot, root, root / "config.json", root / "state.json", root / "trader-state.json",
                   root / "prices.json", root / "notifications.json",
                   root / "errors.json", root / f"{bot}.log")


class ScalperAmountConfig:
    """Secure-panel-owned atomic operator amount. No source cap is imposed."""
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _amount(value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise LiveScalperError("Scalper USDT amount is invalid.") from error
        if not amount.is_finite() or amount <= 0:
            raise LiveScalperError("Scalper USDT amount must be finite and positive.")
        return amount

    def save(self, amount: object, *, now: float | None = None) -> Decimal:
        result = self._amount(amount)
        timestamp = time.time() if now is None else now
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
            raise LiveScalperError("Scalper configuration timestamp is invalid.")
        with self._locked():
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump({"version": CONFIG_VERSION, "amount_usdt": format(result, "f"),
                           "updated_at": timestamp}, handle, sort_keys=True)
                handle.write("\n"); handle.flush(); os.fsync(handle.fileno())
            os.chmod(temporary, 0o600); os.replace(temporary, self.path); os.chmod(self.path, 0o600)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return result

    def read(self, *, now: float | None = None) -> Decimal:
        timestamp = time.time() if now is None else now
        with self._locked():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise LiveScalperError("Scalper configuration is missing or unreadable; entries are blocked.") from error
        if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION:
            raise LiveScalperError("Scalper configuration version is invalid; entries are blocked.")
        updated = raw.get("updated_at")
        if (not isinstance(updated, (int, float)) or not math.isfinite(updated)
                or updated <= 0 or updated > timestamp + 60
                or timestamp - updated > MAX_CONFIG_AGE_SECONDS):
            raise LiveScalperError("Scalper configuration is stale; entries are blocked.")
        return self._amount(raw.get("amount_usdt"))


def parse_quote_json(payload: str | bytes, expected_symbol: str, *, received_at: float | None = None) -> StreamQuote:
    """Parse only Binance mini ticker style JSON; reject permissive coercions."""
    try:
        raw = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise LiveScalperError("WebSocket payload is not valid JSON.") from error
    if not isinstance(raw, dict):
        raise LiveScalperError("WebSocket payload must be an object.")
    raw = raw.get("data", raw)
    if not isinstance(raw, dict) or raw.get("s") != expected_symbol:
        raise LiveScalperError("WebSocket payload has an unexpected symbol.")
    if not isinstance(raw.get("p"), str) or isinstance(raw.get("E"), bool):
        raise LiveScalperError("WebSocket payload has invalid price or event time.")
    try:
        price, event_ms = Decimal(raw["p"]), float(raw["E"])
    except (InvalidOperation, TypeError, ValueError) as error:
        raise LiveScalperError("WebSocket payload has invalid price or event time.") from error
    if not price.is_finite() or price <= 0 or not math.isfinite(event_ms) or event_ms <= 0:
        raise LiveScalperError("WebSocket payload has non-positive price or invalid event time.")
    event_at = event_ms / 1000
    if received_at is not None and event_at > received_at + 60:
        raise LiveScalperError("WebSocket payload event time is implausibly in the future.")
    return StreamQuote(expected_symbol, price, event_at)


class WebSocketConnection(Protocol):
    def recv(self) -> str | bytes: ...
    def ping(self) -> Any: ...
    def close(self) -> Any: ...


class LiveScalper:
    """One-symbol signal loop with reconnect/backoff and durable local isolation."""
    def __init__(self, settings: Settings, *, paths: ScalperPaths, market_symbol: str,
                 trade_symbol: str, connection_factory: Callable[[], WebSocketConnection] | None = None,
                 trader: PancakeSwapTrader | None = None, clock: Callable[[], float] = time.time,
                 stop_event: Event | None = None):
        if not settings.enable_live_trading:
            raise ConfigurationError("Live scalper requires ENABLE_LIVE_TRADING=true; refusing to start.")
        self.settings, self.paths, self.market_symbol, self.trade_symbol = settings, paths, market_symbol, trade_symbol
        self.amounts, self.clock, self.stop_event = ScalperAmountConfig(paths.config), clock, stop_event or Event()
        self.trader = trader or PancakeSwapTrader(settings)
        self.connection_factory = connection_factory or self._default_connection
        self.last_event_at = 0.0
        self.retry_delay = 1.0
        self.position, self.intent, self.last_confirmation = self._load_state()
        self.recent_prices, persisted_event_at = self._load_prices()
        self.last_event_at = persisted_event_at

    def _load_state(
        self,
    ) -> tuple[
        tuple[Decimal, Decimal] | None,
        dict[str, str] | None,
        dict[str, str] | None,
    ]:
        if not self.paths.state.exists():
            return None, None, None
        try:
            raw = json.loads(self.paths.state.read_text(encoding="utf-8"))
            if raw.get("symbol") != self.trade_symbol:
                raise ValueError
            position = (
                None
                if raw.get("position") is None
                else (self._positive(raw["amount"]), self._positive(raw["entry_price"]))
            )
            intent = raw.get("intent")
            if intent is not None and (
                not isinstance(intent, dict)
                or intent.get("action") not in {"buy", "sell"}
                or not isinstance(intent.get("signal_price"), str)
            ):
                raise ValueError
            if intent is not None:
                self._positive(intent["signal_price"])
            confirmation = raw.get("last_confirmation")
            if confirmation is not None and (
                not isinstance(confirmation, dict)
                or confirmation.get("action") not in {"buy", "sell"}
                or confirmation.get("symbol") != self.trade_symbol
                or not isinstance(confirmation.get("tx_hash"), str)
                or not confirmation.get("tx_hash")
                or not isinstance(confirmation.get("amount_out"), str)
            ):
                raise ValueError
            if confirmation is not None:
                self._positive(confirmation["amount_out"])
            return position, intent, confirmation
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, LiveScalperError) as error:
            raise LiveScalperError("Scalper state is unreadable; refusing to trade.") from error

    @staticmethod
    def _positive(value: object) -> Decimal:
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise LiveScalperError("Scalper state contains an invalid amount.") from error
        if not number.is_finite() or number <= 0:
            raise LiveScalperError("Scalper state contains an invalid amount.")
        return number

    def _save_state(self) -> None:
        self.paths.state.parent.mkdir(parents=True, exist_ok=True)
        raw: dict[str, object] = {
            "symbol": self.trade_symbol,
            "position": None,
            "intent": self.intent,
            "last_confirmation": self.last_confirmation,
        }
        if self.position is not None:
            raw.update({"position": "open", "amount": str(self.position[0]), "entry_price": str(self.position[1])})
        temporary = self.paths.state.with_name(f".{self.paths.state.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(raw, output, sort_keys=True); output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, self.paths.state); os.chmod(self.paths.state, 0o600)
        directory_fd = os.open(self.paths.state.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _begin_intent(self, action: str, signal_price: Decimal) -> None:
        if self.intent is not None:
            raise LiveScalperError("A scalper order intent is already pending.")
        self.intent = {"action": action, "signal_price": str(signal_price)}
        self._save_state()

    def _clear_intent(self) -> None:
        self.intent = None
        self._save_state()

    def _finalize_result(self, result: TradeResult) -> None:
        intent = self.intent
        confirmation = {
            "action": result.action,
            "symbol": result.symbol,
            "tx_hash": result.tx_hash,
            "amount_out": result.amount_out,
        }
        if intent is None and self.last_confirmation == confirmation:
            return
        if (
            intent is None
            or intent.get("action") != result.action
            or result.symbol != self.trade_symbol
        ):
            raise LiveScalperError(
                "Confirmed trade does not match the durable scalper intent."
            )
        if result.action == "buy":
            self.position = (
                self._positive(result.amount_out),
                self._positive(intent["signal_price"]),
            )
        else:
            if self.position is None:
                raise LiveScalperError(
                    "Confirmed scalper exit has no durable open position."
                )
            self.position = None
        self.intent = None
        self.last_confirmation = confirmation
        self._save_state()

    def _notify_result(self, result: TradeResult) -> None:
        try:
            self._record(
                self.paths.notifications,
                {"tx": result.tx_hash, "action": result.action},
            )
        except OSError:
            # Confirmed position state is authoritative. Notification logging
            # must never make transaction finalization appear to have failed.
            pass

    def _load_prices(self) -> tuple[list[Decimal], float]:
        if not self.paths.prices.exists():
            return [], 0.0
        try:
            raw = json.loads(self.paths.prices.read_text(encoding="utf-8"))
            if raw.get("symbol") != self.market_symbol:
                raise ValueError
            values = raw["prices"]
            if not isinstance(values, list):
                raise ValueError
            event_at = raw["last_event_at"]
            if not isinstance(event_at, (int, float)) or isinstance(event_at, bool) or not math.isfinite(event_at) or event_at <= 0:
                raise ValueError
            return [self._positive(value) for value in values][-PRICE_HISTORY_LIMIT:], float(event_at)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, LiveScalperError) as error:
            raise LiveScalperError("Scalper price state is unreadable; refusing to trade.") from error

    def _save_prices(self, quote: StreamQuote) -> None:
        self.recent_prices = (self.recent_prices + [quote.price])[-PRICE_HISTORY_LIMIT:]
        self.paths.prices.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.prices.with_name(f".{self.paths.prices.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump({"symbol": self.market_symbol, "last_event_at": quote.event_at,
                       "prices": [str(value) for value in self.recent_prices]}, output, sort_keys=True)
            output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.chmod(temporary, 0o600); os.replace(temporary, self.paths.prices); os.chmod(self.paths.prices, 0o600)
        directory_fd = os.open(self.paths.prices.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _entry_signal(self) -> bool:
        """Require a bounded three-price reversal and a non-trivial move."""
        if len(self.recent_prices) < 3:
            return False
        before, trough, latest = self.recent_prices[-3:]
        movement = (latest - trough) * Decimal(10_000) / trough
        return trough < before and latest > trough and movement >= ENTRY_MOVE_BPS

    def _default_connection(self) -> WebSocketConnection:
        try:
            from websockets.sync.client import connect
        except ImportError as error:
            raise LiveScalperError("websockets is required for live quote streaming.") from error
        return connect(f"wss://stream.binance.com:9443/ws/{self.market_symbol.lower()}@trade",
                       open_timeout=30, ping_interval=10, ping_timeout=10, close_timeout=5)

    def _record(self, path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as output:
            output.write(json.dumps(value, sort_keys=True) + "\n")
        os.chmod(path, 0o600)

    def process_payload(self, payload: str | bytes) -> TradeResult | None:
        now = self.clock()
        quote = parse_quote_json(payload, self.market_symbol, received_at=now)
        if quote.event_at <= self.last_event_at or now - quote.event_at > 20:
            raise LiveScalperError("WebSocket quote is stale or out of order.")
        self.last_event_at = quote.event_at
        self._save_prices(quote)
        pending = self.trader.inspect_pending()
        if pending is not None:
            reconcile = getattr(self.trader, "reconcile_manual", None)
            if not callable(reconcile):
                raise LiveScalperError("Pending trade cannot be reconciled; new actions are blocked.")
            reconciled = reconcile(confirmation_callback=self._finalize_result)
            if reconciled is not None:
                self._notify_result(reconciled)
                return reconciled
            if self.trader.inspect_pending() is not None:
                return None
            if self.intent is not None:
                self._clear_intent()
                return None
        elif self.intent is not None:
            # No trader claim means shutdown happened before a signed hash was
            # persisted, so this local-only intent is safe to release.
            self._clear_intent()
            return None
        if self.position is not None:
            amount, entry = self.position
            if quote.price < entry * Decimal("0.99") or quote.price > entry * Decimal("1.01"):
                self._begin_intent("sell", quote.price)
                result = self.trader.execute(
                    "sell",
                    self.trade_symbol,
                    format(amount, "f"),
                    confirmation_callback=self._finalize_result,
                )
                self._notify_result(result)
                return result
            return None
        try:
            amount = self.amounts.read(now=now)
        except LiveScalperError:
            return None
        if not self._entry_signal():
            return None
        self._begin_intent("buy", quote.price)
        result = self.trader.execute(
            "buy",
            self.trade_symbol,
            format(amount, "f"),
            confirmation_callback=self._finalize_result,
        )
        self._notify_result(result)
        return result

    def run(self) -> None:
        while not self.stop_event.is_set():
            connection = None
            try:
                connection = self.connection_factory()
                self.retry_delay = 1.0
                while not self.stop_event.is_set():
                    self.process_payload(connection.recv())
                    connection.ping()
            except Exception as error:
                self._record(self.paths.errors, {"at": self.clock(), "error": type(error).__name__})
                self.stop_event.wait(self.retry_delay)
                self.retry_delay = min(60.0, self.retry_delay * 2)
            finally:
                if connection is not None:
                    connection.close()