"""Keep public payment-recovery guidance aligned with the middleware contract."""

from pathlib import Path

import pytest

from api.x402.middleware import IDEMPOTENCY_TTL_SECONDS

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("TECH_SPEC.md", "remember each signed payment transaction for {seconds}s"),
        ("DESIGN_NOTES.md", "{seconds}s idempotency window (raised from 60s; see §27)"),
        ("examples/agent/README.md", "payment is remembered for {seconds}\n  seconds"),
        ("examples/agent/selftest.py", "payment to one request for {seconds}s"),
        ("web/docs.html", "payment is remembered for {seconds} seconds"),
    ],
)
def test_active_payment_docs_name_the_replay_window(path: str, expected: str) -> None:
    seconds = int(IDEMPOTENCY_TTL_SECONDS)
    assert expected.format(seconds=seconds) in (ROOT / path).read_text()
