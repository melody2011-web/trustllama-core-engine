import asyncio
import copy
from decimal import Decimal
import json
import math
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from tests.analytics_outage_fixture import (
    ANALYTICS_FAILURE_MODES,
    analytics_outage_page,
)


GAME_DIR = Path(__file__).resolve().parents[1] / "llama_website" / "llama_game"
sys.path.insert(0, str(GAME_DIR))

from backend import (  # noqa: E402
    BUMPS_PER_LIFE,
    PUMA_RADIUS,
    HAZARD_STUN_SECONDS,
    LIFE_LOSS_INVULNERABILITY_SECONDS,
    LEVEL_ONE_CHIPS,
    LEVEL_ONE_FINISH_X,
    LEVEL9_AVATAR_HALF_HEIGHT,
    LEVEL9_AVATAR_HALF_WIDTH,
    LEVEL9_ORB_COUNT,
    GHOST_COLLISION_RADIUS,
    ARCADE_ENTRY_ITEM_ID,
    ARCADE_ENTRY_TARGET_USD,
    CHARACTER_SKINS_ITEM_ID,
    CHARACTER_SKINS_TARGET_USD,
    HEART_MATRIX_AMOUNT_TLAMA,
    HEART_MATRIX_TARGET_USD,
    MAZE_WALL_RADIUS,
    MAZE_WARDEN_RADIUS,
    GameRoom,
    TRACTOR_RADIUS,
    LEVELS,
    ORBS_PER_EXTRA_LIFE,
    MAX_PURCHASED_LIVES,
    STARTING_LIVES,
    STARTING_SHIELD_SECONDS,
    MOCK_TOKEN_PRICE_USD,
    PAYMENT_CATALOG,
    VICTORY_SCORE,
    Player,
    _verify_catalog_settlement_sync,
    _verify_extra_life_settlement_sync,
    _fetch_tlama_oracle_price_sync,
    _issue_oracle_quote,
    _validate_oracle_quote,
    _PAYMENT_QUOTE_CACHE,
    _required_tlama_atomic_for_usd,
    _required_tlama_for_usd,
    _clamp_coordinate,
    _safe_name,
    _strip_game_path,
    level_for_score,
    get_payment_config,
    payment_config,
)
from storage import ArcadeStorage  # noqa: E402


