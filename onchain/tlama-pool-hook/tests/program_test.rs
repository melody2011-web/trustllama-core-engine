//! SBF replay proof for the immutable pool adapter and transfer hook.
//! This test uses no RPC and ProgramTest's in-memory bank only.
#![cfg(feature = "program-test")]

use {
    solana_account::Account,
    solana_instruction::{AccountMeta as HostAccountMeta, Instruction as HostInstruction},
    solana_keypair::Keypair,
    solana_program::{
        instruction::{AccountMeta, Instruction},
        program_option::COption,
        program_pack::Pack,
        pubkey::Pubkey,
    },
    solana_program_test::ProgramTest,
    solana_pubkey::Pubkey as HostPubkey,
    solana_signer::Signer,
    solana_transaction::Transaction,
    spl_token_2022::{
        extension::{ExtensionType, StateWithExtensions},
        instruction::AuthorityType,
        state::{Account as TokenAccount, Mint},
    },
    spl_transfer_hook_interface::get_extra_account_metas_address,
    tlama_pool_adapter::config,
    tlama_transfer_hook::{config as hook_config, instruction::INIT_TAG},
};

fn host_pubkey(key: &Pubkey) -> HostPubkey {
    HostPubkey::new_from_array(key.to_bytes())
}

fn production_pubkey(key: &HostPubkey) -> Pubkey {
    Pubkey::new_from_array(key.to_bytes())
}

fn production_key(keypair: &Keypair) -> Pubkey {
    production_pubkey(&keypair.pubkey())
}

fn tlama_ata(owner: &Pubkey) -> Pubkey {
    let token_program = spl_token_2022::id();
    Pubkey::find_program_address(
        &[
            owner.as_ref(),
            token_program.as_ref(),
            config::TLAMA_MINT.as_ref(),
        ],
        &solana_program::pubkey!("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"),
    )
    .0
}

fn host_instruction(instruction: Instruction) -> HostInstruction {
    HostInstruction {
        program_id: host_pubkey(&instruction.program_id),
        accounts: instruction
            .accounts
            .into_iter()
            .map(|meta| HostAccountMeta {
                pubkey: host_pubkey(&meta.pubkey),
                is_signer: meta.is_signer,
                is_writable: meta.is_writable,
            })
            .collect(),
        data: instruction.data,
    }
}

fn host_account(data: Vec<u8>, owner: Pubkey) -> Account {
    Account {
        lamports: 10_000_000,
        data,
        owner: host_pubkey(&owner),
        executable: false,
        rent_epoch: 0,
    }
}

fn runtime_test() -> ProgramTest {
    let mut test = ProgramTest::new(
        "tlama_pool_adapter",
        host_pubkey(&config::ADAPTER_PROGRAM_ID),
        None,
    );
    test.add_program(
        "tlama_transfer_hook",
        host_pubkey(&hook_config::HOOK_PROGRAM_ID),
        None,
    );
    test.add_program("spl_token_2022", host_pubkey(&spl_token_2022::id()), None);
    test.add_program("spl_token", host_pubkey(&spl_token::id()), None);
    test
}

#[test]
fn adapter_and_hook_ids_are_distinct() {
    assert_ne!(config::ADAPTER_PROGRAM_ID, hook_config::HOOK_PROGRAM_ID);
    assert_eq!(config::ADAPTER_PROGRAM_ID, hook_config::ADAPTER_PROGRAM_ID);
    assert_eq!(config::HOOK_PROGRAM_ID, hook_config::HOOK_PROGRAM_ID);
    assert_eq!(config::TLAMA_MINT, hook_config::TLAMA_MINT);
    assert_eq!(config::TLAMA_VAULT, hook_config::TLAMA_VAULT);
    assert_eq!(config::WSOL_VAULT, hook_config::WSOL_VAULT);
    assert_eq!(config::POOL_AUTHORITY, hook_config::POOL_AUTHORITY);
    let ids = [
        config::ADAPTER_PROGRAM_ID,
        hook_config::HOOK_PROGRAM_ID,
        config::TLAMA_MINT,
        config::TLAMA_VAULT,
        config::WSOL_VAULT,
        config::POOL_AUTHORITY,
    ];
    for (index, id) in ids.iter().enumerate() {
        assert!(!ids[..index].contains(id));
    }
    #[cfg(feature = "production-ids")]
    {
        let configured = include_str!("../../production-ids.txt")
            .lines()
            .map(|line| {
                line.split_once('=')
                    .expect("production ID line must be key=value")
                    .1
                    .parse::<Pubkey>()
                    .expect("production ID must be a public key")
            })
            .collect::<Vec<_>>();
        assert_eq!(configured, ids);
    }
}

