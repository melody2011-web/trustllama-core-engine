import assert from "node:assert/strict";
import {
  connectionFailureStage,
  dispatchConnectionFailure,
  safeConnectionFailureDetail,
} from "../llama_website/llama_game/wallet-failure-stages.js";

assert.equal(
  connectionFailureStage({ name: "WalletConnectionError", error: { code: 4001 } }),
  "provider_rejected"
);
assert.equal(
  connectionFailureStage({
    name: "WalletConnectionError",
    error: { name: "WalletWindowClosedError" },
  }),
  "provider_rejected"
);
assert.equal(
  connectionFailureStage({ name: "WalletConnectionError", error: new Error("offline") }),
  "connection_failed"
);

assert.deepEqual(
  safeConnectionFailureDetail("untrusted adapter", "arbitrary stage"),
  { adapter: "unknown", stage: "connection_failed" }
);
assert.deepEqual(
  safeConnectionFailureDetail("phantom", "provider_rejected"),
  { adapter: "phantom", stage: "provider_rejected" }
);

const events = [];
class TestEvent {
  constructor(type, init) {
    this.type = type;
    this.detail = init.detail;
  }
}
dispatchConnectionFailure(
  { dispatchEvent: (event) => events.push(event) },
  "untrusted adapter",
  "arbitrary stage",
  TestEvent
);
assert.equal(events.length, 1);
assert.equal(events[0].type, "tlama-wallet-connection-failed");
assert.deepEqual(events[0].detail, {
  adapter: "unknown",
  stage: "connection_failed",
});