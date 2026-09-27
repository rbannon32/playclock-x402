"""Warm ``response_cache`` for the endpoints that take no user input.

Why this exists
---------------
The ADK pipeline costs ~72s per run against a <20s p95 target, and settlement
happens *before* the handler returns — so a slow endpoint is not merely slow, it
is a payer waiting a minute for something they have already been charged for.

Four paid endpoints do not depend on anything the caller supplies: ``trending``,
``sleepers``, ``waivers`` and ``report`` are league-wide boards, identical for
every buyer in a given week. There is no reason to generate them inside a paid
request at all. This task generates them on a schedule, where a minute costs
nothing, and writes them into the same ``response_cache`` the routes read — so
the paid call becomes a Firestore read and returns in milliseconds.

Personalized endpoints (``player``, ``matchup``, ``roster``, ``team_report``)
are deliberately untouched: their bodies depend on who is asking.

Key compatibility is the whole trick
------------------------------------
A warmed entry is only useful if the route looks for the *same* key. Each entry
here mirrors exactly what the corresponding route in :mod:`api.routes.paid`
computes — endpoint, week, and the ``extra`` discriminator built from its query
parameters. :data:`WARM_TARGETS` therefore duplicates the routes' **default**
query values, and :func:`test_precompute_targets_match_route_defaults` fails if
the two ever drift.

Only the defaults are warmed. Under ``ENGINE=adk``, a caller passing
``?limit=40`` receives an unbilled ``503`` until that exact key is precomputed:
the route never starts an unbounded live generation after payment verification.
Deterministic and narrated configurations can generate the rare variant live,
so warming the cartesian product of every parameter would cost more than it
saves.

Scheduling
----------
Run it *after* the data tasks, or it will warm a board built from yesterday's
numbers::

    uv run python -m ingest.job --task nightly
    uv run python -m ingest.job --task stats
    uv run python -m ingest.job --task trending
    uv run python -m ingest.job --task precompute

Then run it **more often than the shortest TTL it warms**. A warmed entry is not
permanent: ``CACHE_TTL_SECONDS`` expires ``trending`` after 6h and the other
boards after 12h, so a task that only ran on the stats schedule would leave most
of the week cold. Under ``ENGINE=adk`` the route returns an unbilled ``503`` during that
gap rather than attempting a request that could outlast its deadline.

Waiting for expiry is not enough either, because regenerating takes minutes:
between the entry dying and the next run finishing, ADK callers cannot receive
the board. So this task refreshes *ahead* of expiry — see
:data:`REFRESH_WINDOW_SECONDS`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.agents.engine import (
    RESPONSE_MODELS,
    AnalysisEngine,
    build_engine,
    get_engine,
    primary_engine,
)
from api.core.config import Settings
from api.core.store import Store
from api.core.week import current_week
from api.data.cache import ResponseCache, cache_key
from api.data.predictions import archive_predictions
from api.data.sources import resolve_sources
from api.evals.quality import check_board_quality
from api.routes import CACHE_TTL_SECONDS
from api.routes.paid import missing_datasets, stale_season
from ingest.judge import Judge, judge_board

logger = logging.getLogger(__name__)

#: Where each warmed board's quality record lives, keyed like the cache.
QUALITY_COLLECTION = "quality"

#: Signature of the source resolver, so tests can inject one with no network.
SourceResolver = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]


@dataclass(frozen=True)
class WarmTarget:
    """One cache entry to generate.

    Attributes:
        endpoint_key: Endpoint to generate, e.g. ``"sleepers"``.
        params: Query parameters at their route defaults. These go into the
            engine's request context *and* into the cache key, exactly as the
            route does it.
        extra_fields: Which of ``params`` appear in the key's ``extra``
            discriminator, in the order the route writes them. Order matters:
            ``"lookback=24:limit=25"`` and ``"limit=25:lookback=24"`` are
            different documents.
        fixed_week: Cache-key week for an endpoint that is not week-scoped.
            ``draft_board`` is a preseason artifact and its route caches under
            week 0 so one board serves the whole draft season; warming it under
            the live week would write a document no route ever reads.
        engine: Engine to warm this target with when the job runs the ADK
            pipeline (``ENGINE=adk``); ``None`` means the job's engine. The
            draft board is ``narrated``: the synthesizer was asked for 200 rows
            and wrote 30, with value deltas that did not add up, so the board is
            computed by the deterministic engine and the model writes only the
            tier labels, the notes and the reasoning, each checked against the
            body. Ignored under ``ENGINE=deterministic``, where every board is
            computed anyway.
    """

    endpoint_key: str
    params: dict[str, Any] = field(default_factory=dict)
    extra_fields: tuple[str, ...] = ()
    fixed_week: int | None = None
    engine: str | None = None

    def week_for(self, week: int) -> int:
        """The week this target's cache key uses."""
        return self.fixed_week if self.fixed_week is not None else week

    def context(self, week: int) -> dict[str, Any]:
        """Build the engine request context for ``week``."""
        return {"week": self.week_for(week), **self.params}


