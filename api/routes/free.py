"""Free endpoints: health, the catalog, the trending teaser and public stats.

These are the funnel and the discovery surface (PRD §4.1). They take **no**
payment dependency at all — that is what makes them free, per the route-level
allowlist rule in tech spec §3 — and they never call an analysis engine, so they
cost nothing to serve and can absorb crawler, agent and social traffic.

``GET /v1/health``
    Liveness plus the four facts an operator needs when something looks wrong:
    which store, which engine, which week, and how stale the ingested data is.

``GET /v1/catalog``
    Every endpoint, free and paid, with prices, schema names, cache TTLs and the
    payment configuration (network, ``payTo``, asset, facilitator, challenge
    tag). Built from :data:`api.x402.ENDPOINT_SPECS` and the settings price
    table, so it cannot drift from what the 402s actually charge.

``GET /v1/trending/preview``
    The teaser: top movers with names and counts only, no analysis and no
    verdicts. Deliberately thin — it exists to make the paid board obviously
    worth paying for.

``GET /v1/stats``
    Proof of life: settled paid-call counts built from ``receipts/`` (never a
    payer address) and the backtested hit rate of archived verdicts.
"""

from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, Query

from api.core.config import ENDPOINT_KEYS, Settings, get_settings
from api.core.ratelimit import rate_limit_free_routes
from api.core.store import Store, get_store
from api.core.week import current_season, current_week, ingested_season
from api.data.predictions import accuracy_summary
from api.data.stats_store import (
    PLAYERS_COLLECTION,
    TRENDING_COLLECTION,
    exempt_datasets_with_evidence,
    get_data_freshness,
    get_preseason_gap,
    get_trending,
    stale_datasets,
)
from api.routes import API_VERSION, CACHE_TTL_SECONDS
from api.schemas import (
    AccuracyBucket,
    AccuracySummary,
    AccuracyTotals,
    Catalog,
    CatalogEntry,
    EndpointUsage,
    HealthResponse,
    StatsResponse,
    TrendingPreviewPlayer,
    TrendingPreviewResponse,
)
from api.x402 import ENDPOINT_SPECS, RECEIPTS_COLLECTION, usdc_asset_id
from api.x402.facilitator import facilitator_base_url
from api.x402.receipts import receipt_stats
from api.x402.schemas_compat import network_caip2

logger = logging.getLogger(__name__)

# The limiter hangs off the router rather than each route, so a free endpoint
# added later is covered by default. Payment is the limit on the paid routes.
router = APIRouter(
    prefix="/v1",
    tags=["free"],
    dependencies=[Depends(rate_limit_free_routes)],
)

#: How many rows the free teaser gives away. Five is what PRD §4.1 promises.
PREVIEW_LIMIT = 5

#: The free endpoints, described the same way the paid ones are so that
#: ``/v1/catalog`` is one homogeneous list an agent can iterate.
FREE_ENDPOINTS: tuple[dict[str, Any], ...] = (
    {
        "path": "/v1/health",
        "method": "GET",
        "description": "Service health, active week, and per-dataset ingest freshness.",
        "response_schema": HealthResponse.__name__,
    },
    {
        "path": "/v1/catalog",
        "method": "GET",
        "description": (
            "This document: every endpoint with its price, schemas and payment configuration."
        ),
        "response_schema": Catalog.__name__,
    },
    {
        "path": "/v1/trending/preview",
        "method": "GET",
        "description": (
            "Free teaser: the top five Sleeper trending movers, names and counts only, "
            "no analysis or verdicts."
        ),
        "response_schema": TrendingPreviewResponse.__name__,
    },
    {
        "path": "/v1/stats",
        "method": "GET",
        "description": (
            "Public track record: settled paid calls and the backtested hit rate of "
            "every archived verdict. Counts only, never a payer address."
        ),
        "response_schema": StatsResponse.__name__,
    },
)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health and data freshness",
)
async def health() -> HealthResponse:
    """Report liveness, the resolved NFL week, and how fresh the ingested data is.

    ``status`` is ``"degraded"`` when ``meta/freshness`` is empty or any marker
    exceeds its dataset SLA. The process may be up while its scheduled ingest
    is stopped, which is still worth an alert before paid answers use stale data.

    The preseason gap is applied here for the same reason it is applied at the
    paid gate: a dataset upstream has not published yet is not stale, and
    reporting it as such means health reads ``degraded`` for weeks over a
    condition the API itself has decided is not a fault — which trains an
    operator to ignore the one signal that should wake them. It is *reported*
    rather than silently dropped, in ``unavailable_datasets``, so the state
    stays visible without being an alarm. The exemption expires with the gap
    marker after ``GAP_TRUST_SECONDS``, so a dead ingest still turns this red,
    and it is believed only for datasets the store can still show a row for --
    a cold preseason store reports them missing, which is what they are.
    """
    settings = get_settings()
    store = get_store(settings)
    freshness = await get_data_freshness(store)
    gap = await get_preseason_gap(store)
    stale = stale_datasets(freshness, gap=gap)
    unavailable = sorted(await exempt_datasets_with_evidence(store, gap))
    season = await current_season(store, settings)
    found_season = await ingested_season(store)
    season_mismatch = found_season is not None and found_season != int(settings.season)
    return HealthResponse(
        status="ok" if freshness and not stale and not season_mismatch else "degraded",
        version=API_VERSION,
        week=await current_week(store, settings),
        season=season,
        configured_season=int(settings.season),
        season_mismatch=season_mismatch,
        store_backend=settings.store_backend,
        engine=settings.engine,
        data_freshness=freshness,
        stale_datasets=stale,
        unavailable_datasets=unavailable,
    )


