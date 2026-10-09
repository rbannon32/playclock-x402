# Deploying Play Clock

Everything below is `gcloud`, run from the repo root. Two Cloud Run **services**
(`api` and `web`) plus one Cloud Run **Job** (`ingest`) driven by five
Cloud Scheduler crons — tech spec §1 and §7.

Nothing here is automated in CI on purpose: the CI workflow tests and evaluates,
a human deploys. Wiring `gcloud run deploy` into GitHub Actions is a post-launch
task (see the TODO in `.github/workflows/ci.yml`).

## 0. Variables

```bash
export PROJECT_ID=playclock          # a new, dedicated project (tech spec §7)
export REGION=us-east4                     # nearest Vertex-supported region
export REPO=playclock                        # Artifact Registry repository
export API_URL=https://api.playclock.xyz   # the PUBLIC url agents will call
```

`API_URL` matters more than it looks: the facilitator **permanently catalogs**
the `resource.url` it sees on the first settled payment. Get it wrong once on
MainNet and the Bazaar listing points at the wrong host forever.

## 1. Project bootstrap (once)

```bash
gcloud projects create "$PROJECT_ID"
gcloud config set project "$PROJECT_ID"
gcloud billing projects link "$PROJECT_ID" --billing-account=<BILLING_ACCOUNT_ID>

gcloud services enable \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  firestore.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  monitoring.googleapis.com

gcloud artifacts repositories create "$REPO" \
  --repository-format=docker --location="$REGION" \
  --description="Play Clock images"
```

### Firestore (Native mode)

```bash
gcloud firestore databases create --location="$REGION" --type=firestore-native
```

No schema and no indexes to create up front: every query the app makes is a
single-field equality/range or a collection scan, which Firestore serves from
the automatic single-field indexes. Two composite indexes become worthwhile only
if the receipts dashboard starts filtering by endpoint *and* ordering by time:

```bash
# Only if /v1/stats or a dashboard needs it later — not required to launch.
gcloud firestore indexes composite create \
  --collection-group=receipts --field-config=field-path=endpoint,order=ascending \
  --field-config=field-path=ts,order=descending
```

Collections written at runtime: `response_cache/`, `receipts/`,
`failed_paid_calls/`, `payment_idempotency/`. Everything else (`players/`,
`player_index/`, `id_map/`, `weekly_stats/`, `usage_trends/`, `def_vs_pos/`,
`schedules/`, `trending/`, `meta/`) is written only by the ingest job.

`payment_idempotency/` gets one document per paid request and the app never
deletes one: a record is dead once its `expires_at` passes (300s after settle,
360s for an unfinished claim) but stays on disk. Each record also carries
`expires_at_ts`, the same instant as a Firestore Timestamp, which is the only
field type a TTL policy acts on. Turn the policy on once:

```bash
gcloud firestore fields ttls update expires_at_ts \
  --collection-group=payment_idempotency --enable-ttl --project="$PROJECT_ID"

# Confirm: state goes CREATING -> ACTIVE (can take a while on a large collection).
gcloud firestore fields ttls list --project="$PROJECT_ID"
```

TTL deletion runs within about a day of expiry, not on the second; that is
fine, because liveness is decided by `expires_at`, never by the document
existing. Records written before `expires_at_ts` existed have no Timestamp and
are never swept; they are small and harmless, and can be deleted by hand.

### Service accounts

```bash
gcloud iam service-accounts create playclock-api    --display-name="Play Clock API"
gcloud iam service-accounts create playclock-ingest --display-name="Play Clock ingest"

export API_SA=playclock-api@$PROJECT_ID.iam.gserviceaccount.com
export INGEST_SA=playclock-ingest@$PROJECT_ID.iam.gserviceaccount.com

# API: Firestore read/write + Vertex AI for the ADK pipeline.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$API_SA" --role=roles/datastore.user
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$API_SA" --role=roles/aiplatform.user

# Ingest: Firestore only. It never calls an LLM.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$INGEST_SA" --role=roles/datastore.user
```

## 1b. Wallets

Four Algorand accounts, generated 2026-08-27. The **mnemonics live only in Secret
Manager** in project `playclock` — they were piped to `gcloud` over stdin and never
written to disk or shell history. Addresses are public and safe to commit.

