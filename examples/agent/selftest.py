#!/usr/bin/env python3
"""Ad-hoc end-to-end check for ``fantasy_agent.py``. Run it, do not import it.

Not part of the pytest suite (nothing was added to ``tests/``) — this is the
harness that proves the example agent works, run by hand from the repo root::

    uv run python examples/agent/selftest.py

Why it exists: the agent's happy path needs a *seeded* store, and there is no
HTTP route that seeds one. So instead of a live server this drives the agent's
own ``discover`` / ``ask_once`` against an in-process ASGI app
(``httpx.ASGITransport``) whose store has been filled with the golden fixture
season. Same code path as the CLI — the only difference is which transport the
``httpx.AsyncClient`` was built with.

Five things get checked:

1. **Happy path** — 402 -> mock payment -> 200, with a settlement receipt and a
   real analysis body, for GET ``/v1/trending`` and POST ``/v1/player`` and
   ``/v1/matchup``.
2. **Fresh payment per request** — the same ask twice, and two *different* asks,
   all succeed. A constant payment token would be refused on the second one
   (the server binds a payment to one request for 60s).
3. **Price guard** — a 0.10 USDC quote against a 0.05 ceiling raises
   ``PriceTooHigh`` and signs nothing.
4. **Cold store** — an unseeded app answers 503 and the agent reports
   ``DataNotReady`` ("not charged").
5. **AlgorandSigner, offline** — with a stub Algod supplying suggested params,
   the real SDK scheme builds a payment group; the decoded transaction is
   checked field by field and its ed25519 signature verified with the
   *facilitator's own* verifier. Both the plain and fee-payer (``extra.feePayer``)
   shapes are covered. What this cannot cover is the chain itself — see the
   TestNet notes at the bottom.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

import fantasy_agent as agent  # noqa: E402

# Environment first: every module reads the cached get_settings() singleton.
os.environ.update(
    {
        "STORE_BACKEND": "memory",
        "ENGINE": "deterministic",
        "X402_MODE": "mock",
        "X402_NETWORK": "testnet",
        "SEASON": "2026",
        "WEEK_OVERRIDE": "4",
    }
)

from api.core.config import ENDPOINT_KEYS, get_settings  # noqa: E402
from api.core.store import MemoryStore, set_store  # noqa: E402
from api.evals.golden import seed_store  # noqa: E402
from api.x402 import clear_idempotency_cache  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion."""
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


def quiet(_message: str) -> None:
    """Swallow the agent's progress log inside the noisy checks."""


