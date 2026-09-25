"""Reserved VM supervisor for the API and two isolated grid services."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from threading import Event
from dataclasses import dataclass
from typing import Sequence


logger = logging.getLogger("defi-bot.production")

API_COMMAND: tuple[str, ...] = (
    "node",
    "--enable-source-maps",
    "artifacts/api-server/dist/index.mjs",
)
GRID_BOT_1_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-u",
    "grid_bot_1.py",
)
GRID_BOT_2_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-u",
    "grid_bot_2.py",
)
LIVE_RUNNER_COMMAND = GRID_BOT_1_COMMAND


@dataclass
class Child:
    role: str
    command: tuple[str, ...]
    process: subprocess.Popen[object]
    restart_count: int = 0
    next_restart_at: float = 0.0
    exhausted: bool = False


CHILD_COMMANDS = (
    ("api", API_COMMAND),
    ("grid_bot_1", GRID_BOT_1_COMMAND),
    ("grid_bot_2", GRID_BOT_2_COMMAND),
)
MAX_CHILD_RESTARTS = 3


def _supervise_child(
    child: Child, environment: dict[str, str], *, now: float | None = None
) -> None:
    if child.exhausted:
        return
    exit_code = child.process.poll()
    if exit_code is None:
        return
    current = time.monotonic() if now is None else now
    if child.restart_count >= MAX_CHILD_RESTARTS:
        child.exhausted = True
        logger.error(
            "production_child_restart_exhausted role=%s exit_code=%s",
            child.role,
            exit_code,
        )
        return
    if not child.next_restart_at:
        child.next_restart_at = current + min(4, 2**child.restart_count)
        logger.warning(
            "production_child_restart_scheduled role=%s attempt=%d",
            child.role,
            child.restart_count + 1,
        )
        return
    if current < child.next_restart_at:
        return
    child.process = subprocess.Popen(child.command, env=environment)
    child.restart_count += 1
    child.next_restart_at = 0.0
    logger.warning(
        "production_child_restarted role=%s pid=%s attempt=%d",
        child.role,
        child.process.pid,
        child.restart_count,
    )


def production_environment() -> dict[str, str]:
    """Return the shared environment expected by both production children."""
    environment = os.environ.copy()
    environment.setdefault("PORT", "8080")
    environment.setdefault("NODE_ENV", "production")
    for namespace in ("TRADING_GRID_BOT_1", "TRADING_GRID_BOT_2"):
        environment.setdefault(f"{namespace}_ENABLE_LIVE_TRADING", "false")
        environment.setdefault(f"{namespace}_STRATEGY_MODE", "disabled")
    return environment


def _terminate_children(children: Sequence[subprocess.Popen[object]]) -> None:
    running = [child for child in children if child.poll() is None]
    for child in running:
        child.terminate()

    deadline = time.monotonic() + 10
    for child in running:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            logger.error("production_child_force_stopped pid=%s", child.pid)
            child.kill()
            child.wait()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop_event = Event()

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    environment = production_environment()
    children: list[Child] = []
    try:
        for role, command in CHILD_COMMANDS:
            child = subprocess.Popen(command, env=environment)
            children.append(Child(role, command, child))
            logger.info(
                "production_child_started role=%s pid=%s command=%s",
                role,
                child.pid,
                " ".join(command),
            )
        logger.info(
            "production_service_started port=%s live_trading=%s",
            environment.get("PORT"),
            environment.get("ENABLE_LIVE_TRADING", "false"),
        )

        while not stop_event.wait(0.5):
            for child in children:
                _supervise_child(child, environment)
        logger.info("production_service_stopping reason=signal")
        return 0
    except OSError:
        logger.exception("production_service_startup_failed")
        return 1
    finally:
        _terminate_children([child.process for child in children])
        logger.info("production_service_stopped")


if __name__ == "__main__":
    raise SystemExit(main())