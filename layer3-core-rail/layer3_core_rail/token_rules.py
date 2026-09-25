from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .errors import CoreRailValidationError

TOKEN_DECIMALS = 9
ONE_TLAMA = 10**TOKEN_DECIMALS
TOTAL_SUPPLY_TLAMA = 1_000_000_000
TOTAL_SUPPLY_BASE_UNITS = TOTAL_SUPPLY_TLAMA * ONE_TLAMA
BPS_DENOMINATOR = 10_000
BUY_BURN_BPS = 50
SELL_BURN_BPS = 100
MAX_TRANSACTION_TLAMA = 5_000_000
MAX_TRANSACTION_BASE_UNITS = MAX_TRANSACTION_TLAMA * ONE_TLAMA
MAX_WALLET_TLAMA = 10_000_000
MAX_WALLET_BASE_UNITS = MAX_WALLET_TLAMA * ONE_TLAMA

SwapDirection = Literal["buy", "sell"]


def _positive_base_units(value: int, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CoreRailValidationError(f"{field} must be a positive integer")
    return value


def calculate_directional_burn(
    gross_base_units: int, direction: SwapDirection
) -> int:
    gross = _positive_base_units(gross_base_units, field="gross_base_units")
    if direction == "buy":
        rate = BUY_BURN_BPS
    elif direction == "sell":
        rate = SELL_BURN_BPS
    else:
        raise CoreRailValidationError("direction must be 'buy' or 'sell'")
    burn = (gross * rate + BPS_DENOMINATOR - 1) // BPS_DENOMINATOR
    if burn <= 0 or burn > gross:
        raise CoreRailValidationError("burn is outside the valid amount range")
    return burn


def validate_transaction_limit(amount_base_units: int) -> None:
    amount = _positive_base_units(amount_base_units, field="amount_base_units")
    if amount > MAX_TRANSACTION_BASE_UNITS:
        raise CoreRailValidationError(
            "Anti-whale trade amount exceeds the immutable transaction limit."
        )


def validate_wallet_limit(
    *, current_base_units: int, incoming_base_units: int
) -> None:
    if (
        not isinstance(current_base_units, int)
        or isinstance(current_base_units, bool)
        or current_base_units < 0
    ):
        raise CoreRailValidationError(
            "Anti-whale wallet balances must be valid integers."
        )
    incoming = _positive_base_units(
        incoming_base_units, field="incoming_base_units"
    )
    if current_base_units + incoming > MAX_WALLET_BASE_UNITS:
        raise CoreRailValidationError(
            "Anti-whale wallet holding exceeds the immutable wallet limit."
        )


@dataclass(frozen=True)
class SwapAssessment:
    direction: SwapDirection
    gross_base_units: int
    burn_base_units: int
    net_base_units: int


def assess_swap(
    *,
    direction: SwapDirection,
    gross_base_units: int,
    current_wallet_base_units: int | None = None,
) -> SwapAssessment:
    validate_transaction_limit(gross_base_units)
    burn = calculate_directional_burn(gross_base_units, direction)
    net = gross_base_units - burn
    if direction == "buy" and current_wallet_base_units is not None:
        validate_wallet_limit(
            current_base_units=current_wallet_base_units,
            incoming_base_units=net,
        )
    return SwapAssessment(direction, gross_base_units, burn, net)