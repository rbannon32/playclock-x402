"""Resolve research citations into something a reader can actually check.

The research agent's grounding tool returns its sources as
``vertexaisearch.cloud.google.com/grounding-api-redirect/<300 opaque chars>``
with the bare domain as the title. Served to a payer that reads as
``"footballnationusa.com"`` over a link that says nothing — a citation in
name only. Observed on every warmed board on 2026-09-03.

This module follows each redirect once and swaps in the final URL and the
page's real headline. It runs inside :class:`~api.agents.pipeline.AdkAnalysisEngine`
after synthesis — a body a reader can check is the engine's output contract,
wherever that body is served — and again in :mod:`ingest.precompute`, where
it is a no-op on an already-resolved list and a safety net otherwise. It
lives under ``api/`` because the api image does not ship ``ingest/``. When the page
yields no headline but the final URL carries a slug (``/post/lions-place-
pacheco-on-ir``), the slug is humanised into one — the first gated warm left
``footballnationusa.com`` as a title over a perfectly descriptive URL. Anything
that cannot be resolved at all is left exactly as it was: a citation the
reader has to squint at is still better than a citation that vanished.

Cost and safety: at most :data:`MAX_BYTES` of each page is read, requests run
under a small concurrency cap, and every failure mode (timeout, non-HTML,
connection refused, a page with no title) degrades to "unchanged". Nothing
here can fail a warm.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

#: The marker that identifies an unresolved grounding source.
REDIRECT_MARKER = "grounding-api-redirect"

#: How much of a page to read looking for its title.
MAX_BYTES = 65_536

#: Per-request timeout, seconds. Precompute is not latency-sensitive, but a
#: hung publisher must not hold a warm for minutes.
TIMEOUT_SECONDS = 8.0

#: Per-lookup DNS budget, seconds. Counted inside :data:`TIMEOUT_SECONDS` so a
#: source cannot cost more than the per-request timeout in total.
DNS_TIMEOUT_SECONDS = 4.0

#: How many pages to fetch at once.
CONCURRENCY = 4

MAX_REDIRECTS = 3

#: Pool limits used when connections are address-pinned: no connection is kept
#: alive, so one host's verified TLS session can never serve another's request.
_PINNED_LIMITS = httpx.Limits(max_keepalive_connections=0)


class UnsafeSourceURL(ValueError):
    """A citation URL whose destination is not safe to fetch."""


Resolver = Callable[[str, int], Awaitable[list[str]]]


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Return every address currently resolved for the host.

    Bounded by :data:`DNS_TIMEOUT_SECONDS`. ``getaddrinfo`` runs before any
    request exists, so httpx's own timeout does not cover it; ``precompute``
    awaits the resolver directly, where an unbounded lookup would hold a warm
    past the documented per-source budget.
    """
    loop = asyncio.get_running_loop()
    try:
        async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
            records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except TimeoutError as exc:
        raise UnsafeSourceURL("host resolution timed out") from exc
    except OSError as exc:
        raise UnsafeSourceURL("could not resolve host") from exc
    return sorted({record[4][0] for record in records})


def parse_fetch_url(url: str) -> tuple[str, int]:
    """Validate URL syntax and return its normalized hostname and port."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeSourceURL("invalid URL") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeSourceURL("unsupported URL scheme")
    if not parsed.hostname or parsed.username is not None or parsed.password is not None:
        raise UnsafeSourceURL("invalid URL authority")
    return parsed.hostname, port or (443 if parsed.scheme.lower() == "https" else 80)


async def ensure_safe_fetch_url(url: str, resolver: Resolver) -> list[str]:
    """Resolve URL and reject a non-HTTP(S), private or mixed destination.

    Resolution is bounded by :data:`DNS_TIMEOUT_SECONDS` here rather than only
    inside the default resolver: the lookup happens before any request exists,
    so httpx's own timeout does not cover it, and ``precompute`` awaits the
    resolver directly, where an unbounded lookup would hold a warm open past
    the documented per-source budget.
    """
    host, port = parse_fetch_url(url)
    try:
        async with asyncio.timeout(DNS_TIMEOUT_SECONDS):
            addresses = await resolver(host, port)
    except TimeoutError as exc:
        raise UnsafeSourceURL("host resolution timed out") from exc
    if not addresses:
        raise UnsafeSourceURL("host has no addresses")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise UnsafeSourceURL("host resolves to a non-public address")
    except ValueError as exc:
        raise UnsafeSourceURL("host resolved to an invalid address") from exc
    return addresses


def pinned_request_url(url: str, address: str) -> str:
    """Replace a URL hostname with a vetted address while preserving its port."""
    parsed = urlsplit(url)
    host = f"[{address}]" if ":" in address else address
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def host_header(url: str) -> str:
    """Return the original HTTP Host value, including a non-default port."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return host if parsed.port is None else f"{host}:{parsed.port}"


_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_RE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_BARE_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

#: Sites that answer a bot with an interstitial rather than the article. A
#: title from one of these is worse than the domain.
_INTERSTITIAL_TITLES = ("just a moment", "access denied", "attention required", "403 forbidden")

#: A path segment shorter than this is an id or a section, not a headline.
_SLUG_MIN_CHARS = 12
_SLUG_MAX_TITLE = 120
_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_SLUG_SEPARATOR_RE = re.compile(r"[-_]+")


def needs_resolution(source: dict[str, Any]) -> bool:
    """Whether a source is an opaque redirect or carries a bare-domain title."""
    url = str(source.get("url") or "")
    title = str(source.get("title") or "").strip()
    if not url:
        return False
    return REDIRECT_MARKER in url or bool(_BARE_DOMAIN_RE.match(title))