fn init_instruction(payer: Pubkey, validation: Pubkey) -> Instruction {
    Instruction {
        program_id: hook_config::HOOK_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new(payer, true),
            AccountMeta::new(validation, false),
            AccountMeta::new(config::TLAMA_MINT, false),
            AccountMeta::new_readonly(solana_program::system_program::id(), false),
        ],
        data: INIT_TAG.to_vec(),
    }
}

fn add_uninitialized_hook_mint(test: &mut ProgramTest) {
    let len =
        ExtensionType::try_calculate_account_len::<Mint>(&[ExtensionType::TransferHook]).unwrap();
    test.add_account(
        host_pubkey(&config::TLAMA_MINT),
        host_account(vec![0; len], spl_token_2022::id()),
    );
}

fn add_uninitialized_hook_account(test: &mut ProgramTest, key: Pubkey) {
    let len = ExtensionType::try_calculate_account_len::<TokenAccount>(&[
        ExtensionType::TransferHookAccount,
    ])
    .unwrap();
    test.add_account(
        host_pubkey(&key),
        host_account(vec![0; len], spl_token_2022::id()),
    );
}

async fn initialize_hook_fixture(
    context: &mut solana_program_test::ProgramTestContext,
    mint_authority: &Keypair,
    accounts: &[(Pubkey, Pubkey, u64)],
) {
    let mut instructions = vec![
        spl_token_2022::extension::transfer_hook::instruction::initialize(
            &spl_token_2022::id(),
            &config::TLAMA_MINT,
            None,
            Some(hook_config::HOOK_PROGRAM_ID),
        )
        .unwrap(),
        spl_token_2022::instruction::initialize_mint2(
            &spl_token_2022::id(),
            &config::TLAMA_MINT,
            &production_key(mint_authority),
            None,
            config::DECIMALS,
        )
        .unwrap(),
    ];
    for (account, owner, _) in accounts {
        instructions.push(
            spl_token_2022::instruction::initialize_account3(
                &spl_token_2022::id(),
                account,
                &config::TLAMA_MINT,
                owner,
            )
            .unwrap(),
        );
    }
    for (account, _, amount) in accounts {
        if *amount != 0 {
            instructions.push(
                spl_token_2022::instruction::mint_to_checked(
                    &spl_token_2022::id(),
                    &config::TLAMA_MINT,
                    account,
                    &production_key(mint_authority),
                    &[],
                    *amount,
                    config::DECIMALS,
                )
                .unwrap(),
            );
        }
    }
    instructions.push(
        spl_token_2022::instruction::set_authority(
            &spl_token_2022::id(),
            &config::TLAMA_MINT,
            None,
            AuthorityType::MintTokens,
            &production_key(mint_authority),
            &[],
        )
        .unwrap(),
    );
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &instructions
                .into_iter()
                .map(host_instruction)
                .collect::<Vec<_>>(),
            Some(&context.payer.pubkey()),
            &[&context.payer, mint_authority],
            hash,
        ))
        .await
        .unwrap();
}

fn legacy_account_data(owner: Pubkey, amount: u64) -> Vec<u8> {
    let account = spl_token::state::Account {
        mint: spl_token::native_mint::id(),
        owner,
        amount,
        delegate: COption::None,
        state: spl_token::state::AccountState::Initialized,
        // Test reserve accounts are ordinary legacy SPL accounts whose mint is
        // the canonical WSOL mint; no native-account lamport synchronization
        // is exercised by pool pricing.
        is_native: COption::None,
        delegated_amount: 0,
        close_authority: COption::None,
    };
    let mut data = vec![0; spl_token::state::Account::LEN];
    spl_token::state::Account::pack(account, &mut data).unwrap();
    data
}

