"""Command-line entry point for Solana Webhook Core-Rail Pro."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from typing import Any

from .config import Settings
from .engine import CoreRailEngine
from .models import replay_payload
from .observability import DashboardMetrics
from .rpc import SolanaRpcClient
from .state import DurableState, MAX_REPLAY_RECORDS
from .telemetry import TllamaStreamTelemetry
from .webhook import WebhookDelivery


# Replay delivery failures are returned as records in the successful JSON
# response. This code is reserved for selections rejected before delivery.
REPLAY_SELECTION_REJECTED_EXIT_CODE = 2


class ReplaySelectionRejected(ValueError):
    """A replay selection cannot be fulfilled from retained state."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll Solana RPC and deliver HMAC-signed transaction webhooks."
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Override LOG_LEVEL for this run.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=0,
        help="Serve the local operations dashboard on this port (0 disables it).",
    )
    parser.add_argument(
        "--dashboard-host",
        default="127.0.0.1",
        help="Dashboard bind host; defaults to localhost.",
    )
    commands = parser.add_subparsers(dest="command")
    replay = commands.add_parser(
        "replay-dead-letters",
        help="Retry selected retained webhook failures without deleting their evidence.",
    )
    replay.add_argument(
        "--id",
        dest="dead_letter_ids",
        action="append",
        type=int,
        required=True,
        help="Dead-letter record id to replay; repeat for multiple records.",
    )
    replay.add_argument(
        "--limit",
        type=int,
        default=MAX_REPLAY_RECORDS,
        help=f"Maximum selected records (default: {MAX_REPLAY_RECORDS}).",
    )
    replay.add_argument(
        "--force",
        action="store_true",
        help="Retry records that already have a successful replay recorded.",
    )
    replay.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate selection and print records without sending webhooks.",
    )
    return parser


async def _run(
    settings: Settings,
    *,
    dashboard_host: str,
    dashboard_port: int,
) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    metrics = DashboardMetrics(
        queue_capacity=settings.event_queue_size,
        signature_history_capacity=(
            settings.signature_limit
            * 4
            * max(1, len(settings.watch_addresses))
            + len(settings.watch_signatures)
        ),
        retry_max_attempts=settings.webhook_max_attempts,
        retry_base_backoff_seconds=settings.webhook_base_backoff_seconds,
        block_processing_latency_warning_threshold_ms=(
            settings.block_processing_latency_warning_threshold_ms
        ),
    )
    telemetry = TllamaStreamTelemetry(metrics)
    async with (
        SolanaRpcClient(
            settings.rpc_urls,
            commitment=settings.commitment,
            timeout_seconds=settings.webhook_timeout_seconds,
            max_concurrency=settings.max_rpc_concurrency,
        ) as rpc,
        WebhookDelivery(
            url=settings.webhook_url,
            secret=settings.webhook_secret,
            timeout_seconds=settings.webhook_timeout_seconds,
            max_attempts=settings.webhook_max_attempts,
            base_backoff_seconds=settings.webhook_base_backoff_seconds,
            metrics=metrics,
        ) as delivery,
    ):
        engine = CoreRailEngine(
            settings,
            rpc,
            delivery,
            metrics=metrics,
            telemetry=telemetry,
        )
        dashboard = None
        if dashboard_port:
            from dashboard_server import DashboardServer

            dashboard = DashboardServer(
                metrics,
                host=dashboard_host,
                port=dashboard_port,
            )
            dashboard.start()
        try:
            await engine.run(stop_event)
        finally:
            if dashboard is not None:
                dashboard.stop()


