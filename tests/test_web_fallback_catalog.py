"""The web UI's offline catalog must quote what the server actually charges.

``FALLBACK_CATALOG`` in ``web/js/endpoints.js`` is what the pay page uses when
``/v1/catalog`` cannot be fetched. ``expectedPayment()`` derives the amount it
will sign from it, so a stale price there refuses the real 402 and every
purchase fails until a reload. It drifted once already, silently.
"""

from __future__ import annotations

import re
from pathlib import Path

from api.core.config import ENDPOINT_KEYS, Settings

_ENDPOINTS_JS = Path(__file__).resolve().parents[1] / "web" / "js" / "endpoints.js"


def _fallback_block() -> str:
    source = _ENDPOINTS_JS.read_text(encoding="utf-8")
    start = source.index("export const FALLBACK_CATALOG")
    return source[start : source.index("});", start)]


def _fallback_prices() -> dict[str, float]:
    pairs = re.findall(r'key: "(\w+)",.*?price_usdc: ([0-9.]+)', _fallback_block(), re.S)
    return {key: float(price) for key, price in pairs}


def test_fallback_catalog_lists_every_paid_endpoint_in_order() -> None:
    assert list(_fallback_prices()) == list(ENDPOINT_KEYS)


def test_fallback_catalog_prices_match_settings_defaults() -> None:
    assert _fallback_prices() == Settings(_env_file=None).prices()


def test_fallback_catalog_names_the_live_network() -> None:
    assert re.search(r'network: "mainnet"', _fallback_block())
