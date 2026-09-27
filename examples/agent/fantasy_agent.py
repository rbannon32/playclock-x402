#!/usr/bin/env python3
"""Example fantasy agent: discover Play Clock, pay with x402, get the analysis.

This is the runnable reference client PRD §7 promises — "an example 'fantasy
agent' script (pays via x402, gets analysis) in a public repo". It is deliberately
one file with no framework: an agent author should be able to read it top to
bottom and lift the four functions that matter into their own tool.

The flow (x402 protocol version 2)::

    1. DISCOVER   GET  {base}/v1/catalog          -> the paid menu, prices in USDC
                  (GET {base}/llms.txt is the same story in prose, for LLMs)

    2. ASK        GET  {base}/v1/trending         -> 402 Payment Required
                  or POST {base}/v1/player            body = V2 PaymentRequired:
                                                      {x402Version, error, resource,
                                                       accepts:[{scheme, network,
                                                       asset, amount, payTo, extra}]}
                                                      (also base64 in PAYMENT-REQUIRED)

    3. GUARD      amount is *atomic* USDC (6 decimals): "100000" == 0.10 USDC.
                  Refuse anything over --max-price BEFORE signing. An agent that
                  blind-pays whatever a server quotes is a wallet-draining bug.

    4. PAY        build a PaymentPayload for accepts[0] and base64 it into
                  PAYMENT-SIGNATURE, then send the *same* request again.

    5. RECEIPT    200 + PAYMENT-RESPONSE (base64 SettleResponse: success, txid,
                  network, payer) + the analysis body (verdict, confidence,
                  reasoning, stats_cited[], sources[], meta).

Who does the work
-----------------
Almost none of the protocol is hand-rolled. The installed ``x402-avm`` SDK owns:

* :class:`x402.x402Client` — requirement selection, price policies and the
  ``PaymentPayload`` envelope (``create_payment_payload``);
* :class:`x402.http.x402HTTPClient` — header names and base64 in both directions
  (``get_payment_required_response`` / ``encode_payment_signature_header`` /
  ``get_payment_settle_response``);
* :class:`x402.mechanisms.avm.exact.ExactAvmScheme` — the real Algorand payment:
  the ASA transfer group, fee-payer slot, msgpack encoding and ``paymentIndex``.

What this file adds is the two things the SDK leaves to the integrator: a
**signer** (the SDK ships the ``ClientAvmSigner`` protocol, not an
implementation) and the **agent's own judgement** — the price ceiling, and clean
handling of the failure modes a paying client actually hits.

The SDK also ships a fully automatic transport
(``x402.http.clients.x402HttpxClient``) that swallows the 402 and retries for
you, which is the right choice in production::

    from x402.http.clients import x402HttpxClient
    async with x402HttpxClient(payer.client, base_url=base) as http:
        response = await http.get("/v1/trending")   # pays transparently

It is *not* used here on purpose: this script exists to show the 402 handshake,
and a transport that hides the 402 hides the lesson (and the price quote you
wanted to check before paying).

Payment backends
----------------
``--mock``            :class:`MockSigner`. No chain, no wallet. Mints a fresh
                      ``{"mock": true, "nonce": ...}`` payload per request, which
                      a server running ``X402_MODE=mock`` accepts. One payment
                      buys one request — see :class:`MockSigner` for why a
                      constant token breaks on the second question.
``ALGORAND_MNEMONIC`` :class:`AlgorandSigner`. Real USDC on Algorand
                      TestNet/MainNet through the GoPlausible facilitator.

Usage::

    # against a local mock-mode server (no wallet, no chain)
    python fantasy_agent.py --base-url http://localhost:8000 --mock
    python fantasy_agent.py --base-url http://localhost:8000 --mock \\
        --ask "player:Bijan Robinson" --ask "matchup:Bijan Robinson,Breece Hall"

    # against the real thing, paying real USDC
    export ALGORAND_MNEMONIC="word word ... word"
    python fantasy_agent.py --base-url https://api.example.com --ask trending

Standalone install (this script needs nothing from the Play Clock repo)::

    pip install "httpx" "x402-avm[clients,avm]"

``[clients]`` pulls the HTTP client helpers, ``[avm]`` pulls py-algorand-sdk for
the real payment path. The mock path never imports algosdk.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import textwrap
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# The SDK is the protocol authority. Everything imported here is public API.
from x402 import PaymentRequired, PaymentRequirements, SettleResponse, x402Client
from x402.client import max_amount
from x402.http import x402HTTPClient

__all__ = [
    "AgentError",
    "AlgorandSigner",
    "ApiError",
    "ApiUnreachable",
    "Ask",
    "AnalysisResult",
    "DataNotReady",
    "MockSigner",
    "PaymentRefused",
    "PaymentSigner",
    "Payer",
    "PriceTooHigh",
    "WalletError",
    "ask_once",
    "discover",
    "parse_ask",
    "price_usdc",
    "run",
]

# ---------------------------------------------------------------------------
# Protocol constants
# ---------------------------------------------------------------------------

#: USDC carries 6 decimals on both Algorand networks, so ``amount`` (atomic
#: units, a *string* on the wire) divides by 1e6. The server also states this in
#: ``accepts[].extra.decimals``; we read that when present and fall back here.
USDC_DECIMALS = 6

#: CAIP-2 network ids for Algorand. Mirrors
#: ``x402.mechanisms.avm.constants.ALGORAND_{MAINNET,TESTNET}_CAIP2`` — inlined
#: because importing that package pulls in py-algorand-sdk, which the mock path
#: does not need.
ALGORAND_MAINNET = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="
ALGORAND_TESTNET = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="

#: Wildcard the SDK's ``find_schemes_by_network`` understands: register one
#: scheme for every Algorand network rather than guessing which one this
#: deployment settles on.
ALGORAND_ANY = "algorand:*"

#: The canonical USDC ASA on each network, as the wire's string form. An agent
#: about to sign a transfer should check it is being asked for the asset it
#: thinks it is — a server can quote *any* ASA id.
USDC_ASA_IDS = {ALGORAND_MAINNET: "31566704", ALGORAND_TESTNET: "10458941"}

#: Default ceiling per call. Never unlimited: the price arrives from the server,
#: and an agent loop that pays any quote is one hostile 402 away from empty.
DEFAULT_MAX_PRICE_USDC = 1.00

#: Env var holding the 25-word mnemonic for the real payment path.
MNEMONIC_ENV = "ALGORAND_MNEMONIC"

#: GET endpoints callable as a bare ``--ask <name>``.
SIMPLE_ASKS = {
    "trending": "/v1/trending",
    "sleepers": "/v1/sleepers",
    "waivers": "/v1/waivers",
    "report": "/v1/report",
}


# ---------------------------------------------------------------------------
# Errors — one class per thing that actually goes wrong in the field
# ---------------------------------------------------------------------------


class AgentError(RuntimeError):
    """Base class: anything this agent knows how to explain to its operator."""


class ApiUnreachable(AgentError):
    """The API did not answer at all (DNS, connection refused, timeout)."""


class ApiError(AgentError):
    """The API answered, but not with something this agent can use."""


class PriceTooHigh(AgentError):
    """The quote exceeded ``--max-price``. Nothing was signed and nothing paid."""


class PaymentRefused(AgentError):
    """The retry was 402'd again: the payment was rejected, not just missing."""


