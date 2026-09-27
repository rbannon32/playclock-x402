"""The narrated engine (``ENGINE=narrated``): deterministic body, model prose.

Why this exists
---------------
The public API serves the five personalized endpoints from the deterministic
engine, because the full ADK pipeline runs ~55s against a settle-before-return
payment flow and a caller who times out has paid for nothing. The deterministic
body is fast and every number in it is grounded — but its prose is a template.
A roster audit whose reasoning contains a Python dict literal, sold at LLM
prices, is the "lackluster" the product was accused of.

This engine keeps the fast, grounded body and replaces only the words around
it. After :class:`~api.agents.deterministic.DeterministicAnalysisEngine` has
produced the response, a :class:`Narrator` makes **one tool-free model call**
that returns rewritten text for a whitelisted set of prose fields
(:data:`PROSE_PATHS`). The engine applies those edits through two guards and
re-validates the result against the response contract:

* **Whitelist.** Only the paths in :data:`PROSE_PATHS` for that endpoint may
  change, and only when the existing value is a string. Ranks, calls, grades,
  ids, ``stats_cited`` and ``meta`` are never editable, so the model cannot
  alter a verdict category, invent a citation or fabricate a freshness marker.
* **Grounding.** Every number in the new text must already be present in the
  body (see :func:`allowed_numbers`), and every "Firstname Lastname" it
  mentions must be a name the body already carries (see
  :func:`unknown_names`). An edit that fails either check is dropped and the
  deterministic text survives. This is the whole anti-hallucination story for
  this engine: the model never sees a tool, so the only way a wrong number
  could reach a payer is through prose, and prose is checked in Python.

A narrator that times out, raises or returns nothing usable degrades to the
plain deterministic body — logged and counted, never an :class:`EngineError`.
A paid answer here is always at least the deterministic one; ``meta.model``
says which one was delivered (``null`` = untouched, the model id = narrated).
"""

from __future__ import annotations

import abc
import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from api.agents.engine import AnalysisEngine, response_model_for
from api.agents.vertex import genai_client, thinking_config
from api.core.config import ENDPOINT_KEYS, Settings
from api.data.predictions import verdict_call
from api.schemas import AnalysisResponse

logger = logging.getLogger(__name__)


class ProseEdit(BaseModel):
    """One rewritten prose field: where it goes and what it now says."""

    path: str = Field(
        description=(
            "Dotted/indexed path into the response body, e.g. 'verdict', "
            "'players[3].analysis', 'injury_fallout[0].beneficiaries[1].note'."
        )
    )
    text: str = Field(description="The replacement text for that field.")


class ProseEdits(BaseModel):
    """The narrator's whole answer. A wrapper object because a bare JSON array
    is the one shape structured-output modes handle least reliably."""

    edits: list[ProseEdit] = Field(default_factory=list)


#: The prose fields each endpoint's narrator may rewrite, as path *patterns*:
#: ``[]`` stands for every index of a list. Everything not listed here is
#: structural (a rank, a call, a grade, an id, a number) and is never editable.
#: A test asserts every pattern resolves to a ``str`` field on the endpoint's
#: response model, so a schema rename fails in CI rather than silently making a
#: field un-narratable.
PROSE_PATHS: dict[str, tuple[str, ...]] = {
    "trending": ("verdict", "reasoning", "players[].analysis"),
    "sleepers": (
        "verdict",
        "reasoning",
        "picks[].usage_note",
        "picks[].matchup_note",
        "picks[].rationale",
    ),
    "player": ("verdict", "reasoning", "player.usage_trajectory", "player.schedule_outlook"),
    "matchup": ("verdict", "reasoning", "ranked[].projection_note"),
    "roster": (
        "verdict",
        "reasoning",
        "positional_grades[].note",
        "start_sit[].reason",
        "drop_candidates[].reason",
        "waiver_adds[].reason",
    ),
    "waivers": ("verdict", "reasoning", "board[].rationale"),
    "report": (
        "verdict",
        "reasoning",
        "emerging[].note",
        "injury_fallout[].beneficiaries[].note",
        "stock_up[].note",
        "stock_down[].note",
        "rookie_watch[].note",
        "streamers[].note",
    ),
    "team_report": (
        "verdict",
        "reasoning",
        "deficiencies[].detail",
        "deficiencies[].available_fixes[].why",
        "manager_review.luck_note",
        "preseason_outlook.draft_summary",
        "preseason_outlook.note",
    ),
    # ``values`` and ``reaches`` repeat rows from ``tiers``; all three are
    # editable so the same player does not carry two different notes.
    "draft_board": (
        "verdict",
        "reasoning",
        "tiers[].label",
        "tiers[].players[].note",
        "values[].note",
        "reaches[].note",
    ),
    "draft_report": (
        "verdict",
        "reasoning",
        "best_picks[].note",
        "worst_picks[].note",
        "positional_balance[].note",
        "week_one_plan[]",
    ),
}

