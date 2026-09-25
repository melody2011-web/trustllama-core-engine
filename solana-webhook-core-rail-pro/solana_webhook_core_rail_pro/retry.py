"""Shared webhook retry schedule validation."""

from __future__ import annotations

import math

MAX_RETRY_DELAY_SECONDS = 24 * 60 * 60
RETRY_DELAY_WARNING_THRESHOLD = 0.8
RETRY_SCHEDULE_FINITE_ERROR = "retry backoff schedule must remain finite"
RETRY_DELAY_MAX_ERROR = (
    "retry backoff delay must not exceed "
    f"{MAX_RETRY_DELAY_SECONDS} seconds"
)


def max_retry_delay_seconds(
    base_backoff_seconds: float,
    max_attempts: int,
) -> float:
    """Return the final exponential backoff delay for a retry schedule."""
    if max_attempts <= 1:
        return 0.0
    try:
        return math.ldexp(
            base_backoff_seconds,
            max_attempts - 2,
        )
    except OverflowError:
        return math.inf


def retry_schedule(
    base_backoff_seconds: float,
    max_attempts: int,
) -> tuple[float, ...]:
    """Return every bounded delay between delivery attempts."""
    max_retry_delay = max_retry_delay_seconds(
        base_backoff_seconds,
        max_attempts,
    )
    if not math.isfinite(max_retry_delay):
        raise ValueError(RETRY_SCHEDULE_FINITE_ERROR)
    if max_retry_delay > MAX_RETRY_DELAY_SECONDS:
        raise ValueError(RETRY_DELAY_MAX_ERROR)
    return tuple(
        math.ldexp(base_backoff_seconds, retry_number)
        for retry_number in range(max_attempts - 1)
    )


def validate_retry_schedule(
    base_backoff_seconds: float,
    max_attempts: int,
) -> None:
    """Reject retry schedules that can overflow or wait too long."""
    retry_schedule(base_backoff_seconds, max_attempts)
