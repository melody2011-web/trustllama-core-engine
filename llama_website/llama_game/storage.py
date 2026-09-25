"""Durable local storage for the TLAMA Arcade prototype."""

from __future__ import annotations

import sqlite3
import time
import uuid
import hmac
from datetime import datetime, timezone
from hashlib import sha256
from dataclasses import dataclass
from pathlib import Path


CONFIRMED_COSMETIC_SKIN_IDS = frozenset({"aurora", "midnight"})


@dataclass(frozen=True)
class StoredPlayer:
    player_id: str
    name: str
    high_score: int
    equipped_skin_id: str
    owned_skin_ids: tuple[str, ...]


@dataclass(frozen=True)
class MatchTicket:
    ticket_id: str
    player_id: str
    status: str
    entry_fee_tlama: int


class ArcadeStorage:
    """Small SQLite repository with one short-lived connection per operation."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self, *, timeout: float = 5) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(players)"
                ).fetchall()
            }
            if columns and "identity_hash" not in columns:
                self._migrate_name_keyed_players(connection)
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(players)"
                    ).fetchall()
                }
            if columns and "ownership_hash" not in columns:
                connection.execute(
                    "ALTER TABLE players ADD COLUMN ownership_hash TEXT"
                )
            if columns and "equipped_skin_id" not in columns:
                connection.execute(
                    "ALTER TABLE players ADD COLUMN equipped_skin_id TEXT "
                    "NOT NULL DEFAULT 'default'"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT PRIMARY KEY,
                    identity_hash TEXT NOT NULL UNIQUE,
                    ownership_hash TEXT,
                    name TEXT NOT NULL,
                    high_score INTEGER NOT NULL DEFAULT 0 CHECK (high_score >= 0),
                    equipped_skin_id TEXT NOT NULL DEFAULT 'default',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS player_cosmetics (
                    player_id TEXT NOT NULL REFERENCES players(player_id)
                        ON DELETE CASCADE,
                    skin_id TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source = 'onchain_confirmed'),
                    acquired_at INTEGER NOT NULL,
                    PRIMARY KEY (player_id, skin_id)
                );

                CREATE INDEX IF NOT EXISTS player_cosmetics_player_id
                    ON player_cosmetics(player_id);

                CREATE TABLE IF NOT EXISTS match_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    player_id TEXT NOT NULL REFERENCES players(player_id),
                    status TEXT NOT NULL CHECK (
                        status IN ('simulated_active', 'simulated_completed')
                    ),
                    entry_fee_tlama INTEGER NOT NULL CHECK (entry_fee_tlama >= 0),
                    final_score INTEGER CHECK (final_score IS NULL OR final_score >= 0),
                    issued_at INTEGER NOT NULL,
                    completed_at INTEGER
                );

                CREATE INDEX IF NOT EXISTS match_tickets_player_id
                    ON match_tickets(player_id);

                CREATE TABLE IF NOT EXISTS utility_purchases (
                    signature TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL REFERENCES match_tickets(ticket_id),
                    player_id TEXT NOT NULL REFERENCES players(player_id),
                    item_id TEXT NOT NULL,
                    amount_tlama INTEGER NOT NULL CHECK (amount_tlama > 0),
                    purchased_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS utility_purchases_ticket_item
                    ON utility_purchases(ticket_id, item_id);
                CREATE INDEX IF NOT EXISTS players_high_score
                    ON players(high_score DESC, name COLLATE NOCASE);

                CREATE TABLE IF NOT EXISTS opening_monthly_counts (
                    month_utc TEXT NOT NULL,
                    opening_version TEXT NOT NULL,
                    joined INTEGER NOT NULL DEFAULT 0 CHECK (joined >= 0),
                    first_orb INTEGER NOT NULL DEFAULT 0 CHECK (first_orb >= 0),
                    level_2 INTEGER NOT NULL DEFAULT 0 CHECK (level_2 >= 0),
                    chicken_collision INTEGER NOT NULL DEFAULT 0
                        CHECK (chicken_collision >= 0),
                    PRIMARY KEY (month_utc, opening_version)
                );

                CREATE TABLE IF NOT EXISTS opening_warning_deliveries (
                    warning_key TEXT PRIMARY KEY,
                    delivered_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opening_warning_claims (
                    warning_key TEXT PRIMARY KEY,
                    claimed_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS opening_warning_resolution_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    warning_key TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (
                        action IN ('confirm_delivered', 'request_resend', 'resend_result')
                    ),
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('accepted', 'delivered', 'rejected', 'unknown')
                    ),
                    recorded_at INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS opening_warning_resolution_key
                    ON opening_warning_resolution_audit(warning_key, audit_id);
                """
            )

    def claim_match_utility_purchase(
        self,
        *,
        signature: str,
        ticket_id: str,
        player_id: str,
        item_id: str,
        amount_tlama: int,
        maximum: int,
    ) -> int | None:
        """Atomically consume one finalized transaction for one active match."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ticket = connection.execute(
                """
                SELECT 1 FROM match_tickets
                WHERE ticket_id = ? AND player_id = ? AND status = 'simulated_active'
                """,
                (ticket_id, player_id),
            ).fetchone()
            if ticket is None:
                return None
            count = int(connection.execute(
                """
                SELECT COUNT(*) FROM utility_purchases
                WHERE ticket_id = ? AND item_id = ?
                """,
                (ticket_id, item_id),
            ).fetchone()[0])
            if count >= maximum:
                return None
            try:
                connection.execute(
                    """
                    INSERT INTO utility_purchases (
                        signature, ticket_id, player_id, item_id,
                        amount_tlama, purchased_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (signature, ticket_id, player_id, item_id, amount_tlama, int(time.time())),
                )
            except sqlite3.IntegrityError:
                return None
            return count + 1

    def record_opening_milestone(
        self,
        opening_version: str,
        milestone: str,
        *,
        occurred_at: datetime | None = None,
    ) -> None:
        """Increment one fixed aggregate without retaining a player/session identity."""

        columns = {
            "arcade_joined": "joined",
            "first_orb_collected": "first_orb",
            "level_2_reached": "level_2",
            "chicken_collision": "chicken_collision",
        }
        column = columns.get(milestone)
        if column is None:
            raise ValueError("Unsupported opening milestone.")
        if opening_version not in {"andean_v1", "andean_v2", "andean_v3"}:
            raise ValueError("Unsupported opening version.")
        current = occurred_at or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("Opening milestone timestamps must be timezone-aware.")
        month_utc = current.astimezone(timezone.utc).strftime("%Y-%m")
        with self._connect(timeout=0.05) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO opening_monthly_counts (month_utc, opening_version)
                VALUES (?, ?)
                ON CONFLICT(month_utc, opening_version) DO NOTHING
                """,
                (month_utc, opening_version),
            )
            connection.execute(
                f"""
                UPDATE opening_monthly_counts
                SET {column} = {column} + 1
                WHERE month_utc = ? AND opening_version = ?
                """,
                (month_utc, opening_version),
            )

    def opening_counts_for_months(self, months: tuple[str, str]) -> list[dict[str, object]]:
        placeholders = ",".join("?" for _ in months)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT month_utc, opening_version, joined, first_orb, level_2,
                       chicken_collision
                FROM opening_monthly_counts
                WHERE month_utc IN ({placeholders})
                ORDER BY opening_version, month_utc
                """,
                months,
            ).fetchall()
        return [dict(row) for row in rows]

    def claim_opening_warning(self, warning_key: str) -> bool:
        """Atomically claim an unsent warning across workers and processes."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM opening_warning_deliveries WHERE warning_key = ?",
                (warning_key,),
            ).fetchone():
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO opening_warning_claims (
                    warning_key, claimed_at
                ) VALUES (?, ?)
                """,
                (warning_key, int(time.time())),
            )
            return cursor.rowcount == 1

    def mark_opening_warning_delivered(self, warning_key: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO opening_warning_deliveries (
                    warning_key, delivered_at
                ) VALUES (?, ?)
                """,
                (warning_key, int(time.time())),
            )
            connection.execute(
                "DELETE FROM opening_warning_claims WHERE warning_key = ?",
                (warning_key,),
            )

    def release_opening_warning_claim(self, warning_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM opening_warning_claims WHERE warning_key = ?",
                (warning_key,),
            )

    def uncertain_opening_warnings(self) -> list[dict[str, object]]:
        """Return privacy-safe unresolved delivery claims for operator review."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT c.warning_key, c.claimed_at,
                       EXISTS (
                           SELECT 1
                           FROM opening_warning_resolution_audit a
                           WHERE a.warning_key = c.warning_key
                             AND a.action = 'request_resend'
                       ) AS resend_used
                FROM opening_warning_claims c
                LEFT JOIN opening_warning_deliveries d
                  ON d.warning_key = c.warning_key
                WHERE d.warning_key IS NULL
                ORDER BY c.claimed_at, c.warning_key
                """
            ).fetchall()
        return [
            {
                "warning_key": str(row["warning_key"]),
                "claimed_at": int(row["claimed_at"]),
                "resend_used": bool(row["resend_used"]),
            }
            for row in rows
        ]

    def confirm_uncertain_opening_warning_delivered(self, warning_key: str) -> bool:
        """Atomically resolve an uncertain claim and record the operator action."""

        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT 1 FROM opening_warning_claims
                WHERE warning_key = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM opening_warning_deliveries
                      WHERE warning_key = ?
                  )
                """,
                (warning_key, warning_key),
            ).fetchone()
            if claim is None:
                return False
            connection.execute(
                """
                INSERT INTO opening_warning_resolution_audit (
                    warning_key, action, outcome, recorded_at
                ) VALUES (?, 'confirm_delivered', 'accepted', ?)
                """,
                (warning_key, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO opening_warning_deliveries (
                    warning_key, delivered_at
                ) VALUES (?, ?)
                """,
                (warning_key, now),
            )
            connection.execute(
                "DELETE FROM opening_warning_claims WHERE warning_key = ?",
                (warning_key,),
            )
            return True

    def reserve_uncertain_opening_warning_resend(self, warning_key: str) -> bool:
        """Reserve the warning's sole operator resend before network I/O."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT 1 FROM opening_warning_claims
                WHERE warning_key = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM opening_warning_deliveries
                      WHERE warning_key = ?
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM opening_warning_resolution_audit
                      WHERE warning_key = ? AND action = 'request_resend'
                  )
                """,
                (warning_key, warning_key, warning_key),
            ).fetchone()
            if claim is None:
                return False
            connection.execute(
                """
                INSERT INTO opening_warning_resolution_audit (
                    warning_key, action, outcome, recorded_at
                ) VALUES (?, 'request_resend', 'accepted', ?)
                """,
                (warning_key, int(time.time())),
            )
            return True

    def record_uncertain_opening_warning_resend_result(
        self, warning_key: str, outcome: str
    ) -> None:
        if outcome not in {"delivered", "rejected", "unknown"}:
            raise ValueError("Unsupported opening warning resend outcome.")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            requested = connection.execute(
                """
                SELECT 1 FROM opening_warning_resolution_audit
                WHERE warning_key = ? AND action = 'request_resend'
                """,
                (warning_key,),
            ).fetchone()
            already_recorded = connection.execute(
                """
                SELECT 1 FROM opening_warning_resolution_audit
                WHERE warning_key = ? AND action = 'resend_result'
                """,
                (warning_key,),
            ).fetchone()
            if requested is None or already_recorded is not None:
                raise ValueError("Opening warning resend is not awaiting a result.")
            connection.execute(
                """
                INSERT INTO opening_warning_resolution_audit (
                    warning_key, action, outcome, recorded_at
                ) VALUES (?, 'resend_result', ?, ?)
                """,
                (warning_key, outcome, now),
            )
            if outcome == "delivered":
                connection.execute(
                    """
                    INSERT OR IGNORE INTO opening_warning_deliveries (
                        warning_key, delivered_at
                    ) VALUES (?, ?)
                    """,
                    (warning_key, now),
                )
                connection.execute(
                    "DELETE FROM opening_warning_claims WHERE warning_key = ?",
                    (warning_key,),
                )

    def _migrate_name_keyed_players(self, connection: sqlite3.Connection) -> None:
        """Preserve prototype data while replacing display-name identity."""

        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            BEGIN IMMEDIATE;
            DROP INDEX IF EXISTS match_tickets_player_id;
            DROP INDEX IF EXISTS players_high_score;
            ALTER TABLE players RENAME TO players_name_keyed;
            ALTER TABLE match_tickets RENAME TO match_tickets_name_keyed;

            CREATE TABLE players (
                player_id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL UNIQUE,
                    ownership_hash TEXT,
                name TEXT NOT NULL,
                high_score INTEGER NOT NULL DEFAULT 0 CHECK (high_score >= 0),
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            INSERT INTO players (
                player_id, identity_hash, ownership_hash, name, high_score,
                created_at, updated_at
            )
            SELECT
                player_id, 'legacy:' || player_id, NULL, name, high_score,
                created_at, updated_at
            FROM players_name_keyed;

            CREATE TABLE match_tickets (
                ticket_id TEXT PRIMARY KEY,
                player_id TEXT NOT NULL REFERENCES players(player_id),
                status TEXT NOT NULL CHECK (
                    status IN ('simulated_active', 'simulated_completed')
                ),
                entry_fee_tlama INTEGER NOT NULL CHECK (entry_fee_tlama >= 0),
                final_score INTEGER CHECK (final_score IS NULL OR final_score >= 0),
                issued_at INTEGER NOT NULL,
                completed_at INTEGER
            );
            INSERT INTO match_tickets
            SELECT * FROM match_tickets_name_keyed;

            DROP TABLE match_tickets_name_keyed;
            DROP TABLE players_name_keyed;
            COMMIT;
            """
        )
        connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _identity_hash(player_key: str) -> str:
        normalized = str(player_key or "").strip()
        if not normalized:
            raise ValueError("Player identity cannot be empty.")
        return sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _ownership_hash(ownership_token: str) -> str:
        normalized = str(ownership_token or "").strip()
        if len(normalized) < 32:
            raise ValueError("Player ownership proof is invalid.")
        return sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _owns(row: sqlite3.Row, ownership_hash: str) -> bool:
        saved = row["ownership_hash"]
        return saved is not None and hmac.compare_digest(str(saved), ownership_hash)

    def register_player(
        self, player_key: str, ownership_token: str, name: str
    ) -> StoredPlayer:
        name = " ".join(str(name or "").split())[:18] or "Llama"
        identity_hash = self._identity_hash(player_key)
        ownership_hash = self._ownership_hash(ownership_token)
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT player_id, ownership_hash, name, high_score,
                       equipped_skin_id
                FROM players
                WHERE identity_hash = ?
                """,
                (identity_hash,),
            ).fetchone()
            if row is None:
                player_id = f"llama-{uuid.uuid4().hex[:16]}"
                connection.execute(
                    """
                    INSERT INTO players (
                        player_id, identity_hash, ownership_hash, name, high_score,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (player_id, identity_hash, ownership_hash, name, now, now),
                )
                return StoredPlayer(
                    player_id=player_id,
                    name=name,
                    high_score=0,
                    equipped_skin_id="default",
                    owned_skin_ids=("default",),
                )

            if not self._owns(row, ownership_hash):
                raise PermissionError("Player identity ownership could not be verified.")
            connection.execute(
                "UPDATE players SET name = ?, updated_at = ? WHERE player_id = ?",
                (name, now, row["player_id"]),
            )
            return StoredPlayer(
                player_id=str(row["player_id"]),
                name=name,
                high_score=int(row["high_score"]),
                equipped_skin_id=str(row["equipped_skin_id"] or "default"),
                owned_skin_ids=self._owned_skin_ids(connection, str(row["player_id"])),
            )

    @staticmethod
    def _owned_skin_ids(
        connection: sqlite3.Connection, player_id: str
    ) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT skin_id FROM player_cosmetics
            WHERE player_id = ?
            ORDER BY acquired_at, skin_id
            """,
            (player_id,),
        ).fetchall()
        return ("default", *(str(row["skin_id"]) for row in rows))

    def equip_skin(
        self, player_id: str, ownership_token: str, skin_id: str
    ) -> tuple[str, ...]:
        ownership_hash = self._ownership_hash(ownership_token)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            player = connection.execute(
                "SELECT ownership_hash FROM players WHERE player_id = ?",
                (player_id,),
            ).fetchone()
            if player is None or not self._owns(player, ownership_hash):
                raise PermissionError("Player identity ownership could not be verified.")
            owned = self._owned_skin_ids(connection, player_id)
            if skin_id not in owned:
                raise PermissionError("The cosmetic is not owned by this player.")
            connection.execute(
                """
                UPDATE players
                SET equipped_skin_id = ?, updated_at = ?
                WHERE player_id = ?
                """,
                (skin_id, int(time.time()), player_id),
            )
            return owned

    def grant_confirmed_skin(
        self, player_id: str, ownership_token: str, skin_id: str
    ) -> tuple[str, ...]:
        """Future settlement hook: call only after server-side chain verification."""

        if skin_id not in CONFIRMED_COSMETIC_SKIN_IDS:
            raise ValueError("The cosmetic skin ID is not allowlisted.")
        ownership_hash = self._ownership_hash(ownership_token)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            player = connection.execute(
                "SELECT ownership_hash FROM players WHERE player_id = ?",
                (player_id,),
            ).fetchone()
            if player is None or not self._owns(player, ownership_hash):
                raise PermissionError("Player identity ownership could not be verified.")
            connection.execute(
                """
                INSERT OR IGNORE INTO player_cosmetics (
                    player_id, skin_id, source, acquired_at
                ) VALUES (?, ?, 'onchain_confirmed', ?)
                """,
                (player_id, skin_id, int(time.time())),
            )
            return self._owned_skin_ids(connection, player_id)

    def issue_match_ticket(
        self, player_id: str, ownership_token: str, *, entry_fee_tlama: int
    ) -> MatchTicket:
        ticket = MatchTicket(
            ticket_id=f"ticket-{uuid.uuid4().hex}",
            player_id=player_id,
            status="simulated_active",
            entry_fee_tlama=entry_fee_tlama,
        )
        with self._connect() as connection:
            ownership_hash = self._ownership_hash(ownership_token)
            owner = connection.execute(
                """
                SELECT ownership_hash FROM players
                WHERE player_id = ?
                """,
                (player_id,),
            ).fetchone()
            if owner is None or not self._owns(owner, ownership_hash):
                raise PermissionError("Player identity ownership could not be verified.")
            connection.execute(
                """
                INSERT INTO match_tickets (
                    ticket_id, player_id, status, entry_fee_tlama, issued_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    ticket.ticket_id,
                    ticket.player_id,
                    ticket.status,
                    ticket.entry_fee_tlama,
                    int(time.time()),
                ),
            )
        return ticket

    def record_score(
        self, player_id: str, ownership_token: str, score: int
    ) -> int:
        if score < 0:
            raise ValueError("Score cannot be negative.")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            ownership_hash = self._ownership_hash(ownership_token)
            updated = connection.execute(
                """
                UPDATE players
                SET high_score = MAX(high_score, ?), updated_at = ?
                WHERE player_id = ? AND ownership_hash = ?
                """,
                (score, int(time.time()), player_id, ownership_hash),
            )
            if updated.rowcount != 1:
                raise PermissionError("Player identity ownership could not be verified.")
            row = connection.execute(
                "SELECT high_score FROM players WHERE player_id = ?",
                (player_id,),
            ).fetchone()
            if row is None:
                raise LookupError("Player does not exist.")
            return int(row["high_score"])

    def complete_match_ticket(
        self,
        ticket_id: str,
        player_id: str,
        ownership_token: str,
        *,
        final_score: int,
    ) -> None:
        with self._connect() as connection:
            ownership_hash = self._ownership_hash(ownership_token)
            updated = connection.execute(
                """
                UPDATE match_tickets
                SET status = 'simulated_completed',
                    final_score = ?,
                    completed_at = COALESCE(completed_at, ?)
                WHERE ticket_id = ? AND player_id = ?
                  AND EXISTS (
                      SELECT 1 FROM players
                      WHERE players.player_id = match_tickets.player_id
                        AND players.ownership_hash = ?
                  )
                """,
                (
                    final_score,
                    int(time.time()),
                    ticket_id,
                    player_id,
                    ownership_hash,
                ),
            )
            if updated.rowcount != 1:
                raise PermissionError("Ticket ownership could not be verified.")

    def leaderboard(self, limit: int = 10) -> list[dict[str, object]]:
        safe_limit = max(1, min(100, limit))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT player_id, name, high_score
                FROM players
                ORDER BY high_score DESC, name COLLATE NOCASE, player_id
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        return [
            {
                "player_id": str(row["player_id"]),
                "name": str(row["name"]),
                "score": int(row["high_score"]),
            }
            for row in rows
        ]

    def summary(self) -> dict[str, int]:
        with self._connect() as connection:
            player_count = connection.execute(
                "SELECT COUNT(*) FROM players"
            ).fetchone()[0]
            ticket_count = connection.execute(
                "SELECT COUNT(*) FROM match_tickets"
            ).fetchone()[0]
        return {"saved_players": int(player_count), "saved_match_tickets": int(ticket_count)}
