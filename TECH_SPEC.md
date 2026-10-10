# Tech Spec — Play Clock
**x402 pay-per-analysis fantasy football API on GCP + ADK**

Companion to PRD.md. Written to be executed by Claude Code. Stack: Python 3.12, FastAPI, Google ADK (Python), Cloud Run, Firestore, Cloud Scheduler, Vertex AI (Gemini Flash), Algorand x402 via GoPlausible facilitator.

---

## 1. Architecture

```
                        ┌─────────────────────────────────────────────┐
                        │                Cloud Run: api                │
 Human (web UI) ──────► │  FastAPI                                     │
 AI agent (Bazaar) ───► │  ├─ x402 middleware (402 → verify → settle) │
                        │  ├─ /v1/* route handlers                     │
                        │  ├─ ADK pipeline (in-process)                │
                        │  │   orchestrator → [stats, research] →     │
                        │  │   synthesis                               │
                        │  └─ data access layer (Firestore cache +     │
                        │      Sleeper/ESPN live calls)                │
                        └───────┬───────────────────────┬─────────────┘
                                │                       │
                    Firestore (cache,          Vertex AI Gemini
                    players, weekly stats,     (agents + grounded
                    payment receipts)          Google Search)
                                ▲
                                │ nightly / weekly writes
                        ┌───────┴───────────────┐
                        │ Cloud Run Job: ingest  │◄── Cloud Scheduler
                        │ nflreadpy → Firestore  │    (cron)
                        │ Sleeper players sync   │
                        └────────────────────────┘

 Static web UI: separate Cloud Run service (or Firebase Hosting), calls the API.
 x402 payments: client pays USDC on Algorand MainNet → GoPlausible facilitator
 verifies/settles → activity auto-tracked on challenge leaderboard.
```

**Two Cloud Run services + one Cloud Run Job:**
1. `api` — FastAPI + ADK, min-instances=1 during season (cold starts kill agent latency), concurrency=8, 2 vCPU / 2 GiB
2. `web` — static/SSR frontend (keep dumb; all logic in `api`)
3. `ingest` — Cloud Run Job triggered by Cloud Scheduler (not a service; batch semantics)

## 2. Repo layout

```
playclock/
├── api/
│   ├── main.py                  # FastAPI app, middleware wiring
│   ├── x402/
│   │   ├── middleware.py        # 402 challenge/verify/settle flow
│   │   └── receipts.py          # payment receipt logging → Firestore
│   ├── routes/
│   │   ├── free.py              # /v1/trending/preview, /v1/catalog, /v1/health
│   │   └── paid.py              # trending, sleepers, player, matchup, roster, waivers
│   ├── agents/
│   │   ├── pipeline.py          # ADK SequentialAgent wiring per endpoint
│   │   ├── stats_agent.py       # tools over Firestore stats
│   │   ├── research_agent.py    # google_search-grounded LlmAgent
│   │   ├── synthesis_agent.py   # verdict writer, structured output
│   │   └── schemas.py           # Pydantic response contracts
│   ├── data/
│   │   ├── sleeper.py           # Sleeper API client (httpx, cached)
│   │   ├── espn.py              # ESPN unofficial client (feature-flagged)
│   │   ├── stats_store.py       # Firestore read models
│   │   └── cache.py             # response cache (week-scoped endpoints)
│   └── evals/golden_queries.py  # 20 golden Q&A checks, run pre-deploy
├── ingest/
│   ├── job.py                   # entrypoint, task router
│   ├── nflverse_ingest.py       # nflreadpy pulls → Firestore
│   └── sleeper_players.py       # nightly players dump sync
├── web/                         # minimal UI (see §8)
├── infra/                       # terraform or gcloud scripts, scheduler defs
├── tests/
└── pyproject.toml               # uv-managed
```

## 3. x402 payment layer (the challenge-critical part)

**Requirements from the challenge:** MainNet HTTPS endpoint; payments verify & settle via the **GoPlausible facilitator**; **Bazaar discovery enabled**; **`x402-global-challenge` tag**; at least one real MainNet USDC payment confirmed; entry type **Composite** (all endpoints share one `payTo` address).

Flow per paid request:
1. Request arrives without payment → respond `402 Payment Required` with x402 payment-requirements payload: `payTo` (our Algorand address), asset = USDC (ASA), amount (per-endpoint price table), facilitator URL (GoPlausible), and the challenge tag in metadata.
2. Client retries with `X-PAYMENT` header (signed payment payload).
3. Middleware calls facilitator **verify**; on success, executes the handler; calls **settle**; returns result + `X-PAYMENT-RESPONSE`.
4. Log receipt `{txid, payer, endpoint, amount, ts}` to Firestore `receipts/` — this is our own analytics + the "proof of who is paying" the submission asks for.

