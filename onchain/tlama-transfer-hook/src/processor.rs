use crate::{
    config,
    error::PoolError,
    instruction::{BUY_TAG, INIT_TAG, SELL_TAG},
    state,
};
use solana_program::{
    account_info::AccountInfo,
    entrypoint::ProgramResult,
    program_error::ProgramError,
    rent::Rent,
    system_instruction,
    sysvar::{
        instructions::{load_current_index_checked, load_instruction_at_checked},
        Sysvar,
    },
};
use spl_tlv_account_resolution::{account::ExtraAccountMeta, state::ExtraAccountMetaList};
use spl_token_2022::extension::{
    transfer_hook::TransferHookAccount, BaseStateWithExtensions, StateWithExtensions,
};
use spl_transfer_hook_interface::{
    collect_extra_account_metas_signer_seeds, get_extra_account_metas_address_and_bump_seed,
    instruction::{ExecuteInstruction, TransferHookInstruction},
};

pub fn process(
    program_id: &solana_program::pubkey::Pubkey,
    accounts: &[AccountInfo],
    data: &[u8],
) -> ProgramResult {
    if program_id != &config::HOOK_PROGRAM_ID {
        return Err(ProgramError::IncorrectProgramId);
    }
    if data == INIT_TAG {
        return initialize(accounts);
    }
    match TransferHookInstruction::unpack(data) {
        Ok(TransferHookInstruction::Execute { amount }) => execute(accounts, amount),
        _ => Err(PoolError::InvalidInstruction.into()),
    }
}

fn initialize(accounts: &[AccountInfo]) -> ProgramResult {
    let [payer, validation, mint, system] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    if !payer.is_signer || !payer.is_writable || system.key != &solana_program::system_program::id()
    {
        return Err(PoolError::InvalidAccount.into());
    }
    state::validate_mint(mint)?;
    let (expected, bump) =
        get_extra_account_metas_address_and_bump_seed(mint.key, &config::HOOK_PROGRAM_ID);
    if validation.key != &expected {
        return Err(PoolError::InvalidAccount.into());
    }
    let metas = [ExtraAccountMeta::new_with_pubkey(
        &solana_program::sysvar::instructions::id(),
        false,
        false,
    )?];
    let size = ExtraAccountMetaList::size_of(metas.len())?;
    if validation.owner == &config::HOOK_PROGRAM_ID {
        let data = validation.try_borrow_data()?;
        let mut expected_data = vec![0; size];
        ExtraAccountMetaList::init::<ExecuteInstruction>(&mut expected_data, &metas)?;
        return if data.as_ref() == expected_data {
            Ok(())
        } else {
            Err(PoolError::AlreadyInitialized.into())
        };
    }
    if validation.lamports() != 0 || !validation.data_is_empty() {
        return Err(PoolError::InvalidAccount.into());
    }
    let bump_bytes = [bump];
    let seeds = collect_extra_account_metas_signer_seeds(mint.key, &bump_bytes);
    invoke_signed_create(payer, validation, system, size, &seeds)?;
    ExtraAccountMetaList::init::<ExecuteInstruction>(&mut validation.try_borrow_mut_data()?, &metas)
}

fn invoke_signed_create<'a>(
    payer: &AccountInfo<'a>,
    validation: &AccountInfo<'a>,
    system: &AccountInfo<'a>,
    size: usize,
    seeds: &[&[u8]],
) -> ProgramResult {
    solana_program::program::invoke_signed(
        &system_instruction::create_account(
            payer.key,
            validation.key,
            Rent::get()?.minimum_balance(size),
            size as u64,
            &config::HOOK_PROGRAM_ID,
        ),
        &[payer.clone(), validation.clone(), system.clone()],
        &[seeds],
    )
}

