"""The 20 golden queries (tech spec §5) and the fixture season they run against.

What this is
------------
An eval suite that answers one question before every weekly deploy: *would a
payer get a correct, honest answer?* It does that with **property assertions**,
not golden strings — an LLM will never reproduce a fixed sentence, but it must
always resolve the right player, satisfy the response contract, and cite only
numbers that exist.

The three properties tech spec §5 names are all here:

``valid schema``
    Every response is an instance of ``RESPONSE_MODELS[endpoint_key]``, so the
    body an agent parses is the body the OpenAPI spec promised.
``correct player resolved``
    Name-resolution cases assert the exact Sleeper id that came back, including
    the ambiguous "Josh Allen" collision (QB, not linebacker).
``no uncited stats``
    :func:`check_citations_traceable` walks every ``stats_cited`` entry back to
    the seeded fixture and fails on any number that is not there. This is the
    hallucination gate, and it is the reason the whole suite runs against a
    hermetic :class:`~api.core.store.MemoryStore` rather than live data: a
    number can only be "real" if we put it there.

The fixture is a small but complete week 4 of a 2026 season — 19 players across
14 teams, three weeks of stat lines, usage rollups, defensive splits, a trending
board and a schedule — chosen so that every endpoint has enough data to produce
its full product (12 sleeper candidates, an injury chain with a real
beneficiary, streamers with plus matchups) and so that failure modes are
represented too (an unknown name, an ambiguous name, a player with no usage
rollup).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from api.agents.engine import RESPONSE_MODELS
from api.core.store import MemoryStore, Store
from api.data.stats_store import (
    DEF_VS_POS_COLLECTION,
    META_COLLECTION,
    PLAYER_INDEX_COLLECTION,
    PLAYERS_COLLECTION,
    SCHEDULES_COLLECTION,
    TRENDING_COLLECTION,
    USAGE_TRENDS_COLLECTION,
    normalize_name,
    weekly_stats_collection,
)
from api.evals.quality import check_quality
from api.schemas import AnalysisResponse, StatCitation

#: The fixture season/week every golden query is scoped to.
SEASON = 2026
WEEK = 4

#: Provenance prefixes whose citations must trace back to the seeded store.
STORE_SOURCE_PREFIXES = ("nflverse ", "sleeper ")

#: Provenance for numbers the *route* computed and passed in on
#: ``request_context`` (tech spec §6: team analytics are Python, not LLM).
#: These trace back to the request rather than the store.
CONTEXT_SOURCE = "sleeper league matchups (team_analytics)"

# --------------------------------------------------------------------------
# Fixture data
# --------------------------------------------------------------------------

#: ``player_id -> (name, position, team, years_exp, injury_status)``.
PLAYERS: dict[str, tuple[str, str, str, int, str | None]] = {
    "1001": ("Bijan Robinson", "RB", "ATL", 3, None),
    "1002": ("Breece Hall", "RB", "NYJ", 4, None),
    "1003": ("Ja'Marr Chase", "WR", "CIN", 6, None),
    "1004": ("Marvin Harrison Jr.", "WR", "ARI", 2, None),
    "1005": ("Trey McBride", "TE", "ARI", 4, None),
    "1006": ("Josh Allen", "QB", "BUF", 8, None),
    "1007": ("Josh Allen", "LB", "JAX", 7, None),
    "1008": ("Rome Odunze", "WR", "CHI", 2, None),
    "1009": ("Tyjae Spears", "RB", "TEN", 3, None),
    "1010": ("Jaxon Smith-Njigba", "WR", "SEA", 3, None),
    "1011": ("Tucker Kraft", "TE", "GB", 3, None),
    "1012": ("Bhayshul Tuten", "RB", "JAX", 0, None),
    "1013": ("Cam Ward", "QB", "TEN", 0, None),
    "1014": ("Chase Brown", "RB", "CIN", 3, None),
    "1015": ("Jordan Addison", "WR", "MIN", 3, None),
    "1016": ("Nico Collins", "WR", "HOU", 5, "Out"),
    "1017": ("Jayden Higgins", "WR", "HOU", 1, None),
    "1018": ("Brock Bowers", "TE", "LV", 2, None),
    "1019": ("Cameron Dicker", "K", "LAC", 4, None),
}

#: Fixture market ranks (Sleeper ``search_rank``: lower = drafted earlier).
#: Deliberately *not* the same order as fixture usage, so the draft board has
#: real values and reaches to find rather than reprinting the market.
MARKET_RANKS: dict[str, int] = {
    "1003": 1,  # Ja'Marr Chase — market and usage agree
    "1001": 2,  # Bijan Robinson
    "1004": 3,  # Marvin Harrison Jr. — market high, usage soft: a reach
    "1002": 6,
    "1016": 8,  # Nico Collins — out, market has not caught up
    "1010": 11,
    "1018": 14,
    "1006": 18,
    "1015": 22,
    "1005": 26,
    "1014": 31,
    "1008": 38,
    "1011": 44,
    "1009": 52,  # Tyjae Spears — usage rising, market late: a value
    "1017": 61,
    "1013": 74,
    "1012": 88,
    "1019": 140,
}

#: A fixture draft, as Sleeper returns picks. Every pick carries ``picked_by``
#: and ``draft_slot`` because the route filters on them — a real draft returns
#: every team's picks in one flat list, and grading the lot as a single roster
#: is the bug this shape exists to keep caught.
#:
#: These are one manager's picks in a ten-team draft. Pick numbers are set
#: against :data:`MARKET_RANKS` so the set holds two unmistakable reaches —
#: Tyjae Spears (market 52) at pick 24 and a kicker (market 140) at pick 97 —
#: alongside several clear values.
DRAFT_PICKS: tuple[dict[str, Any], ...] = (
    {
        "player_id": "1003",
        "round": 1,
        "pick_no": 4,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1001",
        "round": 2,
        "pick_no": 17,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1009",
        "round": 3,
        "pick_no": 24,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1010",
        "round": 4,
        "pick_no": 37,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1015",
        "round": 5,
        "pick_no": 44,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1005",
        "round": 6,
        "pick_no": 57,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1006",
        "round": 7,
        "pick_no": 64,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1014",
        "round": 8,
        "pick_no": 77,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1008",
        "round": 9,
        "pick_no": 84,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
    {
        "player_id": "1019",
        "round": 10,
        "pick_no": 97,
        "roster_id": 1,
        "picked_by": "u1",
        "draft_slot": 4,
    },
)


#: Week-4 games, ``(home, away)``. Covers every team in :data:`PLAYERS`.
WEEK4_GAMES: tuple[tuple[str, str], ...] = (
    ("CIN", "ATL"),
    ("NYJ", "BUF"),
    ("ARI", "SEA"),
    ("JAX", "TEN"),
    ("CHI", "GB"),
    ("MIN", "HOU"),
    ("LV", "LAC"),
)

#: ``team -> {position: rank}``. Rank 1 = most fantasy points allowed = the best
#: matchup to attack. Points allowed per game are derived from the rank.
DEF_RANKS: dict[str, dict[str, int]] = {
    "ATL": {"QB": 5, "RB": 3, "WR": 8, "TE": 4, "K": 6, "DEF": 10},
    "CIN": {"QB": 12, "RB": 2, "WR": 15, "TE": 20, "K": 14, "DEF": 12},
    "BUF": {"QB": 25, "RB": 28, "WR": 22, "TE": 26, "K": 20, "DEF": 18},
    "NYJ": {"QB": 18, "RB": 24, "WR": 19, "TE": 9, "K": 15, "DEF": 11},
    "SEA": {"QB": 9, "RB": 14, "WR": 6, "TE": 7, "K": 8, "DEF": 13},
    "ARI": {"QB": 11, "RB": 10, "WR": 12, "TE": 16, "K": 10, "DEF": 14},
    "TEN": {"QB": 4, "RB": 5, "WR": 9, "TE": 3, "K": 5, "DEF": 8},
    "JAX": {"QB": 20, "RB": 21, "WR": 17, "TE": 22, "K": 19, "DEF": 21},
    "GB": {"QB": 16, "RB": 17, "WR": 13, "TE": 12, "K": 13, "DEF": 15},
    "CHI": {"QB": 7, "RB": 8, "WR": 5, "TE": 6, "K": 7, "DEF": 9},
    "HOU": {"QB": 27, "RB": 26, "WR": 29, "TE": 24, "K": 22, "DEF": 25},
    "MIN": {"QB": 6, "RB": 7, "WR": 4, "TE": 5, "K": 4, "DEF": 7},
    "LAC": {"QB": 15, "RB": 13, "WR": 16, "TE": 14, "K": 11, "DEF": 16},
    "LV": {"QB": 3, "RB": 6, "WR": 7, "TE": 2, "K": 3, "DEF": 5},
}

#: ``player_id -> (snap_l4w, target_share_l4w, rz_l4w, snap_delta, ts_delta, trend)``.
#: Player 1007 (the linebacker) deliberately has none, and 1018 has none either
#: so at least one fantasy-relevant player exercises the "no rollup" path.
USAGE: dict[str, tuple[float, float, int, float, float, str]] = {
    "1001": (0.82, 0.14, 11, 0.06, 0.03, "rising"),
    "1002": (0.61, 0.11, 5, -0.09, -0.04, "declining"),
    "1003": (0.94, 0.31, 8, 0.02, 0.05, "rising"),
    "1004": (0.88, 0.26, 7, 0.05, 0.06, "rising"),
    "1005": (0.79, 0.22, 6, 0.04, 0.04, "rising"),
    "1006": (0.99, 0.00, 9, 0.01, 0.00, "flat"),
    "1008": (0.71, 0.19, 4, 0.11, 0.07, "rising"),
    "1009": (0.44, 0.09, 3, 0.13, 0.03, "rising"),
    "1010": (0.91, 0.28, 5, 0.03, 0.04, "rising"),
    "1011": (0.68, 0.17, 6, 0.09, 0.05, "rising"),
    "1012": (0.38, 0.06, 4, 0.18, 0.02, "rising"),
    "1013": (0.97, 0.00, 7, 0.02, 0.01, "rising"),
    "1014": (0.52, 0.08, 3, -0.12, -0.03, "declining"),
    "1015": (0.77, 0.21, 4, 0.03, 0.02, "rising"),
    "1016": (0.31, 0.09, 1, -0.41, -0.16, "declining"),
    "1017": (0.66, 0.18, 5, 0.22, 0.11, "rising"),
    "1019": (1.00, 0.00, 0, 0.00, 0.01, "rising"),
}

#: ``player_id -> [(fantasy_points_ppr, snap_pct, targets, target_share, carries, rz)]``
#: for weeks 1-3. Week 4 has no lines yet — it is the upcoming week.
WEEKLY: dict[str, list[tuple[float, float, int, float, int, int]]] = {
    "1001": [
        (18.4, 0.78, 4, 0.11, 17, 3),
        (22.1, 0.83, 6, 0.15, 19, 4),
        (25.6, 0.85, 5, 0.16, 21, 4),
    ],
    "1002": [(14.2, 0.70, 5, 0.14, 13, 2), (9.8, 0.63, 3, 0.10, 10, 1), (7.4, 0.55, 2, 0.08, 9, 2)],
    "1003": [
        (28.9, 0.93, 12, 0.30, 0, 3),
        (21.4, 0.94, 10, 0.29, 1, 2),
        (31.2, 0.95, 13, 0.34, 0, 3),
    ],
    "1004": [
        (16.7, 0.86, 8, 0.24, 0, 2),
        (19.3, 0.88, 9, 0.26, 0, 3),
        (20.8, 0.90, 10, 0.28, 0, 2),
    ],
    "1005": [(13.1, 0.76, 7, 0.20, 0, 2), (15.9, 0.80, 8, 0.23, 0, 2), (17.2, 0.81, 8, 0.23, 0, 2)],
    "1006": [(24.6, 0.99, 0, 0.0, 6, 3), (19.1, 0.98, 0, 0.0, 5, 3), (27.3, 1.00, 0, 0.0, 7, 3)],
    "1008": [(9.6, 0.66, 5, 0.16, 0, 1), (12.8, 0.72, 7, 0.20, 0, 1), (15.4, 0.75, 8, 0.21, 0, 2)],
    "1009": [(6.2, 0.38, 2, 0.07, 6, 1), (8.9, 0.44, 3, 0.09, 8, 1), (11.5, 0.50, 4, 0.11, 10, 1)],
    "1010": [
        (20.1, 0.90, 9, 0.27, 0, 1),
        (18.6, 0.91, 9, 0.28, 0, 2),
        (22.4, 0.92, 11, 0.30, 0, 2),
    ],
    "1011": [(8.4, 0.62, 4, 0.14, 0, 2), (11.7, 0.69, 6, 0.18, 0, 2), (13.9, 0.72, 6, 0.19, 0, 2)],
    "1012": [(4.1, 0.29, 1, 0.04, 5, 1), (7.8, 0.38, 2, 0.06, 9, 1), (10.3, 0.46, 2, 0.07, 12, 2)],
    "1013": [(17.9, 0.97, 0, 0.0, 4, 2), (14.2, 0.96, 0, 0.0, 3, 1), (21.5, 0.98, 0, 0.0, 5, 3)],
    "1014": [(11.3, 0.58, 3, 0.09, 11, 2), (7.6, 0.52, 2, 0.07, 8, 1), (5.9, 0.46, 2, 0.06, 7, 1)],
    "1015": [(12.4, 0.74, 6, 0.19, 0, 1), (14.1, 0.77, 7, 0.21, 0, 2), (13.6, 0.79, 7, 0.22, 0, 1)],
    "1016": [(23.7, 0.89, 11, 0.29, 0, 3), (5.2, 0.24, 2, 0.06, 0, 0)],
    "1017": [(6.8, 0.51, 4, 0.12, 0, 1), (13.4, 0.68, 7, 0.19, 0, 2), (16.9, 0.74, 8, 0.22, 0, 2)],
    "1018": [(15.2, 0.84, 8, 0.23, 0, 2), (12.7, 0.82, 7, 0.21, 0, 1), (18.3, 0.86, 9, 0.25, 0, 3)],
    "1019": [(9.0, 1.00, 0, 0.0, 0, 0), (11.0, 1.00, 0, 0.0, 0, 0), (7.0, 1.00, 0, 0.0, 0, 0)],
}

#: ``(player_id, add_count)``, Sleeper's own ordering. Counts at or above
#: ``CONSENSUS_ADD_COUNT`` (3000) mark a player as already-claimed, which
#: disqualifies them from the sleepers board but not from the waiver board.
TRENDING_ADD: tuple[tuple[str, int], ...] = (
    ("1012", 45210),
    ("1017", 28140),
    ("1009", 12065),
    # Chase Brown: a big add whose usage is *declining*. The crowd is chasing
    # a name, and the board must say so — a trending board that agrees with
    # every move is the free preview with prose (api/evals/quality.py).
    ("1014", 6100),
    ("1011", 5032),
    ("1013", 2480),
    ("1008", 1817),
    ("1019", 903),
    ("1015", 412),
)

#: Players the crowd has already claimed: at or above ``CONSENSUS_ADD_COUNT``.
#: A sleeper or an "emerging" callout drawn from this set is the free preview
#: with prose attached, which is the failure :mod:`api.evals.quality` exists for.
CONSENSUS_IDS: frozenset[str] = frozenset(pid for pid, count in TRENDING_ADD if count >= 3000)

#: ``(player_id, drop_count)``.
TRENDING_DROP: tuple[tuple[str, int], ...] = (
    ("1016", 9044),
    ("1002", 1503),
)

#: ``meta/freshness`` markers.
# Keep the hermetic fixture fresh without tying the suite to the calendar day
# on which it happens to run. One process-stable timestamp also keeps response
# provenance deterministic within a test/eval run.
_FIXTURE_FRESH_AT = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
FRESHNESS: dict[str, str] = {
    name: _FIXTURE_FRESH_AT
    for name in ("players", "weekly_stats", "usage_trends", "def_vs_pos", "trending", "schedules")
}

#: First game per week, for :mod:`api.core.week`.
SCHEDULE_WEEKS: dict[str, str] = {
    "1": "2026-09-10T00:20:00Z",
    "2": "2026-09-17T00:15:00Z",
    "3": "2026-09-24T00:15:00Z",
    "4": "2026-10-01T00:15:00Z",
}

#: Precomputed team analytics, as ``api/data/team_analytics.py`` will supply
#: them on ``request_context`` (tech spec §6 — Python computes, the LLM narrates).
TEAM_ANALYTICS: dict[str, Any] = {
    "positional_strength": [
        {
            "position": "RB",
            "league_rank": 2,
            "league_size": 12,
            "points_per_week": 31.4,
            "league_avg_points_per_week": 24.8,
            "grade": "A-",
        },
        {
            "position": "WR",
            "league_rank": 11,
            "league_size": 12,
            "points_per_week": 18.9,
            "league_avg_points_per_week": 29.6,
            "grade": "D+",
        },
        {
            "position": "TE",
            "league_rank": 6,
            "league_size": 12,
            "points_per_week": 9.7,
            "league_avg_points_per_week": 10.2,
            "grade": "C+",
        },
    ],
    "manager_review": {
        "bench_points_lost": 41.6,
        "optimal_vs_actual": 58.2,
        "lineup_efficiency_pct": 91.3,
        "efficiency_rank": 4,
        "league_size": 12,
        "luck_note": "Third in points for, fifth in the standings — the schedule has cost a win.",
        "expected_wins": 2.4,
        "actual_wins": 2,
        "mis_start_patterns": ["Benched the higher-projected TE in two of three weeks (-11.4)."],
    },
}

#: A manual roster used by the roster-audit cases.
FIXTURE_ROSTER: tuple[dict[str, Any], ...] = (
    {"player_id": "1006", "name": "Josh Allen", "position": "QB", "starter": True},
    {"player_id": "1001", "name": "Bijan Robinson", "position": "RB", "starter": True},
    {"player_id": "1002", "name": "Breece Hall", "position": "RB", "starter": True},
    {"player_id": "1014", "name": "Chase Brown", "position": "RB", "starter": False},
    {"player_id": "1003", "name": "Ja'Marr Chase", "position": "WR", "starter": True},
    {"player_id": "1015", "name": "Jordan Addison", "position": "WR", "starter": True},
    {"player_id": "1016", "name": "Nico Collins", "position": "WR", "starter": False},
    {"player_id": "1018", "name": "Brock Bowers", "position": "TE", "starter": True},
    {"player_id": "1019", "name": "Cameron Dicker", "position": "K", "starter": True},
)

#: The league's actual free-agent pool for the team-report cases.
FIXTURE_FREE_AGENTS: tuple[dict[str, Any], ...] = (
    {"player_id": "1008", "name": "Rome Odunze", "position": "WR", "team": "CHI"},
    {"player_id": "1010", "name": "Jaxon Smith-Njigba", "position": "WR", "team": "SEA"},
    {"player_id": "1011", "name": "Tucker Kraft", "position": "TE", "team": "GB"},
)


def _points_allowed(rank: int) -> float:
    """Derive a plausible points-allowed-per-game from a rank (1 = most generous)."""
    return round(26.0 - (rank - 1) * 0.45, 2)


async def seed_store(store: Store | None = None) -> Store:
    """Populate a store with the fixture season and return it.

    Args:
        store: Store to write into; a fresh :class:`~api.core.store.MemoryStore`
            when omitted.

    Returns:
        The seeded store. Idempotent — seeding twice overwrites, never appends.
    """
    store = store if store is not None else MemoryStore()

    index: dict[str, list[dict[str, Any]]] = {}
    for pid, (name, position, team, years, injury) in PLAYERS.items():
        await store.set(
            PLAYERS_COLLECTION,
            pid,
            {
                "player_id": pid,
                "name": name,
                "search_name": normalize_name(name),
                "position": position,
                "team": team,
                "status": "Active",
                "injury_status": injury,
                "years_exp": years,
                **({"search_rank": MARKET_RANKS[pid]} if pid in MARKET_RANKS else {}),
            },
        )
        index.setdefault(normalize_name(name), []).append(
            {"player_id": pid, "name": name, "team": team, "position": position}
        )
    for key, candidates in index.items():
        # Sort defensive players first so "Josh Allen" only resolves to the QB if
        # the engine actually applies the fantasy-position preference.
        candidates.sort(key=lambda c: c["position"] in ("QB", "RB", "WR", "TE", "K", "DEF"))
        await store.set(PLAYER_INDEX_COLLECTION, key, {"candidates": candidates})

    for pid, lines in WEEKLY.items():
        team = PLAYERS[pid][2]
        for offset, (points, snap, targets, share, carries, rz) in enumerate(lines):
            week = offset + 1
            opponents = _opponent_map(week)
            await store.set(
                weekly_stats_collection(SEASON, week),
                pid,
                {
                    "player_id": pid,
                    "season": SEASON,
                    "week": week,
                    "team": team,
                    "opponent": opponents.get(team),
                    "fantasy_points": points,
                    "fantasy_points_ppr": points,
                    "snap_pct": snap,
                    "targets": targets,
                    "target_share": share,
                    "carries": carries,
                    "rz_touches": rz,
                },
            )

    for pid, (snap, share, rz, snap_d, share_d, trend) in USAGE.items():
        await store.set(
            USAGE_TRENDS_COLLECTION,
            pid,
            {
                "player_id": pid,
                "season": SEASON,
                "through_week": WEEK - 1,
                "snap_pct_l4w": snap,
                "target_share_l4w": share,
                "rz_touches_l4w": rz,
                "snap_pct_delta": snap_d,
                "target_share_delta": share_d,
                "trend": trend,
            },
        )

    for team, ranks in DEF_RANKS.items():
        await store.set(
            DEF_VS_POS_COLLECTION,
            team,
            {
                "team": team,
                "season": SEASON,
                "through_week": WEEK - 1,
                "positions": {
                    position: {"points_allowed_per_game": _points_allowed(rank), "rank": rank}
                    for position, rank in ranks.items()
                },
            },
        )

    for kind, board in (("add", TRENDING_ADD), ("drop", TRENDING_DROP)):
        await store.set(
            TRENDING_COLLECTION,
            kind,
            {
                "kind": kind,
                "lookback_hours": 24,
                "fetched_at": FRESHNESS["trending"],
                "entries": [
                    {
                        "player_id": pid,
                        "count": count,
                        "name": PLAYERS[pid][0],
                        "position": PLAYERS[pid][1],
                        "team": PLAYERS[pid][2],
                    }
                    for pid, count in board
                ],
            },
        )

    await store.set(
        SCHEDULES_COLLECTION,
        f"{SEASON}_{WEEK}",
        {
            "season": SEASON,
            "week": WEEK,
            "first_game": SCHEDULE_WEEKS[str(WEEK)],
            "games": [
                {"home": home, "away": away, "kickoff": "2026-10-04T17:00:00Z"}
                for home, away in WEEK4_GAMES
            ],
        },
    )
    await store.set(META_COLLECTION, "freshness", dict(FRESHNESS))
    await store.set(
        META_COLLECTION, "schedule_weeks", {"season": SEASON, "weeks": dict(SCHEDULE_WEEKS)}
    )
    return store


def _opponent_map(week: int = WEEK) -> dict[str, str]:
    """``team -> opponent`` for one week.

    Week 4 uses :data:`WEEK4_GAMES` verbatim (it is the week the analysis is
    scoped to and the only one with an ingested schedule document); earlier
    weeks rotate the away teams so past stat lines do not all show the same
    opponent, which would make a fixture bug look like real data.
    """
    homes = [home for home, _ in WEEK4_GAMES]
    aways = [away for _, away in WEEK4_GAMES]
    shift = (WEEK - week) % len(aways)
    rotated = aways[shift:] + aways[:shift]
    out: dict[str, str] = {}
    for home, away in zip(homes, rotated, strict=True):
        out[home] = away
        out[away] = home
    return out


# --------------------------------------------------------------------------
# Traceability index — the anti-hallucination gate
# --------------------------------------------------------------------------


def _key(value: Any) -> str | None:
    """Normalize a value for comparison, or ``None`` if it is not comparable."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return f"{float(value):.6f}"
    if isinstance(value, str):
        return value.strip()
    return None


