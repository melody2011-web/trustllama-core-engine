use crate::{config, error::PoolError};
use solana_program::{account_info::AccountInfo, program_error::ProgramError, program_pack::Pack};
use spl_token_2022::{
    extension::{
        transfer_hook::TransferHook, BaseStateWithExtensions, ExtensionType, StateWithExtensions,
    },
    state::{Account, Mint},
};

/// The sole Token-2022 account at which an ordinary owner may hold TLAMA.
/// The pool vault is intentionally not checked with this helper.
pub fn canonical_tlama_ata(
    owner: &solana_program::pubkey::Pubkey,
) -> solana_program::pubkey::Pubkey {
    let token_program = spl_token_2022::id();
    solana_program::pubkey::Pubkey::find_program_address(
        &[
            owner.as_ref(),
            token_program.as_ref(),
            config::TLAMA_MINT.as_ref(),
        ],
        &solana_program::pubkey!("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
    )
    .0
}

pub fn token_account(info: &AccountInfo) -> Result<Account, ProgramError> {
    if info.owner != &spl_token_2022::id() {
        return Err(PoolError::InvalidAccount.into());
    }
    let data = info.try_borrow_data()?;
    let state = StateWithExtensions::<Account>::unpack(&data)?;
    if state.base.mint != config::TLAMA_MINT {
        return Err(PoolError::InvalidMint.into());
    }
    Ok(state.base)
}

pub fn validate_mint(info: &AccountInfo) -> Result<(), ProgramError> {
    if info.key != &config::TLAMA_MINT || info.owner != &spl_token_2022::id() {
        return Err(PoolError::InvalidMint.into());
    }
    let data = info.try_borrow_data()?;
    let state = StateWithExtensions::<Mint>::unpack(&data)?;
    if state.base.decimals != config::DECIMALS
        || state.base.supply > config::MAX_SUPPLY
        || state.base.mint_authority.is_some()
        || state.base.freeze_authority.is_some()
    {
        return Err(PoolError::InvalidMint.into());
    }
    let hook = state.get_extension::<TransferHook>()?;
    if state.get_extension_types()? != [ExtensionType::TransferHook] {
        // Fail closed against permanent delegates, transfer fees, close
        // authorities, pausing, or other mint-level policy changes.
        return Err(PoolError::InvalidMint.into());
    }
    let expected: Option<solana_program::pubkey::Pubkey> = hook.program_id.into();
    if expected != Some(config::HOOK_PROGRAM_ID) {
        return Err(PoolError::InvalidMint.into());
    }
    Ok(())
}

pub fn token2022_vault_locked(account: &Account) -> bool {
    account.delegate.is_none() && account.close_authority.is_none()
}

pub fn legacy_vault_locked(account: &spl_token::state::Account) -> bool {
    account.delegate.is_none() && account.close_authority.is_none()
}

pub fn legacy_account(info: &AccountInfo) -> Result<spl_token::state::Account, ProgramError> {
    if info.owner != &spl_token::id() {
        return Err(PoolError::InvalidAccount.into());
    }
    spl_token::state::Account::unpack(&info.try_borrow_data()?)
}
