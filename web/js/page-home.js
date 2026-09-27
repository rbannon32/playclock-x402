/**
 * Landing page controller.
 *
 * Two independent fetches, each degrading on its own: the free trending
 * preview and the catalog. Neither can prevent the page from rendering, and
 * neither logs an uncaught error when there is no backend at all.
 */

import { getCatalog, getTrendingPreview } from "./api.js";
import { $, el, emptyState, formatUsdc, replace, setStatus } from "./dom.js";
import { FALLBACK_CATALOG, paidEndpoints, priceRange } from "./endpoints.js";
import { mountChrome } from "./partials.js";
import { endpointForQuestion, labelForEndpoint } from "./question-router.js";
import { renderPricingTable, renderTrendingPreview } from "./render.js";

mountChrome();

/**
 * The design's conversational entry point is a router, not a new untyped API.
 * Keep payment honest by taking the user to the existing structured form, with
 * their original question visible there as context.
 */
function routeQuestion(question) {
  const value = question.trim();
  const endpoint = endpointForQuestion(value);

  if (endpoint === "roster") {
    window.location.assign(`roster.html?question=${encodeURIComponent(value)}`);
    return;
  }
  window.location.assign(
    `analyze.html?endpoint=${encodeURIComponent(endpoint)}&question=${encodeURIComponent(value)}`,
  );
}

function mountQuickAsk() {
  const form = $("#quick-ask-form");
  const input = $("#quick-ask");
  const route = $("#quick-ask-route");
  if (!form || !input) return;

  const explainRoute = () => {
    const label = labelForEndpoint(endpointForQuestion(input.value));
    if (route) route.textContent = `→ ${label.toUpperCase()}`;
  };

  input.addEventListener("input", explainRoute);
  // A textarea is the right control (questions wrap), but Enter has to send it:
  // a stray newline is invisible in the box and changes where the question is
  // routed. Shift+Enter still inserts one.
  input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    form.requestSubmit();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const question = input.value.trim();
    if (!question) {
      input.focus();
      if (route) route.textContent = "Type a question first.";
      return;
    }
    routeQuestion(question);
  });

  for (const chip of document.querySelectorAll("[data-question]")) {
    chip.addEventListener("click", () => {
      input.value = chip.dataset.question || "";
      explainRoute();
      input.focus();
    });
  }
}

mountQuickAsk();

async function loadPreview() {
  const target = $("#trending-preview");
  const status = $("#trending-status");
  const upsell = $("#trending-upsell");
  if (!target) return;

  try {
    const preview = await getTrendingPreview();
    setStatus(status, "");
    replace(target, renderTrendingPreview(preview));
    if (upsell && preview && preview.upsell) {
      replace(upsell, [
        el("span", { text: preview.upsell.replace(/:\s*GET\s+\/v1\/trending$/, ".") }),
        " ",
        el("a", { href: "analyze.html?endpoint=trending", text: "Unlock the full board →" }),
      ]);
    }
  } catch (error) {
    replace(target, emptyState("Live trending data is unavailable right now."));
    setStatus(
      status,
      error && error.userMessage ? error.userMessage : "Could not load the trending preview.",
      "error",
    );
  }
}

/**
 * Fill the two headline price claims from the catalog.
 *
 * The markup ships without numbers — a price typed into a page is a price that
 * goes stale the first time the table moves — so until the catalog resolves the
 * hero reads "Priced per answer" and the CTA reads "Unlock full board".
 */
function fillPriceCopy(catalog) {
  const range = priceRange(catalog);
  const trustPrice = $("#trust-price-range");
  if (trustPrice && range) {
    trustPrice.textContent =
      range.min === range.max
        ? formatUsdc(range.min)
        : `${formatUsdc(range.min)}–${formatUsdc(range.max)}`;
  }

  const cta = $("#trending-cta");
  const trending = paidEndpoints(catalog).find((entry) => entry.key === "trending");
  if (cta && trending) {
    cta.textContent = `Unlock full board · ${formatUsdc(trending.price_usdc)} →`;
  }
}

async function loadPricing() {
  const target = $("#pricing");
  const note = $("#pricing-note");
  if (!target) return;

  let catalog = null;
  try {
    catalog = await getCatalog();
  } catch {
    catalog = FALLBACK_CATALOG;
    if (note) {
      note.textContent =
        "Showing published list prices — the live catalog was unreachable, so these may be stale.";
      note.hidden = false;
    }
  }

  fillPriceCopy(catalog);

  try {
    replace(target, renderPricingTable(paidEndpoints(catalog)));
  } catch (error) {
    console.warn("could not render the pricing table", error);
    replace(target, emptyState("Pricing is temporarily unavailable."));
    return;
  }

  const network = $("#network-note");
  if (network && catalog && catalog.network) {
    network.textContent =
      `Payments settle in USDC on Algorand ${catalog.network} via the x402 protocol.` +
      (catalog.challenge_tag ? ` Challenge tag: ${catalog.challenge_tag}.` : "");
    network.hidden = false;
  }
}

// Both are self-contained; the .catch() guards are belt-and-braces so a
// surprise can never surface as an unhandled rejection in the console.
loadPreview().catch((error) => console.warn("trending preview failed", error));
loadPricing().catch((error) => console.warn("pricing failed", error));
