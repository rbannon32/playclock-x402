"""SleeperClient: happy paths, retry semantics, and error mapping."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from api.core.config import Settings
from api.data.sleeper import (
    MAX_RETRY_AFTER,
    SleeperClient,
    SleeperError,
    SleeperNotFound,
    SleeperUnavailable,
    TrendingEntry,
    retry_after_seconds,
)

BASE = "https://api.sleeper.app/v1"


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, sleeper_base_url=BASE)  # type: ignore[call-arg]


@pytest.fixture
async def client(settings: Settings):
    c = SleeperClient(settings, retry_wait=0)
    try:
        yield c
    finally:
        await c.aclose()


@respx.mock
async def test_get_user(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "12345", "username": "ryan"})
    )
    user = await client.get_user("ryan")
    assert user["user_id"] == "12345"
    assert route.called


@respx.mock
async def test_get_user_404_raises_not_found(client: SleeperClient) -> None:
    respx.get(f"{BASE}/user/ghost").mock(return_value=httpx.Response(404))
    with pytest.raises(SleeperNotFound):
        await client.get_user("ghost")


@respx.mock
async def test_get_user_null_body_raises_not_found(client: SleeperClient) -> None:
    """Sleeper sometimes answers an unknown username with 200 + `null`."""
    respx.get(f"{BASE}/user/ghost").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(SleeperNotFound):
        await client.get_user("ghost")


@respx.mock
async def test_404_is_not_retried(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ghost").mock(return_value=httpx.Response(404))
    with pytest.raises(SleeperNotFound):
        await client.get_user("ghost")
    assert route.call_count == 1


@respx.mock
async def test_other_4xx_raises_sleeper_error_without_retry(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ryan").mock(return_value=httpx.Response(403, text="nope"))
    with pytest.raises(SleeperError) as exc:
        await client.get_user("ryan")
    assert not isinstance(exc.value, SleeperNotFound)
    assert not isinstance(exc.value, SleeperUnavailable)
    assert route.call_count == 1


class _RecordedSleep:
    """Stands in for the retry sleeper: records each wait instead of taking it."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


@respx.mock
async def test_429_is_retried_after_the_retry_after_it_asks_for(settings: Settings) -> None:
    """Rate limiting is "slow down", not "you asked wrongly": wait, then try again."""
    sleep = _RecordedSleep()
    route = respx.get(f"{BASE}/user/ryan").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "3"}, text="slow down"),
            httpx.Response(200, json={"user_id": "12345"}),
        ]
    )
    async with SleeperClient(settings, retry_wait=0, sleep=sleep) as c:
        user = await c.get_user("ryan")

    assert user["user_id"] == "12345"
    assert route.call_count == 2
    assert sleep.waits == [3.0]


@respx.mock
async def test_a_long_retry_after_is_capped_and_a_missing_one_falls_back_to_backoff(
    settings: Settings,
) -> None:
    sleep = _RecordedSleep()
    route = respx.get(f"{BASE}/user/ryan").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(429),  # no hint: plain backoff (0 in this client)
            httpx.Response(200, json={"user_id": "12345"}),
        ]
    )
    async with SleeperClient(settings, retry_wait=0, sleep=sleep) as c:
        await c.get_user("ryan")

    assert route.call_count == 3
    assert sleep.waits == [MAX_RETRY_AFTER, 0.0]


@respx.mock
async def test_persistent_429_gives_up_as_unavailable(settings: Settings) -> None:
    sleep = _RecordedSleep()
    route = respx.get(f"{BASE}/user/ryan").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "1"})
    )
    async with SleeperClient(settings, retry_wait=0, sleep=sleep) as c:
        with pytest.raises(SleeperUnavailable, match="429"):
            await c.get_user("ryan")

    assert route.call_count == 3
    assert sleep.waits == [1.0, 1.0]


def test_retry_after_reads_seconds_and_http_dates() -> None:
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

    def hint(value: str) -> float | None:
        return retry_after_seconds(httpx.Response(429, headers={"Retry-After": value}), now=now)

    assert hint("2.5") == 2.5
    assert hint("Sun, 27 Sep 2026 12:00:04 GMT") == 4.0
    assert hint("Sun, 27 Sep 2026 11:00:00 GMT") == 0.0  # already past
    assert hint("-5") == 0.0
    assert hint("600") == MAX_RETRY_AFTER
    assert hint("soon") is None
    assert retry_after_seconds(httpx.Response(429)) is None


@respx.mock
async def test_retries_on_500_then_succeeds(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ryan").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(503),
            httpx.Response(200, json={"user_id": "12345"}),
        ]
    )
    user = await client.get_user("ryan")
    assert user["user_id"] == "12345"
    assert route.call_count == 3


@respx.mock
async def test_gives_up_after_three_attempts(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ryan").mock(return_value=httpx.Response(500))
    with pytest.raises(SleeperUnavailable):
        await client.get_user("ryan")
    assert route.call_count == 3


@respx.mock
async def test_retries_transport_errors(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/user/ryan").mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"user_id": "12345"}),
        ]
    )
    assert (await client.get_user("ryan"))["user_id"] == "12345"
    assert route.call_count == 2


@respx.mock
async def test_transport_error_exhausted_raises_unavailable(client: SleeperClient) -> None:
    respx.get(f"{BASE}/user/ryan").mock(side_effect=httpx.ConnectTimeout("nope"))
    with pytest.raises(SleeperUnavailable):
        await client.get_user("ryan")


@respx.mock
async def test_get_leagues(client: SleeperClient) -> None:
    respx.get(f"{BASE}/user/12345/leagues/nfl/2026").mock(
        return_value=httpx.Response(200, json=[{"league_id": "999", "name": "Dynasty"}])
    )
    leagues = await client.get_leagues("12345", 2026)
    assert leagues[0]["league_id"] == "999"


