# Play Clock

Pay-per-analysis NFL fantasy football intelligence: an x402-gated API (plus a thin web UI
and an MCP server) that sells one analysis at a time — draft boards, trending players with
context, weekly sleepers, start/sit calls, roster audits — priced in USDC micropayments on
Algorand and purchasable by humans *and* AI agents.

Built for the Algorand Global x402 Challenge. See [PRD.md](PRD.md) for the product,
[TECH_SPEC.md](TECH_SPEC.md) for the architecture, and [DESIGN_NOTES.md](DESIGN_NOTES.md)
for the binding judgement calls made during the build.

**Live on Algorand MainNet since 2026-09-01.**

| | |
|---|---|
| Web UI | <https://playclock.xyz> — connect Pera or Defly, buy one answer |
| API | <https://api.playclock.xyz> — [`/v1/catalog`](https://api.playclock.xyz/v1/catalog), [`/openapi.json`](https://api.playclock.xyz/openapi.json), [`/llms.txt`](https://api.playclock.xyz/llms.txt) |
| Scorecard | [`/v1/stats`](https://api.playclock.xyz/v1/stats) — every paid verdict scored against the week's actual points, free |
| Demo video | <https://www.youtube.com/watch?v=_GVaQr-9rLw> — four and a half minutes: the 402, a wallet payment, an AI agent paying on its own, the on-chain receipt |

## What is for sale

Fantasy advice today sits behind $50–$100 season subscriptions, which no agent can buy and
most casual players will not pay for. x402 makes a single start/sit call, waiver board or
roster audit purchasable in one HTTP request: no account, no API key, no subscription.

| Endpoint | USDC | What comes back |
|---|---|---|
| `GET /v1/trending` | 0.10 | Top Sleeper adds and drops, with the usage behind each move and an add/fade/hold verdict |
| `GET /v1/sleepers` | 0.20 | Eight to twelve low-rostered players to start this week, with the matchup behind each |
| `POST /v1/player` | 0.10 | One player in depth: four weeks of usage, schedule, injury news, a start/sit verdict |
| `POST /v1/matchup` | 0.20 | Two to four players ranked against each other for one week |
| `POST /v1/roster` | 0.35 | A whole roster audited: grades, start/sits, drops, the best adds actually available |
| `GET /v1/waivers` | 0.20 | Ranked waiver targets with a suggested FAB bid and a stash-or-start label |
| `GET /v1/report` | 0.35 | The week in one briefing: emerging players, injury fallout, stock up and down, streamers |
| `POST /v1/team-report` | 0.50 | A roster graded against its real league, with fixes from that league's free agents |
| `GET /v1/draft-board` | 0.20 | 200 players tiered and ranked against where the market drafts them |
| `POST /v1/draft-report` | 0.50 | One manager's finished draft graded pick by pick against the market |

Every paid answer leads with the call that disagrees with the crowd and the number behind
it. `GET /v1/catalog` is the machine-readable version of this table, and each endpoint is
listed in the x402 Bazaar so an agent can find it without reading this page.

## Why an answer can be trusted

- **No invented numbers.** Every body is computed from ingested nflverse and Sleeper data.
  Where a model writes the prose, a grounding guard rejects any sentence that cites a number
  or a name not already in the computed body, and keeps the template text instead.
- **Worth paying for, not just honest.** `api/evals/quality.py` fails an answer that says
  nothing the free preview does not: one with no call against the market, a player without a
  real id, or sources a reader cannot follow. It runs in CI and on every board the ingest job
  warms.
- **Every verdict is scored in public.** Each start/sit, add/fade, sleeper and waiver call is
  archived when it is sold and graded against the week's actual points once nflverse
  publishes them. The hit rate, misses included, is free at
  [`/v1/stats`](https://api.playclock.xyz/v1/stats).
- **A failed answer is never charged.** Payment is verified before the handler runs and
  settled only after it succeeds; if the data an endpoint needs is missing, it answers 503
  "you were not charged".

## Buy one answer in 30 seconds

Ten endpoints, $0.10–$0.50 USDC each, no account, no API key. Ask without paying and the
answer is a 402 that says what it costs, who to pay, and — through the Bazaar discovery
block it carries — what comes back:

```bash
curl -s https://api.playclock.xyz/v1/catalog \
  | jq '.endpoints[] | select(.price_usdc > 0) | {path, price_usdc}'
curl -si https://api.playclock.xyz/v1/trending | head -1                      # HTTP/2 402
curl -s  https://api.playclock.xyz/v1/trending \
  | jq '{quote: (.accepts[0] | {amount, asset, payTo, network}), extensions: (.extensions | keys)}'
```

Pay it with the example agent — a wallet holding a little ALGO and USDC, opted in to ASA
`31566704` ([examples/agent/README.md](examples/agent/README.md) has the setup):

```bash
export ALGORAND_MNEMONIC="word word word ... word"   # 25 words; never committed, never sent to the API
uv run python examples/agent/fantasy_agent.py \
    --base-url https://api.playclock.xyz --ask trending --max-price 0.10
```

The agent reads the catalog, refuses any quote above `--max-price`, signs one USDC
transfer, and prints the settlement receipt beside the analysis. Verification and
settlement run through the GoPlausible facilitator, in the order **verify → handler →
settle**: a failed answer is never charged, and re-sending the same payment within 300 s
returns the cached answer instead of buying it twice. Inside an AI assistant,
[`playclock_mcp/`](playclock_mcp/README.md) does the same thing as tool calls.

## Dev quickstart

Python 3.12, managed with [uv](https://docs.astral.sh/uv/). The version is pinned in
`.python-version`; uv will fetch it if the system python differs.

```bash
uv sync --all-extras   # create .venv and install everything (drop --all-extras to skip ingest deps)
uv run pytest -q       # full suite, hermetic: no network, no GCP credentials
uv run ruff check .    # lint
uv run ruff format .   # format
```

Configuration is entirely environment-driven. Copy `infra/env.example` to `.env` for local
dev — every variable has a default, so an empty `.env` boots a working local setup with an
in-memory store, no LLM, and payments disabled.

CI (`.github/workflows/ci.yml`) runs exactly these commands plus the golden evals on every
push and pull request. No secrets, no network: if it passes locally it passes there.

## Running the API locally

```bash
uv run uvicorn api.main:app --reload --port 8080
```

With the defaults that is an in-memory store, the deterministic (no-LLM) engine and
`X402_MODE=disabled`, so **every endpoint answers without payment** — the fastest way to
see the shapes. The store starts empty, so run the ingest job first if you want real
answers rather than honest "no data ingested yet" ones:

```bash
uv run python -m ingest.job --task all     # needs --all-extras installed
```

### Curl examples

```bash
# Free: no payment, ever.
curl -s localhost:8080/v1/health | jq
curl -s localhost:8080/v1/catalog | jq '.endpoints[] | {path, price_usdc, cache_ttl_seconds}'
curl -s localhost:8080/v1/trending/preview | jq
curl -s localhost:8080/llms.txt          # the agent quickstart

# Paid, with payments disabled (default): answers straight away.
curl -s "localhost:8080/v1/sleepers?week=4" | jq '.verdict, .picks[0]'
curl -s -X POST localhost:8080/v1/player \
  -H 'content-type: application/json' \
  -d '{"name": "Bijan Robinson"}' | jq '.verdict, .confidence'
```

To exercise the real payment path without a chain, run with `X402_MODE=mock`. An unpaid
call then returns a genuine 402 with the payment requirements and the Bazaar discovery
block, and the magic header `mock-paid` stands in for a signed payment:

```bash
X402_MODE=mock uv run uvicorn api.main:app --port 8080

curl -si localhost:8080/v1/trending | head -3                    # 402 Payment Required
curl -s localhost:8080/v1/trending | jq '.accepts[0]'            # what it costs, and to whom

curl -si localhost:8080/v1/trending -H 'PAYMENT-SIGNATURE: mock-paid' \
  | grep -i '^payment-response'                                  # the settlement receipt
curl -s localhost:8080/v1/trending -H 'PAYMENT-SIGNATURE: mock-paid' | jq '.meta.cache'
```

Ask twice and the second answer comes back `"cache": "hit"` — week-scoped endpoints are
generated once per cycle and re-served to later payers (tech spec §6).

### The example agent

`mock-paid` is the shortcut; a real client sends a signed payload. `examples/agent/` has a
runnable one — it reads `/v1/catalog`, refuses anything over its price ceiling, pays, and
prints the receipt with the analysis:

```bash
X402_MODE=mock uv run uvicorn api.main:app
uv run python examples/agent/fantasy_agent.py --base-url http://localhost:8000 --mock
```

See [examples/agent/README.md](examples/agent/README.md) for the flow diagram and for
paying real USDC on TestNet/MainNet.

### The MCP server

`playclock_mcp/` puts the whole paid API inside an AI assistant as tools — each paid tool
call signs a real micropayment from the user's wallet. Tools are derived from `/v1/catalog`
and `/openapi.json` at startup, so they always match what the service actually sells.

```bash
uv sync --extra mcp
X402_MODE=mock uv run uvicorn api.main:app --port 8080     # in one shell
PLAYCLOCK_BASE_URL=http://localhost:8080 PLAYCLOCK_MOCK=1 uv run python -m playclock_mcp
```

Client config, wallet setup and the payment-recovery story are in
[playclock_mcp/README.md](playclock_mcp/README.md).

## Containers and deployment

```bash
docker build -t playclock-api .                       # API service, no ingest deps
docker build -f Dockerfile.ingest -t playclock-ingest . # ingest job, with the ingest extra
docker run --rm -p 8080:8080 -e STORE_BACKEND=memory -e ENGINE=deterministic \
  -e X402_MODE=disabled playclock-api
```

Full GCP deployment — both Cloud Run services, the ingest job, the Cloud Scheduler
crons, Firestore, budget alerts and the MainNet cutover checklist — is in
[infra/deploy.md](infra/deploy.md).

## Repository layout

| Path | Purpose |
|---|---|
| `api/main.py` | App factory, CORS, OpenAPI metadata, `/llms.txt`, the HTML landing page |
| `api/routes/free.py` | `/v1/health`, `/v1/catalog`, `/v1/trending/preview`, `/v1/stats` — no payment |
| `api/routes/paid.py` | The ten x402-gated endpoints, caching and Sleeper flows |
| `api/core/config.py` | `Settings` + `get_settings()`; the price table and `ENDPOINT_KEYS` |
| `api/core/store.py` | Async `Store` seam — `MemoryStore` (hermetic) and `FirestoreStore` |
| `api/core/week.py` | NFL week resolution from the ingested schedule (Tue 3am ET rollover) |
| `api/schemas.py` | Pydantic response/request contracts — the OpenAPI surface agents read |
| `api/x402/` | 402 challenge, verify/settle, receipts, Bazaar discovery |
| `api/agents/` | The `AnalysisEngine` seam: deterministic, narrated and ADK engines |
| `api/evals/` | 23 golden queries (honesty) and the value gate (worth paying for) |
| `api/data/sleeper.py` | Sleeper API client (retry/backoff, injectable transport) |
| `api/data/espn.py` | ESPN unofficial endpoints, feature-flagged, never raises |
| `api/data/stats_store.py` | Firestore read models + the doc shapes ingest must write |
| `api/data/cache.py` | TTL response cache for week-scoped paid endpoints |
| `api/data/team_analytics.py` | Deterministic league/lineup math for `/v1/team-report` |
| `api/data/predictions.py` | The verdict archive behind the public scorecard |
| `ingest/` | The Cloud Run Job: nflverse + Sleeper → Firestore, board warming, backtest scoring |
| `examples/agent/` | Runnable example agent: discover, pay via x402, get the analysis |
| `playclock_mcp/` | MCP server: the paid API as tools an AI assistant can buy |
| `infra/env.example` | Every environment variable, documented |
| `infra/deploy.md` | gcloud deployment, schedules, monitoring, MainNet checklist |
| `web/` | The web UI: plain ES modules, plus a bundled Pera/Defly wallet layer |

## Architecture in one paragraph

FastAPI on Cloud Run. Paid routes declare an x402 payment dependency that 402s, verifies the
client's `PAYMENT-SIGNATURE` via the GoPlausible facilitator, runs the handler, then settles —
so a failed answer is never charged. Analysis comes from an `AnalysisEngine` seam: the
deterministic engine computes every body from Firestore read models; in production the
**narrated** engine has Gemini rewrite the prose under a grounding guard that rejects any
number or name not already in the body; and the ingest job precomputes the league-wide boards
through the full ADK pipeline (stats agent → `google_search` research agent → synthesis)
behind a value gate. All persistence goes through the async `Store` seam, so the whole stack
runs offline against `MemoryStore`. A separate Cloud Run Job ingests nflverse and Sleeper data
on a schedule and scores every archived verdict once the week is played. Details in
[TECH_SPEC.md](TECH_SPEC.md).

## Data attribution

Player statistics come from **[nflverse](https://github.com/nflverse), licensed
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)**. Attribution is carried in every
paid API response (`meta.attribution`) and must remain there. Market signal (trending
adds/drops), league, roster and matchup data come from the public read-only
[Sleeper API](https://docs.sleeper.com/). Supplemental headlines come from ESPN's unofficial
public endpoints and are feature-flagged and non-load-bearing.
