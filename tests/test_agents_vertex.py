"""The Vertex retry policy: one answer to "what does a 429 cost us".

google-genai retries nothing unless asked. These pin that we ask, with the
same policy, from every client this project opens — the ADK model is covered
in test_agents_pipeline.py; here are the policy itself and the direct client
the narrator and the judge share.
"""

from __future__ import annotations

from api.agents.vertex import (
    RETRY_INITIAL_DELAY_SECONDS,
    RETRY_MAX_DELAY_SECONDS,
    genai_client,
    retry_options,
)
from api.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        google_cloud_project="playclock-test",
        google_cloud_location="global",
        **overrides,  # type: ignore[arg-type]
    )


def test_the_default_policy_retries_with_backoff() -> None:
    options = retry_options(_settings())
    assert options is not None
    assert options.attempts == 4
    assert options.initial_delay == RETRY_INITIAL_DELAY_SECONDS == 2.0
    assert options.max_delay == RETRY_MAX_DELAY_SECONDS == 30.0
    # SDK default set (408, 429, 5xx). Naming codes here would silently drop
    # one the SDK adds later, and would be the place someone adds 404.
    assert options.http_status_codes is None


def test_the_ingest_job_can_run_more_patient() -> None:
    """infra/deploy.md sets MODEL_RETRY_ATTEMPTS=6 on the job: nothing waits on it."""
    options = retry_options(_settings(model_retry_attempts=6))
    assert options is not None and options.attempts == 6


def test_one_attempt_is_no_policy() -> None:
    assert retry_options(_settings(model_retry_attempts=1)) is None


def test_the_direct_client_carries_the_policy_and_opens_nothing() -> None:
    """The narrator's and the judge's client. Constructing it needs no
    credentials — the SDK resolves them on the first request — which is what
    lets this run in CI."""
    client = genai_client(_settings())
    options = client._api_client._http_options.retry_options  # noqa: SLF001 - the SDK has no getter
    assert options is not None and options.attempts == 4
    assert client.vertexai is True


# -- thinking ------------------------------------------------------------------
#
# Thinking bills at the output rate and is most of it. It is also a quality
# knob, so the default must stay "the model decides" — the behaviour this
# project shipped and measured its evals against.


def test_thinking_is_the_models_own_default_unless_asked() -> None:
    from api.agents.vertex import thinking_config

    assert thinking_config(_settings()) is None
    assert thinking_config(_settings(model_thinking_level="")) is None


def test_a_level_becomes_a_thinking_config() -> None:
    from api.agents.vertex import thinking_config

    cfg = thinking_config(_settings(model_thinking_level="low"))
    assert cfg is not None and cfg.thinking_level == "LOW"
    # Case and padding come from env vars typed by hand.
    assert thinking_config(_settings(model_thinking_level=" LOW ")).thinking_level == "LOW"
