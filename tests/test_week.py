"""NFL week resolution: override, schedule-driven, Tuesday 3am ET rollover, fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from api.core.config import Settings
from api.core.store import MemoryStore, Store
from api.core.week import (
    FALLBACK_WEEK,
    LEAGUE_TZ,
    META_COLLECTION,
    SCHEDULE_WEEKS_DOC_ID,
    current_season,
    current_week,
    resolve_week,
    week_window_start,
)

# 2026 season openers: Thursday-night kickoffs, expressed in UTC.
WEEK_FIRST_GAMES = {
    "1": "2026-09-11T00:20:00Z",  # Thu Sep 10, 8:20pm ET
    "2": "2026-09-18T00:15:00Z",
    "3": "2026-09-25T00:15:00Z",
}


def _settings(**kwargs: object) -> Settings:
    kwargs.setdefault("season", 2026)
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


@pytest.fixture
async def store() -> Store:
    memory = MemoryStore()
    await memory.set(
        META_COLLECTION, SCHEDULE_WEEKS_DOC_ID, {"season": 2026, "weeks": dict(WEEK_FIRST_GAMES)}
    )
    return memory


def _et(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=LEAGUE_TZ).astimezone(UTC)


# -- the rollover rule ----------------------------------------------------


def test_window_opens_tuesday_3am_et_before_the_first_game() -> None:
    first_game = datetime.fromisoformat("2026-09-11T00:20:00+00:00")
    start = week_window_start(first_game)
    local = start.astimezone(LEAGUE_TZ)
    assert local.weekday() == 1  # Tuesday
    assert (local.hour, local.minute) == (3, 0)
    assert local.date().isoformat() == "2026-09-08"


def test_window_for_a_tuesday_first_game_before_3am_rolls_back_a_week() -> None:
    """A Tuesday 1am kickoff belongs to the week that opened the *previous* Tuesday."""
    first_game = datetime(2026, 9, 15, 1, 0, tzinfo=LEAGUE_TZ)
    assert week_window_start(first_game).astimezone(LEAGUE_TZ).date().isoformat() == "2026-09-08"


def test_window_for_a_tuesday_first_game_after_3am_is_that_day() -> None:
    first_game = datetime(2026, 9, 15, 20, 0, tzinfo=LEAGUE_TZ)
    assert week_window_start(first_game).astimezone(LEAGUE_TZ).date().isoformat() == "2026-09-15"


def test_window_start_handles_a_different_timezone_input() -> None:
    first_game = datetime(2026, 9, 10, 17, 20, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert week_window_start(first_game).astimezone(LEAGUE_TZ).date().isoformat() == "2026-09-08"


# -- resolve_week (pure) --------------------------------------------------


def _weeks() -> dict[int, datetime]:
    return {
        int(k): datetime.fromisoformat(v.replace("Z", "+00:00"))
        for k, v in WEEK_FIRST_GAMES.items()
    }


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (_et(2026, 9, 1), 1),  # before the season -> earliest known week
        (_et(2026, 9, 8, 2, 59), 1),  # minutes before week 1 opens
        (_et(2026, 9, 8, 3, 0), 1),  # exactly at the boundary
        (_et(2026, 9, 13, 16, 0), 1),  # Sunday of week 1
        (_et(2026, 9, 14, 23, 0), 1),  # Monday night of week 1
        (_et(2026, 9, 15, 2, 59), 1),  # Tuesday 2:59am -- still week 1
        (_et(2026, 9, 15, 3, 0), 2),  # Tuesday 3:00am -- flips to week 2
        (_et(2026, 9, 16, 12, 0), 2),  # waiver Wednesday
        (_et(2026, 9, 22, 3, 0), 3),
        (_et(2026, 12, 1), 3),  # past the last known week -> latest known week
    ],
)
def test_resolve_week(moment: datetime, expected: int) -> None:
    assert resolve_week(_weeks(), moment) == expected


def test_resolve_week_empty_map() -> None:
    assert resolve_week({}, datetime.now(UTC)) is None


def test_resolve_week_ignores_gaps_in_week_numbering() -> None:
    weeks = {
        5: datetime.fromisoformat("2026-10-09T00:15:00+00:00"),
        7: datetime.fromisoformat("2026-10-23T00:15:00+00:00"),
    }
    assert resolve_week(weeks, _et(2026, 10, 10)) == 5
    assert resolve_week(weeks, _et(2026, 10, 21)) == 7


# -- current_week ---------------------------------------------------------


async def test_override_wins_over_schedule(store: Store) -> None:
    settings = _settings(week_override=9)
    assert await current_week(store, settings, now=_et(2026, 9, 13)) == 9


async def test_override_works_without_any_schedule_data() -> None:
    assert await current_week(MemoryStore(), _settings(week_override=4)) == 4


async def test_schedule_driven(store: Store) -> None:
    settings = _settings()
    assert await current_week(store, settings, now=_et(2026, 9, 13)) == 1
    assert await current_week(store, settings, now=_et(2026, 9, 16)) == 2


async def test_naive_now_is_treated_as_utc(store: Store) -> None:
    naive = datetime(2026, 9, 16, 12, 0)
    assert await current_week(store, _settings(), now=naive) == 2


async def test_fallback_when_no_schedule_document() -> None:
    assert await current_week(MemoryStore(), _settings()) == FALLBACK_WEEK


async def test_fallback_when_document_has_no_parseable_weeks() -> None:
    memory = MemoryStore()
    await memory.set(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID, {"season": 2026, "weeks": {}})
    assert await current_week(memory, _settings()) == FALLBACK_WEEK

    await memory.set(
        META_COLLECTION, SCHEDULE_WEEKS_DOC_ID, {"season": 2026, "weeks": {"1": "garbage"}}
    )
    assert await current_week(memory, _settings()) == FALLBACK_WEEK


async def test_flat_document_shape_is_accepted() -> None:
    """Week numbers may live at the top level instead of under 'weeks'."""
    memory = MemoryStore()
    await memory.set(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID, dict(WEEK_FIRST_GAMES))
    assert await current_week(memory, _settings(), now=_et(2026, 9, 16)) == 2


async def test_bare_date_values_are_accepted() -> None:
    memory = MemoryStore()
    await memory.set(
        META_COLLECTION,
        SCHEDULE_WEEKS_DOC_ID,
        {"weeks": {"1": "2026-09-10", "2": "2026-09-17"}},
    )
    assert await current_week(memory, _settings(), now=_et(2026, 9, 16)) == 2


# -- current_season -------------------------------------------------------


async def test_current_season_prefers_ingested_value(store: Store) -> None:
    assert await current_season(store, _settings(season=2025)) == 2026


async def test_current_season_accepts_string_season() -> None:
    memory = MemoryStore()
    await memory.set(META_COLLECTION, SCHEDULE_WEEKS_DOC_ID, {"season": "2027", "weeks": {}})
    assert await current_season(memory, _settings()) == 2027


async def test_current_season_falls_back_to_settings() -> None:
    assert await current_season(MemoryStore(), _settings(season=2026)) == 2026