| Secret name | Address | Role |
|---|---|---|
| `playclock-testnet-merchant` | `BJXTJUHHMDDH36GEDNZA4MPXDQ6UMMOKACFN3DMF3TD3HZZKXJEGKXPUYI` | TestNet `X402_PAY_TO` |
| `playclock-testnet-payer` | `KAELWUHBMCXQZMEVIWHOGRDHIUJ27SBW4OCBIUHIAEC4CU5KTPSPJVGKTQ` | TestNet payer (example agent) |
| `playclock-mainnet-merchant` | `MDBJMM6RJ4TM7W5FITZ3MWJTGTHMC4SKQJWRWI2JCQUKIR5LA7BPIUTMMM` | MainNet `X402_PAY_TO` — the challenge entry |
| `playclock-mainnet-payer` | `RCAUDGDLWUQAG64IPLRZPYMXPVQXTR272SZS3GX66DDTLRABRTQ3FRMXT4` | MainNet payer for the example agent — one settle per endpoint on 2026-09-21/22, to catalogue them (DESIGN_NOTES §25) |

**Most MainNet settles came from a different wallet.** The first 15 payments
into the merchant were sent through the web UI by the wallet that funded both
project accounts on 2026-08-31. `playclock-mainnet-payer` made ten settles on
2026-09-21 and 22, 2.70 USDC in total, one per endpoint, through the example
agent's signer — the settles that put all ten endpoints in the Bazaar
(DESIGN_NOTES §25). Its mnemonic is the one the example agent expects
(`ALGORAND_MNEMONIC`); pipe it from Secret Manager into the process env, never
onto disk. The first payer outside the project arrived on 2026-09-27: an
automated agent paying every Bazaar listing in turn. Verify either claim against the indexer rather than this file:

```bash
curl -s "https://mainnet-idx.algonode.cloud/v2/accounts/<address>/transactions?asset-id=31566704&limit=200"
```

Read one back with:

```bash
gcloud secrets versions access latest --secret=playclock-testnet-payer --project=playclock
```

**The API service needs none of these secrets.** It never signs anything — it is
the merchant, and the facilitator does the settling. Cloud Run gets the merchant
*address* as the plain `X402_PAY_TO` env var. Only the payer agent
(`examples/agent/fantasy_agent.py`, via `ALGORAND_MNEMONIC`) needs a mnemonic, and
that runs locally, not in Cloud Run.

### Funding and opt-in

Both ends of a payment must be opted into the USDC ASA or settlement dies at
simulate and surfaces as a second 402, unbilled (`DESIGN_NOTES.md:151`).

- **TestNet** — fund both TestNet addresses with ALGO from the dispenser at
  https://bank.testnet.algorand.network/, then get TestNet USDC (ASA `10458941`)
  into the payer.
- **MainNet** — each address needs ~0.2 ALGO (0.1 min balance + 0.1 for the
  opt-in and fees). The payer additionally needs real USDC (ASA `31566704`).
  A couple of dollars covers the validation settle.

Opt-in is a 0-amount asset transfer from an address to itself. Both merchant
wallets and both payer wallets need it, on their respective networks.

## 1c. Current deployment

Standing environment as of 2026-10-09. MainNet since 2026-09-01
(DESIGN_NOTES §20); the Bazaar entry is `https://api.playclock.xyz`.

| | |
|---|---|
| Project | `playclock` (number `998796693706`), billing linked |
| Region | `us-east4` |
| API service | https://api.playclock.xyz (Cloud Run URL https://api-998796693706.us-east4.run.app), revision `api-00031` |
| Ingest job | `ingest`, one job, task per invocation |
| Firestore | Native mode, `us-east4` |
| Image tag | api `62a94bc`; ingest `62a94bc`; web `62a94bc` |
| Web service | https://playclock.xyz, revision `web-00022` — see §3b |

The api runs `ENGINE=narrated` with `NARRATOR_TIMEOUT_SECONDS=45` and
`RESEARCH_ENDPOINTS=none`, `--min-instances=1`. The budget was chosen from a
measurement against real Vertex before the flip: player 10s, matchup 10s,
roster 25s, no rejected edits; every client waits longer (web 180s, MCP 300s,
example agent 180s, 402 window 120s). The ingest job runs `ENGINE=adk` with
`QUALITY_JUDGE=true`. Rolling either forward is `--update-env-vars`, never
`--set-env-vars`: the latter drops the wallet and facilitator settings.

