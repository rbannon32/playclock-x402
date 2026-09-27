"""End-to-end payment gating tests against a throwaway FastAPI app.

Each test mounts one fake paid route on a :func:`~api.x402.paid_router` router
and drives it over ``httpx.ASGITransport`` — no server, no network, no chain.
The facilitator is a :class:`~api.x402.MockFacilitatorClient` installed through
``set_facilitator()``, and persistence is the ``MemoryStore`` from ``conftest``.

What is being pinned here is the ordering contract of DESIGN_NOTES §2:
**verify -> handler -> settle**, with settlement strictly conditional on a 2xx.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from api.core.config import Settings
from api.core.store import MemoryStore, Store, set_store
from api.x402 import (
    MockFacilitatorClient,
    PaymentContext,
    clear_idempotency_cache,
    install_x402_handlers,
    paid_router,
    require_payment,
    set_facilitator,
)
from api.x402.facilitator import FACILITATOR_UNAVAILABLE
from api.x402.middleware import (
    MAX_REPLAYS,
    PAYMENT_IDEMPOTENCY_COLLECTION,
    REPLAY_LIMIT_REACHED,
)
from api.x402.receipts import FAILED_PAID_CALLS_COLLECTION, RECEIPTS_COLLECTION
from api.x402.schemas_compat import (
    MOCK_PAYMENT_HEADER,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
    VerifyResult,
    build_payment_requirements,
    decode_settlement_header,
    safe_base64_decode,
)
from tests.test_x402_schemas import make_settings

Handler = Callable[[PaymentContext], Any]


@pytest.fixture(autouse=True)
def _reset_payment_state() -> Iterator[None]:
    """Payment caches and facilitator overrides never leak between tests."""
    clear_idempotency_cache()
    set_facilitator(None)
    yield
    clear_idempotency_cache()
    set_facilitator(None)


def build_app(
    settings: Settings,
    *,
    endpoint_key: str = "trending",
    method: str = "GET",
    path: str = "/v1/trending",
    handler: Handler | None = None,
) -> FastAPI:
    """Mount one fake paid route the way wave 3's routers are expected to."""
    router = paid_router()
    dependency = require_payment(endpoint_key, settings=settings)

    async def route(payment: PaymentContext = Depends(dependency)) -> Any:
        if handler is not None:
            return handler(payment)
        return {
            "ok": True,
            "paid": payment.paid,
            "mode": payment.mode,
            "payer": payment.payer,
            "endpoint": payment.endpoint_key,
        }

    router.add_api_route(path, route, methods=[method])
    app = FastAPI()
    install_x402_handlers(app)
    app.include_router(router)
    return app


def build_echo_app(
    settings: Settings, *, endpoint_key: str = "player", path: str = "/v1/player"
) -> tuple[FastAPI, list[dict[str, Any]]]:
    """A paid POST route recording every body its handler actually saw.

    The list is the assertion that matters for replay protection: a rejected
    replay must never reach the handler, so it must never append.
    """
    seen: list[dict[str, Any]] = []
    router = paid_router()
    dependency = require_payment(endpoint_key, settings=settings)

    async def route(body: dict[str, Any], payment: PaymentContext = Depends(dependency)) -> Any:
        seen.append(dict(body))
        return {"ok": True, "echo": body}

    router.add_api_route(path, route, methods=["POST"])
    app = FastAPI()
    install_x402_handlers(app)
    app.include_router(router)
    return app, seen


