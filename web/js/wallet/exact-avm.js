/**
 * The AVM `exact` payment payload — the only money-critical code in the browser.
 *
 * Mirrors `x402/mechanisms/avm/exact/client.py` in the Python SDK, which is the
 * authority. Everything here is deliberately pure and injectable so it can be
 * tested in node against known-good vectors without a wallet, a browser or a
 * network: `buildPaymentGroup` takes suggested params rather than fetching
 * them, and `buildExactAvmPayload` takes the signing function rather than
 * owning a wallet.
 *
 * The shape it produces is `ExactAvmPayload.to_dict()`:
 *
 *     { paymentGroup: ["<base64 msgpack>", ...], paymentIndex: <int> }
 *
 * A note on the two group shapes. GoPlausible advertises a `feePayer` in
 * `GET /supported`, so it will sponsor fees — but it does not require us to use
 * that slot, and Play Clock's own 402 does not offer one (DESIGN_NOTES, TestNet
 * checklist item 1: "our 402's `extra` has no `feePayer`, the agent sent a
 * plain single-txn group, and GoPlausible accepted it"). So the single-txn path
 * is the one that runs in production and the one the tests pin hardest; the
 * sponsored path is implemented because the facilitator may start offering it,
 * and is marked as unvalidated until it is seen live.
 */

import algosdk from "algosdk";

/** CAIP-2 network id -> public algod endpoint. */
export const ALGOD_URLS = Object.freeze({
  "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=": "https://mainnet-api.algonode.cloud",
  "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=": "https://testnet-api.algonode.cloud",
});

/**
 * CAIP-2 network id -> the USDC ASA this page is willing to move.
 *
 * The only asset a Play Clock quote may name. Without this, a misconfigured or
 * tampered 402 could name any positive ASA and the wallet prompt would ask the
 * person to approve moving it — under a UI that says "pay 0.25 USDC". The
 * Python signer has always had this check (`USDC_ASA_IDS`, with an explicit
 * `allow_any_asset` escape hatch that defaults off); the browser needs it more,
 * because the person approving is not the person who read the quote.
 */
export const USDC_ASSETS = Object.freeze({
  "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=": 31566704n,
  "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=": 10458941n,
});

/** CAIP-2 network id -> block explorer, for linking a settled txid. */
export const EXPLORERS = Object.freeze({
  "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=": "https://explorer.perawallet.app/tx/",
  "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=":
    "https://testnet.explorer.perawallet.app/tx/",
});

/** Thrown when a 402 quotes something this client will not sign. */
export class UnsupportedQuote extends Error {
  constructor(message) {
    super(message);
    this.name = "UnsupportedQuote";
  }
}

/**
 * Algod client for the network a 402 quoted.
 *
 * @param {string} network CAIP-2 network id from `accepts[].network`.
 * @returns {algosdk.Algodv2}
 * @throws {UnsupportedQuote} for a network this client has no endpoint for —
 *   including any non-Algorand chain, which must fail loudly rather than be
 *   signed against the wrong ledger.
 */
export function algodFor(network) {
  const url = ALGOD_URLS[network];
  if (!url) {
    throw new UnsupportedQuote(
      `This page can only pay on Algorand MainNet or TestNet; the server quoted ${network || "no network"}.`,
    );
  }
  return new algosdk.Algodv2("", url, "");
}

/** Explorer URL for a settled transaction, or `null` on an unknown network. */
export function explorerUrl(network, txid) {
  const base = EXPLORERS[network];
  return base && txid ? `${base}${txid}` : null;
}

/**
 * Validate one `accepts[]` entry and coerce its wire types.
 *
 * The wire carries `asset` and `amount` as **strings** (x402 V2), and amounts
 * are atomic units. Coercing through `BigInt` rather than `Number` is not
 * pedantry: `Number` silently loses precision above 2^53, and this value is
 * money.
 *
 * @param {object} requirements one entry of `paymentRequired.accepts`
 * @returns {{assetIndex: bigint, amount: bigint, payTo: string, network: string, feePayer: string|null}}
 * @throws {UnsupportedQuote} when the quote is unpayable or malformed.
 */
