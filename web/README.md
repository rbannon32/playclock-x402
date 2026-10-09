# web/ — Play Clock static site

Four HTML pages, one stylesheet, ES modules served as written — **plus one bundled
wallet layer**. Everything except `js/wallet/` needs no build, no npm install and no
framework; open the files and what you read is what the browser runs. There is no CDN.
Every byte the browser loads is in this directory, so the site is auditable in a single
reading, deployable by copying a folder, and works offline apart from the API calls
themselves.

```
web/
├── index.html          landing: free trending preview, pricing, "for agents"
├── analyze.html        endpoint picker → per-endpoint form → pay → result
├── roster.html         Sleeper username → roster audit (+ team-report upsell)
├── docs.html           agent quickstart: the 402 flow, curl, OpenAPI/llms.txt
├── css/style.css       design tokens + every component
└── js/
    ├── config.js       API base URL + mock-mode resolution (query param → localStorage)
    ├── dom.js          el()/replace()/format helpers — the framework substitute
    ├── partials.js     shared nav + footer + developer drawer
    ├── api.js          fetch helpers and the x402 402→pay→retry handshake
    ├── payment.js      THE SEAM: PaymentProvider, MockPaymentProvider, WalletPaymentProvider
    ├── endpoints.js    per-endpoint form fields + offline fallback catalog
    ├── render.js       one renderer per response contract in api/schemas.py
    ├── flow.js         form building + pay-and-reveal, shared by analyze/roster
    └── page-*.js       one controller per page
```

## Serve it locally

The site is fully static — any file server works. It does **not** need the API service to
be running; without a backend it renders with published fallback prices and shows
"API unreachable" states rather than throwing.

```bash
cd web
python3 -m http.server 8000
# → http://localhost:8000/
```

ES modules require an HTTP origin, so opening `index.html` over `file://` will not work.

### Pointing at an API on another port

On `playclock.xyz` and `www.playclock.xyz`, the production default is
`https://api.playclock.xyz`. Other hosts remain **same-origin**: every request is
a relative URL like `/v1/catalog`, which keeps local and preview hosting portable.

For local development the API is usually on `:8080` while the static files are on `:8000`.
Two ways to bridge that:

- **Query param (shareable):** `http://localhost:8000/?api=http://localhost:8080` — the
  value sticks in `localStorage` for subsequent pages.
- **Developer settings:** the collapsed drawer at the bottom of every page sets the same
  value, plus the mock-payment toggle.

The API must send permissive CORS for the cross-port case, including
`Access-Control-Expose-Headers: PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE`
(the payment layer already emits that header on 402s and on settled 2xx responses).

## The wallet layer

`js/wallet/` is the one part that is compiled, because Pera, Defly and algosdk cannot
be vendored honestly — a committed minified bundle has no lockfile and no reviewer.

```bash
cd web
npm ci          # installs algosdk + the Pera and Defly SDKs
npm test        # what we sign and what we send, in node — no wallet, no chain
npm run build   # esbuild -> dist/wallet.js (~1.5MB)
```

**You do not need any of that for local development.** `payment.js` loads the bundle
with a *dynamic* import, only when someone reaches for a wallet, so every page and the
whole mock-payment flow below work with `dist/` absent. If a wallet is needed and the
bundle is missing, it says so.

The two files that decide whether the right money moves stay out of the bundle and
under test:

| file | answers |
|---|---|
| `js/wallet/exact-avm.js` | what we sign — the transaction group for one 402 |
| `js/wallet/envelope.js` | what we send — the V2 payload and its base64 header |

`js/wallet/connectors.js` wraps Pera and Defly, normalising the two things they
disagree about: the numeric chain id, and whether `signTransaction` returns a
slot-aligned array or only the blobs it signed.

## Testing the mock pay flow end to end

`X402_MODE=mock` makes the backend accept mock payments: the literal header `mock-paid`, or
the base64 `{"payload": {"mock": true, "nonce": ...}}` envelope with a fresh nonce per call
that `MockPaymentProvider` sends. That exercises the entire flow — 402, payment
requirements parsing, retry with `PAYMENT-SIGNATURE`, settlement receipt decoding, result
rendering — with no wallet and no chain.

```bash
# terminal 1 — API in mock payment mode
X402_MODE=mock uv run uvicorn api.main:app --reload --port 8080

# terminal 2 — static site
cd web && python3 -m http.server 8000
```

Then open:

