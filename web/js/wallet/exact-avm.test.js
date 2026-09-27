/**
 * The payload builder, tested without a wallet, a browser or a network.
 *
 * Run with `npm test` — node's built-in runner, so no test framework in the
 * dependency tree. Everything here answers one question: does the group we
 * hand a wallet, and the payload we hand the facilitator, say exactly what the
 * 402 asked for? A bug in this file moves the wrong amount to the wrong address.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import algosdk from "algosdk";

import {
  UnsupportedQuote,
  algodFor,
  buildExactAvmPayload,
  buildPaymentGroup,
  explorerUrl,
  readQuote,
} from "./exact-avm.js";

const TESTNET = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=";
const MAINNET = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=";
const PAYER = "KAELWUHBMCXQZMEVIWHOGRDHIUJ27SBW4OCBIUHIAEC4CU5KTPSPJVGKTQ";
const MERCHANT = "BJXTJUHHMDDH36GEDNZA4MPXDQ6UMMOKACFN3DMF3TD3HZZKXJEGKXPUYI";
const FEE_PAYER = "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA";

/** A 0.10 USDC TestNet quote, exactly as the live API sends it. */
function quote(overrides = {}) {
  return {
    scheme: "exact",
    network: TESTNET,
    asset: "10458941",
    amount: "100000",
    payTo: MERCHANT,
    maxTimeoutSeconds: 120,
    extra: { name: "USDC", decimals: 6, tag: "x402-global-challenge" },
    ...overrides,
  };
}

function params(overrides = {}) {
  return {
    fee: 1000n,
    minFee: 1000n,
    firstValid: 1n,
    lastValid: 1001n,
    genesisID: "testnet-v1.0",
    genesisHash: algosdk.base64ToBytes("SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="),
    flatFee: false,
    ...overrides,
  };
}

describe("readQuote", () => {
  it("coerces the wire's string asset and amount", () => {
    const read = readQuote(quote());
    assert.equal(read.assetIndex, 10458941n);
    assert.equal(read.amount, 100000n);
    assert.equal(read.payTo, MERCHANT);
    assert.equal(read.feePayer, null);
  });

  it("keeps precision above Number.MAX_SAFE_INTEGER", () => {
    // This value is money. Number() would silently round it.
    const huge = "9007199254740993";
    assert.equal(readQuote(quote({ amount: huge })).amount, BigInt(huge));
  });

  it("refuses a scheme it cannot satisfy", () => {
    assert.throws(() => readQuote(quote({ scheme: "upto" })), UnsupportedQuote);
  });

  it("refuses a quote with no recipient", () => {
    assert.throws(() => readQuote(quote({ payTo: "" })), UnsupportedQuote);
  });

  it("refuses non-numeric or non-positive amounts", () => {
    assert.throws(() => readQuote(quote({ amount: "free" })), UnsupportedQuote);
    assert.throws(() => readQuote(quote({ amount: "0" })), UnsupportedQuote);
  });

  it("refuses a bare ALGO quote", () => {
    // asset 0 means microAlgos, and `amount` would mean something else entirely.
    assert.throws(() => readQuote(quote({ asset: "0" })), UnsupportedQuote);
  });

  it("reads a sponsored quote's fee payer", () => {
    const read = readQuote(quote({ extra: { decimals: 6, feePayer: FEE_PAYER } }));
    assert.equal(read.feePayer, FEE_PAYER);
  });
});

describe("algodFor", () => {
  it("maps both Algorand networks", () => {
    assert.ok(algodFor(TESTNET));
    assert.ok(algodFor(MAINNET));
  });

  it("refuses a chain it cannot pay on rather than signing against the wrong ledger", () => {
    assert.throws(() => algodFor("eip155:8453"), UnsupportedQuote);
    assert.throws(() => algodFor(""), UnsupportedQuote);
  });
});

describe("buildPaymentGroup — the path that runs in production", () => {
  it("is a single transaction, signed by us, with no group id", () => {
    const group = buildPaymentGroup({
      requirements: quote(),
      address: PAYER,
      suggestedParams: params(),
    });

    assert.equal(group.txns.length, 1);
    assert.equal(group.paymentIndex, 0);
    assert.deepEqual(group.indexesToSign, [0]);
    // A lone transaction needs no group id, which is what keeps SHA-512/256 —
    // a hash the Web Crypto API does not provide — out of this file entirely.
    assert.ok(!group.txns[0].group);
  });

  it("moves exactly what the 402 asked for, to exactly who it named", () => {
    const { txns } = buildPaymentGroup({
      requirements: quote(),
      address: PAYER,
      suggestedParams: params(),
    });
    const decoded = algosdk.decodeUnsignedTransaction(algosdk.encodeUnsignedTransaction(txns[0]));

    assert.equal(algosdk.encodeAddress(decoded.sender.publicKey), PAYER);
    assert.equal(algosdk.encodeAddress(decoded.assetTransfer.receiver.publicKey), MERCHANT);
    assert.equal(decoded.assetTransfer.assetIndex, 10458941n);
    assert.equal(decoded.assetTransfer.amount, 100000n);
  });

  it("never builds the same transaction twice for the same quote", () => {
    const build = () =>
      buildPaymentGroup({ requirements: quote(), address: PAYER, suggestedParams: params() })
        .txns[0];
    const [a, b] = [build(), build()];
    assert.notEqual(a.txID(), b.txID());
    assert.match(new TextDecoder().decode(a.note), /^x402-payment-/);
  });

  it("refuses to build without a connected wallet", () => {
    assert.throws(
      () => buildPaymentGroup({ requirements: quote(), address: "", suggestedParams: params() }),
      UnsupportedQuote,
    );
  });
});

