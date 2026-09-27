"""Deterministic team analytics — the numbers behind ``POST /v1/team-report``.

The fixture is a **4-team, 3-week synthetic league** whose every expected value is
hand-computed in the comments below, so a regression shows up as a wrong number
rather than a wrong-looking number.

League shape: ``["QB", "RB", "WR", "TE", "FLEX", "BN", "BN"]`` — 5 starting slots,
7 players per team.

My roster (id 1) holds ``q1 r1 r2 r3 w1 w2 t1`` and starts the *same* five every
week: ``[q1, r1, w1, t1, r2]``. That fixed lineup against varying weekly points is
what creates the mis-start pattern the report is supposed to catch (w2 benched in
all three weeks).

Weekly points and the hand-checked optimum
------------------------------------------
======  =====================================================  =======  ======  =====
Week    points                                                  actual  optimal  lost
======  =====================================================  =======  ======  =====
1       q1 20, r1 15, r2 9, r3 4, w1 12, w2 18, t1 6            62.0     71.0     9.0
2       q1 25, r1 10, r2 20, r3 3, w1 8, w2 22, t1 11           74.0     88.0    14.0
3       q1 18, r1 12, r2 5, r3 25, w1 14, w2 7, t1 9            58.0     78.0    20.0
======  =====================================================  =======  ======  =====

Rivals score the same totals every week and always start optimally:
roster 2 = 56.0, roster 3 = 67.0, roster 4 = 33.0.

Schedule: wk1 1v2 / 3v4 · wk2 1v3 / 2v4 · wk3 1v4 / 2v3.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from api.data.team_analytics import (
    FACTS_VERSION,
    build_team_report_facts,
    free_agent_pool,
    grade_from_z,
    league_comparison,
    lineup_efficiency,
    luck_analysis,
    mark_unplayed,
    optimal_lineup,
    starting_slots,
    week_one_outlook,
    weekly_review,
)
from api.schemas import Deficiency, ManagerReview, PositionalStrength

# --------------------------------------------------------------------------
# Fixture
# --------------------------------------------------------------------------

ROSTER_POSITIONS = ["QB", "RB", "WR", "TE", "FLEX", "BN", "BN"]
MY_ROSTER_ID = 1

MY_PLAYERS = ["q1", "r1", "r2", "r3", "w1", "w2", "t1"]
MY_STARTERS = ["q1", "r1", "w1", "t1", "r2"]  # QB, RB, WR, TE, FLEX

MY_POINTS: dict[int, dict[str, float]] = {
    1: {"q1": 20.0, "r1": 15.0, "r2": 9.0, "r3": 4.0, "w1": 12.0, "w2": 18.0, "t1": 6.0},
    2: {"q1": 25.0, "r1": 10.0, "r2": 20.0, "r3": 3.0, "w1": 8.0, "w2": 22.0, "t1": 11.0},
    3: {"q1": 18.0, "r1": 12.0, "r2": 5.0, "r3": 25.0, "w1": 14.0, "w2": 7.0, "t1": 9.0},
}

#: Rival scoring, constant across all three weeks. Keys are the per-team suffixes.
RIVAL_POINTS: dict[int, dict[str, float]] = {
    2: {"q": 20.0, "r1": 10.0, "r2": 8.0, "r3": 2.0, "w1": 10.0, "w2": 6.0, "t1": 8.0},
    3: {"q": 15.0, "r1": 14.0, "r2": 12.0, "r3": 6.0, "w1": 16.0, "w2": 11.0, "t1": 10.0},
    4: {"q": 10.0, "r1": 6.0, "r2": 5.0, "r3": 3.0, "w1": 7.0, "w2": 4.0, "t1": 5.0},
}
RIVAL_STARTER_KEYS = ["q", "r1", "w1", "t1", "r2"]  # already the optimal lineup

#: ``{week: [(roster_a, roster_b), ...]}`` — matchup_id is the 1-based pair index.
SCHEDULE: dict[int, list[tuple[int, int]]] = {
    1: [(1, 2), (3, 4)],
    2: [(1, 3), (2, 4)],
    3: [(1, 4), (2, 3)],
}

_POSITION_OF = {"q": "QB", "r1": "RB", "r2": "RB", "r3": "RB", "w1": "WR", "w2": "WR", "t1": "TE"}
_NAME_OF = {
    "q": "Quinn",
    "r1": "Rush One",
    "r2": "Rush Two",
    "r3": "Rush Three",
    "w1": "Wide One",
    "w2": "Wide Two",
    "t1": "Tight One",
}


def _rival_id(team: int, key: str) -> str:
    return f"p{team}_{key}"


def make_player_lookup() -> dict[str, dict[str, Any]]:
    """Return ``{player_id: {"name","position","fantasy_positions"}}`` for the league."""
    lookup: dict[str, dict[str, Any]] = {}
    for player_id in MY_PLAYERS:
        key = player_id if player_id != "q1" else "q"
        position = _POSITION_OF[key]
        lookup[player_id] = {
            "name": _NAME_OF[key],
            "position": position,
            "fantasy_positions": [position],
        }
    for team, points in RIVAL_POINTS.items():
        for key in points:
            position = _POSITION_OF[key]
            lookup[_rival_id(team, key)] = {
                "name": f"T{team} {_NAME_OF[key]}",
                "position": position,
                "fantasy_positions": [position],
            }
    return lookup


def _entry(roster_id: int, week: int, matchup_id: int) -> dict[str, Any]:
    if roster_id == MY_ROSTER_ID:
        players = list(MY_PLAYERS)
        starters = list(MY_STARTERS)
        points = dict(MY_POINTS[week])
    else:
        points_by_key = RIVAL_POINTS[roster_id]
        players = [_rival_id(roster_id, key) for key in points_by_key]
        starters = [_rival_id(roster_id, key) for key in RIVAL_STARTER_KEYS]
        points = {_rival_id(roster_id, key): value for key, value in points_by_key.items()}
    total = round(sum(points[p] for p in starters), 2)
    return {
        "roster_id": roster_id,
        "matchup_id": matchup_id,
        "points": total,
        "players": players,
        "starters": starters,
        "players_points": points,
        "starters_points": [points[p] for p in starters],
    }


def make_matchups(weeks: tuple[int, ...] = (1, 2, 3)) -> dict[int, list[dict[str, Any]]]:
    """Return ``{week: matchup entries}`` for the requested weeks."""
    out: dict[int, list[dict[str, Any]]] = {}
    for week in weeks:
        entries: list[dict[str, Any]] = []
        for matchup_id, pair in enumerate(SCHEDULE[week], start=1):
            entries.extend(_entry(roster_id, week, matchup_id) for roster_id in pair)
        out[week] = entries
    return out


def make_rosters() -> list[dict[str, Any]]:
    """Return the league's rosters, including one orphan and one co-owned team."""
    rosters: list[dict[str, Any]] = [
        {
            "roster_id": 1,
            "owner_id": "u1",
            "players": list(MY_PLAYERS),
            "starters": list(MY_STARTERS),
            "settings": {
                "wins": 3,
                "losses": 0,
                "ties": 0,
                "fpts": 194,
                "fpts_decimal": 0,
                "fpts_against": 156,
                "fpts_against_decimal": 0,
            },
        }
    ]
    records = {2: (1, 2), 3: (2, 1), 4: (0, 3)}
    for team in (2, 3, 4):
        players = [_rival_id(team, key) for key in RIVAL_POINTS[team]]
        wins, losses = records[team]
        rosters.append(
            {
                "roster_id": team,
                # roster 4 is an orphan (abandoned team); roster 2 is co-owned.
                "owner_id": None if team == 4 else f"u{team}",
                "co_owners": ["u5"] if team == 2 else [],
                "players": players,
                "starters": [_rival_id(team, key) for key in RIVAL_STARTER_KEYS],
                "settings": {"wins": wins, "losses": losses, "ties": 0},
            }
        )
    return rosters


