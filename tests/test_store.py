"""MemoryStore behaviour, plus the store factory/override seam."""

from __future__ import annotations

import pytest

from api.core.config import Settings
from api.core.store import (
    FirestoreStore,
    MemoryStore,
    get_store,
    set_store,
)


@pytest.fixture
def mem() -> MemoryStore:
    return MemoryStore()


async def test_set_and_get_roundtrip(mem: MemoryStore) -> None:
    await mem.set("players", "4046", {"name": "Patrick Mahomes", "position": "QB"})
    doc = await mem.get("players", "4046")
    assert doc == {"_id": "4046", "name": "Patrick Mahomes", "position": "QB"}


async def test_get_missing_returns_none(mem: MemoryStore) -> None:
    assert await mem.get("players", "nope") is None
    assert await mem.get("no_such_collection", "x") is None


async def test_set_replaces_by_default(mem: MemoryStore) -> None:
    await mem.set("players", "1", {"a": 1, "b": 2})
    await mem.set("players", "1", {"a": 9})
    doc = await mem.get("players", "1")
    assert doc is not None
    assert "b" not in doc
    assert doc["a"] == 9


async def test_set_merge_preserves_absent_keys(mem: MemoryStore) -> None:
    await mem.set("players", "1", {"a": 1, "b": 2})
    await mem.set("players", "1", {"a": 9, "c": 3}, merge=True)
    doc = await mem.get("players", "1")
    assert doc == {"_id": "1", "a": 9, "b": 2, "c": 3}


async def test_merge_on_missing_doc_creates_it(mem: MemoryStore) -> None:
    await mem.set("players", "new", {"a": 1}, merge=True)
    assert (await mem.get("players", "new")) == {"_id": "new", "a": 1}


async def test_id_field_is_not_persisted(mem: MemoryStore) -> None:
    await mem.set("players", "1", {"_id": "bogus", "name": "x"})
    doc = await mem.get("players", "1")
    assert doc is not None
    assert doc["_id"] == "1"


async def test_delete(mem: MemoryStore) -> None:
    await mem.set("players", "1", {"a": 1})
    await mem.delete("players", "1")
    assert await mem.get("players", "1") is None
    await mem.delete("players", "1")  # deleting twice is a no-op
    await mem.delete("nothing", "1")


async def test_add_generates_unique_ids(mem: MemoryStore) -> None:
    a = await mem.add("receipts", {"amount": 0.10})
    b = await mem.add("receipts", {"amount": 0.25})
    assert a != b
    doc = await mem.get("receipts", a)
    assert doc is not None and doc["amount"] == 0.10
    assert len(await mem.list("receipts")) == 2


async def test_create_is_atomic_and_never_overwrites(mem: MemoryStore) -> None:
    assert await mem.create("claims", "one", {"revision": "a", "owner": "first"}) is True
    assert await mem.create("claims", "one", {"revision": "b", "owner": "second"}) is False
    assert await mem.get("claims", "one") == {
        "_id": "one",
        "revision": "a",
        "owner": "first",
    }


async def test_replace_if_revision_is_compare_and_swap(mem: MemoryStore) -> None:
    await mem.set("claims", "one", {"revision": "a", "status": "claimed"})
    assert (
        await mem.replace_if_revision(
            "claims", "one", "wrong", {"revision": "b", "status": "settled"}
        )
        is False
    )
    assert (
        await mem.replace_if_revision("claims", "one", "a", {"revision": "b", "status": "settled"})
        is True
    )
    assert (await mem.get("claims", "one"))["status"] == "settled"  # type: ignore[index]
    assert await mem.replace_if_revision("claims", "one", "b", None) is True
    assert await mem.get("claims", "one") is None


async def test_deep_copy_isolation_on_write(mem: MemoryStore) -> None:
    payload = {"nested": {"k": [1, 2]}}
    await mem.set("c", "1", payload)
    payload["nested"]["k"].append(3)
    doc = await mem.get("c", "1")
    assert doc is not None
    assert doc["nested"]["k"] == [1, 2]


async def test_deep_copy_isolation_on_read(mem: MemoryStore) -> None:
    await mem.set("c", "1", {"nested": {"k": [1, 2]}})
    first = await mem.get("c", "1")
    assert first is not None
    first["nested"]["k"].append(99)
    second = await mem.get("c", "1")
    assert second is not None
    assert second["nested"]["k"] == [1, 2]


async def test_list_deep_copy_isolation(mem: MemoryStore) -> None:
    await mem.set("c", "1", {"nested": {"k": 1}})
    rows = await mem.list("c")
    rows[0]["nested"]["k"] = 999
    again = await mem.list("c")
    assert again[0]["nested"]["k"] == 1


async def _seed(mem: MemoryStore) -> None:
    await mem.set(
        "p", "a", {"pos": "RB", "pts": 20.5, "tags": ["hot", "rb1"], "usage": {"snap": 0.9}}
    )
    await mem.set("p", "b", {"pos": "WR", "pts": 12.0, "tags": ["cold"], "usage": {"snap": 0.5}})
    await mem.set("p", "c", {"pos": "RB", "pts": 30.1, "tags": ["hot"], "usage": {"snap": 0.7}})
    await mem.set("p", "d", {"pos": "TE"})  # missing pts/usage on purpose


