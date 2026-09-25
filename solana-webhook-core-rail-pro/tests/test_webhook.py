"""Tests for standalone webhook delivery validation."""

from __future__ import annotations

import math
import sys
import unittest
from unittest.mock import patch

from solana_webhook_core_rail_pro.retry import MAX_RETRY_DELAY_SECONDS
from solana_webhook_core_rail_pro.webhook import WebhookDelivery


class WebhookDeliveryTests(unittest.TestCase):
    def test_rejects_invalid_retry_counts_before_session_creation(self) -> None:
        for max_attempts in (0, -1, 1.5, math.nan, math.inf, -math.inf, True, "3"):
            with self.subTest(max_attempts=max_attempts):
                with patch(
                    "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
                ) as session:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"max_attempts must be a positive integer",
                    ):
                        WebhookDelivery(
                            url="https://webhook.example.test/events",
                            secret="secret",
                            max_attempts=max_attempts,
                        )

                session.assert_not_called()

    def test_accepts_smallest_valid_retry_count(self) -> None:
        with patch(
            "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
        ) as session:
            delivery = WebhookDelivery(
                url="https://webhook.example.test/events",
                secret="secret",
                max_attempts=1,
            )

        self.assertEqual(delivery._max_attempts, 1)
        session.assert_not_called()

    def test_rejects_non_positive_or_non_finite_timeout_before_session_creation(
        self,
    ) -> None:
        for timeout in (0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(timeout=timeout):
                with patch(
                    "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
                ) as session:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"timeout_seconds must be finite and greater than zero",
                    ):
                        WebhookDelivery(
                            url="https://webhook.example.test/events",
                            secret="secret",
                            timeout_seconds=timeout,
                        )

                session.assert_not_called()

    def test_rejects_non_positive_or_non_finite_backoff_before_session_creation(
        self,
    ) -> None:
        for backoff in (0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(backoff=backoff):
                with patch(
                    "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
                ) as session:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"base_backoff_seconds must be finite and greater than zero",
                    ):
                        WebhookDelivery(
                            url="https://webhook.example.test/events",
                            secret="secret",
                            base_backoff_seconds=backoff,
                        )

                session.assert_not_called()

    def test_rejects_retry_schedule_that_overflows_before_session_creation(
        self,
    ) -> None:
        for base_backoff, max_attempts in (
            (sys.float_info.max, 3),
            (1.0, 1026),
        ):
            with self.subTest(
                base_backoff=base_backoff,
                max_attempts=max_attempts,
            ):
                with patch(
                    "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
                ) as session, self.assertRaisesRegex(
                    ValueError,
                    r"retry backoff schedule must remain finite",
                ):
                    WebhookDelivery(
                        url="https://webhook.example.test/events",
                        secret="secret",
                        max_attempts=max_attempts,
                        base_backoff_seconds=base_backoff,
                    )

                session.assert_not_called()

    def test_rejects_finite_retry_delay_above_practical_limit(self) -> None:
        with patch(
            "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
        ) as session, self.assertRaisesRegex(
            ValueError,
            rf"retry backoff delay must not exceed {MAX_RETRY_DELAY_SECONDS} seconds",
        ):
            WebhookDelivery(
                url="https://webhook.example.test/events",
                secret="secret",
                max_attempts=1025,
                base_backoff_seconds=1.0,
            )

        session.assert_not_called()

    def test_rejects_retry_delay_above_practical_limit(self) -> None:
        with patch(
            "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
        ) as session, self.assertRaisesRegex(
            ValueError,
            rf"retry backoff delay must not exceed {MAX_RETRY_DELAY_SECONDS} seconds",
        ):
            WebhookDelivery(
                url="https://webhook.example.test/events",
                secret="secret",
                max_attempts=2,
                base_backoff_seconds=MAX_RETRY_DELAY_SECONDS + 1,
            )

        session.assert_not_called()

    def test_accepts_practical_retry_delay_boundary(self) -> None:
        with patch(
            "solana_webhook_core_rail_pro.webhook.aiohttp.ClientSession"
        ) as session:
            delivery = WebhookDelivery(
                url="https://webhook.example.test/events",
                secret="secret",
                max_attempts=2,
                base_backoff_seconds=MAX_RETRY_DELAY_SECONDS,
            )

        self.assertEqual(delivery._base_backoff_seconds, MAX_RETRY_DELAY_SECONDS)
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()