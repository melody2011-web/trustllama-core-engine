"""FastAPI boundary for the isolated Layer 2 competitive fighting game."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import os
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from entry_rail import CoreRailValidationError
from match_state import MatchManager, MatchStateError

app = FastAPI(
    title="TLAMA Layer 2 Fighting Game",
    version="0.1.0",
    docs_url="/docs",
)
matches = MatchManager()
CREATE_WINDOW_SECONDS = 60.0
CREATE_LIMIT_PER_WINDOW = 10
MAX_RESIDENT_MATCHES = 64
COMPLETED_MATCH_TTL_SECONDS = 30.0


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateMatchRequest(StrictModel):
    display_name: str = Field(min_length=2, max_length=24)


class JoinMatchRequest(StrictModel):
    display_name: str = Field(min_length=2, max_length=24)


class CharacterSelectionRequest(StrictModel):
    character_id: str = Field(min_length=2, max_length=40)


def _http_error(exc: Exception) -> HTTPException:
    status = 404 if str(exc) == "match not found" else 409
    return HTTPException(status_code=status, detail=str(exc))


def _credentials(
    player_id: Annotated[str, Header(alias="X-Player-Id")],
    session_token: Annotated[str, Header(alias="X-Session-Token")],
) -> tuple[str, str]:
    return player_id, session_token


async def _enforce_match_creation_limit(request: Request) -> None:
    now = asyncio.get_running_loop().time()
    client_host = request.client.host if request.client else "unknown"
    attempts = app.state.creation_attempts.setdefault(client_host, [])
    attempts[:] = [
        attempted
        for attempted in attempts
        if now - attempted < CREATE_WINDOW_SECONDS
    ]
    if len(attempts) >= CREATE_LIMIT_PER_WINDOW:
        raise HTTPException(
            status_code=429,
            detail="match creation rate limit exceeded",
        )
    attempts.append(now)
    if await matches.match_count() >= MAX_RESIDENT_MATCHES:
        raise HTTPException(
            status_code=503,
            detail="fighting-game sandbox is at match capacity",
        )


@app.get("/fighter-api/healthz")
async def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "tlama-layer2-fighting-game",
        "authority": "server",
    }


@app.post("/fighter-api/matches", status_code=201)
async def create_match(
    request: CreateMatchRequest,
    http_request: Request,
) -> dict[str, Any]:
    async with app.state.creation_lock:
        await _enforce_match_creation_limit(http_request)
        try:
            state, credentials = await matches.create_match(
                display_name=request.display_name,
            )
        except (CoreRailValidationError, MatchStateError) as exc:
            raise _http_error(exc) from exc
    return {"match": state, "player_session": credentials}


@app.post("/fighter-api/matches/ai", status_code=201)
async def create_ai_match(
    request: CreateMatchRequest,
    http_request: Request,
) -> dict[str, Any]:
    async with app.state.creation_lock:
        await _enforce_match_creation_limit(http_request)
        try:
            state, credentials = await matches.create_ai_match(
                display_name=request.display_name,
            )
        except (CoreRailValidationError, MatchStateError) as exc:
            raise _http_error(exc) from exc
    _schedule_match_loop(str(state["match_id"]))
    return {"match": state, "player_session": credentials}


@app.post("/fighter-api/matches/{match_id}/join", status_code=201)
async def join_match(
    match_id: str, request: JoinMatchRequest
) -> dict[str, Any]:
    try:
        state, credentials = await matches.join_match(
            match_id=match_id, display_name=request.display_name
        )
    except MatchStateError as exc:
        raise _http_error(exc) from exc
    return {"match": state, "player_session": credentials}


@app.get("/fighter-api/matches/{match_id}")
async def get_match(match_id: str) -> dict[str, Any]:
    try:
        return {"match": await matches.snapshot(match_id)}
    except MatchStateError as exc:
        raise _http_error(exc) from exc


@app.post("/fighter-api/matches/{match_id}/character")
async def select_character(
    match_id: str,
    request: CharacterSelectionRequest,
    credentials: Annotated[tuple[str, str], Depends(_credentials)],
) -> dict[str, Any]:
    player_id, session_token = credentials
    try:
        state = await matches.select_character(
            match_id=match_id,
            player_id=player_id,
            session_token=session_token,
            character_id=request.character_id,
        )
    except MatchStateError as exc:
        raise _http_error(exc) from exc
    _schedule_round_timeout(match_id, state)
    if state.get("status") == "active":
        _schedule_match_loop(match_id)
    return {"match": state}


async def _broadcast(
    sockets: set[WebSocket], payload: dict[str, Any]
) -> None:
    disconnected: list[WebSocket] = []
    for socket in tuple(sockets):
        try:
            await socket.send_json(payload)
        except (RuntimeError, WebSocketDisconnect):
            disconnected.append(socket)
    for socket in disconnected:
        sockets.discard(socket)


def _schedule_round_timeout(match_id: str, state: dict[str, Any]) -> None:
    if state.get("status") != "active":
        return
    existing = app.state.round_tasks.get(match_id)
    if existing is not None and not existing.done():
        return

    async def complete_round() -> None:
        try:
            await asyncio.sleep(int(state["round_seconds_remaining"]) + 0.05)
            completed = await matches.expire_round(match_id)
            await _broadcast(
                app.state.sockets.get(match_id, set()),
                {"type": "match_state", "match": completed},
            )
        finally:
            app.state.round_tasks.pop(match_id, None)

    app.state.round_tasks[match_id] = asyncio.create_task(complete_round())


def _schedule_match_loop(match_id: str) -> None:
    existing = app.state.match_tasks.get(match_id)
    if existing is not None and not existing.done():
        return

    async def run_match() -> None:
        final_state: dict[str, Any] | None = None
        try:
            while True:
                await asyncio.sleep(0.05)
                state = await matches.tick_match(match_id)
                await _broadcast(
                    app.state.sockets.get(match_id, set()),
                    {"type": "match_state", "match": state},
                )
                if state.get("status") != "active":
                    final_state = state
                    break
        except MatchStateError:
            pass
        finally:
            if final_state is not None:
                await asyncio.sleep(COMPLETED_MATCH_TTL_SECONDS)
                await _evict_match(match_id, cancel_match_task=False)
            app.state.match_tasks.pop(match_id, None)

    app.state.match_tasks[match_id] = asyncio.create_task(run_match())


@app.websocket("/fighter-ws/{match_id}")
async def match_socket(websocket: WebSocket, match_id: str) -> None:
    await websocket.accept()
    sockets: set[WebSocket] = websocket.app.state.sockets.setdefault(
        match_id, set()
    )
    player_id = ""
    session_token = ""
    authenticated = False
    try:
        first_message = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        if first_message.get("type") != "authenticate":
            await websocket.close(code=4401, reason="authentication required")
            return
        player_id = str(first_message.get("player_id", ""))
        session_token = str(first_message.get("session_token", ""))
        state = await matches.set_connected(
            match_id=match_id,
            player_id=player_id,
            session_token=session_token,
            connected=True,
        )
        authenticated = True
        sockets.add(websocket)
        player_key = (match_id, player_id)
        player_sockets: set[WebSocket] = (
            websocket.app.state.player_sockets.setdefault(player_key, set())
        )
        player_sockets.add(websocket)
        await _broadcast(sockets, {"type": "match_state", "match": state})

        while True:
            message = await websocket.receive_json()
            try:
                message_type = message.get("type")
                if message_type == "select_character":
                    state = await matches.select_character(
                        match_id=match_id,
                        player_id=player_id,
                        session_token=session_token,
                        character_id=str(message.get("character_id", "")),
                    )
                    _schedule_round_timeout(match_id, state)
                    if state.get("status") == "active":
                        _schedule_match_loop(match_id)
                elif message_type == "player_input":
                    state = await matches.player_input(
                        match_id=match_id,
                        player_id=player_id,
                        session_token=session_token,
                        action=str(message.get("action", "")),
                    )
                elif message_type == "combat_action":
                    state = await matches.combat_action(
                        match_id=match_id,
                        player_id=player_id,
                        session_token=session_token,
                        action=str(message.get("action", "")),
                    )
                elif message_type == "sync":
                    state = await matches.snapshot(match_id)
                else:
                    raise MatchStateError("message type is not supported")
            except MatchStateError as exc:
                await websocket.send_json(
                    {"type": "error", "detail": str(exc)}
                )
                continue
            await _broadcast(sockets, {"type": "match_state", "match": state})
    except (MatchStateError, CoreRailValidationError) as exc:
        await websocket.send_json({"type": "error", "detail": str(exc)})
    except (asyncio.TimeoutError, WebSocketDisconnect):
        pass
    finally:
        sockets.discard(websocket)
        if authenticated:
            player_key = (match_id, player_id)
            player_sockets = websocket.app.state.player_sockets.get(
                player_key, set()
            )
            player_sockets.discard(websocket)
            if not player_sockets:
                websocket.app.state.player_sockets.pop(player_key, None)
                try:
                    state = await matches.set_connected(
                        match_id=match_id,
                        player_id=player_id,
                        session_token=session_token,
                        connected=False,
                    )
                    await _broadcast(
                        sockets, {"type": "match_state", "match": state}
                    )
                except MatchStateError:
                    pass


@app.on_event("startup")
async def initialize_socket_registry() -> None:
    app.state.sockets = {}
    app.state.round_tasks = {}
    app.state.match_tasks = {}
    app.state.player_sockets = {}
    app.state.creation_attempts = {}
    app.state.creation_lock = asyncio.Lock()
    app.state.cleanup_task = asyncio.create_task(_cleanup_stale_matches())


async def _cleanup_stale_matches() -> None:
    while True:
        await asyncio.sleep(10)
        removed = await matches.cleanup_stale()
        for match_id in removed:
            await _evict_match(match_id)


async def _evict_match(
    match_id: str, *, cancel_match_task: bool = True
) -> None:
    sockets = tuple(app.state.sockets.pop(match_id, set()))
    stale_keys = [
        key for key in app.state.player_sockets if key[0] == match_id
    ]
    for key in stale_keys:
        app.state.player_sockets.pop(key, None)
    await matches.remove_match(match_id)
    for socket in sockets:
        try:
            await socket.close(code=1000, reason="match retention expired")
        except RuntimeError:
            pass
    round_task = app.state.round_tasks.pop(match_id, None)
    if round_task is not None and round_task is not asyncio.current_task():
        round_task.cancel()
    if cancel_match_task:
        match_task = app.state.match_tasks.pop(match_id, None)
        if (
            match_task is not None
            and match_task is not asyncio.current_task()
        ):
            match_task.cancel()


@app.on_event("shutdown")
async def stop_cleanup_task() -> None:
    cleanup_task = getattr(app.state, "cleanup_task", None)
    if cleanup_task is not None:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8008"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")