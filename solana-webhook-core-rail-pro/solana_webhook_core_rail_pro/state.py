"""Durable cursors, recovery evidence, and bounded dead-letter storage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, Sequence

from .models import settlement_route_status

if TYPE_CHECKING:
    from .webhook import DeliveryResult

ReplayOutcome = Literal["delivered", "failed", "skipped"]
MAX_REPLAY_RECORDS = 100
MAX_REPLAY_AUDIT_ENTRIES = 1000
MAX_RECOVERY_BOUNDARIES = 1000
MAX_RESOLVED_RECOVERY_BOUNDARIES = 1000


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DurableState:
    """SQLite-backed state for replay-safe polling and failed deliveries.

    The database is intentionally small and local. SQLite gives cursor updates
    and dead-letter inserts atomic durability without adding a service
    dependency, while still leaving the dead-letter records easy to inspect.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_dead_letters: int = 1000,
        max_replay_audit_entries: int = MAX_REPLAY_AUDIT_ENTRIES,
        max_recovery_boundaries: int = MAX_RECOVERY_BOUNDARIES,
        max_resolved_recovery_boundaries: int = MAX_RESOLVED_RECOVERY_BOUNDARIES,
    ) -> None:
        if max_dead_letters < 1:
            raise ValueError("max_dead_letters must be at least 1")
        if max_replay_audit_entries < 1:
            raise ValueError("max_replay_audit_entries must be at least 1")
        if max_recovery_boundaries < 1:
            raise ValueError("max_recovery_boundaries must be at least 1")
        if max_resolved_recovery_boundaries < 1:
            raise ValueError("max_resolved_recovery_boundaries must be at least 1")
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._max_dead_letters = max_dead_letters
        self._max_replay_audit_entries = max_replay_audit_entries
        self._max_recovery_boundaries = max_recovery_boundaries
        self._max_resolved_recovery_boundaries = max_resolved_recovery_boundaries
        self._connection = sqlite3.connect(self._path)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS address_cursors (
                    address TEXT PRIMARY KEY,
                    signature TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS explicit_signatures (
                    signature TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_boundaries (
                    address TEXT PRIMARY KEY,
                    cursor TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    last_detected_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recovery_boundaries_last_detected_idx
                    ON recovery_boundaries (last_detected_at, address);
                CREATE TABLE IF NOT EXISTS resolved_recovery_boundaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    last_detected_at TEXT NOT NULL,
                    occurrences INTEGER NOT NULL,
                    resolved_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS resolved_recovery_boundaries_resolved_at_idx
                    ON resolved_recovery_boundaries (resolved_at, id);
                CREATE TABLE IF NOT EXISTS recovery_boundary_totals (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    missing_total INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO recovery_boundary_totals(id, missing_total)
                VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS recovery_boundary_retention (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    trimmed_total INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO recovery_boundary_retention(id, trimmed_total)
                VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS recovery_boundary_evictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    address TEXT NOT NULL,
                    cursor TEXT NOT NULL,
                    evicted_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS recovery_boundary_evictions_id_idx
                    ON recovery_boundary_evictions (id);
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signature TEXT,
                    source_address TEXT,
                    payload_json TEXT NOT NULL,
                    attempts INTEGER NOT NULL,
                    status INTEGER,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dead_letters_created_at_idx
                    ON dead_letters (created_at);
                CREATE TABLE IF NOT EXISTS dead_letter_replays (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dead_letter_id INTEGER NOT NULL,
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('delivered', 'failed', 'skipped')
                    ),
                    attempts INTEGER NOT NULL,
                    status INTEGER,
                    error TEXT,
                    replayed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS dead_letter_replays_dead_letter_idx
                    ON dead_letter_replays (dead_letter_id, id);
                CREATE TABLE IF NOT EXISTS dead_letter_replay_successes (
                    dead_letter_id INTEGER PRIMARY KEY,
                    delivered_at TEXT NOT NULL
                );
                """
            )
            self._connection.execute(
                """
                INSERT OR IGNORE INTO dead_letter_replay_successes(
                    dead_letter_id, delivered_at
                )
                SELECT dead_letter_id, replayed_at
                FROM dead_letter_replays
                WHERE outcome = 'delivered'
                  AND EXISTS (
                      SELECT 1
                      FROM dead_letters
                      WHERE dead_letters.id = dead_letter_replays.dead_letter_id
                  )
                """
            )
            self._connection.execute(
                """
                DELETE FROM dead_letter_replay_successes
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM dead_letters
                    WHERE dead_letters.id = dead_letter_replay_successes.dead_letter_id
                )
                """
            )
            self._prune_replay_audits()

    def _prune_replay_audits(self) -> None:
        """Keep only the newest replay audit rows within the configured bound."""
        self._connection.execute(
            """
            DELETE FROM dead_letter_replays
            WHERE id IN (
                SELECT id
                FROM dead_letter_replays
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (self._max_replay_audit_entries,),
        )

    def get_cursor(self, address: str) -> str | None:
        row = self._connection.execute(
            "SELECT signature FROM address_cursors WHERE address = ?",
            (address,),
        ).fetchone()
        return str(row["signature"]) if row else None

    def set_cursor(self, address: str, signature: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO address_cursors(address, signature, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    signature = excluded.signature,
                    updated_at = excluded.updated_at
                """,
                (address, signature, _now()),
            )

    def has_explicit_signature(self, signature: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM explicit_signatures WHERE signature = ?",
            (signature,),
        ).fetchone()
        return row is not None

    def mark_explicit_signature(self, signature: str) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT OR IGNORE INTO explicit_signatures(signature, processed_at)
                VALUES (?, ?)
                """,
                (signature, _now()),
            )

    def record_recovery_boundary_missing(
        self,
        address: str,
        cursor: str,
    ) -> tuple[dict[str, Any], bool]:
        """Persist an unresolved pruning boundary and return its newness.

        The active row is retained until the cursor is observed again. The
        cumulative total is intentionally separate so trimming old active rows
        cannot erase incident detection evidence.
        """
        detected_at = _now()
        with self._connection:
            row = self._connection.execute(
                """
                SELECT address, cursor, detected_at, last_detected_at, occurrences
                FROM recovery_boundaries
                WHERE address = ?
                """,
                (address,),
            ).fetchone()
            newly_detected = row is None
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO recovery_boundaries(
                        address, cursor, detected_at, last_detected_at, occurrences
                    )
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (address, cursor, detected_at, detected_at),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE recovery_boundaries
                    SET cursor = ?, last_detected_at = ?, occurrences = occurrences + 1
                    WHERE address = ?
                    """,
                    (cursor, detected_at, address),
                )
            self._connection.execute(
                """
                UPDATE recovery_boundary_totals
                SET missing_total = missing_total + 1
                WHERE id = 1
                """
            )
            evicted_rows = self._connection.execute(
                """
                SELECT address, cursor
                FROM recovery_boundaries
                WHERE address NOT IN (
                    SELECT address
                    FROM recovery_boundaries
                    ORDER BY last_detected_at DESC, address DESC
                    LIMIT ?
                )
                """,
                (self._max_recovery_boundaries,),
            ).fetchall()
            self._connection.execute(
                """
                DELETE FROM recovery_boundaries
                WHERE address NOT IN (
                    SELECT address
                    FROM recovery_boundaries
                    ORDER BY last_detected_at DESC, address DESC
                    LIMIT ?
                )
                """,
                (self._max_recovery_boundaries,),
            )
            if evicted_rows:
                eviction_time = _now()
                self._connection.executemany(
                    """
                    INSERT INTO recovery_boundary_evictions(address, cursor, evicted_at)
                    VALUES (?, ?, ?)
                    """,
                    [
                        (row["address"], row["cursor"], eviction_time)
                        for row in evicted_rows
                    ],
                )
                self._connection.execute(
                    """
                    UPDATE recovery_boundary_retention
                    SET trimmed_total = trimmed_total + ?
                    WHERE id = 1
                    """,
                    (len(evicted_rows),),
                )
                self._connection.execute(
                    """
                    DELETE FROM recovery_boundary_evictions
                    WHERE id IN (
                        SELECT id
                        FROM recovery_boundary_evictions
                        ORDER BY id DESC
                        LIMIT -1 OFFSET ?
                    )
                    """,
                    (self._max_recovery_boundaries,),
                )
            current = self._connection.execute(
                """
                SELECT address, cursor, detected_at, last_detected_at, occurrences
                FROM recovery_boundaries
                WHERE address = ?
                """,
                (address,),
            ).fetchone()
        if current is None:
            # A new row can only be trimmed when the configured bound is
            # invalid, which is rejected in __init__. Keep this explicit if
            # the schema or retention policy changes in the future.
            raise RuntimeError("recovery boundary was not retained")
        return dict(current), newly_detected

    def clear_recovery_boundary(self, address: str) -> dict[str, Any] | None:
        """Record resolution and remove an active boundary after its cursor is observed."""
        resolved_at = _now()
        with self._connection:
            row = self._connection.execute(
                """
                SELECT address, cursor, detected_at, last_detected_at, occurrences
                FROM recovery_boundaries
                WHERE address = ?
                """,
                (address,),
            ).fetchone()
            if row is None:
                return None
            self._connection.execute(
                """
                INSERT INTO resolved_recovery_boundaries(
                    address, cursor, detected_at, last_detected_at, occurrences, resolved_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["address"],
                    row["cursor"],
                    row["detected_at"],
                    row["last_detected_at"],
                    row["occurrences"],
                    resolved_at,
                ),
            )
            self._connection.execute(
                """
                DELETE FROM resolved_recovery_boundaries
                WHERE id IN (
                    SELECT id
                    FROM resolved_recovery_boundaries
                    ORDER BY resolved_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self._max_resolved_recovery_boundaries,),
            )
            self._connection.execute(
                "DELETE FROM recovery_boundaries WHERE address = ?",
                (address,),
            )
        return {
            **dict(row),
            "resolved_at": resolved_at,
        }

    def list_recovery_boundaries(self) -> list[dict[str, Any]]:
        """Return active pruning boundaries in stable dashboard order."""
        rows = self._connection.execute(
            """
            SELECT address, cursor, detected_at, last_detected_at, occurrences
            FROM recovery_boundaries
            ORDER BY address ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def list_resolved_recovery_boundaries(self) -> list[dict[str, Any]]:
        """Return the newest resolved pruning incidents within the audit bound."""
        rows = self._connection.execute(
            """
            SELECT address, cursor, detected_at, last_detected_at, occurrences, resolved_at
            FROM resolved_recovery_boundaries
            ORDER BY resolved_at DESC, id DESC
            LIMIT ?
            """,
            (self._max_resolved_recovery_boundaries,),
        ).fetchall()
        return [dict(row) for row in rows]

    def recovery_boundary_missing_total(self) -> int:
        row = self._connection.execute(
            "SELECT missing_total FROM recovery_boundary_totals WHERE id = 1"
        ).fetchone()
        return int(row["missing_total"]) if row else 0

    @property
    def max_recovery_boundaries(self) -> int:
        """Return the configured active recovery-evidence capacity."""
        return self._max_recovery_boundaries

    def recovery_boundary_trimmed_total(self) -> int:
        """Return the cumulative number of active evidence rows evicted."""
        row = self._connection.execute(
            "SELECT trimmed_total FROM recovery_boundary_retention WHERE id = 1"
        ).fetchone()
        return int(row["trimmed_total"]) if row else 0

    def list_recovery_boundary_evictions(self) -> list[dict[str, Any]]:
        """Return the newest bounded audit records for evicted boundaries."""
        rows = self._connection.execute(
            """
            SELECT address, cursor, evicted_at
            FROM recovery_boundary_evictions
            ORDER BY id DESC
            LIMIT ?
            """,
            (self._max_recovery_boundaries,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_dead_letter(
        self,
        payload: dict[str, Any],
        result: DeliveryResult,
    ) -> int:
        """Store a permanently failed event and trim the oldest records."""
        with self._connection:
            cursor = self._connection.execute(
                """
                INSERT INTO dead_letters(
                    signature, source_address, payload_json, attempts, status,
                    error, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.get("signature"),
                    payload.get("source_address"),
                    json.dumps(
                        payload,
                        separators=(",", ":"),
                        sort_keys=True,
                        ensure_ascii=False,
                    ),
                    result.attempts,
                    result.status,
                    result.error,
                    _now(),
                ),
            )
            evicted_rows = self._connection.execute(
                """
                SELECT id
                FROM dead_letters
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
                """,
                (self._max_dead_letters,),
            ).fetchall()
            if evicted_rows:
                evicted_ids = tuple(row["id"] for row in evicted_rows)
                placeholders = ",".join("?" for _ in evicted_ids)
                self._connection.execute(
                    f"DELETE FROM dead_letters WHERE id IN ({placeholders})",
                    evicted_ids,
                )
                self._connection.execute(
                    f"""
                    DELETE FROM dead_letter_replay_successes
                    WHERE dead_letter_id IN ({placeholders})
                    """,
                    evicted_ids,
                )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def list_dead_letters(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return newest dead-letter records for operational inspection."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        rows = self._connection.execute(
            """
            SELECT id, signature, source_address, payload_json, attempts,
                   status, error, created_at
            FROM dead_letters
            ORDER BY id DESC
            LIMIT ?
            """,
            (min(limit, self._max_dead_letters),),
        ).fetchall()
        return self._decode_dead_letter_rows(rows)

    def get_dead_letters(
        self,
        ids: Sequence[int],
        *,
        limit: int = MAX_REPLAY_RECORDS,
    ) -> list[dict[str, Any]]:
        """Return a bounded, explicitly selected set of dead-letter records."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        selected_ids = tuple(dict.fromkeys(ids))
        if not selected_ids:
            raise ValueError("at least one dead-letter id is required")
        selection_limit = min(limit, MAX_REPLAY_RECORDS, self._max_dead_letters)
        if len(selected_ids) > selection_limit:
            raise ValueError(
                f"at most {selection_limit} dead-letter records may be selected"
            )
        if any(record_id < 1 for record_id in selected_ids):
            raise ValueError("dead-letter ids must be positive integers")
        placeholders = ",".join("?" for _ in selected_ids)
        rows = self._connection.execute(
            f"""
            SELECT id, signature, source_address, payload_json, attempts,
                   status, error, created_at
            FROM dead_letters
            WHERE id IN ({placeholders})
            ORDER BY id ASC
            """,
            selected_ids,
        ).fetchall()
        return self._decode_dead_letter_rows(rows)

    @staticmethod
    def _decode_dead_letter_rows(
        rows: Sequence[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        """Decode retained payloads and derive route status from their value."""
        records: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            record["payload"] = json.loads(record.pop("payload_json"))
            record["settlement_route_status"] = settlement_route_status(
                record["payload"]
            )
            records.append(record)
        return records

    def has_successful_replay(self, dead_letter_id: int) -> bool:
        row = self._connection.execute(
            """
            SELECT 1
            FROM dead_letter_replay_successes
            WHERE dead_letter_id = ?
            """,
            (dead_letter_id,),
        ).fetchone()
        return row is not None

    def successful_replay_count(self) -> int:
        """Return the number of successful-replay markers for retained dead letters."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM dead_letter_replay_successes"
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def record_replay_outcome(
        self,
        dead_letter_id: int,
        *,
        outcome: ReplayOutcome,
        result: DeliveryResult | None = None,
        error: str | None = None,
    ) -> int:
        """Append an immutable audit record for a replay attempt."""
        if outcome not in {"delivered", "failed", "skipped"}:
            raise ValueError(f"unsupported replay outcome: {outcome}")
        attempts = result.attempts if result is not None else 0
        status = result.status if result is not None else None
        replay_error = error if error is not None else (
            result.error if result is not None else None
        )
        replayed_at = _now()
        with self._connection:
            if outcome == "delivered":
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO dead_letter_replay_successes(
                        dead_letter_id, delivered_at
                    )
                    VALUES (?, ?)
                    """,
                    (dead_letter_id, replayed_at),
                )
            cursor = self._connection.execute(
                """
                INSERT INTO dead_letter_replays(
                    dead_letter_id, outcome, attempts, status, error, replayed_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dead_letter_id,
                    outcome,
                    attempts,
                    status,
                    replay_error,
                    replayed_at,
                ),
            )
            self._prune_replay_audits()
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    def list_replay_outcomes(
        self,
        *,
        dead_letter_id: int | None = None,
        limit: int = MAX_REPLAY_RECORDS,
    ) -> list[dict[str, Any]]:
        """Return newest replay audit records without changing dead letters."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if dead_letter_id is None:
            rows = self._connection.execute(
                """
                SELECT id, dead_letter_id, outcome, attempts, status, error, replayed_at
                FROM dead_letter_replays
                ORDER BY id DESC
                LIMIT ?
                """,
                (min(limit, MAX_REPLAY_RECORDS, self._max_replay_audit_entries),),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT id, dead_letter_id, outcome, attempts, status, error, replayed_at
                FROM dead_letter_replays
                WHERE dead_letter_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (
                    dead_letter_id,
                    min(limit, MAX_REPLAY_RECORDS, self._max_replay_audit_entries),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    @property
    def max_replay_audit_entries(self) -> int:
        """Return the configured replay-audit retention capacity."""
        return self._max_replay_audit_entries

    def dead_letter_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM dead_letters").fetchone()
        assert row is not None
        return int(row["count"])

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
