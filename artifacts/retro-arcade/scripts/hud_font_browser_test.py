import os
import socket
import subprocess
import time
import unittest
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import sync_playwright


PROJECT_DIR = Path(__file__).resolve().parents[1]
NARROWEST_CANVAS_WIDTH = 320
HUD_LEFT = 21
HUD_RIGHT = 12 + 226
HUD_LABELS = (
    "LEVEL 1  •  NEON OUTSKIRTS",
    "TLAMA TOKENS COLLECTED 2/5",
    "STAGE CLEAR!",
)
DISPLAY_FONT_WEIGHTS = (400, 500, 600, 700)


def available_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class MobileHudFontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = available_port()
        environment = os.environ.copy()
        environment.update(
            {
                "BASE_PATH": "/retro-arcade/",
                "PORT": str(cls.port),
            }
        )
        cls.server = subprocess.Popen(
            [
                "pnpm",
                "exec",
                "vite",
                "--host",
                "127.0.0.1",
                "--port",
                str(cls.port),
                "--strictPort",
            ],
            cwd=PROJECT_DIR,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{cls.port}/retro-arcade/"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                raise RuntimeError("Retro Arcade dev server exited before becoming ready")
            try:
                with urlopen(url, timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for the Retro Arcade dev server")

        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get("CHROMIUM_PATH", "/repl/tools/bin/chromium"),
            headless=True,
        )
        cls.page = cls.browser.new_page(
            viewport={"width": NARROWEST_CANVAS_WIDTH, "height": 640}
        )
        cls.page.goto(url, wait_until="networkidle")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "browser"):
            cls.browser.close()
            cls.playwright.stop()
        cls.server.terminate()
        try:
            cls.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait(timeout=5)

    def test_space_mono_hud_labels_fit_the_narrowest_canvas(self) -> None:
        result = self.page.evaluate(
            """async ({ labels, font, left, right, canvasWidth }) => {
                const loadedFaces = await document.fonts.load(font, labels.join(''));
                await document.fonts.ready;
                const canvas = document.createElement('canvas');
                canvas.width = canvasWidth;
                const context = canvas.getContext('2d');
                context.font = font;
                return {
                    fontStatus: document.fonts.status,
                    loadedFaceCount: loadedFaces.length,
                    labels: labels.map((text) => ({
                        text,
                        width: context.measureText(text).width,
                        left,
                        right: left + context.measureText(text).width,
                        limit: right,
                    })),
                };
            }""",
            {
                "labels": HUD_LABELS,
                "font": '11px "Space Mono"',
                "left": HUD_LEFT,
                "right": HUD_RIGHT,
                "canvasWidth": NARROWEST_CANVAS_WIDTH,
            },
        )

        self.assertEqual(result["fontStatus"], "loaded")
        self.assertGreater(
            result["loadedFaceCount"],
            0,
            "Space Mono did not load; refusing to validate HUD widths with a fallback font",
        )
        for label in result["labels"]:
            with self.subTest(label=label["text"]):
                self.assertGreaterEqual(label["left"], 0)
                self.assertLessEqual(label["right"], label["limit"])
                self.assertLessEqual(label["right"], NARROWEST_CANVAS_WIDTH)

    def test_pixelify_sans_display_weights_load_without_fallback(self) -> None:
        results = self.page.evaluate(
            """async (weights) => {
                const sample = 'TRUSTLLAMA ARCADE';
                return Promise.all(weights.map(async (weight) => {
                    const descriptor = `${weight} 32px "Pixelify Sans"`;
                    const loadedFaces = await document.fonts.load(descriptor, sample);
                    await document.fonts.ready;
                    return {
                        weight,
                        loadedFaceCount: loadedFaces.length,
                        available: document.fonts.check(descriptor, sample),
                    };
                }));
            }""",
            DISPLAY_FONT_WEIGHTS,
        )

        for result in results:
            with self.subTest(weight=result["weight"]):
                self.assertTrue(result["available"])
                self.assertGreater(
                    result["loadedFaceCount"],
                    0,
                    (
                        f"Pixelify Sans {result['weight']} did not load; "
                        "the display font silently fell back"
                    ),
                )


if __name__ == "__main__":
    unittest.main()