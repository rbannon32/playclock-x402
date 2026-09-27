"""Settled-payment receipts and bounded usage rollups.

receipts remains the audit trail for every successful settlement. The free stats
endpoint reads a fixed set of sharded rollups instead of streaming that growing
collection. A receipt payment hash is its idempotency key, so a retry cannot
increment a rollup twice.

Existing receipts are deliberately not guessed at: run the receipt-stats
backfill command before enabling the rollup. Until that command marks a network
complete, callers retain the exact legacy scan.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from api.core.store import FirestoreStore, Store

__all__ = [
    "FAILED_PAID_CALLS_COLLECTION",
    "RECEIPTS_COLLECTION",
    "backfill_receipt_stats",
    "log_failed_paid_call",
    "log_receipt",
    "receipt_stats",
    "receipts_summary",
]

logger = logging.getLogger(__name__)

RECEIPTS_COLLECTION = "receipts"
FAILED_PAID_CALLS_COLLECTION = "failed_paid_calls"
_RECEIPT_STATS_SHARDS = "receipt_stats_shards"
_RECEIPT_STATS_PAYER_IDS = "receipt_stats_payers"
_RECEIPT_STATS_EVENTS = "receipt_stats_events"
_RECEIPT_STATS_META = "receipt_stats_meta"
_STATS_SHARDS = 32


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _network_key(network: str) -> str:
    return hashlib.sha256(network.encode("utf-8")).hexdigest()[:24]


def _shard_for(receipt_id: str) -> int:
    return int(hashlib.sha256(receipt_id.encode("utf-8")).hexdigest(), 16) % _STATS_SHARDS


def _receipt_id(doc: dict[str, Any]) -> str:
    payment_hash = str(doc.get("payment_hash") or "").strip()
    if payment_hash:
        return f"payment-{payment_hash}"
    material = json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str)
    return f"legacy-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _payer_id(network: str, payer: str) -> str:
    return f"{_network_key(network)}-{hashlib.sha256(payer.encode('utf-8')).hexdigest()}"


def _shard_id(network: str, receipt_id: str) -> str:
    return f"{_network_key(network)}-{_shard_for(receipt_id):02d}"


def _event_id(network: str, receipt_id: str) -> str:
    return f"{_network_key(network)}-{receipt_id}"


def _updated_shard(
    current: dict[str, Any] | None, receipt: dict[str, Any], *, new_payer: bool
) -> dict[str, Any]:
    endpoint = str(receipt.get("endpoint") or "unknown")
    amount = float(receipt.get("amount_usdc") or 0.0)
    previous = current or {}
    by_endpoint = dict(previous.get("by_endpoint") or {})
    endpoint_total = dict(by_endpoint.get(endpoint) or {})
    endpoint_total["paid_calls"] = int(endpoint_total.get("paid_calls") or 0) + 1
    endpoint_total["usdc"] = float(endpoint_total.get("usdc") or 0.0) + amount
    by_endpoint[endpoint] = endpoint_total
    ts = str(receipt.get("ts") or "")
    since = previous.get("since")
    return {
        "network": receipt["network"],
        "paid_analyses": int(previous.get("paid_analyses") or 0) + 1,
        "unique_payers": int(previous.get("unique_payers") or 0) + int(new_payer),
        "usdc_settled": float(previous.get("usdc_settled") or 0.0) + amount,
        "since": min(str(since), ts) if since and ts else (str(since or ts) or None),
        "by_endpoint": by_endpoint,
    }


async def _record_memory(
    store: Store, receipt_id: str, receipt: dict[str, Any], *, write_receipt: bool
) -> None:
    event_id = _event_id(str(receipt["network"]), receipt_id)
    created = await store.create(_RECEIPT_STATS_EVENTS, event_id, {"receipt_id": receipt_id})
    if not created:
        return
    if write_receipt:
        await store.create(RECEIPTS_COLLECTION, receipt_id, receipt)
    payer = str(receipt.get("payer") or "")
    new_payer = bool(payer) and await store.create(
        _RECEIPT_STATS_PAYER_IDS,
        _payer_id(str(receipt["network"]), payer),
        {"network": receipt["network"]},
    )
    shard_id = _shard_id(str(receipt["network"]), receipt_id)
    current = await store.get(_RECEIPT_STATS_SHARDS, shard_id)
    await store.set(
        _RECEIPT_STATS_SHARDS,
        shard_id,
        _updated_shard(current, receipt, new_payer=bool(new_payer)),
    )


async def _record_firestore(
    store: FirestoreStore, receipt_id: str, receipt: dict[str, Any], *, write_receipt: bool
) -> None:
    from google.cloud import firestore  # noqa: PLC0415

    network = str(receipt["network"])
    receipt_ref = store._doc_ref(RECEIPTS_COLLECTION, receipt_id)
    event_ref = store._doc_ref(_RECEIPT_STATS_EVENTS, _event_id(network, receipt_id))
    shard_ref = store._doc_ref(_RECEIPT_STATS_SHARDS, _shard_id(network, receipt_id))
    payer = str(receipt.get("payer") or "")
    payer_ref = (
        store._doc_ref(_RECEIPT_STATS_PAYER_IDS, _payer_id(network, payer)) if payer else None
    )
    transaction = store._get_client().transaction()

    @firestore.async_transactional
    async def record(active: Any) -> None:
        # Firestore transactions reject every read issued after the first queued
        # write. Resolve the complete decision first, then create/set below.
        if (await event_ref.get(transaction=active)).exists:
            return
        receipt_missing = write_receipt and not (await receipt_ref.get(transaction=active)).exists
        new_payer = payer_ref is not None and not (await payer_ref.get(transaction=active)).exists
        shard = await shard_ref.get(transaction=active)
        current = dict(shard.to_dict() or {}) if shard.exists else None

        if receipt_missing:
            active.create(receipt_ref, receipt)
        if new_payer and payer_ref is not None:
            active.create(payer_ref, {"network": network})
        active.set(shard_ref, _updated_shard(current, receipt, new_payer=new_payer))
        active.create(event_ref, {"receipt_id": receipt_id, "network": network})

    await record(transaction)


async def _record_receipt(
    store: Store, receipt_id: str, receipt: dict[str, Any], *, write_receipt: bool
) -> None:
    if isinstance(store, FirestoreStore):
        await _record_firestore(store, receipt_id, receipt, write_receipt=write_receipt)
    else:
        await _record_memory(store, receipt_id, receipt, write_receipt=write_receipt)


async def log_receipt(
    store: Store,
    *,
    txid: str,
    payer: str | None,
    endpoint: str,
    amount_usdc: float,
    network: str,
    ts: str | None = None,
    payment_hash: str | None = None,
) -> str | None:
    """Atomically persist one settled receipt and its bounded stats contribution."""
    doc: dict[str, Any] = {
        "txid": txid,
        "payer": payer,
        "endpoint": endpoint,
        "amount_usdc": float(amount_usdc),
        "network": network,
        "ts": ts or _now_iso(),
    }
    if payment_hash is not None:
        doc["payment_hash"] = payment_hash
    receipt_id = _receipt_id(doc)
    try:
        await _record_receipt(store, receipt_id, doc, write_receipt=True)
        return receipt_id
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to write receipt for %s (%s): %s", endpoint, txid, exc)
        return None


async def log_failed_paid_call(
    store: Store,
    *,
    endpoint: str,
    payment_hash: str,
    error: str,
    ts: str | None = None,
    payer: str | None = None,
    amount_usdc: float | None = None,
    network: str | None = None,
) -> str | None:
    doc: dict[str, Any] = {
        "endpoint": endpoint,
        "payment_hash": payment_hash,
        "error": error,
        "ts": ts or _now_iso(),
    }
    if payer is not None:
        doc["payer"] = payer
    if amount_usdc is not None:
        doc["amount_usdc"] = float(amount_usdc)
    if network is not None:
        doc["network"] = network
    try:
        return await store.add(FAILED_PAID_CALLS_COLLECTION, doc)
    except Exception as exc:  # noqa: BLE001
        logger.error("failed to write failed_paid_call for %s: %s", endpoint, exc)
        return None


async def receipt_stats(store: Store, network: str) -> dict[str, Any] | None:
    """Return one network bounded rollup, or None before its backfill."""
    meta = await store.get(_RECEIPT_STATS_META, _network_key(network))
    if not meta or not meta.get("complete"):
        return None
    shards = await asyncio.gather(
        *(
            store.get(_RECEIPT_STATS_SHARDS, f"{_network_key(network)}-{index:02d}")
            for index in range(_STATS_SHARDS)
        )
    )
    total = 0.0
    paid = 0
    payer_count = 0
    since: str | None = None
    by_endpoint: dict[str, dict[str, float]] = {}
    for shard in shards:
        if not shard:
            continue
        paid += int(shard.get("paid_analyses") or 0)
        payer_count += int(shard.get("unique_payers") or 0)
        total += float(shard.get("usdc_settled") or 0.0)
        ts = shard.get("since")
        if ts and (since is None or str(ts) < since):
            since = str(ts)
        for endpoint, bucket in (shard.get("by_endpoint") or {}).items():
            output = by_endpoint.setdefault(str(endpoint), {"count": 0, "total_usdc": 0.0})
            output["count"] += int(bucket.get("paid_calls") or 0)
            output["total_usdc"] += float(bucket.get("usdc") or 0.0)
    return {
        "count": paid,
        "total_usdc": round(total, 6),
        "unique_payers": payer_count,
        "since": since,
        "by_endpoint": {
            key: {"count": int(value["count"]), "total_usdc": round(value["total_usdc"], 6)}
            for key, value in sorted(by_endpoint.items())
        },
    }


async def receipts_summary(store: Store) -> dict[str, Any]:
    """Legacy all-network audit summary, kept for callers outside stats."""
    docs = await store.list(RECEIPTS_COLLECTION)
    total = 0.0
    payers: set[str] = set()
    by_endpoint: dict[str, dict[str, float]] = {}
    for doc in docs:
        amount = float(doc.get("amount_usdc") or 0.0)
        endpoint = str(doc.get("endpoint") or "unknown")
        total += amount
        bucket = by_endpoint.setdefault(endpoint, {"count": 0, "total_usdc": 0.0})
        bucket["count"] += 1
        bucket["total_usdc"] += amount
        payer = doc.get("payer")
        if isinstance(payer, str) and payer:
            payers.add(payer)
    return {
        "count": len(docs),
        "total_usdc": round(total, 6),
        "unique_payers": len(payers),
        "by_endpoint": {
            key: {"count": int(value["count"]), "total_usdc": round(value["total_usdc"], 6)}
            for key, value in sorted(by_endpoint.items())
        },
    }


async def backfill_receipt_stats(store: Store, network: str) -> int:
    """Build a network rollup from historical receipts, then mark it safe to read."""
    docs = await store.list(RECEIPTS_COLLECTION, where=[("network", "==", network)])
    for doc in docs:
        receipt_id = str(doc.get("_id") or _receipt_id(doc))
        await _record_receipt(store, receipt_id, doc, write_receipt=False)
    await store.set(
        _RECEIPT_STATS_META,
        _network_key(network),
        {
            "network": network,
            "complete": True,
            "receipts_backfilled": len(docs),
            "completed_at": _now_iso(),
        },
    )
    return len(docs)
