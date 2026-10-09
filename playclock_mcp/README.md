# Play Clock MCP server

Buy fantasy football analysis from inside an AI assistant, one answer at a time,
paid in USDC on Algorand. Your agent gets tools; each paid tool call signs a real
micropayment from your wallet.

```
Claude / Cursor / any MCP client
        │  tool call: playclock_roster { sleeper_username: "ryan" }
        ▼
playclock_mcp ─── POST /v1/roster ────────────► 402 Payment Required (0.35 USDC)
        │                                              │
        │  sign USDC transfer, journal the header      │
        └──────── retry with PAYMENT-SIGNATURE ───────►│
                                                       ▼
                                          settle via GoPlausible → analysis
```

## Install

```bash
uv sync --extra mcp        # from the repo root
```

## Try it with no wallet and no chain

Run the API in mock mode, point the server at it, and nothing costs anything:

```bash
X402_MODE=mock uv run uvicorn api.main:app --port 8080
```

```jsonc
// Claude Code: .mcp.json   |   Claude Desktop: claude_desktop_config.json
{
  "mcpServers": {
    "playclock": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/playclock", "python", "-m", "playclock_mcp"],
      "env": {
        "PLAYCLOCK_BASE_URL": "http://localhost:8080",
        "PLAYCLOCK_MOCK": "1"
      }
    }
  }
}
```

## Pay real USDC

Drop `PLAYCLOCK_MOCK` and supply a funded wallet. The wallet must hold ALGO for
fees **and be opted into the USDC ASA** — settlement otherwise fails at simulate
and surfaces as a second 402 with nothing billed.

```jsonc
{
  "mcpServers": {
    "playclock": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/playclock", "python", "-m", "playclock_mcp"],
      "env": {
        "PLAYCLOCK_BASE_URL": "https://api.playclock.xyz",
        "ALGORAND_MNEMONIC": "word word word ... word",
        "PLAYCLOCK_MAX_PRICE_USDC": "0.50"
      }
    }
  }
}
```

`PLAYCLOCK_MAX_PRICE_USDC` is a hard per-call ceiling, enforced **before**
anything is signed and again inside the SDK. A quote above it is refused with
both numbers named, and the model is told not to retry on its own.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PLAYCLOCK_BASE_URL` | the live deployment | API origin to buy from |
| `ALGORAND_MNEMONIC` | — | 25-word mnemonic of the paying wallet |
| `PLAYCLOCK_MOCK` | off | pay a server running `X402_MODE=mock` |
| `PLAYCLOCK_MAX_PRICE_USDC` | `1.00` | hard ceiling per call |
| `PLAYCLOCK_TIMEOUT_SECONDS` | `240` | HTTP timeout; capped at 270 so a timed-out payment is still inside the 300s replay window |
| `PLAYCLOCK_STATE_DIR` | `~/.playclock` | where the payment journal lives |
| `PLAYCLOCK_LOG_LEVEL` | `INFO` | stderr log level |

## Tools

Tools are **derived from the service at startup**, not hard-coded: the server
reads `/v1/catalog` for prices and cache policy and `/openapi.json` for the real
request schemas. A new paid endpoint becomes a tool with its real schema and its
real price the moment it ships.

Today that is one tool per endpoint —
`playclock_trending`, `playclock_sleepers`, `playclock_player`,
`playclock_matchup`, `playclock_roster`, `playclock_waivers`, `playclock_report`,
`playclock_team_report`, `playclock_draft_board`, `playclock_draft_report` — plus the
free `playclock_health` and `playclock_trending_preview`, and two local tools:

- **`playclock_wallet`** — paying address, ceiling, spend this session, and any
  payment still awaiting an answer. Reads local state; costs nothing.
- **`playclock_recover_payments`** — replays a payment that was signed and sent
  but never answered. Free, and it does not settle a second time.

Every paid tool's description states its price, because the model choosing the
tool is the party deciding to spend the money.

## Why there is a payment journal

Play Clock settles **before** its handler returns. A call that times out has
therefore already moved USDC, and the only way to get the answer is to replay
the identical request with the identical `PAYMENT-SIGNATURE`.

So every payment is written to `~/.playclock/pending-payments.json` (mode
`0600`) *before* the paid request goes out, and cleared only once a response is
in hand. If a call dies mid-flight, `playclock_recover_payments` gets the answer
back for free — the server returns the cached receipt without settling again,
because the payment is bound to that exact request.

Entries past the server's 300-second idempotency window stop being replayable
but stay listed, so a lost payment is visible rather than silent.

## Testing

```bash
uv run pytest tests/test_mcp_tools.py tests/test_mcp_journal.py tests/test_mcp_server.py -q
```

The end-to-end tests drive a real `create_app()` over ASGI in mock mode, so the
payment dependency, the settle-after-2xx wrapper and the response cache are all
production objects. One test deliberately drops the paid response to prove the
recovery path returns the answer without a second settlement.
