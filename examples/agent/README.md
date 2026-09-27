# Example fantasy agent

A single runnable script that **discovers** Play Clock, **pays** for an
analysis over [x402](https://x402.org) with USDC on Algorand, and **reads the
answer plus the settlement receipt**. This is the reference client PRD §7
promises: the thing another agent author copies.

```
examples/agent/
  fantasy_agent.py   the agent (heavily commented; importable core)
  selftest.py        end-to-end check of the agent (run in CI)
  README.md          this file
```

Dependencies are `httpx` and `x402-avm` only — both already in the repo venv, so
from the repo root everything runs under `uv run`. Standalone, outside this repo:

```bash
pip install "httpx" "x402-avm[clients,avm]"
python fantasy_agent.py --base-url https://<host> --ask trending
```

`[clients]` brings the HTTP client helpers; `[avm]` brings py-algorand-sdk for
the real payment path. The mock path never imports algosdk.

---

## What it demonstrates

* **Discovery without paying.** `GET /v1/catalog` is the machine-readable menu —
  path, method, price in USDC, request/response schema names, cache TTL, plus the
  payment config (network, `payTo`, USDC ASA id, facilitator, challenge tag).
  `GET /llms.txt` is the same story in prose for a language model. Neither costs
  anything, and neither needs a wallet.
* **The full 402 handshake**, printed step by step instead of hidden inside a
  transport: the quote, the price check, the signed payload, the retry, the
  receipt.
* **A price guard.** `--max-price` (default `1.00` USDC) is checked against the
  quote *before* anything is signed. An agent that pays whatever a server asks is
  a wallet-draining bug; the ceiling is enforced twice — explicitly in
  `ask_once()`, and again by the SDK's own `max_amount` policy registered on the
  client.
* **A signer seam with two implementations** — `MockSigner` (no chain) and
  `AlgorandSigner` (real USDC) — behind one interface, which happens to be the
  SDK's own `SchemeNetworkClient` protocol, so `x402Client` drives both without
  knowing the difference.
* **The failure modes that actually happen:** API unreachable, price above the
  ceiling, payment refused on retry, `503` because ingestion has not run yet
  ("you were not charged"), and upstream `502`s. Each is its own exception with a
  message an operator can act on.

## The x402 flow

```
  agent                                             Play Clock
    |                                                      |
    |  GET /v1/catalog                                     |   free: the menu,
    |----------------------------------------------------->|   prices, payment
    |<-----------------------------------------------------|   config
    |          200 {endpoints:[{path, price_usdc, ...}]}    |
    |                                                      |
    |  GET /v1/trending                (no payment yet)    |
    |----------------------------------------------------->|
    |<-----------------------------------------------------|
    |   402 Payment Required                                |
    |   body  {x402Version:2, error, resource,              |   the same blob is
    |          accepts:[{scheme:"exact",                     |   also base64 in the
    |                    network:"algorand:<genesis>",       |   PAYMENT-REQUIRED
    |                    asset:"10458941",                   |   response header
    |                    amount:"100000",   <- atomic USDC   |
    |                    payTo:"<address>",                  |
    |                    extra:{decimals:6, tag:...}}],       |
    |          extensions:{bazaar:...}}                      |
    |                                                      |
    [ guard ]  amount/1e6 = 0.10 USDC <= --max-price ?      |   refuse here, and
    |          no  -> stop, nothing signed                   |   nothing is signed
    |          yes -> build a payment for accepts[0]         |
    |                                                      |
    |  GET /v1/trending                                     |   the SAME request,
    |  PAYMENT-SIGNATURE: base64({x402Version:2,            |   now carrying the
    |      payload:{...signed transfer group...},            |   payment
    |      accepted:{...accepts[0]...}})                     |
    |----------------------------------------------------->|
    |                                        verify -> run  |   verified before the
    |                                        -> settle      |   analysis, settled
    |<-----------------------------------------------------|   only after 2xx
    |   200 {verdict, confidence, reasoning,                 |
    |        stats_cited[], sources[], meta}                 |
    |   PAYMENT-RESPONSE: base64({success, transaction,      |   the on-chain
    |        network, payer})                                |   receipt
```

Two properties worth relying on when you write your own client:

* **A failed analysis is never charged.** Verification happens before the handler
  and settlement only after a 2xx, so a `503`/`502`/`500` costs nothing.
* **One payment buys one request.** A verified payment is remembered for 60
  seconds keyed by the request it paid for; replaying it against a *different*
  question is rejected with a fresh 402. Mint a new payment per call — the mock
  signer's `nonce` and the real signer's per-transaction note both do this.

## Quickstart: local mock server, no chain, no wallet

Terminal 1 — the API with mock payments (`X402_MODE=mock` accepts a payment whose
payload carries `{"mock": true}` and mints a fake receipt; no facilitator, no
chain):

```bash
X402_MODE=mock uv run uvicorn api.main:app
```

Terminal 2 — the agent:

```bash
uv run python examples/agent/fantasy_agent.py --base-url http://localhost:8000 --mock
```

You will see the catalog, the 402 with its quote, the payment, and the retry. A
freshly started server has an **empty store**, so the analysis itself comes back
`503 Ingestion has not produced the data 'trending' needs yet ... You were not
charged` — which is the correct, and demonstrable, behaviour: the payment
handshake completed and nothing was billed.

To see a real board come back, the API and the ingest job must share a
**persistent** store: the default `STORE_BACKEND=memory` lives inside each
process, so running `python -m ingest.job` separately populates a store that
vanishes when it exits and never reaches the running server. Either:

- point both processes at Firestore (`STORE_BACKEND=firestore` plus GCP
  credentials, per `infra/env.example`) and run `python -m ingest.job --task
  nightly` then `--task stats` then `--task trending` before asking, or
- run `uv run python examples/agent/selftest.py`, which spins up an in-process
  server with a seeded store and shows the full happy path — settled receipt,
  verdict, cited stats — with no cloud setup at all.

More asks — repeat `--ask` as often as you like:

```bash
uv run python examples/agent/fantasy_agent.py --base-url http://localhost:8000 --mock \
    --ask trending \
    --ask "player:Bijan Robinson" \
    --ask "matchup:Bijan Robinson,Breece Hall" \
    --max-price 0.30 --json
```

Accepted asks: `trending`, `sleepers`, `waivers`, `report`, `player:<name>`,
`matchup:<name>,<name>[,<name>[,<name>]]`.

### End-to-end check with a seeded store

`selftest.py` runs the agent's own `discover()` / `ask_once()` against an
in-process ASGI app whose store has been seeded with the golden fixture season,
so the *whole* happy path — 402, payment, 200, receipt, verdict — is exercised
without a server or a chain. It also covers the price guard, the cold-store 503,
an unreachable host, and builds a real Algorand payment group offline against a
stub Algod:

```bash
uv run python examples/agent/selftest.py    # exits non-zero on any failed check
```

## Paying for real: TestNet and MainNet

```bash
export ALGORAND_MNEMONIC="word word word ... word"   # 25 words, never committed
uv run python examples/agent/fantasy_agent.py \
    --base-url https://<deployed-host> --ask trending --max-price 0.25
```

The wallet needs three things before any of this works:

1. **ALGO for fees.** A few thousand microALGO. (When the server's quote carries
   `extra.feePayer`, the facilitator covers fees and the agent's transfer is
   built with a zero fee — but keep a balance anyway; the plain path pays its
   own.)
2. **A USDC opt-in.** Algorand accounts must opt in to an ASA before they can
   hold or transfer it. Opt in to **10458941** on TestNet, **31566704** on
   MainNet — the same ids the catalog reports in `asset_id` and the 402 quotes in
   `accepts[].asset`. Without the opt-in the transfer fails at the facilitator's
   simulate step and you get a 402, not a charge.
3. **USDC.** TestNet USDC comes from the Circle/Algorand testnet faucets; MainNet
   USDC is real money, and `--max-price` is the only thing between an agent loop
   and a bad quote.

The agent refuses to sign a transfer of any ASA other than the network's
canonical USDC (`--allow-any-asset` overrides, deliberately verbose). Custom
Algod endpoints: `--algod-url`, or the SDK's `ALGOD_TESTNET_URL` /
`ALGOD_MAINNET_URL` environment variables; the default is AlgoNode.

Settlement goes through the **GoPlausible** facilitator
(`https://facilitator.goplausible.xyz`) — the one the Algorand Global x402
Challenge requires — and every quote carries `extra.tag = "x402-global-challenge"`
for leaderboard attribution.

## Discovery beyond this script

* **`/llms.txt`** — the agent quickstart in prose, generated from the same specs
  and prices as the catalog: what the service is, how to pay, what each endpoint
  costs, what the response contract guarantees.
* **`/v1/catalog`** — the machine-readable version of the same thing.
* **`/openapi.json`** — full request/response schemas.
* **The Bazaar** — every 402 carries an `extensions.bazaar` discovery block
  describing the request and response shapes, and the facilitator catalogs the
  resource URL from the first settled payment. That is how an agent that has
  never heard of this service finds it: browse the facilitator's catalog, read
  the discovery block, pay.

## Using the SDK's automatic transport instead

This script drives the handshake by hand because the handshake is the lesson. In
production, `x402-avm` will do it for you — same signer, same client, no visible
402:

```python
from x402.http.clients import x402HttpxClient
from fantasy_agent import MockSigner, Payer

payer = Payer(signer=MockSigner(), max_price_usdc=0.25)
async with x402HttpxClient(payer.client, base_url="http://localhost:8000") as http:
    response = await http.get("/v1/trending")  # pays and retries transparently
```

The trade-off: the transport hides the quote, so the price ceiling has to live in
the SDK policy (`max_amount`, already registered by `Payer`) or in an
`on_before_payment_creation` hook returning `AbortResult` — you no longer get to
look at the number yourself before deciding.
