"""Paid routes end to end: gating, caching, week validation and the Sleeper flows.

Everything runs over ``httpx.ASGITransport`` against a real ``create_app()``:
the payment dependency, :class:`~api.x402.PaidRoute`'s settle-after-2xx wrapper,
the deterministic engine and the response cache are all the production objects.
Only two things are faked — the facilitator (``X402_MODE=mock``, no chain) and
Sleeper (``respx``, no network).

The properties pinned here are the ones a bug would cost real money:

* a paid route without payment is a ``402`` carrying real requirements;
* a paid route with payment settles **once** and writes a receipt;
* week-scoped endpoints cache for exactly the TTL tech spec §6 specifies, and a
  second payer of the same cycle gets ``meta.cache == "hit"``;
* personalized endpoints never cache;
* a Sleeper 404 is a ``404`` and a Sleeper outage is a ``502`` — both non-2xx, so
  neither settles.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from api.agents import AnalysisEngine, get_engine, set_engine
from api.core.config import ENDPOINT_KEYS
from api.core.store import Store
from api.data.cache import CACHE_COLLECTION, cache_key
from api.data.stats_store import (
    FRESHNESS_DOC_ID,
    META_COLLECTION,
    USAGE_TRENDS_COLLECTION,
)
from api.evals.golden import SEASON, WEEK, seed_store
from api.routes import CACHE_TTL_SECONDS
from api.routes.paid import REQUIRED_DATASETS
from api.schemas import AnalysisResponse
from api.x402 import RECEIPTS_COLLECTION, clear_idempotency_cache, set_facilitator
from api.x402.middleware import PAYMENT_IDEMPOTENCY_COLLECTION
from api.x402.schemas_compat import (
    MOCK_PAYMENT_HEADER,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
)
from tests.test_routes_free import api_client, configure

SLEEPER_BASE = "https://api.sleeper.app/v1"

#: What a paying client sends in mock mode.
PAID = {PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}


def paid(nonce: str) -> dict[str, str]:
    """A *distinct* mock payment, for a test that makes two different requests.

    One payment buys one request (api/x402/middleware.py), so two different
    questions need two payments — exactly as a real client would send them.
    """
    body = {"x402Version": 2, "payload": {"mock": True, "nonce": nonce}}
    return {PAYMENT_SIGNATURE_HEADER: base64.b64encode(json.dumps(body).encode()).decode()}


@pytest.fixture(autouse=True)
def _reset_payment_state() -> Iterator[None]:
    """No verified payment or facilitator override survives a test."""
    clear_idempotency_cache()
    set_facilitator(None)
    yield
    clear_idempotency_cache()
    set_facilitator(None)


@pytest.fixture
async def paid_app(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """Seeded store plus ``X402_MODE=mock``: payments are real code, no chain."""
    configure(monkeypatch, X402_MODE="mock")
    await seed_store(store)
    return store


@pytest.fixture
async def open_app(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """Seeded store with payments disabled — for testing handler behaviour alone."""
    configure(monkeypatch)
    await seed_store(store)
    return store


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


async def test_paid_route_402s_without_payment(paid_app: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/trending")

    assert response.status_code == 402
    assert PAYMENT_REQUIRED_HEADER in response.headers
    body = response.json()
    # The V2 payload sits at the root, not under FastAPI's {"detail": ...}.
    assert body["x402Version"] == 2
    accepts = body["accepts"][0]
    assert accepts["amount"] == "100000"  # 0.10 USDC, 6 decimals
    assert accepts["extra"]["tag"] == "x402-global-challenge"
    assert "bazaar" in body["extensions"]


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", "/v1/trending", None),
        ("GET", "/v1/sleepers", None),
        ("GET", "/v1/waivers", None),
        ("GET", "/v1/report", None),
        ("POST", "/v1/player", {"name": "Bijan Robinson"}),
        ("POST", "/v1/matchup", {"players": ["Bijan Robinson", "Breece Hall"]}),
        ("POST", "/v1/roster", {"roster": [{"name": "Bijan Robinson"}]}),
        ("POST", "/v1/team-report", {"sleeper_username": "ryan"}),
    ],
)
async def test_every_paid_route_is_gated(
    paid_app: Store, method: str, path: str, payload: dict[str, Any] | None
) -> None:
    """All eight endpoints are behind the gate — none accidentally free."""
    async with api_client() as client:
        response = await client.request(method, path, json=payload)

    assert response.status_code == 402, path


async def test_paid_route_settles_and_writes_a_receipt(paid_app: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/trending", headers=PAID)

    assert response.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in response.headers

    receipts = await paid_app.list(RECEIPTS_COLLECTION)
    assert len(receipts) == 1
    assert receipts[0]["endpoint"] == "trending"
    assert receipts[0]["amount_usdc"] == 0.10
    assert receipts[0]["txid"]


async def test_one_payment_does_not_buy_a_second_player(paid_app: Store) -> None:
    """One payment buys one deep dive, not every player in the league for the window."""
    payment = paid("one-player")
    async with api_client() as client:
        first = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=payment)
        replay = await client.post("/v1/player", json={"name": "Breece Hall"}, headers=payment)

    assert first.status_code == 200
    assert replay.status_code == 402
    assert replay.json()["error"] == "payment_already_used_for_a_different_request"
    assert len(await paid_app.list(RECEIPTS_COLLECTION)) == 1


async def test_the_shared_mock_marker_never_locks_out_a_different_question(
    paid_app: Store,
) -> None:
    """Every mock caller sends the same ``mock-paid``: it is not one payment.

    Keyed on the header alone, the first player asked about would 402 every
    other player question, for every caller, for the whole idempotency window.
    """
    async with api_client() as client:
        first = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=PAID)
        second = await client.post("/v1/player", json={"name": "Breece Hall"}, headers=PAID)

    assert first.status_code == second.status_code == 200
    assert len(await paid_app.list(RECEIPTS_COLLECTION)) == 2


async def test_disabled_mode_serves_without_payment(open_app: Store) -> None:
    """``X402_MODE=disabled`` is the local-dev bypass, and settles nothing."""
    async with api_client() as client:
        response = await client.get("/v1/trending")

    assert response.status_code == 200
    assert PAYMENT_RESPONSE_HEADER not in response.headers
    assert await open_app.list(RECEIPTS_COLLECTION) == []


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


async def test_trending_pay_generate_then_pay_cache_hit(paid_app: Store) -> None:
    """The whole flow: pay -> fresh -> pay again -> the cached body, for 6h."""
    async with api_client() as client:
        first = await client.get("/v1/trending", headers=PAID)
        second = await client.get("/v1/trending", headers=PAID)

    assert first.status_code == second.status_code == 200
    assert first.json()["meta"]["cache"] == "fresh"
    assert second.json()["meta"]["cache"] == "hit"
    # Same analysis, re-served: only the cache marker differs.
    assert first.json()["verdict"] == second.json()["verdict"]

    entry = await paid_app.get(
        CACHE_COLLECTION, cache_key("trending", WEEK, "lookback=24:limit=25")
    )
    assert entry is not None
    assert entry["ttl_seconds"] == CACHE_TTL_SECONDS["trending"] == 6 * 3600


async def test_trending_reports_the_ingested_window_not_the_requested_one(
    paid_app: Store,
) -> None:
    """The counts are the poll's 24h; claiming "168h" would be a made-up number."""
    async with api_client() as client:
        response = await client.get("/v1/trending?lookback_hours=168", headers=PAID)

    assert response.status_code == 200
    body = response.json()
    assert body["lookback_hours"] == 24
    assert "168h" not in response.text
    # Every lookback reads the one warmed board rather than splitting the cache.
    assert await paid_app.get(CACHE_COLLECTION, cache_key("trending", WEEK, "lookback=24:limit=25"))


