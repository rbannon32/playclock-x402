"""Application wiring: OpenAPI metadata, CORS, lifespan and ``/llms.txt``.

``/llms.txt`` and ``/v1/catalog`` are the two documents an agent reads before it
decides to pay, and both are generated from the same specs and price table — so
the tests here mostly check that they cannot disagree with each other or with
what the 402 will actually charge.
"""

from __future__ import annotations

import pytest

from api.core.config import ENDPOINT_KEYS, get_settings
from api.core.store import Store
from api.evals.golden import seed_store
from api.main import create_app, lifespan, render_llms_txt
from api.routes import API_VERSION
from api.x402 import ENDPOINT_SPECS
from api.x402.schemas_compat import PAYMENT_RESPONSE_HEADER
from tests.test_routes_free import api_client, configure


@pytest.fixture
async def app_store(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch)
    await seed_store(store)
    return store


async def test_openapi_document_describes_the_product(app_store: Store) -> None:
    async with api_client() as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    doc = response.json()
    assert doc["info"]["version"] == API_VERSION
    assert "x402" in doc["info"]["description"]
    assert "CC-BY 4.0" in doc["info"]["description"], "nflverse attribution is mandatory"

    paths = doc["paths"]
    for spec in ENDPOINT_SPECS.values():
        assert spec.path in paths, spec.path
        assert spec.method.lower() in paths[spec.path]
    for path in ("/v1/health", "/v1/catalog", "/v1/trending/preview"):
        assert path in paths

    schemas = doc["components"]["schemas"]
    for name in ("TrendingResponse", "TeamReportResponse", "RosterRequest", "Catalog"):
        assert name in schemas

    tags = {tag["name"] for tag in doc["tags"]}
    assert tags == {"free", "paid"}


async def test_cors_allows_any_origin_and_exposes_payment_headers(app_store: Store) -> None:
    """Browser wallets have to *read* the receipt header, not just receive it."""
    async with api_client() as client:
        preflight = await client.options(
            "/v1/trending",
            headers={
                "origin": "https://playclock.example",
                "access-control-request-method": "GET",
            },
        )
        simple = await client.get("/v1/health", headers={"origin": "https://playclock.example"})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "*"
    exposed = simple.headers["access-control-expose-headers"]
    assert PAYMENT_RESPONSE_HEADER in exposed
    assert "PAYMENT-REQUIRED" in exposed


async def test_llms_txt_lists_every_endpoint_with_its_price(app_store: Store) -> None:
    async with api_client() as client:
        response = await client.get("/llms.txt")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    settings = get_settings()

    for key in ENDPOINT_KEYS:
        spec = ENDPOINT_SPECS[key]
        assert f"{spec.method} {spec.path}" in text, key
        assert f"{settings.price_for(key):.2f} USDC" in text, key

    assert "PAYMENT-SIGNATURE" in text
    assert "402" in text
    assert "/v1/catalog" in text
    assert "/v1/trending/preview" in text
    assert "nflverse" in text
    # Cache policy is advertised so an agent knows when a cheap re-ask is pointless.
    assert "cached 12h" in text and "always fresh" in text


