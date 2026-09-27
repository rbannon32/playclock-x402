"""Free routes: health, catalog and the trending teaser.

These must answer without any payment header at all — that is the whole point of
the free tier — and the catalog must agree with the price table and the cache
policy the paid routes actually apply.

The helpers here (``configure``, ``api_client``) are shared with
``test_routes_paid.py`` and ``test_main.py``; settings are driven through the
environment because every module in the app reads the cached
``get_settings()`` singleton at request time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from api.core.config import ENDPOINT_KEYS, get_settings
from api.core.store import Store
from api.data.stats_store import PLAYERS_COLLECTION, TRENDING_COLLECTION
from api.evals.golden import SEASON, WEEK, seed_store
from api.routes import API_VERSION, CACHE_TTL_SECONDS
from api.routes.free import FREE_ENDPOINTS, PREVIEW_LIMIT, clear_stats_cache
from api.x402.receipts import RECEIPTS_COLLECTION, backfill_receipt_stats, log_receipt
from api.x402.schemas_compat import network_caip2

#: Environment every route test starts from: hermetic store, no LLM, no payments.
ENV_DEFAULTS: dict[str, str] = {
    "STORE_BACKEND": "memory",
    "ENGINE": "deterministic",
    "X402_MODE": "disabled",
    "X402_NETWORK": "testnet",
    "SEASON": str(SEASON),
    "WEEK_OVERRIDE": str(WEEK),
}


def configure(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    """Point the process settings at a test configuration.

    Env vars rather than an injected ``Settings`` object: request handlers, the
    payment dependency and the engine factory all call ``get_settings()``
    themselves, so the environment is the only seam that reaches all of them.
    """
    for key, value in {**ENV_DEFAULTS, **overrides}.items():
        monkeypatch.setenv(key, str(value))
    get_settings.cache_clear()


@asynccontextmanager
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    """Drive a freshly built app over ASGI — no server, no sockets."""
    from api.main import create_app  # noqa: PLC0415 - built after configure()

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def seeded(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """The golden fixture season loaded into the conftest MemoryStore."""
    configure(monkeypatch)
    await seed_store(store)
    return store


async def test_health_reports_configuration_and_freshness(seeded: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == API_VERSION
    assert body["week"] == WEEK
    assert body["season"] == SEASON
    assert body["configured_season"] == SEASON
    assert body["season_mismatch"] is False
    assert body["store_backend"] == "memory"
    assert body["engine"] == "deterministic"
    assert body["data_freshness"], "seeded store should report ingest freshness"


async def test_health_is_degraded_without_ingest(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty store is reachable but useless — say so rather than claim 'ok'."""
    configure(monkeypatch)
    async with api_client() as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"


async def test_health_is_degraded_when_ingest_markers_are_old(seeded: Store) -> None:
    await seeded.set("meta", "freshness", {"players": "2000-01-01T00:00:00Z"})
    async with api_client() as client:
        response = await client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["stale_datasets"] == ["players"]


async def test_health_does_not_age_out_what_upstream_has_not_published(
    seeded: Store,
) -> None:
    """The preseason gap applies here exactly as it does at the paid gate.

    Before Week 1 nflverse has no stats file and nflreadpy rejects the season, so
    these markers cannot be refreshed by anyone. Counting them stale reports
    `degraded` for weeks over a condition the API itself has ruled not a fault,
    which is how an operator learns to ignore health. They are reported in
    `unavailable_datasets` instead, so the state stays visible.
    """
    stale_markers = {name: "2000-01-01T00:00:00Z" for name in ("weekly_stats", "injuries")}
    await seeded.set("meta", "freshness", stale_markers)
    # The exemption is believed per dataset and only where the store can still
    # show a row behind it, so this is a store that genuinely holds both.
    await seeded.set("injuries", "4046", {"player_id": "4046", "status": "Questionable"})
    await seeded.set(
        "meta",
        "preseason_gap",
        {
            "season": 2026,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datasets": ["weekly_stats", "injuries"],
        },
    )
    async with api_client() as client:
        body = (await client.get("/v1/health")).json()

    assert body["status"] == "ok"
    assert body["stale_datasets"] == []
    assert body["unavailable_datasets"] == ["injuries", "weekly_stats"]


