from __future__ import annotations

import json
import logging
import math
import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Event
from typing import Any

from .config import ConfigurationError, Settings
from .contracts import TOKEN_DECIMALS
from .smart_router import SmartRouterBridge, SmartRouterError
from .trader import StrategyPosition, StrategyRules, StrategySignal, TraderError

logger = logging.getLogger("defi-bot.paper")

PAPER_STATE_VERSION = 1
PAPER_SYMBOLS = ("XRP", "BTC")


class PaperLoopError(RuntimeError):
    """Raised when a paper loop iteration cannot safely complete."""


class PaperGuardError(PaperLoopError):
    """Raised when a paper signal is blocked by a strategy safety limit."""


@dataclass(frozen=True)
class PaperSnapshot:
    balances: dict[str, Decimal]
    positions: dict[str, StrategyPosition]
    prices: dict[str, tuple[Decimal, ...]]
    last_trade_at: float
    trades: tuple[dict[str, Any], ...]
    last_successful_quote_at: float
    last_successful_quote_at_by_symbol: dict[str, float]
    last_loop_success_at: float
    last_loop_error_at: float
    last_loop_error: str | None


@dataclass(frozen=True)
class PaperFill:
    action: str
    symbol: str
    amount_in: Decimal
    amount_out: Decimal
    price: Decimal
    reason: str
    at: float


