import helmet from "helmet";
import { ipKeyGenerator, rateLimit } from "express-rate-limit";
import { logger } from "../lib/logger";
import { PostgresRateLimitStore } from "./postgres-rate-limit-store";
import type { NextFunction, Request, Response } from "express";
import cors, { type CorsOptions } from "cors";

const DEFAULT_RATE_LIMIT_WINDOW_MS = 15 * 60 * 1000;
const DEFAULT_RATE_LIMIT_MAX = 300;

const ARCADE_OWNERSHIP_HEADERS = [
  "Content-Type",
  "X-Tlama-Player-Key",
  "X-Tlama-Ownership-Token",
  "X-Tlama-CSRF",
  "X-Tlama-License-Checkout",
];
const STORE_FAILURE_POLICIES = ["fail-closed", "fail-open"] as const;

function rateLimitStoreFailurePolicy(): (typeof STORE_FAILURE_POLICIES)[number] {
  const policy =
    process.env.API_RATE_LIMIT_STORE_FAILURE_POLICY?.trim() || "fail-closed";
  if (!STORE_FAILURE_POLICIES.includes(policy as never)) {
    throw new Error(
      "API_RATE_LIMIT_STORE_FAILURE_POLICY must be fail-closed or fail-open.",
    );
  }
  return policy as (typeof STORE_FAILURE_POLICIES)[number];
}

function positiveIntegerEnvironment(name: string, fallback: number): number {
  const raw = process.env[name]?.trim();
  if (!raw) return fallback;

  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new Error(`${name} must be a positive integer.`);
  }
  return value;
}

export const securityHeaders = helmet({
  crossOriginResourcePolicy: { policy: "same-site" },
  xFrameOptions: { action: "deny" },
  contentSecurityPolicy: {
    directives: {
      defaultSrc: ["'none'"],
      frameAncestors: ["'none'"],
    },
  },
});

const useSharedStore = process.env.NODE_ENV === "production";
const storeFailurePolicy = rateLimitStoreFailurePolicy();

logger.info(
  {
    store: useSharedStore ? "postgres" : "memory",
    storeFailurePolicy,
  },
  "Configured API rate limiter",
);

export const apiRateLimiter = rateLimit({
  windowMs: positiveIntegerEnvironment(
    "API_RATE_LIMIT_WINDOW_MS",
    DEFAULT_RATE_LIMIT_WINDOW_MS,
  ),
  limit: positiveIntegerEnvironment("API_RATE_LIMIT_MAX", DEFAULT_RATE_LIMIT_MAX),
  standardHeaders: "draft-8",
  legacyHeaders: false,
  store: useSharedStore ? new PostgresRateLimitStore() : undefined,
  passOnStoreError: storeFailurePolicy === "fail-open",
  skip: (req) =>
    req.method === "OPTIONS" || req.path === "/" || req.path === "/healthz",
  keyGenerator: (req) =>
    ipKeyGenerator(req.ip ?? req.socket.remoteAddress ?? "unknown"),
  handler: (req: Request, res: Response) => {
    req.log.warn(
      {
        clientIp: req.ip,
        method: req.method,
        path: req.path,
      },
      "API rate limit exceeded",
    );
    res.status(429).json({
      ok: false,
      error: "Too many requests. Please try again later.",
    });
  },
});

function originsFromEnvironment(name: string): string[] {
  return (process.env[name] ?? "")
    .split(",")
    .map(normalizedOrigin)
    .filter((origin): origin is string => origin !== null);
}

export function allowedCorsOrigins(): ReadonlySet<string> {
  const origins = new Set([
    ...originsFromEnvironment("REPLIT_DOMAINS"),
    ...originsFromEnvironment("CORS_ALLOWED_ORIGINS"),
  ]);

  if (process.env.NODE_ENV !== "production") {
    for (const origin of originsFromEnvironment("REPLIT_DEV_DOMAIN")) {
      origins.add(origin);
    }
    origins.add("http://localhost");
    origins.add("http://localhost:5173");
    origins.add("http://127.0.0.1");
    origins.add("http://127.0.0.1:5173");
  }

  return origins;
}

class CorsOriginError extends Error {
  readonly status = 403;
  readonly origin: string;

  constructor(origin: string) {
    super("Origin is not allowed to access this API.");
    this.name = "CorsOriginError";
    this.origin = origin;
  }
}

function normalizedOrigin(value: string): string | null {
  const candidate = value.trim();
  if (!candidate) return null;

  const withProtocol = candidate.includes("://")
    ? candidate
    : `https://${candidate}`;
  try {
    const url = new URL(withProtocol);
    if (
      (url.protocol !== "http:" && url.protocol !== "https:") ||
      url.username ||
      url.password ||
      (url.pathname !== "/" && url.pathname !== "") ||
      url.search ||
      url.hash
    ) {
      throw new Error();
    }
    return url.origin;
  } catch {
    throw new Error(`Invalid CORS origin: "${candidate}".`);
  }
}

export function corsErrorHandler(
  error: Error,
  req: Request,
  res: Response,
  next: NextFunction,
): void {
  if (!(error instanceof CorsOriginError)) {
    next(error);
    return;
  }

  req.log.warn({ origin: error.origin }, "CORS origin rejected");
  res.status(error.status).json({
    ok: false,
    error: "This website is not allowed to access the TrustLlama API.",
  });
}

const corsOptions: CorsOptions = {
  origin(origin, callback) {
    if (!origin || allowedCorsOrigins().has(origin)) {
      callback(null, true);
      return;
    }
    callback(new CorsOriginError(origin));
  },
  methods: ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  allowedHeaders: ARCADE_OWNERSHIP_HEADERS,
  optionsSuccessStatus: 204,
};

export const apiCors = cors(corsOptions);
