from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server
from entry_rail import FightingGameEntryRail
from match_state import MatchManager


class FightingGameRealtimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        server.matches = MatchManager(FightingGameEntryRail("0.01"))
        self.client_context = TestClient(server.app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def _create_match(self, *, ai: bool = False) -> dict[str, object]:
        suffix = "/ai" if ai else ""
        response = self.client.post(
            f"/fighter-api/matches{suffix}",
            json={"display_name": "Realtime Player"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    @staticmethod
    def _authenticate(socket: object, session: dict[str, str]) -> None:
        socket.send_json(
            {
                "type": "authenticate",
                "player_id": session["player_id"],
                "session_token": session["session_token"],
            }
        )

    def _sync_until_state(self, socket: object) -> dict[str, object]:
        socket.send_json({"type": "sync"})
        for _ in range(6):
            payload = socket.receive_json()
            if payload.get("type") == "match_state":
                return payload["match"]
        self.fail("socket did not return match state")

    def test_overlapping_player_sockets_keep_presence_until_last_close(
        self,
    ) -> None:
        created = self._create_match()
        match_id = created["match"]["match_id"]
        session = created["player_session"]
        socket_path = f"/fighter-ws/{match_id}"

        with self.client.websocket_connect(socket_path) as first:
            self._authenticate(first, session)
            first.receive_json()
            with self.client.websocket_connect(socket_path) as second:
                self._authenticate(second, session)
                second.receive_json()
                first.close()
                state = self._sync_until_state(second)
                self.assertTrue(
                    state["players"]["player_1"]["connected"],
                    "closing an older socket must not disconnect its replacement",
                )

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            state = self.client.get(
                f"/fighter-api/matches/{match_id}"
            ).json()["match"]
            if not state["players"]["player_1"]["connected"]:
                break
            time.sleep(0.01)
        self.assertFalse(state["players"]["player_1"]["connected"])
        self.assertNotIn((match_id, session["player_id"]), server.app.state.player_sockets)

    def test_completed_match_closes_socket_and_releases_all_registries(
        self,
    ) -> None:
        with mock.patch.object(server, "COMPLETED_MATCH_TTL_SECONDS", 0.02):
            created = self._create_match(ai=True)
            match_id = created["match"]["match_id"]
            session = created["player_session"]

            with self.client.websocket_connect(
                f"/fighter-ws/{match_id}"
            ) as socket:
                self._authenticate(socket, session)
                socket.receive_json()

                async def complete_match() -> None:
                    async with server.matches._lock:
                        match = server.matches._matches[match_id]
                        match.status = "round_complete"
                        match.completed_at = time.monotonic()

                self.client.portal.call(complete_match)
                with self.assertRaises(WebSocketDisconnect):
                    while True:
                        socket.receive_json()

            self.assertEqual(
                self.client.get(f"/fighter-api/matches/{match_id}").status_code,
                404,
            )
            self.assertNotIn(match_id, server.app.state.sockets)
            self.assertNotIn(match_id, server.app.state.match_tasks)
            self.assertNotIn(match_id, server.app.state.round_tasks)
            self.assertFalse(
                any(key[0] == match_id for key in server.app.state.player_sockets)
            )

    def test_abandoned_match_eviction_closes_socket_and_releases_registries(
        self,
    ) -> None:
        created = self._create_match()
        match_id = created["match"]["match_id"]
        session = created["player_session"]

        with self.client.websocket_connect(f"/fighter-ws/{match_id}") as socket:
            self._authenticate(socket, session)
            socket.receive_json()

            async def abandon_and_evict() -> None:
                async with server.matches._lock:
                    match = server.matches._matches[match_id]
                    match.players["player_1"].connected = False
                    match.last_activity_at = time.monotonic() - 121
                removed = await server.matches.cleanup_stale()
                self.assertEqual(removed, [match_id])
                for removed_id in removed:
                    await server._evict_match(removed_id)

            self.client.portal.call(abandon_and_evict)
            with self.assertRaises(WebSocketDisconnect):
                socket.receive_json()

        self.assertEqual(
            self.client.get(f"/fighter-api/matches/{match_id}").status_code,
            404,
        )
        self.assertNotIn(match_id, server.app.state.sockets)
        self.assertNotIn(match_id, server.app.state.match_tasks)
        self.assertNotIn(match_id, server.app.state.round_tasks)
        self.assertFalse(
            any(key[0] == match_id for key in server.app.state.player_sockets)
        )

    def test_concurrent_creation_burst_obeys_per_client_limit(self) -> None:
        def create(index: int) -> int:
            return self.client.post(
                "/fighter-api/matches",
                json={"display_name": f"Player {index}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=12) as executor:
            statuses = list(executor.map(create, range(12)))

        self.assertEqual(statuses.count(201), server.CREATE_LIMIT_PER_WINDOW)
        self.assertEqual(statuses.count(429), 2)
        self.assertEqual(
            self.client.portal.call(server.matches.match_count),
            server.CREATE_LIMIT_PER_WINDOW,
        )

    def test_concurrent_creation_burst_obeys_global_capacity(self) -> None:
        def create(index: int) -> int:
            return self.client.post(
                "/fighter-api/matches",
                json={"display_name": f"Player {index}"},
            ).status_code

        with (
            mock.patch.object(server, "CREATE_LIMIT_PER_WINDOW", 20),
            mock.patch.object(server, "MAX_RESIDENT_MATCHES", 4),
            ThreadPoolExecutor(max_workers=10) as executor,
        ):
            statuses = list(executor.map(create, range(10)))

        self.assertEqual(statuses.count(201), 4)
        self.assertEqual(statuses.count(503), 6)
        self.assertEqual(self.client.portal.call(server.matches.match_count), 4)


if __name__ == "__main__":
    unittest.main()