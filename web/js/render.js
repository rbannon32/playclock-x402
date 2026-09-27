/**
 * Result rendering. One function per response contract in `api/schemas.py`.
 *
 * Every paid body extends `AnalysisResponse`, so the shared blocks — verdict,
 * confidence, reasoning, `stats_cited`, `sources`, `meta` — render identically
 * for all ten endpoints, and `renderAnalysis()` switches on the endpoint key
 * only for the extra fields each contract adds on top.
 *
 * Defensive by policy: a missing or misshapen section renders as nothing (or an
 * empty state), never as a thrown error. Someone has paid for this response;
 * a schema surprise must not cost them the parts that did arrive.
 */

import { BRAND } from "./config.js";
import {
  append,
  badge,
  el,
  emptyState,
  formatCount,
  formatShare,
  formatTimestamp,
  formatUsdc,
} from "./dom.js";
import { titleFor } from "./endpoints.js";

const CONFIDENCE_VARIANTS = { high: "high", medium: "medium", low: "low" };

function list(value) {
  return Array.isArray(value) ? value : [];
}

function text(value, fallback = "") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

/** "RB · ATL" style secondary line, skipping the parts that are missing. */
function playerMeta(item) {
  return [item.position, item.team, item.opponent ? `vs ${item.opponent}` : null]
    .filter(Boolean)
    .join(" · ");
}

/* --------------------------------------------------------- shared blocks */

/** Verdict banner: the headline call plus the confidence badge. */
export function renderVerdict(data) {
  const confidence = text(data.confidence, "medium");
  return el("div", { class: "verdict" }, [
    el("div", { class: "verdict-top" }, [
      el("span", { class: "badge", text: "Verdict" }),
      badge(`${confidence} confidence`, CONFIDENCE_VARIANTS[confidence] || null),
    ]),
    el("p", { class: "verdict-text", text: text(data.verdict, "No verdict returned.") }),
  ]);
}

/** The long-prose field. Preserved as written (whitespace kept by CSS). */
export function renderReasoning(data) {
  if (!data.reasoning) return null;
  return el("section", { class: "result-block" }, [
    el("h3", { text: "Reasoning" }),
    el("p", { class: "reasoning", text: data.reasoning }),
  ]);
}

/**
 * `stats_cited[]` — every hard number the reasoning leans on, with its source.
 *
 * Collapsed by default: a roster body cites a couple of thousand rows, and the
 * reasoning above it is the answer someone paid for. The count in the summary
 * says what is there; opened, the table scrolls inside its own frame (CSS)
 * rather than pushing the sources and receipt off the bottom of the page.
 */
export function renderStats(data) {
  const stats = list(data.stats_cited);
  if (stats.length === 0) return null;
  return el("section", { class: "result-block stats-cited" }, [
    el("details", {}, [
      el("summary", {}, [el("h3", { text: `Stats cited (${stats.length})` })]),
      el("div", { class: "table-scroll" }, [
        el("table", {}, [
          el("thead", {}, [
            el("tr", {}, [
              el("th", { scope: "col", text: "Stat" }),
              el("th", { scope: "col", class: "num", text: "Value" }),
              el("th", { scope: "col", text: "Player" }),
              el("th", { scope: "col", text: "Source" }),
            ]),
          ]),
          el(
            "tbody",
            {},
            stats.map((stat) =>
              el("tr", {}, [
                el("td", { text: text(stat.stat, "—") }),
                el("td", { class: "num", text: text(stat.value, "—") }),
                el("td", { text: text(stat.player, "—") }),
                el("td", { text: text(stat.source, "—") }),
              ]),
            ),
          ),
        ]),
      ]),
    ]),
  ]);
}

/** `sources[]` — the grounded research citations. */
/**
 * The URL when it is http(s), else null. Sources are model-written, and a
 * `javascript:` href would run on the origin that holds the wallet session.
 */
export function safeHref(url) {
  if (typeof url !== "string" || !url) return null;
  try {
    const { protocol } = new URL(url);
    return protocol === "https:" || protocol === "http:" ? url : null;
  } catch {
    return null;
  }
}

