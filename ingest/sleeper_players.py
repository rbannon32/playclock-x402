"""Nightly Sleeper player-dump sync.

Fetches ``GET /players/nfl`` once (a ~5MB, ~11k-entry blob — tech spec §4.1 says
at most once a day) and derives three collections:

``players/{player_id}``
    The trimmed player document specified in :mod:`api.data.stats_store`, plus
    the extra fields the analysis wave asked for (fantasy positions, depth chart
    slot, injury detail, search rank, news timestamp, cross-ids).

``player_index/{normalize_name(name)}``
    Name -> candidates, so ``resolve_player("josh allen")`` legitimately returns
    both the QB and the LB. Doc ids come from
    :func:`api.data.stats_store.normalize_name` — never re-derived here.

``id_map/{gsis_id}``
    ``{"sleeper_id", "espn_id", "name"}``. This is the join nflverse ingest needs
    (nflverse keys on ``gsis_id``, everything user-facing keys on the Sleeper
    id). Sleeper carries ``gsis_id`` as a string and ``espn_id`` as an **int**,
    and both are null for a large minority of players — missing cross-ids are a
    logged count, never an error (tech spec §4.2). ``espn_id`` is stringified on
    the way in so downstream code never has to care.

The filter
----------
Only players that are ``active`` *and* hold a fantasy-relevant position
(:data:`FANTASY_POSITIONS`) are written. That takes ~11k Sleeper entries down to
~1.5-2k docs, which keeps Firestore document counts, nightly write cost and the
``player_index`` fan-out sane. Practice-squad/retired/off-roster players and
non-fantasy positions (OL, LS, DL, LB, DB...) are dropped; a player who is
rostered but injured stays, because Sleeper keeps ``active=true`` and moves the
detail into ``status``/``injury_status``.

Each run is a full snapshot, not an accumulation: whatever the filter no longer
keeps is deleted from all three collections (:func:`prune_to_snapshot`), so a
player who retires stops resolving by name instead of lingering in lookups and
candidate scans for the rest of the season.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from api.core.store import Store
from api.data.sleeper import SleeperClient
from api.data.stats_store import (
    PLAYER_INDEX_COLLECTION,
    PLAYERS_COLLECTION,
    normalize_name,
)
from ingest.common import delete_missing, update_freshness, write_docs

# The live endpoint normally yields roughly 1.5–2k eligible fantasy players.
# Refuse an implausibly small owned fetch before it can prune a healthy store.
MIN_LIVE_SNAPSHOT_PLAYERS = 500

logger = logging.getLogger(__name__)

#: Collection holding the nflverse ``gsis_id`` -> Sleeper id join.
ID_MAP_COLLECTION = "id_map"

#: Positions kept by the nightly filter. ``DEF`` entries are team defenses whose
#: Sleeper ``player_id`` is the team abbreviation (e.g. ``"KC"``).
FANTASY_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})

#: Scalar fields copied straight from the Sleeper player object when present.
_COPIED_FIELDS: tuple[str, ...] = (
    "first_name",
    "last_name",
    "team",
    "status",
    "active",
    "injury_status",
    "injury_body_part",
    "injury_notes",
    "injury_start_date",
    "depth_chart_order",
    "depth_chart_position",
    "search_rank",
    "age",
    "years_exp",
    "news_updated",
    "number",
)

#: Cross-ids kept on every player document, all coerced to ``str | None``.
_CROSS_ID_FIELDS: tuple[str, ...] = (
    "gsis_id",
    "espn_id",
    "yahoo_id",
    "rotowire_id",
    "sportradar_id",
)

#: Sort key for an unranked player — Sleeper's ``search_rank`` is "lower is more
#: prominent", and missing means "not prominent at all".
_UNRANKED = 10**9


def _clean_id(value: Any) -> str | None:
    """Coerce a Sleeper cross-id to a non-empty string, or ``None``.

    Sleeper types these inconsistently (``espn_id`` is an int, ``gsis_id`` a
    string, both are frequently ``null``), so everything downstream sees a
    string.
    """
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def player_position(raw: dict[str, Any]) -> str | None:
    """Return the player's primary fantasy position, falling back to the list.

    Sleeper leaves ``position`` null for a few entries that still carry a usable
    ``fantasy_positions`` array.
    """
    position = raw.get("position")
    if isinstance(position, str) and position.strip():
        return position.strip().upper()
    for candidate in raw.get("fantasy_positions") or []:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip().upper()
    return None


def player_name(player_id: str, raw: dict[str, Any]) -> str:
    """Return the display name, assembling it from parts when needed.

    Team defenses have no ``full_name`` (``{"first_name": "Kansas City",
    "last_name": "Chiefs"}``), so join the parts; fall back to the id rather than
    writing an empty name.
    """
    full = raw.get("full_name")
    if isinstance(full, str) and full.strip():
        return full.strip()
    parts = [
        str(raw.get(key)).strip()
        for key in ("first_name", "last_name")
        if isinstance(raw.get(key), str) and str(raw.get(key)).strip()
    ]
    return " ".join(parts) if parts else player_id


def is_fantasy_relevant(raw: dict[str, Any]) -> bool:
    """Return whether this Sleeper entry survives the nightly filter."""
    return bool(raw.get("active")) and player_position(raw) in FANTASY_POSITIONS


def build_player_doc(player_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Build one ``players/{player_id}`` document.

    Shape is the one documented in :mod:`api.data.stats_store`: ``player_id``,
    ``name``, ``search_name``, ``position``, ``team``, ``status``,
    ``injury_status``, ``gsis_id``, ``espn_id``, ``years_exp`` are always
    present; the remaining Sleeper fields are copied only when non-null.
    """
    name = player_name(player_id, raw)
    doc: dict[str, Any] = {
        "player_id": player_id,
        "name": name,
        "search_name": normalize_name(name),
        "position": player_position(raw),
        "team": raw.get("team"),
        "status": raw.get("status"),
        "injury_status": raw.get("injury_status"),
        "years_exp": raw.get("years_exp"),
        "fantasy_positions": list(raw.get("fantasy_positions") or []),
    }
    for field in _COPIED_FIELDS:
        value = raw.get(field)
        if value is not None and field not in doc:
            doc[field] = value
    for field in _CROSS_ID_FIELDS:
        doc[field] = _clean_id(raw.get(field))
    return doc


