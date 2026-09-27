/** The envelope, and the two fields that are expensive to drop. */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { X402_VERSION, buildPaymentHeader, decodePaymentHeader } from "./envelope.js";

const requirements = {
  scheme: "exact",
  network: "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
  asset: "10458941",
  amount: "100000",
  payTo: "BJXTJUHHMDDH36GEDNZA4MPXDQ6UMMOKACFN3DMF3TD3HZZKXJEGKXPUYI",
  maxTimeoutSeconds: 120,
  extra: { name: "USDC", decimals: 6, tag: "x402-global-challenge" },
};

const paymentRequired = {
  x402Version: 2,
  error: "payment_required",
  resource: { url: "https://api.example/v1/trending", mimeType: "application/json" },
  accepts: [requirements],
  extensions: { bazaar: { info: { input: { type: "http" } } } },
};

const payload = { paymentGroup: ["c2lnbmVk"], paymentIndex: 0 };

describe("buildPaymentHeader", () => {
  it("round trips through base64", () => {
    const decoded = decodePaymentHeader(buildPaymentHeader({ payload, requirements, paymentRequired }));
    assert.equal(decoded.x402Version, X402_VERSION);
    assert.deepEqual(decoded.payload, payload);
  });

  it("echoes the accepted quote unmodified", () => {
    // The facilitator matches this against its own quote; a tidied copy fails.
    const decoded = decodePaymentHeader(buildPaymentHeader({ payload, requirements, paymentRequired }));
    assert.deepEqual(decoded.accepted, requirements);
    assert.equal(decoded.accepted.extra.tag, "x402-global-challenge");
  });

  it("carries the Bazaar discovery block", () => {
    // Drop this and the payment settles while the endpoint is never catalogued.
    const decoded = decodePaymentHeader(buildPaymentHeader({ payload, requirements, paymentRequired }));
    assert.deepEqual(decoded.extensions, paymentRequired.extensions);
    assert.deepEqual(decoded.resource, paymentRequired.resource);
  });

  it("omits resource and extensions rather than inventing them", () => {
    const decoded = decodePaymentHeader(buildPaymentHeader({ payload, requirements }));
    assert.ok(!("extensions" in decoded));
    assert.ok(!("resource" in decoded));
  });

  it("survives non-ASCII in the quote", () => {
    const accented = { ...requirements, extra: { ...requirements.extra, name: "USDC — Algorand" } };
    const decoded = decodePaymentHeader(
      buildPaymentHeader({ payload, requirements: accented, paymentRequired }),
    );
    assert.equal(decoded.accepted.extra.name, "USDC — Algorand");
  });

  it("emits standard padded base64", () => {
    const header = buildPaymentHeader({ payload, requirements, paymentRequired });
    assert.match(header, /^[A-Za-z0-9+/]+={0,2}$/);
  });

  it("refuses to build half an envelope", () => {
    assert.throws(() => buildPaymentHeader({ payload: null, requirements }), /payload/);
    assert.throws(() => buildPaymentHeader({ payload, requirements: null }), /requirements/);
  });
});
