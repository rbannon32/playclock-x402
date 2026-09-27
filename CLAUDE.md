# CLAUDE.md — Play Clock

Pay-per-analysis NFL fantasy football API, x402-gated with USDC micropayments on
Algorand. Built for the Algorand Foundation **Global x402 Challenge**.

Ten paid endpoints priced $0.10–$0.50 in USDC, sold one analysis at a time to
humans *and* AI agents. The competition scores on real MainNet payment volume,
so "does a stranger's agent actually pay for this" is the product requirement,
not a nice-to-have.

## Read before you change things

| Doc | What it holds | Read it when |
|---|---|---|
| `PRD.md` | Product: endpoints, pricing, personas, success metrics | changing what we sell |
| `TECH_SPEC.md` | Architecture, §5 quality bar, §6 caching, §10 config | changing how it is built |
| **`DESIGN_NOTES.md`** | **Binding judgement calls + verified ecosystem facts** | **before touching payment, model, or data code** |
| `TODO.md` | Ranked launch checklist, dated, with owners | starting a session |
| `infra/deploy.md` | gcloud runbook; §7 is the MainNet cutover checklist | deploying |

`DESIGN_NOTES.md` is the one that saves time. Most of it is things that were
discovered the expensive way against live systems (Vertex model routing,
facilitator behaviour, nflverse table shapes) and cannot be re-derived from the
code. Append to it when you learn something the code cannot tell the next reader.

## Commands

```bash
uv sync --all-extras                                # Python 3.12 via uv; adds ingest + mcp deps
uv run pytest -q                                    # full suite (1,100+ tests) — hermetic: no network, no GCP creds
uv run pytest tests/test_routes_paid.py -q -k cache # one file / one pattern
uv run ruff check . && uv run ruff format .         # lint + format (CI enforces both)
uv run python -m api.evals.run_evals --engine deterministic   # 23 golden queries + value gate (CI gate)
uv run python -m api.evals.run_evals --engine adk    # real-model evals; needs Vertex creds
X402_MODE=mock uv run uvicorn api.main:app --port 8080        # local API with fake payments
uv run python examples/agent/fantasy_agent.py --base-url http://localhost:8080 --mock
uv run python examples/agent/selftest.py            # example agent end to end, in-process (CI gate)
python -m ingest.job --task nightly|stats|backtest|trending|precompute|all   # ingest; chains: --task stats,backtest
python -m ingest.job --task stats --season 2025 --stats-only          # rebuild a prior season's rollups, nothing else
python -m ingest.job --task precompute --force --only draft_board      # re-warm one board, not all five
uv run python infra/optin.py status --network testnet|mainnet      # wallet USDC opt-in state
PLAYCLOCK_MOCK=1 uv run python -m playclock_mcp     # MCP server over stdio (see playclock_mcp/README.md)
cd web && npm ci && npm test && npm run build       # wallet layer: test, then bundle to web/dist/
```

CI (`.github/workflows/ci.yml`) runs exactly these; if it passes locally it
passes there. There are no secrets and no network in CI by design.

## Architecture

FastAPI app (`api/main.py`) with free routes (`api/routes/free.py`) and ten
paid routes (`api/routes/paid.py`) gated by an x402 route dependency. Analysis
comes from an `AnalysisEngine` (`api/agents/`): the deterministic engine
(CI/dev/fallback), the **narrated** engine (deterministic body, one tool-free
Gemini call for the prose, a grounding guard in between), or the full ADK
pipeline, selected by `ENGINE`. Data is
written by `ingest/` (nflverse + Sleeper → Firestore) and read through
`api/data/stats_store.py`. All persistence goes through the `Store` seam
(`api/core/store.py`): `MemoryStore` locally, Firestore in Cloud Run. The
web UI lives in `web/`: plain ES modules served as-is, except the wallet layer
(`web/js/wallet/`), which is bundled with esbuild because Pera, Defly and
algosdk cannot be vendored honestly.

