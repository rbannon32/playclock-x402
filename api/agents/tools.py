"""Stats tools — the only way either engine reads football data.

These are thin async wrappers over :mod:`api.data.stats_store`, shared by both
:class:`~api.agents.deterministic.DeterministicAnalysisEngine` (which calls them
directly) and :class:`~api.agents.pipeline.AdkAnalysisEngine` (which hands them
to the ADK stats agent as ``FunctionTool``\\ s, tech spec §5).

Two invariants make this module load-bearing:

**Provenance on every result.** Every tool return carries a ``"source"`` string
naming the dataset and slice the numbers came from — ``"nflverse weekly_stats
2026w3"``, ``"sleeper trending/add"``. Synthesis builds
:class:`~api.schemas.StatCitation` entries straight from those strings, which is
how a paid response can promise that every number is traceable (PRD §5). A tool
result with no ``source`` is a bug.

**Never raise on missing data.** An unknown player, an un-ingested week or a bye
returns ``{"found": False, ...}``, not an exception. A paid call must be able to
say "I couldn't find that player" instead of 500-ing — and the ADK stats agent
cannot recover from a tool that throws.

Tool results are plain JSON-able dicts: no pydantic models, no ``datetime``
objects, nothing the Gemini function-calling layer would choke on.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from api.core.store import Store
from api.data import stats_store
from api.data.stats_store import USAGE_TRENDS_COLLECTION

#: Names of the methods exposed to the ADK stats agent as function tools.
#: Anything not in this tuple is engine-internal (see
#: :meth:`StatsTools.scan_usage_trends`).
FUNCTION_TOOL_NAMES: tuple[str, ...] = (
    "resolve_player",
    "get_player",
    "get_weekly_stats",
    "get_usage_trends",
    "get_def_vs_pos",
    "get_trending",
    "get_schedule",
    "get_data_freshness",
    "get_draft_pool",
)

#: Positions we will ever recommend. Used to disambiguate name collisions
#: ("Josh Allen" the QB vs. "Josh Allen" the linebacker) and to filter scans.
FANTASY_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "WR", "TE", "K", "DEF"})


#: A team or position code as the store keys it: short and alphanumeric. A
#: model argument is anything at all, and a ``/`` in a Firestore doc id raises.
_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,6}$")


def _code(value: Any) -> str | None:
    """A team/position argument upper-cased, or ``None`` when it is not a code."""
    if not isinstance(value, str) or not _CODE_RE.match(value.strip()):
        return None
    return value.strip().upper()


def _count(value: Any, default: int) -> int | None:
    """An integer argument from a model: ``None`` means ``default``; junk is ``None``.

    Models pass ``"10"``, ``10.0`` or ``None`` for an int parameter as often as
    ``10``, and ``"all"`` or ``"max"`` sometimes. The first three have one
    obvious meaning; the last does not, and guessing would be worse than
    saying so.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _player_id(value: Any) -> str | None:
    """A player-id argument as a doc id, or ``None`` when it cannot be one.

    Models pass ``4046`` as often as ``"4046"``, and sometimes ``""`` or a
    name with a ``/`` in it. Firestore raises on an empty id or one holding a
    ``/``, and that exception fails the whole ADK run, not just the call.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    token = str(value).strip()
    if not token or "/" in token or token in (".", ".."):
        return None
    return token


def _bad_argument(message: str, **fields: Any) -> dict[str, Any]:
    """The ``found=False`` result a tool returns for an argument it cannot use."""
    return {"found": False, **fields, "error": message}


def source_players(player_id: str) -> str:
    """Provenance string for a ``players/{player_id}`` read."""
    return f"sleeper players/{player_id}"


def source_player_index(name: str) -> str:
    """Provenance string for a ``player_index`` name lookup."""
    return f"sleeper player_index/{stats_store.normalize_name(name)}"


def source_weekly(season: int, week: int) -> str:
    """Provenance string for one player-week nflverse stat line."""
    return f"nflverse weekly_stats {season}w{week}"


def source_usage(player_id: str) -> str:
    """Provenance string for a derived L4W usage rollup."""
    return f"nflverse usage_trends/{player_id}"


def source_depth_chart(team: str) -> str:
    """Provenance marker for a depth-chart read."""
    return f"nflverse depth_charts/{team.upper()}"


def source_def_vs_pos(team: str) -> str:
    """Provenance string for a team's fantasy-points-allowed splits."""
    return f"nflverse def_vs_pos/{team.upper()}"


