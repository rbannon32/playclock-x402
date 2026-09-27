"""``infra/optin.py status`` must read no secret: addresses are public."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from algosdk import encoding

ROOT = Path(__file__).resolve().parents[1]


def load_optin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("optin", ROOT / "infra" / "optin.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_status_uses_public_addresses_and_reads_no_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    optin = load_optin()

    def no_secrets(secret: str) -> str:
        raise AssertionError(f"status read secret {secret}")

    optin.read_mnemonic = no_secrets  # a script, not a seam: prove it is never called
    seen: list[tuple[str, str]] = []

    def state(network: str, address: str) -> tuple[int, int | None]:
        seen.append((network, address))
        return 1_000_000, None

    optin.show_status(None, state=state)

    assert [a for _, a in seen] == [w[2] for w in optin.WALLETS.values()]
    assert "MDBJMM6RJ4TM7W5FITZ3MWJTGTHMC4SKQJWRWI2JCQUKIR5LA7BPIUTMMM" in capsys.readouterr().out


def test_published_addresses_are_valid_and_match_the_runbook() -> None:
    optin = load_optin()
    runbook = (ROOT / "infra" / "deploy.md").read_text()
    for name, (_network, secret, address) in optin.WALLETS.items():
        assert encoding.is_valid_address(address), name
        assert f"| `{secret}` | `{address}` |" in runbook, name
