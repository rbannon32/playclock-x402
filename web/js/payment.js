/**
 * THE PAYMENT SEAM.
 *
 * `api.js` knows how to get a 402 and how to retry with a `PAYMENT-SIGNATURE`
 * header. It does not know how that header value is produced. Everything about
 * signing — wallets, msgpack, Algorand SDKs, WalletConnect — lives behind the
 * one-method interface below, so the risky, fast-moving part of the stack is
 * swappable without touching the request layer or any page controller.
 *
 *     interface PaymentProvider {
 *       readonly id: string
 *       readonly label: string
 *       readonly ready: boolean
 *       async pay(paymentRequired): Promise<string>   // header value
 *     }
 *
 * Two implementations ship today:
 *
 *   MockPaymentProvider   mints a fresh base64 payload carrying the mock marker
 *                         an `X402_MODE=mock` backend accepts as a valid payment
 *                         (see api/x402/schemas_compat). One payment per call,
 *                         as a real wallet would. This is what makes the whole
 *                         pay-and-reveal flow demoable and testable with no
 *                         chain and no wallet.
 *
 *   WalletPaymentProvider signs a real USDC transfer on Algorand through Pera
 *                         or Defly. The heavy lifting — algosdk and the wallet
 *                         SDKs — lives in `js/wallet/`, bundled to
 *                         `dist/wallet.js` and loaded with a dynamic import the
 *                         first time someone reaches for a wallet, so no other
 *                         page pays for it.
 *
 * The two money-critical pieces are deliberately kept out of the bundle and
 * under test in node: `js/wallet/exact-avm.js` (what we sign) and
 * `js/wallet/envelope.js` (what we send). See `npm test` in web/.
 */

import { isMockMode } from "./config.js";
import { buildPaymentHeader } from "./wallet/envelope.js";

/**
 * Magic single-use header value an `X402_MODE=mock` backend also accepts, for
 * hand-rolled `curl` checks. `MockPaymentProvider` sends a per-call payload
 * instead, because this constant is the same request-binding payment every time.
 */
export const MOCK_PAYMENT_HEADER = "mock-paid";

/** x402 protocol version this client speaks. V2 = camelCase wire, new header names. */
export const X402_VERSION = 2;

/** Thrown by `pay()` when the user dismisses their wallet. Not an error state. */
export class PaymentCancelled extends Error {
  constructor(message = "Payment cancelled.") {
    super(message);
    this.name = "PaymentCancelled";
    this.cancelled = true;
  }
}

/** Thrown by a provider that exists but cannot sign yet. Carries UI copy. */
export class NotImplementedError extends Error {
  constructor(message) {
    super(message);
    this.name = "NotImplementedError";
    this.notImplemented = true;
  }
}

/**
 * Base class documenting the contract. Subclass and override `pay()`.
 *
 * @abstract
 */
export class PaymentProvider {
  /** @type {string} stable id, used in UI state and telemetry */
  get id() {
    return "abstract";
  }

  /** @type {string} short human label for buttons */
  get label() {
    return "Payment provider";
  }

  /** @type {boolean} whether `pay()` can currently succeed */
  get ready() {
    return false;
  }

  /**
   * Produce the value of the `PAYMENT-SIGNATURE` request header for one 402.
   *
   * @param {object} _paymentRequired the V2 PaymentRequired body, camelCase, as
   *   it arrived at the root of the 402 response: `{x402Version, error,
   *   resource:{url,description,mimeType}, accepts:[{scheme, network, asset,
   *   amount, payTo, maxTimeoutSeconds, extra}], extensions}`.
   * @returns {Promise<string>} base64 JSON, or the literal mock token.
   */
  async pay(_paymentRequired) {
    throw new NotImplementedError("This payment provider cannot sign payments.");
  }
}

/**
 * Test-mode provider. Sends the magic header an `X402_MODE=mock` backend
 * short-circuits on. A live backend rejects it at verify, which is exactly the
 * behaviour we want: the mock never accidentally works in production.
 */
