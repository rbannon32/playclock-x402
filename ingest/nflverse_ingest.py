"""nflverse -> Firestore ingest (tech spec §4.2).

``nflreadpy`` returns polars DataFrames; **all** polars work happens here, at
ingest time, so nothing on the paid request path ever imports polars (tech spec
§4.2). ``nflreadpy`` itself is imported inside :func:`default_loaders` so the api
container never needs the ingest extra.

Loaders are injectable
----------------------
Every network call goes through :class:`Loaders`. Tests pass small hand-built
polars frames with the real nflverse column names; production passes
:func:`default_loaders`. Seasons are **always** passed explicitly —
``load_schedules()`` called bare defaults to every season since 1999.

What gets written
-----------------
* ``weekly_stats/{season}_{week}/players/{player_id}`` — per-player stat line
* ``usage_trends/{player_id}`` — L4W snap%, target share, rz touches, deltas
* ``def_vs_pos/{team}`` — PPR fantasy points allowed per position, season to date
* ``schedules/{season}_{week}`` — one week of games, plus ``meta/schedule_weeks``
* ``injuries/{player_id}`` — latest injury report row per player
* ``depth_charts/{team}`` — depth chart by position

Doc ids follow the package-level id policy: Sleeper ``player_id`` when
``id_map/{gsis_id}`` resolves, else the raw ``gsis_id`` (counted and logged).
Every shape written here is specified in :mod:`api.data.stats_store`'s module
docstring, which is the contract: ``depth_charts/`` is read by
:func:`~api.data.stats_store.get_depth_chart`; ``injuries/`` has no read
function (only its freshness and preseason-gap evidence are read), and the
report-week status is *also* merged into ``players/{player_id}.injury_status``
while that report is the current week's (see :func:`write_injuries`).

Red-zone usage
--------------
No nflverse summary table publishes a red-zone split (checked against 2025 data:
neither ``load_player_stats`` nor ``load_ff_opportunity`` has one), so
``rz_touches`` is counted from ``load_pbp`` — rush attempts plus pass targets
inside the opponent's 20. One season of play-by-play is ~49k rows and aggregates
in well under a second, and the whole thing is supplemental: if it fails,
``rz_touches`` stays 0 and the rest of the run continues.

Graceful degradation
--------------------
Snap counts key on ``pfr_player_id``, not ``gsis_id``; the join runs through
``load_players()`` (``pfr_id`` <-> ``gsis_id``). When that join is unavailable —
column renamed upstream, players frame empty — usage trends are still written,
just without ``snap_pct``, and the miss is logged with counts. The same applies
to injuries and depth charts: a failure there is a warning, not a job failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from api.core.config import Settings, get_settings
from api.core.store import Store
from api.core.week import SCHEDULE_WEEKS_DOC_ID, current_week
from api.data.stats_store import (
    DEF_VS_POS_COLLECTION,
    DEPTH_CHARTS_COLLECTION,
    INJURIES_COLLECTION,
    META_COLLECTION,
    PLAYERS_COLLECTION,
    PRESEASON_GAP_DOC_ID,
    SCHEDULES_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    weekly_stats_collection,
)
from ingest.common import update_freshness, utc_now_iso, write_docs
from ingest.sleeper_players import ID_MAP_COLLECTION

logger = logging.getLogger(__name__)

#: Timezone nflverse schedule ``gametime`` values are expressed in.
SCHEDULE_TZ = ZoneInfo("America/New_York")

#: Positions carried in ``def_vs_pos`` splits. Not kickers: the ingested
#: ``fantasy_points_ppr`` has no kicking in it, so every defense "allowed" 0.0
#: to kickers, tied at rank 1, and every kicker read as a plus-matchup streamer.
DEF_VS_POS_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE")

#: Weeks in the usage-trend lookback window, and the split used for deltas
#: (last two weeks vs the two before them).
USAGE_WINDOW = 4
USAGE_DELTA_SPLIT = 2

#: Minimum share change (in points of a 0-1 rate) that counts as a real move.
TREND_THRESHOLD = 0.05

#: nflverse column candidates for the identity fields, newest naming first.
_GSIS_COLUMNS = ("player_id", "gsis_id")
_NAME_COLUMNS = ("player_display_name", "player_name", "full_name", "display_name")
_TEAM_COLUMNS = ("team", "recent_team", "team_abbr", "club_code")
_OPPONENT_COLUMNS = ("opponent_team", "opponent", "opponent_abbr")
_POSITION_COLUMNS = ("position", "position_group", "depth_position", "pos_abb")

#: Fantasy-relevant subset of the ~150 weekly stat columns: document key ->
#: source column candidates.
STAT_COLUMNS: dict[str, tuple[str, ...]] = {
    "fantasy_points": ("fantasy_points",),
    "fantasy_points_ppr": ("fantasy_points_ppr",),
    "completions": ("completions",),
    "attempts": ("attempts",),
    "passing_yards": ("passing_yards",),
    "passing_tds": ("passing_tds",),
    "interceptions": ("interceptions",),
    "carries": ("carries", "rushing_attempts"),
    "rushing_yards": ("rushing_yards",),
    "rushing_tds": ("rushing_tds",),
    "targets": ("targets",),
    "receptions": ("receptions",),
    "receiving_yards": ("receiving_yards",),
    "receiving_tds": ("receiving_tds",),
    "target_share": ("target_share",),
    "air_yards_share": ("air_yards_share",),
    "wopr": ("wopr",),
}

#: Columns summed into ``rz_touches`` when the source frame happens to carry a
#: red-zone split. Verified against nflverse 2025 data: neither
#: ``load_player_stats`` nor ``load_ff_opportunity`` publishes one, so in
#: practice red-zone usage comes from :func:`build_red_zone_touches` (play-by-play)
#: and these are only a fast path for a future upstream column.
RZ_TOUCH_COLUMNS: tuple[str, ...] = (
    "rushing_red_zone_carries",
    "receiving_red_zone_targets",
    "rush_attempt_rz",
    "rec_attempt_rz",
    "carries_rz",
    "targets_rz",
)

#: Yardline (yards from the opponent's end zone) that defines the red zone.
RED_ZONE_YARDLINE = 20

#: Depth-chart slots worth storing. The 2025+ nflverse depth chart is one row per
#: *every* slot on the roster (~2.3k rows league-wide per snapshot, defense and
#: special teams included); fantasy only needs the offensive skill spots.
DEPTH_CHART_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "FB", "WR", "TE", "K", "PK"})

#: Always present on a weekly stat document, even when the source lacks them.
_CORE_STAT_KEYS = ("fantasy_points", "fantasy_points_ppr", "targets", "target_share", "carries")


@dataclass(frozen=True)
class Loaders:
    """Injectable nflverse loaders.

    Each stat loader takes an explicit sequence of seasons; ``players`` takes
    none (``load_players()`` is a full, season-less player table).
    """

    player_stats: Callable[[Sequence[int]], pl.DataFrame]
    snap_counts: Callable[[Sequence[int]], pl.DataFrame]
    depth_charts: Callable[[Sequence[int]], pl.DataFrame]
    injuries: Callable[[Sequence[int]], pl.DataFrame]
    schedules: Callable[[Sequence[int]], pl.DataFrame]
    players: Callable[[], pl.DataFrame]
    pbp: Callable[[Sequence[int]], pl.DataFrame]
    ff_playerids: Callable[[], pl.DataFrame]


def default_loaders() -> Loaders:
    """Return loaders backed by ``nflreadpy``.

    Imported lazily so that merely importing this module (or the api service)
    never pulls in nflreadpy. Every call passes ``seasons`` explicitly:
    ``load_schedules`` defaults to *all* seasons since 1999 when called bare.
    """
    import nflreadpy  # noqa: PLC0415

    return Loaders(
        player_stats=lambda seasons: nflreadpy.load_player_stats(seasons=list(seasons)),
        snap_counts=lambda seasons: nflreadpy.load_snap_counts(seasons=list(seasons)),
        depth_charts=lambda seasons: nflreadpy.load_depth_charts(seasons=list(seasons)),
        injuries=lambda seasons: nflreadpy.load_injuries(seasons=list(seasons)),
        schedules=lambda seasons: nflreadpy.load_schedules(seasons=list(seasons)),
        players=lambda: nflreadpy.load_players(),
        pbp=lambda seasons: nflreadpy.load_pbp(seasons=list(seasons)),
        ff_playerids=lambda: nflreadpy.load_ff_playerids(),
    )


def sleeper_gsis_bridge(loaders: Loaders | None = None) -> dict[str, str]:
    """Return ``{sleeper_id: gsis_id}`` from nflverse's cross-reference table.

    **Sleeper does not carry ``gsis_id`` for most of the players that matter.**
    Measured 2026-09-01 against the live dump: only 16% of the top 50 by market
    rank had one — Ja'Marr Chase, Bijan Robinson, Jahmyr Gibbs, Justin Jefferson
    and CeeDee Lamb all came back ``None``. Since ``gsis_id`` is the nflverse
    join key, those players had no game log and no usage rollup on any paid
    endpoint, which is the worst possible distribution: absent for exactly the
    players people ask about.

    ``load_ff_playerids()`` carries both ids and closes it — top 50 goes to
    100%, top 200 to 99.5%. DESIGN_NOTES recorded this table as "blocked from
    this environment ... optional enhancement"; it is neither blocked nor
    optional (§23).

    Returns an empty mapping rather than raising: a missing bridge degrades
    ingest to Sleeper's own coverage, which is where it was before.
    """
    loaders = loaders or default_loaders()
    try:
        frame = loaders.ff_playerids()
    except Exception:
        logger.warning("ff_playerids unavailable; gsis coverage falls back to Sleeper's own")
        return {}

    bridge: dict[str, str] = {}
    for row in frame.select(["sleeper_id", "gsis_id"]).to_dicts():
        sleeper_raw, gsis = row.get("sleeper_id"), row.get("gsis_id")
        if sleeper_raw is None or not gsis:
            continue
        # Typed as an integer in this table and as a string everywhere else.
        sleeper_id = (
            str(int(sleeper_raw))
            if isinstance(sleeper_raw, (int, float))
            else str(sleeper_raw).strip()
        )
        cleaned = str(gsis).strip()
        if sleeper_id and cleaned:
            bridge[sleeper_id] = cleaned
    logger.info("sleeper->gsis bridge built", extra={"pairs": len(bridge)})
    return bridge


def resolve_season(settings: Settings | None = None, override: int | None = None) -> int:
    """Resolve the NFL season to ingest.

    Order: explicit ``override`` -> ``SEASON``. Never ``datetime.now().year``
    (the label rolls over in September, so January of 2027 is still season
    2026) and, since 2026-09-01, **never** ``nflreadpy.get_current_season()``
    either.

    That call used to win here, and it took the paid API down on launch day.
    It lags the calendar: on 2026-09-01 it still returned 2025, so the Tuesday
    ``stats`` run re-ingested the 2025 schedule over the 2026 one, ``week``
    resolved to 18, and every paid route 503'd on ``stale_season()``. Nothing
    was mis-sold — the guard is what caught it — but nothing could be sold
    either.

    The deeper problem was precedence, not lag. ``SEASON`` is an explicit
    operator declaration of what this deployment sells, and
    :func:`api.routes.paid.stale_season` refuses to serve anything else. Letting
    a library heuristic outrank it let ingest and serving disagree, which is the
    one thing they must never do. Upstream is now a cross-check that warns when
    it disagrees — useful for knowing when to bump ``SEASON``, never able to
    override it.
    """
    if override is not None:
        return int(override)

    settings = settings or get_settings()
    season = int(settings.season)

    try:
        import nflreadpy  # noqa: PLC0415

        upstream = int(nflreadpy.get_current_season())
    except Exception as exc:  # pragma: no cover - depends on nflreadpy internals
        logger.debug("nflreadpy.get_current_season() unavailable", extra={"error": str(exc)})
        return season

    if upstream != season:
        logger.warning(
            "nflreadpy reports a different season than SEASON; ingesting SEASON. "
            "If upstream has genuinely rolled over, bump SEASON and redeploy the "
            "API in the same change, or the season guard will refuse to serve.",
            extra={"season": season, "upstream_season": upstream},
        )
    return season


# --- frame helpers --------------------------------------------------------


def _column(df: pl.DataFrame, candidates: Iterable[str]) -> str | None:
    """Return the first candidate column present in ``df``."""
    columns = set(df.columns)
    for name in candidates:
        if name in columns:
            return name
    return None


def _rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    """Return ``df`` as a list of dicts (empty frame -> empty list)."""
    if df is None or df.height == 0:
        return []
    return list(df.iter_rows(named=True))


def _as_int(value: Any) -> int | None:
    """Coerce a cell to ``int``, or ``None`` when it is null/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce a cell to ``float``, or ``None`` when it is null/unparseable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result  # drop NaN


