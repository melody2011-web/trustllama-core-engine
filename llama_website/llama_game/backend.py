"""Isolated TLAMA Arcade FastAPI/WebSocket backend.

Live positions and collectibles remain in memory, while player names, simulated
match-ticket history, and high scores are stored in a local SQLite database. It
does not import trading modules, read wallet secrets, or submit a token
transaction. Optional live payments are disabled by default and require an
independently audited settlement hook.
"""

from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import asynccontextmanager, suppress
from collections import deque
from decimal import Decimal
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import sqlite3
import threading
import time
import uuid
import urllib.request
import urllib.parse
from urllib.parse import urlparse
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from layer3_core_rail import (  # noqa: E402
    Layer3CoreRail,
    positive_decimal,
    required_tlama_atomic_for_usd as _required_tlama_atomic_for_usd,
    required_tlama_for_usd as _required_tlama_for_usd,
)

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from reward_policy import (
    REWARD_VAULT_POLICY,
    REWARD_VAULT_RAW,
    VerifiedGameScore,
    allocate_epoch_rewards,
)
from storage import ArcadeStorage
from opening_warnings import run_scheduled_checks

LOG = logging.getLogger("trustllama.llama_game")
GAME_DIR = Path(__file__).parent
CLIENT_PATH = GAME_DIR / "index.html"
CONFIG_PATH = GAME_DIR / "config.js"
WEB3_ONBOARDING_PATH = GAME_DIR / "web3-onboarding.js"
WALLET_FAILURE_STAGES_PATH = GAME_DIR / "wallet-failure-stages.js"
DATABASE_PATH = Path(
    os.environ.get("TLAMA_GAME_DB_PATH", GAME_DIR / "data" / "tlama_arcade.sqlite3")
)
BOARD_MIN = 0.04
BOARD_MAX = 0.96
VICTORY_SCORE = 2800
OPENING_VERSION = "andean_v3"
LEVEL_ONE_CHIPS = {
    "chip-1": (0.14, 0.62),
    "chip-2": (0.31, 0.34),
    "chip-3": (0.50, 0.67),
    "chip-4": (0.69, 0.30),
    "chip-5": (0.84, 0.58),
}
LEVEL_ONE_FINISH_X = 0.94
LEVEL_THREE_ORB_RELOCATE_SECONDS = 3.0
_PAYMENT_QUOTE_CACHE_LOCK = threading.Lock()
_PAYMENT_QUOTE_CACHE: dict[str, Any] = {}


def _positive_decimal_env(name: str, default: str) -> Decimal:
    """Read a positive decimal setting without introducing float rounding."""

    try:
        return positive_decimal(os.environ.get(name, default), field=name)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


MOCK_TOKEN_PRICE_USD = _positive_decimal_env("MOCK_TOKEN_PRICE_USD", "0.01")
LAYER3_CORE_RAIL = Layer3CoreRail(token_price_usd=MOCK_TOKEN_PRICE_USD)
ARCADE_ENTRY_ITEM_ID = "arcade-entry-fee"
EXTRA_LIFE_ITEM_ID = "heart-matrix-upgrade"
CHARACTER_SKINS_ITEM_ID = "llama-character-skins"
ARCADE_ENTRY_TARGET_USD = Decimal("0.25")
HEART_MATRIX_TARGET_USD = Decimal("1.50")
CHARACTER_SKINS_TARGET_USD = Decimal("1.50")


def _mock_priced_catalog_item(
    *, item_id: str, kind: str, label: str, target_usd: Decimal, **details: Any
) -> dict[str, Any]:
    """Build one canonical catalog item through the Layer 3 rail."""

    return LAYER3_CORE_RAIL.price_catalog_item(
        item_id=item_id,
        kind=kind,
        label=label,
        target_usd=target_usd,
        pricing_source="mock-token-price-usd",
        details=details,
    )


PAYMENT_CATALOG = (
    _mock_priced_catalog_item(
        item_id=ARCADE_ENTRY_ITEM_ID,
        kind="entry",
        label="In-Game Entry Fee",
        target_usd=ARCADE_ENTRY_TARGET_USD,
        description="Required once per arcade match.",
    ),
    _mock_priced_catalog_item(
        item_id=EXTRA_LIFE_ITEM_ID,
        kind="utility",
        label="Heart Matrix Upgrade",
        target_usd=HEART_MATRIX_TARGET_USD,
        description="Adds one life to this match, up to two upgrades.",
        unlock_level=6,
        max_per_match=2,
    ),
    _mock_priced_catalog_item(
        item_id=CHARACTER_SKINS_ITEM_ID,
        kind="cosmetic",
        label="Llama Character Skins",
        target_usd=CHARACTER_SKINS_TARGET_USD,
        description="Cosmetic skin with no gameplay advantage.",
        variants=[
            {
                "skin_id": "aurora",
                "label": "Aurora llama skin",
                "preview": {"body": "#7ffcff", "accent": "#ff66d8", "glow": "#63f6ff"},
            },
            {
                "skin_id": "midnight",
                "label": "Midnight llama skin",
                "preview": {"body": "#7d72ff", "accent": "#20e3ff", "glow": "#8b5cff"},
            },
        ],
    ),
)
SIMULATED_ENTRY_FEE_TLAMA = int(PAYMENT_CATALOG[0]["amount_tlama"])
HEART_MATRIX_AMOUNT_TLAMA = int(PAYMENT_CATALOG[1]["amount_tlama"])
SKIN_STATE_TABLE = {
    "default": {"body": "#f6edff", "accent": "#d68cff", "glow": "#ff38d1"},
    **{
        str(variant["skin_id"]): dict(variant["preview"])
        for item in PAYMENT_CATALOG
        if item["kind"] == "cosmetic"
        for variant in item["variants"]
    },
}


@dataclass(frozen=True)
class LevelConfig:
    number: int
    name: str
    min_score: int
    max_score: int | None
    speed: float
    board_min: float
    board_max: float


LEVELS = (
    LevelConfig(1, "The Andean Pastures", 0, 100, 0.44, BOARD_MIN, BOARD_MAX),
    LevelConfig(2, "Electric Woodlands", 101, 250, 0.50, 0.10, 0.90),
    LevelConfig(3, "Plasma Farm", 251, 500, 0.70, 0.08, 0.92),
    LevelConfig(4, "The Haunted Moonkeep", 501, 650, 0.76, 0.07, 0.93),
    LevelConfig(5, "The Quantum Llama Labyrinth", 651, 800, 0.82, 0.06, 0.94),
    LevelConfig(6, "Llama Labyrinth", 801, 1200, 0.88, 0.06, 0.94),
    LevelConfig(7, "Llamacopter Caverns", 1201, 1600, 0.94, 0.09, 0.91),
    LevelConfig(8, "Llama Submarine Deep-Sea Trench", 1601, 2000, 0.72, 0.10, 0.90),
    LevelConfig(9, "Llama Sky Kingdom", 2001, 2400, 1.0, 0.05, 0.95),
    LevelConfig(10, "Trust Llama Citadel Core", 2401, None, 1.0, 0.04, 0.96),
)

HAZARD_STUN_SECONDS = 1.25
LIFE_LOSS_INVULNERABILITY_SECONDS = 10.0
STARTING_SHIELD_SECONDS = 10.0
HAZARD_BROADCAST_SECONDS = 0.10
STARTING_LIVES = 3
MAX_PURCHASED_LIVES = 5
MAX_EXTRA_LIFE_PURCHASES = 2
BUMPS_PER_LIFE = 1
ORBS_PER_EXTRA_LIFE = 50
PUMA_RADIUS = 0.055
LOG_RADIUS = 0.035
TRACTOR_RADIUS = 0.045
GHOST_COLLISION_RADIUS = 0.055
MAZE_WALL_RADIUS = 0.022
MAZE_WARDEN_RADIUS = 0.042
MAZE_COLS = 15
MAZE_ROWS = 11
MAZE_CELL_SIZE = 0.88 / MAZE_COLS
WARDEN_MODES = ("aggressive", "ambush", "patrol", "mimic")
MAZE_TUNNEL_ROW = 5
LEVEL7_GRAVITY = 0.72
LEVEL7_THRUST = 1.35
LEVEL7_AUTOSCROLL_SPEED = 0.22
LEVEL7_FIRE_COOLDOWN = 0.24
LEVEL7_DT_CAP = 0.1
LEVEL7_VEHICLE_RADIUS = 0.035
LEVEL7_ENTRY_X = 0.18
LEVEL7_ENTRY_Y = 0.50
LEVEL8_ENTRY_X = 0.22
LEVEL8_ENTRY_Y = 0.50
LEVEL8_GRAVITY = -0.18  # inverted buoyancy: absent thrust, the submarine rises
LEVEL8_THRUST = 1.15
LEVEL8_AUTOSCROLL_SPEED = 0.16
LEVEL8_DT_CAP = 0.1
LEVEL8_MAX_VELOCITY = 0.85
LEVEL8_OXYGEN_MAX = 100.0
LEVEL8_OXYGEN_DRAIN_PER_SECOND = 2.0
LEVEL8_COLLISION_RADIUS = 0.055
LEVEL8_ORB_RADIUS = 0.075
LEVEL9_DT_CAP = 0.1
LEVEL9_GRAVITY = 2.8
LEVEL9_JUMP_IMPULSE = -1.05
LEVEL9_RUN_ACCELERATION = 3.6
LEVEL9_RUN_SPEED = 0.72
LEVEL9_FRICTION = 5.0
LEVEL9_TERMINAL_VELOCITY = 1.8
LEVEL9_ORB_COUNT = 41
LEVEL9_AVATAR_HALF_WIDTH = 0.09
LEVEL9_AVATAR_HALF_HEIGHT = 0.09
LEVEL9_CAMERA_FOLLOW_X = 0.78
LEVEL10_DT_CAP = 0.1
LEVEL10_MAX_SPEED = 0.78
LEVEL10_ACCELERATION = 2.8
LEVEL10_FRICTION = 2.1
LEVEL10_INPUT_TIMEOUT = 0.25
LEVEL10_BOSS_MAX_HP = 300
LEVEL10_SHOT_COST = 25
LEVEL10_SHOT_DAMAGE = 25
LEVEL10_SHOT_COOLDOWN = 0.42


def level_for_score(score: int) -> LevelConfig:
    if score > 2400:
        return LEVELS[9]
    if score > 2000:
        return LEVELS[8]
    if score > 1600:
        return LEVELS[7]
    if score > 1200:
        return LEVELS[6]
    if score > 800:
        return LEVELS[5]
    if score > 650:
        return LEVELS[4]
    if score > 500:
        return LEVELS[3]
    if score > 250:
        return LEVELS[2]
    if score > 100:
        return LEVELS[1]
    return LEVELS[0]


def level_payload(level: LevelConfig) -> dict[str, Any]:
    return {
        "number": level.number,
        "name": level.name,
        "min_score": level.min_score,
        "max_score": level.max_score,
        "speed": level.speed,
        "board_min": level.board_min,
        "board_max": level.board_max,
        "next_score": (
            level.max_score + 1 if level.max_score is not None else VICTORY_SCORE
        ),
    }


@dataclass
class Player:
    """Server-authoritative state for one connected player."""

    player_id: str
    name: str
    x: float = 0.5
    y: float = 0.5
    score: int = 0
    current_level: int = 1
    victory_announced: bool = False
    entry_fee_tlama: int = SIMULATED_ENTRY_FEE_TLAMA
    ticket_id: str = ""
    ownership_token: str = ""
    stunned_until: float = 0.0
    invulnerable_until: float = 0.0
    lives: int = STARTING_LIVES
    max_lives: int = STARTING_LIVES
    extra_lives_purchased: int = 0
    extra_life_unlocked: bool = False
    bump_count: int = 0
    orbs_collected: int = 0
    level_one_chip_ids: set[str] = field(default_factory=set)
    game_over: bool = False
    movement_direction: str = "right"
    equipped_skin_id: str = "default"
    owned_skin_ids: tuple[str, ...] = ("default",)
    admin_test_skin_ids: tuple[str, ...] = field(default_factory=tuple)
    # Level 7 vehicle state is written only by the authoritative simulation.
    world_progress: float = 0.0
    vehicle_velocity: float = 0.0
    vehicle_thrust: bool = False
    vehicle_input_sequence: int = -1
    projectile_cooldown_until: float = 0.0
    vehicle_fire_requested: bool = False
    # Level 8 submarine state. These are never populated from client coordinates.
    submarine_velocity: float = 0.0
    submarine_thrust: bool = False
    submarine_input_sequence: int = -1
    oxygen: float = LEVEL8_OXYGEN_MAX
    oxygen_zero_latched: bool = False
    # Level 9 platformer state; never populated from client coordinates.
    vx: float = 0.0
    vy: float = 0.0
    grounded: bool = False
    run_axis: int = 0
    platformer_input_sequence: int = -1
    jump_latched: bool = False
    booster_vx: float = 0.0
    booster_vy: float = 0.0
    booster_x: int = 0
    booster_y: int = 0
    citadel_input_sequence: int = -1
    citadel_input_at: float = 0.0
    super_spit_charge: int = 0
    super_spit_held: bool = False
    super_spit_release_latched: bool = False
    super_spit_cooldown_until: float = 0.0
    # Server-only provenance. Test runs never enter persistence or rewards.
    admin_test_mode: bool = False


ADMIN_SESSION_COOKIE = "tlama_arcade_admin_session"
ADMIN_CSRF_COOKIE = "tlama_arcade_admin_csrf"
ADMIN_SESSION_TTL_SECONDS = 300.0
ADMIN_GRANT_TTL_SECONDS = 300.0
ADMIN_MAX_AUTH_BODY_BYTES = 2048
ADMIN_MAX_ATTEMPTS_PER_IP = 5
ADMIN_MAX_ATTEMPTS_GLOBAL = 30
ADMIN_RATE_WINDOW_SECONDS = 60.0
ADMIN_LOCKOUT_BASE_SECONDS = 2.0
ADMIN_MAX_IP_BUCKETS = 4096
ADMIN_MAX_SESSIONS = 2048


@dataclass
class _AdminSession:
    csrf: str
    created_at: float
    last_seen: float
    expires_at: float
    grant_hash: str = ""
    used: bool = False
    websocket: WebSocket | None = None
    player: Player | None = None


