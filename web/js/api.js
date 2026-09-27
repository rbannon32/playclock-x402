/**
 * API client for the Play Clock service, including the x402 handshake.
 *
 * Protocol facts this file is pinned to (api/x402/schemas_compat.py):
 *
 *  - A paid endpoint answers an unpaid request with **402** and the V2
 *    `PaymentRequired` object **at the root of the JSON body** (camelCase). The
 *    same object is also base64'd into the `PAYMENT-REQUIRED` response header;
 *    we read the body first and fall back to the header.
 *  - The client pays by **retrying the identical request** with a
 *    `PAYMENT-SIGNATURE` header whose value is base64 JSON (or, against a
 *    mock-mode backend, the literal string `mock-paid`).
 *  - A successful paid response carries the settlement receipt in the
 *    `PAYMENT-RESPONSE` header, base64 JSON:
 *    `{success, errorReason, errorMessage, payer, transaction, network}`.
 *    The server lists it in `Access-Control-Expose-Headers`, so it is readable
 *    cross-origin.
 *  - `X402_MODE=disabled` backends simply return 200 on the first call. That is
 *    a legitimate configuration (local hacking, CI) and is reported as
 *    `receipt: null, paid: false` rather than treated as an error.
 */

import { PRODUCTION_PAYMENT, apiUrl } from "./config.js";
import { PaymentCancelled, getPaymentProvider } from "./payment.js";

/** Header names — V2. `X-PAYMENT*` are V1 legacy and not used by this client. */
export const HEADERS = Object.freeze({
  paymentSignature: "PAYMENT-SIGNATURE",
  paymentRequired: "PAYMENT-REQUIRED",
  paymentResponse: "PAYMENT-RESPONSE",
});

/**
 * How long any single request may take before we give up. This must exceed the
 * paid quote's 120-second maxTimeoutSeconds: settlement happens before analysis,
 * so a shorter browser timer can charge the user and discard the answer.
 */
const DEFAULT_TIMEOUT_MS = 180_000;

/**
 * Anything that went wrong talking to the API.
 *
 * `kind` is what the UI switches on:
 *   "network"  — could not reach the API at all (server down, CORS, offline)
 *   "http"     — the API answered with a non-2xx we did not expect
 *   "parse"    — the API answered with something that was not the JSON we expect
 *   "timeout"  — the request exceeded the deadline
 */
export class ApiError extends Error {
  constructor(message, { kind = "http", status = 0, body = null, cause = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.body = body;
    if (cause) this.cause = cause;
  }

  /** Copy suitable for showing to a fantasy manager, not a developer. */
  get userMessage() {
    if (this.kind === "network") {
      return "Can't reach the Play Clock API. It may be starting up — try again in a moment.";
    }
    if (this.kind === "timeout") {
      return "That took too long. Try again in a moment.";
    }
    // The API writes user-facing copy into `detail` (e.g. an upstream outage
    // explaining that nothing was charged). Prefer it over anything generic.
    const detail = detailText(this.body);
    if (detail) return detail;
    if (this.status === 404) return "That endpoint isn't available on this server.";
    if (this.status === 422 || this.status === 400) {
      return "The request wasn't valid — check the inputs above.";
    }
    if (this.status >= 500) {
      return "The API hit an error generating that analysis. No payment was settled.";
    }
    return this.message;
  }
}

/** Payment-specific failure (verify rejected, provider threw, settle failed). */
export class PaymentError extends Error {
  constructor(message, { paymentRequired = null, cause = null, cancelled = false } = {}) {
    super(message);
    this.name = "PaymentError";
    this.paymentRequired = paymentRequired;
    this.cancelled = cancelled;
    if (cause) this.cause = cause;
  }
}

/**
 * The payment header was sent and no definitive answer came back: a timeout, a
 * dropped connection, a proxy 502/504, or the server still settling
 * (`payment_in_progress_*`). The USDC may already have moved, so this is never
 * "try again" — a new attempt signs a second payment. Replaying the stored
 * request with the same header returns the paid answer without charging again,
 * for as long as the server's idempotency window holds (`recoverable`).
 */
export class PaymentInFlight extends Error {
  constructor(message, { pending = null, cause = null, reason = "", expired = false } = {}) {
    super(message);
    this.name = "PaymentInFlight";
    this.pending = pending;
    this.reason = reason;
    this.expired = expired;
    if (cause) this.cause = cause;
  }