class DataNotReady(AgentError):
    """503 — ingestion has not produced the data yet. **Not charged.**"""


class PaymentInFlight(AgentError):
    """The payment was sent and the answer never arrived. **It may have been charged.**

    The server settles before its response leaves, so a timeout or a gateway
    error here can follow a real transfer. Paying again buys the answer twice;
    replaying the identical request with the identical header inside the
    server's 300s window returns it for free.
    """


class WalletError(AgentError):
    """The wallet could not be constructed (missing or invalid mnemonic)."""


# ---------------------------------------------------------------------------
# What to ask for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """One question to buy: an HTTP call plus the catalog key that prices it."""

    key: str
    method: str
    path: str
    json_body: dict[str, Any] | None = None
    label: str = ""

    def describe(self) -> str:
        """One-line ``METHOD /path`` (plus the body, when there is one)."""
        if self.json_body:
            return f"{self.method} {self.path} {json.dumps(self.json_body, separators=(',', ':'))}"
        return f"{self.method} {self.path}"


def parse_ask(text: str) -> Ask:
    """Turn a ``--ask`` argument into an :class:`Ask`.

    Accepted forms::

        trending | sleepers | waivers | report
        player:Bijan Robinson
        matchup:Bijan Robinson,Breece Hall

    Raises:
        ValueError: On an unknown form — with the list of valid ones, because a
            typo here should not cost a 402 round trip to discover.
    """
    raw = text.strip()
    lowered = raw.lower()

    if lowered in SIMPLE_ASKS:
        return Ask(key=lowered, method="GET", path=SIMPLE_ASKS[lowered], label=lowered)

    prefix, _, argument = raw.partition(":")
    prefix = prefix.strip().lower()
    argument = argument.strip()

    if prefix == "player" and argument:
        return Ask(
            key="player",
            method="POST",
            path="/v1/player",
            json_body={"name": argument},
            label=f"player {argument}",
        )

    if prefix == "matchup" and argument:
        players = [name.strip() for name in argument.split(",") if name.strip()]
        if not 2 <= len(players) <= 4:
            raise ValueError(
                f"matchup needs 2-4 comma-separated players, got {len(players)}: {argument!r}"
            )
        return Ask(
            key="matchup",
            method="POST",
            path="/v1/matchup",
            json_body={"players": players},
            label="matchup " + " vs ".join(players),
        )

    raise ValueError(
        f"unknown ask {text!r}. Try: "
        + ", ".join(sorted(SIMPLE_ASKS))
        + ", 'player:<name>', or 'matchup:<name>,<name>'"
    )


# ---------------------------------------------------------------------------
# The signer seam
# ---------------------------------------------------------------------------


class PaymentSigner(Protocol):
    """How this agent pays — and, not by accident, the SDK's own client seam.

    This is structurally :class:`x402.interfaces.SchemeNetworkClient`: a scheme
    name plus a factory for the **inner** payload dict.
    :class:`x402.x402Client` wraps whatever comes back into the full V2
    ``PaymentPayload`` envelope (``x402Version``, ``accepted``, ``resource``,
    ``extensions``) and never needs to know which implementation answered.

    Implementations here: :class:`MockSigner` and :class:`AlgorandSigner`.
    """

    @property
    def scheme(self) -> str:
        """Scheme identifier this signer satisfies (``"exact"`` on Algorand)."""
        ...

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Return the scheme-specific inner payload for one 402's requirements."""
        ...


