"""Read models over the store, and the ingest doc-shape contract they imply."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from api.core.store import MemoryStore, Store
from api.data.stats_store import (
    DEF_VS_POS_COLLECTION,
    FRESHNESS_DOC_ID,
    GAP_TRUST_SECONDS,
    META_COLLECTION,
    PLAYER_INDEX_COLLECTION,
    PLAYERS_COLLECTION,
    SCHEDULES_COLLECTION,
    TRENDING_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    exempt_datasets,
    get_data_freshness,
    get_def_vs_pos,
    get_player,
    get_schedule,
    get_trending,
    get_usage_trends,
    get_weekly_stats,
    normalize_name,
    resolve_player,
    stale_datasets,
    weekly_stats_collection,
)


@pytest.fixture
def store() -> Store:
    return MemoryStore()


# -- name normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Patrick Mahomes", "patrick mahomes"),
        ("Ja'Marr Chase", "jamarr chase"),
        ("Marvin Harrison Jr.", "marvin harrison"),
        ("Odell Beckham Jr", "odell beckham"),
        ("  A.J.  Brown  ", "aj brown"),
        ("Amon-Ra St. Brown", "amonra st brown"),
        ("MICHAEL PITTMAN III", "michael pittman"),
        ("José Ramírez", "jose ramirez"),
        ("", ""),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


def test_weekly_stats_collection_path() -> None:
    assert weekly_stats_collection(2026, 3) == "weekly_stats/2026_3/players"


# -- resolve_player -------------------------------------------------------


async def test_resolve_player_hit(store: Store) -> None:
    await store.set(
        PLAYER_INDEX_COLLECTION,
        "josh allen",
        {
            "candidates": [
                {"player_id": "4984", "name": "Josh Allen", "team": "BUF", "position": "QB"},
                {"player_id": "5045", "name": "Josh Allen", "team": "JAX", "position": "LB"},
            ]
        },
    )
    candidates = await resolve_player(store, "Josh Allen")
    assert len(candidates) == 2
    assert {c["team"] for c in candidates} == {"BUF", "JAX"}


async def test_resolve_player_normalizes_input(store: Store) -> None:
    await store.set(
        PLAYER_INDEX_COLLECTION,
        "jamarr chase",
        {
            "candidates": [
                {"player_id": "7564", "name": "Ja'Marr Chase", "team": "CIN", "position": "WR"}
            ]
        },
    )
    assert (await resolve_player(store, "ja'marr CHASE"))[0]["player_id"] == "7564"


async def test_resolve_player_miss_returns_empty(store: Store) -> None:
    assert await resolve_player(store, "Nobody At All") == []
    assert await resolve_player(store, "   ") == []


async def test_resolve_player_tolerates_malformed_index(store: Store) -> None:
    await store.set(
        PLAYER_INDEX_COLLECTION, "broken guy", {"candidates": ["oops", {"player_id": "1"}]}
    )
    assert await resolve_player(store, "Broken Guy") == [{"player_id": "1"}]
    await store.set(PLAYER_INDEX_COLLECTION, "empty guy", {})
    assert await resolve_player(store, "Empty Guy") == []


# -- get_player -----------------------------------------------------------


async def test_get_player(store: Store) -> None:
    await store.set(
        PLAYERS_COLLECTION, "4046", {"name": "Patrick Mahomes", "position": "QB", "team": "KC"}
    )
    doc = await get_player(store, "4046")
    assert doc is not None and doc["team"] == "KC"
    assert await get_player(store, "nope") is None


# -- weekly stats ---------------------------------------------------------


async def test_get_weekly_stats_sorted_and_sparse(store: Store) -> None:
    await store.set(weekly_stats_collection(2026, 1), "4046", {"week": 1, "fantasy_points": 18.2})
    await store.set(weekly_stats_collection(2026, 3), "4046", {"week": 3, "fantasy_points": 25.7})
    rows = await get_weekly_stats(store, "4046", 2026, [3, 2, 1])
    assert [r["week"] for r in rows] == [1, 3]  # week 2 missing (bye) is skipped, output sorted
    assert rows[-1]["fantasy_points"] == 25.7


async def test_get_weekly_stats_infers_week_when_absent(store: Store) -> None:
    await store.set(weekly_stats_collection(2026, 2), "1", {"fantasy_points": 5.0})
    rows = await get_weekly_stats(store, "1", 2026, [2])
    assert rows[0]["week"] == 2


async def test_get_weekly_stats_wrong_season_is_empty(store: Store) -> None:
    await store.set(weekly_stats_collection(2026, 1), "1", {"week": 1})
    assert await get_weekly_stats(store, "1", 2025, [1]) == []


# -- usage trends ---------------------------------------------------------


async def test_get_usage_trends(store: Store) -> None:
    await store.set(
        USAGE_TRENDS_COLLECTION,
        "9502",
        {"snap_pct_l4w": 0.62, "target_share_delta": 0.05, "trend": "rising"},
    )
    doc = await get_usage_trends(store, "9502")
    assert doc is not None and doc["trend"] == "rising"
    assert await get_usage_trends(store, "unknown") is None


# -- def vs pos -----------------------------------------------------------


async def test_get_def_vs_pos(store: Store) -> None:
    await store.set(
        DEF_VS_POS_COLLECTION,
        "ATL",
        {
            "team": "ATL",
            "season": 2026,
            "through_week": 3,
            "positions": {
                "RB": {"points_allowed_per_game": 24.1, "rank": 2},
                "WR": {"points_allowed_per_game": 30.0, "rank": 14},
            },
        },
    )
    rb = await get_def_vs_pos(store, "atl", "rb")
    assert rb == {
        "team": "ATL",
        "position": "RB",
        "season": 2026,
        "through_week": 3,
        "points_allowed_per_game": 24.1,
        "rank": 2,
    }
    assert await get_def_vs_pos(store, "ATL", "QB") is None
    assert await get_def_vs_pos(store, "XXX", "RB") is None


# -- trending -------------------------------------------------------------


async def test_get_trending(store: Store) -> None:
    await store.set(
        TRENDING_COLLECTION,
        "add",
        {
            "kind": "add",
            "entries": [{"player_id": "9502", "count": 51234, "name": "Tank Bigsby"}, "junk"],
        },
    )
    entries = await get_trending(store, "add")
    assert entries == [{"player_id": "9502", "count": 51234, "name": "Tank Bigsby"}]


async def test_get_trending_missing_returns_empty(store: Store) -> None:
    assert await get_trending(store, "drop") == []


async def test_get_trending_rejects_bad_kind(store: Store) -> None:
    with pytest.raises(ValueError):
        await get_trending(store, "sideways")


# -- schedule -------------------------------------------------------------


async def test_get_schedule_home_and_away(store: Store) -> None:
    await store.set(
        SCHEDULES_COLLECTION,
        "2026_3",
        {
            "season": 2026,
            "week": 3,
            "games": [
                {"home": "KC", "away": "ATL", "kickoff": "2026-09-21T17:00:00Z"},
                {"home": "SF", "away": "SEA", "kickoff": "2026-09-21T20:25:00Z"},
            ],
        },
    )
    kc = await get_schedule(store, "kc", 3, 2026)
    assert kc == {
        "season": 2026,
        "week": 3,
        "team": "KC",
        "opponent": "ATL",
        "home": True,
        "kickoff": "2026-09-21T17:00:00Z",
    }
    atl = await get_schedule(store, "ATL", 3, 2026)
    assert atl is not None and atl["home"] is False and atl["opponent"] == "KC"


async def test_get_schedule_bye_week_and_missing_week(store: Store) -> None:
    await store.set(SCHEDULES_COLLECTION, "2026_3", {"games": [{"home": "KC", "away": "ATL"}]})
    assert await get_schedule(store, "BUF", 3, 2026) is None  # on bye
    assert await get_schedule(store, "KC", 9, 2026) is None  # week not ingested


# -- freshness ------------------------------------------------------------


async def test_get_data_freshness(store: Store) -> None:
    await store.set(
        META_COLLECTION,
        FRESHNESS_DOC_ID,
        {"weekly_stats": "2026-09-16T09:02:11Z", "trending": "2026-09-16T13:30:00Z"},
    )
    freshness = await get_data_freshness(store)
    assert freshness == {
        "weekly_stats": "2026-09-16T09:02:11Z",
        "trending": "2026-09-16T13:30:00Z",
    }
    assert "_id" not in freshness


async def test_get_data_freshness_missing_returns_empty(store: Store) -> None:
    assert await get_data_freshness(store) == {}


def test_stale_datasets_uses_per_dataset_age_limits() -> None:
    now = datetime(2026, 9, 1, 12, tzinfo=UTC)
    freshness = {
        "players": "2026-08-31T11:00:00Z",
        "trending": "2026-09-01T09:00:00Z",
        "weekly_stats": "2026-08-29T12:00:01Z",
    }
    assert stale_datasets(freshness, now=now) == ["trending"]


def test_stale_datasets_treats_invalid_markers_as_stale() -> None:
    assert stale_datasets({"players": "not-a-time"}, now=datetime(2026, 9, 1, tzinfo=UTC)) == [
        "players"
    ]


# ---------------------------------------------------------------------------
# The preseason gap: data that does not exist yet is not data that went stale
# ---------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def test_a_dataset_upstream_cannot_publish_is_not_stale() -> None:
    """The interaction that would have taken 8 of 10 endpoints down on 2026-09-05.

    A 96h SLA on `weekly_stats` plus an ingest that correctly refuses to stamp
    freshness for a season nflverse has not published equals a guaranteed,
    dated, silent outage. Neither half is wrong; together they refuse to sell
    over the calendar.
    """
    now = datetime(2026, 9, 6, tzinfo=UTC)
    freshness = {"weekly_stats": _iso(now - timedelta(hours=120))}  # well past 96h
    gap = {
        "season": 2026,
        "datasets": ["weekly_stats"],
        "recorded_at": _iso(now - timedelta(hours=2)),
    }

    assert stale_datasets(freshness, ["weekly_stats"], now=now) == ["weekly_stats"]
    assert stale_datasets(freshness, ["weekly_stats"], now=now, gap=gap) == []


def test_the_exemption_dies_with_the_ingest_that_asserts_it() -> None:
    """A stopped job must not keep its own excuse alive."""
    now = datetime(2026, 9, 6, tzinfo=UTC)
    freshness = {"weekly_stats": _iso(now - timedelta(hours=120))}
    stale_gap = {
        "datasets": ["weekly_stats"],
        "recorded_at": _iso(now - timedelta(hours=GAP_TRUST_SECONDS / 3600 + 1)),
    }

    assert stale_datasets(freshness, ["weekly_stats"], now=now, gap=stale_gap) == ["weekly_stats"]


def test_the_exemption_covers_only_what_it_names() -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    freshness = {
        "weekly_stats": _iso(now - timedelta(hours=120)),
        "trending": _iso(now - timedelta(hours=120)),
    }
    gap = {"datasets": ["weekly_stats"], "recorded_at": _iso(now)}

    assert stale_datasets(freshness, ["weekly_stats", "trending"], now=now, gap=gap) == ["trending"]


def test_a_malformed_or_absent_gap_marker_exempts_nothing() -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    for gap in (None, {}, {"datasets": ["weekly_stats"]}, {"recorded_at": "not-a-date"}):
        assert exempt_datasets(gap, now=now) == set()


# -- the draft pool -----------------------------------------------------------


async def test_draft_pool_excludes_players_with_no_team() -> None:
    """Sleeper still lists Tom Brady as active with a search_rank; his team is None."""
    from api.data.stats_store import PLAYERS_COLLECTION, get_draft_pool

    store = MemoryStore()
    docs = [
        {
            "player_id": "1",
            "name": "Bijan Robinson",
            "position": "RB",
            "team": "ATL",
            "search_rank": 1,
        },
        {"player_id": "2", "name": "Tom Brady", "position": "QB", "team": None, "search_rank": 74},
        {"player_id": "3", "name": "No Rank", "position": "WR", "team": "KC"},
        {"player_id": "4", "name": "Lineman", "position": "OL", "team": "KC", "search_rank": 5},
        {
            "player_id": "5",
            "name": "Bool Rank",
            "position": "TE",
            "team": "KC",
            "search_rank": True,
        },
        {"player_id": "6", "name": "Josh Allen", "position": "QB", "team": "BUF", "search_rank": 3},
    ]
    for doc in docs:
        await store.set(PLAYERS_COLLECTION, doc["player_id"], doc)

    pool = await get_draft_pool(store)
    assert [p["name"] for p in pool] == ["Bijan Robinson", "Josh Allen"]


async def test_draft_pool_excludes_sleepers_unranked_sentinel() -> None:
    """9999999 is Sleeper's "no opinion", not the 9,999,999th pick."""
    from api.data.stats_store import get_draft_pool, market_rank

    store = MemoryStore()
    for pid, rank in (("1", 12), ("2", 9999999), ("3", 4999)):
        await store.set(
            PLAYERS_COLLECTION,
            pid,
            {
                "player_id": pid,
                "name": f"P{pid}",
                "position": "WR",
                "team": "KC",
                "search_rank": rank,
            },
        )

    assert [p["player_id"] for p in await get_draft_pool(store)] == ["1", "3"]
    assert market_rank(12) == 12
    assert market_rank(9999999) is None
    assert market_rank(0) is None
    assert market_rank(True) is None
    assert market_rank("12") is None


