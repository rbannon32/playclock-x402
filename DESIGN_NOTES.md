# Design Notes — Play Clock build

Running log of judgement calls, open questions, and ideas that came up during the build.
(Orchestration: Fable makes the calls recorded here; Opus agents execute the build waves.)

---

## Judgement calls made during the build

### 1. Pluggable storage backend (`STORE_BACKEND=memory|firestore`)
The Tech Spec assumes Firestore everywhere, but this environment (and CI, and any
contributor laptop) has no GCP credentials. All persistence goes through a small async
`Store` interface (`api/core/store.py`) with two implementations:

- `MemoryStore` — in-memory, used by tests, local dev, and the eval suite. Hermetic.
- `FirestoreStore` — thin wrapper over `google.cloud.firestore.AsyncClient`, same
  collection/doc semantics, used in Cloud Run.

This is deliberately *not* a general ORM — it mirrors the exact subset of Firestore we
use (get/set/query-by-field/collection scan) so the Firestore impl stays trivial.
Consequence: everything is testable offline, and milestone 1–5 code runs end-to-end
locally with zero cloud setup.

### 2. Payment gating as a route dependency, not blanket ASGI middleware
The spec asks for a route-level allowlist ("not path-prefix magic"). The cleanest
FastAPI expression of that is a dependency factory: paid routes declare
`Depends(require_payment("trending"))`. Free routes simply don't. There is no global
middleware that has to guess which paths are paid. The dependency:
402s with the x402 payment-requirements payload → verifies `X-PAYMENT` via the
facilitator client → lets the handler run → settles → attaches `X-PAYMENT-RESPONSE`.

