import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, it } from "node:test";

import {
  ApiError,
  PENDING_PAYMENTS_KEY,
  PENDING_REPLAY_WINDOW_MS,
  PaymentError,
  PaymentInFlight,
  callPaidEndpoint,
  findPendingPayment,
  listPendingPayments,
  recoverPendingPayment,
  savePendingPayment,
  validatePaymentQuote,
} from "./api.js";

describe("API error copy", () => {
  it("shows a server-provided 404 detail instead of calling a valid endpoint unavailable", () => {
    const error = new ApiError("/v1/roster returned 404.", {
      status: 404,
      body: { detail: "'bannon' has no NFL leagues for the 2026 season." },
    });

    assert.equal(error.userMessage, "'bannon' has no NFL leagues for the 2026 season.");
  });

  it("does not promise a 20-second analysis time after a timeout", () => {
    const error = new ApiError("/v1/trending timed out.", { kind: "timeout" });

    assert.equal(error.userMessage, "That took too long. Try again in a moment.");
  });

  it("keeps the generic copy for a 404 with no API detail", () => {
    const error = new ApiError("/missing returned 404.", { status: 404 });

    assert.equal(error.userMessage, "That endpoint isn't available on this server.");
  });
});

describe("payment quote trust boundary", () => {
  const expected = {
    scheme: "exact",
    network: "algorand:mainnet",
    asset: "31566704",
    payTo: "MERCHANT",
    amount: "250000",
    path: "/v1/player",
    apiBase: "https://api.playclock.xyz",
  };
  const required = {
    resource: { url: "https://api.playclock.xyz/v1/player" },
    accepts: [{ ...expected }],
  };

  it("accepts an exact match", () => {
    assert.doesNotThrow(() => validatePaymentQuote(required, expected));
  });

  for (const field of ["network", "asset", "payTo", "amount"]) {
    it(`rejects a mismatched ${field} before signing`, () => {
      const altered = structuredClone(required);
      altered.accepts[0][field] = "attacker-value";
      assert.throws(() => validatePaymentQuote(altered, expected), PaymentError);
    });
  }

  it("rejects a different resource", () => {
    const altered = structuredClone(required);
    altered.resource.url = "https://evil.example/v1/player";
    assert.throws(() => validatePaymentQuote(altered, expected), PaymentError);
  });

  it("rejects a missing resource", () => {
    const altered = structuredClone(required);
    delete altered.resource;
    assert.throws(() => validatePaymentQuote(altered, expected), PaymentError);
  });
});

/* ---------------------------------------------------- pending payments */

function fakeWindow({ throwingStorage = false } = {}) {
  const session = new Map();
  const local = new Map();
  const storage = (values) => ({
    getItem: (key) => (values.has(key) ? values.get(key) : null),
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  });
  const throwing = {
    getItem: () => {
      throw new Error("SecurityError");
    },
    setItem: () => {
      throw new Error("QuotaExceededError");
    },
    removeItem: () => {
      throw new Error("SecurityError");
    },
  };
  globalThis.window = {
    location: { hostname: "localhost", search: "", origin: "http://localhost" },
    localStorage: throwingStorage ? throwing : storage(local),
    sessionStorage: storage(session),
  };
  // The pending-payment journal lives in localStorage: it must survive the tab.
  return local;
}

const QUOTE = {
  x402Version: 2,
  resource: { url: "http://localhost/v1/player" },
  accepts: [
    {
      scheme: "exact",
      network: "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
      asset: "10458941",
      payTo: "MERCHANT",
      amount: "100000",
    },
  ],
};

const CALL = { method: "POST", path: "/v1/player", body: { player: "Bijan Robinson", week: 3 } };

function json(status, body, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/**
 * A scripted fetch: the first call answers 402, the paid call runs `paid`.
 * Records every request so tests can compare the replay byte for byte.
 */
function scriptFetch(...paidResponses) {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({ url, method: init.method, headers: { ...init.headers }, body: init.body });
    const signature = init.headers["PAYMENT-SIGNATURE"];
    if (!signature) return json(402, QUOTE);
    const next = paidResponses.shift();
    if (!next) throw new Error("unexpected paid request");
    return typeof next === "function" ? next(init) : next;
  };
  return requests;
}

