#!/usr/bin/env python3
"""Opt a Play Clock wallet into the USDC ASA, and report its balances.

Both ends of an x402 payment must be opted into the USDC asset or settlement
dies at simulate and surfaces to the caller as a second 402, unbilled
(DESIGN_NOTES.md:151). This is the tool that does that opt-in, and the one to
run first when a settle mysteriously fails.

``status`` works from the public addresses in infra/deploy.md §1b and reads no
secret at all. Only ``optin`` reads a mnemonic from Secret Manager, for the one
wallet it is signing for, and never prints it; it refuses if the mnemonic does
not derive the published address. Only addresses, balances, and transaction
ids reach stdout.

    # See where every wallet stands. Reads nothing secret, signs nothing.
    uv run python infra/optin.py status

    # Opt one wallet in. Needs ~0.2 ALGO in the account already.
    uv run python infra/optin.py optin --wallet testnet-merchant

    # Opt in every wallet on a network that still needs it.
    uv run python infra/optin.py optin --network testnet --all
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable

from algosdk import account, mnemonic, transaction
from algosdk.v2client import algod

# USDC asset ids, verified per network (DESIGN_NOTES.md:210). 6 decimals both.
USDC_ASSET = {"testnet": 10458941, "mainnet": 31566704}

ALGOD_URL = {
    "testnet": "https://testnet-api.algonode.cloud",
    "mainnet": "https://mainnet-api.algonode.cloud",
}

# Cloud Run gets the merchant address as plain config; the payer mnemonic is
# only ever used locally, by the example agent and by this script.
# name -> (network, Secret Manager secret, public address). The addresses are
# the published ones in infra/deploy.md §1b; keep the two in step.
WALLETS = {
    "testnet-merchant": (
        "testnet",
        "playclock-testnet-merchant",
        "BJXTJUHHMDDH36GEDNZA4MPXDQ6UMMOKACFN3DMF3TD3HZZKXJEGKXPUYI",
    ),
    "testnet-payer": (
        "testnet",
        "playclock-testnet-payer",
        "KAELWUHBMCXQZMEVIWHOGRDHIUJ27SBW4OCBIUHIAEC4CU5KTPSPJVGKTQ",
    ),
    "mainnet-merchant": (
        "mainnet",
        "playclock-mainnet-merchant",
        "MDBJMM6RJ4TM7W5FITZ3MWJTGTHMC4SKQJWRWI2JCQUKIR5LA7BPIUTMMM",
    ),
    "mainnet-payer": (
        "mainnet",
        "playclock-mainnet-payer",
        "RCAUDGDLWUQAG64IPLRZPYMXPVQXTR272SZS3GX66DDTLRABRTQ3FRMXT4",
    ),
}

PROJECT = "playclock"

# An opt-in is a 0-amount transfer to yourself, and it raises the account's
# minimum balance by 0.1 ALGO. 0.2 leaves room for that plus fees.
MIN_ALGO_FOR_OPTIN = 201_000  # microAlgos


def read_mnemonic(secret: str) -> str:
    """Pull a mnemonic out of Secret Manager. Never logged, never written down."""
    proc = subprocess.run(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "latest",
            "--secret",
            secret,
            "--project",
            PROJECT,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"cannot read secret {secret}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def client(network: str) -> algod.AlgodClient:
    return algod.AlgodClient("", ALGOD_URL[network])


def account_state(network: str, address: str) -> tuple[int, int | None]:
    """Return (microAlgo balance, USDC micro-units or None if not opted in)."""
    info = client(network).account_info(address)
    usdc = None
    for held in info.get("assets", []):
        if held["asset-id"] == USDC_ASSET[network]:
            usdc = held["amount"]
            break
    return info["amount"], usdc


def show_status(
    only_network: str | None,
    state: Callable[[str, str], tuple[int, int | None]] = account_state,
) -> None:
    """Print every wallet's balances from its public address. Reads no secret."""
    rows = [
        (name, net, address)
        for name, (net, _secret, address) in WALLETS.items()
        if only_network in (None, net)
    ]
    print(f"{'wallet':<18} {'address':<60} {'ALGO':>10}  {'USDC':>10}  opted in")
    for name, net, address in rows:
        try:
            algos, usdc = state(net, address)
        except Exception as exc:  # unfunded accounts 404 on some nodes
            print(f"{name:<18} {address:<60} {'—':>10}  {'—':>10}  unfunded ({type(exc).__name__})")
            continue
        usdc_text = "—" if usdc is None else f"{usdc / 1e6:.2f}"
        print(
            f"{name:<18} {address:<60} {algos / 1e6:>10.4f}  {usdc_text:>10}  "
            f"{'yes' if usdc is not None else 'NO'}"
        )


def opt_in(name: str) -> None:
    network, secret, published = WALLETS[name]
    phrase = read_mnemonic(secret)
    private_key = mnemonic.to_private_key(phrase)
    address = account.address_from_private_key(private_key)
    if address != published:
        sys.exit(f"{name}: secret {secret} does not derive the published address {published}")
    asset_id = USDC_ASSET[network]

    algos, usdc = account_state(network, address)
    if usdc is not None:
        print(f"{name}: already opted into USDC ({asset_id}) — nothing to do")
        return
    if algos < MIN_ALGO_FOR_OPTIN:
        sys.exit(
            f"{name}: only {algos / 1e6:.4f} ALGO at {address}; "
            f"need at least {MIN_ALGO_FOR_OPTIN / 1e6:.3f} to opt in"
        )

    algod_client = client(network)
    unsigned = transaction.AssetTransferTxn(
        sender=address,
        sp=algod_client.suggested_params(),
        receiver=address,
        amt=0,
        index=asset_id,
    )
    txid = algod_client.send_transaction(unsigned.sign(private_key))
    transaction.wait_for_confirmation(algod_client, txid, 8)
    print(f"{name}: opted into USDC ({asset_id}) on {network} — txid {txid}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="show balances and opt-in state")
    status.add_argument("--network", choices=sorted(ALGOD_URL))

    optin = sub.add_parser("optin", help="opt a wallet into USDC")
    optin.add_argument("--wallet", choices=sorted(WALLETS))
    optin.add_argument("--network", choices=sorted(ALGOD_URL))
    optin.add_argument("--all", action="store_true", help="every wallet on --network")

    args = parser.parse_args()

    if args.command == "status":
        show_status(args.network)
        return

    if args.all:
        if not args.network:
            sys.exit("--all needs --network, so a stray keystroke cannot touch MainNet")
        targets = [n for n, (net, _, _) in WALLETS.items() if net == args.network]
    elif args.wallet:
        targets = [args.wallet]
    else:
        sys.exit("pass --wallet, or --network with --all")

    for name in targets:
        opt_in(name)


if __name__ == "__main__":
    main()
