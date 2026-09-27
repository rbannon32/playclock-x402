"""Response contract round-trips and catalog construction."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from api.core.config import ENDPOINT_KEYS, Settings
from api.schemas import (
    ATTRIBUTION,
    MAX_ROSTER_SIZE,
    AnalysisMeta,
    AnalysisResponse,
    AvailableFix,
    Catalog,
    CatalogEntry,
    Deficiency,
    DraftBoardResponse,
    DraftReportRequest,
    DraftReportResponse,
    ManagerReview,
    MatchupRanking,
    MatchupRequest,
    MatchupResponse,
    PlayerProfile,
    PlayerRequest,
    PlayerResponse,
    PositionalStrength,
    ReportResponse,
    RosterRequest,
    RosterResponse,
    SleeperPick,
    SleepersResponse,
    SourceRef,
    StatCitation,
    TeamReportRequest,
    TeamReportResponse,
    TrendingPlayer,
    TrendingResponse,
    WaiversResponse,
    WaiverTarget,
)


def _meta() -> AnalysisMeta:
    return AnalysisMeta(
        generated_at=datetime(2026, 9, 16, 13, 0, tzinfo=UTC),
        data_freshness={"weekly_stats": "2026-09-16T09:00:00Z"},
        model="gemini-2.5-flash",
        cache="fresh",
    )


def test_analysis_response_full_round_trip() -> None:
    original = AnalysisResponse(
        verdict="Start Bijan Robinson over Kenneth Walker.",
        confidence="high",
        reasoning="Bijan's snap share is up eight points over two weeks and Atlanta draws a bottom-3 run defense.",
        stats_cited=[
            StatCitation(
                stat="snap_pct",
                value=0.86,
                player="Bijan Robinson",
                source="nflverse weekly_stats 2026w3",
            ),
            StatCitation(
                stat="rush_epa_allowed_rank",
                value=30,
                player=None,
                source="nflverse def_vs_pos 2026",
            ),
        ],
        sources=[
            SourceRef(
                title="Falcons expect heavier Robinson workload",
                url="https://example.com/atl-rb",
                published="2026-09-15",
            )
        ],
        meta=_meta(),
    )

    payload = original.model_dump(mode="json")
    assert json.loads(json.dumps(payload)) == payload  # JSON-serializable end to end

    restored = AnalysisResponse.model_validate(payload)
    assert restored == original
    assert restored.meta.generated_at == datetime(2026, 9, 16, 13, 0, tzinfo=UTC)
    assert restored.meta.attribution == ATTRIBUTION
    assert restored.stats_cited[0].value == 0.86


def test_analysis_meta_defaults() -> None:
    meta = AnalysisMeta(generated_at=datetime.now(UTC))
    assert meta.attribution == ATTRIBUTION
    assert meta.data_freshness == {}
    assert meta.model is None
    assert meta.cache is None


def test_confidence_is_constrained() -> None:
    with pytest.raises(ValidationError):
        AnalysisResponse(verdict="v", confidence="very-high", reasoning="r", meta=_meta())


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AnalysisResponse(verdict="v", confidence="high", reasoning="r", meta=_meta(), sneaky=1)


def test_every_paid_response_extends_the_base_contract() -> None:
    paid = [
        TrendingResponse,
        SleepersResponse,
        PlayerResponse,
        MatchupResponse,
        RosterResponse,
        WaiversResponse,
        ReportResponse,
        TeamReportResponse,
        DraftBoardResponse,
        DraftReportResponse,
    ]
    assert len(paid) == len(ENDPOINT_KEYS)
    for model in paid:
        assert issubclass(model, AnalysisResponse)
        required = set(AnalysisResponse.model_fields)
        assert required.issubset(set(model.model_fields))


def test_trending_response_round_trip() -> None:
    resp = TrendingResponse(
        verdict="Chase the Tucker breakout; fade the Pierce panic drop.",
        confidence="medium",
        reasoning="Two of the top five adds are backed by real snap-share gains.",
        meta=_meta(),
        players=[
            TrendingPlayer(
                player_id="9502",
                name="Tank Bigsby",
                position="RB",
                team="JAX",
                trend="add",
                trend_count=51234,
                analysis="Took 62% of snaps after Etienne exited.",
                verdict="add",
            )
        ],
    )
    restored = TrendingResponse.model_validate(resp.model_dump(mode="json"))
    assert restored.players[0].verdict == "add"
    assert restored.lookback_hours == 24


def test_sleepers_response_round_trip() -> None:
    resp = SleepersResponse(
        verdict="Six starts with real paths to double-digit points.",
        confidence="medium",
        reasoning="All six clear a 15% target share or a 60% snap share.",
        meta=_meta(),
        week=3,
        season=2026,
        picks=[
            SleeperPick(
                player_id="1",
                name="Player One",
                position="WR",
                team="KC",
                opponent="ATL",
                confidence="high",
                usage_note="Target share up to 24%.",
                matchup_note="ATL allows the 3rd-most points to WRs.",
                rationale="Volume plus matchup.",
            )
        ],
    )
    restored = SleepersResponse.model_validate(resp.model_dump(mode="json"))
    assert restored.week == 3
    assert restored.picks[0].confidence == "high"


def test_player_response_round_trip() -> None:
    resp = PlayerResponse(
        verdict="Buy — the usage is real.",
        confidence="high",
        reasoning="Snap share climbed four straight weeks.",
        meta=_meta(),
        week=3,
        player=PlayerProfile(player_id="4046", name="Patrick Mahomes", position="QB", team="KC"),
    )
    restored = PlayerResponse.model_validate(resp.model_dump(mode="json"))
    assert restored.player.name == "Patrick Mahomes"
    assert restored.player.recent_weeks == []


def test_matchup_response_round_trip() -> None:
    resp = MatchupResponse(
        verdict="Start A over B.",
        confidence="low",
        reasoning="Close call decided by matchup.",
        meta=_meta(),
        week=4,
        ranked=[
            MatchupRanking(
                rank=1,
                player_id="1",
                name="A",
                position="RB",
                team="KC",
                opponent="ATL",
                projection_note="Volume floor.",
                def_vs_pos_rank=2,
                call="start",
            ),
            MatchupRanking(
                rank=2,
                player_id="2",
                name="B",
                position="RB",
                team="SEA",
                opponent="SF",
                projection_note="Tough front.",
                def_vs_pos_rank=29,
                call="sit",
            ),
        ],
    )
    restored = MatchupResponse.model_validate(resp.model_dump(mode="json"))
    assert [r.rank for r in restored.ranked] == [1, 2]


def test_roster_and_waivers_and_report_round_trip() -> None:
    roster = RosterResponse(
        verdict="Strong at WR, thin at RB.",
        confidence="medium",
        reasoning="...",
        meta=_meta(),
        week=3,
    )
    assert RosterResponse.model_validate(roster.model_dump(mode="json")).positional_grades == []

    waivers = WaiversResponse(
        verdict="Spend on the JAX backfield.",
        confidence="high",
        reasoning="...",
        meta=_meta(),
        week=3,
        season=2026,
        board=[
            WaiverTarget(
                rank=1,
                player_id="1",
                name="A",
                position="RB",
                team="JAX",
                trend_count=51234,
                stash_or_start="start",
                fab_bid_pct=22.5,
                rationale="Lead back now.",
            )
        ],
    )
    restored = WaiversResponse.model_validate(waivers.model_dump(mode="json"))
    assert restored.board[0].stash_or_start == "start"
    assert restored.board[0].fab_bid_pct == 22.5

    report = ReportResponse(
        verdict="Week 3 was a running-back bloodbath.",
        confidence="medium",
        reasoning="...",
        meta=_meta(),
        week=3,
        season=2026,
    )
    restored_report = ReportResponse.model_validate(report.model_dump(mode="json"))
    assert restored_report.emerging == []
    assert restored_report.injury_fallout == []
    assert restored_report.streamers == []


def test_team_report_round_trip() -> None:
    resp = TeamReportResponse(
        verdict="You are 3rd in points and 8th in wins — bad luck, not a bad team.",
        confidence="high",
        reasoning="Lineup efficiency is 92%, third in the league.",
        meta=_meta(),
        week=6,
        season=2026,
        sleeper_username="ryan",
        league_id="99887766",
        league_name="Dynasty Warriors",
        positional_strength_vs_league=[
            PositionalStrength(
                position="WR",
                league_rank=2,
                league_size=12,
                points_per_week=41.2,
                league_avg_points_per_week=33.8,
                grade="A-",
            )
        ],
        deficiencies=[
            Deficiency(
                position="RB",
                severity="high",
                detail="RB2 slot averaging 5.1 points.",
                available_fixes=[
                    AvailableFix(
                        player_id="9502",
                        name="Tank Bigsby",
                        position="RB",
                        team="JAX",
                        why="Unrostered in your league and now the lead back.",
                    )
                ],
            )
        ],
        manager_review=ManagerReview(
            bench_points_lost=48.6,
            optimal_vs_actual=48.6,
            lineup_efficiency_pct=92.4,
            efficiency_rank=3,
            league_size=12,
            luck_note="You have the 2nd-most points against.",
            expected_wins=4.1,
            actual_wins=2,
            mis_start_patterns=["Started the wrong TE in 3 of 6 weeks (-14.2 pts)."],
            observations=["Tends to chase last week's boom performance."],
        ),
    )
    restored = TeamReportResponse.model_validate(resp.model_dump(mode="json"))
    assert restored.manager_review.efficiency_rank == 3
    assert restored.deficiencies[0].available_fixes[0].name == "Tank Bigsby"
    assert restored.positional_strength_vs_league[0].grade == "A-"


def test_request_models() -> None:
    assert PlayerRequest(name="Bijan Robinson").week is None
    assert PlayerRequest(player_id="9509").player_id == "9509"

    assert MatchupRequest(players=["A", "B"], week=3).week == 3
    with pytest.raises(ValidationError):
        MatchupRequest(players=["only-one"])
    with pytest.raises(ValidationError):
        MatchupRequest(players=["a", "b", "c", "d", "e"])

    assert RosterRequest(sleeper_username="ryan").roster is None
    assert RosterRequest(roster=[{"name": "A", "starter": True}]).sleeper_username is None

    assert TeamReportRequest(sleeper_username="ryan").league_id is None
    with pytest.raises(ValidationError):
        TeamReportRequest()  # sleeper_username is required


def test_catalog_builds_over_every_endpoint_key() -> None:
    settings = Settings(_env_file=None, x402_pay_to="ALGO...ADDR", x402_asset_id=31566704)  # type: ignore[call-arg]
    response_models = {
        "trending": "TrendingResponse",
        "sleepers": "SleepersResponse",
        "player": "PlayerResponse",
        "matchup": "MatchupResponse",
        "roster": "RosterResponse",
        "waivers": "WaiversResponse",
        "report": "ReportResponse",
        "team_report": "TeamReportResponse",
        "draft_board": "DraftBoardResponse",
        "draft_report": "DraftReportResponse",
    }
    entries = [
        CatalogEntry(
            path=f"/v1/{key.replace('_', '-')}",
            method="POST"
            if key in {"player", "matchup", "roster", "team_report", "draft_report"}
            else "GET",
            key=key,
            price_usdc=settings.price_for(key),
            description=f"Paid {key} analysis.",
            response_schema=response_models[key],
            free=False,
        )
        for key in ENDPOINT_KEYS
    ]
    entries.append(
        CatalogEntry(
            path="/v1/trending/preview",
            method="GET",
            key="",
            price_usdc=0.0,
            description="Top 5 trending adds/drops, no analysis.",
            response_schema="TrendingPreviewResponse",
            free=True,
        )
    )

    catalog = Catalog(
        service=settings.app_name,
        version="v1",
        network=settings.x402_network,
        pay_to=settings.x402_pay_to,
        asset_id=settings.x402_asset_id,
        facilitator_url=settings.x402_facilitator_url,
        challenge_tag=settings.x402_challenge_tag,
        endpoints=entries,
    )

    restored = Catalog.model_validate(catalog.model_dump(mode="json"))
    assert len(restored.endpoints) == len(ENDPOINT_KEYS) + 1
    paid = [e for e in restored.endpoints if not e.free]
    assert {e.key for e in paid} == set(ENDPOINT_KEYS)
    # 2.75 across the original eight, plus 0.25 draft board and 0.75 draft report.
    # Derived from the table rather than pinned: `test_config` is where the
    # prices themselves are asserted, and this is about the catalog round-trip.
    assert sum(e.price_usdc for e in paid) == pytest.approx(sum(settings.prices().values()))
    assert restored.challenge_tag == "x402-global-challenge"
    assert restored.attribution == ATTRIBUTION


@pytest.mark.parametrize(
    "body",
    [
        {"sleeper_username": "../x"},
        {"sleeper_username": ".."},
        {"sleeper_username": "a" * 41},
        {"sleeper_username": "ryan", "league_id": "L1"},
        {"sleeper_username": "ryan", "league_id": "1" * 21},
    ],
)
def test_sleeper_path_fields_are_pattern_checked(body: dict[str, str]) -> None:
    """They become Sleeper URL path segments; only real shapes get through."""
    with pytest.raises(ValidationError):
        TeamReportRequest(**body)


def test_real_sleeper_identifiers_still_validate() -> None:
    request = TeamReportRequest(
        sleeper_username=" play.clock_ryan-2 ", league_id="1049283746152738291"
    )
    assert request.sleeper_username == "play.clock_ryan-2"
    assert DraftReportRequest(draft_id="1234567890").draft_id == "1234567890"


def test_pasted_rosters_and_names_are_bounded() -> None:
    with pytest.raises(ValidationError):
        RosterRequest(roster=[{"name": f"P{i}"} for i in range(MAX_ROSTER_SIZE + 1)])
    with pytest.raises(ValidationError):
        MatchupRequest(players=["A", "x" * 101])
    assert len(RosterRequest(roster=[{"name": "P"}] * MAX_ROSTER_SIZE).roster or []) == 40