export class MockPaymentProvider extends PaymentProvider {
  get id() {
    return "mock";
  }

  get label() {
    return "Test payment";
  }

  get ready() {
    return true;
  }

  async pay(paymentRequired) {
    // The 402 is not needed to produce the token, but reading it here keeps the
    // mock honest about the contract: a provider that cannot understand the
    // requirements should not be claiming to satisfy them.
    const accepts = paymentRequired && paymentRequired.accepts;
    if (!Array.isArray(accepts) || accepts.length === 0) {
      throw new Error("402 body carried no payment requirements.");
    }
    // A *distinct* payment per call, like a real wallet: the backend binds one
    // payment to one request and rejects a replay against a different body or
    // query (api/x402/middleware.py), so a constant token would 402 the second
    // question asked inside the 300s idempotency window.
    const payload = {
      x402Version: X402_VERSION,
      payload: { mock: true, nonce: crypto.randomUUID() },
      accepted: accepts[0],
    };
    return btoa(JSON.stringify(payload));
  }
}

/**
 * Where the wallet bundle lives, relative to this module.
 *
 * Loaded with a dynamic `import()` rather than a static one so that the pages
 * that never pay — docs, the free preview, the whole mock-mode demo — do not
 * download 1.2MB of algosdk and two wallet SDKs to render. It also means
 * `npm run build` is not required for local development: only the wallet path
 * needs it, and it says so when the bundle is missing.
 */
const WALLET_BUNDLE = "../dist/wallet.js";

/**
 * sessionStorage key holding `{kind, address}` — never any key material.
 *
 * The *kind* matters as much as the address. Remembering only the address means
 * a reload restores nothing usable for a Defly user: the UI reads the address
 * and shows "connected" while the SDK session is null, and the first payment
 * fails with "Connect a wallet before paying". Worse, a hard-coded Pera
 * reconnect on re-render can tear down a live Defly session.
 */
const SESSION_KEY = "playclock.wallet";

/** Resolved wallet bundle, cached across calls. */
let bundlePromise = null;

/** The live wallet session, if someone has connected on this page load. */
let session = null;

/**
 * Load the wallet bundle on first use.
 *
 * @returns {Promise<object>} the module
 */
export function loadWalletBundle() {
  if (!bundlePromise) {
    bundlePromise = import(WALLET_BUNDLE).catch((cause) => {
      bundlePromise = null;
      throw new Error(
        "The wallet bundle is missing. Run `npm ci && npm run build` in web/ to build it.",
        { cause },
      );
    });
  }
  return bundlePromise;
}

/** `{kind, address}` remembered for this tab, or `null`. */
function rememberedSession() {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && parsed.address ? parsed : null;
  } catch {
    return null; // private mode, storage disabled, or a stale shape
  }
}

/** The connected address, restored from this tab's session if present. */
export function connectedAddress() {
  if (session) return session.address;
  const remembered = rememberedSession();
  return remembered ? remembered.address : null;
}

/** Which wallet is connected (`"pera"` / `"defly"`), or `null`. */
export function connectedWalletKind() {
  if (session) return session.kind;
  const remembered = rememberedSession();
  return remembered ? remembered.kind : null;
}

/**
 * Connect a wallet. Opens the wallet's own modal.
 *
 * @param {object} args
 * @param {string} args.kind `"pera"` or `"defly"`
 * @param {string} args.network CAIP-2 network from the 402
 * @param {boolean} [args.reconnectOnly] restore a session without prompting
 * @returns {Promise<string|null>} the connected address
 */
export async function connectWallet({ kind, network, reconnectOnly = false }) {
  const wallet = await loadWalletBundle();
  try {
    const next = await wallet.connectWallet({ kind, network, reconnectOnly });
    session = next;
  } catch (error) {
    if (error && error.cancelled) throw new PaymentCancelled(error.message);
    throw error;
  }
  const address = session ? session.address : null;
  try {
    if (address) sessionStorage.setItem(SESSION_KEY, JSON.stringify({ kind, address }));
  } catch {
    // Not being able to remember the session costs a reconnect, nothing more.
  }
  return address;
}