class MockSigner:
    """Offline signer for a server running ``X402_MODE=mock``. No chain, no wallet.

    The server accepts a payment whose decoded ``payload`` object carries
    ``{"mock": true}`` (``MockFacilitatorClient`` in ``api/x402/facilitator.py``).
    The raw header value ``"mock-paid"`` is accepted too, and is fine for a
    one-shot ``curl`` — but this class deliberately sends the **structured
    envelope**, for two reasons:

    * it is the shape a real wallet sends, so the mock exercises the same
      code path (decode -> validate -> verify) as a live payment; and
    * every payment is *distinct*. The server binds one payment to one request
      (method + path + query + body) for a 300-second idempotency window and
      rejects a replay against a different request. A constant token therefore
      answers the first question and 402s the second. The ``nonce`` is what makes
      each call a new payment — mirrors ``MockPaymentProvider`` in
      ``web/js/payment.js``.

    A live-mode server rejects this at verify, which is exactly right: the mock
    must never accidentally work in production.
    """

    scheme = "exact"

    def __init__(self) -> None:
        self.payments = 0

    @property
    def label(self) -> str:
        """Human-readable name for logs."""
        return "MockSigner (no chain)"

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Mint a fresh mock payment for ``requirements``.

        The requirements are not needed to produce the marker, but a signer that
        cannot read what it is being asked to pay has no business claiming to
        satisfy it — so they are validated rather than ignored.
        """
        if not requirements.amount or not requirements.pay_to:
            raise PaymentRefused(
                "402 quoted no amount or no payTo; refusing to pay an incomplete quote"
            )
        self.payments += 1
        return {"mock": True, "nonce": uuid.uuid4().hex}


class AlgorandSigner:
    """Real payer: signs a USDC transfer group on Algorand with a 25-word mnemonic.

    Two protocol faces, which is the whole reason this class is short:

    * :class:`x402.interfaces.SchemeNetworkClient` (``scheme`` +
      ``create_payment_payload``) — what :class:`x402.x402Client` calls. Fully
      delegated to the SDK's :class:`~x402.mechanisms.avm.exact.ExactAvmScheme`,
      which builds the atomic group (asset transfer of ``amount`` atomic USDC to
      ``payTo``, an unsigned fee-payer self-payment first when
      ``extra.feePayer`` is present, group id, msgpack + base64, ``paymentIndex``
      pointing at the transfer).
    * :class:`x402.mechanisms.avm.ClientAvmSigner` (``address`` +
      ``sign_transactions``) — what ``ExactAvmScheme`` calls back into. The SDK
      ships that protocol but **no implementation**: supplying it is the
      integrator's job, and it is the only cryptography in this file.

    Encoding contract with ``ExactAvmScheme`` (get this wrong and the facilitator
    returns ``invalid_exact_avm_payload_group_decode_failed``)::

        in :  list[bytes]  raw canonical msgpack, one per transaction
        out:  list[bytes | None]  raw canonical msgpack of the *signed* txn at
              each index in ``indexes_to_sign``, None everywhere else

    ``algosdk.encoding.msgpack_decode`` takes a **base64 string** and
    ``msgpack_encode`` returns one, so both ends need a base64 hop. The example
    in the SDK's own ``x402/mechanisms/avm/__init__.py`` docstring skips both
    hops and does not run; the implementation below is written against
    ``ExactAvmScheme.create_payment_payload``, which b64-decodes what it hands in
    and b64-encodes what it gets back.

    VALIDATION STATUS
        The transaction construction and signing are exercised offline by
        ``examples/agent/selftest.py``, which injects a stub Algod, builds a
        payment group against fabricated TestNet requirements, and checks the
        decoded ``axfer`` (receiver, asset id, amount, sender) plus the ed25519
        signature using the facilitator's own
        ``x402.mechanisms.avm.utils.verify_transaction_signature``. The
        **on-chain** round trip — suggested params from a live Algod, the
        facilitator's simulate step, settlement, the returned txid — is
        validated on TestNet during the pre-MainNet validation session
        (PRD §7, Sept 1-8), not from CI.

    Args:
        phrase: 25-word Algorand mnemonic.
        algod_url: Override the SDK's per-network AlgoNode endpoint. The SDK also
            reads ``ALGOD_TESTNET_URL`` / ``ALGOD_MAINNET_URL`` from the
            environment.
        allow_any_asset: Sign transfers of assets other than the network's
            canonical USDC. Off by default — see :meth:`create_payment_payload`.

    Raises:
        WalletError: When py-algorand-sdk is missing or the mnemonic is invalid.
    """

    scheme = "exact"

    def __init__(
        self,
        phrase: str,
        *,
        algod_url: str | None = None,
        allow_any_asset: bool = False,
    ) -> None:
        try:
            # Imported lazily so the mock path works without the [avm] extra.
            from algosdk import account, mnemonic
            from x402.mechanisms.avm.exact import ExactAvmScheme
        except ImportError as exc:  # pragma: no cover - depends on install extras
            raise WalletError(
                "the Algorand payment path needs py-algorand-sdk: "
                'pip install "x402-avm[clients,avm]"'
            ) from exc

        cleaned = " ".join(phrase.split())
        if not cleaned:
            raise WalletError(
                f"no wallet: set {MNEMONIC_ENV} to a 25-word Algorand mnemonic, "
                "or pass --mock to pay a mock-mode server with no chain at all"
            )
        try:
            self._private_key = mnemonic.to_private_key(cleaned)
        except Exception as exc:
            # Never echo the phrase (or any part of it) into logs.
            raise WalletError(
                f"{MNEMONIC_ENV} is not a valid 25-word Algorand mnemonic "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        self._address: str = account.address_from_private_key(self._private_key)
        self._allow_any_asset = allow_any_asset
        # The SDK owns transaction construction; we only sign.
        self._scheme = ExactAvmScheme(signer=self, algod_url=algod_url)

    # -- ClientAvmSigner ---------------------------------------------------

    @property
    def address(self) -> str:
        """The 58-character Algorand address paying (and the ``payer`` on the receipt)."""
        return self._address

    @property
    def label(self) -> str:
        """Human-readable name for logs, including the paying address."""
        return f"AlgorandSigner {self._address[:8]}...{self._address[-4:]}"

    def sign_transactions(
        self, unsigned_txns: list[bytes], indexes_to_sign: list[int]
    ) -> list[bytes | None]:
        """Sign our transactions in the group; leave the fee payer's alone.

        Args:
            unsigned_txns: Raw canonical msgpack bytes, one per group member.
            indexes_to_sign: Positions whose sender is :attr:`address`. Anything
                else belongs to the facilitator's fee payer and must come back
                ``None`` so it travels unsigned.

        Returns:
            A list parallel to ``unsigned_txns``: raw signed msgpack bytes at the
            requested indexes, ``None`` elsewhere.
        """
        import base64

        from algosdk import encoding

        wanted = set(indexes_to_sign)
        signed: list[bytes | None] = []
        for index, raw in enumerate(unsigned_txns):
            if index not in wanted:
                signed.append(None)
                continue
            # base64 in: msgpack_decode wants a base64 *string*, not bytes.
            txn = encoding.msgpack_decode(base64.b64encode(raw).decode("ascii"))
            # base64 out: msgpack_encode returns a base64 string, and
            # ExactAvmScheme b64-encodes whatever we return, so undo it here.
            signed.append(base64.b64decode(encoding.msgpack_encode(txn.sign(self._private_key))))
        return signed

    # -- SchemeNetworkClient ----------------------------------------------

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Build the ``{paymentGroup, paymentIndex}`` payload for one 402.

        Checks the quoted asset before signing: ``asset`` is an ASA id chosen by
        the server, and a transfer of some other ASA to some other address is
        just as signable as a USDC one. Pass ``allow_any_asset=True`` to opt out
        (e.g. a service priced in a different stablecoin).

        Raises:
            PaymentRefused: When the quote names a non-USDC asset and
                ``allow_any_asset`` is off, or an unsupported network.
        """
        network = str(requirements.network)
        expected = USDC_ASA_IDS.get(network)
        if expected is None:
            raise PaymentRefused(
                f"402 quoted network {network!r}; this signer only pays Algorand "
                f"({', '.join(sorted(USDC_ASA_IDS))})"
            )
        if str(requirements.asset) != expected and not self._allow_any_asset:
            raise PaymentRefused(
                f"402 asked for ASA {requirements.asset} on {network}, but USDC there is "
                f"{expected}. Refusing to sign an unexpected asset (--allow-any-asset overrides)."
            )
        # From here the SDK does everything: suggested params from Algod, the
        # fee-payer slot when extra.feePayer is set, group id, and the callback
        # into sign_transactions() above.
        return self._scheme.create_payment_payload(requirements)