@respx.mock
async def test_get_leagues_empty(client: SleeperClient) -> None:
    respx.get(f"{BASE}/user/12345/leagues/nfl/2026").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    assert await client.get_leagues("12345", 2026) == []


@respx.mock
async def test_get_league_and_rosters_and_matchups(client: SleeperClient) -> None:
    respx.get(f"{BASE}/league/999").mock(
        return_value=httpx.Response(
            200, json={"league_id": "999", "scoring_settings": {"rec": 1.0}}
        )
    )
    respx.get(f"{BASE}/league/999/rosters").mock(
        return_value=httpx.Response(200, json=[{"roster_id": 1, "players": ["1", "2"]}])
    )
    respx.get(f"{BASE}/league/999/matchups/3").mock(
        return_value=httpx.Response(
            200,
            json=[{"roster_id": 1, "starters": ["1"], "players_points": {"1": 12.4, "2": 20.1}}],
        )
    )

    league = await client.get_league("999")
    assert league["scoring_settings"]["rec"] == 1.0
    rosters = await client.get_rosters("999")
    assert rosters[0]["players"] == ["1", "2"]
    matchups = await client.get_matchups("999", 3)
    assert matchups[0]["players_points"]["2"] == 20.1


@respx.mock
async def test_get_users_returns_league_members(client: SleeperClient) -> None:
    """Team labels for /v1/team-report: display name plus optional team_name."""
    respx.get(f"{BASE}/league/999/users").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "user_id": "u1",
                    "display_name": "ryan",
                    "metadata": {"team_name": "Play Clock FC"},
                },
                {"user_id": "u2", "display_name": "rival", "metadata": {}},
            ],
        )
    )
    users = await client.get_users("999")
    assert [u["display_name"] for u in users] == ["ryan", "rival"]
    assert users[0]["metadata"]["team_name"] == "Play Clock FC"


@respx.mock
async def test_get_users_empty(client: SleeperClient) -> None:
    respx.get(f"{BASE}/league/999/users").mock(
        return_value=httpx.Response(
            200, content=b"null", headers={"content-type": "application/json"}
        )
    )
    assert await client.get_users("999") == []


@respx.mock
async def test_get_league_missing_raises(client: SleeperClient) -> None:
    respx.get(f"{BASE}/league/000").mock(return_value=httpx.Response(404))
    with pytest.raises(SleeperNotFound):
        await client.get_league("000")


@respx.mock
async def test_get_trending_shape_and_params(client: SleeperClient) -> None:
    route = respx.get(f"{BASE}/players/nfl/trending/add").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"player_id": 9502, "count": 51234},
                {"player_id": "4046", "count": 20111},
                {"count": 5},  # malformed row is skipped
            ],
        )
    )
    entries = await client.get_trending("add", lookback_hours=12, limit=5)
    assert entries == [
        TrendingEntry(player_id="9502", count=51234),
        TrendingEntry(player_id="4046", count=20111),
    ]
    request = route.calls[0].request
    assert request.url.params["lookback_hours"] == "12"
    assert request.url.params["limit"] == "5"


@respx.mock
async def test_get_trending_drop(client: SleeperClient) -> None:
    respx.get(f"{BASE}/players/nfl/trending/drop").mock(
        return_value=httpx.Response(200, json=[{"player_id": "1", "count": 9}])
    )
    entries = await client.get_trending("drop")
    assert entries[0].count == 9


async def test_get_trending_rejects_bad_kind(client: SleeperClient) -> None:
    with pytest.raises(ValueError):
        await client.get_trending("sideways")  # type: ignore[arg-type]


@respx.mock
async def test_get_players_dump(client: SleeperClient) -> None:
    respx.get(f"{BASE}/players/nfl").mock(
        return_value=httpx.Response(
            200, json={"4046": {"full_name": "Patrick Mahomes", "gsis_id": "00-0033873"}}
        )
    )
    players = await client.get_players()
    assert players["4046"]["gsis_id"] == "00-0033873"


@respx.mock
async def test_custom_transport_injection(settings: Settings) -> None:
    """A caller-supplied transport is honoured (used by fixture-driven evals)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"user_id": "injected"})

    transport = httpx.MockTransport(handler)
    async with SleeperClient(settings, transport=transport, retry_wait=0) as c:
        assert (await c.get_user("anyone"))["user_id"] == "injected"


@respx.mock
async def test_base_url_from_settings_is_respected() -> None:
    settings = Settings(_env_file=None, sleeper_base_url="https://sleeper.test/v1/")  # type: ignore[call-arg]
    route = respx.get("https://sleeper.test/v1/user/ryan").mock(
        return_value=httpx.Response(200, json={"user_id": "1"})
    )
    async with SleeperClient(settings, retry_wait=0) as c:
        await c.get_user("ryan")
    assert route.called


async def test_path_segments_cannot_leave_their_slot(settings: Settings) -> None:
    """Caller-supplied ids are quoted; ``..`` and friends never reach the wire."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json=[] if request.url.path.endswith("/picks") else {"a": 1})

    async with SleeperClient(
        settings, transport=httpx.MockTransport(handler), retry_wait=0
    ) as client:
        await client.get_user("a/../../players/nfl?x=1")
        await client.get_draft_picks("1/../../x")
        for unsafe in ("..", ".", ""):
            with pytest.raises(SleeperNotFound):
                await client.get_draft(unsafe)

    assert seen == [
        "/v1/user/a%2F..%2F..%2Fplayers%2Fnfl%3Fx%3D1",
        "/v1/draft/1%2F..%2F..%2Fx/picks",
    ]
