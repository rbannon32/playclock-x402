"""Per-endpoint metadata for the 402 challenge and Bazaar discovery.

Every paid endpoint needs three things in its ``402 Payment Required`` body that
the price table alone cannot supply:

1. a human-readable ``resource.description`` (what the payment unlocks),
2. an example request (query params for GET, JSON body for POST) and its schema,
3. an example response,

which together become the **Bazaar discovery extension** — the payload an AI
agent reads to decide whether to pay and how to call us. The facilitator
catalogs it when a payment settles, so it is a marketing surface as much as a
technical one (PRD §2 "agent-native distribution").

Honesty rule: the examples here are trimmed but structurally valid instances of
the real contracts in :mod:`api.schemas`, and the model *names* are taken from
those classes by reference — rename a response model and this module moves with
it instead of drifting. Fields shown are a representative subset; ``extra`` keys
are never invented.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from api.core.config import ENDPOINT_KEYS, Settings
from api.schemas import (
    ATTRIBUTION,
    DraftBoardResponse,
    DraftReportRequest,
    DraftReportResponse,
    MatchupRequest,
    MatchupResponse,
    PlayerRequest,
    PlayerResponse,
    ReportResponse,
    RosterRequest,
    RosterResponse,
    SleepersResponse,
    TeamReportRequest,
    TeamReportResponse,
    TrendingResponse,
    WaiversResponse,
)
from api.x402.schemas_compat import BAZAAR, OutputConfig, declare_discovery_extension

__all__ = [
    "ENDPOINT_SPECS",
    "MERCHANT",
    "EndpointSpec",
    "bazaar_extensions",
    "merchant_extension",
    "payment_extensions",
    "spec_for",
]

HttpMethod = Literal["GET", "POST"]

#: Extension key for merchant identity. GoPlausible reads it off the settled
#: payload; the SDK has no constant for it (``x402-avm`` 2.0.2 defines only
#: ``bazaar``), so the name is pinned here to the one its examples use.
MERCHANT = "x402-merchant"

#: The two discovery variants' method sets, from the SDK's ``QueryParamMethods``
#: and ``BodyMethods`` literals. Declaring a method outside its variant fails
#: validation inside the facilitator, silently (see :func:`bazaar_extensions`).
_QUERY_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "DELETE"})
_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH"})


@dataclass(frozen=True)
class EndpointSpec:
    """Everything the payment layer needs to advertise one paid endpoint.

    Attributes:
        key: Stable endpoint key from :data:`api.core.config.ENDPOINT_KEYS`.
        method: HTTP method the paid route is mounted on.
        path: URL path, for documentation and for the catalog builder.
        description: One sentence: what the payment unlocks.
        response_schema: Name of the response model in :mod:`api.schemas`.
        request_schema: Name of the request model, for POST endpoints.
        input_example: Example query params (GET) or JSON body (POST).
        input_schema: JSON Schema fragment (``properties``/``required``) for the input.
        output_example: Trimmed but valid example of the response body.
    """

    key: str
    method: HttpMethod
    path: str
    description: str
    response_schema: str
    request_schema: str | None
    input_example: dict[str, Any]
    input_schema: dict[str, Any]
    output_example: dict[str, Any]

    @property
    def is_body_method(self) -> bool:
        """Whether the input travels in a JSON body rather than query params."""
        return self.method in ("POST", "PUT", "PATCH")


_META_EXAMPLE: dict[str, Any] = {
    "generated_at": "2026-10-07T13:05:00Z",
    "data_freshness": {
        "weekly_stats": "2026-10-07T09:00:00Z",
        "trending": "2026-10-07T12:30:00Z",
    },
    "model": "gemini-3.7-flash",
    "cache": "fresh",
    "attribution": ATTRIBUTION,
}


def _analysis_example(
    verdict: str,
    reasoning: str,
    *,
    confidence: str = "high",
    stat: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build an example body carrying the :class:`~api.schemas.AnalysisResponse` block."""
    example: dict[str, Any] = {
        "verdict": verdict,
        "confidence": confidence,
        "reasoning": reasoning,
        "stats_cited": [stat] if stat else [],
        "sources": [
            {
                "title": "Beat writer: snap share climbing",
                "url": "https://example.com/report",
                "published": "2026-10-06",
            }
        ],
        "meta": _META_EXAMPLE,
    }
    example.update(extra)
    return example


