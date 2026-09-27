"""The golden-query suite itself.

Two jobs: run the deterministic suite end-to-end as a CI gate, and prove the
suite has teeth — an eval that cannot fail is not a gate, so the traceability
checker is exercised against deliberately dishonest input.
"""

from __future__ import annotations

import pytest

from api.agents.engine import RESPONSE_MODELS
from api.core.config import ENDPOINT_KEYS
from api.core.store import MemoryStore
from api.data.stats_store import (
    DEF_VS_POS_COLLECTION,
    PLAYERS_COLLECTION,
    TRENDING_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    normalize_name,
    resolve_player,
)
from api.evals.golden import (
    CONTEXT_SOURCE,
    GOLDEN_CASES,
    PLAYERS,
    SEASON,
    TRENDING_ADD,
    WEEK,
    GoldenCase,
    build_value_index,
    check_citations_traceable,
    seed_store,
    universal_assertions,
)
from api.evals.run_evals import (
    CREDENTIAL_ENV_VARS,
    EvalReport,
    build_engine,
    main_async,
    missing_credentials,
    run_case,
    run_evals,
)
from api.schemas import StatCitation


@pytest.fixture
async def seeded() -> MemoryStore:
    store = MemoryStore()
    await seed_store(store)
    return store


# -- the gate -------------------------------------------------------------


async def test_deterministic_golden_run_passes(seeded: MemoryStore) -> None:
    """The CI gate itself: 20/20 against the LLM-free engine."""
    engine = await build_engine("deterministic", seeded)
    report = await run_evals(engine)
    assert report.passed, report.render(verbose=True)
    assert len(report.results) == 23


async def test_cli_entry_point_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await main_async(["--engine", "deterministic"]) == 0


async def test_cli_rejects_an_unknown_case_name() -> None:
    assert await main_async(["--case", "no_such_case"]) == 1


async def test_cli_can_run_a_single_case() -> None:
    assert await main_async(["--case", "player_by_name", "--verbose"]) == 0


def test_adk_run_is_skipped_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    reason = missing_credentials()
    assert reason and "credentials" in reason
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "some-project")
    assert missing_credentials() is None


async def test_skipped_report_renders_and_counts_as_passing() -> None:
    report = EvalReport(engine_name="adk", skipped_reason="no creds")
    assert report.passed
    assert "SKIPPED" in report.render()


# -- the suite has teeth --------------------------------------------------


async def test_a_lying_engine_fails_the_suite(seeded: MemoryStore) -> None:
    """The whole point: a fabricated number must not survive the gate."""
    honest = await build_engine("deterministic", seeded)

    class Fabricator:
        name = "fabricator"

        async def analyze(self, endpoint_key: str, request_context: dict) -> object:
            response = await honest.analyze(endpoint_key, request_context)
            response.stats_cited.append(
                StatCitation(
                    stat="target_share",
                    value=0.99,
                    player="Bijan Robinson",
                    source="nflverse weekly_stats 2026w3",
                )
            )
            return response

    report = await run_evals(Fabricator(), GOLDEN_CASES[:3])  # type: ignore[arg-type]
    assert not report.passed
    assert all("UNTRACEABLE" in " ".join(result.failures) for result in report.results)


async def test_a_raising_engine_is_a_failed_case_not_a_crash() -> None:
    class Broken:
        name = "broken"

        async def analyze(self, endpoint_key: str, request_context: dict) -> object:
            raise RuntimeError("vertex is down")

    result = await run_case(Broken(), GOLDEN_CASES[0])  # type: ignore[arg-type]
    assert not result.passed
    assert "vertex is down" in (result.error or "")


@pytest.mark.parametrize(
    ("citation", "expected_failure"),
    [
        (
            StatCitation(
                stat="target_share_l4w",
                value=0.42,
                player="Bijan Robinson",
                source="nflverse usage_trends/1001",
            ),
            "UNTRACEABLE",
        ),
        (
            # A real number, but belonging to a different player.
            StatCitation(
                stat="snap_pct_l4w",
                value=0.94,
                player="Bijan Robinson",
                source="nflverse usage_trends/1001",
            ),
            "UNTRACEABLE",
        ),
        (
            StatCitation(stat="vibes", value=7, player="Bijan Robinson", source="my memory"),
            "unrecognised source",
        ),
        (
            StatCitation(stat="vibes", value=7, player="Bijan Robinson", source="  "),
            "no source",
        ),
    ],
)
def test_traceability_rejects_dishonest_citations(
    citation: StatCitation, expected_failure: str
) -> None:
    failures = check_citations_traceable([citation], {})
    assert failures and expected_failure in failures[0]


def test_traceability_accepts_real_fixture_values() -> None:
    honest = [
        StatCitation(
            stat="snap_pct_l4w",
            value=0.82,
            player="Bijan Robinson",
            source="nflverse usage_trends/1001",
        ),
        StatCitation(
            stat="fantasy_points",
            value=25.6,
            player="Bijan Robinson",
            source="nflverse weekly_stats 2026w3",
        ),
        StatCitation(
            stat="def_vs_pos_rank_RB", value=2, player=None, source="nflverse def_vs_pos/CIN"
        ),
        StatCitation(
            stat="trend_count", value=45210, player="Bhayshul Tuten", source="sleeper trending/add"
        ),
    ]
    assert check_citations_traceable(honest, {}) == []


