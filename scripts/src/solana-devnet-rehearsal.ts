#!/usr/bin/env node
import {createHash} from "node:crypto";
import {readFile, rename, unlink, writeFile} from "node:fs/promises";
import {resolve} from "node:path";
import {pathToFileURL} from "node:url";
import {execFileSync} from "node:child_process";
import {
  BPF_LOADER_PROGRAM_ID,
  BpfLoader,
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
  TransactionInstruction,
} from "@solana/web3.js";
import {
  ACCOUNT_SIZE,
  ASSOCIATED_TOKEN_PROGRAM_ID,
  AuthorityType,
  ExtensionType,
  NATIVE_MINT,
  TOKEN_2022_PROGRAM_ID,
  TOKEN_PROGRAM_ID,
  createAssociatedTokenAccountInstruction,
  createInitializeAccount3Instruction,
  createInitializeMint2Instruction,
  createInitializeTransferHookInstruction,
  createMintToCheckedInstruction,
  createSetAuthorityInstruction,
  createSyncNativeInstruction,
  getAccountLen,
  getAssociatedTokenAddressSync,
  getAccount,
  getMint,
  getMintLen,
  getTransferHook,
} from "@solana/spl-token";

const RPC = "https://api.devnet.solana.com";
const ROOT = resolve(import.meta.dirname, "../..");
const DEFAULT_MANIFEST = resolve(ROOT, "audit/solana/releases/2026-09-07-production-candidate/manifest.json");
const MAX_SUPPLY = 1_000_000_000n * 1_000_000_000n;
const MAX_GROSS = 5_000_000n * 1_000_000_000n;
const DECIMALS = 9;
const scenarios = [
  "adapterDeployment", "hookDeployment", "initialize", "buy", "sell",
  "capRejection", "vaultHookRejection", "aliasedBuyRejection",
  "adapterFinalization", "hookFinalization",
] as const;
type Scenario = (typeof scenarios)[number];
type Step = Scenario | "userSetup";
const USER_WSOL_TARGET = 500_000_000n;

export function fundingDeficit(current: bigint, target = USER_WSOL_TARGET) {
  return current < target ? target - current : 0n;
}

function fail(message: string): never {
  throw new Error(`Devnet rehearsal refused: ${message}`);
}

function args(): Record<string, string> {
  const out: Record<string, string> = {};
  for (let i = 2; i < process.argv.length; i += 2) {
    const key = process.argv[i];
    const value = process.argv[i + 1];
    if (!key?.startsWith("--") || !value) fail("every option requires a file path or output path");
    out[key.slice(2)] = value;
  }
  const allowed = new Set(["payer-keypair", "adapter-program-keypair", "hook-program-keypair",
    "trader-keypair", "tlama-mint-keypair", "tlama-vault-keypair", "wsol-vault-keypair", "output"]);
  for (const key of Object.keys(out)) if (!allowed.has(key)) fail("unknown option");
  for (const key of ["payer-keypair", "trader-keypair", "adapter-program-keypair", "hook-program-keypair",
    "tlama-mint-keypair", "tlama-vault-keypair", "wsol-vault-keypair", "output"]) {
    if (!out[key]) fail(`missing --${key}`);
  }
  return out;
}

async function keypair(path: string): Promise<Keypair> {
  const parsed = JSON.parse(await readFile(path, "utf8"));
  if (!Array.isArray(parsed) || parsed.length !== 64 || parsed.some((v) => !Number.isInteger(v) || v < 0 || v > 255)) {
    fail("a keypair file is malformed");
  }
  return Keypair.fromSecretKey(Uint8Array.from(parsed));
}

function rejectSecretEnvironment() {
  for (const name of Object.keys(process.env)) {
    if (/(?:_KEYPAIR|_PRIVATE_KEY|_SECRET|_MNEMONIC|_SEED|SOLANA_KEYPAIR)$/i.test(name)) {
      fail("possible secret-bearing environment variable is present");
    }
  }
}