Settlement ordering: verify → handler → settle. If the handler fails after a
successful verify, nothing is settled (client isn't charged); if *settle* fails after
the handler ran, we still return the result plus log to `failed_paid_calls/` — we ate
one LLM call, the user isn't charged. That's the cheapest goodwill direction, better
than the spec's "settle pre-handler, manual refunds" note.

### 3. Facilitator behind an interface + `X402_MODE=disabled|mock|live`
The GoPlausible verify/settle HTTP shapes are behind a `FacilitatorClient` protocol.
`mock` mode accepts a magic test header (dev/browser testing on a laptop), `disabled`
bypasses payments entirely (unit tests, local hacking), `live` talks to the real
facilitator (TestNet/MainNet chosen by `X402_NETWORK`). Only `x402/facilitator.py`
has to change if the facilitator API shifts before launch — the ecosystem is the
fastest-moving part of this project, so it gets the thinnest, most replaceable seam.

### 4. LLM pipeline behind an `AnalysisEngine` interface
`AdkAnalysisEngine` (real: ADK SequentialAgent per endpoint) and
`DeterministicAnalysisEngine` (fake: builds a valid response contract straight from
the stats tools, no LLM). Selected by `ENGINE=adk|deterministic`. Why:
- Tests and CI validate the full request→402→pay→handler→contract path without Vertex.
- The fake engine doubles as the guaranteed-schema fallback and as a fixture source.
- The golden evals run only against the real engine, gated on creds being present.

### 5. All third-party HTTP through typed clients with injected transports
Sleeper/ESPN/facilitator clients are `httpx.AsyncClient`-based, tested with `respx`.
ESPN failures degrade to empty results by design (spec §4.3); Sleeper gets shared
client + retry/backoff + a courtesy rate cap.

### 6. One `pyproject.toml`, all deps declared up front
Ingest-only deps (nflreadpy/polars) live in an optional-dependency group `ingest` so
the api image stays slim, but there is a single lockfile. Build waves after wave 1
are forbidden from touching pyproject/uv.lock — avoids lockfile merge hell between
parallel agents.

### 7. Week resolution
"Current NFL week" is computed from the ingested schedule table (first game date per
week), with `WEEK_OVERRIDE` env for testing and for the gap before ingest has run.
Never derived from wall-clock math alone.

### 8. Python 3.12, uv-managed (per spec)
The container's default python is 3.11; uv pins 3.12 via `.python-version`. Don't use
3.13+ — polars/nflreadpy/ADK wheel coverage is the constraint.

### 9. Doc ids are Sleeper player ids; gsis is a field, not a key
`weekly_stats/` and `usage_trends/` are keyed by Sleeper `player_id` (mapped from
nflverse `gsis_id` via the ingest-built `id_map/`), because the whole request path —
name resolution, trending, rosters — speaks Sleeper ids. Unmapped players fall back
to their gsis id as doc id (logged, still counted in `def_vs_pos`). Operational
consequence: the `nightly` task must run before `stats` on a cold store.

### 10. Red-zone touches come from play-by-play
Discovered during a live ingest run: **no nflverse weekly table carries red-zone
data** — `load_player_stats` and `load_ff_opportunity` both lack it, and the PRD
sells "red-zone touches" in `/v1/sleepers`. Ingest derives it from `load_pbp`
(rushes + targets snapped inside the 20), as a supplemental dataset that degrades
gracefully. Also found live: the 2025+ depth-chart table changed shape (snapshot
`dt` rows, no season/week columns) — ingest keeps only the latest snapshot.

### 11. Idempotency keys include the endpoint — and the request
Payment-replay cache key is `endpoint_key + SHA256(payment header)` — otherwise a
$0.10 trending payment could be replayed against the $0.75 team report within the
300s idempotency window (raised from 60s; see §27). Cross-endpoint replay is
rejected and tested.

The remembered payment also carries a fingerprint of the request it bought
(method + path + sorted query + SHA256 body). *Same* request inside the window =
a retry: cached receipt, no second settle. *Different* body or query = a second
purchase, and is rejected with a 402 before the handler runs — without that, one
$0.15 `/v1/player` payment could fetch a different player every second for a
minute, since the cache skipped verify/settle but the handler still ran on the
new body. Consequence for clients: mint one payment per request (the web UI's
mock provider now does, like a real wallet).

### 12. Settlement adds one facilitator round-trip before first byte
Settle-after-handler means the receipt header costs one facilitator call of latency
on paid responses. Accepted: correctness (never charge on failure) beats ~1s of
latency at these price points. Revisit only if p95 breaches the 20s target.

### 13. ADK 2.8 deprecates SequentialAgent (in favor of `Workflow`)
We stay on `SequentialAgent` — it works, matches the tech spec, and `google-adk` is
pinned `>=2.8,<3`. Migrating to the Workflow graph API is a contained follow-up in
`api/agents/pipeline.py` when the deprecation bites.

### 14a. Cold store refuses paid work (503, unbilled) — per dataset
Found in the final live smoke test: with no ingested data, a paid call would settle
and return an honest-but-empty "no data" analysis — charging $0.10 for nothing.
Paid routes now 503 ("You were not charged") when the data they read is missing,
which also makes the deploy ordering (ingest before first sale) self-enforcing.
The check is **per endpoint**, not "is `meta/freshness` non-empty": the ingest
tasks fail independently, so a successful `nightly` or `trending` run alone leaves
the marker populated while `weekly_stats`/`usage_trends` are still absent — and
`/v1/sleepers` would have sold an empty board. `REQUIRED_DATASETS` in
`api/routes/paid.py` maps each endpoint to the freshness keys it cannot answer
without, and the 503 names the missing ones.

### 14. Web UI wallet signing — stubbed at build time, implemented 2026-08-30
Shipped as a documented stub because real wallet signing could only be validated
against TestNet from an unblocked network. Now built, and building it forced one
architectural decision: **`web/` gains a build step.**

Pera, Defly and algosdk cannot be vendored honestly — a committed 1.5MB minified
bundle has no lockfile, no audit trail and no reviewer. So `web/` now has a
`package.json`, a committed `package-lock.json`, and an esbuild step that
compiles **only** `js/wallet/` into `dist/wallet.js`. Everything else is still
plain ES modules served exactly as written, and `payment.js` reaches the bundle
through a *dynamic* import, so nothing that does not pay ever downloads it and
local development needs no build.

The two files that decide whether the right money moves — `exact-avm.js` (what
we sign) and `envelope.js` (what we send) — are deliberately kept out of the
bundle and tested under `node --test` with no wallet, browser or network. The
sponsored fee-payer group shape is implemented from the Python SDK but has never
been seen live, and is marked as such in the code.

## 15. The MCP server is a client, and it journals every payment

`playclock_mcp/` exposes the paid API as tools an assistant can buy. Three calls
worth recording:

**Tools are derived, not declared.** The server reads `/v1/catalog` (prices,
cache policy, which routes are free) and `/openapi.json` (the real request
schemas) at startup and builds one MCP tool per endpoint. The alternative — a
hand-written tool list — is a second copy of the contract that drifts. Proof it
works: the draft board and draft report became MCP tools, with their schemas and
prices, without a line of change in `playclock_mcp/`. Only *reachable* `$defs`
travel with each schema; attaching the whole component map shipped every response
model with every request and cost tokens on every tool listing.

**Payments are journalled before they are sent.** Settlement precedes the
handler, so a timed-out call has already moved USDC and the answer is
recoverable only by replaying the identical request with the identical
`PAYMENT-SIGNATURE`. The journal (`~/.playclock/pending-payments.json`, mode
0600) stores the whole request — method, path, sorted query, body — because
anything less reconstructs a *different* request, which the server refuses with
a 402 rather than answering. `playclock_recover_payments` replays what is still
inside the 300s idempotency window; entries past it stay listed so a lost
payment is visible rather than silent. A test drops a paid response on the floor
and proves recovery returns the answer with no second settle.

**No code is shared with `examples/agent/`.** That file is a teaching artifact —
one file, heavy prose, a CLI that narrates the protocol. This is a product
surface that must not print to stdout (it is the MCP transport) and must not
lose a payment. Merging them would make both worse. The duplication is
deliberate and noted in `TODO.md`.

## 16. Draft endpoints: what the market signal is, and which way the sign goes

**`market_rank` is Sleeper's `search_rank` — draft *popularity*, not a consensus
ADP.** We have no projection-service ADP and buying one was out of scope, so the
honest move is to use what we have and never dress it up. The prompts forbid the
term, an eval asserts no board row or verdict uses it, and a second eval asserts
the reasoning *does* disclaim it. Players with no `search_rank` are excluded from
the board rather than ranked last: the market having no opinion is not the same
as a low opinion.

**The board adjusts the market rather than replacing it.** Usage is the one
thing we measure better than the crowd; positional value, offseason signings and
injury news we measure worse. So the board starts from the market order and
shifts each player by at most `DRAFT_RANK_SHIFT` (20) ranks on their
prior-season usage percentile *within their own position* — comparing a running
back's snap share to a quarterback's would rank every quarterback first. A player
with no prior-season usage holds their market rank and the note says so.

**Two different value metrics, and both were wrong first time.**

- *Board* `value_delta` was `market_rank - rank`. Ask for 200 players out of a
  market that ranks thousands and every rank is lower than every market rank, so
  every player reads as a value. It is now the market's ordering **of the same
  board** minus our rank — like for like.
- *Report* `value_delta` was `market_rank - pick_no`, which made a market-rank-14
  player taken at pick 78 a *reach*. It is the opposite: taking a player later
  than the market drafts them is value. Now `pick_no - market_rank`, positive =
  value. A route test pins the sign so it cannot silently flip back.

**The board caches under week 0.** It is season-scoped, and drafts run every
night of the week; keying it to the live week would generate a fresh board every
Tuesday and hand each week's first drafter the cold path. `WarmTarget.fixed_week`
carries this into the warmer, and a precompute test asserts the key the warmer
writes is the key the route reads — it caught this exact mismatch.

## 17. A degraded answer beats an outage — but it is not free

`AdkAnalysisEngine` raises `EngineError` after its retries and the route turns
that into a 500. Vertex answers 429 whenever calls bunch up; `ingest/precompute.py`
already treats that as routine and waits minutes between attempts, which is
exactly the luxury a paid request does not have. Advertising produces bunched
calls, so the endpoints would be down at the moment they are busiest.

`FallbackAnalysisEngine` (`ENGINE_FALLBACK`, default on) answers from the
deterministic engine instead. The trade is worth naming because it is not
obviously right: **a 500 settles nothing, while a fallback answer is billed.**
Falling back charges someone for a worse answer than the one they were quoted.

It is defensible for three reasons and would not be otherwise:

- the deterministic body is a *real* answer — it is the eval baseline, and every
  number in it is traced back to the ingested store by the same check the golden
  suite runs;
- it self-identifies: `meta.model` is `null`, so a caller can see what they
  got (the reasoning used to open with "Deterministic engine" as well; §24
  explains why that preamble was dropped);
- the alternative is a dead endpoint, which serves nobody and still earns no
  volume.

The dangerous version of this feature is the silent one: a credentials mistake
would make *every* answer fall back while callers pay LLM prices and nothing
looks broken. So the failure logs at ERROR with the primary's exception type and
a running count, and `infra/deploy.md` §6 carries an alert on the rate. One
fallback is fine; a sustained rate is an outage in disguise.

## 18. Fresh data for the wrong year looks exactly like fresh data

Observed 2026-08-30: `/v1/health` reported `season: 2025, week: 18` on a service
configured for 2026, and nothing in the stack noticed. Every dataset was
present, every freshness marker was stamped, `missing_datasets` returned
nothing, and every paid answer would have been confidently about a season that
had finished.

The readiness check answered "is the data there", which was the wrong question.
`stale_season()` adds "is it for the year we think it is", comparing the season
on `meta/schedule_weeks` (what ingest actually wrote) against `SEASON` (what the
service is configured for). `current_season` deliberately *prefers* the store so
a rollover needs no redeploy — which is also why the two silently diverging is
possible at all.

Both callers get it: the route 503s unbilled, and `ingest/precompute.py` refuses
the whole run and exits non-zero. The warmer matters more than it looks — a
board warmed from last season is served as a `cache=hit` for its entire TTL,
long after someone would have noticed a single bad answer.

Known limit: the check is on the schedule marker, not per dataset. Schedules and
weekly stats are written by the same ingest task in one pass, so a skew between
them is not a shape this can currently produce.

## 19. Week 1 is empty on purpose, and a real league draft is often tiny

Both verified 2026-08-31 against live Sleeper leagues, not from docs.

**Every played-game metric is legitimately zero before kickoff.** `/v1/team-report`
was returning lineup efficiency 0.0%, efficiency rank 1 of 10, and a "B-" for every
position on 0.0 points per week — for 0.75 USDC. Nothing reported it, because
nothing was wrong: the datasets were present and fresh, `missing_datasets` was
empty, and `team_analytics` had already said so in a `no_matchup_history` warning
nobody read. A zero that means "not yet played" is indistinguishable, to a reader,
from a zero that means "played badly", and the grades made it worse by looking like
assessments. The route now substitutes what has actually happened — the draft and
the week-1 schedule — and 503s ("you were not charged") when neither exists.

**A Sleeper league's draft is frequently 3-6 picks, not a full roster.** League
"Best League Ever" (10 teams, 29-player rosters) has a 40-pick draft: four rounds,
three or four picks per team. It is a keeper/rookie round, and the rest of each
roster came from prior seasons and waivers. Grading those picks with
`pick_no - market_rank` against the *global* `search_rank` scores every one as a
100+ rank reach and lands on an F — the same truncated-board trap §16 describes for
the draft board, arriving from the other direction. The preseason block only grades
a draft when it made at least as many picks as the lineup has starting slots;
otherwise it reports that the draft happened and leaves it ungraded.

**The week-1 lean is a lean, not a probability.** Before kickoff the only
forward-looking number in the store is Sleeper `search_rank`, and `weekly_stats` for
the new season is empty (`usage_trends` still carries *last* season, through week
22). Set lineups are therefore compared on summed market signal and reported as
"clear edge / slight edge / toss-up / underdog" with both totals shown. Emitting a
win percentage from that would be inventing precision the inputs do not contain.

## 20. `get_current_season()` lags, and it used to outrank `SEASON`

**Launch-day outage, 2026-09-01.** MainNet went live in the morning. By early
afternoon every paid route was returning 503: *"The ingested data is for the
2025 season but this service is configured for 2026."*

The Tuesday `ingest-stats` cron (`0 9 * * 2,4,6`) ran at 13:01 UTC and
re-ingested the **2025** schedule over the 2026 one. `meta/schedule_weeks`
flipped to 2025 dates, `current_week()` resolved to 18, and `stale_season()`
did exactly what it was written to do.

Cause: `resolve_season()` ordered its sources
`override -> nflreadpy.get_current_season() -> Settings.season`. The existing
note in §"Data layer" says never to compute the season from the calendar year,
and that is true — but it is not the whole trap. **`get_current_season()` itself
lags:** on 2026-09-01 it still returned 2025. Trusting it over explicit config
is how ingest and serving came to disagree.

The precedence was the bug, not the lag. `SEASON` is an operator declaration of
what a deployment sells, and `stale_season()` refuses to serve anything else —
so anything allowed to outrank `SEASON` can take the paid API down without
touching the API. `resolve_season()` now goes `override -> SEASON`, with
upstream demoted to a cross-check that logs a warning on disagreement (that
warning is how you learn it is time to bump `SEASON`; bump it and redeploy the
API in the same change).

Two things worth keeping from how this failed:

- **The guard paid for itself.** Nothing else reported the problem — data was
  fresh, freshness was stamped, `missing_datasets` was empty, the boards were
  warm. §18 predicted precisely this shape, and it held on the first day it
  mattered.
- **Nobody was charged.** The 503 fires before the handler, so nothing settled.
  A silent wrong-season answer would have been the far worse outcome, and on
  MainNet it would have been a real payment for it.

Recovery, for next time: `gcloud run jobs execute ingest --region=us-east4
--args="--task,schedule,--season,2026"` rewrites `meta/schedule_weeks` on its
own without a full re-ingest. The `schedule` task exists for exactly this.

## 21. Settled 12 payments, catalogued zero — the method is half the key

Discovered 2026-09-08, after a week on MainNet with no third-party traffic.

The challenge leaderboard had Play Clock at rank 38 with 12 settles and 2.2
USDC, `challenge: true`, name and logo scraped correctly — and **`bazaar:
false`**. `GET /discovery/merchants/MDBJMM6R…` returned 404. Every payment had
settled; not one had catalogued. Agents browsing the Bazaar could not find the
API, which is the whole agent-native distribution story.

It is not a volume threshold: of the 124 challenge merchants, `bazaar: true`
matched catalogue presence exactly (0 of 31 `bazaar: false` rows appear in
`/discovery/resources`), and merchants with a single 0.25 USDC settle are
listed.

**A catalogue id is `base64("METHOD:URL")`.** Sampling 60 catalogued resources,
all 45 GETs carry `discoveryInfo.input.method`; ours carried none, because
`declare_discovery_extension` does not add it. Its docstring is explicit: the
method "is automatically inferred from the route key or enriched by
`bazaar_resource_server_extension` at runtime" — the SDK's decorator-based
server. Play Clock gates with a route dependency instead (§2), so nothing ever
enriched it. The SDK is not wrong; we opted out of the mechanism that fills the
field, and nothing said so.

The half-fix that came before made this hard to see. `payment_required_body`
already injects `method` into the 402's `resource` block for the Bazaar's
benefit. That reaches the client and dies on the way back: the returned envelope
is re-validated through the SDK's `ResourceInfo`, which has exactly three fields
and drops the rest, so the facilitator never sees it. The same pydantic
narrowing that docstring warns about, one layer down and in the other direction.

So the method now rides in `extensions.bazaar.info.input.method`, which is a
plain `dict[str, Any]` end to end — client echo, our re-parse, `model_dump` to
the facilitator — and is the field the catalogued records actually carry.
`test_the_discovery_method_survives_the_client_round_trip` asserts both halves,
including that `resource.method` is still dropped, so moving it back there fails
a test instead of failing silently in the catalogue.

**The listing is still one-shot.** This was never a case of the wrong URL being
catalogued — nothing was catalogued at all — so the first settle after this
ships is what registers, and it must come from `https://api.playclock.xyz`.

## 22. The freshness SLA and the preseason gap, four days from an outage

Two correct changes landed hours apart on 2026-09-01, from different sessions,
and together they scheduled an outage for **2026-09-05 13:01Z** — four days
before kickoff, mid-leaderboard-window.

- PR #27 added `DATASET_MAX_AGE_SECONDS`: refuse to sell analysis whose ingest
  marker is older than its SLA. `weekly_stats`, `usage_trends`, `def_vs_pos`
  and `injuries` got 96h. Right — a stopped ingest job should stop sales.
- PR #26 stopped stamping freshness for datasets the ingest did not write,
  because nflverse publishes no `stats_player_week_{season}` file before the
  season starts and nflreadpy rejects the season outright. Also right —
  stamping would claim last season's numbers were today's.

Together: four datasets frozen at their last 2025-era stamp, no way for anyone
to refresh them until nflverse publishes, and a 96h clock running. Eight of the
ten paid endpoints (`sleepers`, `waivers`, `report`, `player`, `matchup`,
`roster`, `team_report`, `draft_board`) would have begun 503ing with "missing
or stale", and the cause would have read as an ingest failure rather than as
the calendar.

The fix makes the distinction explicit rather than inferred. The stats ingest
writes `meta/preseason_gap` naming the datasets upstream cannot supply, and
`stale_datasets()` skips them. **The marker is trusted only while a recent run
keeps re-affirming it** (`GAP_TRUST_SECONDS`, 96h — wider than the cron's widest
gap, Sat→Tue at 72h). A successful stats ingest clears it; a dead ingest job
stops refreshing it and the SLA resumes refusing sales. An exemption that could
not expire would turn "upstream has nothing yet" into a permanent excuse for
serving silence.

The lesson is about review rather than about freshness: **neither change was
wrong, and neither diff contained the bug.** It existed only in the interaction,
and only because both landed the same afternoon. Reading a merged-but-
undeployed PR against everything else that merged that day is what caught it.

## 23. Sleeper omits `gsis_id` for the players that matter most

Measured 2026-09-01 against the live Sleeper dump and our own `players/`:

| population (by market rank) | had a `gsis_id` |
|---|---|
| top 50 | **16.0%** |
| top 100 | 22.0% |
| top 200 | 25.5% |
| all ranked fantasy actives | 30.4% |

Not a long tail — **the inverse**. The missing set inside the top 200 reads
like the first two rounds of a draft: Jahmyr Gibbs, Bijan Robinson, Ja'Marr
Chase, Jonathan Taylor, Justin Jefferson, CeeDee Lamb, Amon-Ra St. Brown, Puka
Nacua, Jaxon Smith-Njigba, De'Von Achane.

`gsis_id` is the nflverse join key, so for every one of those players
`weekly_stats` and `usage_trends` were unreachable: a paid `/v1/player` call
returned a depth chart, a matchup, and nothing else. Worst possible
distribution — absent for exactly the players people ask about.

**It is upstream, not our ingest.** `GET /players/nfl` returns
`"gsis_id": null` for them. (It also returns `" 00-0035710"` — with a leading
space — for Daniel Jones, which `_clean_id` already strips. That is the data
hygiene on offer.)

`nflreadpy.load_ff_playerids()` carries `sleeper_id` **and** `gsis_id` and
closes the gap: top 50 to 100%, top 200 to 99.5%, overall 30.4% -> 66.4%. It is
pulled in `task_nightly` and applied by `backfill_gsis()`, which fills blanks
only — Sleeper stays authoritative where it has the id — and degrades to a
no-op if the table is unavailable.

Two things worth carrying forward. The earlier note recording this table as
"blocked from this environment ... optional enhancement" was wrong on both
counts: it is not blocked, and it is not optional. And `sleeper_id` is typed as
an **integer** in that table while every other surface treats it as a string,
so the join needs an explicit cast — get it wrong and the bridge silently
matches nothing, which looks identical to not having run it.

## 24. Trust is the floor, edge is the product

**Observed 2026-09-03.** MainNet had been live two days and every settle on
the board was Ryan's own. He bought player, sleepers and trending and said the
content felt lackluster. Every one of those answers had passed the suite.

What a buyer actually received, verified against the live Cloud Run config,
the cached boards in Firestore, and the deterministic engine on the fixture:

- Five of ten endpoints were template strings. The api service runs
  `ENGINE=deterministic`, so player, matchup, roster, team report and draft
  report opened with *"Deterministic engine — heuristic analysis from ingested
  stats, no LLM synthesis."* The roster audit's reasoning contained a Python
  dict literal. Every note said "L4W".
- The ADK boards mostly re-narrated Sleeper's add counts. The Week 1 report
  cited six numbers, four of them `trend_count`; its "emerging" section —
  defined in the prompt as usage growing *before* the crowd arrives — listed
  the three most-added players in the league. The sleepers board had twelve
  picks, ten of which said their usage data was not available, at medium
  confidence under a low-confidence verdict; the eval asserted `8 <= picks`,
  so it *rewarded* the padding. FAB bids were 100/80/75% one run and 18/12/8%
  the next. Sources were opaque Vertex grounding redirects titled with a bare
  domain.
- One integrity leak: a beneficiary named in an injury chain with
  `player_id: null`, a name the tools never returned. The citation gate
  checks numbers and never names.

**The diagnosis is about the ethos, not one prompt.** The product was built
to never lie, and it does not. But "never provably wrong" is a floor. A
manager pays for a call they would not have made, with a number they can
repeat in league chat, and nothing measured that. The 23 golden queries are
property tests — schema, resolution, citation traceability, section shapes —
and the ADK evals only run by hand. There was no rubric, no LLM judge and no
backtest anywhere in the repo.

**What changed, and the judgement calls inside each.**

*Prose leads with the claim.* The engine preamble is gone from every
reasoning; `meta.model` being null is the disclosure, and the UI and MCP
server already surface it. Each reasoning now opens with the contrarian call
the body supports (the fade, the buy-low, the biggest value, the move to
make) and ends with the method sentence. Usage and matchup notes say "last
season" when the data predates the season under analysis (`_PlayerView.season`
vs. the rollup's or split's `season`). The waiver board is ranked by usage
growth, matchup and depth-chart seat with add volume only as a logarithmic
tiebreak — the crowd's order is the input, not the answer — and its reasoning
names where the two orders differ.