fn execute(accounts: &[AccountInfo], amount: u64) -> ProgramResult {
    let [source, mint, destination, authority, validation, instructions] = accounts else {
        return Err(ProgramError::NotEnoughAccountKeys);
    };
    if amount == 0 || amount > config::MAX_GROSS || source.key == destination.key {
        return Err(PoolError::InvalidAmount.into());
    }
    state::validate_mint(mint)?;
    if validation.key
        != &spl_transfer_hook_interface::get_extra_account_metas_address(
            mint.key,
            &config::HOOK_PROGRAM_ID,
        )
        || validation.owner != &config::HOOK_PROGRAM_ID
        || instructions.key != &solana_program::sysvar::instructions::id()
    {
        return Err(PoolError::InvalidHookContext.into());
    }
    let mut token_accounts = Vec::with_capacity(2);
    for endpoint in [source, destination] {
        if endpoint.owner != &spl_token_2022::id() {
            return Err(PoolError::InvalidAccount.into());
        }
        let data = endpoint.try_borrow_data()?;
        let account = StateWithExtensions::<spl_token_2022::state::Account>::unpack(&data)?;
        let transferring: bool = account
            .get_extension::<TransferHookAccount>()?
            .transferring
            .into();
        if !transferring || account.base.mint != config::TLAMA_MINT {
            return Err(PoolError::InvalidHookContext.into());
        }
        token_accounts.push(account.base);
    }
    let source_vault = source.key == &config::TLAMA_VAULT;
    let destination_vault = destination.key == &config::TLAMA_VAULT;
    if source_vault {
        if token_accounts[0].owner != config::POOL_AUTHORITY
            || !state::token2022_vault_locked(&token_accounts[0])
            || token_accounts[1].owner == config::POOL_AUTHORITY
            || destination.key != &state::canonical_tlama_ata(&token_accounts[1].owner)
            || authority.key != &config::POOL_AUTHORITY
        {
            return Err(PoolError::InvalidAuthority.into());
        }
    } else if destination_vault {
        if token_accounts[1].owner != config::POOL_AUTHORITY
            || !state::token2022_vault_locked(&token_accounts[1])
            || source.key != &state::canonical_tlama_ata(&token_accounts[0].owner)
            || authority.key != &token_accounts[0].owner
        {
            return Err(PoolError::InvalidAuthority.into());
        }
    } else if source.key != &state::canonical_tlama_ata(&token_accounts[0].owner)
        || destination.key != &state::canonical_tlama_ata(&token_accounts[1].owner)
        || authority.key != &token_accounts[0].owner
    {
        return Err(PoolError::InvalidAuthority.into());
    }
    if source_vault || destination_vault {
        if source_vault == destination_vault {
            return Err(PoolError::InvalidHookContext.into());
        }
        validate_adapter_top_level(
            instructions,
            source,
            mint,
            destination,
            authority,
            validation,
            source_vault,
        )?;
    } else if state::token_account(destination)?.amount > config::MAX_WALLET {
        return Err(PoolError::LimitExceeded.into());
    }
    Ok(())
}

fn validate_adapter_top_level(
    instructions: &AccountInfo,
    source: &AccountInfo,
    mint: &AccountInfo,
    destination: &AccountInfo,
    authority: &AccountInfo,
    validation: &AccountInfo,
    buy: bool,
) -> ProgramResult {
    let index = load_current_index_checked(instructions)? as usize;
    let top = load_instruction_at_checked(index, instructions)?;
    let positions = if buy {
        (3, 4, BUY_TAG)
    } else {
        (1, 2, SELL_TAG)
    };
    if !top_level_layout_allowed(
        &top,
        source.key,
        mint.key,
        destination.key,
        authority.key,
        validation.key,
        positions.0,
        positions.1,
        positions.2,
    ) {
        return Err(PoolError::InvalidHookContext.into());
    }
    Ok(())
}

