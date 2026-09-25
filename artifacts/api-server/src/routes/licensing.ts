import {
  createHash,
  createHmac,
  randomBytes,
  randomUUID,
  timingSafeEqual,
} from "node:crypto";
import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import {
  Connection,
  Keypair,
  PublicKey,
  type ParsedTransactionWithMeta,
} from "@solana/web3.js";
import { pool } from "@workspace/db";
import { Router, type IRouter } from "express";
import { ipKeyGenerator, rateLimit } from "express-rate-limit";
import { verifiedCheckoutStatus } from "../lib/checkout-status";
import { archiveForTier } from "../lib/checkout-archives";

const router: IRouter = Router();

const RECIPIENT = new PublicKey(
  "sGLpe2FLbNfCcBt775jq9TWGVpeqoYfNephHtfYaXAH",
);
const USDC_MINT = new PublicKey(
  "EPjFW3gd5T71f72FcFc71U57UR3L6m93pH584CD6iz7b",
);
const RECIPIENT_USDC_ACCOUNT = getAssociatedTokenAddressSync(
  USDC_MINT,
  RECIPIENT,
);
const INTENT_TTL_MS = 30 * 60 * 1000;
const DOWNLOAD_TTL_MS = 60 * 60 * 1000;
const TIERS = {
  indie: {
    name: "Indie Developer License",
    amountUsdc: "350",
    amountMicroUsdc: 350_000_000,
  },
  startup: {
    name: "Startup Studio License",
    amountUsdc: "750",
    amountMicroUsdc: 750_000_000,
  },
  enterprise: {
    name: "Enterprise Commercial License",
    amountUsdc: "1950",
    amountMicroUsdc: 1_950_000_000,
  },
  webhook_rail_pro: {
    name: "Solana Webhook Core Rail Pro",
    amountUsdc: "450",
    amountMicroUsdc: 450_000_000,
  },
} as const;

type TierId = keyof typeof TIERS;
type IntentRow = {
  id: string;
  reference: string;
  tier: TierId;
  amount_micro_usdc: string;
  status: "pending" | "verified" | "expired";
  signature: string | null;
  checkout_token_hash: string | null;
  created_at: Date;
  expires_at: Date;
  verified_at: Date | null;
  download_expires_at: Date | null;
  download_claimed_at: Date | null;
  downloaded_at: Date | null;
};

const createIntentLimiter = rateLimit({
  windowMs: 10 * 60 * 1000,
  limit: 10,
  standardHeaders: "draft-8",
  legacyHeaders: false,
  keyGenerator: (req) =>
    ipKeyGenerator(req.ip ?? req.socket.remoteAddress ?? "unknown"),
});
const checkoutReadLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  limit: 240,
  standardHeaders: "draft-8",
  legacyHeaders: false,
  keyGenerator: (req) =>
    ipKeyGenerator(req.ip ?? req.socket.remoteAddress ?? "unknown"),
});

function connection(): Connection {
  const endpoint =
    process.env.SOLANA_RPC_URL?.trim() ||
    "https://api.mainnet-beta.solana.com";
  const url = new URL(endpoint);
  if (url.protocol !== "https:") {
    throw new Error("SOLANA_RPC_URL must use HTTPS.");
  }
  return new Connection(url.toString(), "finalized");
}