describe("buildPaymentGroup — sponsored fees", () => {
  const sponsored = quote({ extra: { name: "USDC", decimals: 6, feePayer: FEE_PAYER } });

  it("puts the facilitator's unsigned slot first and ours second", () => {
    const group = buildPaymentGroup({
      requirements: sponsored,
      address: PAYER,
      suggestedParams: params(),
    });

    assert.equal(group.txns.length, 2);
    assert.equal(group.paymentIndex, 1);
    // We sign only our own transaction; slot 0 belongs to the fee payer.
    assert.deepEqual(group.indexesToSign, [1]);
  });

  it("assigns a group id and shifts the whole fee onto the sponsor", () => {
    const { txns } = buildPaymentGroup({
      requirements: sponsored,
      address: PAYER,
      suggestedParams: params(),
    });

    assert.ok(txns[0].group, "a multi-transaction group must be atomic");
    assert.deepEqual(txns[0].group, txns[1].group);
    assert.equal(txns[0].fee, 2000n, "sponsor carries double the minimum fee");
    assert.equal(txns[1].fee, 0n, "our transfer rides free");
  });
});

describe("buildExactAvmPayload", () => {
  const algod = { getTransactionParams: () => ({ do: async () => params() }) };

  it("returns base64 msgpack and the index of the payment", async () => {
    const payload = await buildExactAvmPayload({
      requirements: quote(),
      address: PAYER,
      algod,
      signTransactions: async (unsigned) => unsigned.map((bytes) => new Uint8Array([1, ...bytes])),
    });

    assert.equal(payload.paymentIndex, 0);
    assert.equal(payload.paymentGroup.length, 1);
    assert.doesNotThrow(() => algosdk.base64ToBytes(payload.paymentGroup[0]));
  });

  it("lets an unsigned sponsor slot travel unsigned", async () => {
    const sponsored = quote({ extra: { decimals: 6, feePayer: FEE_PAYER } });
    const payload = await buildExactAvmPayload({
      requirements: sponsored,
      address: PAYER,
      algod,
      // A wallet signs only what it was asked to; slot 0 comes back null.
      signTransactions: async (unsigned, indexes) =>
        unsigned.map((bytes, i) => (indexes.includes(i) ? new Uint8Array([1, ...bytes]) : null)),
    });

    assert.equal(payload.paymentGroup.length, 2);
    assert.equal(payload.paymentIndex, 1);
  });

  it("fails loudly when the wallet signed nothing", async () => {
    // Sending an unsigned payment would surface as a second 402 with no
    // explanation; better to say what actually happened.
    await assert.rejects(
      buildExactAvmPayload({
        requirements: quote(),
        address: PAYER,
        algod,
        signTransactions: async (unsigned) => unsigned.map(() => null),
      }),
      UnsupportedQuote,
    );
  });
});

describe("explorerUrl", () => {
  it("links a settled transaction on both networks", () => {
    assert.match(explorerUrl(TESTNET, "ABC"), /testnet\.explorer\.perawallet\.app\/tx\/ABC$/);
    assert.match(explorerUrl(MAINNET, "ABC"), /\/\/explorer\.perawallet\.app\/tx\/ABC$/);
  });

  it("is null when there is nothing to link", () => {
    assert.equal(explorerUrl(TESTNET, ""), null);
    assert.equal(explorerUrl("eip155:8453", "ABC"), null);
  });
});

describe("readQuote — the asset is not negotiable", () => {
  it("accepts the network's own USDC", () => {
    assert.equal(readQuote(quote()).assetIndex, 10458941n);
    assert.equal(
      readQuote(quote({ network: MAINNET, asset: "31566704" })).assetIndex,
      31566704n,
    );
  });

  it("refuses any other asset, however plausible", () => {
    // The wallet prompt shows an asset, not a promise. A tampered or
    // misconfigured 402 must not get someone to approve moving a different
    // token under a UI that says "pay 0.25 USDC".
    assert.throws(() => readQuote(quote({ asset: "31566704" })), UnsupportedQuote); // MainNet USDC on TestNet
    assert.throws(() => readQuote(quote({ asset: "999999999" })), UnsupportedQuote);
  });

  it("names both assets so the refusal is diagnosable", () => {
    assert.throws(
      () => readQuote(quote({ asset: "12345" })),
      /10458941[\s\S]*12345|12345[\s\S]*10458941/,
    );
  });

  it("refuses a chain it has no USDC id for", () => {
    assert.throws(() => readQuote(quote({ network: "eip155:8453" })), UnsupportedQuote);
  });
});