def client_for(app: FastAPI) -> httpx.AsyncClient:
    """An ASGI-bound client; ``testserver`` keeps live-mode guards honest."""
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def mock_header(
    settings: Settings, endpoint_key: str = "trending", *, marked: bool = True, nonce: str = ""
) -> str:
    """A base64 payment header for ``endpoint_key``; ``marked=False`` fails verify."""
    requirements = build_payment_requirements(endpoint_key, settings)
    # A distinct payment is a distinct signed transaction: the idempotency key.
    signed = base64.b64encode(f"signed:{endpoint_key}:{nonce}".encode()).decode()
    payload: dict[str, Any] = {"paymentGroup": [signed], "paymentIndex": 0}
    if marked:
        payload["mock"] = True
    if nonce:
        payload["nonce"] = nonce
    body = {
        "x402Version": 2,
        "payload": payload,
        "accepted": requirements.model_dump(by_alias=True, exclude_none=True),
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


async def receipts(store: Store) -> list[dict[str, Any]]:
    return await store.list(RECEIPTS_COLLECTION)


async def failed_calls(store: Store) -> list[dict[str, Any]]:
    return await store.list(FAILED_PAID_CALLS_COLLECTION)


# --------------------------------------------------------------------------
# disabled mode
# --------------------------------------------------------------------------


async def test_disabled_mode_passes_straight_through(store: Store) -> None:
    app = build_app(make_settings(x402_mode="disabled"))

    async with client_for(app) as client:
        response = await client.get("/v1/trending")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "paid": False,
        "mode": "disabled",
        "payer": None,
        "endpoint": "trending",
    }
    assert PAYMENT_RESPONSE_HEADER not in response.headers
    assert await receipts(store) == []


# --------------------------------------------------------------------------
# 402 challenge
# --------------------------------------------------------------------------


async def test_missing_payment_returns_the_v2_402_body(store: Store) -> None:
    app = build_app(make_settings(x402_mode="mock"))

    async with client_for(app) as client:
        response = await client.get("/v1/trending")

    assert response.status_code == 402
    body = response.json()

    # The x402 payload sits at the root, not under FastAPI's {"detail": ...}.
    assert "detail" not in body
    assert body["x402Version"] == 2
    assert body["error"] == "payment_required"
    assert body["resource"]["url"] == "http://testserver/v1/trending"
    assert body["resource"]["mimeType"] == "application/json"

    accepts = body["accepts"][0]
    assert accepts["scheme"] == "exact"
    assert accepts["amount"] == "100000"
    assert accepts["asset"] == "10458941"
    assert accepts["network"].startswith("algorand:")
    assert accepts["payTo"] == "PAYTOADDRESS"
    assert accepts["maxTimeoutSeconds"] == 120
    assert accepts["extra"]["tag"] == "x402-global-challenge"
    assert body["extensions"]["bazaar"]["info"]["input"]["type"] == "http"

    # V2 headers accompany the body, and are CORS-exposed for browser wallets.
    assert PAYMENT_REQUIRED_HEADER in response.headers
    assert PAYMENT_RESPONSE_HEADER in response.headers["access-control-expose-headers"]
    assert await receipts(store) == []


