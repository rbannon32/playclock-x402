"""Application settings.

Single source of truth for runtime configuration. Everything is env-overridable
(see ``infra/env.example``) and every field has a default suitable for local dev,
so the whole app boots with zero configuration and no cloud credentials.

Usage::

    from api.core.config import get_settings

    settings = get_settings()          # cached singleton
    price = settings.price_for("trending")

Tests that need custom settings should either set env vars before the first
``get_settings()`` call, or construct ``Settings(...)`` directly and inject it
(every seam in this codebase takes ``settings`` as a parameter rather than
reaching for the global).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Stable keys identifying every paid endpoint. Used by the price table, the
#: ``/v1/catalog`` builder, the x402 payment dependency, and the response cache.
#: Order is the catalog display order.
ENDPOINT_KEYS: tuple[str, ...] = (
    "trending",
    "sleepers",
    "player",
    "matchup",
    "roster",
    "waivers",
    "report",
    "team_report",
    "draft_board",
    "draft_report",
)

StoreBackend = Literal["memory", "firestore"]
Engine = Literal["deterministic", "narrated", "adk"]
X402Mode = Literal["disabled", "mock", "live"]
X402Network = Literal["testnet", "mainnet"]

# USDC has six decimal places on Algorand. Reject values that cannot buy even
# one atomic unit, along with NaN/infinity, at startup rather than emitting an
# invalid (or accidentally free) payment requirement on the first paid call.
UsdcPrice = Annotated[float, Field(ge=0.000001, allow_inf_nan=False)]


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment / ``.env``.

    Field names map to upper-case env vars (``env=`` -> ``ENV``,
    ``price_trending`` -> ``PRICE_TRENDING``), matching tech spec §10.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app -------------------------------------------------------------
    app_name: str = Field(default="Play Clock", description="Human-facing product name.")
    env: str = Field(default="dev", description="Deployment environment: dev|staging|prod.")

    # --- storage ---------------------------------------------------------
    store_backend: StoreBackend = Field(
        default="memory",
        description="Which Store implementation get_store() returns. 'memory' needs no creds.",
    )
    google_cloud_project: str | None = Field(
        default=None, description="GCP project id; required when store_backend='firestore'."
    )
    google_cloud_location: str = Field(
        default="global",
        description=(
            "Vertex AI location. MODEL_ID=gemini-3.7-flash resolves only at 'global' "
            "(DESIGN_NOTES §1); Cloud Run's region is a deploy flag, not this."
        ),
    )

    # --- analysis engine -------------------------------------------------
    engine: Engine = Field(
        default="deterministic",
        description=(
            "Analysis engine selector. 'deterministic' builds responses from stats "
            "tools with no LLM (tests, CI, fallback); 'narrated' keeps that body and "
            "has Gemini rewrite only its prose in one tool-free call; 'adk' runs the "
            "real ADK pipeline."
        ),
    )
    model_id: str = Field(
        default="gemini-3.7-flash",
        description="Vertex AI / Gemini model id for ADK agents and the narrator.",
    )
    narrator_timeout_seconds: float = Field(
        default=20.0,
        gt=0,
        description=(
            "How long ENGINE=narrated waits for the prose rewrite before serving the "
            "deterministic body unchanged. Settlement precedes the response, so this "
            "plus the deterministic build must stay under the client's deadline."
        ),
    )
    model_thinking_level: str | None = Field(
        default=None,
        description=(
            "Gemini thinking level for every Vertex call: 'low' caps the model's "
            "internal reasoning, None leaves the model's default. Thinking bills "
            "at the OUTPUT rate and is most of it — measured on a realistic board "
            "prompt, 562 thinking tokens against 154 of answer (DESIGN_NOTES §27). "
            "It is a quality knob as well as a cost one, so it is off by default "
            "and set per service."
        ),
    )
    model_retry_attempts: int = Field(
        default=4,
        ge=1,
        description=(
            "Attempts per Vertex request, including the first, when it answers 408, "
            "429 or 5xx — google-genai's own exponential backoff, 2s doubling to a "
            "30s ceiling (api/agents/vertex.py). 1 disables. Without it a single 429 "
            "in a nine-call board failed the run and the outer retries bought the "
            "finished calls again. The ingest job runs higher: nothing waits on it."
        ),
    )

    # --- x402 payments ---------------------------------------------------
    x402_mode: X402Mode = Field(
        default="disabled",
        description=(
            "'disabled' bypasses payment entirely, 'mock' accepts a magic test header, "
            "'live' talks to the real facilitator."
        ),
    )
    x402_network: X402Network = Field(
        default="testnet", description="Algorand network for payments."
    )
    x402_pay_to: str = Field(
        default="",
        description="Algorand address that receives USDC (one address, Composite entry).",
    )
    x402_facilitator_url: str = Field(
        default="", description="GoPlausible facilitator base URL for verify/settle."
    )
    x402_asset_id: int = Field(
        default=0,
        ge=0,
        description="USDC ASA id for the active network. 0 means the built-in id for X402_NETWORK.",
    )
    x402_challenge_tag: str = Field(
        default="x402-global-challenge",
        description="Metadata tag required for challenge leaderboard attribution.",
    )

    # --- Bazaar merchant identity (the `x402-merchant` extension) ---------
    #
    # Without these the catalogue and the leaderboard fall back to scraping the
    # resource origin's root HTML and `/apple-touch-icon.png`, and a host that
    # serves neither lists as a truncated payTo address (DESIGN_NOTES §25).
    # Declaring them is the supported override for that guess.
    x402_merchant_name: str = Field(
        default="Play Clock",
        description="Merchant name shown in the Bazaar catalogue and on the leaderboard.",
    )
    x402_merchant_website: str = Field(
        default="https://playclock.xyz",
        description="Merchant site an agent (or a judge) visits to learn what is on sale.",
    )
    x402_merchant_logo: str = Field(
        default="https://api.playclock.xyz/apple-touch-icon.png",
        description="Absolute https URL of the merchant logo. Empty omits the field.",
    )
    x402_merchant_categories: str = Field(
        default="fantasy-football,nfl,sports-analytics,api,algorand,x402",
        description=(
            "Comma-separated Bazaar categories. This is the only way to set them — "
            "everything else about the listing is derived from the settled payment."
        ),
    )

    # --- upstream data sources -------------------------------------------
    enable_espn: bool = Field(
        default=True, description="Feature flag for the unofficial ESPN endpoints (§4.3)."
    )
    sleeper_base_url: str = Field(
        default="https://api.sleeper.app/v1",
        description="Sleeper API base URL (no trailing slash).",
    )

    research_endpoints: str | None = Field(
        default=None,
        description=(
            "Comma-separated endpoint keys that run the google_search research "
            "agent, overriding the per-endpoint default. The search agent is the "
            "slow, high-variance half of the pipeline, and the personalized "
            "endpoints settle before they return — so which endpoints pay for it "
            "is the measurement that decides the latency work. Unset keeps the "
            "defaults in api/agents/prompts.py; 'none' disables it everywhere."
        ),
    )

    free_rate_limit_per_minute: int = Field(
        default=120,
        ge=0,
        description=(
            "Per-client cap on the free routes, per minute, per instance. "
            "Payment is the limit on the paid routes; this stops an advert or a "
            "crawler turning the free ones into an unbounded bill. 0 disables."
        ),
    )

    trusted_proxy_hops: int = Field(
        default=0,
        ge=0,
        description=(
            "Number of X-Forwarded-For entries appended by trusted infrastructure, "
            "counted from the socket end. One trusted proxy appends one entry -- the "
            "address it received from -- so with 1 the caller is the rightmost value. "
            "Required in production when the free-route limit is enabled; 0 uses "
            "the socket peer for local and direct deployments."
        ),
    )

    quality_judge: bool = Field(
        default=False,
        description=(
            "Score every board the precompute task warms with one Gemini call "
            "against the rubric in ingest/judge.py, and write the score next to "
            "the cached body. Needs Vertex credentials; the ingest job has them, "
            "the api service does not. A flagged board is logged, never refused."
        ),
    )

    engine_fallback: bool = Field(
        default=True,
        description=(
            "When the ADK pipeline fails, answer from the deterministic engine "
            "instead of 500ing. The fallback body is schema-valid, grounded in "
            "ingested stats and self-identifying (model=null). Set false to "
            "prefer an outage over a degraded answer."
        ),
    )

    # --- season / week ---------------------------------------------------
    week_override: int | None = Field(
        default=None,
        description="Force the current NFL week (testing, and the gap before ingest has run).",
    )
    season: int = Field(default=2026, description="Active NFL season year.")

    # --- prices (USDC, env-tunable for October experiments) --------------
    price_trending: UsdcPrice = Field(default=0.10, description="Price of GET /v1/trending.")
    price_sleepers: UsdcPrice = Field(default=0.20, description="Price of GET /v1/sleepers.")
    price_player: UsdcPrice = Field(default=0.10, description="Price of POST /v1/player.")
    price_matchup: UsdcPrice = Field(default=0.20, description="Price of POST /v1/matchup.")
    price_roster: UsdcPrice = Field(default=0.35, description="Price of POST /v1/roster.")
    price_waivers: UsdcPrice = Field(default=0.20, description="Price of GET /v1/waivers.")
    price_report: UsdcPrice = Field(default=0.35, description="Price of GET /v1/report.")
    price_team_report: UsdcPrice = Field(default=0.50, description="Price of POST /v1/team-report.")
    price_draft_board: UsdcPrice = Field(default=0.20, description="Price of GET /v1/draft-board.")
    price_draft_report: UsdcPrice = Field(
        default=0.50, description="Price of POST /v1/draft-report."
    )

    @model_validator(mode="after")
    def _require_verified_proxy_for_production_rate_limit(self) -> Settings:
        """Reject a production per-client limit without a verified proxy topology."""
        if (
            self.env.lower() == "prod"
            and self.free_rate_limit_per_minute > 0
            and self.trusted_proxy_hops == 0
        ):
            raise ValueError(
                "TRUSTED_PROXY_HOPS must be set above 0 when the production "
                "free-route rate limit is enabled"
            )
        return self

    def price_for(self, endpoint_key: str) -> float:
        """Return the USDC price for ``endpoint_key``.

        Args:
            endpoint_key: One of :data:`ENDPOINT_KEYS`.

        Returns:
            Price in USDC as a float (e.g. ``0.25``).

        Raises:
            KeyError: If ``endpoint_key`` is not a known paid endpoint.
        """
        if endpoint_key not in ENDPOINT_KEYS:
            raise KeyError(f"unknown endpoint key: {endpoint_key!r}")
        return float(getattr(self, f"price_{endpoint_key}"))

    def prices(self) -> dict[str, float]:
        """Return the full ``{endpoint_key: usdc_price}`` table."""
        return {key: self.price_for(key) for key in ENDPOINT_KEYS}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached process-wide :class:`Settings`.

    Cached so env parsing happens once. Call ``get_settings.cache_clear()`` in
    tests that mutate the environment.
    """
    return Settings()
