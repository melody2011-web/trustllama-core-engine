"""Continuous, guarded live execution for the XRP and BTCB strategy."""

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
from threading import Event, Lock, Thread
from typing import Any, Iterator

from .config import ConfigurationError, DUAL_GRID_PROFILES, Settings
from .contracts import TOKEN_DECIMALS
from .market_data import MarketDataError, MarketDataProvider, dynamic_grid_plan
from .notifier import TelegramNotifier
from .smart_router import SmartRouterBridge, SmartRouterError
from .trader import (
    PancakeSwapTrader,
    StrategyGuardError,
    StrategyRules,
    TradeResult,
    TraderError,
)

logger = logging.getLogger("defi-bot.live")

LIVE_PRICE_STATE_VERSION = 3
LIVE_SYMBOLS = ("XRP", "BTC")
LIVE_BASE_ASSET = "USDT"


class LiveRunnerError(RuntimeError):
    """Raised when live market samples cannot be safely stored or used."""


@dataclass(frozen=True)
class LivePriceSnapshot:
    prices: dict[str, tuple[Decimal, ...]]


class LivePriceStore:
    """A locked, atomic journal for live market samples only.

    Positions, cooldowns, daily limits, and pending transaction claims remain in
    StrategyStateStore, which is owned by PancakeSwapTrader.
    """

    def __init__(self, path: str, symbols: tuple[str, ...] = LIVE_SYMBOLS):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        if not symbols or len(set(symbols)) != len(symbols):
            raise LiveRunnerError("Live price store symbols are invalid.")
        self.symbols = tuple(symbol.upper() for symbol in symbols)

    @contextmanager
    def _locked(self) -> Iterator[None]:
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
            "version": LIVE_PRICE_STATE_VERSION,
            "base_asset": LIVE_BASE_ASSET,
            "prices": {symbol: [] for symbol in self.symbols},
            "notifications": {"pending": [], "delivered": []},
            "heartbeat_at": 0.0,
            "last_successful_poll_at": 0.0,
            "last_retry_at": 0.0,
            "last_retry_delay_seconds": 0,
            "retry_count": 0,
            "last_error_at": 0.0,
            "last_error": None,
            "strategy": {"mode": "disabled", "grid": {}},
        }

    @staticmethod
    def _decimal(value: Any, label: str) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise LiveRunnerError(f"Live price state contains an invalid {label}.") from error
        if not result.is_finite() or result <= 0:
            raise LiveRunnerError(f"Live price state contains an invalid {label}.")
        return result

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            with self.path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise LiveRunnerError(
                f"Live price state file {self.path} is unreadable; refusing to trade."
            ) from error
        if state.get("version") == 1:
            # WBNB-denominated prices cannot be mixed with a USDT strategy.
            # Prices are re-warmed for the USDT strategy.
            state["version"] = LIVE_PRICE_STATE_VERSION
            state["base_asset"] = LIVE_BASE_ASSET
            state["prices"] = {symbol: [] for symbol in self.symbols}
        if state.get("version") == 2:
            state["version"] = LIVE_PRICE_STATE_VERSION
            state.setdefault("strategy", {"mode": "disabled", "grid": {}})
        if state.get("version") != LIVE_PRICE_STATE_VERSION:
            raise LiveRunnerError(
                "Live price state file version is unsupported; refusing to trade."
            )
        if state.get("base_asset") != LIVE_BASE_ASSET:
            raise LiveRunnerError("Live price state is not USDT-denominated; refusing to trade.")
        if not isinstance(state.get("prices"), dict):
            raise LiveRunnerError("Live price state contains invalid price history.")
        notifications = state.setdefault(
            "notifications", {"pending": [], "delivered": []}
        )
        if (
            not isinstance(notifications, dict)
            or not isinstance(notifications.get("pending"), list)
            or not isinstance(notifications.get("delivered"), list)
        ):
            raise LiveRunnerError("Live price state contains invalid notification metadata.")
        state.setdefault("strategy", {"mode": "disabled", "grid": {}})
        return state

    def _write(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        temporary.chmod(0o600)
        temporary.replace(self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _snapshot(self, state: dict[str, Any]) -> LivePriceSnapshot:
        prices = {
            symbol: tuple(
                self._decimal(value, f"{symbol} price")
                for value in state["prices"].get(symbol, [])
            )
            for symbol in self.symbols
        }
        return LivePriceSnapshot(prices=prices)

    def record_price(self, symbol: str, price: Decimal, limit: int) -> LivePriceSnapshot:
        symbol = symbol.upper()
        if symbol not in self.symbols:
            raise LiveRunnerError(f"Live mode does not track {symbol}.")
        if not price.is_finite() or price <= 0:
            raise LiveRunnerError(f"Received an invalid {symbol} price.")
        with self._locked():
            state = self._read()
            prices = state["prices"]
            prices.setdefault(symbol, []).append(str(price))
            prices[symbol] = prices[symbol][-limit:]
            self._write(state)
            return self._snapshot(state)

    def next_prices(self, symbol: str, price: Decimal, limit: int) -> list[Decimal]:
        """Return the candidate history without committing it to the journal."""
        symbol = symbol.upper()
        if symbol not in self.symbols:
            raise LiveRunnerError(f"Live mode does not track {symbol}.")
        if not price.is_finite() or price <= 0:
            raise LiveRunnerError(f"Received an invalid {symbol} price.")
        with self._locked():
            state = self._read()
            history = [
                self._decimal(value, f"{symbol} price")
                for value in state["prices"].get(symbol, [])
            ]
        return (history + [price])[-limit:]

    def queue_notification(self, result: TradeResult) -> None:
        """Durably queue a confirmed alert exactly once by transaction hash."""
        with self._locked():
            state = self._read()
            notifications = state["notifications"]
            normalized = result.tx_hash.lower()
            delivered = {str(value).lower() for value in notifications["delivered"]}
            pending = {
                str(item.get("tx_hash", "")).lower()
                for item in notifications["pending"]
                if isinstance(item, dict)
            }
            if normalized in delivered or normalized in pending:
                return
            notifications["pending"].append(
                {
                    "action": result.action,
                    "symbol": result.symbol,
                    "amount_in": result.amount_in,
                    "amount_out": result.amount_out,
                    "tx_hash": result.tx_hash,
                    "block_number": result.block_number,
                    "confirmed_at": time.time(),
                }
            )
            self._write(state)

    def pending_notifications(self) -> list[TradeResult]:
        with self._locked():
            pending = list(self._read()["notifications"]["pending"])
        results: list[TradeResult] = []
        for item in pending:
            if not isinstance(item, dict):
                raise LiveRunnerError("Live price state contains an invalid notification.")
            try:
                results.append(
                    TradeResult(
                        action=str(item["action"]),
                        symbol=str(item["symbol"]),
                        amount_in=str(item["amount_in"]),
                        amount_out=str(item["amount_out"]),
                        tx_hash=str(item["tx_hash"]),
                        block_number=int(item["block_number"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise LiveRunnerError(
                    "Live price state contains an invalid notification."
                ) from error
        return results

    def mark_notification_delivered(self, tx_hash: str) -> None:
        with self._locked():
            state = self._read()
            notifications = state["notifications"]
            normalized = tx_hash.lower()
            notifications["pending"] = [
                item
                for item in notifications["pending"]
                if not isinstance(item, dict)
                or str(item.get("tx_hash", "")).lower() != normalized
            ]
            delivered = [
                str(value)
                for value in notifications["delivered"]
                if str(value).lower() != normalized
            ]
            notifications["delivered"] = (delivered + [tx_hash])[-200:]
            self._write(state)

    @staticmethod
    def _timestamp(value: float | None, label: str) -> float:
        timestamp = time.time() if value is None else value
        if (
            not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
            or timestamp < 0
        ):
            raise LiveRunnerError(f"Live {label} timestamp is invalid.")
        return float(timestamp)

    def record_heartbeat(self, *, now: float | None = None) -> None:
        """Persist liveness without recording wallet, quote, or trade details."""
        timestamp = self._timestamp(now, "runner heartbeat")
        with self._locked():
            state = self._read()
            state["heartbeat_at"] = timestamp
            self._write(state)

    def record_poll_success(self, *, now: float | None = None) -> None:
        timestamp = self._timestamp(now, "poll")
        with self._locked():
            state = self._read()
            state["heartbeat_at"] = timestamp
            state["last_successful_poll_at"] = timestamp
            state["last_error_at"] = 0.0
            state["last_error"] = None
            state["retry_count"] = 0
            state["last_retry_delay_seconds"] = 0
            self._write(state)

    def record_poll_error(
        self,
        error: str,
        *,
        retry_delay_seconds: int,
        now: float | None = None,
    ) -> None:
        timestamp = self._timestamp(now, "poll error")
        if (
            not isinstance(retry_delay_seconds, int)
            or isinstance(retry_delay_seconds, bool)
            or retry_delay_seconds < 0
        ):
            raise LiveRunnerError("Live retry delay is invalid.")
        with self._locked():
            state = self._read()
            retry_count = state.get("retry_count", 0)
            if (
                not isinstance(retry_count, int)
                or isinstance(retry_count, bool)
                or retry_count < 0
            ):
                raise LiveRunnerError("Live retry count is invalid.")
            state["heartbeat_at"] = timestamp
            state["last_retry_at"] = timestamp
            state["last_retry_delay_seconds"] = retry_delay_seconds
            state["retry_count"] = retry_count + 1
            state["last_error_at"] = timestamp
            state["last_error"] = str(error)[:500]
            self._write(state)

    def record_strategy_status(
        self,
        mode: str,
        grid: dict[str, dict[str, Any]],
        *,
        live_execution: bool = False,
    ) -> None:
        if mode not in {"disabled", "ema", "grid"}:
            raise LiveRunnerError("Live strategy mode is invalid.")
        if not isinstance(live_execution, bool):
            raise LiveRunnerError("Live execution state is invalid.")
        with self._locked():
            state = self._read()
            previous_strategy = state.get("strategy")
            previous_mode = (
                previous_strategy.get("mode")
                if isinstance(previous_strategy, dict)
                else None
            )
            if previous_mode != mode:
                # Price samples are canonical only within one live strategy
                # mode. Clear them in the same durable write as the new mode
                # marker so EMA can never consume samples from another mode.
                state["prices"] = {symbol: [] for symbol in self.symbols}
                logger.info(
                    "live_price_history_reset previous_mode=%s active_mode=%s",
                    previous_mode or "unknown",
                    mode,
                )
            state["strategy"] = {
                "mode": mode,
                "liveExecution": live_execution,
                "grid": grid,
            }
            self._write(state)


class LiveTradingLoop:
    """Poll market prices and hand valid signals to the guarded live executor."""

    def __init__(
        self,
        settings: Settings,
        *,
        bridge: SmartRouterBridge | None = None,
        trader: PancakeSwapTrader | None = None,
        prices: LivePriceStore | None = None,
        notifier: TelegramNotifier | None = None,
        stop_event: Event | None = None,
        settings_provider: Any | None = None,
        market_data_provider: MarketDataProvider | None = None,
        execution_authorizer: Any | None = None,
    ):
        if not settings.enable_live_trading:
            raise ConfigurationError(
                "Live runner requires ENABLE_LIVE_TRADING=true; refusing to start."
            )
        self.settings = settings
        self.bridge = bridge or SmartRouterBridge(settings.bsc_rpc_url)
        self.trader = trader or PancakeSwapTrader(settings)
        self.prices = prices or LivePriceStore(
            settings.live_price_history_file, settings.grid_symbols if settings.isolated_grid_profile else LIVE_SYMBOLS
        )
        self.notifier = notifier or TelegramNotifier(settings)
        self.rules = StrategyRules(settings)
        self.strategy_mode = settings.live_strategy_mode
        self._settings_provider = settings_provider
        self._market_data_provider = market_data_provider
        self._execution_authorizer = execution_authorizer
        if (
            settings.isolated_grid_profile in DUAL_GRID_PROFILES
            and market_data_provider is None
        ):
            raise ConfigurationError(
                "BTC/XRP isolated grids require a validated market-data provider."
            )
        if (
            settings.isolated_grid_profile in DUAL_GRID_PROFILES
            and self.strategy_mode != "grid"
        ):
            raise ConfigurationError("BTC/XRP isolated services are grid-only.")
        self._grid_status: dict[str, dict[str, Any]] = {
            ("BNB" if symbol == "WBNB" else symbol): {
                "anchorPrice": None,
                "nextLevel": None,
                "filledLevels": 0,
                "totalLevels": settings.grid_levels,
                "spentUsdt": "0",
                "remainingUsdt": format(settings.grid_max_budget_usdt.normalize(), "f"),
                "activeExposureUsdt": "0",
                "realizedUsdt": "0",
                "openLots": [],
                "lastSellOutcome": None,
                "state": "not_started",
            }
            for symbol in settings.grid_symbols
        }
        self.stop_event = stop_event or Event()
        self._notification_lock = Lock()
        self._notification_flush_active = False

    def stop(self) -> None:
        self.stop_event.set()

    def _queue_notification(self, result: TradeResult) -> None:
        try:
            self.prices.queue_notification(result)
        except Exception:
            logger.exception(
                "live_notification_queue_failed tx=%s reason=outbox_write_failed",
                result.tx_hash,
            )
            return
        acknowledge = getattr(self.trader, "mark_notification_transferred", None)
        if callable(acknowledge):
            acknowledge(result.tx_hash)
        self._flush_notifications()

    def _recover_confirmed_notifications(self) -> None:
        pending = getattr(self.trader, "confirmed_notifications", None)
        acknowledge = getattr(self.trader, "mark_notification_transferred", None)
        if not callable(pending) or not callable(acknowledge):
            return
        for result in pending():
            self.prices.queue_notification(result)
            acknowledge(result.tx_hash)

    def _flush_notifications(self) -> None:
        with self._notification_lock:
            if self._notification_flush_active:
                return
            self._notification_flush_active = True

        def flush() -> None:
            try:
                for result in self.prices.pending_notifications():
                    if not self.notifier.notify_success(result):
                        logger.error(
                            "live_notification_pending tx=%s reason=delivery_unconfirmed",
                            result.tx_hash,
                        )
                        return
                    self.prices.mark_notification_delivered(result.tx_hash)
                    logger.info("live_notification_delivered tx=%s", result.tx_hash)
            except Exception:
                logger.exception("live_notification_flush_failed")
            finally:
                with self._notification_lock:
                    self._notification_flush_active = False

        Thread(target=flush, name="live-notification-outbox", daemon=True).start()

    def _spot_price(self, symbol: str) -> tuple[Decimal, Any]:
        quote = self.bridge.quote(
            symbol, LIVE_BASE_ASSET, 10**TOKEN_DECIMALS[symbol],
            direct_v3=symbol in {"XRP", "BTC"}
        )
        price = Decimal(quote.amount_out) / Decimal(
            10**TOKEN_DECIMALS[LIVE_BASE_ASSET]
        )
        if not price.is_finite() or price <= 0:
            raise LiveRunnerError(f"Factory quote for {symbol} returned no usable price.")
        return price, quote

    def _take_profit_price(self, symbol: str, amount_token: str) -> tuple[Decimal, Any]:
        """Return the effective USDT/token price for the exact lot sell amount."""
        try:
            amount = Decimal(amount_token)
            amount_in = amount * Decimal(10**TOKEN_DECIMALS[symbol])
        except (InvalidOperation, ValueError, KeyError) as error:
            raise LiveRunnerError("Grid take-profit lot has an invalid token amount.") from error
        if (
            not amount.is_finite()
            or amount <= 0
            or amount_in != amount_in.to_integral_value()
        ):
            raise LiveRunnerError("Grid take-profit lot has an invalid token amount.")
        quote = self.bridge.quote(
            symbol, LIVE_BASE_ASSET, int(amount_in), direct_v3=symbol in {"XRP", "BTC"}
        )
        proceeds = Decimal(quote.amount_out) / Decimal(
            10**TOKEN_DECIMALS[LIVE_BASE_ASSET]
        )
        price = proceeds / amount
        if not proceeds.is_finite() or proceeds <= 0 or not price.is_finite() or price <= 0:
            raise LiveRunnerError(
                f"Factory quote for the {symbol} take-profit lot returned no usable price."
            )
        return price, quote

    def _indicators(self, prices: list[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
        fast = self.rules._ema(prices, self.rules.fast_window)[-1]
        slow = self.rules._ema(prices, self.rules.slow_window)[-1]
        return fast, slow, self.rules._rsi(prices)

    def _reconcile_pending(self) -> bool:
        pending = self.trader.inspect_pending()
        if pending is None:
            logger.info("live_pending_reconciliation status=clear")
            return True
        expected_kind = "grid" if self.strategy_mode == "grid" else "strategy"
        if pending.kind != expected_kind:
            logger.warning(
                "live_pending_blocked kind=%s action=%s symbol=%s phase=%s tx=%s",
                pending.kind,
                pending.action,
                pending.symbol,
                pending.phase or "unknown",
                pending.tx_hash or "unavailable",
            )
            return False
        result = (
            self.trader.reconcile_grid()
            if self.strategy_mode == "grid"
            else self.trader.reconcile_strategy()
        )
        if result is not None:
            logger.info(
                "live_reconciled action=%s symbol=%s amount_in=%s amount_out=%s tx=%s block=%s",
                result.action,
                result.symbol,
                result.amount_in,
                result.amount_out,
                result.tx_hash,
                result.block_number,
            )
            self._queue_notification(result)
        if self.trader.inspect_pending() is not None:
            logger.warning(
                "live_pending_blocked kind=%s action=%s symbol=%s phase=%s tx=%s",
                pending.kind,
                pending.action,
                pending.symbol,
                pending.phase or "unknown",
                pending.tx_hash or "unavailable",
            )
            return False
        return True

    def _record_grid_status(self, snapshot: Any) -> None:
        status_symbol = "BNB" if snapshot.symbol == "WBNB" else snapshot.symbol
        self._grid_status[status_symbol] = {
            "anchorPrice": snapshot.anchor_price,
            "nextLevel": snapshot.next_level,
            "filledLevels": len(snapshot.filled_levels),
            "totalLevels": len(snapshot.levels),
            "spentUsdt": snapshot.spent_usdt,
            "remainingUsdt": snapshot.remaining_usdt,
            "activeExposureUsdt": snapshot.spent_usdt,
            "realizedUsdt": snapshot.realized_usdt,
            "openLots": [
                {
                    "level": lot.level,
                    "amountToken": lot.amount_token,
                    "buyAmountUsdt": lot.buy_amount_usdt,
                    "buyPrice": lot.buy_price,
                    "targetPrice": lot.target_price,
                    "buyTxHash": lot.buy_tx_hash,
                }
                for lot in snapshot.open_lots
            ],
            "lastSellOutcome": snapshot.last_sell_outcome,
            "state": snapshot.status,
        }
        self.prices.record_strategy_status(
            "grid", self._grid_status, live_execution=self.settings.enable_live_trading
        )

    def _run_grid_once(self) -> None:
        for symbol in self.settings.grid_symbols:
            existing = self.trader.grid_snapshot(symbol)
            if existing is not None:
                self._record_grid_status(existing)
                for lot in existing.open_lots:
                    take_profit_price, take_profit_quote = self._take_profit_price(
                        symbol, lot.amount_token
                    )
                    take_profit, take_profit_snapshot = self.trader.grid_take_profit_decide(
                        symbol, take_profit_price, lot_id=lot.lot_id
                    )
                    if take_profit_snapshot is not None:
                        self._record_grid_status(take_profit_snapshot)
                    if take_profit is None:
                        logger.info(
                            "live_grid_take_profit_tracking symbol=%s level=%d "
                            "effective_price_usdt=%s target_price_usdt=%s "
                            "route_candidates=%s status=waiting",
                            symbol,
                            lot.level,
                            take_profit_price,
                            lot.target_price,
                            getattr(take_profit_quote, "route_count", "unknown"),
                        )
                        continue
                    try:
                        result = self.trader.execute_grid_take_profit(take_profit)
                    except StrategyGuardError as error:
                        logger.warning(
                            "live_grid_take_profit_suppressed symbol=%s level=%d reason=%s",
                            take_profit.symbol,
                            take_profit.level,
                            error,
                        )
                        continue
                    updated = self.trader.grid_snapshot(symbol)
                    if updated is not None:
                        self._record_grid_status(updated)
                    logger.info(
                        "live_grid_take_profit_confirmed symbol=%s level=%d amount_in=%s "
                        "amount_out_usdt=%s target_price_usdt=%s tx=%s block=%s",
                        take_profit.symbol,
                        take_profit.level,
                        result.amount_in,
                        result.amount_out,
                        take_profit.target_price,
                        result.tx_hash,
                        result.block_number,
                    )
                    self._queue_notification(result)
                    # One action per pass applies across all buys and sells.
                    return
            plan = None
            if self.settings.isolated_grid_profile in DUAL_GRID_PROFILES:
                try:
                    market = self._market_data_provider.read(symbol)
                    plan = dynamic_grid_plan(
                        market,
                        levels=self.settings.grid_levels,
                        min_spacing_bps=100,
                        max_spacing_bps=500,
                        buy_rsi_max=Decimal(self.settings.strategy_buy_rsi_max),
                    )
                except MarketDataError as error:
                    logger.warning(
                        "live_grid_new_entries_blocked symbol=%s reason=%s",
                        symbol,
                        error,
                    )
                    continue
                if not plan.entry_allowed:
                    logger.info(
                        "live_grid_new_entries_blocked symbol=%s rsi=%s "
                        "volume_confirmed=%s reason=%s",
                        symbol,
                        plan.rsi,
                        plan.volume_confirmed,
                        plan.reason,
                    )
                    continue
            price, quote = self._spot_price(symbol)
            if plan is None:
                signal, snapshot = self.trader.grid_decide(symbol, price)
            else:
                signal, snapshot = self.trader.grid_decide(
                    symbol,
                    price,
                    anchor_price=plan.anchor_price,
                    levels=plan.levels,
                )
            self._record_grid_status(snapshot)
            logger.info(
                "live_grid_tracking symbol=%s price_usdt=%s anchor_price_usdt=%s "
                "next_level=%s filled_levels=%d/%d spent_usdt=%s remaining_usdt=%s "
                "open_lots=%d take_profit_targets=%s state=%s route_candidates=%s "
                "immediate_entry=%s",
                symbol,
                price,
                snapshot.anchor_price or "unavailable",
                snapshot.next_level or "none",
                len(snapshot.filled_levels),
                len(snapshot.levels),
                snapshot.spent_usdt,
                snapshot.remaining_usdt,
                len(snapshot.open_lots),
                ",".join(lot.target_price for lot in snapshot.open_lots) or "none",
                snapshot.status,
                getattr(quote, "route_count", "unknown"),
                bool(signal and signal.immediate),
            )
            try:
                if signal is None:
                    self.prices.record_price(
                        symbol, price, self.settings.live_price_history_limit
                    )
                    continue
                result = self.trader.execute_grid(signal)
            except StrategyGuardError as error:
                self.prices.record_price(symbol, price, self.settings.live_price_history_limit)
                logger.warning(
                    "live_grid_suppressed symbol=%s reason=%s", symbol, error
                )
                continue
            self.prices.record_price(symbol, price, self.settings.live_price_history_limit)
            updated = self.trader.grid_snapshot(symbol)
            if updated is not None:
                self._record_grid_status(updated)
            logger.info(
                "live_grid_execution_confirmed symbol=%s level=%d amount_in=%s "
                "amount_out=%s tx=%s block=%s",
                signal.symbol,
                signal.level,
                result.amount_in,
                result.amount_out,
                result.tx_hash,
                result.block_number,
            )
            self._queue_notification(result)
            # Preserve the one-order-at-a-time safety boundary across assets.
            return

    def _run_once(self) -> None:
        if self._execution_authorizer is not None:
            # Recheck every pass before reconciliation, quotes, approvals, or
            # swaps so expiry, revocation, or gate removal takes effect without
            # restarting the process.
            self._execution_authorizer()
        if self._settings_provider is not None:
            # The provider reads only the locked per-bot public size config.
            # StateStore sees a changed grid profile and allows exits only
            # until old lots are closed.
            refreshed = self._settings_provider()
            if (
                refreshed.isolated_grid_profile in DUAL_GRID_PROFILES
                and refreshed.live_strategy_mode != "grid"
            ):
                raise ConfigurationError("BTC/XRP isolated services are grid-only.")
            if refreshed != self.settings:
                self.settings = refreshed
                self.trader.settings = refreshed
                self.rules = StrategyRules(refreshed)
                self.strategy_mode = refreshed.live_strategy_mode
        if not self._reconcile_pending():
            return
        if self.strategy_mode == "grid":
            self._run_grid_once()
            return
        if self.strategy_mode == "disabled":
            self.prices.record_strategy_status(
                "disabled", {}, live_execution=self.settings.enable_live_trading
            )
            for symbol in LIVE_SYMBOLS:
                price, quote = self._spot_price(symbol)
                self.prices.record_price(
                    symbol, price, self.settings.live_price_history_limit
                )
                logger.info(
                    "live_tracking symbol=%s price_usdt=%s status=execution_paused "
                    "route_candidates=%s",
                    symbol,
                    price,
                    getattr(quote, "route_count", "unknown"),
                )
            return
        required_samples = max(self.rules.slow_window + 1, self.rules.rsi_window + 1)
        self.prices.record_strategy_status(
            "ema", {}, live_execution=self.settings.enable_live_trading
        )
        for symbol in LIVE_SYMBOLS:
            price, quote = self._spot_price(symbol)
            history = self.prices.next_prices(
                symbol, price, self.settings.live_price_history_limit
            )
            if len(history) < required_samples:
                self.prices.record_price(
                    symbol, price, self.settings.live_price_history_limit
                )
                logger.info(
                    "live_tracking symbol=%s price_usdt=%s samples=%d/%d status=warming_up",
                    symbol,
                    price,
                    len(history),
                    required_samples,
                )
                continue
            fast, slow, rsi = self._indicators(history)
            logger.info(
                "live_tracking symbol=%s price_usdt=%s ema_fast=%s ema_slow=%s rsi=%.2f "
                "samples=%d route_candidates=%s status=scanning base_asset=%s",
                symbol,
                price,
                fast,
                slow,
                rsi,
                len(history),
                getattr(quote, "route_count", "unknown"),
                LIVE_BASE_ASSET,
            )
            try:
                result = self.trader.execute_strategy(symbol, history)
            except StrategyGuardError as error:
                self.prices.record_price(
                    symbol, price, self.settings.live_price_history_limit
                )
                logger.warning(
                    "live_signal_suppressed symbol=%s reason=%s", symbol, error
                )
                continue
            self.prices.record_price(
                symbol, price, self.settings.live_price_history_limit
            )
            if result is None:
                continue
            logger.info(
                "live_execution_confirmed action=%s symbol=%s amount_in=%s amount_out=%s "
                "tx=%s block=%s",
                result.action,
                result.symbol,
                result.amount_in,
                result.amount_out,
                result.tx_hash,
                result.block_number,
            )
            self._queue_notification(result)
            # The global strategy cooldown applies to both assets, so do not
            # evaluate a second potential order in the same loop pass.
            return

    def run_once(self) -> None:
        self._recover_confirmed_notifications()
        # Resume any retained alert after a runner restart. The delivery bridge
        # will refuse an automatic duplicate if its prior outcome is unknown.
        self._flush_notifications()
        self.prices.record_heartbeat()
        self._run_once()
        self.prices.record_poll_success()

    def run(self) -> None:
        logger.info(
            "live_runner_started strategy_mode=%s symbols=%s base_asset=%s "
            "interval_seconds=%d live_execution=enabled",
            self.strategy_mode,
            ",".join(
                self.settings.grid_symbols
                if self.strategy_mode == "grid"
                else LIVE_SYMBOLS
            ),
            LIVE_BASE_ASSET,
            self.settings.live_poll_interval_seconds,
        )
        if self.strategy_mode == "grid":
            logger.info(
                "live_grid_profile TOTAL_GRID_LEVELS=%d PER_LINE_USDT=%s "
                "GRID_STEP_PERCENT=%s BUY_IMMEDIATELY_NOW=%s "
                "PROFIT_TARGET_PERCENT=%s GRID_MAX_BUDGET_USDT=%s",
                self.settings.grid_levels,
                format(self.settings.grid_order_usdt.normalize(), "f"),
                format(
                    (Decimal(self.settings.grid_spacing_bps) / Decimal(10_000)).normalize(),
                    "f",
                ),
                self.settings.buy_immediately_now,
                format(
                    (
                        Decimal(self.settings.grid_take_profit_bps) / Decimal(10_000)
                    ).normalize(),
                    "f",
                ),
                format(self.settings.grid_max_budget_usdt.normalize(), "f"),
            )
        retry_delay = self.settings.live_retry_base_seconds
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_once()
                retry_delay = self.settings.live_retry_base_seconds
                wait_seconds = max(
                    0.0,
                    self.settings.live_poll_interval_seconds
                    - (time.monotonic() - started),
                )
            except Exception as error:
                logger.exception(
                    "live_runner_error error=%s retry_in_seconds=%d", error, retry_delay
                )
                try:
                    self.prices.record_poll_error(
                        type(error).__name__,
                        retry_delay_seconds=retry_delay,
                    )
                except LiveRunnerError:
                    logger.exception("live_runner_status_write_failed")
                wait_seconds = retry_delay
                retry_delay = min(
                    self.settings.live_retry_max_seconds,
                    max(self.settings.live_retry_base_seconds, retry_delay * 2),
                )
            self.stop_event.wait(wait_seconds)
        logger.info("live_runner_stopped state_preserved=true")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        settings = Settings.from_environment()
    except ConfigurationError as error:
        logger.error("live_runner_stopped error=%s", error)
        return 1

    stop_event = Event()

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    retry_delay = settings.live_retry_base_seconds
    while not stop_event.is_set():
        try:
            LiveTradingLoop(settings, stop_event=stop_event).run()
            retry_delay = settings.live_retry_base_seconds
        except (LiveRunnerError, SmartRouterError, TraderError) as error:
            logger.exception(
                "live_runner_startup_error error=%s retry_in_seconds=%d",
                error,
                retry_delay,
            )
            try:
                LivePriceStore(settings.live_price_history_file).record_poll_error(
                    type(error).__name__,
                    retry_delay_seconds=retry_delay,
                )
            except LiveRunnerError:
                logger.exception("live_runner_status_write_failed")
            stop_event.wait(retry_delay)
            retry_delay = min(
                settings.live_retry_max_seconds,
                max(settings.live_retry_base_seconds, retry_delay * 2),
            )
    logger.info("live_runner_stopped state_preserved=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
