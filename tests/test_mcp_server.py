"""The MCP server against a real app: buy, fail, recover.

Everything here runs over ``httpx.ASGITransport`` against a real
``create_app()`` in ``X402_MODE=mock`` — the payment dependency, the
settle-after-2xx wrapper, the deterministic engine and the response cache are
all production objects. Only the chain is absent.

The properties pinned are the ones that cost money when they break:

* a paid tool call 402s, pays, retries and comes back with a receipt;
* a quote above the ceiling is refused **before** anything is signed;
* a response lost in flight leaves a journal entry, and replaying it returns
  the paid-for answer without settling a second time;
* a 503 is reported as "not charged", because that is what it means.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from api.core.store import Store
from api.x402 import RECEIPTS_COLLECTION
from playclock_mcp.errors import DataNotReady, PaymentInFlight, PlayClockError, PriceTooHigh
from playclock_mcp.journal import PaymentJournal
from playclock_mcp.payments import MockSigner, Payer, purchase, recover
from playclock_mcp.server import PlayClockMCP
from tests.test_routes_free import api_client, configure


@pytest.fixture
def payer(tmp_path: Path) -> Payer:
    """A mock-signing payer with an isolated journal."""
    return Payer(
        signer=MockSigner(),
        max_price_usdc=1.00,
        journal=PaymentJournal(tmp_path / "pending.json"),
    )


@asynccontextmanager
async def mcp_app(payer: Payer, **kwargs: Any) -> AsyncIterator[PlayClockMCP]:
    """An MCP app bound to the in-process API."""
    async with api_client() as client:
        app = PlayClockMCP(base_url="http://testserver", payer=payer, **kwargs)
        app._http = client  # noqa: SLF001 - the ASGI transport is the point
        yield app


@pytest.fixture
async def seeded_mock(store: Store, monkeypatch: pytest.MonkeyPatch) -> Store:
    """Golden fixture season, payments in mock mode."""
    from api.evals.golden import seed_store

    configure(monkeypatch, X402_MODE="mock")
    await seed_store(store)
    return store


# --------------------------------------------------------------------------
# buying
# --------------------------------------------------------------------------


async def test_paid_call_pays_settles_and_returns_the_analysis(
    seeded_mock: Store, payer: Payer
) -> None:
    async with api_client() as client:
        result = await purchase(
            client, payer, tool="playclock_trending", method="GET", path="/v1/trending"
        )

    assert result.price == pytest.approx(0.10)
    assert result.settled is True
    assert result.body["verdict"]
    # The nflverse CC-BY attribution must survive to whatever shows the user.
    assert result.body["meta"]["attribution"]

    receipts = await seeded_mock.list(RECEIPTS_COLLECTION)
    assert len(receipts) == 1
    assert payer.spent_usdc == pytest.approx(0.10)
    assert payer.calls_paid == 1


async def test_successful_call_leaves_no_pending_payment(seeded_mock: Store, payer: Payer) -> None:
    async with api_client() as client:
        await purchase(client, payer, tool="playclock_trending", method="GET", path="/v1/trending")

    assert payer.journal.pending() == []


async def test_post_endpoint_sends_body_and_query_separately(
    seeded_mock: Store, payer: Payer
) -> None:
    async with api_client() as client:
        result = await purchase(
            client,
            payer,
            tool="playclock_player",
            method="POST",
            path="/v1/player",
            params={"week": 5},
            body={"name": "Tyjae Spears"},
        )

    assert result.settled is True
    assert result.body["player"]["name"]


# --------------------------------------------------------------------------
# the guard
# --------------------------------------------------------------------------


async def test_quote_above_the_ceiling_is_refused_before_signing(
    seeded_mock: Store, tmp_path: Path
) -> None:
    signer = MockSigner()
    payer = Payer(
        signer=signer, max_price_usdc=0.05, journal=PaymentJournal(tmp_path / "pending.json")
    )

    async with api_client() as client:
        with pytest.raises(PriceTooHigh) as excinfo:
            await purchase(
                client, payer, tool="playclock_trending", method="GET", path="/v1/trending"
            )

    # Both numbers named, nothing signed, nothing journalled, nothing spent.
    assert "0.10" in str(excinfo.value) and "0.05" in str(excinfo.value)
    assert signer.payments == 0
    assert payer.journal.pending() == []
    assert payer.spent_usdc == 0.0
    assert "Nothing was signed" in excinfo.value.as_text()


async def test_cold_store_is_reported_as_not_charged(
    store: Store, monkeypatch: pytest.MonkeyPatch, payer: Payer
) -> None:
    # Nothing seeded: the route refuses to sell an empty board.
    configure(monkeypatch, X402_MODE="mock")

    async with api_client() as client:
        with pytest.raises(DataNotReady) as excinfo:
            await purchase(
                client, payer, tool="playclock_trending", method="GET", path="/v1/trending"
            )

    assert "NOT charged" in excinfo.value.as_text()


# --------------------------------------------------------------------------
# recovery — the reason the journal exists
# --------------------------------------------------------------------------


class LosesTheResponse(httpx.AsyncBaseTransport):
    """Passes the 402 through, then drops the paid retry on the floor.

    This is the exact failure the journal exists for: the payment reaches the
    server and settles, and the answer never reaches the client.
    """

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.dropped = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        if "payment-signature" in request.headers:
            await response.aread()
            self.dropped += 1
            raise httpx.ReadTimeout("connection lost", request=request)
        return response


async def test_lost_response_is_recoverable_without_paying_twice(
    seeded_mock: Store, payer: Payer
) -> None:
    from api.main import create_app

    app = create_app()
    flaky = LosesTheResponse(httpx.ASGITransport(app=app))

    # 1. The call is paid for, and the answer is lost in flight.
    async with httpx.AsyncClient(transport=flaky, base_url="http://testserver") as client:
        with pytest.raises(PaymentInFlight) as excinfo:
            await purchase(
                client, payer, tool="playclock_trending", method="GET", path="/v1/trending"
            )

    # Not ApiUnreachable: that one tells the agent nothing was charged and
    # retrying is safe, which past this point signs a second payment.
    hint = excinfo.value.as_text().lower()
    assert "do not retry" in hint
    assert "playclock_recover_payments" in hint

    assert flaky.dropped == 1
    pending = payer.journal.pending()
    assert len(pending) == 1, "a paid call that never answered must stay recoverable"
    assert pending[0].tool == "playclock_trending"

    settles_before = len(await seeded_mock.list(RECEIPTS_COLLECTION))

    # 2. Replaying the identical request with the identical header returns the
    #    answer that was already paid for, and settles nothing further.
    async with api_client() as client:
        recovered = await recover(client, payer)

    assert len(recovered) == 1
    assert recovered[0].recovered is True
    assert recovered[0].body["verdict"]
    assert len(await seeded_mock.list(RECEIPTS_COLLECTION)) == settles_before
    assert payer.journal.pending() == [], "a recovered payment is no longer pending"


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (402, {"error": "payment_in_progress_retry_shortly"}),
        (504, {"detail": "upstream request timeout"}),
        (502, {"detail": "bad gateway"}),
        (503, {"detail": "Data not ready. This payment already settled on its first use."}),
        (500, {"detail": "Internal Server Error"}),
    ],
)
async def test_recovery_keeps_an_entry_the_server_may_still_settle(
    payer: Payer, status: int, body: dict[str, Any]
) -> None:
    """Still generating, or a gateway error: the money may yet move, keep the header."""
    from playclock_mcp.journal import PendingPayment

    payer.journal.record(
        PendingPayment(
            tool="playclock_trending",
            method="GET",
            path="/v1/trending",
            params={},
            body=None,
            header_name="PAYMENT-SIGNATURE",
            header_value="signed",
            price_usdc=0.10,
        )
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert await recover(client, payer) == []

    assert len(payer.journal.pending()) == 1


async def test_the_apps_own_502_clears_the_entry_because_nothing_settled(payer: Payer) -> None:
    """Sleeper down is the app's 502, and it is right that nothing was charged."""
    from playclock_mcp.payments import _finish

    entry = _pending("/v1/roster", "signed-sleeper-down")
    payer.journal.record(entry)
    response = httpx.Response(
        502,
        json={"detail": "Sleeper is not responding right now. You were not charged. Try again."},
    )

    with pytest.raises(PlayClockError) as caught:
        _finish(payer, entry, response, 0.35)

    assert not isinstance(caught.value, PaymentInFlight)
    assert payer.journal.pending() == []


