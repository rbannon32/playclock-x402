"""The prediction archive: what gets filed, that it files once, and that it never bites.

Three things can go wrong with an archive of claims, and each has a test group:

- it files the wrong claim (or a claim from an endpoint that makes none), so
  the hit rate later measures something nobody said;
- a re-warm rewrites yesterday's call with today's, so the archive scores
  hindsight;
- the bookkeeping raises into the paid path and a caller loses an answer they
  have already paid for.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from api.agents.deterministic import DeterministicAnalysisEngine
from api.core.clock import set_clock
from api.core.config import Settings
from api.core.store import MemoryStore, Store
from api.data.predictions import (
    KINDS,
    PREDICTIONS_COLLECTION,
    UNSCORED_ENDPOINTS,
    WAIVER_TOP_N,
    archive_predictions,
    claim_deadline,
    extract_predictions,
    legacy_prediction_ids,
    prediction_id,
    record_predictions,
    verdict_call,
)
from api.data.stats_store import SCHEDULES_COLLECTION
from api.evals.golden import GOLDEN_CASES, SEASON, WEEK, seed_store
from api.evals.run_evals import eval_settings
from api.routes.paid import clear_inflight
from ingest.precompute import warm_response_cache
from tests.test_routes_free import api_client, configure


def _row(player_id: str, **fields: Any) -> dict[str, Any]:
    return {"player_id": player_id, "name": f"Player {player_id}", "position": "RB", **fields}


# --------------------------------------------------------------------------
# Reading a call out of a player verdict
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("Bijan Robinson (RB, ATL): buy the usage, start with confidence in week 4.", "start"),
        ("Breece Hall (RB, NYJ): matchup-based start in week 4.", "start"),
        ("Cam Ward (QB, TEN): starting QB for TEN, a real week 1 option in week 1.", "start"),
        (
            "Tyjae Spears (RB, TEN): role and matchup are both against him, bench him in week 4.",
            "sit",
        ),
        ("Bhayshul Tuten (RB, JAX): backup RB behind the starter, a bench stash in week 1.", "sit"),
        ("Sione Vaki (RB, DET): number 3 at RB, not startable yet in week 1.", "sit"),
        ("Rome Odunze (WR, CHI): hold, no clear edge this week in week 4.", None),
        ("Jordan Addison (WR, MIN): usage is shrinking, treat as a downgrade in week 4.", None),
        ("Nobody: not enough ingested data to make a call in week 4.", None),
        ("Rome Odunze (WR, CHI): don't start him in week 4.", "sit"),
        ("Rome Odunze (WR, CHI): fade the hype, a tough week 4.", "sit"),
        ("", None),
    ],
)
def test_verdict_call_reads_the_deterministic_vocabulary(
    verdict: str, expected: str | None
) -> None:
    """ "behind the starter" and "not startable" both contain "start" — sit words win."""
    assert verdict_call(verdict) == expected


# --------------------------------------------------------------------------
# Extraction, endpoint by endpoint
# --------------------------------------------------------------------------


def test_player_files_the_verdict_call() -> None:
    body = {"verdict": "X (RB, ATL): matchup-based start in week 4.", "player": _row("1001")}

    docs = extract_predictions("player", body, season=2026, week=4)

    assert [(d["kind"], d["player_id"], d["name"], d["position"]) for d in docs] == [
        ("start", "1001", "Player 1001", "RB")
    ]


def test_player_hold_is_no_claim() -> None:
    body = {"verdict": "X (WR, CHI): hold, no clear edge this week.", "player": _row("1008")}
    assert extract_predictions("player", body, season=2026, week=4) == []


def test_matchup_files_the_winner_against_the_whole_group() -> None:
    body = {
        "ranked": [
            _row("1002", rank=2),
            _row("1001", rank=1),
            {"player_id": "", "name": "Unknown", "position": "RB", "rank": 3},
        ]
    }

    docs = extract_predictions("matchup", body, season=2026, week=4)

    assert len(docs) == 1
    assert docs[0]["kind"] == "matchup_top"
    assert docs[0]["player_id"] == "1001"
    assert docs[0]["group"] == ["1001", "1002"]


def test_matchup_with_an_unresolved_winner_or_a_group_of_one_is_not_a_claim() -> None:
    unresolved = {"ranked": [{"player_id": "", "name": "?", "rank": 1}, _row("1002", rank=2)]}
    alone = {"ranked": [_row("1001", rank=1), {"player_id": "", "name": "?", "rank": 2}]}

    assert extract_predictions("matchup", unresolved, season=2026, week=4) == []
    assert extract_predictions("matchup", alone, season=2026, week=4) == []


def test_a_matchup_whose_top_player_is_sat_is_not_a_claim() -> None:
    """Everyone out or on bye: the rank-1 player has no game to out-score anyone in."""
    body = {"ranked": [_row("1001", rank=1, call="sit"), _row("1002", rank=2, call="sit")]}
    assert extract_predictions("matchup", body, season=2026, week=4) == []


def test_roster_maps_every_start_sit_call() -> None:
    body = {
        "start_sit": [
            _row("1", call="start"),
            _row("2", call="flex"),
            _row("3", call="sit"),
            _row("4", call="bench"),
        ]
    }

    docs = extract_predictions("roster", body, season=2026, week=4)

    assert [(d["player_id"], d["kind"]) for d in docs] == [
        ("1", "start"),
        ("2", "start"),
        ("3", "sit"),
        ("4", "sit"),
    ]


def test_trending_files_add_and_fade_but_not_hold() -> None:
    body = {
        "players": [_row("1", verdict="add"), _row("2", verdict="hold"), _row("3", verdict="fade")]
    }

    docs = extract_predictions("trending", body, season=2026, week=4)

    assert [(d["player_id"], d["kind"]) for d in docs] == [("1", "add"), ("3", "fade")]


def test_sleepers_files_every_pick() -> None:
    body = {"picks": [_row("1"), _row("2"), _row("3")]}
    docs = extract_predictions("sleepers", body, season=2026, week=4)
    assert [d["kind"] for d in docs] == ["sleeper"] * 3


def test_waivers_files_the_first_startable_rows_only() -> None:
    """Stashes are not "start him" claims; the cap applies after they are skipped."""
    labels = ["start", "stash", "streamer", "start", "start", "start", "start", "start"]
    body = {
        "board": [
            _row(str(rank), rank=rank, stash_or_start=label)
            for rank, label in enumerate(labels, start=1)
        ]
    }

    docs = extract_predictions("waivers", body, season=2026, week=4)

    assert len(docs) == WAIVER_TOP_N
    assert [d["player_id"] for d in docs] == ["1", "3", "4", "5", "6"]
    assert {d["kind"] for d in docs} == {"waiver"}


def test_report_files_emerging_callouts_that_resolved() -> None:
    body = {
        "emerging": [
            _row("1"),
            {"player_id": None, "name": "Unresolved Name", "position": "WR"},
            _row("2"),
        ],
        "stock_up": [_row("9")],
    }

    docs = extract_predictions("report", body, season=2026, week=4)

    assert [d["player_id"] for d in docs] == ["1", "2"]
    assert {d["kind"] for d in docs} == {"emerging"}


@pytest.mark.parametrize("endpoint_key", sorted(UNSCORED_ENDPOINTS))
def test_season_scoped_and_narrated_endpoints_make_no_claim(endpoint_key: str) -> None:
    body = {
        "verdict": "start everyone",
        "roster": [_row("1")],
        "tiers": [{"tier": 1, "players": [_row("1")]}],
        "start_sit": [_row("1", call="start")],
        "deficiencies": [{"position": "RB", "available_fixes": [_row("2")]}],
    }
    assert extract_predictions(endpoint_key, body, season=2026, week=4) == []


def test_unknown_endpoints_and_non_dict_bodies_make_no_claim() -> None:
    assert extract_predictions("nope", {"players": [_row("1")]}, season=2026, week=4) == []
    assert extract_predictions("trending", "not a body", season=2026, week=4) == []  # type: ignore[arg-type]


def test_every_doc_carries_the_scoring_fields_unset() -> None:
    body = {"picks": [_row("1")]}

    (doc,) = extract_predictions("sleepers", body, season=2026, week=4)

    assert doc["season"] == 2026 and doc["week"] == 4 and doc["endpoint"] == "sleepers"
    assert doc["scored"] is False and doc["hit"] is None and doc["points"] is None
    assert doc["recorded_at"].endswith("Z")
    assert doc["kind"] in KINDS
    assert "group" not in doc


def test_ids_are_stable_and_order_blind_for_matchups() -> None:
    a = {
        "season": 2026,
        "week": 4,
        "endpoint": "matchup",
        "kind": "matchup_top",
        "player_id": "1",
        "group": ["1", "2", "3"],
    }
    b = {**a, "player_id": "2", "group": ["3", "1", "2"]}
    c = {**a, "kind": "sit", "group": None, "player_id": "1"}

    assert prediction_id(a) == prediction_id(b)
    assert prediction_id(a) != prediction_id(c)
    assert prediction_id(a).startswith("2026w4:matchup:matchup_top:")
    # A single-player id carries no direction: "sit X" and "start X" share it.
    assert prediction_id(c) == "2026w4:matchup:1"
    assert prediction_id({**c, "kind": "start"}) == prediction_id(c)


# --------------------------------------------------------------------------
# Against the real engine and fixture
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c.name for c in GOLDEN_CASES])
async def test_every_golden_answer_extracts_cleanly(case: Any) -> None:
    """Extraction runs over every body the engine can produce without raising,
    and claims only where the endpoint makes one."""
    store = await seed_store(MemoryStore())
    engine = DeterministicAnalysisEngine(store=store, settings=eval_settings())
    body = (await engine.analyze(case.endpoint_key, dict(case.request_context))).model_dump(
        mode="json"
    )

    docs = extract_predictions(case.endpoint_key, body, season=SEASON, week=WEEK)

    if case.endpoint_key in UNSCORED_ENDPOINTS:
        assert docs == []
    for doc in docs:
        assert doc["endpoint"] == case.endpoint_key
        assert doc["player_id"]
    if case.name == "matchup_two_players":
        assert [d["kind"] for d in docs] == ["matchup_top"]
    if case.name == "roster_manual_paste":
        assert docs, "a roster audit makes start/sit calls"


# --------------------------------------------------------------------------
# Recording: first write wins, nothing after kickoff, and never raises
# --------------------------------------------------------------------------

#: The fixture's week 4 kicks off 2026-10-01T00:15Z. Every clock-dependent test
#: pins "now" to the Monday before, so the suite does not start failing the
#: day the fixture week is played.
BEFORE_KICKOFF = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
KICKOFF = "2026-10-01T00:15:00Z"


@pytest.fixture(autouse=True)
def _pinned_clock() -> Iterator[None]:
    set_clock(lambda: BEFORE_KICKOFF)
    yield
    set_clock(None)


#: The ids the recording tests make calls on; :func:`_schedule` gives each a
#: team with a game, since a player with no game that week is never filed.
PLAYING_IDS = ("1", "2", "3", "4", "5")


async def _schedule(store: Store, week: int = 4, first_game: str = KICKOFF) -> None:
    """File week ``week``'s schedule (KC hosts ATL) and put :data:`PLAYING_IDS` on KC."""
    await store.set(
        SCHEDULES_COLLECTION,
        f"2026_{week}",
        {
            "season": 2026,
            "week": week,
            "first_game": first_game,
            "games": [{"home": "KC", "away": "ATL", "kickoff": first_game}],
        },
    )
    for player_id in PLAYING_IDS:
        await store.set("players", player_id, {"player_id": player_id, "team": "KC"}, merge=True)


