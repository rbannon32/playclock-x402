"""The real ADK pipeline (``ENGINE=adk``).

One pipeline per endpoint, assembled from three shared sub-agent shapes
(tech spec §5)::

    stats_agent      LlmAgent + StatsTools function tools  -> state['stats_findings']
    research_agent   LlmAgent + google_search (search ONLY) -> state['research_findings']
    synthesis_agent  LlmAgent + output_schema, no tools     -> state['analysis']

**Stats and research run concurrently.** They write different state keys and
neither reads the other's — only the synthesizer reads both — so the two are
independent and a ``ParallelAgent`` wraps them, with synthesis sequenced after::

    SequentialAgent[ ParallelAgent[stats, research], synthesis ]

Running them in series cost a whole extra LLM round trip for nothing: measured
~72s end to end on a paid call against a <20s p95 target. When an endpoint has no
research agent there is nothing to parallelize and the pipeline stays a plain
``SequentialAgent[stats, synthesis]`` — a ``ParallelAgent`` of one is pure
overhead.

Four design points worth knowing before editing this file:

**The research agent gets search and nothing else.** Gemini will not combine the
built-in ``google_search`` tool with function tools on one agent, so the split
above is load-bearing rather than stylistic: the stats agent has function tools
and no search, the research agent has search and no function tools. Do not add a
``FunctionTool`` to ``research_agent``.

**The synthesizer is constrained to a schema without ``meta``.** See
:mod:`api.agents.schemas`: the model produces the analysis, the engine attaches
the provenance envelope. That saves tokens and removes any chance of a
fabricated freshness marker.

**Nothing happens at import time.** Agents are built on first use per endpoint
and cached; constructing them requires no credentials and makes no network
calls, so ``ENGINE=adk`` is safe to import in CI. The first *run* is what needs
Vertex AI.

Failure policy: a synthesis output that will not validate is retried once, then
raises :class:`~api.agents.engine.EngineError`. Routes turn that into a 500; the
payment dependency verifies before the handler and settles after it
(DESIGN_NOTES §2), so a failed run is never billed. A 429 or 5xx from Vertex is
retried *per request* first, inside the SDK (:mod:`api.agents.vertex`), so a
throttled call does not throw away the tool calls already finished; the
pipeline-level retry is the fallback for when that runs out.

Build-time verification notes (tech spec §11.3), ADK 2.8.0
----------------------------------------------------------
* Imports settled at ``google.adk.agents`` / ``.tools`` / ``.runners``;
  ``google_search`` is a pre-instantiated ``GoogleSearchTool`` singleton, not a
  class to construct.
* ``output_schema`` and ``tools`` *can* coexist in 2.8 (structure is enforced
  only on the final output), but the synthesizer still carries no tools — it has
  nothing left to look up, and a tool-free agent is the cheapest turn.
* ``SequentialAgent`` emits a ``DeprecationWarning`` in 2.8 pointing at the new
  ``Workflow`` API, which cannot yet be used as an ``LlmAgent`` sub-agent. We
  stay on ``SequentialAgent`` (the shape tech spec §5 specifies) until
  ``Workflow`` is usable; the pin ``google-adk>=2.8,<3`` keeps that stable.
* ``Runner`` is keyword-only and does **not** auto-create sessions
  (``auto_create_session`` defaults ``False``), so :meth:`_run_once` creates one
  explicitly before ``run_async``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from api.agents.engine import AnalysisEngine, EngineError, response_model_for
from api.agents.prompts import (
    RESEARCH_OUTPUT_KEY,
    STATS_OUTPUT_KEY,
    SYNTHESIS_OUTPUT_KEY,
    includes_research,
    research_instruction,
    stats_instruction,
    synthesis_instruction,
)
from api.agents.schemas import synthesis_schema
from api.agents.tools import StatsTools, run_scope
from api.agents.vertex import retry_options, thinking_config
from api.core.config import Settings, get_settings
from api.core.store import Store
from api.core.week import current_season, current_week
from api.data.sources import resolve_sources
from api.schemas import AnalysisMeta, AnalysisResponse

logger = logging.getLogger(__name__)

#: Sentinel for "not built yet", where ``None`` is a real value.
_UNSET = object()

#: ADK app name; shows up in Vertex traces.
APP_NAME = "playclock"

#: Anonymous user id — we store no user identity anywhere (PRD §4.4, the wallet
#: is the identity), so every run is the same synthetic user in a fresh session.
RUN_USER_ID = "playclock"

#: How many times a synthesis run may be retried after a schema-parse failure.
#: One retry, per tech spec §5; a second would double the cost of a call we are
#: already not going to bill for.
MAX_ATTEMPTS = 2

#: Pause before the retry when Vertex answered 429 RESOURCE_EXHAUSTED. Vertex
#: throttles bunched calls; retrying at once just spends the one retry on the
#: same throttle (the ADK eval lost team_report that way, 2026-09-03).
QUOTA_BACKOFF_SECONDS = 20.0

#: Total budget for resolving a body's research sources after synthesis.
SOURCE_RESOLUTION_SECONDS = 20.0

#: Signature of the source resolver, so tests can inject one with no network.
SourceResolver = Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]]

#: Per-endpoint list field that ``request.limit`` bounds. The synthesizer is
#: told the bound and still overruns it (the live Week 1 trending board held
#: 50 rows under ``limit=25``; the eval's 6-row case came back with 8), so the
#: engine enforces it after the fact. The model orders rows best-first, so
#: keeping the head is keeping the answer.
LIMITED_LISTS: dict[str, str] = {"trending": "players", "sleepers": "picks", "waivers": "board"}


class AdkAnalysisEngine(AnalysisEngine):
    """Run the ADK agent pipeline for one endpoint and return its contract.

    Args:
        store: Store the stats tools read from.
        settings: Settings; defaults to the process settings. ``model_id``
            selects the Gemini model for all three agents.
        include_research: Global default for whether the research agent is part
            of a pipeline. ``True`` defers to the per-endpoint
            :func:`api.agents.prompts.includes_research` flag; ``False`` drops
            research everywhere (useful to halve cost, or when Search grounding
            is unavailable).
    """

    name = "adk"

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        include_research: bool = True,
        source_resolver: SourceResolver = resolve_sources,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self._include_research = include_research
        self._source_resolver = source_resolver
        # endpoint_key -> (SequentialAgent, StatsTools). Built lazily and then
        # shared by every request for that endpoint, including concurrent ones:
        # the run's season/week live in a context variable, never on the tools
        # (:func:`api.agents.tools.run_scope`).
        self._pipelines: dict[str, Any] = {}
        self._tools: dict[str, StatsTools] = {}
        # One model object for every agent of every endpoint: it owns the
        # genai client (built on first run, not here), and the retry policy.
        self._model: Any = None
        # Sentinel, not None: None is a legitimate planner (model's default).
        self._planner_cached: Any = _UNSET

    # -- construction -----------------------------------------------------

    def uses_research(self, endpoint_key: str) -> bool:
        """Whether this engine will include the research agent for an endpoint."""
        return self._include_research and includes_research(endpoint_key, self._settings)

    def build_pipeline(self, endpoint_key: str) -> Any:
        """Build (or return the cached) ``SequentialAgent`` for one endpoint.

        Public so tests can assert the wiring without running anything. Safe to
        call with no credentials: ADK resolves the model lazily at run time.
        """
        cached = self._pipelines.get(endpoint_key)
        if cached is not None:
            return cached

        # Imported here, not at module scope: importing api.agents.pipeline must
        # not drag google-adk into a deterministic-engine process.
        from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent  # noqa: PLC0415
        from google.adk.tools import google_search  # noqa: PLC0415

        model = self._model_for_agents()
        planner = self._planner()
        tools = self._tools_for(endpoint_key)
        with_research = self.uses_research(endpoint_key)

        stats_agent = LlmAgent(
            name=f"stats_{endpoint_key}",
            model=model,
            description="Reads ingested NFL stats through function tools.",
            instruction=stats_instruction(endpoint_key),
            tools=tools.function_tools(),
            output_key=STATS_OUTPUT_KEY,
            planner=planner,
        )
        if with_research:
            # google_search is a pre-instantiated singleton tool, and it is the
            # ONLY tool this agent may carry (see module docstring).
            research_agent = LlmAgent(
                name=f"research_{endpoint_key}",
                model=model,
                description="Finds last-72-hour NFL news with Google Search grounding.",
                instruction=research_instruction(endpoint_key),
                tools=[google_search],
                output_key=RESEARCH_OUTPUT_KEY,
                planner=planner,
            )
            # Independent: different state keys, neither reads the other, only
            # the synthesizer reads both. Series would waste a round trip.
            sub_agents = [
                ParallelAgent(
                    name=f"gather_{endpoint_key}",
                    sub_agents=[stats_agent, research_agent],
                )
            ]
        else:
            # A ParallelAgent wrapping one agent is pure overhead.
            sub_agents = [stats_agent]

        sub_agents.append(
            LlmAgent(
                name=f"synthesis_{endpoint_key}",
                model=model,
                description="Writes the paid response contract.",
                instruction=synthesis_instruction(endpoint_key, include_research=with_research),
                tools=[],  # structured output only; the thinking is already done
                output_schema=synthesis_schema(endpoint_key),
                output_key=SYNTHESIS_OUTPUT_KEY,
                planner=planner,
            )
        )

        pipeline = SequentialAgent(name=f"pipeline_{endpoint_key}", sub_agents=sub_agents)
        self._pipelines[endpoint_key] = pipeline
        return pipeline

    def _model_for_agents(self) -> Any:
        """The (cached) ``Gemini`` model every agent in every pipeline shares.

        A bare model-id string would do the same resolution — ADK wraps it in
        ``Gemini(model=...)`` at run time — minus the one thing that matters:
        ``retry_options``. With none, google-genai stops after the first
        attempt, and a 429 on any of a board's ~9 calls fails the whole run
        (:mod:`api.agents.vertex`). Constructing the object here opens no
        client; ADK builds that on the first request.
        """
        if self._model is None:
            from google.adk.models.google_llm import Gemini  # noqa: PLC0415

            self._model = Gemini(
                model=self._settings.model_id,
                retry_options=retry_options(self._settings),
            )
        return self._model

    def _planner(self) -> Any:
        """The (cached) planner carrying ``MODEL_THINKING_LEVEL``, or ``None``.

        ADK sets thinking through a ``BuiltInPlanner`` rather than on the model,
        so this is where the cap reaches the pipeline's three agents. ``None``
        leaves each agent on the model's own default, which is what shipped.
        """
        if self._planner_cached is _UNSET:
            config = thinking_config(self._settings)
            if config is None:
                self._planner_cached = None
            else:
                from google.adk.planners import BuiltInPlanner  # noqa: PLC0415

                self._planner_cached = BuiltInPlanner(thinking_config=config)
        return self._planner_cached

    def _tools_for(self, endpoint_key: str) -> StatsTools:
        """Return the (cached) tools object backing one endpoint's stats agent.

        Shared across concurrent runs on purpose — its season/week come from
        the active :func:`~api.agents.tools.run_scope`, so the fallback defaults
        set here are only ever used outside a run.
        """
        tools = self._tools.get(endpoint_key)
        if tools is None:
            tools = StatsTools(self._store, season=int(self._settings.season), week=1)
            self._tools[endpoint_key] = tools
        return tools

    # -- execution --------------------------------------------------------

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        """Run the pipeline and return the endpoint's response model.

        Args:
            endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`.
            request_context: Endpoint inputs; see
                :data:`api.agents.engine.REQUEST_CONTEXT_KEYS`. The dict is
                seeded into ADK session state under ``request`` so tools and
                prompts see the same values.

        Raises:
            EngineError: If both attempts fail to produce a body that validates.
        """
        model_cls = response_model_for(endpoint_key)
        ctx = dict(request_context or {})
        season, week = await self._resolve_scope(ctx)
        ctx = await self._with_candidates(endpoint_key, ctx, season, week)

        tools = self._tools_for(endpoint_key)
        pipeline = self.build_pipeline(endpoint_key)

        # The scope is a context variable rather than an assignment onto
        # ``tools``: that object is shared with every other in-flight request
        # for this endpoint (see api/agents/tools.py).
        with run_scope(season, week):
            meta = AnalysisMeta(
                generated_at=datetime.now(UTC),
                data_freshness=dict((await tools.get_data_freshness()).get("freshness") or {}),
                model=self._settings.model_id,
                engine="adk",
                cache=None,
            )

            last_error: Exception | None = None
            attempt = 0
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    raw = await self._run_once(pipeline, endpoint_key, ctx, season, week)
                    payload = enforce_computed_facts(endpoint_key, dict(raw), ctx)
                    payload = enforce_limits(endpoint_key, payload, ctx)
                    payload["meta"] = meta
                    body = model_cls.model_validate(payload)
                except Exception as exc:  # noqa: BLE001 - retried, then re-raised as EngineError
                    last_error = exc
                    logger.warning(
                        "adk synthesis attempt %s/%s failed for %s: %s",
                        attempt,
                        MAX_ATTEMPTS,
                        endpoint_key,
                        _describe(exc),
                    )
                    if _is_permanent_error(exc):
                        # A 400/403/404 is the request or the deployment being
                        # wrong (MODEL_ID at a regional location 404s every
                        # call); the retry would buy the same answer twice.
                        break
                    if attempt < MAX_ATTEMPTS and _is_quota_error(exc):
                        await asyncio.sleep(QUOTA_BACKOFF_SECONDS)
                else:
                    return await self._with_resolved_sources(endpoint_key, body)
        raise EngineError(
            f"ADK pipeline failed to produce a valid {model_cls.__name__} for "
            f"{endpoint_key!r} after {attempt} attempt(s)"
        ) from last_error

    async def _run_once(
        self,
        pipeline: Any,
        endpoint_key: str,
        ctx: dict[str, Any],
        season: int,
        week: int,
    ) -> dict[str, Any]:
        """Execute the pipeline once and return the synthesizer's parsed output."""
        from google.adk.runners import InMemoryRunner  # noqa: PLC0415
        from google.genai import types  # noqa: PLC0415

        runner = InMemoryRunner(agent=pipeline, app_name=APP_NAME)
        session_id = uuid.uuid4().hex
        # Sessions are never auto-created (Runner.auto_create_session defaults
        # False in ADK 2.8) — create it explicitly, seeded with the request.
        await runner.session_service.create_session(
            app_name=APP_NAME,
            user_id=RUN_USER_ID,
            session_id=session_id,
            state={"request": ctx, "endpoint": endpoint_key, "season": season, "week": week},
        )
        prompt = _run_prompt(endpoint_key, ctx, season, week)
        final_text = ""
        try:
            async for event in runner.run_async(
                user_id=RUN_USER_ID,
                session_id=session_id,
                new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
            ):
                text = _event_text(event)
                if text:
                    final_text = text
            session = await runner.session_service.get_session(
                app_name=APP_NAME, user_id=RUN_USER_ID, session_id=session_id
            )
            state = dict(getattr(session, "state", {}) or {})
        finally:
            await _close(runner)

        # With output_schema set, ADK stores the *validated* dict in state; the
        # event text is the fallback for a shape that skipped that path.
        payload = state.get(SYNTHESIS_OUTPUT_KEY)
        if isinstance(payload, str):
            payload = _parse_json(payload)
        if not isinstance(payload, dict):
            payload = _parse_json(final_text)
        if not isinstance(payload, dict):
            raise EngineError(
                f"pipeline for {endpoint_key!r} produced no parseable structured output"
            )
        payload.pop("meta", None)  # the engine owns provenance, not the model
        return payload

    async def _with_resolved_sources(
        self, endpoint_key: str, body: AnalysisResponse
    ) -> AnalysisResponse:
        """Turn grounding redirects into citations a reader can check.

        Part of the output contract rather than a precompute nicety: a source
        titled with a bare domain over an opaque redirect is a citation in
        name only, wherever the body is served. Bounded by
        :data:`SOURCE_RESOLUTION_SECONDS`; any failure keeps the sources as the
        model returned them, because losing a citation is worse than an ugly
        one.
        """
        if not body.sources:
            return body
        try:
            resolved = await asyncio.wait_for(
                self._source_resolver([s.model_dump(mode="json") for s in body.sources]),
                timeout=SOURCE_RESOLUTION_SECONDS,
            )
            return body.model_copy(
                update={"sources": [type(body.sources[0])(**s) for s in resolved]}
            )
        except Exception as exc:  # noqa: BLE001 - never lose a body over a citation
            logger.warning(
                "source resolution failed for %s (%s: %s); serving unresolved sources",
                endpoint_key,
                type(exc).__name__,
                exc,
            )
            return body

    async def _with_candidates(
        self, endpoint_key: str, ctx: dict[str, Any], season: int, week: int
    ) -> dict[str, Any]:
        """Attach the data-chosen candidate list for the boards that need one.

        Left to pick its own sleepers and "emerging" players, the synthesizer
        reached for the most-added names on the trending board — the one set
        those sections exist to exclude (observed on every warmed Week 1 board,
        2026-09-03). So the deterministic engine's scorer chooses the pool and
        the model narrates it: ``request.candidates`` carries identity, usage,
        matchup and add count for each, and the prompts say to draw from it
        only. A caller-supplied list is respected; other endpoints are
        untouched. Never raises — a board with no candidate list is still a
        board, and the quality gate will say if it went wrong.
        """
        from api.agents.deterministic import (  # noqa: PLC0415 - avoid an import cycle
            CANDIDATE_ENDPOINTS,
            DeterministicAnalysisEngine,
        )

        if endpoint_key not in CANDIDATE_ENDPOINTS or "candidates" in ctx:
            return ctx
        try:
            scorer = DeterministicAnalysisEngine(store=self._store, settings=self._settings)
            candidates = await scorer.candidates(
                endpoint_key, {**ctx, "season": season, "week": week}
            )
        except Exception as exc:  # noqa: BLE001 - narrate without a list rather than fail
            logger.warning(
                "could not build the candidate list for %s (%s: %s)",
                endpoint_key,
                type(exc).__name__,
                exc,
            )
            return ctx
        return {**ctx, "candidates": candidates}

    async def _resolve_scope(self, ctx: dict[str, Any]) -> tuple[int, int]:
        """Resolve ``(season, week)``, honouring explicit context values."""
        season = ctx.get("season")
        if not isinstance(season, int):
            season = await current_season(self._store, self._settings)
        week = ctx.get("week")
        if not isinstance(week, int):
            week = await current_week(self._store, self._settings)
        ctx.setdefault("season", int(season))
        ctx.setdefault("week", int(week))
        return int(season), int(week)


#: Response fields that are copied from precomputed analytics rather than
#: written by the model, per endpoint: ``(request context key, field name)``.
#:
#: ``team_report``'s numbers are deterministic arithmetic over Sleeper matchup
#: history (:mod:`api.data.team_analytics`), handed to the synthesizer in
#: ``request.team_analytics`` with instructions to "reproduce the precomputed
#: values exactly". :func:`enforce_computed_facts` makes that true instead of
#: asking for it — the draft board's lesson (DESIGN_NOTES, "computed, not
#: synthesized") applied to the one other endpoint that hands the model a
#: finished table.
COMPUTED_FACTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "team_report": ("team_analytics", ("manager_review", "positional_strength_vs_league")),
}

#: Keys of a computed block the model is *supposed* to fill, so overwriting the
#: block must not erase them. ``observations`` is the one place the team report
#: invites the model to say something not computed for it, and the prompt
#: requires it to be worded as an observation rather than a statistic.
MODEL_WRITTEN_KEYS = ("observations",)


def enforce_computed_facts(
    endpoint_key: str, payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Overwrite the synthesized copy of a precomputed block with the real one.

    The model is told to reproduce ``manager_review`` and
    ``positional_strength_vs_league`` exactly. It mostly does — and the one
    failure mode is invisible from the outside, because a plausible number in
    a sentence it was asked to copy reads exactly like a correct one. Copying
    them here costs nothing and removes the question.

    Model-written keys inside a copied block survive
    (:data:`MODEL_WRITTEN_KEYS`); the analytics deliberately leave
    ``observations`` empty for the model to fill.

    A missing or malformed analytics block leaves the payload alone: the
    endpoint has a documented no-history path, and there is nothing truer to
    substitute.
    """
    spec = COMPUTED_FACTS.get(endpoint_key)
    if spec is None:
        return payload
    ctx_key, fields = spec
    facts = ctx.get(ctx_key)
    if not isinstance(facts, dict):
        return payload
    for field in fields:
        computed = facts.get(field)
        if computed is None:
            continue
        written = payload.get(field)
        if isinstance(computed, dict) and isinstance(written, dict):
            kept = {k: written[k] for k in MODEL_WRITTEN_KEYS if k in written}
            # The analytics carry extras beyond the schema (worst_week,
            # top_offenders...) for the narrator to read; Pydantic drops them.
            merged = {**computed, **kept}
            if merged != written:
                logger.info(
                    "%s: replaced synthesized %s with the computed block", endpoint_key, field
                )
            payload[field] = merged
        elif computed != written:
            logger.info("%s: replaced synthesized %s with the computed value", endpoint_key, field)
            payload[field] = computed
    return payload


def enforce_limits(
    endpoint_key: str, payload: dict[str, Any], ctx: dict[str, Any]
) -> dict[str, Any]:
    """Cut a synthesized body down to ``request.limit`` rows.

    Flat boards (:data:`LIMITED_LISTS`) keep their first ``limit`` rows. The
    draft board is tiered: rows are kept across tiers in order until the
    limit is reached, empty tiers are dropped, and the ``values`` and
    ``reaches`` callouts are filtered to the rows that survived, since a value
    the board no longer shows is not a value the reader can act on. A missing
    or non-integer ``limit`` means no cut.
    """
    limit = ctx.get("limit")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        return payload
    field = LIMITED_LISTS.get(endpoint_key)
    if field is not None:
        rows = payload.get(field)
        if isinstance(rows, list) and len(rows) > limit:
            logger.info(
                "%s returned %d rows for limit=%d; keeping the head", endpoint_key, len(rows), limit
            )
            payload[field] = rows[:limit]
        return payload
    if endpoint_key == "draft_board" and isinstance(payload.get("tiers"), list):
        kept_ids: set[str] = set()
        remaining = limit
        tiers: list[dict[str, Any]] = []
        for tier in payload["tiers"]:
            if not isinstance(tier, dict):
                continue
            players = [p for p in tier.get("players") or [] if isinstance(p, dict)][:remaining]
            remaining -= len(players)
            if players:
                tiers.append({**tier, "players": players})
                kept_ids.update(str(p.get("player_id")) for p in players)
            if remaining <= 0:
                break
        payload["tiers"] = tiers
        for callouts in ("values", "reaches"):
            rows = payload.get(callouts)
            if isinstance(rows, list):
                payload[callouts] = [
                    r for r in rows if isinstance(r, dict) and str(r.get("player_id")) in kept_ids
                ]
    return payload


def _leaf_exceptions(exc: BaseException) -> list[BaseException]:
    """Flatten an exception group to the exceptions that actually happened.

    The ``ParallelAgent`` runs stats and research in a task group, so a Vertex
    429 inside either surfaces as ``unhandled errors in a TaskGroup (1
    sub-exception)`` — a message that names nothing and hid the throttle from
    the backoff on the ADK eval run of 2026-09-03.
    """
    if isinstance(exc, BaseExceptionGroup):
        leaves: list[BaseException] = []
        for sub in exc.exceptions:
            leaves.extend(_leaf_exceptions(sub))
        return leaves
    return [exc]


def _describe(exc: BaseException) -> str:
    """The exception for a log line, unwrapping groups to their leaves."""
    leaves = _leaf_exceptions(exc)
    return "; ".join(f"{type(leaf).__name__}: {leaf}" for leaf in leaves)[:600]


def _is_quota_error(exc: BaseException) -> bool:
    """Whether an exception (or any leaf of a group) is Vertex telling us to slow down."""
    for leaf in _leaf_exceptions(exc):
        text = f"{type(leaf).__name__}: {leaf}"
        if "RESOURCE_EXHAUSTED" in text or " 429" in text or text.startswith("429"):
            return True
    return False


#: 4xx statuses that are *not* the request being wrong: timeout, throttle and
#: Vertex's 499 CANCELLED. Every other 4xx fails identically on a retry.
_TRANSIENT_CLIENT_CODES = frozenset({408, 429, 499})


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status an API error carries, following ``raise ... from`` chains.

    ``google.genai.errors.APIError`` (and its ``ClientError`` / ``ServerError``)
    exposes it as ``.code``. Duck-typed so this module never imports genai.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(code, int) and not isinstance(code, bool) and 100 <= code < 600:
            return code
        current = current.__cause__
    return None


def _is_permanent_error(exc: BaseException) -> bool:
    """Whether retrying ``exc`` cannot help: a non-transient 4xx and nothing retryable.

    Only when *every* leaf is such a client error — a group that also holds a
    429, a 5xx, a timeout or a schema-parse failure is still worth the retry.
    """
    leaves = _leaf_exceptions(exc)
    if not leaves:
        return False
    for leaf in leaves:
        code = _status_code(leaf)
        if code is None or not 400 <= code < 500 or code in _TRANSIENT_CLIENT_CODES:
            return False
    return True


def _run_prompt(endpoint_key: str, ctx: dict[str, Any], season: int, week: int) -> str:
    """Build the single user turn that kicks the pipeline off."""
    return (
        f"Endpoint: {endpoint_key}. Season {season}, NFL week {week}.\n"
        f"Request: {json.dumps(ctx, default=str, sort_keys=True)}\n"
        f"Produce the paid analysis for this request."
    )


def _event_text(event: Any) -> str:
    """Extract non-thought text from a final-response ADK event, or ``''``."""
    try:
        if not event.is_final_response():
            return ""
    except Exception:  # noqa: BLE001 - event shapes vary across ADK versions
        return ""
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) or []
    return "".join(
        part.text
        for part in parts
        if getattr(part, "text", None) and not getattr(part, "thought", False)
    ).strip()


def _parse_json(text: str) -> Any:
    """Parse JSON, tolerating a ```json fence around it. ``None`` on failure."""
    if not text:
        return None
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[: -len("```")]
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


async def _close(runner: Any) -> None:
    """Close a runner if this ADK version exposes a closer."""
    closer = getattr(runner, "close", None) or getattr(runner, "aclose", None)
    if closer is None:
        return
    try:
        result = closer()
        if hasattr(result, "__await__"):
            await result
    except Exception:  # noqa: BLE001 - cleanup must never mask a run failure
        logger.debug("runner close failed", exc_info=True)
