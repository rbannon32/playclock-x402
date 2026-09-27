"""Value assertions: is a paid answer worth paying for, not merely honest?

The golden suite's original properties — valid schema, right player, every
cited number traceable — guarantee an answer never *lies*. They say nothing
about whether it is worth the price. The live boards proved the gap on
2026-09-03: a Week 1 report whose "emerging" section listed the three
most-added players in the league, citing their add counts; a sleepers board
padded to twelve picks, ten of which said their usage data was not available;
prose that opened with "Deterministic engine — heuristic analysis"; a
beneficiary named in an injury chain with no player id, because the model
named someone the tools never returned. Every one of those passed the suite.

This module is the second gate. It takes a *plain dict* (``model_dump``) so the
same checks run in three places:

* the golden suite, against the deterministic engine in CI;
* the same suite against the ADK engine before a deploy; and
* :mod:`ingest.precompute`, against every board it warms — the only place
  the ADK output that real payers receive is ever inspected.

The rules
---------
``no_code_artifacts``
    No dict literal, raw field name (``_l4w``), or engine apology in any string.
``names_grounded``
    Every named row carries a non-empty ``player_id`` — and, when the caller
    knows the universe, one that exists. A name without an id is the shape a
    hallucinated player takes. In a body the ADK pipeline wrote, every
    multi-word name in *prose* must also be one the body names (the narrator's
    :func:`~api.agents.narrator.unknown_names`, with the exemptions
    ``numbers_grounded`` uses): a player invented in a reasoning paragraph has
    no row to be caught in.
``confidence_consistent``
    A row's ``confidence`` never exceeds the top-level tier, and a pick whose
    own note says its usage is not available cannot be more than ``low``.
``cites_beyond_the_crowd``
    A board with three or more rows cites at least
    :data:`MIN_NON_CROWD_CITATIONS` numbers that are not add/drop counts.
    A board that cites only ``trend_count`` is Sleeper's free list with prose.
``disagrees_with_the_crowd``
    Trending must fade or hold at least one add (or buy one drop); the waiver
    board's order must differ from add-count order somewhere; sleepers and
    "emerging" must exclude anyone the crowd has already claimed; a short
    sleepers board must say in its reasoning why it is short.
``sources_resolved``
    No opaque grounding-redirect URLs and no bare-domain titles. A reader who
    cannot see where a claim came from cannot check it.
``prose_names_grounded``
    In the same ADK-written bodies, every multi-word name in prose must be a
    name the body carries (a row, or a citation's ``player``).
``numbers_grounded``
    In a body the ADK pipeline wrote (``meta.engine == "adk"``), every number
    in every prose field must be a numeric leaf of the body or a
    ``stats_cited`` value.
    After the second gated warm this was the judge's one remaining critique
    on three boards: snap rates and target shares quoted in row notes that
    never reached ``stats_cited``. It is the narrator's grounding guard
    applied to ADK output; the deterministic engine is exempt because its
    prose legitimately carries numbers it computed (a scoring average) and
    the narrated engine is guarded at edit time.

Each rule returns failure strings; :func:`check_quality` concatenates them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Iterator
from typing import Any

from api.agents.narrator import leaf_numbers, ungrounded_numbers, unknown_names
from api.core.store import Store
from api.data.stats_store import PLAYERS_COLLECTION, get_trending

#: Citations that only restate the crowd. Everything else is analysis.
CROWD_STATS: frozenset[str] = frozenset({"trend_count"})

#: A board with at least this many rows must cite at least this many
#: non-crowd numbers.
MIN_NON_CROWD_CITATIONS = 3
BOARD_MIN_ROWS = 3

#: Endpoints whose bodies are league-wide boards.
BOARD_ENDPOINTS: tuple[str, ...] = ("trending", "sleepers", "waivers", "report")

#: The product asks the sleepers board for this many; fewer is fine if said.
SLEEPERS_TARGET = 8

#: Sleeper add count at which a player is "already claimed" by the crowd.
#: Mirrors ``api.agents.deterministic.CONSENSUS_ADD_COUNT``; kept literal here
#: so the eval does not import the engine it judges.
CONSENSUS_ADD_COUNT = 3000

#: Substrings that mean code leaked into prose.
CODE_ARTIFACTS: tuple[str, ...] = ("{'", "[{", "_l4w", "Deterministic engine", "None)")

#: Row lists that carry a ``name`` a model could have invented, per endpoint.
#: Dotted paths descend through nested lists (``injury_fallout.beneficiaries``
#: visits every beneficiary of every injury).
NAMED_ROWS: dict[str, tuple[str, ...]] = {
    "trending": ("players",),
    "sleepers": ("picks",),
    "player": (),
    "matchup": (),
    "roster": ("waiver_adds", "drop_candidates"),
    "waivers": ("board",),
    "report": (
        "emerging",
        "stock_up",
        "stock_down",
        "rookie_watch",
        "streamers",
        "injury_fallout.beneficiaries",
    ),
    "team_report": ("deficiencies.available_fixes",),
    "draft_board": ("tiers.players", "values", "reaches"),
    "draft_report": ("roster", "best_picks", "worst_picks"),
}

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_NO_USAGE_RE = re.compile(r"not available|no usage|no rollup|not ingested", re.IGNORECASE)
_SHORT_BOARD_RE = re.compile(
    r"fewer|only \d+ (candidate|player|pick)|cleared the bar", re.IGNORECASE
)
_BARE_DOMAIN_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$", re.IGNORECASE)
_REDIRECT_MARKER = "grounding-api-redirect"
_YEAR_RE = re.compile(r"^20\d\d$")

#: Paths whose strings are not our prose, or carry their own numbers.
_NOT_PROSE_PREFIXES = ("meta", "stats_cited", "sources")

#: Per endpoint, the body paths whose strings are copied verbatim from
#: precomputed analytics rather than written by the model.
#:
#: These are grounded by construction, not by citation:
#: :func:`api.agents.pipeline.enforce_computed_facts` overwrites them with the
#: computed block before the body is validated, so the number in
#: ``"Benched the higher-projected TE in two of three weeks (-11.4)."`` is the
#: one :mod:`api.data.team_analytics` computed, whatever the model wrote.
#: Requiring a citation for them made the gate fail a correct body — it did,
#: on every model tested, because a field the prompt says to copy verbatim does
#: not read like a number the model is quoting.
_COMPUTED_PROSE: dict[str, tuple[str, ...]] = {
    "team_report": (
        "manager_review.luck_note",
        "manager_review.mis_start_patterns",
        "positional_strength_vs_league",
    ),
}

#: How many ungrounded numbers to name per body before summarising.
_MAX_NUMBER_FAILURES = 8


# --------------------------------------------------------------------------
# Walking a body
# --------------------------------------------------------------------------


def _strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """Yield ``(path, text)`` for every string leaf under ``value``."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{path}[{index}]")


