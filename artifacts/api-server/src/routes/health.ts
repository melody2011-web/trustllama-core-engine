import {
  existsSync,
  readFileSync,
  statSync,
} from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Router, type IRouter } from "express";
import { GetLiveStatusResponse, HealthCheckResponse } from "@workspace/api-zod";
import { logger } from "../lib/logger";
import { getUncertainTradeAlerts } from "./telegram";

const router: IRouter = Router();
const DEFAULT_POLL_INTERVAL_SECONDS = 60;
const LIVE_STATE_VERSION = 3;
type JsonRecord = Record<string, unknown>;
type LiveStatus = "disabled" | "not_started" | "healthy" | "stale" | "retrying";

class LiveStatusError extends Error {}

function asRecord(value: unknown, label: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new LiveStatusError(`Live state contains an invalid ${label}.`);
  }
  return value as JsonRecord;
}

function timestamp(value: unknown, label: string): number {
  const result = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(result) || result < 0) {
    throw new LiveStatusError(`Live state contains an invalid ${label}.`);
  }
  return result;
}

function nonnegativeInteger(value: unknown, label: string): number {
  const result = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isSafeInteger(result) || result < 0) {
    throw new LiveStatusError(`Live state contains an invalid ${label}.`);
  }
  return result;
}

function isoTimestamp(value: number): string | null {
  return value > 0 ? new Date(value * 1000).toISOString() : null;
}

function liveStrategyMode(value: unknown): "disabled" | "ema" | "grid" {
  if (value === "disabled" || value === "ema" || value === "grid") {
    return value;
  }
  return "disabled";
}

function nullableString(value: unknown): string | null {
  return value === null || value === undefined ? null : String(value);
}

function gridLot(value: unknown): JsonRecord {
  const lot = asRecord(value, "grid lot");
  return {
    level: nonnegativeInteger(lot.level, "grid lot level"),
    amountToken: String(lot.amountToken ?? ""),
    buyAmountUsdt: String(lot.buyAmountUsdt ?? ""),
    buyPrice: String(lot.buyPrice ?? ""),
    targetPrice: String(lot.targetPrice ?? ""),
  };
}

function lastSellOutcome(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  const outcome = asRecord(value, "last grid sell outcome").outcome;
  if (outcome !== "confirmed" && outcome !== "reverted") {
    throw new LiveStatusError("Live state contains an invalid grid sell outcome.");
  }
  return outcome;
}

function gridProgress(value: unknown): JsonRecord {
  const progress = value ? asRecord(value, "grid progress") : {};
  const state = progress.state;
  if (
    state !== "not_started" &&
    state !== "active" &&
    state !== "complete" &&
    state !== "paused_at_floor"
  ) {
    return {
      anchorPrice: null,
      nextLevel: null,
      filledLevels: 0,
      totalLevels: 0,
      spentUsdt: "0",
      remainingUsdt: "0",
      activeExposureUsdt: "0",
      realizedUsdt: "0",
      openLots: [],
      lastSellOutcome: null,
      state: "not_started",
    };
  }
  const nextLevel =
    progress.nextLevel === null || progress.nextLevel === undefined
      ? null
      : nonnegativeInteger(progress.nextLevel, "grid next level");
  return {
    anchorPrice: nullableString(progress.anchorPrice),
    nextLevel,
    filledLevels: nonnegativeInteger(progress.filledLevels, "filled grid levels"),
    totalLevels: nonnegativeInteger(progress.totalLevels, "total grid levels"),
    spentUsdt: String(progress.spentUsdt ?? "0"),
    remainingUsdt: String(progress.remainingUsdt ?? "0"),
    activeExposureUsdt: String(
      progress.activeExposureUsdt ?? progress.spentUsdt ?? "0",
    ),
    realizedUsdt: String(progress.realizedUsdt ?? "0"),
    openLots: Array.isArray(progress.openLots)
      ? progress.openLots.map(gridLot)
      : [],
    lastSellOutcome: lastSellOutcome(progress.lastSellOutcome),
    state,
  };
}

function liveTradingEnabled(): boolean {
  return ["1", "true", "yes", "on"].includes(
    process.env["ENABLE_LIVE_TRADING"]?.trim().toLowerCase() ?? "",
  );
}

function statePath(): string {
  const configured = process.env["LIVE_PRICE_HISTORY_FILE"]?.trim();
  if (configured) {
    return configured;
  }
  const walletAddress = process.env["WALLET_ADDRESS"]?.trim().toLowerCase();
  const filename = walletAddress
    ? `${walletAddress}.live-prices.json`
    : "live-prices.json";
  return join(homedir(), ".local", "state", "bnb-defi-bot", filename);
}

