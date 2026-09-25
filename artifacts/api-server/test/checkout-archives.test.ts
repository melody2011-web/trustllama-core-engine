import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { archiveForTier, webhookArchiveIn } from "../src/lib/checkout-archives";

test("webhook checkout selects exactly one nonempty archive", () => {
  const directory = mkdtempSync(join(tmpdir(), "webhook-rail-checkout-"));
  const fixture = "sample fixture";
  const fixtureHash = createHash("sha256").update(fixture).digest("hex");
  const filename = "solana-webhook-core-rail-pro.zip";
  const manifestPath = join(directory, "approved-release.json");
  const manifest = (sha256: string, name = filename) =>
    writeFileSync(manifestPath, JSON.stringify({ filename: name, sha256 }));
  try {
    assert.equal(webhookArchiveIn(directory), null);
    const packagePath = join(directory, filename);
    writeFileSync(packagePath, fixture);
    assert.equal(webhookArchiveIn(directory), null, "missing approval cannot be sold");
    manifest("0".repeat(64));
    assert.equal(webhookArchiveIn(directory), null, "a mismatched package cannot be sold");
    manifest(fixtureHash, "different.zip");
    assert.equal(webhookArchiveIn(directory), null, "the filename must also match");
    manifest(fixtureHash);
    assert.deepEqual(webhookArchiveIn(directory), {
      path: packagePath,
      filename,
    });
    writeFileSync(join(directory, "second.tar.gz"), "sample fixture");
    assert.equal(webhookArchiveIn(directory), null);
    rmSync(join(directory, "second.tar.gz"));
    symlinkSync(packagePath, join(directory, "linked.zip"));
    assert.deepEqual(webhookArchiveIn(directory), {
      path: packagePath,
      filename,
    });
    writeFileSync(packagePath, "");
    assert.equal(webhookArchiveIn(directory), null);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("unknown products never resolve to a licensing release", () => {
  assert.equal(archiveForTier("unexpected_product"), null);
});

test("checkout's tier selector refuses an unapproved Webhook Pro release", () => {
  const root = mkdtempSync(join(tmpdir(), "webhook-tier-checkout-"));
  const directory = join(root, "solana-webhook-core-rail-pro");
  const originalCwd = process.cwd();
  mkdirSync(directory);
  try {
    const bytes = "approved fixture";
    const filename = "solana-webhook-core-rail-pro.zip";
    writeFileSync(join(directory, filename), bytes);
    const manifestPath = join(directory, "approved-release.json");
    writeFileSync(manifestPath, JSON.stringify({ filename, sha256: "0".repeat(64) }));
    process.chdir(root);
    assert.equal(archiveForTier("webhook_rail_pro"), null);
    writeFileSync(manifestPath, JSON.stringify({
      filename,
      sha256: createHash("sha256").update(bytes).digest("hex"),
    }));
    assert.deepEqual(archiveForTier("webhook_rail_pro"), {
      path: join(directory, filename),
      filename,
    });
  } finally {
    process.chdir(originalCwd);
    rmSync(root, { recursive: true, force: true });
  }
});