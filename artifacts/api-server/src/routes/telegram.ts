import { createHmac, timingSafeEqual } from "node:crypto";
import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname } from "node:path";
import { Router, type IRouter, type Request } from "express";
import { logger } from "../lib/logger";

type TradeNotification = {
  status: "success";
  environment: "live";
  live: true;
  action: string;
  symbol: string;
  amountIn: string;
  amountOut: string;
  txHash: string;
  blockNumber: number;
};

type DeliveryState = {
  deliveredTxHashes: string[];
  inFlightTxHashes: string[];
  auditEvents: AuditEvent[];
};

type AuditEvent = {
  at: string;
  txHash: string;
  resolution: "delivered" | "resend";
  outcome: "requested" | "delivered" | "rejected" | "unknown";
};

export type ConfirmedAlertReconciliation = {
  txHash: string;
  action: string;
  symbol: string;
  blockNumber: number;
  confirmedAt: string | null;
  resendAvailable: boolean;
};

const router: IRouter = Router();
const TRADE_AUTH_CONTEXT = "bnb-defi-bot/telegram-notification/v1";
const RECONCILIATION_AUTH_CONTEXT =
  "bnb-defi-bot/telegram-reconciliation/v1";
const MAX_HISTORY = 1000;
let deliveryLock: Promise<void> = Promise.resolve();

function telegramServicesEnabled(): boolean {
  const configured = process.env["TELEGRAM_SERVICES_ENABLED"];
  return configured === undefined || configured.trim().toLowerCase() === "true";
}

function hasAccess(req: Request, context: string): boolean {
  const secret = process.env["SESSION_SECRET"];
  const supplied = req.get("X-Internal-Notification-Token");
  if (!secret || !supplied) return false;
  const expected = createHmac("sha256", secret).update(context).digest("hex");
  const left = Buffer.from(supplied);
  const right = Buffer.from(expected);
  return left.length === right.length && timingSafeEqual(left, right);
}

function liveStatePath(): string {
  return (
    process.env["LIVE_PRICE_HISTORY_FILE"]?.trim() ??
    `${process.cwd()}/.live-strategy-prices.json`
  );
}

function deliveryPath(): string {
  return (
    process.env["TRADE_NOTIFICATION_DELIVERY_FILE"]?.trim() ??
    `${liveStatePath()}.trade-notification-delivery.json`
  );
}