async def replay_dead_letters(
    settings: Settings,
    dead_letter_ids: list[int],
    *,
    limit: int = MAX_REPLAY_RECORDS,
    force: bool = False,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Replay a bounded selection and append an audit result for each record."""
    state = DurableState(
        settings.state_path,
        max_dead_letters=settings.dead_letter_max_entries,
        max_replay_audit_entries=settings.replay_audit_max_entries,
    )
    try:
        try:
            records = state.get_dead_letters(dead_letter_ids, limit=limit)
        except ValueError as exc:
            raise ReplaySelectionRejected(str(exc)) from exc
        requested_ids = set(dead_letter_ids)
        found_ids = {int(record["id"]) for record in records}
        missing_ids = sorted(requested_ids - found_ids)
        if missing_ids:
            raise ReplaySelectionRejected(
                "selected dead-letter record(s) are not retained: "
                + ", ".join(str(record_id) for record_id in missing_ids)
            )

        if dry_run:
            return [
                {
                    "dead_letter_id": record["id"],
                    "signature": record["signature"],
                    "settlement_route": replay_payload(record["payload"])[
                        "settlement_route"
                    ],
                    "settlement_route_status": record["settlement_route_status"],
                    "outcome": "dry-run",
                    "created_at": record["created_at"],
                }
                for record in records
            ]

        outcomes: list[dict[str, Any]] = []
        async with WebhookDelivery(
            url=settings.webhook_url,
            secret=settings.webhook_secret,
            timeout_seconds=settings.webhook_timeout_seconds,
            max_attempts=settings.webhook_max_attempts,
            base_backoff_seconds=settings.webhook_base_backoff_seconds,
        ) as delivery:
            for record in records:
                dead_letter_id = int(record["id"])
                summary: dict[str, Any] = {
                    "dead_letter_id": dead_letter_id,
                    "signature": record["signature"],
                    "settlement_route_status": record["settlement_route_status"],
                }
                if state.has_successful_replay(dead_letter_id) and not force:
                    replay_id = state.record_replay_outcome(
                        dead_letter_id,
                        outcome="skipped",
                        error="successful replay already recorded; use --force to retry",
                    )
                    summary.update(
                        {
                            "replay_id": replay_id,
                            "outcome": "skipped",
                            "error": "successful replay already recorded; use --force to retry",
                        }
                    )
                    outcomes.append(summary)
                    continue

                try:
                    result = await delivery.deliver(replay_payload(record["payload"]))
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    replay_id = state.record_replay_outcome(
                        dead_letter_id,
                        outcome="failed",
                        error=error,
                    )
                    summary.update(
                        {
                            "replay_id": replay_id,
                            "outcome": "failed",
                            "attempts": 0,
                            "status": None,
                            "error": error,
                        }
                    )
                else:
                    outcome = "delivered" if result.success else "failed"
                    replay_id = state.record_replay_outcome(
                        dead_letter_id,
                        outcome=outcome,
                        result=result,
                    )
                    summary.update(
                        {
                            "replay_id": replay_id,
                            "outcome": outcome,
                            "attempts": result.attempts,
                            "status": result.status,
                            "error": result.error,
                        }
                    )
                outcomes.append(summary)
        return outcomes
    finally:
        state.close()


def main() -> None:
    args = _parser().parse_args()
    try:
        settings = Settings.from_env(
            require_watch_sources=args.command != "replay-dead-letters"
        )
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    logging.basicConfig(
        level=getattr(logging, args.log_level or settings.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.command == "replay-dead-letters":
        try:
            outcomes = asyncio.run(
                replay_dead_letters(
                    settings,
                    args.dead_letter_ids,
                    limit=args.limit,
                    force=args.force,
                    dry_run=args.dry_run,
                )
            )
        except ReplaySelectionRejected as exc:
            print(f"Replay failed: {exc}", file=sys.stderr)
            raise SystemExit(REPLAY_SELECTION_REJECTED_EXIT_CODE) from exc
        except (OSError, ValueError) as exc:
            raise SystemExit(f"Replay failed: {exc}") from exc
        print(json.dumps({"records": outcomes}, separators=(",", ":")))
        return

    try:
        asyncio.run(
            _run(
                settings,
                dashboard_host=args.dashboard_host,
                dashboard_port=args.dashboard_port,
            )
        )
    except KeyboardInterrupt:
        pass
