"""Read models over :class:`~api.core.store.Store`.

This module is the **contract between the ingest wave and the analysis wave**.
Ingest writes the document shapes documented here; the ADK ``stats_agent`` calls
these functions as its function tools (tech spec §5) and never touches the store
directly. Every function is read-only and returns plain JSON-able data.

Collections written by ingest
-----------------------------

``players/{player_id}``
    One Sleeper player. Source: nightly ``/players/nfl`` dump plus nflverse joins::

        {
          "player_id": "4046",              # Sleeper id, also the doc id
          "name": "Patrick Mahomes",
          "search_name": "patrick mahomes", # normalize_name() output
          "position": "QB",
          "team": "KC",                     # None for free agents
          "status": "Active",
          "injury_status": null,            # "Questionable" | "Out" | ...
          "injury_detail": "Hamstring",     # merged from injuries/ (see below)
          "practice_status": "Limited Participation",   # likewise
          "gsis_id": "00-0033873",          # nflverse join key; may be null
          "espn_id": "3139477",
          "years_exp": 8
        }

``player_index/{normalized_name}``
    Name -> candidate ids. Doc id is :func:`normalize_name` of the display name,
    so ambiguous names ("josh allen") legitimately hold multiple candidates::

        {
          "_id": "josh allen",
          "candidates": [
            {"player_id": "4984", "name": "Josh Allen", "team": "BUF", "position": "QB"},
            {"player_id": "5045", "name": "Josh Allen", "team": "JAX", "position": "LB"}
          ]
        }

``weekly_stats/{season}_{week}/players/{player_id}``
    One regular-season player-week stat line from nflverse (nested path, see
    :mod:`api.core.store` for the path convention). The doc id is the Sleeper
    id when ``id_map/{gsis_id}`` resolves, else the raw ``gsis_id`` — so a
    reader holding a Sleeper id may need ``players/{id}.gsis_id`` to find it::

        {
          "player_id": "4046", "gsis_id": "00-0033873", "season": 2026, "week": 3,
          "name": "Patrick Mahomes", "position": "QB",   # upper-cased; may be null
          "team": "KC", "opponent": "ATL",
          "fantasy_points": 21.4, "fantasy_points_ppr": 21.4,   # null if absent
          "snap_pct": 0.98,        # null when the snap join failed
          "targets": 0, "target_share": 0.0, "carries": 3, "rz_touches": 1,
          "completions": 25.0, "passing_yards": 280.0, ...   # the rest of
        }                          # ingest.nflverse_ingest.STAT_COLUMNS, when present

``usage_trends/{player_id}``
    Derived L4W rollup, recomputed each ingest::

        {
          "player_id": "4046", "gsis_id": "00-0033873", "season": 2026,
          "through_week": 3,
          "weeks_counted": 4,         # games in the window (last 4 *played*)
          "last_week_played": 3,      # a stale rollup is not current form
          "snap_pct_l4w": 0.96, "target_share_l4w": 0.21, "rz_touches_l4w": 5,
          "snap_pct_delta": 0.08,     # L2W minus prior-2W; positive = rising
          "target_share_delta": 0.04,
          "trend": "rising"           # "rising" | "flat" | "declining"
        }

``def_vs_pos/{team}``
    Fantasy points allowed by position, season to date::

        {
          "team": "ATL", "season": 2026, "through_week": 3,
          "positions": {
            "QB": {"points_allowed_per_game": 19.8, "rank": 7},   # rank 1 = most generous
            "RB": {"points_allowed_per_game": 24.1, "rank": 2},
            "WR": {...}, "TE": {...}
          }
        }

``trending/{kind}``  (``kind`` is ``"add"`` or ``"drop"``)
    Sleeper market signal, refreshed every 30 minutes::

        {
          "kind": "add", "lookback_hours": 24, "fetched_at": "2026-09-16T13:30:00Z",
          "entries": [{"player_id": "4046", "count": 51234, "name": "...",
                       "position": "RB", "team": "KC"}]
        }

``schedules/{season}_{week}``
    One week of the NFL schedule. A team on bye is absent from ``games``::

        {
          "season": 2026, "week": 3, "first_game": "2026-09-17T00:15:00Z",
          "games": [{"home": "KC", "away": "ATL", "kickoff": "2026-09-21T17:00:00Z",
                     "venue": "Arrowhead", "game_id": "2026_03_ATL_KC",
                     "game_type": "REG"}]
        }

``injuries/{player_id}``
    The latest nflverse injury report row per player — only the report's own
    week, never an older row carried forward. Doc id follows the weekly-stats
    policy (Sleeper id, else ``gsis_id``). No read function: the status reaches
    the engine through ``players/{id}.injury_status``, into which ingest merges
    the non-null fields below only while ``week`` is the current week::

        {
          "player_id": "6794", "gsis_id": "00-0036322", "season": 2026, "week": 4,
          "team": "MIN", "position": "WR", "name": "Justin Jefferson",
          "injury_status": "Questionable",          # nflverse report_status
          "injury_detail": "Hamstring",
          "practice_status": "Limited Participation",
          "updated_at": "2026-10-01T18:00:00Z"      # nflverse date_modified
        }

``depth_charts/{team}``
    The latest nflverse depth chart snapshot, offensive skill slots (and
    kickers) only; read by :func:`get_depth_chart`. ``player_id`` here is the
    **gsis id**, not the Sleeper id::

        {
          "team": "MIN", "season": 2026,
          "week": null,                             # legacy frames only
          "updated_at": "2026-09-14T07:32:09Z",     # snapshot timestamp
          "positions": {"WR": [{"player_id": "00-0036322",
                                "name": "Justin Jefferson", "rank": 1}, ...]}
        }

``meta/freshness``
    Single doc, ``{dataset: iso8601_timestamp}``, updated by every ingest task::

        {"weekly_stats": "2026-09-16T09:02:11Z", "players": "2026-09-16T04:00:03Z",
         "trending": "2026-09-16T13:30:00Z", "schedules": "2026-09-01T04:00:00Z"}

``meta/schedule_weeks``
    Single doc consumed by :mod:`api.core.week`; see that module for its shape.

``meta/preseason_gap``
    Datasets upstream cannot supply yet (the season's stats file does not
    exist before its first game), re-affirmed by every ``stats`` run and
    cleared (``datasets: []``) once real stats land. Read by
    :func:`get_preseason_gap`; trusted only for :data:`GAP_TRUST_SECONDS`
    after ``recorded_at`` (:func:`exempt_datasets`)::

        {"season": 2026, "datasets": ["def_vs_pos", "injuries", "usage_trends",
                                      "weekly_stats"],
         "recorded_at": "2026-09-01T13:01:00Z"}
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

from api.core.store import Store

logger = logging.getLogger(__name__)

# Collection names, centralized so ingest and the read models cannot drift.
PLAYERS_COLLECTION = "players"
PLAYER_INDEX_COLLECTION = "player_index"
USAGE_TRENDS_COLLECTION = "usage_trends"
INJURIES_COLLECTION = "injuries"
DEF_VS_POS_COLLECTION = "def_vs_pos"
TRENDING_COLLECTION = "trending"
SCHEDULES_COLLECTION = "schedules"
DEPTH_CHARTS_COLLECTION = "depth_charts"
META_COLLECTION = "meta"
FRESHNESS_DOC_ID = "freshness"
PRESEASON_GAP_DOC_ID = "preseason_gap"

# Maximum age of each ingest marker before the API refuses to sell analysis
# backed by it. Schedule data changes infrequently; every other limit is wider
# than its documented scheduler cadence while still detecting a stopped job.
DATASET_MAX_AGE_SECONDS: dict[str, float] = {
    "players": 36 * 3600,
    "player_index": 36 * 3600,
    "id_map": 36 * 3600,
    "trending": 2 * 3600,
    "weekly_stats": 96 * 3600,
    "usage_trends": 96 * 3600,
    "def_vs_pos": 96 * 3600,
    "injuries": 96 * 3600,
    "depth_charts": 96 * 3600,
    "schedules": 370 * 24 * 3600,
}
DEFAULT_MAX_AGE_SECONDS = 24 * 3600

#: Sleeper statuses that keep a player off the field this week. Sleeper writes
#: two vocabularies: the short ``injury_status`` codes ("Out", "IR", "Sus",
#: "PUP", "NFI") and the long roster ``status`` labels ("Injured Reserve",
#: "Physically Unable to Perform", "Suspended", "Inactive"). Both are listed
#: in their canonical spelling so an equality query (``injury_status in
#: [...]``) matches what ingest writes; :func:`is_out_status` compares without
#: case for everything else. "Doubtful" is here on purpose: the engine benches
#: a doubtful player, so it must not archive a call on him either.
OUT_STATUSES: frozenset[str] = frozenset(
    {
        "Out",
        "IR",
        "Injured Reserve",
        "PUP",
        "Physically Unable to Perform",
        "Sus",
        "Suspended",
        "NFI",
        "Non Football Injury",
        "Inactive",
        "Doubtful",
    }
)
_OUT_STATUSES_FOLDED = frozenset(status.casefold() for status in OUT_STATUSES)


def is_out_status(status: Any) -> bool:
    """Whether a Sleeper status string means the player does not play this week."""
    return isinstance(status, str) and status.strip().casefold() in _OUT_STATUSES_FOLDED


#: Sleeper ``search_rank`` at or above which the value is not a rank at all.
#: Players the market has no opinion about carry a sentinel (9999999); read as
#: a rank it turns a late-round pick into a "value" of millions of places and
#: grades a whole draft F. Real ranks stop in the low thousands.
MARKET_RANK_CAP = 5000


def market_rank(value: Any) -> int | None:
    """A Sleeper ``search_rank`` as a usable rank, or ``None`` when unranked."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value < MARKET_RANK_CAP else None


