"""Shared pytest fixtures.

Every test runs against :class:`~api.core.store.MemoryStore` with explicitly
constructed :class:`~api.core.config.Settings` — no env, no network, no creds.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from api.core.config import Settings, get_settings
from api.core.store import MemoryStore, Store, set_store


@pytest.fixture
def settings() -> Settings:
    """Default local-dev settings, isolated from the environment."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        store_backend="memory",
        engine="deterministic",
        x402_mode="disabled",
        week_override=None,
        season=2026,
    )


@pytest.fixture
def store() -> Iterator[Store]:
    """A fresh in-memory store, also installed as the global store override."""
    memory = MemoryStore()
    set_store(memory)
    yield memory
    set_store(None)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the cached global settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _UnknownRound:
    """The chain round is unknown: the validity floor fails open, as with algod down."""

    async def current_round(self) -> int | None:
        return None


@pytest.fixture(autouse=True)
def _no_algod() -> Iterator[None]:
    """Never let a live-mode test reach algod for the current round."""
    from api.x402.validity import set_round_clock

    set_round_clock(_UnknownRound())
    yield
    set_round_clock(None)
