"""Shared ingest plumbing: structured logging, chunked writes, freshness stamps.

Kept deliberately tiny — the interesting code is in the per-source modules.
Everything persists through the :class:`~api.core.store.Store` seam so the whole
job runs against :class:`~api.core.store.MemoryStore` offline (DESIGN_NOTES §1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Collection, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from api.core.store import Store
from api.data.stats_store import FRESHNESS_DOC_ID, META_COLLECTION

logger = logging.getLogger(__name__)

#: Documents written per ``asyncio.gather`` batch. Firestore tolerates far more
#: concurrency than this, but a bounded fan-out keeps memory flat on the ~2k
#: player docs and avoids hammering the emulator/local store during tests.
WRITE_CHUNK_SIZE = 100

#: Fields ``logging`` puts on every record; anything else an ingest call site
#: attaches via ``extra=`` is treated as structured payload.
_STD_LOG_FIELDS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def utc_now_iso() -> str:
    """Return the current UTC time as ``2026-09-16T09:02:11Z``.

    Second precision, ``Z`` suffix — the exact format ``meta/freshness`` and the
    ``trending`` documents are specified in (:mod:`api.data.stats_store`).
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class StructuredFormatter(logging.Formatter):
    """Emit one JSON object per log line, Cloud Logging style.

    ``severity``/``message`` are the fields Cloud Logging promotes; any keyword
    passed through ``extra=`` lands alongside them, so ingest counters are
    queryable (``jsonPayload.written > 0``) instead of buried in prose.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat().replace("+00:00", "Z"),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_LOG_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install :class:`StructuredFormatter` on the root logger (idempotent)."""
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())


async def write_docs(
    store: Store,
    collection: str,
    docs: Sequence[tuple[str, dict[str, Any]]],
    *,
    merge: bool = False,
    chunk_size: int = WRITE_CHUNK_SIZE,
) -> int:
    """Write ``(doc_id, payload)`` pairs to ``collection`` in bounded batches.

    Args:
        store: Backing store.
        collection: Collection path (see :mod:`api.core.store`).
        docs: Pairs to write. Empty is a no-op.
        merge: Shallow-merge into existing documents instead of replacing.
        chunk_size: Documents written concurrently per batch.

    Returns:
        Number of documents written.
    """
    if not docs:
        return 0
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    for start in range(0, len(docs), chunk_size):
        chunk = docs[start : start + chunk_size]
        await asyncio.gather(
            *(store.set(collection, doc_id, payload, merge=merge) for doc_id, payload in chunk)
        )
    logger.debug("wrote documents", extra={"collection": collection, "written": len(docs)})
    return len(docs)


async def delete_missing(
    store: Store,
    collection: str,
    keep: Collection[str],
    *,
    chunk_size: int = WRITE_CHUNK_SIZE,
) -> int:
    """Delete every document in ``collection`` whose id is not in ``keep``.

    The other half of an upsert-only sync: without it a document that drops out
    of the source (a retired player, a name whose last candidate left) lives in
    the store forever and keeps surfacing in lookups.

    Scans the collection to learn which ids exist, so call it once per sync, not
    per document. Deletes are chunked with the same bounded fan-out as
    :func:`write_docs`.

    Args:
        store: Backing store.
        collection: Collection path (see :mod:`api.core.store`).
        keep: Document ids the new snapshot still contains.
        chunk_size: Documents deleted concurrently per batch.

    Returns:
        Number of documents deleted.
    """
    if chunk_size < 1:
        raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
    survivors = set(keep)
    stale = sorted(
        doc_id
        for doc in await store.list(collection)
        if (doc_id := str(doc.get("_id") or "")) and doc_id not in survivors
    )
    for start in range(0, len(stale), chunk_size):
        chunk = stale[start : start + chunk_size]
        await asyncio.gather(*(store.delete(collection, doc_id) for doc_id in chunk))
    if stale:
        logger.info("pruned documents", extra={"collection": collection, "deleted": len(stale)})
    return len(stale)


async def update_freshness(
    store: Store, datasets: Iterable[str], *, timestamp: str | None = None
) -> str:
    """Stamp ``meta/freshness`` with ``{dataset: timestamp}`` for each dataset.

    Merged, never replaced, so a task only touches the datasets it wrote.

    Returns:
        The timestamp written.
    """
    ts = timestamp or utc_now_iso()
    names = sorted({d for d in datasets if d})
    if not names:
        return ts
    await store.set(META_COLLECTION, FRESHNESS_DOC_ID, {name: ts for name in names}, merge=True)
    logger.info("freshness updated", extra={"datasets": names, "timestamp": ts})
    return ts