export function readQuote(requirements = {}) {
  const scheme = requirements.scheme || "exact";
  if (scheme !== "exact") {
    throw new UnsupportedQuote(`This page can only pay the "exact" scheme; the server asked for "${scheme}".`);
  }
  const payTo = requirements.payTo || requirements.pay_to || "";
  if (!payTo) throw new UnsupportedQuote("The payment quote named no recipient.");

  let assetIndex;
  let amount;
  try {
    assetIndex = BigInt(requirements.asset);
    amount = BigInt(requirements.amount);
  } catch {
    throw new UnsupportedQuote(
      `The payment quote was not numeric (asset ${requirements.asset}, amount ${requirements.amount}).`,
    );
  }
  if (amount <= 0n) throw new UnsupportedQuote("The payment quote asked for a non-positive amount.");
  if (assetIndex <= 0n) {
    // amount would then be microAlgos, which this merchant never quotes.
    throw new UnsupportedQuote("This page only pays ASA quotes (USDC), not bare ALGO.");
  }

  const network = requirements.network || "";
  const expected = USDC_ASSETS[network];
  if (!expected) {
    throw new UnsupportedQuote(
      `This page can only pay on Algorand MainNet or TestNet; the server quoted ${network || "no network"}.`,
    );
  }
  if (assetIndex !== expected) {
    // The wallet prompt shows an asset, not a promise. Refusing here is the
    // only place this can be caught before someone approves it.
    throw new UnsupportedQuote(
      `This page only pays USDC (asset ${expected}), but the server asked for asset ${assetIndex}. Nothing was signed.`,
    );
  }

  const extra = requirements.extra || {};
  return { assetIndex, amount, payTo, network, feePayer: extra.feePayer || null };
}

/**
 * Build the transaction group for one quote.
 *
 * @param {object} args
 * @param {object} args.requirements one `accepts[]` entry
 * @param {string} args.address the paying wallet address
 * @param {object} args.suggestedParams from `algod.getTransactionParams().do()`
 * @returns {{txns: object[], paymentIndex: number, indexesToSign: number[]}}
 */
export function buildPaymentGroup({ requirements, address, suggestedParams }) {
  const quote = readQuote(requirements);
  if (!address) throw new UnsupportedQuote("No wallet is connected.");

  // A unique note, as the Python SDK writes: without one, two same-priced
  // purchases inside one round's suggested params are the same transaction,
  // and the server (which keys payments on the txid) 402s the second as a
  // replay of the first.
  const note = new TextEncoder().encode(
    `x402-payment-${Date.now()}-${globalThis.crypto.randomUUID()}`,
  );
  const transfer = (params) =>
    algosdk.makeAssetTransferTxnWithSuggestedParamsFromObject({
      sender: address,
      receiver: quote.payTo,
      amount: quote.amount,
      assetIndex: quote.assetIndex,
      suggestedParams: params,
      note,
    });

  if (!quote.feePayer) {
    // The path that actually runs: one transaction, signed by us, no group id.
    // Worth stating because it is why this file needs no SHA-512/256 — a hash
    // the Web Crypto API does not provide — and therefore why it can be honest
    // browser code rather than a vendored hashing library.
    return { txns: [transfer(suggestedParams)], paymentIndex: 0, indexesToSign: [0] };
  }

  // Sponsored path: the facilitator pays the fees, so slot 0 is its own
  // zero-amount self-payment carrying double the minimum fee, and our transfer
  // rides at zero. UNVALIDATED against a live facilitator — Play Clock's 402
  // has never offered a feePayer.
  const minFee = BigInt(suggestedParams.minFee ?? 1000);
  const sponsor = algosdk.makePaymentTxnWithSuggestedParamsFromObject({
    sender: quote.feePayer,
    receiver: quote.feePayer,
    amount: 0n,
    suggestedParams: { ...suggestedParams, flatFee: true, fee: minFee * 2n },
  });
  const paid = transfer({ ...suggestedParams, flatFee: true, fee: 0n });
  algosdk.assignGroupID([sponsor, paid]);
  return { txns: [sponsor, paid], paymentIndex: 1, indexesToSign: [1] };
}

/**
 * Build the `{paymentGroup, paymentIndex}` payload for one quote.
 *
 * @param {object} args
 * @param {object} args.requirements one `accepts[]` entry
 * @param {string} args.address the paying wallet address
 * @param {object} args.algod an `algosdk.Algodv2`
 * @param {(unsigned: Uint8Array[], indexesToSign: number[]) => Promise<(Uint8Array|null)[]>} args.signTransactions
 *   Signs the requested indexes and returns a slot-aligned array; slots it did
 *   not sign come back `null` and travel unsigned.
 * @returns {Promise<{paymentGroup: string[], paymentIndex: number}>}
 */
export async function buildExactAvmPayload({ requirements, address, algod, signTransactions }) {
  const suggestedParams = await algod.getTransactionParams().do();
  const { txns, paymentIndex, indexesToSign } = buildPaymentGroup({
    requirements,
    address,
    suggestedParams,
  });

  const unsigned = txns.map((txn) => algosdk.encodeUnsignedTransaction(txn));
  const signed = await signTransactions(unsigned, indexesToSign);

  const paymentGroup = unsigned.map((bytes, index) => {
    const blob = signed && signed[index];
    return algosdk.bytesToBase64(blob || bytes);
  });
  if (indexesToSign.some((index) => !(signed && signed[index]))) {
    throw new UnsupportedQuote("The wallet returned no signature for the payment transaction.");
  }
  return { paymentGroup, paymentIndex };
}