def _rows(body: dict[str, Any], path: str) -> list[dict[str, Any]]:
    """Resolve a dotted path to the dict rows it names, descending through lists."""
    current: list[Any] = [body]
    for segment in path.split("."):
        next_level: list[Any] = []
        for node in current:
            if not isinstance(node, dict):
                continue
            value = node.get(segment)
            if isinstance(value, list):
                next_level.extend(value)
            elif isinstance(value, dict):
                next_level.append(value)
        current = next_level
    return [row for row in current if isinstance(row, dict)]


def _named_rows(endpoint_key: str, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every ``(path, row)`` a model could have populated with an invented name."""
    out: list[tuple[str, dict[str, Any]]] = []
    for path in NAMED_ROWS.get(endpoint_key, ()):
        out.extend((path, row) for row in _rows(body, path))
    return out


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


def no_code_artifacts(body: dict[str, Any]) -> list[str]:
    """No dict literal, raw field name or engine apology in any prose field."""
    failures = []
    for path, text in _strings(body):
        if path.startswith("meta") or path.startswith("stats_cited"):
            continue
        for marker in CODE_ARTIFACTS:
            if marker in text:
                failures.append(f"{path} contains a code artifact ({marker!r}): {text[:80]!r}")
                break
    return failures


def names_grounded(
    endpoint_key: str, body: dict[str, Any], known_ids: Iterable[str] | None = None
) -> list[str]:
    """Every named row carries a player id that exists.

    ``known_ids`` is the universe of real ids when the caller has one (the
    fixture in the golden suite, the ``players`` collection in precompute).
    Without it the check is weaker but still catches the observed failure: a
    beneficiary with a name and ``player_id: null``.
    """
    universe = set(known_ids) if known_ids is not None else None
    failures = []
    for path, row in _named_rows(endpoint_key, body):
        name = str(row.get("name") or "").strip()
        pid = str(row.get("player_id") or "").strip()
        if not name:
            continue
        if not pid:
            failures.append(f"{path}: {name!r} has no player_id — a name the tools never returned")
        elif universe is not None and pid not in universe:
            failures.append(f"{path}: {name!r} ({pid}) is not a known player")
    return failures


def confidence_consistent(body: dict[str, Any]) -> list[str]:
    """Per-row confidence never exceeds the verdict's; missing usage means low."""
    top = _CONFIDENCE_RANK.get(str(body.get("confidence") or ""), 2)
    failures = []
    for key, value in body.items():
        if not isinstance(value, list):
            continue
        for index, row in enumerate(value):
            if not isinstance(row, dict) or "confidence" not in row:
                continue
            tier = str(row.get("confidence") or "")
            rank = _CONFIDENCE_RANK.get(tier)
            if rank is None:
                continue
            label = f"{key}[{index}] {row.get('name') or ''}".strip()
            if rank > top:
                failures.append(
                    f"{label}: row confidence {tier!r} exceeds the verdict's "
                    f"{body.get('confidence')!r}"
                )
            note = str(row.get("usage_note") or "")
            if _NO_USAGE_RE.search(note) and tier != "low":
                failures.append(
                    f"{label}: says its usage is not available yet claims {tier!r} confidence"
                )
    return failures


def _is_crowd_stat(stat: Any) -> bool:
    """An add/drop count under any name: an ADK body names its own citations,
    so ``adds`` or ``sleeper_add_count`` must not pass as analysis."""
    name = str(stat or "").lower()
    return name in CROWD_STATS or bool(re.search(r"(?:^|_)(?:adds?|drops?)(?:_|$)", name))


def cites_beyond_the_crowd(endpoint_key: str, body: dict[str, Any]) -> list[str]:
    """A board must cite numbers that are not add/drop counts."""
    if endpoint_key not in BOARD_ENDPOINTS:
        return []
    rows = sum(len(_rows(body, path)) for path in NAMED_ROWS[endpoint_key])
    if rows < BOARD_MIN_ROWS:
        return []
    cited = body.get("stats_cited") or []
    analysis = [c for c in cited if isinstance(c, dict) and not _is_crowd_stat(c.get("stat"))]
    if len(analysis) < MIN_NON_CROWD_CITATIONS:
        return [
            f"{endpoint_key} cites {len(analysis)} non-crowd number(s) across {rows} rows "
            f"(need {MIN_NON_CROWD_CITATIONS}); a board built on add counts alone is the "
            f"free preview with prose"
        ]
    return []


def disagrees_with_the_crowd(
    endpoint_key: str, body: dict[str, Any], consensus_ids: Iterable[str] | None = None
) -> list[str]:
    """At least one call must differ from what the crowd is already doing."""
    consensus = set(consensus_ids) if consensus_ids is not None else set()
    failures: list[str] = []

    if endpoint_key == "trending":
        rows = [r for r in body.get("players") or [] if isinstance(r, dict)]
        if len(rows) >= 5:
            contrarian = [
                r
                for r in rows
                if (r.get("trend") == "add" and r.get("verdict") != "add")
                or (r.get("trend") == "drop" and r.get("verdict") == "add")
            ]
            if not contrarian:
                failures.append(
                    f"trending agrees with the crowd on all {len(rows)} moves; a board that "
                    "never fades an add or buys a drop is the free preview with prose"
                )

    elif endpoint_key == "waivers":
        rows = [r for r in body.get("board") or [] if isinstance(r, dict)]
        counted = [r for r in rows if isinstance(r.get("trend_count"), (int, float))]
        if len(counted) >= 5:
            by_crowd = sorted(counted, key=lambda r: -float(r["trend_count"]))
            if [r.get("player_id") for r in by_crowd] == [r.get("player_id") for r in counted]:
                failures.append(
                    "waiver board is in exact add-count order; ranking by expected value "
                    "means disagreeing with the crowd somewhere"
                )

    elif endpoint_key == "sleepers":
        picks = [r for r in body.get("picks") or [] if isinstance(r, dict)]
        claimed = [r for r in picks if str(r.get("player_id") or "") in consensus]
        if claimed:
            failures.append(
                "sleepers include players the crowd has already claimed: "
                + ", ".join(str(r.get("name")) for r in claimed)
            )
        if 0 < len(picks) < SLEEPERS_TARGET and not _SHORT_BOARD_RE.search(
            str(body.get("reasoning") or "")
        ):
            failures.append(
                f"only {len(picks)} sleeper picks and the reasoning does not say why the "
                "board is short"
            )

    elif endpoint_key == "report":
        emerging = [r for r in body.get("emerging") or [] if isinstance(r, dict)]
        claimed = [r for r in emerging if str(r.get("player_id") or "") in consensus]
        if claimed:
            failures.append(
                "'emerging' lists players the crowd already found: "
                + ", ".join(str(r.get("name")) for r in claimed)
                + " — emerging means before the crowd arrives"
            )

    return failures


def sources_resolved(body: dict[str, Any]) -> list[str]:
    """Sources must be readable citations, not redirect blobs or bare domains."""
    failures = []
    for index, source in enumerate(body.get("sources") or []):
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "")
        title = str(source.get("title") or "").strip()
        if _REDIRECT_MARKER in url:
            failures.append(f"sources[{index}] is an unresolved grounding redirect")
        if title and _BARE_DOMAIN_RE.match(title):
            failures.append(f"sources[{index}] title is a bare domain ({title!r}), not a headline")
    return failures


