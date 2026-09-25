"""Layer 3 payment-rail adapter for mock fighting-game entry validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import os

from layer3_core_rail import (
    CoreRailValidationError,
    Layer3CoreRail,
    TOKEN_DECIMALS,
    validate_transaction_limit,
    validate_wallet_limit,
)

MATCH_ENTRY_TARGET_USD = Decimal("0.25")
MATCH_ENTRY_ITEM_ID = "fighting-game-match-entry"
DEFAULT_MOCK_TOKEN_PRICE_USD = "0.01"


@dataclass(frozen=True)
class MatchEntryValidation:
    accepted: bool
    item_id: str
    target_usd: str
    token_price_usd: str
    amount_tlama: int
    amount_atomic: int
    pricing_source: str


class FightingGameEntryRail:
    """Game-specific adapter over the shared, game-neutral Layer 3 rail."""

    def __init__(self, token_price_usd: Decimal | str | None = None) -> None:
        configured_price = token_price_usd or os.environ.get(
            "FIGHTING_GAME_MOCK_TOKEN_PRICE_USD",
            DEFAULT_MOCK_TOKEN_PRICE_USD,
        )
        self._token_price_usd = Decimal(str(configured_price))
        self._rail = Layer3CoreRail(token_price_usd=self._token_price_usd)

    def validate(self) -> MatchEntryValidation:
        catalog_item = self._rail.price_catalog_item(
            item_id=MATCH_ENTRY_ITEM_ID,
            kind="match-entry",
            label="Competitive Fighting Match Entry",
            target_usd=MATCH_ENTRY_TARGET_USD,
            pricing_source="layer3-dynamic-oracle-mock",
            details={"game_layer": 2},
        )
        amount_atomic = self._rail.quote_atomic(
            MATCH_ENTRY_TARGET_USD, TOKEN_DECIMALS
        )

        # These are preflight checks only. On-chain programs remain authoritative.
        validate_transaction_limit(amount_atomic)
        validate_wallet_limit(
            current_base_units=0,
            incoming_base_units=amount_atomic,
        )

        return MatchEntryValidation(
            accepted=True,
            item_id=MATCH_ENTRY_ITEM_ID,
            target_usd=str(MATCH_ENTRY_TARGET_USD),
            token_price_usd=str(self._token_price_usd),
            amount_tlama=int(catalog_item["amount_tlama"]),
            amount_atomic=amount_atomic,
            pricing_source=str(catalog_item["pricing"]),
        )


__all__ = [
    "CoreRailValidationError",
    "FightingGameEntryRail",
    "MATCH_ENTRY_ITEM_ID",
    "MATCH_ENTRY_TARGET_USD",
    "MatchEntryValidation",
]