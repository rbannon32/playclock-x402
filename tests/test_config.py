"""Settings behaviour: defaults, env overrides, price table."""

from __future__ import annotations

import pytest

from api.core.config import ENDPOINT_KEYS, Settings, get_settings


def _settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


def test_defaults_are_local_dev_safe() -> None:
    s = _settings()
    assert s.app_name == "Play Clock"
    assert s.env == "dev"
    assert s.store_backend == "memory"
    assert s.engine == "deterministic"
    assert s.x402_mode == "disabled"
    assert s.x402_network == "testnet"
    assert s.x402_asset_id == 0
    assert s.x402_challenge_tag == "x402-global-challenge"
    assert s.google_cloud_project is None
    assert s.google_cloud_location == "global"
    assert s.model_id == "gemini-3.7-flash"
    assert s.enable_espn is True
    assert s.sleeper_base_url == "https://api.sleeper.app/v1"
    assert s.week_override is None
    assert s.season == 2026


def test_price_table_matches_prd() -> None:
    s = _settings()
    assert s.prices() == {
        "trending": 0.10,
        "sleepers": 0.20,
        "player": 0.10,
        "matchup": 0.20,
        "roster": 0.35,
        "waivers": 0.20,
        "report": 0.35,
        "team_report": 0.50,
        "draft_board": 0.20,
        "draft_report": 0.50,
    }
    assert set(s.prices()) == set(ENDPOINT_KEYS)


def test_price_for_rejects_unknown_endpoint() -> None:
    with pytest.raises(KeyError):
        _settings().price_for("nope")


@pytest.mark.parametrize("price", [float("-inf"), -0.01, 0, 0.0000001, float("inf"), float("nan")])
def test_price_rejects_values_that_cannot_be_valid_usdc_quotes(price: float) -> None:
    with pytest.raises(ValueError):
        _settings(price_trending=price)


def test_price_accepts_one_usdc_atomic_unit() -> None:
    assert _settings(price_trending=0.000001).price_trending == 0.000001


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("x402_asset_id", -1),
        ("free_rate_limit_per_minute", -1),
        ("narrator_timeout_seconds", 0),
        ("narrator_timeout_seconds", -1),
    ],
)
def test_runtime_limits_reject_invalid_values(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        _settings(**{field: value})


def test_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("STORE_BACKEND", "firestore")
    monkeypatch.setenv("ENGINE", "adk")
    monkeypatch.setenv("MODEL_ID", "gemini-3.0-flash")
    monkeypatch.setenv("X402_MODE", "live")
    monkeypatch.setenv("X402_NETWORK", "mainnet")
    monkeypatch.setenv("X402_ASSET_ID", "31566704")
    monkeypatch.setenv("ENABLE_ESPN", "false")
    monkeypatch.setenv("WEEK_OVERRIDE", "7")
    monkeypatch.setenv("PRICE_TRENDING", "0.20")
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.env == "prod"
    assert s.store_backend == "firestore"
    assert s.engine == "adk"
    assert s.model_id == "gemini-3.0-flash"
    assert s.x402_mode == "live"
    assert s.x402_network == "mainnet"
    assert s.x402_asset_id == 31566704
    assert s.enable_espn is False
    assert s.week_override == 7
    assert s.price_for("trending") == 0.20
    assert s.trusted_proxy_hops == 1


def test_production_rate_limit_requires_verified_proxy_hops() -> None:
    with pytest.raises(ValueError, match="TRUSTED_PROXY_HOPS"):
        _settings(env="prod", free_rate_limit_per_minute=1)

    assert (
        _settings(
            env="prod", free_rate_limit_per_minute=0, trusted_proxy_hops=0
        ).free_rate_limit_per_minute
        == 0
    )


def test_invalid_literal_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_BACKEND", "postgres")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
