use crate::{
    config,
    error::PoolError,
    instruction::{self, PoolInstruction},
    math, state,
};
use solana_program::{
    account_info::AccountInfo,
    entrypoint::ProgramResult,
    program::{invoke, invoke_signed},
    program_error::ProgramError,
    pubkey::Pubkey,
};
pub fn process(program_id: &Pubkey, accounts: &[AccountInfo], data: &[u8]) -> ProgramResult {
    if program_id != &config::ADAPTER_PROGRAM_ID {
        return Err(ProgramError::IncorrectProgramId);
    }
    match instruction::unpack(data)? {
        PoolInstruction::Buy {
            wsol_in,
            min_net_out,
        } => buy(accounts, wsol_in, min_net_out),
        PoolInstruction::Sell {
            gross_tlama_in,
            min_wsol_out,
        } => sell(accounts, gross_tlama_in, min_wsol_out),
    }
}

fn distinct(accounts: &[&AccountInfo]) -> ProgramResult {
    let keys = accounts
        .iter()
        .map(|account| *account.key)
        .collect::<Vec<_>>();
    if !keys_are_distinct(&keys) {
        return Err(PoolError::AliasedAccount.into());
    }
    Ok(())
}

fn keys_are_distinct(keys: &[Pubkey]) -> bool {
    keys.iter()
        .enumerate()
        .all(|(index, key)| !keys[..index].contains(key))
}

fn common<'a, 'b>(
    accounts: &'b [AccountInfo<'a>],
) -> Result<[&'b AccountInfo<'a>; 12], ProgramError> {
    let refs: [&AccountInfo<'a>; 12] = accounts
        .get(..12)
        .ok_or(ProgramError::NotEnoughAccountKeys)?
        .iter()
        .collect::<Vec<_>>()
        .try_into()
        .map_err(|_| ProgramError::NotEnoughAccountKeys)?;
    if !refs[0].is_signer
        || refs[5].key != &config::TLAMA_MINT
        || refs[6].key != &config::POOL_AUTHORITY
        || refs[7].key
            != &spl_transfer_hook_interface::get_extra_account_metas_address(
                &config::TLAMA_MINT,
                &config::HOOK_PROGRAM_ID,
            )
        || refs[7].owner != &config::HOOK_PROGRAM_ID
        || refs[8].key != &solana_program::sysvar::instructions::id()
        || refs[9].key != &spl_token_2022::id()
        || refs[10].key != &spl_token::id()
        || refs[11].key != &config::HOOK_PROGRAM_ID
        || !refs[11].executable
    {
        return Err(PoolError::InvalidAccount.into());
    }
    let (derived, _) =
        Pubkey::find_program_address(&[config::AUTHORITY_SEED], &config::ADAPTER_PROGRAM_ID);
    if derived != config::POOL_AUTHORITY {
        return Err(PoolError::InvalidAuthority.into());
    }
    distinct(&refs[..9])?;
    state::validate_mint(refs[5])?;
    Ok(refs)
}

fn token2022_transfer<'a>(
    source: &AccountInfo<'a>,
    mint: &AccountInfo<'a>,
    destination: &AccountInfo<'a>,
    authority: &AccountInfo<'a>,
    validation: &AccountInfo<'a>,
    instructions: &AccountInfo<'a>,
    token_program: &AccountInfo<'a>,
    hook_program: &AccountInfo<'a>,
    amount: u64,
    signer: Option<&[&[u8]]>,
) -> ProgramResult {
    let mut ix = spl_token_2022::instruction::transfer_checked(
        &spl_token_2022::id(),
        source.key,
        mint.key,
        destination.key,
        authority.key,
        &[],
        amount,
        config::DECIMALS,
    )?;
    let mut infos = vec![
        source.clone(),
        mint.clone(),
        destination.clone(),
        authority.clone(),
    ];
    let additional = [
        hook_program.clone(),
        validation.clone(),
        instructions.clone(),
    ];
    spl_transfer_hook_interface::onchain::add_extra_accounts_for_execute_cpi(
        &mut ix,
        &mut infos,
        &config::HOOK_PROGRAM_ID,
        source.clone(),
        mint.clone(),
        destination.clone(),
        authority.clone(),
        amount,
        &additional,
    )?;
    infos.push(token_program.clone());
    match signer {
        Some(seeds) => invoke_signed(&ix, &infos, &[seeds]),
        None => invoke(&ix, &infos),
    }
}

