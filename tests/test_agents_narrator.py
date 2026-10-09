"""The narrated engine: deterministic body, model prose, Python guards.

The personalized endpoints were being sold as template strings at LLM prices.
``ENGINE=narrated`` fixes the words without reopening the one failure the
product cannot ship with — a number or a name the data does not contain. These
tests pin the guards that make that true, hermetically: the narrator is a fake,
google-genai is never imported, and every body comes from the golden fixture.
"""

from __future__ import annotations

import asyncio
import types
from typing import Any, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from api.agents.deterministic import DeterministicAnalysisEngine
from api.agents.engine import RESPONSE_MODELS, get_engine, set_engine
from api.agents.narrator import (
    PROSE_PATHS,
    NarratedAnalysisEngine,
    Narrator,
    ProseEdit,
    _collect_names,
    _resolve,
    _segments,
    allowed_numbers,
    apply_edits,
    build_prompt,
    editable_paths,
    parse_edits,
    ungrounded_numbers,
    unknown_names,
)
from api.core.config import ENDPOINT_KEYS, Settings
from api.core.store import MemoryStore, Store
from api.evals.golden import GOLDEN_CASES, SEASON, WEEK, GoldenCase, seed_store

MODEL_ID = "gemini-test-model"

#: One representative golden case per endpoint, the full-product ones.
CASE_PER_ENDPOINT: dict[str, str] = {
    "trending": "trending_full_board",
    "sleepers": "sleepers_week4",
    "player": "player_by_name",
    "matchup": "matchup_two_players",
    "roster": "roster_manual_paste",
    "waivers": "waivers_big_board",
    "report": "report_week4",
    "team_report": "team_report_with_analytics",
    "draft_board": "draft_board_full",
    "draft_report": "draft_report_graded",
}

CASES_BY_NAME: dict[str, GoldenCase] = {case.name: case for case in GOLDEN_CASES}


def settings_for(**overrides: Any) -> Settings:
    fields: dict[str, Any] = {
        "store_backend": "memory",
        "engine": "narrated",
        "model_id": MODEL_ID,
        "x402_mode": "disabled",
        "season": SEASON,
        "week_override": WEEK,
    }
    fields.update(overrides)
    return Settings(_env_file=None, **fields)  # type: ignore[call-arg]


class Scripted(Narrator):
    """Returns whatever edits the test hands it, and records the call."""

    name = "scripted"

    def __init__(self, edits: list[ProseEdit] | None = None) -> None:
        self.edits = edits or []
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def narrate(
        self, endpoint_key: str, body: dict[str, Any], request_context: dict[str, Any]
    ) -> list[ProseEdit]:
        self.calls.append((endpoint_key, body, request_context))
        return list(self.edits)


class Sleeping(Narrator):
    name = "sleeping"

    async def narrate(self, endpoint_key: str, body: Any, request_context: Any) -> list[ProseEdit]:
        await asyncio.sleep(5)
        return []


class Exploding(Narrator):
    name = "exploding"

    async def narrate(self, endpoint_key: str, body: Any, request_context: Any) -> list[ProseEdit]:
        raise RuntimeError("vertex said no")


@pytest.fixture(autouse=True)
def _clear_engine_override() -> Any:
    set_engine(None)
    yield
    set_engine(None)


@pytest.fixture
async def seeded(store: Store) -> Store:
    await seed_store(store)
    return store


@pytest.fixture
async def deterministic(seeded: Store) -> DeterministicAnalysisEngine:
    return DeterministicAnalysisEngine(store=seeded, settings=settings_for(engine="deterministic"))


async def body_for(deterministic: DeterministicAnalysisEngine, endpoint_key: str) -> dict[str, Any]:
    case = CASES_BY_NAME[CASE_PER_ENDPOINT[endpoint_key]]
    response = await deterministic.analyze(endpoint_key, dict(case.request_context))
    return response.model_dump(mode="json")


# -- the whitelist against the schema ---------------------------------------


