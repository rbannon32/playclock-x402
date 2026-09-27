/**
 * The pay-and-reveal flow, shared by analyze.html and roster.html.
 *
 * Builds a request form from an endpoint's field spec, runs the 402 handshake
 * through `api.js`, and renders the result with a share-text button. Kept in one
 * place so the two pages cannot drift on the part that touches money.
 */

import {
  ApiError,
  PaymentError,
  PaymentInFlight,
  callPaidEndpoint,
  findPendingPayment,
  pendingPaymentReplayable,
  recoverPendingPayment,
} from "./api.js";
import { CAIP_NETWORKS, isMockMode } from "./config.js";
import { $, append, copyText, el, formatUsdc, replace, setStatus } from "./dom.js";
import { rememberReceipt } from "./history.js";
import {
  connectWallet,
  connectedAddress,
  connectedWalletKind,
  disconnectWallet,
  getPaymentProvider,
} from "./payment.js";
import { downloadShareCard, renderAnalysis, shareText } from "./render.js";

/* ------------------------------------------------------------------ form */

function fieldId(endpointKey, name, index) {
  return `f-${endpointKey}-${name}${index === undefined ? "" : `-${index}`}`;
}

function textInput(spec, id, value = "") {
  return el("input", {
    type: spec.type === "number" ? "number" : "text",
    id,
    name: spec.name,
    value,
    placeholder: spec.placeholder || "",
    min: spec.min !== undefined && spec.type === "number" ? spec.min : null,
    max: spec.max !== undefined && spec.type === "number" ? spec.max : null,
    inputmode: spec.type === "number" ? "numeric" : null,
    autocomplete: "off",
    spellcheck: spec.type === "number" ? "false" : null,
  });
}

/**
 * A 2–4 slot player repeater for POST /v1/matchup.
 * Adds and removes rows without ever dropping below `min` or above `max`.
 */
function playersField(endpointKey, spec) {
  const min = spec.min || 2;
  const max = spec.max || 4;
  const rows = el("div", { class: "grid" });

  const addBtn = el("button", {
    type: "button",
    class: "btn btn-sm btn-ghost",
    text: "+ Add player",
    on: { click: () => addRow("") },
  });

  function syncControls() {
    const inputs = Array.from(rows.querySelectorAll("input"));
    addBtn.disabled = inputs.length >= max;
    for (const row of rows.children) {
      const remove = row.querySelector("button");
      if (remove) remove.disabled = inputs.length <= min;
    }
  }

  function addRow(value) {
    const index = rows.children.length;
    if (index >= max) return;
    const id = fieldId(endpointKey, spec.name, index);
    const input = el("input", {
      type: "text",
      id,
      value,
      placeholder: `Player ${index + 1}`,
      autocomplete: "off",
    });
    const row = el("div", { class: "inline-row" }, [
      el("label", { class: "visually-hidden", for: id, text: `Player ${index + 1}` }),
      input,
      el("button", {
        type: "button",
        class: "btn btn-sm btn-ghost flex-none",
        text: "Remove",
        "aria-label": `Remove player ${index + 1}`,
        on: {
          click: () => {
            row.remove();
            syncControls();
          },
        },
      }),
    ]);
    rows.appendChild(row);
    syncControls();
  }

  for (let i = 0; i < min; i += 1) addRow("");

  const wrapper = el("fieldset", {}, [
    el("legend", { text: spec.label }),
    rows,
    addBtn,
    spec.hint ? el("p", { class: "field-hint", text: spec.hint }) : null,
  ]);

  wrapper.__read = () =>
    Array.from(rows.querySelectorAll("input"))
      .map((input) => input.value.trim())
      .filter(Boolean);

  return wrapper;
}

/**
 * Build the input form for one endpoint.
 *
 * @returns {{node: HTMLElement, read: () => {values: object, errors: string[]}}}
 */
