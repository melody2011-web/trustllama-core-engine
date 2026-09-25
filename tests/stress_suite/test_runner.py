"""Offline tests: all traffic stays on a disposable loopback fixture."""

from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import Settings, WS_GUID, run  # noqa: E402


class LocalNode(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: object) -> None:
        pass

    def respond(self, status: int, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in {"/healthz", "/app/healthz"}:
            self.respond(200, b'{"status":"ok"}')
        elif self.path == "/leaderboard":
            self.respond(200, b'{"leaderboard":[]}')
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/healthz")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/slow":
            self.connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            try:
                for _ in range(10):
                    time.sleep(0.04)
                    self.connection.sendall(b"x")
            except OSError:
                pass
            self.close_connection = True
        elif self.path == "/ws":
            key = self.headers.get("Sec-WebSocket-Key", "")
            accept = base64.b64encode(
                hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.connection.settimeout(2)
            try:
                self.rfile.read(6)  # The client's masked, zero-payload Close frame.
            except OSError:
                pass
            self.close_connection = True
        else:
            self.respond(404)


class ToolkitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalNode)
        cls.server.daemon_threads = True
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def options(self, **overrides: object) -> Settings:
        return Settings(
            **{
                "base_url": self.base,
                "i_own_this_node": True,
                "requests": 3,
                "rate": 20.0,
                **overrides,
            }
        )

    def test_owner_confirmation_required_before_any_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "operate the target"):
            run(Settings(base_url=self.base))

    def test_paths_respect_base_prefix(self) -> None:
        self.assertEqual(
            self.options(base_url=self.base + "/app/").target(),
            self.base + "/app/healthz",
        )

    def test_rejects_url_credentials_query_fragment_and_bad_scheme(self) -> None:
        for value in ("ftp://localhost", "http://user:pass@localhost", self.base + "?a=1",
                      self.base + "#x", "http://"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.options(base_url=value).target()

    def test_rejects_unsafe_paths(self) -> None:
        for path in ("//other-host/", "/../admin", "/healthz?token=x", "/healthz\r\nX: 1"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.options(path=path).target()

    def test_enforces_request_concurrency_rate_and_timeout_caps(self) -> None:
        for override in ({"requests": 501}, {"concurrency": 17}, {"rate": 21.0},
                         {"timeout": 11.0}, {"rate": 0}, {"rate": 0.0001}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.options(**override).target()

    def test_health_probe_reports_actual_request_count(self) -> None:
        report = run(self.options(requests=4))
        self.assertEqual(report["requests"], 4)
        self.assertEqual(report["successes"], 4)
        self.assertEqual(report["status_counts"], {"200": 4})
        self.assertGreaterEqual(report["latency_ms"]["p95"], 0)

    def test_storage_read_uses_only_public_leaderboard(self) -> None:
        report = run(self.options(scenario="storage-read"))
        self.assertEqual(report["successes"], 3)
        self.assertEqual(report["failures"], 0)

    def test_redirect_is_not_followed(self) -> None:
        report = run(self.options(path="/redirect", requests=1))
        self.assertEqual(report["status_counts"], {"302": 1})
        self.assertEqual(report["failures"], 1)

    def test_non_200_response_is_a_failure(self) -> None:
        report = run(self.options(path="/missing", requests=2))
        self.assertEqual(report["status_counts"], {"404": 2})
        self.assertEqual(report["failures"], 2)

    def test_slow_drip_cannot_extend_request_deadline(self) -> None:
        started = time.monotonic()
        report = run(self.options(path="/slow", requests=1, timeout=0.06))
        self.assertEqual(report["failures"], 1)
        self.assertLess(time.monotonic() - started, 0.3)

    def test_websocket_upgrades_and_closes_without_gameplay(self) -> None:
        report = run(self.options(scenario="websocket"))
        self.assertEqual(report["status_counts"], {"101": 3})
        self.assertEqual(report["successes"], 3)

    def test_websocket_rejection_is_reported(self) -> None:
        report = run(self.options(scenario="websocket", path="/missing", requests=1))
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["status_counts"], {"404": 1})


if __name__ == "__main__":
    unittest.main()