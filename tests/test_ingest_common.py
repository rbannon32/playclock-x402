"""Tests for the shared ingest plumbing (chunked writes, freshness, logging)."""

from __future__ import annotations

import json
import logging
import re

import pytest

from api.core.store import Store
from api.data.stats_store import get_data_freshness
from ingest.common import (
    StructuredFormatter,
    delete_missing,
    update_freshness,
    utc_now_iso,
    write_docs,
)

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def test_utc_now_iso_matches_the_documented_format() -> None:
    assert ISO_Z.match(utc_now_iso())


async def test_write_docs_chunks_and_returns_count(store: Store) -> None:
    docs = [(f"p{i}", {"player_id": f"p{i}", "n": i}) for i in range(250)]

    written = await write_docs(store, "players", docs, chunk_size=100)

    assert written == 250
    assert len(await store.list("players")) == 250
    assert (await store.get("players", "p249"))["n"] == 249


async def test_write_docs_empty_is_a_noop(store: Store) -> None:
    assert await write_docs(store, "players", []) == 0
    assert await store.list("players") == []


async def test_write_docs_rejects_a_nonsense_chunk_size(store: Store) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        await write_docs(store, "players", [("a", {})], chunk_size=0)


async def test_write_docs_merge_preserves_untouched_keys(store: Store) -> None:
    await store.set("players", "4046", {"name": "Patrick Mahomes", "team": "KC"})

    await write_docs(store, "players", [("4046", {"injury_status": "Questionable"})], merge=True)

    doc = await store.get("players", "4046")
    assert doc["name"] == "Patrick Mahomes"
    assert doc["injury_status"] == "Questionable"


async def test_delete_missing_prunes_only_what_left_the_snapshot(store: Store) -> None:
    await write_docs(store, "players", [(f"p{i}", {"n": i}) for i in range(250)], chunk_size=100)

    deleted = await delete_missing(store, "players", {f"p{i}" for i in range(200)}, chunk_size=100)

    assert deleted == 50
    assert len(await store.list("players")) == 200
    assert await store.get("players", "p249") is None
    assert (await store.get("players", "p0"))["n"] == 0


async def test_delete_missing_on_an_untouched_collection_is_a_noop(store: Store) -> None:
    assert await delete_missing(store, "players", {"4046"}) == 0


async def test_delete_missing_rejects_a_nonsense_chunk_size(store: Store) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        await delete_missing(store, "players", set(), chunk_size=0)


async def test_update_freshness_merges_datasets(store: Store) -> None:
    await update_freshness(store, ["players"], timestamp="2026-09-16T04:00:03Z")
    await update_freshness(store, ["weekly_stats", "schedules"], timestamp="2026-09-16T09:02:11Z")

    freshness = await get_data_freshness(store)
    assert freshness == {
        "players": "2026-09-16T04:00:03Z",
        "weekly_stats": "2026-09-16T09:02:11Z",
        "schedules": "2026-09-16T09:02:11Z",
    }


async def test_update_freshness_ignores_empty_dataset_lists(store: Store) -> None:
    ts = await update_freshness(store, [])
    assert ISO_Z.match(ts)
    assert await get_data_freshness(store) == {}


def test_structured_formatter_emits_json_with_extra_fields() -> None:
    record = logging.LogRecord("ingest.job", logging.INFO, __file__, 1, "task finished", (), None)
    record.task = "stats"
    record.written = 12

    payload = json.loads(StructuredFormatter().format(record))

    assert payload["severity"] == "INFO"
    assert payload["message"] == "task finished"
    assert payload["logger"] == "ingest.job"
    assert payload["task"] == "stats"
    assert payload["written"] == 12
