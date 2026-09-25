import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.analytics_outage_fixture import (
    ANALYTICS_FAILURE_MODES,
    analytics_outage_page,
)


GAME_PAGE = (
    Path(__file__).resolve().parents[1] / "llama_website" / "llama_game" / "index.html"
).as_uri()


class OpeningAnalyticsOutageTests(unittest.TestCase):
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

    def test_opening_gameplay_continues_during_analytics_outages(self) -> None:
        def state(*, score: int, lives: int, orbs: int, level: int) -> dict:
            return {
                "type": "state",
                "players": [
                    {
                        "player_id": "opening-player",
                        "name": "Opening player",
                        "score": score,
                        "lives": lives,
                        "orbs_collected": orbs,
                        "current_level": level,
                        "x": 0.5,
                        "y": 0.5,
                        "stunned": False,
                    }
                ],
                "collectibles": {},
                "leaderboard": [],
            }

        messages = [
            {
                "type": "welcome",
                "player_id": "opening-player",
                "simulated_entry_fee_tlama": 25,
            },
            state(score=20, lives=5, orbs=2, level=1),
            {
                "type": "event",
                "event": "life_lost",
                "player_id": "opening-player",
                "hazard_kind": "puma",
                "duration_ms": 1000,
            },
            state(score=20, lives=4, orbs=2, level=1),
            {
                "type": "event",
                "event": "orb_collected",
                "player_id": "opening-player",
                "score": 100,
                "current_level": 1,
            },
            state(score=100, lives=4, orbs=10, level=1),
            {
                "type": "event",
                "event": "level_up",
                "player_id": "opening-player",
                "score": 110,
                "current_level": 2,
            },
            state(score=110, lives=4, orbs=11, level=2),
        ]

        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), analytics_outage_page(
                self.browser, failure_mode
            ) as outage:
                outage.page.goto(GAME_PAGE, wait_until="domcontentloaded")
                outage.page.evaluate(
                """async (messages) => {
                    messages.forEach((message) => window.__socket.message(message));
                    await new Promise((resolve) => setTimeout(resolve, 0));
                }""",
                messages,
            )

                page = outage.page
                self.assertEqual(page.locator("#score").text_content(), "110")
                self.assertEqual(page.locator("#lives").text_content(), "4")
                self.assertEqual(page.locator("#orbs").text_content(), "11")
                self.assertEqual(page.locator("#currentLevel").text_content(), "2")
                self.assertEqual(page.locator("#levelName").text_content(), "Electric Woodlands")
                self.assertEqual(page.locator("#progressPercent").text_content(), "6%")
                self.assertEqual(
                    page.locator("#progressFill").get_attribute("style"), "width: 6%;"
                )
                self.assertEqual(
                    page.locator("#progressLabel").text_content(), "Next level: 251 points"
                )
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "arcade_joined",
                        "chicken_collision",
                        "first_orb_collected",
                        "level_2_reached",
                    ],
                )
                self.assertEqual(outage.errors, [])


if __name__ == "__main__":
    unittest.main()