assert set(PROSE_PATHS) == set(ENDPOINT_KEYS), (
    "PROSE_PATHS must cover exactly api.core.config.ENDPOINT_KEYS"
)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"[^.\[\]]+|\[\d*\]")
_INDEX_RE = re.compile(r"\[(\d*)\]")


def _segments(path: str) -> list[str]:
    """Split ``'a[2].b'`` into ``['a', '[2]', 'b']``. Unparseable input -> ``[]``."""
    path = path.strip()
    if not path:
        return []
    segments = _SEGMENT_RE.findall(path)
    # Reject anything the tokenizer skipped (``a..b``, ``a[x]``, stray brackets).
    if "".join(segments).replace(".", "") != path.replace(".", ""):
        return []
    return segments


def _pattern_of(path: str) -> str:
    """The whitelist pattern a concrete path belongs to: ``players[3].x`` -> ``players[].x``."""
    return _INDEX_RE.sub("[]", path.strip())


def _resolve(body: dict[str, Any], path: str) -> tuple[Any, str | int] | None:
    """Return ``(container, key)`` for ``path`` in ``body``, or ``None`` if absent."""
    segments = _segments(path)
    if not segments:
        return None
    node: Any = body
    parent: Any = None
    key: str | int | None = None
    for segment in segments:
        match = _INDEX_RE.fullmatch(segment)
        if match:
            if not match.group(1) or not isinstance(node, list):
                return None
            index = int(match.group(1))
            if index >= len(node):
                return None
            parent, key, node = node, index, node[index]
        else:
            if not isinstance(node, dict) or segment not in node:
                return None
            parent, key, node = node, segment, node[segment]
    assert parent is not None and key is not None
    return parent, key


def editable_paths(endpoint_key: str, body: dict[str, Any]) -> list[str]:
    """Expand :data:`PROSE_PATHS` against a concrete body.

    Only paths that currently hold a string are returned: an absent optional
    block (``preseason_outlook`` mid-season) or a ``None`` field
    (``usage_trajectory`` for a player with no rollup) is not an invitation to
    write one.
    """
    out: list[str] = []
    for pattern in PROSE_PATHS.get(endpoint_key, ()):
        out.extend(_expand(body, _segments(pattern), ""))
    return out


def _expand(node: Any, segments: list[str], prefix: str) -> list[str]:
    if not segments:
        return [prefix] if isinstance(node, str) else []
    head, rest = segments[0], segments[1:]
    if head == "[]":
        if not isinstance(node, list):
            return []
        return [
            path
            for index, item in enumerate(node)
            for path in _expand(item, rest, f"{prefix}[{index}]")
        ]
    if not isinstance(node, dict) or head not in node:
        return []
    return _expand(node[head], rest, f"{prefix}.{head}" if prefix else head)


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------

#: ``(?!ers\b)`` keeps the San Francisco 49ers from reading as the number 49.
_NUMBER_RE = re.compile(
    r"[-+]?\d{1,3}(?:,\d{3})+(?:\.\d+)?(?!\d|ers\b)|[-+]?\d+(?:\.\d+)?(?!\d|ers\b)"
)

#: Integers a sentence may use as ordinals or counts without citing anything:
#: "top 5", "one of two", "tier 3", "week 4". Kept small on purpose.
_FREE_SMALL_INTEGERS = frozenset(str(n) for n in range(1, 13))


def _normalize_number(token: str) -> str:
    """Canonical form for comparison: no sign, no separators, no trailing zeros."""
    token = token.strip().lstrip("+-").replace(",", "")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token or "0"


def _renderings(value: float) -> set[str]:
    """Every way prose may honestly write one numeric leaf."""
    out: set[str] = set()
    value = abs(float(value))
    candidates = [value, round(value, 2), round(value, 1), round(value)]
    if value.is_integer():
        candidates.append(int(value))
    for candidate in candidates:
        literal = repr(candidate) if isinstance(candidate, float) else str(candidate)
        out.add(_normalize_number(literal))
        out.add(_normalize_number(f"{candidate:.0f}"))
        out.add(_normalize_number(f"{candidate:.1f}"))
        out.add(_normalize_number(f"{candidate:.2f}"))
    if 0.0 <= value <= 1.0:
        percent = value * 100.0
        for text in (f"{percent:.0f}", f"{percent:.1f}", f"{percent:.2f}"):
            out.add(_normalize_number(text))
    return out