def _without_none(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        remaining = [arg for arg in get_args(annotation) if arg is not type(None)]
        assert len(remaining) == 1, annotation
        return remaining[0]
    return annotation


def _annotation_for(model: type[BaseModel], pattern: str) -> Any:
    """Walk a PROSE_PATHS pattern through the pydantic model's annotations."""
    annotation: Any = model
    for segment in _segments(pattern):
        annotation = _without_none(annotation)
        if segment == "[]":
            assert get_origin(annotation) is list, f"{pattern}: {segment} is not on a list"
            annotation = get_args(annotation)[0]
        else:
            assert isinstance(annotation, type) and issubclass(annotation, BaseModel), (
                f"{pattern}: {segment} is not on a model"
            )
            assert segment in annotation.model_fields, f"{pattern}: no field {segment!r}"
            annotation = annotation.model_fields[segment].annotation
    return _without_none(annotation)


def test_prose_paths_cover_every_endpoint() -> None:
    assert set(PROSE_PATHS) == set(ENDPOINT_KEYS)


@pytest.mark.parametrize("endpoint_key", ENDPOINT_KEYS)
def test_every_prose_path_is_a_string_field_on_the_schema(endpoint_key: str) -> None:
    """A schema rename must fail here, not silently make a field un-narratable."""
    model = RESPONSE_MODELS[endpoint_key]
    for pattern in PROSE_PATHS[endpoint_key]:
        assert _annotation_for(model, pattern) is str, pattern


@pytest.mark.parametrize("endpoint_key", ENDPOINT_KEYS)
def test_structure_is_never_editable(endpoint_key: str) -> None:
    patterns = PROSE_PATHS[endpoint_key]
    for forbidden in ("meta", "stats_cited", "sources", "confidence"):
        assert not any(p == forbidden or p.startswith(forbidden + ".") for p in patterns)
    for pattern in patterns:
        assert not pattern.endswith((".verdict", ".call", ".rank", ".grade", ".player_id"))


# -- editable_paths against real bodies -------------------------------------


@pytest.mark.parametrize("endpoint_key", ENDPOINT_KEYS)
async def test_editable_paths_expand_to_strings_in_a_real_body(
    deterministic: DeterministicAnalysisEngine, endpoint_key: str
) -> None:
    body = await body_for(deterministic, endpoint_key)
    paths = editable_paths(endpoint_key, body)
    assert "verdict" in paths and "reasoning" in paths
    assert len(paths) > 2, "every full-product body has per-row prose to rewrite"
    for path in paths:
        container, key = _resolve(body, path)
        # A field that disclaims ADP must keep the disclaimer (tested below).
        text = "Fine, and not an ADP." if "ADP" in container[key] else "Fine."
        if endpoint_key == "player" and path == "verdict":
            # A player verdict may be reworded but must keep its start/sit call.
            text = container[key] + " Fine."
        patched, rejected = apply_edits(endpoint_key, body, [ProseEdit(path=path, text=text)])
        assert rejected == [], path


@pytest.mark.parametrize(
    ("before", "after", "rejected"),
    [
        ("Popularity, not a consensus ADP.", "Popularity only.", True),
        ("Popularity, not a consensus ADP.", "This is not an ADP; it is popularity.", False),
        ("A steady value.", "His ADP says 12th.", True),
        ("A steady value.", "A steady value.", False),
        ("Not an ADP.", "Not an ADP, though his ADP is early.", True),
        ("A steady value.", "His average draft position is 45.", True),
        ("Not an ADP.", "Not an average draft position; popularity.", False),
    ],
)
def test_an_edit_may_not_call_market_rank_an_adp(before: str, after: str, rejected: bool) -> None:
    body = {"verdict": "Draft.", "reasoning": before}
    _, rejections = apply_edits("draft_board", body, [ProseEdit(path="reasoning", text=after)])
    assert bool(rejections) is rejected


def test_a_capitalised_word_does_not_hide_a_recombined_name() -> None:
    body = {"rows": [{"name": "Bijan Robinson"}, {"name": "Patrick Mahomes"}]}
    assert unknown_names("Fade Bijan Mahomes this week.", body) == ["Bijan Mahomes"]
    assert unknown_names("Start Bijan Robinson over Patrick Mahomes.", body) == []


def test_ids_and_timestamps_are_not_citable_numbers() -> None:
    body = {
        "verdict": "x",
        "rows": [{"player_id": "4983", "name": "A B"}],
        "meta": {"generated_at": "2026-09-24T19:29:49Z"},
    }
    assert ungrounded_numbers("a 49% snap share and 4983 yards", allowed_numbers(body)) == [
        "49",
        "4983",
    ]
    assert ungrounded_numbers("the 49ers", allowed_numbers(body)) == []


async def test_editable_paths_skip_a_none_field(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    """A player with no usage rollup must not be handed a trajectory to write."""
    case = CASES_BY_NAME["player_without_usage_rollup"]
    body = (await deterministic.analyze("player", dict(case.request_context))).model_dump(
        mode="json"
    )
    assert body["player"]["usage_trajectory"] is None
    assert "player.usage_trajectory" not in editable_paths("player", body)
    _, rejected = apply_edits(
        "player", body, [ProseEdit(path="player.usage_trajectory", text="rising fast")]
    )
    assert rejected and "not a string" in rejected[0]


async def test_week_one_plan_items_are_editable(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "draft_report")
    assert body["week_one_plan"], "fixture draft has a plan"
    patched, rejected = apply_edits(
        "draft_report", body, [ProseEdit(path="week_one_plan[0]", text="Add a RB before Week 1.")]
    )
    assert rejected == []
    assert patched["week_one_plan"][0] == "Add a RB before Week 1."


# -- the grounding guard ----------------------------------------------------


def test_allowed_numbers_renders_every_honest_form() -> None:
    allowed = allowed_numbers({"snap_pct": 0.82, "adds": 165320, "delta": -0.165, "pts": 25.55})
    for token in ("0.82", "82", "82%", "165,320", "165320", "-0.165", "0.165", "25.55", "25.6"):
        assert ungrounded_numbers(token, allowed) == [], token


def test_small_integers_are_free() -> None:
    allowed = allowed_numbers({"x": 0.5})
    assert ungrounded_numbers("top 5 in tier 2, one of 12", allowed) == []
    assert ungrounded_numbers("13 targets", allowed) == ["13"]


def test_numbers_inside_body_strings_count_as_grounded() -> None:
    allowed = allowed_numbers({"note": "The production below is 2025, at 14.3 per game."})
    assert ungrounded_numbers("Back in 2025 he averaged 14.3.", allowed) == []


async def test_ungrounded_number_is_rejected_and_the_original_survives(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "player")
    original = body["reasoning"]
    patched, rejected = apply_edits(
        "player",
        body,
        [ProseEdit(path="reasoning", text="He is averaging 31.7 points and a 44% target share.")],
    )
    assert len(rejected) == 1 and "ungrounded" in rejected[0]
    assert patched["reasoning"] == original


async def test_grounded_percentage_rendering_passes(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "player")
    snap = body["player"]["recent_weeks"][-1]["snap_pct"]
    text = f"He played {round(snap * 100)}% of the snaps last week."
    patched, rejected = apply_edits("player", body, [ProseEdit(path="reasoning", text=text)])
    assert rejected == []
    assert patched["reasoning"] == text


# -- the name guard ---------------------------------------------------------


def test_unknown_name_is_flagged_and_known_forms_are_not() -> None:
    body = {
        "players": [{"name": "Bijan Robinson"}, {"name": "Marvin Harrison Jr."}],
        "week": 4,
    }
    assert unknown_names("Travis Etienne steps into the role.", body) == ["Travis Etienne"]
    assert unknown_names("Bijan Robinson leads.", body) == []
    assert unknown_names("Marvin Harrison is the reach.", body) == []
    assert unknown_names("Start Bijan over the field.", body) == []
    assert unknown_names("Green Bay rests starters; Tampa Bay does not.", body) == []
    assert unknown_names("Red Zone Touches decide it.", body) == []
    assert unknown_names("Faces CIN, ranked 2 against RB.", body) == []


def test_recombined_tokens_are_not_a_known_name() -> None:
    """Sharing one word with one player and one with another is a hallucination.

    The first version pooled every body name's tokens and passed a pair if
    *either* word was in the pool, which let "Bijan Mahomes" through.
    """
    body = {
        "players": [
            {"name": "Bijan Robinson"},
            {"name": "Patrick Mahomes"},
            {"name": "Marvin Harrison Jr."},
        ]
    }
    assert unknown_names("Bijan Mahomes takes over.", body) == ["Bijan Mahomes"]
    assert unknown_names("Marvin Robinson is the reach.", body) == ["Marvin Robinson"]
    assert unknown_names("Bijan Smith is the reach.", body) == ["Bijan Smith"]
    # ...while every real form of a body name still passes.
    assert unknown_names("Bijan Robinson's usage is rising.", body) == []
    assert unknown_names("Harrison Jr. is the reach.", body) == []
    assert unknown_names("Patrick Mahomes and Bijan Robinson both start.", body) == []


#: A body naming the awkward shapes real NFL names take.
AWKWARD_BODY: dict[str, Any] = {
    "players": [
        {"name": "Bijan Robinson"},
        {"name": "CeeDee Lamb"},
        {"name": "DeVonta Smith"},
        {"name": "Christian McCaffrey"},
        {"name": "A.J. Brown"},
        {"name": "DK Metcalf"},
        {"name": "Dallas Goedert"},
        {"name": "Parker Washington"},
        {"name": "Brian Thomas Jr."},
        {"name": "Kenneth Walker III"},
        {"name": "Ja'Marr Chase"},
        {"name": "Amon-Ra St. Brown"},
        {"name": "Jaxon Smith-Njigba"},
        {"name": "Travis Kelce"},
    ]
}


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        # Invented names in the shapes the old [A-Z][a-z]+ pattern never saw.
        ("CeeDee Fakename is the start.", ["CeeDee Fakename"]),
        ("Stash DeVonta Madeup this week.", ["DeVonta Madeup"]),
        ("Ride DeVonta Madeup.", ["Ride DeVonta Madeup"]),  # an unlisted verb stays in the run
        ("Christian McFake takes over.", ["Christian McFake"]),
        ("Start A.J. Nobody over the field.", ["A.J. Nobody"]),
        ("Start DK Fakename over the field.", ["DK Fakename"]),
        ("Fade Jamarr Chasez.", ["Jamarr Chasez"]),
        # One team word no longer clears an invented name.
        ("Dallas Fakename is the waiver add.", ["Dallas Fakename"]),
        ("Grab Parker Nobody off waivers.", ["Parker Nobody"]),
        ("the move is Tampa Bay Fakename", ["Tampa Bay Fakename"]),
        # Whole-word matching: "Ian Thomas" is not inside "Brian Thomas Jr.".
        ("Ian Thomas is the reach.", ["Ian Thomas"]),
        ("Pivot to Ian Thomas now.", ["Ian Thomas"]),
        ("Kenneth Walker IV leads.", []),  # suffixes never decide a match
        ("Kenneth Walkerson leads.", ["Kenneth Walkerson"]),
        # Recombination is still caught across the new token shapes.
        ("CeeDee Smith is the reach.", ["CeeDee Smith"]),
        ("DK Brown is the reach.", ["DK Brown"]),
    ],
)
def test_invented_names_of_every_shape_are_flagged(text: str, flagged: list[str]) -> None:
    assert unknown_names(text, AWKWARD_BODY) == flagged


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        # A city word is a name word: it no longer carries a lone body first
        # or last name past the guard.
        ("Darnell Washington is the TE stream.", ["Darnell Washington"]),
        ("Jordan Washington is the add.", ["Jordan Washington"]),
        ("Travis Houston is the add.", ["Travis Houston"]),
        ("Denver Kelce is the add.", ["Denver Kelce"]),
    ],
)
def test_a_city_word_does_not_carry_a_lone_body_name(text: str, flagged: list[str]) -> None:
    body = {
        "players": [{"name": "Travis Kelce"}, {"name": "Darnell Mooney"}, {"name": "Jordan Love"}]
    }
    assert unknown_names(text, body) == flagged


