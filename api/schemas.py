"""Response and request contracts for every Play Clock endpoint.

These models *are* the product surface: they render into the OpenAPI spec that
AI agents read before paying, and they are the structured-output schema the ADK
synthesis agent must satisfy (tech spec §5). Field descriptions are therefore
part of the deliverable, not decoration.

The core promise (PRD §5) is that **every paid response** carries
:class:`AnalysisResponse`'s fields — ``verdict``, ``confidence``, ``reasoning``,
``stats_cited``, ``sources``, ``meta`` — so a caller can always tell what was
claimed, how sure we are, and which numbers and articles it came from. Trust is
the product.

Composition convention
----------------------
Per-endpoint models **extend** :class:`AnalysisResponse` (rather than nesting it)
so the top-level verdict block is always at the root of the JSON body and agents
can parse any response with one shape. Endpoint-specific payload lives in extra
fields alongside it.

Compact mode (DESIGN_NOTES "agent tier"): ``reasoning`` is deliberately the only
long-prose field, so a ``format=compact`` variant can drop it without touching
any other part of the contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

# --------------------------------------------------------------------------
# Shared primitives
# --------------------------------------------------------------------------

#: How much we trust a verdict. Drives UI emphasis and agent thresholding.
Confidence = Literal["high", "medium", "low"]

#: How a draft pick fared against the market rank it was taken at.
DraftPickVerdict = Literal["value", "fair", "reach"]

#: Verdict for a trending player.
TrendVerdict = Literal["add", "fade", "hold"]

#: Whether Sleeper managers are adding or dropping a player.
TrendKind = Literal["add", "drop"]

#: Whether a waiver target is a bench stash or an immediate starter.
StashOrStart = Literal["stash", "start", "streamer"]

#: Start/sit call for a rostered player.
StartSitCall = Literal["start", "sit", "flex", "bench"]

#: Where a response body came from. ``fresh`` = generated now and cached for
#: later payers; ``hit`` = served from ``response_cache``; ``miss`` = generated
#: now and intentionally not cached (personalized endpoints).
CacheState = Literal["hit", "miss", "fresh"]

#: Required attribution string — nflverse is CC-BY 4.0 (PRD §8).
ATTRIBUTION = "Data: nflverse (CC-BY 4.0), Sleeper"

# Request fields that become Sleeper URL path segments are pattern-checked here,
# at the edge, so ``../`` or ``?`` can never re-point a live call (httpx resolves
# dot segments). ``api/data/sleeper.py`` also quotes every segment; this is the
# layer that tells the caller why, with a 422 that releases the payment claim.

#: A Sleeper league or draft id: numeric (a snowflake), never anything else.
SleeperId = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,20}$")]

#: A Sleeper username. Letters, digits, ``_``, ``-`` and ``.``, not starting with
#: a dot, so neither ``.`` nor ``..`` can be a path segment.
SleeperUsername = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,39}$"),
]

#: A free-text player name or id supplied by the caller.
PlayerText = Annotated[str, StringConstraints(max_length=100)]

#: Longest pasted roster accepted. Deep dynasty benches run ~30.
MAX_ROSTER_SIZE = 40


class StatCitation(BaseModel):
    """One hard number backing a claim.

    The stats agent may only cite numbers that came back from a tool call — never
    a model-recalled figure (tech spec §5). ``source`` records which dataset and
    slice the number came from so a reader can go verify it.
    """

    stat: str = Field(description="Stat name, e.g. 'target_share', 'snap_pct', 'rz_touches'.")
    value: str | float | int = Field(description="The cited value, as returned by the data tool.")
    player: str | None = Field(
        default=None, description="Player this stat is about; None for team/defense-level stats."
    )
    source: str = Field(
        description=(
            "Dataset and slice, e.g. 'nflverse weekly_stats 2026w3' or 'sleeper trending/add'."
        )
    )


class SourceRef(BaseModel):
    """A news/research citation produced by the google_search-grounded agent."""

    title: str = Field(description="Headline or page title.")
    url: str | None = Field(
        default=None, description="Canonical URL, when the grounding tool gave one."
    )
    published: str | None = Field(
        default=None,
        description="Publication timestamp or human label, e.g. '2026-09-14' or '2h ago'.",
    )


class AnalysisMeta(BaseModel):
    """Provenance envelope attached to every paid response."""

    generated_at: datetime = Field(description="UTC timestamp when this analysis was produced.")
    data_freshness: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-dataset as-of markers, e.g. {'weekly_stats': '2026-09-16T09:00:00Z', "
            "'trending': '2026-09-16T13:30:00Z'}. Mirrors the meta/freshness doc "
            "that ingest maintains."
        ),
    )
    model: str | None = Field(
        default=None, description="Model id used for synthesis; None for the deterministic engine."
    )
    engine: str | None = Field(
        default=None,
        description=(
            "Which engine produced the body: 'deterministic' (computed, no model), "
            "'narrated' (computed body, model-written prose checked against it), or "
            "'adk' (the full agent pipeline). None on bodies cached before this field existed."
        ),
    )
    cache: CacheState | None = Field(
        default=None,
        description="Whether the body was served from cache, freshly cached, or uncached.",
    )
    attribution: str = Field(
        default=ATTRIBUTION, description="Data source attribution (nflverse is CC-BY 4.0)."
    )


class AnalysisResponse(BaseModel):
    """Base contract shared by every paid response (PRD §5).

    Endpoint models subclass this, so ``verdict``/``confidence``/``reasoning``/
    ``stats_cited``/``sources``/``meta`` sit at the root of every paid body.
    """

    model_config = ConfigDict(extra="forbid")

    verdict: str = Field(description="The headline call, in one sentence. The thing they paid for.")
    confidence: Confidence = Field(description="Confidence in the verdict.")
    reasoning: str = Field(
        description="Written justification. The only long-prose field; dropped in compact mode."
    )
    stats_cited: list[StatCitation] = Field(
        default_factory=list, description="Every hard number referenced by the reasoning."
    )
    sources: list[SourceRef] = Field(
        default_factory=list, description="News/research citations supporting the reasoning."
    )
    meta: AnalysisMeta = Field(description="Provenance: when, from what data, by which model.")


# --------------------------------------------------------------------------
# GET /v1/trending  (0.10 USDC)
# --------------------------------------------------------------------------


class TrendingPlayer(BaseModel):
    """One row of the trending add/drop board, with our take on it."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position, e.g. 'RB', 'WR', 'TE', 'QB', 'K', 'DEF'.")
    team: str | None = Field(
        default=None, description="NFL team abbreviation; None for free agents."
    )
    trend: TrendKind = Field(description="Whether managers are adding or dropping.")
    trend_count: int = Field(description="Sleeper add/drop count over the lookback window.")
    analysis: str = Field(
        description="Why this is happening — usage, injury, or opportunity change."
    )
    verdict: TrendVerdict = Field(
        description="'add' = follow the crowd, 'fade' = the market is wrong, 'hold' = no action."
    )


