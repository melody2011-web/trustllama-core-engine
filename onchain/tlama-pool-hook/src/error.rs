use solana_program::program_error::ProgramError;

#[repr(u32)]
pub enum PoolError {
    InvalidInstruction = 0,
    InvalidAccount,
    InvalidMint,
    InvalidAuthority,
    InvalidAmount,
    LimitExceeded,
    MathOverflow,
    Slippage,
    InvalidHookContext,
    AliasedAccount,
    AlreadyInitialized,
}

impl From<PoolError> for ProgramError {
    fn from(value: PoolError) -> Self {
        ProgramError::Custom(value as u32)
    }
}
