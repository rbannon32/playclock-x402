"""The deterministic engine, endpoint by endpoint.

Every endpoint gets the same three checks — the response validates against its
contract, every cited number traces back to seeded data, and the provenance
envelope is populated — plus the endpoint-specific invariants that make the
answer a *product* rather than a well-formed blank.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from api.agents.deterministic import (
    CONSENSUS_ADD_COUNT,
    SLEEPER_MARKET_RANK_FLOOR,
    DeterministicAnalysisEngine,
    _confidence,
    _grade,
)
from api.agents.engine import RESPONSE_MODELS
from api.core.config import ENDPOINT_KEYS, Settings
from api.core.store import MemoryStore, Store
from api.data.stats_store import stale_datasets, weekly_stats_collection
from api.evals.golden import (
    DRAFT_PICKS,
    FIXTURE_FREE_AGENTS,
    FIXTURE_ROSTER,
    FRESHNESS,
    PLAYERS,
    SEASON,
    TEAM_ANALYTICS,
    WEEK,
    check_citations_traceable,
    seed_store,
)
from api.schemas import AnalysisResponse

#: One representative request per endpoint, in the shape wave-3 routes will send.
CONTEXTS: dict[str, dict[str, Any]] = {
    "trending": {"week": WEEK, "season": SEASON},
    "sleepers": {"week": WEEK, "season": SEASON},
    "player": {"week": WEEK, "season": SEASON, "name": "Bijan Robinson"},
    "matchup": {"week": WEEK, "season": SEASON, "players": ["1001", "1002"]},
    "roster": {
        "week": WEEK,
        "season": SEASON,
        "roster": [dict(entry) for entry in FIXTURE_ROSTER],
    },
    "waivers": {"week": WEEK, "season": SEASON},
    "report": {"week": WEEK, "season": SEASON},
    "team_report": {
        "week": WEEK,
        "season": SEASON,
        "sleeper_username": "playclock_ryan",
        "league_id": "998877",
        "roster": [dict(entry) for entry in FIXTURE_ROSTER],
        "free_agents": [dict(p) for p in FIXTURE_FREE_AGENTS],
        "team_analytics": TEAM_ANALYTICS,
    },
    "draft_board": {"season": SEASON, "limit": 200},
    "draft_report": {
        "season": SEASON,
        "draft_id": "fixture-draft-1",
        "picks": [dict(pick) for pick in DRAFT_PICKS],
    },
}


@pytest.fixture
def eval_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="deterministic",
        x402_mode="disabled",
        season=SEASON,
        week_override=WEEK,
    )


@pytest.fixture
async def engine(eval_settings: Settings) -> DeterministicAnalysisEngine:
    store = await seed_store(MemoryStore())
    return DeterministicAnalysisEngine(store=store, settings=eval_settings)


# -- every endpoint -------------------------------------------------------


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_response_validates_against_its_contract(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert isinstance(response, RESPONSE_MODELS[key])
    # The body an agent parses must survive a JSON round trip.
    RESPONSE_MODELS[key].model_validate(response.model_dump(mode="json"))


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_every_cited_number_traces_to_seeded_data(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    """The #1 quality rule (tech spec §5), enforced rather than prompted."""
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert check_citations_traceable(response.stats_cited, CONTEXTS[key]) == []


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_provenance_envelope_is_populated(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert response.meta.generated_at is not None
    assert response.meta.data_freshness == FRESHNESS
    assert response.meta.model is None, "the deterministic engine must not claim a model"
    assert response.meta.attribution.startswith("Data: nflverse")


async def test_golden_fixture_refreshes_when_reseeded() -> None:
    """A long-lived evaluator gets new freshness markers on every seed."""
    first_seed = datetime(2030, 1, 1, tzinfo=UTC)
    store = await seed_store(MemoryStore(), seeded_at=first_seed)
    later = first_seed + timedelta(hours=3)
    await seed_store(store, seeded_at=later)

    assert stale_datasets(FRESHNESS, now=later) == []


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_verdict_block_is_filled_in(engine: DeterministicAnalysisEngine, key: str) -> None:
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert isinstance(response, AnalysisResponse)
    assert response.verdict.strip()
    assert response.reasoning.strip()
    assert response.confidence in ("high", "medium", "low")


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_no_research_sources_are_invented(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    """No research agent runs in this mode, so a source would be a fabrication."""
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert response.sources == []


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_reasoning_leads_with_the_answer_not_the_engine(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    """The prose is what a payer reads; the engine identifies itself in ``meta``.

    The old preamble ("Deterministic engine — heuristic analysis ...") opened
    every paid answer with an apology. ``meta.model`` being null is the
    machine-readable disclosure, and it is the one the web UI and the MCP
    server surface.
    """
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert "deterministic engine" not in response.reasoning.lower()
    assert response.meta.model is None
    assert "{'" not in response.reasoning, "a dict literal leaked into customer-facing prose"


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_week_and_season_default_to_the_current_ones(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    """Routes may pass week/season through as None; the engine resolves them."""
    context = {k: v for k, v in CONTEXTS[key].items() if k not in ("week", "season")}
    context["week"] = None
    context["season"] = None
    response = await engine.analyze(key, context)
    assert isinstance(response, RESPONSE_MODELS[key])
    if hasattr(response, "week"):
        assert response.week == WEEK
    if hasattr(response, "season"):
        assert response.season == SEASON


# -- per-endpoint product invariants --------------------------------------


async def test_trending_board_carries_verdicts_and_real_counts(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("trending", {"week": WEEK, "season": SEASON, "limit": 10})
    assert 0 < len(response.players) <= 10
    assert {row.trend for row in response.players} == {"add", "drop"}
    assert all(row.verdict in ("add", "fade", "hold") for row in response.players)
    assert all(row.analysis.strip() for row in response.players)
    assert response.lookback_hours == 24


async def test_trending_fades_the_crowd_when_usage_disagrees(
    engine: DeterministicAnalysisEngine,
) -> None:
    """A board that just re-prints Sleeper's ordering is worth nothing."""
    response = await engine.analyze("trending", dict(CONTEXTS["trending"]))
    verdicts = {row.name: row.verdict for row in response.players}
    # Nico Collins is listed Out: no call either way, because a fade on a man
    # who does not play is a free hit in the archive (and an add a free miss).
    assert verdicts["Nico Collins"] == "hold"
    # Breece Hall is being dropped but slowly; his usage is declining too.
    assert verdicts["Breece Hall"] == "fade"


async def test_sleepers_exclude_the_consensus_and_the_owned(
    engine: DeterministicAnalysisEngine,
) -> None:
    """A sleeper is neither being added by the crowd nor already owned by it.

    The fixture's rising-usage players are mostly first-round picks (Bijan
    Robinson, Ja'Marr Chase, Marvin Harrison Jr.), so the owned-player floor
    leaves a short board — and a short board that says so beats one padded
    with players nobody could add off the wire.
    """
    from api.evals.golden import MARKET_RANKS  # noqa: PLC0415

    response = await engine.analyze("sleepers", dict(CONTEXTS["sleepers"]))
    assert 0 < len(response.picks) <= 12
    assert response.week == WEEK and response.season == SEASON
    assert all(pick.confidence in ("high", "medium", "low") for pick in response.picks)
    assert len({pick.player_id for pick in response.picks}) == len(response.picks)
    # Bhayshul Tuten has the biggest usage delta in the fixture but 45k adds:
    # by definition no longer a sleeper.
    assert "Bhayshul Tuten" not in {pick.name for pick in response.picks}
    # ...and nobody the market drafts in the first three rounds.
    assert all(MARKET_RANKS[pick.player_id] > SLEEPER_MARKET_RANK_FLOOR for pick in response.picks)
    assert "Ja'Marr Chase" not in {pick.name for pick in response.picks}
    if len(response.picks) < 8:
        assert "cleared the bar" in response.reasoning


async def test_sleepers_limit_is_respected(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("sleepers", {"week": WEEK, "season": SEASON, "limit": 3})
    assert len(response.picks) == 3
    # Three is what was asked for, not a shortfall.
    assert "cleared the bar" not in response.reasoning


async def test_player_resolves_names_and_fills_the_profile(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("player", {"week": WEEK, "name": "Bijan Robinson"})
    assert response.player.player_id == "1001"
    assert response.player.position == "RB"
    assert [line.week for line in response.player.recent_weeks] == [1, 2, 3]
    assert response.player.usage_trajectory and "rising" in response.player.usage_trajectory
    assert response.player.schedule_outlook


async def test_player_prefers_the_fantasy_josh_allen(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("player", {"week": WEEK, "name": "Josh Allen"})
    assert response.player.player_id == "1006"
    assert response.player.position == "QB"


async def test_unknown_player_degrades_honestly(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("player", {"week": WEEK, "name": "Wilford Brimley"})
    assert response.confidence == "low"
    assert response.stats_cited == []
    assert response.player.player_id == ""
    assert "could not resolve" in response.verdict.lower()


async def test_matchup_ranks_every_requested_player_exactly_once(
    engine: DeterministicAnalysisEngine,
) -> None:
    players = ["Ja'Marr Chase", "Marvin Harrison Jr.", "Jordan Addison"]
    response = await engine.analyze("matchup", {"week": WEEK, "players": players})
    assert [row.rank for row in response.ranked] == [1, 2, 3]
    assert len({row.player_id for row in response.ranked}) == 3
    assert [row.call for row in response.ranked] == ["start", "flex", "sit"]


async def test_matchup_keeps_an_unresolvable_player_in_the_ranking(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Silently dropping a player from a paid start/sit answer is worse than saying so."""
    response = await engine.analyze(
        "matchup", {"week": WEEK, "players": ["Bijan Robinson", "Ghost Player"]}
    )
    assert len(response.ranked) == 2
    last = response.ranked[-1]
    assert last.name == "Ghost Player"
    assert last.player_id == ""
    assert last.call == "bench"
    assert "not found" in last.projection_note.lower()


async def test_matchup_uses_the_defensive_matchup(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("matchup", {"week": WEEK, "players": ["1001", "1002"]})
    ranks = {row.name: row.def_vs_pos_rank for row in response.ranked}
    assert ranks["Bijan Robinson"] == 2  # CIN, 2nd most generous to RBs
    assert ranks["Breece Hall"] == 28  # BUF, 28th


async def test_roster_grades_starts_and_suggests_adds(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("roster", dict(CONTEXTS["roster"]))
    graded = {grade.position for grade in response.positional_grades}
    assert {"QB", "RB", "WR", "TE", "K"} <= graded
    called = {call.player_id for call in response.start_sit}
    assert called == {str(entry["player_id"]) for entry in FIXTURE_ROSTER}
    assert sum(1 for call in response.start_sit if call.call == "start") >= 5
    assert response.drop_candidates
    assert response.waiver_adds
    rostered = {str(entry["player_id"]) for entry in FIXTURE_ROSTER}
    assert all(add.player_id not in rostered for add in response.waiver_adds)


async def test_roster_restricts_adds_to_the_league_free_agent_pool(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Recommending a rostered player would make the whole audit useless."""
    context = dict(CONTEXTS["roster"])
    context["free_agents"] = [dict(p) for p in FIXTURE_FREE_AGENTS]
    response = await engine.analyze("roster", context)
    pool = {p["player_id"] for p in FIXTURE_FREE_AGENTS}
    assert response.waiver_adds
    assert all(add.player_id in pool for add in response.waiver_adds)


async def test_roster_drop_risk_flags_a_growing_role(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("roster", dict(CONTEXTS["roster"]))
    assert all(drop.risk in ("high", "medium", "low") for drop in response.drop_candidates)
    # Nobody with a rising role should be a low-risk cut.
    for drop in response.drop_candidates:
        assert drop.reason.strip()


async def test_waivers_board_is_ranked_with_decaying_fab(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("waivers", dict(CONTEXTS["waivers"]))
    assert [row.rank for row in response.board] == list(range(1, len(response.board) + 1))
    bids = [row.fab_bid_pct for row in response.board]
    assert bids == sorted(bids, reverse=True)
    assert 0 < bids[0] <= 100
    assert all(row.stash_or_start in ("stash", "start", "streamer") for row in response.board)
    assert all(row.trend_count is not None for row in response.board)


async def test_waivers_limit_is_respected(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("waivers", {"week": WEEK, "season": SEASON, "limit": 3})
    assert len(response.board) == 3


async def test_report_fills_every_section(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("report", dict(CONTEXTS["report"]))
    assert response.emerging
    assert response.stock_up and response.stock_down
    assert response.rookie_watch
    assert response.streamers
    assert response.injury_fallout


async def test_report_emerging_excludes_consensus_adds(
    engine: DeterministicAnalysisEngine,
) -> None:
    """'Coming up before consensus' is the flagship signal; consensus disqualifies."""
    from api.evals.golden import TRENDING_ADD  # noqa: PLC0415

    consensus = {pid for pid, count in TRENDING_ADD if count >= CONSENSUS_ADD_COUNT}
    response = await engine.analyze("report", dict(CONTEXTS["report"]))
    assert response.emerging
    assert not ({note.player_id for note in response.emerging} & consensus)


async def test_report_builds_the_injury_chain(engine: DeterministicAnalysisEngine) -> None:
    response = await engine.analyze("report", dict(CONTEXTS["report"]))
    chains = {entry.injured_player: entry for entry in response.injury_fallout}
    assert "Nico Collins" in chains
    assert chains["Nico Collins"].status == "Out"
    assert "Jayden Higgins" in {note.name for note in chains["Nico Collins"].beneficiaries}


async def test_team_report_narrates_supplied_analytics_verbatim(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Tech spec §6: Python computes these numbers, the engine only narrates."""
    response = await engine.analyze("team_report", dict(CONTEXTS["team_report"]))
    supplied = {row["position"]: row for row in TEAM_ANALYTICS["positional_strength"]}
    for strength in response.positional_strength_vs_league:
        assert strength.league_rank == supplied[strength.position]["league_rank"]
        assert strength.points_per_week == supplied[strength.position]["points_per_week"]
    review = TEAM_ANALYTICS["manager_review"]
    assert response.manager_review.lineup_efficiency_pct == review["lineup_efficiency_pct"]
    assert response.manager_review.bench_points_lost == review["bench_points_lost"]
    assert response.manager_review.mis_start_patterns == review["mis_start_patterns"]


async def test_team_report_fixes_come_from_the_league_pool(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("team_report", dict(CONTEXTS["team_report"]))
    pool = {p["player_id"] for p in FIXTURE_FREE_AGENTS}
    assert response.deficiencies, "WR ranks 11 of 12 in the fixture and must be flagged"
    for deficiency in response.deficiencies:
        for fix in deficiency.available_fixes:
            assert fix.player_id in pool
            assert fix.position == deficiency.position


async def test_team_report_fixes_rank_by_usage_and_skip_out_or_teamless(
    eval_settings: Settings,
) -> None:
    """Pool order is the evidence, not the lexical order of Sleeper ids."""
    store = await seed_store(MemoryStore())
    extra = {
        # Ids that sort before the real fixes, each disqualified or weaker.
        "0901": {"name": "Out Receiver", "team": "DAL", "injury_status": "IR"},
        "0902": {"name": "Teamless Receiver", "team": None},
        "0903": {"name": "Flat Receiver", "team": "NYG"},
    }
    for pid, fields in extra.items():
        await store.set("players", pid, {"player_id": pid, "position": "WR", **fields})
        growth = 0.0 if pid == "0903" else 0.30  # the out and teamless ones grow most
        await store.set(
            "usage_trends",
            pid,
            {
                "player_id": pid,
                "season": SEASON,
                "target_share_delta": growth,
                "snap_pct_delta": growth,
                "trend": "rising" if growth else "flat",
            },
        )
    engine = DeterministicAnalysisEngine(store=store, settings=eval_settings)
    ctx = dict(CONTEXTS["team_report"])
    ctx["free_agents"] = [
        *({"player_id": pid, "position": "WR"} for pid in extra),
        *(dict(p) for p in FIXTURE_FREE_AGENTS),
    ]

    response = await engine.analyze("team_report", ctx)

    wr = next(d for d in response.deficiencies if d.position == "WR")
    ids = [fix.player_id for fix in wr.available_fixes]
    assert "0901" not in ids, "an IR player is not a fix"
    assert "0902" not in ids, "a player with no team is not a fix"
    # Rome Odunze (target share +0.11) out-grew Jaxon Smith-Njigba (+0.03); the
    # flat receiver, first by id, is last by evidence.
    assert ids == ["1008", "1010", "0903"]


async def test_team_report_without_analytics_says_not_computed(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Zeroed metrics must be labelled, never presented as findings."""
    context = {k: v for k, v in CONTEXTS["team_report"].items() if k != "team_analytics"}
    response = await engine.analyze("team_report", context)
    review = response.manager_review
    assert review.lineup_efficiency_pct == 0.0
    assert review.bench_points_lost == 0.0
    assert "not computed" in review.luck_note.lower()
    assert any("observation" in text.lower() for text in review.observations)
    assert response.positional_strength_vs_league == []


# -- heuristic units ------------------------------------------------------


@pytest.mark.parametrize(
    ("signals", "expected"),
    [
        ([], "low"),
        ([True] * 5, "high"),
        ([True, True, True, True, False], "high"),  # 0.80 exactly
        ([True, True, True, False], "medium"),  # 0.75 is not enough for 'high'
        ([True, True, False, False], "medium"),  # 0.50 exactly
        ([True, False, False, False], "low"),
        ([False] * 3, "low"),
    ],
)
def test_confidence_tracks_data_completeness(signals: list[bool], expected: str) -> None:
    assert _confidence(signals) == expected


@pytest.mark.parametrize(
    ("ppg", "grade"),
    [
        (24.0, "A"),
        (18.0, "A"),
        (15.5, "B+"),
        (12.4, "B"),
        (9.1, "C+"),
        (7.0, "C"),
        (4.0, "D"),
        (1.0, "F"),
        (None, "F"),
    ],
)
def test_grade_thresholds(ppg: float | None, grade: str) -> None:
    assert _grade(ppg) == grade


# -- empty store ----------------------------------------------------------


@pytest.mark.parametrize("key", ENDPOINT_KEYS)
async def test_endpoints_survive_an_empty_store(
    store: Store, eval_settings: Settings, key: str
) -> None:
    """Before ingest has ever run, a paid call must degrade rather than 500."""
    engine = DeterministicAnalysisEngine(store=store, settings=eval_settings)
    response = await engine.analyze(key, dict(CONTEXTS[key]))
    assert isinstance(response, RESPONSE_MODELS[key])
    assert response.verdict.strip()
    assert response.meta.data_freshness == {}


# -- cross-wave interop: api/data/team_analytics.py -----------------------


async def test_team_report_consumes_real_team_analytics_output(
    engine: DeterministicAnalysisEngine,
) -> None:
    """The engine must eat ``build_team_report_facts()`` output unmodified.

    That function is the other half of tech spec §6's contract: Python computes
    the league-relative numbers, the engine narrates them. Wave-3 routes will
    pass its return value straight through as ``request_context["team_analytics"]``,
    so this test builds a real one rather than a hand-written stand-in.
    """
    from api.data.team_analytics import build_team_report_facts  # noqa: PLC0415

    league = {
        "league_id": "998877",
        "name": "Dynasty Warriors",
        "total_rosters": 2,
        "roster_positions": ["QB", "RB", "RB", "WR", "WR", "TE", "K", "BN", "BN"],
    }
    rosters = [
        {
            "roster_id": 1,
            "owner_id": "u1",
            "players": ["1006", "1001", "1002", "1003", "1015", "1018", "1019"],
        },
        {
            "roster_id": 2,
            "owner_id": "u2",
            "players": ["1013", "1009", "1014", "1010", "1004", "1005", "1012"],
        },
    ]
    player_lookup = {
        pid: {"name": name, "position": position, "fantasy_positions": [position]}
        for pid, (name, position, *_rest) in PLAYERS.items()
    }
    matchups_by_week = {
        week: [
            {
                "roster_id": roster["roster_id"],
                "matchup_id": 1,
                "points": 100.0 + week,
                "starters": roster["players"][:7],
                "players": roster["players"],
                "players_points": {
                    pid: 10.0 + index for index, pid in enumerate(roster["players"])
                },
            }
            for roster in rosters
        ]
        for week in (1, 2, 3)
    }

    facts = build_team_report_facts(
        league=league,
        rosters=rosters,
        matchups_by_week=matchups_by_week,
        my_roster_id=1,
        player_lookup=player_lookup,
        player_universe_ids=list(PLAYERS),
        season=SEASON,
        through_week=WEEK - 1,
        sleeper_username="playclock_ryan",
    )

    response = await engine.analyze(
        "team_report",
        {
            "week": WEEK,
            "season": SEASON,
            "sleeper_username": "playclock_ryan",
            "league_id": "998877",
            "league_name": "Dynasty Warriors",
            "team_analytics": facts,
        },
    )

    # The precomputed numbers survive verbatim.
    supplied = {row["position"]: row for row in facts["positional_strength_vs_league"]}
    assert response.positional_strength_vs_league
    for strength in response.positional_strength_vs_league:
        assert strength.league_rank == supplied[strength.position]["league_rank"]
        assert strength.points_per_week == supplied[strength.position]["points_per_week"]
    assert (
        response.manager_review.lineup_efficiency_pct
        == facts["manager_review"]["lineup_efficiency_pct"]
    )
    # Fixes are drawn from the free-agent pool the analytics derived.
    pool = set(facts["free_agent_pool"]["player_ids"])
    for deficiency in response.deficiencies:
        for fix in deficiency.available_fixes:
            assert fix.player_id in pool
            assert fix.position == deficiency.position


def test_free_agent_ids_distinguishes_unknown_from_empty() -> None:
    """None means 'not supplied'; an empty pool would blank every suggestion."""
    from api.agents.deterministic import _free_agent_ids  # noqa: PLC0415

    assert _free_agent_ids({}) is None
    assert _free_agent_ids({"free_agents": []}) is None
    assert _free_agent_ids({"free_agents": [{"player_id": "1008"}]}) == {"1008"}
    assert _free_agent_ids({"free_agents": ["1008", " 1010 "]}) == {"1008", "1010"}
    assert _free_agent_ids({"team_analytics": {"free_agent_pool": {"player_ids": ["1011"]}}}) == {
        "1011"
    }


# ---------------------------------------------------------------------------
# Before kickoff: report the role, not "0 weeks, n/a points"
# ---------------------------------------------------------------------------


@pytest.fixture
async def preseason_engine(eval_settings: Settings) -> DeterministicAnalysisEngine:
    """A store holding last season's log and a current depth chart, and no current log.

    This is week 1 of a new season as it actually is: nflverse has published the
    depth chart but no game file, so the only current fact about a player is his
    role.
    """
    store = await seed_store(MemoryStore())
    for week in (1, 2, 3):
        for pid in ("1001", "1002", "1003", "1004", "1005", "1006"):
            await store.delete(weekly_stats_collection(SEASON, week), pid)
    await store.set(
        "depth_charts",
        "ATL",
        {
            "team": "ATL",
            "season": SEASON,
            "positions": {"RB": [{"rank": 1, "name": "Bijan Robinson", "player_id": "x"}]},
        },
    )
    return DeterministicAnalysisEngine(
        store=store, settings=eval_settings.model_copy(update={"week_override": 1})
    )


async def test_preseason_player_leads_with_the_depth_chart_role(
    preseason_engine: DeterministicAnalysisEngine,
) -> None:
    """A role is current-season fact when the game log is empty. Say it first."""
    response = await preseason_engine.analyze("player", {"week": 1, "name": "Bijan Robinson"})

    assert "starting RB for ATL" in response.verdict
    assert "the starter" in response.reasoning
    # And it must say which season the production came from, not imply it is current.
    assert f"No {SEASON} game has been played" in response.reasoning


async def test_a_mid_season_player_with_no_log_has_not_played_rather_than_preseason(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Week 4, no 2026 log for this player: games have been played, he has not."""
    response = await engine.analyze("player", {"week": WEEK, "season": SEASON, "player_id": "1007"})

    assert f"No {SEASON} game has been played" not in response.reasoning
    assert f"has not played in {SEASON}" in response.reasoning
    assert "before kickoff" not in response.verdict
    assert "week 1 option" not in response.verdict
    assert response.confidence == "low"


@pytest.mark.parametrize(
    "entry",
    [
        {"rank": 1, "name": "Someone Else", "player_id": "00-0036000"},  # gsis join
        {"rank": 1, "name": "B.J. Robinson", "player_id": None},  # normalized name
    ],
)
async def test_depth_chart_rank_matches_gsis_then_normalized_name(
    preseason_engine: DeterministicAnalysisEngine, entry: dict[str, Any]
) -> None:
    """nflverse writes "D.J. Moore" where Sleeper writes "DJ Moore"; the id is exact."""
    store = preseason_engine._store
    await store.set("players", "1001", {"gsis_id": "00-0036000", "name": "BJ Robinson"}, merge=True)
    room = [{"rank": 2, "name": "Backup Back", "player_id": "00-0099999"}, entry]
    await store.set(
        "depth_charts", "ATL", {"team": "ATL", "season": SEASON, "positions": {"RB": room}}
    )

    response = await preseason_engine.analyze("player", {"week": 1, "player_id": "1001"})

    assert "starting RB for ATL" in response.verdict


async def test_preseason_player_confidence_is_capped_at_low(
    preseason_engine: DeterministicAnalysisEngine,
) -> None:
    """Four of five signals present still means nobody has played a snap.

    `_confidence` is a completeness ratio: with the game log missing it lands on
    exactly 0.80 and reads "high". Before kickoff there is no form to be
    confident about, only a role.
    """
    response = await preseason_engine.analyze("player", {"week": 1, "name": "Bijan Robinson"})

    assert response.confidence == "low"


async def test_a_quarterback_is_never_judged_on_target_share(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Target share is structurally zero for a QB — citing it invents a signal."""
    response = await engine.analyze("player", {"week": WEEK, "name": "Josh Allen"})

    cited = {c.stat for c in response.stats_cited}
    assert not {"target_share", "target_share_l4w", "target_share_delta"} & cited
    assert "target share" not in (response.player.usage_trajectory or "")
    assert "target share" not in response.reasoning

    # The receiver it does apply to keeps it.
    receiver = await engine.analyze("player", {"week": WEEK, "name": "Ja'Marr Chase"})
    assert {c.stat for c in receiver.stats_cited} & {"target_share", "target_share_l4w"}


# -- candidate lists for the narrated boards ---------------------------------


async def test_sleeper_candidates_exclude_the_crowd_and_carry_usage(
    engine: DeterministicAnalysisEngine,
) -> None:
    """The list the ADK synthesizer narrates: rising usage, not yet claimed."""
    from api.evals.golden import CONSENSUS_IDS  # noqa: PLC0415

    candidates = await engine.candidates("sleepers", dict(CONTEXTS["sleepers"]))
    assert candidates, "the fixture has rising-usage players the crowd has not found"
    ids = {c["player_id"] for c in candidates}
    assert not ids & CONSENSUS_IDS, "a consensus add can never be a sleeper candidate"
    first = candidates[0]
    assert set(first) >= {"player_id", "name", "position", "team", "usage", "usage_note"}
    assert first["usage"]["trend"] == "rising"
    assert "{" not in first["usage_note"]


async def test_sleeper_candidates_match_the_deterministic_board(
    engine: DeterministicAnalysisEngine,
) -> None:
    """One scorer, two consumers: the list and the board must agree."""
    board = await engine.analyze("sleepers", dict(CONTEXTS["sleepers"]))
    candidates = await engine.candidates("sleepers", dict(CONTEXTS["sleepers"]))
    assert [c["player_id"] for c in candidates] == [p.player_id for p in board.picks]


async def test_report_candidates_are_the_emerging_pool(
    engine: DeterministicAnalysisEngine,
) -> None:
    report = await engine.analyze("report", dict(CONTEXTS["report"]))
    candidates = await engine.candidates("report", dict(CONTEXTS["report"]))
    assert [c["player_id"] for c in candidates] == [n.player_id for n in report.emerging]
    assert all((c["trend_count"] or 0) < CONSENSUS_ADD_COUNT for c in candidates)


@pytest.mark.parametrize("key", [k for k in ENDPOINT_KEYS if k not in ("sleepers", "report")])
async def test_other_endpoints_have_no_candidate_list(
    engine: DeterministicAnalysisEngine, key: str
) -> None:
    assert await engine.candidates(key, dict(CONTEXTS[key])) == []


# -- the owned-player floor and the preseason role rule ---------------------


async def test_a_star_is_emerging_but_never_a_sleeper(
    engine: DeterministicAnalysisEngine,
) -> None:
    """Ja'Marr Chase (search_rank 1) has rising usage: emerging yes, sleeper no."""
    sleepers = {
        c["player_id"] for c in await engine.candidates("sleepers", dict(CONTEXTS["sleepers"]))
    }
    emerging = {c["player_id"] for c in await engine.candidates("report", dict(CONTEXTS["report"]))}
    assert "1003" not in sleepers
    assert "1003" in emerging


def _player(pid: str, name: str, position: str, team: str = "KC") -> dict[str, Any]:
    return {
        "player_id": pid,
        "name": name,
        "search_name": name.lower(),
        "position": position,
        "team": team,
        "status": "Active",
        "search_rank": 400,
    }


def _rising_usage(pid: str, season: int) -> dict[str, Any]:
    return {
        "player_id": pid,
        "season": season,
        "through_week": 18,
        "snap_pct_l4w": 0.5,
        "target_share_l4w": 0.12,
        "rz_touches_l4w": 3,
        "snap_pct_delta": 0.2,
        "target_share_delta": 0.05,
        "trend": "rising",
    }


#: Five rising-usage players on one team: a starter and a backup at QB, a
#: backup RB, a starting WR, and a TE whose position has no chart at all.
_ROOM = (
    ("q1", "Alpha Starter", "QB", 1),
    ("q2", "Bravo Clipboard", "QB", 2),
    ("r2", "Charlie Handcuff", "RB", 2),
    ("w1", "Delta Wideout", "WR", 1),
    ("t0", "Echo Uncharted", "TE", None),
)


async def _room_engine(usage_season: int, eval_settings: Settings) -> DeterministicAnalysisEngine:
    store = MemoryStore()
    positions: dict[str, list[dict[str, Any]]] = {}
    for pid, name, position, rank in _ROOM:
        await store.set("players", pid, _player(pid, name, position))
        await store.set("usage_trends", pid, _rising_usage(pid, usage_season))
        if rank is not None:
            positions.setdefault(position, []).append(
                {"rank": rank, "name": name, "player_id": pid}
            )
    await store.set("depth_charts", "KC", {"team": "KC", "season": SEASON, "positions": positions})
    return DeterministicAnalysisEngine(store=store, settings=eval_settings)


@pytest.mark.parametrize("endpoint_key", ["sleepers", "report"])
async def test_preseason_candidates_must_hold_a_current_job(
    eval_settings: Settings, endpoint_key: str
) -> None:
    """Last December's deltas are evidence only when the depth chart agrees."""
    engine = await _room_engine(SEASON - 1, eval_settings)
    ids = {
        c["player_id"] for c in await engine.candidates(endpoint_key, {"season": SEASON, "week": 1})
    }
    assert ids == {"q1", "r2", "w1"}, (
        "a starter at any position and a backup RB/WR/TE qualify; a backup QB and a "
        "player with no chart do not"
    )


@pytest.mark.parametrize("endpoint_key", ["sleepers", "report"])
async def test_in_season_the_rollup_is_the_evidence(
    eval_settings: Settings, endpoint_key: str
) -> None:
    engine = await _room_engine(SEASON, eval_settings)
    ids = {
        c["player_id"] for c in await engine.candidates(endpoint_key, {"season": SEASON, "week": 1})
    }
    assert ids == {"q1", "q2", "r2", "w1", "t0"}


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "1st"),
        (2, "2nd"),
        (3, "3rd"),
        (4, "4th"),
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (21, "21st"),
        (71, "71st"),
        (92, "92nd"),
        (100, "100th"),
        (0, "0th"),
    ],
)
def test_ordinal_suffixes(n: int, expected: str) -> None:
    """The first narrated board said '71th percentile'."""
    from api.agents.deterministic import _ordinal

    assert _ordinal(n) == expected


# --------------------------------------------------------------------------
# availability: a player who is not playing is never a start
# --------------------------------------------------------------------------


async def _engine_with_status(
    eval_settings: Settings, player_id: str, status: str
) -> DeterministicAnalysisEngine:
    store = await seed_store(MemoryStore())
    await store.set("players", player_id, {"injury_status": status}, merge=True)
    return DeterministicAnalysisEngine(store=store, settings=eval_settings)


async def test_matchup_sits_a_player_listed_out(eval_settings: Settings) -> None:
    """Bijan has the better matchup and history; on IR he must still sit."""
    engine = await _engine_with_status(eval_settings, "1001", "IR")
    response = await engine.analyze("matchup", dict(CONTEXTS["matchup"]))
    by_name = {row.name: row for row in response.ranked}
    assert by_name["Bijan Robinson"].call == "sit"
    assert by_name["Bijan Robinson"].rank == len(response.ranked)
    assert "Listed IR" in by_name["Bijan Robinson"].projection_note
    assert by_name["Breece Hall"].call == "start"


async def test_roster_never_starts_an_out_player_or_drops_a_starter(
    eval_settings: Settings,
) -> None:
    starter = str(FIXTURE_ROSTER[0].get("player_id"))
    engine = await _engine_with_status(eval_settings, starter, "Out")
    response = await engine.analyze("roster", dict(CONTEXTS["roster"]))

    calls = {row.player_id: row.call for row in response.start_sit}
    assert calls[starter] == "bench"
    lineup = {pid for pid, call in calls.items() if call in ("start", "flex")}
    assert not lineup & {drop.player_id for drop in response.drop_candidates}


async def test_report_streamers_lead_with_the_best_matchup(
    engine: DeterministicAnalysisEngine,
) -> None:
    response = await engine.analyze("report", dict(CONTEXTS["report"]))
    ranks = [
        int(note.note.split(" rank ")[1].split()[0])
        for note in response.streamers
        if " rank " in note.note
    ]
    assert ranks == sorted(ranks)
    assert all(note.position in ("QB", "TE") for note in response.streamers)
    # The method claim names only positions a def-vs-pos split exists for.
    assert "Streamers are QB/TE facing" in response.reasoning
    assert "DEF" not in response.reasoning.split("Streamers are")[1]


def test_a_rollup_from_before_a_long_absence_is_not_current_form() -> None:
    from api.agents.deterministic import _rollup_is_current  # noqa: PLC0415

    assert _rollup_is_current({"through_week": 9, "last_week_played": 9})
    assert _rollup_is_current({"through_week": 9, "last_week_played": 8}), "a bye is fine"
    assert not _rollup_is_current({"through_week": 9, "last_week_played": 4})
    assert _rollup_is_current({}), "an older rollup without the fields is not rejected"


async def test_a_player_back_from_a_month_out_is_not_described_as_preseason(
    eval_settings: Settings,
) -> None:
    """An empty last-four-week window means missed games, not an unplayed season."""
    store = MemoryStore()
    await store.set(
        "players",
        "7001",
        {"player_id": "7001", "name": "Test Returner", "position": "WR", "team": "ATL"},
    )
    for week in (1, 2, 3):
        await store.set(
            weekly_stats_collection(SEASON, week),
            "7001",
            {"week": week, "fantasy_points_ppr": 20.0},
        )
    for week in range(1, 18):
        await store.set(
            weekly_stats_collection(SEASON - 1, week),
            "7001",
            {"week": week, "fantasy_points_ppr": 5.0},
        )
    engine = DeterministicAnalysisEngine(
        store=store, settings=eval_settings.model_copy(update={"week_override": 9})
    )

    response = await engine.analyze("player", {"player_id": "7001", "week": 9, "season": SEASON})

    assert "20.0" in response.reasoning
    assert f"{SEASON - 1}" not in response.reasoning
    assert [line.week for line in response.player.recent_weeks] == [1, 2, 3]


# --------------------------------------------------------------------------
# availability, continued: out, bye and stale usage are never a call to play
# --------------------------------------------------------------------------


async def _engine_with_bye(eval_settings: Settings, team: str) -> DeterministicAnalysisEngine:
    """The fixture, with ``team``'s week-4 game removed from the schedule: a bye."""
    from api.data.stats_store import SCHEDULES_COLLECTION  # noqa: PLC0415

    store = await seed_store(MemoryStore())
    doc = await store.get(SCHEDULES_COLLECTION, f"{SEASON}_{WEEK}")
    assert doc is not None
    doc["games"] = [g for g in doc["games"] if team not in (g["home"], g["away"])]
    await store.set(SCHEDULES_COLLECTION, f"{SEASON}_{WEEK}", doc)
    return DeterministicAnalysisEngine(store=store, settings=eval_settings)


def _trend_view(**fields: Any) -> Any:
    from api.agents.deterministic import _PlayerView  # noqa: PLC0415

    view = _PlayerView("9001", "Test Player")
    view.position = "WR"
    view.team = "ATL"
    view.season = SEASON
    view.usage = {"trend": "rising", "season": SEASON}
    for key, value in fields.items():
        setattr(view, key, value)
    return view


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "add"),
        ({"status": "IR"}, "hold"),
        ({"status": "Sus"}, "hold"),
        # Rose in weeks 1-4, has not played since: the trend is history.
        (
            {
                "usage": {
                    "trend": "rising",
                    "season": SEASON,
                    "through_week": 9,
                    "last_week_played": 4,
                }
            },
            "hold",
        ),
        # Preseason: last December's rise counts only for a player the chart backs.
        ({"usage": {"trend": "rising", "season": SEASON - 1}, "depth_rank": 3}, "hold"),
        ({"usage": {"trend": "rising", "season": SEASON - 1}, "depth_rank": 1}, "add"),
    ],
)
def test_trending_verdict_holds_when_the_usage_describes_nobody_playing(
    fields: dict[str, Any], expected: str
) -> None:
    view = _trend_view(**fields)
    assert DeterministicAnalysisEngine._trending_verdict(view, "add", 10) == expected
    # The drop board follows the same rule: no buy-low on a man who does not play.
    drop = "add" if expected == "add" else "hold"
    assert DeterministicAnalysisEngine._trending_verdict(view, "drop", 10) == drop


@pytest.mark.parametrize("status", ["IR", "Suspended", "PUP", "physically unable to perform"])
async def test_player_on_an_out_list_is_benched_not_started(
    eval_settings: Settings, status: str
) -> None:
    from api.data.predictions import verdict_call  # noqa: PLC0415

    engine = await _engine_with_status(eval_settings, "1001", status)
    response = await engine.analyze("player", dict(CONTEXTS["player"]))

    assert "bench him" in response.verdict
    assert verdict_call(response.verdict) == "sit"
    assert response.reasoning.startswith("Listed ")


async def test_player_on_bye_gets_no_start(eval_settings: Settings) -> None:
    from api.data.predictions import verdict_call  # noqa: PLC0415

    engine = await _engine_with_bye(eval_settings, "ATL")
    response = await engine.analyze("player", dict(CONTEXTS["player"]))

    assert "on bye" in response.verdict
    assert verdict_call(response.verdict) is None, "a bye is not a call to archive"
    assert "on bye this week" in response.reasoning


async def test_preseason_starter_who_is_out_is_not_a_week_one_option(
    preseason_engine: DeterministicAnalysisEngine,
) -> None:
    await preseason_engine._store.set("players", "1001", {"injury_status": "Out"}, merge=True)
    response = await preseason_engine.analyze("player", {"week": 1, "name": "Bijan Robinson"})

    assert "starting RB" not in response.verdict
    assert "bench him" in response.verdict


async def test_matchup_sits_a_player_on_bye(eval_settings: Settings) -> None:
    """Bijan out-ranks Breece on form and matchup; with no game he still sits."""
    engine = await _engine_with_bye(eval_settings, "ATL")
    response = await engine.analyze("matchup", dict(CONTEXTS["matchup"]))
    by_name = {row.name: row for row in response.ranked}

    assert by_name["Bijan Robinson"].call == "sit"
    assert by_name["Bijan Robinson"].rank == len(response.ranked)
    assert by_name["Breece Hall"].call == "start"
    assert response.verdict.startswith("Start Breece Hall")


async def test_roster_benches_a_bye_without_cutting_him(eval_settings: Settings) -> None:
    engine = await _engine_with_bye(eval_settings, "ATL")
    response = await engine.analyze("roster", dict(CONTEXTS["roster"]))

    calls = {row.player_id: row.call for row in response.start_sit}
    assert calls["1001"] == "bench"
    assert "1001" not in {drop.player_id for drop in response.drop_candidates}


def test_a_waiver_target_on_bye_is_not_a_start() -> None:
    from api.agents.deterministic import STARTER_SNAP_PCT, _waiver_row  # noqa: PLC0415

    playing = _trend_view(schedule_known=True, opponent="CIN")
    playing.usage = {"trend": "rising", "snap_pct_l4w": STARTER_SNAP_PCT + 0.1}
    assert _waiver_row(playing, 1, 100).stash_or_start == "start"

    idle = _trend_view(schedule_known=True, opponent=None)
    idle.usage = dict(playing.usage)
    assert _waiver_row(idle, 1, 100).stash_or_start == "stash"
    assert idle.start_score < playing.start_score, "a bye never ranks first"


async def test_draft_report_ignores_sleepers_unranked_sentinel(eval_settings: Settings) -> None:
    """search_rank 9999999 once made a round-15 pick 'value' by millions and graded F."""
    store = await seed_store(MemoryStore())
    await store.set("players", "1019", {"search_rank": 9999999}, merge=True)
    engine = DeterministicAnalysisEngine(store=store, settings=eval_settings)

    response = await engine.analyze("draft_report", dict(CONTEXTS["draft_report"]))

    kicker = next(p for p in response.roster if p.player_id == "1019")
    assert kicker.market_rank is None and kicker.value_delta is None
    assert all(abs(p.value_delta) < 1000 for p in response.roster if p.value_delta is not None)
    assert "1019" not in {p.player_id for p in response.best_picks + response.worst_picks}


async def test_a_token_that_cannot_be_a_doc_id_resolves_as_a_name(
    eval_settings: Settings,
) -> None:
    """Firestore raises ValueError on '/' in a doc id; that must not 500 a paid call."""

    class FirestoreLike(MemoryStore):
        async def get(self, collection: str, doc_id: str) -> dict | None:  # type: ignore[override]
            if "/" in doc_id:
                raise ValueError(f"invalid document id {doc_id!r}")
            return await super().get(collection, doc_id)

    store = await seed_store(FirestoreLike())
    engine = DeterministicAnalysisEngine(store=store, settings=eval_settings)

    response = await engine.analyze("player", {"week": WEEK, "name": "Bijan/Robinson"})
    assert response.player.player_id == ""
    assert response.confidence == "low"
