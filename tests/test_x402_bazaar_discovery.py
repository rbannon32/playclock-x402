"""The Bazaar cataloguing gate, reproduced in-process.

Play Clock settled twelve MainNet payments and catalogued none of them
(DESIGN_NOTES §21), and nothing in the codebase could have caught it: the
facilitator validates our discovery block against *our own* schema, logs a
warning to its own stderr on failure, and settles the payment anyway. From here
that is indistinguishable from success.

So these tests run the real gate. :func:`x402.extensions.bazaar.extract_discovery_info`
is the facilitator-side reference implementation shipped in the SDK we already
depend on, and it is exactly what decides whether a row appears in
``/discovery/resources``. Feeding it the envelope a paying client actually sends
turns a silent delisting into a red test, with no network and no credentials.

The three traps below are each a way to settle a payment and never be listed,
all of them silent in production:

``method`` absent
    Validates cleanly and is catalogued under ``base64("UNKNOWN:<url>")``,
    because ``_get_method_from_info`` returns the literal string ``"UNKNOWN"``.
    This was §21.
a stray key in ``info.input``
    The generated schema sets ``additionalProperties: false``, so *any* extra
    field — including spec-legal ``headers`` — fails validation.
``method`` contradicting the variant
    Raises inside ``parse_discovery_extension``; ``extract_discovery_info``
    swallows it in a bare ``except`` and returns ``None``.
"""

from __future__ import annotations

import base64
import copy
import json
from typing import Any

import pytest
from x402.extensions.bazaar import extract_discovery_info, validate_discovery_extension
from x402.extensions.bazaar.types import parse_discovery_extension

from api.core.config import Settings
from api.x402.endpoints import (
    ENDPOINT_SPECS,
    MERCHANT,
    EndpointSpec,
    bazaar_extensions,
    merchant_extension,
    payment_extensions,
)
from api.x402.facilitator import (
    EXTENSION_RESPONSES_HEADER,
    decode_extension_responses,
    log_extension_responses,
)
from api.x402.schemas_compat import (
    BAZAAR,
    build_payment_required,
    build_payment_requirements,
    payment_required_body,
)

RESOURCE_HOST = "https://api.playclock.xyz"


@pytest.fixture
def settings() -> Settings:
    """Live MainNet settings — the configuration that actually gets catalogued."""
    return Settings(
        x402_mode="live",
        x402_network="mainnet",
        x402_pay_to="M" * 58,
        x402_facilitator_url="https://facilitator.goplausible.xyz",
    )


def _402_body(spec: EndpointSpec, settings: Settings, *, extensions: Any = None) -> dict[str, Any]:
    """Render the exact 402 body this endpoint serves."""
    payment_required = build_payment_required(
        requirements=build_payment_requirements(spec.key, settings),
        resource_url=f"{RESOURCE_HOST}{spec.path}",
        description=spec.description,
        extensions=payment_extensions(spec, settings) if extensions is None else extensions,
    )
    return payment_required_body(payment_required, method=spec.method)


def _client_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """The ``PaymentPayload`` a paying client sends back.

    Mirrors ``buildPaymentHeader`` in ``web/js/wallet/envelope.js`` and the SDK
    client's ``_create_payment_payload_v2_core``: both echo ``resource`` and
    ``extensions`` from the 402 unmodified. That echo *is* the cataloguing
    mechanism — the facilitator reads the payload, never our 402.
    """
    return {
        "x402Version": body["x402Version"],
        "accepted": body["accepts"][0],
        "resource": body.get("resource"),
        "extensions": body.get("extensions"),
        "payload": {"transaction": "signed-txn-bytes"},
    }