async def test_first_write_wins(store: Store) -> None:
    """A re-warmed board must not rewrite the call that was made first."""
    await _schedule(store)
    first = {"picks": [{**_row("1"), "name": "First"}]}
    second = {"picks": [{**_row("1"), "name": "Second"}, _row("2")]}

    assert await record_predictions(store, "sleepers", first, season=2026, week=4) == 1
    assert await record_predictions(store, "sleepers", second, season=2026, week=4) == 1

    kept = await store.get(PREDICTIONS_COLLECTION, "2026w4:sleepers:1")
    assert kept is not None and kept["name"] == "First"
    assert kept["first_game"] == KICKOFF
    assert kept["recorded_at"] == "2026-09-28T12:00:00Z"


async def test_a_flipped_call_on_the_same_player_is_not_filed(store: Store) -> None:
    """Tuesday's "add X" and Wednesday's "fade X": filing both guarantees a hit."""
    await _schedule(store)
    tuesday = {"players": [_row("1", verdict="add")]}
    wednesday = {"players": [_row("1", verdict="fade"), _row("2", verdict="fade")]}

    assert await record_predictions(store, "trending", tuesday, season=2026, week=4) == 1
    assert await record_predictions(store, "trending", wednesday, season=2026, week=4) == 1

    claims = await store.list(PREDICTIONS_COLLECTION, where=[("player_id", "==", "1")])
    assert [(c["endpoint"], c["kind"]) for c in claims] == [("trending", "add")]
    # A different endpoint's claim on the same player is its own record.
    roster = {"start_sit": [_row("1", call="bench")]}
    assert await record_predictions(store, "roster", roster, season=2026, week=4) == 1
    # And a different week is a different slot (asked during week 5's window).
    await _schedule(store, week=5, first_game="2026-10-08T00:15:00Z")
    week_five = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    assert (
        await record_predictions(store, "trending", wednesday, season=2026, week=5, now=week_five)
        == 2
    )


