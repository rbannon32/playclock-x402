"""Async document store abstraction.

Everything that persists in Play Clock goes through :class:`Store`. It
mirrors the exact subset of Firestore this project uses — get / set / delete /
query / auto-id add / atomic claims — and nothing more, so :class:`FirestoreStore` stays trivial
and :class:`MemoryStore` can be an honest stand-in for tests, CI, local dev and
the eval suite (DESIGN_NOTES §1).

Collection path convention
--------------------------
Collection names are opaque strings. Nested Firestore paths are written with
``/`` separators and **must have an odd number of segments** (collection,
document, collection, ...), e.g.::

    "players"                        -> /players/{doc_id}
    "weekly_stats/2026_1/players"    -> /weekly_stats/2026_1/players/{doc_id}

:class:`MemoryStore` treats the whole string as a flat dict key;
:class:`FirestoreStore` splits on ``/`` and walks
``collection() -> document() -> collection()``. Both therefore address the same
logical location, and callers never need to know which backend is live.

Document shape
--------------
Documents are plain JSON-able dicts. Reads never expose internal storage: every
returned dict is a deep copy, and every dict returned from :meth:`Store.list`
carries its document id under the ``"_id"`` key. ``"_id"`` is *not* persisted as
a field on write — it is projected in on read.
"""

from __future__ import annotations

import abc
import copy
import operator
import uuid
from collections.abc import Callable
from typing import Any

from api.core.config import Settings, get_settings

#: Query operators supported by :meth:`Store.list`. Chosen to match the Firestore
#: operators this project actually needs.
WHERE_OPS: frozenset[str] = frozenset({"==", "<", "<=", ">", ">=", "in", "array_contains"})

#: A single query predicate: ``(field, op, value)``. ``field`` may be a dotted
#: path into nested maps, e.g. ``("usage.snap_pct", ">=", 0.5)``.
Where = tuple[str, str, Any]


class StoreError(RuntimeError):
    """Base class for storage-layer failures."""