/** Drop the wallet session. Best effort — a wallet already gone is fine. */
export async function disconnectWallet() {
  if (session) await session.disconnect();
  session = null;
  try {
    sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* nothing to clean up */
  }
}

/** USDC has 6 decimals on Algorand (ASA 31566704 and TestNet 10458941). */
const USDC_DECIMALS = 6;
const USDC_UNIT = 10n ** BigInt(USDC_DECIMALS);

/**
 * Format atomic USDC units as "0.25 USDC", in BigInt throughout (Number rounds
 * above 2^53, and this is money). At least two decimals, no trailing zeros past
 * them. Returns null for anything that is not a non-negative integer.
 *
 * @param {string|number|bigint} value atomic units
 * @returns {string|null}
 */
export function formatUsdcAtomic(value) {
  let atomic;
  try {
    atomic = BigInt(value);
  } catch {
    return null;
  }
  if (atomic < 0n) return null;
  const whole = atomic / USDC_UNIT;
  const fraction = (atomic % USDC_UNIT)
    .toString()
    .padStart(USDC_DECIMALS, "0")
    .replace(/0+$/, "")
    .padEnd(2, "0");
  return `${whole}.${fraction} USDC`;
}

/**
 * Real wallet provider: signs a USDC transfer on Algorand and returns the
 * `PAYMENT-SIGNATURE` header for it.
 *
 * Constructed from one `accepts[]` entry so the UI can show network, asset,
 * amount and recipient before anyone commits to anything.
 */
export class WalletPaymentProvider extends PaymentProvider {
  /**
   * @param {object} requirements one entry of `paymentRequired.accepts`
   */
  constructor(requirements = {}) {
    super();
    this.requirements = requirements;
    this.scheme = requirements.scheme || "exact";
    this.network = requirements.network || "";
    this.asset = requirements.asset || "";
    this.amount = requirements.amount || "0";
    this.payTo = requirements.payTo || requirements.pay_to || "";
  }

  get id() {
    return "wallet";
  }

  get label() {
    return "Pay with wallet";
  }

  /** True once a wallet is connected; the UI gates the pay button on this. */
  get ready() {
    return Boolean(connectedAddress());
  }

  /** The connected address, or null. */
  get address() {
    return connectedAddress();
  }

  /**
   * Human-readable amount, e.g. "0.25 USDC", from the atomic-unit string.
   * Decimals and name are fixed, never read from the quote's `extra`: the
   * wallet layer only signs the USDC ASA, so a server-supplied label could only
   * ever make the prompt disagree with what is signed.
   */
  get displayAmount() {
    return formatUsdcAtomic(this.amount) ?? `${this.amount} (atomic)`;
  }

  /**
   * Sign one payment and return its header value.
   *
   * @param {object} paymentRequired the whole 402 body
   * @returns {Promise<string>} base64 JSON for `PAYMENT-SIGNATURE`
   */
  async pay(paymentRequired) {
    const accepts = (paymentRequired && paymentRequired.accepts) || [];
    const requirements = accepts[0] || this.requirements;
    if (!requirements) throw new Error("402 body carried no payment requirements.");

    const wallet = await loadWalletBundle();
    if (!session) {
      // A page reload leaves the remembered session but no live SDK. Restore
      // the wallet that was actually connected — reconnecting the wrong SDK
      // finds nothing and, worse, can disconnect the right one.
      const kind = connectedWalletKind();
      if (kind) {
        await connectWallet({ kind, network: requirements.network, reconnectOnly: true });
      }
    }
    if (!session) {
      throw new Error("Connect a wallet before paying.");
    }

    const algod = wallet.algodFor(requirements.network);
    await assertCanPay(wallet, algod, session.address, requirements);

    let payload;
    try {
      payload = await wallet.buildExactAvmPayload({
        requirements,
        address: session.address,
        algod,
        signTransactions: (unsigned, indexes) => session.signTransactions(unsigned, indexes),
      });
    } catch (error) {
      if (wallet.isRejection(error)) {
        throw new PaymentCancelled("You dismissed the signing request. Nothing was charged.");
      }
      throw error;
    }

    return buildPaymentHeader({ payload, requirements, paymentRequired });
  }
}

