"""FastAPI application: wiring, OpenAPI metadata, and the agent-facing `/llms.txt`.

``uvicorn api.main:app`` serves the whole product — free routes, paid routes, the
x402 payment layer and the analysis engine all run in one Cloud Run service
(tech spec §1). There is no separate agent process: keeping the payment
dependency and the engine in one deployable is what makes "verify -> handle ->
settle" a single in-process transaction.

Wiring order in :func:`create_app`:

#. **CORS** — the web UI and browser wallets are cross-origin, and they must be
   able to *read* the x402 headers, not just receive them. ``expose_headers``
   carries ``PAYMENT-REQUIRED`` / ``PAYMENT-RESPONSE`` (plus the V1 legacy
   mirror). :class:`~api.x402.PaidRoute` sets the same
   ``Access-Control-Expose-Headers`` value on paid responses for non-CORS
   clients; Starlette overwrites rather than appends, and both come from the one
   :data:`~api.x402.schemas_compat.EXPOSED_HEADERS` constant, so there is no
   duplication or drift.
#. **x402 error rendering** — :func:`api.x402.install_x402_handlers` puts the
   402 payload at the root of the body app-wide, covering anything not mounted
   through :func:`api.x402.paid_router`.
#. **Routers** — free first (cheap, cacheable, crawlable), then paid.
#. **/llms.txt** — the agent quickstart, generated from the same specs and
   prices as ``/v1/catalog`` so the two can never disagree.

The lifespan owns exactly one resource: the :class:`~api.core.store.Store`
(a Firestore ``AsyncClient`` in production), constructed on startup and closed on
shutdown, plus the engine's own teardown and the facilitator's HTTP client.
"""

from __future__ import annotations

import html
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.responses import Response

from api.agents import get_engine
from api.core.config import ENDPOINT_KEYS, Settings, get_settings
from api.core.store import get_store
from api.routes import API_VERSION, CACHE_TTL_SECONDS, free, paid
from api.schemas import ATTRIBUTION
from api.x402 import ENDPOINT_SPECS, install_x402_handlers
from api.x402.facilitator import close_facilitator
from api.x402.schemas_compat import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    RESOURCE_BASE_URL_ENV,
    X_PAYMENT_RESPONSE_HEADER,
)

logger = logging.getLogger(__name__)

#: Headers a browser client must be able to read cross-origin.
CORS_EXPOSE_HEADERS = [
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
]

API_TITLE = "Play Clock API"

API_DESCRIPTION = f"""
Pay-per-analysis NFL fantasy football intelligence, priced in USDC micropayments on
Algorand via the [x402](https://x402.org) protocol. Humans pay from a wallet; AI agents
pay programmatically. There are no accounts and no subscriptions — the wallet is the
identity, and each answer is bought one at a time.

**How to call a paid endpoint**

1. Call it with no payment. You get `402 Payment Required` whose body carries the
   payment requirements (`accepts[]`), the resource description, and a Bazaar
   discovery block describing the exact request and response shape.
2. Pay and retry with the signed payload in the `{PAYMENT_SIGNATURE_HEADER}` header
   (the legacy `X-PAYMENT` name is also accepted).
3. You get the analysis, plus a settlement receipt in `{PAYMENT_RESPONSE_HEADER}`.

Payment is verified before the handler runs and settled only after it returns 2xx: a
failed analysis is never charged.

**Free endpoints:** `/v1/health`, `/v1/catalog`, `/v1/trending/preview`, `/v1/stats`.
Machine-readable prices and schemas live at `/v1/catalog`; an agent quickstart lives at
`/llms.txt`.

**Data attribution:** {ATTRIBUTION}. Player statistics come from
[nflverse](https://github.com/nflverse) under
[CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/); market signal, league and
roster data come from the public read-only [Sleeper API](https://docs.sleeper.com/).

**Every paid response** carries `verdict`, `confidence`, `reasoning`, `stats_cited[]`,
`sources[]` and `meta` (generation time, per-dataset freshness, cache state). Trust is
the product: if a number is claimed, it is cited.
""".strip()

