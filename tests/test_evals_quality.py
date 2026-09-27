"""The value gate: rules that ask whether an answer is worth paying for.

Each rule is exercised on a minimal body that fails it and one that passes,
plus the fixture-backed bodies the deterministic engine actually produces,
which must pass every rule — the gate is a bar the shipped engine clears,
not an aspiration. The failing bodies are modelled on what the live Week 1
boards looked like on 2026-09-03, because those are the shapes that got
through the old suite.
"""

from __future__ import annotations

from typing import Any

import pytest

from api.core.config import ENDPOINT_KEYS
from api.core.store import MemoryStore
from api.data.stats_store import PLAYERS_COLLECTION, TRENDING_COLLECTION
from api.evals.golden import CONSENSUS_IDS, GOLDEN_CASES, PLAYERS, seed_store
from api.evals.quality import (
    CONSENSUS_ADD_COUNT,
    NAMED_ROWS,
    check_board_quality,
    check_quality,
    cites_beyond_the_crowd,
    confidence_consistent,
    consensus_from_store,
    disagrees_with_the_crowd,
    names_grounded,
    no_code_artifacts,
    sources_resolved,
)
from api.evals.run_evals import build_engine


def _cite(stat: str, value: Any = 1) -> dict[str, Any]:
    return {"stat": stat, "value": value, "player": "X", "source": "nflverse usage_trends/1"}


# -- every endpoint has a named-rows entry ---------------------------------


def test_named_rows_cover_every_endpoint() -> None:
    assert set(NAMED_ROWS) == set(ENDPOINT_KEYS)


# -- the shipped engine clears the bar --------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda c: c.name)
async def test_deterministic_bodies_pass_every_rule(case: Any) -> None:
    store = await seed_store(MemoryStore())
    engine = await build_engine("deterministic", store)
    response = await engine.analyze(case.endpoint_key, dict(case.request_context))
    failures = check_quality(
        case.endpoint_key,
        response.model_dump(mode="json"),
        known_ids=set(PLAYERS),
        consensus_ids=CONSENSUS_IDS,
    )
    assert failures == []


# -- no_code_artifacts -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Deterministic engine — heuristic analysis from ingested stats.",
        "the lineup fills {'QB': 1, 'RB': 2} plus 1 flex",
        "snap_pct_l4w is 0.82",
    ],
)
def test_code_artifacts_in_prose_fail(text: str) -> None:
    body = {"verdict": "ok", "reasoning": text, "meta": {"model": None}}
    failures = no_code_artifacts(body)
    assert len(failures) == 1
    assert "reasoning" in failures[0]


def test_artifacts_are_tolerated_in_provenance_and_citations() -> None:
    """``stats_cited`` legitimately carries raw stat names like ``snap_pct_l4w``."""
    body = {
        "verdict": "ok",
        "reasoning": "clean",
        "stats_cited": [_cite("snap_pct_l4w", 0.82)],
        "meta": {"model": None, "note": "{'x': 1}"},
    }
    assert no_code_artifacts(body) == []


# -- names_grounded ----------------------------------------------------------


def test_a_beneficiary_with_no_player_id_is_a_hallucination() -> None:
    """The live Week 1 report named 'Travis Etienne Jr.' with ``player_id: null``."""
    body = {
        "injury_fallout": [
            {
                "injured_player": "Alvin Kamara",
                "beneficiaries": [
                    {"name": "Travis Etienne Jr.", "player_id": None, "position": "RB"}
                ],
            }
        ]
    }
    failures = names_grounded("report", body)
    assert len(failures) == 1
    assert "Travis Etienne Jr." in failures[0]
    assert "no player_id" in failures[0]


def test_an_unknown_player_id_fails_when_the_universe_is_known() -> None:
    body = {"picks": [{"name": "Nobody Real", "player_id": "999999"}]}
    assert names_grounded("sleepers", body, known_ids={"1001"}) != []
    assert names_grounded("sleepers", body, known_ids=None) == []