def make_league() -> dict[str, Any]:
    return {
        "league_id": "999",
        "name": "Hand-Checked Dynasty",
        "roster_positions": list(ROSTER_POSITIONS),
        "scoring_settings": {"rec": 1.0},
        "settings": {"scoring_type": "ppr", "num_teams": 4},
    }


def make_users() -> list[dict[str, Any]]:
    return [
        {"user_id": "u1", "display_name": "ryan", "metadata": {"team_name": "Bench Warmers"}},
        {"user_id": "u2", "display_name": "dana", "metadata": {}},
        {"user_id": "u3", "display_name": "sam", "metadata": {"team_name": "Median Kings"}},
        {"user_id": "u5", "display_name": "co-dana", "metadata": {}},
    ]


@pytest.fixture
def player_lookup() -> dict[str, dict[str, Any]]:
    return make_player_lookup()


@pytest.fixture
def matchups() -> dict[int, list[dict[str, Any]]]:
    return make_matchups()


@pytest.fixture
def league() -> dict[str, Any]:
    return make_league()


@pytest.fixture
def facts(
    league: dict[str, Any],
    matchups: dict[int, list[dict[str, Any]]],
    player_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    universe = [*make_player_lookup().keys(), "fa1", "fa2", "fa3"]
    return build_team_report_facts(
        league=league,
        rosters=make_rosters(),
        matchups_by_week=matchups,
        my_roster_id=MY_ROSTER_ID,
        player_lookup=player_lookup,
        users=make_users(),
        player_universe_ids=universe,
        season=2026,
        sleeper_username="ryan",
    )


# --------------------------------------------------------------------------
# Slot handling
# --------------------------------------------------------------------------


def test_starting_slots_drops_bench_ir_and_taxi() -> None:
    slots = starting_slots(["QB", "RB", "FLEX", "BN", "BN", "IR", "TAXI"])
    assert slots == ["QB", "RB", "FLEX"]


# --------------------------------------------------------------------------
# optimal_lineup
# --------------------------------------------------------------------------


def test_optimal_lineup_picks_the_right_flex(player_lookup: dict[str, Any]) -> None:
    """Week 1: FLEX must take w1 (12.0), the best player left after the fixed slots.

    QB->q1 20, RB->r1 15, WR->w2 18, TE->t1 6, FLEX-> best of r2 9 / r3 4 / w1 12.
    """
    result = optimal_lineup(MY_PLAYERS, MY_POINTS[1], ROSTER_POSITIONS, player_lookup)

    assert result["points"] == 71.0
    assert result["starters"] == ["q1", "r1", "w2", "t1", "w1"]
    by_slot = {row["slot"]: row for row in result["by_slot"]}
    assert by_slot["FLEX"]["player_id"] == "w1"
    assert by_slot["FLEX"]["points"] == 12.0
    assert by_slot["WR"]["player_id"] == "w2"  # not the guy who actually started
    assert result["warnings"] == []


def test_optimal_lineup_week3_flex_takes_a_running_back(player_lookup: dict[str, Any]) -> None:
    """Week 3: RB->r3 25, FLEX-> best remaining is r1 12 (an RB, not a WR)."""
    result = optimal_lineup(MY_PLAYERS, MY_POINTS[3], ROSTER_POSITIONS, player_lookup)

    assert result["points"] == 78.0
    assert result["starters"] == ["q1", "r3", "w1", "t1", "r1"]


def test_optimal_lineup_super_flex_prefers_the_second_quarterback() -> None:
    lookup = {
        "qa": {"name": "QB A", "position": "QB", "fantasy_positions": ["QB"]},
        "qb": {"name": "QB B", "position": "QB", "fantasy_positions": ["QB"]},
        "rb": {"name": "RB", "position": "RB", "fantasy_positions": ["RB"]},
    }
    points = {"qa": 20.0, "qb": 18.0, "rb": 15.0}

    result = optimal_lineup(["qa", "qb", "rb"], points, ["QB", "SUPER_FLEX", "BN"], lookup)

    assert result["starters"] == ["qa", "qb"]
    assert result["points"] == 38.0


def test_optimal_lineup_missing_points_score_zero(player_lookup: dict[str, Any]) -> None:
    """A bye/DNP player has no players_points entry and is worth 0.0, not skipped."""
    points = {"q1": 20.0, "r1": 15.0, "w1": 12.0, "t1": 6.0}  # r2/r3/w2 absent

    result = optimal_lineup(MY_PLAYERS, points, ROSTER_POSITIONS, player_lookup)

    assert result["points"] == 53.0  # 20 + 15 + 12 + 6 + 0 (FLEX filled at 0.0)
    assert len(result["starters"]) == 5


def test_optimal_lineup_unknown_slot_matches_on_position() -> None:
    lookup = {"e1": {"name": "Edge", "position": "EDGE", "fantasy_positions": ["EDGE"]}}

    result = optimal_lineup(["e1"], {"e1": 9.0}, ["EDGE", "BN"], lookup)

    assert "unknown_slot:EDGE" in result["warnings"]
    assert result["starters"] == ["e1"]
    assert result["points"] == 9.0


def test_optimal_lineup_unfillable_slot_is_skipped_with_a_warning() -> None:
    lookup = {"q": {"name": "QB", "position": "QB", "fantasy_positions": ["QB"]}}

    result = optimal_lineup(["q"], {"q": 20.0}, ["QB", "TE", "BN"], lookup)

    assert "unfilled_slot:TE" in result["warnings"]
    assert result["starters"] == ["q"]
    assert result["by_slot"][1] == {
        "slot_index": 1,
        "slot": "TE",
        "player_id": None,
        "name": None,
        "position": None,
        "points": 0.0,
    }


def test_optimal_lineup_unknown_player_warns_and_is_never_started() -> None:
    lookup = {"q": {"name": "QB", "position": "QB", "fantasy_positions": ["QB"]}}

    result = optimal_lineup(["q", "ghost"], {"q": 20.0, "ghost": 99.0}, ["QB", "BN"], lookup)

    assert "unknown_player:ghost" in result["warnings"]
    assert result["starters"] == ["q"]


def test_optimal_lineup_solves_non_laminar_slot_configurations_exactly() -> None:
    """WRRB_FLEX {RB,WR} and REC_FLEX {WR,TE} overlap without nesting.

    The greedy fills the tie by slot order — WRRB_FLEX takes the WR (20) and
    REC_FLEX is left the TE (2) — while the RB (15) belonged at WRRB_FLEX.
    This configuration used to be flagged ``non_laminar_slots`` and reported
    short; it is now solved.
    """
    lookup = {
        "w": {"name": "WR", "position": "WR", "fantasy_positions": ["WR"]},
        "r": {"name": "RB", "position": "RB", "fantasy_positions": ["RB"]},
        "t": {"name": "TE", "position": "TE", "fantasy_positions": ["TE"]},
    }
    points = {"w": 20.0, "r": 15.0, "t": 2.0}

    result = optimal_lineup(["w", "r", "t"], points, ["WRRB_FLEX", "REC_FLEX", "BN"], lookup)

    assert result["starters"] == ["r", "w"]
    assert result["points"] == 35.0
    assert result["warnings"] == []


def test_optimal_lineup_places_a_dual_position_player_where_he_is_worth_most() -> None:
    """Audit reproduction: laminar slots, but one player is eligible for both.

    Most-restrictive-first started x at QB (20) and z at TE (1) for 21 with no
    warning; x at TE beside y at QB is 35.
    """
    lookup = {
        "x": {"name": "X", "position": "QB", "fantasy_positions": ["QB", "TE"]},
        "y": {"name": "Y", "position": "QB", "fantasy_positions": ["QB"]},
        "z": {"name": "Z", "position": "TE", "fantasy_positions": ["TE"]},
    }
    points = {"x": 20.0, "y": 15.0, "z": 1.0}

    result = optimal_lineup(["x", "y", "z"], points, ["QB", "TE", "BN"], lookup)

    assert result["points"] == 35.0
    assert result["starters"] == ["y", "x"]
    assert [row["slot"] for row in result["by_slot"]] == ["QB", "TE"]
    assert result["warnings"] == []


def test_optimal_lineup_fills_a_slot_the_greedy_would_leave_empty() -> None:
    """Filling every slot outranks points: the greedy spent the only TE-eligible player at QB."""
    lookup = {
        "x": {"name": "X", "position": "QB", "fantasy_positions": ["QB", "TE"]},
        "y": {"name": "Y", "position": "QB", "fantasy_positions": ["QB"]},
    }

    result = optimal_lineup(["x", "y"], {"x": 20.0, "y": 3.0}, ["QB", "TE"], lookup)

    assert result["starters"] == ["y", "x"]
    assert result["points"] == 23.0
    assert not any(w.startswith("unfilled_slot") for w in result["warnings"])


def test_optimal_lineup_ties_resolve_in_roster_order() -> None:
    """Equal scores: the earlier roster entry starts, every time."""
    lookup = {pid: {"name": pid, "position": "RB", "fantasy_positions": ["RB"]} for pid in "abc"}
    points = {"a": 10.0, "b": 10.0, "c": 10.0}

    first = optimal_lineup(["b", "a", "c"], points, ["RB", "FLEX"], lookup)
    again = optimal_lineup(["b", "a", "c"], points, ["RB", "FLEX"], lookup)

    assert first == again
    assert first["starters"] == ["b", "a"]


def test_optimal_lineup_starts_a_negative_scorer_rather_than_leave_a_slot_empty() -> None:
    lookup = {"d": {"name": "D", "position": "DEF", "fantasy_positions": ["DEF"]}}

    result = optimal_lineup(["d"], {"d": -3.0}, ["DEF"], lookup)

    assert result["starters"] == ["d"]
    assert result["points"] == -3.0


def test_optimal_lineup_ignores_empty_starter_markers() -> None:
    lookup = {"q": {"name": "QB", "position": "QB", "fantasy_positions": ["QB"]}}

    result = optimal_lineup(["0", "q", None], {"q": 20.0}, ["QB", "BN"], lookup)

    assert result["starters"] == ["q"]


# --------------------------------------------------------------------------
# lineup_efficiency
# --------------------------------------------------------------------------


def test_lineup_efficiency_exact_bench_points_lost(player_lookup: dict[str, Any]) -> None:
    optimal = optimal_lineup(MY_PLAYERS, MY_POINTS[1], ROSTER_POSITIONS, player_lookup)

    efficiency = lineup_efficiency(MY_STARTERS, MY_POINTS[1], optimal)

    assert efficiency == {
        "actual_points": 62.0,
        "optimal_points": 71.0,
        "bench_points_lost": 9.0,
        "efficiency_pct": 87.32,  # 62 / 71
    }


@pytest.mark.parametrize(
    ("week", "actual", "optimal_points", "lost", "pct"),
    [(1, 62.0, 71.0, 9.0, 87.32), (2, 74.0, 88.0, 14.0, 84.09), (3, 58.0, 78.0, 20.0, 74.36)],
)
def test_lineup_efficiency_every_week(
    player_lookup: dict[str, Any],
    week: int,
    actual: float,
    optimal_points: float,
    lost: float,
    pct: float,
) -> None:
    optimal = optimal_lineup(MY_PLAYERS, MY_POINTS[week], ROSTER_POSITIONS, player_lookup)

    efficiency = lineup_efficiency(MY_STARTERS, MY_POINTS[week], optimal)

    assert efficiency["actual_points"] == actual
    assert efficiency["optimal_points"] == optimal_points
    assert efficiency["bench_points_lost"] == lost
    assert efficiency["efficiency_pct"] == pct


def test_lineup_efficiency_zero_scoring_week_is_100_pct() -> None:
    optimal = {"points": 0.0}

    assert lineup_efficiency([], {}, optimal)["efficiency_pct"] == 100.0


def test_lineup_efficiency_never_exceeds_100_when_a_starter_is_unknown() -> None:
    """An unresolvable starter can outscore the optimizer's best legal lineup."""
    lookup = {"q": {"name": "QB", "position": "QB", "fantasy_positions": ["QB"]}}
    points = {"q": 10.0, "ghost": 40.0}
    optimal = optimal_lineup(["q", "ghost"], points, ["QB", "FLEX", "BN"], lookup)

    efficiency = lineup_efficiency(["q", "ghost"], points, optimal)

    assert efficiency["efficiency_pct"] == 100.0
    assert efficiency["bench_points_lost"] == 0.0


# --------------------------------------------------------------------------
# weekly_review
# --------------------------------------------------------------------------


def test_weekly_review_season_aggregates(
    matchups: dict[int, list[dict[str, Any]]],
    league: dict[str, Any],
    player_lookup: dict[str, Any],
) -> None:
    review = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)
    season = review["season"]

    assert [w["week"] for w in review["weeks"]] == [1, 2, 3]
    assert season["total_actual_points"] == 194.0  # 62 + 74 + 58
    assert season["total_optimal_points"] == 237.0  # 71 + 88 + 78
    assert season["total_bench_points_lost"] == 43.0  # 9 + 14 + 20
    assert season["avg_efficiency_pct"] == 81.86  # 194 / 237
    assert season["mean_weekly_efficiency_pct"] == 81.92  # mean(87.32, 84.09, 74.36)
    assert season["worst_week"]["week"] == 3
    assert season["worst_week"]["efficiency_pct"] == 74.36
    assert season["best_week"]["week"] == 1
    assert review["warnings"] == []