*A second eval gate, on a plain dict.* `api/evals/quality.py` takes
`model_dump()` output so the same rules run in the golden suite (CI, against
the deterministic engine), in `run_evals --engine adk`, and inside
`ingest/precompute.py` on every board it warms — which is the only place the
ADK output payers actually receive is ever inspected. Rules: no code artifacts
in prose; every named row carries a real player id (the store is consulted at
precompute); boards with three or more rows cite at least three non-crowd
numbers; trending must fade or hold an add or buy a drop; the waiver order
must differ from add-count order somewhere; sleepers and "emerging" exclude
anyone at or above 3,000 adds; a short sleepers board must say so; per-row
confidence never exceeds the verdict's and "usage not available" forces low;
sources are not redirects or bare domains. The fixture gained a big add whose
usage is declining so the trending rule has something to prove, and the
sleepers assertion became `1 <= picks <= limit`.

The gate **records and logs, it does not refuse.** A board failing it is
written to `quality/{key}` beside the cached body, counted as `flagged` in the
run summary, and logged on one `board quality flagged` line for the alert in
`infra/deploy.md` §6. Refusing would leave the board cold, and a cold board
503s every caller — worse than a mediocre one. The judgement is that "served
and loud" beats "unserved and quiet".

*Boards narrate a list the data chose.* The synthesizer, left to pick, reached
for the trending board. So `DeterministicAnalysisEngine.candidates()` exposes
the same scorer the deterministic sleepers and report use, `AdkAnalysisEngine`
injects it as `request.candidates`, and the prompts say to draw from it only.
This is the team-report pattern — narrate, do not produce — applied to the two
sections it was missing from, and it is the only pattern here that had a
passing eval behind it before this note.

*`ENGINE=narrated`.* The deterministic engine builds the structured body, one
tool-free Gemini call rewrites only the prose paths in a per-endpoint
whitelist, and `apply_edits` rejects any sentence whose numbers are not
already in the body or that names a player the body does not. A rejected edit
keeps the template text; a timeout (`NARRATOR_TIMEOUT_SECONDS`) or a failure
serves the deterministic body with `meta.model` null and increments a
`degraded` counter, logged for the same alert pattern as §17. It exists
because the personalized endpoints cannot be precomputed and the full
pipeline runs ~55s against a settlement that has already happened; this is
one turn, a few seconds, and the hallucination surface for numbers is closed
in Python rather than by prompt. The api still runs `deterministic` until one
TestNet run measures the narrated path against the client deadline.

*Every verdict is scored.* `api/data/predictions.py` archives each scoreable
call (start/sit, add/fade, sleeper, waiver start, emerging, matchup rank-1)
under `predictions/{season}w{week}:{endpoint}:{kind}:{player_id}` with
`store.create()`, so the first thing we said is what gets graded — a board
re-warmed six times a day does not get to revise its picks. `ingest --task
backtest` scores them once the week's stat lines exist: a hit is scoring at or
above the startable bar for the position (QB/TE/K/DEF 12th, RB/WR 24th), a
sit or fade hits below it, and matchup rank-1 hits when it outscored its
group. `/v1/stats` publishes the summary with the method stated in the body.
It is the only measurement of value in the system, and the honest number for
the §5(b) submission.

*Sources are resolved after synthesis* (`api/data/sources.py`, inside the ADK engine and again at precompute): follow the
grounding redirect, read at most 64KB, take `og:title` or `<title>`; every
failure leaves the citation as it was. *A judge scores each board*
(`ingest/judge.py`, `QUALITY_JUDGE=true` on the ingest job only): four rubric
lines, 1-5 each, mean under 3 flags. It never sees the store and never blocks
a warm.

**Two holes review found in the backtest, both closed.** A claim is filed
only if it predates the week's first kickoff on the ingested schedule, and is
not filed at all when no schedule is ingested — first-write-wins is no
defence when the first write is a request for last week made on Tuesday, and
the paid routes accept any week. And a week is scored only when every kickoff
on its schedule is at least eight hours old: scoring is permanent, so a
Thursday-only stats file would otherwise grade every Sunday starter as a
scratch forever. Both comparisons go through `api/core/clock.py`, the same
shape as `set_store()`, so the route tests can pin the date instead of
inheriting the calendar. The narrator's name guard was tightened at the same
time: a capitalised pair must match one body name, not one word from each of
two — "Bijan Mahomes" was passing.

**The same afternoon's second review, on PR #32.** The scheduler I proposed
for `backtest` — its own 09:30 job — could overlap a long `stats` run, and
`week_complete()` treated any non-empty collection as the played week: a
half-written snapshot would set the bar from the lines present, score the
rest as scratches, and, because scoring is permanent, never correct itself.
Two fixes, belt and braces. The task now runs *inside* the stats invocation
(`--task stats,backtest`; the runner skips `backtest` outright when `stats`
failed), and `week_complete()` additionally requires the `weekly_stats`
freshness marker — stamped only when a stats run finishes — to post-date
the week's last kickoff plus the grace. The marker is the honest signal for
"the collection is whole"; a document count is not.

**The rebuild the judge asked for, and why it needed a flag.** Bijan Robinson
and Ja'Marr Chase carry a gsis id since the §23 bridge but had no
`usage_trends` doc, because the 2025 rollups were built before it landed. The
obvious fix, `--task stats --season 2025`, is the launch-day outage (§20)
with extra steps: it writes the 2025 schedule first, clears the 2026
preseason-gap marker (§22), and overwrites this week's injuries and depth
charts with last year's. `--stats-only` exists so a prior season's stat
rollups can be rebuilt without touching anything that describes the current
one. And the candidate lists gained the two rules the judge's critiques
implied: a sleeper needs a market rank above 36, and in preseason both lists
require a current depth-chart job — rank 1 anywhere, rank 2 for RB/WR/TE,
never a clipboard quarterback, never a player with no chart.

