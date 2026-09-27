"""The LLM-free analysis engine.

``DeterministicAnalysisEngine`` builds a **fully valid** response contract for
every paid endpoint using only :class:`~api.agents.tools.StatsTools` — no model
call, no network, no credentials. It exists for three reasons (DESIGN_NOTES §4):

1. **CI workhorse.** Tests and the golden-eval gate exercise the whole
   request -> 402 -> pay -> handler -> contract path with no Vertex AI.
2. **Production fallback.** If Vertex is down or a synthesis run keeps failing
   schema validation, this engine still returns something honest and correctly
   shaped rather than a 500.
3. **Fixture source.** The eval suite's traceability check ("every cited number
   exists in the seeded data") is only meaningful against an engine that cannot
   hallucinate, which makes this engine the reference implementation of the
   citation discipline the ADK prompts demand.

Honesty rules
-------------
* Every number in ``stats_cited`` is a **raw value read from a tool**, carrying
  that tool's provenance string. Derived quantities (averages, scores, ranks we
  computed) appear in prose, never as a citation — so a citation can always be
  checked against the store.
* ``sources`` is always empty: there is no research agent in this mode, and
  inventing a citation would be worse than having none.
* ``reasoning`` says which engine wrote it. A caller reading a deterministic
  body should never mistake it for the agentic product.

Heuristics
----------
Every threshold used below is a module-level constant with a comment, so the
"why did it say that" question has a code answer rather than a model answer.

Request context
---------------
Per-endpoint keys (all optional unless marked required; ``week``/``season``
default to the current NFL week/season resolved from the store):

``trending``
    ``lookback_hours`` (int, default 24), ``limit`` (int, default 25)
``sleepers``
    ``limit`` (int, default 12)
``player``
    ``name`` (str) **or** ``player_id`` (str) — one is required
``matchup``
    ``players`` (list[str] of names or Sleeper ids, 2-4) — required
``roster``
    ``roster`` (list of ``{name, player_id?, position?, starter?}``) — required;
    ``sleeper_username``, ``league_id``, ``free_agents``
    (list of ``{player_id, name, position, team?}``, the league's actual FA pool)
``waivers``
    ``limit`` (int, default 15)
``report``
    no extra keys
``team_report``
    ``sleeper_username`` (str) — required; ``league_id``, ``league_name``,
    ``roster``, ``free_agents``, and ``team_analytics``
    (the deterministic Python output of ``api/data/team_analytics.py``:
    ``{"positional_strength": [...], "manager_review": {...}}``)
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from api.agents.engine import AnalysisEngine, EngineError, response_model_for
from api.agents.tools import (
    FANTASY_POSITIONS,
    StatsTools,
    source_draft_pool,
    source_trending,
)
from api.core.config import Settings, get_settings
from api.core.store import Store
from api.core.week import current_season, current_week
from api.data.stats_store import (
    OUT_STATUSES,
    TRENDING_COLLECTION,
    is_out_status,
    normalize_name,
)
from api.data.stats_store import market_rank as sleeper_market_rank
from api.schemas import (
    AnalysisMeta,
    AnalysisResponse,
    AvailableFix,
    Confidence,
    Deficiency,
    DraftBoardPlayer,
    DraftBoardResponse,
    DraftedPlayer,
    DraftPickReview,
    DraftReportResponse,
    DraftTier,
    DropCandidate,
    InjuryFallout,
    ManagerReview,
    MatchupRanking,
    MatchupResponse,
    PlayerProfile,
    PlayerResponse,
    PositionalGrade,
    PositionalStrength,
    PreseasonOutlook,
    ReportPlayerNote,
    ReportResponse,
    RosterResponse,
    SleeperPick,
    SleepersResponse,
    StartSitCallout,
    StatCitation,
    TeamReportResponse,
    TrendingPlayer,
    TrendingResponse,
    WaiverAdd,
    WaiversResponse,
    WaiverTarget,
    WeeklyStatLine,
    WeekOneMatchup,
)

# --------------------------------------------------------------------------
# Heuristic constants. Every number the engine "decides" traces to one of these.
# --------------------------------------------------------------------------

#: Sleeper add count above which a player is already consensus — interesting for
#: the waiver board, disqualifying for a *sleeper* pick.
CONSENSUS_ADD_COUNT = 3000

#: Sleeper ``search_rank`` at or below which the market already owns a player.
#:
#: Thirty-six is the first three rounds of a twelve-team draft. A player taken
#: there is not a sleeper whatever his usage says: the crowd is not adding him
#: because it already has him. The add-count filter above cannot see that —
#: nobody is adding Derrick Henry — which is how he (search_rank 7) led the
#: first live sleepers board on 2026-09-03. Sleepers only: a widely owned
#: player whose role is growing is still "stock up" and still "emerging".
SLEEPER_MARKET_RANK_FLOOR = 36

#: Positions where the primary backup is a real preseason candidate.
#:
#: The handcuff shape: a depth-2 RB, WR or TE has a path to touches. A depth-2
#: QB is a clipboard. Cooper Rush, Andy Dalton and Josh Johnson all reached
#: the first live "emerging" pool on December garbage-time deltas, and none of
#: them was going to see a snap in week 1.
PRESEASON_BACKUP_POSITIONS: frozenset[str] = frozenset({"RB", "WR", "TE"})

#: Add count at or above which the market signal alone justifies following it.
STRONG_ADD_COUNT = 8000

#: Usage-delta score above which a sleeper pick earns 'high' confidence.
SLEEPER_SCORE_HIGH = 0.10
#: ...and above which it earns 'medium'.
SLEEPER_SCORE_MEDIUM = 0.04

#: def_vs_pos rank at or below which the matchup counts as a genuine plus
#: (rank 1 = most fantasy points allowed to that position = best matchup).
GOOD_MATCHUP_RANK = 10
#: ...and at or above which it counts as a genuine minus (32 teams).
BAD_MATCHUP_RANK = 23

#: Snap share at or above which a waiver add is a plug-and-play starter.
STARTER_SNAP_PCT = 0.50

#: Fantasy points per game thresholds for roster position grades.
GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (18.0, "A"),
    (15.0, "B+"),
    (12.0, "B"),
    (9.0, "C+"),
    (6.0, "C"),
    (3.0, "D"),
)
GRADE_FLOOR = "F"

#: How many starters a standard lineup carries per position (roster audit).
STARTER_SLOTS: dict[str, int] = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DEF": 1}
#: Extra RB/WR/TE slots filled after the per-position starters.
FLEX_SLOTS = 1

#: Regular-season week count, for pulling a full prior-season game log.
MAX_NFL_WEEK = 18

#: Data-completeness ratio at/above which a verdict is 'high' confidence.
CONFIDENCE_HIGH = 0.80
#: ...and at/above which it is 'medium'.
CONFIDENCE_MEDIUM = 0.50

#: Default board sizes.
DEFAULT_TRENDING_LIMIT = 25

#: The window the trending poll uses when its document does not record one.
TRENDING_LOOKBACK_HOURS = 24
DEFAULT_WAIVERS_LIMIT = 15
DEFAULT_SLEEPERS_LIMIT = 12
#: PRD §4.2 promises 8-12 sleeper picks; below this we say so rather than pad.
MIN_SLEEPER_PICKS = 8
#: Share of the trending board given to adds (the rest are drops).
TRENDING_ADD_SHARE = 0.7
#: How many report callouts per section.
REPORT_SECTION_SIZE = 5

#: Injury statuses that keep a player off the field (and create downstream
#: opportunity) are :data:`api.data.stats_store.OUT_STATUSES`, imported above:
#: the prediction archive must agree with the engine about who is out.

#: Positions a defense-vs-position split can describe. Kicking is not in the
#: ingested fantasy points, so a kicker "split" is 0.0 allowed everywhere and
#: rank 1 for every defense (stores written before the ingest dropped K still
#: hold those docs, so the engine must not read them either).
MATCHUP_POSITIONS: frozenset[str] = frozenset({"QB", "RB", "WR", "TE"})

#: Subtracted from an unavailable player's start score: below any real one.
OUT_SCORE_PENALTY = 1000.0

#: Positions worth streaming week to week. A streamer is picked by its
#: opponent's defense-vs-position rank, which exists only for
#: :data:`MATCHUP_POSITIONS`; a DEF or K streamer could never qualify.
STREAMER_POSITIONS: tuple[str, ...] = ("QB", "TE")

#: Endpoints whose boards are built from a data-chosen candidate list that
#: the ADK synthesizer narrates rather than picks (see :meth:`candidates`).
CANDIDATE_ENDPOINTS: frozenset[str] = frozenset({"sleepers", "report"})

#: Sort key for a board player the market has no opinion about.
_UNRANKED_BOARD = 10**6

#: How far prior-season usage may move a player from their market rank, in ranks.
#: Deliberately modest: the market order already encodes positional value and
#: injury news we do not model, so usage adjusts it rather than replacing it.
DRAFT_RANK_SHIFT = 20

#: Board tier sizes, in order. Early tiers are small because the difference
#: between pick 3 and pick 9 is real; later ones widen because it is not.
DRAFT_TIER_SIZES: tuple[int, ...] = (6, 6, 12, 12, 24, 24, 48, 48)

#: How many value/reach callouts the board surfaces on each side.
DRAFT_CALLOUTS = 8

#: Minimum places moved before a player is worth calling out. Without it the
#: board pads its values list with one-place shuffles and calls them insight.
DRAFT_MOVE_THRESHOLD = 3

#: Roster shape a balanced draft lands on, by position. Used only to grade
#: balance — a deviation is a note, not a failure.
DRAFT_TARGET_COUNTS: dict[str, tuple[int, int]] = {
    "QB": (1, 2),
    "RB": (4, 6),
    "WR": (5, 7),
    "TE": (1, 2),
}

#: Value/reach threshold in ranks — inside this band a pick is simply fair.
DRAFT_FAIR_BAND = 12


# --------------------------------------------------------------------------
# Citation collection
# --------------------------------------------------------------------------


#: Usage fields that mean something for each position. A quarterback's target
#: share is structurally zero, so citing it as evidence of a shrinking role is
#: not a weak signal — it is not a signal, and it reads as one.
_USAGE_FIELDS_BY_POSITION: dict[str, frozenset[str]] = {
    "QB": frozenset({"snap_pct", "carries", "rz_touches"}),
    "RB": frozenset({"snap_pct", "targets", "target_share", "carries", "rz_touches"}),
    "WR": frozenset({"snap_pct", "targets", "target_share", "rz_touches"}),
    "TE": frozenset({"snap_pct", "targets", "target_share", "rz_touches"}),
    "K": frozenset({"snap_pct"}),
    "DEF": frozenset({"snap_pct"}),
}


def _usage_fields_for(position: str) -> frozenset[str]:
    """Which usage fields are worth citing for ``position``; all of them if unknown."""
    return _USAGE_FIELDS_BY_POSITION.get(
        (position or "").upper(),
        frozenset({"snap_pct", "targets", "target_share", "carries", "rz_touches"}),
    )


class _Citations:
    """Accumulates :class:`~api.schemas.StatCitation` entries without duplicates.

    Only raw tool values go in here. The de-duplication key is
    ``(stat, player, source, value)`` so the same number cited for two players
    is kept, but the same read repeated is not.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, str, str, str]] = set()
        self._items: list[StatCitation] = []

    def add(
        self,
        stat: str,
        value: Any,
        source: str,
        player: str | None = None,
    ) -> None:
        """Record one cited number, if it is present and not already recorded."""
        if value is None or isinstance(value, bool):
            return
        if not isinstance(value, (int, float, str)):
            return
        if isinstance(value, str) and not value.strip():
            return
        key = (stat, player or "", source, repr(value))
        if key in self._seen:
            return
        self._seen.add(key)
        self._items.append(StatCitation(stat=stat, value=value, player=player, source=source))

    def extend_usage(
        self, usage: dict[str, Any], player: str, source: str, position: str = ""
    ) -> None:
        """Cite the interesting fields of a usage rollup.

        ``position`` drops fields that are structurally zero for it — a
        quarterback's target share is not a weak signal, it is not a signal.
        """
        allowed = _usage_fields_for(position) if position else None
        for field in (
            "snap_pct_l4w",
            "target_share_l4w",
            "rz_touches_l4w",
            "snap_pct_delta",
            "target_share_delta",
        ):
            base = field.removesuffix("_l4w").removesuffix("_delta")
            if allowed is not None and base not in allowed:
                continue
            self.add(field, usage.get(field), source, player)

    @property
    def items(self) -> list[StatCitation]:
        """The collected citations, in first-cited order."""
        return list(self._items)


def _confidence(signals: list[bool]) -> Confidence:
    """Map data completeness to a confidence tier.

    ``signals`` is one boolean per piece of evidence the endpoint *wanted*
    (player resolved, usage rollup present, matchup split present, ...). The
    ratio present maps to high/medium/low at :data:`CONFIDENCE_HIGH` /
    :data:`CONFIDENCE_MEDIUM`. Confidence is therefore a statement about how
    complete the inputs were — which is the only thing an engine with no
    judgement can honestly claim.
    """
    if not signals:
        return "low"
    ratio = sum(1 for s in signals if s) / len(signals)
    if ratio >= CONFIDENCE_HIGH:
        return "high"
    if ratio >= CONFIDENCE_MEDIUM:
        return "medium"
    return "low"


def _grade(points_per_game: float | None) -> str:
    """Letter grade for a position group's fantasy points per game."""
    if points_per_game is None:
        return GRADE_FLOOR
    for threshold, letter in GRADE_THRESHOLDS:
        if points_per_game >= threshold:
            return letter
    return GRADE_FLOOR