async def test_402_prices_each_endpoint_from_the_settings_table(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    app = build_app(settings, endpoint_key="team_report", method="POST", path="/v1/team-report")

    async with client_for(app) as client:
        response = await client.post("/v1/team-report", json={})

    body = response.json()
    assert response.status_code == 402
    # Derived, not pinned: `test_config` is the single place the price table
    # is asserted. What matters here is that the 402 quotes *that* table.
    assert body["accepts"][0]["amount"] == str(round(settings.price_for("team_report") * 1e6))
    assert body["extensions"]["bazaar"]["info"]["input"]["bodyType"] == "json"


@pytest.mark.parametrize(
    ("endpoint_key", "method", "path"),
    [("trending", "GET", "/v1/trending"), ("player", "POST", "/v1/player")],
)
async def test_402_advertises_the_http_method_for_the_bazaar(
    store: Store, endpoint_key: str, method: str, path: str
) -> None:
    """The Bazaar keys a resource on method + URL, so the 402 must carry both.

    Regression, 2026-09-01: Play Clock settled a challenge-tagged MainNet
    payment and never appeared in the Bazaar catalogue. All 1,792 resources
    listed there carried a `method` and a resource id of
    base64("GET:https://host/path"); ours carried url, description and mimeType
    but no method. `description` and `mimeType` were the optional ones.
    """
    settings = make_settings(x402_mode="mock")
    app = build_app(settings, endpoint_key=endpoint_key, method=method, path=path)

    async with client_for(app) as client:
        response = await client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 402
    body = response.json()
    assert body["resource"]["method"] == method
    # The header is what a client ignoring the body signs against; it must not
    # be able to disagree with the body about what was offered.
    header = json.loads(safe_base64_decode(response.headers[PAYMENT_REQUIRED_HEADER]))
    assert header["resource"]["method"] == method
    assert header["resource"]["url"] == body["resource"]["url"]


async def test_live_mode_refuses_to_advertise_localhost(store: Store) -> None:
    """The facilitator catalogs resource.url permanently — never point it home."""
    app = build_app(make_settings(x402_mode="live", x402_pay_to="PAYTOADDRESS"))

    with pytest.raises(Exception, match="refusing to advertise"):
        async with client_for(app) as client:
            await client.get("/v1/trending")


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


async def test_valid_mock_payment_settles_and_logs_a_receipt(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    assert response.json()["paid"] is True

    receipt_header = response.headers[PAYMENT_RESPONSE_HEADER]
    settle = decode_settlement_header(receipt_header)
    assert settle.success is True
    assert len(settle.transaction) == 52
    # The V1 legacy name mirrors the V2 receipt for clients that have not moved.
    assert response.headers[X_PAYMENT_RESPONSE_HEADER] == receipt_header
    assert PAYMENT_RESPONSE_HEADER in response.headers["access-control-expose-headers"]

    logged = await receipts(store)
    assert len(logged) == 1
    assert logged[0]["endpoint"] == "trending"
    assert logged[0]["amount_usdc"] == 0.10
    assert logged[0]["txid"] == settle.transaction
    assert logged[0]["network"].startswith("algorand:")
    assert logged[0]["payer"] == settle.payer
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1


async def test_magic_mock_header_is_accepted(store: Store) -> None:
    app = build_app(make_settings(x402_mode="mock"))

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}
        )

    assert response.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in response.headers
    assert len(await receipts(store)) == 1


async def test_legacy_x_payment_header_is_accepted(store: Store) -> None:
    """Some clients still send the V1 header name; we take it, and answer in V2."""
    settings = make_settings(x402_mode="mock")
    app = build_app(settings)

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={X_PAYMENT_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in response.headers
    assert len(await receipts(store)) == 1


# --------------------------------------------------------------------------
# failure paths
# --------------------------------------------------------------------------


async def test_invalid_payment_402s_and_settles_nothing(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending",
            headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings, marked=False)},
        )

    assert response.status_code == 402
    assert response.json()["error"] == "mock_marker_missing"
    assert len(facilitator.verify_calls) == 1
    assert facilitator.settle_calls == []
    assert await receipts(store) == []


async def test_undecodable_payment_header_402s(store: Store) -> None:
    app = build_app(make_settings(x402_mode="mock"))

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: "not a payment"}
        )

    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_payload"
    assert await receipts(store) == []


async def test_handler_failure_verifies_but_never_settles(store: Store) -> None:
    """Verify -> handler -> settle: a broken handler must not charge anyone."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)

    def explode(_payment: PaymentContext) -> Any:
        raise RuntimeError("synthesis agent returned malformed JSON")

    app = build_app(settings, handler=explode)

    with pytest.raises(RuntimeError, match="malformed JSON"):
        async with client_for(app) as client:
            await client.get(
                "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
            )

    assert len(facilitator.verify_calls) == 1
    assert facilitator.settle_calls == []
    assert await receipts(store) == []
    assert await failed_calls(store) == []


async def test_non_2xx_handler_response_does_not_settle(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)

    def conflict(_payment: PaymentContext) -> Any:
        return JSONResponse({"error": "player not found"}, status_code=404)

    app = build_app(settings, handler=conflict)

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 404
    assert PAYMENT_RESPONSE_HEADER not in response.headers
    assert facilitator.settle_calls == []
    assert await receipts(store) == []


async def test_settle_failure_still_returns_the_answer_and_logs_the_loss(store: Store) -> None:
    """We ate one LLM call; the caller keeps the analysis and is not charged."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient(settle_error="insufficient_funds"))
    app = build_app(settings)

    async with client_for(app) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True

    settle = decode_settlement_header(response.headers[PAYMENT_RESPONSE_HEADER])
    assert settle.success is False

    assert await receipts(store) == []
    failures = await failed_calls(store)
    assert len(failures) == 1
    assert failures[0]["endpoint"] == "trending"
    assert "insufficient_funds" in failures[0]["error"]
    assert len(failures[0]["payment_hash"]) == 64