#: The four endpoints whose bodies depend only on the week.
#:
#: Values mirror the route signatures in :mod:`api.routes.paid`; a test asserts
#: they stay in step. ``report`` takes no parameters, so its key has no extra.
WARM_TARGETS: tuple[WarmTarget, ...] = (
    WarmTarget("trending", {"lookback_hours": 24, "limit": 25}, ("lookback_hours", "limit")),
    WarmTarget("sleepers", {"limit": 12}, ("limit",)),
    WarmTarget("waivers", {"limit": 15}, ("limit",)),
    WarmTarget("report"),
    # Season-scoped, not week-scoped: the route caches it under week 0 so one
    # board serves the whole draft season (api/routes/paid.py).
    WarmTarget(
        "draft_board",
        {"limit": 200, "scoring": "ppr"},
        ("limit", "scoring"),
        fixed_week=0,
        engine="narrated",
    ),
)

#: ``extra`` uses the route's query-parameter *name*, which is not always the
#: context key: trending's ``lookback_hours`` is written ``lookback=24``.
EXTRA_ALIASES = {"lookback_hours": "lookback"}

#: How many times to attempt one board before giving up on it.
#:
#: Vertex answers 429 RESOURCE_EXHAUSTED when several boards run back to back —
#: each one now issues two concurrent calls from its ParallelAgent, and four
#: boards in a row is enough to trip the quota. Observed live on the first real
#: run: three boards warmed, ``report`` died on a 429.
#:
#: A paid request cannot wait this out, which is precisely why this work is here
#: instead of in the request path. Nothing is waiting on this job, so it can
#: afford to be patient where a caller could not.
#:
#: This is the *outer* retry, and it re-runs the whole pipeline — every tool
#: call the stats agent had already finished is bought again. The first line
#: of defence is therefore the per-request retry inside the SDK
#: (:mod:`api.agents.vertex`, ``MODEL_RETRY_ATTEMPTS``), which keeps that
#: work; this loop is for the run that exhausts it.
MAX_ATTEMPTS = 3

#: Seconds to wait before each retry. Long on purpose: a per-minute quota needs
#: to actually roll over, and backing off for two minutes costs nothing here.
RETRY_BACKOFF_SECONDS = (30.0, 90.0)

#: Regenerate an entry with less than this much life left, instead of waiting
#: for it to expire.
#:
#: Skipping only *expired* entries sounds equivalent and is not. An entry that
#: dies at 15:40 with the next run at 16:00 leaves a gap, and the run that
#: notices does not refill it quickly — ``report`` took 988s live. Every caller
#: in that window pays the slow path this whole module exists to remove.
#:
#: The entry has to survive until its *replacement is written*, so the window
#: must exceed **the scheduler interval plus that board's own generation time**.
#: Against the 2-hourly cadence in ``infra/deploy.md`` §5 and the slowest board
#: measured (``report``, 988s), 2.5h leaves ~13 minutes of margin. It must also
#: stay *below* the shortest TTL, or every run would regenerate everything and
#: the only visible symptom would be the Vertex bill; a test pins that.
REFRESH_WINDOW_SECONDS = 2.5 * 3600