def test_weekly_review_detects_the_repeated_mis_start(
    matchups: dict[int, list[dict[str, Any]]],
    league: dict[str, Any],
    player_lookup: dict[str, Any],
) -> None:
    """w2 is benched in all three weeks; r3 once. w2 is the top offender."""
    season = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)["season"]

    assert season["mis_start_count"] == 4
    assert season["mis_starts_by_slot"] == {
        "WR": {"count": 2, "points_lost": 20.0},  # wk1 +6.0, wk2 +14.0
        "RB": {"count": 1, "points_lost": 13.0},  # wk3 r1 12 -> r3 25
        "FLEX": {"count": 1, "points_lost": 2.0},  # wk3 r2 5 -> w2 7
    }
    assert season["mis_starts_by_position"] == {
        "WR": {"count": 3, "points_lost": 22.0},
        "RB": {"count": 1, "points_lost": 13.0},
    }

    top = season["top_offenders"]
    assert top[0]["player_id"] == "w2"
    assert top[0]["times_benched"] == 3
    assert top[0]["points_lost"] == 22.0
    assert top[0]["weeks"] == [1, 2, 3]
    assert top[1]["player_id"] == "r3"

    assert season["patterns"] == [
        "2 mis-starts in the WR slot, costing 20.0 pts",
        "Benched Wide Two (WR) 3 times for 22.0 pts",
    ]


