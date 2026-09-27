"""The stats tools: provenance on every result, and no exceptions on bad input.

These two properties are what the whole citation story rests on — a tool result
with no ``source`` cannot be cited honestly, and a tool that raises turns a paid
call into a 500 instead of an honest "I couldn't find that player".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from api.agents.tools import FUNCTION_TOOL_NAMES, StatsTools, _best_candidate, run_scope
from api.core.store import MemoryStore, Store
from api.data.stats_store import (
    DEF_VS_POS_COLLECTION,
    META_COLLECTION,
    PLAYER_INDEX_COLLECTION,
    PLAYERS_COLLECTION,
    SCHEDULES_COLLECTION,
    TRENDING_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    weekly_stats_collection,
)

SEASON = 2026
WEEK = 3


@pytest.fixture
async def tools(store: Store) -> StatsTools:
    """A minimal but complete slice of ingested data."""
    await store.set(
        PLAYERS_COLLECTION,
        "4046",
        {
            "player_id": "4046",
            "name": "Patrick Mahomes",
            "position": "QB",
            "team": "KC",
            "injury_status": None,
            "years_exp": 8,
        },
    )
    await store.set(
        PLAYER_INDEX_COLLECTION,
        "josh allen",
        {
            "candidates": [
                {"player_id": "5045", "name": "Josh Allen", "team": "JAX", "position": "LB"},
                {"player_id": "4984", "name": "Josh Allen", "team": "BUF", "position": "QB"},
            ]
        },
    )
    await store.set(
        weekly_stats_collection(SEASON, 3),
        "4046",
        {"player_id": "4046", "week": 3, "fantasy_points_ppr": 21.4, "snap_pct": 0.98},
    )
    await store.set(
        USAGE_TRENDS_COLLECTION,
        "4046",
        {"player_id": "4046", "snap_pct_l4w": 0.96, "trend": "flat"},
    )
    await store.set(
        DEF_VS_POS_COLLECTION,
        "ATL",
        {"team": "ATL", "positions": {"QB": {"points_allowed_per_game": 19.8, "rank": 7}}},
    )
    await store.set(
        TRENDING_COLLECTION,
        "add",
        {"kind": "add", "entries": [{"player_id": "4046", "count": 51234}]},
    )
    await store.set(
        SCHEDULES_COLLECTION,
        f"{SEASON}_{WEEK}",
        {"games": [{"home": "KC", "away": "ATL", "kickoff": "2026-09-21T17:00:00Z"}]},
    )
    await store.set(META_COLLECTION, "freshness", {"weekly_stats": "2026-09-16T09:02:11Z"})
    return StatsTools(store, season=SEASON, week=WEEK)


async def _call_all(tools: StatsTools, *, known: bool) -> dict[str, dict[str, Any]]:
    """Invoke every function tool, with either real or nonexistent arguments."""
    pid = "4046" if known else "0000"
    team = "ATL" if known else "ZZZ"
    return {
        "resolve_player": await tools.resolve_player("Josh Allen" if known else "Nobody At All"),
        "get_player": await tools.get_player(pid),
        "get_weekly_stats": await tools.get_weekly_stats(pid),
        "get_usage_trends": await tools.get_usage_trends(pid),
        "get_def_vs_pos": await tools.get_def_vs_pos(team, "QB"),
        "get_trending": await tools.get_trending("add" if known else "sideways"),
        "get_schedule": await tools.get_schedule("KC" if known else "ZZZ"),
        "get_data_freshness": await tools.get_data_freshness(),
    }


# -- provenance -----------------------------------------------------------


async def test_every_tool_result_carries_provenance(tools: StatsTools) -> None:
    """Without a 'source' string a number cannot be cited, so this is load-bearing."""
    for name, result in (await _call_all(tools, known=True)).items():
        assert isinstance(result, dict), name
        assert result.get("source"), f"{name} returned no provenance"


async def test_provenance_survives_missing_data(tools: StatsTools) -> None:
    """A miss still says which dataset was consulted."""
    for name, result in (await _call_all(tools, known=False)).items():
        assert result.get("source"), f"{name} dropped provenance on a miss"


async def test_provenance_strings_name_the_slice(tools: StatsTools) -> None:
    assert (await tools.get_weekly_stats("4046"))["lines"][0]["source"] == (
        "nflverse weekly_stats 2026w3"
    )
    assert (await tools.get_usage_trends("4046"))["source"] == "nflverse usage_trends/4046"
    assert (await tools.get_def_vs_pos("ATL", "QB"))["source"] == "nflverse def_vs_pos/ATL"
    assert (await tools.get_trending("add"))["source"] == "sleeper trending/add"
    assert (await tools.get_schedule("KC"))["source"] == "nflverse schedules/2026w3"
    assert (await tools.get_data_freshness())["source"] == "meta/freshness"


async def test_trending_entries_carry_their_own_source(tools: StatsTools) -> None:
    entries = (await tools.get_trending("add"))["entries"]
    assert entries and all(entry["source"] == "sleeper trending/add" for entry in entries)


# -- graceful degradation -------------------------------------------------


async def test_unknown_inputs_return_empty_results_not_exceptions(tools: StatsTools) -> None:
    results = await _call_all(tools, known=False)
    assert results["resolve_player"] == {
        "found": False,
        "query": "Nobody At All",
        "candidates": [],
        "best": None,
        "ambiguous": False,
        "source": "sleeper player_index/nobody at all",
    }
    assert results["get_player"]["found"] is False
    assert results["get_player"]["player"] == {}
    assert results["get_weekly_stats"]["lines"] == []
    assert results["get_usage_trends"]["usage"] == {}
    assert results["get_def_vs_pos"]["split"] == {}
    assert results["get_schedule"]["game"] == {}


async def test_bad_trending_kind_is_reported_not_raised(tools: StatsTools) -> None:
    """stats_store raises on a bad kind; the tool layer must absorb that."""
    result = await tools.get_trending("sideways")
    assert result["found"] is False
    assert result["entries"] == []
    assert "error" in result


async def test_empty_name_resolves_to_nothing(tools: StatsTools) -> None:
    assert (await tools.resolve_player(""))["found"] is False


async def test_results_are_json_safe(tools: StatsTools) -> None:
    """Gemini function-calling only sees plain JSON, never pydantic or datetimes."""
    import json  # noqa: PLC0415

    json.dumps(await _call_all(tools, known=True))


# -- resolution semantics -------------------------------------------------


async def test_ambiguity_is_reported_and_the_fantasy_player_preferred(tools: StatsTools) -> None:
    """The linebacker is listed first in the index; the QB must still win."""
    result = await tools.resolve_player("Josh Allen")
    assert result["ambiguous"] is True
    assert len(result["candidates"]) == 2
    assert result["best"]["player_id"] == "4984"
    assert result["best"]["position"] == "QB"


def test_best_candidate_falls_back_when_nobody_is_fantasy_relevant() -> None:
    candidates = [
        {"player_id": "1", "position": "LB"},
        {"player_id": "2", "position": "CB"},
    ]
    assert _best_candidate(candidates)["player_id"] == "1"
    assert _best_candidate([]) is None


async def test_weekly_stats_defaults_to_the_last_four_weeks(tools: StatsTools) -> None:
    result = await tools.get_weekly_stats("4046")
    assert result["weeks_requested"] == [1, 2, 3]  # week 3 run, floored at week 1
    assert [line["week"] for line in result["lines"]] == [3]  # only week 3 is ingested


async def test_recent_weeks_never_runs_below_week_one(store: Store) -> None:
    assert StatsTools(store, season=SEASON, week=2).recent_weeks() == [1, 2]
    assert StatsTools(store, season=SEASON, week=9).recent_weeks() == [6, 7, 8, 9]


# -- ADK wiring -----------------------------------------------------------


def test_function_tool_names_are_all_real_methods(store: Store) -> None:
    tools = StatsTools(store, season=SEASON, week=WEEK)
    for name in FUNCTION_TOOL_NAMES:
        assert callable(getattr(tools, name)), name


def test_function_tools_expose_exactly_the_declared_set(store: Store) -> None:
    """The names ADK advertises to Gemini must match the documented tool list."""
    built = StatsTools(store, season=SEASON, week=WEEK).function_tools()
    assert [tool.name for tool in built] == list(FUNCTION_TOOL_NAMES)
    for tool in built:
        assert tool.description, f"{tool.name} has no docstring for the model to read"


def test_scans_are_not_exposed_to_the_model(store: Store) -> None:
    """Collection scans are engine-internal — an LLM must not pull whole tables."""
    assert "scan_usage_trends" not in FUNCTION_TOOL_NAMES
    assert "scan_players" not in FUNCTION_TOOL_NAMES


# -- per-run scope --------------------------------------------------------


def test_run_scope_overrides_the_instance_defaults(store: Store) -> None:
    tools = StatsTools(store, season=SEASON, week=WEEK)

    with run_scope(2030, 12):
        assert (tools.season, tools.week) == (2030, 12)
        assert tools.recent_weeks() == [9, 10, 11, 12]
    assert (tools.season, tools.week) == (SEASON, WEEK)


async def test_concurrent_scopes_do_not_leak_between_tasks(store: Store) -> None:
    """One shared tools object, two runs: each task must read its own week."""
    tools = StatsTools(store, season=SEASON, week=WEEK)

    async def run(week: int) -> list[int]:
        with run_scope(SEASON, week):
            seen = [tools.week]
            await asyncio.sleep(0)  # hand the loop to the other run
            seen.append(tools.week)
            return seen

    assert await asyncio.gather(run(1), run(17)) == [[1, 1], [17, 17]]


# -- model arguments are anything at all ----------------------------------


@pytest.mark.parametrize("team", [None, "", "K/C", 12, "all teams"])
async def test_team_tools_report_a_bad_team_instead_of_raising(
    tools: StatsTools, team: Any
) -> None:
    for result in (
        await tools.get_def_vs_pos(team, "RB"),
        await tools.get_depth_chart(team, "RB"),
        await tools.get_schedule(team),
    ):
        assert result["found"] is False
        assert "error" in result


async def test_a_bad_position_is_reported_not_raised(tools: StatsTools) -> None:
    result = await tools.get_def_vs_pos("ATL", None)
    assert result["found"] is False and "error" in result


async def test_team_codes_are_normalised(tools: StatsTools) -> None:
    result = await tools.get_def_vs_pos(" atl ", "qb")
    assert result["team"] == "ATL" and result["position"] == "QB"
    assert result["found"] is True


@pytest.mark.parametrize(("limit", "expected_error"), [("all", True), ("1", False), (None, False)])
async def test_trending_limit_is_coerced_or_reported(
    tools: StatsTools, limit: Any, expected_error: bool
) -> None:
    result = await tools.get_trending("add", limit=limit)
    assert ("error" in result) is expected_error
    if not expected_error:
        assert result["found"] is True


@pytest.mark.parametrize(("limit", "expected_error"), [("all", True), ("5", False), (None, False)])
async def test_draft_pool_limit_is_coerced_or_reported(
    tools: StatsTools, limit: Any, expected_error: bool
) -> None:
    result = await tools.get_draft_pool(limit=limit)
    assert ("error" in result) is expected_error
    assert result["players"] == [] if expected_error else isinstance(result["players"], list)


async def test_schedule_week_must_be_a_number(tools: StatsTools) -> None:
    assert "error" in await tools.get_schedule("KC", week="next")
    assert "error" not in await tools.get_schedule("KC", week="3")


class _FirestoreLikeStore(MemoryStore):
    """Rejects the doc ids Firestore rejects, the way Firestore does: by raising."""

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        if not doc_id or "/" in doc_id:
            raise ValueError(f"invalid document id {doc_id!r}")
        return await super().get(collection, doc_id)


@pytest.mark.parametrize("player_id", ["", "  ", "4046/../x", None, True, ["4046"]])
async def test_player_tools_report_a_bad_id_instead_of_raising(player_id: Any) -> None:
    """One junk id from the model must not fail the whole ADK run."""
    tools = StatsTools(_FirestoreLikeStore(), season=SEASON, week=WEEK)
    for result in (
        await tools.get_weekly_stats(player_id),
        await tools.get_usage_trends(player_id),
    ):
        assert result["found"] is False
        assert "error" in result


async def test_weekly_stats_coerces_string_weeks_and_season(tools: StatsTools) -> None:
    result = await tools.get_weekly_stats(4046, weeks=["3", 3.0], season="2026")  # type: ignore[arg-type]
    assert result["found"] is True
    assert result["season"] == SEASON
    assert result["weeks_requested"] == [3, 3]
    assert "error" not in await tools.get_weekly_stats("4046", weeks="3")  # type: ignore[arg-type]


@pytest.mark.parametrize(("weeks", "season"), [(["next"], 0), ([0], 0), (None, "last"), ([2.5], 0)])
async def test_weekly_stats_reports_junk_weeks_or_season(
    tools: StatsTools, weeks: Any, season: Any
) -> None:
    result = await tools.get_weekly_stats("4046", weeks=weeks, season=season)
    assert result["found"] is False and "error" in result
    assert result["lines"] == []
