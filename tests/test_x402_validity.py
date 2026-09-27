"""The validity floor: a payment that lapses before settle is refused before verify.

Without it a payer signs a transfer valid for a few rounds, the handler outlasts
them, settle fails, and the answer is served free — on demand.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from algosdk import account, transaction

from api.core.store import Store
from api.x402 import PAYMENT_SIGNATURE_HEADER
from api.x402.facilitator import MockFacilitatorClient, set_facilitator
from api.x402.schemas_compat import (
    build_payment_requirements,
    payment_identity,
    payment_payload_from_header,
)
from api.x402.validity import (
    EXPIRES_TOO_SOON,
    MIN_REMAINING_ROUNDS,
    AlgodRoundClock,
    expires_too_soon,
    payment_last_valid,
    set_round_clock,
)
from tests.test_x402_middleware import build_app, client_for
from tests.test_x402_schemas import make_settings

CURRENT_ROUND = 50_000_000


class FixedRound:
    def __init__(self, value: int | None) -> None:
        self.value = value

    async def current_round(self) -> int | None:
        return self.value


def signed_header(
    settings: Any, *, last_valid: int, mangle: Any = None, payment_index: Any = 0
) -> str:
    """A real signed USDC transfer, wrapped the way a wallet sends it."""
    key, sender = account.generate_account()
    requirements = build_payment_requirements("trending", settings)
    params = transaction.SuggestedParams(
        fee=1000,
        first=CURRENT_ROUND - 5,
        last=last_valid,
        gh="wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
        flat_fee=True,
    )
    txn = transaction.AssetTransferTxn(
        sender, params, requirements.pay_to, int(requirements.amount), int(requirements.asset)
    )
    signed = transaction.encoding.msgpack_encode(txn.sign(key))  # base64 already
    if mangle is not None:
        signed = mangle(signed)
    body = {
        "x402Version": 2,
        "payload": {"paymentGroup": [signed], "paymentIndex": payment_index},
        "accepted": requirements.model_dump(by_alias=True, exclude_none=True),
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("X402_RESOURCE_BASE_URL", "https://api.playclock.xyz")
    return make_settings(x402_mode="live", x402_pay_to=account.generate_account()[1])


def test_last_valid_is_read_from_the_signed_transaction(live: Any) -> None:
    header = signed_header(live, last_valid=CURRENT_ROUND + 777)
    payload = payment_payload_from_header(header, build_payment_requirements("trending", live))
    assert payload is not None
    assert payment_last_valid(payload) == CURRENT_ROUND + 777


@pytest.mark.parametrize(
    ("remaining", "refused"),
    [(3, True), (MIN_REMAINING_ROUNDS - 1, True), (MIN_REMAINING_ROUNDS, False), (1000, False)],
)
async def test_the_floor_is_enforced_in_rounds(live: Any, remaining: int, refused: bool) -> None:
    set_round_clock(FixedRound(CURRENT_ROUND))
    header = signed_header(live, last_valid=CURRENT_ROUND + remaining)
    requirements = build_payment_requirements("trending", live)
    payload = payment_payload_from_header(header, requirements)
    assert payload is not None
    assert await expires_too_soon(payload, requirements.network, live) is refused


async def test_an_unknown_round_fails_open(live: Any) -> None:
    """algod down must not stop every sale."""
    set_round_clock(FixedRound(None))
    header = signed_header(live, last_valid=CURRENT_ROUND + 1)
    requirements = build_payment_requirements("trending", live)
    payload = payment_payload_from_header(header, requirements)
    assert payload is not None
    assert await expires_too_soon(payload, requirements.network, live) is False


async def test_a_short_lived_payment_is_refused_before_verify(store: Store, live: Any) -> None:
    set_round_clock(FixedRound(CURRENT_ROUND))
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = signed_header(live, last_valid=CURRENT_ROUND + 4)

    async with client_for(build_app(live)) as client:
        response = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})

    assert response.status_code == 402
    assert response.json()["error"] == EXPIRES_TOO_SOON
    assert facilitator.verify_calls == []


async def test_a_normal_payment_reaches_verify(store: Store, live: Any) -> None:
    set_round_clock(FixedRound(CURRENT_ROUND))
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = signed_header(live, last_valid=CURRENT_ROUND + 1000)

    async with client_for(build_app(live)) as client:
        await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})

    assert len(facilitator.verify_calls) == 1


async def test_the_algod_clock_caches_and_extrapolates() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        assert request.url.path == "/v2/status"
        return httpx.Response(200, json={"last-round": CURRENT_ROUND})

    clock = AlgodRoundClock("https://algod.example", transport=httpx.MockTransport(handler))
    first = await clock.current_round()
    second = await clock.current_round()

    assert first is not None and second is not None
    assert CURRENT_ROUND <= first <= second <= CURRENT_ROUND + 1
    assert len(calls) == 1, "one status read serves the refresh window"


async def test_the_algod_clock_returns_none_when_algod_is_down() -> None:
    clock = AlgodRoundClock(
        "https://algod.example",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    assert await clock.current_round() is None


class _FakeTime:
    """A monotonic clock the test advances by hand."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_the_algod_clock_fails_open_once_its_last_reading_is_stale() -> None:
    """A long algod outage must not extrapolate its way into refusing honest payments."""
    up = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        if up[0]:
            return httpx.Response(200, json={"last-round": CURRENT_ROUND})
        return httpx.Response(503)

    fake = _FakeTime()
    clock = AlgodRoundClock(
        "https://algod.example", transport=httpx.MockTransport(handler), monotonic=fake
    )
    assert await clock.current_round() == CURRENT_ROUND

    up[0] = False
    fake.now += 60
    assert await clock.current_round() == CURRENT_ROUND + 24, "a recent reading still counts"

    fake.now += 6 * 3600
    assert await clock.current_round() is None, "hours-old readings fail open"


