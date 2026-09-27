"""ResponseCache: hit, miss, expiry, and key construction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from api.core.store import MemoryStore
from api.data.cache import CACHE_COLLECTION, ResponseCache, cache_key


def test_cache_key_shapes() -> None:
    assert cache_key("sleepers", 3) == "sleepers:w3"
    assert cache_key("trending", 3, "lookback=24") == "trending:w3:lookback=24"
    assert cache_key("report") == "report:wNA"
    assert cache_key("report", None, "x") == "report:wNA:x"


def test_cache_keys_are_distinct_per_week_and_extra() -> None:
    keys = {cache_key("sleepers", 3), cache_key("sleepers", 4), cache_key("sleepers", 3, "compact")}
    assert len(keys) == 3


async def test_miss_on_empty_cache() -> None:
    cache = ResponseCache(MemoryStore())
    assert await cache.get("sleepers:w3") is None


async def test_set_then_hit() -> None:
    cache = ResponseCache(MemoryStore())
    payload = {"verdict": "start him", "picks": [{"name": "A"}]}
    await cache.set("sleepers:w3", payload, 6 * 3600, endpoint="sleepers", week=3)
    assert await cache.get("sleepers:w3") == payload


async def test_hit_is_isolated_from_stored_state() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    payload = {"picks": [{"name": "A"}]}
    await cache.set("k", payload, 60)
    payload["picks"].append({"name": "B"})  # caller mutates after caching
    cached = await cache.get("k")
    assert cached == {"picks": [{"name": "A"}]}


async def test_expired_entry_is_a_miss_and_is_left_for_set_to_replace() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await store.set(
        CACHE_COLLECTION,
        "sleepers:w3",
        {"payload": {"stale": True}, "expires_at": expired_at},
    )
    assert await cache.get("sleepers:w3") is None
    # Not deleted on read: that delete could land on another instance's fresh write.
    assert await store.get(CACHE_COLLECTION, "sleepers:w3") is not None
    await cache.set("sleepers:w3", {"fresh": True}, 3600)
    assert await cache.get("sleepers:w3") == {"fresh": True}


async def test_entry_expiring_exactly_now_is_a_miss() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    await store.set(
        CACHE_COLLECTION, "k", {"payload": {"x": 1}, "expires_at": datetime.now(UTC).isoformat()}
    )
    assert await cache.get("k") is None


async def test_unparseable_expiry_is_a_miss() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    await store.set(CACHE_COLLECTION, "k", {"payload": {"x": 1}, "expires_at": "not-a-date"})
    assert await cache.get("k") is None
    assert await cache.remaining_ttl("k") is None

    await store.set(CACHE_COLLECTION, "k2", {"payload": {"x": 1}})
    assert await cache.get("k2") is None


async def test_naive_timestamps_are_read_as_utc() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    future = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat()
    await store.set(CACHE_COLLECTION, "k", {"payload": {"x": 1}, "expires_at": future})
    assert await cache.get("k") == {"x": 1}


async def test_non_dict_payload_is_a_miss_with_no_usable_life() -> None:
    """A miss that keeps a live TTL is a deadlock, not a miss.

    ``get`` cannot serve this entry, but ``expires_at`` is still in the future.
    If ``remaining_ttl`` reported that life, the warmer would skip
    regeneration while ``ENGINE=adk`` refuses to generate in-request, and every
    caller would 503 until the entry aged out -- up to 12h for ``report``. It
    reports none, so the warmer rebuilds it and ``set`` overwrites it.
    """
    store = MemoryStore()
    cache = ResponseCache(store)
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await store.set(CACHE_COLLECTION, "k", {"payload": "just a string", "expires_at": future})

    assert await cache.remaining_ttl("k") is None, "an unreadable entry has no usable life"
    assert await cache.get("k") is None
    await cache.set("k", {"rebuilt": True}, 3600)
    assert await cache.get("k") == {"rebuilt": True}


async def test_remaining_ttl_still_reports_a_readable_entry() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    await cache.set("warm", {"ok": True}, 3600)

    remaining = await cache.remaining_ttl("warm")
    assert remaining is not None and 0 < remaining <= 3600
    assert await cache.get("warm") == {"ok": True}


async def test_ttl_metadata_is_recorded() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    await cache.set("report:w3", {"x": 1}, 12 * 3600, endpoint="report", week=3)
    doc = await store.get(CACHE_COLLECTION, "report:w3")
    assert doc is not None
    assert doc["ttl_seconds"] == 43200
    assert doc["endpoint"] == "report"
    assert doc["week"] == 3
    created = datetime.fromisoformat(doc["created_at"])
    expires = datetime.fromisoformat(doc["expires_at"])
    assert (expires - created) == timedelta(hours=12)


async def test_nonpositive_ttl_does_not_write() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    await cache.set("k", {"x": 1}, 0)
    await cache.set("k2", {"x": 1}, -5)
    assert await store.get(CACHE_COLLECTION, "k") is None
    assert await store.get(CACHE_COLLECTION, "k2") is None


async def test_set_overwrites_and_delete_evicts() -> None:
    cache = ResponseCache(MemoryStore())
    await cache.set("k", {"v": 1}, 60)
    await cache.set("k", {"v": 2}, 60)
    assert await cache.get("k") == {"v": 2}
    await cache.delete("k")
    assert await cache.get("k") is None
    await cache.delete("k")  # deleting twice is a no-op


async def test_custom_collection_is_namespaced() -> None:
    store = MemoryStore()
    cache = ResponseCache(store, collection="other_cache")
    await cache.set("k", {"v": 1}, 60)
    assert await store.get(CACHE_COLLECTION, "k") is None
    assert await cache.get("k") == {"v": 1}


# -- remaining_ttl --------------------------------------------------------
#
# Cache *warming* asks a different question than serving does: not "is this
# usable?" but "will it still be usable when I next get the chance to look?".


async def test_remaining_ttl_reports_the_time_left() -> None:
    cache = ResponseCache(MemoryStore())
    await cache.set("k", {"v": 1}, 600)

    remaining = await cache.remaining_ttl("k")

    assert remaining is not None
    assert 590 < remaining <= 600


async def test_remaining_ttl_is_none_for_missing_expired_and_unparseable() -> None:
    store = MemoryStore()
    cache = ResponseCache(store)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await store.set(CACHE_COLLECTION, "expired", {"payload": {}, "expires_at": past})
    await store.set(CACHE_COLLECTION, "junk", {"payload": {}, "expires_at": "not-a-date"})

    assert await cache.remaining_ttl("absent") is None
    assert await cache.remaining_ttl("expired") is None
    assert await cache.remaining_ttl("junk") is None


async def test_remaining_ttl_does_not_evict() -> None:
    """Unlike ``get``: the warmer asks about entries that are still being served.

    Evicting on the question would open the very cold window the caller is
    trying to close — and it takes minutes to refill.
    """
    store = MemoryStore()
    cache = ResponseCache(store)
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await store.set(CACHE_COLLECTION, "expired", {"payload": {"v": 1}, "expires_at": past})
    await cache.set("live", {"v": 1}, 600)

    await cache.remaining_ttl("expired")
    await cache.remaining_ttl("live")

    assert await store.get(CACHE_COLLECTION, "expired") is not None
    assert await cache.get("live") == {"v": 1}


class _RacingStore(MemoryStore):
    """Another instance writes a fresh entry between this read and anything after it."""

    def __init__(self) -> None:
        super().__init__()
        self.race: dict[str, Any] | None = None

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = await super().get(collection, doc_id)
        if self.race is not None:
            await super().set(collection, doc_id, self.race)
            self.race = None
        return doc


async def test_reading_an_expired_entry_never_deletes_a_concurrent_fresh_write() -> None:
    """Read-then-delete would evict the other instance's brand-new generation."""
    store = _RacingStore()
    cache = ResponseCache(store)
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    fresh_at = (datetime.now(UTC) + timedelta(hours=6)).isoformat()
    for shape in (
        {"payload": {"stale": True}, "expires_at": expired_at},
        {"payload": {"stale": True}, "expires_at": "not-a-date"},
        {"payload": "not a dict", "expires_at": fresh_at},
    ):
        await store.set(CACHE_COLLECTION, "trending:w3", shape)
        store.race = {"payload": {"fresh": True}, "expires_at": fresh_at}
        assert await cache.get("trending:w3") is None
        assert await cache.get("trending:w3") == {"fresh": True}, shape