**The ADK suite with the gate on, 2026-09-03 night: 7 of 23.** The first
run of `run_evals --engine adk` against the model since the value gate
existed, and the failures sorted into five buckets, none of them the gate's
rules being wrong. Thirteen cases failed `sources_resolved` because the gate
judges the engine's output and source resolution only ran in precompute — so
resolution moved into `AdkAnalysisEngine` after synthesis, where the output
contract lives, and precompute's call became the idempotent safety net
(`api/data/sources.py`, moved under `api/` because the api image does not
ship `ingest/`). The six-row trending case came back with eight rows, the
same overrun the live board showed at fifty under a limit of twenty-five, so
the engine now truncates to `request.limit` after synthesis rather than
trusting the prompt. Three prompt obligations the model was ignoring are
now stated outright: an unresolved player's `player_id` stays empty, a
missing rollup means a null trajectory, and the draft-board reasoning must
say "not a consensus ADP" in those words — the old prompt said never to say
ADP, and the model obeyed so well it omitted the required disclaimer. And
the team report failed both attempts on a Vertex 429 with no pause between
them; a retry at once spends the retry on the same throttle, so the engine
now waits twenty seconds before it.

**What a buyer saw on the draft board, and why.** Ryan's screenshot of a
paid `/v1/draft-board` showed a verdict, three sentences and four citations:
"just a tidbit of info". Two causes. The web UI's `BODY_RENDERERS` table had
entries for the eight original endpoints and none for the draft pair, so the
200-player body the buyer paid for was in the JSON and nowhere on the page;
both renderers now exist. And the body itself held 30 players: the ADK
synthesizer, asked for 200 rows, wrote 30, with value deltas the eval
catches as not adding up. A 200-row ranked board with computed deltas is a
computation, not a synthesis, so the draft board is now warmed through the
narrated engine — `WarmTarget.engine`, honoured only under `ENGINE=adk` —
and the model's job is the tier labels, the notes and the reasoning, each
checked against the computed body. `meta.engine` names the engine on every
body so the value gate can tell a narrated body from an ADK one: the
grounding rule applies to ADK output, where every number must be cited,
and not to narrated notes, which carry numbers the deterministic engine
derived.

The first narrated board also surfaced two small things the judge and a
reader would both catch: Tom Brady at rank 97, because Sleeper's dump still
lists him as active with a search rank and the pool never asked for a team
(it does now — the team field is the one signal that survives retirement,
and seventeen of two hundred rows had none), and "71th percentile" in a
note. And regenerating one board to check a fix should not cost the other
four, so precompute takes `--only`.

**The ADK suite with `numbers_grounded` on: 17 of 23.** Every new failure is
the rule catching the synthesizer quoting a number it did not cite — a
waivers board with zero non-crowd citations and twenty-four uncited numbers,
a trajectory delta, two report notes, an analytics number cited under an
invented source string. Two of those were prompt gaps and are fixed; the
draft-board delta failure is gone by construction. The rest is the honest
finding: the synthesizer's citation discipline is imperfect, the gate now
measures it, and a board that keeps failing it is a candidate for the
narrated pattern, where the numbers are computed and the model cannot
misquote them.

**The gate's last rule, and what it found.** `numbers_grounded` applies the
narrator's grounding guard to model-written bodies: every number in every
prose field must be a numeric leaf of the body or a `stats_cited` value,
with years, zero and 1-12 exempt and only whitespace-bearing strings read as
prose (an id is a value, not a claim). The deterministic engine is exempt
because its prose legitimately carries numbers it computed, and the narrated
engine is guarded at edit time. Run against the freshly warmed boards it
flagged exactly the judge's examples — a `0.1898` target share in a waiver
rationale, a `76977`-add stock-up note — so the judge and the rule now agree
on what "grounded" means, and the synthesis contract says it outright.

**The ADK suite again, after 8c647b8: 18 of 23.** No sources failure
remained. What was left was the model taking the new stats-agent instruction
("report the numbers verbatim with their source strings") into its prose —
`snap_pct_delta of 0.13` in a trending note — so the synthesis contract now
says statistics are written in words and field names live only in
`stats_cited.stat`; the sleepers reasoning not saying why a three-pick board
was short, so that prompt now dictates the sentence; and a roster case whose
Vertex 429 was wrapped in the `ParallelAgent`'s `ExceptionGroup` ("unhandled
errors in a TaskGroup (1 sub-exception)"), invisible to the quota check, so
the retry ran at once and lost. The check now flattens groups to their
leaves and the log line names the real error.

**The second gated warm, 2026-09-03 night, after 8c647b8 and the rebuild.**
All five boards pass the value gate; judge means 4.0 to 4.25. The trending
board that had cited one non-crowd number across fifty rows now carries
twenty-five rows under its limit and twenty-three usage citations, and its
verdict fades the most-added player in the league (Jacob Saylors, 0.0125
snap rate) with the number in hand. Sleepers are Cade Otton, Jalen Coker and
Michael Mayer rather than Derrick Henry and a backup quarterback; the
report's emerging list is tight ends with real December deltas rather than
Cooper Rush and Andy Dalton; sources carry headlines, and the one page that
yielded none carries its slug. What the judge now points at is one thing,
across every board: numbers in per-row notes that never reach
`stats_cited` (grounding 3 on three boards). That is the narrator's grounding
guard, which the ADK path does not have, and it is the next rule for the
gate — every number in a row note must appear among the body's numeric
leaves or in `stats_cited`.

**The first gated warm, 2026-09-03 evening.** Deployed and forced a warm the
same day. Before the gate, the cached Week 1 boards failed it everywhere but
waivers and the draft board: trending cited one non-crowd number across 50
rows; sleepers failed on confidence and on ten consensus picks; the report
failed on an ungrounded beneficiary, on "emerging" being the top adds, and on
every source. After: sleepers, report, waivers and the draft board pass the
rules; sources resolve to real headlines; the report's emerging section is
the candidate list rather than the add leaders. Trending is still flagged —
one non-crowd citation across 50 rows and one source whose page yielded no
title — and is served flagged, as designed. Judge means: trending 4.25,
waivers 4.25, report 4.0, draft board 4.0, sleepers 3.5.

The judge's critiques are the more useful output, and they point at data,
not prose. Preseason is the problem: with 2025 closing-month deltas as the
only usage signal, the emerging pool surfaced Gunner Olszewski, Dante Pettis
and two backup quarterbacks, and the sleepers list carried Derrick Henry
(a sleeper only in the sense that nobody is adding him, because everyone
already owns him) beside a backup QB. The candidate scorer needs a preseason
rule and a rostered-everywhere signal. The draft-board critique names Bijan
Robinson and Ja'Marr Chase as "no usage data available" with players on
wrong teams — a pool join to check after the §23 bridge. All three are in
`TODO.md` §0c.

## 25. The cataloguing gate, read from the facilitator's own source

Researched 2026-09-17, after §21's fix shipped (`api-00024` at `e049217`) and
the listing still had not been confirmed. §21 identified the missing field
correctly but described the mechanism from the outside. The mechanism is
readable: `x402/extensions/bazaar/facilitator.py`, in the SDK we already depend
on, **is the facilitator-side reference implementation**. Everything below was
verified by running it against our real 402s, not inferred.

`extract_discovery_info(payment_payload, payment_requirements)` decides listing,
and three properties of it matter:

1. **It reads the *payload*, not our 402.** `payload["resource"]["url"]` and
   `payload["extensions"]["bazaar"]`. So `web/js/wallet/envelope.js` echoing
   `resource` and `extensions` is not politeness — it *is* the cataloguing
   mechanism. A client that drops `extensions` settles and lists nothing.
2. **It validates `info` against the `schema` we ship beside it**, then on
   failure logs a warning *to the facilitator's own stderr* and returns `None`.
   The payment still settles. From our side success and silent delisting are
   byte-identical, which is why §21 took a week to find.
3. **The row key is `METHOD` + `origin+pathname`** (query and fragment stripped).
   Cataloguing is therefore **per endpoint, not per merchant**: ten endpoints
   need ten settles. A merchant record exists only once ≥1 resource is listed,
   which is why `/discovery/merchants/MDBJMM6R…` returned a 404 rather than an
   empty record.

### §21's actual failure mode was worse than "rejected"

A missing `method` **passes** validation — it is only in `properties`, not
`required` — and `_get_method_from_info()` then returns the literal string
`"UNKNOWN"`. So those twelve payments were not refused; they were offered to the
catalogue under `base64("UNKNOWN:https://api.playclock.xyz/…")`. Nothing
anywhere errored. `bazaar_extensions()` now adds `method` to
`schema.properties.input.required` and narrows its `enum` to the one method the
route serves — exactly what `bazaar_resource_server_extension.enrich_declaration`
does, and what every official GoPlausible example ships
(`"required": ["type", "method"]`). That converts a silent mis-listing into a
validation failure a test can catch.

### Three ways to settle and never be listed, all silent

`tests/test_x402_bazaar_discovery.py` runs the real gate on every endpoint and
pins all three:

- **`method` absent** → catalogued as `UNKNOWN` (above).
- **Any extra key in `info.input`** → the generated schema sets
  `additionalProperties: false`, listing only `type`, `method` and
  `queryParams`/`bodyType`/`body`. Even the spec-legal `headers` fails. Widening
  `info.input` means widening the schema in the same change.
- **A `method` contradicting its variant** (`POST` on a query block) → raises
  inside `parse_discovery_extension`, which `extract_discovery_info` swallows in
  a bare `except`. `bazaar_extensions()` refuses to build it.

### The signal we were not reading

The x402 spec defines an **`EXTENSION-RESPONSES`** response header on
verify/settle: base64 JSON, `bazaar.status` ∈ `success|processing|rejected` plus
`rejectedReason`, explicitly *server internal, never forwarded to the buyer*.
It is a direct answer to "was this catalogued?" and would have reported §21 on
the first settle. `x402-avm` 2.0.2 has no code for it and the spec says
facilitators *may* send it, so `HttpFacilitatorClient` reads the raw header via
an httpx response hook and logs `rejected` at **error** level. An absent header
is silence, not success.

### `x402-merchant`: the listing metadata we were leaving to a guess

GoPlausible's FastAPI, Flask and Express challenge examples all ship **two**
extensions, and we shipped one:

```python
extensions = {"bazaar": BAZAAR_EXT, "x402-merchant": MERCHANT_EXT}
```

`x402-merchant` carries `{name, website, logo, categories}`. The scraping
behaviour documented above under "Leaderboard mechanics" — `<title>` for the
name, `/apple-touch-icon.png` for the logo, truncated `payTo` when neither
resolves — is the *fallback* for merchants who do not declare it. `categories`
has no fallback at all, and it is what an agent browsing the Bazaar filters on.
It is now built from `X402_MERCHANT_*` and rides on every 402.