def test_weekly_review_week_one_mis_start_detail(
    matchups: dict[int, list[dict[str, Any]]],
    league: dict[str, Any],
    player_lookup: dict[str, Any],
) -> None:
    week_one = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)["weeks"][0]

    assert len(week_one["mis_starts"]) == 1
    finding = week_one["mis_starts"][0]
    assert finding["slot"] == "WR"
    assert finding["started_player_id"] == "w1"
    assert finding["started_points"] == 12.0
    assert finding["benched_player_id"] == "w2"
    assert finding["benched_points"] == 18.0
    assert finding["points_lost"] == 6.0
    # Mis-starts only charge *benched* players, so they are a floor on the loss:
    # the full 9.0 also needs the knock-on move of w1 into the FLEX.
    assert week_one["bench_points_lost"] == 9.0


def test_weekly_review_charges_each_benched_player_to_one_slot_only(
    matchups: dict[int, list[dict[str, Any]]],
    league: dict[str, Any],
    player_lookup: dict[str, Any],
) -> None:
    """Week 3: r3 is eligible for RB *and* FLEX but is counted once."""
    week_three = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)["weeks"][2]

    benched = [f["benched_player_id"] for f in week_three["mis_starts"]]
    assert benched == ["r3", "w2"]
    assert [f["slot"] for f in week_three["mis_starts"]] == ["RB", "FLEX"]