_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_SPACE_RE = re.compile(r"\s+")
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    """Normalize a player name into a ``player_index`` document id.

    Lower-cases, strips accents, removes punctuation (so "D'Andre" and "DAndre"
    collide), drops generational suffixes, and collapses whitespace.

    Ingest **must** use this exact function when writing ``player_index`` doc ids.

    Examples:
        >>> normalize_name("Ja'Marr Chase")
        'jamarr chase'
        >>> normalize_name("Marvin Harrison Jr.")
        'marvin harrison'
    """
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = _PUNCT_RE.sub("", ascii_only.lower())
    parts = [p for p in _SPACE_RE.split(lowered.strip()) if p]
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return " ".join(parts)


def weekly_stats_collection(season: int, week: int) -> str:
    """Return the nested collection path holding one week of player stat lines.

    E.g. ``weekly_stats_collection(2026, 3)`` -> ``"weekly_stats/2026_3/players"``.
    """
    return f"weekly_stats/{season}_{week}/players"


async def resolve_player(store: Store, name: str) -> list[dict[str, Any]]:
    """Resolve a player name to candidate players.

    Looks up ``player_index/{normalize_name(name)}``. Returns every candidate,
    ambiguity included — the caller (or the stats agent) decides. An unknown
    name returns ``[]`` rather than raising, so the agent can say "I couldn't
    find that player" instead of erroring the paid call.

    Args:
        store: Backing store.
        name: Player name as typed by a human or an agent.

    Returns:
        Candidate dicts, each with ``player_id``, ``name``, ``team``, ``position``.
        Empty when the name is unknown.
    """
    key = normalize_name(name)
    if not key:
        return []
    doc = await store.get(PLAYER_INDEX_COLLECTION, key)
    if not doc:
        logger.info("player_index miss for %r (normalized %r)", name, key)
        return []
    candidates = doc.get("candidates") or []
    return [c for c in candidates if isinstance(c, dict)]


