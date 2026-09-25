"""Offline, devnet-shaped tests for the TrustLlama fee protocol.

This is deliberately a local state-machine simulation.  It does not implement
an on-chain program, contact Solana, or persist any private key material.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TextIO

from solders.keypair import Keypair


TOKEN_DECIMALS: Final = 9
TOTAL_SUPPLY_TLAMA: Final = 1_000_000_000
TOTAL_SUPPLY_RAW: Final = TOTAL_SUPPLY_TLAMA * 10**TOKEN_DECIMALS
BPS_DENOMINATOR: Final = 10_000
BUY_FEE_BPS: Final = 50
SELL_FEE_BPS: Final = 100
SYSTEM_PROGRAM_ADDRESS: Final = "11111111111111111111111111111111"


class SimulationRejected(ValueError):
    """Raised when the simulated hook would reject a transaction."""


def calculate_fee(amount_raw: int, fee_bps: int) -> int:
    """Calculate a fee with the specification's integer ceiling rule."""

    if (
        not isinstance(amount_raw, int)
        or isinstance(amount_raw, bool)
        or amount_raw <= 0
    ):
        raise SimulationRejected("gross amount must be a positive integer")
    if fee_bps not in (BUY_FEE_BPS, SELL_FEE_BPS):
        raise SimulationRejected("unsupported fee rate")
    fee = (amount_raw * fee_bps + BPS_DENOMINATOR - 1) // BPS_DENOMINATOR
    if fee <= 0 or fee > amount_raw:
        raise SimulationRejected("fee is outside the valid amount range")
    return fee


@dataclass(frozen=True)
class SimulationWallet:
    """An ephemeral wallet identity; its private key never leaves memory."""

    label: str
    public_key: str


@dataclass
class SimulationTokenAccount:
    address: str
    owner: SimulationWallet
    balance_raw: int
    approved_pool_vault: bool = False


@dataclass(frozen=True)
class SwapReceipt:
    direction: str
    gross_raw: int
    fee_raw: int
    net_raw: int
    supply_before_raw: int
    supply_after_raw: int


class TransferHookSimulation:
    """A small atomic ledger modeling the reviewed hook contract."""

    def __init__(self) -> None:
        self.total_supply_raw = TOTAL_SUPPLY_RAW
        self.approved_pool_vaults: set[str] = set()

    def register_pool_vault(self, account: SimulationTokenAccount) -> None:
        if account.address == SYSTEM_PROGRAM_ADDRESS:
            raise SimulationRejected("system program is not a token account")
        account.approved_pool_vault = True
        self.approved_pool_vaults.add(account.address)

    def _validate_token_account(self, account: SimulationTokenAccount) -> None:
        if account.address == SYSTEM_PROGRAM_ADDRESS:
            raise SimulationRejected("system program is not a token account")
        if account.balance_raw < 0:
            raise SimulationRejected("token balance cannot be negative")

    def simulate_swap(
        self,
        *,
        source: SimulationTokenAccount,
        destination: SimulationTokenAccount,
        gross_raw: int,
        include_checked_burn: bool = True,
        burn_raw_override: int | None = None,
    ) -> SwapReceipt:
        """Run one atomic approved-pool swap or roll it back on rejection."""

        self._validate_token_account(source)
        self._validate_token_account(destination)
        if source.address == destination.address:
            raise SimulationRejected("source and destination must differ")

        source_is_pool = source.address in self.approved_pool_vaults
        destination_is_pool = destination.address in self.approved_pool_vaults
        if source_is_pool and not destination_is_pool:
            direction = "buy"
            fee_bps = BUY_FEE_BPS
        elif not source_is_pool and destination_is_pool:
            direction = "sell"
            fee_bps = SELL_FEE_BPS
        else:
            raise SimulationRejected(
                "swap must have exactly one approved pool-vault endpoint"
            )

        fee_raw = calculate_fee(gross_raw, fee_bps)
        if not include_checked_burn:
            raise SimulationRejected("approved swap is missing its checked burn")
        if burn_raw_override is not None and burn_raw_override != fee_raw:
            raise SimulationRejected("checked burn amount does not match policy")
        if source.balance_raw < gross_raw:
            raise SimulationRejected("source balance cannot cover gross amount")

        net_raw = gross_raw - fee_raw
        supply_before_raw = self.total_supply_raw
        source_before_raw = source.balance_raw
        destination_before_raw = destination.balance_raw
        try:
            source.balance_raw -= gross_raw
            destination.balance_raw += net_raw
            self.total_supply_raw -= fee_raw
            if self.total_supply_raw < 0:
                raise SimulationRejected("burn would exceed total supply")
        except Exception:
            source.balance_raw = source_before_raw
            destination.balance_raw = destination_before_raw
            self.total_supply_raw = supply_before_raw
            raise

        return SwapReceipt(
            direction=direction,
            gross_raw=gross_raw,
            fee_raw=fee_raw,
            net_raw=net_raw,
            supply_before_raw=supply_before_raw,
            supply_after_raw=self.total_supply_raw,
        )

    def simulate_peer_transfer(
        self,
        *,
        source: SimulationTokenAccount,
        destination: SimulationTokenAccount,
        amount_raw: int,
    ) -> None:
        """Model a non-pool transfer, which has no swap fee in this policy."""

        self._validate_token_account(source)
        self._validate_token_account(destination)
        if amount_raw <= 0 or source.balance_raw < amount_raw:
            raise SimulationRejected("peer transfer amount is invalid")
        if (
            source.address in self.approved_pool_vaults
            or destination.address in self.approved_pool_vaults
        ):
            raise SimulationRejected("peer transfer touches an approved pool")
        source.balance_raw -= amount_raw
        destination.balance_raw += amount_raw