def build_signer(
    *,
    mock: bool,
    mnemonic_phrase: str | None,
    algod_url: str | None = None,
    allow_any_asset: bool = False,
) -> PaymentSigner:
    """Pick the signer: explicit ``--mock``, else a mnemonic, else a clear failure."""
    if mock:
        return MockSigner()
    if mnemonic_phrase and mnemonic_phrase.strip():
        return AlgorandSigner(mnemonic_phrase, algod_url=algod_url, allow_any_asset=allow_any_asset)
    raise WalletError(
        f'no payment method: export {MNEMONIC_ENV}="<25 words>" to pay real USDC, '
        "or pass --mock to pay a server started with X402_MODE=mock"
    )


# ---------------------------------------------------------------------------
# The payer: SDK client + this agent's spending rules
# ---------------------------------------------------------------------------


def atomic_units(price_usdc: float, decimals: int = USDC_DECIMALS) -> int:
    """Convert a USDC price to atomic units. ``0.10 -> 100000``."""
    return round(price_usdc * (10**decimals))


def price_usdc(requirements: PaymentRequirements) -> float:
    """Human price for a quote, honouring ``extra.decimals`` when the server sets it."""
    decimals = USDC_DECIMALS
    extra = requirements.extra or {}
    if isinstance(extra.get("decimals"), int):
        decimals = int(extra["decimals"])
    try:
        return int(requirements.amount) / (10**decimals)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"402 quoted a non-numeric amount {requirements.amount!r}") from exc


