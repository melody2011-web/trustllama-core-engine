import { existsSync, readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { Router, type IRouter } from "express";
import { GetPaperStatusResponse } from "@workspace/api-zod";
import { logger } from "../lib/logger";

const router: IRouter = Router();
const PAPER_SYMBOLS = ["XRP", "BTC"] as const;
const RECENT_FILL_LIMIT = 20;
const DEFAULT_POLL_INTERVAL_SECONDS = 60;
const DEFAULT_SLOW_WINDOW = 21;
const DEFAULT_RSI_WINDOW = 14;

type PaperSymbol = (typeof PAPER_SYMBOLS)[number];
type JsonRecord = Record<string, unknown>;

class PaperStatusError extends Error {}

function asRecord(value: unknown, label: string): JsonRecord {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new PaperStatusError(`Paper state contains an invalid ${label}.`);
  }
  return value as JsonRecord;
}

function decimalString(value: unknown, label: string): string {
  const result = typeof value === "string" ? value.trim() : String(value);
  if (
    !/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(result) ||
    !Number.isFinite(Number(result)) ||
    Number(result) < 0
  ) {
    throw new PaperStatusError(`Paper state contains an invalid ${label}.`);
  }
  return result;
}

function timestamp(value: unknown, label: string): number {
  const result = typeof value === "number" ? value : Number(value ?? 0);
  if (!Number.isFinite(result) || result < 0) {
    throw new PaperStatusError(`Paper state contains an invalid ${label}.`);
  }
  return result;
}

function isoTimestamp(value: number): string | null {
  return value > 0 ? new Date(value * 1000).toISOString() : null;
}

function positiveEnvironmentNumber(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isInteger(value) || value <= 0) {
    throw new PaperStatusError(`${name} must be a positive integer.`);
  }
  return value;
}

function statePath(): string {
  return (
    process.env["PAPER_STATE_FILE"]?.trim() ||
    join(homedir(), ".local", "state", "bnb-defi-bot", "paper.json")
  );
}


function defaultState(): JsonRecord {
  return {
    balances: {
      BNB: decimalString(process.env["PAPER_STARTING_BNB"] ?? "1", "BNB balance"),
      XRP: decimalString(process.env["PAPER_STARTING_XRP"] ?? "0", "XRP balance"),
      BTC: decimalString(process.env["PAPER_STARTING_BTC"] ?? "0", "BTC balance"),
    },
    positions: {},
    prices: { XRP: [], BTC: [] },
    trades: [],
    last_successful_quote_at: 0,
    last_successful_quote_at_by_symbol: { XRP: 0, BTC: 0 },
    last_loop_success_at: 0,
    last_loop_error_at: 0,
    last_loop_error: null,
  };
}


function readStateAt(path: string): { state: JsonRecord; updatedAt: string | null } {
  if (!existsSync(path)) {
    return { state: {}, updatedAt: null };
  }
  let state: unknown;
  try {
    state = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new PaperStatusError("Paper state file is unreadable.");
  }
  const record = asRecord(state, "state");
  if (record.version !== undefined && record.version !== 1) {
    throw new PaperStatusError("Paper state file version is unsupported.");
  }
  let updatedAt: string | null = null;
  try {
    updatedAt = statSync(path).mtime.toISOString();
  } catch {
    // The journal is atomically replaced by the paper loop; a concurrent replace
    // can make its metadata unavailable even though the state read succeeded.
  }
  return { state: record, updatedAt };
}

function readState(): { state: JsonRecord; updatedAt: string | null } {
  const result = readStateAt(statePath());
  return result.state.version === undefined
    ? { state: defaultState(), updatedAt: result.updatedAt }
    : result;
}

function readBalances(state: JsonRecord): Record<PaperSymbol | "BNB", string> {
  const balances = asRecord(state.balances ?? {}, "balances");
  return {
    BNB: decimalString(balances.BNB ?? 0, "BNB balance"),
    XRP: decimalString(balances.XRP ?? 0, "XRP balance"),
    BTC: decimalString(balances.BTC ?? 0, "BTC balance"),
  };
}


function readPositions(state: JsonRecord): Array<{
  symbol: PaperSymbol;
  amount: string;
  entryPrice: string;
}> {
  const positions = asRecord(state.positions ?? {}, "positions");
  return Object.entries(positions).map(([symbol, value]) => {
    if (!PAPER_SYMBOLS.includes(symbol as PaperSymbol)) {
      throw new PaperStatusError("Paper state contains an invalid position.");
    }
    const position = asRecord(value, `${symbol} position`);
    return {
      symbol: symbol as PaperSymbol,
      amount: decimalString(position.amount, `${symbol} position amount`),
      entryPrice: decimalString(position.entry_price, `${symbol} entry price`),
    };
  });
}

function readPrices(state: JsonRecord): Record<PaperSymbol, number> {
  const prices = asRecord(state.prices ?? {}, "prices");
  return Object.fromEntries(
    PAPER_SYMBOLS.map((symbol) => {
      const samples = prices[symbol];
      if (!Array.isArray(samples)) {
        throw new PaperStatusError(`Paper state contains invalid ${symbol} prices.`);
      }
      return [symbol, samples.length];
    }),
  ) as Record<PaperSymbol, number>;
}