def test_weekly_review_skips_empty_and_absent_weeks(
    league: dict[str, Any], player_lookup: dict[str, Any]
) -> None:
    matchups = make_matchups(weeks=(1,))
    matchups[2] = []  # week not played yet
    matchups[3] = [e for e in make_matchups(weeks=(3,))[3] if e["roster_id"] != MY_ROSTER_ID]

    review = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)

    assert review["season"]["weeks_analyzed"] == [1]


def test_weekly_review_with_no_history_does_not_crash(
    league: dict[str, Any], player_lookup: dict[str, Any]
) -> None:
    review = weekly_review({}, MY_ROSTER_ID, league, player_lookup)

    assert review["weeks"] == []
    assert review["season"]["avg_efficiency_pct"] is None
    assert review["season"]["worst_week"] is None
    assert review["season"]["patterns"] == []
    assert "no_matchup_history" in review["warnings"]


def test_weekly_review_accepts_string_week_keys(
    league: dict[str, Any], player_lookup: dict[str, Any]
) -> None:
    matchups = {str(week): rows for week, rows in make_matchups().items()}

    review = weekly_review(matchups, MY_ROSTER_ID, league, player_lookup)

    assert review["season"]["weeks_analyzed"] == [1, 2, 3]


# --------------------------------------------------------------------------
# luck_analysis
# --------------------------------------------------------------------------


def test_luck_analysis_weekly_medians_and_all_play(
    matchups: dict[int, list[dict[str, Any]]],
) -> None:
    """Even team count -> the median averages the two middle scores."""
    weeks = luck_analysis(matchups, MY_ROSTER_ID)["weeks"]

    # week 1 scores: 62, 56, 67, 33 -> sorted 33/56/62/67 -> median (56+62)/2
    assert weeks[0]["league_median"] == 59.0
    assert weeks[0]["median_delta"] == 3.0
    assert weeks[0]["above_median"] is True
    assert (weeks[0]["all_play_wins"], weeks[0]["all_play_losses"]) == (2, 1)
    assert weeks[0]["opponent_roster_id"] == 2
    assert weeks[0]["points_against"] == 56.0
    assert weeks[0]["result"] == "W"

    # week 2 scores: 74, 56, 67, 33 -> median (56+67)/2 = 61.5; beats everyone
    assert weeks[1]["league_median"] == 61.5
    assert (weeks[1]["all_play_wins"], weeks[1]["all_play_losses"]) == (3, 0)

    # week 3 scores: 58, 56, 67, 33 -> median (56+58)/2 = 57.0
    assert weeks[2]["league_median"] == 57.0
    assert (weeks[2]["all_play_wins"], weeks[2]["all_play_losses"]) == (2, 1)


def test_luck_analysis_positive_score_means_lucky(
    matchups: dict[int, list[dict[str, Any]]],
) -> None:
    """3-0 actual vs 7-2 all-play: expected 2/3 + 3/3 + 2/3 = 2.33 wins."""
    season = luck_analysis(matchups, MY_ROSTER_ID)["season"]

    assert season["actual_record"] == "3-0-0"
    assert season["all_play_record"] == "7-2-0"
    assert season["expected_wins"] == 2.33
    assert season["luck_score"] == 0.67
    assert season["luck_label"] == "lucky"
    assert season["points_for"] == 194.0
    assert season["points_against"] == 156.0  # 56 + 67 + 33
    assert season["points_against_percentile"] == 0.0  # softest schedule in the league
    assert season["median_record"] == "3-0"
    assert season["avg_points"] == 64.67
    assert season["avg_points_against"] == 52.0


def test_luck_analysis_negative_score_means_unlucky(
    matchups: dict[int, list[dict[str, Any]]],
) -> None:
    """Roster 3 scores 67 every week, goes 2-1, and all-plays 8-1."""
    season = luck_analysis(matchups, 3)["season"]

    assert season["actual_record"] == "2-1-0"
    assert season["all_play_record"] == "8-1-0"
    assert season["expected_wins"] == 2.67  # 1 + 2/3 + 1
    assert season["luck_score"] == -0.67
    assert season["luck_label"] == "unlucky"
    # PA 33 + 74 + 56 = 163; two of the three rivals faced fewer points.
    assert season["points_against"] == 163.0
    assert season["points_against_percentile"] == 66.67