@dataclass
class Payer:
    """A configured x402 client: one signer, one price ceiling.

    Attributes:
        signer: The :class:`PaymentSigner` that will produce payloads.
        max_price_usdc: Hard ceiling per call, enforced twice (see below).
        client: The SDK client that selects requirements and builds the envelope.
        codec: The SDK's HTTP codec — header names and base64, both directions.
    """

    signer: PaymentSigner
    max_price_usdc: float = DEFAULT_MAX_PRICE_USDC
    client: x402Client = field(init=False, repr=False)
    codec: x402HTTPClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.client = x402Client()
        # One registration covers TestNet and MainNet: the SDK matches
        # "algorand:*" against whichever CAIP-2 network the 402 quotes.
        self.client.register(ALGORAND_ANY, self.signer)
        # Belt and braces. :func:`ask_once` refuses an over-price quote itself,
        # with a message naming both numbers; this SDK policy is the backstop
        # that keeps a future code path from paying past the ceiling silently.
        self.client.register_policy(max_amount(atomic_units(self.max_price_usdc)))
        self.codec = x402HTTPClient(self.client)

    @property
    def label(self) -> str:
        """Signer name for logs."""
        return getattr(self.signer, "label", type(self.signer).__name__)


@dataclass
class AnalysisResult:
    """One answered question: the body, what it cost, and the settlement receipt."""

    ask: Ask
    body: dict[str, Any]
    quote: PaymentRequirements | None = None
    receipt: SettleResponse | None = None
    price: float = 0.0

    @property
    def settled(self) -> bool:
        """True only when a settlement receipt came back and reports success.

        The server deliberately still returns the analysis when *settlement*
        fails after a good verify (the buyer keeps their USDC and the failure
        is logged server-side), and a deployment with payments disabled sends
        no receipt at all — neither of those cost the agent anything, so only
        a successful receipt counts as money actually spent.
        """
        return self.receipt is not None and bool(self.receipt.success)


# ---------------------------------------------------------------------------
# 1. Discover
# ---------------------------------------------------------------------------