def _collect(value: Any, into: set[str]) -> None:
    """Recursively add every scalar leaf of ``value`` to ``into``."""
    if isinstance(value, dict):
        for item in value.values():
            _collect(item, into)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, into)
    else:
        key = _key(value)
        if key is not None:
            into.add(key)


def build_value_index() -> tuple[dict[str, set[str]], set[str]]:
    """Build the set of values a citation is allowed to contain.

    Returns:
        ``(by_player, team_level)`` where ``by_player`` maps a player's display
        name to every scalar the fixture holds about them, and ``team_level``
        holds every value from the defensive splits (cited with no player).

    Derived from the same constants :func:`seed_store` writes, so the index and
    the store cannot drift.
    """
    by_player: dict[str, set[str]] = {name: set() for name, *_ in PLAYERS.values()}
    for pid, (name, position, team, years, injury) in PLAYERS.items():
        bucket = by_player[name]
        _collect([pid, name, position, team, years, injury, "Active"], bucket)
        if pid in MARKET_RANKS:
            _collect(MARKET_RANKS[pid], bucket)
        for offset, line in enumerate(WEEKLY.get(pid, [])):
            _collect(list(line) + [offset + 1, SEASON], bucket)
        if pid in USAGE:
            _collect(list(USAGE[pid]) + [SEASON, WEEK - 1], bucket)
    for pid, count in list(TRENDING_ADD) + list(TRENDING_DROP):
        _collect(count, by_player[PLAYERS[pid][0]])

    team_level: set[str] = set()
    for ranks in DEF_RANKS.values():
        for rank in ranks.values():
            _collect([rank, _points_allowed(rank)], team_level)
    return by_player, team_level


