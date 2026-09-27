"""The paying client: sign an x402 quote, buy one analysis, never lose a payment.

The protocol shape mirrors ``examples/agent/fantasy_agent.py`` — the x402 SDK is
the authority for envelope construction and header codecs, and this module only
supplies the signature and the spending rules. Two things differ, and both exist
because the caller here is an autonomous agent rather than a human at a terminal:

* **Nothing is printed.** stdout is the MCP transport; a stray ``print`` corrupts
  the session. Progress goes to the ``logging`` module, which the server points
  at stderr.
* **Every payment is journalled before it is sent** (:mod:`playclock_mcp.journal`),
  so a timeout leaves a recoverable entry instead of a silent loss.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

import httpx
from x402 import PaymentRequired, PaymentRequirements, SettleResponse, x402Client
from x402.client import max_amount
from x402.http import x402HTTPClient

from playclock_mcp.errors import (
    ApiError,
    ApiUnreachable,
    DataNotReady,
    PaymentInFlight,
    PaymentRefused,
    PlayClockError,
    PriceTooHigh,
    WalletError,
)
from playclock_mcp.journal import PaymentJournal, PendingPayment

__all__ = [
    "AlgorandSigner",
    "MockSigner",
    "Payer",
    "Purchase",
    "build_signer",
    "price_usdc",
    "purchase",
    "recover",
]

logger = logging.getLogger("playclock_mcp.payments")

#: USDC carries 6 decimals on both Algorand networks. The server restates this in
#: ``accepts[].extra.decimals``, which is deliberately ignored: the price ceiling
#: must not be computed from a number the party being paid chose.
USDC_DECIMALS = 6

#: The USDC ASA on each Algorand network. A quote in any other asset is refused
#: before signing: an ASA id is the server's choice, and a transfer of some
#: other asset is exactly as signable as a USDC one.
USDC_ASA_IDS = {
    "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=": "31566704",
    "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=": "10458941",
}

#: One registration covers TestNet and MainNet — the SDK matches this wildcard
#: against whichever CAIP-2 network the 402 actually quotes.
ALGORAND_ANY = "algorand:*"

MNEMONIC_ENV = "ALGORAND_MNEMONIC"

#: Ceiling per call when nothing is configured. Deliberately above the most
#: expensive endpoint (0.50) so a default install can buy anything on the menu
#: once, and deliberately finite so a pricing bug cannot drain a wallet.
DEFAULT_MAX_PRICE_USDC = 1.00


class PaymentSigner(Protocol):
    """Structurally :class:`x402.interfaces.SchemeNetworkClient`."""

    @property
    def scheme(self) -> str:
        """Scheme identifier this signer satisfies (``"exact"`` on Algorand)."""
        ...

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Return the scheme-specific inner payload for one 402's requirements."""
        ...


