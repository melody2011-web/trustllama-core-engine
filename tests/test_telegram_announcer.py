from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from telegram_announcer.announcer import (
    ANNOUNCEMENTS,
    AnnouncerConfig,
    AnnouncerConfigError,
    send_with_retries,
)


class _RetryingBot:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def send_message(
        self, channel_id: str, message: str, *, parse_mode: str
    ) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("temporary Telegram failure")
        return object()


class TelegramAnnouncerTests(unittest.TestCase):
    def test_default_configuration_is_offline_and_needs_no_credentials(self) -> None:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key
            not in {
                "TELEGRAM_SERVICES_ENABLED",
                "ANNOUNCER_LIVE_SEND",
                "TELEGRAM_BOT_TOKEN",
                "TELEGRAM_CHANNEL_ID",
            }
        }
        with patch.dict(os.environ, environment, clear=True):
            config = AnnouncerConfig.from_environment()

        self.assertFalse(config.live_send)
        self.assertEqual(config.bot_token, "")
        self.assertEqual(config.channel_id, "")
        self.assertTrue(send_with_retries(None, config, ANNOUNCEMENTS[0]))

    def test_live_mode_rejects_missing_credentials(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_SERVICES_ENABLED": "true",
                "ANNOUNCER_LIVE_SEND": "true",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHANNEL_ID": "",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                AnnouncerConfigError, "TELEGRAM_BOT_TOKEN"
            ):
                AnnouncerConfig.from_environment()

    def test_master_switch_keeps_announcer_offline(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TELEGRAM_SERVICES_ENABLED": "false",
                "ANNOUNCER_LIVE_SEND": "true",
                "TELEGRAM_BOT_TOKEN": "",
                "TELEGRAM_CHANNEL_ID": "",
            },
            clear=True,
        ):
            config = AnnouncerConfig.from_environment()

        self.assertFalse(config.live_send)

    def test_transient_failures_use_bounded_exponential_backoff(self) -> None:
        config = AnnouncerConfig(
            bot_token="not-a-real-token",
            channel_id="@example",
            live_send=True,
            interval_seconds=60,
            max_retries=2,
            retry_base_seconds=1,
        )
        bot = _RetryingBot(failures=2)
        sleeps: list[float] = []

        result = send_with_retries(bot, config, "test", sleep=sleeps.append)

        self.assertTrue(result)
        self.assertEqual(bot.calls, 3)
        self.assertEqual(sleeps, [1, 2])


if __name__ == "__main__":
    unittest.main()