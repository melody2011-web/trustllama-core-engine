import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtemp, rm } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { build } from "esbuild";

process.env.API_RATE_LIMIT_MAX = "2";
process.env.API_RATE_LIMIT_WINDOW_MS = "60000";
process.env.LOG_LEVEL = "silent";
process.env.NODE_ENV = "test";

let baseUrl;
let server;
let bundleDirectory;

async function request(pathname, clientIp, init = {}) {
  return fetch(`${baseUrl}${pathname}`, {
    ...init,
    headers: {
      "x-forwarded-for": clientIp,
      ...init.headers,
    },
  });
}

before(async () => {
  bundleDirectory = await mkdtemp(
    path.join(new URL(".", import.meta.url).pathname, ".security-bundle-"),
  );
  const outfile = path.join(bundleDirectory, "app.mjs");

  await build({
    entryPoints: {
      app: new URL("../src/app.ts", import.meta.url).pathname,
    },
    outdir: bundleDirectory,
    outExtension: { ".js": ".mjs" },
    platform: "node",
    bundle: true,
    format: "esm",
    external: ["*.node", "pino", "pino-http"],
    banner: {
      js: `import { createRequire as __createRequire } from "node:module";
globalThis.require = __createRequire(import.meta.url);`,
    },
  });

  const { default: app } = await import(pathToFileURL(outfile).href);
  server = app.listen(0, "127.0.0.1");
  await new Promise((resolve, reject) => {
    server.once("listening", resolve);
    server.once("error", reject);
  });
  const address = server.address();
  assert(address && typeof address === "object");
  baseUrl = `http://127.0.0.1:${address.port}`;
});

after(async () => {
  if (server) {
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
  if (bundleDirectory) await rm(bundleDirectory, { recursive: true, force: true });
});

test("applies critical Helmet headers and hides Express", async () => {
  const response = await request("/api/healthz", "198.51.100.1");

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-powered-by"), null);
  assert.equal(response.headers.get("x-frame-options"), "DENY");
  assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  assert.equal(response.headers.get("cross-origin-resource-policy"), "same-site");
  assert.match(
    response.headers.get("content-security-policy") ?? "",
    /default-src 'none'/,
  );
});

test("returns modern rate-limit headers and a JSON 429 response", async () => {
  const clientIp = "198.51.100.2";
  const first = await request("/api/not-found", clientIp);
  const second = await request("/api/not-found", clientIp);
  const limited = await request("/api/not-found", clientIp);

  assert.match(first.headers.get("ratelimit") ?? "", /^"2-in-1min"; r=1; t=\d+$/);
  assert.match(second.headers.get("ratelimit") ?? "", /^"2-in-1min"; r=0; t=\d+$/);
  assert.equal(first.headers.get("x-ratelimit-limit"), null);
  assert.equal(limited.status, 429);
  assert.match(limited.headers.get("content-type") ?? "", /^application\/json/);
  assert.deepEqual(await limited.json(), {
    ok: false,
    error: "Too many requests. Please try again later.",
  });
});

test("keeps health checks exempt from the request bucket", async () => {
  const clientIp = "198.51.100.3";
  for (const path of ["/api", "/api/healthz"]) {
    for (let index = 0; index < 4; index += 1) {
      assert.equal((await request(path, clientIp)).status, 200);
    }
  }

  assert.equal((await request("/api/not-found", clientIp)).status, 404);
  assert.equal((await request("/api/not-found", clientIp)).status, 404);
  assert.equal((await request("/api/not-found", clientIp)).status, 429);
});

test("keeps OPTIONS requests exempt from the request bucket", async () => {
  const clientIp = "198.51.100.4";
  for (let index = 0; index < 4; index += 1) {
    assert.notEqual(
      (await request("/api/not-found", clientIp, { method: "OPTIONS" })).status,
      429,
    );
  }

  assert.equal((await request("/api/not-found", clientIp)).status, 404);
  assert.equal((await request("/api/not-found", clientIp)).status, 404);
  assert.equal((await request("/api/not-found", clientIp)).status, 429);
});

test("uses separate buckets for separate client identities", async () => {
  const firstClient = "198.51.100.5";
  const secondClient = "198.51.100.6";

  await request("/api/not-found", firstClient);
  await request("/api/not-found", firstClient);
  assert.equal((await request("/api/not-found", firstClient)).status, 429);

  assert.equal((await request("/api/not-found", secondClient)).status, 404);
  assert.equal((await request("/api/not-found", secondClient)).status, 404);
  assert.equal((await request("/api/not-found", secondClient)).status, 429);
});