### The paid request lifecycle

```
client GET /v1/trending
  └─ require_payment("trending") dependency
       ├─ no PAYMENT-SIGNATURE  → 402 + payment requirements + bazaar discovery block
       ├─ replay of a payment already seen (same signed txn + same request fingerprint)
       │                        → cached receipt, handler re-runs, nothing settles again
       ├─ same payment, DIFFERENT request → 402 (a second purchase, refused)
       └─ verify via GoPlausible facilitator
            └─ require_ingested_data → 503 "you were not charged" if datasets missing
                 └─ handler runs (cache hit, or engine generates)
                      ├─ raises   → non-2xx, never settles, client not charged
                      └─ returns  → settle → PAYMENT-RESPONSE receipt header
                                     └─ settle fails → still return 200, log failed_paid_calls/
```

The ordering is the refund posture: **verify → handler → settle.** Nothing is
charged for a failed answer, and the one asymmetric case (settle fails after a
good answer) costs us an LLM call rather than costing the caller money.

### Layout

| Path | Purpose |
|---|---|
| `api/main.py` | App factory, CORS, OpenAPI metadata, `/llms.txt` |
| `api/routes/free.py` | `/v1/health`, `/v1/catalog`, `/v1/trending/preview`, `/v1/stats` |
| `api/routes/paid.py` | The ten gated endpoints, cache policy, `REQUIRED_DATASETS` |
| `api/routes/__init__.py` | `API_VERSION`, `CACHE_TTL_SECONDS`, week bounds |
| `api/core/config.py` | `Settings` + `get_settings()`; price table, `ENDPOINT_KEYS` |
| `api/core/store.py` | Async `Store` seam — `MemoryStore`, `FirestoreStore` |
| `api/core/week.py` | Week resolution from the ingested schedule (Tue 03:00 ET rollover) |
| `api/schemas.py` | Pydantic request/response contracts — the surface agents read |
| `api/x402/` | 402 challenge, verify/settle, receipts, Bazaar discovery, `paid_router()` |
| `api/agents/` | `AnalysisEngine` seam: ADK pipeline, deterministic engine, prompts, tools |
| `api/evals/` | 23 golden queries (`golden.py`, honesty) + the value gate (`quality.py`, worth paying for) |
| `api/data/` | Sleeper + ESPN clients, Firestore read models, response cache, team math, `predictions.py`, `sources.py` |
| `ingest/` | Cloud Run Job: `nightly`, `stats`, `backtest`, `trending`, `precompute`; `judge.py` |
| `examples/agent/` | Runnable paying client: discover → 402 → sign → pay → receipt |
| `playclock_mcp/` | MCP server — the API as paid tools for an AI assistant |
| `web/` | UI. Plain ES modules, plus a bundled wallet layer (`js/wallet/`) |
| `infra/` | `deploy.md` runbook, `env.example`, `cloudbuild.yaml`, `optin.py` |

## The seams (change behavior via env, not code)

| Env | Values | Meaning |
|---|---|---|
| `STORE_BACKEND` | `memory` / `firestore` | persistence backend |
| `ENGINE` | `deterministic` / `narrated` / `adk` | analysis engine |
| `NARRATOR_TIMEOUT_SECONDS` | float | `narrated` only: prose rewrite budget before serving the body as is |
| `MODEL_THINKING_LEVEL` | `low` / unset | Gemini thinking cap. **Leave unset** — see below |
| `QUALITY_JUDGE` | `true` / `false` | ingest job only: score every warmed board with one Gemini call |
| `X402_MODE` | `disabled` / `mock` / `live` | payment gate (`mock` accepts `PAYMENT-SIGNATURE: mock-paid`) |
| `X402_NETWORK` | `testnet` / `mainnet` | Algorand network + USDC ASA selection |
| `ENGINE_FALLBACK` | `true` / `false` | answer from the deterministic engine when ADK fails |
| `RESEARCH_ENDPOINTS` | keys / `none` | which endpoints run the `google_search` agent |
| `FREE_RATE_LIMIT_PER_MINUTE` | int, `0` off | per-client cap on the free routes |