export function renderSources(data) {
  const sources = list(data.sources);
  if (sources.length === 0) return null;
  return el("section", { class: "result-block" }, [
    el("h3", { text: `Sources (${sources.length})` }),
    el(
      "ul",
      { class: "source-list" },
      sources.map((source) =>
        el("li", {}, [
          safeHref(source.url)
            ? el("a", {
                href: source.url,
                rel: "noopener noreferrer nofollow",
                target: "_blank",
                text: text(source.title, source.url),
              })
            : el("span", { text: text(source.title, "Untitled source") }),
          source.published ? el("div", { class: "source-date", text: source.published }) : null,
        ]),
      ),
    ),
  ]);
}

/** Provenance footer: when, from what data, by which model, cache state. */
export function renderMeta(data) {
  const meta = data.meta || {};
  const freshness = meta.data_freshness && typeof meta.data_freshness === "object"
    ? Object.entries(meta.data_freshness)
    : [];

  const rows = [
    ["Generated", formatTimestamp(meta.generated_at)],
    meta.model ? ["Model", meta.model] : null,
    meta.cache ? ["Cache", cacheLabel(meta.cache)] : null,
    ...freshness.map(([dataset, asOf]) => [`Data · ${dataset}`, formatTimestamp(asOf)]),
  ].filter(Boolean);

  return el("footer", { class: "meta-footer" }, [
    el(
      "dl",
      {},
      rows.flatMap(([term, value]) => [
        el("dt", { text: term }),
        el("dd", { text: value }),
      ]),
    ),
    meta.attribution ? el("p", { text: meta.attribution }) : null,
  ]);
}

function cacheLabel(state) {
  if (state === "hit") return "hit — served from cache";
  if (state === "fresh") return "fresh — generated now and cached";
  if (state === "miss") return "miss — generated now, not cached";
  return String(state);
}

/** Settlement receipt from the `PAYMENT-RESPONSE` header. */
export function renderReceipt(receipt, { paid }) {
  if (!paid) {
    return el("p", {
      class: "receipt",
      text: "This server has payments disabled — no USDC was charged for this answer.",
    });
  }
  if (!receipt) {
    return el("p", {
      class: "receipt",
      text: "Paid. The server sent no settlement receipt header for this call.",
    });
  }
  if (!receipt.success) {
    return el("p", { class: "receipt" }, [
      "Answer delivered, but settlement reported a failure",
      receipt.errorReason ? ` (${receipt.errorReason})` : "",
      ". No USDC moved — you were not charged.",
    ]);
  }
  return el("p", { class: "receipt" }, [
    el("strong", { text: "Payment settled. " }),
    receipt.network ? `Network ${receipt.network}. ` : "",
    receipt.transaction ? ["Txid ", el("code", { text: receipt.transaction }), ". "] : "",
    receipt.payer ? ["Payer ", el("code", { text: receipt.payer }), "."] : "",
  ]);
}

/* ------------------------------------------------------ per-endpoint body */

function statBlock(title, children, { emptyMessage } = {}) {
  const body = Array.isArray(children) ? children.filter(Boolean) : [children].filter(Boolean);
  if (body.length === 0 && !emptyMessage) return null;
  return el("section", { class: "result-block" }, [
    el("h3", { text: title }),
    body.length ? body : emptyState(emptyMessage),
  ]);
}

function noteList(items) {
  if (list(items).length === 0) return null;
  return el(
    "ul",
    { class: "note-list" },
    list(items).map((item) =>
      el("li", {}, [
        el("div", {}, [
          el("span", { class: "note-name", text: text(item.name, "Unknown player") }),
          " ",
          el("span", { class: "note-meta", text: playerMeta(item) }),
        ]),
        el("div", { text: text(item.note || item.reason || item.rationale, "") }),
      ]),
    ),
  );
}

function table(columns, rows) {
  return el("div", { class: "table-scroll" }, [
    el("table", {}, [
      el("thead", {}, [
        el(
          "tr",
          {},
          columns.map((col) =>
            el("th", { scope: "col", class: col.numeric ? "num" : null, text: col.label }),
          ),
        ),
      ]),
      el(
        "tbody",
        {},
        rows.map((row) =>
          el(
            "tr",
            {},
            columns.map((col) => {
              const value = col.get(row);
              return el(
                "td",
                { class: col.numeric ? "num" : null },
                value instanceof Node ? [value] : [text(value, "—")],
              );
            }),
          ),
        ),
      ),
    ]),
  ]);
}