async def discover(http: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch ``GET /v1/catalog``: the machine-readable menu, free of charge.

    The catalog is the contract an agent should plan against — path, method,
    price in USDC, request/response schema names, cache TTL, plus the payment
    configuration (network, ``payTo``, USDC ASA id, facilitator, challenge tag).
    ``GET /llms.txt`` says the same thing in prose for a language model.

    Raises:
        ApiUnreachable: The host did not answer.
        ApiError: It answered with something other than a catalog.
    """
    try:
        response = await http.get("/v1/catalog")
    except httpx.RequestError as exc:
        raise ApiUnreachable(
            f"cannot reach {http.base_url}: {type(exc).__name__}: {exc}. "
            "Is the API running? (local: X402_MODE=mock uv run uvicorn api.main:app)"
        ) from exc
    if response.status_code != 200:
        raise ApiError(f"GET /v1/catalog returned {response.status_code}: {response.text[:300]}")
    try:
        catalog = response.json()
    except ValueError as exc:
        raise ApiError("GET /v1/catalog did not return JSON") from exc
    if not isinstance(catalog, dict) or "endpoints" not in catalog:
        raise ApiError("GET /v1/catalog returned an unrecognised document")
    return catalog


# ---------------------------------------------------------------------------
# 2-5. Ask, guard, pay, retry, read the receipt
# ---------------------------------------------------------------------------


async def _send(
    http: httpx.AsyncClient, ask: Ask, headers: dict[str, str] | None = None
) -> httpx.Response:
    """Send ``ask`` once. The retry sends exactly this request plus the payment header."""
    try:
        if ask.method == "GET":
            return await http.get(ask.path, headers=headers)
        return await http.post(ask.path, json=ask.json_body, headers=headers)
    except httpx.RequestError as exc:
        raise ApiUnreachable(
            f"cannot reach {http.base_url}{ask.path}: {type(exc).__name__}: {exc}"
        ) from exc


def _payment_required(response: httpx.Response, codec: x402HTTPClient) -> PaymentRequired:
    """Parse a 402 into the SDK's :class:`PaymentRequired`.

    The SDK reads the base64 ``PAYMENT-REQUIRED`` header first and falls back to
    the body, which is also where a V1 server puts it. Play Clock sends
    both, with the V2 payload at the *root* of the body (not under FastAPI's
    ``detail`` envelope) — worth knowing when pointing this script at another
    x402 service.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    try:
        required = codec.get_payment_required_response(response.headers.get, body)
    except ValueError as exc:
        raise ApiError(f"402 carried no usable payment requirements: {exc}") from exc
    if not isinstance(required, PaymentRequired):  # pragma: no cover - V1 server
        raise ApiError(
            f"server speaks x402 v{required.x402_version}; this agent implements v2 only"
        )
    if not required.accepts:
        raise ApiError("402 listed no acceptable payments")
    return required


def _refusal_reason(response: httpx.Response) -> str:
    """Best-effort extraction of *why* a 402 or an error response was returned."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("error") or body.get("detail")
        if isinstance(detail, dict):
            detail = detail.get("error") or json.dumps(detail)[:200]
        if detail:
            return str(detail)
    return f"HTTP {response.status_code}"


async def ask_once(
    http: httpx.AsyncClient,
    payer: Payer,
    ask: Ask,
    *,
    log: Callable[[str], None] = print,
) -> AnalysisResult:
    """Buy one analysis: call, read the 402, check the price, pay, retry, parse.

    This is the whole protocol, and it is importable on purpose — point it at any
    ``httpx.AsyncClient`` (including one over an in-process ASGI app, as
    ``selftest.py`` does) and it behaves identically.

    Args:
        http: Client whose ``base_url`` is the service origin.
        payer: Signer plus price ceiling.
        ask: What to buy.
        log: Where progress goes. Pass ``lambda _: None`` to silence it.

    Returns:
        The analysis body, the quote it was bought at, and the settlement receipt.

    Raises:
        ApiUnreachable: No answer from the host.
        PriceTooHigh: Quote above ``payer.max_price_usdc``. Nothing was signed.
        PaymentRefused: The paid retry was 402'd again.
        DataNotReady: 503 — ingest has not produced the data. Not charged.
        ApiError: Any other non-2xx, or an unparseable response.
    """
    log(f"  ask       {ask.describe()}")
    response = await _send(http, ask)

    # A server running X402_MODE=disabled (or a free endpoint) answers straight
    # away. Nothing to pay, nothing to settle.
    if response.is_success:
        log(f"  {response.status_code} OK    (no payment required by this deployment)")
        return AnalysisResult(ask=ask, body=response.json())

    if response.status_code != 402:
        raise _http_error(response, ask)

    # --- the 402: read the quote before deciding anything ---
    required = _payment_required(response, payer.codec)
    quote = required.accepts[0]
    price = price_usdc(quote)
    log(f"  402       {required.error or 'payment_required'}")
    log(f"            {quote.amount} atomic = {price:.6f} USDC ({quote.scheme} on {quote.network})")
    log(f"            asset {quote.asset} -> payTo {quote.pay_to}")
    tag = (quote.extra or {}).get("tag")
    if tag:
        log(f"            challenge tag {tag}, valid {quote.max_timeout_seconds}s")

    # --- the guard: an agent never blind-pays ---
    if price > payer.max_price_usdc:
        raise PriceTooHigh(
            f"{ask.describe()} is quoted at {price:.6f} USDC, above the "
            f"{payer.max_price_usdc:.2f} USDC ceiling. Nothing signed, nothing paid. "
            "Raise --max-price if that is genuinely what you meant to spend."
        )

    # --- pay: the SDK selects requirements, calls the signer, builds the envelope ---
    log(f"  paying    {price:.6f} USDC via {payer.label}")
    try:
        payload = await payer.client.create_payment_payload(required)
    except AgentError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any signing failure verbatim
        raise PaymentRefused(f"could not build a payment: {type(exc).__name__}: {exc}") from exc

    headers = payer.codec.encode_payment_signature_header(payload)
    header_name = next(iter(headers))
    log(f"  retry     with {header_name} ({len(headers[header_name])} base64 chars)")

    # --- retry: the same request, now carrying the payment ---
    # Past this line a lost answer may already be paid for: keep the header.
    replay_hint = (
        f"Do not pay again: resend the identical request with {header_name} = "
        f"{headers[header_name]} within 300s to receive the answer free."
    )
    try:
        response = await _send(http, ask, headers)
    except ApiUnreachable as exc:
        raise PaymentInFlight(f"{exc}. The payment may have settled. {replay_hint}") from exc
    if response.status_code == 504 or (
        response.status_code == 502 and "json" not in response.headers.get("content-type", "")
    ):
        # A gateway answered, not the app: the handler may have finished and
        # settled behind it. (The app's own 502 carries a JSON detail.)
        raise PaymentInFlight(
            f"gateway returned {response.status_code} after payment. {replay_hint}"
        )

    if response.status_code == 402:
        raise PaymentRefused(
            f"payment rejected: {_refusal_reason(response)}. "
            "In mock mode the server needs X402_MODE=mock; in live mode the payment must "
            "verify at the facilitator (funded account, USDC opt-in, correct network)."
        )
    if not response.is_success:
        raise _http_error(response, ask)

    # --- the receipt: base64 SettleResponse in PAYMENT-RESPONSE ---
    receipt: SettleResponse | None = None
    try:
        receipt = payer.codec.get_payment_settle_response(response.headers.get)
    except ValueError:
        # The answer arrived and settlement is the server's problem, not ours:
        # it never withholds a paid-for response because settling went wrong.
        log(f"  warning   {response.status_code} without a PAYMENT-RESPONSE receipt header")

    log(f"  {response.status_code} OK    {len(response.content)} bytes")
    return AnalysisResult(ask=ask, body=response.json(), quote=quote, receipt=receipt, price=price)


def _http_error(response: httpx.Response, ask: Ask) -> AgentError:
    """Map a non-2xx onto the clearest error this agent can raise."""
    reason = _refusal_reason(response)
    if response.status_code == 503:
        # The server refuses *before* settling when a dataset is missing, so this
        # costs nothing — it says so in the detail, and it is worth repeating.
        return DataNotReady(f"{reason} (503: the analysis was not generated and not charged)")
    if response.status_code == 404:
        return ApiError(f"not found: {reason}")
    if response.status_code == 400:
        return ApiError(f"bad request for {ask.describe()}: {reason}")
    # The app refuses before settling, and says so itself; a replayed payment
    # that already settled is the exception, and its detail says that instead.
    if response.status_code == 502:
        return ApiError(f"upstream data provider is down: {reason}")
    if response.status_code >= 500:
        return ApiError(f"server error {response.status_code}: {reason}")
    return ApiError(f"unexpected {response.status_code} for {ask.describe()}: {reason}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_WRAP = textwrap.TextWrapper(width=96, initial_indent=" " * 12, subsequent_indent=" " * 12)


def print_catalog(catalog: dict[str, Any], log: Callable[[str], None] = print) -> None:
    """Print the menu the way an operator wants to read it: prices, then endpoints."""
    log(f"  service   {catalog.get('service')} {catalog.get('version')}")
    log(
        f"  payments  Algorand {catalog.get('network')}, USDC ASA {catalog.get('asset_id')}, "
        f"payTo {catalog.get('pay_to') or '(unset)'}"
    )
    log(
        f"  x402      facilitator {catalog.get('facilitator_url')} "
        f"tag {catalog.get('challenge_tag')}"
    )
    endpoints = catalog.get("endpoints") or []
    paid = [entry for entry in endpoints if not entry.get("free")]
    free = [entry for entry in endpoints if entry.get("free")]
    log("  paid menu:")
    for entry in paid:
        ttl = entry.get("cache_ttl_seconds")
        freshness = f"cached {ttl // 3600}h" if ttl else "fresh"
        log(
            f"    {entry['method']:<4} {entry['path']:<16} {entry['price_usdc']:>5.2f} USDC  "
            f"[{freshness:<9}] {_clip(entry['description'], 62)}"
        )
    log("  free:")
    for entry in free:
        log(f"    {entry['method']:<4} {entry['path']:<24} {_clip(entry['description'], 62)}")


def _clip(text: str, width: int) -> str:
    """One-line summary, ellipsised rather than cut mid-word without warning."""
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 3].rstrip() + "..."


def print_receipt(result: AnalysisResult, log: Callable[[str], None] = print) -> None:
    """Print the settlement receipt: what was paid, on what chain, by whom."""
    receipt = result.receipt
    if receipt is None:
        log("  receipt   (none — this deployment is not charging)")
        return
    status = "settled" if receipt.success else f"FAILED ({receipt.error_reason})"
    log(f"  receipt   {status} for {result.price:.6f} USDC")
    log(f"            txid    {receipt.transaction or '(none)'}")
    log(f"            network {receipt.network}")
    log(f"            payer   {receipt.payer or '(unreported)'}")
    if not receipt.success and receipt.error_message:
        log(f"            note    {receipt.error_message}")


def print_analysis(result: AnalysisResult, log: Callable[[str], None] = print) -> None:
    """Print what was bought: the verdict, the reasoning, and the cited numbers.

    Every paid body carries the same core contract — ``verdict``, ``confidence``,
    ``reasoning``, ``stats_cited[]``, ``sources[]``, ``meta`` — plus one
    endpoint-specific list. An agent consuming this programmatically should read
    ``stats_cited`` before trusting a number: the service cites what it claims.
    """
    body = result.body
    log(f"  verdict   {body.get('verdict', '(none)')}")
    log(f"  confidence {body.get('confidence', '(none)')}")
    reasoning = str(body.get("reasoning") or "").strip()
    if reasoning:
        log("  reasoning")
        for line in _WRAP.wrap(reasoning) or []:
            log(line)

    stats = body.get("stats_cited") or []
    if stats:
        log(f"  stats cited ({len(stats)})")
        for stat in stats[:6]:
            who = f" [{stat['player']}]" if stat.get("player") else ""
            log(
                f"            {stat.get('stat')} = {stat.get('value')}{who} <- {stat.get('source')}"
            )
        if len(stats) > 6:
            log(f"            ... {len(stats) - 6} more")

    for line in _highlights(body):
        log(f"            {line}")

    meta = body.get("meta") or {}
    log(
        f"  meta      cache={meta.get('cache')} model={meta.get('model')} "
        f"generated_at={meta.get('generated_at')}"
    )


def _highlights(body: dict[str, Any]) -> list[str]:
    """A few rows from whichever endpoint-specific list this body carries."""
    if isinstance(body.get("players"), list):  # /v1/trending
        rows = body["players"]
        out = [f"board: {len(rows)} players"]
        out += [
            f"{row.get('name')} ({row.get('position')} {row.get('team')}) "
            f"{row.get('trend')} {row.get('trend_count')} -> {row.get('verdict')}"
            for row in rows[:5]
        ]
        return out
    if isinstance(body.get("ranked"), list):  # /v1/matchup
        return [
            f"{row.get('rank')}. {row.get('name')} ({row.get('position')}) -> {row.get('call')}"
            for row in body["ranked"]
        ]
    if isinstance(body.get("picks"), list):  # /v1/sleepers
        return [
            f"{row.get('name')} ({row.get('position')}) {row.get('confidence')}"
            for row in body["picks"][:5]
        ]
    if isinstance(body.get("board"), list):  # /v1/waivers
        return [
            f"{row.get('rank')}. {row.get('name')} ({row.get('position')}) "
            f"{row.get('stash_or_start')} FAB {row.get('fab_bid_pct')}%"
            for row in body["board"][:5]
        ]
    if isinstance(body.get("player"), dict):  # /v1/player
        player = body["player"]
        weeks = player.get("recent_weeks") or []
        return [
            f"{player.get('name')} ({player.get('position')} {player.get('team')}), "
            f"{len(weeks)} recent weeks",
            f"usage: {player.get('usage_trajectory') or 'n/a'}",
        ]
    return []


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def run(
    base_url: str,
    asks: list[Ask],
    payer: Payer,
    *,
    timeout: float = 180.0,
    as_json: bool = False,
    log: Callable[[str], None] = print,
) -> list[AnalysisResult]:
    """Discover the catalog, then buy each ask in turn.

    One failed ask does not abandon the rest: an agent working a queue reports
    the failure and keeps going. The exit code reflects whether anything failed.

    Returns:
        The results that succeeded, in order.
    """
    results: list[AnalysisResult] = []
    failures = 0
    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout) as http:
        log(f"\n[1] discover  GET {base_url.rstrip('/')}/v1/catalog")
        catalog = await discover(http)
        print_catalog(catalog, log)

        prices = {entry.get("key"): entry.get("price_usdc") for entry in catalog["endpoints"]}
        for number, ask in enumerate(asks, start=1):
            listed = prices.get(ask.key)
            log(f"\n[{number + 1}] {ask.label or ask.key}  (catalog price {listed} USDC)")
            try:
                result = await ask_once(http, payer, ask, log=log)
            except AgentError as exc:
                failures += 1
                log(f"  FAILED    {type(exc).__name__}: {exc}")
                continue
            results.append(result)
            print_receipt(result, log)
            print_analysis(result, log)
            if as_json:
                log(json.dumps(result.body, indent=2))

    spent = sum(result.price for result in results if result.settled)
    unsettled = sum(1 for result in results if not result.settled)
    summary = (
        f"\ndone: {len(results)}/{len(asks)} answered, {spent:.6f} USDC spent, {failures} failed"
    )
    if unsettled:
        summary += f" ({unsettled} answered without a settled payment — not counted as spent)"
    log(summary)
    if failures:
        raise AgentError(f"{failures} of {len(asks)} asks failed")
    return results


def build_parser() -> argparse.ArgumentParser:
    """CLI surface. Defaults are the local mock-server demo."""
    parser = argparse.ArgumentParser(
        prog="fantasy_agent.py",
        description="Example agent: discover Play Clock, pay via x402, get analysis.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              %(prog)s --base-url http://localhost:8000 --mock
              %(prog)s --mock --ask "player:Bijan Robinson"
              %(prog)s --ask "matchup:Bijan Robinson,Breece Hall" --max-price 0.30
            """
        ),
    )
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="Service origin (default: %(default)s)."
    )
    parser.add_argument(
        "--ask",
        action="append",
        dest="asks",
        metavar="ASK",
        help=(
            "What to buy; repeatable. One of: "
            + ", ".join(sorted(SIMPLE_ASKS))
            + ", 'player:<name>', 'matchup:<name>,<name>'. Default: trending."
        ),
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=DEFAULT_MAX_PRICE_USDC,
        metavar="USDC",
        help="Refuse any quote above this, per call (default: %(default).2f USDC).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Pay with the mock signer (requires a server started with X402_MODE=mock).",
    )
    parser.add_argument(
        "--mnemonic-env",
        default=MNEMONIC_ENV,
        metavar="VAR",
        help="Env var holding the 25-word mnemonic (default: %(default)s).",
    )
    parser.add_argument(
        "--algod-url",
        default=None,
        help="Override the Algod endpoint used to build the payment (default: AlgoNode).",
    )
    parser.add_argument(
        "--allow-any-asset",
        action="store_true",
        help="Sign transfers of assets other than the network's canonical USDC.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="HTTP timeout in seconds. Must exceed the quote's maxTimeoutSeconds (120) "
        "— a client that gives up after paying is charged for an answer it never sees.",
    )
    parser.add_argument("--json", action="store_true", help="Also dump each raw response body.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code; never raises for expected failures."""
    args = build_parser().parse_args(argv)

    try:
        asks = [parse_ask(text) for text in (args.asks or ["trending"])]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        signer = build_signer(
            mock=args.mock,
            mnemonic_phrase=os.environ.get(args.mnemonic_env),
            algod_url=args.algod_url,
            allow_any_asset=args.allow_any_asset,
        )
    except WalletError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payer = Payer(signer=signer, max_price_usdc=args.max_price)
    print(f"playclock example agent -> {args.base_url}")
    print(f"paying with {payer.label}, ceiling {payer.max_price_usdc:.2f} USDC per call")

    try:
        asyncio.run(run(args.base_url, asks, payer, timeout=args.timeout, as_json=args.json))
    except AgentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