def test_luck_analysis_skips_unplayed_weeks(
    matchups: dict[int, list[dict[str, Any]]],
) -> None:
    for entry in matchups[3]:
        entry["points"] = 0.0

    result = luck_analysis(matchups, MY_ROSTER_ID)

    assert result["season"]["weeks_analyzed"] == [1, 2]
    assert "unplayed_week:w3" in result["warnings"]


def test_luck_analysis_with_no_history_does_not_crash() -> None:
    result = luck_analysis({}, MY_ROSTER_ID)

    assert result["weeks"] == []
    assert result["season"]["expected_wins"] is None
    assert result["season"]["luck_label"] == "unknown"
    assert "no_matchup_history" in result["warnings"]


# --------------------------------------------------------------------------
# league_comparison
# --------------------------------------------------------------------------


def test_league_comparison_efficiency_rank_is_last(
    matchups: dict[int, list[dict[str, Any]]], player_lookup: dict[str, Any]
) -> None:
    """Rivals always start optimally (100%); I do not, so I rank 4th of 4."""
    comparison = league_comparison(
        make_rosters(), matchups, MY_ROSTER_ID, player_lookup, ROSTER_POSITIONS
    )

    assert comparison["league_size"] == 4
    assert comparison["my_efficiency_rank"] == 4
    by_id = {team["roster_id"]: team for team in comparison["teams"]}
    assert by_id[1]["efficiency_pct"] == 81.86
    assert by_id[1]["actual_points"] == 194.0
    assert by_id[1]["bench_points_lost"] == 43.0
    for roster_id in (2, 3, 4):
        assert by_id[roster_id]["efficiency_pct"] == 100.0
        assert by_id[roster_id]["bench_points_lost"] == 0.0
        assert by_id[roster_id]["efficiency_rank"] == 1  # ties share a rank
    assert comparison["my_points_rank"] == 2  # 194 vs 168 / 201 / 99
    assert "roster_without_owner:4" in comparison["warnings"]


def test_league_comparison_positional_strength(
    matchups: dict[int, list[dict[str, Any]]], player_lookup: dict[str, Any]
) -> None:
    """Starters are credited to their own position, so FLEX-started RBs count as RB.

    My weekly starter points by position:
      QB 20+25+18 = 63 -> 21.00/wk
      RB (RB slot + FLEX) 24+30+17 = 71 -> 23.67/wk
      WR 12+8+14 = 34 -> 11.33/wk
      TE 6+11+9 = 26 -> 8.67/wk
    """
    comparison = league_comparison(
        make_rosters(), matchups, MY_ROSTER_ID, player_lookup, ROSTER_POSITIONS
    )

    assert comparison["position_groups"] == ["QB", "RB", "TE", "WR"]
    by_position = {row["position"]: row for row in comparison["positional_strength"]}
    assert by_position["QB"]["points_per_week"] == 21.0
    assert by_position["RB"]["points_per_week"] == 23.67
    assert by_position["WR"]["points_per_week"] == 11.33
    assert by_position["TE"]["points_per_week"] == 8.67

    # League QB pts/wk: 21.0 / 20.0 / 15.0 / 10.0 -> mean 16.5, pstdev 4.387, z 1.026
    assert by_position["QB"]["league_rank"] == 1
    assert by_position["QB"]["league_avg_points_per_week"] == 16.5
    assert by_position["QB"]["z_score"] == 1.03
    assert by_position["QB"]["grade"] == "A-"
    assert by_position["RB"]["league_rank"] == 2
    assert by_position["RB"]["grade"] == "B+"
    assert by_position["WR"]["grade"] == "B-"
    assert by_position["TE"]["grade"] == "B"

    # Nothing is bottom-third for a team ranked 1/2/2/2 of 4.
    assert comparison["deficiencies"] == []


def test_league_comparison_bottom_third_deficiencies(
    matchups: dict[int, list[dict[str, Any]]], player_lookup: dict[str, Any]
) -> None:
    """Roster 4 is last at every position -> four high-severity deficiencies."""
    comparison = league_comparison(make_rosters(), matchups, 4, player_lookup, ROSTER_POSITIONS)

    deficiencies = comparison["deficiencies"]
    assert {d["position"] for d in deficiencies} == {"QB", "RB", "WR", "TE"}
    assert all(d["severity"] == "high" for d in deficiencies)
    assert all(d["league_rank"] == 4 for d in deficiencies)
    assert all(d["available_fixes"] == [] for d in deficiencies)
    assert all("pts/week" in d["detail"] for d in deficiencies)
    # Sorted worst z-score first.
    assert deficiencies[0]["z_score"] <= deficiencies[-1]["z_score"]


@pytest.mark.parametrize(
    ("z_score", "grade"),
    [
        (2.5, "A+"),
        (1.5, "A"),
        (1.0, "A-"),
        (0.6, "B+"),
        (0.3, "B"),
        (0.0, "B-"),
        (-0.1, "C+"),
        (-0.4, "C"),
        (-0.6, "C-"),
        (-1.0, "D"),
        (-3.0, "F"),
    ],
)
def test_grade_from_z_bands(z_score: float, grade: str) -> None:
    assert grade_from_z(z_score) == grade


# --------------------------------------------------------------------------
# free_agent_pool
# --------------------------------------------------------------------------


def test_free_agent_pool_subtracts_every_rostered_player() -> None:
    universe = [*make_player_lookup().keys(), "fa1", "fa2", "fa3"]

    pool = free_agent_pool(make_rosters(), universe)

    assert pool == {"fa1", "fa2", "fa3"}
    assert isinstance(pool, set)


def test_free_agent_pool_ignores_players_outside_the_universe() -> None:
    rosters = [{"roster_id": 1, "players": ["a", "b"], "starters": ["a"]}]

    assert free_agent_pool(rosters, ["b", "c"]) == {"c"}