/** GET /v1/trending -> TrendingResponse */
function renderTrendingBody(data) {
  const players = list(data.players);
  if (players.length === 0) return null;
  return statBlock(
    `Board (${players.length}) · ${text(data.lookback_hours, 24)}h lookback`,
    el(
      "div",
      { class: "grid" },
      players.map((player, index) =>
        el("article", { class: "trend-card", dataset: { trend: text(player.trend, "add") } }, [
          el("div", { class: "trend-rank", text: String(index + 1) }),
          el("div", {}, [
            el("div", { class: "trend-name" }, [
              text(player.name, "Unknown"),
              " ",
              badge(text(player.verdict, "hold"), player.verdict === "add" ? "high" : null),
            ]),
            el("div", { class: "trend-meta", text: playerMeta(player) }),
            el("p", { style: "margin:.4rem 0 0", text: text(player.analysis, "") }),
          ]),
          el("div", { class: "trend-count" }, [
            formatCount(player.trend_count),
            el("small", { text: text(player.trend, "") + "s" }),
          ]),
        ]),
      ),
    ),
  );
}

/** GET /v1/sleepers -> SleepersResponse */
function renderSleepersBody(data) {
  const picks = list(data.picks);
  if (picks.length === 0) return null;
  return statBlock(
    `Picks (${picks.length}) · week ${text(data.week, "?")}`,
    el(
      "div",
      { class: "grid" },
      picks.map((pick) =>
        el("article", { class: "card" }, [
          el("h3", {}, [
            text(pick.name, "Unknown"),
            " ",
            badge(text(pick.confidence, "medium"), CONFIDENCE_VARIANTS[pick.confidence] || null),
          ]),
          el("div", { class: "trend-meta", text: playerMeta(pick) }),
          el("p", { style: "margin:.5rem 0 .3rem" }, [
            el("strong", { text: "Usage: " }),
            text(pick.usage_note, "—"),
          ]),
          el("p", { style: "margin:0 0 .3rem" }, [
            el("strong", { text: "Matchup: " }),
            text(pick.matchup_note, "—"),
          ]),
          el("p", { style: "margin:0", text: text(pick.rationale, "") }),
        ]),
      ),
    ),
  );
}

/** POST /v1/player -> PlayerResponse */
function renderPlayerBody(data) {
  const player = data.player || {};
  const weeks = list(player.recent_weeks);
  return [
    statBlock(
      `${text(player.name, "Player")} · week ${text(data.week, "?")}`,
      el("div", { class: "card" }, [
        el("div", { class: "trend-meta", text: playerMeta(player) }),
        player.status ? el("p", { style: "margin:.4rem 0 0" }, [badge(player.status, "medium")]) : null,
        player.usage_trajectory
          ? el("p", { style: "margin:.6rem 0 .2rem" }, [
              el("strong", { text: "Usage trajectory: " }),
              player.usage_trajectory,
            ])
          : null,
        player.schedule_outlook
          ? el("p", { style: "margin:0" }, [
              el("strong", { text: "Schedule: " }),
              player.schedule_outlook,
            ])
          : null,
      ]),
    ),
    weeks.length
      ? statBlock(
          "Last four weeks",
          table(
            [
              { label: "Wk", get: (row) => row.week, numeric: true },
              { label: "Opp", get: (row) => row.opponent },
              { label: "FP", get: (row) => fixed(row.fantasy_points, 1), numeric: true },
              { label: "Snap%", get: (row) => share(row.snap_pct), numeric: true },
              { label: "Tgt", get: (row) => row.targets, numeric: true },
              { label: "Tgt share", get: (row) => share(row.target_share), numeric: true },
              { label: "Car", get: (row) => row.carries, numeric: true },
              { label: "RZ", get: (row) => row.rz_touches, numeric: true },
            ],
            weeks,
          ),
        )
      : null,
  ];
}

function fixed(value, digits) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : "—";
}

function share(value) {
  return value === null || value === undefined ? "—" : formatShare(value);
}