class TrendingResponse(AnalysisResponse):
    """Full trending board (top 25) with per-player stat context."""

    players: list[TrendingPlayer] = Field(description="Ranked trending players, adds and drops.")
    lookback_hours: int = Field(default=24, description="Sleeper trending lookback window used.")


class TrendingPreviewPlayer(BaseModel):
    """Free-teaser row: identity and trend count only, no analysis (PRD §4.1)."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    trend: TrendKind = Field(description="Add or drop.")
    trend_count: int = Field(description="Sleeper add/drop count over the lookback window.")


class TrendingPreviewResponse(BaseModel):
    """Free ``GET /v1/trending/preview`` body. Carries no verdict block by design."""

    model_config = ConfigDict(extra="forbid")

    players: list[TrendingPreviewPlayer] = Field(description="Top 5 trending players, no analysis.")
    lookback_hours: int = Field(default=24, description="Sleeper trending lookback window used.")
    upsell: str = Field(
        default="Full board with analysis and add/fade verdicts: GET /v1/trending",
        description="Pointer to the paid endpoint.",
    )
    attribution: str = Field(default=ATTRIBUTION, description="Data source attribution.")


# --------------------------------------------------------------------------
# GET /v1/sleepers?week=N  (0.25 USDC)
# --------------------------------------------------------------------------


class SleeperPick(BaseModel):
    """One weekly sleeper recommendation."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    opponent: str | None = Field(default=None, description="Week opponent team abbreviation.")
    confidence: Confidence = Field(description="Confidence tier for this individual pick.")
    usage_note: str = Field(
        description="The usage trend that makes them a sleeper: snap %, target share, RZ touches."
    )
    matchup_note: str = Field(description="Why the matchup helps — opponent defense vs. position.")
    rationale: str = Field(description="One-paragraph case for starting them this week.")


class SleepersResponse(AnalysisResponse):
    """8–12 weekly sleeper picks with usage and matchup reasoning."""

    week: int = Field(description="NFL week these picks apply to.")
    season: int = Field(description="NFL season year.")
    picks: list[SleeperPick] = Field(description="Sleeper picks, best first.")


