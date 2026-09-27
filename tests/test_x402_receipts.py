"""Receipt logging and aggregation tests.

``receipts/`` is our own proof of who paid for what — the challenge submission
asks for it, the paid-calls monitoring metric reads it, and a future free
``/v1/stats`` endpoint aggregates it as social proof. ``failed_paid_calls/``
records the other direction: an answer we produced and were never paid for.
"""

from __future__ import annotations

from typing import Any

import pytest

import api.x402.receipts as receipt_module
from api.core.store import FirestoreStore, MemoryStore, Store
from api.x402.receipts import (
    FAILED_PAID_CALLS_COLLECTION,
    RECEIPTS_COLLECTION,
    log_failed_paid_call,
    log_receipt,
    receipts_summary,
)

NETWORK = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="


async def test_log_receipt_writes_the_documented_shape(store: Store) -> None:
    doc_id = await log_receipt(
        store,
        txid="TXID1",
        payer="PAYER1",
        endpoint="trending",
        amount_usdc=0.10,
        network=NETWORK,
        ts="2026-10-07T13:05:00+00:00",
        payment_hash="a" * 64,
    )

    assert doc_id is not None
    docs = await store.list(RECEIPTS_COLLECTION)
    assert len(docs) == 1
    assert docs[0] == {
        "_id": doc_id,
        "txid": "TXID1",
        "payer": "PAYER1",
        "endpoint": "trending",
        "amount_usdc": 0.10,
        "network": NETWORK,
        "ts": "2026-10-07T13:05:00+00:00",
        "payment_hash": "a" * 64,
    }


async def test_log_receipt_defaults_the_timestamp(store: Store) -> None:
    await log_receipt(
        store, txid="T", payer=None, endpoint="report", amount_usdc=0.5, network=NETWORK
    )
    doc = (await store.list(RECEIPTS_COLLECTION))[0]
    assert doc["ts"].startswith("20")
    assert doc["payer"] is None
    assert "payment_hash" not in doc


async def test_log_failed_paid_call_records_the_loss(store: Store) -> None:
    await log_failed_paid_call(
        store,
        endpoint="team_report",
        payment_hash="b" * 64,
        error="insufficient_funds: mock settlement failed",
        payer="PAYER2",
        amount_usdc=0.75,
        network=NETWORK,
    )

    docs = await store.list(FAILED_PAID_CALLS_COLLECTION)
    assert len(docs) == 1
    assert docs[0]["endpoint"] == "team_report"
    assert docs[0]["payment_hash"] == "b" * 64
    assert docs[0]["amount_usdc"] == 0.75
    assert "insufficient_funds" in docs[0]["error"]


async def test_storage_failures_never_propagate() -> None:
    """A lost receipt must not turn a delivered paid answer into a 500."""

    class BrokenStore(MemoryStore):
        async def create(self, collection: str, doc_id: str, data: dict[str, Any]) -> bool:
            raise RuntimeError("firestore unavailable")

        async def add(self, collection: str, data: dict[str, Any]) -> str:
            raise RuntimeError("firestore unavailable")

    broken = BrokenStore()
    assert (
        await log_receipt(
            broken, txid="T", payer="P", endpoint="trending", amount_usdc=0.1, network=NETWORK
        )
        is None
    )
    assert (
        await log_failed_paid_call(broken, endpoint="trending", payment_hash="c", error="x") is None
    )


async def test_receipts_summary_aggregates_by_endpoint(store: Store) -> None:
    rows = [
        ("trending", 0.10, "PAYER1"),
        ("trending", 0.10, "PAYER2"),
        ("trending", 0.10, "PAYER1"),
        ("team_report", 0.75, "PAYER1"),
        ("report", 0.50, None),
    ]
    for endpoint, amount, payer in rows:
        await log_receipt(
            store,
            txid=f"TX-{endpoint}-{payer}-{amount}",
            payer=payer,
            endpoint=endpoint,
            amount_usdc=amount,
            network=NETWORK,
        )

    summary = await receipts_summary(store)

    assert summary["count"] == 5
    assert summary["total_usdc"] == 1.55
    assert summary["unique_payers"] == 2
    assert summary["by_endpoint"] == {
        "report": {"count": 1, "total_usdc": 0.5},
        "team_report": {"count": 1, "total_usdc": 0.75},
        "trending": {"count": 3, "total_usdc": 0.3},
    }


async def test_receipts_summary_on_an_empty_collection(store: Store) -> None:
    assert await receipts_summary(store) == {
        "count": 0,
        "total_usdc": 0.0,
        "unique_payers": 0,
        "by_endpoint": {},
    }


async def test_firestore_rollup_reads_before_queuing_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protect the Firestore transaction's read-before-write requirement."""

    class Snapshot:
        exists = False

        def to_dict(self) -> dict[str, Any]:
            return {}

    class Transaction:
        def __init__(self) -> None:
            self.wrote = False
            self.operations: list[tuple[str, str]] = []

        def create(self, ref: Any, _data: dict[str, Any]) -> None:
            self.wrote = True
            self.operations.append(("create", ref.name))

        def set(self, ref: Any, _data: dict[str, Any]) -> None:
            self.wrote = True
            self.operations.append(("set", ref.name))

    class Ref:
        def __init__(self, name: str) -> None:
            self.name = name

        async def get(self, *, transaction: Transaction) -> Snapshot:
            if transaction.wrote:
                raise AssertionError("Firestore read queued after a write")
            return Snapshot()

    class Client:
        def __init__(self, transaction: Transaction) -> None:
            self.active = transaction

        def transaction(self) -> Transaction:
            return self.active

    class OrderCheckingStore(FirestoreStore):
        def __init__(self, client: Client) -> None:
            super().__init__(client=client)

        def _doc_ref(self, collection: str, doc_id: str) -> Ref:
            return Ref(f"{collection}/{doc_id}")

    active = Transaction()
    store = OrderCheckingStore(Client(active))
    from google.cloud import firestore  # noqa: PLC0415

    monkeypatch.setattr(firestore, "async_transactional", lambda function: function)
    receipt = {
        "txid": "TX",
        "payer": "PAYER",
        "endpoint": "trending",
        "amount_usdc": 0.1,
        "network": NETWORK,
        "ts": "2026-10-07T13:05:00+00:00",
    }

    await receipt_module._record_firestore(store, "receipt-1", receipt, write_receipt=True)

    assert [kind for kind, _name in active.operations] == ["create", "create", "set", "create"]


def test_the_backfill_entry_point_ships_in_the_api_image() -> None:
    """The documented migration command must exist in the deployed container.

    The runtime stage copies ``api/`` and nothing else, and carries no ``uv``.
    A backfill module anywhere else -- or a runbook that reaches for ``uv`` --
    is a migration that cannot be run, which leaves ``/v1/stats`` on the
    unbounded receipt scan the rollups exist to retire.
    """
    import importlib
    import pathlib

    module = "api.scripts.backfill_receipt_stats"
    assert importlib.import_module(module).main

    root = pathlib.Path(__file__).resolve().parent.parent
    source = root / "api" / "scripts" / "backfill_receipt_stats.py"
    assert source.is_file(), "the backfill module must live under the copied api/ tree"

    runbook = (root / "infra" / "deploy.md").read_text()
    assert f"-m,{module}" in runbook, "deploy.md must document the shipped entry point"
    migration = runbook.split("### Receipt-stat rollup migration", 1)[1].split("\n## ", 1)[0]
    assert "uv run" not in migration, "uv is a builder-stage tool; it is not in the api image"
