"""Tests for the nflverse ingest.

Fully offline: nflreadpy is never called. Fixtures are small polars frames using
the real nflverse column names (``player_id``/``gsis_id``, ``opponent_team``,
``pfr_player_id``/``offense_pct``, ``gameday``/``gametime``...), fed through
:class:`ingest.nflverse_ingest.Loaders`.

The most important test here is
:func:`test_ingest_writes_documents_the_read_models_can_read`: it writes through
the ingest path and reads everything back through :mod:`api.data.stats_store`
and :mod:`api.core.week`, which is the actual ingest<->analysis contract.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
import pytest

from api.core.clock import set_clock
from api.core.store import Store
from api.core.week import current_season, current_week
from api.data.stats_store import (
    FRESHNESS_DOC_ID,
    META_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    get_data_freshness,
    get_def_vs_pos,
    get_player,
    get_schedule,
    get_usage_trends,
    get_weekly_stats,
)
from ingest.nflverse_ingest import (
    DEPTH_CHARTS_COLLECTION,
    INJURIES_COLLECTION,
    Loaders,
    attach_rz_touches,
    attach_snap_pct,
    build_def_vs_pos,
    build_depth_chart_docs,
    build_gsis_to_position,
    build_injury_docs,
    build_pfr_to_gsis,
    build_red_zone_touches,
    build_schedule_docs,
    build_usage_trends,
    default_loaders,
    ingest_nflverse,
    ingest_schedule,
    resolve_season,
    transform_weekly_stats,
    write_injuries,
)
from ingest.sleeper_players import ID_MAP_COLLECTION

SEASON = 2026

MAHOMES = "00-0033873"
JEFFERSON = "00-0036322"
UNMAPPED = "00-0099999"


# --- fixtures -------------------------------------------------------------


def _qb_week(week: int, opponent: str, ppr: float) -> dict[str, Any]:
    return {
        "player_id": MAHOMES,
        "player_display_name": "Patrick Mahomes",
        "position": "QB",
        "season": SEASON,
        "week": week,
        "team": "KC",
        "opponent_team": opponent,
        "completions": 25.0,
        "attempts": 35.0,
        "passing_yards": 280.0,
        "passing_tds": 2.0,
        "interceptions": 0.0,
        "carries": 3.0,
        "rushing_yards": 12.0,
        "rushing_tds": 0.0,
        "targets": 0.0,
        "receptions": 0.0,
        "receiving_yards": 0.0,
        "receiving_tds": 0.0,
        "target_share": 0.0,
        "fantasy_points": ppr,
        "fantasy_points_ppr": ppr,
    }


def _wr_week(week: int, opponent: str, ppr: float, target_share: float) -> dict[str, Any]:
    return {
        "player_id": JEFFERSON,
        "player_display_name": "Justin Jefferson",
        "position": "WR",
        "season": SEASON,
        "week": week,
        "team": "MIN",
        "opponent_team": opponent,
        "completions": 0.0,
        "attempts": 0.0,
        "passing_yards": 0.0,
        "passing_tds": 0.0,
        "interceptions": 0.0,
        "carries": 0.0,
        "rushing_yards": 0.0,
        "rushing_tds": 0.0,
        "targets": 10.0,
        "receptions": 7.0,
        "receiving_yards": 95.0,
        "receiving_tds": 1.0,
        "target_share": target_share,
        "fantasy_points": ppr - 7.0,
        "fantasy_points_ppr": ppr,
    }


def stats_frame() -> pl.DataFrame:
    """Four weeks of weekly stats plus one player with no Sleeper id.

    Includes a prior-season row that must be filtered out.
    """
    rows = [
        _qb_week(1, "ATL", 20.0),
        _qb_week(2, "DEN", 10.0),
        _qb_week(3, "ATL", 30.0),
        _qb_week(4, "DEN", 20.0),
        _wr_week(1, "ATL", 10.0, 0.10),
        _wr_week(2, "ATL", 20.0, 0.20),
        _wr_week(3, "DEN", 15.0, 0.30),
        _wr_week(4, "DEN", 25.0, 0.40),
        {
            **_wr_week(4, "KC", 12.0, 0.15),
            "player_id": UNMAPPED,
            "player_display_name": "Bench Warmer",
            "position": "RB",
            "team": "LV",
        },
        {**_qb_week(1, "ATL", 99.0), "season": SEASON - 1},  # wrong season: dropped
    ]
    return pl.DataFrame(rows)


def snaps_frame() -> pl.DataFrame:
    """Snap counts keyed on ``pfr_player_id``, as PFR publishes them."""
    rows = [
        {"pfr_player_id": "MahoPa00", "season": SEASON, "week": w, "offense_pct": 0.98}
        for w in (1, 2, 3, 4)
    ]
    rows += [
        {"pfr_player_id": "JeffJu00", "season": SEASON, "week": w, "offense_pct": pct}
        for w, pct in ((1, 0.50), (2, 0.60), (3, 0.90), (4, 0.90))
    ]
    # A snap row whose pfr id is unknown to load_players(): counted, not fatal.
    rows.append({"pfr_player_id": "NoboOd00", "season": SEASON, "week": 4, "offense_pct": 0.30})
    return pl.DataFrame(rows)


def players_frame() -> pl.DataFrame:
    """``load_players()``: the gsis <-> pfr bridge the snap join needs."""
    return pl.DataFrame(
        [
            {
                "gsis_id": MAHOMES,
                "pfr_id": "MahoPa00",
                "display_name": "Patrick Mahomes",
                "position": "QB",
            },
            {
                "gsis_id": JEFFERSON,
                "pfr_id": "JeffJu00",
                "display_name": "Justin Jefferson",
                "position": "WR",
            },
            {
                "gsis_id": UNMAPPED,
                "pfr_id": None,
                "display_name": "Bench Warmer",
                "position": "RB",
            },
        ]
    )


def ff_playerids_frame() -> pl.DataFrame:
    """nflverse's cross-reference table: the sleeper_id <-> gsis_id bridge.

    ``sleeper_id`` really is typed as an integer here and as a string
    everywhere else, which is the join's one sharp edge.
    """
    return pl.DataFrame(
        [
            {"name": "Patrick Mahomes", "sleeper_id": 4046, "gsis_id": MAHOMES},
            {"name": "Justin Jefferson", "sleeper_id": 6794, "gsis_id": JEFFERSON},
            {"name": "No Sleeper Id", "sleeper_id": None, "gsis_id": "00-0099999"},
            {"name": "No Gsis", "sleeper_id": 1234, "gsis_id": None},
        ]
    )


def schedules_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "game_id": "2026_01_ATL_KC",
                "season": SEASON,
                "game_type": "REG",
                "week": 1,
                "gameday": "2026-09-10",
                "gametime": "20:20",
                "away_team": "ATL",
                "home_team": "KC",
                "stadium": "GEHA Field at Arrowhead Stadium",
            },
            {
                "game_id": "2026_01_MIN_DEN",
                "season": SEASON,
                "game_type": "REG",
                "week": 1,
                "gameday": "2026-09-13",
                "gametime": "13:00",
                "away_team": "MIN",
                "home_team": "DEN",
                "stadium": "Empower Field",
            },
            {
                "game_id": "2026_02_KC_MIN",
                "season": SEASON,
                "game_type": "REG",
                "week": 2,
                "gameday": "2026-09-17",
                "gametime": "20:15",
                "away_team": "KC",
                "home_team": "MIN",
                "stadium": "U.S. Bank Stadium",
            },
            {
                "game_id": "2026_22_KC_ATL",
                "season": SEASON,
                "game_type": "SB",
                "week": 22,
                "gameday": "2027-02-14",
                "gametime": "18:30",
                "away_team": "KC",
                "home_team": "ATL",
                "stadium": "Neutral Site",
            },
            {
                "game_id": "2025_01_KC_ATL",
                "season": SEASON - 1,
                "game_type": "REG",
                "week": 1,
                "gameday": "2025-09-07",
                "gametime": "13:00",
                "away_team": "KC",
                "home_team": "ATL",
                "stadium": "Old Season",
            },
        ]
    )


def injuries_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "season": SEASON,
                "week": 3,
                "gsis_id": JEFFERSON,
                "team": "MIN",
                "position": "WR",
                "full_name": "Justin Jefferson",
                "report_status": "Out",
                "report_primary_injury": "Hamstring",
                "practice_status": "Did Not Participate",
                "date_modified": "2026-09-24T18:00:00Z",
            },
            {
                "season": SEASON,
                "week": 4,
                "gsis_id": JEFFERSON,
                "team": "MIN",
                "position": "WR",
                "full_name": "Justin Jefferson",
                "report_status": "Questionable",
                "report_primary_injury": "Hamstring",
                "practice_status": "Limited Participation",
                "date_modified": "2026-10-01T18:00:00Z",
            },
            {
                "season": SEASON,
                "week": 4,
                "gsis_id": UNMAPPED,
                "team": "LV",
                "position": "RB",
                "full_name": "Bench Warmer",
                "report_status": "Doubtful",
                "report_primary_injury": "Knee",
                "practice_status": "Did Not Participate",
                "date_modified": "2026-10-01T18:00:00Z",
            },
        ]
    )


def depth_charts_frame() -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "season": SEASON,
                "week": 3,
                "club_code": "MIN",
                "gsis_id": JEFFERSON,
                "full_name": "Justin Jefferson",
                "position": "WR",
                "depth_team": 1,
            },
            {
                "season": SEASON,
                "week": 4,
                "club_code": "MIN",
                "gsis_id": JEFFERSON,
                "full_name": "Justin Jefferson",
                "position": "WR",
                "depth_team": 1,
            },
            {
                "season": SEASON,
                "week": 4,
                "club_code": "MIN",
                "gsis_id": "00-0088888",
                "full_name": "Backup Receiver",
                "position": "WR",
                "depth_team": 2,
            },
            {
                "season": SEASON,
                "week": 4,
                "club_code": "KC",
                "gsis_id": MAHOMES,
                "full_name": "Patrick Mahomes",
                "position": "QB",
                "depth_team": 1,
            },
        ]
    )


def pbp_frame() -> pl.DataFrame:
    """Play-by-play, trimmed to the columns the red-zone counter uses.

    Two red-zone carries for the WR fixture in week 4, one red-zone target, plus
    plays outside the 20 and a null receiver (incompletion) that must not count.
    """
    return pl.DataFrame(
        [
            {
                "season": SEASON,
                "week": 4,
                "yardline_100": 8,
                "rush_attempt": 0,
                "pass_attempt": 1,
                "rusher_player_id": None,
                "receiver_player_id": JEFFERSON,
            },
            {
                "season": SEASON,
                "week": 4,
                "yardline_100": 3,
                "rush_attempt": 1,
                "pass_attempt": 0,
                "rusher_player_id": JEFFERSON,
                "receiver_player_id": None,
            },
            {
                "season": SEASON,
                "week": 4,
                "yardline_100": 12,
                "rush_attempt": 1,
                "pass_attempt": 0,
                "rusher_player_id": JEFFERSON,
                "receiver_player_id": None,
            },
            {  # outside the red zone
                "season": SEASON,
                "week": 4,
                "yardline_100": 55,
                "rush_attempt": 1,
                "pass_attempt": 0,
                "rusher_player_id": JEFFERSON,
                "receiver_player_id": None,
            },
            {  # incompletion: no receiver credited
                "season": SEASON,
                "week": 4,
                "yardline_100": 5,
                "rush_attempt": 0,
                "pass_attempt": 1,
                "rusher_player_id": None,
                "receiver_player_id": None,
            },
            {  # prior season
                "season": SEASON - 1,
                "week": 4,
                "yardline_100": 5,
                "rush_attempt": 1,
                "pass_attempt": 0,
                "rusher_player_id": JEFFERSON,
                "receiver_player_id": None,
            },
        ]
    )


def snapshot_depth_charts_frame() -> pl.DataFrame:
    """The 2025+ nflverse depth chart: ``dt`` snapshots, no season/week columns."""
    return pl.DataFrame(
        [
            {
                "dt": "2026-03-01T07:00:00Z",
                "team": "KC",
                "player_name": "Old Snapshot QB",
                "gsis_id": "00-0077777",
                "pos_abb": "QB",
                "pos_rank": 1,
            },
            {
                "dt": "2026-03-14T07:32:09Z",
                "team": "KC",
                "player_name": "Patrick Mahomes",
                "gsis_id": MAHOMES,
                "pos_abb": "QB",
                "pos_rank": 1,
            },
            {
                "dt": "2026-03-14T07:32:09Z",
                "team": "KC",
                "player_name": "Backup QB",
                "gsis_id": "00-0066666",
                "pos_abb": "QB",
                "pos_rank": 2,
            },
            {
                "dt": "2026-03-14T07:32:09Z",
                "team": "KC",
                "player_name": "Josh Sweat",
                "gsis_id": "00-0055555",
                "pos_abb": "LDE",  # defensive slot: not stored
                "pos_rank": 1,
            },
        ]
    )


def fake_loaders(**overrides: Any) -> Loaders:
    """Loaders over the fixture frames; ``overrides`` swaps individual loaders."""
    calls: dict[str, Any] = {}

    def _record(name: str, frame: pl.DataFrame):
        def loader(seasons):
            calls[name] = list(seasons)
            return frame

        return loader

    loaders = Loaders(
        player_stats=_record("player_stats", stats_frame()),
        snap_counts=_record("snap_counts", snaps_frame()),
        depth_charts=_record("depth_charts", depth_charts_frame()),
        injuries=_record("injuries", injuries_frame()),
        schedules=_record("schedules", schedules_frame()),
        players=players_frame,
        pbp=_record("pbp", pbp_frame()),
        ff_playerids=ff_playerids_frame,
    )
    if overrides:
        loaders = Loaders(**{**loaders.__dict__, **overrides})
    return loaders


async def seed_id_map(store: Store) -> None:
    """Pretend the nightly Sleeper task already ran (Mahomes + Jefferson only)."""
    await store.set(
        ID_MAP_COLLECTION,
        MAHOMES,
        {"gsis_id": MAHOMES, "sleeper_id": "4046", "espn_id": "3139477", "name": "Patrick Mahomes"},
    )
    await store.set(
        ID_MAP_COLLECTION,
        JEFFERSON,
        {"gsis_id": JEFFERSON, "sleeper_id": "6794", "espn_id": None, "name": "Justin Jefferson"},
    )
    await store.set("players", "4046", {"player_id": "4046", "name": "Patrick Mahomes"})
    await store.set("players", "6794", {"player_id": "6794", "name": "Justin Jefferson"})


# --- transforms -----------------------------------------------------------


def test_transform_weekly_stats_normalizes_columns_and_filters_seasons() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)

    assert len(rows) == 9  # the 2025 row is dropped
    row = next(r for r in rows if r["gsis_id"] == MAHOMES and r["week"] == 1)
    assert row["season"] == SEASON
    assert row["team"] == "KC"
    assert row["opponent"] == "ATL"  # opponent_team -> opponent
    assert row["position"] == "QB"
    assert row["name"] == "Patrick Mahomes"
    assert row["passing_yards"] == 280.0
    assert row["fantasy_points_ppr"] == 20.0
    assert row["snap_pct"] is None  # joined separately
    assert row["rz_touches"] == 0  # no red-zone columns in the base stats table


def test_transform_weekly_stats_drops_postseason_rows() -> None:
    """Playoff weeks are not regular-season weeks: no week 19+, no playoff usage."""
    frame = pl.DataFrame(
        [
            {**_qb_week(18, "DEN", 20.0), "season_type": "REG"},
            {**_qb_week(19, "BUF", 30.0), "season_type": "POST"},
            {**_qb_week(22, "PHI", 40.0), "season_type": "post"},
            {**_qb_week(17, "LV", 10.0), "season_type": None},  # unlabeled: kept
        ]
    )

    rows = transform_weekly_stats(frame, season=SEASON)

    assert sorted(r["week"] for r in rows) == [17, 18]


def test_transform_weekly_stats_requires_an_id_column() -> None:
    frame = pl.DataFrame([{"week": 1, "team": "KC"}])

    with pytest.raises(ValueError, match="player id column"):
        transform_weekly_stats(frame, season=SEASON)


def test_transform_weekly_stats_picks_up_red_zone_columns_when_present() -> None:
    frame = pl.DataFrame(
        [
            {
                "player_id": JEFFERSON,
                "season": SEASON,
                "week": 1,
                "team": "MIN",
                "opponent_team": "ATL",
                "position": "WR",
                "rushing_red_zone_carries": 1.0,
                "receiving_red_zone_targets": 2.0,
            }
        ]
    )

    rows = transform_weekly_stats(frame, season=SEASON)

    assert rows[0]["rz_touches"] == 3.0


def test_snap_counts_join_through_pfr_ids() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)
    mapping = build_pfr_to_gsis(players_frame())

    counts = attach_snap_pct(rows, snaps_frame(), mapping)

    assert mapping == {"MahoPa00": MAHOMES, "JeffJu00": JEFFERSON}
    assert counts["joined"] == 8  # 4 weeks x 2 joinable players
    assert counts["unjoinable"] == 1  # the unknown pfr id
    jefferson = sorted((r for r in rows if r["gsis_id"] == JEFFERSON), key=lambda r: r["week"])
    assert [r["snap_pct"] for r in jefferson] == [0.5, 0.6, 0.9, 0.9]
    assert next(r for r in rows if r["gsis_id"] == UNMAPPED)["snap_pct"] is None


def test_snap_join_degrades_when_the_players_bridge_is_missing() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)

    counts = attach_snap_pct(rows, snaps_frame(), build_pfr_to_gsis(pl.DataFrame()))

    assert counts["joined"] == 0
    assert all(r["snap_pct"] is None for r in rows)


def test_snap_join_degrades_when_columns_are_renamed_upstream() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)
    weird = pl.DataFrame([{"some_id": "x", "week": 1, "pct": 0.5}])

    counts = attach_snap_pct(rows, weird, build_pfr_to_gsis(players_frame()))

    assert counts == {"joined": 0, "unjoinable": 0, "rows": len(rows)}


def test_positions_are_backfilled_from_the_players_table() -> None:
    assert build_gsis_to_position(players_frame())[JEFFERSON] == "WR"


# --- derived tables -------------------------------------------------------


def test_usage_trend_deltas_are_l2w_minus_prior_2w() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)
    attach_snap_pct(rows, snaps_frame(), build_pfr_to_gsis(players_frame()))
    for row in rows:
        row["player_id"] = row["gsis_id"]

    trends = {t["player_id"]: t for t in build_usage_trends(rows, season=SEASON)}

    wr = trends[JEFFERSON]
    assert wr["through_week"] == 4
    assert wr["weeks_counted"] == 4
    assert wr["last_week_played"] == 4
    # snaps 0.5, 0.6, 0.9, 0.9 -> mean 0.725, delta (0.9+0.9)/2 - (0.5+0.6)/2 = 0.35
    assert wr["snap_pct_l4w"] == 0.725
    assert wr["snap_pct_delta"] == 0.35
    # target share 0.10, 0.20, 0.30, 0.40 -> mean 0.25, delta 0.35 - 0.15 = 0.20
    assert wr["target_share_l4w"] == 0.25
    assert wr["target_share_delta"] == 0.2
    assert wr["trend"] == "rising"

    qb = trends[MAHOMES]
    assert qb["snap_pct_l4w"] == 0.98
    assert qb["snap_pct_delta"] == 0.0
    assert qb["trend"] == "flat"


def test_usage_trend_declining_and_short_history() -> None:
    rows = [
        {
            "player_id": "p1",
            "gsis_id": "g1",
            "week": w,
            "snap_pct": pct,
            "target_share": None,
            "rz_touches": 1,
        }
        for w, pct in ((1, 0.90), (2, 0.90), (3, 0.40), (4, 0.30))
    ]
    rows.append(
        {
            "player_id": "p2",
            "gsis_id": "g2",
            "week": 1,
            "snap_pct": 0.5,
            "target_share": 0.2,
            "rz_touches": 0,
        }
    )

    trends = {t["player_id"]: t for t in build_usage_trends(rows, season=SEASON)}

    assert trends["p1"]["snap_pct_delta"] == -0.55
    assert trends["p1"]["trend"] == "declining"
    assert trends["p1"]["rz_touches_l4w"] == 4
    # one game played: neither half of the delta window is complete
    assert trends["p2"]["snap_pct_delta"] is None
    assert trends["p2"]["target_share_delta"] is None
    assert trends["p2"]["trend"] == "flat"
    assert trends["p2"]["weeks_counted"] == 1
    # a rollup this stale must be readable as stale by whoever cites it
    assert trends["p2"]["last_week_played"] == 1


def test_usage_trends_respect_through_week() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)
    for row in rows:
        row["player_id"] = row["gsis_id"]

    trends = {t["player_id"]: t for t in build_usage_trends(rows, season=SEASON, through_week=2)}

    assert trends[JEFFERSON]["through_week"] == 2
    assert trends[JEFFERSON]["weeks_counted"] == 2
    assert trends[JEFFERSON]["last_week_played"] == 2
    assert trends[JEFFERSON]["target_share_l4w"] == 0.15


def test_def_vs_pos_aggregates_points_allowed_per_game_and_ranks() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)
    for row in rows:
        row["player_id"] = row["gsis_id"]

    docs = {d["team"]: d for d in build_def_vs_pos(rows, season=SEASON)}

    # ATL was the opponent in weeks 1, 2, 3 -> 3 games.
    atl = docs["ATL"]
    assert atl["through_week"] == 4
    assert atl["positions"]["QB"] == {
        "points_allowed_per_game": 16.67,  # (20 + 30) / 3
        "rank": 1,  # most generous to QBs
        "points_allowed_total": 50.0,
        "games": 3,
    }
    assert atl["positions"]["WR"]["points_allowed_per_game"] == 10.0  # (10 + 20) / 3
    assert atl["positions"]["WR"]["rank"] == 2

    den = docs["DEN"]
    assert den["positions"]["QB"]["points_allowed_per_game"] == 10.0  # (10 + 20) / 3
    assert den["positions"]["QB"]["rank"] == 2
    assert den["positions"]["WR"]["rank"] == 1  # (15 + 25) / 3 = 13.33

    # The unmapped RB still contributes to the defense he played against.
    assert docs["KC"]["positions"]["RB"]["points_allowed_per_game"] == 12.0


def test_def_vs_pos_is_empty_without_opponents() -> None:
    assert build_def_vs_pos([{"week": 1, "position": "WR"}], season=SEASON) == []


def test_schedule_docs_and_week_map() -> None:
    docs, week_map = build_schedule_docs(schedules_frame(), season=SEASON)

    ids = [doc_id for doc_id, _ in docs]
    assert ids == [f"{SEASON}_1", f"{SEASON}_2", f"{SEASON}_22"]  # prior season filtered out

    week1 = dict(docs)[f"{SEASON}_1"]
    assert week1["season"] == SEASON
    assert week1["week"] == 1
    # 20:20 ET on Sept 10 is 00:20Z on Sept 11
    assert week1["first_game"] == "2026-09-11T00:20:00Z"
    assert week1["games"][0] == {
        "home": "KC",
        "away": "ATL",
        "kickoff": "2026-09-11T00:20:00Z",
        "venue": "GEHA Field at Arrowhead Stadium",
        "game_id": "2026_01_ATL_KC",
        "game_type": "REG",
    }
    assert week1["games"][1]["kickoff"] == "2026-09-13T17:00:00Z"  # 13:00 ET -> 17:00Z

    # meta/schedule_weeks drives week resolution: regular season only.
    assert week_map == {"1": "2026-09-11T00:20:00Z", "2": "2026-09-18T00:15:00Z"}


def test_schedule_docs_tolerate_a_missing_gametime() -> None:
    frame = pl.DataFrame(
        [
            {
                "game_id": "x",
                "season": SEASON,
                "week": 5,
                "gameday": "2026-10-08",
                "gametime": None,
                "away_team": "KC",
                "home_team": "ATL",
            }
        ]
    )

    docs, week_map = build_schedule_docs(frame, season=SEASON)

    assert docs[0][1]["first_game"] == "2026-10-08T04:00:00Z"  # midnight ET
    assert week_map == {"5": "2026-10-08T04:00:00Z"}


def test_injury_docs_keep_only_the_latest_week_per_player() -> None:
    frame = pl.concat(
        [
            injuries_frame(),
            # Out in week 2, absent from every report since: healthy, not Out.
            pl.DataFrame(
                [
                    {
                        "season": SEASON,
                        "week": 2,
                        "gsis_id": MAHOMES,
                        "team": "KC",
                        "position": "QB",
                        "full_name": "Patrick Mahomes",
                        "report_status": "Out",
                        "report_primary_injury": "Ankle",
                        "practice_status": "Did Not Participate",
                        "date_modified": "2026-09-17T18:00:00Z",
                    }
                ]
            ),
        ]
    )

    docs = {d["gsis_id"]: d for d in build_injury_docs(frame, season=SEASON)}

    assert MAHOMES not in docs  # a stale week-2 "Out" is not carried forward
    assert set(docs) == {JEFFERSON, UNMAPPED}
    assert docs[JEFFERSON]["week"] == 4
    assert docs[JEFFERSON]["injury_status"] == "Questionable"
    assert docs[JEFFERSON]["injury_detail"] == "Hamstring"
    assert docs[JEFFERSON]["practice_status"] == "Limited Participation"
    assert docs[JEFFERSON]["updated_at"] == "2026-10-01T18:00:00Z"


def test_red_zone_touches_are_counted_from_play_by_play() -> None:
    counts = build_red_zone_touches(pbp_frame(), season=SEASON)

    # 2 carries inside the 20 + 1 target; the 55-yard-line carry, the null
    # receiver and the prior season are all excluded.
    assert counts == {(JEFFERSON, 4): 3}


def test_red_zone_touches_degrade_on_an_unexpected_frame() -> None:
    assert build_red_zone_touches(pl.DataFrame(), season=SEASON) == {}
    assert build_red_zone_touches(pl.DataFrame([{"foo": 1}]), season=SEASON) == {}


def test_attach_rz_touches_leaves_quiet_weeks_at_zero() -> None:
    rows = transform_weekly_stats(stats_frame(), season=SEASON)

    updated = attach_rz_touches(rows, build_red_zone_touches(pbp_frame(), season=SEASON))

    assert updated == 1
    week4 = next(r for r in rows if r["gsis_id"] == JEFFERSON and r["week"] == 4)
    week3 = next(r for r in rows if r["gsis_id"] == JEFFERSON and r["week"] == 3)
    assert week4["rz_touches"] == 3
    assert week3["rz_touches"] == 0


def test_depth_charts_use_the_latest_snapshot_and_skip_non_skill_slots() -> None:
    docs = build_depth_chart_docs(snapshot_depth_charts_frame(), season=SEASON)

    assert len(docs) == 1
    kc = docs[0]
    assert kc["team"] == "KC"
    assert kc["updated_at"] == "2026-03-14T07:32:09Z"
    assert kc["week"] is None  # the current upstream shape has no week column
    assert set(kc["positions"]) == {"QB"}  # LDE dropped
    assert [p["name"] for p in kc["positions"]["QB"]] == ["Patrick Mahomes", "Backup QB"]


def test_depth_chart_docs_use_the_latest_week_and_sort_by_rank() -> None:
    docs = {d["team"]: d for d in build_depth_chart_docs(depth_charts_frame(), season=SEASON)}

    assert docs["MIN"]["week"] == 4
    receivers = docs["MIN"]["positions"]["WR"]
    assert [r["rank"] for r in receivers] == [1, 2]
    assert receivers[0]["name"] == "Justin Jefferson"
    assert set(docs) == {"KC", "MIN"}


# --- orchestration --------------------------------------------------------


async def test_ingest_writes_documents_the_read_models_can_read(store: Store) -> None:
    """The contract test: ingest writes, ``api.data.stats_store`` reads."""
    await seed_id_map(store)

    summary = await ingest_nflverse(store, loaders=fake_loaders(), season=SEASON)

    assert summary["season"] == SEASON
    assert summary["through_week"] == 4
    assert summary["ids"] == {"mapped": 8, "unmapped": 1}

    # weekly_stats/{season}_{week}/players/{sleeper_id}
    lines = await get_weekly_stats(store, "6794", SEASON, [1, 2, 3, 4, 5])
    assert [line["week"] for line in lines] == [1, 2, 3, 4]
    assert lines[0]["player_id"] == "6794"
    assert lines[0]["gsis_id"] == JEFFERSON
    assert lines[0]["opponent"] == "ATL"
    assert lines[0]["snap_pct"] == 0.5
    assert lines[3]["fantasy_points_ppr"] == 25.0
    assert lines[3]["rz_touches"] == 3  # from play-by-play
    assert lines[0]["rz_touches"] == 0

    # usage_trends/{sleeper_id}
    trend = await get_usage_trends(store, "6794")
    assert trend["snap_pct_l4w"] == 0.725
    assert trend["target_share_delta"] == 0.2
    assert trend["trend"] == "rising"
    assert trend["through_week"] == 4
    assert trend["rz_touches_l4w"] == 3

    # def_vs_pos/{team}
    split = await get_def_vs_pos(store, "atl", "qb")
    assert split == {
        "team": "ATL",
        "position": "QB",
        "season": SEASON,
        "through_week": 4,
        "points_allowed_per_game": 16.67,
        "rank": 1,
        "points_allowed_total": 50.0,
        "games": 3,
    }
    assert await get_def_vs_pos(store, "ATL", "TE") is None

    # schedules/{season}_{week}
    game = await get_schedule(store, "kc", 1, SEASON)
    assert game == {
        "season": SEASON,
        "week": 1,
        "team": "KC",
        "opponent": "ATL",
        "home": True,
        "kickoff": "2026-09-11T00:20:00Z",
    }
    assert await get_schedule(store, "SEA", 1, SEASON) is None  # bye / not scheduled

    # meta/schedule_weeks drives api.core.week
    assert await current_season(store) == SEASON
    assert await current_week(store, now=datetime(2026, 9, 15, 12, tzinfo=UTC)) == 2

    # injury status is merged onto the players document the read model documents
    player = await get_player(store, "6794")
    assert player["injury_status"] == "Questionable"
    assert player["name"] == "Justin Jefferson"  # merge, not replace
    assert (await store.get(INJURIES_COLLECTION, "6794"))["injury_detail"] == "Hamstring"
    assert await store.get(DEPTH_CHARTS_COLLECTION, "MIN") is not None

    freshness = await get_data_freshness(store)
    assert set(freshness) == {
        "weekly_stats",
        "usage_trends",
        "def_vs_pos",
        "schedules",
        "injuries",
        "depth_charts",
    }


async def test_schedule_only_advances_the_season_without_loading_stats(store: Store) -> None:
    """Preseason schedules arrive before weekly stats; rollover must still work."""

    def stats_must_not_load(_: Any) -> pl.DataFrame:
        raise AssertionError("schedule-only ingest touched player stats")

    summary = await ingest_schedule(
        store,
        loaders=fake_loaders(player_stats=stats_must_not_load),
        season=SEASON,
    )

    assert summary == {
        "season": SEASON,
        "schedules": 3,
        "schedule_weeks": 2,
        "first_kickoff": "2026-09-11T00:20:00Z",
    }
    assert await current_season(store) == SEASON
    assert await get_schedule(store, "KC", 1, SEASON) is not None
    assert set(await get_data_freshness(store)) == {"schedules"}


async def test_a_null_nflverse_status_never_blanks_sleepers(store: Store) -> None:
    await seed_id_map(store)
    await store.set(
        "players",
        "6794",
        {"player_id": "6794", "name": "Justin Jefferson", "injury_status": "Questionable"},
    )
    frame = injuries_frame().with_columns(pl.lit(None, dtype=pl.String).alias("report_status"))

    written = await write_injuries(store, build_injury_docs(frame, season=SEASON))

    assert written == 2
    player = await get_player(store, "6794")
    assert player["injury_status"] == "Questionable"  # Sleeper's value survives
    assert player["practice_status"] == "Limited Participation"  # non-null still merges


async def test_last_weeks_report_is_not_merged_onto_players_after_the_rollover(
    store: Store,
) -> None:
    """Tuesday's run sees week 4's game statuses while week 5 is current.

    Merging them would stamp last Sunday's "Out" onto ``players/`` until the
    nightly sync; the report is still filed under ``injuries/``.
    """
    await seed_id_map(store)
    docs = build_injury_docs(injuries_frame(), season=SEASON)  # report week 4

    written = await write_injuries(store, docs, current_week=5)

    assert written == 2
    assert (await get_player(store, "6794")).get("injury_status") is None
    stored = await store.get(INJURIES_COLLECTION, "6794")
    assert stored["week"] == 4 and stored["injury_status"] == "Questionable"

    # The same report during its own week merges as before.
    await write_injuries(store, docs, current_week=4)
    assert (await get_player(store, "6794"))["injury_status"] == "Questionable"


async def test_the_ingest_resolves_the_current_week_before_merging_injuries(
    store: Store,
) -> None:
    """The week comes from the schedule the run just wrote, on the clock seam."""
    await seed_id_map(store)
    later = injuries_frame().with_columns(pl.lit(1).alias("week"))  # a week-1 report
    set_clock(lambda: datetime(2026, 9, 16, 13, 1, tzinfo=UTC))  # Tuesday of week 2
    try:
        await ingest_nflverse(store, loaders=fake_loaders(injuries=lambda _: later), season=SEASON)
    finally:
        set_clock(None)

    assert (await get_player(store, "6794")).get("injury_status") is None
    assert (await store.get(INJURIES_COLLECTION, "6794"))["week"] == 1


async def test_unmapped_players_fall_back_to_the_gsis_id(store: Store) -> None:
    await seed_id_map(store)

    await ingest_nflverse(store, loaders=fake_loaders(), season=SEASON)

    assert await get_weekly_stats(store, UNMAPPED, SEASON, [4]) != []
    orphan = await get_usage_trends(store, UNMAPPED)
    assert orphan["gsis_id"] == UNMAPPED
    # the injury row for that player lands under the gsis id, and no phantom
    # players/ document is created for him
    assert await store.get(INJURIES_COLLECTION, UNMAPPED) is not None
    assert await get_player(store, UNMAPPED) is None


async def test_ingest_without_an_id_map_keys_everything_by_gsis_id(store: Store) -> None:
    summary = await ingest_nflverse(store, loaders=fake_loaders(), season=SEASON)

    assert summary["ids"] == {"mapped": 0, "unmapped": 9}
    assert await get_weekly_stats(store, MAHOMES, SEASON, [1]) != []


async def test_supplemental_dataset_failures_do_not_fail_the_run(store: Store) -> None:
    def boom(seasons):
        raise RuntimeError("nflverse 500")

    await seed_id_map(store)
    loaders = fake_loaders(injuries=boom, depth_charts=boom, snap_counts=boom)

    summary = await ingest_nflverse(store, loaders=loaders, season=SEASON)

    assert summary["injuries"] == 0
    assert summary["depth_charts"] == 0
    assert summary["snaps"]["joined"] == 0
    assert summary["weekly_stats"] == {1: 2, 2: 2, 3: 2, 4: 3}
    trend = await get_usage_trends(store, "6794")
    assert trend["snap_pct_l4w"] is None  # degraded, still written
    assert trend["target_share_delta"] == 0.2
    assert set(await get_data_freshness(store)) == {
        "weekly_stats",
        "usage_trends",
        "def_vs_pos",
        "schedules",
    }


def first_reg_kickoff(frame: pl.DataFrame) -> date:
    """The date ``ingest_schedule`` would report as ``first_kickoff``.

    Mirrors :func:`ingest.nflverse_ingest.build_schedule_docs`: current season,
    regular season only. Anchoring the fixtures on the same rows the code reads
    is the point — the previous two breakages both came from a fixture that
    moved a date the kickoff calculation was not looking at.
    """
    current = frame.filter((pl.col("season") == SEASON) & (pl.col("game_type") == "REG"))
    return current["gameday"].str.to_date().min()


def started_schedules_frame() -> pl.DataFrame:
    """The fixture schedule, moved into the past so the season has kicked off."""
    return schedules_frame().with_columns(
        pl.when(pl.col("gameday") == "2026-09-10")
        .then(pl.lit("2026-08-01"))
        .otherwise(pl.col("gameday"))
        .alias("gameday")
    )


def unstarted_schedules_frame() -> pl.DataFrame:
    """The fixture schedule slid forward so the whole season is still ahead.

    ``season_has_kicked_off`` compares the **earliest** gameday against
    ``datetime.now()``, so every date has to move, not just the opener. This has
    now gone red twice for the same reason:

    * 2026-09-10 — the literal opener passed and the preseason tests asserted
      the opposite of what the code correctly did.
    * 2026-09-17 — ``8fe7a06`` computed the opener's replacement date instead of
      bumping it, but still matched the single literal ``"2026-09-10"``. That
      left ``2026-09-13`` and ``2026-09-17`` in place, so the earliest kickoff
      became 2026-09-13 and the same two tests flipped again four days later.

    Shifting the frame by one offset keeps the fixture's week structure and
    relative spacing intact while pinning the opener a month out, so there is no
    literal date left to expire. ``test_the_preseason_fixture_stays_ahead_of_the_clock``
    is the tripwire for a third time.
    """
    frame = schedules_frame()
    target = (datetime.now(UTC) + timedelta(days=30)).date()
    shift = (target - first_reg_kickoff(frame)).days
    # Only this season moves. The SEASON - 1 row is fixture evidence that prior
    # seasons get dropped, and dragging it forward would quietly make it look
    # like a current-season date.
    return frame.with_columns(
        pl.when(pl.col("season") == SEASON)
        .then((pl.col("gameday").str.to_date() + pl.duration(days=shift)).dt.strftime("%Y-%m-%d"))
        .otherwise(pl.col("gameday"))
        .alias("gameday")
    )


def test_the_preseason_fixture_stays_ahead_of_the_clock() -> None:
    """Guard the fixtures themselves — the check missing both times this broke.

    The preseason tests are only meaningful while *every* game in the unstarted
    frame is still in the future, and only the earliest one decides it. Asserting
    the whole column means a new fixture row with an old hard-coded date fails
    here, naming the fixture, instead of thirty tests away in a preseason
    assertion that reads like an ingest bug.
    """
    today = datetime.now(UTC).date()
    unstarted = unstarted_schedules_frame()

    # What `season_has_kicked_off` actually reads.
    assert first_reg_kickoff(unstarted) > today, "the unstarted fixture has already kicked off"

    # And every other current-season game too: only the earliest decides the
    # kickoff, so a stale date on any other row is invisible until it becomes
    # the earliest — which is exactly how this broke the second time.
    this_season = unstarted.filter(pl.col("season") == SEASON)["gameday"].str.to_date()
    assert this_season.min() > today, "a current-season fixture game is in the past"

    # The started frame is the mirror image: its opener must already have passed.
    assert first_reg_kickoff(started_schedules_frame()) < today

    # The shift slides the frame rather than flattening it, and leaves the
    # prior-season row where it is.
    original = schedules_frame().filter(pl.col("season") == SEASON)["gameday"].str.to_date()
    assert this_season.n_unique() == original.n_unique()
    assert this_season.max() - this_season.min() == original.max() - original.min()
    assert "2025-09-07" in unstarted["gameday"].to_list()


async def test_core_loader_failure_propagates_once_the_season_has_started(store: Store) -> None:
    """After kickoff a missing stats file is an outage and must not be swallowed."""

    def boom(seasons):
        raise RuntimeError("nflverse down")

    loaders = fake_loaders(player_stats=boom)
    loaders = replace(loaders, schedules=lambda seasons: started_schedules_frame())

    with pytest.raises(RuntimeError, match="nflverse down"):
        await ingest_nflverse(store, loaders=loaders, season=SEASON)


async def test_missing_stats_before_kickoff_is_the_calendar_not_an_outage(store: Store) -> None:
    """Preseason: nflverse has no stats file yet, so ingest the rest and carry on.

    Regression, 2026-09-01. This 404 used to abort the whole `stats` task, which
    left injuries and depth charts frozen; before that it was masked entirely by
    ingesting *last* season, which took the paid API down (DESIGN_NOTES §20).
    """

    def not_published_yet(seasons):
        raise RuntimeError("404 Not Found: stats_player_week_2026.parquet")

    loaders = fake_loaders(player_stats=not_published_yet)
    loaders = replace(loaders, schedules=lambda seasons: unstarted_schedules_frame())

    summary = await ingest_nflverse(store, loaders=loaders, season=SEASON)

    assert summary["preseason_gap"] is True
    # The schedule still advances, and the datasets that do exist still land.
    assert summary["schedule_weeks"] == 2
    assert summary["injuries"] > 0
    assert summary["depth_charts"] > 0

    # Stat freshness is withheld rather than stamped: we wrote no stats, and
    # claiming otherwise is what the season guard exists to catch.
    fresh = set(await get_data_freshness(store))
    assert "weekly_stats" not in fresh
    assert "usage_trends" not in fresh
    assert {"schedules", "injuries", "depth_charts"} <= fresh


async def test_empty_stats_before_kickoff_does_not_stamp_stat_freshness(store: Store) -> None:
    loaders = fake_loaders(player_stats=lambda seasons: stats_frame().head(0))
    loaders = replace(loaders, schedules=lambda seasons: unstarted_schedules_frame())

    summary = await ingest_nflverse(store, loaders=loaders, season=SEASON)

    assert summary["preseason_gap"] is True
    fresh = set(await get_data_freshness(store))
    assert "weekly_stats" not in fresh
    assert "usage_trends" not in fresh
    assert "def_vs_pos" not in fresh


async def test_empty_stats_after_kickoff_fails_the_ingest(store: Store) -> None:
    loaders = fake_loaders(player_stats=lambda seasons: stats_frame().head(0))
    loaders = replace(loaders, schedules=lambda seasons: started_schedules_frame())

    with pytest.raises(ValueError, match="no 2026 player stats after kickoff"):
        await ingest_nflverse(store, loaders=loaders, season=SEASON)


async def test_loaders_always_receive_explicit_seasons(store: Store) -> None:
    seen: dict[str, Any] = {}

    def spy(name: str, frame: pl.DataFrame):
        def loader(seasons):
            seen[name] = seasons
            return frame

        return loader

    loaders = Loaders(
        player_stats=spy("player_stats", stats_frame()),
        snap_counts=spy("snap_counts", snaps_frame()),
        depth_charts=spy("depth_charts", depth_charts_frame()),
        injuries=spy("injuries", injuries_frame()),
        schedules=spy("schedules", schedules_frame()),
        players=players_frame,
        pbp=spy("pbp", pbp_frame()),
        ff_playerids=ff_playerids_frame,
    )

    await ingest_nflverse(store, loaders=loaders, season=SEASON)

    assert seen == {name: [SEASON] for name in seen}
    assert set(seen) == {
        "player_stats",
        "snap_counts",
        "depth_charts",
        "injuries",
        "schedules",
        "pbp",
    }


# --- season resolution & default loaders ---------------------------------


def test_resolve_season_prefers_the_explicit_override(settings) -> None:
    assert resolve_season(settings, 2031) == 2031


def test_resolve_season_never_lets_upstream_outrank_season(monkeypatch, settings) -> None:
    """SEASON wins, even when nflreadpy disagrees. Especially then.

    Regression, 2026-09-01: `get_current_season()` used to win here and it lags
    the calendar — on Sept 1 it still returned 2025, so the Tuesday `stats` run
    re-ingested the 2025 schedule over the 2026 one and every paid route 503'd
    on `stale_season()`. Ingest and serving must not be able to disagree about
    which season this deployment sells, and SEASON is the side the serving guard
    enforces.
    """
    import nflreadpy

    # The exact shape of the outage: upstream lagging a season behind.
    monkeypatch.setattr(nflreadpy, "get_current_season", lambda *a, **k: settings.season - 1)
    assert resolve_season(settings) == settings.season

    # And a season ahead, which is the same bug pointed the other way.
    monkeypatch.setattr(nflreadpy, "get_current_season", lambda *a, **k: settings.season + 1)
    assert resolve_season(settings) == settings.season


def test_resolve_season_survives_upstream_being_unavailable(monkeypatch, settings) -> None:
    import nflreadpy

    def broken(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr(nflreadpy, "get_current_season", broken)
    assert resolve_season(settings) == settings.season


def test_resolve_season_warns_when_upstream_disagrees(monkeypatch, settings, caplog) -> None:
    """The disagreement is still worth knowing about — it is how you learn to bump SEASON."""
    import logging

    import nflreadpy

    monkeypatch.setattr(nflreadpy, "get_current_season", lambda *a, **k: settings.season + 1)
    with caplog.at_level(logging.WARNING):
        assert resolve_season(settings) == settings.season

    assert any("different season than SEASON" in r.message for r in caplog.records)


def test_resolve_season_is_quiet_when_upstream_agrees(monkeypatch, settings, caplog) -> None:
    import logging

    import nflreadpy

    monkeypatch.setattr(nflreadpy, "get_current_season", lambda *a, **k: settings.season)
    with caplog.at_level(logging.WARNING):
        resolve_season(settings)

    assert not [r for r in caplog.records if "different season" in r.message]


def test_default_loaders_never_call_load_schedules_bare(monkeypatch) -> None:
    """``load_schedules()`` with no seasons downloads every season since 1999."""
    import nflreadpy

    seen: dict[str, Any] = {}

    def fake(name: str):
        def loader(seasons=..., **kwargs):
            seen[name] = seasons
            return pl.DataFrame()

        return loader

    for name in (
        "load_player_stats",
        "load_snap_counts",
        "load_depth_charts",
        "load_injuries",
        "load_schedules",
        "load_pbp",
    ):
        monkeypatch.setattr(nflreadpy, name, fake(name))
    monkeypatch.setattr(nflreadpy, "load_players", lambda: pl.DataFrame())

    loaders = default_loaders()
    for call in (
        loaders.player_stats,
        loaders.snap_counts,
        loaders.depth_charts,
        loaders.injuries,
        loaders.schedules,
        loaders.pbp,
    ):
        call([SEASON])
    loaders.players()

    assert seen == {
        "load_player_stats": [SEASON],
        "load_snap_counts": [SEASON],
        "load_depth_charts": [SEASON],
        "load_injuries": [SEASON],
        "load_schedules": [SEASON],
        "load_pbp": [SEASON],
    }


# -- the prior-season rebuild path -------------------------------------------


async def test_stats_only_rebuilds_the_three_stat_datasets_and_nothing_else(
    store: Store,
) -> None:
    """A rebuild of last season must not overwrite what describes this one.

    Every other dataset the stats task writes — the schedule (and with it
    ``meta/schedule_weeks``), the preseason-gap marker, injuries, depth
    charts — is current-season truth. A 2025 schedule trips the season guard
    (DESIGN_NOTES §20); 2025 depth charts are what the Week 1 boards lean on.
    """
    await seed_id_map(store)
    await store.set(META_COLLECTION, "schedule_weeks", {"season": SEASON + 1, "weeks": {}})
    await store.set(
        META_COLLECTION, "preseason_gap", {"season": SEASON + 1, "datasets": ["weekly_stats"]}
    )
    await store.set(DEPTH_CHARTS_COLLECTION, "MIN", {"team": "MIN", "season": SEASON + 1})
    await store.set(INJURIES_COLLECTION, "6794", {"player_id": "6794", "season": SEASON + 1})
    await store.set(META_COLLECTION, FRESHNESS_DOC_ID, {"depth_charts": "kept"})

    summary = await ingest_nflverse(store, loaders=fake_loaders(), season=SEASON, stats_only=True)

    assert summary["stats_only"] is True
    assert summary["schedules"] == 0 and summary["injuries"] == 0
    assert summary["depth_charts"] == 0
    # The three stat-derived datasets are rebuilt...
    assert (await get_usage_trends(store, "6794"))["through_week"] == 4
    assert (await get_def_vs_pos(store, "atl", "qb"))["rank"] == 1
    assert await get_weekly_stats(store, "6794", SEASON, [1])
    # ...and everything current-season is exactly as it was.
    assert (await store.get(META_COLLECTION, "schedule_weeks"))["season"] == SEASON + 1
    assert (await store.get(META_COLLECTION, "preseason_gap"))["datasets"] == ["weekly_stats"]
    assert (await store.get(DEPTH_CHARTS_COLLECTION, "MIN"))["season"] == SEASON + 1
    assert (await store.get(INJURIES_COLLECTION, "6794"))["season"] == SEASON + 1
    freshness = await store.get(META_COLLECTION, FRESHNESS_DOC_ID)
    assert freshness is not None
    assert {"weekly_stats", "usage_trends", "def_vs_pos"} <= set(freshness)
    assert freshness["depth_charts"] == "kept" and "injuries" not in freshness


async def test_stats_only_refuses_once_the_current_season_has_kicked_off(
    store: Store,
) -> None:
    """Mid-season, last season's usage would replace this season's, stamped fresh."""
    await seed_id_map(store)
    await store.set(
        META_COLLECTION,
        "schedule_weeks",
        {"season": SEASON + 1, "weeks": {"1": "2000-09-10T17:00:00Z"}},
    )
    await store.set(USAGE_TRENDS_COLLECTION, "6794", {"season": SEASON + 1, "through_week": 3})

    with pytest.raises(ValueError, match="kicked off"):
        await ingest_nflverse(store, loaders=fake_loaders(), season=SEASON, stats_only=True)

    assert (await store.get(USAGE_TRENDS_COLLECTION, "6794"))["season"] == SEASON + 1


async def test_stats_only_treats_a_missing_stats_file_as_a_failure(store: Store) -> None:
    """A rebuild is for a season that was played; a 404 is never the calendar."""
    await seed_id_map(store)

    def no_file(seasons: Any) -> pl.DataFrame:
        raise FileNotFoundError("stats_player_week_2025 not published")

    with pytest.raises(FileNotFoundError):
        await ingest_nflverse(
            store, loaders=fake_loaders(player_stats=no_file), season=SEASON, stats_only=True
        )