#: The most one board may take, every attempt and backoff included, before it
#: is abandoned as that board's failure (left cold, so the run still exits
#: non-zero). Without it, three slow ``report`` attempts plus backoff can
#: outlast the job's 45m task timeout, and Cloud Run kills the process before
#: ``draft_board`` is attempted and before the summary or the
#: :class:`PrecomputeError` is ever logged — the one failure with no record.
#:
#: Bounded on both sides, and a test pins it: above the slowest measured board
#: (``report``, 988s) with room for a Vertex backoff, and small enough that the
#: 2h scheduler interval plus this budget stays inside
#: :data:`REFRESH_WINDOW_SECONDS` (2h + 1500s = 2h25m < 2.5h).
BOARD_BUDGET_SECONDS = 1500.0


class PrecomputeError(RuntimeError):
    """Raised when a run finished without warming everything it was asked to.

    The exit code is the only alert: ``ingest/job.py`` maps a raised task to a
    non-zero exit, which is what Cloud Monitoring watches (tech spec §7).
    Returning a summary that merely *counts* failures would exit zero, and a
    board silently left cold is exactly the failure nothing else reports.

    Attributes:
        summary: The same dict a successful run returns, so a caller that
            catches this still gets the per-target detail.
    """

    def __init__(self, message: str, summary: dict[str, Any]) -> None:
        super().__init__(message)
        self.summary = summary


def render_extra(target: WarmTarget) -> str:
    """Render ``target``'s key discriminator, applying :data:`EXTRA_ALIASES`."""
    return ":".join(
        f"{EXTRA_ALIASES.get(name, name)}={target.params[name]}" for name in target.extra_fields
    )