def _pending(path: str, header_value: str) -> Any:
    from playclock_mcp.journal import PendingPayment

    return PendingPayment(
        tool="playclock_trending",
        method="GET",
        path=path,
        params={},
        body=None,
        header_name="PAYMENT-SIGNATURE",
        header_value=header_value,
        price_usdc=0.10,
    )


async def test_a_non_json_paid_answer_keeps_the_journal_entry(payer: Payer) -> None:
    """A settled 2xx whose body will not parse is money moved: keep the record."""
    from playclock_mcp.payments import _finish

    entry = _pending("/v1/trending", "signed")
    payer.journal.record(entry)
    response = httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(PaymentInFlight):
        _finish(payer, entry, response, 0.10)

    assert len(payer.journal.pending()) == 1
    assert payer.calls_paid == 0


async def test_one_broken_entry_does_not_abort_recovery_of_the_rest(payer: Payer) -> None:
    payer.journal.record(_pending("/v1/broken", "signed-a"))
    payer.journal.record(_pending("/v1/trending", "signed-b"))

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/broken":
            raise RuntimeError("transport bug")
        return httpx.Response(200, json={"verdict": "ok"})

    transport = httpx.MockTransport(answer)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        recovered = await recover(client, payer)

    assert [p.body for p in recovered] == [{"verdict": "ok"}]
    assert [e.path for e in payer.journal.pending()] == ["/v1/broken"]


