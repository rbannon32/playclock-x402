/**
 * Shared page chrome without a framework or a template engine.
 *
 * Every page ships a `<div data-chrome="nav">` and a `<div data-chrome="footer">`
 * placeholder; `mountChrome()` fills them from the single definition here. That
 * keeps the nav and the legal footer DRY across four static pages, and it means
 * the HTML files stay valid, readable documents on their own.
 */

import { $, el, replace } from "./dom.js";
import {
  BRAND,
  apiOverridesAllowed,
  apiUrl,
  getApiBase,
  isMockMode,
  setApiBase,
  setMockMode,
} from "./config.js";

const NAV_ITEMS = [
  { href: "index.html", label: "Board" },
  { href: "analyze.html", label: "Analyze" },
  { href: "roster.html", label: "My roster" },
  { href: "docs.html", label: "For agents" },
];

/** Current page filename, e.g. "analyze.html" (directory index -> index.html). */
function currentPage() {
  const path = window.location.pathname;
  const last = path.slice(path.lastIndexOf("/") + 1);
  return last === "" ? "index.html" : last;
}

function buildNav() {
  const here = currentPage();
  return el("nav", { class: "site-nav", "aria-label": "Primary" }, [
    el("div", { class: "wrap" }, [
      el("a", { class: "brand", href: "index.html" }, [
        el("span", { class: "brand-mark", "aria-hidden": "true" }, [
          el("span", { class: "brand-hand" }),
        ]),
        el("span", { text: BRAND.name.toUpperCase() }),
      ]),
      el(
        "ul",
        { class: "nav-links" },
        NAV_ITEMS.map((item) =>
          el("li", {}, [
            el("a", {
              href: item.href,
              text: item.label,
              "aria-current": item.href === here ? "page" : null,
            }),
          ]),
        ),
      ),
      el("span", { class: "nav-season", text: "2026 · NFL" }),
      el("a", { class: "nav-action", href: "analyze.html", text: "Get a verdict" }),
    ]),
  ]);
}

function buildFooter() {
  return el("footer", { class: "site-footer" }, [
    el("div", { class: "wrap" }, [
      el("p", {}, [
        "Player statistics from ",
        el("a", {
          href: "https://github.com/nflverse",
          rel: "noopener noreferrer",
          target: "_blank",
          text: "nflverse",
        }),
        ", licensed ",
        el("a", {
          href: "https://creativecommons.org/licenses/by/4.0/",
          rel: "noopener noreferrer license",
          target: "_blank",
          text: "CC BY 4.0",
        }),
        ". Market signal, league and roster data from the public read-only ",
        el("a", {
          href: "https://docs.sleeper.com/",
          rel: "noopener noreferrer",
          target: "_blank",
          text: "Sleeper API",
        }),
        ".",
      ]),
      el("p", {
        text:
          "Play Clock is an independent project. Not affiliated with, endorsed by, " +
          "or sponsored by Sleeper, the NFL, or any NFL team. Analysis is informational " +
          "only — no outcome is guaranteed.",
      }),
      el("p", {}, [
        el("a", { href: "docs.html", text: "For agents" }),
        " · ",
        el("a", { href: apiUrl("/docs"), text: "OpenAPI" }),
        " · ",
        el("a", { href: apiUrl("/llms.txt"), text: "llms.txt" }),
      ]),
      ...(apiOverridesAllowed() ? [buildDevDrawer()] : []),
    ]),
  ]);
}

/**
 * Collapsed developer drawer: point the site at a different API origin and
 * flip the mock-payment provider. Deliberately in the footer rather than a
 * query string only, so a tester can find it without reading the README.
 */
function buildDevDrawer() {
  const baseInput = el("input", {
    type: "url",
    id: "dev-api-base",
    placeholder: "Blank = production default or same origin locally",
    value: getApiBase(),
    spellcheck: "false",
  });
  const mockToggle = el("input", { type: "checkbox", id: "dev-mock" });
  mockToggle.checked = isMockMode();

  const save = el("button", {
    class: "btn btn-sm flex-none",
    type: "button",
    text: "Apply & reload",
    on: {
      click: () => {
        setApiBase(baseInput.value);
        setMockMode(mockToggle.checked);
        const url = new URL(window.location.href);
        url.searchParams.delete("api");
        url.searchParams.delete("mock");
        window.location.replace(url.toString());
      },
    },
  });

  return el("details", { class: "dev-bar" }, [
    el("summary", { text: "Developer settings" }),
    el("div", { class: "inline-row" }, [
      el("label", { class: "field-label flex-none", for: "dev-api-base", text: "API base URL" }),
      baseInput,
    ]),
    el("div", { class: "inline-row" }, [
      el("label", { class: "field-label flex-none", for: "dev-mock" }, [
        mockToggle,
        " Use mock payments (X402_MODE=mock backends only)",
      ]),
      save,
    ]),
  ]);
}

/**
 * Point every `data-api-href` anchor at the API origin.
 *
 * `/openapi.json`, `/llms.txt`, `/v1/catalog` and the Swagger UI at `/docs` are
 * served by the **API**, not by this site. They were written as same-origin
 * absolute paths back when one service served both, and they have 404ed ever
 * since the hosts split — on the one page whose whole audience is agent
 * developers looking for exactly those files.
 *
 * The real path stays in the attribute, so `docs.html` is still a valid
 * document to read on its own, and the resolved origin follows the dev drawer:
 * a tester pointed at localhost gets localhost links.
 */
export function resolveApiLinks(root = document) {
  for (const anchor of root.querySelectorAll("[data-api-href]")) {
    anchor.href = apiUrl(anchor.dataset.apiHref);
  }
}

/**
 * Fill the nav/footer placeholders and raise the TEST MODE banner when the
 * mock payment provider is active. Safe to call on every page.
 */
export function mountChrome() {
  const nav = $('[data-chrome="nav"]');
  if (nav) replace(nav, buildNav());

  const footer = $('[data-chrome="footer"]');
  if (footer) replace(footer, buildFooter());

  const banner = $('[data-chrome="mode-banner"]');
  if (banner) {
    if (isMockMode()) {
      banner.hidden = false;
      banner.textContent =
        "TEST MODE — payments are mocked (no USDC moves). Turn off in Developer settings.";
    } else {
      banner.hidden = true;
    }
  }

  const year = $('[data-chrome="year"]');
  if (year) year.textContent = String(new Date().getFullYear());

  resolveApiLinks();
}
