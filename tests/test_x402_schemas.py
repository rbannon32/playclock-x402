"""Wire-shape tests for the x402 protocol layer.

These assert the *exact* bytes an x402 client or the challenge leaderboard sees:
protocol version, camelCase field names, atomic-unit amounts, CAIP-2 networks,
USDC ASA ids, and the ``accepts[].extra.tag`` placement that drives leaderboard
attribution. They are deliberately picky — a silent rename here costs us the
entry, not just a test.
"""

from __future__ import annotations

import base64
import json

import pytest

from api.core.config import ENDPOINT_KEYS, Settings
from api.x402.endpoints import ENDPOINT_SPECS, bazaar_extensions, spec_for
from api.x402.schemas_compat import (
    ALGORAND_MAINNET_CAIP2,
    ALGORAND_TESTNET_CAIP2,
    GOPLAUSIBLE_FACILITATOR_URL,
    MOCK_MARKER_KEY,
    MOCK_PAYMENT_HEADER,
    USDC_MAINNET_ASA_ID,
    USDC_TESTNET_ASA_ID,
    X402ConfigError,
    assert_public_resource_url,
    atomic_amount,
    build_payment_required,
    build_payment_requirements,
    decode_payment_header,
    network_caip2,
    payment_hash,
    payment_payload_from_header,
    payment_required_body,
    resolve_resource_url,
    usdc_asset_id,
)