# --------------------------------------------------------------------------
# POST /v1/player  (0.15 USDC)
# --------------------------------------------------------------------------


class WeeklyStatLine(BaseModel):
    """One player-week stat line as ingested from nflverse."""

    week: int = Field(description="NFL week.")
    opponent: str | None = Field(default=None, description="Opponent team abbreviation.")
    fantasy_points: float | None = Field(
        default=None, description="Fantasy points (PPR unless noted)."
    )
    snap_pct: float | None = Field(default=None, description="Share of team offensive snaps, 0–1.")
    targets: int | None = Field(default=None, description="Targets.")
    target_share: float | None = Field(default=None, description="Share of team targets, 0–1.")
    carries: int | None = Field(default=None, description="Rush attempts.")
    rz_touches: int | None = Field(
        default=None, description="Red-zone touches (carries + targets)."
    )


class PlayerProfile(BaseModel):
    """Identity + trajectory block for a single player."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    status: str | None = Field(
        default=None, description="Injury/roster status, e.g. 'Questionable'."
    )
    recent_weeks: list[WeeklyStatLine] = Field(
        default_factory=list, description="Last four weeks of stat lines, oldest first."
    )
    usage_trajectory: str | None = Field(
        default=None,
        description="Direction of usage: 'rising', 'flat', 'declining', with the delta.",
    )
    schedule_outlook: str | None = Field(
        default=None, description="Upcoming schedule difficulty summary."
    )


class PlayerResponse(AnalysisResponse):
    """Deep dive on one player."""

    player: PlayerProfile = Field(description="The player under analysis.")
    week: int = Field(description="Week the analysis is scoped to.")


# --------------------------------------------------------------------------
# POST /v1/matchup  (0.25 USDC)
# --------------------------------------------------------------------------


class MatchupRanking(BaseModel):
    """One player's placement in a start/sit comparison."""

    rank: int = Field(description="1 = start this one first.")
    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    opponent: str | None = Field(default=None, description="Week opponent team abbreviation.")
    projection_note: str = Field(
        description="Expected outcome in plain language, with the drivers."
    )
    def_vs_pos_rank: int | None = Field(
        default=None,
        description=(
            "Opponent's rank in fantasy points allowed to this position (1 = worst defense)."
        ),
    )
    call: StartSitCall = Field(description="Start / sit / flex call for this player.")


class MatchupResponse(AnalysisResponse):
    """Start/sit ranking across 2–4 players."""

    week: int = Field(description="Week the comparison is scoped to.")
    ranked: list[MatchupRanking] = Field(description="Players ordered best-to-worst start.")


# --------------------------------------------------------------------------
# POST /v1/roster  (0.50 USDC)
# --------------------------------------------------------------------------


class PositionalGrade(BaseModel):
    """Letter grade for one roster position group."""

    position: str = Field(description="Position group, e.g. 'RB'.")
    grade: str = Field(description="Letter grade, e.g. 'A-', 'C+'.")
    note: str = Field(description="One-line justification for the grade.")


class StartSitCallout(BaseModel):
    """A single start/sit recommendation within a roster audit."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    call: StartSitCall = Field(description="Start / sit / flex / bench.")
    reason: str = Field(description="Why, grounded in usage and matchup.")


class DropCandidate(BaseModel):
    """A rosterable player the manager should consider cutting."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    reason: str = Field(
        description="Why they are droppable — usage collapse, role loss, bye-week math."
    )
    risk: Confidence = Field(description="Risk of the drop backfiring: 'high' = think twice.")


class WaiverAdd(BaseModel):
    """A waiver-wire add suggested for this specific roster."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    reason: str = Field(description="Why they fit this roster's needs.")
    priority: int = Field(description="1 = claim first.")


class RosterResponse(AnalysisResponse):
    """Full roster audit for one manager's team."""

    week: int = Field(description="Week the audit is scoped to.")
    sleeper_username: str | None = Field(
        default=None, description="Sleeper username, when the roster was pulled rather than pasted."
    )
    league_id: str | None = Field(default=None, description="Sleeper league id, when known.")
    positional_grades: list[PositionalGrade] = Field(
        default_factory=list, description="Grade per position group."
    )
    start_sit: list[StartSitCallout] = Field(
        default_factory=list, description="This week's start/sit calls."
    )
    drop_candidates: list[DropCandidate] = Field(
        default_factory=list, description="Players worth cutting, worst first."
    )
    waiver_adds: list[WaiverAdd] = Field(
        default_factory=list,
        description=(
            "Top available adds. When a league is known, restricted to that league's "
            "free-agent pool."
        ),
    )


