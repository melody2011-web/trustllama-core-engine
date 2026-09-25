from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Sequence
import unittest

from playwright.sync_api import sync_playwright

from tests.analytics_outage_fixture import (
    ANALYTICS_FAILURE_MODES,
    PurchaseOutcome,
    analytics_outage_page,
    install_purchase_behavior_script,
)


GAME_PAGE = (
    Path(__file__).resolve().parents[1] / "llama_website" / "llama_game" / "index.html"
).as_uri()
GAME_README = (
    Path(__file__).resolve().parents[1] / "llama_website" / "llama_game" / "README.md"
)

TEST_CONFIG = {
    "enabled": True,
    "mint_address": "11111111111111111111111111111111",
    "rpc_url": "https://rpc.invalid",
    "decimals": 9,
    "game_economy": {
        "settlement_mode": "audited-transfer-hook-required",
        "reward_vault_percent": 100,
        "reward_vault_address": "VaultAddressMustNeverReachAnalytics",
        "reward_vault_label": "Game Rewards Vault",
        "hook_program_address": "HookAddressMustNeverReachAnalytics",
        "catalog": [
            {
                "id": "entry-ticket",
                "label": "Arcade Entry",
                "kind": "entry",
                "amount_tlama": 10,
            },
            {
                "id": "vip-entry-ticket",
                "label": "VIP Arcade Entry",
                "kind": "entry",
                "amount_tlama": 25,
            },
        ],
    },
}


class ArcadeOpeningWarningContractTests(unittest.TestCase):
    def test_completion_warning_rules_remain_explicit_and_separate(self) -> None:
        readme = GAME_README.read_text(encoding="utf-8")
        warning_section = readme.split("### Completion-rate warnings", 1)[1].split(
            "## Finalized tokenomics snapshot", 1
        )[0]
        normalized_warning_section = " ".join(warning_section.split())

        required_rules = {
            "both months require the minimum sample": (
                "Use **50 joined sessions per opening version per UTC calendar "
                "month** as the default minimum sample",
                "trigger a warning only when both months meet the minimum"
            ),
            "only adjacent completed UTC months are compared": (
                "Compare a completed UTC calendar month only with the immediately "
                "preceding completed month"
            ),
            "first-orb warning is independent": (
                "Evaluate the two completion rates independently",
                "Warn for **join → first orb** when its rate drops by at least **10 "
                "percentage points** from the preceding month",
            ),
            "Level-2 warning is independent": (
                "Warn for **join → Level 2** when its rate drops by at least **10 "
                "percentage points** from the preceding month"
            ),
            "UTC months and opening versions are separate cohorts": (
                "Group counts by both `toStartOfMonth(created_at)` and "
                "`opening_version`",
                "never deduplicate sessions or calculate a completion rate across "
                "calendar-month boundaries",
                "Never compare one opening version with another",
            ),
            "chicken collision cannot affect a completion warning": (
                "never use it to trigger, suppress, or change either completion warning"
            ),
            "sparse cohorts are explicitly inconclusive": (
                "A sparse comparison should be reported as `insufficient sample`, "
                "not as healthy or regressed"
            ),
        }

        for rule, expected_text in required_rules.items():
            with self.subTest(rule=rule):
                snippets = (
                    expected_text
                    if isinstance(expected_text, tuple)
                    else (expected_text,)
                )
                for snippet in snippets:
                    self.assertIn(" ".join(snippet.split()), normalized_warning_section)


