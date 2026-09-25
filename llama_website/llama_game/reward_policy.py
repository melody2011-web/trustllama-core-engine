"""Deterministic TLAMA arcade reward allocation policy.

This module is deliberately pure: it contains no network, wallet, database,
environment-variable, or administrative control surface. A future on-chain
reward vault can use the same integer allocation rules to verify payouts.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

TOTAL_SUPPLY_TLAMA = 1_000_000_000
TOKEN_DECIMALS = 9
REWARD_VAULT_PERCENT = 10
REWARD_VAULT_TLAMA = TOTAL_SUPPLY_TLAMA * REWARD_VAULT_PERCENT // 100
REWARD_VAULT_RAW = REWARD_VAULT_TLAMA * 10**TOKEN_DECIMALS

# A fixed epoch budget means one score board cannot accidentally consume the
# whole vault.  Exactly 1,000 fixed epochs can allocate the full vault.
EPOCH_PAYOUT_TLAMA = 100_000
EPOCH_PAYOUT_RAW = EPOCH_PAYOUT_TLAMA * 10**TOKEN_DECIMALS
MAX_REWARD_EPOCHS = REWARD_VAULT_TLAMA // EPOCH_PAYOUT_TLAMA
SCORE_ATTESTATION = "server-authoritative-room"


@dataclass(frozen=True, slots=True)
class RewardVaultPolicy:
    """The only allocation policy used by this game framework."""

    total_supply_tlama: int
    vault_percent: int
    vault_tlama: int
    epoch_payout_tlama: int
    max_reward_epochs: int
    purpose: str


REWARD_VAULT_POLICY = RewardVaultPolicy(
    total_supply_tlama=TOTAL_SUPPLY_TLAMA,
    vault_percent=REWARD_VAULT_PERCENT,
    vault_tlama=REWARD_VAULT_TLAMA,
    epoch_payout_tlama=EPOCH_PAYOUT_TLAMA,
    max_reward_epochs=MAX_REWARD_EPOCHS,
    purpose="automated in-game player rewards and leaderboard payouts only",
)


@dataclass(frozen=True, slots=True)
class VerifiedGameScore:
    """A score emitted by the server-authoritative game room."""

    player_id: str
    score: int
    attestation: str = SCORE_ATTESTATION

    def __post_init__(self) -> None:
        if not self.player_id or len(self.player_id) > 80:
            raise ValueError("player_id must be a non-empty bounded identifier")
        if self.score < 0:
            raise ValueError("verified scores cannot be negative")
        if self.attestation != SCORE_ATTESTATION:
            raise ValueError("scores must come from the server-authoritative room")


@dataclass(frozen=True, slots=True)
class RewardAllocation:
    player_id: str
    score: int
    amount_raw: int
    claim_id: str


@dataclass(frozen=True, slots=True)
class EpochRewardAllocation:
    epoch_number: int
    vault_total_raw: int
    payout_budget_raw: int
    allocated_raw: int
    remaining_vault_raw: int
    allocations: tuple[RewardAllocation, ...]


def _claim_id(epoch_number: int, player_id: str, amount_raw: int) -> str:
    value = f"{epoch_number}:{player_id}:{amount_raw}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def allocate_epoch_rewards(
    scores: Iterable[VerifiedGameScore],
    *,
    epoch_number: int,
    vault_remaining_raw: int = REWARD_VAULT_RAW,
) -> EpochRewardAllocation:
    """Allocate one fixed epoch budget by verified score.

    The epoch number and remaining balance are canonical game/vault state, not
    administrative inputs. Payouts use integer arithmetic and distribute every
    available unit of that epoch budget exactly once, with deterministic
    largest-remainder tie-breaking.
    """

    if epoch_number < 0 or epoch_number >= MAX_REWARD_EPOCHS:
        raise ValueError("epoch_number is outside the fixed reward schedule")
    if vault_remaining_raw < 0 or vault_remaining_raw > REWARD_VAULT_RAW:
        raise ValueError("vault_remaining_raw must be within the immutable vault")

    normalized = tuple(scores)
    player_ids = [entry.player_id for entry in normalized]
    if len(player_ids) != len(set(player_ids)):
        raise ValueError("each player may appear only once per verified score board")

    positive = tuple(entry for entry in normalized if entry.score > 0)
    available_budget = min(EPOCH_PAYOUT_RAW, vault_remaining_raw)
    if not positive or available_budget == 0:
        return EpochRewardAllocation(
            epoch_number=epoch_number,
            vault_total_raw=REWARD_VAULT_RAW,
            payout_budget_raw=0,
            allocated_raw=0,
            remaining_vault_raw=vault_remaining_raw,
            allocations=(),
        )

    total_score = sum(entry.score for entry in positive)
    base_amounts: dict[str, int] = {}
    remainders: list[tuple[int, str]] = []
    allocated = 0
    for entry in positive:
        numerator = available_budget * entry.score
        base, remainder = divmod(numerator, total_score)
        base_amounts[entry.player_id] = base
        remainders.append((remainder, entry.player_id))
        allocated += base

    # The deterministic order prevents score ties from becoming an
    # administrative choice or depending on dictionary insertion order.
    for _, player_id in sorted(remainders, key=lambda item: (-item[0], item[1]))[
        : available_budget - allocated
    ]:
        base_amounts[player_id] += 1

    allocations = tuple(
        RewardAllocation(
            player_id=entry.player_id,
            score=entry.score,
            amount_raw=base_amounts[entry.player_id],
            claim_id=_claim_id(epoch_number, entry.player_id, base_amounts[entry.player_id]),
        )
        for entry in sorted(positive, key=lambda item: (-item.score, item.player_id))
    )
    allocated_raw = sum(entry.amount_raw for entry in allocations)
    return EpochRewardAllocation(
        epoch_number=epoch_number,
        vault_total_raw=REWARD_VAULT_RAW,
        payout_budget_raw=available_budget,
        allocated_raw=allocated_raw,
        remaining_vault_raw=vault_remaining_raw - allocated_raw,
        allocations=allocations,
    )