async def test_a_failed_settle_does_not_free_the_payment_for_another_request(
    store: Store,
) -> None:
    """The first answer was served; the same payment must not buy a second one."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient(settle_error="facilitator_down"))
    header = mock_header(settings)

    async with client_for(build_app(settings)) as client:
        first = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})
        other = await client.get(
            "/v1/trending", params={"limit": 3}, headers={PAYMENT_SIGNATURE_HEADER: header}
        )
        same = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})

    assert first.status_code == 200
    assert other.status_code == 402
    assert other.json()["error"] == "payment_already_used_for_a_different_request"
    assert same.status_code == 200, "the same request may still try to settle"


async def test_an_abandoned_retry_restores_the_failed_settle_binding(store: Store) -> None:
    """A retry of request A that fails must not unbind the payment from A.

    The retry's claim replaces A's ``failed`` record; releasing that claim by
    deleting it would let the same payment verify afresh and buy request B.
    """
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient(settle_error="facilitator_down"))
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
    calls: list[int] = []

    def flaky(_payment: PaymentContext) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise HTTPException(status_code=503, detail="data missing")
        return {"ok": True}

    async with client_for(build_app(settings, handler=flaky)) as client:
        first = await client.get("/v1/trending", headers=header)
        retry = await client.get("/v1/trending", headers=header)
        other = await client.get("/v1/trending", params={"limit": 3}, headers=header)

    assert first.status_code == 200
    assert retry.status_code == 503
    assert other.status_code == 402
    assert other.json()["error"] == "payment_already_used_for_a_different_request"
    assert len(calls) == 2, "request B never reached the handler"


async def test_a_facilitator_outage_during_verify_is_a_503_not_a_402(store: Store) -> None:
    """A 402 tells a generic x402 client to pay again; nothing was charged."""
    settings = make_settings(x402_mode="mock")

    class Down(MockFacilitatorClient):
        async def verify(self, payment_payload: Any, requirements: Any) -> VerifyResult:
            self.verify_calls.append(payment_payload)
            return VerifyResult(is_valid=False, invalid_reason=FACILITATOR_UNAVAILABLE)

    down = Down()
    set_facilitator(down)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}

    async with client_for(build_app(settings)) as client:
        outage = await client.get("/v1/trending", headers=header)
        set_facilitator(MockFacilitatorClient())
        recovered = await client.get("/v1/trending", headers=header)

    assert outage.status_code == 503
    assert outage.headers["Retry-After"].isdigit()
    assert "not charged" in outage.json()["detail"]
    assert PAYMENT_REQUIRED_HEADER not in outage.headers
    assert down.settle_calls == []
    assert recovered.status_code == 200, "the same header works once verify is back"
    assert len(await receipts(store)) == 1


# --------------------------------------------------------------------------
# idempotency
# --------------------------------------------------------------------------


async def test_replayed_payment_settles_once_and_reuses_the_receipt(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}

    async with client_for(app) as client:
        first = await client.get("/v1/trending", headers=header)
        second = await client.get("/v1/trending", headers=header)

    assert first.status_code == second.status_code == 200
    assert first.headers[PAYMENT_RESPONSE_HEADER] == second.headers[PAYMENT_RESPONSE_HEADER]
    # Verified once, settled once, one receipt — the retry rode the cache.
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_a_failed_replay_never_claims_the_payer_was_not_charged(store: Store) -> None:
    """The replayed payment settled; telling the client otherwise loses the answer."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())
    calls: list[int] = []

    def handler(payment: PaymentContext) -> Any:
        calls.append(1)
        if len(calls) > 1:
            raise HTTPException(status_code=503, detail="Data is stale. You were not charged.")
        return {"ok": True}

    app = build_app(settings, handler=handler)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
    async with client_for(app) as client:
        first = await client.get("/v1/trending", headers=header)
        second = await client.get("/v1/trending", headers=header)

    assert first.status_code == 200
    assert second.status_code == 503
    assert "not charged" not in second.json()["detail"]
    assert "already settled" in second.json()["detail"]