The ingest job additionally carries `MODEL_RETRY_ATTEMPTS=6` (§4) since
`7ef0b85`.

Since `api-00025` (2026-09-21) the api also runs `FREE_RATE_LIMIT_PER_MINUTE=0`
— the free-route limiter is **off** in production, because the image refuses
to boot a positive limit under `ENV=prod` without `TRUSTED_PROXY_HOPS` (§3) —
and `X402_MERCHANT_LOGO=https://api.playclock.xyz/apple-touch-icon.png`, the
one host that actually serves the icon (the web service ships none).

Since `1a14874` every image is built from this repository,
`github.com/rbannon32/playclock-x402`; tags are its commit SHAs.

The `payment_idempotency` TTL policy on `expires_at_ts` (§1) was enabled on
2026-09-27, with the `68e9a14` roll-out.

Since 2026-10-09 (`f016bd2`, then `62a94bc`) a code-only roll-out changes the
image and nothing else: `gcloud run services update api|web --image=…` and
`gcloud run jobs update ingest --image=…`. That keeps every env var, the
scaling flags and the service account; the §3 and §4 commands are for a fresh
environment, and their `--set-env-vars` would drop the wallet settings. The
same day the board-quality alert moved to serious flags only, notified at most
daily (`infra/terraform/hardening`, applied; DESIGN_NOTES).

## 2. Build the images

Both images come out of one build, via `infra/cloudbuild.yaml`:

```bash
export TAG=$(git rev-parse --short HEAD)
export API_IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/api
export INGEST_IMAGE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/ingest

gcloud builds submit --config=infra/cloudbuild.yaml \
  --substitutions=_IMAGE_BASE=$REGION-docker.pkg.dev/$PROJECT_ID/$REPO,_TAG=$TAG \
  --region="$REGION" .
```

**Do not fall back to `gcloud builds submit --tag`.** That shorthand runs Cloud
Build's legacy non-BuildKit builder, and both Dockerfiles use
`RUN --mount=type=cache` for the uv wheel cache — the build dies at the
dependency layer with *"the --mount option requires BuildKit"*. The config file
sets `DOCKER_BUILDKIT=1` on an explicit docker step, which is what fixes it.

(Or build locally and push: `docker build -t "$API_IMAGE:dev" . && docker push "$API_IMAGE:dev"`.)

## 3. Deploy the API service

TestNet first. Everything in `infra/env.example` is settable here; only the
values that differ from the defaults are listed.

```bash
gcloud run deploy api \
  --image="$API_IMAGE:$(git rev-parse --short HEAD)" \
  --region="$REGION" \
  --service-account="$API_SA" \
  --allow-unauthenticated \
  --cpu=2 --memory=2Gi \
  --concurrency=8 \
  --min-instances=1 \
  --max-instances=10 \
  --timeout=300s \
  --set-env-vars="\
ENV=prod,\
FREE_RATE_LIMIT_PER_MINUTE=0,\
STORE_BACKEND=firestore,\
GOOGLE_CLOUD_PROJECT=$PROJECT_ID,\
GOOGLE_CLOUD_LOCATION=global,\
GOOGLE_GENAI_USE_ENTERPRISE=true,\
ENGINE=deterministic,\
MODEL_ID=gemini-3.7-flash,\
ENGINE_FALLBACK=true,\
RESEARCH_ENDPOINTS=none,\
X402_MODE=live,\
X402_NETWORK=testnet,\
X402_PAY_TO=<ALGORAND_ADDRESS>,\
X402_FACILITATOR_URL=https://facilitator.goplausible.xyz,\
X402_RESOURCE_BASE_URL=$API_URL,\
X402_CHALLENGE_TAG=x402-global-challenge"
```

