"""On-disk journal of payments that have been signed but not yet answered.

Why this file exists
--------------------
Play Clock settles a payment **before** its handler returns, and the ADK
endpoints can run tens of seconds (DESIGN_NOTES, "ADK on Vertex"). So the
dangerous moment is not a rejected payment — it is a *successful* one whose
response never arrives. The money has moved; the answer is recoverable only by
replaying the identical request with the identical ``PAYMENT-SIGNATURE``.

Replay is safe and free: the server binds a payment to
``endpoint_key + SHA256(header)`` plus a fingerprint of the request (method,
path, sorted query, body hash). The *same* request inside the idempotency window
returns the cached receipt without settling again; a *different* request with
the same header is refused with a 402. That is exactly the behaviour a recovery
journal needs, and it is why every field of the original request is stored here
rather than just the header.

The journal is therefore written **before** the paid request is sent and cleared
only once a response is in hand. A crash, a timeout or a killed MCP client all
leave a recoverable entry behind.

Storage is a single JSON file under ``PLAYCLOCK_STATE_DIR`` (default
``~/.playclock``), written with ``0o600`` because a pending entry is a bearer
token for an already-paid analysis.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

__all__ = ["PaymentJournal", "PendingPayment", "state_dir"]

#: Entries older than this are dropped on load. The server's idempotency window
#: is 300s (DESIGN_NOTES §11), so a header past that is no longer replayable —
#: keeping it would promise a recovery that cannot happen. The margin is
#: generous because a stale entry is a *diagnostic*: it tells the user a paid
#: call was lost, which is worth surfacing even once replay has expired.
MAX_AGE_SECONDS = 24 * 3600

#: How long the server will honour a replay. Past this, recovery is hopeless and
#: :meth:`PaymentJournal.recoverable` stops offering the entry.
REPLAY_WINDOW_SECONDS = 300


def state_dir() -> Path:
    """Directory holding this client's state. Override with ``PLAYCLOCK_STATE_DIR``."""
    configured = os.environ.get("PLAYCLOCK_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".playclock"


@dataclass
class PendingPayment:
    """One signed payment and the exact request it bought.

    Every field is part of the server's replay fingerprint. Reconstructing the
    request from anything less would produce a *different* request, which the
    server refuses with a 402 rather than answering — the money would stay gone.

    Attributes:
        tool: MCP tool name, for human-readable reporting.
        method: ``GET`` or ``POST``.
        path: Request path, e.g. ``/v1/player``.
        params: Query parameters, exactly as sent.
        body: JSON body for POST, else ``None``.
        header_name: The payment header's name (``PAYMENT-SIGNATURE``).
        header_value: The signed payload. A bearer token for one paid analysis.
        price_usdc: What the quote cost, for reporting.
        created_at: Unix seconds when the payment was signed.
    """

    tool: str
    method: str
    path: str
    header_name: str
    header_value: str
    price_usdc: float
    params: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        """Seconds since the payment was signed."""
        return max(0.0, time.time() - self.created_at)

    @property
    def replayable(self) -> bool:
        """Whether the server's idempotency window can still honour a replay."""
        return self.age_seconds < REPLAY_WINDOW_SECONDS

    def key(self) -> str:
        """Stable identity: one entry per signed header.

        A digest of the **whole** header, not a prefix of it. Encoded x402
        envelopes share a long common base64 prefix — scheme, network, asset,
        payTo and amount all serialize ahead of the part that actually differs
        — so a truncated key collides between two payments made close together.
        The consequence is not cosmetic: the second record would overwrite the
        first, and clearing the second after its response arrived would delete
        the only recovery record for a payment that never answered.
        """
        return hashlib.sha256(self.header_value.encode("utf-8")).hexdigest()


class PaymentJournal:
    """Crash-safe record of payments awaiting an answer.

    Every mutation re-reads the file, applies one change and rewrites it under
    an exclusive lock (where the platform has ``fcntl``), so two MCP processes
    sharing ``~/.playclock`` — Claude Desktop and Claude Code, say — merge their
    entries rather than each overwriting the other's recovery records. The
    rewrite is a 0600 temp file, fsync'd, then renamed over the journal.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (state_dir() / "pending-payments.json")
        self._entries: dict[str, PendingPayment] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        """Read the journal, dropping anything too old to be worth reporting."""
        self._entries = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            try:
                entry = PendingPayment(**item)
            except TypeError:
                continue  # a journal written by a different version; skip it
            if entry.age_seconds < MAX_AGE_SECONDS:
                self._entries[entry.key()] = entry

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive lock on the journal's sidecar lock file."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if fcntl is None:  # pragma: no cover - Windows: merge-on-write only
            yield
            return
        fd = os.open(self._path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)  # closing releases the lock

    def _flush(self) -> None:
        """Rewrite the journal atomically, owner-readable only, durably."""
        payload = json.dumps([asdict(e) for e in self._entries.values()], indent=2)
        # mkstemp creates the file 0600 from the start: the headers are bearer
        # instruments and must never exist world-readable, even briefly.
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".pending-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):  # pragma: no cover - not every OS can
            dir_fd = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    # -- api ---------------------------------------------------------------

    def record(self, entry: PendingPayment) -> None:
        """Persist a payment **before** the paid request goes out."""
        with self._locked():
            self._load()
            self._entries[entry.key()] = entry
            self._flush()

    def clear(self, entry: PendingPayment) -> None:
        """Drop an entry once its response is in hand."""
        with self._locked():
            self._load()
            if self._entries.pop(entry.key(), None) is not None:
                self._flush()

    def pending(self) -> list[PendingPayment]:
        """Every entry still on file, newest first (including other processes')."""
        self._load()
        return sorted(self._entries.values(), key=lambda e: e.created_at, reverse=True)

    def recoverable(self) -> list[PendingPayment]:
        """Entries the server's idempotency window can still answer."""
        return [e for e in self.pending() if e.replayable]
