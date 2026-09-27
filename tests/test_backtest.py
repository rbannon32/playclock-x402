"""The backtest: the bar, the rules, the aggregate, and the task that runs them.

The published hit rate is only worth publishing if the bar is the one the docs
describe and nothing is scored twice. Every rule in :mod:`ingest.backtest`'s
docstring has a case here, including the two that are easy to get backwards:
a scratch is a miss for a start call and a hit for a sit call, and a matchup
tie is not a wrong call.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import pytest

from api.core.config import Settings
from api.core.store import Store
from api.data.predictions import BACKTEST_COLLECTION, PREDICTIONS_COLLECTION, prediction_id
from api.data.stats_store import (
    FRESHNESS_DOC_ID,
    META_COLLECTION,
    PLAYERS_COLLECTION,
    SCHEDULES_COLLECTION,
    weekly_stats_collection,
)
from ingest import job
from ingest.backtest import (
    STARTABLE_RANK,
    UNSCORABLE_POSITIONS,
    WEEK_COMPLETE_GRACE,
    alias_gsis_lines,
    line_points,
    run_backtest,
    score_prediction,
    startable_thresholds,
    summarize,
    teams_missing_lines,
    week_complete,
)

SEASON = 2026

#: Fixture weeks: Thursday night through Monday night, one week apart.
KICKOFFS: dict[int, tuple[str, str]] = {
    1: ("2026-09-11T00:15:00Z", "2026-09-15T00:15:00Z"),
    2: ("2026-09-18T00:15:00Z", "2026-09-22T00:15:00Z"),
    3: ("2026-09-25T00:15:00Z", "2026-09-29T00:15:00Z"),
}

#: Weeks 1 and 2 played; week 3 not yet kicked off.
AFTER_WEEK_2 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
#: Every fixture week played.
AFTER_WEEK_3 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
#: The stats task's Tuesday-morning stamp after week 2, and after week 3.
STAMP_AFTER_WEEK_2 = "2026-09-22T13:01:00Z"
STAMP_AFTER_WEEK_3 = "2026-09-29T13:01:00Z"


async def _stamp_stats(store: Store, marker: str) -> None:
    """What a finished ``stats`` run leaves behind."""
    await store.set(META_COLLECTION, FRESHNESS_DOC_ID, {"weekly_stats": marker}, merge=True)


def _line(player_id: str, position: str, points: float, **fields: Any) -> dict[str, Any]:
    return {
        "player_id": player_id,
        "position": position,
        "fantasy_points_ppr": points,
        "fantasy_points": points - 1.0,
        **fields,
    }


def _claim(kind: str, player_id: str, *, week: int = 1, endpoint: str = "roster", **fields: Any):
    doc = {
        "season": SEASON,
        "week": week,
        "endpoint": endpoint,
        "kind": kind,
        "player_id": player_id,
        "name": f"Player {player_id}",
        "position": "RB",
        "recorded_at": "2026-09-10T12:00:00Z",
        "scored": False,
        "hit": None,
        "points": None,
        **fields,
    }
    return doc


# --------------------------------------------------------------------------
# Points and the startable bar
# --------------------------------------------------------------------------


def test_ppr_is_preferred_and_standard_is_the_fallback() -> None:
    assert line_points({"fantasy_points_ppr": 12.5, "fantasy_points": 9.0}) == 12.5
    assert line_points({"fantasy_points": 9.0}) == 9.0
    assert line_points({"fantasy_points_ppr": None, "fantasy_points": 9.0}) == 9.0
    assert line_points({"fantasy_points_ppr": True}) == 0.0
    assert line_points({}) == 0.0


def test_the_bar_is_the_nth_best_score_at_the_position() -> None:
    lines = [_line(str(i), "RB", float(31 - i)) for i in range(1, 31)]  # 30.0 down to 1.0
    lines += [_line(f"q{i}", "QB", float(20 - i)) for i in range(1, 6)]  # five QBs, 19..15
    lines += [_line("fb", "FB", 40.0), {"player_id": "x", "position": None, "fantasy_points": 50}]

    bars = startable_thresholds(lines)

    assert bars["RB"] == 30.0 - (STARTABLE_RANK["RB"] - 1)
    # Fewer lines than the rank asks for: everyone who played is startable.
    assert bars["QB"] == 15.0
    assert "FB" not in bars and "" not in bars


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "points", "expected"),
    [
        ("start", 10.0, True),
        ("start", 8.0, True),  # at the bar counts
        ("start", 7.9, False),
        ("sit", 7.9, True),
        ("sit", 8.0, False),
        ("add", 9.0, True),
        ("fade", 9.0, False),
        ("sleeper", 8.5, True),
        ("waiver", 2.0, False),
        ("emerging", 12.0, True),
    ],
)
def test_positive_and_negative_kinds_against_the_bar(
    kind: str, points: float, expected: bool
) -> None:
    hit, scored_points = score_prediction(
        _claim(kind, "1"), {"1": points}, {"RB": 8.0}, {"1": "RB"}
    )
    assert (hit, scored_points) == (expected, points)


def test_a_scratch_scores_zero_so_start_misses_and_sit_hits() -> None:
    assert score_prediction(_claim("start", "9"), {}, {"RB": 8.0}) == (False, 0.0)
    assert score_prediction(_claim("sit", "9"), {}, {"RB": 8.0}) == (True, 0.0)


def test_an_unjoinable_player_with_no_line_is_unscorable_in_either_direction() -> None:
    """No gsis id, no line: absence from the stat file says nothing about his game."""
    assert score_prediction(_claim("fade", "9"), {}, {"RB": 8.0}, None, {"9"}) == (None, None)
    assert score_prediction(_claim("add", "9"), {}, {"RB": 8.0}, None, {"9"}) == (None, None)
    # A line under his id always scores, whatever the lookup said.
    assert score_prediction(_claim("add", "9"), {"9": 0.0}, {"RB": 8.0}, None, {"9"}) == (
        False,
        0.0,
    )
    # A matchup against him cannot be judged either.
    doc = _claim("matchup_top", "1", endpoint="matchup", group=["1", "9"])
    assert score_prediction(doc, {"1": 12.0}, {}, None, {"9"}) == (None, 12.0)


def test_a_matchup_tie_is_not_a_wrong_call() -> None:
    doc = _claim("matchup_top", "1", endpoint="matchup", group=["1", "2", "3"])

    assert score_prediction(doc, {"1": 15.0, "2": 15.0, "3": 3.0}, {}) == (True, 15.0)
    assert score_prediction(doc, {"1": 10.0, "2": 15.0}, {}) == (False, 10.0)
    # A missing group member scored zero and cannot beat the winner.
    assert score_prediction(doc, {"1": 0.5}, {}) == (True, 0.5)


def test_a_position_with_no_bar_is_unscorable_not_wrong() -> None:
    doc = _claim("start", "1", position="FB")
    assert score_prediction(doc, {"1": 30.0}, {"RB": 8.0}) == (None, 30.0)


def test_the_stat_line_position_fills_in_for_a_claim_without_one() -> None:
    doc = _claim("start", "1", position=None)
    assert score_prediction(doc, {"1": 9.0}, {"WR": 8.0}, {"1": "WR"}) == (True, 9.0)


# --------------------------------------------------------------------------
# Kickers and defenses: the stat lines cannot express their points
# --------------------------------------------------------------------------


def test_kickers_and_defenses_have_no_bar() -> None:
    """nflverse fantasy points carry no kicking, so every kicker line is ~0."""
    assert "K" not in STARTABLE_RANK and "DEF" not in STARTABLE_RANK
    assert {"K", "DEF"} <= UNSCORABLE_POSITIONS
    lines = [_line(f"k{i}", "K", 0.0) for i in range(20)]
    assert "K" not in startable_thresholds(lines)


@pytest.mark.parametrize("kind", ["add", "waiver", "sleeper", "start", "fade", "sit"])
def test_a_kicker_claim_is_unscorable_in_either_direction(kind: str) -> None:
    doc = _claim(kind, "k1", endpoint="waivers", position="K")
    # Even handed a bar for K (as the old table had), the claim is not scored.
    assert score_prediction(doc, {"k1": 0.0}, {"K": 0.0}, {"k1": "K"}) == (None, 0.0)
    # A claim with no position, placed at K by its stat line, too.
    bare = _claim(kind, "k1", position=None)
    assert score_prediction(bare, {"k1": 0.0}, {"K": 0.0}, {"k1": "K"})[0] is None
    defense = _claim(kind, "SEA", endpoint="waivers", position="DEF")
    assert score_prediction(defense, {}, {"DEF": 0.0}) == (None, 0.0)


def test_a_matchup_involving_a_kicker_is_unscorable() -> None:
    doc = _claim("matchup_top", "k1", endpoint="matchup", position="K", group=["k1", "k2"])
    assert score_prediction(doc, {}, {}, {"k1": "K", "k2": "K"}) == (None, 0.0)
    mixed = _claim("matchup_top", "1", endpoint="matchup", position=None, group=["1", "k2"])
    assert score_prediction(mixed, {"1": 3.0}, {}, {"1": "RB", "k2": "K"}) == (None, 3.0)


def test_kicker_claims_scored_before_the_fix_leave_the_published_rate() -> None:
    """Archived K claims already stored as hits must not count as hits."""
    scored = [
        {**_claim("start", "1"), "scored": True, "hit": True},
        {**_claim("add", "k1", endpoint="waivers", position="K"), "scored": True, "hit": True},
        {**_claim("fade", "k2", endpoint="trending", position="K"), "scored": True, "hit": False},
    ]

    summary = summarize(SEASON, scored, now="2026-09-24T12:00:00Z")

    assert summary["overall"] == {"scored": 1, "hits": 1, "hit_rate": 1.0}
    assert summary["unscorable"] == 2
    assert [row["key"] for row in summary["by_endpoint"]] == ["roster"]


# --------------------------------------------------------------------------
# The aggregate
# --------------------------------------------------------------------------


def test_summary_counts_hits_per_endpoint_and_kind_and_excludes_the_unscorable() -> None:
    scored = [
        {**_claim("start", "1", week=1), "scored": True, "hit": True},
        {**_claim("sit", "2", week=1), "scored": True, "hit": False},
        {**_claim("add", "3", week=2, endpoint="trending"), "scored": True, "hit": True},
        {**_claim("start", "4", week=2), "scored": True, "hit": None},  # no bar
    ]

    summary = summarize(SEASON, scored, now="2026-09-17T12:00:00Z")

    assert summary["overall"] == {"scored": 3, "hits": 2, "hit_rate": 0.667}
    assert summary["by_endpoint"] == [
        {"key": "roster", "scored": 2, "hits": 1, "hit_rate": 0.5},
        {"key": "trending", "scored": 1, "hits": 1, "hit_rate": 1.0},
    ]
    assert [row["key"] for row in summary["by_kind"]] == ["add", "sit", "start"]
    assert summary["weeks"] == [1, 2]
    assert summary["unscorable"] == 1
    assert summary["season"] == SEASON and summary["updated_at"] == "2026-09-17T12:00:00Z"


def test_an_empty_summary_has_no_rate_rather_than_zero() -> None:
    summary = summarize(SEASON, [], now="2026-09-17T12:00:00Z")
    assert summary["overall"] == {"scored": 0, "hits": 0, "hit_rate": None}
    assert summary["weeks"] == [] and summary["by_endpoint"] == []


# --------------------------------------------------------------------------
# The task, end to end on the in-memory store
# --------------------------------------------------------------------------


#: Every team on the fixture schedule. Each week is A at B and C at D.
TEAMS = ("A", "B", "C", "D")


async def _cover(store: Store, week: int, teams: tuple[str, ...] = TEAMS) -> None:
    """One quarterback line per team: what a whole week's nflverse file carries.

    Quarterbacks, so the RB bar every claim here is scored against is unmoved.
    """
    for team in teams:
        await store.set(
            weekly_stats_collection(SEASON, week),
            f"qb-{team}",
            _line(f"qb-{team}", "QB", 15.0, team=team),
        )


async def _seed(store: Store) -> None:
    """Weeks 1 and 2 played and ingested; week 3 claimed but not yet played."""
    for week, (first, last) in KICKOFFS.items():
        await store.set(
            SCHEDULES_COLLECTION,
            f"{SEASON}_{week}",
            {
                "season": SEASON,
                "week": week,
                "first_game": first,
                "games": [
                    {"home": "A", "away": "B", "kickoff": first},
                    {"home": "C", "away": "D", "kickoff": last},
                ],
            },
        )
    for week, scores in ((1, (20.0, 5.0)), (2, (3.0, 18.0))):
        for player_id, points in zip(("1", "2"), scores, strict=True):
            await store.set(
                weekly_stats_collection(SEASON, week), player_id, _line(player_id, "RB", points)
            )
        await _cover(store, week)
    await _stamp_stats(store, STAMP_AFTER_WEEK_2)
    # Mapped players: each carries a gsis id, so a missing line is a scratch.
    for player_id in ("1", "2", "9"):
        await store.set(
            PLAYERS_COLLECTION, player_id, {"player_id": player_id, "gsis_id": f"00-{player_id}"}
        )
    claims = [
        _claim("start", "1", week=1),  # 20 vs bar 5 -> hit
        _claim("start", "2", week=1),  # 5 vs bar 5 -> hit (at the bar)
        _claim("sit", "1", week=2),  # 3 vs bar 3 -> miss
        _claim("matchup_top", "2", week=2, endpoint="matchup", group=["1", "2"]),  # 18 > 3 -> hit
        _claim("start", "9", week=2),  # scratch -> miss
        _claim("start", "1", week=3),  # no lines yet
    ]
    for doc in claims:
        assert await store.create(PREDICTIONS_COLLECTION, prediction_id(doc), doc)


async def test_the_task_scores_played_weeks_and_waits_on_the_rest(
    store: Store, settings: Settings
) -> None:
    await _seed(store)

    summary = await run_backtest(store, settings, now=AFTER_WEEK_2)

    assert summary["scored_this_run"] == 5
    assert summary["waiting_on_stats"] == [3]
    assert summary["overall"] == {"scored": 5, "hits": 3, "hit_rate": 0.6}
    assert summary["weeks"] == [1, 2]
    assert {row["key"]: row["hits"] for row in summary["by_endpoint"]} == {
        "matchup": 1,
        "roster": 2,
    }

    published = await store.get(BACKTEST_COLLECTION, str(SEASON))
    assert published is not None
    assert published["overall"] == summary["overall"]
    assert "scored_this_run" not in published

    pending = await store.get(PREDICTIONS_COLLECTION, "2026w3:roster:1")
    assert pending is not None and pending["scored"] is False
    scratch = await store.get(PREDICTIONS_COLLECTION, "2026w2:roster:9")
    assert scratch is not None
    assert scratch["scored"] is True and scratch["hit"] is False and scratch["points"] == 0.0
    assert scratch["scored_at"]


async def test_a_line_keyed_by_gsis_id_is_found_for_a_sleeper_id_claim(
    store: Store, settings: Settings
) -> None:
    """id_map had no Sleeper id when stats ran, so the line sits under the gsis id.

    Read as a scratch, the add would be a permanent miss and the fade a
    permanent hit.
    """
    await _seed(store)
    await store.set(PLAYERS_COLLECTION, "7", {"player_id": "7", "gsis_id": "00-0077777"})
    await store.set(
        weekly_stats_collection(SEASON, 1), "00-0077777", _line("00-0077777", "RB", 22.0)
    )
    for doc in (
        _claim("add", "7", endpoint="trending"),
        _claim("fade", "7", endpoint="sleepers"),
    ):
        assert await store.create(PREDICTIONS_COLLECTION, prediction_id(doc), doc)

    await run_backtest(store, settings, week=1, now=AFTER_WEEK_2)

    add = await store.get(PREDICTIONS_COLLECTION, "2026w1:trending:7")
    fade = await store.get(PREDICTIONS_COLLECTION, "2026w1:sleepers:7")
    assert add is not None and (add["hit"], add["points"]) == (True, 22.0)
    assert fade is not None and (fade["hit"], fade["points"]) == (False, 22.0)


async def test_an_unjoinable_scratch_is_unscorable_and_a_mapped_one_still_scores_zero(
    store: Store, settings: Settings
) -> None:
    await _seed(store)
    await store.set(PLAYERS_COLLECTION, "8", {"player_id": "8", "gsis_id": None})
    for doc in (
        _claim("sit", "8", week=2),  # no gsis id, no line
        _claim("sit", "6", week=2),  # no players/ doc at all
    ):
        assert await store.create(PREDICTIONS_COLLECTION, prediction_id(doc), doc)

    summary = await run_backtest(store, settings, now=AFTER_WEEK_2)

    for player_id in ("8", "6"):
        doc = await store.get(PREDICTIONS_COLLECTION, f"2026w2:roster:{player_id}")
        assert doc is not None
        assert (doc["scored"], doc["hit"], doc["points"]) == (True, None, None)
    # Player 9 has a gsis id and no line: he did not play, and still scores zero.
    scratch = await store.get(PREDICTIONS_COLLECTION, "2026w2:roster:9")
    assert scratch is not None and (scratch["hit"], scratch["points"]) == (False, 0.0)
    assert summary["overall"]["scored"] == 5 and summary["unscorable"] == 2


async def test_alias_leaves_real_lines_alone(store: Store) -> None:
    """A genuine 0-point line under the Sleeper id is never replaced or looked up."""
    await store.set(PLAYERS_COLLECTION, "1", {"player_id": "1", "gsis_id": "g1"})
    points = {"1": 0.0, "g1": 30.0}
    positions = {"1": "RB", "g1": "RB"}

    assert await alias_gsis_lines(store, {"1"}, points, positions) == set()
    assert points["1"] == 0.0


async def test_a_second_run_scores_nothing_twice(store: Store, settings: Settings) -> None:
    await _seed(store)
    first = await run_backtest(store, settings, now=AFTER_WEEK_2)

    second = await run_backtest(store, settings, now=AFTER_WEEK_2)

    assert second["scored_this_run"] == 0
    assert second["overall"] == first["overall"]


async def test_week_3_is_scored_once_its_lines_land(store: Store, settings: Settings) -> None:
    await _seed(store)
    await run_backtest(store, settings, now=AFTER_WEEK_2)
    # Player 1 does not play in week 3: with two other lines the bar is 8.0 and
    # a scratch is a miss, so the season's hits stay at three.
    await store.set(weekly_stats_collection(SEASON, 3), "2", _line("2", "RB", 12.0))
    await store.set(weekly_stats_collection(SEASON, 3), "3", _line("3", "RB", 8.0))
    await _cover(store, 3)
    await _stamp_stats(store, STAMP_AFTER_WEEK_3)

    summary = await run_backtest(store, settings, now=AFTER_WEEK_3)

    assert summary["scored_this_run"] == 1 and summary["waiting_on_stats"] == []
    assert summary["weeks"] == [1, 2, 3]
    assert summary["overall"]["scored"] == 6 and summary["overall"]["hits"] == 3


async def test_the_week_flag_limits_scoring_to_that_week(store: Store, settings: Settings) -> None:
    await _seed(store)

    summary = await run_backtest(store, settings, week=1, now=AFTER_WEEK_2)

    assert summary["scored_this_run"] == 2 and summary["weeks"] == [1]


# --------------------------------------------------------------------------
# A week is scored only once it is complete
# --------------------------------------------------------------------------


async def test_a_week_with_only_thursday_lines_waits(store: Store, settings: Settings) -> None:
    """Scoring is permanent, so Sunday's starters must not be scored as scratches."""
    await _seed(store)
    await store.set(weekly_stats_collection(SEASON, 3), "2", _line("2", "RB", 12.0))
    friday_of_week_3 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)

    summary = await run_backtest(store, settings, now=friday_of_week_3)

    assert 3 in summary["waiting_on_stats"]
    pending = await store.get(PREDICTIONS_COLLECTION, "2026w3:roster:1")
    assert pending is not None and pending["scored"] is False

    await _cover(store, 3)
    await _stamp_stats(store, STAMP_AFTER_WEEK_3)
    later = await run_backtest(store, settings, now=AFTER_WEEK_3)
    assert later["waiting_on_stats"] == []
    scored = await store.get(PREDICTIONS_COLLECTION, "2026w3:roster:1")
    assert scored is not None and scored["scored"] is True