def _as_str(value: Any) -> str | None:
    """Coerce a cell to a non-empty string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mean(values: Sequence[float]) -> float | None:
    """Arithmetic mean, or ``None`` for an empty sequence."""
    return round(sum(values) / len(values), 4) if values else None


# --- weekly stats ---------------------------------------------------------


def transform_weekly_stats(df: pl.DataFrame, *, season: int) -> list[dict[str, Any]]:
    """Turn an nflverse weekly stats frame into normalized stat rows.

    Rows for other seasons are dropped, and so are postseason rows (a
    ``season_type`` other than ``"REG"``, when the frame carries that column;
    a null one is kept, as before the column was read): playoff weeks 19-22
    would otherwise land in ``weekly_stats`` as extra weeks, feed the usage
    window and def-vs-pos as if they were regular-season games, and push
    ``through_week`` past 18. Rows without a ``gsis_id`` or a week are
    unusable and dropped too. The returned dicts are the weekly-stat documents
    minus ``player_id`` (added by :func:`resolve_player_ids`) and ``snap_pct``
    (added by :func:`attach_snap_pct`).
    """
    rows = _rows(df)
    if not rows:
        return []

    gsis_col = _column(df, _GSIS_COLUMNS)
    if gsis_col is None:
        raise ValueError(f"weekly stats frame has no player id column; got {df.columns}")
    name_col = _column(df, _NAME_COLUMNS)
    team_col = _column(df, _TEAM_COLUMNS)
    opp_col = _column(df, _OPPONENT_COLUMNS)
    pos_col = _column(df, _POSITION_COLUMNS)
    season_col = _column(df, ("season",))
    season_type_col = _column(df, ("season_type",))
    week_col = _column(df, ("week",))
    if week_col is None:
        raise ValueError("weekly stats frame has no 'week' column")
    stat_cols = {key: _column(df, cands) for key, cands in STAT_COLUMNS.items()}
    rz_cols = [c for c in RZ_TOUCH_COLUMNS if c in df.columns]

    out: list[dict[str, Any]] = []
    skipped = 0
    postseason = 0
    for row in rows:
        if season_col is not None and _as_int(row.get(season_col)) != season:
            continue
        season_type = _as_str(row.get(season_type_col)) if season_type_col else None
        if season_type is not None and season_type.upper() != "REG":
            postseason += 1
            continue
        gsis_id = _as_str(row.get(gsis_col))
        week = _as_int(row.get(week_col))
        if not gsis_id or week is None:
            skipped += 1
            continue
        doc: dict[str, Any] = {
            "gsis_id": gsis_id,
            "season": season,
            "week": week,
            "name": _as_str(row.get(name_col)) if name_col else None,
            "position": (_as_str(row.get(pos_col)) or "").upper() or None if pos_col else None,
            "team": (_as_str(row.get(team_col)) or "").upper() or None if team_col else None,
            "opponent": (_as_str(row.get(opp_col)) or "").upper() or None if opp_col else None,
            "snap_pct": None,
            "rz_touches": sum(_as_float(row.get(c)) or 0.0 for c in rz_cols) if rz_cols else 0,
        }
        for key, column in stat_cols.items():
            if column is not None:
                doc[key] = _as_float(row.get(column))
            elif key in _CORE_STAT_KEYS:
                doc[key] = None
        out.append(doc)

    logger.info(
        "transformed weekly stats",
        extra={
            "season": season,
            "rows": len(out),
            "skipped": skipped,
            "postseason": postseason,
            "red_zone_columns": rz_cols,
        },
    )
    return out


def build_pfr_to_gsis(players_df: pl.DataFrame | None) -> dict[str, str]:
    """Map Pro-Football-Reference ids to gsis ids from ``load_players()``.

    Snap counts key on ``pfr_player_id``; every other nflverse table keys on
    ``gsis_id``. Returns ``{}`` (and logs) when the columns are absent, which
    makes the snap join degrade instead of failing.
    """
    if players_df is None or players_df.height == 0:
        return {}
    pfr_col = _column(players_df, ("pfr_id", "pfr_player_id"))
    gsis_col = _column(players_df, _GSIS_COLUMNS)
    if pfr_col is None or gsis_col is None:
        logger.warning(
            "players frame lacks pfr/gsis id columns; snap counts cannot be joined",
            extra={"columns": players_df.columns[:20]},
        )
        return {}
    mapping: dict[str, str] = {}
    for row in _rows(players_df):
        pfr = _as_str(row.get(pfr_col))
        gsis = _as_str(row.get(gsis_col))
        if pfr and gsis:
            mapping[pfr] = gsis
    logger.debug("built pfr->gsis map", extra={"mapped": len(mapping)})
    return mapping


def build_gsis_to_position(players_df: pl.DataFrame | None) -> dict[str, str]:
    """Map gsis id -> position from ``load_players()`` (fallback enrichment)."""
    if players_df is None or players_df.height == 0:
        return {}
    gsis_col = _column(players_df, _GSIS_COLUMNS)
    pos_col = _column(players_df, _POSITION_COLUMNS)
    if gsis_col is None or pos_col is None:
        return {}
    out: dict[str, str] = {}
    for row in _rows(players_df):
        gsis = _as_str(row.get(gsis_col))
        pos = _as_str(row.get(pos_col))
        if gsis and pos:
            out[gsis] = pos.upper()
    return out


def build_red_zone_touches(
    pbp_df: pl.DataFrame | None, *, season: int
) -> dict[tuple[str, int], int]:
    """Count red-zone touches per player-week from play-by-play.

    A "touch" is a rush attempt or a pass target inside the opponent's
    :data:`RED_ZONE_YARDLINE` — the goal-line/scoring-opportunity signal the
    sleeper and waiver products lean on (PRD §4.2). This is computed from
    ``load_pbp`` because *no* nflverse summary table carries a red-zone split:
    verified against 2025 data, neither ``load_player_stats`` nor
    ``load_ff_opportunity`` has one.

    Returns:
        ``{(gsis_id, week): touches}``; empty (with a log line) when the frame is
        missing or shaped unexpectedly.
    """
    if pbp_df is None or pbp_df.height == 0:
        logger.warning("no play-by-play available; rz_touches will stay 0")
        return {}
    yardline_col = _column(pbp_df, ("yardline_100",))
    week_col = _column(pbp_df, ("week",))
    if yardline_col is None or week_col is None:
        logger.warning("pbp frame missing yardline_100/week; skipping red-zone counts")
        return {}

    frame = pbp_df
    season_col = _column(frame, ("season",))
    if season_col is not None:
        frame = frame.filter(pl.col(season_col) == season)
    frame = frame.filter(pl.col(yardline_col) <= RED_ZONE_YARDLINE)

    counts: dict[tuple[str, int], int] = {}
    for id_col, flag_col in (
        (_column(frame, ("rusher_player_id",)), _column(frame, ("rush_attempt",))),
        (_column(frame, ("receiver_player_id",)), _column(frame, ("pass_attempt",))),
    ):
        if id_col is None:
            continue
        plays = frame.filter(pl.col(flag_col) == 1) if flag_col else frame
        grouped = (
            plays.filter(pl.col(id_col).is_not_null())
            .group_by([id_col, week_col])
            .agg(pl.len().alias("touches"))
        )
        for row in _rows(grouped):
            player = _as_str(row[id_col])
            week = _as_int(row[week_col])
            if player is None or week is None:
                continue
            counts[(player, week)] = counts.get((player, week), 0) + int(row["touches"])
    logger.info("counted red-zone touches", extra={"season": season, "player_weeks": len(counts)})
    return counts


def attach_rz_touches(rows: list[dict[str, Any]], red_zone: dict[tuple[str, int], int]) -> int:
    """Set ``rz_touches`` on each row from the play-by-play counts, in place.

    Rows with no red-zone work keep the ``0`` :func:`transform_weekly_stats` set,
    so the field is never null. Returns the number of rows updated.
    """
    if not red_zone:
        return 0
    updated = 0
    for row in rows:
        touches = red_zone.get((row["gsis_id"], row["week"]))
        if touches:
            row["rz_touches"] = touches
            updated += 1
    return updated


def enrich_positions(rows: list[dict[str, Any]], gsis_to_position: dict[str, str]) -> int:
    """Fill missing ``position`` values from the players table. Returns fills."""
    filled = 0
    for row in rows:
        if not row.get("position"):
            position = gsis_to_position.get(row["gsis_id"])
            if position:
                row["position"] = position
                filled += 1
    return filled


def attach_snap_pct(
    rows: list[dict[str, Any]],
    snaps_df: pl.DataFrame | None,
    pfr_to_gsis: dict[str, str],
) -> dict[str, int]:
    """Join offensive snap share onto weekly stat rows, in place.

    Args:
        rows: Rows from :func:`transform_weekly_stats`.
        snaps_df: ``load_snap_counts`` frame (keys on ``pfr_player_id``).
        pfr_to_gsis: Map from :func:`build_pfr_to_gsis`.

    Returns:
        ``{"joined", "unjoinable", "rows"}`` counts. Never raises: an empty
        frame or an unusable id map simply leaves ``snap_pct`` at ``None``.
    """
    counts = {"joined": 0, "unjoinable": 0, "rows": len(rows)}
    if snaps_df is None or snaps_df.height == 0:
        logger.warning("no snap counts available; usage trends will omit snap_pct")
        return counts
    pfr_col = _column(snaps_df, ("pfr_player_id", "pfr_id"))
    pct_col = _column(snaps_df, ("offense_pct", "offense_snap_pct"))
    week_col = _column(snaps_df, ("week",))
    if pfr_col is None or pct_col is None or week_col is None:
        logger.warning(
            "snap counts frame missing expected columns; skipping snap join",
            extra={"columns": snaps_df.columns[:20]},
        )
        return counts

    by_key: dict[tuple[str, int], float] = {}
    for row in _rows(snaps_df):
        pfr = _as_str(row.get(pfr_col))
        week = _as_int(row.get(week_col))
        pct = _as_float(row.get(pct_col))
        if pfr is None or week is None or pct is None:
            continue
        gsis = pfr_to_gsis.get(pfr)
        if gsis is None:
            counts["unjoinable"] += 1
            continue
        by_key[(gsis, week)] = pct

    for row in rows:
        pct = by_key.get((row["gsis_id"], row["week"]))
        if pct is not None:
            row["snap_pct"] = round(pct, 4)
            counts["joined"] += 1

    logger.info("joined snap counts", extra=counts)
    return counts


async def load_id_map(store: Store) -> dict[str, dict[str, Any]]:
    """Load ``id_map/`` as ``{gsis_id: entry}`` (empty before the nightly run)."""
    docs = await store.list(ID_MAP_COLLECTION)
    mapping = {str(doc.get("gsis_id") or doc.get("_id")): doc for doc in docs}
    if not mapping:
        logger.warning(
            "id_map is empty; nflverse docs will be keyed by gsis_id and the stats "
            "agent will not resolve them by name — run the nightly task first"
        )
    return mapping


def resolve_player_ids(
    rows: list[dict[str, Any]], id_map: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Set ``player_id`` on each row from ``id_map``, in place.

    Sleeper id when known, gsis id otherwise (see the package docstring). Returns
    ``{"mapped", "unmapped"}`` counts — an unmapped player is logged, never
    raised (tech spec §4.2).
    """
    counts = {"mapped": 0, "unmapped": 0}
    for row in rows:
        entry = id_map.get(row["gsis_id"])
        sleeper_id = _as_str(entry.get("sleeper_id")) if entry else None
        if sleeper_id:
            row["player_id"] = sleeper_id
            counts["mapped"] += 1
        else:
            row["player_id"] = row["gsis_id"]
            counts["unmapped"] += 1
    if counts["unmapped"]:
        logger.info("nflverse rows without a sleeper id", extra=counts)
    return counts


