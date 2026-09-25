from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

GAME_DIR = Path(__file__).parents[1] / "llama_website" / "llama_game"
sys.path.insert(0, str(GAME_DIR))

from opening_warnings import (  # noqa: E402
    evaluate_regressions,
    last_two_completed_utc_months,
    opening_sample_rows,
    run_check,
    resend_uncertain_warning,
    warning_message,
)
from storage import ArcadeStorage  # noqa: E402


class OpeningRegressionWarningTests(unittest.TestCase):
    def test_selects_last_two_completed_utc_months_across_year_boundary(self) -> None:
        months = last_two_completed_utc_months(
            datetime(2026, 1, 8, 12, tzinfo=timezone.utc)
        )
        self.assertEqual(months, ("2025-11", "2025-12"))

    def test_thresholds_are_independent_and_chicken_is_context_only(self) -> None:
        rows = [
            {
                "month_utc": "2026-06",
                "opening_version": "andean_v1",
                "joined": 100,
                "first_orb": 80,
                "level_2": 60,
                "chicken_collision": 5,
            },
            {
                "month_utc": "2026-07",
                "opening_version": "andean_v1",
                "joined": 100,
                "first_orb": 70,
                "level_2": 51,
                "chicken_collision": 99,
            },
        ]
        regressions = evaluate_regressions(rows, ("2026-06", "2026-07"))

        self.assertEqual([item.metric for item in regressions], ["join_to_first_orb"])
        self.assertIn("99.0%", warning_message(regressions[0]))
        self.assertIn("Context only", warning_message(regressions[0]))

    def test_both_months_must_meet_fixed_minimum_sample(self) -> None:
        rows = [
            {
                "month_utc": month,
                "opening_version": "andean_v1",
                "joined": joined,
                "first_orb": first_orb,
                "level_2": first_orb,
                "chicken_collision": 0,
            }
            for month, joined, first_orb in (
                ("2026-06", 49, 40),
                ("2026-07", 100, 1),
            )
        ]
        self.assertEqual(evaluate_regressions(rows, ("2026-06", "2026-07")), [])

    def test_operator_rows_label_sparse_and_eligible_months(self) -> None:
        rows = [
            {
                "month_utc": "2026-06",
                "opening_version": "andean_v1",
                "joined": 49,
                "first_orb": 40,
                "level_2": 30,
                "chicken_collision": 4,
            },
            {
                "month_utc": "2026-07",
                "opening_version": "andean_v1",
                "joined": 50,
                "first_orb": 39,
                "level_2": 29,
                "chicken_collision": 5,
            },
        ]

        operator_rows = opening_sample_rows(rows, ("2026-06", "2026-07"))

        self.assertEqual(
            operator_rows,
            [
                {
                    "Opening version": "andean_v1",
                    "Month (UTC)": "2026-06",
                    "Joined": 49,
                    "First orb": 40,
                    "Level 2": 30,
                    "Chicken context": 4,
                    "Sample status": "Insufficient sample (49/50 joined)",
                },
                {
                    "Opening version": "andean_v1",
                    "Month (UTC)": "2026-07",
                    "Joined": 50,
                    "First orb": 39,
                    "Level 2": 29,
                    "Chicken context": 5,
                    "Sample status": "Eligible sample (50/50 joined)",
                },
            ],
        )

    def test_operator_rows_fill_a_missing_completed_month_as_sparse(self) -> None:
        rows = [
            {
                "month_utc": "2026-07",
                "opening_version": "andean_v1",
                "joined": 3,
                "first_orb": 2,
                "level_2": 1,
                "chicken_collision": 1,
            }
        ]

        operator_rows = opening_sample_rows(rows, ("2026-06", "2026-07"))

        self.assertEqual(operator_rows[0]["Joined"], 0)
        self.assertEqual(
            operator_rows[0]["Sample status"],
            "Insufficient sample (0/50 joined)",
        )

    def test_operator_rows_show_supported_version_before_samples_arrive(self) -> None:
        operator_rows = opening_sample_rows([], ("2026-06", "2026-07"))

        self.assertEqual(len(operator_rows), 2)
        self.assertEqual(
            {row["Opening version"] for row in operator_rows},
            {"andean_v1"},
        )
        self.assertTrue(
            all(
                row["Sample status"] == "Insufficient sample (0/50 joined)"
                for row in operator_rows
            )
        )

    def test_successful_warning_is_deduplicated_but_failure_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            for month, first_orb, level_2, chicken in (
                (datetime(2026, 6, 1, tzinfo=timezone.utc), 40, 35, 10),
                (datetime(2026, 7, 1, tzinfo=timezone.utc), 30, 20, 25),
            ):
                for _ in range(50):
                    storage.record_opening_milestone(
                        "andean_v1", "arcade_joined", occurred_at=month
                    )
                for _ in range(first_orb):
                    storage.record_opening_milestone(
                        "andean_v1", "first_orb_collected", occurred_at=month
                    )
                for _ in range(level_2):
                    storage.record_opening_milestone(
                        "andean_v1", "level_2_reached", occurred_at=month
                    )
                for _ in range(chicken):
                    storage.record_opening_milestone(
                        "andean_v1", "chicken_collision", occurred_at=month
                    )

            attempts: list[str] = []

            def fail(message: str) -> str:
                attempts.append(message)
                return "rejected"

            now = datetime(2026, 8, 9, tzinfo=timezone.utc)
            regressions = run_check(storage, now=now, send=fail)
            self.assertEqual(len(regressions), 2)
            self.assertEqual(len(attempts), 2)

            delivered: list[str] = []
            run_check(
                storage,
                now=now,
                send=lambda message: delivered.append(message) or "delivered",
            )
            self.assertEqual(len(delivered), 2)

            run_check(
                storage,
                now=now,
                send=lambda message: delivered.append(message) or "delivered",
            )
            self.assertEqual(len(delivered), 2)

    def test_concurrent_checks_claim_each_warning_before_sending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            for month, first_orb in (
                (datetime(2026, 6, 1, tzinfo=timezone.utc), 40),
                (datetime(2026, 7, 1, tzinfo=timezone.utc), 30),
            ):
                for _ in range(50):
                    storage.record_opening_milestone(
                        "andean_v1", "arcade_joined", occurred_at=month
                    )
                for _ in range(first_orb):
                    storage.record_opening_milestone(
                        "andean_v1", "first_orb_collected", occurred_at=month
                    )

            sent: list[str] = []
            barrier = threading.Barrier(2)

            def sender(message: str) -> str:
                sent.append(message)
                return "delivered"

            def check() -> None:
                barrier.wait()
                run_check(
                    storage,
                    now=datetime(2026, 8, 9, tzinfo=timezone.utc),
                    send=sender,
                )

            threads = [threading.Thread(target=check) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(sent), 1)

    def test_unknown_delivery_is_not_automatically_resent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            for month, first_orb in (
                (datetime(2026, 6, 1, tzinfo=timezone.utc), 40),
                (datetime(2026, 7, 1, tzinfo=timezone.utc), 30),
            ):
                for _ in range(50):
                    storage.record_opening_milestone(
                        "andean_v1", "arcade_joined", occurred_at=month
                    )
                for _ in range(first_orb):
                    storage.record_opening_milestone(
                        "andean_v1", "first_orb_collected", occurred_at=month
                    )
            attempts: list[str] = []
            now = datetime(2026, 8, 9, tzinfo=timezone.utc)

            run_check(
                storage,
                now=now,
                send=lambda message: attempts.append(message) or "unknown",
            )
            run_check(
                storage,
                now=now,
                send=lambda message: attempts.append(message) or "delivered",
            )

            self.assertEqual(len(attempts), 1)

    def test_operator_can_confirm_uncertain_delivery_with_durable_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            self.assertTrue(storage.claim_opening_warning("2026-07:andean_v1:join_to_first_orb"))

            self.assertTrue(
                storage.confirm_uncertain_opening_warning_delivered(
                    "2026-07:andean_v1:join_to_first_orb"
                )
            )
            self.assertFalse(
                storage.confirm_uncertain_opening_warning_delivered(
                    "2026-07:andean_v1:join_to_first_orb"
                )
            )
            self.assertEqual(storage.uncertain_opening_warnings(), [])

    def test_operator_resend_is_limited_to_one_and_remains_uncertain_on_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            for month, first_orb in (
                (datetime(2026, 6, 1, tzinfo=timezone.utc), 40),
                (datetime(2026, 7, 1, tzinfo=timezone.utc), 30),
            ):
                for _ in range(50):
                    storage.record_opening_milestone(
                        "andean_v1", "arcade_joined", occurred_at=month
                    )
                for _ in range(first_orb):
                    storage.record_opening_milestone(
                        "andean_v1", "first_orb_collected", occurred_at=month
                    )
            key = "2026-07:andean_v1:join_to_first_orb"
            self.assertTrue(storage.claim_opening_warning(key))
            attempts: list[str] = []

            self.assertEqual(
                resend_uncertain_warning(
                    storage,
                    key,
                    send=lambda message: attempts.append(message) or "unknown",
                ),
                "unknown",
            )
            self.assertIsNone(
                resend_uncertain_warning(
                    storage,
                    key,
                    send=lambda message: attempts.append(message) or "delivered",
                )
            )
            self.assertEqual(len(attempts), 1)
            self.assertTrue(storage.uncertain_opening_warnings()[0]["resend_used"])

    def test_storage_retains_aggregates_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            occurred_at = datetime(2026, 7, 4, 23, tzinfo=timezone.utc)
            storage.record_opening_milestone(
                "andean_v1", "arcade_joined", occurred_at=occurred_at
            )

            rows = storage.opening_counts_for_months(("2026-06", "2026-07"))
            self.assertEqual(
                rows,
                [
                    {
                        "month_utc": "2026-07",
                        "opening_version": "andean_v1",
                        "joined": 1,
                        "first_orb": 0,
                        "level_2": 0,
                        "chicken_collision": 0,
                    }
                ],
            )


if __name__ == "__main__":
    unittest.main()