_WEEK_PARAM: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 18,
    "description": "NFL week. Defaults to the current week when omitted.",
}

_ENDPOINT_SPEC_LIST: tuple[EndpointSpec, ...] = (
    EndpointSpec(
        key="trending",
        method="GET",
        path="/v1/trending",
        description=(
            "Top 25 Sleeper trending adds and drops, each with the usage numbers behind "
            "the move and an add/fade/hold verdict. **Use when** an agent needs to know "
            "who fantasy managers are picking up right now and whether the crowd is "
            "right. Covers the whole league, not one team."
        ),
        response_schema=TrendingResponse.__name__,
        request_schema=None,
        input_example={"lookback_hours": 24},
        input_schema={
            "properties": {
                "lookback_hours": {
                    "type": "integer",
                    "description": "Sleeper trending lookback window. Defaults to 24.",
                }
            },
            "required": [],
        },
        output_example=_analysis_example(
            "Nine of the top ten adds are opportunity-driven, not talent-driven.",
            "Three lead backs left week 5 with injuries, which explains the top of the board.",
            stat={
                "stat": "snap_pct",
                "value": 0.71,
                "player": "Tyjae Spears",
                "source": "nflverse weekly_stats 2026w5",
            },
            players=[
                {
                    "player_id": "8146",
                    "name": "Tyjae Spears",
                    "position": "RB",
                    "team": "TEN",
                    "trend": "add",
                    "trend_count": 41233,
                    "analysis": "Inherits the backfield with the starter on IR; 71% of snaps.",
                    "verdict": "add",
                }
            ],
            lookback_hours=24,
        ),
    ),
    EndpointSpec(
        key="sleepers",
        method="GET",
        path="/v1/sleepers",
        description=(
            "Eight to twelve low-rostered players to start this week, with snap share, "
            "target share, red-zone work and the matchup behind each. **Use when** an "
            "agent needs lineup options the market has not priced in yet. Weekly and "
            "league-wide; for one named player use /v1/player."
        ),
        response_schema=SleepersResponse.__name__,
        request_schema=None,
        input_example={"week": 5},
        input_schema={"properties": {"week": _WEEK_PARAM}, "required": []},
        output_example=_analysis_example(
            "Four low-rostered pass catchers have both usage and matchup on their side.",
            "Each pick clears a 15% target share and faces a bottom-eight defense by position.",
            stat={
                "stat": "target_share",
                "value": 0.23,
                "player": "Jalen McMillan",
                "source": "nflverse weekly_stats 2026w4",
            },
            week=5,
            season=2026,
            picks=[
                {
                    "player_id": "11596",
                    "name": "Jalen McMillan",
                    "position": "WR",
                    "team": "TB",
                    "opponent": "NO",
                    "confidence": "medium",
                    "usage_note": "23% target share over the last two weeks, up from 12%.",
                    "matchup_note": "New Orleans allows the 4th-most fantasy points to slot WRs.",
                    "rationale": "Volume plus a soft interior coverage draw makes him a flex play.",
                }
            ],
        ),
    ),
    EndpointSpec(
        key="player",
        method="POST",
        path="/v1/player",
        description=(
            "One player in depth: four weeks of usage, schedule difficulty, current "
            "injury and beat-writer news, and a start/sit verdict. **Use when** an agent "
            "is asked about a specific player by name. Cheapest paid endpoint; prefer it "
            "over /v1/matchup when only one player is in question."
        ),
        response_schema=PlayerResponse.__name__,
        request_schema=PlayerRequest.__name__,
        input_example={"name": "Bijan Robinson", "week": 5},
        input_schema={
            "properties": {
                "name": {"type": "string", "description": "Player name; resolved to a Sleeper id."},
                "player_id": {"type": "string", "description": "Sleeper player_id, when known."},
                "week": _WEEK_PARAM,
            },
            "required": [],
        },
        output_example=_analysis_example(
            "Start him with confidence; the usage trend is the highest of his career.",
            "Snap share and red-zone touches have both risen in each of the last three weeks.",
            player={
                "player_id": "8155",
                "name": "Bijan Robinson",
                "position": "RB",
                "team": "ATL",
                "status": None,
                "recent_weeks": [
                    {
                        "week": 4,
                        "opponent": "TB",
                        "fantasy_points": 21.4,
                        "snap_pct": 0.82,
                        "targets": 5,
                        "target_share": 0.16,
                        "carries": 18,
                        "rz_touches": 4,
                    }
                ],
                "usage_trajectory": "rising: +11pp snap share over four weeks",
                "schedule_outlook": "Two bottom-five run defenses in the next three weeks.",
            },
            week=5,
        ),
    ),
    EndpointSpec(
        key="matchup",
        method="POST",
        path="/v1/matchup",
        description=(
            "Rank two to four players against each other for one week, with the "
            "opponent defense, weather and injury news that decide it. **Use when** an "
            "agent must choose between named players for a lineup slot. For a single "
            "player use /v1/player; for a whole lineup use /v1/roster."
        ),
        response_schema=MatchupResponse.__name__,
        request_schema=MatchupRequest.__name__,
        input_example={"players": ["Bijan Robinson", "De'Von Achane"], "week": 5},
        input_schema={
            "properties": {
                "players": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 4,
                    "description": "Two to four player names or Sleeper player_ids.",
                },
                "week": _WEEK_PARAM,
            },
            "required": ["players"],
        },
        output_example=_analysis_example(
            "Start Robinson over Achane; the matchup gap outweighs the talent gap.",
            "Atlanta draws the 30th-ranked run defense while Miami faces a top-five front.",
            week=5,
            ranked=[
                {
                    "rank": 1,
                    "player_id": "8155",
                    "name": "Bijan Robinson",
                    "position": "RB",
                    "team": "ATL",
                    "opponent": "CAR",
                    "projection_note": "Volume floor of 20 touches against a bottom-tier front.",
                    "def_vs_pos_rank": 2,
                    "call": "start",
                }
            ],
        ),
    ),
    EndpointSpec(
        key="roster",
        method="POST",
        path="/v1/roster",
        description=(
            "Audit a whole fantasy roster: positional grades, this week's start/sit "
            "calls, drop candidates, and the best adds actually available in that "
            "league. **Use when** an agent has a Sleeper username or a list of rostered "
            "players and is asked what to do with the team."
        ),
        response_schema=RosterResponse.__name__,
        request_schema=RosterRequest.__name__,
        input_example={"sleeper_username": "example_manager", "week": 5},
        input_schema={
            "properties": {
                "sleeper_username": {
                    "type": "string",
                    "description": "Sleeper username; roster pulled via the free Sleeper API.",
                },
                "league_id": {"type": "string", "description": "Sleeper league id, when known."},
                "roster": {
                    "type": "array",
                    "description": "Manually pasted roster, for managers not on Sleeper.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "position": {"type": "string"},
                            "starter": {"type": "boolean"},
                        },
                        "required": ["name"],
                    },
                },
                "week": _WEEK_PARAM,
            },
            "required": [],
        },
        output_example=_analysis_example(
            "Strong at receiver, thin at running back; one waiver claim fixes the week.",
            "Two of three starting backs are in committees, which caps the weekly floor.",
            confidence="medium",
            week=5,
            sleeper_username="example_manager",
            league_id="1049283746152738291",
            positional_grades=[
                {"position": "RB", "grade": "C+", "note": "No back clears 60% of snaps."}
            ],
            start_sit=[
                {
                    "player_id": "8155",
                    "name": "Bijan Robinson",
                    "position": "RB",
                    "call": "start",
                    "reason": "Bell-cow usage against a bottom-five run defense.",
                }
            ],
            drop_candidates=[
                {
                    "player_id": "4034",
                    "name": "Example Bench Back",
                    "position": "RB",
                    "reason": "Snap share under 20% for four straight weeks.",
                    "risk": "low",
                }
            ],
            waiver_adds=[
                {
                    "player_id": "8146",
                    "name": "Tyjae Spears",
                    "position": "RB",
                    "team": "TEN",
                    "reason": "Free in your league and inherits a full workload.",
                    "priority": 1,
                }
            ],
        ),
    ),
    EndpointSpec(
        key="waivers",
        method="GET",
        path="/v1/waivers",
        description=(
            "Ranked waiver-wire targets with a suggested FAB bid and a stash-or-start "
            "label on each. **Use when** an agent is asked who to claim this week and "
            "how much to spend. League-wide; for adds available in one specific league "
            "use /v1/roster."
        ),
        response_schema=WaiversResponse.__name__,
        request_schema=None,
        input_example={"week": 5},
        input_schema={"properties": {"week": _WEEK_PARAM}, "required": []},
        output_example=_analysis_example(
            "One must-claim back, then a steep drop to streamers.",
            "Only the top target has both a vacated workload and a favourable schedule.",
            week=5,
            season=2026,
            board=[
                {
                    "rank": 1,
                    "player_id": "8146",
                    "name": "Tyjae Spears",
                    "position": "RB",
                    "team": "TEN",
                    "trend_count": 41233,
                    "stash_or_start": "start",
                    "fab_bid_pct": 28.0,
                    "rationale": "Inherits a 20-touch role with the starter on IR.",
                }
            ],
        ),
    ),
    EndpointSpec(
        key="report",
        method="GET",
        path="/v1/report",
        description=(
            "The whole week in one briefing: players emerging before consensus, injury "
            "fallout and handcuffs, stock up and down, rookies and streamers. **Use "
            "when** an agent needs broad weekly context rather than an answer about a "
            "particular player or team. The widest and most expensive league-wide read."
        ),
        response_schema=ReportResponse.__name__,
        request_schema=None,
        input_example={"week": 5},
        input_schema={"properties": {"week": _WEEK_PARAM}, "required": []},
        output_example=_analysis_example(
            "Week 5 was an injury week; the waiver wire matters more than the matchups.",
            "Three backfields changed hands, and each created a startable replacement.",
            week=5,
            season=2026,
            emerging=[
                {
                    "player_id": "11596",
                    "name": "Jalen McMillan",
                    "position": "WR",
                    "team": "TB",
                    "note": "Target share up 11pp over two weeks while rostership lags.",
                }
            ],
            injury_fallout=[
                {
                    "injured_player": "Example Starter",
                    "team": "TEN",
                    "status": "Out 4-6 weeks",
                    "beneficiaries": [
                        {
                            "player_id": "8146",
                            "name": "Tyjae Spears",
                            "position": "RB",
                            "team": "TEN",
                            "note": "Direct handcuff; 71% snaps once the starter left.",
                        }
                    ],
                }
            ],
            stock_up=[],
            stock_down=[],
            rookie_watch=[],
            streamers=[],
        ),
    ),
    EndpointSpec(
        key="team_report",
        method="POST",
        path="/v1/team-report",
        description=(
            "A roster graded against the other teams in its actual league, with fixes "
            "drawn only from that league's free agents and a manager review (optimal "
            "versus started lineup, luck, lineup efficiency). **Use when** an agent has "
            "a Sleeper league and is asked how a team really compares. Needs live league "
            "history; /v1/roster is the cheaper answer when it does not."
        ),
        response_schema=TeamReportResponse.__name__,
        request_schema=TeamReportRequest.__name__,
        input_example={
            "sleeper_username": "example_manager",
            "league_id": "1049283746152738291",
            "week": 5,
        },
        input_schema={
            "properties": {
                "sleeper_username": {
                    "type": "string",
                    "description": "Sleeper username whose team is analysed.",
                },
                "league_id": {
                    "type": "string",
                    "description": "Sleeper league id. Omitted: first NFL league of the season.",
                },
                "week": _WEEK_PARAM,
            },
            "required": ["sleeper_username"],
        },
        output_example=_analysis_example(
            "A top-three roster managed to a bottom-three lineup efficiency.",
            "You have left 84 points on the bench, the second-worst mark in the league.",
            confidence="medium",
            week=5,
            season=2026,
            sleeper_username="example_manager",
            league_id="1049283746152738291",
            league_name="The Example League",
            positional_strength_vs_league=[
                {
                    "position": "WR",
                    "league_rank": 2,
                    "league_size": 12,
                    "points_per_week": 38.4,
                    "league_avg_points_per_week": 31.1,
                    "grade": "A-",
                }
            ],
            deficiencies=[
                {
                    "position": "RB",
                    "severity": "high",
                    "detail": "9th of 12 in RB points per week, 6.2 below league average.",
                    "available_fixes": [
                        {
                            "player_id": "8146",
                            "name": "Tyjae Spears",
                            "position": "RB",
                            "team": "TEN",
                            "why": "Unrostered in your league and inherits a starter's workload.",
                        }
                    ],
                }
            ],
            manager_review={
                "bench_points_lost": 84.2,
                "optimal_vs_actual": 84.2,
                "lineup_efficiency_pct": 88.1,
                "efficiency_rank": 10,
                "league_size": 12,
                "luck_note": "2nd in points for, 5th in record: you have drawn the top scorers.",
                "expected_wins": 3.6,
                "actual_wins": 2,
                "mis_start_patterns": ["Started the lower-projected TE in 3 of 5 weeks (-19.4)."],
                "observations": ["Sets lineups early in the week, before Friday injury news."],
            },
        ),
    ),
    EndpointSpec(
        key="draft_board",
        method="GET",
        path="/v1/draft-board",
        description=(
            "A tiered board of 200 players ranked against where the market drafts them, "
            "with prior-season usage behind every ranking and the values and reaches "
            "called out. **Use when** an agent is preparing for or sitting in a fantasy "
            "draft. Season-scoped, not weekly. Ranks against Sleeper draft popularity, "
            "which is a market signal and not a consensus ADP."
        ),
        response_schema=DraftBoardResponse.__name__,
        request_schema=None,
        input_example={"limit": 200},
        input_schema={
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 25,
                    "maximum": 200,
                    "description": "How many players to rank. Defaults to 200.",
                },
                "scoring": {
                    "type": "string",
                    "description": "Scoring format the board assumes. Defaults to 'ppr'.",
                },
            },
            "required": [],
        },
        output_example=_analysis_example(
            "Bijan Robinson heads the board; eight players are ranked well above the market.",
            "The board starts from draft-popularity order and shifts on prior-season usage.",
            stat={
                "stat": "market_rank",
                "value": 14,
                "player": "Jaylen Waddle",
                "source": "sleeper players/search_rank",
            },
            season=2026,
            scoring="ppr",
            tiers=[
                {
                    "tier": 1,
                    "label": "Picks 1-6 · RB/WR",
                    "players": [
                        {
                            "player_id": "8155",
                            "name": "Bijan Robinson",
                            "position": "RB",
                            "team": "ATL",
                            "rank": 1,
                            "market_rank": 2,
                            "value_delta": 1,
                            "note": "78% snap share, 6 red-zone touches (94th percentile at RB).",
                        }
                    ],
                }
            ],
            values=[],
            reaches=[],
        ),
    ),
    EndpointSpec(
        key="draft_report",
        method="POST",
        path="/v1/draft-report",
        description=(
            "Grade one manager's completed draft: every pick scored against the market "
            "rank it was taken at, positional balance, best and worst picks, and what to "
            "do before Week 1. **Use when** an agent is given a Sleeper draft id or "
            "username after a draft. Grades one roster, so it needs to know whose."
        ),
        response_schema=DraftReportResponse.__name__,
        request_schema=DraftReportRequest.__name__,
        input_example={"sleeper_username": "ryan"},
        input_schema={
            "properties": {
                "draft_id": {"type": "string", "description": "Sleeper draft id."},
                "sleeper_username": {
                    "type": "string",
                    "description": "Sleeper username; the season's most recent draft is used.",
                },
                "draft_slot": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "Seat to grade, 1-indexed. Needed when a draft_id is given "
                        "without a username, since a draft holds every team's picks."
                    ),
                },
                "season": {"type": "integer", "description": "NFL season."},
            },
            "required": [],
        },
        output_example=_analysis_example(
            "B+ draft: three clear values, one reach, no position left short.",
            "Picks averaged +11 ranks of value against where the market drafts them.",
            confidence="medium",
            stat={
                "stat": "market_rank",
                "value": 41,
                "player": "Jaylen Waddle",
                "source": "sleeper players/search_rank",
            },
            draft_id="1234567890",
            season=2026,
            grade="B+",
            roster=[
                {
                    "player_id": "6786",
                    "name": "Jaylen Waddle",
                    "position": "WR",
                    "team": "MIA",
                    "round": 5,
                    "pick_no": 53,
                    "market_rank": 41,
                    "value_delta": -12,
                }
            ],
            positional_balance=[
                {"position": "RB", "grade": "A-", "note": "5 rostered — a workable RB room."}
            ],
            best_picks=[],
            worst_picks=[],
            week_one_plan=["No structural holes. Watch the depth charts."],
        ),
    ),
)

