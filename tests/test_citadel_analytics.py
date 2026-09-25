import json
import os
import re
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.analytics_outage_fixture import (
    ANALYTICS_FAILURE_MODES,
    analytics_outage_page,
)


INDEX = Path("llama_website/llama_game/index.html")
GAME_PAGE = (Path(__file__).resolve().parents[1] / INDEX).as_uri()
CITADEL_EVENTS = [
    "citadel_level_entered",
    "citadel_super_spit_released",
    "citadel_boss_hit",
    "citadel_completed",
]


class CitadelAnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INDEX.read_text(encoding="utf-8")

    def test_privacy_safe_funnel_events_are_present(self):
        for event_name in (
            "citadel_level_entered",
            "citadel_super_spit_released",
            "citadel_boss_hit",
            "citadel_completed",
        ):
            self.assertIn(f'"{event_name}"', self.source)

        helper = re.search(
            r"const trackCitadelMilestone = \(name, phase\) => \{(.*?)\n        \};",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(helper)
        body = helper.group(1)
        analytics_data = re.search(r"trackEvent\(name, \{(.*?)\}\);", body, re.DOTALL)
        self.assertIsNotNone(analytics_data)
        fields = re.findall(r"^\s*(\w+):", analytics_data.group(1), re.MULTILINE)
        self.assertEqual(
            fields,
            ["encounter_version", "control_type", "boss_phase"],
        )
        self.assertIn(
            "encounter_version: CITADEL_ENCOUNTER_VERSION",
            analytics_data.group(1),
        )
        self.assertIn("control_type: citadelControlType", analytics_data.group(1))
        self.assertIn("boss_phase: safeBossPhase(phase)", analytics_data.group(1))
        self.assertNotRegex(body, r"player_id|nickname|wallet|address|free.form")

    def test_encounter_version_is_fixed_and_documents_when_to_change(self):
        self.assertIn(
            "Bump only when Level 10 difficulty or controls materially change.",
            self.source,
        )
        self.assertRegex(
            self.source,
            r'const CITADEL_ENCOUNTER_VERSION = "citadel_v1";',
        )

    def test_funnel_is_once_per_match_and_analytics_is_a_safe_noop(self):
        track_event = re.search(
            r"const trackEvent = \(name, data\) => \{(.*?)\n        \};",
            self.source,
            re.DOTALL,
        )
        self.assertIsNotNone(track_event)
        self.assertIn("if (adminTestSession) return;", track_event.group(1))
        self.assertEqual(self.source.count("if (adminTestSession) return;"), 3)
        self.assertIn("if (citadelMilestones.has(name)) return;", self.source)
        self.assertIn("citadelMilestones.clear();", self.source)
        self.assertIn("openingMilestones.clear();", self.source)
        self.assertRegex(
            self.source,
            r'(?s)message\.event === "admin_level_selected".*?adminTestSession = true;',
        )
        self.assertRegex(
            self.source,
            r'(?s)message\.event === "game_reset".*?adminTestSession = false;',
        )
        self.assertIn("const tracking = window.umami?.track(name, data);", self.source)
        self.assertIn("Promise.resolve(tracking).catch(() => {", self.source)
        self.assertRegex(
            self.source,
            r"(?s)const trackEvent = \(name, data\) => \{\s*"
            r"if \(adminTestSession\) return;\s*try \{.*?\}\s*catch \{",
        )

    def test_admin_confirmation_precedes_level_state_without_private_data(self):
        backend = Path("llama_website/llama_game/backend.py").read_text(encoding="utf-8")
        confirmation = backend.index('"event": "admin_level_selected"')
        state_broadcast = backend.index(
            'await self.broadcast({"type": "state", **self.snapshot(now)})',
            confirmation,
        )
        self.assertLess(confirmation, state_broadcast)

        event_body = backend[confirmation:state_broadcast]
        self.assertNotRegex(
            event_body,
            r"player_id|nickname|wallet|address|password|token|credential",
        )


class CitadelAnalyticsRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/repl/tools/bin/chromium"),
            headless=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_admin_run_is_suppressed_and_player_milestones_emit_once(self):
        context = self.browser.new_context()
        page = context.new_page()
        page.add_init_script(
            script="""(() => {
                window.__events = [];
                class TestWebSocket {
                    static CONNECTING = 0;
                    static OPEN = 1;
                    constructor() {
                        this.readyState = TestWebSocket.OPEN;
                        this.listeners = {};
                        window.__socket = this;
                    }
                    addEventListener(name, callback) {
                        (this.listeners[name] ||= []).push(callback);
                    }
                    send() {}
                    message(payload) {
                        (this.listeners.message || []).forEach((callback) =>
                            callback({ data: JSON.stringify(payload) })
                        );
                    }
                }
                window.WebSocket = TestWebSocket;
                window.umami = {
                    track(name, data) {
                        window.__events.push({ name, data: data || null });
                    },
                };
            })();"""
        )
        page.route("**/config.js", lambda route: route.fulfill(body=""))
        page.route("**/web3-onboarding.js", lambda route: route.fulfill(body=""))

        def player_state(level):
            return {
                "type": "state",
                "players": [
                    {
                        "player_id": "private-player-id",
                        "name": "Private nickname",
                        "score": 2500,
                        "lives": 3,
                        "orbs_collected": 0,
                        "current_level": level,
                        "x": 0.5,
                        "y": 0.5,
                        "stunned": False,
                    }
                ],
                "collectibles": {},
                "level10_worlds": {
                    "private-player-id": {"boss": {"hp": 200, "phase": 2}}
                },
                "leaderboard": [],
            }

        milestone_events = [
            {
                "type": "event",
                "event": "super_spit",
                "player_id": "private-player-id",
                "phase": 2,
            },
            {
                "type": "event",
                "event": "boss_hit",
                "player_id": "private-player-id",
                "phase": 2,
            },
            {
                "type": "event",
                "event": "victory",
                "player_id": "private-player-id",
                "event_metadata": {"citadel_complete": True, "boss_phase": 3},
            },
        ]

        try:
            page.goto(GAME_PAGE, wait_until="domcontentloaded")
            page.evaluate(
                """([adminState, milestoneEvents]) => {
                    window.__socket.message({
                        type: "welcome",
                        player_id: "private-player-id",
                        simulated_entry_fee_tlama: 25,
                    });
                    window.__socket.message({
                        type: "event",
                        event: "admin_level_selected",
                    });
                    window.__socket.message(adminState);
                    milestoneEvents.forEach((event) => window.__socket.message(event));
                }""",
                [player_state(10), milestone_events],
            )
            self.assertEqual(
                page.evaluate(
                    "(names) => window.__events.filter((event) => names.includes(event.name))",
                    CITADEL_EVENTS,
                ),
                [],
            )

            page.evaluate(
                """([levelNineState, levelTenState, milestoneEvents]) => {
                    window.__socket.message({
                        type: "event",
                        event: "game_reset",
                        player_id: "private-player-id",
                    });
                    window.__socket.message(levelNineState);
                    window.__socket.message(levelTenState);
                    milestoneEvents.forEach((event) => window.__socket.message(event));
                    window.__socket.message(levelTenState);
                    milestoneEvents.forEach((event) => window.__socket.message(event));
                }""",
                [player_state(9), player_state(10), milestone_events],
            )

            citadel_events = page.evaluate(
                "(names) => window.__events.filter((event) => names.includes(event.name))",
                CITADEL_EVENTS,
            )
            self.assertEqual(
                [event["name"] for event in citadel_events],
                CITADEL_EVENTS,
            )
            self.assertEqual(
                [event["data"]["boss_phase"] for event in citadel_events],
                [2, 2, 2, 3],
            )
            self.assertTrue(
                all(
                    event["data"]["encounter_version"] == "citadel_v1"
                    and event["data"]["control_type"] == "unknown"
                    for event in citadel_events
                )
            )
            self.assertNotIn("private-player-id", json.dumps(citadel_events))
            self.assertNotIn("Private nickname", json.dumps(citadel_events))
        finally:
            context.close()

    def test_citadel_gameplay_continues_during_analytics_outages(self):
        level_ten_state = {
            "type": "state",
            "players": [
                {
                    "player_id": "citadel-player",
                    "name": "Citadel player",
                    "score": 2600,
                    "lives": 3,
                    "orbs_collected": 0,
                    "current_level": 10,
                    "x": 0.5,
                    "y": 0.5,
                    "stunned": False,
                    "super_spit_charge": 100,
                }
            ],
            "collectibles": {},
            "level10_worlds": {
                "citadel-player": {"boss": {"hp": 175, "phase": 2}}
            },
            "leaderboard": [],
            "victory_score": 2800,
        }
        milestone_events = [
            {
                "type": "event",
                "event": "super_spit",
                "player_id": "citadel-player",
                "phase": 2,
            },
            {
                "type": "event",
                "event": "boss_hit",
                "player_id": "citadel-player",
                "phase": 2,
            },
            {
                "type": "event",
                "event": "victory",
                "player_id": "citadel-player",
                "event_metadata": {"citadel_complete": True, "boss_phase": 3},
            },
        ]

        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), analytics_outage_page(
                self.browser, failure_mode
            ) as outage:
                outage.page.goto(GAME_PAGE, wait_until="domcontentloaded")
                outage.page.evaluate(
                """async ([state, events]) => {
                    window.__socket.message({
                        type: "welcome",
                        player_id: "citadel-player",
                        simulated_entry_fee_tlama: 25,
                    });
                    window.__socket.message(state);
                    events.forEach((event) => window.__socket.message(event));
                    await new Promise((resolve) => setTimeout(resolve, 0));
                }""",
                [level_ten_state, milestone_events],
            )

                page = outage.page
                self.assertEqual(page.locator("#currentLevel").text_content(), "10")
                self.assertEqual(
                    page.locator("#levelName").text_content(),
                    "Trust Llama Citadel Core",
                )
                self.assertEqual(page.locator("#superCharge").text_content(), "100%")
                self.assertIn(
                    "Warden HP 175/300 · Phase 2",
                    page.locator("#progressLabel").text_content(),
                )
                self.assertTrue(page.locator("#victoryBanner").evaluate(
                    "(element) => element.classList.contains('visible')"
                ))
                self.assertEqual(
                    page.evaluate(
                        "(names) => window.__analyticsAttempts.filter((name) => names.includes(name))",
                        CITADEL_EVENTS,
                    ),
                    CITADEL_EVENTS,
                )
                self.assertEqual(outage.errors, [])


if __name__ == "__main__":
    unittest.main()
