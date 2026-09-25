from __future__ import annotations

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from web3 import Web3

from bot.config import Settings
from bot.main import main
from bot.trader import (
    LiveTradingDisabled,
    PancakeSwapTrader,
    StrategyGuardError,
    StrategyStateStore,
    TradeResult,
    TraderError,
    V3Route,
)
from bot.smart_router import SmartRouterError, SmartRouterTrade
from bot.contracts import PANCAKESWAP_SMART_ROUTER


def settings(**overrides: object) -> Settings:
    base = Settings(
        bsc_rpc_url="http://example.invalid",
        wallet_address="0x0000000000000000000000000000000000000000",
        wallet_private_key="not-used-by-these-tests",
        enable_live_trading=True,
        slippage_bps=100,
        deadline_seconds=120,
        receipt_timeout_seconds=180,
        max_gas_limit=700_000,
        strategy_fast_window=2,
        strategy_slow_window=3,
        strategy_rsi_window=2,
        strategy_buy_rsi_max=100,
    )
    return Settings(**{**base.__dict__, **overrides})


class _Hash(bytes):
    def hex(self) -> str:
        return "0x" + super().hex()


class _SignedTransaction:
    hash = _Hash(b"\x01" * 32)
    raw_transaction = b"signed"


class _FakeAccount:
    address = "0x0000000000000000000000000000000000000000"

    def __init__(self) -> None:
        self.signed_transactions: list[dict[str, object]] = []

    def sign_transaction(self, transaction: dict[str, object]) -> _SignedTransaction:
        self.signed_transactions.append(dict(transaction))
        return _SignedTransaction()


class _FakeEth:
    gas_price = 3

    def __init__(self, receipts: list[dict[str, object]], *, token_balance: int = 10**18):
        self.receipts = iter(receipts)
        self.token_balance = token_balance
        self.sent: list[bytes] = []
        self.bnb_balance = 10**18
        self.receipt_count = 0
        self.on_receipt: object | None = None

    def get_balance(self, _address: str) -> int:
        return self.bnb_balance

    def get_transaction_count(self, _address: str, _block: str) -> int:
        return len(self.sent)

    def get_block(self, _block: str) -> dict[str, int]:
        return {"timestamp": 1_000}

    def estimate_gas(self, _transaction: dict[str, object]) -> int:
        return 100

    def send_raw_transaction(self, raw_transaction: bytes) -> _Hash:
        self.sent.append(raw_transaction)
        return _Hash(b"\x02" * 32)

    def wait_for_transaction_receipt(
        self, _tx_hash: _Hash, timeout: int
    ) -> dict[str, object]:
        self.last_timeout = timeout
        self.receipt_count += 1
        if callable(self.on_receipt):
            self.on_receipt(self.receipt_count)
        return next(self.receipts)


class _FakeCall:
    def __init__(self, value: int):
        self.value = value

    def call(self) -> int:
        return self.value


class _FakeFunction:
    def __init__(self, name: str, transactions: list[dict[str, object]]):
        self.name = name
        self.transactions = transactions

    def estimate_gas(self, _transaction: dict[str, object]) -> int:
        return 100

    def build_transaction(self, transaction: dict[str, object]) -> dict[str, object]:
        self.transactions.append({"function": self.name, **transaction})
        return transaction

    def _encode_transaction_data(self) -> str:
        return "0x1234"


class _FakeTokenFunctions:
    def __init__(
        self,
        allowance: int,
        balance: int,
        transactions: list[dict[str, object]],
    ):
        self.allowance_value = allowance
        self.balance = balance
        self.transactions = transactions
        self.allowance_spenders: list[str] = []
        self.approval_spenders: list[str] = []

    def allowance(self, _owner: str, spender: str) -> _FakeCall:
        self.allowance_spenders.append(spender)
        return _FakeCall(self.allowance_value)

    def balanceOf(self, _owner: str) -> _FakeCall:
        return _FakeCall(self.balance)

    def decimals(self) -> _FakeCall:
        return _FakeCall(18)

    def approve(self, spender: str, _amount: int) -> _FakeFunction:
        self.approval_spenders.append(spender)
        return _FakeFunction("approve", self.transactions)


class _FakeRouterFunctions:
    def __init__(self, transactions: list[dict[str, object]]):
        self.transactions = transactions

    def exactInput(self, _params: tuple[object, ...]) -> _FakeFunction:
        return _FakeFunction("exactInput", self.transactions)

    def unwrapWETH9(self, _minimum: int, _recipient: str) -> _FakeFunction:
        return _FakeFunction("unwrapWETH9", self.transactions)

    def multicall(self, _calls: list[bytes]) -> _FakeFunction:
        return _FakeFunction("multicall", self.transactions)


