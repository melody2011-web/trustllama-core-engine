"""Locked configuration for the dedicated, single-asset grid launchers."""
from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator

from .config import ConfigurationError, Settings

CONFIG_VERSION = 1
MAX_LINE_USDT = Decimal("50")
MAX_BUDGET_USDT = Decimal("300")
MAX_CONFIG_AGE_SECONDS = 30 * 24 * 60 * 60
ACTIVATION_VERSION = 1
MAX_ACTIVATION_AGE_SECONDS = 24 * 60 * 60


class IsolatedGridConfigError(ConfigurationError):
    pass


@dataclass(frozen=True)
class IsolatedGridProfile:
    name: str
    symbol: str
    environment_namespace: str
    control_port: int

    @property
    def root(self) -> Path:
        return Path.home() / ".local" / "state" / "bnb-defi-bot" / self.name

    @property
    def state(self) -> Path:
        return self.root / "state.json"

    @property
    def config(self) -> Path:
        return self.root / "config.json"

    @property
    def prices(self) -> Path:
        return self.root / "live-prices.json"

    @property
    def market_data(self) -> Path:
        return self.root / "market-data.json"

    @property
    def log(self) -> Path:
        return self.root / f"{self.name}.log"

    @property
    def activation(self) -> Path:
        return self.root / "operator-activation.json"


ISOLATED_PROFILES = {
    "grid_bot_1": IsolatedGridProfile("grid_bot_1", "BTC", "TRADING_GRID_BOT_1", 8008),
    "grid_bot_2": IsolatedGridProfile("grid_bot_2", "XRP", "TRADING_GRID_BOT_2", 9000),
    # These profiles remain available to the existing launchers.
    "grid_bot_4": IsolatedGridProfile("grid_bot_4", "WBNB", "TRADING_GRID_BOT_4", 8804),
    "grid_bot_5": IsolatedGridProfile("grid_bot_5", "ETH", "TRADING_GRID_BOT_5", 8805),
    "grid_bot_6": IsolatedGridProfile("grid_bot_6", "SOL", "TRADING_GRID_BOT_6", 8806),
}


def isolated_profile(name: str) -> IsolatedGridProfile:
    try:
        return ISOLATED_PROFILES[name]
    except KeyError as error:
        raise IsolatedGridConfigError(f"Unknown isolated grid profile {name!r}.") from error


class OperatorActivationStore:
    """Short-lived, non-secret proof that the authenticated panel approved a bot."""

    def __init__(self, path: str | Path, profile: str):
        self.path = Path(path).expanduser()
        self.profile = isolated_profile(profile).name

    def activate(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        if not math.isfinite(timestamp) or timestamp <= 0:
            raise IsolatedGridConfigError("Activation timestamp is invalid.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                {
                    "version": ACTIVATION_VERSION,
                    "profile": self.profile,
                    "approved_at": timestamp,
                },
                handle,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def revoke(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return

    def require_fresh(self, *, now: float | None = None) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IsolatedGridConfigError(
                "Fresh password-gated operator activation is required."
            ) from error
        current = time.time() if now is None else now
        approved_at = raw.get("approved_at") if isinstance(raw, dict) else None
        if (
            not isinstance(raw, dict)
            or raw.get("version") != ACTIVATION_VERSION
            or raw.get("profile") != self.profile
            or isinstance(approved_at, bool)
            or not isinstance(approved_at, (int, float))
            or not math.isfinite(float(approved_at))
            or approved_at > current + 5
            or current - approved_at > MAX_ACTIVATION_AGE_SECONDS
        ):
            raise IsolatedGridConfigError(
                "Fresh password-gated operator activation is required."
            )


class IsolatedGridConfig:
    """Per-bot config; malformed or unexpectedly missing files fail closed."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(self.lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _line(value: object) -> Decimal:
        try:
            line = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise IsolatedGridConfigError("Per-line USDT size is invalid.") from error
        if not line.is_finite() or line <= 0 or line > MAX_LINE_USDT:
            raise IsolatedGridConfigError("Per-line USDT size must be finite, positive, and no more than 50.")
        if line * 6 > MAX_BUDGET_USDT:
            raise IsolatedGridConfigError("Six grid lines may not exceed the 300 USDT budget.")
        return line

    def _validated(self, raw: object) -> Decimal:
        if not isinstance(raw, dict) or raw.get("version") != CONFIG_VERSION:
            raise IsolatedGridConfigError("Isolated grid configuration version is invalid.")
        timestamp = raw.get("updated_at")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0:
            raise IsolatedGridConfigError("Isolated grid configuration audit timestamp is invalid.")
        if time.time() - float(timestamp) > MAX_CONFIG_AGE_SECONDS:
            raise IsolatedGridConfigError("Isolated grid configuration is stale; save a new operator-approved size.")
        return self._line(raw.get("per_line_usdt"))

    def _write_unlocked(self, line: Decimal) -> None:
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump({"version": CONFIG_VERSION, "per_line_usdt": format(line, "f"), "updated_at": time.time()}, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def initialize_default(self) -> Decimal:
        with self._locked():
            if not self.path.exists():
                self._write_unlocked(Decimal("50"))
                return Decimal("50")
            try:
                return self._validated(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as error:
                raise IsolatedGridConfigError(
                    "Isolated grid configuration is unreadable; refusing to trade."
                ) from error

    def read(self) -> Decimal:
        with self._locked():
            if not self.path.exists():
                raise IsolatedGridConfigError("Isolated grid configuration is missing; refusing to trade.")
            try:
                return self._validated(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as error:
                raise IsolatedGridConfigError("Isolated grid configuration is unreadable; refusing to trade.") from error

    def save(self, per_line_usdt: object) -> Decimal:
        line = self._line(per_line_usdt)
        with self._locked():
            self._write_unlocked(line)
        return line


def isolated_settings(profile: str, config_path: str | Path) -> Settings:
    """Build settings only for an explicit launcher and its locked config."""
    isolated_profile(profile)
    for variable in ("STRATEGY_STATE_FILE", "LIVE_PRICE_HISTORY_FILE"):
        if os.getenv(variable, "").strip():
            raise IsolatedGridConfigError(
                f"{variable} overrides are not accepted by isolated profile {profile}."
            )
    settings = Settings.from_environment(isolated_grid_profile=profile)
    line = IsolatedGridConfig(config_path).initialize_default()
    return replace(settings, grid_order_usdt=line)