"""Server-authoritative in-memory match state for the Layer 2 fighting game."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
import secrets
import time
from typing import Any, Literal
import uuid

from entry_rail import FightingGameEntryRail

PLAYER_ONE = "player_1"
PLAYER_TWO = "player_2"
PLAYER_SLOTS = (PLAYER_ONE, PLAYER_TWO)
MAX_HEALTH = 100
ROUND_SECONDS = 99
MAX_COMBO = 12
ARENA_MIN_X = 80.0
ARENA_MAX_X = 920.0
GROUND_Y = 0.0
MOVE_SPEED = 260.0
MOVE_INPUT_TTL = 0.16
JUMP_VELOCITY = 520.0
GRAVITY = 1080.0
CHARACTERS = frozenset({"andean-guardian", "neon-puma", "quantum-llama"})
CombatAction = Literal[
    "idle",
    "move_left",
    "move_right",
    "crouch",
    "jump",
    "light_punch",
    "heavy_kick",
    "block",
]

ACTION_DAMAGE: dict[str, int] = {
    "light_punch": 6,
    "heavy_kick": 11,
}
ACTION_COOLDOWNS: dict[str, float] = {
    "light_punch": 0.28,
    "heavy_kick": 0.62,
}
ACTION_RANGES: dict[str, float] = {
    "light_punch": 105.0,
    "heavy_kick": 145.0,
}


class MatchStateError(ValueError):
    """Raised when a client requests an invalid authoritative state transition."""


@dataclass
class ComboState:
    active: bool = False
    hits: int = 0
    total_damage: int = 0

    def reset(self) -> None:
        self.active = False
        self.hits = 0
        self.total_damage = 0


@dataclass
class PlayerState:
    player_id: str
    slot: str
    display_name: str
    session_token: str = field(repr=False)
    character_id: str | None = None
    health: int = MAX_HEALTH
    combo: ComboState = field(default_factory=ComboState)
    last_action_at: float = 0.0
    connected: bool = False
    position_x: float = 220.0
    position_y: float = GROUND_Y
    velocity_y: float = 0.0
    facing: str = "right"
    animation_state: str = "idle"
    blocking: bool = False
    is_computer: bool = False
    move_direction: int = 0
    move_input_until: float = 0.0

    def public(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("session_token", None)
        payload.pop("last_action_at", None)
        payload.pop("velocity_y", None)
        payload.pop("move_direction", None)
        payload.pop("move_input_until", None)
        return payload


@dataclass
class MatchState:
    match_id: str
    players: dict[str, PlayerState]
    entry_validation: dict[str, Any]
    status: str = "waiting_for_players"
    round_number: int = 1
    round_started_at: float | None = None
    winner_slot: str | None = None
    revision: int = 0
    last_tick_at: float | None = None
    next_ai_action_at: float = 0.0
    ai_cycle: int = 0
    last_activity_at: float = field(default_factory=time.monotonic)
    completed_at: float | None = None

    def round_seconds_remaining(self, now: float | None = None) -> int:
        if self.round_started_at is None:
            return ROUND_SECONDS
        elapsed = max(0.0, (now or time.monotonic()) - self.round_started_at)
        return max(0, ROUND_SECONDS - int(elapsed))

    def public(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "status": self.status,
            "round_number": self.round_number,
            "round_seconds_remaining": self.round_seconds_remaining(),
            "winner_slot": self.winner_slot,
            "revision": self.revision,
            "entry_validation": dict(self.entry_validation),
            "players": {
                slot: player.public() for slot, player in self.players.items()
            },
        }


class MatchManager:
    """Serializes all state transitions behind one asynchronous lock."""

    def __init__(self, entry_rail: FightingGameEntryRail | None = None) -> None:
        self._entry_rail = entry_rail or FightingGameEntryRail()
        self._matches: dict[str, MatchState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _new_player(slot: str, display_name: str) -> PlayerState:
        return PlayerState(
            player_id=uuid.uuid4().hex,
            slot=slot,
            display_name=display_name,
            session_token=secrets.token_urlsafe(32),
            position_x=220.0 if slot == PLAYER_ONE else 780.0,
            facing="right" if slot == PLAYER_ONE else "left",
        )

    @staticmethod
    def _safe_name(value: str) -> str:
        normalized = " ".join(value.split())
        if not 2 <= len(normalized) <= 24:
            raise MatchStateError("display_name must contain 2 to 24 characters")
        return normalized

    async def create_match(
        self, *, display_name: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        entry = self._entry_rail.validate()
        player = self._new_player(PLAYER_ONE, self._safe_name(display_name))
        match = MatchState(
            match_id=uuid.uuid4().hex,
            players={PLAYER_ONE: player},
            entry_validation=asdict(entry),
        )
        async with self._lock:
            self._matches[match.match_id] = match
        return match.public(), self._credentials(player)

    async def create_ai_match(
        self, *, display_name: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        first_entry = self._entry_rail.validate()
        self._entry_rail.validate()
        player = self._new_player(PLAYER_ONE, self._safe_name(display_name))
        computer = self._new_player(PLAYER_TWO, "Cinder AI")
        player.character_id = "andean-guardian"
        computer.character_id = "neon-puma"
        computer.is_computer = True
        computer.connected = True
        now = time.monotonic()
        match = MatchState(
            match_id=uuid.uuid4().hex,
            players={PLAYER_ONE: player, PLAYER_TWO: computer},
            entry_validation={
                **asdict(first_entry),
                "validated_slots": list(PLAYER_SLOTS),
            },
            status="active",
            round_started_at=now,
            last_tick_at=now,
            next_ai_action_at=now + 0.4,
        )
        async with self._lock:
            self._matches[match.match_id] = match
        return match.public(), self._credentials(player)

    async def join_match(
        self, *, match_id: str, display_name: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        async with self._lock:
            match = self._require_match(match_id)
            if PLAYER_TWO in match.players:
                raise MatchStateError("match already has two players")
            self._entry_rail.validate()
            player = self._new_player(PLAYER_TWO, self._safe_name(display_name))
            match.players[PLAYER_TWO] = player
            match.entry_validation["validated_slots"] = list(PLAYER_SLOTS)
            match.status = "character_select"
            match.last_activity_at = time.monotonic()
            match.revision += 1
            return match.public(), self._credentials(player)

    async def select_character(
        self,
        *,
        match_id: str,
        player_id: str,
        session_token: str,
        character_id: str,
    ) -> dict[str, Any]:
        if character_id not in CHARACTERS:
            raise MatchStateError("character_id is not available")
        async with self._lock:
            match, player = self._authenticate(
                match_id, player_id, session_token
            )
            if match.status not in {"waiting_for_players", "character_select"}:
                raise MatchStateError(
                    "character selection is closed for this round"
                )
            player.character_id = character_id
            match.last_activity_at = time.monotonic()
            match.revision += 1
            if (
                len(match.players) == 2
                and all(candidate.character_id for candidate in match.players.values())
            ):
                started_at = time.monotonic()
                match.status = "active"
                match.round_started_at = started_at
                match.last_tick_at = started_at
            return match.public()

    async def expire_round(
        self, match_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            match = self._require_match(match_id)
            self._finish_if_timer_expired(match, now=now)
            return match.public()

    async def set_connected(
        self,
        *,
        match_id: str,
        player_id: str,
        session_token: str,
        connected: bool,
    ) -> dict[str, Any]:
        async with self._lock:
            match, player = self._authenticate(
                match_id, player_id, session_token
            )
            player.connected = connected
            match.last_activity_at = time.monotonic()
            match.revision += 1
            return match.public()

    async def combat_action(
        self,
        *,
        match_id: str,
        player_id: str,
        session_token: str,
        action: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = now or time.monotonic()
        async with self._lock:
            match, attacker = self._authenticate(
                match_id, player_id, session_token
            )
            self._finish_if_timer_expired(match, now=current_time)
            if match.status != "active":
                raise MatchStateError("round is not active")
            aliases = {
                "light": "light_punch",
                "heavy": "heavy_kick",
                "special": "heavy_kick",
                "break_combo": "idle",
            }
            self._apply_input(
                match,
                attacker,
                aliases.get(action, action),
                current_time,
            )
            match.last_activity_at = current_time
            return match.public()

    async def player_input(
        self,
        *,
        match_id: str,
        player_id: str,
        session_token: str,
        action: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        current_time = time.monotonic() if now is None else now
        async with self._lock:
            match, player = self._authenticate(
                match_id, player_id, session_token
            )
            self._finish_if_timer_expired(match, now=current_time)
            if match.status != "active":
                raise MatchStateError("round is not active")
            self._apply_input(match, player, action, current_time)
            match.last_activity_at = current_time
            return match.public()

    async def tick_match(
        self, match_id: str, *, now: float | None = None
    ) -> dict[str, Any]:
        current_time = time.monotonic() if now is None else now
        async with self._lock:
            match = self._require_match(match_id)
            if match.status != "active":
                return match.public()
            previous = match.last_tick_at or current_time
            delta = min(0.1, max(0.0, current_time - previous))
            match.last_tick_at = current_time
            for player in match.players.values():
                if (
                    player.move_direction
                    and current_time <= player.move_input_until
                ):
                    player.position_x = min(
                        ARENA_MAX_X,
                        max(
                            ARENA_MIN_X,
                            player.position_x
                            + player.move_direction * MOVE_SPEED * delta,
                        ),
                    )
                elif player.move_direction:
                    player.move_direction = 0
                    if (
                        player.position_y == GROUND_Y
                        and player.animation_state
                        in {"move_left", "move_right"}
                    ):
                        player.animation_state = "idle"
                if player.position_y > GROUND_Y or player.velocity_y > 0:
                    player.position_y = max(
                        GROUND_Y,
                        player.position_y + player.velocity_y * delta,
                    )
                    player.velocity_y -= GRAVITY * delta
                    if player.position_y == GROUND_Y:
                        player.velocity_y = 0.0
                        if player.animation_state == "jump":
                            player.animation_state = "idle"
            first = match.players[PLAYER_ONE]
            second = match.players[PLAYER_TWO]
            first.facing = "right" if first.position_x <= second.position_x else "left"
            second.facing = "left" if second.position_x >= first.position_x else "right"
            if second.is_computer and current_time >= match.next_ai_action_at:
                distance = abs(first.position_x - second.position_x)
                match.ai_cycle += 1
                if distance > ACTION_RANGES["heavy_kick"]:
                    action = (
                        "move_left"
                        if second.position_x > first.position_x
                        else "move_right"
                    )
                    delay = 0.08
                elif distance > ACTION_RANGES["light_punch"]:
                    action = "heavy_kick"
                    delay = ACTION_COOLDOWNS["heavy_kick"] + 0.05
                elif match.ai_cycle % 7 == 0:
                    action = "block"
                    delay = 0.32
                elif match.ai_cycle % 3 == 0:
                    action = "heavy_kick"
                    delay = 0.68
                else:
                    action = "light_punch"
                    delay = 0.31
                try:
                    self._apply_input(match, second, action, current_time)
                except MatchStateError as exc:
                    if str(exc) != "combat action is cooling down":
                        raise
                    delay = 0.1
                match.next_ai_action_at = current_time + delay
            self._finish_if_timer_expired(match, now=current_time)
            match.revision += 1
            return match.public()

    def _apply_input(
        self,
        match: MatchState,
        attacker: PlayerState,
        action: str,
        current_time: float,
    ) -> None:
        if action not in {
            "idle",
            "move_left",
            "move_right",
            "crouch",
            "jump",
            "light_punch",
            "heavy_kick",
            "block",
        }:
            raise MatchStateError("player input is not supported")
        attacker.blocking = action == "block"
        if action == "idle":
            attacker.move_direction = 0
            attacker.move_input_until = 0.0
            if attacker.position_y == GROUND_Y:
                attacker.animation_state = "idle"
            attacker.combo.reset()
        elif action in {"move_left", "move_right"}:
            direction = -1 if action == "move_left" else 1
            attacker.move_direction = direction
            attacker.move_input_until = current_time + MOVE_INPUT_TTL
            if attacker.position_y == GROUND_Y:
                attacker.animation_state = action
        elif action == "crouch":
            if attacker.position_y == GROUND_Y:
                attacker.animation_state = "crouch"
        elif action == "jump":
            if attacker.position_y == GROUND_Y:
                attacker.velocity_y = JUMP_VELOCITY
                attacker.animation_state = "jump"
        elif action == "block":
            attacker.animation_state = "block"
        else:
            cooldown = ACTION_COOLDOWNS[action]
            if current_time - attacker.last_action_at < cooldown:
                raise MatchStateError("combat action is cooling down")
            defender = next(
                player
                for player in match.players.values()
                if player.player_id != attacker.player_id
            )
            attacker.animation_state = action
            attacker.last_action_at = current_time
            distance = abs(attacker.position_x - defender.position_x)
            vertical_gap = abs(attacker.position_y - defender.position_y)
            if distance <= ACTION_RANGES[action] and vertical_gap <= 85:
                damage = ACTION_DAMAGE[action]
                if defender.blocking:
                    damage = max(1, damage // 4)
                defender.health = max(0, defender.health - damage)
                defender.combo.reset()
                defender.animation_state = (
                    "knockout" if defender.health == 0 else "hit"
                )
                attacker.combo.active = True
                attacker.combo.hits = min(
                    MAX_COMBO, attacker.combo.hits + 1
                )
                attacker.combo.total_damage += damage
                if defender.health == 0:
                    match.status = "round_complete"
                    match.winner_slot = attacker.slot
                    match.completed_at = current_time
        match.revision += 1

    async def snapshot(self, match_id: str) -> dict[str, Any]:
        async with self._lock:
            match = self._require_match(match_id)
            self._finish_if_timer_expired(match)
            return match.public()

    async def match_count(self) -> int:
        async with self._lock:
            return len(self._matches)

    async def remove_match(self, match_id: str) -> None:
        async with self._lock:
            self._matches.pop(match_id, None)

    async def cleanup_stale(
        self,
        *,
        now: float | None = None,
        idle_seconds: float = 120.0,
        completed_seconds: float = 30.0,
    ) -> list[str]:
        current_time = time.monotonic() if now is None else now
        removed: list[str] = []
        async with self._lock:
            for match_id, match in tuple(self._matches.items()):
                completed_stale = (
                    match.status == "round_complete"
                    and match.completed_at is not None
                    and current_time - match.completed_at >= completed_seconds
                )
                human_connected = any(
                    player.connected and not player.is_computer
                    for player in match.players.values()
                )
                abandoned_stale = (
                    match.status != "round_complete"
                    and not human_connected
                    and current_time - match.last_activity_at >= idle_seconds
                )
                if completed_stale or abandoned_stale:
                    self._matches.pop(match_id, None)
                    removed.append(match_id)
        return removed

    def _finish_if_timer_expired(
        self, match: MatchState, *, now: float | None = None
    ) -> None:
        if (
            match.status != "active"
            or match.round_seconds_remaining(now=now) > 0
        ):
            return
        match.status = "round_complete"
        match.completed_at = time.monotonic() if now is None else now
        first = match.players[PLAYER_ONE]
        second = match.players[PLAYER_TWO]
        match.winner_slot = (
            first.slot
            if first.health > second.health
            else second.slot
            if second.health > first.health
            else None
        )
        match.revision += 1

    def _require_match(self, match_id: str) -> MatchState:
        match = self._matches.get(match_id)
        if match is None:
            raise MatchStateError("match not found")
        return match

    def _authenticate(
        self, match_id: str, player_id: str, session_token: str
    ) -> tuple[MatchState, PlayerState]:
        match = self._require_match(match_id)
        for player in match.players.values():
            if (
                secrets.compare_digest(player.player_id, player_id)
                and secrets.compare_digest(player.session_token, session_token)
            ):
                return match, player
        raise MatchStateError("player session authentication failed")

    @staticmethod
    def _credentials(player: PlayerState) -> dict[str, str]:
        return {
            "player_id": player.player_id,
            "player_slot": player.slot,
            "session_token": player.session_token,
        }