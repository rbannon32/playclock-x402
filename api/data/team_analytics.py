"""Deterministic team analytics for ``POST /v1/team-report``.

**Binding rule (tech spec §6):** lineup efficiency, bench points lost, positional
splits, luck and leaguemate rankings are computed *here*, in Python, from Sleeper
matchup history. The LLM narrates these numbers; it never produces, adjusts or
re-derives them. Everything this module returns is stamped with a ``computed``
provenance marker so the synthesis agent (and any agent consuming the response)
can tell arithmetic from prose.

Purity
------
Every function in this module is pure: plain dicts and lists in, plain JSON-able
dicts out. No I/O, no :class:`~api.core.store.Store`, no ``httpx``, no clock, no
global state. The request handler fetches from Sleeper and hands the raw payloads
straight in. That makes the whole computation trivially testable and identical in
CI, local dev and Cloud Run.

Inputs (raw Sleeper shapes, tech spec §4.1)
-------------------------------------------
``league``
    ``GET /league/{id}`` — ``{"roster_positions": [...], "scoring_settings": {...},
    "settings": {...}, "name": ...}``.

``rosters``
    ``GET /league/{id}/rosters`` — ``[{"roster_id", "owner_id", "co_owners",
    "players": [...], "starters": [...], "settings": {"wins","losses","fpts",
    "fpts_decimal","fpts_against",...}}]``.

``matchups_by_week``
    ``{week: GET /league/{id}/matchups/{week}}`` — one entry per roster per week:
    ``{"roster_id", "matchup_id", "points", "players", "starters",
    "players_points", "starters_points"}``. Week keys may be ``int`` or ``str``.

``player_lookup``
    ``{player_id: {"name":..., "position":..., "fantasy_positions": [...]}}`` —
    projected from the ingested ``players`` collection
    (:mod:`api.data.stats_store`). Unknown ids degrade to "eligible for nothing"
    plus a warning, never a crash.

``users``
    ``GET /league/{id}/users`` — ``[{"user_id", "display_name",
    "metadata": {"team_name": ...}}]``, used only to label teams.

Warnings
--------
Nothing in here raises on messy input. Every degradation appends a short machine-
readable token to a ``warnings`` list that travels with the result, e.g.
``"unknown_slot:DL"``, ``"unknown_player:9999"``, ``"unfilled_slot:TE"``,
``"optimal_below_actual:w3"``, ``"no_matchup_history"``. (``"non_laminar_slots"``
is retired: the lineup solver is exact for every slot configuration.)
Wave 3 can surface or ignore them; they exist so a silently wrong number is
impossible.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

#: Bumped whenever the shape of :func:`build_team_report_facts` changes, so the
#: agents wave and the response cache can detect a stale fact block.
FACTS_VERSION = "1"

#: Slots that are not part of the starting lineup and are ignored everywhere.
BENCH_SLOTS: frozenset[str] = frozenset({"BN", "BENCH", "IR", "TAXI"})

#: Slot name -> the set of player positions allowed to fill it.
#:
#: Sleeper's ``roster_positions`` uses these names. FLEX accepts RB/WR/TE;
#: SUPER_FLEX adds QB; IDP_FLEX accepts the defensive-player positions. Any slot
#: name *not* listed here is treated as a single-position slot named after
#: itself (so a league with a ``"DL"`` or a bespoke IDP slot still computes),
#: and records an ``unknown_slot:`` warning.
SLOT_ELIGIBILITY: dict[str, frozenset[str]] = {
    "QB": frozenset({"QB"}),
    "RB": frozenset({"RB"}),
    "WR": frozenset({"WR"}),
    "TE": frozenset({"TE"}),
    "K": frozenset({"K"}),
    "DEF": frozenset({"DEF"}),
    "DL": frozenset({"DL", "DE", "DT"}),
    "LB": frozenset({"LB"}),
    "DB": frozenset({"DB", "CB", "S"}),
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "IDP_FLEX": frozenset({"DL", "DE", "DT", "LB", "DB", "CB", "S"}),
}

#: Sleeper writes an empty starting slot as the string ``"0"``.
EMPTY_SLOT_IDS: frozenset[str] = frozenset({"", "0", "None", "null"})

#: Letter grade bands over the z-score of a team's points-per-week for a position
#: group, measured against the league (population) mean and standard deviation.
#:
#: Bands are a uniform 0.25σ wide near the mean and widen at the tails; the league
#: mean sits exactly on the ``B-``/``C+`` boundary, i.e. "dead average is a low B
#: / high C". Entries are ``(lower_bound_inclusive, grade)``, highest first.
GRADE_BANDS: tuple[tuple[float, str], ...] = (
    (1.75, "A+"),
    (1.25, "A"),
    (0.75, "A-"),
    (0.50, "B+"),
    (0.25, "B"),
    (0.00, "B-"),
    (-0.25, "C+"),
    (-0.50, "C"),
    (-0.75, "C-"),
    (-1.25, "D"),
    (float("-inf"), "F"),
)

#: |luck_score| below this many wins is reported as ``"neutral"`` rather than
#: lucky/unlucky — a third of a win is noise, not a story.
LUCK_NEUTRAL_BAND = 0.5


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------


def _norm_id(value: Any) -> str:
    """Normalize a Sleeper player id to a string (Sleeper mixes int and str)."""
    if value is None:
        return ""
    return str(value).strip()


def _is_real_player(player_id: str) -> bool:
    """Return whether ``player_id`` names a player rather than an empty slot."""
    return bool(player_id) and player_id not in EMPTY_SLOT_IDS


def _as_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float, falling back to ``default`` on junk/None."""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _points_of(players_points: Mapping[str, Any] | None, player_id: str) -> float:
    """Return a player's points for the week.

    A missing entry means bye week, DNP, or a player who was never on the roster
    that week — all of which are worth exactly ``0.0`` fantasy points, so they are
    scored as such rather than dropped (dropping would silently shrink the
    denominator of every efficiency number).
    """
    if not players_points:
        return 0.0
    if player_id in players_points:
        return _as_float(players_points[player_id])
    # Sleeper occasionally returns integer-keyed maps after a JSON round-trip.
    for key, value in players_points.items():
        if _norm_id(key) == player_id:
            return _as_float(value)
    return 0.0


def _player_positions(player_id: str, player_lookup: Mapping[str, Any]) -> frozenset[str]:
    """Return every position ``player_id`` may be slotted as, upper-cased.

    Uses ``position`` plus ``fantasy_positions`` (Sleeper lists e.g. a converted
    WR/RB in both). An unknown player yields the empty set: they are eligible for
    no slot, so they can never enter an *optimal* lineup, while their actual
    points still count. Callers record an ``unknown_player:`` warning.
    """
    entry = player_lookup.get(player_id)
    if not isinstance(entry, Mapping):
        return frozenset()
    positions: set[str] = set()
    primary = entry.get("position")
    if primary:
        positions.add(str(primary).upper())
    for extra in entry.get("fantasy_positions") or []:
        if extra:
            positions.add(str(extra).upper())
    return frozenset(positions)


def _player_name(player_id: str, player_lookup: Mapping[str, Any]) -> str | None:
    entry = player_lookup.get(player_id)
    if isinstance(entry, Mapping):
        name = entry.get("name")
        return str(name) if name else None
    return None


def _primary_position(player_id: str, player_lookup: Mapping[str, Any]) -> str | None:
    entry = player_lookup.get(player_id)
    if isinstance(entry, Mapping):
        position = entry.get("position")
        if position:
            return str(position).upper()
        for extra in entry.get("fantasy_positions") or []:
            if extra:
                return str(extra).upper()
    return None


def starting_slots(roster_positions: Sequence[Any] | None) -> list[str]:
    """Return the upper-cased starting slots of a league, bench slots removed.

    The returned list is index-aligned with a Sleeper matchup entry's
    ``starters`` array — that alignment is what makes per-slot mis-start
    detection possible.
    """
    return [
        str(slot).upper()
        for slot in (roster_positions or [])
        if str(slot).upper() not in BENCH_SLOTS
    ]


def _slot_descriptors(
    roster_positions: Sequence[Any] | None,
) -> tuple[list[tuple[int, str, frozenset[str]]], list[str]]:
    """Return ``[(index, slot_name, eligible_positions)]`` plus warnings."""
    warnings: list[str] = []
    descriptors: list[tuple[int, str, frozenset[str]]] = []
    for index, slot in enumerate(starting_slots(roster_positions)):
        eligible = SLOT_ELIGIBILITY.get(slot)
        if eligible is None:
            # Unknown slot name: accept any player whose position (or one of its
            # fantasy_positions) equals the slot name. Standard for bespoke IDP
            # configurations, where the slot *is* the position.
            eligible = frozenset({slot})
            warnings.append(f"unknown_slot:{slot}")
        descriptors.append((index, slot, eligible))
    return descriptors, warnings