async def get_player(store: Store, player_id: str) -> dict[str, Any] | None:
    """Return the ``players/{player_id}`` document, or ``None`` if unknown.

    ``player_id`` can be a raw caller token (``/v1/player`` tries it as an id
    before the name index). Firestore rejects a doc id containing ``/`` with a
    ``ValueError``, which would 500 a paid call for a typo; an id that cannot
    exist is simply unknown.
    """
    token = str(player_id or "").strip()
    if not token or "/" in token:
        return None
    try:
        return await store.get(PLAYERS_COLLECTION, token)
    except ValueError:
        logger.info("rejected player id %r as a document id", token)
        return None


async def get_weekly_stats(
    store: Store, player_id: str, season: int, weeks: list[int] | tuple[int, ...]
) -> list[dict[str, Any]]:
    """Return this player's stat lines for the given weeks.

    Args:
        store: Backing store.
        player_id: Sleeper player_id.
        season: NFL season year.
        weeks: Weeks to fetch. Missing weeks (bye, DNP, not yet ingested) are
            silently skipped rather than returned as blanks.

    Returns:
        Stat-line dicts sorted by week ascending. Each includes ``"week"``.
    """
    out: list[dict[str, Any]] = []
    for week in weeks:
        doc = await store.get(weekly_stats_collection(season, week), player_id)
        if doc:
            doc.setdefault("week", week)
            out.append(doc)
    out.sort(key=lambda d: d.get("week", 0))
    return out


