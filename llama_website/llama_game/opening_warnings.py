"""Scheduled, privacy-safe regression warnings for the TLAMA Arcade opening."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal
from urllib.request import Request, urlopen

from storage import ArcadeStorage

LOG = logging.getLogger("trustllama.opening_warnings")
DEFAULT_MINIMUM_SAMPLE = 50
FIRST_ORB_DROP_THRESHOLD = 0.10
LEVEL_2_DROP_THRESHOLD = 0.10
SUPPORTED_OPENING_VERSIONS = ("andean_v1",)


@dataclass(frozen=True)
class OpeningRegression:
    warning_key: str
    opening_version: str
    metric: str
    previous_month: str
    current_month: str
    previous_rate: float
    current_rate: float
    previous_joined: int
    current_joined: int
    current_predator_collision_rate: float

def opening_sample_rows(
    rows: list[dict[str, object]],
    months: tuple[str, str],
    *,
    minimum_sample: int = DEFAULT_MINIMUM_SAMPLE,
) -> list[dict[str, object]]:
    """Build privacy-safe operator rows using the scheduler's sample rule."""

    by_version: dict[str, dict[str, dict[str, object]]] = {
        version: {} for version in SUPPORTED_OPENING_VERSIONS
    }
    for row in rows:
        version = str(row["opening_version"])
        month = str(row["month_utc"])
        if month in months:
            by_version.setdefault(version, {})[month] = row

    result: list[dict[str, object]] = []
    for version, monthly in sorted(by_version.items()):
        for month in months:
            source = monthly.get(month, {})
            joined = int(source.get("joined", 0))
            eligible = joined >= minimum_sample
            result.append(
                {
                    "Opening version": version,
                    "Month (UTC)": month,
                    "Joined": joined,
                    "First orb": int(source.get("first_orb", 0)),
                    "Level 2": int(source.get("level_2", 0)),
                    "Chicken context": int(source.get("chicken_collision", 0)),
                    "Sample status": (
                        f"Eligible sample ({joined}/{minimum_sample} joined)"
                        if eligible
                        else f"Insufficient sample ({joined}/{minimum_sample} joined)"
                    ),
                }
            )
    return result
def last_two_completed_utc_months(now: datetime | None = None) -> tuple[str, str]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    this_month = current.year * 12 + current.month - 1

    def label(month_number: int) -> str:
        return f"{month_number // 12:04d}-{month_number % 12 + 1:02d}"

    return label(this_month - 2), label(this_month - 1)


def evaluate_regressions(
    rows: list[dict[str, object]],
    months: tuple[str, str],
    *,
    minimum_sample: int = DEFAULT_MINIMUM_SAMPLE,
) -> list[OpeningRegression]:
    by_version: dict[str, dict[str, dict[str, object]]] = {}
    for row in rows:
        version = str(row["opening_version"])
        month = str(row["month_utc"])
        if month in months:
            by_version.setdefault(version, {})[month] = row

    regressions: list[OpeningRegression] = []
    for version, monthly in sorted(by_version.items()):
        if not all(month in monthly for month in months):
            continue
        previous, current = (monthly[month] for month in months)
        previous_joined = int(previous["joined"])
        current_joined = int(current["joined"])
        if previous_joined < minimum_sample or current_joined < minimum_sample:
            continue
        predator_collision_rate = int(current["chicken_collision"]) / current_joined
        for metric, column, threshold in (
            ("join_to_first_orb", "first_orb", FIRST_ORB_DROP_THRESHOLD),
            ("join_to_level_2", "level_2", LEVEL_2_DROP_THRESHOLD),
        ):
            previous_rate = int(previous[column]) / previous_joined
            current_rate = int(current[column]) / current_joined
            if previous_rate - current_rate + 1e-12 < threshold:
                continue
            warning_key = f"{months[1]}:{version}:{metric}"
            regressions.append(
                OpeningRegression(
                    warning_key=warning_key,
                    opening_version=version,
                    metric=metric,
                    previous_month=months[0],
                    current_month=months[1],
                    previous_rate=previous_rate,
                    current_rate=current_rate,
                    previous_joined=previous_joined,
                    current_joined=current_joined,
                    current_predator_collision_rate=predator_collision_rate,
                )
            )
    return regressions