fn buy(accounts: &[AccountInfo], wsol_in: u64, min_net_out: u64) -> ProgramResult {
    let a = common(accounts)?;
    let [user, user_wsol, wsol_vault, tlama_vault, user_tlama, mint, authority, validation, instructions, token2022, legacy, hook_program] =
        a;
    if tlama_vault.key != &config::TLAMA_VAULT || wsol_vault.key != &config::WSOL_VAULT {
        return Err(PoolError::InvalidAccount.into());
    }
    let uw = state::legacy_account(user_wsol)?;
    let wv = state::legacy_account(wsol_vault)?;
    let tv = state::token_account(tlama_vault)?;
    let ut = state::token_account(user_tlama)?;
    if uw.owner != *user.key
        || uw.mint != spl_token::native_mint::id()
        || wv.owner != config::POOL_AUTHORITY
        || wv.mint != spl_token::native_mint::id()
        || tv.owner != config::POOL_AUTHORITY
        || !state::legacy_vault_locked(&wv)
        || !state::token2022_vault_locked(&tv)
        || ut.owner != *user.key
        || user_tlama.key != &state::canonical_tlama_ata(user.key)
    {
        return Err(PoolError::InvalidAuthority.into());
    }
    let gross = math::cp_out(wsol_in, wv.amount, tv.amount, config::POOL_FEE_BPS)?;
    if gross > config::MAX_GROSS {
        return Err(PoolError::LimitExceeded.into());
    }
    let burn = math::ceil_bps(gross, config::BUY_BURN_BPS)?;
    let net = gross.checked_sub(burn).ok_or(PoolError::MathOverflow)?;
    if net == 0
        || net < min_net_out
        || ut.amount.checked_add(net).ok_or(PoolError::MathOverflow)? > config::MAX_WALLET
    {
        return Err(PoolError::Slippage.into());
    }
    invoke(
        &spl_token::instruction::transfer(
            &spl_token::id(),
            user_wsol.key,
            wsol_vault.key,
            user.key,
            &[],
            wsol_in,
        )?,
        &[
            user_wsol.clone(),
            wsol_vault.clone(),
            user.clone(),
            legacy.clone(),
        ],
    )?;
    let (_, bump) =
        Pubkey::find_program_address(&[config::AUTHORITY_SEED], &config::ADAPTER_PROGRAM_ID);
    let bump_bytes = [bump];
    let seeds: &[&[u8]] = &[config::AUTHORITY_SEED, &bump_bytes];
    invoke_signed(
        &spl_token_2022::instruction::burn_checked(
            &spl_token_2022::id(),
            tlama_vault.key,
            mint.key,
            authority.key,
            &[],
            burn,
            config::DECIMALS,
        )?,
        &[
            tlama_vault.clone(),
            mint.clone(),
            authority.clone(),
            token2022.clone(),
        ],
        &[seeds],
    )?;
    token2022_transfer(
        tlama_vault,
        mint,
        user_tlama,
        authority,
        validation,
        instructions,
        token2022,
        hook_program,
        net,
        Some(seeds),
    )
}

fn sell(accounts: &[AccountInfo], gross: u64, min_wsol_out: u64) -> ProgramResult {
    let a = common(accounts)?;
    let [user, user_tlama, tlama_vault, wsol_vault, user_wsol, mint, authority, validation, instructions, token2022, legacy, hook_program] =
        a;
    if gross == 0
        || gross > config::MAX_GROSS
        || tlama_vault.key != &config::TLAMA_VAULT
        || wsol_vault.key != &config::WSOL_VAULT
    {
        return Err(PoolError::LimitExceeded.into());
    }
    let ut = state::token_account(user_tlama)?;
    let tv = state::token_account(tlama_vault)?;
    let wv = state::legacy_account(wsol_vault)?;
    let uw = state::legacy_account(user_wsol)?;
    if ut.owner != *user.key
        || tv.owner != config::POOL_AUTHORITY
        || wv.owner != config::POOL_AUTHORITY
        || !state::token2022_vault_locked(&tv)
        || !state::legacy_vault_locked(&wv)
        || uw.owner != *user.key
        || wv.mint != spl_token::native_mint::id()
        || uw.mint != spl_token::native_mint::id()
        || user_tlama.key != &state::canonical_tlama_ata(user.key)
    {
        return Err(PoolError::InvalidAuthority.into());
    }
    let burn = math::ceil_bps(gross, config::SELL_BURN_BPS)?;
    let net = gross.checked_sub(burn).ok_or(PoolError::MathOverflow)?;
    if ut.amount < gross {
        return Err(PoolError::InvalidAmount.into());
    }
    let out = math::cp_out(net, tv.amount, wv.amount, config::POOL_FEE_BPS)?;
    if out < min_wsol_out {
        return Err(PoolError::Slippage.into());
    }
    invoke(
        &spl_token_2022::instruction::burn_checked(
            &spl_token_2022::id(),
            user_tlama.key,
            mint.key,
            user.key,
            &[],
            burn,
            config::DECIMALS,
        )?,
        &[
            user_tlama.clone(),
            mint.clone(),
            user.clone(),
            token2022.clone(),
        ],
    )?;
    token2022_transfer(
        user_tlama,
        mint,
        tlama_vault,
        user,
        validation,
        instructions,
        token2022,
        hook_program,
        net,
        None,
    )?;
    let (_, bump) =
        Pubkey::find_program_address(&[config::AUTHORITY_SEED], &config::ADAPTER_PROGRAM_ID);
    let bump_bytes = [bump];
    let seeds: &[&[u8]] = &[config::AUTHORITY_SEED, &bump_bytes];
    invoke_signed(
        &spl_token::instruction::transfer(
            &spl_token::id(),
            wsol_vault.key,
            user_wsol.key,
            authority.key,
            &[],
            out,
        )?,
        &[
            wsol_vault.clone(),
            user_wsol.clone(),
            authority.clone(),
            legacy.clone(),
        ],
        &[seeds],
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duplicate_and_aliased_keys_are_rejected() {
        let a = Pubkey::new_from_array([7; 32]);
        let b = Pubkey::new_from_array([8; 32]);
        assert!(keys_are_distinct(&[a, b]));
        assert!(!keys_are_distinct(&[a, b, a]));
    }
}
