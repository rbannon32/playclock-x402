"""MCP server exposing Play Clock's paid endpoints as tools an agent can buy.

Wiring only: :mod:`playclock_mcp.tools` decides what the tools are,
:mod:`playclock_mcp.payments` decides how they get paid for, and this module
connects the two to an MCP session.

Configuration is environment-only, matching the rest of the project:

==============================  ==========================================
``PLAYCLOCK_BASE_URL``          API origin to buy from.
``ALGORAND_MNEMONIC``           25-word mnemonic of the paying wallet.
``PLAYCLOCK_MOCK``              ``1`` to pay a ``X402_MODE=mock`` server.
``PLAYCLOCK_MAX_PRICE_USDC``    Hard ceiling per call (default 1.00).
``PLAYCLOCK_TIMEOUT_SECONDS``   HTTP timeout (default 240; capped below the replay window).
``PLAYCLOCK_STATE_DIR``         Where the payment journal lives.
==============================  ==========================================

The default timeout is 240 seconds (capped at 270, inside the server's 300s
replay window) rather than httpx's 5 because of the
asymmetry this whole package is built around: the server settles before its
handler returns, so giving up early does not save money, it only loses the
answer that was already paid for.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
import mcp.types as types
from mcp.server import ServerRequestContext
from mcp.server.lowlevel.server import Server

from playclock_mcp import __version__
from playclock_mcp.errors import PlayClockError, WalletError
from playclock_mcp.journal import REPLAY_WINDOW_SECONDS, PendingPayment
from playclock_mcp.payments import DEFAULT_MAX_PRICE_USDC, Payer, build_signer, purchase, recover
from playclock_mcp.tools import EndpointTool, build_tools

__all__ = ["PlayClockMCP", "build_server", "main"]

logger = logging.getLogger("playclock_mcp")

DEFAULT_BASE_URL = "https://api.playclock.xyz"
#: Must stay well inside the server's 300s replay window: a paid call that
#: times out is recoverable only by replaying it inside that window, so a
#: timeout at (or past) 300s leaves every timed-out payment unrecoverable.
DEFAULT_TIMEOUT_SECONDS = 240.0
MAX_TIMEOUT_SECONDS = REPLAY_WINDOW_SECONDS - 30.0

WALLET_TOOL = "playclock_wallet"
RECOVER_TOOL = "playclock_recover_payments"

INSTRUCTIONS = """\
Play Clock sells NFL fantasy football analysis one answer at a time, paid in USDC
micropayments on Algorand. Each tool whose description says COSTS spends real
money from the user's wallet when called.

Rules that matter:
- Check the price in the tool description before calling. Prefer the free
  playclock_trending_preview when the user just wants a look.
- Never call a paid tool speculatively or in a loop. One call, one answer.
- Every number in a response is traceable: `stats_cited` names the stat, the
  value and its source. Quote those rather than paraphrasing figures.
- Responses carry `meta.attribution`, which must be shown to the user when you
  present the data.