`FREE_RATE_LIMIT_PER_MINUTE=0` is deliberate and is the only safe value for
this command. The limiter refuses to start a production revision that has a
positive limit and no `TRUSTED_PROXY_HOPS`, because without a verified proxy
topology it cannot tell a caller's `X-Forwarded-For` prefix from one its own
infrastructure appended — it would either bucket every caller together or hand
out a fresh budget per spoofed prefix. To enable the limit, put the service
behind a proxy that sanitises the header, restrict Cloud Run ingress to it,
verify how many entries it appends, then set `TRUSTED_PROXY_HOPS` to that
count (one proxy that overwrites the header appends one entry, so `1`).

Why these numbers (tech spec §1):

- The public API uses the deterministic engine for personalized requests. A
  paid cache-miss `/v1/roster` still took 53.637s on ADK with research disabled,
  so this protects a caller from settling and then timing out. The separate
  ingest job remains on ADK and precomputes the five default premium boards;
  those cached responses retain the grounded Vertex synthesis.

- `--min-instances=1` **during the season**. Agentic synthesis already costs
  ~10-20s; a cold start on top of that loses the sale. Drop it to 0 in the
  off-season — it is the single biggest line on the bill.
- `--concurrency=8`: each in-flight paid call holds an ADK pipeline; more
  concurrency per instance just queues on CPU.
- `--timeout=300s`, **not** the 60s this originally said. Measured ADK latency on
  a real paid `/v1/player` call is ~72s, well past the <20s p95 target and past
  60s. A Cloud Run timeout below the real latency does not just fail the call —
  the payment settles first, so the caller is charged for an answer the proxy
  then cuts off. Keep this above `accepts[].maxTimeoutSeconds` (120s), and treat
  the p95 gap as a performance bug to fix, not a number to keep raising.

Then map the domain so `X402_RESOURCE_BASE_URL` is a URL agents can actually reach:

```bash
gcloud beta run domain-mappings create --service=api --domain=api.playclock.xyz --region="$REGION"
```

Smoke test before anyone pays:

```bash
curl -s "$API_URL/v1/health" | jq
curl -s "$API_URL/v1/catalog" | jq '.network, .pay_to, .facilitator_url'
curl -s -o /dev/null -w '%{http_code}\n' "$API_URL/v1/trending"   # expect 402
curl -s "$API_URL/llms.txt" | head -20
```

## 3b. Deploy the web UI

A second Cloud Run service, static files behind nginx. It talks to the API the
same way any browser would — same origin rules, same 402 — so it needs no
service account, no Firestore, no secrets and no Vertex.

```bash
gcloud run deploy web \
  --image="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/web:$(git rev-parse --short HEAD)" \
  --region="$REGION" \
  --allow-unauthenticated \
  --cpu=1 --memory=512Mi \
  --concurrency=80 \
  --min-instances=0 \
  --max-instances=4 \
  --timeout=30s
```

Why these differ from the API's numbers: nothing here is slow, so concurrency is
high and the timeout is short. `--min-instances=0` because a cold start on a
static file is a few hundred milliseconds, not the ~15s an ADK call costs — the
warm-instance argument that justifies the API's floor does not apply.

**The image builds the wallet bundle and runs its tests.** `Dockerfile.web`
stage one is `npm ci && npm run build && npm test`, so a wallet-layer test
failure fails the image rather than shipping a bundle that signs the wrong
thing. Nothing node-shaped survives into the runtime layer.

### Pointing it at the API

The UI defaults to same-origin for the API, which is wrong for two separate
services. Set the API base at build time or let a visitor override it in
Developer settings; the simplest correct answer is a single domain with a path
split, once a domain exists (§1.1 of `TODO.md`):

```
api.playclock.xyz   -> the api service
playclock.xyz       -> the web service
```

Until then the UI reads `?api=` and remembers it, which is enough for a demo but
not for an advert.

### Smoke test

```bash
export WEB_URL=$(gcloud run services describe web --region="$REGION" --format='value(status.url)')

curl -s -o /dev/null -w '%{http_code}\n' "$WEB_URL/"              # 200
curl -s -o /dev/null -w '%{http_code}\n' "$WEB_URL/analyze"        # 200
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' "$WEB_URL/dist/wallet.js"   # 200, ~1.5MB
curl -s -o /dev/null -w '%{http_code}\n' "$WEB_URL/package.json"   # 404 — build inputs must not ship
```

