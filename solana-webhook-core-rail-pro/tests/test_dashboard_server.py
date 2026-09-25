"""Unit tests for the decoupled local operations dashboard."""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen

from dashboard_server import DASHBOARD_HTML, DashboardServer, _LOG_DETAIL_LIMIT
from solana_webhook_core_rail_pro.observability import (
    BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
    DashboardMetrics,
)
from solana_webhook_core_rail_pro.retry import (
    MAX_RETRY_DELAY_SECONDS,
    RETRY_DELAY_WARNING_THRESHOLD,
)
import asyncio
import tempfile
from dataclasses import replace
from unittest.mock import patch
from solana_webhook_core_rail_pro import cli
from solana_webhook_core_rail_pro.config import Settings
from solana_webhook_core_rail_pro.engine import CoreRailEngine
from solana_webhook_core_rail_pro.webhook import WebhookDelivery


CUSTOM_LATENCY_WARNING_THRESHOLD_MS = 750.0


class DashboardServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            recovery_boundary_limit=2,
            retry_max_attempts=7,
            retry_base_backoff_seconds=3.25,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        self.metrics.record_parsed("sig-dashboard-1")
        self.metrics.record_queue_depth(3)
        self.server = DashboardServer(self.metrics, port=0)
        self.server.start()
        assert self.server.address is not None
        self.base_url = f"http://127.0.0.1:{self.server.address[1]}"
    def setUp(self) -> None:
        self.metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            recovery_boundary_limit=2,
            retry_max_attempts=7,
            retry_base_backoff_seconds=3.25,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        self.metrics.record_parsed("sig-dashboard-1")
        self.metrics.record_queue_depth(3)
        self.server = DashboardServer(self.metrics, port=0)
        self.server.start()
        assert self.server.address is not None
        self.base_url = f"http://127.0.0.1:{self.server.address[1]}"

    def tearDown(self) -> None:
        self.server.stop()

    def _render_dashboard_in_browser(
        self,
        base_url: str | None = None,
        *,
        browser_locale: str | None = None,
    ) -> str:
        browser = shutil.which("chromium")
        if browser is None:
            browser = "/repl/tools/bin/chromium"
            if not shutil.which(browser):
                self.skipTest("Chromium is not available for dashboard browser coverage")
        browser_args = [
            browser,
            "--headless=new",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--dump-dom",
            "--virtual-time-budget=1500",
        ]
        if browser_locale is not None:
            browser_args.append(f"--lang={browser_locale}")
        browser_args.append(f"{base_url or self.base_url}/")
        result = subprocess.run(
            browser_args,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return result.stdout

    @staticmethod
    def _rendered_pulse(html: str) -> tuple[str, str]:
        match = re.search(
            r'<div class="pulse"([^>]*)><i([^>]*)></i>',
            html,
        )
        if match is None:
            raise AssertionError("browser output did not contain the live pulse")
        return match.group(1), match.group(2)

    @staticmethod
    def _rendered_recovery_panel(html: str) -> str:
        match = re.search(r'<tbody id="recovery">(.*?)</tbody>', html, re.DOTALL)
        if match is None:
            raise AssertionError("browser output did not contain the recovery panel")
        return match.group(1)

    @staticmethod
    def _rendered_resolved_recovery_panel(html: str) -> str:
        match = re.search(
            r'<tbody id="resolved-recovery">(.*?)</tbody>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError(
                "browser output did not contain the resolved recovery panel"
            )
        return match.group(1)

    @staticmethod
    def _rendered_recovery_summary(html: str) -> str:
        match = re.search(
            r'<span\b[^>]*\bid="recovery-summary"[^>]*>(.*?)</span>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("browser output did not contain the recovery summary")
        return match.group(1)

    @staticmethod
    def _rendered_recovery_retention(html: str) -> str:
        match = re.search(
            r'<p\b[^>]*\bid="recovery-retention"[^>]*>(.*?)</p>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("browser output did not contain the recovery retention notice")
        return match.group(1)

    @staticmethod
    def _rendered_table(html: str, table_id: str) -> str:
        match = re.search(
            rf'<tbody id="{re.escape(table_id)}">(.*?)</tbody>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"browser output did not contain the {table_id} table")
        return match.group(1)

    @staticmethod
    def _rendered_element(html: str, element_id: str) -> str:
        match = re.search(
            rf'<(?:ol|ul|div)\b[^>]*\bid="{re.escape(element_id)}"[^>]*>(.*?)</(?:ol|ul|div)>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError(f"browser output did not contain {element_id}")
        return match.group(1)

    @staticmethod
    def _rendered_timing_status(html: str) -> str:
        match = re.search(
            r'<p\b[^>]*\bid="block-timing-status"[^>]*>(.*?)</p>',
            html,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("browser output did not contain the timing status")
        return match.group(1)

    def _assert_rendered_latency(
        self,
        html: str,
        *,
        last: str,
        average: str,
        status: str,
    ) -> None:
        self.assertIn(f'id="last-block-latency">{last} ms</div>', html)
        self.assertIn(f'id="average-block-latency">{average} ms</div>', html)
        self.assertEqual(self._rendered_timing_status(html), status)

    def test_html_panel_and_health_endpoint(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=2) as response:
            html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Live Netcode Metrics", html)
        self.assertIn("Recovery Boundaries", html)
        self.assertIn("Evicted Recovery Evidence", html)
        self.assertIn("Compatibility", html)
        self.assertIn("No pruned recovery boundaries detected", html)
        self.assertIn("Dead-Letter Queue", html)
        self.assertIn("Starting backoff", html)
        self.assertIn("Maximum permitted delay", html)

        with urlopen(f"{self.base_url}/healthz", timeout=2) as response:
            self.assertEqual(json.load(response)["status"], "ok")

    def test_metrics_endpoint_exposes_bounds_and_live_counts(self) -> None:
        with urlopen(f"{self.base_url}/api/metrics", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["snapshot_revision"], 1)
        self.assertEqual(payload["streaming"]["parsed_signatures"], 1)
        self.assertEqual(payload["streaming"]["active_events"], 3)
        self.assertEqual(payload["memory"]["tracked_event_bound"], 50)
        self.assertEqual(payload["webhooks"]["success_rate_percent"], 0.0)
        self.assertEqual(
            payload["telemetry"]["latency_display_decimal_places"],
            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
        )
        self.assertEqual(payload["retry_policy"]["max_attempts"], 7)
        self.assertEqual(payload["retry_policy"]["base_backoff_seconds"], 3.25)
        self.assertEqual(
            payload["retry_policy"]["max_delay_seconds"],
            MAX_RETRY_DELAY_SECONDS,
        )
        self.assertEqual(
            payload["retry_policy"]["configured_max_delay_seconds"],
            3.25 * 32,
        )
        self.assertEqual(
            payload["retry_policy"]["delay_warning_threshold_seconds"],
            MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD,
        )
        self.assertFalse(payload["retry_policy"]["delay_ceiling_warning"])
        self.assertEqual(
            payload["retry_policy"]["schedule_seconds"],
            [3.25, 6.5, 13.0, 26.0, 52.0, 104.0],
        )
        self.assertEqual(payload["recovery"]["boundary_missing_total"], 0)
        self.assertEqual(payload["recovery"]["active_pruned_boundaries"], 0)
        self.assertEqual(payload["recovery"]["active_evidence_limit"], 2)
        self.assertFalse(payload["recovery"]["active_evidence_trimmed"])
        self.assertEqual(payload["recovery"]["active_evidence_trimmed_total"], 0)
        self.assertEqual(payload["recovery"]["evicted_boundaries"], [])

    def test_metrics_endpoint_returns_controlled_error_when_snapshot_fails(self) -> None:
        failing_server = DashboardServer(FailingMetricsProvider(), port=0)
        failing_server.start()
        try:
            assert failing_server.address is not None
            with self.assertLogs("dashboard_server", level="WARNING") as logs:
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        f"http://127.0.0.1:{failing_server.address[1]}/api/metrics",
                        timeout=2,
                    )
        finally:
            failing_server.stop()

        response = raised.exception
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        response_body = response.read().decode("utf-8")
        self.assertNotIn(FailingMetricsProvider.ERROR_DETAIL, response_body)
        self.assertEqual(json.loads(response_body), {"error": "metrics_unavailable"})
        self.assertEqual(len(logs.records), 1)
        self.assertIn("dashboard metrics snapshot failed", logs.records[0].message)
        self.assertIn("error_type=RuntimeError", logs.records[0].message)
        self.assertIn("provider failure", logs.records[0].message)

    def test_metrics_failure_log_redacts_provider_credentials_and_signed_urls(self) -> None:
        failing_server = DashboardServer(
            FailingMetricsProvider(
                "RPC failed at https://user:password@rpc.example.test/"
                "?api-key=provider-secret&X-Amz-Signature=signed-secret "
                "Authorization: Bearer bearer-secret detail=retryable"
            ),
            port=0,
        )
        failing_server.start()
        try:
            assert failing_server.address is not None
            with self.assertLogs("dashboard_server", level="WARNING") as logs:
                with self.assertRaises(HTTPError):
                    urlopen(
                        f"http://127.0.0.1:{failing_server.address[1]}/api/metrics",
                        timeout=2,
                    )
        finally:
            failing_server.stop()

        log_message = logs.records[0].message
        self.assertIn("RPC failed", log_message)
        self.assertIn("detail=retryable", log_message)
        for secret in (
            "password",
            "provider-secret",
            "signed-secret",
            "bearer-secret",
        ):
            self.assertNotIn(secret, log_message)
        self.assertIn("https://<redacted>@rpc.example.test/", log_message)
        self.assertIn("api-key=<redacted>", log_message)
        self.assertIn("X-Amz-Signature=<redacted>", log_message)
        self.assertIn("Authorization: <redacted>", log_message)

    def test_metrics_failure_log_normalizes_and_bounds_oversized_provider_errors(self) -> None:
        provider_error = (
            "RPC provider failure\ntraceback line\r\n"
            "api-key=oversized-secret password=another-secret\x00"
            + "diagnostic-payload " * (_LOG_DETAIL_LIMIT + 100)
            + "\x1f\x7f token=trailing-secret"
        )
        failing_server = DashboardServer(
            FailingMetricsProvider(provider_error),
            port=0,
        )
        failing_server.start()
        try:
            assert failing_server.address is not None
            with (
                self.assertLogs("dashboard_server", level="WARNING") as logs,
                self.assertRaises(HTTPError),
            ):
                urlopen(
                    f"http://127.0.0.1:{failing_server.address[1]}/api/metrics",
                    timeout=2,
                )
        finally:
            failing_server.stop()

        self.assertEqual(len(logs.records), 1)
        log_message = logs.records[0].message
        self.assertNotRegex(log_message, r"[\x00-\x1f\x7f]")
        detail = log_message.split("detail=", 1)[1]
        self.assertEqual(len(detail), _LOG_DETAIL_LIMIT + 1)
        self.assertTrue(detail.endswith("…"))
        self.assertIn("RPC provider failure traceback line", detail)
        for secret in (
            "oversized-secret",
            "another-secret",
            "trailing-secret",
        ):
            self.assertNotIn(secret, log_message)

    def test_metrics_endpoint_rejects_invalid_top_level_snapshot_values(self) -> None:
        for invalid_snapshot in (None, [], "not-a-snapshot"):
            with self.subTest(snapshot_type=type(invalid_snapshot).__name__):
                invalid_server = DashboardServer(
                    TopLevelSnapshotProvider(invalid_snapshot),
                    port=0,
                )
                invalid_server.start()
                try:
                    assert invalid_server.address is not None
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(
                            f"http://127.0.0.1:{invalid_server.address[1]}/api/metrics",
                            timeout=2,
                        )
                finally:
                    invalid_server.stop()

                response = raised.exception
                self.assertEqual(response.status, 502)
                self.assertEqual(
                    response.headers["Content-Type"],
                    "application/json; charset=utf-8",
                )
                self.assertEqual(json.load(response), {"error": "metrics_invalid"})

    def test_metrics_endpoint_returns_controlled_error_for_non_json_snapshot_values(
        self,
    ) -> None:
        invalid_snapshot = self.metrics.snapshot()
        invalid_snapshot["provider_value"] = object()
        invalid_server = DashboardServer(
            TopLevelSnapshotProvider(invalid_snapshot),
            port=0,
        )
        invalid_server.start()
        try:
            assert invalid_server.address is not None
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    f"http://127.0.0.1:{invalid_server.address[1]}/api/metrics",
                    timeout=2,
                )
        finally:
            invalid_server.stop()

        response = raised.exception
        self.assertEqual(response.status, 502)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/json; charset=utf-8",
        )
        self.assertEqual(json.load(response), {"error": "metrics_invalid"})

    def test_metrics_endpoint_rejects_non_finite_snapshot_values(self) -> None:
        for invalid_value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_value=invalid_value):
                invalid_snapshot = self.metrics.snapshot()
                telemetry = invalid_snapshot["telemetry"]
                assert isinstance(telemetry, dict)
                latency = telemetry["block_processing_latency_ms"]
                assert isinstance(latency, dict)
                latency["last"] = invalid_value
                invalid_server = DashboardServer(
                    TopLevelSnapshotProvider(invalid_snapshot),
                    port=0,
                )
                invalid_server.start()
                try:
                    assert invalid_server.address is not None
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(
                            f"http://127.0.0.1:{invalid_server.address[1]}/api/metrics",
                            timeout=2,
                        )
                finally:
                    invalid_server.stop()

                response = raised.exception
                self.assertEqual(response.status, 502)
                self.assertEqual(
                    response.headers["Content-Type"],
                    "application/json; charset=utf-8",
                )
                self.assertEqual(json.load(response), {"error": "metrics_invalid"})

    def test_metrics_endpoint_preserves_current_and_legacy_snapshot_shapes(self) -> None:
        current_snapshot = self.metrics.snapshot()
        legacy_snapshot = copy.deepcopy(current_snapshot)
        legacy_snapshot.pop("telemetry")
        legacy_snapshot.pop("snapshot_revision")

        for snapshot_name, expected_snapshot in (
            ("current", current_snapshot),
            ("legacy", legacy_snapshot),
        ):
            with self.subTest(snapshot=snapshot_name):
                snapshot_server = DashboardServer(
                    TopLevelSnapshotProvider(expected_snapshot),
                    port=0,
                )
                snapshot_server.start()
                try:
                    assert snapshot_server.address is not None
                    with urlopen(
                        f"http://127.0.0.1:{snapshot_server.address[1]}/api/metrics",
                        timeout=2,
                    ) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response), expected_snapshot)
                finally:
                    snapshot_server.stop()

    def test_serialized_latency_precision_drives_dashboard_formatter_contract(self) -> None:
        serialized_snapshot = json.loads(json.dumps(self.metrics.snapshot()))
        telemetry = serialized_snapshot["telemetry"]
        self.assertEqual(
            telemetry["latency_display_decimal_places"],
            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
        )

        self.assertRegex(
            DASHBOARD_HTML,
            r"const latencyValue = \(value, decimalPlaces\)",
        )
        self.assertIn(
            "maximumFractionDigits: decimalPlaces",
            DASHBOARD_HTML,
        )
        self.assertIn(
            'const LATENCY_DISPLAY_LOCALE = "en-US"',
            DASHBOARD_HTML,
        )
        self.assertIn(
            "value.toLocaleString(LATENCY_DISPLAY_LOCALE",
            DASHBOARD_HTML,
        )
        self.assertIn(
            "latencyPrecision = telemetry.latency_display_decimal_places",
            DASHBOARD_HTML,
        )
        self.assertIn(
            "latencyValue(latency.last, latencyPrecision)",
            DASHBOARD_HTML,
        )
        self.assertIn(
            "latencyValue(latency.average, latencyPrecision)",
            DASHBOARD_HTML,
        )
        self.assertNotRegex(
            DASHBOARD_HTML,
            r"LATENCY_DISPLAY_DECIMAL_PLACES\s*=",
        )

    def test_serialized_zero_latency_precision_formats_whole_milliseconds(self) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 0
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency["last"] = 1_250.0
        latency["average"] = 1_250.0
        latency["warning_threshold_ms"] = 2_500.0

        serialized_snapshot = json.loads(json.dumps(snapshot))
        self.assertEqual(
            serialized_snapshot["telemetry"]["latency_display_decimal_places"],
            0,
        )

        formatter_match = re.search(
            r"(const LATENCY_DISPLAY_LOCALE = .*?)(?=    const isRecord =)",
            DASHBOARD_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(formatter_match)
        assert formatter_match is not None
        formatter_source = formatter_match.group(1)
        formatter_script = f"""
{formatter_source}
const snapshot = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const telemetry = snapshot.telemetry;
const latency = telemetry.block_processing_latency_ms;
process.stdout.write(JSON.stringify({{
  last: latencyValue(latency.last, telemetry.latency_display_decimal_places),
  average: latencyValue(latency.average, telemetry.latency_display_decimal_places),
  threshold: latencyValue(latency.warning_threshold_ms, telemetry.latency_display_decimal_places),
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", formatter_script],
            input=json.dumps(serialized_snapshot),
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "last": "1,250 ms",
                "average": "1,250 ms",
                "threshold": "2,500 ms",
            },
        )

    def test_serialized_fractional_latency_uses_fixed_locale_formatting(self) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 4
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency["last"] = 1_234.5678
        latency["average"] = 12.34567
        latency["warning_threshold_ms"] = 987.6543

        formatter_match = re.search(
            r"(const LATENCY_DISPLAY_LOCALE = .*?)(?=    const isRecord =)",
            DASHBOARD_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(formatter_match)
        assert formatter_match is not None
        formatter_source = formatter_match.group(1)
        formatter_script = f"""
{formatter_source}
const snapshot = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const telemetry = snapshot.telemetry;
const latency = telemetry.block_processing_latency_ms;
process.stdout.write(JSON.stringify({{
  last: latencyValue(latency.last, telemetry.latency_display_decimal_places),
  average: latencyValue(latency.average, telemetry.latency_display_decimal_places),
  threshold: latencyValue(latency.warning_threshold_ms, telemetry.latency_display_decimal_places),
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", formatter_script],
            input=json.dumps(snapshot),
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "last": "1,234.5678 ms",
                "average": "12.3457 ms",
                "threshold": "987.6543 ms",
            },
        )

    def test_zero_latency_precision_keeps_just_over_threshold_warning_clear(self) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 0
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency.update(
            {
                "last": 750.1,
                "average": 750.1,
                "min": 750.1,
                "max": 750.1,
                "sample_count": 1,
                "warning_threshold_ms": 750.0,
                "last_exceeds_threshold": True,
                "recent_exceeds_threshold": True,
            }
        )

        serialized_snapshot = json.loads(json.dumps(snapshot))
        formatter_match = re.search(
            r"(const LATENCY_DISPLAY_LOCALE = .*?const isSupportedLatencyPrecision = .*?)(?=    const duration =)",
            DASHBOARD_HTML,
            re.DOTALL,
        )
        self.assertIsNotNone(formatter_match)
        assert formatter_match is not None
        formatter_source = formatter_match.group(1)
        formatter_script = f"""
{formatter_source}
const snapshot = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const telemetry = snapshot.telemetry;
const latency = telemetry.block_processing_latency_ms;
process.stdout.write(JSON.stringify({{
  last: latencyValue(latency.last, telemetry.latency_display_decimal_places),
  threshold: latencyValue(latency.warning_threshold_ms, telemetry.latency_display_decimal_places),
  status: timingStatusMessage(
    latency,
    telemetry.latency_display_decimal_places,
    true,
  ),
}}));
"""
        result = subprocess.run(
            ["node", "--input-type=commonjs", "--eval", formatter_script],
            input=json.dumps(serialized_snapshot),
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(
            json.loads(result.stdout),
            {
                "last": "750 ms",
                "threshold": "750 ms",
                "status": (
                    "Processed block latency warning: the latest and a recent sample "
                    "exceed the configured 750 ms threshold. Displayed latencies are "
                    "rounded to 0 decimal places."
                ),
            },
        )

    def test_browser_renders_configured_retry_policy(self) -> None:
        html = self._render_dashboard_in_browser()
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="retry-base-backoff">3.25 seconds</div>', html)
        self.assertIn('id="retry-configured-delay">104 seconds</div>', html)
        self.assertIn('id="retry-max-delay">24 hours</div>', html)
        schedule = self._rendered_element(html, "retry-schedule")
        self.assertIn("Retry 1", schedule)
        self.assertIn("3.25 seconds", schedule)
        self.assertIn("Retry 6", schedule)
        self.assertIn("104 seconds", schedule)

    def test_browser_renders_processed_and_discarded_timing_outcomes(self) -> None:
        processed_signature = "signature-processed-browser"
        second_processed_signature = "signature-processed-browser-2"
        discarded_signature = "signature-discarded-browser"
        self.metrics.record_block_processing(processed_signature, 12.5)
        self.metrics.record_block_processing(second_processed_signature, 7.5)
        self.metrics.record_block_discarded(discarded_signature)

        html = self._render_dashboard_in_browser()

        self.assertIn('id="processed-blocks">2</div>', html)
        self.assertIn('id="discarded-blocks">1</div>', html)
        self.assertIn('id="last-block-latency">7.5 ms</div>', html)
        self.assertIn('id="average-block-latency">10 ms</div>', html)
        self.assertIn('id="block-latency-samples">2</div>', html)
        self.assertIn('id="block-timing-status" role="status" hidden=""', html)
        self.assertEqual(self._rendered_timing_status(html), "")
        self.assertNotIn("block_discarded", html)
        self.assertNotIn(processed_signature, html)
        self.assertNotIn(second_processed_signature, html)
        self.assertNotIn(discarded_signature, html)
        self.assertLess(len(html), 50_000)

    def test_browser_uses_backend_latency_precision_contract(self) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 2
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency["last"] = 12.345678
        latency["average"] = 12.345678

        snapshot_server = DashboardServer(
            SnapshotMetricsProvider(snapshot),
            port=0,
        )
        snapshot_server.start()
        try:
            assert snapshot_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{snapshot_server.address[1]}",
            )
        finally:
            snapshot_server.stop()

        self.assertIn('id="last-block-latency">12.35 ms</div>', html)
        self.assertIn('id="average-block-latency">12.35 ms</div>', html)

    def test_browser_refresh_keeps_latency_locale_stable_for_non_english_browsers(
        self,
    ) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 4
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency.update(
            {
                "last": 1_250.0,
                "average": 12.34567,
                "min": 12.34567,
                "max": 1_250.0,
                "sample_count": 2,
                "warning_threshold_ms": 987.6543,
                "last_exceeds_threshold": True,
                "recent_exceeds_threshold": True,
            }
        )

        rendered_by_locale: dict[str, str] = {}
        refresh_calls_by_locale: dict[str, int] = {}
        for browser_locale in ("de-DE", "fr-FR", "pt-BR"):
            provider = SnapshotMetricsProvider(snapshot)
            snapshot_server = DashboardServer(provider, port=0)
            snapshot_server.start()
            try:
                assert snapshot_server.address is not None
                rendered_by_locale[browser_locale] = self._render_dashboard_in_browser(
                    f"http://127.0.0.1:{snapshot_server.address[1]}",
                    browser_locale=browser_locale,
                )
                refresh_calls_by_locale[browser_locale] = provider.snapshot_calls
            finally:
                snapshot_server.stop()

        expected_status = (
            "Processed block latency warning: the latest and a recent "
            "sample exceed the configured 987.6543 ms threshold."
        )
        for browser_locale, html in rendered_by_locale.items():
            with self.subTest(browser_locale=browser_locale):
                self.assertGreaterEqual(refresh_calls_by_locale[browser_locale], 2)
                self._assert_rendered_latency(
                    html,
                    last="1,250",
                    average="12.3457",
                    status=expected_status,
                )

    def test_browser_identifies_malformed_latency_precision_and_recovers(self) -> None:
        self.metrics.record_block_processing("precision-recovery-signature", 125.0)
        malformed_precisions = (
            ("unsupported", 21),
            ("missing", None),
        )

        for label, malformed_precision in malformed_precisions:
            with self.subTest(metadata=label):
                provider = LatencyPrecisionMetricsProvider(
                    self.metrics,
                    malformed_precision,
                )
                precision_server = DashboardServer(provider, port=0)
                precision_server.start()
                try:
                    assert precision_server.address is not None
                    malformed_html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{precision_server.address[1]}",
                    )
                    provider.restore_timing()
                    recovered_html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{precision_server.address[1]}",
                    )
                finally:
                    precision_server.stop()

                self.assertIn('id="last-block-latency">Unavailable</div>', malformed_html)
                self.assertIn(
                    'id="average-block-latency">Unavailable</div>',
                    malformed_html,
                )
                self.assertEqual(
                    self._rendered_timing_status(malformed_html),
                    "Timing telemetry unavailable — provider latency display precision is "
                    "missing or unsupported.",
                )
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    malformed_html,
                )
                self.assertIn('id="last-block-latency">125 ms</div>', recovered_html)
                self.assertIn('id="average-block-latency">125 ms</div>', recovered_html)
                self.assertIn(
                    'id="block-timing-status" role="status" hidden=""',
                    recovered_html,
                )
                self.assertEqual(self._rendered_timing_status(recovered_html), "")

    def test_browser_shows_warning_when_processed_latency_exceeds_threshold(self) -> None:
        self.metrics.record_block_processing("signature-slow-browser", 1_001.0)

        html = self._render_dashboard_in_browser()

        self.assertEqual(
            self._rendered_timing_status(html),
            "Processed block latency warning: the latest and a recent sample exceed the configured 750 ms threshold.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            html,
        )

    def test_browser_refresh_keeps_exact_threshold_latency_healthy(self) -> None:
        self.metrics.record_block_processing(
            "signature-at-latency-threshold-browser",
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS,
        )

        at_threshold_html = self._render_dashboard_in_browser()

        self.assertEqual(self._rendered_timing_status(at_threshold_html), "")
        self.assertIn(
            'id="block-timing-status" role="status" hidden=""',
            at_threshold_html,
        )
        telemetry = self.metrics.snapshot()["telemetry"]
        latency = telemetry["block_processing_latency_ms"]
        self.assertFalse(latency["last_exceeds_threshold"])
        self.assertFalse(latency["recent_exceeds_threshold"])

        self.metrics.record_block_processing(
            "signature-just-over-latency-threshold-browser",
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS + 0.001,
        )

        above_threshold_html = self._render_dashboard_in_browser()

        self.assertEqual(
            self._rendered_timing_status(above_threshold_html),
            "Processed block latency warning: the latest and a recent sample exceed the configured 750 ms threshold.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            above_threshold_html,
        )

    def test_browser_refresh_keeps_sub_millisecond_warning_boundary_clear(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            block_processing_latency_warning_threshold_ms=0.75,
        )
        metrics.record_block_processing("signature-sub-millisecond-browser", 0.7501)
        boundary_server = DashboardServer(metrics, port=0)
        boundary_server.start()
        try:
            assert boundary_server.address is not None
            first_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
            refreshed_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
        finally:
            boundary_server.stop()

        for html in (first_html, refreshed_html):
            with self.subTest(render="first" if html is first_html else "refresh"):
                self.assertIn('id="last-block-latency">0.7501 ms</div>', html)
                self.assertIn('id="average-block-latency">0.7501 ms</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    "Processed block latency warning: the latest and a recent sample exceed the configured 0.75 ms threshold.",
                )
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    html,
                )

    def test_browser_explains_sub_microsecond_warning_rounding_after_refresh(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            block_processing_latency_warning_threshold_ms=0.75,
        )
        metrics.record_block_processing("signature-sub-microsecond-browser", 0.7500001)
        boundary_server = DashboardServer(metrics, port=0)
        boundary_server.start()
        try:
            assert boundary_server.address is not None
            first_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
            refreshed_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
        finally:
            boundary_server.stop()

        expected_status = (
            "Processed block latency warning: the latest and a recent sample exceed "
            "the configured 0.75 ms threshold. Displayed latencies are rounded to "
            "6 decimal places."
        )
        for html in (first_html, refreshed_html):
            with self.subTest(render="first" if html is first_html else "refresh"):
                self.assertIn('id="last-block-latency">0.75 ms</div>', html)
                self.assertIn('id="average-block-latency">0.75 ms</div>', html)
                self.assertEqual(self._rendered_timing_status(html), expected_status)
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    html,
                )

    def test_browser_refresh_keeps_zero_decimal_warning_explanation(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        metrics.record_block_processing(
            "signature-zero-decimal-warning-browser",
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS + 0.1,
        )
        snapshot = metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 0
        provider = SnapshotMetricsProvider(snapshot)
        boundary_server = DashboardServer(provider, port=0)
        boundary_server.start()
        try:
            assert boundary_server.address is not None
            first_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
            refreshed_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{boundary_server.address[1]}",
            )
        finally:
            boundary_server.stop()

        expected_status = (
            "Processed block latency warning: the latest and a recent sample "
            "exceed the configured 750 ms threshold. Displayed latencies are "
            "rounded to 0 decimal places."
        )
        for html in (first_html, refreshed_html):
            with self.subTest(render="first" if html is first_html else "refresh"):
                self._assert_rendered_latency(
                    html,
                    last="750",
                    average="750",
                    status=expected_status,
                )
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    html,
                )

    def test_browser_refresh_keeps_only_recent_latency_warning_after_recovery(self) -> None:
        self.metrics.record_block_processing("signature-slow-browser", 1_001.0)

        warning_html = self._render_dashboard_in_browser()

        self.metrics.record_block_processing("signature-recovered-browser", 50.0)

        recovered_html = self._render_dashboard_in_browser()

        self.assertIn('id="last-block-latency">1,001 ms</div>', warning_html)
        self.assertIn('id="average-block-latency">1,001 ms</div>', warning_html)
        self.assertEqual(
            self._rendered_timing_status(warning_html),
            "Processed block latency warning: the latest and a recent sample "
            "exceed the configured 750 ms threshold.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            warning_html,
        )

        self.assertIn('id="last-block-latency">50 ms</div>', recovered_html)
        self.assertIn('id="average-block-latency">525.5 ms</div>', recovered_html)
        self.assertIn('id="block-latency-samples">2</div>', recovered_html)
        recovered_status = self._rendered_timing_status(recovered_html)
        self.assertEqual(
            recovered_status,
            "Processed block latency warning: a recent sample exceeds the configured 750 ms threshold.",
        )
        self.assertNotIn("latest sample", recovered_status)
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            recovered_html,
        )

        recovered_latency = self.metrics.snapshot()["telemetry"][
            "block_processing_latency_ms"
        ]
        self.assertFalse(recovered_latency["last_exceeds_threshold"])
        self.assertTrue(recovered_latency["recent_exceeds_threshold"])

    def test_browser_identifies_timing_state_before_first_sample(self) -> None:
        html = self._render_dashboard_in_browser()

        self.assertEqual(
            self._rendered_timing_status(html),
            "Timing telemetry is waiting for the first valid sample.",
        )

    def test_browser_refresh_keeps_timing_warning_for_discarded_blocks(self) -> None:
        discarded_signature = "discarded-only-timing-signature"
        self.metrics.record_block_discarded(discarded_signature)
        discarded_server = DashboardServer(
            DiscardedTimingMetricsProvider(self.metrics),
            port=0,
        )
        discarded_server.start()
        try:
            assert discarded_server.address is not None
            first_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{discarded_server.address[1]}",
            )
            refreshed_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{discarded_server.address[1]}",
            )
        finally:
            discarded_server.stop()

        for html in (first_html, refreshed_html):
            with self.subTest(render="first" if html is first_html else "refresh"):
                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                self.assertIn('id="processed-blocks">0</div>', html)
                self.assertIn('id="discarded-blocks">1</div>', html)
                self.assertIn('id="last-block-latency">0 ms</div>', html)
                self.assertIn('id="average-block-latency">0 ms</div>', html)
                self.assertIn('id="block-latency-samples">0</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    "Timing telemetry is waiting for the first valid sample.",
                )
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    html,
                )
                pulse_style, indicator_style = self._rendered_pulse(html)
                self.assertNotIn("color: var(--red)", pulse_style)
                self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_and_clears_timing_threshold_warning(self) -> None:
        timing_signature = "slow-timing-threshold-signature"
        self.metrics.record_block_processing(timing_signature, 1250.0)
        provider = ThresholdTimingMetricsProvider(self.metrics)
        threshold_server = DashboardServer(provider, port=0)
        threshold_server.start()
        try:
            assert threshold_server.address is not None
            first_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{threshold_server.address[1]}",
            )
            refreshed_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{threshold_server.address[1]}",
            )

            provider.restore_timing()
            healthy_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{threshold_server.address[1]}",
            )
        finally:
            threshold_server.stop()

        for html in (first_html, refreshed_html):
            with self.subTest(render="first" if html is first_html else "refresh"):
                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                self.assertIn('id="processed-blocks">1</div>', html)
                self.assertIn('id="discarded-blocks">0</div>', html)
                self.assertIn('id="last-block-latency">1,250 ms</div>', html)
                self.assertIn('id="average-block-latency">1,250 ms</div>', html)
                self.assertIn('id="block-latency-samples">1</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    "Processed block latency warning: the latest and a recent sample exceed the configured 750 ms threshold.",
                )
                self.assertNotIn(
                    'id="block-timing-status" role="status" hidden=""',
                    html,
                )

        self.assertIn('id="parsed">1</div>', healthy_html)
        self.assertIn('id="active">3</div>', healthy_html)
        self.assertIn('id="bound">50</div>', healthy_html)
        self.assertIn('id="processed-blocks">1</div>', healthy_html)
        self.assertIn('id="discarded-blocks">0</div>', healthy_html)
        self.assertIn('id="last-block-latency">125 ms</div>', healthy_html)
        self.assertIn('id="average-block-latency">125 ms</div>', healthy_html)
        self.assertIn('id="block-latency-samples">1</div>', healthy_html)
        self.assertIn('id="block-timing-status" role="status" hidden=""', healthy_html)
        self.assertEqual(self._rendered_timing_status(healthy_html), "")
        healthy_latency = provider.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertEqual(
            healthy_latency["warning_threshold_ms"],
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS,
        )

    def test_browser_marks_malformed_timing_threshold_metadata_unavailable(self) -> None:
        timing_signature = "malformed-timing-threshold-signature"
        self.metrics.record_block_processing(timing_signature, 125.0)
        valid_latency = {
            "last": 125.0,
            "average": 125.0,
            "min": 125.0,
            "max": 125.0,
            "sample_count": 1,
            "warning_threshold_ms": 1000.0,
            "last_exceeds_threshold": False,
            "recent_exceeds_threshold": False,
        }
        missing_threshold_latency = {
            **valid_latency,
            "recent_exceeds_threshold": True,
        }
        missing_threshold_latency.pop("warning_threshold_ms")
        malformed_latencies = (
            (
                "missing threshold",
                missing_threshold_latency,
            ),
            (
                "non-numeric threshold",
                {
                    **valid_latency,
                    "warning_threshold_ms": "1000",
                    "recent_exceeds_threshold": True,
                },
            ),
            (
                "malformed warning flags",
                {
                    **valid_latency,
                    "warning_threshold_ms": -1.0,
                    "last_exceeds_threshold": "false",
                    "recent_exceeds_threshold": "true",
                },
            ),
        )

        for label, malformed_latency in malformed_latencies:
            with self.subTest(metadata=label):
                provider = TimingMetadataMetricsProvider(self.metrics, malformed_latency)
                timing_server = DashboardServer(provider, port=0)
                timing_server.start()
                try:
                    assert timing_server.address is not None
                    malformed_html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{timing_server.address[1]}",
                    )
                    provider.restore_timing()
                    healthy_html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{timing_server.address[1]}",
                    )
                finally:
                    timing_server.stop()

                self.assertIn(
                    'id="last-block-latency">125 ms</div>',
                    malformed_html,
                )
                self.assertEqual(
                    self._rendered_timing_status(malformed_html),
                    "Timing telemetry unavailable — provider timing data is unusable.",
                )
                self.assertIn(
                    'id="block-timing-status" role="status" hidden=""',
                    healthy_html,
                )
                self.assertEqual(self._rendered_timing_status(healthy_html), "")

    def test_retry_delay_warning_only_applies_at_threshold(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=4,
            retry_base_backoff_seconds=(
                MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD / 4
            ),
        )
        policy = metrics.snapshot()["retry_policy"]
        self.assertEqual(
            policy["configured_max_delay_seconds"],
            MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD,
        )
        self.assertTrue(policy["delay_ceiling_warning"])

        below_threshold = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=4,
            retry_base_backoff_seconds=(
                MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD / 4
                - 1
            ),
        )
        self.assertFalse(
            below_threshold.snapshot()["retry_policy"]["delay_ceiling_warning"]
        )

    def test_retry_delay_warning_uses_attempt_count_and_backoff(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=5,
            retry_base_backoff_seconds=MAX_RETRY_DELAY_SECONDS / 16,
        )
        policy = metrics.snapshot()["retry_policy"]
        self.assertEqual(
            policy["configured_max_delay_seconds"],
            MAX_RETRY_DELAY_SECONDS / 2,
        )
        self.assertFalse(policy["delay_ceiling_warning"])

    def test_pruned_recovery_boundary_is_visible_and_clears(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        with urlopen(f"{self.base_url}/api/metrics", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["recovery"]["boundary_missing_total"], 1)
        self.assertEqual(payload["recovery"]["active_pruned_boundaries"], 1)
        self.assertEqual(
            payload["recovery"]["pruned_boundaries"][0]["status"],
            "pruned",
        )
        self.assertEqual(
            payload["recovery"]["pruned_boundaries"][0]["cursor"],
            "cursor-pruned",
        )
        self.assertTrue(
            payload["recovery"]["pruned_boundaries"][0]["newly_detected"]
        )

        self.metrics.record_recovery_boundary_found("address-dashboard")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 0)
        self.assertEqual(snapshot["recovery"]["pruned_boundaries"], [])
        self.assertEqual(snapshot["recovery"]["boundary_missing_total"], 1)
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["address"],
            "address-dashboard",
        )
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["cursor"],
            "cursor-pruned",
        )
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["status"],
            "resolved",
        )
        self.assertIsInstance(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["resolved_at"],
            str,
        )

    def test_browser_refresh_renders_active_and_cleared_recovery_states(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        active_html = self._render_dashboard_in_browser()
        active_panel = self._rendered_recovery_panel(active_html)
        self.assertEqual(
            self._rendered_recovery_summary(active_html),
            "1 detected · 1 active",
        )
        self.assertIn("address-dashboard", active_panel)
        self.assertIn("cursor-pruned", active_panel)
        self.assertIn('class="status pruned"', active_panel)
        self.assertNotIn("No pruned recovery boundaries detected", active_panel)

        self.metrics.record_recovery_boundary_found("address-dashboard")
        cleared_html = self._render_dashboard_in_browser()
        cleared_panel = self._rendered_recovery_panel(cleared_html)
        self.assertEqual(
            self._rendered_recovery_summary(cleared_html),
            "1 detected · 0 active",
        )
        self.assertIn("No pruned recovery boundaries detected", cleared_panel)
        self.assertNotIn("address-dashboard", cleared_panel)
        self.assertNotIn("cursor-pruned", cleared_panel)
        self.assertNotIn('class="status pruned"', cleared_panel)
        resolved_panel = self._rendered_resolved_recovery_panel(cleared_html)
        self.assertIn("address-dashboard", resolved_panel)
        self.assertIn("cursor-pruned", resolved_panel)
        self.assertIn('class="status resolved"', resolved_panel)
        self.assertIn("Resolved incident history", cleared_html)

    def test_browser_escapes_recovery_identifiers_as_text(self) -> None:
        address = '<img src=x onerror="alert(1)">address'
        cursor = 'cursor&"><script>alert(2)</script>'
        self.metrics.record_recovery_boundary_missing(address, cursor)

        panel = self._rendered_recovery_panel(self._render_dashboard_in_browser())

        self.assertIn('&lt;img src=x onerror="alert(1)"&gt;address', panel)
        self.assertIn('cursor&amp;"&gt;&lt;script&gt;alert(2)&lt;/script&gt;', panel)
        self.assertNotIn("<img", panel)
        self.assertNotIn("<script", panel)

    def test_browser_escapes_webhook_details_in_live_tables(self) -> None:
        outbound_signature = '<img src=x onerror="alert(1)">outbound'
        outbound_event = '<script>alert(2)</script>&event'
        outbound_error = '<svg onload="alert(3)">outbound error'
        retry_signature = '<b onmouseover="alert(4)">retry'
        dead_signature = '<iframe src="javascript:alert(5)">dead'
        dead_error = '<object data="javascript:alert(6)">dead error'
        self.metrics.record_delivery_started(outbound_signature, outbound_event)
        self.metrics.record_delivery_attempt(
            signature=outbound_signature,
            event_name=outbound_event,
            attempt=1,
            status=503,
            error=outbound_error,
        )
        self.metrics.record_retry(
            signature=retry_signature,
            attempt=1,
            max_attempts=3,
            delay_seconds=30,
            error='<marquee onstart="alert(7)">retry error',
        )
        self.metrics.record_delivery_result(
            signature=dead_signature,
            event_name='<img src=x onerror="alert(8)">dead event',
            attempts=3,
            status=503,
            success=False,
            dropped=True,
            error=dead_error,
        )

        html = self._render_dashboard_in_browser()
        outbound_panel = self._rendered_table(html, "logs")
        retry_panel = self._rendered_table(html, "retries")
        dead_panel = self._rendered_table(html, "dead")

        self.assertIn(
            '&lt;img src=x onerror="alert(1)"&gt;outbound',
            outbound_panel,
        )
        self.assertIn(
            '&lt;script&gt;alert(2)&lt;/script&gt;&amp;event',
            outbound_panel,
        )
        self.assertIn(
            '&lt;svg onload="alert(3)"&gt;outbound error',
            outbound_panel,
        )
        self.assertIn(
            '&lt;b onmouseover="alert(4)"&gt;retry',
            retry_panel,
        )
        self.assertIn(
            '&lt;iframe src="javascript:alert(5)"&gt;dead',
            dead_panel,
        )
        self.assertIn(
            '&lt;object data="javascript:alert(6)"&gt;dead error',
            dead_panel,
        )
        for panel in (outbound_panel, retry_panel, dead_panel):
            self.assertNotRegex(panel, r"<(?:img|script|svg|b|iframe|object|marquee)\b")

    def test_browser_refresh_keeps_normal_empty_poll_non_incident(self) -> None:
        html = self._render_dashboard_in_browser()
        panel = self._rendered_recovery_panel(html)
        self.assertEqual(
            self._rendered_recovery_summary(html),
            "0 detected · 0 active",
        )
        self.assertIn("No pruned recovery boundaries detected", panel)
        self.assertNotIn('class="status pruned"', panel)
        self.assertNotIn("address-dashboard", panel)

    def test_browser_refresh_supports_current_and_legacy_snapshot_shapes(self) -> None:
        legacy_signature = "legacy-telemetry-signature"
        self.metrics.record_block_processing(legacy_signature, 12.5)
        current_snapshot = self.metrics.snapshot()
        legacy_snapshot = copy.deepcopy(current_snapshot)
        legacy_snapshot.pop("telemetry")
        legacy_snapshot.pop("snapshot_revision")

        fixtures = (
            ("current", current_snapshot, "1", "0", "12.5 ms", "v1"),
            (
                "legacy without telemetry",
                legacy_snapshot,
                "Unavailable",
                "Unavailable",
                "Unavailable",
                "legacy",
            ),
        )
        for name, snapshot, processed, discarded, latency, revision in fixtures:
            with self.subTest(snapshot=name):
                snapshot_server = DashboardServer(
                    SnapshotMetricsProvider(snapshot),
                    port=0,
                )
                snapshot_server.start()
                try:
                    assert snapshot_server.address is not None
                    html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{snapshot_server.address[1]}",
                    )
                finally:
                    snapshot_server.stop()

                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                self.assertIn(f'id="processed-blocks">{processed}</div>', html)
                self.assertIn(f'id="discarded-blocks">{discarded}</div>', html)
                self.assertIn(f'id="last-block-latency">{latency}</div>', html)
                self.assertIn(f'id="snapshot-revision">{revision}</span>', html)
                self.assertNotIn(legacy_signature, html)
                pulse_style, indicator_style = self._rendered_pulse(html)
                self.assertNotIn("color: var(--red)", pulse_style)
                self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_supports_snapshots_without_recovery_history_sections(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-legacy",
            "cursor-legacy",
        )
        legacy_snapshot = self.metrics.snapshot()
        recovery = legacy_snapshot["recovery"]
        assert isinstance(recovery, dict)
        recovery.pop("resolved_pruned_boundaries")
        recovery.pop("resolved_boundary_history_limit")
        recovery.pop("evicted_boundaries")

        snapshot_server = DashboardServer(
            SnapshotMetricsProvider(legacy_snapshot),
            port=0,
        )
        snapshot_server.start()
        try:
            assert snapshot_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{snapshot_server.address[1]}",
            )
        finally:
            snapshot_server.stop()

        self.assertEqual(
            self._rendered_recovery_summary(html),
            "1 detected · 1 active",
        )
        active_panel = self._rendered_recovery_panel(html)
        self.assertIn("address-legacy", active_panel)
        self.assertNotIn("No pruned recovery boundaries detected", active_panel)

        resolved_panel = self._rendered_resolved_recovery_panel(html)
        self.assertIn("No resolved recovery boundaries retained", resolved_panel)
        self.assertIn('id="recovery-history-summary">0 retained · 0 max</span>', html)

        eviction_panel = self._rendered_table(html, "recovery-evictions")
        self.assertIn("No recovery evidence has been evicted", eviction_panel)
        self.assertIn('id="recovery-audit-summary">0 retained</span>', html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_live_metrics_for_partial_telemetry_snapshot(self) -> None:
        partial_signature = "partial-telemetry-signature"
        self.metrics.record_block_processing(partial_signature, 12.5)
        partial_server = DashboardServer(
            PartialTelemetryMetricsProvider(self.metrics),
            port=0,
        )
        partial_server.start()
        try:
            assert partial_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{partial_server.address[1]}",
            )
        finally:
            partial_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">Unavailable</div>', html)
        self.assertIn('id="discarded-blocks">Unavailable</div>', html)
        self.assertIn('id="last-block-latency">Unavailable</div>', html)
        self.assertIn('id="average-block-latency">Unavailable</div>', html)
        self.assertIn('id="block-latency-samples">Unavailable</div>', html)
        self.assertEqual(
            self._rendered_timing_status(html),
            "Timing telemetry unavailable — provider timing data is unusable.",
        )
        self.assertNotIn(partial_signature, html)
        self.assertNotIn("block_processed", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_clears_timing_warning_when_samples_return(self) -> None:
        timing_signature = "timing-recovery-signature"
        self.metrics.record_block_processing(timing_signature, 12.5)
        provider = RecoveringTimingMetricsProvider(self.metrics)
        recovering_server = DashboardServer(provider, port=0)
        recovering_server.start()
        try:
            assert recovering_server.address is not None
            unavailable_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovering_server.address[1]}",
            )
            self.assertIn('id="parsed">1</div>', unavailable_html)
            self.assertIn('id="active">3</div>', unavailable_html)
            self.assertIn('id="bound">50</div>', unavailable_html)
            self.assertIn('id="processed-blocks">0</div>', unavailable_html)
            self.assertEqual(
                self._rendered_timing_status(unavailable_html),
                "Timing telemetry unavailable — provider timing data is unusable.",
            )
            self.assertNotIn(
                'id="block-timing-status" role="status" hidden=""',
                unavailable_html,
            )

            provider.restore_timing()
            recovered_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovering_server.address[1]}",
            )
        finally:
            recovering_server.stop()

        self.assertIn('id="parsed">1</div>', recovered_html)
        self.assertIn('id="active">3</div>', recovered_html)
        self.assertIn('id="bound">50</div>', recovered_html)
        self.assertIn('id="processed-blocks">1</div>', recovered_html)
        self.assertIn('id="last-block-latency">12.5 ms</div>', recovered_html)
        self.assertIn('id="average-block-latency">12.5 ms</div>', recovered_html)
        self.assertIn('id="block-latency-samples">1</div>', recovered_html)
        self.assertIn('id="block-timing-status" role="status" hidden=""', recovered_html)
        self.assertEqual(self._rendered_timing_status(recovered_html), "")

    def test_browser_refresh_keeps_live_metrics_for_malformed_telemetry_values(self) -> None:
        malformed_signature = "malformed-telemetry-signature"
        self.metrics.record_block_processing(malformed_signature, 12.5)
        malformed_server = DashboardServer(
            MalformedTelemetryMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        for element_id in (
            "processed-blocks",
            "discarded-blocks",
            "last-block-latency",
            "average-block-latency",
            "block-latency-samples",
        ):
            self.assertIn(f'id="{element_id}">Unavailable</div>', html)
        self.assertEqual(
            self._rendered_timing_status(html),
            "Timing telemetry unavailable — provider timing data is unusable.",
        )
        self.assertNotIn(malformed_signature, html)
        self.assertNotIn("block_processed", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_dashboard_readable_for_malformed_core_metrics(self) -> None:
        valid_signature = "valid-core-metrics-signature"
        self.metrics.record_block_processing(valid_signature, 12.5)
        malformed_server = DashboardServer(
            MalformedCoreMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">Unavailable</div>', html)
        self.assertIn('id="active">Unavailable</div>', html)
        self.assertIn('id="bound">Unavailable</div>', html)
        self.assertIn('id="rss">Unavailable</div>', html)
        self.assertIn('id="success">Unavailable</div>', html)
        self.assertIn(
            'id="attempts">Unavailable attempts · Unavailable drops</div>',
            html,
        )
        self.assertIn('id="processed-blocks">1</div>', html)
        self.assertIn('id="last-block-latency">12.5 ms</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertNotIn(valid_signature, html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_core_metrics_for_malformed_telemetry_containers(self) -> None:
        malformed_signature = "malformed-telemetry-container-signature"
        self.metrics.record_block_processing(malformed_signature, 12.5)

        for shape, telemetry in (
            ("scalar", "provider timing details"),
            ("array", ["provider timing details"]),
            ("null", None),
        ):
            with self.subTest(shape=shape):
                malformed_server = DashboardServer(
                    MalformedTelemetryContainerProvider(self.metrics, telemetry),
                    port=0,
                )
                malformed_server.start()
                try:
                    assert malformed_server.address is not None
                    html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{malformed_server.address[1]}",
                    )
                finally:
                    malformed_server.stop()

                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                for element_id in (
                    "processed-blocks",
                    "discarded-blocks",
                    "last-block-latency",
                    "average-block-latency",
                    "block-latency-samples",
                ):
                    self.assertIn(f'id="{element_id}">Unavailable</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    "Timing telemetry unavailable — provider timing data is unusable.",
                )
                self.assertNotIn("provider timing details", html)
                self.assertNotIn(malformed_signature, html)
                pulse_style, indicator_style = self._rendered_pulse(html)
                self.assertNotIn("color: var(--red)", pulse_style)
                self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_tables_empty_for_malformed_snapshot_lists(self) -> None:
        malformed_server = DashboardServer(
            MalformedTableCollectionsMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">0</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertIn("Waiting for outbound events…", self._rendered_table(html, "logs"))
        self.assertIn("No active backoffs", self._rendered_table(html, "retries"))
        self.assertIn("No exhausted retries", self._rendered_table(html, "dead"))
        self.assertNotIn("TypeError", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_tables_readable_for_malformed_rows(self) -> None:
        malformed_server = DashboardServer(
            MalformedTableRowsMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">0</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertIn("Waiting for outbound events…", self._rendered_table(html, "logs"))
        self.assertIn("No active backoffs", self._rendered_table(html, "retries"))
        self.assertIn("No exhausted retries", self._rendered_table(html, "dead"))
        self.assertNotIn("TypeError", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_marks_metrics_failure_on_live_pulse(self) -> None:
        failing_server = DashboardServer(FailingMetricsProvider(), port=0)
        failing_server.start()
        try:
            assert failing_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{failing_server.address[1]}",
            )
        finally:
            failing_server.stop()

        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertIn("color: var(--red)", pulse_style)
        self.assertIn("background: var(--red)", indicator_style)

    def test_browser_refresh_marks_old_latency_warning_as_stale_after_failure(
        self,
    ) -> None:
        self.metrics.record_block_processing("signature-slow-before-refresh-failure", 1_001.0)
        provider = FailingAfterFirstMetricsProvider(self.metrics)
        failing_server = DashboardServer(provider, port=0)
        failing_server.start()
        try:
            assert failing_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{failing_server.address[1]}",
            )
        finally:
            failing_server.stop()

        self.assertGreaterEqual(provider.snapshot_calls, 2)
        self.assertIn('id="last-block-latency">1,001 ms</div>', html)
        self.assertNotIn(
            "Processed block latency warning: the latest sample exceeds",
            self._rendered_timing_status(html),
        )
        self.assertEqual(
            self._rendered_timing_status(html),
            "Metrics refresh failed — displayed metrics are stale.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            html,
        )

    def test_repeated_boundary_detection_retains_one_active_alert(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 1)
        self.assertEqual(
            snapshot["recovery"]["pruned_boundaries"][0]["occurrences"],
            2,
        )
        self.assertFalse(
            snapshot["recovery"]["pruned_boundaries"][0]["newly_detected"]
        )
        self.assertEqual(
            snapshot["recovery"]["pruned_boundaries"][0]["incident_status"],
            "known_unresolved",
        )

    def test_browser_labels_trimmed_recovery_evidence(self) -> None:
        for index in range(3):
            self.metrics.record_recovery_boundary_missing(
                f"address-{index}",
                f"cursor-{index}",
            )

        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 2)
        self.assertTrue(snapshot["recovery"]["active_evidence_trimmed"])
        self.assertEqual(snapshot["recovery"]["active_evidence_trimmed_total"], 1)
        self.assertEqual(
            snapshot["recovery"]["evicted_boundaries"][0]["address"],
            "address-0",
        )
        self.assertEqual(
            snapshot["recovery"]["evicted_boundaries"][0]["cursor"],
            "cursor-0",
        )

        html = self._render_dashboard_in_browser()
        retention = self._rendered_recovery_retention(html)
        self.assertIn("Evidence retention limit reached", retention)
        self.assertIn("at most 2 active boundaries", retention)
        self.assertIn("1 row evicted", retention)
        audit = self._rendered_table(html, "recovery-evictions")
        self.assertIn("address-0", audit)
        self.assertIn("cursor-0", audit)
        self.assertNotIn("No recovery evidence has been evicted", audit)

    def test_resolved_recovery_history_is_bounded_and_newest_first(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            resolved_recovery_boundary_limit=2,
        )
        for index in range(3):
            address = f"address-{index}"
            metrics.record_recovery_boundary_missing(address, f"cursor-{index}")
            metrics.record_recovery_boundary_found(address)

        recovery = metrics.snapshot()["recovery"]
        self.assertEqual(recovery["resolved_boundary_history_limit"], 2)
        self.assertEqual(
            [boundary["address"] for boundary in recovery["resolved_pruned_boundaries"]],
            ["address-2", "address-1"],
        )

    def test_retry_countdown_and_dead_letter_are_visible(self) -> None:
        self.metrics.record_retry(
            signature="sig-failed",
            attempt=4,
            max_attempts=5,
            delay_seconds=2,
            error="HTTP 503",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["retry_monitor"][0]["signature"], "sig-failed")
        self.assertGreaterEqual(snapshot["retry_monitor"][0]["retry_in_seconds"], 0)

        self.metrics.record_delivery_result(
            signature="sig-failed",
            event_name="solana.transaction",
            attempts=5,
            status=503,
            success=False,
            dropped=True,
            error="HTTP 503",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["webhooks"]["failure_drops"], 1)
        self.assertEqual(
            snapshot["dead_letter_queue"][0]["status_label"],
            "dead_letter",
        )
        self.assertEqual(snapshot["retry_monitor"], [])

    def test_server_is_concurrent(self) -> None:
        responses: list[bytes] = []

        def read_metrics() -> None:
            with urlopen(f"{self.base_url}/api/metrics", timeout=2) as response:
                responses.append(response.read())

        threads = [threading.Thread(target=read_metrics) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(responses), 8)

    def test_latency_warning_keeps_locale_formatting_and_precision_note_consistent(self) -> None:
        formatter_start = DASHBOARD_HTML.index(
            "    const LATENCY_DISPLAY_LOCALE",
        )
        formatter_end = DASHBOARD_HTML.index(
            "    const duration",
            formatter_start,
        )
        formatter_source = DASHBOARD_HTML[formatter_start:formatter_end]
        node_script = (
            formatter_source
            + """
Number.prototype.toLocaleString = function (_locale, options) {
  return new Intl.NumberFormat("de-DE", options).format(this.valueOf());
};

const latency = {
  last: 1250.501,
  average: 9876.543,
  min: 1250.501,
  max: 1250.501,
  sample_count: 1,
  warning_threshold_ms: 1250.5,
  last_exceeds_threshold: true,
  recent_exceeds_threshold: true,
};
process.stdout.write(JSON.stringify({
  last: latencyValue(latency.last, 2),
  average: latencyValue(latency.average, 2),
  threshold: latencyValue(latency.warning_threshold_ms, 2),
  warning: timingStatusMessage(latency, 2, true),
}));
"""
        )

        result = subprocess.run(
            ["node", "--input-type=commonjs", "-e", node_script],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        rendered = json.loads(result.stdout)

        self.assertEqual(rendered["last"], "1.250,5 ms")
        self.assertEqual(rendered["average"], "9.876,54 ms")
        self.assertEqual(rendered["threshold"], "1.250,5 ms")
        self.assertEqual(
            rendered["warning"],
            "Processed block latency warning: the latest and a recent sample "
            "exceed the configured 1.250,5 ms threshold. Displayed latencies are "
            "rounded to 2 decimal places.",
        )

    def test_browser_refresh_keeps_zero_decimal_recovery_quiet_at_display_threshold(
        self,
    ) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        metrics.record_block_processing(
            "signature-zero-decimal-recovery-browser",
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS + 0.1,
        )
        provider = ThresholdTimingMetricsProvider(
            metrics,
            healthy_latency_ms=CUSTOM_LATENCY_WARNING_THRESHOLD_MS - 0.1,
            latency_display_decimal_places=0,
        )
        recovery_server = DashboardServer(provider, port=0)
        recovery_server.start()
        try:
            assert recovery_server.address is not None
            warning_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovery_server.address[1]}",
            )
            provider.restore_timing()
            recovered_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovery_server.address[1]}",
            )
        finally:
            recovery_server.stop()

        self.assertIn('id="last-block-latency">750 ms</div>', warning_html)
        self.assertIn('id="average-block-latency">750 ms</div>', warning_html)
        self.assertEqual(
            self._rendered_timing_status(warning_html),
            "Processed block latency warning: the latest and a recent sample "
            "exceed the configured 750 ms threshold. Displayed latencies are "
            "rounded to 0 decimal places.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            warning_html,
        )

        self.assertIn('id="last-block-latency">750 ms</div>', recovered_html)
        self.assertIn('id="average-block-latency">750 ms</div>', recovered_html)
        self.assertIn(
            'id="block-timing-status" role="status" hidden=""',
            recovered_html,
        )
        self.assertEqual(self._rendered_timing_status(recovered_html), "")

    def test_browser_keeps_latency_locale_stable_for_non_english_and_rtl_browsers(
        self,
    ) -> None:
        snapshot = self.metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["latency_display_decimal_places"] = 4
        latency = telemetry["block_processing_latency_ms"]
        assert isinstance(latency, dict)
        latency.update(
            {
                "last": 1_250.0,
                "average": 12.34567,
                "min": 12.34567,
                "max": 1_250.0,
                "sample_count": 2,
                "warning_threshold_ms": 987.6543,
                "last_exceeds_threshold": True,
                "recent_exceeds_threshold": True,
            }
        )

        snapshot_server = DashboardServer(
            SnapshotMetricsProvider(snapshot),
            port=0,
        )
        snapshot_server.start()
        try:
            assert snapshot_server.address is not None
            browser_url = f"http://127.0.0.1:{snapshot_server.address[1]}"
            rendered_by_locale = {
                browser_locale: self._render_dashboard_in_browser(
                    browser_url,
                    browser_locale=browser_locale,
                )
                for browser_locale in ("de-DE", "fr-FR", "pt-BR", "ar-EG")
            }
        finally:
            snapshot_server.stop()

        for browser_locale, html in rendered_by_locale.items():
            with self.subTest(browser_locale=browser_locale):
                self.assertIn('id="last-block-latency">1,250 ms</div>', html)
                self.assertIn('id="average-block-latency">12.3457 ms</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    (
                        "Processed block latency warning: the latest and a recent "
                        "sample exceed the configured 987.6543 ms threshold."
                    ),
                )

    def test_metrics_endpoint_returns_controlled_error_for_cyclic_nested_snapshot(
        self,
    ) -> None:
        invalid_snapshot = self.metrics.snapshot()
        nested_snapshot: dict[str, object] = {}
        nested_snapshot["self"] = nested_snapshot
        invalid_snapshot["provider_value"] = {"nested": nested_snapshot}
        invalid_server = DashboardServer(
            TopLevelSnapshotProvider(invalid_snapshot),
            port=0,
        )
        invalid_server.start()
        try:
            assert invalid_server.address is not None
            with self.assertRaises(HTTPError) as raised:
                urlopen(
                    f"http://127.0.0.1:{invalid_server.address[1]}/api/metrics",
                    timeout=2,
                )
        finally:
            invalid_server.stop()

        response = raised.exception
        self.assertEqual(response.status, 502)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/json; charset=utf-8",
        )
        response_body = response.read().decode("utf-8")
        self.assertNotIn("Circular reference detected", response_body)
        self.assertEqual(json.loads(response_body), {"error": "metrics_invalid"})
            }
        finally:
            snapshot_server.stop()

        for browser_locale, html in rendered_by_locale.items():
            with self.subTest(browser_locale=browser_locale):
                self.assertIn('id="last-block-latency">1,250 ms</div>', html)
                self.assertIn('id="average-block-latency">12.3457 ms</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    (
                        "Processed block latency warning: the latest and a recent "
                        "sample exceed the configured 987.6543 ms threshold."
                    ),
                )

    def test_browser_refresh_keeps_zero_decimal_recovery_quiet_at_display_threshold(
        self,
    ) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        metrics.record_block_processing(
            "signature-zero-decimal-recovery-browser",
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS + 0.1,
        )
        provider = ThresholdTimingMetricsProvider(
            metrics,
            healthy_latency_ms=CUSTOM_LATENCY_WARNING_THRESHOLD_MS - 0.1,
            latency_display_decimal_places=0,
        )
        recovery_server = DashboardServer(provider, port=0)
        recovery_server.start()
        try:
            assert recovery_server.address is not None
            warning_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovery_server.address[1]}",
            )
            provider.restore_timing()
            recovered_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovery_server.address[1]}",
            )
        finally:
            recovery_server.stop()

        self.assertIn('id="last-block-latency">750 ms</div>', warning_html)
        self.assertIn('id="average-block-latency">750 ms</div>', warning_html)
        self.assertEqual(
            self._rendered_timing_status(warning_html),
            "Processed block latency warning: the latest and a recent sample "
            "exceed the configured 750 ms threshold. Displayed latencies are "
            "rounded to 0 decimal places.",
        )
        self.assertNotIn(
            'id="block-timing-status" role="status" hidden=""',
            warning_html,
        )

        self.assertIn('id="last-block-latency">750 ms</div>', recovered_html)
        self.assertIn('id="average-block-latency">750 ms</div>', recovered_html)
        self.assertIn(
            'id="block-timing-status" role="status" hidden=""',
            recovered_html,
        )
        self.assertEqual(self._rendered_timing_status(recovered_html), "")

    def test_retry_delay_warning_only_applies_at_threshold(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=4,
            retry_base_backoff_seconds=(
                MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD / 4
            ),
        )
        policy = metrics.snapshot()["retry_policy"]
        self.assertEqual(
            policy["configured_max_delay_seconds"],
            MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD,
        )
        self.assertTrue(policy["delay_ceiling_warning"])

        below_threshold = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=4,
            retry_base_backoff_seconds=(
                MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD / 4
                - 1
            ),
        )
        self.assertFalse(
            below_threshold.snapshot()["retry_policy"]["delay_ceiling_warning"]
        )

    def test_retry_delay_warning_uses_attempt_count_and_backoff(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            retry_max_attempts=5,
            retry_base_backoff_seconds=MAX_RETRY_DELAY_SECONDS / 16,
        )
        policy = metrics.snapshot()["retry_policy"]
        self.assertEqual(
            policy["configured_max_delay_seconds"],
            MAX_RETRY_DELAY_SECONDS / 2,
        )
        self.assertFalse(policy["delay_ceiling_warning"])

    def test_pruned_recovery_boundary_is_visible_and_clears(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        with urlopen(f"{self.base_url}/api/metrics", timeout=2) as response:
            payload = json.load(response)
        self.assertEqual(payload["recovery"]["boundary_missing_total"], 1)
        self.assertEqual(payload["recovery"]["active_pruned_boundaries"], 1)
        self.assertEqual(
            payload["recovery"]["pruned_boundaries"][0]["status"],
            "pruned",
        )
        self.assertEqual(
            payload["recovery"]["pruned_boundaries"][0]["cursor"],
            "cursor-pruned",
        )
        self.assertTrue(
            payload["recovery"]["pruned_boundaries"][0]["newly_detected"]
        )

        self.metrics.record_recovery_boundary_found("address-dashboard")
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 0)
        self.assertEqual(snapshot["recovery"]["pruned_boundaries"], [])
        self.assertEqual(snapshot["recovery"]["boundary_missing_total"], 1)
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["address"],
            "address-dashboard",
        )
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["cursor"],
            "cursor-pruned",
        )
        self.assertEqual(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["status"],
            "resolved",
        )
        self.assertIsInstance(
            snapshot["recovery"]["resolved_pruned_boundaries"][0]["resolved_at"],
            str,
        )

    def test_browser_refresh_renders_active_and_cleared_recovery_states(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        active_html = self._render_dashboard_in_browser()
        active_panel = self._rendered_recovery_panel(active_html)
        self.assertEqual(
            self._rendered_recovery_summary(active_html),
            "1 detected · 1 active",
        )
        self.assertIn("address-dashboard", active_panel)
        self.assertIn("cursor-pruned", active_panel)
        self.assertIn('class="status pruned"', active_panel)
        self.assertNotIn("No pruned recovery boundaries detected", active_panel)

        self.metrics.record_recovery_boundary_found("address-dashboard")
        cleared_html = self._render_dashboard_in_browser()
        cleared_panel = self._rendered_recovery_panel(cleared_html)
        self.assertEqual(
            self._rendered_recovery_summary(cleared_html),
            "1 detected · 0 active",
        )
        self.assertIn("No pruned recovery boundaries detected", cleared_panel)
        self.assertNotIn("address-dashboard", cleared_panel)
        self.assertNotIn("cursor-pruned", cleared_panel)
        self.assertNotIn('class="status pruned"', cleared_panel)
        resolved_panel = self._rendered_resolved_recovery_panel(cleared_html)
        self.assertIn("address-dashboard", resolved_panel)
        self.assertIn("cursor-pruned", resolved_panel)
        self.assertIn('class="status resolved"', resolved_panel)
        self.assertIn("Resolved incident history", cleared_html)

    def test_browser_escapes_recovery_identifiers_as_text(self) -> None:
        address = '<img src=x onerror="alert(1)">address'
        cursor = 'cursor&"><script>alert(2)</script>'
        self.metrics.record_recovery_boundary_missing(address, cursor)

        panel = self._rendered_recovery_panel(self._render_dashboard_in_browser())

        self.assertIn('&lt;img src=x onerror="alert(1)"&gt;address', panel)
        self.assertIn('cursor&amp;"&gt;&lt;script&gt;alert(2)&lt;/script&gt;', panel)
        self.assertNotIn("<img", panel)
        self.assertNotIn("<script", panel)

    def test_browser_escapes_webhook_details_in_live_tables(self) -> None:
        outbound_signature = '<img src=x onerror="alert(1)">outbound'
        outbound_event = '<script>alert(2)</script>&event'
        outbound_error = '<svg onload="alert(3)">outbound error'
        retry_signature = '<b onmouseover="alert(4)">retry'
        dead_signature = '<iframe src="javascript:alert(5)">dead'
        dead_error = '<object data="javascript:alert(6)">dead error'
        self.metrics.record_delivery_started(outbound_signature, outbound_event)
        self.metrics.record_delivery_attempt(
            signature=outbound_signature,
            event_name=outbound_event,
            attempt=1,
            status=503,
            error=outbound_error,
        )
        self.metrics.record_retry(
            signature=retry_signature,
            attempt=1,
            max_attempts=3,
            delay_seconds=30,
            error='<marquee onstart="alert(7)">retry error',
        )
        self.metrics.record_delivery_result(
            signature=dead_signature,
            event_name='<img src=x onerror="alert(8)">dead event',
            attempts=3,
            status=503,
            success=False,
            dropped=True,
            error=dead_error,
        )

        html = self._render_dashboard_in_browser()
        outbound_panel = self._rendered_table(html, "logs")
        retry_panel = self._rendered_table(html, "retries")
        dead_panel = self._rendered_table(html, "dead")

        self.assertIn(
            '&lt;img src=x onerror="alert(1)"&gt;outbound',
            outbound_panel,
        )
        self.assertIn(
            '&lt;script&gt;alert(2)&lt;/script&gt;&amp;event',
            outbound_panel,
        )
        self.assertIn(
            '&lt;svg onload="alert(3)"&gt;outbound error',
            outbound_panel,
        )
        self.assertIn(
            '&lt;b onmouseover="alert(4)"&gt;retry',
            retry_panel,
        )
        self.assertIn(
            '&lt;iframe src="javascript:alert(5)"&gt;dead',
            dead_panel,
        )
        self.assertIn(
            '&lt;object data="javascript:alert(6)"&gt;dead error',
            dead_panel,
        )
        for panel in (outbound_panel, retry_panel, dead_panel):
            self.assertNotRegex(panel, r"<(?:img|script|svg|b|iframe|object|marquee)\b")

    def test_browser_refresh_keeps_normal_empty_poll_non_incident(self) -> None:
        html = self._render_dashboard_in_browser()
        panel = self._rendered_recovery_panel(html)
        self.assertEqual(
            self._rendered_recovery_summary(html),
            "0 detected · 0 active",
        )
        self.assertIn("No pruned recovery boundaries detected", panel)
        self.assertNotIn('class="status pruned"', panel)
        self.assertNotIn("address-dashboard", panel)

    def test_browser_refresh_supports_current_and_legacy_snapshot_shapes(self) -> None:
        legacy_signature = "legacy-telemetry-signature"
        self.metrics.record_block_processing(legacy_signature, 12.5)
        current_snapshot = self.metrics.snapshot()
        legacy_snapshot = copy.deepcopy(current_snapshot)
        legacy_snapshot.pop("telemetry")
        legacy_snapshot.pop("snapshot_revision")

        fixtures = (
            ("current", current_snapshot, "1", "0", "12.5 ms", "v1"),
            (
                "legacy without telemetry",
                legacy_snapshot,
                "Unavailable",
                "Unavailable",
                "Unavailable",
                "legacy",
            ),
        )
        for name, snapshot, processed, discarded, latency, revision in fixtures:
            with self.subTest(snapshot=name):
                snapshot_server = DashboardServer(
                    SnapshotMetricsProvider(snapshot),
                    port=0,
                )
                snapshot_server.start()
                try:
                    assert snapshot_server.address is not None
                    html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{snapshot_server.address[1]}",
                    )
                finally:
                    snapshot_server.stop()

                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                self.assertIn(f'id="processed-blocks">{processed}</div>', html)
                self.assertIn(f'id="discarded-blocks">{discarded}</div>', html)
                self.assertIn(f'id="last-block-latency">{latency}</div>', html)
                self.assertIn(f'id="snapshot-revision">{revision}</span>', html)
                self.assertNotIn(legacy_signature, html)
                pulse_style, indicator_style = self._rendered_pulse(html)
                self.assertNotIn("color: var(--red)", pulse_style)
                self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_supports_snapshots_without_recovery_history_sections(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-legacy",
            "cursor-legacy",
        )
        legacy_snapshot = self.metrics.snapshot()
        recovery = legacy_snapshot["recovery"]
        assert isinstance(recovery, dict)
        recovery.pop("resolved_pruned_boundaries")
        recovery.pop("resolved_boundary_history_limit")
        recovery.pop("evicted_boundaries")

        snapshot_server = DashboardServer(
            SnapshotMetricsProvider(legacy_snapshot),
            port=0,
        )
        snapshot_server.start()
        try:
            assert snapshot_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{snapshot_server.address[1]}",
            )
        finally:
            snapshot_server.stop()

        self.assertEqual(
            self._rendered_recovery_summary(html),
            "1 detected · 1 active",
        )
        active_panel = self._rendered_recovery_panel(html)
        self.assertIn("address-legacy", active_panel)
        self.assertNotIn("No pruned recovery boundaries detected", active_panel)

        resolved_panel = self._rendered_resolved_recovery_panel(html)
        self.assertIn("No resolved recovery boundaries retained", resolved_panel)
        self.assertIn('id="recovery-history-summary">0 retained · 0 max</span>', html)

        eviction_panel = self._rendered_table(html, "recovery-evictions")
        self.assertIn("No recovery evidence has been evicted", eviction_panel)
        self.assertIn('id="recovery-audit-summary">0 retained</span>', html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_live_metrics_for_partial_telemetry_snapshot(self) -> None:
        partial_signature = "partial-telemetry-signature"
        self.metrics.record_block_processing(partial_signature, 12.5)
        partial_server = DashboardServer(
            PartialTelemetryMetricsProvider(self.metrics),
            port=0,
        )
        partial_server.start()
        try:
            assert partial_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{partial_server.address[1]}",
            )
        finally:
            partial_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">Unavailable</div>', html)
        self.assertIn('id="discarded-blocks">Unavailable</div>', html)
        self.assertIn('id="last-block-latency">Unavailable</div>', html)
        self.assertIn('id="average-block-latency">Unavailable</div>', html)
        self.assertIn('id="block-latency-samples">Unavailable</div>', html)
        self.assertEqual(
            self._rendered_timing_status(html),
            "Timing telemetry unavailable — provider timing data is unusable.",
        )
        self.assertNotIn(partial_signature, html)
        self.assertNotIn("block_processed", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_clears_timing_warning_when_samples_return(self) -> None:
        timing_signature = "timing-recovery-signature"
        self.metrics.record_block_processing(timing_signature, 12.5)
        provider = RecoveringTimingMetricsProvider(self.metrics)
        recovering_server = DashboardServer(provider, port=0)
        recovering_server.start()
        try:
            assert recovering_server.address is not None
            unavailable_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovering_server.address[1]}",
            )
            self.assertIn('id="parsed">1</div>', unavailable_html)
            self.assertIn('id="active">3</div>', unavailable_html)
            self.assertIn('id="bound">50</div>', unavailable_html)
            self.assertIn('id="processed-blocks">0</div>', unavailable_html)
            self.assertEqual(
                self._rendered_timing_status(unavailable_html),
                "Timing telemetry unavailable — provider timing data is unusable.",
            )
            self.assertNotIn(
                'id="block-timing-status" role="status" hidden=""',
                unavailable_html,
            )

            provider.restore_timing()
            recovered_html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{recovering_server.address[1]}",
            )
        finally:
            recovering_server.stop()

        self.assertIn('id="parsed">1</div>', recovered_html)
        self.assertIn('id="active">3</div>', recovered_html)
        self.assertIn('id="bound">50</div>', recovered_html)
        self.assertIn('id="processed-blocks">1</div>', recovered_html)
        self.assertIn('id="last-block-latency">12.5 ms</div>', recovered_html)
        self.assertIn('id="average-block-latency">12.5 ms</div>', recovered_html)
        self.assertIn('id="block-latency-samples">1</div>', recovered_html)
        self.assertIn('id="block-timing-status" role="status" hidden=""', recovered_html)
        self.assertEqual(self._rendered_timing_status(recovered_html), "")

    def test_browser_refresh_keeps_live_metrics_for_malformed_telemetry_values(self) -> None:
        malformed_signature = "malformed-telemetry-signature"
        self.metrics.record_block_processing(malformed_signature, 12.5)
        malformed_server = DashboardServer(
            MalformedTelemetryMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        for element_id in (
            "processed-blocks",
            "discarded-blocks",
            "last-block-latency",
            "average-block-latency",
            "block-latency-samples",
        ):
            self.assertIn(f'id="{element_id}">Unavailable</div>', html)
        self.assertEqual(
            self._rendered_timing_status(html),
            "Timing telemetry unavailable — provider timing data is unusable.",
        )
        self.assertNotIn(malformed_signature, html)
        self.assertNotIn("block_processed", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_dashboard_readable_for_malformed_core_metrics(self) -> None:
        valid_signature = "valid-core-metrics-signature"
        self.metrics.record_block_processing(valid_signature, 12.5)
        malformed_server = DashboardServer(
            MalformedCoreMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">Unavailable</div>', html)
        self.assertIn('id="active">Unavailable</div>', html)
        self.assertIn('id="bound">Unavailable</div>', html)
        self.assertIn('id="rss">Unavailable</div>', html)
        self.assertIn('id="success">Unavailable</div>', html)
        self.assertIn(
            'id="attempts">Unavailable attempts · Unavailable drops</div>',
            html,
        )
        self.assertIn('id="processed-blocks">1</div>', html)
        self.assertIn('id="last-block-latency">12.5 ms</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertNotIn(valid_signature, html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_core_metrics_for_malformed_telemetry_containers(self) -> None:
        malformed_signature = "malformed-telemetry-container-signature"
        self.metrics.record_block_processing(malformed_signature, 12.5)

        for shape, telemetry in (
            ("scalar", "provider timing details"),
            ("array", ["provider timing details"]),
            ("null", None),
        ):
            with self.subTest(shape=shape):
                malformed_server = DashboardServer(
                    MalformedTelemetryContainerProvider(self.metrics, telemetry),
                    port=0,
                )
                malformed_server.start()
                try:
                    assert malformed_server.address is not None
                    html = self._render_dashboard_in_browser(
                        f"http://127.0.0.1:{malformed_server.address[1]}",
                    )
                finally:
                    malformed_server.stop()

                self.assertIn('id="parsed">1</div>', html)
                self.assertIn('id="active">3</div>', html)
                self.assertIn('id="bound">50</div>', html)
                for element_id in (
                    "processed-blocks",
                    "discarded-blocks",
                    "last-block-latency",
                    "average-block-latency",
                    "block-latency-samples",
                ):
                    self.assertIn(f'id="{element_id}">Unavailable</div>', html)
                self.assertEqual(
                    self._rendered_timing_status(html),
                    "Timing telemetry unavailable — provider timing data is unusable.",
                )
                self.assertNotIn("provider timing details", html)
                self.assertNotIn(malformed_signature, html)
                pulse_style, indicator_style = self._rendered_pulse(html)
                self.assertNotIn("color: var(--red)", pulse_style)
                self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_tables_empty_for_malformed_snapshot_lists(self) -> None:
        malformed_server = DashboardServer(
            MalformedTableCollectionsMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">0</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertIn("Waiting for outbound events…", self._rendered_table(html, "logs"))
        self.assertIn("No active backoffs", self._rendered_table(html, "retries"))
        self.assertIn("No exhausted retries", self._rendered_table(html, "dead"))
        self.assertNotIn("TypeError", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_keeps_tables_readable_for_malformed_rows(self) -> None:
        malformed_server = DashboardServer(
            MalformedTableRowsMetricsProvider(self.metrics),
            port=0,
        )
        malformed_server.start()
        try:
            assert malformed_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{malformed_server.address[1]}",
            )
        finally:
            malformed_server.stop()

        self.assertIn('id="parsed">1</div>', html)
        self.assertIn('id="active">3</div>', html)
        self.assertIn('id="bound">50</div>', html)
        self.assertIn('id="processed-blocks">0</div>', html)
        self.assertIn('id="retry-attempts">7 total</div>', html)
        self.assertIn('id="recovery-summary">0 detected · 0 active</span>', html)
        self.assertIn('id="uptime">0m</span>', html)
        self.assertIn("Waiting for outbound events…", self._rendered_table(html, "logs"))
        self.assertIn("No active backoffs", self._rendered_table(html, "retries"))
        self.assertIn("No exhausted retries", self._rendered_table(html, "dead"))
        self.assertNotIn("TypeError", html)
        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertNotIn("color: var(--red)", pulse_style)
        self.assertNotIn("background: var(--red)", indicator_style)

    def test_browser_refresh_marks_metrics_failure_on_live_pulse(self) -> None:
        failing_server = DashboardServer(FailingMetricsProvider(), port=0)
        failing_server.start()
        try:
            assert failing_server.address is not None
            html = self._render_dashboard_in_browser(
                f"http://127.0.0.1:{failing_server.address[1]}",
            )
        finally:
            failing_server.stop()

        pulse_style, indicator_style = self._rendered_pulse(html)
        self.assertIn("color: var(--red)", pulse_style)
        self.assertIn("background: var(--red)", indicator_style)

    def test_repeated_boundary_detection_retains_one_active_alert(self) -> None:
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        self.metrics.record_recovery_boundary_missing(
            "address-dashboard",
            "cursor-pruned",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 1)
        self.assertEqual(
            snapshot["recovery"]["pruned_boundaries"][0]["occurrences"],
            2,
        )
        self.assertFalse(
            snapshot["recovery"]["pruned_boundaries"][0]["newly_detected"]
        )
        self.assertEqual(
            snapshot["recovery"]["pruned_boundaries"][0]["incident_status"],
            "known_unresolved",
        )

    def test_browser_labels_trimmed_recovery_evidence(self) -> None:
        for index in range(3):
            self.metrics.record_recovery_boundary_missing(
                f"address-{index}",
                f"cursor-{index}",
            )

        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["recovery"]["active_pruned_boundaries"], 2)
        self.assertTrue(snapshot["recovery"]["active_evidence_trimmed"])
        self.assertEqual(snapshot["recovery"]["active_evidence_trimmed_total"], 1)
        self.assertEqual(
            snapshot["recovery"]["evicted_boundaries"][0]["address"],
            "address-0",
        )
        self.assertEqual(
            snapshot["recovery"]["evicted_boundaries"][0]["cursor"],
            "cursor-0",
        )

        html = self._render_dashboard_in_browser()
        retention = self._rendered_recovery_retention(html)
        self.assertIn("Evidence retention limit reached", retention)
        self.assertIn("at most 2 active boundaries", retention)
        self.assertIn("1 row evicted", retention)
        audit = self._rendered_table(html, "recovery-evictions")
        self.assertIn("address-0", audit)
        self.assertIn("cursor-0", audit)
        self.assertNotIn("No recovery evidence has been evicted", audit)

    def test_resolved_recovery_history_is_bounded_and_newest_first(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=40,
            resolved_recovery_boundary_limit=2,
        )
        for index in range(3):
            address = f"address-{index}"
            metrics.record_recovery_boundary_missing(address, f"cursor-{index}")
            metrics.record_recovery_boundary_found(address)

        recovery = metrics.snapshot()["recovery"]
        self.assertEqual(recovery["resolved_boundary_history_limit"], 2)
        self.assertEqual(
            [boundary["address"] for boundary in recovery["resolved_pruned_boundaries"]],
            ["address-2", "address-1"],
        )

    def test_retry_countdown_and_dead_letter_are_visible(self) -> None:
        self.metrics.record_retry(
            signature="sig-failed",
            attempt=4,
            max_attempts=5,
            delay_seconds=2,
            error="HTTP 503",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["retry_monitor"][0]["signature"], "sig-failed")
        self.assertGreaterEqual(snapshot["retry_monitor"][0]["retry_in_seconds"], 0)

        self.metrics.record_delivery_result(
            signature="sig-failed",
            event_name="solana.transaction",
            attempts=5,
            status=503,
            success=False,
            dropped=True,
            error="HTTP 503",
        )
        snapshot = self.metrics.snapshot()
        self.assertEqual(snapshot["webhooks"]["failure_drops"], 1)
        self.assertEqual(
            snapshot["dead_letter_queue"][0]["status_label"],
            "dead_letter",
        )
        self.assertEqual(snapshot["retry_monitor"], [])

    def test_server_is_concurrent(self) -> None:
        responses: list[bytes] = []

        def read_metrics() -> None:
            with urlopen(f"{self.base_url}/api/metrics", timeout=2) as response:
                responses.append(response.read())

        threads = [threading.Thread(target=read_metrics) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(responses), 8)

class StartupRetryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_and_engine_metrics_match_webhook_retry_configuration(
        self,
    ) -> None:
        settings = Settings(
            rpc_urls=("https://rpc.example.test",),
            commitment="confirmed",
            watch_addresses=("11111111111111111111111111111111",),
            watch_signatures=(),
            webhook_url="https://webhook.example.test/events",
            webhook_secret="secret-that-is-long-enough",
            webhook_max_attempts=4,
            webhook_base_backoff_seconds=7.5,
            block_processing_latency_warning_threshold_ms=(
                CUSTOM_LATENCY_WARNING_THRESHOLD_MS
            ),
        )
        cli_startup: dict[str, object] = {}

        class NoopRpc:
            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            async def __aenter__(self) -> "NoopRpc":
                return self

            async def __aexit__(self, *_: object) -> None:
                return None

        class CapturingEngine:
            def __init__(
                self,
                _settings: Settings,
                _rpc: NoopRpc,
                delivery: WebhookDelivery,
                *,
                metrics: DashboardMetrics,
                telemetry: object,
            ) -> None:
                cli_startup["delivery"] = delivery
                cli_startup["metrics"] = metrics
                cli_startup["telemetry"] = telemetry

            async def run(self, stop_event: asyncio.Event) -> None:
                stop_event.set()

        with (
            patch.object(cli, "SolanaRpcClient", NoopRpc),
            patch.object(cli, "CoreRailEngine", CapturingEngine),
        ):
            await cli._run(settings, dashboard_host="127.0.0.1", dashboard_port=0)

        cli_delivery = cli_startup["delivery"]
        cli_metrics = cli_startup["metrics"]
        self.assertIsInstance(cli_delivery, WebhookDelivery)
        self.assertIsInstance(cli_metrics, DashboardMetrics)
        self.assertIs(cli_delivery._metrics, cli_metrics)
        self.assertEqual(
            cli_metrics.snapshot()["retry_policy"]["max_attempts"],
            cli_delivery._max_attempts,
        )
        self.assertEqual(cli_delivery._max_attempts, settings.webhook_max_attempts)
        self.assertEqual(
            cli_metrics.snapshot()["retry_policy"]["base_backoff_seconds"],
            cli_delivery._base_backoff_seconds,
        )
        self.assertEqual(
            cli_delivery._base_backoff_seconds,
            settings.webhook_base_backoff_seconds,
        )
        self.assertEqual(
            cli_metrics.snapshot()["telemetry"]["block_processing_latency_ms"][
                "warning_threshold_ms"
            ],
            CUSTOM_LATENCY_WARNING_THRESHOLD_MS,
        )

        with tempfile.TemporaryDirectory() as directory:
            fallback_settings = replace(
                settings,
                state_path=f"{directory}/state.sqlite3",
            )
            fallback_engine = CoreRailEngine(
                fallback_settings,
                rpc=object(),  # type: ignore[arg-type]
                delivery=object(),  # type: ignore[arg-type]
            )
            try:
                fallback_delivery = WebhookDelivery(
                    url=fallback_settings.webhook_url,
                    secret=fallback_settings.webhook_secret,
                    timeout_seconds=fallback_settings.webhook_timeout_seconds,
                    max_attempts=fallback_settings.webhook_max_attempts,
                    base_backoff_seconds=fallback_settings.webhook_base_backoff_seconds,
                )
                self.assertEqual(
                    fallback_engine.metrics.snapshot()["retry_policy"]["max_attempts"],
                    fallback_delivery._max_attempts,
                )
                self.assertEqual(
                    fallback_delivery._max_attempts,
                    fallback_settings.webhook_max_attempts,
                )
                self.assertEqual(
                    fallback_engine.metrics.snapshot()["retry_policy"][
                        "base_backoff_seconds"
                    ],
                    fallback_delivery._base_backoff_seconds,
                )
                self.assertEqual(
                    fallback_engine.metrics.snapshot()["telemetry"][
                        "block_processing_latency_ms"
                    ]["warning_threshold_ms"],
                    CUSTOM_LATENCY_WARNING_THRESHOLD_MS,
                )
                self.assertEqual(
                    fallback_delivery._base_backoff_seconds,
                    fallback_settings.webhook_base_backoff_seconds,
                )
            finally:
                fallback_engine._state.close()  # type: ignore[attr-defined]
class FailingMetricsProvider:
    """Metrics provider used to exercise the dashboard's failed refresh path."""

    ERROR_DETAIL = "internal provider failure detail"

    def __init__(self, error_detail: str | None = None) -> None:
        self._error_detail = error_detail or self.ERROR_DETAIL

    def snapshot(self) -> dict[str, object]:
        raise RuntimeError(self._error_detail)

class TopLevelSnapshotProvider:
    """Metrics provider used to exercise the endpoint's top-level contract."""

    def __init__(self, snapshot: object) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> object:
        return copy.deepcopy(self._snapshot)
class SnapshotMetricsProvider:
    """Metrics provider that serves a stable current or historical snapshot fixture."""

    def __init__(self, snapshot: dict[str, object]) -> None:
        self._snapshot = snapshot
        self.snapshot_calls = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        return copy.deepcopy(self._snapshot)

class LatencyPrecisionMetricsProvider:
    """Metrics provider whose display precision can be repaired between refreshes."""

    def __init__(
        self,
        metrics: DashboardMetrics,
        malformed_precision: object,
    ) -> None:
        self._metrics = metrics
        self._malformed_precision = malformed_precision
        self._healthy = False

    def restore_timing(self) -> None:
        self._healthy = True

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        if self._healthy:
            return snapshot
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        if self._malformed_precision is None:
            telemetry.pop("latency_display_decimal_places", None)
        else:
            telemetry["latency_display_decimal_places"] = self._malformed_precision
        return snapshot

class PartialTelemetryMetricsProvider:
    """Metrics provider with a present but incomplete telemetry section."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["blocks_processed"] = None
        telemetry.pop("blocks_discarded", None)
        telemetry["block_processing_latency_ms"] = {"last": None}
        return snapshot

class RecoveringTimingMetricsProvider:
    """Metrics provider whose timing samples become available on a later refresh."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics
        self._timing_available = False

    def restore_timing(self) -> None:
        self._timing_available = True

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        if self._timing_available:
            return snapshot
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["blocks_processed"] = 0
        telemetry["block_processing_latency_ms"] = {
            "last": None,
            "average": None,
            "sample_count": 0,
        }
        return snapshot


class ThresholdTimingMetricsProvider:
    """Metrics provider that returns high latency before a healthy refresh."""

    def __init__(
        self,
        metrics: DashboardMetrics,
        *,
        healthy_latency_ms: float = 125.0,
        latency_display_decimal_places: int | None = None,
    ) -> None:
        self._metrics = metrics
        self._healthy_latency_ms = healthy_latency_ms
        self._latency_display_decimal_places = latency_display_decimal_places
        self._healthy = False

    def restore_timing(self) -> None:
        self._healthy = True

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        if self._latency_display_decimal_places is not None:
            telemetry = snapshot["telemetry"]
            assert isinstance(telemetry, dict)
            telemetry["latency_display_decimal_places"] = (
                self._latency_display_decimal_places
            )
        if not self._healthy:
            return snapshot
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        current_latency = telemetry["block_processing_latency_ms"]
        assert isinstance(current_latency, dict)
        telemetry["block_processing_latency_ms"] = {
            "last": self._healthy_latency_ms,
            "average": self._healthy_latency_ms,
            "min": self._healthy_latency_ms,
            "max": self._healthy_latency_ms,
            "sample_count": 1,
            "warning_threshold_ms": current_latency["warning_threshold_ms"],
            "last_exceeds_threshold": False,
            "recent_exceeds_threshold": False,
        }
        return snapshot

class TimingMetadataMetricsProvider:
    """Metrics provider whose threshold metadata can be repaired between refreshes."""

    def __init__(
        self,
        metrics: DashboardMetrics,
        malformed_latency: dict[str, object],
    ) -> None:
        self._metrics = metrics
        self._malformed_latency = copy.deepcopy(malformed_latency)
        self._healthy = False

    def restore_timing(self) -> None:
        self._healthy = True

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        if self._healthy:
            return snapshot
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["block_processing_latency_ms"] = copy.deepcopy(self._malformed_latency)
        return snapshot
class DiscardedTimingMetricsProvider:
    """Metrics provider with discarded timing activity but no valid samples."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["blocks_processed"] = 0
        telemetry["block_processing_latency_ms"] = {
            "last": 0.0,
            "average": 0.0,
            "min": 0.0,
            "max": 0.0,
            "sample_count": 0,
            "warning_threshold_ms": 1000.0,
            "last_exceeds_threshold": False,
            "recent_exceeds_threshold": False,
        }
        return snapshot

class MalformedTelemetryMetricsProvider:
    """Metrics provider with JSON-safe but unusable timing values."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        telemetry = snapshot["telemetry"]
        assert isinstance(telemetry, dict)
        telemetry["blocks_processed"] = "not-a-number"
        telemetry["blocks_discarded"] = {"count": 1}
        telemetry["block_processing_latency_ms"] = {
            "last": "NaN",
            "average": "Infinity",
            "sample_count": ["2"],
        }
        return snapshot

class MalformedTelemetryContainerProvider:
    """Metrics provider with a scalar, array, or null telemetry container."""

    def __init__(
        self,
        metrics: DashboardMetrics,
        telemetry: object,
    ) -> None:
        self._metrics = metrics
        self._telemetry = telemetry

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        snapshot["telemetry"] = self._telemetry
        return snapshot
class MalformedCoreMetricsProvider:
    """Metrics provider with JSON-safe but unusable core card values."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        streaming = snapshot["streaming"]
        memory = snapshot["memory"]
        webhooks = snapshot["webhooks"]
        assert isinstance(streaming, dict)
        assert isinstance(memory, dict)
        assert isinstance(webhooks, dict)
        streaming["parsed_signatures"] = None
        streaming["active_events"] = {"count": 3}
        memory["tracked_event_bound"] = "not-a-number"
        memory["process_rss_mb"] = {"mb": 1}
        webhooks["success_rate_percent"] = None
        webhooks["delivery_attempts"] = []
        webhooks["failure_drops"] = {"count": 1}
        return snapshot


class MalformedTableCollectionsMetricsProvider:
    """Metrics provider with valid cards and unusable table collections."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        snapshot["webhook_logs"] = {"entries": []}
        snapshot["retry_monitor"] = "not-a-list"
        snapshot["dead_letter_queue"] = None
        return snapshot


class MalformedTableRowsMetricsProvider:
    """Metrics provider with valid collections containing unusable rows."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics

    def snapshot(self) -> dict[str, object]:
        snapshot = self._metrics.snapshot()
        snapshot["webhook_logs"] = [None, "not-a-row"]
        snapshot["retry_monitor"] = [None, ["not-a-row"]]
        snapshot["dead_letter_queue"] = ["not-a-row", None]
        return snapshot

class FailingAfterFirstMetricsProvider:
    """Metrics provider that fails after the first successful dashboard refresh."""

    def __init__(self, metrics: DashboardMetrics) -> None:
        self._metrics = metrics
        self.snapshot_calls = 0

    def snapshot(self) -> dict[str, object]:
        self.snapshot_calls += 1
        if self.snapshot_calls > 1:
            raise RuntimeError("metrics unavailable after initial refresh")
        return self._metrics.snapshot()
