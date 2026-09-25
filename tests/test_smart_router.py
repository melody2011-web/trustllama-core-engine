from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from bot.smart_router import SmartRouterBridge


class SmartRouterBridgeTests(TestCase):
    @patch("bot.smart_router.shutil.which", return_value="/usr/bin/node")
    @patch("bot.smart_router.subprocess.run")
    def test_quote_child_receives_only_rpc_endpoint(self, run, _which) -> None:
        run.return_value = SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": True,
                    "amountOut": "42",
                    "routeCount": 1,
                    "candidatePools": {"v2": 1, "v3": 2, "stable": 3},
                }
            )
        )
        os.environ["WALLET_PRIVATE_KEY"] = "should-not-reach-node"
        quote = SmartRouterBridge("https://public.example").quote("XRP", "BTC", 10)

        self.assertEqual(quote.amount_out, 42)
        self.assertEqual(
            run.call_args.kwargs["env"],
            {"BSC_RPC_URL": "https://public.example"},
        )

    @patch("bot.smart_router.shutil.which", return_value="/usr/bin/node")
    @patch("bot.smart_router.subprocess.run")
    def test_direct_v3_build_trade_returns_validated_router_calldata_without_wallet_secret(
        self, run, _which
    ) -> None:
        run.return_value = SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": True,
                    "amountOut": "42",
                    "routeCount": 1,
                    "candidatePools": {"v2": 0, "v3": 1, "stable": 0},
                    "routerAddress": "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4",
                    "calldata": "0xac9650d8",
                    "value": "0",
                    "minimumOut": "40",
                }
            )
        )

        trade = SmartRouterBridge("https://public.example").build_trade(
            "USDT",
            "XRP",
            10,
            recipient="0x0000000000000000000000000000000000000000",
            slippage_bps=100,
            deadline=2_000_000_000,
            direct_v3=True,
        )

        self.assertEqual(trade.amount_out, 42)
        self.assertEqual(trade.value, 0)
        self.assertEqual(trade.minimum_out, 40)
        self.assertEqual(trade.calldata, "0xac9650d8")
        request = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(request["operation"], "build")
        self.assertEqual(request["routePolicy"], "direct_v3")
        self.assertFalse(request["nativeInput"])
        self.assertFalse(request["nativeOutput"])
        self.assertEqual(run.call_args.kwargs["env"], {"BSC_RPC_URL": "https://public.example"})

    @patch("bot.smart_router.shutil.which", return_value="/usr/bin/node")
    @patch("bot.smart_router.subprocess.run")
    def test_explicit_bridge_failure_reports_each_bridge(self, run, _which) -> None:
        run.return_value = SimpleNamespace(
            stdout=json.dumps(
                {
                    "ok": False,
                    "reason": "no_valid_explicit_bridge_route",
                    "candidatePools": {"v2": 0, "v3": 0, "stable": 0},
                    "bridgeAttempts": [
                        {
                            "bridge": {"symbol": "WBNB"},
                            "candidatePools": {"v2": 0, "v3": 0, "stable": 0},
                        },
                        {
                            "bridge": {"symbol": "USDT"},
                            "candidatePools": {"v2": 0, "v3": 0, "stable": 0},
                        },
                    ],
                }
            )
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"explicit bridges \(WBNB: V2=0, V3=0, stable=0, USDT: V2=0, V3=0, stable=0\)",
        ):
            SmartRouterBridge("https://public.example").quote("XRP", "BTC", 10)