"""NFL week resolution.

DESIGN_NOTES §7: the current week is derived from the *ingested schedule*, never
from wall-clock arithmetic alone. ``WEEK_OVERRIDE`` short-circuits everything for
testing and for the gap before ingest has run.

The rule
--------
Fantasy weeks flip at **Tuesday 03:00 America/New_York** — after Monday Night
Football has finalized and before waiver processing. So for each NFL week *N*
with a known first-game timestamp, that week's window opens at the Tuesday
03:00 ET on-or-before its first game (Thursday night in a normal week, so the
preceding Tuesday), and closes when week *N+1*'s window opens.

The active week is therefore the largest *N* whose window has already opened.
Before the season's first window, we return the earliest known week.

Data
----
``meta/schedule_weeks``, written by the ingest wave, maps week number -> the
first game of that week as an ISO-8601 timestamp (a bare ``YYYY-MM-DD`` date is
also accepted and read as midnight UTC)::

    {
      "_id": "schedule_weeks",
      "season": 2026,
      "weeks": {
        "1": "2026-09-10T00:20:00Z",
        "2": "2026-09-17T00:15:00Z",
        "3": "2026-09-24T00:15:00Z"
      }
    }

For backward tolerance the week numbers may also live as top-level keys of the
document (``{"1": "...", "2": "..."}``) rather than under ``"weeks"``.

Fallback
--------
No override and no usable schedule document -> log a warning and return week 1.
A wrong-but-sane week is better than a 500 on a paid call; the ``data_freshness``
block on the response tells the caller the schedule was missing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from api.core.clock import utcnow
from api.core.config import Settings, get_settings
from api.core.store import Store

logger = logging.getLogger(__name__)

#: Timezone the NFL/fantasy week boundary is defined in.
LEAGUE_TZ = ZoneInfo("America/New_York")

#: Local time on Tuesday at which the fantasy week rolls over.
WEEK_ROLLOVER_TIME = time(hour=3)

#: ``date.weekday()`` value for Tuesday.
_TUESDAY = 1

#: Collection/doc holding the ingested week->first-game map.
META_COLLECTION = "meta"
SCHEDULE_WEEKS_DOC_ID = "schedule_weeks"

#: Week returned when nothing else is known.
FALLBACK_WEEK = 1


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp or bare date into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def week_window_start(first_game: datetime) -> datetime:
    """Return when the week containing ``first_game`` becomes active.

    That is the most recent Tuesday 03:00 America/New_York at or before
    ``first_game``. Returned as an aware UTC datetime.
    """
    local = first_game.astimezone(LEAGUE_TZ)
    days_since_tuesday = (local.weekday() - _TUESDAY) % 7
    candidate = datetime.combine(
        (local - timedelta(days=days_since_tuesday)).date(), WEEK_ROLLOVER_TIME, tzinfo=LEAGUE_TZ
    )
    if candidate > local:
        # first_game is Tuesday before 03:00 local — the window opened a week earlier.
        candidate -= timedelta(days=7)
    return candidate.astimezone(UTC)


def _extract_weeks(doc: dict[str, Any]) -> dict[int, datetime]:
    """Pull ``{week: first_game_utc}`` out of a ``meta/schedule_weeks`` document."""
    raw = doc.get("weeks")
    if not isinstance(raw, dict):
        raw = doc
    weeks: dict[int, datetime] = {}
    for key, value in raw.items():
        try:
            week = int(key)
        except (TypeError, ValueError):
            continue  # "_id", "season", and any other metadata keys
        ts = parse_ts(value)
        if ts is not None:
            weeks[week] = ts
    return weeks


def resolve_week(weeks: dict[int, datetime], now: datetime) -> int | None:
    """Resolve the active week from a ``{week: first_game_utc}`` map.

    Pure function — the testable core of :func:`current_week`.

    Args:
        weeks: Week number -> first game timestamp (aware).
        now: Current time (aware).

    Returns:
        The largest week whose window has opened; the earliest known week when
        ``now`` precedes the season; ``None`` when ``weeks`` is empty.
    """
    if not weeks:
        return None
    windows = sorted((week_window_start(ts), week) for week, ts in weeks.items())
    active = None
    for start, week in windows:
        if start <= now:
            active = week
        else:
            break
    return active if active is not None else windows[0][1]


async def current_week(
    store: Store, settings: Settings | None = None, *, now: datetime | None = None
) -> int:
    """Return the current NFL week.

    Resolution order:
        1. ``settings.week_override`` when set.
        2. The ``meta/schedule_weeks`` document, via :func:`resolve_week`.
        3. :data:`FALLBACK_WEEK` with a logged warning.

    Args:
        store: Store holding the ingested schedule.
        settings: Settings; defaults to the process settings.
        now: Override the clock (testing). Naive values are read as UTC.

    Returns:
        The active NFL week number.
    """
    settings = settings or get_settings()
    if settings.week_override is not None:
        return int(settings.week_override)

    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)

    doc = await store.get(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID)
    if doc:
        week = resolve_week(_extract_weeks(doc), moment)
        if week is not None:
            return week
        logger.warning(
            "meta/schedule_weeks holds no parseable weeks; falling back to week %s", FALLBACK_WEEK
        )
    else:
        logger.warning("no meta/schedule_weeks document; falling back to week %s", FALLBACK_WEEK)
    return FALLBACK_WEEK


async def ingested_season(store: Store) -> int | None:
    """Return the season the ingested schedule is for, or ``None`` if unknown.

    This is what the store actually holds, with no configured fallback — which
    is the whole point. :func:`current_season` prefers it and falls back to
    settings, so the two agreeing is the normal case and the two *disagreeing*
    is a store that was filled for a different year. Nothing else in the stack
    notices that: the freshness markers are stamped, the readiness check passes,
    and every answer is confidently about the wrong season.
    """
    doc = await store.get(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID)
    if not doc:
        return None
    season = doc.get("season")
    if isinstance(season, int):
        return season
    if isinstance(season, str) and season.isdigit():
        return int(season)
    return None


async def current_season(store: Store, settings: Settings | None = None) -> int:
    """Return the active NFL season year.

    Prefers the ``season`` recorded on ``meta/schedule_weeks`` by ingest (so a
    season rollover does not require a redeploy) and falls back to
    ``settings.season``.
    """
    settings = settings or get_settings()
    found = await ingested_season(store)
    return found if found is not None else int(settings.season)
