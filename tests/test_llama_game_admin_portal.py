"""Focused contracts for the server-authoritative Arcade testing portal."""

import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

GAME_DIR = Path(__file__).resolve().parents[1] / "llama_website" / "llama_game"
import sys

sys.path.insert(0, str(GAME_DIR))

from backend import (  # noqa: E402
    ADMIN_MAX_ATTEMPTS_GLOBAL,
    ADMIN_MAX_ATTEMPTS_PER_IP,
    ADMIN_MAX_IP_BUCKETS,
    LEVEL_THREE_ORB_RELOCATE_SECONDS,
    AdminPortalSecurity,
    GameRoom,
    LEVELS,
    Player,
    _admin_allowed_origins,
)
from storage import ArcadeStorage  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from backend import app, admin_security  # noqa: E402


class FakeSocket:
    def __init__(self, cookie: str = ""):
        self.headers = {"cookie": cookie}
        self.messages = []
        self.closed = None

    async def send_json(self, message):
        self.messages.append(copy.deepcopy(message))

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class AdminSecurityTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "TLAMA_ARCADE_ADMIN_PORTAL_ENABLED": "true",
                "TLAMA_ARCADE_ADMIN_PASSWORD": "correct horse battery staple",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.security = AdminPortalSecurity()

    def test_missing_or_disabled_secret_fails_closed(self):
        session, csrf = self.security.new_session()
        with patch.dict(
            os.environ,
            {"TLAMA_ARCADE_ADMIN_PORTAL_ENABLED": "false"},
        ):
            self.assertFalse(
                self.security.authenticate(session, csrf, "correct horse battery staple", "127.0.0.1")[0]
            )
        with patch.dict(
            os.environ,
            {"TLAMA_ARCADE_ADMIN_PORTAL_ENABLED": "true", "TLAMA_ARCADE_ADMIN_PASSWORD": ""},
        ):
            self.assertFalse(
                self.security.authenticate(session, csrf, "correct horse battery staple", "127.0.0.1")[0]
            )

    def test_authentication_mints_server_only_grant_without_client_target(self):
        session, csrf = self.security.new_session()
        ok, result = self.security.authenticate(
            session, csrf, "correct horse battery staple", "127.0.0.1"
        )
        self.assertTrue(ok)
        self.assertEqual(result, "authenticated")
        self.assertTrue(self.security.sessions[session].grant_hash)
        self.assertEqual(self.security.sessions[session].player, None)

    def test_bad_password_and_rate_limit(self):
        session, csrf = self.security.new_session()
        for _ in range(ADMIN_MAX_ATTEMPTS := 5):
            ok, reason = self.security.authenticate(session, csrf, "wrong", "192.0.2.4")
            self.assertFalse(ok)
            self.assertIn(reason, {"invalid_password", "rate_limited"})
        self.assertEqual(
            self.security.authenticate(session, csrf, "correct horse battery staple", "192.0.2.4")[1],
            "rate_limited",
        )

    def test_session_socket_player_binding_and_atomic_one_use(self):
        session, csrf = self.security.new_session()
        ok, result = self.security.authenticate(
            session, csrf, "correct horse battery staple", "127.0.0.1"
        )
        self.assertTrue(ok)
        socket = FakeSocket(f"tlama_arcade_admin_session={session}")
        self.assertTrue(self.security.bind_websocket(socket))
        player = Player("player", "Player")
        self.security.bind_player(socket, player)
        results = []

        async def consume():
            results.extend(
                await asyncio.gather(
                    asyncio.to_thread(self.security.consume, socket, player),
                    asyncio.to_thread(self.security.consume, socket, player),
                )
            )

        asyncio.run(consume())
        self.assertEqual(sum(results), 1)
        self.assertIn(session, self.security.sessions)
        self.assertEqual(self.security.sessions[session].grant_hash, "")
        self.assertEqual(result, "authenticated")

    def test_expiry_replay_and_disconnect_revoke(self):
        session, csrf = self.security.new_session()
        ok, _ = self.security.authenticate(session, csrf, "correct horse battery staple", "127.0.0.1")
        self.assertTrue(ok)
        socket = FakeSocket(f"tlama_arcade_admin_session={session}")
        self.assertTrue(self.security.bind_websocket(socket))
        player = Player("player", "Player")
        self.security.bind_player(socket, player)
        with patch.object(
            self.security,
            "_now",
            return_value=self.security.sessions[session].expires_at + 1,
        ):
            self.assertFalse(self.security.consume(socket, player))
        self.security.disconnect(socket)
        self.assertNotIn(session, self.security.sessions)

        session, csrf = self.security.new_session()
        self.assertTrue(
            self.security.authenticate(session, csrf, "correct horse battery staple", "127.0.0.1")[0]
        )
        socket = FakeSocket(f"tlama_arcade_admin_session={session}")
        self.assertTrue(self.security.bind_websocket(socket))
        self.security.disconnect(socket)
        self.assertIn(session, self.security.sessions)
        self.assertEqual(self.security.sessions[session].grant_hash, "")

    def test_grant_is_bound_to_one_socket_and_exact_player_across_reconnect(self):
        session, csrf = self.security.new_session()
        self.assertTrue(
            self.security.authenticate(
                session, csrf, "correct horse battery staple", "127.0.0.1"
            )[0]
        )
        socket = FakeSocket(f"tlama_arcade_admin_session={session}")
        other_socket = FakeSocket(f"tlama_arcade_admin_session={session}")
        self.assertTrue(self.security.bind_websocket(socket))
        self.assertFalse(self.security.bind_websocket(other_socket))
        player = Player("player-a", "A")
        other_player = Player("player-b", "B")
        self.security.bind_player(socket, player)
        self.assertFalse(self.security.consume(socket, other_player))
        self.security.disconnect(socket)
        self.assertTrue(self.security.bind_websocket(other_socket))
        self.assertFalse(self.security.consume(other_socket, player))
        self.assertFalse(self.security.consume(other_socket, other_player))

    def test_rate_limiter_is_hard_bounded_for_one_ip(self):
        session, csrf = self.security.new_session()
        for _ in range(1000):
            self.security.authenticate(session, csrf, "wrong", "192.0.2.77")
        ip_key = self.security._ip_hash("192.0.2.77")
        self.assertLessEqual(len(self.security.ip_attempts), 1)
        self.assertLessEqual(
            len(self.security.ip_attempts.get(ip_key, ())),
            ADMIN_MAX_ATTEMPTS_PER_IP,
        )
        self.assertLessEqual(len(self.security.global_attempts), ADMIN_MAX_ATTEMPTS_GLOBAL)

    def test_rate_limiter_refuses_distributed_bucket_exhaustion(self):
        session, csrf = self.security.new_session()
        for index in range(ADMIN_MAX_IP_BUCKETS):
            self.security.authenticate(
                session,
                csrf,
                "wrong",
                f"198.51.{index // 256}.{index % 256}",
            )
            # Isolate per-IP bucket creation so the global window does not
            # mask the distinct-bucket hard cap.
            with self.security._lock:
                self.security.global_attempts.clear()
        self.assertEqual(len(self.security.ip_attempts), ADMIN_MAX_IP_BUCKETS)
        for index in range(1000):
            self.security.authenticate(
                session,
                csrf,
                "wrong",
                f"203.0.{index // 256}.{index % 256}",
            )
        self.assertLessEqual(len(self.security.ip_attempts), ADMIN_MAX_IP_BUCKETS)
        self.assertLessEqual(len(self.security.global_attempts), ADMIN_MAX_ATTEMPTS_GLOBAL)
        self.assertLessEqual(
            max((len(attempts) for attempts in self.security.ip_attempts.values()), default=0),
            ADMIN_MAX_ATTEMPTS_PER_IP,
        )


