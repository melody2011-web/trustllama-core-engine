from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.request import Request, urlopen

from .config import Settings
from .trader import TradeResult

logger = logging.getLogger(__name__)

NOTIFICATION_AUTH_CONTEXT = b"bnb-defi-bot/telegram-notification/v1"


class TelegramNotifier:
    """Submit a confirmed swap to the local authenticated Telegram bridge."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _authentication_token(self) -> str:
        return hmac.new(
            self.settings.notification_auth_secret.encode("utf-8"),
            NOTIFICATION_AUTH_CONTEXT,
            hashlib.sha256,
        ).hexdigest()

    def notify_success(self, result: TradeResult) -> bool:
        if not self.settings.notification_auth_secret:
            logger.error("Telegram notification authentication is not configured.")
            return False
        request = Request(
            self.settings.notification_api_url,
            data=json.dumps(result.notification_payload()).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Internal-Notification-Token": self._authentication_token(),
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=15) as response:
                body = json.loads(response.read().decode("utf-8"))
                return 200 <= response.status < 300 and body.get("ok") is True
        except Exception:
            logger.exception("Telegram bridge could not confirm alert delivery.")
            return False