def _catalogued(body: dict[str, Any]) -> Any:
    """Run the facilitator's own extraction over the client's envelope."""
    return extract_discovery_info(_client_envelope(body), body["accepts"][0])


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(ENDPOINT_SPECS))
def test_every_endpoint_survives_the_facilitators_discovery_gate(key: str, settings: Settings):
    """Each paid endpoint is catalogued, under its own method and URL."""
    spec = ENDPOINT_SPECS[key]
    body = _402_body(spec, settings)

    found = _catalogued(body)

    assert found is not None, f"{key} would settle and never be catalogued"
    assert found.method == spec.method
    assert found.resource_url == f"{RESOURCE_HOST}{spec.path}"
    assert found.x402_version == 2
    # `description` and `mimeType` are read off `payload.resource`, so they are
    # the only prose an agent browsing the catalogue sees.
    assert found.description == spec.description
    assert found.mime_type == "application/json"


def test_endpoints_get_distinct_catalogue_ids(settings: Settings):
    """A catalogue id is ``base64("METHOD:URL")`` — method is half the key.

    Ten endpoints across GET and POST must produce ten rows, not fewer.
    """
    ids = set()
    for spec in ENDPOINT_SPECS.values():
        found = _catalogued(_402_body(spec, settings))
        ids.add(base64.b64encode(f"{found.method}:{found.resource_url}".encode()).decode())
    assert len(ids) == len(ENDPOINT_SPECS)


@pytest.mark.parametrize("key", sorted(ENDPOINT_SPECS))
def test_discovery_block_validates_against_its_own_schema(key: str):
    """The facilitator validates ``info`` against the ``schema`` we ship beside it."""
    extension = parse_discovery_extension(bazaar_extensions(ENDPOINT_SPECS[key])[BAZAAR])
    result = validate_discovery_extension(extension)
    assert result.valid, result.errors


@pytest.mark.parametrize("key", sorted(ENDPOINT_SPECS))
def test_method_is_declared_and_required(key: str):
    """``method`` rides in ``info.input`` *and* is required by the schema.

    ``declare_discovery_extension`` supplies neither — the SDK fills them in
    ``bazaar_resource_server_extension``, the decorator-based server path we do
    not use (DESIGN_NOTES §2). Requiring it is what makes its absence loud.
    """
    spec = ENDPOINT_SPECS[key]
    bazaar = bazaar_extensions(spec)[BAZAAR]

    assert bazaar["info"]["input"]["method"] == spec.method
    schema_input = bazaar["schema"]["properties"]["input"]
    assert "method" in schema_input["required"]
    assert schema_input["properties"]["method"] == {"type": "string", "enum": [spec.method]}


def test_the_discovery_method_survives_the_client_round_trip(settings: Settings):
    """``resource.method`` is dropped in transit; ``extensions`` is not.

    The SDK's ``ResourceInfo`` has exactly three fields, so a client re-parsing
    our 402 silently discards ``resource.method`` before echoing it back —
    which is why the method has to ride in ``extensions`` instead. If anyone
    moves it back, this fails rather than the catalogue failing.
    """
    spec = ENDPOINT_SPECS["trending"]
    body = _402_body(spec, settings)

    # We do advertise it on `resource` for human readers of the 402 ...
    assert body["resource"]["method"] == "GET"
    # ... but only the `extensions` copy reaches the facilitator.
    assert _catalogued(body).method == "GET"


# ---------------------------------------------------------------------------
# The three silent traps
# ---------------------------------------------------------------------------


def test_a_missing_method_is_rejected_rather_than_catalogued_as_unknown(settings: Settings):
    """§21's failure: absent ``method`` validated fine and listed as ``UNKNOWN``."""
    spec = ENDPOINT_SPECS["trending"]
    extensions = copy.deepcopy(payment_extensions(spec, settings))
    extensions[BAZAAR]["info"]["input"].pop("method")

    body = _402_body(spec, settings, extensions=extensions)

    assert not validate_discovery_extension(parse_discovery_extension(extensions[BAZAAR])).valid
    assert _catalogued(body) is None


def test_an_undeclared_input_field_silently_delists_the_endpoint(settings: Settings):
    """``additionalProperties: false`` on ``info.input`` admits nothing new.

    ``headers`` is a spec-legal optional field and is still rejected by the
    schema the SDK generates, so widening ``info.input`` means widening the
    schema in the same change.
    """
    spec = ENDPOINT_SPECS["trending"]
    extensions = copy.deepcopy(payment_extensions(spec, settings))
    extensions[BAZAAR]["info"]["input"]["headers"] = {"X-Trace": "1"}

    result = validate_discovery_extension(parse_discovery_extension(extensions[BAZAAR]))

    assert not result.valid
    assert "Additional properties are not allowed" in result.errors[0]
    assert _catalogued(_402_body(spec, settings, extensions=extensions)) is None


