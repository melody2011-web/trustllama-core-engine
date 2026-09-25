const WALLET_ADAPTERS = ["phantom", "solflare", "coinbase"];
const FAILURE_STAGES = ["unavailable_setup", "provider_rejected", "connection_failed"];

export const safeWalletAdapter = (kind) =>
  WALLET_ADAPTERS.includes(kind) ? kind : "unknown";

const hasExplicitRejectionSignal = (error) => {
  let current = error;
  for (let depth = 0; depth < 3 && current; depth += 1) {
    if (current.code === 4001 || current.name === "WalletWindowClosedError") return true;
    current = current.error;
  }
  return false;
};

export const connectionFailureStage = (error) =>
  hasExplicitRejectionSignal(error) ? "provider_rejected" : "connection_failed";

export const safeConnectionFailureDetail = (kind, stage) => ({
  adapter: safeWalletAdapter(kind),
  stage: FAILURE_STAGES.includes(stage) ? stage : "connection_failed",
});

export const dispatchConnectionFailure = (
  target,
  kind,
  stage,
  EventConstructor = CustomEvent
) => {
  target.dispatchEvent(
    new EventConstructor("tlama-wallet-connection-failed", {
      detail: safeConnectionFailureDetail(kind, stage),
    })
  );
};