  /** Whether the stored request can still be replayed for the paid answer. */
  get recoverable() {
    return !this.expired && pendingPaymentReplayable(this.pending);
  }

  /** Copy suitable for showing to a fantasy manager, not a developer. */
  get userMessage() {
    if (!this.recoverable) {
      return (
        "Your payment was sent too long ago: the server holds a paid answer for about five " +
        "minutes, so it can no longer be retrieved. It may have been charged — check your " +
        "wallet history before paying again."
      );
    }
    if (this.reason === "replay_failed") {
      return (
        "Your payment went through, but generating the answer failed. Don't pay again — use " +
        "“Retrieve my paid answer” in a minute to try again without a new charge."
      );
    }
    if (this.reason === "in_progress") {
      return (
        "Your payment is still being processed. Don't pay again — use “Retrieve my paid " +
        "answer” in a few seconds to collect it without a new charge."
      );
    }
    return (
      "Your payment was sent but the answer didn't arrive, so it may already have been " +
      "charged. Don't pay again — use “Retrieve my paid answer” within the next few minutes " +
      "to collect it without a new charge."
    );
  }
}

/** Pull a human string out of a FastAPI/pydantic error body. */
function detailText(body) {
  if (!body) return "";
  const detail = body.detail ?? body.error ?? body.message;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const loc = Array.isArray(item.loc) ? item.loc.filter((p) => p !== "body").join(".") : "";
        return loc ? `${loc}: ${item.msg}` : item.msg;
      })
      .filter(Boolean)
      .join("; ");
  }
  return "";
}

/**
 * Decode a base64 (standard or url-safe, padded or not) JSON header value.
 *
 * @returns {object|null} null rather than throwing — a malformed receipt must
 *   never lose the user the answer they already paid for.
 */
export function decodeBase64Json(value) {
  if (!value) return null;
  let text = String(value).trim().replace(/-/g, "+").replace(/_/g, "/");
  text += "=".repeat((4 - (text.length % 4)) % 4);
  try {
    const binary = atob(text);
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    const json = new TextDecoder().decode(bytes);
    const parsed = JSON.parse(json);
    return typeof parsed === "object" && parsed !== null ? parsed : null;
  } catch {
    // Some servers/proxies hand back unencoded JSON. Accept that too.
    try {
      const parsed = JSON.parse(String(value));
      return typeof parsed === "object" && parsed !== null ? parsed : null;
    } catch {
      return null;
    }
  }
}

/** Base64-encode a JS object as JSON, UTF-8 safe. Used by wallet providers. */
export function encodeBase64Json(object) {
  const bytes = new TextEncoder().encode(JSON.stringify(object));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

/** Build a query string from a plain object, dropping empty values. */
function queryString(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

/**
 * One HTTP round trip. Returns the raw Response plus its parsed body; never
 * throws on a non-2xx status (callers decide what a 402 means).
 *
 * @returns {Promise<{response: Response, body: any}>}
 */
async function rawRequest(
  path,
  { method = "GET", query, body, headers = {}, timeoutMs, url: fixedUrl } = {},
) {
  const url = fixedUrl || apiUrl(path) + queryString(query);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs || DEFAULT_TIMEOUT_MS);

  const init = {
    method,
    headers: { Accept: "application/json", ...headers },
    signal: controller.signal,
  };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }

  let response;
  let text;
  try {
    response = await fetch(url, init);
    // Headers alone are not an answer. Keep the deadline active while reading
    // the body, and preserve payment recovery if the stream fails.
    text = await response.text();
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new ApiError(`Request to ${path} timed out.`, { kind: "timeout", cause: error });
    }
    throw new ApiError(`Could not reach ${url}.`, { kind: "network", cause: error });
  } finally {
    clearTimeout(timer);
  }

  let parsed = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
      if (response.ok) {
        throw new ApiError(`${path} returned a non-JSON body.`, {
          kind: "parse",
          status: response.status,
        });
      }
    }
  }
  if (response.ok && (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))) {
    throw new ApiError(`${path} returned no JSON object.`, {
      kind: "parse",
      status: response.status,
    });
  }
  return { response, body: parsed };
}

