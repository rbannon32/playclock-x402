/**
 * The endpoint model the UI builds forms from.
 *
 * Two sources, merged at runtime:
 *
 *  - **`GET /v1/catalog` (authoritative)** supplies path, method, price and
 *    description. Prices are never hardcoded in a page; the catalog is the
 *    single source of truth so an October price experiment changes the site
 *    with no redeploy.
 *  - **`FORM_SPECS` (this file)** supplies the input fields, because the catalog
 *    entry names a request *schema* but does not carry its shape. The field
 *    lists below mirror `api/x402/endpoints.py`'s `input_schema` blocks exactly.
 *
 * `FALLBACK_CATALOG` keeps the site presentable when the API is unreachable:
 * copy and prices from PRD §4.2 / `api/core/config.py` defaults, clearly marked
 * stale in the UI.
 */
import { expectedPayment } from "./config.js";
import { formSpecFor, humanize } from "./forms-from-openapi.js";

/**
 * Display titles. The catalog carries a sentence, not a name, and a key an
 * endpoint ships under is not one either — anything missing here is humanised
 * from its key rather than shown raw, so a new endpoint reads as "Draft board"
 * instead of `draft_board`.
 */
const TITLES = {
  trending: "Trending board",
  sleepers: "Weekly sleepers",
  player: "Player deep dive",
  matchup: "Start / sit",
  roster: "Roster audit",
  waivers: "Waiver big board",
  report: "Weekly briefing",
  team_report: "Team report",
  draft_board: "Draft board",
  draft_report: "Draft grade",
};

/** Preferred display order. Anything not listed follows, in catalog order. */
export const ENDPOINT_ORDER = [
  "trending",
  "sleepers",
  "player",
  "matchup",
  "roster",
  "waivers",
  "report",
  "team_report",
  "draft_board",
  "draft_report",
];

const WEEK_FIELD = {
  name: "week",
  label: "NFL week",
  type: "number",
  min: 1,
  max: 18,
  placeholder: "current week",
  hint: "Leave blank for the current week.",
};

/**
 * Hand-written overrides, for the inputs that are real widgets rather than a
 * labelled text box: the two-to-four player repeater and the pasted roster.
 * Everything not listed here is derived from `/openapi.json`.
 *
 * Input fields per endpoint key.
 *
 * `in`: "query" for GET endpoints, "body" for POST endpoints.
 * `type`: "number" | "text" | "players" (the 2–4 player repeater).
 */
export const FORM_SPECS = {
  trending: {
    in: "query",
    fields: [
      {
        name: "lookback_hours",
        label: "Lookback window (hours)",
        type: "number",
        min: 1,
        max: 168,
        placeholder: "24",
        hint: "How far back Sleeper add/drop counts are summed. Defaults to 24.",
      },
    ],
  },
  sleepers: { in: "query", fields: [WEEK_FIELD] },
  player: {
    in: "body",
    fields: [
      {
        name: "name",
        label: "Player",
        type: "text",
        required: true,
        placeholder: "Bijan Robinson",
        hint: "Full name. Resolved against the Sleeper player index.",
      },
      WEEK_FIELD,
    ],
  },
  matchup: {
    in: "body",
    fields: [
      {
        name: "players",
        label: "Players to compare",
        type: "players",
        min: 2,
        max: 4,
        required: true,
        hint: "Two to four names. The verdict ranks them best-to-worst start.",
      },
      WEEK_FIELD,
    ],
  },
  roster: {
    in: "body",
    fields: [
      {
        name: "sleeper_username",
        label: "Sleeper username",
        type: "text",
        required: true,
        placeholder: "your_sleeper_handle",
        hint: "Public read-only lookup. Your roster is never stored on our servers.",
      },
      {
        name: "league_id",
        label: "League ID",
        type: "text",
        placeholder: "optional",
        hint: "Leave blank to use your first NFL league this season.",
      },
      WEEK_FIELD,
    ],
  },
  waivers: { in: "query", fields: [WEEK_FIELD] },
  report: { in: "query", fields: [WEEK_FIELD] },
  team_report: {
    in: "body",
    fields: [
      {
        name: "sleeper_username",
        label: "Sleeper username",
        type: "text",
        required: true,
        placeholder: "your_sleeper_handle",
        hint: "Graded against your actual leaguemates.",
      },
      {
        name: "league_id",
        label: "League ID",
        type: "text",
        placeholder: "optional",
        hint: "Leave blank to use your first NFL league this season.",
      },
      WEEK_FIELD,
    ],
  },
};

