import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { validatePaymentQuote } from "./api.js";
import {
  CAIP_NETWORKS,
  PRODUCTION_API_BASE,
  PRODUCTION_PAYMENT,
  STORAGE_KEYS,
  apiUrl,
  caipNetwork,
  expectedPayment,
  getApiBase,
  isMockMode,
} from "./config.js";

function browser({ hostname, search = "", stored = {} }) {
  const values = new Map(Object.entries(stored));
  globalThis.window = {
    location: { hostname, search },
    localStorage: {
      getItem: (key) => (values.has(key) ? values.get(key) : null),
      removeItem: (key) => values.delete(key),
      setItem: (key, value) => values.set(key, value),
    },
  };
  return values;
}

afterEach(() => {
  delete globalThis.window;
});

describe("production API origin", () => {
  it("points the apex web host at the permanent API host", () => {
    browser({ hostname: "playclock.xyz" });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
    assert.equal(apiUrl("/v1/catalog"), "https://api.playclock.xyz/v1/catalog");
  });

  it("does the same for www", () => {
    browser({ hostname: "www.playclock.xyz" });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
  });

  it("keeps local and preview hosts same-origin", () => {
    browser({ hostname: "localhost" });
    assert.equal(getApiBase(), "");
    assert.equal(apiUrl("/v1/health"), "/v1/health");
  });

  it("ignores and clears an explicit developer override", () => {
    const values = browser({
      hostname: "playclock.xyz",
      search: "?api=http%3A%2F%2Flocalhost%3A8080%2F",
    });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
    assert.equal(values.has(STORAGE_KEYS.apiBase), false);
  });

  it("ignores and clears a stored override", () => {
    const values = browser({
      hostname: "playclock.xyz",
      stored: { [STORAGE_KEYS.apiBase]: "https://preview.example/" },
    });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
    assert.equal(values.has(STORAGE_KEYS.apiBase), false);
  });

  it("disables and clears mock payments", () => {
    const values = browser({
      hostname: "playclock.xyz",
      search: "?mock=1",
      stored: { [STORAGE_KEYS.mock]: "1" },
    });
    assert.equal(isMockMode(), false);
    assert.equal(values.has(STORAGE_KEYS.mock), false);
  });

  it("pins the production recipient, network, and USDC asset", () => {
    browser({ hostname: "playclock.xyz" });
    const payment = expectedPayment(
      { path: "/v1/player", price_usdc: 0.15 },
      { network: "testnet", asset_id: 1, pay_to: "ATTACKER" },
    );
    assert.equal(payment.payTo, PRODUCTION_PAYMENT.payTo);
    assert.equal(payment.network, PRODUCTION_PAYMENT.network);
    assert.equal(payment.asset, PRODUCTION_PAYMENT.asset);
    assert.equal(payment.amount, "150000");
  });

  it("migrates the old blank production override to the permanent API", () => {
    const values = browser({
      hostname: "playclock.xyz",
      stored: { [STORAGE_KEYS.apiBase]: "" },
    });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
    assert.equal(values.has(STORAGE_KEYS.apiBase), false);
  });

  it("treats an old production ?api= link as the permanent API", () => {
    const values = browser({ hostname: "www.playclock.xyz", search: "?api=" });
    assert.equal(getApiBase(), PRODUCTION_API_BASE);
    assert.equal(values.has(STORAGE_KEYS.apiBase), false);
  });

  it("preserves blank same-origin configuration off production", () => {
    browser({
      hostname: "localhost",
      stored: { [STORAGE_KEYS.apiBase]: "" },
    });
    assert.equal(getApiBase(), "");
  });
});

describe("development payment network", () => {
  const catalog = { network: "testnet", asset_id: 10458941, pay_to: "DEVMERCHANT" };
  const entry = { path: "/v1/trending", price_usdc: 0.1 };

  for (const hostname of ["localhost", "playclock-web-abc123-uk.a.run.app"]) {
    it(`expects the CAIP-2 id, not the catalog name, on ${hostname}`, () => {
      browser({ hostname });
      const payment = expectedPayment(entry, catalog);
      assert.equal(payment.network, CAIP_NETWORKS.testnet);

      // The 402 a dev backend actually sends: CAIP-2 in accepts[].network.
      const quote = {
        resource: { url: `http://${hostname}/v1/trending` },
        accepts: [
          {
            scheme: "exact",
            network: CAIP_NETWORKS.testnet,
            asset: "10458941",
            payTo: "DEVMERCHANT",
            amount: "100000",
          },
        ],
      };
      globalThis.window.location.origin = `http://${hostname}`;
      assert.doesNotThrow(() => validatePaymentQuote(quote, payment));
    });
  }

  it("maps mainnet too, and still refuses a quote on the other network", () => {
    browser({ hostname: "localhost" });
    const payment = expectedPayment(entry, { ...catalog, network: "mainnet" });
    assert.equal(payment.network, CAIP_NETWORKS.mainnet);
    assert.equal(payment.network, PRODUCTION_PAYMENT.network);

    const testnetQuote = {
      resource: { url: "http://localhost/v1/trending" },
      accepts: [
        {
          scheme: "exact",
          network: CAIP_NETWORKS.testnet,
          asset: "10458941",
          payTo: "DEVMERCHANT",
          amount: "100000",
        },
      ],
    };
    globalThis.window.location.origin = "http://localhost";
    assert.throws(() => validatePaymentQuote(testnetQuote, payment), /network/);
  });

  it("pins MainNet to the production recipient on any host (?api= cannot redirect it)", () => {
    browser({ hostname: "localhost", search: "?api=https%3A%2F%2Fevil.example" });
    const payment = expectedPayment(entry, {
      network: "mainnet",
      asset_id: 31566704,
      pay_to: "ATTACKER",
    });
    assert.equal(payment.payTo, PRODUCTION_PAYMENT.payTo);
    assert.equal(payment.asset, PRODUCTION_PAYMENT.asset);
    assert.equal(payment.network, PRODUCTION_PAYMENT.network);

    const attackerQuote = {
      resource: { url: "https://evil.example/v1/trending" },
      accepts: [
        {
          scheme: "exact",
          network: CAIP_NETWORKS.mainnet,
          asset: "31566704",
          payTo: "ATTACKER",
          amount: "100000",
        },
      ],
    };
    globalThis.window.location.origin = "http://localhost";
    assert.throws(() => validatePaymentQuote(attackerQuote, payment), /payTo|recipient/);
    // Even a caller that passes no expectation cannot sign a MainNet quote to
    // anyone but Play Clock.
    assert.throws(() => validatePaymentQuote(attackerQuote, null), /recipient/);
  });

  it("still lets TestNet follow the dev catalog", () => {
    browser({ hostname: "localhost" });
    const payment = expectedPayment(entry, catalog);
    assert.equal(payment.payTo, "DEVMERCHANT");
    assert.equal(payment.asset, "10458941");
  });

  it("passes CAIP-2 through and maps an unknown name to nothing", () => {
    assert.equal(caipNetwork(CAIP_NETWORKS.testnet), CAIP_NETWORKS.testnet);
    assert.equal(caipNetwork("TestNet"), CAIP_NETWORKS.testnet);
    assert.equal(caipNetwork("devnet"), "");
    assert.equal(caipNetwork(undefined), "");
  });
});