/** POST /v1/matchup -> MatchupResponse */
function renderMatchupBody(data) {
  const ranked = list(data.ranked);
  if (ranked.length === 0) return null;
  return statBlock(
    `Ranking · week ${text(data.week, "?")}`,
    el(
      "div",
      { class: "grid" },
      ranked.map((entry) =>
        el("article", { class: "trend-card", dataset: { trend: entry.call === "sit" ? "drop" : "add" } }, [
          el("div", { class: "trend-rank", text: `#${text(entry.rank, "?")}` }),
          el("div", {}, [
            el("div", { class: "trend-name" }, [
              text(entry.name, "Unknown"),
              " ",
              badge(text(entry.call, "—"), entry.call === "start" ? "high" : null),
            ]),
            el("div", { class: "trend-meta", text: playerMeta(entry) }),
            el("p", { style: "margin:.4rem 0 0", text: text(entry.projection_note, "") }),
            entry.def_vs_pos_rank
              ? el("div", {
                  class: "trend-meta",
                  text: `Opponent ranks #${entry.def_vs_pos_rank} in fantasy points allowed to this position (1 = worst defense).`,
                })
              : null,
          ]),
        ]),
      ),
    ),
  );
}

/** POST /v1/roster -> RosterResponse */
function renderRosterBody(data) {
  return [
    list(data.positional_grades).length
      ? statBlock(
          "Positional grades",
          table(
            [
              { label: "Position", get: (row) => row.position },
              { label: "Grade", get: (row) => row.grade },
              { label: "Note", get: (row) => row.note },
            ],
            list(data.positional_grades),
          ),
        )
      : null,
    list(data.start_sit).length
      ? statBlock(
          "Start / sit this week",
          table(
            [
              { label: "Player", get: (row) => row.name },
              { label: "Pos", get: (row) => row.position },
              { label: "Call", get: (row) => badge(text(row.call, "—"), row.call === "start" ? "high" : null) },
              { label: "Why", get: (row) => row.reason },
            ],
            list(data.start_sit),
          ),
        )
      : null,
    list(data.drop_candidates).length
      ? statBlock(
          "Drop candidates",
          table(
            [
              { label: "Player", get: (row) => row.name },
              { label: "Pos", get: (row) => row.position },
              {
                label: "Risk",
                get: (row) => badge(text(row.risk, "—"), CONFIDENCE_VARIANTS[row.risk] || null),
              },
              { label: "Why", get: (row) => row.reason },
            ],
            list(data.drop_candidates),
          ),
        )
      : null,
    list(data.waiver_adds).length
      ? statBlock(
          "Waiver adds for this roster",
          table(
            [
              { label: "#", get: (row) => row.priority, numeric: true },
              { label: "Player", get: (row) => row.name },
              { label: "Pos", get: (row) => row.position },
              { label: "Team", get: (row) => row.team },
              { label: "Why", get: (row) => row.reason },
            ],
            list(data.waiver_adds),
          ),
        )
      : null,
  ];
}

/** GET /v1/waivers -> WaiversResponse */
function renderWaiversBody(data) {
  const board = list(data.board);
  if (board.length === 0) return null;
  return statBlock(
    `Big board · week ${text(data.week, "?")}`,
    table(
      [
        { label: "#", get: (row) => row.rank, numeric: true },
        { label: "Player", get: (row) => row.name },
        { label: "Pos", get: (row) => row.position },
        { label: "Team", get: (row) => row.team },
        { label: "Adds", get: (row) => (row.trend_count == null ? "—" : formatCount(row.trend_count)), numeric: true },
        { label: "Role", get: (row) => badge(text(row.stash_or_start, "—")) },
        { label: "FAB", get: (row) => `${fixed(row.fab_bid_pct, 1)}%`, numeric: true },
        { label: "Why", get: (row) => row.rationale },
      ],
      board,
    ),
  );
}

/** GET /v1/report -> ReportResponse */
function renderReportBody(data) {
  const injury = list(data.injury_fallout);
  return [
    statBlock("Emerging before consensus", noteList(data.emerging)),
    injury.length
      ? statBlock(
          "Injury fallout",
          el(
            "div",
            { class: "grid" },
            injury.map((item) =>
              el("article", { class: "card" }, [
                el("h3", {}, [
                  text(item.injured_player, "Unknown"),
                  " ",
                  item.team ? el("span", { class: "note-meta", text: item.team }) : null,
                ]),
                item.status ? el("p", { style: "margin:0 0 .5rem" }, [badge(item.status, "low")]) : null,
                noteList(item.beneficiaries) || emptyState("No beneficiaries listed."),
              ]),
            ),
          ),
        )
      : null,
    statBlock("Stock up", noteList(data.stock_up)),
    statBlock("Stock down", noteList(data.stock_down)),
    statBlock("Rookie watch", noteList(data.rookie_watch)),
    statBlock("Streamers", noteList(data.streamers)),
  ];
}

