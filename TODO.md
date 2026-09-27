# Play Clock — launch checklist

**Status 2026-09-27.** Ranked by what blocks what. Sections below are dated where
they were written; the gate table is current.

Two hard dates, from the [Official Rules](https://algorand.co/hubfs/x402%20competition%20Official%20Rules.pdf) §5:

| Gate | Window | State |
|---|---|---|
| **§5(a) Program Registration** | closed 11:45pm ET Tue Sept 1, 2026 | **DONE — registered 2026-08-31** |
| §5(b) Final Presentation Registration | Sept 2 – Sept 29, 2026 | **IN PROGRESS** — form received by email Sept 15; repo, demo video and Electric Capital step outstanding |
| §5(c) Shortlist | Sept 30 – Oct 8; notified Oct 9 | top 50 on leaderboard required |
| §5(d) Final Presentation | Nov 2, 2026 | — |
| §5(e) Winners announced | by Nov 12, 2026 | — |

The Sept-1-vs-September ambiguity that `DESIGN_NOTES.md` flagged is **resolved**:
they are two different gates. §5(a) closes Sept 1 and is the one at risk; the
"submission form shipping closer to the deadline" the blog mentions is §5(b),
which is emailed to entrants afterwards. Miss Sept 1 and there is no §5(b).

---

## 0. Register — **DONE 2026-08-31**

Registered through the HubSpot form on `algorand.co/global-x402-challenge`. It
asked only for contact details and an optional project name and description,
so registration was never gated on MainNet being live. §5(b)'s submission form
is emailed to the registered address; there is no other channel for it.

§5(c) shortlisting reads "both leaderboard review and the project information
submitted", so the description matters. The current one, reused for §5(b):

> Play Clock turns NFL fantasy football analysis into a pay-per-request API:
> ten endpoints priced $0.10–$0.50 in USDC on Algorand, sold one analysis at a
> time to humans and to AI agents. Fantasy advice today sits behind $50–$100
> season subscriptions, which no agent can buy and most casual players will not
> pay for; x402 makes a single start/sit call, waiver board or roster audit
> purchasable in one request with no account, no API key and no subscription.
> Every answer is computed from nflverse and Sleeper data; Gemini writes the
> prose under a guard that rejects any number or name not in the computed body,
> and the league-wide boards are generated ahead of time by a Google ADK
> pipeline and scored by a value gate before anyone pays for them. Every
> verdict sold is graded against the week's actual points, and the hit rate is
> public at /v1/stats. Payment is the core flow, not a wrapper: the 402 carries
> the Bazaar discovery block, settlement runs through the GoPlausible
> facilitator after the answer succeeds, and each endpoint is listed in the
> Bazaar.

---

## 0b. The ten development tasks before you advertise

Checked against the code on 2026-08-30, not against this file. Ranked by what
breaks if you advertise tomorrow. Sizes are rough: S = an afternoon, M = a day
or two, L = more.

**The human funnel is dead, and that is the headline.** Everything below #1
assumes people can pay; today they cannot.

### 1. Wallet signing in the web UI — **BUILT 2026-08-30**
Pera and Defly, via `web/js/wallet/`. Only that directory is bundled (esbuild →
`dist/wallet.js`); `payment.js` loads it with a dynamic import, so nothing that
does not pay downloads 1.5MB and local dev still needs no build.

`web/` therefore gains a build step — `package.json`, a committed lockfile, and
a CI job that runs `npm ci && npm test && npm run build`. The alternative was
committing a minified bundle with no lockfile and no reviewer, which is worse.

The two files that decide whether the right money moves are kept out of the
bundle and tested in node with no wallet, browser or network: `exact-avm.js`
(what we sign, 19 tests) and `envelope.js` (what we send, 7 tests).

Also shipped: `Dockerfile.web` + `infra/nginx.conf.template`, a `web` step in
`infra/cloudbuild.yaml`, and `infra/deploy.md` §3b — the UI had **no deploy path
at all** before this.

**Still to do before it earns a payment:**
- [x] Deployed at `https://playclock.xyz` and `https://www.playclock.xyz`, with
      production calls pointed at `https://api.playclock.xyz`.
- [x] **Signed a payment through the public web UI with a real wallet on
      TestNet** on 2026-08-30. Wallet connection, signing, settlement, and the
      paid response worked through `https://playclock.xyz`.
- [ ] The sponsored (`feePayer`) group shape is implemented from the Python SDK
      but has never been seen live. Play Clock's 402 does not offer one.

### 2. Season rollover guard — **BUILT 2026-08-30**
`stale_season()` compares the season on `meta/schedule_weeks` (what ingest
actually wrote) against `SEASON` (what the service is configured for). A
mismatch is a 503 naming both years, unbilled; `ingest/precompute.py` refuses
the whole run and exits non-zero, because a board warmed from last season is
served as a `cache=hit` for its entire TTL.

The guard caught the stale 2025 store before launch. Completed on 2026-08-30:
- [x] Ran `ingest --task schedule --season 2026` and confirmed
      `meta/schedule_weeks.season` becomes 2026. A full 2026 stats ingest cannot
      run before Week 1 because nflverse has not published its weekly-stats file;
      the schedule-only task exists for this honest preseason split.
- [x] Re-ran `precompute`; trending, sleepers, waivers, report, and draft board
      were warmed for the 2026 store.
- [x] Confirmed `https://api.playclock.xyz/v1/health` reports 2026 week 1.

### 3. Latency on the personalized endpoints — **MEASURABLE NOW**, decision open
The fix still needs a number nobody has. What shipped is the instrument:
`RESEARCH_ENDPOINTS` overrides which endpoints run the `google_search` agent, so
the experiment is an env change rather than a code change.

```bash
RESEARCH_ENDPOINTS=none            # no search anywhere
RESEARCH_ENDPOINTS=trending,report # only the cached boards pay for it
```

- [x] **Ran it against Vertex and timed a paid `/v1/roster`.** With the API on
      `RESEARCH_ENDPOINTS=none` (the separate ingest job researches the cached
      `trending,report` boards), a cache-miss manual roster audit took **53.637s**
      end to end and settled 0.50 TestNet USDC successfully. Search is therefore
      not the main cause of the personalized-endpoint latency; §1.2 still needs
      a launch-policy choice.

### 4. A real fallback when ADK fails — **BUILT 2026-08-30**
`FallbackAnalysisEngine` wraps the ADK engine and answers from the deterministic
one when it fails, rather than 500ing. `ENGINE_FALLBACK=false` opts out.

The trade is deliberate and is written up in `DESIGN_NOTES.md` §17: a 500
settles nothing, a fallback answer is billed. It holds only because the fallback
is grounded in ingested stats (the golden citation check passes against it) and
says what it is — `meta.model` is null.

- [ ] **Add the alert.** `infra/deploy.md` §6 now lists a log-based metric on
      the degrade line. One fallback is fine; a sustained rate means ADK is down
      and every caller is quietly paying LLM prices for heuristics.

### 5. Single-flight on a cold cache key — **BUILT 2026-08-30**
`_generate_once` collapses concurrent callers on one cold key onto a single
generation; the leader writes the cache, the followers each validate their own
model from the shared payload.

Scope, stated rather than implied: **per process.** Cloud Run runs up to ten
instances, so a truly simultaneous cold start can still produce one generation
per instance. That is a tenth of the problem and needs no distributed lock; the
other tenth is what warming is for.

### 6. A page and an icon at the API origin — **BUILT 2026-08-30**
`GET /` now serves HTML with a `<title>` and `og:title` of "Play Clock", and
`/apple-touch-icon.png` resolves at the exact conventional path the leaderboard
probes. The old JSON pointers survive under `Accept: application/json`.

Both live in the **API** image on purpose: the origin the Bazaar catalogues is
the one that served the 402, and that is this one — not the web service.

### 7. Point every default at the real host — **PARTLY DONE**, blocked on §1.1
Still blocked on the domain decision, so the default base URL is unchanged. What
shipped is the guard against the failure it causes: the MCP server now reads the
catalog's `network` at startup, **logs a warning** when a real wallet is pointed
at TestNet, and reports `network` and `spending_real_usdc` from
`playclock_wallet`. Someone paying worthless USDC now finds out.

- [x] Domain chosen and registered 2026-08-30: `api.playclock.xyz` is the one
      true API host. `DEFAULT_BASE_URL` and `infra/deploy.md` §0 point there;
      deployment sets `X402_RESOURCE_BASE_URL` to the same origin.
- [x] Old browsers that had persisted a blank `playclock.apiBase` now migrate
      automatically to `https://api.playclock.xyz`; before this fix the live UI
      rendered its fallback catalog but submitted paid paths to static nginx,
      which returned 404 before payment.

### 8. Web UI endpoints derived from the API — **BUILT 2026-08-30**
`web/js/forms-from-openapi.js` derives each endpoint's form fields from
`/openapi.json`, the way `playclock_mcp/tools.py` derives its tools. A new paid
endpoint now arrives with a working form and the bounds the server actually
enforces, rather than a hand-written second copy free to drift.

`FORM_SPECS` survives as an **override** for the inputs that are real widgets
rather than labelled text boxes (the two-to-four player repeater, the pasted
roster); a derived field would render the wrong control. Titles fall back to a
humanised key, so an unknown endpoint reads as "Draft board" and not
`draft_board`.

The draft pair is now buyable by a human — it was invisible in the UI before —
and is carried in the offline fallback catalog too.

### 9. Resource descriptions written for an agent — **BUILT 2026-08-30**
All ten rewritten as tool-selection prose: what it answers, a **Use when**
clause, and where relevant which endpoint to prefer instead
(`/v1/player` over `/v1/matchup` for one player; `/v1/roster` over
`/v1/team-report` without league history). These are the strings the Bazaar
catalogues and an agent reads before deciding to pay.

### 10. Proof and cost control — **BUILT 2026-08-30**
- `GET /v1/stats` — free, built from settled receipts: paid analyses, unique
  payers, USDC settled, per-endpoint breakdown. No payer address is
  republished; how *many* people paid is both the useful number and the safe
  one. Memoised for 60s so a free endpoint never rescans a growing collection.
- `FREE_RATE_LIMIT_PER_MINUTE` (default 120, 0 disables) caps the free routes
  per client per instance. Payment is the limit on the paid ones — a 402 quote
  is never rate limited.
- **2026-09-22, cost:** the bill was ≈ $5/day with Vertex two thirds of it,
  and essentially all of Vertex was the warming loop regenerating boards
  nobody had bought (14 sales in three weeks). Shipped: `sleepers` and
  `waivers` cached 12h instead of 6h (trending stays the one intraday board),
  and Vertex 429s retried per request instead of restarting the pipeline
  (DESIGN_NOTES §26, including why "renew unchanged boards" was tried and
  dropped).
  Still open, in order of size: cap thinking on the stats agent and judge
  (measure `thoughts_token_count` first); shrink the api's always-on instance
  from 2 vCPU / 2Gi; an Artifact Registry cleanup policy (10.5 GB of images);
  find out what the `us-east10` billing line is.

### 0c. Content quality — **BUILT and DEPLOYED 2026-09-03**

Ryan bought player, sleepers and trending on MainNet and called the content
lackluster. He was right, and the suite could not see it: the golden queries
prove an answer never lies, not that it is worth paying for. What shipped
(DESIGN_NOTES §24): the engine preamble is gone and every reasoning leads with
its claim; `api/evals/quality.py` is a second gate that runs in CI and on every
warmed board; boards narrate a data-chosen candidate list; `ENGINE=narrated`
puts one guarded Gemini call over the deterministic body; every verdict is
archived and `ingest --task backtest` scores it; grounding redirects are
resolved into real citations; `QUALITY_JUDGE` scores each board.

- [x] **Flipped the api to `ENGINE=narrated`** on 2026-09-03 (revision
      `api-00017`, image `e4c1f2d`). Measured first against real Vertex on the
      fixture: player 10.3s, matchup 9.9s, roster 24.6s, zero rejected edits,
      so the budget is `NARRATOR_TIMEOUT_SECONDS=45`, not the 20s default.
- [x] **`QUALITY_JUDGE=true` on the ingest job** and a forced warm run the
      same evening. First gated boards: waivers, sleepers, report and the
      draft board pass the rules (judge means 4.25 / 3.5 / 4.0 / 4.0);
      trending is flagged for citing one non-crowd number across 50 rows and
      one unresolved source title. Served anyway, by design; see the
      follow-ups below.
- [x] **Backtest runs inside the stats invocation**, not on its own schedule.
      Review on PR #32 caught the race: a separate 09:30 job could overlap a
      long stats run and grade a half-written week for good. `--task` now
      takes a chain (`stats,backtest`), backtest is skipped when stats failed
      in the same run, and `week_complete()` also requires the `weekly_stats`
      freshness marker to post-date the week. Terraform's `ingest-stats`
      schedule carries the chain and two new alert policies (`board quality
      flagged`, `serving the deterministic body`).
- [x] **Live `ingest-stats` schedule switched to `stats,backtest`** on
      2026-09-03 after 8c647b8 deployed (gcloud, body and description
      matching the Terraform). The two alert policies were created through
      the Monitoring API the same evening; `infra/terraform/hardening/README.md`
      has the `terraform import` lines the next apply needs first.
- [x] **Preseason candidate lists surface junk** — fixed in the scorer. A
      sleeper candidate now needs a market rank above 36 (the crowd is not
      adding Derrick Henry because it already owns him), and in preseason
      both lists require a current depth-chart job: rank 1 anywhere, rank 2
      for RB/WR/TE only, never a clipboard QB, never a player with no chart.
      The fixture's sleepers board drops from ten picks to three, which was
      the fixture's flaw.
- [x] **Trending's synthesizer cites too little** — the prompt now makes every
      fade, and every hold on an add, name its usage number in `analysis`
      and carry it into `stats_cited`, and says a board of add counts alone
      will be flagged. A source page with no title takes its headline from
      the URL slug.
- [x] **Rebuilt the 2025 rollups with the bridged ids** on 2026-09-03
      (`ingest --task stats --season 2025 --stats-only`, ~2 minutes). Bijan
      Robinson and Ja'Marr Chase carry usage again; `/v1/health` stayed on
      2026 week 1 and only the three stat markers moved, which is the whole
      point of the flag. A forced warm followed.
- [ ] **Create the two log-based alerts** in `infra/deploy.md` §6: `board
      quality flagged` and `narrator .* falling back`.
- [ ] **Run the first backtest** the Tuesday after Week 1 stats land
      (`ingest --task backtest`, it is in `--task all` too) and check
      `/v1/stats` publishes an `accuracy` block. That number goes in the §5(b)
      submission.
- [x] **Ran `run_evals --engine adk`** on 2026-09-03 against PR #33's code:
      7/23. Every failure was accounted for and fixed in the next PR — source
      resolution moved into the ADK engine (13 cases failed the sources rule
      because the gate judges the engine's output and resolution only ran in
      precompute), `request.limit` enforced by the engine after synthesis,
      three prompt obligations the model was ignoring, and a 20s backoff
      before the one retry on a Vertex 429.
- [x] **Re-ran `run_evals --engine adk`** against 8c647b8: **18/23**, from
      7. No sources failures at all. The five left: raw tool field names
      (`snap_pct_delta`, `target_share_l4w`) leaking into prose on two cases,
      the sleepers reasoning not saying why a three-pick board is short on
      two, and one roster case whose Vertex 429 hid inside the
      ParallelAgent's `ExceptionGroup` so the backoff never saw it. All three
      fixed the same night: the synthesis contract bans field names in prose,
      the sleepers prompt dictates the exact short-board sentence, and the
      quota check unwraps exception groups. The five cases were re-run
      individually against the fix: **5/5**, so the suite stands at 23/23
      with the value gate on. Run the whole thing again before the next
      prompt change, not after.
- [x] **Per-row numbers must reach `stats_cited`** — built 2026-09-03 as
      `numbers_grounded` in the value gate: in a model-written body every
      number in every prose field must be a numeric leaf or a citation
      (years, zero and 1-12 excepted; ids and grades are values, not prose).
      Against the live boards it flagged exactly the judge's examples
      (`0.1898`, `0.2104`, a `76977`-add note never cited). The synthesis
      contract now says so. Those boards are served flagged until the next
      warm regenerates them under the new contract.
- [x] **The web UI had no renderer for the draft pair** — a draft board
      displayed as its verdict block and four citations, which is what Ryan
      saw ("just a tidbit of info"). Both renderers added 2026-09-03: tiers
      with rank, market rank, signed delta and the note; values and reaches;
      the report's picks, balance, best and worst picks and the week-one
      plan. Verified with headless Chrome against the live body.
- [x] **The draft board is warmed through the narrated engine.** The ADK
      synthesizer, asked for 200 rows, wrote 30 with value deltas that did
      not add up (the eval's `value_delta must be market position minus our
      rank` failure). `WarmTarget.engine="narrated"`: the deterministic engine
      ranks the whole pool and computes every delta; the model writes tier
      labels, notes and reasoning, each checked against the body. Ingest job
      gets `NARRATOR_TIMEOUT_SECONDS=120` for a 200-row body.
- [x] **`meta.engine`** now names the engine on every body, so the value
      gate applies `numbers_grounded` to ADK bodies only; a narrated body's
      computed notes carry derived numbers that are not citations.
- [ ] **ADK run 3, with `numbers_grounded` on: 17/23.** Every new failure is
      the rule catching the synthesizer quoting numbers it did not cite
      (a waivers board with zero non-crowd citations and 24 uncited numbers;
      a trajectory delta; two report notes; an analytics number). Two prompt
      gaps fixed the same night (team-report analytics must cite with the
      exact context source string; trajectory deltas must be cited); the
      draft-board delta failure is gone by construction. Re-run after the
      next deploy. The honest reading: the ADK synthesizer's citation
      discipline is imperfect and the gate now measures it; boards that
      keep failing are candidates for the narrated pattern.
- [x] **Terraform root applied 2026-09-03** with terraform installed from
      the HashiCorp tap; the two hand-made alert policies were imported and
      normalised to the declared filters and text; the plan is clean.

### Not on this list, deliberately

**The MainNet cutover** (§1.3–1.4) is the gate, not a development task: fund and
opt in the wallets, flip the env, take one settle, confirm the leaderboard. It
is ops work and it is already written down.

**The example agent's dropped `PAYMENT-SIGNATURE`** stays in §6. It is a real
bug, but it is a teaching artifact — the MCP server, which is the surface real
users install, already journals and recovers.

---

## 1. Blocking MainNet launch

### 1.1 Public host — **DECIDED 2026-08-30**
The GoPlausible facilitator **permanently** catalogues the `resourceUrl` from
the first settled challenge-tagged payment (`DESIGN_NOTES.md`, Bazaar listing).
Whichever host takes the first MainNet settle is the Bazaar listing forever.

`playclock.xyz` was registered and verified with Google on 2026-08-30. The
permanent split is `playclock.xyz` / `www.playclock.xyz` for the web UI and
`api.playclock.xyz` for the paid API. The first MainNet settle must advertise
that API origin; the TestNet deployment proves it before MainNet is enabled.

### 1.2 Resolve the ADK latency problem on the personalized endpoints
`matchup`, `roster`, `team_report` and `draft_report` cannot be precomputed and
run ~55s against a <20s p95 promise. This is not merely slow — settlement happens before the
handler returns, so a client timeout charges the caller and returns nothing.
Observed live: one call charged **0.15 USDC and returned nothing**.

Pick one before MainNet:
- speed the pipeline up to something defensible at these prices, or
- price/label them honestly as long-running, or
- launch them on `ENGINE=deterministic` and turn ADK on when it is faster.

The five league-wide boards are precomputed. This is only about the four
personalized endpoints.

**Launch decision (2026-08-30): hybrid.** The public API serves fresh,
personalized requests with `ENGINE=deterministic`; the separate ingest job stays
on ADK/Vertex and precomputes the five default premium boards. This preserves
grounded synthesis where it can be generated before payment while keeping the
uncacheable paid path fast. Revisit when ADK's personalized p95 is defensibly
below the client deadline.

### 1.3 Fund and opt in the two MainNet wallets
Addresses in `infra/deploy.md` §1b; mnemonics in Secret Manager only.
Each address needs ~0.2 ALGO; the payer additionally needs real USDC (ASA
`31566704`). Both ends must be opted into the USDC ASA or settlement dies at
simulate and surfaces as a second 402, unbilled.

```bash
uv run python infra/optin.py optin  --network mainnet --all
uv run python infra/optin.py status --network mainnet
```

### 1.4 Run the MainNet cutover
`infra/deploy.md` §7, in order. Summary: opt in → `X402_NETWORK=mainnet` with a
MainNet `X402_PAY_TO` → confirm the GoPlausible facilitator URL (never the SDK's
`x402.org` default) → confirm `extra.tag` → `--min-instances=1` → one real settle
→ re-run evals against the deployed model → confirm the endpoint surfaces on the
leaderboard.

### 1.5 Validate ADK on TestNet before flipping both switches
`ENGINE=adk` now runs on the live TestNet deployment and has been exercised, but
do not change engine *and* network in the same step. One clean TestNet run of
the full paid path on `adk` immediately before the MainNet flip.

Completed 2026-08-30 against `https://api.playclock.xyz`: a paid `/v1/roster`
returned a grounded `gemini-3.7-flash` answer and a successful TestNet settlement
receipt in 53.637s. Repeat immediately before MainNet only if the deployment
changes materially.

---

## 2. Season rollover — verify before selling anything

Completed 2026-08-30. The schedule-only ingest wrote the published 2026 schedule
without relabelling unavailable 2025 weekly stats; `/v1/health` reports
**`season: 2026, week: 1`**, and all five cacheable boards were regenerated.
The stale-season guard remains the stop-the-line protection for future rollovers.

---

## 3. Volume — a quarter of the judging, and the field is not thin

Assessment criteria (§8) are **evenly weighted**: Volume, Use case quality,
Sustained potential, Innovation. Awards are $25k/$22.5k/$20k/$17.5k/$15k plus
**500,000 ALGO split across the top 20** on the leaderboard.

### Reading the leaderboard correctly

`GET https://facilitator.goplausible.xyz/data/leaderboards` **defaults to
`limit=12` and `range=24h`**. Both defaults mislead: a bare call looks like a
12-entrant field. Always pass both:

```bash
curl -s "https://facilitator.goplausible.xyz/data/leaderboards?src=x402-global-challenge&limit=500&range=all" | jq '.total'
```

Sampled 2026-08-29:

| window | merchants | volume (USDC) | settles |
|---|---|---|---|
| 24h | 24 | 431 | 21.8k |
| 7d | 51 | 1,868 | 108k |
| 30d | 98 | 22,053 | 401k |
| **all** | **107** | **22,209** | **407k** |

30d ≈ all-time, so essentially the entire field arrived in the last month and is
still accelerating into the Sept 1 registration close.

### The cut lines that matter

| bar | rank | volume needed |
|---|---|---|
| ALGO pool | top 20 | **~23.25 USDC** (rank 20 today) |
| finalist eligibility | top 50 | **~0.30 USDC** (rank 50 today, 3 settles) |

Top 50 is nearly free — a handful of real settles clears it. **Top 20 is the
real target**: ~23 USDC is roughly 230 calls at $0.10 or ~95 at $0.25. That is
a genuine-demand number, not a burst.

### What the top of the board looks like

| rank | host | volume | settles | $/call | named? |
|---|---|---|---|---|---|
| 1 | x402-quant-signals.onrender.com | 14,388 | 143,954 | 0.1000 | no — wallet address |
| 2 | x402-trading-news.onrender.com | 3,235 | 32,351 | 0.1000 | no — wallet address |
| 3 | api.syraa.fun (Syra) | 2,217 | 13,488 | 0.1644 | yes |
| 7 | tendrilhq.com (TENDRIL) | 230 | 462 | 0.4972 | yes |
| 10 | onestepchess.xyz | 138 | 154,827 | 0.0009 | yes |
| 22 | iomarkets.app | 15.31 | **1** | 15.3100 | yes |

Ranks 1 and 2 are unnamed, carry no Bazaar listing, and settle six-figure call
counts at a constant 0.10. Whether that survives §14 review ("artificial volume,
wash transactions, repeated self-payments") is the Administrator's call, not
ours — but do not benchmark against it, and do not copy it.

**Do not manufacture volume.** §14 lets the Administrator exclude manipulated
activity at its sole discretion, and GoPlausible already classifies localhost
and self-payment loops as DEV. One validation settle is required and expected;
a loop risks the whole entry to move one of four criteria.

### Price positioning

Field prices cluster at **0.001–0.10 USDC**. Play Clock at **0.10–0.75** sits at
the top of the range — only TENDRIL (0.50/call), AgentMesh (0.76), x402.ondapc
(1.91) and IoMarkets (15.31) are higher, and all of them are low-count. At $0.10
a call, rank 20 is 230 sales; at $0.75 it is 31. Higher prices are not obviously
wrong here — they just need the demand work in §3 below to be about *agents that
actually want fantasy analysis*, not volume for its own sake.

Legitimate demand generation, in rough order of effort:
- Publish the example agent + `llms.txt` where agent builders look.
- The free `/v1/trending/preview` teaser is the funnel — make sure it is good.
- `DESIGN_NOTES.md` "Ideas": free `/v1/stats` endpoint built from `receipts/`
  as social proof for the §5(b) submission and the finals pitch.
- Discord/community post in the Algorand challenge channels.
- **Time it to the NFL season.** Week 1 is ~Sept 10 and the measurement window is
  in October — peak fantasy attention. This is the one advantage in the field
  that no other entrant has.

---

## 3b. Serve a real page at the API origin (cheap, do before the first settle)

The leaderboard renders a `label` and `logo` per merchant, and both are derived
from **what the resource origin serves at `/`** — not from the 402 payload:

- Hosts serving HTML get their `<title>`/`og:title` as the leaderboard name
  (IoMarkets, AgentHub, TENDRIL, Scrape402, AgentMesh all match exactly).
- Hosts that 404 or return bare JSON at `/` get a **truncated wallet address**
  instead (`SGLTUP…SPPI`, `QSNLPP…U6EY`, `LN745U…N3YY`) — including ranks 1 and 2.
- `logo` is only populated for hosts serving **`/apple-touch-icon.png`** at that
  exact conventional path (Syra, One Step Chess, TENDRIL → 200; everyone else
  404s → `logo: null`).

**Play Clock today returns JSON at `/` and 404s on `/apple-touch-icon.png`** — so
as deployed it would list as a truncated wallet address with no logo, next to
competitors showing a name and an icon.

Fix before the first MainNet settle (the listing is one-shot):

- [ ] Serve an HTML landing page at the API origin `/`, with a `<title>` and
      `og:title` of "Play Clock" and a real `og:description`. `web/index.html`
      already has good copy — the constraint is that it must be on the **same
      origin as the paid endpoints**, since that origin is what gets catalogued.
- [ ] Serve `/apple-touch-icon.png` (180×180 PNG) at that origin.
- [ ] Keep the JSON service pointers (`/v1/catalog`, `/llms.txt`, `/openapi.json`)
      — move them to content negotiation or keep them linked from the page.

## 3d. Rewrite the resource descriptions as tool descriptions

Cheap, and it decides whether an agent browsing the Bazaar picks us. Every
serious entrant writes `description` for an LLM's tool-selection step, not for a
human reading marketing copy:

> "Solana DeFi total value locked overview from DefiLlama. **Use when** an agent
> assesses macro DeFi health and capital allocation on the Solana ecosystem."

Ours currently read as product copy ("Top 25 Sleeper trending adds and drops
with per-player stat context…"). Add the *when to call this* clause to all eight
`description` fields in `api/x402/endpoints.py`. These strings are what the
Bazaar catalogues and what an agent sees before deciding to pay.

## 3c. MCP server — BUILT 2026-08-29

`playclock_mcp/` ships. It is a client: it runs on the user's machine, holds
their Algorand key, and pays per tool call.

- **Tools are derived from `/v1/catalog` + `/openapi.json` at startup**, so a new
  paid endpoint becomes a tool automatically — proven by the draft pair, which
  appeared with no MCP change.
- **Every payment is journalled before it is sent**, so a timeout is recoverable
  via `playclock_recover_payments` instead of being money gone. This is the bug
  the bundled example agent still has.
- Hard per-call price ceiling (`PLAYCLOCK_MAX_PRICE_USDC`), enforced before
  signing and again in the SDK.
- 32 tests, including one that drops a paid response on the floor and proves
  recovery returns the answer with no second settle.

Setup and config: `playclock_mcp/README.md`.

**Still to do for it to earn volume:**
- [ ] Publish install instructions somewhere agent builders will find them.
- [x] `DEFAULT_BASE_URL` points at `https://api.playclock.xyz`.
- [ ] Converge the payment path with `examples/agent/fantasy_agent.py`. They are
      deliberately separate today (teaching artifact vs. product surface), but
      two implementations of a payment protocol is a real long-term smell.

## 3e. Draft endpoints — BUILT 2026-08-29

Draft season is now, so this shipped ahead of the MainNet work.

- `GET /v1/draft-board` — 0.25 USDC, tiered board of 200 players ranked against
  the market, values and reaches called out. Cached 12h under **week 0** (it is
  season-scoped) and warmed by `ingest/precompute.py`.
- `POST /v1/draft-report` — 0.75 USDC, grades a completed Sleeper draft pick by
  pick, positional balance, best/worst picks, and a plan before Week 1. Takes a
  `draft_id` or a `sleeper_username`.

Both are live in MCP as `playclock_draft_board` / `playclock_draft_report`.

**Known limits, deliberately shipped:**
- The market signal is Sleeper's `search_rank` — draft *popularity*, not a
  consensus ADP. Never described as ADP anywhere in a response; prompts, evals
  and route tests all enforce that. Buying a real ADP feed is the upgrade.
- The board ranks on **prior-season** usage, so rookies and players returning
  from a missed year hold their market rank and say so. The ADK research agent
  is what covers offseason role changes; the deterministic engine cannot.
- `draft_report` runs the personalized (uncached) path, so it inherits the §1.2
  latency problem when `ENGINE=adk`.

**Timing note worth being clear-eyed about:** drafts finish before mid-September
and the leaderboard is measured over an unannounced window in **October**. So
these two endpoints are a real-usage and §5(b)-evidence play — proof of
non-self payers — more than a leaderboard play. That is a good reason to ship
them, not a reason to expect October volume from them.

## 4. The §5(b) submission (closes Sept 29)

The form arrived by email on Sept 15 ("Submit your project here"). It needs a
public GitHub URL and a 3–5 minute demo video; the same email asks for a
separate submission of the repo to Electric Capital.

- [x] Repo scanned for secrets across full history (2026-09-21, again 2026-09-27).
- [ ] Repo public; README's demo-video row filled in.
- [ ] Demo video: 402 by curl → Bazaar listing → pay in the web UI with Pera →
      receipt txid on the explorer → an agent paying on its own → `/v1/stats`.
      One or two payments, not a loop (Official Rules §14).
- [ ] Form submitted. Entry type **Composite** (ten endpoints, one `payTo`).
      Description: §0 above.
- [ ] Electric Capital repo submission.

Payment evidence, stated as the indexer shows it: every settle before
2026-09-27 came from project-owned wallets; the first outside payer was an
automated agent paying each Bazaar listing in turn. Say so plainly rather than
quoting `/v1/stats`' unique-payer count, which counts the project's own wallets.

---

## 5. Done — do not redo

- **TestNet validated end to end (2026-08-28).** Two real 0.10 USDC settles on
  `https://api-998796693706.us-east4.run.app`. All seven `DESIGN_NOTES` TestNet
  items verified: fee-payer, opt-in, amounts, receipt semantics, a 3496-char
  header through Cloud Run's proxy, idempotent replay, and the SDK msgpack
  workaround.
- **A TestNet settle does not consume the one-shot Bazaar listing.** After a
  challenge-tagged TestNet settle, `/data/leaderboards` was unchanged. The
  MainNet host is still a free choice.
- **ADK on Vertex works** — real Gemini synthesis with grounded stats and
  sources on a paid call. `GOOGLE_CLOUD_LOCATION=global` is required for
  `gemini-3.7-flash`.
- **Latency work (2026-08-28):** parallel gather 72.5s → 55.6s; four boards
  precomputed and warmed; idempotency TTL raised past handler latency; the
  three disagreeing timeouts reconciled.

---

## 6. Deferred, non-blocking

- `DESIGN_NOTES.md` "Timeline flag" still hedges the Sept 1 deadline as
  unverified. It is now confirmed against the Official Rules PDF §5(a), and the
  §5(a)/§5(b) ambiguity is resolved — promote to fact and drop the note.
- CI (`.github/workflows/ci.yml`): `actions/checkout@v4` and
  `astral-sh/setup-uv@v5` target deprecated Node 20 and are force-run on Node 24.
- Wire `gcloud run deploy` into CI (post-launch; `infra/deploy.md` preamble).
- The bundled example agent discards `PAYMENT-SIGNATURE` on failure, so a
  paid-but-timed-out call is unrecoverable from the client side. Persist it.
- `DESIGN_NOTES.md` open questions 3–5 (product name, refund posture wording,
  Discord bot) — none block launch.