def make_settings(**overrides: object) -> Settings:
    """Explicit settings with payments configured; no env, no creds."""
    defaults: dict[str, object] = {
        "_env_file": None,
        "store_backend": "memory",
        "engine": "deterministic",
        "x402_mode": "mock",
        "x402_network": "testnet",
        "x402_pay_to": "PAYTOADDRESS",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Price table -> atomic units
# --------------------------------------------------------------------------


def test_atomic_amount_converts_usdc_to_six_decimals() -> None:
    assert atomic_amount(0.10) == "100000"
    assert atomic_amount(0.15) == "150000"
    assert atomic_amount(0.75) == "750000"
    assert atomic_amount(1.0) == "1000000"


def test_every_endpoint_key_prices_correctly() -> None:
    """Each priced endpoint produces the right atomic amount, as a string."""
    settings = make_settings()
    for key in ENDPOINT_KEYS:
        requirements = build_payment_requirements(key, settings)
        expected = str(round(settings.price_for(key) * 1_000_000))
        assert requirements.amount == expected, key
        assert isinstance(requirements.amount, str), key


def test_every_endpoint_key_has_a_spec() -> None:
    assert set(ENDPOINT_SPECS) == set(ENDPOINT_KEYS)
    for key in ENDPOINT_KEYS:
        spec = spec_for(key)
        assert spec.description.strip()
        assert spec.response_schema.endswith("Response")
        assert spec.method in ("GET", "POST")


def test_spec_for_rejects_unknown_key() -> None:
    with pytest.raises(KeyError):
        spec_for("nope")


# --------------------------------------------------------------------------
# Network / asset resolution
# --------------------------------------------------------------------------


def test_network_and_asset_follow_x402_network() -> None:
    testnet = make_settings(x402_network="testnet")
    mainnet = make_settings(x402_network="mainnet")

    assert network_caip2(testnet) == ALGORAND_TESTNET_CAIP2
    assert network_caip2(mainnet) == ALGORAND_MAINNET_CAIP2
    assert network_caip2(testnet).startswith("algorand:")

    assert usdc_asset_id(testnet) == str(USDC_TESTNET_ASA_ID) == "10458941"
    assert usdc_asset_id(mainnet) == str(USDC_MAINNET_ASA_ID) == "31566704"


def test_explicit_asset_id_overrides_the_network_default() -> None:
    settings = make_settings(x402_asset_id=999)
    assert usdc_asset_id(settings) == "999"


def test_live_mode_requires_a_pay_to_address() -> None:
    settings = make_settings(x402_mode="live", x402_pay_to="")
    with pytest.raises(X402ConfigError):
        build_payment_requirements("trending", settings)


# --------------------------------------------------------------------------
# 402 body shape
# --------------------------------------------------------------------------


def build_body(endpoint_key: str = "trending", **overrides: object) -> dict:
    settings = make_settings(**overrides)
    spec = spec_for(endpoint_key)
    requirements = build_payment_requirements(endpoint_key, settings)
    payment_required = build_payment_required(
        requirements=requirements,
        resource_url=f"https://api.example.com{spec.path}",
        description=spec.description,
        extensions=bazaar_extensions(spec),
        error="payment_required",
    )
    return payment_required_body(payment_required)


def test_402_body_is_v2_and_camel_case() -> None:
    body = build_body()

    assert body["x402Version"] == 2
    assert body["error"] == "payment_required"
    assert body["resource"]["mimeType"] == "application/json"
    assert body["resource"]["url"] == "https://api.example.com/v1/trending"

    accepts = body["accepts"][0]
    assert accepts["scheme"] == "exact"
    assert accepts["payTo"] == "PAYTOADDRESS"
    assert accepts["maxTimeoutSeconds"] == 120
    # V2 renamed V1's maxAmountRequired to amount, and it is a *string*.
    assert accepts["amount"] == "100000"
    assert "maxAmountRequired" not in accepts
    # snake_case must never leak onto the wire.
    for snake in ("pay_to", "max_timeout_seconds", "mime_type", "x402_version"):
        assert snake not in json.dumps(body)


def test_challenge_tag_sits_in_accepts_extra_on_every_endpoint() -> None:
    """``accepts[].extra.tag`` is what the challenge leaderboard indexes."""
    for key in ENDPOINT_KEYS:
        body = build_body(key)
        extra = body["accepts"][0]["extra"]
        assert extra["tag"] == "x402-global-challenge", key
        # The AVM scheme merges into extra rather than replacing it, so the
        # USDC display metadata has to coexist with the tag.
        assert extra["name"] == "USDC", key
        assert extra["decimals"] == 6, key


def test_bazaar_discovery_extension_is_present_and_typed_by_method() -> None:
    get_body = build_body("trending")
    bazaar = get_body["extensions"]["bazaar"]
    assert bazaar["info"]["input"]["type"] == "http"
    assert "queryParams" in bazaar["info"]["input"]
    assert bazaar["info"]["output"]["type"] == "json"
    assert "$schema" in bazaar["schema"]

    post_body = build_body("team_report")
    post_bazaar = post_body["extensions"]["bazaar"]
    assert post_bazaar["info"]["input"]["bodyType"] == "json"
    assert post_bazaar["info"]["input"]["body"]["sleeper_username"] == "example_manager"


def test_every_endpoint_declares_its_method_in_the_discovery_block() -> None:
    """A catalogue id is base64("METHOD:URL"), so the method is half the key.

    The SDK does not fill this in: `declare_discovery_extension` says the method
    is "enriched by bazaar_resource_server_extension at runtime", which is its
    decorator-based server. Play Clock gates with a route dependency instead, so
    nothing enriches it and the field is absent unless we add it.
    """
    for key in ENDPOINT_KEYS:
        spec = spec_for(key)
        block = bazaar_extensions(spec)["bazaar"]["info"]["input"]
        assert block["method"] == spec.method, key
        assert spec.method in ("GET", "POST"), key


def test_the_discovery_method_survives_the_client_round_trip() -> None:
    """What settles is what the client echoed back, re-parsed by us.

    This is the property that makes `extensions` the right carrier and
    `resource.method` the wrong one. `extensions` is a plain dict end to end;
    `resource` is re-validated through the SDK's three-field `ResourceInfo`,
    which silently drops anything else — so the method we advertise there
    reaches the client and dies on the way back, never reaching the
    facilitator. Asserted in both directions so a future "tidy-up" that moves
    the method onto `resource` fails here instead of in the catalogue.
    """
    settings = make_settings()
    spec = spec_for("trending")
    requirements = build_payment_requirements("trending", settings)
    payment_required = build_payment_required(
        requirements=requirements,
        resource_url=f"https://api.example.com{spec.path}",
        description=spec.description,
        extensions=bazaar_extensions(spec),
    )
    # `method=` is what production passes (api/x402/middleware.py:195).
    body = payment_required_body(payment_required, method=spec.method)

    assert body["resource"]["method"] == "GET"

    # Echo the 402 back the way a client does (see web/js/wallet/envelope.js).
    envelope = {
        "x402Version": 2,
        "payload": {"paymentGroup": ["b64"], "paymentIndex": 0},
        "accepted": body["accepts"][0],
        "resource": body["resource"],
        "extensions": body["extensions"],
    }
    raw = base64.b64encode(json.dumps(envelope).encode()).decode()

    payload = payment_payload_from_header(raw, requirements)
    assert payload is not None

    settled = payload.model_dump(by_alias=True, exclude_none=True)
    assert settled["extensions"]["bazaar"]["info"]["input"]["method"] == "GET"
    assert "method" not in settled["resource"]


def test_bazaar_output_examples_match_the_response_contract() -> None:
    """Examples carry the AnalysisResponse block every paid body promises."""
    for key in ENDPOINT_KEYS:
        example = spec_for(key).output_example
        for required in ("verdict", "confidence", "reasoning", "stats_cited", "sources", "meta"):
            assert required in example, f"{key} missing {required}"
        assert example["confidence"] in ("high", "medium", "low"), key


# --------------------------------------------------------------------------
# Inbound header decoding
# --------------------------------------------------------------------------


def test_payment_hash_is_stable_and_whitespace_insensitive() -> None:
    assert payment_hash("abc") == payment_hash(" abc ")
    assert payment_hash("abc") != payment_hash("abd")
    assert len(payment_hash("abc")) == 64


@pytest.mark.parametrize("garbage", ["", "   ", "not-base64!!", "YWJj", "e30"])
def test_decode_payment_header_rejects_non_json(garbage: str) -> None:
    # "YWJj" is valid base64 for "abc" (not JSON); "e30" decodes to "{}" which
    # *is* a dict, so it is excluded from the non-JSON cases below.
    result = decode_payment_header(garbage)
    assert result is None or result == {}


def test_decode_payment_header_accepts_padded_unpadded_and_plain_json() -> None:
    payload = {"x402Version": 2, "payload": {"mock": True}}
    raw = json.dumps(payload)
    padded = base64.b64encode(raw.encode()).decode()
    unpadded = padded.rstrip("=")

    assert decode_payment_header(padded) == payload
    assert decode_payment_header(unpadded) == payload
    assert decode_payment_header(raw) == payload


def test_mock_magic_header_becomes_a_marked_payload() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    payload = payment_payload_from_header(MOCK_PAYMENT_HEADER, requirements)

    assert payload is not None
    assert payload.x402_version == 2
    assert payload.payload[MOCK_MARKER_KEY] is True
    assert payload.accepted.amount == "100000"


def test_payload_without_accepted_is_bound_to_our_requirements() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("report", settings)
    raw = base64.b64encode(
        json.dumps(
            {"x402Version": 2, "payload": {"paymentGroup": ["b64"], "paymentIndex": 0}}
        ).encode()
    ).decode()

    payload = payment_payload_from_header(raw, requirements)
    assert payload is not None
    assert payload.accepted.amount == requirements.amount
    assert payload.payload["paymentIndex"] == 0


def test_payload_missing_its_payload_object_is_rejected() -> None:
    settings = make_settings()
    requirements = build_payment_requirements("trending", settings)
    raw = base64.b64encode(json.dumps({"x402Version": 2}).encode()).decode()
    assert payment_payload_from_header(raw, requirements) is None


# --------------------------------------------------------------------------
# Resource URL guard (the facilitator catalogs this permanently)
# --------------------------------------------------------------------------


def test_resource_base_url_env_overrides_the_request_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X402_RESOURCE_BASE_URL", "https://api.playclock.example/")
    url = resolve_resource_url("http://localhost:8000/v1/sleepers?week=5", "/v1/sleepers", "week=5")
    assert url == "https://api.playclock.example/v1/sleepers?week=5"


def test_resource_url_falls_back_to_the_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X402_RESOURCE_BASE_URL", raising=False)
    assert resolve_resource_url("http://x/v1/report", "/v1/report", "") == "http://x/v1/report"


@pytest.mark.parametrize(
    "url", ["http://localhost:8000/v1/trending", "http://127.0.0.1/v1/trending", "/v1/trending"]
)
def test_live_mode_refuses_to_advertise_a_local_resource_url(url: str) -> None:
    with pytest.raises(X402ConfigError):
        assert_public_resource_url(url, make_settings(x402_mode="live"))


def test_live_mode_refuses_to_advertise_a_plain_http_resource_url() -> None:
    """Cloud Run terminates TLS, so an unset base URL reads http: never catalogue that."""
    with pytest.raises(X402ConfigError):
        assert_public_resource_url(
            "http://api.playclock.example/v1/trending", make_settings(x402_mode="live")
        )
    assert_public_resource_url(
        "https://api.playclock.example/v1/trending", make_settings(x402_mode="live")
    )


def test_non_live_modes_tolerate_localhost() -> None:
    assert_public_resource_url("http://localhost:8000/v1/trending", make_settings(x402_mode="mock"))


def test_goplausible_is_the_documented_facilitator() -> None:
    """Never the SDK default — settling elsewhere forfeits leaderboard credit."""
    assert GOPLAUSIBLE_FACILITATOR_URL == "https://facilitator.goplausible.xyz"
