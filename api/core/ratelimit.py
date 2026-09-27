"""A per-client cap on the free routes.

Payment is the rate limit on the paid routes: an agent that wants a thousand
analyses buys a thousand analyses. The free routes have no such governor, and
three of them (/v1/health, /v1/catalog, /v1/trending/preview) read Firestore on
every call. An advert, a crawler, or one enthusiastic agent is therefore an
unbounded bill against a $50/month budget.

Deliberately small: a fixed-window counter in process memory, no Redis, no
dependency. It is per instance and resets when an instance recycles. This is a
cost guard, not a product-metering system.

Set FREE_RATE_LIMIT_PER_MINUTE=0 to turn it off. Production deployments must
also configure TRUSTED_PROXY_HOPS: the number of trusted proxies that append
addresses to X-Forwarded-For. This module never assumes a provider's header
layout.
"""

from __future__ import annotations

import ipaddress
import math
import time
from collections import OrderedDict

from fastapi import HTTPException, Request

from api.core.config import Settings, get_settings

__all__ = ["client_key", "clear_rate_limits", "rate_limit_free_routes"]

# {client: (window_start, count)}, least-recently used first.
_HITS: OrderedDict[str, tuple[float, int]] = OrderedDict()

# Maximum distinct clients retained in-process. When it is full, expired entries
# and then only the least-recently-used entry are evicted. Existing clients keep
# their counters; rotating identities cannot reset the entire table.
_MAX_TRACKED = 10_000

WINDOW_SECONDS = 60.0


def _peer_key(request: Request) -> str:
    """Return the socket peer, for direct and local deployments."""
    return request.client.host if request.client else "unknown"


def client_key(request: Request, *, trusted_proxy_hops: int = 0) -> str:
    """Identify a caller by counting inwards from the socket, never from the header.

    Each proxy *appends* the address it received the request from, so with N
    trusted proxies the rightmost N-1 entries are proxies and the entry at
    ``len(values) - N`` is the address the outermost trusted proxy actually
    observed. That is the caller. Everything to its left was supplied by the
    caller and is untrusted.

    Counting from the left instead is the bypass: with one trusted proxy, a
    caller sending ``X-Forwarded-For: 198.51.100.1`` produces
    ``198.51.100.1, <real client>`` at the application, and any index measured
    from the start of the header selects the value the attacker chose --
    rotating it defeats the limit entirely. It is also why a single-value
    header is not "too short": one trusted proxy that overwrites the header
    leaves exactly one value, and that value is the client.

    A missing, malformed, or genuinely too-short header falls back to the
    socket peer -- that is a request that did not traverse the trusted path.
    """
    if trusted_proxy_hops <= 0:
        return _peer_key(request)

    values = [value.strip() for value in request.headers.get("x-forwarded-for", "").split(",")]
    values = [value for value in values if value]
    client_index = len(values) - trusted_proxy_hops
    if client_index < 0:
        return _peer_key(request)

    try:
        return ipaddress.ip_address(values[client_index]).compressed
    except ValueError:
        return _peer_key(request)


def clear_rate_limits() -> None:
    """Forget every counter. For tests."""
    _HITS.clear()


def _make_room(now: float) -> None:
    """Evict expired clients, then the least-recently-used client if necessary."""
    # Expiry is checked when a client returns. Until capacity is reached,
    # retaining inactive entries is bounded and avoids a scan on every new IP.
    if len(_HITS) < _MAX_TRACKED:
        return

    expired = [
        client
        for client, (window_start, _count) in _HITS.items()
        if now - window_start >= WINDOW_SECONDS
    ]
    for client in expired:
        del _HITS[client]

    if len(_HITS) >= _MAX_TRACKED:
        _HITS.popitem(last=False)


async def rate_limit_free_routes(request: Request) -> None:
    """Cap free-route calls per client per minute.

    Raises:
        HTTPException: 429 with a Retry-After header when over the cap.
    """
    settings: Settings = get_settings()
    limit = int(settings.free_rate_limit_per_minute)
    if limit <= 0:
        return

    now = time.monotonic()
    key = client_key(request, trusted_proxy_hops=settings.trusted_proxy_hops)

    window_start, count = _HITS.get(key, (now, 0))
    if now - window_start >= WINDOW_SECONDS:
        window_start, count = now, 0

    if key not in _HITS:
        _make_room(now)

    count += 1
    _HITS[key] = (window_start, count)
    _HITS.move_to_end(key)

    if count > limit:
        retry_after = max(1, math.ceil(WINDOW_SECONDS - (now - window_start)))
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many free requests — {limit} per minute. The paid endpoints "
                "have no such limit; payment is their rate limit."
            ),
            headers={"Retry-After": str(retry_after)},
        )