function writeAtomic(path: string, value: unknown): void {
  mkdirSync(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.tmp`;
  const fd = openSync(temporary, "w", 0o600);
  try {
    writeFileSync(fd, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  renameSync(temporary, path);
  const directory = openSync(dirname(path), "r");
  try {
    fsyncSync(directory);
  } finally {
    closeSync(directory);
  }
}

function readDelivery(): DeliveryState {
  if (!existsSync(deliveryPath())) {
    return { deliveredTxHashes: [], inFlightTxHashes: [], auditEvents: [] };
  }
  const value = JSON.parse(readFileSync(deliveryPath(), "utf8")) as DeliveryState;
  if (
    !Array.isArray(value.deliveredTxHashes) ||
    !Array.isArray(value.inFlightTxHashes)
  ) {
    throw new Error("Trade notification delivery journal is invalid.");
  }
  return {
    deliveredTxHashes: value.deliveredTxHashes.map(String),
    inFlightTxHashes: value.inFlightTxHashes.map(String),
    auditEvents: Array.isArray(value.auditEvents)
      ? value.auditEvents as AuditEvent[]
      : [],
  };
}

async function claimControlledResend(txHash: string): Promise<boolean> {
  return withDeliveryLock(async () => {
    const state = readDelivery();
    const normalized = txHash.toLowerCase();
    if (
      !state.inFlightTxHashes.includes(normalized) ||
      state.deliveredTxHashes.includes(normalized)
    ) {
      return false;
    }
    const alreadyClaimed = state.auditEvents.some(
      (event) =>
        event.txHash.toLowerCase() === txHash.toLowerCase() &&
        event.resolution === "resend" &&
        event.outcome === "requested",
    );
    if (alreadyClaimed) return false;
    state.auditEvents.push({
      at: new Date().toISOString(),
      txHash,
      resolution: "resend",
      outcome: "requested",
    });
    writeAtomic(deliveryPath(), state);
    return true;
  });
}

async function resolveAsDelivered(txHash: string): Promise<boolean> {
  return withDeliveryLock(async () => {
    const state = readDelivery();
    const normalized = txHash.toLowerCase();
    if (
      !state.inFlightTxHashes.includes(normalized) ||
      state.deliveredTxHashes.includes(normalized)
    ) {
      return false;
    }
    state.auditEvents.push({
      at: new Date().toISOString(),
      txHash,
      resolution: "delivered",
      outcome: "requested",
    });
    state.inFlightTxHashes = state.inFlightTxHashes.filter(
      (value) => value !== normalized,
    );
    state.deliveredTxHashes = [
      ...state.deliveredTxHashes.filter((value) => value !== normalized),
      normalized,
    ].slice(-MAX_HISTORY);
    state.auditEvents.push({
      at: new Date().toISOString(),
      txHash,
      resolution: "delivered",
      outcome: "delivered",
    });
    writeAtomic(deliveryPath(), state);
    return true;
  });
}

function withDeliveryLock<T>(operation: () => Promise<T>): Promise<T> {
  const previous = deliveryLock;
  let release!: () => void;
  deliveryLock = new Promise<void>((resolve) => {
    release = resolve;
  });
  return previous
    .then(async () => {
      const lockPath = `${deliveryPath()}.lock`;
      mkdirSync(dirname(lockPath), { recursive: true });
      let lock: number | undefined;
      for (let attempt = 0; attempt < 2 && lock === undefined; attempt += 1) {
        try {
          lock = openSync(lockPath, "wx", 0o600);
          writeFileSync(lock, String(process.pid), "utf8");
          fsyncSync(lock);
        } catch {
          try {
            const owner = Number(readFileSync(lockPath, "utf8"));
            if (!Number.isSafeInteger(owner) || owner <= 0) {
              const ageMilliseconds = Date.now() - statSync(lockPath).mtimeMs;
              if (ageMilliseconds > 30_000) {
                unlinkSync(lockPath);
                continue;
              }
              throw new Error("Trade notification delivery journal is busy.");
            }
            process.kill(owner, 0);
          } catch (error) {
            if (
              error &&
              typeof error === "object" &&
              "code" in error &&
              (error as NodeJS.ErrnoException).code === "ESRCH"
            ) {
              unlinkSync(lockPath);
              continue;
            }
          }
          throw new Error("Trade notification delivery journal is busy.");
        }
      }
      if (lock === undefined) throw new Error("Could not acquire delivery journal.");
      try {
        return await operation();
      } finally {
        closeSync(lock);
        unlinkSync(lockPath);
      }
    })
    .finally(release);
}

function isTrade(value: unknown): value is TradeNotification {
  if (!value || typeof value !== "object") return false;
  const body = value as Record<string, unknown>;
  return (
    body.status === "success" &&
    body.environment === "live" &&
    body.live === true &&
    typeof body.action === "string" &&
    typeof body.symbol === "string" &&
    typeof body.amountIn === "string" &&
    typeof body.amountOut === "string" &&
    typeof body.txHash === "string" &&
    /^0x[a-fA-F0-9]{64}$/.test(body.txHash) &&
    Number.isSafeInteger(body.blockNumber)
  );
}

function escapeText(value: string): string {
  return value.replace(/[<>&]/g, (character) => {
    return { "<": "&lt;", ">": "&gt;", "&": "&amp;" }[character] ?? character;
  });
}

function tradeMessage(trade: TradeNotification): string {
  return [
    "LIVE TRADE CONFIRMED",
    `${escapeText(trade.action.toUpperCase())} ${escapeText(trade.symbol)}`,
    `Input: ${escapeText(trade.amountIn)}`,
    `Output: ${escapeText(trade.amountOut)}`,
    `Tx: https://bscscan.com/tx/${trade.txHash}`,
    `Block: ${trade.blockNumber}`,
  ].join("\n");
}

