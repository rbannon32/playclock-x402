"""How this project talks to Vertex AI: the retry policy, and the client that carries it.

Why a retry policy at all
-------------------------
``google-genai`` does not retry unless asked. With ``retry_options`` unset its
client is built with ``stop_after_attempt(1)``, so every 429 reaches the caller
as an exception on the first try. Vertex answers ``429 RESOURCE_EXHAUSTED``
under dynamic shared quota whenever calls bunch up — which is exactly what the
stats agent's tool loop does — and a warmed board is ~9 calls in a row.

Before this module, a 429 on *any* of those nine failed the whole pipeline run,
and the retries that existed sat above it: :mod:`api.agents.pipeline` ran the
pipeline again from the first call, then :mod:`ingest.precompute` ran the
pipeline again from the first call. Every retry re-bought the stats agent's
finished work. In three days of ingest logs, 38 of ~105 pipeline runs died that
way — roughly a third of the model bill spent on output that was thrown away
(DESIGN_NOTES §26).

Retrying the *request* keeps the finished tool calls and costs one short pause.
The SDK's own tenacity policy does the work: exponential backoff with jitter on
408, 429 and 5xx, configured once here and handed to every client — the ADK
``Gemini`` model in the pipeline, the narrator's client and the judge's — so
there is one answer to "what does a 429 cost us". The outer retries remain as
the last resort they were meant to be.

Everything Google is imported lazily, as the rest of :mod:`api.agents` does:
a deterministic deployment and the test suite never import ``google-genai``.
"""

from __future__ import annotations

from typing import Any

from api.core.config import Settings

#: Pause before the first retry. Vertex's 429s clear in seconds, not minutes,
#: when the caller stops hammering; the SDK doubles this each attempt.
RETRY_INITIAL_DELAY_SECONDS = 2.0

#: Ceiling on any single pause. With the default four attempts the backoff
#: never reaches it (2s, 4s, 8s); the ingest job, which runs more attempts
#: because nothing is waiting on it, does.
RETRY_MAX_DELAY_SECONDS = 30.0


def retry_options(settings: Settings) -> Any | None:
    """The ``HttpRetryOptions`` every Vertex client in this project uses.

    ``None`` when ``MODEL_RETRY_ATTEMPTS`` is 1, which is the SDK's own
    no-retry behaviour spelled out rather than implied. Status codes are left
    to the SDK default (408, 429, 5xx): they are the transient set, and a 400
    or 404 — the wrong model at the wrong location — must fail fast.
    """
    attempts = settings.model_retry_attempts
    if attempts <= 1:
        return None
    from google.genai import types  # noqa: PLC0415

    return types.HttpRetryOptions(
        attempts=attempts,
        initial_delay=RETRY_INITIAL_DELAY_SECONDS,
        max_delay=RETRY_MAX_DELAY_SECONDS,
    )


def thinking_config(settings: Settings) -> Any | None:
    """The ``ThinkingConfig`` every Vertex call carries, or ``None`` for default.

    Thinking tokens bill at the output rate and dominate it: on a realistic
    board prompt, 562 thinking tokens against 154 tokens of answer, five runs
    out of five, with the same rows produced either way. ``MODEL_THINKING_LEVEL=low``
    took that to zero.

    Unset leaves the model to decide, which is the behaviour this project shipped
    with. It is off by default because thinking buys reasoning quality as well as
    tokens, and the gate that would notice the difference (``api/evals``) has to
    be run against the change rather than assumed.
    """
    level = (settings.model_thinking_level or "").strip().lower()
    if not level:
        return None
    from google.genai import types  # noqa: PLC0415

    return types.ThinkingConfig(thinking_level=level)


def genai_client(settings: Settings) -> Any:
    """A Vertex-backed ``google.genai.Client`` carrying :func:`retry_options`.

    For the callers that talk to the SDK directly (the narrator, the judge).
    The ADK pipeline goes through ``google.adk.models.Gemini`` instead, which
    builds its own client and takes the same options — see
    :meth:`api.agents.pipeline.AdkAnalysisEngine.build_pipeline`.
    """
    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    return genai.Client(
        vertexai=True,
        project=settings.google_cloud_project,
        location=settings.google_cloud_location,
        http_options=types.HttpOptions(retry_options=retry_options(settings)),
    )