async def write_weekly_stats(
    store: Store, rows: list[dict[str, Any]], *, season: int
) -> dict[int, int]:
    """Write stat rows into ``weekly_stats/{season}_{week}/players``.

    Returns:
        ``{week: documents_written}``.
    """
    by_week: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for row in rows:
        by_week.setdefault(row["week"], []).append((row["player_id"], row))
    written: dict[int, int] = {}
    for week, docs in sorted(by_week.items()):
        written[week] = await write_docs(store, weekly_stats_collection(season, week), docs)
    logger.info("wrote weekly stats", extra={"season": season, "weeks": written})
    return written


# --- derived tables -------------------------------------------------------


def build_usage_trends(
    rows: list[dict[str, Any]],
    *,
    season: int,
    through_week: int | None = None,
    window: int = USAGE_WINDOW,
    threshold: float = TREND_THRESHOLD,
) -> list[dict[str, Any]]:
    """Build the L4W usage rollup documented in :mod:`api.data.stats_store`.

    The window is the player's last ``window`` **games played** at or before
    ``through_week`` — not the last four calendar weeks — so a bye or a single
    inactive week doesn't dilute the rate stats. The consequence is that a player
    who has been out for a month still shows his last four *played* games, so
    ``last_week_played`` and ``weeks_counted`` are written alongside and callers
    must not present a stale rollup as current form.

    ``*_l4w`` are means (rz touches are summed) and ``*_delta`` is
    ``mean(last 2 games) - mean(the 2 games before that)`` — positive means
    rising usage. A delta is ``None`` when either half has no data (fewer than
    three games played, or the metric is missing), and ``trend`` then reads
    ``"flat"``.

    ``trend`` keys off the snap-share delta when snaps joined, otherwise the
    target-share delta: ``>= threshold`` is ``"rising"``, ``<= -threshold`` is
    ``"declining"``.
    """
    if through_week is None:
        weeks = [r["week"] for r in rows if r.get("week") is not None]
        through_week = max(weeks) if weeks else 0

    by_player: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("week") is None or row["week"] > through_week:
            continue
        by_player.setdefault(row["player_id"], []).append(row)

    def _half_delta(values: list[float | None]) -> float | None:
        """Mean of the last ``USAGE_DELTA_SPLIT`` values minus the prior ones."""
        recent = [v for v in values[-USAGE_DELTA_SPLIT:] if v is not None]
        prior = [v for v in values[-2 * USAGE_DELTA_SPLIT : -USAGE_DELTA_SPLIT] if v is not None]
        if not recent or not prior:
            return None
        return round(sum(recent) / len(recent) - sum(prior) / len(prior), 4)

    out: list[dict[str, Any]] = []
    for player_id, player_rows in sorted(by_player.items()):
        player_rows.sort(key=lambda r: r["week"])
        recent = player_rows[-window:]
        snaps = [r.get("snap_pct") for r in recent]
        shares = [r.get("target_share") for r in recent]
        snap_delta = _half_delta(snaps)
        share_delta = _half_delta(shares)
        primary = snap_delta if snap_delta is not None else share_delta
        if primary is None:
            trend = "flat"
        elif primary >= threshold:
            trend = "rising"
        elif primary <= -threshold:
            trend = "declining"
        else:
            trend = "flat"
        out.append(
            {
                "player_id": player_id,
                "gsis_id": recent[-1].get("gsis_id"),
                "season": season,
                "through_week": through_week,
                "weeks_counted": len(recent),
                "last_week_played": recent[-1]["week"],
                "snap_pct_l4w": _mean([v for v in snaps if v is not None]),
                "target_share_l4w": _mean([v for v in shares if v is not None]),
                "rz_touches_l4w": round(sum(r.get("rz_touches") or 0 for r in recent), 2),
                "snap_pct_delta": snap_delta,
                "target_share_delta": share_delta,
                "trend": trend,
            }
        )
    logger.info(
        "built usage trends",
        extra={"players": len(out), "season": season, "through_week": through_week},
    )
    return out


