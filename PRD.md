# PRD — Play Clock
**Pay-per-analysis fantasy football intelligence, built for the Algorand Global x402 Challenge**

Version 1.0 · August 2026 · Target ship: MainNet live by Sept 8 (NFL Week 1 kickoff Sept 10)

---

## 1. One-liner

An x402-gated API (plus thin web UI) that sells NFL fantasy football analysis one request at a time: trending players with context, weekly sleeper picks, start/sit matchup calls, and full roster audits — priced in USDC micropayments on Algorand, purchasable by humans *and* AI agents.

## 2. Why this wins the challenge

The challenge rewards **real on-chain usage** during an unannounced October measurement window, plus use-case quality, technical execution, and long-term potential. Fantasy football is uniquely aligned:

- **Demand spikes weekly, all fall.** Waiver wire (Tue–Wed) and start/sit panic (Sat–Sun) create recurring purchase moments every single week of the measurement window. October = NFL Weeks 4–9, peak in-season anxiety.
- **Micropayments fit the product.** Nobody wants a $9.99/mo subscription for one start/sit question. $0.10–$0.50 per answer is the natural price point — exactly what x402 exists for.
- **Agent-native distribution.** Bazaar discovery means AI agents (fantasy bots, Discord bots, agentic assistants) can find and pay the endpoints autonomously. We publish an OpenAPI spec + llms.txt so agents can self-serve. This is the "long-term potential" story for the Devcon pitch.
- **Entry type: Composite** — multiple paid endpoints under one project, one payTo address.

## 3. Users

| Persona | Job to be done | Entry point |
|---|---|---|
| Casual fantasy manager | "Who do I start this week?" without paying for a season subscription | Web UI + Pera/Defly wallet |
| Sleeper power user | Full roster audit + waiver targets tailored to *their* team | Enters Sleeper username; we pull rosters via free Sleeper API |
| AI agents / bot builders | Programmatic fantasy intelligence as a paid tool call | Bazaar discovery → x402 payment → JSON response |
| Content creators / group chats | Shareable weekly sleepers & trending analysis | Free teaser endpoints → paid full versions |

## 4. Product scope

### 4.1 Free teaser endpoints (funnel + Bazaar visibility)
- `GET /v1/trending/preview` — top 5 trending adds/drops (names + trend counts only, no analysis)
- `GET /v1/health`, `GET /v1/catalog` — machine-readable catalog of paid endpoints, prices, schemas

### 4.2 Paid endpoints (x402-gated, USDC on Algorand)

| Endpoint | What the payment unlocks | Price (USDC) |
|---|---|---|
| `GET /v1/trending` | Full trending adds/drops (top 25) with per-player stat context, why-it's-happening analysis, add/fade verdicts | 0.10 |
| `GET /v1/sleepers?week=N` | 8–12 weekly sleeper picks with usage trends (snap %, target share, red-zone touches), matchup reasoning, confidence tiers | 0.25 |
| `POST /v1/player` | Deep dive on one player: L4-week stat trends, usage trajectory, upcoming schedule difficulty, fresh news via internet research, verdict | 0.15 |
| `POST /v1/matchup` | Start/sit between 2–4 players: head-to-head stat comparison, opponent defense vs. position, weather/injury news, ranked recommendation | 0.25 |
| `POST /v1/roster` | Full roster audit (Sleeper username or manual roster): positional grades, start/sit for the week, drop candidates, top waiver adds available in their league | 0.50 |
| `GET /v1/waivers?week=N` | Waiver wire big board: ranked FAB/priority targets with % rostered proxy (Sleeper trending), stash vs. start labels | 0.25 |
| `GET /v1/report?week=N` | Full league-wide weekly briefing: emerging players (usage-delta × trending detection, i.e. "coming up" before consensus), injury fallout chains + handcuffs, stock up/down, rookie watch, streamers. Generated once per cycle, cached — flagship margin product | 0.50 |
| `POST /v1/team-report` | Team-aware deep report (Sleeper username + league): positional strength graded vs. actual leaguemates, deficiency callouts with fixes from players available in *their* league, and a manager performance review — optimal vs. started lineup (bench points lost), positional mis-start patterns, luck/points-against analysis, efficiency rank vs. leaguemates | 0.75 |

Pricing rationale: low enough for impulse repeat purchases (drives leaderboard volume), high enough to signal value. Revisit after Week 1–2 data; consider 0.05 "agent tier" responses (compact JSON, no prose) to maximize agent call volume.

