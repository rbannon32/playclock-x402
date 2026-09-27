"""The prediction archive: what every paid answer claimed, kept so it can be scored.

Why this exists
---------------
The golden queries (:mod:`api.evals.golden`) prove a paid answer is *honest*:
the schema validates, the right player resolved, every number traces back to
the store. Nothing proves it was *right*. A start/sit call that is always
defensible and usually wrong is still worthless to the manager who paid for
it, and until this module existed nothing in the stack could tell the two
apart.

So every response body is mined for its scoreable claims the moment it is
produced and the claims are filed under ``predictions/``. :mod:`ingest.backtest`
scores them once the week's stat lines land, and ``GET /v1/stats`` publishes
the hit rate. Three properties matter more than the extraction details:

* **First write wins — per player, per endpoint, per week, whatever the
  direction.** A board is re-warmed several times a day and a personalized
  answer can be regenerated on a retry. The claim that gets scored is the one
  that was made first, never a revision written after the games — and a
  revision that flips the call ("add" on Tuesday, "fade" on Wednesday) is a
  revision too: filing both would score both, and one of the pair always
  hits. The id carries no kind (see :func:`prediction_id`), docs go in through
  :meth:`~api.core.store.Store.create`, which refuses to overwrite, and an
  old-format id on the same player (:func:`legacy_prediction_ids`) blocks the
  write as well. A re-warm is a no-op here.
* **Nothing is filed after kickoff.** First-write-wins is no protection when
  the first write is itself late: the paid routes accept any week 1-18, so a
  request for week 3 made on the Tuesday after it can produce a "start" call
  that already knows the score. :func:`record_predictions` looks up the
  week's first kickoff on the ingested schedule and files nothing once it has
  passed — and files nothing when no schedule is ingested, because a claim
  that cannot be shown to predate the games is not a prediction. The kickoff
  it beat is stamped on the doc as ``first_game``. (Raised in review on PR
  #31; the published hit rate must never include hindsight.)
* **Nothing is filed for a future week.** The paid routes accept any week, so
  a request for week 17 made in week 3 builds its calls on week-3 data — and
  first-write-wins would then block the real week-17 calls for good. A claim
  is filed only for the current week (:func:`api.core.week.current_week`) or
  a week whose first kickoff is within :data:`CLAIM_HORIZON`.
* **Nothing is filed on a player who is out.** A "sit" on an IR'd player is a
  free hit and a "start" a free miss; either way the outcome was decided before
  the call and would inflate (or sink) the published hit rate. Claims whose
  ``players/{id}.injury_status`` is an out status
  (:data:`api.data.stats_store.OUT_STATUSES`) are skipped.
* **Nothing is filed on a player who has no game that week.** A player whose
  ``players/{id}.team`` is empty (a free agent, a retiree) or whose team is not
  on the week's ingested schedule (a bye) has no stat line to score, and the
  backtest scores a missing line as zero: a "sit" or "fade" on him is a free
  hit. His claims are skipped — and a matchup whose group includes such a
  player is skipped too, since out-scoring zero is not a call.
* **It never raises into the paid path.** Archiving is bookkeeping. A store
  hiccup must not cost a caller an answer they have already paid for, so
  :func:`record_predictions` logs at WARNING and returns rather than raising.
* **It is read-only over the body.** Nothing here changes what the caller sees.

What is scored, per endpoint
----------------------------
================  ==============  ================================================
endpoint          kind            the claim
================  ==============  ================================================
``player``        start / sit     inferred from the verdict's own words; skipped
                                  when the verdict is a hold
``matchup``       matchup_top     the rank-1 player out-scores the rest of the group;
                                  skipped when the answer sits him (out, or on bye)
``roster``        start / sit     every ``start_sit`` call (flex counts as start,
                                  bench as sit)
``trending``      add / fade      per row; ``hold`` is no claim and is skipped
``sleepers``      sleeper         every pick
``waivers``       waiver          the first :data:`WAIVER_TOP_N` rows marked
                                  start or streamer
``report``        emerging        every emerging callout that resolved to an id
``draft_board``   —               season-scoped, nothing weekly to score
``draft_report``  —
``team_report``   —               narrates supplied analytics; makes no call
================  ==============  ================================================

Doc shape, id ``{season}w{week}:{endpoint}:{player_id}`` (a matchup's id is
``{season}w{week}:matchup:matchup_top:{digest}`` over its sorted group ids;
single-player claims filed before 2026-09-24 carry the kind as well,
``{season}w{week}:{endpoint}:{kind}:{player_id}``)::

    {
      "season": 2026, "week": 4, "endpoint": "matchup", "kind": "matchup_top",
      "player_id": "1001", "name": "Bijan Robinson", "position": "RB",
      "group": ["1001", "1002"],            # matchup only: every id ranked
      "recorded_at": "2026-09-24T13:00:00Z",
      "first_game": "2026-09-25T00:15:00Z",  # the kickoff this claim predates
      "scored": false, "hit": null, "points": null
    }

``scored``/``hit``/``points`` are filled in by the backtest; ``hit`` stays
``null`` after scoring only when the position has no startable threshold.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel

from api.core.clock import utcnow
from api.core.config import Settings, get_settings
from api.core.store import Store
from api.core.week import current_season, current_week, parse_ts
from api.data.stats_store import PLAYERS_COLLECTION, SCHEDULES_COLLECTION, is_out_status

logger = logging.getLogger(__name__)

#: Where every claim is filed.
PREDICTIONS_COLLECTION = "predictions"

#: Where :mod:`ingest.backtest` writes its per-season summary (doc id = season).
BACKTEST_COLLECTION = "backtest"

#: Every claim kind the backtest knows how to score.
KINDS: tuple[str, ...] = (
    "start",
    "sit",
    "add",
    "fade",
    "sleeper",
    "waiver",
    "emerging",
    "matchup_top",
)

#: Kinds that claim the player will produce (hit when he clears the threshold).
POSITIVE_KINDS: frozenset[str] = frozenset({"start", "add", "sleeper", "waiver", "emerging"})

#: Kinds that claim the player will not (hit when he falls short of it).
NEGATIVE_KINDS: frozenset[str] = frozenset({"sit", "fade"})

#: How close a week's first kickoff must be before a claim about it is filed,
#: when that week is not the current one. A week's window opens Tuesday 03:00
#: ET and its first game is Thursday night, under three days later, so the
#: current week always fits; next week's kickoff is seven days out and never
#: does. This is what lets a claim file when the current week cannot be
#: resolved (no ``meta/schedule_weeks``), without letting week 17 in.
CLAIM_HORIZON = timedelta(days=3)

#: How far down the waiver board a "start him" claim is taken seriously.
WAIVER_TOP_N = 5

#: Endpoints whose bodies carry no weekly claim worth scoring.
UNSCORED_ENDPOINTS: frozenset[str] = frozenset({"draft_board", "draft_report", "team_report"})

# The deterministic player verdict is a short fixed vocabulary ("buy the usage,
# start with confidence", "bench him", "a bench stash", "not startable yet").
# Negative words are checked first because "not startable" and "behind the
# starter" both contain "start".
_SIT_RE = re.compile(
    r"\b(?:not startable|bench|stash|sit|avoid|fade)\b|\b(?:don'?t|do not|never) start"
)
_START_RE = re.compile(r"\bstart")


def verdict_call(verdict: str) -> str | None:
    """Read a start or sit claim out of a player verdict, or ``None`` for a hold.

    Args:
        verdict: The response's headline sentence.

    Returns:
        ``"start"``, ``"sit"``, or ``None`` when the verdict makes neither claim
        (a hold, a downgrade, "not enough data").
    """
    text = (verdict or "").lower()
    if _SIT_RE.search(text):
        return "sit"
    if _START_RE.search(text):
        return "start"
    return None


def prediction_id(doc: dict[str, Any]) -> str:
    """The stable document id for one claim.

    A single-player claim is keyed by season, week, endpoint and player —
    **not** by its direction. A Tuesday "add X" and a Wednesday "fade X" from
    the same endpoint are the same slot, so the first one is the claim and the
    second is refused by :meth:`~api.core.store.Store.create`. With the kind in
    the id both were filed and both scored, and exactly one of them always hit.

    Matchup claims are keyed by the group rather than the winner, so the same
    four players asked in a different order still collapse onto one document.
    """
    group = doc.get("group")
    if group:
        key = hashlib.sha1(",".join(sorted(str(g) for g in group)).encode()).hexdigest()[:16]
        return f"{doc['season']}w{doc['week']}:{doc['endpoint']}:{doc['kind']}:{key}"
    return f"{doc['season']}w{doc['week']}:{doc['endpoint']}:{doc.get('player_id') or ''}"


def legacy_prediction_ids(doc: dict[str, Any]) -> list[str]:
    """Ids a claim on the same player would have had under the old scheme.

    Until 2026-09-24 single-player ids carried the kind
    (``{season}w{week}:{endpoint}:{kind}:{player_id}``). Claims filed that way
    this season are still in the archive, so before filing under the new id
    :func:`record_predictions` checks every kind the endpoint can make — an
    "add X" filed under the old scheme must still block a later "fade X".
    Empty for matchup claims, whose id did not change.
    """
    if doc.get("group"):
        return []
    kinds = _ENDPOINT_KINDS.get(str(doc.get("endpoint") or ""), KINDS)
    prefix = f"{doc['season']}w{doc['week']}:{doc['endpoint']}"
    return [f"{prefix}:{kind}:{doc.get('player_id') or ''}" for kind in kinds]


async def _player_is_out(store: Store, doc: dict[str, Any]) -> bool:
    """Whether the claim's player is listed out, so its outcome is already decided.

    Reads ``status`` when ``injury_status`` is empty, as the engine does: Sleeper
    records IR, PUP and suspensions there alone.
    """
    player = await store.get(PLAYERS_COLLECTION, str(doc.get("player_id") or ""))
    return bool(player) and is_out_status(player.get("injury_status") or player.get("status"))


#: Codes that name the same franchise in nflverse schedules and Sleeper player
#: docs (nflverse says ``LA`` for the Rams, Sleeper ``LAR``). Without these a
#: whole roster reads as on bye and its claims are silently dropped.
_TEAM_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"LA", "LAR"}),
    frozenset({"JAX", "JAC"}),
    frozenset({"WAS", "WSH"}),
)


def _teams_playing(schedule: dict[str, Any] | None) -> frozenset[str]:
    """Every team with a game on a ``schedules/`` document, upper-cased."""
    teams: set[str] = set()
    for game in (schedule or {}).get("games") or []:
        if not isinstance(game, dict):
            continue
        for side in ("home", "away"):
            team = str(game.get(side) or "").strip().upper()
            if team:
                teams.add(team)
    for alias in _TEAM_ALIASES:
        if teams & alias:
            teams |= alias
    return frozenset(teams)


async def _has_no_game(store: Store, player_id: str, playing: frozenset[str]) -> bool:
    """Whether ``player_id`` has no team, or a team not playing this week.

    An unknown player (no ``players/`` doc) has no team either: nothing shows
    he will produce a stat line, so a call on him cannot be scored honestly.
    """
    player = await store.get(PLAYERS_COLLECTION, player_id) if player_id else None
    team = str((player or {}).get("team") or "").strip().upper()
    return not team or team not in playing


async def _without_a_game(store: Store, doc: dict[str, Any], playing: frozenset[str]) -> bool:
    """Whether the claim's player, or anyone in its matchup group, has no game."""
    ids = [str(doc.get("player_id") or "")] + [
        str(g) for g in doc.get("group") or [] if str(g) != str(doc.get("player_id") or "")
    ]
    for player_id in ids:
        if await _has_no_game(store, player_id, playing):
            return True
    return False