Full list with docs: `infra/env.example` (43 variables, every one defaulted or commented out).
Test hooks: `set_store()`, `set_engine()`, `set_facilitator()`, `set_clock()`
— inject fakes, never monkeypatch internals.

Endpoint keys and default prices (USDC), in catalog order:

| key | path | price | cached |
|---|---|---|---|
| `trending` | `GET /v1/trending` | 0.10 | 6h |
| `sleepers` | `GET /v1/sleepers` | 0.20 | 12h |
| `player` | `POST /v1/player` | 0.10 | never |
| `matchup` | `POST /v1/matchup` | 0.20 | never |
| `roster` | `POST /v1/roster` | 0.35 | never |
| `waivers` | `GET /v1/waivers` | 0.20 | 12h |
| `report` | `GET /v1/report` | 0.35 | 12h |
| `team_report` | `POST /v1/team-report` | 0.50 | never |
| `draft_board` | `GET /v1/draft-board` | 0.20 | 12h (week 0) |
| `draft_report` | `POST /v1/draft-report` | 0.50 | never |

## Rules that are not obvious from the code

### Quality
- **Trust is the floor; edge is the product.** An answer that never lies but
  says nothing the free preview does not is a failed answer that passed every
  test — which is exactly what shipped on 2026-09-03 (DESIGN_NOTES §24). Every
  paid answer leads with the one call that disagrees with the crowd or the
  market and the number behind it: the fade, the buy-low, the biggest value,
  the move to make. `api/evals/quality.py` is the gate: no code artifacts in
  prose, every named player carries a real id, boards cite more than add
  counts, at least one call disagrees with the market, sources are readable,
  and in a model-written body every number in prose is a body leaf or a
  citation.
  It runs in the golden suite AND on every board precompute warms (the only
  place the ADK output payers actually receive is inspected), recording
  `quality/{key}` and logging `board quality flagged`.
- **The engine never apologises in prose.** `meta.model` being null is the
  disclosure that the deterministic engine answered; the reasoning leads with
  the answer. A dict literal or a raw field name in a customer-facing string
  is a test failure.
- **The draft board is computed, not synthesized.** Asked for 200 rows the
  ADK synthesizer wrote 30 with deltas that did not add up, so precompute
  warms `draft_board` through the narrated engine (`WarmTarget.engine`): the
  deterministic engine ranks the pool and computes every delta, the model
  writes labels, notes and reasoning under the grounding guard. `meta.engine`
  says which engine wrote a body; the gate's number-grounding rule applies to
  `adk` bodies only.
- **Boards narrate a data-chosen list; the model does not pick it.** Left to
  choose, the synthesizer put the most-added players in "emerging" and
  "sleepers" — the one set those sections exist to exclude. The deterministic
  scorer's `candidates()` is injected as `request.candidates` and the prompts
  say to draw from it only.
- **Every verdict is scored later.** `api/data/predictions.py` archives each
  start/sit, add/fade, sleeper, waiver and emerging call (first write wins,
  per player per endpoint per week, whichever way the call points);
  `ingest --task backtest` scores them against the week's actual points once
  nflverse publishes; `/v1/stats` publishes the hit rate. It is the only
  measurement of value, and the honest social proof for the finals pitch.
- **Never let an LLM cite a number that didn't come from a tool.** Enforced by
  prompts and evals (`api/evals/`); it is the #1 quality risk (TECH_SPEC §5).
  The narrated engine closes the same door in Python: a rewritten sentence
  containing a number not present in the body, or a name not in it, is
  rejected and the template text kept.
- A paid answer that is empty is worse than no answer. Paid routes 503
  ("You were not charged") when the datasets they read are missing —
  per endpoint, via `REQUIRED_DATASETS`, not a blanket freshness check.
