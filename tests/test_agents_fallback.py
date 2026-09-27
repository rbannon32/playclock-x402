"""A degraded answer instead of an outage, when the ADK pipeline fails.

Vertex answers 429 whenever calls bunch up — `ingest/precompute.py` already
treats that as normal and waits minutes between attempts, which a paid request
cannot afford. Advertised traffic produces exactly those bursts, so without a
fallback the endpoints are down at the moment they are busiest.

The trade is real and worth stating in a test file: a 500 settles nothing,
while a fallback answer is billed. These tests pin the two things that make it
defensible — the fallback is a genuine grounded answer, and it says what it is.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.agents.deterministic import DeterministicAnalysisEngine
from api.agents.engine import (
    AnalysisEngine,
    EngineError,
    FallbackAnalysisEngine,
    get_engine,
    set_engine,
)
from api.core.config import Settings
from api.core.store import MemoryStore, Store
from api.evals.golden import SEASON, WEEK, seed_store
from api.schemas import AnalysisResponse, TrendingResponse

CONTEXT = {"week": WEEK, "season": SEASON}


def settings_for(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "store_backend": "memory",
        "engine": "deterministic",
        "x402_mode": "disabled",
        "season": SEASON,
        "week_override": WEEK,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


class Exploding(AnalysisEngine):
    """A primary engine that always fails, the way ADK does after its retries."""

    name = "exploding"

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error or EngineError("pipeline failed after 2 attempts")
        self.closed = False

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        self.calls += 1
        raise self.error

    async def aclose(self) -> None:
        self.closed = True


class Counting(AnalysisEngine):
    """A fallback that records whether it was reached."""

    name = "counting"

    def __init__(self, inner: AnalysisEngine) -> None:
        self.inner = inner
        self.calls = 0
        self.closed = False

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        self.calls += 1
        return await self.inner.analyze(endpoint_key, request_context)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
async def deterministic(store: Store) -> DeterministicAnalysisEngine:
    await seed_store(store)
    return DeterministicAnalysisEngine(store=store, settings=settings_for())


async def test_a_failed_primary_still_answers(deterministic: DeterministicAnalysisEngine) -> None:
    primary = Exploding()
    engine = FallbackAnalysisEngine(primary=primary, fallback=deterministic)

    body = await engine.analyze("trending", dict(CONTEXT))

    assert isinstance(body, TrendingResponse)
    assert body.verdict
    assert primary.calls == 1


async def test_the_fallback_body_says_what_it_is(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    """A caller paid for this. They are owed a way to tell what they got."""
    engine = FallbackAnalysisEngine(primary=Exploding(), fallback=deterministic)

    body = await engine.analyze("trending", dict(CONTEXT))

    assert body.meta.model is None, "no model ran, so none may be claimed"
    assert body.reasoning.strip(), "a fallback still owes the caller a real answer"


async def test_every_cited_number_is_still_real(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    """Degraded must not mean invented — the #1 quality rule still holds."""
    from api.evals.golden import check_citations_traceable

    engine = FallbackAnalysisEngine(primary=Exploding(), fallback=deterministic)
    body = await engine.analyze("trending", dict(CONTEXT))

    assert check_citations_traceable(body.stats_cited, CONTEXT) == []