async def test_only_trending_is_an_intraday_board(paid_app: Store) -> None:
    """Tech spec §6: trending holds 6h; sleepers, waivers and report 12h.

    Trending is about the last 24h of adds and the free preview shows the live
    counts. The other boards say things that hold for a day, and at 6h the
    warming loop was regenerating each of them six times a day for a handful
    of sales (DESIGN_NOTES §26).
    """
    async with api_client() as client:
        for path in ("/v1/report", "/v1/waivers", "/v1/sleepers", "/v1/trending"):
            assert (await client.get(path, headers=PAID)).status_code == 200

    entries = {
        "report": await paid_app.get(CACHE_COLLECTION, cache_key("report", WEEK)),
        "waivers": await paid_app.get(CACHE_COLLECTION, cache_key("waivers", WEEK, "limit=15")),
        "sleepers": await paid_app.get(CACHE_COLLECTION, cache_key("sleepers", WEEK, "limit=12")),
        "trending": await paid_app.get(
            CACHE_COLLECTION, cache_key("trending", WEEK, "lookback=24:limit=25")
        ),
    }
    assert all(entry is not None for entry in entries.values())
    assert entries["trending"]["ttl_seconds"] == 6 * 3600  # type: ignore[index]
    for key in ("report", "waivers", "sleepers"):
        assert entries[key]["ttl_seconds"] == 12 * 3600, key  # type: ignore[index]
        assert entries[key]["expires_at"] > entries["trending"]["expires_at"]  # type: ignore[index]


