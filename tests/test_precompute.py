"""The precompute task: cache-key compatibility, staleness, and failure reporting.

The interesting risk here is not that warming breaks — it is that warming
*silently does nothing*. Three ways it can go quiet, one test group each:

- the key this task writes differs by one character from the key the route
  reads, so every entry is a miss and every paid call still costs 72 seconds;
- the entry expires between two runs, so the schedule looks healthy while most
  of the week is served cold;
- a board fails or has no data behind it, and the run still exits zero.

None of those raise anything on their own. Most of this file exists to make them
loud.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.core.config import Settings
from api.core.store import Store
from api.data.cache import CACHE_COLLECTION, ResponseCache, cache_key
from api.data.stats_store import FRESHNESS_DOC_ID, META_COLLECTION
from api.routes import CACHE_TTL_SECONDS, paid
from ingest import precompute
from ingest.precompute import (
    WARM_TARGETS,
    PrecomputeError,
    WarmTarget,
    render_extra,
    warm_response_cache,
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep retry tests instant. The real waits are minutes by design."""
    monkeypatch.setattr(precompute, "RETRY_BACKOFF_SECONDS", (0.0,))


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="deterministic",
        x402_mode="disabled",
        season=2026,
        week_override=4,
    )


@pytest.fixture
async def ingested(store: Store) -> Store:
    """A store whose ``meta/freshness`` claims every dataset the boards read.

    Warming refuses to run against a store ingest has not filled, so every
    behaviour test below needs this. Tests about that refusal use bare ``store``.
    """
    every = sorted({name for names in paid.REQUIRED_DATASETS.values() for name in names})
    await store.set(META_COLLECTION, FRESHNESS_DOC_ID, {name: _in_seconds(-60) for name in every})
    return store


# -- key compatibility ----------------------------------------------------


def test_every_warm_target_is_a_cacheable_endpoint() -> None:
    """Warming an endpoint the routes never cache would write dead documents."""
    for target in WARM_TARGETS:
        assert target.endpoint_key in CACHE_TTL_SECONDS


def test_warm_targets_cover_exactly_the_cacheable_endpoints() -> None:
    """If a new cached endpoint appears, it should be warmed or consciously skipped."""
    assert {t.endpoint_key for t in WARM_TARGETS} == set(CACHE_TTL_SECONDS)


@pytest.mark.parametrize("target", WARM_TARGETS, ids=lambda t: t.endpoint_key)
def test_precompute_params_match_route_defaults(target: WarmTarget) -> None:
    """The warmed body must be the one an unparameterized caller asks for.

    Route defaults live in the endpoint signatures as ``Query(default, ...)``.
    A default changed there without changing WARM_TARGETS would leave the warm
    entry keyed to a body nobody requests.
    """
    route_fn = getattr(paid, target.endpoint_key)
    signature = inspect.signature(route_fn)
    for name, value in target.params.items():
        assert name in signature.parameters, f"{target.endpoint_key} has no {name} parameter"
        assert signature.parameters[name].default.default == value, (
            f"{target.endpoint_key}.{name} default drifted from WARM_TARGETS"
        )


def test_rendered_keys_match_the_routes_exactly() -> None:
    """Pinned against the literals in api/routes/paid.py.

    These strings are the contract between this task and the routes; they are
    written out rather than computed so a change to either side fails here.
    """
    rendered = {
        t.endpoint_key: cache_key(t.endpoint_key, t.week_for(4), render_extra(t))
        for t in WARM_TARGETS
    }
    assert rendered == {
        "trending": "trending:w4:lookback=24:limit=25",
        "sleepers": "sleepers:w4:limit=12",
        "waivers": "waivers:w4:limit=15",
        "report": "report:w4",
        # w0, not w4: the draft board is season-scoped and its route caches
        # under a constant week so one board serves the whole draft season.
        "draft_board": "draft_board:w0:limit=200:scoring=ppr",
    }


# -- behaviour ------------------------------------------------------------


async def test_warming_makes_the_route_key_a_hit(ingested: Store, settings: Settings) -> None:
    """The whole point: after warming, the key the route reads is populated."""
    result = await warm_response_cache(ingested, settings, week=4)

    assert result["warmed"] == len(WARM_TARGETS)
    assert result["failed"] == 0

    cache = ResponseCache(ingested)
    for target in WARM_TARGETS:
        key = cache_key(target.endpoint_key, target.week_for(4), render_extra(target))
        assert await cache.get(key) is not None, f"{key} was not warmed"


async def test_rerun_skips_already_warm_entries(ingested: Store, settings: Settings) -> None:
    """A retry after a partial failure should be cheap, not a full regeneration."""
    await warm_response_cache(ingested, settings, week=4)
    again = await warm_response_cache(ingested, settings, week=4)

    assert again["warmed"] == 0
    assert again["skipped"] == len(WARM_TARGETS)