```
http://localhost:8000/analyze.html?api=http://localhost:8080&mock=1
```

A yellow **TEST MODE** banner confirms the mock provider is active. Pick an endpoint, submit,
and you should see the status line walk through *asking → payment required → paid →
generating*, then the rendered verdict with a settlement receipt at the bottom.

Verify the same thing from the shell:

```bash
curl -si localhost:8080/v1/trending | head -1              # HTTP/1.1 402 Payment Required
curl -s localhost:8080/v1/trending -H 'PAYMENT-SIGNATURE: mock-paid' | jq .verdict
```

`?mock=1` and `?mock=0` both persist; the Developer settings drawer is the UI equivalent.
Never ship a deployment where mock mode is the default — a live backend rejects
`mock-paid` at verify, which is the intended safety property.

## Payment seam

`js/payment.js` is the only file that knows how a payment is produced. It exports one
interface:

```js
class PaymentProvider {
  async pay(paymentRequired) -> string   // value of the PAYMENT-SIGNATURE header
}
```

- `MockPaymentProvider` returns a base64 mock payload with a fresh nonce per call (a distinct
  payment each time, like a real wallet). Active when mock mode is on.
- `WalletPaymentProvider` signs real Algorand USDC transfers through Pera or Defly. It
  validates the quoted network, asset, recipient and amount before signing, checks opt-in
  and balance when algod is reachable, builds the exact AVM transaction group, and returns
  the V2 `PAYMENT-SIGNATURE` envelope. The wallet bundle is loaded only when this path is
  used.

`api.js` owns the surrounding 402 → sign → retry → receipt flow. It journals the signed
request before sending it and retains an uncertain payment for replay, so a timeout or
dropped response does not silently turn into a second charge.

## Deployment

Any static host. The site has no server-side requirements and no runtime dependencies, but
the wallet layer must be built before copying or uploading `web/`:

```bash
cd web
npm ci
npm run build
```

That creates the gitignored `dist/wallet.js` loaded on demand by real wallet payments.

**Firebase Hosting** (rewrite `/v1/**`, `/docs`, `/openapi.json`, `/llms.txt` to the API
service so the site can stay same-origin):

```json
{
  "hosting": {
    "public": "web",
    "ignore": ["**/.*", "**/README.md"],
    "rewrites": [
      { "source": "/v1/**", "run": { "serviceId": "api", "region": "us-east4" } },
      { "source": "/docs", "run": { "serviceId": "api", "region": "us-east4" } },
      { "source": "/openapi.json", "run": { "serviceId": "api", "region": "us-east4" } },
      { "source": "/llms.txt", "run": { "serviceId": "api", "region": "us-east4" } }
    ]
  }
}
```

**Cloud Run + nginx** — use the repository's `Dockerfile.web`, which builds and tests the
wallet bundle before copying the static site into nginx. Proxy `/v1/` to the API service to
keep same-origin.

**Mounted by the API service** — if `api/main.py` mounts this directory as static files at
`/` after the build step above, everything works unchanged with no configuration, because
the default API base is same-origin. The site does not depend on being mounted that way.

Content-Security-Policy friendly: no external scripts, styles, fonts or images. The only
inline anything is a data-URI SVG favicon. A policy as tight as
`default-src 'self'; img-src 'self' data:; connect-src 'self'` works as-is when the API is
same-origin (widen `connect-src` for a split-origin deployment).

## Accessibility and behaviour notes

- Skip link, labelled form controls, `aria-current` on the active nav item,
  `role="status"` on live regions, visible focus rings, and reduced-motion support.
- Every fetch has a timeout and an error path; nothing logs an uncaught rejection when the
  API is absent.
- All user-visible content is built with `createElement`/`textContent`, never `innerHTML`
  with API data, so a hostile response body cannot inject markup.
- Prices and endpoint copy come from `/v1/catalog`; `js/endpoints.js` holds a published
  fallback, and the UI says so when it is being used.
- The landing-page question router selects an existing typed endpoint; it never turns free-form
  text into an unreviewed payment request. The user still completes the endpoint's real form.
- Settled receipt metadata is kept in local storage for the on-device history rail. Response
  bodies are not persisted. Paid results can be exported as a 1200×630 PNG or copied as text.

## Known gaps

- **Manual roster paste** for non-Sleeper managers — the API accepts a `roster[]` array on
  `POST /v1/roster`, but the UI only offers the username path.