async def test_expired_cache_entry_regenerates(paid_app: Store) -> None:
    """Expiry is lazy: a stale entry reads as a miss and is replaced."""
    key = cache_key("report", WEEK)
    await paid_app.set(
        CACHE_COLLECTION,
        key,
        {
            "endpoint": "report",
            "week": WEEK,
            "payload": {"stale": True},
            "created_at": "2020-01-01T00:00:00+00:00",
            "expires_at": "2020-01-01T12:00:00+00:00",
            "ttl_seconds": 43200,
        },
    )
    async with api_client() as client:
        response = await client.get("/v1/report", headers=PAID)

    assert response.status_code == 200
    assert response.json()["meta"]["cache"] == "fresh"


async def test_adk_cache_miss_is_not_billed_or_generated(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADK boards must be precomputed: a cold request cannot outlive its buyer."""
    configure(monkeypatch, X402_MODE="mock", ENGINE="adk")
    calls: list[str] = []

    class MustNotRun(AnalysisEngine):
        name = "must-not-run"

        async def analyze(
            self, endpoint_key: str, request_context: dict[str, Any]
        ) -> AnalysisResponse:
            calls.append(endpoint_key)
            raise AssertionError("an ADK cache miss must not enter the engine")

    set_engine(MustNotRun())
    try:
        async with api_client() as client:
            response = await client.get("/v1/trending", headers=paid("cold-adk"))
    finally:
        set_engine(None)

    assert response.status_code == 503
    assert "being prepared" in response.json()["detail"]
    assert "not charged" in response.json()["detail"]
    assert calls == []
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


async def test_adk_serves_a_warmed_board_and_settles(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same ADK configuration still sells a valid precomputed cache hit."""
    async with api_client() as client:
        warmed = await client.get("/v1/trending", headers=paid("warm-board"))
    assert warmed.status_code == 200
    assert warmed.json()["meta"]["cache"] == "fresh"

    configure(monkeypatch, X402_MODE="mock", ENGINE="adk")
    calls: list[str] = []

    class MustNotRun(AnalysisEngine):
        name = "must-not-run"

        async def analyze(
            self, endpoint_key: str, request_context: dict[str, Any]
        ) -> AnalysisResponse:
            calls.append(endpoint_key)
            raise AssertionError("a warmed board must not enter the engine")

    set_engine(MustNotRun())
    try:
        async with api_client() as client:
            response = await client.get("/v1/trending", headers=paid("warmed-adk"))
    finally:
        set_engine(None)

    assert response.status_code == 200
    assert response.json()["meta"]["cache"] == "hit"
    assert calls == []
    assert [r["endpoint"] for r in await paid_app.list(RECEIPTS_COLLECTION)] == [
        "trending",
        "trending",
    ]


async def test_cache_key_separates_parameter_variants(paid_app: Store) -> None:
    """Different board sizes are different products, not cache collisions."""
    async with api_client() as client:
        small = await client.get("/v1/waivers?limit=3", headers=paid("small"))
        large = await client.get("/v1/waivers?limit=10", headers=paid("large"))

    assert small.json()["meta"]["cache"] == "fresh"
    assert large.json()["meta"]["cache"] == "fresh"
    assert len(small.json()["board"]) <= 3
    assert len(large.json()["board"]) >= len(small.json()["board"])


async def test_personalized_endpoints_are_never_cached(paid_app: Store) -> None:
    async with api_client() as client:
        response = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=PAID)

    assert response.status_code == 200
    assert response.json()["meta"]["cache"] == "miss"
    assert await paid_app.list(CACHE_COLLECTION) == []


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("week", [0, 19, 99, -3])
async def test_week_bounds_are_rejected(open_app: Store, week: int) -> None:
    async with api_client() as client:
        query = await client.get(f"/v1/trending?week={week}")
        body = await client.post("/v1/player", json={"name": "Bijan Robinson", "week": week})

    assert query.status_code == 400
    assert body.status_code == 400
    assert "week must be between 1 and 18" in query.json()["detail"]


