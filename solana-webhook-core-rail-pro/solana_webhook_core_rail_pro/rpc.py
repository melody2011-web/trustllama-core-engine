"""Async JSON-RPC client for public Solana endpoints."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Sequence
from typing import Any

import aiohttp

from .logging_safety import safe_exception_detail, safe_log_detail

LOGGER = logging.getLogger(__name__)


class SolanaRpcError(RuntimeError):
    """Raised for a malformed or failed Solana JSON-RPC response."""


class SolanaRpcClient:
    """Reusable, failover-aware async Solana RPC client."""

    def __init__(
        self,
        rpc_urls: Sequence[str],
        *,
        commitment: str = "confirmed",
        timeout_seconds: float = 10.0,
        max_concurrency: int = 16,
    ) -> None:
        if not rpc_urls:
            raise ValueError("At least one RPC URL is required")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        self._rpc_urls = tuple(rpc_urls)
        self._commitment = commitment
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._endpoint_index = 0
        self._request_id = 0
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def __aenter__(self) -> SolanaRpcClient:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Content-Type": "application/json"},
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def call(self, method: str, params: list[Any]) -> Any:
        if self._session is None:
            await self.start()
        assert self._session is not None

        last_error: Exception | None = None
        async with self._semaphore:
            for offset in range(len(self._rpc_urls)):
                endpoint_index = (self._endpoint_index + offset) % len(self._rpc_urls)
                endpoint = self._rpc_urls[endpoint_index]
                self._request_id += 1
                response_status: int | None = None
                request = {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params,
                }
                try:
                    async with self._session.post(endpoint, json=request) as response:
                        response_status = response.status
                        if response.status != 200:
                            body = (await response.text())[:300]
                            raise SolanaRpcError(
                                f"{method} returned HTTP {response.status}: {body}"
                            )
                        payload = await response.json()
                    if "error" in payload:
                        error = payload["error"]
                        raise SolanaRpcError(f"{method} returned RPC error: {error}")
                    if "result" not in payload:
                        raise SolanaRpcError(f"{method} response omitted result")
                    self._endpoint_index = endpoint_index
                    return payload["result"]
                except (TimeoutError, aiohttp.ClientError, SolanaRpcError) as exc:
                    last_error = exc
                    LOGGER.warning(
                        "Solana RPC request failed method=%s endpoint=%s "
                        "status=%s error_type=%s detail=%s",
                        method,
                        safe_log_detail(endpoint),
                        response_status,
                        type(exc).__name__,
                        safe_exception_detail(exc),
                    )

        raise SolanaRpcError(f"All RPC endpoints failed for {method}: {last_error}")

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 50,
        before: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        options: dict[str, Any] = {
            "commitment": self._commitment,
            "limit": min(limit, 1000),
        }
        if before is not None:
            options["before"] = before
        if until is not None:
            options["until"] = until
        result = await self.call(
            "getSignaturesForAddress",
            [
                address,
                options,
            ],
        )
        if not isinstance(result, list):
            raise SolanaRpcError("getSignaturesForAddress result was not a list")
        return [item for item in result if isinstance(item, dict)]

    async def get_transaction(self, signature: str) -> dict[str, Any] | None:
        result = await self.call(
            "getTransaction",
            [
                signature,
                {
                    "commitment": self._commitment,
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise SolanaRpcError("getTransaction result was not an object")
        return result