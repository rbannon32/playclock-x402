"""Refuse payments that expire before we can settle them.

Settlement runs *after* the handler (verify → handler → settle), and the one
asymmetric case of that ordering is a settle that fails after a good answer:
the caller keeps the answer and we log ``failed_paid_calls/``. Left unchecked, a
payer can cause that case on purpose. The facilitator's ``verify`` checks
amount, receiver, signature and a simulation against the *current* ledger; it
sets no floor on how long the transaction stays valid. So a transfer signed
with ``lastValid`` a handful of rounds ahead verifies, the handler takes longer
than those rounds, settle fails with the transaction expired, and the answer is
served for free — as often as the payer likes.

This module closes that door before verify: decode the earliest ``lastValid``
in the payment group, estimate the current round, and 402 when fewer than
:data:`MIN_REMAINING_ROUNDS` remain. Every honest client signs with algod's
suggested params, which give ``current + 1000`` rounds (~45 minutes), so the
floor costs them nothing.

The current round comes from algod's ``/v2/status``, fetched at most every
:data:`REFRESH_SECONDS` and extrapolated in between. If algod cannot be reached
the check **fails open** (logged): refusing every sale because a status endpoint
blinked would cost far more than the exploit it prevents.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Protocol

import httpx

from api.core.config import Settings
from api.x402.schemas_compat import PaymentPayload, payment_group_transactions

logger = logging.getLogger(__name__)

#: Rounds a payment must still be valid for when it is verified. Covers the
#: slowest handler (Cloud Run's 300s timeout) plus settle, with margin, at any
#: plausible round time; honest clients arrive with ~1000.
MIN_REMAINING_ROUNDS = 200

#: How long one algod status reading is trusted before it is refetched.
REFRESH_SECONDS = 30.0

#: Seconds per round used to extrapolate between readings. Slightly faster than
#: MainNet's ~2.8s on purpose: over-estimating the current round is the strict
#: direction, and the 1000-round default leaves ample slack for honest clients.
ROUND_SECONDS = 2.5

#: Oldest reading that is still extrapolated. Past this, algod has been
#: unreachable for a while and ``ROUND_SECONDS``' deliberate over-estimate has
#: drifted far enough (~0.3s a round) to refuse honest payments, so the check
#: fails open instead of projecting a stale reading forever.
MAX_READING_AGE_SECONDS = 120.0

#: After a failed status read, wait this long before asking again, so a hung
#: algod does not add its timeout to every paid request.
RETRY_AFTER_FAILURE_SECONDS = 10.0

#: Error string in the 402 body; stable, so clients can match on it.
EXPIRES_TOO_SOON = "payment_expires_too_soon"


class RoundClock(Protocol):
    """Anything that can say roughly which round the chain is on."""

    async def current_round(self) -> int | None:
        """The estimated current round, or ``None`` when it is unknown."""
        ...


class AlgodRoundClock:
    """Reads ``last-round`` from algod ``/v2/status`` and extrapolates between reads.

    Args:
        algod_url: Base URL of an algod node for the payment network.
        transport: Optional ``httpx`` transport, injected by tests.
    """

    def __init__(
        self,
        algod_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = algod_url.rstrip("/") + "/v2/status"
        self._transport = transport
        self._monotonic = monotonic
        self._reading: tuple[int, float] | None = None
        self._failed_at: float | None = None

    async def current_round(self) -> int | None:
        now = self._monotonic()
        due = self._reading is None or now - self._reading[1] > REFRESH_SECONDS
        backing_off = (
            self._failed_at is not None and now - self._failed_at < RETRY_AFTER_FAILURE_SECONDS
        )
        if due and not backing_off:
            fetched = await self._fetch()
            if fetched is not None:
                self._reading = (fetched, self._monotonic())
                self._failed_at = None
            else:
                self._failed_at = self._monotonic()
        if self._reading is None:
            return None
        last_round, read_at = self._reading
        age = self._monotonic() - read_at
        if age > MAX_READING_AGE_SECONDS:
            return None
        return last_round + int(age / ROUND_SECONDS)

    async def _fetch(self) -> int | None:
        try:
            async with httpx.AsyncClient(transport=self._transport, timeout=3.0) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                value = response.json().get("last-round")
        except (httpx.HTTPError, ValueError, AttributeError):
            logger.warning("algod status unavailable; validity check skipped", exc_info=True)
            return None
        return value if isinstance(value, int) and not isinstance(value, bool) else None


_clocks: dict[str, RoundClock] = {}
_override: RoundClock | None = None


def set_round_clock(clock: RoundClock | None) -> None:
    """Test hook: inject a clock for every network (``None`` restores algod)."""
    global _override
    _override = clock
    _clocks.clear()


def get_round_clock(network: str) -> RoundClock | None:
    """The clock for a CAIP-2 ``network``, or ``None`` when no algod is known for it."""
    if _override is not None:
        return _override
    if network not in _clocks:
        try:
            from x402.mechanisms.avm.utils import get_network_config

            url = get_network_config(network)["algod_url"]
        except (ImportError, KeyError, ValueError):
            logger.warning("no algod known for %s; payment validity check skipped", network)
            return None
        _clocks[network] = AlgodRoundClock(url)
    return _clocks[network]


def payment_last_valid(payload: PaymentPayload) -> int | None:
    """The earliest ``lastValid`` in the payment group, or ``None``.

    Minimum across *every* transaction, not just the one that moves the money:
    an atomic group settles all or nothing, so one short-lived sibling lapses
    the whole group and the settle fails exactly as if the payment itself had.

    Decoded exactly as leniently as the facilitator decodes it: a stricter
    decode here would read "undecodable" for a transaction the facilitator
    accepts, and the floor fails open on ``None``. An undecodable sibling is
    skipped for the same reason; verify rejects it.
    """
    try:
        from x402.mechanisms.avm.utils import decode_transaction_bytes
    except ImportError:
        return None
    rounds: list[int] = []
    for raw in payment_group_transactions(payload):
        try:
            last_valid = decode_transaction_bytes(raw).last_valid
        except Exception:  # noqa: BLE001 - an undecodable txn is verify's to reject
            continue
        if isinstance(last_valid, int) and not isinstance(last_valid, bool) and last_valid > 0:
            rounds.append(last_valid)
    return min(rounds) if rounds else None


async def expires_too_soon(payload: PaymentPayload, network: str, settings: Settings) -> bool:
    """True when the payment would lapse before a slow handler could settle it.

    Only enforced in live mode: mock payloads carry no transaction.
    """
    if settings.x402_mode != "live":
        return False
    last_valid = payment_last_valid(payload)
    if last_valid is None:
        return False
    clock = get_round_clock(network)
    current = await clock.current_round() if clock is not None else None
    if current is None:
        return False
    remaining = last_valid - current
    if remaining < MIN_REMAINING_ROUNDS:
        logger.warning(
            "refusing payment valid for %d more rounds (lastValid %d, current ~%d)",
            remaining,
            last_valid,
            current,
        )
        return True
    return False