/** GET a free endpoint and return its parsed body, throwing ApiError on failure. */
async function getJson(path, options = {}) {
  const { response, body } = await rawRequest(path, options);
  if (!response.ok) {
    throw new ApiError(`${path} returned ${response.status}.`, {
      status: response.status,
      body,
    });
  }
  return body;
}

/* ------------------------------------------------------------------ free */

/** `GET /v1/health` */
export function getHealth() {
  return getJson("/v1/health", { timeoutMs: 8000 });
}

/** `GET /v1/catalog` — prices, descriptions, network and payTo. */
export function getCatalog() {
  return getJson("/v1/catalog", { timeoutMs: 10_000 });
}

/** `GET /openapi.json` — the request schemas the forms are built from. */
export function getOpenApi() {
  return getJson("/openapi.json", { timeoutMs: 10_000 });
}

/** `GET /v1/trending/preview` — the free teaser board. */
export function getTrendingPreview() {
  return getJson("/v1/trending/preview", { timeoutMs: 15_000 });
}

/* ------------------------------------------------------ pending payments */

/**
 * CLAUDE.md: clients must persist PAYMENT-SIGNATURE until they hold the
 * response. Settlement precedes the handler, so a paid request that times out
 * has usually already moved USDC; replaying the identical request (method,
 * path, query, body, header) inside the server's 300-second idempotency window
 * returns the paid answer and settles nothing again.
 *
 * Stored in localStorage, so it survives a reload *and* a closed tab (the
 * sessionStorage it used to live in died with the tab, taking the only way
 * back to a paid answer with it). Entries stop being replayable after 290s and
 * are capped at ten, so nothing sensitive lingers usefully. Keyed by the
 * *whole* signature: x402 envelopes share a long base64 prefix, so anything
 * shorter would merge two pending payments.
 */
export const PENDING_PAYMENTS_KEY = "playclock.pendingPayments";

/** Server window is 300s from verify; stop offering a replay a little before. */
export const PENDING_REPLAY_WINDOW_MS = 290_000;

/** Expired entries are kept this long only so the UI can say they expired. */
const PENDING_RETENTION_MS = 60 * 60 * 1000;
const PENDING_MAX_ENTRIES = 10;

function journalStore() {
  try {
    return globalThis.window && globalThis.window.localStorage
      ? globalThis.window.localStorage
      : null;
  } catch {
    return null;
  }
}

function readPendingList() {
  try {
    const store = journalStore();
    const raw = store ? store.getItem(PENDING_PAYMENTS_KEY) : null;
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((item) => item && typeof item.signature === "string" && item.signature)
      : [];
  } catch {
    return [];
  }
}

function writePendingList(list) {
  try {
    const store = journalStore();
    if (!store) return false;
    if (list.length) store.setItem(PENDING_PAYMENTS_KEY, JSON.stringify(list));
    else store.removeItem(PENDING_PAYMENTS_KEY);
    return true;
  } catch {
    return false;
  }
}

/** Whether a stored entry is still inside the replay window. */
export function pendingPaymentReplayable(entry, now = Date.now()) {
  if (!entry || !entry.signature || !Number.isFinite(entry.ts)) return false;
  return now - entry.ts < PENDING_REPLAY_WINDOW_MS;
}

/** Every stored pending payment, newest first (expired ones included). */
export function listPendingPayments(now = Date.now()) {
  return readPendingList()
    .filter((item) => now - (item.ts || 0) < PENDING_RETENTION_MS)
    .sort((a, b) => (b.ts || 0) - (a.ts || 0));
}

/**
 * Persist a paid request *before* it is sent. Returns the entry even when
 * storage is unavailable, so the in-memory error can still offer a replay.
 */
export function savePendingPayment(entry, now = Date.now()) {
  const record = { ...entry, ts: Number.isFinite(entry.ts) ? entry.ts : now };
  const rest = listPendingPayments(now).filter((item) => item.signature !== record.signature);
  writePendingList([record, ...rest].slice(0, PENDING_MAX_ENTRIES));
  return record;
}

