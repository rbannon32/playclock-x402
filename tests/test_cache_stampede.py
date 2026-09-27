"""One generation per cold cache key, not one per caller.

Ten callers arriving on an expired board start ten identical generations
without this. The measured cold times make it concrete: `sleepers` takes 374s
and `report` 988s, both past the 300s Cloud Run request timeout — so all ten
pay, all ten time out, and Vertex is billed ten times for one answer. Warming
makes the window rare; advertising is what makes rare happen.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from api.agents.engine import AnalysisEngine, EngineError, set_engine
from api.core.store import Store
from api.data.cache import CACHE_COLLECTION
from api.evals.golden import SEASON, WEEK, seed_store
from api.routes.paid import clear_inflight
from api.schemas import AnalysisResponse
from api.x402 import RECEIPTS_COLLECTION, clear_idempotency_cache, set_facilitator
from api.x402.schemas_compat import PAYMENT_SIGNATURE_HEADER
from tests.test_routes_free import api_client, configure


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    clear_inflight()
    clear_idempotency_cache()
    set_facilitator(None)
    yield
    clear_inflight()
    clear_idempotency_cache()
    set_facilitator(None)
    set_engine(None)


def paid(nonce: str) -> dict[str, str]:
    import base64
    import json

    body = {"x402Version": 2, "payload": {"mock": True, "nonce": nonce}}
    return {PAYMENT_SIGNATURE_HEADER: base64.b64encode(json.dumps(body).encode()).decode()}


class SlowEngine(AnalysisEngine):
    """Counts generations and holds them open until released."""

    name = "slow"

    def __init__(self, inner: AnalysisEngine) -> None:
        self.inner = inner
        self.calls = 0
        self.gate = asyncio.Event()

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        self.calls += 1
        await self.gate.wait()
        return await self.inner.analyze(endpoint_key, request_context)


class FailingEngine(AnalysisEngine):
    name = "failing"

    def __init__(self) -> None:
        self.calls = 0

    async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> AnalysisResponse:
        self.calls += 1
        await asyncio.sleep(0)
        raise EngineError("boom")


@pytest.fixture
async def seeded(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    configure(monkeypatch, X402_MODE="mock", SEASON=str(SEASON), WEEK_OVERRIDE=str(WEEK))
    await seed_store(store)
    return store


async def test_concurrent_callers_share_one_generation(seeded: Store) -> None:
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.core.config import get_settings

    slow = SlowEngine(DeterministicAnalysisEngine(store=seeded, settings=get_settings()))
    set_engine(slow)

    async with api_client() as client:
        calls = [client.get("/v1/trending", headers=paid(f"n{i}")) for i in range(5)]
        task = asyncio.gather(*calls)
        await asyncio.sleep(0.05)  # let all five reach the generator
        slow.gate.set()
        responses = await task

    assert all(r.status_code == 200 for r in responses)
    assert slow.calls == 1, f"{slow.calls} generations for one cold key"
    # Everyone still paid — they each asked a real question and got an answer.
    assert len(await seeded.list(RECEIPTS_COLLECTION)) == 5


async def test_each_caller_gets_its_own_body(seeded: Store) -> None:
    """Sharing one model instance would let one caller mutate another's."""
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.core.config import get_settings

    slow = SlowEngine(DeterministicAnalysisEngine(store=seeded, settings=get_settings()))
    set_engine(slow)

    async with api_client() as client:
        task = asyncio.gather(
            client.get("/v1/trending", headers=paid("a")),
            client.get("/v1/trending", headers=paid("b")),
        )
        await asyncio.sleep(0.05)
        slow.gate.set()
        first, second = await task

    assert first.json()["verdict"] == second.json()["verdict"]
    assert first.json()["meta"]["cache"] == "fresh"
    assert second.json()["meta"]["cache"] == "fresh"


async def test_one_body_is_written_to_the_cache(seeded: Store) -> None:
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.core.config import get_settings

    slow = SlowEngine(DeterministicAnalysisEngine(store=seeded, settings=get_settings()))
    set_engine(slow)

    async with api_client() as client:
        task = asyncio.gather(
            *[client.get("/v1/trending", headers=paid(f"c{i}")) for i in range(3)]
        )
        await asyncio.sleep(0.05)
        slow.gate.set()
        await task

    entries = [d for d in await seeded.list(CACHE_COLLECTION) if d["_id"].startswith("trending:")]
    assert len(entries) == 1