Implementation notes for Claude Code:
- Use the official Algorand x402 SDK/middleware if available for Python/FastAPI (check the Algorand x402 developer guide at algorand.co/agentic-commerce/x402/developers and GoPlausible docs at build time — this ecosystem moves fast; do NOT hand-roll settlement if a maintained middleware exists). Hand-rolling only the thin FastAPI adapter around facilitator verify/settle calls is acceptable.
- Build against **TestNet** first (test USDC), behind `X402_NETWORK` env var; flip to MainNet for launch. Both configs in `infra/`.
- Idempotency: remember each signed payment transaction for 300s, bound to the
  request fingerprint (method, path, sorted query, and body hash), so an identical
  retry cannot double-settle and the payment cannot buy a different request.
- Free endpoints bypass middleware entirely (route-level allowlist, not path-prefix magic).

## 4. Data layer

### 4.1 Sleeper API (free, no key, read-only)
Base: `https://api.sleeper.app/v1`
- `GET /players/nfl` — full player DB (~5MB). **Fetch once nightly** in `ingest`, store to Firestore `players/{player_id}` + a name→id search index. Never call live.
- `GET /players/nfl/trending/add?lookback_hours=24&limit=25` and `.../trending/drop` — poll every 30 min via scheduler, cache in `trending/{add|drop}`.
- `GET /user/{username}` → `GET /user/{user_id}/leagues/nfl/2026` → `GET /league/{league_id}/rosters` — called live on `/v1/roster` and `/v1/team-report` requests (user-triggered, low volume).
- `GET /league/{league_id}/matchups/{week}` — per-week matchups including per-player points and starters vs. bench. Pull weeks 1..N live for `/v1/team-report`; powers lineup-efficiency math (optimal vs. actual lineup, bench points lost), luck analysis, and leaguemate comparisons. `GET /league/{league_id}` gives scoring/roster settings needed to compute "optimal lineup" correctly per league.
- **Derived: league free-agent pool** — all rostered player_ids across the league's rosters, subtracted from the player universe = who's actually available. Deficiency suggestions in `/v1/team-report` must only recommend players in this pool.
- Sleeper also exposes undocumented projections endpoints (`api.sleeper.app/projections/nfl/...`); treat as optional enhancement — verify shape at build time, feature-flag it, never depend on it.
- Discipline: stay far under ~1000 req/min; single shared httpx client with retry/backoff.

### 4.2 nflverse via nflreadpy (free, CC-BY 4.0 — add attribution to responses/footer)
Runs only in `ingest` (Polars-based; keep out of request path):
- `load_player_stats([2025, 2026])` — weekly stat lines → Firestore `weekly_stats/{season}_{week}/{player}`
- `load_snap_counts`, `load_depth_charts`, `load_injuries`, `load_schedules`
- Derived tables computed at ingest: `usage_trends/{player}` (L4W snap%, target share, rz touches, trend deltas) and `def_vs_pos/{team}` (fantasy points allowed by position, season-to-date)
- Schedule: Tue + Thu + Sat mornings ET (stats finalize after MNF; injury reports land Wed–Fri)
- **Name/ID mapping:** nflverse `gsis_id` ≠ Sleeper `player_id`. Sleeper's player objects include cross-IDs (`gsis_id`/`espn_id` fields); build `id_map/` at ingest and treat unmapped players as a logged warning, not a crash.

### 4.3 ESPN unofficial endpoints (supplemental, feature-flagged `ENABLE_ESPN`)
- `site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard`, `.../news` — schedule context and headlines. Unofficial and can break/block at any time: 5s timeout, on any failure return empty and continue. No hard dependency anywhere.

### 4.4 Internet research
- ADK `research_agent` = `LlmAgent` (Gemini Flash) with the built-in `google_search` grounding tool. Prompted to find last-72h injury/depth-chart/beat-writer news for the specific players in scope and return structured findings with source URLs. This is the "internet research" feature — no scraping, no extra API keys.

### 4.5 Explicitly not used in v1
- The Odds API (free tier no longer covers NFL; Business tier $99/mo has NFL props in season) — v1.1 candidate.
- FantasyPros / paywalled rankings — licensing risk, excluded.

## 5. ADK agent design

One `SequentialAgent` pipeline per paid endpoint, assembled from shared sub-agents; run in-process in the `api` service (simplest ops; Agent Engine is an option later, but one Cloud Run service keeps the x402 middleware and agents in one deployable).

```
pipeline(request):
  1. stats_agent (LlmAgent + function tools):
       tools: get_player_stats(player_ids, weeks), get_usage_trends(player_ids),
              get_def_vs_pos(team, position), get_trending(kind),
              get_schedule(team, week), resolve_player(name)  # all read Firestore
  2. research_agent (LlmAgent + google_search):
       input: resolved player list + week; output: structured news findings w/ sources
  3. synthesis_agent (LlmAgent, temperature low):
       input: session state from 1+2; output: response contract JSON
       (verdict, confidence: high|medium|low, reasoning, stats_cited[], sources[],
        generated_at, data_freshness)
```

