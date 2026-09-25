use solana_program::pubkey::Pubkey;

include!(concat!(env!("OUT_DIR"), "/ids.rs"));

pub const DECIMALS: u8 = 9;
pub const ONE_TLAMA: u64 = 1_000_000_000;
pub const MAX_GROSS: u64 = 5_000_000 * ONE_TLAMA;
pub const MAX_WALLET: u64 = 10_000_000 * ONE_TLAMA;
pub const MAX_SUPPLY: u64 = 1_000_000_000 * ONE_TLAMA;
pub const BPS: u64 = 10_000;
pub const BUY_BURN_BPS: u64 = 50;
pub const SELL_BURN_BPS: u64 = 100;
pub const POOL_FEE_BPS: u64 = 25;
pub const AUTHORITY_SEED: &[u8] = b"pool-authority";
