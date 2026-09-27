"""The clock seam.

Two pieces of logic compare "now" against the ingested schedule and must be
testable without a frozen calendar: the prediction archive refuses a claim
made after its week has kicked off (:mod:`api.data.predictions`), and the
backtest refuses to score a week until every game in it has been played
(:mod:`ingest.backtest`). Both reach the route layer, where a test cannot pass
``now=`` explicitly, so this is the same shape as :func:`~api.core.store.set_store`
and :func:`~api.agents.engine.set_engine`: a process-wide default with a test
hook, never a monkeypatched module internal.

Usage::

    from api.core.clock import utcnow
    if utcnow() >= deadline: ...

    # in a test
    set_clock(lambda: datetime(2026, 9, 28, tzinfo=UTC))
    ...
    set_clock(None)
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_override: Callable[[], datetime] | None = None


def utcnow() -> datetime:
    """The current time as an aware UTC datetime, honouring :func:`set_clock`."""
    if _override is not None:
        moment = _override()
        return moment.astimezone(UTC) if moment.tzinfo else moment.replace(tzinfo=UTC)
    return datetime.now(UTC)


def set_clock(source: Callable[[], datetime] | None) -> None:
    """Override :func:`utcnow` (tests / DI). Pass ``None`` to restore the real clock."""
    global _override
    _override = source
