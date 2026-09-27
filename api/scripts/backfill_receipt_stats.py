"""Backfill the bounded receipt-stat rollups.

Run after the receipt-rollup writer is deployed. It is repeatable: every
historical receipt has a durable event marker, and concurrent new settlements
write through the same transaction. The completion marker is written only after
every receipt returned by the indexed network query has been applied.

Invoked from a one-off API container as ``python -m
api.scripts.backfill_receipt_stats``. It lives under ``api/`` because that is
the only tree the runtime image copies, and ``uv`` is a builder-stage tool that
the runtime image does not carry -- ``python`` on ``PATH`` is already the venv
interpreter there.
"""

from __future__ import annotations

import asyncio

from api.core.config import get_settings
from api.core.store import get_store
from api.x402.receipts import backfill_receipt_stats
from api.x402.schemas_compat import network_caip2


async def main() -> None:
    settings = get_settings()
    store = get_store(settings)
    network = network_caip2(settings)
    count = await backfill_receipt_stats(store, network)
    print(f"Backfilled {count} receipts for {network}.")


if __name__ == "__main__":
    asyncio.run(main())
