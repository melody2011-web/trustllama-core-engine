import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";
import path from "node:path";
import test from "node:test";

import {
  checkPublicGameCopy,
  checkPublicGameImages,
  findFinancialPromotionCopy,
  listPublicGameCopyFiles,
} from "./check-public-game-copy.mjs";

test("rejects each prohibited financial-promotion category", () => {
  const source = [
    "The token price is $0.05.",
    "Join the token launch in Q4.",
    "An investment with guaranteed returns.",
    "Liquidity pool rewards are live.",
    "Buy the token and trade now.",
  ].join("\n");

  assert.deepEqual(
    new Set(findFinancialPromotionCopy(source).map(({ category }) => category)),
    new Set([
      "token pricing or valuation",
      "token sale or launch timeline",
      "investment or returns language",
      "liquidity promotion",
      "token trading promotion",
    ]),
  );
});

test("rejects realistic ticker-based pricing, returns, and trading promotion", () => {
  const prohibitedExamples = [
    "TrustLlama GameFi",
    "TLAMA price is $0.05.",
    "TLAMA is priced at $0.05.",
    "Earn 20% returns.",
    "100x gains.",
    "Stake TLAMA for yield.",
    "Buy TLAMA now.",
    "Purchase TLAMA.",
    "TLAMA is listed on Binance.",
    "TLAMA launches in Q4.",
    "0.5% buy tax and 1% sell tax.",
    "Burn on every trade.",
    "Swap SOL for TLAMA.",
    "Earn TLAMA.",
    "Purchase with TLAMA.",
    "Join the public sale.",
    "Get TLAMA.",
    "Inspect wallet balances and request a live quote.",
    "Approve the final swap.",
  ];

  for (const copy of prohibitedExamples) {
    assert.notEqual(
      findFinancialPromotionCopy(copy).length,
      0,
      `Expected prohibited finding for: ${copy}`,
    );
  }
});

test("allows gameplay mechanics that share financial vocabulary", () => {
  const gameplayCopy = [
    "Insert Coin",
    "Defeat the Anti-Whale Warden boss.",
    "Open the vault after collecting 50 Orbs.",
    "Spend one match ticket to enter.",
    "Trade places with another player.",
  ].join("\n");

  assert.deepEqual(findFinancialPromotionCopy(gameplayCopy), []);
});

test("the current public presentation sources pass", async () => {
  assert.deepEqual(await checkPublicGameCopy(), []);
  assert.deepEqual(await checkPublicGameImages(), []);
});

test("covers every routed page and directly served text asset", async () => {
  const files = await listPublicGameCopyFiles();

  assert.ok(files.includes("artifacts/llama-website/src/pages/landing.tsx"));
  assert.ok(files.includes("artifacts/llama-website/src/pages/arcade.tsx"));
  assert.ok(files.includes("artifacts/llama-website/src/pages/memes.tsx"));
  assert.ok(files.includes("artifacts/llama-website/src/pages/not-found.tsx"));
  assert.ok(files.includes("artifacts/llama-website/index.html"));
  assert.ok(files.includes("artifacts/llama-website/public/whitepaper.html"));
  assert.ok(files.includes("artifacts/llama-website/public/whitepaper.md"));
  assert.ok(
    !files.includes(
      "artifacts/llama-website/src/components/web3-onboarding.tsx",
    ),
  );
});

test("fails when another public route introduces prohibited copy", async (t) => {
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), "public-copy-check-"));
  t.after(() => rm(temporaryRoot, { recursive: true, force: true }));

  const pages = path.join(
    temporaryRoot,
    "artifacts/llama-website/src/pages",
  );
  const publicAssets = path.join(
    temporaryRoot,
    "artifacts/llama-website/public",
  );
  await mkdir(pages, { recursive: true });
  await mkdir(publicAssets, { recursive: true });
  await writeFile(
    path.join(temporaryRoot, "artifacts/llama-website/index.html"),
    "<title>Arcade</title>",
  );
  await writeFile(
    path.join(pages, "arcade.tsx"),
    'export const copy = "Guaranteed returns for early investors";',
  );

  const findings = await checkPublicGameCopy(
    pathToFileURL(`${temporaryRoot}/`),
  );
  assert.equal(findings.length, 2);
  assert.ok(
    findings.every(
      ({ file }) => file === "artifacts/llama-website/src/pages/arcade.tsx",
    ),
  );
});

test("does not scan private funding documentation outside the public artifact", async (t) => {
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), "public-copy-check-"));
  t.after(() => rm(temporaryRoot, { recursive: true, force: true }));

  await mkdir(
    path.join(temporaryRoot, "artifacts/llama-website/src/pages"),
    { recursive: true },
  );
  await mkdir(path.join(temporaryRoot, "artifacts/llama-website/public"), {
    recursive: true,
  });
  await mkdir(path.join(temporaryRoot, "docs"), { recursive: true });
  await writeFile(
    path.join(temporaryRoot, "artifacts/llama-website/index.html"),
    "<title>Arcade</title>",
  );
  await writeFile(
    path.join(temporaryRoot, "docs/private-funding.md"),
    "Private investment and liquidity planning.",
  );

  assert.deepEqual(
    await checkPublicGameCopy(pathToFileURL(`${temporaryRoot}/`)),
    [],
  );
});

test("recursively scans nested presentation sources and public assets", async (t) => {
  const temporaryRoot = await mkdtemp(path.join(tmpdir(), "public-copy-check-"));
  t.after(() => rm(temporaryRoot, { recursive: true, force: true }));

  const nestedComponent = path.join(
    temporaryRoot,
    "artifacts/llama-website/src/features/promo/banner.ts",
  );
  const nestedAsset = path.join(
    temporaryRoot,
    "artifacts/llama-website/public/content/social/campaign.json",
  );
  await mkdir(path.dirname(nestedComponent), { recursive: true });
  await mkdir(path.dirname(nestedAsset), { recursive: true });
  await writeFile(
    path.join(temporaryRoot, "artifacts/llama-website/index.html"),
    "<title>Arcade</title>",
  );
  await writeFile(nestedComponent, 'export const banner = "Buy TLAMA now";');
  await writeFile(nestedAsset, '{"description":"Earn 20% returns"}');

  const findings = await checkPublicGameCopy(
    pathToFileURL(`${temporaryRoot}/`),
  );
  assert.deepEqual(
    new Set(findings.map(({ file }) => file)),
    new Set([
      "artifacts/llama-website/src/features/promo/banner.ts",
      "artifacts/llama-website/public/content/social/campaign.json",
    ]),
  );
});