def build_def_vs_pos(
    rows: list[dict[str, Any]],
    *,
    season: int,
    through_week: int | None = None,
    positions: Sequence[str] = DEF_VS_POS_POSITIONS,
) -> list[dict[str, Any]]:
    """Aggregate PPR fantasy points allowed by each defense to each position.

    Computed in polars from the weekly rows: total points scored *against* a team
    (``opponent``) by each position, divided by that team's games played.
    ``rank`` 1 is the most generous defense to the position, matching
    :func:`api.data.stats_store.get_def_vs_pos`.
    """
    if through_week is None:
        weeks = [r["week"] for r in rows if r.get("week") is not None]
        through_week = max(weeks) if weeks else 0
    wanted = {p.upper() for p in positions}

    records = [
        {
            "opponent": str(row["opponent"]).upper(),
            "position": str(row["position"]).upper(),
            "week": int(row["week"]),
            "points": float(row.get("fantasy_points_ppr") or 0.0),
        }
        for row in rows
        if row.get("opponent")
        and row.get("position")
        and str(row["position"]).upper() in wanted
        and row.get("week") is not None
        and row["week"] <= through_week
    ]
    if not records:
        logger.warning("no rows with an opponent/position; def_vs_pos not built")
        return []

    df = pl.DataFrame(
        records,
        schema={"opponent": pl.Utf8, "position": pl.Utf8, "week": pl.Int64, "points": pl.Float64},
    )
    games = df.group_by("opponent").agg(pl.col("week").n_unique().alias("games"))
    aggregated = (
        df.group_by(["opponent", "position"])
        .agg(pl.col("points").sum().alias("points_allowed"))
        .join(games, on="opponent", how="left")
        .with_columns((pl.col("points_allowed") / pl.col("games")).alias("ppg"))
        .with_columns(
            pl.col("ppg").rank(method="min", descending=True).over("position").alias("rank")
        )
        .sort(["opponent", "position"])
    )

    docs: dict[str, dict[str, Any]] = {}
    for row in _rows(aggregated):
        team = row["opponent"]
        doc = docs.setdefault(
            team,
            {"team": team, "season": season, "through_week": through_week, "positions": {}},
        )
        doc["positions"][row["position"]] = {
            "points_allowed_per_game": round(float(row["ppg"]), 2),
            "rank": int(row["rank"]),
            "points_allowed_total": round(float(row["points_allowed"]), 2),
            "games": int(row["games"]),
        }
    logger.info("built def_vs_pos", extra={"teams": len(docs), "through_week": through_week})
    return [docs[team] for team in sorted(docs)]


