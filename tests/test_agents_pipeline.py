"""The ADK pipeline: wiring, prompts, and import hygiene.

Nothing here calls Vertex AI. What it proves is that the pipeline **can be
built** in an environment with no credentials and no network — which is exactly
what CI and a cold Cloud Run container do before the first paid request — and
that the agent graph is wired the way tech spec §5 describes.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any

import pytest

from api.agents.deterministic import DeterministicAnalysisEngine
from api.agents.engine import RESPONSE_MODELS, EngineError
from api.agents.pipeline import (
    MAX_ATTEMPTS,
    AdkAnalysisEngine,
    _event_text,
    _parse_json,
    _run_prompt,
)
from api.agents.prompts import (
    ENDPOINT_PROMPTS,
    NO_UNCITED_STATS_RULE,
    RESEARCH_OUTPUT_KEY,
    STATS_OUTPUT_KEY,
    SYNTHESIS_OUTPUT_KEY,
    includes_research,
    research_instruction,
    stats_instruction,
    synthesis_instruction,
)
from api.agents.schemas import ENGINE_OWNED_FIELDS, synthesis_schema
from api.agents.tools import FUNCTION_TOOL_NAMES
from api.core.config import ENDPOINT_KEYS, Settings
from api.core.store import Store


def leaf_agents(agent: Any) -> list[Any]:
    """Flatten a pipeline to its LlmAgent leaves.

    The graph is no longer flat: stats and research sit inside a ParallelAgent,
    so a plain walk of ``pipeline.sub_agents`` would hand back a workflow node
    that has no ``.model`` or ``.tools``.
    """
    children = getattr(agent, "sub_agents", None)
    if not children:
        return [agent]
    return [leaf for child in children for leaf in leaf_agents(child)]


def named(pipeline: Any, name: str) -> Any:
    """The one leaf agent called ``name``."""
    return next(a for a in leaf_agents(pipeline) if a.name == name)


@pytest.fixture
def adk_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="adk",
        x402_mode="disabled",
        model_id="gemini-3.7-flash",
        season=2026,
        week_override=4,
    )


@pytest.fixture
def engine(store: Store, adk_settings: Settings) -> AdkAnalysisEngine:
    return AdkAnalysisEngine(store=store, settings=adk_settings)


# -- wiring ---------------------------------------------------------------


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_pipeline_builds_without_credentials(engine: AdkAnalysisEngine, key: str) -> None:
    """No GCP creds exist here; construction must still succeed and cache."""
    pipeline = engine.build_pipeline(key)
    assert pipeline.name == f"pipeline_{key}"
    assert engine.build_pipeline(key) is pipeline


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_pipeline_gathers_in_parallel_then_synthesises(engine: AdkAnalysisEngine, key: str) -> None:
    """Stats and research are independent, so they run concurrently.

    They write different state keys and neither reads the other's — only the
    synthesizer reads both — so running them in series cost a whole extra LLM
    round trip. Synthesis still comes last, and still comes after both.
    """
    from google.adk.agents import ParallelAgent  # noqa: PLC0415

    pipeline = engine.build_pipeline(key)
    top = [agent.name for agent in pipeline.sub_agents]

    if includes_research(key):
        assert top == [f"gather_{key}", f"synthesis_{key}"]
        gather = pipeline.sub_agents[0]
        assert isinstance(gather, ParallelAgent)
        assert [a.name for a in gather.sub_agents] == [f"stats_{key}", f"research_{key}"]
    else:
        # Nothing to run alongside stats; a ParallelAgent of one is overhead.
        assert top == [f"stats_{key}", f"synthesis_{key}"]

    assert [a.name for a in leaf_agents(pipeline)][-1] == f"synthesis_{key}"


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_stats_agent_carries_exactly_the_declared_tools(
    engine: AdkAnalysisEngine, key: str
) -> None:
    stats = named(engine.build_pipeline(key), f"stats_{key}")
    assert [tool.name for tool in stats.tools] == list(FUNCTION_TOOL_NAMES)
    assert stats.output_key == STATS_OUTPUT_KEY


@pytest.mark.parametrize("key", [k for k in ENDPOINT_KEYS if includes_research(k)])
def test_research_agent_has_search_and_nothing_else(engine: AdkAnalysisEngine, key: str) -> None:
    """Gemini will not combine built-in search with function tools on one agent."""
    from google.adk.tools.google_search_tool import GoogleSearchTool  # noqa: PLC0415

    research = named(engine.build_pipeline(key), f"research_{key}")
    assert len(research.tools) == 1
    assert isinstance(research.tools[0], GoogleSearchTool)
    assert research.output_key == RESEARCH_OUTPUT_KEY


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_synthesis_agent_is_schema_constrained_and_tool_free(
    engine: AdkAnalysisEngine, key: str
) -> None:
    synthesis = named(engine.build_pipeline(key), f"synthesis_{key}")
    assert synthesis.tools == []
    assert synthesis.output_schema is synthesis_schema(key)
    assert synthesis.output_key == SYNTHESIS_OUTPUT_KEY


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_every_agent_uses_the_configured_model(engine: AdkAnalysisEngine, key: str) -> None:
    for agent in leaf_agents(engine.build_pipeline(key)):
        assert agent.model.model == "gemini-3.7-flash"


def test_every_agent_shares_one_model_that_retries_throttled_requests(
    engine: AdkAnalysisEngine, adk_settings: Settings
) -> None:
    """The 429 fix lives on the model object, so every agent must carry it.

    A bare model-id string is what ADK accepts and what this used to pass; it
    resolves to the same ``Gemini`` minus ``retry_options``, and google-genai
    then stops after the first attempt. One 429 in a nine-call board failed
    the run and the outer retries re-bought the finished calls
    (api/agents/vertex.py).
    """
    from google.adk.models.google_llm import Gemini

    models = {
        id(agent.model)
        for key in ENDPOINT_KEYS
        for agent in leaf_agents(engine.build_pipeline(key))
    }
    assert len(models) == 1  # one client, one policy, across every endpoint

    model = named(engine.build_pipeline("player"), "stats_player").model
    assert isinstance(model, Gemini)
    assert model.retry_options is not None
    assert model.retry_options.attempts == adk_settings.model_retry_attempts
    assert model.retry_options.initial_delay == 2.0
    assert model.retry_options.max_delay == 30.0
    # Left to the SDK default set (408, 429, 5xx): a 404 must fail fast.
    assert model.retry_options.http_status_codes is None


def test_one_attempt_means_no_retry_policy(store: Store) -> None:
    """MODEL_RETRY_ATTEMPTS=1 is the SDK's own behaviour, spelled out."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="adk",
        x402_mode="disabled",
        model_retry_attempts=1,
        season=2026,
        week_override=4,
    )
    engine = AdkAnalysisEngine(store=store, settings=settings)
    model = named(engine.build_pipeline("player"), "stats_player").model
    assert model.retry_options is None