async def get_usage_trends(store: Store, player_id: str) -> dict[str, Any] | None:
    """Return the derived L4W usage rollup for a player, or ``None`` if absent.

    This is the emerging-player signal: ``snap_pct_delta`` / ``target_share_delta``
    crossed with Sleeper trending is how ``/v1/report`` detects players "coming
    up" before consensus (PRD §4.2).
    """
    return await store.get(USAGE_TRENDS_COLLECTION, player_id)


async def get_depth_chart(store: Store, team: str, position: str) -> dict[str, Any] | None:
    """Return one team's depth chart for one position.

    Before a season starts this is the most informative thing the store holds
    about a player: nflverse publishes depth charts through the preseason, so
    "QB1, ahead of two others" is current-season fact when the game log is still
    empty. Shape is ``{"team", "season", "position", "players": [{"rank",
    "name", "player_id"}, ...]}`` ordered by rank, or ``None``.
    """
    doc = await store.get(DEPTH_CHARTS_COLLECTION, str(team).upper())
    if not doc:
        return None
    positions = doc.get("positions") or {}
    room = positions.get(str(position).upper())
    if not isinstance(room, list) or not room:
        return None
    ordered = sorted(
        (r for r in room if isinstance(r, dict)),
        key=lambda r: int(r.get("rank") or 99),
    )
    return {
        "team": str(team).upper(),
        "season": doc.get("season"),
        "position": str(position).upper(),
        "players": ordered,
    }


async def get_def_vs_pos(store: Store, team: str, position: str) -> dict[str, Any] | None:
    """Return one team's fantasy points allowed to one position.

    Args:
        store: Backing store.
        team: NFL team abbreviation, case-insensitive (stored upper-case).
        position: Position abbreviation, case-insensitive (stored upper-case).

    Returns:
        ``{"team", "position", "season", "through_week", "points_allowed_per_game",
        "rank"}`` where ``rank`` 1 = most generous defense to that position.
        ``None`` when the team or position has no ingested split.
    """
    doc = await store.get(DEF_VS_POS_COLLECTION, team.upper())
    if not doc:
        return None
    entry = (doc.get("positions") or {}).get(position.upper())
    if not isinstance(entry, dict):
        return None
    return {
        "team": doc.get("team", team.upper()),
        "position": position.upper(),
        "season": doc.get("season"),
        "through_week": doc.get("through_week"),
        **entry,
    }


async def get_trending(store: Store, kind: str = "add") -> list[dict[str, Any]]:
    """Return the cached Sleeper trending board.

    Reads ``trending/{kind}`` (refreshed every 30 min by the scheduler) rather
    than calling Sleeper live — request paths must never hit the Sleeper
    trending endpoint directly.

    Args:
        store: Backing store.
        kind: ``"add"`` or ``"drop"``.

    Returns:
        Entry dicts (``player_id``, ``count``, and whatever identity fields
        ingest joined in), Sleeper's ordering preserved. Empty when the poll has
        not run yet.
    """
    if kind not in ("add", "drop"):
        raise ValueError(f"kind must be 'add' or 'drop', got {kind!r}")
    doc = await store.get(TRENDING_COLLECTION, kind)
    if not doc:
        logger.warning("no trending/%s document; trending poll may not have run", kind)
        return []
    entries = doc.get("entries") or []
    return [e for e in entries if isinstance(e, dict)]


