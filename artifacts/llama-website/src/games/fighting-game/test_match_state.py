from __future__ import annotations

import asyncio
import time
import unittest

from entry_rail import FightingGameEntryRail
from match_state import MAX_HEALTH, MatchManager, MatchStateError


class FightingGameBackendTests(unittest.TestCase):
    def test_layer3_entry_target_and_anti_whale_preflight(self) -> None:
        entry = FightingGameEntryRail(token_price_usd="0.01").validate()
        self.assertTrue(entry.accepted)
        self.assertEqual(entry.target_usd, "0.25")
        self.assertEqual(entry.amount_tlama, 25)
        self.assertEqual(entry.amount_atomic, 25_000_000_000)

    def test_two_players_select_characters_and_start_round(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, first = await manager.create_match(display_name="First")
            match_id = state["match_id"]
            state, second = await manager.join_match(
                match_id=match_id, display_name="Second"
            )
            self.assertEqual(state["status"], "character_select")
            await manager.select_character(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                character_id="andean-guardian",
            )
            state = await manager.select_character(
                match_id=match_id,
                player_id=second["player_id"],
                session_token=second["session_token"],
                character_id="neon-puma",
            )
            self.assertEqual(state["status"], "active")
            self.assertEqual(state["round_seconds_remaining"], 99)

        asyncio.run(scenario())

    def test_server_owns_damage_combo_and_health(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, first = await manager.create_match(display_name="First")
            match_id = state["match_id"]
            _, second = await manager.join_match(
                match_id=match_id, display_name="Second"
            )
            for session, character in (
                (first, "andean-guardian"),
                (second, "quantum-llama"),
            ):
                await manager.select_character(
                    match_id=match_id,
                    player_id=session["player_id"],
                    session_token=session["session_token"],
                    character_id=character,
                )
            started = time.monotonic()
            for step in range(1, 36):
                await manager.player_input(
                    match_id=match_id,
                    player_id=first["player_id"],
                    session_token=first["session_token"],
                    action="move_right",
                    now=started + step * 0.05,
                )
                await manager.tick_match(
                    match_id,
                    now=started + step * 0.05,
                )
            state = await manager.combat_action(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                action="heavy",
                now=started + 2.0,
            )
            self.assertEqual(
                state["players"]["player_2"]["health"], MAX_HEALTH - 11
            )
            self.assertEqual(state["players"]["player_1"]["combo"]["hits"], 1)
            with self.assertRaisesRegex(MatchStateError, "cooling down"):
                await manager.combat_action(
                    match_id=match_id,
                    player_id=first["player_id"],
                    session_token=first["session_token"],
                    action="heavy",
                    now=started + 2.1,
                )

        asyncio.run(scenario())

    def test_ai_match_owns_movement_hitboxes_and_computer_state(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, first = await manager.create_ai_match(display_name="First")
            match_id = state["match_id"]
            self.assertTrue(state["players"]["player_2"]["is_computer"])
            self.assertEqual(state["status"], "active")
            state = await manager.player_input(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                action="light_punch",
                now=10.0,
            )
            self.assertEqual(state["players"]["player_2"]["health"], MAX_HEALTH)
            started = time.monotonic()
            for step in range(1, 36):
                state = await manager.player_input(
                    match_id=match_id,
                    player_id=first["player_id"],
                    session_token=first["session_token"],
                    action="move_right",
                    now=started + step * 0.05,
                )
                state = await manager.tick_match(
                    match_id,
                    now=started + step * 0.05,
                )
            state = await manager.player_input(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                action="heavy_kick",
                now=started + 2.0,
            )
            self.assertLess(state["players"]["player_2"]["health"], MAX_HEALTH)

        asyncio.run(scenario())

    def test_movement_distance_is_not_amplified_by_input_flooding(self) -> None:
        async def scenario() -> None:
            managers: list[tuple[MatchManager, str, dict[str, str]]] = []
            for _ in range(2):
                manager = MatchManager(FightingGameEntryRail("0.01"))
                state, first = await manager.create_ai_match(
                    display_name="First"
                )
                managers.append((manager, state["match_id"], first))
            started = time.monotonic()
            slow_manager, slow_match, slow_player = managers[0]
            fast_manager, fast_match, fast_player = managers[1]
            await slow_manager.player_input(
                match_id=slow_match,
                player_id=slow_player["player_id"],
                session_token=slow_player["session_token"],
                action="move_right",
                now=started,
            )
            for _ in range(100):
                await fast_manager.player_input(
                    match_id=fast_match,
                    player_id=fast_player["player_id"],
                    session_token=fast_player["session_token"],
                    action="move_right",
                    now=started,
                )
            slow_state = await slow_manager.tick_match(
                slow_match, now=started + 0.05
            )
            fast_state = await fast_manager.tick_match(
                fast_match, now=started + 0.05
            )
            self.assertAlmostEqual(
                slow_state["players"]["player_1"]["position_x"],
                fast_state["players"]["player_1"]["position_x"],
                delta=0.1,
            )

        asyncio.run(scenario())

    def test_abandoned_waiting_match_is_removed(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            await manager.create_match(display_name="First")
            self.assertEqual(await manager.match_count(), 1)
            removed = await manager.cleanup_stale(
                now=time.monotonic() + 121,
            )
            self.assertEqual(len(removed), 1)
            self.assertEqual(await manager.match_count(), 0)

        asyncio.run(scenario())

    def test_human_match_tick_integrates_movement_and_jump(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, first = await manager.create_match(display_name="First")
            match_id = state["match_id"]
            _, second = await manager.join_match(
                match_id=match_id, display_name="Second"
            )
            for session, character in (
                (first, "andean-guardian"),
                (second, "neon-puma"),
            ):
                await manager.select_character(
                    match_id=match_id,
                    player_id=session["player_id"],
                    session_token=session["session_token"],
                    character_id=character,
                )
            started = time.monotonic()
            await manager.player_input(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                action="move_right",
                now=started,
            )
            moved = await manager.tick_match(
                match_id, now=started + 0.05
            )
            self.assertGreater(
                moved["players"]["player_1"]["position_x"],
                220,
            )
            await manager.player_input(
                match_id=match_id,
                player_id=first["player_id"],
                session_token=first["session_token"],
                action="jump",
                now=started + 0.06,
            )
            airborne = await manager.tick_match(
                match_id, now=started + 0.11
            )
            self.assertGreater(
                airborne["players"]["player_1"]["position_y"],
                0,
            )

        asyncio.run(scenario())

    def test_ai_loop_survives_mixed_attack_cooldowns(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, _ = await manager.create_ai_match(display_name="First")
            match_id = state["match_id"]
            started = time.monotonic()
            for step in range(1, 161):
                state = await manager.tick_match(
                    match_id,
                    now=started + step * 0.05,
                )
            self.assertLess(state["players"]["player_1"]["health"], MAX_HEALTH)
            self.assertGreater(
                state["players"]["player_2"]["combo"]["total_damage"],
                0,
            )

        asyncio.run(scenario())

    def test_live_or_completed_round_cannot_be_restarted_by_selection(self) -> None:
        async def scenario() -> None:
            manager = MatchManager(FightingGameEntryRail("0.01"))
            state, first = await manager.create_match(display_name="First")
            match_id = state["match_id"]
            _, second = await manager.join_match(
                match_id=match_id, display_name="Second"
            )
            for session, character in (
                (first, "andean-guardian"),
                (second, "neon-puma"),
            ):
                state = await manager.select_character(
                    match_id=match_id,
                    player_id=session["player_id"],
                    session_token=session["session_token"],
                    character_id=character,
                )
            started_at = state["round_seconds_remaining"]
            with self.assertRaisesRegex(MatchStateError, "selection is closed"):
                await manager.select_character(
                    match_id=match_id,
                    player_id=first["player_id"],
                    session_token=first["session_token"],
                    character_id="quantum-llama",
                )
            state = await manager.expire_round(
                match_id,
                now=10**12,
            )
            self.assertEqual(state["status"], "round_complete")
            self.assertEqual(started_at, 99)
            with self.assertRaisesRegex(MatchStateError, "selection is closed"):
                await manager.select_character(
                    match_id=match_id,
                    player_id=second["player_id"],
                    session_token=second["session_token"],
                    character_id="quantum-llama",
                )

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()