function hashToken(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

function downloadSecret(): string {
  const secret = process.env.SESSION_SECRET?.trim();
  if (!secret || secret.length < 32) {
    throw new Error("SESSION_SECRET must contain at least 32 characters.");
  }
  return secret;
}

function downloadTokenFor(intent: IntentRow): string {
  if (
    !intent.signature ||
    !intent.verified_at ||
    !intent.download_expires_at
  ) {
    throw new Error("Verified intent is missing fulfillment metadata.");
  }
  return createHmac("sha256", downloadSecret())
    .update(intent.id)
    .update("\0")
    .update(intent.signature)
    .update("\0")
    .update(intent.download_expires_at.toISOString())
    .digest("base64url");
}

function tokensMatch(actual: string, expected: string): boolean {
  const actualBuffer = Buffer.from(actual, "utf8");
  const expectedBuffer = Buffer.from(expected, "utf8");
  return (
    actualBuffer.length === expectedBuffer.length &&
    timingSafeEqual(actualBuffer, expectedBuffer)
  );
}

function accountKeyStrings(transaction: ParsedTransactionWithMeta): string[] {
  return transaction.transaction.message.accountKeys.map(({ pubkey }) =>
    pubkey.toBase58(),
  );
}

function creditedMicroUsdc(
  transaction: ParsedTransactionWithMeta,
): number {
  const keys = accountKeyStrings(transaction);
  const accountIndex = keys.indexOf(RECIPIENT_USDC_ACCOUNT.toBase58());
  if (accountIndex < 0) return 0;

  const matchingBalance = (
    transaction.meta?.postTokenBalances ?? []
  ).find(
    (balance) =>
      balance.accountIndex === accountIndex &&
      balance.mint === USDC_MINT.toBase58() &&
      balance.owner === RECIPIENT.toBase58(),
  );
  if (!matchingBalance) return 0;
  if (matchingBalance.uiTokenAmount.decimals !== 6) return 0;

  const before =
    (transaction.meta?.preTokenBalances ?? []).find(
      (balance) =>
        balance.accountIndex === accountIndex &&
        balance.mint === USDC_MINT.toBase58(),
    )?.uiTokenAmount.amount ?? "0";
  const after = matchingBalance.uiTokenAmount.amount;
  const delta = BigInt(after) - BigInt(before);
  if (delta < 0n || delta > BigInt(Number.MAX_SAFE_INTEGER)) return 0;
  return Number(delta);
}

async function findMatchingSignature(
  intent: IntentRow,
): Promise<string | null> {
  const reference = new PublicKey(intent.reference);
  const signatures = await connection().getSignaturesForAddress(reference, {
    limit: 20,
  }, "finalized");
  const earliestAllowed = Math.floor(intent.created_at.getTime() / 1000) - 30;

  for (const candidate of signatures) {
    if (
      candidate.err ||
      (candidate.blockTime != null && candidate.blockTime < earliestAllowed)
    ) {
      continue;
    }

    const transaction = await connection().getParsedTransaction(
      candidate.signature,
      {
        commitment: "finalized",
        maxSupportedTransactionVersion: 0,
      },
    );
    if (
      !transaction ||
      transaction.meta?.err ||
      !accountKeyStrings(transaction).includes(intent.reference)
    ) {
      continue;
    }
    if (
      creditedMicroUsdc(transaction) === Number(intent.amount_micro_usdc)
    ) {
      return candidate.signature;
    }
  }
  return null;
}

async function loadIntent(
  id: string,
  checkoutTokenHash: string,
): Promise<IntentRow | null> {
  const result = await pool.query<IntentRow>(
    `SELECT id, reference, tier, amount_micro_usdc, status, signature,
      checkout_token_hash, created_at, expires_at, verified_at,
      download_expires_at, download_claimed_at, downloaded_at
     FROM license_checkout_intents
     WHERE id = $1 AND checkout_token_hash = $2`,
    [id, checkoutTokenHash],
  );
  return result.rows[0] ?? null;
}

router.use("/licensing", (_req, res, next) => {
  res.setHeader("Cache-Control", "no-store, private");
  next();
});

router.post("/licensing/intents", createIntentLimiter, async (req, res) => {
  const tierId = req.body?.tier;
  if (typeof tierId !== "string" || !(tierId in TIERS)) {
    res.status(400).json({ ok: false, error: "Select a valid license tier." });
    return;
  }
  if (tierId === "webhook_rail_pro" && !archiveForTier(tierId)) {
    res.status(503).json({
      ok: false,
      error: "Webhook rail checkout is unavailable while its package cannot be delivered. No payment request was created.",
    });
    return;
  }

  const tier = TIERS[tierId as TierId];
  const id = randomUUID();
  const reference = Keypair.generate().publicKey.toBase58();
  const checkoutToken = randomBytes(32).toString("base64url");
  const expiresAt = new Date(Date.now() + INTENT_TTL_MS);

  try {
    await pool.query(
      `INSERT INTO license_checkout_intents
        (id, reference, tier, amount_micro_usdc, status, expires_at,
         checkout_token_hash)
       VALUES ($1, $2, $3, $4, 'pending', $5, $6)`,
      [
        id,
        reference,
        tierId,
        tier.amountMicroUsdc,
        expiresAt,
        hashToken(checkoutToken),
      ],
    );
    res.status(201).json({
      ok: true,
      intent: {
        id,
        checkoutToken,
        tier: tierId,
        tierName: tier.name,
        amountUsdc: tier.amountUsdc,
        recipient: RECIPIENT.toBase58(),
        splToken: USDC_MINT.toBase58(),
        reference,
        label: tierId === "webhook_rail_pro"
          ? "TrustLlama B2B Software"
          : "TrustLlama B2B Licensing",
        message: `${tier.name} — ${tier.amountUsdc} USDC`,
        expiresAt: expiresAt.toISOString(),
      },
    });
  } catch (error) {
    req.log.error({ error }, "License checkout intent creation failed");
    res.status(503).json({
      ok: false,
      error: "Checkout initialization is temporarily unavailable.",
    });
  }
});

router.get("/licensing/intents/:id/status", checkoutReadLimiter, async (req, res) => {
  try {
  const intentId = req.params.id;
    const checkoutToken = req.get("X-Tlama-License-Checkout");
    if (
      typeof intentId !== "string" ||
      !checkoutToken ||
      !/^[A-Za-z0-9_-]{43}$/.test(checkoutToken)
    ) {
      res.status(400).json({ ok: false, error: "Invalid checkout intent." });
      return;
    }
    let intent = await loadIntent(intentId, hashToken(checkoutToken));
    if (!intent) {
      res.status(404).json({ ok: false, error: "Checkout intent not found." });
      return;
    }

    if (intent.status === "pending" && intent.expires_at <= new Date()) {
      await pool.query(
        `UPDATE license_checkout_intents
         SET status = 'expired'
         WHERE id = $1 AND status = 'pending'`,
        [intent.id],
      );
      intent = { ...intent, status: "expired" };
    }

    if (intent.status === "pending") {
      const signature = await findMatchingSignature(intent);
      if (signature) {
        const downloadExpiresAt = new Date(Date.now() + DOWNLOAD_TTL_MS);
        const updated = await pool.query<IntentRow>(
          `UPDATE license_checkout_intents
           SET status = 'verified', signature = $2, verified_at = NOW(),
               download_expires_at = $3
           WHERE id = $1 AND status = 'pending'
             AND NOT EXISTS (
               SELECT 1 FROM license_checkout_intents WHERE signature = $2
             )
           RETURNING id, reference, tier, amount_micro_usdc, status, signature,
             checkout_token_hash, created_at, expires_at, verified_at,
             download_expires_at, download_claimed_at, downloaded_at`,
          [intent.id, signature, downloadExpiresAt],
        );
        if (updated.rowCount === 1) {
          const verifiedIntent = updated.rows[0];
          res.json(verifiedCheckoutStatus(verifiedIntent, (row) => downloadTokenFor(row as IntentRow)));
          return;
        }
      }
    }

    if (intent.status === "verified") {
      res.json(verifiedCheckoutStatus(intent, (row) => downloadTokenFor(row as IntentRow)));
      return;
    }

    res.json({ ok: true, status: intent.status });
  } catch (error) {
    req.log.error({ error }, "License settlement verification failed");
    res.status(503).json({
      ok: false,
      error: "Settlement verification is temporarily unavailable.",
    });
  }
});

router.get("/licensing/download/:id/:token", checkoutReadLimiter, async (req, res) => {
  const intentId = req.params.id;
  const token = req.params.token;
  if (
    typeof intentId !== "string" ||
    typeof token !== "string" ||
    !/^[A-Za-z0-9_-]{43}$/.test(token)
  ) {
    res.status(404).json({ ok: false, error: "Download grant not found." });
    return;
  }

  const grantResult = await pool.query<IntentRow>(
    `SELECT id, reference, tier, amount_micro_usdc, status, signature,
      checkout_token_hash, created_at, expires_at, verified_at,
      download_expires_at, download_claimed_at, downloaded_at
     FROM license_checkout_intents
      WHERE id = $1 AND status = 'verified'
          AND tier IN ('indie', 'startup', 'enterprise', 'webhook_rail_pro')`,
    [intentId],
  );
  const grant = grantResult.rows[0];
  if (
    !grant ||
    !grant.download_expires_at ||
    grant.download_expires_at <= new Date() ||
    !tokensMatch(token, downloadTokenFor(grant))
  ) {
    res.status(404).json({ ok: false, error: "Download grant not found." });
    return;
  }

  const archive = archiveForTier(grant.tier);
  if (!archive) {
    req.log.error({ tier: grant.tier, intentId }, "Checkout package is missing or ambiguous");
    res.status(503).json({
      ok: false,
      error: "The purchased package is temporarily unavailable. Please retry or contact support.",
    });
    return;
  }

  const claimed = await pool.query<{ id: string }>(
    `UPDATE license_checkout_intents
     SET download_claimed_at = NOW()
      WHERE id = $1 AND status = 'verified' AND tier = $2
       AND downloaded_at IS NULL
       AND download_expires_at > NOW()
       AND (
         download_claimed_at IS NULL OR
         download_claimed_at < NOW() - INTERVAL '5 minutes'
       )
     RETURNING id`,
    [intentId, grant.tier],
  );
  if (claimed.rowCount !== 1) {
    res.status(404).json({
      ok: false,
      error: "This download grant is invalid, expired, or already used.",
    });
    return;
  }

  res.setHeader("Cache-Control", "no-store, private");
  res.setHeader("X-Content-Type-Options", "nosniff");
  res.download(archive.path, archive.filename, async (error) => {
    if (error) {
      await pool.query(
        `UPDATE license_checkout_intents
         SET download_claimed_at = NULL
         WHERE id = $1 AND downloaded_at IS NULL`,
        [intentId],
      );
      req.log.error({ error, intentId }, "Licensed archive transfer failed");
      if (!res.headersSent) {
        res.status(500).json({
          ok: false,
          error: "The archive transfer failed. Please retry.",
        });
      }
      return;
    }
    await pool.query(
      `UPDATE license_checkout_intents
       SET downloaded_at = NOW(), download_claimed_at = NULL
       WHERE id = $1 AND downloaded_at IS NULL`,
      [intentId],
    );
  });
});

export default router;