class _LiveTraderFixture:
    def trader(
        self,
        receipts: list[dict[str, object]],
        *,
        allowance: int = 0,
        token_balance: int = 10**18,
        minimum_out: int = 800,
    ) -> tuple[PancakeSwapTrader, _FakeEth, _FakeTokenFunctions, list[dict[str, object]]]:
        trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
        trader.settings = settings()
        eth = _FakeEth(receipts, token_balance=token_balance)
        trader.w3 = SimpleNamespace(eth=eth)
        trader.account = _FakeAccount()
        transactions: list[dict[str, object]] = []
        token_functions = _FakeTokenFunctions(allowance, token_balance, transactions)
        token = SimpleNamespace(functions=token_functions)
        trader._token = lambda _symbol: token  # type: ignore[method-assign]
        output_receipt = 1 if allowance >= 10**18 else 2

        def apply_confirmed_output(receipt_count: int) -> None:
            if receipt_count == output_receipt:
                token_functions.balance += 900

        eth.on_receipt = apply_confirmed_output
        trader.router = SimpleNamespace(
            functions=_FakeRouterFunctions(transactions)
        )
        def build_trade(
            _input: str,
            _output: str,
            amount: int,
            *,
            native_input: bool,
            native_output: bool,
            direct_v3: bool,
            **_kwargs: object,
        ) -> SmartRouterTrade:
            self.assertFalse(native_input)
            self.assertFalse(native_output)
            self.assertTrue(direct_v3)
            return SmartRouterTrade(
                amount_out=900,
                route_count=1,
                v2_candidates=0,
                v3_candidates=1,
                stable_candidates=0,
                router_address=PANCAKESWAP_SMART_ROUTER,
                calldata="0xac9650d8",
                value=0,
                minimum_out=minimum_out,
            )

        trader.smart_router = SimpleNamespace(build_trade=build_trade)
        trader._estimate_gas = lambda _function, _transaction: 120  # type: ignore[method-assign]
        trader._broadcast_attempted = False
        trader._strategy_broadcast_callback = None
        return trader, eth, token_functions, transactions


def _receipt(
    status: int, block: int, *, native_proceeds: int = 900
) -> dict[str, object]:
    withdrawal_topic = Web3.keccak(text="Withdrawal(address,uint256)").hex()
    router_topic = "0x" + "0" * 24 + PANCAKESWAP_SMART_ROUTER[2:].lower()
    return {
        "status": status,
        "blockNumber": block,
        "transactionHash": _Hash(bytes([block]) * 32),
        "gasUsed": 100,
        "effectiveGasPrice": 3,
        "logs": [
            {
                "address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
                "topics": [withdrawal_topic, router_topic],
                "data": "0x" + native_proceeds.to_bytes(32, "big").hex(),
            }
        ],
    }


