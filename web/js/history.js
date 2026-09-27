const STORAGE_KEY = "playclock.receipts.v1";
const MAX_RECEIPTS = 25;

/**
 * The only fields that reach storage.
 *
 * A whitelist rather than a "don't pass the body" convention: the caller hands
 * this function whatever it has, and "response bodies are not persisted" has to
 * be a property of this file, not of every call site. Everything here is
 * settlement metadata the on-device rail renders or identifies a receipt by.
 */
const PERSISTED_FIELDS = [
  "transaction",
  "payer",
  "network",
  "endpoint",
  "title",
  "price",
  "settledAt",
];

function metadataOnly(entry) {
  const kept = {};
  for (const field of PERSISTED_FIELDS) {
    const value = entry[field];
    if (value === null || value === undefined || value === "") continue;
    kept[field] = String(value);
  }
  return kept;
}

function safeStorage(storage) {
  if (storage) return storage;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

/** Read locally persisted settlement receipts, newest first. */
export function readReceiptHistory(storage = null) {
  const target = safeStorage(storage);
  if (!target) return [];
  try {
    const parsed = JSON.parse(target.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(parsed) ? parsed.filter((item) => item && item.transaction) : [];
  } catch {
    return [];
  }
}

/** Persist receipt metadata only; paid response bodies never enter storage. */
export function rememberReceipt(entry, storage = null) {
  if (!entry || !entry.transaction) return false;
  const target = safeStorage(storage);
  if (!target) return false;
  const receipt = metadataOnly(entry);
  try {
    const prior = readReceiptHistory(target).filter(
      (item) => item.transaction !== receipt.transaction,
    );
    target.setItem(STORAGE_KEY, JSON.stringify([receipt, ...prior].slice(0, MAX_RECEIPTS)));
    return true;
  } catch {
    return false;
  }
}

export { STORAGE_KEY };
