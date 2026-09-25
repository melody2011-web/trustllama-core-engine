"""Low-overhead stream telemetry wired into the dashboard metrics store."""

from __future__ import annotations

import math
import time
from typing import Callable

from .observability import DashboardMetrics


class TllamaStreamTelemetry:
    """Record bounded Solana block-processing timings.

    The engine owns the stream lifecycle while this adapter owns only timing
    boundaries. All retained aggregates are written to ``DashboardMetrics``;
    no transaction payloads or unbounded per-signature history is retained.
    """

    def __init__(
        self,
        metrics: DashboardMetrics,
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._metrics = metrics
        self._clock = clock
        self._started_blocks: dict[str, float] = {}

    def begin_block(self, signature: str) -> float:
        """Start a timing boundary for one parsed Solana transaction."""
        started_at = self._read_start_timestamp()
        self._started_blocks[signature] = started_at
        return started_at

    def record_block_processed(
        self,
        signature: str,
        *,
        started_at: float | None = None,
        completed_at: float | None = None,
    ) -> float:
        """Record one block latency in milliseconds and return that latency.

        Explicit timestamps are used as-is. If no start timestamp is supplied
        or stored for ``signature``, the current clock value is used as the
        start boundary so the event still produces a bounded, non-negative
        dashboard sample.
        """
        if started_at is None:
            started = self._started_blocks.pop(signature, None)
            if started is None:
                started = self._read_start_timestamp()
        else:
            started = self._validate_start_timestamp(started_at)
        self._started_blocks.pop(signature, None)
        completed = self._clock() if completed_at is None else completed_at
        elapsed_ms = (completed - started) * 1000
        if not math.isfinite(elapsed_ms):
            raise ValueError("block processing latency must be finite")
        latency_ms = max(0.0, elapsed_ms)
        self._metrics.record_block_processing(signature, latency_ms)
        return latency_ms

    def record_block_processing_latency(
        self,
        signature: str,
        latency_ms: float,
    ) -> None:
        """Publish an already-measured latency for integrations with their own clock."""
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("block processing latency must be finite and non-negative")
        self._started_blocks.pop(signature, None)
        self._metrics.record_block_processing(signature, latency_ms)

    def discard_block(self, signature: str) -> None:
        """Discard and report a timing boundary when RPC returns no transaction or fails."""
        if self._started_blocks.pop(signature, None) is not None:
            self._metrics.record_block_discarded(signature)

    @property
    def active_boundaries(self) -> int:
        """Expose the bounded number of in-flight timing boundaries for diagnostics."""
        return len(self._started_blocks)

    def _read_start_timestamp(self) -> float:
        return self._validate_start_timestamp(self._clock())

    @staticmethod
    def _validate_start_timestamp(started_at: float) -> float:
        if not math.isfinite(started_at):
            raise ValueError("block processing start timestamp must be finite")
        return started_at