class LiveSwapReceiptTests(unittest.TestCase, _LiveTraderFixture):
    def _state_file(self, trader: PancakeSwapTrader, directory: str) -> StrategyStateStore:
        trader.settings = settings(
            strategy_state_file=str(Path(directory) / "trade-state.json")
        )
        return StrategyStateStore(trader.settings.strategy_state_file)

    def test_sell_confirms_approval_before_final_swap(self) -> None:
        trader, eth, token, transactions = self.trader(
            [_receipt(1, 10), _receipt(1, 11)],
            allowance=0,
        )

        result = trader.execute("sell", "XRP", "1")

        self.assertEqual(result.block_number, 11)
        self.assertEqual(len(eth.sent), 2)
        self.assertEqual(
            [transaction["function"] for transaction in transactions],
            ["approve"],
        )
        self.assertEqual(token.allowance_spenders, [PANCAKESWAP_SMART_ROUTER])
        self.assertEqual(token.approval_spenders, [PANCAKESWAP_SMART_ROUTER])
        self.assertEqual(
            trader.account.signed_transactions[-1]["to"], PANCAKESWAP_SMART_ROUTER
        )
        self.assertEqual(trader.account.signed_transactions[-1]["value"], 0)
        self.assertTrue(trader._broadcast_attempted)
        self.assertEqual(token.allowance_value, 0)

    def test_reverted_approval_stops_before_final_swap(self) -> None:
        trader, eth, _token, transactions = self.trader(
            [_receipt(0, 10)],
            allowance=0,
        )

        with self.assertRaisesRegex(TraderError, "reverted"):
            trader.execute("sell", "XRP", "1")

        self.assertEqual(len(eth.sent), 1)
        self.assertEqual([transaction["function"] for transaction in transactions], ["approve"])

    def test_reverted_final_swap_is_not_a_trade_result(self) -> None:
        trader, eth, _token, transactions = self.trader(
            [_receipt(1, 10), _receipt(0, 11)],
            allowance=0,
        )

        with self.assertRaisesRegex(TraderError, "reverted"):
            trader.execute("sell", "XRP", "1")

        self.assertEqual(len(eth.sent), 2)
        self.assertEqual(
            [transaction["function"] for transaction in transactions],
            ["approve"],
        )
        self.assertEqual(
            trader.account.signed_transactions[-1]["to"], PANCAKESWAP_SMART_ROUTER
        )
        self.assertEqual(trader.account.signed_transactions[-1]["value"], 0)

    def test_usdt_buy_approves_input_and_sends_zero_native_value(self) -> None:
        trader, eth, token, transactions = self.trader(
            [_receipt(1, 10), _receipt(1, 11)], allowance=0
        )

        result = trader.execute("buy", "XRP", "1")

        self.assertEqual(len(eth.sent), 2)
        self.assertEqual([transaction["function"] for transaction in transactions], ["approve"])
        self.assertEqual(token.approval_spenders, [PANCAKESWAP_SMART_ROUTER])
        self.assertEqual(result.amount_in, "1")
        self.assertEqual(result.amount_out, "9E-16")
        transaction = trader.account.signed_transactions[-1]
        self.assertEqual(transaction["to"], PANCAKESWAP_SMART_ROUTER)
        self.assertEqual(transaction["data"], "0xac9650d8")
        self.assertEqual(transaction["value"], 0)

    def test_insufficient_native_gas_stops_before_broadcast(self) -> None:
        trader, eth, _token, _transactions = self.trader([], allowance=10**18)
        eth.bnb_balance = 0

        with self.assertRaisesRegex(TraderError, "native BNB.*estimated transaction gas"):
            trader.execute("buy", "XRP", "1")

        self.assertFalse(trader._broadcast_attempted)
        self.assertEqual(eth.sent, [])

    def test_wrapped_bnb_target_is_rejected_before_quote_or_broadcast(self) -> None:
        trader, eth, _token, _transactions = self.trader([], allowance=10**18)
        trader.smart_router.build_trade = Mock()  # type: ignore[method-assign]

        with self.assertRaisesRegex(TraderError, "supports XRP and BTC only"):
            trader.execute("buy", "WBNB", "1")

        trader.smart_router.build_trade.assert_not_called()
        self.assertEqual(eth.sent, [])

    def test_missing_smart_router_route_fails_without_an_approval_or_broadcast(self) -> None:
        trader = PancakeSwapTrader.__new__(PancakeSwapTrader)
        trader.settings = settings()
        trader.account = _FakeAccount()
        eth = _FakeEth([])
        trader.w3 = SimpleNamespace(eth=eth)
        trader.smart_router = SimpleNamespace(
            build_trade=Mock(side_effect=SmartRouterError("No valid PancakeSwap Smart Router path"))
        )
        trader._broadcast_attempted = False

        with self.assertRaisesRegex(TraderError, "No valid PancakeSwap Smart Router path"):
            trader.execute("buy", "XRP", "1")

        self.assertFalse(trader._broadcast_attempted)
        self.assertEqual(eth.sent, [])

    def test_zero_minimum_smart_router_trade_fails_before_approval_or_broadcast(self) -> None:
        trader, eth, _token, transactions = self.trader(
            [], allowance=0, minimum_out=0
        )

        with self.assertRaisesRegex(TraderError, "minimum output is zero"):
            trader.execute("sell", "XRP", "1")

        self.assertFalse(trader._broadcast_attempted)
        self.assertEqual(eth.sent, [])
        self.assertEqual(transactions, [])

    def test_manual_timeout_keeps_signed_transaction_claimed_until_reconciled(self) -> None:
        trader, eth, token, _transactions = self.trader([], allowance=10**18)
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)

            def timeout_after_send(raw_transaction: bytes) -> _Hash:
                eth.sent.append(raw_transaction)
                raise TimeoutError("RPC connection dropped after send")

            eth.send_raw_transaction = timeout_after_send  # type: ignore[method-assign]
            with self.assertRaisesRegex(TimeoutError, "dropped"):
                trader.execute("buy", "XRP", "1")

            pending = state.pending()
            self.assertIsNotNone(pending)
            self.assertEqual(pending["kind"], "manual")
            self.assertEqual(pending["phase"], "swap")
            self.assertEqual(pending["tx_hash"], "0x" + "01" * 32)
            with self.assertRaisesRegex(StrategyGuardError, "already pending"):
                trader.execute("buy", "XRP", "1")

            token.balance = 10**18 + 500
            eth.get_transaction_receipt = lambda _tx_hash: _receipt(1, 12)  # type: ignore[attr-defined]
            result = trader.reconcile_manual(now=2_000)

            self.assertEqual(result.block_number, 12)
            self.assertEqual(Decimal(result.amount_out), Decimal(500) / Decimal(10**18))
            self.assertEqual(trader._last_manual_reconciliation_status, "confirmed")
            self.assertIsNone(state.pending())

    def test_manual_reconciliation_releases_a_reverted_transaction(self) -> None:
        trader, eth, _token, _transactions = self.trader([], allowance=10**18)
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)

            def disconnected_after_send(raw_transaction: bytes) -> _Hash:
                eth.sent.append(raw_transaction)
                raise ConnectionError("RPC disconnected")

            eth.send_raw_transaction = disconnected_after_send  # type: ignore[method-assign]
            with self.assertRaisesRegex(ConnectionError, "disconnected"):
                trader.execute("sell", "XRP", "1")

            eth.get_transaction_receipt = lambda _tx_hash: _receipt(0, 13)  # type: ignore[attr-defined]
            self.assertIsNone(trader.reconcile_manual(now=2_000))

            self.assertEqual(trader._last_manual_reconciliation_status, "reverted")
            self.assertIsNone(state.pending())

    def test_manual_sell_reconciliation_reports_confirmed_usdt_proceeds(self) -> None:
        trader, eth, token, _transactions = self.trader([], allowance=10**18)
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)

            def disconnected_after_send(raw_transaction: bytes) -> _Hash:
                eth.sent.append(raw_transaction)
                raise ConnectionError("RPC disconnected")

            eth.send_raw_transaction = disconnected_after_send  # type: ignore[method-assign]
            with self.assertRaisesRegex(ConnectionError, "disconnected"):
                trader.execute("sell", "XRP", "1")

            # The actual receipt is below the Smart Router quote (900) but
            # still successful. The USDT balance delta is the confirmed output.
            # An unrelated native-BNB balance change must not alter the result.
            eth.bnb_balance = 10**18 + 1_000_000
            token.balance += 750
            eth.get_transaction_receipt = lambda _tx_hash: _receipt(  # type: ignore[attr-defined]
                1, 13, native_proceeds=750
            )
            result = trader.reconcile_manual(now=2_000)

            self.assertEqual(Decimal(result.amount_out), Decimal(750) / Decimal(10**18))
            self.assertNotEqual(Decimal(result.amount_out), Decimal(900) / Decimal(10**18))
            self.assertIsNone(state.pending())

    def test_manual_wbnb_buy_reconciliation_uses_trusted_native_withdrawal(self) -> None:
        trader, eth, _token, _transactions = self.trader(
            [], allowance=10**18
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)
            trader.settings = settings(
                strategy_state_file=trader.settings.strategy_state_file,
                isolated_scalper_profile="scalper_bot_7",
            )
            fingerprint = state.claim_manual(
                "buy", "WBNB", "1", now=1_000,
                pre_trade_token_balance=0, expected_amount_out="0.0000000000000009",
            )
            state.record_manual_attempt(
                fingerprint, "0x" + "07" * 32, "swap"
            )
            eth.get_transaction_receipt = lambda _tx_hash: _receipt(  # type: ignore[attr-defined]
                1, 14, native_proceeds=750
            )

            result = trader.reconcile_manual(now=2_000)

            self.assertEqual(result.symbol, "WBNB")
            self.assertEqual(
                Decimal(result.amount_out), Decimal(750) / Decimal(10**18)
            )
            self.assertIsNone(state.pending())

    def test_confirmation_callback_failure_keeps_manual_claim_pending(self) -> None:
        trader, _eth, _token, _transactions = self.trader(
            [_receipt(1, 15)], allowance=10**18
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)

            def fail_local_commit(_result: TradeResult) -> None:
                raise OSError("local journal unavailable")

            with self.assertRaisesRegex(OSError, "journal unavailable"):
                trader.execute(
                    "buy", "XRP", "1",
                    confirmation_callback=fail_local_commit,
                )

            pending = state.pending()
            self.assertIsNotNone(pending)
            self.assertEqual(pending["phase"], "swap")

    def test_reconciliation_callback_failure_keeps_manual_claim_pending(self) -> None:
        trader, eth, token, _transactions = self.trader(
            [], allowance=10**18
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            state = self._state_file(trader, temporary_directory)
            fingerprint = state.claim_manual(
                "buy", "XRP", "1", now=1_000,
                pre_trade_token_balance=token.balance,
                expected_amount_out="0.0000000000000009",
            )
            state.record_manual_attempt(
                fingerprint, "0x" + "08" * 32, "swap"
            )
            token.balance += 900
            eth.get_transaction_receipt = lambda _tx_hash: _receipt(1, 16)  # type: ignore[attr-defined]

            def fail_local_commit(_result: TradeResult) -> None:
                raise OSError("recovery journal unavailable")

            with self.assertRaisesRegex(OSError, "recovery journal unavailable"):
                trader.reconcile_manual(
                    now=2_000, confirmation_callback=fail_local_commit
                )

            self.assertIsNotNone(state.pending())


class ConsoleLoggingTests(unittest.TestCase):
    def _run_main_with_trader(self, argv: list[str], trader: object) -> int:
        with (
            patch.object(sys, "argv", ["bot", *argv]),
            patch("bot.main.Settings.from_environment", return_value=settings()),
            patch("bot.main.PancakeSwapTrader", return_value=trader),
        ):
            return main()

    def test_disabled_trade_does_not_log_a_confirmed_trade(self) -> None:
        trader = Mock()
        trader.execute.side_effect = LiveTradingDisabled("disabled")

        with self.assertLogs("defi-bot", level="ERROR") as logs:
            result = self._run_main_with_trader(
                ["--action", "buy", "--symbol", "XRP", "--amount", "1"],
                trader,
            )

        self.assertEqual(result, 1)
        self.assertFalse(any("Confirmed live" in line for line in logs.output))

    def test_confirmed_trade_is_logged_to_console(self) -> None:
        trader = Mock()
        trader.execute.return_value = TradeResult(
            action="sell",
            symbol="BTC",
            amount_in="1",
            amount_out="2",
            tx_hash="0x" + "1" * 64,
            block_number=10,
        )

        with self.assertLogs("defi-bot", level="INFO") as logs:
            result = self._run_main_with_trader(
                ["--action", "sell", "--symbol", "BTC", "--amount", "1"],
                trader,
            )

        self.assertEqual(result, 0)
        self.assertTrue(any("Confirmed live sell BTC trade" in line for line in logs.output))

    def test_manual_reconciliation_is_console_only(self) -> None:
        trader = Mock()
        trader.reconcile_manual.return_value = None
        trader._last_manual_reconciliation_status = "reverted"

        result = self._run_main_with_trader(["--reconcile-manual"], trader)

        self.assertEqual(result, 0)
        trader.reconcile_manual.assert_called_once_with()

    def test_pending_inspection_is_read_only_and_console_only(self) -> None:
        trader = Mock()
        state = Mock()
        state.pending_trade.return_value = SimpleNamespace(
            kind="manual",
            action="sell",
            symbol="XRP",
            amount="1.5",
            tx_hash="0x" + "ab" * 32,
            phase="approval",
        )

        with self.assertLogs("defi-bot", level="INFO") as logs:
            with patch("bot.main.StrategyStateStore", return_value=state):
                result = self._run_main_with_trader(["--inspect-pending"], trader)

        self.assertEqual(result, 0)
        self.assertTrue(
            any(
                "action=sell" in line
                and "asset=XRP" in line
                and "amount=1.5" in line
                and "tx=0x" + "ab" * 32 in line
                and "phase=approval" in line
                for line in logs.output
            )
        )
        state.pending_trade.assert_called_once_with()
        trader.inspect_pending.assert_not_called()
        trader.reconcile_manual.assert_not_called()
        trader.reconcile_strategy.assert_not_called()
        trader.execute.assert_not_called()

    def test_paper_strategy_without_an_order_is_console_only(self) -> None:
        trader = Mock()
        trader.execute_strategy.return_value = None

        result = self._run_main_with_trader(
            ["--strategy", "--symbol", "XRP", "--prices", "10,9,8,7,8,9"],
            trader,
        )

        self.assertEqual(result, 0)
        trader.execute_strategy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