async def get_schedule(store: Store, team: str, week: int, season: int) -> dict[str, Any] | None:
    """Return one team's game for a given week.

    Args:
        store: Backing store.
        team: NFL team abbreviation, case-insensitive.
        week: NFL week.
        season: NFL season year.

    Returns:
        ``{"season", "week", "team", "opponent", "home", "kickoff"}`` where
        ``home`` is whether ``team`` is the home side. ``None`` when the week is
        not ingested or the team is on bye.
    """
    doc = await store.get(SCHEDULES_COLLECTION, f"{season}_{week}")
    if not doc:
        return None
    target = team.upper()
    for game in doc.get("games") or []:
        if not isinstance(game, dict):
            continue
        home = str(game.get("home", "")).upper()
        away = str(game.get("away", "")).upper()
        if target == home:
            opponent, is_home = away, True
        elif target == away:
            opponent, is_home = home, False
        else:
            continue
        return {
            "season": season,
            "week": week,
            "team": target,
            "opponent": opponent,
            "home": is_home,
            "kickoff": game.get("kickoff"),
        }
    return None  # bye week or team unknown for this week


async def get_draft_pool(
    store: Store, *, limit: int = 200, positions: tuple[str, ...] = ("QB", "RB", "WR", "TE")
) -> list[dict[str, Any]]:
    """Return the draftable player universe, ordered by market prominence.

    The ordering key is Sleeper's ``search_rank`` — how early the market drafts
    a player. It is a *popularity* signal, not a consensus ADP from a projection
    service, and callers must say so: the draft board labels it ``market_rank``
    and never calls it ADP.

    Players with no ``search_rank`` (or Sleeper's unranked sentinel, see
    :data:`MARKET_RANK_CAP`) are excluded rather than sorted last. An
    unranked player is one the market has no opinion about, which on a draft
    board is the same as not being draftable. Players with no ``team`` are
    excluded too: Sleeper's dump still carries Tom Brady as ``active`` with a
    ``search_rank`` of 74, and the first narrated board ranked him 97th. The
    team field is the one signal that survives retirement.

    Each entry is the ``players/`` document plus its ``usage_trends/`` rollup
    under ``usage`` (``None`` when the player has no prior-season usage — a
    rookie, or someone who did not play).

    Args:
        store: Store to read from.
        limit: Maximum players to return.
        positions: Position filter; the fantasy-relevant four by default.

    Returns:
        Up to ``limit`` players, most prominent first.
    """
    players = await store.list(PLAYERS_COLLECTION)
    ranked = [
        player
        for player in players
        if player.get("position") in positions
        and market_rank(player.get("search_rank")) is not None
        and player.get("team")
    ]
    ranked.sort(key=lambda p: p["search_rank"])
    selected = ranked[:limit]
    for player in selected:
        player["usage"] = await get_usage_trends(store, str(player.get("player_id")))
    return selected


async def get_data_freshness(store: Store) -> dict[str, str]:
    """Return the ``meta/freshness`` map of ``{dataset: iso8601_timestamp}``.

    Feeds :attr:`api.schemas.AnalysisMeta.data_freshness` — the promise that
    every paid response says how stale its inputs are. Returns ``{}`` when
    ingest has never run.
    """
    doc = await store.get(META_COLLECTION, FRESHNESS_DOC_ID)
    if not doc:
        logger.warning("no meta/freshness document; ingest may not have run")
        return {}
    return {k: str(v) for k, v in doc.items() if k != "_id"}


#: How long a preseason-gap marker is believed without being re-affirmed.
#: Longer than the widest gap in the stats cron (Sat -> Tue, 72h), so a running
#: job keeps the exemption alive; shorter than forever, so a *stopped* job loses
#: it and the SLA starts refusing sales again. The exemption has to be able to
#: expire, or "upstream has no data yet" becomes a permanent excuse for silence.
GAP_TRUST_SECONDS = 96 * 3600


def exempt_datasets(gap: dict[str, Any] | None, *, now: datetime | None = None) -> set[str]:
    """Datasets the ingest has declared genuinely unavailable, not merely stale.

    Before a season's first game, nflverse has no stats file to publish and
    nflreadpy rejects the season outright, so `weekly_stats` and friends cannot
    be refreshed by anyone. Ageing them out would refuse to sell analysis over a
    gap that is the calendar rather than a fault — on 2026-09-01 that combination
    was four days from taking eight of ten endpoints down (DESIGN_NOTES §22).

    Trusted only while a recent ingest run keeps re-affirming it.
    """
    if not gap:
        return set()
    recorded = _parse_marker(gap.get("recorded_at"))
    if recorded is None:
        return set()
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if (current - recorded).total_seconds() > GAP_TRUST_SECONDS:
        return set()
    return {str(name) for name in (gap.get("datasets") or []) if name}