- **A paid answer about the wrong season is worse still**, because nothing else
  reports it: the data is present, the freshness markers are stamped and
  `missing_datasets` is empty. `stale_season()` compares the ingested schedule's
  season against `SEASON` and 503s when they differ; `ingest/precompute.py`
  refuses (and exits non-zero) for the same reason, since a warmed board is
  served for its whole TTL.
- When the ADK pipeline fails, `FallbackAnalysisEngine` answers from the
  deterministic engine rather than 500ing (`ENGINE_FALLBACK=false` to opt out).
  A 500 settles nothing while a fallback answer is billed, so this is a real
  trade — it is defensible only because the fallback is grounded in ingested
  stats and self-identifies (`meta.model` is null). **Alert on the fallback
  rate**: one degraded answer is fine, every answer degrading is an outage
  wearing a disguise.

### Payments
- **Paid handlers never touch payments.** Return the model; raise on failure. A
  non-2xx means the payment never settles — that's the refund posture.
- Paid routes must be registered on `paid_router()`. A bare `APIRouter` would
  still 402 (the dependency raises) but would never settle — the API looks
  healthy while the leaderboard stays empty.
- **Every paid request needs a fresh payment payload.** Replays are bound to
  the SHA-256 of the **signed payment transaction** (`payment_identity`, not the
  raw header, so a re-encoded header is the same payment, and not the endpoint,
  so one payment cannot buy two same-priced routes) *and* a fingerprint of the
  request (method + path + key-sorted query + body hash). Same request inside
  the window = free retry; different request = 402.
- The settled payload carries **our** `resource` and `extensions`, never the
  client's echo: the facilitator catalogues whatever `resource.url` it is
  handed, permanently.
- **A payment must outlive the slowest handler.** Settle runs after the
  handler, so a transfer signed with `lastValid` a few rounds ahead verifies,
  lapses, fails to settle, and the answer ships free. `api/x402/validity.py`
  402s (`payment_expires_too_soon`) anything with fewer than 200 rounds left,
  reading the round from algod `/v2/status`; it fails open if algod is down.
- A replay re-runs the handler (only verify and settle are skipped). If that
  re-run fails, the error never says "you were not charged" — it was.
  Each settled payment serves at most `MAX_REPLAYS` (5) replays, counted by
  compare-and-swap on its record; past that it 402s `payment_replay_limit_reached`,
  or one payment buys unlimited generations.
- The idempotency TTL is **300s**, and the clock starts at *verify*, before the
  handler runs. It must stay above the slowest handler or a client that times
  out and retries gets charged twice.
- Clients must **persist `PAYMENT-SIGNATURE` until they hold the response**.
  Replay is the only way to recover a paid-but-timed-out call.
- The x402 SDK (`x402-avm`, import name `x402`) is confined to `api/x402/`.

### Challenge constraints (these are one-way doors)
- In live mode the facilitator **must** be GoPlausible
  (`https://facilitator.goplausible.xyz`). The SDK's default is `x402.org`, and
  using it silently forfeits all leaderboard attribution.
- `accepts[].extra.tag = "x402-global-challenge"` is what earns attribution.
- The Bazaar catalogues the **first settled payment's resource URL,
  permanently**. Never settle a challenge-tagged MainNet payment against
  localhost, a preview revision, or a host you don't intend to keep.
- **Cataloguing is per endpoint, not per merchant.** The row key is
  `METHOD + origin+pathname`, so each of the ten needs its own settle to appear
  (DESIGN_NOTES §25). The facilitator reads the discovery block off the
  *client's echoed payload*, validates it against the schema we ship beside it,
  and on failure logs to its own stderr and settles anyway — success and silent
  delisting look identical from here. `tests/test_x402_bazaar_discovery.py`
  runs that exact gate in-process; keep it passing or the listing is a guess.