def extract_title(page: str) -> str | None:
    """Pull a headline out of an HTML page, preferring ``og:title``."""
    for pattern in (_OG_TITLE_RE, _OG_TITLE_RE_REV, _TITLE_RE):
        match = pattern.search(page)
        if match:
            title = _WS_RE.sub(" ", html.unescape(match.group(1))).strip()
            if title and not any(marker in title.lower() for marker in _INTERSTITIAL_TITLES):
                return title[:200]
    return None


def slug_title(url: str) -> str | None:
    """A headline recovered from the URL's last path segment, or ``None``.

    The last-resort title: only when the segment is long enough to be a slug
    (:data:`_SLUG_MIN_CHARS`), is hyphen- or underscore-separated, and contains
    a letter — ``/post/2026-nfl-injury-report`` qualifies, ``/news/9081726``
    and ``/2026-09-03-1234`` do not. Any file extension is dropped, separators
    become spaces, the first letter is capitalised, and the result is capped at
    :data:`_SLUG_MAX_TITLE` characters.
    """
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return None
    slug = _EXTENSION_RE.sub("", unquote(segments[-1]))
    if len(slug) < _SLUG_MIN_CHARS or not _SLUG_SEPARATOR_RE.search(slug):
        return None
    words = _WS_RE.sub(" ", _SLUG_SEPARATOR_RE.sub(" ", slug)).strip()
    if not re.search(r"[A-Za-z]", words):
        return None
    return (words[0].upper() + words[1:])[:_SLUG_MAX_TITLE]


async def resolve_source(
    source: dict[str, Any],
    client: httpx.AsyncClient,
    *,
    resolver: Resolver = resolve_public_addresses,
    pin_connections: bool = True,
) -> dict[str, Any]:
    """Return a copy of source with a safe final URL and page title.

    URL validation happens before the initial request and every redirect.
    Each DNS answer must contain only global IPs, so private, loopback and
    link-local addresses (including a mixed DNS-rebinding answer) are never
    requested. Every failure returns the source unchanged.
    """
    if not needs_resolution(source):
        return dict(source)
    original_url = str(source["url"])
    current_url = original_url
    body = b""
    try:
        for redirect_count in range(MAX_REDIRECTS + 1):
            addresses = await ensure_safe_fetch_url(current_url, resolver)
            original_host, _ = parse_fetch_url(current_url)
            is_https = urlsplit(current_url).scheme.lower() == "https"
            request_url = (
                pinned_request_url(current_url, addresses[0]) if pin_connections else current_url
            )
            extensions = {"sni_hostname": original_host} if pin_connections and is_https else {}
            async with client.stream(
                "GET",
                request_url,
                follow_redirects=False,
                headers={"Host": host_header(current_url)},
                extensions=extensions,
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or redirect_count == MAX_REDIRECTS:
                        raise UnsafeSourceURL("invalid or excessive redirects")
                    current_url = urljoin(current_url, location)
                    continue
                content_type = response.headers.get("content-type", "")
                if response.status_code < 400 and "html" in content_type.lower():
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_BYTES:
                            break
                    # The cap is on what is *parsed*, not just on when reading
                    # stops: a single oversized chunk must not smuggle a title in.
                    body = body[:MAX_BYTES]
                break
        else:  # pragma: no cover - the explicit redirect guard always exits
            raise UnsafeSourceURL("excessive redirects")
    except Exception as exc:  # noqa: BLE001 - every failure degrades to "unchanged"
        logger.info("source unresolved (%s): %s", type(exc).__name__, original_url[:80])
        return dict(source)

    resolved = dict(source)
    if current_url != original_url and REDIRECT_MARKER not in current_url:
        resolved["url"] = current_url
    title = extract_title(body.decode("utf-8", errors="replace")) if body else None
    if title:
        resolved["title"] = title
    elif _BARE_DOMAIN_RE.match(str(resolved.get("title") or "").strip()):
        fallback = slug_title(str(resolved.get("url") or ""))
        if fallback:
            resolved["title"] = fallback
    return resolved


async def resolve_sources(
    sources: list[dict[str, Any]],
    *,
    client: httpx.AsyncClient | None = None,
    concurrency: int = CONCURRENCY,
    resolver: Resolver = resolve_public_addresses,
    pin_connections: bool = True,
) -> list[dict[str, Any]]:
    """Resolve every source that needs it, preserving order and count.

    Args:
        sources: The ``sources`` list from a response body.
        client: Injected HTTP client (tests pass one with a mock transport).
        concurrency: Maximum simultaneous fetches.

    Returns:
        A new list, same length, each entry resolved or unchanged.
    """
    if not sources:
        return []
    own_client = client is None
    client = client or httpx.AsyncClient(
        timeout=TIMEOUT_SECONDS,
        trust_env=False,
        headers={"User-Agent": "PlayClock/1.0 (+https://playclock.xyz)"},
        # Pinning puts the vetted *address* in the request URL, which is also
        # what httpx keys its connection pool on. Two hostnames behind one CDN
        # address would therefore share a pool entry, and a keep-alive
        # connection opened with the first host's SNI would be reused for the
        # second — whose certificate was never checked against that name.
        # Nothing here is latency-sensitive, so the pool is simply disabled
        # while pinning is on rather than made hostname-aware.
        limits=_PINNED_LIMITS if pin_connections else httpx.Limits(),
    )
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(source: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(source, dict):
            return source
        async with gate:
            return await resolve_source(
                source, client, resolver=resolver, pin_connections=pin_connections
            )

    try:
        return list(await asyncio.gather(*(one(source) for source in sources)))
    finally:
        if own_client:
            await client.aclose()
