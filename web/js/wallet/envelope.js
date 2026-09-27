/**
 * The V2 `PaymentPayload` envelope and its base64 header encoding.
 *
 * Deliberately dependency-free: no algosdk, no wallet SDK, no browser globals.
 * That means this file is served to the browser as-is (it never enters the
 * bundle) *and* runs under `node --test`, which matters because two of the
 * fields it copies are easy to drop and expensive to lose.
 *
 * `accepted` must echo the chosen `accepts[]` entry **unmodified** — the
 * facilitator matches it against its own quote, and a re-serialised or
 * "tidied" copy fails verification.
 *
 * `extensions` must be echoed too, and this is the one nobody guesses: the
 * Bazaar discovery block travels in-band on the payment, so dropping it means
 * the payment settles and the endpoint is never catalogued
 * (DESIGN_NOTES, "Bazaar listing is implicit"). A listing that never appears is
 * attribution that does not count.
 */

/** x402 protocol version this client speaks. */
export const X402_VERSION = 2;

/**
 * Base64 of a byte array, in the browser or in node.
 *
 * @param {Uint8Array} bytes
 * @returns {string} standard padded base64
 */
export function bytesToBase64(bytes) {
  if (typeof btoa === "function") {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return btoa(binary);
  }
  // node, for the tests
  return Buffer.from(bytes).toString("base64");
}

/**
 * Assemble the `PAYMENT-SIGNATURE` header value for one settled quote.
 *
 * @param {object} args
 * @param {object} args.payload the scheme payload, e.g. `{paymentGroup, paymentIndex}`
 * @param {object} args.requirements the `accepts[]` entry being satisfied
 * @param {object} [args.paymentRequired] the whole 402 body, for `resource`/`extensions`
 * @returns {string} base64 JSON, ready for the header
 */
export function buildPaymentHeader({ payload, requirements, paymentRequired = {} }) {
  if (!payload) throw new Error("Cannot build a payment header without a payload.");
  if (!requirements) throw new Error("Cannot build a payment header without requirements.");

  const envelope = {
    x402Version: X402_VERSION,
    payload,
    accepted: requirements,
  };
  // Both are optional on the wire and both are load-bearing in practice, so
  // they are copied when present and never invented when absent.
  if (paymentRequired.resource) envelope.resource = paymentRequired.resource;
  if (paymentRequired.extensions) envelope.extensions = paymentRequired.extensions;

  return bytesToBase64(new TextEncoder().encode(JSON.stringify(envelope)));
}

/**
 * Decode a header back to its envelope. Used by the tests and by dev tooling.
 *
 * @param {string} header base64 JSON
 * @returns {object}
 */
export function decodePaymentHeader(header) {
  const binary =
    typeof atob === "function"
      ? Uint8Array.from(atob(header), (c) => c.charCodeAt(0))
      : Buffer.from(header, "base64");
  return JSON.parse(new TextDecoder().decode(binary));
}