/** Forget a pending payment once its response is in hand or it was refused. */
export function clearPendingPayment(signature) {
  if (!signature) return false;
  const list = readPendingList();
  const kept = list.filter((item) => item.signature !== signature);
  return kept.length === list.length ? true : writePendingList(kept);
}

function sameJson(a, b) {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/** The newest stored payment for exactly this request, if any. */
export function findPendingPayment(call, now = Date.now()) {
  if (!call) return null;
  return (
    listPendingPayments(now).find(
      (item) =>
        item.method === (call.method || "GET") &&
        item.path === call.path &&
        sameJson(item.query, call.query) &&
        sameJson(item.body, call.body),
    ) || null
  );
}

/** Marker the server adds when a replayed, already-settled payment's re-run fails. */
const REPLAY_SETTLED_MARKER = /already settled on its first use/i;

/**
 * Whether a non-2xx answered a payment that had already settled. The server
 * says so in the detail; a 5xx while recovering is treated the same, since
 * recovery only ever replays a payment that may have settled.
 */
/** The app's own words on a failure its handler raised: nothing settles. */
const NOT_CHARGED_MARKER = /you were not charged/i;

function settledReplayFailed(response, body, recovering) {
  if (REPLAY_SETTLED_MARKER.test(detailText(body))) return true;
  return recovering && response.status >= 500;
}

/**
 * Send the paid request described by a pending entry and classify the result.
 *
 * Definitive outcomes clear the entry: a 2xx (we hold the answer), a 402 other
 * than `payment_in_progress*` (the payment was refused, nothing settles), or
 * any other app status (the handler failed, settlement never runs). Anything
 * that leaves the outcome unknown keeps it and throws PaymentInFlight.
 */
async function sendPendingPayment(entry, onStage, { recovering = false } = {}) {
  let reply;
  try {
    reply = await rawRequest(entry.path, {
      method: entry.method,
      query: entry.query ?? undefined,
      // null is how an absent body is stored; sending "null" would change the
      // request fingerprint and turn a free replay into a refused one.
      body: entry.body ?? undefined,
      url: entry.url,
      headers: { [entry.header || HEADERS.paymentSignature]: entry.signature },
    });
  } catch (error) {
    if (error instanceof ApiError && ["timeout", "network", "parse"].includes(error.kind)) {
      throw new PaymentInFlight(error.message, {
        pending: entry,
        cause: error,
        reason: error.kind,
      });
    }
    // An unexpected local failure also cannot prove that no payment settled.
    // Leave the journal available for recovery.
    throw error;
  }

  const { response, body } = reply;
  if (response.status === 402) {
    const retryRequired = readPaymentRequired(response, body) || {};
    const reason = retryRequired.error || detailText(body) || "payment rejected";
    if (String(reason).startsWith("payment_in_progress")) {
      throw new PaymentInFlight(`Payment still in progress: ${reason}`, {
        pending: entry,
        reason: "in_progress",
      });
    }
    clearPendingPayment(entry.signature);
    throw new PaymentError(`Payment was not accepted: ${reason}`, {
      paymentRequired: retryRequired,
    });
  }

  const appNotCharged = response.status === 502 && NOT_CHARGED_MARKER.test(detailText(body));
  if ((response.status === 502 || response.status === 504) && !appNotCharged) {
    // A proxy/gateway answered, not the app: the handler may still be running
    // and settle behind it. The app's own 502 (Sleeper down) says "You were
    // not charged" in its JSON detail, and it is right: its handler raised, so
    // nothing settles. That one falls through to the definitive branch below.
    throw new PaymentInFlight(`${entry.path} returned ${response.status} after payment.`, {
      pending: entry,
      reason: "gateway",
    });
  }

  if (!response.ok && settledReplayFailed(response, body, recovering)) {
    // The payment settled on its first use and only this re-run failed: the
    // server says to retry with the same header, and this entry is the only
    // copy of it.
    throw new PaymentInFlight(`${entry.path} returned ${response.status} on replay.`, {
      pending: entry,
      reason: "replay_failed",
    });
  }

  if (!response.ok) {
    clearPendingPayment(entry.signature);
    throw new ApiError(`${entry.path} returned ${response.status} after payment.`, {
      status: response.status,
      body,
    });
  }

  clearPendingPayment(entry.signature);
  const receipt = readReceipt(response);
  onStage("done", receipt);
  return { data: body, receipt, paid: true };
}

/**
 * Retrieve the answer for a payment that was sent but never answered, by
 * replaying the identical request with the identical PAYMENT-SIGNATURE. Signs
 * nothing: the server returns the paid answer without settling again.
 *
 * @param {object|string} [which] a pending entry or its signature; defaults to
 *   the newest stored one.
 * @param {object} [options]
 * @param {(stage: string, detail?: any) => void} [options.onStage]
 * @param {number} [options.now] clock override for tests
 * @returns {Promise<{data: object, receipt: object|null, paid: true,
 *   recovered: true, paymentRequired: null}>}
 * @throws {PaymentInFlight|PaymentError|ApiError}
 */
export async function recoverPendingPayment(which, options = {}) {
  const now = options.now ?? Date.now();
  const onStage = options.onStage || (() => {});
  const stored = listPendingPayments(now);
  let entry = null;
  if (which && typeof which === "object") {
    entry = stored.find((item) => item.signature === which.signature) || which;
  } else if (typeof which === "string") {
    entry = stored.find((item) => item.signature === which) || null;
  } else {
    entry = stored[0] || null;
  }
  if (!entry || !entry.signature) {
    throw new PaymentError("There is no unanswered payment in this tab to retrieve.");
  }
  if (!pendingPaymentReplayable(entry, now)) {
    clearPendingPayment(entry.signature);
    throw new PaymentInFlight("The pending payment is past the replay window.", {
      pending: entry,
      expired: true,
    });
  }

  onStage("recovering");
  const result = await sendPendingPayment(entry, onStage, { recovering: true });
  return { ...result, recovered: true, paymentRequired: null };
}

/* ------------------------------------------------------------ paid flow */

/**
 * Read the 402's `PaymentRequired` object: root of the body, header as backup.
 */
function readPaymentRequired(response, body) {
  if (body && Array.isArray(body.accepts)) return body;
  // An app that forgot `paid_router()` nests the payload under `detail`.
  if (body && body.detail && Array.isArray(body.detail.accepts)) return body.detail;
  const header = response.headers.get(HEADERS.paymentRequired);
  const decoded = decodeBase64Json(header);
  if (decoded && Array.isArray(decoded.accepts)) return decoded;
  return null;
}

/** Read and decode the settlement receipt from a paid 2xx response. */
function readReceipt(response) {
  const raw = response.headers.get(HEADERS.paymentResponse);
  if (!raw) return null;
  const decoded = decodeBase64Json(raw);
  if (!decoded) return null;
  return {
    success: decoded.success !== false,
    transaction: decoded.transaction || "",
    network: decoded.network || "",
    payer: decoded.payer || null,
    errorReason: decoded.errorReason || decoded.error_reason || null,
    errorMessage: decoded.errorMessage || decoded.error_message || null,
  };
}

/** Refuse a quote that differs from what the rendered endpoint advertised. */
export function validatePaymentQuote(paymentRequired, expected) {
  const quote = paymentRequired && paymentRequired.accepts && paymentRequired.accepts[0];
  // Real money is pinned on every host: a MainNet quote may only pay Play
  // Clock's checked-in recipient in USDC, whatever the catalog claimed.
  if (quote && quote.network === PRODUCTION_PAYMENT.network) {
    const payTo = quote.payTo || quote.pay_to || "";
    const asset = String(quote.asset || "");
    if (payTo !== PRODUCTION_PAYMENT.payTo || asset !== PRODUCTION_PAYMENT.asset) {
      throw new PaymentError(
        "The MainNet payment quote did not name Play Clock's recipient and USDC. Nothing was signed.",
        { paymentRequired },
      );
    }
  }
  if (!expected) return;
  if (!quote) throw new PaymentError("The server sent no payable quote.", { paymentRequired });

  const actual = {
    scheme: quote.scheme || "exact",
    network: quote.network || "",
    asset: String(quote.asset || ""),
    payTo: quote.payTo || quote.pay_to || "",
    amount: String(quote.amount || ""),
  };
  for (const field of ["scheme", "network", "asset", "payTo", "amount"]) {
    if (!expected[field] || actual[field] !== String(expected[field])) {
      throw new PaymentError(
        `The payment quote did not match the ${field} shown by Play Clock. Nothing was signed.`,
        { paymentRequired },
      );
    }
  }

  const resourceUrl = paymentRequired.resource && paymentRequired.resource.url;
  if (expected.path) {
    if (!resourceUrl) {
      throw new PaymentError("The payment quote named no API resource. Nothing was signed.", {
        paymentRequired,
      });
    }
    try {
      const pageOrigin = typeof window !== "undefined" ? window.location.origin : expected.apiBase;
      const base = expected.apiBase || pageOrigin;
      const resource = new URL(resourceUrl, base);
      const expectedOrigin = new URL(base, pageOrigin).origin;
      if (resource.origin !== expectedOrigin || resource.pathname !== expected.path) {
        throw new Error("resource mismatch");
      }
    } catch {
      throw new PaymentError(
        "The payment quote named a different API resource. Nothing was signed.",
        { paymentRequired },
      );
    }
  }
}

/**
 * Call a paid endpoint, running the full 402 -> pay -> retry handshake.
 *
 * @param {object} call
 * @param {"GET"|"POST"} call.method
 * @param {string} call.path e.g. "/v1/player"
 * @param {object} [call.query] query params for GET endpoints
 * @param {object} [call.body] JSON body for POST endpoints
 * @param {object} [options]
 * @param {import("./payment.js").PaymentProvider} [options.provider] override
 *   the provider chosen from the current mode.
 * @param {(stage: string, detail?: any) => void} [options.onStage] progress
 *   callback: "requesting" | "quoted" | "paying" | "delivering" | "done".
 * @returns {Promise<{data: object, receipt: object|null, paid: boolean,
 *   paymentRequired: object|null}>}
 * @throws {ApiError|PaymentError}
 */
export async function callPaidEndpoint(call, options = {}) {
  const { method, path, query, body } = call;
  const onStage = options.onStage || (() => {});

  onStage("requesting");
  const first = await rawRequest(path, { method, query, body });

  if (first.response.ok) {
    // X402_MODE=disabled, or a cached freebie. Nothing was charged.
    onStage("done");
    return { data: first.body, receipt: null, paid: false, paymentRequired: null };
  }

  if (first.response.status !== 402) {
    throw new ApiError(`${path} returned ${first.response.status}.`, {
      status: first.response.status,
      body: first.body,
    });
  }

  const paymentRequired = readPaymentRequired(first.response, first.body);
  if (!paymentRequired) {
    throw new PaymentError(
      "The server asked for payment but sent no payment requirements we could read.",
    );
  }
  validatePaymentQuote(paymentRequired, options.expectedPayment);
  onStage("quoted", paymentRequired);

  const provider = options.provider || getPaymentProvider(paymentRequired);

  let signature;
  try {
    onStage("paying", provider);
    signature = await provider.pay(paymentRequired);
  } catch (error) {
    if (error instanceof PaymentCancelled || (error && error.cancelled)) {
      throw new PaymentError(error.message || "Payment cancelled.", {
        paymentRequired,
        cause: error,
        cancelled: true,
      });
    }
    throw new PaymentError(error && error.message ? error.message : "Payment failed.", {
      paymentRequired,
      cause: error,
    });
  }

  if (!signature) {
    throw new PaymentError("The payment provider returned no signature.", { paymentRequired });
  }

  // Persist before sending: once this header leaves, the USDC may move even
  // if the response never arrives, and replaying it is the only recovery.
  const pending = savePendingPayment({
    method,
    path,
    query: query ?? null,
    body: body ?? null,
    url: apiUrl(path) + queryString(query),
    header: HEADERS.paymentSignature,
    signature,
  });

  onStage("delivering");
  const result = await sendPendingPayment(pending, onStage);
  return { ...result, paymentRequired };
}