class ArcadeAnalyticsTests(unittest.TestCase):
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

    def open_page(
        self,
        *,
        analytics_throws: bool = False,
        purchase_outcomes: Sequence[PurchaseOutcome] = (),
        operator_enabled: bool = False,
    ):
        context = self.browser.new_context()
        page = context.new_page()
        init_script = """(() => {
                const [config, analyticsThrows, purchaseOutcomes] = __ARGS__;
                window.__events = [];
                window.__trackAttempts = [];
                window.__connectCalls = 0;
                window.__socket = null;
                window.__sockets = [];
                class TestWebSocket {
                    static CONNECTING = 0;
                    static OPEN = 1;
                    static CLOSED = 3;
                    constructor() {
                        this.readyState = TestWebSocket.OPEN;
                        this.listeners = {};
                        window.__socket = this;
                        window.__sockets.push(this);
                        queueMicrotask(() => this.emit("open", {}));
                    }
                    addEventListener(name, callback) {
                        (this.listeners[name] ||= []).push(callback);
                    }
                    send() {}
                    close(code = 1000, reason = "") {
                        this.readyState = TestWebSocket.CLOSED;
                        this.emit("close", { code, reason });
                    }
                    emit(name, payload) {
                        (this.listeners[name] || []).forEach((callback) => callback(payload));
                    }
                    message(payload) {
                        this.emit("message", { data: JSON.stringify(payload) });
                    }
                }
                window.WebSocket = TestWebSocket;
                window.TLAMA_PAYMENT_CONFIG = config;
                window.umami = {
                    track(name, data) {
                        window.__trackAttempts.push({ name, data: data || null });
                        if (analyticsThrows) throw new Error("analytics unavailable");
                        window.__events.push({ name, data: data || null });
                    },
                };
                window.TLAMA_WEB3 = {
                    getWallet() { return null; },
                    async connect() {
                        window.__connectCalls += 1;
                        const wallet = {
                            publicKey: {
                                toString: () => "WalletAddressMustNeverReachAnalytics",
                            },
                        };
                        window.dispatchEvent(new CustomEvent("tlama-wallet-connected"));
                        return wallet;
                    },
                };
                __PURCHASE_BEHAVIOR_SCRIPT__
            })();"""
        page.add_init_script(
            script=init_script.replace(
                "__ARGS__",
                json.dumps(
                    [
                        TEST_CONFIG,
                        analytics_throws,
                        [asdict(outcome) for outcome in purchase_outcomes],
                    ]
                ),
            ).replace(
                "__PURCHASE_BEHAVIOR_SCRIPT__", install_purchase_behavior_script()
            )
        )
        page.route("**/config.js", lambda route: route.fulfill(body=""))
        page.route("**/web3-onboarding.js", lambda route: route.fulfill(body=""))
        if operator_enabled:
            page.route(
                "**/admin/status",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"enabled":true}',
                ),
            )
            page.route(
                "**/admin/auth",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/json",
                    body='{"ok":true}',
                ),
            )
        page.goto(GAME_PAGE, wait_until="domcontentloaded")
        page.locator("#shop button").first.wait_for()
        return context, page

    def events(self, page):
        return page.evaluate("window.__events")

    def outage_page(
        self,
        failure_mode,
        *,
        purchase_outcomes,
    ):
        return analytics_outage_page(
            self.browser,
            failure_mode,
            payment_config=TEST_CONFIG,
            purchase_outcomes=purchase_outcomes,
        )

    def assert_safe_events(self, events) -> None:
        allowed_names = {
            "wallet_dialog_opened",
            "wallet_dialog_cancelled",
            "wallet_option_selected",
            "wallet_connected",
            "purchase_review_opened",
            "purchase_cancelled",
            "purchase_wallet_handoff",
            "purchase_failed",
            "purchase_confirmed",
            "arcade_joined",
            "first_orb_collected",
            "chicken_collision",
            "level_2_reached",
        }
        allowed_fields = {"adapter", "location", "item_kind", "opening_version"}
        allowed_values = {
            None,
            "phantom",
            "solflare",
            "coinbase",
            "unknown",
            "dialog",
            "web3_panel",
            "entry",
            "cosmetic",
            "other",
            "andean_v3",
        }
        for event in events:
            self.assertIn(event["name"], allowed_names)
            payload = event["data"] or {}
            self.assertLessEqual(set(payload), allowed_fields)
            self.assertTrue(
                all(value in allowed_values for value in payload.values()),
                f"free-form analytics value found in {event}",
            )

    def test_authenticated_operator_session_suppresses_every_analytics_caller(self) -> None:
        context, page = self.open_page(operator_enabled=True)
        try:
            page.locator("#adminPortalButton").click()
            page.locator("#adminPassword").fill("must-not-reach-analytics")
            page.locator("#adminPortalSubmit").click()
            page.locator("#adminPortalDialog").wait_for(state="hidden")

            page.evaluate(
                """() => {
                    window.__socket.message({
                        type: "welcome",
                        player_id: "private-operator-player-id",
                        simulated_entry_fee_tlama: 25,
                    });
                    window.__socket.message({
                        type: "event",
                        event: "orb_collected",
                        player_id: "private-operator-player-id",
                        score: 10,
                        current_level: 1,
                    });
                }"""
            )
            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-close-dialog]').click()
            page.locator("#shop button").first.click()
            page.locator("#cancelPurchaseButton").click()

            self.assertEqual(page.evaluate("window.__trackAttempts"), [])

            page.evaluate(
                """() => {
                    window.__socket.message({
                        type: "event",
                        event: "game_reset",
                        player_id: "private-operator-player-id",
                    });
                }"""
            )
            page.locator("#walletButton").click()
            self.assertEqual(
                [event["name"] for event in page.evaluate("window.__trackAttempts")],
                ["wallet_dialog_opened"],
            )
        finally:
            context.close()

    def test_all_analytics_emissions_use_the_operator_guarded_boundary(self) -> None:
        source = (
            Path(__file__).resolve().parents[1]
            / "llama_website"
            / "llama_game"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertEqual(source.count("window.umami?.track("), 1)
        boundary = source.split("const trackEvent = (name, data) => {", 1)[1].split(
            "};", 1
        )[0]
        self.assertIn("if (adminTestSession) return;", boundary)

    def test_wallet_open_selection_cancellation_and_connection_emit_once(self) -> None:
        context, page = self.open_page()
        try:
            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-close-dialog]').click()
            page.wait_for_function("window.__events.length === 2")
            self.assertEqual(
                [event["name"] for event in self.events(page)],
                ["wallet_dialog_opened", "wallet_dialog_cancelled"],
            )

            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-dialog-wallet="phantom"]').click()
            page.locator("#walletDialog").wait_for(state="hidden")
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "wallet_dialog_opened",
                    "wallet_option_selected",
                    "wallet_connected",
                ],
            )
            self.assertEqual(
                events[-2]["data"],
                {"adapter": "phantom", "location": "dialog"},
            )
            self.assertEqual(events[-1]["data"], {"adapter": "phantom"})
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_first_level_funnel_events_are_anonymous_and_emit_once(self) -> None:
        context, page = self.open_page()
        try:
            messages = [
                {
                    "type": "welcome",
                    "player_id": "private-player-id",
                    "simulated_entry_fee_tlama": 25,
                },
                {
                    "type": "event",
                    "event": "orb_collected",
                    "player_id": "private-player-id",
                    "score": 10,
                    "current_level": 1,
                },
                {
                    "type": "event",
                    "event": "hazard_stun",
                    "player_id": "private-player-id",
                    "hazard_kind": "puma",
                    "duration_ms": 1000,
                },
                {
                    "type": "event",
                    "event": "level_up",
                    "player_id": "private-player-id",
                    "score": 110,
                    "current_level": 2,
                },
            ]
            page.evaluate(
                """(messages) => {
                    messages.forEach((message) => window.__socket.message(message));
                    messages.forEach((message) => window.__socket.message(message));
                }""",
                messages,
            )

            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "arcade_joined",
                    "first_orb_collected",
                    "chicken_collision",
                    "level_2_reached",
                ],
            )
            self.assertTrue(
                all(
                    event["data"] == {"opening_version": "andean_v3"}
                    for event in events
                )
            )
            self.assertNotIn("private-player-id", json.dumps(events))
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_funnel_analytics_exceptions_do_not_interrupt_game_state(self) -> None:
        context, page = self.open_page(analytics_throws=True)
        try:
            page.evaluate(
                """() => {
                    window.__socket.message({
                        type: "welcome",
                        player_id: "private-player-id",
                        simulated_entry_fee_tlama: 25,
                    });
                    window.__socket.message({
                        type: "event",
                        event: "level_up",
                        player_id: "private-player-id",
                        score: 110,
                        current_level: 2,
                        players: [{
                            player_id: "private-player-id",
                            name: "Hidden nickname",
                            score: 110,
                            current_level: 2,
                            x: 0.5,
                            y: 0.5,
                            stunned: false,
                        }],
                        collectibles: {},
                    });
                }"""
            )

            self.assertEqual(page.locator("#currentLevel").text_content(), "2")
            self.assertEqual(page.locator("#score").text_content(), "110")
            self.assertEqual(
                [event["name"] for event in page.evaluate("window.__trackAttempts")],
                ["arcade_joined", "level_2_reached"],
            )
        finally:
            context.close()

    def test_purchase_cancel_handoff_failure_and_confirmation_emit_once(self) -> None:
        context, page = self.open_page(
            purchase_outcomes=[
                PurchaseOutcome.successful(
                    {"signature": "SignatureMustNeverReachAnalytics"}
                )
            ]
        )
        try:
            page.locator("#shop button").first.click()
            page.locator("#cancelPurchaseButton").click()
            page.locator("#purchaseDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.ready").wait_for()
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "purchase_review_opened",
                    "purchase_cancelled",
                    "purchase_review_opened",
                    "purchase_wallet_handoff",
                    "wallet_connected",
                    "purchase_confirmed",
                ],
            )
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_completed_entry_purchase_cannot_be_submitted_again_immediately(
        self,
    ) -> None:
        context, page = self.open_page(
            purchase_outcomes=[
                PurchaseOutcome.successful({"signature": "confirmed-signature"})
            ]
        )
        try:
            shop_button = page.locator("#shop button").first
            confirm_button = page.locator("#confirmPurchaseButton")

            shop_button.click()
            confirm_button.click()
            page.locator("#paymentNotice.ready").wait_for()

            shop_button.evaluate("(button) => { button.click(); button.click(); }")
            confirm_button.evaluate("(button) => { button.click(); button.click(); }")

            self.assertTrue(shop_button.is_disabled())
            self.assertTrue(confirm_button.is_disabled())
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assertEqual(page.evaluate("window.__confirmedPurchaseOutcomes"), 1)
            self.assertEqual(
                [event["name"] for event in self.events(page)],
                [
                    "purchase_review_opened",
                    "purchase_wallet_handoff",
                    "wallet_connected",
                    "purchase_confirmed",
                ],
            )
        finally:
            context.close()

        context, page = self.open_page(
            purchase_outcomes=[PurchaseOutcome.rejected("Sensitive failure details")]
        )
        try:
            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.error").wait_for()
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "purchase_review_opened",
                    "purchase_wallet_handoff",
                    "wallet_connected",
                    "purchase_failed",
                ],
            )
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_escape_cancels_wallet_and_purchase_dialogs_without_handoff(self) -> None:
        context, page = self.open_page()
        try:
            initial_wallet_text = page.locator("#walletButton").text_content()
            initial_payment_notice = page.locator("#paymentNotice").text_content()

            page.locator("#walletButton").click()
            page.keyboard.press("Escape")
            page.locator("#walletDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.keyboard.press("Escape")
            page.locator("#purchaseDialog").wait_for(state="hidden")
            page.wait_for_function("window.__trackAttempts.length === 4")

            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "purchase_review_opened",
                    "purchase_cancelled",
                ],
            )
            self.assertEqual(
                [event["data"] for event in events],
                [None, None, {"item_kind": "entry"}, {"item_kind": "entry"}],
            )
            self.assertEqual(page.evaluate("window.__connectCalls"), 0)
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 0)
            self.assertEqual(
                page.locator("#walletButton").text_content(), initial_wallet_text
            )
            self.assertEqual(
                page.locator("#paymentNotice").text_content(), initial_payment_notice
            )
            self.assert_safe_events(events)
        finally:
            context.close()

        context, page = self.open_page(analytics_throws=True)
        try:
            initial_wallet_text = page.locator("#walletButton").text_content()
            initial_payment_notice = page.locator("#paymentNotice").text_content()

            page.locator("#walletButton").click()
            page.keyboard.press("Escape")
            page.locator("#walletDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.keyboard.press("Escape")
            page.locator("#purchaseDialog").wait_for(state="hidden")

            page.wait_for_function("window.__trackAttempts.length === 4")
            self.assertEqual(
                [event["name"] for event in page.evaluate("window.__trackAttempts")],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "purchase_review_opened",
                    "purchase_cancelled",
                ],
            )
            self.assertEqual(page.evaluate("window.__connectCalls"), 0)
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 0)
            self.assertEqual(
                page.locator("#walletButton").text_content(), initial_wallet_text
            )
            self.assertEqual(
                page.locator("#paymentNotice").text_content(), initial_payment_notice
            )
        finally:
            context.close()

    def test_analytics_exceptions_do_not_change_wallet_or_purchase_outcomes(self) -> None:
        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(
                failure_mode=failure_mode, purchase_outcome="confirmed"
            ), self.outage_page(
                failure_mode,
                purchase_outcomes=[
                    PurchaseOutcome.successful(
                        {"signature": "confirmed-signature"}
                    )
                ],
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                page.locator("#shop button").first.wait_for()
                page.locator("#walletButton").click()
                page.locator('#walletDialog [data-dialog-wallet="solflare"]').click()
                page.locator("#walletDialog").wait_for(state="hidden")
                self.assertEqual(
                    page.locator("#walletButton").text_content(), "Wallet connected"
                )

                page.locator("#shop button").first.click()
                page.locator("#confirmPurchaseButton").click()
                page.locator("#paymentNotice.ready").wait_for()
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
                self.assertIn("purchased", page.locator("#paymentNotice").text_content())
                self.assertEqual(outage.errors, [])

            with self.subTest(
                failure_mode=failure_mode, purchase_outcome="rejected"
            ), self.outage_page(
                failure_mode,
                purchase_outcomes=[PurchaseOutcome.rejected("wallet rejected")],
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                page.locator("#shop button").first.wait_for()
                page.locator("#shop button").first.click()
                page.locator("#confirmPurchaseButton").click()
                page.locator("#paymentNotice.error").wait_for()
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
                self.assertEqual(
                    page.locator("#paymentNotice").text_content(), "wallet rejected"
                )
                self.assertEqual(outage.errors, [])

    def test_rapid_purchase_clicks_submit_and_confirm_once_during_analytics_outages(
        self,
    ) -> None:
        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), self.outage_page(
                failure_mode,
                purchase_outcomes=[
                    PurchaseOutcome.successful(
                        {"signature": "confirmed-signature"}, deferred=True
                    )
                ],
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                shop_button = page.locator("#shop button").first
                shop_button.wait_for()

                shop_button.evaluate("(button) => { button.click(); button.click(); }")
                page.locator("#purchaseDialog").wait_for(state="visible")
                page.locator("#confirmPurchaseButton").evaluate(
                    "(button) => { button.click(); button.click(); }"
                )
                page.wait_for_function("window.__purchaseCalls === 1")

                self.assertTrue(shop_button.is_disabled())
                self.assertTrue(page.locator("#confirmPurchaseButton").is_disabled())

                page.evaluate("window.__settlePurchase()")
                page.locator("#paymentNotice.ready").wait_for()

                attempts = page.evaluate("window.__analyticsAttempts")
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
                self.assertEqual(page.evaluate("window.__confirmedPurchaseOutcomes"), 1)
                self.assertEqual(attempts.count("purchase_confirmed"), 1)
                self.assertEqual(
                    attempts,
                    [
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "wallet_connected",
                        "purchase_confirmed",
                    ],
                )
                self.assertIn("purchased", page.locator("#paymentNotice").text_content())
                self.assertEqual(outage.errors, [])

    def test_completed_entry_purchase_stays_locked_during_analytics_outages(
        self,
    ) -> None:
        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), self.outage_page(
                failure_mode,
                purchase_outcomes=[
                    PurchaseOutcome.successful(
                        {"signature": "confirmed-signature"}
                    )
                ],
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                shop_button = page.locator("#shop button").first
                confirm_button = page.locator("#confirmPurchaseButton")
                shop_button.wait_for()

                shop_button.click()
                confirm_button.click()
                page.locator("#paymentNotice.ready").wait_for()

                shop_button.evaluate("(button) => { button.click(); button.click(); }")
                confirm_button.evaluate("(button) => { button.click(); button.click(); }")

                self.assertTrue(shop_button.is_disabled())
                self.assertTrue(confirm_button.is_disabled())
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
                self.assertEqual(page.evaluate("window.__confirmedPurchaseOutcomes"), 1)
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "wallet_connected",
                        "purchase_confirmed",
                    ],
                )
                self.assertEqual(outage.errors, [])

    def test_rapid_purchase_clicks_submit_once_and_recover_after_rejection(self) -> None:
        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), self.outage_page(
                failure_mode,
                purchase_outcomes=[
                    PurchaseOutcome.rejected("wallet rejected", deferred=True),
                    PurchaseOutcome.successful(
                        {"signature": "confirmed-signature"}
                    ),
                ],
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                shop_buttons = page.locator("#shop button")
                self.assertEqual(shop_buttons.count(), 2)
                other_shop_button = shop_buttons.nth(0)
                shop_button = shop_buttons.nth(1)
                shop_button.wait_for()

                shop_button.evaluate("(button) => { button.click(); button.click(); }")
                page.locator("#purchaseDialog").wait_for(state="visible")
                page.locator("#confirmPurchaseButton").evaluate(
                    "(button) => { button.click(); button.click(); }"
                )
                page.wait_for_function("window.__purchaseCalls === 1")

                self.assertTrue(shop_button.is_disabled())
                self.assertTrue(page.locator("#confirmPurchaseButton").is_disabled())
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "wallet_connected",
                    ],
                )

                page.evaluate("window.__settlePurchase()")
                page.locator("#paymentNotice.error").wait_for()
                self.assertFalse(shop_button.is_disabled())
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "wallet_connected",
                        "purchase_failed",
                    ],
                )
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
                self.assertEqual(
                    page.locator("#paymentNotice").text_content(), "wallet rejected"
                )

                shop_button.click()
                page.locator("#purchaseDialog").wait_for(state="visible")
                page.locator("#confirmPurchaseButton").evaluate(
                    "(button) => { button.click(); button.click(); }"
                )
                page.locator("#paymentNotice.ready").wait_for()
                self.assertEqual(page.evaluate("window.__purchaseCalls"), 2)
                self.assertEqual(
                    page.evaluate("window.__purchaseItemIds"),
                    ["vip-entry-ticket", "vip-entry-ticket"],
                )
                self.assertNotIn(
                    "entry-ticket", page.evaluate("window.__purchaseItemIds")
                )
                self.assertFalse(other_shop_button.is_disabled())
                self.assertEqual(page.evaluate("window.__confirmedPurchaseOutcomes"), 1)
                self.assertIn("purchased", page.locator("#paymentNotice").text_content())
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "wallet_connected",
                        "purchase_failed",
                        "purchase_review_opened",
                        "purchase_wallet_handoff",
                        "purchase_confirmed",
                    ],
                )
                self.assertEqual(outage.errors, [])

    def test_wallet_open_selection_cancellation_and_connection_emit_once(self) -> None:
        context, page = self.open_page()
        try:
            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-close-dialog]').click()
            page.wait_for_function("window.__events.length === 2")
            self.assertEqual(
                [event["name"] for event in self.events(page)],
                ["wallet_dialog_opened", "wallet_dialog_cancelled"],
            )

            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-dialog-wallet="phantom"]').click()
            page.locator("#walletDialog").wait_for(state="hidden")
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "wallet_dialog_opened",
                    "wallet_option_selected",
                    "wallet_connected",
                ],
            )
            self.assertEqual(
                events[-2]["data"],
                {"adapter": "phantom", "location": "dialog"},
            )
            self.assertEqual(events[-1]["data"], {"adapter": "phantom"})
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_purchase_cancel_handoff_failure_and_confirmation_emit_once(self) -> None:
        context, page = self.open_page(
            purchase_outcomes=[
                PurchaseOutcome.successful(
                    {"signature": "SignatureMustNeverReachAnalytics"}
                )
            ]
        )
        try:
            page.locator("#shop button").first.click()
            page.locator("#cancelPurchaseButton").click()
            page.locator("#purchaseDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.ready").wait_for()
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "purchase_review_opened",
                    "purchase_cancelled",
                    "purchase_review_opened",
                    "purchase_wallet_handoff",
                    "wallet_connected",
                    "purchase_confirmed",
                ],
            )
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assert_safe_events(events)
        finally:
            context.close()

        context, page = self.open_page(
            purchase_outcomes=[PurchaseOutcome.rejected("Sensitive failure details")]
        )
        try:
            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.error").wait_for()
            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "purchase_review_opened",
                    "purchase_wallet_handoff",
                    "wallet_connected",
                    "purchase_failed",
                ],
            )
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assert_safe_events(events)
        finally:
            context.close()

    def test_escape_cancels_wallet_and_purchase_dialogs_without_handoff(self) -> None:
        context, page = self.open_page()
        try:
            initial_wallet_text = page.locator("#walletButton").text_content()
            initial_payment_notice = page.locator("#paymentNotice").text_content()

            page.locator("#walletButton").click()
            page.keyboard.press("Escape")
            page.locator("#walletDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.keyboard.press("Escape")
            page.locator("#purchaseDialog").wait_for(state="hidden")
            page.wait_for_function("window.__trackAttempts.length === 4")

            events = self.events(page)
            self.assertEqual(
                [event["name"] for event in events],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "purchase_review_opened",
                    "purchase_cancelled",
                ],
            )
            self.assertEqual(
                [event["data"] for event in events],
                [None, None, {"item_kind": "entry"}, {"item_kind": "entry"}],
            )
            self.assertEqual(page.evaluate("window.__connectCalls"), 0)
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 0)
            self.assertEqual(
                page.locator("#walletButton").text_content(), initial_wallet_text
            )
            self.assertEqual(
                page.locator("#paymentNotice").text_content(), initial_payment_notice
            )
            self.assert_safe_events(events)
        finally:
            context.close()

        context, page = self.open_page(analytics_throws=True)
        try:
            initial_wallet_text = page.locator("#walletButton").text_content()
            initial_payment_notice = page.locator("#paymentNotice").text_content()

            page.locator("#walletButton").click()
            page.keyboard.press("Escape")
            page.locator("#walletDialog").wait_for(state="hidden")

            page.locator("#shop button").first.click()
            page.keyboard.press("Escape")
            page.locator("#purchaseDialog").wait_for(state="hidden")

            page.wait_for_function("window.__trackAttempts.length === 4")
            self.assertEqual(
                [event["name"] for event in page.evaluate("window.__trackAttempts")],
                [
                    "wallet_dialog_opened",
                    "wallet_dialog_cancelled",
                    "purchase_review_opened",
                    "purchase_cancelled",
                ],
            )
            self.assertEqual(page.evaluate("window.__connectCalls"), 0)
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 0)
            self.assertEqual(
                page.locator("#walletButton").text_content(), initial_wallet_text
            )
            self.assertEqual(
                page.locator("#paymentNotice").text_content(), initial_payment_notice
            )
        finally:
            context.close()

    def test_analytics_exceptions_do_not_change_wallet_or_purchase_outcomes(self) -> None:
        context, page = self.open_page(
            analytics_throws=True,
            purchase_outcomes=[
                PurchaseOutcome.successful({"signature": "confirmed-signature"})
            ],
        )
        try:
            page.locator("#walletButton").click()
            page.locator('#walletDialog [data-dialog-wallet="solflare"]').click()
            page.locator("#walletDialog").wait_for(state="hidden")
            self.assertEqual(page.locator("#walletButton").text_content(), "Wallet connected")

            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.ready").wait_for()
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assertIn("purchased", page.locator("#paymentNotice").text_content())
        finally:
            context.close()

        context, page = self.open_page(
            analytics_throws=True,
            purchase_outcomes=[PurchaseOutcome.rejected("wallet rejected")],
        )
        try:
            page.locator("#shop button").first.click()
            page.locator("#confirmPurchaseButton").click()
            page.locator("#paymentNotice.error").wait_for()
            self.assertEqual(page.evaluate("window.__purchaseCalls"), 1)
            self.assertEqual(page.locator("#paymentNotice").text_content(), "wallet rejected")
        finally:
            context.close()