# --- schedules ------------------------------------------------------------


def _kickoff_iso(gameday: Any, gametime: Any) -> str | None:
    """Combine nflverse ``gameday`` (date) and ``gametime`` (ET ``HH:MM``) to UTC.

    Returns an ISO-8601 ``...Z`` timestamp, or ``None`` when the date is
    unusable. A missing/garbled ``gametime`` degrades to midnight ET.
    """
    if isinstance(gameday, datetime):
        date_part = gameday.date()
    elif hasattr(gameday, "year") and hasattr(gameday, "month"):
        date_part = gameday  # datetime.date
    else:
        text = _as_str(gameday)
        if not text:
            return None
        try:
            date_part = datetime.fromisoformat(text[:10]).date()
        except ValueError:
            return None

    kickoff_time = time(0, 0)
    text_time = _as_str(gametime)
    if text_time:
        try:
            parts = [int(p) for p in text_time.split(":")[:2]]
            kickoff_time = time(parts[0], parts[1] if len(parts) > 1 else 0)
        except (ValueError, IndexError):
            kickoff_time = time(0, 0)

    local = datetime.combine(date_part, kickoff_time, tzinfo=SCHEDULE_TZ)
    return local.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_schedule_docs(
    df: pl.DataFrame, *, season: int
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, str]]:
    """Build ``schedules/{season}_{week}`` docs and the ``meta/schedule_weeks`` map.

    Only regular-season games feed the week map (:mod:`api.core.week` uses it to
    decide "what week is it"); playoff rows are still written to the per-week
    schedule documents when present.

    Returns:
        ``([(doc_id, doc)], {week_str: first_game_iso})``.
    """
    rows = _rows(df)
    if not rows:
        return [], {}
    week_col = _column(df, ("week",))
    home_col = _column(df, ("home_team", "home"))
    away_col = _column(df, ("away_team", "away"))
    season_col = _column(df, ("season",))
    if week_col is None or home_col is None or away_col is None:
        raise ValueError(f"schedule frame missing week/home/away columns; got {df.columns}")
    gameday_col = _column(df, ("gameday", "game_date", "date"))
    gametime_col = _column(df, ("gametime", "game_time"))
    venue_col = _column(df, ("stadium", "venue", "stadium_id"))
    type_col = _column(df, ("game_type", "season_type"))
    id_col = _column(df, ("game_id",))

    by_week: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        if season_col is not None and _as_int(row.get(season_col)) != season:
            continue
        week = _as_int(row.get(week_col))
        home = _as_str(row.get(home_col))
        away = _as_str(row.get(away_col))
        if week is None or not home or not away:
            continue
        game: dict[str, Any] = {
            "home": home.upper(),
            "away": away.upper(),
            "kickoff": _kickoff_iso(
                row.get(gameday_col) if gameday_col else None,
                row.get(gametime_col) if gametime_col else None,
            ),
            "venue": _as_str(row.get(venue_col)) if venue_col else None,
        }
        if id_col:
            game["game_id"] = _as_str(row.get(id_col))
        if type_col:
            game["game_type"] = _as_str(row.get(type_col))
        by_week.setdefault(week, []).append(game)

    docs: list[tuple[str, dict[str, Any]]] = []
    week_map: dict[str, str] = {}
    for week, games in sorted(by_week.items()):
        games.sort(key=lambda g: (g["kickoff"] or "", g["home"]))
        kickoffs = [g["kickoff"] for g in games if g["kickoff"]]
        first_game = min(kickoffs) if kickoffs else None
        docs.append(
            (
                f"{season}_{week}",
                {
                    "season": season,
                    "week": week,
                    "first_game": first_game,
                    "games": games,
                },
            )
        )
        regular = [g for g in games if g.get("game_type", "REG") == "REG" and g["kickoff"]]
        if regular:
            week_map[str(week)] = min(g["kickoff"] for g in regular)
    logger.info("built schedule", extra={"season": season, "weeks": len(docs)})
    return docs, week_map