def test_nested_paths_visit_every_row() -> None:
    body = {
        "tiers": [
            {"tier": 1, "players": [{"name": "A", "player_id": "1"}]},
            {"tier": 2, "players": [{"name": "B", "player_id": ""}]},
        ]
    }
    failures = names_grounded("draft_board", body)
    assert len(failures) == 1
    assert "'B'" in failures[0]


# -- confidence_consistent ---------------------------------------------------


def test_a_pick_cannot_be_more_confident_than_the_verdict() -> None:
    body = {
        "confidence": "low",
        "picks": [{"name": "Malik Davis", "confidence": "medium", "usage_note": "ok"}],
    }
    failures = confidence_consistent(body)
    assert len(failures) == 1
    assert "exceeds" in failures[0]


def test_usage_not_available_forces_low() -> None:
    body = {
        "confidence": "high",
        "picks": [
            {
                "name": "Devaughn Vele",
                "confidence": "medium",
                "usage_note": "In-season snap share and target share are not available.",
            }
        ],
    }
    failures = confidence_consistent(body)
    assert len(failures) == 1
    assert "not available" in failures[0]


def test_consistent_confidence_passes() -> None:
    body = {
        "confidence": "medium",
        "picks": [{"name": "A", "confidence": "low", "usage_note": "rising"}],
    }
    assert confidence_consistent(body) == []


# -- cites_beyond_the_crowd --------------------------------------------------


def test_a_board_built_on_add_counts_alone_fails() -> None:
    """The live Week 1 trending board: 50 rows, one non-crowd citation."""
    body = {
        "players": [{"name": f"P{i}", "player_id": str(i)} for i in range(6)],
        "stats_cited": [_cite("trend_count", 100)] * 5 + [_cite("rz_touches_l4w", 17)],
    }
    failures = cites_beyond_the_crowd("trending", body)
    assert len(failures) == 1
    assert "free preview" in failures[0]


def test_a_board_that_cites_usage_passes() -> None:
    body = {
        "players": [{"name": f"P{i}", "player_id": str(i)} for i in range(6)],
        "stats_cited": [
            _cite("trend_count", 100),
            _cite("snap_pct_l4w"),
            _cite("target_share_l4w"),
            _cite("rz_touches_l4w"),
        ],
    }
    assert cites_beyond_the_crowd("trending", body) == []


def test_small_boards_and_non_boards_are_exempt() -> None:
    small = {"players": [{"name": "A", "player_id": "1"}], "stats_cited": []}
    assert cites_beyond_the_crowd("trending", small) == []
    assert cites_beyond_the_crowd("player", {"stats_cited": []}) == []


# -- disagrees_with_the_crowd ------------------------------------------------


def test_trending_that_follows_every_move_fails() -> None:
    rows = [{"name": f"P{i}", "trend": "add", "verdict": "add"} for i in range(5)]
    failures = disagrees_with_the_crowd("trending", {"players": rows})
    assert len(failures) == 1
    assert "agrees with the crowd" in failures[0]


def test_trending_with_one_fade_passes() -> None:
    rows = [{"name": f"P{i}", "trend": "add", "verdict": "add"} for i in range(4)]
    rows.append({"name": "Chase Brown", "trend": "add", "verdict": "fade"})
    assert disagrees_with_the_crowd("trending", {"players": rows}) == []


def test_a_buy_low_on_a_drop_counts_as_disagreeing() -> None:
    rows = [{"name": f"P{i}", "trend": "add", "verdict": "add"} for i in range(4)]
    rows.append({"name": "Breece Hall", "trend": "drop", "verdict": "add"})
    assert disagrees_with_the_crowd("trending", {"players": rows}) == []


def test_waivers_in_exact_add_count_order_fail() -> None:
    board = [{"player_id": str(i), "trend_count": 1000 - i} for i in range(5)]
    failures = disagrees_with_the_crowd("waivers", {"board": board})
    assert len(failures) == 1
    assert "add-count order" in failures[0]


def test_waivers_that_move_one_player_pass() -> None:
    board = [{"player_id": str(i), "trend_count": 1000 - i} for i in range(5)]
    board[0], board[1] = board[1], board[0]
    assert disagrees_with_the_crowd("waivers", {"board": board}) == []