async def test_valid_week_is_honoured(open_app: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/sleepers?week=3")

    assert response.status_code == 200
    assert response.json()["week"] == 3


async def test_player_requires_a_name_or_id(open_app: Store) -> None:
    async with api_client() as client:
        response = await client.post("/v1/player", json={})

    assert response.status_code == 400
    assert "player_id" in response.json()["detail"]


async def test_roster_requires_a_username_or_roster(open_app: Store) -> None:
    async with api_client() as client:
        response = await client.post("/v1/roster", json={})

    assert response.status_code == 400


async def test_matchup_rejects_a_single_player(open_app: Store) -> None:
    """2-4 players is a schema rule, so pydantic answers 422 before we do."""
    async with api_client() as client:
        response = await client.post("/v1/matchup", json={"players": ["Bijan Robinson"]})

    assert response.status_code == 422


async def test_matchup_ranks_every_requested_player(open_app: Store) -> None:
    async with api_client() as client:
        response = await client.post(
            "/v1/matchup", json={"players": ["Bijan Robinson", "Breece Hall", "Ja'Marr Chase"]}
        )

    assert response.status_code == 200
    ranked = response.json()["ranked"]
    assert [row["rank"] for row in ranked] == [1, 2, 3]


async def test_manual_roster_audit(open_app: Store) -> None:
    """No Sleeper username: the pasted roster is audited with no live calls."""
    async with api_client() as client:
        response = await client.post(
            "/v1/roster",
            json={
                "roster": [
                    {"name": "Bijan Robinson", "position": "RB", "starter": True},
                    {"name": "Ja'Marr Chase", "position": "WR", "starter": True},
                    {"name": "Breece Hall", "position": "RB"},
                ]
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sleeper_username"] is None
    assert {grade["position"] for grade in body["positional_grades"]} == {"RB", "WR"}
    assert body["start_sit"]


# ---------------------------------------------------------------------------
# Sleeper-backed flows
# ---------------------------------------------------------------------------


def mock_league(
    *,
    username: str = "ryan",
    user_id: str = "u1",
    league_id: str = "L1",
    my_players: list[str] | None = None,
) -> None:
    """Register the user -> leagues -> rosters chain both Sleeper routes walk."""
    mine = my_players or ["1001", "1003", "1006", "1005"]
    respx.get(f"{SLEEPER_BASE}/user/{username}").mock(
        return_value=httpx.Response(200, json={"user_id": user_id, "username": username})
    )
    respx.get(f"{SLEEPER_BASE}/user/{user_id}/leagues/nfl/{SEASON}").mock(
        return_value=httpx.Response(
            200, json=[{"league_id": league_id, "name": "The Example League"}]
        )
    )
    respx.get(f"{SLEEPER_BASE}/league/{league_id}/rosters").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "roster_id": 1,
                    "owner_id": user_id,
                    "players": mine,
                    "starters": mine[:3],
                    "settings": {"wins": 2, "losses": 1, "fpts": 300, "fpts_against": 280},
                },
                {
                    "roster_id": 2,
                    "owner_id": "u2",
                    "players": ["1002", "1004", "1010", "1011"],
                    "starters": ["1002", "1004", "1010"],
                    "settings": {"wins": 1, "losses": 2, "fpts": 270, "fpts_against": 300},
                },
            ],
        )
    )


@respx.mock
async def test_roster_audit_from_sleeper_username(paid_app: Store) -> None:
    mock_league()
    async with api_client() as client:
        response = await client.post("/v1/roster", json={"sleeper_username": "ryan"}, headers=PAID)

    assert response.status_code == 200
    body = response.json()
    assert body["sleeper_username"] == "ryan"
    assert body["league_id"] == "L1"
    assert body["week"] == WEEK
    assert body["meta"]["cache"] == "miss"
    names = {call["name"] for call in body["start_sit"]}
    assert "Bijan Robinson" in names
    # Waiver adds are restricted to players nobody in the league rosters.
    rostered = {"1001", "1003", "1006", "1005", "1002", "1004", "1010", "1011"}
    assert {add["player_id"] for add in body["waiver_adds"]}.isdisjoint(rostered)


@respx.mock
async def test_roster_picks_the_named_league(paid_app: Store) -> None:
    respx.get(f"{SLEEPER_BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "u1"})
    )
    respx.get(f"{SLEEPER_BASE}/user/u1/leagues/nfl/{SEASON}").mock(
        return_value=httpx.Response(200, json=[{"league_id": "1002"}, {"league_id": "2002"}])
    )
    respx.get(f"{SLEEPER_BASE}/league/2002/rosters").mock(
        return_value=httpx.Response(
            200,
            json=[{"roster_id": 7, "owner_id": "u1", "players": ["1001"], "starters": ["1001"]}],
        )
    )
    async with api_client() as client:
        response = await client.post(
            "/v1/roster", json={"sleeper_username": "ryan", "league_id": "2002"}, headers=PAID
        )

    assert response.status_code == 200
    assert response.json()["league_id"] == "2002"


@respx.mock
async def test_unknown_sleeper_user_is_404_and_settles_nothing(paid_app: Store) -> None:
    respx.get(f"{SLEEPER_BASE}/user/ghost").mock(return_value=httpx.Response(404))
    async with api_client() as client:
        response = await client.post("/v1/roster", json={"sleeper_username": "ghost"}, headers=PAID)

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "ghost" in detail
    # The wallet has already asked for a signature by this point; an error that
    # does not mention the money reads as a charge for nothing.
    assert "not charged" in detail
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_user_without_a_league_is_404(paid_app: Store) -> None:
    respx.get(f"{SLEEPER_BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "u1"})
    )
    respx.get(f"{SLEEPER_BASE}/user/u1/leagues/nfl/{SEASON}").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with api_client() as client:
        response = await client.post("/v1/roster", json={"sleeper_username": "ryan"}, headers=PAID)

    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "no NFL leagues" in detail
    assert "not charged" in detail
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_sleeper_outage_is_502(paid_app: Store) -> None:
    respx.get(f"{SLEEPER_BASE}/user/ryan").mock(return_value=httpx.Response(503))
    async with api_client() as client:
        response = await client.post("/v1/roster", json={"sleeper_username": "ryan"}, headers=PAID)

    assert response.status_code == 502
    assert "not charged" in response.json()["detail"]
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