async function finalizedEntry(connection: Connection, signature: string, rejected = false) {
  for (;;) {
    const status = (await connection.getSignatureStatuses([signature], {searchTransactionHistory: true})).value[0];
    if (status?.confirmationStatus === "finalized") {
      if ((status.err !== null) !== rejected) fail("finalized transaction outcome did not match the scenario");
      return {signature, slot: status.slot, confirmationStatus: "finalized", outcome: rejected ? "rejected" : "successful"};
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
}

async function deploy(connection: Connection, payer: Keypair, program: Keypair, bytes: Buffer) {
  if (!await BpfLoader.load(connection, payer, program, bytes, BPF_LOADER_PROGRAM_ID)) {
    fail("program deployment failed");
  }
}

function base58Encode(data: Uint8Array) {
  let number = BigInt(`0x${Buffer.from(data).toString("hex")}`);
  let output = "";
  const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  while (number > 0n) {
    output = alphabet[Number(number % 58n)] + output;
    number /= 58n;
  }
  for (const byte of data) {
    if (byte !== 0) break;
    output = `1${output}`;
  }
  return output;
}

export type PendingTransaction = {
  signature: string;
  lastValidBlockHeight: number;
  wireBase64: string;
  rejected: boolean;
};

export async function reconcilePending(connection: Pick<Connection,
  "getBlockHeight" | "getSignatureStatuses" | "sendRawTransaction">, pending: PendingTransaction,
  rebroadcast = false): Promise<Awaited<ReturnType<typeof finalizedEntry>> | null> {
  for (;;) {
    // Read finalized height first. A subsequent historical status read cannot
    // miss a transaction that lands later with this already-expired blockhash.
    const height = await connection.getBlockHeight("finalized");
    const status = (await connection.getSignatureStatuses([pending.signature],
      {searchTransactionHistory: true})).value[0];
    if (status?.confirmationStatus === "finalized") {
      if ((status.err !== null) !== pending.rejected) fail("finalized transaction outcome mismatch");
      return {signature: pending.signature, slot: status.slot, confirmationStatus: "finalized",
        outcome: pending.rejected ? "rejected" : "successful"};
    }
    if (height > pending.lastValidBlockHeight && status === null) return null;
    if (rebroadcast) {
      const sent = await connection.sendRawTransaction(Buffer.from(pending.wireBase64, "base64"),
        {skipPreflight: pending.rejected, maxRetries: 5});
      if (sent !== pending.signature) fail("reconciled transaction signature mismatch");
      rebroadcast = false;
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

async function submit(connection: Connection, tx: Transaction, signers: Keypair[], rejected = false,
  onPrepared?: (pending: PendingTransaction) => Promise<void>) {
  const latest = await connection.getLatestBlockhash("finalized");
  tx.feePayer = signers[0].publicKey;
  tx.recentBlockhash = latest.blockhash;
  const simulated = await connection.simulateTransaction(tx);
  if (!rejected && simulated.value.err) fail(`simulation failed: ${JSON.stringify(simulated.value.err)}`);
  if (rejected && !simulated.value.err) fail("a required rejection simulated successfully");
  tx.sign(...signers);
  const wire = tx.serialize();
  const signature = base58Encode(tx.signature!);
  await onPrepared?.({signature, lastValidBlockHeight: latest.lastValidBlockHeight,
    wireBase64: wire.toString("base64"), rejected});
  const sent = await connection.sendRawTransaction(wire, {skipPreflight: rejected, maxRetries: 5});
  if (sent !== signature) fail("RPC returned a different transaction signature");
  return reconcilePending(connection, {signature, lastValidBlockHeight: latest.lastValidBlockHeight,
    wireBase64: wire.toString("base64"), rejected});
}

async function loaderProof(connection: Connection, target: PublicKey) {
  for (;;) {
    let before: string | undefined;
    let creation: {signature: string; slot: number} | undefined;
    let finalization: {signature: string; slot: number} | undefined;
    for (;;) {
      const page = await connection.getSignaturesForAddress(target, {before, limit: 1000}, "finalized");
      for (const item of page) {
        if (item.err !== null) continue;
        const tx = await connection.getTransaction(item.signature,
          {commitment: "finalized", maxSupportedTransactionVersion: 0});
        if (!tx || tx.meta?.err) continue;
        const keys = tx.transaction.message.getAccountKeys().staticAccountKeys;
        for (const ix of tx.transaction.message.compiledInstructions) {
          const program = keys[ix.programIdIndex];
          const accounts = ix.accountKeyIndexes.map((index) => keys[index]);
          const data = Buffer.from(ix.data);
          if (program.equals(SystemProgram.programId) && accounts.some((key) => key.equals(target))
            && data.length >= 4 && data.readUInt32LE(0) === 0) creation = item;
          if (program.equals(BPF_LOADER_PROGRAM_ID) && accounts[0]?.equals(target)
            && data.equals(Buffer.from([1, 0, 0, 0]))) finalization ??= item;
        }
      }
      if (page.length < 1000) break;
      before = page.at(-1)!.signature;
    }
    if (creation && finalization && creation.slot <= finalization.slot) {
      return {deployment: creation.signature, finalization: finalization.signature};
    }
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

function poolIx(ids: Record<string, PublicKey>, user: PublicKey, userWsol: PublicKey,
  userTlama: PublicKey, tag: string, a: bigint, b: bigint, alias = false) {
  const data = Buffer.alloc(24);
  data.write(tag, 0, "ascii");
  data.writeBigUInt64LE(a, 8);
  data.writeBigUInt64LE(b, 16);
  const validation = PublicKey.findProgramAddressSync(
    [Buffer.from("extra-account-metas"), ids.tlamaMint.toBuffer()],
    ids.hook,
  )[0];
  const first = tag === "TLAMABUY";
  const roles = first
    ? [userWsol, ids.wsolVault, ids.tlamaVault, userTlama]
    : [userTlama, ids.tlamaVault, ids.wsolVault, userWsol];
  if (alias) roles[1] = roles[0];
  return new TransactionInstruction({
    programId: ids.adapter,
    keys: [
      {pubkey: user, isSigner: true, isWritable: false},
      ...roles.map((pubkey) => ({pubkey, isSigner: false, isWritable: true})),
      {pubkey: ids.tlamaMint, isSigner: false, isWritable: true},
      {pubkey: ids.poolAuthority, isSigner: false, isWritable: false},
      {pubkey: validation, isSigner: false, isWritable: false},
      {pubkey: new PublicKey("Sysvar1nstructions1111111111111111111111111"), isSigner: false, isWritable: false},
      {pubkey: TOKEN_2022_PROGRAM_ID, isSigner: false, isWritable: false},
      {pubkey: TOKEN_PROGRAM_ID, isSigner: false, isWritable: false},
      {pubkey: ids.hook, isSigner: false, isWritable: false},
    ],
    data,
  });
}

async function main() {
  rejectSecretEnvironment();
  if (!process.stdin.isTTY) fail("stdin is not accepted");
  const opt = args();
  const output = resolve(opt.output);
  const progressPath = `${output}.progress`;
  const manifestPath = DEFAULT_MANIFEST;
  const artifactDir = resolve(manifestPath, "..", "artifacts");
  execFileSync("python3", [
    resolve(ROOT, "audit/solana/verify_artifact_manifest.py"), manifestPath, artifactDir,
  ], {stdio: ["ignore", "ignore", "inherit"], env: {}});
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  if (manifest.schemaVersion !== 3 || manifest.buildKind !== "production") fail("the frozen production manifest is required");
  const id = Object.fromEntries(Object.entries(manifest.ids).map(([k, v]) => [k, new PublicKey(v as string)]));
  const [payer, trader, adapter, hook, mint, tlamaVault, wsolVault] = await Promise.all([
    keypair(opt["payer-keypair"]), keypair(opt["trader-keypair"]), keypair(opt["adapter-program-keypair"]), keypair(opt["hook-program-keypair"]),
    keypair(opt["tlama-mint-keypair"]), keypair(opt["tlama-vault-keypair"]), keypair(opt["wsol-vault-keypair"]),
  ]);
  for (const [name, kp] of [["adapter", adapter], ["hook", hook], ["tlamaMint", mint],
    ["tlamaVault", tlamaVault], ["wsolVault", wsolVault]] as const) {
    if (!kp.publicKey.equals(id[name])) fail(`${name} custody file does not match the frozen ID`);
  }
  const expectedAuthority = PublicKey.findProgramAddressSync([Buffer.from("pool-authority")], id.adapter)[0];
  if (!expectedAuthority.equals(id.poolAuthority)) fail("the frozen pool authority is not the adapter PDA");
  if (trader.publicKey.equals(payer.publicKey)) fail("trader must be separate from the writable fee payer");
  const elf = {
    adapter: Buffer.from(await readFile(resolve(artifactDir, "tlama_pool_adapter.so"))),
    hook: Buffer.from(await readFile(resolve(artifactDir, "tlama_transfer_hook.so"))),
  };
  for (const [name, file] of [["adapter", "tlama_pool_adapter.so"], ["hook", "tlama_transfer_hook.so"]] as const) {
    if (createHash("sha256").update(elf[name]).digest("hex") !== manifest.artifacts[file]) fail("frozen ELF hash mismatch");
  }

  const connection = new Connection(RPC, "finalized");
  if ((await connection.getGenesisHash()) !== "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG") fail("RPC is not canonical Solana Devnet");
  const rent = await connection.getMinimumBalanceForRentExemption(elf.adapter.length)
    + await connection.getMinimumBalanceForRentExemption(elf.hook.length)
    + await connection.getMinimumBalanceForRentExemption(getMintLen([ExtensionType.TransferHook]))
    + await connection.getMinimumBalanceForRentExemption(getAccountLen([ExtensionType.TransferHookAccount]))
    + await connection.getMinimumBalanceForRentExemption(ACCOUNT_SIZE) + 2_000_000_000;
  if (await connection.getBalance(payer.publicKey, "finalized") < rent) fail("payer is not funded for rent, liquidity, and fees");

  type Entry = Awaited<ReturnType<typeof finalizedEntry>>;
  type Progress = {sourceCommit: string; artifactHashes: Record<string, string>;
    programIds: Record<string, string>; signatures: Partial<Record<Scenario, Entry>>;
    userSetup?: Entry;
    pending?: Partial<Record<Step, PendingTransaction>>};
  let progress: Progress;
  try {
    progress = JSON.parse(await readFile(progressPath, "utf8"));
    if (progress.sourceCommit !== manifest.sourceCommit
      || JSON.stringify(progress.artifactHashes) !== JSON.stringify(manifest.artifacts)
      || JSON.stringify(progress.programIds) !== JSON.stringify({adapter: manifest.ids.adapter, hook: manifest.ids.hook})) {
      fail("existing public progress is not bound to the frozen manifest");
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    progress = {sourceCommit: manifest.sourceCommit, artifactHashes: manifest.artifacts,
      programIds: {adapter: manifest.ids.adapter, hook: manifest.ids.hook}, signatures: {}};
  }
  const saveProgress = async () => {
    const pending = `${progressPath}.pending`;
    await writeFile(pending, `${JSON.stringify(progress, null, 2)}\n`, {encoding: "utf8", mode: 0o600, flag: "w"});
    await rename(pending, progressPath);
  };
  const signatures = progress.signatures as Record<Scenario, Entry>;
  const run = async (name: Step, operation: (onPrepared: (pending: PendingTransaction) => Promise<void>) => Promise<Entry | null>) => {
    const existing = name === "userSetup" ? progress.userSetup : signatures[name];
    if (existing) {
      const checked = await finalizedEntry(connection, existing.signature, name.endsWith("Rejection"));
      if (checked.slot !== existing.slot) fail(`checkpoint slot mismatch: ${name}`);
      return existing;
    }
    progress.pending ??= {};
    const execute = async () => {
      for (;;) {
        const result = await operation(async (prepared) => {
          progress.pending![name] = prepared;
          await saveProgress();
        });
        if (result) return result;
        delete progress.pending![name];
        await saveProgress();
      }
    };
    if (progress.pending[name]) {
      const pending = progress.pending[name];
      const result = await reconcilePending(connection, pending, true) ?? await execute();
      if (name === "userSetup") progress.userSetup = result;
      else signatures[name] = result;
    } else {
      const result = await execute();
      if (name === "userSetup") progress.userSetup = result;
      else signatures[name] = result;
    }
    delete progress.pending[name];
    await saveProgress();
    return name === "userSetup" ? progress.userSetup! : signatures[name];
  };
  // BpfLoader performs RPC preflight simulation for every write and permanently
  // finalizes legacy-loader programs. Exact create/finalize proofs are recovered
  // from finalized ledger instructions, including after an interrupted process.
  let deployedAdapter = await connection.getAccountInfo(id.adapter, "finalized");
  if (deployedAdapter === null || !deployedAdapter.executable) {
    await deploy(connection, payer, adapter, elf.adapter);
  }
  const adapterProof = await loaderProof(connection, id.adapter);
  deployedAdapter = await connection.getAccountInfo(id.adapter, "finalized");
  if (!deployedAdapter?.executable || !deployedAdapter.owner.equals(BPF_LOADER_PROGRAM_ID)
    || !deployedAdapter.data.equals(elf.adapter)) fail("finalized adapter bytes do not match the frozen ELF");
  await run("adapterDeployment", () => finalizedEntry(connection, adapterProof.deployment));
  await run("adapterFinalization", () => finalizedEntry(connection, adapterProof.finalization));
  let deployedHook = await connection.getAccountInfo(id.hook, "finalized");
  if (deployedHook === null || !deployedHook.executable) {
    await deploy(connection, payer, hook, elf.hook);
  }
  const hookProof = await loaderProof(connection, id.hook);
  deployedHook = await connection.getAccountInfo(id.hook, "finalized");
  if (!deployedHook?.executable || !deployedHook.owner.equals(BPF_LOADER_PROGRAM_ID)
    || !deployedHook.data.equals(elf.hook)) fail("finalized hook bytes do not match the frozen ELF");
  await run("hookDeployment", () => finalizedEntry(connection, hookProof.deployment));
  await run("hookFinalization", () => finalizedEntry(connection, hookProof.finalization));

  const mintLen = getMintLen([ExtensionType.TransferHook]);
  const tlamaLen = getAccountLen([ExtensionType.TransferHookAccount]);
  const userWsol = getAssociatedTokenAddressSync(NATIVE_MINT, trader.publicKey, false,
    TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID);
  const userTlama = getAssociatedTokenAddressSync(id.tlamaMint, trader.publicKey, false, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID);
  const existingMint = await connection.getAccountInfo(mint.publicKey, "finalized");
  const existingTlamaVault = await connection.getAccountInfo(tlamaVault.publicKey, "finalized");
  const existingWsolVault = await connection.getAccountInfo(wsolVault.publicKey, "finalized");
  if ((existingMint === null) !== (existingTlamaVault === null)) fail("mint and TLAMA vault are in an unsafe partial state");
  if (existingMint === null && existingWsolVault !== null) fail("WSOL vault exists before the frozen mint state");
  if (existingMint === null) {
    const mintSetup = new Transaction().add(
    SystemProgram.createAccount({fromPubkey: payer.publicKey, newAccountPubkey: mint.publicKey,
      lamports: await connection.getMinimumBalanceForRentExemption(mintLen), space: mintLen, programId: TOKEN_2022_PROGRAM_ID}),
    createInitializeTransferHookInstruction(mint.publicKey, payer.publicKey, hook.publicKey, TOKEN_2022_PROGRAM_ID),
    createInitializeMint2Instruction(mint.publicKey, DECIMALS, payer.publicKey, null, TOKEN_2022_PROGRAM_ID),
    SystemProgram.createAccount({fromPubkey: payer.publicKey, newAccountPubkey: tlamaVault.publicKey,
      lamports: await connection.getMinimumBalanceForRentExemption(tlamaLen), space: tlamaLen, programId: TOKEN_2022_PROGRAM_ID}),
    createInitializeAccount3Instruction(tlamaVault.publicKey, mint.publicKey, id.poolAuthority, TOKEN_2022_PROGRAM_ID),
    createMintToCheckedInstruction(mint.publicKey, tlamaVault.publicKey, payer.publicKey, MAX_SUPPLY, DECIMALS, [], TOKEN_2022_PROGRAM_ID),
    );
    await submit(connection, mintSetup, [payer, mint, tlamaVault]);
  } else {
    const mintState = await getMint(connection, mint.publicKey, "finalized", TOKEN_2022_PROGRAM_ID);
    const vaultState = await getAccount(connection, tlamaVault.publicKey, "finalized", TOKEN_2022_PROGRAM_ID);
    const transferHook = getTransferHook(mintState);
    if (mintState.decimals !== DECIMALS || !transferHook?.programId.equals(hook.publicKey)
      || !vaultState.mint.equals(mint.publicKey) || !vaultState.owner.equals(id.poolAuthority)) {
      fail("existing mint or TLAMA vault does not match the frozen campaign");
    }
  }
  if (existingWsolVault === null) {
    const currentMint = await getMint(connection, mint.publicKey, "finalized", TOKEN_2022_PROGRAM_ID);
    if (!currentMint.mintAuthority?.equals(payer.publicKey)) fail("mint authority cannot finalize the interrupted setup");
    const authorityRent = await connection.getMinimumBalanceForRentExemption(0);
    const vaultSetup = new Transaction().add(
      createSetAuthorityInstruction(mint.publicKey, payer.publicKey, AuthorityType.MintTokens, null, [], TOKEN_2022_PROGRAM_ID),
      SystemProgram.transfer({fromPubkey: payer.publicKey, toPubkey: id.poolAuthority, lamports: authorityRent}),
    SystemProgram.createAccount({fromPubkey: payer.publicKey, newAccountPubkey: wsolVault.publicKey,
      lamports: await connection.getMinimumBalanceForRentExemption(ACCOUNT_SIZE) + 1_000_000_000, space: ACCOUNT_SIZE, programId: TOKEN_PROGRAM_ID}),
    createInitializeAccount3Instruction(wsolVault.publicKey, NATIVE_MINT, id.poolAuthority, TOKEN_PROGRAM_ID),
    );
    await submit(connection, vaultSetup, [payer, wsolVault]);
  } else {
    const currentMint = await getMint(connection, mint.publicKey, "finalized", TOKEN_2022_PROGRAM_ID);
    const vaultState = await getAccount(connection, wsolVault.publicKey, "finalized", TOKEN_PROGRAM_ID);
    if (currentMint.mintAuthority !== null || !vaultState.mint.equals(NATIVE_MINT)
      || !vaultState.owner.equals(id.poolAuthority)) fail("existing finalized vault state is incompatible");
  }
  const userSetup = new Transaction();
  const existingUserWsol = await connection.getAccountInfo(userWsol, "finalized");
  if (!existingUserWsol) {
    userSetup.add(createAssociatedTokenAccountInstruction(payer.publicKey, userWsol, trader.publicKey,
      NATIVE_MINT, TOKEN_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID));
  }
  if (!await connection.getAccountInfo(userTlama, "finalized")) {
    userSetup.add(createAssociatedTokenAccountInstruction(payer.publicKey, userTlama, trader.publicKey,
      mint.publicKey, TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID));
  }
  const currentWsol = existingUserWsol
    ? (await getAccount(connection, userWsol, "finalized", TOKEN_PROGRAM_ID)).amount
    : 0n;
  const deficit = fundingDeficit(currentWsol);
  if (deficit > 0n) {
    userSetup.add(
      SystemProgram.transfer({fromPubkey: payer.publicKey, toPubkey: userWsol, lamports: deficit}),
      createSyncNativeInstruction(userWsol, TOKEN_PROGRAM_ID),
    );
  }
  if (userSetup.instructions.length || progress.pending?.userSetup) {
    await run("userSetup", (prepared) => {
      if (!userSetup.instructions.length) {
        fail("user setup checkpoint expired without finalized ledger evidence");
      }
      return submit(connection, userSetup, [payer], false, prepared);
    });
  } else if (!progress.userSetup) {
    fail("user setup is complete on chain but lacks a public transaction checkpoint");
  }
  const validation = PublicKey.findProgramAddressSync([Buffer.from("extra-account-metas"), mint.publicKey.toBuffer()], hook.publicKey)[0];
  await run("initialize", (broadcast) => submit(connection, new Transaction().add(new TransactionInstruction({
    programId: hook.publicKey,
    keys: [{pubkey: payer.publicKey, isSigner: true, isWritable: true}, {pubkey: validation, isSigner: false, isWritable: true},
      {pubkey: mint.publicKey, isSigner: false, isWritable: true}, {pubkey: SystemProgram.programId, isSigner: false, isWritable: false}],
    data: Buffer.from("TLAMINIT"),
  })), [payer], false, broadcast));
  await run("buy", (broadcast) => submit(connection, new Transaction().add(poolIx(id, trader.publicKey, userWsol, userTlama, "TLAMABUY", 1_000_000n, 1n)), [payer, trader], false, broadcast));
  await run("sell", (broadcast) => submit(connection, new Transaction().add(poolIx(id, trader.publicKey, userWsol, userTlama, "TLAMASEL", 2_000_000_000n, 1n)), [payer, trader], false, broadcast));
  await run("capRejection", (broadcast) => submit(connection, new Transaction().add(poolIx(id, trader.publicKey, userWsol, userTlama, "TLAMABUY", MAX_GROSS, 1n)), [payer, trader], true, broadcast));
  // A direct hook invocation cannot possess Token-2022's transferring flags and must fail closed.
  await run("vaultHookRejection", (broadcast) => submit(connection, new Transaction().add(new TransactionInstruction({
    programId: hook.publicKey,
    keys: [{pubkey: tlamaVault.publicKey, isSigner: false, isWritable: true}, {pubkey: mint.publicKey, isSigner: false, isWritable: false},
      {pubkey: userTlama, isSigner: false, isWritable: true}, {pubkey: id.poolAuthority, isSigner: false, isWritable: false},
      {pubkey: validation, isSigner: false, isWritable: false}, {pubkey: new PublicKey("Sysvar1nstructions1111111111111111111111111"), isSigner: false, isWritable: false}],
    data: Buffer.from([105, 37, 101, 197, 75, 251, 102, 26, 1, 0, 0, 0, 0, 0, 0, 0]),
  })), [payer], true, broadcast));
  await run("aliasedBuyRejection", (broadcast) => submit(connection, new Transaction().add(poolIx(id, trader.publicKey, userWsol, userTlama, "TLAMABUY", 1n, 1n, true)), [payer, trader], true, broadcast));

  const accountSnapshots: Record<string, object> = {};
  for (const name of ["adapter", "hook", "tlamaMint", "tlamaVault", "wsolVault", "poolAuthority"]) {
    const response = await connection.getAccountInfoAndContext(id[name], "finalized");
    if (!response.value) fail(`finalized snapshot is absent: ${name}`);
    accountSnapshots[name] = {address: id[name].toBase58(), owner: response.value.owner.toBase58(),
      lamports: response.value.lamports, dataSha256: createHash("sha256").update(response.value.data).digest("hex"),
      slot: response.context.slot, confirmationStatus: "finalized"};
  }
  const evidence = {schemaVersion: 1, cluster: "devnet",
    manifest: {sourceCommit: manifest.sourceCommit, artifactHashes: manifest.artifacts,
      programIds: {adapter: manifest.ids.adapter, hook: manifest.ids.hook}},
    signatures, accountSnapshots, scenarios: Object.fromEntries(scenarios.map((name) => [name, true]))};
  const pending = `${output}.pending`;
  await writeFile(pending, `${JSON.stringify(evidence, null, 2)}\n`, {encoding: "utf8", mode: 0o600, flag: "wx"});
  try {
    execFileSync("python3", [
      resolve(ROOT, "audit/solana/verify_devnet_evidence.py"),
      pending, manifestPath, artifactDir, "--rpc-url", RPC,
    ], {stdio: ["ignore", "inherit", "inherit"], env: {}});
    await rename(pending, output);
  } catch (error) {
    await unlink(pending).catch(() => undefined);
    throw error;
  }
  console.log("Devnet rehearsal finalized; sanitized public evidence written.");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error instanceof Error ? error.message : "Devnet rehearsal failed");
    process.exitCode = 1;
  });
}