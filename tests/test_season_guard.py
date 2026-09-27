"""Refusing to sell last season's football as this season's.

The failure this guards is the quietest one in the stack. Every dataset is
present, every freshness marker is stamped, ``missing_datasets`` returns
nothing — and every answer is about a season that finished. Fresh data for the
wrong year is indistinguishable from fresh data for the right one until a
customer reads it, which is after they have paid.

Observed live on 2026-08-30: ``/v1/health`` reported ``season: 2025, week: 18``
against a service configured for 2026, and nothing anywhere reported a problem.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from api.core.config import Settings
from api.core.store import Store
from api.core.week import current_season, ingested_season
from api.data.stats_store import META_COLLECTION
from api.evals.golden import SEASON, seed_store
from api.routes.paid import stale_season
from api.x402 import RECEIPTS_COLLECTION, clear_idempotency_cache, set_facilitator
from api.x402.schemas_compat import MOCK_PAYMENT_HEADER, PAYMENT_SIGNATURE_HEADER
from tests.test_routes_free import api_client, configure

SCHEDULE_WEEKS = "schedule_weeks"
PAID = {PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}


@pytest.fixture(autouse=True)
def _reset_payment_state() -> Iterator[None]:
    clear_idempotency_cache()
    set_facilitator(None)
    yield
    clear_idempotency_cache()
    set_facilitator(None)


def settings_for(season: int) -> Settings:
    return Settings(_env_file=None, season=season)  # type: ignore[call-arg]


async def set_ingested_season(store: Store, season: int | None) -> None:
    """Rewrite the schedule marker, as an ingest run for that season would."""
    doc = await store.get(META_COLLECTION, SCHEDULE_WEEKS) or {}
    if season is None:
        doc.pop("season", None)
    else:
        doc["season"] = season
    await store.set(META_COLLECTION, SCHEDULE_WEEKS, doc)


# --------------------------------------------------------------------------
# the marker itself
# --------------------------------------------------------------------------


async def test_ingested_season_reports_what_the_store_holds(store: Store) -> None:
    await seed_store(store)
    assert await ingested_season(store) == SEASON


async def test_ingested_season_is_none_when_ingest_has_not_run(store: Store) -> None:
    # Distinct from "the wrong season": plain missing data, reported elsewhere.
    assert await ingested_season(store) is None


async def test_current_season_still_falls_back_to_settings(store: Store) -> None:
    assert await current_season(store, settings_for(2031)) == 2031


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------


async def test_matching_seasons_are_not_stale(store: Store) -> None:
    await seed_store(store)
    assert await stale_season(store, settings_for(SEASON)) is None


async def test_a_store_from_last_season_is_stale(store: Store) -> None:
    await seed_store(store)
    await set_ingested_season(store, SEASON - 1)

    assert await stale_season(store, settings_for(SEASON)) == (SEASON - 1, SEASON)


async def test_an_empty_store_is_not_reported_as_stale(store: Store) -> None:
    # `missing_datasets` says something more useful about a cold store, and two
    # 503s racing to explain the same thing helps nobody.
    assert await stale_season(store, settings_for(SEASON)) is None


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------


@pytest.fixture
async def seeded_mock(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch, X402_MODE="mock", SEASON=str(SEASON))
    await seed_store(store)
    return store


async def test_health_degrades_and_reports_the_wrong_season(seeded_mock: Store) -> None:
    await set_ingested_season(seeded_mock, SEASON - 1)

    async with api_client() as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["season"] == SEASON - 1
    assert body["configured_season"] == SEASON
    assert body["season_mismatch"] is True


async def test_a_paid_call_is_refused_and_unbilled_on_the_wrong_season(
    seeded_mock: Store,
) -> None:
    await set_ingested_season(seeded_mock, SEASON - 1)

    async with api_client() as client:
        response = await client.get("/v1/trending", headers=PAID)

    assert response.status_code == 503
    detail = response.json()["detail"]
    # Both years named: "stale data" is not actionable, "2025 vs 2026" is.
    assert str(SEASON - 1) in detail and str(SEASON) in detail
    assert "not charged" in detail
    # Non-2xx never settles.
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


async def test_the_right_season_sells_normally(seeded_mock: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/trending", headers=PAID)

    assert response.status_code == 200
    assert len(await seeded_mock.list(RECEIPTS_COLLECTION)) == 1


async def test_every_paid_endpoint_is_covered_not_just_one(seeded_mock: Store) -> None:
    # The guard sits in require_ingested_data, which every paid route runs.
    await set_ingested_season(seeded_mock, SEASON - 1)

    async with api_client() as client:
        board = await client.get("/v1/draft-board", headers=PAID)
        player = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=PAID)

    assert board.status_code == 503
    assert player.status_code == 503
    assert await seeded_mock.list(RECEIPTS_COLLECTION) == []


# --------------------------------------------------------------------------
# the warmer
# --------------------------------------------------------------------------


async def test_precompute_refuses_and_exits_non_zero_on_the_wrong_season(
    store: Store,
) -> None:
    """A warmed board outlives the mistake: it is served for its whole TTL."""
    from ingest.precompute import PrecomputeError, warm_response_cache

    await seed_store(store)
    await set_ingested_season(store, SEASON - 1)
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="deterministic",
        x402_mode="disabled",
        season=SEASON,
        week_override=4,
    )

    with pytest.raises(PrecomputeError) as excinfo:
        await warm_response_cache(store, settings, week=4)

    summary = excinfo.value.summary
    assert summary["warmed"] == 0
    assert summary["blocked"] > 0
    assert str(SEASON - 1) in str(excinfo.value)

    from api.data.cache import CACHE_COLLECTION

    assert await store.list(CACHE_COLLECTION) == [], "nothing may be cached from the wrong year"


# --------------------------------------------------------------------------
# the cache is the dangerous path, not the engine
# --------------------------------------------------------------------------


async def test_a_warm_board_from_last_season_is_still_refused(seeded_mock: Store) -> None:
    """The readiness check has to run BEFORE the cache lookup.

    A warm entry never reaches the engine, so a guard living inside `analyze`
    would only cover a cache miss — and the entry most likely to be wrong is
    exactly the one warmed from last season, served as a paid hit for its whole
    TTL. This is the reproduction: warm it, then roll the store back a year.
    """
    async with api_client() as client:
        first = await client.get("/v1/trending", headers=PAID)
    assert first.json()["meta"]["cache"] == "fresh"

    await set_ingested_season(seeded_mock, SEASON - 1)
    before = len(await seeded_mock.list(RECEIPTS_COLLECTION))

    async with api_client() as client:
        second = await client.get(
            "/v1/trending",
            headers={PAYMENT_SIGNATURE_HEADER: _distinct("second")},
        )

    assert second.status_code == 503
    assert len(await seeded_mock.list(RECEIPTS_COLLECTION)) == before, "a 503 must not settle"


def _distinct(nonce: str) -> str:
    import base64
    import json

    body = {"x402Version": 2, "payload": {"mock": True, "nonce": nonce}}
    return base64.b64encode(json.dumps(body).encode()).decode()