Then, in a browser with Pera or Defly installed: connect, buy the cheapest
endpoint, and confirm the receipt renders a txid that resolves on the explorer.
**Do that on TestNet first.** The wallet path has been tested offline against
known-good vectors (`npm test` in `web/`), but signing through a real wallet on
a real network has its own failure modes, and the first MainNet settle is the
one that permanently catalogues the resource URL (§7).

## 4. Deploy the ingest job

One job, three schedules, task chosen per invocation (`ingest/__init__.py`).

```bash
gcloud run jobs deploy ingest \
  --image="$INGEST_IMAGE:$(git rev-parse --short HEAD)" \
  --region="$REGION" \
  --service-account="$INGEST_SA" \
  --cpu=1 --memory=2Gi \
  --task-timeout=45m \
  --max-retries=1 \
  --set-env-vars="\
STORE_BACKEND=firestore,\
GOOGLE_CLOUD_PROJECT=$PROJECT_ID,\
GOOGLE_CLOUD_LOCATION=global,\
GOOGLE_GENAI_USE_ENTERPRISE=true,\
ENGINE=adk,\
MODEL_ID=gemini-3.7-flash,\
QUALITY_JUDGE=true,\
NARRATOR_TIMEOUT_SECONDS=120,\
MODEL_RETRY_ATTEMPTS=6,\
SEASON=2026"
```

The last seven are for the `precompute` task, which runs the real ADK pipeline
and, with `QUALITY_JUDGE=true`, scores each board it warms (`ingest/judge.py`).
`MODEL_RETRY_ATTEMPTS=6` is above the api's default of 4 because nothing is
waiting on this job: a Vertex 429 on one of a board's ~9 calls is retried
inside the SDK (2s, 4s, 8s, 16s, 30s) rather than failing the run and
re-buying the calls already made — which is what a third of pipeline runs
were doing before it existed (DESIGN_NOTES §26).
The draft board alone is warmed through the narrated engine — the synthesizer
returned 30 of 200 rows with deltas that did not add up, so the board is
computed and the model writes only its prose — and a 200-row body needs a
longer narrator budget than the api's 45s; a timeout still serves the computed
board with template notes.
That also means the ingest service account needs Vertex, which the §1 bootstrap
does not grant it:

```bash
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$INGEST_SA" --role=roles/aiplatform.user
```

`GOOGLE_CLOUD_LOCATION=global` is not optional — `gemini-3.7-flash` resolves only
at `locations/global` (DESIGN_NOTES, "ADK on Vertex").

`--memory=2Gi` is sized for the `stats` task: nflverse downloads plus Polars
aggregation are the heavy part. `trending` finishes in seconds.

`--task-timeout=45m`, up from 15m, is sized for `precompute`. Measured on real
runs: `report` took **988s (16.5 min)**, `sleepers` 374s, `waivers` 117s, and a
board that trips Vertex's 429 waits out two minutes of backoff before retrying.
`report` alone exceeds the old 15m limit, so that timeout was not conservative —
it would have killed the task outright.

Those numbers are also the clearest argument for this task existing: none of
those boards can be generated inside a paid request under any timeout a caller
would tolerate. Warmed, the whole paid `/v1/report` flow takes 15s end to end,
most of it Algorand settlement.

Run each task once by hand, **in this order**, before wiring the crons — a cold
store must get `nightly` (which builds `id_map/`) before `stats`, or every stat
row lands under a gsis id the name index cannot reach:

```bash
gcloud run jobs execute ingest --region="$REGION" --args=--task,nightly --wait
gcloud run jobs execute ingest --region="$REGION" --args=--task,stats   --wait
# Before Week 1, weekly stats may not exist yet. Advance schedule metadata alone:
gcloud run jobs execute ingest --region="$REGION" --args=--task,schedule,--season,$SEASON --wait
gcloud run jobs execute ingest --region="$REGION" --args=--task,trending --wait
gcloud run jobs execute ingest --region="$REGION" --args=--task,precompute --wait
curl -s "$API_URL/v1/health" | jq .data_freshness   # every dataset should be stamped
```

`--task` takes a comma-separated chain, run in the order given; the scheduler
uses `stats,backtest` so claims are scored in the same run that wrote the
lines (a separate schedule can overlap a long stats run and grade a
half-written week permanently).

