//! Offline launch-manifest validation; this module never opens a network connection.
use crate::{config, error::PoolError, state};
use solana_program::{program_error::ProgramError, pubkey::Pubkey};

pub struct Allocation {
    pub account: Pubkey,
    pub owner: Pubkey,
    pub amount: u64,
    pub is_pool_vault: bool,
    pub is_game_allocation: bool,
}

/// Every on-chain public ID is explicit so a manifest cannot substitute a
/// compatible-looking program or account.
pub struct LaunchManifest<'a> {
    pub pool_adapter_program_id: Pubkey,
    pub transfer_hook_program_id: Pubkey,
    pub tlama_mint: Pubkey,
    pub tlama_vault: Pubkey,
    pub wsol_vault: Pubkey,
    pub pool_authority: Pubkey,
    pub mint_authority_present: bool,
    pub freeze_authority_present: bool,
    pub allocations: &'a [Allocation],
}

fn has_expected_public_ids(manifest: &LaunchManifest<'_>) -> bool {
    manifest.pool_adapter_program_id == config::ADAPTER_PROGRAM_ID
        && manifest.transfer_hook_program_id == config::HOOK_PROGRAM_ID
        && manifest.tlama_mint == config::TLAMA_MINT
        && manifest.tlama_vault == config::TLAMA_VAULT
        && manifest.wsol_vault == config::WSOL_VAULT
        && manifest.pool_authority == config::POOL_AUTHORITY
}

/// Checks the static evidence that must be collected before any launch.
pub fn verify(manifest: &LaunchManifest<'_>) -> Result<(), ProgramError> {
    if !has_expected_public_ids(manifest)
        || manifest.mint_authority_present
        || manifest.freeze_authority_present
    {
        return Err(PoolError::InvalidMint.into());
    }

    let mut sum = 0u64;
    let mut vaults = 0usize;
    let mut game_accounts = 0usize;
    let mut game_total = 0u64;
    for (index, allocation) in manifest.allocations.iter().enumerate() {
        if manifest.allocations[..index]
            .iter()
            .any(|previous| previous.account == allocation.account)
        {
            return Err(PoolError::InvalidAccount.into());
        }
        sum = sum
            .checked_add(allocation.amount)
            .ok_or(PoolError::MathOverflow)?;

        if allocation.is_pool_vault {
            // A vault is a distinct role: it cannot also claim game tokens.
            if allocation.is_game_allocation
                || allocation.account != config::TLAMA_VAULT
                || allocation.owner != config::POOL_AUTHORITY
                || allocation.amount <= config::MAX_WALLET
            {
                return Err(PoolError::InvalidAccount.into());
            }
            vaults += 1;
        } else {
            // Only the compiled vault may be controlled by the pool authority.
            // All ordinary allocations must be held in their owner's canonical
            // Token-2022 ATA, so the wallet cap cannot be fragmented.
            if allocation.owner == config::POOL_AUTHORITY
                || allocation.account == config::TLAMA_VAULT
                || allocation.account != state::canonical_tlama_ata(&allocation.owner)
            {
                return Err(PoolError::InvalidAccount.into());
            }
            if allocation.amount > config::MAX_WALLET {
                return Err(PoolError::LimitExceeded.into());
            }
            if allocation.is_game_allocation {
                game_accounts += 1;
                game_total = game_total
                    .checked_add(allocation.amount)
                    .ok_or(PoolError::MathOverflow)?;
            }
        }
    }
    if sum != config::MAX_SUPPLY
        || vaults != 1
        || game_accounts < 10
        || game_total != 100_000_000 * config::ONE_TLAMA
    {
        return Err(PoolError::InvalidAmount.into());
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn account(seed: u8) -> Pubkey {
        Pubkey::new_from_array([seed; 32])
    }

    fn valid_allocations() -> Vec<Allocation> {
        let mut allocations = vec![Allocation {
            account: config::TLAMA_VAULT,
            owner: config::POOL_AUTHORITY,
            amount: 890_000_000 * config::ONE_TLAMA,
            is_pool_vault: true,
            is_game_allocation: false,
        }];
        for seed in 1..=10 {
            allocations.push(Allocation {
                account: state::canonical_tlama_ata(&account(seed)),
                owner: account(seed),
                amount: 10_000_000 * config::ONE_TLAMA,
                is_pool_vault: false,
                is_game_allocation: true,
            });
        }
        allocations.push(Allocation {
            account: state::canonical_tlama_ata(&account(11)),
            owner: account(11),
            amount: 10_000_000 * config::ONE_TLAMA,
            is_pool_vault: false,
            is_game_allocation: false,
        });
        allocations
    }

    fn manifest(allocations: &[Allocation]) -> LaunchManifest<'_> {
        LaunchManifest {
            pool_adapter_program_id: config::ADAPTER_PROGRAM_ID,
            transfer_hook_program_id: config::HOOK_PROGRAM_ID,
            tlama_mint: config::TLAMA_MINT,
            tlama_vault: config::TLAMA_VAULT,
            wsol_vault: config::WSOL_VAULT,
            pool_authority: config::POOL_AUTHORITY,
            mint_authority_present: false,
            freeze_authority_present: false,
            allocations,
        }
    }

    #[test]
    fn accepts_the_exact_release_manifest() {
        let allocations = valid_allocations();
        assert!(verify(&manifest(&allocations)).is_ok());
    }

    #[test]
    fn rejects_every_substituted_public_id_and_authorities() {
        let allocations = valid_allocations();
        let wrong = account(42);
        let mut release = manifest(&allocations);
        release.pool_adapter_program_id = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.transfer_hook_program_id = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.tlama_mint = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.tlama_vault = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.wsol_vault = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.pool_authority = wrong;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.mint_authority_present = true;
        assert!(verify(&release).is_err());
        release = manifest(&allocations);
        release.freeze_authority_present = true;
        assert!(verify(&release).is_err());
    }

    #[test]
    fn rejects_duplicate_accounts_and_invalid_roles() {
        let mut allocations = valid_allocations();
        allocations[1].account = config::TLAMA_VAULT;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[0].is_game_allocation = true;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[0].amount = config::MAX_WALLET;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[0].account = account(77);
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[0].is_pool_vault = false;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[1].amount = config::MAX_WALLET + 1;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[1].owner = config::POOL_AUTHORITY;
        assert!(verify(&manifest(&allocations)).is_err());
    }

    #[test]
    fn rejects_noncanonical_account_and_owner_substitution() {
        let mut allocations = valid_allocations();
        allocations[1].account = account(77);
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        // The account remains an ATA, but it belongs to the original owner,
        // not the substituted one recorded in the manifest.
        allocations[1].owner = account(77);
        assert!(verify(&manifest(&allocations)).is_err());
    }

    #[test]
    fn rejects_bad_game_distribution_supply_and_overflow() {
        let mut allocations = valid_allocations();
        allocations[10].is_game_allocation = false;
        assert!(verify(&manifest(&allocations)).is_err());

        let mut allocations = valid_allocations();
        allocations[11].amount -= 1;
        assert!(verify(&manifest(&allocations)).is_err());

        let overflow = [
            Allocation {
                account: config::TLAMA_VAULT,
                owner: config::POOL_AUTHORITY,
                amount: u64::MAX,
                is_pool_vault: true,
                is_game_allocation: false,
            },
            Allocation {
                account: account(99),
                owner: account(99),
                amount: 1,
                is_pool_vault: false,
                is_game_allocation: false,
            },
        ];
        assert!(verify(&manifest(&overflow)).is_err());
    }
}
