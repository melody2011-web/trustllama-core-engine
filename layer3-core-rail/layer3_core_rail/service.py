from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .pricing import DynamicOraclePricingRouter
from .token_rules import SwapAssessment, SwapDirection, assess_swap


class Layer3CoreRail:
    """Single game-neutral interface for pricing and fixed token rules."""

    def __init__(self, *, token_price_usd: Decimal | str) -> None:
        self._pricing = DynamicOraclePricingRouter(token_price_usd)

    def price_catalog_item(
        self,
        *,
        item_id: str,
        kind: str,
        label: str,
        target_usd: Decimal | str,
        pricing_source: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._pricing.catalog_item(
            item_id=item_id,
            kind=kind,
            label=label,
            target_usd=target_usd,
            pricing_source=pricing_source,
            details=details,
        )

    def quote_atomic(self, target_usd: Decimal | str, token_decimals: int) -> int:
        return self._pricing.atomic_units(target_usd, token_decimals)

    def assess_swap(
        self,
        *,
        direction: SwapDirection,
        gross_base_units: int,
        current_wallet_base_units: int | None = None,
    ) -> SwapAssessment:
        return assess_swap(
            direction=direction,
            gross_base_units=gross_base_units,
            current_wallet_base_units=current_wallet_base_units,
        )