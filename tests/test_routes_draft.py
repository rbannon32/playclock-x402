"""The draft pair: the Sleeper draft client, the board, and the graded report.

Draft season is the one time of year a fantasy API sees concentrated traffic, so
these two endpoints get their own file rather than riding along in
``test_routes_paid.py``. The properties pinned are the ones that would make the
product wrong rather than merely broken:

* the board is *not* a reprint of Sleeper's ordering — that is the whole value;
* ``market_rank`` is never presented as a consensus ADP, because it is not one;
* the board caches under a season-constant week, so a draft on Tuesday and one
  on Thursday hit the same warm entry;
* a draft with no picks is a 404, not an empty analysis sold for 0.75 USDC.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from api.core.store import Store
from api.data.cache import CACHE_COLLECTION, cache_key
from api.data.sleeper import SleeperClient
from api.data.stats_store import get_draft_pool
from api.evals.golden import DRAFT_PICKS, MARKET_RANKS, SEASON, seed_store
from api.x402 import RECEIPTS_COLLECTION, clear_idempotency_cache, set_facilitator
from api.x402.schemas_compat import MOCK_PAYMENT_HEADER, PAYMENT_SIGNATURE_HEADER
from tests.test_routes_free import api_client, configure

BASE = "https://api.sleeper.app/v1"
PAID = {PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}


@pytest.fixture(autouse=True)
def _reset_payment_state() -> Iterator[None]:
    clear_idempotency_cache()
    set_facilitator(None)
    yield
    clear_idempotency_cache()
    set_facilitator(None)


@pytest.fixture
async def seeded_mock(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch, X402_MODE="mock", SLEEPER_BASE_URL=BASE)
    await seed_store(store)
    return store


# --------------------------------------------------------------------------
# the Sleeper draft client
# --------------------------------------------------------------------------


@pytest.fixture
async def client(monkeypatch: pytest.MonkeyPatch):
    from api.core.config import Settings

    c = SleeperClient(Settings(_env_file=None, sleeper_base_url=BASE), retry_wait=0)  # type: ignore[call-arg]
    try:
        yield c
    finally:
        await c.aclose()


@respx.mock
async def test_drafts_come_back_newest_first(client: SleeperClient) -> None:
    """A caller who gave only a username means "the draft I just did"."""
    respx.get(f"{BASE}/user/42/drafts/nfl/2026").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"draft_id": "older", "start_time": 1000},
                {"draft_id": "newest", "start_time": 3000},
                {"draft_id": "middle", "start_time": 2000},
            ],
        )
    )
    drafts = await client.get_drafts("42", 2026)
    assert [d["draft_id"] for d in drafts] == ["newest", "middle", "older"]


@respx.mock
async def test_drafts_without_start_times_do_not_crash(client: SleeperClient) -> None:
    respx.get(f"{BASE}/user/42/drafts/nfl/2026").mock(
        return_value=httpx.Response(200, json=[{"draft_id": "a"}, {"draft_id": "b"}])
    )
    assert len(await client.get_drafts("42", 2026)) == 2


@respx.mock
async def test_picks_come_back_in_pick_order(client: SleeperClient) -> None:
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"player_id": "3", "pick_no": 3},
                {"player_id": "1", "pick_no": 1},
                {"player_id": "2", "pick_no": 2},
            ],
        )
    )
    picks = await client.get_draft_picks("1001")
    assert [p["pick_no"] for p in picks] == [1, 2, 3]


@respx.mock
async def test_an_unstarted_draft_returns_no_picks_rather_than_raising(
    client: SleeperClient,
) -> None:
    """An in-progress or unstarted draft is a normal outcome, not an error."""
    respx.get(f"{BASE}/draft/1001/picks").mock(return_value=httpx.Response(200, json=[]))
    assert await client.get_draft_picks("1001") == []


# --------------------------------------------------------------------------
# the draft pool read model
# --------------------------------------------------------------------------


async def test_draft_pool_is_ordered_by_market_prominence(store: Store) -> None:
    await seed_store(store)
    pool = await get_draft_pool(store, limit=50)

    ranks = [p["search_rank"] for p in pool]
    assert ranks == sorted(ranks), "the pool must come back most-prominent first"
    assert all(p["position"] in ("QB", "RB", "WR", "TE") for p in pool)


async def test_players_the_market_has_no_opinion_on_are_excluded(store: Store) -> None:
    """An unranked player is not draftable, which is different from ranked last."""
    await seed_store(store)
    await store.set(
        "players",
        "9999",
        {"player_id": "9999", "name": "Practice Squad Guy", "position": "WR", "team": "SEA"},
    )
    pool = await get_draft_pool(store, limit=200)
    assert "9999" not in {p["player_id"] for p in pool}
    # Every ranked player except the kicker: the pool's default position filter
    # is the four positions where draft value is actually decided.
    ranked_skill = {pid for pid in MARKET_RANKS if pid != "1019"}
    assert {p["player_id"] for p in pool} == ranked_skill


# --------------------------------------------------------------------------
# GET /v1/draft-board
# --------------------------------------------------------------------------


async def test_draft_board_requires_payment(seeded_mock: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/draft-board")
    assert response.status_code == 402


async def test_draft_board_refuses_a_scoring_format_it_does_not_compute(
    seeded_mock: Store,
) -> None:
    """A PPR board labelled "standard" is a paid answer that lies about itself."""
    async with api_client() as client:
        response = await client.get("/v1/draft-board?scoring=standard", headers=PAID)
    assert response.status_code == 422
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


async def test_draft_board_pays_and_returns_a_tiered_board(seeded_mock: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/draft-board", headers=PAID)

    assert response.status_code == 200
    body = response.json()
    assert body["tiers"], "the board has no tiers"
    assert body["season"] == SEASON
    assert len(await seeded_mock.list(RECEIPTS_COLLECTION)) == 1


async def test_the_board_disagrees_with_the_market(seeded_mock: Store) -> None:
    """A board that reprints Sleeper's order is not worth 0.25 USDC."""
    async with api_client() as client:
        body = (await client.get("/v1/draft-board", headers=PAID)).json()

    rows = [player for tier in body["tiers"] for player in tier["players"]]
    assert any(row["value_delta"] for row in rows), "no player moved from the market order"
    assert body["values"] and body["reaches"]