fn pool_instruction(
    user: Pubkey,
    user_wsol: Pubkey,
    user_tlama: Pubkey,
    tag: [u8; 8],
    a: u64,
    b: u64,
) -> Instruction {
    let mut data = tag.to_vec();
    data.extend_from_slice(&a.to_le_bytes());
    data.extend_from_slice(&b.to_le_bytes());
    Instruction {
        program_id: config::ADAPTER_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new_readonly(user, true),
            AccountMeta::new(user_wsol, false),
            AccountMeta::new(config::WSOL_VAULT, false),
            AccountMeta::new(config::TLAMA_VAULT, false),
            AccountMeta::new(user_tlama, false),
            AccountMeta::new(config::TLAMA_MINT, false),
            AccountMeta::new_readonly(config::POOL_AUTHORITY, false),
            AccountMeta::new_readonly(
                get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
                false,
            ),
            AccountMeta::new_readonly(solana_program::sysvar::instructions::id(), false),
            AccountMeta::new_readonly(spl_token_2022::id(), false),
            AccountMeta::new_readonly(spl_token::id(), false),
            AccountMeta::new_readonly(hook_config::HOOK_PROGRAM_ID, false),
        ],
        data,
    }
}

fn sell_instruction(
    user: Pubkey,
    user_tlama: Pubkey,
    user_wsol: Pubkey,
    gross: u64,
) -> Instruction {
    let mut data = b"TLAMASEL".to_vec();
    data.extend_from_slice(&gross.to_le_bytes());
    data.extend_from_slice(&1u64.to_le_bytes());
    Instruction {
        program_id: config::ADAPTER_PROGRAM_ID,
        accounts: vec![
            AccountMeta::new_readonly(user, true),
            AccountMeta::new(user_tlama, false),
            AccountMeta::new(config::TLAMA_VAULT, false),
            AccountMeta::new(config::WSOL_VAULT, false),
            AccountMeta::new(user_wsol, false),
            AccountMeta::new(config::TLAMA_MINT, false),
            AccountMeta::new_readonly(config::POOL_AUTHORITY, false),
            AccountMeta::new_readonly(
                get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
                false,
            ),
            AccountMeta::new_readonly(solana_program::sysvar::instructions::id(), false),
            AccountMeta::new_readonly(spl_token_2022::id(), false),
            AccountMeta::new_readonly(spl_token::id(), false),
            AccountMeta::new_readonly(hook_config::HOOK_PROGRAM_ID, false),
        ],
        data,
    }
}

async fn send(context: &mut solana_program_test::ProgramTestContext, ix: Instruction) {
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(ix)],
            Some(&context.payer.pubkey()),
            &[&context.payer],
            hash,
        ))
        .await
        .unwrap();
}

fn tlama_amount(account: &Account) -> u64 {
    StateWithExtensions::<TokenAccount>::unpack(&account.data)
        .unwrap()
        .base
        .amount
}

fn mint_supply(account: &Account) -> u64 {
    StateWithExtensions::<Mint>::unpack(&account.data)
        .unwrap()
        .base
        .supply
}

fn legacy_amount(account: &Account) -> u64 {
    spl_token::state::Account::unpack(&account.data)
        .unwrap()
        .amount
}

