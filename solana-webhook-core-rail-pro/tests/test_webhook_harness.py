"""Independent localhost harness for signed webhook delivery.

Run from this directory with:
    uv run python -m unittest discover -s tests -p 'test_*.py'
"""

from __future__ import annotations

import asyncio
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import web

from solana_webhook_core_rail_pro import cli
from solana_webhook_core_rail_pro.cli import replay_dead_letters
from solana_webhook_core_rail_pro.config import Settings, StagingDistribution
from solana_webhook_core_rail_pro.models import TransactionEvent
from solana_webhook_core_rail_pro.signing import SIGNATURE_HEADER, verify_signature
from solana_webhook_core_rail_pro.state import DurableState
from solana_webhook_core_rail_pro.webhook import DeliveryResult, WebhookDelivery


SECRET = "local-harness-secret-that-is-long-enough"


class SignedWebhookHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.received_attempts: list[tuple[bytes, str | None]] = []
        self.attempts = 0
        self.failures_before_success = 0
        self.app = web.Application()
        self.app.router.add_post("/webhook", self._handler)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}/webhook"
    async def asyncSetUp(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.received_attempts: list[tuple[bytes, str | None]] = []
        self.attempts = 0
        self.failures_before_success = 0
        self.app = web.Application()
        self.app.router.add_post("/webhook", self._handler)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}/webhook"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def _handler(self, request: web.Request) -> web.Response:
        self.attempts += 1
        body = await request.read()
        signature = request.headers.get(SIGNATURE_HEADER)
        self.received_attempts.append((body, signature))
        self.assertTrue(verify_signature(body, signature, SECRET))
        if self.failures_before_success and self.attempts <= self.failures_before_success:
            return web.Response(status=503, text="try again")
        payload = json.loads(body)
        self.assertEqual(payload["event"], "solana.transaction")
        self.received.append(payload)
        return web.Response(status=200, text="accepted")

    async def test_mocked_transaction_is_signed_and_accepted(self) -> None:
        event = TransactionEvent.from_rpc_result(
            signature="5HmockedSignatureForTheHarness",
            source_address="11111111111111111111111111111111",
            result={
                "slot": 123456,
                "blockTime": 1_700_000_000,
                "transaction": {
                    "message": {
                        "accountKeys": [],
                        "instructions": [],
                    },
                    "signatures": ["5HmockedSignatureForTheHarness"],
                },
                "meta": {"err": None, "fee": 5000},
            },
        )
        async with WebhookDelivery(
            url=self.url,
            secret=SECRET,
            timeout_seconds=2,
            max_attempts=1,
        ) as delivery:
            result = await delivery.deliver(
                event.webhook_payload(
                    "localnet",
                    settlement_route="primary-product-settlement",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0]["signature"], "5HmockedSignatureForTheHarness")
        self.assertEqual(
            self.received[0]["settlement_route"],
            "primary-product-settlement",
        )

    async def test_non_200_uses_exponential_retry_until_200(self) -> None:
        self.failures_before_success = 3

        async with WebhookDelivery(
            url=self.url,
            secret=SECRET,
            timeout_seconds=2,
            max_attempts=5,
            base_backoff_seconds=0.001,
        ) as delivery:
            result = await delivery.deliver(
                {"event": "solana.transaction", "value": 42}
            )

        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(self.attempts, 4)

    async def test_retries_keep_route_payload_bytes_and_signature_identical(self) -> None:
        self.failures_before_success = 3
        event = TransactionEvent.from_rpc_result(
            signature="5HretrySignatureForTheHarness",
            source_address="11111111111111111111111111111111",
            result={
                "slot": 123456,
                "blockTime": 1_700_000_000,
                "transaction": {
                    "message": {
                        "accountKeys": [],
                        "instructions": [],
                    },
                    "signatures": ["5HretrySignatureForTheHarness"],
                },
                "meta": {"err": None, "fee": 5000},
            },
        )

        async with WebhookDelivery(
            url=self.url,
            secret=SECRET,
            timeout_seconds=2,
            max_attempts=5,
            base_backoff_seconds=0.001,
        ) as delivery:
            result = await delivery.deliver(
                event.webhook_payload(
                    "localnet",
                    settlement_route="primary-product-settlement",
                )
            )

        self.assertTrue(result.success)
        self.assertEqual(result.attempts, 4)
        self.assertEqual(self.attempts, 4)
        self.assertEqual(len(self.received_attempts), 4)

        bodies = [body for body, _ in self.received_attempts]
        signatures = [signature for _, signature in self.received_attempts]
        self.assertEqual(bodies, [bodies[0]] * 4)
        self.assertEqual(signatures, [signatures[0]] * 4)
        self.assertEqual(json.loads(bodies[0])["settlement_route"], "primary-product-settlement")

    async def test_replay_preserves_dead_letter_and_skips_successful_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            payload = {
                "event": "solana.transaction",
                "signature": "replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "slot": 42,
                "settlement_route": "primary-product-settlement",
            }
            state = DurableState(state_path)
            dead_letter_id = state.add_dead_letter(
                payload,
                DeliveryResult(False, 5, 503, True, "receiver unavailable"),
            )
            state.close()

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
            )

            first = await replay_dead_letters(settings, [dead_letter_id])
            self.assertEqual(first[0]["outcome"], "delivered")
            self.assertEqual(self.received, [payload])

            second = await replay_dead_letters(settings, [dead_letter_id])
            self.assertEqual(second[0]["outcome"], "skipped")
            self.assertEqual(self.attempts, 1)

            state = DurableState(state_path)
            self.assertEqual(state.dead_letter_count(), 1)
            audits = state.list_replay_outcomes(dead_letter_id=dead_letter_id)
            self.assertEqual([audit["outcome"] for audit in audits], ["skipped", "delivered"])
            state.close()

            forced = await replay_dead_letters(settings, [dead_letter_id], force=True)
            self.assertEqual(forced[0]["outcome"], "delivered")
            self.assertEqual(self.attempts, 2)

            self.failures_before_success = 100
            state = DurableState(state_path)
            failed_id = state.add_dead_letter(
                {
                    **payload,
                    "signature": "replay-failure-signature",
                },
                DeliveryResult(False, 5, 503, True, "receiver unavailable"),
            )
            state.close()
            failed = await replay_dead_letters(settings, [failed_id])
            self.assertEqual(failed[0]["outcome"], "failed")
            state = DurableState(state_path)
            self.assertEqual(
                state.list_replay_outcomes(dead_letter_id=failed_id)[0]["outcome"],
                "failed",
            )
            self.assertEqual(state.dead_letter_count(), 2)
            state.close()

    async def test_cli_rejects_selection_over_replay_limit_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            payload = {
                "event": "solana.transaction",
                "signature": "oversized-selection-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "slot": 42,
                "settlement_route": "primary-product-settlement",
            }
            state = DurableState(state_path)
            first_id = state.add_dead_letter(
                payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            second_id = state.add_dead_letter(
                {
                    **payload,
                    "signature": "second-oversized-selection-signature",
                },
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            state.close()

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
            )

            stderr = StringIO()
            with (
                patch.object(cli.Settings, "from_env", return_value=settings),
                patch.object(
                    sys,
                    "argv",
                    [
                        "solana-webhook-core-rail-pro",
                        "replay-dead-letters",
                        "--id",
                        str(first_id),
                        "--id",
                        str(second_id),
                        "--limit",
                        "1",
                    ],
                ),
                redirect_stderr(stderr),
            ):
                with self.assertRaises(SystemExit) as raised:
                    await asyncio.to_thread(cli.main)

            self.assertEqual(raised.exception.code, cli.REPLAY_SELECTION_REJECTED_EXIT_CODE)
            self.assertIn(
                "Replay failed: at most 1 dead-letter records may be selected",
                stderr.getvalue(),
            )

            self.assertEqual(self.attempts, 0)
            self.assertEqual(self.received_attempts, [])
            state = DurableState(state_path)
            self.assertEqual(state.list_replay_outcomes(), [])
            state.close()

    async def test_cli_rejects_non_positive_replay_limit_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            payload = {
                "event": "solana.transaction",
                "signature": "non-positive-limit-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "slot": 42,
                "settlement_route": "primary-product-settlement",
            }
            state = DurableState(state_path)
            dead_letter_id = state.add_dead_letter(
                payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            state.close()

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
            )

            with (
                patch.object(cli.Settings, "from_env", return_value=settings),
                patch.object(
                    sys,
                    "argv",
                    [
                        "solana-webhook-core-rail-pro",
                        "replay-dead-letters",
                        "--id",
                        str(dead_letter_id),
                        "--limit",
                        "0",
                    ],
                ),
            ):
                with self.assertRaisesRegex(
                    SystemExit,
                    r"Replay failed: limit must be at least 1",
                ):
                    await asyncio.to_thread(cli.main)

            self.assertEqual(self.attempts, 0)
            self.assertEqual(self.received_attempts, [])
            state = DurableState(state_path)
            self.assertEqual(state.list_replay_outcomes(), [])
            state.close()

    async def test_replay_marks_legacy_route_as_unknown_after_route_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            current_payload = {
                "event": "solana.transaction",
                "signature": "route-bearing-replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "settlement_route": "previous-settlement-route",
            }
            legacy_payload = {
                "event": "solana.transaction",
                "signature": "legacy-replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
            }
            malformed_payload = {
                "event": "solana.transaction",
                "signature": "malformed-route-replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "settlement_route": {"route": "not-a-string"},
            }
            blank_payload = {
                "event": "solana.transaction",
                "signature": "blank-route-replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "settlement_route": "   ",
            }
            state = DurableState(state_path)
            current_id = state.add_dead_letter(
                current_payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            legacy_id = state.add_dead_letter(
                legacy_payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            malformed_id = state.add_dead_letter(
                malformed_payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            blank_id = state.add_dead_letter(
                blank_payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            records = state.list_dead_letters()
            self.assertEqual(
                {record["settlement_route_status"] for record in records},
                {"recorded", "legacy_unknown"},
            )
            state.close()

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
                staging_distribution=StagingDistribution(
                    environment="staging",
                    visibility="public",
                    enabled=True,
                    url="https://example.test/staging",
                    route="current-settlement-route",
                    metadata_layout="metadata.json",
                    metadata_layout_path=Path("metadata.json"),
                ),
            )

            stdout = StringIO()
            with (
                patch.object(cli.Settings, "from_env", return_value=settings),
                patch.object(
                    sys,
                    "argv",
                    [
                        "solana-webhook-core-rail-pro",
                        "replay-dead-letters",
                        "--id",
                        str(current_id),
                        "--id",
                        str(legacy_id),
                        "--id",
                        str(malformed_id),
                        "--id",
                        str(blank_id),
                        "--dry-run",
                    ],
                ),
                redirect_stdout(stdout),
            ):
                await asyncio.to_thread(cli.main)

            output = json.loads(stdout.getvalue())
            records = output["records"]
            self.assertEqual(
                [record["dead_letter_id"] for record in records],
                [current_id, legacy_id, malformed_id, blank_id],
            )
            self.assertEqual(
                [record["settlement_route"] for record in records],
                ["previous-settlement-route", None, None, None],
            )
            self.assertEqual(
                [record["settlement_route_status"] for record in records],
                ["recorded", "legacy_unknown", "legacy_unknown", "legacy_unknown"],
            )
            self.assertEqual(
                [record["outcome"] for record in records],
                ["dry-run"] * 4,
            )
            self.assertNotIn(settings.settlement_route, stdout.getvalue())
            self.assertEqual(self.attempts, 0)
            self.assertEqual(self.received_attempts, [])
            state = DurableState(state_path)
            self.assertEqual(state.list_replay_outcomes(), [])
            state.close()

            previews = await replay_dead_letters(
                settings,
                [current_id, legacy_id, malformed_id, blank_id],
                dry_run=True,
            )
            self.assertEqual(
                [preview["settlement_route_status"] for preview in previews],
                ["recorded", "legacy_unknown", "legacy_unknown", "legacy_unknown"],
            )
            self.assertEqual(
                [preview["settlement_route"] for preview in previews],
                ["previous-settlement-route", None, None, None],
            )
            self.assertNotIn(
                settings.settlement_route,
                [preview["settlement_route"] for preview in previews],
            )

            outcomes = await replay_dead_letters(
                settings,
                [current_id, legacy_id, malformed_id, blank_id],
            )

            self.assertEqual(
                [outcome["settlement_route_status"] for outcome in outcomes],
                ["recorded", "legacy_unknown", "legacy_unknown", "legacy_unknown"],
            )
            self.assertEqual(self.received[0], current_payload)
            for payload in self.received[1:]:
                self.assertIsNone(payload["settlement_route"])
                self.assertEqual(
                    payload["settlement_route_status"],
                    "legacy_unknown",
                )
                self.assertNotEqual(payload["settlement_route"], settings.settlement_route)

    async def test_replay_cli_rejects_missing_ids_without_delivery_or_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            payload = {
                "event": "solana.transaction",
                "signature": "retained-replay-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "slot": 42,
                "settlement_route": "primary-product-settlement",

    async def test_replay_duplicate_protection_survives_audit_retention_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            payload = {
                "event": "solana.transaction",
                "signature": "retention-boundary-signature",
                "source_address": "11111111111111111111111111111111",
                "network": "localnet",
                "slot": 42,
                "settlement_route": "primary-product-settlement",
            }
            state = DurableState(state_path, max_replay_audit_entries=1)
            first_id = state.add_dead_letter(
                payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            newer_id = state.add_dead_letter(
                {
                    **payload,
                    "signature": "newer-audit-signature",
                },
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            state.close()

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
                replay_audit_max_entries=1,
            )

            first = await replay_dead_letters(settings, [first_id])
            self.assertEqual(first[0]["outcome"], "delivered")
            self.assertEqual(self.attempts, 1)

            newer = await replay_dead_letters(settings, [newer_id])
            self.assertEqual(newer[0]["outcome"], "delivered")
            self.assertEqual(self.attempts, 2)

            state = DurableState(state_path, max_replay_audit_entries=1)
            audits = state.list_replay_outcomes()
            self.assertEqual(len(audits), 1)
            self.assertEqual(
                {
                    key: audits[0][key]
                    for key in ("id", "dead_letter_id", "outcome", "attempts", "status", "error")
                },
                {
                    "id": newer[0]["replay_id"],
                    "dead_letter_id": newer_id,
                    "outcome": "delivered",
                    "attempts": 1,
                    "status": 200,
                    "error": None,
                },
            )
            self.assertTrue(state.has_successful_replay(first_id))
            state.close()

            skipped = await replay_dead_letters(settings, [first_id])
            self.assertEqual(skipped[0]["outcome"], "skipped")
            self.assertEqual(self.attempts, 2)
            state = DurableState(state_path, max_replay_audit_entries=1)
            self.assertEqual(
                [audit["outcome"] for audit in state.list_replay_outcomes()],
                ["skipped"],
            )
            state.close()

            forced = await replay_dead_letters(settings, [first_id], force=True)
            self.assertEqual(forced[0]["outcome"], "delivered")
            self.assertEqual(self.attempts, 3)
            }
            state = DurableState(state_path, max_dead_letters=1)
            evicted_id = state.add_dead_letter(
                {
                    **payload,
                    "signature": "evicted-replay-signature",
                },
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            retained_id = state.add_dead_letter(
                payload,
                DeliveryResult(False, 1, 503, True, "receiver unavailable"),
            )
            state.close()
            never_created_id = retained_id + 1000

            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url=self.url,
                webhook_secret=SECRET,
                webhook_timeout_seconds=2,
                webhook_max_attempts=1,
                webhook_base_backoff_seconds=0.001,
                state_path=state_path,
            )

            stderr = StringIO()
            with (
                patch.object(cli.Settings, "from_env", return_value=settings),
                patch.object(
                    sys,
                    "argv",
                    [
                        "solana-webhook-core-rail-pro",
                        "replay-dead-letters",
                        "--id",
                        str(retained_id),
                        "--id",
                        str(never_created_id),
                        "--id",
                        str(evicted_id),
                    ],
                ),
                redirect_stderr(stderr),
            ):
                with self.assertRaises(SystemExit) as raised:
                    await asyncio.to_thread(cli.main)

            self.assertEqual(raised.exception.code, cli.REPLAY_SELECTION_REJECTED_EXIT_CODE)
            self.assertIn(
                (
                    "Replay failed: selected dead-letter record(s) are not retained: "
                    f"{evicted_id}, {never_created_id}"
                ),
                stderr.getvalue(),
            )

            self.assertEqual(self.attempts, 0)
            self.assertEqual(self.received_attempts, [])
            self.assertEqual(self.received, [])
            state = DurableState(state_path)
            self.assertEqual(state.dead_letter_count(), 1)
            self.assertEqual(state.list_replay_outcomes(), [])
            state.close()


if __name__ == "__main__":
    unittest.main()