async def _already_claimed(store: Store, doc: dict[str, Any]) -> bool:
    """Whether an old-format claim on the same player/endpoint/week exists."""
    for legacy in legacy_prediction_ids(doc):
        if await store.get(PREDICTIONS_COLLECTION, legacy) is not None:
            return True
    return False


def extract_predictions(
    endpoint_key: str, body: dict[str, Any], *, season: int, week: int
) -> list[dict[str, Any]]:
    """Pull the scoreable claims out of one response body.

    Args:
        endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`.
        body: The response as a JSON-able dict (``model_dump(mode="json")``).
        season: Season the answer is about.
        week: Week the answer is about.

    Returns:
        One doc per claim, in the shape the module docstring describes. Empty
        for the endpoints in :data:`UNSCORED_ENDPOINTS`, for rows with no
        ``player_id``, and for bodies that make no claim.
    """
    extractor = _EXTRACTORS.get(endpoint_key)
    if extractor is None or not isinstance(body, dict):
        return []
    recorded_at = _iso(utcnow())
    docs: list[dict[str, Any]] = []
    for claim in extractor(body):
        player_id = str(claim.get("player_id") or "").strip()
        if not player_id:
            continue
        docs.append(
            {
                "season": int(season),
                "week": int(week),
                "endpoint": endpoint_key,
                "kind": claim["kind"],
                "player_id": player_id,
                "name": claim.get("name"),
                "position": claim.get("position"),
                **({"group": list(claim["group"])} if claim.get("group") else {}),
                "recorded_at": recorded_at,
                "scored": False,
                "hit": None,
                "points": None,
            }
        )
    return docs