def test_a_real_name_containing_a_city_word_still_passes() -> None:
    body = {"players": [{"name": "Darnell Washington"}, {"name": "Travis Kelce"}]}
    text = "Darnell Washington is the stream; Chiefs Travis Kelce and Chiefs Kelce sit."
    assert unknown_names(text, body) == []
    assert unknown_names("Kansas City Chiefs at Washington Commanders.", body) == []


@pytest.mark.parametrize(
    "text",
    [
        "Start Bijan Robinson over the field.",
        "CeeDee Lamb and DeVonta Smith both start.",
        "Christian McCaffrey's usage is rising.",
        "A.J. Brown is the WR1; AJ Brown again.",
        "Start DK Metcalf.",
        "Dallas Goedert is the TE add.",
        "Parker Washington is the waiver add.",
        "Brian Thomas Jr. leads; Brian Thomas is the pick. Thomas Jr. again.",
        "Kenneth Walker III and Walker III both.",
        "Ja'Marr Chase and Ja\u2019Marr Chase are one player.",
        "Amon-Ra St. Brown is the WR1. St. Brown again.",
        "Jaxon Smith-Njigba is rising.",
        "The Chiefs' Travis Kelce; Chiefs Travis Kelce; Chiefs Kelce.",
        "Lean Bijan Robinson this week.",
        "Faces Dallas on Sunday.",
        "The Bills host Green Bay.",
        "Kansas City Chiefs at Philadelphia Eagles.",
        "Monday Night Football decides it.",
        "Tampa Bay Buccaneers defense, New England Patriots and Los Angeles Rams.",
        "Faces CIN, ranked 2 against RB.",
        "Start DK Metcalf over the KC defense.",
        "Red Zone Touches decide it.",
        # Production board-gate false positives, 2026-09 and 2026-10: a
        # transaction gerund before a surname, and an injury described in caps.
        "Dropping Goedert is a mistake.",
        "Cutting Metcalf now sells low.",
        "Back from Neck Surgery; Ankle Sprain cleared; Knee ACL Surgery last year.",
    ],
)
def test_real_body_names_and_ordinary_prose_pass(text: str) -> None:
    assert unknown_names(text, AWKWARD_BODY) == []


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Dropping Josh Goedert is a mistake.", ["Josh Goedert"]),
        ("Cutting Bijan Mahomes now.", ["Bijan Mahomes"]),
        ("Back from Ankle Sprain, Travis Fakename starts.", ["Travis Fakename"]),
    ],
)
def test_a_gerund_or_injury_word_does_not_hide_an_invented_name(
    text: str, flagged: list[str]
) -> None:
    assert unknown_names(text, AWKWARD_BODY) == flagged


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.name)
async def test_realistic_rewrites_of_real_bodies_pass_the_name_guard(
    deterministic: DeterministicAnalysisEngine, case: GoldenCase
) -> None:
    """The deterministic prose, and a verb in front of every body name, are clean.

    A false positive drops an honest edit; across every golden body it must be
    zero, or the narrator silently stops narrating whole endpoints.
    """
    body = (await deterministic.analyze(case.endpoint_key, dict(case.request_context))).model_dump(
        mode="json"
    )
    for path in editable_paths(case.endpoint_key, body):
        container, key = _resolve(body, path)
        assert unknown_names(container[key], body) == [], path
    names: set[str] = set()
    _collect_names(body, names)
    for name in names:
        for text in (f"Start {name} this week.", f"Ride {name}.", f"{name}'s role grows."):
            assert unknown_names(text, body) == [], text


