"""The engine seam: the response-model map, the factory, and the test hook."""

from __future__ import annotations

from typing import Any

import pytest

from api.agents import (
    REQUEST_CONTEXT_KEYS,
    RESPONSE_MODELS,
    AnalysisEngine,
    DeterministicAnalysisEngine,
    EngineError,
    get_engine,
    response_model_for,
    set_engine,
)
from api.core.config import ENDPOINT_KEYS, Settings
from api.core.store import MemoryStore, Store
from api.schemas import AnalysisResponse


@pytest.fixture(autouse=True)
def _clear_engine_override() -> Any:
    """No test may leak an engine override into the next one."""
    set_engine(None)
    yield
    set_engine(None)


# -- the map wave-3 routes depend on --------------------------------------


def test_response_models_cover_every_paid_endpoint() -> None:
    assert set(RESPONSE_MODELS) == set(ENDPOINT_KEYS)


def test_request_context_keys_cover_every_paid_endpoint() -> None:
    assert set(REQUEST_CONTEXT_KEYS) == set(ENDPOINT_KEYS)


def test_every_response_model_extends_the_shared_contract() -> None:
    """PRD §5: verdict/confidence/reasoning/stats_cited/sources/meta at the root."""
    for key, model in RESPONSE_MODELS.items():
        assert issubclass(model, AnalysisResponse), key
        assert {"verdict", "confidence", "reasoning", "stats_cited", "sources", "meta"} <= set(
            model.model_fields
        ), key


def test_response_models_are_distinct() -> None:
    assert len(set(RESPONSE_MODELS.values())) == len(RESPONSE_MODELS)


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_response_model_for_known_key(key: str) -> None:
    assert response_model_for(key) is RESPONSE_MODELS[key]


def test_response_model_for_unknown_key_raises_engine_error() -> None:
    with pytest.raises(EngineError, match="unknown endpoint key"):
        response_model_for("not_an_endpoint")


# -- the factory ----------------------------------------------------------


def test_get_engine_returns_deterministic_by_default(settings: Settings, store: Store) -> None:
    engine = get_engine(settings, store)
    assert isinstance(engine, DeterministicAnalysisEngine)
    assert engine.name == "deterministic"


def test_get_engine_caches_per_store(settings: Settings, store: Store) -> None:
    assert get_engine(settings, store) is get_engine(settings, store)


def test_get_engine_rebuilds_when_the_store_changes(settings: Settings, store: Store) -> None:
    """A test swapping stores must not get an engine pointing at the old one."""
    first = get_engine(settings, store)
    second = get_engine(settings, MemoryStore())
    assert first is not second


def test_set_engine_overrides_the_factory(settings: Settings, store: Store) -> None:
    class Stub(AnalysisEngine):
        name = "stub"

        async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> Any:
            raise AssertionError("not called")

    stub = Stub()
    set_engine(stub)
    assert get_engine(settings, store) is stub
    set_engine(None)
    assert get_engine(settings, store) is not stub


def test_engine_abc_requires_analyze() -> None:
    with pytest.raises(TypeError):
        AnalysisEngine()  # type: ignore[abstract]


async def test_unknown_endpoint_key_raises_engine_error(settings: Settings, store: Store) -> None:
    engine = DeterministicAnalysisEngine(store=store, settings=settings)
    with pytest.raises(EngineError):
        await engine.analyze("nope", {})


def test_adk_engine_is_lazily_exported() -> None:
    """``api.agents.AdkAnalysisEngine`` resolves, but only on attribute access."""
    import api.agents as agents  # noqa: PLC0415
    from api.agents.pipeline import AdkAnalysisEngine  # noqa: PLC0415

    assert agents.AdkAnalysisEngine is AdkAnalysisEngine
    with pytest.raises(AttributeError):
        _ = agents.NoSuchEngine  # type: ignore[attr-defined]