async def test_a_reordered_repeated_query_key_is_a_different_request(store: Store) -> None:
    """FastAPI reads the last value, so ?week=3&week=4 and ?week=4&week=3 differ."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
    async with client_for(build_app(settings)) as client:
        first = await client.get("/v1/trending?week=3&week=4", headers=header)
        second = await client.get("/v1/trending?week=4&week=3", headers=header)

    assert first.status_code == 200
    assert second.status_code == 402
    assert second.json()["error"] == "payment_already_used_for_a_different_request"


async def test_concurrent_replay_cannot_run_two_handlers_or_settle_twice(store: Store) -> None:
    """The durable claim closes the race across workers, not just sequential retries."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    entered = asyncio.Event()
    release = asyncio.Event()
    handler_calls = 0

    router = paid_router()
    dependency = require_payment("trending", settings=settings)

    async def route(payment: PaymentContext = Depends(dependency)) -> Any:
        nonlocal handler_calls
        handler_calls += 1
        entered.set()
        await release.wait()
        return {"paid": payment.paid}

    router.add_api_route("/v1/trending", route, methods=["GET"])
    app = FastAPI()
    install_x402_handlers(app)
    app.include_router(router)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}

    async with client_for(app) as client:
        first_task = asyncio.create_task(client.get("/v1/trending", headers=header))
        await entered.wait()
        concurrent = await client.get("/v1/trending", headers=header)
        release.set()
        first = await first_task

    assert first.status_code == 200
    assert concurrent.status_code == 402
    assert concurrent.json()["error"] == "payment_in_progress_retry_shortly"
    assert handler_calls == 1
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_replayed_payment_on_the_same_body_is_idempotent(store: Store) -> None:
    """An agent retrying the *same* POST rides the cache: one verify, one settle."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app, seen = build_echo_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings, "player")}

    async with client_for(app) as client:
        first = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=header)
        second = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=header)

    assert first.status_code == second.status_code == 200
    assert first.headers[PAYMENT_RESPONSE_HEADER] == second.headers[PAYMENT_RESPONSE_HEADER]
    assert len(seen) == 2
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_replayed_payment_on_a_different_body_is_rejected(store: Store) -> None:
    """One $0.15 payment buys one analysis, not a 60-second all-you-can-eat window."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app, seen = build_echo_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings, "player")}

    async with client_for(app) as client:
        paid = await client.post("/v1/player", json={"name": "Bijan Robinson"}, headers=header)
        replay = await client.post("/v1/player", json={"name": "Breece Hall"}, headers=header)

    assert paid.status_code == 200
    assert replay.status_code == 402
    assert replay.json()["error"] == "payment_already_used_for_a_different_request"
    # The handler never ran for the replay, and nothing settled a second time.
    assert seen == [{"name": "Bijan Robinson"}]
    assert PAYMENT_RESPONSE_HEADER not in replay.headers
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_replayed_payment_on_a_different_query_is_rejected(store: Store) -> None:
    """GET has no body, so the fingerprint is the path plus the query string."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}

    async with client_for(app) as client:
        paid = await client.get("/v1/trending?limit=3", headers=header)
        same = await client.get("/v1/trending?limit=3", headers=header)
        replay = await client.get("/v1/trending?limit=50", headers=header)

    assert paid.status_code == same.status_code == 200
    assert replay.status_code == 402
    assert replay.json()["error"] == "payment_already_used_for_a_different_request"
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_query_parameter_order_is_not_a_different_request(store: Store) -> None:
    """Fingerprints are order-insensitive: ``?a=1&b=2`` is ``?b=2&a=1``."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}

    async with client_for(app) as client:
        first = await client.get("/v1/trending?limit=3&lookback_hours=24", headers=header)
        second = await client.get("/v1/trending?lookback_hours=24&limit=3", headers=header)

    assert first.status_code == second.status_code == 200
    assert len(facilitator.settle_calls) == 1


