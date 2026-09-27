"""The ten x402-gated endpoints — the product.

Every route here follows the same three-step shape:

#. ``Depends(require_payment(<endpoint_key>))`` gates the call. The dependency
   402s with the payment requirements and Bazaar discovery block, verifies a
   ``PAYMENT-SIGNATURE`` header, and :class:`~api.x402.PaidRoute` settles after
   a 2xx. A handler that raises settles nothing, so a failed analysis is never
   billed (DESIGN_NOTES §2) — which is why every failure path below raises
   rather than returning a partial body.
#. The handler assembles a ``request_context`` and hands it to
   :func:`api.agents.get_engine`. Routes own I/O (Sleeper calls, the response
   cache, week resolution); engines own analysis. Nothing here knows whether the
   deterministic engine or the ADK pipeline answered.
#. ``meta.cache`` is stamped by the route, because caching is a route policy:
   ``fresh`` = generated now and cached for later payers, ``hit`` = re-served
   from ``response_cache``, ``miss`` = generated now and deliberately not cached.

Caching (tech spec §6)
----------------------
``trending`` is cached 6h; ``sleepers``, ``waivers``, ``report`` and
``draft-board`` (under week 0, since it is season-scoped) 12h — see
:data:`api.routes.CACHE_TTL_SECONDS`. That is the unit economics: 100 payers of
``/v1/sleepers`` cost one analysis run. The personalized endpoints
(``player``, ``matchup``, ``roster``, ``team-report``, ``draft-report``) are
always fresh; caching
one manager's roster audit and serving it to another would be a correctness bug,
not a saving.

Sleeper live calls
------------------
``/v1/roster`` and ``/v1/team-report`` are the only request paths allowed to call
Sleeper live (tech spec §4.1) — user-triggered and low volume. Unknown user or
league is a clean ``404``; a Sleeper outage is a ``502``, never a 500 that looks
like our bug.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query
from pydantic import ValidationError

from api.agents import RESPONSE_MODELS, EngineError, get_engine
from api.core.config import Settings, get_settings
from api.core.store import Store, get_store
from api.core.week import current_season, current_week, ingested_season
from api.data.cache import ResponseCache, cache_key
from api.data.predictions import archive_predictions
from api.data.sleeper import SleeperClient, SleeperError, SleeperNotFound
from api.data.stats_store import (
    PLAYERS_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    exempt_datasets_with_evidence,
    get_data_freshness,
    get_preseason_gap,
    get_trending,
    stale_datasets,
)
from api.data.team_analytics import (
    build_team_report_facts,
    free_agent_pool,
    mark_unplayed,
    starting_slots,
    week_one_outlook,
)
from api.routes import CACHE_TTL_SECONDS, MAX_WEEK, MIN_WEEK
from api.schemas import (
    AnalysisResponse,
    DraftBoardResponse,
    DraftReportRequest,
    DraftReportResponse,
    MatchupRequest,
    MatchupResponse,
    PlayerRequest,
    PlayerResponse,
    ReportResponse,
    RosterRequest,
    RosterResponse,
    SleepersResponse,
    TeamReportRequest,
    TeamReportResponse,
    TrendingResponse,
    WaiversResponse,
)
from api.x402 import PaymentContext, paid_router, require_payment

logger = logging.getLogger(__name__)

#: Paid routes settle payments; a bare ``APIRouter`` would 402 correctly and then
#: silently never settle (api/x402/middleware.py).
router = paid_router(prefix="/v1", tags=["paid"])

#: Upper bound on the candidate pool used to derive a league's free agents.
#:
#: The literal definition (tech spec §4.1) is "every Sleeper player minus everyone
#: rostered in the league", but the full player universe is ~11k documents and a
#: recommendation we cannot justify is worthless anyway. The candidates are
#: therefore the players the market is moving on (Sleeper trending) plus the
#: players with an ingested usage rollup — exactly the set the analysis can say
#: something about — capped so a paid call has a bounded read budget.
FREE_AGENT_UNIVERSE_LIMIT = 250

#: Concurrency for the fan-out player-document reads.
_FETCH_CHUNK = 25

#: ``meta/freshness`` keys each endpoint needs before it can answer at all.
#:
#: The names are exactly the dataset names ingest stamps: ``nightly`` writes
#: ``players``/``player_index``/``id_map``, ``trending`` writes ``trending``, and
#: ``stats`` writes ``weekly_stats``/``usage_trends``/``def_vs_pos``/
#: ``schedules`` (plus ``injuries``/``depth_charts`` when those sub-pulls
#: succeed). Only the datasets whose absence would empty the response are listed
#: — ``def_vs_pos`` and ``schedules`` sharpen an answer but never are one, and
#: the optional sub-pulls must never gate a sale.
REQUIRED_DATASETS: dict[str, tuple[str, ...]] = {
    # The board is the Sleeper market signal; names come from players/.
    "trending": ("players", "trending"),
    # Picks are scored from usage rollups and weekly lines.
    "sleepers": ("players", "weekly_stats", "usage_trends"),
    # The waiver board *is* the trending add list, ranked by usage.
    "waivers": ("players", "weekly_stats", "usage_trends", "trending"),
    "report": ("players", "weekly_stats", "usage_trends"),
    "player": ("players", "weekly_stats"),
    "matchup": ("players", "weekly_stats"),
    # Roster grades and league-available fixes are usage calls, not box scores.
    "roster": ("players", "usage_trends"),
    "team_report": ("players", "usage_trends"),
    # The board is ranked from the market order in players/ and adjusted by
    # prior-season usage; without usage_trends it would be a re-print of
    # Sleeper's own ordering, which is not worth paying for.
    "draft_board": ("players", "usage_trends"),
    "draft_report": ("players",),
}


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------


def validate_week(week: int | None) -> int | None:
    """Return ``week`` unchanged, or raise ``400`` when it is out of season.

    ``None`` (meaning "use the current week") passes through.
    """
    if week is None:
        return None
    if not MIN_WEEK <= week <= MAX_WEEK:
        raise HTTPException(
            status_code=400,
            detail=f"week must be between {MIN_WEEK} and {MAX_WEEK}; got {week}.",
        )
    return week


async def resolve_week(store: Store, settings: Settings, week: int | None) -> int:
    """Validate a caller-supplied week, defaulting to the current NFL week."""
    validated = validate_week(week)
    if validated is not None:
        return validated
    return await current_week(store, settings)


async def missing_datasets(store: Store, endpoint_key: str) -> list[str]:
    """Return required datasets that are missing or older than their SLA.

    ``meta/freshness`` is stamped **per dataset** by whichever ingest task wrote
    it (``ingest/common.py``), and the tasks fail independently: a successful
    ``nightly`` or ``trending`` run leaves the marker non-empty while
    ``weekly_stats``/``usage_trends``/``schedules`` are still missing. Checking
    only that the document exists would therefore treat a half-ingested store as
    ready — so each endpoint declares the datasets it actually reads
    (:data:`REQUIRED_DATASETS`).

    Separate from :func:`require_ingested_data` because the same question has a
    second, non-HTTP caller: :mod:`ingest.precompute` must not warm a board out
    of data that is not there, and it has no response to raise.
    """
    freshness = await get_data_freshness(store)
    required = REQUIRED_DATASETS[endpoint_key]
    gap = await get_preseason_gap(store)
    exempt = await exempt_datasets_with_evidence(store, gap)

    # A dataset upstream has not published yet is neither missing nor stale.
    # Ingest deliberately withholds freshness markers when nflverse has no
    # preseason stats file, so the exemption must cover absent markers too --
    # but only where the store can still show a row behind the claim. On a cold
    # preseason store there is nothing to answer from, and exempting there would
    # settle a payment for an empty board rather than 503 without charging.
    unavailable = [name for name in required if name not in freshness and name not in exempt]
    unavailable.extend(stale_datasets(freshness, required, gap=gap))
    return unavailable


async def stale_season(store: Store, settings: Settings) -> tuple[int, int] | None:
    """Return ``(ingested, expected)`` when the store holds a different season.

    The failure this exists for is the one nothing else reports. Every dataset
    is present, every freshness marker is stamped, :func:`missing_datasets`
    returns nothing — and every answer is about last season. Fresh data for the
    wrong year looks identical to fresh data for the right one, right up until a
    customer reads it.

    Returns ``None`` when the store has no schedule at all: that is plain
    missing data and :func:`missing_datasets` says so more usefully.

    Like :func:`missing_datasets`, this has a second, non-HTTP caller —
    :mod:`ingest.precompute` must not warm a board out of last season's numbers,
    and a cached board is served for its whole TTL before anyone could notice.
    """
    found = await ingested_season(store)
    if found is None:
        return None
    expected = int(settings.season)
    return (found, expected) if found != expected else None


async def require_ingested_data(store: Store, endpoint_key: str) -> None:
    """Refuse paid work this endpoint has no data for, so nothing empty is billed.

    Selling an empty sleepers board at full price is the bug this prevents; any
    dataset :func:`missing_datasets` reports is a 503 — as does a store filled
    for a different season, which is the same sale with better camouflage.

    A 503 is non-2xx, so :class:`~api.x402.PaidRoute` never settles and the
    caller keeps their USDC.
    """
    settings = get_settings()
    wrong = await stale_season(store, settings)
    if wrong:
        found, expected = wrong
        raise HTTPException(
            status_code=503,
            detail=(
                f"The ingested data is for the {found} season but this service is "
                f"configured for {expected}. Refusing to sell {expected} analysis "
                f"built from {found} numbers. You were not charged."
            ),
        )

    missing = await missing_datasets(store, endpoint_key)
    if missing:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Ingestion has not produced the data {endpoint_key!r} needs yet "
                f"(missing or stale: {', '.join(missing)}). You were not charged. "
                "Try again shortly."
            ),
        )


async def _run_engine(endpoint_key: str, context: dict[str, Any]) -> AnalysisResponse:
    """Run the active engine, mapping an engine failure to ``500``.

    The 500 matters: a non-2xx means :class:`~api.x402.PaidRoute` never settles,
    so a caller whose analysis failed keeps their USDC.

    Split from :func:`analyze` so :func:`cached_analysis` can run the readiness
    check *before* its cache lookup without paying for a second one on a miss.
    """
    try:
        return await get_engine().analyze(endpoint_key, context)
    except EngineError as exc:
        logger.exception("engine failed for %s", endpoint_key)
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed for {endpoint_key!r}. You were not charged.",
        ) from exc


async def analyze(endpoint_key: str, context: dict[str, Any]) -> AnalysisResponse:
    """Check readiness, then run the engine."""
    await require_ingested_data(get_store(), endpoint_key)
    return await _run_engine(endpoint_key, context)


async def fresh_analysis(endpoint_key: str, context: dict[str, Any]) -> AnalysisResponse:
    """Generate an uncached (personalized) body and stamp ``meta.cache='miss'``."""
    body = await analyze(endpoint_key, context)
    body.meta.cache = "miss"
    # File the claims so the weekly backtest can score them. Never raises.
    await archive_predictions(get_store(), endpoint_key, body, week=context.get("week"))
    return body


async def cached_analysis(
    endpoint_key: str,
    context: dict[str, Any],
    *,
    week: int,
    extra: str = "",
) -> AnalysisResponse:
    """Serve ``endpoint_key`` from ``response_cache``, generating only when safe.

    Args:
        endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`; must have an
            entry in :data:`api.routes.CACHE_TTL_SECONDS`.
        context: The engine's ``request_context``.
        week: Week the body is scoped to — part of the cache key.
        extra: Any further parameter that changes the body (``limit``, lookback
            window). Must be deterministic for a given set of inputs, or two
            callers asking the same question would miss each other's cache.

    Returns:
        The response body with ``meta.cache`` set to ``"hit"`` or ``"fresh"``.
    """
    # BEFORE the cache read, not after. A warm entry is returned without ever
    # reaching the engine, so a readiness check that lives inside `analyze` only
    # guards a cache *miss* — and the entry most likely to be wrong is precisely
    # the one warmed from last season's data, which would be served as a paid
    # hit for its whole TTL.
    store = get_store()
    await require_ingested_data(store, endpoint_key)

    cache = ResponseCache(store)
    key = cache_key(endpoint_key, week, extra)
    model = RESPONSE_MODELS[endpoint_key]

    payload = await cache.get(key)
    if payload is not None:
        try:
            body = model.model_validate(payload)
        except ValidationError:
            # A contract change since the entry was written. Drop it and
            # regenerate rather than 500 on a paid call.
            logger.warning("evicting unreadable cache entry %s", key)
            await cache.delete(key)
        else:
            body.meta.cache = "hit"
            return body

    # An ADK board can take longer than every request deadline (observed cold
    # runs reached 988s). These league-wide responses are precomputed, so never
    # start that work after a customer has signed a payment: a 503 is non-2xx
    # and the paid-route wrapper therefore settles nothing.
    if get_settings().engine == "adk":
        raise HTTPException(
            status_code=503,
            detail=(
                f"The {endpoint_key!r} board is being prepared and is not ready to serve. "
                "You were not charged. Try again shortly."
            ),
        )

    payload = await _generate_once(key, endpoint_key, context, cache, week)
    body = model.model_validate(payload)
    body.meta.cache = "fresh"
    return body


#: In-flight generations, keyed by cache key. See :func:`_generate_once`.
_INFLIGHT: dict[str, asyncio.Task[dict[str, Any]]] = {}


async def _generate_once(
    key: str,
    endpoint_key: str,
    context: dict[str, Any],
    cache: ResponseCache,
    week: int,
) -> dict[str, Any]:
    """Generate and cache one body, collapsing concurrent callers onto one run.

    Without this, ten callers arriving on an expired deterministic or narrated
    board start ten identical generations. ADK cache misses never reach this
    function: cached_analysis returns an unbilled 503 before the engine runs,
    because observed cold boards can exceed the request deadline.

    The generation runs as a detached task that *every* caller — the first
    one included — awaits through :func:`asyncio.shield`. So a caller whose
    request is cancelled (a client disconnect) stops waiting without
    cancelling the generation or failing the callers still waiting on it; the
    body is finished and cached either way. A failure reaches every waiter,
    and the task is dropped from :data:`_INFLIGHT` when it finishes. Each
    caller validates its own model instance from the shared payload, so nobody
    hands out a body another caller can mutate.

    Scope, stated plainly: this is per-process. Cloud Run runs up to ten
    instances, so a truly simultaneous cold start can still produce one
    generation per instance. That is a tenth of the problem and needs no
    distributed lock; the remaining tenth is what warming is for.
    """
    task = _INFLIGHT.get(key)
    if task is not None and not task.done():
        logger.info("joining in-flight generation for %s", key)
    else:
        task = asyncio.create_task(
            _generate(key, endpoint_key, context, cache, week), name=f"generate:{key}"
        )
        _INFLIGHT[key] = task
        task.add_done_callback(lambda done: _generation_finished(key, done))
    return await asyncio.shield(task)


async def _generate(
    key: str,
    endpoint_key: str,
    context: dict[str, Any],
    cache: ResponseCache,
    week: int,
) -> dict[str, Any]:
    """The one generation behind :func:`_generate_once`: run, cache, archive."""
    body = await _run_engine(endpoint_key, context)
    payload = body.model_dump(mode="json")
    await cache.set(
        key,
        payload,
        CACHE_TTL_SECONDS[endpoint_key],
        endpoint=endpoint_key,
        week=week,
    )
    await archive_predictions(get_store(), endpoint_key, payload, week=week)
    return payload


def _generation_finished(key: str, task: asyncio.Task[dict[str, Any]]) -> None:
    """Forget a finished generation and retrieve its outcome.

    Retrieving the exception here is what keeps a failure nobody was left
    awaiting (every caller disconnected) from logging "Task exception was
    never retrieved"; each waiter still receives it through its shield.
    """
    if _INFLIGHT.get(key) is task:
        del _INFLIGHT[key]
    if not task.cancelled():
        task.exception()


def clear_inflight() -> None:
    """Drop any in-flight generation bookkeeping. For tests."""
    _INFLIGHT.clear()


# ---------------------------------------------------------------------------
# GET /v1/trending
# ---------------------------------------------------------------------------


@router.get(
    "/trending",
    response_model=TrendingResponse,
    summary="Full trending board with analysis and add/fade verdicts",
)
async def trending(
    week: int | None = Query(None, description="NFL week. Defaults to the current week."),
    lookback_hours: int = Query(
        24,
        ge=1,
        le=168,
        description=(
            "Accepted for compatibility and ignored: the board covers the window "
            "the trending poll ingests, which the response's lookback_hours reports."
        ),
    ),
    limit: int = Query(25, ge=1, le=50, description="Board size."),
    payment: PaymentContext = Depends(require_payment("trending")),
) -> TrendingResponse:
    """Top trending adds and drops with per-player stat context and a verdict."""
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, week)
    # One board per limit: every lookback reads the same ingested window, so it
    # must not split the cache (and miss the warmed ``lookback=24`` entry).
    del lookback_hours
    context = {"week": resolved, "lookback_hours": 24, "limit": limit}
    body = await cached_analysis(
        "trending",
        context,
        week=resolved,
        extra=f"lookback=24:limit={limit}",
    )
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# GET /v1/sleepers
# ---------------------------------------------------------------------------


@router.get(
    "/sleepers",
    response_model=SleepersResponse,
    summary="Weekly sleeper picks with usage and matchup reasoning",
)
async def sleepers(
    week: int | None = Query(None, description="NFL week. Defaults to the current week."),
    limit: int = Query(12, ge=1, le=25, description="Maximum picks to return."),
    payment: PaymentContext = Depends(require_payment("sleepers")),
) -> SleepersResponse:
    """Eight to twelve low-rostered starts for the week, with confidence tiers."""
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, week)
    body = await cached_analysis(
        "sleepers",
        {"week": resolved, "limit": limit},
        week=resolved,
        extra=f"limit={limit}",
    )
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# GET /v1/waivers
# ---------------------------------------------------------------------------


@router.get(
    "/waivers",
    response_model=WaiversResponse,
    summary="Waiver wire big board with FAB guidance",
)
async def waivers(
    week: int | None = Query(None, description="NFL week. Defaults to the current week."),
    limit: int = Query(15, ge=1, le=50, description="Board size."),
    payment: PaymentContext = Depends(require_payment("waivers")),
) -> WaiversResponse:
    """Ranked waiver targets with stash/start labels and suggested FAB bids."""
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, week)
    body = await cached_analysis(
        "waivers",
        {"week": resolved, "limit": limit},
        week=resolved,
        extra=f"limit={limit}",
    )
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# GET /v1/report
# ---------------------------------------------------------------------------


@router.get(
    "/report",
    response_model=ReportResponse,
    summary="League-wide weekly briefing (cached 12h)",
)
async def report(
    week: int | None = Query(None, description="NFL week. Defaults to the current week."),
    payment: PaymentContext = Depends(require_payment("report")),
) -> ReportResponse:
    """Emerging players, injury fallout chains, stock up/down, rookies, streamers."""
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, week)
    body = await cached_analysis("report", {"week": resolved}, week=resolved)
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/player
# ---------------------------------------------------------------------------


@router.post(
    "/player",
    response_model=PlayerResponse,
    summary="Deep dive on one player",
)
async def player(
    request: PlayerRequest,
    payment: PaymentContext = Depends(require_payment("player")),
) -> PlayerResponse:
    """Last-four-week trends, usage trajectory, schedule outlook and a verdict."""
    if not (request.name or request.player_id):
        raise HTTPException(status_code=400, detail="Supply either 'name' or 'player_id'.")
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, request.week)
    body = await fresh_analysis(
        "player",
        {"week": resolved, "name": request.name, "player_id": request.player_id},
    )
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/matchup
# ---------------------------------------------------------------------------


@router.post(
    "/matchup",
    response_model=MatchupResponse,
    summary="Start/sit ranking across two to four players",
)
async def matchup(
    request: MatchupRequest,
    payment: PaymentContext = Depends(require_payment("matchup")),
) -> MatchupResponse:
    """Rank the supplied players best-to-worst start for the week."""
    settings = get_settings()
    resolved = await resolve_week(get_store(settings), settings, request.week)
    body = await fresh_analysis("matchup", {"week": resolved, "players": list(request.players)})
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/roster
# ---------------------------------------------------------------------------


@router.post(
    "/roster",
    response_model=RosterResponse,
    summary="Full roster audit (Sleeper username or pasted roster)",
)
async def roster(
    request: RosterRequest,
    payment: PaymentContext = Depends(require_payment("roster")),
) -> RosterResponse:
    """Grade a roster, call the lineup, and name the adds available in that league.

    With a ``sleeper_username`` the roster and the league's free-agent pool are
    pulled live from Sleeper; with a pasted ``roster`` no league is known, so
    ``free_agents`` is left **absent** rather than empty — absent means "we do
    not know who is available", empty would mean "nobody is", and the engine
    treats those differently.
    """
    if not (request.sleeper_username or request.roster):
        raise HTTPException(
            status_code=400,
            detail="Supply either 'sleeper_username' or a 'roster' list.",
        )

    settings = get_settings()
    store = get_store(settings)
    resolved = await resolve_week(store, settings, request.week)

    context: dict[str, Any] = {"week": resolved}
    if request.sleeper_username:
        season = await current_season(store, settings)
        async with sleeper_client(settings) as client:
            league = await load_league(client, request.sleeper_username, request.league_id, season)
        universe = await free_agent_universe(store)
        docs = await player_docs(store, [*league.my_player_ids, *universe])
        pool = free_agent_pool(league.rosters, universe)
        context.update(
            sleeper_username=request.sleeper_username,
            league_id=league.league_id,
            roster=roster_entries(league, docs),
            free_agents=free_agent_entries(sorted(pool), docs),
        )
    else:
        context.update(
            sleeper_username=None,
            league_id=request.league_id,
            roster=[entry.model_dump() for entry in request.roster or []],
        )

    body = await fresh_analysis("roster", context)
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/team-report
# ---------------------------------------------------------------------------


@router.post(
    "/team-report",
    response_model=TeamReportResponse,
    summary="Team-aware deep report graded against your actual leaguemates",
)
async def team_report(
    request: TeamReportRequest,
    payment: PaymentContext = Depends(require_payment("team_report")),
) -> TeamReportResponse:
    """Positional strength vs. leaguemates, league-available fixes, manager review.

    Every league-relative number is computed in Python by
    :func:`api.data.team_analytics.build_team_report_facts` from Sleeper matchup
    history and passed to the engine as ``team_analytics`` — the engine narrates
    those numbers and never produces them (tech spec §6). That history covers
    completed weeks only; see ``history_week`` below.
    """
    settings = get_settings()
    store = get_store(settings)
    resolved = await resolve_week(store, settings, request.week)
    season = await current_season(store, settings)

    # History stops at the last *completed* week. Sleeper's week flips Tuesday
    # morning ET while the games are played Thursday to Monday, so the active
    # week is at best partially played: including it would drag every manager
    # metric (lineup efficiency, points for, all-play record) toward zero for
    # most of the week. ``team_analytics`` skips an all-zero week, but a week
    # with Thursday's game in the books is neither zero nor comparable. The
    # report is still *about* week ``resolved`` — only its history is trimmed,
    # and in week 1 that history is legitimately empty. A future ``week`` does
    # not move the line: the last completed week is still the one before today's.
    active = resolved if request.week is None else await current_week(store, settings)
    history_week = min(resolved, active) - 1

    async with sleeper_client(settings) as client:
        league = await load_league(client, request.sleeper_username, request.league_id, season)
        details = await client.get_league(league.league_id)
        users = await league_users(client, league.league_id)
        matchups = await load_matchups(client, league.league_id, history_week)
        # Before kickoff there is no history to trim — there is none at all. The
        # draft and the schedule are the only things that have happened, so they
        # are what the report is built from.
        preseason = await load_preseason(client, league, resolved) if history_week < 1 else None

    universe = await free_agent_universe(store)
    week_one_entries = (preseason or {}).get("week_one_entries") or []
    docs = await player_docs(
        store,
        [
            *league.all_player_ids,
            *matchup_player_ids(matchups),
            *matchup_player_ids({resolved: week_one_entries}),
            *[str(p.get("player_id") or "") for p in (preseason or {}).get("my_picks") or []],
            *universe,
        ],
    )
    facts = build_team_report_facts(
        league={**details, "league_id": league.league_id},
        rosters=league.rosters,
        matchups_by_week=matchups,
        my_roster_id=league.my_roster_id,
        player_lookup=player_lookup(docs),
        users=users,
        player_universe_ids=universe,
        season=season,
        through_week=history_week,
        sleeper_username=request.sleeper_username,
    )
    pool_ids = (facts.get("free_agent_pool") or {}).get("player_ids") or []
    # Sanitize before any engine sees the facts, not inside one of them: the ADK
    # synthesis agent is told to reproduce these blocks exactly, so a grade
    # derived from zero games has to be gone by the time it is asked to.
    facts = mark_unplayed(facts)

    if preseason is not None:
        # A rookie or keeper draft is a handful of picks against a full global
        # market rank, so every pick scores as an enormous reach and the roster
        # looks thin at every position. That is the truncated-board trap the
        # draft board already guards against: the arithmetic is only meaningful
        # when the draft actually built the starting lineup.
        preseason["gradeable"] = len(preseason["my_picks"]) >= len(
            starting_slots(details.get("roster_positions"))
        )
        preseason["week_one"] = week_one_outlook(
            matchups=week_one_entries,
            my_roster_id=league.my_roster_id,
            rosters=league.rosters,
            users=users,
            market_ranks={
                player_id: doc.get("search_rank") for player_id, doc in docs.items() if doc
            },
        )
        # Nothing played, nothing drafted, nobody scheduled: there is no analysis
        # to sell. Refusing costs us the sale; billing for a page of zeros costs
        # more than that.
        if not preseason["gradeable"] and preseason["week_one"] is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"No games have been played in league {league.league_id!r} yet, and "
                    "there is no full draft or scheduled week-1 matchup to report on "
                    "either. You were not charged. Try again once the season starts."
                ),
            )

    body = await fresh_analysis(
        "team_report",
        {
            "week": resolved,
            "season": season,
            "sleeper_username": request.sleeper_username,
            "league_id": league.league_id,
            "league_name": details.get("name"),
            "roster": roster_entries(league, docs),
            "free_agents": free_agent_entries(pool_ids, docs),
            "team_analytics": facts,
            "preseason": preseason,
        },
    )
    return body  # type: ignore[return-value]


async def load_preseason(client: SleeperClient, league: LeagueView, week: int) -> dict[str, Any]:
    """Fetch the league's draft and its week-``week`` schedule.

    Both are optional and neither failure is fatal here: the caller decides
    whether what came back is enough to be worth selling. A Sleeper outage still
    surfaces from the calls the report cannot do without, higher up.
    """
    draft_id = ""
    my_picks: list[dict[str, Any]] = []
    try:
        for draft in await client.get_league_drafts(league.league_id):
            candidate = str(draft.get("draft_id") or "")
            if not candidate:
                continue
            picks = await client.get_draft_picks(candidate)
            if not picks:
                continue
            draft_id = candidate
            my_picks = [p for p in picks if _pick_belongs_to(p, league)]
            break
    except (SleeperError, SleeperNotFound) as exc:
        logger.info("no draft for league %s: %s", league.league_id, exc)

    try:
        week_one_entries = await client.get_matchups(league.league_id, week)
    except (SleeperError, SleeperNotFound) as exc:
        logger.info("no week %s schedule for league %s: %s", week, league.league_id, exc)
        week_one_entries = []

    return {"draft_id": draft_id, "my_picks": my_picks, "week_one_entries": week_one_entries}


def _pick_belongs_to(pick: dict[str, Any], league: LeagueView) -> bool:
    """Whether one draft pick was made by the roster this report is about.

    ``roster_id`` is the reliable key in a league draft; ``picked_by`` is the
    fallback, and is empty on autopicks (DESIGN_NOTES, "Draft endpoints").
    """
    roster_id = pick.get("roster_id")
    if roster_id is not None and str(roster_id) == str(league.my_roster_id):
        return True
    picked_by = str(pick.get("picked_by") or "")
    return bool(picked_by) and picked_by == league.user_id


# ---------------------------------------------------------------------------
# Sleeper access
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LeagueView:
    """One manager's place in one Sleeper league, resolved from live calls."""

    user_id: str
    league_id: str
    rosters: list[dict[str, Any]]
    my_roster: dict[str, Any]

    @property
    def my_roster_id(self) -> int:
        """The caller's ``roster_id``; ``0`` when Sleeper omitted it (never seen)."""
        try:
            return int(self.my_roster.get("roster_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    @property
    def my_player_ids(self) -> list[str]:
        """Every player on the caller's roster, starters first, deduplicated."""
        return _dedupe(
            [*(self.my_roster.get("starters") or []), *(self.my_roster.get("players") or [])]
        )

    @property
    def all_player_ids(self) -> list[str]:
        """Every player rostered anywhere in the league."""
        ids: list[Any] = []
        for entry in self.rosters:
            ids.extend(entry.get("players") or [])
            ids.extend(entry.get("starters") or [])
        return _dedupe(ids)

    def is_starter(self, player_id: str) -> bool:
        """Whether ``player_id`` is in the caller's current starting lineup."""
        return player_id in {str(s) for s in self.my_roster.get("starters") or []}


#: Every lookup failure below is raised inside a paid handler — after verify and
#: before settle — so no USDC moved. Saying so is not decoration: the wallet has
#: already asked the caller to sign, and an error that is silent about the money
#: reads as a charge for nothing. ``sleeper_client``'s 502 has said this since it
#: was written; the 404s shared the posture and none of the words.
NOT_CHARGED = "You were not charged."


def _lookup_failed(status_code: int, detail: str) -> HTTPException:
    """A pre-settle client-side failure, with the refund posture spelled out."""
    return HTTPException(status_code=status_code, detail=f"{detail} {NOT_CHARGED}")


@asynccontextmanager
async def sleeper_client(settings: Settings) -> AsyncIterator[SleeperClient]:
    """Yield a Sleeper client, mapping its failures onto clean HTTP statuses.

    ``404`` for a resource that does not exist, ``502`` for an upstream outage.
    Both are non-2xx, so nothing settles and the caller is not charged for a
    request Sleeper could not answer.
    """
    client = SleeperClient(settings)
    try:
        yield client
    except SleeperNotFound as exc:
        logger.info("sleeper resource not found: %s", exc)
        raise _lookup_failed(404, "That Sleeper user, league or roster does not exist.") from exc
    except SleeperError as exc:
        logger.warning("sleeper unavailable: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Sleeper is not responding right now. You were not charged. Try again shortly.",
        ) from exc
    finally:
        await client.aclose()


async def load_league(
    client: SleeperClient, username: str, league_id: str | None, season: int
) -> LeagueView:
    """Resolve username -> user -> league -> the caller's roster.

    Args:
        client: Live Sleeper client.
        username: Sleeper username as typed by the caller.
        league_id: Specific league to analyse; the first NFL league of the season
            is used when omitted (matches the request-model contract).
        season: NFL season year.

    Raises:
        HTTPException: ``404`` when the user, the league, or the caller's roster
            in that league cannot be found.
    """
    try:
        user = await client.get_user(username)
    except SleeperNotFound as exc:
        raise _lookup_failed(404, f"No Sleeper user named {username!r}.") from exc

    user_id = str(user.get("user_id") or "")
    if not user_id:
        raise _lookup_failed(404, f"No Sleeper user named {username!r}.")

    leagues = await client.get_leagues(user_id, season)
    if league_id:
        chosen = next((lg for lg in leagues if str(lg.get("league_id")) == str(league_id)), None)
        if chosen is None:
            raise _lookup_failed(
                404, f"{username!r} is not in league {league_id!r} for the {season} season."
            )
    elif leagues:
        chosen = leagues[0]
    else:
        raise _lookup_failed(404, f"{username!r} has no NFL leagues for the {season} season.")

    resolved_league_id = str(chosen.get("league_id") or league_id or "")
    rosters = await client.get_rosters(resolved_league_id)
    mine = next((r for r in rosters if _owns(r, user_id)), None)
    if mine is None:
        raise _lookup_failed(404, f"{username!r} has no roster in league {resolved_league_id!r}.")
    return LeagueView(
        user_id=user_id, league_id=resolved_league_id, rosters=list(rosters), my_roster=dict(mine)
    )


async def league_users(client: SleeperClient, league_id: str) -> list[dict[str, Any]]:
    """Return league members for team labels, degrading to ``[]`` on failure.

    Display names are cosmetic: losing them costs a nicer narration, not the
    report, so this never turns a paid call into an error.
    """
    try:
        return await client.get_users(league_id)
    except SleeperError as exc:
        logger.warning("could not load league users for %s: %s", league_id, exc)
        return []


async def load_matchups(
    client: SleeperClient, league_id: str, through_week: int
) -> dict[int, list[dict[str, Any]]]:
    """Fetch weeks ``1..through_week`` of matchups concurrently.

    A single failed week is dropped with a warning — a report over eight of nine
    weeks is still worth what was paid for it. If *every* week fails the first
    error propagates, so a total Sleeper outage surfaces as a ``502`` rather than
    as a report that quietly says "no matchup history".
    """
    weeks = list(range(1, max(0, through_week) + 1))
    if not weeks:
        return {}

    results = await asyncio.gather(
        *(client.get_matchups(league_id, week) for week in weeks), return_exceptions=True
    )
    out: dict[int, list[dict[str, Any]]] = {}
    errors: list[BaseException] = []
    for week, result in zip(weeks, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(result)
            logger.warning("sleeper matchups failed for week %s: %s", week, result)
            continue
        entries = [entry for entry in result if isinstance(entry, dict)]
        if entries:
            out[week] = entries
    if errors and not out:
        raise errors[0]
    return out


# ---------------------------------------------------------------------------
# Player-document projections
# ---------------------------------------------------------------------------


async def player_docs(store: Store, player_ids: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """Fetch ``players/{id}`` for every id, concurrently, skipping unknowns.

    Reads are chunked rather than fired all at once so a 250-player league does
    not open 250 simultaneous Firestore reads.
    """
    unique = _dedupe(player_ids)
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(unique), _FETCH_CHUNK):
        chunk = unique[start : start + _FETCH_CHUNK]
        docs = await asyncio.gather(*(store.get(PLAYERS_COLLECTION, pid) for pid in chunk))
        for player_id, doc in zip(chunk, docs, strict=True):
            if doc:
                out[player_id] = doc
    return out


def player_lookup(docs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project player documents into the shape ``team_analytics`` expects.

    ``fantasy_positions`` falls back to the primary position: Sleeper's dump
    carries it, our ingested projection may not, and an empty list would make a
    player eligible for no lineup slot and silently deflate optimal lineups.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for player_id, doc in docs.items():
        position = doc.get("position")
        fantasy = doc.get("fantasy_positions") or ([position] if position else [])
        lookup[player_id] = {
            "name": doc.get("name"),
            "position": position,
            "fantasy_positions": list(fantasy),
        }
    return lookup


def roster_entries(league: LeagueView, docs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Render the caller's Sleeper roster as engine ``request_context`` entries."""
    entries: list[dict[str, Any]] = []
    for player_id in league.my_player_ids:
        doc = docs.get(player_id) or {}
        entries.append(
            {
                "player_id": player_id,
                "name": doc.get("name") or player_id,
                "position": doc.get("position"),
                "starter": league.is_starter(player_id),
            }
        )
    return entries


def free_agent_entries(
    player_ids: Sequence[Any], docs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Render free-agent ids as ``{player_id, name, position, team}`` entries.

    Ids with no ingested player document are dropped: we cannot name them, so we
    could never justify recommending them.
    """
    out: list[dict[str, Any]] = []
    for raw in player_ids:
        player_id = str(raw)
        doc = docs.get(player_id)
        if not doc:
            continue
        out.append(
            {
                "player_id": player_id,
                "name": doc.get("name") or player_id,
                "position": doc.get("position"),
                "team": doc.get("team"),
            }
        )
    return out


async def free_agent_universe(store: Store, limit: int = FREE_AGENT_UNIVERSE_LIMIT) -> list[str]:
    """Candidate player ids from which a league's free-agent pool is derived.

    Trending movers first (what managers are actually claiming this cycle), then
    players carrying an ingested usage rollup (players with a real role). See
    :data:`FREE_AGENT_UNIVERSE_LIMIT` for why this is not the whole player DB.
    """
    ids: list[str] = []
    seen: set[str] = set()

    def _push(value: Any) -> None:
        player_id = str(value or "").strip()
        if player_id and player_id not in seen and len(ids) < limit:
            seen.add(player_id)
            ids.append(player_id)

    for kind in ("add", "drop"):
        for entry in await get_trending(store, kind):
            _push(entry.get("player_id"))
    if len(ids) < limit:
        for doc in await store.list(USAGE_TRENDS_COLLECTION, limit=limit):
            _push(doc.get("player_id") or doc.get("_id"))
    return ids


def matchup_player_ids(matchups: dict[int, list[dict[str, Any]]]) -> list[str]:
    """Every player id appearing in any week's matchup entries."""
    ids: list[Any] = []
    for entries in matchups.values():
        for entry in entries:
            ids.extend(entry.get("players") or [])
            ids.extend(entry.get("starters") or [])
            ids.extend((entry.get("players_points") or {}).keys())
    return _dedupe(ids)


def _dedupe(values: Iterable[Any]) -> list[str]:
    """Normalize ids to strings, dropping blanks and Sleeper's ``"0"`` empty slot."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        player_id = str(raw or "").strip()
        if not player_id or player_id in {"0", "None", "null"} or player_id in seen:
            continue
        seen.add(player_id)
        out.append(player_id)
    return out


def _owns(roster: dict[str, Any], user_id: str) -> bool:
    """Whether ``user_id`` owns or co-owns this roster."""
    if str(roster.get("owner_id") or "") == user_id:
        return True
    return any(str(co) == user_id for co in roster.get("co_owners") or [])


# ---------------------------------------------------------------------------
# GET /v1/draft-board
# ---------------------------------------------------------------------------


@router.get(
    "/draft-board",
    response_model=DraftBoardResponse,
    summary="Tiered pre-draft board ranked against the market",
)
async def draft_board(
    limit: int = Query(200, ge=25, le=200, description="How many players to rank."),
    scoring: Literal["ppr"] = Query(
        "ppr", description="Scoring format the board assumes. Only PPR is computed."
    ),
    payment: PaymentContext = Depends(require_payment("draft_board")),
) -> DraftBoardResponse:
    """A tiered draft board with the market's values and reaches called out."""
    body = await cached_analysis(
        "draft_board",
        {"limit": limit, "scoring": scoring},
        # Not week-scoped: a draft board is a preseason artifact and must not
        # cache-miss every Tuesday. A constant keeps one board per season.
        week=0,
        extra=f"limit={limit}:scoring={scoring}",
    )
    return body  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# POST /v1/draft-report
# ---------------------------------------------------------------------------


@router.post(
    "/draft-report",
    response_model=DraftReportResponse,
    summary="Grade a completed fantasy draft",
)
async def draft_report(
    request: DraftReportRequest,
    payment: PaymentContext = Depends(require_payment("draft_report")),
) -> DraftReportResponse:
    """Grade one drafted roster pick by pick against where the market had them."""
    if not (request.draft_id or request.sleeper_username):
        raise HTTPException(
            status_code=400,
            detail="Supply either 'draft_id' or 'sleeper_username'.",
        )

    settings = get_settings()
    store = get_store(settings)
    season = request.season or await current_season(store, settings)

    async with sleeper_client(settings) as client:
        draft_id, picks = await _resolve_draft(client, request, season)

    context = {"season": season, "draft_id": draft_id, "picks": picks}
    body = await fresh_analysis("draft_report", context)
    return body  # type: ignore[return-value]


async def _resolve_draft(
    client: SleeperClient, request: DraftReportRequest, season: int
) -> tuple[str, list[dict[str, Any]]]:
    """Resolve the draft and return **only the requested manager's** picks.

    ``GET /draft/{id}/picks`` returns every pick made by every team. Grading
    that whole list as one roster is not a smaller mistake than grading the
    wrong team — a twelve-team draft would come back with 150 players, an
    A-grade at every position and a Week 1 plan for a roster nobody owns. So an
    identity is required, and the endpoint refuses rather than guessing.

    Identity comes from, in order: an explicit ``draft_slot``; the
    ``sleeper_username``'s own picks; and — only when the draft turns out to
    have a single participant, i.e. a solo mock — the whole board.

    Raises:
        HTTPException: ``404`` when the user, the draft, the manager's seat or
            the picks do not exist; ``400`` when the draft has several teams and
            nothing identifies which one to grade.
    """
    user_id: str | None = None
    if request.sleeper_username:
        user_id = await _user_id(client, request.sleeper_username)

    draft_id = request.draft_id
    if not draft_id:
        drafts = await client.get_drafts(str(user_id), season) if user_id else []
        if not drafts:
            raise _lookup_failed(
                404, f"{request.sleeper_username!r} has no {season} NFL drafts on Sleeper."
            )
        draft_id = str(drafts[0].get("draft_id") or "")

    picks = await client.get_draft_picks(draft_id)
    if not picks:
        # An empty draft is a client-side mistake, not an empty analysis to
        # sell: non-2xx, so nothing settles.
        raise _lookup_failed(404, f"Sleeper draft {draft_id!r} has no picks yet.")

    mine = await _picks_for_manager(client, draft_id, picks, user_id, request.draft_slot)
    if mine is None:
        raise _lookup_failed(
            400,
            f"Sleeper draft {draft_id!r} has multiple teams. Supply "
            "'sleeper_username' or 'draft_slot' so the report grades one roster.",
        )
    if not mine:
        who = request.sleeper_username or f"slot {request.draft_slot}"
        raise _lookup_failed(404, f"{who} made no picks in draft {draft_id!r}.")
    return draft_id, mine


async def _user_id(client: SleeperClient, username: str) -> str:
    """Resolve a Sleeper username to its user id.

    Raises:
        HTTPException: ``404`` when no such user exists.
    """
    try:
        user = await client.get_user(username)
    except SleeperNotFound as exc:
        raise _lookup_failed(404, f"No Sleeper user named {username!r}.") from exc
    user_id = str(user.get("user_id") or "")
    if not user_id:
        raise _lookup_failed(404, f"No Sleeper user named {username!r}.")
    return user_id


async def _picks_for_manager(
    client: SleeperClient,
    draft_id: str,
    picks: list[dict[str, Any]],
    user_id: str | None,
    draft_slot: int | None,
) -> list[dict[str, Any]] | None:
    """Narrow ``picks`` to one manager. ``None`` means "ambiguous, ask".

    ``draft_slot`` is checked before ``picked_by`` because it is the more
    reliable field: an autopicked or commissioner-made selection can carry an
    empty ``picked_by`` while still sitting in the right seat. When a username
    matches no picks for that reason, the draft's own ``draft_order`` map is
    consulted to find their seat before giving up.
    """
    if draft_slot is not None:
        return [p for p in picks if _as_int(p.get("draft_slot")) == draft_slot]

    if user_id:
        mine = [p for p in picks if str(p.get("picked_by") or "") == user_id]
        if mine:
            return mine
        order = (await client.get_draft(draft_id)).get("draft_order") or {}
        slot = _as_int(order.get(user_id))
        return [p for p in picks if _as_int(p.get("draft_slot")) == slot] if slot else []

    # No identity given. Only safe when the draft has exactly one participant.
    seats = {_as_int(p.get("draft_slot")) for p in picks}
    pickers = {str(p.get("picked_by") or "") for p in picks}
    if len(seats) <= 1 and len(pickers) <= 1:
        return picks
    return None


def _as_int(value: Any) -> int | None:
    """Coerce a Sleeper numeric field, which may be a string or absent."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