#[tokio::test]
async fn buy_invokes_real_token_cpis_and_hook() {
    let user = Keypair::new();
    let user_wsol = Keypair::new();
    let user_tlama = tlama_ata(&production_key(&user));
    let peer = Keypair::new();
    let peer_tlama = tlama_ata(&production_key(&peer));
    // Deliberately not an ATA: this is the fragmentation attempt.
    let fragmented_tlama = Keypair::new();
    let capped = Keypair::new();
    let capped_wsol = Keypair::new();
    let capped_tlama = tlama_ata(&production_key(&capped));
    let mint_authority = Keypair::new();
    let treasury = tlama_ata(&production_key(&mint_authority));
    let mut test = runtime_test();
    add_uninitialized_hook_mint(&mut test);
    add_uninitialized_hook_account(&mut test, config::TLAMA_VAULT);
    test.add_account(
        host_pubkey(&config::WSOL_VAULT),
        host_account(
            legacy_account_data(config::POOL_AUTHORITY, 100_000 * config::ONE_TLAMA),
            spl_token::id(),
        ),
    );
    test.add_account(
        user_wsol.pubkey(),
        host_account(
            legacy_account_data(production_key(&user), 1_000 * config::ONE_TLAMA),
            spl_token::id(),
        ),
    );
    add_uninitialized_hook_account(&mut test, user_tlama);
    add_uninitialized_hook_account(&mut test, peer_tlama);
    add_uninitialized_hook_account(&mut test, production_key(&fragmented_tlama));
    test.add_account(
        capped_wsol.pubkey(),
        host_account(
            legacy_account_data(production_key(&capped), 1_000 * config::ONE_TLAMA),
            spl_token::id(),
        ),
    );
    add_uninitialized_hook_account(&mut test, capped_tlama);
    add_uninitialized_hook_account(&mut test, treasury);
    let mut context = test.start_with_context().await;
    initialize_hook_fixture(
        &mut context,
        &mint_authority,
        &[
            (
                config::TLAMA_VAULT,
                config::POOL_AUTHORITY,
                8_000_000 * config::ONE_TLAMA,
            ),
            (user_tlama, production_key(&user), 0),
            (peer_tlama, production_key(&peer), 0),
            (production_key(&fragmented_tlama), production_key(&user), 0),
            (
                capped_tlama,
                production_key(&capped),
                config::MAX_WALLET - 1,
            ),
            (
                treasury,
                production_key(&mint_authority),
                config::MAX_SUPPLY - 8_000_000 * config::ONE_TLAMA - (config::MAX_WALLET - 1),
            ),
        ],
    )
    .await;
    let payer = production_key(&context.payer);
    send(
        &mut context,
        init_instruction(
            payer,
            get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
        ),
    )
    .await;
    let ix = pool_instruction(
        production_key(&user),
        production_key(&user_wsol),
        user_tlama,
        *b"TLAMABUY",
        100 * config::ONE_TLAMA,
        1,
    );
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(ix)],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash,
        ))
        .await
        .unwrap();
    let gross = tlama_pool_adapter::math::cp_out(
        100 * config::ONE_TLAMA,
        100_000 * config::ONE_TLAMA,
        8_000_000 * config::ONE_TLAMA,
        config::POOL_FEE_BPS,
    )
    .unwrap();
    let burn = tlama_pool_adapter::math::ceil_bps(gross, config::BUY_BURN_BPS).unwrap();
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&user_tlama))
                .await
                .unwrap()
                .unwrap()
        ),
        gross - burn
    );
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_VAULT))
                .await
                .unwrap()
                .unwrap()
        ),
        8_000_000 * config::ONE_TLAMA - gross
    );
    assert_eq!(
        legacy_amount(
            &context
                .banks_client
                .get_account(user_wsol.pubkey())
                .await
                .unwrap()
                .unwrap()
        ),
        900 * config::ONE_TLAMA
    );
    assert_eq!(
        mint_supply(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_MINT))
                .await
                .unwrap()
                .unwrap()
        ),
        config::MAX_SUPPLY - burn
    );

    // Direct peer transfers remain usable when both holders use their
    // canonical Token-2022 ATAs.
    let mut peer_transfer = spl_token_2022::instruction::transfer_checked(
        &spl_token_2022::id(),
        &user_tlama,
        &config::TLAMA_MINT,
        &peer_tlama,
        &production_key(&user),
        &[],
        1,
        config::DECIMALS,
    )
    .unwrap();
    peer_transfer.accounts.extend([
        AccountMeta::new_readonly(solana_program::sysvar::instructions::id(), false),
        AccountMeta::new_readonly(
            get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
            false,
        ),
        AccountMeta::new_readonly(hook_config::HOOK_PROGRAM_ID, false),
    ]);
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(peer_transfer)],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash,
        ))
        .await
        .unwrap();

    // A second Token-2022 account for the same owner cannot receive TLAMA,
    // preventing a holder from fragmenting balances around the wallet cap.
    let mut fragmented_transfer = spl_token_2022::instruction::transfer_checked(
        &spl_token_2022::id(),
        &user_tlama,
        &config::TLAMA_MINT,
        &production_key(&fragmented_tlama),
        &production_key(&user),
        &[],
        1,
        config::DECIMALS,
    )
    .unwrap();
    fragmented_transfer.accounts.extend([
        AccountMeta::new_readonly(solana_program::sysvar::instructions::id(), false),
        AccountMeta::new_readonly(
            get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
            false,
        ),
        AccountMeta::new_readonly(hook_config::HOOK_PROGRAM_ID, false),
    ]);
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    assert!(context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(fragmented_transfer)],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash,
        ))
        .await
        .is_err());
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(fragmented_tlama.pubkey())
                .await
                .unwrap()
                .unwrap()
        ),
        0
    );

    // The adapter independently rejects the same account before its WSOL CPI.
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    assert!(context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(pool_instruction(
                production_key(&user),
                production_key(&user_wsol),
                production_key(&fragmented_tlama),
                *b"TLAMABUY",
                config::ONE_TLAMA,
                0,
            ))],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash,
        ))
        .await
        .is_err());

    // A pool Buy which would exceed the cap fails before its first WSOL CPI.
    let before_wsol = legacy_amount(
        &context
            .banks_client
            .get_account(capped_wsol.pubkey())
            .await
            .unwrap()
            .unwrap(),
    );
    let before_vault = legacy_amount(
        &context
            .banks_client
            .get_account(host_pubkey(&config::WSOL_VAULT))
            .await
            .unwrap()
            .unwrap(),
    );
    let before_mint = mint_supply(
        &context
            .banks_client
            .get_account(host_pubkey(&config::TLAMA_MINT))
            .await
            .unwrap()
            .unwrap(),
    );
    let before_tlama_vault = tlama_amount(
        &context
            .banks_client
            .get_account(host_pubkey(&config::TLAMA_VAULT))
            .await
            .unwrap()
            .unwrap(),
    );
    let before_user = tlama_amount(
        &context
            .banks_client
            .get_account(host_pubkey(&capped_tlama))
            .await
            .unwrap()
            .unwrap(),
    );
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    assert!(context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(pool_instruction(
                production_key(&capped),
                production_key(&capped_wsol),
                capped_tlama,
                *b"TLAMABUY",
                config::ONE_TLAMA,
                0,
            ))],
            Some(&context.payer.pubkey()),
            &[&context.payer, &capped],
            hash
        ))
        .await
        .is_err());
    assert_eq!(
        legacy_amount(
            &context
                .banks_client
                .get_account(capped_wsol.pubkey())
                .await
                .unwrap()
                .unwrap()
        ),
        before_wsol
    );
    assert_eq!(
        legacy_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&config::WSOL_VAULT))
                .await
                .unwrap()
                .unwrap()
        ),
        before_vault
    );
    assert_eq!(
        mint_supply(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_MINT))
                .await
                .unwrap()
                .unwrap()
        ),
        before_mint
    );
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_VAULT))
                .await
                .unwrap()
                .unwrap()
        ),
        before_tlama_vault
    );
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&capped_tlama))
                .await
                .unwrap()
                .unwrap()
        ),
        before_user
    );

    // A direct Token-2022 transfer to the vault invokes the hook but lacks a
    // top-level pool instruction, so it rolls back.
    let before_user = tlama_amount(
        &context
            .banks_client
            .get_account(host_pubkey(&user_tlama))
            .await
            .unwrap()
            .unwrap(),
    );
    let before_vault = tlama_amount(
        &context
            .banks_client
            .get_account(host_pubkey(&config::TLAMA_VAULT))
            .await
            .unwrap()
            .unwrap(),
    );
    let mut direct = spl_token_2022::instruction::transfer_checked(
        &spl_token_2022::id(),
        &user_tlama,
        &config::TLAMA_MINT,
        &config::TLAMA_VAULT,
        &production_key(&user),
        &[],
        1,
        config::DECIMALS,
    )
    .unwrap();
    direct.accounts.push(AccountMeta::new_readonly(
        solana_program::sysvar::instructions::id(),
        false,
    ));
    direct.accounts.push(AccountMeta::new_readonly(
        get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
        false,
    ));
    direct.accounts.push(AccountMeta::new_readonly(
        hook_config::HOOK_PROGRAM_ID,
        false,
    ));
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    assert!(context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(direct)],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash
        ))
        .await
        .is_err());
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&user_tlama))
                .await
                .unwrap()
                .unwrap()
        ),
        before_user
    );
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_VAULT))
                .await
                .unwrap()
                .unwrap()
        ),
        before_vault
    );

    // Duplicating source and destination public keys is rejected by common()
    // before any CPI can mutate either account.
    let before = legacy_amount(
        &context
            .banks_client
            .get_account(user_wsol.pubkey())
            .await
            .unwrap()
            .unwrap(),
    );
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    assert!(context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(pool_instruction(
                production_key(&user),
                production_key(&user_wsol),
                production_key(&user_wsol),
                *b"TLAMABUY",
                config::ONE_TLAMA,
                0,
            ))],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash
        ))
        .await
        .is_err());
    assert_eq!(
        legacy_amount(
            &context
                .banks_client
                .get_account(user_wsol.pubkey())
                .await
                .unwrap()
                .unwrap()
        ),
        before
    );
}