class AdminPortalSecurity:
    """Small, bounded, server-only grant store for the testing portal."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sessions: dict[str, _AdminSession] = {}
        self.ip_attempts: dict[str, deque[float]] = {}
        self.global_attempts: deque[float] = deque(maxlen=ADMIN_MAX_ATTEMPTS_GLOBAL)
        self.locked_until: dict[str, float] = {}

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _ip_hash(ip: str) -> str:
        return hashlib.sha256(("tlama-arcade-ip:" + ip).encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _cleanup_locked(self, now: float) -> None:
        cutoff = now - ADMIN_RATE_WINDOW_SECONDS
        while self.global_attempts and self.global_attempts[0] < cutoff:
            self.global_attempts.popleft()
        for ip, attempts in list(self.ip_attempts.items()):
            while attempts and attempts[0] < cutoff:
                attempts.popleft()
            if not attempts:
                self.ip_attempts.pop(ip, None)
                if self.locked_until.get(ip, 0.0) <= now:
                    self.locked_until.pop(ip, None)
        for session_id, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.sessions.pop(session_id, None)

    def new_session(self) -> tuple[str, str] | None:
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            now = self._now()
            self._cleanup_locked(now)
            if len(self.sessions) >= ADMIN_MAX_SESSIONS:
                inactive = [
                    (key, session)
                    for key, session in self.sessions.items()
                    if session.websocket is None and not session.grant_hash
                ]
                if not inactive:
                    return None
                evict_key, _ = min(inactive, key=lambda item: item[1].last_seen)
                self.sessions.pop(evict_key, None)
            self.sessions[session_id] = _AdminSession(
                csrf=csrf,
                created_at=now,
                last_seen=now,
                expires_at=now + ADMIN_SESSION_TTL_SECONDS,
            )
        return session_id, csrf

    def ensure_session(
        self, session_id: str | None, csrf: str | None
    ) -> tuple[str, str] | None:
        with self._lock:
            now = self._now()
            self._cleanup_locked(now)
            if (
                session_id
                and csrf
                and len(session_id) <= 128
                and len(csrf) <= 128
                and session_id in self.sessions
                and self.sessions[session_id].expires_at > now
                and hmac.compare_digest(self.sessions[session_id].csrf, csrf)
            ):
                self.sessions[session_id].last_seen = now
                return session_id, csrf
        return self.new_session()

    def authenticate(
        self, session_id: str, csrf: str, password: str, ip: str
    ) -> tuple[bool, str]:
        ip_key = self._ip_hash(ip)
        now = self._now()
        configured_password = os.environ.get("TLAMA_ARCADE_ADMIN_PASSWORD", "")
        enabled = _env_flag("TLAMA_ARCADE_ADMIN_PORTAL_ENABLED") and bool(
            configured_password
        )
        with self._lock:
            self._cleanup_locked(now)
            attempts = self.ip_attempts.get(ip_key)
            bucket_available = attempts is not None or (
                len(self.ip_attempts) < ADMIN_MAX_IP_BUCKETS
            )
            blocked = (
                not bucket_available
                or now < self.locked_until.get(ip_key, 0.0)
                or (attempts is not None and len(attempts) >= ADMIN_MAX_ATTEMPTS_PER_IP)
                or len(self.global_attempts) >= ADMIN_MAX_ATTEMPTS_GLOBAL
            )
            if not blocked and attempts is None:
                attempts = deque(maxlen=ADMIN_MAX_ATTEMPTS_PER_IP)
                self.ip_attempts[ip_key] = attempts
            valid_session = session_id in self.sessions
            valid_csrf = valid_session and hmac.compare_digest(
                self.sessions[session_id].csrf, csrf
            )
            # Always perform a constant-time comparison, including disabled and
            # malformed requests, without retaining the submitted secret.
            password_ok = hmac.compare_digest(
                password if isinstance(password, str) else "",
                configured_password,
            )
            if not enabled or blocked or not valid_csrf or not password_ok:
                if not blocked:
                    assert attempts is not None
                    attempts.append(now)
                    self.global_attempts.append(now)
                if not enabled:
                    reason = "disabled"
                elif blocked:
                    reason = "rate_limited"
                elif not valid_csrf:
                    reason = "csrf_failed"
                else:
                    reason = "invalid_password"
                if reason == "invalid_password" and len(attempts) >= 3:
                    self.locked_until[ip_key] = now + min(
                        ADMIN_MAX_ATTEMPTS_PER_IP,
                        2 ** min(len(attempts), 6),
                    )
                return False, reason
            grant = secrets.token_urlsafe(32)
            session = self.sessions[session_id]
            session.grant_hash = self._hash(grant)
            session.expires_at = now + ADMIN_GRANT_TTL_SECONDS
            session.used = False
            session.last_seen = now
            return True, "authenticated"

    def bind_websocket(self, websocket: WebSocket) -> bool:
        headers = getattr(websocket, "headers", {})
        cookies = _parse_cookie_header(headers.get("cookie", ""))
        session_id = cookies.get(ADMIN_SESSION_COOKIE)
        if not session_id:
            return True
        with self._lock:
            self._cleanup_locked(self._now())
            session = self.sessions.get(session_id)
            if session is None:
                return False
            if session.websocket is not None and session.websocket is not websocket:
                return False
            session.websocket = websocket
            session.last_seen = self._now()
            return True

    def bind_player(self, websocket: WebSocket, player: Player) -> None:
        with self._lock:
            for session in self.sessions.values():
                if session.websocket is websocket:
                    if session.player is None:
                        session.player = player
                    return

    def consume(self, websocket: WebSocket, player: Player) -> bool:
        with self._lock:
            now = self._now()
            self._cleanup_locked(now)
            for session in self.sessions.values():
                if (
                    session.websocket is websocket
                    and session.player is player
                    and not session.used
                    and session.grant_hash
                    and session.expires_at > now
                ):
                    session.used = True
                    session.grant_hash = ""
                    session.expires_at = now + ADMIN_SESSION_TTL_SECONDS
                    session.last_seen = now
                    return True
        return False

    def disconnect(self, websocket: WebSocket) -> None:
        with self._lock:
            now = self._now()
            for session in self.sessions.values():
                if session.websocket is websocket:
                    # Keep the short-lived browser session usable for a normal
                    # reconnect, but revoke its grant and player binding.
                    session.grant_hash = ""
                    session.used = True
                    session.websocket = None
                    session.player = None
                    session.expires_at = now + ADMIN_SESSION_TTL_SECONDS
                    session.last_seen = now


def _parse_cookie_header(header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in header.split(";"):
        if "=" in item:
            key, value = item.strip().split("=", 1)
            if key and value:
                cookies[key] = value
    return cookies


admin_security = AdminPortalSecurity()


def _clamp_coordinate(
    value: Any, fallback: float, board_min: float = BOARD_MIN, board_max: float = BOARD_MAX
) -> float:
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(coordinate):
        return fallback
    return max(board_min, min(board_max, coordinate))


def _safe_name(value: Any) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized[:18] if len(normalized) >= 2 else None


def _safe_player_key(value: Any) -> str | None:
    """Accept only a browser UUID; invalid identities must fail closed."""

    try:
        return uuid.UUID(str(value)).hex
    except (AttributeError, TypeError, ValueError):
        return None


def _safe_ownership_token(value: Any) -> str | None:
    token = str(value or "").strip()
    if 32 <= len(token) <= 128 and all(
        character.isalnum() or character in "-_" for character in token
    ):
        return token
    return None


def _safe_solana_value(value: Any, *, minimum: int, maximum: int) -> str | None:
    text = str(value or "").strip()
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    if minimum <= len(text) <= maximum and all(character in alphabet for character in text):
        return text
    return None


def _fetch_tlama_oracle_price_sync(
    config: dict[str, Any], *, now: int | None = None
) -> tuple[Decimal, int]:
    """Read and strictly validate the configured Pyth TLAMA/USD price."""

    endpoint = str(config.get("oracle_url", "")).rstrip("/")
    feed_id = str(config.get("oracle_feed_id", "")).lower().removeprefix("0x")
    if (
        not endpoint.startswith("https://")
        or len(feed_id) != 64
        or any(character not in "0123456789abcdef" for character in feed_id)
    ):
        raise ValueError("Pyth oracle endpoint and feed identity are required")
    query = urllib.parse.urlencode([("ids[]", feed_id), ("parsed", "true")])
    request = urllib.request.Request(
        f"{endpoint}/v2/updates/price/latest?{query}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = json.loads(response.read(1_000_000))
    parsed = payload.get("parsed")
    if not isinstance(parsed, list) or len(parsed) != 1:
        raise ValueError("Pyth response must contain exactly one parsed feed")
    update = parsed[0]
    if not isinstance(update, dict) or str(update.get("id", "")).lower().removeprefix("0x") != feed_id:
        raise ValueError("Pyth response feed identity does not match")
    price = update.get("price")
    if not isinstance(price, dict):
        raise ValueError("Pyth response has no price")
    try:
        raw_price = int(price["price"])
        confidence = int(price["conf"])
        exponent = int(price["expo"])
        publish_time = int(price["publish_time"])
        expected_exponent = int(config["oracle_exponent"])
        max_age = int(config["oracle_max_age_seconds"])
        max_confidence_bps = int(config["oracle_max_confidence_bps"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Pyth price fields are malformed") from exc
    current_time = int(time.time()) if now is None else now
    if (
        raw_price <= 0
        or confidence < 0
        or exponent != expected_exponent
        or not -18 <= exponent <= 0
        or max_age <= 0
        or publish_time > current_time + 5
        or current_time - publish_time > max_age
        or not 0 <= max_confidence_bps <= 10_000
        or confidence * 10_000 > raw_price * max_confidence_bps
    ):
        raise ValueError("Pyth price failed sign, decimals, freshness, or confidence checks")
    value = Decimal(raw_price).scaleb(exponent)
    if not value.is_finite() or value <= 0:
        raise ValueError("Pyth price is not a positive finite decimal")
    return value, publish_time


def _oracle_quote_secret() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "").encode("utf-8")
    if len(secret) < 32:
        raise ValueError("SESSION_SECRET must contain at least 32 bytes")
    return secret


def _issue_oracle_quote(
    config: dict[str, Any],
    *,
    item_id: str,
    atomic_amount: int,
    publish_time: int,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else now
    ttl = int(config.get("oracle_quote_ttl_seconds", 180))
    if not 30 <= ttl <= 300 or atomic_amount <= 0:
        raise ValueError("Oracle quote TTL or amount is invalid")
    payload = {
        "v": 1,
        "item_id": item_id,
        "feed_id": str(config["oracle_feed_id"]).lower().removeprefix("0x"),
        "publish_time": publish_time,
        "amount_atomic": str(atomic_amount),
        "decimals": int(config["decimals"]),
        "issued_at": issued_at,
        "expires_at": issued_at + ttl,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).rstrip(b"=")
    signature = hmac.new(_oracle_quote_secret(), encoded, hashlib.sha256).digest()
    return f"{encoded.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def _validate_oracle_quote(
    token: str,
    config: dict[str, Any],
    *,
    expected_item_id: str | None = None,
    expected_atomic_amount: int | None = None,
    now: int | None = None,
    check_expiry: bool = True,
) -> dict[str, Any]:
    try:
        encoded_text, signature_text = token.split(".", 1)
        encoded = encoded_text.encode("ascii")
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.new(_oracle_quote_secret(), encoded, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Oracle quote signature does not match")
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))
        )
        current_time = int(time.time()) if now is None else now
        amount_atomic = int(payload["amount_atomic"])
        if (
            payload.get("v") != 1
            or (
                expected_item_id is not None
                and payload.get("item_id") != expected_item_id
            )
            or (
                expected_atomic_amount is not None
                and amount_atomic != expected_atomic_amount
            )
            or payload.get("feed_id")
            != str(config["oracle_feed_id"]).lower().removeprefix("0x")
            or int(payload.get("decimals")) != int(config["decimals"])
            or amount_atomic <= 0
            or int(payload.get("issued_at")) > current_time + 5
            or (check_expiry and current_time > int(payload.get("expires_at")))
            or int(payload.get("expires_at")) - int(payload.get("issued_at")) > 300
            or int(payload.get("publish_time")) > int(payload.get("issued_at")) + 5
        ):
            raise ValueError("Oracle quote fields are invalid or expired")
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Oracle quote is malformed or invalid") from exc
    return payload


def _verify_catalog_settlement_sync(
    signature: str,
    wallet_address: str,
    item_id: str,
    oracle_quote: str,
) -> bool:
    config = payment_config(issue_quote=False)
    rpc_url = str(config.get("rpc_url", ""))
    if not config.get("enabled") or not rpc_url.startswith("https://"):
        return False
    catalog_item = next(
        (
            item
            for item in PAYMENT_CATALOG
            if item.get("id") == item_id
        ),
        None,
    )
    if catalog_item is None:
        return False
    expected_atomic_price = (
        int(catalog_item["amount_tlama"]) * 10 ** int(config["decimals"])
    )
    def rpc_result(method: str, params: list[Any]) -> Any:
        request_body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params
        }).encode("utf-8")
        request = urllib.request.Request(
            rpc_url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read(2_000_000)).get("result")

    token_2022_program = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
    try:
        mint_result = rpc_result(
            "getAccountInfo",
            [config["mint_address"], {"commitment": "finalized", "encoding": "jsonParsed"}],
        )
        mint_value = (mint_result or {}).get("value") or {}
        mint_info = (((mint_value.get("data") or {}).get("parsed") or {}).get("info") or {})
        extensions = mint_info.get("extensions") or []
        hook_extension = next(
            (
                extension
                for extension in extensions
                if isinstance(extension, dict)
                and extension.get("extension") == "transferHook"
            ),
            None,
        )
        if (
            mint_value.get("owner") != token_2022_program
            or not isinstance(hook_extension, dict)
            or (hook_extension.get("state") or {}).get("programId")
            != config["hook_program_address"]
        ):
            return False
        mint_decimals = int(mint_info.get("decimals"))
        if mint_decimals != int(config["decimals"]):
            return False
        _validate_oracle_quote(
            oracle_quote,
            config,
            expected_item_id=item_id,
            expected_atomic_amount=expected_atomic_price,
            check_expiry=False,
        )
        atomic_price = expected_atomic_price
        result = rpc_result(
            "getTransaction",
            [
                signature,
                {
                    "commitment": "finalized",
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                },
            ],
        )
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(result, dict):
        return False
    try:
        block_time = int(result["blockTime"])
        _validate_oracle_quote(
            oracle_quote,
            config,
            expected_item_id=item_id,
            expected_atomic_amount=expected_atomic_price,
            now=block_time,
        )
    except (KeyError, TypeError, ValueError):
        return False
    meta = result.get("meta")
    message = (result.get("transaction") or {}).get("message") or {}
    if not isinstance(meta, dict) or meta.get("err") is not None:
        return False
    raw_keys = message.get("accountKeys")
    if not isinstance(raw_keys, list):
        return False
    keys = [
        entry.get("pubkey") if isinstance(entry, dict) else entry
        for entry in raw_keys
    ]
    signer_keys = {
        entry.get("pubkey")
        for entry in raw_keys
        if isinstance(entry, dict) and entry.get("signer") is True
    }
    mint = config["mint_address"]
    vault = config["reward_vault_address"]
    hook = config["hook_program_address"]
    if wallet_address not in signer_keys or vault not in keys:
        return False
    pre = {
        int(balance["accountIndex"]): balance
        for balance in meta.get("preTokenBalances", [])
        if isinstance(balance, dict) and balance.get("mint") == mint
    }
    post = {
        int(balance["accountIndex"]): balance
        for balance in meta.get("postTokenBalances", [])
        if isinstance(balance, dict) and balance.get("mint") == mint
    }
    def amount(balance: dict[str, Any] | None) -> int:
        try:
            return int(((balance or {}).get("uiTokenAmount") or {}).get("amount", "0"))
        except (TypeError, ValueError):
            return 0
    vault_index = keys.index(vault)
    if amount(post.get(vault_index)) - amount(pre.get(vault_index)) != atomic_price:
        return False
    wallet_indices = {
        index
        for index in {*pre, *post}
        if (pre.get(index) or {}).get("owner") == wallet_address
        or (post.get(index) or {}).get("owner") == wallet_address
    }
    wallet_net_change = sum(
        amount(post.get(index)) - amount(pre.get(index))
        for index in wallet_indices
    )
    if wallet_net_change != -atomic_price:
        return False

    outer = message.get("instructions")
    if not isinstance(outer, list):
        return False
    inner_by_index = {
        int(group["index"]): group.get("instructions", [])
        for group in meta.get("innerInstructions", [])
        if isinstance(group, dict) and isinstance(group.get("instructions"), list)
    }
    for index, instruction in enumerate(outer):
        inner = inner_by_index.get(index, [])
        group = [instruction, *inner]
        for candidate_index, candidate in enumerate(group):
            if (
                not isinstance(candidate, dict)
                or candidate.get("programId") != token_2022_program
            ):
                continue
            parsed = candidate.get("parsed")
            if not isinstance(parsed, dict) or parsed.get("type") != "transferChecked":
                continue
            info = parsed.get("info")
            if not isinstance(info, dict):
                continue
            token_amount = info.get("tokenAmount")
            source = info.get("source")
            try:
                transferred_amount = int((token_amount or {}).get("amount", "-1"))
                transfer_decimals = int((token_amount or {}).get("decimals"))
            except (AttributeError, TypeError, ValueError):
                continue
            if (
                info.get("destination") == vault
                and info.get("mint") == mint
                and info.get("authority") == wallet_address
                and isinstance(token_amount, dict)
                and transferred_amount == atomic_price
                and transfer_decimals == mint_decimals
                and source in keys
            ):
                source_index = keys.index(source)
                source_owner = (pre.get(source_index) or post.get(source_index) or {}).get("owner")
                transfer_stack = (
                    1 if candidate_index == 0 else int(candidate.get("stackHeight") or 0)
                )
                hook_is_child = False
                for following in group[candidate_index + 1:]:
                    if not isinstance(following, dict):
                        continue
                    following_stack = int(following.get("stackHeight") or 0)
                    if following_stack <= transfer_stack:
                        break
                    if (
                        following_stack == transfer_stack + 1
                        and following.get("programId") == hook
                    ):
                        hook_is_child = True
                        break
                if source_owner == wallet_address and hook_is_child:
                    return True
    return False


def _verify_extra_life_settlement_sync(
    signature: str, wallet_address: str, oracle_quote: str
) -> bool:
    """Verify a Heart Matrix payment through the generic catalog verifier."""

    return _verify_catalog_settlement_sync(
        signature,
        wallet_address,
        EXTRA_LIFE_ITEM_ID,
        oracle_quote,
    )


class GameRoom:
    """Small in-memory room with authoritative broadcasts."""

    def __init__(self, storage: ArcadeStorage) -> None:
        self.storage = storage
        self.connections: dict[WebSocket, Player] = {}
        self.collectibles: dict[str, dict[str, float]] = {
            "orb-1": {"x": 0.22, "y": 0.24},
            "orb-2": {"x": 0.78, "y": 0.28},
            "orb-3": {"x": 0.31, "y": 0.72},
            "orb-4": {"x": 0.70, "y": 0.76},
        }
        self.lock = asyncio.Lock()
        self.orb_relocation_tick = 0
        self.orb_timer_task: asyncio.Task[None] | None = None
        self.hazard_timer_task: asyncio.Task[None] | None = None
        self.opening_warning_task: asyncio.Task[None] | None = None
        self.opening_milestones: dict[WebSocket, set[str]] = {}
        self.utility_purchase_pending: set[str] = set()
        self.utility_purchase_attempts: dict[str, deque[float]] = {}
        self.level7_worlds: dict[str, dict[str, Any]] = {}
        self.level7_timer_task: asyncio.Task[None] | None = None
        self.level8_worlds: dict[str, dict[str, Any]] = {}
        self.level8_timer_task: asyncio.Task[None] | None = None
        self.level9_worlds: dict[str, dict[str, Any]] = {}
        self.level9_timer_task: asyncio.Task[None] | None = None
        self.level10_worlds: dict[str, dict[str, Any]] = {}
        self.level10_timer_task: asyncio.Task[None] | None = None
        self.admin_collectibles: dict[str, dict[str, dict[str, float]]] = {}
        self.admin_hazards: dict[str, dict[str, Any]] = {}
        self.admin_relocations: dict[str, dict[str, Any]] = {}
        try:
            self.hazard_started_at = asyncio.get_event_loop().time()
        except RuntimeError:
            self.hazard_started_at = time.monotonic()
        self.maze_grid = self._build_maze_grid()
        self.wardens = self._reset_wardens()

    @staticmethod
    def _level7_world(player_id: str) -> dict[str, Any]:
        """Create a stable, private cavern layout (never shared between players)."""
        orbs = {
            f"cavern-orb-{i}": {"id": f"cavern-orb-{i}", "x": 0.18 + i * 0.035,
                                "y": 0.20 + ((i * 37) % 58) / 100}
            for i in range(48)
        }
        blocks = [
            {"id": f"cavern-block-{i}", "x": 0.42 + i * 0.24,
             "y": 0.22 + ((i * 29) % 52) / 100, "destroyed": False}
            for i in range(9)
        ]
        wardens = [
            {"id": "cavern-warden-1", "x": 0.68, "y": 0.28, "mode": "sine", "phase": 0.0},
            {"id": "cavern-warden-2", "x": 0.79, "y": 0.60, "mode": "vertical", "phase": 1.7},
            {"id": "cavern-warden-3", "x": 0.91, "y": 0.40, "mode": "sine", "phase": 3.1},
        ]
        return {"seed": 701 + sum(ord(c) for c in player_id) % 997,
                "camera_progress": 0.0, "orbs": orbs, "blocks": blocks,
                "wardens": wardens, "projectiles": [], "last_tick": None}

    def _ensure_level7(self, player: Player) -> dict[str, Any]:
        world = self.level7_worlds.setdefault(player.player_id, self._level7_world(player.player_id))
        return world

    @staticmethod
    def _level8_world(player_id: str) -> dict[str, Any]:
        seed = 800 + sum(ord(c) for c in player_id) % 997
        orbs = {
            f"trench-orb-{i}": {
                "id": f"trench-orb-{i}", "x": 0.22 + (i % 12) * 0.07,
                "y": 0.18 + ((i * 37) % 64) / 100, "collected": False,
            } for i in range(64)
        }
        bubbles = {
            f"oxygen-bubble-{i}": {
                "id": f"oxygen-bubble-{i}", "x": 0.35 + (i % 8) * 0.09,
                "y": 0.20 + ((i * 29) % 58) / 100, "amount": 20.0,
                "collected": False,
            } for i in range(8)
        }
        vents = [
            {"id": "vent-1", "x": 0.52, "y": 0.74, "radius": 0.10, "force": 0.55, "active": True},
            {"id": "vent-2", "x": 0.82, "y": 0.32, "radius": 0.085, "force": 0.42, "active": True},
            {"id": "vent-3", "x": 0.38, "y": 0.84, "radius": 0.09, "force": 0.48, "active": True},
        ]
        wardens = [
            {"id": "W1", "name": "W1", "mode": "horizontal_patrol", "x": 0.70, "y": 0.32,
             "phase": 0.0, "pulse_hz": 1.2, "light_cone": False, "charging": False,
             "cooldown_until": 0.0},
            {"id": "W2", "name": "W2", "mode": "stationary_guardian", "x": 0.76, "y": 0.50,
             "phase": 1.0, "pulse_hz": 0.8, "light_cone": True, "charging": False,
             "cooldown_until": 0.0},
            {"id": "W3", "name": "W3", "mode": "vertical_loop", "x": 0.91, "y": 0.56,
             "phase": 2.0, "pulse_hz": 1.0, "light_cone": False, "charging": False,
             "cooldown_until": 0.0},
        ]
        return {"seed": seed, "camera_progress": 0.0, "orbs": orbs, "bubbles": bubbles,
                "vents": vents, "wardens": wardens, "last_tick": None, "vent_pulse_until": 0.0}

    def _ensure_level8(self, player: Player) -> dict[str, Any]:
        return self.level8_worlds.setdefault(player.player_id, self._level8_world(player.player_id))

    def _admin_collectibles_for_level(self, level: int) -> dict[str, dict[str, float]]:
        """Return a private canonical map without touching the shared room map."""

        if level == 6:
            return copy.deepcopy(self._maze_orbs())
        return copy.deepcopy(self.collectibles)

    def _admin_hazard_state(self, player: Player, now: float | None = None) -> dict[str, Any]:
        current = asyncio.get_event_loop().time() if now is None else now
        return self.admin_hazards.setdefault(
            player.player_id,
            {
                "level": player.current_level,
                "hazard_started_at": current,
                "wardens": self._reset_wardens(),
            },
        )

    def _initialize_admin_hazards(self, player: Player, now: float) -> None:
        self.admin_hazards[player.player_id] = {
            "level": player.current_level,
            "hazard_started_at": now,
            "wardens": self._reset_wardens(),
        }

    def _initialize_admin_relocation(self, player: Player, now: float) -> None:
        self.admin_relocations[player.player_id] = {
            "tick": 0,
            "last_relocation_at": now,
        }

    def _collectibles_for_player(self, player: Player) -> dict[str, dict[str, float]]:
        if player.admin_test_mode and player.current_level <= 6:
            return self.admin_collectibles.setdefault(
                player.player_id,
                self._admin_collectibles_for_level(player.current_level),
            )
        return self.collectibles

    @staticmethod
    def _level9_world(player_id: str) -> dict[str, Any]:
        seed = 900 + sum(ord(c) for c in player_id) % 997
        platforms = [
            {"id": "sky-start", "x": 1.2, "y": 0.82, "w": 2.4, "h": 0.12, "solid": True},
            *({"id": f"brick-{i}", "x": 2.0 + i * 1.7, "y": 0.62 - (i % 3) * .12,
               "w": .9, "h": .12, "solid": True} for i in range(12)),
        ]
        pipes = [{"id": f"pipe-{i}", "x": 5.0 + i * 8.0, "y": .58, "w": .34, "h": .24, "solid": True}
                 for i in range(5)]
        orbs = {f"sky-orb-{i}": {"id": f"sky-orb-{i}", "x": .8 + i * 2.15,
                                 "y": .45 + (i % 4) * .07, "collected": False}
                for i in range(LEVEL9_ORB_COUNT)}
        blocks = [{"id": f"question-{i}", "x": 3.0 + i * 5.2, "y": .42,
                   "w": .34, "h": .34, "hit": False} for i in range(8)]
        wardens = [
            {"id": "W1", "name": "W1", "kind": "robotic_turtle", "mode": "platform_patrol",
             "x": 4.0, "y": .54, "min_x": 3.2, "max_x": 5.0, "direction": 1},
            {"id": "W2", "name": "W2", "kind": "spiky_cloud", "mode": "projectile_guard",
             "x": 12.0, "y": .16, "cooldown_until": 0.0},
            {"id": "W3", "name": "W3", "kind": "robotic_turtle_cloud", "mode": "pipe_patrol",
             "x": 9.0, "y": .52, "pipe_index": 1, "direction": 1},
        ]
        return {"seed": seed, "camera_progress": 0.0, "orbs": orbs, "platforms": platforms,
                "cloud_platforms": [{"id": "cloud-1", "x": 6.0, "base_x": 6.0, "y": .38, "w": 1.1, "phase": 0.0}],
                "pipes": pipes, "question_blocks": blocks, "secret_bundles": [],
                "wardens": wardens, "projectiles": [], "last_tick": None}

    def _ensure_level9(self, player: Player) -> dict[str, Any]:
        return self.level9_worlds.setdefault(player.player_id, self._level9_world(player.player_id))

    @staticmethod
    def _level10_world(player_id: str) -> dict[str, Any]:
        seed = 1000 + sum(ord(c) for c in player_id) % 997
        return {
            "seed": seed, "camera": {"x": 0.5, "y": 0.5, "zoom": 1.0},
            "bounds": {"min_x": 0.04, "max_x": 0.96, "min_y": 0.08, "max_y": 0.92},
            "boss": {"id": "anti-whale-warden", "x": 0.5, "y": 0.42,
                     "max_hp": 300, "hp": 300, "phase": 1,
                     "shield_until": 0.0, "vulnerable": False},
            "phase_started": 0.0, "klaxons": [], "beams": [], "traps": [],
            "drones": [{"id": "citadel-drone-1", "x": 0.14, "y": 0.18, "phase": 0.0},
                       {"id": "citadel-drone-2", "x": 0.86, "y": 0.82, "phase": 2.4}],
            "orbs": {f"citadel-orb-{i}": {"id": f"citadel-orb-{i}",
                      "x": 0.12 + (i * 17 % 76) / 100,
                      "y": 0.12 + (i * 31 % 76) / 100, "collected": False}
                    for i in range(80)},
            "projectiles": [], "hazards": [], "events": [], "last_tick": None,
        }

    def _ensure_level10(self, player: Player) -> dict[str, Any]:
        return self.level10_worlds.setdefault(player.player_id, self._level10_world(player.player_id))

    def _enter_level10(self, player: Player, now: float) -> None:
        self.level10_worlds[player.player_id] = self._level10_world(player.player_id)
        world = self.level10_worlds[player.player_id]
        world["boss"]["shield_until"] = now + STARTING_SHIELD_SECONDS
        world["phase_started"] = now
        player.current_level, player.x, player.y = 10, 0.18, 0.82
        player.booster_vx = player.booster_vy = 0.0
        player.booster_x = player.booster_y = 0
        player.citadel_input_sequence = -1
        player.citadel_input_at = 0.0
        player.super_spit_charge = 0
        player.super_spit_held = False
        player.super_spit_release_latched = False
        player.super_spit_cooldown_until = 0.0
        player.invulnerable_until = now + STARTING_SHIELD_SECONDS

    def _enter_level9(self, player: Player, now: float) -> None:
        self.level9_worlds[player.player_id] = self._level9_world(player.player_id)
        player.current_level = 9
        player.x, player.y = 0.45, 0.67
        player.world_progress = 0.0
        player.vx = player.vy = 0.0
        player.grounded = True
        player.run_axis = 0
        player.platformer_input_sequence = -1
        player.jump_latched = False
        player.invulnerable_until = now + STARTING_SHIELD_SECONDS

    def _enter_level8(self, player: Player, now: float) -> None:
        self.level8_worlds[player.player_id] = self._level8_world(player.player_id)
        player.current_level = 8
        player.x, player.y = LEVEL8_ENTRY_X, LEVEL8_ENTRY_Y
        player.world_progress = 0.0
        player.submarine_velocity = 0.0
        player.submarine_thrust = False
        player.submarine_input_sequence = -1
        player.oxygen = LEVEL8_OXYGEN_MAX
        player.oxygen_zero_latched = False
        player.invulnerable_until = now + STARTING_SHIELD_SECONDS

    def _enter_level7(self, player: Player, now: float) -> None:
        self.level7_worlds[player.player_id] = self._level7_world(player.player_id)
        player.current_level = 7
        player.x, player.y = LEVEL7_ENTRY_X, LEVEL7_ENTRY_Y
        player.world_progress = 0.0
        player.vehicle_velocity = 0.0
        player.vehicle_thrust = False
        player.vehicle_input_sequence = -1
        player.projectile_cooldown_until = 0.0
        player.vehicle_fire_requested = False
        player.invulnerable_until = now + STARTING_SHIELD_SECONDS

    @staticmethod
    def _build_maze_grid() -> tuple[str, ...]:
        """A deterministic closed maze: # is solid, . is a traversable cell."""
        rows = [["#"] * MAZE_COLS for _ in range(MAZE_ROWS)]
        for y in range(1, MAZE_ROWS - 1):
            for x in range(1, MAZE_COLS - 1):
                if x % 2 == 1 or y % 2 == 1:
                    rows[y][x] = "."
        # Join alternating verticals with safe horizontal corridors.
        for y in (1, 5, 9):
            for x in range(1, MAZE_COLS - 1):
                rows[y][x] = "."
        # Central pen is always open and is the only warden spawn area.
        for y in range(4, 7):
            for x in range(6, 9):
                rows[y][x] = "."
        return tuple("".join(row) for row in rows)

    def _reset_wardens(self) -> dict[str, dict[str, Any]]:
        starts = ((0.46, 0.46), (0.54, 0.46), (0.46, 0.54), (0.54, 0.54))
        return {
            f"warden-{index + 1}": {
                "id": f"warden-{index + 1}",
                "x": x,
                "y": y,
                "mode": WARDEN_MODES[index],
                "threat": 0.0,
                "direction": "up" if index < 2 else "down",
            }
            for index, (x, y) in enumerate(starts)
        }

    def _maze_orbs(self) -> dict[str, dict[str, float]]:
        open_cells = [
            (x, y)
            for y, row in enumerate(self.maze_grid)
            for x, cell in enumerate(row)
            if cell == "." and not (6 <= x <= 8 and 4 <= y <= 6)
        ]
        return {
            f"maze-orb-{index + 1}": {
                "x": BOARD_MIN + (x + 0.5) * MAZE_CELL_SIZE,
                "y": 0.06 + (y + 0.5) * (0.88 / MAZE_ROWS),
            }
            for index, (x, y) in enumerate(open_cells)
        }

    async def start(self) -> None:
        if self.orb_timer_task is None or self.orb_timer_task.done():
            self.orb_timer_task = asyncio.create_task(self._level_three_orb_timer())
        if self.hazard_timer_task is None or self.hazard_timer_task.done():
            self.hazard_timer_task = asyncio.create_task(self._hazard_timer())
        if self.opening_warning_task is None or self.opening_warning_task.done():
            self.opening_warning_task = asyncio.create_task(
                run_scheduled_checks(self.storage)
            )
        if not hasattr(self, "level7_timer_task") or self.level7_timer_task is None or self.level7_timer_task.done():
            self.level7_timer_task = asyncio.create_task(self._level7_timer())
        if self.level8_timer_task is None or self.level8_timer_task.done():
            self.level8_timer_task = asyncio.create_task(self._level8_timer())
        if self.level9_timer_task is None or self.level9_timer_task.done():
            self.level9_timer_task = asyncio.create_task(self._level9_timer())
        if self.level10_timer_task is None or self.level10_timer_task.done():
            self.level10_timer_task = asyncio.create_task(self._level10_timer())

    async def stop(self) -> None:
        tasks = tuple(
            task
            for task in (
                self.orb_timer_task,
                self.hazard_timer_task,
                self.opening_warning_task,
            )
            if task is not None
        )
        tasks = tasks + ((self.level7_timer_task,) if self.level7_timer_task is not None else ())
        tasks = tasks + ((self.level8_timer_task,) if self.level8_timer_task is not None else ())
        tasks = tasks + ((self.level9_timer_task,) if self.level9_timer_task is not None else ())
        tasks = tasks + ((self.level10_timer_task,) if self.level10_timer_task is not None else ())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.orb_timer_task = None
        self.hazard_timer_task = None
        self.opening_warning_task = None
        self.level7_timer_task = None
        self.level8_timer_task = None
        self.level9_timer_task = None
        self.level10_timer_task = None

    def level7_tick(self, dt: float = 1 / 30, now: float | None = None) -> list[dict[str, Any]]:
        """Advance each Level 7 vehicle once; dt is deliberately bounded."""
        dt = max(0.0, min(float(dt), LEVEL7_DT_CAP))
        current = asyncio.get_event_loop().time() if now is None else now
        events: list[dict[str, Any]] = []
        for websocket, player in tuple(self.connections.items()):
            if player.current_level != 7 or player.game_over:
                continue
            world = self._ensure_level7(player)
            scroll_delta = LEVEL7_AUTOSCROLL_SPEED * dt
            world["camera_progress"] += scroll_delta
            player.world_progress = world["camera_progress"]
            # World objects move toward the vehicle as the camera advances.
            for object_list in (world["orbs"],):
                for obj in object_list.values():
                    obj["x"] -= scroll_delta
            for block in world["blocks"]:
                if not block["destroyed"]:
                    block["x"] -= scroll_delta
            player.vehicle_velocity += (-LEVEL7_THRUST if player.vehicle_thrust else LEVEL7_GRAVITY) * dt
            player.vehicle_velocity = max(-0.85, min(0.85, player.vehicle_velocity))
            previous_y = player.y
            player.y += player.vehicle_velocity * dt
            ceiling, floor = 0.12, 0.88
            boundary_hit = player.y < ceiling or player.y > floor
            if boundary_hit:
                player.y = max(ceiling, min(floor, player.y))
                player.vehicle_velocity = 0.0
                if player.invulnerable_until <= current:
                    player.stunned_until = current + HAZARD_STUN_SECONDS
                    player.lives -= 1
                    player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                    event_name = "life_lost"
                    if player.lives <= 0:
                        player.lives = 0
                        player.game_over = True
                        event_name = "game_over"
                    events.append({"type": "event", "event": event_name,
                                   "player_id": player.player_id, "hazard_kind": "cavern_boundary",
                                   "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                                   "invulnerability_duration_ms": (
                                       int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                                       if event_name == "life_lost" else 0
                                   ), "lives": player.lives})
            # Projectiles are owned by this player and use swept horizontal collision.
            active_projectiles = []
            for projectile in world["projectiles"]:
                old_x = projectile["x"]
                projectile["x"] += projectile["speed"] * dt
                projectile["ttl"] -= dt
                broken = False
                for block in world["blocks"]:
                    if block["destroyed"]:
                        continue
                    if old_x <= block["x"] <= projectile["x"] and abs(block["y"] - projectile["y"]) < 0.07:
                        block["destroyed"] = True
                        broken = True
                        events.append({"type": "event", "event": "block_broken",
                                       "player_id": player.player_id, "block_id": block["id"]})
                        break
                if not broken and projectile["ttl"] > 0 and projectile["x"] < 1.3:
                    active_projectiles.append(projectile)
            world["projectiles"] = active_projectiles
            # Front bumper breaks blocks without causing damage.
            for block in world["blocks"]:
                if not block["destroyed"] and block["x"] < player.x - 0.045:
                    # An intact block is an impassable foreground obstacle.
                    block["x"] = player.x - 0.045
                if not block["destroyed"] and player.x <= block["x"] <= player.x + 0.05 and abs(block["y"] - player.y) < 0.08:
                    block["destroyed"] = True
                    events.append({"type": "event", "event": "block_broken",
                                   "player_id": player.player_id, "block_id": block["id"],
                                   "source": "bumper"})
            # Deterministic patrols, and hazard contact.
            for warden in world["wardens"]:
                phase = current * 0.9 + warden["phase"]
                warden["y"] = 0.5 + (0.22 * math.sin(phase) if warden["mode"] == "sine" else 0.25 * math.sin(phase))
                warden["x"] -= scroll_delta
                if warden["x"] < 0.45:
                    warden["x"] = 1.02
                if math.hypot(warden["x"] - player.x, warden["y"] - player.y) < 0.065 and player.invulnerable_until <= current:
                    player.lives -= 1
                    player.stunned_until = current + HAZARD_STUN_SECONDS
                    player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                    event_name = "life_lost" if player.lives > 0 else "game_over"
                    if player.lives <= 0:
                        player.lives = 0
                        player.game_over = True
                    events.append({"type": "event", "event": event_name, "player_id": player.player_id,
                                   "hazard_kind": "cavern_warden",
                                   "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                                   "invulnerability_duration_ms": (
                                       int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                                       if event_name == "life_lost" else 0
                                   ), "lives": player.lives})
            # Collection is server-only and uses the established scoring contract.
            for orb_id, orb in list(world["orbs"].items()):
                if math.hypot(orb["x"] - player.x, orb["y"] - player.y) <= 0.075:
                    del world["orbs"][orb_id]
                    player.score += 10
                    player.orbs_collected += 1
                    extra_life_awarded = player.orbs_collected % ORBS_PER_EXTRA_LIFE == 0 and player.lives < player.max_lives
                    if extra_life_awarded:
                        player.lives += 1
                    try:
                        if not player.admin_test_mode:
                            self.storage.record_score(
                                player.player_id, player.ownership_token, player.score
                            )
                    except (PermissionError, ValueError):
                        # Direct in-memory test players may not have a registered ticket.
                        pass
                    previous_level = player.current_level
                    if player.current_level != 10 and player.score >= VICTORY_SCORE and not player.victory_announced:
                        player.victory_announced = True
                        events.append({"type": "event", "event": "victory", "player_id": player.player_id,
                                       "score": player.score, "current_level": 8,
                                       "orbs_collected": player.orbs_collected, "lives": player.lives})
                    events.append({"type": "event", "event": "orb_collected", "player_id": player.player_id,
                                   "orb_id": orb_id,
                                   "score": player.score, "current_level": player.current_level,
                                   "orbs_collected": player.orbs_collected,
                                   "extra_life_awarded": extra_life_awarded, "lives": player.lives})
                    if player.score >= 1601 and player.current_level == 7:
                        self._enter_level8(player, current)
                        events.append({"type": "event", "event": "level_up",
                                       "player_id": player.player_id, "current_level": 8})
        return events

    async def _level7_timer(self) -> None:
        while True:
            await asyncio.sleep(1 / 30)
            async with self.lock:
                events = self.level7_tick(1 / 30)
                if events:
                    for event in events:
                        await self.broadcast({**event, **self.snapshot()})
                elif any(p.current_level == 7 for p in self.connections.values()):
                    await self._send_state()

    def level8_tick(self, dt: float = 1 / 30, now: float | None = None) -> list[dict[str, Any]]:
        """Advance private submarine worlds; all collection and hazards are server-owned."""
        dt = max(0.0, min(float(dt), LEVEL8_DT_CAP))
        current = asyncio.get_event_loop().time() if now is None else now
        events: list[dict[str, Any]] = []
        for player in tuple(self.connections.values()):
            if player.current_level != 8 or player.game_over:
                continue
            world = self._ensure_level8(player)
            scroll = LEVEL8_AUTOSCROLL_SPEED * dt
            world["camera_progress"] += scroll
            player.world_progress = world["camera_progress"]
            for obj in list(world["orbs"].values()) + list(world["bubbles"].values()):
                obj["x"] -= scroll
            for vent in world["vents"]:
                vent["x"] -= scroll
                if vent["x"] < -0.15:
                    vent["x"] = 1.05
            player.oxygen = max(0.0, min(LEVEL8_OXYGEN_MAX,
                player.oxygen - LEVEL8_OXYGEN_DRAIN_PER_SECOND * dt))
            if player.oxygen <= 0 and not player.oxygen_zero_latched:
                player.oxygen_zero_latched = True
                event = "oxygen_zero"
                if player.invulnerable_until <= current:
                    player.stunned_until = current + HAZARD_STUN_SECONDS
                    player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                    player.lives -= 1
                    event = "game_over" if player.lives <= 0 else "life_lost"
                    player.lives = max(0, player.lives)
                    player.game_over = player.lives == 0
                events.append({"type": "event", "event": event, "hazard_kind": "oxygen_zero",
                               "player_id": player.player_id, "oxygen": player.oxygen,
                               "lives": player.lives, "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                               "invulnerability_duration_ms": int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                               if event == "life_lost" else 0})
                player.oxygen = 35.0
                player.oxygen_zero_latched = False
            elif player.oxygen > 0:
                player.oxygen_zero_latched = False
            player.submarine_velocity += (LEVEL8_THRUST if player.submarine_thrust else LEVEL8_GRAVITY) * dt
            player.submarine_velocity = max(-LEVEL8_MAX_VELOCITY,
                                            min(LEVEL8_MAX_VELOCITY, player.submarine_velocity))
            player.y += player.submarine_velocity * dt
            ceiling = 0.14 + 0.025 * math.sin(player.world_progress * 0.7)
            floor = 0.86 + 0.025 * math.sin(player.world_progress * 0.53 + 1.2)
            if player.y < ceiling or player.y > floor:
                player.y = max(ceiling, min(floor, player.y))
                player.submarine_velocity = 0.0
                if player.invulnerable_until <= current:
                    player.stunned_until = current + HAZARD_STUN_SECONDS
                    player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                    player.lives -= 1
                    player.game_over = player.lives <= 0
                    events.append({"type": "event", "event": "game_over" if player.game_over else "life_lost",
                                   "player_id": player.player_id, "hazard_kind": "trench_boundary",
                                   "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                                   "invulnerability_duration_ms": int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                                   if not player.game_over else 0, "lives": max(0, player.lives)})
            vent_active = False
            for vent in world["vents"]:
                if vent["active"] and math.hypot(vent["x"] - player.x, vent["y"] - player.y) <= vent["radius"]:
                    player.submarine_velocity -= vent["force"] * dt
                    vent_active = True
            if vent_active and current >= world["vent_pulse_until"]:
                world["vent_pulse_until"] = current + 0.35
                events.append({"type": "event", "event": "vent_force", "player_id": player.player_id,
                               "force": "upward"})
            for bubble_id, bubble in list(world["bubbles"].items()):
                if not bubble["collected"] and math.hypot(bubble["x"] - player.x, bubble["y"] - player.y) <= LEVEL8_COLLISION_RADIUS:
                    bubble["collected"] = True
                    amount = bubble["amount"]
                    player.oxygen = min(LEVEL8_OXYGEN_MAX, player.oxygen + amount)
                    events.append({"type": "event", "event": "oxygen_refilled", "player_id": player.player_id,
                                   "bubble_id": bubble_id, "amount": amount, "oxygen": player.oxygen})
            for orb_id, orb in list(world["orbs"].items()):
                if not orb["collected"] and math.hypot(orb["x"] - player.x, orb["y"] - player.y) <= LEVEL8_ORB_RADIUS:
                    orb["collected"] = True
                    player.score += 10
                    player.orbs_collected += 1
                    extra = player.orbs_collected % ORBS_PER_EXTRA_LIFE == 0 and player.lives < player.max_lives
                    if extra:
                        player.lives += 1
                    if player.current_level != 10 and player.score >= VICTORY_SCORE and not player.victory_announced:
                        player.victory_announced = True
                        events.append({"type": "event", "event": "victory", "player_id": player.player_id,
                                       "score": player.score, "current_level": 8,
                                       "orbs_collected": player.orbs_collected, "lives": player.lives})
                    events.append({"type": "event", "event": "orb_collected", "player_id": player.player_id,
                                   "orb_id": orb_id, "score": player.score, "current_level": 8,
                                   "orbs_collected": player.orbs_collected,
                                   "extra_life_awarded": extra, "lives": player.lives})
                    if player.score >= 2000 and player.current_level == 8:
                        self._enter_level9(player, current)
                        events.append({"type": "event", "event": "level_up",
                                       "player_id": player.player_id, "current_level": 9,
                                       "transition_score": player.score})
            for warden in world["wardens"]:
                if warden["mode"] == "horizontal_patrol":
                    warden["x"] -= scroll
                    if warden["x"] < 0.35: warden["x"] = 1.02
                elif warden["mode"] == "vertical_loop":
                    warden["y"] = 0.5 + 0.27 * math.sin(current * 0.42 + warden["phase"])
                elif warden["mode"] == "stationary_guardian":
                    cone = player.x < warden["x"] and abs(player.y - warden["y"]) < 0.18
                    warden["charging"] = cone and current >= warden["cooldown_until"]
                    if warden["charging"]:
                        warden["cooldown_until"] = current + 3.0
                if math.hypot(warden["x"] - player.x, warden["y"] - player.y) < LEVEL8_COLLISION_RADIUS:
                    if player.invulnerable_until <= current:
                        player.lives -= 1
                        player.stunned_until = current + HAZARD_STUN_SECONDS
                        player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                        player.game_over = player.lives <= 0
                        events.append({"type": "event", "event": "game_over" if player.game_over else "life_lost",
                                       "player_id": player.player_id, "hazard_kind": "anglerfish_warden",
                                       "duration_ms": int(HAZARD_STUN_SECONDS * 1000), "lives": max(0, player.lives)})
        return events

    async def _level8_timer(self) -> None:
        while True:
            await asyncio.sleep(1 / 30)
            async with self.lock:
                events = self.level8_tick()
                if events:
                    for event in events:
                        await self.broadcast({**event, **self.snapshot()})
                elif any(p.current_level == 8 for p in self.connections.values()):
                    await self._send_state()

    @staticmethod
    def _aabb(x: float, y: float, w: float, h: float, obj: dict[str, Any]) -> bool:
        return (x + w / 2 > obj["x"] - obj["w"] / 2 and x - w / 2 < obj["x"] + obj["w"] / 2
                and y + h / 2 > obj["y"] - obj["h"] / 2 and y - h / 2 < obj["y"] + obj["h"] / 2)

    def level9_tick(self, dt: float = 1 / 30, now: float | None = None) -> list[dict[str, Any]]:
        dt = max(0.0, min(float(dt), LEVEL9_DT_CAP))
        current = asyncio.get_event_loop().time() if now is None else now
        events: list[dict[str, Any]] = []
        for player in tuple(self.connections.values()):
            if player.current_level != 9 or player.game_over:
                continue
            world = self._ensure_level9(player)
            for cloud in world["cloud_platforms"]:
                cloud["x"] = cloud["base_x"] + math.sin(current * .8 + cloud["phase"]) * .65
            old_y = player.y
            player.vx += (LEVEL9_RUN_ACCELERATION * player.run_axis if player.run_axis else
                          -math.copysign(min(abs(player.vx), LEVEL9_FRICTION * dt), player.vx)) * dt
            player.vx = max(-LEVEL9_RUN_SPEED, min(LEVEL9_RUN_SPEED, player.vx))
            if player.jump_latched and player.grounded:
                player.vy = LEVEL9_JUMP_IMPULSE
                player.grounded = False
            player.jump_latched = False
            player.vy = min(LEVEL9_TERMINAL_VELOCITY, player.vy + LEVEL9_GRAVITY * dt)
            next_x, next_y = player.x + player.vx * dt, player.y + player.vy * dt
            player.grounded = False
            solids = world["platforms"] + world["pipes"] + world["cloud_platforms"] + [
                block for block in world["question_blocks"] if not block["hit"]
            ]
            for solid in solids:
                if self._aabb(next_x, next_y, .18, .18, solid):
                    # Swept vertical resolution prevents tunnelling and distinguishes head hits.
                    if old_y + .09 <= solid["y"] - solid["h"] / 2 and next_y + .09 >= solid["y"] - solid["h"] / 2:
                        next_y = solid["y"] - solid["h"] / 2 - .09
                        player.vy, player.grounded = 0.0, True
                    elif old_y - .09 >= solid["y"] + solid["h"] / 2 and next_y - .09 <= solid["y"] + solid["h"] / 2:
                        next_y = solid["y"] + solid["h"] / 2 + .09
                        player.vy = abs(player.vy) * .2
                        for block in world["question_blocks"]:
                            if not block["hit"] and self._aabb(next_x, next_y - .1, .18, .18, block):
                                block["hit"] = True
                                bundle = {"id": f"{block['id']}-bundle", "x": block["x"], "y": block["y"] - .35, "count": 3}
                                world["secret_bundles"].append(bundle)
                                events.append({"type": "event", "event": "question_block_hit", "block_id": block["id"], "count": 3, "player_id": player.player_id})
                                events.append({"type": "event", "event": "block_impact", "block_id": block["id"], "player_id": player.player_id})
                    else:
                        next_x = player.x
            camera_progress = max(
                0.0,
                world["camera_progress"] + max(0.0, player.vx) * dt * .35,
                next_x - LEVEL9_CAMERA_FOLLOW_X,
            )
            min_x = camera_progress + LEVEL9_AVATAR_HALF_WIDTH
            max_x = camera_progress + 1.0 - LEVEL9_AVATAR_HALF_WIDTH
            min_y = LEVEL9_AVATAR_HALF_HEIGHT
            max_y = 1.0 - LEVEL9_AVATAR_HALF_HEIGHT
            clamped_x = max(min_x, min(max_x, next_x))
            clamped_y = max(min_y, min(max_y, next_y))
            if clamped_x != next_x and (
                (next_x < min_x and player.vx < 0)
                or (next_x > max_x and player.vx > 0)
            ):
                player.vx = 0.0
            if clamped_y != next_y and (
                (next_y < min_y and player.vy < 0)
                or (next_y > max_y and player.vy > 0)
            ):
                player.vy = 0.0
                if next_y > max_y:
                    player.grounded = True
            world["camera_progress"] = camera_progress
            player.world_progress = camera_progress
            player.x, player.y = clamped_x, clamped_y
            entered_level10 = False
            for orb_id, orb in world["orbs"].items():
                if not orb["collected"] and math.hypot(orb["x"] - player.x, orb["y"] - player.y) < .12:
                    orb["collected"] = True
                    player.score += 10
                    player.orbs_collected += 1
                    extra = player.orbs_collected % ORBS_PER_EXTRA_LIFE == 0 and player.lives < player.max_lives
                    if extra: player.lives += 1
                    events.append({"type": "event", "event": "orb_collected", "orb_id": orb_id,
                                   "player_id": player.player_id, "score": player.score, "current_level": 9,
                                   "extra_life_awarded": extra, "lives": player.lives})
                    level9_cleared = all(item["collected"] for item in world["orbs"].values())
                    if level9_cleared and player.score > 2400:
                        self._enter_level10(player, current)
                        self.level9_worlds.pop(player.player_id, None)
                        events.append({"type": "event", "event": "level_up",
                                       "player_id": player.player_id, "current_level": 10,
                                        "score": player.score,
                                        "event_metadata": {"level_clear": True, "orbs_cleared": True}})
                        entered_level10 = True
                        break
                    if player.score >= VICTORY_SCORE and not player.victory_announced and player.current_level != 10:
                        player.victory_announced = True
                        events.append({"type": "event", "event": "victory", "event_metadata": {"level_clear": True},
                                       "player_id": player.player_id, "score": player.score, "current_level": 9})
            if entered_level10:
                continue
            # Deterministic Warden movement and one hazard decision per tick.
            w1, w2, w3 = world["wardens"]
            damage_taken = False
            w1["x"] += w1["direction"] * .22 * dt
            if w1["x"] <= w1["min_x"] or w1["x"] >= w1["max_x"]: w1["direction"] *= -1
            if current >= w2["cooldown_until"]:
                world["projectiles"].append({"id": f"{w2['id']}-{int(current*30)}", "x": w2["x"], "y": w2["y"], "vy": .5, "ttl": 3.0})
                w2["cooldown_until"] = current + 2.0
            w3["x"] += w3["direction"] * .16 * dt
            if w3["x"] < 7.5 or w3["x"] > 11.0: w3["direction"] *= -1
            active = []
            for projectile in world["projectiles"]:
                projectile["y"] += projectile["vy"] * dt; projectile["ttl"] -= dt
                if projectile["ttl"] > 0 and projectile["y"] < 1.1: active.append(projectile)
                if abs(projectile["x"] - player.x) < .12 and abs(projectile["y"] - player.y) < .12:
                    if not damage_taken and player.invulnerable_until <= current:
                        damage_taken = True
                        player.lives -= 1; player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                        player.stunned_until = current + HAZARD_STUN_SECONDS; player.game_over = player.lives <= 0
                        events.append({"type": "event", "event": "game_over" if player.game_over else "life_lost",
                                       "hazard_kind": "sky_projectile", "player_id": player.player_id,
                                       "lives": max(0, player.lives),
                                       "duration_ms": int(HAZARD_STUN_SECONDS*1000),
                                       "invulnerability_duration_ms": (
                                           int(LIFE_LOSS_INVULNERABILITY_SECONDS*1000)
                                           if not player.game_over else 0)})
            world["projectiles"] = active
            if any(math.hypot(w["x"]-player.x, w["y"]-player.y) < .14 for w in (w1, w3)):
                if not damage_taken and player.invulnerable_until <= current:
                    damage_taken = True
                    player.lives -= 1; player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                    player.game_over = player.lives <= 0
                    events.append({"type": "event", "event": "game_over" if player.game_over else "life_lost",
                                   "hazard_kind": "cloud_kingdom_warden", "player_id": player.player_id,
                                   "lives": max(0, player.lives),
                                   "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                                   "invulnerability_duration_ms": (
                                       int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                                       if not player.game_over else 0)})
        return events

    async def _level9_timer(self) -> None:
        while True:
            await asyncio.sleep(1 / 30)
            async with self.lock:
                events = self.level9_tick(1 / 30)
                for event in events:
                    await self.broadcast({**event, **self.snapshot()})
                if not events and any(p.current_level == 9 for p in self.connections.values()):
                    await self._send_state()

    def level10_tick(self, dt: float = 1 / 30, now: float | None = None) -> list[dict[str, Any]]:
        dt = max(0.0, min(float(dt), LEVEL10_DT_CAP))
        current = asyncio.get_event_loop().time() if now is None else now
        events: list[dict[str, Any]] = []
        for player in tuple(self.connections.values()):
            if player.current_level != 10 or player.game_over or player.victory_announced:
                continue
            world = self._ensure_level10(player)
            if (
                player.citadel_input_at > 0
                and current - player.citadel_input_at > LEVEL10_INPUT_TIMEOUT
            ):
                player.booster_x = player.booster_y = 0
                player.super_spit_held = False
                player.super_spit_release_latched = False
            old_x, old_y = player.x, player.y
            axis_length = math.hypot(player.booster_x, player.booster_y) or 1.0
            ax = player.booster_x / axis_length * LEVEL10_ACCELERATION
            ay = player.booster_y / axis_length * LEVEL10_ACCELERATION
            player.booster_vx += ax * dt
            player.booster_vy += ay * dt
            if not ax: player.booster_vx *= max(0.0, 1.0 - LEVEL10_FRICTION * dt)
            if not ay: player.booster_vy *= max(0.0, 1.0 - LEVEL10_FRICTION * dt)
            speed = math.hypot(player.booster_vx, player.booster_vy)
            if speed > LEVEL10_MAX_SPEED:
                factor = LEVEL10_MAX_SPEED / speed
                player.booster_vx *= factor; player.booster_vy *= factor
            player.x += player.booster_vx * dt; player.y += player.booster_vy * dt
            bounds = world["bounds"]
            hit_boundary = not (bounds["min_x"] <= player.x <= bounds["max_x"] and bounds["min_y"] <= player.y <= bounds["max_y"])
            player.x = max(bounds["min_x"], min(bounds["max_x"], player.x))
            player.y = max(bounds["min_y"], min(bounds["max_y"], player.y))
            # Charge is exclusively earned by overlap with server-created orbs.
            for orb in world["orbs"].values():
                if not orb["collected"] and math.hypot(orb["x"] - player.x, orb["y"] - player.y) <= .065:
                    orb["collected"] = True
                    player.score += 10; player.orbs_collected += 1
                    player.super_spit_charge = min(100, player.super_spit_charge + 10)
                    extra = player.orbs_collected % ORBS_PER_EXTRA_LIFE == 0 and player.lives < player.max_lives
                    if extra: player.lives += 1
                    events.append({"type": "event", "event": "orb_collected", "orb_id": orb["id"],
                                   "player_id": player.player_id, "score": player.score,
                                   "current_level": 10, "charge": player.super_spit_charge,
                                   "extra_life_awarded": extra, "lives": player.lives})
            boss = world["boss"]
            boss["vulnerable"] = current >= boss["shield_until"] and int(current * 2) % 4 < 3
            phase = 1 if boss["hp"] >= 201 else 2 if boss["hp"] >= 101 else 3
            if phase != boss["phase"]:
                boss["phase"] = phase
                world["klaxons"].append({"phase": phase, "at": current})
                world["klaxons"] = world["klaxons"][-3:]
                events.append({"type": "event", "event": "phase_klaxon", "phase": phase,
                               "player_id": player.player_id, "cue": "klaxon"})
            # Deterministic attack metadata and collision primitives.
            t = current - world["phase_started"]
            world["beams"] = [{"id": "energy-beam-a", "angle": (t * 1.2) % (math.pi * 2),
                               "telegraph": True, "radius": .018},
                              {"id": "energy-beam-b", "angle": ((t * 1.2) + math.pi) % (math.pi * 2),
                               "telegraph": True, "radius": .018}]
            world["traps"] = [{"id": f"floor-trap-{i}", "x": .22 + i * .18,
                               "y": .72 + .06 * math.sin(t * .8 + i), "active": int(t * 2 + i) % 3 != 0,
                               "telegraph": True} for i in range(4)]
            if phase == 1:
                world["hazards"] = [{"kind": "circular_ring", "radius": .12 + (t * .16 % .46),
                                     "x": .5, "y": .42, "telegraph": True}]
            elif phase == 2:
                world["hazards"] = [{"kind": "seeking_laser", "x": .5, "y": .42,
                                     "target_x": player.x, "target_y": player.y, "telegraph": True}]
            else:
                world["hazards"] = [{"kind": "grid_sweep", "axis": "x" if int(t * 2) % 2 else "y",
                                     "position": .2 + (t * .45 % .6), "telegraph": True}]
            for drone in world["drones"]:
                angle = current * .55 + drone["phase"]
                drone["x"] = .5 + math.cos(angle) * .38
                drone["y"] = .5 + math.sin(angle) * .34
            damaged = hit_boundary or any(math.hypot(player.x - d["x"], player.y - d["y"]) < .065 for d in world["drones"])
            if damaged and player.invulnerable_until <= current:
                player.lives -= 1; player.stunned_until = current + HAZARD_STUN_SECONDS
                player.invulnerable_until = current + LIFE_LOSS_INVULNERABILITY_SECONDS
                event = "game_over" if player.lives <= 0 else "life_lost"
                player.lives = max(0, player.lives); player.game_over = event == "game_over"
                events.append({"type": "event", "event": event, "player_id": player.player_id,
                               "hazard_kind": "citadel_boundary" if hit_boundary else "citadel_drone",
                               "lives": player.lives, "duration_ms": int(HAZARD_STUN_SECONDS * 1000)})
            # Release edge is latched by the sequenced input handler.
            if player.super_spit_release_latched:
                player.super_spit_release_latched = False
                if player.super_spit_charge >= LEVEL10_SHOT_COST and current >= player.super_spit_cooldown_until:
                    player.super_spit_charge -= LEVEL10_SHOT_COST
                    player.super_spit_cooldown_until = current + LEVEL10_SHOT_COOLDOWN
                    world["projectiles"].append({"id": f"{player.player_id}-{player.citadel_input_sequence}",
                                                 "x": player.x, "y": player.y, "ttl": 2.0})
                    events.append({"type": "event", "event": "super_spit", "player_id": player.player_id,
                                   "charge": player.super_spit_charge, "phase": boss["phase"]})
            for projectile in list(world["projectiles"]):
                dx, dy = boss["x"] - projectile["x"], boss["y"] - projectile["y"]
                distance = math.hypot(dx, dy) or 1.0
                projectile["x"] += dx / distance * 1.5 * dt
                projectile["y"] += dy / distance * 1.5 * dt
                projectile["ttl"] -= dt
                if projectile["ttl"] <= 0:
                    world["projectiles"].remove(projectile); continue
                # Server-owned projectile always homes to the static boss.
                if math.hypot(projectile["x"] - boss["x"], projectile["y"] - boss["y"]) < .18:
                    world["projectiles"].remove(projectile)
                    if boss["vulnerable"]:
                        boss["hp"] = max(0, boss["hp"] - LEVEL10_SHOT_DAMAGE)
                        events.append({"type": "event", "event": "boss_hit", "player_id": player.player_id,
                                       "boss_hp": boss["hp"], "phase": boss["phase"]})
                        if boss["hp"] == 0 and not player.victory_announced:
                            player.victory_announced = True
                            events.append({"type": "event", "event": "victory", "player_id": player.player_id,
                                           "score": player.score, "current_level": 10,
                                           "event_metadata": {"citadel_complete": True, "level_clear": True,
                                                              "boss_phase": boss["phase"]},
                                           "cue": "grand_fanfare"})
            world["projectiles"] = world["projectiles"][-64:]
            world["last_tick"] = current
        return events

    async def _level10_timer(self) -> None:
        while True:
            await asyncio.sleep(1 / 30)
            async with self.lock:
                events = self.level10_tick(1 / 30)
                for event in events:
                    await self.broadcast({**event, **self.snapshot()})
                if not events and any(p.current_level == 10 for p in self.connections.values()):
                    await self._send_state()

    def _record_opening_once(self, websocket: WebSocket, milestone: str) -> None:
        player = self.connections.get(websocket)
        if player is not None and player.admin_test_mode:
            return
        recorded = self.opening_milestones.setdefault(websocket, set())
        if milestone in recorded:
            return
        try:
            self.storage.record_opening_milestone(OPENING_VERSION, milestone)
        except (OSError, sqlite3.Error):
            LOG.exception(
                "opening_milestone_not_recorded milestone=%s",
                milestone,
            )
            return
        recorded.add(milestone)

    def andean_pumas(
        self, now: float | None = None, *, active_override: bool | None = None,
        hazard_started_at: float | None = None,
    ) -> list[dict[str, Any]]:
        current = asyncio.get_event_loop().time() if now is None else now
        active = (
            active_override
            if active_override is not None
            else any(
                player.current_level == 1 and not player.admin_test_mode
                for player in self.connections.values()
            )
        )
        elapsed = max(0.0, current - (
            self.hazard_started_at if hazard_started_at is None else hazard_started_at
        ))
        pumas = []
        for index, y_pos in enumerate((0.26, 0.51, 0.76)):
            direction = 1 if index % 2 == 0 else -1
            progress = (elapsed * (0.16 + index * 0.022) + index * 0.31) % 1.0
            x_pos = 0.08 + 0.84 * (progress if direction > 0 else 1.0 - progress)
            pumas.append({
                "id": f"andean-puma-{index}",
                "kind": "puma",
                "active": active,
                "x": x_pos,
                "y": y_pos + math.sin(elapsed * 2.0 + index * 1.7) * 0.018,
                "direction": direction,
                "radius": PUMA_RADIUS,
            })
        return pumas

    def woodland_logs(
        self, now: float | None = None, *, active_override: bool | None = None,
        hazard_started_at: float | None = None,
    ) -> list[dict[str, Any]]:
        current = asyncio.get_event_loop().time() if now is None else now
        active = (
            active_override
            if active_override is not None
            else any(
                player.current_level == 2 and not player.admin_test_mode
                for player in self.connections.values()
            )
        )
        elapsed = max(0.0, current - (
            self.hazard_started_at if hazard_started_at is None else hazard_started_at
        ))
        logs = []
        for index, x_pos in enumerate((0.2, 0.5, 0.8)):
            y_pos = (elapsed * (0.3 + index * 0.1) + index * 0.3) % 1.0
            logs.append({
                "id": f"woodland-log-{index}",
                "kind": "log",
                "active": active,
                "x": x_pos,
                "y": y_pos,
                "radius": LOG_RADIUS,
            })
        return logs

    def farm_tractors(
        self, now: float | None = None, *, active_override: bool | None = None,
        hazard_started_at: float | None = None,
    ) -> list[dict[str, Any]]:
        current = asyncio.get_event_loop().time() if now is None else now
        active = (
            active_override
            if active_override is not None
            else any(
                player.current_level == 3 and not player.admin_test_mode
                for player in self.connections.values()
            )
        )
        elapsed = max(0.0, current - (
            self.hazard_started_at if hazard_started_at is None else hazard_started_at
        ))
        tractors = []
        for index in range(2):
            angle = elapsed * 0.5 + index * math.pi
            x_pos = 0.5 + 0.3 * math.cos(angle)
            y_pos = 0.5 + 0.3 * math.sin(angle)
            tractors.append({
                "id": f"farm-tractor-{index}",
                "kind": "tractor",
                "active": active,
                "x": x_pos,
                "y": y_pos,
                "radius": TRACTOR_RADIUS,
            })
        return tractors

    def castle_ghosts(
        self, now: float | None = None, *, active_override: bool | None = None,
        hazard_started_at: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return deterministic Stage 4 ghost positions from the server clock."""

        current = asyncio.get_event_loop().time() if now is None else now
        active = (
            active_override
            if active_override is not None
            else any(
                player.current_level == 4 and not player.admin_test_mode
                for player in self.connections.values()
            )
        )
        elapsed = max(0.0, current - (
            self.hazard_started_at if hazard_started_at is None else hazard_started_at
        ))
        ghosts = []
        for index, phase in enumerate((0.0, 2.1, 4.2), start=1):
            angle = elapsed * (0.72 + index * 0.08) + phase
            ghosts.append(
                {
                    "id": f"castle-ghost-{index}",
                    "active": active,
                    "x": 0.5 + math.cos(angle) * (0.24 + index * 0.025),
                    "y": 0.5 + math.sin(angle * 1.17) * (0.20 + index * 0.018),
                    "radius": GHOST_COLLISION_RADIUS,
                }
            )
        return ghosts

    def _warden_target(self, warden: dict[str, Any], target: Player | None) -> tuple[float, float, str]:
        if target is None:
            return warden["x"], warden["y"], "pen"
        mode = warden["mode"]
        if mode == "aggressive":
            return target.x, target.y, "current_player"
        if mode == "ambush":
            direction = target.movement_direction
            offsets = {"up": (0, -4), "down": (0, 4), "left": (-4, 0), "right": (4, 0)}
            ox, oy = offsets.get(direction, (4, 0))
            return target.x + ox * MAZE_CELL_SIZE, target.y + oy * (0.88 / MAZE_ROWS), "four_tiles_ahead"
        if mode == "mimic":
            return target.x, target.y, f"movement_{target.movement_direction}"
        # Stable quadrant patrol: each warden owns a quadrant and visits its center.
        quadrant = ((0.26, 0.26), (0.74, 0.26), (0.26, 0.74), (0.74, 0.74))[
            int(warden["id"].split("-")[-1]) - 1
        ]
        return *quadrant, "quadrant_patrol"

    def _advance_wardens(
        self,
        now: float | None = None,
        *,
        wardens: dict[str, dict[str, Any]] | None = None,
        target: Player | None = None,
    ) -> None:
        active_wardens = self.wardens if wardens is None else wardens
        if target is None:
            target = next(
                (
                    p for p in self.connections.values()
                    if p.current_level in {5, 6}
                    and not p.game_over
                    and not p.admin_test_mode
                ),
                None,
            )
        cell_h = 0.88 / MAZE_ROWS
        for warden in active_wardens.values():
            tx, ty, rule = self._warden_target(warden, target)
            warden["target_rule"] = rule
            warden["target_x"], warden["target_y"] = tx, ty
            col = round((warden["x"] - BOARD_MIN) / MAZE_CELL_SIZE - 0.5)
            row = round((warden["y"] - 0.06) / cell_h - 0.5)
            target_col = max(0, min(MAZE_COLS - 1, int((tx - BOARD_MIN) / MAZE_CELL_SIZE)))
            target_row = max(0, min(MAZE_ROWS - 1, int((ty - 0.06) / cell_h)))
            candidates = []
            if target_col != col:
                candidates.append((col + (1 if target_col > col else -1), row, "right" if target_col > col else "left"))
            if target_row != row:
                candidates.append((col, row + (1 if target_row > row else -1), "down" if target_row > row else "up"))
            # Cardinal movement with deterministic tie-breaking and no diagonal traversal.
            candidates.extend(((col + 1, row, "right"), (col - 1, row, "left"),
                               (col, row + 1, "down"), (col, row - 1, "up")))
            for next_col, next_row, direction in candidates:
                if 0 <= next_col < MAZE_COLS and 0 <= next_row < MAZE_ROWS and self.maze_grid[next_row][next_col] == ".":
                    warden["x"] = BOARD_MIN + (next_col + 0.5) * MAZE_CELL_SIZE
                    warden["y"] = 0.06 + (next_row + 0.5) * cell_h
                    warden["direction"] = direction
                    break
            warden["threat"] = round(
                max(0.0, 1.0 - math.hypot(
                    (target.x if target else 0.5) - warden["x"],
                    (target.y if target else 0.5) - warden["y"],
                ) / 0.35), 3
            )

    def advance_wardens(self, now: float | None = None) -> None:
        """Advance the authoritative warden simulation exactly one server tick."""
        normal_maze = any(
            player.current_level in {5, 6} and not player.admin_test_mode
            for player in self.connections.values()
        )
        admin_active = any(
            player.current_level in {1, 2, 3, 4, 5, 6}
            and player.admin_test_mode
            for player in self.connections.values()
        )
        if admin_active and not normal_maze:
            return
        self._advance_wardens(now)

    def _advance_admin_hazards(self, now: float | None = None) -> None:
        current = asyncio.get_event_loop().time() if now is None else now
        for websocket, player in tuple(self.connections.items()):
            if not player.admin_test_mode or player.current_level > 6 or player.game_over:
                continue
            state = self._admin_hazard_state(player, current)
            state["level"] = player.current_level
            if player.current_level in {5, 6}:
                self._advance_wardens(
                    current,
                    wardens=state["wardens"],
                    target=player,
                )

    def _hazards_for_player(
        self, player: Player, now: float | None = None
    ) -> list[dict[str, Any]]:
        current = asyncio.get_event_loop().time() if now is None else now
        if not player.admin_test_mode or player.current_level > 6:
            if player.current_level == 1:
                return self.andean_pumas(current)
            if player.current_level == 2:
                return self.woodland_logs(current)
            if player.current_level == 3:
                return self.farm_tractors(current)
            if player.current_level == 4:
                return self.castle_ghosts(current)
            if player.current_level in {5, 6}:
                return self.labyrinth_hazards(current)
            return []
        state = self._admin_hazard_state(player, current)
        started = state["hazard_started_at"]
        if player.current_level == 1:
            return self.andean_pumas(
                current, active_override=True, hazard_started_at=started
            )
        if player.current_level == 2:
            return self.woodland_logs(
                current, active_override=True, hazard_started_at=started
            )
        if player.current_level == 3:
            return self.farm_tractors(
                current, active_override=True, hazard_started_at=started
            )
        if player.current_level == 4:
            return self.castle_ghosts(
                current, active_override=True, hazard_started_at=started
            )
        return self.labyrinth_hazards(
            current,
            active_override=True,
            wardens=state["wardens"],
            target=player,
        )

    def labyrinth_hazards(
        self,
        now: float | None = None,
        *,
        active_override: bool | None = None,
        wardens: dict[str, dict[str, Any]] | None = None,
        target: Player | None = None,
    ) -> list[dict[str, Any]]:
        """Return the closed Level 6 maze and server-controlled Hazard Wardens."""

        current = asyncio.get_event_loop().time() if now is None else now
        active = (
            active_override
            if active_override is not None
            else any(
                player.current_level in {5, 6} and not player.admin_test_mode
                for player in self.connections.values()
            )
        )
        active_wardens = self.wardens if wardens is None else wardens
        hazards: list[dict[str, Any]] = []
        cell_h = 0.88 / MAZE_ROWS
        for y, row in enumerate(self.maze_grid):
            for x, cell in enumerate(row):
                if cell == "#":
                    hazards.append({
                        "id": f"maze-wall-{x}-{y}", "kind": "maze_wall",
                        "active": active, "x": BOARD_MIN + (x + 0.5) * MAZE_CELL_SIZE,
                        "y": 0.06 + (y + 0.5) * cell_h, "radius": MAZE_WALL_RADIUS,
                    })
        for index, warden in enumerate(active_wardens.values()):
            warden_target = target
            if warden_target is None:
                warden_target = next(
                    (
                        p for p in self.connections.values()
                        if p.current_level in {5, 6}
                        and not p.game_over
                        and not p.admin_test_mode
                    ),
                    None,
                )
            _, _, target_rule = self._warden_target(warden, warden_target)
            hazards.append({
                **warden, "target_rule": warden.get("target_rule", target_rule),
                "kind": "maze_warden", "active": active,
                "radius": MAZE_WARDEN_RADIUS,
            })
        return hazards

    def _maze_position_open(self, x: float, y: float) -> bool:
        col = int((x - BOARD_MIN) / MAZE_CELL_SIZE)
        row = int((y - 0.06) / (0.88 / MAZE_ROWS))
        return 0 <= col < MAZE_COLS and 0 <= row < MAZE_ROWS and self.maze_grid[row][col] == "."

    @staticmethod
    def movement_intersects_hazard(
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        hazard: dict[str, Any],
    ) -> bool:
        """Measure the closest point on a movement segment to a circular hazard."""

        if not hazard.get("active"):
            return False

        dx, dy = end_x - start_x, end_y - start_y
        length_squared = dx * dx + dy * dy
        if length_squared == 0:
            closest_x, closest_y = start_x, start_y
        else:
            projection = (
                (float(hazard["x"]) - start_x) * dx
                + (float(hazard["y"]) - start_y) * dy
            ) / length_squared
            projection = max(0.0, min(1.0, projection))
            closest_x = start_x + projection * dx
            closest_y = start_y + projection * dy
        return math.hypot(closest_x - float(hazard["x"]), closest_y - float(hazard["y"])) <= float(
            hazard["radius"]
        )

    async def _hazard_tick(self, now: float | None = None) -> None:
        current = asyncio.get_event_loop().time() if now is None else now
        normal_active = any(
            player.current_level in {1, 2, 3, 4, 5, 6}
            and not player.admin_test_mode
            for player in self.connections.values()
        )
        if normal_active:
            self._advance_wardens(current)
            await self.broadcast(
                {"type": "event", "event": "hazard_update", **self.snapshot()},
                include_admin=False,
            )
        self._advance_admin_hazards(current)
        for websocket, player in tuple(self.connections.items()):
            if player.admin_test_mode and player.current_level <= 6:
                await self._send_personalized(
                    websocket,
                    {"type": "event", "event": "hazard_update", **self.snapshot()},
                )

    async def _hazard_timer(self) -> None:
        while True:
            await asyncio.sleep(HAZARD_BROADCAST_SECONDS)
            async with self.lock:
                await self._hazard_tick()

    def relocate_level_three_orbs(self) -> None:
        normal_level3 = any(
            player.current_level == 3 and not player.admin_test_mode
            for player in self.connections.values()
        )
        admin_level3 = any(
            player.current_level == 3 and player.admin_test_mode
            for player in self.connections.values()
        )
        if admin_level3 and not normal_level3:
            return
        self.orb_relocation_tick += 1
        self._apply_level_three_relocation(self.collectibles, self.orb_relocation_tick)

    @staticmethod
    def _apply_level_three_relocation(
        collectibles: dict[str, dict[str, float]], tick: float
    ) -> None:
        for index, orb_id in enumerate(collectibles, start=1):
            collectibles[orb_id] = {
                "x": 0.12 + ((tick * 23 + index * 17) % 76) / 100,
                "y": 0.12 + ((tick * 31 + index * 29) % 76) / 100,
            }

    def _advance_admin_level_three_relocations(self, now: float) -> list[WebSocket]:
        relocated: list[WebSocket] = []
        for websocket, player in tuple(self.connections.items()):
            if not player.admin_test_mode or player.current_level != 3:
                continue
            state = self.admin_relocations.get(player.player_id)
            if state is None:
                self._initialize_admin_relocation(player, now)
                state = self.admin_relocations[player.player_id]
            if now - state["last_relocation_at"] < LEVEL_THREE_ORB_RELOCATE_SECONDS:
                continue
            state["tick"] += 1
            state["last_relocation_at"] = now
            self._apply_level_three_relocation(
                self.admin_collectibles[player.player_id], state["tick"]
            )
            relocated.append(websocket)
        return relocated

    async def _level_three_orb_tick(self, now: float | None = None) -> None:
        current = asyncio.get_event_loop().time() if now is None else now
        if any(
            player.current_level == 3 and not player.admin_test_mode
            for player in self.connections.values()
        ):
            self.relocate_level_three_orbs()
            await self.broadcast(
                {"type": "event", "event": "orbs_relocated", **self.snapshot()},
                include_admin=False,
            )
        relocated = self._advance_admin_level_three_relocations(current)
        for websocket in relocated:
            await self._send_personalized(
                websocket,
                {
                    "type": "event",
                    "event": "orbs_relocated",
                    **self.snapshot(),
                },
            )

    async def _level_three_orb_timer(self) -> None:
        while True:
            await asyncio.sleep(LEVEL_THREE_ORB_RELOCATE_SECONDS)
            async with self.lock:
                await self._level_three_orb_tick()

    async def connect(self, websocket: WebSocket) -> bool:
        if not admin_security.bind_websocket(websocket):
            await websocket.close(code=1008, reason="Session is already connected.")
            return False
        await websocket.accept()
        LOG.info("game_socket_connected active_connections=%d", len(self.connections) + 1)
        return True

    async def disconnect(self, websocket: WebSocket) -> None:
        player = self.connections.pop(websocket, None)
        self.opening_milestones.pop(websocket, None)
        admin_security.disconnect(websocket)
        if player:
            self.utility_purchase_pending.discard(player.player_id)
            self.utility_purchase_attempts.pop(player.player_id, None)
            self.level7_worlds.pop(player.player_id, None)
            self.level8_worlds.pop(player.player_id, None)
            self.level9_worlds.pop(player.player_id, None)
            self.level10_worlds.pop(player.player_id, None)
            self.admin_collectibles.pop(player.player_id, None)
            self.admin_hazards.pop(player.player_id, None)
            self.admin_relocations.pop(player.player_id, None)
            if not player.admin_test_mode:
                try:
                    self.storage.record_score(
                        player.player_id, player.ownership_token, player.score
                    )
                    self.storage.complete_match_ticket(
                        player.ticket_id,
                        player.player_id,
                        player.ownership_token,
                        final_score=player.score,
                    )
                except (PermissionError, ValueError):
                    pass
            LOG.info(
                "game_player_disconnected player_id=%s active_players=%d",
                player.player_id,
                len(self.connections),
            )
            await self.broadcast({"type": "state", **self.snapshot()})

    async def handle_message(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        message_type = payload.get("type")
        if message_type == "purchase_extra_life":
            await self._purchase_extra_life(websocket, payload)
            return
        async with self.lock:
            if message_type == "join":
                await self._join(websocket, payload)
            elif websocket not in self.connections:
                await websocket.send_json(
                    {"type": "error", "message": "Join the room before sending game events."}
                )
            elif message_type == "reset":
                await self._reset(websocket)
            elif message_type == "admin_select_level":
                await self._admin_select_level(websocket, payload)
            elif message_type == "admin_configure_test":
                await self._admin_configure_test(websocket, payload)
            elif message_type == "equip_skin":
                await self._equip_skin(websocket, payload)
            elif message_type == "position":
                player = self.connections[websocket]
                if player.game_over:
                    await self._send_state()
                    return
                level = level_for_score(player.score)
                player.current_level = level.number
                if level.number in {7, 8, 9, 10}:
                    # Coordinates are never accepted for the vehicle.
                    await self._send_state()
                    return
                try:
                    raw_x = float(payload.get("x"))
                    raw_y = float(payload.get("y"))
                except (TypeError, ValueError):
                    raw_x, raw_y = player.x, player.y
                wrapped = False
                if level.number == 6:
                    span = level.board_max - level.board_min
                    tunnel_y = int((player.y - 0.06) / (0.88 / MAZE_ROWS))
                    if raw_x < level.board_min or raw_x > level.board_max:
                        if tunnel_y != MAZE_TUNNEL_ROW:
                            await self._send_state()
                            return
                        proposed_x = (
                            level.board_max - 0.001
                            if raw_x < level.board_min
                            else level.board_min + 0.001
                        )
                        wrapped = True
                    else:
                        proposed_x = raw_x
                else:
                    proposed_x = _clamp_coordinate(
                        raw_x, player.x, level.board_min, level.board_max
                    )
                proposed_y = _clamp_coordinate(
                    raw_y, player.y, level.board_min, level.board_max
                )
                now = asyncio.get_event_loop().time()
                if player.stunned_until > now:
                    await self._send_state()
                    return
                hazards = self._hazards_for_player(player, now)

                if level.number == 6:
                    if not self._maze_position_open(proposed_x, proposed_y):
                        await self._send_state()
                        return
                    player.movement_direction = (
                        "right" if proposed_x > player.x else
                        "left" if proposed_x < player.x else
                        "down" if proposed_y > player.y else "up"
                    )

                for hazard in hazards:
                    if hazard.get("kind") == "maze_wall":
                        continue
                    if hazard.get("active") and self.movement_intersects_hazard(
                        proposed_x if level.number == 6 and wrapped else player.x,
                        player.y,
                        proposed_x,
                        proposed_y,
                        hazard,
                    ):
                        if player.invulnerable_until > now:
                            continue
                        player.stunned_until = now + HAZARD_STUN_SECONDS
                        player.bump_count += 1
                        if level.number == 1 and hazard.get("kind") == "puma":
                            # Keep the historical storage key for schema compatibility;
                            # opening_version separates puma data from legacy chickens.
                            self._record_opening_once(websocket, "chicken_collision")
                        player.bump_count = 0
                        player.lives -= 1
                        player.invulnerable_until = (
                            now + LIFE_LOSS_INVULNERABILITY_SECONDS
                        )
                        player.x = 0.5
                        player.y = 0.5
                        event = "life_lost"
                        if player.lives <= 0:
                            player.lives = 0
                            player.game_over = True
                            player.invulnerable_until = 0.0
                            event = "game_over"
                        collision_message = {
                            "type": "event",
                            "event": event,
                            "player_id": player.player_id,
                            "hazard_kind": hazard.get("kind", "hazard"),
                            "duration_ms": int(HAZARD_STUN_SECONDS * 1000),
                            "invulnerability_duration_ms": (
                                int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000)
                                if event == "life_lost"
                                else 0
                            ),
                            "lives": player.lives,
                            "bump_count": player.bump_count,
                            **self.snapshot(now),
                        }
                        if player.admin_test_mode:
                            await self._send_personalized(websocket, collision_message)
                        else:
                            await self.broadcast(collision_message)
                        return

                player.x = proposed_x
                player.y = proposed_y
                await self._send_state()
            elif message_type == "level_one_chip":
                await self._collect_level_one_chip(websocket, payload)
            elif message_type == "level_one_complete":
                await self._complete_level_one(websocket)
            elif message_type == "vehicle_input":
                player = self.connections[websocket]
                if player.game_over or level_for_score(player.score).number != 7:
                    return
                try:
                    sequence = int(payload.get("sequence"))
                except (TypeError, ValueError):
                    return
                if sequence <= player.vehicle_input_sequence:
                    return
                player.vehicle_input_sequence = sequence
                player.vehicle_thrust = bool(payload.get("thrust", False))
                now = asyncio.get_event_loop().time()
                if bool(payload.get("fire", False)) and now >= player.projectile_cooldown_until:
                    world = self._ensure_level7(player)
                    world["projectiles"].append({
                        "id": f"{player.player_id}-spit-{sequence}",
                        "x": player.x + 0.04, "y": player.y,
                        "speed": 1.25, "ttl": 1.4,
                    })
                    player.projectile_cooldown_until = now + LEVEL7_FIRE_COOLDOWN
                await self._send_state()
            elif message_type == "submarine_input":
                player = self.connections[websocket]
                if player.game_over or level_for_score(player.score).number != 8:
                    return
                try:
                    sequence = int(payload.get("sequence"))
                except (TypeError, ValueError):
                    return
                if sequence <= player.submarine_input_sequence:
                    return
                player.submarine_input_sequence = sequence
                player.submarine_thrust = bool(payload.get("thrust", False))
                await self._send_state()
            elif message_type == "platformer_input":
                player = self.connections[websocket]
                if player.game_over or player.current_level != 9:
                    return
                try:
                    sequence = int(payload.get("sequence"))
                    axis = int(payload.get("run_axis"))
                except (TypeError, ValueError):
                    return
                if axis not in (-1, 0, 1) or sequence <= player.platformer_input_sequence:
                    return
                player.platformer_input_sequence = sequence
                player.run_axis = axis
                if bool(payload.get("jump_pressed", False)):
                    player.jump_latched = bool(player.grounded)
                await self._send_state()
            elif message_type == "citadel_input":
                player = self.connections[websocket]
                if player.game_over or player.current_level != 10:
                    return
                try:
                    sequence = int(payload.get("sequence"))
                    axes = (int(payload.get("booster_x")), int(payload.get("booster_y")))
                except (TypeError, ValueError):
                    return
                if sequence < 0 or sequence <= player.citadel_input_sequence or any(axis not in (-1, 0, 1) for axis in axes):
                    return
                if not isinstance(payload.get("charge_pressed"), bool) or not isinstance(payload.get("fire_released"), bool):
                    return
                player.citadel_input_sequence = sequence
                player.citadel_input_at = asyncio.get_event_loop().time()
                player.booster_x, player.booster_y = axes
                player.super_spit_held = payload["charge_pressed"]
                player.super_spit_release_latched = payload["fire_released"] and not player.super_spit_held
                await self._send_state()
            elif message_type == "collect":
                await self._collect(websocket, str(payload.get("orb_id", "")))
            else:
                await websocket.send_json({"type": "error", "message": "Unknown game event."})

    async def _admin_select_level(
        self, websocket: WebSocket, payload: dict[str, Any]
    ) -> None:
        """Consume the one-use grant and reset only this socket's player."""

        if set(payload) != {"type", "level"}:
            await websocket.send_json(
                {"type": "error", "message": "Administrative target fields are not accepted."}
            )
            return
        level = payload.get("level")
        if type(level) is not int or not 1 <= level <= len(LEVELS):
            await websocket.send_json(
                {"type": "error", "message": "Administrative level selection is invalid."}
            )
            return
        player = self.connections.get(websocket)
        if player is None:
            await websocket.send_json(
                {"type": "error", "message": "Join the room before sending game events."}
            )
            return
        if not admin_security.consume(websocket, player):
            LOG.warning(
                "admin_override_failure reason=grant_invalid player_hash=%s",
                AdminPortalSecurity._hash(player.player_id)[:16],
            )
            await websocket.send_json(
                {"type": "error", "message": "Administrative authorization failed."}
            )
            return
        await self._admin_override_level_locked(websocket, level)

    async def _admin_configure_test(
        self, websocket: WebSocket, payload: dict[str, Any]
    ) -> None:
        if set(payload) != {"type", "level", "test_skin_id"}:
            await websocket.send_json(
                {"type": "error", "message": "Administrative test settings are invalid."}
            )
            return
        level = payload.get("level")
        test_skin_id = payload.get("test_skin_id")
        player = self.connections.get(websocket)
        if (
            type(level) is not int
            or not 1 <= level <= len(LEVELS)
            or not isinstance(test_skin_id, str)
            or test_skin_id not in {"", *(skin_id for skin_id in SKIN_STATE_TABLE if skin_id != "default")}
            or player is None
            or not admin_security.consume(websocket, player)
        ):
            await websocket.send_json(
                {"type": "error", "message": "Administrative authorization failed."}
            )
            return
        await self._admin_override_level_locked(websocket, level)
        player.admin_test_skin_ids = (test_skin_id,) if test_skin_id else ()
        if player.equipped_skin_id not in {
            *player.owned_skin_ids,
            *player.admin_test_skin_ids,
        }:
            player.equipped_skin_id = "default"
        LOG.info(
            "admin_cosmetic_test_configured enabled=%s player_hash=%s",
            bool(test_skin_id),
            AdminPortalSecurity._hash(player.player_id)[:16],
        )
        await self._send_state()

    async def _equip_skin(
        self, websocket: WebSocket, payload: dict[str, Any]
    ) -> None:
        if set(payload) != {"type", "skin_id"}:
            await websocket.send_json(
                {"type": "error", "message": "Cosmetic selection is invalid."}
            )
            return
        player = self.connections.get(websocket)
        skin_id = payload.get("skin_id")
        if player is None or not isinstance(skin_id, str) or skin_id not in SKIN_STATE_TABLE:
            await websocket.send_json(
                {"type": "error", "message": "Cosmetic selection is invalid."}
            )
            return
        allowed = {*player.owned_skin_ids, *player.admin_test_skin_ids}
        if skin_id not in allowed:
            await websocket.send_json(
                {"type": "error", "message": "Purchase this cosmetic before equipping it."}
            )
            return
        if skin_id not in player.admin_test_skin_ids:
            try:
                player.owned_skin_ids = self.storage.equip_skin(
                    player.player_id, player.ownership_token, skin_id
                )
            except PermissionError:
                await websocket.send_json(
                    {"type": "error", "message": "Cosmetic ownership could not be verified."}
                )
                return
        player.equipped_skin_id = skin_id
        await self.broadcast(
            {
                "type": "event",
                "event": "skin_equipped",
                "player_id": player.player_id,
                "skin_id": skin_id,
                **self.snapshot(),
            }
        )

    async def _purchase_extra_life(
        self, websocket: WebSocket, payload: dict[str, Any]
    ) -> None:
        signature = _safe_solana_value(payload.get("signature"), minimum=64, maximum=96)
        wallet = _safe_solana_value(payload.get("wallet_address"), minimum=32, maximum=44)
        oracle_quote = str(payload.get("oracle_quote", ""))
        item = next(
            (entry for entry in PAYMENT_CATALOG if entry["id"] == EXTRA_LIFE_ITEM_ID),
            None,
        )
        async with self.lock:
            player = self.connections.get(websocket)
            now = time.monotonic()
            attempts = (
                self.utility_purchase_attempts.setdefault(player.player_id, deque())
                if player is not None
                else deque()
            )
            while attempts and attempts[0] < now - 60:
                attempts.popleft()
            if (
                set(payload)
                != {"type", "item_id", "signature", "wallet_address", "oracle_quote"}
                or player is None
                or player.admin_test_mode
                or player.game_over
                or payload.get("item_id") != EXTRA_LIFE_ITEM_ID
                or item is None
                or not player.extra_life_unlocked
                or player.extra_lives_purchased >= MAX_EXTRA_LIFE_PURCHASES
                or signature is None
                or wallet is None
                or not 80 <= len(oracle_quote) <= 1_500
                or player.player_id in self.utility_purchase_pending
                or len(attempts) >= 5
            ):
                await websocket.send_json(
                    {"type": "error", "message": "Heart Matrix Upgrade is unavailable."}
                )
                return
            player_id = player.player_id
            ticket_id = player.ticket_id
            attempts.append(now)
            self.utility_purchase_pending.add(player_id)

        try:
            verified = await asyncio.to_thread(
                _verify_extra_life_settlement_sync, signature, wallet, oracle_quote
            )
        except Exception:
            verified = False
        finally:
            async with self.lock:
                self.utility_purchase_pending.discard(player_id)
        async with self.lock:
            player = self.connections.get(websocket)
            if not verified:
                await websocket.send_json(
                    {"type": "error", "message": "Confirmed TLAMA settlement could not be verified."}
                )
                return
            if (
                player is None
                or player.player_id != player_id
                or player.ticket_id != ticket_id
                or player.admin_test_mode
                or player.game_over
                or not player.extra_life_unlocked
                or player.extra_lives_purchased >= MAX_EXTRA_LIFE_PURCHASES
            ):
                await websocket.send_json(
                    {"type": "error", "message": "Heart Matrix Upgrade is unavailable."}
                )
                return
            count = self.storage.claim_match_utility_purchase(
                signature=signature,
                ticket_id=ticket_id,
                player_id=player_id,
                item_id=EXTRA_LIFE_ITEM_ID,
                # This legacy audit column stores whole TLAMA and is not used
                # for authorization. The verifier above binds the exact atomic
                # amount; keep the record positive until storage gains an
                # atomic-amount column.
                amount_tlama=max(1, int(item["amount_tlama"])),
                maximum=MAX_EXTRA_LIFE_PURCHASES,
            )
            if count is None:
                await websocket.send_json(
                    {"type": "error", "message": "This purchase was already used or the limit was reached."}
                )
                return
            player.extra_lives_purchased = count
            player.max_lives = min(MAX_PURCHASED_LIVES, STARTING_LIVES + count)
            player.lives = min(player.max_lives, player.lives + 1)
            message = {
                "type": "event",
                "event": "extra_life_purchased",
                "player_id": player.player_id,
                "lives": player.lives,
                "max_lives": player.max_lives,
                **self.snapshot(),
            }
        await self._send_personalized(websocket, message)

    async def _admin_override_level_locked(self, websocket: WebSocket, level: int) -> None:
        """Canonical, lock-held reset used exclusively by the testing portal."""

        player = self.connections.get(websocket)
        if player is None:
            return
        baseline = LEVELS[level - 1].min_score
        now = asyncio.get_event_loop().time()
        # Private worlds and all state which can carry an incompatible input or
        # hazard are discarded before invoking the canonical level entry helper.
        self.level7_worlds.pop(player.player_id, None)
        self.level8_worlds.pop(player.player_id, None)
        self.level9_worlds.pop(player.player_id, None)
        self.level10_worlds.pop(player.player_id, None)
        player.score = baseline
        player.current_level = level
        player.victory_announced = False
        player.x, player.y = 0.5, 0.5
        player.stunned_until = 0.0
        player.invulnerable_until = now + STARTING_SHIELD_SECONDS
        player.lives = STARTING_LIVES
        player.max_lives = STARTING_LIVES
        player.extra_lives_purchased = 0
        player.extra_life_unlocked = False
        player.bump_count = 0
        player.orbs_collected = 0
        player.level_one_chip_ids.clear()
        player.game_over = False
        player.movement_direction = "right"
        player.world_progress = 0.0
        player.vehicle_velocity = 0.0
        player.vehicle_thrust = False
        player.vehicle_input_sequence = -1
        player.projectile_cooldown_until = 0.0
        player.vehicle_fire_requested = False
        player.submarine_velocity = 0.0
        player.submarine_thrust = False
        player.submarine_input_sequence = -1
        player.oxygen = LEVEL8_OXYGEN_MAX
        player.oxygen_zero_latched = False
        player.vx = player.vy = 0.0
        player.grounded = False
        player.run_axis = 0
        player.platformer_input_sequence = -1
        player.jump_latched = False
        player.booster_vx = player.booster_vy = 0.0
        player.booster_x = player.booster_y = 0
        player.citadel_input_sequence = -1
        player.citadel_input_at = 0.0
        player.super_spit_charge = 0
        player.super_spit_held = False
        player.super_spit_release_latched = False
        player.super_spit_cooldown_until = 0.0
        player.admin_test_mode = True
        if level <= 6:
            self.admin_collectibles[player.player_id] = (
                self._admin_collectibles_for_level(level)
            )
            self._initialize_admin_hazards(player, now)
            if level == 3:
                self._initialize_admin_relocation(player, now)
            else:
                self.admin_relocations.pop(player.player_id, None)
        else:
            self.admin_collectibles.pop(player.player_id, None)
            self.admin_hazards.pop(player.player_id, None)
            self.admin_relocations.pop(player.player_id, None)
        if level == 7:
            self._enter_level7(player, now)
        elif level == 8:
            self._enter_level8(player, now)
        elif level == 9:
            self._enter_level9(player, now)
        elif level == 10:
            self._enter_level10(player, now)
        LOG.info(
            "admin_override_success level=%d player_id_hash=%s",
            level,
            AdminPortalSecurity._hash(player.player_id)[:16],
        )
        # Confirm the private admin session before broadcasting the selected
        # level so its browser can suppress player-funnel analytics.
        await websocket.send_json(
            {
                "type": "event",
                "event": "admin_level_selected",
                "current_level": level,
                "score": baseline,
            }
        )
        await self.broadcast({"type": "state", **self.snapshot(now)})

    async def admin_override_level(self, websocket: WebSocket, level: Any) -> bool:
        """Safe test hook; production callers use the authenticated WS command."""

        if type(level) is not int or not 1 <= level <= len(LEVELS):
            return False
        async with self.lock:
            if websocket not in self.connections:
                return False
            await self._admin_override_level_locked(websocket, level)
            return True

    async def _join(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        if websocket in self.connections:
            return
        player_key = _safe_player_key(payload.get("player_key"))
        ownership_token = _safe_ownership_token(payload.get("ownership_token"))
        player_name = _safe_name(payload.get("name"))
        if player_key is None or ownership_token is None or player_name is None:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": (
                        "Enter a nickname with at least 2 characters."
                        if player_name is None
                        else "Player identity could not be verified."
                    ),
                }
            )
            return
        try:
            stored_player = self.storage.register_player(
                player_key, ownership_token, player_name
            )
            ticket = self.storage.issue_match_ticket(
                stored_player.player_id,
                ownership_token,
                entry_fee_tlama=SIMULATED_ENTRY_FEE_TLAMA,
            )
        except (PermissionError, ValueError):
            await websocket.send_json(
                {"type": "error", "message": "Player identity could not be verified."}
            )
            return
        duplicate_sockets = [
            active_socket
            for active_socket, active_player in self.connections.items()
            if active_socket is not websocket
            and (
                active_player.player_id == stored_player.player_id
                or active_player.name.casefold() == stored_player.name.casefold()
            )
        ]
        for duplicate_socket in duplicate_sockets:
            duplicate_player = self.connections.pop(duplicate_socket)
            admin_security.disconnect(duplicate_socket)
            self.level7_worlds.pop(duplicate_player.player_id, None)
            self.level8_worlds.pop(duplicate_player.player_id, None)
            self.level9_worlds.pop(duplicate_player.player_id, None)
            self.level10_worlds.pop(duplicate_player.player_id, None)
            self.admin_collectibles.pop(duplicate_player.player_id, None)
            self.admin_hazards.pop(duplicate_player.player_id, None)
            self.admin_relocations.pop(duplicate_player.player_id, None)
            self.opening_milestones.pop(duplicate_socket, None)
            if not duplicate_player.admin_test_mode:
                self.storage.record_score(
                    duplicate_player.player_id,
                    duplicate_player.ownership_token,
                    duplicate_player.score,
                )
                self.storage.complete_match_ticket(
                    duplicate_player.ticket_id,
                    duplicate_player.player_id,
                    duplicate_player.ownership_token,
                    final_score=duplicate_player.score,
                )
            try:
                await duplicate_socket.close(
                    code=4001, reason="Replaced by a newer player connection."
                )
            except RuntimeError:
                pass
            LOG.info(
                "game_duplicate_player_connection_replaced player_id=%s",
                duplicate_player.player_id,
            )
        player = Player(
            player_id=stored_player.player_id,
            name=stored_player.name,
            ticket_id=ticket.ticket_id,
            ownership_token=ownership_token,
            invulnerable_until=(
                asyncio.get_event_loop().time() + STARTING_SHIELD_SECONDS
            ),
            equipped_skin_id=(
                stored_player.equipped_skin_id
                if stored_player.equipped_skin_id in SKIN_STATE_TABLE
                and stored_player.equipped_skin_id in stored_player.owned_skin_ids
                else "default"
            ),
            owned_skin_ids=stored_player.owned_skin_ids,
        )
        self.connections[websocket] = player
        admin_security.bind_player(websocket, player)
        self._record_opening_once(websocket, "arcade_joined")
        LOG.info(
            "game_player_joined player_id=%s simulated_entry_fee_tlama=%.2f",
            player.player_id,
            player.entry_fee_tlama,
        )
        await websocket.send_json(
            {
                "type": "welcome",
                "player_id": player.player_id,
                "ticket_id": player.ticket_id,
                "ticket_status": ticket.status,
                "saved_high_score": stored_player.high_score,
                "simulated_entry_fee_tlama": player.entry_fee_tlama,
                "message": "Welcome to the simulated TLAMA arcade room.",
            }
        )
        await self._send_state()

    async def _reset(self, websocket: WebSocket) -> None:
        player = self.connections[websocket]
        self.utility_purchase_pending.discard(player.player_id)
        self.utility_purchase_attempts.pop(player.player_id, None)
        if not player.game_over and not player.victory_announced:
            await websocket.send_json(
                {
                    "type": "error",
                    "message": "Reset is available after game over or victory.",
                }
            )
            return
        if not player.admin_test_mode:
            try:
                self.storage.record_score(
                    player.player_id, player.ownership_token, player.score
                )
                self.storage.complete_match_ticket(
                    player.ticket_id, player.player_id, player.ownership_token,
                    final_score=player.score,
                )
            except (PermissionError, ValueError):
                pass
        try:
            ticket = self.storage.issue_match_ticket(
                player.player_id,
                player.ownership_token,
                entry_fee_tlama=SIMULATED_ENTRY_FEE_TLAMA,
            )
            player.ticket_id = ticket.ticket_id
        except (PermissionError, ValueError):
            ticket = None
        player.x = 0.5
        player.y = 0.5
        player.score = 0
        player.current_level = 1
        player.victory_announced = False
        player.stunned_until = 0.0
        player.invulnerable_until = (
            asyncio.get_event_loop().time() + STARTING_SHIELD_SECONDS
        )
        player.lives = STARTING_LIVES
        player.max_lives = STARTING_LIVES
        player.extra_lives_purchased = 0
        player.extra_life_unlocked = False
        player.bump_count = 0
        player.orbs_collected = 0
        player.game_over = False
        self.collectibles = {
            "orb-1": {"x": 0.22, "y": 0.24},
            "orb-2": {"x": 0.78, "y": 0.28},
            "orb-3": {"x": 0.31, "y": 0.72},
            "orb-4": {"x": 0.70, "y": 0.76},
        }
        self.level7_worlds.pop(player.player_id, None)
        self.level8_worlds.pop(player.player_id, None)
        self.level9_worlds.pop(player.player_id, None)
        self.level10_worlds.pop(player.player_id, None)
        self.admin_collectibles.pop(player.player_id, None)
        self.admin_hazards.pop(player.player_id, None)
        self.admin_relocations.pop(player.player_id, None)
        # Play Again starts a fresh normal match at level 1/score 0.
        player.admin_test_mode = False
        player.admin_test_skin_ids = ()
        if player.equipped_skin_id not in player.owned_skin_ids:
            player.equipped_skin_id = "default"
        player.world_progress = 0.0
        player.vehicle_velocity = 0.0
        player.vehicle_thrust = False
        player.vehicle_input_sequence = -1
        player.projectile_cooldown_until = 0.0
        player.vehicle_fire_requested = False
        player.submarine_velocity = 0.0
        player.submarine_thrust = False
        player.submarine_input_sequence = -1
        player.oxygen = LEVEL8_OXYGEN_MAX
        player.oxygen_zero_latched = False
        player.vx = player.vy = 0.0
        player.grounded = False
        player.run_axis = 0
        player.platformer_input_sequence = -1
        player.jump_latched = False
        player.booster_vx = player.booster_vy = 0.0
        player.booster_x = player.booster_y = 0
        player.citadel_input_sequence = -1
        player.citadel_input_at = 0.0
        player.super_spit_charge = 0
        player.super_spit_held = False
        player.super_spit_release_latched = False
        player.super_spit_cooldown_until = 0.0
        self.wardens = self._reset_wardens()
        await self.broadcast(
            {
                "type": "event",
                "event": "game_reset",
                "player_id": player.player_id,
                "starting_shield_duration_ms": int(
                    STARTING_SHIELD_SECONDS * 1000
                ),
                **self.snapshot(),
            }
        )

    async def _collect_level_one_chip(
        self, websocket: WebSocket, payload: dict[str, Any]
    ) -> None:
        player = self.connections[websocket]
        if player.game_over or level_for_score(player.score).number != 1:
            await self._send_state()
            return
        chip_id = payload.get("chip_id")
        chip = LEVEL_ONE_CHIPS.get(chip_id)
        if chip is None or math.hypot(player.x - chip[0], player.y - chip[1]) > 0.075:
            await self._send_state()
            return
        player.level_one_chip_ids.add(chip_id)
        await self._send_state()

    async def _complete_level_one(self, websocket: WebSocket) -> None:
        player = self.connections[websocket]
        if (
            player.game_over
            or level_for_score(player.score).number != 1
            or player.level_one_chip_ids != set(LEVEL_ONE_CHIPS)
            or player.x < LEVEL_ONE_FINISH_X - 0.03
        ):
            await self._send_state()
            return
        player.score = max(player.score, 110)
        player.current_level = 2
        player.invulnerable_until = (
            asyncio.get_event_loop().time() + STARTING_SHIELD_SECONDS
        )
        self._record_opening_once(websocket, "level_2_reached")
        if not player.admin_test_mode:
            try:
                self.storage.record_score(
                    player.player_id, player.ownership_token, player.score
                )
            except (PermissionError, ValueError):
                pass
        await self._send_state()

    async def _collect(self, websocket: WebSocket, orb_id: str) -> None:
        player = self.connections[websocket]
        if player.game_over:
            return
        # Level 8 collection is exclusively overlap-driven by level8_tick.
        if level_for_score(player.score).number in {8, 10} or player.current_level in {8, 10}:
            await self._send_state()
            return
        collectibles = self._collectibles_for_player(player)
        orb = collectibles.get(orb_id)
        if orb is None:
            return
        distance = math.hypot(player.x - orb["x"], player.y - orb["y"])
        if distance > 0.075:
            return
        previous_level = player.current_level
        player.score += 10
        player.orbs_collected += 1
        if player.orbs_collected == 1:
            self._record_opening_once(websocket, "first_orb_collected")
        extra_life_awarded = (
            player.orbs_collected % ORBS_PER_EXTRA_LIFE == 0
            and player.lives < player.max_lives
        )
        if extra_life_awarded:
            player.lives += 1
        level = level_for_score(player.score)
        player.current_level = level.number
        if player.current_level != previous_level:
            # Every level entry, including Level 6, receives a fresh shield.
            player.invulnerable_until = (
                asyncio.get_event_loop().time() + STARTING_SHIELD_SECONDS
            )
            if player.current_level == 6:
                if not player.admin_test_mode:
                    player.extra_life_unlocked = True
                if player.admin_test_mode:
                    self.admin_collectibles[player.player_id] = (
                        self._admin_collectibles_for_level(6)
                    )
                else:
                    self.collectibles = self._maze_orbs()
            if player.admin_test_mode and player.current_level <= 6:
                if player.current_level != 6:
                    self.admin_collectibles[player.player_id] = (
                        self._admin_collectibles_for_level(player.current_level)
                    )
                self._initialize_admin_hazards(
                    player, asyncio.get_event_loop().time()
                )
                if player.current_level == 3:
                    self._initialize_admin_relocation(
                        player, asyncio.get_event_loop().time()
                    )
                else:
                    self.admin_relocations.pop(player.player_id, None)
            if player.current_level == 7:
                self.admin_collectibles.pop(player.player_id, None)
                self.admin_hazards.pop(player.player_id, None)
                self.admin_relocations.pop(player.player_id, None)
                self._enter_level7(player, asyncio.get_event_loop().time())
            if player.current_level == 8:
                self.admin_collectibles.pop(player.player_id, None)
                self.admin_hazards.pop(player.player_id, None)
                self.admin_relocations.pop(player.player_id, None)
                self._enter_level8(player, asyncio.get_event_loop().time())
            if player.current_level == 10:
                self.admin_collectibles.pop(player.player_id, None)
                self.admin_hazards.pop(player.player_id, None)
                self.admin_relocations.pop(player.player_id, None)
                self._enter_level10(player, asyncio.get_event_loop().time())
        if previous_level == 1 and player.current_level == 2:
            self._record_opening_once(websocket, "level_2_reached")
        collectibles = self._collectibles_for_player(player)
        player.x = _clamp_coordinate(player.x, player.x, level.board_min, level.board_max)
        player.y = _clamp_coordinate(player.y, player.y, level.board_min, level.board_max)
        if not player.admin_test_mode:
            try:
                self.storage.record_score(
                    player.player_id, player.ownership_token, player.score
                )
            except (PermissionError, ValueError):
                pass
        collectibles[orb_id] = {
            "x": 0.12 + ((player.score * 17) % 76) / 100,
            "y": 0.12 + ((player.score * 29) % 76) / 100,
        }
        event = "orb_collected"
        if player.current_level != 10 and player.score >= VICTORY_SCORE and not player.victory_announced:
            event = "victory"
            player.victory_announced = True
        elif player.current_level != previous_level:
            event = "level_up"
        message = {
            "type": "event",
            "event": event,
            "player_id": player.player_id,
            "score": player.score,
            "current_level": player.current_level,
            "extra_life_awarded": extra_life_awarded,
            "lives": player.lives,
            "orbs_collected": player.orbs_collected,
            **self.snapshot(),
        }
        if player.admin_test_mode:
            await self._send_personalized(websocket, message)
        else:
            await self.broadcast(message)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        current = asyncio.get_event_loop().time() if now is None else now
        players = [
            {
                key: value
                for key, value in asdict(player).items()
                if key
                 not in {"ownership_token", "stunned_until", "invulnerable_until",
                         "platformer_input_sequence", "jump_latched",
                         "citadel_input_sequence", "citadel_input_at",
                         "super_spit_release_latched",
                         "super_spit_cooldown_until", "admin_test_mode",
                         "admin_test_skin_ids", "owned_skin_ids",
                          "extra_lives_purchased", "extra_life_unlocked",
                          "level_one_chip_ids"}
            }
            | {
                "stunned": player.stunned_until > current,
                "stun_remaining_ms": max(
                    0, round((player.stunned_until - current) * 1000)
                ),
                "invulnerable": player.invulnerable_until > current,
                "invulnerability_remaining_ms": max(
                    0, round((player.invulnerable_until - current) * 1000)
                ),
                "oxygen": max(0.0, min(LEVEL8_OXYGEN_MAX, player.oxygen)),
            }
            for player in self.connections.values()
        ]
        level7_worlds = {
            player_id: {
                "camera_progress": world["camera_progress"],
                "orbs": dict(world["orbs"]),
                "blocks": [dict(block) for block in world["blocks"]],
                "projectiles": [dict(projectile) for projectile in world["projectiles"]],
                "wardens": [dict(warden) for warden in world["wardens"]],
                "bounds": {"ceiling": 0.12, "floor": 0.88},
            }
            for player_id, world in self.level7_worlds.items()
        }
        level8_worlds = {
            player_id: copy.deepcopy(world)
            | {"bounds": {"ceiling": 0.14, "floor": 0.86},
               "oxygen_max": LEVEL8_OXYGEN_MAX}
            for player_id, world in self.level8_worlds.items()
        }
        level10_worlds = {}
        for player_id, world in self.level10_worlds.items():
            safe_world = copy.deepcopy(world)
            safe_world.pop("last_tick", None)
            safe_world["boss"].pop("shield_until", None)
            level10_worlds[player_id] = safe_world
        return {
            "players": players,
            "leaderboard": self.storage.leaderboard(),
            "collectibles": copy.deepcopy(self.collectibles),
            "levels": [level_payload(level) for level in LEVELS],
            "victory_score": VICTORY_SCORE,
            "level7_worlds": level7_worlds,
            "level8_worlds": level8_worlds,
            "level9_worlds": {
                player_id: copy.deepcopy(world)
                for player_id, world in self.level9_worlds.items()
            },
            "level10_worlds": level10_worlds,
            "starting_lives": STARTING_LIVES,
            "maximum_lives": MAX_PURCHASED_LIVES,
            "extra_life_catalog": [
                dict(item)
                for item in PAYMENT_CATALOG
                if item["id"] == EXTRA_LIFE_ITEM_ID
            ],
            "bumps_per_life": BUMPS_PER_LIFE,
            "orbs_per_extra_life": ORBS_PER_EXTRA_LIFE,
            "starting_shield_duration_ms": int(STARTING_SHIELD_SECONDS * 1000),
            "simulated_entry_fee_tlama": SIMULATED_ENTRY_FEE_TLAMA,
            "skin_catalog": [
                {
                    "id": item["id"],
                    "skin_id": variant["skin_id"],
                    "label": variant["label"],
                    "amount_tlama": item["amount_tlama"],
                    "description": item["description"],
                    "preview": variant["preview"],
                }
                for item in PAYMENT_CATALOG
                if item["kind"] == "cosmetic"
                for variant in item["variants"]
            ],
            "pumas": self.andean_pumas(current),
            "logs": self.woodland_logs(current),
            "tractors": self.farm_tractors(current),
            "ghosts": self.castle_ghosts(current),
            "maze_hazards": self.labyrinth_hazards(current),
            "maze_grid": self.maze_grid,
            "maze_dimensions": {"columns": MAZE_COLS, "rows": MAZE_ROWS},
            "warden_modes": WARDEN_MODES,
        }

    async def _send_state(self) -> None:
        await self.broadcast({"type": "state", **self.snapshot()})

    def _personalize_message(
        self, websocket: WebSocket, message: dict[str, Any]
    ) -> dict[str, Any]:
        player = self.connections.get(websocket)
        personalized = message
        if player is not None and "players" in message:
            personalized = copy.deepcopy(message)
            for public_player in personalized.get("players", []):
                if public_player.get("player_id") == player.player_id:
                    public_player["owned_skin_ids"] = sorted(
                        {*player.owned_skin_ids, *player.admin_test_skin_ids}
                    )
                    public_player["extra_lives_purchased"] = player.extra_lives_purchased
                    public_player["extra_life_unlocked"] = player.extra_life_unlocked
                    break
        if (
            player is None
            or not player.admin_test_mode
            or player.current_level > 6
            or "collectibles" not in message
        ):
            return personalized
        if personalized is message:
            personalized = copy.deepcopy(message)
        personalized["collectibles"] = copy.deepcopy(
            self._collectibles_for_player(player)
        )
        if player.admin_test_mode and player.current_level <= 6:
            current = asyncio.get_event_loop().time()
            state = self._admin_hazard_state(player, current)
            started = state["hazard_started_at"]
            personalized["pumas"] = self.andean_pumas(
                current,
                active_override=player.current_level == 1,
                hazard_started_at=started,
            )
            personalized["logs"] = self.woodland_logs(
                current,
                active_override=player.current_level == 2,
                hazard_started_at=started,
            )
            personalized["tractors"] = self.farm_tractors(
                current,
                active_override=player.current_level == 3,
                hazard_started_at=started,
            )
            personalized["ghosts"] = self.castle_ghosts(
                current,
                active_override=player.current_level == 4,
                hazard_started_at=started,
            )
            personalized["maze_hazards"] = self.labyrinth_hazards(
                current,
                active_override=player.current_level in {5, 6},
                wardens=state["wardens"],
                target=player,
            )
        return personalized

    async def _send_personalized(
        self, websocket: WebSocket, message: dict[str, Any]
    ) -> None:
        try:
            await websocket.send_json(self._personalize_message(websocket, message))
        except Exception:
            self.connections.pop(websocket, None)
            admin_security.disconnect(websocket)

    async def broadcast(
        self, message: dict[str, Any], *, include_admin: bool = True
    ) -> None:
        disconnected: list[WebSocket] = []
        for websocket, player in tuple(self.connections.items()):
            if player.admin_test_mode and not include_admin:
                continue
            try:
                await websocket.send_json(
                    self._personalize_message(websocket, message)
                )
            except Exception:
                disconnected.append(websocket)
        for websocket in disconnected:
            self.connections.pop(websocket, None)
            admin_security.disconnect(websocket)


storage = ArcadeStorage(DATABASE_PATH)
room = GameRoom(storage)


@asynccontextmanager
async def game_lifespan(_: FastAPI):
    await room.start()
    try:
        yield
    finally:
        await room.stop()


app = FastAPI(title="TLAMA Arcade", version="0.1.0", lifespan=game_lifespan)

GAME_BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
LEGACY_GAME_BASE_PATH = "/llama-website/game"


def _strip_game_path(scope: dict[str, Any]) -> dict[str, Any]:
    """Route both the root Arcade and its stable legacy artifact URL."""

    path = scope["path"]
    prefix = GAME_BASE_PATH
    if LEGACY_GAME_BASE_PATH and path.startswith(LEGACY_GAME_BASE_PATH):
        prefix = LEGACY_GAME_BASE_PATH
    if prefix and path.startswith(prefix):
        scope = dict(scope)
        scope["root_path"] = prefix
        scope["path"] = path[len(prefix):] or "/"
    return scope


@app.middleware("http")
async def strip_game_base_path(request: Request, call_next):
    """Allow the same app to run standalone or behind an artifact path."""

    request.scope.update(_strip_game_path(request.scope))
    return await call_next(request)


class GameBasePathMiddleware:
    """Strip the artifact prefix for both HTTP and WebSocket requests."""

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            scope = _strip_game_path(scope)
        await self.asgi_app(scope, receive, send)


app.add_middleware(GameBasePathMiddleware)


def _env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def admin_portal_enabled() -> bool:
    """The portal is available only when both operator switches are present."""

    return _env_flag("TLAMA_ARCADE_ADMIN_PORTAL_ENABLED") and bool(
        os.environ.get("TLAMA_ARCADE_ADMIN_PASSWORD", "")
    )


def _published_mode() -> bool:
    return bool(
        _env_flag("TLAMA_ARCADE_PRODUCTION")
        or _env_flag("REPLIT_DEPLOYMENT")
        or os.environ.get("TLAMA_ARCADE_ENV", "").lower() in {"production", "prod"}
        or os.environ.get("TLAMA_ARCADE_PUBLISHED_ORIGIN", "").strip()
    )


def _admin_allowed_origins() -> set[str]:
    origins: set[str] = set()
    for name in (
        "TLAMA_ARCADE_ALLOWED_ORIGINS",
        "TLAMA_ARCADE_ALLOWED_ORIGIN",
        "TLAMA_ARCADE_DASHBOARD_ORIGINS",
    ):
        for raw in os.environ.get(name, "").split(","):
            parsed = urlparse(raw.strip())
            if (
                parsed.scheme in {"http", "https"}
                and parsed.netloc
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            ):
                origins.add(f"{parsed.scheme}://{parsed.netloc}")
    published = os.environ.get("TLAMA_ARCADE_PUBLISHED_ORIGIN", "").strip()
    parsed_published = urlparse(published)
    if parsed_published.scheme in {"http", "https"} and parsed_published.netloc:
        origins.add(f"{parsed_published.scheme}://{parsed_published.netloc}")
    dashboard = urlparse(os.environ.get("TLAMA_ARCADE_DASHBOARD_URL", "").strip())
    if dashboard.scheme in {"http", "https"} and dashboard.netloc:
        origins.add(f"{dashboard.scheme}://{dashboard.netloc}")
    for domain in (
        os.environ.get("REPLIT_DOMAINS", "")
        + ","
        + os.environ.get("REPLIT_DEV_DOMAIN", "")
    ).split(","):
        domain = domain.strip()
        if domain:
            origins.add(f"https://{domain}")
    # Local development is an explicit, non-production allowlist rather than
    # an inference from Host. Published deployments must configure HTTPS above.
    if not _published_mode():
        port = os.environ.get("PORT", os.environ.get("TLAMA_GAME_PORT", "8099"))
        origins.update(
            {
                "http://localhost",
                "http://127.0.0.1",
                f"http://localhost:{port}",
                f"http://127.0.0.1:{port}",
                "http://localhost:8000",
                "http://127.0.0.1:8000",
            }
        )
    return origins


def _origin_allowed(headers: Any) -> bool:
    origin = headers.get("origin")
    if not origin:
        return False
    parsed = urlparse(str(origin))
    normalized = (
        f"{parsed.scheme}://{parsed.netloc}"
        if (
            parsed.scheme in {"http", "https"}
            and parsed.netloc
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
        else ""
    )
    return bool(normalized and normalized in _admin_allowed_origins())


def _trusted_https(headers: Any, scheme: str) -> bool:
    if scheme.lower() == "https":
        return True
    if not _env_flag("REPLIT_DEPLOYMENT"):
        return False
    # Replit's edge must supply both values. A caller-controlled
    # X-Forwarded-Proto by itself is never trusted.
    return (
        headers.get("x-replit-proxy", "").strip().lower() == "replit"
        and headers.get("x-forwarded-proto", "").strip().lower() == "https"
    )


def _admin_transport_allowed(headers: Any, scheme: str, origin: str | None) -> bool:
    if _published_mode():
        return _trusted_https(headers, scheme) and bool(
            origin and urlparse(origin).scheme == "https"
        )
    if _trusted_https(headers, scheme):
        return True
    if scheme.lower() != "http" or not origin:
        return False
    host = urlparse(origin).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def _status_context_safe(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin:
        return _origin_allowed(request.headers)
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site in {"same-origin", "same-site"}:
        return True
    return (
        not _published_mode()
        and request.url.scheme == "http"
        and (request.client.host if request.client else "") in {"127.0.0.1", "::1"}
    )


def _audit_admin(category: str, ip: str) -> None:
    LOG.warning(
        "admin_portal_audit category=%s ip_hash=%s",
        category,
        AdminPortalSecurity._ip_hash(ip),
    )


def _cookie_secure(request: Request) -> bool:
    return _trusted_https(request.headers, request.url.scheme) or _published_mode()


def _set_admin_cookies(response: JSONResponse, request: Request, session: str, csrf: str) -> None:
    secure = _cookie_secure(request)
    response.set_cookie(
        ADMIN_SESSION_COOKIE, session, httponly=True, secure=secure,
        samesite="strict", max_age=int(ADMIN_SESSION_TTL_SECONDS), path="/",
    )
    response.set_cookie(
        ADMIN_CSRF_COOKIE, csrf, httponly=False, secure=secure,
        samesite="strict", max_age=int(ADMIN_SESSION_TTL_SECONDS), path="/",
    )


def _admin_failure_response(status_code: int = 401) -> JSONResponse:
    return JSONResponse({"detail": "Administrative authentication failed."}, status_code=status_code)


def dashboard_url() -> str:
    """Return a dashboard URL only when its HTTPS origin is explicitly trusted."""

    configured = os.environ.get("TLAMA_ARCADE_DASHBOARD_URL", "").strip()
    replit_domains = [
        domain.strip()
        for domain in os.environ.get("REPLIT_DOMAINS", "").split(",")
        if domain.strip()
    ]
    if not configured:
        if replit_domains:
            configured = f"https://{replit_domains[0]}/llama-website/arcade"

    allowed_origins = {
        f"https://{domain}"
        for domain in replit_domains
    }
    for value in os.environ.get("TLAMA_ARCADE_DASHBOARD_ORIGINS", "").split(","):
        parsed_allowed = urlparse(value.strip())
        if parsed_allowed.scheme == "https" and parsed_allowed.netloc:
            allowed_origins.add(f"https://{parsed_allowed.netloc}")

    parsed = urlparse(configured)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return configured if parsed.scheme == "https" and origin in allowed_origins else ""


def payment_config(*, issue_quote: bool = True) -> dict[str, Any]:
    """Return public payment configuration, never credentials or private keys.

    The only permitted future game settlement route is an audited Transfer
    Hook adapter targeting the fixed game rewards vault. Blank public
    addresses intentionally keep the client disabled.
    """

    mint_address = os.environ.get("TLAMA_MINT_ADDRESS", "").strip()
    reward_vault_address = os.environ.get(
        "TLAMA_GAME_REWARDS_VAULT_ADDRESS", ""
    ).strip()
    hook_program_address = os.environ.get(
        "TLAMA_GAME_HOOK_PROGRAM_ADDRESS", ""
    ).strip()
    rpc_url = os.environ.get("TLAMA_PUBLIC_SOLANA_RPC_URL", "").strip()
    raydium_api = os.environ.get("TLAMA_RAYDIUM_SWAP_API", "").strip()
    requested = _env_flag("TLAMA_PAYMENT_ENABLED")
    settlement_mode = "audited-transfer-hook-required"
    configured = requested and bool(
        mint_address
        and reward_vault_address
        and hook_program_address
        and rpc_url
        and len(os.environ.get("SESSION_SECRET", "")) >= 32
    )
    web3_enabled = _env_flag("TLAMA_WEB3_ENABLED") and bool(
        mint_address and rpc_url and raydium_api
    )
    decimals = int(os.environ.get("TLAMA_TOKEN_DECIMALS", "9"))
    oracle_config = {
        "oracle_feed_id": "mock-token-price-usd",
        "oracle_quote_ttl_seconds": int(
            os.environ.get("TLAMA_PYTH_QUOTE_TTL_SECONDS", "180")
        ),
        "decimals": decimals,
    }
    catalog = [dict(item) for item in PAYMENT_CATALOG]
    enabled = False
    if configured and issue_quote:
        now = int(time.time())
        expires_at = now + oracle_config["oracle_quote_ttl_seconds"]
        for item in catalog:
            atomic_amount = int(item["amount_tlama"]) * 10 ** decimals
            item["amount_atomic"] = str(atomic_amount)
            item["oracle_quote"] = _issue_oracle_quote(
                oracle_config,
                item_id=str(item["id"]),
                atomic_amount=atomic_amount,
                publish_time=now,
                now=now,
            )
            item["quote_expires_at"] = expires_at
        enabled = True
    elif configured:
        enabled = True
    economy = {
        "entry_fee_tlama": next(
            int(item["amount_tlama"])
            for item in catalog
            if item["id"] == ARCADE_ENTRY_ITEM_ID
        ),
        "entry_fee_required": True,
        "settlement_mode": settlement_mode,
        "reward_vault_percent": 100,
        "reward_vault_label": "10% Game Rewards Vault",
        "reward_vault_address": reward_vault_address,
        "hook_program_address": hook_program_address,
        "catalog": catalog,
    }
    return {
        "enabled": enabled,
        "web3_enabled": web3_enabled,
        "mint_address": mint_address,
        "destination_address": reward_vault_address,
        "destination_label": "10% Game Rewards Vault",
        "reward_vault_address": reward_vault_address,
        "hook_program_address": hook_program_address,
        "rpc_url": rpc_url,
        "raydium_api": raydium_api,
        "swap_slippage_bps": int(os.environ.get("TLAMA_SWAP_SLIPPAGE_BPS", "100")),
        "decimals": decimals,
        **oracle_config,
        "catalog": catalog,
        "game_economy": economy,
        "dashboard_url": dashboard_url(),
    }


@app.get("/")
async def index() -> FileResponse:
    """Serve the standalone game client."""

    return FileResponse(
        CLIENT_PATH,
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/admin/status")
async def admin_status(request: Request) -> JSONResponse:
    """Expose availability only; no grant or operator secret is returned."""

    if not admin_portal_enabled():
        return JSONResponse({"enabled": False})
    if not _status_context_safe(request):
        _audit_admin("origin_rejected", request.client.host if request.client else "unknown")
        return JSONResponse({"enabled": False})
    if request.headers.get("origin") and not _admin_transport_allowed(
        request.headers, request.url.scheme, request.headers.get("origin")
    ):
        return JSONResponse({"enabled": False})
    if _published_mode() and not _trusted_https(request.headers, request.url.scheme):
        return JSONResponse({"enabled": False})
    cookies = _parse_cookie_header(request.headers.get("cookie", ""))
    session_data = admin_security.ensure_session(
        cookies.get(ADMIN_SESSION_COOKIE), cookies.get(ADMIN_CSRF_COOKIE)
    )
    if session_data is None:
        return JSONResponse({"enabled": False})
    session, csrf = session_data
    response = JSONResponse({"enabled": True})
    _set_admin_cookies(response, request, session, csrf)
    return response


@app.post("/admin/auth")
async def admin_auth(request: Request) -> JSONResponse:
    """Authenticate the operator without ever returning the configured password."""

    ip = request.client.host if request.client else "unknown"
    if not admin_portal_enabled():
        _audit_admin("disabled", ip)
        return _admin_failure_response(404)
    if not _origin_allowed(request.headers) or not _admin_transport_allowed(
        request.headers, request.url.scheme, request.headers.get("origin")
    ):
        _audit_admin("origin_rejected", ip)
        return _admin_failure_response(403)
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    content_length = request.headers.get("content-length")
    if content_type != "application/json" or (
        content_length and (not content_length.isdigit() or int(content_length) > ADMIN_MAX_AUTH_BODY_BYTES)
    ):
        _audit_admin("malformed_request", ip)
        return _admin_failure_response()
    body = await request.body()
    if len(body) > ADMIN_MAX_AUTH_BODY_BYTES:
        _audit_admin("malformed_request", ip)
        return _admin_failure_response()
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"password", "csrf"}
        or not isinstance(payload.get("password"), str)
        or not isinstance(payload.get("csrf"), str)
        or len(payload["password"]) > 512
        or len(payload["csrf"]) > 128
    ):
        _audit_admin("malformed_request", ip)
        return _admin_failure_response()
    cookies = _parse_cookie_header(request.headers.get("cookie", ""))
    session_id = cookies.get(ADMIN_SESSION_COOKIE, "")
    csrf_cookie = cookies.get(ADMIN_CSRF_COOKIE, "")
    csrf = payload["csrf"]
    if not csrf_cookie or not hmac.compare_digest(csrf_cookie, csrf):
        _audit_admin("csrf_failed", ip)
        return _admin_failure_response()
    authenticated, result = admin_security.authenticate(
        session_id, csrf, payload["password"], ip
    )
    # Clear the local request reference immediately; it is never logged or
    # included in an exception response.
    payload["password"] = ""
    if not authenticated:
        _audit_admin(result, ip)
        return _admin_failure_response(429 if result == "rate_limited" else 401)
    response = JSONResponse({"authenticated": True})
    _set_admin_cookies(response, request, session_id, csrf)
    _audit_admin("authenticated", ip)
    return response


@app.get("/config.js", include_in_schema=False)
async def browser_config() -> FileResponse:
    """Serve disabled-by-default public browser configuration."""

    return FileResponse(CONFIG_PATH, media_type="text/javascript")


@app.get("/web3-onboarding.js", include_in_schema=False)
async def web3_onboarding() -> FileResponse:
    """Serve the guarded wallet-onboarding client module."""

    return FileResponse(WEB3_ONBOARDING_PATH, media_type="text/javascript")


@app.get("/wallet-failure-stages.js", include_in_schema=False)
async def wallet_failure_stages() -> FileResponse:
    """Serve the privacy-safe wallet failure classifier module."""

    return FileResponse(WALLET_FAILURE_STAGES_PATH, media_type="text/javascript")


@app.get("/payment-config")
async def get_payment_config() -> dict[str, Any]:
    """Expose only public, operator-approved payment settings to the client."""

    return await asyncio.to_thread(payment_config)


@app.get("/rewards/allocation-preview")
async def rewards_allocation_preview() -> dict[str, Any]:
    """Return a deterministic, read-only payout preview for verified scores."""

    verified_scores = tuple(
        VerifiedGameScore(player_id=player.player_id, score=player.score)
        for player in room.connections.values()
        if not player.admin_test_mode
    )
    allocation = allocate_epoch_rewards(
        verified_scores,
        epoch_number=0,
        vault_remaining_raw=REWARD_VAULT_RAW,
    )
    return {
        "status": "deterministic_preview_only",
        "policy": {
            "total_supply_tlama": REWARD_VAULT_POLICY.total_supply_tlama,
            "vault_percent": REWARD_VAULT_POLICY.vault_percent,
            "vault_tlama": REWARD_VAULT_POLICY.vault_tlama,
            "epoch_payout_tlama": REWARD_VAULT_POLICY.epoch_payout_tlama,
            "max_reward_epochs": REWARD_VAULT_POLICY.max_reward_epochs,
            "purpose": REWARD_VAULT_POLICY.purpose,
        },
        "epoch_number": allocation.epoch_number,
        "payout_budget_raw": allocation.payout_budget_raw,
        "allocated_raw": allocation.allocated_raw,
        "remaining_vault_raw": allocation.remaining_vault_raw,
        "allocations": [
            {
                "player_id": item.player_id,
                "score": item.score,
                "amount_raw": item.amount_raw,
                "claim_id": item.claim_id,
            }
            for item in allocation.allocations
        ],
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    """Expose non-sensitive room health for a future supervisor."""

    return {
        "status": "ok",
        "active_players": len(room.connections),
        "simulated_entry_fee_tlama": SIMULATED_ENTRY_FEE_TLAMA,
        # Health checks are local-only and never wait on the external oracle.
        "live_payments_enabled": (
            int(_PAYMENT_QUOTE_CACHE.get("expires_at", 0)) > int(time.time())
        ),
        "reward_vault_tlama": REWARD_VAULT_POLICY.vault_tlama,
        "storage": storage.summary(),
    }


@app.get("/leaderboard")
async def persistent_leaderboard() -> dict[str, Any]:
    """Return the persistent all-time leaderboard."""

    return {"leaderboard": storage.leaderboard()}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not _origin_allowed(websocket.headers) or not _admin_transport_allowed(
        websocket.headers,
        "https" if websocket.url.scheme == "wss" else "http",
        websocket.headers.get("origin"),
    ):
        _audit_admin(
            "websocket_origin_rejected",
            websocket.client.host if websocket.client else "unknown",
        )
        await websocket.close(code=1008, reason="Origin is not allowed.")
        return
    if not await room.connect(websocket):
        return
    try:
        while True:
            payload = await websocket.receive_json()
            if not isinstance(payload, dict):
                await websocket.send_json(
                    {"type": "error", "message": "Game messages must be JSON objects."}
                )
                continue
            await room.handle_message(websocket, payload)
    except WebSocketDisconnect:
        await room.disconnect(websocket)
    except Exception:
        LOG.exception("game_socket_error")
        await room.disconnect(websocket)


def main() -> None:
    """Start the server only when this file is explicitly run."""

    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    listen_fd = os.environ.get("TLAMA_GAME_LISTEN_FD")
    server_options = (
        {"fd": int(listen_fd)}
        if listen_fd is not None
        else {
            "host": os.environ.get("TLAMA_GAME_HOST", "0.0.0.0"),
            "port": int(
                os.environ.get("PORT", os.environ.get("TLAMA_GAME_PORT", "8099"))
            ),
        }
    )
    uvicorn.run(app, **server_options)


if __name__ == "__main__":
    main()