/** POST /v1/team-report -> TeamReportResponse */
function renderTeamReportBody(data) {
  const review = data.manager_review || {};
  const strength = list(data.positional_strength_vs_league);
  const deficiencies = list(data.deficiencies);

  return [
    data.league_name || data.sleeper_username
      ? el("p", { class: "trend-meta" }, [
          text(data.sleeper_username, ""),
          data.league_name ? ` · ${data.league_name}` : "",
          data.week ? ` · week ${data.week}` : "",
        ])
      : null,
    strength.length
      ? statBlock(
          "Positional strength vs. your league",
          table(
            [
              { label: "Position", get: (row) => row.position },
              { label: "Grade", get: (row) => row.grade },
              {
                label: "Rank",
                get: (row) => `${row.league_rank} of ${row.league_size}`,
                numeric: true,
              },
              { label: "PPW", get: (row) => fixed(row.points_per_week, 1), numeric: true },
              {
                label: "League avg",
                get: (row) => fixed(row.league_avg_points_per_week, 1),
                numeric: true,
              },
            ],
            strength,
          ),
        )
      : null,
    deficiencies.length
      ? statBlock(
          "Deficiencies and available fixes",
          el(
            "div",
            { class: "grid" },
            deficiencies.map((item) =>
              el("article", { class: "card" }, [
                el("h3", {}, [
                  text(item.position, "—"),
                  " ",
                  badge(`${text(item.severity, "medium")} severity`, CONFIDENCE_VARIANTS[item.severity] || null),
                ]),
                el("p", { style: "margin:.3rem 0 .6rem", text: text(item.detail, "") }),
                list(item.available_fixes).length
                  ? el(
                      "ul",
                      { class: "note-list" },
                      list(item.available_fixes).map((fix) =>
                        el("li", {}, [
                          el("div", {}, [
                            el("span", { class: "note-name", text: text(fix.name, "Unknown") }),
                            " ",
                            el("span", { class: "note-meta", text: playerMeta(fix) }),
                          ]),
                          el("div", { text: text(fix.why, "") }),
                        ]),
                      ),
                    )
                  : emptyState("No free-agent fix identified in this league."),
              ]),
            ),
          ),
        )
      : null,
    statBlock(
      "Manager review",
      el("div", { class: "card" }, [
        el(
          "dl",
          { class: "meta-footer", style: "margin:0;border:0;padding:0" },
          [
            ["Bench points lost", fixed(review.bench_points_lost, 1)],
            ["Optimal minus actual", fixed(review.optimal_vs_actual, 1)],
            ["Lineup efficiency", `${fixed(review.lineup_efficiency_pct, 1)}%`],
            [
              "Efficiency rank",
              review.efficiency_rank ? `${review.efficiency_rank} of ${review.league_size}` : "—",
            ],
            [
              "Wins (actual / expected)",
              review.actual_wins == null && review.expected_wins == null
                ? "—"
                : `${text(review.actual_wins, "—")} / ${fixed(review.expected_wins, 1)}`,
            ],
          ].flatMap(([term, value]) => [el("dt", { text: term }), el("dd", { text: value })]),
        ),
        review.luck_note ? el("p", { style: "margin:.8rem 0 0", text: review.luck_note }) : null,
        list(review.mis_start_patterns).length
          ? el("div", { style: "margin-top:.8rem" }, [
              el("h4", { style: "margin:0 0 .3rem;font-size:.9rem", text: "Mis-start patterns" }),
              el(
                "ul",
                { class: "note-list" },
                list(review.mis_start_patterns).map((line) => el("li", { text: line })),
              ),
            ])
          : null,
        list(review.observations).length
          ? el("div", { style: "margin-top:.8rem" }, [
              el("h4", {
                style: "margin:0 0 .3rem;font-size:.9rem",
                text: "Observations (not statistics)",
              }),
              el(
                "ul",
                { class: "note-list" },
                list(review.observations).map((line) => el("li", { text: line })),
              ),
            ])
          : null,
      ]),
    ),
  ];
}