async def test_a_failed_generation_fails_every_waiter(seeded: Store) -> None:
    """Waiters must fail like the leader, not hang until the request times out."""
    failing = FailingEngine()
    set_engine(failing)

    async with api_client() as client:
        responses = await asyncio.gather(
            *[client.get("/v1/trending", headers=paid(f"f{i}")) for i in range(3)]
        )

    assert all(r.status_code == 500 for r in responses)
    assert failing.calls == 1
    # Non-2xx never settles, so nobody was charged for the failure.
    assert await seeded.list(RECEIPTS_COLLECTION) == []


async def test_the_key_is_released_after_a_failure(seeded: Store) -> None:
    """A poisoned key would make the endpoint permanently dead."""
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.core.config import get_settings

    set_engine(FailingEngine())
    async with api_client() as client:
        assert (await client.get("/v1/trending", headers=paid("x"))).status_code == 500

    set_engine(DeterministicAnalysisEngine(store=seeded, settings=get_settings()))
    async with api_client() as client:
        assert (await client.get("/v1/trending", headers=paid("y"))).status_code == 200


# -- the single-flight itself, below the HTTP layer --------------------------


async def _call(seeded: Store, key: str = "trending:w-test") -> dict[str, Any]:
    from api.core.config import get_settings
    from api.data.cache import ResponseCache
    from api.routes.paid import _generate_once

    context = {"season": get_settings().season, "week": WEEK}
    return await _generate_once(key, "trending", context, ResponseCache(seeded), WEEK)


async def test_a_cancelled_leader_does_not_fail_its_followers(seeded: Store) -> None:
    """A client disconnect cancels one request, not the generation others wait on."""
    from api.agents.deterministic import DeterministicAnalysisEngine
    from api.core.config import get_settings
    from api.routes import paid as paid_module

    slow = SlowEngine(DeterministicAnalysisEngine(store=seeded, settings=get_settings()))
    set_engine(slow)

    leader = asyncio.create_task(_call(seeded))
    await asyncio.sleep(0.01)
    follower = asyncio.create_task(_call(seeded))
    await asyncio.sleep(0.01)

    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    slow.gate.set()
    payload = await follower

    assert payload["verdict"]
    assert slow.calls == 1
    # The generation finished despite the leader leaving, cached, and let go.
    assert [d for d in await seeded.list(CACHE_COLLECTION) if d["_id"] == "trending:w-test"]
    assert "trending:w-test" not in paid_module._INFLIGHT


async def test_a_generation_everyone_abandoned_still_finishes_quietly(
    seeded: Store,
) -> None:
    """With every caller gone, the failure is retrieved: no 'never retrieved' warning."""
    from api.routes import paid as paid_module

    gate = asyncio.Event()

    class GatedFailure(AnalysisEngine):
        name = "gated-failure"

        async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> Any:
            await gate.wait()
            raise EngineError("boom")

    set_engine(GatedFailure())
    only = asyncio.create_task(_call(seeded))
    await asyncio.sleep(0.01)
    generation = paid_module._INFLIGHT["trending:w-test"]
    only.cancel()
    with pytest.raises(asyncio.CancelledError):
        await only

    unretrieved: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unretrieved.append(context))
    try:
        gate.set()
        await asyncio.wait([generation])
        await asyncio.sleep(0)
        assert "trending:w-test" not in paid_module._INFLIGHT
        del generation
        import gc

        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)
    assert unretrieved == []


async def test_an_exception_reaches_every_waiter(seeded: Store) -> None:
    from api.routes import paid as paid_module

    gate = asyncio.Event()
    calls = {"n": 0}

    class GatedFailure(AnalysisEngine):
        name = "gated-failure"

        async def analyze(self, endpoint_key: str, request_context: dict[str, Any]) -> Any:
            calls["n"] += 1
            await gate.wait()
            raise EngineError("boom")

    set_engine(GatedFailure())
    waiters = [asyncio.create_task(_call(seeded)) for _ in range(3)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert calls["n"] == 1
    assert len(results) == 3
    from fastapi import HTTPException

    assert all(isinstance(r, HTTPException) and r.status_code == 500 for r in results), results
    assert "trending:w-test" not in paid_module._INFLIGHT