Note it is merchant identity, not per-resource identity. The spec does define
per-resource `serviceName`, `tags[]` and `iconUrl` on `resource` — but the SDK's
three-field `ResourceInfo` drops them exactly as it drops `resource.method`
(§21), so all ten endpoints necessarily carry the same merchant block.

### Verified 2026-09-21: catalogued on the first settle after this shipped

`api-00025` (`955326f`) deployed the two changes above — `x402-merchant` and
`required: ["type", "method"]` — and the first MainNet settle against it was
listed within the same second:

| | |
|---|---|
| payment | `GET /v1/trending`, 0.10 USDC, txid `EH5RQLRXSPICYF5VVYRXTFJM7BVPMHCNXC7WUWYHJIBXIKAUD4CA` |
| payer | `playclock-mainnet-payer` via `examples/agent/fantasy_agent.py` — its first ever transfer |
| merchant record | `/discovery/merchants/TURCSk1NNlJKNFRNN1c1RklUWjNNV0pU`, firstSeen `01:52:05.508Z`, name/website/logo/categories all from the merchant block |
| resource | `/discovery/resources?search=playclock` → one row, firstSeen `01:52:05.657Z` |
| leaderboard | `bazaar: false` → `bazaar: true`; `sub` changed from the hostname to the first category |

Three things this one settle established that the SDK gate could not:

1. **§21's fix alone was not enough.** Two settles landed against `api-00024`
   (`method` present in `info.input`, absent from `schema.required`, no
   merchant block) on 09-10 and 09-21, and neither was catalogued — even
   though `extract_discovery_info` from the SDK accepts that exact 402 for all
   ten endpoints (checked against the live 402s the same evening). The live
   facilitator is stricter than its published reference on *something* in the
   diff between `api-00024` and `api-00025`. One settle cannot say which of
   the two changes it was; the merchant record landing 150ms before the
   resource is consistent with merchant-first, and no more than that.
2. **`feePayer` is not causal.** Every Algorand row in the catalogue carried
   one (96/96 on the page sampled), and the `experiment/testnet-fee-payer`
   branch settled a control (`testnet-a`, none) and a treatment (`testnet-b`,
   sponsored) pair against the real facilitator on 2026-09-12. Neither host
   is in the catalogue. The correlation is everyone else following the
   GoPlausible examples, which ship `feePayer` *and* `x402-merchant`.
3. **The facilitator sends no `EXTENSION-RESPONSES` on settle.** The hook in
   `HttpFacilitatorClient` logged nothing on a settle that *was* catalogued,
   so an absent header is silence in both directions. The catalogue query is
   the only signal there is:

```bash
# merchantId is base64(payTo[:24]); `search=playclock` is unreliable — it
# returned one row while the merchant record listed eight.
curl -s "https://facilitator.goplausible.xyz/discovery/resources?merchantId=TURCSk1NNlJKNFRNN1c1RklUWjNNV0pU&limit=100" \
  | jq '.items[] | {resourceUrl, method, settleCount, firstSeen}'
curl -s "https://facilitator.goplausible.xyz/discovery/merchants/TURCSk1NNlJKNFRNN1c1RklUWjNNV0pU" \
  | jq '{name, categories, resources: [.resources[] | "\(.method) \(.resourceUrl)"]}'
```

**All ten are listed**, as of 2026-09-22 12:30 ET. The row key is per
method+path, so each needed its own settle: Ryan authorised one payment per
endpoint from the payer wallet, 2.70 USDC across ten calls, 10/10 settled and
every one catalogued on its first settle. That is the §14 line — one
validation settle per endpoint, never again by us. **The payer wallet is now
spent as a validation instrument**: any further payment from it is a repeated
self-payment, which §14 makes excludable. Volume from here has to come from
somebody else's agent.

The eight that came after `trending` split into two runs. Seven needed no
identity (`sleepers`, `waivers`, `report`, `draft-board`, `player`, `matchup`,
and `roster` through the manual-roster path). The last two did, and that is
the part worth writing down.

### The identity the paid draft endpoints need, and how to find one

`team-report` and `draft-report` resolve a real Sleeper account, and a 404
never settles — so an unlisted endpoint stays unlisted until a *resolvable*
identity is found. The test account has no 2026 leagues (it never did; that is
not a bug), so the search is for a public username that does. `boss`
(`3719834651475968`) has three in-season 2026 leagues; Best League Ever
(`1333584309650460672`, draft `1333584309667250176`) was used for both.

Two things to check against Sleeper's public API *before* paying, because both
are invisible from the endpoint's side until the money has moved:

- **The roster resolves.** `/league/{id}/rosters` must contain a row whose
  `owner_id` is that user. For `boss`: 26 players, 10 starters.