async def test_edit_naming_a_player_not_in_the_body_is_rejected(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "matchup")
    patched, rejected = apply_edits(
        "matchup",
        body,
        [ProseEdit(path="verdict", text="Start Bijan Robinson; Zamir White is the pivot.")],
    )
    assert len(rejected) == 1 and "Zamir White" in rejected[0]
    assert patched["verdict"] == body["verdict"]


# -- the whitelist at apply time --------------------------------------------


async def test_paths_outside_the_whitelist_are_rejected(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "trending")
    edits = [
        ProseEdit(path="players[0].verdict", text="fade"),
        ProseEdit(path="confidence", text="high"),
        ProseEdit(path="meta.model", text="gpt-9"),
        ProseEdit(path="stats_cited[0].value", text="99"),
        ProseEdit(path="players[].analysis", text="wildcard literal"),
    ]
    patched, rejected = apply_edits("trending", body, edits)
    assert len(rejected) == len(edits)
    assert patched == body


async def test_out_of_range_index_and_empty_text_are_rejected(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "trending")
    count = len(body["players"])
    patched, rejected = apply_edits(
        "trending",
        body,
        [
            ProseEdit(path=f"players[{count}].analysis", text="Nobody lives here."),
            ProseEdit(path="players[0].analysis", text="   "),
        ],
    )
    assert len(rejected) == 2
    assert "does not resolve" in rejected[0]
    assert "empty" in rejected[1]
    assert patched == body