class TransferHookSimulationTests(unittest.TestCase):
    """Automated protocol checks using a fresh wallet environment per run."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wallet_directory = tempfile.TemporaryDirectory(
            prefix="tlama-transfer-hook-wallets-"
        )
        cls.addClassCleanup(cls.wallet_directory.cleanup)
        cls.wallets = {}
        cls.addClassCleanup(cls.wallets.clear)
        for label in ("pool", "buyer", "seller", "peer"):
            keypair = Keypair()
            cls.wallets[label] = SimulationWallet(
                label=label, public_key=str(keypair.pubkey())
            )

        # The manifest intentionally contains public keys only. Keypair objects
        # are discarded after this setup method and never written to disk.
        manifest = {
            "environment": "ephemeral-local-simulation",
            "wallets": {
                label: wallet.public_key for label, wallet in cls.wallets.items()
            },
        }
        Path(cls.wallet_directory.name, "public-wallet-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )

    def setUp(self) -> None:
        self.simulation = TransferHookSimulation()
        self.pool = SimulationTokenAccount(
            address=str(Keypair().pubkey()),
            owner=self.wallets["pool"],
            balance_raw=100_000_000 * 10**TOKEN_DECIMALS,
        )
        self.buyer = SimulationTokenAccount(
            address=str(Keypair().pubkey()),
            owner=self.wallets["buyer"],
            balance_raw=0,
        )
        self.seller = SimulationTokenAccount(
            address=str(Keypair().pubkey()),
            owner=self.wallets["seller"],
            balance_raw=100_000_000 * 10**TOKEN_DECIMALS,
        )
        self.peer = SimulationTokenAccount(
            address=str(Keypair().pubkey()),
            owner=self.wallets["peer"],
            balance_raw=10_000 * 10**TOKEN_DECIMALS,
        )
        self.simulation.register_pool_vault(self.pool)

    def test_buy_burns_exact_half_percent_and_transfers_net(self) -> None:
        receipt = self.simulation.simulate_swap(
            source=self.pool,
            destination=self.buyer,
            gross_raw=10_001,
        )
        self.assertEqual(receipt.fee_raw, 51)
        self.assertEqual(receipt.net_raw, 9_950)
        self.assertEqual(receipt.supply_before_raw - receipt.supply_after_raw, 51)
        self.assertEqual(self.pool.balance_raw, 100_000_000 * 10**TOKEN_DECIMALS - 10_001)
        self.assertEqual(self.buyer.balance_raw, 9_950)

    def test_sell_burns_exact_one_percent_and_transfers_net(self) -> None:
        receipt = self.simulation.simulate_swap(
            source=self.seller,
            destination=self.pool,
            gross_raw=10_001,
        )
        self.assertEqual(receipt.fee_raw, 101)
        self.assertEqual(receipt.net_raw, 9_900)
        self.assertEqual(receipt.supply_before_raw - receipt.supply_after_raw, 101)
        self.assertEqual(self.seller.balance_raw, 100_000_000 * 10**TOKEN_DECIMALS - 10_001)
        self.assertEqual(self.pool.balance_raw, 100_000_000 * 10**TOKEN_DECIMALS + 9_900)

    def test_boundary_and_micro_amounts_use_integer_ceiling(self) -> None:
        vectors = (
            (1, BUY_FEE_BPS, 1),
            (10_000, BUY_FEE_BPS, 50),
            (10_001, BUY_FEE_BPS, 51),
            (10_000, SELL_FEE_BPS, 100),
            (10_001, SELL_FEE_BPS, 101),
        )
        for amount_raw, fee_bps, expected in vectors:
            self.assertEqual(calculate_fee(amount_raw, fee_bps), expected)
            fee_raw = calculate_fee(amount_raw, fee_bps)
            self.assertEqual(fee_raw + (amount_raw - fee_raw), amount_raw)

    def test_missing_burn_rolls_back_entire_swap(self) -> None:
        pool_before = self.pool.balance_raw
        buyer_before = self.buyer.balance_raw
        supply_before = self.simulation.total_supply_raw
        with self.assertRaisesRegex(SimulationRejected, "missing"):
            self.simulation.simulate_swap(
                source=self.pool,
                destination=self.buyer,
                gross_raw=10_000,
                include_checked_burn=False,
            )
        self.assertEqual(self.pool.balance_raw, pool_before)
        self.assertEqual(self.buyer.balance_raw, buyer_before)
        self.assertEqual(self.simulation.total_supply_raw, supply_before)

    def test_wrong_burn_amount_rolls_back_entire_swap(self) -> None:
        pool_before = self.pool.balance_raw
        with self.assertRaisesRegex(SimulationRejected, "does not match"):
            self.simulation.simulate_swap(
                source=self.pool,
                destination=self.buyer,
                gross_raw=10_000,
                burn_raw_override=100,
            )
        self.assertEqual(self.pool.balance_raw, pool_before)
        self.assertEqual(self.buyer.balance_raw, 0)

    def test_unapproved_pool_is_rejected(self) -> None:
        unknown_pool = SimulationTokenAccount(
            address=str(Keypair().pubkey()),
            owner=self.wallets["pool"],
            balance_raw=100_000,
        )
        with self.assertRaisesRegex(SimulationRejected, "approved pool"):
            self.simulation.simulate_swap(
                source=unknown_pool,
                destination=self.buyer,
                gross_raw=10_000,
            )

    def test_peer_transfer_is_fee_free_and_supply_neutral(self) -> None:
        supply_before = self.simulation.total_supply_raw
        self.simulation.simulate_peer_transfer(
            source=self.peer,
            destination=self.buyer,
            amount_raw=10_000,
        )
        self.assertEqual(self.simulation.total_supply_raw, supply_before)
        self.assertEqual(self.peer.balance_raw, 10_000 * 10**TOKEN_DECIMALS - 10_000)
        self.assertEqual(self.buyer.balance_raw, 10_000)

    def test_system_program_address_cannot_be_a_token_account(self) -> None:
        system_account = SimulationTokenAccount(
            address=SYSTEM_PROGRAM_ADDRESS,
            owner=self.wallets["pool"],
            balance_raw=100_000,
        )
        with self.assertRaisesRegex(SimulationRejected, "system program"):
            self.simulation.register_pool_vault(system_account)

    def test_harness_has_no_network_or_signing_path(self) -> None:
        source = Path(__file__).read_text(encoding="utf-8")
        forbidden_parts = (
            ("Rpc", "Client"),
            ("send", "_transaction"),
            ("send_raw", "_transaction"),
            ("WALLET", "_PRIVATE_KEY"),
            ("sub", "process"),
            ("requests", "."),
            ("http", "://"),
            ("https", "://"),
        )
        for parts in forbidden_parts:
            marker = "".join(parts)
            self.assertNotIn(marker, source)


def run_validation(log_stream: TextIO) -> bool:
    """Run the full suite into a caller-owned log stream."""

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        TransferHookSimulationTests
    )
    result = unittest.TextTestRunner(
        stream=log_stream, verbosity=2, buffer=True
    ).run(suite)
    return result.wasSuccessful()