#[tokio::test]
async fn sell_burns_then_transfers_net_and_pays_wsol() {
    let user = Keypair::new();
    let user_wsol = Keypair::new();
    let user_tlama = tlama_ata(&production_key(&user));
    let mint_authority = Keypair::new();
    let treasury = tlama_ata(&production_key(&mint_authority));
    let mut test = runtime_test();
    add_uninitialized_hook_mint(&mut test);
    add_uninitialized_hook_account(&mut test, config::TLAMA_VAULT);
    test.add_account(
        host_pubkey(&config::WSOL_VAULT),
        host_account(
            legacy_account_data(config::POOL_AUTHORITY, 100_000 * config::ONE_TLAMA),
            spl_token::id(),
        ),
    );
    add_uninitialized_hook_account(&mut test, user_tlama);
    add_uninitialized_hook_account(&mut test, treasury);
    test.add_account(
        user_wsol.pubkey(),
        host_account(
            legacy_account_data(production_key(&user), 0),
            spl_token::id(),
        ),
    );
    let mut context = test.start_with_context().await;
    initialize_hook_fixture(
        &mut context,
        &mint_authority,
        &[
            (
                config::TLAMA_VAULT,
                config::POOL_AUTHORITY,
                8_000_000 * config::ONE_TLAMA,
            ),
            (user_tlama, production_key(&user), 1_000 * config::ONE_TLAMA),
            (
                treasury,
                production_key(&mint_authority),
                config::MAX_SUPPLY - 8_000_000 * config::ONE_TLAMA - 1_000 * config::ONE_TLAMA,
            ),
        ],
    )
    .await;
    let payer = production_key(&context.payer);
    send(
        &mut context,
        init_instruction(
            payer,
            get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID),
        ),
    )
    .await;
    let gross = 100 * config::ONE_TLAMA;
    let hash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(sell_instruction(
                production_key(&user),
                user_tlama,
                production_key(&user_wsol),
                gross,
            ))],
            Some(&context.payer.pubkey()),
            &[&context.payer, &user],
            hash,
        ))
        .await
        .unwrap();
    let burn = tlama_pool_adapter::math::ceil_bps(gross, config::SELL_BURN_BPS).unwrap();
    let net = gross - burn;
    let out = tlama_pool_adapter::math::cp_out(
        net,
        8_000_000 * config::ONE_TLAMA,
        100_000 * config::ONE_TLAMA,
        config::POOL_FEE_BPS,
    )
    .unwrap();
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&user_tlama))
                .await
                .unwrap()
                .unwrap()
        ),
        1_000 * config::ONE_TLAMA - gross
    );
    assert_eq!(
        tlama_amount(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_VAULT))
                .await
                .unwrap()
                .unwrap()
        ),
        8_000_000 * config::ONE_TLAMA + net
    );
    assert_eq!(
        legacy_amount(
            &context
                .banks_client
                .get_account(user_wsol.pubkey())
                .await
                .unwrap()
                .unwrap()
        ),
        out
    );
    assert_eq!(
        mint_supply(
            &context
                .banks_client
                .get_account(host_pubkey(&config::TLAMA_MINT))
                .await
                .unwrap()
                .unwrap()
        ),
        config::MAX_SUPPLY - burn
    );
}