# --------------------------------------------------------------------------
# GET /v1/waivers?week=N  (0.25 USDC)
# --------------------------------------------------------------------------


class WaiverTarget(BaseModel):
    """One row of the waiver-wire big board."""

    rank: int = Field(description="Board rank, 1 = top priority.")
    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    trend_count: int | None = Field(
        default=None, description="Sleeper add count — our proxy for % rostered movement."
    )
    stash_or_start: StashOrStart = Field(
        description="'start' = plays now, 'stash' = future value, 'streamer' = one-week play."
    )
    fab_bid_pct: float = Field(
        description="Suggested FAB bid as a percentage of remaining budget, e.g. 12.5."
    )
    rationale: str = Field(
        description="Why they are worth the claim, grounded in usage/opportunity."
    )


class WaiversResponse(AnalysisResponse):
    """Ranked waiver-wire big board for the week."""

    week: int = Field(description="NFL week this board applies to.")
    season: int = Field(description="NFL season year.")
    board: list[WaiverTarget] = Field(description="Waiver targets, best first.")


# --------------------------------------------------------------------------
# GET /v1/report?week=N  (0.50 USDC)
# --------------------------------------------------------------------------


class ReportPlayerNote(BaseModel):
    """A single named callout inside a weekly-report section."""

    player_id: str | None = Field(default=None, description="Sleeper player_id, when resolved.")
    name: str = Field(description="Player name.")
    position: str | None = Field(default=None, description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    note: str = Field(description="The callout itself, grounded in a stat or a news event.")


class InjuryFallout(BaseModel):
    """An injury and the downstream opportunity it creates."""

    injured_player: str = Field(description="Who went down.")
    team: str | None = Field(default=None, description="Their NFL team abbreviation.")
    status: str | None = Field(default=None, description="Reported status, e.g. 'Out 4-6 weeks'.")
    beneficiaries: list[ReportPlayerNote] = Field(
        default_factory=list, description="Handcuffs and role-inheritors, best first."
    )


class ReportResponse(AnalysisResponse):
    """League-wide weekly briefing — the flagship cached product."""

    week: int = Field(description="NFL week this briefing covers.")
    season: int = Field(description="NFL season year.")
    emerging: list[ReportPlayerNote] = Field(
        default_factory=list,
        description="Usage-delta x trending detections: players 'coming up' before consensus.",
    )
    injury_fallout: list[InjuryFallout] = Field(
        default_factory=list, description="Injuries and their handcuff/opportunity chains."
    )
    stock_up: list[ReportPlayerNote] = Field(default_factory=list, description="Rising value.")
    stock_down: list[ReportPlayerNote] = Field(default_factory=list, description="Falling value.")
    rookie_watch: list[ReportPlayerNote] = Field(
        default_factory=list, description="Rookies whose role is trending up."
    )
    streamers: list[ReportPlayerNote] = Field(
        default_factory=list, description="One-week plays: QB/TE streaming options."
    )


# --------------------------------------------------------------------------
# POST /v1/team-report  (0.75 USDC)
# --------------------------------------------------------------------------


class PositionalStrength(BaseModel):
    """How one position group grades against the manager's actual leaguemates.

    Computed deterministically in Python from Sleeper matchup history — the LLM
    narrates these numbers, it never produces them (tech spec §6).
    """

    position: str = Field(description="Position group, e.g. 'WR'.")
    league_rank: int = Field(description="Rank within the league, 1 = best.")
    league_size: int = Field(description="Number of teams in the league.")
    points_per_week: float = Field(description="Average points from this position group per week.")
    league_avg_points_per_week: float = Field(description="League average for this position group.")
    grade: str = Field(description="Letter grade relative to leaguemates.")


class AvailableFix(BaseModel):
    """A concrete roster fix drawn from the league's actual free-agent pool."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="NFL team abbreviation.")
    why: str = Field(description="How this player addresses the deficiency.")


class Deficiency(BaseModel):
    """A weak spot on the roster plus what can actually be done about it."""

    position: str = Field(description="Position group that is weak.")
    severity: Confidence = Field(description="'high' = costing wins now.")
    detail: str = Field(description="What is wrong, with the supporting numbers.")
    available_fixes: list[AvailableFix] = Field(
        default_factory=list,
        description=(
            "Fixes restricted to players unrostered in THIS league (derived free-agent pool)."
        ),
    )


class ManagerReview(BaseModel):
    """Manager performance review computed from Sleeper matchup history.

    Every field here is deterministic arithmetic (tech spec §6). Behavioural
    observations that are not computable belong in ``observations`` and must be
    labelled as observations, not statistics.
    """

    bench_points_lost: float = Field(
        description=(
            "Total points scored by benched players who should have started, season to date."
        )
    )
    optimal_vs_actual: float = Field(
        description="Optimal-lineup points minus actual points, season to date."
    )
    lineup_efficiency_pct: float = Field(description="actual / optimal as a percentage, e.g. 92.4.")
    efficiency_rank: int = Field(description="Lineup-efficiency rank in the league, 1 = best.")
    league_size: int = Field(description="Number of teams in the league.")
    luck_note: str = Field(
        description=(
            "Points-for vs. points-against and record-vs-expected analysis in plain language."
        )
    )
    expected_wins: float | None = Field(
        default=None, description="Wins expected from points scored against an all-play schedule."
    )
    actual_wins: int | None = Field(default=None, description="Actual wins to date.")
    mis_start_patterns: list[str] = Field(
        default_factory=list,
        description="Recurring positional mis-start patterns, each with its cost.",
    )
    observations: list[str] = Field(
        default_factory=list,
        description="Non-computable behavioural observations. Explicitly not statistics.",
    )


class WeekOneMatchup(BaseModel):
    """The upcoming week-1 game, before any of it has been played."""

    opponent_roster_id: int | None = Field(default=None, description="Opposing roster id.")
    opponent_team_name: str | None = Field(default=None, description="Opposing team label.")
    my_market_score: float | None = Field(
        default=None, description="Summed market signal of this team's set starters."
    )
    opponent_market_score: float | None = Field(
        default=None, description="Summed market signal of the opponent's set starters."
    )
    my_starters_scored: str | None = Field(
        default=None, description="How many of this lineup's starters carried a market rank."
    )
    opponent_starters_scored: str | None = Field(
        default=None, description="How many of the opposing starters carried a market rank."
    )
    lean: str = Field(
        description="Plain-language lean: clear edge / slight edge / toss-up / underdog."
    )
    basis: str = Field(description="What the lean is computed from, and what it is not.")


class PreseasonOutlook(BaseModel):
    """What can honestly be said about a team that has not played a game yet.

    Week 1 is the one week where every historical team metric is legitimately
    empty. Rather than report zeros as if they were assessments, the report
    substitutes the two things that *have* happened: the draft, and the
    schedule. See :class:`WeekOneMatchup` for why the matchup is a lean and
    never a win probability.
    """

    draft_id: str | None = Field(default=None, description="Sleeper draft graded, if any.")
    draft_grade: str | None = Field(default=None, description="Overall draft letter grade.")
    draft_summary: str | None = Field(
        default=None, description="One line on values, reaches and thin spots."
    )
    positional_balance: list[PositionalGrade] = Field(
        default_factory=list,
        description="Draft-based positional grades, standing in for league-relative ones.",
    )
    week_one: WeekOneMatchup | None = Field(
        default=None, description="The week-1 matchup lean, when an opponent is scheduled."
    )
    note: str = Field(description="Why this block is present instead of played-game metrics.")


class TeamReportResponse(AnalysisResponse):
    """Team-aware deep report: roster strength, deficiencies, and a manager review."""

    week: int = Field(description="Week the report is scoped to.")
    season: int = Field(description="NFL season year.")
    sleeper_username: str = Field(description="Sleeper username the report was built for.")
    league_id: str = Field(description="Sleeper league id analysed.")
    league_name: str | None = Field(default=None, description="League display name.")
    positional_strength_vs_league: list[PositionalStrength] = Field(
        default_factory=list, description="Position-by-position grading against actual leaguemates."
    )
    deficiencies: list[Deficiency] = Field(
        default_factory=list, description="Weak spots with league-available fixes."
    )
    manager_review: ManagerReview = Field(description="Deterministic manager performance review.")
    preseason_outlook: PreseasonOutlook | None = Field(
        default=None,
        description=(
            "Draft grade and week-1 matchup lean. Present only before any game has "
            "been played, when the played-game metrics above are legitimately empty."
        ),
    )


# --------------------------------------------------------------------------
# Request models
# --------------------------------------------------------------------------
# GET /v1/draft-board  (0.25 USDC)
# --------------------------------------------------------------------------


class DraftBoardPlayer(BaseModel):
    """One player on the draft board, ranked against the market."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="Team abbreviation; None for free agents.")
    rank: int = Field(description="Our overall board rank, 1 = first off the board.")
    market_rank: int | None = Field(
        default=None,
        description=(
            "Sleeper draft-popularity rank (`search_rank`), lower = drafted earlier. "
            "This is a market signal, NOT a consensus ADP from a projection service."
        ),
    )
    value_delta: int | None = Field(
        default=None,
        description=(
            "How many places we move the player against the market's ordering OF "
            "THIS SAME BOARD. Positive means we rank him earlier than the market "
            "does (a value); negative means the market likes him more than we do. "
            "Compared position-to-position, not against the global market_rank, "
            "which would call every player on a truncated board a value."
        ),
    )
    note: str = Field(description="Why this player sits here, grounded in prior-season usage.")


class DraftTier(BaseModel):
    """A group of players who should be treated as interchangeable on the clock."""

    tier: int = Field(description="Tier number, 1 = best.")
    label: str = Field(description="Short tier name, e.g. 'Every-down RB1s'.")
    players: list[DraftBoardPlayer] = Field(description="Players in this tier, best first.")


class DraftBoardResponse(AnalysisResponse):
    """A tiered pre-draft board with value and reach calls against the market."""

    season: int = Field(description="NFL season this board is for.")
    scoring: str = Field(default="ppr", description="Scoring format the board assumes.")
    tiers: list[DraftTier] = Field(description="Tiered board, best tier first.")
    values: list[DraftBoardPlayer] = Field(
        default_factory=list,
        description="Players we rank furthest ABOVE their market rank — draft-day values.",
    )
    reaches: list[DraftBoardPlayer] = Field(
        default_factory=list,
        description="Players we rank furthest BELOW their market rank — let someone else.",
    )


# --------------------------------------------------------------------------
# POST /v1/draft-report  (0.75 USDC)
# --------------------------------------------------------------------------


class DraftedPlayer(BaseModel):
    """One pick made in the draft being graded."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    position: str = Field(description="Position abbreviation.")
    team: str | None = Field(default=None, description="Team abbreviation.")
    round: int = Field(description="Draft round this pick was made in.")
    pick_no: int = Field(description="Overall pick number.")
    market_rank: int | None = Field(
        default=None, description="Sleeper draft-popularity rank at the time of ingest."
    )
    value_delta: int | None = Field(
        default=None,
        description=(
            "pick_no - market_rank. Positive means the player was taken LATER than "
            "the market drafts him — value; negative means earlier — a reach."
        ),
    )


class DraftPickReview(BaseModel):
    """A pick worth calling out, good or bad."""

    player_id: str = Field(description="Sleeper player_id.")
    name: str = Field(description="Full player name.")
    round: int = Field(description="Draft round.")
    pick_no: int = Field(description="Overall pick number.")
    verdict: DraftPickVerdict = Field(description="Value, fair, or reach.")
    value_delta: int | None = Field(
        default=None,
        description="pick_no - market_rank. Positive = value; the callouts rank on this.",
    )
    note: str = Field(description="Why, grounded in usage and market rank.")


class DraftReportResponse(AnalysisResponse):
    """A graded post-draft report on one drafted roster."""

    draft_id: str = Field(description="Sleeper draft id this report covers.")
    season: int = Field(description="NFL season.")
    grade: str = Field(description="Overall letter grade for the draft, e.g. 'B+'.")
    roster: list[DraftedPlayer] = Field(description="Every pick, in draft order.")
    positional_balance: list[PositionalGrade] = Field(
        default_factory=list, description="Grade per position group as drafted."
    )
    best_picks: list[DraftPickReview] = Field(
        default_factory=list, description="The picks that beat the market."
    )
    worst_picks: list[DraftPickReview] = Field(
        default_factory=list, description="The picks that cost value."
    )
    week_one_plan: list[str] = Field(
        default_factory=list,
        description="Concrete actions before Week 1: waiver targets, roles to watch.",
    )


# --------------------------------------------------------------------------


class PlayerRequest(BaseModel):
    """Body for ``POST /v1/player``. Supply ``name`` or ``player_id``."""

    model_config = ConfigDict(extra="forbid")

    name: PlayerText | None = Field(
        default=None,
        description="Player name; resolved via the player_index. e.g. 'Bijan Robinson'.",
    )
    player_id: Annotated[str, StringConstraints(max_length=32)] | None = Field(
        default=None, description="Sleeper player_id. Preferred when known — skips name resolution."
    )
    week: int | None = Field(
        default=None, description="Week to analyse. Defaults to the current NFL week."
    )


class MatchupRequest(BaseModel):
    """Body for ``POST /v1/matchup``. 2–4 players, names or Sleeper ids."""

    model_config = ConfigDict(extra="forbid")

    players: list[PlayerText] = Field(
        min_length=2,
        max_length=4,
        description="2–4 player names or Sleeper player_ids to compare.",
    )
    week: int | None = Field(
        default=None, description="Week to compare. Defaults to the current NFL week."
    )


class ManualRosterPlayer(BaseModel):
    """One pasted roster slot, for managers not on Sleeper."""

    name: PlayerText = Field(description="Player name.")
    position: Annotated[str, StringConstraints(max_length=8)] | None = Field(
        default=None, description="Position abbreviation, if known."
    )
    starter: bool = Field(default=False, description="Whether they are currently in the lineup.")


class RosterRequest(BaseModel):
    """Body for ``POST /v1/roster``. Supply ``sleeper_username`` **or** ``roster``."""

    model_config = ConfigDict(extra="forbid")

    sleeper_username: SleeperUsername | None = Field(
        default=None, description="Sleeper username; we pull the roster via the free Sleeper API."
    )
    league_id: SleeperId | None = Field(
        default=None,
        description="Sleeper league id. Omitted: first NFL league for the season is used.",
    )
    roster: list[ManualRosterPlayer] | None = Field(
        default=None,
        max_length=MAX_ROSTER_SIZE,
        description="Manually pasted roster, for non-Sleeper managers.",
    )
    week: int | None = Field(
        default=None, description="Week to audit. Defaults to the current NFL week."
    )


class TeamReportRequest(BaseModel):
    """Body for ``POST /v1/team-report``."""

    model_config = ConfigDict(extra="forbid")

    sleeper_username: SleeperUsername = Field(
        description="Sleeper username whose team is analysed."
    )
    league_id: SleeperId | None = Field(
        default=None,
        description="Sleeper league id. Omitted: the first NFL league for the season is used.",
    )
    week: int | None = Field(
        default=None, description="Week through which to analyse. Defaults to the current NFL week."
    )


# --------------------------------------------------------------------------
# GET /v1/catalog  (free — agent discovery surface)
class DraftReportRequest(BaseModel):
    """Body for ``POST /v1/draft-report``.

    Either identifier works: a ``draft_id`` names the draft directly, while a
    ``sleeper_username`` finds that user's most recent draft for the season.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: SleeperId | None = Field(
        default=None, description="Sleeper draft id. Preferred when known."
    )
    sleeper_username: SleeperUsername | None = Field(
        default=None,
        description="Sleeper username; the season's most recent draft is used.",
    )
    draft_slot: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Which seat in the draft to grade, 1-indexed. Only needed when a "
            "'draft_id' is given without a username, since a draft contains every "
            "team's picks and a report grades one roster."
        ),
    )
    season: int | None = Field(
        default=None, description="NFL season. Defaults to the active season."
    )