function provider(signature = "c2lnbmVkLXBheW1lbnQtb25l") {
  let calls = 0;
  return {
    get calls() {
      return calls;
    },
    pay: async () => {
      calls += 1;
      return signature;
    },
  };
}

function networkDown() {
  throw new TypeError("Failed to fetch");
}

function stored(journal) {
  return JSON.parse(journal.get(PENDING_PAYMENTS_KEY) || "[]");
}

describe("paid request persistence", () => {
  let journal;
  beforeEach(() => {
    journal = fakeWindow();
  });
  afterEach(() => {
    delete globalThis.window;
    delete globalThis.fetch;
  });

  it("stores the signed request before sending it and clears it on 2xx", async () => {
    let seenDuringSend = null;
    scriptFetch(() => {
      seenDuringSend = stored(journal);
      return json(200, { ok: true });
    });
    const result = await callPaidEndpoint(CALL, { provider: provider() });

    assert.equal(result.paid, true);
    assert.deepEqual(result.data, { ok: true });
    assert.equal(seenDuringSend.length, 1);
    const [entry] = seenDuringSend;
    assert.equal(entry.method, "POST");
    assert.equal(entry.path, "/v1/player");
    assert.deepEqual(entry.body, CALL.body);
    assert.equal(entry.header, "PAYMENT-SIGNATURE");
    assert.equal(entry.signature, "c2lnbmVkLXBheW1lbnQtb25l");
    assert.equal(typeof entry.ts, "number");
    assert.equal(journal.has(PENDING_PAYMENTS_KEY), false);
  });

  it("keeps a payment when the response body stream fails, then recovers without signing", async () => {
    const wallet = provider();
    scriptFetch(
      () => ({
        ok: true,
        status: 200,
        text: async () => { throw new TypeError("connection closed during body"); },
      }),
      json(200, { verdict: "Recovered answer" }),
    );
    const error = await callPaidEndpoint(CALL, { provider: wallet }).catch((e) => e);
    assert.ok(error instanceof PaymentInFlight);
    assert.equal(stored(journal).length, 1);
    const recovered = await recoverPendingPayment();
    assert.deepEqual(recovered.data, { verdict: "Recovered answer" });
    assert.equal(wallet.calls, 1);
    assert.equal(stored(journal).length, 0);
  });

  it("keeps the deadline active until the response body finishes", async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    scriptFetch((init) => ({
      ok: true,
      status: 200,
      text: () => new Promise((resolve, reject) => {
        init.signal.addEventListener("abort", () => {
          reject(new DOMException("body timed out", "AbortError"));
        }, { once: true });
        // Advance time after headers have arrived, while the body is pending.
        queueMicrotask(() => t.mock.timers.tick(180_000));
      }),
    }));
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.reason, "timeout");
    assert.equal(stored(journal).length, 1);
  });

  for (const body of ["", "null", "[]", '"not an answer"', "42", "{broken"]) {
    it(`keeps the payment for an unusable successful body: ${JSON.stringify(body)}`, async () => {
      scriptFetch(new Response(body, { status: 200 }));
      const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
      assert.ok(error instanceof PaymentInFlight);
      assert.equal(stored(journal).length, 1);
    });
  }

  it("keeps the payment on a network error and says it may have been charged", async () => {
    scriptFetch(networkDown);
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);

    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.recoverable, true);
    assert.match(error.userMessage, /may already have been charged/);
    assert.match(error.userMessage, /Don't pay again/);
    assert.doesNotMatch(error.userMessage, /try again/i);
    assert.equal(stored(journal).length, 1);
  });

  it("treats a timeout after payment as in flight, not as 'try again'", async () => {
    scriptFetch(() => {
      const abort = new Error("aborted");
      abort.name = "AbortError";
      throw abort;
    });
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.reason, "timeout");
    assert.equal(stored(journal).length, 1);
  });

  for (const status of [502, 504]) {
    it(`keeps the payment on a gateway ${status} instead of claiming nothing settled`, async () => {
      scriptFetch(json(status, { error: "upstream" }));
      const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
      assert.ok(error instanceof PaymentInFlight);
      assert.doesNotMatch(error.userMessage, /No payment was settled/);
      assert.equal(stored(journal).length, 1);
    });
  }

  it("clears the payment on the app's own 502, which says nothing was charged", async () => {
    scriptFetch(
      json(502, {
        detail: "Sleeper is not responding right now. You were not charged. Try again shortly.",
      }),
    );
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(!(error instanceof PaymentInFlight));
    assert.equal(error.status, 502);
    assert.equal(stored(journal).length, 0);
  });

  it("keeps the payment while the server reports payment_in_progress", async () => {
    scriptFetch(json(402, { ...QUOTE, error: "payment_in_progress_retry_shortly" }));
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.reason, "in_progress");
    assert.match(error.userMessage, /still being processed/);
    assert.equal(stored(journal).length, 1);
  });

  it("clears the payment on a definitive 402 refusal", async () => {
    scriptFetch(json(402, { ...QUOTE, error: "invalid_payment" }));
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(error instanceof PaymentError);
    assert.ok(!(error instanceof PaymentInFlight));
    assert.equal(journal.has(PENDING_PAYMENTS_KEY), false);
  });

  for (const status of [400, 404, 500, 503]) {
    it(`clears the payment on an app ${status} (the handler failed, nothing settles)`, async () => {
      scriptFetch(json(status, { detail: "You were not charged." }));
      const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, status);
      assert.equal(journal.has(PENDING_PAYMENTS_KEY), false);
    });
  }

  it("still pays and answers when localStorage throws", async () => {
    fakeWindow({ throwingStorage: true });
    scriptFetch(json(200, { ok: true }));
    const result = await callPaidEndpoint(CALL, { provider: provider() });
    assert.equal(result.paid, true);
  });

  it("still offers the in-memory replay when localStorage throws", async () => {
    fakeWindow({ throwingStorage: true });
    scriptFetch(networkDown);
    const error = await callPaidEndpoint(CALL, { provider: provider() }).catch((e) => e);
    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.recoverable, true);
    assert.equal(error.pending.signature, "c2lnbmVkLXBheW1lbnQtb25l");
  });
});

