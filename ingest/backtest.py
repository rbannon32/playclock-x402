"""The backtest task: score last week's claims against what actually happened.

:mod:`api.data.predictions` files every claim a paid answer made. This task
runs after ``stats`` has written a week's nflverse lines and answers the one
question the golden queries cannot: *was the call right?* The result is
published, unedited, on ``GET /v1/stats`` — a hit rate that nobody has to take
on trust is the honest version of social proof, and a hit rate that is bad is
the earliest possible warning that the product is not worth its price.

Scoring rules
-------------
The bar is *startable*: the Nth-best PPR score at the position that week, with
N from :data:`STARTABLE_RANK` (a twelve-team league's starting lineup, flex
folded into RB/WR). Then:

* ``start``, ``add``, ``sleeper``, ``waiver``, ``emerging`` hit when the player
  scored at or above the bar;
* ``sit``, ``fade`` hit when he scored below it;
* ``matchup_top`` hits when the player ranked first scored at least as much as
  everyone else in the group (a tie is not a wrong call);
* a claim with no stat line under its Sleeper id is looked up again under the
  player's ``players/{id}.gsis_id``: the ``stats`` task keys a line by the raw
  gsis id whenever ``id_map`` has no Sleeper id for it, and scoring that
  player as a scratch made every add on him a permanent miss and every fade a
  permanent hit (:func:`alias_gsis_lines`);
* a player with a gsis id but no stat line under either id did not play and
  scored zero — a start call on a scratch is a miss, a sit call on one is a
  hit. The call was filed before kickoff on a player who was not listed out
  and whose team had a game (:mod:`api.data.predictions` refuses the rest), so
  a surprise inactive is a real outcome of the call, not a free one;
* a player with no stat line and **no gsis id at all** (or no ``players/`` doc)
  cannot be joined to nflverse, so "no line" says nothing about whether he
  played: the claim is marked scored with ``hit = null`` and ``points = null``,
  like a kicker, and stays out of every rate. A matchup with such a player in
  its group is unscorable the same way;
* a position with no threshold (a stat line the ingest could not place), and
  any claim on a kicker or defense, is marked scored with ``hit = null`` and
  stays out of every rate.

**A week is scored only once it is complete, and only from a stats snapshot
taken after it ended.** Scoring is permanent — a scored claim is never
revisited — so two things must both be true before a week is graded:

* every kickoff on the week's ingested schedule is at least
  :data:`WEEK_COMPLETE_GRACE` in the past (a week with no ingested schedule
  is never complete), or a Thursday-only stats file grades every Sunday
  starter as a scratch forever (PR #31 review); and
* the ``weekly_stats`` freshness marker — stamped by the ``stats`` task only
  at the end of a successful run — is itself later than that same moment.
  Without this, a backtest that overlaps a running ``stats`` task reads a
  half-written collection: the lines present set a bar, the lines not yet
  written score as scratches, and neither is ever corrected (PR #32 review).
  The marker also means "the run that wrote this week finished", which a
  non-empty collection does not.

The marker proves a run finished, not that nflverse's file covered the week:
a run that lands before Monday night is published stamps it all the same. So
the data is checked too: every team on the week's schedule must have at
least one stat line (:func:`teams_missing_lines`; bye teams are not on the
schedule). In the schedulers the task runs *inside* the stats invocation
(``--task stats,backtest``), so the marker check is the belt to that
ordering's braces.

Kickers and team defenses are never scored (:data:`UNSCORABLE_POSITIONS`):
the ingested fantasy points count no kicking, so every kicker would score
zero and every kicker add would hit.

A prediction is scored once. Re-running the task re-derives the summary from
every scored document for the season, so the published numbers are always the
whole record and never one run's slice.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from api.core.clock import utcnow
from api.core.config import Settings
from api.core.store import Store
from api.core.week import current_season, parse_ts
from api.data.predictions import (
    BACKTEST_COLLECTION,
    NEGATIVE_KINDS,
    POSITIVE_KINDS,
    PREDICTIONS_COLLECTION,
)
from api.data.stats_store import (
    PLAYERS_COLLECTION,
    SCHEDULES_COLLECTION,
    get_data_freshness,
    weekly_stats_collection,
)

logger = logging.getLogger(__name__)

#: How long after a week's last kickoff it counts as played. Monday Night
#: Football kicks off around 00:15 UTC Tuesday and ends by 04:00; the stats
#: cron runs at 13:01 UTC, so eight hours clears the last game with room to
#: spare and never lets a Thursday-only stats file score the whole week.
WEEK_COMPLETE_GRACE = timedelta(hours=8)


async def week_played_by(store: Store, season: int, week: int) -> datetime | None:
    """The moment ``week`` counts as played: its last kickoff plus the grace.

    Reads ``schedules/{season}_{week}``. ``None`` for a missing schedule or
    one with no parseable kickoffs — the cost of waiting a run is nothing, the
    cost of scoring early is a permanently wrong record.
    """
    doc = await store.get(SCHEDULES_COLLECTION, f"{season}_{week}")
    if not doc:
        return None
    kickoffs = [
        parse_ts(game.get("kickoff")) for game in doc.get("games") or [] if isinstance(game, dict)
    ]
    known = [stamp for stamp in kickoffs if stamp is not None]
    if not known:
        return None
    return max(known) + WEEK_COMPLETE_GRACE


async def stats_snapshot_at(store: Store) -> datetime | None:
    """When the ``stats`` task last finished, from ``meta/freshness``.

    The marker is written only at the end of a successful run, so it doubles
    as "the collection is whole" — which a non-empty collection does not.
    """
    freshness = await get_data_freshness(store)
    return parse_ts(freshness.get("weekly_stats"))


async def week_complete(store: Store, season: int, week: int, now: datetime) -> bool:
    """Whether ``week`` has been played *and* a stats run has finished since.

    Both halves are required; see the module docstring for why each one on
    its own scores a week wrongly and permanently.
    """
    played_by = await week_played_by(store, season, week)
    if played_by is None or now < played_by:
        return False
    snapshot = await stats_snapshot_at(store)
    return snapshot is not None and snapshot >= played_by


async def teams_missing_lines(
    store: Store, season: int, week: int, lines: list[dict[str, Any]]
) -> list[str]:
    """Teams on the week's schedule with no stat line in ``lines``, sorted.

    The freshness marker says a ``stats`` run *finished*, not that the nflverse
    file it read covered the whole week: a run that lands before nflverse has
    published Monday night stamps the marker all the same, and every MNF
    player would then score as a scratch — permanently. Every team that played
    has at least one offensive stat line (its quarterback), so a scheduled team
    with none means the source file is still short. Bye teams are not on the
    week's schedule and are not asked for.

    A stat line with no ``team`` cannot vouch for anyone, so a collection
    written without team fields waits rather than guessing. An unreadable
    schedule returns an empty list: :func:`week_complete` already refuses it.
    """
    doc = await store.get(SCHEDULES_COLLECTION, f"{season}_{week}")
    played: set[str] = set()
    for game in (doc or {}).get("games") or []:
        if not isinstance(game, dict):
            continue
        for side in ("home", "away"):
            team = str(game.get(side) or "").strip().upper()
            if team:
                played.add(team)
    present = {str(line.get("team") or "").strip().upper() for line in lines}
    return sorted(played - present)


#: Startable cut line per position: the Nth-best scorer that week sets the bar.
#: Kickers and team defenses are deliberately absent; see
#: :data:`UNSCORABLE_POSITIONS`.
STARTABLE_RANK: dict[str, int] = {"QB": 12, "RB": 24, "WR": 24, "TE": 12}

#: Positions whose fantasy points the ingested stat lines cannot express.
#: nflverse's ``fantasy_points``/``fantasy_points_ppr`` count passing, rushing
#: and receiving only, and the ingest keeps no kicking columns (no FG or PAT
#: makes, see ``ingest.nflverse_ingest.STAT_COLUMNS``); there are no team
#: defense lines at all. Every kicker therefore "scored" about zero: an add
#: always hit (0 >= a bar of 0) and a fade always missed. A claim at one of
#: these positions is scored ``hit = null`` and stays out of every rate, and
#: :func:`summarize` excludes one even if an earlier run stored a boolean.
UNSCORABLE_POSITIONS: frozenset[str] = frozenset({"K", "PK", "DEF", "DST", "D/ST"})


def line_points(line: dict[str, Any]) -> float:
    """PPR points from one stat line, falling back to standard scoring, then zero."""
    for key in ("fantasy_points_ppr", "fantasy_points"):
        value = line.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return 0.0


def startable_thresholds(lines: list[dict[str, Any]]) -> dict[str, float]:
    """The startable bar per position for one week of stat lines.

    With fewer lines at a position than :data:`STARTABLE_RANK` asks for (a
    fixture, or the first week of a thin dataset) the lowest score on record is
    the bar: everyone who played counts as startable. A position with no lines
    has no bar and is left out.
    """
    by_position: dict[str, list[float]] = defaultdict(list)
    for line in lines:
        position = str(line.get("position") or "").upper()
        if position in STARTABLE_RANK:
            by_position[position].append(line_points(line))
    thresholds: dict[str, float] = {}
    for position, scores in by_position.items():
        scores.sort(reverse=True)
        index = min(STARTABLE_RANK[position], len(scores)) - 1
        thresholds[position] = scores[index]
    return thresholds


async def alias_gsis_lines(
    store: Store,
    player_ids: set[str],
    points_by_player: dict[str, float],
    positions_by_player: dict[str, str],
) -> set[str]:
    """Find the stat lines of claimed players whose line is keyed by gsis id.

    For every id in ``player_ids`` with no entry in ``points_by_player``, reads
    ``players/{id}.gsis_id``; a line under that gsis id is aliased onto the
    Sleeper id, in place, in both maps.

    Returns:
        The ids with no line that cannot be joined to nflverse at all (no
        ``players/`` doc, or no gsis id on it): their absence from the stat
        file is not evidence that they did not play.
    """
    unjoinable: set[str] = set()
    for player_id in sorted(player_ids):
        if not player_id or player_id in points_by_player:
            continue
        player = await store.get(PLAYERS_COLLECTION, player_id)
        gsis_id = str((player or {}).get("gsis_id") or "").strip()
        if not gsis_id:
            unjoinable.add(player_id)
            continue
        if gsis_id in points_by_player:
            points_by_player[player_id] = points_by_player[gsis_id]
            if gsis_id in positions_by_player:
                positions_by_player[player_id] = positions_by_player[gsis_id]
    return unjoinable


def score_prediction(
    doc: dict[str, Any],
    points_by_player: dict[str, float],
    thresholds: dict[str, float],
    positions_by_player: dict[str, str] | None = None,
    unjoinable: set[str] | frozenset[str] = frozenset(),
) -> tuple[bool | None, float | None]:
    """Score one claim.

    Args:
        doc: A ``predictions/`` document.
        points_by_player: ``player_id -> points`` for the week. Absent ids
            scored zero (a scratch), unless they are in ``unjoinable``.
        thresholds: From :func:`startable_thresholds`.
        positions_by_player: ``player_id -> position`` from the stat lines,
            used when the claim itself carries no position.
        unjoinable: Ids with no line that cannot be matched to one (from
            :func:`alias_gsis_lines`); a claim on one, or a matchup including
            one, is unscorable.

    Returns:
        ``(hit, points)`` — ``hit`` is ``None`` when the claim cannot be scored:
        its position has no threshold, it (or, for a matchup, any member of its
        group) is at a position in :data:`UNSCORABLE_POSITIONS`, or it (or a
        group member) is unjoinable. ``points`` is ``None`` only when the
        claim's own player is unjoinable: no line exists to read it from.
    """
    kind = str(doc.get("kind") or "")
    player_id = str(doc.get("player_id") or "")
    if player_id in unjoinable and player_id not in points_by_player:
        return None, None
    points = points_by_player.get(player_id, 0.0)
    known_positions = positions_by_player or {}

    position = str(doc.get("position") or "").upper()
    if not position:
        position = str(known_positions.get(player_id) or "").upper()
    if position in UNSCORABLE_POSITIONS:
        return None, points

    if kind == "matchup_top":
        group = [str(g) for g in doc.get("group") or []]
        if any(str(known_positions.get(g) or "").upper() in UNSCORABLE_POSITIONS for g in group):
            return None, points
        if any(g in unjoinable and g not in points_by_player for g in group):
            return None, points
        best = max((points_by_player.get(g, 0.0) for g in group), default=points)
        return points >= best, points

    bar = thresholds.get(position)
    if bar is None:
        return None, points
    if kind in POSITIVE_KINDS:
        return points >= bar, points
    if kind in NEGATIVE_KINDS:
        return points < bar, points
    return None, points


def summarize(season: int, scored: list[dict[str, Any]], *, now: str) -> dict[str, Any]:
    """Aggregate every scored document for ``season`` into the published summary.

    Documents with ``hit = null`` are counted under ``unscorable`` and excluded
    from every rate, and so is any document at a position in
    :data:`UNSCORABLE_POSITIONS` whatever its stored ``hit`` — kicker claims
    were scored as booleans before those positions were excluded, and the
    stored verdicts measure nothing.
    """
    rated = [
        doc
        for doc in scored
        if isinstance(doc.get("hit"), bool)
        and str(doc.get("position") or "").upper() not in UNSCORABLE_POSITIONS
    ]

    def bucket(docs: list[dict[str, Any]]) -> dict[str, Any]:
        hits = sum(1 for doc in docs if doc["hit"])
        rate = round(hits / len(docs), 3) if docs else None
        return {"scored": len(docs), "hits": hits, "hit_rate": rate}

    def grouped(field: str) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in rated:
            groups[str(doc.get(field) or "unknown")].append(doc)
        return [{"key": key, **bucket(docs)} for key, docs in sorted(groups.items())]

    return {
        "season": season,
        "updated_at": now,
        "weeks": sorted({int(doc["week"]) for doc in rated if doc.get("week") is not None}),
        "overall": bucket(rated),
        "by_endpoint": grouped("endpoint"),
        "by_kind": grouped("kind"),
        "unscorable": len(scored) - len(rated),
    }


async def run_backtest(
    store: Store,
    settings: Settings,
    *,
    season: int | None = None,
    week: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Score every unscored claim whose week is complete and has stat lines.

    Args:
        store: Backing store.
        settings: Active settings (season fallback).
        season: Season to score; defaults to the ingested season.
        week: Score only this week. Default: every week with unscored claims.
        now: The moment of the run; defaults to :func:`api.core.clock.utcnow`.

    Returns:
        The summary written to ``backtest/{season}``, plus ``scored_this_run``
        and ``waiting_on_stats`` (weeks with claims that are not yet complete
        or have no stat lines yet).
    """
    resolved = season if season is not None else await current_season(store, settings)
    moment = now or utcnow()
    now = moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    unscored = await store.list(
        PREDICTIONS_COLLECTION, where=[("season", "==", resolved), ("scored", "==", False)]
    )
    by_week: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for doc in unscored:
        doc_week = doc.get("week")
        if isinstance(doc_week, int) and (week is None or doc_week == week):
            by_week[doc_week].append(doc)

    scored_now = 0
    waiting: list[int] = []
    for doc_week in sorted(by_week):
        if not await week_complete(store, resolved, doc_week, moment):
            waiting.append(doc_week)
            logger.info("backtest: %sw%s is not complete yet; not scoring", resolved, doc_week)
            continue
        lines = await store.list(weekly_stats_collection(resolved, doc_week))
        if not lines:
            waiting.append(doc_week)
            logger.info("backtest: no stat lines yet for %sw%s", resolved, doc_week)
            continue
        missing = await teams_missing_lines(store, resolved, doc_week, lines)
        if missing:
            waiting.append(doc_week)
            logger.warning(
                "backtest: %sw%s has no stat lines for %s; the stats source is not "
                "complete for the week yet, not scoring",
                resolved,
                doc_week,
                ", ".join(missing),
            )
            continue
        points_by_player: dict[str, float] = {}
        positions_by_player: dict[str, str] = {}
        for line in lines:
            player_id = str(line.get("player_id") or line.get("_id") or "")
            if not player_id:
                continue
            points_by_player[player_id] = line_points(line)
            if line.get("position"):
                positions_by_player[player_id] = str(line["position"]).upper()
        thresholds = startable_thresholds(lines)
        claimed = {str(doc.get("player_id") or "") for doc in by_week[doc_week]}
        claimed |= {str(g) for doc in by_week[doc_week] for g in doc.get("group") or []}
        unjoinable = await alias_gsis_lines(store, claimed, points_by_player, positions_by_player)

        for doc in by_week[doc_week]:
            hit, points = score_prediction(
                doc, points_by_player, thresholds, positions_by_player, unjoinable
            )
            await store.set(
                PREDICTIONS_COLLECTION,
                str(doc["_id"]),
                {"scored": True, "hit": hit, "points": points, "scored_at": now},
                merge=True,
            )
            scored_now += 1
        logger.info(
            "backtest: scored %d claim(s) for %sw%s", len(by_week[doc_week]), resolved, doc_week
        )

    scored = await store.list(
        PREDICTIONS_COLLECTION, where=[("season", "==", resolved), ("scored", "==", True)]
    )
    summary = summarize(resolved, scored, now=now)
    await store.set(BACKTEST_COLLECTION, str(resolved), summary)

    summary["scored_this_run"] = scored_now
    summary["waiting_on_stats"] = waiting
    logger.info(
        "backtest: %s overall %s/%s, %d newly scored, waiting on weeks %s",
        resolved,
        summary["overall"]["hits"],
        summary["overall"]["scored"],
        scored_now,
        waiting,
    )
    return summary
