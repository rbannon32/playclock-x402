"""x402 payment layer — the challenge-critical part (tech spec §3).

Public surface, in the order a route author meets it::

    from api.x402 import PaymentContext, paid_router, require_payment

    router = paid_router(prefix="/v1")

    @router.get("/trending", response_model=TrendingResponse)
    async def trending(payment: PaymentContext = Depends(require_payment("trending"))):
        return await build_trending()

Three modes, from ``X402_MODE``: ``disabled`` (no payment at all), ``mock`` (a
magic ``PAYMENT-SIGNATURE: mock-paid`` header, no chain) and ``live`` (the
GoPlausible facilitator on Algorand TestNet or MainNet). Verify runs in the
dependency, settlement runs in :class:`~api.x402.middleware.PaidRoute` after a
2xx, and every settled payment lands in ``receipts/``.

Module map
----------
``schemas_compat``  SDK types + wire helpers; the only place ``x402`` internals
                    are touched, so an SDK bump is a one-directory change.
``endpoints``       Per-endpoint description, example request/response and the
                    Bazaar discovery block advertised in each 402.
``facilitator``     ``FacilitatorClient`` protocol, HTTP and mock clients, and
                    the mode-driven ``get_facilitator`` / ``set_facilitator``.
``middleware``      ``require_payment`` dependency, ``PaidRoute``/``paid_router``.
``receipts``        ``receipts/`` and ``failed_paid_calls/`` writers, plus the
                    aggregation behind the free ``/v1/stats``.
"""

from __future__ import annotations

from api.x402.endpoints import ENDPOINT_SPECS, EndpointSpec, bazaar_extensions, spec_for
from api.x402.facilitator import (
    FacilitatorClient,
    HttpFacilitatorClient,
    MockFacilitatorClient,
    get_facilitator,
    set_facilitator,
)
from api.x402.middleware import (
    IDEMPOTENCY_TTL_SECONDS,
    PaidRoute,
    PaymentContext,
    PaymentRequiredError,
    clear_idempotency_cache,
    install_x402_handlers,
    paid_router,
    require_payment,
    settle_payment,
)
from api.x402.receipts import (
    FAILED_PAID_CALLS_COLLECTION,
    RECEIPTS_COLLECTION,
    log_failed_paid_call,
    log_receipt,
    receipts_summary,
)
from api.x402.schemas_compat import (
    GOPLAUSIBLE_FACILITATOR_URL,
    MOCK_PAYMENT_HEADER,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
    PaymentRequired,
    PaymentRequirements,
    SettleResult,
    VerifyResult,
    X402ConfigError,
    build_payment_requirements,
    network_caip2,
    usdc_asset_id,
)

__all__ = [
    # Route wiring
    "PaidRoute",
    "PaymentContext",
    "PaymentRequiredError",
    "install_x402_handlers",
    "paid_router",
    "require_payment",
    "settle_payment",
    # Endpoint metadata
    "ENDPOINT_SPECS",
    "EndpointSpec",
    "bazaar_extensions",
    "spec_for",
    # Facilitator
    "FacilitatorClient",
    "HttpFacilitatorClient",
    "MockFacilitatorClient",
    "GOPLAUSIBLE_FACILITATOR_URL",
    "get_facilitator",
    "set_facilitator",
    # Receipts
    "FAILED_PAID_CALLS_COLLECTION",
    "RECEIPTS_COLLECTION",
    "log_failed_paid_call",
    "log_receipt",
    "receipts_summary",
    # Protocol shapes and constants
    "IDEMPOTENCY_TTL_SECONDS",
    "MOCK_PAYMENT_HEADER",
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "PAYMENT_SIGNATURE_HEADER",
    "PaymentRequired",
    "PaymentRequirements",
    "SettleResult",
    "VerifyResult",
    "X402ConfigError",
    "X_PAYMENT_HEADER",
    "X_PAYMENT_RESPONSE_HEADER",
    "build_payment_requirements",
    "clear_idempotency_cache",
    "network_caip2",
    "usdc_asset_id",
]
