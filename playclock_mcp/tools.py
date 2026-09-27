"""Turn Play Clock's own catalog into MCP tool definitions.

Nothing about the endpoint list is hard-coded here. The server fetches
``GET /v1/catalog`` (prices, methods, cache policy, which routes are free) and
``GET /openapi.json`` (the real request schemas) at startup and derives one MCP
tool per endpoint from the two.

That is a deliberate coupling choice rather than laziness. The catalog is
already the machine-readable contract this API asks agents to plan against
(PRD §2, "agent-native distribution"), and the OpenAPI document is generated
from the same Pydantic models the routes validate against. Deriving tools from
both means a new paid endpoint — a draft board, say — becomes an MCP tool with
its real schema and its real price the moment it ships, with no second copy of
the contract to drift.

The price goes in the tool *description* on purpose: the model choosing a tool
is the party deciding to spend money, and it cannot weigh that against a number
it never sees.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

import mcp.types as types

__all__ = ["EndpointTool", "build_tools", "tool_name_for"]

#: Free routes worth exposing. ``/v1/catalog`` is omitted: the server already
#: read it, and re-exposing it invites a model to spend a turn rediscovering
#: what its own tool list already says.
FREE_TOOLS = ("/v1/health", "/v1/trending/preview")

#: Prefix so the tools stay legible when a client has many servers connected.
TOOL_PREFIX = "playclock_"


def tool_name_for(path: str) -> str:
    """Derive a stable MCP tool name from a URL path.

    ``/v1/team-report`` -> ``playclock_team_report``. Hyphens become underscores
    because URL paths use hyphens while endpoint keys use underscores (CLAUDE.md),
    and a tool name is closer to a key than to a path.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", path.removeprefix("/v1/").lower()).strip("_")
    return f"{TOOL_PREFIX}{slug}"