#: Every paid endpoint, keyed by :data:`api.core.config.ENDPOINT_KEYS` entry.
ENDPOINT_SPECS: dict[str, EndpointSpec] = {spec.key: spec for spec in _ENDPOINT_SPEC_LIST}

# The price table, the catalog and the payment layer must agree on the endpoint
# set; a mismatch here would mean an endpoint that can be priced but not sold.
# Checked at import (not with `assert`, which `python -O` would strip).
if set(ENDPOINT_SPECS) != set(ENDPOINT_KEYS):  # pragma: no cover - guard
    raise RuntimeError(
        "ENDPOINT_SPECS drifted from api.core.config.ENDPOINT_KEYS: "
        f"{sorted(set(ENDPOINT_SPECS) ^ set(ENDPOINT_KEYS))}"
    )


def spec_for(endpoint_key: str) -> EndpointSpec:
    """Return the :class:`EndpointSpec` for ``endpoint_key``.

    Raises:
        KeyError: If ``endpoint_key`` is not a known paid endpoint.
    """
    try:
        return ENDPOINT_SPECS[endpoint_key]
    except KeyError:
        raise KeyError(f"unknown endpoint key: {endpoint_key!r}") from None


def bazaar_extensions(spec: EndpointSpec) -> dict[str, Any]:
    """Build the ``extensions`` block of a 402 body: Bazaar discovery for ``spec``.

    Returns a ``{"bazaar": {...}}`` dict from the SDK's
    :func:`~x402.extensions.bazaar.declare_discovery_extension`, which chooses
    the query-param or JSON-body variant based on whether a body type is given,
    with ``input.method`` added.

    **The method has to ride here, and the SDK will not add it for us.** Its
    docstring says the method "is automatically inferred from the route key or
    enriched by ``bazaar_resource_server_extension`` at runtime" — that is the
    SDK's own decorator-based server, which we do not use (payment is a route
    dependency, DESIGN_NOTES §2). So it never gets enriched and the field is
    simply absent.

    That is fatal to discovery, because a catalogue id is
    ``base64("METHOD:URL")`` — method is half the primary key. Sampled live on
    2026-09-08, all 45 GET resources in a 60-record page of
    ``/discovery/resources`` carry ``discoveryInfo.input.method``; Play Clock
    carried none and was catalogued as none, with ``bazaar: false`` on the
    challenge leaderboard despite 12 settled MainNet payments.

    ``resource.method`` in the 402 body (see
    :func:`~api.x402.schemas_compat.payment_required_body`) does not cover this.
    It reaches the *client*, but the payload that comes back is re-validated
    through the SDK's three-field ``ResourceInfo``, which drops it before we
    ever forward it to the facilitator. ``extensions`` is a plain
    ``dict[str, Any]`` the whole way, so what we put here is what settles.
    """
    extensions = declare_discovery_extension(
        input=spec.input_example,
        input_schema=spec.input_schema,
        body_type="json" if spec.is_body_method else None,
        output=OutputConfig(example=spec.output_example),
    )
    bazaar = extensions.get(BAZAAR)
    if not isinstance(bazaar, dict):  # pragma: no cover - SDK contract
        raise RuntimeError(f"declare_discovery_extension returned no {BAZAAR!r} block")

    # The variant is chosen by ``body_type``, and each variant's ``method`` is a
    # pydantic ``Literal``. A method that contradicts it raises inside the
    # facilitator's ``parse_discovery_extension`` — which ``extract_discovery_info``
    # swallows in a bare ``except``, logs, and returns ``None`` from. That is a
    # silent delisting, so it is a startup error here instead.
    allowed = _BODY_METHODS if spec.is_body_method else _QUERY_METHODS
    if spec.method not in allowed:
        raise RuntimeError(
            f"{spec.key}: method {spec.method!r} contradicts the "
            f"{'body' if spec.is_body_method else 'query'} discovery variant "
            f"(allowed: {sorted(allowed)})"
        )

    info = bazaar.setdefault("info", {})
    payload_input = info.setdefault("input", {})
    payload_input["method"] = spec.method

    # Schema parity with ``bazaar_resource_server_extension.enrich_declaration``,
    # the SDK-server path we opt out of. It narrows the declared ``method`` enum
    # to the one this route serves and makes it *required*, which is also what
    # every official GoPlausible example ships (``"required": ["type", "method"]``).
    #
    # This matters more than it looks. The facilitator validates ``info`` against
    # this very schema and, with ``method`` merely optional, an absent method
    # still validates — it is then read as the literal string ``"UNKNOWN"`` and
    # catalogued under ``base64("UNKNOWN:<url>")``. Requiring it converts that
    # silent mis-listing into a validation error we can see and test for.
    schema_input = (
        bazaar.setdefault("schema", {}).setdefault("properties", {}).setdefault("input", {})
    )
    schema_input.setdefault("properties", {})["method"] = {
        "type": "string",
        "enum": [spec.method],
    }
    required = schema_input.setdefault("required", [])
    if "method" not in required:
        required.append("method")
    return extensions