@router.get(
    "/catalog",
    response_model=Catalog,
    summary="Machine-readable endpoint catalog for agents",
)
async def catalog() -> Catalog:
    """Return every endpoint with its price, schemas, cache policy and payment config.

    This is the agent-facing discovery document (PRD §4.1). It is assembled from
    the same :data:`api.x402.ENDPOINT_SPECS` the 402 challenges are built from
    and the same price table the payment layer charges against, so a price shown
    here is by construction the price a caller will be quoted.
    """
    settings = get_settings()
    return Catalog(
        service=settings.app_name,
        version=API_VERSION,
        network=settings.x402_network,
        pay_to=settings.x402_pay_to,
        asset_id=int(usdc_asset_id(settings)),
        facilitator_url=facilitator_base_url(settings),
        challenge_tag=settings.x402_challenge_tag,
        endpoints=build_catalog_entries(settings),
    )


def build_catalog_entries(settings: Settings) -> list[CatalogEntry]:
    """Build the catalog's endpoint list: free endpoints first, then paid.

    Paid entries follow :data:`api.core.config.ENDPOINT_KEYS` order, which is the
    intended display order (cheap and broad first, personalized and expensive
    last).
    """
    entries = [
        CatalogEntry(
            path=str(free["path"]),
            method=str(free["method"]),  # type: ignore[arg-type]
            key="",
            price_usdc=0.0,
            description=str(free["description"]),
            request_schema=None,
            response_schema=str(free["response_schema"]),
            free=True,
            cache_ttl_seconds=None,
        )
        for free in FREE_ENDPOINTS
    ]
    entries += [
        CatalogEntry(
            path=spec.path,
            method=spec.method,
            key=spec.key,
            price_usdc=settings.price_for(spec.key),
            description=spec.description,
            request_schema=spec.request_schema,
            response_schema=spec.response_schema,
            free=False,
            cache_ttl_seconds=CACHE_TTL_SECONDS.get(spec.key),
        )
        for spec in (ENDPOINT_SPECS[key] for key in ENDPOINT_KEYS)
    ]
    return entries


@router.get(
    "/trending/preview",
    response_model=TrendingPreviewResponse,
    summary="Free teaser: top five trending players, no analysis",
)
async def trending_preview(
    limit: int = Query(
        PREVIEW_LIMIT,
        ge=1,
        le=PREVIEW_LIMIT,
        description="Rows to return. Capped at the free-teaser size.",
    ),
) -> TrendingPreviewResponse:
    """Return the top movers with identity and trend count only.

    Adds come first (that is where waiver interest lives); drops top the list up
    when fewer than ``limit`` adds have been ingested. No engine runs here — the
    rows are read straight from the ``trending/{add,drop}`` documents the
    30-minute poll writes, joined to ``players/`` only for identity fields the
    poll did not already carry.
    """
    store = get_store()
    rows: list[TrendingPreviewPlayer] = []
    lookback = 24

    for kind in ("add", "drop"):
        if len(rows) >= limit:
            break
        doc = await store.get(TRENDING_COLLECTION, kind)
        if isinstance(doc, dict) and isinstance(doc.get("lookback_hours"), int):
            lookback = int(doc["lookback_hours"])
        for entry in await get_trending(store, kind):
            if len(rows) >= limit:
                break
            row = await _preview_row(store, entry, kind)
            if row is not None:
                rows.append(row)

    return TrendingPreviewResponse(players=rows, lookback_hours=lookback)


