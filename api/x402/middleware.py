"""Payment gating: a route dependency plus a settle-after-handler route class.

Why a dependency and not ASGI middleware
----------------------------------------
The tech spec asks for a route-level allowlist, "not path-prefix magic" (§3), so
paid routes opt in explicitly and free routes simply do not (DESIGN_NOTES §2)::

    from api.x402 import PaymentContext, paid_router, require_payment

    router = paid_router(prefix="/v1")

    @router.get("/trending", response_model=TrendingResponse)
    async def trending(payment: PaymentContext = Depends(require_payment("trending"))):
        ...

Ordering: **verify -> handler -> settle** (DESIGN_NOTES §2). A handler that fails
after a successful verify settles nothing, so the caller is not charged for an
answer they never got. A settlement that fails after a successful handler still
returns the answer and writes ``failed_paid_calls/`` — we ate one LLM call, which
is the cheaper direction to be wrong in.

The settle-after-handler mechanism
----------------------------------
FastAPI dependencies cannot attach a response header after the handler has run —
``yield``-dependency teardown happens once the response is already on its way out
— so settlement lives in :class:`PaidRoute`, an ``APIRoute`` subclass that wraps
the compiled route handler:

#. the dependency verifies and stashes a :class:`PaymentContext` on
   ``request.state.payment``;
#. the handler runs;
#. :class:`PaidRoute` inspects the returned ``Response`` and settles **only** on
   a 2xx, then attaches ``PAYMENT-RESPONSE`` (plus the ``X-PAYMENT-RESPONSE``
   legacy mirror and ``Access-Control-Expose-Headers``);
#. an exception from the handler propagates untouched — nothing settles.

:class:`PaidRoute` also renders :class:`PaymentRequiredError` into a proper V2
402 body, so the 402 payload sits at the root of the response rather than under
FastAPI's ``{"detail": ...}`` envelope. Use :func:`paid_router` to get a router
with the route class already set; :func:`install_x402_handlers` registers the
same rendering app-wide as a safety net for anything mounted another way.

Idempotency
-----------
Agents retry. A verified payment is claimed atomically in the configured
:class:`~api.core.store.Store` for :data:`IDEMPOTENCY_TTL_SECONDS` (300s), keyed
by the SHA-256 of the signed payment transaction (:func:`payment_identity`),
not of the header: a re-encoded header is the same payment. A replay skips
re-verification and re-serves the original settlement receipt instead of
settling twice. The key deliberately omits the endpoint, so one payment sent to
two same-priced routes meets its own record on the second and is refused as a
different request (below); a cheaper payment sent fresh to a dearer route fails
verify on amount. Only the mock marker, which has no transaction, is keyed per
endpoint on the raw header; the literal ``mock-paid`` also folds in the request
fingerprint, since every caller sends the same one. The cache
uses create-if-absent and revision compare-and-swap operations, so concurrent
requests across Cloud Run instances cannot both run the handler or settle.

A replay re-runs the handler (only verify and settle are skipped), so each
record counts its replays and refuses past :data:`MAX_REPLAYS` with
``payment_replay_limit_reached``: otherwise one payment and fifty concurrent
retries buy fifty-one generations. The count is taken with the same
compare-and-swap, so racing replays cannot all read the same number.

A remembered payment is also bound to the *request* it paid for, by the
:func:`_request_fingerprint` of method + path + query + body. Skipping
verification and settlement is only a retry when the question is the same one;
with a different body or query it would be a free second analysis, so one
``POST /v1/player`` payment could otherwise fetch a different player every time
for the whole window. A payment replayed against a different request is rejected with
a ``402`` **before the handler runs**, so nothing is generated and nothing is
settled twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from api.core.config import Settings, get_settings
from api.core.store import Store, get_store
from api.x402.endpoints import EndpointSpec, payment_extensions, spec_for
from api.x402.facilitator import FACILITATOR_UNAVAILABLE, FacilitatorClient, get_facilitator
from api.x402.receipts import log_failed_paid_call, log_receipt
from api.x402.schemas_compat import (
    MOCK_PAYMENT_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X_PAYMENT_HEADER,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResult,
    assert_public_resource_url,
    build_payment_required,
    build_payment_requirements,
    payment_hash,
    payment_identity,
    payment_payload_from_header,
    payment_required_body,
    payment_required_headers,
    resolve_resource_url,
    settlement_headers,
)
from api.x402.validity import EXPIRES_TOO_SOON, expires_too_soon

__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "MAX_REPLAYS",
    "PAYMENT_IDEMPOTENCY_COLLECTION",
    "PaidRoute",
    "PaymentContext",
    "PaymentRequiredError",
    "build_402",
    "clear_idempotency_cache",
    "install_x402_handlers",
    "paid_router",
    "payment_required_response",
    "require_payment",
    "settle_payment",
]

logger = logging.getLogger(__name__)

#: How long a settlement outcome is remembered so retries do not double-settle.
#: The clock starts when the outcome replaces the verification claim, after the
#: handler and settlement finish. While the handler runs, ``_CLAIM_LEASE_SECONDS``
#: prevents another request from using the same payment concurrently.
IDEMPOTENCY_TTL_SECONDS = 300.0

#: How many times one settled payment may be replayed. Each replay re-runs the
#: handler, so without a bound one payment buys as many generations as a
#: client can send inside the window. Five covers any honest retry loop.
MAX_REPLAYS = 5

#: Error string on the 402 past :data:`MAX_REPLAYS`. It deliberately says
#: nothing about charges: the payment settled on its first use.
REPLAY_LIMIT_REACHED = "payment_replay_limit_reached"

#: Compare-and-swap attempts at counting one replay before asking the client
#: to retry; only concurrent replays of the same payment ever contend.
_REPLAY_COUNT_ATTEMPTS = 5

#: ``Retry-After`` on the 503 sent when the facilitator cannot verify.
FACILITATOR_RETRY_AFTER_SECONDS = 30

#: Body of that 503. The payment was verified by no one and submitted nowhere.
FACILITATOR_UNAVAILABLE_DETAIL = (
    "The payment facilitator is temporarily unavailable, so this payment could "
    "not be verified. You were not charged. Retry shortly with the same "
    "PAYMENT-SIGNATURE; do not sign a new payment."
)


@dataclass
class PaymentContext:
    """What the payment layer learned about this request.

    Handlers receive one from ``Depends(require_payment(...))`` and may read it
    freely (e.g. to tag a response with the payer). In ``disabled`` mode it is a
    stub with ``paid=False`` so handlers never need a mode check.

    Attributes:
        paid: Whether a payment was verified for this request.
        mode: Active ``X402_MODE``.
        endpoint_key: Endpoint key this payment was priced against.
        payer: Payer address reported by the facilitator, when known.
        amount_usdc: Price in USDC.
        amount_atomic: Same price in atomic units, as sent on the wire.
        network: CAIP-2 network.
        asset: USDC ASA id, as a string.
        payment_hash: SHA-256 of the signed payment transaction (of the raw
            header for the mock marker, which carries none, plus the request
            fingerprint for the literal ``mock-paid``).
        request_fingerprint: SHA-256 of the request this payment bought (see
            :func:`_request_fingerprint`); a replay carrying a different one is
            a different purchase, not a retry.
        replayed: True when this payment was already settled inside the
            idempotency window and the cached receipt is being re-served
            (at most :data:`MAX_REPLAYS` times per payment).
        settle: The settlement receipt, available only after :class:`PaidRoute`
            has settled (or immediately, on a replay).
    """

    paid: bool
    mode: str
    endpoint_key: str = ""
    payer: str | None = None
    amount_usdc: float = 0.0
    amount_atomic: str = "0"
    network: str = ""
    asset: str = ""
    payment_hash: str = ""
    request_fingerprint: str = ""
    replayed: bool = False
    settle: SettleResult | None = None

    # --- internal plumbing, carried so PaidRoute can settle without re-deriving ---
    requirements: PaymentRequirements | None = field(default=None, repr=False)
    payload: PaymentPayload | None = field(default=None, repr=False)
    facilitator: Any = field(default=None, repr=False)
    cache_key: str = field(default="", repr=False)
    claim_revision: str = field(default="", repr=False)
    #: True for the literal ``mock-paid`` header, which every caller shares:
    #: there is no payment behind it to amplify, so its replays are uncounted.
    mock_marker: bool = field(default=False, repr=False)
    #: The live record this claim replaced (a ``failed`` settle bound to the
    #: request that was answered), restored if the claim is abandoned.
    superseded_record: dict[str, Any] | None = field(default=None, repr=False)
    idempotency_store: Store | None = field(default=None, repr=False)


class PaymentRequiredError(HTTPException):
    """Raised by the dependency to abort with a V2 ``402 Payment Required``.

    Subclasses :class:`fastapi.HTTPException` so that an app which forgot to use
    :func:`paid_router` still returns 402 (with the payload nested under
    ``detail``) rather than a 500. :class:`PaidRoute` and
    :func:`install_x402_handlers` render it properly, at the root of the body.
    """

    def __init__(self, payment_required: PaymentRequired, *, method: str | None = None) -> None:
        self.payment_required = payment_required
        #: HTTP method of the paid route. Carried alongside rather than on the
        #: model because the SDK's ResourceInfo drops unknown fields; the Bazaar
        #: keys resources on method + URL, so a 402 without it is not listable.
        self.method = method
        super().__init__(
            status_code=402,
            detail=payment_required_body(payment_required, method=method),
            headers=payment_required_headers(payment_required, method=method),
        )


def payment_required_response(error: PaymentRequiredError) -> JSONResponse:
    """Render a :class:`PaymentRequiredError` as the V2 402 response."""
    return JSONResponse(
        status_code=402,
        content=payment_required_body(error.payment_required, method=error.method),
        headers=payment_required_headers(error.payment_required, method=error.method),
    )


# ---------------------------------------------------------------------------
# Durable idempotency claims
# ---------------------------------------------------------------------------

PAYMENT_IDEMPOTENCY_COLLECTION = "payment_idempotency"
# A claim has to outlive the longest allowed request. Settled receipts retain
# the public retry window above; claims get extra room so a request at the
# timeout boundary cannot be stolen while its handler is still finishing.
_CLAIM_LEASE_SECONDS = IDEMPOTENCY_TTL_SECONDS + 60.0


def clear_idempotency_cache() -> None:
    """Backward-compatible test hook; durable records live in the active Store."""


def _record_is_live(record: dict[str, Any]) -> bool:
    try:
        return float(record.get("expires_at", 0)) > time.time()
    except (TypeError, ValueError):
        return False


def _expiry(seconds: float) -> dict[str, Any]:
    """``expires_at`` fields for a record that lives ``seconds`` from now.

    ``expires_at`` (epoch float) is what :func:`_record_is_live` reads.
    ``expires_at_ts`` is the same instant as a timezone-aware datetime, which
    Firestore stores as a Timestamp: the only field type a Firestore TTL policy
    deletes on (infra/deploy.md). Without it every record lives forever.
    """
    expires_at = time.time() + seconds
    return {
        "expires_at": expires_at,
        "expires_at_ts": datetime.fromtimestamp(expires_at, tz=UTC),
    }


def _replay_count(record: dict[str, Any]) -> int:
    try:
        return max(int(record.get("replays") or 0), 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# 402 construction
# ---------------------------------------------------------------------------


def _resource_url(request: Request, settings: Settings) -> str:
    """Resolve, and sanity-check, the URL advertised as ``resource.url``."""
    url = resolve_resource_url(str(request.url), request.url.path, request.url.query)
    assert_public_resource_url(url, settings)
    return url


def build_402(
    request: Request,
    spec: EndpointSpec,
    settings: Settings,
    requirements: PaymentRequirements,
    *,
    error: str | None,
) -> PaymentRequiredError:
    """Build the ``402`` for ``spec``: requirements, resource info, discovery + merchant."""
    return PaymentRequiredError(
        build_payment_required(
            requirements=requirements,
            resource_url=_resource_url(request, settings),
            description=spec.description,
            extensions=payment_extensions(spec, settings),
            error=error,
        ),
        method=spec.method,
    )


def _read_payment_header(request: Request) -> str | None:
    """Return the inbound payment header, accepting the V2 and V1 legacy names."""
    raw = request.headers.get(PAYMENT_SIGNATURE_HEADER) or request.headers.get(X_PAYMENT_HEADER)
    raw = (raw or "").strip()
    return raw or None


#: Methods whose body is part of the fingerprint. ``GET``/``HEAD``/``OPTIONS``
#: carry none, and reading one would only wait on an empty receive channel.
_BODY_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _canonical_body(body: bytes) -> bytes:
    """``body`` re-serialized as canonical JSON, or unchanged when it is not JSON."""
    if not body:
        return body
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return body
    return json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode("utf-8")


async def _request_fingerprint(request: Request) -> str:
    """SHA-256 of everything that makes this a *different* paid request.

    Method, path, query and body: two requests sharing a fingerprint ask the
    same question, so re-serving one payment's receipt for the other is a retry.
    Query parameters are sorted by key so ``?a=1&b=2`` and ``?b=2&a=1`` are one request.

    Reading the body here is safe: Starlette caches it on the request, and
    FastAPI has already read it for any route with a body parameter, so this
    neither consumes the stream nor re-reads the socket.

    A JSON body is hashed in canonical form (sorted keys, no whitespace): the
    handler sees the parsed object, so a client that re-serializes the same
    question with its keys in another order is retrying, not buying a second
    answer, and must not be 402'd on the paid retry. Anything that does not
    parse as JSON is hashed as sent.
    """
    body = await request.body() if request.method.upper() in _BODY_METHODS else b""
    body = _canonical_body(body)
    # Sorted by key only: the stable sort keeps a repeated key's values in
    # order, since FastAPI answers with the last one (``?week=3&week=4`` is not
    # ``?week=4&week=3``). JSON, because decoded values may contain ``&`` or ``=``.
    query = json.dumps(sorted(request.query_params.multi_items(), key=lambda item: item[0]))
    material = "\n".join(
        [
            request.method.upper(),
            request.url.path,
            query,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The dependency
# ---------------------------------------------------------------------------


def require_payment(
    endpoint_key: str,
    *,
    settings: Settings | None = None,
) -> Callable[[Request], Awaitable[PaymentContext]]:
    """Build the payment dependency for one paid endpoint.

    Args:
        endpoint_key: One of :data:`api.core.config.ENDPOINT_KEYS`. Determines
            the price, the description and the Bazaar discovery block.
        settings: Settings to use instead of the cached global. Tests and any
            caller doing dependency injection pass this; production omits it.

    Returns:
        An async FastAPI dependency yielding a :class:`PaymentContext`.

    Raises:
        KeyError: At *construction* time if ``endpoint_key`` is unknown — a typo
            fails at import, not on the first paying customer.

    The returned dependency raises :class:`PaymentRequiredError` (402) when
    payment is missing or invalid, and :class:`~api.x402.schemas_compat.X402ConfigError`
    (500) when live mode is misconfigured. It never settles; see
    :class:`PaidRoute`.
    """
    spec = spec_for(endpoint_key)

    async def dependency(request: Request) -> PaymentContext:
        active = settings or get_settings()

        if active.x402_mode == "disabled":
            context = PaymentContext(paid=False, mode="disabled", endpoint_key=endpoint_key)
            request.state.payment = context
            return context

        requirements = build_payment_requirements(endpoint_key, active)
        raw_header = _read_payment_header(request)
        if raw_header is None:
            raise build_402(request, spec, active, requirements, error="payment_required")

        payload = payment_payload_from_header(raw_header, requirements)
        if payload is None:
            raise build_402(request, spec, active, requirements, error="invalid_payment_payload")

        # One signed transaction is one purchase, on any endpoint and in any
        # encoding: keyed on it, a re-encoded header or the same header sent to
        # another route at the same price finds this record and is refused as
        # a different request, instead of verifying again (the transaction is
        # not on chain until settle) and being answered before settle fails.
        # Only the mock marker, which has no transaction, keys on the header
        # per endpoint.
        identity = payment_identity(payload)
        if identity is None and active.x402_mode == "live":
            # A live payment with no readable payment transaction would key on
            # the header, per endpoint, which reopens both holes above; the SDK
            # coerces nothing, so e.g. ``paymentIndex: true`` still verifies.
            raise build_402(request, spec, active, requirements, error="invalid_payment_payload")
        fingerprint = await _request_fingerprint(request)
        # The literal ``mock-paid`` is one header shared by every caller, so
        # keyed on the header alone the first question asked on an endpoint
        # would 402 every *other* question on it, for everyone, for the whole
        # window. Binding it to the request makes each question its own record.
        mock_marker = identity is None and raw_header == MOCK_PAYMENT_HEADER
        if mock_marker:
            digest = payment_hash(f"{raw_header}\n{fingerprint}")
        else:
            digest = identity or payment_hash(raw_header)
        cache_key = f"txn:{digest}" if identity else f"{endpoint_key}:{digest}"

        # What the facilitator catalogues is read off this payload (DESIGN_NOTES
        # §25), so it carries our resource and discovery block, never the
        # client's echo of them. An honest client echoes these unchanged; any
        # other client could otherwise list a URL of its choosing under our
        # merchant, permanently.
        advertised = build_402(request, spec, active, requirements, error=None).payment_required
        payload = payload.model_copy(
            update={"resource": advertised.resource, "extensions": advertised.extensions}
        )
        context = PaymentContext(
            paid=True,
            mode=active.x402_mode,
            endpoint_key=endpoint_key,
            amount_usdc=active.price_for(endpoint_key),
            amount_atomic=requirements.amount,
            network=requirements.network,
            asset=requirements.asset,
            payment_hash=digest,
            request_fingerprint=fingerprint,
            requirements=requirements,
            cache_key=cache_key,
            mock_marker=mock_marker,
        )
        context.payload = payload

        store = get_store(active)
        context.idempotency_store = store

        def in_progress() -> PaymentRequiredError:
            return build_402(
                request,
                spec,
                active,
                requirements,
                error="payment_in_progress_retry_shortly",
            )

        async def count_replay(record: dict[str, Any]) -> bool:
            """Spend one of the record's replays; ``False`` if another request raced us."""
            used = _replay_count(record)
            if used >= MAX_REPLAYS:
                logger.warning(
                    "refusing %s replay: %d already served for one payment",
                    endpoint_key,
                    used,
                )
                raise build_402(request, spec, active, requirements, error=REPLAY_LIMIT_REACHED)
            counted = {k: v for k, v in record.items() if k != "_id"}
            counted.update(replays=used + 1, revision=uuid.uuid4().hex)
            return await store.replace_if_revision(
                PAYMENT_IDEMPOTENCY_COLLECTION,
                cache_key,
                str(record.get("revision") or ""),
                counted,
            )

        async def replay_or_reject(record: dict[str, Any] | None) -> PaymentContext | None:
            for _ in range(_REPLAY_COUNT_ATTEMPTS):
                if record is None or not _record_is_live(record):
                    return None
                if record.get("request_fingerprint") != fingerprint:
                    # One payment, one analysis. Re-serving the receipt here would
                    # hand out a *second* answer to a different question for free,
                    # so reject before the handler generates anything.
                    logger.warning(
                        "rejecting replayed %s payment against a different request", endpoint_key
                    )
                    raise build_402(
                        request,
                        spec,
                        active,
                        requirements,
                        error="payment_already_used_for_a_different_request",
                    )

                if record.get("status") == "settled" and isinstance(record.get("settle"), dict):
                    if not mock_marker and not await count_replay(record):
                        # A concurrent replay of the same payment moved the
                        # revision; re-read and count against what it wrote.
                        record = await store.get(PAYMENT_IDEMPOTENCY_COLLECTION, cache_key)
                        continue
                    context.payer = record.get("payer")
                    context.replayed = True
                    context.settle = SettleResult.model_validate(record["settle"])
                    request.state.payment = context
                    return context
                if record.get("status") == "claimed":
                    # Another request/instance owns this payment. Never let a
                    # second handler run while its settlement outcome is unknown.
                    raise in_progress()
                context.payer = record.get("payer")
                return None
            raise in_progress()

        record = await store.get(PAYMENT_IDEMPOTENCY_COLLECTION, cache_key)
        replay = await replay_or_reject(record)
        if replay is not None:
            return replay

        if await expires_too_soon(payload, requirements.network, active):
            # Would lapse before a slow handler settles it: the answer would be
            # served and the settle would fail, on purpose (see validity.py).
            raise build_402(request, spec, active, requirements, error=EXPIRES_TOO_SOON)

        facilitator = get_facilitator(active)
        context.facilitator = facilitator
        result = await facilitator.verify(payload, requirements)
        if not result.is_valid and result.invalid_reason == FACILITATOR_UNAVAILABLE:
            # Nothing is known about the payment, so a 402 would be a lie that
            # generic x402 clients act on by signing and paying again. The
            # transfer was never submitted: say so, and ask for the same header.
            raise HTTPException(
                status_code=503,
                detail=FACILITATOR_UNAVAILABLE_DETAIL,
                headers={"Retry-After": str(FACILITATOR_RETRY_AFTER_SECONDS)},
            )
        if not result.is_valid:
            raise build_402(
                request,
                spec,
                active,
                requirements,
                error=result.invalid_reason or "invalid_payment",
            )

        context.payer = result.payer
        revision = uuid.uuid4().hex
        claim = {
            "revision": revision,
            "status": "claimed",
            "request_fingerprint": fingerprint,
            "payer": result.payer,
            **_expiry(_CLAIM_LEASE_SECONDS),
        }
        if record is None:
            claimed = await store.create(PAYMENT_IDEMPOTENCY_COLLECTION, cache_key, claim)
        else:
            claimed = await store.replace_if_revision(
                PAYMENT_IDEMPOTENCY_COLLECTION,
                cache_key,
                str(record.get("revision") or ""),
                claim,
            )

        if not claimed:
            # A competing instance won after our read. Its durable state is now
            # authoritative, even though both requests may have verified.
            winner = await store.get(PAYMENT_IDEMPOTENCY_COLLECTION, cache_key)
            replay = await replay_or_reject(winner)
            if replay is not None:
                return replay
            raise in_progress()

        context.claim_revision = revision
        if record is not None and _record_is_live(record):
            context.superseded_record = record
        request.state.payment = context
        return context

    dependency.__name__ = f"require_payment_{endpoint_key}"
    dependency.__doc__ = f"Verify an x402 payment for the {endpoint_key!r} endpoint."
    return dependency