def allowed_numbers(body: dict[str, Any]) -> set[str]:
    """The set of numbers prose about this body may contain.

    The rule, exactly:

    * every numeric leaf of the body (``bool`` excluded), rendered as itself,
      rounded to 0, 1 and 2 decimals, and — when the value lies in ``[0, 1]`` —
      as a percentage at the same precisions (so ``0.82`` permits ``0.82``,
      ``82`` and ``82.0``);
    * every number literal already present in one of the body's string fields
      (those were written by the deterministic engine from the same leaves);
    * the integers 1 through 12 (:data:`_FREE_SMALL_INTEGERS`), so ordinals and
      counts such as "top 5" or "one of two" do not need a citation.

    ``meta`` is skipped, as are string ids and timestamps (``player_id``,
    ``generated_at``): their digits are not facts a sentence may cite.

    Signs, thousands separators and trailing zeros are ignored on both sides,
    so ``165,320`` and ``-0.165`` match ``165320`` and ``0.165``.
    """
    allowed: set[str] = set(_FREE_SMALL_INTEGERS)
    _collect_numbers(body, allowed)
    return allowed


def leaf_numbers(body: dict[str, Any]) -> set[str]:
    """Like :func:`allowed_numbers` but from numeric leaves *only*.

    The narrator's own set also admits any number literal already inside a
    string field, because the deterministic engine wrote those strings from
    the same leaves (a scoring average, say). That is exactly the exemption a
    model-written body must not get: its prose is what is being checked. The
    value gate (:mod:`api.evals.quality`) uses this stricter set, plus the
    same small integers.
    """
    allowed: set[str] = set(_FREE_SMALL_INTEGERS)
    _collect_numbers(body, allowed, strings=False)
    return allowed


def _is_identifier_key(key: Any) -> bool:
    """Ids and timestamps hold digits that are not facts: ``player_id``, ``generated_at``."""
    return isinstance(key, str) and (key == "id" or key.endswith(("_id", "_at")))


def _collect_numbers(value: Any, into: set[str], *, strings: bool = True) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        into.update(_renderings(value))
    elif isinstance(value, str):
        if strings:
            for token in _NUMBER_RE.findall(value):
                into.add(_normalize_number(token))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "meta":
                continue
            if isinstance(item, str) and _is_identifier_key(key):
                continue
            _collect_numbers(item, into, strings=strings)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_numbers(item, into, strings=strings)


def ungrounded_numbers(text: str, allowed: set[str]) -> list[str]:
    """Numbers in ``text`` that :func:`allowed_numbers` does not permit."""
    return [token for token in _NUMBER_RE.findall(text) if _normalize_number(token) not in allowed]


#: One capitalised word as prose writes a name: dotted initials (``A.J.``),
#: inner capitals (``CeeDee``, ``McCaffrey``), all-caps initials (``DK``),
#: apostrophes and hyphens (``Ja'Marr``, ``Amon-Ra``, ``Smith-Njigba``).
_NAME_TOKEN_RE = re.compile(r"(?:[A-Z]\.){2,}|[A-Z][A-Za-z]*(?:['’\-][A-Za-z]+)*")

#: What may separate two words of one name: blanks, or a period after an
#: abbreviation (``St. Brown``, ``Harrison Jr. is``, ``A. Brown``).
_BLANK_RE = re.compile(r"[ \t]+")
_ABBREV_GAP_RE = re.compile(r"\.[ \t]+")
_ABBREVIATIONS = frozenset({"Jr", "Sr", "St"})

#: Name suffixes: part of a name when present, never required to match one.
_SUFFIXES = frozenset({"Jr", "Sr", "II", "III", "IV", "V"})

#: A trailing possessive (``Robinson's``) — stripped, and it ends the name.
_POSSESSIVE_RE = re.compile(r"['’]s$")

