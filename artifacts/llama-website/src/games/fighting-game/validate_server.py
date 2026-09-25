"""Compile and boot-check the isolated fighting-game backend."""

from __future__ import annotations

import compileall
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

GAME_DIR = Path(__file__).resolve().parent


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            **({"Content-Type": "application/json"} if payload is not None else {}),
            **(headers or {}),
        },
        method=method,
    )
    with urlopen(request, timeout=5) as response:
        return json.load(response)


def main() -> int:
    if not compileall.compile_dir(GAME_DIR, quiet=1, force=True):
        raise RuntimeError("fighting-game backend compilation failed")

    port = available_port()
    server = subprocess.Popen(
        [sys.executable, str(GAME_DIR / "server.py")],
        cwd=GAME_DIR,
        env={**dict(__import__("os").environ), "PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if server.poll() is not None:
                output = server.stdout.read() if server.stdout else ""
                raise RuntimeError(f"server exited before ready:\n{output}")
            try:
                with urlopen(
                    f"{base_url}/fighter-api/healthz", timeout=1
                ) as response:
                    health = json.load(response)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("server did not become ready within 20 seconds")

        created = request_json(
            f"{base_url}/fighter-api/matches",
            method="POST",
            payload={
                "display_name": "Validator One",
            },
        )
        entry = created["match"]["entry_validation"]
        match_id = str(created["match"]["match_id"])
        first = created["player_session"]
        joined = request_json(
            f"{base_url}/fighter-api/matches/{match_id}/join",
            method="POST",
            payload={"display_name": "Validator Two"},
        )
        second = joined["player_session"]
        for session, character in (
            (first, "andean-guardian"),
            (second, "neon-puma"),
        ):
            selected = request_json(
                f"{base_url}/fighter-api/matches/{match_id}/character",
                method="POST",
                payload={"character_id": character},
                headers={
                    "X-Player-Id": str(session["player_id"]),
                    "X-Session-Token": str(session["session_token"]),
                },
            )
        active_match = selected["match"]
        if (
            health.get("status") != "ok"
            or entry.get("target_usd") != "0.25"
            or entry.get("amount_tlama") != 25
            or not entry.get("accepted")
            or active_match.get("status") != "active"
            or active_match.get("round_seconds_remaining") != 99
            or any(
                player.get("health") != 100
                for player in active_match.get("players", {}).values()
            )
        ):
            raise RuntimeError(
                "health, entry validation, or match startup was incorrect"
            )
        print(
            json.dumps(
                {
                    "compiled": True,
                    "server_started": True,
                    "sandbox_host": "127.0.0.1",
                    "sandbox_port": port,
                    "entry_target_usd": entry["target_usd"],
                    "entry_amount_tlama": entry["amount_tlama"],
                    "match_status": active_match["status"],
                    "round_seconds_remaining": active_match[
                        "round_seconds_remaining"
                    ],
                    "authority": health["authority"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())