async def test_health_reports_missing_preseason_datasets_as_unavailable(
    seeded: Store,
) -> None:
    """Gap datasets remain visible when ingest correctly withholds their markers."""
    freshness = await seeded.get("meta", "freshness")
    assert freshness is not None
    freshness.pop("weekly_stats", None)
    freshness.pop("usage_trends", None)
    await seeded.set("meta", "freshness", freshness)
    await seeded.set(
        "meta",
        "preseason_gap",
        {
            "season": SEASON,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datasets": ["weekly_stats", "usage_trends"],
        },
    )

    async with api_client() as client:
        body = (await client.get("/v1/health")).json()

    assert body["status"] == "ok"
    assert body["stale_datasets"] == []
    assert body["unavailable_datasets"] == ["usage_trends", "weekly_stats"]


async def test_health_still_goes_red_when_the_gap_marker_goes_unrefreshed(
    seeded: Store,
) -> None:
    """A stopped ingest must not hide behind a gap it declared days ago.

    `exempt_datasets` trusts the marker only while a recent run keeps
    re-affirming it, so an ingest that dies takes the exemption with it.
    """
    await seeded.set("meta", "freshness", {"weekly_stats": "2000-01-01T00:00:00Z"})
    await seeded.set(
        "meta",
        "preseason_gap",
        {
            "season": 2026,
            "recorded_at": "2000-01-01T00:00:00Z",
            "datasets": ["weekly_stats"],
        },
    )
    async with api_client() as client:
        body = (await client.get("/v1/health")).json()

    assert body["status"] == "degraded"
    assert body["stale_datasets"] == ["weekly_stats"]
    assert body["unavailable_datasets"] == []