class AdminOverrideTests(unittest.IsolatedAsyncioTestCase):
    async def make_room(self):
        directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, directory.cleanup)
        room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
        socket = FakeSocket()
        player = Player("00000000-0000-4000-8000-000000000001", "Operator")
        room.connections[socket] = player
        return room, socket, player

    async def test_all_exact_baselines_and_level_ten_not_victory(self):
        for level, config in enumerate(LEVELS, 1):
            room, socket, player = await self.make_room()
            await room.admin_override_level(socket, level)
            self.assertEqual(player.score, config.min_score)
            self.assertEqual(player.current_level, level)
            self.assertFalse(player.victory_announced)
            self.assertFalse(player.game_over)
            self.assertEqual(player.lives, 3)
            self.assertTrue(player.admin_test_mode)
            self.assertEqual(player.ownership_token, "")
            self.assertNotIn("admin_test_mode", room.snapshot()["players"][0])
            if level == 10:
                self.assertEqual(player.score, 2401)
                self.assertFalse(player.victory_announced)

    async def test_cosmetic_test_grant_is_bound_to_player_and_not_persisted(self):
        room, socket, player = await self.make_room()
        other_socket = FakeSocket()
        other = Player("00000000-0000-4000-8000-000000000002", "Other")
        room.connections[other_socket] = other
        with patch.object(admin_security, "consume", return_value=True):
            await room._admin_configure_test(
                socket,
                {
                    "type": "admin_configure_test",
                    "level": 2,
                    "test_skin_id": "aurora",
                },
            )
        self.assertEqual(player.admin_test_skin_ids, ("aurora",))
        self.assertEqual(other.admin_test_skin_ids, ())
        self.assertNotIn("admin_test_skin_ids", room.snapshot()["players"][0])
        self.assertNotIn("owned_skin_ids", room.snapshot()["players"][0])
        personalized = room._personalize_message(
            socket, {"type": "state", **room.snapshot()}
        )
        self.assertEqual(
            personalized["players"][0]["owned_skin_ids"], ["aurora", "default"]
        )
        self.assertNotIn("aurora", player.owned_skin_ids)

        await room._equip_skin(
            socket, {"type": "equip_skin", "skin_id": "aurora"}
        )
        self.assertEqual(player.equipped_skin_id, "aurora")
        self.assertEqual(
            room.storage.summary(),
            {"saved_players": 0, "saved_match_tickets": 0},
        )
        player.game_over = True
        await room._reset(socket)
        self.assertFalse(player.admin_test_mode)
        self.assertEqual(player.admin_test_skin_ids, ())
        self.assertEqual(player.equipped_skin_id, "default")

    async def test_cosmetic_admin_payload_rejects_unknown_skin(self):
        room, socket, player = await self.make_room()
        with patch.object(admin_security, "consume", return_value=True) as consume:
            await room._admin_configure_test(
                socket,
                {
                    "type": "admin_configure_test",
                    "level": 2,
                    "test_skin_id": "injected-skin",
                },
            )
        self.assertFalse(player.admin_test_mode)
        consume.assert_not_called()
        self.assertEqual(socket.messages[-1]["type"], "error")

    async def test_private_worlds_are_initialized_and_shared_state_is_unchanged(self):
        room, socket, player = await self.make_room()
        other_socket = FakeSocket()
        other = Player("00000000-0000-4000-8000-000000000002", "Other", score=801, current_level=6)
        room.connections[other_socket] = other
        original_map = copy.deepcopy(room.collectibles)
        original_other = copy.deepcopy(other)
        await room.admin_override_level(socket, 8)
        self.assertIn(player.player_id, room.level8_worlds)
        self.assertNotIn(player.player_id, room.level7_worlds)
        self.assertEqual(room.collectibles, original_map)
        self.assertEqual(other, original_other)

    async def test_success_event_is_private_while_state_refresh_is_shared(self):
        room, socket, player = await self.make_room()
        other_socket = FakeSocket()
        room.connections[other_socket] = Player(
            "00000000-0000-4000-8000-000000000002", "Other"
        )
        await room.admin_override_level(socket, 2)
        self.assertTrue(
            any(message.get("event") == "admin_level_selected" for message in socket.messages)
        )
        self.assertFalse(
            any(message.get("event") == "admin_level_selected" for message in other_socket.messages)
        )
        self.assertTrue(any(message.get("type") == "state" for message in other_socket.messages))

    async def test_test_mode_never_persists_on_collect_or_disconnect(self):
        room, socket, player = await self.make_room()
        await room.admin_override_level(socket, 1)
        calls = []
        room.storage.record_score = lambda *args: calls.append(("score", args))
        room.storage.complete_match_ticket = lambda *args, **kwargs: calls.append(("ticket", args))
        player.x, player.y = room.collectibles["orb-1"]["x"], room.collectibles["orb-1"]["y"]
        await room.handle_message(socket, {"type": "collect", "orb_id": "orb-1"})
        await room.disconnect(socket)
        self.assertEqual(calls, [])

    async def test_private_collect_and_transition_do_not_touch_normal_player_or_room(self):
        room, socket, tester = await self.make_room()
        other_socket = FakeSocket()
        other = Player(
            "00000000-0000-4000-8000-000000000002",
            "Other",
            score=25,
            current_level=1,
        )
        room.connections[other_socket] = other
        await room.admin_override_level(socket, 1)
        tester.score = 100
        tester.x, tester.y = 0.22, 0.24
        room_map_before = copy.deepcopy(room.collectibles)
        wardens_before = copy.deepcopy(room.wardens)
        other_before = copy.deepcopy(other)
        persistence_calls = []
        room.storage.record_score = lambda *args: persistence_calls.append(("score", args))
        room.storage.complete_match_ticket = lambda *args, **kwargs: persistence_calls.append(
            ("ticket", args)
        )
        room.storage.record_opening_milestone = lambda *args: persistence_calls.append(
            ("opening", args)
        )

        await room.handle_message(socket, {"type": "collect", "orb_id": "orb-1"})

        self.assertEqual(room.collectibles, room_map_before)
        self.assertEqual(room.wardens, wardens_before)
        self.assertEqual(other, other_before)
        self.assertEqual(persistence_calls, [])
        self.assertEqual(tester.current_level, 2)
        self.assertIn(tester.player_id, room.admin_collectibles)
        self.assertTrue(
            any(message.get("event") == "level_up" for message in socket.messages)
        )
        self.assertTrue(
            any(
                message.get("collectibles") == room.admin_collectibles[tester.player_id]
                for message in socket.messages
            )
        )
        await room._send_state()
        tester_state = socket.messages[-1]
        normal_state = other_socket.messages[-1]
        self.assertEqual(tester_state["collectibles"], room.admin_collectibles[tester.player_id])
        self.assertEqual(normal_state["collectibles"], room.collectibles)
        self.assertFalse(
            any(message.get("event") in {"orb_collected", "level_up"} for message in other_socket.messages)
        )

    async def test_each_level_one_to_six_uses_private_hazards_and_shared_timer_is_unchanged(self):
        for level in range(1, 7):
            room, socket, tester = await self.make_room()
            other_socket = FakeSocket()
            other = Player(
                f"00000000-0000-4000-8000-{level:012d}",
                "Other",
                current_level=1,
            )
            room.connections[other_socket] = other
            other_before = copy.deepcopy(other)
            await room.admin_override_level(socket, level)
            map_before = copy.deepcopy(room.collectibles)
            wardens_before = copy.deepcopy(room.wardens)
            relocation_before = room.orb_relocation_tick
            tester_hazards = room._hazards_for_player(tester, now=room.hazard_started_at + 1)
            self.assertTrue(any(hazard.get("active") for hazard in tester_hazards))
            self.assertEqual(room.collectibles, map_before)
            self.assertEqual(room.wardens, wardens_before)
            room.advance_wardens(room.hazard_started_at + 1)
            if level == 3:
                room.relocate_level_three_orbs()
            self.assertEqual(room.wardens, wardens_before)
            self.assertEqual(room.collectibles, map_before)
            self.assertEqual(room.orb_relocation_tick, relocation_before)
            room._advance_admin_hazards(room.hazard_started_at + 1)
            if level == 6:
                self.assertIsNot(
                    room.admin_hazards[tester.player_id]["wardens"],
                    room.wardens,
                )
            room.admin_collectibles[tester.player_id]["private-marker"] = {
                "x": 0.44,
                "y": 0.44,
            }
            self.assertEqual(other, other_before)
            await room._send_state()
            normal_snapshot = other_socket.messages[-1]
            tester_snapshot = socket.messages[-1]
            self.assertEqual(normal_snapshot["collectibles"], room.collectibles)
            self.assertNotEqual(
                tester_snapshot["collectibles"],
                normal_snapshot["collectibles"],
            )

    async def test_actual_private_level_three_timer_isolated_and_single_event(self):
        room, socket, tester = await self.make_room()
        normal_socket = FakeSocket()
        normal = Player(
            "00000000-0000-4000-8000-000000000002",
            "Normal",
            current_level=1,
        )
        room.connections[normal_socket] = normal
        await room.admin_override_level(socket, 3)
        started = room.admin_relocations[tester.player_id]["last_relocation_at"]
        private_before = copy.deepcopy(room.admin_collectibles[tester.player_id])
        shared_before = copy.deepcopy(room.collectibles)
        shared_tick = room.orb_relocation_tick

        await room._level_three_orb_tick(
            started + LEVEL_THREE_ORB_RELOCATE_SECONDS - 0.001
        )
        self.assertEqual(room.admin_collectibles[tester.player_id], private_before)
        self.assertEqual(room.collectibles, shared_before)
        self.assertEqual(
            len([message for message in socket.messages if message.get("event") == "orbs_relocated"]),
            0,
        )

        socket.messages.clear()
        normal_socket.messages.clear()
        await room._level_three_orb_tick(started + LEVEL_THREE_ORB_RELOCATE_SECONDS)
        self.assertNotEqual(room.admin_collectibles[tester.player_id], private_before)
        self.assertEqual(room.collectibles, shared_before)
        self.assertEqual(room.orb_relocation_tick, shared_tick)
        private_events = [
            message for message in socket.messages
            if message.get("event") == "orbs_relocated"
        ]
        self.assertEqual(len(private_events), 1)
        self.assertEqual(normal_socket.messages, [])
        self.assertEqual(
            private_events[0]["collectibles"],
            room.admin_collectibles[tester.player_id],
        )

        # The hazard tick likewise gives the tester one private event and
        # excludes the tester from the normal shared broadcast.
        socket.messages.clear()
        normal_socket.messages.clear()
        await room._hazard_tick(started + 1)
        self.assertEqual(
            len([message for message in socket.messages if message.get("event") == "hazard_update"]),
            1,
        )
        self.assertEqual(
            len([message for message in normal_socket.messages if message.get("event") == "hazard_update"]),
            1,
        )

    async def test_invalid_levels_do_not_reset_or_consume_server_state(self):
        room, socket, player = await self.make_room()
        original = copy.deepcopy(player)
        for invalid in (True, False, 1.0, "1", None, 0, 11):
            self.assertFalse(await room.admin_override_level(socket, invalid))
        self.assertEqual(player, original)


