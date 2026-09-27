"""The clock seam: real by default, overridable, always aware UTC."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from api.core.clock import set_clock, utcnow


@pytest.fixture(autouse=True)
def _real_clock_after() -> Iterator[None]:
    yield
    set_clock(None)


def test_the_default_is_the_real_clock() -> None:
    before = datetime.now(UTC)
    moment = utcnow()
    assert moment.tzinfo is not None
    assert abs((moment - before).total_seconds()) < 5


def test_an_override_is_returned_as_aware_utc() -> None:
    set_clock(lambda: datetime(2026, 9, 28, 12, 0))  # naive: treated as UTC
    assert utcnow() == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

    eastern = timezone(timedelta(hours=-4))
    set_clock(lambda: datetime(2026, 9, 28, 8, 0, tzinfo=eastern))
    assert utcnow() == datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def test_none_restores_the_real_clock() -> None:
    set_clock(lambda: datetime(2000, 1, 1, tzinfo=UTC))
    set_clock(None)
    assert utcnow().year >= 2026