function readFills(state: JsonRecord): Array<{
  at: string;
  action: string;
  symbol: string;
  amountIn: string;
  amountOut: string;
  price: string;
  reason: string;
  outcome: string;
}> {
  const trades = state.trades ?? [];
  if (!Array.isArray(trades)) {
    throw new PaperStatusError("Paper state contains invalid fills.");
  }
  return trades
    .slice(-RECENT_FILL_LIMIT)
    .reverse()
    .map((value) => {
      const trade = asRecord(value, "fill");
      return {
        at: new Date(timestamp(trade.at, "fill timestamp") * 1000).toISOString(),
        action: String(trade.action ?? ""),
        symbol: String(trade.symbol ?? ""),
        amountIn: decimalString(trade.amountIn, "fill input amount"),
        amountOut: decimalString(trade.amountOut, "fill output amount"),
        price: decimalString(trade.price, "fill price"),
        reason: String(trade.reason ?? ""),
        outcome: String(trade.outcome ?? ""),
      };
    });
}

function statusFor(
  quoteTimes: Record<PaperSymbol, number>,
  sampleCounts: Record<PaperSymbol, number>,
  requiredSamples: number,
  loopSuccessAt: number,
  loopErrorAt: number,
  now: number,
  staleAfterSeconds: number,
): "healthy" | "warming_up" | "stale" | "degraded" | "not_started" {
  const hasQuote = PAPER_SYMBOLS.some((symbol) => quoteTimes[symbol] > 0);
  if (!hasQuote) {
    return "not_started";
  }
  if (loopErrorAt > loopSuccessAt) {
    return "degraded";
  }
  if (
    PAPER_SYMBOLS.some(
      (symbol) =>
        quoteTimes[symbol] <= 0 ||
        now - quoteTimes[symbol] > staleAfterSeconds,
    )
  ) {
    return "stale";
  }
  if (PAPER_SYMBOLS.some((symbol) => sampleCounts[symbol] < requiredSamples)) {
    return "warming_up";
  }
  return "healthy";
}

router.get("/paper/status", (_req, res) => {
  try {
    const { state, updatedAt } = readState();
    const balances = readBalances(state);
    const positions = readPositions(state);
    const sampleCounts = readPrices(state);
    const requiredSamples = Math.max(
      positiveEnvironmentNumber("STRATEGY_SLOW_WINDOW", DEFAULT_SLOW_WINDOW) + 1,
      positiveEnvironmentNumber("STRATEGY_RSI_WINDOW", DEFAULT_RSI_WINDOW) + 1,
    );
    const rawQuoteTimes = asRecord(
      state.last_successful_quote_at_by_symbol ?? {},
      "quote health metadata",
    );
    const quoteTimes = Object.fromEntries(
      PAPER_SYMBOLS.map((symbol) => [
        symbol,
        timestamp(rawQuoteTimes[symbol], `${symbol} quote timestamp`),
      ]),
    ) as Record<PaperSymbol, number>;
    const lastSuccessfulQuoteAt = timestamp(
      state.last_successful_quote_at,
      "last successful quote timestamp",
    );
    const loopSuccessAt = timestamp(state.last_loop_success_at, "loop success timestamp");
    const loopErrorAt = timestamp(state.last_loop_error_at, "loop error timestamp");
    const lastLoopError =
      state.last_loop_error === null || state.last_loop_error === undefined
        ? null
        : String(state.last_loop_error);
    const now = Date.now() / 1000;
    const staleAfterSeconds = Math.max(
      positiveEnvironmentNumber(
        "PAPER_POLL_INTERVAL_SECONDS",
        DEFAULT_POLL_INTERVAL_SECONDS,
      ) * 2,
      60,
    );

    const response = {
      mode: "paper",
      liveExecution: false,
      status: statusFor(
        quoteTimes,
        sampleCounts,
        requiredSamples,
        loopSuccessAt,
        loopErrorAt,
        now,
        staleAfterSeconds,
      ),
      balances,
      openPositions: positions,
      recentFills: readFills(state),
      warmup: Object.fromEntries(
        PAPER_SYMBOLS.map((symbol) => [
          symbol,
          {
            samples: sampleCounts[symbol],
            required: requiredSamples,
            ready: sampleCounts[symbol] >= requiredSamples,
          },
        ]),
      ),
      lastSuccessfulQuoteAt: isoTimestamp(lastSuccessfulQuoteAt),
      lastSuccessfulQuoteAtBySymbol: Object.fromEntries(
        PAPER_SYMBOLS.map((symbol) => [symbol, isoTimestamp(quoteTimes[symbol])]),
      ),
      lastLoopSuccessAt: isoTimestamp(loopSuccessAt),
      lastLoopErrorAt: isoTimestamp(loopErrorAt),
      lastLoopError,
      stateUpdatedAt: updatedAt,
    };
    res.json(GetPaperStatusResponse.parse(response));
  } catch (error) {
    const message = error instanceof PaperStatusError ? error.message : "Paper status is unavailable.";
    logger.error({ err: error }, "Paper status could not be read");
    res.status(503).json({ ok: false, error: message });
  }
});

export default router;