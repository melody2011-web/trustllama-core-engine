"""Small, serializable models for Solana transactions and webhook events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

SETTLEMENT_ROUTE_FIELD = "settlement_route"
SETTLEMENT_ROUTE_STATUS_FIELD = "settlement_route_status"
SETTLEMENT_ROUTE_RECORDED = "recorded"
SETTLEMENT_ROUTE_LEGACY_UNKNOWN = "legacy_unknown"


def _utc_isoformat(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def settlement_route_status(payload: dict[str, Any]) -> str:
    """Classify whether a payload carries usable historical route metadata.

    Only a non-empty string is evidence of a recorded route. Missing, blank,
    and otherwise malformed values remain explicitly unknown.
    """
    route = payload.get(SETTLEMENT_ROUTE_FIELD)
    if isinstance(route, str) and route.strip():
        return SETTLEMENT_ROUTE_RECORDED
    return SETTLEMENT_ROUTE_LEGACY_UNKNOWN


def replay_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Prepare a payload without allowing current config to rewrite history.

    Records with a non-empty string route are delivered unchanged. Older
    records, plus records with missing, blank, or malformed route metadata,
    receive an explicit null route and status so receivers do not infer the
    current configured route as historical fact.
    """
    if settlement_route_status(payload) == SETTLEMENT_ROUTE_RECORDED:
        return payload
    replay = dict(payload)
    replay[SETTLEMENT_ROUTE_FIELD] = None
    replay[SETTLEMENT_ROUTE_STATUS_FIELD] = SETTLEMENT_ROUTE_LEGACY_UNKNOWN
    return replay


@dataclass(frozen=True, slots=True)
class TransactionEvent:
    """A normalized transaction event emitted by the RPC streamer."""

    signature: str
    source_address: str | None
    slot: int | None
    block_time: str | None
    transaction: dict[str, Any]
    meta: dict[str, Any] | None
    observed_at: str

    @classmethod
    def from_rpc_result(
        cls,
        *,
        signature: str,
        source_address: str | None,
        result: dict[str, Any],
    ) -> "TransactionEvent":
        transaction = result.get("transaction")
        if not isinstance(transaction, dict):
            transaction = {}
        meta = result.get("meta")
        if not isinstance(meta, dict):
            meta = None
        return cls(
            signature=signature,
            source_address=source_address,
            slot=result.get("slot"),
            block_time=_utc_isoformat(result.get("blockTime")),
            transaction=transaction,
            meta=meta,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

    def webhook_payload(
        self,
        network: str,
        settlement_route: str | None = None,
    ) -> dict[str, Any]:
        """Return the exact event envelope delivered to the configured webhook."""
        payload: dict[str, Any] = {
            "event": "solana.transaction",
            "network": network,
            "signature": self.signature,
            "source_address": self.source_address,
            "slot": self.slot,
            "block_time": self.block_time,
            "observed_at": self.observed_at,
            "transaction": self.transaction,
            "meta": self.meta,
        }
        if settlement_route is not None:
            payload[SETTLEMENT_ROUTE_FIELD] = settlement_route
        return payload