async def warm_response_cache(
    store: Store,
    settings: Settings,
    *,
    week: int | None = None,
    targets: tuple[WarmTarget, ...] = WARM_TARGETS,
    force: bool = False,
    refresh_window: float = REFRESH_WINDOW_SECONDS,
    judge: Judge | None = None,
    source_resolver: SourceResolver = resolve_sources,
    engine_factory: Callable[[str], AnalysisEngine] | None = None,
    board_budget: float = BOARD_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Generate and cache every league-wide board for ``week``.

    Args:
        store: Backing store; the cache and the engine both read through it.
        settings: Active settings. ``ENGINE`` decides whether this does real LLM
            work or deterministic assembly — both are worth warming.
        week: Week to warm. Defaults to the resolved current week.
        targets: What to warm. Overridable for tests and partial reruns.
        force: Regenerate every target, however much life it has left. Off by
            default so a re-run after a partial failure is cheap.
        refresh_window: Regenerate an entry with fewer than this many seconds
            left (:data:`REFRESH_WINDOW_SECONDS`).
        judge: Optional LLM judge (:mod:`ingest.judge`) run on every warmed
            board. ``None`` skips the rubric score; the rule-based checks in
            :mod:`api.evals.quality` always run.
        source_resolver: Turns grounding-redirect sources into real URLs and
            headlines (:mod:`api.data.sources`). Injected so tests need no network.
        engine_factory: Builds the engine a target names in ``WarmTarget.engine``
            (only consulted under ``ENGINE=adk``). Defaults to
            :func:`api.agents.engine.build_engine`; injected so tests never
            construct a narrator.
        board_budget: Seconds one board may spend generating, retries and
            backoff included (:data:`BOARD_BUDGET_SECONDS`). Past it the board
            counts as failed.

    Returns:
        A summary dict: counts plus per-target status and elapsed seconds.

    Raises:
        PrecomputeError: If any target ended ``failed`` or ``blocked``. Raised
            once, after every target has been attempted — one bad board must not
            cost the others.
    """
    resolved = week if week is not None else await current_week(store, settings)

    # One check for the whole run rather than per target: a store filled for the
    # wrong season is wrong for every board, and warming even one of them caches
    # last season's answer under the key the route reads, where it is served as
    # a hit for its whole TTL. Raising here trips the ingest-failure alert,
    # which is the only thing that would ever notice.
    wrong = await stale_season(store, settings)
    if wrong:
        found, expected = wrong
        summary = {
            "week": resolved,
            "warmed": 0,
            "skipped": 0,
            "failed": 0,
            "blocked": len(targets),
            "targets": [
                {"key": t.endpoint_key, "status": "blocked", "season": found} for t in targets
            ],
        }
        raise PrecomputeError(
            f"ingested data is for the {found} season, expected {expected}; "
            "refusing to warm boards from the wrong year",
            summary,
        )
    cache = ResponseCache(store)
    # Deliberately NOT the fallback wrapper. `get_engine` returns
    # FallbackAnalysisEngine under the production pairing (ENGINE=adk +
    # ENGINE_FALLBACK=true), which turns an ADK failure into a *successful*
    # deterministic body — and this task's whole job is to cache what it gets.
    # A transient 429 during warming would then be cached for 6-12 hours and
    # billed to every caller at LLM prices, with a single line in the logs.
    #
    # The retry loop below is the right response to a transient failure here
    # precisely because nothing is waiting: it can afford the minutes a paid
    # request cannot. A board that still fails is left cold and raises, which
    # is loud.
    engine = primary_engine(get_engine(settings=settings, store=store))
    factory = engine_factory or (lambda name: build_engine(settings, store, name))
    overrides: dict[str, AnalysisEngine] = {}

    def engine_for(target: WarmTarget) -> AnalysisEngine:
        """The engine this target warms with (see ``WarmTarget.engine``)."""
        if not target.engine or settings.engine != "adk":
            return engine
        if target.engine not in overrides:
            overrides[target.engine] = factory(target.engine)
        return overrides[target.engine]

    results: list[dict[str, Any]] = []
    warmed = skipped = failed = blocked = flagged = 0

    for target in targets:
        # Not `resolved`: a target may pin its own week (see WarmTarget.fixed_week).
        target_week = target.week_for(resolved)
        key = cache_key(target.endpoint_key, target_week, render_extra(target))
        started = time.monotonic()

        if not force:
            remaining = await cache.remaining_ttl(key)
            if remaining is not None and remaining > refresh_window:
                skipped += 1
                results.append(
                    {"key": key, "status": "already-warm", "expires_in": round(remaining)}
                )
                logger.info("precompute skip %s (warm for another %.0fs)", key, remaining)
                continue

        # The route refuses to *sell* a board built from data that is not there
        # (503, unbilled). Warming bypasses the route, so without this check a
        # cold store gets a schema-valid empty board cached under the key the
        # route reads — and ``cached_analysis`` returns a hit before it ever
        # reaches its readiness check. Every caller for the next 6 hours would
        # then be charged for the emptiness the 503 exists to prevent.
        absent = await missing_datasets(store, target.endpoint_key)
        if absent:
            blocked += 1
            results.append({"key": key, "status": "blocked", "missing": absent})
            logger.error(
                "precompute cannot warm %s: ingest has not written %s",
                key,
                ", ".join(absent),
            )
            continue

        async def generate(target: WarmTarget = target, key: str = key) -> Any:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    return await engine_for(target).analyze(
                        target.endpoint_key, target.context(resolved)
                    )
                except Exception:
                    if attempt == MAX_ATTEMPTS:
                        # One bad board must not cost the others. The endpoint
                        # stays live-generating, which is its behaviour anyway.
                        logger.exception(
                            "precompute gave up on %s after %d attempts", key, MAX_ATTEMPTS
                        )
                        return None
                    pause = RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                    logger.warning(
                        "precompute attempt %d/%d failed for %s; retrying in %.0fs",
                        attempt,
                        MAX_ATTEMPTS,
                        key,
                        pause,
                        exc_info=True,
                    )
                    await asyncio.sleep(pause)
            return None

        try:
            body = await asyncio.wait_for(generate(), timeout=board_budget)
        except TimeoutError:
            # Same outcome as exhausting the attempts: this board is left cold
            # and the run raises at the end, but the boards after it still get
            # their turn inside the job's task timeout.
            logger.error("precompute abandoned %s after its %.0fs budget", key, board_budget)
            body = None

        if body is None:
            failed += 1
            elapsed = time.monotonic() - started
            results.append({"key": key, "status": "failed", "seconds": round(elapsed, 1)})
            continue

        try:
            # Validate before caching: a body the route cannot parse would be
            # evicted on read anyway, and writing it would hide the real failure.
            model = RESPONSE_MODELS[target.endpoint_key]
            payload = model.model_validate(body.model_dump(mode="json")).model_dump(mode="json")

            # The research agent's citations arrive as opaque grounding redirects
            # with a bare domain for a title. Resolve them here, where latency is
            # free, so the cached body carries a headline a reader can check.
            # A resolver failure keeps the unresolved list: the warm is not at risk.
            if payload.get("sources"):
                try:
                    payload["sources"] = await source_resolver(list(payload["sources"]))
                    payload = model.model_validate(payload).model_dump(mode="json")
                except Exception:  # noqa: BLE001 - never lose a board over a citation
                    logger.warning("source resolution failed for %s", key, exc_info=True)

            await cache.set(
                key,
                payload,
                CACHE_TTL_SECONDS[target.endpoint_key],
                endpoint=target.endpoint_key,
                week=target_week,
            )
            await archive_predictions(
                store, target.endpoint_key, payload, week=target_week, settings=settings
            )
        except Exception:  # noqa: BLE001 - one bad board must not cost the others
            # A body that fails validation, or a transient store error on the
            # write, would otherwise escape the loop and leave every later
            # target cold too.
            logger.exception("precompute could not cache %s", key)
            failed += 1
            elapsed = time.monotonic() - started
            results.append({"key": key, "status": "failed", "seconds": round(elapsed, 1)})
            continue

        # The value gate. This is the only place the ADK output that payers
        # actually receive is inspected: the golden suite runs against a
        # fixture, and the route serves whatever is cached. A failing board
        # is still served — a cold board 503s every caller, which is worse
        # than a mediocre one — but it is recorded and logged loudly, and
        # `flagged` in the summary is what the deploy runbook alerts on.
        quality = await _assess_board(store, target.endpoint_key, key, target_week, payload, judge)
        if quality["flagged"]:
            flagged += 1
        warmed += 1
        elapsed = time.monotonic() - started
        results.append(
            {
                "key": key,
                "status": "warmed",
                "seconds": round(elapsed, 1),
                "quality": {
                    "checks_failed": len(quality["checks"]),
                    "judge_mean": (quality.get("judge") or {}).get("mean"),
                    "flagged": quality["flagged"],
                },
            }
        )
        logger.info("precompute warmed %s in %.1fs", key, elapsed)

    summary = {
        "week": resolved,
        "warmed": warmed,
        "skipped": skipped,
        "failed": failed,
        "blocked": blocked,
        "flagged": flagged,
        "targets": results,
    }

    if failed or blocked:
        # Log the detail first: the raise below reaches the job runner as an
        # error string, and the per-target breakdown is what an operator needs.
        logger.error("precompute run incomplete", extra={"summary": summary})
        raise PrecomputeError(
            f"{failed + blocked} of {len(targets)} boards left cold "
            f"(failed={failed}, blocked={blocked}) for week {resolved}",
            summary,
        )
    return summary


async def _assess_board(
    store: Store,
    endpoint_key: str,
    key: str,
    week: int,
    payload: dict[str, Any],
    judge: Judge | None,
) -> dict[str, Any]:
    """Run the value checks and the judge on one warmed board; record both.

    Writes ``quality/{key}`` so the score sits next to the body it describes,
    and logs a single ``board quality flagged`` line per bad board. Never
    raises: the board is already cached, and an assessment that could take it
    down would be worse than the board.
    """
    checks: list[str] = []
    verdict: dict[str, Any] | None = None
    try:
        checks = await check_board_quality(store, endpoint_key, payload)
    except Exception:  # noqa: BLE001 - the gate reports, it never blocks
        logger.warning("quality checks failed to run for %s", key, exc_info=True)
    if judge is not None:
        verdict = await judge_board(judge, endpoint_key, payload)
    flagged = bool(checks) or bool(verdict and verdict.get("flagged"))
    record = {
        "key": key,
        "endpoint": endpoint_key,
        "week": week,
        "checked_at": datetime.now(UTC).isoformat(),
        "generated_at": (payload.get("meta") or {}).get("generated_at"),
        "model": (payload.get("meta") or {}).get("model"),
        "checks": checks,
        "judge": verdict,
        "flagged": flagged,
    }
    if checks:
        logger.warning(
            "board quality flagged: %s failed %d value check(s): %s",
            key,
            len(checks),
            "; ".join(checks[:5]),
        )
    try:
        await store.set(QUALITY_COLLECTION, key, record)
    except Exception:  # noqa: BLE001 - a lost record is not a lost board
        logger.warning("could not record quality for %s", key, exc_info=True)
    return record