def mock_team_report_league(
    weeks: int = WEEK - 1,
    *,
    active_week: int = WEEK,
    active_entries: list[dict[str, Any]] | None = None,
) -> respx.Route:
    """The full team-report chain: league settings, users and per-week matchups.

    Matchups are mocked for the **completed** weeks ``1..weeks`` only. The
    returned route is the *active* week's matchups — the report must never fetch
    it (a partly-played week would dilute every manager metric), so tests assert
    it stays uncalled.
    """
    mock_league()
    respx.get(f"{SLEEPER_BASE}/league/L1").mock(
        return_value=httpx.Response(
            200,
            json={
                "league_id": "L1",
                "name": "The Example League",
                "roster_positions": ["QB", "RB", "WR", "TE", "FLEX", "BN", "BN"],
                "settings": {"scoring_type": "ppr"},
            },
        )
    )
    respx.get(f"{SLEEPER_BASE}/league/L1/users").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "user_id": "u1",
                    "display_name": "ryan",
                    "metadata": {"team_name": "Play Clock FC"},
                },
                {"user_id": "u2", "display_name": "rival"},
            ],
        )
    )
    for week in range(1, weeks + 1):
        respx.get(f"{SLEEPER_BASE}/league/L1/matchups/{week}").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "roster_id": 1,
                        "matchup_id": 1,
                        "points": 100.0 + week,
                        "players": ["1001", "1003", "1006", "1005"],
                        "starters": ["1006", "1001", "1003", "1005", "1005"],
                        "players_points": {
                            "1001": 20.0,
                            "1003": 25.0,
                            "1006": 30.0 + week,
                            "1005": 25.0,
                        },
                    },
                    {
                        "roster_id": 2,
                        "matchup_id": 1,
                        "points": 90.0,
                        "players": ["1002", "1004", "1010", "1011"],
                        "starters": ["1002", "1004", "1010", "1011", "1002"],
                        "players_points": {
                            "1002": 10.0,
                            "1004": 30.0,
                            "1010": 25.0,
                            "1011": 25.0,
                        },
                    },
                ],
            )
        )
    return respx.get(f"{SLEEPER_BASE}/league/L1/matchups/{active_week}").mock(
        return_value=httpx.Response(200, json=list(active_entries or []))
    )


@respx.mock
async def test_team_report_end_to_end(paid_app: Store) -> None:
    """League history in, deterministic facts computed, engine narrates them."""
    active = mock_team_report_league()
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    body = response.json()
    assert body["sleeper_username"] == "ryan"
    assert body["league_id"] == "L1"
    assert body["league_name"] == "The Example League"
    # The report is *about* the active week, but its history stops at the last
    # completed one: week WEEK's games have not been played yet.
    assert body["week"] == WEEK
    assert body["season"] == SEASON
    assert body["meta"]["cache"] == "miss"
    assert not active.called, "the active, partly-played week must not be analysed"

    # Positional strength is graded against the actual two-team league.
    strength = {row["position"]: row for row in body["positional_strength_vs_league"]}
    assert strength, "expected league-relative positional grades"
    assert all(row["league_size"] == 2 for row in strength.values())

    # The manager review is real arithmetic from the matchup history, not zeros.
    review = body["manager_review"]
    assert review["league_size"] == 2
    assert review["efficiency_rank"] in (1, 2)
    assert review["lineup_efficiency_pct"] > 0
    assert "all-play" in review["luck_note"]

    # A settled payment for the most expensive endpoint.
    receipts = await paid_app.list(RECEIPTS_COLLECTION)
    assert [r["endpoint"] for r in receipts] == ["team_report"]
    assert receipts[0]["amount_usdc"] == 0.50


@respx.mock
async def test_team_report_degrades_without_league_users(paid_app: Store) -> None:
    """Display names are cosmetic: losing them must not lose the paid report."""
    mock_team_report_league()
    respx.get(f"{SLEEPER_BASE}/league/L1/users").mock(return_value=httpx.Response(500))
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200


@respx.mock
async def test_team_report_survives_one_missing_week(paid_app: Store) -> None:
    """A single failed week is dropped; the rest of the season still reports."""
    mock_team_report_league()
    respx.get(f"{SLEEPER_BASE}/league/L1/matchups/2").mock(return_value=httpx.Response(500))
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    assert response.json()["manager_review"]["lineup_efficiency_pct"] > 0


