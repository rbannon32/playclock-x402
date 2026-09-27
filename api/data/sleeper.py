"""Sleeper API client (free, no key, read-only).

Base: ``https://api.sleeper.app/v1`` (overridable via ``SLEEPER_BASE_URL``).

Discipline (tech spec §4.1): one shared ``httpx.AsyncClient``, retry with
exponential backoff on transport errors, 5xx and 429, 10s timeout, and stay far
under Sleeper's ~1000 req/min courtesy limit. A 429 is Sleeper saying "slow
down", not "you asked wrongly", so it is retried like a 5xx, waiting at least
as long as its ``Retry-After`` asks (capped at :data:`MAX_RETRY_AFTER`: a
paid ``/v1/roster`` call cannot wait a minute on a hint). Every other 4xx is
*not* retried — it means the request was wrong, not that the service blipped.

Live-call policy:
    - :meth:`SleeperClient.get_players` fetches the ~5MB player dump. **Only the
      ingest job may call it**, once nightly. Never call it from a request path.
    - :meth:`SleeperClient.get_trending` is polled every 30 min by the scheduler
      and cached in Firestore; request paths read the cache, not this method.
    - The user/league/roster/matchup methods *are* called live on
      ``/v1/roster`` and ``/v1/team-report`` — user-triggered and low volume.

Testing: pass ``transport=respx.MockTransport(...)`` (or any
``httpx.AsyncBaseTransport``) to the constructor. ``retry_wait`` can be set to 0
so retry tests don't sleep, and ``sleep`` replaces the retry sleeper so a test
can record the waits a ``Retry-After`` asked for without taking them.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from api.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Wall-clock timeout for every Sleeper request, in seconds.
DEFAULT_TIMEOUT = 10.0

#: Total attempts (1 initial + 2 retries) for retryable failures.
MAX_ATTEMPTS = 3

#: Longest ``Retry-After`` honoured, in seconds. Longer hints are clipped: the
#: roster and team-report routes call Sleeper live inside a paid request.
MAX_RETRY_AFTER = 10.0

#: Retried like a 5xx (every other 4xx is not): Sleeper asking us to slow down.
TOO_MANY_REQUESTS = 429

TrendKind = Literal["add", "drop"]


class SleeperError(RuntimeError):
    """Base class for Sleeper client failures."""


class SleeperNotFound(SleeperError):
    """The requested Sleeper resource does not exist (HTTP 404).

    Most commonly an unknown username on :meth:`SleeperClient.get_user`.
    """


class SleeperUnavailable(SleeperError):
    """Sleeper failed persistently — 5xx or transport error after all retries."""


class TrendingEntry(BaseModel):
    """One row of ``/players/nfl/trending/{add,drop}``.

    Sleeper returns only ids and counts here; names/positions must be joined
    from the ingested ``players`` collection.
    """

    player_id: str = Field(description="Sleeper player_id.")
    count: int = Field(description="Number of adds (or drops) in the lookback window.")


def _seg(value: object) -> str:
    """One URL path segment, percent-encoded so it cannot leave its slot.

    Usernames and ids arrive from paying callers. Unquoted, ``a/../../x`` or
    ``1?y=`` would re-point the request, and httpx resolves dot segments, so
    ``.``, ``..`` and the empty string are refused outright: quoting leaves
    dots alone. :mod:`api.schemas` rejects these first; this is the backstop.
    """
    text = str(value)
    if text.strip(".") == "":
        raise SleeperNotFound(f"not a sleeper path segment: {text!r}")
    return quote(text, safe="")


class _RetryableStatus(Exception):
    """Internal signal that a 5xx or 429 response should be retried."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"sleeper returned {response.status_code}")
        self.response = response