# ---------------------------------------------------------------------------
# Settlement (runs after the handler, only on 2xx)
# ---------------------------------------------------------------------------


async def settle_payment(
    context: PaymentContext,
    response: Response,
    *,
    store: Store | None = None,
    settings: Settings | None = None,
) -> SettleResult | None:
    """Settle ``context`` and attach the receipt headers to ``response``.

    Called by :class:`PaidRoute` after a 2xx. Exposed so a route that cannot use
    the route class (a mounted sub-app, a hand-built ``Response``) can do the
    same thing explicitly.

    Never raises: a settlement problem is logged to ``failed_paid_calls/`` and
    the caller keeps the answer they already paid nothing for.

    ``settings`` is only consulted when ``context`` carries no facilitator or
    store of its own, i.e. when it was not built by :func:`require_payment`.

    Returns:
        The settlement receipt, or ``None`` when there was nothing to settle.
    """
    if not context.paid or context.payload is None or context.requirements is None:
        return None

    if context.replayed and context.settle is not None:
        response.headers.update(settlement_headers(context.settle))
        return context.settle

    facilitator: FacilitatorClient = context.facilitator or get_facilitator(settings)
    try:
        settle = await facilitator.settle(context.payload, context.requirements)
    except Exception as exc:  # noqa: BLE001 - the answer is already generated
        logger.exception("settle raised for %s", context.endpoint_key)
        settle = SettleResult(
            success=False,
            error_reason="settle_exception",
            error_message=str(exc),
            payer=context.payer,
            transaction="",
            network=context.network,
        )

    context.settle = settle
    context.payer = settle.payer or context.payer
    response.headers.update(settlement_headers(settle))

    # The claim lives in the dependency's store; the outcome must land beside it.
    active_store = store or context.idempotency_store or get_store(settings)
    try:
        await _persist_settlement(context, settle, active_store)
    except Exception:  # noqa: BLE001 - the payer's money has moved; the answer must still ship
        # A transient store error here must not turn a settled payment into a
        # 500: the USDC is gone, and after the claim lease the transaction is
        # on chain, so re-verify fails and the answer is unrecoverable.
        logger.critical(
            "settled=%s for %s but could not record it",
            settle.success,
            context.endpoint_key,
            exc_info=True,
        )
    return settle


