"""The :class:`AnalysisEngine` seam.

Every paid route asks *one* object for its body::

    from api.agents import get_engine

    engine = get_engine()
    body = await engine.analyze("player", {"name": "Bijan Robinson", "week": 4})

Two implementations sit behind that call (DESIGN_NOTES §4):

``DeterministicAnalysisEngine`` (``ENGINE=deterministic``)
    Builds a fully valid response contract straight from the stats tools, with
    no LLM anywhere. Hermetic, sub-millisecond, and always schema-correct. It is
    the CI workhorse, the eval baseline, and the production fallback.

``AdkAnalysisEngine`` (``ENGINE=adk``)
    The real thing: a per-endpoint ``SequentialAgent`` of stats -> research ->
    synthesis agents on Vertex AI (tech spec §5).

``NarratedAnalysisEngine`` (``ENGINE=narrated``, :mod:`api.agents.narrator`)
    The deterministic body with its prose rewritten by one tool-free model
    call, guarded so no number or name the body does not already carry can
    reach the payer. Fast enough for the personalized endpoints.

Both return the *same* pydantic model for a given endpoint key, so routes are
engine-agnostic and the x402 layer never sees the difference.

Request context
---------------
``request_context`` is a plain JSON-able dict. Its per-endpoint schema is
documented on :data:`REQUEST_CONTEXT_KEYS` and in
:mod:`api.agents.deterministic` — routes are responsible for populating it
(resolving Sleeper rosters, computing team analytics), engines only read it.
Both engines resolve ``week``/``season`` themselves when omitted, so a route may
always pass them through as ``None``.
"""

from __future__ import annotations

import abc
import logging
from typing import Any

from api.core.config import ENDPOINT_KEYS, Settings, get_settings
from api.core.store import Store, get_store
from api.schemas import (
    AnalysisResponse,
    DraftBoardResponse,
    DraftReportResponse,
    MatchupResponse,
    PlayerResponse,
    ReportResponse,
    RosterResponse,
    SleepersResponse,
    TeamReportResponse,
    TrendingResponse,
    WaiversResponse,
)

logger = logging.getLogger(__name__)


class EngineError(RuntimeError):
    """An analysis run failed and produced no valid response body.

    Routes translate this to a 500. The payment dependency verifies before the
    handler and settles after it (DESIGN_NOTES §2), so a raised ``EngineError``
    means the caller is never charged.
    """


#: ``endpoint_key`` -> the response model that endpoint must produce.
#:
#: This is the single mapping wave-3 routes use to know what an ``analyze()``
#: call returns, and the schema the ADK synthesis agent is constrained to.
#: Keys are exactly :data:`api.core.config.ENDPOINT_KEYS`.
RESPONSE_MODELS: dict[str, type[AnalysisResponse]] = {
    "trending": TrendingResponse,
    "sleepers": SleepersResponse,
    "player": PlayerResponse,
    "matchup": MatchupResponse,
    "roster": RosterResponse,
    "waivers": WaiversResponse,
    "report": ReportResponse,
    "team_report": TeamReportResponse,
    "draft_board": DraftBoardResponse,
    "draft_report": DraftReportResponse,
}

#: Documentation-only map of the ``request_context`` keys each endpoint reads.
#: ``week`` and ``season`` are accepted (and may be ``None``) everywhere.
REQUEST_CONTEXT_KEYS: dict[str, tuple[str, ...]] = {
    "trending": ("week", "season", "lookback_hours", "limit"),
    "sleepers": ("week", "season", "limit"),
    "player": ("week", "season", "name", "player_id"),
    "matchup": ("week", "season", "players"),
    "roster": ("week", "season", "sleeper_username", "league_id", "roster", "free_agents"),
    "waivers": ("week", "season", "limit"),
    "report": ("week", "season"),
    "draft_board": ("season", "limit", "scoring"),
    "draft_report": ("season", "draft_id", "sleeper_username", "picks"),
    "team_report": (
        "week",
        "season",
        "sleeper_username",
        "league_id",
        "league_name",
        "roster",
        "free_agents",
        "team_analytics",
    ),
}