class MockSigner:
    """Offline signer for a server running ``X402_MODE=mock``. No chain, no wallet.

    Sends the structured envelope rather than the bare ``mock-paid`` token, for
    the same reason the example agent does: every payment must be *distinct*, or
    the server's request-fingerprint binding 402s the second tool call.
    """

    scheme = "exact"

    def __init__(self) -> None:
        self.payments = 0

    @property
    def label(self) -> str:
        """Human-readable name for logs."""
        return "MockSigner (no chain, no wallet)"

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Mint a fresh mock payment for ``requirements``."""
        if not requirements.amount or not requirements.pay_to:
            raise PaymentRefused("402 quoted no amount or no payTo; refusing an incomplete quote")
        self.payments += 1
        return {"mock": True, "nonce": uuid4().hex}


class AlgorandSigner:
    """Signs a USDC transfer group on Algorand with a 25-word mnemonic.

    The SDK's ``ExactAvmScheme`` owns transaction construction; this class only
    supplies ``ClientAvmSigner`` (``address`` + ``sign_transactions``). The
    base64 hop in :meth:`sign_transactions` is load-bearing — ``msgpack_decode``
    takes a base64 *string* while the scheme passes raw bytes, and the example in
    the SDK's own docstring omits both hops and does not run (DESIGN_NOTES,
    TestNet checklist item 7).

    Args:
        phrase: 25-word Algorand mnemonic. Never logged, in whole or in part.
        algod_url: Override the SDK's per-network AlgoNode endpoint.

    Raises:
        WalletError: py-algorand-sdk missing, or the mnemonic is invalid.
    """

    scheme = "exact"

    def __init__(self, phrase: str, *, algod_url: str | None = None) -> None:
        try:
            from algosdk import account, mnemonic
            from x402.mechanisms.avm.exact import ExactAvmScheme
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise WalletError(
                'the Algorand payment path needs py-algorand-sdk: pip install "mcp,avm" extras'
            ) from exc

        cleaned = " ".join(phrase.split())
        if not cleaned:
            raise WalletError(f"{MNEMONIC_ENV} is empty")
        try:
            self._private_key = mnemonic.to_private_key(cleaned)
        except Exception as exc:
            raise WalletError(
                f"{MNEMONIC_ENV} is not a valid 25-word Algorand mnemonic ({type(exc).__name__})"
            ) from exc

        self._address: str = account.address_from_private_key(self._private_key)
        self._scheme = ExactAvmScheme(signer=self, algod_url=algod_url)

    @property
    def address(self) -> str:
        """The address paying, and the ``payer`` on the settlement receipt."""
        return self._address

    @property
    def label(self) -> str:
        """Human-readable name for logs — truncated, never the full key material."""
        return f"Algorand {self._address[:8]}...{self._address[-4:]}"

    def sign_transactions(
        self, unsigned_txns: list[bytes], indexes_to_sign: list[int]
    ) -> list[bytes | None]:
        """Sign our transactions in the group; leave the fee payer's unsigned.

        Args:
            unsigned_txns: Raw canonical msgpack bytes, one per group member.
            indexes_to_sign: Positions whose sender is :attr:`address`.

        Returns:
            Signed msgpack at each requested index, ``None`` everywhere else.
        """
        import base64

        from algosdk import encoding

        signed: list[bytes | None] = [None] * len(unsigned_txns)
        for index in indexes_to_sign:
            txn = encoding.msgpack_decode(base64.b64encode(unsigned_txns[index]).decode())
            signed[index] = base64.b64decode(encoding.msgpack_encode(txn.sign(self._private_key)))
        return signed

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Refuse anything but USDC, then delegate envelope construction to the SDK.

        Raises:
            PaymentRefused: The quote names an unsupported network or an asset
                other than that network's USDC. Nothing is signed.
        """
        network = str(requirements.network)
        expected = USDC_ASA_IDS.get(network)
        if expected is None:
            raise PaymentRefused(f"402 quoted network {network!r}; only Algorand USDC is paid")
        if str(requirements.asset) != expected:
            raise PaymentRefused(
                f"402 asked for ASA {requirements.asset} on {network}, but USDC there is "
                f"{expected}. Refusing to sign an unexpected asset."
            )
        return self._scheme.create_payment_payload(requirements)


def build_signer(*, mock: bool, mnemonic_phrase: str | None) -> PaymentSigner:
    """Pick a signer: explicit mock, else a mnemonic, else a clear failure."""
    if mock:
        return MockSigner()
    if mnemonic_phrase and mnemonic_phrase.strip():
        return AlgorandSigner(mnemonic_phrase)
    raise WalletError("no payment method configured")


def atomic_units(price: float, decimals: int = USDC_DECIMALS) -> int:
    """Convert a USDC price to atomic units. ``0.10 -> 100000``."""
    return round(price * (10**decimals))


def price_usdc(requirements: PaymentRequirements) -> float:
    """Human price for a quote, always at USDC's 6 decimals (see :data:`USDC_DECIMALS`)."""
    try:
        return int(requirements.amount) / (10**USDC_DECIMALS)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"402 quoted a non-numeric amount {requirements.amount!r}") from exc


@dataclass
class Payer:
    """A configured x402 client: one signer, one ceiling, one journal."""

    signer: PaymentSigner
    max_price_usdc: float = DEFAULT_MAX_PRICE_USDC
    journal: PaymentJournal = field(default_factory=PaymentJournal)
    client: x402Client = field(init=False, repr=False)
    codec: x402HTTPClient = field(init=False, repr=False)
    spent_usdc: float = field(default=0.0, init=False)
    calls_paid: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.client = x402Client()
        self.client.register(ALGORAND_ANY, self.signer)
        # :func:`purchase` refuses an over-price quote itself with a message
        # naming both numbers; this is the backstop that stops a future code
        # path paying past the ceiling silently.
        self.client.register_policy(max_amount(atomic_units(self.max_price_usdc)))
        self.codec = x402HTTPClient(self.client)

    @property
    def label(self) -> str:
        """Signer name, for logs and the wallet-status tool."""
        return getattr(self.signer, "label", type(self.signer).__name__)

    @property
    def is_mock(self) -> bool:
        """Whether this payer signs nothing real."""
        return isinstance(self.signer, MockSigner)


@dataclass
class Purchase:
    """One answered call: the body, what it cost, and the settlement receipt."""

    body: Any
    price: float = 0.0
    receipt: SettleResponse | None = None
    recovered: bool = False

    @property
    def settled(self) -> bool:
        """True only when a receipt came back and reports success."""
        return self.receipt is not None and bool(self.receipt.success)