async def test_llms_txt_prefers_the_public_base_url(
    app_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behind a custom domain, an agent must not copy the internal Cloud Run host."""
    monkeypatch.setenv("X402_RESOURCE_BASE_URL", "https://api.playclock.example/")
    async with api_client() as client:
        text = (await client.get("/llms.txt")).text

    assert "https://api.playclock.example/v1/catalog" in text
    assert "testserver" not in text


async def test_price_changes_reach_both_discovery_documents(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """October price experiments are env-only: no code, no drift between docs."""
    configure(monkeypatch, PRICE_TRENDING="0.05")
    await seed_store(store)

    async with api_client() as client:
        catalog = (await client.get("/v1/catalog")).json()
        text = (await client.get("/llms.txt")).text
        quote = await client.get("/v1/trending")

    entry = next(e for e in catalog["endpoints"] if e["key"] == "trending")
    assert entry["price_usdc"] == 0.05
    assert "0.05 USDC" in text
    # ...and the same number is what an unpaid caller is actually quoted.
    configure(monkeypatch, X402_MODE="mock", PRICE_TRENDING="0.05")
    async with api_client() as client:
        quote = await client.get("/v1/trending")
    assert quote.status_code == 402
    assert quote.json()["accepts"][0]["amount"] == "50000"


async def test_root_identifies_this_origin_to_a_crawler(app_store: Store) -> None:
    """HTML by default, because that is what reads the merchant name.

    The GoPlausible leaderboard takes a merchant's name from whatever the
    resource origin serves at `/` and falls back to a truncated payTo address
    when it finds nothing. Ranked entrants that 404 here show as `SGLTUP…SPPI`.
    """
    async with api_client() as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Play Clock</title>" in response.text
    assert 'property="og:title"' in response.text


def test_the_landing_page_escapes_a_hostile_host() -> None:
    """Without a configured origin the base is the request's Host header."""
    from api.core.config import Settings
    from api.main import render_landing_page

    page = render_landing_page(Settings(), base_url='http://x"><script>alert(1)</script>')

    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


async def test_root_canonicalises_to_the_public_origin(
    app_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloud Run answers on the mapped domain and on *.run.app with one image.

    Without a canonical a crawler indexes the same page under both hosts and
    picks the winner itself — which is the Search Console report that prompted
    this. The tag names the configured origin, never the request host.
    """
    monkeypatch.setenv("X402_RESOURCE_BASE_URL", "https://api.playclock.example/")
    async with api_client() as client:
        text = (await client.get("/")).text

    assert '<link rel="canonical" href="https://api.playclock.example/">' in text
    assert "testserver" not in text


async def test_root_omits_the_canonical_when_no_origin_is_configured(
    app_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No declared origin means no way to name the URL we want indexed.

    The request host is not a substitute: it is whichever hostname the crawler
    used, so falling back to it emits a self-referencing canonical under every
    hostname and consolidates nothing — the exact duplicate this tag exists to
    resolve. Better to say nothing and let Google choose than to point ranking
    at a URL we do not control.
    """
    monkeypatch.delenv("X402_RESOURCE_BASE_URL", raising=False)
    async with api_client() as client:
        text = (await client.get("/")).text

    assert "canonical" not in text
    assert 'property="og:url"' not in text


async def test_generated_docs_pages_are_not_indexable(app_store: Store) -> None:
    """Swagger and ReDoc are the same bytes under both of this service's hosts.

    The web UI footer links to `/docs` from every indexed page, so a crawler
    gets there. They are JS shells with nothing to rank; the documents worth
    finding stay crawlable.
    """
    async with api_client() as client:
        for path in ("/docs", "/redoc"):
            response = await client.get(path)
            assert response.status_code == 200, path
            assert response.headers["x-robots-tag"] == "noindex", path

        for path in ("/llms.txt", "/openapi.json", "/"):
            response = await client.get(path)
            assert "x-robots-tag" not in response.headers, path


async def test_root_still_points_at_the_machine_readable_documents(app_store: Store) -> None:
    """The old JSON contract survives, for anything that asks for it."""
    async with api_client() as client:
        body = (await client.get("/", headers={"accept": "application/json"})).json()

    assert body["catalog"] == "/v1/catalog"
    assert body["openapi"] == "/openapi.json"
    assert body["agents"] == "/llms.txt"


async def test_the_leaderboard_can_find_an_icon(app_store: Store) -> None:
    """A logo appears only where /apple-touch-icon.png resolves at that path."""
    async with api_client() as client:
        response = await client.get("/apple-touch-icon.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_lifespan_opens_and_closes_the_store(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup builds the store; shutdown releases it and the engine."""
    configure(monkeypatch)
    closed: list[str] = []
    monkeypatch.setattr(type(store), "close", lambda self: _record(closed), raising=False)

    app = create_app()
    async with lifespan(app):
        async with api_client() as client:
            assert (await client.get("/v1/health")).status_code == 200
    assert closed == ["closed"]


async def _record(sink: list[str]) -> None:
    sink.append("closed")


def test_render_llms_txt_is_pure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The document is generated, never checked in — no I/O, no request needed."""
    configure(monkeypatch)
    text = render_llms_txt(get_settings(), base_url="https://example.test/")
    assert text.startswith("# Play Clock")
    assert "https://example.test/openapi.json" in text