def test_a_method_contradicting_its_variant_dies_silently_at_the_facilitator(
    settings: Settings,
):
    """The trap the build-time guard exists to prevent.

    The SDK picks the variant from ``body_type`` and types each variant's
    ``method`` as a ``Literal``, so a query-variant block claiming ``POST``
    raises inside ``parse_discovery_extension`` — which ``extract_discovery_info``
    catches in a bare ``except``, logs, and returns ``None`` from. No error
    reaches us; the payment settles and the endpoint is never listed.
    """
    spec = ENDPOINT_SPECS["trending"]  # GET -> query variant
    extensions = copy.deepcopy(payment_extensions(spec, settings))
    extensions[BAZAAR]["info"]["input"]["method"] = "POST"

    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        parse_discovery_extension(extensions[BAZAAR])
    assert _catalogued(_402_body(spec, settings, extensions=extensions)) is None


@pytest.mark.parametrize("key", sorted(ENDPOINT_SPECS))
def test_every_spec_method_agrees_with_its_discovery_variant(key: str):
    """The invariant :func:`bazaar_extensions` enforces, asserted over the real specs.

    ``is_body_method`` derives the variant from ``method`` today, so these
    cannot drift by accident — but the pairing is what keeps the block parseable,
    so it is checked rather than assumed.
    """
    spec = ENDPOINT_SPECS[key]
    declared = bazaar_extensions(spec)[BAZAAR]["info"]["input"]
    if spec.is_body_method:
        assert declared["method"] in {"POST", "PUT", "PATCH"}
        assert declared["bodyType"] == "json"
    else:
        assert declared["method"] in {"GET", "HEAD", "DELETE"}
        assert "bodyType" not in declared


def test_the_variant_guard_refuses_a_contradictory_spec():
    """A spec whose variant and method disagree fails loudly at build time."""
    from dataclasses import dataclass, fields, replace

    @dataclass(frozen=True)
    class _ForcedQueryVariant(EndpointSpec):
        """Always declares the query variant, whatever ``method`` says."""

        @property
        def is_body_method(self) -> bool:
            return False

    trending = ENDPOINT_SPECS["trending"]
    forced = _ForcedQueryVariant(**{f.name: getattr(trending, f.name) for f in fields(trending)})

    with pytest.raises(RuntimeError, match="contradicts the query discovery variant"):
        bazaar_extensions(replace(forced, method="POST"))


# ---------------------------------------------------------------------------
# Merchant identity
# ---------------------------------------------------------------------------


def test_merchant_identity_rides_beside_the_discovery_block(settings: Settings):
    """Both extensions ship, and the client echoes the whole ``extensions`` dict."""
    body = _402_body(ENDPOINT_SPECS["trending"], settings)

    extensions = _client_envelope(body)["extensions"]
    assert set(extensions) == {BAZAAR, MERCHANT}

    info = extensions[MERCHANT]["info"]
    assert info["name"] == "Play Clock"
    assert info["website"].startswith("https://")
    assert "fantasy-football" in info["categories"]


def test_merchant_block_matches_its_declared_schema(settings: Settings):
    """The merchant block carries its own schema, and must satisfy it."""
    jsonschema = pytest.importorskip("jsonschema")
    block = merchant_extension(settings)[MERCHANT]
    jsonschema.validate(instance=block["info"], schema=block["schema"])


def test_merchant_identity_is_configurable(settings: Settings):
    """Name, site, logo and categories are env-driven, not baked in."""
    configured = merchant_extension(
        settings.model_copy(
            update={
                "x402_merchant_name": "Other Co",
                "x402_merchant_categories": "alpha, beta ,",
                "x402_merchant_logo": "",
            }
        )
    )[MERCHANT]["info"]

    assert configured["name"] == "Other Co"
    assert configured["categories"] == ["alpha", "beta"]
    # An empty logo is omitted rather than advertised as a broken URL.
    assert "logo" not in configured


