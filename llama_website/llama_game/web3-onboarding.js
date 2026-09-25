import {
  connectionFailureStage,
  dispatchConnectionFailure,
} from "./wallet-failure-stages.js";

const SOL_MINT = "So11111111111111111111111111111111111111112";
const state = {
  config: window.TLAMA_PAYMENT_CONFIG || {},
  adapter: null,
  connection: null,
  quote: null,
  gameSettlementHook: null,
  balances: { available: false, sol: null, tlama: null },
};

const elements = {
  address: document.getElementById("web3Address"),
  solBalance: document.getElementById("web3SolBalance"),
  tlamaBalance: document.getElementById("web3TlamaBalance"),
  amount: document.getElementById("web3SolAmount"),
  output: document.getElementById("web3SwapOutput"),
  quoteButton: document.getElementById("web3QuoteButton"),
  swapButton: document.getElementById("web3SwapButton"),
  status: document.getElementById("web3Status"),
};

const loadModule = (url) => import(url);
const reportConnectionFailure = (kind, stage) => {
  dispatchConnectionFailure(window, kind, stage);
};
const walletConfigured = (config = state.config) =>
  Boolean(
    config.web3_enabled &&
      config.mint_address &&
      config.rpc_url
  );
const configured = () => Boolean(walletConfigured() && state.config.raydium_api);

const setStatus = (message) => {
  elements.status.textContent = message;
};

const decimalToAtomic = (value, decimals) => {
  if (!/^\d+(\.\d+)?$/.test(value.trim())) throw new Error("Enter a valid SOL amount.");
  const [whole, fraction = ""] = value.trim().split(".");
  const padded = `${fraction}${"0".repeat(decimals)}`.slice(0, decimals);
  return BigInt(whole) * 10n ** BigInt(decimals) + BigInt(padded || "0");
};

const createAdapter = async (kind) => {
  if (kind === "phantom") {
    const module = await loadModule("https://esm.sh/@solana/wallet-adapter-phantom@0.9.28");
    return new module.PhantomWalletAdapter();
  }
  if (kind === "solflare") {
    const [module, base] = await Promise.all([
      loadModule("https://esm.sh/@solana/wallet-adapter-solflare@0.6.32"),
      loadModule("https://esm.sh/@solana/wallet-adapter-base@0.9.27"),
    ]);
    return new module.SolflareWalletAdapter({ network: base.WalletAdapterNetwork.Mainnet });
  }
  const module = await loadModule("https://esm.sh/@solana/wallet-adapter-coinbase@0.1.22");
  return new module.CoinbaseWalletAdapter();
};

const refreshBalances = async () => {
  if (!state.adapter?.publicKey || !configured()) {
    state.balances = { available: false, sol: null, tlama: null };
    return { ...state.balances };
  }
  const [web3, splToken] = await Promise.all([
    loadModule("https://esm.sh/@solana/web3.js@1.98.4"),
    loadModule("https://esm.sh/@solana/spl-token@0.4.14"),
  ]);
  state.connection ||= new web3.Connection(state.config.rpc_url, "confirmed");
  const owner = new web3.PublicKey(state.adapter.publicKey.toBase58());
  const lamports = await state.connection.getBalance(owner, "confirmed");
  const sol = lamports / web3.LAMPORTS_PER_SOL;
  elements.solBalance.textContent = sol.toLocaleString(
    undefined,
    { maximumFractionDigits: 5 }
  );
  try {
    const mint = new web3.PublicKey(state.config.mint_address);
    const account = await splToken.getAssociatedTokenAddress(mint, owner);
    const balance = await state.connection.getTokenAccountBalance(account, "confirmed");
    const tlama = Number(balance.value.uiAmountString || "0");
    elements.tlamaBalance.textContent = balance.value.uiAmountString || "0";
    state.balances = {
      available: Number.isFinite(tlama),
      sol,
      tlama: Number.isFinite(tlama) ? tlama : 0,
    };
  } catch {
    elements.tlamaBalance.textContent = "0";
    state.balances = { available: true, sol, tlama: 0 };
  }
  return { ...state.balances };
};

const checkTokenAffordability = async (amountTlama) => {
  const price = Number(amountTlama);
  if (!Number.isFinite(price) || price < 0) {
    throw new Error("The cosmetic price is invalid.");
  }
  const balances = await refreshBalances();
  return {
    ...balances,
    priceTlama: price,
    affordable: balances.available && Number(balances.tlama) >= price,
  };
};

