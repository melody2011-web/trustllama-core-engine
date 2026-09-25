from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from web3 import Web3

FIXED_STRATEGY_POSITION_BPS = 2_000
DEFAULT_GRID_SPACING_BPS = 500
DEFAULT_GRID_TAKE_PROFIT_BPS = 500
LEGACY_GRID_TAKE_PROFIT_BPS = 200
# Kept as a compatibility export for callers that imported the old name.
FIXED_GRID_TAKE_PROFIT_BPS = DEFAULT_GRID_TAKE_PROFIT_BPS
APPROVED_GRID_PROFILES = frozenset(
    {
        (5, Decimal("50"), Decimal("250")),
        (6, Decimal("50"), Decimal("300")),
    }
)
APPROVED_FUNDED_GRID_RATE_PROFILES = frozenset(
    {
        (100, 100),
        (500, 500),
    }
)
LIVE_STRATEGY_MODES = frozenset({"disabled", "ema", "grid"})
GRID_SYMBOLS = frozenset({"XRP", "BTC"})
ISOLATED_GRID_PROFILES = {
    "grid_bot_1": "BTC",
    "grid_bot_2": "XRP",
    "grid_bot_4": "WBNB",
    "grid_bot_5": "ETH",
    "grid_bot_6": "SOL",
}
DUAL_GRID_PROFILES = frozenset({"grid_bot_1", "grid_bot_2"})
ISOLATED_SCALPER_PROFILES = {
    "scalper_bot_7": "WBNB",
    "scalper_bot_8": "ETH",
    "scalper_bot_9": "SOL",
    "scalper_bot_10": "XRP",
    "scalper_bot_11": "BTC",
}


class ConfigurationError(ValueError):
    """Raised when a required setting is absent or invalid."""