export function buildForm(endpoint) {
  const spec = endpoint.form || { fields: [] };
  const container = el("div");
  const readers = [];

  for (const field of spec.fields) {
    if (field.type === "players") {
      const node = playersField(endpoint.key, field);
      container.appendChild(node);
      readers.push(() => {
        const players = node.__read();
        const errors = [];
        if (players.length < (field.min || 2)) {
          errors.push(`Enter at least ${field.min || 2} players to compare.`);
        }
        return { name: field.name, value: players.length ? players : undefined, errors };
      });
      continue;
    }

    const id = fieldId(endpoint.key, field.name);
    const input = textInput(field, id);
    container.appendChild(
      el("div", { class: "field" }, [
        el("label", { for: id, text: field.label + (field.required ? " *" : "") }),
        input,
        field.hint ? el("p", { class: "field-hint", text: field.hint }) : null,
      ]),
    );
    readers.push(() => {
      const raw = input.value.trim();
      const errors = [];
      if (!raw) {
        if (field.required) errors.push(`${field.label} is required.`);
        return { name: field.name, value: undefined, errors };
      }
      if (field.type === "number") {
        const num = Number(raw);
        if (!Number.isFinite(num)) {
          errors.push(`${field.label} must be a number.`);
          return { name: field.name, value: undefined, errors };
        }
        if (field.min !== undefined && num < field.min) {
          errors.push(`${field.label} must be at least ${field.min}.`);
        }
        if (field.max !== undefined && num > field.max) {
          errors.push(`${field.label} must be at most ${field.max}.`);
        }
        return { name: field.name, value: num, errors };
      }
      return { name: field.name, value: raw, errors };
    });
  }

  function read() {
    const values = {};
    const errors = [];
    for (const reader of readers) {
      const result = reader();
      errors.push(...result.errors);
      if (result.value !== undefined) values[result.name] = result.value;
    }
    return { values, errors };
  }

  return { node: container, read };
}

/** Split collected values into the query/body shape the endpoint expects. */
export function buildCall(endpoint, values) {
  const inBody = (endpoint.form && endpoint.form.in) === "body" || endpoint.method !== "GET";
  return {
    method: endpoint.method,
    path: endpoint.path,
    query: inBody ? undefined : values,
    body: inBody ? values : undefined,
  };
}

/* ------------------------------------------------------------- providers */

/** Shorten an Algorand address for display: first six and last four. */
function shortAddress(address) {
  return address.length > 12 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}

/**
 * The payment-provider row: a TEST MODE badge in mock mode, otherwise a live
 * wallet control that connects, shows who is connected, and disconnects.
 *
 * The row re-renders itself in place on every state change rather than asking
 * the page to re-run, so it can live inside any layout.
 *
 * @param {object} [catalog] `GET /v1/catalog`, for the network to connect on.
 *   Defaults to TestNet, which is the safe way to be wrong.
 */
export function providerNotice(catalog) {
  if (isMockMode()) {
    return el("div", { class: "inline-row", style: "margin:.4rem 0 .9rem" }, [
      el("span", { class: "badge badge-test flex-none", text: "Test mode" }),
      el("span", {
        class: "field-hint",
        style: "margin:0",
        text: "Payments are mocked. Works against an X402_MODE=mock backend only; no USDC moves.",
      }),
    ]);
  }

  const network = CAIP_NETWORKS[(catalog && catalog.network) || "testnet"] || CAIP_NETWORKS.testnet;
  const row = el("div", { class: "inline-row", style: "margin:.4rem 0 .9rem" });

  const hint = (text) => el("span", { id: "wallet-note", class: "field-hint", style: "margin:0", text });

  async function act(fn, busyLabel) {
    replace(row, [el("span", { class: "field-hint", style: "margin:0", text: busyLabel })]);
    try {
      await fn();
    } catch (error) {
      // A dismissed wallet prompt is not a failure; anything else gets said out
      // loud rather than leaving a button that silently does nothing.
      if (!(error && error.cancelled)) {
        render(error.message || "Could not reach the wallet.");
        return;
      }
    }
    render();
  }

  function render(problem) {
    const address = connectedAddress();
    if (address) {
      replace(row, [
        el("span", { class: "badge flex-none", text: `Wallet ${shortAddress(address)}` }),
        el("button", {
          class: "btn btn-sm btn-quiet flex-none",
          type: "button",
          text: "Disconnect",
          on: { click: () => act(() => disconnectWallet(), "Disconnecting…") },
        }),
        hint(
          "Each answer is signed as a separate USDC payment. There is no account, and " +
            "nothing you ask or receive is stored on our servers.",
        ),
      ]);
      return;
    }
    replace(row, [
      ...["pera", "defly"].map((kind) =>
        el("button", {
          class: "btn btn-sm flex-none",
          type: "button",
          text: `Connect ${kind === "pera" ? "Pera" : "Defly"}`,
          "aria-describedby": "wallet-note",
          on: { click: () => act(() => connectWallet({ kind, network }), "Opening your wallet…") },
        }),
      ),
      hint(problem || "Connect a wallet to pay per answer in USDC on Algorand."),
    ]);
  }

  render();
  // A page navigation loses the SDK but not the tab's remembered session, so
  // try to restore *the wallet that was actually connected*. Reconnecting a
  // hard-coded Pera would find nothing for a Defly user and could tear down
  // their live session. No remembered kind means nothing to restore.
  const remembered = connectedWalletKind();
  if (remembered) {
    connectWallet({ kind: remembered, network, reconnectOnly: true })
      .then((address) => {
        if (address) render();
      })
      // Silent on purpose: the buttons are already rendered and pressing one
      // is the fallback.
      .catch(() => {});
  }

  return row;
}