# --- injuries & depth charts ---------------------------------------------


def build_injury_docs(df: pl.DataFrame, *, season: int) -> list[dict[str, Any]]:
    """Reduce the injury report to the report week's row per player.

    Only rows from the frame's latest week (the current report week) are kept.
    Keeping each player's own latest row instead would carry a week-2 "Out"
    forward for a player who has been healthy ever since (healthy players are
    simply absent from later reports), and that stale status would overwrite
    Sleeper's fresher one on ``players/`` until the next nightly sync.

    The document shape is specified in :mod:`api.data.stats_store`; the status
    is additionally merged onto ``players/{player_id}.injury_status`` by
    :func:`write_injuries` while the report is the current week's.
    """
    rows = _rows(df)
    if not rows:
        return []
    gsis_col = _column(df, _GSIS_COLUMNS)
    if gsis_col is None:
        logger.warning("injury frame has no gsis id column; skipping", extra={"cols": df.columns})
        return []
    week_col = _column(df, ("week",))
    team_col = _column(df, _TEAM_COLUMNS)
    pos_col = _column(df, _POSITION_COLUMNS)
    name_col = _column(df, _NAME_COLUMNS)
    status_col = _column(df, ("report_status", "game_status", "status"))
    primary_col = _column(df, ("report_primary_injury", "primary_injury"))
    practice_col = _column(df, ("practice_status",))
    modified_col = _column(df, ("date_modified", "last_modified"))
    season_col = _column(df, ("season",))

    in_season = [
        row for row in rows if season_col is None or _as_int(row.get(season_col)) == season
    ]
    report_week: int | None = None
    if week_col is not None:
        weeks = [w for w in (_as_int(row.get(week_col)) for row in in_season) if w is not None]
        report_week = max(weeks) if weeks else None

    latest: dict[str, dict[str, Any]] = {}
    for row in in_season:
        gsis_id = _as_str(row.get(gsis_col))
        if not gsis_id:
            continue
        week = _as_int(row.get(week_col)) if week_col else None
        if report_week is not None and week != report_week:
            continue
        if gsis_id in latest:
            continue
        latest[gsis_id] = {
            "gsis_id": gsis_id,
            "season": season,
            "week": week,
            "team": (_as_str(row.get(team_col)) or "").upper() or None if team_col else None,
            "position": (_as_str(row.get(pos_col)) or "").upper() or None if pos_col else None,
            "name": _as_str(row.get(name_col)) if name_col else None,
            "injury_status": _as_str(row.get(status_col)) if status_col else None,
            "injury_detail": _as_str(row.get(primary_col)) if primary_col else None,
            "practice_status": _as_str(row.get(practice_col)) if practice_col else None,
            "updated_at": _as_str(row.get(modified_col)) if modified_col else None,
        }
    logger.info("built injury docs", extra={"players": len(latest), "season": season})
    return [latest[k] for k in sorted(latest)]


def build_depth_chart_docs(df: pl.DataFrame, *, season: int) -> list[dict[str, Any]]:
    """Build ``depth_charts/{team}`` from the most recent chart in the frame.

    Two upstream shapes are handled, because nflverse changed this table in 2025:

    * the current one — one row per roster slot per **snapshot**, with a ``dt``
      timestamp, ``team``, ``pos_abb`` and ``pos_rank`` and *no* season or week
      columns (554k rows for a season, 221 snapshots);
    * the legacy one — ``season``/``week``/``club_code``/``position``/``depth_team``.

    Either way only the latest snapshot (or week) survives, and only the skill
    positions in :data:`DEPTH_CHART_POSITIONS` — otherwise a team document would
    carry every defensive slot of every snapshot in the season.

    Shape (specified in :mod:`api.data.stats_store`, read by
    :func:`~api.data.stats_store.get_depth_chart`): ``{"team", "season",
    "week", "updated_at", "positions": {"RB": [{"player_id"(gsis), "name",
    "rank"}]}}``. Powers handcuff/next-man-up reasoning in ``/v1/report``.
    """
    if df is None or df.height == 0:
        return []
    team_col = _column(df, _TEAM_COLUMNS)
    pos_col = _column(df, _POSITION_COLUMNS)
    if team_col is None or pos_col is None:
        logger.warning("depth chart frame missing team/position columns", extra={"c": df.columns})
        return []
    week_col = _column(df, ("week",))
    snapshot_col = _column(df, ("dt", "last_updated", "updated"))
    rank_col = _column(df, ("pos_rank", "depth_team", "depth_chart_order", "rank"))
    name_col = _column(df, _NAME_COLUMNS)
    gsis_col = _column(df, _GSIS_COLUMNS)
    season_col = _column(df, ("season",))

    scoped = df
    if season_col is not None:
        scoped = scoped.filter((pl.col(season_col) == season) | pl.col(season_col).is_null())
    # Keep one chart: the latest snapshot timestamp, else the latest week.
    snapshot = None
    if snapshot_col is not None and scoped.height:
        snapshot = scoped[snapshot_col].max()
        scoped = scoped.filter(pl.col(snapshot_col) == snapshot)
    latest_week = None
    if week_col is not None and scoped.height:
        latest_week = _as_int(scoped[week_col].max())
        if latest_week is not None:
            scoped = scoped.filter(pl.col(week_col) == latest_week)

    teams: dict[str, dict[str, Any]] = {}
    for row in _rows(scoped):
        team = (_as_str(row.get(team_col)) or "").upper()
        position = (_as_str(row.get(pos_col)) or "").upper()
        if not team or position not in DEPTH_CHART_POSITIONS:
            continue
        doc = teams.setdefault(
            team,
            {
                "team": team,
                "season": season,
                "week": latest_week,
                "updated_at": _as_str(snapshot),
                "positions": {},
            },
        )
        doc["positions"].setdefault(position, []).append(
            {
                "player_id": _as_str(row.get(gsis_col)) if gsis_col else None,
                "name": _as_str(row.get(name_col)) if name_col else None,
                "rank": _as_int(row.get(rank_col)) if rank_col else None,
            }
        )
    for doc in teams.values():
        for entries in doc["positions"].values():
            entries.sort(key=lambda e: (e["rank"] is None, e["rank"] or 0))
    logger.info(
        "built depth charts",
        extra={"teams": len(teams), "week": latest_week, "snapshot": str(snapshot)},
    )
    return [teams[t] for t in sorted(teams)]