async def test_re_serialized_json_is_not_a_different_request(store: Store) -> None:
    """Key order and whitespace change the bytes, not the question."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app, seen = build_echo_app(settings)
    header = {
        PAYMENT_SIGNATURE_HEADER: mock_header(settings, "player"),
        "content-type": "application/json",
    }

    async with client_for(app) as client:
        first = await client.post(
            "/v1/player", content='{"name":"Bijan Robinson","week":5}', headers=header
        )
        retry = await client.post(
            "/v1/player", content='{\n  "week": 5,\n  "name": "Bijan Robinson"\n}', headers=header
        )
        other = await client.post(
            "/v1/player", content='{"week":6,"name":"Bijan Robinson"}', headers=header
        )

    assert first.status_code == retry.status_code == 200
    assert other.status_code == 402
    assert len(seen) == 2
    assert len(facilitator.settle_calls) == 1


async def test_distinct_payments_settle_independently(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    app = build_app(settings)

    async with client_for(app) as client:
        await client.get(
            "/v1/trending",
            headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings, nonce="a")},
        )
        await client.get(
            "/v1/trending",
            headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings, nonce="b")},
        )

    assert len(facilitator.settle_calls) == 2
    assert len(await receipts(store)) == 2


async def test_idempotency_is_scoped_to_the_endpoint(store: Store) -> None:
    """A cheap payment must not ride the cache onto an expensive endpoint."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = mock_header(settings, "trending")

    cheap = build_app(settings)
    async with client_for(cheap) as client:
        assert (
            await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})
        ).status_code == 200

    expensive = build_app(
        settings, endpoint_key="team_report", method="POST", path="/v1/team-report"
    )
    async with client_for(expensive) as client:
        response = await client.post(
            "/v1/team-report", json={}, headers={PAYMENT_SIGNATURE_HEADER: header}
        )

    assert response.status_code == 402
    assert response.json()["error"] == "payment_already_used_for_a_different_request"
    assert len(await receipts(store)) == 1


async def test_a_cheap_payment_never_verifies_on_an_expensive_endpoint(store: Store) -> None:
    """A fresh trending payment sent straight to team-report fails verify on amount."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())
    expensive = build_app(
        settings, endpoint_key="team_report", method="POST", path="/v1/team-report"
    )
    async with client_for(expensive) as client:
        response = await client.post(
            "/v1/team-report",
            json={},
            headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings, "trending")},
        )

    assert response.status_code == 402
    assert response.json()["error"] == "amount_mismatch"


async def test_one_payment_buys_one_answer_across_same_priced_endpoints(store: Store) -> None:
    """trending and player cost the same; one signed payment must not buy both."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = mock_header(settings, "trending")

    async with client_for(build_app(settings)) as client:
        first = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})
    other = build_app(settings, endpoint_key="player", method="POST", path="/v1/player")
    async with client_for(other) as client:
        second = await client.post(
            "/v1/player", json={}, headers={PAYMENT_SIGNATURE_HEADER: header}
        )

    assert first.status_code == 200
    assert second.status_code == 402
    assert second.json()["error"] == "payment_already_used_for_a_different_request"
    assert len(facilitator.verify_calls) == 1