def _numeric_citations(cited: Any) -> list[Any]:
    """``stats_cited`` with numeric-looking string values read as numbers.

    ``StatCitation.value`` may be a string, and a model that cites ``"0.245"``
    and writes "24.5%" has grounded the number exactly as one citing ``0.245``
    has; :func:`leaf_numbers` reads numeric leaves only.
    """
    if not isinstance(cited, list):
        return []
    out: list[Any] = []
    for item in cited:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            text = item["value"].strip()
            percent = text.endswith("%")
            try:
                number = float(text.rstrip("%").strip())
            except ValueError:
                number = math.nan
            if math.isfinite(number):
                # "24.5%" is the ratio 0.245, whose renderings include 24.5.
                item = {**item, "value": number / 100 if percent else number}
        out.append(item)
    return out


def _is_computed_prose(endpoint_key: str | None, path: str) -> bool:
    """Whether ``path`` is a verbatim copy of computed analytics, not model prose."""
    for prefix in _COMPUTED_PROSE.get(endpoint_key or "", ()):
        if path == prefix or path.startswith(f"{prefix}.") or path.startswith(f"{prefix}["):
            return True
    return False


def _is_model_body(body: dict[str, Any]) -> bool:
    """Whether the ADK pipeline wrote ``body``'s prose.

    ``meta.engine == "adk"``, or a body from before that field existed with
    ``meta.model`` set. Deterministic and narrated bodies are exempt: the first
    writes no free prose, the second is guarded at edit time.
    """
    meta = body.get("meta") or {}
    if not isinstance(meta, dict):
        return False
    engine = meta.get("engine")
    return engine == "adk" or (engine is None and bool(meta.get("model")))