# --- orchestration --------------------------------------------------------


async def ingest_nflverse(
    store: Store,
    *,
    loaders: Loaders | None = None,
    season: int | None = None,
    settings: Settings | None = None,
    stats_only: bool = False,
) -> dict[str, Any]:
    """Run the full nflverse ingest for one season.

    Core datasets (weekly stats, usage trends, def-vs-pos, schedules) propagate
    their failures; the supplemental ones (snap counts, injuries, depth charts)
    are wrapped so an upstream schema change degrades the run instead of failing
    it.

    ``stats_only`` is the prior-season rebuild path. It writes the three
    stat-derived datasets (weekly stats, usage trends, def-vs-pos) and nothing
    else: no schedule, no preseason-gap marker, no injuries, no depth charts.
    Every one of those four is *current-season* truth that a run for last
    season would overwrite with last season's — a 2025 schedule trips the
    season guard and 503s every paid route (DESIGN_NOTES §20), and 2025 depth
    charts are what the Week 1 boards lean on. The case it exists for: the
    gsis bridge (§23) landed after the 2025 rollups were built, so the top of
    the draft board had no usage until they were rebuilt with the fixed ids.

    Args:
        store: Destination store.
        loaders: Injected loaders; :func:`default_loaders` when omitted.
        season: Season override; :func:`resolve_season` when omitted.
        settings: Settings used only for the season fallback.
        stats_only: Rebuild only the stat-derived datasets (see above). The
            stats file must exist: a missing one is an error, never a gap.

    Returns:
        A counts dict, also emitted as the job's structured log summary.
    """
    loaders = loaders or default_loaders()
    season = resolve_season(settings, season)
    seasons = [season]
    summary: dict[str, Any] = {"season": season, "stats_only": stats_only}

    if stats_only:
        await _refuse_prior_season_rebuild_after_kickoff(store, season)
        # A rebuild of a season's stats is by definition a season that has
        # been played; a missing file is a failure, never the calendar.
        summary["schedules"] = 0
        summary["schedule_weeks"] = 0
        kicked_off = True
    else:
        # Schedule first. It is the one dataset nflverse publishes *before*
        # the season's stats file exists, and it is what tells us which of
        # those two situations we are in below.
        schedule_summary = await ingest_schedule(store, loaders=loaders, season=season)
        summary["schedules"] = schedule_summary["schedules"]
        summary["schedule_weeks"] = schedule_summary["schedule_weeks"]
        kicked_off = season_has_kicked_off(schedule_summary.get("first_kickoff"))

    # Before the first game, nflverse has no ``stats_player_week_{season}``
    # file to publish and the download 404s. That is the calendar, not an
    # outage — and the alternative is what happened on 2026-09-01, when
    # ingesting *last* season instead took the paid API down (DESIGN_NOTES §20).
    # After kickoff the same 404 is a real failure and still raises.
    preseason_gap = False
    try:
        rows = transform_weekly_stats(loaders.player_stats(seasons), season=season)
    except Exception:
        if kicked_off:
            raise
        logger.warning(
            "no %s player stats published yet and the season has not kicked off; "
            "ingesting everything else and leaving stat freshness untouched",
            season,
            exc_info=True,
        )
        rows = []
        preseason_gap = True
    if not rows and not preseason_gap:
        if kicked_off:
            raise ValueError(f"nflverse returned no {season} player stats after kickoff")
        logger.warning(
            "nflverse returned an empty %s player-stats snapshot before kickoff; "
            "leaving stat freshness untouched",
            season,
        )
        preseason_gap = True
    summary["preseason_gap"] = preseason_gap
    # Record *which* datasets upstream cannot supply, so the API can tell
    # "not published yet" from "the ingest job died". Re-affirmed on every run
    # and cleared the moment real stats arrive, so it cannot outlive its cause.
    # A prior-season rebuild leaves it alone: the marker describes the current
    # season, and clearing it from 2025 data would end the exemption that
    # keeps the 2026 preseason from 503ing (§22).
    if not stats_only:
        await write_preseason_gap(
            store, season=season, datasets=STAT_DERIVED_DATASETS if preseason_gap else []
        )

    players_df: pl.DataFrame | None = None
    try:
        players_df = loaders.players()
    except Exception:
        logger.warning("load_players() failed; snap counts and positions degrade", exc_info=True)

    summary["positions_filled"] = enrich_positions(rows, build_gsis_to_position(players_df))
    try:
        summary["snaps"] = attach_snap_pct(
            rows, loaders.snap_counts(seasons), build_pfr_to_gsis(players_df)
        )
    except Exception:
        logger.warning("snap count ingest failed; continuing without snap_pct", exc_info=True)
        summary["snaps"] = {"joined": 0, "unjoinable": 0, "rows": len(rows)}

    try:
        summary["red_zone"] = attach_rz_touches(
            rows, build_red_zone_touches(loaders.pbp(seasons), season=season)
        )
    except Exception:
        logger.warning("red-zone ingest failed; rz_touches stay 0", exc_info=True)
        summary["red_zone"] = 0

    summary["ids"] = resolve_player_ids(rows, await load_id_map(store))

    weeks = [r["week"] for r in rows]
    through_week = max(weeks) if weeks else 0
    summary["through_week"] = through_week
    summary["weekly_stats"] = await write_weekly_stats(store, rows, season=season)

    usage = build_usage_trends(rows, season=season, through_week=through_week)
    summary["usage_trends"] = await write_docs(
        store, USAGE_TRENDS_COLLECTION, [(d["player_id"], d) for d in usage]
    )

    defense = build_def_vs_pos(rows, season=season, through_week=through_week)
    summary["def_vs_pos"] = await write_docs(
        store, DEF_VS_POS_COLLECTION, [(d["team"], d) for d in defense]
    )

    # Stamping freshness for datasets we did not write would report last
    # season's numbers as today's — the exact lie the season guard exists to
    # catch. The documents already in the store stay; only the marker is
    # withheld, so nothing starts 503ing over a gap that is just the calendar.
    datasets = [] if preseason_gap else ["weekly_stats", "usage_trends", "def_vs_pos"]

    if stats_only:
        # Injuries and depth charts are current-season truth. Last season's
        # would overwrite what the boards lean on this week.
        summary["injuries"] = 0
        summary["depth_charts"] = 0
        await update_freshness(store, datasets)
        logger.info("nflverse stats-only rebuild complete", extra=summary)
        return summary

    try:
        injuries = build_injury_docs(loaders.injuries(seasons), season=season)
        # Resolved from the schedule written above, so it is this run's week.
        week_now = await current_week(store, settings)
        summary["injuries"] = await write_injuries(store, injuries, current_week=week_now)
        datasets.append("injuries")
    except Exception:
        logger.warning("injury ingest failed; continuing", exc_info=True)
        summary["injuries"] = 0

    try:
        charts = build_depth_chart_docs(loaders.depth_charts(seasons), season=season)
        summary["depth_charts"] = await write_docs(
            store, DEPTH_CHARTS_COLLECTION, [(d["team"], d) for d in charts]
        )
        datasets.append("depth_charts")
    except Exception:
        logger.warning("depth chart ingest failed; continuing", exc_info=True)
        summary["depth_charts"] = 0

    await update_freshness(store, datasets)
    logger.info("nflverse ingest complete", extra=summary)
    return summary