async def test_a_re_encoded_header_is_the_same_payment(store: Store) -> None:
    """URL-safe, unpadded or raw-JSON re-encodings replay; they never verify again."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = mock_header(settings)
    raw_json = base64.b64decode(header).decode()
    variants = [header, base64.urlsafe_b64encode(raw_json.encode()).decode().rstrip("="), raw_json]

    async with client_for(build_app(settings)) as client:
        responses = [
            await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: variant})
            for variant in variants
        ]

    assert [r.status_code for r in responses] == [200, 200, 200]
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1


async def test_replays_of_one_payment_are_capped(store: Store) -> None:
    """A replay re-runs the handler; one payment must not buy unbounded generations."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    calls: list[int] = []

    def handler(_payment: PaymentContext) -> Any:
        calls.append(1)
        return {"ok": True}

    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
    async with client_for(build_app(settings, handler=handler)) as client:
        served = [await client.get("/v1/trending", headers=header) for _ in range(MAX_REPLAYS + 1)]
        refused = await client.get("/v1/trending", headers=header)

    assert [r.status_code for r in served] == [200] * (MAX_REPLAYS + 1)
    assert refused.status_code == 402
    assert refused.json()["error"] == REPLAY_LIMIT_REACHED
    # The payment settled on its first use: the refusal must not say otherwise.
    assert "not charged" not in refused.text.lower()
    assert len(calls) == MAX_REPLAYS + 1
    assert len(facilitator.verify_calls) == 1
    assert len(facilitator.settle_calls) == 1
    assert len(await receipts(store)) == 1


async def test_concurrent_replays_cannot_outrun_the_cap(store: Store) -> None:
    """Fifty racing replays read the same count; the compare-and-swap keeps it honest."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())
    calls: list[int] = []

    def handler(_payment: PaymentContext) -> Any:
        calls.append(1)
        return {"ok": True}

    header = {PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
    async with client_for(build_app(settings, handler=handler)) as client:
        first = await client.get("/v1/trending", headers=header)
        replays = await asyncio.gather(
            *(client.get("/v1/trending", headers=header) for _ in range(50))
        )

    assert first.status_code == 200
    served = [r for r in replays if r.status_code == 200]
    refused = [r for r in replays if r.status_code != 200]
    assert len(served) <= MAX_REPLAYS
    assert len(calls) == 1 + len(served)
    assert {r.status_code for r in refused} == {402}
    assert {r.json()["error"] for r in refused} <= {
        REPLAY_LIMIT_REACHED,
        "payment_in_progress_retry_shortly",
    }
    [record] = await store.list(PAYMENT_IDEMPOTENCY_COLLECTION)
    assert record["replays"] == len(served)


async def test_the_literal_mock_marker_is_bound_to_the_request(store: Store) -> None:
    """Every mock caller sends ``mock-paid``: one question must not lock out the next."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())
    app, seen = build_echo_app(settings)
    header = {PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}

    async with client_for(app) as client:
        first = await client.post("/v1/player", json={"name": "A"}, headers=header)
        second = await client.post("/v1/player", json={"name": "B"}, headers=header)
        # The same question again is still a retry, and is never capped: there
        # is no payment behind the shared marker to amplify.
        repeats = [
            await client.post("/v1/player", json={"name": "A"}, headers=header)
            for _ in range(MAX_REPLAYS + 1)
        ]

    assert first.status_code == second.status_code == 200
    assert [r.status_code for r in repeats] == [200] * (MAX_REPLAYS + 1)
    assert seen[:2] == [{"name": "A"}, {"name": "B"}]
    assert len(await receipts(store)) == 2


async def test_settled_records_carry_a_firestore_ttl_timestamp(store: Store) -> None:
    """``expires_at_ts`` is what a Firestore TTL policy deletes on (infra/deploy.md)."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())

    async with client_for(build_app(settings)) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    [record] = await store.list(PAYMENT_IDEMPOTENCY_COLLECTION)
    assert record["status"] == "settled"
    stamp = record["expires_at_ts"]
    assert isinstance(stamp, datetime)
    assert stamp.tzinfo is not None
    assert stamp.timestamp() == pytest.approx(record["expires_at"])


class _IdempotencyWriteFails(MemoryStore):
    """A store whose idempotency compare-and-swap raises, as Firestore can."""

    async def replace_if_revision(
        self,
        collection: str,
        doc_id: str,
        expected_revision: str,
        data: dict[str, Any] | None,
    ) -> bool:
        if collection == PAYMENT_IDEMPOTENCY_COLLECTION:
            raise RuntimeError("firestore unavailable")
        return await super().replace_if_revision(collection, doc_id, expected_revision, data)


@pytest.fixture
def flaky_store() -> Iterator[MemoryStore]:
    flaky = _IdempotencyWriteFails()
    set_store(flaky)
    yield flaky
    set_store(None)


async def test_a_failed_idempotency_write_still_logs_the_receipt(
    flaky_store: MemoryStore,
) -> None:
    """The receipt is the record of money moved; it must not share the swap's fate."""
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient())

    async with client_for(build_app(settings)) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    assert PAYMENT_RESPONSE_HEADER in response.headers
    assert len(await receipts(flaky_store)) == 1


