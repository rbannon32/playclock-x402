import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { WalletPaymentProvider, formatUsdcAtomic } from "./payment.js";

describe("USDC display", () => {
  it("formats atomic units with BigInt and six fixed decimals", () => {
    assert.equal(formatUsdcAtomic("100000"), "0.10 USDC");
    assert.equal(formatUsdcAtomic("350000"), "0.35 USDC");
    assert.equal(formatUsdcAtomic("125000"), "0.125 USDC");
    assert.equal(formatUsdcAtomic(1n), "0.000001 USDC");
    // Above 2^53 a Number would silently round; BigInt does not.
    assert.equal(formatUsdcAtomic("9007199254740993"), "9007199254.740993 USDC");
    assert.equal(formatUsdcAtomic("free"), null);
    assert.equal(formatUsdcAtomic("-1"), null);
  });

  it("ignores the server's extra.decimals and extra.name", () => {
    const provider = new WalletPaymentProvider({
      amount: "250000",
      extra: { decimals: 2, name: "FREE MONEY" },
    });
    assert.equal(provider.displayAmount, "0.25 USDC");
  });
});