async def test_list_all(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p")
    assert len(rows) == 4
    assert all("_id" in r for r in rows)


async def test_list_where_eq(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", where=[("pos", "==", "RB")])
    assert {r["_id"] for r in rows} == {"a", "c"}


async def test_list_where_comparisons(mem: MemoryStore) -> None:
    await _seed(mem)
    assert {r["_id"] for r in await mem.list("p", where=[("pts", ">", 15)])} == {"a", "c"}
    assert {r["_id"] for r in await mem.list("p", where=[("pts", ">=", 20.5)])} == {"a", "c"}
    assert {r["_id"] for r in await mem.list("p", where=[("pts", "<", 20.5)])} == {"b"}
    assert {r["_id"] for r in await mem.list("p", where=[("pts", "<=", 20.5)])} == {"a", "b"}


async def test_list_where_in_and_array_contains(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", where=[("pos", "in", ["WR", "TE"])])
    assert {r["_id"] for r in rows} == {"b", "d"}
    rows = await mem.list("p", where=[("tags", "array_contains", "hot")])
    assert {r["_id"] for r in rows} == {"a", "c"}


async def test_list_where_nested_field_path(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", where=[("usage.snap", ">=", 0.7)])
    assert {r["_id"] for r in rows} == {"a", "c"}


async def test_list_where_conjunction(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", where=[("pos", "==", "RB"), ("pts", ">", 25)])
    assert [r["_id"] for r in rows] == ["c"]


async def test_list_missing_field_never_matches_comparison(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", where=[("pts", ">", 0)])
    assert "d" not in {r["_id"] for r in rows}


async def test_list_rejects_unknown_operator(mem: MemoryStore) -> None:
    await _seed(mem)
    with pytest.raises(ValueError):
        await mem.list("p", where=[("pts", "!=", 1)])


async def test_list_order_by_and_descending(mem: MemoryStore) -> None:
    await _seed(mem)
    asc = await mem.list("p", where=[("pos", "in", ["RB", "WR"])], order_by="pts")
    assert [r["_id"] for r in asc] == ["b", "a", "c"]
    desc = await mem.list("p", where=[("pos", "in", ["RB", "WR"])], order_by="pts", descending=True)
    assert [r["_id"] for r in desc] == ["c", "a", "b"]


async def test_list_order_by_excludes_docs_missing_the_field(mem: MemoryStore) -> None:
    """Firestore parity: order_by drops documents that lack the field."""
    await _seed(mem)
    rows = await mem.list("p", order_by="pts")
    assert [r["_id"] for r in rows] == ["b", "a", "c"]  # 'd' has no pts
    assert [r["_id"] for r in await mem.list("p", order_by="usage.snap")] == ["b", "c", "a"]


async def test_list_limit_applies_after_sort(mem: MemoryStore) -> None:
    await _seed(mem)
    rows = await mem.list("p", order_by="pts", descending=True, limit=2)
    assert [r["_id"] for r in rows] == ["c", "a"]
    assert await mem.list("p", limit=0) == []


async def test_nested_collection_paths_are_distinct(mem: MemoryStore) -> None:
    await mem.set("weekly_stats/2026_1/players", "4046", {"pts": 10})
    await mem.set("weekly_stats/2026_2/players", "4046", {"pts": 20})
    w1 = await mem.get("weekly_stats/2026_1/players", "4046")
    w2 = await mem.get("weekly_stats/2026_2/players", "4046")
    assert w1 is not None and w1["pts"] == 10
    assert w2 is not None and w2["pts"] == 20


async def test_clear(mem: MemoryStore) -> None:
    await mem.set("p", "1", {"a": 1})
    mem.clear()
    assert await mem.list("p") == []


def test_get_store_defaults_to_memory() -> None:
    set_store(None)
    try:
        store = get_store(Settings(_env_file=None, store_backend="memory"))  # type: ignore[call-arg]
        assert isinstance(store, MemoryStore)
        assert get_store() is store  # memoized
    finally:
        set_store(None)


def test_get_store_firestore_backend_does_not_need_creds() -> None:
    """Construction is lazy: no client is built until a call is made."""
    set_store(None)
    try:
        settings = Settings(_env_file=None, store_backend="firestore", google_cloud_project="p")  # type: ignore[call-arg]
        store = get_store(settings)
        assert isinstance(store, FirestoreStore)
    finally:
        set_store(None)


def test_set_store_override_wins() -> None:
    fake = MemoryStore()
    set_store(fake)
    try:
        assert get_store() is fake
    finally:
        set_store(None)
    assert get_store() is not fake


def test_firestore_collection_path_validation() -> None:
    store = FirestoreStore(client=object())
    with pytest.raises(ValueError):
        store._collection_ref("weekly_stats/2026_1")