/**
 * Offline copy of the catalog. Prices track `api/core/config.py` defaults; the
 * live catalog always wins when it can be fetched.
 */
export const FALLBACK_CATALOG = Object.freeze({
  service: "Play Clock",
  version: "unknown",
  network: "mainnet",
  pay_to: "",
  asset_id: 0,
  facilitator_url: "https://facilitator.goplausible.xyz",
  challenge_tag: "x402-global-challenge",
  stale: true,
  endpoints: [
    {
      key: "trending",
      path: "/v1/trending",
      method: "GET",
      price_usdc: 0.1,
      free: false,
      description:
        "Top 25 Sleeper trending adds and drops with per-player stat context, " +
        "why-it's-happening analysis, and an add/fade/hold verdict on each.",
      response_schema: "TrendingResponse",
      request_schema: null,
    },
    {
      key: "sleepers",
      path: "/v1/sleepers",
      method: "GET",
      price_usdc: 0.2,
      free: false,
      description:
        "Eight to twelve weekly sleeper picks with usage trends (snap share, target " +
        "share, red-zone touches), matchup reasoning and confidence tiers.",
      response_schema: "SleepersResponse",
      request_schema: null,
    },
    {
      key: "player",
      path: "/v1/player",
      method: "POST",
      price_usdc: 0.1,
      free: false,
      description:
        "Deep dive on one player: last-four-week stat trends, usage trajectory, " +
        "schedule difficulty, fresh injury and beat-writer news, and a verdict.",
      response_schema: "PlayerResponse",
      request_schema: "PlayerRequest",
    },
    {
      key: "matchup",
      path: "/v1/matchup",
      method: "POST",
      price_usdc: 0.2,
      free: false,
      description:
        "Start/sit call across two to four players: head-to-head stat comparison, " +
        "opponent defense versus position, weather and injury news, ranked.",
      response_schema: "MatchupResponse",
      request_schema: "MatchupRequest",
    },
    {
      key: "roster",
      path: "/v1/roster",
      method: "POST",
      price_usdc: 0.35,
      free: false,
      description:
        "Full roster audit: positional grades, this week's start/sit calls, drop " +
        "candidates, and the top waiver adds available in your league.",
      response_schema: "RosterResponse",
      request_schema: "RosterRequest",
    },
    {
      key: "waivers",
      path: "/v1/waivers",
      method: "GET",
      price_usdc: 0.2,
      free: false,
      description:
        "Waiver wire big board: ranked FAB and priority targets with a rostership " +
        "proxy from Sleeper trending, plus stash-versus-start labels.",
      response_schema: "WaiversResponse",
      request_schema: null,
    },
    {
      key: "report",
      path: "/v1/report",
      method: "GET",
      price_usdc: 0.35,
      free: false,
      description:
        "League-wide weekly briefing: emerging players before consensus, injury " +
        "fallout chains and handcuffs, stock up/down, rookie watch and streamers.",
      response_schema: "ReportResponse",
      request_schema: null,
    },
    {
      key: "team_report",
      path: "/v1/team-report",
      method: "POST",
      price_usdc: 0.5,
      free: false,
      description:
        "Team-aware deep report: positional strength graded against your actual " +
        "leaguemates, deficiency fixes from your league's free-agent pool, and a " +
        "manager performance review (optimal versus started lineup, luck, efficiency).",
      response_schema: "TeamReportResponse",
      request_schema: "TeamReportRequest",
    },
    {
      key: "draft_board",
      path: "/v1/draft-board",
      method: "GET",
      price_usdc: 0.2,
      free: false,
      cache_ttl_seconds: 43200,
      description:
        "A tiered board of 200 players ranked against where the market drafts " +
        "them, with prior-season usage behind every ranking and the values and " +
        "reaches called out.",
      response_schema: "DraftBoardResponse",
      request_schema: null,
    },
    {
      key: "draft_report",
      path: "/v1/draft-report",
      method: "POST",
      price_usdc: 0.5,
      free: false,
      description:
        "Grade one manager's completed draft: every pick scored against the " +
        "market rank it was taken at, positional balance, best and worst picks, " +
        "and what to do before Week 1.",
      response_schema: "DraftReportResponse",
      request_schema: "DraftReportRequest",
    },
  ],
});

