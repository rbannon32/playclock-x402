/**
 * Analyze page controller: endpoint picker -> per-endpoint form -> pay -> render.
 *
 * The endpoint list, prices and descriptions all come from `GET /v1/catalog`;
 * only the *input fields* come from this build (see js/endpoints.js). If the
 * catalog is unreachable the page still works against the published fallback,
 * with a visible warning that prices may be stale.
 */

import { getCatalog, getOpenApi } from "./api.js";
import { $, el, formatUsdc, replace, setStatus } from "./dom.js";
import { FALLBACK_CATALOG, paidEndpoints } from "./endpoints.js";
import { buildForm, providerNotice, runPaidCall } from "./flow.js";
import { readReceiptHistory } from "./history.js";
import { mountChrome } from "./partials.js";

mountChrome();

const pickerEl = $("#endpoint-picker");
const formEl = $("#endpoint-form");
const statusEl = $("#analyze-status");
const resultEl = $("#analyze-result");
const catalogNoteEl = $("#catalog-note");
const routedQuestionEl = $("#routed-question");
const receiptHistoryEl = $("#receipt-history");

let endpoints = [];
let selected = null;
let form = null;
/** The catalog this page rendered from. `select()` needs its payment network. */
let catalogDoc = null;
/** `/openapi.json`, when it loaded. Forms are derived from it. */
let openApiDoc = null;

function shortTransaction(value) {
  const text = String(value || "");
  return text.length > 12 ? `${text.slice(0, 6)}…${text.slice(-4)}` : text;
}

function renderReceiptHistory() {
  if (!receiptHistoryEl) return;
  const receipts = readReceiptHistory().slice(0, 5);
  if (receipts.length === 0) {
    replace(receiptHistoryEl, el("p", { class: "receipt-empty", text: "No purchases here yet." }));
    return;
  }
  replace(
    receiptHistoryEl,
    receipts.map((receipt) =>
      el("div", { class: "receipt-history-item" }, [
        el("span", { text: receipt.title || receipt.endpoint || "Analysis" }),
        el("strong", { text: receipt.price || "" }),
        el("code", { text: shortTransaction(receipt.transaction) }),
      ]),
    ),
  );
}

renderReceiptHistory();
window.addEventListener("playclock:receipt", renderReceiptHistory);

/** Endpoint key requested via `?endpoint=` (the landing page deep-links here). */
function requestedKey() {
  try {
    return new URLSearchParams(window.location.search).get("endpoint");
  } catch {
    return null;
  }
}

function showRoutedQuestion() {
  if (!routedQuestionEl) return;
  try {
    const question = new URLSearchParams(window.location.search).get("question");
    if (!question) return;
    routedQuestionEl.textContent = `ROUTED QUESTION · ${question}`;
    routedQuestionEl.hidden = false;
  } catch {
    /* The query is context only; the structured form remains authoritative. */
  }
}

function renderPicker() {
  replace(
    pickerEl,
    el(
      "ul",
      { class: "endpoint-list" },
      endpoints.map((endpoint) =>
        el("li", {}, [
          el("label", { class: "endpoint-option" }, [
            el("input", {
              type: "radio",
              name: "endpoint",
              value: endpoint.key,
              checked: selected && selected.key === endpoint.key,
              on: {
                change: () => select(endpoint.key),
              },
            }),
            el("span", { class: "endpoint-top" }, [
              el("span", { class: "endpoint-name", text: endpoint.title }),
              el("span", { class: "price-tag", text: formatUsdc(endpoint.price_usdc) }),
              el("span", { class: "endpoint-path", text: `${endpoint.method} ${endpoint.path}` }),
            ]),
            el("p", { class: "endpoint-desc", text: endpoint.description }),
          ]),
        ]),
      ),
    ),
  );
}

function select(key) {
  selected = endpoints.find((endpoint) => endpoint.key === key) || endpoints[0] || null;
  if (!selected) return;
  form = buildForm(selected);

  const submit = el("button", {
    type: "submit",
    class: "btn btn-primary btn-block",
    text: `Get analysis · ${formatUsdc(selected.price_usdc)} USDC`,
  });

  const formNode = el(
    "form",
    {
      novalidate: true,
      on: {
        submit: async (event) => {
          event.preventDefault();
          const { values, errors } = form.read();
          if (errors.length) {
            setStatus(statusEl, errors.join(" "), "error");
            return;
          }
          await runPaidCall({
            endpoint: selected,
            values,
            statusEl,
            resultEl,
            submitEl: submit,
          });
        },
      },
    },
    [
      el("h2", { text: selected.title }),
      el("p", { class: "field-hint", style: "margin-top:0", text: selected.description }),
      form.node,
      providerNotice(catalogDoc),
      submit,
    ],
  );

  replace(formEl, formNode);
  replace(resultEl, []);
  setStatus(statusEl, "");
  // Keep the deep link honest when the user changes their mind.
  try {
    const url = new URL(window.location.href);
    url.searchParams.set("endpoint", selected.key);
    window.history.replaceState(null, "", url);
  } catch {
    /* history is a nicety, not a requirement */
  }
}

async function init() {
  showRoutedQuestion();
  let catalog = null;
  try {
    catalog = await getCatalog();
  } catch {
    catalog = FALLBACK_CATALOG;
    if (catalogNoteEl) {
      catalogNoteEl.hidden = false;
      catalogNoteEl.textContent =
        "The API is unreachable, so this list shows published prices and may be stale. " +
        "Requests will fail until the API is up — check Developer settings for the API base URL.";
    }
  }

  catalogDoc = catalog;
  // Best effort: without it the hand-written specs still cover the endpoints
  // this build knows, and a newer one renders with price and description only.
  openApiDoc = await getOpenApi().catch(() => null);
  endpoints = paidEndpoints(catalog, openApiDoc);
  if (endpoints.length === 0) {
    replace(pickerEl, el("p", { class: "empty", text: "No paid endpoints are advertised." }));
    return;
  }

  const wanted = requestedKey();
  selected = endpoints.find((endpoint) => endpoint.key === wanted) || endpoints[0];
  renderPicker();
  select(selected.key);
}

init().catch((error) => console.warn("page init failed", error));
