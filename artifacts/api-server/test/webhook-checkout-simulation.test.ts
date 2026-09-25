import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { after, test } from "node:test";
import { getAssociatedTokenAddressSync } from "@solana/spl-token";
import { Connection, PublicKey } from "@solana/web3.js";
import { pool } from "@workspace/db";
import express from "express";
import licensingRouter from "../src/routes/licensing";

const recipient = new PublicKey("sGLpe2FLbNfCcBt775jq9TWGVpeqoYfNephHtfYaXAH");
const mint = new PublicKey("EPjFW3gd5T71f72FcFc71U57UR3L6m93pH584CD6iz7b");
const recipientAccount = getAssociatedTokenAddressSync(mint, recipient);
const archive = resolve(process.cwd(), "../../solana-webhook-core-rail-pro/solana-webhook-core-rail-pro.zip");
const simulatedSignature = "simulation-finalized-450-usdc";
const originalQuery = pool.query;
const originalSignatures = Connection.prototype.getSignaturesForAddress;
const originalTransaction = Connection.prototype.getParsedTransaction;

after(() => {
  pool.query = originalQuery;
  Connection.prototype.getSignaturesForAddress = originalSignatures;
  Connection.prototype.getParsedTransaction = originalTransaction;
});

test("simulated finalized $450 Webhook Pro checkout grants and transfers the exact ZIP", async () => {
  let intent: Record<string, any> | undefined;
  const trace: string[] = [];
  const record = (message: string) => trace.push(`SIM ${trace.length + 1}: ${message}`);

  // This test process replaces all SQL and Solana RPC calls; no live ledger or
  // development/production database receives a simulated settlement.
  pool.query = (async (sql: string, params: any[] = []) => {
    if (sql.includes("INSERT INTO license_checkout_intents")) {
      assert.equal(params[2], "webhook_rail_pro");
      assert.equal(params[3], 450_000_000);
      intent = {
        id: params[0], reference: params[1], tier: params[2],
        amount_micro_usdc: String(params[3]), status: "pending",
        expires_at: params[4], checkout_token_hash: params[5],
        created_at: new Date(), signature: null, verified_at: null,
        download_expires_at: null, download_claimed_at: null, downloaded_at: null,
      };
      return { rows: [], rowCount: 1 };
    }
    if (sql.includes("WHERE id = $1 AND checkout_token_hash = $2")) {
      return { rows: intent?.id === params[0] && intent.checkout_token_hash === params[1] ? [intent] : [], rowCount: 1 };
    }
    if (sql.includes("SET status = 'verified'")) {
      assert.equal(intent?.status, "pending");
      intent.status = "verified";
      intent.signature = params[1];
      intent.verified_at = new Date();
      intent.download_expires_at = params[2];
      return { rows: [intent], rowCount: 1 };
    }
    if (sql.includes("AND status = 'verified'") && sql.includes("tier IN")) {
      return { rows: intent?.id === params[0] && intent.status === "verified" ? [intent] : [], rowCount: 1 };
    }
    if (sql.includes("SET download_claimed_at = NOW()")) {
      if (intent?.id !== params[0] || intent.tier !== params[1] || intent.downloaded_at) {
        return { rows: [], rowCount: 0 };
      }
      intent.download_claimed_at = new Date();
      return { rows: [{ id: intent.id }], rowCount: 1 };
    }
    if (sql.includes("SET downloaded_at = NOW()")) {
      intent!.downloaded_at = new Date();
      intent!.download_claimed_at = null;
      return { rows: [], rowCount: 1 };
    }
    throw new Error(`Unexpected simulation SQL: ${sql.slice(0, 100)}`);
  }) as typeof pool.query;

  Connection.prototype.getSignaturesForAddress = (async (reference: PublicKey) => {
    assert.equal(reference.toBase58(), intent?.reference);
    return [{ signature: simulatedSignature, err: null, blockTime: Math.floor(Date.now() / 1000) }];
  }) as typeof Connection.prototype.getSignaturesForAddress;
  Connection.prototype.getParsedTransaction = (async (signature: string) => {
    assert.equal(signature, simulatedSignature);
    return {
      transaction: { message: { accountKeys: [
        { pubkey: new PublicKey(intent!.reference) },
        { pubkey: recipientAccount },
      ] } },
      meta: {
        err: null,
        preTokenBalances: [{ accountIndex: 1, mint: mint.toBase58(), uiTokenAmount: { amount: "0", decimals: 6 } }],
        postTokenBalances: [{
          accountIndex: 1, mint: mint.toBase58(), owner: recipient.toBase58(),
          uiTokenAmount: { amount: "450000000", decimals: 6 },
        }],
      },
    };
  }) as typeof Connection.prototype.getParsedTransaction;

  const app = express();
  app.use(express.json());
  app.use((req, _res, next) => {
    (req as any).log = { error: (error: unknown) => { throw new Error(`Route error: ${String(error)}`); } };
    next();
  });
  app.use("/api", licensingRouter);
  const server = createServer(app);
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  try {
    const address = server.address();
    assert(address && typeof address !== "string");
    const base = `http://127.0.0.1:${address.port}`;
    const createResponse = await fetch(`${base}/api/licensing/intents`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tier: "webhook_rail_pro" }),
    });
    const created = await createResponse.json();
    assert.equal(createResponse.status, 201);
    assert.equal(created.intent.amountUsdc, "450");
    assert.equal(created.intent.tier, "webhook_rail_pro");
    record("POST intent -> HTTP 201; webhook_rail_pro; 450 USDC; pending");

    const statusResponse = await fetch(`${base}/api/licensing/intents/${created.intent.id}/status`, {
      headers: { "X-Tlama-License-Checkout": created.intent.checkoutToken },
    });
    const status = await statusResponse.json();
    assert.equal(statusResponse.status, 200);
    assert.equal(status.status, "verified");
    assert.equal(status.signature, simulatedSignature);
    assert.match(status.downloadUrl, /^\/api\/licensing\/download\//);
    assert(new Date(status.downloadExpiresAt) > new Date());
    record("Simulated finalized exact-amount USDC credit -> HTTP 200; intent verified; one-hour product grant issued");

    const downloadResponse = await fetch(`${base}${status.downloadUrl}`);
    const bytes = Buffer.from(await downloadResponse.arrayBuffer());
    assert.equal(downloadResponse.status, 200);
    assert.match(downloadResponse.headers.get("content-disposition") ?? "", /solana-webhook-core-rail-pro\.zip/);
    const digest = (data: Buffer) => createHash("sha256").update(data).digest("hex");
    assert.equal(digest(bytes), digest(readFileSync(archive)));
    record(`GET grant -> HTTP 200; ${bytes.length} ZIP bytes; SHA-256 ${digest(bytes)} matches approved package`);

    for (let i = 0; i < 20 && !intent?.downloaded_at; i++) {
      await new Promise((done) => setTimeout(done, 10));
    }
    assert(intent?.downloaded_at);
    const replayResponse = await fetch(`${base}${status.downloadUrl}`);
    assert.equal(replayResponse.status, 404);
    record("Second use of consumed grant -> HTTP 404; ZIP not delivered again");
  } finally {
    server.closeAllConnections();
    await new Promise<void>((done) => server.close(done));
    for (const line of trace) console.log(line);
  }
});