async def _persist_settlement(
    context: PaymentContext, settle: SettleResult, active_store: Store
) -> None:
    """Record the settle outcome: the idempotency record plus a receipt or failure row.

    The two writes are independent. A store error on the idempotency swap
    must not also lose the receipt: ``receipts/`` is what ``/v1/stats`` and the
    books are built from, and ``failed_paid_calls/`` is the only trace of a
    loss.
    """
    if settle.success:
        await _record_outcome(
            context,
            {
                "revision": uuid.uuid4().hex,
                "status": "settled",
                "request_fingerprint": context.request_fingerprint,
                "payer": context.payer,
                **_expiry(IDEMPOTENCY_TTL_SECONDS),
                "settle": settle.model_dump(by_alias=True, mode="json"),
            },
            active_store,
        )
        await log_receipt(
            active_store,
            txid=settle.transaction,
            payer=context.payer,
            endpoint=context.endpoint_key,
            amount_usdc=context.amount_usdc,
            network=context.network,
            payment_hash=context.payment_hash,
        )
    else:
        # Live for the window, bound to this request: the same request may
        # try to settle again, but a different one must not reuse a payment
        # whose first answer has already been served.
        await _record_outcome(
            context,
            {
                "revision": uuid.uuid4().hex,
                "status": "failed",
                "request_fingerprint": context.request_fingerprint,
                "payer": context.payer,
                **_expiry(IDEMPOTENCY_TTL_SECONDS),
            },
            active_store,
        )
        detail = settle.error_message or settle.error_reason or "settlement failed"
        logger.error("settlement failed for %s: %s", context.endpoint_key, detail)
        await log_failed_paid_call(
            active_store,
            endpoint=context.endpoint_key,
            payment_hash=context.payment_hash,
            error=f"{settle.error_reason or 'settle_failed'}: {detail}",
            payer=context.payer,
            amount_usdc=context.amount_usdc,
            network=context.network,
        )