- **The draft identity is unambiguous.** `boss`'s `picked_by` entries span
  **two** `draft_slot`s in every one of their leagues — an autopick artifact,
  and exactly the case that grades a blend of two teams. The route checks
  `draft_slot` before `picked_by`, so passing `draft_slot` explicitly (6, read
  from the draft's own `draft_order` map) is what makes the answer one team's.
  Read the seat from `draft_order[user_id]`, never from the picks.

The drafts are shallow — four rounds — so the report is thin, but real and
honest: it graded `boss` an F for zero values and three reaches. A thin true
answer still lists the endpoint, which is all a validation settle is for.

One more deploy-discovered fact: the merchant-block default named
`https://playclock.xyz/apple-touch-icon.png`, which **404s** — the web image
ships no icon, only the api image does (`api/static/`). The default now says
`api.playclock.xyz` and the service carries it as an explicit env var.

## Challenge rules — verified 2026-08-29 against the Official Rules PDF

Read directly from
https://algorand.co/hubfs/x402%20competition%20Official%20Rules.pdf. This
supersedes the earlier "verify immediately" timeline flag, which hedged the
Sept 1 date and guessed at a single deadline. There are **two** gates, and they
were being conflated:

| § | Gate | Window |
|---|---|---|
| 5(a) | **Program Registration** | June 12 → **11:45pm ET Sept 1, 2026**; the form is disabled after |
| 5(b) | Final Presentation Registration | Sept 2 → Sept 29, 2026; form emailed to registered entrants |
| 5(c) | Shortlist | Sept 30 → Oct 8; finalists notified Oct 9 |
| 5(d) | Final Presentation | Nov 2, 2026 |
| 5(e) | Winners announced | by Nov 12, 2026 |

The algorand.co blog's "we'll share the submission form closer to the deadline"
refers to **5(b)**, not 5(a). The PRD's assumption of a Sept 8 submission was
wrong in both directions: registration is a week earlier, project submission is
three weeks later.

**Registration asks nothing about the endpoint.** The form is a HubSpot embed on
the challenge page (portal `26119259`, form
`d20e17c1-d646-4033-a580-e1f64844cbf6`): first name, last name, email, country,
plus optional project name and description. No captcha; submitting is itself
agreement to the Official Rules. §6 does require a MainNet endpoint on the
GoPlausible facilitator to *participate*, but that is enforced through the
leaderboard in October, not at registration. So registration was never blocked
on the MainNet deploy — the two were treated as one item for weeks.

**§8 judging is four evenly-weighted criteria**: Volume (USDC processed),
Use case quality (x402 in the core flow, not bolted on), Sustained potential,
Innovation. Volume is 25%, not the whole game.

**§14 forbids manufactured volume** — "artificial volume, wash transactions,
repeated self-payments, or other activity intended to manipulate leaderboard
results," excludable at the Administrator's sole discretion. GoPlausible
independently classifies localhost and self-payment loops as DEV traffic. One
self-settle to validate MainNet is required and expected; a loop is a
disqualification risk. Prizes: $25k/$22.5k/$20k/$17.5k/$15k, plus 500,000 ALGO
across the top 20 on the leaderboard. Finalist eligibility needs top 50 *and* a
submitted project description.

**§7**: one team per person, one project per team. Entry type for this project
is **Composite** — multiple endpoints under one project sharing one `payTo`.

### Leaderboard mechanics and field size, sampled 2026-08-29

`GET /data/leaderboards` on the facilitator **defaults to `limit=12` and
`range=24h`**. A bare call returns 12 rows and reads like a 12-entrant field; it
is not. Pass `?src=x402-global-challenge&limit=500&range=all`:

| window | merchants | volume (USDC) | settles |
|---|---|---|---|
| 24h | 24 | 431 | 21.8k |
| 7d | 51 | 1,868 | 108k |
| 30d | 98 | 22,053 | 401k |
| all | **107** | **22,209** | 407k |

`limit` caps the returned page at 50 regardless; `total` carries the real count.
30d ≈ all-time, so the field materialized almost entirely in the month before
the Sept 1 registration close. Cut lines: **rank 20 ≈ 23.25 USDC**, rank 50 ≈
0.30 USDC. `bazaar` and `challenge` are independent flags — ranks 1 and 2 carry
`challenge: true`, `bazaar: false`.

**`label` and `logo` are scraped from the resource origin's root, not from the
402 payload.** Verified across all ranked hosts:

- Root serves HTML → the leaderboard name is its `<title>` or `og:title`
  (IoMarkets, AgentHub, TENDRIL, Scrape402, AgentMesh all match exactly).
- Root 404s or returns bare JSON → the name falls back to a **truncated payTo
  address** (`SGLTUP…SPPI` at rank 1, `QSNLPP…U6EY` at rank 4).
- `logo` is populated only where **`/apple-touch-icon.png`** resolves at that
  exact conventional path — Syra, One Step Chess and TENDRIL return 200 there
  and carry logos; every other ranked host 404s and has `logo: null`, including
  hosts that *declare* an apple-touch-icon at a non-standard path (Scrape402
  points at `/public/logo.png` and still gets nothing).

Consequence for us: the API origin currently returns JSON at `/` and 404s on
`/apple-touch-icon.png`, so as deployed it would list as a wallet address with
no logo. Fixing that is a static-file change, but it has to happen **before the
first MainNet settle**, because the catalogued origin is one-shot.

### The competing field, sampled 2026-08-29

908 MainNet-Algorand resources across 65 hosts are in the Bazaar catalog — far
more hosts than have ever settled, so listing ≠ volume. Two shapes dominate:

- **High-frequency micro-priced feeds.** Rank 1 (`x402-quant-signals`) at 143,954
  settles × exactly 0.10; One Step Chess at 154,827 settles × 0.0009. Endpoint
  farms are common: `agent402.tools` publishes **581** resources, `vead.app` 105,
  `algorandtracker.com` 34.
- **Low-count, high-value services.** IoMarkets Topup: **one** settle at 15.31
  USDC (agents buying real mobile top-ups). TENDRIL at ~0.50/call.

Prices cluster at 0.001–0.10; our 0.10–0.75 is at the top of the field. Every
serious entrant writes resource `description` fields as *tool descriptions for
an LLM* ("Use when an agent assesses macro DeFi health…"), not as marketing.
Syra, the highest-ranked named entrant, is a pre-existing Solana x402 product
that added Algorand and quotes the same resource on Algorand, Solana and Base
simultaneously — its volume is imported, not built for this challenge.

## TestNet validation checklist (first task on an unblocked network)

The example agent (`examples/agent/`) exercises everything except the real chain.
When validating on TestNet, confirm specifically:

**Status 2026-08-28 — ALL SEVEN VERIFIED.** Run end to end against the live
Cloud Run TestNet deployment with a funded, opted-in payer. Two real settles of
0.10 USDC each landed, receipts written, replay proved idempotent.

One further finding, which answers the question this checklist could not:
**a TestNet settle does not register in the facilitator's catalogue.** After a
successful challenge-tagged TestNet settle, `/data/leaderboards` still showed 12
rows with no `run.app` entry. So TestNet validation does not consume the one-shot
Bazaar listing, and the MainNet host is still an open choice.

1. **Fee-payer expectations** — VERIFIED. Our 402's `extra` has no `feePayer`, the
   agent sent a plain single-txn group, and GoPlausible accepted it: it simulated
   the transaction and rejected only on balance, never on group shape. (Note
   `GET /supported` *does* advertise an Algorand `feePayer`, so the facilitator
   will sponsor fees — but it does not require us to use that slot.)
2. **USDC opt-in on both ends** — VERIFIED as required, and now done on TestNet
   for both wallets via `infra/optin.py`. Payer AND `X402_PAY_TO` must be opted
   into the USDC ASA or settlement dies at simulate (surfaces as a second 402,
   unbilled).
3. **Amounts** — VERIFIED. A 0.10 quote (`"100000"` atomic) moved exactly 0.10
   USDC on chain: merchant +0.10, payer -0.10. Not micro-units.
4. **Receipt semantics** — VERIFIED. GoPlausible returns the asset-transfer txid
   for the group, and `payer` does match the signer address. The `receipts/`
   document carries endpoint, amount_usdc, payer, txid, network and ts;
   `failed_paid_calls/` stayed empty.
5. **Header size** — VERIFIED. A real `PAYMENT-SIGNATURE` of **3496 base64 chars**
   went through Cloud Run's proxy intact and reached the facilitator, which parsed
   it and simulated the transaction. No proxy limit hit.
6. **Idempotent retry against the real facilitator** — VERIFIED. Replaying an
   identical request with the same `PAYMENT-SIGNATURE` returned 200 with the same
   body and charged nothing further (payer held at 9.80 USDC across the replay).
   No double settle.
7. **SDK docstring bug (found during the example build)** — VERIFIED that our
   workaround is right: `AlgorandSigner` produced a group the facilitator parsed
   and simulated. The `ClientAvmSigner` example in `x402.mechanisms.avm.__init__`
   doesn't run as written — the scheme passes raw msgpack bytes but
   `algosdk.encoding.msgpack_decode/encode` speak base64 strings. Ours does both
   hops; don't copy the SDK example.

## 26. The bill was the warming loop, not the customers

Measured 2026-09-21 from the billing console, Cloud Monitoring and three days
of ingest logs. None of it is derivable from the code, and the first instinct
("the ADK pipeline is expensive per call") was the wrong diagnosis.

**The numbers.** September 1–21 cost $105.37, ≈ $5.02/day. Vertex AI was
≈ $3.27/day of that (65%); the *Text Output* SKU alone ≈ $2.03/day. Cloud
Monitoring for the trailing week: ~200 `gemini-3.7-flash` calls a day, ~2.1M
input tokens, ~510K output tokens — ~2.5K output tokens per call for
structured-output responses, which is thinking. Over the same period the api
served **3 paid calls a day**, each one narrator call; `/v1/stats` showed 14
paid analyses and $2.50 USDC since MainNet opened. Essentially every model
call was the ingest job.

**What the job was doing.** 22–24 full ADK board generations a day. The 2h
cadence with a 2.5h refresh window revisits a 6h board every **4h**, not the
3.5h the runbook estimated (skip at 4h left, regenerate at 2h left), so
`trending`, `sleepers` and `waivers` each regenerated 6×/day and `report` and
`draft_board` ~2.3×/day. Each generation is ~9 Vertex calls — the stats
agent's tool loop, the synthesizer, the judge — at ~90K input / ~22K output
tokens. And 38 of ~105 pipeline runs over three days died on
`_ResourceExhaustedError` (Vertex 429 under dynamic shared quota): google-genai
retries nothing unless asked (`stop_after_attempt(1)` with `retry_options`
unset), so a 429 on any one call failed the run, and both retries above it —
the pipeline's `MAX_ATTEMPTS` and precompute's — started over from the first
call. About a third of the pipeline runs paid for were thrown away.

**The insight, and its first, wrong form.** The numbers behind `sleepers`,
`report` and `draft_board` — the usage rollups and weekly lines — change when
`stats` runs (Tue/Thu/Sat) and the player metadata when `nightly` runs
(daily). Regenerating those boards every 4–10h looked like paying Gemini to
re-narrate the same numbers: a different paraphrase, the same board, at full
price. That is half right. What it missed is that `sleepers` and `report` also
read `trending` (see below), so their inputs *do* move intraday; what is true
is that the boards' *calls* hold for a day, which is a statement about the
right TTL, not about which datasets changed.

**What was tried first, and why it was wrong.** The obvious fix was "renew,
don't regenerate": a board inside the refresh window whose inputs had not been
re-ingested since it was built would get its `expires_at` pushed out instead
of a pipeline run, with the body's own `meta.data_freshness` compared against
the store's markers over the endpoint's `REQUIRED_DATASETS`. Built, tested,
and caught in review (PR #65, Codex): **`REQUIRED_DATASETS` is the readiness
table, not the dependency table.** It lists what must exist for a board to be
sellable, and `sleepers` and `report` both read `trending` without listing it
— `_sleeper_candidates()` excludes anyone at ≥ `CONSENSUS_ADD_COUNT` adds and
cites `trend_count`; `_report_views()` attaches add counts to every player. A
renewed sleepers board would have kept selling a player as a sleeper after the
crowd claimed him, until the nightly `players` marker moved. And it is not
fixable with a better table: under `ENGINE=adk` (how the job warms boards)
`StatsTools.function_tools()` hands every tool to every board's stats agent,
so the model may read any dataset for any board. The only comparison that
needs no hand-maintained table is "every marker", and `trending` is restamped
every 30 minutes, so that comparison never says "unchanged" in season. A
content digest of trending coarse enough to hold across 4h (membership of the
≥ 3,000 set, top-N ids) is a per-board judgement about what "stale" means —
the same hazard, made explicit, for modest savings. Dropped.

**Decisions, shipped 2026-09-22:**

1. **`sleepers` and `waivers` are cached 12h, not 6h.** `trending` stays at 6h:
   it is about the last 24h of adds and drops, and the free preview shows the
   live counts, so a stale paid board would visibly disagree with it. Sleepers
   are a usage-trend call over weeks and waiver claims process daily; both hold
   for 12h, the post-stats `--force` refresh still rebuilds them the moment the
   numbers actually change, and `/v1/catalog` advertises the new number, which
   is what makes this the honest version of the same saving. Each goes from 6
   generations a day to 2.4 — the same ≈ 7/day the renewal design promised,
   with nothing new to maintain. This is the lever for the warming cost: the
   TTL, not the loop interval, and it is a product decision.
2. **Retry the request, not the pipeline.** `api/agents/vertex.py` is the one
   place the Vertex retry policy lives; the ADK `Gemini` model object, the
   narrator's client and the judge's all carry it. `MODEL_RETRY_ATTEMPTS`
   (default 4; the ingest job runs 6) with 2s initial delay doubling to a 30s
   ceiling, SDK default status set (408, 429, 5xx) so a 404 still fails fast.
   The outer retries stay as the last resort they were meant to be.

**Expected effect:** ~16 generations a day instead of 23, ~0 wasted runs
instead of ~13, so roughly 16 pipeline runs a day where there were 36 —
Vertex from ≈ $3.30 to ≈ $1.50/day. Verify from the precompute summary
(`warmed` per run), the Monitoring `model_invocation_count` by
`response_code`, and the billing SKU line a week on.

**Left on the table, in order of size:** thinking tokens (nothing sets a
`ThinkingConfig`; the stats agent's mechanical tool round-trips think at output
prices — check `usage_metadata.thoughts_token_count` on one run before capping);
the api's `--min-instances=1` at 2 vCPU / 2Gi, sized for ADK-per-request and
now running one small narrator call per paid request; 10.5 GB of images in
Artifact Registry with no cleanup policy; and a `us-east10` line
(+$9.73 for the month) nothing we deploy explains — click it in the billing
report.

## 27. Two cost levers measured against the gate; one survived

Measured 2026-09-22, the day after §26 shipped, asking two questions that look
like free money and are not. Both were answered by running `api/evals` against
real Vertex rather than reasoning about them, which is the only reason the
second one did not ship.

### gemini-3.8-flash: works, costs the same, runs 3.5x slower

`gemini-3.8-flash` is GA (2026-09-02), resolves at `GOOGLE_CLOUD_LOCATION=global`
and 404s at `us-east4` — the same one-way pairing as 3.7 (see "ADK on Vertex").
Structured output, function tools and `googleSearch` grounding all work.

It is **not cheaper**: both are $0.75/$3.75 per 1M input/output on the
introductory rate through 2026-12-31, then $1.50/$7.50. So the only question is
quality against latency, and the golden suite answered it:

| | 3.8-flash | 3.7-flash |
|---|---|---|
| golden suite | 22/23 | 21/23 |
| wall clock | **85.6 min** | **24.4 min** |
| median case | 126s | 57s |
| Vertex calls | 125 | 125 |
| 500 / 429 | 8.8% / 3.2% | 8.0% / 0% |

Same call count and the same server-error rate, so the 3.5x is the model, not
retries. (The 500s are a Vertex-wide condition that day, not a 3.8 defect — an
earlier reading that blamed 3.8 came from a query whose alignment period
bucketed both halves together. Check the window before believing a rate.)

**The latency is disqualifying, not merely unattractive.** `report` takes 988s
on 3.7; at 2.9x it exceeds the ingest job's `--task-timeout=45m` and the
precompute task is killed mid-run. `player_by_name` went 65s to 17.5 min. This
is the latency-money coupling in one sentence: settlement precedes the handler's
return, so a model slower than the timeout charges a caller for an answer the
proxy then cuts off. The +1 case is a single non-deterministic run and is inside
the suite's own variance (below). **Stay on 3.7.**

### Capping thinking: 78% of the output bill, and it buys the grounding

Thinking tokens bill at the **output** rate, and output was the largest SKU in
§26. On a realistic board prompt, five runs each: default spent a mean of 562
thinking tokens against 154 tokens of answer; `thinkingLevel=low` spent **zero**,
produced the same number of rows and the same answer length. A 78% cut of the
dominant SKU, apparently free.

It is not free. The golden suite with `MODEL_THINKING_LEVEL=low`:

| | passed |
|---|---|
| default thinking | 21/23 |
| `thinkingLevel=low` | **11/23** |

Sixty-five-plus failures, nearly all one class — *"quotes 20.6 which is in
neither stats_cited nor the body"* — plus `team_report_without_analytics`
failing with *"manager metrics were invented although no analytics were
supplied"*. That is TECH_SPEC §5's first risk, reproduced on demand.

So thinking is not buying eloquence, it is buying **the discipline of tracking
which numbers came from a tool**. Remove it and the model confabulates
plausibly. `MODEL_THINKING_LEVEL` exists as a seam and is **off**; this table is
why, and the knob is kept so the next person to have this idea can re-measure in
one command instead of rebuilding it.

Note the shape of the trap: a cheap proxy (row count, answer length) said the
capped output was identical. Only the gate disagreed. §24's lesson in the other
direction — there, an answer that passed every test failed the customer; here, an
answer that passed every cheap check failed the gate.

### The suite is flaky at 19-21/23, in that same failure class

Three uncapped 3.7 runs the same evening scored 21, 19 and (capped) 11. Between
the two uncapped runs `waivers_big_board`, `waivers_top_five` and
`draft_board_full` each flipped PASS to FAIL with ungrounded numbers in
`rationale` and `reasoning`, and `sleepers_week4` failed in both. Nothing in
between changed those code paths.

**Do not read a single ADK eval run as a quality measurement**; the number moves
by two cases on identical code. More importantly this is a live product issue,
not a test artifact: the same class shows up in production as
`board quality flagged` on warmed boards. The narrated engine closes the door in
Python for the api's own answers, but boards are warmed through ADK. Worth its
own investigation — it is the one risk the PRD calls the most expensive to get
wrong.

### team_report's computed block is now copied, not requested

The one eval failure that was ours rather than the model's:
`team_report_with_analytics` failed on **every** model tested, with
`manager_review.mis_start_patterns[0] quotes -11.4 which is in neither
stats_cited nor the body`. The number was real — `api/data/team_analytics.py`
computes it — and the prompt asks for two things at once: reproduce
`manager_review` verbatim *and* put every analytics number quoted in prose into
`stats_cited`. Models did the first and not the second, which is unsurprising: a
field you were told to copy does not feel like a number you are quoting.

Fixed on both sides. `enforce_computed_facts` (api/agents/pipeline.py) overwrites
`manager_review` and `positional_strength_vs_league` with the block from
`request.team_analytics` before validation, keeping `observations` — the one key
the analytics leave empty for the model. The gate then exempts those paths
(`_COMPUTED_PROSE` in api/evals/quality.py) because they are grounded by
construction rather than by citation; the same number still fails inside
`observations`, which is model prose. This is the draft board's rule
("computed, not synthesized") applied to the other endpoint that hands the model
a finished table: **if the value must match, copy it — do not ask for it.**

## ADK on Vertex — validated 2026-08-28, with two problems

Verified working end to end: a paid `/v1/player` call on Cloud Run returned real
LLM synthesis (`model: gemini-3.7-flash`, 7 grounded stats, 2 sources, a verdict
that actually reasons from snap share and red-zone touches).

**1. The model id only resolves on the `global` endpoint.** `gemini-3.7-flash`
404s in `us-east4` *and* `us-central1`; it resolves only at
`locations/global`. Regional endpoints in us-east4 offer just `gemini-2.5-flash`,
`gemini-2.5-flash-lite`, `gemini-2.5-pro` — no Gemini 3 at all. So the deployment
needs `GOOGLE_CLOUD_LOCATION=global`, which is safe: only the Gemini clients
read it (`api/agents/vertex.py` passes it explicitly; ADK's SDK reads it from the
environment), Firestore's location is fixed at database creation, and Cloud Run's
region is a deploy flag. `Settings` defaults it to `global` since 2026-09-26. Pairing `MODEL_ID=gemini-3.7-flash` with
`GOOGLE_CLOUD_LOCATION=us-east4` — which is what the config defaults did — 404s
on every paid call.

**2. Latency is ~72s, and the timeouts were all set below it.** This is worse
than a slow endpoint, because settlement happens *before* the handler returns:

- Observed live. A paid `/v1/player` call charged **0.15 USDC, wrote a receipt,
  and returned nothing** — the client gave up at its 60s default while the server
  was still synthesizing. The agent printed `0.000000 USDC spent, 1 failed`,
  which was wrong; the money had moved.
- The answer *is* recoverable, but only by replaying the identical
  `PAYMENT-SIGNATURE` (verified: replay returns 200 and does not double-charge).
  The bundled agent discards that header on failure, so from the client's side
  the money is simply gone. **Any client that pays should persist the payment
  header until it has the response.**
- Three timeouts disagreed with each other and with reality: agent default 60s,
  Cloud Run `--timeout=60s`, quoted `maxTimeoutSeconds` 120s. The quote is the
  contract; the other two are now 180s and 300s.

Raising timeouts is mitigation, not a fix. Three things were done about the
latency itself (2026-08-28):

**Stats and research now run concurrently.** They write different state keys and
neither reads the other's, so the `SequentialAgent` was costing a full LLM round
trip for nothing. A `ParallelAgent` wraps them, synthesis still runs after both.
Measured on the same `/v1/player` call: **72.5s -> 55.6s**, same 2 sources and a
comparable verdict.

**The league-wide boards are precomputed.** `trending`, `sleepers`, `waivers` and
`report` depend only on the week, so `ingest/precompute.py` generates them on the
ingest schedule and warms `response_cache`; the paid call becomes a Firestore
read. Verified live on Cloud Run: paid `/v1/sleepers` and `/v1/report` both
returned `cache=hit`, and the whole paid `/v1/report` flow — discovery, 402,
signing, settlement, response — finished in **15s**, most of it Algorand.

This is not an optimization, it is the only way those endpoints work. Measured
generation time on real runs:

| Board | Generate | Served warm |
|---|---|---|
| `report` | **988.5s** (16.5 min) | `cache=hit` |
| `sleepers` | 374.6s | `cache=hit` |
| `waivers` | 117.3s | `cache=hit` |

Nothing in that first column could be produced inside a paid request under any
timeout a caller would tolerate. Note 988s also exceeds the ingest job's original
15m `--task-timeout`, which is why it is now 45m.

**Warming runs on its own clock, not the ingest schedule.** Those TTLs are 6h and
12h, so a warmer that only ran with `ingest-stats` (Tue/Thu/Sat) would leave most
of the week cold and hand the first caller of each gap a 374s or 988s generation
— charged, then timed out at the 300s Cloud Run limit. It now runs every 2h and
regenerates a board once it drops below 2.5h of remaining life. Refreshing
*ahead* of expiry is the point: the old entry has to keep serving until the new
one is written, and writing `report` takes 16 minutes. The invariant to preserve
is `interval + slowest board < refresh window < shortest TTL`. This costs roughly
25 board generations a day against a $50/mo budget — deliberate, since the
alternative is endpoints that do not work.

**Warming refuses to run on data that is not there.** `warm_response_cache` calls
the engine directly, bypassing the route's `require_ingested_data`. On a cold or
half-ingested store the deterministic engine returns a schema-valid *empty*
board, and cached under the key the route reads, `cached_analysis` serves it as a
`cache=hit` before the readiness check ever runs — so the 503 that exists to
refuse selling an empty board is skipped, and every caller for the whole TTL is
billed for nothing. It now checks `missing_datasets` per target first. A board
left cold for that reason, or for exhausted retries, raises and **exits the job
non-zero**: a warmer that silently warmed nothing is indistinguishable from a
healthy run while every paid call falls back to the slow path.

**Idempotency TTL raised 60s -> 300s.** The clock starts at *verify*, before the
handler runs, so at 60s with a 72s handler the record expired before the response
existed: a client that timed out and retried got a cache miss and was charged
twice — the exact double-charge the cache exists to prevent.

Still open: the three personalized endpoints (`matchup`, `roster`, `team_report`)
cannot be precomputed and still run ~55s against a <20s p95 promise. Either speed
them up, label them as long-running, or launch them on the deterministic engine.

Vertex quota is a real constraint here: four boards back to back, each now issuing
two concurrent calls, returns 429 RESOURCE_EXHAUSTED. Precompute retries with
minutes of backoff, which it can afford and a paid request cannot.

## Open questions for Ryan (need answers before MainNet, not before code)

1. **Algorand `payTo` address** — need the real MainNet (and TestNet) address; it's
   env config (`X402_PAY_TO`), so any placeholder works until deploy.
2. **GCP project id / region confirm** (`us-east4` per spec?) — needed at deploy time.
3. **Product name** — still "Play Clock" everywhere; renaming later is a
   find-replace in `catalog` + web copy, cheap until Bazaar listing happens.
4. **Refund posture** — design here assumes "never settle on handler failure" (see #2);
   if the facilitator's flow forces settle-before-execute, we fall back to the spec's
   `failed_paid_calls/` + manual refund policy. Decide once facilitator docs verified.
5. **Discord bot v1 vs v1.1** — PRD recommends v1.1; nothing in this build blocks it.

## Ideas worth considering (not built unless noted)

- **Agent tier (`?format=compact`)**: PRD floats $0.05 compact responses. Cheaper:
  same price, but a `format=compact` query param that strips prose and returns only
  the structured verdict block — agents save tokens, we keep margin, no new price SKU.
  Catalog advertises it. (Built into the schema design: `reasoning` is separable.)
- **Receipts as marketing**: `receipts/` gives us per-endpoint revenue and unique-payer
  counts for free — expose an aggregate `/v1/stats` free endpoint late in October
  ("12,431 paid analyses served") as social proof for the finals pitch.
- **Cache-warming cron**: for week-scoped endpoints, have Cloud Scheduler hit the
  generator internally Tuesday morning so the *first* paying customer of the cycle
  also gets a <3s cache hit, not the 20s cold generation.
- **`Idempotency-Key` passthrough**: agents retry; honoring a client idempotency key on
  POST endpoints (returning the cached result for the same key+payment) makes us a
  well-behaved tool call.
- **Eval-as-fixture**: the 20 golden queries double as recorded HTTP fixtures, so the
  eval suite catches upstream API shape drift (Sleeper/nflverse), not just LLM drift.

## Build-time verification log (Tech Spec §11) — research pass, 2026-08-27

Verified by two research agents (PyPI + installed-package introspection + GitHub raw
docs; algorand.co / Google docs / api.sleeper.app are blocked from this environment,
so items marked *secondary* came via mirrors, SDK source, or typed client libraries).

### x402 / Algorand (confirmed unless noted)
- **Python SDK: `x402-avm` 2.0.2** (PyPI, GoPlausible's fork of Coinbase's x402 SDK;
  import name `x402`; alpha status, Feb 2026). Installed with extras
  `[fastapi,avm,extensions]`. No separate "official Algorand" SDK exists.
- **Protocol is x402 V2**: headers renamed — client pays with `PAYMENT-SIGNATURE`,
  server 402 carries `PAYMENT-REQUIRED`, receipt in `PAYMENT-RESPONSE`
  (`X-PAYMENT`/`X-PAYMENT-RESPONSE` are V1 legacy; we accept both inbound).
  V1's `maxAmountRequired` is renamed to `amount` (string, atomic units); wire
  format is camelCase.
- **USDC ASA IDs**: MainNet **31566704**, TestNet **10458941**, 6 decimals
  (triple-sourced: SDK constants, Circle docs, Pera explorer).
- **GoPlausible facilitator**: `https://facilitator.goplausible.xyz`
  (`POST /verify`, `POST /settle`, `GET /supported`). **Gotcha:** the SDK default —
  and GoPlausible's own FastAPI example — point at `https://x402.org/facilitator`;
  using that default would silently forfeit leaderboard attribution. Our config
  requires the URL to be set explicitly in live mode.
- **Challenge tag placement**: `accepts[].extra.tag = "x402-global-challenge"` —
  empirical sample of 500 live catalog records showed 454 use `extra.tag`, 0 use a
  `tags` array; the catalog schema has no tags field. (High confidence, secondary.)
- **Bazaar listing is implicit**: the facilitator catalogs the `resourceUrl` from the
  first settled payment's discovery extension — permanently. Never settle a
  challenge-tagged payment against localhost. No /.well-known involved; discovery is
  in-band via `extensions.bazaar` (`declare_discovery_extension`).
- Could not verify live: the facilitator's actual runtime responses (egress blocked).
  Verify on TestNet from a real network before trusting the shapes end-to-end.

### ADK / Vertex (confirmed by installing google-adk 2.8.0)
- Imports unchanged from 1.x for our usage: `google.adk.agents.{LlmAgent,
  SequentialAgent}`, `google.adk.tools.{google_search, FunctionTool}`,
  `google.adk.runners.InMemoryRunner`. ADK 2.x's four documented breaking changes all
  concern custom session services / agent subclasses — none affect this design.
- `output_schema` (Pydantic) + tools may now be combined (2.7+); synthesis agent uses
  output_schema with no tools anyway.
- `google_search` can't be combined with function tools on one agent by default —
  our research agent is search-only, so unaffected.
- **Model: `gemini-3.7-flash`** (GA Aug 13, 2026). The spec's `gemini-2.5-flash`
  retires ~Oct 16, 2026 — inside the measurement window. *Secondary source; confirm
  the exact id against live Vertex docs at deploy.*
- `GOOGLE_GENAI_USE_VERTEXAI` is deprecated (ADK ≥2.3) → use
  `GOOGLE_GENAI_USE_ENTERPRISE=true`. `GOOGLE_CLOUD_PROJECT`/`GOOGLE_CLOUD_LOCATION`
  unchanged.

### Data layer (nflreadpy 0.1.5 confirmed by live execution; Sleeper secondary)
- Sleeper player objects carry `gsis_id` (string) + `espn_id` (int) + many other
  cross-ids; trending returns `[{player_id, count}]`, limit max 50. Several fields
  frequently null — modeled Optional.
- nflverse join key is `gsis_id` (no sleeper_id column in `load_players()`), and
  **snap counts key on `pfr_player_id`** — a second hop through `load_players()`'s
  `pfr_id` is required. `load_ff_playerids()` (has Sleeper ids) is blocked from this
  environment but should work in prod — optional enhancement.
- **Season-boundary hazard**: `nflreadpy.get_current_season()` returns 2025 until the
  season label rolls in September — never compute season from the calendar year.
  `load_schedules()` with no args pulls ALL history — always pass seasons.
- `firestore.AsyncClient` (google-cloud-firestore 2.29.0) is the async surface; one
  client per app lifespan, injected — matches our Store design.

## Backtest audit, 2026-09-24: three ways the published hit rate flattered itself

An audit of `/v1/stats` found three holes, each biased toward hits. All three
are closed in `ingest/backtest.py` and `api/data/predictions.py`.

- **Kickers always hit.** `STARTABLE_RANK` had `K: 12`, but nflverse
  `fantasy_points`/`fantasy_points_ppr` count no kicking and the ingest keeps
  no FG/PAT columns (`STAT_COLUMNS`), so every kicker line is ~0, the K bar is
  0, and every K add/waiver/sleeper claim hit (`0 >= 0`) while every K fade
  missed. K and DEF are now `UNSCORABLE_POSITIONS`: scored `hit = null`, and
  `summarize()` drops them from every rate even when an earlier run stored a
  boolean, so claims already graded this season stop counting without
  rewriting them. Computing kicker points from FG/PAT would need those columns
  ingested first; until then "unscorable" is the honest answer.
- **The freshness marker is not coverage.** It says a `stats` run finished,
  not that nflverse's file included Monday night; a run that lands before MNF
  is published stamps it anyway and every MNF player scores as a permanent
  scratch. A week is now scored only when every team on its schedule has at
  least one stat line (`teams_missing_lines()`; bye teams are not on the
  schedule). Stat lines with no `team` field vouch for nobody, so a
  collection written without teams waits rather than guesses.
- **A flipped call was two claims.** The id carried the kind, so Tuesday's
  "add X" and Wednesday's "fade X" from the same endpoint were both filed and
  exactly one always hit. Single-player ids are now
  `{season}w{week}:{endpoint}:{player_id}`, so `store.create()` makes the
  first call per player per endpoint per week the only one. Claims already
  filed under the old `...:{kind}:{player_id}` ids are honoured: before
  writing, `record_predictions()` point-reads every old-format id the
  endpoint could have produced (`legacy_prediction_ids()`, at most two) and
  files nothing if one exists. Matchup ids (keyed by group digest) are
  unchanged. Different endpoints still file their own claim on the same
  player — they are different products making different calls. Pre-existing
  duplicate pairs from before the fix stay in the archive as they are;
  scoring is permanent and there is no honest way to pick which of the two
  was "first" after the fact beyond `recorded_at`, which a cleanup can do
  deliberately if it matters.

## Repo audit, 2026-09-25: facts the code alone does not show

- **The facilitator's base64 is lenient, so ours must be too.** x402-avm's
  `decode_base64_transaction` calls plain `base64.b64decode`, which discards
  every character outside the alphabet (Node's `Buffer.from(…, "base64")` does
  the same). Our identity and `lastValid` reads used `validate=True`: one stray
  `!` in `paymentGroup[i]` produced a new idempotency key for the same signed
  transaction (one payment, N concurrent answers) and an unreadable `lastValid`
  (the floor failed open). `payment_identity` now hashes the Algorand **txid**
  of a leniently decoded transaction, which no re-encoding (alphabet, padding,
  junk, non-canonical msgpack) can change, and live mode 402s a payload with no
  readable payment transaction instead of keying on the header per endpoint.
  Rule: anything that gates on a payment decodes it exactly as the facilitator
  does, or stricter parsing becomes a bypass.
- **A failed replay is not a refund.** A replay re-runs the handler on a
  payment that already settled; the server marks the failure with
  `REPLAY_FAILED_NOTE`. Both clients (web `api.js`, `playclock_mcp`) used to
  clear their pending entry on any non-402 app error, so a brief 503 during
  "Retrieve my paid answer" lost the answer for good. They now keep the entry
  on that marker, and on any 5xx while recovering.
- **Kickers have no defense-vs-position signal.** nflverse
  `fantasy_points_ppr` carries no kicking, so every defense "allowed" 0.0 to
  K and tied at rank 1; every kicker read as a rank-1 streamer. K is out of
  `DEF_VS_POS_POSITIONS`, and the engine reads splits only for
  `MATCHUP_POSITIONS` because stores written earlier still hold the K docs.
- **Open, deliberately not changed here:** a payer can make settle fail on
  purpose (move the USDC or burn the lease while an uncached handler runs) and
  still receive the 200. Turning a definitive facilitator rejection into a 402
  would close it, but it reverses the documented "settle fails after a good
  answer costs us an LLM call" posture, so it is a product decision. Watch
  `failed_paid_calls/` for repeat payers.

## Alert filters must match the payload the service actually logs, 2026-09-29

The "board failed the value gate" alert never fired. Its filter was
`textPayload:"board quality flagged"`, but the ingest job logs structured JSON,
so the line lives in `jsonPayload.message`; the flags since 2026-09-20 alone
number over fifty (including a sleepers board served for 12h with nine
ungrounded numbers on 2026-09-27), and not one reached the email channel. The
api is different: the narrator writes plain text to stderr, so its alert's
`textPayload` filter is correct and did fire (2026-09-22). The ingest-failure
alert matches `jsonPayload.message="task failed"` and has fired nine times.

Rule: before trusting a log-matched alert, run its exact filter through
`gcloud logging read` **with an explicit time range** (the command's default
freshness is one day, which makes a working filter look dead) and confirm it
returns the lines it exists to catch. The Monitoring log records alert firings
as `ViolationOpenEventv1` entries, which is the only evidence one ever worked.


## The board alert pages on serious flags only, once a day, 2026-10-09

Once its filter matched (above), the board alert fired 132 times in ten days:
the gate flagged 68 of 131 warmed boards in a week, and an hourly rate limit
turned that into an email an hour. Most single-check flags were a true number
missing from `stats_cited` (sleepers quoting Aaron Jones's real 0.1315 target
share) or the name guard reading "Dropping Waller" and "Ankle Sprain" as
people; the judge scored the same boards 3.75–5.0. The policy now matches
three or more failed checks, a crowd-only board, or a judge flag, and notifies
at most daily. Every flag still lands in `quality/{key}` and the logs.

Replaying 249 historical flags: 165 were serious under the new rule, on 31
days. The alert stays loud because the boards still fail; the lever is the
ADK prose (uncited numbers, context-only player names), not the filter.
Transaction gerunds are now `_NOT_A_NAME` words in the narrator's guard, which
also stops it rejecting honest live rewrites. Injury terms are not: several are
surnames ("Da'Shawn Hand"), and a separator word cuts a name down to a lone
first name the guard never checks. `_INJURY_WORDS` clears a run only when every
word in it is one ("Ankle Sprain"), the way an all-team run is a team.
