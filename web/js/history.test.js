import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { readReceiptHistory, rememberReceipt, STORAGE_KEY } from "./history.js";

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
  };
}

describe("local receipt history", () => {
  it("stores newest first", () => {
    const storage = memoryStorage();
    rememberReceipt({ transaction: "TX1", endpoint: "player", price: "$0.15" }, storage);
    rememberReceipt({ transaction: "TX2", endpoint: "matchup", price: "$0.25" }, storage);
    assert.deepEqual(readReceiptHistory(storage).map((item) => item.transaction), ["TX2", "TX1"]);
  });

  it("drops the analysis body a caller passes alongside the metadata", () => {
    const storage = memoryStorage();
    rememberReceipt(
      {
        transaction: "TX1",
        payer: "PAYER",
        network: "algorand:mainnet",
        endpoint: "player",
        title: "Player deep dive",
        price: "$0.10",
        settledAt: "2026-09-04T00:00:00.000Z",
        verdict: "Start him — the target share is real.",
        reasoning: "Four straight weeks above a 25% target share.",
      },
      storage,
    );

    const raw = storage.getItem(STORAGE_KEY);
    assert.equal(raw.includes("verdict"), false);
    assert.equal(raw.includes("reasoning"), false);
    assert.equal(raw.includes("target share"), false);
    assert.deepEqual(Object.keys(readReceiptHistory(storage)[0]).sort(), [
      "endpoint",
      "network",
      "payer",
      "price",
      "settledAt",
      "title",
      "transaction",
    ]);
  });

  it("deduplicates a replayed settlement", () => {
    const storage = memoryStorage();
    rememberReceipt({ transaction: "TX1", endpoint: "player" }, storage);
    rememberReceipt({ transaction: "TX1", endpoint: "player" }, storage);
    assert.equal(readReceiptHistory(storage).length, 1);
  });

  it("degrades safely when stored JSON is corrupt", () => {
    const storage = memoryStorage({ [STORAGE_KEY]: "not-json" });
    assert.deepEqual(readReceiptHistory(storage), []);
  });
});
