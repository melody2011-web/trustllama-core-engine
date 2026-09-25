from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from eth_account import Account
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput, ContractLogicError, TransactionNotFound
from web3.middleware import ExtraDataToPOAMiddleware

from .config import LEGACY_GRID_TAKE_PROFIT_BPS, Settings
from .contracts import (
    ERC20_ABI,
    PANCAKESWAP_SMART_ROUTER,
    PANCAKESWAP_V3_QUOTER_V2,
    PANCAKESWAP_V3_ROUTER,
    ROUTING_TOKENS,
    TOKENS,
    TOKEN_DECIMALS,
    V3_FEE_TIERS,
    V3_QUOTER_V2_ABI,
    V3_ROUTER_ABI,
)
from .smart_router import SmartRouterBridge, SmartRouterError, SmartRouterTrade

BSC_CHAIN_ID = 56
STATE_VERSION = 3
STRATEGY_BASE_ASSET = "USDT"
STRATEGY_TARGETS = frozenset({"XRP", "BTC"})
GRID_STATE_VERSION = 2
WBNB_WITHDRAWAL_TOPIC = Web3.keccak(text="Withdrawal(address,uint256)").hex()


class TraderError(RuntimeError):
    """Raised when a trade cannot be safely prepared or confirmed."""


class LiveTradingDisabled(TraderError):
    """Raised when a live transaction is requested without an explicit gate."""


class StrategyGuardError(TraderError):
    """Raised when a strategy order would duplicate or overlap another order."""


class TransactionReverted(TraderError):
    """Raised when a transaction receipt explicitly reports a revert."""


@dataclass(frozen=True)
class V3Route:
    tokens: tuple[str, ...]
    fees: tuple[int, ...]
    encoded_path: bytes
    amount_out: int

    def display(self) -> str:
        parts = [self.tokens[0]]
        for fee, token in zip(self.fees, self.tokens[1:]):
            parts.extend((f"-{fee}->", token))
        return " ".join(parts)


@dataclass(frozen=True)
class TradeResult:
    action: str
    symbol: str
    amount_in: str
    amount_out: str
    tx_hash: str
    block_number: int

    def notification_payload(self) -> dict[str, Any]:
        """Return only confirmed trade details safe for the Telegram bridge."""
        return {
            "status": "success",
            "environment": "live",
            "live": True,
            "action": self.action,
            "symbol": self.symbol,
            "amountIn": self.amount_in,
            "amountOut": self.amount_out,
            "txHash": self.tx_hash,
            "blockNumber": self.block_number,
        }


@dataclass(frozen=True)
class PendingTrade:
    """The read-only details an operator needs before receipt reconciliation."""

    kind: str
    action: str
    symbol: str
    amount: str
    tx_hash: str | None
    phase: str | None


@dataclass(frozen=True)
class JournalExposure:
    """Read-only exposure summary for operator preflight checks."""

    pending: PendingTrade | None
    open_lot_count: int
    open_lots_by_symbol: dict[str, int]
    open_exposure_usdt: Decimal
    open_exposure_by_symbol: dict[str, Decimal]
    strategy_positions: dict[str, str]


@dataclass(frozen=True)
class StrategyPosition:
    amount: str
    entry_price: str


@dataclass(frozen=True)
class StrategySignal:
    action: str
    symbol: str
    amount: str
    price: str
    reason: str

    @property
    def fingerprint(self) -> str:
        return ":".join((self.action, self.symbol, self.amount, self.price))


@dataclass(frozen=True)
class GridSignal:
    symbol: str
    level: int
    amount: str
    price: str
    anchor_price: str
    immediate: bool = False

    @property
    def fingerprint(self) -> str:
        return ":".join(
            ("grid", self.symbol, str(self.level), self.amount, self.anchor_price)
        )


@dataclass(frozen=True)
class GridTakeProfitSignal:
    symbol: str
    level: int
    amount: str
    price: str
    target_price: str
    anchor_price: str
    lot_id: str

    @property
    def fingerprint(self) -> str:
        return ":".join(
            (
                "grid-sell",
                self.symbol,
                str(self.level),
                self.lot_id,
                self.price,
            )
        )


@dataclass(frozen=True)
class GridLot:
    lot_id: str
    level: int
    amount_token: str
    buy_amount_usdt: str
    buy_price: str
    target_price: str
    buy_tx_hash: str


@dataclass(frozen=True)
class GridSnapshot:
    symbol: str
    anchor_price: str | None
    levels: tuple[str, ...]
    filled_levels: tuple[int, ...]
    spent_usdt: str
    remaining_usdt: str
    status: str
    open_lots: tuple[GridLot, ...] = ()
    realized_usdt: str = "0"
    last_sell_outcome: dict[str, Any] | None = None

    @property
    def next_level(self) -> int | None:
        if self.status != "active":
            return None
        filled = set(self.filled_levels)
        return next((level for level in range(1, len(self.levels) + 1) if level not in filled), None)