async def test_the_algod_clock_backs_off_after_a_failed_read() -> None:
    """A hung algod must not add its timeout to every paid request."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503)

    fake = _FakeTime()
    clock = AlgodRoundClock(
        "https://algod.example", transport=httpx.MockTransport(handler), monotonic=fake
    )
    assert await clock.current_round() is None
    assert await clock.current_round() is None
    assert len(calls) == 1

    fake.now += 11
    assert await clock.current_round() is None
    assert len(calls) == 2


def _with_junk(signed: str) -> str:
    """Characters the facilitator's base64 decoder silently discards."""
    return f"{signed[:10]}!{signed[10:20]} {signed[20:]}"


def _payload(header: str, settings: Any) -> Any:
    payload = payment_payload_from_header(header, build_payment_requirements("trending", settings))
    assert payload is not None
    return payload


def test_junk_characters_do_not_hide_last_valid(live: Any) -> None:
    """The facilitator ignores them, so the floor must too, or it fails open."""
    header = signed_header(live, last_valid=CURRENT_ROUND + 4, mangle=_with_junk)
    assert payment_last_valid(_payload(header, live)) == CURRENT_ROUND + 4


def test_re_encoding_the_transaction_does_not_change_its_identity(live: Any) -> None:
    """One signed transaction is one purchase, however its base64 is dressed."""
    captured: list[str] = []

    def keep(signed: str) -> str:
        captured.append(signed)
        return signed

    header = signed_header(live, last_valid=CURRENT_ROUND + 1000, mangle=keep)
    body = json.loads(base64.b64decode(header))
    original = payment_identity(_payload(header, live))

    for variant in (
        _with_junk(captured[0]),
        base64.urlsafe_b64encode(base64.b64decode(captured[0])).decode().rstrip("="),
    ):
        body["payload"]["paymentGroup"] = [variant]
        reencoded = base64.b64encode(json.dumps(body).encode()).decode()
        assert payment_identity(_payload(reencoded, live)) == original


async def test_live_refuses_a_payload_without_a_readable_payment_index(
    store: Store, live: Any
) -> None:
    """``paymentIndex: true`` would key on the header, per endpoint."""
    set_round_clock(FixedRound(CURRENT_ROUND))
    facilitator = MockFacilitatorClient()
    set_facilitator(facilitator)
    header = signed_header(live, last_valid=CURRENT_ROUND + 1000, payment_index=True)

    async with client_for(build_app(live)) as client:
        response = await client.get("/v1/trending", headers={PAYMENT_SIGNATURE_HEADER: header})

    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_payload"
    assert facilitator.verify_calls == []


def group_header(settings: Any, *, last_valids: list[int], payment_index: int) -> str:
    """An atomic group of signed transfers; ``payment_index`` names the payment."""
    key, sender = account.generate_account()
    requirements = build_payment_requirements("trending", settings)
    txns = [
        transaction.AssetTransferTxn(
            sender,
            transaction.SuggestedParams(
                fee=1000,
                first=CURRENT_ROUND - 5,
                last=last_valid,
                gh="wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=",
                flat_fee=True,
            ),
            requirements.pay_to,
            int(requirements.amount) if i == payment_index else 0,
            int(requirements.asset),
        )
        for i, last_valid in enumerate(last_valids)
    ]
    transaction.assign_group_id(txns)
    body = {
        "x402Version": 2,
        "payload": {
            "paymentGroup": [transaction.encoding.msgpack_encode(t.sign(key)) for t in txns],
            "paymentIndex": payment_index,
        },
        "accepted": requirements.model_dump(by_alias=True, exclude_none=True),
    }
    return base64.b64encode(json.dumps(body).encode()).decode()


@pytest.mark.parametrize("payment_index", [0, 1])
async def test_a_short_lived_sibling_lapses_the_whole_group(live: Any, payment_index: int) -> None:
    """A group settles all or nothing, so its earliest ``lastValid`` is the floor."""
    set_round_clock(FixedRound(CURRENT_ROUND))
    rounds = [CURRENT_ROUND + 1000, CURRENT_ROUND + 4]
    header = group_header(live, last_valids=rounds, payment_index=payment_index)
    requirements = build_payment_requirements("trending", live)
    payload = _payload(header, live)

    assert payment_last_valid(payload) == CURRENT_ROUND + 4
    assert await expires_too_soon(payload, requirements.network, live) is True
