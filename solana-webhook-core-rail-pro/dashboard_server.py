"""Concurrent local operations dashboard for Solana Webhook Core-Rail Pro.

The server is intentionally decoupled from the listening loop. Pass the
engine's ``DashboardMetrics`` instance to ``DashboardServer`` and run it in a
separate thread or process:

    dashboard = DashboardServer(engine.metrics)
    dashboard.start()
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from solana_webhook_core_rail_pro.logging_safety import (
    safe_exception_detail as _safe_exception_detail,
)

LOGGER = logging.getLogger(__name__)
PUBLIC_ROOT = Path(__file__).with_name("public").resolve()


class MetricsProvider(Protocol):
    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-ready dashboard snapshot."""


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Small read-only HTTP surface with inline HTML/CSS/JS."""

    server_version = "CoreRailDashboard/1.0"

    def __init__(
        self,
        request: Any,
        client_address: Any,
        server: ThreadingHTTPServer,
        *,
        metrics: MetricsProvider,
    ) -> None:
        self.metrics = metrics
        super().__init__(request, client_address, server)

    def do_GET(self) -> None:
        request_path = urlsplit(self.path).path
        if request_path == "/" or request_path == "/index.html":
            self._send_text(HTTPStatus.OK, "text/html; charset=utf-8", DASHBOARD_HTML)
        elif request_path == "/api/metrics":
            try:
                snapshot = self.metrics.snapshot()
            except Exception as error:
                LOGGER.warning(
                    "dashboard metrics snapshot failed error_type=%s detail=%s",
                    type(error).__name__,
                    _safe_exception_detail(error),
                )
                self._send_json(
                    {"error": "metrics_unavailable"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
            else:
                if not isinstance(snapshot, Mapping):
                    LOGGER.warning(
                        "dashboard metrics snapshot invalid: expected mapping, got %s",
                        type(snapshot).__name__,
                    )
                    self._send_json(
                        {"error": "metrics_invalid"},
                        HTTPStatus.BAD_GATEWAY,
                    )
                else:
                    try:
                        self._send_json(snapshot)
                    except (TypeError, ValueError, OverflowError) as error:
                        LOGGER.warning(
                            "dashboard metrics snapshot serialization failed: %s",
                            error,
                        )
                        self._send_json(
                            {"error": "metrics_invalid"},
                            HTTPStatus.BAD_GATEWAY,
                        )
        elif request_path == "/healthz":
            self._send_json({"status": "ok"})
        else:
            self._send_public_file(request_path)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.debug("dashboard %s - %s", self.address_string(), format % args)

    def _send_json(
        self,
        payload: Mapping[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        self._send_text(
            status,
            "application/json; charset=utf-8",
            json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ),
        )

    def _send_text(self, status: HTTPStatus, content_type: str, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_public_file(self, request_path: str) -> None:
        """Serve files from ``public`` without allowing path traversal."""
        relative_path = unquote(request_path).lstrip("/")
        candidate = (PUBLIC_ROOT / relative_path).resolve()
        try:
            candidate.relative_to(PUBLIC_ROOT)
        except ValueError:
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self._send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = (
            "application/zip"
            if candidate.suffix.lower() == ".zip"
            else "application/octet-stream"
        )
        self._send_bytes(HTTPStatus.OK, content_type, candidate.read_bytes())

    def _send_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


class DashboardServer:
    """ThreadingHTTPServer wrapper with clean start/stop lifecycle."""

    def __init__(
        self,
        metrics: MetricsProvider,
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
    ) -> None:
        self.metrics = metrics
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        return self._server.server_address

    def start(self) -> None:
        if self._server is not None:
            return
        handler = partial(DashboardRequestHandler, metrics=self.metrics)
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            name="core-rail-dashboard",
            daemon=True,
        )
        self._thread.start()
        LOGGER.info("Dashboard listening on http://%s:%s", *self._server.server_address)

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


def _demo_metrics() -> Any:
    from solana_webhook_core_rail_pro.observability import DashboardMetrics

    metrics = DashboardMetrics(queue_capacity=1000, signature_history_capacity=200)
    metrics.record_parsed("demo-signature")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Core-Rail local metrics dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    server = DashboardServer(_demo_metrics(), host=args.host, port=args.port)
    server.start()
    try:
        while True:
            input()
    except (EOFError, KeyboardInterrupt):
        server.stop()
