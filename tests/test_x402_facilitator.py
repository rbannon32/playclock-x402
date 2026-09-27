"""Facilitator client tests — offline, with injected transports.

Covers the mock client's accept/reject rules and deterministic receipts, and the
HTTP client's request shape against the GoPlausible verify/settle contract. No
network: :class:`httpx.MockTransport` stands in for the facilitator.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from api.core.config import Settings
from api.x402.facilitator import (
    MOCK_PAYER_ADDRESS,
    HttpFacilitatorClient,
    MockFacilitatorClient,
    close_facilitator,
    facilitator_base_url,
    get_facilitator,
    set_facilitator,
)
from api.x402.schemas_compat import (
    GOPLAUSIBLE_FACILITATOR_URL,
    MOCK_PAYMENT_HEADER,
    PaymentPayload,
    X402ConfigError,
    build_payment_requirements,
    payment_payload_from_header,
)
from tests.test_x402_schemas import make_settings


@pytest.fixture(autouse=True)
def _clear_facilitator_override() -> Any:
    set_facilitator(None)
    yield
    set_facilitator(None)


def mock_payload(
    endpoint_key: str = "trending", settings: Settings | None = None
) -> PaymentPayload:
    """A payload carrying the mock marker, bound to ``endpoint_key``'s requirements."""
    active = settings or make_settings()
    requirements = build_payment_requirements(endpoint_key, active)
    payload = payment_payload_from_header(MOCK_PAYMENT_HEADER, requirements)
    assert payload is not None
    return payload


# --------------------------------------------------------------------------
# MockFacilitatorClient
# --------------------------------------------------------------------------


async def test_mock_accepts_a_marked_payload() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    client = MockFacilitatorClient()

    result = await client.verify(mock_payload(), requirements)

    assert result.is_valid is True
    assert result.payer == MOCK_PAYER_ADDRESS
    assert len(client.verify_calls) == 1


async def test_mock_rejects_an_unmarked_payload() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    payload = PaymentPayload(payload={"paymentGroup": []}, accepted=requirements)
    client = MockFacilitatorClient()

    result = await client.verify(payload, requirements)

    assert result.is_valid is False
    assert result.invalid_reason == "mock_marker_missing"


async def test_mock_rejects_a_payment_priced_for_another_endpoint() -> None:
    """A $0.10 trending payment must not buy a $0.75 team report."""
    settings = make_settings()
    cheap = mock_payload("trending", settings)
    expensive = build_payment_requirements("team_report", settings)

    result = await MockFacilitatorClient().verify(cheap, expensive)

    assert result.is_valid is False
    assert result.invalid_reason == "amount_mismatch"


async def test_mock_settlement_is_deterministic() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    client = MockFacilitatorClient()

    first = await client.settle(mock_payload(), requirements)
    second = await client.settle(mock_payload(), requirements)

    assert first.success is True
    assert first.transaction == second.transaction
    assert len(first.transaction) == 52
    assert first.network == requirements.network
    assert len(client.settle_calls) == 2


async def test_mock_settlement_can_be_forced_to_fail() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    client = MockFacilitatorClient(settle_error="insufficient_funds")

    result = await client.settle(mock_payload(), requirements)

    assert result.success is False
    assert result.error_reason == "insufficient_funds"
    assert result.transaction == ""


# --------------------------------------------------------------------------
# HttpFacilitatorClient
# --------------------------------------------------------------------------