- Every 402 carries **two** extensions: `bazaar` (discovery) and `x402-merchant`
  (name, website, logo, categories). Without the second, the leaderboard scrapes
  the origin's `<title>` and `/apple-touch-icon.png` and guesses; `categories`
  has no fallback. Both must be echoed back by the client unmodified.
- **Repeated self-payments are explicitly disqualifiable** (Official Rules §14:
  "artificial volume, wash transactions, repeated self-payments"). One
  validation settle is required; a self-payment loop to pump volume is not a
  growth tactic, it is a disqualification.

### ADK / Vertex
- `MODEL_ID=gemini-3.7-flash` resolves **only** at `GOOGLE_CLOUD_LOCATION=global`.
  Pairing it with a regional location 404s on every paid call.
- Latency and timeouts are coupled to money: settlement happens before the
  handler returns, so a timeout below real latency charges the caller for an
  answer the proxy then cuts off. Keep
  `Cloud Run --timeout` > `accepts[].maxTimeoutSeconds` > real p95.
- Stats and research agents run in a `ParallelAgent` (they share no state keys);
  synthesis runs after both. Don't re-serialize them.

### The web UI's wallet layer (`web/js/wallet/`)
- Only this directory is bundled; everything else under `web/` is served as
  written. `payment.js` loads the bundle with a **dynamic import**, so the docs
  pages, the free preview and the whole mock-mode demo never download 1.5MB —
  and local development needs no `npm run build` at all.
- The two money-critical files stay **out** of the bundle and under `node --test`:
  `exact-avm.js` (what we sign) and `envelope.js` (what we send).
- Amounts are `BigInt`. `Number` silently rounds above 2^53, and this is money.
- The envelope must echo `accepted` **and** `extensions` unmodified. Dropping
  `extensions` settles the payment and never catalogues the endpoint, because
  the Bazaar discovery block rides in-band on it.
- Play Clock's 402 offers no `feePayer`, so the group is one transaction with no
  group id — which is why no SHA-512/256 (absent from Web Crypto) is needed. The
  sponsored two-transaction path is implemented but has never been seen live.

### Serving and cost
- `GET /` serves **HTML**, and `/apple-touch-icon.png` resolves. Both live in the
  API image because the origin the Bazaar catalogues is the one that served the
  402. The leaderboard reads the merchant name from that page's title and falls
  back to a truncated payTo address; `Accept: application/json` still gets the
  old pointers.
- `cached_analysis` is single-flight per cache key (`_generate_once`): ten
  callers on an expired board share one generation, not ten. Per process, so
  ten Cloud Run instances can still make ten — warming covers the rest.
- Free routes can be rate limited per client (`FREE_RATE_LIMIT_PER_MINUTE`); paid
  routes are not, because payment is their limit. **Production runs it at `0`
  (off)**: under `ENV=prod` a positive limit is refused without
  `TRUSTED_PROXY_HOPS`, because the client address cannot be trusted without a
  verified proxy topology (`infra/deploy.md`).
- `/v1/stats` is free and built from `receipts/`. It publishes counts, never a
  payer address.

### The MCP server (`playclock_mcp/`)
- It is a **client**: it pays, it never serves. The api image never imports it.
- **Tools are derived from `/v1/catalog` + `/openapi.json` at startup**, never
  hard-coded. A new paid endpoint becomes a tool with its real schema and price
  the moment it ships — which is how the draft pair arrived with no MCP change.
- Every payment is journalled to disk **before** the paid request is sent
  (`~/.playclock/pending-payments.json`, mode 0600) and cleared only once a
  response is in hand. Settlement precedes the handler, so a timeout has already
  moved USDC; the journal is what makes it recoverable. Entries are keyed by a
  **SHA-256 of the whole header** — x402 envelopes share a long base64 prefix,
  so a truncated key silently merges two pending payments.
- A failure **after** the payment header is sent raises `PaymentInFlight`, never
  `ApiUnreachable`. The latter tells the agent nothing was charged and retrying
  is safe; past that line both may be false, and a retry buys the answer twice.
