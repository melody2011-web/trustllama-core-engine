use crate::{config::BPS, error::PoolError};
use solana_program::program_error::ProgramError;

pub fn ceil_bps(amount: u64, rate: u64) -> Result<u64, ProgramError> {
    if amount == 0 {
        return Err(PoolError::InvalidAmount.into());
    }
    let n = (amount as u128)
        .checked_mul(rate as u128)
        .and_then(|v| v.checked_add(BPS as u128 - 1))
        .ok_or(PoolError::MathOverflow)?;
    u64::try_from(n / BPS as u128).map_err(|_| PoolError::MathOverflow.into())
}

/// Constant-product output. The pool fee remains in the input reserve.
pub fn cp_out(
    input: u64,
    reserve_in: u64,
    reserve_out: u64,
    fee_bps: u64,
) -> Result<u64, ProgramError> {
    if input == 0 || reserve_in == 0 || reserve_out == 0 || fee_bps >= BPS {
        return Err(PoolError::InvalidAmount.into());
    }
    let effective = (input as u128)
        .checked_mul((BPS - fee_bps) as u128)
        .ok_or(PoolError::MathOverflow)?;
    let denominator = (reserve_in as u128)
        .checked_mul(BPS as u128)
        .and_then(|v| v.checked_add(effective))
        .ok_or(PoolError::MathOverflow)?;
    let out = effective
        .checked_mul(reserve_out as u128)
        .ok_or(PoolError::MathOverflow)?
        / denominator;
    let out = u64::try_from(out).map_err(|_| PoolError::MathOverflow)?;
    if out == 0 || out >= reserve_out {
        return Err(PoolError::InvalidAmount.into());
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn python_fee_vectors_and_edges() {
        assert_eq!(ceil_bps(1, 50).unwrap(), 1);
        assert_eq!(ceil_bps(10_000, 50).unwrap(), 50);
        assert_eq!(ceil_bps(10_001, 50).unwrap(), 51);
        assert_eq!(ceil_bps(10_000, 100).unwrap(), 100);
        assert_eq!(ceil_bps(10_001, 100).unwrap(), 101);
        assert!(ceil_bps(0, 50).is_err());
    }
    #[test]
    fn invariant_and_reserve_edges() {
        let x = 1_000_000u64;
        let y = 2_000_000u64;
        let input = 50_000u64;
        let out = cp_out(input, x, y, 25).unwrap();
        assert!((x as u128 + input as u128) * (y as u128 - out as u128) >= x as u128 * y as u128);
        assert!(cp_out(0, x, y, 25).is_err());
        assert!(cp_out(1, 0, y, 25).is_err());
        assert!(cp_out(1, x, 0, 25).is_err());
    }
    #[test]
    fn u64_inputs_use_u128_without_overflow() {
        assert!(cp_out(u64::MAX, u64::MAX, u64::MAX, 25).is_err());
        assert_eq!(ceil_bps(u64::MAX, 100).unwrap(), 184_467_440_737_095_517);
    }
}