Rules:
- **Output schema enforced** via ADK structured output / Pydantic; a malformed synthesis is retried once, then 500 (never bill... note: payment settles pre-handler, so on handler failure return the error AND log for manual refund policy — keep a `failed_paid_calls/` collection; cheap goodwill).
- stats_agent may only cite numbers returned by tools (prompt + eval-enforced) — no LLM-recalled stats, ever. This is the #1 quality risk.
- Model: `gemini-2.5-flash` class via Vertex AI (`GOOGLE_GENAI_USE_VERTEXAI=1`); pin exact model id at build time — verify current Vertex model names then.
- Per-endpoint token budgets; target < $0.02 LLM cost per paid call (leaves margin at $0.10 price floor).
- `adk eval` golden set: 20 queries with expected-property assertions (correct player resolved, no uncited stats, valid schema). Run in CI before deploy.

## 6. Caching & freshness

| Data | Refresh | Serve from |
|---|---|---|
| Player DB, ID map | Nightly | Firestore |
| Weekly stats, usage, def-vs-pos | Tue/Thu/Sat ingest | Firestore |
| Trending add/drop | 30 min | Firestore |
| `/v1/trending` response | Generated on first paid call of the cycle, cached 6h, re-served to subsequent payers | `response_cache/` |
| `/v1/sleepers`, `/v1/waivers`, `/v1/report` | Generated on first paid call of the cycle, cached 12h (was 6h for the first two until 2026-09-22; DESIGN_NOTES §26) | `response_cache/` |
| `/v1/player`, `/v1/matchup`, `/v1/roster`, `/v1/team-report` | Always fresh (personalized) | — |

**Team-report computation rule:** lineup efficiency, bench points lost, positional splits, and leaguemate rankings are computed deterministically in Python (a `team_analytics.py` module in `data/`) from Sleeper matchup history — the LLM narrates these numbers, it does not produce them. Behavioral observations beyond the computable stats must be labeled as observations in the response, not presented as statistics.

Cached week-scoped responses keep unit economics excellent: 100 payers of `/v1/sleepers` ≈ 1 LLM run.

## 7. GCP setup

- Project: new, dedicated. Region `us-east4` (nearest, and Vertex-supported).
- Services: Cloud Run (api, web), Cloud Run Jobs (ingest), Cloud Scheduler, Firestore (Native), Secret Manager (Algorand mnemonic for payTo account ops if needed, any keys), Artifact Registry, Cloud Logging/Monitoring.
- Deploy: `gcloud run deploy` from source or Dockerfile; CI via GitHub Actions (test → eval → deploy).
- Budget alert at $50/mo. Expected steady-state: Cloud Run ~$15–30 (min-instance), Vertex usage-based, Firestore pennies, total well under $75/mo at target volume.
- Monitoring: log-based metrics on `receipts` writes (paid calls/day — mirrors leaderboard), 402→paid conversion rate, p95 latency, agent failure rate. Alert on ingest job failure (stale data silently degrades quality).

## 8. Web UI (minimal by design)

Static SPA (React/Vite or plain HTML+JS) on Cloud Run/Firebase Hosting:
- Pages: Home (free trending preview + sample analysis), Analyze (endpoint picker → wallet pay → result), Roster (Sleeper username flow), Docs (OpenAPI + llms.txt + "for agents" quickstart)
- Wallet: Pera/Defly via `@txnlab/use-wallet` (or current standard — verify at build); client constructs x402 payment from the 402 payload, retries with `X-PAYMENT`
- Result cards rendered client-side with a "share card" PNG export (watermark + URL)

## 9. Build milestones (Claude Code work plan)

| # | Milestone | Definition of done |
|---|---|---|
| 1 | Skeleton + data layer | ingest job populates Firestore from nflreadpy + Sleeper; ID map built; unit tests |
| 2 | Free endpoints | `/v1/trending/preview`, `/v1/catalog` live locally; Sleeper trending flowing |
| 3 | ADK pipeline | `/v1/player` end-to-end locally (no payments); golden evals passing |
| 4 | x402 on TestNet | 402 → pay (test USDC) → verify → settle → result; receipts logged |
| 5 | All six paid endpoints | Shared pipeline + per-endpoint prompts; response cache working |
| 6 | Web UI + wallet flow | Pay-and-reveal works on TestNet in browser |
| 7 | MainNet launch | Deployed, Bazaar-listed, challenge tag set, first real USDC payment confirmed, entry submitted |
| 8 | Hardening | Monitoring, budget alerts, eval-gated CI, share cards |

## 10. Config

```
X402_NETWORK=testnet|mainnet     X402_PAY_TO=<algorand address>
X402_FACILITATOR_URL=<goplausible> X402_ASSET_ID=<USDC ASA id per network>
GOOGLE_CLOUD_PROJECT / LOCATION   GOOGLE_GENAI_USE_VERTEXAI=1
MODEL_ID=<pin at build>           ENABLE_ESPN=true
PRICE_TRENDING=0.10 ... (per-endpoint, env-tunable for October experiments)
```

## 11. Things Claude Code must verify at build time (fast-moving)
1. Current Algorand x402 Python middleware/SDK name + GoPlausible facilitator URL and Bazaar registration steps (algorand.co x402 dev guide)
2. USDC ASA IDs (TestNet vs MainNet)
3. Current ADK version + `google_search` tool import path (ADK 2.x had breaking changes)
4. Vertex AI current Gemini Flash model id
5. Sleeper player object cross-ID field names for the ID map
