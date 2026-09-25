"""Regression tests for durable cursors and dead-letter recovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from solana_webhook_core_rail_pro.config import Settings, StagingDistribution
from solana_webhook_core_rail_pro.engine import CoreRailEngine
from solana_webhook_core_rail_pro.observability import DashboardMetrics
from solana_webhook_core_rail_pro.rpc import SolanaRpcClient, SolanaRpcError
from solana_webhook_core_rail_pro.state import DurableState
from solana_webhook_core_rail_pro.webhook import DeliveryResult

ADDRESS = "11111111111111111111111111111111"


def _transaction(signature: str) -> dict[str, Any]:
    return {
        "slot": 123,
        "blockTime": 1_700_000_000,
        "transaction": {"signatures": [signature], "message": {}},
        "meta": {"err": None},
    }


class FakeRpc:
    def __init__(self, history: list[str]) -> None:
        self.history = history
        self.signature_calls: list[dict[str, Any]] = []

    async def get_signatures_for_address(
        self,
        address: str,
        *,
        limit: int = 50,
        before: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        self.signature_calls.append(
            {"address": address, "limit": limit, "before": before, "until": until}
        )
        signatures = self.history
        if before is not None:
            signatures = signatures[signatures.index(before) + 1 :]
        if until is not None:
            signatures = signatures[: signatures.index(until) + 1]
        return [{"signature": signature} for signature in signatures[:limit]]

    async def get_transaction(self, signature: str) -> dict[str, Any]:
        return _transaction(signature)


class EmptyTransactionRpc(FakeRpc):
    async def get_transaction(self, signature: str) -> None:
        return None


class FailingTransactionRpc(FakeRpc):
    async def get_transaction(self, signature: str) -> dict[str, Any]:
        raise SolanaRpcError("getTransaction failed")


class FakeDelivery:
    def __init__(self, *, dropped: bool = False, failures: int = 0) -> None:
        self.dropped = dropped
        self.failures = failures
        self.delivered: list[str] = []

    async def deliver(self, payload: dict[str, Any]) -> DeliveryResult:
        self.delivered.append(payload["signature"])
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary worker failure")
        return DeliveryResult(
            success=not self.dropped,
            attempts=2 if self.dropped else 1,
            status=503 if self.dropped else 200,
            dropped=self.dropped,
            error="receiver unavailable" if self.dropped else None,
        )


class JsonRpcMock:
    def __init__(
        self,
        *,
        signature_pages: list[list[dict[str, Any]]],
        transaction_results: dict[str, dict[str, Any] | None],
        expected_signature_params: list[list[Any]],
    ) -> None:
        self._signature_pages = list(signature_pages)
        self._transaction_results = transaction_results
        self._expected_signature_params = expected_signature_params
        self.signature_params: list[list[Any]] = []
        self.transaction_signatures: list[str] = []
        self.errors: list[str] = []
        self.app = web.Application()
        self.app.router.add_post("/", self._handler)
        self.runner = web.AppRunner(self.app)
        self.site: web.TCPSite | None = None
        self.url: str | None = None

    async def start(self) -> None:
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        sockets = self.site._server.sockets  # type: ignore[union-attr]
        self.url = f"http://127.0.0.1:{sockets[0].getsockname()[1]}/"

    async def close(self) -> None:
        await self.runner.cleanup()

    async def _handler(self, request: web.Request) -> web.Response:
        payload = await request.json()
        method = payload.get("method")
        request_id = payload.get("id")
        if method == "getSignaturesForAddress":
            params = payload.get("params")
            self.signature_params.append(params)
            expected_index = len(self.signature_params) - 1
            if expected_index >= len(self._expected_signature_params):
                self.errors.append(f"unexpected signature request: {params!r}")
            elif params != self._expected_signature_params[expected_index]:
                self.errors.append(
                    "signature request mismatch: "
                    f"expected={self._expected_signature_params[expected_index]!r} "
                    f"actual={params!r}"
                )
            if not self._signature_pages:
                self.errors.append("signature response queue was exhausted")
                result: list[dict[str, Any]] = []
            else:
                result = self._signature_pages.pop(0)
        elif method == "getTransaction":
            signature = payload["params"][0]
            self.transaction_signatures.append(signature)
            if signature not in self._transaction_results:
                self.errors.append(f"unexpected transaction request: {signature}")
                result = None
            else:
                result = self._transaction_results[signature]
        else:
            self.errors.append(f"unexpected RPC method: {method!r}")
            result = None
        return web.json_response({"jsonrpc": "2.0", "id": request_id, "result": result})


def _settings(
    state_path: str,
    *,
    max_dead_letters: int = 2,
    replay_audit_max_entries: int = 1000,
    max_recovery_boundaries: int = 1000,
    settlement_route: str | None = None,
) -> Settings:
    staging_distribution = (
        StagingDistribution(
            environment="staging",
            visibility="public",
            enabled=True,
            url="https://staging.example.test/settlement",
            route=settlement_route,
            metadata_layout="metadata.json",
            metadata_layout_path=Path("metadata.json"),
        )
        if settlement_route is not None
        else None
    )
    return Settings(
        rpc_urls=("https://rpc.example.test",),
        commitment="confirmed",
        watch_addresses=(ADDRESS,),
        watch_signatures=(),
        webhook_url="https://webhook.example.test/events",
        webhook_secret="secret-that-is-long-enough",
        signature_limit=2,
        webhook_max_concurrency=1,
        state_path=state_path,
        dead_letter_max_entries=max_dead_letters,
        replay_audit_max_entries=replay_audit_max_entries,
        max_recovery_boundaries=max_recovery_boundaries,
        staging_distribution=staging_distribution,
    )


class DurableRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_transaction_records_discarded_timing_without_processing_block(
        self,
    ) -> None:
        state = DurableState(":memory:")
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            event_log_limit=1,
        )
        engine = CoreRailEngine(
            _settings(":memory:"),
            EmptyTransactionRpc([]),
            FakeDelivery(),
            metrics=metrics,
            state=state,
        )

        try:
            self.assertIsNone(await engine._get_transaction("signature-empty"))
            snapshot = metrics.snapshot()
        finally:
            state.close()

        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
        self.assertEqual(snapshot["telemetry"]["blocks_discarded"], 1)
        self.assertEqual(snapshot["streaming"]["blocks_processed"], 0)
        self.assertEqual(snapshot["streaming"]["blocks_discarded"], 1)
        self.assertLessEqual(len(snapshot["webhook_logs"]), 1)
        discard_logs = [
            entry
            for entry in snapshot["webhook_logs"]
            if entry["status"] == "block_discarded"
        ]
        self.assertEqual(len(discard_logs), 1)
        self.assertEqual(discard_logs[0]["signature"], "signature-empty")
        self.assertNotIn("payload", discard_logs[0])

    async def test_failed_transaction_records_discarded_timing_before_propagating(
        self,
    ) -> None:
        state = DurableState(":memory:")
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            event_log_limit=1,
        )
        engine = CoreRailEngine(
            _settings(":memory:"),
            FailingTransactionRpc([]),
            FakeDelivery(),
            metrics=metrics,
            state=state,
        )

        try:
            with self.assertRaisesRegex(SolanaRpcError, "getTransaction failed"):
                await engine._get_transaction("signature-failed")
            snapshot = metrics.snapshot()
        finally:
            state.close()

        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
        self.assertEqual(snapshot["telemetry"]["blocks_discarded"], 1)
        self.assertEqual(snapshot["streaming"]["blocks_processed"], 0)
        self.assertEqual(snapshot["streaming"]["blocks_discarded"], 1)
        self.assertLessEqual(len(snapshot["webhook_logs"]), 1)
        discard_logs = [
            entry
            for entry in snapshot["webhook_logs"]
            if entry["status"] == "block_discarded"
        ]
        self.assertEqual(len(discard_logs), 1)
        self.assertEqual(discard_logs[0]["signature"], "signature-failed")
        self.assertNotIn("payload", discard_logs[0])

    async def test_block_latency_metrics_keep_processing_alive_and_validate_values(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path)
            engine = CoreRailEngine(
                _settings(state_path),
                FakeRpc(["signature-1"]),
                FakeDelivery(),
                state=state,
            )

            transaction = await engine._get_transaction("signature-1")

            self.assertIsNotNone(transaction)
            telemetry = engine.metrics.snapshot()["telemetry"]
            self.assertEqual(telemetry["blocks_processed"], 1)
            self.assertGreaterEqual(
                telemetry["block_processing_latency_ms"]["last"],
                0.0,
            )

            for invalid_latency in (-1.0, float("nan"), float("inf"), float("-inf")):
                with self.subTest(invalid_latency=invalid_latency):
                    with self.assertRaisesRegex(
                        ValueError,
                        "block processing latency must be finite and non-negative",
                    ):
                        engine.metrics.record_block_processing(
                            "signature-invalid",
                            invalid_latency,
                        )

            self.assertEqual(engine.metrics.snapshot()["telemetry"]["blocks_processed"], 1)
            state.close()

    async def test_recovery_evidence_survives_engine_restart_as_known_incident(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            first_state = DurableState(state_path)
            first_metrics = DashboardMetrics(
                queue_capacity=10,
                signature_history_capacity=40,
            )
            first_state.set_cursor(ADDRESS, "pruned-cursor")
            first_metrics.record_recovery_boundary_missing(
                ADDRESS,
                "pruned-cursor",
                first_state,
            )
            first_state.close()

            restarted_state = DurableState(state_path)
            restarted_engine = CoreRailEngine(
                _settings(state_path),
                FakeRpc([]),
                FakeDelivery(),
                state=restarted_state,
            )
            recovery = restarted_engine.metrics.snapshot()["recovery"]
            self.assertEqual(recovery["boundary_missing_total"], 1)
            self.assertEqual(recovery["active_pruned_boundaries"], 1)
            self.assertEqual(
                recovery["pruned_boundaries"][0]["incident_status"],
                "known_unresolved",
            )
            self.assertFalse(recovery["pruned_boundaries"][0]["newly_detected"])
            restarted_state.close()

    async def test_recovery_boundary_retention_is_bounded_without_losing_total(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path, max_recovery_boundaries=2)
            for index in range(3):
                state.record_recovery_boundary_missing(
                    f"address-{index}",
                    f"cursor-{index}",
                )
            self.assertEqual(len(state.list_recovery_boundaries()), 2)
            self.assertEqual(state.recovery_boundary_missing_total(), 3)
            self.assertEqual(state.max_recovery_boundaries, 2)
            self.assertEqual(state.recovery_boundary_trimmed_total(), 1)
            evictions = state.list_recovery_boundary_evictions()
            self.assertEqual(len(evictions), 1)
            self.assertEqual(evictions[0]["address"], "address-0")
            self.assertEqual(evictions[0]["cursor"], "cursor-0")
            self.assertTrue(evictions[0]["evicted_at"])
            state.close()

            restarted_state = DurableState(
                state_path,
                max_recovery_boundaries=2,
            )
            self.assertEqual(len(restarted_state.list_recovery_boundaries()), 2)
            self.assertEqual(restarted_state.recovery_boundary_missing_total(), 3)
            self.assertEqual(restarted_state.max_recovery_boundaries, 2)
            self.assertEqual(restarted_state.recovery_boundary_trimmed_total(), 1)
            self.assertEqual(
                restarted_state.list_recovery_boundary_evictions(),
                evictions,
            )
            restarted_state.close()

    async def test_recovery_boundary_eviction_audit_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path, max_recovery_boundaries=2)
            for index in range(4):
                state.record_recovery_boundary_missing(
                    f"address-{index}",
                    f"cursor-{index}",
                )

            evictions = state.list_recovery_boundary_evictions()
            self.assertEqual(len(evictions), 2)
            self.assertEqual(
                [eviction["address"] for eviction in evictions],
                ["address-1", "address-0"],
            )
            state.close()

            restarted_state = DurableState(
                state_path,
                max_recovery_boundaries=2,
            )
            self.assertEqual(
                restarted_state.list_recovery_boundary_evictions(),
                evictions,
            )
            restarted_state.close()

    async def test_restart_applies_lower_recovery_capacity_without_losing_totals(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            first_state = DurableState(state_path, max_recovery_boundaries=4)
            for index in range(4):
                first_state.record_recovery_boundary_missing(
                    f"address-{index}",
                    f"cursor-{index}",
                )
            self.assertEqual(len(first_state.list_recovery_boundaries()), 4)
            self.assertEqual(first_state.recovery_boundary_missing_total(), 4)
            self.assertEqual(first_state.recovery_boundary_trimmed_total(), 0)
            first_state.close()

            restarted_state = DurableState(
                state_path,
                max_recovery_boundaries=2,
            )
            restarted_engine = CoreRailEngine(
                _settings(state_path, max_recovery_boundaries=2),
                FakeRpc([]),
                FakeDelivery(),
                state=restarted_state,
            )
            recovery = restarted_engine.metrics.snapshot()["recovery"]
            self.assertEqual(restarted_state.max_recovery_boundaries, 2)
            self.assertEqual(len(restarted_state.list_recovery_boundaries()), 2)
            self.assertEqual(recovery["active_evidence_limit"], 2)
            self.assertEqual(recovery["active_pruned_boundaries"], 2)
            self.assertEqual(recovery["boundary_missing_total"], 4)
            self.assertTrue(recovery["active_evidence_trimmed"])
            self.assertEqual(recovery["active_evidence_trimmed_total"], 2)

            restarted_state.record_recovery_boundary_missing(
                "address-new",
                "cursor-new",
            )
            self.assertEqual(len(restarted_state.list_recovery_boundaries()), 2)
            self.assertEqual(restarted_state.recovery_boundary_missing_total(), 5)
            self.assertEqual(restarted_state.recovery_boundary_trimmed_total(), 3)
            restarted_state.close()

    async def test_resolved_recovery_boundary_history_is_bounded_and_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(
                state_path,
                max_resolved_recovery_boundaries=2,
            )
            for index in range(3):
                address = f"address-{index}"
                state.record_recovery_boundary_missing(address, f"cursor-{index}")
                resolved = state.clear_recovery_boundary(address)
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved["address"], address)
                self.assertEqual(resolved["cursor"], f"cursor-{index}")
                self.assertIsInstance(resolved["resolved_at"], str)

            resolved_boundaries = state.list_resolved_recovery_boundaries()
            self.assertEqual(
                [boundary["address"] for boundary in resolved_boundaries],
                ["address-2", "address-1"],
            )
            self.assertEqual(state.list_recovery_boundaries(), [])
            state.close()

            restarted_state = DurableState(
                state_path,
                max_resolved_recovery_boundaries=2,
            )
            self.assertEqual(
                [
                    boundary["cursor"]
                    for boundary in restarted_state.list_resolved_recovery_boundaries()
                ],
                ["cursor-2", "cursor-1"],
            )
            restarted_state.close()

    async def test_restart_resumes_from_cursor_and_paginates_large_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")

            first_rpc = FakeRpc(["s2", "s1"])
            first_state = DurableState(state_path)
            first_engine = CoreRailEngine(
                _settings(state_path),
                first_rpc,
                FakeDelivery(),
                state=first_state,
            )
            self.assertEqual(await first_engine.poll_once(), 0)
            first_state.close()
            persisted_state = DurableState(state_path)
            self.assertEqual(persisted_state.get_cursor(ADDRESS), "s2")
            persisted_state.close()

            second_rpc = FakeRpc(["s5", "s4", "s3", "s2"])
            second_state = DurableState(state_path)
            delivery = FakeDelivery()
            second_engine = CoreRailEngine(
                _settings(state_path),
                second_rpc,
                delivery,
                state=second_state,
            )
            self.assertEqual(await second_engine.poll_once(), 3)
            self.assertEqual(delivery.delivered, ["s3", "s4", "s5"])
            self.assertEqual(second_state.get_cursor(ADDRESS), "s5")
            self.assertEqual(
                second_rpc.signature_calls,
                [
                    {
                        "address": ADDRESS,
                        "limit": 2,
                        "before": None,
                        "until": "s2",
                    },
                    {
                        "address": ADDRESS,
                        "limit": 2,
                        "before": "s4",
                        "until": "s2",
                    },
                ],
            )
            second_state.close()

    async def test_real_json_rpc_pagination_keeps_cursor_on_empty_or_missing_result(
        self,
    ) -> None:
        rpc_mock = JsonRpcMock(
            signature_pages=[
                [{"signature": "s5"}, {"signature": "s4"}],
                [{"signature": "s3"}, {"signature": "s2"}],
                [],
            ],
            transaction_results={
                "s3": _transaction("s3"),
                "s4": None,
                "s5": _transaction("s5"),
            },
            expected_signature_params=[
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s2",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "before": "s4",
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s2",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s2",
                    },
                ],
            ],
        )
        await rpc_mock.start()
        assert rpc_mock.url is not None

        state = DurableState(":memory:")
        state.set_cursor(ADDRESS, "s2")
        delivery = FakeDelivery()
        try:
            async with SolanaRpcClient((rpc_mock.url,), timeout_seconds=2) as rpc:
                engine = CoreRailEngine(
                    _settings(":memory:"),
                    rpc,
                    delivery,
                    state=state,
                )

                self.assertEqual(await engine.poll_once(), 2)
                self.assertEqual(delivery.delivered, ["s3", "s5"])
                self.assertEqual(state.get_cursor(ADDRESS), "s2")
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_pruned_boundaries"],
                    0,
                )
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["resolved_pruned_boundaries"],
                    [],
                )

                self.assertEqual(await engine.poll_once(), 0)
                self.assertEqual(state.get_cursor(ADDRESS), "s2")
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_pruned_boundaries"],
                    0,
                )
        finally:
            state.close()
            await rpc_mock.close()

        self.assertEqual(rpc_mock.errors, [])
        self.assertEqual(
            rpc_mock.signature_params,
            rpc_mock._expected_signature_params,
        )
        self.assertCountEqual(rpc_mock.transaction_signatures, ["s3", "s4", "s5"])

    async def test_missing_cursor_after_full_pages_commits_newest_available_signature(
        self,
    ) -> None:
        rpc_mock = JsonRpcMock(
            signature_pages=[
                [{"signature": "s5"}, {"signature": "s4"}],
                [{"signature": "s3"}, {"signature": "s2"}],
                [],
                [],
                [{"signature": "s5"}],
            ],
            transaction_results={
                signature: _transaction(signature)
                for signature in ("s2", "s3", "s4", "s5")
            },
            expected_signature_params=[
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "before": "s4",
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "before": "s2",
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s5",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s5",
                    },
                ],
            ],
        )
        await rpc_mock.start()
        assert rpc_mock.url is not None

        state = DurableState(":memory:")
        state.set_cursor(ADDRESS, "s1")
        delivery = FakeDelivery()
        try:
            async with SolanaRpcClient((rpc_mock.url,), timeout_seconds=2) as rpc:
                engine = CoreRailEngine(
                    _settings(":memory:"),
                    rpc,
                    delivery,
                    state=state,
                )

                with self.assertLogs(
                    "solana_webhook_core_rail_pro.engine",
                    level="WARNING",
                ) as logs:
                    self.assertEqual(await engine.poll_once(), 4)

                self.assertEqual(
                    delivery.delivered,
                    ["s2", "s3", "s4", "s5"],
                )
                self.assertEqual(state.get_cursor(ADDRESS), "s5")
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_pruned_boundaries"],
                    1,
                )
                self.assertTrue(
                    any(
                        "Durable cursor was not found in RPC history" in message
                        and "cursor=s1" in message
                        for message in logs.output
                    )
                )
                self.assertTrue(
                    any(
                        "Resuming address without cursor boundary" in message
                        for message in logs.output
                    )
                )

                self.assertEqual(await engine.poll_once(), 0)
                self.assertEqual(state.get_cursor(ADDRESS), "s5")
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_pruned_boundaries"],
                    1,
                )

                self.assertEqual(await engine.poll_once(), 0)
                self.assertEqual(state.get_cursor(ADDRESS), "s5")
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_pruned_boundaries"],
                    0,
                )
                resolved = engine.metrics.snapshot()["recovery"]["resolved_pruned_boundaries"]
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0]["address"], ADDRESS)
                self.assertEqual(resolved[0]["cursor"], "s1")
                self.assertEqual(resolved[0]["status"], "resolved")
                self.assertIsInstance(resolved[0]["resolved_at"], str)
        finally:
            state.close()
            await rpc_mock.close()

        self.assertEqual(rpc_mock.errors, [])
        self.assertEqual(
            rpc_mock.signature_params,
            rpc_mock._expected_signature_params,
        )
        self.assertCountEqual(
            rpc_mock.transaction_signatures,
            ["s2", "s3", "s4", "s5"],
        )

    async def test_malformed_signature_page_delivers_valid_records_without_advancing_cursor(
        self,
    ) -> None:
        rpc_mock = JsonRpcMock(
            signature_pages=[
                [{"signature": "s3"}, {"slot": 123}],
            ],
            transaction_results={"s3": _transaction("s3")},
            expected_signature_params=[
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
            ],
        )
        await rpc_mock.start()
        assert rpc_mock.url is not None

        state = DurableState(":memory:")
        state.set_cursor(ADDRESS, "s1")
        delivery = FakeDelivery()
        try:
            async with SolanaRpcClient((rpc_mock.url,), timeout_seconds=2) as rpc:
                engine = CoreRailEngine(
                    _settings(":memory:"),
                    rpc,
                    delivery,
                    state=state,
                )

                self.assertEqual(await engine.poll_once(), 1)
                self.assertEqual(delivery.delivered, ["s3"])
                self.assertEqual(state.get_cursor(ADDRESS), "s1")
        finally:
            state.close()
            await rpc_mock.close()

        self.assertEqual(rpc_mock.errors, [])
        self.assertEqual(rpc_mock.signature_params, rpc_mock._expected_signature_params)
        self.assertEqual(rpc_mock.transaction_signatures, ["s3"])

    async def test_repeated_pagination_marker_terminates_without_advancing_cursor(
        self,
    ) -> None:
        rpc_mock = JsonRpcMock(
            signature_pages=[
                [{"signature": "s5"}, {"signature": "s4"}],
                [{"signature": "s3"}, {"signature": "s4"}],
            ],
            transaction_results={
                signature: _transaction(signature)
                for signature in ("s3", "s4", "s5")
            },
            expected_signature_params=[
                [
                    ADDRESS,
                    {
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
                [
                    ADDRESS,
                    {
                        "before": "s4",
                        "commitment": "confirmed",
                        "limit": 2,
                        "until": "s1",
                    },
                ],
            ],
        )
        await rpc_mock.start()
        assert rpc_mock.url is not None

        state = DurableState(":memory:")
        state.set_cursor(ADDRESS, "s1")
        delivery = FakeDelivery()
        try:
            async with SolanaRpcClient((rpc_mock.url,), timeout_seconds=2) as rpc:
                engine = CoreRailEngine(
                    _settings(":memory:"),
                    rpc,
                    delivery,
                    state=state,
                )

                self.assertEqual(await engine.poll_once(), 3)
                self.assertEqual(delivery.delivered, ["s4", "s3", "s5"])
                self.assertEqual(state.get_cursor(ADDRESS), "s1")
        finally:
            state.close()
            await rpc_mock.close()

        self.assertEqual(rpc_mock.errors, [])
        self.assertEqual(rpc_mock.signature_params, rpc_mock._expected_signature_params)
        self.assertEqual(
            rpc_mock.transaction_signatures,
            ["s4", "s3", "s5"],
        )

    async def test_dead_lettered_event_advances_cursor_after_retry_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path)
            state.set_cursor(ADDRESS, "s1")
            rpc = FakeRpc(["s2", "s1"])
            delivery = FakeDelivery(dropped=True)
            engine = CoreRailEngine(
                _settings(state_path, settlement_route="primary-product-settlement"),
                rpc,
                delivery,
                state=state,
            )

            self.assertEqual(await engine.poll_once(), 1)
            self.assertEqual(state.get_cursor(ADDRESS), "s2")
            self.assertEqual(len(state.list_dead_letters()), 1)
            self.assertEqual(
                state.list_dead_letters()[0]["payload"]["settlement_route"],
                "primary-product-settlement",
            )
            state.close()

    async def test_unexpected_worker_failure_retries_without_advancing_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path)
            state.set_cursor(ADDRESS, "s1")
            rpc = FakeRpc(["s2", "s1"])
            delivery = FakeDelivery(failures=1)
            engine = CoreRailEngine(
                _settings(state_path),
                rpc,
                delivery,
                state=state,
            )

            self.assertEqual(await engine.poll_once(), 1)
            self.assertEqual(state.get_cursor(ADDRESS), "s1")
            self.assertEqual(await engine.poll_once(), 1)
            self.assertEqual(state.get_cursor(ADDRESS), "s2")
            self.assertEqual(delivery.delivered, ["s2", "s2"])
            state.close()

    async def test_dead_letter_store_is_bounded_and_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path, max_dead_letters=2)
            result = DeliveryResult(False, 5, 503, True, "unavailable")
            for signature in ("s1", "s2", "s3"):
                state.add_dead_letter(
                    {
                        "event": "solana.transaction",
                        "signature": signature,
                        "source_address": ADDRESS,
                    },
                    result,
                )

            records = state.list_dead_letters()
            self.assertEqual([record["signature"] for record in records], ["s3", "s2"])
            self.assertEqual(records[0]["payload"]["event"], "solana.transaction")
            self.assertEqual(state.dead_letter_count(), 2)
            state.close()

    async def test_dead_letter_eviction_prunes_only_evicted_replay_successes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            state = DurableState(state_path, max_dead_letters=2)
            result = DeliveryResult(False, 1, 503, True, "unavailable")
            first_id = state.add_dead_letter({"signature": "s1"}, result)
            retained_id = state.add_dead_letter({"signature": "s2"}, result)
            state.record_replay_outcome(first_id, outcome="delivered", result=result)
            state.record_replay_outcome(retained_id, outcome="delivered", result=result)

            evicted_id = state.add_dead_letter({"signature": "s3"}, result)

            self.assertFalse(state.has_successful_replay(first_id))
            self.assertTrue(state.has_successful_replay(retained_id))
            self.assertFalse(state.has_successful_replay(evicted_id))
            self.assertEqual(
                [record["signature"] for record in state.list_dead_letters()],
                ["s3", "s2"],
            )
            state.close()

    async def test_dead_letter_replay_success_cleanup_is_durable_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            result = DeliveryResult(False, 1, 503, True, "unavailable")
            state = DurableState(state_path, max_dead_letters=2)
            evicted_id = state.add_dead_letter({"signature": "s1"}, result)
            retained_id = state.add_dead_letter({"signature": "s2"}, result)
            state.record_replay_outcome(evicted_id, outcome="delivered", result=result)
            state.record_replay_outcome(retained_id, outcome="delivered", result=result)
            state.add_dead_letter({"signature": "s3"}, result)
            state.close()

            restarted_state = DurableState(state_path, max_dead_letters=2)
            self.assertFalse(restarted_state.has_successful_replay(evicted_id))
            self.assertTrue(restarted_state.has_successful_replay(retained_id))
            restarted_state.close()

    async def test_replay_success_markers_stay_bounded_through_dead_letter_churn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            retention_limit = 3
            result = DeliveryResult(False, 1, 503, True, "unavailable")
            state = DurableState(state_path, max_dead_letters=retention_limit)
            replayed_ids: list[int] = []

            for index in range(10):
                dead_letter_id = state.add_dead_letter(
                    {"signature": f"churn-{index}"},
                    result,
                )
                replayed_ids.append(dead_letter_id)
                state.record_replay_outcome(
                    dead_letter_id,
                    outcome="delivered",
                    result=result,
                )

                retained_ids = {
                    record["id"] for record in state.list_dead_letters()
                }
                self.assertLessEqual(len(retained_ids), retention_limit)
                self.assertEqual(state.successful_replay_count(), len(retained_ids))
                for retained_id in retained_ids:
                    self.assertTrue(state.has_successful_replay(retained_id))
                for evicted_id in set(replayed_ids) - retained_ids:
                    self.assertFalse(state.has_successful_replay(evicted_id))

            state.close()

            restarted_state = DurableState(
                state_path,
                max_dead_letters=retention_limit,
            )
            retained_ids = {
                record["id"] for record in restarted_state.list_dead_letters()
            }
            self.assertEqual(len(retained_ids), retention_limit)
            self.assertEqual(
                restarted_state.successful_replay_count(),
                len(retained_ids),
            )
            for retained_id in retained_ids:
                self.assertTrue(restarted_state.has_successful_replay(retained_id))
            for evicted_id in set(replayed_ids) - retained_ids:
                self.assertFalse(restarted_state.has_successful_replay(evicted_id))
            restarted_state.close()

    async def test_restart_applies_lower_replay_audit_capacity_without_changing_dead_letters(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = str(Path(directory) / "state.sqlite3")
            initial_settings = _settings(
                state_path,
                max_dead_letters=2,
                replay_audit_max_entries=4,
            )
            state = DurableState(
                state_path,
                max_dead_letters=initial_settings.dead_letter_max_entries,
                max_replay_audit_entries=initial_settings.replay_audit_max_entries,
            )
            result = DeliveryResult(False, 1, 503, True, "unavailable")
            dead_letter_id = state.add_dead_letter(
                {
                    "event": "solana.transaction",
                    "signature": "audit-retention-signature",
                    "source_address": ADDRESS,
                },
                result,
            )
            selected_dead_letters = state.get_dead_letters([dead_letter_id])
            dead_letter_count = state.dead_letter_count()

            replay_ids = [
                state.record_replay_outcome(
                    dead_letter_id,
                    outcome=outcome,
                    result=result,
                )
                for outcome in ("failed", "skipped", "delivered", "failed")
            ]

            audits = state.list_replay_outcomes(limit=1000)
            self.assertEqual([audit["id"] for audit in audits], replay_ids[::-1])
            self.assertEqual(
                [audit["outcome"] for audit in audits],
                ["failed", "delivered", "skipped", "failed"],
            )
            self.assertEqual(
                state.max_replay_audit_entries,
                initial_settings.replay_audit_max_entries,
            )
            state.close()

            restarted_settings = _settings(
                state_path,
                max_dead_letters=2,
                replay_audit_max_entries=2,
            )
            restarted_state = DurableState(
                state_path,
                max_dead_letters=restarted_settings.dead_letter_max_entries,
                max_replay_audit_entries=restarted_settings.replay_audit_max_entries,
            )
            self.assertEqual(
                [audit["id"] for audit in restarted_state.list_replay_outcomes()],
                replay_ids[-2:][::-1],
            )
            self.assertEqual(
                restarted_state.max_replay_audit_entries,
                restarted_settings.replay_audit_max_entries,
            )
            self.assertEqual(restarted_state.dead_letter_count(), dead_letter_count)
            self.assertEqual(
                restarted_state.get_dead_letters([dead_letter_id]),
                selected_dead_letters,
            )
            restarted_state.close()

    async def test_replay_audit_retention_requires_positive_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError,
                r"max_replay_audit_entries must be at least 1",
            ):
                DurableState(
                    str(Path(directory) / "state.sqlite3"),
                    max_replay_audit_entries=0,
                )


if __name__ == "__main__":
    unittest.main()
