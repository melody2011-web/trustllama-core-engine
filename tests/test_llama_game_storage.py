from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


GAME_DIR = Path(__file__).resolve().parents[1] / "llama_website" / "llama_game"
sys.path.insert(0, str(GAME_DIR))

from storage import ArcadeStorage  # noqa: E402


class ArcadeStorageTests(unittest.TestCase):
    OWNER_ONE = "owner-one-proof-that-is-at-least-32-characters"
    OWNER_TWO = "owner-two-proof-that-is-at-least-32-characters"

    def test_cosmetic_ownership_and_equipped_skin_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "arcade.sqlite3"
            storage = ArcadeStorage(database_path)
            player = storage.register_player(
                "cosmetic-browser", self.OWNER_ONE, "Styled Llama"
            )
            self.assertEqual(player.owned_skin_ids, ("default",))
            self.assertEqual(player.equipped_skin_id, "default")

            owned = storage.grant_confirmed_skin(
                player.player_id, self.OWNER_ONE, "aurora"
            )
            self.assertEqual(owned, ("default", "aurora"))
            storage.equip_skin(player.player_id, self.OWNER_ONE, "aurora")

            reopened = ArcadeStorage(database_path).register_player(
                "cosmetic-browser", self.OWNER_ONE, "Styled Llama"
            )
            self.assertEqual(reopened.owned_skin_ids, ("default", "aurora"))
            self.assertEqual(reopened.equipped_skin_id, "aurora")

    def test_unowned_or_wrong_identity_cannot_equip_cosmetic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            player = storage.register_player(
                "cosmetic-browser", self.OWNER_ONE, "Styled Llama"
            )
            with self.assertRaises(PermissionError):
                storage.equip_skin(player.player_id, self.OWNER_ONE, "midnight")
            with self.assertRaises(PermissionError):
                storage.equip_skin(player.player_id, self.OWNER_TWO, "default")
            with self.assertRaises(ValueError):
                storage.grant_confirmed_skin(
                    player.player_id, self.OWNER_ONE, "injected-skin"
                )

    def test_existing_player_schema_migrates_with_default_cosmetic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "arcade.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE players (
                        player_id TEXT PRIMARY KEY,
                        identity_hash TEXT NOT NULL UNIQUE,
                        ownership_hash TEXT,
                        name TEXT NOT NULL,
                        high_score INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    )
                    """
                )
            ArcadeStorage(database_path)
            with sqlite3.connect(database_path) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(players)")
                }
                cosmetic_table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name = 'player_cosmetics'
                    """
                ).fetchone()
            self.assertIn("equipped_skin_id", columns)
            self.assertEqual(cosmetic_table, ("player_cosmetics",))

    def test_player_ticket_and_high_score_survive_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "arcade.sqlite3"
            storage = ArcadeStorage(database_path)
            player = storage.register_player("browser-one", self.OWNER_ONE, "  Moon   Llama ")
            ticket = storage.issue_match_ticket(
                player.player_id, self.OWNER_ONE, entry_fee_tlama=25
            )

            self.assertEqual(storage.record_score(player.player_id, self.OWNER_ONE, 40), 40)
            self.assertEqual(storage.record_score(player.player_id, self.OWNER_ONE, 20), 40)
            storage.complete_match_ticket(
                ticket.ticket_id, player.player_id, self.OWNER_ONE, final_score=40
            )

            reopened = ArcadeStorage(database_path)
            same_player = reopened.register_player(
                "browser-one", self.OWNER_ONE, "moon llama"
            )
            self.assertEqual(same_player.player_id, player.player_id)
            self.assertEqual(same_player.high_score, 40)
            self.assertEqual(reopened.leaderboard()[0]["score"], 40)
            self.assertEqual(
                reopened.summary(),
                {"saved_players": 1, "saved_match_tickets": 1},
            )

            with sqlite3.connect(database_path) as connection:
                saved_ticket = connection.execute(
                    """
                    SELECT status, entry_fee_tlama, final_score
                    FROM match_tickets
                    WHERE ticket_id = ?
                    """,
                    (ticket.ticket_id,),
                ).fetchone()
                journal_mode = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]

            self.assertEqual(saved_ticket, ("simulated_completed", 25, 40))
            self.assertEqual(journal_mode, "wal")

    def test_score_rejects_negative_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            player = storage.register_player("browser-safe", self.OWNER_ONE, "Safe Llama")

            with self.assertRaisesRegex(ValueError, "negative"):
                storage.record_score(player.player_id, self.OWNER_ONE, -1)

    def test_same_display_name_creates_separate_players_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")

            first = storage.register_player("browser-one", self.OWNER_ONE, "Twin Llama")
            second = storage.register_player("browser-two", self.OWNER_TWO, "Twin Llama")

            self.assertNotEqual(first.player_id, second.player_id)
            self.assertEqual(storage.summary()["saved_players"], 2)

    def test_same_identity_can_change_its_display_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")

            first = storage.register_player("browser-one", self.OWNER_ONE, "Old Name")
            renamed = storage.register_player("browser-one", self.OWNER_ONE, "New Name")

            self.assertEqual(first.player_id, renamed.player_id)
            self.assertEqual(renamed.name, "New Name")
            self.assertEqual(storage.summary()["saved_players"], 1)

    def test_name_keyed_database_migrates_without_losing_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "arcade.sqlite3"
            with sqlite3.connect(database_path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE players (
                        player_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        name_key TEXT NOT NULL UNIQUE,
                        high_score INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE match_tickets (
                        ticket_id TEXT PRIMARY KEY,
                        player_id TEXT NOT NULL REFERENCES players(player_id),
                        status TEXT NOT NULL,
                        entry_fee_tlama INTEGER NOT NULL,
                        final_score INTEGER,
                        issued_at INTEGER NOT NULL,
                        completed_at INTEGER
                    );
                    INSERT INTO players VALUES (
                        'llama-legacy', 'Legacy Llama', 'legacy llama', 70, 1, 2
                    );
                    INSERT INTO match_tickets VALUES (
                        'ticket-legacy', 'llama-legacy',
                        'simulated_completed', 25, 70, 1, 2
                    );
                    """
                )

            storage = ArcadeStorage(database_path)

            self.assertEqual(storage.leaderboard()[0]["score"], 70)
            self.assertEqual(
                storage.summary(),
                {"saved_players": 1, "saved_match_tickets": 1},
            )
            new_player = storage.register_player(
                "new-browser", self.OWNER_ONE, "Legacy Llama"
            )
            self.assertNotEqual(new_player.player_id, "llama-legacy")
            self.assertEqual(storage.summary()["saved_players"], 2)

            with sqlite3.connect(database_path) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(players)"
                    ).fetchall()
                }
                saved_ticket = connection.execute(
                    """
                    SELECT player_id, status, final_score
                    FROM match_tickets
                    WHERE ticket_id = 'ticket-legacy'
                    """
                ).fetchone()

            self.assertIn("identity_hash", columns)
            self.assertIn("ownership_hash", columns)
            self.assertNotIn("name_key", columns)
            self.assertEqual(
                saved_ticket,
                ("llama-legacy", "simulated_completed", 70),
            )

    def test_copied_identity_cannot_read_or_mutate_owner_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            player = storage.register_player(
                "browser-one", self.OWNER_ONE, "Owner"
            )
            ticket = storage.issue_match_ticket(
                player.player_id, self.OWNER_ONE, entry_fee_tlama=25
            )
            storage.record_score(player.player_id, self.OWNER_ONE, 40)

            with self.assertRaises(PermissionError):
                storage.register_player("browser-one", self.OWNER_TWO, "Attacker")
            with self.assertRaises(PermissionError):
                storage.record_score(player.player_id, self.OWNER_TWO, 999)
            with self.assertRaises(PermissionError):
                storage.issue_match_ticket(
                    player.player_id, self.OWNER_TWO, entry_fee_tlama=25
                )
            with self.assertRaises(PermissionError):
                storage.complete_match_ticket(
                    ticket.ticket_id,
                    player.player_id,
                    self.OWNER_TWO,
                    final_score=999,
                )

            same_player = storage.register_player(
                "browser-one", self.OWNER_ONE, "Owner"
            )
            self.assertEqual(same_player.high_score, 40)

    def test_unowned_legacy_identity_fails_closed_and_cannot_be_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "arcade.sqlite3"
            storage = ArcadeStorage(database_path)
            player = storage.register_player(
                "browser-one", self.OWNER_ONE, "Owner"
            )
            ticket = storage.issue_match_ticket(
                player.player_id, self.OWNER_ONE, entry_fee_tlama=25
            )
            storage.record_score(player.player_id, self.OWNER_ONE, 40)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "UPDATE players SET ownership_hash = NULL WHERE player_id = ?",
                    (player.player_id,),
                )

            with self.assertRaises(PermissionError):
                storage.register_player(
                    "browser-one", self.OWNER_TWO, "Attacker"
                )
            with self.assertRaises(PermissionError):
                storage.record_score(player.player_id, self.OWNER_TWO, 999)
            with self.assertRaises(PermissionError):
                storage.issue_match_ticket(
                    player.player_id, self.OWNER_TWO, entry_fee_tlama=25
                )
            with self.assertRaises(PermissionError):
                storage.complete_match_ticket(
                    ticket.ticket_id,
                    player.player_id,
                    self.OWNER_TWO,
                    final_score=999,
                )

            fresh = storage.register_player(
                "fresh-browser", self.OWNER_TWO, "Owner"
            )
            self.assertNotEqual(fresh.player_id, player.player_id)
            self.assertEqual(fresh.high_score, 0)


if __name__ == "__main__":
    unittest.main()