async def test_the_board_never_calls_its_market_signal_adp(seeded_mock: Store) -> None:
    async with api_client() as client:
        body = (await client.get("/v1/draft-board", headers=PAID)).json()

    rows = [player for tier in body["tiers"] for player in tier["players"]]
    claimed = " ".join([body["verdict"]] + [row["note"] for row in rows]).lower()
    assert "adp" not in claimed
    # ...but the reasoning must say so out loud.
    assert "adp" in body["reasoning"].lower()


async def test_the_board_caches_under_a_season_constant_week(seeded_mock: Store) -> None:
    """Drafts run all week; they must all hit one warm entry, not one per week."""
    async with api_client() as client:
        await client.get("/v1/draft-board", headers=PAID)

    keys = {doc["_id"] for doc in await seeded_mock.list(CACHE_COLLECTION)}
    assert cache_key("draft_board", 0, "limit=200:scoring=ppr") in keys


async def test_a_second_drafter_gets_the_cached_board(seeded_mock: Store) -> None:
    async with api_client() as client:
        first = (await client.get("/v1/draft-board", headers=PAID)).json()
        second = (
            await client.get(
                "/v1/draft-board",
                headers={PAYMENT_SIGNATURE_HEADER: _distinct_payment("second")},
            )
        ).json()

    assert first["meta"]["cache"] == "fresh"
    assert second["meta"]["cache"] == "hit"
    assert len(await seeded_mock.list(RECEIPTS_COLLECTION)) == 2, "both drafters paid"


def _distinct_payment(nonce: str) -> str:
    import base64
    import json

    body = {"x402Version": 2, "payload": {"mock": True, "nonce": nonce}}
    return base64.b64encode(json.dumps(body).encode()).decode()


# --------------------------------------------------------------------------
# POST /v1/draft-report
# --------------------------------------------------------------------------


def _picks_payload() -> list[dict[str, Any]]:
    return [dict(pick) for pick in DRAFT_PICKS]


@respx.mock
async def test_draft_report_grades_a_draft_by_id(seeded_mock: Store) -> None:
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        response = await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)

    assert response.status_code == 200
    body = response.json()
    assert body["draft_id"] == "1001"
    assert body["grade"]
    assert len(body["roster"]) == len(DRAFT_PICKS)
    assert body["week_one_plan"]