def _required_secret(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"{name} must be configured as a Replit Secret; it is never read from source files."
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigurationError(f"{name} must be an integer.") from error
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _positive_int_alias(name: str, legacy_name: str, default: int) -> int:
    primary = os.getenv(name, "").strip()
    legacy = os.getenv(legacy_name, "").strip()
    if primary and legacy:
        primary_value = _positive_int(name, default)
        legacy_value = _positive_int(legacy_name, default)
        if primary_value != legacy_value:
            raise ConfigurationError(f"{name} and {legacy_name} must match when both are set.")
        return primary_value
    if primary:
        return _positive_int(name, default)
    if legacy:
        return _positive_int(legacy_name, default)
    return default


def _basis_points(name: str, default: int) -> int:
    value = _positive_int(name, default)
    if value >= 10_000:
        raise ConfigurationError(f"{name} must be less than 10000 basis points.")
    return value


def _percentage_fraction_as_bps(name: str, default: str) -> int:
    value = _positive_decimal(name, default)
    if value >= 1:
        raise ConfigurationError(f"{name} must be a fraction between 0 and 1.")
    basis_points = value * Decimal(10_000)
    if basis_points != basis_points.to_integral_value():
        raise ConfigurationError(f"{name} must convert to a whole number of basis points.")
    return int(basis_points)


def _grid_spacing_bps() -> int:
    percent = os.getenv("GRID_STEP_PERCENT", "").strip()
    legacy = os.getenv("GRID_SPACING_BPS", "").strip()
    if percent and legacy:
        percent_value = _percentage_fraction_as_bps("GRID_STEP_PERCENT", "0.05")
        legacy_value = _basis_points("GRID_SPACING_BPS", DEFAULT_GRID_SPACING_BPS)
        if percent_value != legacy_value:
            raise ConfigurationError(
                "GRID_STEP_PERCENT and GRID_SPACING_BPS must describe the same spacing."
            )
        return percent_value
    if percent:
        return _percentage_fraction_as_bps("GRID_STEP_PERCENT", "0.05")
    if legacy:
        return _basis_points("GRID_SPACING_BPS", DEFAULT_GRID_SPACING_BPS)
    return DEFAULT_GRID_SPACING_BPS


def _grid_take_profit_bps() -> int:
    percent = os.getenv("PROFIT_TARGET_PERCENT", "").strip()
    legacy = os.getenv("GRID_TAKE_PROFIT_BPS", "").strip()
    if percent and legacy:
        percent_value = _percentage_fraction_as_bps("PROFIT_TARGET_PERCENT", "0.05")
        legacy_value = _basis_points("GRID_TAKE_PROFIT_BPS", DEFAULT_GRID_TAKE_PROFIT_BPS)
        if percent_value != legacy_value:
            raise ConfigurationError(
                "PROFIT_TARGET_PERCENT and GRID_TAKE_PROFIT_BPS must describe the same target."
            )
        return percent_value
    if percent:
        return _percentage_fraction_as_bps("PROFIT_TARGET_PERCENT", "0.05")
    if legacy:
        return _basis_points("GRID_TAKE_PROFIT_BPS", DEFAULT_GRID_TAKE_PROFIT_BPS)
    return DEFAULT_GRID_TAKE_PROFIT_BPS


def _positive_decimal_alias(name: str, legacy_name: str, default: str) -> Decimal:
    primary = os.getenv(name, "").strip()
    legacy = os.getenv(legacy_name, "").strip()
    if primary and legacy:
        primary_value = _positive_decimal(name, default)
        legacy_value = _positive_decimal(legacy_name, default)
        if primary_value != legacy_value:
            raise ConfigurationError(f"{name} and {legacy_name} must match when both are set.")
        return primary_value
    if primary:
        return _positive_decimal(name, default)
    if legacy:
        return _positive_decimal(legacy_name, default)
    return Decimal(default)


def _percentage(name: str, default: int) -> int:
    value = _positive_int(name, default)
    if value > 100:
        raise ConfigurationError(f"{name} must be between 1 and 100.")
    return value


def _nonnegative_decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise ConfigurationError(f"{name} must be a decimal number.") from error
    if not value.is_finite() or value < 0:
        raise ConfigurationError(f"{name} must be finite and non-negative.")
    return value


def _positive_decimal(name: str, default: str) -> Decimal:
    value = _nonnegative_decimal(name, default)
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false.")


def _choice(name: str, choices: set[str], default: str) -> str:
    value = os.getenv(name, default).strip().lower()
    if value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ConfigurationError(f"{name} must be one of: {allowed}.")
    return value


def _symbols(name: str, default: str) -> tuple[str, ...]:
    symbols = tuple(
        symbol.strip().upper() for symbol in os.getenv(name, default).split(",") if symbol.strip()
    )
    if not symbols or len(set(symbols)) != len(symbols) or any(
        symbol not in GRID_SYMBOLS for symbol in symbols
    ):
        raise ConfigurationError(f"{name} must be a comma-separated subset of XRP,BTC.")
    return symbols


@dataclass(frozen=True)
class Settings:
    bsc_rpc_url: str
    wallet_address: str
    wallet_private_key: str
    enable_live_trading: bool
    slippage_bps: int
    deadline_seconds: int
    receipt_timeout_seconds: int
    max_gas_limit: int
    notification_api_url: str = "http://127.0.0.1:8080/api/notifications/trade"
    notification_auth_secret: str = ""
    strategy_fast_window: int = 9
    strategy_slow_window: int = 21
    strategy_rsi_window: int = 14
    strategy_buy_rsi_max: int = 65
    strategy_sell_rsi_min: int = 65
    strategy_position_bps: int = FIXED_STRATEGY_POSITION_BPS
    strategy_cooldown_seconds: int = 900
    strategy_stop_loss_bps: int = 500
    strategy_take_profit_bps: int = 1_000
    strategy_max_trades_per_day: int = 3
    strategy_state_file: str = ".bot-strategy-state.json"
    live_strategy_mode: str = "disabled"
    grid_symbols: tuple[str, ...] = ("XRP", "BTC")
    grid_spacing_bps: int = DEFAULT_GRID_SPACING_BPS
    grid_levels: int = 5
    grid_order_usdt: Decimal = Decimal("50")
    grid_max_budget_usdt: Decimal = Decimal("250")
    grid_take_profit_bps: int = DEFAULT_GRID_TAKE_PROFIT_BPS
    buy_immediately_now: bool = False
    paper_poll_interval_seconds: int = 60
    paper_retry_base_seconds: int = 5
    paper_retry_max_seconds: int = 300
    paper_price_history_limit: int = 100
    paper_starting_bnb: Decimal = Decimal("1")
    paper_starting_xrp: Decimal = Decimal("0")
    paper_starting_btc: Decimal = Decimal("0")
    paper_state_file: str = ".paper-strategy-state.json"
    live_poll_interval_seconds: int = 60
    live_retry_base_seconds: int = 5
    live_retry_max_seconds: int = 300
    live_price_history_limit: int = 100
    live_price_history_file: str = ".live-strategy-prices.json"
    # This is deliberately not environment-selected. Only the dedicated root
    # launchers opt into one of these otherwise forbidden funded profiles.
    isolated_grid_profile: str | None = None
    # Only the dedicated scalper launchers set this value.  It is deliberately
    # not sourced from an environment variable.
    isolated_scalper_profile: str | None = None

    def __post_init__(self) -> None:
        if self.strategy_position_bps != FIXED_STRATEGY_POSITION_BPS:
            raise ConfigurationError(
                "STRATEGY_POSITION_BPS must be exactly 2000 for the USDT live strategy."
            )
        if self.live_strategy_mode not in LIVE_STRATEGY_MODES:
            raise ConfigurationError("LIVE_STRATEGY_MODE must be one of: disabled, ema, grid.")
        isolated_symbol = ISOLATED_GRID_PROFILES.get(self.isolated_grid_profile or "")
        scalper_symbol = ISOLATED_SCALPER_PROFILES.get(self.isolated_scalper_profile or "")
        if self.isolated_grid_profile is not None and isolated_symbol is None:
            raise ConfigurationError("Unknown isolated grid profile.")
        if self.isolated_scalper_profile is not None and scalper_symbol is None:
            raise ConfigurationError("Unknown isolated scalper profile.")
        if self.isolated_grid_profile is not None and self.isolated_scalper_profile is not None:
            raise ConfigurationError("A settings instance cannot combine grid and scalper profiles.")
        allowed_symbols = {"XRP", "BTC"} if isolated_symbol is None else {isolated_symbol}
        if (
            not self.grid_symbols
            or len(set(self.grid_symbols)) != len(self.grid_symbols)
            or any(symbol not in allowed_symbols for symbol in self.grid_symbols)
        ):
            raise ConfigurationError("GRID_SYMBOLS are not permitted for this trading profile.")
        if self.grid_levels <= 0:
            raise ConfigurationError("GRID_LEVELS must be greater than zero.")
        if self.grid_spacing_bps <= 0 or self.grid_spacing_bps * self.grid_levels >= 10_000:
            raise ConfigurationError(
                "GRID_SPACING_BPS multiplied by GRID_LEVELS must be less than 10000."
            )
        if (
            not self.grid_order_usdt.is_finite()
            or not self.grid_max_budget_usdt.is_finite()
            or self.grid_order_usdt <= 0
            or self.grid_max_budget_usdt <= 0
        ):
            raise ConfigurationError("GRID_ORDER_USDT and GRID_MAX_BUDGET_USDT must be positive.")
        if not isinstance(self.buy_immediately_now, bool):
            raise ConfigurationError("BUY_IMMEDIATELY_NOW must be true or false.")
        if self.live_strategy_mode == "grid" and isolated_symbol is not None and (
            self.grid_symbols != (isolated_symbol,)
            or self.grid_levels != 6
            or self.grid_spacing_bps < 100
            or self.grid_spacing_bps > 500
            or self.grid_take_profit_bps != 100
            or self.grid_max_budget_usdt != Decimal("300")
            or self.grid_order_usdt > Decimal("50")
        ):
            raise ConfigurationError(
                "Isolated grid profiles require one symbol, six levels, "
                "100-500bps spacing, a 1% target, and a 300 USDT cap."
            )
        if self.live_strategy_mode == "grid" and isolated_symbol is None and (
            self.grid_symbols != ("XRP", "BTC")
            or (
                self.grid_levels,
                self.grid_order_usdt,
                self.grid_max_budget_usdt,
            )
            not in APPROVED_GRID_PROFILES
            or (
                self.grid_spacing_bps,
                self.grid_take_profit_bps,
            )
            not in APPROVED_FUNDED_GRID_RATE_PROFILES
        ):
            raise ConfigurationError(
                "Funded grid mode is fixed at XRP,BTC; approved profiles are "
                "5 levels at 50/250 USDT or 6 levels at 50/300 USDT; and matching "
                "spacing/take-profit rates of 100 or 500 bps."
            )
        if self.grid_order_usdt * self.grid_levels > self.grid_max_budget_usdt:
            raise ConfigurationError(
                "GRID_LEVELS multiplied by GRID_ORDER_USDT must not exceed GRID_MAX_BUDGET_USDT."
            )

    @classmethod
    def from_environment(
        cls,
        *,
        require_wallet: bool = True,
        require_private_key: bool | None = None,
        isolated_grid_profile: str | None = None,
        isolated_scalper_profile: str | None = None,
    ) -> "Settings":
        # Most callers need both credentials. Read-only tools can require the
        # public wallet address without ever loading the private key.
        if require_private_key is None:
            require_private_key = require_wallet
        wallet_address = os.getenv("WALLET_ADDRESS", "").strip()
        if not wallet_address and require_wallet:
            wallet_address = _required_secret("WALLET_ADDRESS")
        if not wallet_address:
            wallet_address = "0x0000000000000000000000000000000000000000"
        try:
            wallet_address = Web3.to_checksum_address(wallet_address)
        except ValueError as error:
            raise ConfigurationError("WALLET_ADDRESS is not a valid EVM address.") from error
        wallet_private_key = (
            _required_secret("WALLET_PRIVATE_KEY") if require_private_key else ""
        )
        if isolated_grid_profile is not None and isolated_grid_profile not in ISOLATED_GRID_PROFILES:
            raise ConfigurationError("Unknown isolated grid profile.")
        if isolated_scalper_profile is not None and isolated_scalper_profile not in ISOLATED_SCALPER_PROFILES:
            raise ConfigurationError("Unknown isolated scalper profile.")
        isolated_root = Path.home() / ".local" / "state" / "bnb-defi-bot" / (isolated_grid_profile or "")
        strategy_state_file = os.getenv("STRATEGY_STATE_FILE", "").strip() or str(
            isolated_root / f"{wallet_address.lower()}.json"
            if isolated_grid_profile else
            Path.home()
            / ".local"
            / "state"
            / "bnb-defi-bot"
            / f"{wallet_address.lower()}.json"
        )
        paper_state_file = os.getenv("PAPER_STATE_FILE", "").strip() or str(
            Path.home() / ".local" / "state" / "bnb-defi-bot" / "paper.json"
        )
        paper_retry_max_seconds = _positive_int("PAPER_RETRY_MAX_SECONDS", 300)
        paper_retry_base_seconds = _positive_int("PAPER_RETRY_BASE_SECONDS", 5)
        if paper_retry_base_seconds > paper_retry_max_seconds:
            raise ConfigurationError(
                "PAPER_RETRY_BASE_SECONDS must not exceed PAPER_RETRY_MAX_SECONDS."
            )
        paper_price_history_limit = _positive_int("PAPER_PRICE_HISTORY_LIMIT", 100)
        live_retry_max_seconds = _positive_int("LIVE_RETRY_MAX_SECONDS", 300)
        live_retry_base_seconds = _positive_int("LIVE_RETRY_BASE_SECONDS", 5)
        if live_retry_base_seconds > live_retry_max_seconds:
            raise ConfigurationError(
                "LIVE_RETRY_BASE_SECONDS must not exceed LIVE_RETRY_MAX_SECONDS."
            )
        live_price_history_limit = _positive_int("LIVE_PRICE_HISTORY_LIMIT", 100)
        minimum_history = max(
            _positive_int("STRATEGY_SLOW_WINDOW", 21) + 1,
            _positive_int("STRATEGY_RSI_WINDOW", 14) + 1,
        )
        if paper_price_history_limit < minimum_history:
            raise ConfigurationError(
                "PAPER_PRICE_HISTORY_LIMIT must cover the strategy warm-up history."
            )
        if live_price_history_limit < minimum_history:
            raise ConfigurationError(
                "LIVE_PRICE_HISTORY_LIMIT must cover the strategy warm-up history."
            )
        live_price_history_file = os.getenv("LIVE_PRICE_HISTORY_FILE", "").strip() or str(
            isolated_root / f"{wallet_address.lower()}.live-prices.json"
            if isolated_grid_profile else
            Path.home()
            / ".local"
            / "state"
            / "bnb-defi-bot"
            / f"{wallet_address.lower()}.live-prices.json"
        )

        return cls(
            bsc_rpc_url=_required_secret("BSC_RPC_URL"),
            wallet_address=wallet_address,
            wallet_private_key=wallet_private_key,
            enable_live_trading=_boolean("ENABLE_LIVE_TRADING", False),
            slippage_bps=_basis_points("SLIPPAGE_BPS", 100),
            deadline_seconds=_positive_int("DEADLINE_SECONDS", 120),
            receipt_timeout_seconds=_positive_int("RECEIPT_TIMEOUT_SECONDS", 180),
            max_gas_limit=_positive_int("MAX_GAS_LIMIT", 700_000),
            notification_api_url=os.getenv(
                "NOTIFICATION_API_URL",
                "http://127.0.0.1:8080/api/notifications/trade",
            ).strip(),
            notification_auth_secret=(
                _required_secret("SESSION_SECRET") if require_private_key else ""
            ),
            strategy_fast_window=_positive_int("STRATEGY_FAST_WINDOW", 9),
            strategy_slow_window=_positive_int("STRATEGY_SLOW_WINDOW", 21),
            strategy_rsi_window=_positive_int("STRATEGY_RSI_WINDOW", 14),
            strategy_buy_rsi_max=_percentage("STRATEGY_BUY_RSI_MAX", 65),
            strategy_sell_rsi_min=_percentage("STRATEGY_SELL_RSI_MIN", 65),
            strategy_position_bps=_basis_points(
                "STRATEGY_POSITION_BPS", FIXED_STRATEGY_POSITION_BPS
            ),
            strategy_cooldown_seconds=_positive_int("STRATEGY_COOLDOWN_SECONDS", 900),
            strategy_stop_loss_bps=_basis_points("STRATEGY_STOP_LOSS_BPS", 500),
            strategy_take_profit_bps=_basis_points("STRATEGY_TAKE_PROFIT_BPS", 1_000),
            strategy_max_trades_per_day=_positive_int("STRATEGY_MAX_TRADES_PER_DAY", 3),
            strategy_state_file=strategy_state_file,
            live_strategy_mode=_choice(
                "LIVE_STRATEGY_MODE", set(LIVE_STRATEGY_MODES), "disabled"
            ),
            grid_symbols=(ISOLATED_GRID_PROFILES[isolated_grid_profile],) if isolated_grid_profile else _symbols("GRID_SYMBOLS", "XRP,BTC"),
            grid_spacing_bps=100 if isolated_grid_profile else _grid_spacing_bps(),
            grid_levels=6 if isolated_grid_profile else _positive_int_alias("TOTAL_GRID_LEVELS", "GRID_LEVELS", 5),
            grid_order_usdt=Decimal("50") if isolated_grid_profile else _positive_decimal_alias("PER_LINE_USDT", "GRID_ORDER_USDT", "50"),
            grid_max_budget_usdt=Decimal("300") if isolated_grid_profile else _positive_decimal("GRID_MAX_BUDGET_USDT", "250"),
            grid_take_profit_bps=100 if isolated_grid_profile else _grid_take_profit_bps(),
            buy_immediately_now=True if isolated_grid_profile else _boolean("BUY_IMMEDIATELY_NOW", False),
            paper_poll_interval_seconds=_positive_int("PAPER_POLL_INTERVAL_SECONDS", 60),
            paper_retry_base_seconds=paper_retry_base_seconds,
            paper_retry_max_seconds=paper_retry_max_seconds,
            paper_price_history_limit=paper_price_history_limit,
            paper_starting_bnb=_nonnegative_decimal("PAPER_STARTING_BNB", "1"),
            paper_starting_xrp=_nonnegative_decimal("PAPER_STARTING_XRP", "0"),
            paper_starting_btc=_nonnegative_decimal("PAPER_STARTING_BTC", "0"),
            paper_state_file=paper_state_file,
            live_poll_interval_seconds=_positive_int("LIVE_POLL_INTERVAL_SECONDS", 60),
            live_retry_base_seconds=live_retry_base_seconds,
            live_retry_max_seconds=live_retry_max_seconds,
            live_price_history_limit=live_price_history_limit,
            live_price_history_file=live_price_history_file,
            isolated_grid_profile=isolated_grid_profile,
            isolated_scalper_profile=isolated_scalper_profile,
        )