@dataclass(frozen=True)
class EndpointTool:
    """One MCP tool bound to one Play Clock endpoint.

    Attributes:
        tool: The MCP tool definition handed to the client.
        method: ``GET`` or ``POST``.
        path: Request path to call.
        price_usdc: Quoted price; ``0.0`` for free routes.
        free: Whether calling this ever costs money.
        query_params: Names that belong in the query string rather than the body.
    """

    tool: types.Tool
    method: str
    path: str
    price_usdc: float
    free: bool
    query_params: frozenset[str]

    def split_arguments(self, arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Split tool arguments into ``(query params, JSON body)``.

        A POST endpoint could still declare query parameters, so membership in
        the OpenAPI parameter list — not the HTTP method — decides where each
        argument goes.
        """
        params = {k: v for k, v in arguments.items() if k in self.query_params and v is not None}
        if self.method == "GET":
            return params, {}
        body = {k: v for k, v in arguments.items() if k not in self.query_params}
        return params, body


def _referenced_names(node: Any, out: set[str]) -> set[str]:
    """Collect every ``#/components/schemas/X`` name reachable from ``node``."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _referenced_names(value, out)
    elif isinstance(node, list):
        for item in node:
            _referenced_names(item, out)
    return out


def _resolve_body_schema(openapi: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    """Inline a request body's component schema so it stands alone.

    The OpenAPI document refers to ``#/components/schemas/X``; an MCP client
    receives only the tool's own schema, so the reference has to travel with it.

    Only *reachable* definitions are attached. Copying the whole component map
    would be simpler and is what the first cut did, but it ships every response
    model with every request schema — tens of kilobytes of irrelevant shapes
    that the model pays attention costs to read on every tool listing. The
    closure is computed transitively, so nested models still resolve.
    """
    body = operation.get("requestBody") or {}
    schema = (body.get("content") or {}).get("application/json", {}).get("schema") or {}
    ref = schema.get("$ref")
    if not ref:
        return copy.deepcopy(schema)
    name = ref.rsplit("/", 1)[-1]
    components = (openapi.get("components") or {}).get("schemas") or {}
    resolved = copy.deepcopy(components.get(name) or {})
    if not resolved:
        return {}

    wanted = _referenced_names(resolved, set())
    seen: set[str] = set()
    while wanted - seen:
        for target in list(wanted - seen):
            seen.add(target)
            wanted |= _referenced_names(components.get(target) or {}, set())
    wanted.discard(name)

    defs = {k: copy.deepcopy(components[k]) for k in sorted(wanted) if k in components}
    if defs:
        resolved["$defs"] = defs
    return _rewrite_refs(resolved)


def _rewrite_refs(node: Any) -> Any:
    """Rewrite ``#/components/schemas/X`` references to ``#/$defs/X`` in place."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            node["$ref"] = ref.replace("#/components/schemas/", "#/$defs/")
        for value in node.values():
            _rewrite_refs(value)
    elif isinstance(node, list):
        for item in node:
            _rewrite_refs(item)
    return node


def _query_schema(operation: dict[str, Any]) -> tuple[dict[str, Any], list[str], frozenset[str]]:
    """Build ``(properties, required, names)`` from an operation's query parameters."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    names: set[str] = set()
    for parameter in operation.get("parameters") or []:
        if parameter.get("in") != "query":
            continue
        name = parameter["name"]
        names.add(name)
        schema = copy.deepcopy(parameter.get("schema") or {})
        if parameter.get("description") and "description" not in schema:
            schema["description"] = parameter["description"]
        properties[name] = schema
        if parameter.get("required"):
            required.append(name)
    return properties, required, frozenset(names)


def _describe(entry: dict[str, Any], operation: dict[str, Any]) -> str:
    """Compose the tool description: what it does, then what it costs."""
    summary = entry.get("description") or operation.get("summary") or ""
    if entry.get("free"):
        return f"{summary}\n\nFree — this call never costs anything."
    price = float(entry.get("price_usdc") or 0.0)
    lines = [summary, "", f"COSTS {price:.2f} USDC per call, paid from the configured wallet."]
    ttl = entry.get("cache_ttl_seconds")
    if ttl:
        lines.append(
            f"Answers are generated once per cycle and re-served for {int(ttl) // 3600}h, "
            "so repeat callers get the same board without a second generation."
        )
    else:
        lines.append("Personalized and never cached — every call generates a fresh answer.")
    return "\n".join(lines)


def build_tools(catalog: dict[str, Any], openapi: dict[str, Any]) -> list[EndpointTool]:
    """Derive the full tool list from a catalog document and an OpenAPI document.

    Free routes outside :data:`FREE_TOOLS` are skipped; every paid route in the
    catalog becomes a tool.
    """
    paths = openapi.get("paths") or {}
    tools: list[EndpointTool] = []

    for entry in catalog.get("endpoints") or []:
        path, method = entry.get("path"), (entry.get("method") or "GET").upper()
        free = bool(entry.get("free"))
        if not path or (free and path not in FREE_TOOLS):
            continue
        operation = (paths.get(path) or {}).get(method.lower())
        if operation is None:
            continue

        properties, required, query_names = _query_schema(operation)
        if method == "POST":
            body_schema = _resolve_body_schema(openapi, operation)
            properties.update(body_schema.get("properties") or {})
            required.extend(body_schema.get("required") or [])
            defs = body_schema.get("$defs")
        else:
            defs = None

        input_schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            input_schema["required"] = sorted(set(required))
        if defs:
            input_schema["$defs"] = defs

        tools.append(
            EndpointTool(
                tool=types.Tool(
                    name=tool_name_for(path),
                    title=operation.get("summary") or path,
                    description=_describe(entry, operation),
                    input_schema=input_schema,
                ),
                method=method,
                path=path,
                price_usdc=float(entry.get("price_usdc") or 0.0),
                free=free,
                query_params=query_names,
            )
        )
    return tools
