"""x402 protocol shapes — SDK models re-exported, plus the thin shims we add.

This module is the **only** place that touches ``x402`` SDK internals for type
shapes, so an SDK bump is a one-directory change (DESIGN_NOTES §3). Everything
else in ``api/x402/`` (and the whole rest of the app) imports from here.

Protocol facts this build is pinned to (verified against ``x402-avm`` 2.0.2 —
GoPlausible's Algorand-capable fork of Coinbase's x402 SDK; import name is
``x402``):

* **Protocol version 2.** ``x402Version: 2`` everywhere. Wire encoding is
  camelCase (the SDK's :class:`~x402.schemas.base.BaseX402Model` sets a
  ``to_camel`` alias generator), so every dump uses ``by_alias=True``.
* **V2 header names.** The client sends ``PAYMENT-SIGNATURE``; a 402 carries
  ``PAYMENT-REQUIRED``; the settlement receipt comes back in
  ``PAYMENT-RESPONSE``. ``X-PAYMENT`` / ``X-PAYMENT-RESPONSE`` are V1 legacy.
  We **accept** both inbound names (clients lag) and **emit** the V2 names,
  mirroring the legacy response header for V1 clients, with both listed in
  ``Access-Control-Expose-Headers`` so browser wallets can read them.
* **Networks are CAIP-2.** ``algorand:<genesis-hash>``; USDC is an ASA id
  carried **as a string** with 6 decimals:

  =========  ========================================================  ==========
  network    CAIP-2                                                    USDC ASA
  =========  ========================================================  ==========
  mainnet    ``algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=``  31566704
  testnet    ``algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=``  10458941
  =========  ========================================================  ==========

* **``amount`` is a string of atomic units.** V1's ``maxAmountRequired`` was
  renamed to ``amount`` in V2. $0.10 -> ``"100000"``.
* **Challenge tag placement.** ``accepts[].extra.tag`` must equal
  ``settings.x402_challenge_tag`` (``"x402-global-challenge"``) on *every* paid
  route — that field is what the Algorand Global x402 Challenge leaderboard
  indexes. The AVM ``exact`` scheme *merges* into ``extra`` rather than
  replacing it, so we also carry ``{"name": "USDC", "decimals": 6}`` there.

Facilitator URL
---------------
The challenge requires the **GoPlausible** facilitator,
``https://facilitator.goplausible.xyz`` — see :data:`GOPLAUSIBLE_FACILITATOR_URL`.
The SDK's own default (``https://x402.org/facilitator``) is deliberately never
used: settling through it would forfeit leaderboard attribution. Deployments
set ``X402_FACILITATOR_URL``; ``infra/env.example`` (owned by another wave)
should document that value as the live-mode default.

Config gaps worked around here
------------------------------
``api.core.config.Settings`` has no public-base-URL field, but the facilitator
**permanently catalogs** the ``resource.url`` it sees when a payment settles, so
live mode must never advertise ``localhost``. :func:`resolve_resource_url` reads
the optional ``X402_RESOURCE_BASE_URL`` env var directly and
:func:`assert_public_resource_url` refuses to run live against a loopback host.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

from x402.extensions.bazaar import (
    BAZAAR,
    OutputConfig,
    declare_discovery_extension,
)
from x402.http.constants import (
    ACCESS_CONTROL_EXPOSE_HEADERS,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X_PAYMENT_HEADER,
    X_PAYMENT_RESPONSE_HEADER,
)
from x402.http.utils import (
    encode_payment_response_header,
    safe_base64_decode,
    safe_base64_encode,
)
from x402.mechanisms.avm.constants import (
    ALGORAND_MAINNET_CAIP2,
    ALGORAND_TESTNET_CAIP2,
    DEFAULT_DECIMALS,
    SCHEME_EXACT,
    USDC_MAINNET_ASA_ID,
    USDC_TESTNET_ASA_ID,
)
from x402.mechanisms.avm.utils import to_atomic_amount
from x402.schemas import (
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    SettleResponse,
    VerifyResponse,
)
from x402.schemas.payments import ResourceInfo

from api.core.config import Settings

__all__ = [
    # SDK types used as-is
    "PaymentPayload",
    "PaymentRequired",
    "PaymentRequirements",
    "ResourceInfo",
    "SettleResponse",
    "SettleResult",
    "VerifyResponse",
    "VerifyResult",
    "X402_VERSION",
    # Bazaar
    "BAZAAR",
    "OutputConfig",
    "declare_discovery_extension",
    # Header names
    "ACCESS_CONTROL_EXPOSE_HEADERS",
    "EXPOSED_HEADERS",
    "PAYMENT_REQUIRED_HEADER",
    "PAYMENT_RESPONSE_HEADER",
    "PAYMENT_SIGNATURE_HEADER",
    "X_PAYMENT_HEADER",
    "X_PAYMENT_RESPONSE_HEADER",
    # Chain constants
    "ALGORAND_MAINNET_CAIP2",
    "ALGORAND_TESTNET_CAIP2",
    "GOPLAUSIBLE_FACILITATOR_URL",
    "USDC_DECIMALS",
    "USDC_MAINNET_ASA_ID",
    "USDC_TESTNET_ASA_ID",
    # Errors / helpers
    "X402ConfigError",
    "atomic_amount",
    "build_payment_required",
    "build_payment_requirements",
    "decode_payment_header",
    "network_caip2",
    "payment_hash",
    "payment_payload_from_header",
    "payment_required_headers",
    "resolve_resource_url",
    "settlement_headers",
    "usdc_asset_id",
]

#: The challenge-mandated facilitator. Never the SDK's ``x402.org`` default:
#: settlements have to flow through GoPlausible to be attributed to our entry.
GOPLAUSIBLE_FACILITATOR_URL = "https://facilitator.goplausible.xyz"

#: USDC has 6 decimals on both Algorand networks.
USDC_DECIMALS = DEFAULT_DECIMALS

#: Default validity window advertised in ``accepts[].maxTimeoutSeconds``.
DEFAULT_MAX_TIMEOUT_SECONDS = 120

#: Response headers browser clients must be able to read cross-origin.
EXPOSED_HEADERS = ", ".join(
    (PAYMENT_REQUIRED_HEADER, PAYMENT_RESPONSE_HEADER, X_PAYMENT_RESPONSE_HEADER)
)

#: Magic raw header value accepted in ``X402_MODE=mock`` (browser/laptop testing).
MOCK_PAYMENT_HEADER = "mock-paid"

#: Marker key inside a decoded payment payload that ``MockFacilitatorClient`` honours.
MOCK_MARKER_KEY = "mock"

#: Stand-in ``payTo`` used when ``X402_PAY_TO`` is unset outside live mode, so a
#: dev/mock 402 body is still shape-valid. Live mode refuses to run without a
#: real address instead of falling back to this.
PLACEHOLDER_PAY_TO = ("PLAYCLOCK" * 7)[:58]

#: Hosts that must never appear in a live-mode ``resource.url`` — the facilitator
#: catalogs whatever it sees, permanently.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "testserver", "test"})

#: Optional env override for the public origin advertised to the Bazaar. Read
#: directly (not via ``Settings``) because ``api/core/config.py`` is owned by
#: another wave; see the module docstring.
RESOURCE_BASE_URL_ENV = "X402_RESOURCE_BASE_URL"

#: Names the brief asks for; the SDK models already fit, so these are aliases
#: rather than wrappers.
VerifyResult = VerifyResponse
SettleResult = SettleResponse


class X402ConfigError(RuntimeError):
    """Payment configuration is unusable — refuse to serve rather than mis-bill."""


# ---------------------------------------------------------------------------
# Network / asset / amount resolution
# ---------------------------------------------------------------------------


def network_caip2(settings: Settings) -> str:
    """Return the CAIP-2 network id for ``settings.x402_network``."""
    return ALGORAND_MAINNET_CAIP2 if settings.x402_network == "mainnet" else ALGORAND_TESTNET_CAIP2


def usdc_asset_id(settings: Settings) -> str:
    """Return the USDC ASA id (as the wire's string form) for the active network.

    ``settings.x402_asset_id`` wins when set; ``0`` (the config default) falls
    back to the SDK's per-network USDC constant so local dev works unconfigured.
    """
    if settings.x402_asset_id:
        return str(settings.x402_asset_id)
    return str(USDC_MAINNET_ASA_ID if settings.x402_network == "mainnet" else USDC_TESTNET_ASA_ID)


def atomic_amount(price_usdc: float) -> str:
    """Convert a USDC price to the wire's atomic-unit string. ``0.10 -> "100000"``."""
    return str(to_atomic_amount(price_usdc, USDC_DECIMALS))


def pay_to_address(settings: Settings) -> str:
    """Return the configured ``payTo`` address.

    Raises:
        X402ConfigError: In live mode with ``X402_PAY_TO`` unset — we would be
            advertising payments to nowhere.
    """
    if settings.x402_pay_to:
        return settings.x402_pay_to
    if settings.x402_mode == "live":
        raise X402ConfigError("X402_PAY_TO must be set when X402_MODE=live")
    return PLACEHOLDER_PAY_TO


# ---------------------------------------------------------------------------
# Payment requirements / 402 body
# ---------------------------------------------------------------------------


def build_payment_requirements(endpoint_key: str, settings: Settings) -> PaymentRequirements:
    """Build the single :class:`PaymentRequirements` entry for ``endpoint_key``.

    ``extra`` carries the challenge tag (leaderboard attribution) alongside the
    USDC display metadata the AVM scheme expects.
    """
    return PaymentRequirements(
        scheme=SCHEME_EXACT,
        network=network_caip2(settings),
        asset=usdc_asset_id(settings),
        amount=atomic_amount(settings.price_for(endpoint_key)),
        pay_to=pay_to_address(settings),
        max_timeout_seconds=DEFAULT_MAX_TIMEOUT_SECONDS,
        extra={
            "name": "USDC",
            "decimals": USDC_DECIMALS,
            "tag": settings.x402_challenge_tag,
        },
    )


def build_payment_required(
    *,
    requirements: PaymentRequirements,
    resource_url: str,
    description: str,
    extensions: dict[str, Any] | None = None,
    error: str | None = None,
) -> PaymentRequired:
    """Assemble the V2 402 body."""
    return PaymentRequired(
        x402_version=X402_VERSION,
        error=error,
        resource=ResourceInfo(
            url=resource_url,
            description=description,
            mime_type="application/json",
        ),
        accepts=[requirements],
        extensions=extensions,
    )


def payment_required_body(
    payment_required: PaymentRequired, *, method: str | None = None
) -> dict[str, Any]:
    """Render a :class:`PaymentRequired` as the camelCase JSON body of a 402.

    ``method`` is injected here rather than carried on the model because the
    SDK's :class:`ResourceInfo` has exactly three fields (``url``,
    ``description``, ``mimeType``) and pydantic revalidates a subclass back down
    to it, silently dropping anything extra.

    The Bazaar needs it. Every one of the 1,792 resources catalogued on
    2026-09-01 carried a ``method``, and a resource's id there is
    ``base64("GET:https://host/path")`` — method plus URL *is* the primary key.
    Play Clock sells ten endpoints across GET and POST, so without it the
    catalogue cannot tell them apart, and we were the one challenge-tagged
    merchant with a settled payment and no listing (DESIGN_NOTES §21).
    """
    body = payment_required.model_dump(by_alias=True, exclude_none=True)
    resource = body.get("resource")
    if method and isinstance(resource, dict):
        resource["method"] = method
    return body


def payment_required_headers(
    payment_required: PaymentRequired, *, method: str | None = None
) -> dict[str, str]:
    """Headers accompanying a 402: the V2 ``PAYMENT-REQUIRED`` blob + CORS exposure.

    Encoded from :func:`payment_required_body` rather than from the model, so
    the header and the body can never disagree about what was offered — the
    header is what a client that ignores the body signs against.
    """
    return {
        PAYMENT_REQUIRED_HEADER: safe_base64_encode(
            json.dumps(
                payment_required_body(payment_required, method=method), separators=(",", ":")
            )
        ),
        ACCESS_CONTROL_EXPOSE_HEADERS: EXPOSED_HEADERS,
    }


def settlement_headers(settle: SettleResponse) -> dict[str, str]:
    """Headers carrying a settlement receipt on a paid 2xx response.

    Emits the V2 ``PAYMENT-RESPONSE`` and mirrors it into the V1 legacy name so
    clients that have not moved yet still get their receipt.
    """
    encoded = encode_payment_response_header(settle)
    return {
        PAYMENT_RESPONSE_HEADER: encoded,
        X_PAYMENT_RESPONSE_HEADER: encoded,
        ACCESS_CONTROL_EXPOSE_HEADERS: EXPOSED_HEADERS,
    }


# ---------------------------------------------------------------------------
# Inbound payment header decoding
# ---------------------------------------------------------------------------


def payment_hash(raw_header: str) -> str:
    """SHA-256 of the raw payment header — the idempotency key and receipt id."""
    return hashlib.sha256(raw_header.strip().encode("utf-8")).hexdigest()


_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/]")


