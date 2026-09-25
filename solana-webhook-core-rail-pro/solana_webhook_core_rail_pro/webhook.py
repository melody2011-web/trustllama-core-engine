"""Reliable async webhook delivery with HMAC authentication and backoff."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from typing import Any

import aiohttp

from .logging_safety import safe_exception_detail, safe_log_detail
from .observability import DashboardMetrics
from .retry import retry_schedule
from .signing import SIGNATURE_HEADER, sign_payload

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    success: bool
    attempts: int
    status: int | None
    dropped: bool
    error: str | None = None


class WebhookDelivery:
    """POST JSON events and retry every non-200 response or transport error."""

    def __init__(
        self,
        *,
        url: str,
        secret: str,
        timeout_seconds: float = 10.0,
        max_attempts: int = 5,
        base_backoff_seconds: float = 2.0,
        metrics: DashboardMetrics | None = None,
    ) -> None:
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if not math.isfinite(base_backoff_seconds) or base_backoff_seconds <= 0:
            raise ValueError(
                "base_backoff_seconds must be finite and greater than zero"
            )
        self._retry_schedule = retry_schedule(base_backoff_seconds, max_attempts)
        self._url = url
        self._secret = secret
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_attempts = max_attempts
        self._base_backoff_seconds = base_backoff_seconds
        self._metrics = metrics
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "WebhookDelivery":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @staticmethod
    def serialize_payload(payload: dict[str, Any]) -> bytes:
        """Create deterministic compact JSON bytes for signing and transmission."""
        return json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")

    async def deliver(self, payload: dict[str, Any]) -> DeliveryResult:
        if self._session is None:
            await self.start()
        assert self._session is not None

        body = self.serialize_payload(payload)
        signature = sign_payload(body, self._secret)
        event_name = str(payload.get("event", "unknown"))
        event_signature = str(payload.get("signature", "unknown"))
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
        }
        last_error: str | None = None
        last_error_type: str | None = None
        last_status: int | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                async with self._session.post(
                    self._url,
                    data=body,
                    headers=headers,
                ) as response:
                    last_status = response.status
                    if self._metrics is not None:
                        self._metrics.record_delivery_attempt(
                            signature=event_signature,
                            event_name=event_name,
                            attempt=attempt,
                            status=response.status,
                            error=None if response.status == 200 else f"HTTP {response.status}",
                        )
                    if response.status == 200:
                        await response.read()
                        LOGGER.info(
                            "Webhook delivered event=%s attempt=%s",
                            payload.get("event", "unknown"),
                            attempt,
                        )
                        result = DeliveryResult(True, attempt, response.status, False)
                        if self._metrics is not None:
                            self._metrics.record_delivery_result(
                                signature=event_signature,
                                event_name=event_name,
                                attempts=result.attempts,
                                status=result.status,
                                success=result.success,
                                dropped=result.dropped,
                                error=result.error,
                            )
                        return result
                    response_body = (await response.text())[:300]
                    last_error_type = "HTTPStatusError"
                    last_error = (
                        f"HTTP {response.status}: {safe_log_detail(response_body)}"
                    )
            except (aiohttp.ClientError, TimeoutError) as exc:
                last_error_type = type(exc).__name__
                last_error = f"{type(exc).__name__}: {safe_exception_detail(exc)}"
                if self._metrics is not None:
                    self._metrics.record_delivery_attempt(
                        signature=event_signature,
                        event_name=event_name,
                        attempt=attempt,
                        status=None,
                        error=last_error,
                    )

            if attempt < self._max_attempts:
                delay = self._retry_schedule[attempt - 1]
                if self._metrics is not None:
                    self._metrics.record_retry(
                        signature=event_signature,
                        attempt=attempt,
                        max_attempts=self._max_attempts,
                        delay_seconds=delay,
                        error=last_error,
                    )
                LOGGER.warning(
                    "Webhook delivery failed endpoint=%s attempt=%s/%s "
                    "retry_in=%.1fs error_type=%s detail=%s",
                    safe_log_detail(self._url),
                    attempt,
                    self._max_attempts,
                    delay,
                    last_error_type,
                    safe_log_detail(last_error),
                )
                await asyncio.sleep(delay)

        LOGGER.error(
            "Permanent webhook failure endpoint=%s after %s attempts "
            "status=%s error_type=%s detail=%s",
            safe_log_detail(self._url),
            self._max_attempts,
            last_status,
            last_error_type or "UnknownError",
            safe_log_detail(last_error) if last_error else "<no detail>",
        )
        result = DeliveryResult(
            success=False,
            attempts=self._max_attempts,
            status=last_status,
            dropped=True,
            error=last_error,
        )
        if self._metrics is not None:
            self._metrics.record_delivery_result(
                signature=event_signature,
                event_name=event_name,
                attempts=result.attempts,
                status=result.status,
                success=result.success,
                dropped=result.dropped,
                error=result.error,
            )
        return result
