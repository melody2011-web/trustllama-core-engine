import assert from "node:assert/strict";
import test from "node:test";
import type {Connection, SignatureStatus} from "@solana/web3.js";
import {fundingDeficit, reconcilePending, type PendingTransaction} from "./solana-devnet-rehearsal.js";

const signature = "1".repeat(64);
const pending: PendingTransaction = {
  signature,
  lastValidBlockHeight: 100,
  wireBase64: Buffer.from("public signed transaction").toString("base64"),
  rejected: false,
};

function fakeConnection(heights: number[], statuses: Array<SignatureStatus | null>) {
  let sends = 0;
  const connection = {
    getBlockHeight: async () => heights.shift() ?? 101,
    getSignatureStatuses: async () => ({
      context: {apiVersion: "test", slot: 1},
      value: [statuses.shift() ?? null],
    }),
    sendRawTransaction: async () => {
      sends += 1;
      return signature;
    },
  } as unknown as Pick<Connection, "getBlockHeight" | "getSignatureStatuses" | "sendRawTransaction">;
  return {connection, sends: () => sends};
}

test("restart returns an already-finalized checkpoint without rebroadcast", async () => {
  const {connection, sends} = fakeConnection([99], [{
    slot: 7, confirmations: null, err: null, confirmationStatus: "finalized",
  }]);
  const result = await reconcilePending(connection, pending, true);
  assert.equal(result?.slot, 7);
  assert.equal(sends(), 0);
});

test("restart rebroadcasts a live unconfirmed checkpoint once", async () => {
  const {connection, sends} = fakeConnection([99, 100], [null, {
    slot: 8, confirmations: null, err: null, confirmationStatus: "finalized",
  }]);
  const result = await reconcilePending(connection, pending, true);
  assert.equal(result?.slot, 8);
  assert.equal(sends(), 1);
});

test("restart regenerates an expired checkpoint absent from history", async () => {
  const {connection, sends} = fakeConnection([101], [null]);
  const result = await reconcilePending(connection, pending, true);
  assert.equal(result, null);
  assert.equal(sends(), 0);
});

test("user funding only covers a verified deficit", () => {
  assert.equal(fundingDeficit(0n), 500_000_000n);
  assert.equal(fundingDeficit(200_000_000n), 300_000_000n);
  assert.equal(fundingDeficit(500_000_000n), 0n);
  assert.equal(fundingDeficit(600_000_000n), 0n);
});