/** endpoint key -> extra-fields renderer. */
/** Signed rank movement: "+12" is a value (taken later than the market), "-8" a reach. */
function delta(value) {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n > 0 ? `+${n}` : String(n);
}

const DRAFT_VERDICT_VARIANTS = { value: "high", fair: "medium", reach: "low" };

const BOARD_COLUMNS = [
  { label: "#", get: (row) => row.rank, numeric: true },
  { label: "Player", get: (row) => row.name },
  { label: "Pos", get: (row) => row.position },
  { label: "Team", get: (row) => row.team },
  { label: "Market", get: (row) => row.market_rank, numeric: true },
  { label: "Δ", get: (row) => delta(row.value_delta), numeric: true },
  { label: "Why", get: (row) => row.note },
];

/** GET /v1/draft-board -> DraftBoardResponse */
function renderDraftBoardBody(data) {
  const tiers = list(data.tiers).filter((tier) => list(tier.players).length > 0);
  const total = tiers.reduce((n, tier) => n + list(tier.players).length, 0);
  const values = list(data.values);
  const reaches = list(data.reaches);
  if (tiers.length === 0 && values.length === 0 && reaches.length === 0) return null;
  return [
    el("p", { class: "trend-meta" }, [
      `${text(data.season, "")} season`,
      data.scoring ? ` · ${String(data.scoring).toUpperCase()}` : "",
      ` · ${total} players in ${tiers.length} tier${tiers.length === 1 ? "" : "s"}`,
      " · market rank is Sleeper draft popularity, not an ADP",
    ]),
    values.length ? statBlock(`Values (${values.length}) · taken later than the market does`, table(BOARD_COLUMNS, values)) : null,
    reaches.length ? statBlock(`Reaches (${reaches.length}) · the market drafts them earlier`, table(BOARD_COLUMNS, reaches)) : null,
    ...tiers.map((tier) =>
      statBlock(
        `Tier ${text(tier.tier, "?")} · ${text(tier.label, "")} (${list(tier.players).length})`,
        table(BOARD_COLUMNS, list(tier.players)),
      ),
    ),
  ];
}

/** POST /v1/draft-report -> DraftReportResponse */
function renderDraftReportBody(data) {
  const roster = list(data.roster);
  const balance = list(data.positional_balance);
  const plan = list(data.week_one_plan);
  const pickReview = (items) =>
    list(items).length === 0
      ? null
      : el(
          "ul",
          { class: "note-list" },
          list(items).map((item) =>
            el("li", {}, [
              el("div", {}, [
                el("span", { class: "note-name", text: text(item.name, "Unknown player") }),
                " ",
                el("span", { class: "note-meta", text: `R${text(item.round, "?")} · pick ${text(item.pick_no, "?")} · ${delta(item.value_delta)}` }),
                " ",
                badge(text(item.verdict, "fair"), DRAFT_VERDICT_VARIANTS[item.verdict] || null),
              ]),
              el("div", { text: text(item.note, "") }),
            ]),
          ),
        );
  return [
    el("p", { class: "trend-meta" }, [
      data.grade ? [el("strong", { text: `Draft grade ${data.grade}` }), " · "] : "",
      `${text(data.season, "")} season`,
      data.draft_id ? ` · draft ${data.draft_id}` : "",
      ` · ${roster.length} pick${roster.length === 1 ? "" : "s"}`,
    ]),
    roster.length
      ? statBlock(
          "Your picks, in draft order",
          table(
            [
              { label: "Rd", get: (row) => row.round, numeric: true },
              { label: "Pick", get: (row) => row.pick_no, numeric: true },
              { label: "Player", get: (row) => row.name },
              { label: "Pos", get: (row) => row.position },
              { label: "Team", get: (row) => row.team },
              { label: "Market", get: (row) => row.market_rank, numeric: true },
              { label: "Δ", get: (row) => delta(row.value_delta), numeric: true },
            ],
            roster,
          ),
        )
      : null,
    balance.length
      ? statBlock(
          "Positional balance",
          table(
            [
              { label: "Position", get: (row) => row.position },
              { label: "Grade", get: (row) => row.grade },
              { label: "Note", get: (row) => row.note },
            ],
            balance,
          ),
        )
      : null,
    statBlock("Best picks", pickReview(data.best_picks)),
    statBlock("Worst picks", pickReview(data.worst_picks)),
    plan.length
      ? statBlock(
          "Before week 1",
          el("ol", { class: "note-list" }, plan.map((step) => el("li", { text: text(step, "") }))),
        )
      : null,
  ];
}

