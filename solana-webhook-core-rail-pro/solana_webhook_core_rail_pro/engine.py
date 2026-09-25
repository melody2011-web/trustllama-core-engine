"""Bounded-memory async Solana polling and webhook delivery engine."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .logging_safety import safe_exception_detail
from .models import TransactionEvent
from .observability import DashboardMetrics
from .rpc import SolanaRpcClient, SolanaRpcError
from .state import DurableState
from .telemetry import TllamaStreamTelemetry
from .webhook import DeliveryResult, WebhookDelivery

LOGGER = logging.getLogger(__name__)
MAX_RECOVERY_PAGES = 100

EventSink = Callable[[TransactionEvent], Awaitable[DeliveryResult]]


@dataclass(frozen=True, slots=True)
class _AddressPoll:
    cursor: str | None
    can_commit: bool


class CoreRailEngine:
    """Poll watched addresses/signatures and deliver normalized events."""

    def __init__(
        self,
        settings: Settings,
        rpc: SolanaRpcClient,
        delivery: WebhookDelivery,
        metrics: DashboardMetrics | None = None,
        state: DurableState | None = None,
        telemetry: TllamaStreamTelemetry | None = None,
    ) -> None:
        self._settings = settings
        self._rpc = rpc
        self._delivery = delivery
        self._state = state or DurableState(
            settings.state_path,
            max_dead_letters=settings.dead_letter_max_entries,
            max_replay_audit_entries=settings.replay_audit_max_entries,
            max_recovery_boundaries=settings.max_recovery_boundaries,
        )
        self._owns_state = state is None
        self.metrics = metrics or DashboardMetrics(
            queue_capacity=settings.event_queue_size,
            signature_history_capacity=(
                settings.signature_limit
                * 4
                * max(1, len(settings.watch_addresses))
                + len(settings.watch_signatures)
            ),
            recovery_boundary_limit=self._state.max_recovery_boundaries,
            retry_max_attempts=settings.webhook_max_attempts,
            retry_base_backoff_seconds=settings.webhook_base_backoff_seconds,
            block_processing_latency_warning_threshold_ms=(
                settings.block_processing_latency_warning_threshold_ms
            ),
        )
        self.telemetry = telemetry or TllamaStreamTelemetry(self.metrics)
        self._queue: asyncio.Queue[TransactionEvent] = asyncio.Queue(
            maxsize=settings.event_queue_size
        )
        self._address_polls: dict[str, _AddressPoll] = {}
        self._address_batch_signatures: dict[str, set[str]] = {}
        self._delivery_failures: set[tuple[str | None, str]] = set()
        self._processed_explicit: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._stop_event: asyncio.Event | None = None
        self.metrics.restore_recovery_state(
            self._state.list_recovery_boundaries(),
            self._state.recovery_boundary_missing_total(),
            self._state.list_resolved_recovery_boundaries(),
            active_evidence_limit=self._state.max_recovery_boundaries,
            trimmed_total=self._state.recovery_boundary_trimmed_total(),
            evictions=self._state.list_recovery_boundary_evictions(),
        )

    async def run(self, stop_event: asyncio.Event | None = None) -> None:
        """Run until the stop event is set, keeping polling and delivery decoupled."""
        self._stop_event = stop_event or asyncio.Event()
        self._start_workers()
        LOGGER.info(
            "Core-Rail engine started addresses=%s explicit_signatures=%s rpc_endpoints=%s",
            len(self._settings.watch_addresses),
            len(self._settings.watch_signatures),
            len(self._settings.rpc_urls),
        )
        try:
            while not self._stop_event.is_set():
                started_at = asyncio.get_running_loop().time()
                try:
                    await self.poll_once()
                except (SolanaRpcError, TimeoutError) as exc:
                    LOGGER.error(
                        "Solana poll failed; will retry next cycle "
                        "error_type=%s detail=%s",
                        type(exc).__name__,
                        safe_exception_detail(exc),
                    )
                elapsed = asyncio.get_running_loop().time() - started_at
                remaining = max(0.0, self._settings.poll_interval_seconds - elapsed)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=remaining)
                except TimeoutError:
                    pass
        finally:
            await self._shutdown_workers()
            if self._owns_state:
                self._state.close()
            LOGGER.info("Core-Rail engine stopped")

    async def poll_once(self) -> int:
        """Poll watched sources once and return the number of queued events."""
        started_workers_here = not self._workers
        if started_workers_here:
            self._start_workers()
        explicit_events: list[TransactionEvent] = []
        queued = 0
        try:
            if self._settings.watch_signatures:
                explicit_events = await self._fetch_explicit_signatures()
                for event in explicit_events:
                    await self._queue.put(event)
                    self.metrics.record_parsed(event.signature)
                    self.metrics.record_queue_depth(self._queue.qsize())
                    queued += 1

            address_results = await asyncio.gather(
                *(self._poll_address(address) for address in self._settings.watch_addresses)
            )
            for events in address_results:
                for event in events:
                    await self._queue.put(event)
                    self.metrics.record_parsed(event.signature)
                    self.metrics.record_queue_depth(self._queue.qsize())
                    queued += 1

            # Cursor writes happen only after the queue is drained. A process
            # crash before this point re-reads the same events instead of
            # advancing past an event that was never delivered.
            await self._queue.join()
            for event in explicit_events:
                if (None, event.signature) not in self._delivery_failures:
                    self._state.mark_explicit_signature(event.signature)
                    self._processed_explicit.add(event.signature)
                self._delivery_failures.discard((None, event.signature))
            for address, poll in self._address_polls.items():
                batch_signatures = self._address_batch_signatures.get(address, set())
                batch_failed = any(
                    (address, signature) in self._delivery_failures
                    for signature in batch_signatures
                )
                if poll.cursor is not None and poll.can_commit and not batch_failed:
                    self._state.set_cursor(address, poll.cursor)
                for signature in batch_signatures:
                    self._delivery_failures.discard((address, signature))
            self._address_polls.clear()
            self._address_batch_signatures.clear()
            if queued:
                LOGGER.info("Queued Solana transaction events count=%s", queued)
            return queued
        finally:
            if started_workers_here:
                await self._shutdown_workers()

    async def _fetch_explicit_signatures(self) -> list[TransactionEvent]:
        events: list[TransactionEvent] = []
        for signature in self._settings.watch_signatures:
            if signature in self._processed_explicit:
                continue
            if self._state.has_explicit_signature(signature):
                self._processed_explicit.add(signature)
                continue
            result = await self._get_transaction(signature)
            if result is None:
                continue
            events.append(
                TransactionEvent.from_rpc_result(
                    signature=signature,
                    source_address=None,
                    result=result,
                )
            )
        return events

    async def _poll_address(self, address: str) -> list[TransactionEvent]:
        cursor = self._state.get_cursor(address)
        records, scan_safe = await self._fetch_address_records(address, cursor)
        raw_signatures = [
            record["signature"]
            for record in records
            if self._record_signature(record) is not None
        ]
        if not raw_signatures:
            self._address_polls[address] = _AddressPoll(None, True)
            return []

        newest_signature = raw_signatures[0]
        if cursor is None:
            self._address_polls[address] = _AddressPoll(newest_signature, scan_safe)
            LOGGER.info(
                "Primed watched address address=%s signatures=%s",
                address,
                len(raw_signatures),
            )
            return []

        candidates = [
            signature
            for record in reversed(records)
            if (signature := self._record_signature(record)) is not None
            and signature != cursor
            and not record.get("err")
        ]
        candidates = list(dict.fromkeys(candidates))
        self._address_polls[address] = _AddressPoll(
            newest_signature if candidates else None,
            scan_safe,
        )
        self._address_batch_signatures[address] = set(candidates)

        results = await asyncio.gather(
            *(self._get_transaction(signature) for signature in candidates)
        )
        if any(result is None for result in results):
            self._address_polls[address] = _AddressPoll(None, False)
        events: list[TransactionEvent] = []
        for signature, result in zip(candidates, results, strict=True):
            if result is not None:
                events.append(
                    TransactionEvent.from_rpc_result(
                        signature=signature,
                        source_address=address,
                        result=result,
                    )
                )
        return events

    async def _fetch_address_records(
        self,
        address: str,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read pages newer than the durable cursor, including large gaps."""
        before: str | None = None
        records: list[dict[str, Any]] = []
        cursor_found = cursor is None
        scan_safe = True
        seen_markers: set[str] = set()
        for _ in range(MAX_RECOVERY_PAGES):
            kwargs: dict[str, Any] = {"limit": self._settings.signature_limit}
            if before is not None:
                kwargs["before"] = before
            if cursor is not None:
                kwargs["until"] = cursor
            page = await self._rpc.get_signatures_for_address(address, **kwargs)
            records.extend(page)
            page_signatures = [
                signature
                for record in page
                if (signature := self._record_signature(record)) is not None
            ]
            signatures = {signature for signature in page_signatures}
            if len(page_signatures) != len(page):
                scan_safe = False
                LOGGER.warning(
                    "RPC returned a signature page with malformed records "
                    "address=%s before=%s",
                    address,
                    before,
                )
            if cursor is None or cursor in signatures:
                cursor_found = True
                if cursor is not None:
                    cursor_index = next(
                        (
                            index
                            for index, record in enumerate(records)
                            if record.get("signature") == cursor
                        ),
                        None,
                    )
                    if cursor_index is not None:
                        records = records[: cursor_index + 1]
                break
            if len(page) < self._settings.signature_limit:
                LOGGER.warning(
                    "Durable cursor was not found in RPC history address=%s cursor=%s",
                    address,
                    cursor,
                )
                break
            last_signature = self._record_signature(page[-1])
            if (
                last_signature is None
                or last_signature == before
                or last_signature in seen_markers
            ):
                scan_safe = False
                LOGGER.warning(
                    "Stopping unsafe RPC signature scan address=%s before=%s marker=%s",
                    address,
                    before,
                    last_signature,
                )
                break
            seen_markers.add(last_signature)
            before = last_signature
        else:
            scan_safe = False
            LOGGER.warning(
                "Stopping RPC signature scan after page limit "
                "address=%s limit=%s",
                address,
                MAX_RECOVERY_PAGES,
            )
        if cursor is not None and records and not cursor_found:
            self.metrics.record_recovery_boundary_missing(
                address,
                cursor,
                self._state,
            )
            LOGGER.warning(
                "Resuming address without cursor boundary address=%s",
                address,
            )
        elif cursor is not None and records:
            self.metrics.record_recovery_boundary_found(address, self._state)
        return records, scan_safe

    async def _get_transaction(self, signature: str) -> dict[str, Any] | None:
        started_at = self.telemetry.begin_block(signature)
        try:
            result = await self._rpc.get_transaction(signature)
        except Exception:
            self.telemetry.discard_block(signature)
            raise
        if result is None:
            self.telemetry.discard_block(signature)
        else:
            self.telemetry.record_block_processed(
                signature,
                started_at=started_at,
            )
        return result

    @staticmethod
    def _record_signature(record: dict[str, Any]) -> str | None:
        signature = record.get("signature")
        if isinstance(signature, str) and signature:
            return signature
        return None

    async def _delivery_worker(self, index: int) -> None:
        while True:
            event = await self._queue.get()
            self.metrics.record_queue_depth(self._queue.qsize())
            self.metrics.record_delivery_started(
                event.signature,
                "solana.transaction",
            )
            try:
                payload = event.webhook_payload(
                    self._settings.network,
                    self._settings.settlement_route,
                )
                result = await self._delivery.deliver(payload)
                if result.dropped:
                    self._state.add_dead_letter(payload, result)
                    LOGGER.error(
                        "Event moved to dead-letter store after webhook retries "
                        "worker=%s signature=%s",
                        index,
                        event.signature,
                    )
            except Exception as exc:
                self._delivery_failures.add((event.source_address, event.signature))
                LOGGER.error(
                    "Unexpected webhook worker failure worker=%s signature=%s "
                    "error_type=%s detail=%s",
                    index,
                    event.signature,
                    type(exc).__name__,
                    safe_exception_detail(exc),
                )
            finally:
                self.metrics.record_delivery_finished()
                self._queue.task_done()

    async def _shutdown_workers(self) -> None:
        if not self._workers:
            return
        await self._queue.join()
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def _start_workers(self) -> None:
        self._workers = [
            asyncio.create_task(
                self._delivery_worker(index),
                name=f"webhook-worker-{index}",
            )
            for index in range(self._settings.webhook_max_concurrency)
        ]