@respx.mock
async def test_team_report_for_a_future_week_keeps_history_at_completed_weeks(
    paid_app: Store,
) -> None:
    """Asking about week N+2 must not pull the partly-played current week in."""
    active = mock_team_report_league()
    beyond = respx.get(f"{SLEEPER_BASE}/league/L1/matchups/{WEEK + 1}").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan", "week": WEEK + 2}, headers=PAID
        )

    assert response.status_code == 200
    assert response.json()["week"] == WEEK + 2
    assert not active.called, "the active, partly-played week must not be analysed"
    assert not beyond.called


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/team-report", {"sleeper_username": "../../players/nfl"}),
        ("/v1/team-report", {"sleeper_username": "ryan", "league_id": "1/../../x"}),
        ("/v1/roster", {"sleeper_username": ".."}),
        ("/v1/roster", {"roster": [{"name": f"Player {i}"} for i in range(41)]}),
        ("/v1/player", {"name": "x" * 101}),
        ("/v1/draft-report", {"draft_id": "1?x=1"}),
    ],
)
async def test_unsafe_or_oversized_bodies_are_422_and_release_the_payment(
    paid_app: Store, path: str, body: dict[str, Any]
) -> None:
    """Rejected at the edge, before any Sleeper call, and the payment is not spent."""
    with respx.mock(assert_all_called=False) as router:
        sleeper = router.route(host="api.sleeper.app").mock(return_value=httpx.Response(200))
        async with api_client() as client:
            response = await client.post(path, json=body, headers=PAID)

    assert response.status_code == 422
    assert not sleeper.called
    assert await paid_app.list(RECEIPTS_COLLECTION) == []
    assert await paid_app.list(PAYMENT_IDEMPOTENCY_COLLECTION) == []


#: A week-1 schedule: roster 1 (ours) drawn against roster 2, both lineups set.
WEEK_ONE_SCHEDULE: list[dict[str, Any]] = [
    {"roster_id": 1, "matchup_id": 1, "points": 0.0, "starters": ["1003", "1001", "1006"]},
    {"roster_id": 2, "matchup_id": 1, "points": 0.0, "starters": ["1012", "1019", "1013"]},
]


def mock_league_draft(picks: list[dict[str, Any]] | None = None) -> None:
    """The league's completed draft. Pass ``[]`` for a league that never drafted."""
    drafts = [{"draft_id": "D1", "start_time": 1, "status": "complete"}] if picks else []
    respx.get(f"{SLEEPER_BASE}/league/L1/drafts").mock(
        return_value=httpx.Response(200, json=drafts)
    )
    if picks:
        respx.get(f"{SLEEPER_BASE}/draft/D1/picks").mock(
            return_value=httpx.Response(200, json=picks)
        )


#: A full draft for roster 1: one pick per starting slot (QB/RB/WR/TE/FLEX), so
#: it is gradeable against the market's board. Chase at 1 is fair; Bijan (market
#: 2) at 25 and Bowers (market 14) at 30 are clear values.
MY_DRAFT_PICKS: list[dict[str, Any]] = [
    {"player_id": "1003", "roster_id": 1, "picked_by": "u1", "round": 1, "pick_no": 1},
    {"player_id": "1006", "roster_id": 1, "picked_by": "u1", "round": 2, "pick_no": 20},
    {"player_id": "1001", "roster_id": 1, "picked_by": "u1", "round": 3, "pick_no": 25},
    {"player_id": "1018", "roster_id": 1, "picked_by": "u1", "round": 4, "pick_no": 30},
    {"player_id": "1010", "roster_id": 1, "picked_by": "u1", "round": 5, "pick_no": 35},
    {"player_id": "1012", "roster_id": 2, "picked_by": "u2", "round": 1, "pick_no": 2},
]

#: A rookie/keeper round: fewer picks than the lineup has starting slots.
PARTIAL_DRAFT_PICKS: list[dict[str, Any]] = [
    {"player_id": "1012", "roster_id": 1, "picked_by": "u1", "round": 1, "pick_no": 1},
    {"player_id": "1013", "roster_id": 1, "picked_by": "u1", "round": 2, "pick_no": 12},
]