const connect = async (kind = "phantom") => {
  if (!walletConfigured()) {
    reportConnectionFailure(kind, "unavailable_setup");
    throw new Error("Web3 is disabled until public settings are verified.");
  }

  let adapter;
  try {
    if (state.adapter) await state.adapter.disconnect();
    setStatus(`Opening ${kind}…`);
    adapter = await createAdapter(kind);
  } catch (error) {
    reportConnectionFailure(kind, "unavailable_setup");
    throw error;
  }

  try {
    await adapter.connect();
  } catch (error) {
    reportConnectionFailure(
      kind,
      connectionFailureStage(error)
    );
    throw error;
  }

  try {
    if (!adapter.publicKey) throw new Error("The wallet returned no public address.");
    state.adapter = adapter;
    const address = adapter.publicKey.toBase58();
    elements.address.textContent = `${address.slice(0, 5)}…${address.slice(-5)}`;
    elements.quoteButton.disabled = false;
    await refreshBalances();
    setStatus("Wallet connected. Balances were read from the configured public RPC.");
    window.dispatchEvent(new CustomEvent("tlama-wallet-connected", { detail: { address } }));
    return { provider: adapter, publicKey: adapter.publicKey };
  } catch (error) {
    reportConnectionFailure(kind, "connection_failed");
    throw error;
  }
};

