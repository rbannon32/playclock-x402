"""The two free-route additions: proof of usage, and a cap on the cost of it.

Payment is the rate limit on the paid routes — an agent that wants a thousand
analyses buys a thousand analyses. The free routes have no such governor and
three of them read Firestore per call, so an advert or a crawler is otherwise
an unbounded bill against a $50/month budget.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from api.core.config import Settings
from api.core.ratelimit import _HITS, clear_rate_limits
from api.core.store import Store
from api.data.predictions import BACKTEST_COLLECTION
from api.evals.golden import SEASON, seed_store
from api.routes.free import clear_stats_cache
from api.x402 import RECEIPTS_COLLECTION
from api.x402.schemas_compat import (
    ALGORAND_MAINNET_CAIP2,
    ALGORAND_TESTNET_CAIP2,
)
from tests.test_routes_free import api_client, configure


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    clear_rate_limits()
    clear_stats_cache()
    yield
    clear_rate_limits()
    clear_stats_cache()


@pytest.fixture
async def seeded(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch)
    await seed_store(store)
    return store


async def receipt(store: Store, doc_id: str, **fields: object) -> None:
    """Write a settled receipt, defaulting it to the network under test."""
    fields.setdefault("network", ALGORAND_TESTNET_CAIP2)
    await store.set(RECEIPTS_COLLECTION, doc_id, fields)


# --------------------------------------------------------------------------
# /v1/stats
# --------------------------------------------------------------------------


async def test_stats_is_empty_and_free_before_anyone_pays(seeded: Store) -> None:
    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    assert body["paid_analyses"] == 0
    assert body["unique_payers"] == 0
    assert body["usdc_settled"] == 0.0
    assert body["since"] is None


async def test_stats_aggregates_settled_receipts(seeded: Store) -> None:
    await receipt(
        seeded, "r1", endpoint="trending", amount_usdc=0.10, payer="A", ts="2026-10-01T00:00:00Z"
    )
    await receipt(
        seeded, "r2", endpoint="trending", amount_usdc=0.10, payer="B", ts="2026-10-02T00:00:00Z"
    )
    await receipt(
        seeded, "r3", endpoint="report", amount_usdc=0.50, payer="A", ts="2026-10-03T00:00:00Z"
    )

    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    assert body["paid_analyses"] == 3
    assert body["unique_payers"] == 2
    assert body["usdc_settled"] == pytest.approx(0.70)
    assert body["since"] == "2026-10-01T00:00:00Z"
    assert body["by_endpoint"][0] == {"key": "trending", "paid_calls": 2, "usdc": 0.20}


async def test_stats_never_republishes_a_payer_address(seeded: Store) -> None:
    """How many people paid is the useful number, and the safe one."""
    await receipt(
        seeded,
        "r1",
        endpoint="trending",
        amount_usdc=0.10,
        payer="SECRETADDR",
        ts="2026-10-01T00:00:00Z",
    )

    async with api_client() as client:
        raw = (await client.get("/v1/stats")).text

    assert "SECRETADDR" not in raw


async def test_stats_is_memoised_rather_than_rescanning_per_request(seeded: Store) -> None:
    """A free endpoint must not scan a growing collection on every call."""
    await receipt(
        seeded, "r1", endpoint="trending", amount_usdc=0.10, payer="A", ts="2026-10-01T00:00:00Z"
    )

    async with api_client() as client:
        first = (await client.get("/v1/stats")).json()
        await receipt(
            seeded,
            "r2",
            endpoint="trending",
            amount_usdc=0.10,
            payer="B",
            ts="2026-10-02T00:00:00Z",
        )
        second = (await client.get("/v1/stats")).json()

    assert first == second, "the second call should have been served from memory"
    clear_stats_cache()
    async with api_client() as client:
        assert (await client.get("/v1/stats")).json()["paid_analyses"] == 2


async def test_stats_never_reports_another_network_s_volume(seeded: Store) -> None:
    """Receipts from a different network are not this network's volume.

    `network` in the response is read from settings, so an unfiltered scan would
    republish every TestNet payment under a MainNet label the moment the network
    flips — a public endpoint overstating real payment volume, on a project whose
    rules explicitly police manufactured volume.
    """
    await receipt(
        seeded,
        "testnet-1",
        endpoint="trending",
        amount_usdc=0.10,
        payer="A",
        ts="2026-10-01T00:00:00Z",
    )
    await receipt(
        seeded,
        "mainnet-1",
        endpoint="trending",
        amount_usdc=0.10,
        payer="B",
        ts="2026-10-02T00:00:00Z",
        network=ALGORAND_MAINNET_CAIP2,
    )

    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    # The suite runs on TestNet, so only the TestNet receipt counts.
    assert body["paid_analyses"] == 1
    assert body["usdc_settled"] == 0.10
    assert body["unique_payers"] == 1


async def test_stats_reports_no_accuracy_before_the_first_backtest(seeded: Store) -> None:
    """No played week, no hit rate — never a zero that reads as "always wrong"."""
    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    assert body["accuracy"] is None


async def test_stats_publishes_the_backtest_hit_rate(seeded: Store) -> None:
    """The aggregate is republished as written, with the rule that produced it."""
    await seeded.set(
        BACKTEST_COLLECTION,
        str(SEASON),
        {
            "season": SEASON,
            "updated_at": "2026-09-17T12:00:00Z",
            "weeks": [1, 2],
            "overall": {"scored": 5, "hits": 3, "hit_rate": 0.6},
            "by_endpoint": [
                {"key": "matchup", "scored": 1, "hits": 1, "hit_rate": 1.0},
                {"key": "roster", "scored": 4, "hits": 2, "hit_rate": 0.5},
            ],
            "by_kind": [{"key": "start", "scored": 5, "hits": 3, "hit_rate": 0.6}],
            "unscorable": 0,
        },
    )

    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    accuracy = body["accuracy"]
    assert accuracy["season"] == SEASON
    assert accuracy["weeks_scored"] == [1, 2]
    assert accuracy["overall"] == {"scored": 5, "hits": 3, "hit_rate": 0.6}
    assert [row["key"] for row in accuracy["by_endpoint"]] == ["matchup", "roster"]
    assert "startable" in accuracy["method"]
    assert "by_kind" not in accuracy


async def test_stats_ignores_a_receipt_it_cannot_attribute(seeded: Store) -> None:
    """An untagged receipt is not counted: undercounting is the safe direction."""
    await store_untagged(seeded)

    async with api_client() as client:
        body = (await client.get("/v1/stats")).json()

    assert body["paid_analyses"] == 0


async def store_untagged(store: Store) -> None:
    await store.set(
        RECEIPTS_COLLECTION,
        "no-network",
        {"endpoint": "trending", "amount_usdc": 0.10, "payer": "A", "ts": "2026-10-01T00:00:00Z"},
    )


# --------------------------------------------------------------------------
# the cap
# --------------------------------------------------------------------------


async def test_free_routes_are_capped_per_client(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="3")
    await seed_store(store)

    async with api_client() as client:
        codes = [(await client.get("/v1/health")).status_code for _ in range(5)]

    assert codes == [200, 200, 200, 429, 429]


async def test_the_cap_names_a_retry_time_and_says_paid_routes_differ(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1")
    await seed_store(store)

    async with api_client() as client:
        await client.get("/v1/health")
        blocked = await client.get("/v1/health")

    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert "payment is their rate limit" in blocked.json()["detail"]


async def test_clients_are_counted_separately(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(
        monkeypatch,
        FREE_RATE_LIMIT_PER_MINUTE="1",
        TRUSTED_PROXY_HOPS="1",
    )
    await seed_store(store)

    async with api_client() as client:
        first = await client.get(
            "/v1/health",
            headers={"x-forwarded-for": "198.51.100.9, 1.1.1.1"},
        )
        second = await client.get(
            "/v1/health",
            headers={"x-forwarded-for": "198.51.100.9, 2.2.2.2"},
        )
        again = await client.get(
            "/v1/health",
            headers={"x-forwarded-for": "203.0.113.7, 1.1.1.1"},
        )

    assert first.status_code == 200
    assert second.status_code == 200, "a different client has its own budget"
    # Same caller, different caller-supplied prefix: still the same budget.
    assert again.status_code == 429


async def test_untrusted_xff_cannot_split_the_socket_peer_budget(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1")
    await seed_store(store)

    async with api_client() as client:
        first = await client.get("/v1/health", headers={"x-forwarded-for": "1.1.1.1"})
        second = await client.get("/v1/health", headers={"x-forwarded-for": "2.2.2.2"})

    assert first.status_code == 200
    assert second.status_code == 429


async def test_rate_limit_eviction_keeps_existing_clients_counters(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table at capacity evicts one LRU client instead of clearing every limit."""
    configure(
        monkeypatch,
        FREE_RATE_LIMIT_PER_MINUTE="1",
        TRUSTED_PROXY_HOPS="1",
    )
    monkeypatch.setattr("api.core.ratelimit._MAX_TRACKED", 2)
    await seed_store(store)

    async with api_client() as client:
        await client.get("/v1/health", headers={"x-forwarded-for": "198.51.100.9, 1.1.1.1"})
        await client.get("/v1/health", headers={"x-forwarded-for": "198.51.100.9, 2.2.2.2"})
        still_limited = await client.get(
            "/v1/health",
            headers={"x-forwarded-for": "203.0.113.7, 2.2.2.2"},
        )
        await client.get("/v1/health", headers={"x-forwarded-for": "198.51.100.9, 3.3.3.3"})
        still_limited_after_eviction = await client.get(
            "/v1/health",
            headers={"x-forwarded-for": "203.0.113.7, 2.2.2.2"},
        )

    assert len(_HITS) == 2
    assert still_limited.status_code == 429
    assert still_limited_after_eviction.status_code == 429


