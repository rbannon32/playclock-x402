"""Play Clock MCP server — an agent's wallet-side view of the paid API.

This package is a **client**, not a service. It runs on the user's machine,
holds their Algorand key, and pays Play Clock's x402 endpoints per tool call so
that an assistant (Claude Code, Claude Desktop, Cursor, anything speaking MCP)
can buy one fantasy football answer at a time.

Why it lives in this repo rather than beside ``examples/agent``
--------------------------------------------------------------
``examples/agent/fantasy_agent.py`` is a *teaching artifact*: one file, heavy
prose, a CLI that narrates the protocol step by step. This package is a
*product surface* with different obligations — it must never lose a payment, it
must present typed tools to a model, and it must not print anything to stdout
(stdout is the MCP transport). The two deliberately do not share code; see
``TODO.md`` for the convergence note.

The one rule that shapes everything here
----------------------------------------
Settlement happens **before** the paid handler returns (DESIGN_NOTES §2 and
"ADK on Vertex"). A call that times out has therefore already moved money, and
the *only* way to recover the answer is to replay the identical request with the
identical ``PAYMENT-SIGNATURE``. So this package journals every payment header
to disk **before** it is sent and clears it only once a response is in hand.
That is the bug the bundled example agent still has, and in an MCP tool — where
a timeout is routine — it would be money quietly gone.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
