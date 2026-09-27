"""Analysis engines — the layer that turns ingested data into a paid answer.

Routes should import from this package and nothing deeper::

    from api.agents import RESPONSE_MODELS, EngineError, get_engine

    engine = get_engine()
    body = await engine.analyze("matchup", {"players": ["Bijan Robinson", "Breece Hall"]})

Two implementations satisfy the same :class:`~api.agents.engine.AnalysisEngine`
contract — the deterministic, LLM-free one (CI, fallback) and the real ADK
pipeline — selected by the ``ENGINE`` setting. See
:mod:`api.agents.engine` for the seam and
:mod:`api.agents.deterministic` for the per-endpoint ``request_context`` schema.

``DeterministicAnalysisEngine`` is exported eagerly; ``AdkAnalysisEngine`` is
resolved lazily via ``__getattr__`` so importing this package never pulls in
google-adk.
"""

from __future__ import annotations

from typing import Any

from api.agents.deterministic import DeterministicAnalysisEngine
from api.agents.engine import (
    REQUEST_CONTEXT_KEYS,
    RESPONSE_MODELS,
    AnalysisEngine,
    EngineError,
    get_engine,
    response_model_for,
    set_engine,
)

__all__ = [
    "REQUEST_CONTEXT_KEYS",
    "RESPONSE_MODELS",
    "AdkAnalysisEngine",
    "AnalysisEngine",
    "DeterministicAnalysisEngine",
    "EngineError",
    "get_engine",
    "response_model_for",
    "set_engine",
]


def __getattr__(name: str) -> Any:
    """Resolve ``AdkAnalysisEngine`` on demand, keeping ADK off the import path."""
    if name == "AdkAnalysisEngine":
        from api.agents.pipeline import AdkAnalysisEngine  # noqa: PLC0415

        return AdkAnalysisEngine
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