async def test_the_cap_can_be_turned_off(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="0")
    await seed_store(store)

    async with api_client() as client:
        codes = [(await client.get("/v1/health")).status_code for _ in range(6)]

    assert set(codes) == {200}


async def test_paid_routes_are_not_capped(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Payment is their limit; a cap would only stop someone spending money."""
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1", X402_MODE="mock")
    await seed_store(store)

    async with api_client() as client:
        await client.get("/v1/health")  # spend the free budget
        quotes = [(await client.get("/v1/trending")).status_code for _ in range(3)]

    assert quotes == [402, 402, 402], "a 402 quote is not rate limited"


async def test_a_spoofed_xff_prefix_cannot_buy_a_fresh_budget(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass the hop count exists to close.

    One trusted proxy appends the address it received from, so the caller is
    the rightmost value and everything left of it is caller-supplied. Selecting
    by an index measured from the start of the header picks the attacker's own
    value, and rotating it resets the limit on every request.
    """
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1", TRUSTED_PROXY_HOPS="1")
    await seed_store(store)

    async with api_client() as client:
        first = await client.get(
            "/v1/health", headers={"x-forwarded-for": "198.51.100.1, 203.0.113.9"}
        )
        rotated = await client.get(
            "/v1/health", headers={"x-forwarded-for": "198.51.100.2, 203.0.113.9"}
        )
        longer_prefix = await client.get(
            "/v1/health", headers={"x-forwarded-for": "10.0.0.1, 198.51.100.3, 203.0.113.9"}
        )

    assert first.status_code == 200
    assert rotated.status_code == 429, "a rotated prefix must not reset the window"
    assert longer_prefix.status_code == 429, "nor may a longer one"


async def test_a_sanitising_proxy_that_overwrites_the_header_still_separates_callers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-value header is the recommended topology, not a too-short one.

    Treating it as too short collapses every caller into the shared proxy
    socket, which is the opposite failure: one bucket for the whole internet.
    """
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1", TRUSTED_PROXY_HOPS="1")
    await seed_store(store)

    async with api_client() as client:
        first = await client.get("/v1/health", headers={"x-forwarded-for": "198.51.100.9"})
        other = await client.get("/v1/health", headers={"x-forwarded-for": "203.0.113.7"})
        repeat = await client.get("/v1/health", headers={"x-forwarded-for": "198.51.100.9"})

    assert first.status_code == 200
    assert other.status_code == 200, "each caller gets its own budget"
    assert repeat.status_code == 429


async def test_two_trusted_proxies_look_past_the_inner_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1", TRUSTED_PROXY_HOPS="2")
    await seed_store(store)

    async with api_client() as client:
        # [caller-supplied, client, inner proxy] -- the client is at len - 2.
        first = await client.get(
            "/v1/health", headers={"x-forwarded-for": "198.51.100.1, 203.0.113.9, 192.0.2.1"}
        )
        same_client = await client.get(
            "/v1/health", headers={"x-forwarded-for": "198.51.100.2, 203.0.113.9, 192.0.2.1"}
        )
        other_client = await client.get(
            "/v1/health", headers={"x-forwarded-for": "198.51.100.1, 203.0.113.8, 192.0.2.1"}
        )

    assert first.status_code == 200
    assert same_client.status_code == 429
    assert other_client.status_code == 200


async def test_the_documented_production_deployment_boots(monkeypatch: pytest.MonkeyPatch) -> None:
    """infra/deploy.md's prod command must survive Settings validation.

    The validator refuses ENV=prod with a positive limit and no trusted proxy
    topology. If the runbook does not disable the limit, the documented
    revision never becomes ready -- a validator that takes production down is
    worse than the bypass it was added to prevent.
    """
    import re

    runbook = (Path(__file__).resolve().parent.parent / "infra" / "deploy.md").read_text()
    block = runbook.split("ENV=prod,", 1)[1].split("```", 1)[0]
    env = dict(
        pair.split("=", 1)
        for pair in (line.strip().rstrip(",\\") for line in re.split(r",\\\n", "ENV=prod," + block))
        if "=" in pair and not pair.startswith("--")
    )

    assert env.get("FREE_RATE_LIMIT_PER_MINUTE") == "0" or env.get("TRUSTED_PROXY_HOPS", "0") != "0"
    Settings(
        env="prod",
        free_rate_limit_per_minute=int(env.get("FREE_RATE_LIMIT_PER_MINUTE", 120)),
        trusted_proxy_hops=int(env.get("TRUSTED_PROXY_HOPS", 0)),
    )


async def test_rate_limit_retry_after_covers_the_remaining_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(monkeypatch, FREE_RATE_LIMIT_PER_MINUTE="1")
    await seed_store(store)
    now = [100.0]
    monkeypatch.setattr("api.core.ratelimit.time.monotonic", lambda: now[0])
    async with api_client() as client:
        assert (await client.get("/v1/health")).status_code == 200
        now[0] += 0.25
        limited = await client.get("/v1/health")
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "60"
        now[0] += int(limited.headers["retry-after"])
        assert (await client.get("/v1/health")).status_code == 200


def test_rate_limit_only_prunes_expired_entries_when_full(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.core.ratelimit import _make_room

    monkeypatch.setattr("api.core.ratelimit._MAX_TRACKED", 3)
    _HITS.update(expired=(0.0, 1), active=(99.0, 1))
    _make_room(100.0)
    assert set(_HITS) == {"expired", "active"}
    _HITS["new"] = (100.0, 1)
    _make_room(100.0)
    assert set(_HITS) == {"active", "new"}