def test_free_agent_pool_handles_missing_arrays_and_id_types() -> None:
    rosters = [{"roster_id": 1}, {"roster_id": 2, "players": [11, "0", None]}]

    assert free_agent_pool(rosters, [11, 12, "0"]) == {"12"}


# --------------------------------------------------------------------------
# build_team_report_facts
# --------------------------------------------------------------------------


def test_facts_are_json_serializable(facts: dict[str, Any]) -> None:
    encoded = json.dumps(facts)

    assert json.loads(encoded) == facts


def test_facts_provenance_and_week_range(facts: dict[str, Any]) -> None:
    assert facts["computed"]["method"] == "deterministic"
    assert facts["computed"]["by"] == "api.data.team_analytics"
    assert facts["computed"]["version"] == FACTS_VERSION
    assert "do not recompute" in facts["computed"]["rule"]

    assert facts["week_range"] == {
        "season": 2026,
        "first_week": 1,
        "last_week": 3,
        "through_week": 3,
        "weeks_analyzed": [1, 2, 3],
        "weeks_missing": [],
        "weeks_count": 3,
    }


def test_facts_league_and_team_identity(facts: dict[str, Any]) -> None:
    assert facts["league"]["league_id"] == "999"
    assert facts["league"]["size"] == 4
    assert facts["league"]["starting_slots"] == ["QB", "RB", "WR", "TE", "FLEX"]
    assert facts["league"]["scoring_type"] == "ppr"

    assert facts["team"]["roster_id"] == 1
    assert facts["team"]["team_name"] == "Bench Warmers"
    assert facts["team"]["display_name"] == "ryan"
    assert facts["team"]["sleeper_username"] == "ryan"
    assert facts["team"]["is_orphan"] is False
    assert facts["team"]["wins"] == 3
    assert facts["team"]["points_for"] == 194.0


def test_facts_label_orphan_and_co_owned_teams(facts: dict[str, Any]) -> None:
    by_id = {team["roster_id"]: team for team in facts["league_comparison"]["teams"]}

    assert by_id[4]["team_name"] == "Team 4"  # abandoned roster, no owner
    assert by_id[2]["team_name"] == "dana"  # no metadata.team_name -> display_name
    assert facts["warnings"] == ["roster_without_owner:4"]


def test_facts_manager_review_matches_the_schema(facts: dict[str, Any]) -> None:
    review = facts["manager_review"]

    assert review["bench_points_lost"] == 43.0
    assert review["optimal_vs_actual"] == 43.0
    assert review["lineup_efficiency_pct"] == 81.86
    assert review["efficiency_rank"] == 4
    assert review["league_size"] == 4
    assert review["expected_wins"] == 2.33
    assert review["actual_wins"] == 3
    assert review["luck_score"] == 0.67
    assert review["observations"] == []
    assert review["mis_start_patterns"][0].startswith("2 mis-starts in the WR slot")
    assert "all-play record of 7-2-0" in review["luck_note"]

    model = ManagerReview(**review)
    assert model.lineup_efficiency_pct == 81.86
    assert model.efficiency_rank == 4


def test_facts_positional_strength_and_deficiencies_match_the_schemas(
    facts: dict[str, Any],
) -> None:
    strength = facts["positional_strength_vs_league"]
    assert [row["position"] for row in strength] == ["QB", "RB", "TE", "WR"]
    models = [PositionalStrength(**row) for row in strength]
    assert models[0].grade == "A-"
    assert models[0].league_size == 4

    # A bottom-third team's deficiencies also load into the response model.
    weak = build_team_report_facts(
        league=make_league(),
        rosters=make_rosters(),
        matchups_by_week=make_matchups(),
        my_roster_id=4,
        player_lookup=make_player_lookup(),
        users=make_users(),
        player_universe_ids=list(make_player_lookup()),
    )
    assert len(weak["deficiencies"]) == 4
    assert all(Deficiency(**d).available_fixes == [] for d in weak["deficiencies"])


def test_facts_free_agent_pool_is_a_sorted_list(facts: dict[str, Any]) -> None:
    pool = facts["free_agent_pool"]

    assert pool["count"] == 3
    assert pool["player_ids"] == ["fa1", "fa2", "fa3"]


def test_facts_without_a_player_universe_warns(
    league: dict[str, Any], matchups: dict[int, list[dict[str, Any]]]
) -> None:
    result = build_team_report_facts(
        league=league,
        rosters=make_rosters(),
        matchups_by_week=matchups,
        my_roster_id=MY_ROSTER_ID,
        player_lookup=make_player_lookup(),
    )

    assert result["free_agent_pool"] == {"count": 0, "player_ids": []}
    assert "no_player_universe" in result["warnings"]


def test_facts_week_one_only_is_a_minimal_but_complete_report(
    league: dict[str, Any],
) -> None:
    """Week 1: no history, but every block must still be present and sane."""
    result = build_team_report_facts(
        league=league,
        rosters=make_rosters(),
        matchups_by_week=make_matchups(weeks=(1,)),
        my_roster_id=MY_ROSTER_ID,
        player_lookup=make_player_lookup(),
        users=make_users(),
        player_universe_ids=list(make_player_lookup()),
        season=2026,
        sleeper_username="ryan",
    )

    assert result["week_range"]["weeks_analyzed"] == [1]
    assert result["lineup_efficiency"]["season"]["total_bench_points_lost"] == 9.0
    assert result["manager_review"]["lineup_efficiency_pct"] == 87.32
    assert result["manager_review"]["actual_wins"] == 1
    assert result["manager_review"]["expected_wins"] == 0.67
    # Single mis-start, no repeats -> the "biggest single mis-start" fallback fires.
    assert result["manager_review"]["mis_start_patterns"][0].startswith("Biggest single mis-start")
    assert ManagerReview(**result["manager_review"]).league_size == 4
    json.dumps(result)


