"""Tests for stream telemetry and engine integration."""

from __future__ import annotations

import unittest

from solana_webhook_core_rail_pro.observability import (
    BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
    BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS,
    DashboardMetrics,
)
from solana_webhook_core_rail_pro.telemetry import TllamaStreamTelemetry


class TllamaStreamTelemetryTests(unittest.TestCase):
    def test_records_block_processing_latency_in_dashboard_metrics(self) -> None:
        ticks = iter((10.0, 10.125))
        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=lambda: next(ticks))

        telemetry.begin_block("signature-1")
        latency = telemetry.record_block_processed("signature-1")

        self.assertEqual(latency, 125.0)
        self.assertEqual(telemetry.active_boundaries, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 1)
        self.assertEqual(
            snapshot["telemetry"]["block_processing_latency_ms"]["last"],
            125.0,
        )
        self.assertEqual(
            snapshot["telemetry"]["latency_display_decimal_places"],
            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
        )
        self.assertEqual(
            snapshot["streaming"]["block_processing_latency_ms"],
            125.0,
        )
        self.assertFalse(
            snapshot["telemetry"]["block_processing_latency_ms"][
                "last_exceeds_threshold"
            ]
        )
        self.assertFalse(
            snapshot["telemetry"]["block_processing_latency_ms"][
                "recent_exceeds_threshold"
            ]
        )

    def test_explicit_timestamps_do_not_consult_clock_and_update_dashboard(self) -> None:
        def unexpected_clock_read() -> float:
            self.fail("explicit timestamps should not consult the clock")

        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=unexpected_clock_read)

        latency = telemetry.record_block_processed(
            "signature-explicit",
            started_at=20.0,
            completed_at=20.075,
        )

        self.assertAlmostEqual(latency, 75.0)
        self.assertEqual(telemetry.active_boundaries, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 1)
        self.assertEqual(
            snapshot["telemetry"]["block_processing_latency_ms"],
            {
                "last": 75.0,
                "average": 75.0,
                "min": 75.0,
                "max": 75.0,
                "sample_count": 1,
                "warning_threshold_ms": BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS,
                "last_exceeds_threshold": False,
                "recent_exceeds_threshold": False,
            },
        )
        self.assertEqual(
            snapshot["telemetry"]["latency_display_decimal_places"],
            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
        )
        self.assertEqual(
            snapshot["streaming"]["block_processing_latency_ms"],
            75.0,
        )

    def test_missing_start_boundary_samples_one_and_updates_dashboard(self) -> None:
        ticks = iter((30.0, 30.05))
        clock_reads: list[float] = []

        def clock() -> float:
            timestamp = next(ticks)
            clock_reads.append(timestamp)
            return timestamp

        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=clock)

        latency = telemetry.record_block_processed("signature-without-start")

        self.assertAlmostEqual(latency, 50.0)
        self.assertEqual(clock_reads, [30.0, 30.05])
        self.assertEqual(len(clock_reads), 2)
        self.assertEqual(telemetry.active_boundaries, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 1)
        self.assertEqual(
            snapshot["telemetry"]["block_processing_latency_ms"]["last"],
            50.0,
        )
        self.assertEqual(
            snapshot["streaming"]["block_processing_latency_ms"],
            50.0,
        )

    def test_missing_start_boundary_uses_explicit_completion_without_extra_clock_read(self) -> None:
        clock_reads: list[float] = []

        def clock() -> float:
            clock_reads.append(30.0)
            return 30.0

        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=clock)

        latency = telemetry.record_block_processed(
            "signature-without-start-explicit-completion",
            completed_at=30.05,
        )

        self.assertAlmostEqual(latency, 50.0)
        self.assertEqual(clock_reads, [30.0])
        self.assertEqual(len(clock_reads), 1)
        self.assertEqual(telemetry.active_boundaries, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 1)
        self.assertEqual(
            snapshot["telemetry"]["block_processing_latency_ms"]["last"],
            50.0,
        )
        self.assertEqual(
            snapshot["streaming"]["block_processing_latency_ms"],
            50.0,
        )

    def test_repeated_missing_start_boundaries_do_not_retain_timing_state(self) -> None:
        ticks = iter(
            (
                30.0,
                30.05,
                40.0,
                40.05,
                50.0,
                50.05,
            )
        )
        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=lambda: next(ticks))

        for signature in (
            "signature-without-start-1",
            "signature-without-start-2",
            "signature-without-start-3",
        ):
            latency = telemetry.record_block_processed(signature)

            self.assertAlmostEqual(latency, 50.0)
            self.assertEqual(telemetry.active_boundaries, 0)

        self.assertEqual(telemetry._started_blocks, {})

    def test_backward_clock_reading_records_zero_latency_in_both_snapshots(self) -> None:
        ticks = iter((40.0, 39.875))
        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics, clock=lambda: next(ticks))

        telemetry.begin_block("signature-backward-clock")
        latency = telemetry.record_block_processed("signature-backward-clock")

        self.assertEqual(latency, 0.0)
        snapshot = metrics.snapshot()
        self.assertEqual(
            snapshot["telemetry"]["block_processing_latency_ms"],
            {
                "last": 0.0,
                "average": 0.0,
                "min": 0.0,
                "max": 0.0,
                "sample_count": 1,
                "warning_threshold_ms": BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS,
                "last_exceeds_threshold": False,
                "recent_exceeds_threshold": False,
            },
        )
        self.assertEqual(
            snapshot["streaming"]["block_processing_latency_ms"],
            0.0,
        )

    def test_latency_warning_identifies_above_threshold_samples(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            block_processing_latency_warning_threshold_ms=100.0,
        )
        telemetry = TllamaStreamTelemetry(metrics)

        telemetry.record_block_processing_latency("signature-at-threshold", 100.0)
        at_threshold = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertFalse(at_threshold["last_exceeds_threshold"])
        self.assertFalse(at_threshold["recent_exceeds_threshold"])

        telemetry.record_block_processing_latency("signature-below", 99.999)
        below = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertFalse(below["last_exceeds_threshold"])
        self.assertFalse(below["recent_exceeds_threshold"])

        telemetry.record_block_processing_latency("signature-above", 100.001)
        above = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertTrue(above["last_exceeds_threshold"])
        self.assertTrue(above["recent_exceeds_threshold"])
        self.assertEqual(above["warning_threshold_ms"], 100.0)

    def test_sub_millisecond_latency_warning_retains_boundary_precision(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            block_processing_latency_warning_threshold_ms=0.75,
        )
        telemetry = TllamaStreamTelemetry(metrics)

        telemetry.record_block_processing_latency("signature-sub-millisecond", 0.7501)

        latency = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertEqual(latency["last"], 0.7501)
        self.assertEqual(latency["average"], 0.7501)
        self.assertEqual(latency["warning_threshold_ms"], 0.75)
        self.assertTrue(latency["last_exceeds_threshold"])
        self.assertTrue(latency["recent_exceeds_threshold"])

    def test_latency_warning_below_supported_precision_is_explicitly_rounded(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            block_processing_latency_warning_threshold_ms=0.75,
        )
        telemetry = TllamaStreamTelemetry(metrics)

        telemetry.record_block_processing_latency(
            "signature-below-microsecond-boundary",
            0.7500001,
        )

        latency = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertEqual(latency["last"], 0.75)
        self.assertEqual(latency["average"], 0.75)
        self.assertEqual(latency["warning_threshold_ms"], 0.75)
        self.assertTrue(latency["last_exceeds_threshold"])
        self.assertTrue(latency["recent_exceeds_threshold"])

    def test_recent_latency_warning_remains_after_current_sample_recovers(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            block_processing_latency_warning_threshold_ms=100.0,
        )
        telemetry = TllamaStreamTelemetry(metrics)

        telemetry.record_block_processing_latency("signature-slow", 150.0)
        telemetry.record_block_processing_latency("signature-recovered", 50.0)

        latency = metrics.snapshot()["telemetry"]["block_processing_latency_ms"]
        self.assertFalse(latency["last_exceeds_threshold"])
        self.assertTrue(latency["recent_exceeds_threshold"])

    def test_non_finite_completion_readings_do_not_count_as_processed(self) -> None:
        for completion in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(completion=completion):
                metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
                telemetry = TllamaStreamTelemetry(metrics)

                with self.assertRaises(ValueError):
                    telemetry.record_block_processed(
                        "signature-invalid-clock",
                        started_at=40.0,
                        completed_at=completion,
                    )

                snapshot = metrics.snapshot()
                self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
                self.assertEqual(
                    snapshot["telemetry"]["block_processing_latency_ms"]["sample_count"],
                    0,
                )
                self.assertEqual(snapshot["streaming"]["block_processing_latency_ms"], 0.0)

    def test_non_finite_clock_start_readings_are_rejected_without_boundaries(self) -> None:
        for start in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(start=start):
                metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
                telemetry = TllamaStreamTelemetry(metrics, clock=lambda: start)

                with self.assertRaisesRegex(ValueError, "start timestamp must be finite"):
                    telemetry.begin_block("signature-invalid-clock-start")

                self.assertEqual(telemetry.active_boundaries, 0)
                snapshot = metrics.snapshot()
                self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
                self.assertEqual(
                    snapshot["telemetry"]["block_processing_latency_ms"]["sample_count"],
                    0,
                )
                self.assertEqual(snapshot["streaming"]["block_processing_latency_ms"], 0.0)

    def test_non_finite_fallback_clock_start_is_rejected_before_completion_read(self) -> None:
        for start in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(start=start):
                clock_reads: list[float] = []

                def clock() -> float:
                    clock_reads.append(start)
                    return start

                metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
                telemetry = TllamaStreamTelemetry(metrics, clock=clock)

                with self.assertRaisesRegex(ValueError, "start timestamp must be finite"):
                    telemetry.record_block_processed("signature-invalid-fallback-start")

                self.assertEqual(clock_reads, [start])
                self.assertEqual(telemetry.active_boundaries, 0)
                snapshot = metrics.snapshot()
                self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
                self.assertEqual(
                    snapshot["telemetry"]["block_processing_latency_ms"]["sample_count"],
                    0,
                )
                self.assertEqual(snapshot["streaming"]["block_processing_latency_ms"], 0.0)

    def test_non_finite_explicit_start_timestamps_are_rejected_without_samples(self) -> None:
        def unexpected_clock_read() -> float:
            raise AssertionError("explicit timestamps should not consult the clock")

        for start in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(start=start):
                metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
                telemetry = TllamaStreamTelemetry(metrics, clock=unexpected_clock_read)

                with self.assertRaisesRegex(ValueError, "start timestamp must be finite"):
                    telemetry.record_block_processed(
                        "signature-invalid-explicit-start",
                        started_at=start,
                        completed_at=40.0,
                    )

                self.assertEqual(telemetry.active_boundaries, 0)
                snapshot = metrics.snapshot()
                self.assertEqual(snapshot["telemetry"]["blocks_processed"], 0)
                self.assertEqual(
                    snapshot["telemetry"]["block_processing_latency_ms"]["sample_count"],
                    0,
                )
                self.assertEqual(snapshot["streaming"]["block_processing_latency_ms"], 0.0)

    def test_metrics_reject_non_finite_latency_samples_without_mutating_snapshot(self) -> None:
        invalid_samples = (float("nan"), float("inf"), float("-inf"))
        recording_paths = (
            "metrics",
            "telemetry",
        )

        for path in recording_paths:
            with self.subTest(path=path):
                metrics = DashboardMetrics(
                    queue_capacity=10,
                    signature_history_capacity=20,
                    block_processing_latency_warning_threshold_ms=100.0,
                )
                telemetry = TllamaStreamTelemetry(metrics)
                metrics.record_block_processing("signature-valid-slow", 100.001)
                before = metrics.snapshot()

                for invalid_sample in invalid_samples:
                    with self.subTest(sample=invalid_sample):
                        with self.assertRaisesRegex(
                            ValueError,
                            "finite and non-negative",
                        ):
                            if path == "metrics":
                                metrics.record_block_processing(
                                    "signature-invalid",
                                    invalid_sample,
                                )
                            else:
                                telemetry.record_block_processing_latency(
                                    "signature-invalid",
                                    invalid_sample,
                                )

                after = metrics.snapshot()
                self.assertEqual(after["telemetry"], before["telemetry"])
                self.assertEqual(after["streaming"], before["streaming"])
                self.assertEqual(after["webhook_logs"], before["webhook_logs"])

    def test_manual_latency_rejects_invalid_values(self) -> None:
        metrics = DashboardMetrics(queue_capacity=10, signature_history_capacity=20)
        telemetry = TllamaStreamTelemetry(metrics)

        with self.assertRaises(ValueError):
            telemetry.record_block_processing_latency("signature-1", -1)
        for invalid_sample in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(sample=invalid_sample):
                with self.assertRaises(ValueError):
                    telemetry.record_block_processing_latency(
                        "signature-1",
                        invalid_sample,
                    )

    def test_discarded_boundaries_are_distinct_bounded_diagnostics(self) -> None:
        metrics = DashboardMetrics(
            queue_capacity=10,
            signature_history_capacity=20,
            event_log_limit=2,
        )
        telemetry = TllamaStreamTelemetry(metrics)

        telemetry.begin_block("signature-missing")
        telemetry.discard_block("signature-missing")
        telemetry.discard_block("signature-without-boundary")
        telemetry.begin_block("signature-processed")
        telemetry.record_block_processed(
            "signature-processed",
            started_at=20.0,
            completed_at=20.001,
        )

        self.assertEqual(telemetry.active_boundaries, 0)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["telemetry"]["blocks_processed"], 1)
        self.assertEqual(snapshot["telemetry"]["blocks_discarded"], 1)
        self.assertEqual(snapshot["streaming"]["blocks_discarded"], 1)
        discard_logs = [
            entry
            for entry in snapshot["webhook_logs"]
            if entry["status"] == "block_discarded"
        ]
        self.assertEqual(len(discard_logs), 1)
        self.assertEqual(discard_logs[0]["signature"], "signature-missing")
        self.assertNotIn("payload", discard_logs[0])
        self.assertLessEqual(len(snapshot["webhook_logs"]), 2)


if __name__ == "__main__":
    unittest.main()
