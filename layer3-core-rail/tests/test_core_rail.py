from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from layer3_core_rail import (  # noqa: E402
    BUY_BURN_BPS,
    MAX_TRANSACTION_BASE_UNITS,
    MAX_WALLET_BASE_UNITS,
    SELL_BURN_BPS,
    CoreRailValidationError,
    Layer3CoreRail,
    calculate_directional_burn,
)
from trust_llama_setup import (  # noqa: E402
    TLAMA_MAX_TRANSACTION_BASE_UNITS,
    TLAMA_MAX_WALLET_HOLDING_BASE_UNITS,
    TrustLlamaAntiWhalePolicy,
    TrustLlamaSetupError,
)


class Layer3CoreRailTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rail = Layer3CoreRail(token_price_usd=Decimal("0.01"))

    def test_router_uses_ceiling_without_float_rounding(self) -> None:
        item = self.rail.price_catalog_item(
            item_id="future-game-entry",
            kind="entry",
            label="Future game entry",
            target_usd=Decimal("0.25"),
            pricing_source="test-oracle",
        )
        self.assertEqual(item["amount_tlama"], 25)
        self.assertEqual(self.rail.quote_atomic("0.25", 9), 25_000_000_000)

    def test_catalog_details_cannot_replace_canonical_price_fields(self) -> None:
        for protected in (
            "id",
            "kind",
            "label",
            "target_usd",
            "pricing",
            "amount_tlama",
        ):
            with self.subTest(protected=protected), self.assertRaises(
                CoreRailValidationError
            ):
                self.rail.price_catalog_item(
                    item_id="future-game-entry",
                    kind="entry",
                    label="Future game entry",
                    target_usd="0.25",
                    pricing_source="test-oracle",
                    details={protected: "replacement"},
                )

    def test_directional_burn_matches_onchain_vectors(self) -> None:
        self.assertEqual(BUY_BURN_BPS, 50)
        self.assertEqual(SELL_BURN_BPS, 100)
        self.assertEqual(calculate_directional_burn(10_001, "buy"), 51)
        self.assertEqual(calculate_directional_burn(10_001, "sell"), 101)

    def test_single_interface_enforces_transaction_and_wallet_caps(self) -> None:
        with self.assertRaises(CoreRailValidationError):
            self.rail.assess_swap(
                direction="sell",
                gross_base_units=MAX_TRANSACTION_BASE_UNITS + 1,
            )
        with self.assertRaises(CoreRailValidationError):
            self.rail.assess_swap(
                direction="buy",
                gross_base_units=10_000,
                current_wallet_base_units=MAX_WALLET_BASE_UNITS,
            )

    def test_offline_setup_delegates_to_the_shared_boundary(self) -> None:
        policy = TrustLlamaAntiWhalePolicy()
        policy.validate_trade_amount(TLAMA_MAX_TRANSACTION_BASE_UNITS)
        policy.validate_wallet_holding(
            current_base_units=TLAMA_MAX_WALLET_HOLDING_BASE_UNITS - 1,
            incoming_base_units=1,
        )
        with self.assertRaisesRegex(TrustLlamaSetupError, "positive integer"):
            policy.validate_trade_amount(True)
        with self.assertRaisesRegex(TrustLlamaSetupError, "transaction limit"):
            policy.validate_trade_amount(TLAMA_MAX_TRANSACTION_BASE_UNITS + 1)
        with self.assertRaisesRegex(TrustLlamaSetupError, "valid integers"):
            policy.validate_wallet_holding(
                current_base_units=-1,
                incoming_base_units=1,
            )
        with self.assertRaisesRegex(TrustLlamaSetupError, "wallet limit"):
            policy.validate_wallet_holding(
                current_base_units=TLAMA_MAX_WALLET_HOLDING_BASE_UNITS,
                incoming_base_units=1,
            )


if __name__ == "__main__":
    unittest.main()