def _model_prose(body: dict[str, Any], endpoint_key: str | None) -> Iterator[tuple[str, str]]:
    """``(path, text)`` for every string a model wrote as prose.

    Skips :data:`_NOT_PROSE_PREFIXES` (``sources`` titles are not our prose),
    the computed fields an endpoint copies verbatim (:data:`_COMPUTED_PROSE`),
    and strings with no whitespace: an id ("11581"), a grade ("B+") or a team
    code is a value, not a sentence.
    """
    for path, text in _strings(body):
        if path.split(".")[0].split("[")[0] in _NOT_PROSE_PREFIXES:
            continue
        if _is_computed_prose(endpoint_key, path):
            continue
        if not any(ch.isspace() for ch in text.strip()):
            continue
        yield path, text


def prose_names_grounded(body: dict[str, Any], endpoint_key: str | None = None) -> list[str]:
    """Every multi-word name a model wrote in prose is a name the body carries.

    The row check in :func:`names_grounded` sees only ``name`` fields; the
    reasoning paragraph that recommends "Travis Etienne" when no row or
    citation mentions him slips past it. Scoped exactly as
    :func:`numbers_grounded` is. Names are matched against the whole body,
    ``stats_cited[].player`` included, so a cited player may be named.
    """
    if not _is_model_body(body):
        return []
    failures: list[str] = []
    for path, text in _model_prose(body, endpoint_key):
        for name in unknown_names(text, body):
            failures.append(f"{path} names {name!r}, whom the body never returned")
    if len(failures) > _MAX_NUMBER_FAILURES:
        extra = len(failures) - _MAX_NUMBER_FAILURES
        failures = failures[:_MAX_NUMBER_FAILURES] + [f"...and {extra} more unknown name(s)"]
    return failures