@respx.mock
async def test_draft_report_resolves_a_username_to_their_latest_draft(
    seeded_mock: Store,
) -> None:
    respx.get(f"{BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "u1", "username": "ryan"})
    )
    respx.get(f"{BASE}/user/u1/drafts/nfl/{SEASON}").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"draft_id": "old", "start_time": 1},
                {"draft_id": "latest", "start_time": 999},
            ],
        )
    )
    respx.get(f"{BASE}/draft/latest/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        response = await client.post(
            "/v1/draft-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    assert response.json()["draft_id"] == "latest"


@respx.mock
async def test_the_report_flags_a_reach(seeded_mock: Store) -> None:
    """Tyjae Spears (market rank 52) went at pick 24. That must be called out."""
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        body = (
            await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)
        ).json()

    assert "1009" in {pick["player_id"] for pick in body["worst_picks"]}


@respx.mock
async def test_taking_a_top_player_late_reads_as_value_not_a_reach(
    seeded_mock: Store,
) -> None:
    """The sign convention, pinned: pick_no - market_rank, positive = value."""
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        body = (
            await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)
        ).json()

    by_id = {pick["player_id"]: pick for pick in body["roster"]}
    # Josh Allen: market rank 18, taken at 64 — a bargain, not a reach.
    assert by_id["1006"]["value_delta"] > 0


async def test_a_report_with_neither_identifier_is_a_400(seeded_mock: Store) -> None:
    async with api_client() as client:
        response = await client.post("/v1/draft-report", json={}, headers=PAID)

    assert response.status_code == 400
    # Non-2xx: nothing settled, nobody was charged.
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_an_empty_draft_is_a_404_not_an_empty_analysis(seeded_mock: Store) -> None:
    """Selling a graded report on a draft that has not happened would be theft."""
    respx.get(f"{BASE}/draft/1001/picks").mock(return_value=httpx.Response(200, json=[]))

    async with api_client() as client:
        response = await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)

    assert response.status_code == 404
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_an_unknown_username_is_a_404(seeded_mock: Store) -> None:
    respx.get(f"{BASE}/user/ghost").mock(return_value=httpx.Response(404))

    async with api_client() as client:
        response = await client.post(
            "/v1/draft-report", json={"sleeper_username": "ghost"}, headers=PAID
        )

    assert response.status_code == 404
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


# --------------------------------------------------------------------------
# whose draft is this? — a draft holds every team's picks
# --------------------------------------------------------------------------


def _league_picks() -> list[dict[str, Any]]:
    """The fixture manager's picks, interleaved with two other teams' picks.

    This is what Sleeper actually returns: one flat list for the whole draft.
    """
    others = [
        {
            "player_id": pid,
            "round": 1,
            "pick_no": no,
            "roster_id": rid,
            "picked_by": picker,
            "draft_slot": slot,
        }
        for pid, no, rid, picker, slot in (
            ("1004", 1, 2, "u2", 1),
            ("1016", 2, 3, "u3", 2),
            ("1018", 3, 2, "u2", 1),
            ("1011", 5, 3, "u3", 2),
        )
    ]
    return sorted([*_picks_payload(), *others], key=lambda p: p["pick_no"])


@respx.mock
async def test_a_username_grades_only_that_managers_picks(seeded_mock: Store) -> None:
    """The bug this guards: grading the whole league as one roster."""
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_league_picks())
    )
    respx.get(f"{BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "u1", "username": "ryan"})
    )

    async with api_client() as client:
        body = (
            await client.post(
                "/v1/draft-report",
                json={"draft_id": "1001", "sleeper_username": "ryan"},
                headers=PAID,
            )
        ).json()

    assert len(body["roster"]) == len(DRAFT_PICKS)
    assert {"1004", "1016", "1018", "1011"}.isdisjoint({p["player_id"] for p in body["roster"]})


@respx.mock
async def test_a_draft_slot_grades_that_seat(seeded_mock: Store) -> None:
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_league_picks())
    )

    async with api_client() as client:
        body = (
            await client.post(
                "/v1/draft-report", json={"draft_id": "1001", "draft_slot": 1}, headers=PAID
            )
        ).json()

    assert {p["player_id"] for p in body["roster"]} == {"1004", "1018"}