class _FirestoreLikeStore(MemoryStore):
    """Firestore raises ValueError on a doc id containing a path separator."""

    async def get(self, collection: str, doc_id: str) -> dict | None:  # type: ignore[override]
        if "/" in doc_id or not doc_id:
            raise ValueError(f"invalid document id {doc_id!r}")
        return await super().get(collection, doc_id)


@pytest.mark.parametrize("token", ["a/b", "", "  ", "../players"])
async def test_get_player_treats_an_impossible_id_as_unknown(token: str) -> None:
    assert await get_player(_FirestoreLikeStore(), token) is None


async def test_get_player_absorbs_a_store_that_rejects_the_id() -> None:
    class Rejecting(MemoryStore):
        async def get(self, collection: str, doc_id: str) -> dict | None:  # type: ignore[override]
            raise ValueError("Document path must not contain '//'")

    assert await get_player(Rejecting(), "4046") is None


@pytest.mark.parametrize(
    ("status", "out"),
    [
        ("Out", True),
        ("IR", True),
        ("Injured Reserve", True),
        ("Sus", True),
        ("Suspended", True),
        ("PUP", True),
        ("Physically Unable to Perform", True),
        ("NFI", True),
        ("Non Football Injury", True),
        ("Inactive", True),
        ("Doubtful", True),
        ("out", True),
        (" sus ", True),
        ("Questionable", False),
        ("Active", False),
        (None, False),
        ("", False),
    ],
)
def test_out_statuses_cover_both_sleeper_vocabularies(status: str | None, out: bool) -> None:
    from api.data.stats_store import is_out_status

    assert is_out_status(status) is out