/** Human title for an endpoint key, humanising anything not named here. */
export function titleFor(key) {
  return TITLES[key] || humanize(key);
}

/**
 * Normalise a catalog body (live or fallback) into the paid-endpoint list the
 * UI renders, in `ENDPOINT_ORDER`, with the form spec attached.
 *
 * @param {object|null} catalog
 * @returns {Array<object>}
 */
export function paidEndpoints(catalog, openapi = null) {
  const source = catalog && Array.isArray(catalog.endpoints) ? catalog : FALLBACK_CATALOG;
  const byKey = new Map();
  for (const entry of source.endpoints) {
    if (entry.free || !entry.key) continue;
    byKey.set(entry.key, entry);
  }
  const ordered = [];
  const seen = new Set();
  for (const key of ENDPOINT_ORDER) {
    const entry = byKey.get(key);
    if (!entry) continue;
    seen.add(key);
    ordered.push(decorate(entry, openapi, source));
  }
  // Anything the server advertises that this build has never heard of still
  // renders, and — given an OpenAPI document — with a working form.
  for (const [key, entry] of byKey) {
    if (!seen.has(key)) ordered.push(decorate(entry, openapi, source));
  }
  return ordered;
}

/**
 * Attach a title and a form spec to one catalog entry.
 *
 * `FORM_SPECS` wins where it exists, because three inputs are real widgets
 * rather than labelled text boxes — the two-to-four player repeater, the pasted
 * roster — and a derived field would render the wrong control. Everything else
 * is derived from `/openapi.json`, so a new endpoint arrives with the bounds
 * the server will actually enforce instead of a second copy of them.
 */
function decorate(entry, openapi, catalog) {
  const derived = openapi ? formSpecFor(openapi, entry) : null;
  const form =
    FORM_SPECS[entry.key] ||
    derived || { in: entry.method === "GET" ? "query" : "body", fields: [] };
  return { ...entry, title: titleFor(entry.key), form, payment: expectedPayment(entry, catalog) };
}

/**
 * Cheapest and dearest paid price in a catalog.
 *
 * Headline copy ("$0.10–$0.50") is a price claim like any other, so it is
 * derived from the same catalog the price table renders instead of being typed
 * into the markup, where it goes stale the first time a price moves.
 *
 * @param {object|null} catalog
 * @returns {{min: number, max: number}|null} null when nothing is priced
 */
export function priceRange(catalog) {
  const prices = paidEndpoints(catalog)
    .map((entry) => Number(entry.price_usdc))
    .filter((value) => Number.isFinite(value));
  if (prices.length === 0) return null;
  return { min: Math.min(...prices), max: Math.max(...prices) };
}

/** Look up one decorated endpoint by key from a catalog body. */
export function endpointByKey(catalog, key, openapi = null) {
  return paidEndpoints(catalog, openapi).find((entry) => entry.key === key) || null;
}
