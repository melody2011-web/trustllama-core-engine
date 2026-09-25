"""Bounded, read-only load probes for nodes you operate. Python standard library only."""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import math
import os
import ssl
import time
from urllib.parse import urlsplit, urlunsplit


DEFAULT_PATHS = {
    "health": "/healthz",
    "storage-read": "/leaderboard",
    "websocket": "/ws",
}
MAX_REQUESTS = 500
MAX_CONCURRENCY = 16
MAX_RATE = 20.0
MAX_TIMEOUT = 10.0
MAX_SUBMISSION_SECONDS = 30.0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass(frozen=True)
class Settings:
    base_url: str
    scenario: str = "health"
    path: str | None = None
    requests: int = 20
    concurrency: int = 4
    rate: float = 5.0
    timeout: float = 3.0
    i_own_this_node: bool = False

    def target(self) -> str:
        if not self.i_own_this_node:
            raise ValueError("Confirm you operate the target with --i-own-this-node.")
        if self.scenario not in DEFAULT_PATHS:
            raise ValueError("Unknown scenario.")
        if not 1 <= self.requests <= MAX_REQUESTS:
            raise ValueError(f"Requests must be between 1 and {MAX_REQUESTS}.")
        if not 1 <= self.concurrency <= MAX_CONCURRENCY:
            raise ValueError(f"Concurrency must be between 1 and {MAX_CONCURRENCY}.")
        if not 0 < self.rate <= MAX_RATE or not math.isfinite(self.rate):
            raise ValueError(f"Rate must be greater than zero and at most {MAX_RATE}/s.")
        if (self.requests - 1) > self.rate * MAX_SUBMISSION_SECONDS:
            raise ValueError(f"Submission window must fit within {MAX_SUBMISSION_SECONDS:g}s.")
        if not 0 < self.timeout <= MAX_TIMEOUT or not math.isfinite(self.timeout):
            raise ValueError(f"Timeout must be greater than zero and at most {MAX_TIMEOUT}s.")

        if any(ord(char) < 33 or ord(char) > 126 for char in self.base_url):
            raise ValueError("Base URL must contain only printable ASCII without spaces.")
        base = urlsplit(self.base_url)
        if (
            base.scheme not in {"http", "https"}
            or not base.hostname
            or base.username is not None
            or base.password is not None
            or base.query
            or base.fragment
        ):
            raise ValueError("Base URL must be an http(s) node URL without credentials, query or fragment.")
        try:
            port = base.port
        except ValueError as exc:
            raise ValueError("Invalid port in base URL.") from exc
        if port is not None and port == 0:
            raise ValueError("Invalid port in base URL.")
        path = self.path if self.path is not None else DEFAULT_PATHS[self.scenario]
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(char in path for char in "?#\r\n\\")
            or any(part in {".", ".."} for part in path.split("/"))
            or any(ord(char) < 33 or ord(char) > 126 for char in path)
        ):
            raise ValueError("Path must be a simple absolute path without query or traversal.")
        prefix = base.path.rstrip("/")
        if any(part in {".", ".."} for part in prefix.split("/")):
            raise ValueError("Base URL path must not contain traversal.")
        return urlunsplit((base.scheme, base.netloc, prefix + path, "", ""))


@dataclass(frozen=True)
class Probe:
    status: int
    elapsed_ms: float
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {200, 101} and not self.error


async def _read_headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    headers = b""
    while b"\r\n\r\n" not in headers and len(headers) < 8192:
        part = await reader.read(min(1024, 8192 - len(headers)))
        if not part:
            break
        headers += part
    if b"\r\n\r\n" not in headers:
        raise ValueError("Incomplete or oversized response headers.")
    head, _, _ = headers.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ValueError("Invalid HTTP response.")
    status = int(parts[1])
    fields = dict(line.split(":", 1) for line in lines[1:] if ":" in line)
    return status, {name.strip().lower(): value.strip() for name, value in fields.items()}


async def _connect(url: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return await asyncio.open_connection(
        parsed.hostname,
        port,
        ssl=ssl.create_default_context() if parsed.scheme == "https" else None,
        server_hostname=parsed.hostname if parsed.scheme == "https" else None,
    )


async def _http_request(url: str) -> int:
    parsed = urlsplit(url)
    reader, writer = await _connect(url)
    try:
        writer.write((
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            "User-Agent: TrustLlama-public-load-toolkit/1\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii"))
        await writer.drain()
        status, _ = await _read_headers(reader)
        return status
    finally:
        writer.close()


async def _websocket_request(url: str) -> int:
    """Only upgrade and close. No game messages, authentication or state writes."""
    parsed = urlsplit(url)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    request = (
        f"GET {parsed.path} HTTP/1.1\r\n"
        f"Host: {parsed.netloc}\r\n"
        f"Origin: {origin}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode("ascii")
    reader, writer = await _connect(url)
    try:
        writer.write(request)
        await writer.drain()
        status, fields = await _read_headers(reader)
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if status != 101 or fields.get("sec-websocket-accept") != expected:
            return status
        # RFC 6455 requires client frames to be masked, including Close.
        writer.write(b"\x88\x80" + os.urandom(4))
        await writer.drain()
        return 101
    finally:
        writer.close()


def probe(url: str, timeout: float, scenario: str) -> Probe:
    start = time.monotonic()
    try:
        request = _websocket_request(url) if scenario == "websocket" else _http_request(url)
        status = asyncio.run(asyncio.wait_for(request, timeout=timeout))
        expected = 101 if scenario == "websocket" else 200
        return Probe(status, (time.monotonic() - start) * 1000, "" if status == expected else "Unexpected HTTP status")
    except (OSError, TimeoutError, ValueError, ssl.SSLError) as exc:
        return Probe(0, (time.monotonic() - start) * 1000, type(exc).__name__)


def run(settings: Settings) -> dict[str, object]:
    url = settings.target()  # Validate before making any request.
    results: list[Probe] = []
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=settings.concurrency) as pool:
        futures = []
        for index in range(settings.requests):
            deadline = started + index / settings.rate
            if deadline > time.monotonic():
                time.sleep(deadline - time.monotonic())
            futures.append(pool.submit(probe, url, settings.timeout, settings.scenario))
        for future in as_completed(futures):
            results.append(future.result())
    latencies = sorted(result.elapsed_ms for result in results)

    def percentile(percent: float) -> float:
        return round(latencies[max(0, math.ceil(len(latencies) * percent) - 1)], 2)

    return {
        "scenario": settings.scenario,
        "requests": len(results),
        "successes": sum(result.ok for result in results),
        "failures": sum(not result.ok for result in results),
        "status_counts": dict(sorted(Counter(str(result.status) for result in results).items())),
        "latency_ms": {"p50": percentile(0.50), "p95": percentile(0.95), "p99": percentile(0.99)},
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Node URL, including its path prefix if needed")
    parser.add_argument("--scenario", choices=tuple(DEFAULT_PATHS), default="health")
    parser.add_argument("--path", help="Override the selected scenario's read-only route")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rate", type=float, default=5, help="Maximum request submissions per second")
    parser.add_argument("--timeout", type=float, default=3, help="Per-request timeout in seconds")
    parser.add_argument("--i-own-this-node", action="store_true", help="Required authorization confirmation")
    args = parser.parse_args()
    try:
        result = run(Settings(**vars(args)))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0 if result["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())