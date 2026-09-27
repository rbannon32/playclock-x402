"""Facilitator clients — the one seam that moves when the x402 ecosystem does.

``verify`` and ``settle`` are the only two calls Play Clock makes against
the payment network, and they are the fastest-moving part of the stack, so they
live behind :class:`FacilitatorClient` (DESIGN_NOTES §3). Three implementations,
selected by ``X402_MODE``:

============  ==========================================================
``disabled``  Payments are bypassed entirely; the dependency never calls a
              facilitator. :func:`get_facilitator` still returns a
              :class:`MockFacilitatorClient` so callers never handle ``None``.
``mock``      :class:`MockFacilitatorClient` — accepts a magic marker, mints a
              deterministic fake txid. Laptop and browser testing, CI.
``live``      :class:`HttpFacilitatorClient` — talks HTTP to the GoPlausible
              facilitator (``X402_FACILITATOR_URL``).
============  ==========================================================

Facilitator URL policy
----------------------
Live mode targets **GoPlausible**
(:data:`~api.x402.schemas_compat.GOPLAUSIBLE_FACILITATOR_URL`,
``https://facilitator.goplausible.xyz``) — that is what the Algorand Global x402
Challenge requires, and settling anywhere else forfeits leaderboard attribution.
The SDK's own default (``https://x402.org/facilitator``) is never used: we always
pass an explicit ``url`` into :class:`~x402.http.FacilitatorConfig`.

Wire contract (unchanged from the SDK, which we delegate to)::

    POST {base}/verify   {"x402Version": 2, "paymentPayload": ..., "paymentRequirements": ...}
      -> {"isValid": bool, "invalidReason": str?, "invalidMessage": str?, "payer": str?}
    POST {base}/settle   (same request body)
      -> {"success": bool, "errorReason": str?, "errorMessage": str?,
          "payer": str?, "transaction": str, "network": str}

Failure posture: transport errors and non-200s never escape as exceptions.
``verify`` degrades to an invalid result whose reason is
:data:`FACILITATOR_UNAVAILABLE` when the facilitator could not answer (the
dependency turns that into a ``503`` — a 402 would tell a generic x402 client
to sign and pay *again*), and ``settle`` degrades to ``success=false`` (the
caller keeps their answer and we log to ``failed_paid_calls/`` — DESIGN_NOTES
§2 ordering rule).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from typing import Any, Protocol

import httpx

from api.core.config import Settings, get_settings
from api.x402.schemas_compat import (
    BAZAAR,
    GOPLAUSIBLE_FACILITATOR_URL,
    MOCK_MARKER_KEY,
    PaymentPayload,
    PaymentRequirements,
    SettleResult,
    VerifyResult,
    X402ConfigError,
)

__all__ = [
    "EXTENSION_RESPONSES_HEADER",
    "FACILITATOR_UNAVAILABLE",
    "FacilitatorClient",
    "HttpFacilitatorClient",
    "MockFacilitatorClient",
    "MOCK_PAYER_ADDRESS",
    "close_facilitator",
    "decode_extension_responses",
    "facilitator_base_url",
    "get_facilitator",
    "log_extension_responses",
    "set_facilitator",
]

logger = logging.getLogger(__name__)

#: Request timeout for facilitator calls. Settlement is on-chain; 15s is
#: generous for Algorand's ~3s finality but still well inside our p95 budget.
FACILITATOR_TIMEOUT_SECONDS = 15.0

#: Deterministic payer address reported by :class:`MockFacilitatorClient`.
#: Shaped like an Algorand address (58 chars, base32 alphabet) so downstream
#: code, receipts and UIs exercise realistic values.
MOCK_PAYER_ADDRESS = ("MOCKPAYER" * 7)[:58]

#: Facilitator -> resource-server sidechannel on ``verify``/``settle``: base64
#: JSON keyed by extension name. The x402 spec marks it **server internal only,
#: never forwarded to the buyer**, and it is the one direct answer to "did this
#: payment actually get catalogued?".
#:
#: It is the instrument DESIGN_NOTES §21 did not have. Twelve MainNet payments
#: settled and catalogued nothing, and the only way to learn that was to go
#: reading the public catalogue a week later. ``bazaar.status`` says it on the
#: settle itself. The spec says facilitators *may* send it and ``x402-avm``
#: 2.0.2 has no code for it at all, so this reads the raw header: an absent
#: header is silence, not success, and is logged as such at debug level only.
EXTENSION_RESPONSES_HEADER = "EXTENSION-RESPONSES"

#: ``invalid_reason`` of a verify the facilitator never answered: a transport
#: error, a timeout, a 5xx or a 429. It says nothing about the payment, so the
#: dependency answers ``503`` (not charged, retry the same header) rather than 402.
FACILITATOR_UNAVAILABLE = "facilitator_unavailable"

#: The SDK's non-200 error reads ``"Facilitator verify failed (503): ..."``.
_SDK_STATUS = re.compile(r"failed \((\d{3})\)")


def _facilitator_could_not_answer(exc: Exception) -> bool:
    """True unless ``exc`` is the facilitator deliberately refusing (a 4xx).

    The SDK raises ``ValueError`` on *any* non-200, so a 400 about this payload
    and a 503 about the facilitator arrive as the same type. Only the second is
    an outage; a 4xx is an answer about the payment and stays a 402.
    """
    match = _SDK_STATUS.search(str(exc)) if isinstance(exc, ValueError) else None
    if match is None:
        return True
    status = int(match.group(1))
    return status >= 500 or status in (408, 429)


#: ``bazaar.status`` values, from the spec's sidechannel table.
_BAZAAR_OK = frozenset({"success", "processing"})


def decode_extension_responses(value: str) -> dict[str, Any]:
    """Decode an ``EXTENSION-RESPONSES`` header into ``{extension: response}``.

    Tolerant by design: this is diagnostic telemetry riding on a ``MAY`` clause,
    so a malformed or truncated header returns ``{}`` rather than costing us a
    settlement we have already made.
    """
    try:
        decoded = json.loads(base64.b64decode(value))
    except Exception:  # noqa: BLE001 - telemetry must never break settlement
        logger.debug("undecodable %s header: %r", EXTENSION_RESPONSES_HEADER, value[:80])
        return {}
    return decoded if isinstance(decoded, dict) else {}


def log_extension_responses(headers: Any, *, call: str) -> dict[str, Any]:
    """Log the facilitator's extension sidechannel for one ``verify``/``settle``.

    A ``rejected`` Bazaar status is logged at **error** level with the reason:
    the payment still settled and the caller still has their answer, but the
    endpoint did not get catalogued, which is a silent failure everywhere else.
    """
    value = headers.get(EXTENSION_RESPONSES_HEADER) or headers.get(
        EXTENSION_RESPONSES_HEADER.lower()
    )
    if not value:
        return {}
    responses = decode_extension_responses(value)
    bazaar = responses.get(BAZAAR)
    if isinstance(bazaar, dict):
        status = str(bazaar.get("status", "")).lower()
        if status == "rejected":
            logger.error(
                "bazaar discovery REJECTED on %s — endpoint not catalogued: %s",
                call,
                bazaar.get("rejectedReason") or "no reason given",
            )
        elif status in _BAZAAR_OK:
            logger.info("bazaar discovery %s on %s", status, call)
        else:
            logger.warning("bazaar discovery returned unknown status %r on %s", status, call)
    return responses


class FacilitatorClient(Protocol):
    """The two calls the payment layer makes against the x402 network."""

    async def verify(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        """Check that ``payment_payload`` satisfies ``requirements``, without settling."""
        ...

    async def settle(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        """Broadcast the payment and return the on-chain receipt."""
        ...


def facilitator_base_url(settings: Settings) -> str:
    """Return the facilitator base URL, defaulting to GoPlausible.

    ``X402_FACILITATOR_URL`` defaults to ``""`` in :mod:`api.core.config`, so an
    unconfigured deployment would otherwise fall through to the SDK default and
    silently lose challenge attribution. We substitute GoPlausible instead.
    """
    return (settings.x402_facilitator_url or GOPLAUSIBLE_FACILITATOR_URL).rstrip("/")


class HttpFacilitatorClient:
    """:class:`FacilitatorClient` over HTTP, wrapping the SDK's async client.

    The SDK's :class:`~x402.http.HTTPFacilitatorClient` already takes an explicit
    base URL, a timeout and an injectable ``httpx.AsyncClient`` via
    :class:`~x402.http.FacilitatorConfig`, and it builds exactly the request
    bodies the facilitator expects — so this is a thin wrapper, not a
    reimplementation. What it adds is the two things the SDK deliberately leaves
    to the caller: a **default URL that is GoPlausible rather than x402.org**,
    and **error containment** (the SDK raises ``ValueError`` on any non-200).

    Args:
        base_url: Facilitator base URL. Defaults to GoPlausible.
        timeout: Request timeout in seconds.
        transport: ``httpx`` transport to inject (tests use
            ``httpx.MockTransport``); ignored when ``http_client`` is given.
        http_client: A fully built ``httpx.AsyncClient`` to reuse.
        auth_provider: Optional SDK auth provider for facilitators that require
            signed headers.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = FACILITATOR_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
        auth_provider: Any | None = None,
    ) -> None:
        from x402.http import FacilitatorConfig, HTTPFacilitatorClient

        self.base_url = (base_url or GOPLAUSIBLE_FACILITATOR_URL).rstrip("/")
        self._timeout = timeout
        # We always own the httpx client now, because the extension sidechannel
        # (:data:`EXTENSION_RESPONSES_HEADER`) is a *response header* and the SDK
        # surfaces only the parsed body. An event hook is the one seam that sees
        # it without reimplementing verify/settle.
        self._owns_client = http_client is None
        if http_client is None:
            http_client = httpx.AsyncClient(transport=transport, timeout=timeout)
        http_client.event_hooks["response"] = [
            *http_client.event_hooks.get("response", []),
            self._on_response,
        ]
        self._client = HTTPFacilitatorClient(
            FacilitatorConfig(
                url=self.base_url,
                timeout=timeout,
                http_client=http_client,
                auth_provider=auth_provider,
                identifier="playclock",
            )
        )

    async def _on_response(self, response: httpx.Response) -> None:
        """Read the facilitator's extension sidechannel off every response.

        Headers are available before the body is streamed, so this never reads
        or consumes the response. Raising here would turn a settled payment into
        a transport error, so it cannot.
        """
        try:
            path = response.request.url.path.rsplit("/", 1)[-1] or "facilitator"
            log_extension_responses(response.headers, call=path)
        except Exception as exc:  # noqa: BLE001 - telemetry is never load-bearing
            logger.debug("extension sidechannel logging failed: %s", exc)

    async def verify(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        """Verify via ``POST {base}/verify``; failures read as invalid, never raise.

        An outage is reported as :data:`FACILITATOR_UNAVAILABLE`; a 4xx from the
        facilitator is its verdict on the payment and reads as rejected.
        """
        try:
            return await self._client.verify(payment_payload, requirements)
        except Exception as exc:  # noqa: BLE001 - any failure must become a 402/503
            logger.warning("facilitator verify failed (%s): %s", self.base_url, exc)
            return VerifyResult(
                is_valid=False,
                invalid_reason=(
                    FACILITATOR_UNAVAILABLE
                    if _facilitator_could_not_answer(exc)
                    else "facilitator_rejected_payment"
                ),
                invalid_message=str(exc),
            )

    async def settle(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        """Settle via ``POST {base}/settle``; failures return ``success=False``.

        Never raises: by the time settlement runs the handler has already
        produced the caller's answer, and losing that answer would be worse for
        us than losing the payment (DESIGN_NOTES §2).
        """
        try:
            return await self._client.settle(payment_payload, requirements)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.warning("facilitator settle failed (%s): %s", self.base_url, exc)
            return SettleResult(
                success=False,
                error_reason=FACILITATOR_UNAVAILABLE,
                error_message=str(exc),
                transaction="",
                network=requirements.network,
            )

    async def aclose(self) -> None:
        """Close the underlying HTTP client when we created it."""
        if self._owns_client:
            await self._client.aclose()


class MockFacilitatorClient:
    """Offline :class:`FacilitatorClient` for ``X402_MODE=mock``, CI and tests.

    A payment is accepted when its decoded payload carries the mock marker —
    ``payload.payload["mock"] is True``, which is also what the magic raw header
    ``mock-paid`` normalises to (see
    :func:`~api.x402.schemas_compat.payment_payload_from_header`). Anything else
    is rejected exactly the way a real facilitator rejects a bad signature, so
    the 402-on-invalid path is exercised without a chain.

    Settlement mints a deterministic pseudo-txid derived from the payload, so the
    same payment always yields the same receipt and tests can assert on it.

    Args:
        settle_error: When set, every settlement fails with this ``errorReason``
            (drives the settle-failure path in tests and manual QA).

    Attributes:
        verify_calls: Payloads passed to :meth:`verify`, in order.
        settle_calls: Payloads passed to :meth:`settle`, in order.
    """

    def __init__(self, *, settle_error: str | None = None) -> None:
        self.settle_error = settle_error
        self.verify_calls: list[PaymentPayload] = []
        self.settle_calls: list[PaymentPayload] = []

    @staticmethod
    def _has_marker(payment_payload: PaymentPayload) -> bool:
        return payment_payload.payload.get(MOCK_MARKER_KEY) is True

    @staticmethod
    def _payer(payment_payload: PaymentPayload) -> str:
        payer = payment_payload.payload.get("payer")
        return payer if isinstance(payer, str) and payer else MOCK_PAYER_ADDRESS

    @staticmethod
    def fake_txid(payment_payload: PaymentPayload) -> str:
        """Deterministic 52-character base32 pseudo-txid for ``payment_payload``."""
        digest = hashlib.sha256(
            payment_payload.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        ).digest()
        return base64.b32encode(digest).decode("ascii").rstrip("=")[:52]

    async def verify(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> VerifyResult:
        """Accept payloads carrying the mock marker; reject everything else."""
        self.verify_calls.append(payment_payload)
        if not self._has_marker(payment_payload):
            return VerifyResult(
                is_valid=False,
                invalid_reason="mock_marker_missing",
                invalid_message=(
                    "X402_MODE=mock accepts only a mock payment: send the raw header "
                    "'mock-paid', or a payload whose payload object contains {\"mock\": true}."
                ),
            )
        if payment_payload.accepted.amount != requirements.amount:
            return VerifyResult(
                is_valid=False,
                invalid_reason="amount_mismatch",
                invalid_message=(
                    f"payment is for {payment_payload.accepted.amount}, "
                    f"resource costs {requirements.amount}"
                ),
            )
        return VerifyResult(is_valid=True, payer=self._payer(payment_payload))

    async def settle(
        self, payment_payload: PaymentPayload, requirements: PaymentRequirements
    ) -> SettleResult:
        """Mint a deterministic receipt, or fail when ``settle_error`` is set."""
        self.settle_calls.append(payment_payload)
        if self.settle_error is not None:
            return SettleResult(
                success=False,
                error_reason=self.settle_error,
                error_message=f"mock settlement failed: {self.settle_error}",
                payer=self._payer(payment_payload),
                transaction="",
                network=requirements.network,
            )
        return SettleResult(
            success=True,
            payer=self._payer(payment_payload),
            transaction=self.fake_txid(payment_payload),
            network=requirements.network,
        )


# ---------------------------------------------------------------------------
# Mode-driven factory (mirrors api.core.store.get_store / set_store)
# ---------------------------------------------------------------------------

_facilitator_override: FacilitatorClient | None = None
_default_facilitator: FacilitatorClient | None = None
_default_key: tuple[str, str, str] | None = None


def get_facilitator(settings: Settings | None = None) -> FacilitatorClient:
    """Return the process-wide :class:`FacilitatorClient`.

    Honours :func:`set_facilitator` overrides first (tests / DI), otherwise
    builds the client named by ``Settings.x402_mode`` and caches it until the
    mode, network or facilitator URL changes.

    ``disabled`` mode returns a :class:`MockFacilitatorClient`: the payment
    dependency short-circuits long before it would be used, and returning a real
    object keeps every caller free of ``None`` handling.
    """
    if _facilitator_override is not None:
        return _facilitator_override

    settings = settings or get_settings()
    key = (settings.x402_mode, settings.x402_network, facilitator_base_url(settings))

    global _default_facilitator, _default_key
    if _default_facilitator is None or _default_key != key:
        _default_facilitator = _build_facilitator(settings)
        _default_key = key
    return _default_facilitator


def _build_facilitator(settings: Settings) -> FacilitatorClient:
    """Construct the client for ``settings.x402_mode``."""
    if settings.x402_mode == "live":
        base_url = facilitator_base_url(settings)
        if not base_url.startswith("https://"):
            raise X402ConfigError(
                f"X402_FACILITATOR_URL must be https in live mode, got {base_url!r}"
            )
        return HttpFacilitatorClient(base_url)
    return MockFacilitatorClient()


async def close_facilitator() -> None:
    """Close the settings-built client, if one was built; for app shutdown.

    An override installed with :func:`set_facilitator` belongs to whoever
    installed it and is left alone. Safe to call more than once.
    """
    global _default_facilitator, _default_key
    client, _default_facilitator, _default_key = _default_facilitator, None, None
    aclose = getattr(client, "aclose", None)
    if aclose is not None:
        await aclose()


def set_facilitator(client: FacilitatorClient | None) -> None:
    """Override the client returned by :func:`get_facilitator`.

    Pass ``None`` to clear the override and fall back to settings-driven
    construction. Intended for tests and dependency injection; mirrors
    :func:`api.core.store.set_store`.
    """
    global _facilitator_override, _default_facilitator, _default_key
    _facilitator_override = client
    if client is None:
        _default_facilitator = None
        _default_key = None
