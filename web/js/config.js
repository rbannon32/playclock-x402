/**
 * Runtime configuration for the static site.
 *
 * Nothing here is baked in at build time (there is no build step). The API base
 * URL and the payment mode are resolved at page load from, in order:
 *
 *   1. a query parameter (`?api=`, `?mock=1`) — shareable non-production dev links,
 *   2. localStorage (sticky across pages, set by the dev drawer),
 *   3. the deployed Play Clock API on the production web hosts, otherwise
 *      same-origin (and real wallet payments everywhere).
 *
 * The production UI and API are separate Cloud Run services, at
 * `playclock.xyz` and `api.playclock.xyz`. Laptop development remains
 * same-origin by default; when the API is on another port, use
 * `?api=http://localhost:8080` (or the dev drawer). Production ignores both
 * override settings and pins the quote recipient/network/asset; a MainNet quote
 * is pinned to the same values on every host. See web/README.md.
 */

export const STORAGE_KEYS = Object.freeze({
  apiBase: "playclock.apiBase",
  mock: "playclock.mockPayments",
});

export const PRODUCTION_API_BASE = "https://api.playclock.xyz";
const PRODUCTION_WEB_HOSTS = new Set(["playclock.xyz", "www.playclock.xyz"]);

/** Money-moving values the production site is allowed to sign. */
export const PRODUCTION_PAYMENT = Object.freeze({
  network: "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
  asset: "31566704",
  payTo: "MDBJMM6RJ4TM7W5FITZ3MWJTGTHMC4SKQJWRWI2JCQUKIR5LA7BPIUTMMM",
});

/**
 * Catalog network name -> the CAIP-2 id the wallet and the 402 both speak.
 * `/v1/catalog` says "testnet"/"mainnet"; `accepts[].network` is CAIP-2.
 */
export const CAIP_NETWORKS = Object.freeze({
  mainnet: "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
  testnet: "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
});

/**
 * Resolve a catalog network name to its CAIP-2 id. A value that is already
 * CAIP-2 passes through; an unknown name yields "" so the quote check refuses
 * rather than comparing against something it cannot mean.
 */
export function caipNetwork(name) {
  const value = String(name || "").trim();
  if (!value) return "";
  if (value.includes(":")) return value;
  return CAIP_NETWORKS[value.toLowerCase()] || "";
}

/** Product copy that must render even when /v1/catalog is unreachable. */
export const BRAND = Object.freeze({
  name: "Play Clock",
  tagline: "Pay-per-answer fantasy football intelligence.",
  watermark: "playclock • pay-per-answer fantasy AI",
});

/**
 * localStorage can throw (Safari private mode, disabled cookies). Every access
 * is wrapped so a hostile storage environment degrades to defaults rather than
 * throwing during module evaluation.
 */
function readStore(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStore(key, value) {
  try {
    if (value === null || value === undefined || value === "") {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
    return true;
  } catch {
    return false;
  }
}

function queryParam(name) {
  try {
    return new URLSearchParams(window.location.search).get(name);
  } catch {
    return null;
  }
}

/** Strip a trailing slash so `${base}${path}` never double-slashes. */
function normalizeBase(value) {
  const trimmed = (value || "").trim();
  if (!trimmed) return "";
  return trimmed.replace(/\/+$/, "");
}

function defaultApiBase() {
  try {
    return PRODUCTION_WEB_HOSTS.has(window.location.hostname.toLowerCase())
      ? PRODUCTION_API_BASE
      : "";
  } catch {
    return "";
  }
}

/** Production never accepts a query-string or local-storage API override. */
export function apiOverridesAllowed() {
  try {
    return !PRODUCTION_WEB_HOSTS.has(window.location.hostname.toLowerCase());
  } catch {
    return true;
  }
}

/**
 * Resolve a configured value, migrating the pre-domain blank override on the
 * production web hosts. Before the API had its own hostname, Developer
 * settings persisted an empty string to mean "same origin". Keeping that value
 * after the domain launch sends `/v1/*` to the static nginx service, where every
 * endpoint is a 404. Local and preview hosts still use blank as same-origin.
 */
function configuredApiBase(value) {
  const normalized = normalizeBase(value);
  const productionDefault = defaultApiBase();
  if (!normalized && productionDefault) {
    writeStore(STORAGE_KEYS.apiBase, null);
    return productionDefault;
  }
  return normalized;
}

/**
 * Resolve the API origin. Empty string means "same origin", which keeps local
 * development and alternate static hosts portable. The production web hosts
 * default to the permanent API origin.
 *
 * @returns {string} e.g. "" or "http://localhost:8080"
 */
export function getApiBase() {
  if (!apiOverridesAllowed()) {
    writeStore(STORAGE_KEYS.apiBase, null);
    return PRODUCTION_API_BASE;
  }
  const fromQuery = queryParam("api");
  if (fromQuery !== null) {
    const configured = configuredApiBase(fromQuery);
    if (normalizeBase(fromQuery)) writeStore(STORAGE_KEYS.apiBase, configured);
    return configured;
  }
  const stored = readStore(STORAGE_KEYS.apiBase);
  return stored === null ? defaultApiBase() : configuredApiBase(stored);
}

/** Persist the API origin used by every subsequent request. */
export function setApiBase(value) {
  if (!apiOverridesAllowed()) return writeStore(STORAGE_KEYS.apiBase, null);
  return writeStore(STORAGE_KEYS.apiBase, normalizeBase(value));
}

/** Absolute (or same-origin relative) URL for an API path. */
export function apiUrl(path) {
  return `${getApiBase()}${path}`;
}

/**
 * Whether the mock payment provider is active.
 *
 * `?mock=1` turns it on and sticks; `?mock=0` turns it off and sticks. This is
 * the switch that makes the whole pay-and-reveal flow exercisable against an
 * `X402_MODE=mock` backend with no wallet and no chain.
 */
export function isMockMode() {
  if (!apiOverridesAllowed()) {
    writeStore(STORAGE_KEYS.mock, null);
    return false;
  }
  const fromQuery = queryParam("mock");
  if (fromQuery !== null) {
    const on = fromQuery !== "0" && fromQuery !== "false";
    writeStore(STORAGE_KEYS.mock, on ? "1" : "");
    return on;
  }
  return readStore(STORAGE_KEYS.mock) === "1";
}

/** Persist the mock-payment toggle. */
export function setMockMode(on) {
  if (!apiOverridesAllowed()) return writeStore(STORAGE_KEYS.mock, null);
  return writeStore(STORAGE_KEYS.mock, on ? "1" : "");
}

/**
 * Expected quote values for an endpoint. Production, and any MainNet quote on
 * any host, uses the checked-in trust anchor; only TestNet follows the catalog.
 *
 * The host is not the trust boundary: off production `?api=` points the page at
 * any origin, and if that origin's catalog could name the MainNet payTo, a real
 * wallet would sign a real USDC transfer to whoever wrote it.
 */
export function expectedPayment(entry, catalog = {}) {
  const production =
    !apiOverridesAllowed() || caipNetwork(catalog.network) === CAIP_NETWORKS.mainnet;
  return {
    scheme: "exact",
    network: production ? PRODUCTION_PAYMENT.network : caipNetwork(catalog.network),
    asset: String(production ? PRODUCTION_PAYMENT.asset : catalog.asset_id || ""),
    payTo: production ? PRODUCTION_PAYMENT.payTo : catalog.pay_to,
    amount: String(Math.round(Number(entry.price_usdc) * 1_000_000)),
    path: entry.path,
    apiBase: getApiBase(),
  };
}