async def test_a_working_primary_is_never_second_guessed(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    fallback = Counting(deterministic)
    engine = FallbackAnalysisEngine(primary=deterministic, fallback=fallback)

    await engine.analyze("trending", dict(CONTEXT))

    assert fallback.calls == 0


async def test_any_primary_failure_falls_back_not_only_engine_error(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    # A Vertex 429 or an auth error is not an EngineError, and an outage is an
    # outage whatever its type.
    engine = FallbackAnalysisEngine(
        primary=Exploding(RuntimeError("429 RESOURCE_EXHAUSTED")), fallback=deterministic
    )

    assert await engine.analyze("trending", dict(CONTEXT))


async def test_degraded_answers_are_counted(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    """The bad outcome is not one fallback, it is every answer falling back."""
    engine = FallbackAnalysisEngine(primary=Exploding(), fallback=deterministic)

    await engine.analyze("trending", dict(CONTEXT))
    await engine.analyze("trending", dict(CONTEXT))

    assert engine.degraded == 2


async def test_it_is_loud_about_degrading(
    deterministic: DeterministicAnalysisEngine, caplog: pytest.LogCaptureFixture
) -> None:
    engine = FallbackAnalysisEngine(primary=Exploding(), fallback=deterministic)

    with caplog.at_level("ERROR"):
        await engine.analyze("trending", dict(CONTEXT))

    assert any(record.levelname == "ERROR" for record in caplog.records)
    assert "EngineError" in caplog.text


async def test_closing_closes_both_engines(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    primary, fallback = Exploding(), Counting(deterministic)
    await FallbackAnalysisEngine(primary=primary, fallback=fallback).aclose()

    assert primary.closed and fallback.closed


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


async def test_the_deterministic_engine_is_never_wrapped() -> None:
    # It has nothing to fall back to, and wrapping it would only add a frame.
    set_engine(None)
    try:
        engine = get_engine(settings=settings_for(), store=MemoryStore())
        assert isinstance(engine, DeterministicAnalysisEngine)
    finally:
        set_engine(None)


async def test_the_fallback_can_be_turned_off() -> None:
    """An operator may legitimately prefer an outage to a degraded answer."""
    settings = settings_for(engine="adk", engine_fallback=False)
    set_engine(None)
    try:
        engine = get_engine(settings=settings, store=MemoryStore())
        assert not isinstance(engine, FallbackAnalysisEngine)
    finally:
        set_engine(None)


async def test_adk_is_wrapped_by_default() -> None:
    settings = settings_for(engine="adk")
    set_engine(None)
    try:
        engine = get_engine(settings=settings, store=MemoryStore())
        assert isinstance(engine, FallbackAnalysisEngine)
        assert engine.primary.name == "adk"
        assert engine.fallback.name == "deterministic"
    finally:
        set_engine(None)


# --------------------------------------------------------------------------
# the warmer must see failures, not degraded answers
# --------------------------------------------------------------------------


async def test_primary_engine_unwraps_the_fallback(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    from api.agents.engine import primary_engine

    primary = Exploding()
    wrapped = FallbackAnalysisEngine(primary=primary, fallback=deterministic)

    assert primary_engine(wrapped) is primary
    # Idempotent on an unwrapped engine, so callers need no isinstance check.
    assert primary_engine(deterministic) is deterministic


async def test_the_warmer_retries_instead_of_caching_a_degraded_answer(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient 429 during warming must not be cached for 6-12 hours.

    The warmer caches whatever it is handed. Handed a fallback body, it would
    bill every caller LLM prices for heuristics until the TTL expired, with one
    line in the logs to show for it. It has its own retry loop and nothing is
    waiting on it, so a failure is the right thing for it to see.
    """
    from ingest import precompute
    from ingest.precompute import warm_response_cache

    await seed_store(store)
    monkeypatch.setattr(precompute, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))

    real = DeterministicAnalysisEngine(store=store, settings=settings_for())
    attempts = {"n": 0}

    class FlakyPrimary(AnalysisEngine):
        name = "adk"

        async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise EngineError("429 RESOURCE_EXHAUSTED")
            return await real.analyze(endpoint_key, request_context)

    wrapped = FallbackAnalysisEngine(primary=FlakyPrimary(), fallback=real)
    monkeypatch.setattr(precompute, "get_engine", lambda **_: wrapped)

    result = await warm_response_cache(
        store,
        settings_for(),
        week=WEEK,
        targets=(precompute.WarmTarget("report"),),
    )

    assert result["warmed"] == 1
    assert result["failed"] == 0
    # The retry ran, which means the wrapper never swallowed the first failure.
    assert attempts["n"] == 2
    assert wrapped.degraded == 0, "the warmer must never accept a fallback body"