def select_players(dump: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter and transform the raw Sleeper dump into ``players/`` documents.

    Args:
        dump: ``{player_id: player_object}`` exactly as Sleeper returns it.

    Returns:
        Player documents, sorted by ``player_id`` for deterministic writes.
    """
    docs: list[dict[str, Any]] = []
    skipped = 0
    for player_id, raw in (dump or {}).items():
        if not isinstance(raw, dict):
            skipped += 1
            continue
        if not is_fantasy_relevant(raw):
            skipped += 1
            continue
        docs.append(build_player_doc(str(player_id), raw))
    docs.sort(key=lambda d: str(d["player_id"]))
    logger.info(
        "filtered sleeper dump",
        extra={"kept": len(docs), "skipped": skipped, "total": len(dump or {})},
    )
    return docs


def build_player_index(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Build ``player_index/{normalized_name}`` documents from player docs.

    Candidates for one name are ordered by Sleeper ``search_rank`` (most
    prominent first) so a caller taking ``candidates[0]`` gets the player a human
    almost certainly meant.

    Returns:
        ``(doc_id, {"candidates": [...]})`` pairs.
    """
    buckets: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for doc in docs:
        key = doc.get("search_name") or normalize_name(str(doc.get("name", "")))
        if not key:
            continue
        rank = doc.get("search_rank")
        buckets.setdefault(key, []).append(
            (
                _UNRANKED if rank is None else int(rank),
                {
                    # Exactly the candidate shape stats_store documents — prominence
                    # is carried by the ordering, not by an extra field.
                    "player_id": doc["player_id"],
                    "name": doc["name"],
                    "team": doc.get("team"),
                    "position": doc.get("position"),
                },
            )
        )
    out: list[tuple[str, dict[str, Any]]] = []
    for key, ranked in sorted(buckets.items()):
        ranked.sort(key=lambda item: (item[0], item[1]["player_id"]))
        out.append((key, {"candidates": [candidate for _, candidate in ranked]}))
    ambiguous = sum(1 for _, doc in out if len(doc["candidates"]) > 1)
    logger.info("built player index", extra={"names": len(out), "ambiguous": ambiguous})
    return out


def build_id_map(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Build ``id_map/{gsis_id}`` documents from player docs.

    Players without a ``gsis_id`` (team defenses, undrafted rookies Sleeper has
    not reconciled yet) simply produce no entry — counted at ``debug``, never an
    error. ``espn_id`` may be ``None`` in an otherwise valid entry.

    Returns:
        ``(gsis_id, {"gsis_id", "sleeper_id", "espn_id", "name"})`` pairs.
    """
    out: dict[str, dict[str, Any]] = {}
    no_gsis = 0
    no_espn = 0
    collisions = 0
    for doc in docs:
        gsis_id = _clean_id(doc.get("gsis_id"))
        if not gsis_id:
            no_gsis += 1
            continue
        espn_id = _clean_id(doc.get("espn_id"))
        if not espn_id:
            no_espn += 1
        if gsis_id in out:
            collisions += 1
        out[gsis_id] = {
            "gsis_id": gsis_id,
            "sleeper_id": doc["player_id"],
            "espn_id": espn_id,
            "name": doc["name"],
        }
    logger.debug(
        "built id map",
        extra={
            "mapped": len(out),
            "missing_gsis_id": no_gsis,
            "missing_espn_id": no_espn,
            "gsis_collisions": collisions,
        },
    )
    if no_gsis:
        logger.info(
            "players without a gsis_id are unreachable from nflverse stats",
            extra={"missing_gsis_id": no_gsis, "mapped": len(out)},
        )
    return sorted(out.items())


def backfill_gsis(docs: list[dict[str, Any]], bridge: Mapping[str, str] | None) -> int:
    """Fill in ``gsis_id`` from an external bridge where Sleeper has none.

    Sleeper is authoritative where it *has* the id; this only fills blanks. It
    has to fill a lot of them: 84% of the top 50 fantasy players by market rank
    came back with ``gsis_id: None`` on 2026-09-01, and without the nflverse
    join key none of them had a game log or a usage rollup on any paid endpoint
    (DESIGN_NOTES §23).

    Returns:
        How many documents were filled in.
    """
    if not bridge:
        return 0
    filled = 0
    for doc in docs:
        if doc.get("gsis_id"):
            continue
        gsis = _clean_id(bridge.get(str(doc.get("player_id") or "")))
        if gsis:
            doc["gsis_id"] = gsis
            filled += 1
    return filled


async def sync_players(
    store: Store,
    *,
    client: SleeperClient | None = None,
    dump: dict[str, Any] | None = None,
    gsis_bridge: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Run the nightly players sync.

    Args:
        store: Destination store.
        client: Sleeper client to fetch with. Ignored when ``dump`` is given;
            constructed (and closed) here when both are omitted.
        dump: Pre-fetched ``/players/nfl`` payload — the injection point for
            offline tests.
        gsis_bridge: ``{sleeper_id: gsis_id}`` used to fill blanks Sleeper
            leaves. Omitted, coverage is Sleeper's own — which is poor exactly
            where it matters most.

    Returns:
        Write counts (``players``, ``index_names``, ``id_map``) plus the
        matching ``*_deleted`` prune counts.
    """
    owned_fetch = dump is None and client is None
    if dump is None:
        owned = client is None
        sleeper = client or SleeperClient()
        try:
            dump = await sleeper.get_players()
        finally:
            if owned:
                await sleeper.aclose()

    docs = select_players(dump)
    if not docs or (owned_fetch and len(docs) < MIN_LIVE_SNAPSHOT_PLAYERS):
        # Never turn an empty/truncated upstream response into a successful
        # freshness marker. Existing documents remain available and visibly
        # stale until a real snapshot arrives.
        raise ValueError(
            f"Sleeper player snapshot contained only {len(docs)} eligible players; "
            "refusing to replace the live snapshot"
        )
    gsis_filled = backfill_gsis(docs, gsis_bridge)
    written = await write_docs(store, PLAYERS_COLLECTION, [(d["player_id"], d) for d in docs])
    index = build_player_index(docs)
    index_written = await write_docs(store, PLAYER_INDEX_COLLECTION, index)
    id_map = build_id_map(docs)
    id_map_written = await write_docs(store, ID_MAP_COLLECTION, id_map)

    deleted = await prune_to_snapshot(store, docs, index, id_map)

    await update_freshness(store, ["players", "player_index", "id_map"])
    counts = {
        "players": written,
        "index_names": index_written,
        "id_map": id_map_written,
        "gsis_backfilled": gsis_filled,
        **deleted,
    }
    logger.info("sleeper players sync complete", extra=counts)
    return counts


async def prune_to_snapshot(
    store: Store,
    docs: list[dict[str, Any]],
    index: list[tuple[str, dict[str, Any]]],
    id_map: list[tuple[str, dict[str, Any]]],
) -> dict[str, int]:
    """Delete anything the new snapshot no longer contains.

    The sync is otherwise upsert-only, so a player who leaves the filter set —
    retired, waived into inactivity, moved off a fantasy position — would keep
    resolving by name and keep turning up in candidate scans forever. The three
    collections are pruned to exactly what this run wrote.

    ``player_index`` needs both halves of that: a name doc whose candidate list
    merely *shrank* was already rewritten with the survivors by
    :func:`build_player_index`, and only a name with no surviving candidate at
    all is missing from ``index`` and therefore deleted here.

    Empty snapshots are rejected by :func:`sync_players` before any writes.

    Returns:
        ``{"players_deleted", "index_names_deleted", "id_map_deleted"}``.
    """
    return {
        "players_deleted": await delete_missing(
            store, PLAYERS_COLLECTION, {d["player_id"] for d in docs}
        ),
        "index_names_deleted": await delete_missing(
            store, PLAYER_INDEX_COLLECTION, {name for name, _ in index}
        ),
        "id_map_deleted": await delete_missing(
            store, ID_MAP_COLLECTION, {gsis_id for gsis_id, _ in id_map}
        ),
    }
