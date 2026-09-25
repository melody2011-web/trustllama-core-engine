import assert from "node:assert/strict";
import { test } from "node:test";
import { verifiedCheckoutStatus } from "../src/lib/checkout-status";

test("verified webhook payment receives its own download grant", () => {
  const intent = {
    id: "existing-intent",
    tier: "webhook_rail_pro",
    signature: "finalized-signature",
    download_expires_at: new Date(Date.now() + 60_000),
    downloaded_at: null,
  };
  const result = verifiedCheckoutStatus(intent, () => "webhook-token");
  assert.equal(result.status, "verified");
  assert.equal(result.signature, "finalized-signature");
  assert.equal(result.downloadUrl, "/api/licensing/download/existing-intent/webhook-token");
});

test("previous webhook settlements without a grant do not invent one", () => {
  const legacy = verifiedCheckoutStatus({
    id: "previous-intent",
    tier: "webhook_rail_pro",
    signature: "finalized-signature",
    download_expires_at: null,
    downloaded_at: null,
  }, () => { throw new Error("No grant should be generated without a deadline"); });
  assert.equal(legacy.downloadUrl, null);
  assert.match(legacy.message, /Contact support/);
});

test("licensed products still receive their own active grants", () => {
  const result = verifiedCheckoutStatus({
    id: "licensed-intent",
    tier: "indie",
    signature: "license-signature",
    download_expires_at: new Date(Date.now() + 60_000),
    downloaded_at: null,
  }, () => "licensed-token");
  assert.equal(result.downloadUrl, "/api/licensing/download/licensed-intent/licensed-token");
});