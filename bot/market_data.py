"""Fail-closed market-data validation and dynamic grid planning.

The live grid services consume a small, operator-approved market-data contract.
The producer of that contract is deliberately separate from the trading
executor: this module only reads and validates JSON and never opens a network
connection, reads wallet material, or signs a transaction.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Protocol


class MarketDataError(ValueError):
    """Raised when market data cannot safely drive a new grid entry."""


@dataclass(frozen=True)
class Candle:
    opened_at: float
    closed_at: float
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class VolumeNode:
    price: Decimal
    volume: Decimal


@dataclass(frozen=True)
class ValidatedMarketData:
    symbol: str
    base_asset: str
    captured_at: float
    candles: tuple[Candle, ...]
    volume_profile: tuple[VolumeNode, ...]
    rsi: Decimal

    @property
    def latest(self) -> Candle:
        return self.candles[-1]


@dataclass(frozen=True)
class DynamicGridPlan:
    """The one-entry plan produced from a validated market snapshot."""

    anchor_price: Decimal
    levels: tuple[Decimal, ...]
    spacing_bps: int
    rsi: Decimal
    volume_confirmed: bool
    entry_allowed: bool
    reason: str


class MarketDataProvider(Protocol):
    def read(self, symbol: str) -> ValidatedMarketData:
        ...


def _decimal(value: Any, label: str, *, positive: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise MarketDataError(f"Market data {label} is not a decimal.") from error
    if not parsed.is_finite() or (positive and parsed <= 0):
        qualifier = "positive and finite" if positive else "finite"
        raise MarketDataError(f"Market data {label} must be {qualifier}.")
    return parsed


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketDataError(f"Market data {label} is not a number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise MarketDataError(f"Market data {label} is not finite.")
    return parsed


def _candle(raw: Any, index: int) -> Candle:
    if not isinstance(raw, dict):
        raise MarketDataError(f"Market data candle {index} is invalid.")
    opened_at = _number(raw.get("opened_at"), f"candle {index} opened_at")
    closed_at = _number(raw.get("closed_at"), f"candle {index} closed_at")
    if closed_at <= opened_at:
        raise MarketDataError(f"Market data candle {index} has invalid time order.")
    opened = _decimal(raw.get("open"), f"candle {index} open")
    high = _decimal(raw.get("high"), f"candle {index} high")
    low = _decimal(raw.get("low"), f"candle {index} low")
    close = _decimal(raw.get("close"), f"candle {index} close")
    volume = _decimal(raw.get("volume"), f"candle {index} volume")
    if high < max(opened, close) or low > min(opened, close) or low > high:
        raise MarketDataError(f"Market data candle {index} violates OHLC bounds.")
    return Candle(opened_at, closed_at, opened, high, low, close, volume)


def _volume_node(raw: Any, index: int) -> VolumeNode:
    if not isinstance(raw, dict):
        raise MarketDataError(f"Market data volume node {index} is invalid.")
    return VolumeNode(
        _decimal(raw.get("price"), f"volume node {index} price"),
        _decimal(raw.get("volume"), f"volume node {index} volume"),
    )


def _computed_rsi(candles: tuple[Candle, ...], period: int) -> Decimal:
    closes = [candle.close for candle in candles]
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    window = changes[-period:]
    gains = sum((change for change in window if change > 0), Decimal(0))
    losses = sum((-change for change in window if change < 0), Decimal(0))
    if losses == 0:
        return Decimal("100") if gains > 0 else Decimal("50")
    relative_strength = gains / losses
    return Decimal("100") - (Decimal("100") / (Decimal(1) + relative_strength))


def validate_market_data(
    raw: Any,
    expected_symbol: str,
    *,
    now: float | None = None,
    max_age_seconds: int = 300,
    rsi_period: int = 14,
) -> ValidatedMarketData:
    """Validate all fields needed by the dynamic grid.

    The supplied RSI is checked against the candle-derived value. This avoids
    trusting a stale or contradictory indicator pasted beside otherwise valid
    candles.
    """

    if not isinstance(raw, dict):
        raise MarketDataError("Market data document is invalid.")
    symbol = str(raw.get("symbol", "")).upper()
    expected = expected_symbol.upper()
    if symbol != expected:
        raise MarketDataError("Market data symbol does not match the isolated bot.")
    if str(raw.get("base_asset", "")).upper() != "USDT":
        raise MarketDataError("Market data must be USDT-denominated.")
    captured_at = _number(raw.get("captured_at"), "captured_at")
    current = float(__import__("time").time() if now is None else now)
    if captured_at > current + 5 or current - captured_at > max_age_seconds:
        raise MarketDataError("Market data is stale or from the future.")
    raw_candles = raw.get("candles")
    if not isinstance(raw_candles, list) or len(raw_candles) < rsi_period + 1:
        raise MarketDataError("Market data does not contain enough candles for RSI.")
    candles = tuple(_candle(item, index) for index, item in enumerate(raw_candles))
    for previous, current_candle in zip(candles, candles[1:]):
        if current_candle.opened_at < previous.closed_at:
            raise MarketDataError("Market data candle intervals overlap or are unordered.")
    if candles[-1].closed_at > current + 5:
        raise MarketDataError("The latest candle is not closed yet.")
    if current - candles[-1].closed_at > max_age_seconds:
        raise MarketDataError("The latest closed candle is stale.")

    raw_profile = raw.get("volume_profile")
    if not isinstance(raw_profile, list) or not raw_profile:
        raise MarketDataError("Market data volume profile is missing.")
    profile = tuple(_volume_node(item, index) for index, item in enumerate(raw_profile))
    if any(left.price >= right.price for left, right in zip(profile, profile[1:])):
        raise MarketDataError("Market data volume profile must be strictly price-ordered.")
    supplied_rsi = _decimal(raw.get("rsi"), "RSI", positive=False)
    if supplied_rsi < 0 or supplied_rsi > 100:
        raise MarketDataError("Market data RSI must be between 0 and 100.")
    computed_rsi = _computed_rsi(candles, rsi_period)
    if abs(supplied_rsi - computed_rsi) > Decimal("0.5"):
        raise MarketDataError("Market data RSI contradicts the supplied candles.")
    return ValidatedMarketData(
        symbol=expected,
        base_asset="USDT",
        captured_at=captured_at,
        candles=candles,
        volume_profile=profile,
        rsi=supplied_rsi,
    )


def dynamic_grid_plan(
    market: ValidatedMarketData,
    *,
    levels: int = 6,
    min_spacing_bps: int = 100,
    max_spacing_bps: int = 500,
    buy_rsi_max: Decimal = Decimal("65"),
) -> DynamicGridPlan:
    """Build an anchor and descending levels from candles, volume, and RSI."""

    if levels <= 0 or min_spacing_bps <= 0 or max_spacing_bps < min_spacing_bps:
        raise MarketDataError("Dynamic grid planner bounds are invalid.")
    latest = market.latest
    total_profile_volume = sum((node.volume for node in market.volume_profile), Decimal(0))
    weighted_volume_price = sum(
        (node.price * node.volume for node in market.volume_profile), Decimal(0)
    ) / total_profile_volume
    poc = max(market.volume_profile, key=lambda node: node.volume).price
    candle_volume = sum((candle.volume for candle in market.candles[-5:]), Decimal(0))
    average_volume = candle_volume / Decimal(min(5, len(market.candles)))
    volume_confirmed = latest.volume >= average_volume

    true_ranges: list[Decimal] = []
    for previous, candle in zip(market.candles, market.candles[1:]):
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous.close),
                abs(candle.low - previous.close),
            )
        )
    atr = sum(true_ranges[-14:], Decimal(0)) / Decimal(min(14, len(true_ranges)))
    volatility_bps = int(
        (atr / latest.close * Decimal(10_000)).quantize(Decimal("1"), rounding=ROUND_DOWN)
    )
    spacing = max(min_spacing_bps, min(max_spacing_bps, volatility_bps))
    spacing = max(min_spacing_bps, (spacing // 10) * 10)

    # The anchor blends independent candle and profile information. RSI nudges
    # it toward support during oversold conditions without becoming a source
    # level or a hard-coded price override.
    vwap = sum(
        (((candle.high + candle.low + candle.close) / Decimal(3)) * candle.volume)
        for candle in market.candles[-14:]
    ) / sum((candle.volume for candle in market.candles[-14:]), Decimal(0))
    rsi_bias = (Decimal("50") - market.rsi) / Decimal("10_000")
    anchor = ((latest.close + weighted_volume_price + poc + vwap) / Decimal(4)) * (
        Decimal(1) + rsi_bias
    )
    if not anchor.is_finite() or anchor <= 0:
        raise MarketDataError("Dynamic grid anchor is invalid.")
    planned_levels = tuple(
        (anchor * Decimal(10_000 - spacing * level) / Decimal(10_000)).normalize()
        for level in range(1, levels + 1)
    )
    if any(level <= 0 for level in planned_levels):
        raise MarketDataError("Dynamic grid levels are invalid.")
    entry_allowed = market.rsi <= buy_rsi_max and volume_confirmed
    reason = (
        "RSI and volume confirmed"
        if entry_allowed
        else "new entries blocked by RSI or volume confirmation"
    )
    return DynamicGridPlan(
        anchor_price=anchor.normalize(),
        levels=planned_levels,
        spacing_bps=spacing,
        rsi=market.rsi,
        volume_confirmed=volume_confirmed,
        entry_allowed=entry_allowed,
        reason=reason,
    )


class MarketDataFileProvider:
    """Read one bot's atomically replaced market-data document."""

    def __init__(
        self,
        path: str | Path,
        symbol: str,
        *,
        max_age_seconds: int = 300,
        rsi_period: int = 14,
        clock: Any | None = None,
    ):
        self.path = Path(path).expanduser()
        self.symbol = symbol.upper()
        self.max_age_seconds = max_age_seconds
        self.rsi_period = rsi_period
        self.clock = clock

    def read(self, symbol: str | None = None) -> ValidatedMarketData:
        requested = (symbol or self.symbol).upper()
        if requested != self.symbol:
            raise MarketDataError("Market data provider is bound to one isolated symbol.")
        try:
            with self.path.open(encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, json.JSONDecodeError) as error:
            raise MarketDataError(
                f"Market data file {self.path} is missing or unreadable."
            ) from error
        now = self.clock() if callable(self.clock) else None
        return validate_market_data(
            raw,
            self.symbol,
            now=now,
            max_age_seconds=self.max_age_seconds,
            rsi_period=self.rsi_period,
        )