def lenient_b64decode(text: str) -> bytes | None:
    """Decode base64 the way the facilitator does, or ``None``.

    The SDK's facilitator (``decode_base64_transaction``) and Node's ``Buffer``
    both silently discard characters outside the alphabet, so ``"ab!cd"`` and
    ``"abcd"`` are the same transaction to them. Anything that keys or gates
    on a transaction must decode it the same way, or a stray character buys a
    second identity (and a second answer) for one payment.
    """
    cleaned = _NOT_BASE64.sub("", text.translate(str.maketrans("-_", "+/")))
    try:
        return base64.b64decode(cleaned + "=" * (-len(cleaned) % 4))
    except (binascii.Error, ValueError):
        return None


def payment_transaction(payload: PaymentPayload) -> bytes | None:
    """Raw msgpack bytes of the transaction that moves the money, or ``None``."""
    inner = payload.payload if isinstance(payload.payload, dict) else {}
    group = inner.get("paymentGroup")
    if not isinstance(group, list) or not group:
        return None
    index = inner.get("paymentIndex", 0)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(group):
        return None
    entry = group[index]
    return lenient_b64decode(entry) if isinstance(entry, str) else None


def payment_group_transactions(payload: PaymentPayload) -> list[bytes]:
    """Raw msgpack bytes of every transaction in the payment group, in order.

    Empty when the payload has no readable payment transaction at all (see
    :func:`payment_transaction`); entries that are not base64 strings are skipped.
    """
    if payment_transaction(payload) is None:
        return []
    group = payload.payload["paymentGroup"]
    decoded = (lenient_b64decode(entry) for entry in group if isinstance(entry, str))
    return [raw for raw in decoded if raw is not None]