class PaperStateStore:
    """A locked, atomic journal for paper balances, samples, and fills."""

    def __init__(
        self,
        path: str,
        *,
        starting_bnb: Decimal,
        starting_xrp: Decimal,
        starting_btc: Decimal,
    ):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        self.starting_balances = {
            "BNB": starting_bnb,
            "XRP": starting_xrp,
            "BTC": starting_btc,
        }

    @contextmanager
    def _locked(self):
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _default(self) -> dict[str, Any]:
        return {
            "version": PAPER_STATE_VERSION,
            "balances": {key: str(value) for key, value in self.starting_balances.items()},
            "positions": {},
            "prices": {symbol: [] for symbol in PAPER_SYMBOLS},
            "last_trade_at": 0.0,
            "trades": [],
            "last_successful_quote_at": 0.0,
            "last_successful_quote_at_by_symbol": {symbol: 0.0 for symbol in PAPER_SYMBOLS},
            "last_loop_success_at": 0.0,
            "last_loop_error_at": 0.0,
            "last_loop_error": None,
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            with self.path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise PaperLoopError(
                f"Paper state file {self.path} is unreadable; refusing to continue."
            ) from error
        if state.get("version") != PAPER_STATE_VERSION:
            raise PaperLoopError("Paper state file version is unsupported; refusing to continue.")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise PaperLoopError(f"Paper state contains an invalid {label}.") from error
        if not result.is_finite() or result < 0:
            raise PaperLoopError(f"Paper state contains an invalid {label}.")
        return result

    def _snapshot(self, state: dict[str, Any]) -> PaperSnapshot:
        balances = {
            symbol: self._decimal(state.get("balances", {}).get(symbol, 0), f"{symbol} balance")
            for symbol in ("BNB", "XRP", "BTC")
        }
        positions: dict[str, StrategyPosition] = {}
        for symbol, raw in state.get("positions", {}).items():
            if symbol not in PAPER_SYMBOLS or not isinstance(raw, dict):
                raise PaperLoopError("Paper state contains an invalid position.")
            positions[symbol] = StrategyPosition(
                amount=str(self._decimal(raw.get("amount"), f"{symbol} position")),
                entry_price=str(self._decimal(raw.get("entry_price"), f"{symbol} entry price")),
            )
        prices: dict[str, tuple[Decimal, ...]] = {}
        for symbol in PAPER_SYMBOLS:
            prices[symbol] = tuple(
                self._decimal(value, f"{symbol} price") for value in state.get("prices", {}).get(symbol, [])
            )
        raw_quote_times = state.get("last_successful_quote_at_by_symbol", {})
        if not isinstance(raw_quote_times, dict):
            raise PaperLoopError("Paper state contains invalid quote health metadata.")
        quote_times = {
            symbol: float(raw_quote_times.get(symbol, 0.0)) for symbol in PAPER_SYMBOLS
        }
        last_loop_error = state.get("last_loop_error")
        if last_loop_error is not None and not isinstance(last_loop_error, str):
            raise PaperLoopError("Paper state contains invalid loop error metadata.")
        return PaperSnapshot(
            balances=balances,
            positions=positions,
            prices=prices,
            last_trade_at=float(state.get("last_trade_at", 0.0)),
            trades=tuple(state.get("trades", [])),
            last_successful_quote_at=float(state.get("last_successful_quote_at", 0.0)),
            last_successful_quote_at_by_symbol=quote_times,
            last_loop_success_at=float(state.get("last_loop_success_at", 0.0)),
            last_loop_error_at=float(state.get("last_loop_error_at", 0.0)),
            last_loop_error=last_loop_error,
        )

    def snapshot(self) -> PaperSnapshot:
        with self._locked():
            state = self._read()
            if not self.path.exists():
                self._write(state)
            return self._snapshot(state)

    def record_price(
        self,
        symbol: str,
        price: Decimal,
        limit: int,
        *,
        now: float | None = None,
    ) -> PaperSnapshot:
        symbol = symbol.upper()
        if symbol not in PAPER_SYMBOLS:
            raise PaperLoopError(f"Paper mode does not track {symbol}.")
        if not price.is_finite() or price <= 0:
            raise PaperLoopError(f"Received an invalid {symbol} price.")
        timestamp = time.time() if now is None else now
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
            raise PaperLoopError("Paper quote timestamp is invalid.")
        with self._locked():
            state = self._read()
            prices = state.setdefault("prices", {tracked: [] for tracked in PAPER_SYMBOLS})
            prices.setdefault(symbol, []).append(str(price))
            prices[symbol] = prices[symbol][-limit:]
            state["last_successful_quote_at"] = timestamp
            quote_times = state.setdefault(
                "last_successful_quote_at_by_symbol",
                {tracked: 0.0 for tracked in PAPER_SYMBOLS},
            )
            quote_times[symbol] = timestamp
            self._write(state)
            return self._snapshot(state)

    def record_loop_success(self, *, now: float | None = None) -> PaperSnapshot:
        timestamp = time.time() if now is None else now
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
            raise PaperLoopError("Paper loop timestamp is invalid.")
        with self._locked():
            state = self._read()
            state["last_loop_success_at"] = timestamp
            state["last_loop_error"] = None
            state["last_loop_error_at"] = 0.0
            self._write(state)
            return self._snapshot(state)

    def record_loop_error(self, error: str, *, now: float | None = None) -> PaperSnapshot:
        timestamp = time.time() if now is None else now
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp < 0:
            raise PaperLoopError("Paper loop timestamp is invalid.")
        with self._locked():
            state = self._read()
            state["last_loop_error_at"] = timestamp
            state["last_loop_error"] = error
            self._write(state)
            return self._snapshot(state)

    def apply_fill(
        self,
        signal: StrategySignal,
        amount_out: Decimal,
        *,
        now: float,
        cooldown_seconds: int,
        max_trades_per_day: int,
    ) -> PaperFill:
        if not amount_out.is_finite() or amount_out <= 0:
            raise PaperLoopError("Paper quote returned a non-positive fill amount.")
        with self._locked():
            state = self._read()
            positions = state.setdefault("positions", {})
            balances = state.setdefault("balances", {})
            last_trade_at = float(state.get("last_trade_at", 0.0))
            if now < last_trade_at or now - last_trade_at < cooldown_seconds:
                raise PaperGuardError("paper fill blocked: strategy cooldown is active")
            recent = [
                trade for trade in state.get("trades", []) if now - float(trade["at"]) < 86_400
            ]
            if len(recent) >= max_trades_per_day:
                raise PaperGuardError("paper fill blocked: daily trade limit is reached")
            if signal.action == "buy":
                if signal.symbol in positions:
                    raise PaperGuardError(
                        f"paper fill blocked: {signal.symbol} already has an open position"
                    )
                cost = self._decimal(signal.amount, "paper buy amount")
                available = self._decimal(balances.get("BNB", 0), "BNB balance")
                if cost > available:
                    raise PaperGuardError("paper fill blocked: insufficient paper BNB balance")
                balances["BNB"] = str(available - cost)
                balances[signal.symbol] = str(
                    self._decimal(balances.get(signal.symbol, 0), f"{signal.symbol} balance")
                    + amount_out
                )
                positions[signal.symbol] = {
                    "amount": str(amount_out),
                    "entry_price": signal.price,
                }
                amount_in = cost
            elif signal.action == "sell":
                if signal.symbol not in positions:
                    raise PaperGuardError(
                        f"paper fill blocked: no open {signal.symbol} position"
                    )
                position_amount = self._decimal(
                    positions[signal.symbol].get("amount"), f"{signal.symbol} position"
                )
                held = self._decimal(
                    balances.get(signal.symbol, 0), f"{signal.symbol} balance"
                )
                if position_amount > held:
                    raise PaperLoopError(
                        f"Paper {signal.symbol} position exceeds its paper balance."
                    )
                balances["BNB"] = str(
                    self._decimal(balances.get("BNB", 0), "BNB balance") + amount_out
                )
                balances[signal.symbol] = str(held - position_amount)
                positions.pop(signal.symbol)
                amount_in = position_amount
            else:
                raise PaperLoopError(f"Unsupported paper action {signal.action}.")

            fill = {
                "at": now,
                "kind": "paper",
                "action": signal.action,
                "symbol": signal.symbol,
                "amountIn": str(amount_in),
                "amountOut": str(amount_out),
                "price": signal.price,
                "reason": signal.reason,
                "outcome": "filled",
            }
            state["trades"] = list(state.get("trades", [])) + [fill]
            state["last_trade_at"] = now
            self._write(state)
            return PaperFill(
                action=signal.action,
                symbol=signal.symbol,
                amount_in=amount_in,
                amount_out=amount_out,
                price=Decimal(signal.price),
                reason=signal.reason,
                at=now,
            )


class PaperTradingLoop:
    """Continuous read-only quote polling and simulated strategy execution."""

    def __init__(
        self,
        settings: Settings,
        *,
        bridge: SmartRouterBridge | None = None,
        state: PaperStateStore | None = None,
    ):
        self.settings = settings
        self.bridge = bridge or SmartRouterBridge(settings.bsc_rpc_url)
        self.state = state or PaperStateStore(
            settings.paper_state_file,
            starting_bnb=settings.paper_starting_bnb,
            starting_xrp=settings.paper_starting_xrp,
            starting_btc=settings.paper_starting_btc,
        )
        self.rules = StrategyRules(settings)
        self.stop_event = Event()

    def stop(self) -> None:
        self.stop_event.set()

    @staticmethod
    def _raw_amount(amount: Decimal, symbol: str) -> int:
        scaled = amount * Decimal(10**TOKEN_DECIMALS[symbol])
        if scaled != scaled.to_integral_value():
            raise PaperLoopError(f"Paper amount has too many {symbol} decimal places.")
        return int(scaled)

    def _spot_price(self, symbol: str) -> tuple[Decimal, Any]:
        quote = self.bridge.quote(symbol, "WBNB", 10**TOKEN_DECIMALS[symbol])
        price = Decimal(quote.amount_out) / Decimal(10**TOKEN_DECIMALS["WBNB"])
        if not price.is_finite() or price <= 0:
            raise PaperLoopError(f"Factory quote for {symbol} returned no usable price.")
        return price, quote

    def _fill_quote(self, signal: StrategySignal) -> Decimal:
        if signal.action == "buy":
            raw_amount = self._raw_amount(Decimal(signal.amount), "WBNB")
            quote = self.bridge.quote("WBNB", signal.symbol, raw_amount)
            return Decimal(quote.amount_out) / Decimal(10**TOKEN_DECIMALS[signal.symbol])
        raw_amount = self._raw_amount(Decimal(signal.amount), signal.symbol)
        quote = self.bridge.quote(signal.symbol, "WBNB", raw_amount)
        return Decimal(quote.amount_out) / Decimal(10**TOKEN_DECIMALS["WBNB"])

    def _indicators(self, prices: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
        fast = self.rules._ema(prices, self.rules.fast_window)[-1]
        slow = self.rules._ema(prices, self.rules.slow_window)[-1]
        rsi = self.rules._rsi(prices)
        return fast, slow, rsi

    def run_once(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        for symbol in PAPER_SYMBOLS:
            price, quote = self._spot_price(symbol)
            snapshot = self.state.record_price(
                symbol,
                price,
                self.settings.paper_price_history_limit,
                now=timestamp,
            )
            prices = list(snapshot.prices[symbol])
            if len(prices) < max(self.rules.slow_window + 1, self.rules.rsi_window + 1):
                logger.info(
                    "paper_tracking symbol=%s price_bnb=%s samples=%d/%d status=warming_up",
                    symbol,
                    price,
                    len(prices),
                    max(self.rules.slow_window + 1, self.rules.rsi_window + 1),
                )
                continue

            fast, slow, rsi = self._indicators(prices)
            position = snapshot.positions.get(symbol)
            signal = self.rules.decide(
                symbol,
                prices,
                position,
                snapshot.balances["BNB"],
            )
            logger.info(
                "paper_tracking symbol=%s price_bnb=%s ema_fast=%s ema_slow=%s rsi=%.2f "
                "samples=%d bnb_balance=%s position=%s signal=%s route_candidates=%s",
                symbol,
                price,
                fast,
                slow,
                rsi,
                len(prices),
                snapshot.balances["BNB"],
                position.amount if position else "none",
                signal.action if signal else "none",
                getattr(quote, "route_count", "unknown"),
            )
            if signal is None:
                continue
            amount_out = self._fill_quote(signal)
            try:
                fill = self.state.apply_fill(
                    signal,
                    amount_out,
                    now=timestamp,
                    cooldown_seconds=self.settings.strategy_cooldown_seconds,
                    max_trades_per_day=self.settings.strategy_max_trades_per_day,
                )
            except PaperGuardError as error:
                logger.warning(
                    "paper_signal_suppressed symbol=%s action=%s reason=%s",
                    symbol,
                    signal.action,
                    error,
                )
                continue
            logger.info(
                "paper_fill action=%s symbol=%s amount_in=%s amount_out=%s price_bnb=%s "
                "reason=%s tx=none",
                fill.action,
                fill.symbol,
                fill.amount_in,
                fill.amount_out,
                fill.price,
                fill.reason,
            )
        self.state.record_loop_success(now=timestamp)

    def run(self) -> None:
        logger.info(
            "paper_loop_started symbols=%s interval_seconds=%d state_file=%s "
            "live_execution=disabled",
            ",".join(PAPER_SYMBOLS),
            self.settings.paper_poll_interval_seconds,
            self.state.path,
        )
        retry_delay = self.settings.paper_retry_base_seconds
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_once()
                retry_delay = self.settings.paper_retry_base_seconds
                wait_seconds = max(
                    0.0,
                    self.settings.paper_poll_interval_seconds - (time.monotonic() - started),
                )
            except (SmartRouterError, PaperLoopError, TraderError) as error:
                try:
                    self.state.record_loop_error(type(error).__name__)
                except PaperLoopError:
                    logger.exception("paper_loop_health_update_failed")
                logger.error(
                    "paper_loop_error error=%s retry_in_seconds=%d",
                    error,
                    retry_delay,
                )
                wait_seconds = retry_delay
                retry_delay = min(
                    self.settings.paper_retry_max_seconds,
                    max(self.settings.paper_retry_base_seconds, retry_delay * 2),
                )
            self.stop_event.wait(wait_seconds)
        logger.info("paper_loop_stopped state_preserved=true live_execution=disabled")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_environment(require_wallet=False)
        loop = PaperTradingLoop(settings)

        def stop(_signum: int, _frame: Any) -> None:
            loop.stop()

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        loop.run()
        return 0
    except (ConfigurationError, PaperLoopError, TraderError) as error:
        logger.error("paper_loop_stopped error=%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())