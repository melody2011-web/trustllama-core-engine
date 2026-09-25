"""Offline TrustLlama (TLAMA) SPL-token setup builder.

This module intentionally builds *unsigned* Solana instructions only.  It does
not create an RPC client, read a private key, sign a transaction, or submit
anything to a network.  A deployment operator can inspect the returned
instructions and sign them in a separate, explicitly controlled process.

TrustLlama uses the legacy SPL Token program and Metaplex Token Metadata:

* fixed supply: 1,000,000,000 TLAMA (9 decimals)
* one-time mint of the complete supply
* mint authority permanently set to None in the same instruction bundle
* freeze authority absent from mint initialization
* metadata created with ``is_mutable=False`` and zero seller royalties

Solana programs do not have an EVM-style "contract owner".  For this token,
the meaningful administrative authorities are the mint and freeze authorities;
this builder removes both.  Metadata immutability removes the metadata update
backdoor as well.

This builder does not configure Token-2022 extensions or directional transfer
fees.  A truthful buy/sell fee design requires an independently audited
Token-2022 Transfer Hook plus compatible, explicitly approved pool adapters;
the offline architecture and its limitations are documented in
``TRUST_LLAMA_TRANSFER_HOOK_SPEC.md``.  That specification is not deployment
code and does not change the unsigned legacy mint plan built here.

The hard-coded anti-whale policy constants below are also offline policy
inputs only.  Token-2022 has no native maximum-wallet extension; enforcement
would require the separately audited Transfer Hook design described in
``TRUST_LLAMA_ANTI_WHALE_SPEC.md``.  They are intentionally not converted into
legacy SPL instructions by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from layer3_core_rail import (  # noqa: E402
    CoreRailValidationError,
    MAX_TRANSACTION_BASE_UNITS,
    MAX_TRANSACTION_TLAMA,
    MAX_WALLET_BASE_UNITS,
    MAX_WALLET_TLAMA,
    validate_transaction_limit,
    validate_wallet_limit,
)

from solders.instruction import AccountMeta, Instruction
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, create_account
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    AuthorityType,
    create_associated_token_account,
    initialize_mint,
    mint_to,
    set_authority,
)
from spl.token.instructions import get_associated_token_address
from spl.token.models import InitializeMintParams, MintToParams, SetAuthorityParams


TLAMA_NAME: Final = "TrustLlama"
TLAMA_SYMBOL: Final = "TLAMA"
TLAMA_DECIMALS: Final = 9
TLAMA_TOTAL_SUPPLY: Final = 1_000_000_000
TLAMA_TOTAL_SUPPLY_BASE_UNITS: Final = TLAMA_TOTAL_SUPPLY * 10**TLAMA_DECIMALS
TLAMA_MAX_TRANSACTION: Final = MAX_TRANSACTION_TLAMA
TLAMA_MAX_TRANSACTION_BASE_UNITS: Final = MAX_TRANSACTION_BASE_UNITS
TLAMA_MAX_WALLET_HOLDING: Final = MAX_WALLET_TLAMA
TLAMA_MAX_WALLET_HOLDING_BASE_UNITS: Final = MAX_WALLET_BASE_UNITS
ANTI_WHALE_REQUIRES_TOKEN_2022_TRANSFER_HOOK: Final = True
ANTI_WHALE_REQUIRES_RENOUNCED_HOOK_UPGRADE_AUTHORITY: Final = True
ANTI_WHALE_ALLOWS_ADMIN_OVERRIDE: Final = False

TOKEN_ACCOUNT_SIZE: Final = 165
METADATA_PROGRAM_ID: Final = Pubkey.from_string(
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
)
SYSTEM_PROGRAM_ID: Final = Pubkey.from_string(
    "11111111111111111111111111111111"
)
SYSVAR_RENT_ID: Final = Pubkey.from_string(
    "SysvarRent111111111111111111111111111111111"
)


class TrustLlamaSetupError(ValueError):
    """Raised when an offline setup plan is unsafe or malformed."""


@dataclass(frozen=True)
class TrustLlamaAntiWhalePolicy:
    """Immutable offline policy values for a future audited Transfer Hook."""

    max_transaction_base_units: int = field(
        init=False, default=TLAMA_MAX_TRANSACTION_BASE_UNITS
    )
    max_wallet_holding_base_units: int = field(
        init=False, default=TLAMA_MAX_WALLET_HOLDING_BASE_UNITS
    )
    requires_token_2022_transfer_hook: bool = field(
        init=False, default=(
        ANTI_WHALE_REQUIRES_TOKEN_2022_TRANSFER_HOOK
        )
    )
    requires_renounced_hook_upgrade_authority: bool = field(
        init=False, default=(
            ANTI_WHALE_REQUIRES_RENOUNCED_HOOK_UPGRADE_AUTHORITY
        )
    )
    allows_administrative_override: bool = field(
        init=False, default=ANTI_WHALE_ALLOWS_ADMIN_OVERRIDE
    )

    def validate_trade_amount(self, amount_base_units: int) -> None:
        """Reject a trade amount above the fixed maximum, without side effects."""

        try:
            validate_transaction_limit(amount_base_units)
        except CoreRailValidationError as exc:
            if "positive integer" in str(exc):
                raise TrustLlamaSetupError(
                    "Anti-whale trade amount must be a positive integer."
                ) from exc
            raise TrustLlamaSetupError(
                "Anti-whale trade amount exceeds the immutable transaction limit."
            ) from exc

    def validate_wallet_holding(
        self, *, current_base_units: int, incoming_base_units: int
    ) -> None:
        """Reject a post-transfer balance above the fixed maximum."""

        try:
            validate_wallet_limit(
                current_base_units=current_base_units,
                incoming_base_units=incoming_base_units,
            )
        except CoreRailValidationError as exc:
            if "holding exceeds" in str(exc):
                raise TrustLlamaSetupError(
                    "Anti-whale wallet holding exceeds the immutable wallet limit."
                ) from exc
            raise TrustLlamaSetupError(
                "Anti-whale wallet balances must be valid integers."
            ) from exc


@dataclass(frozen=True)
class TrustLlamaSetupPlan:
    """Unsigned instructions and derived addresses for offline review."""

    mint: Pubkey
    payer: Pubkey
    token_account: Pubkey
    metadata_account: Pubkey
    instructions: tuple[Instruction, ...]


def _metadata_address(mint: Pubkey) -> Pubkey:
    metadata, _bump = Pubkey.find_program_address(
        [b"metadata", bytes(METADATA_PROGRAM_ID), bytes(mint)],
        METADATA_PROGRAM_ID,
    )
    return metadata


def _borsh_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(4, "little") + encoded


def _immutable_metadata_instruction(
    *,
    metadata: Pubkey,
    mint: Pubkey,
    mint_authority: Pubkey,
    payer: Pubkey,
    uri: str,
) -> Instruction:
    """Build Metaplex CreateMetadataAccountV3 with immutable DataV2.

    The instruction discriminator is the Metaplex enum value for
    CreateMetadataAccountV3.  The option fields are encoded according to the
    program's Borsh layout: creators, collection, uses, and collection details
    are all absent; seller fee is zero; and is_mutable is false.
    """

    data = (
        bytes([33])
        + _borsh_string(TLAMA_NAME)
        + _borsh_string(TLAMA_SYMBOL)
        + _borsh_string(uri)
        + (0).to_bytes(2, "little")
        + bytes([0])  # creators: None
        + bytes([0])  # collection: None
        + bytes([0])  # uses: None
        + bytes([0])  # is_mutable: false
        + bytes([0])  # collection_details: None
    )
    accounts = [
        AccountMeta(metadata, is_signer=False, is_writable=True),
        AccountMeta(mint, is_signer=False, is_writable=False),
        AccountMeta(mint_authority, is_signer=True, is_writable=False),
        AccountMeta(payer, is_signer=True, is_writable=True),
        AccountMeta(payer, is_signer=False, is_writable=False),
        AccountMeta(SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(SYSVAR_RENT_ID, is_signer=False, is_writable=False),
    ]
    return Instruction(METADATA_PROGRAM_ID, data, accounts)


def build_permanent_renunciation_instruction(
    *, mint: Pubkey, current_authority: Pubkey
) -> Instruction:
    """Return the unsigned instruction that permanently disables minting.

    Passing ``new_authority=None`` is the SPL Token program's irreversible
    authority-renunciation operation.  There is no later recovery path.
    """

    return set_authority(
        SetAuthorityParams(
            program_id=TOKEN_PROGRAM_ID,
            account=mint,
            authority=AuthorityType.MINT_TOKENS,
            current_authority=current_authority,
            new_authority=None,
        )
    )


def build_trust_llama_setup(
    *,
    mint: Pubkey,
    payer: Pubkey,
    rent_exempt_mint_lamports: int,
    metadata_uri: str,
    token_account: Pubkey | None = None,
) -> TrustLlamaSetupPlan:
    """Build the complete unsigned setup sequence without network access.

    ``rent_exempt_mint_lamports`` must come from a separately reviewed offline
    preflight; this function deliberately does not query a Solana RPC endpoint.
    ``metadata_uri`` is included in immutable metadata and must therefore point
    to content that will not be changed after deployment.
    """

    if not isinstance(rent_exempt_mint_lamports, int) or isinstance(
        rent_exempt_mint_lamports, bool
    ):
        raise TrustLlamaSetupError("Mint rent must be an integer.")
    if rent_exempt_mint_lamports <= 0:
        raise TrustLlamaSetupError("Mint rent must be positive.")
    if not metadata_uri or len(metadata_uri.encode("utf-8")) > 200:
        raise TrustLlamaSetupError(
            "Immutable metadata URI must be non-empty and at most 200 UTF-8 bytes."
        )
    if not isinstance(mint, Pubkey) or not isinstance(payer, Pubkey):
        raise TrustLlamaSetupError("Mint and payer must be Solana public keys.")

    destination = token_account or get_associated_token_address(payer, mint)
    metadata = _metadata_address(mint)
    instructions = (
        create_account(
            CreateAccountParams(
                from_pubkey=payer,
                to_pubkey=mint,
                lamports=rent_exempt_mint_lamports,
                space=82,
                owner=TOKEN_PROGRAM_ID,
            )
        ),
        initialize_mint(
            InitializeMintParams(
                decimals=TLAMA_DECIMALS,
                program_id=TOKEN_PROGRAM_ID,
                mint=mint,
                mint_authority=payer,
                freeze_authority=None,
            )
        ),
        create_associated_token_account(payer, payer, mint),
        mint_to(
            MintToParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=mint,
                dest=destination,
                mint_authority=payer,
                amount=TLAMA_TOTAL_SUPPLY_BASE_UNITS,
            )
        ),
        _immutable_metadata_instruction(
            metadata=metadata,
            mint=mint,
            mint_authority=payer,
            payer=payer,
            uri=metadata_uri,
        ),
        build_permanent_renunciation_instruction(
            mint=mint, current_authority=payer
        ),
    )
    return TrustLlamaSetupPlan(
        mint=mint,
        payer=payer,
        token_account=destination,
        metadata_account=metadata,
        instructions=instructions,
    )


if __name__ == "__main__":
    raise SystemExit(
        "TrustLlama is an offline instruction builder. Import this module from a "
        "reviewed deployment tool; no network or transaction execution is provided."
    )