async def _record_outcome(
    context: PaymentContext, record: dict[str, Any], active_store: Store
) -> None:
    """Swap the claim for its settle outcome. Never raises.

    On failure the claim stays ``claimed`` until its lease ends, so a retry
    inside that lease is told to retry shortly rather than served; that is the
    cost of a store outage, and it must not also cost the receipt.
    """
    try:
        persisted = await active_store.replace_if_revision(
            PAYMENT_IDEMPOTENCY_COLLECTION,
            context.cache_key,
            context.claim_revision,
            record,
        )
    except Exception:  # noqa: BLE001 - the receipt/failure row must still be written
        logger.critical(
            "%s %s but could not record its idempotency outcome",
            record["status"],
            context.endpoint_key,
            exc_info=True,
        )
        return
    if not persisted:
        logger.critical(
            "%s %s but could not persist its idempotency outcome",
            record["status"],
            context.endpoint_key,
        )


async def _abandon_payment(context: PaymentContext) -> None:
    """Release a claim when the handler produced no billable response.

    A claim that replaced a live record puts that record back rather than
    deleting it. The only live record a claim can replace is a ``failed``
    settle, bound to the request whose answer was already served; deleting it
    would free the payment to verify again and buy a *different* request.
    """
    if not context.claim_revision or not context.cache_key:
        return
    store = context.idempotency_store or get_store()
    restored = None
    if context.superseded_record is not None:
        # A fresh revision, so a reader still holding the old one re-reads.
        restored = {**context.superseded_record, "revision": uuid.uuid4().hex}
    try:
        await store.replace_if_revision(
            PAYMENT_IDEMPOTENCY_COLLECTION,
            context.cache_key,
            context.claim_revision,
            restored,
        )
    except Exception:
        # The caller's real outcome (often a 503 "you were not charged") must
        # reach them; an unreleased claim only delays a retry until its lease ends.
        logger.exception("failed to release payment claim %s", context.cache_key)


