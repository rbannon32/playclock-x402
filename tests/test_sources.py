"""Resolving grounding redirects into citations a reader can check.

Everything runs against a ``respx`` mock transport: the redirect hop, the
article page, the timeouts and the non-HTML answers are all scripted. The
rule under test is "resolved or unchanged, never lost" — a citation must
survive every failure mode with at least what it started with.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import httpx
import pytest
import respx

from api.data import sources as sources_module
from api.data.sources import (
    MAX_BYTES,
    UnsafeSourceURL,
    extract_title,
    needs_resolution,
    resolve_public_addresses,
    resolve_source,
    resolve_sources,
    slug_title,
)


async def failing_resolver(host: str, port: int) -> list[str]:
    """Refuse every host: these tests only care how the client is built."""
    raise UnsafeSourceURL("not resolved in this test")


def _unreachable(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"no request should have been made: {request.url}")


REDIRECT = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQF"
ARTICLE = "https://www.nfl.com/news/lions-place-pacheco-on-ir"
#: A final URL with nothing in its path worth turning into a headline.
ARTICLE_NO_SLUG = "https://www.nfl.com/news/9081726"
PAGE = (
    "<html><head><title>Lions place RB Isiah Pacheco on IR | NFL.com</title>"
    '<meta property="og:title" content="Lions place RB Isiah Pacheco on IR">'
    "</head><body>...</body></html>"
)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=1.0)


async def public_resolver(host: str, port: int) -> list[str]:
    """Use a stable public address so resolver tests need no network."""
    return ["8.8.8.8"]


# -- what needs resolving --------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ({"title": "nfl.com", "url": REDIRECT}, True),
        ({"title": "A real headline", "url": REDIRECT}, True),
        ({"title": "nfl.com", "url": ARTICLE}, True),
        ({"title": "A real headline", "url": ARTICLE}, False),
        ({"title": "nfl.com", "url": None}, False),
    ],
)
def test_needs_resolution(source: dict, expected: bool) -> None:
    assert needs_resolution(source) is expected


# -- title extraction --------------------------------------------------------


def test_og_title_is_preferred_over_title_tag() -> None:
    assert extract_title(PAGE) == "Lions place RB Isiah Pacheco on IR"


def test_title_tag_is_the_fallback_and_is_unescaped() -> None:
    page = "<title>  Jags &amp; Titans:\n  week one </title>"
    assert extract_title(page) == "Jags & Titans: week one"


def test_bot_interstitials_are_not_titles() -> None:
    assert extract_title("<title>Just a moment...</title>") is None
    assert extract_title("<html><body>no title</body></html>") is None


# -- the slug fallback -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (ARTICLE, "Lions place pacheco on ir"),
        (
            "https://www.footballnationusa.com/post/2026-nfl-injury-report-week-1",
            "2026 nfl injury report week 1",
        ),
        ("https://site.example/blog/lions_place_pacheco_on_ir.html", "Lions place pacheco on ir"),
        ("https://site.example/blog/lions-place-pacheco-on-ir/", "Lions place pacheco on ir"),
        ("https://site.example/blog/2026-09-03-1234567", None),  # numeric only
        (ARTICLE_NO_SLUG, None),  # too short, no separator
        ("https://site.example/", None),  # no path at all
        ("https://site.example/news/shortslug-x", None),  # under the length floor
    ],
)
def test_slug_title(url: str, expected: str | None) -> None:
    assert slug_title(url) == expected


def test_slug_title_is_capped() -> None:
    long = "https://site.example/post/" + "-".join(["word"] * 60)
    title = slug_title(long)
    assert title is not None and len(title) == 120


# -- resolution -----------------------------------------------------------------


@respx.mock
async def test_a_redirect_resolves_to_the_article_and_its_headline(
    client: httpx.AsyncClient,
) -> None:
    respx.get(REDIRECT).mock(return_value=httpx.Response(302, headers={"location": ARTICLE}))
    respx.get(ARTICLE).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": REDIRECT, "published": "x"},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved == {
        "title": "Lions place RB Isiah Pacheco on IR",
        "url": ARTICLE,
        "published": "x",
    }


@respx.mock
async def test_a_page_with_no_title_still_keeps_the_final_url(
    client: httpx.AsyncClient,
) -> None:
    """With no headline and no slug to make one from, the domain stays."""
    respx.get(REDIRECT).mock(
        return_value=httpx.Response(302, headers={"location": ARTICLE_NO_SLUG})
    )
    respx.get(ARTICLE_NO_SLUG).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>")
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": REDIRECT},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved["url"] == ARTICLE_NO_SLUG
    assert resolved["title"] == "nfl.com"


@respx.mock
async def test_a_page_with_no_title_falls_back_to_the_slug(client: httpx.AsyncClient) -> None:
    """The first gated warm's footballnationusa.com case: the URL said it all."""
    respx.get(REDIRECT).mock(return_value=httpx.Response(302, headers={"location": ARTICLE}))
    respx.get(ARTICLE).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>")
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": REDIRECT},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved == {"url": ARTICLE, "title": "Lions place pacheco on ir"}