/**
 * Check the wallet can actually pay, before opening a signing prompt.
 *
 * Both failures here are common and neither produces a readable error on its
 * own: an un-opted-in account fails at the facilitator's simulate step and
 * comes back as a bare second 402, and an underfunded one does the same. One
 * algod read turns both into a sentence someone can act on.
 */
async function assertCanPay(wallet, algod, address, requirements) {
  const { assetIndex, amount } = wallet.readQuote(requirements);
  let holding;
  try {
    holding = await algod.accountAssetInformation(address, assetIndex).do();
  } catch (cause) {
    if (cause && /404|not found|no such/i.test(String(cause.message || cause))) {
      throw new Error(
        `This wallet is not opted in to the asset being charged (ASA ${assetIndex}). ` +
          "Opt in from your wallet, then try again — opting in is free.",
        { cause },
      );
    }
    return; // algod unreachable: let the facilitator be the judge rather than blocking
  }

  const held = BigInt(holding?.assetHolding?.amount ?? holding?.["asset-holding"]?.amount ?? 0);
  if (held < amount) {
    throw new Error(`This wallet holds too little USDC for a ${formatUsdcAtomic(amount)} payment.`);
  }
}

/**
 * Pick the provider for this page load.
 *
 * @param {object} [paymentRequired] the 402 body, when one is already in hand;
 *   its `accepts[0]` seeds the wallet provider.
 * @returns {PaymentProvider}
 */
export function getPaymentProvider(paymentRequired) {
  if (isMockMode()) return new MockPaymentProvider();
  const accepts = (paymentRequired && paymentRequired.accepts) || [];
  return new WalletPaymentProvider(accepts[0] || {});
}

/* ===========================================================================
 * HOW A WALLET PAYMENT IS PUT TOGETHER
 * ===========================================================================
 *
 * 1. CONNECT — `connectWallet({kind, network})` loads the bundle, opens Pera or
 *    Defly, and remembers the *address* (never key material) in sessionStorage
 *    so a page navigation does not force a reconnect.
 *
 * 2. PRE-FLIGHT — `assertCanPay` reads the account's holding of the quoted ASA.
 *    Both common failures — not opted in, not enough balance — otherwise fail
 *    at the facilitator's simulate step and come back as a bare second 402 with
 *    nothing to tell the person. One algod read turns them into a sentence.
 *
 * 3. BUILD — `js/wallet/exact-avm.js` turns `accepts[0]` into the transaction
 *    group. Play Clock's 402 offers no `feePayer`, so that is one asset-transfer
 *    transaction and no group id (verified on TestNet: DESIGN_NOTES, checklist
 *    item 1). The sponsored two-transaction shape is implemented but has never
 *    been seen live, and is marked as such.
 *
 * 4. SIGN — the wallet signs only the slots whose sender is its own address.
 *    `alignSignatures` normalises the two different return shapes Pera and
 *    Defly have shipped.
 *
 * 5. ENVELOPE — `js/wallet/envelope.js` wraps `{paymentGroup, paymentIndex}` as
 *    a V2 PaymentPayload and base64s it. It echoes `accepted` unmodified, and
 *    `extensions` with it: the Bazaar discovery block rides in-band on the
 *    payment, so dropping it settles the money and never lists the endpoint.
 *
 * 6. RECEIPT — api.js decodes `PAYMENT-RESPONSE`; `explorerUrl()` in
 *    `js/wallet/exact-avm.js` turns the txid into a Pera explorer link.
 *
 * Failure modes are handled where they happen: a dismissed prompt becomes
 * PaymentCancelled (the UI treats it as "no harm done"), a 402 on the paid
 * retry surfaces the server's own reason, and a settlement failure after a 2xx
 * still delivers the answer and says no USDC moved.
 * =========================================================================== */