class AdminSourceBoundaryTests(unittest.TestCase):
    def test_client_has_no_target_header_or_secret_storage(self):
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("X-TLAMA-Player-ID", source)
        self.assertIn("adminPassword.value = \"\";", source)
        self.assertNotIn("localStorage.setItem(\"tlama-arcade-admin", source)
        self.assertNotIn("sessionStorage.setItem(\"tlama-arcade-admin", source)
        self.assertNotIn("admin_select_level=", source)

    def test_backend_rejects_admin_targets_and_omits_server_provenance(self):
        source = (GAME_DIR / "backend.py").read_text(encoding="utf-8")
        self.assertIn('set(payload) != {"type", "level"}', source)
        self.assertIn('"admin_test_mode"', source)
        self.assertIn("if not player.admin_test_mode", source)

    def test_portless_loopback_preview_origins_are_development_only(self):
        with patch.dict(
            os.environ,
            {
                "TLAMA_ARCADE_ALLOWED_ORIGINS": "",
                "TLAMA_ARCADE_PUBLISHED_ORIGIN": "",
                "TLAMA_ARCADE_PRODUCTION": "false",
                "REPLIT_DEPLOYMENT": "false",
                "TLAMA_ARCADE_ENV": "development",
            },
            clear=False,
        ):
            self.assertIn("http://localhost", _admin_allowed_origins())
            self.assertIn("http://127.0.0.1", _admin_allowed_origins())
        with patch.dict(
            os.environ,
            {
                "TLAMA_ARCADE_ALLOWED_ORIGINS": "https://published.example",
                "TLAMA_ARCADE_PUBLISHED_ORIGIN": "https://published.example",
                "TLAMA_ARCADE_PRODUCTION": "true",
            },
            clear=False,
        ):
            self.assertNotIn("http://localhost", _admin_allowed_origins())
            self.assertNotIn("http://127.0.0.1", _admin_allowed_origins())


class AdminHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "TLAMA_ARCADE_ADMIN_PORTAL_ENABLED": "true",
                "TLAMA_ARCADE_ADMIN_PASSWORD": "correct horse battery staple",
                "TLAMA_ARCADE_ALLOWED_ORIGINS": "http://localhost:8099",
            },
            clear=False,
        )
        self.env.start()
        self.addAsyncCleanup(asyncio.to_thread, self.env.stop)
        with admin_security._lock:
            admin_security.sessions.clear()
            admin_security.ip_attempts.clear()
            admin_security.global_attempts.clear()
            admin_security.locked_until.clear()

    async def test_csrf_origin_and_cookie_contract(self):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8099") as client:
            status = await client.get("/admin/status")
            self.assertEqual(status.status_code, 200)
            self.assertTrue(status.json()["enabled"])
            csrf = client.cookies.get("tlama_arcade_admin_csrf")
            self.assertTrue(csrf)
            bad = await client.post(
                "/admin/auth",
                headers={"Origin": "http://evil.invalid", "Content-Type": "application/json"},
                json={"password": "correct horse battery staple", "csrf": csrf},
            )
            self.assertEqual(bad.status_code, 403)
            missing_origin = await client.post(
                "/admin/auth",
                headers={"Content-Type": "application/json"},
                json={"password": "correct horse battery staple", "csrf": csrf},
            )
            self.assertEqual(missing_origin.status_code, 403)
            missing_csrf = await client.post(
                "/admin/auth",
                headers={"Origin": "http://localhost:8099", "Content-Type": "application/json"},
                json={"password": "correct horse battery staple", "csrf": "wrong"},
            )
            self.assertEqual(missing_csrf.status_code, 401)
            good = await client.post(
                "/admin/auth",
                headers={"Origin": "http://localhost:8099", "Content-Type": "application/json"},
                json={"password": "correct horse battery staple", "csrf": csrf},
            )
            self.assertEqual(good.status_code, 200)
            cookies = good.headers.get("set-cookie", "")
            self.assertIn("HttpOnly", cookies)
            self.assertIn("samesite=strict", cookies.lower())
            self.assertNotIn("correct horse", good.text)
            cookie_names = {
                item.split("=", 1)[0]
                for item in cookies.split(", ")
                if "=" in item
            }
            self.assertEqual(
                cookie_names,
                {"tlama_arcade_admin_session", "tlama_arcade_admin_csrf"},
            )

    async def test_production_requires_secure_origin_and_proxy_contract(self):
        with patch.dict(
            os.environ,
            {
                "REPLIT_DEPLOYMENT": "true",
                "TLAMA_ARCADE_PUBLISHED_ORIGIN": "https://published.example",
                "TLAMA_ARCADE_ALLOWED_ORIGINS": "https://published.example",
            },
        ):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://published.example") as client:
                response = await client.post(
                    "/admin/auth",
                    headers={"Origin": "https://published.example", "Content-Type": "application/json"},
                    json={"password": "x", "csrf": "x"},
                )
                self.assertEqual(response.status_code, 403)

    async def test_production_proxy_auth_binds_ws_and_consumes_server_grant_once(self):
        with patch.dict(
            os.environ,
            {
                "REPLIT_DEPLOYMENT": "true",
                "TLAMA_ARCADE_PUBLISHED_ORIGIN": "https://published.example",
                "TLAMA_ARCADE_ALLOWED_ORIGINS": "https://published.example",
            },
        ):
            transport = ASGITransport(app=app)
            proxy_headers = {
                "Origin": "https://published.example",
                "X-Replit-Proxy": "replit",
                "X-Forwarded-Proto": "https",
            }
            async with AsyncClient(
                transport=transport, base_url="https://published.example"
            ) as client:
                status = await client.get(
                    "/admin/status",
                    headers={**proxy_headers, "Sec-Fetch-Site": "same-origin"},
                )
                self.assertEqual(status.status_code, 200)
                csrf = client.cookies.get("tlama_arcade_admin_csrf")
                response = await client.post(
                    "/admin/auth",
                    headers={**proxy_headers, "Content-Type": "application/json"},
                    json={
                        "password": "correct horse battery staple",
                        "csrf": csrf,
                    },
                )
                self.assertEqual(response.status_code, 200)
                cookies = client.cookies
                session = cookies.get("tlama_arcade_admin_session")
                self.assertTrue(session)
                self.assertTrue(admin_security.sessions[session].grant_hash)

            socket = FakeSocket(f"tlama_arcade_admin_session={session}")
            self.assertTrue(admin_security.bind_websocket(socket))
            directory = tempfile.TemporaryDirectory()
            self.addAsyncCleanup(asyncio.to_thread, directory.cleanup)
            room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
            player = Player("proxy-player", "Proxy")
            room.connections[socket] = player
            admin_security.bind_player(socket, player)
            await room._admin_select_level(
                socket, {"type": "admin_select_level", "level": 3}
            )
            self.assertTrue(player.admin_test_mode)
            self.assertEqual(
                len([message for message in socket.messages if message.get("event") == "admin_level_selected"]),
                1,
            )
            await room._admin_select_level(
                socket, {"type": "admin_select_level", "level": 4}
            )
            self.assertEqual(
                len([message for message in socket.messages if message.get("event") == "admin_level_selected"]),
                1,
            )


if __name__ == "__main__":
    unittest.main()