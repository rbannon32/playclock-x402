"""The payment journal — the thing that stops a timeout costing real money.

Settlement happens before the paid handler returns, so a call that never comes
back has already moved USDC. These tests pin the two properties that make that
recoverable: the header is on disk *before* the request goes out, and enough of
the original request is stored to reproduce it byte for byte.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from playclock_mcp.journal import (
    MAX_AGE_SECONDS,
    REPLAY_WINDOW_SECONDS,
    PaymentJournal,
    PendingPayment,
    state_dir,
)


def make_entry(**overrides: object) -> PendingPayment:
    base: dict[str, object] = {
        "tool": "playclock_player",
        "method": "POST",
        "path": "/v1/player",
        "params": {"week": 4},
        "body": {"name": "Bijan Robinson"},
        "header_name": "PAYMENT-SIGNATURE",
        "header_value": "abc123",
        "price_usdc": 0.15,
    }
    base.update(overrides)
    return PendingPayment(**base)  # type: ignore[arg-type]


def test_entry_round_trips_through_disk(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    PaymentJournal(path).record(make_entry())

    reloaded = PaymentJournal(path).pending()

    assert len(reloaded) == 1
    # Every field of the request is needed: the server binds a payment to
    # method + path + sorted query + body hash, so a partial replay is a
    # *different* request and gets 402'd instead of answered.
    assert reloaded[0].method == "POST"
    assert reloaded[0].path == "/v1/player"
    assert reloaded[0].params == {"week": 4}
    assert reloaded[0].body == {"name": "Bijan Robinson"}
    assert reloaded[0].header_value == "abc123"


def test_clearing_an_entry_removes_it_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    journal = PaymentJournal(path)
    entry = make_entry()
    journal.record(entry)

    journal.clear(entry)

    assert journal.pending() == []
    assert PaymentJournal(path).pending() == []


def test_journal_is_written_owner_readable_only(tmp_path: Path) -> None:
    # A pending entry is a bearer token for an already-paid analysis.
    path = tmp_path / "pending.json"
    PaymentJournal(path).record(make_entry())
    assert path.stat().st_mode & 0o077 == 0


def test_entries_past_the_replay_window_are_kept_but_not_offered(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    journal = PaymentJournal(path)
    journal.record(make_entry(created_at=time.time() - REPLAY_WINDOW_SECONDS - 1))

    # Still listed: a lost payment should be visible, not silently dropped.
    assert len(journal.pending()) == 1
    # But not replayable: the server's idempotency window has closed.
    assert journal.recoverable() == []


def test_fresh_entries_are_offered_for_recovery(tmp_path: Path) -> None:
    journal = PaymentJournal(tmp_path / "pending.json")
    journal.record(make_entry())
    assert len(journal.recoverable()) == 1


def test_ancient_entries_are_dropped_on_load(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    journal = PaymentJournal(path)
    journal.record(make_entry(created_at=time.time() - MAX_AGE_SECONDS - 1))

    assert PaymentJournal(path).pending() == []


def test_unreadable_or_foreign_journal_does_not_crash_the_client(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"

    path.write_text("not json at all", encoding="utf-8")
    assert PaymentJournal(path).pending() == []

    # A journal written by a different version: skip rows we cannot construct.
    path.write_text(json.dumps([{"unexpected": "shape"}]), encoding="utf-8")
    assert PaymentJournal(path).pending() == []


def test_payments_sharing_a_long_prefix_get_distinct_entries(tmp_path: Path) -> None:
    """Encoded x402 envelopes share a long base64 prefix before they differ.

    Keying on a prefix silently collapses two pending payments into one, and
    clearing the survivor then deletes the only recovery record for the other.
    """
    shared = "eyJ4NDAyVmVyc2lvbiI6MiwiYWNjZXB0ZWQiOnsic2NoZW1lIjoiZXhhY3QifX0" * 4
    first = make_entry(header_value=shared + "AAAA")
    second = make_entry(header_value=shared + "BBBB")
    assert first.key() != second.key()

    journal = PaymentJournal(tmp_path / "pending.json")
    journal.record(first)
    journal.record(second)
    assert len(journal.pending()) == 2

    journal.clear(second)
    assert [e.header_value for e in journal.pending()] == [first.header_value]


def test_state_dir_honours_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PLAYCLOCK_STATE_DIR", str(tmp_path / "custom"))
    assert state_dir() == tmp_path / "custom"

    monkeypatch.delenv("PLAYCLOCK_STATE_DIR")
    assert state_dir() == Path.home() / ".playclock"


def test_two_processes_sharing_a_journal_keep_each_others_entries(tmp_path: Path) -> None:
    """Desktop and Code share ~/.playclock; neither may erase the other's payment."""
    path = tmp_path / "pending.json"
    desktop, code = PaymentJournal(path), PaymentJournal(path)
    first, second = make_entry(header_value="one"), make_entry(header_value="two")

    desktop.record(first)
    code.record(second)
    assert {e.header_value for e in PaymentJournal(path).pending()} == {"one", "two"}

    desktop.clear(first)
    assert [e.header_value for e in PaymentJournal(path).pending()] == ["two"]


def test_the_state_directory_is_private(tmp_path: Path) -> None:
    path = tmp_path / "state" / "pending.json"
    PaymentJournal(path).record(make_entry())
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    assert not list(path.parent.glob("*.tmp")), "no temp file is left behind"