def recording_transport(
    responses: dict[str, tuple[int, dict[str, Any]]],
    seen: list[httpx.Request],
) -> httpx.MockTransport:
    """A transport that records requests and replies per URL path suffix."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for suffix, (status, body) in responses.items():
            if request.url.path.endswith(suffix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "no stub"})

    return httpx.MockTransport(handler)


async def test_http_verify_sends_the_v2_facilitator_body() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("player", settings)
    seen: list[httpx.Request] = []
    transport = recording_transport(
        {"/verify": (200, {"isValid": True, "payer": "PAYERADDR"})}, seen
    )
    client = HttpFacilitatorClient(GOPLAUSIBLE_FACILITATOR_URL, transport=transport)

    result = await client.verify(mock_payload("player", settings), requirements)

    assert result.is_valid is True
    assert result.payer == "PAYERADDR"

    request = seen[0]
    assert str(request.url) == f"{GOPLAUSIBLE_FACILITATOR_URL}/verify"
    body = json.loads(request.content)
    assert body["x402Version"] == 2
    assert body["paymentPayload"]["accepted"]["payTo"] == "PAYTOADDRESS"
    assert body["paymentRequirements"]["amount"] == "100000"
    assert body["paymentRequirements"]["extra"]["tag"] == "x402-global-challenge"


async def test_http_settle_parses_the_receipt() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    seen: list[httpx.Request] = []
    transport = recording_transport(
        {
            "/settle": (
                200,
                {
                    "success": True,
                    "payer": "PAYERADDR",
                    "transaction": "TXID123",
                    "network": requirements.network,
                },
            )
        },
        seen,
    )
    client = HttpFacilitatorClient(GOPLAUSIBLE_FACILITATOR_URL, transport=transport)

    result = await client.settle(mock_payload(), requirements)

    assert result.success is True
    assert result.transaction == "TXID123"
    assert seen[0].url.path.endswith("/settle")


async def test_http_verify_contains_transport_failures() -> None:
    """A facilitator outage must read as 'unpaid', not as a 500."""

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("facilitator down")

    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    client = HttpFacilitatorClient(GOPLAUSIBLE_FACILITATOR_URL, transport=httpx.MockTransport(boom))

    result = await client.verify(mock_payload(), requirements)

    assert result.is_valid is False
    assert result.invalid_reason == "facilitator_unavailable"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (503, "facilitator_unavailable"),
        (500, "facilitator_unavailable"),
        (429, "facilitator_unavailable"),
        (400, "facilitator_rejected_payment"),
    ],
)
async def test_http_verify_separates_an_outage_from_a_rejection(status: int, reason: str) -> None:
    """Only an outage becomes a 503 upstream; a 4xx is a verdict on the payment."""
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    transport = recording_transport({"/verify": (status, {"error": "nope"})}, [])
    client = HttpFacilitatorClient(GOPLAUSIBLE_FACILITATOR_URL, transport=transport)

    result = await client.verify(mock_payload(), requirements)

    assert result.is_valid is False
    assert result.invalid_reason == reason


async def test_http_settle_contains_non_200_responses() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    seen: list[httpx.Request] = []
    transport = recording_transport({"/settle": (503, {"error": "unavailable"})}, seen)
    client = HttpFacilitatorClient(GOPLAUSIBLE_FACILITATOR_URL, transport=transport)

    result = await client.settle(mock_payload(), requirements)

    assert result.success is False
    assert result.error_reason == "facilitator_unavailable"
    assert result.network == requirements.network


# --------------------------------------------------------------------------
# Mode-driven factory
# --------------------------------------------------------------------------


def test_facilitator_url_defaults_to_goplausible_not_the_sdk_default() -> None:
    assert facilitator_base_url(make_settings(x402_facilitator_url="")) == (
        GOPLAUSIBLE_FACILITATOR_URL
    )
    assert "x402.org" not in facilitator_base_url(make_settings())
    assert facilitator_base_url(make_settings(x402_facilitator_url="https://f.example/")) == (
        "https://f.example"
    )


def test_get_facilitator_follows_the_mode() -> None:
    assert isinstance(get_facilitator(make_settings(x402_mode="mock")), MockFacilitatorClient)
    # disabled never reaches a facilitator; a mock stands in so callers avoid None.
    assert isinstance(get_facilitator(make_settings(x402_mode="disabled")), MockFacilitatorClient)

    live = get_facilitator(make_settings(x402_mode="live"))
    assert isinstance(live, HttpFacilitatorClient)
    assert live.base_url == GOPLAUSIBLE_FACILITATOR_URL


def test_live_mode_rejects_a_non_https_facilitator() -> None:
    with pytest.raises(X402ConfigError):
        get_facilitator(make_settings(x402_mode="live", x402_facilitator_url="http://f.example"))


def test_set_facilitator_overrides_everything() -> None:
    sentinel = MockFacilitatorClient()
    set_facilitator(sentinel)
    assert get_facilitator(make_settings(x402_mode="live")) is sentinel
    set_facilitator(None)
    assert get_facilitator(make_settings(x402_mode="mock")) is not sentinel


async def test_close_facilitator_closes_the_built_client_and_spares_an_override() -> None:
    """App shutdown releases the live client's connections, never a test's fake."""
    live = get_facilitator(make_settings(x402_mode="live"))
    assert isinstance(live, HttpFacilitatorClient)
    closed: list[bool] = []

    async def aclose() -> None:
        closed.append(True)

    live.aclose = aclose  # type: ignore[method-assign]
    await close_facilitator()
    await close_facilitator()  # idempotent: nothing left to close
    assert closed == [True]
    assert get_facilitator(make_settings(x402_mode="live")) is not live

    override = MockFacilitatorClient()
    set_facilitator(override)
    try:
        await close_facilitator()
        assert get_facilitator(make_settings(x402_mode="live")) is override
    finally:
        set_facilitator(None)
