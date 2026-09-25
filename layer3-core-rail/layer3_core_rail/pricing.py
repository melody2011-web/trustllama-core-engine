from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Mapping

from .errors import CoreRailValidationError


def positive_decimal(value: Decimal | str, *, field: str) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CoreRailValidationError(f"{field} must be a valid decimal number") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise CoreRailValidationError(f"{field} must be greater than zero")
    return parsed


def required_tlama_for_usd(
    target_usd: Decimal | str, token_price_usd: Decimal | str
) -> int:
    target = positive_decimal(target_usd, field="target_usd")
    price = positive_decimal(token_price_usd, field="token_price_usd")
    return int((target / price).to_integral_value(rounding=ROUND_CEILING))


def required_tlama_atomic_for_usd(
    target_usd: Decimal | str,
    token_price_usd: Decimal | str,
    token_decimals: int,
) -> int:
    target = positive_decimal(target_usd, field="target_usd")
    price = positive_decimal(token_price_usd, field="token_price_usd")
    if (
        not isinstance(token_decimals, int)
        or isinstance(token_decimals, bool)
        or not 0 <= token_decimals <= 18
    ):
        raise CoreRailValidationError("token_decimals must be an integer from 0 to 18")
    return int(
        (target * (10**token_decimals) / price).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


@dataclass(frozen=True)
class DynamicOraclePricingRouter:
    """Pure fiat-target conversion using an injected oracle observation."""

    token_price_usd: Decimal

    def __init__(self, token_price_usd: Decimal | str) -> None:
        object.__setattr__(
            self,
            "token_price_usd",
            positive_decimal(token_price_usd, field="token_price_usd"),
        )

    def whole_tokens(self, target_usd: Decimal | str) -> int:
        return required_tlama_for_usd(target_usd, self.token_price_usd)

    def atomic_units(self, target_usd: Decimal | str, token_decimals: int) -> int:
        return required_tlama_atomic_for_usd(
            target_usd, self.token_price_usd, token_decimals
        )

    def catalog_item(
        self,
        *,
        item_id: str,
        kind: str,
        label: str,
        target_usd: Decimal | str,
        pricing_source: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = positive_decimal(target_usd, field="target_usd")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (item_id, kind, label, pricing_source)
        ):
            raise CoreRailValidationError("catalog identity fields must be non-empty")
        protected_fields = {"id", "kind", "label", "target_usd", "pricing", "amount_tlama"}
        collisions = protected_fields.intersection(details or {})
        if collisions:
            names = ", ".join(sorted(collisions))
            raise CoreRailValidationError(
                f"catalog details cannot replace protected fields: {names}"
            )
        return {
            "id": item_id,
            "kind": kind,
            "label": label,
            "target_usd": str(target),
            "pricing": pricing_source,
            "amount_tlama": self.whole_tokens(target),
            **dict(details or {}),
        }