async def test_a_failed_idempotency_write_still_logs_the_failed_settle(
    flaky_store: MemoryStore,
) -> None:
    settings = make_settings(x402_mode="mock")
    set_facilitator(MockFacilitatorClient(settle_error="insufficient_funds"))

    async with client_for(build_app(settings)) as client:
        response = await client.get(
            "/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: mock_header(settings)}
        )

    assert response.status_code == 200
    failures = await failed_calls(flaky_store)
    assert len(failures) == 1
    assert "insufficient_funds" in failures[0]["error"]


async def test_the_settled_payload_carries_our_resource_not_the_clients(store: Store) -> None:
    """The facilitator catalogues payload.resource; a client must not choose it."""
    settings = make_settings(x402_mode="mock")
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    body = json.loads(base64.b64decode(mock_header(settings)))
    body["resource"] = {"url": "https://phish.example/v1/trending"}
    body["extensions"] = {"bazaar": {"info": {}}}
    forged = base64.b64encode(json.dumps(body).encode()).decode()

    async with client_for(build_app(settings)) as client:
        response = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: forged})

    assert response.status_code == 200
    settled = facilitator.settle_calls[0]
    assert "phish.example" not in settled.resource.url
    assert settled.extensions != {"bazaar": {"info": {}}}


# --------------------------------------------------------------------------
# wiring safety net
# --------------------------------------------------------------------------


async def test_context_is_available_on_request_state(store: Store) -> None:
    settings = make_settings(x402_mode="mock")
    seen: list[PaymentContext] = []

    router = paid_router()
    dependency = require_payment("player", settings=settings)

    async def route(request: Request, payment: PaymentContext = Depends(dependency)) -> Any:
        seen.append(request.state.payment)
        return {"same": request.state.payment is payment}

    router.add_api_route("/v1/player", route, methods=["POST"])
    app = FastAPI()
    install_x402_handlers(app)
    app.include_router(router)

    async with client_for(app) as client:
        response = await client.post(
            "/v1/player", json={}, headers={PAYMENT_SIGNATURE_HEADER: MOCK_PAYMENT_HEADER}
        )

    assert response.status_code == 200
    assert response.json() == {"same": True}
    assert seen[0].endpoint_key == "player"
    assert seen[0].amount_usdc == settings.price_for("player")


async def test_exception_handler_renders_402_on_a_plain_router(store: Store) -> None:
    """Safety net: a paid route on a bare APIRouter still emits a root-level 402.

    ``install_x402_handlers`` covers routers that were not built with
    ``paid_router()``. Such a route cannot settle — which is exactly why wave 3
    should use ``paid_router()`` — but it must never leak the payload into
    FastAPI's ``{"detail": ...}`` envelope.
    """
    from fastapi import APIRouter

    settings = make_settings(x402_mode="mock")
    router = APIRouter()
    dependency = require_payment("waivers", settings=settings)

    async def route(payment: PaymentContext = Depends(dependency)) -> Any:
        return {"paid": payment.paid}

    router.add_api_route("/v1/waivers", route, methods=["GET"])
    app = FastAPI()
    install_x402_handlers(app)
    app.include_router(router)

    async with client_for(app) as client:
        response = await client.get("/v1/waivers")

    assert response.status_code == 402
    body = response.json()
    assert "detail" not in body
    assert body["x402Version"] == 2
    assert body["accepts"][0]["amount"] == str(round(settings.price_for("waivers") * 1e6))
    assert PAYMENT_REQUIRED_HEADER in response.headers