fn top_level_layout_allowed(
    top: &solana_program::instruction::Instruction,
    source: &solana_program::pubkey::Pubkey,
    mint: &solana_program::pubkey::Pubkey,
    destination: &solana_program::pubkey::Pubkey,
    authority: &solana_program::pubkey::Pubkey,
    validation: &solana_program::pubkey::Pubkey,
    source_position: usize,
    destination_position: usize,
    tag: [u8; 8],
) -> bool {
    top.program_id == config::ADAPTER_PROGRAM_ID
        && top.data.len() == 24
        && top.data[..8] == tag
        && top.accounts.len() == 12
        && top.accounts[0].is_signer
        && !top.accounts[0].is_writable
        && top.accounts[1..6]
            .iter()
            .all(|meta| meta.is_writable && !meta.is_signer)
        && top.accounts[6..]
            .iter()
            .all(|meta| !meta.is_writable && !meta.is_signer)
        && top.accounts[source_position].pubkey == *source
        && top.accounts[destination_position].pubkey == *destination
        && top.accounts[5].pubkey == *mint
        && top.accounts[6].pubkey == config::POOL_AUTHORITY
        && top.accounts[7].pubkey == *validation
        && top.accounts[8].pubkey == solana_program::sysvar::instructions::id()
        && top.accounts[9].pubkey == spl_token_2022::id()
        && top.accounts[10].pubkey == spl_token::id()
        && top.accounts[11].pubkey == config::HOOK_PROGRAM_ID
        && if source_position == 3 {
            top.accounts[2].pubkey == config::WSOL_VAULT && top.accounts[6].pubkey == *authority
        } else {
            top.accounts[0].pubkey == *authority
                && top.accounts[2].pubkey == config::TLAMA_VAULT
                && top.accounts[3].pubkey == config::WSOL_VAULT
        }
}

#[cfg(test)]
mod tests {
    use super::*;
    use solana_program::{
        instruction::{AccountMeta, Instruction},
        pubkey::Pubkey,
    };

    fn buy(source: Pubkey, destination: Pubkey, validation: Pubkey) -> Instruction {
        let mut data = BUY_TAG.to_vec();
        data.extend_from_slice(&[0; 16]);
        let mut accounts = (0..12)
            .map(|_| AccountMeta::new_readonly(Pubkey::new_unique(), false))
            .collect::<Vec<_>>();
        accounts[0] = AccountMeta::new_readonly(Pubkey::new_unique(), true);
        for account in &mut accounts[1..6] {
            account.is_writable = true;
        }
        accounts[2] = AccountMeta::new(config::WSOL_VAULT, false);
        accounts[3] = AccountMeta::new(source, false);
        accounts[4] = AccountMeta::new(destination, false);
        accounts[5] = AccountMeta::new(config::TLAMA_MINT, false);
        accounts[6] = AccountMeta::new_readonly(config::POOL_AUTHORITY, false);
        accounts[7] = AccountMeta::new_readonly(validation, false);
        accounts[8] = AccountMeta::new_readonly(solana_program::sysvar::instructions::id(), false);
        accounts[9] = AccountMeta::new_readonly(spl_token_2022::id(), false);
        accounts[10] = AccountMeta::new_readonly(spl_token::id(), false);
        accounts[11] = AccountMeta::new_readonly(config::HOOK_PROGRAM_ID, false);
        Instruction {
            program_id: config::ADAPTER_PROGRAM_ID,
            accounts,
            data,
        }
    }

    #[test]
    fn malformed_adapter_data_or_direction_is_rejected() {
        let source = config::TLAMA_VAULT;
        let destination = Pubkey::new_unique();
        let validation = spl_transfer_hook_interface::get_extra_account_metas_address(
            &config::TLAMA_MINT,
            &config::HOOK_PROGRAM_ID,
        );
        let mut top = buy(source, destination, validation);
        assert!(top_level_layout_allowed(
            &top,
            &source,
            &config::TLAMA_MINT,
            &destination,
            &config::POOL_AUTHORITY,
            &validation,
            3,
            4,
            BUY_TAG
        ));
        top.data.pop();
        assert!(!top_level_layout_allowed(
            &top,
            &source,
            &config::TLAMA_MINT,
            &destination,
            &config::POOL_AUTHORITY,
            &validation,
            3,
            4,
            BUY_TAG
        ));
        let top = buy(destination, source, validation);
        assert!(!top_level_layout_allowed(
            &top,
            &source,
            &config::TLAMA_MINT,
            &destination,
            &config::POOL_AUTHORITY,
            &validation,
            3,
            4,
            BUY_TAG
        ));
        let mut top = buy(source, destination, validation);
        top.accounts[4].is_writable = false;
        assert!(!top_level_layout_allowed(
            &top,
            &source,
            &config::TLAMA_MINT,
            &destination,
            &config::POOL_AUTHORITY,
            &validation,
            3,
            4,
            BUY_TAG
        ));
    }
}