def retry_after_seconds(response: httpx.Response, *, now: datetime | None = None) -> float | None:
    """The wait a response's ``Retry-After`` header asks for, in seconds.

    Accepts both forms RFC 9110 allows: delta-seconds and an HTTP-date. The
    result is clipped to ``[0, MAX_RETRY_AFTER]``; ``None`` when the header is
    absent or unparseable, in which case plain backoff applies.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


class SleeperClient:
    """Async client for the public Sleeper API.

    Args:
        settings: Settings providing ``sleeper_base_url``. Defaults to the
            process settings.
        client: A pre-built ``httpx.AsyncClient`` to use (takes precedence over
            ``transport``); the caller owns its lifecycle.
        transport: Transport for a client this instance builds and owns —
            the injection point for ``respx`` in tests.
        timeout: Per-request timeout in seconds.
        retry_wait: Base seconds for exponential backoff. Set to 0 in tests.
        sleep: Replaces the async sleep between retries (tests record the
            requested waits instead of taking them). Default: tenacity's.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_wait: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = self._settings.sleeper_base_url.rstrip("/")
        self._timeout = timeout
        self._retry_wait = retry_wait
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"accept": "application/json"},
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- transport ---------------------------------------------------------

    def _wait(
        self, backoff: Callable[[RetryCallState], float]
    ) -> Callable[[RetryCallState], float]:
        """Exponential backoff, stretched to a 429's ``Retry-After`` when longer."""

        def wait(state: RetryCallState) -> float:
            delay = backoff(state)
            failure = state.outcome.exception() if state.outcome else None
            if isinstance(failure, _RetryableStatus):
                hinted = retry_after_seconds(failure.response)
                if hinted is not None:
                    delay = max(delay, hinted)
            return delay

        return wait

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET ``path`` (relative to the Sleeper base) and return parsed JSON.

        Retries transport errors, 5xx and 429 up to :data:`MAX_ATTEMPTS` times
        with exponential backoff (a ``Retry-After`` stretches the wait, see
        :func:`retry_after_seconds`). Raises :class:`SleeperNotFound` on 404 and
        :class:`SleeperError` on any other 4xx.
        """
        url = f"{self._base_url}/{path.lstrip('/')}"
        backoff = wait_exponential(multiplier=self._retry_wait, min=self._retry_wait, max=8)
        retryer = AsyncRetrying(
            stop=stop_after_attempt(MAX_ATTEMPTS),
            wait=self._wait(backoff),
            retry=retry_if_exception_type((_RetryableStatus, httpx.TransportError)),
            reraise=True,
            **({"sleep": self._sleep} if self._sleep is not None else {}),
        )
        try:
            async for attempt in retryer:
                with attempt:
                    response = await self._client.get(url, params=params, timeout=self._timeout)
                    if response.status_code >= 500 or response.status_code == TOO_MANY_REQUESTS:
                        raise _RetryableStatus(response)
                    if response.status_code == 404:
                        raise SleeperNotFound(f"sleeper 404 for {path}")
                    if response.status_code >= 400:
                        raise SleeperError(
                            f"sleeper {response.status_code} for {path}: {response.text[:200]}"
                        )
                    return response.json()
        except _RetryableStatus as exc:
            raise SleeperUnavailable(
                f"sleeper {exc.response.status_code} for {path} after {MAX_ATTEMPTS} attempts"
            ) from exc
        except httpx.TransportError as exc:
            raise SleeperUnavailable(
                f"sleeper transport failure for {path} after {MAX_ATTEMPTS} attempts: {exc}"
            ) from exc
        raise SleeperUnavailable(
            f"sleeper request for {path} produced no response"
        )  # pragma: no cover

    # -- user / league -----------------------------------------------------

    async def get_user(self, username: str) -> dict[str, Any]:
        """Return the Sleeper user object for ``username``.

        Sleeper answers unknown usernames with 404 (and occasionally a JSON
        ``null`` body with a 200); both surface as :class:`SleeperNotFound`.

        Returns:
            The user object; ``user_id`` is the field the league lookup needs.
        """
        data = await self._get(f"/user/{_seg(username)}")
        if not data:
            raise SleeperNotFound(f"no sleeper user named {username!r}")
        return data

    async def get_leagues(self, user_id: str, season: int | str) -> list[dict[str, Any]]:
        """Return the user's NFL leagues for ``season``.

        Callers that were given no ``league_id`` should take the first entry
        (matches the request-model contract in :mod:`api.schemas`).
        """
        data = await self._get(f"/user/{_seg(user_id)}/leagues/nfl/{_seg(season)}")
        return list(data or [])

    async def get_league(self, league_id: str) -> dict[str, Any]:
        """Return league settings — scoring rules and roster positions.

        Required to compute an optimal lineup correctly for *this* league
        (tech spec §4.1).
        """
        data = await self._get(f"/league/{_seg(league_id)}")
        if not data:
            raise SleeperNotFound(f"no sleeper league {league_id!r}")
        return data

    async def get_rosters(self, league_id: str) -> list[dict[str, Any]]:
        """Return every roster in the league.

        The union of all ``players`` arrays across rosters, subtracted from the
        player universe, is the league's free-agent pool — the only pool
        ``/v1/team-report`` may recommend from.
        """
        data = await self._get(f"/league/{_seg(league_id)}/rosters")
        return list(data or [])

    async def get_users(self, league_id: str) -> list[dict[str, Any]]:
        """Return every user in the league.

        Each entry carries ``user_id``, ``display_name`` and a ``metadata`` map
        that may hold ``team_name``. Used only to label rosters in
        ``/v1/team-report`` — an orphan or co-owned roster still computes without
        it, so callers should degrade rather than fail when this call errors.
        """
        data = await self._get(f"/league/{_seg(league_id)}/users")
        return list(data or [])

    async def get_matchups(self, league_id: str, week: int) -> list[dict[str, Any]]:
        """Return per-roster matchup entries for ``week``.

        Each entry carries ``starters``, ``players`` and ``players_points``,
        which is what lineup-efficiency and bench-points-lost math needs.
        """
        data = await self._get(f"/league/{_seg(league_id)}/matchups/{_seg(week)}")
        return list(data or [])

    # -- drafts ------------------------------------------------------------

    async def get_drafts(self, user_id: str, season: int | str) -> list[dict[str, Any]]:
        """Return the user's NFL drafts for ``season``, newest first.

        Sleeper returns these in no documented order, so they are sorted by
        ``start_time`` here: a caller who supplied only a username means "the
        draft I just did", and that is the most recent one.

        SHAPE NOTE: the draft endpoints are modelled from Sleeper's published
        docs rather than a live sample (DESIGN_NOTES, "Draft endpoints"), so
        every field is read defensively and nothing here raises on a missing key.
        """
        data = await self._get(f"/user/{_seg(user_id)}/drafts/nfl/{_seg(season)}")
        drafts = list(data or [])
        drafts.sort(key=lambda d: d.get("start_time") or 0, reverse=True)
        return drafts

    async def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        """Return the drafts belonging to ``league_id``, newest first.

        Distinct from :meth:`get_drafts`, which is keyed on a *user*. A team
        report knows its league but not necessarily who ran the draft, and in
        the preseason the league's own draft is the only completed event there
        is to report on.
        """
        data = await self._get(f"/league/{_seg(league_id)}/drafts")
        drafts = list(data or [])
        drafts.sort(key=lambda d: d.get("start_time") or 0, reverse=True)
        return drafts

    async def get_draft(self, draft_id: str) -> dict[str, Any]:
        """Return one draft's settings: type, rounds, teams, slot mapping.

        Raises:
            SleeperNotFound: No draft with that id.
        """
        data = await self._get(f"/draft/{_seg(draft_id)}")
        if not data:
            raise SleeperNotFound(f"no sleeper draft {draft_id!r}")
        return data

    async def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        """Return every pick made in ``draft_id``, in pick order.

        Each entry carries ``player_id``, ``picked_by``, ``roster_id``,
        ``round``, ``draft_slot``, ``pick_no`` and a ``metadata`` map. An
        in-progress draft returns only the picks made so far, which is a normal
        outcome rather than an error.
        """
        data = await self._get(f"/draft/{_seg(draft_id)}/picks")
        picks = list(data or [])
        picks.sort(key=lambda p: p.get("pick_no") or 0)
        return picks

    # -- market signal -----------------------------------------------------

    async def get_trending(
        self,
        kind: TrendKind = "add",
        lookback_hours: int = 24,
        limit: int = 25,
    ) -> list[TrendingEntry]:
        """Return trending adds or drops.

        Args:
            kind: ``"add"`` or ``"drop"``.
            lookback_hours: Sleeper trending window.
            limit: Maximum rows.

        Returns:
            Parsed :class:`TrendingEntry` rows, highest count first (Sleeper's
            own ordering is preserved). Names and positions must be joined from
            the ingested ``players`` collection.
        """
        if kind not in ("add", "drop"):
            raise ValueError(f"kind must be 'add' or 'drop', got {kind!r}")
        data = await self._get(
            f"/players/nfl/trending/{kind}",
            params={"lookback_hours": lookback_hours, "limit": limit},
        )
        entries: list[TrendingEntry] = []
        for row in data or []:
            player_id = row.get("player_id")
            if player_id is None:
                continue
            entries.append(TrendingEntry(player_id=str(player_id), count=int(row.get("count", 0))))
        return entries

    # -- bulk (ingest only) ------------------------------------------------

    async def get_players(self) -> dict[str, Any]:
        """Return the full NFL player dump (~5MB), keyed by Sleeper player_id.

        **Ingest only.** Sleeper asks that this be fetched at most once a day;
        request paths must read the ingested ``players`` collection instead.

        Player objects carry the cross-id fields (``gsis_id``, ``espn_id``, ...)
        that the ingest wave needs to build ``id_map/``.
        """
        logger.info("fetching full sleeper player dump (~5MB) — ingest only")
        data = await self._get("/players/nfl")
        return dict(data or {})