- Never `print` — stdout is the MCP transport. Log to stderr.
- It deliberately shares no code with `examples/agent/`: that file is a teaching
  artifact, this is a product surface with different obligations.

### Draft endpoints
- **`market_rank` is Sleeper's `search_rank` — draft *popularity*, not a
  consensus ADP.** Never call it ADP in a response; prompts, evals and route
  tests all enforce this. Saying "this is not an ADP" in the reasoning is
  required; using the term as a claim is a test failure.
- Board `value_delta` compares our rank to the market's ordering **of the same
  board**. Comparing against the global `search_rank` would call every player on
  a truncated board a value.
- Report `value_delta` is `pick_no - market_rank`: **positive means value**
  (taken later than the market drafts them). The inverse reads backwards and was
  the first version's bug.
- **`GET /draft/{id}/picks` returns every team's picks, in one flat list.**
  `/v1/draft-report` grades ONE roster, so it resolves an identity first —
  `draft_slot`, else the username's `picked_by` (falling back to the draft's
  `draft_order` when autopicks leave it empty) — and 400s on a multi-team draft
  with no identity rather than grading a roster nobody owns.
- Board and report callouts filter by a signed threshold **before** truncating.
  Slicing a sorted list alone pads "values" with unmoved players.
- `/v1/draft-board` caches under **week 0** — it is season-scoped, and drafts run
  all week. `WarmTarget.fixed_week` exists for exactly this.

### Ingest and data
- **Firestore doc shapes are specified in `api/data/stats_store.py`'s module
  docstring.** Ingest writes to match; change shapes there first, then both sides.
- Doc ids for stats are **Sleeper player ids** (gsis is a field); the `nightly`
  task must run before `stats` on a cold store.
- The four league-wide boards (`trending`, `sleepers`, `waivers`, `report`) are
  **precomputed by `ingest/precompute.py`**, not generated in the request.
  Measured cold generation: `report` 988s, `sleepers` 375s, `waivers` 117s —
  all beyond any timeout a caller tolerates. Warming is load-bearing, not an
  optimization.
- Warming invariant: **`interval + slowest board < refresh window < shortest TTL`**
  (today: 2h + 988s < 2.5h < 6h). Break it and a board expires with nothing
  reporting it, and the next caller pays for a 375s generation.
- **The warming cadence is the bill.** With a 2h loop and a 2.5h window a 6h
  board regenerates every 4h and a 12h board every 10h, at ~9 Vertex calls a
  board; that loop was two thirds of the project's spend for fourteen sales
  (DESIGN_NOTES §26). Only `trending` is a 6h board now. Do not "renew"
  unchanged entries by comparing `REQUIRED_DATASETS` markers: that is the
  readiness table, not the dependency table — `sleepers` and `report` read
  `trending` without listing it, and under ADK every board's stats agent can
  read every dataset. The lever for cost is the TTL, which `/v1/catalog`
  advertises, so it is a product decision.
- **Vertex retries live in one place**, `api/agents/vertex.py`, and every
  client carries them (`MODEL_RETRY_ATTEMPTS`; the ingest job runs 6). The
  SDK retries nothing by default, and a 429 that fails a nine-call board
  makes the outer retries buy the finished calls again.
- **Do not cap thinking to save money.** It is 78% of the output SKU and
  capping it looked free on every cheap proxy, then took the golden suite from
  21/23 to 11/23 — almost all of it ungrounded numbers, plus a team report that
  invented metrics it had not been given. Thinking is what makes the model
  track which numbers came from a tool (DESIGN_NOTES §27). `MODEL_THINKING_LEVEL`
  exists so this can be re-measured, not so it can be turned on.
- **Do not upgrade to `gemini-3.8-flash`.** Same price, 3.5x slower; `report`
  would exceed the ingest job's 45m task timeout (§27).
