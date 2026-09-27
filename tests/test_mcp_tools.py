"""Tool derivation from the service's own catalog and OpenAPI documents.

These tests pin the contract that lets a new paid endpoint become an MCP tool
with no code change here: the catalog supplies price and cache policy, the
OpenAPI document supplies the real request schema, and nothing about the
endpoint list is written down twice.
"""

from __future__ import annotations

from typing import Any

from playclock_mcp.tools import FREE_TOOLS, build_tools, tool_name_for


def make_catalog(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"service": "Play Clock", "endpoints": list(entries)}


CATALOG_TRENDING = {
    "path": "/v1/trending",
    "method": "GET",
    "price_usdc": 0.10,
    "description": "Trending adds and drops.",
    "free": False,
    "cache_ttl_seconds": 21600,
}
CATALOG_PLAYER = {
    "path": "/v1/player",
    "method": "POST",
    "price_usdc": 0.15,
    "description": "One player, deep.",
    "free": False,
    "cache_ttl_seconds": None,
}
CATALOG_HEALTH = {
    "path": "/v1/health",
    "method": "GET",
    "price_usdc": 0.0,
    "description": "Health.",
    "free": True,
    "cache_ttl_seconds": None,
}
CATALOG_SECRET_FREE = {
    "path": "/v1/internal",
    "method": "GET",
    "price_usdc": 0.0,
    "description": "Not for agents.",
    "free": True,
    "cache_ttl_seconds": None,
}

OPENAPI: dict[str, Any] = {
    "paths": {
        "/v1/trending": {
            "get": {
                "summary": "Trending",
                "parameters": [
                    {
                        "name": "week",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                        "description": "NFL week.",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "integer", "default": 25},
                    },
                ],
            }
        },
        "/v1/player": {
            "post": {
                "summary": "Player",
                "parameters": [
                    {
                        "name": "week",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer"},
                    }
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/PlayerRequest"}
                        }
                    }
                },
            }
        },
        "/v1/health": {"get": {"summary": "Health"}},
        "/v1/internal": {"get": {"summary": "Internal"}},
    },
    "components": {
        "schemas": {
            "PlayerRequest": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "note": {"$ref": "#/components/schemas/Note"},
                },
                "required": ["name"],
            },
            "Note": {"type": "object", "properties": {"text": {"type": "string"}}},
            "Unrelated": {"type": "object", "properties": {"x": {"type": "integer"}}},
        }
    },
}


def test_tool_name_converts_path_to_key_spelling() -> None:
    # URL paths use hyphens, endpoint keys use underscores (CLAUDE.md).
    assert tool_name_for("/v1/team-report") == "playclock_team_report"
    assert tool_name_for("/v1/trending/preview") == "playclock_trending_preview"


def test_paid_endpoints_become_tools_with_price_in_the_description() -> None:
    tools = build_tools(make_catalog(CATALOG_TRENDING), OPENAPI)

    assert len(tools) == 1
    entry = tools[0]
    assert entry.tool.name == "playclock_trending"
    assert entry.price_usdc == 0.10
    assert entry.free is False
    # The model choosing the tool is the party spending the money.
    assert "COSTS 0.10 USDC" in (entry.tool.description or "")
    assert "6h" in (entry.tool.description or "")


def test_uncached_endpoint_is_labelled_as_always_fresh() -> None:
    tools = build_tools(make_catalog(CATALOG_PLAYER), OPENAPI)
    assert "never cached" in (tools[0].tool.description or "")


def test_get_parameters_become_the_input_schema() -> None:
    entry = build_tools(make_catalog(CATALOG_TRENDING), OPENAPI)[0]
    schema = entry.tool.input_schema

    assert set(schema["properties"]) == {"week", "limit"}
    assert schema["required"] == ["limit"]
    assert schema["properties"]["week"]["description"] == "NFL week."


def test_post_body_is_inlined_and_merged_with_query_parameters() -> None:
    entry = build_tools(make_catalog(CATALOG_PLAYER), OPENAPI)[0]
    schema = entry.tool.input_schema

    # `week` is a query parameter, `name` comes from the request body.
    assert set(schema["properties"]) == {"week", "name", "note"}
    assert schema["required"] == ["name"]


def test_only_reachable_definitions_travel_with_the_schema() -> None:
    # Shipping the whole component map would attach every response model to
    # every request schema — pure token cost on each tool listing.
    entry = build_tools(make_catalog(CATALOG_PLAYER), OPENAPI)[0]
    defs = entry.tool.input_schema["$defs"]

    assert set(defs) == {"Note"}
    assert entry.tool.input_schema["properties"]["note"]["$ref"] == "#/$defs/Note"


def test_arguments_split_by_openapi_parameter_list_not_by_method() -> None:
    entry = build_tools(make_catalog(CATALOG_PLAYER), OPENAPI)[0]

    params, body = entry.split_arguments({"week": 4, "name": "Bijan Robinson"})

    assert params == {"week": 4}
    assert body == {"name": "Bijan Robinson"}


def test_get_endpoints_send_everything_as_query_parameters() -> None:
    entry = build_tools(make_catalog(CATALOG_TRENDING), OPENAPI)[0]
    params, body = entry.split_arguments({"week": 4, "limit": 10})
    assert params == {"week": 4, "limit": 10}
    assert body == {}


def test_none_valued_arguments_are_dropped_rather_than_sent() -> None:
    # A model filling an optional field with null must not turn into `?week=None`.
    entry = build_tools(make_catalog(CATALOG_TRENDING), OPENAPI)[0]
    params, _ = entry.split_arguments({"week": None, "limit": 10})
    assert params == {"limit": 10}


def test_allowlisted_free_routes_are_exposed_and_others_are_not() -> None:
    tools = build_tools(make_catalog(CATALOG_HEALTH, CATALOG_SECRET_FREE), OPENAPI)

    names = {t.tool.name for t in tools}
    assert names == {"playclock_health"}
    assert "/v1/health" in FREE_TOOLS


def test_endpoint_missing_from_openapi_is_skipped_not_crashed() -> None:
    ghost = {**CATALOG_TRENDING, "path": "/v1/ghost"}
    assert build_tools(make_catalog(ghost), OPENAPI) == []
