"""Response cache for week-scoped paid endpoints.

Tech spec §6: ``/v1/trending`` is generated on the first paid call of the cycle
and re-served for 6h; ``/v1/sleepers``, ``/v1/waivers`` and ``/v1/report`` are
cached 12h. That is what keeps unit economics good — 100 payers of
``/v1/sleepers`` cost one LLM run. Personalized endpoints (``player``, ``matchup``, ``roster``,
``team-report``) are never cached here.

TTL policy lives with the **callers** (routes), not here: :meth:`ResponseCache.set`
takes an explicit ``ttl_seconds``.

Storage: one document per key in ``response_cache``::

    {
      "_id": "sleepers:w3",
      "endpoint": "sleepers",
      "week": 3,
      "payload": { ...the response body... },
      "created_at": "2026-09-16T13:00:00Z",
      "expires_at": "2026-09-16T19:00:00Z",   # absolute, ISO-8601 UTC
      "ttl_seconds": 21600
    }

Expiry is read-side only: an expired or unreadable document is treated as a
miss and left where it is. Reads never delete, because a read-then-delete races
another instance's fresh write to the same key — the delete would land on the
new entry and throw away a paid-for generation. No sweeper is needed either:
keys are one per board per week, and the regeneration that follows a miss
writes the same key with :meth:`ResponseCache.set`, fully replacing the stale
document.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from api.core.store import Store

logger = logging.getLogger(__name__)

#: Collection holding cached response bodies.
CACHE_COLLECTION = "response_cache"


def cache_key(endpoint: str, week: int | None = None, extra: str = "") -> str:
    """Build a stable cache key.

    Format: ``"{endpoint}:w{week}"`` with ``":{extra}"`` appended when ``extra``
    is non-empty; ``week=None`` renders as ``"wNA"``.

    Args:
        endpoint: Endpoint key, e.g. ``"sleepers"`` (see
            :data:`api.core.config.ENDPOINT_KEYS`).
        week: NFL week the response is scoped to.
        extra: Any further discriminator that changes the body — a format
            variant, a lookback window. Keep it short and deterministic;
            callers must sort/normalize before passing it in.

    Returns:
        A key safe to use as a Firestore document id.

    Examples:
        >>> cache_key("sleepers", 3)
        'sleepers:w3'
        >>> cache_key("trending", 3, "lookback=24")
        'trending:w3:lookback=24'
    """
    base = f"{endpoint}:w{week if week is not None else 'NA'}"
    return f"{base}:{extra}" if extra else base


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a timezone-aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class ResponseCache:
    """TTL cache for generated response bodies, backed by a :class:`Store`."""

    def __init__(self, store: Store, collection: str = CACHE_COLLECTION) -> None:
        """Args:
        store: Backing store.
        collection: Override the cache collection (tests, namespacing).
        """
        self._store = store
        self._collection = collection

    async def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached payload for ``key``, or ``None`` on miss/expiry.

        Never deletes (see the module docstring): an expired, unparseable or
        non-dict entry is simply a miss, and the regeneration it causes
        overwrites it. :meth:`remaining_ttl` reports the same three shapes as
        having no usable life, so the warmer rebuilds them rather than
        skipping an entry no caller can read.
        """
        doc = await self._store.get(self._collection, key)
        if not doc:
            return None

        expires_at = _parse_iso(doc.get("expires_at"))
        if expires_at is None:
            logger.warning("cache entry %s has no parseable expires_at; treating as miss", key)
            return None
        if expires_at <= datetime.now(UTC):
            return None

        payload = doc.get("payload")
        if not isinstance(payload, dict):
            logger.warning("cache entry %s has a non-dict payload; treating as miss", key)
            return None
        return payload

    async def remaining_ttl(self, key: str) -> float | None:
        """Return how many seconds of life ``key``'s entry has left.

        ``None`` means "there is nothing usable here": no document, an
        unparseable ``expires_at``, an entry that has already expired, or a
        payload :meth:`get` could not serve. That last case matters to the
        warmer specifically: reporting life for an entry no caller can read
        is how a board stays cold for its whole TTL while the warmer skips it.

        Unlike :meth:`get` this never evicts. It exists for cache *warming*,
        which needs to know an entry is about to die while it is still being
        served — evicting it there would open the cold window the warmer is
        trying to close.
        """
        doc = await self._store.get(self._collection, key)
        if not doc:
            return None
        expires_at = _parse_iso(doc.get("expires_at"))
        if expires_at is None:
            return None
        if not isinstance(doc.get("payload"), dict):
            return None
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        return remaining if remaining > 0 else None

    async def set(
        self,
        key: str,
        payload: dict[str, Any],
        ttl_seconds: int,
        *,
        endpoint: str | None = None,
        week: int | None = None,
    ) -> None:
        """Cache ``payload`` under ``key`` for ``ttl_seconds``.

        Args:
            key: Key from :func:`cache_key`.
            payload: JSON-able response body. Pydantic models must be dumped
                first (``model_dump(mode="json")``) so the store holds plain data.
            ttl_seconds: Lifetime. Must be positive; ``<= 0`` is a no-op with a
                warning, so a misconfigured TTL never writes an entry that is
                born expired.
            endpoint: Optional endpoint key, stored for observability.
            week: Optional week, stored for observability.
        """
        if ttl_seconds <= 0:
            logger.warning("refusing to cache %s with ttl_seconds=%s", key, ttl_seconds)
            return
        now = datetime.now(UTC)
        await self._store.set(
            self._collection,
            key,
            {
                "endpoint": endpoint,
                "week": week,
                "payload": payload,
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
                "ttl_seconds": ttl_seconds,
            },
        )

    async def delete(self, key: str) -> None:
        """Evict ``key`` immediately. Used by cache-warming and manual invalidation."""
        await self._store.delete(self._collection, key)
