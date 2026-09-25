"""Tests for standalone Solana RPC client validation."""

from __future__ import annotations

import math
import unittest
from unittest.mock import patch

from solana_webhook_core_rail_pro.rpc import SolanaRpcClient


class SolanaRpcClientTests(unittest.TestCase):
    def test_rejects_non_positive_or_non_finite_timeout_before_session_creation(
        self,
    ) -> None:
        for timeout in (0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(timeout=timeout):
                with patch(
                    "solana_webhook_core_rail_pro.rpc.aiohttp.ClientSession"
                ) as session:
                    with self.assertRaisesRegex(
                        ValueError,
                        r"timeout_seconds must be finite and greater than zero",
                    ):
                        SolanaRpcClient(
                            ("https://rpc.example.test",),
                            timeout_seconds=timeout,
                        )

                session.assert_not_called()


if __name__ == "__main__":
    unittest.main()