def response_model_for(endpoint_key: str) -> type[AnalysisResponse]:
    """Return the response model for ``endpoint_key``.

    Raises:
        EngineError: If ``endpoint_key`` is not a known paid endpoint.
    """
    try:
        return RESPONSE_MODELS[endpoint_key]
    except KeyError as exc:
        raise EngineError(f"unknown endpoint key: {endpoint_key!r}") from exc


class AnalysisEngine(abc.ABC):
    """Produces a paid response body for one endpoint.

    An ABC rather than a ``Protocol`` so tests and the factory can
    ``isinstance``-check an engine, and so both implementations inherit the
    ``name`` contract used in ``meta``/health output.
    """

    #: Stable identifier, matching the ``ENGINE`` setting value.
    name: str = "abstract"

    @abc.abstractmethod
    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        """Produce the response body for one paid call.

        Args:
            endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`.
            request_context: Endpoint inputs; see :data:`REQUEST_CONTEXT_KEYS`.

        Returns:
            An instance of ``RESPONSE_MODELS[endpoint_key]``.

        Raises:
            EngineError: On an unknown endpoint key, or when the engine cannot
                produce a body that validates against the response contract.
        """

    async def aclose(self) -> None:  # pragma: no cover - trivial default
        """Release engine resources. Safe to call more than once."""
        return None


_engine_override: AnalysisEngine | None = None
_default_engine: AnalysisEngine | None = None
_default_key: tuple[str, int] | None = None


def get_engine(settings: Settings | None = None, store: Store | None = None) -> AnalysisEngine:
    """Return the process-wide :class:`AnalysisEngine`.

    Honours :func:`set_engine` overrides first (tests / DI), otherwise builds
    the engine named by ``Settings.engine`` once and reuses it. The cached
    instance is rebuilt when the selected engine or the active store changes,
    so a test that swaps :class:`~api.core.store.MemoryStore` never gets an
    engine pointing at the previous one.

    Args:
        settings: Settings; defaults to the process settings.
        store: Store the engine reads stats from; defaults to
            :func:`api.core.store.get_store`.
    """
    if _engine_override is not None:
        return _engine_override

    global _default_engine, _default_key
    settings = settings or get_settings()
    store = store or get_store(settings)
    key = (settings.engine, id(store))
    if _default_engine is None or _default_key != key:
        _default_engine = _build_engine(settings, store)
        _default_key = key
    return _default_engine


class FallbackAnalysisEngine(AnalysisEngine):
    """Runs ``primary``; answers from ``fallback`` when it fails.

    Why this exists: :class:`~api.agents.pipeline.AdkAnalysisEngine` raises
    :class:`EngineError` after its retries, the route turns that into a 500, and
    Vertex answers 429 whenever calls bunch up. :mod:`ingest.precompute` already
    treats that as normal and waits minutes between attempts — a luxury a paid
    request does not have. Advertised traffic produces exactly those bursts, so
    without this the endpoints are simply down at the moment they are busiest.

    Serving the fallback rather than 500ing is a deliberate trade, and it is not
    free: a 500 settles nothing, while a fallback answer is billed. It is the
    right trade only because the deterministic body is a real answer — it is the
    eval baseline, every number in it comes from the ingested store, and it says
    what it is: ``meta.model`` is ``null``, which the web UI and the MCP server
    both surface. (The reasoning used to open with "Deterministic engine" too;
    that preamble made every answer read as an apology and was dropped in
    favour of leading with the call. DESIGN_NOTES §24.)

    The failure is logged at ERROR with the primary's exception type, because
    the bad outcome here is not one fallback — it is *every* answer quietly
    falling back on a credentials mistake while callers pay LLM prices. Alert on
    the rate, not the event.
    """

    name = "fallback"

    def __init__(self, primary: AnalysisEngine, fallback: AnalysisEngine) -> None:
        self.primary = primary
        self.fallback = fallback
        #: Fallbacks served since process start, for logging and health.
        self.degraded = 0

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        """Try the primary engine, then the fallback."""
        try:
            return await self.primary.analyze(endpoint_key, request_context)
        except Exception as exc:  # noqa: BLE001 - any primary failure is a fallback
            self.degraded += 1
            logger.error(
                "engine %s failed for %s (%s: %s); answering from %s instead "
                "[degraded=%d since start]",
                self.primary.name,
                endpoint_key,
                type(exc).__name__,
                exc,
                self.fallback.name,
                self.degraded,
            )
            return await self.fallback.analyze(endpoint_key, request_context)

    async def aclose(self) -> None:
        """Close both engines."""
        for engine in (self.primary, self.fallback):
            closer = getattr(engine, "aclose", None)
            if closer is not None:
                await closer()