@respx.mock
async def test_a_real_title_is_never_replaced_by_the_slug(client: httpx.AsyncClient) -> None:
    respx.get(REDIRECT).mock(return_value=httpx.Response(302, headers={"location": ARTICLE}))
    respx.get(ARTICLE).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text="<p>hi</p>")
    )
    resolved = await resolve_source(
        {"title": "A real headline", "url": REDIRECT},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved["title"] == "A real headline"


@respx.mock
async def test_a_non_html_answer_leaves_the_title_alone(client: httpx.AsyncClient) -> None:
    respx.get(REDIRECT).mock(
        return_value=httpx.Response(302, headers={"location": ARTICLE_NO_SLUG})
    )
    respx.get(ARTICLE_NO_SLUG).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF"
        )
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": REDIRECT},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved == {"title": "nfl.com", "url": ARTICLE_NO_SLUG}


@respx.mock
async def test_a_timeout_returns_the_source_unchanged(client: httpx.AsyncClient) -> None:
    respx.get(REDIRECT).mock(side_effect=httpx.ReadTimeout("slow"))
    original = {"title": "nfl.com", "url": REDIRECT}
    assert (
        await resolve_source(original, client, resolver=public_resolver, pin_connections=False)
        == original
    )


@respx.mock
async def test_an_error_page_returns_the_source_unchanged_but_for_the_url(
    client: httpx.AsyncClient,
) -> None:
    respx.get(REDIRECT).mock(
        return_value=httpx.Response(302, headers={"location": ARTICLE_NO_SLUG})
    )
    respx.get(ARTICLE_NO_SLUG).mock(
        return_value=httpx.Response(
            403, headers={"content-type": "text/html"}, text="<title>Access Denied</title>"
        )
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": REDIRECT},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved["title"] == "nfl.com"
    assert resolved["url"] == ARTICLE_NO_SLUG


@respx.mock
async def test_only_the_first_max_bytes_are_read(client: httpx.AsyncClient) -> None:
    """A page whose title sits past the cap is treated as having none."""
    padding = "x" * (MAX_BYTES + 10)
    respx.get(ARTICLE_NO_SLUG).mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=f"<html>{padding}<title>Late title</title></html>",
        )
    )
    resolved = await resolve_source(
        {"title": "nfl.com", "url": ARTICLE_NO_SLUG},
        client,
        resolver=public_resolver,
        pin_connections=False,
    )
    assert resolved["title"] == "nfl.com"


@respx.mock
async def test_resolve_sources_preserves_order_and_count(client: httpx.AsyncClient) -> None:
    respx.get(REDIRECT).mock(return_value=httpx.Response(302, headers={"location": ARTICLE}))
    respx.get(ARTICLE).mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, text=PAGE)
    )
    sources = [
        {"title": "A real headline", "url": "https://example.com/a"},
        {"title": "nfl.com", "url": REDIRECT},
        {"title": "no url", "url": None},
    ]
    resolved = await resolve_sources(
        sources, client=client, resolver=public_resolver, pin_connections=False
    )
    assert len(resolved) == 3
    assert resolved[0] == sources[0]
    assert resolved[1]["title"] == "Lions place RB Isiah Pacheco on IR"
    assert resolved[2] == sources[2]