async def _preview_row(
    store: Store, entry: dict[str, Any], kind: str
) -> TrendingPreviewPlayer | None:
    """Build one teaser row, filling identity from ``players/`` when ingest did not.

    Returns ``None`` for an entry with no player id — a row we cannot name is
    worse than one row fewer.
    """
    player_id = str(entry.get("player_id") or "").strip()
    if not player_id:
        return None

    name = entry.get("name")
    position = entry.get("position")
    team = entry.get("team")
    if not name or not position:
        doc = await store.get(PLAYERS_COLLECTION, player_id) or {}
        name = name or doc.get("name")
        position = position or doc.get("position")
        team = team or doc.get("team")

    return TrendingPreviewPlayer(
        player_id=player_id,
        name=str(name or player_id),
        position=str(position or "UNK").upper(),
        team=str(team).upper() if team else None,
        trend=kind,  # type: ignore[arg-type]
        trend_count=int(entry.get("count") or 0),
    )


#: How long an aggregate is reused. Receipts only grow, so a minute-stale count
#: is honest, and rebuilding it per request would scan the whole collection on a
#: free endpoint — an invitation to make someone else's traffic our bill.
_STATS_TTL_SECONDS = 60.0
_stats_cache: tuple[float, StatsResponse] | None = None


def clear_stats_cache() -> None:
    """Drop the memoised aggregate. For tests."""
    global _stats_cache
    _stats_cache = None


@router.get(
    "/stats",
    response_model=StatsResponse,
    summary="Aggregate usage: how much has actually been paid for",
)
async def stats() -> StatsResponse:
    """Free proof of life from settled receipts without an unbounded hot-path read.

    Production reads 32 per-network rollup shards after the explicit historical
    backfill.  Before that marker exists, this retains the exact legacy scan:
    an incomplete migration must never make a public payment total look smaller.
    """
    global _stats_cache
    now = monotonic()
    if _stats_cache and now - _stats_cache[0] < _STATS_TTL_SECONDS:
        return _stats_cache[1]

    settings = get_settings()
    active = network_caip2(settings)
    store = get_store(settings)
    summary = await receipt_stats(store, active)
    if summary is None:
        receipts = [
            receipt
            for receipt in await store.list(RECEIPTS_COLLECTION)
            if str(receipt.get("network") or "") == active
        ]
        payers = {str(receipt["payer"]) for receipt in receipts if receipt.get("payer")}
        by_endpoint: dict[str, dict[str, float]] = {}
        earliest: str | None = None
        for receipt in receipts:
            endpoint = str(receipt.get("endpoint") or "unknown")
            amount = float(receipt.get("amount_usdc") or 0.0)
            bucket = by_endpoint.setdefault(endpoint, {"count": 0, "total_usdc": 0.0})
            bucket["count"] += 1
            bucket["total_usdc"] += amount
            ts = receipt.get("ts")
            if ts and (earliest is None or str(ts) < earliest):
                earliest = str(ts)
        summary = {
            "count": len(receipts),
            "total_usdc": round(sum(float(r.get("amount_usdc") or 0.0) for r in receipts), 6),
            "unique_payers": len(payers),
            "since": earliest,
            "by_endpoint": by_endpoint,
        }

    breakdown = sorted(
        (
            EndpointUsage(
                key=key,
                paid_calls=int(bucket["count"]),
                usdc=round(float(bucket["total_usdc"]), 6),
            )
            for key, bucket in summary["by_endpoint"].items()
        ),
        key=lambda entry: (-entry.paid_calls, entry.key),
    )
    body = StatsResponse(
        paid_analyses=int(summary["count"]),
        unique_payers=int(summary["unique_payers"]),
        usdc_settled=round(float(summary["total_usdc"]), 6),
        network=settings.x402_network,
        since=summary["since"],
        by_endpoint=breakdown,
        accuracy=_accuracy_block(
            await accuracy_summary(store, await current_season(store, settings))
        ),
    )
    _stats_cache = (now, body)
    return body


def _accuracy_block(doc: dict[str, Any] | None) -> AccuracySummary | None:
    """Shape the backtest's summary for publication, or ``None`` before one exists.

    Only the aggregate numbers are republished. The per-claim documents name
    players and calls, which is fine, but they are the backtest's working state
    and not a contract.
    """
    if not doc:
        return None
    try:
        overall = dict(doc.get("overall") or {})
        return AccuracySummary(
            season=int(doc["season"]),
            weeks_scored=[int(w) for w in doc.get("weeks") or []],
            overall=AccuracyTotals(
                scored=int(overall.get("scored") or 0),
                hits=int(overall.get("hits") or 0),
                hit_rate=overall.get("hit_rate"),
            ),
            by_endpoint=[
                AccuracyBucket(
                    key=str(row.get("key") or "unknown"),
                    scored=int(row.get("scored") or 0),
                    hits=int(row.get("hits") or 0),
                    hit_rate=row.get("hit_rate"),
                )
                for row in doc.get("by_endpoint") or []
                if isinstance(row, dict)
            ],
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("backtest summary is malformed; publishing no accuracy block")
        return None