**Rebuilding a prior season's rollups** — needed once after the gsis bridge
(DESIGN_NOTES §23), and any time the id map improves — is a different
command, and the difference matters:

```bash
gcloud run jobs execute ingest --region="$REGION" \
  --args=--task,stats,--season,2025,--stats-only --wait
gcloud run jobs execute ingest --region="$REGION" --args=--task,precompute,--force --wait
```

Never run `--task stats --season 2025` without `--stats-only` on the live
store. The plain task writes the 2025 schedule and `meta/schedule_weeks`
first, at which point the season guard 503s every paid route (DESIGN_NOTES
§20); it also clears the 2026 preseason-gap marker and overwrites this week's
injuries and depth charts with last year's. `--stats-only` writes weekly
stats, usage trends and def-vs-pos and nothing that describes the current
season.

## 5. Cloud Scheduler

Times are Eastern; `--time-zone` handles the DST shift so the crons stay put
relative to the NFL week (tech spec §4.2, §6).

The production definitions live in `infra/terraform/hardening/`; Terraform owns
the scheduler service account, job-level `roles/run.invoker` binding, all five
jobs, the operator email channel, and the ingest-failure alert. Apply that root
instead of creating these resources ad hoc. The commands and rationale below
remain the human-readable equivalent of the declared resources.

```bash
gcloud iam service-accounts create playclock-scheduler --display-name="Play Clock scheduler"
export SCHED_SA=playclock-scheduler@$PROJECT_ID.iam.gserviceaccount.com
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$SCHED_SA" --role=roles/run.invoker

JOB_URI="https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/ingest:run"

# Sleeper players dump + id map — nightly, 04:00 ET (quiet hours; Sleeper asks
# for at most one dump a day).
gcloud scheduler jobs create http ingest-nightly \
  --location="$REGION" --schedule="0 4 * * *" --time-zone="America/New_York" \
  --uri="$JOB_URI" --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--task","nightly"]}]}}'

# nflverse stats/usage/def-vs-pos — Tue/Thu/Sat 09:00 ET. Tuesday catches the
# post-MNF finalization, Thursday and Saturday catch the injury reports. The
# backtest runs in the same invocation, after stats: a separate schedule could
# overlap a still-running stats task and grade a half-written week for good.
gcloud scheduler jobs create http ingest-stats \
  --location="$REGION" --schedule="0 9 * * 2,4,6" --time-zone="America/New_York" \
  --uri="$JOB_URI" --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--task","stats,backtest"]}]}}'

# Sleeper trending add/drop — every 30 minutes, all week. This is the market
# signal every board endpoint is ranked on.
gcloud scheduler jobs create http ingest-trending \
  --location="$REGION" --schedule="*/30 * * * *" --time-zone="America/New_York" \
  --uri="$JOB_URI" --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--task","trending"]}]}}'
```

The remaining crons are the ones that matter most for latency. `precompute`
generates the four league-wide boards and warms `response_cache`, turning a paid
call from a ~55s LLM run into a Firestore read.

It needs **two** schedules, for two different reasons.

The first is a keep-warm loop, and its cadence is not a matter of taste. A warmed
entry is not permanent — `CACHE_TTL_SECONDS` expires `trending` after 6h and
`sleepers`, `waivers` and `report` after 12h. A task that ran only alongside
`ingest-stats` would leave most of the week with no warm entry at all, and the
first caller of each gap would pay the live generation: for `sleepers` that is
374 seconds, longer than the 300s Cloud Run request timeout, so they would be
charged and then time out. Run it **every two hours, all week**:

```bash
# Keep-warm loop. Every 2h; must stay well inside the 6h TTL.
gcloud scheduler jobs create http ingest-precompute \
  --location="$REGION" --schedule="20 */2 * * *" --time-zone="America/New_York" \
  --uri="$JOB_URI" --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--task","precompute"]}]}}'
```

Most of those runs do nothing: a board is regenerated only once it drops below
`REFRESH_WINDOW_SECONDS` (2.5h) of remaining life, so a 6h board regenerates
every **4h** (skipped at 4h left, rebuilt at 2h left) and a 12h board every
10h. That window refreshes *ahead* of expiry deliberately. Waiting for an entry
to actually expire would still leave a gap, because the entry has to survive
until its replacement is *written* and `report` takes 988s to write.