#: Capitalised words that start sentences or name football things rather than
#: people. They are never part of a name, so they split a run of capitalised
#: words: in "Start Bijan Robinson" only "Bijan Robinson" is a name candidate.
_NOT_A_NAME: frozenset[str] = frozenset(
    """
    The A An In On At For With Against After Before Behind Ahead Over Under His Her Their He She
    They This That These Those But And Or If When While Where Which Who Whose What Why How
    Start Sit Bench Flex Add Adds Drop Drops Fade Hold Buy Sell Stash Stream Streamer Streamers
    Red Zone Week Weeks Season Fantasy Points Point Target Targets Snap Snaps Share Touches Usage
    Market Rank Ranks Tier Tiers Value Values Reach Reaches Draft Waiver Waivers Wire Roster
    Lineup Matchup Defense Offense Sleeper Sleepers Trending Play Clock Injured Reserve
    Questionable Doubtful Out Active Inactive Rookie Veteran Running Back Backs Wide Receiver
    Receivers Tight End Ends Quarterback Quarterbacks Kicker Kickers Monday Thursday Sunday Night
    Football League Team Teams Game Games Home Away Round Rounds Pick Picks Grade Grades
    Confidence High Medium Low Last Next Prior Current First Second Third Fourth Fifth Top Bottom
    Every No Not Yes Only Still Even Also Both Each Most More Less Best Worst Better Worse
    Weak Plus Minus Elite Prime Primary Backup Depth Chart Order Starting Starter Starters Role
    Form Data Stats Numbers Number Trend Trends Rising Flat Declining Expect Expected Actual
    Optimal Efficiency Bench Manager Managers Owner Owners Free Agent Agents Available Claim
    Priority Handcuff Handcuffs Bye Byes Injury Injuries Status Practice Report Reports Preseason
    Regular Playoff Playoffs Championship Standings Schedule Opponent Opponents Versus Vs
    Ppr Half Full Standard Scoring Projection Projections Floor Ceiling Upside Downside Risk
    Safe Boom Bust Cut Keep Trade Trades Sell Hold Wait Watch Monitor Confirm Verify Check
    Note Notes Verdict Reasoning Summary Overall Total Average Median Mean Per Game
    Nfl Afc Nfc East West North South Division Conference
    I It Its We You Your Our My Me Us Is Are Was Were Be Been Has Have Had Do Does Did Can Could
    Would Should Must May Might Then Now Here There However Meanwhile Instead Otherwise
    Although Though Despite Since Because Without Across Through Between Among Unlike Via Given
    Lean Pivot Consider Avoid Prefer Trust Temper Take Grab Move Swap Upgrade Downgrade Favor
    Today Tonight Tomorrow Yesterday Tuesday Wednesday Friday Saturday Weekend Coach Coaches
    January February March April June July August September October November December
    Sources Source Tape Film Stadium Dome Week-to-week
    QB QBs RB RBs WR WRs TE TEs FLEX DST DEF IDP PPR ADP NFL AFC NFC IR PUP NFI COVID TD TDs
    YAC EPA ROS DFS ESPN ET PT PM AM OK US USD USDC API AI MVP OC DC HC
    ARI ATL BAL BUF CAR CHI CIN CLE DAL DEN DET GB HOU IND JAX JAC KC LV LAC LAR LA MIA MIN NE
    NO NYG NYJ PHI PIT SF SEA TB TEN WAS WSH
    """.split()
)

#: NFL team nicknames. Nobody is named "Chiefs", so one standing beside a
#: player's name ("Chiefs Kelce") is a modifier and is skipped.
_TEAM_NICKNAMES: frozenset[str] = frozenset(
    """
    Cardinals Falcons Ravens Bills Panthers Bears Bengals Browns Cowboys Broncos Lions Packers
    Texans Colts Jaguars Chiefs Raiders Chargers Rams Dolphins Vikings Patriots Saints Giants
    Jets Eagles Steelers 49ers Seahawks Buccaneers Titans Commanders
    """.split()
)

#: NFL cities and nicknames. A run made only of these ("Green Bay", "Kansas
#: City Chiefs") is a team, but unlike :data:`_NOT_A_NAME` they do not clear a
#: name that contains one: "Dallas Goedert" and "Parker Washington" are real
#: players, so "Dallas Fakename" must be checked like any other name. City
#: words are names too ("Darnell Washington", "Travis Houston"), so outside an
#: all-team run they count as name words; only :data:`_TEAM_NICKNAMES` skip.
_TEAM_WORDS: frozenset[str] = _TEAM_NICKNAMES | frozenset(
    """
    Arizona Atlanta Baltimore Buffalo Carolina Chicago Cincinnati Cleveland Dallas Denver
    Detroit Green Bay Houston Indianapolis Jacksonville Kansas City Las Vegas Los Angeles Miami
    Minnesota New England Orleans York Philadelphia Pittsburgh San Francisco Seattle Tampa
    Tennessee Washington
    """.split()
)