async def _send(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Send one request. The paid retry sends exactly this plus the payment header."""
    try:
        if method == "GET":
            return await http.get(path, params=params or None, headers=headers)
        return await http.post(path, params=params or None, json=body, headers=headers)
    except httpx.RequestError as exc:
        raise ApiUnreachable(
            f"cannot reach {http.base_url}{path}: {type(exc).__name__}: {exc}"
        ) from exc


def _refusal_reason(response: httpx.Response) -> str:
    """Best-effort extraction of *why* a response was refused."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"
    if isinstance(payload, dict):
        detail = payload.get("error") or payload.get("detail")
        if isinstance(detail, dict):
            detail = detail.get("error") or str(detail)[:200]
        if detail:
            return str(detail)
    return f"HTTP {response.status_code}"


def _http_error(response: httpx.Response) -> PlayClockError:
    """Map a non-2xx onto the clearest error a model can act on."""
    reason = _refusal_reason(response)
    if response.status_code == 503:
        return DataNotReady(f"the service has no data to answer this yet: {reason}")
    if response.status_code == 400:
        return ApiError(f"the request was rejected as malformed: {reason}")
    return ApiError(f"HTTP {response.status_code}: {reason}")


async def purchase(
    http: httpx.AsyncClient,
    payer: Payer,
    *,
    tool: str,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Purchase:
    """Buy one analysis: call, read the 402, check the price, pay, retry, parse.

    The payment header is journalled **before** the paid retry is sent and
    cleared only once a response is in hand, so a timeout or a crash leaves a
    recoverable entry rather than a silent loss.

    Raises:
        ApiUnreachable: No answer from the host. Nothing signed.
        PriceTooHigh: Quote above the ceiling. Nothing signed.
        PaymentRefused: The paid retry was 402'd, or signing failed.
        DataNotReady: 503 — not charged, resolves after the next ingest.
        ApiError: Any other non-2xx, or an unparseable response.
    """
    params = params or {}
    response = await _send(http, method, path, params=params, body=body)

    # X402_MODE=disabled, or a free route: answered outright, nothing to settle.
    if response.is_success:
        return Purchase(body=response.json())
    if response.status_code != 402:
        raise _http_error(response)

    required = _parse_402(response, payer)
    quote = required.accepts[0]
    price = price_usdc(quote)

    if price > payer.max_price_usdc:
        raise PriceTooHigh(
            f"{tool} is quoted at {price:.6f} USDC, above the "
            f"{payer.max_price_usdc:.2f} USDC per-call ceiling. Nothing was signed."
        )

    logger.info("paying %.6f USDC for %s via %s", price, tool, payer.label)
    try:
        payload = await payer.client.create_payment_payload(required)
    except PlayClockError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any signing failure verbatim
        raise PaymentRefused(f"could not build a payment: {type(exc).__name__}: {exc}") from exc

    headers = payer.codec.encode_payment_signature_header(payload)
    header_name = next(iter(headers))
    entry = PendingPayment(
        tool=tool,
        method=method,
        path=path,
        params=params,
        body=body,
        header_name=header_name,
        header_value=headers[header_name],
        price_usdc=price,
    )
    # Before the request, not after: the window this protects is exactly the one
    # where no response ever comes back.
    payer.journal.record(entry)

    try:
        response = await _send(http, method, path, params=params, body=body, headers=headers)
    except ApiUnreachable as exc:
        # Past this line the payment has left the building. ApiUnreachable would
        # tell the agent nothing was charged and retrying is safe; both may now
        # be false, and acting on that advice signs a second payment for an
        # answer already bought. The journal entry stays for recovery.
        raise PaymentInFlight(
            f"{tool}: paid {price:.6f} USDC, then lost the response ({exc})"
        ) from exc

    return _finish(payer, entry, response, price)


def _parse_402(response: httpx.Response, payer: Payer) -> PaymentRequired:
    """Parse a 402 body/headers into the SDK's :class:`PaymentRequired`."""
    try:
        body = response.json()
    except ValueError:
        body = None
    try:
        required = payer.codec.get_payment_required_response(response.headers.get, body)
    except ValueError as exc:
        raise ApiError(f"402 carried no usable payment requirements: {exc}") from exc
    if not isinstance(required, PaymentRequired):  # pragma: no cover - V1 server
        raise ApiError(f"server speaks x402 v{required.x402_version}; this client is v2 only")
    if not required.accepts:
        raise ApiError("402 listed no acceptable payments")
    return required


#: Statuses a proxy in front of the app returns (Cloud Run / GFE timeouts and
#: upstream failures). The app's own refusals are 400/404/409/500/503.
_GATEWAY_STATUSES = frozenset({502, 504})


#: The app's own words when its handler raised (a Sleeper outage is its 502).
_NOT_CHARGED_MARKER = "you were not charged"


def _app_says_not_charged(response: httpx.Response) -> bool:
    """Whether the app itself, not a proxy, answered with a failure that settles nothing."""
    if "json" not in response.headers.get("content-type", ""):
        return False
    return _NOT_CHARGED_MARKER in _refusal_reason(response).lower()


#: What the server appends when a replayed, already-settled payment's re-run fails.
_REPLAY_SETTLED_MARKER = "already settled on its first use"


def _settled_replay_failed(response: httpx.Response, *, recovered: bool) -> bool:
    """Whether a non-2xx answered a payment that had already settled.

    The server says so in the detail. A 5xx during recovery is treated the
    same: recovery only replays payments that may have settled, and clearing
    the entry on a transient failure would lose the only way back to the answer.
    """
    if _REPLAY_SETTLED_MARKER in _refusal_reason(response):
        return True
    return recovered and response.status_code >= 500


def _finish(
    payer: Payer,
    entry: PendingPayment,
    response: httpx.Response,
    price: float,
    *,
    recovered: bool = False,
) -> Purchase:
    """Turn the paid response into a :class:`Purchase` and retire the journal entry."""
    if response.status_code == 402:
        reason = _refusal_reason(response)
        if reason.startswith("payment_in_progress"):
            # The first request is still generating and will settle when it
            # finishes. This entry is the only way back to that answer.
            raise PaymentInFlight(
                f"{entry.tool}: the paid request is still being answered; "
                "recover again in a few seconds"
            )
        # Verify failed, so nothing settled: this entry is not recoverable money.
        payer.journal.clear(entry)
        raise PaymentRefused(f"payment rejected: {reason}")
    if response.status_code in _GATEWAY_STATUSES and not _app_says_not_charged(response):
        # The proxy answered, not the app: the handler may still have finished
        # and settled behind it, so this is not proof nothing was charged.
        raise PaymentInFlight(
            f"{entry.tool}: paid {price:.6f} USDC, then the gateway returned "
            f"HTTP {response.status_code}"
        )
    if not response.is_success and _settled_replay_failed(response, recovered=recovered):
        # The payment settled on its first use and only this re-run failed.
        # The server says to retry with the same header; this entry is it.
        raise PaymentInFlight(
            f"{entry.tool}: the payment settled but the answer failed "
            f"(HTTP {response.status_code}); recover again shortly — do not pay again"
        )
    if not response.is_success:
        payer.journal.clear(entry)
        raise _http_error(response)

    receipt: SettleResponse | None = None
    try:
        receipt = payer.codec.get_payment_settle_response(response.headers.get)
    except ValueError:
        # The server never withholds a paid-for answer because settling failed.
        logger.warning("%s answered without a PAYMENT-RESPONSE receipt", entry.tool)

    try:
        body = response.json()
    except ValueError as exc:
        # A 2xx is the settled branch: the money moved. Parse before clearing,
        # or an unreadable body destroys the only record that it did.
        raise PaymentInFlight(
            f"{entry.tool}: paid {price:.6f} USDC, but the HTTP {response.status_code} "
            f"answer was not readable JSON ({exc})"
        ) from exc

    payer.journal.clear(entry)
    if receipt is not None and receipt.success:
        payer.spent_usdc += price
        payer.calls_paid += 1
    return Purchase(body=body, price=price, receipt=receipt, recovered=recovered)


async def recover(http: httpx.AsyncClient, payer: Payer) -> list[Purchase]:
    """Replay every still-replayable journalled payment and return what comes back.

    Safe by construction: an identical request with an identical header inside
    the server's idempotency window returns the cached receipt without settling
    again. Entries past that window are left on file as a diagnostic — they name
    a call that was paid for and lost.
    """
    recovered: list[Purchase] = []
    for entry in payer.journal.recoverable():
        headers = {entry.header_name: entry.header_value}
        try:
            response = await _send(
                http,
                entry.method,
                entry.path,
                params=entry.params,
                body=entry.body,
                headers=headers,
            )
            recovered.append(_finish(payer, entry, response, entry.price_usdc, recovered=True))
        except (ApiUnreachable, PlayClockError):
            continue
        except Exception:
            # One bad entry must never abort recovery of the rest: each one is
            # money already sent. Logged (stderr, never stdout) and left on file.
            logger.exception("recovering %s %s failed; entry kept", entry.method, entry.path)
            continue
    return recovered