class StrategyStateStore:
    """A small locked journal that survives separate CLI process invocations."""

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

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

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "version": STATE_VERSION,
                "base_asset": STRATEGY_BASE_ASSET,
                "pending": None,
                "last_trade_at": 0.0,
                "trades": [],
                "positions": {},
                "grid": {"version": GRID_STATE_VERSION, "config": None, "assets": {}},
                "confirmed_notifications": [],
            }
        try:
            with self.path.open(encoding="utf-8") as state_file:
                state = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise StrategyGuardError(
                f"Strategy state file {self.path} is unreadable; refusing to trade."
            ) from error
        if state.get("version") == 1:
            if state.get("pending") is not None or state.get("positions"):
                raise StrategyGuardError(
                    "Legacy WBNB-denominated strategy state has an open position or "
                    "pending transaction; reconcile it explicitly before USDT trading."
                )
            state["version"] = STATE_VERSION
            state["base_asset"] = STRATEGY_BASE_ASSET
        if state.get("version") == 2:
            state["version"] = STATE_VERSION
            state.setdefault(
                "grid",
                {"version": GRID_STATE_VERSION, "config": None, "assets": {}},
            )
        if state.get("version") != STATE_VERSION:
            raise StrategyGuardError("Strategy state file version is unsupported; refusing to trade.")
        if state.get("base_asset") != STRATEGY_BASE_ASSET:
            raise StrategyGuardError(
                "Strategy state is not USDT-denominated; refusing to submit a trade."
            )
        state.setdefault("confirmed_notifications", [])
        if not isinstance(state["confirmed_notifications"], list):
            raise StrategyGuardError("Strategy notification recovery state is invalid.")
        return state

    @staticmethod
    def _stage_confirmed_notification(
        state: dict[str, Any], result: TradeResult
    ) -> None:
        pending = state.setdefault("confirmed_notifications", [])
        normalized = result.tx_hash.lower()
        if any(
            isinstance(item, dict)
            and str(item.get("tx_hash", "")).lower() == normalized
            for item in pending
        ):
            return
        pending.append(
            {
                "action": result.action,
                "symbol": result.symbol,
                "amount_in": result.amount_in,
                "amount_out": result.amount_out,
                "tx_hash": result.tx_hash,
                "block_number": result.block_number,
            }
        )

    @staticmethod
    def _text(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @staticmethod
    def _grid_config(settings: Settings) -> dict[str, Any]:
        return {
            "symbols": list(settings.grid_symbols),
            "spacing_bps": settings.grid_spacing_bps,
            "levels": settings.grid_levels,
            "order_usdt": StrategyStateStore._text(settings.grid_order_usdt),
            "max_budget_usdt": StrategyStateStore._text(settings.grid_max_budget_usdt),
            "take_profit_bps": settings.grid_take_profit_bps,
            "buy_immediately_now": settings.buy_immediately_now,
        }

    def _grid(self, state: dict[str, Any], settings: Settings) -> dict[str, Any]:
        config = self._grid_config(settings)
        grid = state.get("grid")
        if not isinstance(grid, dict):
            raise StrategyGuardError("Grid state is invalid; refusing to trade.")
        if grid.get("version") == 1:
            assets = grid.get("assets")
            if not isinstance(assets, dict):
                raise StrategyGuardError("Legacy grid state is invalid; refusing to trade.")
            for asset in assets.values():
                if not isinstance(asset, dict):
                    raise StrategyGuardError(
                        "Legacy grid state is invalid; refusing to trade."
                    )
                filled_levels = asset.get("filled_levels", [])
                spent_usdt = asset.get("spent_usdt", "0")
                try:
                    has_fills = bool(filled_levels) or Decimal(str(spent_usdt)) != 0
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise StrategyGuardError(
                        "Legacy grid state is invalid; refusing to trade."
                    ) from error
                if has_fills:
                    raise StrategyGuardError(
                        "Legacy grid fills lack take-profit lot details; refusing to reset them."
                    )
            # Empty pre-recycling journals are unambiguous and can be upgraded.
            grid["version"] = GRID_STATE_VERSION
            for asset in assets.values():
                asset.setdefault("lots", [])
                asset.setdefault("realized_usdt", "0")
                asset.setdefault("last_sell_outcome", None)
        if grid.get("version") != GRID_STATE_VERSION:
            raise StrategyGuardError(
                "Grid state version is unsupported; refusing to trade."
            )
        if grid.get("config") is None:
            grid["config"] = config
        elif grid.get("config") != config:
            assets = grid.get("assets")
            if not isinstance(assets, dict):
                raise StrategyGuardError("Grid asset state is invalid; refusing to trade.")
            for asset in assets.values():
                if not isinstance(asset, dict):
                    raise StrategyGuardError("Grid asset state is invalid; refusing to trade.")
                lots = asset.get("lots", [])
                if not isinstance(lots, list):
                    raise StrategyGuardError("Grid lots are invalid; refusing to trade.")
                if any(
                    isinstance(lot, dict) and lot.get("status") == "open"
                    for lot in lots
                ):
                    break
            else:
                if state.get("pending") is None:
                    for asset in assets.values():
                        try:
                            anchor = Decimal(str(asset.get("anchor_price")))
                        except (InvalidOperation, TypeError, ValueError) as error:
                            raise StrategyGuardError(
                                "Grid anchor is invalid; refusing to change configuration."
                            ) from error
                        if not anchor.is_finite() or anchor <= 0:
                            raise StrategyGuardError(
                                "Grid anchor is invalid; refusing to change configuration."
                            )
                        asset["levels"] = [
                            self._text(
                                anchor
                                * Decimal(10_000 - settings.grid_spacing_bps * level)
                                / Decimal(10_000)
                            )
                            for level in range(1, settings.grid_levels + 1)
                        ]
                        asset["filled_levels"] = []
                        asset["spent_usdt"] = "0"
                        asset["paused_at_floor"] = False
                        asset["immediate_entry_claimed"] = False
                    # Activate a new profile only after all prior exposure is closed.
                    grid["config"] = config
        if not isinstance(grid.get("assets"), dict):
            raise StrategyGuardError("Grid asset state is invalid; refusing to trade.")
        return grid

    @staticmethod
    def _grid_snapshot(
        symbol: str,
        asset: dict[str, Any],
        settings: Settings,
        *,
        profile_change_pending: bool = False,
    ) -> GridSnapshot:
        try:
            anchor = Decimal(str(asset["anchor_price"]))
            levels = tuple(str(value) for value in asset["levels"])
            parsed_levels = tuple(Decimal(value) for value in levels)
        except (KeyError, InvalidOperation, TypeError, ValueError) as error:
            raise StrategyGuardError("Grid state contains invalid numeric values.") from error
        raw_lots = asset.get("lots", [])
        if not isinstance(raw_lots, list):
            raise StrategyGuardError("Grid lots are invalid; refusing to trade.")
        if not isinstance(asset.get("immediate_entry_claimed", False), bool):
            raise StrategyGuardError(
                "Grid immediate-entry state is invalid; refusing to trade."
            )
        lots: list[GridLot] = []
        open_levels: set[int] = set()
        lot_ids: set[str] = set()
        spent = Decimal(0)
        for raw in raw_lots:
            if not isinstance(raw, dict):
                raise StrategyGuardError("Grid lots are invalid; refusing to trade.")
            try:
                status = str(raw["status"])
                level = int(raw["level"])
                amount_token = Decimal(str(raw["amount_token"]))
                buy_amount = Decimal(str(raw["buy_amount_usdt"]))
                buy_price = Decimal(str(raw["buy_price"]))
                target_price = Decimal(str(raw["target_price"]))
                lot_id = str(raw["lot_id"])
                buy_tx_hash = str(raw["buy_tx_hash"])
            except (KeyError, InvalidOperation, TypeError, ValueError) as error:
                raise StrategyGuardError(
                    "Grid lot contains invalid numeric values; refusing to trade."
                ) from error
            values = (amount_token, buy_amount, buy_price, target_price)
            if (
                status not in {"open", "closed"}
                or level < 1
                or level > settings.grid_levels
                or lot_id in lot_ids
                or not lot_id
                or not buy_tx_hash
                or any(not value.is_finite() or value <= 0 for value in values)
                or target_price <= buy_price
            ):
                raise StrategyGuardError("Grid lot fails safety validation; refusing to trade.")
            lot_ids.add(lot_id)
            if status == "open":
                if level in open_levels:
                    raise StrategyGuardError(
                        "Grid has duplicate open levels; refusing to trade."
                    )
                open_levels.add(level)
                spent += buy_amount
                lots.append(
                    GridLot(
                        lot_id=lot_id,
                        level=level,
                        amount_token=StrategyStateStore._text(amount_token),
                        buy_amount_usdt=StrategyStateStore._text(buy_amount),
                        buy_price=StrategyStateStore._text(buy_price),
                        target_price=StrategyStateStore._text(target_price),
                        buy_tx_hash=buy_tx_hash,
                    )
                )
            else:
                try:
                    sell_amount = Decimal(str(raw["sell_amount_usdt"]))
                    sell_price = Decimal(str(raw["sell_price"]))
                    sell_tx_hash = str(raw["sell_tx_hash"])
                except (KeyError, InvalidOperation, TypeError, ValueError) as error:
                    raise StrategyGuardError(
                        "Closed grid lot lacks verified sell details; refusing to trade."
                    ) from error
                if (
                    not sell_tx_hash
                    or not sell_amount.is_finite()
                    or sell_amount <= 0
                    or not sell_price.is_finite()
                    or sell_price <= 0
                ):
                    raise StrategyGuardError(
                        "Closed grid lot lacks verified sell details; refusing to trade."
                    )
        legacy_filled = asset.get("filled_levels", [])
        if legacy_filled:
            try:
                if tuple(sorted(int(level) for level in legacy_filled)) != tuple(sorted(open_levels)):
                    raise StrategyGuardError(
                        "Grid filled levels do not match saved lots; refusing to trade."
                    )
            except (TypeError, ValueError) as error:
                raise StrategyGuardError(
                    "Grid filled levels are invalid; refusing to trade."
                ) from error
        if not spent.is_finite() or spent < 0 or spent > settings.grid_max_budget_usdt:
            raise StrategyGuardError("Grid state fails safety validation; refusing to trade.")
        try:
            realized = Decimal(str(asset.get("realized_usdt", "0")))
        except (InvalidOperation, TypeError, ValueError) as error:
            raise StrategyGuardError("Grid realized proceeds are invalid; refusing to trade.") from error
        if not realized.is_finite() or realized < 0:
            raise StrategyGuardError("Grid realized proceeds are invalid; refusing to trade.")
        last_sell = asset.get("last_sell_outcome")
        if last_sell is not None and (
            not isinstance(last_sell, dict)
            or last_sell.get("outcome") not in {"confirmed", "reverted"}
        ):
            raise StrategyGuardError("Grid sell outcome is invalid; refusing to trade.")
        if (
            not anchor.is_finite()
            or anchor <= 0
            or len(parsed_levels) != settings.grid_levels
            or any(not value.is_finite() or value <= 0 for value in parsed_levels)
        ):
            raise StrategyGuardError("Grid state fails safety validation; refusing to trade.")
        status = (
            "paused_at_floor"
            if asset.get("paused_at_floor") is True
            else "complete"
            if len(open_levels) == len(parsed_levels)
            else "profile_change_pending"
            if profile_change_pending
            else "active"
        )
        remaining = max(Decimal(0), settings.grid_max_budget_usdt - spent)
        return GridSnapshot(
            symbol=symbol,
            anchor_price=StrategyStateStore._text(anchor),
            levels=levels,
            filled_levels=tuple(sorted(open_levels)),
            spent_usdt=StrategyStateStore._text(spent),
            remaining_usdt=StrategyStateStore._text(remaining),
            status=status,
            open_lots=tuple(sorted(lots, key=lambda lot: lot.level)),
            realized_usdt=StrategyStateStore._text(realized),
            last_sell_outcome=dict(last_sell) if isinstance(last_sell, dict) else None,
        )

    def _write(self, state: dict[str, Any]) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as state_file:
            json.dump(state, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
            state_file.flush()
            os.fsync(state_file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def position(self, symbol: str) -> StrategyPosition | None:
        with self._locked():
            raw = self._read().get("positions", {}).get(symbol.upper())
        return (
            StrategyPosition(amount=str(raw["amount"]), entry_price=str(raw["entry_price"]))
            if raw
            else None
        )

    def claim(
        self,
        signal: StrategySignal,
        *,
        now: float,
        cooldown_seconds: int,
        max_trades_per_day: int,
        pre_trade_token_balance: int | None = None,
    ) -> None:
        with self._locked():
            state = self._read()
            if state.get("pending") is not None:
                raise StrategyGuardError(
                    "A strategy order is already pending; reconcile it before retrying."
                )
            last_trade_at = float(state.get("last_trade_at", 0))
            if now < last_trade_at or now - last_trade_at < cooldown_seconds:
                raise StrategyGuardError("Strategy cooldown is active; refusing another order.")
            recent = [
                trade for trade in state.get("trades", []) if now - float(trade["at"]) < 86_400
            ]
            if len(recent) >= max_trades_per_day:
                raise StrategyGuardError("Strategy daily trade limit has been reached.")
            positions = state.setdefault("positions", {})
            if signal.action == "buy" and signal.symbol in positions:
                raise StrategyGuardError(f"Strategy already has an open {signal.symbol} position.")
            if signal.action == "sell" and signal.symbol not in positions:
                raise StrategyGuardError(f"Strategy has no open {signal.symbol} position.")
            state["trades"] = recent
            state["pending"] = {
                "kind": "strategy",
                "fingerprint": signal.fingerprint,
                "action": signal.action,
                "symbol": signal.symbol,
                "amount": signal.amount,
                "price": signal.price,
                "claimed_at": now,
                "pre_trade_token_balance": pre_trade_token_balance,
                "base_asset": STRATEGY_BASE_ASSET,
                "output_symbol": signal.symbol if signal.action == "buy" else STRATEGY_BASE_ASSET,
            }
            self._write(state)

    @staticmethod
    def _manual_fingerprint(action: str, symbol: str, amount: str) -> str:
        return ":".join(("manual", action, symbol, amount))

    def claim_manual(
        self,
        action: str,
        symbol: str,
        amount: str,
        *,
        now: float,
        pre_trade_token_balance: int | None = None,
        expected_amount_out: str | None = None,
    ) -> str:
        fingerprint = self._manual_fingerprint(action, symbol, amount)
        with self._locked():
            state = self._read()
            if state.get("pending") is not None:
                raise StrategyGuardError(
                    "A live transaction is already pending; reconcile it before retrying."
                )
            state["pending"] = {
                "kind": "manual",
                "fingerprint": fingerprint,
                "action": action,
                "symbol": symbol,
                "amount": amount,
                "claimed_at": now,
                "pre_trade_token_balance": pre_trade_token_balance,
                "expected_amount_out": expected_amount_out,
                "base_asset": STRATEGY_BASE_ASSET,
                "output_symbol": symbol if action == "buy" else STRATEGY_BASE_ASSET,
            }
            self._write(state)
        return fingerprint

    def record_manual_attempt(self, fingerprint: str, tx_hash: str, phase: str = "swap") -> None:
        if phase not in {"approval", "swap"}:
            raise StrategyGuardError("Manual transaction phase is invalid.")
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "manual"
                or pending.get("fingerprint") != fingerprint
            ):
                raise StrategyGuardError(
                    "Manual transaction claim is missing; refusing to record attempt."
                )
            pending["tx_hash"] = tx_hash
            pending["phase"] = phase
            self._write(state)

    def release_manual(self, fingerprint: str) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                pending
                and pending.get("kind") == "manual"
                and pending.get("fingerprint") == fingerprint
            ):
                state["pending"] = None
                self._write(state)

    def confirm_manual(self, fingerprint: str, *, now: float) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "manual"
                or pending.get("fingerprint") != fingerprint
            ):
                raise StrategyGuardError(
                    "Manual transaction claim is missing; refusing to update state."
                )
            state["pending"] = None
            state.setdefault("manual_trades", []).append(
                {
                    "at": now,
                    "action": pending["action"],
                    "symbol": pending["symbol"],
                    "amount": pending["amount"],
                    "tx_hash": pending.get("tx_hash"),
                    "outcome": "confirmed",
                }
            )
            self._write(state)

    def reject_manual(self, fingerprint: str, *, now: float) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "manual"
                or pending.get("fingerprint") != fingerprint
            ):
                raise StrategyGuardError(
                    "Manual transaction claim is missing; refusing to update state."
                )
            state["pending"] = None
            state.setdefault("manual_trades", []).append(
                {
                    "at": now,
                    "action": pending["action"],
                    "symbol": pending["symbol"],
                    "amount": pending["amount"],
                    "tx_hash": pending.get("tx_hash"),
                    "outcome": "reverted",
                }
            )
            self._write(state)

    def clear_confirmed_manual_approval(self, fingerprint: str) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "manual"
                or pending.get("fingerprint") != fingerprint
                or pending.get("phase") != "approval"
            ):
                raise StrategyGuardError(
                    "Manual approval claim is missing; refusing to update state."
                )
            state["pending"] = None
            self._write(state)

    def record_attempt(self, signal: StrategySignal, tx_hash: str, phase: str = "swap") -> None:
        if phase not in {"approval", "swap"}:
            raise StrategyGuardError("Strategy transaction phase is invalid.")
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind", "strategy") != "strategy"
                or pending.get("fingerprint") != signal.fingerprint
            ):
                raise StrategyGuardError("Strategy order claim is missing; refusing to record attempt.")
            pending["tx_hash"] = tx_hash
            pending["phase"] = phase
            self._write(state)

    def record_broadcast(self, signal: StrategySignal, tx_hash: str) -> None:
        """Compatibility wrapper for recording a final swap transaction."""
        self.record_attempt(signal, tx_hash)

    def pending(self) -> dict[str, Any] | None:
        with self._locked():
            pending = self._read().get("pending")
        return dict(pending) if pending else None

    def pending_trade(self) -> PendingTrade | None:
        pending = self.pending()
        if pending is None:
            return None
        return PendingTrade(
            kind=str(pending.get("kind", "strategy")),
            action=str(pending.get("action", "unknown")),
            symbol=str(pending.get("symbol", "unknown")),
            amount=str(pending.get("amount", "unknown")),
            tx_hash=str(pending["tx_hash"]) if pending.get("tx_hash") else None,
            phase=str(pending["phase"]) if pending.get("phase") else None,
        )

    def read_only_exposure(self) -> JournalExposure:
        """Read the journal without upgrading, claiming, or writing any state."""
        with self._locked():
            state = self._read()
            raw_pending = state.get("pending")
            pending = (
                PendingTrade(
                    kind=str(raw_pending.get("kind", "strategy")),
                    action=str(raw_pending.get("action", "unknown")),
                    symbol=str(raw_pending.get("symbol", "unknown")),
                    amount=str(raw_pending.get("amount", "unknown")),
                    tx_hash=str(raw_pending["tx_hash"])
                    if raw_pending.get("tx_hash")
                    else None,
                    phase=str(raw_pending["phase"]) if raw_pending.get("phase") else None,
                )
                if isinstance(raw_pending, dict)
                else None
            )

            open_lots_by_symbol: dict[str, int] = {}
            open_exposure_by_symbol: dict[str, Decimal] = {}
            grid = state.get("grid")
            assets = grid.get("assets", {}) if isinstance(grid, dict) else {}
            if not isinstance(assets, dict):
                raise StrategyGuardError("Grid asset state is invalid; refusing to inspect it.")
            for symbol, asset in assets.items():
                if not isinstance(asset, dict):
                    raise StrategyGuardError("Grid asset state is invalid; refusing to inspect it.")
                lots = asset.get("lots", [])
                if not isinstance(lots, list):
                    raise StrategyGuardError("Grid lots are invalid; refusing to inspect them.")
                open_count = 0
                open_exposure = Decimal("0")
                for lot in lots:
                    if not isinstance(lot, dict) or lot.get("status") != "open":
                        continue
                    try:
                        amount = Decimal(str(lot["buy_amount_usdt"]))
                    except (KeyError, InvalidOperation, TypeError, ValueError) as error:
                        raise StrategyGuardError(
                            "Grid lot exposure is invalid; refusing to inspect it."
                        ) from error
                    if not amount.is_finite() or amount < 0:
                        raise StrategyGuardError(
                            "Grid lot exposure is invalid; refusing to inspect it."
                        )
                    open_count += 1
                    open_exposure += amount
                if open_count:
                    normalized = str(symbol).upper()
                    open_lots_by_symbol[normalized] = open_count
                    open_exposure_by_symbol[normalized] = open_exposure

            raw_positions = state.get("positions", {})
            if not isinstance(raw_positions, dict):
                raise StrategyGuardError(
                    "Strategy position state is invalid; refusing to inspect it."
                )
            strategy_positions = {
                str(symbol).upper(): str(position.get("amount", "unknown"))
                for symbol, position in raw_positions.items()
                if isinstance(position, dict)
            }
            return JournalExposure(
                pending=pending,
                open_lot_count=sum(open_lots_by_symbol.values()),
                open_lots_by_symbol=open_lots_by_symbol,
                open_exposure_usdt=sum(open_exposure_by_symbol.values(), Decimal("0")),
                open_exposure_by_symbol=open_exposure_by_symbol,
                strategy_positions=strategy_positions,
            )

    def confirmed_notifications(self) -> list[TradeResult]:
        with self._locked():
            pending = list(self._read()["confirmed_notifications"])
        results: list[TradeResult] = []
        for item in pending:
            if not isinstance(item, dict):
                raise StrategyGuardError("Strategy notification recovery state is invalid.")
            try:
                results.append(
                    TradeResult(
                        str(item["action"]),
                        str(item["symbol"]),
                        str(item["amount_in"]),
                        str(item["amount_out"]),
                        str(item["tx_hash"]),
                        int(item["block_number"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise StrategyGuardError(
                    "Strategy notification recovery state is invalid."
                ) from error
        return results

    def mark_notification_transferred(self, tx_hash: str) -> None:
        with self._locked():
            state = self._read()
            normalized = tx_hash.lower()
            state["confirmed_notifications"] = [
                item
                for item in state["confirmed_notifications"]
                if not isinstance(item, dict)
                or str(item.get("tx_hash", "")).lower() != normalized
            ]
            self._write(state)

    def release(self, signal: StrategySignal) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                pending
                and pending.get("kind", "strategy") == "strategy"
                and pending.get("fingerprint") == signal.fingerprint
            ):
                state["pending"] = None
                self._write(state)

    def confirm(self, signal: StrategySignal, result: TradeResult, *, now: float) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind", "strategy") != "strategy"
                or pending.get("fingerprint") != signal.fingerprint
            ):
                raise StrategyGuardError("Strategy order claim is missing; refusing to update state.")
            positions = state.setdefault("positions", {})
            if signal.action == "buy":
                positions[signal.symbol] = {
                    "amount": result.amount_out,
                    "entry_price": signal.price,
                }
            else:
                positions.pop(signal.symbol, None)
            state["pending"] = None
            state["last_trade_at"] = now
            state.setdefault("trades", []).append({"at": now, "fingerprint": signal.fingerprint})
            self._stage_confirmed_notification(state, result)
            self._write(state)

    def reject(self, signal: StrategySignal, *, now: float) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind", "strategy") != "strategy"
                or pending.get("fingerprint") != signal.fingerprint
            ):
                raise StrategyGuardError("Strategy order claim is missing; refusing to update state.")
            state["pending"] = None
            state["last_trade_at"] = now
            state.setdefault("trades", []).append(
                {"at": now, "fingerprint": signal.fingerprint, "outcome": "reverted"}
            )
            self._write(state)

    def clear_confirmed_approval(self, signal: StrategySignal) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind", "strategy") != "strategy"
                or pending.get("fingerprint") != signal.fingerprint
                or pending.get("phase") != "approval"
            ):
                raise StrategyGuardError("Approval claim is missing; refusing to update state.")
            state["pending"] = None
            self._write(state)

    def grid_decide(
        self,
        symbol: str,
        price: Decimal | str,
        settings: Settings,
        *,
        anchor_price: Decimal | str | None = None,
        levels: Sequence[Decimal | str] | None = None,
    ) -> tuple[GridSignal | None, GridSnapshot]:
        normalized = symbol.upper()
        if normalized not in settings.grid_symbols:
            raise StrategyGuardError(f"{normalized} is not enabled for this grid.")
        try:
            latest = Decimal(str(price))
        except (InvalidOperation, ValueError) as error:
            raise StrategyGuardError("Grid price must be a decimal number.") from error
        if not latest.is_finite() or latest <= 0:
            raise StrategyGuardError("Grid price must be positive and finite.")
        with self._locked():
            state = self._read()
            legacy_version = state.get("grid", {}).get("version") == 1
            grid = self._grid(state, settings)
            assets = grid["assets"]
            asset = assets.get(normalized)
            if asset is None:
                try:
                    anchor = latest if anchor_price is None else Decimal(str(anchor_price))
                    planned_levels = (
                        [Decimal(str(value)) for value in levels]
                        if levels is not None
                        else [
                            anchor
                            * Decimal(10_000 - settings.grid_spacing_bps * level)
                            / Decimal(10_000)
                            for level in range(1, settings.grid_levels + 1)
                        ]
                    )
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise StrategyGuardError(
                        "Dynamic grid anchor or levels are invalid."
                    ) from error
                if (
                    not anchor.is_finite()
                    or anchor <= 0
                    or len(planned_levels) != settings.grid_levels
                    or any(
                        not level.is_finite()
                        or level <= 0
                        or level >= anchor
                        for level in planned_levels
                    )
                    or any(
                        left <= right
                        for left, right in zip(planned_levels, planned_levels[1:])
                    )
                ):
                    raise StrategyGuardError("Dynamic grid anchor or levels are invalid.")
                asset = {
                    "anchor_price": self._text(anchor),
                    "levels": [self._text(level) for level in planned_levels],
                    "filled_levels": [],
                    "spent_usdt": "0",
                    "paused_at_floor": False,
                    "lots": [],
                    "realized_usdt": "0",
                    "last_sell_outcome": None,
                    "immediate_entry_claimed": False,
                }
                assets[normalized] = asset
                self._write(state)
            elif not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset state is invalid; refusing to trade.")
            elif "immediate_entry_claimed" not in asset:
                asset["immediate_entry_claimed"] = bool(asset.get("lots"))
                self._write(state)
            elif legacy_version:
                self._write(state)
            snapshot = self._grid_snapshot(
                normalized,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            if snapshot.status != "active":
                return None, snapshot
            if (
                settings.buy_immediately_now
                and not asset.get("immediate_entry_claimed", False)
                and not snapshot.filled_levels
            ):
                return (
                    GridSignal(
                        symbol=normalized,
                        level=1,
                        amount=self._text(settings.grid_order_usdt),
                        price=self._text(latest),
                        anchor_price=snapshot.anchor_price or "",
                        immediate=True,
                    ),
                    snapshot,
                )
            if latest < Decimal(snapshot.levels[-1]):
                asset["paused_at_floor"] = True
                self._write(state)
                return None, self._grid_snapshot(
                    normalized,
                    asset,
                    settings,
                    profile_change_pending=grid.get("config")
                    != self._grid_config(settings),
                )
            next_level = snapshot.next_level
            if next_level is None or latest > Decimal(snapshot.levels[next_level - 1]):
                return None, snapshot
            return (
                GridSignal(
                    symbol=normalized,
                    level=next_level,
                    amount=self._text(settings.grid_order_usdt),
                    price=self._text(latest),
                    anchor_price=snapshot.anchor_price or "",
                ),
                snapshot,
            )

    def grid_take_profit_decide(
        self,
        symbol: str,
        price: Decimal | str,
        settings: Settings,
        *,
        lot_id: str | None = None,
    ) -> tuple[GridTakeProfitSignal | None, GridSnapshot | None]:
        normalized = symbol.upper()
        if normalized not in settings.grid_symbols:
            raise StrategyGuardError(f"{normalized} is not enabled for this grid.")
        try:
            latest = Decimal(str(price))
        except (InvalidOperation, ValueError) as error:
            raise StrategyGuardError("Grid price must be a decimal number.") from error
        if not latest.is_finite() or latest <= 0:
            raise StrategyGuardError("Grid price must be positive and finite.")
        with self._locked():
            state = self._read()
            legacy_version = state.get("grid", {}).get("version") == 1
            grid = self._grid(state, settings)
            asset = grid["assets"].get(normalized)
            if legacy_version:
                self._write(state)
            if not isinstance(asset, dict):
                return None, None
            snapshot = self._grid_snapshot(
                normalized,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            for lot in snapshot.open_lots:
                if lot_id is not None and lot.lot_id != lot_id:
                    continue
                if latest < Decimal(lot.target_price):
                    continue
                return (
                    GridTakeProfitSignal(
                        symbol=normalized,
                        level=lot.level,
                        amount=lot.amount_token,
                        price=self._text(latest),
                        target_price=lot.target_price,
                        anchor_price=snapshot.anchor_price or "",
                        lot_id=lot.lot_id,
                    ),
                    snapshot,
                )
            return None, snapshot

    def grid_snapshot(self, symbol: str, settings: Settings) -> GridSnapshot | None:
        normalized = symbol.upper()
        with self._locked():
            state = self._read()
            legacy_version = state.get("grid", {}).get("version") == 1
            grid = self._grid(state, settings)
            if legacy_version:
                self._write(state)
            asset = grid["assets"].get(normalized)
            return (
                self._grid_snapshot(
                    normalized,
                    asset,
                    settings,
                    profile_change_pending=grid.get("config")
                    != self._grid_config(settings),
                )
                if asset
                else None
            )

    def claim_grid(
        self, signal: GridSignal, settings: Settings, *, pre_trade_token_balance: int
    ) -> None:
        with self._locked():
            state = self._read()
            if state.get("pending") is not None:
                raise StrategyGuardError(
                    "A live transaction is already pending; reconcile it before another grid buy."
                )
            grid = self._grid(state, settings)
            asset = grid["assets"].get(signal.symbol)
            if not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset has no saved anchor; refusing to trade.")
            snapshot = self._grid_snapshot(
                signal.symbol,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            if (
                snapshot.status != "active"
                or signal.level != snapshot.next_level
                or signal.anchor_price != snapshot.anchor_price
                or signal.amount != self._text(settings.grid_order_usdt)
            ):
                raise StrategyGuardError("Grid level changed before it could be claimed.")
            if not signal.immediate and Decimal(signal.price) > Decimal(
                snapshot.levels[signal.level - 1]
            ):
                raise StrategyGuardError("Grid price no longer reaches the claimed level.")
            next_spend = Decimal(snapshot.spent_usdt) + settings.grid_order_usdt
            if next_spend > settings.grid_max_budget_usdt:
                raise StrategyGuardError("Grid per-asset USDT budget has been reached.")
            if signal.immediate:
                if (
                    not isinstance(signal.immediate, bool)
                    or
                    not settings.buy_immediately_now
                    or asset.get("immediate_entry_claimed", False)
                    or snapshot.filled_levels
                ):
                    raise StrategyGuardError("Immediate Grid entry is no longer available.")
                asset["immediate_entry_claimed"] = True
            state["pending"] = {
                "kind": "grid",
                "fingerprint": signal.fingerprint,
                "action": "buy",
                "symbol": signal.symbol,
                "amount": signal.amount,
                "price": signal.price,
                "grid_level": signal.level,
                "grid_anchor_price": signal.anchor_price,
                "grid_immediate": signal.immediate,
                "grid_take_profit_bps": settings.grid_take_profit_bps,
                "claimed_at": time.time(),
                "pre_trade_token_balance": pre_trade_token_balance,
                "base_asset": STRATEGY_BASE_ASSET,
                "output_symbol": signal.symbol,
            }
            self._write(state)

    def claim_grid_take_profit(
        self,
        signal: GridTakeProfitSignal,
        settings: Settings,
        *,
        pre_trade_usdt_balance: int,
    ) -> None:
        with self._locked():
            state = self._read()
            if state.get("pending") is not None:
                raise StrategyGuardError(
                    "A live transaction is already pending; reconcile it before another grid sell."
                )
            grid = self._grid(state, settings)
            asset = grid["assets"].get(signal.symbol)
            if not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset has no saved lot; refusing to sell.")
            snapshot = self._grid_snapshot(
                signal.symbol,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            lot = next(
                (candidate for candidate in snapshot.open_lots if candidate.lot_id == signal.lot_id),
                None,
            )
            if (
                lot is None
                or lot.level != signal.level
                or lot.amount_token != signal.amount
                or lot.target_price != signal.target_price
                or signal.anchor_price != snapshot.anchor_price
                or Decimal(signal.price) < Decimal(lot.target_price)
            ):
                raise StrategyGuardError(
                    "Grid take-profit lot changed before it could be claimed."
                )
            if (
                not isinstance(pre_trade_usdt_balance, int)
                or isinstance(pre_trade_usdt_balance, bool)
                or pre_trade_usdt_balance < 0
            ):
                raise StrategyGuardError("Grid sell balance metadata is invalid.")
            state["pending"] = {
                "kind": "grid",
                "fingerprint": signal.fingerprint,
                "action": "sell",
                "symbol": signal.symbol,
                "amount": signal.amount,
                "price": signal.price,
                "grid_level": signal.level,
                "grid_anchor_price": signal.anchor_price,
                "grid_lot_id": signal.lot_id,
                "grid_target_price": signal.target_price,
                "claimed_at": time.time(),
                "pre_trade_token_balance": pre_trade_usdt_balance,
                "base_asset": STRATEGY_BASE_ASSET,
                "output_symbol": STRATEGY_BASE_ASSET,
            }
            self._write(state)

    def record_grid_attempt(
        self, signal: GridSignal | GridTakeProfitSignal, tx_hash: str, phase: str = "swap"
    ) -> None:
        if phase not in {"approval", "swap"}:
            raise StrategyGuardError("Grid transaction phase is invalid.")
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "grid"
                or pending.get("fingerprint") != signal.fingerprint
            ):
                raise StrategyGuardError("Grid order claim is missing; refusing to record attempt.")
            pending["tx_hash"] = tx_hash
            pending["phase"] = phase
            self._write(state)

    def release_grid(self, signal: GridSignal | GridTakeProfitSignal) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                pending
                and pending.get("kind") == "grid"
                and pending.get("fingerprint") == signal.fingerprint
            ):
                if pending.get("grid_immediate"):
                    grid = state.get("grid")
                    assets = grid.get("assets") if isinstance(grid, dict) else None
                    asset = (
                        assets.get(signal.symbol)
                        if isinstance(assets, dict)
                        else None
                    )
                    if isinstance(asset, dict):
                        asset["immediate_entry_claimed"] = False
                state["pending"] = None
                self._write(state)

    def confirm_grid(self, signal: GridSignal, result: TradeResult, settings: Settings) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "grid"
                or pending.get("fingerprint") != signal.fingerprint
                or bool(pending.get("grid_immediate", False)) != signal.immediate
            ):
                raise StrategyGuardError("Grid order claim is missing; refusing to update state.")
            grid = self._grid(state, settings)
            asset = grid["assets"].get(signal.symbol)
            if not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset is missing; refusing to update state.")
            snapshot = self._grid_snapshot(
                signal.symbol,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            if signal.level in snapshot.filled_levels:
                raise StrategyGuardError("Grid level was already filled; refusing duplicate spend.")
            try:
                buy_amount_usdt = Decimal(result.amount_in)
                amount_token = Decimal(result.amount_out)
            except (InvalidOperation, ValueError) as error:
                raise StrategyGuardError(
                    "Confirmed grid buy has invalid fill amounts; refusing to open a lot."
                ) from error
            if (
                result.action != "buy"
                or buy_amount_usdt <= 0
                or amount_token <= 0
                or not buy_amount_usdt.is_finite()
                or not amount_token.is_finite()
            ):
                raise StrategyGuardError(
                    "Confirmed grid buy has no usable fill; refusing to open a lot."
                )
            buy_price = buy_amount_usdt / amount_token
            target_bps = pending.get("grid_take_profit_bps")
            if target_bps is None:
                saved_config = grid.get("config")
                target_bps = (
                    saved_config.get(
                        "take_profit_bps", LEGACY_GRID_TAKE_PROFIT_BPS
                    )
                    if isinstance(saved_config, dict)
                    else LEGACY_GRID_TAKE_PROFIT_BPS
                )
            if (
                isinstance(target_bps, bool)
                or not isinstance(target_bps, int)
                or not (1 <= target_bps <= 10_000)
            ):
                raise StrategyGuardError(
                    "Pending Grid take-profit target is invalid; order remains blocked."
                )
            target_price = (
                buy_price
                * Decimal(10_000 + target_bps)
                / Decimal(10_000)
            )
            lot_id = f"{signal.symbol}:{signal.level}:{result.tx_hash}"
            asset["filled_levels"] = [*snapshot.filled_levels, signal.level]
            asset["spent_usdt"] = self._text(
                Decimal(snapshot.spent_usdt) + buy_amount_usdt
            )
            asset.setdefault("lots", []).append(
                {
                    "lot_id": lot_id,
                    "level": signal.level,
                    "amount_token": self._text(amount_token),
                    "buy_amount_usdt": self._text(buy_amount_usdt),
                    "buy_price": self._text(buy_price),
                    "target_price": self._text(target_price),
                    "buy_tx_hash": result.tx_hash,
                    "status": "open",
                    "opened_at": time.time(),
                }
            )
            asset["last_fill_tx"] = result.tx_hash
            state["pending"] = None
            self._stage_confirmed_notification(state, result)
            self._write(state)

    def confirm_grid_take_profit(
        self,
        signal: GridTakeProfitSignal,
        result: TradeResult,
        settings: Settings,
    ) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "grid"
                or pending.get("fingerprint") != signal.fingerprint
                or pending.get("action") != "sell"
            ):
                raise StrategyGuardError(
                    "Grid take-profit claim is missing; refusing to close a lot."
                )
            grid = self._grid(state, settings)
            asset = grid["assets"].get(signal.symbol)
            if not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset is missing; refusing to close a lot.")
            snapshot = self._grid_snapshot(
                signal.symbol,
                asset,
                settings,
                profile_change_pending=grid.get("config") != self._grid_config(settings),
            )
            if result.action != "sell" or result.amount_out == "0":
                raise StrategyGuardError(
                    "Confirmed grid sell has no usable USDT proceeds; lot remains open."
                )
            lots = asset.get("lots")
            if not isinstance(lots, list):
                raise StrategyGuardError("Grid lots are invalid; refusing to close a lot.")
            raw_lot = next(
                (
                    raw
                    for raw in lots
                    if isinstance(raw, dict)
                    and raw.get("lot_id") == signal.lot_id
                    and raw.get("status") == "open"
                ),
                None,
            )
            if raw_lot is None:
                raise StrategyGuardError(
                    "Grid take-profit lot is missing or already closed; refusing duplicate sale."
                )
            proceeds = Decimal(result.amount_out)
            if not proceeds.is_finite() or proceeds <= 0:
                raise StrategyGuardError(
                    "Confirmed grid sell proceeds are invalid; lot remains open."
                )
            raw_lot.update(
                {
                    "status": "closed",
                    "sell_tx_hash": result.tx_hash,
                    "sell_amount_usdt": self._text(proceeds),
                    "sell_price": self._text(
                        proceeds / Decimal(str(raw_lot["amount_token"]))
                    ),
                    "closed_at": time.time(),
                }
            )
            realized = Decimal(snapshot.realized_usdt) + proceeds
            asset["realized_usdt"] = self._text(realized)
            remaining_open = [
                raw
                for raw in lots
                if isinstance(raw, dict) and raw.get("status") == "open"
            ]
            asset["filled_levels"] = sorted(
                int(raw["level"]) for raw in remaining_open
            )
            asset["spent_usdt"] = self._text(
                sum(
                    (Decimal(str(raw["buy_amount_usdt"])) for raw in remaining_open),
                    Decimal(0),
                )
            )
            asset["last_sell_outcome"] = {
                "outcome": "confirmed",
                "level": signal.level,
                "amount_token": signal.amount,
                "proceeds_usdt": self._text(proceeds),
                "tx_hash": result.tx_hash,
            }
            state["pending"] = None
            self._stage_confirmed_notification(state, result)
            self._write(state)

    def reject_grid(self, signal: GridSignal) -> None:
        self.release_grid(signal)

    def reject_grid_take_profit(
        self, signal: GridTakeProfitSignal, settings: Settings
    ) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "grid"
                or pending.get("fingerprint") != signal.fingerprint
                or pending.get("action") != "sell"
            ):
                raise StrategyGuardError(
                    "Grid take-profit claim is missing; refusing to release it."
                )
            grid = self._grid(state, settings)
            asset = grid["assets"].get(signal.symbol)
            if not isinstance(asset, dict):
                raise StrategyGuardError("Grid asset is missing; refusing to release it.")
            asset["last_sell_outcome"] = {
                "outcome": "reverted",
                "level": signal.level,
                "amount_token": signal.amount,
                "proceeds_usdt": "0",
                "tx_hash": pending.get("tx_hash"),
            }
            state["pending"] = None
            self._write(state)

    def clear_confirmed_grid_approval(
        self, signal: GridSignal | GridTakeProfitSignal
    ) -> None:
        with self._locked():
            state = self._read()
            pending = state.get("pending")
            if (
                not pending
                or pending.get("kind") != "grid"
                or pending.get("fingerprint") != signal.fingerprint
                or pending.get("phase") != "approval"
            ):
                raise StrategyGuardError("Grid approval claim is missing; refusing to update state.")
            if pending.get("grid_immediate"):
                grid = state.get("grid")
                assets = grid.get("assets") if isinstance(grid, dict) else None
                asset = assets.get(signal.symbol) if isinstance(assets, dict) else None
                if isinstance(asset, dict):
                    asset["immediate_entry_claimed"] = False
            state["pending"] = None
            self._write(state)