def numbers_grounded(body: dict[str, Any], endpoint_key: str | None = None) -> list[str]:
    """Every number a model wrote in prose is a leaf of the body or a citation.

    Applies to bodies the ADK pipeline wrote: ``meta.engine == "adk"``, or a
    body from before that field existed with ``meta.model`` set. A narrated
    body is not checked here — its prose was checked against the computed body
    at edit time, and the computed notes carry numbers the deterministic engine
    derived rather than cited. Years
    (``20xx``), zero and the integers 1-12 need no citation; everything else
    does, including percentages, which are matched against the ``0-1`` leaves
    they render. Only strings containing whitespace are read as prose: an id,
    a grade or a team code is a value, not a claim. Numbers inside ``sources``
    titles are not our prose and are not checked, and neither are the fields an
    endpoint copies verbatim from computed analytics (:data:`_COMPUTED_PROSE`),
    which ``endpoint_key`` selects — omit it and every string is treated as
    model prose, which is the stricter reading.
    """
    if not _is_model_body(body):
        return []
    allowed = leaf_numbers({k: v for k, v in body.items() if k not in _NOT_PROSE_PREFIXES})
    allowed |= leaf_numbers({"stats_cited": _numeric_citations(body.get("stats_cited"))})
    allowed.add("0")  # "0 red-zone touches" is a count, not a statistic to cite
    failures: list[str] = []
    for path, text in _model_prose(body, endpoint_key):
        for token in ungrounded_numbers(text, allowed):
            if _YEAR_RE.match(token.strip().lstrip("+-").replace(",", "")):
                continue
            failures.append(f"{path} quotes {token} which is in neither stats_cited nor the body")
    if len(failures) > _MAX_NUMBER_FAILURES:
        extra = len(failures) - _MAX_NUMBER_FAILURES
        failures = failures[:_MAX_NUMBER_FAILURES] + [f"...and {extra} more ungrounded number(s)"]
    return failures


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def check_quality(
    endpoint_key: str,
    body: dict[str, Any],
    *,
    known_ids: Iterable[str] | None = None,
    consensus_ids: Iterable[str] | None = None,
) -> list[str]:
    """Run every value rule against one response body.

    Args:
        endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`.
        body: The response as a plain dict (``model_dump(mode="json")``).
        known_ids: Universe of real player ids, when the caller has one.
        consensus_ids: Players the crowd has already claimed (add count at or
            above :data:`CONSENSUS_ADD_COUNT`), when the caller knows them.

    Returns:
        Failure messages, each prefixed ``QUALITY:``; empty when the body passes.
    """
    failures = (
        no_code_artifacts(body)
        + names_grounded(endpoint_key, body, known_ids)
        + prose_names_grounded(body, endpoint_key)
        + confidence_consistent(body)
        + cites_beyond_the_crowd(endpoint_key, body)
        + disagrees_with_the_crowd(endpoint_key, body, consensus_ids)
        + sources_resolved(body)
        + numbers_grounded(body, endpoint_key)
    )
    return [f"QUALITY: {message}" for message in failures]


async def consensus_from_store(store: Store) -> set[str]:
    """Players the live trending poll says the crowd has already claimed."""
    entries = await get_trending(store, "add")
    return {
        str(entry.get("player_id"))
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("count"), (int, float))
        and float(entry["count"]) >= CONSENSUS_ADD_COUNT
    }


async def check_board_quality(store: Store, endpoint_key: str, body: dict[str, Any]) -> list[str]:
    """Run :func:`check_quality` against a live board, resolving ids in the store.

    Used by :mod:`ingest.precompute` on every warmed board. Player ids are
    checked against the ``players`` collection so a name the model invented
    fails even when it came with a plausible-looking id.
    """
    ids = {
        str(row.get("player_id"))
        for _, row in _named_rows(endpoint_key, body)
        if row.get("player_id")
    }
    known: set[str] = set()
    for pid in ids:
        if await store.get(PLAYERS_COLLECTION, pid) is not None:
            known.add(pid)
    consensus = await consensus_from_store(store)
    return check_quality(endpoint_key, body, known_ids=known, consensus_ids=consensus)
