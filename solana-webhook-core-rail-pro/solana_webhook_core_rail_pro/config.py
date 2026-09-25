"""Environment-backed configuration with strict validation."""

from __future__ import annotations

import json
import math
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .observability import BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS
from .retry import validate_retry_schedule
from .state import MAX_REPLAY_AUDIT_ENTRIES, MAX_RECOVERY_BOUNDARIES

DEFAULT_RPC_URL = "https://api.mainnet-beta.solana.com"
DEFAULT_STAGING_DISTRIBUTION_PATH = (
    Path(__file__).resolve().parents[2] / "staging-webhook-distribution.toml"
)
SUPPORTED_PRODUCT_METADATA_ENTRY_TYPES = frozenset({"string", "number", "boolean"})


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _positive_float(name: str, value: str | None, default: float) -> float:
    try:
        result = float(value) if value is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _validate_positive_float(name: str, value: float) -> None:
    try:
        finite = math.isfinite(value)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not finite:
        raise ValueError(f"{name} must be a finite number")
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")


def _positive_int(name: str, value: str | None, default: int) -> int:
    try:
        result = int(value) if value is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return result


def _http_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return value


def _required_string(
    section: Mapping[str, object],
    name: str,
    *,
    setting_name: str,
) -> str:
    value = section.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{setting_name}.{name} must be a non-empty string")
    return value.strip()


def _validate_product_metadata_layout(
    document: object,
    *,
    setting_name: str,
    metadata_path: Path,
) -> None:
    field_prefix = f"{setting_name}.metadata_layout"
    if not isinstance(document, Mapping):
        raise ValueError(
            f"{field_prefix} must contain a JSON object: {metadata_path}"
        )

    if "entries" not in document:
        raise ValueError(
            f"{field_prefix}.entries is required: {metadata_path}"
        )
    entries = document["entries"]
    if not isinstance(entries, list):
        raise ValueError(
            f"{field_prefix}.entries must be an array: {metadata_path}"
        )

    for index, entry in enumerate(entries):
        entry_prefix = f"{field_prefix}.entries[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(
                f"{entry_prefix} must be an object: {metadata_path}"
            )

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"{entry_prefix}.name must be a non-empty string: {metadata_path}"
            )

        entry_type = entry.get("type")
        if not isinstance(entry_type, str) or not entry_type.strip():
            raise ValueError(
                f"{entry_prefix}.type must be a non-empty string: {metadata_path}"
            )
        if entry_type not in SUPPORTED_PRODUCT_METADATA_ENTRY_TYPES:
            supported_types = ", ".join(
                sorted(SUPPORTED_PRODUCT_METADATA_ENTRY_TYPES)
            )
            raise ValueError(
                f"{entry_prefix}.type has unsupported value {entry_type!r}; "
                f"expected one of {supported_types}: {metadata_path}"
            )