#: Dataset -> the flat collection that must still hold at least one document
#: for a preseason exemption to be believed.
#:
#: ``weekly_stats`` is season/week-nested (``weekly_stats/{season}_{week}/players``)
#: so it has no single collection to probe. ``usage_trends`` is the rollup
#: computed from it and rewritten on every ingest, so rows there are proof that
#: stat history landed -- and it is what the boards actually scan.
_GAP_EVIDENCE: dict[str, str] = {
    "weekly_stats": USAGE_TRENDS_COLLECTION,
    "usage_trends": USAGE_TRENDS_COLLECTION,
    "def_vs_pos": DEF_VS_POS_COLLECTION,
    "injuries": INJURIES_COLLECTION,
}


async def exempt_datasets_with_evidence(
    store: Store, gap: dict[str, Any] | None, *, now: datetime | None = None
) -> set[str]:
    """Exempt only the datasets the store can still actually answer from.

    :func:`exempt_datasets` says upstream has nothing new to publish. That is
    the right call on a store carrying last season's rollups: the numbers are
    old because the calendar is, and refusing to sell over it was what nearly
    took eight endpoints down (DESIGN_NOTES §22).

    It is the wrong call on a *cold* store. A fresh preseason deployment gets
    no rows, writes no freshness markers, and records the same gap -- so the
    exemption would pass readiness and let ``/v1/sleepers`` settle a payment
    for a board ``usage_trends`` has nothing to fill. An empty paid answer is
    worse than no answer, and unlike a 503 it is billed.

    So the marker is believed per dataset, and only where the store can show
    a row behind it. One ``limit=1`` read per distinct collection, and none at
    all when there is no live gap -- which is every request outside the
    preseason window.
    """
    declared = exempt_datasets(gap, now=now)
    if not declared:
        return set()
    seen: dict[str, bool] = {}
    kept: set[str] = set()
    for name in sorted(declared):
        collection = _GAP_EVIDENCE.get(name)
        if collection is None:
            # An unrecognised dataset has no row to point at, so it cannot be
            # shown to be answerable. Refusing is the safe direction.
            continue
        if collection not in seen:
            seen[collection] = bool(await store.list(collection, limit=1))
        if seen[collection]:
            kept.add(name)
    return kept


def _parse_marker(marker: Any) -> datetime | None:
    """Parse an ISO marker into an aware UTC datetime, or ``None`` if unusable."""
    if not isinstance(marker, str):
        return None
    try:
        parsed = datetime.fromisoformat(marker.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def stale_datasets(
    freshness: dict[str, str],
    required: list[str] | tuple[str, ...] | None = None,
    *,
    now: datetime | None = None,
    gap: dict[str, Any] | None = None,
) -> list[str]:
    """Return present datasets whose marker is invalid or older than its SLA.

    Datasets covered by a live preseason-gap marker are skipped: they are not
    stale, they do not exist yet. See :func:`exempt_datasets`.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    exempt = exempt_datasets(gap, now=current)
    names = required if required is not None else tuple(freshness)
    stale: list[str] = []
    for name in names:
        if name in exempt:
            continue
        marker = freshness.get(name)
        if marker is None:
            continue
        try:
            parsed = datetime.fromisoformat(marker.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            age = (current - parsed.astimezone(UTC)).total_seconds()
        except (TypeError, ValueError):
            stale.append(name)
            continue
        if age > DATASET_MAX_AGE_SECONDS.get(name, DEFAULT_MAX_AGE_SECONDS):
            stale.append(name)
    return stale


async def get_preseason_gap(store: Store) -> dict[str, Any]:
    """Return ``meta/preseason_gap``, or ``{}`` when the ingest has not set one."""
    doc = await store.get(META_COLLECTION, PRESEASON_GAP_DOC_ID)
    if not doc:
        return {}
    return {k: v for k, v in doc.items() if k != "_id"}
