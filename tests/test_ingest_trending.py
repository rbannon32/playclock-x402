"""Tests for the Sleeper trending refresh (30-minute poll)."""

from __future__ import annotations

from typing import Any

import pytest

from api.core.store import Store
from api.data.sleeper import TrendingEntry
from api.data.stats_store import get_data_freshness, get_trending
from ingest.trending import build_trending_doc, refresh_trending


async def seed_players(store: Store) -> None:
    await store.set(
        "players",
        "6794",
        {"player_id": "6794", "name": "Ja'Marr Chase", "position": "WR", "team": "CIN"},
    )


async def test_build_trending_doc_joins_identity_from_players(store: Store) -> None:
    await seed_players(store)

    doc = await build_trending_doc(
        store,
        "add",
        [{"player_id": "6794", "count": 51234}, {"player_id": "unknown", "count": 12}],
        fetched_at="2026-09-16T13:30:00Z",
    )

    assert doc["kind"] == "add"
    assert doc["lookback_hours"] == 24
    assert doc["fetched_at"] == "2026-09-16T13:30:00Z"
    assert doc["entries"][0] == {
        "player_id": "6794",
        "count": 51234,
        "name": "Ja'Marr Chase",
        "position": "WR",
        "team": "CIN",
    }
    # a player Sleeper knows but the last nightly sync did not: id + count only
    assert doc["entries"][1] == {"player_id": "unknown", "count": 12}


async def test_refresh_trending_writes_both_boards(store: Store) -> None:
    await seed_players(store)

    counts = await refresh_trending(
        store,
        boards={
            "add": [{"player_id": "6794", "count": 51234}],
            "drop": [{"player_id": "6794", "count": 900}, {"player_id": "4046", "count": 100}],
        },
    )

    assert counts == {"add": 1, "drop": 2}

    adds = await get_trending(store, "add")
    assert [e["player_id"] for e in adds] == ["6794"]
    assert adds[0]["name"] == "Ja'Marr Chase"

    drops = await get_trending(store, "drop")
    assert [e["count"] for e in drops] == [900, 100]  # Sleeper ordering preserved

    assert set(await get_data_freshness(store)) == {"trending"}


async def test_refresh_trending_fetches_through_the_client(store: Store) -> None:
    class StubClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, int]] = []

        async def get_trending(
            self, kind: str, lookback_hours: int = 24, limit: int = 25
        ) -> list[TrendingEntry]:
            self.calls.append((kind, lookback_hours, limit))
            return [TrendingEntry(player_id="6794", count=7)]

    client = StubClient()
    counts = await refresh_trending(store, client=client, lookback_hours=12, limit=5)  # type: ignore[arg-type]

    assert counts == {"add": 1, "drop": 1}
    assert client.calls == [("add", 12, 5), ("drop", 12, 5)]
    doc: dict[str, Any] = await store.get("trending", "add")
    assert doc["lookback_hours"] == 12


async def test_refresh_trending_replaces_the_previous_board(store: Store) -> None:
    await refresh_trending(store, boards={"add": [{"player_id": "1", "count": 5}]})
    await refresh_trending(store, boards={"add": [{"player_id": "2", "count": 9}]})

    assert [e["player_id"] for e in await get_trending(store, "add")] == ["2"]


async def test_an_empty_add_board_keeps_the_previous_one(store: Store) -> None:
    """An empty 200 from Sleeper must not stamp `trending` fresh over nothing."""
    await refresh_trending(store, boards={"add": [{"player_id": "1", "count": 5}]})

    with pytest.raises(ValueError, match="empty trending add board"):
        await refresh_trending(store, boards={"add": [], "drop": []})

    assert [e["player_id"] for e in await get_trending(store, "add")] == ["1"]