@dataclass(frozen=True, slots=True)
class StagingDistribution:
    """Validated public staging distribution metadata."""

    environment: str
    visibility: str
    enabled: bool
    url: str
    route: str
    metadata_layout: str
    metadata_layout_path: Path
    archive: str | None = None

    @classmethod
    def from_file(cls, path: str | os.PathLike[str]) -> "StagingDistribution":
        distribution_path = Path(path)
        setting_name = "STAGING_DISTRIBUTION"
        try:
            with distribution_path.open("rb") as stream:
                document = tomllib.load(stream)
        except FileNotFoundError as exc:
            raise ValueError(
                f"{setting_name}_PATH file does not exist: {distribution_path}"
            ) from exc
        except OSError as exc:
            raise ValueError(
                f"{setting_name}_PATH could not be read: {distribution_path}"
            ) from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(
                f"{setting_name}_PATH contains invalid TOML: {distribution_path}"
            ) from exc

        section = document.get("webhook_distribution")
        if not isinstance(section, Mapping):
            raise ValueError(
                f"{setting_name} must define a [webhook_distribution] table"
            )

        environment = _required_string(
            section,
            "environment",
            setting_name=setting_name,
        )
        if environment != "staging":
            raise ValueError(
                f"{setting_name}.environment must be 'staging', got {environment!r}"
            )

        visibility = _required_string(
            section,
            "visibility",
            setting_name=setting_name,
        )
        if visibility != "public":
            raise ValueError(
                f"{setting_name}.visibility must be 'public', got {visibility!r}"
            )

        enabled = section.get("enabled")
        if enabled is not True:
            raise ValueError(f"{setting_name}.enabled must be true")

        url = _required_string(section, "url", setting_name=setting_name)
        parsed_url = urlparse(url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.netloc
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            raise ValueError(
                f"{setting_name}.url must be an absolute HTTPS URL for public distribution"
            )

        route = _required_string(section, "route", setting_name=setting_name)
        metadata_layout = _required_string(
            section,
            "metadata_layout",
            setting_name=setting_name,
        )
        metadata_path = Path(metadata_layout)
        if not metadata_path.is_absolute():
            metadata_path = distribution_path.parent / metadata_path
        metadata_path = metadata_path.resolve()
        try:
            with metadata_path.open(encoding="utf-8") as stream:
                metadata_document = json.load(stream)
        except FileNotFoundError as exc:
            raise ValueError(
                f"{setting_name}.metadata_layout file does not exist: {metadata_path}"
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"{setting_name}.metadata_layout could not be read: {metadata_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{setting_name}.metadata_layout contains invalid JSON: {metadata_path}"
            ) from exc
        _validate_product_metadata_layout(
            metadata_document,
            setting_name=setting_name,
            metadata_path=metadata_path,
        )

        archive = section.get("archive")
        if archive is not None and (
            not isinstance(archive, str) or not archive.strip()
        ):
            raise ValueError(f"{setting_name}.archive must be a non-empty string")

        return cls(
            environment=environment,
            visibility=visibility,
            enabled=enabled,
            url=url,
            route=route,
            metadata_layout=metadata_layout,
            metadata_layout_path=metadata_path,
            archive=archive.strip() if isinstance(archive, str) else None,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime settings loaded from environment variables."""

    rpc_urls: tuple[str, ...]
    commitment: str
    watch_addresses: tuple[str, ...]
    watch_signatures: tuple[str, ...]
    webhook_url: str
    webhook_secret: str
    webhook_timeout_seconds: float = 10.0
    webhook_max_attempts: int = 5
    webhook_base_backoff_seconds: float = 2.0
    poll_interval_seconds: float = 2.0
    signature_limit: int = 50
    max_rpc_concurrency: int = 16
    webhook_max_concurrency: int = 16
    event_queue_size: int = 1000
    state_path: str = "./core-rail-state.sqlite3"
    dead_letter_max_entries: int = 1000
    replay_audit_max_entries: int = MAX_REPLAY_AUDIT_ENTRIES
    max_recovery_boundaries: int = MAX_RECOVERY_BOUNDARIES
    block_processing_latency_warning_threshold_ms: float = (
        BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS
    )
    log_level: str = "INFO"
    network: str = "mainnet-beta"
    staging_distribution: StagingDistribution | None = None

    @property
    def settlement_route(self) -> str | None:
        """Return the validated public route used for settlement delivery."""
        if self.staging_distribution is None:
            return None
        return self.staging_distribution.route

    def __post_init__(self) -> None:
        if self.replay_audit_max_entries < 1:
            raise ValueError("replay_audit_max_entries must be at least 1")
        for name in (
            "webhook_timeout_seconds",
            "webhook_base_backoff_seconds",
            "poll_interval_seconds",
            "block_processing_latency_warning_threshold_ms",
        ):
            _validate_positive_float(name, getattr(self, name))
        validate_retry_schedule(
            self.webhook_base_backoff_seconds,
            self.webhook_max_attempts,
        )

    @classmethod
    def from_env(cls, *, require_watch_sources: bool = True) -> Settings:
        staging_distribution_path = (
            os.getenv("STAGING_DISTRIBUTION_PATH", "").strip()
            or str(DEFAULT_STAGING_DISTRIBUTION_PATH)
        )
        staging_distribution = StagingDistribution.from_file(staging_distribution_path)
        rpc_urls = _csv(os.getenv("SOLANA_RPC_URLS"))
        if not rpc_urls:
            rpc_urls = (os.getenv("SOLANA_RPC_URL", DEFAULT_RPC_URL),)
        rpc_urls = tuple(_http_url("SOLANA_RPC_URL", url) for url in rpc_urls)

        webhook_url = os.getenv("WEBHOOK_URL", "").strip()
        if not webhook_url:
            raise ValueError("WEBHOOK_URL is required")
        webhook_secret = os.getenv("WEBHOOK_SECRET", "")
        if len(webhook_secret) < 16:
            raise ValueError("WEBHOOK_SECRET must be at least 16 characters")

        watch_addresses = _csv(os.getenv("SOLANA_WATCH_ADDRESSES"))
        watch_signatures = _csv(os.getenv("SOLANA_WATCH_SIGNATURES"))
        if require_watch_sources and not watch_addresses and not watch_signatures:
            raise ValueError(
                "Set at least one wallet/program in SOLANA_WATCH_ADDRESSES "
                "or one signature in SOLANA_WATCH_SIGNATURES"
            )

        commitment = os.getenv("SOLANA_COMMITMENT", "confirmed").strip().lower()
        if commitment not in {"processed", "confirmed", "finalized"}:
            raise ValueError("SOLANA_COMMITMENT must be processed, confirmed, or finalized")

        network = os.getenv("SOLANA_NETWORK", "mainnet-beta").strip()
        if not network:
            raise ValueError("SOLANA_NETWORK cannot be empty")

        return cls(
            rpc_urls=rpc_urls,
            commitment=commitment,
            watch_addresses=watch_addresses,
            watch_signatures=watch_signatures,
            webhook_url=_http_url("WEBHOOK_URL", webhook_url),
            webhook_secret=webhook_secret,
            webhook_timeout_seconds=_positive_float(
                "WEBHOOK_TIMEOUT_SECONDS", os.getenv("WEBHOOK_TIMEOUT_SECONDS"), 10.0
            ),
            webhook_max_attempts=_positive_int(
                "WEBHOOK_MAX_ATTEMPTS", os.getenv("WEBHOOK_MAX_ATTEMPTS"), 5
            ),
            webhook_base_backoff_seconds=_positive_float(
                "WEBHOOK_BASE_BACKOFF_SECONDS",
                os.getenv("WEBHOOK_BASE_BACKOFF_SECONDS"),
                2.0,
            ),
            poll_interval_seconds=_positive_float(
                "SOLANA_POLL_INTERVAL_SECONDS",
                os.getenv("SOLANA_POLL_INTERVAL_SECONDS"),
                2.0,
            ),
            signature_limit=min(
                _positive_int(
                    "SOLANA_SIGNATURE_LIMIT", os.getenv("SOLANA_SIGNATURE_LIMIT"), 50
                ),
                1000,
            ),
            max_rpc_concurrency=_positive_int(
                "SOLANA_MAX_RPC_CONCURRENCY",
                os.getenv("SOLANA_MAX_RPC_CONCURRENCY"),
                16,
            ),
            webhook_max_concurrency=_positive_int(
                "WEBHOOK_MAX_CONCURRENCY",
                os.getenv("WEBHOOK_MAX_CONCURRENCY"),
                16,
            ),
            event_queue_size=_positive_int(
                "EVENT_QUEUE_SIZE", os.getenv("EVENT_QUEUE_SIZE"), 1000
            ),
            state_path=os.getenv("STATE_PATH", "./core-rail-state.sqlite3").strip()
            or "./core-rail-state.sqlite3",
            dead_letter_max_entries=_positive_int(
                "DEAD_LETTER_MAX_ENTRIES",
                os.getenv("DEAD_LETTER_MAX_ENTRIES"),
                1000,
            ),
            replay_audit_max_entries=_positive_int(
                "REPLAY_AUDIT_MAX_ENTRIES",
                os.getenv("REPLAY_AUDIT_MAX_ENTRIES"),
                MAX_REPLAY_AUDIT_ENTRIES,
            ),
            max_recovery_boundaries=_positive_int(
                "RECOVERY_BOUNDARY_MAX_ENTRIES",
                os.getenv("RECOVERY_BOUNDARY_MAX_ENTRIES"),
                MAX_RECOVERY_BOUNDARIES,
            ),
            block_processing_latency_warning_threshold_ms=_positive_float(
                "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS",
                os.getenv("BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS"),
                BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS,
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            network=network,
            staging_distribution=staging_distribution,
        )