def source_draft_pool() -> str:
    """Provenance string for the market-ordered draftable pool."""
    return "sleeper players/search_rank"


def source_trending(kind: str) -> str:
    """Provenance string for the Sleeper market signal."""
    return f"sleeper trending/{kind}"


def source_schedule(season: int, week: int) -> str:
    """Provenance string for one week of the NFL schedule."""
    return f"nflverse schedules/{season}w{week}"


#: Provenance string for the ingest freshness marker document.
SOURCE_FRESHNESS = "meta/freshness"


#: The ``(season, week)`` the analysis run on *this* task is about.
#:
#: :class:`~api.agents.pipeline.AdkAnalysisEngine` caches one agent pipeline per
#: endpoint, and those agents hold ``FunctionTool``\\ s bound to the methods of
#: one shared :class:`StatsTools`. Per-run scope therefore cannot be assigned
#: onto that object: two concurrent requests for the same endpoint would
#: interleave, and the first run would read the second's week mid-pipeline. A
#: context variable is read at *call* time and is private to the task that set
#: it, so concurrent runs never see each other's scope.
_run_scope: ContextVar[tuple[int, int] | None] = ContextVar("stats_run_scope", default=None)


@contextmanager
def run_scope(season: int, week: int) -> Iterator[None]:
    """Bind ``season``/``week`` for every :class:`StatsTools` call in this task.

    Wrap one analysis run. Inside the block every tool call — including calls
    made through a :class:`StatsTools` instance shared with other requests —
    reads this scope instead of the instance's own defaults. Nested scopes
    restore the enclosing one on exit.
    """
    token = _run_scope.set((int(season), int(week)))
    try:
        yield
    finally:
        _run_scope.reset(token)


