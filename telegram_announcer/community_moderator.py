"""TrustLlama community protection bot.

This file is intentionally independent from the announcement bot and all
trading code. It uses python-telegram-bot, keeps moderation state in memory,
and refuses to initialize a Telegram client unless COMMUNITY_MODERATOR_LIVE is
explicitly enabled.

Moderation behavior:
* New members are muted until they solve a button-based captcha.
* Unknown text links are removed unless their domain is allowlisted.
* New members keep media permissions disabled for a configurable hold period
  after captcha completion. Telegram does not expose account creation dates to
  bots, so "brand new" is represented by this post-join safety window.
* An automatic mute-on-attack flag and admin-only /mutechat and /unmutechat
  commands change the chat's default member permissions.

No wallet, RPC, blockchain, or trading module is imported here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Any

try:
    from telegram import (
        ChatPermissions,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        Update,
    )
    from telegram.error import InvalidToken
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CallbackQueryHandler,
        ChatMemberHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    PTB_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised before optional install
    PTB_AVAILABLE = False
    Application = Any  # type: ignore[assignment,misc]
    ApplicationBuilder = Any  # type: ignore[assignment,misc]
    CallbackQueryHandler = Any  # type: ignore[assignment,misc]
    ChatMemberHandler = Any  # type: ignore[assignment,misc]
    CommandHandler = Any  # type: ignore[assignment,misc]
    ContextTypes = Any  # type: ignore[assignment,misc]
    MessageHandler = Any  # type: ignore[assignment,misc]
    Update = Any  # type: ignore[assignment,misc]
    filters = Any  # type: ignore[assignment,misc]
    InvalidToken = RuntimeError  # type: ignore[assignment,misc]


LOG = logging.getLogger("trustllama.community_moderator")
URL_PATTERN = re.compile(
    r"(?ix)(?:\bhttps?://[^\s<>()]+|\bwww\.[^\s<>()]+|"
    r"(?<![\w.])(?:t\.me|telegram\.me|discord\.gg)/[^\s<>()]+)"
)
BOT_TOKEN_PATTERN = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,80}$")
MEDIA_ATTRIBUTES = (
    "photo",
    "video",
    "animation",
    "document",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "contact",
    "location",
    "venue",
)


class _SecretRedactionFilter(logging.Filter):
    def __init__(self, secret: str):
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secret:
            record.msg = record.getMessage().replace(self._secret, "[REDACTED]")
            record.args = ()
        return True


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _parse_ids(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for value in raw.split(","):
        try:
            if value.strip():
                values.add(int(value.strip()))
        except ValueError:
            LOG.warning("Ignoring invalid moderator ID value=%r", value)
    return frozenset(values)


@dataclass(frozen=True)
class ModeratorConfig:
    bot_token: str
    chat_id: str
    live: bool
    admin_ids: frozenset[int]
    allowed_domains: frozenset[str]
    captcha_timeout_seconds: int = 300
    new_member_hold_seconds: int = 86_400
    spam_window_seconds: int = 30
    spam_message_threshold: int = 30
    mute_seconds: int = 300
    auto_mute_on_attack: bool = False

    @classmethod
    def from_environment(cls) -> "ModeratorConfig":
        raw_domains = os.environ.get("MODERATOR_ALLOWED_DOMAINS", "")
        domains = frozenset(
            domain.strip().lower().lstrip(".")
            for domain in raw_domains.split(",")
            if domain.strip()
        )
        return cls(
            bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.environ.get("MODERATOR_CHAT_ID", "").strip(),
            live=_env_bool("TELEGRAM_SERVICES_ENABLED")
            and _env_bool("COMMUNITY_MODERATOR_LIVE"),
            admin_ids=_parse_ids(os.environ.get("MODERATOR_ADMIN_IDS", "")),
            allowed_domains=domains,
            captcha_timeout_seconds=_env_int(
                "MODERATOR_CAPTCHA_TIMEOUT_SECONDS", 300, minimum=30
            ),
            new_member_hold_seconds=_env_int(
                "MODERATOR_NEW_MEMBER_HOLD_SECONDS", 86_400, minimum=0
            ),
            spam_window_seconds=_env_int(
                "MODERATOR_SPAM_WINDOW_SECONDS", 30, minimum=5
            ),
            spam_message_threshold=_env_int(
                "MODERATOR_SPAM_MESSAGE_THRESHOLD", 30, minimum=3
            ),
            mute_seconds=_env_int("MODERATOR_MUTE_SECONDS", 300, minimum=30),
            auto_mute_on_attack=_env_bool("MODERATOR_AUTO_MUTE_ON_ATTACK"),
        )

    def validate_live(self) -> None:
        if not self.live:
            raise RuntimeError(
                "COMMUNITY_MODERATOR_LIVE is false; refusing to initialize Telegram."
            )
        if not self.bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required in live mode.")
        if not BOT_TOKEN_PATTERN.fullmatch(self.bot_token):
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN has an invalid format; enter only the "
                "BotFather token, without labels or surrounding text."
            )
        if not self.chat_id:
            raise RuntimeError("MODERATOR_CHAT_ID is required in live mode.")
        if not PTB_AVAILABLE:
            raise RuntimeError(
                "python-telegram-bot is unavailable; install telegram_announcer/"
                "moderator_requirements.txt in the isolated environment."
            )


@dataclass
class CaptchaChallenge:
    answer: int
    expires_at: float
    message_id: int | None = None
    attempts: int = 0


@dataclass
class CommunityModerator:
    config: ModeratorConfig
    pending_captchas: dict[tuple[int, int], CaptchaChallenge] = field(
        default_factory=dict
    )
    media_holds: dict[tuple[int, int], float] = field(default_factory=dict)
    spam_events: dict[int, list[float]] = field(
        default_factory=lambda: defaultdict(list)
    )
    muted_until: dict[int, float] = field(default_factory=dict)
    restore_permissions: dict[int, Any] = field(default_factory=dict)

    def _in_scope(self, chat_id: int | str | None) -> bool:
        return chat_id is not None and str(chat_id) == self.config.chat_id

    def _key(self, chat_id: int, user_id: int) -> tuple[int, int]:
        return chat_id, user_id

    async def _is_privileged(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return False
        if user.id in self.config.admin_ids:
            return True
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
            return member.status in {"administrator", "creator"}
        except Exception:
            LOG.exception("moderator_privilege_check_failed user_id=%s", user.id)
            return False

    @staticmethod
    def _captcha_permissions() -> ChatPermissions:
        return ChatPermissions(can_send_messages=False)

    @staticmethod
    def _media_limited_permissions() -> ChatPermissions:
        return ChatPermissions(
            can_send_messages=True,
            can_send_audios=False,
            can_send_documents=False,
            can_send_photos=False,
            can_send_videos=False,
            can_send_video_notes=False,
            can_send_voice_notes=False,
            can_send_polls=True,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        )

    @staticmethod
    def _normal_permissions() -> ChatPermissions:
        return ChatPermissions(
            can_send_messages=True,
            can_send_audios=True,
            can_send_documents=True,
            can_send_photos=True,
            can_send_videos=True,
            can_send_video_notes=True,
            can_send_voice_notes=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
        )

    def _captcha_markup(self, chat_id: int, user_id: int) -> InlineKeyboardMarkup:
        answer = secrets.randbelow(3) + 1
        challenge = CaptchaChallenge(
            answer=answer,
            expires_at=time.monotonic() + self.config.captcha_timeout_seconds,
        )
        self.pending_captchas[self._key(chat_id, user_id)] = challenge
        buttons = [
            InlineKeyboardButton(
                f"Option {choice}",
                callback_data=f"captcha:{chat_id}:{user_id}:{choice}",
            )
            for choice in secrets.SystemRandom().sample((1, 2, 3), 3)
        ]
        return InlineKeyboardMarkup([buttons])

    async def _restrict(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        user_id: int,
        permissions: ChatPermissions,
        *,
        reason: str,
    ) -> None:
        LOG.info(
            "moderation_restrict chat_id=%s user_id=%s reason=%s",
            chat_id,
            user_id,
            reason,
        )
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
        )

    async def handle_new_member(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        member_update = update.chat_member
        if not member_update or not self._in_scope(member_update.chat.id):
            return
        old_status = member_update.old_chat_member.status
        new_status = member_update.new_chat_member.status
        if old_status not in {"left", "kicked"} or new_status not in {
            "member",
            "restricted",
        }:
            return
        user = member_update.new_chat_member.user
        chat_id = member_update.chat.id
        markup = self._captcha_markup(chat_id, user.id)
        await self._restrict(
            context,
            chat_id,
            user.id,
            self._captcha_permissions(),
            reason="captcha_pending",
        )
        message = await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Welcome, {user.first_name}. Complete this captcha within "
                f"{self.config.captcha_timeout_seconds // 60} minutes to chat."
            ),
            reply_markup=markup,
        )
        challenge = self.pending_captchas[self._key(chat_id, user.id)]
        challenge.message_id = message.message_id
        LOG.info("captcha_issued chat_id=%s user_id=%s", chat_id, user.id)

        if context.job_queue:
            context.job_queue.run_once(
                self.expire_captcha,
                when=self.config.captcha_timeout_seconds,
                data={"chat_id": chat_id, "user_id": user.id},
                name=f"captcha-expiry-{chat_id}-{user.id}",
            )

    async def expire_captcha(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = context.job.data
        key = self._key(data["chat_id"], data["user_id"])
        challenge = self.pending_captchas.get(key)
        if challenge and challenge.expires_at <= time.monotonic():
            self.pending_captchas.pop(key, None)
            LOG.warning(
                "captcha_expired chat_id=%s user_id=%s",
                data["chat_id"],
                data["user_id"],
            )

    async def handle_captcha(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data or not query.from_user:
            return
        try:
            _, raw_chat_id, raw_user_id, raw_choice = query.data.split(":")
            chat_id, user_id, choice = (
                int(raw_chat_id),
                int(raw_user_id),
                int(raw_choice),
            )
        except (ValueError, AttributeError):
            await query.answer("Invalid captcha.", show_alert=True)
            return
        if query.from_user.id != user_id:
            await query.answer("This captcha belongs to another member.", show_alert=True)
            return
        challenge = self.pending_captchas.get(self._key(chat_id, user_id))
        if not challenge or challenge.expires_at < time.monotonic():
            await query.answer("This captcha has expired.", show_alert=True)
            return
        if choice != challenge.answer:
            challenge.attempts += 1
            await query.answer("Not quite. Try again.", show_alert=True)
            return

        self.pending_captchas.pop(self._key(chat_id, user_id), None)
        await query.answer("Captcha passed.")
        if query.message:
            await query.message.edit_text("Captcha passed. Media stays limited during the new-member safety window.")
        hold_until = time.monotonic() + self.config.new_member_hold_seconds
        self.media_holds[self._key(chat_id, user_id)] = hold_until
        await self._restrict(
            context,
            chat_id,
            user_id,
            self._media_limited_permissions(),
            reason="new_member_media_hold",
        )
        if context.job_queue and self.config.new_member_hold_seconds:
            context.job_queue.run_once(
                self.release_media_hold,
                when=self.config.new_member_hold_seconds,
                data={"chat_id": chat_id, "user_id": user_id},
                name=f"media-hold-release-{chat_id}-{user_id}",
            )
        LOG.info("captcha_passed chat_id=%s user_id=%s", chat_id, user_id)

    async def release_media_hold(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = context.job.data
        key = self._key(data["chat_id"], data["user_id"])
        if key not in self.media_holds:
            return
        self.media_holds.pop(key, None)
        await self._restrict(
            context,
            data["chat_id"],
            data["user_id"],
            self._normal_permissions(),
            reason="new_member_media_hold_expired",
        )
        LOG.info("media_hold_released chat_id=%s user_id=%s", **data)

    @staticmethod
    def _message_text(message: Any) -> str:
        return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()

    @classmethod
    def _contains_media(cls, message: Any) -> bool:
        return any(getattr(message, attribute, None) for attribute in MEDIA_ATTRIBUTES)

    def _urls(self, message: Any) -> list[str]:
        text = self._message_text(message)
        urls = URL_PATTERN.findall(text)
        entities = getattr(message, "entities", None) or getattr(message, "caption_entities", None) or []
        for entity in entities:
            if getattr(entity, "type", "") == "text_link" and getattr(entity, "url", None):
                urls.append(entity.url)
        return urls

    def _is_allowed_url(self, url: str) -> bool:
        normalized = url if "://" in url else f"https://{url}"
        host = (urlparse(normalized).hostname or "").lower().rstrip(".")
        return bool(
            host
            and any(host == allowed or host.endswith(f".{allowed}") for allowed in self.config.allowed_domains)
        )

    async def _delete_message(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        message_id: int,
        *,
        reason: str,
    ) -> None:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        LOG.info(
            "moderation_message_deleted chat_id=%s message_id=%s reason=%s",
            chat_id,
            message_id,
            reason,
        )

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        user = update.effective_user
        if not message or not chat or not user or not self._in_scope(chat.id):
            return
        if await self._is_privileged(update, context):
            return

        key = self._key(chat.id, user.id)
        if key in self.pending_captchas:
            await self._delete_message(
                context, chat.id, message.message_id, reason="captcha_pending"
            )
            return

        hold_until = self.media_holds.get(key, 0)
        if hold_until and hold_until <= time.monotonic():
            self.media_holds.pop(key, None)
            hold_until = 0
            await self._restrict(
                context,
                chat.id,
                user.id,
                self._normal_permissions(),
                reason="new_member_media_hold_expired_on_message",
            )
        if hold_until and self._contains_media(message):
            await self._delete_message(
                context, chat.id, message.message_id, reason="new_member_media_block"
            )
            return

        urls = [url for url in self._urls(message) if not self._is_allowed_url(url)]
        if urls:
            await self._delete_message(
                context, chat.id, message.message_id, reason="unauthorized_text_link"
            )

        now = time.monotonic()
        events = self.spam_events[chat.id]
        events[:] = [timestamp for timestamp in events if now - timestamp <= self.config.spam_window_seconds]
        events.append(now)
        if (
            self.config.auto_mute_on_attack
            and len(events) >= self.config.spam_message_threshold
            and self.muted_until.get(chat.id, 0) <= now
        ):
            await self.mute_chat(context, chat.id, self.config.mute_seconds, reason="automatic_spam_attack")

    async def mute_chat(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        chat_id: int,
        seconds: int,
        *,
        reason: str,
    ) -> None:
        if chat_id not in self.restore_permissions:
            chat = await context.bot.get_chat(chat_id)
            self.restore_permissions[chat_id] = chat.permissions
        await context.bot.set_chat_permissions(
            chat_id=chat_id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        self.muted_until[chat_id] = time.monotonic() + seconds
        LOG.warning("chat_muted chat_id=%s seconds=%s reason=%s", chat_id, seconds, reason)
        if context.job_queue:
            context.job_queue.run_once(
                self.unmute_chat,
                when=seconds,
                data={"chat_id": chat_id},
                name=f"chat-unmute-{chat_id}",
            )

    async def _restore_chat_permissions(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int
    ) -> None:
        permissions = self.restore_permissions.pop(chat_id, None) or self._normal_permissions()
        await context.bot.set_chat_permissions(chat_id=chat_id, permissions=permissions)
        self.muted_until.pop(chat_id, None)
        LOG.warning("chat_unmuted chat_id=%s", chat_id)

    async def unmute_chat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = int(context.job.data["chat_id"])
        await self._restore_chat_permissions(context, chat_id)

    async def command_mute(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.effective_chat or not await self._is_privileged(update, context):
            return
        seconds = self.config.mute_seconds
        if context.args:
            try:
                seconds = max(30, min(86_400, int(context.args[0])))
            except ValueError:
                await update.effective_message.reply_text("Usage: /mutechat [seconds]")
                return
        await self.mute_chat(context, update.effective_chat.id, seconds, reason="admin_command")

    async def command_unmute(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not update.effective_chat or not await self._is_privileged(update, context):
            return
        await self._restore_chat_permissions(context, update.effective_chat.id)

    async def handle_error(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        LOG.exception("community_moderator_handler_error", exc_info=context.error)


def build_application(config: ModeratorConfig) -> Application:
    config.validate_live()
    moderator = CommunityModerator(config)
    application = ApplicationBuilder().token(config.bot_token).concurrent_updates(False).build()
    application.add_handler(
        ChatMemberHandler(moderator.handle_new_member, ChatMemberHandler.CHAT_MEMBER)
    )
    application.add_handler(
        CallbackQueryHandler(moderator.handle_captcha, pattern=r"^captcha:")
    )
    application.add_handler(CommandHandler("mutechat", moderator.command_mute))
    application.add_handler(CommandHandler("unmutechat", moderator.command_unmute))
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL, moderator.handle_message),
        group=1,
    )
    application.add_error_handler(moderator.handle_error)
    return application


async def run_forever(config: ModeratorConfig) -> None:
    retry_seconds = 5
    while True:
        application = build_application(config)
        try:
            await application.initialize()
            await application.start()
            if not application.updater:
                raise RuntimeError("python-telegram-bot updater is unavailable.")
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=False,
            )
            LOG.info("community_moderator_started chat_id=%s", config.chat_id)
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise
        except InvalidToken:
            # python-telegram-bot includes the rejected token in InvalidToken's
            # message. Never pass that exception to logging or retry forever.
            LOG.error(
                "community_moderator_authentication_failed token_redacted=true"
            )
            return
        except Exception:
            LOG.exception("community_moderator_loop_failed retry_seconds=%s", retry_seconds)
            await asyncio.sleep(retry_seconds)
            retry_seconds = min(retry_seconds * 2, 300)
        finally:
            if application.updater and application.updater.running:
                await application.updater.stop()
            if application.running:
                await application.stop()
            await application.shutdown()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("MODERATOR_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = ModeratorConfig.from_environment()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    redactor = _SecretRedactionFilter(config.bot_token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)
    if not config.live:
        LOG.info(
            "community_moderator_safe_mode live=false; no Telegram client initialized"
        )
        return
    try:
        config.validate_live()
        asyncio.run(run_forever(config))
    except KeyboardInterrupt:
        LOG.info("community_moderator_stopped")
    except Exception:
        LOG.exception("community_moderator_start_failed")
        raise


if __name__ == "__main__":
    main()