async def test_recovery_reports_nothing_to_do_when_the_journal_is_empty(
    seeded_mock: Store, payer: Payer
) -> None:
    async with mcp_app(payer) as app:
        result = await app.call("playclock_recover_payments", {})

    assert result.is_error is not True
    assert "No payments are awaiting" in result.content[0].text


async def test_recovery_does_not_call_a_still_generating_payment_lost(payer: Payer) -> None:
    """A 402 payment_in_progress is recoverable later, not gone: never say "lost"."""
    payer.journal.record(_pending("/v1/trending", "signed-in-progress"))
    transport = httpx.MockTransport(
        lambda request: httpx.Response(402, json={"error": "payment_in_progress_retry_shortly"})
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        app = PlayClockMCP(base_url="http://testserver", payer=payer)
        app._http = client  # noqa: SLF001 - the mock transport is the point
        result = await app.call("playclock_recover_payments", {})

    text = result.content[0].text
    assert "still_in_progress" in text
    assert "unrecoverable" not in text
    assert "past the server's replay window" not in text


# --------------------------------------------------------------------------
# the MCP surface
# --------------------------------------------------------------------------


async def test_tool_list_covers_every_paid_endpoint_plus_the_wallet_tools(
    seeded_mock: Store, payer: Payer
) -> None:
    from api.core.config import ENDPOINT_KEYS

    async with mcp_app(payer) as app:
        names = {t.name for t in await app.list_tools()}

    assert "playclock_wallet" in names
    assert "playclock_recover_payments" in names
    # Every paid endpoint became a tool, with no per-endpoint code here.
    assert len(names) >= len(ENDPOINT_KEYS) + 2


async def test_calling_a_paid_tool_through_mcp_reports_what_it_cost(
    seeded_mock: Store, payer: Payer
) -> None:
    async with mcp_app(payer) as app:
        result = await app.call("playclock_trending", {})

    assert result.is_error is not True
    text = result.content[0].text
    assert "Paid 0.100000 USDC" in text
    assert "settled" in text


async def test_free_tool_costs_nothing(seeded_mock: Store, payer: Payer) -> None:
    async with mcp_app(payer) as app:
        result = await app.call("playclock_trending_preview", {})

    assert result.is_error is not True
    assert payer.spent_usdc == 0.0


async def test_wallet_tool_reports_spend_without_touching_the_network(
    seeded_mock: Store, payer: Payer
) -> None:
    async with mcp_app(payer) as app:
        await app.call("playclock_trending", {})
        result = await app.call("playclock_wallet", {})

    status = result.structured_content
    assert status["spent_this_session_usdc"] == pytest.approx(0.10)
    assert status["paid_calls_this_session"] == 1
    assert status["max_price_usdc_per_call"] == 1.00
    assert status["payments_awaiting_answer"] == []


async def test_unknown_tool_is_an_error_not_a_crash(seeded_mock: Store, payer: Payer) -> None:
    async with mcp_app(payer) as app:
        result = await app.call("playclock_nonexistent", {})

    assert result.is_error is True
    assert "unknown tool" in result.content[0].text


async def test_over_ceiling_call_through_mcp_is_an_actionable_error(
    seeded_mock: Store, tmp_path: Path
) -> None:
    payer = Payer(
        signer=MockSigner(),
        max_price_usdc=0.05,
        journal=PaymentJournal(tmp_path / "pending.json"),
    )
    async with mcp_app(payer) as app:
        result = await app.call("playclock_trending", {})

    assert result.is_error is True
    # The model is told not to retry on its own — this is the user's call.
    assert "do not retry" in result.content[0].text.lower()


async def test_the_wallet_tool_says_which_network_it_is_paying_on(
    seeded_mock: Store, payer: Payer
) -> None:
    """The default base URL is the TestNet deployment, so this is the likeliest
    way to have the server configured wrong: a real wallet spending worthless
    USDC for real analysis."""
    async with mcp_app(payer) as app:
        await app.list_tools()  # triggers discovery
        status = (await app.call("playclock_wallet", {})).structured_content

    assert status["network"] == "testnet"
    # Mock signer, so nothing real is at stake here.
    assert status["spending_real_usdc"] is False


# -- the signer pays USDC and nothing else ----------------------------------


def _requirements(asset: str, network: str) -> Any:
    from x402 import PaymentRequirements

    return PaymentRequirements(
        scheme="exact",
        network=network,
        asset=asset,
        amount="100000",
        pay_to="A" * 58,
        max_timeout_seconds=300,
        extra={"decimals": 0},
    )


@pytest.mark.parametrize(
    ("asset", "network"),
    [
        ("12345", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="),
        ("10458941", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="),
        ("31566704", "eip155:8453"),
    ],
)
def test_the_algorand_signer_refuses_anything_but_usdc(asset: str, network: str) -> None:
    from algosdk import account, mnemonic

    from playclock_mcp.errors import PaymentRefused
    from playclock_mcp.payments import AlgorandSigner

    key, _ = account.generate_account()
    signer = AlgorandSigner(mnemonic.from_private_key(key))
    with pytest.raises(PaymentRefused):
        signer.create_payment_payload(_requirements(asset, network))


def test_the_price_ignores_the_servers_decimals() -> None:
    from playclock_mcp.payments import price_usdc

    # A quote claiming 0 decimals would otherwise read as 100000 USDC, or with
    # 12 as a fraction of a cent: the ceiling must not trust the payee's scale.
    quote = _requirements("31566704", "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=")
    assert price_usdc(quote) == pytest.approx(0.10)
