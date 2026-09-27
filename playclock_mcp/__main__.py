"""``python -m playclock_mcp`` — run the MCP server over stdio."""

from __future__ import annotations

import sys

from playclock_mcp.server import main

if __name__ == "__main__":
    sys.exit(main())