const BODY_RENDERERS = {
  trending: renderTrendingBody,
  sleepers: renderSleepersBody,
  player: renderPlayerBody,
  matchup: renderMatchupBody,
  roster: renderRosterBody,
  waivers: renderWaiversBody,
  report: renderReportBody,
  team_report: renderTeamReportBody,
  draft_board: renderDraftBoardBody,
  draft_report: renderDraftReportBody,
};

/**
 * Render a complete paid result: verdict, endpoint-specific body, reasoning,
 * stats, sources, provenance, receipt.
 *
 * @param {string} endpointKey
 * @param {object} data the response body
 * @param {object} [context] `{receipt, paid}` from `callPaidEndpoint`
 * @returns {HTMLElement}
 */
export function renderAnalysis(endpointKey, data, context = {}) {
  const root = el("article", { class: "panel", "aria-live": "polite" });
  if (!data || typeof data !== "object") {
    append(root, emptyState("The API returned an empty body."));
    return root;
  }

  append(root, [
    el("p", { class: "eyebrow", text: titleFor(endpointKey) }),
    renderVerdict(data),
  ]);

  const bodyRenderer = BODY_RENDERERS[endpointKey];
  if (bodyRenderer) {
    try {
      append(root, bodyRenderer(data));
    } catch (error) {
      console.warn("could not render endpoint body", endpointKey, error);
      append(root, emptyState("Part of this response could not be displayed."));
    }
  }

  append(root, [
    renderReasoning(data),
    renderStats(data),
    renderSources(data),
    renderMeta(data),
    renderReceipt(context.receipt || null, { paid: context.paid !== false }),
  ]);
  return root;
}

/* ------------------------------------------------------------ share card */

/**
 * Watermarked plain-text summary of a paid result. It remains available beside
 * the PNG card because league chats are often faster with pasteable text.
 */
export function shareText(endpointKey, data) {
  const lines = [];
  const scope = [];
  if (data.week) scope.push(`Week ${data.week}`);
  if (data.player && data.player.name) scope.push(data.player.name);
  lines.push(`${BRAND.name} — ${titleFor(endpointKey)}${scope.length ? ` (${scope.join(", ")})` : ""}`);
  lines.push("");
  lines.push(`"${text(data.verdict, "No verdict")}"`);
  lines.push(`Confidence: ${text(data.confidence, "unknown")}`);

  const stat = list(data.stats_cited)[0];
  if (stat) {
    const who = stat.player ? `${stat.player} ` : "";
    lines.push(`${who}${text(stat.stat, "stat")}: ${text(stat.value, "—")} (${text(stat.source, "source")})`);
  }

  lines.push("");
  lines.push(BRAND.watermark);
  return lines.join("\n");
}

/**
 * Word-wrap `value` to `maxWidth` under a canvas context, capped at `maxLines`.
 *
 * Every word is wrapped first and the result truncated only if it genuinely
 * overflows: stopping the wrap early instead ends the card on one word plus an
 * ellipsis whenever the remaining words would have fit.
 *
 * @param {{measureText: (text: string) => {width: number}}} context
 * @returns {string[]} at most `maxLines` lines, the last ellipsised on overflow
 */
export function canvasLines(context, value, maxWidth, maxLines) {
  const words = String(value || "").split(/\s+/).filter(Boolean);
  const lines = [];
  let current = "";
  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (context.measureText(candidate).width <= maxWidth || !current) {
      current = candidate;
      continue;
    }
    lines.push(current);
    current = word;
  }
  if (current) lines.push(current);
  if (lines.length <= maxLines) return lines;

  const kept = lines.slice(0, maxLines);
  let last = kept[maxLines - 1];
  while (last && context.measureText(`${last}…`).width > maxWidth) {
    last = last.slice(0, -1).trim();
  }
  kept[maxLines - 1] = `${last}…`;
  return kept;
}

