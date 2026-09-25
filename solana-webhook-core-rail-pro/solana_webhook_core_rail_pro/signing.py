"""HMAC-SHA256 signing helpers for webhook authenticity."""

from __future__ import annotations

import hashlib
import hmac


SIGNATURE_HEADER = "X-TrustLlama-Signature"
SIGNATURE_PREFIX = "sha256="


def sign_payload(payload: bytes, secret: str) -> str:
    """Sign the exact bytes sent over HTTP using HMAC-SHA256."""
    digest = hmac.new(
        secret.encode("utf-8"),
        payload,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Constant-time verification for the X-TrustLlama-Signature header."""
    if not signature or not signature.startswith(SIGNATURE_PREFIX):
        return False
    expected = sign_payload(payload, secret)
    return hmac.compare_digest(expected, signature)