- If a call fails after payment, playclock_recover_payments replays it for free.
"""


class PlayClockMCP:
    """Holds the HTTP client, the payer, and the lazily-discovered tool list."""

    def __init__(
        self,
        *,
        base_url: str,
        payer: Payer,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._payer = payer
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)
        self._endpoints: dict[str, EndpointTool] | None = None
        self._network = ""

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    # -- discovery ---------------------------------------------------------

    async def endpoints(self) -> dict[str, EndpointTool]:
        """Fetch and cache the tool list derived from the live service.

        Discovery failures are not fatal: the two local tools still work, and
        one of them is how a user recovers a lost payment while the API is down.
        """
        if self._endpoints is None:
            try:
                catalog = (await self._http.get("/v1/catalog")).json()
                openapi = (await self._http.get("/openapi.json")).json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("discovery failed against %s: %s", self._base_url, exc)
                return {}
            self._endpoints = {e.tool.name: e for e in build_tools(catalog, openapi)}
            self._network = str(catalog.get("network") or "")
            logger.info(
                "discovered %d endpoints at %s (network=%s)",
                len(self._endpoints),
                self._base_url,
                self._network or "unknown",
            )
            if self._network == "testnet" and not self._payer.is_mock:
                # A real wallet pointed at TestNet spends worthless USDC for
                # real analysis, and none of it counts anywhere. Pointing
                # PLAYCLOCK_BASE_URL at a TestNet deployment with a funded
                # MainNet mnemonic is the likeliest way to configure this wrong.
                logger.warning(
                    "%s is on TESTNET. Payments will use TestNet USDC and count "
                    "for nothing. Set PLAYCLOCK_BASE_URL to the MainNet host.",
                    self._base_url,
                )
        return self._endpoints

    async def list_tools(self) -> list[types.Tool]:
        """Every endpoint tool, plus the two local wallet tools."""
        endpoints = await self.endpoints()
        tools = [e.tool for e in endpoints.values()]
        tools.append(
            types.Tool(
                name=WALLET_TOOL,
                title="Wallet and spend status",
                description=(
                    "Show the paying wallet, the per-call price ceiling, what has been "
                    "spent this session, and any payments still awaiting an answer. "
                    "Free — reads local state only."
                ),
                input_schema={"type": "object", "properties": {}},
            )
        )
        tools.append(
            types.Tool(
                name=RECOVER_TOOL,
                title="Recover a paid-but-lost answer",
                description=(
                    "Replay any payment that was signed and sent but never got a "
                    "response — a timeout, a crash, a dropped connection. Free: the "
                    "server returns the already-paid-for answer without charging "
                    "again. Call this when a paid tool failed mid-flight."
                ),
                input_schema={"type": "object", "properties": {}},
            )
        )
        return tools

    # -- dispatch ----------------------------------------------------------

    async def call(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Run one tool call, turning any failure into a readable tool error."""
        try:
            if name == WALLET_TOOL:
                return _ok(self._wallet_status())
            if name == RECOVER_TOOL:
                return await self._recover()
            return await self._buy(name, arguments)
        except PlayClockError as exc:
            return _error(exc.as_text())
        except Exception as exc:  # noqa: BLE001 - a tool must never kill the session
            logger.exception("tool %s failed", name)
            return _error(f"{type(exc).__name__}: {exc}")

    async def _buy(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """Call one endpoint, paying if it asks."""
        endpoints = await self.endpoints()
        endpoint = endpoints.get(name)
        if endpoint is None:
            return _error(
                f"unknown tool {name!r}. The service at {self._base_url} advertises: "
                f"{', '.join(sorted(endpoints)) or '(discovery failed)'}"
            )

        params, body = endpoint.split_arguments(arguments or {})
        result = await purchase(
            self._http,
            self._payer,
            tool=name,
            method=endpoint.method,
            path=endpoint.path,
            params=params,
            body=body or None,
        )

        note = ""
        if result.price:
            txid = getattr(result.receipt, "transaction", None) or ""
            if result.settled:
                settled = "settled"
            elif result.receipt is None:
                settled = "settlement status unknown (no receipt came back; check the wallet)"
            else:
                settled = "NOT settled (you were not charged)"
            note = f"\n\nPaid {result.price:.6f} USDC — {settled}."
            if txid:
                note += f" txid {txid}"
        return _ok(result.body, note=note)

    async def _recover(self) -> types.CallToolResult:
        """Replay journalled payments and report what came back."""
        results = await recover(self._http, self._payer)
        pending = self._payer.journal.pending()
        # An entry still inside the replay window survived the replay because
        # the server has not answered it yet (payment_in_progress, a gateway
        # timeout). Calling that "lost" would send an agent off to buy again.
        in_progress = [_describe(e) for e in pending if e.replayable]
        expired = [_describe(e) for e in pending if not e.replayable]
        report: dict[str, Any] = {"recovered": len(results)}
        if results:
            report["answers"] = [r.body for r in results]
        if in_progress:
            report["still_in_progress"] = in_progress
        if expired:
            report["unrecoverable"] = expired
        messages = []
        if not results and not pending:
            messages.append("No payments are awaiting an answer.")
        if in_progress:
            messages.append(
                "Some paid answers are still being prepared or the server did not "
                "respond. They are recoverable at no extra cost: call this tool "
                "again in a few seconds. Do NOT buy them again."
            )
        if expired:
            messages.append(
                "Some payments are past the server's replay window, so their answers "
                "cannot be retrieved. They are listed so the loss is visible rather "
                "than silent."
            )
        if messages:
            report["message"] = " ".join(messages)
        note = (
            f"\n\nRecovered {len(results)} paid answer(s) at no additional cost." if results else ""
        )
        return _ok(report, note=note)

    def _wallet_status(self) -> dict[str, Any]:
        """Local state only — no network, no cost."""
        pending = self._payer.journal.pending()
        return {
            "base_url": self._base_url,
            "network": self._network or "unknown (discovery has not run)",
            "spending_real_usdc": bool(self._network == "mainnet" and not self._payer.is_mock),
            "wallet": self._payer.label,
            "max_price_usdc_per_call": self._payer.max_price_usdc,
            "spent_this_session_usdc": round(self._payer.spent_usdc, 6),
            "paid_calls_this_session": self._payer.calls_paid,
            "payments_awaiting_answer": [
                {
                    "tool": e.tool,
                    "price_usdc": e.price_usdc,
                    "age_seconds": round(e.age_seconds),
                    "recoverable": e.replayable,
                }
                for e in pending
            ],
        }


def _describe(entry: PendingPayment) -> str:
    """One line naming a journalled payment for the agent."""
    return f"{entry.tool} ({entry.price_usdc:.2f} USDC, {entry.age_seconds / 60:.0f} min ago)"


def _ok(payload: Any, *, note: str = "") -> types.CallToolResult:
    """A successful tool result: JSON for the model, plus any payment note."""
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"{text}{note}")],
        structured_content=payload if isinstance(payload, dict) else None,
    )