def warning_message(regression: OpeningRegression) -> str:
    label = {
        "join_to_first_orb": "Join → first orb",
        "join_to_level_2": "Join → Level 2",
    }[regression.metric]
    return "\n".join(
        (
            "TLAMA ARCADE OPENING REGRESSION",
            f"{label} · {regression.opening_version}",
            (
                f"{regression.previous_month}: {regression.previous_rate:.1%} "
                f"({regression.previous_joined} joined)"
            ),
            (
                f"{regression.current_month}: {regression.current_rate:.1%} "
                f"({regression.current_joined} joined)"
            ),
            f"Drop: {regression.previous_rate - regression.current_rate:.1%}",
            (
                "Context only — current predator collision rate "
                "(puma in andean_v2/andean_v3; chicken in andean_v1): "
                f"{regression.current_predator_collision_rate:.1%}"
            ),
        )
    )


def regression_for_warning(
    storage: ArcadeStorage, warning_key: str
) -> OpeningRegression | None:
    """Rebuild a fixed aggregate warning without persisting message text."""

    try:
        current_month = warning_key.split(":", 1)[0]
        current = datetime.strptime(current_month, "%Y-%m").replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return None
    month_number = current.year * 12 + current.month - 1
    previous_number = month_number - 1
    previous_month = (
        f"{previous_number // 12:04d}-{previous_number % 12 + 1:02d}"
    )
    regressions = evaluate_regressions(
        storage.opening_counts_for_months((previous_month, current_month)),
        (previous_month, current_month),
    )
    return next(
        (item for item in regressions if item.warning_key == warning_key),
        None,
    )


def resend_uncertain_warning(
    storage: ArcadeStorage,
    warning_key: str,
    *,
    send: Callable[[str], DeliveryOutcome] | None = None,
) -> DeliveryOutcome | None:
    """Perform at most one operator-requested resend for an uncertain warning."""

    regression = regression_for_warning(storage, warning_key)
    if regression is None:
        return None
    if not storage.reserve_uncertain_opening_warning_resend(warning_key):
        return None
    sender = send or send_operator_notification
    outcome = sender(warning_message(regression))
    storage.record_uncertain_opening_warning_resend_result(warning_key, outcome)
    return outcome


DeliveryOutcome = Literal["delivered", "rejected", "unknown"]


def send_operator_notification(message: str) -> DeliveryOutcome:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        LOG.warning("opening_warning_not_sent reason=telegram_not_configured")
        return "rejected"
    body = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        return "delivered" if result.get("ok") is True else "rejected"
    except Exception:
        LOG.exception("opening_warning_delivery_failed")
        return "unknown"


def run_check(
    storage: ArcadeStorage,
    *,
    now: datetime | None = None,
    send: Callable[[str], DeliveryOutcome] = send_operator_notification,
) -> list[OpeningRegression]:
    months = last_two_completed_utc_months(now)
    rows = storage.opening_counts_for_months(months)
    regressions = evaluate_regressions(rows, months)
    for regression in regressions:
        if not storage.claim_opening_warning(regression.warning_key):
            continue
        outcome = send(warning_message(regression))
        if outcome == "delivered":
            storage.mark_opening_warning_delivered(regression.warning_key)
            LOG.warning(
                "opening_regression_warning_delivered key=%s",
                regression.warning_key,
            )
        elif outcome == "rejected":
            storage.release_opening_warning_claim(regression.warning_key)
        else:
            LOG.error(
                "opening_regression_warning_uncertain key=%s action=manual_reconciliation",
                regression.warning_key,
            )
    return regressions


async def run_scheduled_checks(storage: ArcadeStorage) -> None:
    interval = max(3600, int(os.environ.get("OPENING_WARNING_INTERVAL_SECONDS", "21600")))
    while True:
        try:
            await asyncio.to_thread(run_check, storage)
        except Exception:
            LOG.exception("opening_regression_check_failed")
        await asyncio.sleep(interval)