@respx.mock
async def test_a_multi_team_draft_without_an_identity_is_refused(seeded_mock: Store) -> None:
    """Better to ask than to grade a roster nobody owns."""
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_league_picks())
    )

    async with api_client() as client:
        response = await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)

    assert response.status_code == 400
    assert "draft_slot" in response.json()["detail"]
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_autopicked_selections_fall_back_to_the_draft_order(seeded_mock: Store) -> None:
    """An autopick can carry an empty picked_by while still sitting in a seat."""
    picks = [{**pick, "picked_by": ""} for pick in _picks_payload()]
    respx.get(f"{BASE}/draft/1001/picks").mock(return_value=httpx.Response(200, json=picks))
    respx.get(f"{BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "u1", "username": "ryan"})
    )
    respx.get(f"{BASE}/draft/1001").mock(
        return_value=httpx.Response(200, json={"draft_id": "1001", "draft_order": {"u1": 4}})
    )

    async with api_client() as client:
        body = (
            await client.post(
                "/v1/draft-report",
                json={"draft_id": "1001", "sleeper_username": "ryan"},
                headers=PAID,
            )
        ).json()

    assert len(body["roster"]) == len(DRAFT_PICKS)


@respx.mock
async def test_a_manager_with_no_picks_in_that_draft_is_a_404(seeded_mock: Store) -> None:
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_league_picks())
    )

    async with api_client() as client:
        response = await client.post(
            "/v1/draft-report", json={"draft_id": "1001", "draft_slot": 9}, headers=PAID
        )

    assert response.status_code == 404
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_a_solo_mock_draft_needs_no_identity(seeded_mock: Store) -> None:
    """One participant is unambiguous, so don't make the caller say so."""
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        response = await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)

    assert response.status_code == 200
    assert len(response.json()["roster"]) == len(DRAFT_PICKS)


# --------------------------------------------------------------------------
# the callouts have to be the actual best and worst
# --------------------------------------------------------------------------


@respx.mock
async def test_pick_callouts_are_ranked_by_value_not_draft_order(seeded_mock: Store) -> None:
    respx.get(f"{BASE}/draft/1001/picks").mock(
        return_value=httpx.Response(200, json=_picks_payload())
    )

    async with api_client() as client:
        body = (
            await client.post("/v1/draft-report", json={"draft_id": "1001"}, headers=PAID)
        ).json()

    best = [p["value_delta"] for p in body["best_picks"]]
    worst = [p["value_delta"] for p in body["worst_picks"]]
    assert best == sorted(best, reverse=True), "best picks must lead with the biggest bargain"
    assert worst == sorted(worst), "worst picks must lead with the worst reach"
    assert all(delta > 0 for delta in best)
    assert all(delta < 0 for delta in worst)


async def test_board_callouts_never_include_an_unmoved_player(seeded_mock: Store) -> None:
    """A one-place shuffle is not a draft-day value, and must not be sold as one."""
    async with api_client() as client:
        body = (await client.get("/v1/draft-board?limit=25", headers=PAID)).json()

    assert all(v["value_delta"] > 0 for v in body["values"])
    assert all(r["value_delta"] < 0 for r in body["reaches"])


async def test_a_player_with_no_usage_is_never_called_a_value_or_reach(
    seeded_mock: Store,
) -> None:
    """Held at his market rank, he moves only because others move around him.

    Nine low-usage receivers drafted ahead of a rookie with no usage all slide
    down the board, which lifts the rookie several places without a single
    fact about him. That is not a value call, and the response must not sell
    it as one.
    """
    for rank in range(1, 41):
        pid = f"syn{rank}"
        await seeded_mock.set(
            "players",
            pid,
            {
                "player_id": pid,
                "name": f"Synthetic Receiver{rank}",
                "position": "WR",
                "team": "SEA",
                "search_rank": rank,
            },
        )
        if rank != 10:  # the rookie: no prior-season usage on file
            share = 0.0 if rank < 10 else rank / 40
            await seeded_mock.set(
                "usage_trends",
                pid,
                {
                    "player_id": pid,
                    "snap_pct_l4w": share,
                    "target_share_l4w": share / 4,
                    "rz_touches_l4w": 0,
                    "trend": "flat",
                },
            )

    async with api_client() as client:
        body = (await client.get("/v1/draft-board?limit=200", headers=PAID)).json()

    rows = {p["player_id"]: p for tier in body["tiers"] for p in tier["players"]}
    assert rows["syn10"]["value_delta"] >= 3, "the fixture must actually move the rookie"
    called = {row["player_id"] for row in body["values"] + body["reaches"]}
    assert "syn10" not in called