- **If a value must match a computed one, copy it — do not ask for it.**
  `enforce_computed_facts` overwrites `team_report`'s `manager_review` and
  `positional_strength_vs_league` from `request.team_analytics`, keeping the
  model's `observations`. Those paths are then exempt from the gate's number
  rule (`_COMPUTED_PROSE`) because they are grounded by construction. The draft
  board's rule, applied to the other endpoint handed a finished table.
- **One ADK eval run is not a quality measurement.** Identical code scored 21
  and 19 of 23 the same evening, the swings all in the ungrounded-number class.
- Warming refuses to run on missing datasets and **exits non-zero** if any board
  is left cold. A cached empty board is served as a `cache=hit` for the whole
  TTL, bypassing the 503 — it bills every caller for nothing.
- Never compute the season from the calendar year: `nflreadpy.get_current_season()`
  lags until September. `load_schedules()` with no args pulls all history.
- **Never run `--task stats --season <last year>` on the live store without
  `--stats-only`.** The plain task writes that season's schedule first, and the
  season guard then 503s every paid route (DESIGN_NOTES §20); it also clears
  the preseason-gap marker and overwrites this week's injuries and depth
  charts. `--stats-only` rebuilds weekly stats, usage trends and def-vs-pos
  and touches nothing that describes the current season.
- **`backtest` runs in the same invocation as `stats`** (`--task stats,backtest`),
  never on its own schedule, and scores a week only when the `weekly_stats`
  freshness marker post-dates the week's last kickoff. Scoring is permanent;
  a half-written week graded once is graded wrong forever.
- `ESPNClient` never raises — empty results are a normal outcome, not an error.

### Conventions
- Endpoint keys use underscores (`team_report`), URL paths use hyphens
  (`/v1/team-report`); the key↔path table lives in `api/x402/endpoints.py`.
- Cache TTLs are route policy (`api/routes/__init__.py`) and are the single
  source of truth — `/v1/catalog` advertises those exact numbers.
- `pyproject.toml` pins `google-adk >=2.8,<3` deliberately (2.x import surface
  verified; SequentialAgent deprecation noted in DESIGN_NOTES §13).

## Testing conventions

The suite is hermetic: no network, no GCP credentials, no clock dependence.
Keep it that way — CI has no secrets.

- Inject fakes through `set_store()` / `set_engine()` / `set_facilitator()`.
  Never monkeypatch module internals.
- HTTP clients take an injected `httpx` transport; third-party APIs are stubbed
  with `respx`.
- Construct `Settings(...)` directly and pass it in rather than mutating env —
  every seam takes `settings` as a parameter instead of reaching for the global.
- Anything requiring real credentials (ADK evals, live facilitator) must be
  skipped by default, not marked slow.

## Deploy and current state

`infra/deploy.md` is the runbook (Cloud Run api + web, ingest job, four
schedulers, budget alerts); its §7 is the MainNet launch checklist. `Dockerfile`
(api, slim) and `Dockerfile.ingest` (with polars/nflreadpy). Build via
`infra/cloudbuild.yaml` — **not** `gcloud builds submit --tag`, which uses the
legacy non-BuildKit builder and dies on the `RUN --mount=type=cache` layers.

Live today: `https://api.playclock.xyz` (project `playclock`, region
`us-east4`), **MainNet since 2026-09-01** (`X402_MODE=live`,
`X402_NETWORK=mainnet`). Since 2026-09-03 the api service runs
`ENGINE=narrated` (`NARRATOR_TIMEOUT_SECONDS=45`, `RESEARCH_ENDPOINTS=none`)
and the ingest job runs `ENGINE=adk` with `QUALITY_JUDGE=true`, precomputing
the five boards through the value gate. Current image tag and revision:
`infra/deploy.md` §1c.

Wallet mnemonics live only in Secret Manager; addresses are public and in
`infra/deploy.md` §1b. The API service signs nothing and needs no mnemonic.