# -- fetch safety --------------------------------------------------------------


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["::1"],
        ["169.254.169.254"],
        ["10.0.0.5"],
        ["8.8.8.8", "127.0.0.1"],
    ],
)
async def test_private_or_rebound_destination_is_never_requested(addresses: list[str]) -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200)

    async def resolver(host: str, port: int) -> list[str]:
        return addresses

    original = {"title": "nfl.com", "url": "https://internal.example/article"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        resolved = await resolve_source(
            original, mock_client, resolver=resolver, pin_connections=False
        )
    assert resolved == original
    assert seen == []


async def test_pinned_https_request_keeps_the_original_host_for_sni() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["host"] = request.headers["host"]
        captured["sni_hostname"] = request.extensions["sni_hostname"]
        return httpx.Response(200, headers={"content-type": "application/pdf"})

    original = {"title": "nfl.com", "url": "HTTPS://Example.com/article"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        resolved = await resolve_source(original, mock_client, resolver=public_resolver)

    assert resolved == original
    assert captured == {
        "url": "https://8.8.8.8/article",
        "host": "example.com",
        "sni_hostname": "example.com",
    }


async def test_unsafe_redirect_is_never_followed() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == REDIRECT:
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        raise AssertionError(f"unexpected request: {request.url}")

    async def resolver(host: str, port: int) -> list[str]:
        return ["169.254.169.254"] if host == "169.254.169.254" else ["8.8.8.8"]

    original = {"title": "nfl.com", "url": REDIRECT}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        resolved = await resolve_source(
            original, mock_client, resolver=resolver, pin_connections=False
        )
    assert resolved == original
    assert seen == [REDIRECT]


async def test_excessive_redirects_leave_the_source_unchanged() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "/next"})

    original = {"title": "nfl.com", "url": "https://example.com/start"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as mock_client:
        resolved = await resolve_source(
            original, mock_client, resolver=public_resolver, pin_connections=False
        )
    assert resolved == original
    assert len(seen) == 4


async def test_resolve_sources_with_nothing_to_do_makes_no_requests() -> None:
    assert await resolve_sources([]) == []


async def test_a_pinned_client_never_keeps_a_connection_alive() -> None:
    """Two hosts behind one CDN address must not share a TLS connection.

    Pinning puts the vetted address in the request URL, which is what httpx
    keys its pool on — so without this, a keep-alive connection opened with
    ``a.example``'s SNI would be reused for ``b.example``, whose certificate
    was never checked against that name.
    """
    captured: list[httpx.Limits] = []
    real_client = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured.append(kwargs["limits"])  # type: ignore[arg-type]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(sources_module, "httpx", wraps=httpx) as patched:
        patched.AsyncClient = spy
        patched.Limits = httpx.Limits
        await resolve_sources([{"title": "nfl.com", "url": ARTICLE}], resolver=failing_resolver)

    assert captured and captured[0].max_keepalive_connections == 0


async def test_an_unpinned_client_keeps_the_default_pool() -> None:
    captured: list[httpx.Limits] = []
    real_client = httpx.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured.append(kwargs["limits"])  # type: ignore[arg-type]
        return real_client(*args, **kwargs)  # type: ignore[arg-type]

    with mock.patch.object(sources_module, "httpx", wraps=httpx) as patched:
        patched.AsyncClient = spy
        patched.Limits = httpx.Limits
        await resolve_sources(
            [{"title": "nfl.com", "url": ARTICLE}],
            resolver=failing_resolver,
            pin_connections=False,
        )

    assert (
        captured
        and captured[0].max_keepalive_connections == httpx.Limits().max_keepalive_connections
    )


async def test_a_stalled_dns_lookup_is_bounded_and_leaves_the_source_unchanged() -> None:
    """``getaddrinfo`` runs before any request, so httpx's timeout misses it."""

    async def never_resolves(host: str, port: int) -> list[str]:
        await asyncio.sleep(60)
        return ["8.8.8.8"]

    original = {"title": "nfl.com", "url": REDIRECT}
    with mock.patch.object(sources_module, "DNS_TIMEOUT_SECONDS", 0.01):
        async with httpx.AsyncClient(transport=httpx.MockTransport(_unreachable)) as mock_client:
            resolved = await resolve_source(original, mock_client, resolver=never_resolves)

    # Unchanged, and _unreachable proves the stalled lookup never reached a
    # request: the bound is on resolution itself, not on the fetch that follows.
    assert resolved == original


async def test_resolve_public_addresses_times_out_rather_than_hanging() -> None:
    async def slow_getaddrinfo(*args: object, **kwargs: object) -> list:
        await asyncio.sleep(60)
        return []

    loop = asyncio.get_running_loop()
    with (
        mock.patch.object(sources_module, "DNS_TIMEOUT_SECONDS", 0.01),
        mock.patch.object(loop, "getaddrinfo", slow_getaddrinfo),
    ):
        with pytest.raises(UnsafeSourceURL, match="timed out"):
            await resolve_public_addresses("example.com", 443)