def client_for(app: Any) -> httpx.AsyncClient:
    """An httpx client that talks to ``app`` in-process — no socket, no server."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver", timeout=30.0
    )


async def build_app(seeded: bool) -> Any:
    """Create the API with a fresh store, optionally seeded with the fixture season."""
    get_settings.cache_clear()
    clear_idempotency_cache()
    store = MemoryStore()
    if seeded:
        await seed_store(store)
    set_store(store)
    from api.main import create_app

    return create_app()


# ---------------------------------------------------------------------------
# 1-3. The agent against a seeded, mock-mode app
# ---------------------------------------------------------------------------


async def check_paid_flow() -> None:
    """Discover, then buy three different analyses over a real 402 handshake."""
    print("\n[1] paid flow against a seeded in-process app (X402_MODE=mock)")
    app = await build_app(seeded=True)
    payer = agent.Payer(signer=agent.MockSigner(), max_price_usdc=1.00)

    async with client_for(app) as http:
        catalog = await agent.discover(http)
        paid = [entry for entry in catalog["endpoints"] if not entry["free"]]
        check(
            f"catalog lists {len(ENDPOINT_KEYS)} paid endpoints",
            len(paid) == len(ENDPOINT_KEYS),
            f"got {len(paid)}",
        )
        check(
            "catalog prices trending at 0.10 USDC",
            any(e["key"] == "trending" and e["price_usdc"] == 0.10 for e in paid),
        )

        # --- GET /v1/trending ---
        result = await agent.ask_once(http, payer, agent.parse_ask("trending"), log=quiet)
        check("trending: settlement receipt returned", result.receipt is not None)
        check("trending: settled successfully", bool(result.receipt and result.receipt.success))
        check("trending: txid present", bool(result.receipt and result.receipt.transaction))
        check(
            "trending: network is algorand testnet",
            bool(result.receipt and result.receipt.network == agent.ALGORAND_TESTNET),
            f"got {result.receipt.network if result.receipt else None}",
        )
        check("trending: priced at 0.10 USDC", abs(result.price - 0.10) < 1e-9)
        check("trending: verdict present", bool(result.body.get("verdict")))
        check(
            "trending: confidence is a known tier",
            result.body.get("confidence") in {"high", "medium", "low"},
        )
        check("trending: reasoning present", bool(result.body.get("reasoning")))
        check("trending: stats cited", len(result.body.get("stats_cited") or []) > 0)
        check("trending: board populated", len(result.body.get("players") or []) > 0)
        check("trending: cache stamped fresh", (result.body.get("meta") or {}).get("cache"))

        # Show one full render exactly as the CLI would print it.
        print("\n  --- rendered output for GET /v1/trending ---")
        agent.print_receipt(result)
        agent.print_analysis(result)
        print("  --- end ---\n")

        # --- the same ask again: a *new* payment, not a replayed one ---
        again = await agent.ask_once(http, payer, agent.parse_ask("trending"), log=quiet)
        check("repeat ask succeeds with a fresh nonce", again.receipt is not None)
        check(
            "repeat ask is a distinct payment",
            bool(
                again.receipt
                and result.receipt
                and again.receipt.transaction != result.receipt.transaction
            ),
        )
        check("repeat ask served from cache", (again.body.get("meta") or {}).get("cache") == "hit")

        # --- POST /v1/player ---
        player = await agent.ask_once(
            http, payer, agent.parse_ask("player:Bijan Robinson"), log=quiet
        )
        check("player: settled", bool(player.receipt and player.receipt.success))
        check(
            "player: resolved the right player",
            (player.body.get("player") or {}).get("name") == "Bijan Robinson",
        )
        player_price = get_settings().price_for("player")
        check(f"player: priced at {player_price:.2f} USDC", abs(player.price - player_price) < 1e-9)

        # --- POST /v1/matchup ---
        matchup = await agent.ask_once(
            http, payer, agent.parse_ask("matchup:Bijan Robinson,Breece Hall"), log=quiet
        )
        check("matchup: settled", bool(matchup.receipt and matchup.receipt.success))
        check("matchup: ranked two players", len(matchup.body.get("ranked") or []) == 2)
        check("matchup: verdict present", bool(matchup.body.get("verdict")))

        check(
            "mock signer minted one payment per paid call",
            payer.signer.payments == 4,
            f"got {payer.signer.payments}",
        )

        # --- the price guard: refuse before signing ---
        thrifty = agent.Payer(signer=agent.MockSigner(), max_price_usdc=0.05)
        try:
            await agent.ask_once(http, thrifty, agent.parse_ask("trending"), log=quiet)
        except agent.PriceTooHigh as exc:
            check("price guard refuses a 0.10 quote at a 0.05 ceiling", True)
            check("price guard signed nothing", thrifty.signer.payments == 0)
            print(f"        -> {exc}")
        else:
            check("price guard refuses a 0.10 quote at a 0.05 ceiling", False, "no error raised")


async def check_cold_store() -> None:
    """An unseeded app refuses with 503 and the agent explains it was not charged."""
    print("\n[2] cold store (nothing ingested)")
    app = await build_app(seeded=False)
    payer = agent.Payer(signer=agent.MockSigner(), max_price_usdc=1.00)
    async with client_for(app) as http:
        try:
            await agent.ask_once(http, payer, agent.parse_ask("trending"), log=quiet)
        except agent.DataNotReady as exc:
            check("cold store raises DataNotReady", True)
            check("cold store message says not charged", "not charged" in str(exc).lower())
            print(f"        -> {exc}")
        else:
            check("cold store raises DataNotReady", False, "no error raised")


async def check_unreachable() -> None:
    """A dead host is a clean ApiUnreachable, not a traceback."""
    print("\n[3] unreachable API")
    payer = agent.Payer(signer=agent.MockSigner(), max_price_usdc=1.00)
    async with httpx.AsyncClient(base_url="http://127.0.0.1:9", timeout=2.0) as http:
        try:
            await agent.discover(http)
        except agent.ApiUnreachable as exc:
            check("unreachable host raises ApiUnreachable", True)
            print(f"        -> {str(exc)[:120]}")
        else:
            check("unreachable host raises ApiUnreachable", False, "no error raised")
        try:
            await agent.ask_once(http, payer, agent.parse_ask("trending"), log=quiet)
        except agent.ApiUnreachable:
            check("unreachable host fails the ask cleanly too", True)
        else:
            check("unreachable host fails the ask cleanly too", False, "no error raised")


# ---------------------------------------------------------------------------
# 4. AlgorandSigner, offline
# ---------------------------------------------------------------------------


class StubAlgod:
    """Stands in for ``algosdk.v2client.algod.AlgodClient`` — params, no network.

    ``ExactAvmScheme`` only calls ``suggested_params()`` while building a
    payment, so a stub is enough to exercise every line of transaction
    construction and signing offline. Round numbers are arbitrary; the genesis
    hash is TestNet's, because the facilitator checks it against the CAIP-2
    network in the requirements.
    """

    def __init__(self, genesis_hash: str) -> None:
        self.genesis_hash = genesis_hash

    def suggested_params(self) -> Any:
        from algosdk import transaction

        return transaction.SuggestedParams(
            fee=0,
            first=41_000_000,
            last=41_001_000,
            gh=self.genesis_hash,
            gen="testnet-v1.0",
            flat_fee=False,
            min_fee=1000,
        )


def avm_requirements(pay_to: str, *, fee_payer: str | None = None) -> agent.PaymentRequirements:
    """A TestNet 402 quote shaped exactly like the one this API serves.

    ``pay_to`` must be a real (checksummed) Algorand address: py-algorand-sdk
    decodes the receiver while estimating the fee, so a made-up 58-character
    string fails before anything is signed.
    """
    extra: dict[str, Any] = {"name": "USDC", "decimals": 6, "tag": "x402-global-challenge"}
    if fee_payer:
        extra["feePayer"] = fee_payer
    return agent.PaymentRequirements(
        scheme="exact",
        network=agent.ALGORAND_TESTNET,
        asset=agent.USDC_ASA_IDS[agent.ALGORAND_TESTNET],
        amount="100000",  # 0.10 USDC
        pay_to=pay_to,
        max_timeout_seconds=120,
        extra=extra,
    )


def check_algorand_signer() -> None:
    """Build and verify a real AVM payment group without touching a chain."""
    print("\n[4] AlgorandSigner (offline: stub Algod, real SDK scheme, real signatures)")
    from algosdk import account, encoding, mnemonic
    from x402.mechanisms.avm.utils import (
        decode_base64_transaction,
        verify_transaction_signature,
    )

    # Throwaway accounts. Never funded ones, and the phrase is never printed.
    private_key, address = account.generate_account()
    phrase = mnemonic.from_private_key(private_key)
    pay_to = account.generate_account()[1]

    try:
        agent.AlgorandSigner("not a real mnemonic at all")
    except agent.WalletError:
        check("invalid mnemonic raises WalletError", True)
    else:
        check("invalid mnemonic raises WalletError", False, "no error raised")

    try:
        agent.AlgorandSigner("   ")
    except agent.WalletError as exc:
        check("empty mnemonic explains --mock", "--mock" in str(exc))
    else:
        check("empty mnemonic explains --mock", False, "no error raised")

    signer = agent.AlgorandSigner(phrase)
    check("signer derives the account address", signer.address == address)
    # Inject the stub Algod the SDK scheme would otherwise construct per network.
    signer._scheme._clients[agent.ALGORAND_TESTNET] = StubAlgod(
        agent.ALGORAND_TESTNET.split(":", 1)[1]
    )

    # --- refuses an unexpected asset before signing ---
    wrong_asset = avm_requirements(pay_to)
    wrong_asset = wrong_asset.model_copy(update={"asset": "12345"})
    try:
        signer.create_payment_payload(wrong_asset)
    except agent.PaymentRefused:
        check("refuses a non-USDC ASA", True)
    else:
        check("refuses a non-USDC ASA", False, "no error raised")

    # --- plain path: one signed asset transfer, paymentIndex 0 ---
    payload = signer.create_payment_payload(avm_requirements(pay_to))
    group = payload["paymentGroup"]
    check("plain: single-transaction group", len(group) == 1, f"got {len(group)}")
    check("plain: paymentIndex is 0", payload["paymentIndex"] == 0)

    info = decode_base64_transaction(group[0])
    check("plain: transaction is an asset transfer", info.type == "axfer", f"got {info.type}")
    check("plain: sender is our address", info.sender == address)
    check("plain: receiver is payTo", info.asset_receiver == pay_to)
    check("plain: amount is 100000 atomic", info.asset_amount == 100_000)
    check("plain: asset is USDC testnet 10458941", info.asset_index == 10458941)
    check("plain: genesis hash is testnet's", info.genesis_hash == agent.ALGORAND_TESTNET[9:])
    check("plain: no rekey", info.rekey_to is None)
    check("plain: no close-to", info.asset_close_to is None)
    check("plain: transaction is signed", info.is_signed)
    valid, error = verify_transaction_signature(group[0])
    check("plain: ed25519 signature verifies against the sender", valid, error)

    # Two payments in a row must differ — the SDK notes each with time_ns(), so a
    # replayed group would be rejected by the server's request binding.
    second = signer.create_payment_payload(avm_requirements(pay_to))
    check("plain: consecutive payments are distinct", second["paymentGroup"][0] != group[0])

    # --- fee-abstraction path: unsigned fee payer first, signed transfer second ---
    fee_payer = account.generate_account()[1]
    grouped = signer.create_payment_payload(avm_requirements(pay_to, fee_payer=fee_payer))
    members = grouped["paymentGroup"]
    check("feePayer: two-transaction group", len(members) == 2, f"got {len(members)}")
    check("feePayer: paymentIndex points at the transfer", grouped["paymentIndex"] == 1)

    first_info = decode_base64_transaction(members[0])
    second_info = decode_base64_transaction(members[1])
    check("feePayer: slot 0 is the fee payer's self-payment", first_info.sender == fee_payer)
    check("feePayer: slot 0 is left unsigned for the facilitator", not first_info.is_signed)
    check("feePayer: slot 0 moves no ALGO", (first_info.amount or 0) == 0)
    check("feePayer: slot 0 carries the pooled fee", first_info.fee == 2000, f"{first_info.fee}")
    check("feePayer: slot 1 is our signed transfer", second_info.is_signed)
    check("feePayer: slot 1 pays zero fee", second_info.fee == 0)
    check(
        "feePayer: group ids match",
        bool(first_info.group) and first_info.group == second_info.group,
    )
    valid, error = verify_transaction_signature(members[1])
    check("feePayer: transfer signature verifies", valid, error)

    # --- the signer contract with the SDK, spelled out ---
    unsigned = base64.b64decode(encoding.msgpack_encode(_sample_txn(address)))
    signed = signer.sign_transactions([unsigned, unsigned], [1])
    check("sign_transactions returns None where it must not sign", signed[0] is None)
    check("sign_transactions returns raw msgpack bytes", isinstance(signed[1], bytes))
    check(
        "signed bytes decode as a SignedTransaction",
        decode_base64_transaction(base64.b64encode(signed[1]).decode()).is_signed,
    )


def _sample_txn(address: str) -> Any:
    """An unsigned asset transfer used to exercise ``sign_transactions`` directly."""
    from algosdk import transaction

    params = StubAlgod(agent.ALGORAND_TESTNET[9:]).suggested_params()
    return transaction.AssetTransferTxn(
        sender=address,
        sp=params,
        receiver=address,
        amt=1,
        index=10458941,
    )


# ---------------------------------------------------------------------------


async def main() -> int:
    """Run every check and report."""
    print("fantasy_agent selftest")
    await check_paid_flow()
    await check_cold_store()
    await check_unreachable()
    check_algorand_signer()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
