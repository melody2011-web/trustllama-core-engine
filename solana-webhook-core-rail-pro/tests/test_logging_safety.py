"""Regression tests for provider-safe operational failure logging."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import aiohttp

from solana_webhook_core_rail_pro.rpc import SolanaRpcClient, SolanaRpcError
from solana_webhook_core_rail_pro.webhook import WebhookDelivery


PROVIDER_ENDPOINT = (
    "https://user:password@rpc.example.test/"
    "?api-key=provider-secret&X-Amz-Signature=signed-secret"
)
PROVIDER_BODY = "Authorization: Bearer bearer-secret detail=retryable"
SECRETS = ("password", "provider-secret", "signed-secret", "bearer-secret")


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def text(self) -> str:
        return self._body

    async def json(self) -> dict[str, object]:
        return {}

    async def read(self) -> bytes:
        return self._body.encode()


class _Session:
    closed = False

    def __init__(self, response: _Response) -> None:
        self.response = response

    def post(self, *_: object, **__: object) -> _Response:
        return self.response


class ProviderFailureLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_rpc_failure_log_redacts_endpoint_and_exception_detail(self) -> None:
        client = SolanaRpcClient((PROVIDER_ENDPOINT,))
        client._session = _Session(_Response(503, PROVIDER_BODY))  # type: ignore[assignment]

        with self.assertLogs(
            "solana_webhook_core_rail_pro.rpc",
            level="WARNING",
        ) as logs:
            with self.assertRaises(SolanaRpcError):
                await client.call("getBalance", [])

        message = logs.records[0].message
        self.assertIn("method=getBalance", message)
        self.assertIn("endpoint=https://<redacted>@rpc.example.test/", message)
        self.assertIn("status=503", message)
        self.assertIn("error_type=SolanaRpcError", message)
        self.assertIn("Authorization: <redacted>", message)
        for secret in SECRETS:
            self.assertNotIn(secret, message)

    async def test_webhook_failure_log_and_result_redact_provider_details(self) -> None:
        delivery = WebhookDelivery(
            url=PROVIDER_ENDPOINT,
            secret="webhook-secret",
            max_attempts=1,
        )
        delivery._session = _Session(_Response(503, PROVIDER_BODY))  # type: ignore[assignment]

        with self.assertLogs(
            "solana_webhook_core_rail_pro.webhook",
            level="ERROR",
        ) as logs:
            result = await delivery.deliver({"event": "solana.transaction"})

        message = logs.records[0].message
        self.assertIn("endpoint=https://<redacted>@rpc.example.test/", message)
        self.assertIn("status=503", message)
        self.assertIn("error_type=HTTPStatusError", message)
        self.assertIn("Authorization: <redacted>", message)
        self.assertIsNotNone(result.error)
        for secret in SECRETS:
            self.assertNotIn(secret, message)
            self.assertNotIn(secret, result.error or "")

    async def test_transport_failure_log_keeps_exception_type_without_traceback(self) -> None:
        delivery = WebhookDelivery(
            url=PROVIDER_ENDPOINT,
            secret="webhook-secret",
            max_attempts=1,
        )
        delivery._session = _Session(_Response(200, ""))  # type: ignore[assignment]
        delivery._session.post = patch.object(  # type: ignore[method-assign]
            delivery._session,
            "post",
            side_effect=aiohttp.ClientConnectionError(PROVIDER_BODY),
        ).start()

        try:
            with self.assertLogs(
                "solana_webhook_core_rail_pro.webhook",
                level="ERROR",
            ) as logs:
                result = await delivery.deliver({"event": "solana.transaction"})
        finally:
            patch.stopall()

        self.assertFalse(result.success)
        message = logs.records[0].message
        self.assertIn("error_type=ClientConnectionError", message)
        self.assertNotIn("Traceback", message)
        for secret in SECRETS:
            self.assertNotIn(secret, message)


if __name__ == "__main__":
    unittest.main()