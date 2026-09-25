"""24/7 TrustLlama Telegram announcement stream.

The service is intentionally isolated from all trading code. It only sends
static, factual community announcements through telebot when live sending is
explicitly enabled. The default mode is dry-run and makes no network calls.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Callable, Final, Protocol

try:
    import telebot
except ImportError:  # pragma: no cover - exercised in dependency-free dry runs
    telebot = None  # type: ignore[assignment]


LOG = logging.getLogger("trustllama.telegram_announcer")
ANNOUNCEMENTS: Final[tuple[str, ...]] = (
    (
        "<b>TrustLlama / TLAMA</b>\n\n"
        "Finalized public tokenomics model:\n"
        "• Fixed supply: <b>1,000,000,000 TLAMA</b>\n"
        "• Developer allocation: <b>1%</b> / 10,000,000 TLAMA\n"
        "• Automated Web3 game payout pool: <b>10%</b> / 100,000,000 TLAMA\n"
        "• Public liquidity pool: <b>89%</b> / 890,000,000 TLAMA\n"
        "• Approved buy tax: <b>0.5%</b> | sell tax: <b>1.0%</b>\n"
        "• Trade cap: <b>0.5%</b> / 5,000,000 TLAMA\n"
        "• Wallet cap: <b>1.0%</b> / 10,000,000 TLAMA\n\n"
        "Pre-launch only: not deployed or traded. On-chain rules require "
        "independent verification."
    ),
    (
        "<b>The TLAMA allocation is fully visible.</b>\n\n"
        "<b>1%</b> developer allocation + <b>10%</b> automated Web3 game payout "
        "pool + <b>89%</b> public liquidity pool. LP tokens are intended to be "
        "burned after a verified launch.\n\n"
        "The published limits are a <b>0.5%</b> buy tax, <b>1.0%</b> sell tax, "
        "<b>0.5%</b> maximum trade, and <b>1.0%</b> maximum wallet holding. "
        "These are pre-launch design rules, not deployed-chain claims."
    ),
    (
        "<b>Build in public. Keep the rules visible.</b>\n\n"
        "TrustLlama's finalized public model is <b>1B fixed</b>: "
        "<b>1% developer</b>, <b>10% automated game payouts</b>, and "
        "<b>89% public liquidity</b> with LP tokens intended for burning.\n\n"
        "Published swap rules: buy <b>0.5%</b>, sell <b>1.0%</b>, trade cap "
        "<b>0.5%</b>, wallet cap <b>1.0%</b>. Status: offline, not deployed, "
        "not traded."
    ),
)


class AnnouncerConfigError(ValueError):
    """Raised when the announcer configuration is unsafe or incomplete."""


class TelegramSender(Protocol):
    """Small telebot-compatible interface used by the retry boundary."""

    def send_message(self, channel_id: str, message: str, *, parse_mode: str) -> object:
        ...


@dataclass(frozen=True)
class AnnouncerConfig:
    """Validated runtime settings for the isolated announcement process."""

    bot_token: str
    channel_id: str
    live_send: bool
    interval_seconds: float
    max_retries: int
    retry_base_seconds: float

    @classmethod
    def from_environment(cls) -> "AnnouncerConfig":
        live_send = _read_bool(
            "TELEGRAM_SERVICES_ENABLED", default=False
        ) and _read_bool("ANNOUNCER_LIVE_SEND", default=False)
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "").strip()
        if live_send and not bot_token:
            raise AnnouncerConfigError(
                "TELEGRAM_BOT_TOKEN is required when live sending is enabled."
            )
        if live_send and not channel_id:
            raise AnnouncerConfigError(
                "TELEGRAM_CHANNEL_ID is required when live sending is enabled."
            )
        if live_send and telebot is None:
            raise AnnouncerConfigError(
                "pyTelegramBotAPI is required when live sending is enabled."
            )

        return cls(
            bot_token=bot_token,
            channel_id=channel_id,
            live_send=live_send,
            interval_seconds=_read_float(
                "ANNOUNCER_INTERVAL_SECONDS", default=14_400, minimum=60
            ),
            max_retries=_read_int(
                "ANNOUNCER_MAX_RETRIES", default=5, minimum=0, maximum=10
            ),
            retry_base_seconds=_read_float(
                "ANNOUNCER_RETRY_BASE_SECONDS", default=5, minimum=1, maximum=300
            ),
        )


def _read_bool(name: str, *, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise AnnouncerConfigError(f"{name} must be true or false.")


def _read_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AnnouncerConfigError(f"{name} must be an integer.") from exc
    if parsed < minimum or parsed > maximum:
        raise AnnouncerConfigError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def _read_float(
    name: str, *, default: float, minimum: float, maximum: float | None = None
) -> float:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise AnnouncerConfigError(f"{name} must be a number.") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        upper = f" and at most {maximum:g}" if maximum is not None else ""
        raise AnnouncerConfigError(f"{name} must be at least {minimum:g}{upper}.")
    return parsed


def _redacted_token_state(config: AnnouncerConfig) -> str:
    if not config.live_send:
        return "dry_run"
    return "configured"


def send_with_retries(
    bot: TelegramSender | None,
    config: AnnouncerConfig,
    message: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Send one message, retrying bounded transient failures."""

    if not config.live_send:
        LOG.info("announcement_dry_run message=%r", message)
        return True

    if bot is None:
        raise AnnouncerConfigError("Live sending requires an initialized telebot client.")

    for attempt in range(config.max_retries + 1):
        try:
            bot.send_message(config.channel_id, message, parse_mode="HTML")
            LOG.info(
                "announcement_sent channel=%s attempt=%d",
                config.channel_id,
                attempt + 1,
            )
            return True
        except Exception as exc:  # telebot exposes provider-specific exceptions
            if attempt >= config.max_retries:
                LOG.exception(
                    "announcement_failed channel=%s attempts=%d error=%s",
                    config.channel_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                return False
            delay = min(config.retry_base_seconds * (2**attempt), 300)
            LOG.warning(
                "announcement_retry channel=%s attempt=%d retry_in_seconds=%s error=%s",
                config.channel_id,
                attempt + 1,
                delay,
                type(exc).__name__,
            )
            sleep(delay)
    return False


def run_forever(
    config: AnnouncerConfig,
    *,
    sleep: Callable[[float], None] = time.sleep,
    random_source: object = random,
) -> None:
    """Run the rotating stream until the process receives a termination signal."""

    bot = telebot.TeleBot(config.bot_token) if config.live_send and telebot else None
    LOG.info(
        "announcer_started mode=%s interval_seconds=%s token=%s",
        _redacted_token_state(config),
        config.interval_seconds,
        _redacted_token_state(config),
    )
    index = 0
    while True:
        message = ANNOUNCEMENTS[index % len(ANNOUNCEMENTS)]
        send_with_retries(bot, config, message, sleep=sleep)
        index += 1
        jitter = getattr(random_source, "uniform")(
            0, min(config.interval_seconds * 0.1, 300)
        )
        sleep(config.interval_seconds + jitter)


def main() -> None:
    """Validate settings and start the long-running announcement loop."""

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        config = AnnouncerConfig.from_environment()
        run_forever(config)
    except AnnouncerConfigError as exc:
        LOG.error("announcer_configuration_error error=%s", exc)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()