class StatsTools:
    """Store-bound stats tools.

    ``season`` and ``week`` are the run's defaults, so an agent that omits them
    still reads the right slice. They are read through the active
    :func:`run_scope` when there is one, which is what makes an instance shared
    between concurrent runs safe — see :data:`_run_scope`.

    Args:
        store: Backing store.
        season: Default NFL season for week-scoped reads.
        week: Default NFL week for week-scoped reads.
    """

    def __init__(self, store: Store, season: int, week: int) -> None:
        self._store = store
        self._season = int(season)
        self._week = int(week)
        self._scheduled: dict[tuple[int, int], bool] = {}

    @property
    def season(self) -> int:
        """The run's NFL season: the active :func:`run_scope`, else the default."""
        scope = _run_scope.get()
        return scope[0] if scope is not None else self._season

    @season.setter
    def season(self, value: int) -> None:
        self._season = int(value)

    @property
    def week(self) -> int:
        """The run's NFL week: the active :func:`run_scope`, else the default."""
        scope = _run_scope.get()
        return scope[1] if scope is not None else self._week

    @week.setter
    def week(self, value: int) -> None:
        self._week = int(value)

    # -- resolution -------------------------------------------------------

    async def resolve_player(self, name: str) -> dict[str, Any]:
        """Resolve a player name to Sleeper candidates.

        Use this before any other tool when you only have a name. Ambiguous
        names legitimately return several candidates; ``best`` is the
        fantasy-relevant one when exactly one candidate plays a fantasy
        position, otherwise the first candidate.

        Args:
            name: Player name as typed by a human or an agent.

        Returns:
            ``{"found", "query", "candidates", "best", "source"}``. ``found`` is
            ``False`` and ``candidates`` empty for an unknown name.
        """
        candidates = await stats_store.resolve_player(self._store, name)
        best = _best_candidate(candidates)
        return {
            "found": bool(candidates),
            "query": name,
            "candidates": candidates,
            "best": best,
            "ambiguous": len(candidates) > 1,
            "source": source_player_index(name),
        }

    async def get_player(self, player_id: str) -> dict[str, Any]:
        """Fetch one player's identity, team and injury status.

        Args:
            player_id: Sleeper player_id.

        Returns:
            ``{"found", "player", "source"}``; ``player`` is ``{}`` when the id
            is unknown.
        """
        doc = await stats_store.get_player(self._store, player_id)
        return {
            "found": doc is not None,
            "player": doc or {},
            "source": source_players(player_id),
        }

    # -- performance ------------------------------------------------------

    async def get_weekly_stats(
        self,
        player_id: str,
        weeks: list[int] | None = None,
        season: int = 0,
    ) -> dict[str, Any]:
        """Fetch a player's weekly stat lines.

        Args:
            player_id: Sleeper player_id.
            weeks: Weeks to fetch. Defaults to the last four weeks up to and
                including the run's week. Weeks with no ingested line (bye, DNP,
                not yet ingested) are simply absent from the result.
            season: NFL season; 0 means the run's season.

        Returns:
            ``{"found", "player_id", "season", "weeks_requested", "lines",
            "source"}``. Each entry in ``lines`` carries its own per-week
            ``source``.
        """
        pid = _player_id(player_id)
        season_arg = _count(season, 0)
        raw_weeks = weeks if isinstance(weeks, (list, tuple)) or weeks is None else [weeks]
        week_args = [_count(week, 0) for week in raw_weeks or []]
        if pid is None or season_arg is None or any(w is None or w < 1 for w in week_args):
            return _bad_argument(
                "player_id must be a Sleeper id such as '4046'; weeks and season are integers",
                player_id=player_id,
                season=season,
                weeks_requested=weeks,
                lines=[],
            )
        player_id = pid
        season = season_arg or self.season
        wanted = [int(w) for w in week_args if w is not None] or self.recent_weeks()
        lines = await stats_store.get_weekly_stats(self._store, player_id, season, wanted)
        for line in lines:
            line["source"] = source_weekly(season, int(line.get("week", 0)))
        return {
            "found": bool(lines),
            "player_id": player_id,
            "season": season,
            "weeks_requested": wanted,
            "lines": lines,
            "source": source_weekly(season, wanted[-1] if wanted else self.week),
        }

    async def get_usage_trends(self, player_id: str) -> dict[str, Any]:
        """Fetch a player's derived last-four-week usage rollup.

        This is the opportunity signal: snap share, target share, red-zone
        touches, and the deltas that say whether the role is growing.

        Args:
            player_id: Sleeper player_id.

        Returns:
            ``{"found", "player_id", "usage", "source"}``; ``usage`` is ``{}``
            when the player has no rollup.
        """
        pid = _player_id(player_id)
        if pid is None:
            return _bad_argument(
                "player_id must be a Sleeper id such as '4046'",
                player_id=player_id,
                usage={},
            )
        player_id = pid
        doc = await stats_store.get_usage_trends(self._store, player_id)
        return {
            "found": doc is not None,
            "player_id": player_id,
            "usage": doc or {},
            "source": source_usage(player_id),
        }

    async def get_depth_chart(self, team: str, position: str) -> dict[str, Any]:
        """Fetch a team's depth chart for one position.

        The preseason's best signal: a role is current-season fact while the
        game log is still empty.

        Returns:
            ``{"found", "team", "position", "rank", "room_size", "chart",
            "source"}``. ``rank`` is filled by the caller against a player.
        """
        team_code, position_code = _code(team), _code(position)
        if team_code is None or position_code is None:
            return _bad_argument(
                "team and position must be abbreviations such as 'KC' and 'RB'",
                team=team,
                position=position,
                chart={},
            )
        chart = await stats_store.get_depth_chart(self._store, team_code, position_code)
        return {
            "found": chart is not None,
            "team": team_code,
            "position": position_code,
            "chart": chart or {},
            "source": source_depth_chart(team_code),
        }

    async def get_def_vs_pos(self, team: str, position: str) -> dict[str, Any]:
        """Fetch how generous one defense is to one position.

        Args:
            team: NFL team abbreviation of the *defense*, e.g. ``"ATL"``.
            position: Position abbreviation, e.g. ``"RB"``.

        Returns:
            ``{"found", "split", "source"}`` where ``split`` holds
            ``points_allowed_per_game`` and ``rank`` (rank 1 = most generous
            defense, i.e. the best matchup).
        """
        team_code, position_code = _code(team), _code(position)
        if team_code is None or position_code is None:
            return _bad_argument(
                "team and position must be abbreviations such as 'ATL' and 'RB'",
                team=team,
                position=position,
                split={},
            )
        doc = await stats_store.get_def_vs_pos(self._store, team_code, position_code)
        return {
            "found": doc is not None,
            "team": team_code,
            "position": position_code,
            "split": doc or {},
            "source": source_def_vs_pos(team_code),
        }

    # -- market signal ----------------------------------------------------

    async def get_trending(self, kind: str = "add", limit: int = 25) -> dict[str, Any]:
        """Fetch the cached Sleeper trending add/drop board.

        This is what 13M+ managers are doing right now — market signal, not a
        stat. Never a substitute for usage data.

        Args:
            kind: ``"add"`` or ``"drop"``. Anything else returns ``found=False``
                rather than raising.
            limit: Maximum entries to return, Sleeper ordering preserved.

        Returns:
            ``{"found", "kind", "entries", "source"}``.
        """
        if kind not in ("add", "drop"):
            return {
                "found": False,
                "kind": kind,
                "entries": [],
                "error": "kind must be 'add' or 'drop'",
                "source": source_trending(kind),
            }
        count = _count(limit, 25)
        if count is None:
            return _bad_argument(
                "limit must be a whole number (0 for the full board)",
                kind=kind,
                entries=[],
                source=source_trending(kind),
            )
        limit = count
        entries = await stats_store.get_trending(self._store, kind)
        if limit > 0:
            entries = entries[:limit]
        for entry in entries:
            entry["source"] = source_trending(kind)
        return {
            "found": bool(entries),
            "kind": kind,
            "entries": entries,
            "source": source_trending(kind),
        }

    # -- schedule ---------------------------------------------------------

    async def get_schedule(self, team: str, week: int = 0, season: int = 0) -> dict[str, Any]:
        """Fetch one team's game for one week.

        Args:
            team: NFL team abbreviation.
            week: NFL week; 0 means the run's week.
            season: NFL season; 0 means the run's season.

        Returns:
            ``{"found", "game", "source"}``. ``found`` is ``False`` on a bye
            week or an un-ingested week; ``game`` holds ``opponent``, ``home``
            and ``kickoff``.
        """
        team_code = _code(team)
        week_no, season_no = _count(week, 0), _count(season, 0)
        if team_code is None or week_no is None or season_no is None:
            return _bad_argument(
                "team must be an abbreviation such as 'KC'; week and season whole numbers",
                team=team,
                game={},
            )
        week = week_no or self.week
        season = season_no or self.season
        doc = await stats_store.get_schedule(self._store, team_code, week, season)
        return {
            "found": doc is not None,
            "team": team_code,
            "week": week,
            "season": season,
            "game": doc or {},
            "source": source_schedule(season, week),
        }

    async def get_draft_pool(self, limit: int = 50) -> dict[str, Any]:
        """Return the draftable player universe, most market-prominent first.

        The ordering key is Sleeper's ``search_rank``: how early the market
        drafts a player. It is a **popularity signal, not a consensus ADP** from
        a projection service, and it is returned as ``market_rank`` so nothing
        downstream can mistake it for one. Never describe it as ADP.

        Each entry carries the player's identity plus their prior-season usage
        rollup, so a caller can compare what the market thinks to what the
        player actually did.

        Args:
            limit: How many players to return, capped at 200.

        Returns:
            ``{"players": [...], "count": n, "source": ...}``.
        """
        requested = _count(limit, 50)
        if requested is None:
            return _bad_argument(
                "limit must be a whole number between 1 and 200",
                players=[],
                count=0,
                source=source_draft_pool(),
            )
        capped = max(1, min(requested or 50, 200))
        pool = await stats_store.get_draft_pool(self._store, limit=capped)
        players = [
            {
                "player_id": str(entry.get("player_id") or ""),
                "name": entry.get("name") or "",
                "position": entry.get("position") or "",
                "team": entry.get("team"),
                "market_rank": stats_store.market_rank(entry.get("search_rank")),
                "years_exp": entry.get("years_exp"),
                "injury_status": entry.get("injury_status"),
                "usage": entry.get("usage"),
            }
            for entry in pool
        ]
        return {"players": players, "count": len(players), "source": source_draft_pool()}

    async def get_data_freshness(self) -> dict[str, Any]:
        """Report how stale each ingested dataset is.

        Returns:
            ``{"found", "freshness", "source"}`` where ``freshness`` maps
            dataset name -> ISO-8601 timestamp. Empty when ingest never ran.
        """
        freshness = await stats_store.get_data_freshness(self._store)
        return {
            "found": bool(freshness),
            "freshness": freshness,
            "source": SOURCE_FRESHNESS,
        }

    # -- engine-internal helpers (not exposed as function tools) -----------

    def recent_weeks(self, count: int = 4, through: int | None = None) -> list[int]:
        """Return the last ``count`` week numbers up to ``through`` (or the run week)."""
        end = int(through if through is not None else self.week)
        start = max(1, end - count + 1)
        return list(range(start, end + 1))

    async def week_scheduled(self) -> bool:
        """Whether the run week's schedule is ingested. Engine-internal.

        Tells a bye apart from a missing schedule: :meth:`get_schedule` is
        ``found=False`` for both. Memoised per ``(season, week)``, because the
        engine asks once per player.
        """
        key = (self.season, self.week)
        if key not in self._scheduled:
            doc = await self._store.get(stats_store.SCHEDULES_COLLECTION, f"{key[0]}_{key[1]}")
            self._scheduled[key] = bool(doc and doc.get("games"))
        return self._scheduled[key]

    async def scan_usage_trends(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Return every derived usage rollup in the store.

        Deliberately **not** a function tool: an LLM has no business pulling the
        whole collection into its context. The deterministic engine uses it to
        build candidate pools for ``sleepers``/``waivers``/``report``; the ADK
        stats agent reaches the same players through ``get_trending`` plus
        per-player ``get_usage_trends`` calls.
        """
        docs = await self._store.list(USAGE_TRENDS_COLLECTION, limit=limit)
        for doc in docs:
            doc.setdefault("player_id", doc.get("_id", ""))
            doc["source"] = source_usage(str(doc.get("player_id", "")))
        return docs

    async def scan_players(
        self, where: list[tuple[str, str, Any]] | None = None, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Query the ``players`` collection. Engine-internal, like
        :meth:`scan_usage_trends`."""
        docs = await self._store.list(
            stats_store.PLAYERS_COLLECTION, where=list(where) if where else None, limit=limit
        )
        for doc in docs:
            doc.setdefault("player_id", doc.get("_id", ""))
            doc["source"] = source_players(str(doc.get("player_id", "")))
        return docs

    def function_tools(self) -> list[Any]:
        """Wrap :data:`FUNCTION_TOOL_NAMES` as ADK ``FunctionTool`` objects.

        ADK is imported here rather than at module scope so the deterministic
        engine — which uses the very same methods directly — never pulls in
        google-adk or google-genai.
        """
        from google.adk.tools import FunctionTool  # noqa: PLC0415

        return [FunctionTool(getattr(self, name)) for name in FUNCTION_TOOL_NAMES]


def _best_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the most plausible candidate from a name resolution.

    Rule (documented because the eval suite asserts it): prefer candidates whose
    position is fantasy-relevant; among those, or when none are, take the first
    in ``player_index`` order. So "Josh Allen" resolves to the QB rather than the
    linebacker, and an all-defense collision still resolves to *something*.
    """
    if not candidates:
        return None
    fantasy = [c for c in candidates if str(c.get("position", "")).upper() in FANTASY_POSITIONS]
    return (fantasy or candidates)[0]