def test_facts_with_no_matchups_at_all_does_not_crash(league: dict[str, Any]) -> None:
    result = build_team_report_facts(
        league=league,
        rosters=make_rosters(),
        matchups_by_week={},
        my_roster_id=MY_ROSTER_ID,
        player_lookup=make_player_lookup(),
        player_universe_ids=[],
    )

    assert result["week_range"]["weeks_analyzed"] == []
    assert result["week_range"]["last_week"] is None
    assert result["manager_review"]["lineup_efficiency_pct"] == 0.0
    assert result["manager_review"]["expected_wins"] is None
    assert result["manager_review"]["mis_start_patterns"] == []
    assert "no_matchup_history" in result["warnings"]
    assert ManagerReview(**result["manager_review"]).bench_points_lost == 0.0
    json.dumps(result)


def test_facts_are_pure_and_do_not_mutate_their_inputs(
    league: dict[str, Any], player_lookup: dict[str, Any]
) -> None:
    matchups = make_matchups()
    rosters = make_rosters()
    before = json.dumps([league, matchups, rosters, player_lookup], sort_keys=True)

    build_team_report_facts(
        league=league,
        rosters=rosters,
        matchups_by_week=matchups,
        my_roster_id=MY_ROSTER_ID,
        player_lookup=player_lookup,
        users=make_users(),
        player_universe_ids=list(player_lookup),
    )

    assert json.dumps([league, matchups, rosters, player_lookup], sort_keys=True) == before


# ---------------------------------------------------------------------------
# Before kickoff
# ---------------------------------------------------------------------------


def test_mark_unplayed_removes_grades_computed_from_no_games() -> None:
    """A "B-" off zero games is no assessment, and it reads as a weak one.

    This runs in the route rather than in an engine on purpose: the ADK
    synthesis agent is instructed to reproduce these blocks exactly, so the
    misleading numbers have to be gone before it is asked to narrate them.
    """
    facts = {
        "warnings": ["no_matchup_history"],
        "positional_strength_vs_league": [
            {"position": "QB", "league_rank": 1, "league_size": 10, "grade": "B-"}
        ],
        "manager_review": {"lineup_efficiency_pct": 0.0, "efficiency_rank": 1},
    }

    result = mark_unplayed(facts)

    assert result["positional_strength_vs_league"] == []
    review = result["manager_review"]
    assert "not yet played" in review["observations"][0]
    assert "nothing to call lucky" in review["luck_note"]
    # The zeroed shape survives — the contract requires the block.
    assert review["lineup_efficiency_pct"] == 0.0


def test_mark_unplayed_leaves_a_played_season_alone() -> None:
    """Mid-season the same numbers are real, and must not be stripped."""
    facts = {
        "warnings": [],
        "positional_strength_vs_league": [{"position": "QB", "grade": "A-"}],
        "manager_review": {"lineup_efficiency_pct": 91.4, "observations": ["kept"]},
    }

    result = mark_unplayed(facts)

    assert result["positional_strength_vs_league"] == [{"position": "QB", "grade": "A-"}]
    assert result["manager_review"]["observations"] == ["kept"]


def test_mark_unplayed_is_idempotent_and_pure() -> None:
    facts = {"warnings": ["no_matchup_history"], "manager_review": {}}
    before = json.dumps(facts, sort_keys=True)

    once = mark_unplayed(facts)
    twice = mark_unplayed(once)

    assert once == twice
    assert json.dumps(facts, sort_keys=True) == before


def test_week_one_outlook_leans_to_the_better_rated_lineup() -> None:
    """The lean follows the market totals, and is never a probability."""
    matchups = [
        {"roster_id": 1, "matchup_id": 7, "starters": ["p1", "p2"]},
        {"roster_id": 2, "matchup_id": 7, "starters": ["p3", "p4"]},
    ]
    outlook = week_one_outlook(
        matchups=matchups,
        my_roster_id=1,
        rosters=[{"roster_id": 2, "owner_id": "u2"}],
        users=[{"user_id": "u2", "display_name": "rival", "metadata": {"team_name": "Rivals"}}],
        market_ranks={"p1": 1, "p2": 2, "p3": 300, "p4": 320},
    )

    assert outlook is not None
    assert outlook["opponent_team_name"] == "Rivals"
    assert outlook["my_market_score"] > outlook["opponent_market_score"]
    assert outlook["lean"] == "clear edge"
    assert "not an ADP" in outlook["basis"]
    assert "%" not in outlook["lean"]


def test_week_one_outlook_is_none_without_an_opponent() -> None:
    """A bye, or a roster missing from the week, is not a matchup to lean on."""
    matchups = [{"roster_id": 1, "matchup_id": 7, "starters": ["p1"]}]

    assert week_one_outlook(matchups=matchups, my_roster_id=1, market_ranks={"p1": 1}) is None
    assert week_one_outlook(matchups=[], my_roster_id=1) is None


def test_week_one_outlook_survives_players_with_no_market_rank() -> None:
    """An unranked bench-filler contributes nothing rather than crashing."""
    matchups = [
        {"roster_id": 1, "matchup_id": 1, "starters": ["p1", "unknown", "0"]},
        {"roster_id": 2, "matchup_id": 1, "starters": ["p2"]},
    ]
    outlook = week_one_outlook(matchups=matchups, my_roster_id=1, market_ranks={"p1": 10, "p2": 12})

    assert outlook is not None
    assert outlook["my_starters_scored"] == "1 of 2"
    assert "partial_market_coverage" in outlook["warnings"]