OPENAPI_TAGS = [
    {
        "name": "free",
        "description": (
            "No payment required. Health, the endpoint catalog, and the trending teaser."
        ),
    },
    {
        "name": "paid",
        "description": (
            "x402-gated. Returns 402 with payment requirements until a valid payment is "
            "presented; settles only after a successful response."
        ),
    },
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Construct the store (and engine) on startup, release them on shutdown."""
    settings = get_settings()
    store = get_store(settings)
    logger.info(
        "playclock starting: env=%s store=%s engine=%s x402=%s/%s",
        settings.env,
        settings.store_backend,
        settings.engine,
        settings.x402_mode,
        settings.x402_network,
    )
    try:
        yield
    finally:
        try:
            await get_engine(settings, store).aclose()
        finally:
            try:
                await close_facilitator()
            finally:
                await store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application.

    Args:
        settings: Settings to describe the app with. Defaults to the process
            settings. Note that request handlers always read the *live*
            settings, so this only affects startup-time metadata.

    Returns:
        A fully wired :class:`fastapi.FastAPI` instance.
    """
    settings = settings or get_settings()
    app = FastAPI(
        title=API_TITLE,
        description=API_DESCRIPTION,
        version=API_VERSION,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        contact={"name": "Play Clock", "url": "https://github.com/"},
        license_info={"name": "MIT"},
    )

    # Any origin: the web UI, third-party agents and Bazaar clients all call this
    # from elsewhere, and there is nothing to protect with an origin check —
    # authorization is the payment, not a cookie. Credentials stay off, which is
    # also what makes the wildcard origin legal.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=CORS_EXPOSE_HEADERS,
    )

    # Read off the app rather than hard-coded, so turning a docs page off (or
    # moving it) cannot leave a stale path behind here.
    noindex_paths = frozenset(
        path for path in (app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url) if path
    )

    @app.middleware("http")
    async def noindex_generated_docs(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Keep FastAPI's Swagger and ReDoc pages out of the index.

        Both are HTML, both are byte-identical under the mapped domain and the
        `*.run.app` host, and the web UI footer links to `/docs` from every
        indexed page — so a crawler reaches them and reports the pair as a
        duplicate with no canonical. They are JS shells with nothing to rank
        anyway; the documents worth finding here are `/llms.txt`, `/v1/catalog`
        and `/openapi.json`, which stay crawlable.

        `noindex` as a header rather than a robots.txt `Disallow`: a disallowed
        URL is never fetched, so a directive inside it is never read, and Google
        may still index the URL on the strength of inbound links alone.
        """
        response = await call_next(request)
        if request.url.path in noindex_paths:
            response.headers["X-Robots-Tag"] = "noindex"
        return response

    install_x402_handlers(app)
    app.include_router(free.router)
    app.include_router(paid.router)

    @app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
    async def llms_txt(request: Request) -> str:
        """Agent-readable quickstart: what this is, how to pay, what it costs."""
        return render_llms_txt(get_settings(), base_url=_public_base_url(request))

    @app.get("/", include_in_schema=False)
    async def root(request: Request) -> Response:
        """Identify this origin — as a page for a crawler, as JSON for a client.

        The page half is not decoration. The GoPlausible leaderboard takes a
        merchant's **name from whatever the resource origin serves at `/`** —
        its `<title>` or `og:title` — and falls back to a truncated payTo
        address when it finds nothing. Ranked entrants that 404 here appear as
        `SGLTUP…SPPI`. Since the Bazaar catalogues the origin of the first
        settled payment permanently, this has to be right before that payment,
        not after it.

        Content negotiation keeps the old contract: anything asking for JSON
        still gets the three pointers it always did.
        """
        accept = request.headers.get("accept", "")
        wants_json = "application/json" in accept and "text/html" not in accept
        if wants_json:
            return JSONResponse(
                {
                    "service": settings.app_name,
                    "catalog": "/v1/catalog",
                    "openapi": "/openapi.json",
                    "agents": "/llms.txt",
                }
            )
        return HTMLResponse(
            render_landing_page(
                settings,
                _public_base_url(request),
                canonical_url=_configured_base_url(),
            )
        )

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def apple_touch_icon() -> Response:
        """The icon the leaderboard probes for at this exact conventional path.

        Verified against every ranked entrant: a logo appears only where
        `/apple-touch-icon.png` resolves. Declaring one at another path in the
        HTML is not enough — Scrape402 points at `/public/logo.png` and shows
        no logo.
        """
        return FileResponse(
            _STATIC_DIR / "apple-touch-icon.png",
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    return app


def _configured_base_url() -> str:
    """Return the origin an operator declared, or ``""`` when none is set.

    Separate from :func:`_public_base_url` because the two callers want opposite
    things from a missing value. ``/llms.txt`` needs *some* reachable URL, so
    falling back to the request host is right there. A canonical link needs the
    one URL we want indexed, and the request host is by definition whichever
    hostname the crawler happened to use — so falling back to it would emit a
    self-referencing canonical under every hostname and consolidate nothing.
    """
    return os.environ.get(RESOURCE_BASE_URL_ENV, "").strip().rstrip("/")


def _public_base_url(request: Request) -> str:
    """Return the origin to advertise in ``/llms.txt``.

    ``X402_RESOURCE_BASE_URL`` wins when set — behind a custom domain the request
    URL is the internal Cloud Run host, and an agent that copies it would call
    the wrong place. Read per request (not at import) so the same image works
    behind a new domain without a code change.
    """
    return _configured_base_url() or str(request.base_url).rstrip("/")


#: Files served directly by the API. Only two, and both exist because the
#: leaderboard reads them: the icon below, and nothing else so far.
_STATIC_DIR = Path(__file__).parent / "static"


def render_landing_page(settings: Settings, base_url: str = "", canonical_url: str = "") -> str:
    """The HTML this origin serves at ``/``.

    Deliberately one hand-written string rather than a template engine or a
    second static service: it must exist in the *API* image, because the origin
    the Bazaar catalogues is the one that served the 402, and that is this one.

    The `<title>` is the merchant name that appears on the challenge
    leaderboard. Keep it short — a name, not a sentence — because that is what
    a ranked row has room for.

    `canonical_url` is deliberately not `base_url`: Cloud Run answers on the
    mapped domain *and* on `*.run.app`, so a canonical derived from the request
    would name whichever host the crawler used and consolidate nothing. Only an
    operator-declared origin can name the one URL we want indexed, so the tag is
    omitted when there is none rather than guessed — a wrong canonical points
    ranking at a URL we do not control, which is worse than no canonical.
    """
    # The base falls back to the request's Host header, which the client
    # chooses: escape everything interpolated into markup.
    name = html.escape(settings.app_name)
    base = html.escape(base_url.rstrip("/"))
    canonical = html.escape(canonical_url.rstrip("/"))
    summary = (
        "Pay-per-answer NFL fantasy football analysis. One question, one "
        "USDC micropayment on Algorand, no account and no subscription."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<meta name="description" content="{summary}">
<meta property="og:title" content="{name}">
<meta property="og:description" content="{summary}">
<meta property="og:type" content="website">
{f'<meta property="og:url" content="{canonical}/">' if canonical else ""}
{f'<link rel="canonical" href="{canonical}/">' if canonical else ""}
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<link rel="icon" href="/apple-touch-icon.png">
<style>
  :root {{ color-scheme: dark light; }}
  body {{ margin:0; padding:3rem 1.25rem; background:#0b0f0c; color:#e8f5ec;
         font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif; }}
  main {{ max-width:44rem; margin:0 auto; }}
  h1 {{ font-size:2rem; margin:0 0 .25rem; letter-spacing:-.02em; }}
  p.lede {{ color:#9fbfab; margin:0 0 2rem; font-size:1.05rem; }}
  h2 {{ font-size:.8rem; text-transform:uppercase; letter-spacing:.08em;
        color:#7da88f; margin:2rem 0 .6rem; }}
  a {{ color:#4ade80; }}
  ul {{ padding-left:1.1rem; }} li {{ margin:.3rem 0; }}
  code {{ background:#152019; padding:.15em .4em; border-radius:4px; font-size:.9em; }}
  footer {{ margin-top:2.5rem; color:#6b8a77; font-size:.85rem; }}
</style>
</head>
<body>
<main>
  <h1>{name}</h1>
  <p class="lede">{summary}</p>

  <h2>For agents</h2>
  <ul>
    <li><a href="/llms.txt">/llms.txt</a> — what this is and how to pay, in prose</li>
    <li><a href="/v1/catalog">/v1/catalog</a> — every endpoint with its price and schema</li>
    <li><a href="/openapi.json">/openapi.json</a> — the full request and response contracts</li>
  </ul>

  <h2>For people</h2>
  <ul>
    <li><a href="/v1/trending/preview">/v1/trending/preview</a> — a free look at the board</li>
    <li><a href="/v1/health">/v1/health</a> — status and how fresh the data is</li>
  </ul>

  <h2>How paying works</h2>
  <p>
    Every paid route answers <code>402</code> with what it costs and who to pay.
    Send the signed payment back on the same request and the answer comes with a
    settlement receipt. Payment runs over
    <a href="https://x402.org">x402</a> on Algorand, settled through the
    GoPlausible facilitator.
  </p>

  <footer>
    Data: nflverse (CC-BY 4.0) and Sleeper.{f" Serving from {base}." if base else ""}
  </footer>
</main>
</body>
</html>
"""


def render_llms_txt(settings: Settings, base_url: str = "") -> str:
    """Render the ``/llms.txt`` agent guide from the live specs and prices.

    Generated per request rather than checked in, so a price experiment or a new
    endpoint shows up without anyone remembering to edit a text file.
    """
    origin = base_url.rstrip("/")
    lines = [
        f"# {settings.app_name}",
        "",
        "> Pay-per-analysis NFL fantasy football intelligence. One question, one",
        "> micropayment, one structured answer. No account, no subscription: the",
        "> wallet is the identity.",
        "",
        "## For agents",
        "",
        "Every paid endpoint speaks x402 (protocol version 2) over USDC on Algorand"
        f" {settings.x402_network}.",
        "",
        "1. Call the endpoint with no payment header. The response is 402 and its body",
        "   carries `accepts[]` (scheme, network, asset, amount in atomic units, payTo)",
        "   plus a Bazaar `extensions` block describing the request and response shapes.",
        f"2. Retry with the signed payment payload in the `{PAYMENT_SIGNATURE_HEADER}`",
        "   header. (`X-PAYMENT` is accepted for older clients.)",
        f"3. The answer comes back 200 with a settlement receipt in `{PAYMENT_RESPONSE_HEADER}`.",
        "",
        "Payment is verified before the analysis runs and settled only after it succeeds,",
        "so a failed call is never charged. Retries of the same payment inside 300 seconds",
        "are idempotent and re-serve the original receipt.",
        "",
        "## Paid endpoints",
        "",
    ]

    for key in ENDPOINT_KEYS:
        spec = ENDPOINT_SPECS[key]
        ttl = CACHE_TTL_SECONDS.get(key)
        freshness = f"cached {ttl // 3600}h" if ttl else "always fresh"
        lines.append(
            f"- `{spec.method} {spec.path}` — {settings.price_for(key):.2f} USDC "
            f"({freshness}): {spec.description}"
        )

    lines += [
        "",
        "## Free endpoints",
        "",
        "- `GET /v1/health` — status, active NFL week, per-dataset ingest freshness.",
        "- `GET /v1/catalog` — machine-readable prices, schemas, cache TTLs, payment config.",
        "- `GET /v1/trending/preview` — top five trending players, names and counts only.",
        "- `GET /v1/stats` — settled paid calls and the backtested hit rate of past verdicts.",
        "",
        "## Machine-readable",
        "",
        f"- Catalog: {origin}/v1/catalog",
        f"- OpenAPI: {origin}/openapi.json",
        f"- Payment network: Algorand {settings.x402_network}"
        f" (challenge tag `{settings.x402_challenge_tag}`)",
        "",
        "## Response contract",
        "",
        "Every paid body carries `verdict`, `confidence` (high|medium|low), `reasoning`,",
        "`stats_cited[]` (each with the dataset it came from), `sources[]`, and `meta`",
        "(`generated_at`, `data_freshness`, `model`, `cache`). Numbers are cited, never",
        "recalled — check `stats_cited` before trusting a claim.",
        "",
        "## Attribution",
        "",
        ATTRIBUTION + ". nflverse data is CC-BY 4.0; please keep the attribution.",
        "",
    ]
    return "\n".join(lines)


#: Module-level app for ``uvicorn api.main:app``.
app = create_app()