def _pct(value: Any) -> str:
    """Render a 0-1 share as a percentage string, or ``'n/a'``."""
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value) * 100:.0f}%"


def _num(value: Any, digits: int = 1) -> str:
    """Render a number for prose, or ``'n/a'``."""
    if not isinstance(value, (int, float)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _names(names: Sequence[str], limit: int = 3) -> str:
    """Join names for prose: ``"A"``, ``"A and B"``, ``"A, B and C"``."""
    shown = [n for n in names if n][:limit]
    if not shown:
        return ""
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + f" and {shown[-1]}"


_COUNT_WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}


def _slots_prose(slots: dict[str, int], flex: int) -> str:
    """Render the lineup shape in words, never as a dict literal."""
    parts = [f"{_COUNT_WORDS.get(n, str(n))} {pos}" for pos, n in slots.items() if n > 0]
    if flex > 0:
        parts.append(f"{_COUNT_WORDS.get(flex, str(flex))} flex")
    return _names(parts, limit=len(parts))


def _ordinal(n: int) -> str:
    """``1st``, ``2nd``, ``3rd``, ``4th`` ... ``11th``, ``12th``, ``13th``, ``21st``, ``100th``."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _is_prior_season(data_season: Any, run_season: int | None) -> bool:
    """Whether a dataset's season predates the season being analysed."""
    return isinstance(data_season, int) and isinstance(run_season, int) and data_season < run_season


def _points(line: dict[str, Any]) -> float | None:
    """Extract fantasy points from a weekly stat line, PPR first."""
    for field in ("fantasy_points_ppr", "fantasy_points"):
        value = line.get(field)
        if isinstance(value, (int, float)):
            return float(value)
    return None


class _PlayerView:
    """Everything one analysis needs to know about one player.

    Assembled by :meth:`DeterministicAnalysisEngine._load_view` from tool calls,
    with every number it read already pushed into the run's citation collector.
    """

    __slots__ = (
        "player_id",
        "name",
        "position",
        "team",
        "status",
        "player",
        "usage",
        "lines",
        "opponent",
        "def_split",
        "trend_count",
        "resolved",
        "depth_rank",
        "depth_room",
        "lines_season",
        "season",
        "schedule_known",
    )

    def __init__(self, player_id: str, name: str) -> None:
        self.player_id = player_id
        self.name = name
        self.position = "UNK"
        self.team: str | None = None
        self.status: str | None = None
        self.player: dict[str, Any] = {}
        self.usage: dict[str, Any] = {}
        self.lines: list[dict[str, Any]] = []
        self.opponent: str | None = None
        self.def_split: dict[str, Any] = {}
        self.trend_count: int | None = None
        self.resolved = False
        #: Depth-chart rank at this position (1 = starter), and how many are listed.
        self.depth_rank: int | None = None
        self.depth_room: int = 0
        #: Season the stat lines came from. Differs from the run's season only
        #: before the new season has produced any, and must be said out loud.
        self.lines_season: int | None = None
        #: Season the analysis is for. Lets the prose say "last season" when a
        #: usage rollup or defensive split predates it.
        self.season: int | None = None
        #: Whether the run week's schedule is ingested. With it, a missing
        #: opponent is a bye (or no team at all); without it, it is unknown.
        self.schedule_known = False

    # -- derived quantities (prose only, never cited) ---------------------

    @property
    def avg_points(self) -> float | None:
        """Mean fantasy points across the stat lines we actually read."""
        values = [p for p in (_points(line) for line in self.lines) if p is not None]
        return sum(values) / len(values) if values else None

    @property
    def def_rank(self) -> int | None:
        """Opponent's rank in points allowed to this position, 1 = most generous."""
        rank = self.def_split.get("rank")
        return int(rank) if isinstance(rank, (int, float)) else None

    @property
    def trend(self) -> str:
        """``'rising'`` / ``'flat'`` / ``'declining'`` / ``'unknown'``."""
        value = self.usage.get("trend")
        return str(value) if isinstance(value, str) and value else "unknown"

    @property
    def usage_delta(self) -> float:
        """Weighted usage-growth score. Target share moves the needle most."""
        target = self.usage.get("target_share_delta")
        snap = self.usage.get("snap_pct_delta")
        rz = self.usage.get("rz_touches_l4w")
        score = 0.0
        if isinstance(target, (int, float)):
            score += 3.0 * float(target)
        if isinstance(snap, (int, float)):
            score += 2.0 * float(snap)
        if isinstance(rz, (int, float)):
            score += 0.01 * float(rz)
        return score

    @property
    def matchup_bonus(self) -> float:
        """Fantasy-point nudge from the opponent's generosity to this position.

        Linear in rank across a 32-team league: rank 1 is worth +1.5, rank 32
        is worth -1.5, unknown is worth nothing.
        """
        rank = self.def_rank
        if rank is None:
            return 0.0
        return (16.5 - float(rank)) / 15.5 * 1.5

    @property
    def is_out(self) -> bool:
        """Listed with a status that keeps him off the field this week."""
        return is_out_status(self.status)

    @property
    def idle(self) -> bool:
        """No game this week: the week's schedule is ingested and names no opponent.

        The same rule as :func:`_drop_byes`, per player: the schedule doc that
        gives anyone an opponent is the one this player is missing from.
        """
        return self.schedule_known and not self.opponent

    @property
    def on_bye(self) -> bool:
        """On a team, and that team has no game this week."""
        return self.idle and bool(self.team)

    @property
    def form_score(self) -> float:
        """Recent scoring plus matchup, less the out penalty; blind to byes.

        A player listed out sinks below every available one: his average is
        often last season's full log (no recent lines to replace it), so it
        would otherwise rank him first. A bye is not a reason to cut anyone,
        which is why drop candidates rank on this rather than
        :attr:`start_score`.
        """
        base = self.avg_points
        if base is None:
            # No stat lines: fall back to usage so a debuting role still ranks.
            base = 6.0 * max(0.0, self.usage_delta)
        return base + self.matchup_bonus - (OUT_SCORE_PENALTY if self.is_out else 0.0)

    @property
    def start_score(self) -> float:
        """Ranking score for start/sit decisions: a player with no game sinks too."""
        return self.form_score - (OUT_SCORE_PENALTY if self.idle else 0.0)

    def status_note(self) -> str:
        """``"Listed IR. "`` for an unavailable player, else empty."""
        return f"Listed {self.status}, so he does not play this week. " if self.is_out else ""

    def matchup_note(self) -> str:
        """Plain-language description of the week's matchup."""
        if self.on_bye:
            return f"{self.team} is on bye this week, so he has no game."
        if self.idle:
            return "No game this week: he is not on an NFL roster."
        if not self.opponent:
            return "No scheduled opponent found for this week (bye, or schedule not ingested)."
        rank = self.def_rank
        if rank is None:
            return f"Faces {self.opponent}; no defensive split ingested for {self.position} yet."
        allowed = self.def_split.get("points_allowed_per_game")
        quality = (
            "a plus matchup"
            if rank <= GOOD_MATCHUP_RANK
            else "a tough matchup"
            if rank >= BAD_MATCHUP_RANK
            else "a neutral matchup"
        )
        basis = (
            " on last season's numbers"
            if _is_prior_season(self.def_split.get("season"), self.season)
            else ""
        )
        return (
            f"Faces {self.opponent}, {quality}: they rank {rank} against {self.position}"
            f"{basis} ({_num(allowed)} fantasy points allowed per game)."
        )

    def usage_note(self) -> str:
        """Plain-language description of the usage trend."""
        if not self.usage:
            return (
                "No usage rollup on file, so the case rests on role and news rather than "
                "measured work."
            )
        allowed = _usage_fields_for(self.position)
        parts = [f"snap share {_pct(self.usage.get('snap_pct_l4w'))}"]
        if "target_share" in allowed:
            parts.append(f"target share {_pct(self.usage.get('target_share_l4w'))}")
        if "rz_touches" in allowed:
            parts.append(f"{_num(self.usage.get('rz_touches_l4w'), 0)} red-zone touches")
        detail = ", ".join(parts)
        if _is_prior_season(self.usage.get("season"), self.season):
            return f"Last season's closing four weeks: {detail}, trending {self.trend}."
        return f"Usage is {self.trend} over the last four weeks: {detail}."

    def form_note(self) -> str:
        """Scoring average over the stat lines read, naming the season."""
        if not self.lines:
            return "No game log on file."
        when = "last season" if _is_prior_season(self.lines_season, self.season) else "this season"
        return f"Averaged {_num(self.avg_points)} points across {len(self.lines)} game(s) {when}."