def primary_engine(engine: AnalysisEngine) -> AnalysisEngine:
    """Unwrap :class:`FallbackAnalysisEngine`, returning the engine it fronts.

    For callers that must see a failure rather than a degraded answer. The
    warmer is the one that matters: it caches whatever it is handed, so a
    silently-substituted deterministic body would be served — and billed at LLM
    prices — for the whole TTL. It has its own retry loop and can afford to wait
    minutes, which is the better answer to a transient quota error.
    """
    return engine.primary if isinstance(engine, FallbackAnalysisEngine) else engine


def build_engine(settings: Settings, store: Store, engine: str | None = None) -> AnalysisEngine:
    """Construct an engine, optionally one other than ``settings.engine``.

    ``engine`` overrides the selector for this construction only; the process
    default from :func:`get_engine` is untouched. :mod:`ingest.precompute` uses
    it to warm the draft board through the narrated engine while the rest of
    the boards run the ADK pipeline: a 200-player board is a computation the
    synthesizer cannot reproduce (it returned 30 rows), so the body is computed
    and only the prose is the model's.
    """
    if engine and engine != settings.engine:
        settings = settings.model_copy(update={"engine": engine})
    return _build_engine(settings, store)


def _build_engine(settings: Settings, store: Store) -> AnalysisEngine:
    """Construct the engine named by ``settings.engine``.

    ADK is imported lazily so that a deterministic-engine deployment (and every
    unit test) never pays the ADK import cost or touches google-genai at all.
    """
    from api.agents.deterministic import DeterministicAnalysisEngine  # noqa: PLC0415

    if settings.engine == "narrated":
        # Deterministic body, one model call for the prose, and its own fallback
        # to the untouched body — so no FallbackAnalysisEngine wrapper here.
        from api.agents.narrator import GeminiNarrator, NarratedAnalysisEngine  # noqa: PLC0415

        return NarratedAnalysisEngine(
            deterministic=DeterministicAnalysisEngine(store=store, settings=settings),
            narrator=GeminiNarrator(settings),
            settings=settings,
        )
    if settings.engine != "adk":
        return DeterministicAnalysisEngine(store=store, settings=settings)

    from api.agents.pipeline import AdkAnalysisEngine  # noqa: PLC0415

    adk = AdkAnalysisEngine(store=store, settings=settings)
    if not settings.engine_fallback:
        return adk
    return FallbackAnalysisEngine(
        primary=adk,
        fallback=DeterministicAnalysisEngine(store=store, settings=settings),
    )


def set_engine(engine: AnalysisEngine | None) -> None:
    """Override the engine returned by :func:`get_engine`.

    Pass ``None`` to clear the override and fall back to settings-driven
    construction. Intended for tests and dependency injection.
    """
    global _engine_override, _default_engine, _default_key
    _engine_override = engine
    if engine is None:
        _default_engine = None
        _default_key = None


# Fail loudly at import time if the response-model map ever drifts from the
# canonical endpoint list — a missing key would only surface as a 500 on a paid
# call otherwise.
assert set(RESPONSE_MODELS) == set(ENDPOINT_KEYS), (
    "RESPONSE_MODELS must cover exactly api.core.config.ENDPOINT_KEYS"
)
assert set(REQUEST_CONTEXT_KEYS) == set(ENDPOINT_KEYS), (
    "REQUEST_CONTEXT_KEYS must cover exactly api.core.config.ENDPOINT_KEYS"
)