@respx.mock
async def test_team_report_in_week_one_reports_the_draft_and_the_matchup(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing played yet, so report the two things that did happen.

    The old behaviour billed 0.75 USDC for a page of zeros: every metric 0.0,
    every positional rank 1, every grade "B-". Those are not weak assessments,
    they are no assessment, and they read as the former.
    """
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=WEEK_ONE_SCHEDULE)
    mock_league_draft(MY_DRAFT_PICKS)
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    body = response.json()
    assert body["week"] == 1

    outlook = body["preseason_outlook"]
    assert outlook, "week 1 must carry a preseason outlook"
    assert outlook["draft_id"] == "D1"
    assert outlook["draft_grade"], "the draft is gradeable and must be graded"
    assert outlook["positional_balance"], "draft balance stands in for league-relative grades"

    week_one = outlook["week_one"]
    assert week_one["opponent_roster_id"] == 2
    # Our starters are market 1/2/18; theirs are 88/140 and one unranked. The
    # lean must follow the numbers, and must not be a fabricated percentage.
    assert week_one["my_market_score"] > week_one["opponent_market_score"]
    assert week_one["lean"] == "clear edge"
    assert "not an ADP" in week_one["basis"]
    assert "%" not in week_one["lean"]

    # The zeros must no longer masquerade as assessments.
    assert body["positional_strength_vs_league"] == []
    assert "not yet played" in " ".join(body["manager_review"]["observations"])
    assert "No games played yet" in body["verdict"]


@respx.mock
async def test_team_report_bows_out_when_nothing_has_happened_at_all(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No games, no draft, no opponent: refuse the sale rather than bill zeros."""
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=[])
    mock_league_draft([])
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not charged" in detail
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


@respx.mock
async def test_team_report_still_answers_with_a_draft_but_no_schedule(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Half the preseason signal is still worth what was paid for it."""
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=[])
    mock_league_draft(MY_DRAFT_PICKS)
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    outlook = response.json()["preseason_outlook"]
    assert outlook["draft_grade"]
    assert outlook["week_one"] is None


@respx.mock
async def test_team_report_does_not_grade_a_partial_draft(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rookie round is not a draft that built this lineup, so it is not graded.

    Two picks scored against a full global market rank would read as two huge
    reaches and grade F — a confident number that means nothing. Same trap the
    draft board avoids by ranking within its own board.
    """
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=WEEK_ONE_SCHEDULE)
    mock_league_draft(PARTIAL_DRAFT_PICKS)
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 200
    outlook = response.json()["preseason_outlook"]
    assert outlook["draft_grade"] is None, "a partial draft must not carry a letter grade"
    assert outlook["positional_balance"] == []
    assert "partial draft" in outlook["draft_summary"]
    # The matchup still carries the report.
    assert outlook["week_one"]["lean"] == "clear edge"