describe("pending-payment journal storage", () => {
  afterEach(() => {
    delete globalThis.window;
  });

  it("persists in localStorage so closing the tab does not lose a paid header", () => {
    const journal = fakeWindow();
    savePendingPayment({ signature: "c2ln", path: "/v1/player", method: "POST" });
    assert.equal(stored(journal).length, 1);
    assert.equal(globalThis.window.sessionStorage.getItem(PENDING_PAYMENTS_KEY), null);
  });
});

describe("recoverPendingPayment", () => {
  let journal;
  beforeEach(() => {
    journal = fakeWindow();
  });
  afterEach(() => {
    delete globalThis.window;
    delete globalThis.fetch;
  });

  it("replays the identical request and header, signs nothing, and clears the entry", async () => {
    const receipt = Buffer.from(
      JSON.stringify({ success: true, transaction: "TXID", network: "algorand:test" }),
    ).toString("base64");
    const requests = scriptFetch(
      networkDown,
      json(200, { ok: "replayed" }, { "PAYMENT-RESPONSE": receipt }),
    );
    const wallet = provider();
    await callPaidEndpoint(CALL, { provider: wallet }).catch(() => {});
    assert.equal(wallet.calls, 1);

    const result = await recoverPendingPayment();

    assert.equal(wallet.calls, 1, "recovery must not sign a second payment");
    assert.equal(result.recovered, true);
    assert.deepEqual(result.data, { ok: "replayed" });
    assert.equal(result.receipt.transaction, "TXID");
    const [, paid, replay] = requests;
    assert.equal(replay.url, paid.url);
    assert.equal(replay.method, paid.method);
    assert.equal(replay.body, paid.body);
    assert.equal(replay.headers["PAYMENT-SIGNATURE"], paid.headers["PAYMENT-SIGNATURE"]);
    assert.equal(journal.has(PENDING_PAYMENTS_KEY), false);
  });

  it("keeps the entry when the replayed re-run fails after settling", async () => {
    scriptFetch(
      networkDown,
      json(503, { detail: "Data not ready. This payment already settled on its first use." }),
      json(200, { ok: "second try" }),
    );
    await callPaidEndpoint(CALL, { provider: provider() }).catch(() => {});

    await assert.rejects(recoverPendingPayment(), (error) => {
      assert.equal(error.name, "PaymentInFlight");
      assert.equal(error.reason, "replay_failed");
      assert.equal(error.recoverable, true);
      return true;
    });
    assert.equal(journal.has(PENDING_PAYMENTS_KEY), true, "the header is the only way back");

    const result = await recoverPendingPayment();
    assert.deepEqual(result.data, { ok: "second try" });
    assert.equal(journal.has(PENDING_PAYMENTS_KEY), false);
  });

  it("replays a GET without inventing a body", async () => {
    const requests = scriptFetch(networkDown, json(200, { ok: true }));
    const call = { method: "GET", path: "/v1/trending", query: { week: 3 } };
    await callPaidEndpoint(call, { provider: provider() }).catch(() => {});
    await recoverPendingPayment();
    const [, paid, replay] = requests;
    assert.equal(paid.body, undefined);
    assert.equal(replay.body, undefined);
    assert.equal(replay.url, "/v1/trending?week=3");
    assert.equal(replay.headers["Content-Type"], undefined);
  });

  it("finds the stored payment for the same request only", async () => {
    scriptFetch(networkDown);
    await callPaidEndpoint(CALL, { provider: provider() }).catch(() => {});
    assert.ok(findPendingPayment({ ...CALL, body: { ...CALL.body } }));
    assert.equal(findPendingPayment({ ...CALL, body: { player: "Someone Else", week: 3 } }), null);
    assert.equal(findPendingPayment({ method: "GET", path: "/v1/player" }), null);
  });

  it("keeps two pending payments apart even with a shared signature prefix", async () => {
    const prefix = "eyJ4NDAyVmVyc2lvbiI6MiwicGF5bG9hZCI6".repeat(4);
    scriptFetch(networkDown, networkDown);
    await callPaidEndpoint(CALL, { provider: provider(`${prefix}A`) }).catch(() => {});
    const other = { method: "GET", path: "/v1/trending" };
    await callPaidEndpoint(other, { provider: provider(`${prefix}B`) }).catch(() => {});
    assert.equal(listPendingPayments().length, 2);
  });

  it("stays pending when the replay also fails, and can be retried", async () => {
    scriptFetch(networkDown, networkDown, json(200, { ok: true }));
    await callPaidEndpoint(CALL, { provider: provider() }).catch(() => {});
    const again = await recoverPendingPayment().catch((e) => e);
    assert.ok(again instanceof PaymentInFlight);
    assert.equal(again.recoverable, true);
    const result = await recoverPendingPayment();
    assert.deepEqual(result.data, { ok: true });
  });

  it("refuses to replay past the window and says so", async () => {
    const requests = scriptFetch(networkDown);
    await callPaidEndpoint(CALL, { provider: provider() }).catch(() => {});
    const [entry] = stored(journal);

    const error = await recoverPendingPayment(null, {
      now: entry.ts + PENDING_REPLAY_WINDOW_MS + 1,
    }).catch((e) => e);

    assert.ok(error instanceof PaymentInFlight);
    assert.equal(error.recoverable, false);
    assert.match(error.userMessage, /can no longer be retrieved/);
    assert.match(error.userMessage, /check your wallet history/);
    assert.equal(requests.length, 2, "an expired payment is not replayed");
  });

  it("says so when there is nothing to recover", async () => {
    await assert.rejects(recoverPendingPayment(), PaymentError);
  });
});