#[tokio::test]
async fn canonical_extra_meta_list_is_created_and_idempotent() {
    let mint_authority = Keypair::new();
    let mut test = runtime_test();
    add_uninitialized_hook_mint(&mut test);
    let mut context = test.start_with_context().await;
    initialize_hook_fixture(&mut context, &mint_authority, &[]).await;
    let payer = production_key(&context.payer);
    let validation =
        get_extra_account_metas_address(&config::TLAMA_MINT, &hook_config::HOOK_PROGRAM_ID);
    let ix = init_instruction(payer, validation);
    let blockhash = context.last_blockhash;
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(ix.clone())],
            Some(&context.payer.pubkey()),
            &[&context.payer],
            blockhash,
        ))
        .await
        .unwrap();
    let first = context
        .banks_client
        .get_account(host_pubkey(&validation))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(first.owner, host_pubkey(&hook_config::HOOK_PROGRAM_ID));
    assert!(!first.data.is_empty());
    let blockhash = context.banks_client.get_latest_blockhash().await.unwrap();
    context
        .banks_client
        .process_transaction(Transaction::new_signed_with_payer(
            &[host_instruction(ix)],
            Some(&context.payer.pubkey()),
            &[&context.payer],
            blockhash,
        ))
        .await
        .unwrap();
}