@respx.mock
async def test_team_report_bows_out_on_a_partial_draft_with_no_schedule(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ungradeable picks and no opponent leaves nothing worth 0.75 USDC."""
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=[])
    mock_league_draft(PARTIAL_DRAFT_PICKS)
    async with api_client() as client:
        response = await client.post(
            "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
        )

    assert response.status_code == 503
    assert "not charged" in response.json()["detail"]
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


async def test_cold_store_is_503_and_settles_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ingested data -> 503 before the engine runs; the payment never settles."""
    configure(monkeypatch, X402_MODE="mock")
    async with api_client() as client:
        response = await client.get("/v1/trending", headers=PAID)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "not charged" in detail
    # The 503 names what is missing, so an operator knows which task to run.
    assert "players" in detail and "trending" in detail
    assert await store.list(RECEIPTS_COLLECTION) == []


async def test_stale_required_data_is_503_and_settles_nothing(paid_app: Store) -> None:
    freshness = await paid_app.get(META_COLLECTION, FRESHNESS_DOC_ID)
    assert freshness is not None
    freshness["players"] = "2000-01-01T00:00:00Z"
    await paid_app.set(META_COLLECTION, FRESHNESS_DOC_ID, freshness)

    async with api_client() as client:
        response = await client.get("/v1/trending", headers=paid("trending"))

    assert response.status_code == 503
    assert "missing or stale: players" in response.json()["detail"]
    assert "not charged" in response.json()["detail"]
    assert await paid_app.list(RECEIPTS_COLLECTION) == []


async def test_missing_preseason_datasets_are_exempted_by_a_live_gap(
    paid_app: Store,
) -> None:
    """Ingest withholds markers for unpublished stats; that must not block a sale."""
    freshness = await paid_app.get(META_COLLECTION, FRESHNESS_DOC_ID)
    assert freshness is not None
    freshness.pop("weekly_stats", None)
    freshness.pop("usage_trends", None)
    await paid_app.set(META_COLLECTION, FRESHNESS_DOC_ID, freshness)
    await paid_app.set(
        META_COLLECTION,
        "preseason_gap",
        {
            "season": SEASON,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datasets": ["weekly_stats", "usage_trends"],
        },
    )

    async with api_client() as client:
        response = await client.get("/v1/sleepers", headers=paid("preseason"))

    assert response.status_code == 200
    receipts = await paid_app.list(RECEIPTS_COLLECTION)
    assert [receipt["endpoint"] for receipt in receipts] == ["sleepers"]


async def test_a_gap_on_a_cold_store_refuses_instead_of_selling_an_empty_board(
    paid_app: Store,
) -> None:
    """The exemption must not outrun the data it claims to stand in for.

    A fresh preseason deployment ingests no rows, writes no stat markers, and
    records exactly the same gap a warm store does. Believing it there passes
    readiness and settles a payment for a board ``usage_trends`` has nothing to
    fill -- an empty paid answer, which is worse than no answer and, unlike the
    503, is billed.
    """
    freshness = await paid_app.get(META_COLLECTION, FRESHNESS_DOC_ID)
    assert freshness is not None
    freshness.pop("weekly_stats", None)
    freshness.pop("usage_trends", None)
    await paid_app.set(META_COLLECTION, FRESHNESS_DOC_ID, freshness)
    for doc in await paid_app.list(USAGE_TRENDS_COLLECTION):
        await paid_app.delete(USAGE_TRENDS_COLLECTION, doc["_id"])
    await paid_app.set(
        META_COLLECTION,
        "preseason_gap",
        {
            "season": SEASON,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datasets": ["weekly_stats", "usage_trends"],
        },
    )

    async with api_client() as client:
        response = await client.get("/v1/sleepers", headers=paid("cold-preseason"))

    assert response.status_code == 503
    assert "not charged" in response.json()["detail"]
    assert await paid_app.list(RECEIPTS_COLLECTION) == [], "nothing may settle"


async def test_partial_ingest_only_serves_the_endpoints_it_can_answer(paid_app: Store) -> None:
    """``nightly`` + ``trending`` ran, ``stats`` did not.

    ``meta/freshness`` is non-empty, but ``/v1/sleepers`` has no usage rollups to
    build picks from — settling $0.25 for an empty board is the bug this guards.
    """
    await paid_app.set(
        META_COLLECTION,
        FRESHNESS_DOC_ID,
        {
            "players": "2026-09-29T04:00:03Z",
            "player_index": "2026-09-29T04:00:03Z",
            "id_map": "2026-09-29T04:00:03Z",
            "trending": "2026-09-30T13:30:00Z",
        },
    )
    async with api_client() as client:
        trending = await client.get("/v1/trending", headers=paid("trending"))
        sleepers = await client.get("/v1/sleepers", headers=paid("sleepers"))

    assert trending.status_code == 200
    assert sleepers.status_code == 503
    detail = sleepers.json()["detail"]
    assert "usage_trends" in detail
    assert "weekly_stats" in detail
    assert "not charged" in detail

    # Only the endpoint that could actually answer was billed.
    receipts = await paid_app.list(RECEIPTS_COLLECTION)
    assert [r["endpoint"] for r in receipts] == ["trending"]


def test_every_paid_endpoint_declares_its_required_datasets() -> None:
    """A new endpoint must not default to 'any freshness will do'."""
    assert set(REQUIRED_DATASETS) == set(ENDPOINT_KEYS)
    assert all(REQUIRED_DATASETS[key] for key in ENDPOINT_KEYS)


@respx.mock
async def test_week_one_facts_are_sanitised_before_any_engine_sees_them(
    paid_app: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zeros are stripped in the route, not inside one engine.

    The ADK synthesis agent is instructed to reproduce 'manager_review' and
    'positional_strength_vs_league' exactly, so a "B-" computed from zero games
    makes faithful narration produce a confident lie. Guarding that in the
    deterministic engine alone would leave the trap armed for ENGINE=adk, so the
    guard runs before the engine seam — and this test watches that seam.
    """
    configure(monkeypatch, X402_MODE="mock", WEEK_OVERRIDE=1)
    mock_team_report_league(weeks=0, active_week=1, active_entries=WEEK_ONE_SCHEDULE)
    mock_league_draft(MY_DRAFT_PICKS)

    seen: dict[str, Any] = {}
    inner = get_engine()

    class Watching(AnalysisEngine):
        name = "watching"

        async def analyze(
            self, endpoint_key: str, request_context: dict[str, Any]
        ) -> AnalysisResponse:
            seen.update(request_context)
            return await inner.analyze(endpoint_key, request_context)

    set_engine(Watching())
    try:
        async with api_client() as client:
            response = await client.post(
                "/v1/team-report", json={"sleeper_username": "ryan"}, headers=PAID
            )
    finally:
        set_engine(None)

    assert response.status_code == 200
    facts = seen["team_analytics"]
    assert "no_matchup_history" in facts["warnings"]
    # Whatever narrates these has nothing misleading to reproduce.
    assert facts["positional_strength_vs_league"] == []
    assert "not yet played" in facts["manager_review"]["observations"][0]
    # And it is handed the preseason material to narrate instead.
    assert seen["preseason"]["gradeable"] is True
    assert seen["preseason"]["week_one"]["lean"] == "clear edge"
