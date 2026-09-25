"""Safe operational logging helpers for provider-facing failures."""

from __future__ import annotations

import re

_LOG_DETAIL_LIMIT = 512
_SENSITIVE_PARAMETER_NAMES = (
    r"access[-_]?token|api[-_]?key|authorization|bearer|client[-_]?secret|"
    r"credential|password|private[-_]?key|secret|session|signature|sig|token|"
    r"x[-_]?amz[-_]?(?:credential|signature)"
)
_URL_CREDENTIALS = re.compile(
    r"(?P<scheme>https?://)(?P<credentials>[^/\s:@]+(?::[^/\s@]*)?@)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?P<prefix>\b(?:authorization|proxy-authorization)\s*[:=]\s*)"
    r"(?P<scheme>bearer|basic)\s+\S+",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_VALUE = re.compile(
    rf"(?P<prefix>[?&](?:{_SENSITIVE_PARAMETER_NAMES})\s*=\s*)"
    r"(?P<value>[^&#\s,}\]]+)",
    re.IGNORECASE,
)
_SENSITIVE_ASSIGNMENT_VALUE = re.compile(
    rf"(?P<prefix>(?<![\w-])(?:{_SENSITIVE_PARAMETER_NAMES})(?![\w-])"
    r"\s*[:=]\s*[\"']?)"
    r"(?P<value>[^,\s&;}\]\"']+)",
    re.IGNORECASE,
)


def safe_log_detail(value: object) -> str:
    """Return useful text without copying provider secrets to operational logs."""
    detail = str(value)
    detail = _URL_CREDENTIALS.sub(r"\g<scheme><redacted>@", detail)
    detail = _AUTHORIZATION_VALUE.sub(r"\g<prefix><redacted>", detail)
    detail = _SENSITIVE_QUERY_VALUE.sub(r"\g<prefix><redacted>", detail)
    detail = _SENSITIVE_ASSIGNMENT_VALUE.sub(r"\g<prefix><redacted>", detail)
    detail = re.sub(r"[\x00-\x1f\x7f]+", " ", detail).strip()
    if not detail:
        return "<no detail>"
    if len(detail) > _LOG_DETAIL_LIMIT:
        return f"{detail[:_LOG_DETAIL_LIMIT]}…"
    return detail


def safe_exception_detail(error: BaseException) -> str:
    """Return exception context without copying provider secrets to logs."""
    return safe_log_detail(error)