def check_citations_traceable(
    citations: Iterable[StatCitation], request_context: dict[str, Any]
) -> list[str]:
    """Return one failure message per citation that cannot be traced.

    A citation is traceable when:

    * its ``source`` names a store dataset (``nflverse ...`` / ``sleeper ...``)
      **and** its value appears in the fixture for that player — or, when the
      citation is team-level (``player is None``), in the defensive splits; or
    * its ``source`` is :data:`CONTEXT_SOURCE`, meaning the route computed it,
      **and** its value appears somewhere in ``request_context``.

    Anything else is a hallucinated number, which is the one failure mode this
    product cannot ship with.
    """
    by_player, team_level = build_value_index()
    context_values: set[str] = set()
    _collect(request_context, context_values)

    failures: list[str] = []
    for citation in citations:
        value = _key(citation.value)
        if value is None:
            failures.append(f"citation {citation.stat!r} has an uncomparable value")
            continue
        if not citation.source.strip():
            failures.append(f"citation {citation.stat!r} has no source")
            continue
        if citation.source == CONTEXT_SOURCE:
            allowed = context_values
            where = "request_context"
        elif citation.source.startswith(STORE_SOURCE_PREFIXES):
            if citation.player:
                allowed = by_player.get(citation.player, set())
                where = f"fixture data for {citation.player!r}"
            else:
                allowed = team_level
                where = "fixture defensive splits"
        else:
            failures.append(
                f"citation {citation.stat!r} has unrecognised source {citation.source!r}"
            )
            continue
        if value not in allowed:
            failures.append(
                f"UNTRACEABLE {citation.stat}={citation.value!r} "
                f"(player={citation.player!r}, source={citation.source!r}) "
                f"is not present in {where}"
            )
    return failures


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------