def test_sleepers_drawn_from_the_consensus_fail() -> None:
    """Ten of the twelve live Week 1 picks were players the crowd had already added."""
    body = {"reasoning": "x", "picks": [{"name": "Malik Davis", "player_id": "8800"}] * 8}
    failures = disagrees_with_the_crowd("sleepers", body, consensus_ids={"8800"})
    assert len(failures) == 1
    assert "already claimed" in failures[0]


def test_a_short_sleepers_board_must_explain_itself() -> None:
    picks = [{"name": "A", "player_id": "1"}] * 3
    silent = {"reasoning": "Three picks.", "picks": picks}
    honest = {"reasoning": "Only 3 candidates cleared the bar this week.", "picks": picks}
    assert disagrees_with_the_crowd("sleepers", silent) != []
    assert disagrees_with_the_crowd("sleepers", honest) == []


def test_emerging_that_lists_the_most_added_players_fails() -> None:
    """The live Week 1 report's 'emerging' section was the top three adds."""
    body = {"emerging": [{"name": "Jacob Saylors", "player_id": "11237"}]}
    failures = disagrees_with_the_crowd("report", body, consensus_ids={"11237"})
    assert len(failures) == 1
    assert "before the crowd" in failures[0]


# -- sources_resolved --------------------------------------------------------


def test_grounding_redirects_and_bare_domains_fail() -> None:
    body = {
        "sources": [
            {
                "title": "footballnationusa.com",
                "url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ",
            }
        ]
    }
    failures = sources_resolved(body)
    assert len(failures) == 2


def test_a_real_citation_passes() -> None:
    body = {
        "sources": [
            {"title": "Lions place Pacheco on IR", "url": "https://www.nfl.com/news/lions-ir"}
        ]
    }
    assert sources_resolved(body) == []


# -- entry points ------------------------------------------------------------


def test_check_quality_prefixes_and_aggregates() -> None:
    body = {
        "verdict": "ok",
        "reasoning": "Deterministic engine — x",
        "confidence": "low",
        "picks": [{"name": "A", "player_id": "", "confidence": "high", "usage_note": "ok"}],
    }
    failures = check_quality("sleepers", body)
    assert failures and all(f.startswith("QUALITY: ") for f in failures)
    assert any("code artifact" in f for f in failures)
    assert any("no player_id" in f for f in failures)
    assert any("exceeds" in f for f in failures)


async def test_consensus_from_store_reads_the_live_trending_poll() -> None:
    store = MemoryStore()
    await store.set(
        TRENDING_COLLECTION,
        "add",
        {
            "kind": "add",
            "entries": [
                {"player_id": "1", "count": CONSENSUS_ADD_COUNT},
                {"player_id": "2", "count": CONSENSUS_ADD_COUNT - 1},
            ],
        },
    )
    assert await consensus_from_store(store) == {"1"}


async def test_check_board_quality_resolves_ids_against_the_store() -> None:
    """A plausible-looking id that is not in ``players`` is still a hallucination."""
    store = MemoryStore()
    await store.set(PLAYERS_COLLECTION, "1", {"player_id": "1", "name": "Real Player"})
    body = {
        "verdict": "ok",
        "reasoning": "fine",
        "confidence": "medium",
        "board": [
            {"name": "Real Player", "player_id": "1", "trend_count": 5},
            {"name": "Invented Player", "player_id": "424242", "trend_count": 4},
        ],
        "stats_cited": [],
    }
    failures = await check_board_quality(store, "waivers", body)
    assert any("Invented Player" in f for f in failures)
    assert not any("Real Player" in f for f in failures)


# -- numbers_grounded -----------------------------------------------------------


from api.evals.quality import numbers_grounded  # noqa: E402


def _model_body(**fields: Any) -> dict[str, Any]:
    return {"meta": {"model": "gemini-3.7-flash"}, **fields}