# -- the engine --------------------------------------------------------------


@pytest.mark.parametrize("endpoint_key", ENDPOINT_KEYS)
async def test_edits_are_applied_on_every_endpoint(
    deterministic: DeterministicAnalysisEngine, endpoint_key: str
) -> None:
    case = CASES_BY_NAME[CASE_PER_ENDPOINT[endpoint_key]]
    reasoning = "The usage says start; the crowd is late."
    if endpoint_key == "draft_board":
        reasoning += " Market rank is popularity, not a consensus ADP."
    verdict = "Buy the role, not the name."
    if endpoint_key == "player":
        # The archived start/sit call is read from this sentence; keep it.
        original = await deterministic.analyze(endpoint_key, dict(case.request_context))
        verdict = f"{original.verdict} {verdict}"
    narrator = Scripted(
        [
            ProseEdit(path="verdict", text=verdict),
            ProseEdit(path="reasoning", text=reasoning),
        ]
    )
    engine = NarratedAnalysisEngine(deterministic, narrator, settings_for())

    response = await engine.analyze(endpoint_key, dict(case.request_context))

    assert isinstance(response, RESPONSE_MODELS[endpoint_key])
    assert response.verdict == verdict
    assert response.reasoning == reasoning
    assert response.meta.model == MODEL_ID
    assert engine.degraded == 0 and engine.rejected == 0
    called_key, called_body, called_ctx = narrator.calls[0]
    assert called_key == endpoint_key
    assert called_body["verdict"] != response.verdict, "the narrator saw the original"
    assert called_ctx == dict(case.request_context)