class DeterministicAnalysisEngine(AnalysisEngine):
    """Build every paid response from ingested stats, with no LLM.

    Args:
        store: Store holding the ingested data.
        settings: Settings; defaults to the process settings.
    """

    name = "deterministic"

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()

    # -- entry point ------------------------------------------------------

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        """Produce the response body for ``endpoint_key``.

        See the module docstring for the per-endpoint ``request_context`` keys.
        """
        model_cls = response_model_for(endpoint_key)  # raises EngineError on a bad key
        ctx = dict(request_context or {})
        season, week = await self._resolve_scope(ctx)
        tools = StatsTools(self._store, season=season, week=week)
        cites = _Citations()

        builders = {
            "trending": self._build_trending,
            "sleepers": self._build_sleepers,
            "player": self._build_player,
            "matchup": self._build_matchup,
            "roster": self._build_roster,
            "waivers": self._build_waivers,
            "report": self._build_report,
            "team_report": self._build_team_report,
            "draft_board": self._build_draft_board,
            "draft_report": self._build_draft_report,
        }
        response = await builders[endpoint_key](tools, ctx, cites)
        if not isinstance(response, model_cls):  # pragma: no cover - defensive
            raise EngineError(
                f"deterministic engine produced {type(response).__name__} for {endpoint_key!r}"
            )
        return response

    # -- shared plumbing --------------------------------------------------

    async def _resolve_scope(self, ctx: dict[str, Any]) -> tuple[int, int]:
        """Resolve ``(season, week)``, honouring explicit context values."""
        season = ctx.get("season")
        if not isinstance(season, int):
            season = await current_season(self._store, self._settings)
        week = ctx.get("week")
        if not isinstance(week, int):
            week = await current_week(self._store, self._settings)
        return int(season), int(week)

    async def _meta(self, tools: StatsTools) -> tuple[AnalysisMeta, bool]:
        """Build the provenance envelope; also report whether ingest has run."""
        freshness = await tools.get_data_freshness()
        return (
            AnalysisMeta(
                generated_at=datetime.now(UTC),
                data_freshness=dict(freshness.get("freshness") or {}),
                model=None,
                engine="deterministic",
                cache=None,
            ),
            bool(freshness.get("found")),
        )

    async def _load_view(
        self,
        tools: StatsTools,
        cites: _Citations,
        *,
        player_id: str,
        fallback_name: str = "",
        want_weeks: bool = True,
        want_schedule: bool = True,
    ) -> _PlayerView:
        """Assemble a :class:`_PlayerView`, citing every raw number it reads."""
        view = _PlayerView(player_id, fallback_name or player_id)
        view.season = tools.season

        player_result = await tools.get_player(player_id)
        doc = player_result.get("player") or {}
        if player_result.get("found"):
            view.resolved = True
            view.player = doc
            view.name = str(doc.get("name") or fallback_name or player_id)
            view.position = str(doc.get("position") or "UNK").upper()
            team = doc.get("team")
            view.team = str(team).upper() if team else None
            view.status = doc.get("injury_status") or doc.get("status")

        usage_result = await tools.get_usage_trends(player_id)
        if usage_result.get("found"):
            view.usage = usage_result.get("usage") or {}
            cites.extend_usage(view.usage, view.name, usage_result["source"], view.position)

        if want_weeks:
            weekly = await tools.get_weekly_stats(player_id)
            view.lines = weekly.get("lines") or []
            view.lines_season = tools.season if view.lines else None
            if not view.lines and tools.week > 1:
                # An empty last-four-week window is not a preseason: a player
                # back from a month out has an earlier log this season, and
                # reaching past it to last year describes him as unplayed.
                earlier = await tools.get_weekly_stats(
                    player_id, weeks=list(range(1, tools.week + 1))
                )
                if earlier.get("lines"):
                    view.lines = earlier["lines"]
                    view.lines_season = tools.season
                    weekly = earlier
            if not view.lines:
                # Before the season's first game there is no current-season log,
                # and "0 ingested weeks, n/a points" is not an analysis. Last
                # season's production is real and useful — provided the answer
                # says which season it is, which `lines_season` makes possible.
                prior = await tools.get_weekly_stats(
                    player_id, weeks=list(range(1, MAX_NFL_WEEK + 1)), season=tools.season - 1
                )
                view.lines = prior.get("lines") or []
                view.lines_season = tools.season - 1 if view.lines else None
                weekly = prior
            fields = _usage_fields_for(view.position)
            for line in view.lines:
                source = str(line.get("source") or weekly["source"])
                cites.add("fantasy_points", _points(line), source, view.name)
                for field in fields:
                    cites.add(field, line.get(field), source, view.name)

        if view.team and view.position not in ("", "UNK"):
            depth = await tools.get_depth_chart(view.team, view.position)
            room = (depth.get("chart") or {}).get("players") or []
            view.depth_room = len(room)
            entry = _depth_entry(room, gsis_id=(view.player or {}).get("gsis_id"), name=view.name)
            if entry is not None:
                rank = entry.get("rank")
                view.depth_rank = int(rank) if isinstance(rank, int) else None
            if view.depth_rank is not None:
                cites.add("depth_chart_rank", view.depth_rank, depth["source"], view.name)

        if want_schedule:
            view.schedule_known = await tools.week_scheduled()
        if want_schedule and view.team:
            schedule = await tools.get_schedule(view.team)
            game = schedule.get("game") or {}
            opponent = game.get("opponent")
            view.opponent = str(opponent).upper() if opponent else None
            if view.opponent and view.position in MATCHUP_POSITIONS:
                split = await tools.get_def_vs_pos(view.opponent, view.position)
                if split.get("found"):
                    view.def_split = split.get("split") or {}
                    cites.add(
                        f"def_vs_pos_rank_{view.position}",
                        view.def_split.get("rank"),
                        split["source"],
                    )
                    cites.add(
                        f"points_allowed_per_game_{view.position}",
                        view.def_split.get("points_allowed_per_game"),
                        split["source"],
                    )
        return view

    async def _resolve_to_id(
        self, tools: StatsTools, cites: _Citations, token: str
    ) -> tuple[str | None, str]:
        """Resolve a name-or-id token to a player_id.

        Returns ``(player_id_or_None, display_name)``. A token that is already a
        known player_id short-circuits the name index.
        """
        token = str(token or "").strip()
        if not token:
            return None, ""
        direct = await tools.get_player(token)
        if direct.get("found"):
            doc = direct.get("player") or {}
            return token, str(doc.get("name") or token)
        resolved = await tools.resolve_player(token)
        best = resolved.get("best")
        if isinstance(best, dict) and best.get("player_id"):
            return str(best["player_id"]), str(best.get("name") or token)
        return None, token

    async def _trending_counts(self, tools: StatsTools, kind: str = "add") -> dict[str, int]:
        """Return ``{player_id: count}`` for one trending board."""
        board = await tools.get_trending(kind, limit=0)
        counts: dict[str, int] = {}
        for entry in board.get("entries") or []:
            pid = str(entry.get("player_id") or "")
            count = entry.get("count")
            if pid and isinstance(count, (int, float)):
                counts[pid] = int(count)
        return counts

    # -- GET /v1/trending -------------------------------------------------

    async def _build_trending(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> TrendingResponse:
        """Full add/drop board with a verdict per player.

        Heuristic: the board is :data:`TRENDING_ADD_SHARE` adds and the rest
        drops, Sleeper's own ordering preserved. Each player's verdict is a
        function of the *usage rollup* rather than the crowd — that is the whole
        point of paying for the board:

        * add board: usage rising -> ``add``; usage declining -> ``fade``;
          otherwise ``add`` when the crowd is at :data:`STRONG_ADD_COUNT`, else ``hold``.
        * drop board: usage rising -> ``add`` (the market is wrong, buy low);
          usage declining -> ``fade``; otherwise ``hold``.
        """
        limit = max(1, int(ctx.get("limit") or DEFAULT_TRENDING_LIMIT))
        # The board is whatever window ingest polled, not what the caller
        # asked for: stating the caller's number over 24h counts is a
        # fabricated figure the grounding guard cannot see.
        add_doc = await self._store.get(TRENDING_COLLECTION, "add")
        if isinstance(add_doc, dict) and isinstance(add_doc.get("lookback_hours"), int):
            lookback = int(add_doc["lookback_hours"])
        else:
            lookback = TRENDING_LOOKBACK_HOURS
        n_add = max(1, round(limit * TRENDING_ADD_SHARE))
        n_drop = max(0, limit - n_add)

        rows: list[TrendingPlayer] = []
        signals: list[bool] = []
        for kind, take in (("add", n_add), ("drop", n_drop)):
            if take <= 0:
                continue
            board = await tools.get_trending(kind, limit=take)
            signals.append(bool(board.get("found")))
            for entry in board.get("entries") or []:
                pid = str(entry.get("player_id") or "")
                count = int(entry.get("count") or 0)
                view = await self._load_view(
                    tools,
                    cites,
                    player_id=pid,
                    fallback_name=str(entry.get("name") or pid),
                    want_weeks=False,
                    want_schedule=False,
                )
                view.trend_count = count
                cites.add("trend_count", count, source_trending(kind), view.name)
                signals.append(bool(view.usage))
                rows.append(
                    TrendingPlayer(
                        player_id=pid,
                        name=view.name,
                        position=view.position
                        if view.resolved
                        else str(entry.get("position") or "UNK").upper(),
                        team=view.team
                        or (str(entry["team"]).upper() if entry.get("team") else None),
                        trend=kind,  # type: ignore[arg-type]
                        trend_count=count,
                        analysis=self._trending_analysis(view, kind, count, lookback),
                        verdict=self._trending_verdict(view, kind, count),
                    )
                )

        adds = sum(1 for r in rows if r.verdict == "add")
        fades = sum(1 for r in rows if r.verdict == "fade")
        meta, fresh = await self._meta(tools)
        signals.append(fresh)
        verdict = (
            f"{adds} of {len(rows)} trending moves are worth following; "
            f"{fades} look like market overreactions."
            if rows
            else "No trending data available — the Sleeper trending poll has not run yet."
        )
        return TrendingResponse(
            verdict=verdict,
            confidence=_confidence(signals),
            reasoning=_trending_reasoning(rows, lookback),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            players=rows,
            lookback_hours=lookback,
        )

    @staticmethod
    def _trending_analysis(view: _PlayerView, kind: str, count: int, lookback: int) -> str:
        """One line explaining why a trending move is happening."""
        noun = "adds" if kind == "add" else "drops"
        head = f"{count:,} {noun} in the last {lookback}h."
        if view.status and view.status != "Active":
            head += f" Listed {view.status}."
        return f"{head} {view.usage_note()}"

    @staticmethod
    def _trending_verdict(view: _PlayerView, kind: str, count: int) -> str:
        """Apply the documented add/fade/hold rules.

        A player who is out, or whose rollup no longer describes someone
        playing (:func:`_role_backs_the_usage`: stale in-season, or not backed
        by the depth chart before the season), is a ``hold`` whatever the
        crowd is doing. An "add" on an IR'd receiver is a call on a man who
        will not play, and it would be archived and scored as one.
        """
        if view.is_out or not _role_backs_the_usage(view):
            return "hold"
        trend = view.trend
        if kind == "add":
            if trend == "rising":
                return "add"
            if trend == "declining":
                return "fade"
            return "add" if count >= STRONG_ADD_COUNT else "hold"
        if trend == "rising":
            return "add"
        if trend == "declining":
            return "fade"
        return "hold"

    # -- GET /v1/sleepers -------------------------------------------------

    async def _build_sleepers(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> SleepersResponse:
        """8-12 sleeper picks: rising usage that the market has *not* found yet.

        Heuristic: scan every ingested usage rollup, score it by
        :attr:`_PlayerView.usage_delta` (target-share delta weighted 3x, snap
        delta 2x, red-zone volume as a tiebreak), then **exclude anyone the
        crowd has already claimed** — an add count at or above
        :data:`CONSENSUS_ADD_COUNT` means they are no longer a sleeper. Only
        fantasy positions with a real team survive. Per-pick confidence comes
        from the score plus whether the week's matchup is a plus one.
        """
        limit = max(1, int(ctx.get("limit") or DEFAULT_SLEEPERS_LIMIT))
        rollups = await tools.scan_usage_trends()
        chosen = await self._sleeper_candidates(tools, cites, rollups, limit)

        picks = [
            SleeperPick(
                player_id=view.player_id,
                name=view.name,
                position=view.position,
                team=view.team,
                opponent=view.opponent,
                confidence=self._sleeper_confidence(view),
                usage_note=view.usage_note(),
                matchup_note=view.matchup_note(),
                rationale=(
                    f"{view.name} ({view.position}, {view.team}) has a {view.trend} role that "
                    f"the market has not priced in"
                    + (
                        f" — only {view.trend_count:,} Sleeper adds this cycle. "
                        if view.trend_count is not None
                        else " and is not on the trending board at all. "
                    )
                    + view.matchup_note()
                ),
            )
            for view in chosen
        ]

        meta, fresh = await self._meta(tools)
        wanted = min(limit, MIN_SLEEPER_PICKS)
        signals = [fresh, bool(rollups), len(picks) >= wanted]
        signals += [bool(v.def_split) for v in chosen]
        verdict = (
            f"{len(picks)} sleeper starts for week {tools.week}, led by {picks[0].name}."
            if picks
            else f"No sleeper candidates for week {tools.week} — no rising-usage players ingested."
        )
        return SleepersResponse(
            verdict=verdict,
            confidence=_confidence(signals),
            reasoning=(
                (
                    f"{picks[0].name} leads the list. {picks[0].usage_note} "
                    f"{picks[0].matchup_note} "
                    if picks
                    else ""
                )
                + f"Candidates are ranked by four-week usage growth (target-share "
                f"delta weighted 3x, snap-share delta 2x) and anyone the crowd has already "
                f"claimed ({CONSENSUS_ADD_COUNT:,} or more Sleeper adds) is excluded on "
                f"purpose: a sleeper the whole league is adding is not one. Matchup quality "
                f"from defense-vs-position splits breaks ties."
                + (
                    ""
                    if len(picks) >= wanted
                    else f" Only {len(picks)} candidates cleared the bar this week; padding the "
                    f"list to {wanted} would mean recommending players the data "
                    f"does not support."
                )
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            season=tools.season,
            picks=picks,
        )

    async def _sleeper_candidates(
        self,
        tools: StatsTools,
        cites: _Citations,
        rollups: list[dict[str, Any]],
        limit: int,
    ) -> list[_PlayerView]:
        """Rising-usage players the crowd has not claimed, best case first.

        Shared with the ADK pipeline (:meth:`candidates`): the model narrates
        this list rather than choosing its own, because left to choose it
        reached for the most-added players — the one set a sleeper cannot come
        from.
        """
        add_counts = await self._trending_counts(tools, "add")
        scored: list[tuple[float, _PlayerView]] = []
        for rollup in rollups:
            pid = str(rollup.get("player_id") or "")
            if not pid or add_counts.get(pid, 0) >= CONSENSUS_ADD_COUNT:
                continue
            # No weekly stat lines here: a sleeper case is made from role growth
            # and matchup, so pulling (and therefore citing) every box score
            # would bloat the body with numbers the reasoning never uses.
            view = await self._load_view(tools, cites, player_id=pid, want_weeks=False)
            if not view.resolved or view.position not in FANTASY_POSITIONS or not view.team:
                continue
            if view.usage_delta <= 0:
                continue
            if _owned_by_the_market(view) or not _role_backs_the_usage(view):
                continue
            view.trend_count = add_counts.get(pid)
            if view.trend_count is not None:
                cites.add("trend_count", view.trend_count, source_trending("add"), view.name)
            scored.append((view.usage_delta + view.matchup_bonus / 20.0, view))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = _drop_byes([view for _, view in scored])
        return chosen[:limit]

    async def candidates(
        self, endpoint_key: str, request_context: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """The players a board must be built from, as plain dicts.

        For ``sleepers`` these are the rising-usage, not-yet-claimed players;
        for ``report`` they are the emerging pool. The ADK pipeline injects
        this list into the request so its synthesizer narrates a list chosen
        by the data rather than one it picked from the trending board. Other
        endpoints return ``[]``.
        """
        if endpoint_key not in CANDIDATE_ENDPOINTS:
            return []
        ctx = dict(request_context or {})
        season, week = await self._resolve_scope(ctx)
        tools = StatsTools(self._store, season=season, week=week)
        cites = _Citations()
        rollups = await tools.scan_usage_trends()
        if endpoint_key == "sleepers":
            limit = max(1, int(ctx.get("limit") or DEFAULT_SLEEPERS_LIMIT))
            views = await self._sleeper_candidates(tools, cites, rollups, limit)
        else:
            views = await self._emerging_candidates(tools, cites, rollups)
        return [_candidate_dict(view) for view in views]

    @staticmethod
    def _sleeper_confidence(view: _PlayerView) -> Confidence:
        """Per-pick tier: usage score first, matchup as the tiebreak."""
        score = view.usage_delta
        good_matchup = view.def_rank is not None and view.def_rank <= GOOD_MATCHUP_RANK
        if score >= SLEEPER_SCORE_HIGH and good_matchup:
            return "high"
        if score >= SLEEPER_SCORE_MEDIUM:
            return "medium"
        return "low"

    # -- POST /v1/player --------------------------------------------------

    async def _build_player(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> PlayerResponse:
        """Deep dive on one player: L4W lines, usage trajectory, schedule.

        ``recent_weeks`` covers the four weeks up to and including the run week;
        weeks with no ingested line are simply absent rather than zero-filled.
        """
        requested = str(ctx.get("player_id") or ctx.get("name") or "").strip()
        player_id, display = await self._resolve_to_id(tools, cites, requested)
        meta, fresh = await self._meta(tools)

        if player_id is None:
            return PlayerResponse(
                verdict=f"Could not resolve a player named {requested!r}.",
                confidence="low",
                reasoning=(
                    "The name did not match any entry in the ingested Sleeper "
                    "player index, so no stats were read and nothing is cited. Check the "
                    "spelling, or pass a Sleeper player_id directly."
                ),
                stats_cited=[],
                sources=[],
                meta=meta,
                player=PlayerProfile(
                    player_id="", name=display or requested, position="UNK", team=None
                ),
                week=tools.week,
            )

        view = await self._load_view(tools, cites, player_id=player_id, fallback_name=display)
        add_counts = await self._trending_counts(tools, "add")
        if view.player_id in add_counts:
            cites.add("trend_count", add_counts[view.player_id], source_trending("add"), view.name)

        profile = PlayerProfile(
            player_id=view.player_id,
            name=view.name,
            position=view.position,
            team=view.team,
            status=view.status,
            recent_weeks=[
                WeeklyStatLine(
                    week=int(line.get("week") or 0),
                    opponent=str(line["opponent"]).upper() if line.get("opponent") else None,
                    fantasy_points=_points(line),
                    snap_pct=line.get("snap_pct"),
                    targets=line.get("targets"),
                    target_share=line.get("target_share"),
                    carries=line.get("carries"),
                    rz_touches=line.get("rz_touches"),
                )
                for line in view.lines
            ],
            usage_trajectory=_usage_trajectory(view),
            schedule_outlook=view.matchup_note() if view.team else None,
        )

        # "No current-season log" is the condition, whether or not a prior-season
        # one was found. Requiring the fallback to succeed left a player with no
        # history at all on the old path — reporting "0 weeks, n/a points" at
        # medium confidence, which is the failure this whole change is about.
        no_current_log = view.lines_season != tools.season
        # Whether the *season* has started is a property of the week, not of
        # this player: a week-4 backup with no 2026 snaps has not played, but
        # games have. Only week 1 is the preseason (see _load_view's matching
        # `tools.week > 1` rule for reaching back to earlier weeks).
        preseason = no_current_log and tools.week <= 1
        signals = [
            view.resolved,
            bool(view.usage),
            bool(view.lines),
            bool(view.def_split),
            fresh,
        ]
        avg = view.avg_points
        call = self._player_call(view)
        return PlayerResponse(
            verdict=(
                f"{view.name} ({view.position}, {view.team or 'FA'}): "
                f"{_preseason_call(view) if preseason else call} in week {tools.week}."
            ),
            # A completeness ratio treats a missing game log as one absent input
            # of five and still reads "high". It is the input the whole answer
            # rests on: before kickoff nobody can be confident about form, only
            # about role. Cap it rather than let 4-of-5 clear the bar.
            confidence="low" if no_current_log else _confidence(signals),
            reasoning=(
                (
                    (
                        f"No {tools.season} game has been played yet, so there is no "
                        f"current-season form to read. What is current is the role: "
                        if preseason
                        else f"{view.name} has not played in {tools.season}, so there is "
                        f"no current-season form to read. What is current is the role: "
                    )
                    + f"{_depth_note(view)} "
                    + (
                        f"The production below is {view.lines_season}, across "
                        f"{len(view.lines)} week(s) at {_num(avg)} fantasy points per game — "
                        f"last season, not this one. "
                        if view.lines
                        else "No prior-season game log is ingested for him either, so the role "
                        "above is the only thing here that is measured. "
                    )
                    + f"{view.usage_note()} {view.matchup_note()}"
                )
                if no_current_log
                # Lead with why he is not playing, when he is not.
                else (
                    f"{view.status_note()}"
                    + (f"{view.matchup_note()} " if view.on_bye else "")
                    + f"{view.name} averaged {_num(avg)} fantasy points across "
                    f"{len(view.lines)} game(s) this season. {view.usage_note()}"
                    + ("" if view.on_bye else f" {view.matchup_note()}")
                )
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            player=profile,
            week=tools.week,
        )

    @staticmethod
    def _player_call(view: _PlayerView) -> str:
        """Headline call for a single-player deep dive."""
        unavailable = _availability_call(view)
        if unavailable:
            return unavailable
        rising = view.trend == "rising"
        declining = view.trend == "declining"
        rank = view.def_rank
        good = rank is not None and rank <= GOOD_MATCHUP_RANK
        bad = rank is not None and rank >= BAD_MATCHUP_RANK
        if rising and not bad:
            return "buy the usage, start with confidence"
        if declining and bad:
            return "role and matchup are both against him, bench him"
        if declining:
            return "usage is shrinking, treat as a downgrade"
        if good:
            return "matchup-based start"
        if not view.usage and not view.lines:
            return "not enough ingested data to make a call"
        return "hold, no clear edge this week"

    # -- POST /v1/matchup -------------------------------------------------

    async def _build_matchup(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> MatchupResponse:
        """Rank 2-4 players for a start/sit decision.

        Heuristic: score = mean fantasy points over the ingested weeks, plus a
        matchup nudge that is linear in the opponent's defense-vs-position rank
        (+1.5 at rank 1, -1.5 at rank 32). Every requested player is ranked
        exactly once, including ones that could not be resolved — a paid
        start/sit answer that silently drops a player is worse than one that
        says "I don't have this guy".
        """
        tokens = [str(t) for t in (ctx.get("players") or []) if str(t).strip()]
        views: list[_PlayerView] = []
        for token in tokens:
            player_id, display = await self._resolve_to_id(tools, cites, token)
            if player_id is None:
                unresolved = _PlayerView("", display or token)
                views.append(unresolved)
                continue
            views.append(
                await self._load_view(tools, cites, player_id=player_id, fallback_name=display)
            )

        # Unresolved players sort last; resolved ones by score descending. A
        # player with no game this week (bye) never ranks first — start_score
        # sinks him like an out player — but he is still ranked, never dropped.
        order = sorted(
            views, key=lambda v: (v.resolved, v.start_score if v.resolved else 0.0), reverse=True
        )
        ranked = [
            MatchupRanking(
                rank=index,
                player_id=view.player_id,
                name=view.name,
                position=view.position,
                team=view.team,
                opponent=view.opponent,
                projection_note=(
                    f"{view.status_note()}{view.form_note()} {view.matchup_note()}"
                    if view.resolved
                    else "Not found in the ingested player index — ranked last by default."
                ),
                def_vs_pos_rank=view.def_rank,
                call=self._matchup_call(index, len(order), view),
            )
            for index, view in enumerate(order, start=1)
        ]

        meta, fresh = await self._meta(tools)
        signals = [fresh] + [v.resolved for v in views] + [bool(v.def_split) for v in views]
        top = ranked[0] if ranked else None
        if top is None:
            verdict = "No players supplied to compare."
        elif top.call in ("start", "flex"):
            verdict = f"Start {top.name} first in week {tools.week}."
        else:
            # Everyone is out, on bye or unresolved: naming a start would be a
            # call on a player with no game.
            verdict = (
                f"None of these players is in line to play in week {tools.week}; "
                f"{top.name} ranks first on form."
            )
        return MatchupResponse(
            verdict=verdict,
            confidence=_confidence(signals),
            reasoning=_matchup_reasoning(ranked),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            ranked=ranked,
        )

    @staticmethod
    def _matchup_call(rank: int, total: int, view: _PlayerView) -> str:
        """Start/sit label from placement: 1 starts, 2 flexes in a 3+ pool."""
        if not view.resolved:
            return "bench"
        if view.is_out or view.idle:
            return "sit"
        if rank == 1:
            return "start"
        if rank == 2 and total >= 3:
            return "flex"
        return "sit"

    # -- POST /v1/roster --------------------------------------------------

    async def _build_roster(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> RosterResponse:
        """Audit a full roster: grades, start/sit, drops, adds.

        Heuristics: position grades come from mean fantasy points per game
        against :data:`GRADE_THRESHOLDS`; start/sit fills
        :data:`STARTER_SLOTS` per position by ``start_score``, then one flex
        from the best remaining RB/WR/TE; drop candidates are the lowest-scoring
        players with non-rising usage, and the ``risk`` field flags any whose
        usage is actually rising (i.e. think twice). Waiver adds come from the
        league's free-agent pool when the route supplied one, otherwise from the
        Sleeper trending board minus the players already rostered.
        """
        entries = [e for e in (ctx.get("roster") or []) if isinstance(e, dict)]
        views: list[_PlayerView] = []
        for entry in entries:
            token = str(entry.get("player_id") or entry.get("name") or "")
            player_id, display = await self._resolve_to_id(tools, cites, token)
            if player_id is None:
                continue
            view = await self._load_view(tools, cites, player_id=player_id, fallback_name=display)
            if not view.resolved:
                continue
            views.append(view)

        by_position: dict[str, list[_PlayerView]] = {}
        for view in views:
            by_position.setdefault(view.position, []).append(view)
        for group in by_position.values():
            group.sort(key=lambda v: v.start_score, reverse=True)

        grades = [
            PositionalGrade(
                position=position,
                grade=_grade(_group_ppg(group)),
                note=(
                    f"{len(group)} rostered; {_num(_group_ppg(group))} fantasy points per game "
                    f"across ingested weeks. Best: {group[0].name}."
                ),
            )
            for position, group in sorted(by_position.items())
        ]

        start_sit = self._roster_start_sit(by_position)
        lineup = {c.player_id for c in start_sit if c.call in ("start", "flex")}
        drops = self._roster_drops([v for v in views if v.player_id not in lineup])
        rostered = {v.player_id for v in views}
        waiver_adds = await self._roster_waiver_adds(tools, cites, ctx, rostered)

        meta, fresh = await self._meta(tools)
        signals = [fresh, bool(views), bool(grades)] + [bool(v.usage) for v in views]
        starters = [c.name for c in start_sit if c.call in ("start", "flex")]
        return RosterResponse(
            verdict=(
                f"Start {len(starters)} of {len(views)} rostered players in week {tools.week}; "
                f"{len(drops)} drop candidate(s), {len(waiver_adds)} add(s) worth a claim."
                if views
                else "No rostered players could be resolved from the supplied roster."
            ),
            confidence=_confidence(signals),
            reasoning=_roster_reasoning(grades, drops, waiver_adds),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            sleeper_username=ctx.get("sleeper_username"),
            league_id=ctx.get("league_id"),
            positional_grades=grades,
            start_sit=start_sit,
            drop_candidates=drops,
            waiver_adds=waiver_adds,
        )

    @staticmethod
    def _roster_start_sit(by_position: dict[str, list[_PlayerView]]) -> list[StartSitCallout]:
        """Fill starters per position, then one flex, then bench the rest."""
        calls: list[StartSitCallout] = []
        flex_pool: list[_PlayerView] = []
        for position, group in by_position.items():
            slots = STARTER_SLOTS.get(position, 0)
            for index, view in enumerate(group):
                if view.is_out or view.idle:
                    calls.append(_callout(view, "bench"))
                elif index < slots:
                    calls.append(_callout(view, "start"))
                else:
                    if position in ("RB", "WR", "TE"):
                        flex_pool.append(view)
                    else:
                        calls.append(_callout(view, "bench"))
        flex_pool.sort(key=lambda v: v.start_score, reverse=True)
        for index, view in enumerate(flex_pool):
            calls.append(_callout(view, "flex" if index < FLEX_SLOTS else "bench"))
        calls.sort(key=lambda c: (c.call != "start", c.call != "flex", c.name))
        return calls

    @staticmethod
    def _roster_drops(views: list[_PlayerView]) -> list[DropCandidate]:
        """Weakest scorers with non-rising usage, worst first.

        The caller passes only the bench: a player the same answer starts is
        never also the one to cut.
        """
        # form_score, not start_score: a bye sinks a starter out of the
        # lineup, and must not then make him the first name to cut.
        candidates = sorted((v for v in views if v.trend != "rising"), key=lambda v: v.form_score)[
            :3
        ]
        return [
            DropCandidate(
                player_id=view.player_id,
                name=view.name,
                position=view.position,
                reason=f"{view.form_note()} {view.usage_note()}",
                # 'high' risk means the drop could backfire — reserved for players
                # whose role is actually growing despite weak scoring.
                risk="high" if view.trend == "rising" else "low",
            )
            for view in candidates
        ]

    async def _roster_waiver_adds(
        self,
        tools: StatsTools,
        cites: _Citations,
        ctx: dict[str, Any],
        rostered: set[str],
    ) -> list[WaiverAdd]:
        """Top adds for this roster, restricted to the league pool when known."""
        pool_ids = _free_agent_ids(ctx)
        board = await tools.get_trending("add", limit=0)
        adds: list[WaiverAdd] = []
        for entry in board.get("entries") or []:
            pid = str(entry.get("player_id") or "")
            if not pid or pid in rostered:
                continue
            if pool_ids is not None and pid not in pool_ids:
                continue
            view = await self._load_view(tools, cites, player_id=pid, want_weeks=False)
            if not view.resolved:
                continue
            count = int(entry.get("count") or 0)
            cites.add("trend_count", count, source_trending("add"), view.name)
            adds.append(
                WaiverAdd(
                    player_id=pid,
                    name=view.name,
                    position=view.position,
                    team=view.team,
                    reason=(
                        f"{count:,} Sleeper adds this cycle. {view.usage_note()}"
                        + (
                            " Restricted to your league's free-agent pool."
                            if pool_ids is not None
                            else ""
                        )
                    ),
                    priority=len(adds) + 1,
                )
            )
            if len(adds) >= 5:
                break
        return adds

    # -- GET /v1/waivers --------------------------------------------------

    async def _build_waivers(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> WaiversResponse:
        """Ranked waiver big board with FAB guidance.

        Heuristic: Sleeper add count is the ranking signal (our proxy for
        rostered-percentage movement, PRD §4.2). FAB suggestion decays
        geometrically from 30% of remaining budget at rank 1 (``30 * 0.85^(rank-1)``,
        floored at 1%), which spends aggressively on the top of the board and
        keeps change for the rest of the season. ``stash_or_start`` is a usage
        call: a rising role at :data:`STARTER_SNAP_PCT`+ snaps plays now, a
        streaming position with a plus matchup is a one-week play, everything
        else is a stash.
        """
        limit = max(1, int(ctx.get("limit") or DEFAULT_WAIVERS_LIMIT))
        board_doc = await tools.get_trending("add", limit=limit)
        rows: list[_PlayerView] = []
        counts: dict[str, int] = {}
        for entry in board_doc.get("entries") or []:
            pid = str(entry.get("player_id") or "")
            if not pid:
                continue
            view = await self._load_view(tools, cites, player_id=pid, want_weeks=False)
            if not view.resolved:
                continue
            counts[pid] = int(entry.get("count") or 0)
            cites.add("trend_count", counts[pid], source_trending("add"), view.name)
            rows.append(view)

        # The crowd's order is the input, not the answer. Rank by what the
        # ingested data says the role is worth — usage growth, matchup, depth
        # chart seat — with add volume as a tiebreak, so a starter with a real
        # job outranks a name being added on hype. `_waivers_reasoning` says
        # where the two orders disagree, which is the part worth paying for.
        crowd_order = [v.player_id for v in rows]
        rows.sort(
            key=lambda v: (
                _waiver_score(v, counts.get(v.player_id, 0)),
                counts.get(v.player_id, 0),
            ),
            reverse=True,
        )
        board = [
            _waiver_row(view, rank, counts.get(view.player_id))
            for rank, view in enumerate(rows, start=1)
        ]
        meta, fresh = await self._meta(tools)
        signals = [fresh, bool(rows)] + [bool(v.usage) for v in rows]
        return WaiversResponse(
            verdict=(
                f"{board[0].name} is the week {tools.week} waiver priority; "
                f"{sum(1 for b in board if b.stash_or_start == 'start')} of {len(board)} targets "
                f"start right away."
                if board
                else f"No waiver targets for week {tools.week} — the trending poll has not run."
            ),
            confidence=_confidence(signals),
            reasoning=_waivers_reasoning(rows, crowd_order, counts),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            season=tools.season,
            board=board,
        )

    # -- GET /v1/report ---------------------------------------------------

    async def _build_report(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> ReportResponse:
        """League-wide weekly briefing.

        Sections and their heuristics:

        * **emerging** — highest usage-growth players who are *not* yet
          consensus adds (< :data:`CONSENSUS_ADD_COUNT`): the "coming up before
          the crowd" signal the PRD asks for.
        * **injury_fallout** — players whose ingested status is in
          :data:`OUT_STATUSES`, with same-team same-position teammates as
          beneficiaries, ordered by usage growth.
        * **stock_up / stock_down** — usage rollups flagged rising / declining.
        * **rookie_watch** — ``years_exp == 0`` with a usage rollup.
        * **streamers** — :data:`STREAMER_POSITIONS` players whose week opponent
          ranks at or below :data:`GOOD_MATCHUP_RANK` against them.
        """
        rollups = await tools.scan_usage_trends()
        views = await self._report_views(tools, cites, rollups)
        emerging_pool = _emerging_pool(views)
        emerging = [
            _note(
                v,
                f"Usage up ahead of the market ({_pct(v.usage.get('target_share_delta'))} target "
                f"share delta, {_pct(v.usage.get('snap_pct_delta'))} snap delta)"
                + (
                    f" with only {v.trend_count:,} Sleeper adds."
                    if v.trend_count is not None
                    else ", and not on the trending board yet."
                ),
            )
            for v in emerging_pool[:REPORT_SECTION_SIZE]
        ]

        injuries = await self._report_injuries(tools, cites, views)

        rising = sorted(
            (v for v in views if v.trend == "rising"), key=lambda v: v.usage_delta, reverse=True
        )
        falling = sorted((v for v in views if v.trend == "declining"), key=lambda v: v.usage_delta)
        stock_up = [
            _note(v, f"Role expanding. {v.usage_note()}") for v in rising[:REPORT_SECTION_SIZE]
        ]
        stock_down = [
            _note(v, f"Role shrinking. {v.usage_note()}") for v in falling[:REPORT_SECTION_SIZE]
        ]

        rookies = [v for v in views if v.player.get("years_exp") == 0]
        rookies.sort(key=lambda v: v.usage_delta, reverse=True)
        rookie_watch = [
            _note(v, f"Rookie with an ingested role. {v.usage_note()}")
            for v in rookies[:REPORT_SECTION_SIZE]
        ]

        streamers = await self._report_streamers(tools, cites, views)

        meta, fresh = await self._meta(tools)
        signals = [fresh, bool(views), bool(emerging), bool(stock_up), bool(injuries)]
        return ReportResponse(
            verdict=(
                f"Week {tools.week} briefing: {len(emerging)} emerging player(s), "
                f"{len(injuries)} injury chain(s), {len(streamers)} streamer(s)."
            ),
            confidence=_confidence(signals),
            reasoning=(
                (
                    f"{emerging[0].name} is the one to move on first. {emerging[0].note} "
                    if emerging
                    else ""
                )
                + (
                    f"The injury that matters most is {injuries[0].injured_player}"
                    + (
                        f", with {_names([b.name for b in injuries[0].beneficiaries])} "
                        f"inheriting the work. "
                        if injuries[0].beneficiaries
                        else ". "
                    )
                    if injuries
                    else ""
                )
                + f"Emerging players are ranked by four-week usage growth and capped at "
                f"{CONSENSUS_ADD_COUNT:,} Sleeper adds, so the section surfaces roles that "
                f"grew before the market noticed rather than the players it is already "
                f"adding. Injury chains list same-team, same-position teammates of every "
                f"player whose ingested status is out or doubtful. Streamers are "
                f"{'/'.join(STREAMER_POSITIONS)} facing a defense ranked "
                f"{GOOD_MATCHUP_RANK} or better in points allowed to them."
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            season=tools.season,
            emerging=emerging,
            injury_fallout=injuries,
            stock_up=stock_up,
            stock_down=stock_down,
            rookie_watch=rookie_watch,
            streamers=streamers,
        )

    async def _report_views(
        self, tools: StatsTools, cites: _Citations, rollups: list[dict[str, Any]]
    ) -> list[_PlayerView]:
        """Every player with a usage rollup, with their add count attached."""
        add_counts = await self._trending_counts(tools, "add")
        views: list[_PlayerView] = []
        for rollup in rollups:
            pid = str(rollup.get("player_id") or "")
            if not pid:
                continue
            view = await self._load_view(tools, cites, player_id=pid, want_weeks=False)
            if view.resolved:
                view.trend_count = add_counts.get(pid)
                if view.trend_count is not None:
                    cites.add("trend_count", view.trend_count, source_trending("add"), view.name)
                views.append(view)
        return views

    async def _emerging_candidates(
        self, tools: StatsTools, cites: _Citations, rollups: list[dict[str, Any]]
    ) -> list[_PlayerView]:
        """The report's emerging pool: usage growing, crowd not there yet."""
        views = await self._report_views(tools, cites, rollups)
        return _emerging_pool(views)[:REPORT_SECTION_SIZE]

    async def _report_injuries(
        self, tools: StatsTools, cites: _Citations, views: list[_PlayerView]
    ) -> list[InjuryFallout]:
        """Injured players and the teammates who inherit their work."""
        injured = await tools.scan_players(
            where=[("injury_status", "in", sorted(OUT_STATUSES))], limit=None
        )
        # Rank before cutting: the scan comes back in doc-id order, which put the
        # five lowest Sleeper ids (teamless veterans, kickers) in the section
        # the reasoning calls "the injury that matters most".
        relevant = [
            doc
            for doc in injured
            if doc.get("team") and str(doc.get("position") or "").upper() in MATCHUP_POSITIONS
        ]
        fallout: list[tuple[dict[str, Any], list[_PlayerView]]] = []
        for doc in relevant:
            team = str(doc.get("team") or "").upper()
            position = str(doc.get("position") or "").upper()
            beneficiaries = sorted(
                (
                    v
                    for v in views
                    if v.team == team
                    and v.position == position
                    and v.player_id != str(doc.get("player_id") or doc.get("_id") or "")
                ),
                key=lambda v: v.usage_delta,
                reverse=True,
            )[:3]
            fallout.append((doc, beneficiaries))
        # An injury with someone to pick up matters more than one without;
        # among those, the one the market drafts earliest.
        fallout.sort(key=lambda pair: (not pair[1], _market_prominence(pair[0])))
        return [
            InjuryFallout(
                injured_player=str(doc.get("name") or doc.get("player_id") or "unknown"),
                team=str(doc.get("team") or "").upper() or None,
                status=str(doc.get("injury_status")) if doc.get("injury_status") else None,
                beneficiaries=[
                    _note(v, f"Same team and position. {v.usage_note()}") for v in beneficiaries
                ],
            )
            for doc, beneficiaries in fallout[:REPORT_SECTION_SIZE]
        ]

    async def _report_streamers(
        self, tools: StatsTools, cites: _Citations, views: list[_PlayerView]
    ) -> list[ReportPlayerNote]:
        """One-week plays at the streaming positions with a plus matchup."""
        plays = [
            view
            for view in views
            if view.position in STREAMER_POSITIONS
            and not view.is_out
            and view.def_rank is not None
            and view.def_rank <= GOOD_MATCHUP_RANK
        ]
        # Best matchup first, then cut: scan order would keep a rank-10 play
        # and drop a rank-1 one.
        plays.sort(key=lambda v: (v.def_rank, -v.start_score))
        return [
            _note(view, f"Streaming play. {view.matchup_note()}")
            for view in plays[:REPORT_SECTION_SIZE]
        ]

    # -- POST /v1/team-report ---------------------------------------------

    async def _build_team_report(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> TeamReportResponse:
        """Team-aware report over league-relative analytics.

        Tech spec §6 is explicit that lineup efficiency, bench points lost and
        leaguemate rankings are computed **deterministically in Python** by
        ``api/data/team_analytics.py`` — the engine narrates them, it never
        produces them. So they arrive on ``request_context["team_analytics"]``.
        When the route could not compute them (no league history, Sleeper
        unavailable), the review is emitted with zeroed fields and an explicit
        observation saying so rather than invented numbers.

        Deficiencies come precomputed when ``team_analytics`` carries them (they
        are grounded in real league scoring); otherwise they are derived here as
        the position groups ranked in the bottom third. Either way the
        ``available_fixes`` are filled from the league's actual free-agent pool,
        falling back to the trending board when the route did not supply one.

        The dict is read tolerantly so a route can pass
        ``team_analytics.build_team_report_facts(...)`` straight through:
        positional strength is accepted under either
        ``positional_strength_vs_league`` or ``positional_strength``, and extra
        keys on each row (``z_score`` and friends) are ignored.
        """
        analytics = ctx.get("team_analytics") if isinstance(ctx.get("team_analytics"), dict) else {}
        # Week 1 is the one week where the played-game metrics are legitimately
        # empty rather than bad. `team_analytics` already says so in its
        # warnings; this is where that stops being a footnote.
        no_history = "no_matchup_history" in {str(w) for w in (analytics.get("warnings") or [])}
        raw_preseason = ctx.get("preseason")
        outlook = (
            await self._preseason_outlook(tools, raw_preseason, cites)
            if isinstance(raw_preseason, dict)
            else None
        )

        strengths_raw = (
            analytics.get("positional_strength_vs_league")
            or analytics.get("positional_strength")
            or []
        )
        # A "B-" computed from 0.0 points per week across 0 games is not a weak
        # assessment, it is no assessment — and it reads as the former. Drop the
        # league-relative grades entirely and let the draft's positional balance
        # stand in for them.
        if no_history:
            strengths_raw = []
        review_raw = {} if no_history else (analytics.get("manager_review") or {})

        strengths = [
            PositionalStrength(**_fields_for(PositionalStrength, row))
            for row in strengths_raw
            if isinstance(row, dict)
        ]
        for row in strengths:
            cites.add(
                f"points_per_week_{row.position}",
                row.points_per_week,
                "sleeper league matchups (team_analytics)",
                ctx.get("sleeper_username"),
            )

        league_size = (analytics.get("league") or {}).get("size") or 0
        review = self._manager_review(review_raw, {**ctx, "league_size": league_size})
        if no_history:
            review = review.model_copy(
                update={
                    "luck_note": (
                        "No games have been played yet, so there is nothing to call "
                        "lucky or unlucky."
                    ),
                    "observations": [
                        "Every played-game number above is zero because week 1 has not "
                        "kicked off. That means 'not yet played', not 'played badly' — "
                        "the preseason outlook is what can honestly be assessed today.",
                    ],
                }
            )
        deficiencies = await self._team_deficiencies(
            tools, cites, ctx, strengths, analytics.get("deficiencies")
        )

        meta, fresh = await self._meta(tools)
        signals = [fresh, bool(strengths), bool(review_raw), bool(deficiencies)]
        if no_history:
            signals = [fresh, outlook is not None, bool(outlook and outlook.draft_grade), True]
        weakest = min(strengths, key=lambda s: -s.league_rank, default=None)
        return TeamReportResponse(
            verdict=(
                _preseason_verdict(outlook, ctx)
                if no_history and outlook
                else (
                    f"Lineup efficiency {_num(review.lineup_efficiency_pct)}% "
                    f"(rank {review.efficiency_rank} of {review.league_size}); "
                    f"{len(deficiencies)} deficiency(ies) to fix."
                    if review_raw
                    else (
                        f"Roster review for {ctx.get('sleeper_username') or 'this team'}: "
                        f"{len(deficiencies)} deficiency(ies) flagged. League-relative manager "
                        f"metrics were not supplied and are reported as not computed."
                    )
                )
            ),
            confidence=_confidence(signals),
            reasoning=(
                (
                    "No game has been played in this league yet, so lineup "
                    "efficiency, luck and leaguemate rankings are reported as not computed "
                    "rather than as zeros. What is assessable is the draft — scored as "
                    "pick number minus market_rank, where a player taken later than the "
                    "market drafts them is value — and the week-1 schedule. The matchup "
                    "lean compares set lineups on Sleeper market signal, which is draft "
                    "popularity and not an ADP, so it is a lean and not a win probability."
                )
                if no_history and outlook
                else (
                    (
                        f"The weakest group is {weakest.position} at league rank "
                        f"{weakest.league_rank} of {weakest.league_size}, and fixing it is "
                        f"the highest-leverage move available. "
                        if weakest
                        else "No league-relative positional data was supplied. "
                    )
                    + "League-relative numbers (positional strength, lineup efficiency, "
                    "bench points lost, luck) are computed from Sleeper matchup history and "
                    "reported verbatim."
                )
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            week=tools.week,
            season=tools.season,
            sleeper_username=str(ctx.get("sleeper_username") or ""),
            league_id=str(ctx.get("league_id") or ""),
            league_name=ctx.get("league_name"),
            positional_strength_vs_league=strengths,
            deficiencies=deficiencies,
            manager_review=review,
            preseason_outlook=outlook,
        )

    async def _preseason_outlook(
        self, tools: StatsTools, preseason: dict[str, Any], cites: _Citations
    ) -> PreseasonOutlook | None:
        """Grade the draft and lean the week-1 matchup, for a team yet to play.

        Both halves are optional and the block is emitted with whichever
        arrived. The route refuses the sale outright when neither did, so an
        outlook with nothing in it never reaches a payer.
        """
        picks = list(preseason.get("my_picks") or [])
        # A partial draft — a rookie or keeper round — cannot be graded against a
        # global market rank: three picks out of forty score as three enormous
        # reaches and grade F, which says nothing true about the team. Report
        # that the draft happened and leave it ungraded.
        gradeable = bool(preseason.get("gradeable")) and bool(picks)
        roster, reviews = await self._grade_picks(tools, picks, cites) if gradeable else ([], [])
        balance = _draft_balance(roster) if roster else []
        deltas = [r.value_delta for r in roster if r.value_delta is not None]
        average = sum(deltas) / len(deltas) if deltas else 0.0
        grade = _draft_grade(average, balance) if roster else None

        summary = None
        if roster:
            values = sum(1 for r in reviews if r.verdict == "value")
            reaches = sum(1 for r in reviews if r.verdict == "reach")
            summary = (
                f"{len(roster)} picks: {values} clear value(s), {reaches} reach(es), "
                f"{_weakest(balance)}."
            )
        elif picks:
            summary = (
                f"{len(picks)} pick(s) — a partial draft (rookie or keeper round) rather "
                f"than one that built this starting lineup. Too few to grade against the "
                f"market's full board, so it is reported and not graded."
            )

        raw_week_one = preseason.get("week_one")
        week_one = (
            WeekOneMatchup(**_fields_for(WeekOneMatchup, raw_week_one))
            if isinstance(raw_week_one, dict)
            else None
        )
        if week_one is None and not roster and not picks:
            return None

        return PreseasonOutlook(
            draft_id=str(preseason.get("draft_id") or "") or None,
            draft_grade=grade,
            draft_summary=summary,
            positional_balance=balance,
            week_one=week_one,
            note=(
                "No games have been played, so lineup efficiency, luck and leaguemate "
                "rankings have nothing to measure and are reported as not computed. "
                "The draft and the week-1 schedule are what exist today, so they are "
                "what this report is built from."
            ),
        )

    @staticmethod
    def _manager_review(raw: dict[str, Any], ctx: dict[str, Any]) -> ManagerReview:
        """Narrate the precomputed review, or say honestly that it is missing."""
        if raw:
            payload = dict(raw)
            payload.setdefault("luck_note", "No luck analysis supplied.")
            payload.setdefault("observations", [])
            return ManagerReview(**payload)
        return ManagerReview(
            bench_points_lost=0.0,
            optimal_vs_actual=0.0,
            lineup_efficiency_pct=0.0,
            efficiency_rank=0,
            league_size=int(ctx.get("league_size") or 0),
            luck_note=(
                "Not computed: no Sleeper matchup history was supplied to this analysis, so "
                "points-for vs. points-against cannot be assessed."
            ),
            mis_start_patterns=[],
            observations=[
                "Observation, not a statistic: lineup efficiency, bench points lost and "
                "leaguemate rankings require league matchup history, which was not available "
                "for this run. The zeroed fields above mean 'not computed', not 'zero'.",
            ],
        )

    async def _team_deficiencies(
        self,
        tools: StatsTools,
        cites: _Citations,
        ctx: dict[str, Any],
        strengths: list[PositionalStrength],
        supplied: Any = None,
    ) -> list[Deficiency]:
        """Weak position groups plus fixes from the league's actual FA pool.

        ``supplied`` is ``team_analytics["deficiencies"]`` when the route
        computed them from real league scoring. Those already carry a severity
        and a detail line grounded in the league's own numbers, so they are used
        as-is and this method only fills in ``available_fixes``. Without them,
        the bottom third of :data:`strengths` is derived here instead.
        """
        weak = [
            Deficiency(**_fields_for(Deficiency, row))
            for row in (supplied or [])
            if isinstance(row, dict) and row.get("position")
        ]
        if not weak:
            candidates = [
                s for s in strengths if s.league_size and s.league_rank > (s.league_size * 2) // 3
            ]
            candidates.sort(key=lambda s: s.league_rank, reverse=True)
            weak = [
                Deficiency(
                    position=s.position,
                    severity="high" if s.league_rank >= s.league_size else "medium",
                    detail=(
                        f"{s.position} ranks {s.league_rank} of {s.league_size} at "
                        f"{_num(s.points_per_week)} points per week against a league average "
                        f"of {_num(s.league_avg_points_per_week)}."
                    ),
                )
                for s in candidates
            ]
        if not weak:
            return []

        add_counts = await self._trending_counts(tools, "add")
        pool_ids = _free_agent_ids(ctx)
        pool_supplied = pool_ids is not None
        if pool_ids is None:
            pool_ids = set(add_counts)

        # Each pool player is loaded once, whatever the number of weak groups,
        # into a scratch citation list: only the players offered as a fix put
        # their numbers in the body. A player who is out or has no team is no
        # fix; the rest rank by usage growth, then the crowd's adds, then the
        # market's draft order — never by the lexical order of their ids.
        weak_positions = {d.position for d in weak[:3]}
        candidates: list[tuple[_PlayerView, _Citations]] = []
        for pid in sorted(pool_ids):
            scratch = _Citations()
            view = await self._load_view(
                tools, scratch, player_id=pid, want_weeks=False, want_schedule=False
            )
            if not view.resolved or view.position not in weak_positions:
                continue
            if view.is_out or not view.team:
                continue
            view.trend_count = add_counts.get(pid)
            candidates.append((view, scratch))
        candidates.sort(
            key=lambda pair: (
                -pair[0].usage_delta,
                -(pair[0].trend_count or 0),
                _market_prominence(pair[0].player),
            )
        )

        out: list[Deficiency] = []
        for deficiency in weak[:3]:
            fixes: list[AvailableFix] = []
            for view, scratch in candidates:
                if view.position != deficiency.position:
                    continue
                for cite in scratch.items:
                    cites.add(cite.stat, cite.value, cite.source, cite.player)
                fixes.append(
                    AvailableFix(
                        player_id=view.player_id,
                        name=view.name,
                        position=view.position,
                        team=view.team,
                        why=(
                            "Available "
                            + ("in your league. " if pool_supplied else "on the trending board. ")
                            + view.usage_note()
                        ),
                    )
                )
                if len(fixes) >= 3:
                    break
            out.append(deficiency.model_copy(update={"available_fixes": fixes}))
        return out

    # --------------------------------------------------------------------------
    # Small builders kept out of the class for readability
    # -- GET /v1/draft-board ----------------------------------------------

    async def _build_draft_board(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> DraftBoardResponse:
        """Tiered pre-draft board, ranked against the market.

        Heuristic: start from the market order (Sleeper ``search_rank``, which
        already encodes positional value and offseason news we do not model),
        then move each player by up to :data:`DRAFT_RANK_SHIFT` ranks according
        to how their prior-season usage compares to others **at their own
        position**. Adjusting the market rather than replacing it is the honest
        posture: usage is the one thing we measure better than the crowd, and
        everything else about draft value we measure worse.

        A player with no prior-season usage — a rookie, or someone who missed
        the year — is not guessed at. They keep their market rank and say so.
        """
        limit = max(1, min(int(ctx.get("limit") or 200), 200))
        pool = await tools.get_draft_pool(limit=limit)
        players = pool.get("players") or []

        scored = _draft_scores(players)
        ranked = sorted(players, key=lambda p: _adjusted_rank(p, scored))
        # The market's own ordering *of this same set*. Comparing our rank
        # against a global search_rank would call everyone a value: ask for 200
        # players out of a market that ranks thousands and every rank is lower
        # than every market_rank. Position-within-set is the like-for-like
        # comparison, and it is what "we moved them up" actually means.
        market_positions = {
            str(player.get("player_id") or ""): position
            for position, player in enumerate(
                sorted(players, key=lambda p: _adjusted_rank(p, {})), start=1
            )
        }
        board = [
            _draft_row(player, rank, scored, market_positions)
            for rank, player in enumerate(ranked, start=1)
        ]
        for row in board[:DRAFT_CALLOUTS]:
            if row.market_rank is not None:
                cites.add("market_rank", row.market_rank, source_draft_pool(), row.name)

        # A player with no usage is held at his market rank; he only moves
        # because scored players move around him, which is not a call about
        # him. Calling that a value (or a reach) would put a number on a guess.
        graded = [row for row in board if row.value_delta is not None and row.player_id in scored]
        # Filter by the signed threshold *before* truncating. Slicing the sorted
        # list alone pads both callouts with zero-delta rows — and on a short
        # board even with rows that moved the other way — while the response
        # calls all of them values or reaches.
        values = [r for r in graded if (r.value_delta or 0) >= DRAFT_MOVE_THRESHOLD]
        reaches = [r for r in graded if (r.value_delta or 0) <= -DRAFT_MOVE_THRESHOLD]
        values.sort(key=lambda r: -(r.value_delta or 0))
        reaches.sort(key=lambda r: r.value_delta or 0)
        values = values[:DRAFT_CALLOUTS]
        reaches = reaches[:DRAFT_CALLOUTS]

        meta, fresh = await self._meta(tools)
        with_usage = sum(1 for p in players if p.get("usage"))
        return DraftBoardResponse(
            verdict=(
                f"{board[0].name} heads the board; {len(values)} "
                f"player{'s' if len(values) != 1 else ''} worth taking earlier than the "
                f"market does and {len(reaches)} worth waiting on."
                if board
                else "No draft board — the player dump has not been ingested."
            ),
            confidence=_confidence([fresh, bool(board), with_usage >= len(players) // 2]),
            reasoning=(
                (
                    f"{values[0].name} is the board's biggest value: the market has him "
                    f"{values[0].value_delta} places lower than his usage earns. "
                    if values
                    else ""
                )
                + (
                    f"{reaches[0].name} is the biggest reach at market rank "
                    f"{reaches[0].market_rank}: {reaches[0].note} "
                    if reaches
                    else ""
                )
                + f"The board starts from Sleeper's draft-popularity order "
                f"(market_rank) and shifts each player by up to {DRAFT_RANK_SHIFT} ranks "
                f"on prior-season usage — snap share, target share and red-zone work, "
                f"compared within position. {with_usage} of {len(players)} players had "
                f"usage to judge; the rest hold their market rank rather than being "
                f"guessed at. market_rank is a popularity signal, not a consensus ADP."
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            season=tools.season,
            scoring=str(ctx.get("scoring") or "ppr"),
            tiers=_draft_tiers(board),
            values=values,
            reaches=reaches,
        )

    # -- POST /v1/draft-report ---------------------------------------------

    async def _grade_picks(
        self, tools: StatsTools, picks: Sequence[dict[str, Any]], cites: _Citations
    ) -> tuple[list[DraftedPlayer], list[DraftPickReview]]:
        """Score a list of Sleeper draft picks against the market's ordering.

        Shared by ``draft_report``, which grades a draft on its own, and by
        ``team_report`` in the preseason, where the draft is the only thing that
        has actually happened yet. One implementation so the two endpoints can
        never disagree about what a pick was worth.
        """
        roster: list[DraftedPlayer] = []
        reviews: list[DraftPickReview] = []

        for pick in picks:
            player_id = str(pick.get("player_id") or "")
            if not player_id:
                continue
            found = await tools.get_player(player_id)
            player = found.get("player") or {}
            if not player.get("player_id"):
                continue
            # Sleeper's unranked sentinel is not a rank: read as one, a pick
            # at 150 was "9,999,849 places of value" and graded the draft F.
            ranked = sleeper_market_rank(player.get("search_rank"))
            pick_no = int(pick.get("pick_no") or 0)
            # pick_no - market_rank: taking a player LATER than the market
            # ranks them is value. The other way round reads as a reach.
            delta = (pick_no - ranked) if ranked is not None else None
            name = str(player.get("name") or "")
            drafted = DraftedPlayer(
                player_id=player_id,
                name=name,
                position=str(player.get("position") or ""),
                team=player.get("team"),
                round=int(pick.get("round") or 0),
                pick_no=pick_no,
                market_rank=ranked,
                value_delta=delta,
            )
            roster.append(drafted)
            if delta is not None:
                cites.add("market_rank", ranked, source_draft_pool(), name)
                reviews.append(
                    DraftPickReview(
                        player_id=player_id,
                        name=name,
                        round=drafted.round,
                        pick_no=pick_no,
                        verdict=_pick_verdict(delta),
                        value_delta=delta,
                        note=_pick_note(drafted, delta),
                    )
                )
        return roster, reviews

    async def _build_draft_report(
        self, tools: StatsTools, ctx: dict[str, Any], cites: _Citations
    ) -> DraftReportResponse:
        """Grade one completed draft, pick by pick.

        Heuristic: every pick is scored as ``pick_no - market_rank``. Taking a
        player later than the market drafts them is value; taking them earlier
        is a reach. Inside :data:`DRAFT_FAIR_BAND` ranks it is neither, because
        no draft board is precise to a dozen picks.

        The overall grade combines that average against positional balance, so a
        roster of nothing but value picks at one position still grades down.
        """
        picks = list(ctx.get("picks") or [])
        roster, reviews = await self._grade_picks(tools, picks, cites)

        balance = _draft_balance(roster)
        deltas = [r.value_delta for r in roster if r.value_delta is not None]
        average = sum(deltas) / len(deltas) if deltas else 0.0
        grade = _draft_grade(average, balance)

        # Ranked by how much value actually moved. Ordering by pick number (or
        # by draft order, which is what taking the first three reaches did)
        # buries the biggest bargain and the worst mistake behind whichever
        # happened later in the draft.
        best = sorted(
            [r for r in reviews if r.verdict == "value"],
            key=lambda r: -(r.value_delta or 0),
        )[:3]
        worst = sorted(
            [r for r in reviews if r.verdict == "reach"],
            key=lambda r: r.value_delta or 0,
        )[:3]

        meta, fresh = await self._meta(tools)
        return DraftReportResponse(
            verdict=(
                f"{grade} draft: {len(best)} clear values, {len(worst)} reaches, "
                f"{_weakest(balance)}."
                if roster
                else "No picks found for that draft — it may not have started yet."
            ),
            confidence=_confidence([fresh, bool(roster), len(deltas) >= len(roster) // 2]),
            reasoning=(
                (f"The pick of the draft was {best[0].name}: {best[0].note} " if best else "")
                + (f"The costliest reach was {worst[0].name}: {worst[0].note} " if worst else "")
                + f"Each pick is scored as pick number minus market_rank, so "
                f"a player taken later than the market drafts them is value and earlier is "
                f"a reach; anything inside {DRAFT_FAIR_BAND} ranks is fair. The average "
                f"across {len(deltas)} gradeable picks was {average:+.0f} ranks, combined "
                f"with positional balance for the overall grade."
            ),
            stats_cited=cites.items,
            sources=[],
            meta=meta,
            draft_id=str(ctx.get("draft_id") or ""),
            season=tools.season,
            grade=grade,
            roster=roster,
            positional_balance=balance,
            best_picks=best,
            worst_picks=worst,
            week_one_plan=_week_one_plan(roster, balance),
        )


# --------------------------------------------------------------------------


def _usage_trajectory(view: _PlayerView) -> str | None:
    """Usage trend, naming only the deltas that mean something for the position."""
    if not view.usage:
        return None
    allowed = _usage_fields_for(view.position)
    parts = [f"snap share delta {_num(view.usage.get('snap_pct_delta'), 3)}"]
    if "target_share" in allowed:
        parts.append(f"target share delta {_num(view.usage.get('target_share_delta'), 3)}")
    return f"{view.trend} (" + ", ".join(parts) + ")"


def _depth_note(view: _PlayerView) -> str:
    """Where the player sits on his team's depth chart, in words."""
    if view.depth_rank is None:
        return (
            f"no {view.position} depth chart is ingested for {view.team or 'his team'} yet, "
            "so the starting job cannot be confirmed."
        )
    label = {1: "the starter", 2: "the primary backup"}.get(view.depth_rank)
    seat = label or f"number {view.depth_rank}"
    room = f" of {view.depth_room} listed" if view.depth_room else ""
    return f"{view.name} is {seat}{room} at {view.position} for {view.team}."


def _preseason_call(view: _PlayerView) -> str:
    """A week-1 verdict built on role, because form does not exist yet."""
    unavailable = _availability_call(view)
    if unavailable:
        return unavailable
    if view.depth_rank == 1:
        return f"starting {view.position} for {view.team}, a real week 1 option"
    if view.depth_rank == 2:
        return f"backup {view.position} behind the starter, a bench stash"
    if view.depth_rank is not None:
        return f"number {view.depth_rank} at {view.position}, not startable yet"
    return "role unconfirmed before kickoff; no depth chart ingested"


def _availability_call(view: _PlayerView) -> str | None:
    """The verdict for a player who will not play this week, else ``None``.

    Checked before form, role or matchup: none of them matter for a player who
    is out or whose team is on bye. The words are load-bearing —
    :func:`api.data.predictions.verdict_call` reads "bench" as a sit claim and
    finds no claim at all in "on bye", which is right: a bye is not a call.
    """
    if view.is_out:
        return f"listed {view.status}, bench him"
    if view.on_bye:
        return "on bye"
    return None


def _free_agent_ids(ctx: dict[str, Any]) -> set[str] | None:
    """The league's actual free-agent pool, or ``None`` when none was supplied.

    ``None`` and an empty set mean different things and must not be conflated:
    ``None`` = "the route did not tell us who is available, fall back to the
    trending board"; an empty set would mean "nobody is available", which would
    silently blank every recommendation. Accepts either the route's
    ``free_agents`` list (dicts or bare ids) or
    ``team_analytics["free_agent_pool"]["player_ids"]`` from
    ``api/data/team_analytics.py``.
    """
    ids: set[str] = set()
    for entry in ctx.get("free_agents") or []:
        if isinstance(entry, dict) and entry.get("player_id"):
            ids.add(str(entry["player_id"]))
        elif isinstance(entry, str) and entry.strip():
            ids.add(entry.strip())
    analytics = ctx.get("team_analytics")
    if isinstance(analytics, dict):
        pool = analytics.get("free_agent_pool")
        if isinstance(pool, dict):
            ids.update(str(pid) for pid in pool.get("player_ids") or [] if pid)
    return ids or None


def _fields_for(model: type[BaseModel], row: dict[str, Any]) -> dict[str, Any]:
    """Keep only the keys ``model`` declares.

    ``api/data/team_analytics.py`` deliberately returns a few extra keys per row
    (``z_score`` and friends) for callers that want them. Filtering here rather
    than relying on pydantic's default extra-ignore keeps the intent explicit
    and survives a future ``extra="forbid"`` on those contracts.
    """
    return {key: value for key, value in row.items() if key in model.model_fields}


def _group_ppg(group: list[_PlayerView]) -> float | None:
    """Mean fantasy points per game across a position group."""
    values = [v.avg_points for v in group if v.avg_points is not None]
    return sum(values) / len(values) if values else None


def _trending_reasoning(rows: list[TrendingPlayer], lookback: int) -> str:
    """Lead with the calls that disagree with the crowd, then say the method."""
    fades = [r.name for r in rows if r.verdict == "fade"]
    buy_low = [r.name for r in rows if r.trend == "drop" and r.verdict == "add"]
    follow = [r.name for r in rows if r.trend == "add" and r.verdict == "add"]
    lead: list[str] = []
    if fades:
        lead.append(
            f"Fade {_names(fades)}: the crowd is chasing a name whose measured usage is declining."
        )
    if buy_low:
        lead.append(
            f"{_names(buy_low)} {'is' if len(buy_low) == 1 else 'are'} being dropped while "
            f"usage rises, which is the buy-low signal."
        )
    if follow:
        lead.append(
            f"The adds worth following are {_names(follow)}, where rising usage backs the "
            f"add count."
        )
    if not lead:
        lead.append("No move on the board is backed by a rising usage trend yet.")
    lead.append(
        f"Each verdict compares the {lookback}h add/drop count against the player's "
        f"last-four-week usage trend; a count alone never earns an add."
    )
    return " ".join(lead)


def _matchup_reasoning(ranked: list[MatchupRanking]) -> str:
    """Name the start and the closest alternative, then say the method."""
    if not ranked:
        return "Nothing to rank: no players were supplied."
    top = ranked[0]
    lead = [f"{top.name} ranks first. {top.projection_note}"]
    if len(ranked) > 1:
        runner = ranked[1]
        lead.append(f"{runner.name} is the closest alternative. {runner.projection_note}")
    lead.append(
        "Players are ordered by recent scoring average adjusted for the week's defensive "
        "matchup (defense-vs-position rank, 1 = most generous)."
    )
    return " ".join(lead)


def _roster_reasoning(
    grades: list[PositionalGrade], drops: list[DropCandidate], adds: list[WaiverAdd]
) -> str:
    """Lead with the single most valuable move, then the weakest group, then the method."""
    lead: list[str] = []
    if adds and drops:
        lead.append(
            f"The move that matters is claiming {adds[0].name} and cutting {drops[0].name}. "
            f"{adds[0].reason}"
        )
    elif adds:
        lead.append(f"The move that matters is claiming {adds[0].name}. {adds[0].reason}")
    elif drops:
        lead.append(f"The move that matters is cutting {drops[0].name}. {drops[0].reason}")
    weakest = min(grades, key=lambda g: _grade_order(g.grade), default=None)
    if weakest is not None:
        lead.append(f"{weakest.position} is the thinnest group at {weakest.grade}: {weakest.note}")
    lead.append(
        f"Position grades are mean fantasy points per game across the ingested weeks; the "
        f"lineup fills {_slots_prose(STARTER_SLOTS, FLEX_SLOTS)} by matchup-adjusted scoring "
        f"average, and drop candidates are the weakest scorers whose usage is not rising."
    )
    return " ".join(lead)


_GRADE_SCALE = ("F", "D-", "D", "D+", "C-", "C", "C+", "B-", "B", "B+", "A-", "A", "A+")


def _grade_order(grade: str) -> int:
    """Position of a letter grade on the scale, lowest first; unknown sorts high."""
    try:
        return _GRADE_SCALE.index(grade)
    except ValueError:
        return len(_GRADE_SCALE)


def _waiver_score(view: _PlayerView, count: int) -> float:
    """Rank a waiver target by what the data says the role is worth.

    ``start_score`` carries usage growth and the matchup; a depth-chart starter
    gets a real bonus and a primary backup a small one; add volume enters only
    logarithmically, so ten times the adds is worth one point, not ten. The
    crowd breaks ties, it does not set the order.
    """
    score = view.start_score
    if view.depth_rank == 1:
        score += 3.0
    elif view.depth_rank == 2:
        score += 1.0
    if count > 0:
        score += math.log10(count)
    return score


def _waivers_reasoning(
    rows: list[_PlayerView], crowd_order: list[str], counts: dict[str, int]
) -> str:
    """Say where the board disagrees with the crowd, then the method."""
    if not rows:
        return "No waiver targets: the trending poll has not run."
    lead = [f"{rows[0].name} tops the board. {rows[0].usage_note()}"]
    crowd_rank = {pid: index for index, pid in enumerate(crowd_order, start=1)}
    for index, view in enumerate(rows, start=1):
        if crowd_rank.get(view.player_id, index) > index:
            passed = [
                v.name
                for v in rows[index:]
                if crowd_rank.get(v.player_id, 10**6) < crowd_rank.get(view.player_id, 10**6)
            ]
            if passed:
                lead.append(
                    f"{view.name} ranks ahead of {_names(passed)} despite fewer adds "
                    f"({counts.get(view.player_id, 0):,}): "
                    + (
                        f"{_depth_note(view)}"
                        if view.depth_rank is not None
                        else "the usage trend is the stronger signal."
                    )
                )
                break
    lead.append(
        "Targets are ranked by usage growth, matchup and depth-chart seat, with add volume "
        "only as a tiebreak; snap share, target share and red-zone work decide stash versus "
        "start, and FAB guidance decays from 30% of remaining budget at rank 1."
    )
    return " ".join(lead)


def _callout(view: _PlayerView, call: str) -> StartSitCallout:
    """Build one roster start/sit row."""
    return StartSitCallout(
        player_id=view.player_id,
        name=view.name,
        position=view.position,
        call=call,  # type: ignore[arg-type]
        reason=f"{view.status_note()}{view.form_note()} {view.matchup_note()}",
    )


def _depth_entry(room: list[dict[str, Any]], *, gsis_id: Any, name: str) -> dict[str, Any] | None:
    """The depth-chart entry for one player, by gsis id first, then by name.

    Chart entries carry nflverse's gsis id as ``player_id``, so the id is the
    exact join. Names are the fallback and compare normalized: nflverse writes
    "D.J. Moore" where Sleeper writes "DJ Moore", and an exact match would
    leave the preseason's best signal blank for both.
    """
    if gsis_id:
        for entry in room:
            if entry.get("player_id") and str(entry["player_id"]) == str(gsis_id):
                return entry
    wanted = normalize_name(name) if name else ""
    if not wanted:
        return None
    for entry in room:
        if normalize_name(str(entry.get("name") or "")) == wanted:
            return entry
    return None


def _waiver_row(view: _PlayerView, rank: int, count: int | None) -> WaiverTarget:
    """Build one waiver big-board row, including the FAB suggestion.

    FAB decays geometrically (``30 * 0.85^(rank-1)``, floored at 1%) so the top
    of the board gets real money and the tail costs pocket change.
    """
    fab = round(max(1.0, 30.0 * (0.85 ** (rank - 1))), 1)
    rising = view.trend == "rising"
    snaps = view.usage.get("snap_pct_l4w")
    plays_now = (
        rising
        and not view.is_out
        and not view.idle
        and isinstance(snaps, (int, float))
        and float(snaps) >= STARTER_SNAP_PCT
    )
    good_matchup = view.def_rank is not None and view.def_rank <= GOOD_MATCHUP_RANK
    if plays_now:
        label = "start"
    elif view.position in STREAMER_POSITIONS and good_matchup:
        label = "streamer"
    else:
        label = "stash"
    return WaiverTarget(
        rank=rank,
        player_id=view.player_id,
        name=view.name,
        position=view.position,
        team=view.team,
        trend_count=count,
        stash_or_start=label,  # type: ignore[arg-type]
        fab_bid_pct=fab,
        rationale=(
            (f"{_depth_note(view)} " if view.depth_rank is not None else "")
            + f"{view.usage_note()} {view.matchup_note()} {(count or 0):,} Sleeper adds this cycle."
        ),
    )


def _emerging_pool(views: list[_PlayerView]) -> list[_PlayerView]:
    """Rising usage the crowd has not claimed, strongest growth first.

    No market-rank floor here, on purpose: a first-round pick whose role is
    growing is not a sleeper but he is emerging. The preseason role rule does
    apply — see :func:`_role_backs_the_usage`.
    """
    pool = [
        v
        for v in views
        if v.usage_delta > 0
        and (v.trend_count or 0) < CONSENSUS_ADD_COUNT
        and _role_backs_the_usage(v)
    ]
    pool.sort(key=lambda v: v.usage_delta, reverse=True)
    return _drop_byes(pool)


def _drop_byes(views: list[_PlayerView]) -> list[_PlayerView]:
    """Drop players with no game this week, once the week's schedule is known.

    A pick on bye is archived and later scored against a stat line that does
    not exist: a guaranteed permanent miss. A missing opponent means "bye" only
    when someone else in the pool has one; with none at all the week's schedule
    is simply not ingested, and that is no reason to empty the board.
    """
    if not any(v.opponent for v in views):
        return views
    return [v for v in views if v.opponent]


def _market_prominence(player: dict[str, Any]) -> int:
    """Sleeper ``search_rank`` for sorting, most drafted first; unranked last."""
    rank = player.get("search_rank")
    return rank if isinstance(rank, int) and not isinstance(rank, bool) else 1_000_000


def _owned_by_the_market(view: _PlayerView) -> bool:
    """Whether the market drafts this player inside :data:`SLEEPER_MARKET_RANK_FLOOR`."""
    rank = view.player.get("search_rank")
    return (
        isinstance(rank, int) and not isinstance(rank, bool) and rank <= SLEEPER_MARKET_RANK_FLOOR
    )


def _role_backs_the_usage(view: _PlayerView) -> bool:
    """Whether a candidate's usage trend is evidence of a job he actually holds.

    In-season the rollup *is* the evidence — it was measured this season — and
    this passes. Before the season the rollup is last December's: garbage-time
    snaps for a backup, a week-18 start for a third-string QB. Then the current
    depth chart has to agree: the player is the starter, or the primary backup
    at a position where that is a real role (:data:`PRESEASON_BACKUP_POSITIONS`).

    No chart at all (``depth_rank`` is ``None``) excludes the player in
    preseason. Without it the only evidence is those December deltas, and the
    first live boards showed exactly what they are worth.
    """
    if view.is_out:
        return False
    if not _is_prior_season(view.usage.get("season"), view.season):
        return _rollup_is_current(view.usage)
    if view.depth_rank == 1:
        return True
    return view.depth_rank == 2 and view.position in PRESEASON_BACKUP_POSITIONS


def _rollup_is_current(usage: dict[str, Any]) -> bool:
    """Whether an in-season rollup still describes a player who is playing.

    Rollups cover a player's last four games *played*: a receiver who rose in
    weeks 1-4 and went on IR in week 5 still reads "rising" in week 9. One
    missed week (a bye) is allowed; more means the trend is history.
    """
    through = usage.get("through_week")
    last = usage.get("last_week_played")
    if not isinstance(through, int) or not isinstance(last, int):
        return True
    return last >= through - 1


def _candidate_dict(view: _PlayerView) -> dict[str, Any]:
    """A candidate as the ADK pipeline receives it: identity, usage, matchup."""
    return {
        "player_id": view.player_id,
        "name": view.name,
        "position": view.position,
        "team": view.team,
        "opponent": view.opponent,
        "trend_count": view.trend_count,
        "usage": {
            key: view.usage.get(key)
            for key in (
                "snap_pct_l4w",
                "target_share_l4w",
                "rz_touches_l4w",
                "snap_pct_delta",
                "target_share_delta",
                "trend",
                "season",
            )
            if key in view.usage
        },
        "def_vs_pos_rank": view.def_rank,
        "usage_note": view.usage_note(),
        "matchup_note": view.matchup_note(),
    }


def _note(view: _PlayerView, text: str) -> ReportPlayerNote:
    """Build one weekly-report callout row."""
    return ReportPlayerNote(
        player_id=view.player_id or None,
        name=view.name,
        position=view.position,
        team=view.team,
        note=text,
    )


# --------------------------------------------------------------------------
# Draft helpers
# --------------------------------------------------------------------------


def _usage_score(usage: dict[str, Any] | None) -> float | None:
    """Collapse a usage rollup into one 0..1 number, or ``None`` if there is none.

    Snap share carries the most weight because it is the least position-specific
    signal of "this player is on the field"; target share and red-zone work
    separate the players who matter once they are out there.
    """
    if not usage:
        return None
    snap = float(usage.get("snap_pct_l4w") or 0.0)
    target = float(usage.get("target_share_l4w") or 0.0)
    redzone = float(usage.get("rz_touches_l4w") or 0.0)
    score = 0.5 * min(snap, 1.0) + 0.3 * min(target * 3.0, 1.0) + 0.2 * min(redzone / 8.0, 1.0)
    if usage.get("trend") == "rising":
        score += 0.05
    elif usage.get("trend") == "declining":
        score -= 0.05
    return max(0.0, min(1.0, score))


def _draft_scores(players: list[dict[str, Any]]) -> dict[str, float]:
    """Map player_id -> usage percentile **within that player's position**.

    Comparing a running back's snap share to a quarterback's would rank every
    quarterback first, so the percentile is taken inside each position group.
    Players with no usage are absent from the map and keep their market rank.
    """
    by_position: dict[str, list[tuple[str, float]]] = {}
    for player in players:
        score = _usage_score(player.get("usage"))
        if score is None:
            continue
        position = str(player.get("position") or "")
        by_position.setdefault(position, []).append((str(player.get("player_id") or ""), score))

    percentiles: dict[str, float] = {}
    for group in by_position.values():
        group.sort(key=lambda item: item[1])
        last = len(group) - 1
        for index, (player_id, _) in enumerate(group):
            percentiles[player_id] = index / last if last > 0 else 0.5
    return percentiles


def _adjusted_rank(player: dict[str, Any], scored: dict[str, float]) -> float:
    """Market rank shifted by usage percentile; lower sorts earlier."""
    market = player.get("market_rank")
    base = float(market) if isinstance(market, int) else float(_UNRANKED_BOARD)
    percentile = scored.get(str(player.get("player_id") or ""))
    if percentile is None:
        return base
    return base - (percentile - 0.5) * 2 * DRAFT_RANK_SHIFT


def _draft_row(
    player: dict[str, Any],
    rank: int,
    scored: dict[str, float],
    market_positions: dict[str, int],
) -> DraftBoardPlayer:
    """Build one board row, including the honest note about what is known."""
    market = player.get("market_rank")
    market_rank = int(market) if isinstance(market, int) else None
    market_position = market_positions.get(str(player.get("player_id") or ""))
    usage = player.get("usage")
    percentile = scored.get(str(player.get("player_id") or ""))

    if percentile is None:
        note = "No prior-season usage on file — held at the market rank rather than guessed."
    else:
        note = (
            f"{_pct(usage.get('snap_pct_l4w'))} snap share, "
            f"{_pct(usage.get('target_share_l4w'))} target share, "
            f"{_num(usage.get('rz_touches_l4w'), 0)} red-zone touches last season "
            f"({_ordinal(int(percentile * 100))} percentile at "
            f"{player.get('position') or 'their spot'})."
        )
    return DraftBoardPlayer(
        player_id=str(player.get("player_id") or ""),
        name=str(player.get("name") or ""),
        position=str(player.get("position") or ""),
        team=player.get("team"),
        rank=rank,
        market_rank=market_rank,
        value_delta=(market_position - rank) if market_position is not None else None,
        note=note,
    )


def _draft_tiers(board: list[DraftBoardPlayer]) -> list[DraftTier]:
    """Split the ranked board into tiers of increasing width."""
    tiers: list[DraftTier] = []
    start = 0
    for index, size in enumerate(DRAFT_TIER_SIZES, start=1):
        chunk = board[start : start + size]
        if not chunk:
            break
        positions = sorted({p.position for p in chunk if p.position})
        label = f"Picks {start + 1}-{start + len(chunk)}"
        if positions:
            label += f" · {'/'.join(positions[:3])}"
        tiers.append(DraftTier(tier=index, label=label, players=chunk))
        start += size
    if start < len(board):
        rest = board[start:]
        tiers.append(
            DraftTier(
                tier=len(tiers) + 1, label=f"Picks {start + 1}-{len(board)} · depth", players=rest
            )
        )
    return tiers


def _pick_verdict(delta: int) -> str:
    """Value, fair or reach — with a band, because boards are not that precise."""
    if delta > DRAFT_FAIR_BAND:
        return "value"
    if delta < -DRAFT_FAIR_BAND:
        return "reach"
    return "fair"


def _pick_note(pick: DraftedPlayer, delta: int) -> str:
    """One line on how a pick compared to the market."""
    if delta > DRAFT_FAIR_BAND:
        return (
            f"Taken at {pick.pick_no} with a market rank of {pick.market_rank} — "
            f"{delta} picks later than the market drafts him."
        )
    if delta < -DRAFT_FAIR_BAND:
        return (
            f"Taken at {pick.pick_no} with a market rank of {pick.market_rank} — "
            f"{abs(delta)} picks early; he was likely still there later."
        )
    return f"Taken at {pick.pick_no} against a market rank of {pick.market_rank} — market price."


def _preseason_verdict(outlook: PreseasonOutlook, ctx: dict[str, Any]) -> str:
    """One line for a team that has not played: what it drafted, who it plays."""
    who = ctx.get("sleeper_username") or "this team"
    parts: list[str] = []
    if outlook.draft_grade:
        parts.append(f"{outlook.draft_grade} draft")
    if outlook.week_one:
        opponent = outlook.week_one.opponent_team_name or "an unnamed opponent"
        parts.append(f"week 1 vs {opponent} is a {outlook.week_one.lean}")
    detail = "; ".join(parts) if parts else "nothing assessable yet"
    return f"No games played yet for {who} — {detail}."


def _draft_balance(roster: list[DraftedPlayer]) -> list[PositionalGrade]:
    """Grade each position group against a workable roster shape."""
    counts: dict[str, int] = {}
    for pick in roster:
        counts[pick.position] = counts.get(pick.position, 0) + 1

    grades: list[PositionalGrade] = []
    for position, (low, high) in DRAFT_TARGET_COUNTS.items():
        have = counts.get(position, 0)
        if low <= have <= high:
            grade, note = "A-", f"{have} rostered — a workable {position} room."
        elif have < low:
            short = low - have
            grade = "C" if short == 1 else "D"
            note = f"Only {have} {position}s; {short} short of a comfortable {low}."
        else:
            grade = "B-"
            note = f"{have} {position}s is more than the {high} a lineup needs — tradeable depth."
        grades.append(PositionalGrade(position=position, grade=grade, note=note))
    return grades


def _draft_grade(average_delta: float, balance: list[PositionalGrade]) -> str:
    """Combine average pick value with positional balance into one letter.

    Balance can only cost, never add: a perfectly shaped roster of reaches is
    still a bad draft, while a lopsided roster of values is a fixable one.
    """
    if average_delta >= 20:
        base = 5
    elif average_delta >= 8:
        base = 4
    elif average_delta >= -8:
        base = 3
    elif average_delta >= -20:
        base = 2
    else:
        base = 1
    penalties = sum(1 for grade in balance if grade.grade in {"C", "D"})
    return ("F", "D", "C", "B-", "B+", "A-")[max(0, min(5, base - penalties))]


def _weakest(balance: list[PositionalGrade]) -> str:
    """Name the thinnest position group, for the verdict line."""
    weak = [g for g in balance if g.grade in {"C", "D"}]
    if not weak:
        return "no position left short"
    return ", ".join(g.position for g in weak) + " left thin"


def _week_one_plan(roster: list[DraftedPlayer], balance: list[PositionalGrade]) -> list[str]:
    """Concrete pre-Week-1 actions, derived from what the draft actually produced."""
    plan: list[str] = []
    for grade in balance:
        if grade.grade in {"C", "D"}:
            plan.append(
                f"Add a {grade.position} before Week 1 — {grade.note} "
                f"Waivers are cheapest now, before anyone is injured."
            )
    late = [p for p in roster if p.value_delta is not None and p.value_delta > DRAFT_FAIR_BAND]
    if late:
        names = ", ".join(p.name for p in late[:3])
        plan.append(
            f"Confirm the role behind your value picks ({names}) in preseason usage "
            f"reporting — the market discounted them for a reason worth checking."
        )
    if not plan:
        plan.append("No structural holes. Watch the depth charts and hold your waiver budget.")
    return plan
