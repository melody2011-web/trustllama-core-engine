"""Paused-by-default runtime wrapper for the two isolated dynamic grids."""

from __future__ import annotations

import json
import logging
import os
import signal
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from threading import Event, Lock, Thread
from typing import Any

from .isolated_grid import (
    IsolatedGridConfigError,
    IsolatedGridProfile,
    OperatorActivationStore,
    isolated_profile,
    isolated_settings,
)
from .live_runner import LivePriceStore, LiveTradingLoop
from .market_data import MarketDataFileProvider


class IsolatedServiceError(RuntimeError):
    pass


class ServiceStatus:
    def __init__(self, profile: IsolatedGridProfile):
        self._lock = Lock()
        self._value: dict[str, Any] = {
            "service": profile.name,
            "symbol": profile.symbol,
            "baseAsset": "USDT",
            "environmentNamespace": profile.environment_namespace,
            "state": "starting",
            "liveExecution": False,
            "controlPort": profile.control_port,
        }

    def update(self, **values: Any) -> None:
        with self._lock:
            self._value.update(values)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._value)


def _control_server(
    profile: IsolatedGridProfile, status: ServiceStatus
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path not in {"/health", "/control/status"}:
                self.send_error(404)
                return
            payload = json.dumps(status.snapshot(), sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    return ThreadingHTTPServer(("127.0.0.1", profile.control_port), Handler)


def _enabled(profile: IsolatedGridProfile) -> bool:
    global_gate = os.getenv("ENABLE_LIVE_TRADING", "false").strip().lower() == "true"
    bot_gate = (
        os.getenv(f"{profile.environment_namespace}_ENABLE_LIVE_TRADING", "false")
        .strip()
        .lower()
        == "true"
    )
    return global_gate and bot_gate


def _mode(profile: IsolatedGridProfile) -> str:
    return os.getenv(
        f"{profile.environment_namespace}_STRATEGY_MODE", "disabled"
    ).strip().lower()


def run_isolated_service(profile_name: str) -> int:
    profile = isolated_profile(profile_name)
    namespaced_port = os.getenv(f"{profile.environment_namespace}_CONTROL_PORT", "").strip()
    if namespaced_port and namespaced_port != str(profile.control_port):
        raise IsolatedServiceError("The isolated control port cannot be overridden.")
    if os.getenv("CONTROL_PORT", "").strip():
        raise IsolatedServiceError("Generic CONTROL_PORT overrides are forbidden.")

    profile.root.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(profile.log, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger = logging.getLogger(profile.name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    live_logger = logging.getLogger("defi-bot.live")
    live_logger.addHandler(handler)
    live_logger.setLevel(logging.INFO)
    live_logger.propagate = False

    stop_event = Event()
    signal.signal(signal.SIGINT, lambda *_args: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_args: stop_event.set())
    status = ServiceStatus(profile)
    server = _control_server(profile, status)
    server_thread = Thread(target=server.serve_forever, name=f"{profile.name}-control")
    server_thread.start()
    try:
        if not _enabled(profile) or _mode(profile) == "disabled":
            status.update(state="paused", liveExecution=False)
            logger.info("isolated_service_paused live_execution=false")
            stop_event.wait()
            return 0
        if _mode(profile) != "grid" or os.getenv(
            "LIVE_STRATEGY_MODE", "disabled"
        ).strip().lower() != "grid":
            raise IsolatedServiceError("Isolated services accept only disabled or grid mode.")
        activation = OperatorActivationStore(profile.activation, profile.name)
        activation.require_fresh()

        def authorize() -> None:
            if (
                not _enabled(profile)
                or _mode(profile) != "grid"
                or os.getenv("LIVE_STRATEGY_MODE", "disabled").strip().lower()
                != "grid"
            ):
                status.update(state="paused", liveExecution=False)
                raise IsolatedServiceError("Live execution gates are no longer enabled.")
            try:
                activation.require_fresh()
            except IsolatedGridConfigError:
                status.update(state="blocked", liveExecution=False)
                raise
            status.update(state="running", liveExecution=True)

        def load():
            settings = isolated_settings(profile.name, profile.config)
            if settings.grid_symbols != (profile.symbol,):
                raise IsolatedServiceError("Isolated symbol boundary is invalid.")
            return replace(
                settings,
                strategy_state_file=str(profile.state),
                live_price_history_file=str(profile.prices),
            )

        settings = load()
        status.update(state="running", liveExecution=True)
        LiveTradingLoop(
            settings,
            prices=LivePriceStore(str(profile.prices), (profile.symbol,)),
            settings_provider=load,
            market_data_provider=MarketDataFileProvider(
                profile.market_data,
                profile.symbol,
                rsi_period=settings.strategy_rsi_window,
            ),
            execution_authorizer=authorize,
            stop_event=stop_event,
        ).run()
        return 0
    except IsolatedGridConfigError:
        status.update(state="blocked", liveExecution=False)
        logger.exception("isolated_service_blocked")
        return 1
    except Exception:
        status.update(state="failed", liveExecution=False)
        logger.exception("isolated_service_failed")
        return 1
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        logger.info("isolated_service_stopped state_preserved=true")