/* ------------------------------------------------------------------ flow */

function stageMessage(stage, detail, endpoint) {
  switch (stage) {
    case "requesting":
      return ["Requesting the answer…", "working"];
    case "quoted": {
      const accepts = (detail && detail.accepts && detail.accepts[0]) || {};
      const price = formatUsdc(Number(accepts.amount || 0) / 1_000_000);
      const network = accepts.network ? ` on ${accepts.network.split(":")[0]}` : "";
      return [`Payment required: ${price} USDC${network}. Authorising…`, "working"];
    }
    case "paying":
      return ["Waiting for the payment to be signed…", "working"];
    case "recovering":
      return ["Retrieving the answer you already paid for — no new payment…", "working"];
    case "delivering":
      return [
        "Payment authorized. Generating the analysis — it settles only if the answer succeeds…",
        "working",
      ];
    default:
      return ["", "info"];
  }
}

/**
 * Run one paid call and render it.
 *
 * @param {object} options
 * @param {object} options.endpoint decorated catalog entry
 * @param {object} options.values collected form values
 * @param {HTMLElement} options.statusEl status paragraph
 * @param {HTMLElement} options.resultEl container for the rendered analysis
 * @param {HTMLButtonElement} [options.submitEl] button to disable while running
 * @returns {Promise<boolean>} whether a result was rendered
 */
export async function runPaidCall({ endpoint, values, statusEl, resultEl, submitEl }) {
  if (submitEl) submitEl.disabled = true;
  replace(resultEl, []);
  setStatus(statusEl, "Requesting the answer…", "working");

  const call = buildCall(endpoint, values);
  const onStage = (stage, detail) => {
    const [message, kind] = stageMessage(stage, detail, endpoint);
    if (message) setStatus(statusEl, message, kind);
  };
  const context = { endpoint, statusEl, resultEl, submitEl };
  // Set once a paid 2xx is in hand: past that point a failure is a rendering
  // failure, and "nothing was charged" would be a lie.
  let result = null;

  try {
    // The identical request was already paid for and never answered: pressing
    // the button again must collect that answer, not sign a second payment.
    const pending = findPendingPayment(call);
    result =
      pending && pendingPaymentReplayable(pending)
        ? await recoverPendingPayment(pending, { onStage })
        : await callPaidEndpoint(call, {
            provider: getPaymentProvider(),
            expectedPayment: endpoint.payment,
            onStage,
          });
    showResult(endpoint, result, statusEl, resultEl);
    return true;
  } catch (error) {
    renderError(error, statusEl, { ...context, ...paidState(result) });
    return false;
  } finally {
    if (submitEl) submitEl.disabled = false;
  }
}

/** Render a paid (or free) answer, its share row, and remember the receipt. */
function showResult(endpoint, result, statusEl, resultEl) {
  setStatus(statusEl, "");
  const analysis = renderAnalysis(endpoint.key, result.data, {
    receipt: result.receipt,
    paid: result.paid,
  });
  append(analysis, shareRow(endpoint.key, result.data));
  replace(resultEl, analysis);
  // A mock settlement carries a fake txid that satisfies every check below;
  // TEST MODE purchases must not land in the real receipt rail.
  if (!isMockMode() && result.receipt && result.receipt.success && result.receipt.transaction) {
    rememberReceipt({
      transaction: result.receipt.transaction,
      payer: result.receipt.payer || connectedAddress() || "",
      network: result.receipt.network || "",
      endpoint: endpoint.key,
      title: endpoint.title,
      price: formatUsdc(endpoint.price_usdc),
      settledAt: new Date().toISOString(),
    });
    window.dispatchEvent(new CustomEvent("playclock:receipt"));
  }
  resultEl.scrollIntoView({ behavior: "smooth", block: "start" });
}

/**
 * The "your payment may have been charged" panel. Its only action replays the
 * stored request with the stored PAYMENT-SIGNATURE; it never signs anything.
 */