async def test_a_played_week_waits_for_a_stats_run_that_finished_after_it(
    store: Store, settings: Settings
) -> None:
    """Lines present, week played, but the stats stamp predates the week's end.

    That is the shape of a backtest overlapping a running ``stats`` task: the
    collection is half-written and nothing marks it so. The marker is only
    stamped when the run finishes, so waiting on it is waiting on the whole
    snapshot.
    """
    await _seed(store)
    await store.set(weekly_stats_collection(SEASON, 3), "2", _line("2", "RB", 12.0))
    # No stamp after week 3: the run that is writing week 3 has not finished.

    summary = await run_backtest(store, settings, now=AFTER_WEEK_3)

    assert 3 in summary["waiting_on_stats"]
    pending = await store.get(PREDICTIONS_COLLECTION, "2026w3:roster:1")
    assert pending is not None and pending["scored"] is False

    await _cover(store, 3)
    await _stamp_stats(store, STAMP_AFTER_WEEK_3)
    later = await run_backtest(store, settings, now=AFTER_WEEK_3)
    assert later["waiting_on_stats"] == []


async def test_a_week_missing_monday_night_waits_though_the_stamp_is_fresh(
    store: Store, settings: Settings
) -> None:
    """A stats run that finished before nflverse published MNF still stamps the marker.

    Scoring then would grade every Monday-night player as a scratch forever, so
    the week waits until every scheduled team has a line.
    """
    await _seed(store)
    await store.set(weekly_stats_collection(SEASON, 3), "2", _line("2", "RB", 12.0))
    await _cover(store, 3, teams=("A", "B"))  # C at D is Monday night: not in the file yet
    await _stamp_stats(store, STAMP_AFTER_WEEK_3)

    summary = await run_backtest(store, settings, now=AFTER_WEEK_3)

    assert summary["waiting_on_stats"] == [3]
    assert await teams_missing_lines(
        store, SEASON, 3, await store.list(weekly_stats_collection(SEASON, 3))
    ) == ["C", "D"]
    pending = await store.get(PREDICTIONS_COLLECTION, "2026w3:roster:1")
    assert pending is not None and pending["scored"] is False

    await _cover(store, 3, teams=("C", "D"))
    later = await run_backtest(store, settings, now=AFTER_WEEK_3)
    assert later["waiting_on_stats"] == [] and later["scored_this_run"] == 1