async def test_a_claim_filed_under_the_old_id_scheme_still_wins(store: Store) -> None:
    """Ids carried the kind until 2026-09-24; those claims must block a flip too."""
    await _schedule(store)
    old = {
        "season": 2026,
        "week": 4,
        "endpoint": "trending",
        "kind": "add",
        "player_id": "1",
        "scored": False,
    }
    assert await store.create(PREDICTIONS_COLLECTION, "2026w4:trending:add:1", old)
    assert "2026w4:trending:fade:1" in legacy_prediction_ids({**old, "kind": "fade"})

    flipped = {"players": [_row("1", verdict="fade")]}
    same = {"players": [_row("1", verdict="add")]}
    assert await record_predictions(store, "trending", flipped, season=2026, week=4) == 0
    assert await record_predictions(store, "trending", same, season=2026, week=4) == 0
    assert await store.get(PREDICTIONS_COLLECTION, "2026w4:trending:1") is None
    assert len(await store.list(PREDICTIONS_COLLECTION)) == 1


async def test_nothing_is_filed_at_or_after_kickoff(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """A week-3 request made the Tuesday after week 3 is hindsight, not a claim."""
    await _schedule(store)
    body = {"picks": [_row("1")]}
    at_kickoff = datetime(2026, 10, 1, 0, 15, tzinfo=UTC)
    after = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    with caplog.at_level(logging.INFO, logger="api.data.predictions"):
        assert (
            await record_predictions(store, "sleepers", body, season=2026, week=4, now=at_kickoff)
            == 0
        )
        assert (
            await record_predictions(store, "sleepers", body, season=2026, week=4, now=after) == 0
        )

    assert await store.list(PREDICTIONS_COLLECTION) == []
    assert any("hindsight" in r.message for r in caplog.records)


async def test_nothing_is_filed_without_an_ingested_schedule(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """No kickoff to beat means no way to show the claim predates the games."""
    body = {"picks": [_row("1")]}

    with caplog.at_level(logging.INFO, logger="api.data.predictions"):
        assert await record_predictions(store, "sleepers", body, season=2026, week=4) == 0

    assert await store.list(PREDICTIONS_COLLECTION) == []
    assert any("no ingested schedule" in r.message for r in caplog.records)


async def test_nothing_is_filed_for_a_future_week(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """Week 17 asked in week 4 is built on week-4 data; filing it would lock out the real call."""
    await _schedule(store)
    await _schedule(store, week=17, first_game="2026-12-25T01:15:00Z")
    body = {"picks": [_row("1")]}

    with caplog.at_level(logging.INFO, logger="api.data.predictions"):
        assert await record_predictions(store, "sleepers", body, season=2026, week=17) == 0

    assert await store.list(PREDICTIONS_COLLECTION) == []
    assert any("future week" in r.message for r in caplog.records)
    # The current week still files, and so does week 17 once it is here.
    assert await record_predictions(store, "sleepers", body, season=2026, week=4) == 1
    in_week_17 = datetime(2026, 12, 23, 12, 0, tzinfo=UTC)
    assert (
        await record_predictions(store, "sleepers", body, season=2026, week=17, now=in_week_17) == 1
    )


async def test_the_current_week_files_however_far_off_its_kickoff(store: Store) -> None:
    """Preseason: week 1 is current weeks before it kicks off, and its claims count."""
    await _schedule(store, week=1, first_game="2026-10-20T00:15:00Z")
    settings = Settings(_env_file=None, week_override=1)  # type: ignore[call-arg]
    body = {"picks": [_row("1")]}

    assert (
        await record_predictions(store, "sleepers", body, season=2026, week=1, settings=settings)
        == 1
    )


async def test_no_claim_is_filed_on_a_player_who_is_out(store: Store) -> None:
    """A sit on an IR'd player is a free hit; a start on one a free miss."""
    await _schedule(store)
    await store.set("players", "1", {"injury_status": "IR"}, merge=True)
    await store.set("players", "2", {"injury_status": "sus"}, merge=True)
    await store.set("players", "3", {"injury_status": "Questionable"}, merge=True)
    # Sleeper records IR/PUP/suspensions in ``status`` alone.
    await store.set(
        "players", "5", {"injury_status": None, "status": "Injured Reserve"}, merge=True
    )
    body = {
        "start_sit": [
            _row("1", call="bench"),
            _row("2", call="start"),
            _row("3", call="start"),
            _row("4", call="bench"),
            _row("5", call="bench"),
        ]
    }

    assert await record_predictions(store, "roster", body, season=2026, week=4) == 2

    filed = {d["player_id"] for d in await store.list(PREDICTIONS_COLLECTION)}
    assert filed == {"3", "4"}


async def test_no_claim_is_filed_on_a_player_with_no_game_that_week(store: Store) -> None:
    """Bye week, free agent, unknown id: no stat line, so a sit or fade would be a free hit."""
    await _schedule(store)
    await store.set("players", "6", {"player_id": "6", "team": "DEN"})  # DEN is on bye
    await store.set("players", "7", {"player_id": "7", "team": None})  # a free agent
    await store.set("players", "9", {"player_id": "9", "team": "atl"})  # case-blind
    body = {
        "players": [
            _row("1", verdict="fade"),
            _row("6", verdict="fade"),
            _row("7", verdict="fade"),
            _row("8", verdict="fade"),  # no players/ doc at all
            _row("9", verdict="add"),
        ]
    }

    assert await record_predictions(store, "trending", body, season=2026, week=4) == 2

    filed = {d["player_id"] for d in await store.list(PREDICTIONS_COLLECTION)}
    assert filed == {"1", "9"}


async def test_a_matchup_against_a_player_with_no_game_is_not_filed(store: Store) -> None:
    """Out-scoring a bye-week player's zero is not a call."""
    await _schedule(store)
    await store.set("players", "6", {"player_id": "6", "team": "DEN"})
    against_bye = {"ranked": [_row("1", rank=1), _row("6", rank=2)]}
    fair = {"ranked": [_row("1", rank=1), _row("2", rank=2)]}

    assert await record_predictions(store, "matchup", against_bye, season=2026, week=4) == 0
    assert await record_predictions(store, "matchup", fair, season=2026, week=4) == 1


async def test_the_deadline_is_the_earliest_kickoff_on_record(store: Store) -> None:
    await store.set(
        SCHEDULES_COLLECTION,
        "2026_4",
        {
            "first_game": "2026-10-01T00:15:00Z",
            "games": [
                {"home": "A", "away": "B", "kickoff": "2026-09-30T23:00:00Z"},
                {"home": "C", "away": "D", "kickoff": "2026-10-04T17:00:00Z"},
                {"home": "E", "away": "F", "kickoff": None},
            ],
        },
    )
    assert await claim_deadline(store, 2026, 4) == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)

    await store.set(SCHEDULES_COLLECTION, "2026_5", {"games": [{"kickoff": "not a date"}]})
    assert await claim_deadline(store, 2026, 5) is None
    assert await claim_deadline(store, 2026, 6) is None


class _BrokenStore(MemoryStore):
    async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
        raise RuntimeError("firestore is having a day")


async def test_a_store_failure_is_logged_and_swallowed(caplog: pytest.LogCaptureFixture) -> None:
    body = {"picks": [_row("1")]}
    broken = _BrokenStore()
    await _schedule(broken)

    with caplog.at_level(logging.WARNING, logger="api.data.predictions"):
        written = await record_predictions(broken, "sleepers", body, season=2026, week=4)

    assert written == 0
    assert any("could not archive predictions" in r.message for r in caplog.records)


async def test_archive_with_no_week_files_nothing(store: Store, settings: Settings) -> None:
    body = {"picks": [_row("1")]}
    assert await archive_predictions(store, "sleepers", body, week=None, settings=settings) == 0
    assert await store.list(PREDICTIONS_COLLECTION) == []


async def test_archive_resolves_the_season_from_the_store(store: Store, settings: Settings) -> None:
    """The ingested schedule's season wins over the configured one, as everywhere else."""
    await seed_store(store)
    body = {"picks": [_row("1001")]}  # a fixture player whose team plays in WEEK

    written = await archive_predictions(store, "sleepers", body, week=WEEK, settings=settings)

    assert written == 1
    (doc,) = await store.list(PREDICTIONS_COLLECTION)
    assert doc["season"] == SEASON and doc["week"] == WEEK


# --------------------------------------------------------------------------
# Wired into the paths that actually produce answers
# --------------------------------------------------------------------------


@pytest.fixture
async def open_app(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch)
    clear_inflight()
    await seed_store(store)
    return store


async def test_a_personalized_answer_is_archived(open_app: Store) -> None:
    async with api_client() as client:
        response = await client.post(
            "/v1/matchup", json={"players": ["Bijan Robinson", "Breece Hall"]}
        )

    assert response.status_code == 200
    docs = await open_app.list(PREDICTIONS_COLLECTION)
    assert [(d["endpoint"], d["kind"], d["week"]) for d in docs] == [
        ("matchup", "matchup_top", WEEK)
    ]
    assert docs[0]["player_id"] == response.json()["ranked"][0]["player_id"]


async def test_a_freshly_generated_board_is_archived_once(open_app: Store) -> None:
    async with api_client() as client:
        first = await client.get("/v1/trending")
        second = await client.get("/v1/trending")

    assert first.status_code == 200 and second.json()["meta"]["cache"] == "hit"
    docs = await open_app.list(PREDICTIONS_COLLECTION)
    assert docs, "the trending board makes add/fade calls on this fixture"
    assert {d["endpoint"] for d in docs} == {"trending"}
    assert len({d["_id"] for d in docs}) == len(docs)


async def test_warming_archives_the_boards(store: Store) -> None:
    await seed_store(store)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="deterministic",
        x402_mode="disabled",
        season=SEASON,
        week_override=WEEK,
    )

    summary = await warm_response_cache(store, settings, force=True)

    assert summary["failed"] == 0
    endpoints = {d["endpoint"] for d in await store.list(PREDICTIONS_COLLECTION)}
    assert {"trending", "sleepers", "waivers", "report"} <= endpoints
    assert "draft_board" not in endpoints


def test_team_codes_that_differ_between_sources_still_count_as_playing() -> None:
    """nflverse schedules the Rams as LA; Sleeper rosters them as LAR."""
    from api.data.predictions import _teams_playing

    playing = _teams_playing({"games": [{"home": "LA", "away": "SF"}]})
    assert {"LA", "LAR", "SF"} <= playing
