"""Sleeper trending add/drop refresh (every 30 minutes, tech spec §4.1).

Writes ``trending/{add|drop}`` in the shape :mod:`api.data.stats_store`
documents. Sleeper returns only ``{player_id, count}``; the identity fields
(``name``, ``position``, ``team``) are joined from the ingested ``players``
collection so the free preview and the paid ``/v1/trending`` never have to hit
Sleeper on the request path. A player missing from ``players/`` (added to
Sleeper since the last nightly sync) still gets an entry — id and count only,
counted in the ``unjoined`` log field.
"""

from __future__ import annotations

import logging
from typing import Any

from api.core.store import Store
from api.data.sleeper import SleeperClient, TrendKind
from api.data.stats_store import PLAYERS_COLLECTION, TRENDING_COLLECTION
from ingest.common import update_freshness, utc_now_iso

logger = logging.getLogger(__name__)

#: Sleeper trending window and board size (PRD §4.2: top 25 for the paid board).
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_LIMIT = 25

#: The two boards Sleeper publishes.
TREND_KINDS: tuple[TrendKind, ...] = ("add", "drop")


async def build_trending_doc(
    store: Store,
    kind: str,
    entries: list[dict[str, Any]],
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Join identity fields onto raw ``{player_id, count}`` rows.

    Args:
        store: Store holding the ingested ``players`` collection.
        kind: ``"add"`` or ``"drop"``.
        entries: Raw rows, Sleeper's ordering preserved.
        lookback_hours: Window the counts cover; recorded on the document.
        fetched_at: Timestamp override (testing).

    Returns:
        The ``trending/{kind}`` document.
    """
    joined: list[dict[str, Any]] = []
    unjoined = 0
    for entry in entries:
        player_id = str(entry.get("player_id", "")).strip()
        if not player_id:
            continue
        row: dict[str, Any] = {"player_id": player_id, "count": int(entry.get("count", 0) or 0)}
        player = await store.get(PLAYERS_COLLECTION, player_id)
        if player:
            row["name"] = player.get("name")
            row["position"] = player.get("position")
            row["team"] = player.get("team")
        else:
            unjoined += 1
        joined.append(row)
    if unjoined:
        logger.info(
            "trending entries with no players/ document",
            extra={"kind": kind, "unjoined": unjoined, "entries": len(joined)},
        )
    return {
        "kind": kind,
        "lookback_hours": lookback_hours,
        "fetched_at": fetched_at or utc_now_iso(),
        "entries": joined,
    }


async def refresh_trending(
    store: Store,
    *,
    client: SleeperClient | None = None,
    boards: dict[str, list[dict[str, Any]]] | None = None,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, int]:
    """Refresh both trending boards.

    Args:
        store: Destination store.
        client: Sleeper client. Constructed (and closed) here when omitted and
            ``boards`` is not supplied.
        boards: Pre-fetched ``{kind: [{player_id, count}, ...]}`` — the
            injection point for offline tests.
        lookback_hours: Sleeper trending window.
        limit: Rows per board.

    Returns:
        ``{kind: entry_count}``.
    """
    if boards is None:
        owned = client is None
        sleeper = client or SleeperClient()
        try:
            boards = {
                kind: [
                    row.model_dump()
                    for row in await sleeper.get_trending(
                        kind, lookback_hours=lookback_hours, limit=limit
                    )
                ]
                for kind in TREND_KINDS
            }
        finally:
            if owned:
                await sleeper.aclose()

    if not (boards.get("add") or []):
        # Sleeper's add board is never legitimately empty; an empty 200 is an
        # upstream hiccup. Writing it would stamp `trending` fresh over nothing,
        # and every board ranked on it would answer from an empty list.
        raise ValueError(
            "Sleeper returned an empty trending add board; keeping the previous boards"
        )

    counts: dict[str, int] = {}
    for kind, entries in boards.items():
        doc = await build_trending_doc(
            store, kind, list(entries or []), lookback_hours=lookback_hours
        )
        await store.set(TRENDING_COLLECTION, kind, doc)
        counts[kind] = len(doc["entries"])

    await update_freshness(store, ["trending"])
    logger.info("trending refresh complete", extra=counts)
    return counts