function readState(): { state: JsonRecord; updatedAt: string | null } {
  const path = statePath();
  if (!existsSync(path)) {
    return { state: {}, updatedAt: null };
  }
  let state: unknown;
  try {
    state = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new LiveStatusError("Live state file is unreadable.");
  }
  const record = asRecord(state, "state");
  if (record.version !== undefined && record.version !== LIVE_STATE_VERSION) {
    throw new LiveStatusError("Live state file version is unsupported.");
  }
  let updatedAt: string | null = null;
  try {
    updatedAt = statSync(path).mtime.toISOString();
  } catch {
    // The journal is atomically replaced by the live runner.
  }
  return { state: record, updatedAt };
}

function positiveEnvironmentNumber(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new LiveStatusError(`${name} must be a positive integer.`);
  }
  return value;
}

function statusFor(
  enabled: boolean,
  heartbeatAt: number,
  lastSuccessfulPollAt: number,
  lastErrorAt: number,
  now: number,
  staleAfterSeconds: number,
): LiveStatus {
  if (!enabled) {
    return "disabled";
  }
  const heartbeatIsRecent =
    heartbeatAt > 0 && now - heartbeatAt <= staleAfterSeconds;
  if (lastErrorAt > lastSuccessfulPollAt) {
    return heartbeatIsRecent ? "retrying" : "stale";
  }
  if (lastSuccessfulPollAt <= 0) {
    return heartbeatIsRecent ? "retrying" : "not_started";
  }
  return now - lastSuccessfulPollAt <= staleAfterSeconds ? "healthy" : "stale";
}

const sendHealth = (_req: unknown, res: { json: (data: unknown) => void }) => {
  const data = HealthCheckResponse.parse({ status: "ok" });
  res.json(data);
};

// Replit's artifact sidecar probes the mounted service base path during promote.
router.get("/", sendHealth);
router.get("/healthz", sendHealth);

router.get("/live/status", (_req, res) => {
  try {
    const { state, updatedAt } = readState();
    const strategy = state.strategy ? asRecord(state.strategy, "strategy") : {};
    const enabled =
      strategy.liveExecution === true || liveTradingEnabled();
    const strategyMode = liveStrategyMode(strategy.mode);
    const grid = strategy.grid ? asRecord(strategy.grid, "grid") : {};
    const heartbeatAt = timestamp(state.heartbeat_at, "heartbeat timestamp");
    const lastSuccessfulPollAt = timestamp(
      state.last_successful_poll_at,
      "successful poll timestamp",
    );
    const lastRetryAt = timestamp(state.last_retry_at, "retry timestamp");
    const lastRetryDelaySeconds = nonnegativeInteger(
      state.last_retry_delay_seconds,
      "retry delay",
    );
    const retryCount = nonnegativeInteger(state.retry_count, "retry count");
    const lastErrorAt = timestamp(state.last_error_at, "error timestamp");
    const lastError =
      state.last_error === null || state.last_error === undefined
        ? null
        : String(state.last_error);
    const now = Date.now() / 1000;
    const staleAfterSeconds = Math.max(
      positiveEnvironmentNumber(
        "LIVE_POLL_INTERVAL_SECONDS",
        DEFAULT_POLL_INTERVAL_SECONDS,
      ) * 2,
      60,
    );
    const polling =
      enabled && heartbeatAt > 0 && now - heartbeatAt <= staleAfterSeconds;
    const response = {
      mode: "live",
      liveExecution: enabled,
      strategyMode,
      grid: {
        XRP: gridProgress(grid.XRP),
        BTC: gridProgress(grid.BTC),
      },
      status: statusFor(
        enabled,
        heartbeatAt,
        lastSuccessfulPollAt,
        lastErrorAt,
        now,
        staleAfterSeconds,
      ),
      polling,
      heartbeatAt: isoTimestamp(heartbeatAt),
      lastSuccessfulPollAt: isoTimestamp(lastSuccessfulPollAt),
      lastRetryAt: isoTimestamp(lastRetryAt),
      retryDelaySeconds: lastRetryDelaySeconds,
      retryCount,
      lastErrorAt: isoTimestamp(lastErrorAt),
      lastError,
      confirmedAlertReconciliation: getUncertainTradeAlerts(),
      stateUpdatedAt: updatedAt,
    };
    res.json(GetLiveStatusResponse.parse(response));
  } catch (error) {
    const message =
      error instanceof LiveStatusError
        ? error.message
        : "Live status is unavailable.";
    logger.error({ err: error }, "Live status could not be read");
    res.status(503).json({ ok: false, error: message });
  }
});

export default router;
