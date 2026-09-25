from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase

from web3 import Web3

from bot.contracts import (
    PANCAKESWAP_V3_ROUTER,
    TOKENS,
    V3_ROUTER_ABI,
)
from bot.trader import PancakeSwapTrader, TraderError


class _QuoteCall:
    def __init__(self, encoded_path: bytes):
        self.encoded_path = encoded_path

    def call(self) -> tuple[int, list[int], list[int], int]:
        # Return a different deterministic amount for every fee combination so
        # the test can verify that the best fee-tier route is selected.
        return (
            int.from_bytes(self.encoded_path[20:23], byteorder="big")
            + int.from_bytes(self.encoded_path[43:46], byteorder="big"),
            [],
            [],
            0,
        )


class _QuoterFunctions:
    def quoteExactInput(self, encoded_path: bytes, _amount_in: int) -> _QuoteCall:
        return _QuoteCall(encoded_path)


class _NoLiquidityQuoterFunctions:
    def quoteExactInput(self, _encoded_path: bytes, _amount_in: int) -> _QuoteCall:
        class _NoLiquidityCall:
            def call(self) -> tuple[int, list[int], list[int], int]:
                raise ValueError("PancakeSwap V3 pool cannot quote this path")

        return _NoLiquidityCall()  # type: ignore[return-value]


class V3RouteTests(TestCase):
    def _trader(self) -> PancakeSwapTrader:
        trader = object.__new__(PancakeSwapTrader)
        trader.quoter = SimpleNamespace(functions=_QuoterFunctions())
        trader.settings = SimpleNamespace(slippage_bps=50)
        return trader

    def test_router_exact_input_includes_deadline(self) -> None:
        components = V3_ROUTER_ABI[0]["inputs"][0]["components"]
        self.assertEqual(
            [component["name"] for component in components],
            ["path", "recipient", "deadline", "amountIn", "amountOutMinimum"],
        )

        router = Web3().eth.contract(address=PANCAKESWAP_V3_ROUTER, abi=V3_ROUTER_ABI)
        encoded = router.functions.exactInput(
            (b"\x00" * 43, TOKENS["WBNB"], 1234567890, 10, 9)
        )._encode_transaction_data()
        self.assertTrue(encoded.startswith("0xc04b8d59"))

    def test_tracked_assets_are_verified_bnb_chain_binance_peg_contracts(self) -> None:
        self.assertEqual(
            TOKENS["XRP"], Web3.to_checksum_address("0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe")
        )
        self.assertEqual(
            TOKENS["BTC"], Web3.to_checksum_address("0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c")
        )

    def test_xrp_to_btc_quote_uses_usdt_bridge_and_best_fee_tier(self) -> None:
        route = self._trader()._quote_best(
            self._trader()._routes_for_tokens(
                (TOKENS["XRP"], TOKENS["USDT"], TOKENS["BTC"])
            ),
            10**18,
        )
        self.assertEqual(route.tokens, (TOKENS["XRP"], TOKENS["USDT"], TOKENS["BTC"]))
        self.assertEqual(route.fees, (10000, 10000))

    def test_btc_to_xrp_quote_uses_usdt_bridge_and_best_fee_tier(self) -> None:
        route = self._trader()._quote_best(
            self._trader()._routes_for_tokens(
                (TOKENS["BTC"], TOKENS["USDT"], TOKENS["XRP"])
            ),
            10**18,
        )
        self.assertEqual(route.tokens, (TOKENS["BTC"], TOKENS["USDT"], TOKENS["XRP"]))
        self.assertEqual(route.fees, (10000, 10000))

    def test_pair_quote_reports_unsupported_bridge_liquidity_clearly(self) -> None:
        trader = self._trader()
        trader.quoter = SimpleNamespace(functions=_NoLiquidityQuoterFunctions())

        with self.assertRaisesRegex(
            TraderError,
            r"Unsupported PancakeSwap V3 liquidity for XRP -> USDT -> BTC across fee tiers",
        ):
            trader.quote_pair("XRP", "BTC", "1")