async def test_bye_teams_are_not_asked_for_and_teamless_lines_vouch_for_nobody(
    store: Store,
) -> None:
    await _seed(store)
    lines = [_line("1", "RB", 5.0, team="a"), _line("2", "RB", 5.0, team="B")]
    lines += [_line("3", "WR", 5.0, team="C"), _line("4", "WR", 5.0, team="D")]
    # E is on bye (absent from the schedule) and has no lines: nothing is missing.
    assert await teams_missing_lines(store, SEASON, 1, lines) == []
    # A collection written without team fields cannot show coverage: wait.
    teamless = [_line("1", "RB", 5.0), _line("2", "RB", 5.0)]
    assert await teams_missing_lines(store, SEASON, 1, teamless) == list(TEAMS)


async def test_a_week_with_no_ingested_schedule_is_never_complete(store: Store) -> None:
    assert await week_complete(store, SEASON, 9, AFTER_WEEK_3) is False
    await store.set(SCHEDULES_COLLECTION, f"{SEASON}_9", {"games": [{"kickoff": "garbage"}]})
    assert await week_complete(store, SEASON, 9, AFTER_WEEK_3) is False


async def test_complete_means_the_last_kickoff_plus_the_grace_period(store: Store) -> None:
    await _seed(store)
    last = datetime(2026, 9, 22, 0, 15, tzinfo=UTC)  # week 2's Monday night
    assert await week_complete(store, SEASON, 2, last) is False
    # Played, and the seeded stamp (Tue 13:01Z) is after last kickoff + grace.
    assert await week_complete(store, SEASON, 2, last + WEEK_COMPLETE_GRACE) is True


async def test_complete_also_needs_a_stats_stamp_after_the_week(store: Store) -> None:
    await _seed(store)
    played = datetime(2026, 9, 22, 8, 15, tzinfo=UTC)
    await _stamp_stats(store, "2026-09-22T06:00:00Z")  # a run that finished mid-MNF
    assert await week_complete(store, SEASON, 2, played) is False
    await store.set(META_COLLECTION, FRESHNESS_DOC_ID, {}, merge=False)  # no marker at all
    assert await week_complete(store, SEASON, 2, played) is False
    await _stamp_stats(store, "2026-09-22T08:15:00Z")  # exactly at the line counts
    assert await week_complete(store, SEASON, 2, played) is True


async def test_the_task_is_registered_between_stats_and_precompute(
    store: Store, settings: Settings
) -> None:
    order = job.selected_tasks(job.ALL_TASKS)
    assert order.index("backtest") == order.index("stats") + 1
    assert order.index("backtest") < order.index("precompute")

    args = argparse.Namespace(season=None, week=None)
    results = await job.run_tasks(["backtest"], store, settings, args)

    assert results["backtest"]["status"] == "ok"
    assert results["backtest"]["result"]["overall"] == {"scored": 0, "hits": 0, "hit_rate": None}