async def test_force_regenerates(ingested: Store, settings: Settings) -> None:
    await warm_response_cache(ingested, settings, week=4)
    forced = await warm_response_cache(ingested, settings, week=4, force=True)

    assert forced["warmed"] == len(WARM_TARGETS)
    assert forced["skipped"] == 0


async def test_one_failing_board_does_not_stop_the_others(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad board leaves that endpoint live-generating; the rest still warm."""
    from api.agents import engine as engine_module

    real = engine_module.get_engine(settings=settings, store=ingested)

    class OneBadBoard:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            if endpoint_key == "waivers":
                raise RuntimeError("upstream exploded")
            return await real.analyze(endpoint_key, context)

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: OneBadBoard())

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(ingested, settings, week=4)

    result = excinfo.value.summary
    assert result["failed"] == 1
    assert result["warmed"] == len(WARM_TARGETS) - 1
    statuses = {r["key"].split(":")[0]: r["status"] for r in result["targets"]}
    assert statuses["waivers"] == "failed"

    cache = ResponseCache(ingested)
    assert await cache.get(cache_key("waivers", 4, "limit=15")) is None
    assert await cache.get(cache_key("report", 4)) is not None


async def test_a_store_error_caching_one_board_does_not_stop_the_others(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write, not only the generation, is inside the per-board guard."""
    real_set = ResponseCache.set

    async def flaky_set(self: ResponseCache, key: str, *args: Any, **kwargs: Any) -> Any:
        if key.startswith("waivers"):
            raise RuntimeError("firestore unavailable")
        return await real_set(self, key, *args, **kwargs)

    monkeypatch.setattr(ResponseCache, "set", flaky_set)

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(ingested, settings, week=4)

    result = excinfo.value.summary
    assert result["failed"] == 1
    assert result["warmed"] == len(WARM_TARGETS) - 1


# -- staleness ------------------------------------------------------------
#
# A schedule that only refills *expired* entries leaves the cache cold from the
# moment one dies until the next run finishes generating — and generation is
# minutes (``sleepers`` took 374s live). So warming happens ahead of expiry.


def test_the_refresh_window_fits_inside_every_ttl() -> None:
    """A window wider than a TTL would regenerate everything on every run.

    The opposite mistake to the one below, and just as quiet: the cache would
    stay warm while the LLM bill went up by the scheduling frequency.
    """
    assert precompute.REFRESH_WINDOW_SECONDS < min(CACHE_TTL_SECONDS.values())


async def test_an_entry_about_to_expire_is_regenerated(ingested: Store, settings: Settings) -> None:
    """The fix for the real bug: don't wait for the gap, refill before it opens."""
    await warm_response_cache(ingested, settings, week=4)

    # Age every entry until it has an hour of life left — still a cache hit for
    # the routes, but too little to survive until the next scheduled run.
    for doc in await ingested.list(CACHE_COLLECTION):
        await ingested.set(
            CACHE_COLLECTION,
            doc["_id"],
            {"expires_at": _in_seconds(3600)},
            merge=True,
        )

    again = await warm_response_cache(ingested, settings, week=4, refresh_window=2.5 * 3600)

    assert again["warmed"] == len(WARM_TARGETS)
    assert again["skipped"] == 0


async def test_an_entry_with_plenty_of_life_left_is_left_alone(
    ingested: Store, settings: Settings
) -> None:
    """Refreshing ahead must not collapse into regenerating on every run."""
    await warm_response_cache(ingested, settings, week=4)

    again = await warm_response_cache(ingested, settings, week=4, refresh_window=60)

    assert again["skipped"] == len(WARM_TARGETS)
    assert all(r["expires_in"] > 0 for r in again["targets"])


# -- readiness ------------------------------------------------------------


async def test_it_refuses_to_warm_a_board_ingest_has_no_data_for(
    store: Store, settings: Settings
) -> None:
    """The expensive one to get wrong: warming bypasses the route's 503.

    On a cold store the deterministic engine happily returns a schema-valid
    *empty* board. Cached under the key the route reads, ``cached_analysis``
    serves it as a hit before ``require_ingested_data`` ever runs — so every
    caller for the next 6 hours is charged for the emptiness the 503 exists to
    refuse. Nothing warmed is the correct outcome; the endpoint 503s instead.
    """
    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(store, settings, week=4)

    result = excinfo.value.summary
    assert result["warmed"] == 0
    assert result["blocked"] == len(WARM_TARGETS)
    assert {"players"} <= set(result["targets"][0]["missing"])

    assert await store.list(CACHE_COLLECTION) == []


async def test_a_partial_ingest_warms_only_what_it_can_answer(
    store: Store, settings: Settings
) -> None:
    """``nightly`` and ``trending`` ran; ``stats`` did not.

    ``meta/freshness`` is non-empty, so an existence check would pass and three
    empty boards would be sold. Only ``trending`` has everything it reads.
    """
    await store.set(
        META_COLLECTION,
        FRESHNESS_DOC_ID,
        {"players": _in_seconds(-60), "trending": _in_seconds(-60)},
    )

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(store, settings, week=4)

    result = excinfo.value.summary
    statuses = {r["key"].split(":")[0]: r["status"] for r in result["targets"]}
    assert statuses == {
        "trending": "warmed",
        "sleepers": "blocked",
        "waivers": "blocked",
        "report": "blocked",
        # The board is ranked from usage, which `stats` never wrote.
        "draft_board": "blocked",
    }

    cache = ResponseCache(store)
    assert await cache.get(cache_key("trending", 4, "lookback=24:limit=25")) is not None
    assert await cache.get(cache_key("sleepers", 4, "limit=12")) is None


# -- retry ----------------------------------------------------------------


async def test_a_transient_failure_is_retried_and_succeeds(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vertex answers 429 when boards run back to back; that is worth waiting out.

    Observed live on the first real run: three boards warmed and ``report`` died
    on a RESOURCE_EXHAUSTED. Nothing is waiting on this job, so it retries where
    a paid request could not.
    """
    from api.agents import engine as engine_module

    real = engine_module.get_engine(settings=settings, store=ingested)
    attempts = {"report": 0}

    class FlakyReport:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            if endpoint_key == "report":
                attempts["report"] += 1
                if attempts["report"] == 1:
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return await real.analyze(endpoint_key, context)

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: FlakyReport())

    result = await warm_response_cache(ingested, settings, week=4)

    assert attempts["report"] == 2
    assert result["failed"] == 0
    assert result["warmed"] == len(WARM_TARGETS)

    cache = ResponseCache(ingested)
    assert await cache.get(cache_key("report", 4)) is not None


async def test_it_gives_up_after_max_attempts(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board that is genuinely broken must not retry forever."""
    calls = {"n": 0}

    class AlwaysBroken:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            calls["n"] += 1
            raise RuntimeError("still broken")

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: AlwaysBroken())

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(ingested, settings, week=4, targets=WARM_TARGETS[:1])

    assert calls["n"] == precompute.MAX_ATTEMPTS
    assert excinfo.value.summary["failed"] == 1
    assert excinfo.value.summary["warmed"] == 0


async def test_a_board_over_its_budget_fails_and_the_rest_still_warm(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow ``report`` must not eat the job's task timeout and every later board."""
    import asyncio

    from api.agents import engine as engine_module

    real = engine_module.get_engine(settings=settings, store=ingested)

    class HangingReport:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            if endpoint_key == "report":
                await asyncio.sleep(3600)
            return await real.analyze(endpoint_key, context)

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: HangingReport())

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(ingested, settings, week=4, board_budget=0.05)

    statuses = {r["key"].split(":")[0]: r["status"] for r in excinfo.value.summary["targets"]}
    assert statuses["report"] == "failed"
    assert statuses["draft_board"] == "warmed"  # after report, still attempted
    assert excinfo.value.summary["failed"] == 1


def test_the_board_budget_fits_the_measured_board_and_the_refresh_window() -> None:
    """Above the slowest measured board (report, 988s); inside the warming invariant."""
    keep_warm_interval = 2 * 3600
    assert precompute.BOARD_BUDGET_SECONDS > 988
    assert keep_warm_interval + precompute.BOARD_BUDGET_SECONDS < precompute.REFRESH_WINDOW_SECONDS
    assert precompute.BOARD_BUDGET_SECONDS < 45 * 60  # the ingest job's task timeout


def _in_seconds(seconds: float) -> str:
    """An ISO-8601 UTC timestamp ``seconds`` from now, as the cache writes it."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


# -- the value gate -------------------------------------------------------
#
# Warming is the only place the boards payers actually receive are inspected.
# Every warmed board gets a ``quality/{key}`` record; nothing in the gate can
# take a board down, and a bad board is loud rather than refused.


class _LowJudge:
    """A judge that hates everything, so ``flagged`` has something to count."""

    name = "low"

    async def score(self, endpoint_key: str, body: dict[str, Any]) -> Any:
        from ingest.judge import JudgeScore  # noqa: PLC0415

        return JudgeScore(
            specificity=1, actionability=1, beyond_the_crowd=1, grounding=2, critique="Bland."
        )


async def test_every_warmed_board_gets_a_quality_record(
    ingested: Store, settings: Settings
) -> None:
    result = await warm_response_cache(ingested, settings, week=4)
    assert result["flagged"] == 0
    for entry in result["targets"]:
        record = await ingested.get(precompute.QUALITY_COLLECTION, entry["key"])
        assert record is not None, entry["key"]
        assert record["checks"] == []
        assert record["judge"] is None
        assert record["flagged"] is False
        assert entry["quality"] == {"checks_failed": 0, "judge_mean": None, "flagged": False}


async def test_the_judge_score_is_recorded_and_flags_the_run(
    ingested: Store, settings: Settings
) -> None:
    result = await warm_response_cache(ingested, settings, week=4, judge=_LowJudge())
    assert result["flagged"] == len(WARM_TARGETS)
    assert result["warmed"] == len(WARM_TARGETS), "a flagged board is still served"
    record = await ingested.get(precompute.QUALITY_COLLECTION, result["targets"][0]["key"])
    assert record is not None
    assert record["judge"]["mean"] == 1.25
    assert record["judge"]["judge"] == "low"
    assert record["flagged"] is True


async def test_grounding_redirects_are_resolved_before_caching(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached body carries the headline, not the opaque redirect."""
    from api.agents import engine as engine_module
    from api.schemas import SourceRef

    real = engine_module.get_engine(settings=settings, store=ingested)
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ"

    class WithASource:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            body = await real.analyze(endpoint_key, context)
            if endpoint_key == "report":
                body.sources = [SourceRef(title="nfl.com", url=redirect, published="today")]
            return body

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: WithASource())
    seen: list[list[dict[str, Any]]] = []

    async def resolver(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen.append(sources)
        return [
            {**s, "title": "Lions place Pacheco on IR", "url": "https://www.nfl.com/news/x"}
            for s in sources
        ]

    await warm_response_cache(ingested, settings, week=4, source_resolver=resolver)

    assert len(seen) == 1 and seen[0][0]["url"] == redirect
    cached = await ResponseCache(ingested).get(cache_key("report", 4))
    assert cached is not None
    assert cached["sources"] == [
        {
            "title": "Lions place Pacheco on IR",
            "url": "https://www.nfl.com/news/x",
            "published": "today",
        }
    ]
    record = await ingested.get(precompute.QUALITY_COLLECTION, cache_key("report", 4))
    assert record is not None and not any("redirect" in c for c in record["checks"])


async def test_a_broken_resolver_keeps_the_unresolved_sources(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.agents import engine as engine_module
    from api.schemas import SourceRef

    real = engine_module.get_engine(settings=settings, store=ingested)
    redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ"

    class WithASource:
        async def analyze(self, endpoint_key: str, context: dict[str, Any]) -> Any:
            body = await real.analyze(endpoint_key, context)
            if endpoint_key == "report":
                body.sources = [SourceRef(title="nfl.com", url=redirect)]
            return body

    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: WithASource())

    async def broken(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise RuntimeError("dns down")

    result = await warm_response_cache(ingested, settings, week=4, source_resolver=broken)
    assert result["warmed"] == len(WARM_TARGETS)
    cached = await ResponseCache(ingested).get(cache_key("report", 4))
    assert cached is not None and cached["sources"][0]["url"] == redirect
    # ...and the value gate says so, which is the point of keeping it visible.
    record = await ingested.get(precompute.QUALITY_COLLECTION, cache_key("report", 4))
    assert record is not None and record["flagged"] is True
    assert any("redirect" in c for c in record["checks"])


async def test_the_gate_itself_failing_never_blocks_a_warm(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("store hiccup")

    monkeypatch.setattr("ingest.precompute.check_board_quality", explode)
    result = await warm_response_cache(ingested, settings, week=4)
    assert result["warmed"] == len(WARM_TARGETS)
    assert result["flagged"] == 0


# -- per-target engines -----------------------------------------------------


def test_the_draft_board_is_the_only_target_with_its_own_engine() -> None:
    """The synthesizer wrote 30 of 200 rows; the computed board is the product."""
    engines = {t.endpoint_key: t.engine for t in WARM_TARGETS}
    assert engines["draft_board"] == "narrated"
    assert all(engine is None for key, engine in engines.items() if key != "draft_board")


async def test_the_target_engine_is_used_only_under_adk(
    ingested: Store, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.agents import engine as engine_module

    real = engine_module.get_engine(settings=settings, store=ingested)
    asked: list[str] = []

    def factory(name: str) -> Any:
        asked.append(name)
        return real

    # ENGINE=deterministic: every board is computed anyway; the override is inert.
    await warm_response_cache(ingested, settings, week=4, engine_factory=factory)
    assert asked == []

    # ENGINE=adk: the draft board asks for its narrated engine, exactly once.
    adk = settings.model_copy(update={"engine": "adk"})
    monkeypatch.setattr("ingest.precompute.get_engine", lambda **_: real)
    await warm_response_cache(ingested, adk, week=4, force=True, engine_factory=factory)
    assert asked == ["narrated"]