def _error(message: str) -> types.CallToolResult:
    """A failed tool result. ``is_error`` so the model treats it as a failure."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


def build_server(app: PlayClockMCP) -> Server[None]:
    """Wire a :class:`PlayClockMCP` onto an MCP low-level server.

    MCP 2.x takes handlers as constructor arguments rather than decorators, and
    ``types.Tool`` spells the schema field ``input_schema``; both changed from
    1.x, so do not port examples from the older SDK without checking.
    """

    async def on_list_tools(
        _ctx: ServerRequestContext[None],
        _params: types.PaginatedRequestParams | None,
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await app.list_tools())

    async def on_call_tool(
        _ctx: ServerRequestContext[None],
        params: types.CallToolRequestParams,
    ) -> types.CallToolResult:
        return await app.call(params.name, dict(params.arguments or {}))

    return Server(
        "playclock",
        version=__version__,
        title="Play Clock",
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def build_app_from_env() -> PlayClockMCP:
    """Construct the server from environment configuration.

    Raises:
        WalletError: Neither a mnemonic nor mock mode was configured.
    """
    mock = os.environ.get("PLAYCLOCK_MOCK", "").strip().lower() in {"1", "true", "yes"}
    try:
        signer = build_signer(mock=mock, mnemonic_phrase=os.environ.get("ALGORAND_MNEMONIC"))
    except WalletError as exc:
        raise WalletError(
            "Play Clock MCP has no way to pay. Set ALGORAND_MNEMONIC to a funded, "
            "USDC-opted-in 25-word Algorand mnemonic, or PLAYCLOCK_MOCK=1 to explore "
            "against a server running X402_MODE=mock."
        ) from exc

    try:
        ceiling = float(os.environ.get("PLAYCLOCK_MAX_PRICE_USDC", DEFAULT_MAX_PRICE_USDC))
    except ValueError:
        ceiling = DEFAULT_MAX_PRICE_USDC
    try:
        timeout = float(os.environ.get("PLAYCLOCK_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    except ValueError:
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)

    return PlayClockMCP(
        base_url=os.environ.get("PLAYCLOCK_BASE_URL", DEFAULT_BASE_URL),
        payer=Payer(signer=signer, max_price_usdc=ceiling),
        timeout=timeout,
    )


async def serve() -> None:
    """Run the server over stdio until the client disconnects."""
    from mcp.server.stdio import stdio_server

    app = build_app_from_env()
    server = build_server(app)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        await app.aclose()


def main() -> int:
    """Entry point. Logs to stderr — stdout belongs to the MCP transport."""
    import anyio

    logging.basicConfig(
        level=os.environ.get("PLAYCLOCK_LOG_LEVEL", "INFO").upper(),
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        anyio.run(serve)
    except WalletError as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 0
    return 0
