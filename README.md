# 🔒 TrustLlama Core Engine (Public Mirror)

Welcome to the public repository mirror for the **TrustLlama Core Engine**. It documents the server-authoritative architecture and provides a free, standalone node load-testing toolkit.

🏠 **Official Website:** [tllama.me](https://tllama.me)  
💳 **Secure USDC Checkout:** [tllama.me/buy](https://tllama.me)

---

## 📊 Verification & Architecture

The licensed arcade framework centers on 30Hz FastAPI/WebSocket server-authoritative gameplay logic and local SQLite Write-Ahead Logging (WAL) persistence.
* **Public load toolkit:** Three bounded, read-only HTTP/WebSocket scenarios and 12 offline self-tests. These are the actual published toolkit counts.
* **Isolated token framework:** The Token-2022 Transfer Hook and Pool Adapter elements are gated behind a secure Fail-Closed/Devnet Simulation boundary, not presented as a universal exploit patch or live on-chain execution.

---

## Free Node Load Toolkit

The [standalone toolkit](tests/stress_suite/README.md) uses Python 3 standard-library modules only. It checks a node you operate through its health endpoint, a persistence-backed read endpoint, or a WebSocket connect-and-close handshake. It sends no gameplay, payment, wallet, or write requests.

```sh
python3 -m unittest discover -s tests/stress_suite -p "test_*.py" -v
python3 tests/stress_suite/runner.py --base-url http://127.0.0.1:8099 --scenario health --requests 20 --rate 5 --concurrency 4 --i-own-this-node
```

See the toolkit README for authorization, rate limits, optional scenarios, and route overrides. The 12 offline self-tests check the toolkit itself; load-test request totals are set by the runner options, not by a claimed repository-wide test count.

---

## 🛠️ Commercial Software Licensing

Commercial studios can license the separate 30Hz FastAPI/WebSocket server-authoritative arcade backend with local SQLite WAL persistence through [secure USDC checkout](https://tllama.me/buy), from **$350 to $1,950 USDC**. The free toolkit does not include the commercial engine. License scope is governed by the executed corporate licensing agreement; checkout accepts native USDC on Solana.

### Available Licensing Packages:
* **Indie Developer License (\$350 USDC):** Entry-tier package for solo engineers and rapid prototyping.
* **Startup Studio License (\$750 USDC) `MOST CHOSEN`:** Commercial rights for multi-project development teams and growing studios. Includes advanced automated webhook delivery rails.
* **Enterprise Commercial License (\$1,950 USDC):** Full institutional, unrestricted high-throughput usage rights with priority production master distribution frameworks.

*Note: License scope is strictly governed by the executed corporate licensing agreement.*

---

## 🛡️ Repository Structure & Governance

To maintain total system integrity, our architecture operates across a strict multi-layer deployment model:
1. **Public Engine Skeleton (`trustllama-core-engine`):** Provides transparent visibility into our 30Hz server-authoritative event loops, state persistence boundaries, and client input clamping.
2. **Guarded Platform Infrastructure (`trustllama-platform-private`):** Our separate, private mirror contains audited Token-2022 programs, custom pricing oracle routers, directional burn hooks, and production mainnet runbooks.

---
© 2026 TRUSTLLAMA LTD (Reference: 17473862). Registered in England & Wales. All rights reserved.