#: Datasets derived from the season's player-stats file. When that file does
#: not exist yet, none of them can be written by anyone.
STAT_DERIVED_DATASETS: tuple[str, ...] = ("weekly_stats", "usage_trends", "def_vs_pos", "injuries")


async def write_preseason_gap(
    store: Store, *, season: int, datasets: Iterable[str]
) -> dict[str, Any]:
    """Record (or clear) the datasets upstream cannot supply for ``season``.

    Written on every stats run so the marker is continuously re-affirmed; an
    empty ``datasets`` clears it, which is what a successful stats ingest does.
    :func:`api.data.stats_store.exempt_datasets` stops trusting a marker that
    stops being refreshed, so a dead ingest job cannot hide behind it.
    """
    names = sorted({d for d in datasets if d})
    doc = {"season": season, "datasets": names, "recorded_at": utc_now_iso()}
    await store.set(META_COLLECTION, PRESEASON_GAP_DOC_ID, doc)
    return doc


async def ingest_schedule(
    store: Store,
    *,
    loaders: Loaders | None = None,
    season: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Write one season's schedule without requiring that season's stats file.

    nflverse publishes the next schedule before it publishes weekly player
    stats. During that preseason window a full stats ingest legitimately 404s,
    but leaving ``meta/schedule_weeks`` on last season makes every paid route
    stale. This narrow task advances only schedule truth; it never relabels last
    season's statistics as current-season data.
    """
    loaders = loaders or default_loaders()
    season = resolve_season(settings, season)
    schedule_docs, week_map = build_schedule_docs(loaders.schedules([season]), season=season)
    if not week_map:
        raise ValueError(f"nflverse schedule for {season} has no regular-season weeks")

    written = await write_docs(store, SCHEDULES_COLLECTION, schedule_docs)
    await store.set(
        META_COLLECTION,
        SCHEDULE_WEEKS_DOC_ID,
        {"season": season, "weeks": week_map},
    )
    await update_freshness(store, ["schedules"])
    summary = {
        "season": season,
        "schedules": written,
        "schedule_weeks": len(week_map),
        # The earliest regular-season kickoff, so a caller can tell "this season
        # has not started" from "this season's data is missing".
        "first_kickoff": min(week_map.values()) if week_map else None,
    }
    logger.info("schedule ingest complete", extra=summary)
    return summary


async def _refuse_prior_season_rebuild_after_kickoff(store: Store, season: int) -> None:
    """Refuse a stats-only rebuild of an older season once the current one has started.

    ``usage_trends/`` and ``def_vs_pos/`` are flat, one doc per player or team,
    and the paid routes read them as *this* season's. Before kickoff that is the
    point (the draft board ranks on last season's usage); after it, the rebuild
    would replace this season's rollups with last season's and stamp them fresh
    — and that stamp would also satisfy the backtest's completeness gate.

    Raises:
        ValueError: When ``meta/schedule_weeks`` holds a later season that has
            kicked off.
    """
    doc = await store.get(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID) or {}
    current = doc.get("season")
    weeks = doc.get("weeks") or {}
    if not isinstance(current, int) or current <= season or not weeks:
        return
    if season_has_kicked_off(min(str(v) for v in weeks.values())):
        raise ValueError(
            f"refusing --stats-only for {season}: the {current} season has kicked off, and "
            f"usage_trends/def_vs_pos would be overwritten with {season} numbers stamped fresh"
        )


def season_has_kicked_off(first_kickoff: str | None, now: datetime | None = None) -> bool:
    """Whether the season's first regular-season game has started.

    An unparseable or absent kickoff reads as *started*, so a malformed schedule
    can never talk the ingest into tolerating a missing stats file.
    """
    if not first_kickoff:
        return True
    try:
        kickoff = datetime.fromisoformat(str(first_kickoff).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (now or datetime.now(UTC)) >= kickoff


async def write_injuries(
    store: Store, docs: list[dict[str, Any]], *, current_week: int | None = None
) -> int:
    """Write ``injuries/{player_id}`` and merge the status into ``players/``.

    Doc ids follow the same policy as weekly stats (Sleeper id when the gsis id
    resolves through ``id_map``). The merge into ``players/`` only touches
    documents that already exist — an injury row for a player Sleeper does not
    carry must never conjure a half-empty player document.

    The merge is skipped when the report is for a week before
    ``current_week``. The Tuesday stats run lands after the week rolls over
    (Tue 03:00 ET) but before the first practice report of the new week, so the
    latest report nflverse has is *last* week's game statuses. Merging it would
    stamp last Sunday's "Out" onto ``players/`` — where the engine benches on it
    and :mod:`api.data.predictions` refuses to file calls on it — until the
    nightly Sleeper sync overwrites it. ``injuries/`` is still written: it is
    the latest report on record, and it says which week it is for.

    Args:
        current_week: The week the store is serving
            (:func:`api.core.week.current_week`); ``None`` merges every report,
            which was the behaviour before the guard.
    """
    if not docs:
        return 0
    id_map = await load_id_map(store)
    injury_docs: list[tuple[str, dict[str, Any]]] = []
    player_updates: list[tuple[str, dict[str, Any]]] = []
    stale_reports = 0
    for doc in docs:
        entry = id_map.get(doc["gsis_id"])
        sleeper_id = _as_str(entry.get("sleeper_id")) if entry else None
        doc_id = sleeper_id or doc["gsis_id"]
        injury_docs.append((doc_id, {**doc, "player_id": doc_id}))
        # A null nflverse field never blanks what Sleeper already wrote: absence
        # of a value upstream is not evidence the player is healthy.
        update = {
            key: doc[key]
            for key in ("injury_status", "injury_detail", "practice_status")
            if doc.get(key) is not None
        }
        report_week = doc.get("week")
        if (
            update
            and current_week is not None
            and isinstance(report_week, int)
            and report_week < current_week
        ):
            stale_reports += 1
            continue
        if update and sleeper_id and await store.get(PLAYERS_COLLECTION, sleeper_id):
            player_updates.append((sleeper_id, update))
    written = await write_docs(store, INJURIES_COLLECTION, injury_docs)
    merged = await write_docs(store, PLAYERS_COLLECTION, player_updates, merge=True)
    logger.info(
        "wrote injuries",
        extra={
            "injuries": written,
            "players_merged": merged,
            "stale_report_rows_not_merged": stale_reports,
            "current_week": current_week,
        },
    )
    return written