def test_a_number_in_a_row_note_must_be_cited() -> None:
    """The judge's remaining critique after the second warm, three boards over."""
    body = _model_body(
        players=[
            {
                "name": "Jacob Saylors",
                "player_id": "1",
                "trend_count": 138055,
                "analysis": "Empty volume: a 0.0125 snap rate and 0 red zone touches.",
            }
        ],
        stats_cited=[_cite("trend_count", 138055)],
    )
    failures = numbers_grounded(body)
    assert len(failures) == 1
    assert "players[0].analysis quotes 0.0125" in failures[0]


def test_a_cited_number_and_its_percent_rendering_pass() -> None:
    body = _model_body(
        players=[
            {
                "name": "X",
                "player_id": "1",
                "trend_count": 100,
                "analysis": "A 38% snap share and 4 red-zone touches on 100 adds.",
            }
        ],
        stats_cited=[_cite("snap_pct_l4w", 0.38), _cite("rz_touches_l4w", 4)],
    )
    assert numbers_grounded(body) == []


def test_a_citation_whose_value_is_a_string_still_grounds_its_number() -> None:
    """``StatCitation.value`` may be a string; "0.245" grounds "24.5%" like 0.245 does."""
    body = _model_body(
        players=[
            {
                "name": "X",
                "player_id": "1",
                "trend_count": 100,
                "analysis": "A 24.5% target share on 100 adds.",
            }
        ],
        stats_cited=[_cite("target_share_l4w", "0.245")],
    )
    assert numbers_grounded(body) == []


@pytest.mark.parametrize(
    ("cited", "grounded"),
    [("24.5%", True), ("0.245%", False), ("NaN", False), ("Infinity", False)],
)
def test_string_citations_keep_their_scale_and_skip_non_numbers(cited: str, grounded: bool) -> None:
    body = _model_body(
        players=[{"name": "X", "player_id": "1", "analysis": "A 24.5% target share."}],
        stats_cited=[_cite("target_share_l4w", cited)],
    )
    assert (numbers_grounded(body) == []) is grounded


def test_body_leaves_years_and_small_integers_need_no_citation() -> None:
    body = _model_body(
        week=4,
        ranked=[
            {
                "name": "X",
                "player_id": "1",
                "def_vs_pos_rank": 28,
                "projection_note": "Ranks 28 against RBs in week 4 of the 2026 season, top 5.",
            }
        ],
        stats_cited=[],
    )
    assert numbers_grounded(body) == []


def test_source_titles_and_provenance_are_not_checked() -> None:
    body = _model_body(
        verdict="Fine.",
        sources=[{"title": "Week 1 waiver wire: 15 adds", "url": "https://x/17"}],
        stats_cited=[],
    )
    body["meta"]["data_freshness"] = {"trending": "2026-09-03T14:30:48Z"}
    assert numbers_grounded(body) == []


def test_the_deterministic_engine_is_exempt() -> None:
    """Its prose carries numbers it computed (an average); the model's may not."""
    body = {"meta": {"model": None}, "reasoning": "Averaged 22.0 points across 3 games."}
    assert numbers_grounded(body) == []


def test_failures_are_capped_with_a_count() -> None:
    body = _model_body(
        reasoning=" ".join(f"{n}.{n}5 points" for n in range(20, 40)), stats_cited=[]
    )
    failures = numbers_grounded(body)
    assert len(failures) == 9 and failures[-1].endswith("more ungrounded number(s)")


def test_check_quality_runs_the_rule() -> None:
    body = _model_body(verdict="Start him.", reasoning="A 0.9775 snap rate.", stats_cited=[])
    assert any("0.9775" in f for f in check_quality("player", body))


def test_the_grounding_rule_is_scoped_to_adk_bodies() -> None:
    """Narrated notes carry numbers the deterministic engine derived, not cited."""
    prose = {"reasoning": "A 0.9775 snap rate.", "stats_cited": []}
    assert numbers_grounded({"meta": {"model": "m", "engine": "adk"}, **prose})
    assert numbers_grounded({"meta": {"model": "m"}, **prose}), "pre-field cached bodies"
    assert numbers_grounded({"meta": {"model": "m", "engine": "narrated"}, **prose}) == []
    assert numbers_grounded({"meta": {"model": None, "engine": "deterministic"}, **prose}) == []