class StrategyRules:
    """The paper strategy, kept pure so it can be evaluated without a wallet."""

    def __init__(self, settings: Settings):
        self.fast_window = settings.strategy_fast_window
        self.slow_window = settings.strategy_slow_window
        self.rsi_window = settings.strategy_rsi_window
        self.buy_rsi_max = settings.strategy_buy_rsi_max
        self.sell_rsi_min = settings.strategy_sell_rsi_min
        self.position_bps = settings.strategy_position_bps
        self.stop_loss_bps = settings.strategy_stop_loss_bps
        self.take_profit_bps = settings.strategy_take_profit_bps
        if self.fast_window >= self.slow_window:
            raise TraderError("STRATEGY_FAST_WINDOW must be less than STRATEGY_SLOW_WINDOW.")

    @staticmethod
    def _ema(values: Sequence[Decimal], window: int) -> list[Decimal]:
        multiplier = Decimal(2) / Decimal(window + 1)
        result = [values[0]]
        for value in values[1:]:
            result.append((value - result[-1]) * multiplier + result[-1])
        return result

    def _rsi(self, values: Sequence[Decimal]) -> Decimal:
        if len(values) <= self.rsi_window:
            raise TraderError("RSI requires at least one more price than its window.")
        changes = [
            values[index] - values[index - 1]
            for index in range(len(values) - self.rsi_window, len(values))
        ]
        if not changes:
            raise TraderError("RSI price history produced no changes to evaluate.")
        gains = sum((max(change, Decimal(0)) for change in changes), Decimal(0))
        losses = sum((max(-change, Decimal(0)) for change in changes), Decimal(0))
        if losses == 0:
            return Decimal(100) if gains > 0 else Decimal(50)
        if gains == 0:
            return Decimal(0)
        rsi = Decimal(100) - Decimal(100) / (1 + gains / losses)
        return max(Decimal(0), min(Decimal(100), rsi))

    def decide(
        self,
        symbol: str,
        prices: Sequence[Decimal | str],
        position: StrategyPosition | None,
        available_usdt: Decimal | str,
    ) -> StrategySignal | None:
        normalized_symbol = symbol.upper()
        if normalized_symbol not in {"XRP", "BTC"}:
            raise TraderError("Strategy supports XRP and BTC only.")
        try:
            values = [Decimal(str(price)) for price in prices]
            available = Decimal(str(available_usdt))
        except (InvalidOperation, ValueError) as error:
            raise TraderError("Strategy prices and balance must be decimal numbers.") from error
        if any(not value.is_finite() or value <= 0 for value in values) or available < 0:
            raise TraderError("Strategy prices must be positive and balance non-negative.")
        if len(values) < max(self.slow_window + 1, self.rsi_window + 1):
            raise TraderError("Strategy has too few prices to evaluate its rules.")
        fast, slow, rsi, latest = self._ema(values, self.fast_window), self._ema(
            values, self.slow_window
        ), self._rsi(values), values[-1]
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        text = lambda value: format(value.normalize(), "f")
        if position is None:
            if not crossed_up or rsi > self.buy_rsi_max:
                return None
            amount = (
                available * Decimal(self.position_bps) / Decimal(10_000)
            ).quantize(
                Decimal(1).scaleb(-TOKEN_DECIMALS[STRATEGY_BASE_ASSET]),
                rounding=ROUND_DOWN,
            )
            return (
                StrategySignal("buy", normalized_symbol, text(amount), text(latest), f"EMA/RSI {rsi:.2f}")
                if amount > 0
                else None
            )
        entry = Decimal(position.entry_price)
        stop = entry * Decimal(10_000 - self.stop_loss_bps) / Decimal(10_000)
        target = entry * Decimal(10_000 + self.take_profit_bps) / Decimal(10_000)
        if latest <= stop:
            reason = "stop-loss reached"
        elif latest >= target:
            reason = "take-profit reached"
        elif crossed_down and rsi >= self.sell_rsi_min:
            reason = f"EMA bearish crossover confirmed by RSI {rsi:.2f}"
        else:
            return None
        return StrategySignal("sell", normalized_symbol, position.amount, text(latest), reason)