#: Body fields whose string values name a person or a team the prose may cite.
_NAME_FIELDS = frozenset(
    {
        "name",
        "injured_player",
        "league_name",
        "sleeper_username",
        "opponent_team_name",
        "label",
        "player",
    }
)


def _norm(token: str) -> str:
    """Comparison form of one name word: ``A.J.`` == ``AJ``, curly == straight."""
    return token.replace(".", "").replace("’", "'").lower()


def _body_name_tokens(body: dict[str, Any]) -> list[tuple[str, ...]]:
    """One ordered, normalised token tuple per name the body carries.

    Tokens are kept *per name* rather than pooled: pooling is what let
    "Bijan Mahomes" through when the body named Bijan Robinson and Patrick
    Mahomes separately (PR #31 review). They are whole words, so "Ian Thomas"
    is not found inside "Brian Thomas Jr." the way a substring test finds it.
    """
    names: set[str] = set()
    _collect_names(body, names)
    per_name = {
        tuple(_norm(m.group()) for m in _NAME_TOKEN_RE.finditer(name.replace("’", "'")))
        for name in names
    }
    return [tokens for tokens in per_name if tokens]


def _collect_names(value: Any, into: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _NAME_FIELDS and isinstance(item, str) and item.strip():
                into.add(item.strip())
            else:
                _collect_names(item, into)
    elif isinstance(value, list):
        for item in value:
            _collect_names(item, into)


def _capitalised_runs(text: str) -> list[tuple[list[str], bool]]:
    """Maximal runs of capitalised words that could together be one name.

    Returns ``(words, at_sentence_start)`` per run. A run breaks at any
    punctuation (except an abbreviation's period), at a line break, after a
    possessive, and at a :data:`_NOT_A_NAME` word, which is dropped.
    """
    runs: list[tuple[list[str], bool]] = []
    words: list[str] = []
    start = False
    prev_end = -1
    prev_word = ""
    for match in _NAME_TOKEN_RE.finditer(text):
        word = match.group()
        possessive = bool(_POSSESSIVE_RE.search(word))
        word = _POSSESSIVE_RE.sub("", word)
        gap = text[prev_end : match.start()] if prev_end >= 0 else None
        joined = (
            words
            and gap is not None
            and (
                _BLANK_RE.fullmatch(gap)
                or (
                    _ABBREV_GAP_RE.fullmatch(gap)
                    and (prev_word in _ABBREVIATIONS or len(prev_word) == 1)
                )
            )
        )
        # A word glued to the previous letters ("iPhone", "McX" mid-token) is
        # not the start of a word at all.
        if match.start() > 0 and text[match.start() - 1].isalnum():
            joined = False
            if words:
                runs.append((words, start))
            words, prev_end, prev_word = [], match.end(), ""
            continue
        if not joined and words:
            runs.append((words, start))
            words = []
        if word in _NOT_A_NAME:
            if words:
                runs.append((words, start))
            words, prev_end, prev_word = [], -1, ""
            continue
        if not words:
            before = text[: match.start()].rstrip(" \t")
            start = not before or before[-1] in ".!?:;\n\"'([-*•—–"
        words.append(word)
        prev_end, prev_word = match.end(), word
        if possessive:
            runs.append((words, start))
            words, prev_end, prev_word = [], -1, ""
    if words:
        runs.append((words, start))
    return runs


def _in_one_name(span: list[str], names: list[tuple[str, ...]]) -> bool:
    """``span`` is, in order, whole words of one single body name."""
    wanted = [_norm(word) for word in span]
    for tokens in names:
        remaining = iter(tokens)
        if all(any(word == token for token in remaining) for word in wanted):
            return True
    return False


def _run_is_known(words: list[str], names: list[tuple[str, ...]], *, lone_ok: bool) -> bool:
    """Every name word in the run is covered by a body name.

    Walks the run greedily, consuming the longest span (two words or more)
    that belongs to one body name; team nicknames and suffixes are skipped.
    A run made only of team words is a team. Otherwise a city word is a name
    word like any other, so "Darnell Washington" and "Denver Kelce" are two
    left-over words even when the body names a Darnell or a Kelce.
    ``lone_ok`` lets a single left-over name word pass when it is a body
    name's word ("Chiefs Kelce"); two left-over words never pass, since that
    is the recombination ("Bijan Mahomes") the guard exists to stop.
    """
    if all(word in _TEAM_WORDS for word in words):
        return True
    leftovers: list[str] = []
    i = 0
    while i < len(words):
        for j in range(len(words), i + 1, -1):
            if _in_one_name(words[i:j], names):
                i = j
                break
        else:
            word = words[i]
            if word not in _TEAM_NICKNAMES and word not in _SUFFIXES:
                leftovers.append(word)
            i += 1
    if not leftovers:
        return True
    return lone_ok and len(leftovers) == 1 and _in_one_name(leftovers, names)


def unknown_names(text: str, body: dict[str, Any]) -> list[str]:
    """Multi-word names in ``text`` that the body does not name.

    Conservative about *what counts as a name*, strict about *matching it*,
    because the cost of a false positive is a dropped edit while the cost of a
    miss is a hallucinated player in a paid answer:

    * a candidate is a run of two or more capitalised words (``Travis
      Etienne``, ``CeeDee Lamb``, ``A.J. Brown``, ``Amon-Ra St. Brown``); a
      single word is never flagged, and a trailing possessive is stripped;
    * sentence-start and football words (:data:`_NOT_A_NAME`) are never part
      of a name and split a run, so "Start Bijan Robinson" checks "Bijan
      Robinson"; a run made only of team words ("Kansas City Chiefs") is a
      team — but a team word does not clear the rest of the run, so "Dallas
      Fakename" is checked;
    * otherwise the run's words must be whole words of *one* body name, in
      order (``Marvin Harrison`` and ``Harrison Jr`` match ``Marvin Harrison
      Jr.``; ``Ian Thomas`` does not match ``Brian Thomas Jr.``). Sharing one
      word with one name and one with another is not a match — ``Bijan
      Mahomes`` is exactly the recombination the guard exists to stop;
    * the first word of a sentence may be an unlisted verb ("Ride Bijan
      Robinson", "Faces Dallas"): it is ignored when the rest of the run is
      fully covered without it — unless it is a body name's word and the rest
      is a city ("Travis Houston"), which is a name, not a verb and a team.
    """
    names = _body_name_tokens(body)
    flagged: list[str] = []
    for words, at_start in _capitalised_runs(text):
        if len(words) < 2:
            continue
        if _run_is_known(words, names, lone_ok=True):
            continue
        if at_start and _run_is_known(words[1:], names, lone_ok=False):
            # "Faces Dallas" is a verb and a team; "Travis Houston" is a body
            # first name and a city, i.e. a name the body does not contain.
            rest_is_team = all(word in _TEAM_WORDS for word in words[1:])
            if not (rest_is_team and _in_one_name(words[:1], names)):
                continue
        flagged.append(" ".join(words))
    return flagged


# --------------------------------------------------------------------------
# Applying edits
# --------------------------------------------------------------------------


def apply_edits(
    endpoint_key: str, body: dict[str, Any], edits: list[ProseEdit]
) -> tuple[dict[str, Any], list[str]]:
    """Apply the narrator's edits to a copy of ``body``.

    Args:
        endpoint_key: The endpoint the body belongs to; selects the whitelist.
        body: The deterministic body as a JSON-able dict. Not mutated.
        edits: What the narrator returned.

    Returns:
        ``(patched_body, rejections)``. There is exactly one rejection string
        per edit that was not applied, so ``len(edits) - len(rejections)`` is
        the number applied. Each rejection is also logged at WARNING.
    """
    patched = json.loads(json.dumps(body))
    allowed = allowed_numbers(body)
    whitelist = set(PROSE_PATHS.get(endpoint_key, ()))
    rejections: list[str] = []

    for edit in edits:
        path = edit.path.strip()
        reason = _reject_reason(patched, body, path, edit.text, whitelist, allowed, endpoint_key)
        if reason:
            rejections.append(f"{path}: {reason}")
            logger.warning("narrator edit rejected for %s at %s: %s", endpoint_key, path, reason)
            continue
        located = _resolve(patched, path)
        assert located is not None  # _reject_reason resolved it already
        container, key = located
        container[key] = edit.text.strip()
    return patched, rejections


def _reject_reason(
    patched: dict[str, Any],
    original: dict[str, Any],
    path: str,
    text: str,
    whitelist: set[str],
    allowed: set[str],
    endpoint_key: str = "",
) -> str | None:
    if not path or _pattern_of(path) not in whitelist:
        return "path is not an editable prose field"
    located = _resolve(patched, path)
    if located is None:
        return "path does not resolve in the body"
    container, key = located
    if not isinstance(container[key], str):
        return "field is not a string in this body"
    if not text or not text.strip():
        return "replacement text is empty"
    bad_numbers = ungrounded_numbers(text, allowed)
    if bad_numbers:
        return f"ungrounded number(s) {bad_numbers}"
    bad_names = unknown_names(text, original)
    if bad_names:
        return f"unknown player name(s) {bad_names}"
    adp = _adp_reason(container[key], text)
    if adp:
        return adp
    if (
        endpoint_key == "player"
        and path == "verdict"
        and verdict_call(container[key]) != verdict_call(text)
    ):
        # The archive reads the start/sit call out of this sentence and scores
        # it permanently, so a rewrite may change the wording, never the call.
        return "changes the start/sit call the verdict makes"
    return None


_ADP_TERM = r"(?:\bADPs?\b|\baverage draft positions?\b)"
_ADP_RE = re.compile(_ADP_TERM, re.IGNORECASE)
_ADP_DISCLAIMED_RE = re.compile(r"\b(?:not|no|never)\b[^.;]{0,40}?" + _ADP_TERM, re.IGNORECASE)


def _adp_reason(before: str, after: str) -> str | None:
    """``market_rank`` is Sleeper popularity, never a consensus ADP.

    A field that disclaims the term must keep the disclaimer, and a field that
    did not mention it must not start: every use has to be a negation.
    """
    if bool(_ADP_RE.search(before)) != bool(_ADP_RE.search(after)):
        return "adds or drops the not-an-ADP disclaimer"
    if len(_ADP_RE.findall(after)) != len(_ADP_DISCLAIMED_RE.findall(after)):
        return "calls the market signal an ADP"
    return None


# --------------------------------------------------------------------------
# Narrators
# --------------------------------------------------------------------------


class Narrator(abc.ABC):
    """Rewrites the prose of a finished body. Sees no tools, computes nothing."""

    #: Stable identifier for logs.
    name: str = "abstract"

    @abc.abstractmethod
    async def narrate(
        self, endpoint_key: str, body: dict[str, Any], request_context: dict[str, Any]
    ) -> list[ProseEdit]:
        """Return replacement text for some of :func:`editable_paths`.

        Args:
            endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`.
            body: The deterministic response as a JSON-able dict.
            request_context: What the route asked for.

        Returns:
            Edits, each naming a concrete path. Unlisted paths keep their text.
        """


#: How much of the request context the prompt carries. A pasted roster or a
#: league's free-agent pool can run to tens of kilobytes and the body already
#: reflects them; the context is there for intent, not data.
_CONTEXT_CHARS = 4000

#: The narrator's own role. The ADK synthesizer's role talks about news
#: findings in session state; a narrated run has none, and telling the model
#: they exist invites it to supply some.
NARRATOR_ROLE = (
    "You are the analyst for Play Clock. Readers paid real money for this "
    "answer, and trust is the product. Write like a sharp friend who has done "
    "the homework: specific, decisive, no hedging filler. Everything you know "
    "about this question is in the body below; there is no other source."
)


def build_prompt(endpoint_key: str, body: dict[str, Any], request_context: dict[str, Any]) -> str:
    """The single narrator prompt: the body, the editable paths, the rules."""
    shown = {key: value for key, value in body.items() if key != "meta"}
    context_json = json.dumps(request_context, default=str, sort_keys=True)
    if len(context_json) > _CONTEXT_CHARS:
        context_json = context_json[:_CONTEXT_CHARS] + " ...(truncated)"
    paths = editable_paths(endpoint_key, body)
    return "\n\n".join(
        [
            NARRATOR_ROLE,
            (
                "You are rewriting the prose of a FINISHED analysis, not producing the "
                "analysis. The body below was computed from ingested stats and is correct "
                "as it stands: every rank, call, grade, verdict category, confidence tier "
                "and number in it is final. Your job is to make its prose read like an "
                "analyst wrote it, so a reader who paid for this answer gets a specific, "
                "decisive explanation instead of a template."
            ),
            f"Endpoint: {endpoint_key}",
            f"Request: {context_json}",
            f"Body (JSON):\n{json.dumps(shown, default=str)}",
            "Rewrite ONLY these paths. Return one edit per path you rewrite; skip a path "
            "whose text already reads well:\n" + "\n".join(f"- {path}" for path in paths),
            (
                "Hard rules:\n"
                "- Every number you write must already appear in the body above, verbatim "
                "or as an obvious rendering of it (0.82 may be written 82%). Never compute, "
                "average, estimate, project or recall a number. One number that is not in "
                "the body invalidates the edit.\n"
                "- Never name a player who is not named in the body.\n"
                "- State no fact that is not in the body: no injuries, news, quotes, "
                "weather, projections, matchups or history the body does not contain. "
                "You have no source but the body.\n"
                "- market_rank is Sleeper draft popularity, never an ADP. Never call it "
                "one; a field that says it is not an ADP must still say so after your "
                "rewrite.\n"
                "- Do not change any call, rank, grade, verdict category or confidence; you "
                "are rewriting the words around them.\n"
                "- 'verdict' is one decisive sentence: the thing they paid for.\n"
                "- 'reasoning' is two to five sentences. When the body contains a claim that "
                "disagrees with the crowd or the market (a fade, a hold on a heavily-added "
                "player, a value, a reach, a sit on a popular player), lead with it and give "
                "the number behind it.\n"
                "- Per-row notes are one or two sentences, specific to that player.\n"
                "- Never describe the engine, the method or the data pipeline. No hedging "
                "filler, no markdown."
            ),
            'Return JSON only, shaped {"edits": [{"path": "...", "text": "..."}]}.',
        ]
    )


def parse_edits(text: str | None) -> list[ProseEdit]:
    """Parse the model's JSON into edits. Accepts the wrapper object or a bare list."""
    if not text or not text.strip():
        return []
    raw = json.loads(text)
    if isinstance(raw, list):
        raw = {"edits": raw}
    return ProseEdits.model_validate(raw).edits


class GeminiNarrator(Narrator):
    """One structured-output Gemini call on Vertex AI. Imports lazily, so a
    deterministic deployment and every unit test never touch google-genai."""

    name = "gemini"

    #: Low, not zero: the words should vary, the numbers cannot anyway.
    temperature = 0.3

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = genai_client(self._settings)
        return self._client

    async def narrate(
        self, endpoint_key: str, body: dict[str, Any], request_context: dict[str, Any]
    ) -> list[ProseEdit]:
        from google.genai import types  # noqa: PLC0415

        response = await self._get_client().aio.models.generate_content(
            model=self._settings.model_id,
            contents=build_prompt(endpoint_key, body, request_context),
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ProseEdits,
                temperature=self.temperature,
                thinking_config=thinking_config(self._settings),
            ),
        )
        return parse_edits(getattr(response, "text", None))


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class NarratedAnalysisEngine(AnalysisEngine):
    """Deterministic body, narrated prose, deterministic fallback.

    ``meta.model`` is the honest label: it stays ``null`` whenever the payer
    got the untouched deterministic body — narrator failure, timeout, or every
    edit rejected — and carries the model id only when at least one edit was
    actually applied.
    """

    name = "narrated"

    def __init__(self, deterministic: AnalysisEngine, narrator: Narrator, settings: Settings):
        self.deterministic = deterministic
        self.narrator = narrator
        self._settings = settings
        #: Answers served without narration since process start. One is fine; a
        #: rate means the model is unreachable and every payer gets templates.
        self.degraded = 0
        #: Edits dropped by the guards since process start.
        self.rejected = 0

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        """Produce the deterministic body, then narrate it if the model cooperates."""
        base = await self.deterministic.analyze(endpoint_key, request_context)
        body = base.model_dump(mode="json")
        try:
            edits = await asyncio.wait_for(
                self.narrator.narrate(endpoint_key, body, dict(request_context or {})),
                timeout=self._settings.narrator_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - any narrator failure is a plain body
            self.degraded += 1
            logger.warning(
                "narrator %s failed for %s (%s: %s); serving the deterministic body "
                "[degraded=%d since start]",
                self.narrator.name,
                endpoint_key,
                type(exc).__name__,
                str(exc) or "timed out",
                self.degraded,
            )
            return base

        patched, rejections = apply_edits(endpoint_key, body, list(edits or []))
        self.rejected += len(rejections)
        applied = len(edits or []) - len(rejections)
        if applied == 0:
            return base
        try:
            narrated = response_model_for(endpoint_key).model_validate(patched)
        except ValidationError as exc:
            self.degraded += 1
            logger.warning(
                "narrated %s body failed validation (%s); serving the deterministic body",
                endpoint_key,
                exc,
            )
            return base
        narrated.meta.model = self._settings.model_id
        narrated.meta.engine = "narrated"
        logger.info(
            "narrated %s: %d edit(s) applied, %d rejected", endpoint_key, applied, len(rejections)
        )
        return narrated

    async def aclose(self) -> None:
        """Close the deterministic engine; the narrator holds no connections."""
        closer = getattr(self.deterministic, "aclose", None)
        if closer is not None:
            await closer()
