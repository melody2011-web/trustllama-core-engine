use std::{env, fs, path::PathBuf};

const FIXTURES: [[u8; 32]; 5] = [[0x11; 32], [0x22; 32], [0x33; 32], [0x44; 32], [0x55; 32]];
const DEVNET_IDS: &str = "../devnet-ids.txt";
const PRODUCTION_IDS: &str = "../production-ids.txt";

fn main() {
    let devnet = env::var_os("CARGO_FEATURE_DEVNET_IDS").is_some();
    let production = env::var_os("CARGO_FEATURE_PRODUCTION_IDS").is_some();
    if devnet && production {
        panic!("devnet-ids and production-ids are mutually exclusive");
    }
    for name in [
        "TLAMA_ADAPTER_PROGRAM_ID",
        "TLAMA_HOOK_PROGRAM_ID",
        "TLAMA_MINT_ID",
        "TLAMA_VAULT_ID",
        "WSOL_VAULT_ID",
        "POOL_AUTHORITY_ID",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    let configured_file = if production {
        Some(PRODUCTION_IDS)
    } else if devnet {
        Some(DEVNET_IDS)
    } else {
        None
    };
    let ids_file = configured_file
        .map(|name| PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap()).join(name));
    if let Some(path) = &ids_file {
        println!("cargo:rerun-if-changed={}", path.display());
    }
    let names = [
        "TLAMA_ADAPTER_PROGRAM_ID",
        "TLAMA_HOOK_PROGRAM_ID",
        "TLAMA_MINT_ID",
        "TLAMA_VAULT_ID",
        "WSOL_VAULT_ID",
    ];
    let keys = [
        "adapter",
        "hook",
        "tlamaMint",
        "tlamaVault",
        "wsolVault",
        "poolAuthority",
    ];
    let configured_values = if let Some(ids_file) = &ids_file {
        if names
            .iter()
            .chain(std::iter::once(&"POOL_AUTHORITY_ID"))
            .any(|name| env::var_os(name).is_some())
        {
            panic!("network ID builds read only their committed ID file; environment ID overrides are forbidden");
        }
        let text = fs::read_to_string(&ids_file).unwrap_or_else(|_| {
            panic!("network ID build requires committed {}", ids_file.display())
        });
        let lines: Vec<_> = text.lines().collect();
        if lines.len() != keys.len() {
            panic!("network ID file must contain exactly six key=value lines");
        }
        lines
            .iter()
            .zip(keys)
            .map(|(line, key)| {
                let (actual, value) = line
                    .split_once('=')
                    .unwrap_or_else(|| panic!("network ID file contains malformed line"));
                if actual != key || value.is_empty() {
                    panic!(
                        "network ID file has missing, reordered, or placeholder value for {key}"
                    );
                }
                let decoded = bs58::decode(value)
                    .into_vec()
                    .unwrap_or_else(|_| panic!("network ID value for {key} is not valid base58"));
                <[u8; 32]>::try_from(decoded).unwrap_or_else(|_| {
                    panic!("network ID value for {key} must decode to exactly 32 bytes")
                })
            })
            .collect::<Vec<_>>()
    } else {
        Vec::new()
    };
    let mut ids = Vec::new();
    for (i, name) in names.iter().enumerate() {
        let bytes = if configured_file.is_some() {
            configured_values[i]
        } else {
            match env::var(name) {
                Ok(value) => {
                    let decoded = bs58::decode(&value)
                        .into_vec()
                        .unwrap_or_else(|_| panic!("{name} is not valid base58"));
                    <[u8; 32]>::try_from(decoded)
                        .unwrap_or_else(|_| panic!("{name} must decode to exactly 32 bytes"))
                }
                Err(_) => FIXTURES[i],
            }
        };
        if configured_file.is_some() && FIXTURES.contains(&bytes) {
            panic!("{name} is a conspicuous host-test fixture");
        }
        if ids.contains(&bytes) {
            panic!("all configured public IDs must be distinct");
        }
        ids.push(bytes);
    }
    let derived = solana_pubkey::Pubkey::find_program_address(
        &[b"pool-authority"],
        &solana_pubkey::Pubkey::new_from_array(ids[0]),
    );
    let authority = if configured_file.is_some() {
        configured_values[5]
    } else {
        match env::var("POOL_AUTHORITY_ID") {
            Ok(value) => {
                let decoded = bs58::decode(&value)
                    .into_vec()
                    .expect("POOL_AUTHORITY_ID is not valid base58");
                <[u8; 32]>::try_from(decoded)
                    .expect("POOL_AUTHORITY_ID must decode to exactly 32 bytes")
            }
            Err(_) => derived.0.to_bytes(),
        }
    };
    if authority != derived.0.to_bytes() {
        panic!("POOL_AUTHORITY_ID is not the pool-authority PDA for TLAMA_ADAPTER_PROGRAM_ID");
    }
    if ids.contains(&authority) {
        panic!("all configured public IDs must be distinct");
    }
    ids.push(authority);
    let labels = [
        "ADAPTER_PROGRAM_ID",
        "HOOK_PROGRAM_ID",
        "TLAMA_MINT",
        "TLAMA_VAULT",
        "WSOL_VAULT",
        "POOL_AUTHORITY",
    ];
    let generated = labels
        .iter()
        .zip(ids)
        .map(|(label, bytes)| {
            format!("pub const {label}: Pubkey = Pubkey::new_from_array({bytes:?});\n")
        })
        .collect::<String>();
    fs::write(
        PathBuf::from(env::var("OUT_DIR").unwrap()).join("ids.rs"),
        format!("pub const PROGRAM_ID: Pubkey = ADAPTER_PROGRAM_ID;\n{generated}"),
    )
    .unwrap();
}
