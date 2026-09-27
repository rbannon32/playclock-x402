"""ESPNClient: never raises, degrades to empty on every failure mode."""

from __future__ import annotations

import httpx
import pytest
import respx

from api.core.config import Settings
from api.data.espn import ESPN_BASE_URL, ESPNClient


def _settings(enable: bool = True) -> Settings:
    return Settings(_env_file=None, enable_espn=enable)  # type: ignore[call-arg]


@pytest.fixture
async def client():
    c = ESPNClient(_settings(True))
    try:
        yield c
    finally:
        await c.aclose()


@respx.mock
async def test_get_scoreboard_happy_path(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(
        return_value=httpx.Response(200, json={"events": [{"id": "1"}], "week": {"number": 3}})
    )
    board = await client.get_scoreboard()
    assert board["week"]["number"] == 3


@respx.mock
async def test_get_news_happy_path_and_limit(client: ESPNClient) -> None:
    route = respx.get(f"{ESPN_BASE_URL}/news").mock(
        return_value=httpx.Response(
            200,
            json={
                "articles": [
                    {"headline": "A"},
                    {"headline": "B"},
                    "not-a-dict",
                    {"headline": "C"},
                ]
            },
        )
    )
    news = await client.get_news(limit=2)
    assert [a["headline"] for a in news] == ["A", "B"]
    assert route.calls[0].request.url.params["limit"] == "2"


@respx.mock
async def test_flag_off_returns_empty_without_calling(client: ESPNClient) -> None:
    route = respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(
        return_value=httpx.Response(200, json={"x": 1})
    )
    async with ESPNClient(_settings(False)) as off:
        assert off.enabled is False
        assert await off.get_scoreboard() == {}
        assert await off.get_news() == []
    assert not route.called


@respx.mock
async def test_non_200_returns_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(return_value=httpx.Response(403, text="blocked"))
    respx.get(f"{ESPN_BASE_URL}/news").mock(return_value=httpx.Response(500))
    assert await client.get_scoreboard() == {}
    assert await client.get_news() == []


@respx.mock
async def test_transport_error_returns_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(side_effect=httpx.ConnectError("dns dead"))
    assert await client.get_scoreboard() == {}


@respx.mock
async def test_timeout_returns_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/news").mock(side_effect=httpx.ReadTimeout("too slow"))
    assert await client.get_news() == []


@respx.mock
async def test_malformed_json_returns_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    assert await client.get_scoreboard() == {}


@respx.mock
async def test_unexpected_shapes_return_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/scoreboard").mock(return_value=httpx.Response(200, json=[1, 2, 3]))
    respx.get(f"{ESPN_BASE_URL}/news").mock(
        return_value=httpx.Response(200, json={"articles": "nope"})
    )
    assert await client.get_scoreboard() == {}
    assert await client.get_news() == []


@respx.mock
async def test_missing_articles_key_returns_empty(client: ESPNClient) -> None:
    respx.get(f"{ESPN_BASE_URL}/news").mock(return_value=httpx.Response(200, json={}))
    assert await client.get_news() == []