async function sendTelegram(message: string): Promise<"delivered" | "rejected" | "unknown"> {
  const chatId = process.env["TELEGRAM_CHAT_ID"];
  const token = process.env["TELEGRAM_BOT_TOKEN"];
  if (!chatId || !token) return "rejected";
  try {
    const response = await fetch(
      `https://api.telegram.org/bot${token}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, text: message }),
        signal: AbortSignal.timeout(15_000),
      },
    );
    const body = (await response.json()) as { ok?: unknown };
    return response.ok && body.ok === true ? "delivered" : "rejected";
  } catch (error) {
    logger.error({ err: error }, "Telegram notification outcome is unknown");
    return "unknown";
  }
}

async function deliver(
  trade: TradeNotification,
  controlledResend = false,
): Promise<"delivered" | "duplicate" | "rejected" | "unknown" | "uncertain"> {
  return withDeliveryLock(async () => {
    const hash = trade.txHash.toLowerCase();
    const state = readDelivery();
    if (state.deliveredTxHashes.includes(hash)) return "duplicate";
    if (state.inFlightTxHashes.includes(hash) && !controlledResend) {
      return "uncertain";
    }
    state.inFlightTxHashes = [
      ...state.inFlightTxHashes.filter((value) => value !== hash),
      hash,
    ].slice(-MAX_HISTORY);
    writeAtomic(deliveryPath(), state);
    const outcome = await sendTelegram(tradeMessage(trade));
    if (outcome === "unknown") {
      if (controlledResend) {
        state.auditEvents.push({
          at: new Date().toISOString(),
          txHash: trade.txHash,
          resolution: "resend",
          outcome,
        });
        writeAtomic(deliveryPath(), state);
      }
      return outcome;
    }
    state.inFlightTxHashes = state.inFlightTxHashes.filter(
      (value) => value !== hash,
    );
    if (outcome === "delivered") {
      state.deliveredTxHashes = [
        ...state.deliveredTxHashes.filter((value) => value !== hash),
        hash,
      ].slice(-MAX_HISTORY);
    }
    if (controlledResend) {
      state.auditEvents.push({
        at: new Date().toISOString(),
        txHash: trade.txHash,
        resolution: "resend",
        outcome,
      });
    }
    writeAtomic(deliveryPath(), state);
    return outcome;
  });
}

function pendingTrade(txHash: string): TradeNotification | null {
  if (!existsSync(liveStatePath())) return null;
  const state = JSON.parse(readFileSync(liveStatePath(), "utf8")) as {
    notifications?: { pending?: unknown };
  };
  const pending = state.notifications?.pending;
  if (!Array.isArray(pending)) return null;
  const item = pending.find(
    (entry) =>
      entry &&
      typeof entry === "object" &&
      String((entry as Record<string, unknown>).tx_hash).toLowerCase() ===
        txHash.toLowerCase(),
  ) as Record<string, unknown> | undefined;
  if (!item) return null;
  const candidate = {
    status: "success",
    environment: "live",
    live: true,
    action: String(item.action),
    symbol: String(item.symbol),
    amountIn: String(item.amount_in),
    amountOut: String(item.amount_out),
    txHash: String(item.tx_hash),
    blockNumber: Number(item.block_number),
  } as const;
  return isTrade(candidate) ? candidate : null;
}

export function getUncertainTradeAlerts(): ConfirmedAlertReconciliation[] {
  if (!existsSync(liveStatePath())) return [];
  const state = JSON.parse(readFileSync(liveStatePath(), "utf8")) as {
    notifications?: { pending?: unknown };
  };
  const pending = state.notifications?.pending;
  if (!Array.isArray(pending)) return [];
  const delivery = readDelivery();
  return pending.flatMap((value) => {
    if (!value || typeof value !== "object") return [];
    const item = value as Record<string, unknown>;
    const txHash = String(item.tx_hash ?? "");
    const normalized = txHash.toLowerCase();
    if (
      !/^0x[a-fA-F0-9]{64}$/.test(txHash) ||
      !delivery.inFlightTxHashes.includes(normalized) ||
      delivery.deliveredTxHashes.includes(normalized)
    ) {
      return [];
    }
    return [{
      txHash,
      action: String(item.action ?? "unknown"),
      symbol: String(item.symbol ?? "unknown"),
      blockNumber: Number(item.block_number ?? 0),
      confirmedAt:
        typeof item.confirmed_at === "number"
          ? new Date(item.confirmed_at * 1000).toISOString()
          : null,
      resendAvailable: !delivery.auditEvents.some(
        (event) =>
          event.txHash.toLowerCase() === normalized &&
          event.resolution === "resend" &&
          event.outcome === "requested",
      ),
    }];
  });
}

router.post("/notifications/trade", async (req, res) => {
  if (!telegramServicesEnabled()) {
    res.json({ ok: true, disabled: true });
    return;
  }
  if (!hasAccess(req, TRADE_AUTH_CONTEXT)) {
    res.status(401).json({ ok: false, error: "Internal authentication is required." });
    return;
  }
  if (!isTrade(req.body)) {
    res.status(400).json({ ok: false, error: "A confirmed live trade is required." });
    return;
  }
  const outcome = await deliver(req.body);
  if (outcome === "delivered" || outcome === "duplicate") {
    res.json({ ok: true, duplicate: outcome === "duplicate" });
    return;
  }
  res.status(502).json({ ok: false, error: "Telegram delivery is unconfirmed." });
});

router.post("/notifications/reconciliation/:txHash", async (req, res) => {
  if (!telegramServicesEnabled()) {
    res.json({ ok: true, disabled: true });
    return;
  }
  if (!hasAccess(req, RECONCILIATION_AUTH_CONTEXT)) {
    res.status(401).json({ ok: false, error: "Operator authentication is required." });
    return;
  }
  const resolution = req.body?.resolution;
  if (resolution !== "delivered" && resolution !== "resend") {
    res.status(400).json({ ok: false, error: "Resolution must be delivered or resend." });
    return;
  }
  const trade = pendingTrade(req.params.txHash);
  const uncertain = getUncertainTradeAlerts().find(
    (item) => item.txHash.toLowerCase() === req.params.txHash.toLowerCase(),
  );
  if (!trade || !uncertain) {
    res.status(409).json({ ok: false, error: "Alert is not awaiting reconciliation." });
    return;
  }
  if (resolution === "resend" && !uncertain.resendAvailable) {
    res.status(409).json({ ok: false, error: "The controlled resend was already used." });
    return;
  }

  if (resolution === "delivered") {
    if (!(await resolveAsDelivered(trade.txHash))) {
      res.status(409).json({ ok: false, error: "Alert is already resolved." });
      return;
    }
    res.json({ ok: true, outcome: "delivered" });
    return;
  }

  if (!(await claimControlledResend(trade.txHash))) {
    res.status(409).json({ ok: false, error: "The controlled resend was already used." });
    return;
  }
  const outcome = await deliver(trade, true);
  if (outcome === "delivered" || outcome === "duplicate") {
    res.json({ ok: true, outcome: "delivered" });
    return;
  }
  res.status(502).json({ ok: false, outcome, error: "Controlled resend was not confirmed." });
});

export default router;