def test_team_report_skips_research_by_design(engine: AdkAnalysisEngine) -> None:
    """Its numbers are precomputed; a news agent would only add cost and risk."""
    assert includes_research("team_report") is False
    assert engine.uses_research("team_report") is False
    # No research agent, so no parallel gather: just stats then synthesis.
    assert len(engine.build_pipeline("team_report").sub_agents) == 2
    assert len(leaf_agents(engine.build_pipeline("team_report"))) == 2


def test_research_can_be_disabled_globally(store: Store, adk_settings: Settings) -> None:
    lean = AdkAnalysisEngine(store=store, settings=adk_settings, include_research=False)
    for key in ENDPOINT_KEYS:
        assert lean.uses_research(key) is False
        assert len(lean.build_pipeline(key).sub_agents) == 2
        assert len(leaf_agents(lean.build_pipeline(key))) == 2


# -- concurrency ----------------------------------------------------------


async def test_concurrent_runs_never_see_each_others_week(
    engine: AdkAnalysisEngine, store: Store, adk_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two paying callers hit one endpoint with different weeks — no crosstalk.

    The stats agent's ``FunctionTool``\\ s are bound methods of a single cached
    :class:`~api.agents.tools.StatsTools` per endpoint, so per-run state cannot
    live on that object: request B would overwrite request A's week while A is
    still mid-pipeline. The fake ``_run_once`` reads the tools the way the agent
    would — once, then again after yielding control to the other request.
    """
    body = await DeterministicAnalysisEngine(store=store, settings=adk_settings).analyze(
        "player", {"week": 4, "season": 2026, "name": "Nobody At All"}
    )
    payload = body.model_dump(mode="json")
    payload.pop("meta")

    observed: dict[int, list[str]] = {}

    async def fake_run_once(
        pipeline: Any, endpoint_key: str, ctx: dict[str, Any], season: int, week: int
    ) -> dict[str, Any]:
        tools = engine._tools_for(endpoint_key)  # the object the agents hold
        seen = [(await tools.get_weekly_stats("1001"))["source"]]
        await asyncio.sleep(0)  # let the other request take the loop
        seen.append((await tools.get_weekly_stats("1001"))["source"])
        seen.append(f"week={tools.week}")
        observed[week] = seen
        return dict(payload)

    monkeypatch.setattr(engine, "_run_once", fake_run_once)

    await asyncio.gather(
        engine.analyze("player", {"week": 3, "season": 2026, "name": "A"}),
        engine.analyze("player", {"week": 11, "season": 2026, "name": "B"}),
    )

    assert observed[3] == ["nflverse weekly_stats 2026w3"] * 2 + ["week=3"]
    assert observed[11] == ["nflverse weekly_stats 2026w11"] * 2 + ["week=11"]


# -- structured output ----------------------------------------------------


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_synthesis_schema_is_the_contract_minus_engine_owned_fields(key: str) -> None:
    schema = synthesis_schema(key)
    contract = set(RESPONSE_MODELS[key].model_fields)
    assert set(schema.model_fields) == contract - ENGINE_OWNED_FIELDS
    assert "meta" not in schema.model_fields


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_synthesis_schema_converts_to_a_gemini_schema(key: str) -> None:
    """Structured output is only enforceable if google-genai can convert it."""
    from google.genai import _transformers  # noqa: PLC0415

    converted = _transformers.t_schema(None, synthesis_schema(key))
    assert converted is not None
    assert converted.properties


def test_synthesis_schemas_are_cached() -> None:
    """ADK and google-genai key schema conversion off the class object."""
    assert synthesis_schema("player") is synthesis_schema("player")


# -- prompts --------------------------------------------------------------


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_every_prompt_carries_the_no_uncited_stats_rule(key: str) -> None:
    """Tech spec §5's #1 quality risk, restated to all three agents."""
    for instruction in (
        stats_instruction(key),
        research_instruction(key),
        synthesis_instruction(key),
    ):
        assert NO_UNCITED_STATS_RULE in instruction


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_prompts_name_their_endpoint_and_stay_terse(key: str) -> None:
    """Token budget is a product constraint (< $0.02 per paid call)."""
    for instruction in (
        stats_instruction(key),
        research_instruction(key),
        synthesis_instruction(key),
    ):
        assert f"Endpoint: {key}" in instruction
        assert len(instruction) < 4000, "prompt is drifting past its token budget"


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
def test_synthesis_prompt_demands_the_json_contract(key: str) -> None:
    instruction = synthesis_instruction(key)
    assert "Return ONLY the JSON object matching the response schema" in instruction
    assert "stats_cited" in instruction
    assert "sources" in instruction
    assert "high | medium | low" in instruction


def test_synthesis_prompt_forbids_sources_when_no_research_ran() -> None:
    without = synthesis_instruction("report", include_research=False)
    assert "No research agent ran" in without
    assert "leave 'sources' empty" in without
    assert RESEARCH_OUTPUT_KEY not in without.split("No research agent ran")[0]


def test_prompts_cover_every_endpoint() -> None:
    assert set(ENDPOINT_PROMPTS) == set(ENDPOINT_KEYS)


def test_endpoint_prompts_carry_product_guidance() -> None:
    """Per-endpoint blocks mirror the PRD §4.2 product table."""
    assert "add / fade / hold" in ENDPOINT_PROMPTS["trending"].synthesis
    # The first gated warm flagged a 50-row board that cited one usage number:
    # the fade/hold calls must carry their justification into stats_cited.
    assert "stats_cited" in ENDPOINT_PROMPTS["trending"].synthesis
    assert "MUST name" in ENDPOINT_PROMPTS["trending"].synthesis
    assert "source" in ENDPOINT_PROMPTS["trending"].stats
    assert "candidates" in ENDPOINT_PROMPTS["sleepers"].synthesis
    assert "padding is a failure" in ENDPOINT_PROMPTS["sleepers"].synthesis
    assert "rank 1 = " in ENDPOINT_PROMPTS["matchup"].synthesis
    assert "fab_bid_pct" in ENDPOINT_PROMPTS["waivers"].synthesis
    assert "free-agent pool" in ENDPOINT_PROMPTS["roster"].synthesis
    assert "emerging" in ENDPOINT_PROMPTS["report"].synthesis
    assert "observations" in ENDPOINT_PROMPTS["team_report"].synthesis


def test_team_report_prompt_covers_the_unplayed_season() -> None:
    """The synthesis agent is told what a zeroed review means before kickoff.

    The default instruction is to reproduce 'manager_review' and
    'positional_strength_vs_league' *exactly*, which in week 1 would mean
    faithfully reproducing a "B-" computed from zero games. The route already
    strips those grades before the model sees them; this is the other half, so
    the model also knows to fill 'preseason_outlook' and how to word the rest.
    """
    synthesis = ENDPOINT_PROMPTS["team_report"].synthesis

    assert "no_matchup_history" in synthesis
    assert "preseason_outlook" in synthesis
    # The lean is a lean. Restating it as a percentage is the failure mode the
    # whole preseason block exists to avoid.
    assert (
        "not a win probability" in synthesis or "never restate it as a win probability" in synthesis
    )
    assert "NOT an ADP" in synthesis
    # A partial draft must not be graded against the full market board.
    assert "gradeable" in synthesis


def test_research_prompt_refuses_stats_tools() -> None:
    assert "You have NO stats tools" in research_instruction("player")


# -- helpers --------------------------------------------------------------


def test_run_prompt_carries_the_request() -> None:
    prompt = _run_prompt("player", {"name": "Bijan Robinson"}, 2026, 4)
    assert "Endpoint: player" in prompt
    assert "week 4" in prompt
    assert "Bijan Robinson" in prompt


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ("not json", None),
        ("", None),
    ],
)
def test_parse_json_tolerates_code_fences(text: str, expected: object) -> None:
    assert _parse_json(text) == expected