class PancakeSwapTrader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.bsc_rpc_url, request_kwargs={"timeout": 30}))
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.account = Account.from_key(settings.wallet_private_key)
        if self.account.address != settings.wallet_address:
            raise TraderError("WALLET_ADDRESS does not match the address derived from WALLET_PRIVATE_KEY.")
        if not self.w3.is_connected():
            raise TraderError("Could not connect to BSC_RPC_URL.")
        if self.w3.eth.chain_id != BSC_CHAIN_ID:
            raise TraderError("RPC is not on BNB Smart Chain mainnet.")
        self.router: Contract = self.w3.eth.contract(address=PANCAKESWAP_V3_ROUTER, abi=V3_ROUTER_ABI)
        self.quoter: Contract = self.w3.eth.contract(address=PANCAKESWAP_V3_QUOTER_V2, abi=V3_QUOTER_V2_ABI)
        self.smart_router = SmartRouterBridge(settings.bsc_rpc_url)
        self._broadcast_attempted = False
        self._strategy_broadcast_callback: Any = None
        self._last_manual_reconciliation_status: str | None = None

    def _token(self, symbol: str) -> Contract:
        normalized = symbol.upper()
        if normalized not in TOKENS:
            raise TraderError(f"Unsupported token {symbol!r}.")
        return self.w3.eth.contract(address=TOKENS[normalized], abi=ERC20_ABI)

    def _strategy_target(self, symbol: str) -> str:
        normalized = symbol.upper()
        isolated = {
            "grid_bot_1": "BTC",
            "grid_bot_2": "XRP",
            "grid_bot_4": "WBNB",
            "grid_bot_5": "ETH",
            "grid_bot_6": "SOL",
        }.get(self.settings.isolated_grid_profile or "")
        scalper = {
            "scalper_bot_7": "WBNB",
            "scalper_bot_8": "ETH",
            "scalper_bot_9": "SOL",
            "scalper_bot_10": "XRP",
            "scalper_bot_11": "BTC",
        }.get(self.settings.isolated_scalper_profile or "")
        permitted = {isolated or scalper} if isolated or scalper else STRATEGY_TARGETS
        if normalized not in permitted:
            if isolated is None:
                raise TraderError("USDT strategy supports XRP and BTC only.")
            raise TraderError("Token is not permitted by this strategy profile.")
        return normalized

    def _amount_for_token(self, symbol: str, amount: str) -> tuple[int, int]:
        try:
            human = Decimal(amount)
        except InvalidOperation as error:
            raise TraderError("Trade amount must be a positive decimal number.") from error
        if human <= 0:
            raise TraderError("Trade amount must be greater than zero.")
        decimals = TOKEN_DECIMALS[symbol.upper()]
        scaled = human * Decimal(10) ** decimals
        if scaled != scaled.to_integral_value():
            raise TraderError(f"Trade amount has more than {decimals} decimal places.")
        return int(scaled), decimals

    def _amount_in_wei(self, action: str, symbol: str, amount: str) -> tuple[int, int]:
        if action == "buy":
            return self._amount_for_token(STRATEGY_BASE_ASSET, amount)
        if symbol.upper() == "WBNB" and (
            action != "sell"
            or (
                self.settings.isolated_grid_profile != "grid_bot_4"
                and self.settings.isolated_scalper_profile != "scalper_bot_7"
            )
        ):
            raise TraderError("WBNB is only a native sell target for grid_bot_4 or scalper_bot_7.")
        if symbol.upper() == STRATEGY_BASE_ASSET:
            raise TraderError(f"{symbol.upper()} is not a strategy sale target.")
        return self._amount_for_token(symbol, amount)

    @staticmethod
    def _encode_path(tokens: Iterable[str], fees: Iterable[int]) -> bytes:
        token_list, fee_list = list(tokens), list(fees)
        if len(token_list) != len(fee_list) + 1:
            raise TraderError("A V3 path needs one more token than fee tiers.")
        encoded = bytes.fromhex(token_list[0][2:])
        for fee, token in zip(fee_list, token_list[1:]):
            encoded += fee.to_bytes(3, "big") + bytes.fromhex(token[2:])
        return encoded

    def _routes_for_tokens(self, tokens: tuple[str, ...]) -> Iterable[V3Route]:
        for fees in product(V3_FEE_TIERS, repeat=len(tokens) - 1):
            yield V3Route(tokens, fees, self._encode_path(tokens, fees), 0)

    def _candidate_routes(self, action: str, symbol: str) -> Iterable[V3Route]:
        token = TOKENS[symbol.upper()]
        if action == "buy":
            paths = [(TOKENS[STRATEGY_BASE_ASSET], token)]
        else:
            paths = [(token, TOKENS[STRATEGY_BASE_ASSET])]
        for path in paths:
            yield from self._routes_for_tokens(path)

    def _quote_best(
        self,
        routes: Iterable[V3Route],
        amount_in: int,
        *,
        route_description: str = "requested route",
    ) -> V3Route:
        quotes: list[V3Route] = []
        for route in routes:
            try:
                amount_out = int(self.quoter.functions.quoteExactInput(route.encoded_path, amount_in).call()[0])
                if amount_out:
                    quotes.append(V3Route(route.tokens, route.fees, route.encoded_path, amount_out))
            except (BadFunctionCallOutput, ContractLogicError, ValueError):
                continue
        if not quotes:
            tiers = ", ".join(str(fee) for fee in V3_FEE_TIERS)
            raise TraderError(
                f"Unsupported PancakeSwap V3 liquidity for {route_description} "
                f"across fee tiers {tiers}."
            )
        return max(quotes, key=lambda route: route.amount_out)

    def _route_and_quote(self, action: str, symbol: str, amount_in: int) -> V3Route:
        return self._quote_best(self._candidate_routes(action, symbol), amount_in)

    @staticmethod
    def _route_symbols(route: V3Route) -> V3Route:
        symbols = {address: name for name, address in TOKENS.items()} | {
            address: name for name, address in ROUTING_TOKENS.items()
        }
        return V3Route(tuple(symbols.get(token, token) for token in route.tokens), route.fees, route.encoded_path, route.amount_out)

    def _minimum_output(self, quoted: int) -> int:
        return quoted * (10_000 - self.settings.slippage_bps) // 10_000

    def quote(self, action: str, symbol: str, amount: str) -> dict[str, str | int]:
        action, symbol = action.lower(), symbol.upper()
        if action not in {"buy", "sell"}:
            raise TraderError("Action must be buy or sell.")
        self._strategy_target(symbol)
        amount_in, input_decimals = self._amount_in_wei(action, symbol, amount)
        route = self._route_and_quote(action, symbol, amount_in)
        output_decimals = (
            TOKEN_DECIMALS[symbol] if action == "buy" else TOKEN_DECIMALS[STRATEGY_BASE_ASSET]
        )
        return self._quote_response(
            action, symbol, amount_in, input_decimals, route, output_decimals
        )

    def quote_pair(self, input_symbol: str, output_symbol: str, amount: str) -> dict[str, str | int]:
        input_symbol, output_symbol = input_symbol.upper(), output_symbol.upper()
        if {input_symbol, output_symbol} != {"XRP", "BTC"}:
            raise TraderError("Pair quotes support XRP and BTC only.")
        amount_in, input_decimals = self._amount_for_token(input_symbol, amount)
        route = self._quote_best(
            self._routes_for_tokens(
                (TOKENS[input_symbol], TOKENS[STRATEGY_BASE_ASSET], TOKENS[output_symbol])
            ),
            amount_in,
            route_description=f"{input_symbol} -> USDT -> {output_symbol}",
        )
        return self._quote_response(
            "pair",
            f"{input_symbol}/{output_symbol}",
            amount_in,
            input_decimals,
            route,
            TOKEN_DECIMALS[output_symbol],
        )

    def _smart_router_quote_response(
        self,
        action: str,
        symbol: str,
        amount_in: int,
        input_decimals: int,
        quote: Any,
        output_decimals: int,
    ) -> dict[str, str | int]:
        return {
            "action": action,
            "symbol": symbol,
            "amountIn": str(Decimal(amount_in) / Decimal(10) ** input_decimals),
            "quotedOut": str(Decimal(quote.amount_out) / Decimal(10) ** output_decimals),
            "minimumOut": str(
                Decimal(self._minimum_output(quote.amount_out)) / Decimal(10) ** output_decimals
            ),
            "slippageBps": self.settings.slippage_bps,
            "route": (
                "PancakeSwap factory price query "
                f"(V2={quote.v2_candidates}, V3={quote.v3_candidates}, "
                f"stable={quote.stable_candidates}, routes={quote.route_count})"
            ),
        }

    def _quote_response(self, action: str, symbol: str, amount_in: int, input_decimals: int, route: V3Route, output_decimals: int) -> dict[str, str | int]:
        return {
            "action": action,
            "symbol": symbol,
            "amountIn": str(Decimal(amount_in) / Decimal(10) ** input_decimals),
            "quotedOut": str(Decimal(route.amount_out) / Decimal(10) ** output_decimals),
            "minimumOut": str(Decimal(self._minimum_output(route.amount_out)) / Decimal(10) ** output_decimals),
            "slippageBps": self.settings.slippage_bps,
            "route": self._route_symbols(route).display(),
        }

    def _base_transaction(self, nonce: int) -> dict[str, Any]:
        return {"from": self.account.address, "nonce": nonce, "chainId": BSC_CHAIN_ID, "gasPrice": self.w3.eth.gas_price}

    def _estimate_gas(self, function: Any, transaction: dict[str, Any]) -> int:
        buffered = int(function.estimate_gas(transaction)) * 120 // 100
        if buffered > self.settings.max_gas_limit:
            raise TraderError(f"Estimated gas {buffered} exceeds MAX_GAS_LIMIT.")
        return buffered

    def _estimate_raw_gas(self, transaction: dict[str, Any]) -> int:
        buffered = int(self.w3.eth.estimate_gas(transaction)) * 120 // 100
        if buffered > self.settings.max_gas_limit:
            raise TraderError(f"Estimated gas {buffered} exceeds MAX_GAS_LIMIT.")
        return buffered

    def _require_native_gas(self, transaction: dict[str, Any]) -> None:
        """Allow native BNB only for the signed transaction's estimated gas."""
        if int(transaction.get("value", 0)) != 0:
            raise TraderError("USDT strategy transactions must not send native BNB value.")
        required = int(transaction["gas"]) * int(transaction["gasPrice"])
        available = int(self.w3.eth.get_balance(self.account.address))
        if available < required:
            raise TraderError(
                "Insufficient native BNB to cover the estimated transaction gas."
            )

    @staticmethod
    def _hex(value: Any) -> str:
        return value if isinstance(value, str) else Web3.to_hex(value)

    def _native_proceeds(self, receipt: Any, *, expected_source: str | None = None) -> int:
        """Read a receipt-verified WBNB Withdrawal from the trusted source."""
        proceeds = 0
        source = (expected_source or PANCAKESWAP_SMART_ROUTER).removeprefix("0x").lower()
        try:
            for log in receipt["logs"]:
                if str(log["address"]).lower() != TOKENS["WBNB"].lower():
                    continue
                topics = log["topics"]
                if (
                    self._hex(topics[0]).lower() != WBNB_WITHDRAWAL_TOPIC.lower()
                    or not self._hex(topics[1]).lower().endswith(source)
                ):
                    continue
                proceeds += int(self._hex(log["data"]), 16)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise StrategyGuardError(
                "Could not establish confirmed native sell proceeds; it remains blocked."
            ) from error
        if proceeds <= 0:
            raise StrategyGuardError(
                "The confirmed sell receipt has no Smart Router native withdrawal; it remains blocked."
            )
        return proceeds

    def _send_and_confirm(self, transaction: dict[str, Any], phase: str = "swap") -> Any:
        signed = self.account.sign_transaction(transaction)
        if self._strategy_broadcast_callback:
            self._strategy_broadcast_callback(signed.hash.hex(), phase)
        self._broadcast_attempted = True
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.settings.receipt_timeout_seconds)
        if receipt["status"] != 1:
            raise TransactionReverted(f"Blockchain transaction reverted: {tx_hash.hex()}.")
        return receipt

    @contextmanager
    def _nonce_locked(self) -> Iterator[None]:
        """Serialize nonce-sensitive sends for one public wallet across processes.

        This is intentionally the only shared scalper coordination primitive:
        bot state, claims, prices, notifications, and logs remain per-bot.
        """
        import fcntl

        address = self.account.address.lower()
        path = Path.home() / ".local" / "state" / "bnb-defi-bot" / "nonces" / f"{address}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as lock:
            os.chmod(path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _approve_if_needed(self, token: Contract, amount: int, spender: str) -> None:
        if int(token.functions.allowance(self.account.address, spender).call()) >= amount:
            return
        function = token.functions.approve(spender, amount)
        base = self._base_transaction(self.w3.eth.get_transaction_count(self.account.address, "pending"))
        transaction = function.build_transaction({**base, "gas": self._estimate_gas(function, base)})
        self._require_native_gas(transaction)
        self._send_and_confirm(transaction, "approval")

    def _build_smart_router_trade(
        self, action: str, symbol: str, amount_in: int, deadline: int
    ) -> SmartRouterTrade:
        input_symbol, output_symbol = (
            (STRATEGY_BASE_ASSET, symbol)
            if action == "buy"
            else (symbol, STRATEGY_BASE_ASSET)
        )
        try:
            trade = self.smart_router.build_trade(
                input_symbol,
                output_symbol,
                amount_in,
                recipient=self.account.address,
                slippage_bps=self.settings.slippage_bps,
                deadline=deadline,
                native_input=symbol == "WBNB" and action == "sell",
                native_output=symbol == "WBNB" and action == "buy",
                direct_v3=symbol in STRATEGY_TARGETS,
            )
        except SmartRouterError as error:
            raise TraderError(str(error)) from error
        if trade.router_address != PANCAKESWAP_SMART_ROUTER:
            raise TraderError("Smart Router returned an unexpected BNB Chain router address.")
        if symbol != "WBNB" and trade.value != 0:
            raise TraderError("USDT strategy routes must not send native BNB value.")
        if symbol in STRATEGY_TARGETS and (trade.v2_candidates or trade.stable_candidates or trade.v3_candidates <= 0):
            raise TraderError("Strategy route must be a direct PancakeSwap V3 USDT pool.")
        if trade.minimum_out <= 0:
            raise TraderError(
                "Smart Router minimum output is zero; refusing to submit an unprotected trade."
            )
        required_minimum = getattr(self, "_required_grid_minimum_out", 0)
        if required_minimum and trade.minimum_out < required_minimum:
            raise StrategyGuardError(
                "Smart Router minimum output is below the saved grid take-profit target."
            )
        return trade

    def execute(
        self,
        action: str,
        symbol: str,
        amount: str,
        *,
        confirmation_callback: Callable[[TradeResult], None] | None = None,
    ) -> TradeResult:
        if not self.settings.enable_live_trading:
            raise LiveTradingDisabled("Live execution is disabled. Set ENABLE_LIVE_TRADING=true only after review.")
        action, symbol = action.lower(), symbol.upper()
        if action not in {"buy", "sell"}:
            raise TraderError("Action must be buy or sell.")
        self._strategy_target(symbol)
        self._broadcast_attempted = False
        amount_in, input_decimals = self._amount_in_wei(action, symbol, amount)
        deadline = int(self.w3.eth.get_block("latest")["timestamp"]) + self.settings.deadline_seconds
        trade = self._build_smart_router_trade(action, symbol, amount_in, deadline)
        token = self._token(symbol)
        input_symbol, output_symbol = (
            (STRATEGY_BASE_ASSET, symbol)
            if action == "buy"
            else (symbol, STRATEGY_BASE_ASSET)
        )
        input_token, output_token = self._token(input_symbol), self._token(output_symbol)
        before = (
            0 if symbol == "WBNB" and action == "buy"
            else int(output_token.functions.balanceOf(self.account.address).call())
        )
        output_decimals = TOKEN_DECIMALS[output_symbol]
        if action == "buy":
            if int(input_token.functions.balanceOf(self.account.address).call()) < amount_in:
                raise TraderError("Insufficient USDT balance for the requested trade.")
        elif symbol != "WBNB":
            if int(token.functions.balanceOf(self.account.address).call()) < amount_in:
                raise TraderError(f"Insufficient {symbol} balance for the requested trade.")
        else:
            # Native input is sent as transaction value; never approve WBNB.
            gas_reserve = self.settings.max_gas_limit * int(self.w3.eth.gas_price)
            if int(self.w3.eth.get_balance(self.account.address)) < amount_in + gas_reserve:
                raise TraderError("Insufficient native BNB after required gas reserve.")

        # Strategy execution installs its own callback and owns its claim. A
        # direct/manual execution installs the equivalent callback here, before
        # any approval or swap can be handed to the RPC.
        state: StrategyStateStore | None = None
        manual_fingerprint: str | None = None
        previous_callback = self._strategy_broadcast_callback
        if previous_callback is None:
            state = StrategyStateStore(self.settings.strategy_state_file)
            expected_amount_out = str(
                Decimal(trade.amount_out) / Decimal(10) ** output_decimals
            )
            manual_fingerprint = state.claim_manual(
                action,
                symbol,
                amount,
                now=time.time(),
                pre_trade_token_balance=before,
                expected_amount_out=expected_amount_out,
            )
            self._strategy_broadcast_callback = (
                lambda tx_hash, phase: state.record_manual_attempt(
                    manual_fingerprint, tx_hash, phase
                )
            )

        try:
            # Keep approval nonce allocation, swap nonce allocation, broadcast,
            # and receipt wait together: another process sharing this wallet
            # cannot obtain the same pending nonce in between.
            with self._nonce_locked():
                if symbol != "WBNB" or action == "buy":
                    self._approve_if_needed(input_token, amount_in, trade.router_address)
                base = self._base_transaction(
                    self.w3.eth.get_transaction_count(self.account.address, "pending")
                )
                base.update(
                    {"to": trade.router_address, "data": trade.calldata, "value": trade.value}
                )
                transaction = {**base, "gas": self._estimate_raw_gas(base)}
                if symbol == "WBNB" and action == "sell":
                    required = int(transaction["gas"]) * int(transaction["gasPrice"]) + trade.value
                    if int(self.w3.eth.get_balance(self.account.address)) < required:
                        raise TraderError("Insufficient native BNB after estimated gas reserve.")
                else:
                    self._require_native_gas(transaction)
                receipt = self._send_and_confirm(transaction)
                actual_out = (
                    self._native_proceeds(receipt, expected_source=PANCAKESWAP_SMART_ROUTER)
                    if symbol == "WBNB" and action == "buy"
                    else int(output_token.functions.balanceOf(self.account.address).call()) - before
                )
                if actual_out <= 0:
                    raise StrategyGuardError(
                        "Could not confirm the USDT strategy output; order remains blocked."
                    )

                result = TradeResult(
                    action,
                    symbol,
                    str(Decimal(amount_in) / Decimal(10) ** input_decimals),
                    str(Decimal(actual_out) / Decimal(10) ** output_decimals),
                    receipt["transactionHash"].hex(),
                    int(receipt["blockNumber"]),
                )
        except TransactionReverted:
            # A receipt with status 0 is definitive, so it is safe to release
            # a manual claim immediately. Strategy claims intentionally retain
            # their existing reconciliation behavior.
            if state is not None and manual_fingerprint is not None:
                state.reject_manual(manual_fingerprint, now=time.time())
            raise
        except Exception:
            # The callback has already persisted the signed hash before the
            # send call. Any later exception is therefore potentially a
            # broadcast and must remain blocked.
            if (
                state is not None
                and manual_fingerprint is not None
                and not self._broadcast_attempted
            ):
                state.release_manual(manual_fingerprint)
            raise
        finally:
            if state is not None:
                self._strategy_broadcast_callback = previous_callback

        # A caller with its own durable strategy journal must finalize that
        # journal before this manual claim is cleared. If its write fails, the
        # pending transaction remains available for receipt reconciliation.
        if confirmation_callback is not None:
            confirmation_callback(result)
        if state is not None and manual_fingerprint is not None:
            state.confirm_manual(manual_fingerprint, now=time.time())
        return result

    def execute_strategy(self, symbol: str, prices: Sequence[Decimal | str], *, available_usdt: Decimal | str | None = None, now: float | None = None) -> TradeResult | None:
        if self.settings.live_strategy_mode != "ema":
            raise StrategyGuardError("EMA/RSI execution is paused while live grid mode is selected.")
        if not self.settings.enable_live_trading:
            raise LiveTradingDisabled("Live execution is disabled. Set ENABLE_LIVE_TRADING=true only after review.")
        timestamp, state = (time.time() if now is None else now), StrategyStateStore(self.settings.strategy_state_file)
        if available_usdt is None:
            usdt = self._token(STRATEGY_BASE_ASSET)
            available_usdt = Decimal(usdt.functions.balanceOf(self.account.address).call()) / Decimal(
                10**TOKEN_DECIMALS[STRATEGY_BASE_ASSET]
            )
        signal = StrategyRules(self.settings).decide(symbol, prices, state.position(symbol), available_usdt)
        if signal is None:
            return None
        output_symbol = signal.symbol if signal.action == "buy" else STRATEGY_BASE_ASSET
        before = int(self._token(output_symbol).functions.balanceOf(self.account.address).call())
        state.claim(signal, now=timestamp, cooldown_seconds=self.settings.strategy_cooldown_seconds, max_trades_per_day=self.settings.strategy_max_trades_per_day, pre_trade_token_balance=before)
        self._strategy_broadcast_callback = lambda tx_hash, phase: state.record_attempt(signal, tx_hash, phase)
        try:
            result = self.execute(signal.action, signal.symbol, signal.amount)
        except Exception:
            if not self._broadcast_attempted:
                state.release(signal)
            raise
        finally:
            self._strategy_broadcast_callback = None
        state.confirm(signal, result, now=timestamp)
        return result

    def grid_decide(
        self,
        symbol: str,
        price: Decimal | str,
        *,
        anchor_price: Decimal | str | None = None,
        levels: Sequence[Decimal | str] | None = None,
    ) -> tuple[GridSignal | None, GridSnapshot]:
        return StrategyStateStore(self.settings.strategy_state_file).grid_decide(
            symbol,
            price,
            self.settings,
            anchor_price=anchor_price,
            levels=levels,
        )

    def grid_snapshot(self, symbol: str) -> GridSnapshot | None:
        return StrategyStateStore(self.settings.strategy_state_file).grid_snapshot(
            symbol, self.settings
        )

    def grid_take_profit_decide(
        self, symbol: str, price: Decimal | str, *, lot_id: str | None = None
    ) -> tuple[GridTakeProfitSignal | None, GridSnapshot | None]:
        return StrategyStateStore(self.settings.strategy_state_file).grid_take_profit_decide(
            symbol, price, self.settings, lot_id=lot_id
        )

    def execute_grid(
        self,
        signal: GridSignal,
        *,
        available_usdt: Decimal | str | None = None,
    ) -> TradeResult:
        if self.settings.live_strategy_mode != "grid":
            raise StrategyGuardError("Grid execution requires LIVE_STRATEGY_MODE=grid.")
        if not self.settings.enable_live_trading:
            raise LiveTradingDisabled(
                "Live execution is disabled. Set ENABLE_LIVE_TRADING=true only after review."
            )
        if available_usdt is None:
            usdt = self._token(STRATEGY_BASE_ASSET)
            available_usdt = Decimal(usdt.functions.balanceOf(self.account.address).call()) / Decimal(
                10**TOKEN_DECIMALS[STRATEGY_BASE_ASSET]
            )
        if Decimal(str(available_usdt)) < Decimal(signal.amount):
            raise StrategyGuardError("Insufficient USDT balance for the requested grid level.")
        state = StrategyStateStore(self.settings.strategy_state_file)
        # A WBNB grid buy is deliberately delivered as native BNB. Its receipt
        # Withdrawal event, not a volatile wallet balance delta, proves the fill.
        before = (
            0
            if signal.symbol == "WBNB"
            else int(self._token(signal.symbol).functions.balanceOf(self.account.address).call())
        )
        state.claim_grid(signal, self.settings, pre_trade_token_balance=before)
        self._strategy_broadcast_callback = lambda tx_hash, phase: state.record_grid_attempt(
            signal, tx_hash, phase
        )
        try:
            result = self.execute("buy", signal.symbol, signal.amount)
        except TransactionReverted:
            state.reject_grid(signal)
            raise
        except Exception:
            if not self._broadcast_attempted:
                state.release_grid(signal)
            raise
        finally:
            self._strategy_broadcast_callback = None
        state.confirm_grid(signal, result, self.settings)
        return result

    def execute_grid_take_profit(
        self, signal: GridTakeProfitSignal
    ) -> TradeResult:
        if self.settings.live_strategy_mode != "grid":
            raise StrategyGuardError("Grid execution requires LIVE_STRATEGY_MODE=grid.")
        if not self.settings.enable_live_trading:
            raise LiveTradingDisabled(
                "Live execution is disabled. Set ENABLE_LIVE_TRADING=true only after review."
            )
        state = StrategyStateStore(self.settings.strategy_state_file)
        usdt = self._token(STRATEGY_BASE_ASSET)
        before = int(usdt.functions.balanceOf(self.account.address).call())
        state.claim_grid_take_profit(
            signal,
            self.settings,
            pre_trade_usdt_balance=before,
        )
        self._strategy_broadcast_callback = lambda tx_hash, phase: state.record_grid_attempt(
            signal, tx_hash, phase
        )
        try:
            try:
                amount = Decimal(signal.amount)
                target = Decimal(signal.target_price)
                required = (amount * target * Decimal(10**TOKEN_DECIMALS["USDT"])).to_integral_value(
                    rounding=ROUND_CEILING
                )
            except (InvalidOperation, ValueError) as error:
                raise StrategyGuardError("Grid take-profit target has invalid numeric values.") from error
            if not amount.is_finite() or not target.is_finite() or amount <= 0 or target <= 0 or required <= 0:
                raise StrategyGuardError("Grid take-profit target has invalid numeric values.")
            self._required_grid_minimum_out = int(required)
            result = self.execute("sell", signal.symbol, signal.amount)
        except TransactionReverted:
            state.reject_grid_take_profit(signal, self.settings)
            raise
        except Exception:
            if not self._broadcast_attempted:
                state.release_grid(signal)
            raise
        finally:
            self._required_grid_minimum_out = 0
            self._strategy_broadcast_callback = None
        state.confirm_grid_take_profit(signal, result, self.settings)
        return result

    def inspect_pending(self) -> PendingTrade | None:
        """Return the saved pending claim without contacting the chain or changing state."""
        return StrategyStateStore(self.settings.strategy_state_file).pending_trade()

    def confirmed_notifications(self) -> list[TradeResult]:
        return StrategyStateStore(
            self.settings.strategy_state_file
        ).confirmed_notifications()

    def mark_notification_transferred(self, tx_hash: str) -> None:
        StrategyStateStore(
            self.settings.strategy_state_file
        ).mark_notification_transferred(tx_hash)

    def reconcile_strategy(self, *, now: float | None = None) -> TradeResult | None:
        state, pending = StrategyStateStore(self.settings.strategy_state_file), StrategyStateStore(self.settings.strategy_state_file).pending()
        if pending is None:
            return None
        if pending.get("kind", "strategy") != "strategy":
            raise StrategyGuardError(
                "A non-EMA transaction is pending; reconcile it before EMA strategy orders."
            )
        if not (tx_hash := pending.get("tx_hash")):
            raise StrategyGuardError("The pending strategy order has no transaction hash; it remains blocked.")
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        except ValueError as error:
            raise TraderError("Could not fetch the pending strategy receipt.") from error
        signal = StrategySignal(str(pending["action"]), str(pending["symbol"]), str(pending["amount"]), str(pending["price"]), "receipt reconciliation")
        timestamp = time.time() if now is None else now
        if pending.get("phase") == "approval" and receipt["status"] == 1:
            state.clear_confirmed_approval(signal)
            return None
        if receipt["status"] != 1:
            state.reject(signal, now=timestamp)
            return None
        if pending.get("base_asset") != STRATEGY_BASE_ASSET:
            raise StrategyGuardError("Pending strategy order is not USDT-denominated; it remains blocked.")
        previous = pending.get("pre_trade_token_balance")
        output_symbol = str(pending.get("output_symbol", ""))
        if previous is None or output_symbol not in TOKENS:
            raise StrategyGuardError("The pending USDT order lacks output-balance metadata; it remains blocked.")
        token = self._token(output_symbol)
        received = (
            self._native_proceeds(receipt, expected_source=PANCAKESWAP_SMART_ROUTER)
            if output_symbol == "WBNB" and signal.action == "buy"
            else int(token.functions.balanceOf(self.account.address).call()) - int(previous)
        )
        if received <= 0:
            raise StrategyGuardError("Could not confirm the received output amount; order remains blocked.")
        amount_out = str(Decimal(received) / Decimal(10) ** int(token.functions.decimals().call()))
        result = TradeResult(signal.action, signal.symbol, signal.amount, amount_out, tx_hash, int(receipt["blockNumber"]))
        state.confirm(signal, result, now=timestamp)
        return result

    def reconcile_grid(self) -> TradeResult | None:
        state = StrategyStateStore(self.settings.strategy_state_file)
        pending = state.pending()
        if pending is None:
            return None
        if pending.get("kind") != "grid":
            raise StrategyGuardError(
                "A non-grid transaction is pending; reconcile it before grid orders."
            )
        if not (tx_hash := pending.get("tx_hash")):
            raise StrategyGuardError(
                "The pending grid order has no transaction hash; it remains blocked."
            )
        action = pending.get("action")
        if action not in {"buy", "sell"}:
            raise StrategyGuardError("Pending grid action is invalid; it remains blocked.")
        if action == "sell":
            signal: GridTakeProfitSignal | GridSignal = GridTakeProfitSignal(
                symbol=str(pending["symbol"]),
                level=int(pending["grid_level"]),
                amount=str(pending["amount"]),
                price=str(pending["price"]),
                target_price=str(pending["grid_target_price"]),
                anchor_price=str(pending["grid_anchor_price"]),
                lot_id=str(pending["grid_lot_id"]),
            )
        else:
            immediate = pending.get("grid_immediate", False)
            if not isinstance(immediate, bool):
                raise StrategyGuardError(
                    "Pending Grid immediate-entry state is invalid; it remains blocked."
                )
            signal = GridSignal(
                symbol=str(pending["symbol"]),
                level=int(pending["grid_level"]),
                amount=str(pending["amount"]),
                price=str(pending["price"]),
                anchor_price=str(pending["grid_anchor_price"]),
                immediate=immediate,
            )
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            return None
        except ValueError as error:
            raise TraderError("Could not fetch the pending grid receipt.") from error
        if pending.get("phase") == "approval" and receipt["status"] == 1:
            state.clear_confirmed_grid_approval(signal)
            return None
        if receipt["status"] != 1:
            if isinstance(signal, GridTakeProfitSignal):
                state.reject_grid_take_profit(signal, self.settings)
            else:
                state.reject_grid(signal)
            return None
        if pending.get("base_asset") != STRATEGY_BASE_ASSET:
            raise StrategyGuardError("Pending grid order is not USDT-denominated; it remains blocked.")
        previous = pending.get("pre_trade_token_balance")
        output_symbol = str(pending.get("output_symbol", ""))
        if previous is None or output_symbol not in TOKENS:
            raise StrategyGuardError(
                "The pending grid order lacks output-balance metadata; it remains blocked."
            )
        received = (
            self._native_proceeds(receipt, expected_source=PANCAKESWAP_SMART_ROUTER)
            if output_symbol == "WBNB" and action == "buy"
            else int(self._token(output_symbol).functions.balanceOf(self.account.address).call()) - int(previous)
        )
        if received <= 0:
            raise StrategyGuardError(
                "Could not confirm the received grid output amount; order remains blocked."
            )
        output_decimals = TOKEN_DECIMALS["WBNB"] if output_symbol == "WBNB" else int(
            self._token(output_symbol).functions.decimals().call()
        )
        amount_out = str(Decimal(received) / Decimal(10) ** output_decimals)
        result = TradeResult(
            "sell" if isinstance(signal, GridTakeProfitSignal) else "buy",
            signal.symbol,
            signal.amount,
            amount_out,
            tx_hash,
            int(receipt["blockNumber"]),
        )
        if isinstance(signal, GridTakeProfitSignal):
            state.confirm_grid_take_profit(signal, result, self.settings)
        else:
            state.confirm_grid(signal, result, self.settings)
        return result

    def reconcile_manual(
        self,
        *,
        now: float | None = None,
        confirmation_callback: Callable[[TradeResult], None] | None = None,
    ) -> TradeResult | None:
        """Resolve a manual transaction that may have been broadcast."""
        state = StrategyStateStore(self.settings.strategy_state_file)
        pending = state.pending()
        self._last_manual_reconciliation_status = "none"
        if pending is None:
            return None
        if pending.get("kind") != "manual":
            raise StrategyGuardError(
                "A strategy order is pending; reconcile it before manual trades."
            )
        if not (tx_hash := pending.get("tx_hash")):
            self._last_manual_reconciliation_status = "blocked"
            raise StrategyGuardError(
                "The pending manual transaction has no transaction hash; it remains blocked."
            )
        try:
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            self._last_manual_reconciliation_status = "pending"
            return None
        except ValueError as error:
            raise TraderError("Could not fetch the pending manual receipt.") from error

        fingerprint = str(pending["fingerprint"])
        timestamp = time.time() if now is None else now
        if pending.get("phase") == "approval" and receipt["status"] == 1:
            state.clear_confirmed_manual_approval(fingerprint)
            self._last_manual_reconciliation_status = "approval_confirmed"
            return None
        if receipt["status"] != 1:
            state.reject_manual(fingerprint, now=timestamp)
            self._last_manual_reconciliation_status = "reverted"
            return None

        action, symbol, amount = (
            str(pending["action"]),
            str(pending["symbol"]),
            str(pending["amount"]),
        )
        if pending.get("base_asset") != STRATEGY_BASE_ASSET:
            self._last_manual_reconciliation_status = "blocked"
            raise StrategyGuardError(
                "Pending manual order is not USDT-denominated; it remains blocked."
            )
        previous = pending.get("pre_trade_token_balance")
        output_symbol = str(pending.get("output_symbol", ""))
        if previous is None or output_symbol not in TOKENS:
            self._last_manual_reconciliation_status = "blocked"
            raise StrategyGuardError(
                "The pending manual order lacks output-balance metadata; it remains blocked."
            )
        token = self._token(output_symbol)
        received = (
            self._native_proceeds(
                receipt, expected_source=PANCAKESWAP_SMART_ROUTER
            )
            if action == "buy" and symbol == "WBNB"
            else int(token.functions.balanceOf(self.account.address).call()) - int(previous)
        )
        if received <= 0:
            self._last_manual_reconciliation_status = "blocked"
            raise StrategyGuardError(
                "Could not confirm the received output amount; manual trade remains blocked."
            )
        amount_out = str(
            Decimal(received) / Decimal(10) ** int(token.functions.decimals().call())
        )
        result = TradeResult(
            action,
            symbol,
            amount,
            amount_out,
            str(tx_hash),
            int(receipt["blockNumber"]),
        )
        if confirmation_callback is not None:
            confirmation_callback(result)
        state.confirm_manual(fingerprint, now=timestamp)
        self._last_manual_reconciliation_status = "confirmed"
        return result


class ReadOnlyPancakeSwapTrader(PancakeSwapTrader):
    """PancakeSwap V3 quote and balance client with no wallet access.

    This deliberately does not call ``Account.from_key`` and does not create a
    router, Smart Router, or transaction signer. It reuses only the existing
    quote formatting and direct V3 route selection methods.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.w3 = Web3(
            Web3.HTTPProvider(settings.bsc_rpc_url, request_kwargs={"timeout": 30})
        )
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        if not self.w3.is_connected():
            raise TraderError("Could not connect to BSC_RPC_URL.")
        if int(self.w3.eth.chain_id) != BSC_CHAIN_ID:
            raise TraderError(
                f"RPC returned chain ID {self.w3.eth.chain_id}; expected BSC chain ID {BSC_CHAIN_ID}."
            )
        self.quoter: Contract = self.w3.eth.contract(
            address=PANCAKESWAP_V3_QUOTER_V2,
            abi=V3_QUOTER_V2_ABI,
        )

    def native_balance_wei(self) -> int:
        return int(self.w3.eth.get_balance(self.settings.wallet_address))

    def usdt_balance(self) -> Decimal:
        balance = self._token(STRATEGY_BASE_ASSET).functions.balanceOf(
            self.settings.wallet_address
        ).call()
        return Decimal(int(balance)) / Decimal(10**TOKEN_DECIMALS[STRATEGY_BASE_ASSET])


class BscPreflight:
    """Run the operator's complete read-only check for the active Grid profile."""

    def __init__(
        self,
        settings: Settings,
        *,
        trader: ReadOnlyPancakeSwapTrader | None = None,
    ):
        if settings.live_strategy_mode != "grid":
            raise TraderError(
                "BSC preflight requires LIVE_STRATEGY_MODE=grid; no live profile is active."
            )
        self.settings = settings
        self.trader = trader or ReadOnlyPancakeSwapTrader(settings)

    @staticmethod
    def _decimal_text(value: Decimal) -> str:
        return format(value.normalize(), "f")

    @staticmethod
    def _safe_error(error: BaseException) -> str:
        # Do not echo provider URLs or RPC error payloads, which can contain
        # credentials when operators use a credential-bearing endpoint.
        return type(error).__name__

    @staticmethod
    def _redacted_address(address: str) -> str:
        return f"{address[:6]}…{address[-4:]}"

    def _journal_report(self) -> dict[str, object]:
        try:
            exposure = StrategyStateStore(
                self.settings.strategy_state_file
            ).read_only_exposure()
        except TraderError as error:
            return {"status": "error", "error": self._safe_error(error)}

        pending = exposure.pending
        pending_report: dict[str, object] | None = None
        if pending is not None:
            pending_report = {
                "kind": pending.kind,
                "action": pending.action,
                "symbol": pending.symbol,
                "amount": pending.amount,
                "transactionHash": pending.tx_hash,
                "phase": pending.phase,
            }
        return {
            "status": "pass",
            "pending": pending_report,
            "openLotCount": exposure.open_lot_count,
            "openLotsBySymbol": exposure.open_lots_by_symbol,
            "openExposureUsdt": self._decimal_text(exposure.open_exposure_usdt),
            "openExposureBySymbol": {
                symbol: self._decimal_text(amount)
                for symbol, amount in exposure.open_exposure_by_symbol.items()
            },
            "strategyPositions": exposure.strategy_positions,
        }

    def _quote_report(
        self,
        quote_specs: tuple[
            tuple[str, Callable[[], dict[str, str | int]]],
            ...,
        ],
    ) -> dict[str, dict[str, object]]:
        reports: dict[str, dict[str, object]] = {}
        for label, operation in quote_specs:
            try:
                quote = operation()
                reports[label] = {
                    "status": "pass",
                    "amountIn": quote["amountIn"],
                    "quotedOut": quote["quotedOut"],
                    "route": quote["route"],
                }
            except Exception as error:
                reports[label] = {
                    "status": "error",
                    "error": self._safe_error(error),
                }
        return reports

    def run(self) -> dict[str, object]:
        required_usdt = (
            self.settings.grid_max_budget_usdt * len(self.settings.grid_symbols)
        )
        profile = {
            "status": "pass",
            "mode": self.settings.live_strategy_mode,
            "symbols": list(self.settings.grid_symbols),
            "levels": self.settings.grid_levels,
            "lineUsdt": self._decimal_text(self.settings.grid_order_usdt),
            "maxExposurePerSymbolUsdt": self._decimal_text(
                self.settings.grid_max_budget_usdt
            ),
            "maxExposureTotalUsdt": self._decimal_text(required_usdt),
            "spacingBps": self.settings.grid_spacing_bps,
            "takeProfitBps": self.settings.grid_take_profit_bps,
            "buyImmediatelyNow": self.settings.buy_immediately_now,
        }

        gas_price_wei = int(self.trader.w3.eth.gas_price)
        bnb_balance = Decimal(self.trader.native_balance_wei()) / Decimal(10**18)
        max_gas_cost = Decimal(gas_price_wei * self.settings.max_gas_limit) / Decimal(
            10**18
        )
        gas_sufficient = bnb_balance >= max_gas_cost
        gas = {
            "status": "pass" if gas_sufficient else "error",
            "bnbBalance": self._decimal_text(bnb_balance),
            "gasPriceWei": gas_price_wei,
            "maxGasLimit": self.settings.max_gas_limit,
            "maxGasCostBnb": self._decimal_text(max_gas_cost),
            "sufficient": gas_sufficient,
        }

        usdt_balance = self.trader.usdt_balance()
        usdt_sufficient = usdt_balance >= required_usdt
        funding = {
            "status": "pass" if usdt_sufficient else "error",
            "usdtBalance": self._decimal_text(usdt_balance),
            "requiredForTotalGridCap": self._decimal_text(required_usdt),
            "requiredPerSymbol": self._decimal_text(
                self.settings.grid_max_budget_usdt
            ),
            "sufficient": usdt_sufficient,
        }

        line_amount = self._decimal_text(self.settings.grid_order_usdt)
        quote_specs = (
            (
                "USDT/XRP buy",
                lambda: self.trader.quote("buy", "XRP", line_amount),
            ),
            (
                "XRP/USDT sell",
                lambda: self.trader.quote("sell", "XRP", "1"),
            ),
            (
                "USDT/BTC buy",
                lambda: self.trader.quote("buy", "BTC", line_amount),
            ),
            (
                "BTC/USDT sell",
                lambda: self.trader.quote("sell", "BTC", "1"),
            ),
        )
        quotes = self._quote_report(quote_specs)
        quote_ok = all(report["status"] == "pass" for report in quotes.values())
        journal = self._journal_report()
        chain_id = int(self.trader.w3.eth.chain_id)
        report: dict[str, object] = {
            "ok": (
                chain_id == BSC_CHAIN_ID
                and gas_sufficient
                and usdt_sufficient
                and journal["status"] == "pass"
                and quote_ok
            ),
            "profile": profile,
            "chain": {
                "status": "pass" if chain_id == BSC_CHAIN_ID else "error",
                "chainId": chain_id,
                "expectedChainId": BSC_CHAIN_ID,
            },
            "wallet": {
                "address": self._redacted_address(self.settings.wallet_address)
            },
            "gas": gas,
            "funding": funding,
            "journal": journal,
            "quotes": quotes,
        }
        return report
