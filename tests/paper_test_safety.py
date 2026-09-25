from __future__ import annotations

import os
import socket
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from eth_account.signers.local import LocalAccount
from web3.eth import Eth

from bot.smart_router import SmartRouterBridge
from bot.trader import PancakeSwapTrader


@contextmanager
def paper_test_safety_guard():
    """Make accidental live-chain use fail loudly in paper-account tests."""

    credential_names = ("BSC_RPC_URL", "WALLET_ADDRESS", "WALLET_PRIVATE_KEY")
    saved_credentials = {
        name: os.environ.pop(name)
        for name in credential_names
        if name in os.environ
    }
    real_connect = socket.socket.connect

    def guarded_connect(sock: socket.socket, address: object) -> object:
        if sock.family == socket.AF_UNIX:
            return real_connect(sock, address)
        host = address[0] if isinstance(address, tuple) and address else address
        if str(host) not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError(
                "Paper safety guard blocked network access to "
                f"{host!s}; mock the RPC or use the loopback status server."
            )
        return real_connect(sock, address)

    def blocked(operation: str):
        def fail(*_args: object, **_kwargs: object) -> None:
            raise AssertionError(
                "Paper safety guard blocked live "
                f"{operation}; paper tests must use mocked quotes and never "
                "sign, approve, or broadcast transactions."
            )

        return fail

    try:
        with ExitStack() as guard:
            guard.enter_context(patch.object(socket.socket, "connect", guarded_connect))
            guard.enter_context(
                patch.object(LocalAccount, "sign_transaction", blocked("signing"))
            )
            guard.enter_context(
                patch.object(Eth, "send_raw_transaction", blocked("broadcast"))
            )
            guard.enter_context(
                patch.object(
                    PancakeSwapTrader,
                    "_approve_if_needed",
                    blocked("approval"),
                )
            )
            guard.enter_context(
                patch.object(
                    SmartRouterBridge,
                    "quote",
                    blocked("Smart Router RPC"),
                )
            )
            guard.enter_context(
                patch.object(
                    SmartRouterBridge,
                    "build_trade",
                    blocked("Smart Router RPC"),
                )
            )
            yield
    finally:
        os.environ.update(saved_credentials)