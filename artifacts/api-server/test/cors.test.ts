import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import express from "express";
import {
  apiCors,
  allowedCorsOrigins,
  corsErrorHandler,
} from "../src/middleware/security.ts";

const savedEnvironment = {
  NODE_ENV: process.env.NODE_ENV,
  REPLIT_DOMAINS: process.env.REPLIT_DOMAINS,
  REPLIT_DEV_DOMAIN: process.env.REPLIT_DEV_DOMAIN,
  CORS_ALLOWED_ORIGINS: process.env.CORS_ALLOWED_ORIGINS,
};

const app = express();
app.use((req, _res, next) => {
  req.log = {
    warn() {},
  } as typeof req.log;
  next();
});
app.use(apiCors);
app.get("/api/example", (_req, res) => res.json({ ok: true }));
app.use(corsErrorHandler);

let server: ReturnType<typeof app.listen>;
let baseUrl: string;

before(() => {
  server = app.listen(0);
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("Test server did not bind.");
  baseUrl = `http://127.0.0.1:${address.port}`;
});

beforeEach(() => {
  process.env.NODE_ENV = "production";
  process.env.REPLIT_DOMAINS = "trustllama.example";
  delete process.env.REPLIT_DEV_DOMAIN;
  delete process.env.CORS_ALLOWED_ORIGINS;
});

after(async () => {
  await new Promise<void>((resolve, reject) =>
    server.close((error) => (error ? reject(error) : resolve())),
  );
  for (const [name, value] of Object.entries(savedEnvironment)) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

test("production allows the published site and an explicit approved origin", () => {
  process.env.CORS_ALLOWED_ORIGINS = "https://partner.example";
  assert.deepEqual([...allowedCorsOrigins()].sort(), [
    "https://partner.example",
    "https://trustllama.example",
  ]);
});

test("production does not allow development origins", () => {
  process.env.REPLIT_DEV_DOMAIN = "temporary.replit.dev";
  const origins = allowedCorsOrigins();
  assert.equal(origins.has("https://temporary.replit.dev"), false);
  assert.equal(origins.has("http://localhost:5173"), false);
});

test("approved origin receives CORS headers", async () => {
  const response = await fetch(`${baseUrl}/api/example`, {
    headers: { Origin: "https://trustllama.example" },
  });
  assert.equal(response.status, 200);
  assert.equal(
    response.headers.get("access-control-allow-origin"),
    "https://trustllama.example",
  );
});

test("Arcade ownership headers pass preflight", async () => {
  const response = await fetch(`${baseUrl}/api/example`, {
    method: "OPTIONS",
    headers: {
      Origin: "https://trustllama.example",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers":
        "X-Tlama-Player-Key,X-Tlama-Ownership-Token",
    },
  });
  assert.equal(response.status, 204);
  assert.match(
    response.headers.get("access-control-allow-headers") ?? "",
    /X-Tlama-Ownership-Token/,
  );
});

test("rejected origin receives a clear 403 response", async () => {
  const response = await fetch(`${baseUrl}/api/example`, {
    headers: { Origin: "https://unapproved.example" },
  });
  assert.equal(response.status, 403);
  assert.equal(response.headers.get("access-control-allow-origin"), null);
  assert.deepEqual(await response.json(), {
    ok: false,
    error: "This website is not allowed to access the TrustLlama API.",
  });
});