def _transaction_id(raw: bytes) -> str | None:
    """Algorand txid of a (signed or unsigned) msgpack transaction, or ``None``."""
    try:
        from algosdk import encoding

        decoded = encoding.msgpack_decode(base64.b64encode(raw).decode("ascii"))
        txid = decoded.get_txid()
    except Exception:  # noqa: BLE001 - an undecodable txn is verify's to reject
        return None
    return txid if isinstance(txid, str) and txid else None


def payment_identity(payload: PaymentPayload) -> str | None:
    """SHA-256 of the transaction that moves the money, or ``None``.

    The raw header is not an identity: the same signed payment re-encoded
    (URL-safe base64, no padding, stray non-alphabet characters, re-indented
    JSON, raw JSON, non-canonical msgpack) hashes differently, and each
    encoding would verify, run the handler and be answered before the
    duplicate settle fails. The Algorand txid is what the ledger itself
    refuses to accept twice, so it is the one thing no re-encoding can change;
    bytes that are not a transaction at all (the mock marker's placeholder,
    which no facilitator would verify) hash as decoded. ``None`` when there is
    no payment group, and live mode refuses such a payload rather than falling
    back to the header.
    """
    raw = payment_transaction(payload)
    if raw is None:
        return None
    txid = _transaction_id(raw)
    return hashlib.sha256(txid.encode("ascii") if txid else raw).hexdigest()