class ArcadeLevelTests(unittest.TestCase):
    def test_every_catalog_item_uses_mock_usd_conversion(self) -> None:
        targets = {
            ARCADE_ENTRY_ITEM_ID: ARCADE_ENTRY_TARGET_USD,
            "heart-matrix-upgrade": HEART_MATRIX_TARGET_USD,
            CHARACTER_SKINS_ITEM_ID: CHARACTER_SKINS_TARGET_USD,
        }
        self.assertEqual(
            {item["id"] for item in PAYMENT_CATALOG},
            set(targets),
        )
        for item in PAYMENT_CATALOG:
            target = targets[str(item["id"])]
            self.assertEqual(item["target_usd"], str(target))
            self.assertEqual(item["pricing"], "mock-token-price-usd")
            self.assertEqual(
                item["amount_tlama"],
                _required_tlama_for_usd(target, MOCK_TOKEN_PRICE_USD),
            )
        self.assertEqual(
            _required_tlama_for_usd(Decimal("0.25"), Decimal("0.03")),
            9,
        )
        self.assertEqual(
            _required_tlama_for_usd(Decimal("1.50"), Decimal("0.04")),
            38,
        )

    def test_pyth_price_validates_identity_freshness_confidence_and_shape(self) -> None:
        feed_id = "ab" * 32
        config = {
            "oracle_url": "https://hermes.example",
            "oracle_feed_id": feed_id,
            "oracle_exponent": -8,
            "oracle_max_age_seconds": 60,
            "oracle_max_confidence_bps": 100,
        }

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _limit):
                return json.dumps(self.payload).encode()

        def payload(**price_changes):
            price = {
                "price": "4000000",
                "conf": "10000",
                "expo": -8,
                "publish_time": 1_000,
            }
            price.update(price_changes)
            return {"parsed": [{"id": feed_id, "price": price}]}

        with patch("backend.urllib.request.urlopen", return_value=Response(payload())):
            self.assertEqual(
                _fetch_tlama_oracle_price_sync(config, now=1_030),
                (Decimal("0.04000000"), 1_000),
            )
        invalid_payloads = (
            {"parsed": [{"id": "cd" * 32, "price": payload()["parsed"][0]["price"]}]},
            payload(publish_time=900),
            payload(conf="50000"),
            payload(price="-1"),
            payload(expo=-6),
            {"parsed": []},
        )
        for invalid in invalid_payloads:
            with self.subTest(invalid=invalid), patch(
                "backend.urllib.request.urlopen", return_value=Response(invalid)
            ):
                with self.assertRaises(ValueError):
                    _fetch_tlama_oracle_price_sync(config, now=1_030)
        with patch("backend.urllib.request.urlopen", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                _fetch_tlama_oracle_price_sync(config, now=1_030)

    def test_authenticated_oracle_quote_survives_price_changes_and_expires(self) -> None:
        config = {
            "oracle_feed_id": "ab" * 32,
            "oracle_quote_ttl_seconds": 180,
            "decimals": 9,
        }
        with patch.dict(os.environ, {"SESSION_SECRET": "s" * 32}):
            quote = _issue_oracle_quote(
                config,
                item_id="heart-matrix-upgrade",
                atomic_amount=37_500_000_000,
                publish_time=1_000,
                now=1_030,
            )
            self.assertEqual(
                _validate_oracle_quote(
                    quote,
                    config,
                    expected_item_id="heart-matrix-upgrade",
                    expected_atomic_amount=37_500_000_000,
                    now=1_100,
                )["amount_atomic"],
                "37500000000",
            )
            with self.assertRaises(ValueError):
                _validate_oracle_quote(quote, config, now=1_211)
            with self.assertRaises(ValueError):
                _validate_oracle_quote(quote[:-1] + "A", config, now=1_100)

    def test_payment_config_issues_mock_quotes_for_every_catalog_item(self) -> None:
        settings = {
            "TLAMA_PAYMENT_ENABLED": "true",
            "TLAMA_MINT_ADDRESS": "m",
            "TLAMA_GAME_REWARDS_VAULT_ADDRESS": "v",
            "TLAMA_GAME_HOOK_PROGRAM_ADDRESS": "h",
            "TLAMA_PUBLIC_SOLANA_RPC_URL": "https://rpc.example",
            "SESSION_SECRET": "s" * 32,
        }
        with patch.dict(os.environ, settings, clear=False):
            config = payment_config()
        self.assertTrue(config["enabled"])
        self.assertEqual(
            {item["id"] for item in config["catalog"]},
            {ARCADE_ENTRY_ITEM_ID, "heart-matrix-upgrade", CHARACTER_SKINS_ITEM_ID},
        )
        for item in config["catalog"]:
            self.assertEqual(
                int(item["amount_atomic"]),
                int(item["amount_tlama"]) * 10 ** config["decimals"],
            )
            self.assertTrue(item["oracle_quote"])


class ArcadePaymentConfigConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_oracle_does_not_block_event_loop(self):
        progressed = False

        def slow_config():
            time.sleep(0.10)
            return {"enabled": False}

        async def ticker():
            nonlocal progressed
            await asyncio.sleep(0.01)
            progressed = True

        with patch("backend.payment_config", side_effect=slow_config):
            config_task = asyncio.create_task(get_payment_config())
            await ticker()
            self.assertTrue(progressed)
            self.assertFalse(config_task.done())
            self.assertEqual(await config_task, {"enabled": False})

    def test_arcade_artifact_defines_a_production_service(self) -> None:
        manifest_path = (
            GAME_DIR.parents[1]
            / "artifacts"
            / "llama-website"
            / ".replit-artifact"
            / "artifact.toml"
        )
        manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        arcade = next(
            service for service in manifest["services"] if service["name"] == "arcade"
        )
        self.assertEqual(manifest["previewPath"], "/")
        self.assertEqual(arcade["paths"], ["/llama-website/game/"])
        self.assertEqual(
            arcade["production"]["run"]["args"],
            [
                ".pythonlibs/bin/python",
                "-u",
                "llama_website/llama_game/backend.py",
            ],
        )
        self.assertEqual(
            arcade["production"]["health"]["startup"]["path"],
            "/healthz",
        )
        self.assertEqual(
            arcade["production"]["run"]["env"]["BASE_PATH"],
            "/llama-website/game",
        )

    def test_root_and_legacy_arcade_routes_resolve_to_the_same_app_paths(self) -> None:
        root_scope = {"path": "/ws", "root_path": ""}
        legacy_scope = {"path": "/llama-website/game/ws", "root_path": ""}

        self.assertEqual(_strip_game_path(root_scope)["path"], "/ws")
        self.assertEqual(
            _strip_game_path(legacy_scope),
            {"path": "/ws", "root_path": "/llama-website/game"},
        )

    def test_first_four_levels_have_distinct_music_themes(self) -> None:
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("const LEVEL_MUSIC = Object.freeze({", source)
        for level in range(1, 5):
            self.assertIn(f"          {level}: Object.freeze({{", source)
        self.assertNotIn("          5: Object.freeze({", source)
        self.assertIn("          6: Object.freeze({", source)
        self.assertIn("currentLevel.number === 5 || currentLevel.number === 6", source)

    def test_snapshot_authoritative_facing_is_cosmetic_and_scoped(self) -> None:
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("const AUTHORITATIVE_FACING_DEADBAND = 0.0005;", source)
        self.assertIn("const authoritativeFacing = new Map();", source)
        self.assertIn("const authoritativePlayerX = new Map();", source)

        apply_state_start = source.index("const applyState = (state) => {")
        apply_state_end = source.index("\n        const connect = () => {", apply_state_start)
        apply_state = source[apply_state_start:apply_state_end]
        self.assertIn("(state.players || []).forEach((player) => {", apply_state)
        self.assertIn("const previousX = authoritativePlayerX.get(playerId);", apply_state)
        self.assertIn(
            "Math.abs(snapshotX - previousX) > AUTHORITATIVE_FACING_DEADBAND",
            apply_state,
        )
        self.assertIn(
            "authoritativeFacing.set(playerId, snapshotX < previousX ? -1 : 1);",
            apply_state,
        )
        self.assertIn(
            "if (!authoritativeFacing.has(playerId)) authoritativeFacing.set(playerId, 1);",
            apply_state,
        )
        self.assertNotIn("localPosition.x - previousMotion.x", apply_state)

        sprite_start = source.index("if (llamaSpriteReady) {")
        sprite_end = source.index("\n            if (player.invulnerable) {", sprite_start)
        sprite_block = source[sprite_start:sprite_end]
        self.assertLess(
            sprite_block.index("context.save();"),
            sprite_block.index("context.translate(x, y);"),
        )
        self.assertLess(
            sprite_block.index("context.translate(x, y);"),
            sprite_block.index("context.scale(authoritativeFacing.get(id) || 1, 1);"),
        )
        self.assertLess(
            sprite_block.index("context.scale(authoritativeFacing.get(id) || 1, 1);"),
            sprite_block.index("context.drawImage("),
        )
        self.assertLess(
            sprite_block.index("context.drawImage("),
            sprite_block.index("context.restore();"),
        )
        self.assertNotIn("context.fillText", sprite_block)
        self.assertIn("snapshotX < previousX ? -1 : 1", apply_state)
        self.assertIn("authoritativeFacing.get(id) || 1", source)
        self.assertNotIn("previousMotion.facing", source)
        self.assertNotRegex(source, r"send\(\{[^}]*\bfacing\s*:")
        self.assertNotRegex(source, r"\{\s*facing\s*:")

        self.assertIn(
            'context.translate(localPosition.x, localPosition.y);',
            source,
        )
        self.assertIn(
            'context.translate(localPosition.x - (level9World.camera_progress || 0), localPosition.y);',
            source,
        )
        self.assertGreaterEqual(
            source.count(
                'context.scale(authoritativeFacing.get(localPlayerId) || 1, 1);'
            ),
            2,
        )

    def test_level_thresholds_and_configuration(self) -> None:
        self.assertEqual(level_for_score(0), LEVELS[0])
        self.assertEqual(level_for_score(100), LEVELS[0])
        self.assertEqual(level_for_score(101), LEVELS[1])
        self.assertEqual(level_for_score(250), LEVELS[1])
        self.assertEqual(level_for_score(251), LEVELS[2])
        self.assertEqual(level_for_score(500), LEVELS[2])
        self.assertEqual(level_for_score(501), LEVELS[3])
        self.assertEqual(level_for_score(650), LEVELS[3])
        self.assertEqual(level_for_score(651), LEVELS[4])
        self.assertEqual(level_for_score(800), LEVELS[4])
        self.assertEqual(level_for_score(1200), LEVELS[5])
        self.assertEqual(level_for_score(1600), LEVELS[6])
        self.assertEqual(level_for_score(1601), LEVELS[7])
        self.assertEqual(level_for_score(2000), LEVELS[7])
        self.assertEqual(level_for_score(2001), LEVELS[8])
        self.assertEqual(level_for_score(2400), LEVELS[8])
        self.assertEqual(level_for_score(2401), LEVELS[9])
        self.assertEqual(level_for_score(VICTORY_SCORE), LEVELS[9])
        self.assertEqual(LEVELS[7].max_score, 2000)
        self.assertEqual(LEVELS[8].min_score, 2001)
        self.assertEqual(VICTORY_SCORE, 2800)
        self.assertEqual([level.speed for level in LEVELS], [0.44, 0.50, 0.70, 0.76, 0.82, 0.88, 0.94, 0.72, 1.0, 1.0])
        self.assertEqual(LEVELS[0].name, "The Andean Pastures")
        self.assertEqual(STARTING_LIVES, 3)
        self.assertEqual(MAX_PURCHASED_LIVES, 5)
        self.assertEqual(BUMPS_PER_LIFE, 1)

    def test_public_rules_match_three_life_baseline_and_five_life_maximum(self) -> None:
        root = GAME_DIR.parents[1]
        public_copy = {
            "README": (GAME_DIR / "README.md").read_text(),
            "whitepaper": (
                root / "artifacts/llama-website/public/whitepaper.md"
            ).read_text(),
            "landing": (
                root / "artifacts/llama-website/src/pages/landing.tsx"
            ).read_text(),
            "technical evidence": (
                root / "docs/TRUSTLLAMA_TECHNICAL_EVIDENCE.md"
            ).read_text(),
        }
        stale_claims = (
            "starts with five lives",
            "start with five lives",
            "normal five lives",
        )
        for label, text in public_copy.items():
            normalized = text.lower()
            with self.subTest(document=label):
                self.assertTrue(
                    "three lives" in normalized or "three-life" in normalized
                )
                self.assertTrue(
                    "five" in normalized
                    and ("maximum" in normalized or "five-life" in normalized)
                )
                self.assertFalse(any(claim in normalized for claim in stale_claims))

    def test_andean_pumas_cross_three_separate_lanes(self) -> None:
        room = GameRoom(object())
        room.connections[FakeWebSocket()] = Player(
            "pasture-player", "Explorer", current_level=1
        )
        pumas = room.andean_pumas(room.hazard_started_at)
        self.assertEqual(len(pumas), 3)
        self.assertTrue(all(puma["active"] for puma in pumas))
        self.assertTrue(all(puma["kind"] == "puma" for puma in pumas))
        self.assertTrue(all(puma["radius"] == PUMA_RADIUS for puma in pumas))
        self.assertEqual({puma["direction"] for puma in pumas}, {-1, 1})
        self.assertGreaterEqual(PUMA_RADIUS, 0.055)

    def test_level_specific_boundaries_are_enforced(self) -> None:
        ridge = LEVELS[1]
        self.assertEqual(
            _clamp_coordinate(-5, 0.5, ridge.board_min, ridge.board_max),
            ridge.board_min,
        )
        self.assertEqual(
            _clamp_coordinate(5, 0.5, ridge.board_min, ridge.board_max),
            ridge.board_max,
        )

    def test_players_must_choose_a_real_nickname(self) -> None:
        self.assertIsNone(_safe_name(""))
        self.assertIsNone(_safe_name("x"))
        self.assertEqual(_safe_name("  Moon   Rider  "), "Moon Rider")

    def test_level_three_orbs_relocate_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            original = dict(room.collectibles)
            room.relocate_level_three_orbs()
            self.assertNotEqual(room.collectibles, original)
            self.assertEqual(set(room.collectibles), set(original))
            for orb in room.collectibles.values():
                self.assertGreaterEqual(orb["x"], 0.12)
                self.assertLessEqual(orb["x"], 0.87)
                self.assertGreaterEqual(orb["y"], 0.12)
                self.assertLessEqual(orb["y"], 0.87)

    def test_farm_tractors_move_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            room.connections[FakeWebSocket()] = Player("player", "Tester", current_level=3)
            tractors = room.farm_tractors(room.hazard_started_at)
            self.assertEqual(len(tractors), 2)
            self.assertTrue(all(t["active"] for t in tractors))
            self.assertEqual(tractors[0]["radius"], TRACTOR_RADIUS)

    def test_hazard_collision_detects_full_movement_segment(self) -> None:
        hazard = {
            "active": True,
            "x": 0.5,
            "y": 0.3,
            "radius": 0.1,
        }
        self.assertTrue(
            GameRoom.movement_intersects_hazard(0.2, 0.3, 0.8, 0.3, hazard)
        )
        self.assertFalse(
            GameRoom.movement_intersects_hazard(0.2, 0.3, 0.35, 0.3, hazard)
        )

    def test_castle_ghosts_are_authoritative_and_detect_crossing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            room.connections[FakeWebSocket()] = Player("player", "Tester", current_level=4)
            ghost = room.castle_ghosts(room.hazard_started_at)[0]
            self.assertTrue(ghost["active"])
            self.assertEqual(ghost["radius"], GHOST_COLLISION_RADIUS)
            self.assertTrue(
                GameRoom.movement_intersects_hazard(
                    ghost["x"] - 0.1,
                    ghost["y"],
                    ghost["x"] + 0.1,
                    ghost["y"],
                    ghost,
                )
            )

    def test_labyrinth_walls_and_wardens_are_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            room.connections[FakeWebSocket()] = Player("player", "Tester", current_level=6)
            hazards = room.labyrinth_hazards(room.hazard_started_at)
            walls = [hazard for hazard in hazards if hazard["kind"] == "maze_wall"]
            wardens = [hazard for hazard in hazards if hazard["kind"] == "maze_warden"]
            self.assertGreater(len(walls), 50)
            self.assertEqual(len(wardens), 4)
            self.assertTrue(all(hazard["active"] for hazard in hazards))
            self.assertTrue(all(wall["radius"] == MAZE_WALL_RADIUS for wall in walls))
            self.assertTrue(all(warden["radius"] == MAZE_WARDEN_RADIUS for warden in wardens))

    def test_level_five_wardens_patrol_instead_of_falling_back_to_pen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            player = Player(
                "level-five-player",
                "Maze Runner",
                current_level=5,
                x=0.22,
                y=0.24,
                movement_direction="right",
            )
            room.connections[FakeWebSocket()] = player
            starts = {
                warden_id: (warden["x"], warden["y"])
                for warden_id, warden in room.wardens.items()
            }
            visited = {warden_id: {position} for warden_id, position in starts.items()}

            for tick in range(6):
                room.advance_wardens(float(tick))
                for warden_id, warden in room.wardens.items():
                    visited[warden_id].add((warden["x"], warden["y"]))

            self.assertTrue(
                all(warden.get("target_rule") != "pen" for warden in room.wardens.values())
            )
            self.assertTrue(
                all(
                    (warden["x"], warden["y"]) != starts[warden_id]
                    for warden_id, warden in room.wardens.items()
                )
            )
            self.assertTrue(all(len(positions) >= 3 for positions in visited.values()))


class ArcadeResponsiveLayoutTests(unittest.TestCase):
    VIEWPORTS = {
        "phone portrait": {"width": 390, "height": 844},
        "phone landscape": {"width": 844, "height": 390},
        "tablet portrait": {"width": 768, "height": 1024},
        "tablet landscape": {"width": 1024, "height": 768},
    }
    SAFE_AREA_VIEWPORTS = {
        "notched phone portrait": {
            "viewport": {"width": 390, "height": 844},
            "insets": {"top": 47, "right": 0, "bottom": 34, "left": 0},
        },
        "notched phone landscape": {
            "viewport": {"width": 844, "height": 390},
            "insets": {"top": 0, "right": 44, "bottom": 21, "left": 44},
        },
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/repl/tools/bin/chromium"),
            headless=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def open_game(self, viewport, safe_area=None):
        context = self.browser.new_context(viewport=viewport, has_touch=True)
        page = context.new_page()
        page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
        if safe_area:
            page.locator(":root").evaluate(
                """(root, insets) => {
                    for (const [edge, value] of Object.entries(insets)) {
                        root.style.setProperty(`--safe-area-inset-${edge}`, `${value}px`);
                    }
                }""",
                safe_area,
            )
        return context, page

    def test_mid_game_levels_continue_during_analytics_outages(self) -> None:
        levels = [
            {
                "number": level.number,
                "name": level.name,
                "min_score": level.min_score,
                "next_score": level.max_score + 1 if level.max_score is not None else None,
            }
            for level in LEVELS
        ]
        scores = {"3": 300, "4": 550, "5": 700, "6": 900, "7": 1300, "8": 1700, "9": 2100}
        events = [
            {"type": "event", "event": "hazard_stun", "player_id": "mid-game-player", "duration_ms": 1},
            {"type": "event", "event": "ghost_stun", "player_id": "mid-game-player", "duration_ms": 1},
            {"type": "event", "event": "block_broken", "player_id": "mid-game-player"},
            {"type": "event", "event": "block_impact", "player_id": "mid-game-player"},
            {"type": "event", "event": "question_block_hit", "player_id": "mid-game-player"},
            {"type": "event", "event": "orb_collected", "player_id": "mid-game-player", "x": 0.4, "y": 0.4},
            {"type": "event", "event": "level_up", "player_id": "mid-game-player", "current_level": 9},
        ]

        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), analytics_outage_page(
                self.browser,
                failure_mode,
                viewport={"width": 1280, "height": 900},
            ) as outage:
                outage.page.goto(
                    (GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded"
                )
                outage.page.evaluate(
                """async ([levels, scores, events]) => {
                    window.__socket.message({
                        type: "welcome",
                        player_id: "mid-game-player",
                        simulated_entry_fee_tlama: 25,
                    });
                    for (let level = 3; level <= 9; level += 1) {
                        const state = {
                            type: "state",
                            levels,
                            players: [{
                                player_id: "mid-game-player",
                                name: "Mid-game player",
                                score: scores[level],
                                lives: 4,
                                orbs_collected: 17,
                                current_level: level,
                                x: 0.5,
                                y: 0.5,
                                oxygen: level === 8 ? 42 : 100,
                                stunned: false,
                            }],
                            collectibles: {},
                            leaderboard: [],
                            victory_score: 2800,
                        };
                        if (level === 7) state.level7_worlds = {"mid-game-player": {camera_progress: 0.4}};
                        if (level === 8) state.level8_worlds = {"mid-game-player": {camera_progress: 0.5}};
                        if (level === 9) state.level9_worlds = {"mid-game-player": {camera_progress: 0.6}};
                        window.__socket.message(state);
                        if (level === 8) {
                            window.__level8Oxygen = document.querySelector("#oxygen").textContent;
                        }
                        window.__socket.message(events[level - 3]);
                    }
                    await new Promise((resolve) => setTimeout(resolve, 0));
                }""",
                [levels, scores, events],
            )

                page = outage.page
                self.assertEqual(page.locator("#currentLevel").text_content(), "9")
                self.assertEqual(page.locator("#levelName").text_content(), "Llama Sky Kingdom")
                self.assertEqual(page.evaluate("window.__level8Oxygen"), "42%")
                self.assertEqual(page.locator("#orbs").text_content(), "17")
                self.assertIn(
                    "Victory: 2401 points", page.locator("#progressLabel").text_content()
                )
                self.assertTrue(page.locator("#labyrinthBanner").evaluate(
                    "(element) => element.classList.contains('visible')"
                ))
                self.assertIn("arcade_joined", page.evaluate("window.__analyticsAttempts"))
                self.assertEqual(outage.errors, [])

    def assert_inside_safe_area(self, locator, label, viewport, insets, fixture_name):
        bounds = locator.bounding_box()
        self.assertIsNotNone(bounds, f"{label} is not rendered at {fixture_name}")
        self.assertGreaterEqual(
            bounds["x"], insets["left"] - 0.5,
            f"{label} enters the left safe-area inset at {fixture_name}",
        )
        self.assertLessEqual(
            bounds["x"] + bounds["width"], viewport["width"] - insets["right"] + 0.5,
            f"{label} enters the right safe-area inset at {fixture_name}",
        )
        self.assertGreaterEqual(
            bounds["y"], insets["top"] - 0.5,
            f"{label} enters the top safe-area inset at {fixture_name}",
        )
        self.assertLessEqual(
            bounds["y"] + bounds["height"], viewport["height"] - insets["bottom"] + 0.5,
            f"{label} enters the bottom safe-area inset at {fixture_name}",
        )

    def test_controls_avoid_notches_and_home_indicators(self) -> None:
        selectors = {
            "wallet button": "#walletButton",
            "game canvas": "#gameCanvas",
            "joystick": "#joystick",
            "movement hint": ".canvas-help",
            "progress display": ".level-progress",
        }
        for name, fixture in self.SAFE_AREA_VIEWPORTS.items():
            with self.subTest(viewport=name):
                viewport = fixture["viewport"]
                insets = fixture["insets"]
                context, page = self.open_game(viewport, safe_area=insets)
                try:
                    for label, selector in selectors.items():
                        locator = page.locator(selector)
                        locator.evaluate(
                            "(element) => element.scrollIntoView({block: 'center', inline: 'center'})"
                        )
                        self.assert_inside_safe_area(locator, label, viewport, insets, name)
                finally:
                    context.close()

    def test_wallet_and_purchase_dialogs_avoid_safe_areas_and_remain_usable(self) -> None:
        for name, fixture in self.SAFE_AREA_VIEWPORTS.items():
            with self.subTest(viewport=name):
                viewport = fixture["viewport"]
                insets = fixture["insets"]
                context, page = self.open_game(viewport, safe_area=insets)
                try:
                    page.locator("#walletButton").click()
                    wallet_dialog = page.locator("#walletDialog")
                    self.assertTrue(wallet_dialog.evaluate("(dialog) => dialog.open"))
                    for label, selector in {
                        "wallet dialog content": "#walletDialog .dialog-card",
                        "wallet close control": "#walletDialog .dialog-close",
                        "wallet primary action": "#walletDialog [data-dialog-wallet='phantom']",
                    }.items():
                        self.assert_inside_safe_area(
                            page.locator(selector), label, viewport, insets, name
                        )
                    page.keyboard.press("Escape")
                    self.assertFalse(wallet_dialog.evaluate("(dialog) => dialog.open"))

                    page.evaluate(
                        """() => {
                            const dialog = document.querySelector("#purchaseDialog");
                            document.querySelector("#purchaseDialogCopy").textContent =
                                "Purchase Arcade Ticket for 25 TLAMA?";
                            dialog.showModal();
                        }"""
                    )
                    purchase_dialog = page.locator("#purchaseDialog")
                    for label, selector in {
                        "purchase dialog content": "#purchaseDialog .dialog-card",
                        "purchase close control": "#purchaseDialog .dialog-close",
                        "purchase primary action": "#confirmPurchaseButton",
                    }.items():
                        self.assert_inside_safe_area(
                            page.locator(selector), label, viewport, insets, name
                        )
                    confirm = page.locator("#confirmPurchaseButton")
                    self.assertEqual(confirm.evaluate("(button) => getComputedStyle(button).minHeight"), "44px")
                    confirm.focus()
                    self.assertTrue(confirm.evaluate("(button) => button === document.activeElement"))
                    page.locator("#purchaseDialog .dialog-close").tap()
                    self.assertFalse(purchase_dialog.evaluate("(dialog) => dialog.open"))
                finally:
                    context.close()

    def test_wallet_dialog_recovers_from_missing_and_rejecting_providers(self) -> None:
        context, page = self.open_game(self.VIEWPORTS["phone portrait"])
        try:
            wallet_dialog = page.locator("#walletDialog")
            phantom_button = page.locator(
                "#walletDialog [data-dialog-wallet='phantom']"
            )
            dialog_status = page.locator("#walletDialogStatus")

            page.evaluate("delete window.TLAMA_WEB3")
            page.locator("#walletButton").tap()
            phantom_button.tap()

            self.assertTrue(wallet_dialog.evaluate("(dialog) => dialog.open"))
            self.assertIn("still loading", dialog_status.text_content())
            self.assertIn("Try again", dialog_status.text_content())
            self.assertTrue(
                phantom_button.evaluate(
                    "(button) => button === document.activeElement && !button.disabled"
                )
            )

            page.evaluate(
                """() => {
                    window.TLAMA_WEB3 = {
                        connect: async () => {
                            await Promise.resolve();
                            throw new Error("Wallet request was rejected.");
                        }
                    };
                }"""
            )
            page.keyboard.press("Enter")

            self.assertTrue(wallet_dialog.evaluate("(dialog) => dialog.open"))
            self.assertIn("Wallet request was rejected.", dialog_status.text_content())
            self.assertIn("Try again", dialog_status.text_content())
            self.assertTrue(
                phantom_button.evaluate(
                    "(button) => button === document.activeElement && !button.disabled"
                )
            )

            page.evaluate(
                """() => {
                    window.TLAMA_WEB3.connect = async () => ({
                        publicKey: { toString: () => "RetryWallet111111111111111111111111111111" }
                    });
                }"""
            )
            page.keyboard.press("Enter")

            self.assertFalse(wallet_dialog.evaluate("(dialog) => dialog.open"))
            self.assertEqual(page.locator("#walletButton").text_content(), "Wallet connected")
            self.assertIn("connected", page.locator("#walletStatus").text_content())
        finally:
            context.close()

    def test_mobile_and_tablet_controls_do_not_clip_horizontally(self) -> None:
        selectors = {
            "joystick": "#joystick",
            "canvas": "#gameCanvas",
            "level display": "#currentLevel",
            "progress bar": ".progress-track",
            "wallet button": "#walletButton",
        }
        for name, viewport in self.VIEWPORTS.items():
            with self.subTest(viewport=name):
                context, page = self.open_game(viewport)
                try:
                    overflow = page.evaluate(
                        "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
                    )
                    self.assertFalse(overflow, f"{name} has horizontal document overflow")
                    for label, selector in selectors.items():
                        bounds = page.locator(selector).bounding_box()
                        self.assertIsNotNone(bounds, f"{label} is not rendered at {name}")
                        self.assertGreaterEqual(bounds["x"], 0, f"{label} clips left at {name}")
                        self.assertLessEqual(
                            bounds["x"] + bounds["width"],
                            viewport["width"] + 0.5,
                            f"{label} clips right at {name}",
                        )
                finally:
                    context.close()

    def test_phone_joystick_does_not_cover_the_game_canvas(self) -> None:
        context, page = self.open_game(self.VIEWPORTS["phone portrait"])
        try:
            canvas = page.locator("#gameCanvas").bounding_box()
            joystick = page.locator("#joystick").bounding_box()
            self.assertIsNotNone(canvas)
            self.assertIsNotNone(joystick)
            self.assertGreaterEqual(
                joystick["y"],
                canvas["y"] + canvas["height"],
                "The phone joystick must sit below the game canvas.",
            )
        finally:
            context.close()

    def test_touch_release_and_cancellation_center_joystick_knob(self) -> None:
        context, page = self.open_game(self.VIEWPORTS["phone portrait"])
        try:
            joystick = page.locator("#joystick")
            box = joystick.bounding_box()
            self.assertIsNotNone(box)
            center = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}

            for terminal_event in ("touchend", "touchcancel"):
                with self.subTest(event=terminal_event):
                    page.evaluate(
                        """({ center, terminalEvent }) => {
                            const joystick = document.querySelector("#joystick");
                            const touch = new Touch({
                                identifier: 7,
                                target: joystick,
                                clientX: center.x + 30,
                                clientY: center.y + 20,
                            });
                            joystick.dispatchEvent(new TouchEvent("touchstart", {
                                changedTouches: [touch],
                                touches: [touch],
                                bubbles: true,
                                cancelable: true,
                            }));
                            joystick.dispatchEvent(new TouchEvent(terminalEvent, {
                                changedTouches: [touch],
                                touches: [],
                                bubbles: true,
                                cancelable: true,
                            }));
                        }""",
                        {"center": center, "terminalEvent": terminal_event},
                    )
                    transform = page.locator("#joystickKnob").evaluate(
                        "(element) => element.style.transform"
                    )
                    self.assertEqual(transform, "translate3d(0px, 0px, 0px)")
        finally:
            context.close()

    def test_ios_retina_touch_and_pointer_coordinates_use_css_pixels(self) -> None:
        context = self.browser.new_context(
            viewport=self.VIEWPORTS["phone portrait"],
            has_touch=True,
            is_mobile=True,
            device_scale_factor=3,
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            joystick = page.locator("#joystick")
            box = joystick.bounding_box()
            self.assertIsNotNone(box)
            center_x = box["x"] + box["width"] / 2
            center_y = box["y"] + box["height"] / 2
            page.evaluate(
                """({ x, y }) => {
                    const joystick = document.querySelector("#joystick");
                    const touch = new Touch({
                        identifier: 91,
                        target: joystick,
                        clientX: x + 12,
                        clientY: y - 8,
                    });
                    joystick.dispatchEvent(new TouchEvent("touchstart", {
                        changedTouches: [touch],
                        touches: [touch],
                        bubbles: true,
                        cancelable: true,
                    }));
                }""",
                {"x": center_x, "y": center_y},
            )
            transform = page.locator("#joystickKnob").evaluate(
                "(element) => element.style.transform"
            )
            self.assertIn("12px", transform)
            self.assertIn("-8px", transform)
            page.evaluate(
                """() => {
                    const joystick = document.querySelector("#joystick");
                    const touch = new Touch({
                        identifier: 91,
                        target: joystick,
                        clientX: 0,
                        clientY: 0,
                    });
                    joystick.dispatchEvent(new TouchEvent("touchend", {
                        changedTouches: [touch],
                        touches: [],
                        bubbles: true,
                        cancelable: true,
                    }));
                }"""
            )
            joystick.dispatch_event(
                "pointerdown",
                {
                    "pointerId": 92,
                    "pointerType": "touch",
                    "isPrimary": True,
                    "clientX": center_x + 10,
                    "clientY": center_y,
                },
            )
            self.assertIn(
                "10px",
                page.locator("#joystickKnob").evaluate(
                    "(element) => element.style.transform"
                ),
            )
            joystick.dispatch_event(
                "pointerup",
                {
                    "pointerId": 92,
                    "pointerType": "touch",
                    "isPrimary": True,
                    "clientX": center_x + 10,
                    "clientY": center_y,
                },
            )
            self.assertEqual(
                page.locator("#joystickKnob").evaluate(
                    "(element) => element.style.transform"
                ),
                "translate3d(0px, 0px, 0px)",
            )
            canvas_sizes = page.locator("#gameCanvas").evaluate(
                """(canvas) => ({
                    cssWidth: canvas.getBoundingClientRect().width,
                    backingWidth: canvas.width,
                })"""
            )
            self.assertAlmostEqual(
                canvas_sizes["backingWidth"],
                canvas_sizes["cssWidth"] * 2,
                delta=2,
            )
        finally:
            context.close()

    def test_vibration_control_is_hidden_when_unsupported(self) -> None:
        context = self.browser.new_context()
        context.add_init_script(
            """
                Object.defineProperty(navigator, "vibrate", {
                    configurable: true,
                    value: undefined
                });
            """
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            self.assertTrue(page.locator("#vibrationButton").is_hidden())
        finally:
            context.close()

    def test_unavailable_storage_does_not_abort_vibration_or_game_startup(self) -> None:
        context = self.browser.new_context()
        context.add_init_script(
            """
                Storage.prototype.getItem = () => { throw new DOMException("denied", "SecurityError"); };
                Storage.prototype.setItem = () => { throw new DOMException("denied", "SecurityError"); };
                Object.defineProperty(navigator, "vibrate", {
                    configurable: true,
                    value: () => true
                });
            """
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            vibration_button = page.locator("#vibrationButton")
            self.assertTrue(vibration_button.is_visible())
            self.assertEqual(vibration_button.text_content(), "Vibration on")
            vibration_button.click()
            self.assertEqual(vibration_button.text_content(), "Vibration off")
            self.assertEqual(vibration_button.get_attribute("aria-pressed"), "false")
        finally:
            context.close()

    def test_level_ten_touch_action_charges_releases_and_cancels(self) -> None:
        context = self.browser.new_context(
            viewport=self.VIEWPORTS["phone portrait"], has_touch=True
        )
        context.add_init_script(
            """
                class TestWebSocket extends EventTarget {
                    static OPEN = 1;
                    static CONNECTING = 0;
                    constructor() {
                        super();
                        this.readyState = TestWebSocket.OPEN;
                        this.sent = [];
                        window.__testSocket = this;
                        queueMicrotask(() => this.dispatchEvent(new Event("open")));
                    }
                    send(payload) { this.sent.push(JSON.parse(payload)); }
                    emit(message) {
                        this.dispatchEvent(new MessageEvent("message", {
                            data: JSON.stringify(message)
                        }));
                    }
                }
                window.WebSocket = TestWebSocket;
                window.__vibrations = [];
                Object.defineProperty(navigator, "vibrate", {
                    configurable: true,
                    value: (pattern) => { window.__vibrations.push(pattern); return true; }
                });
            """
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("window.__testSocket")
            vibration_button = page.locator("#vibrationButton")
            self.assertTrue(vibration_button.is_visible())
            self.assertEqual(vibration_button.text_content(), "Vibration on")
            self.assertEqual(vibration_button.get_attribute("aria-pressed"), "true")
            vibration_button.click()
            self.assertEqual(vibration_button.text_content(), "Vibration off")
            self.assertEqual(vibration_button.get_attribute("aria-pressed"), "false")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function("window.__testSocket")
            vibration_button = page.locator("#vibrationButton")
            self.assertEqual(vibration_button.text_content(), "Vibration off")
            self.assertEqual(vibration_button.get_attribute("aria-pressed"), "false")
            page.evaluate(
                """() => {
                    window.__testSocket.emit({type: "welcome", player_id: "mobile-pilot"});
                    window.__testSocket.emit({
                        players: [{
                            player_id: "mobile-pilot", x: .2, y: .5, score: 2410,
                            lives: 3, orbs_collected: 0, super_spit_charge: 0,
                            current_level: 10, game_over: false
                        }],
                        collectibles: {},
                        level10_worlds: {
                            "mobile-pilot": {
                                boss: {hp: 300, max_hp: 300, phase: 1},
                                camera: {}, bounds: {}, drones: [], orbs: {},
                                hazards: [], projectiles: []
                            }
                        }
                    });
                }"""
            )
            button = page.locator("#spitButton")
            self.assertTrue(button.is_visible())
            self.assertEqual(button.get_attribute("aria-pressed"), "false")

            button.dispatch_event("pointerdown", {"pointerId": 17})
            self.assertEqual(button.get_attribute("aria-pressed"), "true")
            page.evaluate(
                """() => window.__testSocket.emit({
                    players: [{
                        player_id: "mobile-pilot", x: .2, y: .5, score: 2410,
                        lives: 3, orbs_collected: 0, super_spit_charge: 100,
                        current_level: 10, game_over: false
                    }],
                    collectibles: {}
                })"""
            )
            self.assertTrue(button.evaluate("(element) => element.classList.contains('super-ready')"))
            self.assertEqual(button.text_content(), "SUPER SPIT READY — release!")
            self.assertIn("fully charged", button.get_attribute("aria-label"))
            self.assertEqual(page.evaluate("window.__vibrations"), [])
            vibration_button.click()
            self.assertEqual(
                page.evaluate(
                    "window.localStorage.getItem('tlama-arcade-vibration-enabled')"
                ),
                "true",
            )
            button.dispatch_event("pointerup", {"pointerId": 17})
            button.dispatch_event("pointerdown", {"pointerId": 18})
            page.evaluate(
                """() => window.__testSocket.emit({
                    players: [{
                        player_id: "mobile-pilot", x: .2, y: .5, score: 2410,
                        lives: 3, orbs_collected: 0, super_spit_charge: 100,
                        current_level: 10, game_over: false
                    }],
                    collectibles: {}
                })"""
            )
            self.assertEqual(page.evaluate("window.__vibrations"), [[45, 35, 90]])
            page.evaluate(
                """() => window.__testSocket.emit({
                    players: [{
                        player_id: "mobile-pilot", x: .2, y: .5, score: 2410,
                        lives: 3, orbs_collected: 0, super_spit_charge: 100,
                        current_level: 10, game_over: false
                    }],
                    collectibles: {}
                })"""
            )
            self.assertEqual(page.evaluate("window.__vibrations.length"), 1)
            button.dispatch_event("pointerup", {"pointerId": 18})
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertFalse(button.evaluate("(element) => element.classList.contains('super-ready')"))

            button.dispatch_event("pointerdown", {"pointerId": 19})
            button.dispatch_event("pointercancel", {"pointerId": 19})
            self.assertEqual(button.get_attribute("aria-pressed"), "false")
            self.assertFalse(button.evaluate("(element) => element.classList.contains('super-ready')"))
            messages = page.evaluate(
                """() => window.__testSocket.sent.filter(
                    (message) => message.type === "citadel_input"
                )"""
            )
            self.assertTrue(
                any(message["charge_pressed"] is True for message in messages)
            )
            self.assertTrue(
                any(message["fire_released"] is True for message in messages)
            )
            self.assertEqual(messages[-1]["charge_pressed"], False)
            self.assertEqual(messages[-1]["fire_released"], False)
            sequences = [message["sequence"] for message in messages]
            self.assertEqual(sequences, sorted(set(sequences)))
        finally:
            context.close()


@unittest.skipUnless(
    os.environ.get("ARCADE_WEBKIT_VALIDATION") == "1",
    "Run in the WebKit-capable release validation environment.",
)
class ArcadeWebKitMobileControlTests(unittest.TestCase):
    """Focused Safari-engine regression check; physical iPhone passes remain required."""

    NOTCHED_IPHONE_PORTRAIT = {
        "viewport": {"width": 390, "height": 844},
        "insets": {"top": 47, "right": 0, "bottom": 34, "left": 0},
    }
    NOTCHED_IPHONE_LANDSCAPE = {
        "viewport": {"width": 844, "height": 390},
        "insets": {"top": 0, "right": 44, "bottom": 21, "left": 44},
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        engine = os.environ.get("ARCADE_BROWSER_ENGINE", "webkit")
        browser_type = getattr(cls.playwright, engine)
        launch_options = {"headless": True}
        if engine == "chromium":
            launch_options["executable_path"] = os.environ.get(
                "CHROMIUM_PATH", "/repl/tools/bin/chromium"
            )
        cls.browser = browser_type.launch(**launch_options)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def set_safe_area_insets(self, page, insets) -> None:
        page.locator(":root").evaluate(
            """(root, safeAreaInsets) => {
                for (const [edge, value] of Object.entries(safeAreaInsets)) {
                    root.style.setProperty(`--safe-area-inset-${edge}`, `${value}px`);
                }
            }""",
            insets,
        )

    def assert_rotation_layout(self, page, fixture, orientation) -> None:
        viewport = fixture["viewport"]
        insets = fixture["insets"]
        selectors = {
            "joystick": "#joystick",
            "Jump action": "#spitButton",
            "game canvas": "#gameCanvas",
            "progress controls": ".level-progress",
        }
        self.assertEqual(
            page.evaluate("() => ({width: innerWidth, height: innerHeight})"),
            viewport,
            f"visual viewport dimensions stayed stale after rotating to {orientation}",
        )
        self.assertLessEqual(
            page.evaluate("() => document.documentElement.scrollWidth"),
            viewport["width"],
            f"page has horizontal overflow after rotating to {orientation}",
        )
        for label, selector in selectors.items():
            with self.subTest(orientation=orientation, selector=selector):
                locator = page.locator(selector)
                locator.evaluate(
                    "(element) => element.scrollIntoView({block: 'center', inline: 'center'})"
                )
                bounds = locator.bounding_box()
                self.assertIsNotNone(
                    bounds,
                    f"{label} ({selector}) is not rendered after rotating to {orientation}",
                )
                self.assertTrue(
                    locator.is_visible(),
                    f"{label} ({selector}) is not visible after rotating to {orientation}",
                )
                self.assertGreater(
                    bounds["width"] * bounds["height"],
                    0,
                    f"{label} ({selector}) has no usable area after rotating to {orientation}",
                )
                self.assertGreaterEqual(
                    bounds["x"],
                    insets["left"] - 0.5,
                    f"{label} ({selector}) enters the left safe area in {orientation}",
                )
                self.assertLessEqual(
                    bounds["x"] + bounds["width"],
                    viewport["width"] - insets["right"] + 0.5,
                    f"{label} ({selector}) enters the right safe area in {orientation}",
                )
                self.assertGreaterEqual(
                    bounds["y"],
                    insets["top"] - 0.5,
                    f"{label} ({selector}) enters the top safe area in {orientation}",
                )
                self.assertLessEqual(
                    bounds["y"] + bounds["height"],
                    viewport["height"] - insets["bottom"] + 0.5,
                    f"{label} ({selector}) enters the bottom safe area in {orientation}",
                )
                if selector in ("#joystick", "#spitButton"):
                    self.assertTrue(
                        locator.is_enabled(),
                        f"{label} ({selector}) is disabled after rotating to {orientation}",
                    )
                    self.assertTrue(
                        locator.evaluate(
                            """(element) => {
                                const bounds = element.getBoundingClientRect();
                                const hit = document.elementFromPoint(
                                    bounds.left + bounds.width / 2,
                                    bounds.top + bounds.height / 2
                                );
                                return hit === element || element.contains(hit);
                            }"""
                        ),
                        f"{label} ({selector}) cannot receive input after rotating to {orientation}",
                    )

    def test_active_match_controls_follow_safe_areas_across_live_rotation(self) -> None:
        portrait = self.NOTCHED_IPHONE_PORTRAIT
        landscape = self.NOTCHED_IPHONE_LANDSCAPE
        context = self.browser.new_context(
            viewport=portrait["viewport"],
            has_touch=True,
            is_mobile=True,
            device_scale_factor=3,
        )
        context.add_init_script(
            """
            class TestWebSocket extends EventTarget {
                static OPEN = 1;
                static CONNECTING = 0;
                constructor() {
                    super();
                    this.readyState = TestWebSocket.OPEN;
                    this.sent = [];
                    window.__testSocket = this;
                    queueMicrotask(() => this.dispatchEvent(new Event("open")));
                }
                send(payload) { this.sent.push(JSON.parse(payload)); }
            }
            window.WebSocket = TestWebSocket;
            """
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("window.__testSocket")
            page.evaluate(
                """() => {
                    const emit = (message) => window.__testSocket.dispatchEvent(
                        new MessageEvent("message", {data: JSON.stringify(message)})
                    );
                    emit({type: "welcome", player_id: "rotation-player"});
                    emit({
                        type: "state",
                        players: [{
                            player_id: "rotation-player", name: "Rotation player",
                            x: .2, y: .5, score: 2100, lives: 3,
                            orbs_collected: 5, current_level: 9, game_over: false
                        }],
                        collectibles: {},
                        leaderboard: [],
                        level9_worlds: {
                            "rotation-player": {
                                camera_progress: 0, orbs: {}, platforms: [],
                                cloud_platforms: [], pipes: [], question_blocks: [],
                                secret_bundles: [], wardens: [], projectiles: []
                            }
                        }
                    });
                }"""
            )
            self.assertEqual(page.locator("#currentLevel").text_content(), "9")
            self.assertEqual(page.locator("#spitButton").text_content(), "Jump")

            for orientation, fixture in (
                ("portrait", portrait),
                ("notched landscape", landscape),
                ("portrait after rotating back", portrait),
            ):
                page.set_viewport_size(fixture["viewport"])
                self.set_safe_area_insets(page, fixture["insets"])
                page.evaluate(
                    """() => {
                        window.dispatchEvent(new Event("orientationchange"));
                        window.dispatchEvent(new Event("resize"));
                    }"""
                )
                page.wait_for_timeout(100)
                self.assert_rotation_layout(page, fixture, orientation)
        finally:
            context.close()

    def test_notched_iphone_landscape_controls_stay_inside_safe_areas(self) -> None:
        fixture = self.NOTCHED_IPHONE_LANDSCAPE
        viewport = fixture["viewport"]
        insets = fixture["insets"]
        context = self.browser.new_context(
            viewport=viewport,
            has_touch=True,
            is_mobile=True,
            device_scale_factor=3,
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            self.set_safe_area_insets(page, insets)
            page.locator("#spitButton").evaluate(
                """(button) => {
                    button.hidden = false;
                    button.textContent = "Jump";
                }"""
            )

            selectors = {
                "joystick": "#joystick",
                "Jump action": "#spitButton",
                "game canvas": "#gameCanvas",
                "progress controls": ".level-progress",
            }
            for label, selector in selectors.items():
                with self.subTest(selector=selector):
                    locator = page.locator(selector)
                    locator.scroll_into_view_if_needed()
                    bounds = locator.bounding_box()
                    self.assertIsNotNone(
                        bounds,
                        f"{label} ({selector}) is not rendered in notched iPhone landscape",
                    )
                    self.assertGreaterEqual(
                        bounds["x"],
                        insets["left"] - 0.5,
                        f"{label} ({selector}) enters the left safe area",
                    )
                    self.assertLessEqual(
                        bounds["x"] + bounds["width"],
                        viewport["width"] - insets["right"] + 0.5,
                        f"{label} ({selector}) enters the right safe area",
                    )
                    self.assertGreaterEqual(
                        bounds["y"],
                        insets["top"] - 0.5,
                        f"{label} ({selector}) enters the top safe area",
                    )
                    self.assertLessEqual(
                        bounds["y"] + bounds["height"],
                        viewport["height"] - insets["bottom"] + 0.5,
                        f"{label} ({selector}) enters the bottom safe area",
                    )
        finally:
            context.close()

    def test_iphone_controls_neutralize_server_input_when_page_hides(self) -> None:
        context = self.browser.new_context(
            viewport={"width": 390, "height": 844},
            has_touch=True,
            is_mobile=True,
            device_scale_factor=3,
        )
        context.add_init_script(
            """
            class TestWebSocket extends EventTarget {
                static OPEN = 1;
                static CONNECTING = 0;
                constructor() {
                    super();
                    this.readyState = TestWebSocket.OPEN;
                    this.sent = [];
                    window.__testSocket = this;
                    queueMicrotask(() => this.dispatchEvent(new Event("open")));
                }
                send(payload) { this.sent.push(JSON.parse(payload)); }
                emit(message) {
                    this.dispatchEvent(new MessageEvent("message", {
                        data: JSON.stringify(message)
                    }));
                }
            }
            window.WebSocket = TestWebSocket;
            """
        )
        page = context.new_page()
        try:
            page.goto((GAME_DIR / "index.html").as_uri(), wait_until="domcontentloaded")
            page.wait_for_function("window.__testSocket")
            page.evaluate(
                """() => {
                    window.__testSocket.emit({type: "welcome", player_id: "webkit-player"});
                    window.__testSocket.emit({
                        type: "state",
                        players: [{
                            player_id: "webkit-player", name: "WebKit player",
                            x: .2, y: .5, score: 100, lives: 3,
                            orbs_collected: 4, current_level: 1, game_over: false
                        }],
                        collectibles: {},
                        leaderboard: []
                    });
                }"""
            )
            self.assertEqual(page.locator("#orbs").text_content(), "4")
            levels = [
                {
                    "number": level.number,
                    "name": level.name,
                    "min_score": level.min_score,
                    "max_score": level.max_score,
                    "speed": level.speed,
                    "board_min": level.board_min,
                    "board_max": level.board_max,
                }
                for level in LEVELS
            ]
            page.evaluate(
                """(levels) => window.__testSocket.emit({
                    type: "state",
                    levels,
                    players: [{
                        player_id: "webkit-player", name: "WebKit player",
                        x: .2, y: .5, score: 110, lives: 3,
                        orbs_collected: 5, current_level: 1, game_over: false
                    }],
                    collectibles: {},
                    leaderboard: []
                })"""
            )
            self.assertEqual(page.locator("#orbs").text_content(), "5")

            for level_number, key, message_type, active_field in (
                (7, "ArrowUp", "vehicle_input", "thrust"),
                (8, "ArrowDown", "submarine_input", "thrust"),
            ):
                with self.subTest(level=level_number, control="keyboard"):
                    page.evaluate(
                        """({levelNumber}) => {
                            const state = {
                                type: "state",
                                players: [{
                                    player_id: "webkit-player", name: "WebKit player",
                                    x: .2, y: .5, score: levelNumber === 7 ? 1600 : 1850,
                                    lives: 3, orbs_collected: 5,
                                    current_level: levelNumber, game_over: false
                                }],
                                collectibles: {},
                                leaderboard: []
                            };
                            state[`level${levelNumber}_worlds`] = {
                                "webkit-player": {camera_progress: 0}
                            };
                            window.__testSocket.emit(state);
                            window.__testSocket.sent = [];
                        }""",
                        {"levelNumber": level_number},
                    )
                    page.keyboard.down(key)
                    page.wait_for_timeout(120)
                    page.evaluate(
                        "window.dispatchEvent(new PageTransitionEvent('pagehide'))"
                    )
                    protocol_messages = page.evaluate(
                        """(messageType) => window.__testSocket.sent.filter(
                            message => message.type === messageType
                        )""",
                        message_type,
                    )
                    self.assertTrue(
                        any(message.get(active_field) is True for message in protocol_messages)
                    )
                    self.assertFalse(protocol_messages[-1][active_field])
                    page.keyboard.up(key)

            joystick = page.locator("#joystick")
            box = joystick.bounding_box()
            self.assertIsNotNone(box)
            center = {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2}

            for terminal_event in ("touchend", "touchcancel"):
                page.evaluate(
                    """({center, terminalEvent}) => {
                        const joystick = document.querySelector("#joystick");
                        const touch = new Touch({
                            identifier: terminalEvent === "touchend" ? 31 : 32,
                            target: joystick,
                            clientX: center.x + 28,
                            clientY: center.y
                        });
                        joystick.dispatchEvent(new TouchEvent("touchstart", {
                            changedTouches: [touch], touches: [touch],
                            bubbles: true, cancelable: true
                        }));
                        joystick.dispatchEvent(new TouchEvent(terminalEvent, {
                            changedTouches: [touch], touches: [],
                            bubbles: true, cancelable: true
                        }));
                    }""",
                    {"center": center, "terminalEvent": terminal_event},
                )
                self.assertEqual(
                    page.locator("#joystickKnob").evaluate(
                        "(element) => element.style.transform"
                    ),
                    "translate3d(0px, 0px, 0px)",
                )

            page.evaluate(
                """() => window.__testSocket.emit({
                    type: "state",
                    players: [{
                        player_id: "webkit-player", name: "WebKit player",
                        x: .2, y: .5, score: 2100, lives: 3,
                        orbs_collected: 5, current_level: 9, game_over: false
                    }],
                    collectibles: {},
                    level9_worlds: {
                        "webkit-player": {
                            camera_progress: 0, orbs: {}, platforms: [],
                            cloud_platforms: [], pipes: [], question_blocks: [],
                            secret_bundles: [], wardens: [], projectiles: []
                        }
                    }
                })""",
                levels,
            )
            self.assertEqual(page.locator("#currentLevel").text_content(), "9")
            page.evaluate("window.__testSocket.sent = []")
            page.evaluate(
                """({center}) => {
                    const joystick = document.querySelector("#joystick");
                    const touch = new Touch({
                        identifier: 41, target: joystick,
                        clientX: center.x + 28, clientY: center.y
                    });
                    window.__level9Touch = touch;
                    joystick.dispatchEvent(new TouchEvent("touchstart", {
                        changedTouches: [touch], touches: [touch],
                        bubbles: true, cancelable: true
                    }));
                }""",
                {"center": center},
            )
            page.wait_for_timeout(250)
            movement_snapshot = page.evaluate(
                """() => ({
                    messages: window.__testSocket.sent,
                    knob: document.querySelector("#joystickKnob").style.transform,
                    level: document.querySelector("#currentLevel").textContent
                })"""
            )
            self.assertTrue(
                any(
                    message.get("type") == "platformer_input"
                    and message.get("run_axis") == 1
                    for message in movement_snapshot["messages"]
                ),
                f"Level 9 joystick movement was not sent: {movement_snapshot}",
            )
            page.locator("#spitButton").tap()
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            messages = page.evaluate(
                "() => window.__testSocket.sent.filter(message => message.type === 'platformer_input')"
            )
            run_index = next(
                index for index, message in enumerate(messages)
                if message["run_axis"] == 1 and not message["jump_pressed"]
            )
            jump_index = next(
                index for index, message in enumerate(messages[run_index + 1:], run_index + 1)
                if message["jump_pressed"]
            )
            release_index = next(
                index for index, message in enumerate(messages[jump_index + 1:], jump_index + 1)
                if message["run_axis"] == 0 and not message["jump_pressed"]
            )
            self.assertLess(run_index, jump_index)
            self.assertLess(jump_index, release_index)
            self.assertEqual(
                page.locator("#joystickKnob").evaluate(
                    "(element) => element.style.transform"
                ),
                "translate3d(0px, 0px, 0px)",
            )
            self.assertEqual(messages[-1]["run_axis"], 0)
            self.assertFalse(messages[-1]["jump_pressed"])

            page.evaluate(
                """() => {
                    window.__testSocket.emit({
                        type: "state",
                        players: [{
                            player_id: "webkit-player", name: "WebKit player",
                            x: .2, y: .5, score: 2410, lives: 3,
                            orbs_collected: 5, current_level: 10, game_over: false
                        }],
                        collectibles: {},
                        leaderboard: [],
                        level10_worlds: {
                            "webkit-player": {
                                boss: {hp: 300, max_hp: 300, phase: 1},
                                camera: {}, bounds: {}, drones: [], orbs: {},
                                hazards: [], projectiles: []
                            }
                        }
                    });
                    window.__testSocket.sent = [];
                }"""
            )
            page.evaluate(
                """({center}) => {
                    const joystick = document.querySelector("#joystick");
                    const touch = new Touch({
                        identifier: 51, target: joystick,
                        clientX: center.x + 28, clientY: center.y
                    });
                    joystick.dispatchEvent(new TouchEvent("touchstart", {
                        changedTouches: [touch], touches: [touch],
                        bubbles: true, cancelable: true
                    }));
                }""",
                {"center": center},
            )
            page.locator("#spitButton").dispatch_event(
                "pointerdown",
                {"pointerId": 52, "pointerType": "touch", "isPrimary": True},
            )
            page.wait_for_timeout(80)
            page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
            citadel_messages = page.evaluate(
                "() => window.__testSocket.sent.filter(message => message.type === 'citadel_input')"
            )
            self.assertTrue(
                any(
                    message["booster_x"] != 0 or message["booster_y"] != 0
                    for message in citadel_messages
                ),
                f"Level 10 joystick movement was not sent: {citadel_messages}",
            )
            self.assertTrue(
                any(message["charge_pressed"] for message in citadel_messages),
                f"Level 10 charge input was not sent: {citadel_messages}",
            )
            self.assertEqual(
                {
                    key: citadel_messages[-1][key]
                    for key in (
                        "booster_x",
                        "booster_y",
                        "charge_pressed",
                        "fire_released",
                    )
                },
                {
                    "booster_x": 0,
                    "booster_y": 0,
                    "charge_pressed": False,
                    "fire_released": False,
                },
            )
            self.assertEqual(
                page.locator("#spitButton").get_attribute("aria-pressed"), "false"
            )
            self.assertEqual(
                page.locator("#joystickKnob").evaluate(
                    "(element) => element.style.transform"
                ),
                "translate3d(0px, 0px, 0px)",
            )
        finally:
            context.close()


class ArcadeLevelEightTests(unittest.IsolatedAsyncioTestCase):
    """Focused contract tests for the authoritative submarine stage."""

    async def test_registry_boundary_and_pristine_entry(self) -> None:
        self.assertEqual(LEVELS[6].max_score, 1600)
        self.assertEqual(LEVELS[7].min_score, 1601)
        self.assertEqual(LEVELS[7].name, "Llama Submarine Deep-Sea Trench")
        self.assertEqual(VICTORY_SCORE, 2800)
        room = GameRoom(type("Storage", (), {"leaderboard": lambda self: []})())
        socket = FakeWebSocket()
        player = Player("submarine", "Diver", score=1601, current_level=8)
        room.connections[socket] = player
        room._enter_level8(player, 100.0)
        world = room.level8_worlds[player.player_id]
        self.assertEqual(player.oxygen, 100.0)
        self.assertEqual(player.submarine_input_sequence, -1)
        self.assertGreaterEqual(len(world["orbs"]), 48)
        self.assertEqual(len(world["wardens"]), 3)

    async def test_input_is_sequenced_and_position_collect_are_ignored(self) -> None:
        room = GameRoom(type("Storage", (), {"leaderboard": lambda self: []})())
        socket = FakeWebSocket()
        player = Player("submarine", "Diver", score=1601, current_level=8)
        room.connections[socket] = player
        room._enter_level8(player, 0.0)
        original = (player.x, player.y, player.score)
        await room.handle_message(socket, {"type": "position", "x": 0.9, "y": 0.9})
        await room.handle_message(socket, {"type": "collect", "orb_id": "trench-orb-0"})
        await room.handle_message(socket, {"type": "submarine_input", "sequence": 4, "thrust": True})
        await room.handle_message(socket, {"type": "submarine_input", "sequence": 3, "thrust": False})
        self.assertEqual((player.x, player.y, player.score), original)
        self.assertEqual(player.submarine_input_sequence, 4)
        self.assertTrue(player.submarine_thrust)
        await room.handle_message(socket, {"type": "submarine_input", "sequence": 5, "thrust": False})
        self.assertFalse(player.submarine_thrust)

    def make_room(self, **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
        socket = FakeWebSocket()
        options = {"score": 1601, "current_level": 8}
        options.update(kwargs)
        player = Player(options.pop("player_id", "deep-diver"), "Deep Diver", **options)
        room.connections[socket] = player
        room._enter_level8(player, 0.0)
        return room, socket, player

    def test_client_defaults_music_render_and_oxygen_source(self):
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        for token in ("Llama Submarine Deep-Sea Trench", "victoryScore = 2800",
                      "8: Object.freeze({", "submarine_input", "level8World",
                      'id="oxygen"', "oxygen"):
            self.assertIn(token, source)

    def test_level8_render_branch_is_reachable_and_uses_authoritative_coordinates(self):
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")

        def balanced_block(token):
            token_start = source.index(token)
            opening = source.index("{", token_start)
            depth = 0
            quote = None
            escaped = False
            index = opening
            while index < len(source):
                character = source[index]
                if quote:
                    if escaped:
                        escaped = False
                    elif character == "\\":
                        escaped = True
                    elif character == quote:
                        quote = None
                elif character in "'\"`":
                    quote = character
                elif source.startswith("//", index):
                    newline = source.find("\n", index + 2)
                    index = len(source) if newline == -1 else newline
                    continue
                elif source.startswith("/*", index):
                    comment_end = source.find("*/", index + 2)
                    index = len(source) if comment_end == -1 else comment_end + 1
                elif character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth == 0:
                        return token_start, index + 1, source[token_start:index + 1]
                index += 1
            self.fail(f"Unbalanced JavaScript block for {token!r}")

        _, andes_end, _ = balanced_block(
            'if (andes) {\n            context.fillStyle = "#535d5d";'
        )
        trench_start, _, trench_block = balanced_block(
            'if (trench) {\n             context.fillStyle = "#061334";'
        )
        self.assertGreater(trench_start, andes_end)
        self.assertLess(
            source.index("const trench = currentLevel.number === 8;"),
            trench_start,
        )
        self.assertIn("context.arc(vent.x, vent.y, vent.radius * 0.45", trench_block)
        self.assertIn("context.arc(orb.x, orb.y, 0.012", trench_block)
        self.assertIn("context.arc(bubble.x, bubble.y, 0.014", trench_block)
        self.assertIn(
            "context.fillRect(warden.x - 0.025, warden.y - 0.018",
            trench_block,
        )
        self.assertNotIn("camera_progress", trench_block)
        self.assertIn(
            "context.scale(authoritativeFacing.get(localPlayerId) || 1, 1);",
            trench_block,
        )

    def test_entry_is_exact_shield_and_pristine(self):
        room, _, player = self.make_room()
        world = room.level8_worlds[player.player_id]
        self.assertEqual(player.oxygen, 100)
        self.assertAlmostEqual(player.invulnerable_until, 10.0)
        self.assertEqual(player.submarine_velocity, 0)
        self.assertEqual(player.submarine_input_sequence, -1)
        self.assertEqual(len(world["orbs"]), 64)
        self.assertEqual(len(world["vents"]), 3)
        self.assertEqual(len(world["bubbles"]), 8)
        self.assertEqual([w["mode"] for w in world["wardens"]],
                         ["horizontal_patrol", "stationary_guardian", "vertical_loop"])

    def test_buoyancy_thrust_dt_and_scroll(self):
        room, _, player = self.make_room()
        room.level8_tick(9, now=20)
        rising = player.y
        self.assertLess(rising, 0.5)
        player.submarine_thrust = True
        before = player.y
        room.level8_tick(0.1, now=21)
        self.assertGreater(player.y, before)
        self.assertLessEqual(abs(player.submarine_velocity), 0.85)
        self.assertGreater(player.world_progress, 0)

    def test_boundary_shield_and_unshielded_event_parity(self):
        room, _, player = self.make_room()
        player.y = 0.15
        player.submarine_velocity = -1
        room.level8_tick(0.1, now=1)
        self.assertEqual(player.lives, STARTING_LIVES)
        player.invulnerable_until = 0
        player.y, player.submarine_velocity = 0.15, -1
        events = room.level8_tick(0.1, now=2)
        loss = next(e for e in events if e["event"] == "life_lost")
        self.assertEqual(loss["hazard_kind"], "trench_boundary")
        self.assertEqual(loss["duration_ms"], int(HAZARD_STUN_SECONDS * 1000))
        self.assertEqual(player.lives, STARTING_LIVES - 1)
        self.assertEqual(sum(e["event"] == "life_lost" for e in room.level8_tick(0.1, now=2.1)), 0)

    def test_vent_force_and_oxygen_bubble_authority(self):
        room, _, player = self.make_room()
        world = room.level8_worlds[player.player_id]
        world["vents"][0].update(x=player.x, y=player.y)
        player.submarine_velocity = 0.3
        events = room.level8_tick(0.1, now=1)
        self.assertLess(player.submarine_velocity, 0.3)
        self.assertTrue(any(e["event"] == "vent_force" for e in events))
        world["bubbles"]["b"] = {"id": "b", "x": player.x, "y": player.y, "amount": 20, "collected": False}
        player.oxygen = 90
        events = room.level8_tick(0, now=2)
        self.assertEqual(player.oxygen, 100)
        self.assertTrue(any(e["event"] == "oxygen_refilled" for e in events))
        self.assertTrue(world["bubbles"]["b"]["collected"])

    def test_oxygen_drain_clamp_zero_latch_and_game_over(self):
        room, _, player = self.make_room()
        player.oxygen = 0.1
        events = room.level8_tick(0.5, now=1)
        self.assertEqual(player.oxygen, 35)
        self.assertEqual(sum(e.get("hazard_kind") == "oxygen_zero" for e in events), 1)
        player.invulnerable_until = 0
        player.lives = 1
        player.oxygen = 0
        player.oxygen_zero_latched = True
        self.assertEqual(room.level8_tick(0, now=2), [])
        self.assertGreaterEqual(player.oxygen, 0)

    def test_wardens_contact_and_charge_metadata(self):
        room, _, player = self.make_room()
        world = room.level8_worlds[player.player_id]
        self.assertEqual(len(world["wardens"]), 3)
        self.assertTrue(all("pulse_hz" in w and "light_cone" in w for w in world["wardens"]))
        w1, w2, w3 = world["wardens"]
        x0 = w1["x"]
        room.level8_tick(0.1, now=1)
        self.assertLess(w1["x"], x0)
        player.x, player.y = w2["x"] - 0.05, w2["y"]
        w2["cooldown_until"] = 0
        room.level8_tick(0.01, now=2)
        self.assertTrue(w2["charging"])
        self.assertGreater(w2["cooldown_until"], 2)
        y0 = w3["y"]
        room.level8_tick(0.1, now=3)
        self.assertNotEqual(w3["y"], y0)
        player.invulnerable_until = 0
        player.x, player.y = w1["x"], w1["y"]
        self.assertEqual(sum(e["event"] == "life_lost" for e in room.level8_tick(0, now=4)), 1)

    async def test_auto_orbs_lives_persistence_and_level_nine_transition(self):
        room, _, player = self.make_room(score=1990, lives=2, orbs_collected=49)
        world = room.level8_worlds[player.player_id]
        world["orbs"] = {"finish": {"id": "finish", "x": player.x, "y": player.y, "collected": False}}
        events = room.level8_tick(0, now=1)
        self.assertEqual(player.score, 2000)
        self.assertFalse(player.victory_announced)
        self.assertEqual(player.current_level, 9)
        self.assertIn(player.player_id, room.level9_worlds)
        self.assertTrue(player.grounded)
        self.assertAlmostEqual(player.invulnerable_until, 11.0)
        self.assertEqual(player.lives, 3)
        self.assertEqual(sum(e["event"] == "victory" for e in events), 0)
        world["orbs"] = {"again": {"id": "again", "x": player.x, "y": player.y, "collected": False}}
        self.assertEqual(sum(e["event"] == "victory" for e in room.level8_tick(0, now=2)), 0)

    async def test_reset_restores_submarine_world(self):
        room, socket, player = self.make_room()
        world = room.level8_worlds[player.player_id]
        world["vents"][0]["active"] = False
        world["bubbles"]["oxygen-bubble-0"]["collected"] = True
        world["orbs"]["trench-orb-0"]["collected"] = True
        player.game_over = True
        await room._reset(socket)
        self.assertNotIn(player.player_id, room.level8_worlds)
        room._enter_level8(player, 0)
        fresh = room.level8_worlds[player.player_id]
        self.assertEqual(player.oxygen, 100)
        self.assertEqual(player.world_progress, 0)
        self.assertTrue(fresh["vents"][0]["active"])
        self.assertFalse(fresh["bubbles"]["oxygen-bubble-0"]["collected"])
        self.assertFalse(fresh["orbs"]["trench-orb-0"]["collected"])

    async def test_multiplayer_disconnect_and_timer_lifecycle(self):
        room, socket, player = self.make_room(player_id="deep-diver")
        other_socket = FakeWebSocket()
        other = Player("other-diver", "Other Diver", score=1601, current_level=8)
        room.connections[other_socket] = other
        room._enter_level8(other, 0)
        room.level8_worlds[player.player_id]["vents"][0]["active"] = False
        self.assertTrue(room.level8_worlds[other.player_id]["vents"][0]["active"])
        await room.disconnect(socket)
        self.assertNotIn(player.player_id, room.level8_worlds)
        await room.start()
        self.assertIsNotNone(room.level8_timer_task)
        await room.stop()
        self.assertIsNone(room.level8_timer_task)

    def test_world_snapshots_are_deep_and_isolated(self) -> None:
        room = GameRoom(type("Storage", (), {"leaderboard": lambda self: []})())
        first = Player("one", "One", score=1601, current_level=8)
        second = Player("two", "Two", score=1601, current_level=8)
        room.connections[FakeWebSocket()] = first
        room.connections[FakeWebSocket()] = second
        room._enter_level8(first, 0.0)
        room._enter_level8(second, 0.0)
        snapshot = room.snapshot()
        snapshot["level8_worlds"]["one"]["orbs"]["trench-orb-0"]["x"] = 999
        snapshot["level8_worlds"]["one"]["vents"][0]["active"] = False
        self.assertNotEqual(room.level8_worlds["one"]["orbs"]["trench-orb-0"]["x"], 999)
        self.assertTrue(room.level8_worlds["two"]["vents"][0]["active"])


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = []
        self.closed = None

    async def send_json(self, message) -> None:
        self.messages.append(message)

    async def close(self, code=1000, reason="") -> None:
        self.closed = (code, reason)


class ArcadeVictoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_level_one_rejects_remote_chip_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            player = Player("player", "Runner", x=0.5, y=0.5)
            room.connections[socket] = player

            await room.handle_message(
                socket, {"type": "level_one_chip", "chip_id": "chip-1"}
            )

            self.assertEqual(player.level_one_chip_ids, set())
            self.assertEqual(player.current_level, 1)

    async def test_level_one_validates_chips_and_advances_at_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            player = Player("player", "Runner")
            room.connections[socket] = player

            for chip_id, (x, y) in LEVEL_ONE_CHIPS.items():
                player.x, player.y = x, y
                await room.handle_message(
                    socket, {"type": "level_one_chip", "chip_id": chip_id}
                )
            player.x = LEVEL_ONE_FINISH_X
            await room.handle_message(socket, {"type": "level_one_complete"})

            self.assertEqual(player.level_one_chip_ids, set(LEVEL_ONE_CHIPS))
            self.assertEqual(player.score, 110)
            self.assertEqual(player.current_level, 2)
            json.dumps(room.snapshot())

    async def test_new_connection_replaces_duplicate_player_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            first_socket = FakeWebSocket()
            second_socket = FakeWebSocket()

            await room.handle_message(
                first_socket,
                {
                    "type": "join",
                    "player_key": "11111111-1111-4111-8111-111111111111",
                    "ownership_token": "first-ownership-token-that-is-long-enough",
                    "name": "Kream",
                },
            )
            await room.handle_message(
                second_socket,
                {
                    "type": "join",
                    "player_key": "22222222-2222-4222-8222-222222222222",
                    "ownership_token": "second-ownership-token-that-is-long-enough",
                    "name": "kream",
                },
            )

            self.assertNotIn(first_socket, room.connections)
            self.assertIn(second_socket, room.connections)
            self.assertEqual(len(room.connections), 1)
            self.assertEqual(first_socket.closed[0], 4001)

    async def test_one_hazard_hit_costs_one_life_and_stuns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            room.connections[socket] = Player(
                player_id="player",
                name="Runner",
                x=0.2,
                y=0.4,
                score=251,
                current_level=3,
            )
            now = asyncio.get_event_loop().time()
            hazard = room.farm_tractors(now)[0]
            destination = {"x": hazard["x"], "y": hazard["y"]}
            await room.handle_message(socket, {"type": "position", **destination})
            player = room.connections[socket]
            self.assertGreaterEqual(player.stunned_until - now, HAZARD_STUN_SECONDS - 0.05)
            self.assertEqual((player.x, player.y), (0.5, 0.5))
            self.assertEqual(socket.messages[-1]["event"], "life_lost")
            self.assertEqual(socket.messages[-1]["duration_ms"], int(HAZARD_STUN_SECONDS * 1000))
            self.assertEqual(
                socket.messages[-1]["invulnerability_duration_ms"],
                int(LIFE_LOSS_INVULNERABILITY_SECONDS * 1000),
            )
            self.assertEqual(player.bump_count, 0)
            self.assertEqual(player.lives, STARTING_LIVES - 1)
            self.assertGreaterEqual(
                player.invulnerable_until - now,
                LIFE_LOSS_INVULNERABILITY_SECONDS - 0.05,
            )

            await room.handle_message(socket, {"type": "position", "x": 0.8, "y": 0.8})
            self.assertEqual((player.x, player.y), (0.5, 0.5))

    async def test_invulnerability_prevents_immediate_second_life_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            player = Player("player", "Runner", x=0.2, y=0.4, score=251, current_level=3)
            room.connections[socket] = player
            hazard = room.farm_tractors(asyncio.get_event_loop().time())[0]

            await room.handle_message(
                socket, {"type": "position", "x": hazard["x"], "y": hazard["y"]}
            )
            lives_after_first_hit = player.lives
            player.stunned_until = 0
            player.x, player.y = 0.2, 0.4
            await room.handle_message(
                socket, {"type": "position", "x": hazard["x"], "y": hazard["y"]}
            )

            self.assertEqual(player.lives, lives_after_first_hit)
            self.assertEqual((player.x, player.y), (hazard["x"], hazard["y"]))
            snapshot_player = room.snapshot()["players"][0]
            self.assertTrue(snapshot_player["invulnerable"])
            self.assertGreater(snapshot_player["invulnerability_remaining_ms"], 0)

    async def test_five_hits_cost_five_lives_and_end_game(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            player = Player("player", "Runner", x=0.2, y=0.4, score=251, current_level=3)
            room.connections[socket] = player

            for lost_life in range(STARTING_LIVES):
                player.stunned_until = 0
                player.invulnerable_until = 0
                hazard = room.farm_tractors(asyncio.get_event_loop().time())[0]
                await room.handle_message(
                    socket, {"type": "position", "x": hazard["x"], "y": hazard["y"]}
                )
                self.assertEqual(player.lives, STARTING_LIVES - lost_life - 1)
                if not player.game_over:
                    player.x, player.y = 0.2, 0.4

            self.assertTrue(player.game_over)
            self.assertEqual(player.lives, 0)
            self.assertEqual(socket.messages[-1]["event"], "game_over")

    async def test_every_fifty_orbs_awards_an_extra_life(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            stored = storage.register_player(
                "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "another-ownership-token-long-enough",
                "Collector",
            )
            ticket = storage.issue_match_ticket(
                stored.player_id,
                "another-ownership-token-long-enough",
                entry_fee_tlama=25,
            )
            room = GameRoom(storage)
            socket = FakeWebSocket()
            player = Player(
                stored.player_id,
                "Collector",
                x=0.22,
                y=0.24,
                ticket_id=ticket.ticket_id,
                ownership_token="another-ownership-token-long-enough",
                lives=2,
                orbs_collected=ORBS_PER_EXTRA_LIFE - 1,
            )
            room.connections[socket] = player

            await room._collect(socket, "orb-1")

            self.assertEqual(player.orbs_collected, ORBS_PER_EXTRA_LIFE)
            self.assertEqual(player.lives, 3)
            self.assertTrue(socket.messages[-1]["extra_life_awarded"])

    async def test_extra_life_recovery_never_exceeds_five_lives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            stored = storage.register_player(
                "cccccccccccccccccccccccccccccccc",
                "maximum-life-ownership-token-long-enough",
                "Survivor",
            )
            ticket = storage.issue_match_ticket(
                stored.player_id,
                "maximum-life-ownership-token-long-enough",
                entry_fee_tlama=25,
            )
            room = GameRoom(storage)
            socket = FakeWebSocket()
            room.connections[socket] = Player(
                stored.player_id,
                "Survivor",
                x=0.22,
                y=0.24,
                ticket_id=ticket.ticket_id,
                ownership_token="maximum-life-ownership-token-long-enough",
                lives=STARTING_LIVES,
                orbs_collected=ORBS_PER_EXTRA_LIFE - 1,
            )

            await room._collect(socket, "orb-1")

            self.assertEqual(room.connections[socket].lives, STARTING_LIVES)
            self.assertFalse(socket.messages[-1]["extra_life_awarded"])

    async def test_new_match_starts_with_ten_second_shield(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            room = GameRoom(ArcadeStorage(Path(directory) / "arcade.sqlite3"))
            socket = FakeWebSocket()
            before_join = asyncio.get_event_loop().time()

            await room.handle_message(
                socket,
                {
                    "type": "join",
                    "player_key": "33333333-3333-4333-8333-333333333333",
                    "ownership_token": "starting-shield-token-long-enough",
                    "name": "Shielded",
                },
            )

            player = room.connections[socket]
            self.assertGreaterEqual(
                player.invulnerable_until - before_join,
                STARTING_SHIELD_SECONDS - 0.05,
            )
            self.assertTrue(room.snapshot()["players"][0]["invulnerable"])

    async def test_game_over_player_can_reset_without_reloading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            room = GameRoom(storage)
            socket = FakeWebSocket()
            stored = storage.register_player(
                "44444444444444444444444444444444",
                "reset-owner-token-that-is-definitely-long-enough",
                "Again",
            )
            player = Player(
                player_id=stored.player_id,
                name="Again",
                score=420,
                current_level=3,
                victory_announced=False,
                ticket_id="old-ticket",
                ownership_token="reset-owner-token-that-is-definitely-long-enough",
                lives=0,
                orbs_collected=42,
                game_over=True,
            )
            room.connections[socket] = player
            ticket = storage.issue_match_ticket(
                player.player_id,
                player.ownership_token,
                entry_fee_tlama=25,
            )
            player.ticket_id = ticket.ticket_id
            before_reset = asyncio.get_event_loop().time()

            await room.handle_message(socket, {"type": "reset"})

            self.assertEqual(player.score, 0)
            self.assertEqual(player.current_level, 1)
            self.assertEqual(player.lives, STARTING_LIVES)
            self.assertEqual(player.orbs_collected, 0)
            self.assertFalse(player.game_over)
            self.assertFalse(player.victory_announced)
            self.assertEqual((player.x, player.y), (0.5, 0.5))
            self.assertGreaterEqual(
                player.invulnerable_until - before_reset,
                STARTING_SHIELD_SECONDS - 0.05,
            )
            self.assertEqual(socket.messages[-1]["event"], "game_reset")

    async def test_victory_is_emitted_once_at_victory_score(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = ArcadeStorage(Path(directory) / "arcade.sqlite3")
            stored = storage.register_player(
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "ownership-token-that-is-long-enough",
                "Winner",
            )
            ticket = storage.issue_match_ticket(
                stored.player_id,
                "ownership-token-that-is-long-enough",
                entry_fee_tlama=25,
            )
            room = GameRoom(storage)
            socket = FakeWebSocket()
            room.connections[socket] = Player(
                player_id=stored.player_id,
                name="Winner",
                x=0.50,
                y=0.50,
                score=2390,
                current_level=9,
                ticket_id=ticket.ticket_id,
                ownership_token="ownership-token-that-is-long-enough",
            )

            room._enter_level10(room.connections[socket], 0.0)
            player = room.connections[socket]
            world = room.level10_worlds[stored.player_id]
            player.super_spit_charge = 25
            player.super_spit_release_latched = True
            world["boss"]["shield_until"] = 0
            world["boss"]["vulnerable"] = True
            world["projectiles"] = [{"id": "authoritative", "x": .5, "y": .42, "ttl": 1.0}]
            world["boss"]["hp"] = 25
            events = room.level10_tick(0.0, now=1.0)
            victory_messages = [message for message in events if message.get("event") == "victory"]
            self.assertEqual(len(victory_messages), 1)
            self.assertEqual(victory_messages[0]["event_metadata"]["citadel_complete"], True)
            self.assertEqual(victory_messages[0]["event_metadata"]["level_clear"], True)
            self.assertEqual(victory_messages[0]["cue"], "grand_fanfare")
            self.assertEqual(len([e for e in room.level10_tick(0, now=2) if e.get("event") == "victory"]), 0)


class ArcadeLevelSevenTests(unittest.IsolatedAsyncioTestCase):
    def make_room(self, player_id="level-seven-player", **kwargs):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
        socket = FakeWebSocket()
        options = {
            "score": 1201, "current_level": 7,
            "invulnerable_until": asyncio.get_event_loop().time() + STARTING_SHIELD_SECONDS,
        }
        options.update(kwargs)
        player = Player(player_id, "Cavern Pilot", **options)
        room.connections[socket] = player
        room._ensure_level7(player)
        return room, socket, player

    def test_registry_and_client_level_seven_defaults(self):
        self.assertEqual(LEVELS[5].max_score, 1200)
        self.assertEqual(LEVELS[6].min_score, 1201)
        self.assertEqual(level_for_score(1200).number, 6)
        self.assertEqual(level_for_score(1201).number, 7)
        self.assertEqual(VICTORY_SCORE, 2800)
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn("number: 7, name: \"Llamacopter Caverns\"", source)
        self.assertIn("victoryScore = 2800", source)
        self.assertIn("7: Object.freeze({", source)
        self.assertIn('id="spitButton"', source)
        self.assertIn('type: "vehicle_input"', source)

    async def test_entry_initializes_exact_shield_and_dense_world(self):
        room, socket, player = self.make_room(score=1200, current_level=6)
        room.collectibles["orb-1"] = {"x": player.x, "y": player.y}
        before = asyncio.get_event_loop().time()
        await room._collect(socket, "orb-1")
        self.assertEqual(player.current_level, 7)
        self.assertEqual((player.x, player.y), (0.18, 0.50))
        self.assertEqual(player.world_progress, 0.0)
        self.assertEqual(player.vehicle_velocity, 0.0)
        self.assertGreaterEqual(player.invulnerable_until - before, 9.95)
        self.assertGreaterEqual(len(room.level7_worlds[player.player_id]["orbs"]), 48)

    async def test_position_injection_is_ignored(self):
        room, socket, player = self.make_room()
        original = (player.x, player.y)
        await room.handle_message(socket, {"type": "position", "x": 0.99, "y": 0.01})
        self.assertEqual((player.x, player.y), original)

    async def test_owned_sequenced_input_and_fire_cooldown(self):
        room, socket, player = self.make_room()
        other = FakeWebSocket()
        room.connections[other] = Player("other", "Other", score=1201, current_level=7)
        await room.handle_message(socket, {"type": "vehicle_input", "thrust": True, "fire": True, "sequence": 4})
        self.assertTrue(player.vehicle_thrust)
        self.assertEqual(len(room.level7_worlds[player.player_id]["projectiles"]), 1)
        await room.handle_message(socket, {"type": "vehicle_input", "thrust": False, "fire": True, "sequence": 3})
        self.assertTrue(player.vehicle_thrust)
        self.assertEqual(len(room.level7_worlds[player.player_id]["projectiles"]), 1)
        await room.handle_message(other, {"type": "vehicle_input", "thrust": True, "fire": True, "sequence": 1})
        self.assertEqual(len(room.level7_worlds[player.player_id]["projectiles"]), 1)
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        self.assertIn('thrust: false, fire: true', source)

    def test_bounded_signed_physics_and_autoscroll(self):
        room, _, player = self.make_room()
        player.vehicle_thrust = False
        room.level7_tick(99)
        self.assertGreater(player.vehicle_velocity, 0)
        falling_y = player.y
        player.vehicle_velocity = 0
        room.level7_tick(0.1)
        self.assertGreater(player.y, falling_y)
        player.vehicle_thrust = True
        room.level7_tick(0.1)
        self.assertLess(player.vehicle_velocity, 0)
        self.assertGreater(player.world_progress, 0)

    def test_wardens_are_three_deterministic_visible_recycling_guards(self):
        room, _, player = self.make_room()
        wardens = room.level7_worlds[player.player_id]["wardens"]
        self.assertEqual([warden["mode"] for warden in wardens], ["sine", "vertical", "sine"])
        room.level7_tick(0.1, now=10)
        self.assertEqual(len(wardens), 3)
        self.assertTrue(all(0.45 <= warden["x"] <= 1.02 for warden in wardens))

    def test_projectile_swept_and_bumper_forward_collisions(self):
        room, _, player = self.make_room()
        world = room.level7_worlds[player.player_id]
        world["blocks"] = [{"id": "swept", "x": 0.30, "y": player.y, "destroyed": False}]
        world["projectiles"] = [{"id": "p", "x": 0.20, "y": player.y, "speed": 2, "ttl": 1}]
        room.level7_tick(0.1)
        self.assertTrue(world["blocks"][0]["destroyed"])
        world["blocks"] = [{"id": "miss", "x": player.x + 0.03, "y": 0.2, "destroyed": False}]
        events = room.level7_tick(0.01)
        self.assertFalse(world["blocks"][0]["destroyed"])
        world["blocks"] = [{"id": "bumper", "x": player.x + 0.03, "y": player.y, "destroyed": False}]
        events = room.level7_tick(0.01)
        self.assertTrue(world["blocks"][0]["destroyed"])

    async def test_shield_boundary_and_unshielded_boundary_event_parity(self):
        room, _, player = self.make_room()
        player.y = 0.13
        player.vehicle_velocity = -1
        room.level7_tick(0.1, now=1)
        self.assertEqual(player.lives, STARTING_LIVES)
        player.invulnerable_until = 0
        player.y = 0.13
        player.vehicle_velocity = -1
        events = room.level7_tick(0.1, now=2)
        self.assertEqual(player.lives, STARTING_LIVES - 1)
        loss = next(event for event in events if event["event"] == "life_lost")
        self.assertIn("duration_ms", loss)
        self.assertIn("invulnerability_duration_ms", loss)
        self.assertEqual(sum(event["event"] == "life_lost" for event in events), 1)

    async def test_auto_orbs_persist_score_lives_and_world(self):
        room, socket, player = self.make_room(lives=2, orbs_collected=49)
        world = room.level7_worlds[player.player_id]
        world["orbs"] = {"one": {"id": "one", "x": player.x, "y": player.y}}
        await asyncio.sleep(0)
        events = room.level7_tick(0.01)
        self.assertEqual(player.score, 1211)
        self.assertEqual(player.lives, 3)
        self.assertEqual(player.orbs_collected, 50)
        self.assertIn("one", {event.get("orb_id") for event in events})
        self.assertEqual(len(world["blocks"]), 9)
        self.assertIsNotNone(room.storage.leaderboard())

    async def test_reset_pristinely_restores_private_world(self):
        room, socket, player = self.make_room()
        world = room.level7_worlds[player.player_id]
        world["blocks"][0]["destroyed"] = True
        world["projectiles"].append({"id": "x"})
        player.game_over = True
        await room._reset(socket)
        self.assertNotIn(player.player_id, room.level7_worlds)
        player.score, player.current_level = 1201, 7
        fresh = room._ensure_level7(player)
        self.assertEqual(player.world_progress, 0.0)
        self.assertFalse(fresh["blocks"][0]["destroyed"])
        self.assertGreaterEqual(len(fresh["orbs"]), 48)

    async def test_multiplayer_worlds_and_disconnect_cleanup_are_isolated(self):
        room, socket, player = self.make_room("one")
        second = FakeWebSocket()
        other = Player("two", "Second", score=1201, current_level=7)
        room.connections[second] = other
        room._ensure_level7(other)
        await room.handle_message(socket, {"type": "vehicle_input", "fire": True, "sequence": 1})
        self.assertEqual(len(room.level7_worlds["one"]["projectiles"]), 1)
        self.assertEqual(len(room.level7_worlds["two"]["projectiles"]), 0)
        await room.disconnect(socket)
        self.assertNotIn("one", room.level7_worlds)
        self.assertIn("two", room.level7_worlds)

    def test_snapshot_is_deep_non_aliasing(self):
        room, _, player = self.make_room()
        first = room.snapshot()
        first["level7_worlds"][player.player_id]["blocks"][0]["destroyed"] = True
        first["level7_worlds"][player.player_id]["orbs"].clear()
        second = room.snapshot()
        self.assertFalse(second["level7_worlds"][player.player_id]["blocks"][0]["destroyed"])
        self.assertGreater(len(second["level7_worlds"][player.player_id]["orbs"]), 0)

    async def test_level_seven_transitions_to_level_eight_at_1601(self):
        room, socket, player = self.make_room(score=1590)
        world = room.level7_worlds[player.player_id]
        world["orbs"] = {"finish": {"id": "finish", "x": player.x, "y": player.y}}
        events = room.level7_tick(0.01)
        self.assertEqual(player.score, 1600)
        self.assertFalse(player.victory_announced)
        world["orbs"] = {"again": {"id": "again", "x": player.x, "y": player.y}}
        events = room.level7_tick(0.01)
        self.assertEqual(player.score, 1610)
        self.assertEqual(player.current_level, 8)
        self.assertEqual(player.oxygen, 100)
        self.assertEqual(player.submarine_velocity, 0)
        self.assertEqual(player.submarine_input_sequence, -1)
        self.assertGreaterEqual(player.invulnerable_until - asyncio.get_event_loop().time(), 9.8)
        self.assertGreaterEqual(len(room.level8_worlds[player.player_id]["orbs"]), 48)


class ArcadeLevelNineTests(unittest.IsolatedAsyncioTestCase):
    """Focused contracts for the terminal server-authoritative sky platformer."""

    def make_room(self, score=2001, player_id="sky-player"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
        socket = FakeWebSocket()
        player = Player(player_id, "Sky Runner", score=score, current_level=9)
        room.connections[socket] = player
        room._enter_level9(player, 100.0)
        return room, socket, player

    def test_registry_entry_client_and_terminal_contract(self):
        self.assertEqual((LEVELS[7].max_score, LEVELS[8].min_score), (2000, 2001))
        self.assertEqual(level_for_score(2000), LEVELS[7])
        self.assertEqual(level_for_score(2001), LEVELS[8])
        self.assertEqual(VICTORY_SCORE, 2800)
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        for token in ("Llama Sky Kingdom", "victoryScore = 2800",
                      "9: Object.freeze({", "platformer_input", "level9World"):
            self.assertIn(token, source)

    def test_pristine_start_shield_world_and_wardens(self):
        room, _, player = self.make_room()
        world = room.level9_worlds[player.player_id]
        self.assertTrue(player.grounded)
        self.assertEqual((player.vx, player.vy, player.platformer_input_sequence), (0, 0, -1))
        self.assertAlmostEqual(player.invulnerable_until, 110)
        self.assertEqual(len(world["orbs"]), LEVEL9_ORB_COUNT)
        self.assertEqual(len(world["wardens"]), 3)

    async def test_injection_and_sequence_validation(self):
        room, socket, player = self.make_room()
        original = (player.x, player.y, player.score)
        for payload in ({"type": "position", "x": 99, "y": 99},
                        {"type": "collect", "orb_id": "sky-orb-0"},
                        {"type": "vehicle_input", "sequence": 2, "thrust": True},
                        {"type": "submarine_input", "sequence": 2, "thrust": True}):
            await room.handle_message(socket, payload)
        self.assertEqual((player.x, player.y, player.score), original)
        await room.handle_message(socket, {"type": "platformer_input", "sequence": 2, "run_axis": 1, "jump_pressed": True})
        await room.handle_message(socket, {"type": "platformer_input", "sequence": 1, "run_axis": -1, "jump_pressed": True})
        await room.handle_message(socket, {"type": "platformer_input", "sequence": 3, "run_axis": 4, "jump_pressed": True})
        self.assertEqual((player.run_axis, player.platformer_input_sequence), (1, 2))
        self.assertTrue(player.jump_latched)
        player.grounded = False
        await room.handle_message(socket, {"type": "platformer_input", "sequence": 4, "run_axis": 0, "jump_pressed": True})
        self.assertFalse(player.jump_latched)

    def test_physics_collision_question_blocks_and_clouds(self):
        room, _, player = self.make_room()
        world = room.level9_worlds[player.player_id]
        player.run_axis = 1
        room.level9_tick(99, now=101)
        self.assertLessEqual(player.vx, .72)
        player.run_axis = 0
        room.level9_tick(.1, now=102)
        self.assertLess(player.vx, .72)
        block = world["question_blocks"][0]
        player.x, player.y, player.vy = block["x"], block["y"] + .4, -3
        events = room.level9_tick(.1, now=103)
        self.assertTrue(block["hit"])
        self.assertEqual(sum(e["event"] == "question_block_hit" for e in events), 1)
        self.assertEqual(len(world["secret_bundles"]), 1)
        self.assertLessEqual(player.vy, 1.8)

    def test_physics_keeps_avatar_inside_camera_and_canvas_boundaries(self):
        room, _, player = self.make_room()
        world = room.level9_worlds[player.player_id]
        player.x, player.y = -50, -50
        player.vx, player.vy = -99, -99
        room.level9_tick(.1, now=101)
        screen_x = player.x - world["camera_progress"]
        self.assertGreaterEqual(screen_x, LEVEL9_AVATAR_HALF_WIDTH)
        self.assertLessEqual(screen_x, 1 - LEVEL9_AVATAR_HALF_WIDTH)
        self.assertGreaterEqual(player.y, LEVEL9_AVATAR_HALF_HEIGHT)
        self.assertLessEqual(player.y, 1 - LEVEL9_AVATAR_HALF_HEIGHT)
        self.assertGreaterEqual(player.vx, -0.72)
        self.assertGreaterEqual(player.vy, 0)

    def test_wardens_hazards_orbs_victory_and_metadata(self):
        room, _, player = self.make_room(score=2390)
        world = room.level9_worlds[player.player_id]
        self.assertEqual([w["mode"] for w in world["wardens"]],
                         ["platform_patrol", "projectile_guard", "pipe_patrol"])
        w1, w2, _ = world["wardens"]
        w1["x"], w1["direction"] = w1["max_x"], 1
        player.orbs_collected, player.lives = 49, 2
        world["orbs"] = {"finish": {"id": "finish", "x": player.x, "y": player.y, "collected": False}}
        events = room.level9_tick(0, now=101)
        self.assertEqual(w1["direction"], -1)
        self.assertEqual(player.score, 2400)
        self.assertFalse(any(event["event"] == "victory" for event in events))
        self.assertEqual(player.current_level, 9)
        self.assertTrue(world["orbs"]["finish"]["collected"])
        self.assertGreaterEqual(w2["cooldown_until"], 101)

    def test_orb_overlap_updates_score_and_counter_once(self):
        room, _, player = self.make_room(score=2000)
        world = room.level9_worlds[player.player_id]
        orb = world["orbs"]["sky-orb-0"]
        player.x, player.y = orb["x"], orb["y"]
        room.level9_tick(0, now=101)
        self.assertEqual((player.score, player.orbs_collected), (2010, 1))
        self.assertTrue(orb["collected"])
        room.level9_tick(0, now=102)
        self.assertEqual((player.score, player.orbs_collected), (2010, 1))

    def test_collecting_every_level_nine_orb_clears_room_and_enters_level_ten(self):
        room, _, player = self.make_room(score=2000)
        world = room.level9_worlds[player.player_id]
        for orb in world["orbs"].values():
            orb["x"], orb["y"] = player.x, player.y
        events = room.level9_tick(0, now=101)
        self.assertEqual(player.score, 2410)
        self.assertEqual(player.orbs_collected, LEVEL9_ORB_COUNT)
        self.assertEqual(player.current_level, 10)
        self.assertNotIn(player.player_id, room.level9_worlds)
        self.assertIn(player.player_id, room.level10_worlds)
        level_up = [event for event in events if event["event"] == "level_up"]
        self.assertEqual(len(level_up), 1)
        self.assertEqual(
            level_up[0]["event_metadata"],
            {"level_clear": True, "orbs_cleared": True},
        )

    def test_level_eight_authoritative_orb_enters_level_nine_once(self):
        room = GameRoom(type("Storage", (), {"leaderboard": lambda self: []})())
        socket = FakeWebSocket()
        player = Player("transition", "Transition", score=1990, current_level=8)
        room.connections[socket] = player
        room._enter_level8(player, 0)
        world = room.level8_worlds[player.player_id]
        world["orbs"] = {"finish": {"id": "finish", "x": player.x, "y": player.y, "collected": False}}
        events = room.level8_tick(0, now=1)
        self.assertEqual((player.score, player.current_level), (2000, 9))
        self.assertTrue(player.grounded)
        self.assertAlmostEqual(player.invulnerable_until, 11)
        self.assertEqual(sum(event["event"] == "level_up" for event in events), 1)
        self.assertEqual(
            len(room.level9_worlds[player.player_id]["orbs"]),
            LEVEL9_ORB_COUNT,
        )

    async def test_reset_isolation_snapshot_disconnect_and_timer_lifecycle(self):
        room, socket, player = self.make_room()
        second = FakeWebSocket()
        other = Player("other", "Other", score=2001, current_level=9)
        room.connections[second] = other
        room._enter_level9(other, 100)
        snap = room.snapshot()
        snap["level9_worlds"][player.player_id]["orbs"]["sky-orb-0"]["collected"] = True
        self.assertFalse(room.level9_worlds[player.player_id]["orbs"]["sky-orb-0"]["collected"])
        await room.disconnect(socket)
        self.assertNotIn(player.player_id, room.level9_worlds)
        self.assertIn(other.player_id, room.level9_worlds)
        other.game_over = True
        await room._reset(second)
        self.assertNotIn(other.player_id, room.level9_worlds)
        await room.start()
        self.assertIsNotNone(room.level9_timer_task)
        await room.stop()
        self.assertIsNone(room.level9_timer_task)

class ArcadeLevelTenTests(unittest.IsolatedAsyncioTestCase):
    """Dedicated server/client contracts for the final Citadel Core."""

    def make_room(self, score=2410, player_id="citadel-player"):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        room = GameRoom(ArcadeStorage(Path(directory.name) / "arcade.sqlite3"))
        socket = FakeWebSocket()
        player = Player(player_id, "Citadel Pilot", score=score, current_level=10)
        room.connections[socket] = player
        room._enter_level10(player, 100.0)
        return room, socket, player

    def test_registry_boundaries_and_client_registry(self):
        self.assertEqual(len(LEVELS), 10)
        self.assertEqual(LEVELS[8].max_score, 2400)
        self.assertEqual(LEVELS[9].min_score, 2401)
        self.assertEqual(level_for_score(2400), LEVELS[8])
        self.assertEqual(level_for_score(2401), LEVELS[9])
        self.assertEqual(VICTORY_SCORE, 2800)
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        for token in ("Trust Llama Citadel Core", "10: Object.freeze", "citadel_input",
                      "level10World", "superCharge", "grand_fanfare"):
            self.assertIn(token, source)
        self.assertIn("const quantizeCitadelAxis = (value) => {", source)
        self.assertIn("Math.abs(value) < 0.12", source)
        self.assertIn("const axisX = quantizeCitadelAxis(dx);", source)
        self.assertIn("const axisY = quantizeCitadelAxis(dy);", source)
        self.assertIn("authoritativeCitadelPosition", source)
        self.assertIn("1 - Math.exp(-20 * delta)", source)
        self.assertIn("levelNumber === 10 && now - lastPositionSent > 40", source)
        self.assertIn("const wasActive = activeTouchId !== null", source)
        self.assertNotIn(
            "const axisX = Math.max(-1, Math.min(1, Math.round(dx)));",
            source,
        )
        for token in (
            'let citadelChargeHeld = false;',
             'let citadelSuperReady = false;',
            'let localVictory = false;',
            'spitButton.textContent = isCitadel',
             'citadelSuperReady ? "SUPER SPIT READY — release!"',
             'const setCitadelSuperReady = (ready, { notify = true } = {}) => {',
            'spitButton.addEventListener("pointerdown"',
            'spitButton.addEventListener("pointerup"',
            'spitButton.addEventListener("pointercancel"',
            'spitButton.addEventListener("lostpointercapture"',
            'window.addEventListener("blur"',
            'charge_pressed: keys.has("Space") || citadelChargeHeld',
            'spitButton.disabled = localGameOver || localVictory;',
        ):
            self.assertIn(token, source)
        self.assertIn('clearCitadelCharge({ fire: false, sendInput: false });', source)

    def test_pristine_entry_shield_boss_and_drones(self):
        room, _, player = self.make_room()
        world = room.level10_worlds[player.player_id]
        self.assertEqual(player.x, .18)
        self.assertAlmostEqual(player.invulnerable_until, 110)
        self.assertEqual(world["boss"]["hp"], 300)
        self.assertEqual(world["boss"]["phase"], 1)
        self.assertEqual(len(world["drones"]), 2)
        self.assertEqual(player.super_spit_charge, 0)

    async def test_protocol_injection_and_sequence_validation(self):
        room, socket, player = self.make_room()
        original = (player.x, player.y, player.score)
        for payload in ({"type": "position", "x": 9, "y": 9},
                        {"type": "collect", "orb_id": "citadel-orb-0"},
                        {"type": "vehicle_input", "sequence": 5, "thrust": True},
                        {"type": "submarine_input", "sequence": 6, "thrust": True},
                        {"type": "platformer_input", "sequence": 7, "run_axis": 1}):
            await room.handle_message(socket, payload)
        await room.handle_message(socket, {"type": "citadel_input", "sequence": 3,
                                           "booster_x": 1, "booster_y": 1,
                                           "charge_pressed": False, "fire_released": False})
        await room.handle_message(socket, {"type": "citadel_input", "sequence": 2,
                                           "booster_x": 0, "booster_y": 0,
                                           "charge_pressed": False, "fire_released": False})
        await room.handle_message(socket, {"type": "citadel_input", "sequence": 4,
                                           "booster_x": 9, "booster_y": 0,
                                           "charge_pressed": False, "fire_released": False})
        self.assertEqual((player.x, player.y, player.score), original)
        self.assertEqual(player.citadel_input_sequence, 3)

    def test_physics_dt_bounds_and_normalized_diagonal(self):
        room, _, player = self.make_room()
        player.booster_x = player.booster_y = 1
        room.level10_tick(99, now=101)
        self.assertLessEqual(math.hypot(player.booster_vx, player.booster_vy), .78)
        self.assertGreater(player.x, .18)
        player.x, player.y = .95, .91
        player.booster_vx = player.booster_vy = 1
        room.level10_tick(.1, now=102)
        self.assertLessEqual(player.x, .96)
        self.assertLessEqual(player.y, .92)

    def test_stale_citadel_input_stops_without_firing(self):
        room, _, player = self.make_room()
        player.booster_x = 1
        player.super_spit_held = True
        player.super_spit_release_latched = True
        player.citadel_input_at = 100.0
        room.level10_tick(0.1, now=100.3)
        self.assertEqual((player.booster_x, player.booster_y), (0, 0))
        self.assertFalse(player.super_spit_held)
        self.assertFalse(player.super_spit_release_latched)

    def test_orb_charge_score_life_cap_and_no_duplicate(self):
        room, _, player = self.make_room(score=2410, player_id="orb-pilot")
        world = room.level10_worlds[player.player_id]
        world["orbs"] = {"orb": {"id": "orb", "x": player.x, "y": player.y, "collected": False}}
        player.lives, player.orbs_collected = 2, 49
        events = room.level10_tick(0, now=111)
        self.assertEqual((player.score, player.super_spit_charge, player.lives), (2420, 10, 3))
        self.assertTrue(world["orbs"]["orb"]["collected"])
        room.level10_tick(0, now=112)
        self.assertEqual(player.score, 2420)
        player.super_spit_charge = 100
        room.level10_tick(0, now=113)
        self.assertEqual(player.super_spit_charge, 100)

    def test_phase_thresholds_klaxon_and_deterministic_attacks(self):
        room, _, player = self.make_room()
        world = room.level10_worlds[player.player_id]
        for hp, phase in ((300, 1), (200, 2), (100, 3), (1, 3)):
            world["boss"]["hp"] = hp
            events = room.level10_tick(0, now=120 + hp)
            self.assertEqual(world["boss"]["phase"], phase)
            if phase > 1:
                self.assertTrue(any(e["event"] == "phase_klaxon" for e in events)
                                or any(k["phase"] == phase for k in world["klaxons"]))
        self.assertEqual(world["hazards"][0]["kind"], "grid_sweep")

    def test_shield_rejects_and_vulnerability_accepts_authoritative_projectile(self):
        room, _, player = self.make_room()
        world = room.level10_worlds[player.player_id]
        player.super_spit_charge = 25
        world["projectiles"] = [{"id": "p", "x": .5, "y": .42, "ttl": 1}]
        self.assertEqual(room.level10_tick(0, now=101), [])
        self.assertEqual(world["boss"]["hp"], 300)
        world["boss"]["shield_until"] = 0
        world["boss"]["vulnerable"] = True
        world["projectiles"] = [{"id": "p2", "x": .5, "y": .42, "ttl": 1}]
        events = room.level10_tick(0, now=102)
        self.assertEqual(world["boss"]["hp"], 275)
        self.assertTrue(any(e["event"] == "boss_hit" for e in events))

    def test_score_target_alone_cannot_victory_and_boss_death_once(self):
        room, _, player = self.make_room(score=2800)
        world = room.level10_worlds[player.player_id]
        self.assertFalse(room.level10_tick(0, now=101))
        self.assertFalse(player.victory_announced)
        world["boss"]["hp"] = 25; world["boss"]["shield_until"] = 0
        world["boss"]["vulnerable"] = True
        world["projectiles"] = [{"id": "final", "x": .5, "y": .42, "ttl": 1}]
        events = room.level10_tick(0, now=102)
        victory = [e for e in events if e["event"] == "victory"]
        self.assertEqual(len(victory), 1)
        self.assertTrue(victory[0]["event_metadata"]["citadel_complete"])
        self.assertEqual(victory[0]["cue"], "grand_fanfare")
        self.assertEqual(len([e for e in room.level10_tick(0, now=103) if e["event"] == "victory"]), 0)

    def test_snapshot_is_deep_and_omits_private_timing(self):
        room, _, player = self.make_room()
        snap = room.snapshot()
        snap["level10_worlds"][player.player_id]["boss"]["hp"] = 1
        self.assertEqual(room.level10_worlds[player.player_id]["boss"]["hp"], 300)
        self.assertNotIn("shield_until", snap["level10_worlds"][player.player_id]["boss"])
        self.assertNotIn("ownership_token", snap["players"][0])

    async def test_reset_and_timer_lifecycle(self):
        room, socket, player = self.make_room()
        player.game_over = True
        await room._reset(socket)
        self.assertEqual((player.current_level, player.score, player.super_spit_charge), (1, 0, 0))
        self.assertNotIn(player.player_id, room.level10_worlds)
        await room.start()
        self.assertIsNotNone(room.level10_timer_task)
        await room.stop()
        self.assertIsNone(room.level10_timer_task)


class ArcadeCosmeticContractTests(unittest.IsolatedAsyncioTestCase):
    OWNER = "cosmetic-owner-proof-that-is-at-least-32-characters"

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.directory.cleanup)
        self.storage = ArcadeStorage(Path(self.directory.name) / "arcade.sqlite3")
        self.stored = self.storage.register_player(
            "cosmetic-browser", self.OWNER, "Cosmetic Tester"
        )
        self.socket = FakeWebSocket()
        self.room = GameRoom(self.storage)
        self.player = Player(
            self.stored.player_id,
            self.stored.name,
            ownership_token=self.OWNER,
            owned_skin_ids=self.stored.owned_skin_ids,
            equipped_skin_id=self.stored.equipped_skin_id,
        )
        self.room.connections[self.socket] = self.player

    async def test_server_rejects_unknown_and_unowned_skin_ids(self):
        await self.room.handle_message(
            self.socket, {"type": "equip_skin", "skin_id": "unknown"}
        )
        self.assertEqual(self.player.equipped_skin_id, "default")
        self.assertEqual(self.socket.messages[-1]["type"], "error")

        await self.room.handle_message(
            self.socket, {"type": "equip_skin", "skin_id": "midnight"}
        )
        self.assertEqual(self.player.equipped_skin_id, "default")
        self.assertEqual(self.socket.messages[-1]["type"], "error")

    async def test_confirmed_owned_skin_equips_and_persists(self):
        self.player.owned_skin_ids = self.storage.grant_confirmed_skin(
            self.player.player_id, self.OWNER, "aurora"
        )
        await self.room.handle_message(
            self.socket, {"type": "equip_skin", "skin_id": "aurora"}
        )
        self.assertEqual(self.player.equipped_skin_id, "aurora")
        self.assertEqual(self.socket.messages[-1]["event"], "skin_equipped")
        restored = self.storage.register_player(
            "cosmetic-browser", self.OWNER, "Cosmetic Tester"
        )
        self.assertEqual(restored.equipped_skin_id, "aurora")

    def test_client_contract_has_safe_fallback_and_affordability_without_grant(self):
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        web3 = (GAME_DIR / "web3-onboarding.js").read_text(encoding="utf-8")
        for token in (
            'id="shopOverlayDialog"',
            'window.location.hash === "#prize-cabinet"',
            "SPRITE_STATE_TABLE[player.equipped_skin_id] || SPRITE_STATE_TABLE.default",
            'send({ type: "equip_skin", skin_id: item.skin_id })',
            "Live TLAMA settlement is disabled",
            "Cosmetic settlement remains preview-only",
            "@media (max-width: 600px)",
            "var(--safe-area-inset-bottom)",
        ):
            self.assertIn(token, source)
        self.assertIn("checkTokenAffordability", web3)
        self.assertNotIn(
            "await purchaseItem",
            source[source.index("const requestSkinPurchase"):source.index("const requestExtraLifePurchase")],
        )
        self.assertNotIn("grant", web3[web3.index("checkTokenAffordability"):])


class ArcadeExtraLifeContractTests(unittest.IsolatedAsyncioTestCase):
    OWNER = "extra-life-owner-proof-that-is-at-least-32-characters"

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addAsyncCleanup(asyncio.to_thread, self.directory.cleanup)
        self.storage = ArcadeStorage(Path(self.directory.name) / "arcade.sqlite3")
        stored = self.storage.register_player("extra-life-browser", self.OWNER, "Heart Tester")
        ticket = self.storage.issue_match_ticket(
            stored.player_id, self.OWNER, entry_fee_tlama=25
        )
        self.socket = FakeWebSocket()
        self.room = GameRoom(self.storage)
        self.player = Player(
            stored.player_id,
            stored.name,
            ownership_token=self.OWNER,
            ticket_id=ticket.ticket_id,
        )
        self.room.connections[self.socket] = self.player

    async def purchase(self, signature_character: str) -> None:
        await self.room.handle_message(
            self.socket,
            {
                "type": "purchase_extra_life",
                "item_id": "heart-matrix-upgrade",
                "signature": signature_character * 64,
                "wallet_address": "2" * 32,
                "oracle_quote": "q" * 80,
            },
        )

    async def test_upgrade_is_locked_until_normal_level_six_progression(self):
        with patch("backend._verify_extra_life_settlement_sync", return_value=True) as verifier:
            await self.purchase("1")
        self.assertEqual(self.player.lives, 3)
        verifier.assert_not_called()

        self.player.score = 800
        self.player.current_level = 5
        self.player.x, self.player.y = 0.22, 0.24
        await self.room._collect(self.socket, "orb-1")
        self.assertTrue(self.player.extra_life_unlocked)

    async def test_two_verified_purchases_raise_match_cap_from_three_to_five(self):
        self.player.extra_life_unlocked = True
        with patch("backend._verify_extra_life_settlement_sync", return_value=True):
            await self.purchase("1")
            await self.purchase("3")
            await self.purchase("4")
        self.assertEqual(self.player.extra_lives_purchased, 2)
        self.assertEqual((self.player.lives, self.player.max_lives), (5, 5))
        self.assertEqual(self.socket.messages[-1]["type"], "error")

    async def test_reset_clears_unlock_and_match_upgrade_limit(self):
        self.player.extra_life_unlocked = True
        self.player.extra_lives_purchased = 2
        self.player.max_lives = 5
        self.player.lives = 0
        self.player.game_over = True
        await self.room._reset(self.socket)
        self.assertEqual(
            (
                self.player.lives,
                self.player.max_lives,
                self.player.extra_lives_purchased,
                self.player.extra_life_unlocked,
            ),
            (3, 3, 0, False),
        )

    async def test_delayed_payment_verification_does_not_block_gameplay(self):
        self.player.extra_life_unlocked = True
        original_x = self.player.x
        with patch(
            "backend._verify_extra_life_settlement_sync",
            side_effect=lambda *_args: (__import__("time").sleep(0.15) or False),
        ):
            purchase = asyncio.create_task(self.purchase("1"))
            await asyncio.sleep(0.02)
            await asyncio.wait_for(
                self.room.handle_message(
                    self.socket,
                    {"type": "position", "x": original_x + 0.01, "y": self.player.y},
                ),
                timeout=0.08,
            )
            self.assertGreater(self.player.x, original_x)
            await purchase

    async def test_verifier_exception_releases_pending_purchase_reservation(self):
        self.player.extra_life_unlocked = True
        with patch(
            "backend._verify_extra_life_settlement_sync",
            side_effect=RuntimeError("malformed RPC response"),
        ):
            await self.purchase("1")
        self.assertNotIn(self.player.player_id, self.room.utility_purchase_pending)

        with patch("backend._verify_extra_life_settlement_sync", return_value=True):
            await self.purchase("3")
        self.assertEqual(self.player.extra_lives_purchased, 1)

    def test_cabinet_uses_server_unlock_price_and_max_state(self):
        source = (GAME_DIR / "index.html").read_text(encoding="utf-8")
        for token in (
            'player?.extra_life_unlocked',
            'item?.id === "heart-matrix-upgrade"',
            '"MAX"',
            'type: "purchase_extra_life"',
            "signature: result.signature",
        ):
            self.assertIn(token, source)

    def test_rpc_verifier_binds_exact_transfer_to_hook_group(self):
        wallet = "2" * 32
        source = "3" * 32
        vault = "4" * 32
        mint = "5" * 32
        hook = "6" * 32
        price = HEART_MATRIX_AMOUNT_TLAMA
        token_program = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
        transaction = {
            "blockTime": int(time.time()),
            "transaction": {
                "message": {
                    "accountKeys": [
                        {"pubkey": wallet, "signer": True},
                        {"pubkey": source, "signer": False},
                        {"pubkey": vault, "signer": False},
                        {"pubkey": hook, "signer": False},
                    ],
                    "instructions": [{
                        "programId": token_program,
                        "parsed": {
                            "type": "transferChecked",
                            "info": {
                                "source": source,
                                "destination": vault,
                                "mint": mint,
                                "authority": wallet,
                                "tokenAmount": {"amount": str(price), "decimals": 0},
                            },
                        },
                    }],
                },
            },
            "meta": {
                "err": None,
                "preTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(price * 2)}},
                    {"accountIndex": 2, "mint": mint, "owner": vault, "uiTokenAmount": {"amount": "0"}},
                ],
                "postTokenBalances": [
                    {"accountIndex": 1, "mint": mint, "owner": wallet, "uiTokenAmount": {"amount": str(price)}},
                    {"accountIndex": 2, "mint": mint, "owner": vault, "uiTokenAmount": {"amount": str(price)}},
                ],
                "innerInstructions": [{
                    "index": 0,
                    "instructions": [{"programId": hook, "stackHeight": 2}],
                }],
            },
        }

        class Response:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _limit):
                body = (
                    self.payload
                    if isinstance(self.payload, dict) and "parsed" in self.payload
                    else {"result": self.payload}
                )
                return json.dumps(body).encode()

        config = {
            "enabled": True,
            "rpc_url": "https://rpc.example",
            "mint_address": mint,
            "reward_vault_address": vault,
            "hook_program_address": hook,
            "decimals": 0,
            "oracle_url": "https://hermes.example",
            "oracle_feed_id": "ab" * 32,
            "oracle_exponent": -8,
            "oracle_max_age_seconds": 60,
            "oracle_max_confidence_bps": 100,
        }
        mint_account = {
            "value": {
                "owner": token_program,
                "data": {
                    "parsed": {
                        "info": {
                            "decimals": 0,
                            "extensions": [{
                                "extension": "transferHook",
                                "state": {"programId": hook},
                            }]
                        }
                    }
                },
            }
        }
        def rpc_response(request, **_kwargs):
            if request.data is None:
                return Response({"parsed": [{
                    "id": config["oracle_feed_id"],
                    "price": {
                        "price": "4000000",
                        "conf": "10000",
                        "expo": -8,
                        "publish_time": int(time.time()),
                    },
                }]})
            method = json.loads(request.data)["method"]
            return Response(mint_account if method == "getAccountInfo" else transaction)
        with patch.dict(os.environ, {"SESSION_SECRET": "s" * 32}):
            oracle_quote = _issue_oracle_quote(
                config,
                item_id="heart-matrix-upgrade",
                atomic_amount=price,
                publish_time=int(time.time()),
            )
            skin_quote = _issue_oracle_quote(
                config,
                item_id=CHARACTER_SKINS_ITEM_ID,
                atomic_amount=price,
                publish_time=int(time.time()),
            )
        with (
            patch("backend.payment_config", return_value=config),
            patch("backend.urllib.request.urlopen", side_effect=rpc_response),
            patch.dict(os.environ, {"SESSION_SECRET": "s" * 32}),
        ):
            with patch("backend.time.time", return_value=time.time() + 1_000):
                self.assertTrue(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )
                self.assertTrue(
                    _verify_catalog_settlement_sync(
                        "1" * 64,
                        wallet,
                        CHARACTER_SKINS_ITEM_ID,
                        skin_quote,
                    )
                )
                self.assertFalse(
                    _verify_catalog_settlement_sync(
                        "1" * 64,
                        wallet,
                        ARCADE_ENTRY_ITEM_ID,
                        skin_quote,
                    )
                )

            expired_on_chain = copy.deepcopy(transaction)
            expired_on_chain["blockTime"] += 1_000
            def expired_on_chain_response(request, **_kwargs):
                method = json.loads(request.data)["method"]
                return Response(
                    mint_account if method == "getAccountInfo" else expired_on_chain
                )
            with patch(
                "backend.urllib.request.urlopen",
                side_effect=expired_on_chain_response,
            ):
                self.assertFalse(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )

            sibling_hook = copy.deepcopy(transaction)
            sibling_hook["transaction"]["message"]["instructions"] = [{
                "programId": "8" * 32,
            }]
            sibling_transfer = copy.deepcopy(transaction["transaction"]["message"]["instructions"][0])
            sibling_transfer["stackHeight"] = 2
            sibling_hook["meta"]["innerInstructions"][0]["instructions"] = [
                sibling_transfer,
                {"programId": hook, "stackHeight": 2},
            ]
            def sibling_hook_response(request, **_kwargs):
                if request.data is None:
                    return rpc_response(request)
                method = json.loads(request.data)["method"]
                return Response(mint_account if method == "getAccountInfo" else sibling_hook)
            with patch(
                "backend.urllib.request.urlopen",
                side_effect=sibling_hook_response,
            ):
                self.assertFalse(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )

            over_debit = copy.deepcopy(transaction)
            over_debit["meta"]["postTokenBalances"][0]["uiTokenAmount"]["amount"] = str(price - 1)
            def over_debit_response(request, **_kwargs):
                if request.data is None:
                    return rpc_response(request)
                method = json.loads(request.data)["method"]
                return Response(mint_account if method == "getAccountInfo" else over_debit)
            with patch(
                "backend.urllib.request.urlopen",
                side_effect=over_debit_response,
            ):
                self.assertFalse(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )

            wrong_decimals = copy.deepcopy(mint_account)
            wrong_decimals["value"]["data"]["parsed"]["info"]["decimals"] = 9
            def wrong_decimals_response(request, **_kwargs):
                if request.data is None:
                    return rpc_response(request)
                method = json.loads(request.data)["method"]
                return Response(wrong_decimals if method == "getAccountInfo" else transaction)
            with patch(
                "backend.urllib.request.urlopen",
                side_effect=wrong_decimals_response,
            ):
                self.assertFalse(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )

            with patch(
                "backend.urllib.request.urlopen",
                side_effect=OSError("oracle unavailable"),
            ):
                self.assertFalse(
                    _verify_extra_life_settlement_sync("1" * 64, wallet, oracle_quote)
                )


if __name__ == "__main__":
    unittest.main()