def test_route_computed_values_trace_to_the_request_not_the_store() -> None:
    """Team analytics are Python output passed in on the request (tech spec §6)."""
    citation = StatCitation(
        stat="points_per_week_WR", value=18.9, player="playclock_ryan", source=CONTEXT_SOURCE
    )
    assert check_citations_traceable([citation], {"analytics": {"points_per_week": 18.9}}) == []
    assert check_citations_traceable([citation], {}) != []


def test_team_level_citations_check_against_the_defensive_splits() -> None:
    good = StatCitation(stat="rank", value=7, player=None, source="nflverse def_vs_pos/CHI")
    bad = StatCitation(stat="rank", value=99, player=None, source="nflverse def_vs_pos/CHI")
    assert check_citations_traceable([good], {}) == []
    assert check_citations_traceable([bad], {}) != []


def test_value_index_is_built_from_the_same_constants_as_the_seed() -> None:
    by_player, team_level = build_value_index()
    assert set(by_player) == {name for name, *_ in PLAYERS.values()}
    assert team_level


# -- the suite is complete ------------------------------------------------


def test_the_golden_set_covers_every_endpoint() -> None:
    # Tech spec §5 fixed 20 queries across the original eight endpoints; the
    # draft pair added three more. The number is pinned so a dropped case is a
    # test failure rather than a quietly smaller quality gate.
    assert len(GOLDEN_CASES) == 23


def test_case_names_are_unique() -> None:
    names = [case.name for case in GOLDEN_CASES]
    assert len(set(names)) == len(names)


def test_every_paid_endpoint_is_exercised() -> None:
    assert {case.endpoint_key for case in GOLDEN_CASES} == set(ENDPOINT_KEYS)
    assert {case.endpoint_key for case in GOLDEN_CASES} == set(RESPONSE_MODELS)


def test_every_case_states_a_human_intent() -> None:
    for case in GOLDEN_CASES:
        assert case.intent.strip(), case.name


def test_failure_modes_are_represented() -> None:
    """A suite of only happy paths would not catch the ways this actually breaks."""
    names = {case.name for case in GOLDEN_CASES}
    assert "player_unknown_name" in names
    assert "player_ambiguous_name" in names
    assert "matchup_with_unknown_player" in names
    assert "team_report_without_analytics" in names
    assert "player_without_usage_rollup" in names


# -- the fixture ----------------------------------------------------------


async def test_seed_populates_every_collection_the_engines_read(
    seeded: MemoryStore,
) -> None:
    assert await seeded.get(PLAYERS_COLLECTION, "1001")
    assert await seeded.get(USAGE_TRENDS_COLLECTION, "1001")
    assert await seeded.get(DEF_VS_POS_COLLECTION, "CIN")
    assert await seeded.get(TRENDING_COLLECTION, "add")
    assert await seeded.get(TRENDING_COLLECTION, "drop")
    assert await seeded.get("meta", "freshness")
    assert await seeded.get("meta", "schedule_weeks")
    assert await seeded.get(f"weekly_stats/{SEASON}_1/players", "1001")
    assert await seeded.get("schedules", f"{SEASON}_{WEEK}")


async def test_seed_indexes_the_ambiguous_name(seeded: MemoryStore) -> None:
    candidates = await resolve_player(seeded, "Josh Allen")
    assert len(candidates) == 2
    assert {c["position"] for c in candidates} == {"QB", "LB"}
    # The linebacker is listed first on purpose, so preference logic is tested.
    assert candidates[0]["position"] == "LB"


async def test_seed_is_idempotent(seeded: MemoryStore) -> None:
    before = await seeded.list(PLAYERS_COLLECTION)
    await seed_store(seeded)
    assert len(await seeded.list(PLAYERS_COLLECTION)) == len(before)


def test_fixture_has_enough_sleeper_candidates() -> None:
    """PRD §4.2 promises 8-12 picks; the fixture must be able to produce them."""
    consensus = {pid for pid, count in TRENDING_ADD if count >= 3000}
    from api.evals.golden import USAGE  # noqa: PLC0415

    eligible = [
        pid
        for pid, values in USAGE.items()
        if pid not in consensus and (values[3] > 0 or values[4] > 0)
    ]
    assert len(eligible) >= 8


def test_fixture_names_normalize_to_distinct_index_keys() -> None:
    keys = {normalize_name(name) for name, *_ in PLAYERS.values()}
    # 19 players, two of whom share "josh allen".
    assert len(keys) == len(PLAYERS) - 1


# -- assertion plumbing ---------------------------------------------------


async def test_universal_assertions_catch_a_missing_freshness_block(
    seeded: MemoryStore,
) -> None:
    """A response that cannot say how stale it is fails the trust promise."""
    engine = await build_engine("deterministic", seeded)
    case: GoldenCase = GOLDEN_CASES[0]
    response = await engine.analyze(case.endpoint_key, dict(case.request_context))
    assert universal_assertions(response, case) == []
    response.meta.data_freshness = {}
    assert any("data_freshness" in failure for failure in universal_assertions(response, case))
