use crate::error::PoolError;
use solana_program::program_error::ProgramError;

pub const BUY_TAG: [u8; 8] = *b"TLAMABUY";
pub const SELL_TAG: [u8; 8] = *b"TLAMASEL";

pub enum PoolInstruction {
    Buy {
        wsol_in: u64,
        min_net_out: u64,
    },
    Sell {
        gross_tlama_in: u64,
        min_wsol_out: u64,
    },
}

pub fn unpack(data: &[u8]) -> Result<PoolInstruction, ProgramError> {
    let tag: [u8; 8] = data
        .get(..8)
        .ok_or(PoolError::InvalidInstruction)?
        .try_into()
        .unwrap();
    if data.len() != 24 {
        return Err(PoolError::InvalidInstruction.into());
    }
    let a = u64::from_le_bytes(data[8..16].try_into().unwrap());
    let b = u64::from_le_bytes(data[16..24].try_into().unwrap());
    match tag {
        BUY_TAG => Ok(PoolInstruction::Buy {
            wsol_in: a,
            min_net_out: b,
        }),
        SELL_TAG => Ok(PoolInstruction::Sell {
            gross_tlama_in: a,
            min_wsol_out: b,
        }),
        _ => Err(PoolError::InvalidInstruction.into()),
    }
}
