from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .contracts import TOKEN_DECIMALS, TOKENS


class SmartRouterError(RuntimeError):
    """Raised when the read-only Smart Router bridge cannot supply a quote."""


@dataclass(frozen=True)
class SmartRouterQuote:
    amount_out: int
    route_count: int
    v2_candidates: int
    v3_candidates: int
    stable_candidates: int

@dataclass(frozen=True)
class SmartRouterTrade(SmartRouterQuote):
    """A fresh Smart Router quote with validated transaction parameters."""

    router_address: str
    calldata: str
    value: int
    minimum_out: int
class SmartRouterBridge:
    """Runs read-only PancakeSwap factory price queries without wallet access."""

    _script = Path(__file__).with_name("smart-router.mjs")

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url

    @staticmethod
    def _token(symbol: str) -> dict[str, str | int]:
        normalized = symbol.upper()
        return {
            "symbol": normalized,
            "address": TOKENS[normalized],
            "decimals": TOKEN_DECIMALS[normalized],
        }

    def _request(self, request: dict[str, object]) -> dict[str, object]:
        node = shutil.which("node")
        if not node:
            raise SmartRouterError("Node.js is required for Smart Router quoting.")
        try:
            result = subprocess.run(
                [node, str(self._script)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
                # Keep the dependency-heavy Node quote process isolated from
                # wallet and application secrets. It needs only a public RPC.
                env={"BSC_RPC_URL": self.rpc_url},
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SmartRouterError("Smart Router quote process could not complete.") from error
        try:
            response: dict[str, object] = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SmartRouterError("Smart Router returned an invalid quote response.") from error
        return response

    @staticmethod
    def _quote_from_response(response: dict[str, object]) -> SmartRouterQuote:
        candidates = response.get("candidatePools", {})
        if not isinstance(candidates, dict):
            raise SmartRouterError("Smart Router returned invalid candidate pool data.")

        if response.get("ok") is not True:
            if response.get("reason") in {
                "no_valid_hybrid_route",
                "no_valid_explicit_bridge_route",
            }:
                bridge_attempts = response.get("bridgeAttempts", [])
                bridge_details = ", ".join(
                    (
                        f"{attempt.get('bridge', {}).get('symbol', 'unknown')}: "
                        f"V2={attempt.get('candidatePools', {}).get('v2', 0)}, "
                        f"V3={attempt.get('candidatePools', {}).get('v3', 0)}, "
                        f"stable={attempt.get('candidatePools', {}).get('stable', 0)}"
                    )
                    for attempt in bridge_attempts
                )
                raise SmartRouterError(
                    "No valid PancakeSwap Smart Router path was found"
                    + (f" via explicit bridges ({bridge_details})" if bridge_details else "")
                    + " "
                    f"(V2={candidates.get('v2', 0)}, V3={candidates.get('v3', 0)}, "
                    f"stable={candidates.get('stable', 0)} candidates)."
                )
            raise SmartRouterError(response.get("error", "Smart Router quote failed."))
        return SmartRouterQuote(
            amount_out=int(response["amountOut"]),
            route_count=int(response.get("routeCount", 1)),
            v2_candidates=int(candidates.get("v2", 0)),
            v3_candidates=int(candidates.get("v3", 0)),
            stable_candidates=int(candidates.get("stable", 0)),
        )

    def quote(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: int,
        *,
        direct_v3: bool = False,
    ) -> SmartRouterQuote:
        return self._quote_from_response(
            self._request(
                {
                    "operation": "quote",
                    "input": self._token(input_symbol),
                    "output": self._token(output_symbol),
                    "amountIn": str(amount_in),
                    "routePolicy": "direct_v3" if direct_v3 else "hybrid",
                }
            )
        )

    def build_trade(
        self,
        input_symbol: str,
        output_symbol: str,
        amount_in: int,
        *,
        recipient: str,
        slippage_bps: int,
        deadline: int,
        native_input: bool = False,
        native_output: bool = False,
        direct_v3: bool = False,
    ) -> SmartRouterTrade:
        response = self._request(
            {
                "operation": "build",
                "input": self._token(input_symbol),
                "output": self._token(output_symbol),
                "amountIn": str(amount_in),
                "recipient": recipient,
                "slippageBps": slippage_bps,
                "deadline": deadline,
                "nativeInput": native_input,
                "nativeOutput": native_output,
                "routePolicy": "direct_v3" if direct_v3 else "hybrid",
            }
        )
        quote = self._quote_from_response(response)
        try:
            router_address = str(response["routerAddress"])
            calldata = str(response["calldata"])
            value = int(response["value"])
            minimum_out = int(response["minimumOut"])
        except (KeyError, TypeError, ValueError) as error:
            raise SmartRouterError(
                "Smart Router did not return valid transaction parameters."
            ) from error
        if (
            not router_address.startswith("0x")
            or not calldata.startswith("0x")
            or value < 0
            or minimum_out < 0
        ):
            raise SmartRouterError("Smart Router returned unsafe transaction parameters.")
        return SmartRouterTrade(
            amount_out=quote.amount_out,
            route_count=quote.route_count,
            v2_candidates=quote.v2_candidates,
            v3_candidates=quote.v3_candidates,
            stable_candidates=quote.stable_candidates,
            router_address=router_address,
            calldata=calldata,
            value=value,
            minimum_out=minimum_out,
        )