async def test_catalog_lists_every_endpoint_with_matching_prices(seeded: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/catalog")

    assert response.status_code == 200
    body = response.json()
    settings = get_settings()

    entries = body["endpoints"]
    assert len(entries) == len(FREE_ENDPOINTS) + len(ENDPOINT_KEYS)

    free = [e for e in entries if e["free"]]
    assert {e["path"] for e in free} == {
        "/v1/health",
        "/v1/catalog",
        "/v1/trending/preview",
        "/v1/stats",
    }
    assert all(e["price_usdc"] == 0.0 and e["key"] == "" for e in free)

    paid = {e["key"]: e for e in entries if not e["free"]}
    assert list(paid) == list(ENDPOINT_KEYS)
    for key, entry in paid.items():
        assert entry["price_usdc"] == settings.price_for(key)
        assert entry["cache_ttl_seconds"] == CACHE_TTL_SECONDS.get(key)

    assert paid["team_report"]["path"] == "/v1/team-report"
    assert paid["team_report"]["request_schema"] == "TeamReportRequest"
    assert paid["report"]["cache_ttl_seconds"] == 12 * 3600
    assert paid["trending"]["cache_ttl_seconds"] == 6 * 3600
    assert paid["player"]["cache_ttl_seconds"] is None

    assert body["network"] == "testnet"
    assert body["asset_id"] > 0
    assert "goplausible" in body["facilitator_url"]
    assert body["challenge_tag"] == "x402-global-challenge"
    assert "nflverse" in body["attribution"]


async def test_trending_preview_is_five_rows_without_analysis(seeded: Store) -> None:
    async with api_client() as client:
        response = await client.get("/v1/trending/preview")

    assert response.status_code == 200
    body = response.json()
    assert 0 < len(body["players"]) <= PREVIEW_LIMIT
    assert body["lookback_hours"] == 24
    assert "/v1/trending" in body["upsell"]
    for row in body["players"]:
        assert row["name"] and row["trend_count"] >= 0
        assert row["trend"] in ("add", "drop")
        # The teaser gives away identity and counts only.
        assert "analysis" not in row
        assert "verdict" not in row


async def test_trending_preview_tops_up_from_drops(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fewer than five adds ingested: fill the teaser from the drop board."""
    configure(monkeypatch)
    await store.set(PLAYERS_COLLECTION, "1001", {"name": "Bijan Robinson", "position": "RB"})
    await store.set(
        TRENDING_COLLECTION,
        "add",
        {"kind": "add", "lookback_hours": 48, "entries": [{"player_id": "1001", "count": 10}]},
    )
    await store.set(
        TRENDING_COLLECTION,
        "drop",
        {"kind": "drop", "lookback_hours": 48, "entries": [{"player_id": "9999", "count": 4}]},
    )

    async with api_client() as client:
        body = (await client.get("/v1/trending/preview")).json()

    assert [row["trend"] for row in body["players"]] == ["add", "drop"]
    assert body["lookback_hours"] == 48
    # Identity is joined from players/ when the trending poll did not carry it.
    assert body["players"][0]["name"] == "Bijan Robinson"
    # An id with no player document still renders rather than vanishing.
    assert body["players"][1]["name"] == "9999"


async def test_trending_preview_is_empty_before_the_poll_runs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch)
    async with api_client() as client:
        response = await client.get("/v1/trending/preview")

    assert response.status_code == 200
    assert response.json()["players"] == []


async def test_free_routes_need_no_payment_even_in_mock_mode(
    seeded: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Payment gating is a per-route dependency, not a path prefix (tech spec §3)."""
    configure(monkeypatch, X402_MODE="mock")
    async with api_client() as client:
        for path in ("/v1/health", "/v1/catalog", "/v1/trending/preview"):
            assert (await client.get(path)).status_code == 200, path


async def test_stats_uses_fixed_rollups_after_backfill(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed migration removes the receipts collection from the request path."""
    configure(monkeypatch)
    active = network_caip2(get_settings())
    await store.add(
        RECEIPTS_COLLECTION,
        {
            "network": active,
            "payer": "legacy-payer",
            "endpoint": "trending",
            "amount_usdc": 0.1,
            "ts": "2026-09-01T00:00:00Z",
        },
    )
    # A writer may be deployed before backfill completes. The later backfill
    # sees this receipt again, but its durable event marker keeps the total exact.
    await log_receipt(
        store,
        txid="during-migration",
        payer="writer-payer",
        endpoint="player",
        amount_usdc=0.25,
        network=active,
        ts="2026-09-01T12:00:00Z",
        payment_hash="b" * 64,
    )
    assert await backfill_receipt_stats(store, active) == 2
    await log_receipt(
        store,
        txid="new-settlement",
        payer="new-payer",
        endpoint="player",
        amount_usdc=0.25,
        network=active,
        ts="2026-09-02T00:00:00Z",
        payment_hash="a" * 64,
    )
    # A settlement retry has one payment hash and must not inflate public totals.
    await log_receipt(
        store,
        txid="new-settlement",
        payer="new-payer",
        endpoint="player",
        amount_usdc=0.25,
        network=active,
        ts="2026-09-02T00:00:00Z",
        payment_hash="a" * 64,
    )
    # Re-running the migration observes all three receipts but changes no shard.
    assert await backfill_receipt_stats(store, active) == 3

    async def receipt_scan_is_not_allowed(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("completed stats migration must not stream receipts")

    monkeypatch.setattr(store, "list", receipt_scan_is_not_allowed)
    clear_stats_cache()
    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    assert body["paid_analyses"] == 3
    assert body["unique_payers"] == 3
    assert body["usdc_settled"] == 0.6
    assert body["since"] == "2026-09-01T00:00:00Z"
    assert body["by_endpoint"] == [
        {"key": "player", "paid_calls": 2, "usdc": 0.5},
        {"key": "trending", "paid_calls": 1, "usdc": 0.1},
    ]


async def test_health_will_not_call_a_cold_preseason_store_merely_unavailable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gap marker on an empty store is missing data, not a publishing calendar.

    A fresh preseason deployment ingests no rows, writes no stat markers, and
    records the same gap a warm store does. Believing it there would report
    `ok` over a store that can answer nothing.
    """
    configure(monkeypatch)
    await store.set("meta", "freshness", {"players": datetime.now(UTC).isoformat()})
    await store.set(
        "meta",
        "preseason_gap",
        {
            "season": SEASON,
            "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "datasets": ["weekly_stats", "usage_trends"],
        },
    )

    async with api_client() as client:
        body = (await client.get("/v1/health")).json()

    assert body["unavailable_datasets"] == [], "nothing in the store backs the claim"
