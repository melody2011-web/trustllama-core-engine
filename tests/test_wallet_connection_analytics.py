import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from tests.analytics_outage_fixture import (
    ANALYTICS_FAILURE_MODES,
    analytics_outage_page,
)


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "llama_website/llama_game/index.html").read_text()
ONBOARDING = (ROOT / "llama_website/llama_game/web3-onboarding.js").read_text()
GAME_PAGE = (ROOT / "llama_website/llama_game/index.html").as_uri()


class WalletConnectionAnalyticsTests(unittest.TestCase):
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

    def test_missing_portal_reports_unavailable_setup(self):
        portal_guard = re.search(
            r'if \(!window\.TLAMA_WEB3.*?throw new Error\("The wallet adapter portal is still loading\."\);',
            INDEX,
            re.DOTALL,
        )
        self.assertIsNotNone(portal_guard)
        self.assertIn('trackEvent("wallet_connection_failed"', portal_guard.group())
        self.assertIn('stage: "unavailable_setup"', portal_guard.group())

    def test_failure_event_payload_is_allowlisted(self):
        listener = re.search(
            r'addEventListener\("tlama-wallet-connection-failed".*?\n        \}\);',
            INDEX,
            re.DOTALL,
        )
        self.assertIsNotNone(listener)
        source = listener.group()
        self.assertIn('trackEvent("wallet_connection_failed"', source)
        self.assertIn("adapter: safeWalletAdapter(", source)
        self.assertIn(
            '["unavailable_setup", "provider_rejected", "connection_failed"]',
            source,
        )
        for forbidden in ("message:", "address:", "signature:", "provider:", "payload:"):
            self.assertNotIn(forbidden, source)

    def test_failure_stage_behavior(self):
        subprocess.run(
            ["node", str(ROOT / "tests/wallet_failure_stages.test.mjs")],
            cwd=ROOT,
            check=True,
        )

    def test_backend_serves_onboarding_module_dependencies(self):
        script = f"""
import sys
sys.path.insert(0, {str(ROOT / "llama_website/llama_game")!r})
from fastapi.testclient import TestClient
from backend import app
with TestClient(app) as client:
    onboarding = client.get("/web3-onboarding.js")
    helper = client.get("/wallet-failure-stages.js")
    assert onboarding.status_code == 200
    assert helper.status_code == 200
    assert './wallet-failure-stages.js' in onboarding.text
    assert 'connectionFailureStage' in helper.text
"""
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env["TLAMA_GAME_DB_PATH"] = str(Path(directory) / "arcade.sqlite3")
            subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=env,
                check=True,
            )

    def test_each_failure_branch_reports_once(self):
        connect = re.search(
            r'const connect = async \(kind = "phantom"\) => \{(.*?)\n\};\n\nconst requestQuote',
            ONBOARDING,
            re.DOTALL,
        )
        self.assertIsNotNone(connect)
        source = connect.group(1)
        self.assertEqual(source.count("reportConnectionFailure("), 4)
        self.assertIn('reportConnectionFailure(kind, "unavailable_setup")', source)
        self.assertIn("connectionFailureStage(error)", source)
        self.assertIn('reportConnectionFailure(kind, "connection_failed")', source)

    def test_analytics_errors_are_isolated(self):
        wrapper = re.search(
            r"const trackEvent = \(name, data\) => \{(.*?)\n        \};",
            INDEX,
            re.DOTALL,
        )
        self.assertIsNotNone(wrapper)
        self.assertIn("try {", wrapper.group())
        self.assertIn("} catch {", wrapper.group())

    def test_wallet_failure_remains_usable_during_analytics_outages(self):
        for failure_mode in ANALYTICS_FAILURE_MODES:
            with self.subTest(failure_mode=failure_mode), analytics_outage_page(
                self.browser, failure_mode
            ) as outage:
                page = outage.page
                page.goto(GAME_PAGE, wait_until="domcontentloaded")
                page.locator("#walletButton").click()
                page.locator('#walletDialog [data-dialog-wallet="phantom"]').click()
                page.wait_for_function(
                    "window.__analyticsAttempts.includes('wallet_connection_failed')"
                )

                self.assertTrue(page.locator("#walletDialog").is_visible())
                self.assertEqual(
                    page.locator("#walletDialogStatus").text_content(),
                    "The wallet adapter portal is still loading. "
                    "Try again or choose another wallet.",
                )
                self.assertEqual(
                    page.evaluate("window.__analyticsAttempts"),
                    [
                        "wallet_dialog_opened",
                        "wallet_option_selected",
                        "wallet_connection_failed",
                    ],
                )
                self.assertEqual(outage.errors, [])


if __name__ == "__main__":
    unittest.main()