# -- computed prose -------------------------------------------------------------
#
# The gate's number rule assumes every string is model prose. team_report has
# fields that are not: api.agents.pipeline.enforce_computed_facts overwrites
# them with the analytics block, so their numbers are grounded by construction.
# Before this, the gate failed a correct team report on every model tested.


def _team_report(**review: object) -> dict:
    return {
        "meta": {"model": "gemini-3.7-flash", "engine": "adk"},
        "verdict": "Your lineup decisions are costing you a win.",
        "stats_cited": [],
        "manager_review": {
            "luck_note": "Third in points for, fifth in the standings — the schedule cost a win.",
            "mis_start_patterns": [
                "Benched the higher-projected TE in two of three weeks (-11.4)."
            ],
            **review,
        },
    }


def test_computed_team_report_fields_need_no_citation() -> None:
    """-11.4 is computed by team_analytics and copied through, not quoted."""
    assert numbers_grounded(_team_report(), "team_report") == []


def test_the_same_numbers_still_fail_in_the_models_own_prose() -> None:
    """The exemption is per path, not per number: observations are model-written."""
    body = _team_report(observations=["You leave about -11.4 points on the bench each week."])
    failures = numbers_grounded(body, "team_report")
    assert any("observations" in f and "-11.4" in f for f in failures)


def test_the_exemption_does_not_leak_to_other_endpoints() -> None:
    body = dict(_team_report())
    assert numbers_grounded(body, "report") != []
    assert numbers_grounded(body) != [], "no endpoint given = the stricter reading"


def test_a_renamed_add_count_is_still_the_crowd() -> None:
    """An ADK body names its own citations; ``adds`` is ``trend_count`` renamed."""
    body = {
        "players": [{"name": f"P{i}", "player_id": str(i)} for i in range(6)],
        "stats_cited": [_cite("adds", 100), _cite("sleeper_add_count", 90), _cite("drops", 3)],
    }
    assert cites_beyond_the_crowd("trending", body)


# -- names in model prose ----------------------------------------------------

from api.evals.quality import prose_names_grounded  # noqa: E402


def test_a_player_invented_in_adk_prose_fails() -> None:
    """A reasoning paragraph has no row for names_grounded to catch."""
    body = {
        "meta": {"model": "gemini-3.7-flash", "engine": "adk"},
        "reasoning": "Fade Bijan Robinson; Travis Etienne inherits the work.",
        "players": [{"name": "Bijan Robinson", "player_id": "1001", "analysis": "Rising usage."}],
        "stats_cited": [_cite("snap_pct", 0.8)],
    }
    failures = prose_names_grounded(body, "trending")
    assert failures == ["reasoning names 'Travis Etienne', whom the body never returned"]
    assert any("Travis Etienne" in f for f in check_quality("trending", body))


def test_a_cited_player_and_team_names_may_appear_in_prose() -> None:
    body = {
        "meta": {"model": "gemini-3.7-flash", "engine": "adk"},
        "reasoning": "Start Bijan Robinson over Nico Collins against the Kansas City Chiefs.",
        "players": [{"name": "Bijan Robinson", "player_id": "1001"}],
        "stats_cited": [
            {"stat": "snap_pct", "value": 0.8, "player": "Nico Collins", "source": "x"}
        ],
    }
    assert prose_names_grounded(body, "trending") == []


def test_prose_names_are_checked_only_in_adk_bodies() -> None:
    prose = {"reasoning": "Travis Etienne inherits the work.", "players": []}
    assert prose_names_grounded({"meta": {"model": "m", "engine": "adk"}, **prose})
    assert prose_names_grounded({"meta": {"model": "m"}, **prose}), "pre-engine-field ADK body"
    assert prose_names_grounded({"meta": {"model": "m", "engine": "narrated"}, **prose}) == []
    assert prose_names_grounded({"meta": {"model": None, "engine": "deterministic"}, **prose}) == []


def test_source_titles_are_not_our_prose() -> None:
    body = {
        "meta": {"model": "m", "engine": "adk"},
        "sources": [{"title": "Travis Etienne Signs Extension", "url": "https://x.test/a"}],
    }
    assert prose_names_grounded(body, "player") == []