#: An assertion takes the parsed response and its case, and returns failures.
Assertion = Callable[[AnalysisResponse, "GoldenCase"], list[str]]


@dataclass(frozen=True)
class GoldenCase:
    """One golden query.

    Attributes:
        name: Stable identifier, printed by the runner.
        endpoint_key: Which paid endpoint to exercise.
        request_context: Exactly what a route would pass to
            :meth:`~api.agents.engine.AnalysisEngine.analyze`.
        intent: What a payer asked, in words — the human-readable query.
        assertions: Endpoint-specific property checks. The universal checks in
            :func:`universal_assertions` always run too.
    """

    name: str
    endpoint_key: str
    request_context: dict[str, Any]
    intent: str
    assertions: tuple[Assertion, ...] = field(default=())


def _fail(condition: bool, message: str) -> list[str]:
    """Return ``[message]`` when ``condition`` is false."""
    return [] if condition else [message]


def universal_assertions(response: Any, case: GoldenCase) -> list[str]:
    """Checks every paid response must pass, whatever the endpoint."""
    failures: list[str] = []
    expected = RESPONSE_MODELS[case.endpoint_key]
    failures += _fail(
        isinstance(response, expected),
        f"expected {expected.__name__}, got {type(response).__name__}",
    )
    if not isinstance(response, AnalysisResponse):
        return failures

    failures += _fail(bool(response.verdict.strip()), "verdict is empty")
    failures += _fail(bool(response.reasoning.strip()), "reasoning is empty")
    failures += _fail(
        response.confidence in ("high", "medium", "low"),
        f"confidence {response.confidence!r} is not a valid tier",
    )
    failures += _fail(response.meta.generated_at is not None, "meta.generated_at was not populated")
    failures += _fail(
        bool(response.meta.data_freshness),
        "meta.data_freshness is empty — the response cannot say how stale it is",
    )
    failures += _fail(
        response.meta.attribution.startswith("Data: nflverse"),
        "meta.attribution is missing the required nflverse credit",
    )
    for source in response.sources:
        failures += _fail(bool(source.title.strip()), "a source has no title")
    failures += check_citations_traceable(response.stats_cited, case.request_context)
    # Honest is the floor. These ask whether the answer is *worth paying for*:
    # no engine apology or code artifact in the prose, every named player real,
    # boards that cite more than the crowd's add counts, and at least one call
    # that disagrees with the market. See :mod:`api.evals.quality`.
    failures += check_quality(
        case.endpoint_key,
        response.model_dump(mode="json"),
        known_ids=set(PLAYERS),
        consensus_ids=CONSENSUS_IDS,
    )
    # Re-serialising must round-trip: this is the body an agent will parse.
    try:
        expected.model_validate(response.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        failures.append(f"response does not round-trip through its own schema: {exc}")
    return failures


# -- endpoint-specific assertions -----------------------------------------


def _trending_shape(response: Any, case: GoldenCase) -> list[str]:
    limit = int(case.request_context.get("limit") or 25)
    failures = _fail(bool(response.players), "trending board is empty")
    failures += _fail(
        len(response.players) <= limit,
        f"trending board returned {len(response.players)} rows, over the {limit} limit",
    )
    for row in response.players:
        failures += _fail(
            row.verdict in ("add", "fade", "hold"), f"{row.name}: bad verdict {row.verdict!r}"
        )
        failures += _fail(row.trend in ("add", "drop"), f"{row.name}: bad trend {row.trend!r}")
        failures += _fail(bool(row.analysis.strip()), f"{row.name}: empty analysis")
        seeded = dict(list(TRENDING_ADD) + list(TRENDING_DROP)).get(row.player_id)
        failures += _fail(
            seeded is not None and row.trend_count == seeded,
            f"{row.name}: trend_count {row.trend_count} does not match the fixture {seeded}",
        )
    return failures


def _sleepers_shape(response: Any, case: GoldenCase) -> list[str]:
    # An upper bound only. The product asks for 8-12, but a board padded to
    # eight with players the data does not support is worse than a short one
    # that says why it is short — the old ``8 <=`` here rewarded the padding.
    limit = int(case.request_context.get("limit") or 12)
    failures = _fail(
        0 < len(response.picks) <= limit,
        f"expected 1-{limit} sleeper picks with this fixture, got {len(response.picks)}",
    )
    failures += _fail(response.week == WEEK, f"week is {response.week}, expected {WEEK}")
    seen: set[str] = set()
    for pick in response.picks:
        failures += _fail(
            pick.player_id in PLAYERS, f"{pick.name}: not a fixture player ({pick.player_id})"
        )
        failures += _fail(pick.player_id not in seen, f"{pick.name}: duplicated in the board")
        seen.add(pick.player_id)
        failures += _fail(
            pick.confidence in ("high", "medium", "low"),
            f"{pick.name}: bad confidence tier {pick.confidence!r}",
        )
        failures += _fail(bool(pick.usage_note.strip()), f"{pick.name}: empty usage_note")
        failures += _fail(bool(pick.matchup_note.strip()), f"{pick.name}: empty matchup_note")
    consensus = {pid for pid, count in TRENDING_ADD if count >= 3000}
    overlap = seen & consensus
    failures += _fail(
        not overlap, f"sleepers must exclude already-consensus adds, found {sorted(overlap)}"
    )
    return failures


def _expect_player(expected_id: str) -> Assertion:
    """Assert the deep dive resolved to exactly ``expected_id``."""

    def check(response: Any, case: GoldenCase) -> list[str]:
        name, position, team, *_ = PLAYERS[expected_id]
        failures = _fail(
            response.player.player_id == expected_id,
            f"resolved to {response.player.player_id!r} ({response.player.name}), "
            f"expected {expected_id!r} ({name})",
        )
        failures += _fail(
            response.player.position == position,
            f"position is {response.player.position!r}, expected {position!r}",
        )
        failures += _fail(
            response.player.team == team, f"team is {response.player.team!r}, expected {team!r}"
        )
        seeded_weeks = {i + 1 for i in range(len(WEEKLY.get(expected_id, [])))}
        got_weeks = {line.week for line in response.player.recent_weeks}
        failures += _fail(
            got_weeks <= seeded_weeks,
            f"recent_weeks {sorted(got_weeks)} includes weeks with no fixture data",
        )
        return failures

    return check


def _unresolved_player(response: Any, case: GoldenCase) -> list[str]:
    failures = _fail(
        response.confidence == "low",
        f"an unresolvable player must be low confidence, got {response.confidence!r}",
    )
    failures += _fail(
        not response.stats_cited,
        f"an unresolvable player must cite nothing, got {len(response.stats_cited)} citation(s)",
    )
    failures += _fail(
        not response.player.player_id, "an unresolvable player must not carry a player_id"
    )
    return failures


def _matchup_shape(response: Any, case: GoldenCase) -> list[str]:
    requested = list(case.request_context.get("players") or [])
    failures = _fail(
        len(response.ranked) == len(requested),
        f"ranked {len(response.ranked)} players, {len(requested)} were requested",
    )
    ranks = [row.rank for row in response.ranked]
    failures += _fail(
        ranks == list(range(1, len(response.ranked) + 1)),
        f"ranks must be 1..n exactly once, got {ranks}",
    )
    names = {row.name.lower() for row in response.ranked}
    ids = {row.player_id for row in response.ranked}
    for token in requested:
        matched = token in ids or token.lower() in names or _is_fixture_name(token, names)
        failures += _fail(matched, f"requested player {token!r} is missing from the ranking")
    for row in response.ranked:
        failures += _fail(
            row.call in ("start", "sit", "flex", "bench"), f"{row.name}: bad call {row.call!r}"
        )
    return failures


def _is_fixture_name(token: str, names: set[str]) -> bool:
    """Whether ``token`` names a fixture player present in ``names``."""
    normalized = normalize_name(token)
    return any(normalize_name(name) == normalized for name in names)


def _roster_shape(response: Any, case: GoldenCase) -> list[str]:
    roster = case.request_context.get("roster") or []
    failures = _fail(bool(response.positional_grades), "no positional grades produced")
    failures += _fail(bool(response.start_sit), "no start/sit calls produced")
    graded = {grade.position for grade in response.positional_grades}
    expected_positions = {str(entry.get("position")) for entry in roster if entry.get("position")}
    failures += _fail(
        expected_positions <= graded,
        f"positions {sorted(expected_positions - graded)} were not graded",
    )
    called = {call.player_id for call in response.start_sit}
    expected_ids = {str(entry.get("player_id")) for entry in roster if entry.get("player_id")}
    failures += _fail(
        expected_ids <= called,
        f"roster players {sorted(expected_ids - called)} got no start/sit call",
    )
    for add in response.waiver_adds:
        failures += _fail(
            add.player_id not in expected_ids,
            f"{add.name} is already on the roster and cannot be a waiver add",
        )
    return failures


def _roster_pool_respected(response: Any, case: GoldenCase) -> list[str]:
    pool = {p["player_id"] for p in case.request_context.get("free_agents") or []}
    if not pool:
        return []
    return [
        f"{add.name} ({add.player_id}) is not in this league's free-agent pool"
        for add in response.waiver_adds
        if add.player_id not in pool
    ]


def _waivers_shape(response: Any, case: GoldenCase) -> list[str]:
    limit = int(case.request_context.get("limit") or 15)
    failures = _fail(bool(response.board), "waiver board is empty")
    failures += _fail(
        len(response.board) <= limit,
        f"board returned {len(response.board)} rows, over the {limit} limit",
    )
    failures += _fail(
        [row.rank for row in response.board] == list(range(1, len(response.board) + 1)),
        "board ranks must run 1..n in order",
    )
    for row in response.board:
        failures += _fail(
            row.stash_or_start in ("stash", "start", "streamer"),
            f"{row.name}: bad stash_or_start {row.stash_or_start!r}",
        )
        failures += _fail(
            0 < row.fab_bid_pct <= 100, f"{row.name}: implausible FAB bid {row.fab_bid_pct}"
        )
    return failures


def _report_shape(response: Any, case: GoldenCase) -> list[str]:
    failures = _fail(bool(response.emerging), "the emerging section is empty")
    failures += _fail(bool(response.stock_up), "the stock_up section is empty")
    failures += _fail(bool(response.injury_fallout), "the injury_fallout section is empty")
    for injury in response.injury_fallout:
        failures += _fail(
            bool(injury.injured_player.strip()), "an injury entry names no injured player"
        )
    for note in response.emerging + response.stock_up + response.stock_down:
        failures += _fail(bool(note.note.strip()), f"{note.name}: empty note")
    return failures


def _report_finds_the_injury(response: Any, case: GoldenCase) -> list[str]:
    injured = {entry.injured_player for entry in response.injury_fallout}
    failures = _fail(
        "Nico Collins" in injured,
        f"the fixture's only out player was not surfaced; got {sorted(injured)}",
    )
    beneficiaries = {note.name for entry in response.injury_fallout for note in entry.beneficiaries}
    failures += _fail(
        "Jayden Higgins" in beneficiaries,
        f"the obvious role-inheritor was not named; got {sorted(beneficiaries)}",
    )
    return failures


def _team_report_shape(response: Any, case: GoldenCase) -> list[str]:
    failures = _fail(
        response.sleeper_username == case.request_context.get("sleeper_username"),
        "the report is not attributed to the requested manager",
    )
    for strength in response.positional_strength_vs_league:
        failures += _fail(
            0 < strength.league_rank <= strength.league_size,
            f"{strength.position}: rank {strength.league_rank} outside a "
            f"{strength.league_size}-team league",
        )
    for deficiency in response.deficiencies:
        for fix in deficiency.available_fixes:
            failures += _fail(
                fix.position == deficiency.position,
                f"fix {fix.name} ({fix.position}) does not address the "
                f"{deficiency.position} deficiency",
            )
    return failures


def _team_report_narrates_analytics(response: Any, case: GoldenCase) -> list[str]:
    """The supplied numbers must survive verbatim — the engine narrates only."""
    supplied = {
        row["position"]: row
        for row in case.request_context["team_analytics"]["positional_strength"]
    }
    failures: list[str] = []
    for strength in response.positional_strength_vs_league:
        source = supplied.get(strength.position)
        if source is None:
            failures.append(f"{strength.position} was not in the supplied analytics")
            continue
        failures += _fail(
            strength.league_rank == source["league_rank"]
            and strength.points_per_week == source["points_per_week"],
            f"{strength.position}: analytics were altered "
            f"({strength.league_rank}/{strength.points_per_week} vs "
            f"{source['league_rank']}/{source['points_per_week']})",
        )
    review = case.request_context["team_analytics"]["manager_review"]
    failures += _fail(
        response.manager_review.lineup_efficiency_pct == review["lineup_efficiency_pct"],
        "lineup efficiency was altered rather than narrated",
    )
    failures += _fail(
        response.manager_review.bench_points_lost == review["bench_points_lost"],
        "bench points lost was altered rather than narrated",
    )
    return failures


def _team_report_honest_placeholder(response: Any, case: GoldenCase) -> list[str]:
    """With no analytics supplied, missing metrics must be labelled, not invented."""
    review = response.manager_review
    failures = _fail(
        review.lineup_efficiency_pct == 0.0 and review.bench_points_lost == 0.0,
        "manager metrics were invented although no analytics were supplied",
    )
    failures += _fail(
        bool(review.observations),
        "a review with no computed metrics must say so in observations",
    )
    failures += _fail(
        any("not computed" in text.lower() for text in [review.luck_note, *review.observations]),
        "the response does not disclose that the metrics were not computed",
    )
    return failures


def _no_usage_rollup(response: Any, case: GoldenCase) -> list[str]:
    """A player with stat lines but no usage rollup still yields a full answer."""
    return _fail(
        response.player.usage_trajectory is None,
        "a player with no ingested usage rollup must not claim a usage trajectory",
    )


# --------------------------------------------------------------------------
# The 20 golden queries
# --------------------------------------------------------------------------


def _draft_board_shape(response: Any, case: GoldenCase) -> list[str]:
    limit = int(case.request_context.get("limit") or 200)
    rows = [player for tier in response.tiers for player in tier.players]
    failures = _fail(bool(response.tiers), "the draft board has no tiers")
    failures += _fail(
        len(rows) <= limit, f"board returned {len(rows)} rows, over the {limit} limit"
    )
    failures += _fail(
        [row.rank for row in rows] == list(range(1, len(rows) + 1)),
        "board ranks must run 1..n in tier order",
    )
    failures += _fail(
        [tier.tier for tier in response.tiers] == list(range(1, len(response.tiers) + 1)),
        "tier numbers must run 1..n",
    )
    # value_delta compares our rank to the market's ordering of this same set,
    # so recompute those positions rather than reaching for the global rank.
    ranked_by_market = sorted(
        [row for row in rows if row.market_rank is not None],
        key=lambda row: row.market_rank or 0,
    )
    positions = {row.player_id: index for index, row in enumerate(ranked_by_market, start=1)}
    for row in rows:
        if row.player_id in positions:
            failures += _fail(
                row.value_delta == positions[row.player_id] - row.rank,
                f"{row.name}: value_delta must be market position minus our rank",
            )
        failures += _fail(bool(row.note), f"{row.name}: every board row needs a note")
    return failures


def _draft_board_is_not_the_market(response: Any, case: GoldenCase) -> list[str]:
    """A board that reprints Sleeper's ordering is not worth paying for."""
    rows = [player for tier in response.tiers for player in tier.players]
    graded = [row for row in rows if row.value_delta is not None]
    moved = [row for row in graded if row.value_delta != 0]
    failures = _fail(
        bool(moved),
        "no player moved from their market rank — the board is a copy of the market",
    )
    failures += _fail(bool(response.values), "no draft-day values were identified")
    failures += _fail(bool(response.reaches), "no reaches were identified")
    return failures


def _draft_board_never_says_adp(response: Any, case: GoldenCase) -> list[str]:
    """``market_rank`` is Sleeper draft popularity, not a consensus ADP.

    Two obligations that pull in opposite directions: the per-player notes and
    the verdict must never *use* the term, while the reasoning must explicitly
    *disclaim* it. A blanket "the word must not appear" check fails the
    disclaimer, which is the one place it belongs.
    """
    claimed = " ".join(
        [response.verdict] + [player.note for tier in response.tiers for player in tier.players]
    ).lower()
    failures = _fail(
        "adp" not in claimed,
        "a board row or the verdict called the market signal ADP, which it is not",
    )
    failures += _fail(
        "adp" in response.reasoning.lower(),
        "the reasoning must say plainly that market_rank is not a consensus ADP",
    )
    return failures


def _draft_report_shape(response: Any, case: GoldenCase) -> list[str]:
    picks = case.request_context.get("picks") or []
    failures = _fail(bool(response.roster), "the graded roster is empty")
    failures += _fail(
        len(response.roster) <= len(picks),
        f"graded {len(response.roster)} picks from {len(picks)} supplied",
    )
    failures += _fail(bool(response.grade), "the draft was not graded")
    failures += _fail(bool(response.positional_balance), "positional balance is missing")
    failures += _fail(bool(response.week_one_plan), "the week-one plan is empty")
    for pick in response.roster:
        if pick.market_rank is not None:
            failures += _fail(
                pick.value_delta == pick.pick_no - pick.market_rank,
                f"{pick.name}: value_delta must be pick_no - market_rank",
            )
    for review in [*response.best_picks, *response.worst_picks]:
        failures += _fail(
            review.verdict in ("value", "fair", "reach"),
            f"{review.name}: bad verdict {review.verdict!r}",
        )
        failures += _fail(bool(review.note), f"{review.name}: a called-out pick needs a note")
    return failures


def _draft_report_finds_the_reach(response: Any, case: GoldenCase) -> list[str]:
    """The fixture draft contains a deliberate reach; a grader must see it."""
    called = {review.player_id for review in response.worst_picks}
    return _fail(
        "1009" in called,
        "Tyjae Spears was drafted 28 picks before his market rank and was not flagged",
    )


GOLDEN_CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        name="trending_full_board",
        endpoint_key="trending",
        request_context={"week": WEEK, "season": SEASON},
        intent="Who is everyone adding and dropping right now, and should I follow?",
        assertions=(_trending_shape,),
    ),
    GoldenCase(
        name="trending_small_board",
        endpoint_key="trending",
        request_context={"week": WEEK, "season": SEASON, "limit": 6, "lookback_hours": 48},
        intent="Just the top handful of trending moves, 48h window.",
        assertions=(_trending_shape,),
    ),
    GoldenCase(
        name="sleepers_week4",
        endpoint_key="sleepers",
        request_context={"week": WEEK, "season": SEASON},
        intent="Give me this week's sleeper starts.",
        assertions=(_sleepers_shape,),
    ),
    GoldenCase(
        name="sleepers_capped",
        endpoint_key="sleepers",
        request_context={"week": WEEK, "season": SEASON, "limit": 9},
        intent="Nine sleepers, no filler.",
        assertions=(_sleepers_shape,),
    ),
    GoldenCase(
        name="player_by_name",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "name": "Bijan Robinson"},
        intent="Deep dive on Bijan Robinson.",
        assertions=(_expect_player("1001"),),
    ),
    GoldenCase(
        name="player_by_id",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "player_id": "1003"},
        intent="Deep dive by Sleeper id (Ja'Marr Chase).",
        assertions=(_expect_player("1003"),),
    ),
    GoldenCase(
        name="player_ambiguous_name",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "name": "Josh Allen"},
        intent="Josh Allen — the fantasy one, not the linebacker.",
        assertions=(_expect_player("1006"),),
    ),
    GoldenCase(
        name="player_punctuation_and_suffix",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "name": "marvin harrison jr"},
        intent="Sloppy spelling must still resolve.",
        assertions=(_expect_player("1004"),),
    ),
    GoldenCase(
        name="player_unknown_name",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "name": "Wilford Brimley"},
        intent="A name that is not in the league at all.",
        assertions=(_unresolved_player,),
    ),
    GoldenCase(
        name="player_without_usage_rollup",
        endpoint_key="player",
        request_context={"week": WEEK, "season": SEASON, "player_id": "1018"},
        intent="A player with stat lines but no ingested usage rollup.",
        assertions=(_expect_player("1018"), _no_usage_rollup),
    ),
    GoldenCase(
        name="matchup_two_players",
        endpoint_key="matchup",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "players": ["Bijan Robinson", "Breece Hall"],
        },
        intent="Bijan or Breece this week?",
        assertions=(_matchup_shape,),
    ),
    GoldenCase(
        name="matchup_four_players",
        endpoint_key="matchup",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "players": ["1003", "1004", "1010", "1015"],
        },
        intent="Rank my four wideouts.",
        assertions=(_matchup_shape,),
    ),
    GoldenCase(
        name="matchup_with_unknown_player",
        endpoint_key="matchup",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "players": ["Trey McBride", "Tucker Kraft", "Not A Player"],
        },
        intent="A start/sit where one name is junk — nobody may be silently dropped.",
        assertions=(_matchup_shape,),
    ),
    GoldenCase(
        name="waivers_big_board",
        endpoint_key="waivers",
        request_context={"week": WEEK, "season": SEASON},
        intent="Waiver wire big board for the week.",
        assertions=(_waivers_shape,),
    ),
    GoldenCase(
        name="waivers_top_five",
        endpoint_key="waivers",
        request_context={"week": WEEK, "season": SEASON, "limit": 5},
        intent="Only my top five claims — I have one waiver priority.",
        assertions=(_waivers_shape,),
    ),
    GoldenCase(
        name="roster_manual_paste",
        endpoint_key="roster",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "roster": [dict(entry) for entry in FIXTURE_ROSTER],
        },
        intent="Audit this pasted roster.",
        assertions=(_roster_shape,),
    ),
    GoldenCase(
        name="roster_with_league_pool",
        endpoint_key="roster",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "sleeper_username": "playclock_ryan",
            "league_id": "998877",
            "roster": [dict(entry) for entry in FIXTURE_ROSTER],
            "free_agents": [dict(p) for p in FIXTURE_FREE_AGENTS],
        },
        intent="Audit my Sleeper roster and only suggest players free in my league.",
        assertions=(_roster_shape, _roster_pool_respected),
    ),
    GoldenCase(
        name="report_week4",
        endpoint_key="report",
        request_context={"week": WEEK, "season": SEASON},
        intent="The full weekly briefing.",
        assertions=(_report_shape, _report_finds_the_injury),
    ),
    GoldenCase(
        name="team_report_with_analytics",
        endpoint_key="team_report",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "sleeper_username": "playclock_ryan",
            "league_id": "998877",
            "league_name": "Dynasty Warriors",
            "roster": [dict(entry) for entry in FIXTURE_ROSTER],
            "free_agents": [dict(p) for p in FIXTURE_FREE_AGENTS],
            "team_analytics": TEAM_ANALYTICS,
        },
        intent="Grade my team against my actual leaguemates and review my management.",
        assertions=(_team_report_shape, _team_report_narrates_analytics),
    ),
    GoldenCase(
        name="team_report_without_analytics",
        endpoint_key="team_report",
        request_context={
            "week": WEEK,
            "season": SEASON,
            "sleeper_username": "playclock_ryan",
            "league_id": "998877",
            "roster": [dict(entry) for entry in FIXTURE_ROSTER],
        },
        intent="Same report when league history could not be fetched — must not invent numbers.",
        assertions=(_team_report_shape, _team_report_honest_placeholder),
    ),
    GoldenCase(
        name="draft_board_full",
        endpoint_key="draft_board",
        request_context={"season": SEASON, "limit": 200},
        intent="Give me a draft board before my league drafts tonight.",
        assertions=(
            _draft_board_shape,
            _draft_board_is_not_the_market,
            _draft_board_never_says_adp,
        ),
    ),
    GoldenCase(
        name="draft_board_capped",
        endpoint_key="draft_board",
        request_context={"season": SEASON, "limit": 25},
        intent="Just the first two rounds' worth.",
        assertions=(_draft_board_shape,),
    ),
    GoldenCase(
        name="draft_report_graded",
        endpoint_key="draft_report",
        request_context={
            "season": SEASON,
            "draft_id": "fixture-draft-1",
            "picks": list(DRAFT_PICKS),
        },
        intent="How did I do in my draft?",
        assertions=(_draft_report_shape, _draft_report_finds_the_reach),
    ),
)

assert len(GOLDEN_CASES) == 23, (
    "tech spec §5 calls for 20 golden queries across the original eight endpoints, "
    "plus three for the draft pair"
)
assert {case.endpoint_key for case in GOLDEN_CASES} == set(RESPONSE_MODELS), (
    "the golden set must exercise every paid endpoint"
)
