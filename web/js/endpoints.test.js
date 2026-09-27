import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { FALLBACK_CATALOG, priceRange } from "./endpoints.js";

// `paidEndpoints` decorates each entry with `expectedPayment`, which reads the
// browser globals through config.js. It degrades to defaults when they are
// absent, but pin them so this test never depends on that.
globalThis.window = { location: { hostname: "localhost", search: "" } };

function catalog(prices) {
  return {
    ...FALLBACK_CATALOG,
    endpoints: prices.map((price_usdc, index) => ({
      key: `k${index}`,
      path: `/v1/k${index}`,
      method: "GET",
      price_usdc,
      free: false,
    })),
  };
}

describe("catalog price range", () => {
  it("spans the cheapest and dearest paid endpoint", () => {
    assert.deepEqual(priceRange(catalog([0.35, 0.1, 0.5, 0.2])), { min: 0.1, max: 0.5 });
  });

  it("ignores free endpoints", () => {
    const doc = catalog([0.2, 0.5]);
    doc.endpoints.push({ key: "health", path: "/v1/health", method: "GET", price_usdc: 0, free: true });
    assert.deepEqual(priceRange(doc), { min: 0.2, max: 0.5 });
  });

  it("collapses to a single price when everything costs the same", () => {
    assert.deepEqual(priceRange(catalog([0.25, 0.25])), { min: 0.25, max: 0.25 });
  });

  it("falls back to the published catalog rather than inventing a range", () => {
    assert.deepEqual(priceRange(null), priceRange(FALLBACK_CATALOG));
  });

  it("returns null when nothing is priced", () => {
    assert.equal(priceRange({ ...FALLBACK_CATALOG, endpoints: [] }), null);
  });
});