# --------------------------------------------------------------------------


class CatalogEntry(BaseModel):
    """One endpoint as advertised to agents and the Bazaar."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(description="URL path, e.g. '/v1/player'.")
    method: Literal["GET", "POST"] = Field(description="HTTP method.")
    key: str = Field(
        description=(
            "Stable endpoint key (see api.core.config.ENDPOINT_KEYS). Empty for free endpoints."
        )
    )
    price_usdc: float = Field(description="Price in USDC. 0 for free endpoints.")
    description: str = Field(description="What the payment unlocks, in one sentence.")
    request_schema: str | None = Field(
        default=None, description="Name of the request model in the OpenAPI components, if any."
    )
    response_schema: str = Field(
        description="Name of the response model in the OpenAPI components."
    )
    free: bool = Field(description="Whether the endpoint is callable without payment.")
    cache_ttl_seconds: int | None = Field(
        default=None, description="How long a generated response is reused; None = always fresh."
    )


class Catalog(BaseModel):
    """Machine-readable catalog served at ``GET /v1/catalog`` (PRD §4.1)."""

    model_config = ConfigDict(extra="forbid")

    service: str = Field(description="Product name.")
    version: str = Field(description="API version string.")
    network: str = Field(description="Algorand network payments settle on: 'testnet' or 'mainnet'.")
    pay_to: str = Field(
        description="Algorand address receiving USDC for every endpoint (Composite entry)."
    )
    asset_id: int = Field(
        description="USDC ASA id on the active network. 0 means payments are not configured."
    )
    facilitator_url: str = Field(description="x402 facilitator base URL used for verify/settle.")
    challenge_tag: str = Field(description="x402 challenge tag included in payment metadata.")
    endpoints: list[CatalogEntry] = Field(description="Every endpoint, free and paid.")
    attribution: str = Field(default=ATTRIBUTION, description="Data source attribution.")


class EndpointUsage(BaseModel):
    """How much one endpoint has been bought."""

    key: str = Field(description="Endpoint key, e.g. 'trending'.")
    paid_calls: int = Field(description="Settled payments for this endpoint.")
    usdc: float = Field(description="USDC settled through this endpoint.")


#: What a "hit" means on ``/v1/stats``. Published with the numbers so nobody has
#: to guess how generous the bar is; the ingest backtest implements exactly this.
ACCURACY_METHOD = (
    "Every claim is scored against the week's actual PPR points once nflverse "
    "publishes them. start/add/sleeper/waiver/emerging hit when the player finished "
    "inside the startable range for his position (QB12, RB24, WR24, TE12, K12, DEF12); "
    "sit/fade hit when he did not; a matchup call hits when the player ranked first "
    "scored at least as much as the rest of the group. A player with no stat line "
    "scored zero. First recorded claim only; re-warms never rewrite a call."
)


class AccuracyTotals(BaseModel):
    """Hits over scored claims."""

    scored: int = Field(description="Claims scored against a played week.")
    hits: int = Field(description="Claims that were right.")
    hit_rate: float | None = Field(
        default=None, description="hits / scored, rounded; None until something is scored."
    )


class AccuracyBucket(AccuracyTotals):
    """Hits over scored claims for one endpoint or claim kind."""

    key: str = Field(description="Endpoint key (e.g. 'matchup') or claim kind.")


class AccuracySummary(BaseModel):
    """How often the paid calls were right, from the ingest backtest."""

    season: int = Field(description="Season the claims were made in.")
    weeks_scored: list[int] = Field(
        default_factory=list, description="Weeks with at least one scored claim."
    )
    overall: AccuracyTotals = Field(description="Every scored claim, all endpoints.")
    by_endpoint: list[AccuracyBucket] = Field(
        default_factory=list, description="Per-endpoint breakdown, alphabetical."
    )
    method: str = Field(default=ACCURACY_METHOD, description="What counts as a hit.")


class StatsResponse(BaseModel):
    """Aggregate usage, built from settled receipts. Free, and deliberately coarse.

    Individual payer addresses are public on chain but are not republished here:
    the useful number is how many different people paid, not who they were.
    """

    paid_analyses: int = Field(description="Total settled payments served.")
    unique_payers: int = Field(description="Distinct paying addresses.")
    usdc_settled: float = Field(description="Total USDC settled.")
    network: str = Field(description="Algorand network these settlements are on.")
    since: str | None = Field(default=None, description="Timestamp of the first settlement.")
    by_endpoint: list[EndpointUsage] = Field(
        default_factory=list, description="Per-endpoint breakdown, busiest first."
    )
    accuracy: AccuracySummary | None = Field(
        default=None,
        description=(
            "How often the paid calls were right, scored against real results by the "
            "weekly backtest. None until the first played week has been scored."
        ),
    )


class HealthResponse(BaseModel):
    """Free ``GET /v1/health`` body."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "degraded"] = Field(description="Overall service health.")
    version: str = Field(description="Deployed API version.")
    week: int | None = Field(
        default=None, description="Current NFL week as resolved from the schedule."
    )
    season: int = Field(description="Active NFL season year.")
    configured_season: int | None = Field(
        default=None,
        description="Season the service is configured to sell analysis for.",
    )
    season_mismatch: bool = Field(
        default=False,
        description="Whether the ingested schedule belongs to a different configured season.",
    )
    store_backend: str = Field(description="Active Store implementation.")
    engine: str = Field(description="Active analysis engine.")
    data_freshness: dict[str, str] = Field(
        default_factory=dict, description="Per-dataset as-of markers from meta/freshness."
    )
    stale_datasets: list[str] = Field(
        default_factory=list, description="Datasets whose freshness marker exceeds its SLA."
    )
    unavailable_datasets: list[str] = Field(
        default_factory=list,
        description=(
            "Datasets upstream has not published yet, exempted from the SLA by the "
            "preseason gap. Reported, not counted as stale."
        ),
    )