class Store(abc.ABC):
    """Async document store.

    Implementations must be safe to share across concurrent requests.
    """

    @abc.abstractmethod
    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        """Return the document at ``collection/doc_id``, or ``None`` if absent.

        The returned dict includes ``"_id"`` and is a deep copy — mutating it
        never affects stored state.
        """

    @abc.abstractmethod
    async def set(
        self, collection: str, doc_id: str, data: dict[str, Any], merge: bool = False
    ) -> None:
        """Write ``data`` to ``collection/doc_id``.

        Args:
            collection: Collection path (see module docstring).
            doc_id: Document id.
            data: JSON-able payload. A ``"_id"`` key, if present, is ignored.
            merge: When ``True``, shallow-merge into any existing document
                (Firestore ``set(..., merge=True)`` semantics: top-level keys
                present in ``data`` are replaced, absent keys are preserved).
                When ``False``, the document is fully replaced.
        """

    @abc.abstractmethod
    async def delete(self, collection: str, doc_id: str) -> None:
        """Delete ``collection/doc_id``. Deleting a missing document is a no-op."""

    @abc.abstractmethod
    async def list(
        self,
        collection: str,
        *,
        where: list[Where] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query a collection.

        Args:
            collection: Collection path.
            where: Conjunctive predicates, each ``(field, op, value)`` with ``op``
                in :data:`WHERE_OPS`. Dotted field paths address nested maps.
            order_by: Field (dotted path allowed) to sort by. **Documents that
                lack the field are excluded from the results**, matching
                Firestore's ``order_by`` semantics — a real gotcha, so
                :class:`MemoryStore` reproduces it deliberately rather than
                being quietly more forgiving than production.
            descending: Reverse the sort order.
            limit: Maximum documents to return, applied after filter+sort.

        Returns:
            Deep-copied documents, each including ``"_id"``.
        """

    @abc.abstractmethod
    async def add(self, collection: str, data: dict[str, Any]) -> str:
        """Write ``data`` under a freshly generated document id and return that id."""

    @abc.abstractmethod
    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        """Atomically create a document, returning ``False`` if it already exists."""

    @abc.abstractmethod
    async def replace_if_revision(
        self,
        collection: str,
        doc_id: str,
        expected_revision: str,
        data: dict[str, Any] | None,
    ) -> bool:
        """Atomically replace/delete a document only when ``revision`` matches."""

    async def close(self) -> None:  # pragma: no cover - trivial default
        """Release backend resources. Safe to call more than once."""
        return None


def _get_path(doc: dict[str, Any], field: str) -> Any:
    """Resolve a dotted ``field`` path within ``doc``; return ``None`` if absent."""
    current: Any = doc
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


#: Ordered comparison operators, resolved by symbol.
_ORDERED_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


def _matches(doc: dict[str, Any], where: list[Where]) -> bool:
    """Return whether ``doc`` satisfies every predicate in ``where``."""
    for field, op, value in where:
        if op not in WHERE_OPS:
            raise ValueError(f"unsupported where operator: {op!r}")
        actual = _get_path(doc, field)
        if op == "==":
            ok = actual == value
        elif op == "in":
            ok = actual in value if isinstance(value, (list, tuple, set)) else False
        elif op == "array_contains":
            ok = isinstance(actual, (list, tuple)) and value in actual
        else:
            # Ordered comparisons: a missing or type-incompatible value never matches.
            if actual is None:
                ok = False
            else:
                try:
                    ok = bool(_ORDERED_OPS[op](actual, value))
                except TypeError:
                    ok = False
        if not ok:
            return False
    return True


class _SortKey:
    """Sort wrapper tolerating heterogeneous types across documents."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __lt__(self, other: _SortKey) -> bool:
        a, b = self.value, other.value
        try:
            return bool(a < b)
        except TypeError:
            return str(a) < str(b)


class MemoryStore(Store):
    """In-process :class:`Store` backed by nested dicts.

    Hermetic and dependency-free: used by tests, local dev and the eval suite.
    Deep-copies on both read and write so callers cannot alias stored state.
    """

    def __init__(self) -> None:
        self._data: dict[str, dict[str, dict[str, Any]]] = {}

    def _collection(self, collection: str) -> dict[str, dict[str, Any]]:
        return self._data.setdefault(collection, {})

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        doc = self._data.get(collection, {}).get(doc_id)
        if doc is None:
            return None
        out = copy.deepcopy(doc)
        out["_id"] = doc_id
        return out

    async def set(
        self, collection: str, doc_id: str, data: dict[str, Any], merge: bool = False
    ) -> None:
        payload = {k: v for k, v in copy.deepcopy(data).items() if k != "_id"}
        bucket = self._collection(collection)
        if merge and doc_id in bucket:
            bucket[doc_id].update(payload)
        else:
            bucket[doc_id] = payload

    async def delete(self, collection: str, doc_id: str) -> None:
        self._data.get(collection, {}).pop(doc_id, None)

    async def list(
        self,
        collection: str,
        *,
        where: list[Where] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for doc_id, doc in self._data.get(collection, {}).items():
            candidate = copy.deepcopy(doc)
            candidate["_id"] = doc_id
            if where and not _matches(candidate, where):
                continue
            docs.append(candidate)

        if order_by is not None:
            # Firestore parity: documents lacking the ordered field drop out.
            docs = [d for d in docs if _get_path(d, order_by) is not None]
            docs.sort(key=lambda d: _SortKey(_get_path(d, order_by)), reverse=descending)
        elif descending:
            docs.reverse()

        if limit is not None:
            docs = docs[:limit]
        return docs

    async def add(self, collection: str, data: dict[str, Any]) -> str:
        doc_id = uuid.uuid4().hex
        await self.set(collection, doc_id, data)
        return doc_id

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        bucket = self._collection(collection)
        if doc_id in bucket:
            return False
        bucket[doc_id] = {k: v for k, v in copy.deepcopy(data).items() if k != "_id"}
        return True

    async def replace_if_revision(
        self,
        collection: str,
        doc_id: str,
        expected_revision: str,
        data: dict[str, Any] | None,
    ) -> bool:
        bucket = self._data.get(collection, {})
        current = bucket.get(doc_id)
        if current is None or current.get("revision") != expected_revision:
            return False
        if data is None:
            bucket.pop(doc_id, None)
        else:
            bucket[doc_id] = {k: v for k, v in copy.deepcopy(data).items() if k != "_id"}
        return True

    def clear(self) -> None:
        """Drop all data. Test convenience; not part of the :class:`Store` contract."""
        self._data.clear()


class FirestoreStore(Store):
    """Thin :class:`Store` wrapper over ``google.cloud.firestore.AsyncClient``.

    Deliberately minimal — the abstraction exists so this class can stay a
    near-passthrough. The Firestore client is imported lazily and constructed on
    first use so that importing this module never requires GCP credentials.
    """

    def __init__(self, project: str | None = None, client: Any | None = None) -> None:
        """Args:
        project: GCP project id. ``None`` defers to ADC / ``GOOGLE_CLOUD_PROJECT``.
        client: Pre-built ``AsyncClient`` (dependency injection / tests).
        """
        self._project = project
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                from google.cloud import firestore  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - dependency always installed
                raise StoreError(
                    "google-cloud-firestore is required for STORE_BACKEND=firestore"
                ) from exc
            self._client = firestore.AsyncClient(project=self._project)
        return self._client

    def _doc_ref(self, collection: str, doc_id: str) -> Any:
        return self._collection_ref(collection).document(doc_id)

    def _collection_ref(self, collection: str) -> Any:
        """Resolve a ``a/b/c`` collection path into a Firestore CollectionReference."""
        segments = [s for s in collection.split("/") if s]
        if len(segments) % 2 == 0:
            raise ValueError(
                f"collection path must have an odd number of segments, got {collection!r}"
            )
        ref = self._get_client().collection(segments[0])
        for i in range(1, len(segments), 2):
            ref = ref.document(segments[i]).collection(segments[i + 1])
        return ref

    async def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snapshot = await self._doc_ref(collection, doc_id).get()
        if not snapshot.exists:
            return None
        data = dict(snapshot.to_dict() or {})
        data["_id"] = snapshot.id
        return data

    async def set(
        self, collection: str, doc_id: str, data: dict[str, Any], merge: bool = False
    ) -> None:
        payload = {k: v for k, v in data.items() if k != "_id"}
        await self._doc_ref(collection, doc_id).set(payload, merge=merge)

    async def delete(self, collection: str, doc_id: str) -> None:
        await self._doc_ref(collection, doc_id).delete()

    async def list(
        self,
        collection: str,
        *,
        where: list[Where] | None = None,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import (  # noqa: PLC0415
            FieldFilter,
        )

        query = self._collection_ref(collection)
        for field, op, value in where or []:
            if op not in WHERE_OPS:
                raise ValueError(f"unsupported where operator: {op!r}")
            query = query.where(filter=FieldFilter(field, op, value))
        if order_by is not None:
            query = query.order_by(order_by, direction="DESCENDING" if descending else "ASCENDING")
        if limit is not None:
            query = query.limit(limit)

        out: list[dict[str, Any]] = []
        async for snapshot in query.stream():
            data = dict(snapshot.to_dict() or {})
            data["_id"] = snapshot.id
            out.append(data)
        return out

    async def add(self, collection: str, data: dict[str, Any]) -> str:
        payload = {k: v for k, v in data.items() if k != "_id"}
        _, ref = await self._collection_ref(collection).add(payload)
        return str(ref.id)

    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        from google.api_core.exceptions import AlreadyExists  # noqa: PLC0415

        payload = {k: v for k, v in data.items() if k != "_id"}
        try:
            await self._doc_ref(collection, doc_id).create(payload)
        except AlreadyExists:
            return False
        return True

    async def replace_if_revision(
        self,
        collection: str,
        doc_id: str,
        expected_revision: str,
        data: dict[str, Any] | None,
    ) -> bool:
        from google.cloud import firestore  # noqa: PLC0415

        ref = self._doc_ref(collection, doc_id)
        transaction = self._get_client().transaction()
        payload = None if data is None else {k: v for k, v in data.items() if k != "_id"}

        @firestore.async_transactional
        async def replace(active_transaction: Any) -> bool:
            snapshot = await ref.get(transaction=active_transaction)
            current = dict(snapshot.to_dict() or {}) if snapshot.exists else {}
            if current.get("revision") != expected_revision:
                return False
            if payload is None:
                active_transaction.delete(ref)
            else:
                active_transaction.set(ref, payload)
            return True

        return bool(await replace(transaction))

    async def close(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            self._client.close()


_store_override: Store | None = None
_default_store: Store | None = None


def get_store(settings: Settings | None = None) -> Store:
    """Return the process-wide :class:`Store`.

    Honours :func:`set_store` overrides first (tests / DI), otherwise builds the
    backend named by ``Settings.store_backend`` once and reuses it.
    """
    if _store_override is not None:
        return _store_override

    global _default_store
    if _default_store is None:
        settings = settings or get_settings()
        if settings.store_backend == "firestore":
            _default_store = FirestoreStore(project=settings.google_cloud_project)
        else:
            _default_store = MemoryStore()
    return _default_store


def set_store(store: Store | None) -> None:
    """Override the store returned by :func:`get_store`.

    Pass ``None`` to clear the override and fall back to settings-driven
    construction. Intended for tests and dependency injection.
    """
    global _store_override, _default_store
    _store_override = store
    if store is None:
        _default_store = None