function recoveryPanel(error, { endpoint, statusEl, resultEl, submitEl }) {
  const button = el("button", {
    type: "button",
    class: "btn btn-primary flex-none",
    text: "Retrieve my paid answer",
    on: {
      click: async () => {
        button.disabled = true;
        if (submitEl) submitEl.disabled = true;
        let result = null;
        try {
          result = await recoverPendingPayment(error.pending, {
            onStage: (stage, detail) => {
              const [message, kind] = stageMessage(stage, detail, endpoint);
              if (message) setStatus(statusEl, message, kind);
            },
          });
          showResult(endpoint, result, statusEl, resultEl);
        } catch (next) {
          renderError(next, statusEl, {
            endpoint,
            statusEl,
            resultEl,
            submitEl,
            ...paidState(result),
          });
        } finally {
          button.disabled = false;
          if (submitEl) submitEl.disabled = false;
        }
      },
    },
  });
  return el("div", { class: "inline-row", style: "margin-top:1rem" }, [
    button,
    el("span", {
      class: "field-hint",
      style: "margin:0",
      text: "Replays the payment you already signed. Nothing new is signed or charged.",
    }),
  ]);
}

/**
 * What a failure after a paid 2xx may truthfully say: the answer arrived
 * (`paid`), and the PAYMENT-RESPONSE receipt confirms settlement (`settled`).
 * A 2xx with a missing or unsuccessful receipt moved no USDC we can vouch for.
 */
function paidState(result) {
  return {
    paid: Boolean(result && result.paid),
    settled: Boolean(result && result.receipt && result.receipt.success),
  };
}

/**
 * Turn any thrown error into one honest sentence for the status line.
 *
 * @param {object} [context] `{endpoint, statusEl, resultEl, submitEl, paid}`;
 *   when given, an unanswered payment gets a "Retrieve my paid answer" button in
 *   `resultEl` instead of an invitation to pay again. `paid` means a paid 2xx
 *   was already in hand, so the failure came after the charge; `settled` means
 *   its receipt confirmed the charge.
 */
export function renderError(error, statusEl, context = null) {
  if (context && context.paid) {
    // The payment settled and the answer arrived; only showing it failed.
    // Paying again would buy the same answer twice.
    console.error("rendering a paid answer failed", error);
    setStatus(
      statusEl,
      context.settled
        ? "Your payment went through, but the answer could not be displayed. " +
            "Do not pay again — reload the page, or check your receipt in the wallet."
        : "The answer arrived but could not be displayed, and its payment was not " +
            "confirmed. Check your wallet before paying again, then reload the page.",
      "error",
    );
    return;
  }
  if (error instanceof PaymentInFlight) {
    setStatus(statusEl, error.userMessage, "error");
    if (context && context.resultEl) {
      replace(context.resultEl, error.recoverable ? recoveryPanel(error, context) : []);
    }
    return;
  }
  if (error instanceof PaymentError) {
    if (error.cancelled) {
      setStatus(statusEl, "Payment cancelled — nothing was charged.", "info");
    } else {
      setStatus(statusEl, error.message, "error");
    }
    return;
  }
  if (error instanceof ApiError) {
    setStatus(statusEl, error.userMessage, "error");
    return;
  }
  console.error("unexpected failure", error);
  setStatus(statusEl, "Something went wrong. Nothing was charged.", "error");
}

function shareRow(endpointKey, data) {
  const feedback = el("span", { class: "field-hint", style: "margin:0", role: "status" });
  const pngButton = el("button", {
    type: "button",
    class: "btn btn-sm flex-none",
    text: "Download share card (PNG)",
    on: {
      click: async () => {
        pngButton.disabled = true;
        const ok = await downloadShareCard(endpointKey, data).catch(() => false);
        pngButton.disabled = false;
        feedback.textContent = ok ? "1200×630 card downloaded." : "Couldn’t create the image.";
      },
    },
  });
  const textButton = el("button", {
    type: "button",
    class: "btn btn-sm btn-ghost flex-none",
    text: "Copy share text",
    on: {
      click: async () => {
        const ok = await copyText(shareText(endpointKey, data));
        feedback.textContent = ok
          ? "Copied — paste it in your league chat."
          : "Couldn't copy automatically. Select the text and copy manually.";
      },
    },
  });
  return el("div", { class: "share-actions", style: "margin-top:1.2rem" }, [
    pngButton,
    textButton,
    feedback,
  ]);
}

/** Shorthand used by pages that just need the status element. */
export function statusRegion(root = document) {
  return $(".status", root);
}
