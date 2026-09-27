"""Tests for the nightly Sleeper player sync.

Offline: the Sleeper dump is injected, never fetched. The load-bearing assertion
is that the documents written here are readable through
:mod:`api.data.stats_store` — that module's docstring is the contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.core.store import Store
from api.data.stats_store import (
    PLAYER_INDEX_COLLECTION,
    PLAYERS_COLLECTION,
    get_data_freshness,
    get_player,
    normalize_name,
    resolve_player,
)
from ingest.sleeper_players import (
    ID_MAP_COLLECTION,
    backfill_gsis,
    build_id_map,
    build_player_index,
    is_fantasy_relevant,
    select_players,
    sync_players,
)


def sleeper_dump() -> dict[str, dict[str, Any]]:
    """A miniature ``/players/nfl`` dump with the awkward cases baked in.

    Covers: a normal star, two players sharing a name, a team defense (no
    ``full_name``, no ``gsis_id``), an accented/suffixed name, a player with a
    null ``espn_id``, an inactive player, and a non-fantasy position.
    """
    return {
        "4046": {
            "player_id": "4046",
            "full_name": "Patrick Mahomes",
            "first_name": "Patrick",
            "last_name": "Mahomes",
            "position": "QB",
            "fantasy_positions": ["QB"],
            "team": "KC",
            "status": "Active",
            "active": True,
            "injury_status": None,
            "depth_chart_order": 1,
            "depth_chart_position": "QB",
            "search_rank": 3,
            "age": 30,
            "years_exp": 8,
            "news_updated": 1758000000000,
            "gsis_id": "00-0033873",
            "espn_id": 3139477,  # int on the wire
        },
        "4984": {
            "player_id": "4984",
            "full_name": "Josh Allen",
            "position": "QB",
            "fantasy_positions": ["QB"],
            "team": "BUF",
            "status": "Active",
            "active": True,
            "search_rank": 1,
            "years_exp": 8,
            "gsis_id": "00-0034857",
            "espn_id": 3918298,
        },
        # The docstring's canonical ambiguity example is "Josh Allen" QB vs LB —
        # but the LB is filtered out by the fantasy-position filter, so the
        # ambiguity that actually survives is between fantasy positions.
        "5045": {
            "player_id": "5045",
            "full_name": "Josh Allen",
            "position": "LB",
            "fantasy_positions": ["LB"],
            "team": "JAX",
            "status": "Active",
            "active": True,
            "search_rank": 400,
            "gsis_id": "00-0035240",
            "espn_id": 3915511,
        },
        "6001": {
            "player_id": "6001",
            "full_name": "Josh Allen",
            "position": "RB",
            "fantasy_positions": ["RB"],
            "team": "JAX",
            "status": "Active",
            "active": True,
            "search_rank": 400,
            "gsis_id": "00-0035240",
            "espn_id": 3915511,
        },
        "6794": {
            "player_id": "6794",
            "full_name": "Ja'Marr Chase",
            "position": "WR",
            "fantasy_positions": ["WR"],
            "team": "CIN",
            "status": "Active",
            "active": True,
            "search_rank": 2,
            "years_exp": 5,
            "gsis_id": "00-0036900",
            "espn_id": None,  # cross-id gaps are normal, not an error
        },
        "8110": {
            "player_id": "8110",
            "full_name": "Marvin Harrison Jr.",
            "position": "WR",
            "fantasy_positions": ["WR"],
            "team": "ARI",
            "status": "Active",
            "active": True,
            "search_rank": 12,
            "years_exp": 2,
            "gsis_id": None,  # unreconciled by Sleeper -> no id_map entry
            "espn_id": 4432708,
        },
        "KC": {
            "player_id": "KC",
            "first_name": "Kansas City",
            "last_name": "Chiefs",
            "position": "DEF",
            "fantasy_positions": ["DEF"],
            "team": "KC",
            "status": "Active",
            "active": True,
        },
        "1234": {
            "player_id": "1234",
            "full_name": "Retired Guy",
            "position": "RB",
            "fantasy_positions": ["RB"],
            "team": None,
            "status": "Inactive",
            "active": False,
            "gsis_id": "00-0011111",
        },
        "9999": {
            "player_id": "9999",
            "full_name": "Anonymous Lineman",
            "position": "OL",
            "fantasy_positions": ["OL"],
            "team": "SEA",
            "status": "Active",
            "active": True,
            "gsis_id": "00-0022222",
        },
        "bogus": "not-a-dict",
    }


def test_filter_keeps_active_fantasy_positions_only() -> None:
    dump = sleeper_dump()

    kept = {doc["player_id"] for doc in select_players(dump)}

    assert kept == {"4046", "4984", "6001", "6794", "8110", "KC"}
    assert not is_fantasy_relevant(dump["1234"])  # inactive
    assert not is_fantasy_relevant(dump["9999"])  # non-fantasy position (OL)
    assert not is_fantasy_relevant(dump["5045"])  # non-fantasy position (LB)


def test_player_doc_matches_the_documented_shape() -> None:
    docs = {d["player_id"]: d for d in select_players(sleeper_dump())}

    mahomes = docs["4046"]
    assert mahomes["name"] == "Patrick Mahomes"
    assert mahomes["search_name"] == normalize_name("Patrick Mahomes") == "patrick mahomes"
    assert mahomes["position"] == "QB"
    assert mahomes["team"] == "KC"
    assert mahomes["status"] == "Active"
    assert mahomes["injury_status"] is None
    assert mahomes["gsis_id"] == "00-0033873"
    assert mahomes["espn_id"] == "3139477"  # int on the wire, string in the store
    assert mahomes["years_exp"] == 8
    # extras the analysis wave asked for
    assert mahomes["fantasy_positions"] == ["QB"]
    assert mahomes["depth_chart_order"] == 1
    assert mahomes["search_rank"] == 3
    assert mahomes["news_updated"] == 1758000000000
    assert mahomes["active"] is True


def test_team_defense_name_is_assembled_from_parts() -> None:
    docs = {d["player_id"]: d for d in select_players(sleeper_dump())}

    assert docs["KC"]["name"] == "Kansas City Chiefs"
    assert docs["KC"]["position"] == "DEF"
    assert docs["KC"]["gsis_id"] is None


def test_player_index_groups_ambiguous_names_by_search_rank() -> None:
    docs = select_players(sleeper_dump())

    index = dict(build_player_index(docs))

    assert "josh allen" in index
    candidates = index["josh allen"]["candidates"]
    assert [c["player_id"] for c in candidates] == ["4984", "6001"]  # QB (rank 1) first
    assert candidates[0]["position"] == "QB"
    # normalize_name() rules must hold: apostrophes dropped, suffixes trimmed
    assert "jamarr chase" in index
    assert "marvin harrison" in index


def test_id_map_skips_missing_gsis_and_tolerates_null_espn() -> None:
    docs = select_players(sleeper_dump())

    id_map = dict(build_id_map(docs))

    assert set(id_map) == {"00-0033873", "00-0034857", "00-0035240", "00-0036900"}
    assert id_map["00-0033873"] == {
        "gsis_id": "00-0033873",
        "sleeper_id": "4046",
        "espn_id": "3139477",
        "name": "Patrick Mahomes",
    }
    assert id_map["00-0035240"]["sleeper_id"] == "6001"  # the LB never made it in
    assert id_map["00-0036900"]["espn_id"] is None  # missing cross-id, not a failure
    assert "KC" not in id_map


async def test_sync_players_writes_every_collection_and_is_readable(store: Store) -> None:
    counts = await sync_players(store, dump=sleeper_dump())

    assert counts == {
        "gsis_backfilled": 0,
        "players": 6,
        "index_names": 5,
        "id_map": 4,
        "players_deleted": 0,
        "index_names_deleted": 0,
        "id_map_deleted": 0,
    }

    # read back through the contract, not the raw store
    mahomes = await get_player(store, "4046")
    assert mahomes["name"] == "Patrick Mahomes"

    allens = await resolve_player(store, "Josh Allen")
    assert [c["player_id"] for c in allens] == ["4984", "6001"]
    assert await resolve_player(store, "  ja'marr  CHASE ") == [
        {"player_id": "6794", "name": "Ja'Marr Chase", "team": "CIN", "position": "WR"}
    ]
    assert await resolve_player(store, "Nobody At All") == []

    assert (await store.get(ID_MAP_COLLECTION, "00-0033873"))["sleeper_id"] == "4046"

    freshness = await get_data_freshness(store)
    assert set(freshness) == {"players", "player_index", "id_map"}


async def test_sync_players_fetches_through_the_client_when_no_dump(store: Store) -> None:
    class StubClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get_players(self) -> dict[str, Any]:
            self.calls += 1
            return sleeper_dump()

    client = StubClient()
    counts = await sync_players(store, client=client)  # type: ignore[arg-type]

    assert client.calls == 1
    assert counts["players"] == 6


async def test_sync_prunes_players_that_left_the_filter_set(store: Store) -> None:
    """A retired player must not linger in name resolution or candidate scans."""
    await sync_players(store, dump=sleeper_dump())

    tomorrow = sleeper_dump()
    tomorrow["4046"]["active"] = False  # Mahomes retires overnight
    tomorrow["6001"]["active"] = False  # one of the two Josh Allens does too
    counts = await sync_players(store, dump=tomorrow)

    assert counts["players_deleted"] == 2
    assert counts["id_map_deleted"] == 2

    # Gone from every collection the nightly sync owns.
    assert await get_player(store, "4046") is None
    assert await resolve_player(store, "Patrick Mahomes") == []
    assert await store.get(ID_MAP_COLLECTION, "00-0033873") is None
    assert await store.get(PLAYER_INDEX_COLLECTION, "patrick mahomes") is None

    # A name whose candidate list merely *shrank* survives, minus the leaver.
    allens = await resolve_player(store, "Josh Allen")
    assert [c["player_id"] for c in allens] == ["4984"]

    # Survivors are untouched.
    assert (await get_player(store, "6794"))["name"] == "Ja'Marr Chase"
    assert (await store.get(ID_MAP_COLLECTION, "00-0036900"))["sleeper_id"] == "6794"


async def test_sync_never_prunes_against_an_empty_dump(store: Store) -> None:
    """A truncated or failed Sleeper fetch must not empty the player database."""
    await sync_players(store, dump=sleeper_dump())
    freshness_before = await get_data_freshness(store)

    with pytest.raises(ValueError, match="only 0 eligible players"):
        await sync_players(store, dump={})

    assert (await get_player(store, "4046"))["name"] == "Patrick Mahomes"
    assert await get_data_freshness(store) == freshness_before


# ---------------------------------------------------------------------------
# gsis backfill: Sleeper omits the join key for the players that matter most
# ---------------------------------------------------------------------------


def test_backfill_fills_only_the_blanks_sleeper_leaves() -> None:
    """Sleeper is authoritative where it has the id; the bridge fills gaps.

    Measured 2026-09-01: only 16% of the top 50 fantasy players by market rank
    had a `gsis_id` from Sleeper. Without it there is no nflverse join, so
    Ja'Marr Chase and Bijan Robinson had no game log on any paid endpoint.
    """
    docs = [
        {"player_id": "7564", "gsis_id": None},  # Chase — Sleeper has none
        {"player_id": "5870", "gsis_id": "00-0035710"},  # Jones — Sleeper has one
        {"player_id": "9999", "gsis_id": None},  # not in the bridge either
    ]

    filled = backfill_gsis(docs, {"7564": "00-0036900", "5870": "SHOULD-NOT-WIN"})

    assert filled == 1
    assert docs[0]["gsis_id"] == "00-0036900"
    assert docs[1]["gsis_id"] == "00-0035710", "Sleeper's own id must not be overwritten"
    assert docs[2]["gsis_id"] is None


def test_backfill_without_a_bridge_changes_nothing() -> None:
    """A missing cross-reference degrades to Sleeper's coverage, never raises."""
    docs = [{"player_id": "7564", "gsis_id": None}]

    assert backfill_gsis(docs, None) == 0
    assert backfill_gsis(docs, {}) == 0
    assert docs[0]["gsis_id"] is None


async def test_sync_players_backfills_and_reports_the_count(store: Store) -> None:
    # 8110 (Marvin Harrison Jr.) is the dump's no-gsis case — the same shape as
    # the real gap, where Sleeper omits the key for a first-round fantasy pick.
    counts = await sync_players(store, dump=sleeper_dump(), gsis_bridge={"8110": "00-0039337"})

    assert counts["gsis_backfilled"] == 1
    assert (await store.get(PLAYERS_COLLECTION, "8110"))["gsis_id"] == "00-0039337"
    # The id map is built from the filled-in id, which is the whole point:
    # without that row, nflverse stats never reach a Sleeper-keyed document.
    assert (await store.get(ID_MAP_COLLECTION, "00-0039337"))["sleeper_id"] == "8110"
