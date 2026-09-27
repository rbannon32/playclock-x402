"""HTTP surface: the free teaser routes and the ten x402-gated paid routes.

Two routers, mounted by :func:`api.main.create_app`:

``free``
    A plain ``APIRouter`` with **no payment dependency** — ``/v1/health``,
    ``/v1/catalog``, ``/v1/trending/preview`` and ``/v1/stats``. The tech spec asks for a
    route-level allowlist rather than path-prefix magic (§3), and "free" here
    means literally "this router declares no ``require_payment`` dependency".

``paid``
    Built with :func:`api.x402.paid_router`, so every route settles a verified
    payment after a 2xx. Never a bare ``APIRouter``: a bare router still 402s
    (the dependency raises) but nothing would ever settle, so the challenge
    leaderboard would stay empty while the API looked healthy.

Shared constants live here because both routers need them: the catalog
advertises the cache TTLs the paid routes actually apply, and both validate the
same week bounds.
"""

from __future__ import annotations

#: Version of the public API contract, reported by ``/v1/health``,
#: ``/v1/catalog`` and the OpenAPI document. Distinct from the package version:
#: this one only moves when the wire contract does.
API_VERSION = "1.0.0"

#: Inclusive bounds for a caller-supplied ``week``. The NFL regular season is 18
#: weeks; anything outside is a client error, not an empty analysis.
MIN_WEEK = 1
MAX_WEEK = 18

#: How long a generated response body is re-served to later payers, per endpoint
#: key (tech spec §6). Endpoints absent from this map are personalized and are
#: never cached: ``player``, ``matchup``, ``roster``, ``team_report``,
#: ``draft_report``.
#:
#: This is the single source of truth — ``/v1/catalog`` advertises these exact
#: numbers and :mod:`api.routes.paid` applies them.
CACHE_TTL_SECONDS: dict[str, int] = {
    # The one intraday board: it is about the last 24h of adds and drops, and
    # the free preview shows the live counts, so a stale paid board would
    # visibly disagree with it.
    "trending": 6 * 3600,
    # Sleepers are a usage-trend call over weeks and waiver claims process
    # daily; both were 6h, which with the 2h warming loop meant regenerating
    # each ~6 times a day for boards that sold a handful of times in three
    # weeks — the loop was two thirds of the bill (DESIGN_NOTES §26). 12h is
    # the honest freshness for what they say, and the post-stats forced
    # refresh still rebuilds them the moment the numbers actually change.
    "sleepers": 12 * 3600,
    "waivers": 12 * 3600,
    "report": 12 * 3600,
    # The board only moves when ingest revises prior-season usage, and draft
    # season is the one time callers hammer a single endpoint — 12h keeps a
    # 988s-class generation off the request path all the way through a draft.
    "draft_board": 12 * 3600,
}

__all__ = ["API_VERSION", "CACHE_TTL_SECONDS", "MAX_WEEK", "MIN_WEEK"]