async def test_everything_but_prose_survives_narration(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    case = CASES_BY_NAME["matchup_two_players"]
    before = (await deterministic.analyze("matchup", dict(case.request_context))).model_dump(
        mode="json"
    )
    engine = NarratedAnalysisEngine(
        deterministic,
        Scripted([ProseEdit(path="ranked[0].projection_note", text="Start him with confidence.")]),
        settings_for(),
    )
    after = (await engine.analyze("matchup", dict(case.request_context))).model_dump(mode="json")

    assert after["ranked"][0]["projection_note"] == "Start him with confidence."
    after["ranked"][0]["projection_note"] = before["ranked"][0]["projection_note"]
    # The two provenance fields that legitimately change: which model wrote
    # the prose, and which engine produced the body.
    assert after["meta"]["model"] == MODEL_ID and after["meta"]["engine"] == "narrated"
    after["meta"]["model"] = None
    after["meta"]["engine"] = "deterministic"
    after["meta"]["generated_at"] = before["meta"]["generated_at"]
    assert after == before


async def test_timeout_serves_the_deterministic_body(
    deterministic: DeterministicAnalysisEngine, caplog: pytest.LogCaptureFixture
) -> None:
    engine = NarratedAnalysisEngine(
        deterministic, Sleeping(), settings_for(narrator_timeout_seconds=0.01)
    )
    case = CASES_BY_NAME["player_by_name"]

    response = await engine.analyze("player", dict(case.request_context))

    assert response.meta.model is None
    assert response.verdict
    assert engine.degraded == 1
    # TimeoutError's message is empty; the log must still say what happened.
    assert "TimeoutError: timed out" in caplog.text


async def test_a_narrator_exception_serves_the_deterministic_body(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    engine = NarratedAnalysisEngine(deterministic, Exploding(), settings_for())
    case = CASES_BY_NAME["player_by_name"]

    response = await engine.analyze("player", dict(case.request_context))

    assert response.meta.model is None
    assert engine.degraded == 1


async def test_meta_model_is_claimed_only_when_an_edit_applied(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    """Every edit rejected means the payer got the deterministic body — say so."""
    engine = NarratedAnalysisEngine(
        deterministic,
        Scripted([ProseEdit(path="reasoning", text="He scored 99.9 points, trust me.")]),
        settings_for(),
    )
    case = CASES_BY_NAME["player_by_name"]

    response = await engine.analyze("player", dict(case.request_context))

    assert response.meta.model is None
    assert engine.rejected == 1 and engine.degraded == 0
    assert "99.9" not in response.reasoning


async def test_an_empty_edit_list_leaves_the_body_untouched(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    engine = NarratedAnalysisEngine(deterministic, Scripted([]), settings_for())
    case = CASES_BY_NAME["player_by_name"]
    response = await engine.analyze("player", dict(case.request_context))
    assert response.meta.model is None


async def test_get_engine_builds_the_narrated_engine(seeded: Store) -> None:
    from api.agents.narrator import GeminiNarrator

    engine = get_engine(settings=settings_for(), store=seeded)

    assert isinstance(engine, NarratedAnalysisEngine)
    assert engine.name == "narrated"
    assert isinstance(engine.deterministic, DeterministicAnalysisEngine)
    assert isinstance(engine.narrator, GeminiNarrator)


async def test_aclose_closes_the_deterministic_engine(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    closed: list[bool] = []

    class Closing(DeterministicAnalysisEngine):
        async def aclose(self) -> None:
            closed.append(True)

    inner = Closing(store=MemoryStore(), settings=settings_for(engine="deterministic"))
    await NarratedAnalysisEngine(inner, Scripted(), settings_for()).aclose()
    assert closed == [True]


# -- prompt and parsing -----------------------------------------------------


async def test_prompt_lists_the_paths_and_omits_meta(
    deterministic: DeterministicAnalysisEngine,
) -> None:
    body = await body_for(deterministic, "sleepers")
    prompt = build_prompt("sleepers", body, {"week": WEEK, "limit": 12})

    for path in editable_paths("sleepers", body):
        assert f"- {path}" in prompt
    assert '"generated_at"' not in prompt, "meta is the engine's, not the model's"
    assert "Never name a player who is not named in the body" in prompt
    assert "disagrees with the crowd" in prompt
    assert '"edits"' in prompt


def test_prompt_has_its_own_role_and_forbids_outside_facts() -> None:
    prompt = build_prompt("player", {"verdict": "x", "reasoning": "y"}, {})
    assert "news findings" not in prompt, "a narrated run has no news; do not invite any"
    assert "State no fact that is not in the body" in prompt
    for kind in ("injuries", "news", "quotes", "weather", "projections"):
        assert kind in prompt


def test_prompt_truncates_a_huge_request_context() -> None:
    prompt = build_prompt("roster", {"verdict": "x", "reasoning": "y"}, {"roster": ["p"] * 5000})
    assert "...(truncated)" in prompt
    assert len(prompt) < 10_000


def test_parse_edits_accepts_wrapper_bare_list_and_nothing() -> None:
    assert parse_edits('{"edits": [{"path": "verdict", "text": "Go."}]}') == [
        ProseEdit(path="verdict", text="Go.")
    ]
    assert parse_edits('[{"path": "verdict", "text": "Go."}]') == [
        ProseEdit(path="verdict", text="Go.")
    ]
    assert parse_edits("") == []
    assert parse_edits(None) == []


def test_narrated_is_a_valid_engine_setting() -> None:
    assert settings_for().engine == "narrated"
    assert settings_for().narrator_timeout_seconds == 20.0


async def test_meta_names_the_engine_that_wrote_the_body() -> None:
    """``meta.engine`` is how the value gate tells a narrated body from an ADK one."""
    store = await seed_store(MemoryStore())
    case = CASES_BY_NAME["player_by_name"]
    deterministic = DeterministicAnalysisEngine(store=store, settings=settings_for())
    base = await deterministic.analyze("player", dict(case.request_context))
    assert base.meta.engine == "deterministic" and base.meta.model is None

    edits = [ProseEdit(path="verdict", text="Start him.")]
    engine = NarratedAnalysisEngine(deterministic, Scripted(edits), settings_for())
    narrated = await engine.analyze("player", dict(case.request_context))
    assert narrated.meta.engine == "narrated" and narrated.meta.model == MODEL_ID

    untouched = await NarratedAnalysisEngine(deterministic, Scripted([]), settings_for()).analyze(
        "player", dict(case.request_context)
    )
    assert untouched.meta.engine == "deterministic" and untouched.meta.model is None


@pytest.mark.parametrize(
    ("before", "after", "rejected"),
    [
        ("Start Bijan Robinson with confidence.", "Bijan Robinson is a clear start.", False),
        ("Start Bijan Robinson with confidence.", "Do not bench Bijan Robinson.", True),
        ("Bijan Robinson: hold, no clear edge.", "Bijan Robinson is a fringe starter.", True),
        ("Bench Bijan Robinson this week.", "Sit Bijan Robinson this week.", False),
    ],
)
def test_a_player_verdict_edit_keeps_its_start_sit_call(
    before: str, after: str, rejected: bool
) -> None:
    body = {"verdict": before, "reasoning": "x", "player": {"name": "Bijan Robinson"}}
    _, rejections = apply_edits("player", body, [ProseEdit(path="verdict", text=after)])
    assert bool(rejections) is rejected


def test_a_first_name_that_is_also_a_word_still_names_a_player() -> None:
    body = {"verdict": "x", "player": {"name": "Bijan Robinson"}}
    assert unknown_names("Will Levis is the better streamer.", body) == ["Will Levis"]
    assert unknown_names("Pierre Strong is a sleeper.", body) == ["Pierre Strong"]
    assert unknown_names("Will he start? Bijan Robinson is fine.", body) == []