def decode_payment_header(raw_header: str) -> dict[str, Any] | None:
    """Decode a base64 payment header into its JSON dict, or ``None`` if malformed.

    Tolerates both standard and URL-safe base64 and missing ``=`` padding, which
    real-world wallets get wrong often enough to be worth handling.
    """
    candidate = raw_header.strip()
    if not candidate:
        return None
    padded = candidate + "=" * (-len(candidate) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            text = decoder(padded.encode("utf-8")).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    # Some clients send the JSON unencoded; accept that too rather than 402ing.
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def payment_payload_from_header(
    raw_header: str, requirements: PaymentRequirements
) -> PaymentPayload | None:
    """Turn a raw payment header into a V2 :class:`PaymentPayload`.

    Normalisations applied, all of them client-lag tolerance rather than
    protocol changes:

    * the mock magic value :data:`MOCK_PAYMENT_HEADER` becomes a payload whose
      ``payload`` carries the mock marker (only ``MockFacilitatorClient`` will
      accept it — live mode still rejects it at verify);
    * a payload without ``accepted`` (V1-shaped, or a terse client) is bound to
      the requirements we advertised;
    * ``x402Version`` is forced to 2, since that is the shape we send onward.

    Returns:
        The payload, or ``None`` when the header could not be decoded at all.
    """
    if raw_header.strip() == MOCK_PAYMENT_HEADER:
        return PaymentPayload(
            x402_version=X402_VERSION,
            payload={MOCK_MARKER_KEY: True},
            accepted=requirements,
        )

    data = decode_payment_header(raw_header)
    if data is None:
        return None

    inner = data.get("payload")
    if not isinstance(inner, dict):
        return None

    accepted = data.get("accepted")
    try:
        accepted_model = (
            PaymentRequirements.model_validate(accepted)
            if isinstance(accepted, dict)
            else requirements
        )
    except ValueError:
        accepted_model = requirements

    extensions = data.get("extensions")
    resource = data.get("resource")
    try:
        return PaymentPayload(
            x402_version=X402_VERSION,
            payload=inner,
            accepted=accepted_model,
            resource=ResourceInfo.model_validate(resource) if isinstance(resource, dict) else None,
            extensions=extensions if isinstance(extensions, dict) else None,
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Resource URL (what the facilitator permanently catalogs)
# ---------------------------------------------------------------------------


def resolve_resource_url(request_url: str, path: str, query: str) -> str:
    """Return the canonical public URL to advertise for this resource.

    ``X402_RESOURCE_BASE_URL`` wins when set (Cloud Run behind a custom domain);
    otherwise the request's own URL is used.
    """
    base = os.environ.get(RESOURCE_BASE_URL_ENV, "").strip()
    if not base:
        return request_url
    url = f"{base.rstrip('/')}{path}"
    return f"{url}?{query}" if query else url


def assert_public_resource_url(url: str, settings: Settings) -> None:
    """Refuse to advertise a loopback or plain-http resource URL in live mode.

    The facilitator catalogs ``resource.url`` **permanently** when a payment
    settles, so a stray ``localhost`` entry in the Bazaar is not recoverable.
    Nor is an ``http://`` one: behind Cloud Run's TLS-terminating proxy the
    request URL reads ``http`` unless ``X402_RESOURCE_BASE_URL`` is set.

    Raises:
        X402ConfigError: When live mode resolved a local/unset host or a
            scheme other than ``https``.
    """
    if settings.x402_mode != "live":
        return
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or host in _LOCAL_HOSTS or parts.scheme.lower() != "https":
        raise X402ConfigError(
            f"refusing to advertise resource url {url!r} in live mode: "
            f"set {RESOURCE_BASE_URL_ENV} to the public https origin "
            "(the facilitator catalogs this url permanently)"
        )


def decode_settlement_header(value: str) -> SettleResponse:
    """Decode a ``PAYMENT-RESPONSE`` header back into a :class:`SettleResponse`.

    Used by tests and by any client-side code we ship; kept here so the base64
    convention lives in exactly one place.
    """
    return SettleResponse.model_validate_json(safe_base64_decode(value))