That arithmetic is the Vertex bill: at ~9 model calls a board, `trending`
alone costs six generations a day and every 12h board 2.4. The lever is the
TTL in `api/routes/__init__.py` — a product decision, since `/v1/catalog`
advertises it — not the loop interval, and not skipping "unchanged" boards
(DESIGN_NOTES §26 explains why that was tried and dropped).

**If you change either number, keep `interval + slowest board < window < shortest
TTL`** (`--refresh-window <hours>` moves the window). At 2h and 2.5h that is
`2h + 988s` against 2.5h — about 13 minutes of margin. Too small a window and a
board expires between two runs with nothing reporting it; too large and every run
regenerates everything, visible only on the Vertex bill.

Budget note: measured over 2026-09-19..21, with `sleepers` and `waivers` still
at 6h, this cadence was 22–24 board generations a day plus ~13 pipeline runs a
day thrown away on Vertex 429s, against 14 paid calls in the first three weeks
of MainNet — about $3.30/day of Vertex, two thirds of the project's spend
(DESIGN_NOTES §26). The 12h TTLs and the per-request retry above are expected
to bring it to ~16 generations and ~0 wasted runs a day; the numbers to watch
are `warmed` in the precompute summary, Monitoring's `model_invocation_count`
by `response_code`, and the Vertex line against the $50 alert below.

The second schedule exists because warming reads what ingest wrote. A board
warmed before `ingest-stats` is built from yesterday's numbers and is then served,
unchallenged, until it refreshes. So force a regeneration behind each stats run:

```bash
# Post-stats refresh. 09:40 ET Tue/Thu/Sat, 40 minutes behind ingest-stats, so
# the boards pick up the numbers that run just wrote instead of waiting out the
# keep-warm cadence. --force because the entries are still live.
gcloud scheduler jobs create http ingest-precompute-refresh \
  --location="$REGION" --schedule="40 9 * * 2,4,6" --time-zone="America/New_York" \
  --uri="$JOB_URI" --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  --message-body='{"overrides":{"containerOverrides":[{"args":["--task","precompute","--force"]}]}}'
```

`--only draft_board` (comma-separated keys) warms one board instead of all
five — the way to regenerate a single board after a data fix without paying
for the other four.

It only warms each endpoint's **default** parameters, which is what an
unparameterized agent asks for. A caller passing `?limit=40` still generates live
and waits; warming the cartesian product would cost more than it saves.

A run that leaves any board cold — generation failed all three attempts, ran
past its 1500s budget (`BOARD_BUDGET_SECONDS`, so one slow `report` cannot eat
the 45m task timeout and kill the job before the later boards and the summary),
or the data it reads was never ingested — **exits non-zero** and trips the ingest-failure
alert below. That is deliberate: the endpoint silently falling back to a 374s
paid call is exactly the failure nothing else reports. Warming also refuses to
run a board whose datasets are missing rather than caching the empty result the
engine would return, since a cached empty board is billed as a cache *hit* for
the whole TTL, bypassing the 503 that would otherwise refuse the sale.

## 6. Monitoring and budget

