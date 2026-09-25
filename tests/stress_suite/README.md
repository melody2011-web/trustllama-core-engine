# Public node load toolkit

This standalone, Python-standard-library toolkit probes **nodes you operate**. It was designed from the public HTTP and WebSocket surfaces of the TrustLlama arcade backend, not copied from its `backend.py` or `storage.py` implementation. It contains one runner and **12 offline self-tests**; it does **not** contain or claim 601 stress cases or 59 test files.

## Quick start

From the repository root, run the offline tests first:

```sh
python3 -m unittest discover -s tests/stress_suite -p 'test_*.py' -v
```

Then point the runner at a node you are authorized to load-test:

```sh
python3 tests/stress_suite/runner.py \
  --base-url http://127.0.0.1:8099 \
  --scenario health \
  --requests 20 --rate 5 --concurrency 4 \
  --i-own-this-node
```

Scenarios (all read-only):

| Scenario | Default path | What it checks |
| --- | --- | --- |
| `health` | `/healthz` | HTTP 200 health responses |
| `storage-read` | `/leaderboard` | HTTP 200 from a persistence-backed public read route; never opens the database |
| `websocket` | `/ws` | HTTP 101 upgrade and masked close; sends no game messages |

For the latter two scenarios, change `--scenario`. If your app uses a path prefix, include it in `--base-url` (for example `https://your-node.example/game`); override a route with `--path /your-read-only-route` **only after confirming that GET route has no side effects**. The WebSocket probe sends an Origin based on the node URL. The target may reject it if that origin is not allowlisted.

Output is JSON with actual request count, successes, failures, HTTP status counts, elapsed time, and p50/p95/p99 latency in milliseconds. The process exits nonzero when any probe fails. The harness runs no payment, admin, wallet, write, RPC, or token transactions; it does not prove game correctness, token security, SBF reproducibility, or SQLite write concurrency.

## Safety limits

- Explicit `--i-own-this-node` acknowledgement is required, even on localhost. Never test an unaffiliated service.
- Default: 20 requests, 5 new requests/second, 4 concurrent workers, 3-second per-request timeout.
- Hard ceilings: 500 requests, 20 new requests/second, 16 workers, 10-second per-request timeout.
- All requests must be submitted within 30 seconds; excessively low rates are rejected. Each request has a total deadline, including connection and response headers.
- No credentials in the URL, no redirects to another host, no query strings or path traversal. Response bodies are capped at 8 KiB and never printed.
- Start on a disposable/local node. Watch its CPU, storage, and error rate; stop if the node becomes unhealthy.

This toolkit is free to run against nodes you control. Commercial TrustLlama engine licenses are separate.