def merchant_extension(settings: Settings) -> dict[str, Any]:
    """Build the ``x402-merchant`` block: who the Bazaar says is selling this.

    The SDK has no constant or helper for this extension — ``x402-avm`` 2.0.2
    knows only ``bazaar`` — so the dict is written out here, matching the shape
    GoPlausible's own FastAPI, Flask and Express challenge examples ship.

    It is optional, and skipping it is how Play Clock ended up relying on the
    facilitator *guessing*: with no merchant block the leaderboard scrapes the
    resource origin's root for a ``<title>`` and ``/apple-touch-icon.png``, and
    hosts that serve neither list as a truncated ``payTo`` address
    (DESIGN_NOTES §25). ``categories`` cannot be guessed at all, and it is what
    an agent browsing the catalogue filters on.

    Note this is merchant identity, not per-resource identity. The spec's
    per-resource ``serviceName``/``tags``/``iconUrl`` live on ``resource``, which
    the SDK's three-field ``ResourceInfo`` drops (DESIGN_NOTES §21) — so all ten
    endpoints necessarily carry the same merchant block.
    """
    info: dict[str, Any] = {
        "name": settings.x402_merchant_name,
        "website": settings.x402_merchant_website,
        "categories": [
            part.strip() for part in settings.x402_merchant_categories.split(",") if part.strip()
        ],
    }
    if settings.x402_merchant_logo:
        info["logo"] = settings.x402_merchant_logo
    return {
        MERCHANT: {
            "info": info,
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "website": {"type": "string"},
                    "logo": {"type": "string"},
                    "categories": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    }


def payment_extensions(spec: EndpointSpec, settings: Settings) -> dict[str, Any]:
    """The whole ``extensions`` block of a 402: Bazaar discovery + merchant identity.

    Both ride in-band and are echoed back by the client unmodified; the
    facilitator reads them off the *payload*, never off our 402 (CLAUDE.md,
    "the envelope must echo ``accepted`` **and** ``extensions``").
    """
    return {**bazaar_extensions(spec), **merchant_extension(settings)}