/** Download a self-contained 1200×630 PNG result card. */
export async function downloadShareCard(endpointKey, data) {
  if (typeof document === "undefined") return false;
  const canvas = document.createElement("canvas");
  canvas.width = 1200;
  canvas.height = 630;
  const context = canvas.getContext("2d");
  if (!context) return false;

  const lime = "#b4ff2a";
  const paper = "#f2f4ee";
  const dim = "#9aa196";
  context.fillStyle = "#080908";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.strokeStyle = lime;
  context.lineWidth = 4;
  context.strokeRect(24, 24, canvas.width - 48, canvas.height - 48);

  context.fillStyle = lime;
  context.font = "600 22px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.fillText(`PLAY CLOCK · ${titleFor(endpointKey).toUpperCase()}`, 70, 88);
  context.textAlign = "right";
  context.fillStyle = dim;
  context.fillText(data.week ? `WEEK ${data.week}` : "PAY PER ANSWER", 1130, 88);
  context.textAlign = "left";

  context.fillStyle = paper;
  context.font = "800 58px -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif";
  const verdict = text(data.verdict, "No verdict returned.");
  canvasLines(context, verdict, 1020, 3).forEach((line, index) => {
    context.fillText(line, 70, 185 + index * 70);
  });

  const confidence = text(data.confidence, "unknown").toUpperCase();
  context.fillStyle = lime;
  context.font = "600 20px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.fillText(`${confidence} CONFIDENCE`, 70, 430);

  const stat = list(data.stats_cited)[0];
  if (stat) {
    context.fillStyle = paper;
    context.font = "600 24px ui-monospace, SFMono-Regular, Menlo, monospace";
    const who = stat.player ? `${stat.player} · ` : "";
    context.fillText(`${who}${text(stat.stat, "STAT")}  ${text(stat.value, "—")}`, 70, 486);
    context.fillStyle = dim;
    context.font = "400 17px ui-monospace, SFMono-Regular, Menlo, monospace";
    context.fillText(`SOURCE · ${text(stat.source, "not provided")}`, 70, 520);
  }

  context.fillStyle = dim;
  context.font = "500 18px ui-monospace, SFMono-Regular, Menlo, monospace";
  context.fillText("USDC ON ALGORAND · X402", 70, 568);
  context.textAlign = "right";
  context.fillStyle = paper;
  context.fillText("playclock.xyz", 1130, 568);

  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) return false;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `play-clock-${endpointKey.replace(/[^a-z0-9-]+/gi, "-")}.png`;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
  return true;
}

/* ----------------------------------------------------------- free blocks */

/** Free trending preview rows for the landing page. */
export function renderTrendingPreview(preview) {
  const players = list(preview && preview.players);
  if (players.length === 0) {
    return emptyState("No trending players right now — check back after the next waiver run.");
  }
  return el(
    "div",
    { class: "trending-board" },
    players.map((player, index) =>
      el("article", { class: "trend-card", dataset: { trend: text(player.trend, "add") } }, [
        el("div", { class: "trend-rank", text: String(index + 1).padStart(2, "0") }),
        el("div", {}, [
          el("div", { class: "trend-name", text: text(player.name, "Unknown") }),
          el("div", { class: "trend-meta", text: playerMeta(player) }),
        ]),
        el("div", { class: "trend-count" }, [
          formatCount(player.trend_count),
          el("small", { text: `${text(player.trend, "add")}s` }),
        ]),
      ]),
    ),
  );
}

/** Pricing table built from the catalog — never from hardcoded numbers. */
export function renderPricingTable(endpoints) {
  return el("div", { class: "table-scroll" }, [
    el("table", { class: "pricing" }, [
      el("caption", { text: "One payment, one answer. No subscription, no account." }),
      el("thead", {}, [
        el("tr", {}, [
          el("th", { scope: "col", text: "Analysis" }),
          el("th", { scope: "col", text: "Endpoint" }),
          el("th", { scope: "col", class: "num", text: "USDC" }),
        ]),
      ]),
      el(
        "tbody",
        {},
        endpoints.map((entry) =>
          el("tr", {}, [
            el("td", {}, [
              el("div", { text: entry.title }),
              el("div", { class: "endpoint-desc", text: entry.description }),
            ]),
            el("td", {}, [
              el("code", { text: `${entry.method} ${entry.path}` }),
            ]),
            el("td", { class: "num" }, [
              el("span", { class: "price-tag", text: formatUsdc(entry.price_usdc) }),
            ]),
          ]),
        ),
      ),
    ]),
  ]);
}
