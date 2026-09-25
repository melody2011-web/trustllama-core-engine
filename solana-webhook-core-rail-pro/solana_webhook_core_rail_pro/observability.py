"""Thread-safe operational metrics shared by the engine and local dashboard."""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone
from threading import RLock
from typing import TYPE_CHECKING, Any

from .retry import (
    MAX_RETRY_DELAY_SECONDS,
    RETRY_DELAY_WARNING_THRESHOLD,
    max_retry_delay_seconds,
    retry_schedule,
)
from .state import MAX_RECOVERY_BOUNDARIES

if TYPE_CHECKING:
    from .state import DurableState


MAX_RESOLVED_RECOVERY_BOUNDARIES = 1000
BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS = 1000.0
BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES = 6
DASHBOARD_SNAPSHOT_REVISION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DashboardMetrics:
    """Bounded metrics store safe to read from an HTTP server thread."""

    def __init__(
        self,
        *,
        queue_capacity: int,
        signature_history_capacity: int,
        event_log_limit: int = 200,
        recovery_boundary_limit: int = MAX_RECOVERY_BOUNDARIES,
        resolved_recovery_boundary_limit: int = MAX_RESOLVED_RECOVERY_BOUNDARIES,
        retry_max_attempts: int = 5,
        retry_base_backoff_seconds: float = 2.0,
        block_processing_latency_warning_threshold_ms: float = (
            BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS
        ),
    ) -> None:
        if recovery_boundary_limit < 1:
            raise ValueError("recovery_boundary_limit must be at least 1")
        if resolved_recovery_boundary_limit < 1:
            raise ValueError("resolved_recovery_boundary_limit must be at least 1")
        if (
            not math.isfinite(block_processing_latency_warning_threshold_ms)
            or block_processing_latency_warning_threshold_ms <= 0
        ):
            raise ValueError(
                "block_processing_latency_warning_threshold_ms must be finite and positive"
            )
        self._lock = RLock()
        self._started_at = time.time()
        self._queue_capacity = queue_capacity
        self._signature_history_capacity = signature_history_capacity
        self._event_log_limit = event_log_limit
        self._queue_depth = 0
        self._active_delivery = 0
        self._parsed_signatures = 0
        self._delivery_attempts = 0
        self._delivery_successes = 0
        self._failure_drops = 0
        self._blocks_processed = 0
        self._blocks_discarded = 0
        self._block_latency_total_ms = 0.0
        self._block_latency_samples: deque[float] = deque(maxlen=event_log_limit)
        self._last_block_latency_ms: float | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=event_log_limit)
        self._active_retries: dict[str, dict[str, Any]] = {}
        self._recovery_boundary_misses = 0
        self._recovery_boundary_limit = recovery_boundary_limit
        self._recovery_boundary_trimmed_total = 0
        self._pruned_boundaries: dict[str, dict[str, Any]] = {}
        self._resolved_pruned_boundaries: deque[dict[str, Any]] = deque(
            maxlen=resolved_recovery_boundary_limit
        )
        self._resolved_recovery_boundary_limit = resolved_recovery_boundary_limit
        self._evicted_boundaries: list[dict[str, Any]] = []
        self._retry_max_attempts = retry_max_attempts
        self._retry_base_backoff_seconds = retry_base_backoff_seconds
        self._block_processing_latency_warning_threshold_ms = (
            block_processing_latency_warning_threshold_ms
        )
        self._retry_schedule = retry_schedule(
            retry_base_backoff_seconds,
            retry_max_attempts,
        )

    def record_parsed(self, signature: str) -> None:
        with self._lock:
            self._parsed_signatures += 1
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "parsed",
                    "signature": signature,
                    "status": "parsed",
                }
            )

    def record_queue_depth(self, depth: int) -> None:
        with self._lock:
            self._queue_depth = max(0, min(depth, self._queue_capacity))

    def record_block_processing(self, signature: str, latency_ms: float) -> None:
        """Record bounded latency telemetry, rejecting invalid samples before mutation."""
        if not math.isfinite(latency_ms) or latency_ms < 0:
            raise ValueError("block processing latency must be finite and non-negative")
        with self._lock:
            self._blocks_processed += 1
            self._block_latency_total_ms += latency_ms
            self._block_latency_samples.append(latency_ms)
            self._last_block_latency_ms = latency_ms
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "telemetry",
                    "signature": signature,
                    "status": "block_processed",
                    "latency_ms": round(
                        latency_ms,
                        BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                    ),
                }
            )

    def record_block_discarded(self, signature: str) -> None:
        """Record a bounded diagnostic for discarded transaction timing work."""
        with self._lock:
            self._blocks_discarded += 1
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "telemetry",
                    "signature": signature,
                    "status": "block_discarded",
                }
            )

    def record_delivery_started(self, signature: str, event_name: str) -> None:
        with self._lock:
            self._active_delivery += 1
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "outbound",
                    "signature": signature,
                    "event": event_name,
                    "status": "in_flight",
                }
            )

    def record_delivery_attempt(
        self,
        *,
        signature: str,
        event_name: str,
        attempt: int,
        status: int | None,
        error: str | None,
    ) -> None:
        with self._lock:
            self._delivery_attempts += 1
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "outbound",
                    "signature": signature,
                    "event": event_name,
                    "attempt": attempt,
                    "status": status,
                    "status_label": "ok" if status == 200 else "retrying",
                    "error": error,
                }
            )

    def record_retry(
        self,
        *,
        signature: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: float,
        error: str | None,
    ) -> None:
        with self._lock:
            self._active_retries[signature] = {
                "signature": signature,
                "attempt": attempt,
                "next_attempt": attempt + 1,
                "max_attempts": max_attempts,
                "retry_at_epoch": time.time() + delay_seconds,
                "delay_seconds": delay_seconds,
                "error": error,
            }

    def record_delivery_result(
        self,
        *,
        signature: str,
        event_name: str,
        attempts: int,
        status: int | None,
        success: bool,
        dropped: bool,
        error: str | None,
    ) -> None:
        with self._lock:
            self._active_retries.pop(signature, None)
            if success:
                self._delivery_successes += 1
            if dropped:
                self._failure_drops += 1
            self._logs.append(
                {
                    "timestamp": _utc_now(),
                    "kind": "terminal",
                    "signature": signature,
                    "event": event_name,
                    "attempts": attempts,
                    "status": status,
                    "status_label": "delivered" if success else "dead_letter",
                    "error": error,
                }
            )

    def record_delivery_finished(self) -> None:
        with self._lock:
            self._active_delivery = max(0, self._active_delivery - 1)

    def restore_recovery_state(
        self,
        boundaries: list[dict[str, Any]],
        missing_total: int,
        resolved_boundaries: list[dict[str, Any]] | None = None,
        *,
        active_evidence_limit: int = MAX_RECOVERY_BOUNDARIES,
        trimmed_total: int = 0,
        evictions: list[dict[str, Any]] | None = None,
    ) -> None:
        """Load durable recovery evidence into the in-process dashboard view."""
        if missing_total < 0:
            raise ValueError("missing_total must be non-negative")
        if active_evidence_limit < 1:
            raise ValueError("active_evidence_limit must be at least 1")
        if trimmed_total < 0:
            raise ValueError("trimmed_total must be non-negative")
        restored_evictions = evictions or []
        with self._lock:
            self._recovery_boundary_misses = missing_total
            self._recovery_boundary_limit = active_evidence_limit
            self._recovery_boundary_trimmed_total = trimmed_total
            self._evicted_boundaries = [
                {
                    "address": str(eviction["address"]),
                    "cursor": str(eviction["cursor"]),
                    "evicted_at": str(eviction["evicted_at"]),
                }
                for eviction in restored_evictions[:active_evidence_limit]
            ]
            self._pruned_boundaries = {
                str(boundary["address"]): {
                    "address": str(boundary["address"]),
                    "cursor": str(boundary["cursor"]),
                    "status": "pruned",
                    "detected_at": str(boundary["detected_at"]),
                    "last_detected_at": str(boundary["last_detected_at"]),
                    "occurrences": int(boundary["occurrences"]),
                    "newly_detected": False,
                    "incident_status": "known_unresolved",
                }
                for boundary in boundaries
            }
            self._resolved_pruned_boundaries.clear()
            for boundary in reversed(resolved_boundaries or []):
                self._resolved_pruned_boundaries.appendleft(
                    self._format_resolved_boundary(boundary)
                )

    def record_recovery_boundary_missing(
        self,
        address: str,
        cursor: str,
        state: DurableState | None = None,
    ) -> None:
        """Record that an address's durable cursor was pruned from RPC history."""
        if state is not None:
            boundary, newly_detected = state.record_recovery_boundary_missing(
                address,
                cursor,
            )
            active_evidence_limit = state.max_recovery_boundaries
            trimmed_total = state.recovery_boundary_trimmed_total()
            evictions = state.list_recovery_boundary_evictions()
        else:
            boundary = None
            newly_detected = address not in self._pruned_boundaries
            active_evidence_limit = self._recovery_boundary_limit
            trimmed_total = self._recovery_boundary_trimmed_total
            evictions = None
        with self._lock:
            self._recovery_boundary_limit = active_evidence_limit
            self._recovery_boundary_trimmed_total = trimmed_total
            if evictions is not None:
                self._evicted_boundaries = [
                    dict(eviction)
                    for eviction in evictions[:active_evidence_limit]
                ]
            detected_at = _utc_now()
            self._recovery_boundary_misses += 1
            existing = self._pruned_boundaries.get(address)
            if boundary is None:
                boundary = {
                    "address": address,
                    "cursor": cursor,
                    "detected_at": existing["detected_at"] if existing else detected_at,
                    "last_detected_at": detected_at,
                    "occurrences": (existing["occurrences"] if existing else 0) + 1,
                }
            self._pruned_boundaries[address] = {
                "address": address,
                "cursor": boundary["cursor"],
                "status": "pruned",
                "detected_at": boundary["detected_at"],
                "last_detected_at": boundary["last_detected_at"],
                "occurrences": boundary["occurrences"],
                "newly_detected": newly_detected,
                "incident_status": (
                    "new" if newly_detected else "known_unresolved"
                ),
            }
            if state is None and len(self._pruned_boundaries) > self._recovery_boundary_limit:
                oldest_address = min(
                    self._pruned_boundaries,
                    key=lambda item: (
                        self._pruned_boundaries[item]["last_detected_at"],
                        item,
                    ),
                )
                evicted_boundary = self._pruned_boundaries[oldest_address]
                self._pruned_boundaries.pop(oldest_address)
                self._recovery_boundary_trimmed_total += 1
                self._evicted_boundaries.insert(
                    0,
                    {
                        "address": evicted_boundary["address"],
                        "cursor": evicted_boundary["cursor"],
                        "evicted_at": detected_at,
                    },
                )
                del self._evicted_boundaries[active_evidence_limit:]

    def record_recovery_boundary_found(
        self,
        address: str,
        state: DurableState | None = None,
    ) -> None:
        """Clear an active pruning alert after RPC returns the durable cursor."""
        if state is not None:
            resolved_boundary = state.clear_recovery_boundary(address)
        else:
            resolved_boundary = None
        with self._lock:
            active_boundary = self._pruned_boundaries.pop(address, None)
            if resolved_boundary is None and active_boundary is not None:
                resolved_boundary = {
                    **active_boundary,
                    "resolved_at": _utc_now(),
                }
            if resolved_boundary is not None:
                self._resolved_pruned_boundaries.appendleft(
                    self._format_resolved_boundary(resolved_boundary)
                )

    @staticmethod
    def _format_resolved_boundary(boundary: dict[str, Any]) -> dict[str, Any]:
        return {
            "address": str(boundary["address"]),
            "cursor": str(boundary["cursor"]),
            "status": "resolved",
            "detected_at": str(boundary["detected_at"]),
            "last_detected_at": str(boundary["last_detected_at"]),
            "resolved_at": str(boundary["resolved_at"]),
            "occurrences": int(boundary["occurrences"]),
            "incident_status": "resolved",
        }

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-ready point-in-time snapshot for the dashboard."""
        with self._lock:
            now = time.time()
            terminal_deliveries = self._delivery_successes + self._failure_drops
            success_rate = (
                round(self._delivery_successes / terminal_deliveries * 100, 1)
                if terminal_deliveries
                else 0.0
            )
            average_block_latency_ms = (
                self._block_latency_total_ms / self._blocks_processed
                if self._blocks_processed
                else 0.0
            )
            last_block_latency_exceeds_threshold = (
                self._last_block_latency_ms is not None
                and self._last_block_latency_ms
                > self._block_processing_latency_warning_threshold_ms
            )
            recent_block_latency_exceeds_threshold = any(
                sample > self._block_processing_latency_warning_threshold_ms
                for sample in self._block_latency_samples
            )
            active_retries = []
            for retry in self._active_retries.values():
                retry_copy = dict(retry)
                retry_copy["retry_in_seconds"] = round(
                    max(0.0, retry["retry_at_epoch"] - now),
                    1,
                )
                active_retries.append(retry_copy)

            configured_max_delay = max_retry_delay_seconds(
                self._retry_base_backoff_seconds,
                self._retry_max_attempts,
            )
            warning_threshold = (
                MAX_RETRY_DELAY_SECONDS * RETRY_DELAY_WARNING_THRESHOLD
            )
            return {
                "snapshot_revision": DASHBOARD_SNAPSHOT_REVISION,
                "generated_at": _utc_now(),
                "uptime_seconds": round(now - self._started_at, 1),
                "streaming": {
                    "parsed_signatures": self._parsed_signatures,
                    "blocks_processed": self._blocks_processed,
                    "blocks_discarded": self._blocks_discarded,
                    "block_processing_latency_ms": round(
                        self._last_block_latency_ms or 0.0,
                        BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                    ),
                    "active_events": self._queue_depth + self._active_delivery,
                    "queue_depth": self._queue_depth,
                    "queue_capacity": self._queue_capacity,
                    "active_delivery_workers": self._active_delivery,
                },
                "memory": {
                    "process_rss_mb": self._process_rss_mb(),
                    "queue_bound_events": self._queue_capacity,
                    "signature_history_bound": self._signature_history_capacity,
                    "tracked_event_bound": (
                        self._queue_capacity + self._signature_history_capacity
                    ),
                },
                "webhooks": {
                    "delivery_attempts": self._delivery_attempts,
                    "successful_deliveries": self._delivery_successes,
                    "failure_drops": self._failure_drops,
                    "success_rate_percent": success_rate,
                },
                "telemetry": {
                    "blocks_processed": self._blocks_processed,
                    "blocks_discarded": self._blocks_discarded,
                    "latency_display_decimal_places": (
                        BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES
                    ),
                    "block_processing_latency_ms": {
                        "last": round(
                            self._last_block_latency_ms or 0.0,
                            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                        ),
                        "average": round(
                            average_block_latency_ms,
                            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                        ),
                        "min": round(
                            min(self._block_latency_samples),
                            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                        )
                        if self._block_latency_samples
                        else 0.0,
                        "max": round(
                            max(self._block_latency_samples),
                            BLOCK_PROCESSING_LATENCY_DECIMAL_PLACES,
                        )
                        if self._block_latency_samples
                        else 0.0,
                        "sample_count": len(self._block_latency_samples),
                        "warning_threshold_ms": (
                            self._block_processing_latency_warning_threshold_ms
                        ),
                        "last_exceeds_threshold": (
                            last_block_latency_exceeds_threshold
                        ),
                        "recent_exceeds_threshold": (
                            recent_block_latency_exceeds_threshold
                        ),
                    },
                },
                "recovery": {
                    "boundary_missing_total": self._recovery_boundary_misses,
                    "active_pruned_boundaries": len(self._pruned_boundaries),
                    "active_evidence_limit": self._recovery_boundary_limit,
                    "active_evidence_trimmed": (
                        self._recovery_boundary_trimmed_total > 0
                    ),
                    "active_evidence_trimmed_total": (
                        self._recovery_boundary_trimmed_total
                    ),
                    "pruned_boundaries": [
                        dict(boundary)
                        for boundary in sorted(
                            self._pruned_boundaries.values(),
                            key=lambda item: item["address"],
                        )
                    ],
                    "resolved_pruned_boundaries": [
                        dict(boundary)
                        for boundary in self._resolved_pruned_boundaries
                    ],
                    "resolved_boundary_history_limit": (
                        self._resolved_recovery_boundary_limit
                    ),
                    "evicted_boundaries": [
                        dict(boundary) for boundary in self._evicted_boundaries
                    ],
                },
                "retry_monitor": sorted(
                    active_retries,
                    key=lambda item: item["retry_in_seconds"],
                ),
                "retry_policy": {
                    "max_attempts": self._retry_max_attempts,
                    "base_backoff_seconds": self._retry_base_backoff_seconds,
                    "configured_max_delay_seconds": configured_max_delay,
                    "max_delay_seconds": MAX_RETRY_DELAY_SECONDS,
                    "delay_warning_threshold_seconds": warning_threshold,
                    "delay_ceiling_warning": (
                        configured_max_delay >= warning_threshold
                    ),
                    "schedule_seconds": list(self._retry_schedule),
                },
                "dead_letter_queue": [
                    entry
                    for entry in reversed(self._logs)
                    if entry.get("status_label") == "dead_letter"
                ][: self._event_log_limit],
                "webhook_logs": list(reversed(self._logs)),
            }

    @staticmethod
    def _process_rss_mb() -> float | None:
        try:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # Linux reports KiB; macOS reports bytes.
            if rss > 10_000_000:
                return round(rss / 1024 / 1024, 1)
            return round(rss / 1024, 1)
        except (ImportError, OSError):
            return None