const requestQuote = async () => {
  if (!state.adapter?.publicKey) throw new Error("Connect a wallet first.");
  const amount = decimalToAtomic(elements.amount.value, 9);
  if (amount <= 0n) throw new Error("SOL amount must be greater than zero.");
  const endpoint = new URL("/compute/swap-base-in", state.config.raydium_api);
  endpoint.searchParams.set("inputMint", SOL_MINT);
  endpoint.searchParams.set("outputMint", state.config.mint_address);
  endpoint.searchParams.set("amount", amount.toString());
  endpoint.searchParams.set(
    "slippageBps",
    String(state.config.swap_slippage_bps || 100)
  );
  endpoint.searchParams.set("txVersion", "V0");
  setStatus("Requesting a Raydium quote…");
  const response = await fetch(endpoint, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Raydium quote failed (${response.status}).`);
  const quote = await response.json();
  if (!quote?.data?.outputAmount) throw new Error("Raydium returned no swap output.");
  state.quote = quote;
  const decimals = Number(state.config.decimals || 9);
  elements.output.textContent = `${(
    Number(quote.data.outputAmount) /
    10 ** decimals
  ).toLocaleString(undefined, { maximumFractionDigits: 6 })} TLAMA`;
  elements.swapButton.disabled = false;
  setStatus("Quote ready. Review the estimate before approving the wallet transaction.");
};

const executeSwap = async () => {
  if (!state.adapter?.publicKey || !state.quote) throw new Error("Request a quote first.");
  const slippage = Number(state.config.swap_slippage_bps || 100) / 100;
  const approved = window.confirm(
    `Swap ${elements.amount.value} SOL for ${elements.output.textContent}?\n\n` +
      `Slippage limit: ${slippage.toFixed(2)}%\n` +
      "Raydium will build the transaction. Your wallet will show the final details before signing."
  );
  if (!approved) return;
  const web3 = await loadModule("https://esm.sh/@solana/web3.js@1.98.4");
  state.connection ||= new web3.Connection(state.config.rpc_url, "confirmed");
  const endpoint = new URL("/transaction/swap-base-in", state.config.raydium_api);
  const response = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      computeUnitPriceMicroLamports: "600000",
      swapResponse: state.quote,
      txVersion: "V0",
      wallet: state.adapter.publicKey.toBase58(),
      wrapSol: true,
      unwrapSol: false,
    }),
  });
  if (!response.ok) throw new Error(`Raydium transaction build failed (${response.status}).`);
  const payload = await response.json();
  if (!Array.isArray(payload?.data) || !payload.data.length) {
    throw new Error("Raydium returned no transaction to review.");
  }
  const transactions = payload.data.map(({ transaction }) =>
    web3.VersionedTransaction.deserialize(
      Uint8Array.from(atob(transaction), (character) => character.charCodeAt(0))
    )
  );
  const signed = state.adapter.signAllTransactions
    ? await state.adapter.signAllTransactions(transactions)
    : await Promise.all(transactions.map((transaction) => state.adapter.signTransaction(transaction)));
  const signatures = [];
  for (const transaction of signed) {
    const signature = await state.connection.sendRawTransaction(transaction.serialize(), {
      skipPreflight: false,
      preflightCommitment: "confirmed",
    });
    await state.connection.confirmTransaction(signature, "confirmed");
    signatures.push(signature);
  }
  setStatus(`Swap confirmed. Signature ${signatures.at(-1).slice(0, 10)}…`);
  state.quote = null;
  elements.swapButton.disabled = true;
  await refreshBalances();
};

const registerGameSettlementHook = (hook) => {
  if (!hook || typeof hook.purchase !== "function") {
    throw new Error("A settlement hook must expose a purchase function.");
  }
  state.gameSettlementHook = hook;
};

const purchaseGameItem = async (item, config = state.config) => {
  if (!state.adapter?.publicKey) throw new Error("Connect a wallet first.");
  if (!walletConfigured(config) || !config.enabled) {
    throw new Error("Live game purchases are disabled.");
  }
  const economy = config.game_economy || {};
  if (
    economy.settlement_mode !== "audited-transfer-hook-required" ||
    economy.reward_vault_percent !== 100 ||
    !economy.reward_vault_address ||
    !economy.hook_program_address
  ) {
    throw new Error("The immutable 100% Game Rewards Vault route is not configured.");
  }
  if (!state.gameSettlementHook) {
    throw new Error("No independently audited game settlement hook is installed.");
  }
  const decimals = Number(config.decimals);
  const isHeartMatrix = item.id === "heart-matrix-upgrade";
  const quoteExpiresAt = Number(item.quote_expires_at);
  if (
    isHeartMatrix &&
    (
      !item.oracle_quote ||
      !Number.isSafeInteger(quoteExpiresAt) ||
      Math.floor(Date.now() / 1000) + 30 >= quoteExpiresAt
    )
  ) {
    throw new Error("The Heart Matrix price quote is too close to expiry.");
  }
  const atomicAmount = item.amount_atomic
    ? BigInt(item.amount_atomic)
    : BigInt(item.amount_tlama) * 10n ** BigInt(decimals);
  const result = await state.gameSettlementHook.purchase({
    item: Object.freeze({
      id: item.id,
      kind: item.kind,
      label: item.label,
      amount_tlama: item.amount_tlama,
      amount_atomic: atomicAmount.toString(),
      oracle_quote: item.oracle_quote,
    }),
    settlement: Object.freeze({
      mint_address: config.mint_address,
      hook_program_address: economy.hook_program_address,
      reward_vault_address: economy.reward_vault_address,
      required_route_percent: 100,
      ...(isHeartMatrix ? { quote_expires_at: quoteExpiresAt } : {}),
    }),
    wallet: state.adapter,
    rpc_url: config.rpc_url,
  });
  if (
    !result?.signature ||
    result.confirmed !== true ||
    String(result.routed_amount_atomic) !== atomicAmount.toString() ||
    (
      isHeartMatrix &&
      (
        !Number.isSafeInteger(Number(result.block_time)) ||
        Number(result.block_time) > quoteExpiresAt
      )
    )
  ) {
    throw new Error("The settlement hook did not prove a confirmed full-value vault route.");
  }
  return isHeartMatrix ? { ...result, oracle_quote: item.oracle_quote } : result;
};

const safely = (action) => async () => {
  try {
    await action();
  } catch (error) {
    setStatus(error?.message || "The wallet action was not completed.");
  }
};

try {
  const response = await fetch("./payment-config", { cache: "no-store" });
  if (response.ok) state.config = await response.json();
} catch {
  // Static mode uses config.js, which is disabled by default.
}

document.querySelectorAll("[data-wallet-adapter]").forEach((button) => {
  button.disabled = !walletConfigured();
  button.addEventListener("click", safely(() => connect(button.dataset.walletAdapter)));
});
elements.quoteButton.addEventListener("click", safely(requestQuote));
elements.swapButton.addEventListener("click", safely(executeSwap));

window.TLAMA_WEB3 = {
  connect,
  registerGameSettlementHook,
  purchaseGameItem,
  getWallet: () =>
    state.adapter?.publicKey
      ? { provider: state.adapter, publicKey: state.adapter.publicKey }
      : null,
  refreshBalances,
  checkTokenAffordability,
  getBalances: () => ({ ...state.balances }),
};