"""ESPN unofficial endpoints — supplemental, feature-flagged, never load-bearing.

Tech spec §4.3: these are undocumented endpoints that can break or start blocking
us at any time. The contract of this module is therefore stronger than usual:

    **No method here ever raises.** Any failure — flag off, timeout, transport
    error, non-200, malformed JSON — is logged at WARNING and returns the empty
    value for that method (``{}`` / ``[]``). Callers must treat empty as normal.

Nothing in the product may depend on ESPN data being present.
"""

from __future__ import annotations

import logging
from typing import Any, Self

import httpx

from api.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Base for the public ESPN site API.
ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"

#: Hard timeout — ESPN is supplemental, so we give up fast (tech spec §4.3).
DEFAULT_TIMEOUT = 5.0


class ESPNClient:
    """Best-effort client for ESPN's unofficial NFL endpoints.

    Args:
        settings: Provides ``enable_espn``. Defaults to process settings.
        client: Pre-built ``httpx.AsyncClient`` (caller owns its lifecycle).
        transport: Transport for a client this instance builds and owns —
            the injection point for ``respx`` in tests.
        timeout: Per-request timeout in seconds.
        base_url: Override the ESPN base URL.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        base_url: str = ESPN_BASE_URL,
    ) -> None:
        self._settings = settings or get_settings()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            transport=transport,
            headers={"accept": "application/json"},
        )

    @property
    def enabled(self) -> bool:
        """Whether the ``ENABLE_ESPN`` feature flag is on."""
        return bool(self._settings.enable_espn)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying client if this instance created it."""
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        """GET and return parsed JSON, or ``None`` on any failure whatsoever."""
        if not self.enabled:
            logger.debug("espn disabled by feature flag; skipping %s", path)
            return None
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            response = await self._client.get(url, params=params, timeout=self._timeout)
            if response.status_code != 200:
                logger.warning(
                    "espn %s returned %s; degrading to empty", path, response.status_code
                )
                return None
            return response.json()
        except Exception as exc:  # noqa: BLE001 - by design: ESPN must never break a request
            logger.warning("espn %s failed (%s); degrading to empty", path, exc)
            return None

    async def get_scoreboard(self) -> dict[str, Any]:
        """Return the current NFL scoreboard payload, or ``{}`` on any failure."""
        data = await self._get("/scoreboard")
        return data if isinstance(data, dict) else {}

    async def get_news(self, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to ``limit`` NFL headlines, or ``[]`` on any failure.

        Returns:
            The ``articles`` array from ESPN's news payload. Each item typically
            has ``headline``, ``description``, ``published`` and ``links``.
        """
        data = await self._get("/news", params={"limit": limit})
        if not isinstance(data, dict):
            return []
        articles = data.get("articles")
        if not isinstance(articles, list):
            return []
        return [a for a in articles if isinstance(a, dict)][:limit]