def _iso(moment: datetime) -> str:
    """Render an aware datetime the way every ingest marker is written."""
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def claim_deadline(store: Store, season: int, week: int) -> datetime | None:
    """The week's first kickoff on the ingested schedule, or ``None`` if unknown.

    Reads ``schedules/{season}_{week}`` and takes the earliest of its
    ``first_game`` marker and every game's ``kickoff``, so a schedule doc that
    carries only one of the two still yields a deadline.
    """
    return _deadline_of(await store.get(SCHEDULES_COLLECTION, f"{season}_{week}"))


def _deadline_of(doc: dict[str, Any] | None) -> datetime | None:
    """:func:`claim_deadline` over an already-loaded schedule document."""
    if not doc:
        return None
    stamps = [parse_ts(doc.get("first_game"))]
    stamps += [
        parse_ts(game.get("kickoff")) for game in doc.get("games") or [] if isinstance(game, dict)
    ]
    known = [stamp for stamp in stamps if stamp is not None]
    return min(known) if known else None


async def record_predictions(
    store: Store,
    endpoint_key: str,
    body: dict[str, Any],
    *,
    season: int,
    week: int,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> int:
    """File every claim in ``body`` that has not already been filed.

    First write wins, nothing is filed at or after the week's first kickoff or
    for a future week, nothing is filed on a player who is out or has no game
    that week, and nothing raises: a paid answer is never lost to bookkeeping (see the module
    docstring for each).

    Args:
        now: The moment the claim is being made; defaults to the process clock
            (:func:`api.core.clock.utcnow`).
        settings: Settings for resolving the current week; defaults to the
            process settings.

    Returns:
        How many documents were newly written. Existing ones, and every claim
        made after kickoff, are left alone and not counted.
    """
    written = 0
    try:
        schedule = await store.get(SCHEDULES_COLLECTION, f"{season}_{week}")
        deadline = _deadline_of(schedule)
        if deadline is None:
            logger.info(
                "no ingested schedule for %sw%s; archiving nothing — a claim that cannot be "
                "shown to predate kickoff is not a prediction",
                season,
                week,
            )
            return 0
        moment = now or utcnow()
        if moment >= deadline:
            logger.info(
                "%sw%s kicked off at %s; archiving nothing — a claim made now would be hindsight",
                season,
                week,
                _iso(deadline),
            )
            return 0
        if deadline - moment > CLAIM_HORIZON:
            current = await current_week(store, settings or get_settings(), now=moment)
            if int(week) != current:
                logger.info(
                    "%sw%s is a future week (current week %s, kickoff %s); archiving nothing — "
                    "its calls were built on this week's data",
                    season,
                    week,
                    current,
                    _iso(deadline),
                )
                return 0
        playing = _teams_playing(schedule)
        seen: set[str] = set()
        for doc in extract_predictions(endpoint_key, body, season=season, week=week):
            doc["recorded_at"] = _iso(moment)
            doc["first_game"] = _iso(deadline)
            doc_id = prediction_id(doc)
            if doc_id in seen or await _already_claimed(store, doc):
                continue
            if await _player_is_out(store, doc) or await _without_a_game(store, doc, playing):
                continue
            seen.add(doc_id)
            if await store.create(PREDICTIONS_COLLECTION, doc_id, doc):
                written += 1
    except Exception:  # noqa: BLE001 - bookkeeping must never fail the paid path
        logger.warning(
            "could not archive predictions for %s (%sw%s); the answer was still served",
            endpoint_key,
            season,
            week,
            exc_info=True,
        )
    return written


async def archive_predictions(
    store: Store,
    endpoint_key: str,
    body: BaseModel | dict[str, Any],
    *,
    week: int | None,
    settings: Settings | None = None,
) -> int:
    """Route-facing wrapper: resolve the season, dump the model, file the claims.

    Args:
        store: The store to file into.
        endpoint_key: The endpoint that produced ``body``.
        body: The response model or its JSON dict.
        week: Week the answer is scoped to. ``None`` (a draft report) files
            nothing, because there is no week to score against.
        settings: Settings for the season fallback; defaults to the process
            settings.

    Returns:
        Documents newly written; ``0`` on any failure.
    """
    if week is None:
        return 0
    try:
        season = await current_season(store, settings or get_settings())
        payload = body.model_dump(mode="json") if isinstance(body, BaseModel) else dict(body)
    except Exception:  # noqa: BLE001 - see record_predictions
        logger.warning("could not prepare predictions for %s", endpoint_key, exc_info=True)
        return 0
    return await record_predictions(
        store, endpoint_key, payload, season=season, week=int(week), settings=settings
    )


async def accuracy_summary(store: Store, season: int) -> dict[str, Any] | None:
    """The backtest's published summary for ``season``, or ``None`` before the first run."""
    return await store.get(BACKTEST_COLLECTION, str(season))


# --------------------------------------------------------------------------
# Per-endpoint extractors
# --------------------------------------------------------------------------

Claim = dict[str, Any]


def _identity(row: dict[str, Any], kind: str) -> Claim:
    return {
        "kind": kind,
        "player_id": row.get("player_id"),
        "name": row.get("name"),
        "position": row.get("position"),
    }


def _rows(body: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = body.get(key)
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _from_player(body: dict[str, Any]) -> Iterable[Claim]:
    call = verdict_call(str(body.get("verdict") or ""))
    player = body.get("player")
    if call is None or not isinstance(player, dict):
        return []
    return [_identity(player, call)]


def _from_matchup(body: dict[str, Any]) -> Iterable[Claim]:
    ranked = sorted(_rows(body, "ranked"), key=lambda row: int(row.get("rank") or 0))
    ids = [str(row.get("player_id") or "").strip() for row in ranked]
    group = [pid for pid in ids if pid]
    # An unresolved player is ranked last with no id; a group of one is not a
    # comparison, and a winner with no id cannot be scored.
    if len(group) < 2 or not ranked or not ids[0]:
        return []
    # A top-ranked player the answer itself sits (out, or on bye) is not a
    # claim that he out-scores anyone; it is a group with nobody to start.
    if str(ranked[0].get("call") or "") in ("sit", "bench"):
        return []
    top = _identity(ranked[0], "matchup_top")
    top["group"] = group
    return [top]


def _from_roster(body: dict[str, Any]) -> Iterable[Claim]:
    out: list[Claim] = []
    for row in _rows(body, "start_sit"):
        call = str(row.get("call") or "")
        if call in ("start", "flex"):
            out.append(_identity(row, "start"))
        elif call in ("sit", "bench"):
            out.append(_identity(row, "sit"))
    return out


def _from_trending(body: dict[str, Any]) -> Iterable[Claim]:
    out: list[Claim] = []
    for row in _rows(body, "players"):
        verdict = str(row.get("verdict") or "")
        if verdict in ("add", "fade"):
            out.append(_identity(row, verdict))
    return out


def _from_sleepers(body: dict[str, Any]) -> Iterable[Claim]:
    return [_identity(row, "sleeper") for row in _rows(body, "picks")]


def _from_waivers(body: dict[str, Any]) -> Iterable[Claim]:
    board = sorted(_rows(body, "board"), key=lambda row: int(row.get("rank") or 0))
    startable = [row for row in board if row.get("stash_or_start") in ("start", "streamer")]
    return [_identity(row, "waiver") for row in startable[:WAIVER_TOP_N]]


def _from_report(body: dict[str, Any]) -> Iterable[Claim]:
    return [_identity(row, "emerging") for row in _rows(body, "emerging")]


#: Every kind each endpoint's extractor can file, for :func:`legacy_prediction_ids`.
_ENDPOINT_KINDS: dict[str, tuple[str, ...]] = {
    "player": ("start", "sit"),
    "matchup": ("matchup_top",),
    "roster": ("start", "sit"),
    "trending": ("add", "fade"),
    "sleepers": ("sleeper",),
    "waivers": ("waiver",),
    "report": ("emerging",),
}

_EXTRACTORS: dict[str, Callable[[dict[str, Any]], Iterable[Claim]]] = {
    "player": _from_player,
    "matchup": _from_matchup,
    "roster": _from_roster,
    "trending": _from_trending,
    "sleepers": _from_sleepers,
    "waivers": _from_waivers,
    "report": _from_report,
}