def test_event_text_ignores_non_final_and_thought_parts() -> None:
    class Part:
        def __init__(self, text: str, thought: bool = False) -> None:
            self.text = text
            self.thought = thought

    class Content:
        def __init__(self, parts: list[Part]) -> None:
            self.parts = parts

    class Event:
        def __init__(self, final: bool, parts: list[Part]) -> None:
            self._final = final
            self.content = Content(parts)

        def is_final_response(self) -> bool:
            return self._final

    assert _event_text(Event(False, [Part("body")])) == ""
    assert _event_text(Event(True, [Part("thinking", thought=True), Part("body")])) == "body"
    assert _event_text(object()) == ""


def test_retry_budget_is_one_extra_attempt() -> None:
    """Tech spec §5: retry a malformed synthesis once, then fail."""
    assert MAX_ATTEMPTS == 2


# -- import hygiene -------------------------------------------------------


def test_importing_api_agents_does_not_import_adk() -> None:
    """A deterministic-engine process must never pay the ADK import cost."""
    code = (
        "import sys, api.agents;"
        "assert not [m for m in sys.modules if m.startswith('google.adk')],"
        " sorted(m for m in sys.modules if m.startswith('google.adk'))"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_importing_the_pipeline_makes_no_network_calls(
    engine: AdkAnalysisEngine,
) -> None:
    """Building agents must not open a client.

    The model is a ``Gemini`` object now (it carries the retry policy), but
    its genai client is a ``cached_property`` that ADK evaluates on the first
    request — and CI has no credentials for that evaluation to succeed.
    """
    import functools

    pipeline = engine.build_pipeline("player")
    for agent in leaf_agents(pipeline):
        assert isinstance(type(agent.model).__dict__["api_client"], functools.cached_property)
        assert "api_client" not in vars(agent.model), "building the pipeline opened a client"


# -- candidate injection ----------------------------------------------------


async def test_the_boards_get_a_data_chosen_candidate_list(adk_settings: Settings) -> None:
    """Sleepers and the report narrate a list the scorer chose, not one the model picked."""
    from api.core.store import MemoryStore  # noqa: PLC0415
    from api.evals.golden import CONSENSUS_IDS, seed_store  # noqa: PLC0415

    seeded = await seed_store(MemoryStore())
    engine = AdkAnalysisEngine(store=seeded, settings=adk_settings)
    for key in ("sleepers", "report"):
        ctx = await engine._with_candidates(key, {"week": 4, "season": 2026}, 2026, 4)
        assert ctx["candidates"], key
        assert not {c["player_id"] for c in ctx["candidates"]} & CONSENSUS_IDS


async def test_a_caller_supplied_candidate_list_is_respected(engine: AdkAnalysisEngine) -> None:
    supplied = [{"player_id": "1", "name": "Given"}]
    ctx = await engine._with_candidates("sleepers", {"candidates": supplied}, 2026, 4)
    assert ctx["candidates"] is supplied


@pytest.mark.parametrize("key", [k for k in ENDPOINT_KEYS if k not in ("sleepers", "report")])
async def test_other_endpoints_are_left_alone(engine: AdkAnalysisEngine, key: str) -> None:
    ctx = {"week": 4}
    assert await engine._with_candidates(key, ctx, 2026, 4) == ctx


async def test_a_scorer_failure_degrades_to_no_list(
    engine: AdkAnalysisEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board with no candidate list is still a board; the quality gate will say."""
    from api.agents import deterministic  # noqa: PLC0415

    async def explode(self: object, key: str, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        raise RuntimeError("store down")

    monkeypatch.setattr(deterministic.DeterministicAnalysisEngine, "candidates", explode)
    ctx = await engine._with_candidates("sleepers", {"week": 4}, 2026, 4)
    assert "candidates" not in ctx


def test_the_board_prompts_point_at_the_candidate_list() -> None:
    for key in ("sleepers", "report"):
        assert "candidates" in ENDPOINT_PROMPTS[key].stats
        assert "candidates" in ENDPOINT_PROMPTS[key].synthesis


# -- the engine's own guards over the synthesizer -----------------------------


def test_enforce_limits_cuts_flat_boards_to_the_head() -> None:
    from api.agents.pipeline import enforce_limits

    rows = [{"player_id": str(i), "rank": i} for i in range(1, 9)]
    for key, field in (("trending", "players"), ("sleepers", "picks"), ("waivers", "board")):
        cut = enforce_limits(key, {field: list(rows)}, {"limit": 6})
        assert [r["player_id"] for r in cut[field]] == ["1", "2", "3", "4", "5", "6"]
    untouched = enforce_limits("trending", {"players": list(rows)}, {"limit": 25})
    assert len(untouched["players"]) == 8


def test_enforce_limits_walks_the_draft_board_tiers_and_filters_the_callouts() -> None:
    from api.agents.pipeline import enforce_limits

    payload = {
        "tiers": [
            {"tier": 1, "players": [{"player_id": "a"}, {"player_id": "b"}]},
            {"tier": 2, "players": [{"player_id": "c"}, {"player_id": "d"}]},
            {"tier": 3, "players": [{"player_id": "e"}]},
        ],
        "values": [{"player_id": "b"}, {"player_id": "e"}],
        "reaches": [{"player_id": "c"}, {"player_id": "d"}],
    }
    cut = enforce_limits("draft_board", payload, {"limit": 3})
    assert [[p["player_id"] for p in t["players"]] for t in cut["tiers"]] == [["a", "b"], ["c"]]
    assert [v["player_id"] for v in cut["values"]] == ["b"]
    assert [r["player_id"] for r in cut["reaches"]] == ["c"]


@pytest.mark.parametrize("limit", [None, 0, -1, "6", True])
def test_enforce_limits_ignores_a_missing_or_bad_limit(limit: Any) -> None:
    from api.agents.pipeline import enforce_limits

    rows = [{"player_id": str(i)} for i in range(3)]
    assert enforce_limits("trending", {"players": list(rows)}, {"limit": limit})["players"] == rows


async def test_sources_are_resolved_through_the_injected_resolver(
    store: Store, adk_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate judges the engine's output, so resolution belongs to the engine."""
    from api.evals.golden import GOLDEN_CASES, seed_store

    seeded = await seed_store(store)
    seen: list[list[dict[str, Any]]] = []

    async def resolver(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen.append(sources)
        return [
            {**s, "title": "Real headline", "url": "https://www.nfl.com/news/x"} for s in sources
        ]

    engine = AdkAnalysisEngine(store=seeded, settings=adk_settings, source_resolver=resolver)
    case = next(c for c in GOLDEN_CASES if c.name == "player_by_name")
    from api.agents.deterministic import DeterministicAnalysisEngine

    body = await DeterministicAnalysisEngine(store=seeded, settings=adk_settings).analyze(
        "player", dict(case.request_context)
    )
    payload = body.model_dump(mode="json")
    payload.pop("meta")
    payload["sources"] = [
        {
            "title": "nfl.com",
            "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/X",
        }
    ]

    async def fake_run_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(payload)

    monkeypatch.setattr(engine, "_run_once", fake_run_once)
    response = await engine.analyze("player", dict(case.request_context))

    assert len(seen) == 1 and seen[0][0]["title"] == "nfl.com"
    assert response.sources[0].title == "Real headline"
    assert response.sources[0].url == "https://www.nfl.com/news/x"


async def test_a_failing_resolver_keeps_the_sources_the_model_returned(
    store: Store, adk_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.evals.golden import GOLDEN_CASES, seed_store

    seeded = await seed_store(store)

    async def broken(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise RuntimeError("dns down")

    engine = AdkAnalysisEngine(store=seeded, settings=adk_settings, source_resolver=broken)
    case = next(c for c in GOLDEN_CASES if c.name == "player_by_name")
    body = await DeterministicAnalysisEngine(store=seeded, settings=adk_settings).analyze(
        "player", dict(case.request_context)
    )
    payload = body.model_dump(mode="json")
    payload.pop("meta")
    payload["sources"] = [{"title": "nfl.com", "url": "https://example.com/redirect"}]

    async def fake_run_once(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(payload)

    monkeypatch.setattr(engine, "_run_once", fake_run_once)
    response = await engine.analyze("player", dict(case.request_context))
    assert response.sources[0].title == "nfl.com"


async def test_a_quota_error_waits_before_the_one_retry(
    engine: AdkAnalysisEngine, store: Store, adk_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retrying a 429 at once spends the retry on the same throttle."""
    from api.agents import pipeline as pipeline_module
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.evals.golden import GOLDEN_CASES, seed_store

    seeded = await seed_store(store)
    case = next(c for c in GOLDEN_CASES if c.name == "player_by_name")
    body = await DeterministicAnalysisEngine(store=seeded, settings=adk_settings).analyze(
        "player", dict(case.request_context)
    )
    payload = body.model_dump(mode="json")
    payload.pop("meta")
    attempts = {"n": 0}
    slept: list[float] = []

    async def flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")
        return dict(payload)

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    engine = AdkAnalysisEngine(store=seeded, settings=adk_settings)
    monkeypatch.setattr(engine, "_run_once", flaky)
    monkeypatch.setattr(pipeline_module.asyncio, "sleep", record_sleep)

    response = await engine.analyze("player", dict(case.request_context))
    assert response.player.name == "Bijan Robinson"
    assert attempts["n"] == 2
    assert slept == [pipeline_module.QUOTA_BACKOFF_SECONDS]


async def test_a_schema_failure_retries_without_waiting(
    store: Store, adk_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.agents import pipeline as pipeline_module

    slept: list[float] = []

    async def record_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def always_garbage(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"verdict": "x"}

    engine = AdkAnalysisEngine(store=store, settings=adk_settings)
    monkeypatch.setattr(engine, "_run_once", always_garbage)
    monkeypatch.setattr(pipeline_module.asyncio, "sleep", record_sleep)
    with pytest.raises(EngineError):
        await engine.analyze("player", {"week": 4, "season": 2026, "name": "A"})
    assert slept == []


def _genai_error(code: int) -> Exception:
    errors = pytest.importorskip("google.genai.errors")
    status = {400: "INVALID_ARGUMENT", 403: "PERMISSION_DENIED", 404: "NOT_FOUND"}
    body = {"error": {"code": code, "message": "m", "status": status.get(code, "X")}}
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body)


@pytest.mark.parametrize(
    ("code", "wrap", "expected_attempts"),
    [
        (400, False, 1),
        (403, False, 1),
        (404, False, 1),
        (404, True, 1),  # inside the ParallelAgent's TaskGroup
        (429, False, 2),
        (408, False, 2),
        (500, False, 2),
        (503, True, 2),
    ],
)
async def test_a_client_error_is_not_retried_but_throttles_and_5xx_are(
    store: Store,
    adk_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    wrap: bool,
    expected_attempts: int,
) -> None:
    """A 404 (model at the wrong location) fails identically twice, at twice the cost."""
    from api.agents import pipeline as pipeline_module

    attempts = {"n": 0}

    async def failing(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts["n"] += 1
        error = _genai_error(code)
        if wrap:
            raise ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [error])
        raise error

    async def no_sleep(seconds: float) -> None:
        return None

    engine = AdkAnalysisEngine(store=store, settings=adk_settings)
    monkeypatch.setattr(engine, "_run_once", failing)
    monkeypatch.setattr(pipeline_module.asyncio, "sleep", no_sleep)
    with pytest.raises(EngineError):
        await engine.analyze("player", {"week": 4, "season": 2026, "name": "A"})
    assert attempts["n"] == expected_attempts


def test_permanent_errors_need_every_leaf_to_be_a_client_error() -> None:
    from api.agents.pipeline import _is_permanent_error

    not_found = _genai_error(404)
    assert _is_permanent_error(not_found)
    assert not _is_permanent_error(ExceptionGroup("g", [not_found, _genai_error(429)]))
    assert not _is_permanent_error(ExceptionGroup("g", [not_found, ValueError("schema")]))
    assert not _is_permanent_error(ValueError("schema"))
    assert not _is_permanent_error(TimeoutError())
    try:
        raise RuntimeError("adk wrapped it") from not_found
    except RuntimeError as chained:
        assert _is_permanent_error(chained)


def test_quota_errors_are_found_inside_exception_groups() -> None:
    """The ParallelAgent wraps a 429 in a TaskGroup; the backoff must still see it."""
    from api.agents.pipeline import _describe, _is_quota_error

    quota = RuntimeError("429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}")
    wrapped = ExceptionGroup("unhandled errors in a TaskGroup (1 sub-exception)", [quota])
    nested = ExceptionGroup("outer", [ExceptionGroup("inner", [quota])])
    assert _is_quota_error(quota)
    assert _is_quota_error(wrapped)
    assert _is_quota_error(nested)
    assert not _is_quota_error(ExceptionGroup("g", [ValueError("schema")]))
    assert "RESOURCE_EXHAUSTED" in _describe(wrapped)
    assert "TaskGroup" not in _describe(wrapped)


def test_prompts_carry_the_eval_obligations() -> None:
    """Each of these was a real ADK eval failure on 2026-09-03."""
    from api.agents.prompts import SYNTHESIS_CONTRACT

    assert "never as a tool field name" in SYNTHESIS_CONTRACT
    assert "must also appear in stats_cited" in SYNTHESIS_CONTRACT
    assert (
        "every delta you quote in it goes into stats_cited" in ENDPOINT_PROMPTS["player"].synthesis
    )
    assert "'sleeper league matchups (team_analytics)'" in ENDPOINT_PROMPTS["team_report"].synthesis
    assert "Only N candidates cleared the bar this week." in ENDPOINT_PROMPTS["sleepers"].synthesis
    assert "player_id as an empty string" in ENDPOINT_PROMPTS["player"].synthesis
    assert "'usage_trajectory' is null" in ENDPOINT_PROMPTS["player"].synthesis
    assert "not a consensus ADP" in ENDPOINT_PROMPTS["draft_board"].synthesis
    for key in ("trending", "sleepers", "waivers"):
        assert "request.limit" in ENDPOINT_PROMPTS[key].synthesis


# -- computed facts ---------------------------------------------------------
#
# team_report hands the synthesizer a finished table of deterministic arithmetic
# and asks it to "reproduce the precomputed values exactly". These make that a
# guarantee rather than an instruction — the draft board's lesson applied to the
# one other endpoint that works this way.


def _analytics() -> dict[str, Any]:
    return {
        "manager_review": {
            "bench_points_lost": 41.6,
            "lineup_efficiency_pct": 91.3,
            "luck_note": "Third in points for, fifth in the standings.",
            "mis_start_patterns": [
                "Benched the higher-projected TE in two of three weeks (-11.4)."
            ],
            "observations": [],
            "worst_week": 3,  # an extra beyond the schema, for the narrator
        },
        "positional_strength_vs_league": [
            {"position": "WR", "grade": "C+", "points_per_week": 9.7}
        ],
    }


def test_computed_blocks_replace_whatever_the_model_wrote() -> None:
    from api.agents.pipeline import enforce_computed_facts

    facts = _analytics()
    drifted = {
        "manager_review": {
            "bench_points_lost": 40.0,  # close enough to look right
            "lineup_efficiency_pct": 91.3,
            "luck_note": "Third in points for, fifth in the standings.",
            "mis_start_patterns": ["Benched the higher-projected TE in two of three weeks (-9.9)."],
            "observations": ["Starts the same flex every week."],
        },
        "positional_strength_vs_league": [
            {"position": "WR", "grade": "B", "points_per_week": 11.0},
        ],
    }
    out = enforce_computed_facts("team_report", dict(drifted), {"team_analytics": facts})

    assert out["manager_review"]["bench_points_lost"] == 41.6
    assert (
        out["manager_review"]["mis_start_patterns"] == facts["manager_review"]["mis_start_patterns"]
    )
    assert out["positional_strength_vs_league"] == facts["positional_strength_vs_league"]
    # The one block the model is invited to write survives the overwrite.
    assert out["manager_review"]["observations"] == ["Starts the same flex every week."]


def test_computed_facts_leave_other_endpoints_alone() -> None:
    from api.agents.pipeline import enforce_computed_facts

    payload = {"players": [{"name": "Bijan Robinson"}]}
    assert (
        enforce_computed_facts("trending", dict(payload), {"team_analytics": _analytics()})
        == payload
    )


@pytest.mark.parametrize("ctx", [{}, {"team_analytics": None}, {"team_analytics": "nope"}])
def test_computed_facts_need_analytics_to_enforce_anything(ctx: dict[str, Any]) -> None:
    """The no-history path is documented; there is nothing truer to substitute."""
    from api.agents.pipeline import enforce_computed_facts

    payload = {"manager_review": {"bench_points_lost": 1.0}}
    assert enforce_computed_facts("team_report", dict(payload), ctx) == payload


def test_no_planner_means_the_models_own_thinking(engine: AdkAnalysisEngine) -> None:
    """The shipped default: every agent decides its own thinking budget."""
    for agent in leaf_agents(engine.build_pipeline("player")):
        assert agent.planner is None


def test_the_thinking_cap_reaches_every_agent(store: Store) -> None:
    """ADK sets thinking through a planner, not the model, so it has to be
    attached per agent — including the research agent, which is the one most
    likely to be added later and forgotten."""
    from google.adk.planners import BuiltInPlanner

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="adk",
        x402_mode="disabled",
        model_id="gemini-3.7-flash",
        model_thinking_level="low",
        season=2026,
        week_override=4,
    )
    engine = AdkAnalysisEngine(store=store, settings=settings)
    agents = leaf_agents(engine.build_pipeline("trending"))
    assert len(agents) >= 2
    planners = {id(a.planner) for a in agents}
    assert len(planners) == 1, "one planner shared, not one per agent"
    for agent in agents:
        assert isinstance(agent.planner, BuiltInPlanner)
        assert agent.planner.thinking_config.thinking_level == "LOW"