#: Replaces a handler's refund wording when the payment being replayed settled.
REPLAY_FAILED_NOTE = (
    "This payment already settled on its first use. Retry with the same "
    "PAYMENT-SIGNATURE (not a new payment) within the replay window to receive "
    "the answer without being charged again."
)


_NOT_CHARGED_RE = re.compile(r"\s*You were not charged[.;,]?", re.IGNORECASE)


def _replayed_failure(error: HTTPException) -> HTTPException:
    detail = error.detail
    if isinstance(detail, str):
        detail = _NOT_CHARGED_RE.sub("", detail).strip()
        detail = f"{detail} {REPLAY_FAILED_NOTE}".strip()
    return HTTPException(status_code=error.status_code, detail=detail, headers=error.headers)


class PaidRoute(APIRoute):
    """``APIRoute`` that settles a verified payment after a successful handler.

    Wraps the compiled route handler so it can see both the request (for the
    :class:`PaymentContext` the dependency stashed) and the finished response
    (to attach ``PAYMENT-RESPONSE``). Also renders :class:`PaymentRequiredError`
    into a root-level V2 402 body.

    Safe on free routes: with no :class:`PaymentContext` on the request it is a
    passthrough, so a router may mix paid and free endpoints.
    """

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def paid_route_handler(request: Request) -> Response:
            try:
                response = await original(request)
            except PaymentRequiredError as error:
                return payment_required_response(error)
            except HTTPException as error:
                context = getattr(request.state, "payment", None)
                if isinstance(context, PaymentContext) and context.replayed:
                    # A replay re-runs the handler on a payment that already
                    # settled. Its "you were not charged" is false here, and a
                    # client that believes it discards the one header that can
                    # still recover the answer.
                    raise _replayed_failure(error) from error
                if isinstance(context, PaymentContext) and context.paid:
                    await _abandon_payment(context)
                raise
            except BaseException as error:
                context = getattr(request.state, "payment", None)
                if (
                    isinstance(error, Exception)
                    and isinstance(context, PaymentContext)
                    and context.replayed
                ):
                    # An unhandled crash on a replay: the bare 500 would read as
                    # an ordinary failure, and the client would drop the header.
                    logger.exception("replayed %s handler raised", context.endpoint_key)
                    raise HTTPException(
                        status_code=500, detail=f"Analysis failed. {REPLAY_FAILED_NOTE}"
                    ) from error
                if isinstance(context, PaymentContext) and context.paid:
                    await _abandon_payment(context)
                raise

            context = getattr(request.state, "payment", None)
            if isinstance(context, PaymentContext) and context.paid:
                if 200 <= response.status_code < 300:
                    await settle_payment(context, response)
                else:
                    await _abandon_payment(context)
                    logger.info(
                        "not settling %s: handler returned %s",
                        context.endpoint_key,
                        response.status_code,
                    )
            return response

        return paid_route_handler


def paid_router(**kwargs: Any) -> APIRouter:
    """Return an ``APIRouter`` whose routes settle payments (``route_class=PaidRoute``).

    Every paid route must be registered on one of these: a bare ``APIRouter``
    still 402s (the dependency raises) but never settles. ``include_router``
    preserves the route class, so nothing else in ``main.py`` has to know. Any
    ``APIRouter`` keyword argument is forwarded.
    """
    kwargs.setdefault("route_class", PaidRoute)
    return APIRouter(**kwargs)


def install_x402_handlers(app: FastAPI) -> None:
    """Register app-wide rendering of :class:`PaymentRequiredError`.

    Belt-and-braces for paid routes that were not built with :func:`paid_router`:
    without it such a route still returns 402, but with the payload nested under
    FastAPI's ``{"detail": ...}`` envelope instead of at the root where x402
    clients look for it. One line in ``main.py``.
    """

    async def handler(_request: Request, exc: Exception) -> Response:
        if not isinstance(exc, PaymentRequiredError):  # pragma: no cover - registration contract
            raise exc
        return payment_required_response(exc)

    app.add_exception_handler(PaymentRequiredError, handler)
