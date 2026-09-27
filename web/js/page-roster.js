/**
 * Roster page controller.
 *
 * One form, two paid destinations: `POST /v1/roster` (the audit) and
 * `POST /v1/team-report` (the upsell — same inputs, deeper answer, graded
 * against the user's actual leaguemates). Both run through the identical
 * pay-and-reveal flow, so the upsell costs no extra client code.
 */

import { getCatalog, getOpenApi } from "./api.js";
import { $, el, formatUsdc, replace, setStatus } from "./dom.js";
import { FALLBACK_CATALOG, endpointByKey } from "./endpoints.js";
import { providerNotice, runPaidCall } from "./flow.js";
import { mountChrome } from "./partials.js";

mountChrome();

const statusEl = $("#roster-status");
const resultEl = $("#roster-result");
const actionsEl = $("#roster-actions");
const upsellEl = $("#roster-upsell");
const noteEl = $("#roster-catalog-note");

const usernameEl = $("#sleeper-username");
const leagueEl = $("#league-id");
const weekEl = $("#roster-week");

/** Collect and validate the shared inputs once, for either destination. */
function readValues() {
  const errors = [];
  const values = {};

  const username = usernameEl.value.trim();
  if (!username) errors.push("Enter your Sleeper username.");
  else values.sleeper_username = username;

  const league = leagueEl.value.trim();
  if (league) values.league_id = league;

  const week = weekEl.value.trim();
  if (week) {
    const num = Number(week);
    if (!Number.isFinite(num) || num < 1 || num > 18) errors.push("Week must be between 1 and 18.");
    else values.week = num;
  }

  return { values, errors };
}

function runner(endpoint, button) {
  return async () => {
    const { values, errors } = readValues();
    if (errors.length) {
      setStatus(statusEl, errors.join(" "), "error");
      usernameEl.focus();
      return;
    }
    await runPaidCall({ endpoint, values, statusEl, resultEl, submitEl: button });
  };
}

/** Set once the catalog resolves, so Enter-in-a-field runs the audit too. */
let submitAudit = () => {};

async function init() {
  let catalog = null;
  try {
    catalog = await getCatalog();
  } catch {
    catalog = FALLBACK_CATALOG;
    if (noteEl) {
      noteEl.hidden = false;
      noteEl.textContent =
        "The API is unreachable, so these are published list prices and may be stale.";
    }
  }

  const openapi = await getOpenApi().catch(() => null);
  const roster = endpointByKey(catalog, "roster", openapi);
  const teamReport = endpointByKey(catalog, "team_report", openapi);

  const buttons = [];

  if (roster) {
    const button = el("button", {
      type: "submit",
      class: "btn btn-primary flex-none",
      text: `Audit my roster · ${formatUsdc(roster.price_usdc)} USDC`,
    });
    submitAudit = runner(roster, button);
    button.addEventListener("click", (event) => {
      event.preventDefault();
      submitAudit();
    });
    buttons.push(button);
  }

  replace(actionsEl, [providerNotice(catalog), el("div", { class: "inline-row" }, buttons)]);

  if (teamReport && upsellEl) {
    const button = el("button", {
      type: "button",
      class: "btn flex-none",
      text: `Run the full team report · ${formatUsdc(teamReport.price_usdc)} USDC`,
    });
    button.addEventListener("click", () => runner(teamReport, button)());
    replace(upsellEl, [
      el("h3", { text: "Want the version that knows your league?" }),
      el("p", { class: "endpoint-desc", text: teamReport.description }),
      button,
    ]);
    upsellEl.hidden = false;
  }
}

// Enter in any field runs the audit rather than reloading the page.
const formEl = $("#roster-form");
if (formEl) {
  formEl.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAudit();
  });
}

init().catch((error) => console.warn("page init failed", error));