#: Fixed-point scale for points inside the assignment solver. Scores are
#: rounded to this many decimals only for *choosing* the lineup, which keeps
#: the solver in exact integer arithmetic; totals are summed from the raw floats.
_POINT_SCALE = 10**6


def _max_weight_assignment(weights: Sequence[Sequence[int]]) -> list[int]:
    """Solve the assignment problem exactly: rows -> distinct columns, max total weight.

    The Hungarian algorithm (shortest augmenting paths with potentials), in
    integer arithmetic so there is no float tie to break differently from one
    run to the next. Requires ``len(rows) <= len(columns)``; every row is
    assigned. ``O(rows^2 * columns)`` — a starting lineup against a roster is
    at most a few thousand cell visits.

    Returns:
        The column assigned to each row, index-aligned with ``weights``.
    """
    rows = len(weights)
    cols = len(weights[0]) if rows else 0
    if rows == 0:
        return []
    inf = float("inf")
    # 1-indexed potentials; column 0 is the virtual start of each augmenting path.
    # Integers in practice: every slack is a difference of integer weights.
    u: list[float] = [0] * (rows + 1)
    v: list[float] = [0] * (cols + 1)
    owner = [0] * (cols + 1)  # owner[j] = row (1-indexed) holding column j, 0 = free
    way = [0] * (cols + 1)
    for row in range(1, rows + 1):
        owner[0] = row
        j0 = 0
        min_slack: list[float] = [inf] * (cols + 1)
        visited = [False] * (cols + 1)
        while True:
            visited[j0] = True
            i0 = owner[j0]
            delta: float = inf
            j1 = 0
            for j in range(1, cols + 1):
                if visited[j]:
                    continue
                # Minimize the negated weight.
                slack = -weights[i0 - 1][j - 1] - u[i0] - v[j]
                if slack < min_slack[j]:
                    min_slack[j] = slack
                    way[j] = j0
                if min_slack[j] < delta:
                    delta = min_slack[j]
                    j1 = j
            for j in range(cols + 1):
                if visited[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    min_slack[j] -= delta
            j0 = j1
            if owner[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            owner[j0] = owner[j1]
            j0 = j1
    assigned = [-1] * rows
    for j in range(1, cols + 1):
        if owner[j]:
            assigned[owner[j] - 1] = j - 1
    return assigned


def _solve_lineup(
    descriptors: Sequence[tuple[int, str, frozenset[str]]],
    pool: Sequence[tuple[str, frozenset[str], float]],
) -> dict[int, tuple[str, float]]:
    """Return ``{slot_index: (player_id, points)}`` for a best legal lineup.

    Exact: a maximum-weight bipartite matching of slots to eligible players,
    with a lexicographic objective encoded in integer weights —

    1. fill as many slots as possible (a filled slot is worth more than any
       difference in points, so a negative-scoring player still beats an
       empty slot, as it always did);
    2. then maximize total points;
    3. then, among equal lineups, agree with the most-restrictive-first
       greedy as often as possible, so a tie resolves the way it always has
       (roster order, most restrictive slot first) and the result is a pure
       function of the input.

    The greedy alone was optimal only when no player is eligible for two
    slots that do not nest. A QB/TE-eligible player broke it with plain
    ``QB`` and ``TE`` slots: the greedy started him at QB and a 1-point TE
    beside him while the lineup with him at TE scored 14 more (tested).
    """
    slots = list(descriptors)
    players = [entry for entry in pool if entry[1]]  # unknown players fit no slot
    if not slots or not players:
        return {}

    greedy = _greedy_lineup(slots, players)
    scaled = [round(points * _POINT_SCALE) for _, _, points in players]
    # Weight = fill_bonus + points * tie_span + agrees. Two lineups' point
    # totals differ by less than point_span, so one more filled slot always
    # wins; one scaled point is worth tie_span, more than every agreement
    # bonus combined, so points always beat the tie-break.
    point_span = 2 * sum(abs(value) for value in scaled) + 1
    tie_span = len(slots) + 1
    fill_bonus = point_span * tie_span

    weights: list[list[int]] = []
    for index, _, eligible in slots:
        row: list[int] = []
        for column, (player_id, positions, _) in enumerate(players):
            if positions & eligible:
                agrees = 1 if greedy.get(index) == player_id else 0
                row.append(fill_bonus + scaled[column] * tie_span + agrees)
            else:
                row.append(0)  # not an edge: equivalent to leaving the slot empty
        weights.append(row)
    # Pad with "empty" columns so every slot can be left unfilled if it must.
    width = len(players) + len(slots)
    for row in weights:
        row.extend([0] * (width - len(row)))

    assignment: dict[int, tuple[str, float]] = {}
    for (index, _, eligible), column in zip(slots, _max_weight_assignment(weights), strict=True):
        if column >= len(players):
            continue
        player_id, positions, points = players[column]
        if positions & eligible:
            assignment[index] = (player_id, points)
    return assignment


def _greedy_lineup(
    slots: Sequence[tuple[int, str, frozenset[str]]],
    players: Sequence[tuple[str, frozenset[str], float]],
) -> dict[int, str]:
    """Most-restrictive-first greedy: the tie-break preference for :func:`_solve_lineup`.

    Slots are filled fewest-eligible-positions first (ties by slot order),
    each with the highest-scoring unused eligible player (ties by roster
    order). Not optimal on its own; see :func:`_solve_lineup`.
    """
    used: set[str] = set()
    picks: dict[int, str] = {}
    for index, _, eligible in sorted(slots, key=lambda d: (len(d[2]), d[0])):
        best: tuple[str, float] | None = None
        for player_id, positions, points in players:
            if player_id in used or not (positions & eligible):
                continue
            if best is None or points > best[1]:
                best = (player_id, points)
        if best is not None:
            used.add(best[0])
            picks[index] = best[0]
    return picks


def _candidate_pool(
    roster_player_ids: Iterable[Any] | None,
    players_points: Mapping[str, Any] | None,
    player_lookup: Mapping[str, Any],
) -> tuple[list[tuple[str, frozenset[str], float]], list[str]]:
    """Return ``[(player_id, eligible_positions, points)]`` plus warnings.

    Order follows ``roster_player_ids`` and duplicates are dropped, which makes
    every downstream tie-break deterministic.
    """
    warnings: list[str] = []
    pool: list[tuple[str, frozenset[str], float]] = []
    seen: set[str] = set()
    for raw in roster_player_ids or []:
        player_id = _norm_id(raw)
        if not _is_real_player(player_id) or player_id in seen:
            continue
        seen.add(player_id)
        positions = _player_positions(player_id, player_lookup)
        if not positions:
            warnings.append(f"unknown_player:{player_id}")
        pool.append((player_id, positions, _points_of(players_points, player_id)))
    return pool, warnings


def _round(value: float, digits: int = 2) -> float:
    """Round for JSON output, normalizing ``-0.0`` to ``0.0``."""
    result = round(float(value), digits)
    return 0.0 if result == 0 else result


def grade_from_z(z_score: float) -> str:
    """Return the letter grade for a z-score using :data:`GRADE_BANDS`.

    Scale (documented so the narration can explain it): the grade is a pure
    function of how many population standard deviations a team's points-per-week
    for a position group sits above or below the league mean. ``B-``/``C+`` is
    the mean itself, ``A`` is roughly the top 10% of a normal league, ``F`` is
    the bottom ~10%.
    """
    for lower_bound, grade in GRADE_BANDS:
        if z_score >= lower_bound:
            return grade
    return "F"  # pragma: no cover - the final band is -inf


# --------------------------------------------------------------------------
# Optimal lineup + efficiency
# --------------------------------------------------------------------------


def optimal_lineup(
    roster_player_ids: Iterable[Any] | None,
    players_points: Mapping[str, Any] | None,
    roster_positions: Sequence[Any] | None,
    player_lookup: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the highest-scoring legal lineup for one week.

    Solved exactly as an assignment problem (slots x eligible players, see
    :func:`_solve_lineup`): the most slots that can be filled, then the most
    points, then — among equal lineups — the one closest to filling the most
    restrictive slot first with the best player in roster order, so ties are
    deterministic. Bench slots (``BN``/``IR``/``TAXI``) are ignored.

    This replaced a most-restrictive-first greedy that was argued optimal
    whenever the *slots'* eligibility sets were laminar (disjoint or nested).
    That argument missed players eligible at two positions: with slots
    ``[QB, TE]`` and a QB/TE player, the greedy spent him at QB and reported
    a lineup 14 points short of the real optimum with no warning — and every
    efficiency number downstream (bench points lost, mis-starts) inherits the
    optimum. Any slot configuration, laminar or not, is now solved exactly.

    Args:
        roster_player_ids: Every player available to the team that week
            (a matchup entry's ``players``), starters and bench alike.
        players_points: ``{player_id: points}`` for the week. Missing entries
            score ``0.0`` (bye/DNP).
        roster_positions: The league's ``roster_positions`` array.
        player_lookup: ``{player_id: {"name","position","fantasy_positions"}}``.

    Returns:
        ``{"starters": [player_id, ...], "points": float, "by_slot": [...],
        "warnings": [...]}``. ``starters`` is in slot order and omits unfilled
        slots; ``by_slot`` keeps every slot, with ``player_id: None`` for one
        that could not be filled.
    """
    descriptors, warnings = _slot_descriptors(roster_positions)
    pool, pool_warnings = _candidate_pool(roster_player_ids, players_points, player_lookup)
    warnings.extend(pool_warnings)

    assignment = _solve_lineup(descriptors, pool)
    for index, slot, _ in sorted(descriptors, key=lambda d: (len(d[2]), d[0])):
        if index not in assignment:
            warnings.append(f"unfilled_slot:{slot}")

    by_slot: list[dict[str, Any]] = []
    starters: list[str] = []
    total = 0.0
    for index, slot, _ in descriptors:
        picked = assignment.get(index)
        player_id = picked[0] if picked else None
        points = picked[1] if picked else 0.0
        total += points
        by_slot.append(
            {
                "slot_index": index,
                "slot": slot,
                "player_id": player_id,
                "name": _player_name(player_id, player_lookup) if player_id else None,
                "position": _primary_position(player_id, player_lookup) if player_id else None,
                "points": _round(points),
            }
        )
        if player_id:
            starters.append(player_id)

    return {
        "starters": starters,
        "points": _round(total),
        "by_slot": by_slot,
        "warnings": warnings,
    }


def lineup_efficiency(
    actual_starters: Iterable[Any] | None,
    players_points: Mapping[str, Any] | None,
    optimal: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare what the manager started against the optimal lineup.

    Args:
        actual_starters: The matchup entry's ``starters`` array. Empty-slot
            markers (``"0"``) are ignored and score nothing.
        players_points: ``{player_id: points}`` for the same week.
        optimal: The result of :func:`optimal_lineup` for the same week.

    Returns:
        ``{"actual_points", "optimal_points", "bench_points_lost",
        "efficiency_pct"}``. ``bench_points_lost`` is optimal minus actual — the
        points a benched player would have added had the lineup been perfect.
        ``efficiency_pct`` is ``actual / optimal * 100``, and is ``100.0`` when
        the optimal lineup scores zero (nothing was left on the bench).

    Note:
        ``optimal_points`` is floored at ``actual_points``. That only bites when
        an actual starter is missing from ``player_lookup`` (so the optimizer
        could not consider them); the caller records an ``optimal_below_actual``
        warning for the week rather than publishing an efficiency above 100%.
    """
    actual = 0.0
    for raw in actual_starters or []:
        player_id = _norm_id(raw)
        if _is_real_player(player_id):
            actual += _points_of(players_points, player_id)

    optimal_points = max(_as_float(optimal.get("points")), actual)
    lost = max(0.0, optimal_points - actual)
    efficiency = 100.0 if optimal_points <= 0 else min(100.0, actual / optimal_points * 100.0)
    return {
        "actual_points": _round(actual),
        "optimal_points": _round(optimal_points),
        "bench_points_lost": _round(lost),
        "efficiency_pct": _round(efficiency),
    }


# --------------------------------------------------------------------------
# Week iteration
# --------------------------------------------------------------------------


def _iter_weeks(
    matchups_by_week: Mapping[Any, Any] | None,
) -> Iterator[tuple[int, list[Mapping[str, Any]]]]:
    """Yield ``(week, entries)`` in ascending week order, skipping empty weeks.

    Week keys may be ``int`` or ``str``; non-numeric keys and weeks with no
    matchup entries are skipped (a week the league has not played yet comes back
    from Sleeper as ``[]``).
    """
    if not matchups_by_week:
        return
    numbered: list[tuple[int, list[Mapping[str, Any]]]] = []
    for raw_week, entries in matchups_by_week.items():
        try:
            week = int(raw_week)
        except (TypeError, ValueError):
            logger.warning("skipping non-numeric matchup week key %r", raw_week)
            continue
        rows = [e for e in (entries or []) if isinstance(e, Mapping)]
        if not rows:
            continue
        numbered.append((week, rows))
    numbered.sort(key=lambda item: item[0])
    yield from numbered


def _entry_for(entries: Sequence[Mapping[str, Any]], roster_id: int) -> Mapping[str, Any] | None:
    """Return the matchup entry belonging to ``roster_id``, or ``None``."""
    for entry in entries:
        if _as_int(entry.get("roster_id")) == roster_id:
            return entry
    return None


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _week_roster_players(entry: Mapping[str, Any]) -> list[str]:
    """Return every player available to a team that week, deduped, in a stable order.

    Prefers the entry's ``players`` array; falls back to ``starters`` plus the
    keys of ``players_points`` when Sleeper omits it (it does for some legacy
    leagues), so the optimizer always sees the full bench.
    """
    ordered: list[str] = []
    seen: set[str] = set()

    def _push(raw: Any) -> None:
        player_id = _norm_id(raw)
        if _is_real_player(player_id) and player_id not in seen:
            seen.add(player_id)
            ordered.append(player_id)

    for raw in entry.get("players") or []:
        _push(raw)
    for raw in entry.get("starters") or []:
        _push(raw)
    for raw in entry.get("players_points") or {}:
        _push(raw)
    return ordered


def _players_points(entry: Mapping[str, Any]) -> dict[str, float]:
    """Return a normalized ``{player_id: points}`` map for one matchup entry.

    Merges ``players_points`` with the index-aligned ``starters_points`` fallback
    so a starter still scores when Sleeper omits them from the map.
    """
    points: dict[str, float] = {}
    raw_points = entry.get("players_points") or {}
    if isinstance(raw_points, Mapping):
        for key, value in raw_points.items():
            player_id = _norm_id(key)
            if _is_real_player(player_id):
                points[player_id] = _as_float(value)
    starters = [_norm_id(s) for s in entry.get("starters") or []]
    starters_points = entry.get("starters_points") or []
    if isinstance(starters_points, Sequence) and not isinstance(starters_points, (str, bytes)):
        for index, player_id in enumerate(starters):
            if index < len(starters_points) and _is_real_player(player_id):
                points.setdefault(player_id, _as_float(starters_points[index]))
    return points


# --------------------------------------------------------------------------
# Mis-start detection
# --------------------------------------------------------------------------


def _mis_starts(
    descriptors: Sequence[tuple[int, str, frozenset[str]]],
    actual_starters: Sequence[str],
    pool: Sequence[tuple[str, frozenset[str], float]],
    players_points: Mapping[str, float],
    player_lookup: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Return this week's "started X over higher-scoring benched Y" findings.

    A mis-start is recorded for a starting slot when some player left on the
    bench was *eligible for that slot* and outscored whoever actually filled it.
    Slots are walked most-restrictive-first and each benched player is charged to
    at most one slot, so a single bench stud cannot be counted three times.

    ``actual_starters`` is index-aligned with ``descriptors`` — that is Sleeper's
    own convention for the ``starters`` array — and any excess on either side is
    ignored.
    """
    started_ids = {p for p in actual_starters if _is_real_player(p)}
    bench = [row for row in pool if row[0] not in started_ids]
    charged: set[str] = set()
    findings: list[dict[str, Any]] = []

    for index, slot, eligible in sorted(descriptors, key=lambda d: (len(d[2]), d[0])):
        if index >= len(actual_starters):
            continue
        started_id = actual_starters[index]
        started_points = (
            _points_of(players_points, started_id) if _is_real_player(started_id) else 0.0
        )
        best: tuple[str, float] | None = None
        for player_id, positions, points in bench:
            if player_id in charged or not (positions & eligible):
                continue
            if points <= started_points:
                continue
            if best is None or points > best[1]:
                best = (player_id, points)
        if best is None:
            continue
        charged.add(best[0])
        findings.append(
            {
                "slot": slot,
                "slot_index": index,
                "started_player_id": started_id if _is_real_player(started_id) else None,
                "started_name": _player_name(started_id, player_lookup),
                "started_position": _primary_position(started_id, player_lookup),
                "started_points": _round(started_points),
                "benched_player_id": best[0],
                "benched_name": _player_name(best[0], player_lookup),
                "benched_position": _primary_position(best[0], player_lookup),
                "benched_points": _round(best[1]),
                "points_lost": _round(best[1] - started_points),
            }
        )

    findings.sort(key=lambda f: (-f["points_lost"], f["slot_index"]))
    return findings


def _mis_start_patterns(
    by_slot: Mapping[str, dict[str, Any]],
    offenders: Sequence[Mapping[str, Any]],
    largest: Mapping[str, Any] | None,
) -> list[str]:
    """Render the repeated mis-start patterns as narration-ready sentences.

    Feeds :attr:`api.schemas.ManagerReview.mis_start_patterns` verbatim — every
    number in these strings is computed, so the LLM may quote them as-is.
    """
    patterns: list[str] = []
    for slot, stats in sorted(
        by_slot.items(), key=lambda kv: (-kv[1]["count"], -kv[1]["points_lost"], kv[0])
    ):
        if stats["count"] < 2:
            continue
        patterns.append(
            f"{stats['count']} mis-starts in the {slot} slot, costing {stats['points_lost']} pts"
        )
    for offender in offenders:
        if offender["times_benched"] < 2:
            continue
        name = offender["name"] or offender["player_id"]
        patterns.append(
            f"Benched {name} ({offender['position'] or 'unknown'}) "
            f"{offender['times_benched']} times for {offender['points_lost']} pts"
        )
    if not patterns and largest:
        name = largest["benched_name"] or largest["benched_player_id"]
        started = largest["started_name"] or largest["started_player_id"] or "an empty slot"
        patterns.append(
            f"Biggest single mis-start: week {largest['week']} {largest['slot']} — "
            f"started {started} ({largest['started_points']}) over {name} "
            f"({largest['benched_points']}), -{largest['points_lost']} pts"
        )
    return patterns


# --------------------------------------------------------------------------
# Weekly review
# --------------------------------------------------------------------------


def weekly_review(
    matchups_by_week: Mapping[Any, Any] | None,
    my_roster_id: int,
    league: Mapping[str, Any] | None,
    player_lookup: Mapping[str, Any],
) -> dict[str, Any]:
    """Per-week lineup efficiency plus season aggregates for one team.

    Args:
        matchups_by_week: ``{week: matchup entries}``. Weeks with no entries, and
            weeks where this roster does not appear, are skipped.
        my_roster_id: The roster being reviewed.
        league: The league object; only ``roster_positions`` is read.
        player_lookup: ``{player_id: {...}}`` position/name lookup.

    Returns:
        ``{"weeks": [...], "season": {...}, "warnings": [...]}``.

        Each ``weeks`` entry carries ``week``, ``actual_points``,
        ``optimal_points``, ``bench_points_lost``, ``efficiency_pct``,
        ``reported_points`` (Sleeper's own total, for cross-checking),
        ``actual_starters``, ``optimal_starters``, ``optimal_by_slot`` and
        ``mis_starts``.

        ``season`` carries ``weeks_analyzed``, ``total_actual_points``,
        ``total_optimal_points``, ``total_bench_points_lost``,
        ``avg_efficiency_pct`` (the season's actual/optimal ratio — points-
        weighted, not a mean of weekly percentages), ``mean_weekly_efficiency_pct``,
        ``worst_week``, ``best_week``, ``mis_start_count``, ``mis_starts_by_slot``,
        ``mis_starts_by_position``, ``top_offenders`` and ``patterns``.
    """
    roster_positions = (league or {}).get("roster_positions") or []
    descriptors, slot_warnings = _slot_descriptors(roster_positions)
    warnings: list[str] = list(dict.fromkeys(slot_warnings))

    weeks_out: list[dict[str, Any]] = []
    all_mis_starts: list[dict[str, Any]] = []

    for week, entries in _iter_weeks(matchups_by_week):
        entry = _entry_for(entries, my_roster_id)
        if entry is None:
            continue
        points_map = _players_points(entry)
        roster_player_ids = _week_roster_players(entry)
        actual_starters = [_norm_id(s) for s in entry.get("starters") or []]

        optimal = optimal_lineup(roster_player_ids, points_map, roster_positions, player_lookup)
        for token in optimal["warnings"]:
            if token not in warnings:
                warnings.append(token)

        efficiency = lineup_efficiency(actual_starters, points_map, optimal)
        if _as_float(optimal["points"]) < efficiency["actual_points"] - 0.01:
            warnings.append(f"optimal_below_actual:w{week}")

        reported = entry.get("points")
        if reported is not None and abs(_as_float(reported) - efficiency["actual_points"]) > 0.5:
            warnings.append(f"points_mismatch:w{week}")

        if len(actual_starters) != len(descriptors):
            warnings.append(f"starter_slot_count_mismatch:w{week}")

        mis_starts = _mis_starts(
            descriptors,
            actual_starters,
            _candidate_pool(roster_player_ids, points_map, player_lookup)[0],
            points_map,
            player_lookup,
        )
        for finding in mis_starts:
            all_mis_starts.append({**finding, "week": week})

        weeks_out.append(
            {
                "week": week,
                **efficiency,
                "reported_points": None if reported is None else _round(_as_float(reported)),
                "actual_starters": [s for s in actual_starters if _is_real_player(s)],
                "optimal_starters": optimal["starters"],
                "optimal_by_slot": optimal["by_slot"],
                "mis_starts": mis_starts,
            }
        )

    season = _season_efficiency(weeks_out, all_mis_starts)
    if not weeks_out:
        warnings.append("no_matchup_history")
    return {"weeks": weeks_out, "season": season, "warnings": warnings}


def _season_efficiency(
    weeks_out: Sequence[Mapping[str, Any]], all_mis_starts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Aggregate per-week efficiency rows into the season block."""
    total_actual = sum(_as_float(w["actual_points"]) for w in weeks_out)
    total_optimal = sum(_as_float(w["optimal_points"]) for w in weeks_out)
    total_lost = sum(_as_float(w["bench_points_lost"]) for w in weeks_out)

    if not weeks_out:
        avg_efficiency: float | None = None
        mean_weekly: float | None = None
        worst: dict[str, Any] | None = None
        best: dict[str, Any] | None = None
    else:
        avg_efficiency = (
            100.0 if total_optimal <= 0 else min(100.0, total_actual / total_optimal * 100.0)
        )
        mean_weekly = statistics.fmean(_as_float(w["efficiency_pct"]) for w in weeks_out)
        ranked = sorted(weeks_out, key=lambda w: (_as_float(w["efficiency_pct"]), -w["week"]))
        worst = _week_summary(ranked[0])
        best = _week_summary(ranked[-1])

    by_slot: dict[str, dict[str, Any]] = {}
    by_position: dict[str, dict[str, Any]] = {}
    offenders: dict[str, dict[str, Any]] = {}
    for finding in all_mis_starts:
        slot_stats = by_slot.setdefault(finding["slot"], {"count": 0, "points_lost": 0.0})
        slot_stats["count"] += 1
        slot_stats["points_lost"] = _round(slot_stats["points_lost"] + finding["points_lost"])

        position = finding["benched_position"] or "UNKNOWN"
        position_stats = by_position.setdefault(position, {"count": 0, "points_lost": 0.0})
        position_stats["count"] += 1
        position_stats["points_lost"] = _round(
            position_stats["points_lost"] + finding["points_lost"]
        )

        offender = offenders.setdefault(
            finding["benched_player_id"],
            {
                "player_id": finding["benched_player_id"],
                "name": finding["benched_name"],
                "position": finding["benched_position"],
                "times_benched": 0,
                "points_lost": 0.0,
                "weeks": [],
            },
        )
        offender["times_benched"] += 1
        offender["points_lost"] = _round(offender["points_lost"] + finding["points_lost"])
        offender["weeks"].append(finding["week"])

    ranked_offenders = sorted(
        offenders.values(),
        key=lambda o: (-o["times_benched"], -o["points_lost"], o["player_id"]),
    )
    largest = max(all_mis_starts, key=lambda f: f["points_lost"], default=None)

    return {
        "weeks_analyzed": [w["week"] for w in weeks_out],
        "weeks_count": len(weeks_out),
        "total_actual_points": _round(total_actual),
        "total_optimal_points": _round(total_optimal),
        "total_bench_points_lost": _round(total_lost),
        "avg_efficiency_pct": None if avg_efficiency is None else _round(avg_efficiency),
        "mean_weekly_efficiency_pct": None if mean_weekly is None else _round(mean_weekly),
        "worst_week": worst,
        "best_week": best,
        "mis_start_count": len(all_mis_starts),
        "mis_starts_by_slot": by_slot,
        "mis_starts_by_position": by_position,
        "top_offenders": ranked_offenders[:3],
        "largest_mis_start": dict(largest) if largest else None,
        "patterns": _mis_start_patterns(by_slot, ranked_offenders, largest),
    }


def _week_summary(week_row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "week": week_row["week"],
        "efficiency_pct": week_row["efficiency_pct"],
        "bench_points_lost": week_row["bench_points_lost"],
        "actual_points": week_row["actual_points"],
        "optimal_points": week_row["optimal_points"],
    }


# --------------------------------------------------------------------------
# Luck
# --------------------------------------------------------------------------


def luck_analysis(matchups_by_week: Mapping[Any, Any] | None, my_roster_id: int) -> dict[str, Any]:
    """Separate scoring from schedule: all-play record, median record, luck score.

    For every week this computes the team's points against the league median, its
    would-be record against *every* roster ("all-play"), and its actual head-to-
    head result. Over the season, ``expected_wins`` is the all-play win rate
    applied to the games actually played — the wins a neutral schedule would have
    produced — and:

    .. code-block:: text

        luck_score = actual_wins - expected_wins

    **Sign convention: positive means lucky** (won more than the scoring
    deserved), negative means unlucky. ``luck_label`` reports ``"neutral"``
    inside ±:data:`LUCK_NEUTRAL_BAND` wins.

    ``points_against_percentile`` is the share of *other* rosters whose season
    points-against is strictly lower than this team's, as 0–100. **High = faced
    the toughest schedule** (unlucky); 100 means nobody in the league was shot at
    harder. ``None`` in a one-team league.

    Args:
        matchups_by_week: ``{week: matchup entries}``.
        my_roster_id: The roster being analysed.

    Returns:
        ``{"weeks": [...], "season": {...}, "warnings": [...]}``. Weeks where
        every roster scored zero are treated as unplayed and skipped with an
        ``unplayed_week:`` warning.
    """
    warnings: list[str] = []
    weeks_out: list[dict[str, Any]] = []
    points_against_totals: dict[int, float] = {}
    points_for_totals: dict[int, float] = {}

    for week, entries in _iter_weeks(matchups_by_week):
        scores: list[tuple[int, float]] = []
        seen: set[int] = set()
        for entry in entries:
            roster_id = _as_int(entry.get("roster_id"))
            if roster_id is None or roster_id in seen:
                continue
            seen.add(roster_id)
            scores.append((roster_id, _as_float(entry.get("points"))))
        if not scores:
            continue
        if all(points == 0.0 for _, points in scores):
            warnings.append(f"unplayed_week:w{week}")
            continue

        # Opponent mapping for every roster, so league-wide points-against works.
        by_matchup: dict[Any, list[int]] = {}
        for entry in entries:
            roster_id = _as_int(entry.get("roster_id"))
            matchup_id = entry.get("matchup_id")
            if roster_id is None or matchup_id is None:
                continue
            by_matchup.setdefault(matchup_id, []).append(roster_id)
        opponent_of: dict[int, int] = {}
        for roster_ids in by_matchup.values():
            if len(roster_ids) == 2:
                opponent_of[roster_ids[0]] = roster_ids[1]
                opponent_of[roster_ids[1]] = roster_ids[0]
        score_of = dict(scores)
        for roster_id, points in scores:
            points_for_totals[roster_id] = points_for_totals.get(roster_id, 0.0) + points
            opponent = opponent_of.get(roster_id)
            if opponent is not None:
                points_against_totals[roster_id] = points_against_totals.get(
                    roster_id, 0.0
                ) + score_of.get(opponent, 0.0)

        if my_roster_id not in score_of:
            continue
        my_points = score_of[my_roster_id]
        median = statistics.median(points for _, points in scores)

        all_play_wins = sum(1 for rid, pts in scores if rid != my_roster_id and my_points > pts)
        all_play_losses = sum(1 for rid, pts in scores if rid != my_roster_id and my_points < pts)
        all_play_ties = sum(1 for rid, pts in scores if rid != my_roster_id and my_points == pts)

        opponent = opponent_of.get(my_roster_id)
        if opponent is None:
            result: str | None = None
            points_against: float | None = None
            warnings.append(f"no_opponent:w{week}")
        else:
            points_against = score_of.get(opponent, 0.0)
            if my_points > points_against:
                result = "W"
            elif my_points < points_against:
                result = "L"
            else:
                result = "T"

        weeks_out.append(
            {
                "week": week,
                "points": _round(my_points),
                "league_median": _round(median),
                "median_delta": _round(my_points - median),
                "above_median": my_points > median,
                "opponent_roster_id": opponent,
                "points_against": None if points_against is None else _round(points_against),
                "result": result,
                "all_play_wins": all_play_wins,
                "all_play_losses": all_play_losses,
                "all_play_ties": all_play_ties,
                "all_play_win_pct": _round(
                    100.0
                    * (all_play_wins + 0.5 * all_play_ties)
                    / max(1, all_play_wins + all_play_losses + all_play_ties)
                ),
            }
        )

    season = _luck_season(weeks_out, my_roster_id, points_against_totals, points_for_totals)
    if not weeks_out:
        warnings.append("no_matchup_history")
    return {"weeks": weeks_out, "season": season, "warnings": warnings}


def _luck_season(
    weeks_out: Sequence[Mapping[str, Any]],
    my_roster_id: int,
    points_against_totals: Mapping[int, float],
    points_for_totals: Mapping[int, float],
) -> dict[str, Any]:
    """Roll weekly luck rows into the season block."""
    played = [w for w in weeks_out if w["result"] is not None]
    actual_wins = sum(1 for w in played if w["result"] == "W")
    actual_losses = sum(1 for w in played if w["result"] == "L")
    actual_ties = sum(1 for w in played if w["result"] == "T")

    all_play_wins = sum(w["all_play_wins"] for w in weeks_out)
    all_play_losses = sum(w["all_play_losses"] for w in weeks_out)
    all_play_ties = sum(w["all_play_ties"] for w in weeks_out)

    # Expected wins = the all-play win rate applied to the games actually played.
    expected_wins: float | None
    if played:
        rates = [
            (w["all_play_wins"] + 0.5 * w["all_play_ties"])
            / max(1, w["all_play_wins"] + w["all_play_losses"] + w["all_play_ties"])
            for w in played
        ]
        expected_wins = _round(sum(rates))
    else:
        expected_wins = None

    luck_score = None if expected_wins is None else _round(actual_wins - expected_wins)
    if luck_score is None:
        luck_label = "unknown"
    elif luck_score > LUCK_NEUTRAL_BAND:
        luck_label = "lucky"
    elif luck_score < -LUCK_NEUTRAL_BAND:
        luck_label = "unlucky"
    else:
        luck_label = "neutral"

    my_points_against = points_against_totals.get(my_roster_id)
    others = [v for k, v in points_against_totals.items() if k != my_roster_id]
    if my_points_against is None or not others:
        percentile: float | None = None
    else:
        percentile = _round(
            100.0 * sum(1 for value in others if value < my_points_against) / len(others)
        )

    weeks_above_median = sum(1 for w in weeks_out if w["above_median"])
    return {
        "weeks_analyzed": [w["week"] for w in weeks_out],
        "actual_wins": actual_wins,
        "actual_losses": actual_losses,
        "actual_ties": actual_ties,
        "actual_record": f"{actual_wins}-{actual_losses}-{actual_ties}",
        "all_play_wins": all_play_wins,
        "all_play_losses": all_play_losses,
        "all_play_ties": all_play_ties,
        "all_play_record": f"{all_play_wins}-{all_play_losses}-{all_play_ties}",
        "expected_wins": expected_wins,
        "luck_score": luck_score,
        "luck_label": luck_label,
        "points_for": _round(points_for_totals.get(my_roster_id, 0.0)),
        "points_against": None if my_points_against is None else _round(my_points_against),
        "points_against_percentile": percentile,
        "weeks_above_median": weeks_above_median,
        "weeks_below_median": len(weeks_out) - weeks_above_median,
        "median_record": f"{weeks_above_median}-{len(weeks_out) - weeks_above_median}",
        "avg_points": _round(statistics.fmean([w["points"] for w in weeks_out]))
        if weeks_out
        else None,
        "avg_points_against": _round(
            statistics.fmean(
                [w["points_against"] for w in played if w["points_against"] is not None]
            )
        )
        if any(w["points_against"] is not None for w in played)
        else None,
    }


# --------------------------------------------------------------------------
# League comparison
# --------------------------------------------------------------------------


def league_comparison(
    rosters: Sequence[Mapping[str, Any]] | None,
    matchups_by_week: Mapping[Any, Any] | None,
    my_roster_id: int,
    player_lookup: Mapping[str, Any],
    roster_positions: Sequence[Any] | None,
) -> dict[str, Any]:
    """Rank this team against its actual leaguemates.

    For every roster this computes season actual/optimal points, bench points
    lost, lineup efficiency, and average weekly **starter** points by position
    group. Position groups are the concrete positions the league actually starts
    (derived from ``roster_positions``), and a started player is credited to
    *their own* position — a RB started at FLEX counts as RB, which is what
    "positional strength" means to a manager.

    Grading uses the z-score of this team's points-per-week against the league's
    population mean and standard deviation for that position, mapped through
    :func:`grade_from_z` / :data:`GRADE_BANDS`. Population (not sample) statistics
    are correct here because the league *is* the entire population.

    Deficiencies are the position groups where this team ranks in the bottom
    third (``rank > league_size * 2 / 3``). Severity is ``high`` at z ≤ -1.0 or
    last place, ``medium`` at z ≤ -0.5, else ``low``. ``available_fixes`` is left
    empty — the agents wave fills it from :func:`free_agent_pool`.

    Args:
        rosters: ``GET /league/{id}/rosters`` payload; used for identity and the
            league's own W/L record. Teams appearing only in matchups are still
            included.
        matchups_by_week: ``{week: matchup entries}``.
        my_roster_id: The roster being analysed.
        player_lookup: ``{player_id: {...}}`` position/name lookup.
        roster_positions: The league's ``roster_positions`` array.

    Returns:
        ``{"league_size", "position_groups", "teams", "my_efficiency_rank",
        "positional_strength", "deficiencies", "warnings"}``. Each
        ``positional_strength`` item is shaped to construct
        :class:`api.schemas.PositionalStrength` directly.
    """
    descriptors, warnings = _slot_descriptors(roster_positions)
    warnings = list(dict.fromkeys(warnings))
    position_groups = sorted({p for _, _, eligible in descriptors for p in eligible})

    teams: dict[int, dict[str, Any]] = {}
    for roster in rosters or []:
        roster_id = _as_int(roster.get("roster_id"))
        if roster_id is None:
            warnings.append("roster_without_id")
            continue
        settings = roster.get("settings") or {}
        teams[roster_id] = _blank_team(roster_id, position_groups)
        teams[roster_id].update(
            {
                "owner_id": roster.get("owner_id"),
                "co_owners": list(roster.get("co_owners") or []),
                "wins": _as_int(settings.get("wins")),
                "losses": _as_int(settings.get("losses")),
                "ties": _as_int(settings.get("ties")) or 0,
                "points_for": _round(
                    _as_float(settings.get("fpts"))
                    + _as_float(settings.get("fpts_decimal")) / 100.0
                ),
                "points_against": _round(
                    _as_float(settings.get("fpts_against"))
                    + _as_float(settings.get("fpts_against_decimal")) / 100.0
                ),
            }
        )
        if roster.get("owner_id") is None:
            warnings.append(f"roster_without_owner:{roster_id}")

    for _week, entries in _iter_weeks(matchups_by_week):
        for entry in entries:
            roster_id = _as_int(entry.get("roster_id"))
            if roster_id is None:
                continue
            team = teams.setdefault(roster_id, _blank_team(roster_id, position_groups))
            points_map = _players_points(entry)
            roster_player_ids = _week_roster_players(entry)
            actual_starters = [_norm_id(s) for s in entry.get("starters") or []]
            optimal = optimal_lineup(roster_player_ids, points_map, roster_positions, player_lookup)
            efficiency = lineup_efficiency(actual_starters, points_map, optimal)

            team["weeks_analyzed"] += 1
            team["actual_points"] += efficiency["actual_points"]
            team["optimal_points"] += efficiency["optimal_points"]
            team["bench_points_lost"] += efficiency["bench_points_lost"]
            for player_id in actual_starters:
                if not _is_real_player(player_id):
                    continue
                position = _primary_position(player_id, player_lookup)
                if position is None or position not in team["points_by_position"]:
                    continue
                team["points_by_position"][position] += _points_of(points_map, player_id)

    for team in teams.values():
        weeks = max(1, team["weeks_analyzed"])
        team["actual_points"] = _round(team["actual_points"])
        team["optimal_points"] = _round(team["optimal_points"])
        team["bench_points_lost"] = _round(team["bench_points_lost"])
        team["efficiency_pct"] = (
            100.0
            if team["optimal_points"] <= 0
            else _round(min(100.0, team["actual_points"] / team["optimal_points"] * 100.0))
        )
        team["points_per_week_by_position"] = {
            position: _round(total / weeks)
            for position, total in team["points_by_position"].items()
        }
        team["points_by_position"] = {
            position: _round(total) for position, total in team["points_by_position"].items()
        }
        team["is_me"] = team["roster_id"] == my_roster_id

    ordered_teams = [teams[key] for key in sorted(teams)]
    _rank_desc(ordered_teams, "efficiency_pct", "efficiency_rank")
    _rank_desc(ordered_teams, "actual_points", "points_rank")

    league_size = len(ordered_teams)
    me = teams.get(my_roster_id)
    if me is None:
        warnings.append("my_roster_not_in_league")

    strength = _positional_strength(ordered_teams, me, position_groups, league_size)
    deficiencies = _deficiencies(strength, league_size)

    return {
        "league_size": league_size,
        "position_groups": position_groups,
        "teams": ordered_teams,
        "my_efficiency_rank": me["efficiency_rank"] if me else None,
        "my_points_rank": me["points_rank"] if me else None,
        "positional_strength": strength,
        "deficiencies": deficiencies,
        "warnings": warnings,
    }


def _blank_team(roster_id: int, position_groups: Sequence[str]) -> dict[str, Any]:
    return {
        "roster_id": roster_id,
        "owner_id": None,
        "co_owners": [],
        "team_name": None,
        "is_me": False,
        "weeks_analyzed": 0,
        "actual_points": 0.0,
        "optimal_points": 0.0,
        "bench_points_lost": 0.0,
        "efficiency_pct": 100.0,
        "points_by_position": dict.fromkeys(position_groups, 0.0),
        "points_per_week_by_position": {},
        "wins": None,
        "losses": None,
        "ties": 0,
        "points_for": None,
        "points_against": None,
    }


def _rank_desc(rows: Sequence[dict[str, Any]], field: str, target: str) -> None:
    """Assign 1-based competition ranks (ties share a rank), highest value first."""
    ordered = sorted(rows, key=lambda r: -_as_float(r.get(field)))
    previous_value: float | None = None
    previous_rank = 0
    for index, row in enumerate(ordered, start=1):
        value = _as_float(row.get(field))
        if previous_value is not None and value == previous_value:
            row[target] = previous_rank
        else:
            row[target] = index
            previous_rank = index
            previous_value = value


def _positional_strength(
    teams: Sequence[Mapping[str, Any]],
    me: Mapping[str, Any] | None,
    position_groups: Sequence[str],
    league_size: int,
) -> list[dict[str, Any]]:
    """Grade my position groups against the league. Shaped for PositionalStrength."""
    if me is None:
        return []
    out: list[dict[str, Any]] = []
    for position in position_groups:
        values = [
            _as_float((team.get("points_per_week_by_position") or {}).get(position))
            for team in teams
        ]
        mine = _as_float((me.get("points_per_week_by_position") or {}).get(position))
        mean = statistics.fmean(values) if values else 0.0
        stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
        z_score = 0.0 if stdev == 0 else (mine - mean) / stdev
        better = sum(1 for value in values if value > mine)
        out.append(
            {
                # --- api.schemas.PositionalStrength fields ---
                "position": position,
                "league_rank": better + 1,
                "league_size": league_size,
                "points_per_week": _round(mine),
                "league_avg_points_per_week": _round(mean),
                "grade": grade_from_z(z_score),
                # --- extras (ignored by the pydantic model, useful to narrate) ---
                "z_score": _round(z_score),
                "league_stdev": _round(stdev),
                "league_best_points_per_week": _round(max(values)) if values else 0.0,
                "league_worst_points_per_week": _round(min(values)) if values else 0.0,
            }
        )
    return out


def _deficiencies(strength: Sequence[Mapping[str, Any]], league_size: int) -> list[dict[str, Any]]:
    """Return bottom-third position groups, shaped for api.schemas.Deficiency."""
    if league_size <= 0:
        return []
    cutoff = league_size * 2 / 3
    out: list[dict[str, Any]] = []
    for row in strength:
        if row["league_rank"] <= cutoff:
            continue
        z_score = _as_float(row["z_score"])
        if z_score <= -1.0 or row["league_rank"] == league_size:
            severity = "high"
        elif z_score <= -0.5:
            severity = "medium"
        else:
            severity = "low"
        out.append(
            {
                "position": row["position"],
                "severity": severity,
                "detail": (
                    f"{row['position']} is producing {row['points_per_week']} pts/week, "
                    f"rank {row['league_rank']} of {row['league_size']} "
                    f"(league average {row['league_avg_points_per_week']}, "
                    f"z={row['z_score']}, grade {row['grade']})."
                ),
                "available_fixes": [],
                # extras for the agents wave
                "league_rank": row["league_rank"],
                "z_score": row["z_score"],
                "grade": row["grade"],
                "points_per_week": row["points_per_week"],
                "league_avg_points_per_week": row["league_avg_points_per_week"],
            }
        )
    out.sort(key=lambda d: (_as_float(d["z_score"]), d["position"]))
    return out


# --------------------------------------------------------------------------
# Free-agent pool
# --------------------------------------------------------------------------


def free_agent_pool(
    all_rosters: Sequence[Mapping[str, Any]] | None,
    player_universe_ids: Iterable[Any] | None,
) -> set[str]:
    """Return the player ids in the universe that no roster in this league holds.

    PRD §4.2 / tech spec §4.1: ``/v1/team-report`` deficiency fixes may **only**
    name players from this pool — recommending a player who is already rostered
    in the manager's league is the fastest way to look useless.

    Args:
        all_rosters: Every roster in the league. Both ``players`` and ``starters``
            are subtracted (``starters`` should be a subset, but Sleeper has been
            seen to carry a starter that is absent from ``players``).
        player_universe_ids: Candidate ids — typically the ingested ``players``
            collection, or a pre-filtered shortlist.

    Returns:
        A set of player id strings. Empty-slot markers are never included.
    """
    rostered: set[str] = set()
    for roster in all_rosters or []:
        if not isinstance(roster, Mapping):
            continue
        for key in ("players", "starters", "reserve", "taxi"):
            for raw in roster.get(key) or []:
                player_id = _norm_id(raw)
                if _is_real_player(player_id):
                    rostered.add(player_id)
    universe = {
        _norm_id(raw) for raw in player_universe_ids or [] if _is_real_player(_norm_id(raw))
    }
    return universe - rostered


# --------------------------------------------------------------------------
# Team labelling
# --------------------------------------------------------------------------


def _user_index(users: Sequence[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for user in users or []:
        if not isinstance(user, Mapping):
            continue
        user_id = _norm_id(user.get("user_id"))
        if not user_id:
            continue
        metadata = user.get("metadata") or {}
        index[user_id] = {
            "user_id": user_id,
            "display_name": user.get("display_name"),
            "team_name": metadata.get("team_name") if isinstance(metadata, Mapping) else None,
        }
    return index


def _label_team(
    roster_id: int,
    owner_id: Any,
    co_owners: Sequence[Any],
    users: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve a display label for a roster.

    Handles the two messy realities: an **orphan roster** (``owner_id is None``,
    common after a mid-season abandonment) falls back to ``"Team {roster_id}"``,
    and a **co-owned team** lists every co-owner's display name so the narration
    can address the right humans.
    """
    owner = users.get(_norm_id(owner_id)) if owner_id is not None else None
    co_owner_names = [
        name
        for name in ((users.get(_norm_id(co)) or {}).get("display_name") for co in co_owners or [])
        if name
    ]
    team_name = (owner or {}).get("team_name") or (owner or {}).get("display_name")
    return {
        "roster_id": roster_id,
        "owner_id": _norm_id(owner_id) or None,
        "display_name": (owner or {}).get("display_name"),
        "team_name": team_name or f"Team {roster_id}",
        "co_owner_names": co_owner_names,
        "is_orphan": owner is None,
    }


#: Starters ranked outside this many players contribute nothing to a lineup's
#: market score. Sleeper's ``search_rank`` has a very long tail, and the gap
#: between the 600th and the 900th most-searched player is noise rather than
#: lineup strength — summing it unweighted would let a deep bench of nobodies
#: outscore a good starting eleven.
MARKET_POOL = 400


def _market_points(rank: Any) -> float:
    """Score one starter from their market rank. Higher is better."""
    value = _as_int(rank)
    if value is None or value < 1:
        return 0.0
    return float(max(0, MARKET_POOL - value))


def _lineup_market_score(
    entry: Mapping[str, Any] | None, market_ranks: Mapping[str, Any]
) -> tuple[float, int, int]:
    """Total market score of a matchup entry's starters.

    Returns ``(score, ranked_starters, total_starters)`` so the caller can say
    how much of the lineup the number actually covers.
    """
    starters = [_norm_id(s) for s in (entry or {}).get("starters") or []]
    real = [s for s in starters if _is_real_player(s)]
    score = 0.0
    ranked = 0
    for player_id in real:
        points = _market_points(market_ranks.get(player_id))
        if points > 0:
            ranked += 1
        score += points
    return _round(score), ranked, len(real)


def _lean_from_share(share: float) -> str:
    """Turn a share of combined market score into a plain-language lean."""
    if share >= 0.56:
        return "clear edge"
    if share >= 0.52:
        return "slight edge"
    if share > 0.48:
        return "toss-up"
    if share > 0.44:
        return "slight underdog"
    return "clear underdog"


#: What a zeroed manager review means before kickoff, in words. The numbers are
#: structurally zero and a reader cannot tell that from "played badly", so the
#: distinction has to be carried by text that travels with them.
UNPLAYED_LUCK_NOTE = "No games have been played yet, so there is nothing to call lucky or unlucky."

UNPLAYED_OBSERVATION = (
    "Every played-game number here is zero because the season has not kicked off. "
    "That means 'not yet played', not 'played badly' — the preseason outlook is what "
    "can honestly be assessed today."
)


def mark_unplayed(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Strip metrics that describe games nobody has played.

    Called by the route *before* the facts reach any engine, which is the point:
    the ADK synthesis agent is instructed to reproduce ``manager_review`` and
    ``positional_strength_vs_league`` exactly, so handing it a "B-" computed from
    0.0 points per week across 0 games makes faithful narration produce a
    confident lie. The deterministic engine guards this itself, but a guard that
    lives in one of two engines is not a guard.

    Positional grades go entirely: a letter derived from no games is not a weak
    assessment, it is no assessment, and it reads as the former. The review keeps
    its zeroed shape — the response contract requires it — and carries the words
    that say what those zeros mean.

    A no-op unless ``warnings`` carries ``no_matchup_history``.
    """
    warnings = {str(w) for w in (facts.get("warnings") or [])}
    if "no_matchup_history" not in warnings:
        return dict(facts)
    review = dict(facts.get("manager_review") or {})
    review["luck_note"] = UNPLAYED_LUCK_NOTE
    existing = [o for o in (review.get("observations") or []) if o != UNPLAYED_OBSERVATION]
    review["observations"] = [UNPLAYED_OBSERVATION, *existing]
    return {**dict(facts), "positional_strength_vs_league": [], "manager_review": review}


def week_one_outlook(
    *,
    matchups: Sequence[Mapping[str, Any]] | None,
    my_roster_id: int,
    rosters: Sequence[Mapping[str, Any]] | None = None,
    users: Sequence[Mapping[str, Any]] | None = None,
    market_ranks: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Compare two set lineups for a game that has not been played.

    This exists because week 1 is the one week where every *historical* team
    metric is legitimately empty: no games have been played, so lineup
    efficiency, luck and leaguemate ranking have nothing to describe. What does
    exist is the schedule and two set lineups, and those support one honest
    statement — which roster the market rates more highly.

    **The market signal is Sleeper's ``search_rank``: draft popularity, not a
    consensus ADP and not a projection.** It is the only forward-looking number
    in the store before kickoff, so the comparison is reported as a *lean* with
    both totals shown, never as a win probability. Inventing a percentage here
    would be exactly the failure this whole block exists to avoid.

    Returns ``None`` when there is no opponent to compare against — an unplayed
    bye, a malformed schedule, or a roster missing from the week's entries.
    """
    market_ranks = market_ranks or {}
    entries = [m for m in (matchups or []) if isinstance(m, Mapping)]
    mine = _entry_for(entries, my_roster_id)
    if mine is None:
        return None

    matchup_id = mine.get("matchup_id")
    if matchup_id is None:
        return None
    opponent = next(
        (
            e
            for e in entries
            if e.get("matchup_id") == matchup_id and _as_int(e.get("roster_id")) != my_roster_id
        ),
        None,
    )
    if opponent is None:
        return None

    opponent_roster_id = _as_int(opponent.get("roster_id"))
    my_score, my_ranked, my_total = _lineup_market_score(mine, market_ranks)
    their_score, their_ranked, their_total = _lineup_market_score(opponent, market_ranks)

    warnings: list[str] = []
    combined = my_score + their_score
    if combined <= 0:
        warnings.append("no_market_signal")
        lean = "toss-up"
        share = 0.5
    else:
        share = my_score / combined
        lean = _lean_from_share(share)
    if my_ranked < my_total or their_ranked < their_total:
        warnings.append("partial_market_coverage")

    user_index = _user_index(users)
    opponent_label = None
    for roster in rosters or []:
        if not isinstance(roster, Mapping):
            continue
        if _as_int(roster.get("roster_id")) != opponent_roster_id:
            continue
        opponent_label = _label_team(
            opponent_roster_id or 0,
            roster.get("owner_id"),
            roster.get("co_owners") or [],
            user_index,
        )
        break

    return {
        "opponent_roster_id": opponent_roster_id,
        "opponent_team_name": (opponent_label or {}).get("team_name")
        or (f"Team {opponent_roster_id}" if opponent_roster_id else None),
        "my_market_score": my_score,
        "opponent_market_score": their_score,
        "my_share": _round(share, 3),
        "my_starters_scored": f"{my_ranked} of {my_total}",
        "opponent_starters_scored": f"{their_ranked} of {their_total}",
        "lean": lean,
        "basis": (
            "Set lineups compared on Sleeper market signal (search_rank — draft "
            "popularity, not an ADP and not a projection). No games have been "
            "played, so this is a lean, not a win probability."
        ),
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# Assembler
# --------------------------------------------------------------------------


def build_team_report_facts(
    *,
    league: Mapping[str, Any] | None,
    rosters: Sequence[Mapping[str, Any]] | None,
    matchups_by_week: Mapping[Any, Any] | None,
    my_roster_id: int,
    player_lookup: Mapping[str, Any] | None = None,
    users: Sequence[Mapping[str, Any]] | None = None,
    player_universe_ids: Iterable[Any] | None = None,
    season: int | None = None,
    through_week: int | None = None,
    sleeper_username: str | None = None,
) -> dict[str, Any]:
    """Assemble every deterministic number ``POST /v1/team-report`` needs.

    This is the single object wave 3 puts into the engine's ``request_context``
    and the synthesis agent narrates. It is JSON-safe throughout (only ``dict``,
    ``list``, ``str``, ``int``, ``float``, ``bool``, ``None``) and self-describing:
    the ``computed`` block states, in the payload itself, that these numbers are
    Python-computed and must not be altered by the model (tech spec §6).

    Args:
        league: ``GET /league/{id}``.
        rosters: ``GET /league/{id}/rosters``.
        matchups_by_week: ``{week: GET /league/{id}/matchups/{week}}``.
        my_roster_id: The roster the report is about.
        player_lookup: ``{player_id: {"name","position","fantasy_positions"}}``.
        users: ``GET /league/{id}/users``, for team labels. Optional.
        player_universe_ids: Candidate ids for the free-agent pool. Optional —
            omit it and ``free_agent_pool.player_ids`` comes back empty with a
            ``no_player_universe`` warning.
        season: NFL season year, echoed into ``week_range``.
        through_week: Week the report is scoped through. Defaults to the last
            week present in ``matchups_by_week``.
        sleeper_username: Echoed for the response envelope.

    Returns:
        See the module-level "Output contract" table in the tests, and the key
        map below::

            {
              "computed":       {...provenance marker...},
              "week_range":     {season, first_week, last_week, through_week,
                                 weeks_analyzed, weeks_missing, weeks_count},
              "league":         {league_id, name, size, roster_positions,
                                 starting_slots, position_groups, scoring_type},
              "team":           {roster_id, owner_id, team_name, display_name,
                                 co_owner_names, is_orphan, sleeper_username,
                                 wins, losses, ties, points_for, points_against},
              "lineup_efficiency": weekly_review(...),
              "luck":              luck_analysis(...),
              "league_comparison": league_comparison(...),
              "positional_strength_vs_league": [PositionalStrength-shaped],
              "deficiencies":                  [Deficiency-shaped],
              "manager_review":                {ManagerReview-shaped},
              "free_agent_pool": {"count": int, "player_ids": [...]},
              "warnings": [...]
            }
    """
    league = league or {}
    player_lookup = player_lookup or {}
    roster_positions = list(league.get("roster_positions") or [])

    review = weekly_review(matchups_by_week, my_roster_id, league, player_lookup)
    luck = luck_analysis(matchups_by_week, my_roster_id)
    comparison = league_comparison(
        rosters, matchups_by_week, my_roster_id, player_lookup, roster_positions
    )

    warnings: list[str] = []
    for token in [*review["warnings"], *luck["warnings"], *comparison["warnings"]]:
        if token not in warnings:
            warnings.append(token)

    weeks_analyzed = review["season"]["weeks_analyzed"]
    all_weeks = [week for week, _ in _iter_weeks(matchups_by_week)]
    last_week = through_week or (max(all_weeks) if all_weeks else None)
    first_week = min(all_weeks) if all_weeks else None
    weeks_missing = (
        [w for w in range(first_week, last_week + 1) if w not in weeks_analyzed]
        if first_week is not None and last_week is not None
        else []
    )

    user_index = _user_index(users)
    my_roster = next((r for r in rosters or [] if _as_int(r.get("roster_id")) == my_roster_id), {})
    label = _label_team(
        my_roster_id, my_roster.get("owner_id"), my_roster.get("co_owners") or [], user_index
    )
    for team in comparison["teams"]:
        team_label = _label_team(
            team["roster_id"], team.get("owner_id"), team.get("co_owners") or [], user_index
        )
        team["team_name"] = team_label["team_name"]
        team["display_name"] = team_label["display_name"]

    my_team_row = next((t for t in comparison["teams"] if t["is_me"]), None)
    pool = free_agent_pool(rosters, player_universe_ids)
    if player_universe_ids is None:
        warnings.append("no_player_universe")

    facts: dict[str, Any] = {
        "computed": {
            "by": "api.data.team_analytics",
            "method": "deterministic",
            "version": FACTS_VERSION,
            "rule": (
                "Every number in this object was computed in Python from Sleeper matchup "
                "history. Narrate them; do not recompute, round, adjust or invent any value. "
                "Anything not present here is an observation, not a statistic."
            ),
        },
        "week_range": {
            "season": season,
            "first_week": first_week,
            "last_week": last_week,
            "through_week": last_week,
            "weeks_analyzed": weeks_analyzed,
            "weeks_missing": weeks_missing,
            "weeks_count": len(weeks_analyzed),
        },
        "league": {
            "league_id": league.get("league_id"),
            "name": league.get("name"),
            "size": comparison["league_size"],
            "roster_positions": roster_positions,
            "starting_slots": starting_slots(roster_positions),
            "position_groups": comparison["position_groups"],
            "scoring_type": (league.get("settings") or {}).get("scoring_type")
            if isinstance(league.get("settings"), Mapping)
            else None,
        },
        "team": {
            **label,
            "sleeper_username": sleeper_username,
            "wins": (my_team_row or {}).get("wins"),
            "losses": (my_team_row or {}).get("losses"),
            "ties": (my_team_row or {}).get("ties"),
            "points_for": (my_team_row or {}).get("points_for"),
            "points_against": (my_team_row or {}).get("points_against"),
        },
        "lineup_efficiency": review,
        "luck": luck,
        "league_comparison": comparison,
        "positional_strength_vs_league": comparison["positional_strength"],
        "deficiencies": comparison["deficiencies"],
        "free_agent_pool": {"count": len(pool), "player_ids": sorted(pool)},
        "warnings": warnings,
    }
    facts["manager_review"] = _manager_review(review, luck, comparison)
    return facts


def _manager_review(
    review: Mapping[str, Any],
    luck: Mapping[str, Any],
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the :class:`api.schemas.ManagerReview`-shaped block.

    ``bench_points_lost`` and ``optimal_vs_actual`` are the same quantity by
    construction (optimal minus actual); the schema names both, so both are
    emitted rather than leaving one for the model to "derive".

    ``observations`` is deliberately empty: tech spec §6 reserves it for
    non-computable behavioural notes, which the synthesis agent adds and must
    label as observations, not statistics.
    """
    season = review["season"]
    luck_season = luck["season"]
    efficiency = season["avg_efficiency_pct"]
    return {
        "bench_points_lost": season["total_bench_points_lost"],
        "optimal_vs_actual": _round(
            _as_float(season["total_optimal_points"]) - _as_float(season["total_actual_points"])
        ),
        "lineup_efficiency_pct": 0.0 if efficiency is None else efficiency,
        "efficiency_rank": comparison["my_efficiency_rank"] or 0,
        "league_size": comparison["league_size"],
        "luck_note": _luck_note(luck_season),
        "expected_wins": luck_season["expected_wins"],
        "actual_wins": luck_season["actual_wins"],
        "mis_start_patterns": season["patterns"],
        "observations": [],
        # extras beyond the schema, useful to the narrator
        "weeks_analyzed": season["weeks_analyzed"],
        "worst_week": season["worst_week"],
        "best_week": season["best_week"],
        "top_offenders": season["top_offenders"],
        "luck_score": luck_season["luck_score"],
        "luck_label": luck_season["luck_label"],
        "all_play_record": luck_season["all_play_record"],
        "actual_record": luck_season["actual_record"],
        "points_against_percentile": luck_season["points_against_percentile"],
    }


def _luck_note(luck_season: Mapping[str, Any]) -> str:
    """Render the luck block as one computed, quotable sentence."""
    if not luck_season["weeks_analyzed"]:
        return "No completed matchups yet, so there is nothing to call lucky or unlucky."
    parts = [
        f"Record {luck_season['actual_record']} against an all-play record of "
        f"{luck_season['all_play_record']}",
        f"expected wins {luck_season['expected_wins']} vs {luck_season['actual_wins']} actual "
        f"(luck score {luck_season['luck_score']}, {luck_season['luck_label']})",
        f"{luck_season['points_for']} points for and {luck_season['points_against']} against",
    ]
    percentile = luck_season["points_against_percentile"]
    if percentile is not None:
        parts.append(
            f"points-against sits at the {percentile}th percentile of the league "
            "(100 = toughest schedule faced)"
        )
    parts.append(f"above the weekly median in {luck_season['median_record']} weeks")
    return "; ".join(parts) + "."