def test_merchant_block_does_not_disturb_discovery(settings: Settings):
    """Adding an unrelated extension must not change what gets catalogued."""
    spec = ENDPOINT_SPECS["player"]
    with_merchant = _catalogued(_402_body(spec, settings))
    bazaar_only = _catalogued(_402_body(spec, settings, extensions=bazaar_extensions(spec)))

    assert with_merchant.method == bazaar_only.method == "POST"
    assert with_merchant.resource_url == bazaar_only.resource_url


# ---------------------------------------------------------------------------
# The EXTENSION-RESPONSES sidechannel
# ---------------------------------------------------------------------------


def _sidechannel(payload: dict[str, Any]) -> dict[str, str]:
    return {EXTENSION_RESPONSES_HEADER: base64.b64encode(json.dumps(payload).encode()).decode()}


def test_sidechannel_decodes_the_spec_example():
    """The worked example from the x402 spec's sidechannel section."""
    assert decode_extension_responses("eyJiYXphYXIiOnsic3RhdHVzIjoic3VjY2VzcyJ9fQ==") == {
        "bazaar": {"status": "success"}
    }


@pytest.mark.parametrize("status", ["success", "processing"])
def test_a_catalogued_payment_logs_at_info(status: str, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("INFO"):
        log_extension_responses(_sidechannel({BAZAAR: {"status": status}}), call="settle")
    assert f"bazaar discovery {status} on settle" in caplog.text


def test_a_rejected_payment_logs_an_error_with_the_reason(caplog: pytest.LogCaptureFixture):
    """The alert §21 never got: the payment settled, the listing did not."""
    header = _sidechannel(
        {BAZAAR: {"status": "rejected", "rejectedReason": "input: 'method' is required"}}
    )

    with caplog.at_level("ERROR"):
        log_extension_responses(header, call="settle")

    assert "REJECTED" in caplog.text
    assert "input: 'method' is required" in caplog.text


async def test_a_real_settle_reads_the_sidechannel_off_the_response(
    caplog: pytest.LogCaptureFixture, settings: Settings
):
    """End-to-end: the httpx response hook fires on an actual settle call.

    The SDK surfaces only the parsed body, so the header is reachable only
    through the event hook :class:`HttpFacilitatorClient` installs. If that
    wiring ever comes loose the sidechannel is dead code that still unit-tests
    green, so this drives a settle through a transport and asserts the log.
    """
    import httpx

    from api.x402.facilitator import HttpFacilitatorClient
    from api.x402.schemas_compat import MOCK_PAYMENT_HEADER, payment_payload_from_header

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "transaction": "TXID",
                "network": "algorand:mainnet",
                "payer": "P" * 58,
            },
            headers=_sidechannel(
                {BAZAAR: {"status": "rejected", "rejectedReason": "schema validation failed"}}
            ),
        )

    client = HttpFacilitatorClient(
        "https://facilitator.goplausible.xyz", transport=httpx.MockTransport(handler)
    )
    requirements = build_payment_requirements("trending", settings)
    payload = payment_payload_from_header(MOCK_PAYMENT_HEADER, requirements)

    with caplog.at_level("ERROR"):
        result = await client.settle(payload, requirements)

    # The payment still succeeded — only the listing failed, which is the whole
    # point of the sidechannel.
    assert result.success is True
    assert "REJECTED" in caplog.text
    assert "schema validation failed" in caplog.text


def test_an_absent_or_broken_sidechannel_is_silent():
    """It rides on a ``MAY`` clause, so absence is normal and must never raise."""
    assert log_extension_responses({}, call="settle") == {}
    assert (
        log_extension_responses({EXTENSION_RESPONSES_HEADER: "not-base64!!"}, call="settle") == {}
    )
    assert decode_extension_responses("") == {}
    # Valid base64 of a non-object is still not a response map.
    assert decode_extension_responses(base64.b64encode(b'"str"').decode()) == {}