```bash
# Budget alert at $50/mo (tech spec §7).
gcloud billing budgets create \
  --billing-account=<BILLING_ACCOUNT_ID> \
  --display-name="Play Clock" \
  --budget-amount=50USD \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

Expected steady state is well under that: Cloud Run with `min-instances=1` is
the floor (~$15-30), Vertex is usage-based at a target of <$0.02 per paid call,
and Firestore is pennies. If the bill runs away, the two suspects are
`min-instances` and an endpoint that stopped hitting its response cache.

Log-based metrics worth creating (each mirrors a number in PRD §6):

| Metric | Filter | Why |
|---|---|---|
| paid calls | writes to `receipts/` | mirrors the challenge leaderboard |
| 402 -> paid conversion | count of 402 responses vs. receipts | funnel health |
| ingest failures | job execution with non-zero exit | stale data degrades answers silently |
| degraded answers | log `engine adk failed .* answering from` | ADK is down and every caller is quietly getting the fallback |
| narrator degraded | log `serving the deterministic body` (api, `ENGINE=narrated`) | every answer is the template again and nobody sees it |
| board quality flagged | log `board quality flagged` (ingest job) | a warmed board failed the value gate or the judge; it is being served for its whole TTL |
| wrong-season refusals | 503s naming a season mismatch | ingest is filling the store for the wrong year |
| p95 latency | Cloud Run request latency, paid paths | the <20s promise |

Alert on **ingest failure** first. A broken API is loud; a stale ingest is
silent and still charges people.


### Receipt-stat rollup migration

The free stats endpoint reads 32 fixed Firestore rollup shards once a network is
backfilled. Deploy the receipt-rollup writer first, then run the backfill from a
one-off API image with the same MainNet/TestNet environment as the service:

```bash
gcloud run jobs deploy backfill-receipt-stats \
  --image="$API_IMAGE:$(git rev-parse --short HEAD)" \
  --region="$REGION" \
  --service-account="$API_SA" \
  --task-timeout=30m \
  --max-retries=0 \
  --command=python \
  --args=-m,api.scripts.backfill_receipt_stats \
  --set-env-vars="\
STORE_BACKEND=firestore,\
GOOGLE_CLOUD_PROJECT=$PROJECT_ID,\
X402_NETWORK=$X402_NETWORK"

gcloud run jobs execute backfill-receipt-stats --region="$REGION" --wait
```

Run it once per payment network, with `X402_NETWORK` matching the service whose
receipts you are rolling up. Delete the job afterwards
(`gcloud run jobs delete backfill-receipt-stats --region="$REGION"`); it exists
only for the migration.

`--command=python` is deliberate: `python` on the runtime image's `PATH` is
already the venv interpreter, and `uv` is a builder-stage tool that the runtime
stage does not copy. The module lives under `api/` for the same reason — that
is the only source tree the runtime stage copies, so a top-level `scripts/`
module could not be executed from the deployed image at all.

The command is safe to rerun. Each historical receipt and each concurrent new
settlement has a transactionally-created event marker, so it contributes once;
the completion marker is written only after every receipt in the indexed network
query has been applied. Before that marker exists, stats deliberately retains
the legacy exact receipt scan rather than publishing an incomplete total. The
bounded-read benefit begins after this command completes.

## 7. MainNet cutover checklist

Do this only after a TestNet payment has verified, settled and produced a
receipt end to end.

0. **Opt both MainNet wallets into USDC first** —
   `uv run python infra/optin.py optin --network mainnet --all`. If the merchant
   is not opted in, settlement fails at simulate and the caller sees a second
   402 with nothing billed (`DESIGN_NOTES.md:151`). Confirm with
   `uv run python infra/optin.py status --network mainnet`.
1. `X402_NETWORK=mainnet` and a **MainNet** `X402_PAY_TO` (the same single
   address for every endpoint — the challenge's Composite entry type).
2. `X402_ASSET_ID` left at `0` so the verified MainNet USDC ASA (31566704) is
   used, or set explicitly if it ever changes.
3. `X402_FACILITATOR_URL=https://facilitator.goplausible.xyz` — **never** the
   SDK's `x402.org` default, which would forfeit leaderboard attribution.
4. `X402_RESOURCE_BASE_URL` set to the real public https origin. The app refuses
   to serve a live 402 that would advertise localhost or a plain `http://` URL
   (what Cloud Run's request URL reads without it), but it cannot know a
   wrong-but-public host is wrong.
5. `X402_CHALLENGE_TAG=x402-global-challenge` present in `accepts[].extra.tag` —
   verify with `curl -s "$API_URL/v1/trending" | jq '.accepts[0].extra.tag'`.
6. Make the first real MainNet USDC payment yourself and confirm the receipt in
   `receipts/`. Bazaar listing happens implicitly, from the discovery extension
   on that first settled payment — so it must be against the public URL, never
   localhost or a preview revision.
7. Re-run the golden evals against the deployed model
   (`uv run python -m api.evals.run_evals --engine adk`) from an authenticated
   shell before announcing.
8. Confirm `--min-instances=1` is on for the season.
9. Confirm the endpoint actually surfaced on the challenge leaderboard:
   `curl -s https://facilitator.goplausible.xyz/data/merchants | jq '.[] | select(.sub | test("playclock"))'`.
   Attribution that never appears here is attribution that does not count.