### 4.3 Web UI (thin, mobile-first)
- Landing page: this week's free trending preview + one sample paid analysis (cached, watermarked "sample")
- Wallet connect (Pera / Defly via WalletConnect), pay-and-reveal flow per analysis
- Sleeper username input → roster displayed → "Audit my roster ($0.50)" CTA
- Every paid result gets a shareable (non-paywalled, watermarked) card image → social distribution loop

### 4.4 Explicitly out of scope for v1
- Yahoo/ESPN league OAuth connect (Sleeper only; manual roster paste covers everyone else)
- Vegas odds & player props (free tiers no longer cover NFL props; add as v1.1 with The Odds API paid tier if revenue supports it)
- DFS lineup optimization, dynasty/keeper valuations, season-long projections model
- Accounts, subscriptions, or any stored user PII — the wallet is the identity

## 5. What "analysis" means (the paid product)

Every paid response is generated fresh (or from a ≤6h cache for `/v1/trending` and a ≤12h cache for the other week-scoped boards) by an ADK agent pipeline that combines:
1. **Hard stats** — nflverse weekly player stats, snap counts, target share, red-zone usage, opponent defensive splits (positional points allowed)
2. **Market signal** — Sleeper trending add/drop counts (what 13M+ managers are doing right now)
3. **Fresh intel** — Google Search-grounded research agent: injuries, beat-writer reports, depth chart changes, weather
4. **Synthesis** — a verdict with confidence level, written reasoning, and structured JSON (so agents and UIs both consume it)

Response contract: every paid response includes `verdict`, `confidence`, `reasoning`, `stats_cited[]`, `sources[]`, `generated_at`, `data_freshness`. Trust is the product.

Trust is also only the floor. A manager pays for a call they would not have made, with a number they can repeat in their league chat — so every paid answer leads with the one claim that disagrees with the crowd or the market and cites what backs it. A board that re-narrates Sleeper's most-added list is the free preview with prose, and the value gate (`api/evals/quality.py`) fails it. Every verdict is archived and scored against the week's real points once they exist; the hit rate is published on `/v1/stats`.

## 6. Success metrics

| Metric | Target (by mid-October) |
|---|---|
| Paid on-chain calls / week | 500+ (leaderboard-driving metric) |
| Unique payer addresses / week | 75+ (rules reward *real* usage; concentrated self-dealing looks bad) |
| Repeat payer rate | 30%+ week-over-week |
| Agent-originated calls | 15%+ of volume (differentiator for finals pitch) |
| p95 latency, paid endpoints | < 20s (agentic synthesis) / < 3s (cache hits) |
| Backtested hit rate, published on `/v1/stats` | above the naive baseline (start the higher scorer, add the most-added) every week it is measured |

## 7. Launch & usage-driving plan (usage IS the judging criteria)

- **Sept 1–8:** TestNet validation → MainNet deploy, Bazaar listing + challenge tag, first real USDC payment confirmed, submit entry
- **Week 1 (Sept 10):** Launch thread + free trending preview shared to r/fantasyfootball adjacent communities, Sleeper subreddit, Algorand Discord
- **Weekly cadence:** Tuesday "Waiver Wire Board" and Saturday "Start/Sit window" posts with free teasers linking to paid endpoints; shareable result cards
- **Agent distribution:** publish OpenAPI + llms.txt + an example "fantasy agent" script (pays via x402, gets analysis) in a public repo; list in x402/agentic-commerce directories
- **October:** volume push — price experiments, referral watermarks on shared cards, Discord bot that quotes the paid endpoints

## 8. Risks & mitigations

| Risk | Mitigation |
|---|---|
| ESPN unofficial endpoints break or get blocked | They're supplemental only; core = Sleeper + nflverse. Feature-flag the ESPN source. |
| Sleeper API terms / rate limits (stay well under ~1000 req/min; players dump fetched 1×/day as they request) | Aggressive caching in Firestore; nightly players sync; per-IP backoff |
| LLM analysis quality embarrassment (wrong player, stale injury) | Ground every claim in retrieved stats; include data timestamps; eval suite of 20 golden queries run before each weekly deploy |
| Cost per paid call exceeds price | Gemini Flash for synthesis, cache week-scoped endpoints, compact agent tier; track unit economics from day 1 |
| Low organic volume in measurement window | Free-teaser funnel + shareable cards + agent SDK; worst case, the Discord bot drives legitimate third-party usage |
| Fantasy data licensing optics | nflverse is CC-BY 4.0 (attribute it), Sleeper is public read-only, no scraping of paywalled rankings (FantasyPros et al. excluded) |

## 9. Open questions (answer before build freeze)

1. Final product/domain name (Play Clock is a placeholder)
2. Exact prices — validate against x402 Bazaar norms once live
3. Whether the Discord bot ships in v1 or v1.1 (recommend v1.1, ~Week 3)
