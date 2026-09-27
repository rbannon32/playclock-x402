/**
 * Docs page controller.
 *
 * The prose is static HTML (it must be readable with no backend), but the
 * numbers in the curl examples and the endpoint table come from the live
 * catalog when it is reachable — an agent developer reading this page should
 * never be quoted a price the server no longer charges.
 */

import { getCatalog, getHealth } from "./api.js";
import { $, el, formatUsdc, replace } from "./dom.js";
import { FALLBACK_CATALOG, paidEndpoints } from "./endpoints.js";
import { mountChrome } from "./partials.js";

mountChrome();

function renderEndpointTable(endpoints) {
  return el("div", { class: "table-scroll" }, [
    el("table", {}, [
      el("thead", {}, [
        el("tr", {}, [
          el("th", { scope: "col", text: "Endpoint" }),
          el("th", { scope: "col", text: "Key" }),
          el("th", { scope: "col", text: "Response schema" }),
          el("th", { scope: "col", class: "num", text: "USDC" }),
        ]),
      ]),
      el(
        "tbody",
        {},
        endpoints.map((entry) =>
          el("tr", {}, [
            el("td", {}, [el("code", { text: `${entry.method} ${entry.path}` })]),
            el("td", {}, [el("code", { text: entry.key })]),
            el("td", {}, [el("code", { text: entry.response_schema || "—" })]),
            el("td", { class: "num" }, [
              el("span", { class: "price-tag", text: formatUsdc(entry.price_usdc) }),
            ]),
          ]),
        ),
      ),
    ]),
  ]);
}

async function init() {
  let catalog = null;
  try {
    catalog = await getCatalog();
  } catch {
    catalog = FALLBACK_CATALOG;
    const note = $("#docs-catalog-note");
    if (note) {
      note.hidden = false;
      note.textContent = "Live catalog unreachable — showing published values.";
    }
  }

  replace($("#docs-endpoints"), renderEndpointTable(paidEndpoints(catalog)));

  const facts = $("#docs-facts");
  if (facts) {
    replace(
      facts,
      [
        ["Network", catalog.network ? `Algorand ${catalog.network}` : "—"],
        ["Facilitator", catalog.facilitator_url || "—"],
        ["Challenge tag", catalog.challenge_tag || "—"],
        ["payTo", catalog.pay_to || "not configured on this deployment"],
        ["USDC ASA id", catalog.asset_id ? String(catalog.asset_id) : "not configured"],
        ["API version", catalog.version || "—"],
      ].flatMap(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })]),
    );
  }
}

async function checkHealth() {
  const target = $("#docs-health");
  if (!target) return;
  try {
    const health = await getHealth();
    replace(target, [
      el("span", {
        class: `badge badge-${health.status === "ok" ? "high" : "medium"}`,
        text: health.status === "ok" ? "API online" : "API degraded",
      }),
      " ",
      el("span", {
        class: "field-hint",
        style: "margin:0",
        text: `season ${health.season ?? "?"}, week ${health.week ?? "?"}, engine ${health.engine ?? "?"}`,
      }),
    ]);
  } catch {
    replace(target, [
      el("span", { class: "badge badge-low", text: "API unreachable" }),
      " ",
      el("span", {
        class: "field-hint",
        style: "margin:0",
        text: "Set the API base URL in Developer settings, or start the service locally.",
      }),
    ]);
  }
}

init().catch((error) => console.warn("page init failed", error));
checkHealth().catch((error) => console.warn("health check failed", error));
