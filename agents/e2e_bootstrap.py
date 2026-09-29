"""Generate and record the accounts the TestNet end-to-end suite signs with.

Four identities take part in a `hash` agreement, and they must be four
distinct accounts:

  admin      creates agreements, and is also the deployer -- `bootstrap` sets
             `admin = Txn.sender`, so whoever deploys the application is its
             admin and the two cannot be separated
  verifier   the only address that can release. The spec requires a different
             key from admin: an admin who could also release would make the
             release condition a promise rather than a constraint
  treasury   the `hash` beneficiary, which receives on release
  buyer      pays the 402

Mnemonics are written straight to `.env`, which is gitignored, and are never
printed. That is the whole reason this is a script: key material that passes
through a terminal is key material in a scrollback buffer.

An existing value is left alone unless `--force` is given, so re-running this
after a partial bootstrap is safe.

Usage:
    python -m agents.e2e_bootstrap
    E2E_FORCE_CONFIRM=yes python -m agents.e2e_bootstrap --force
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from algosdk import account, mnemonic
from dotenv import dotenv_values, set_key

from agents.common import get_algod_client

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CONTRACTS_ENV_PATH = REPO_ROOT / "contracts" / ".env.testnet"

USDC_TESTNET_ASA_ID = 10458941
TESTNET_CAIP2 = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="

# The facilitator's advertised fee sponsor, observed on TestNet and recorded in
# tests/FINDINGS.md. This value is used as configured: api/pricing.py puts
# settings.fee_payer straight into extra.feePayer, and nothing reconciles it
# against /supported. If the facilitator rotates its sponsor, verify fails with
# `fee_payer_not_managed_by_facilitator` and this constant is what to update.
FACILITATOR_FEE_SPONSOR = "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA"

# Enough for the deploy (which sends the application account 1 ALGO of its own)
# plus the 95,800 microALGO every live agreement parks until it is closed.
ADMIN_ALGO = 5
# Minimum balance, one ASA opt-in, and fees on the calls each one sends.
# A fee is ~0.001, so these are almost all minimum balance and headroom.
VERIFIER_ALGO = 1
TREASURY_ALGO = 1
BUYER_ALGO = 1


def _is_set(values: dict[str, str | None], key: str) -> bool:
    return bool(values.get(key))


def _address_of(phrase: str) -> str:
    return account.address_from_private_key(mnemonic.to_private_key(phrase))


def _account_for(
    values: dict[str, str | None],
    mnemonic_key: str,
    *,
    force: bool,
) -> tuple[str, str, bool]:
    """Return (mnemonic, address, is_new) for one role.

    An existing mnemonic is reused rather than replaced, so a second run does
    not orphan a funded account -- which on TestNet means orphaning the USDC
    that took a captcha to obtain.
    """
    existing = values.get(mnemonic_key)
    if existing and not force:
        return existing, _address_of(existing), False
    private_key, _address = account.generate_account()
    phrase = mnemonic.from_private_key(private_key)
    return phrase, _address_of(phrase), True


def _buyer_for(values: dict[str, str | None], *, force: bool) -> tuple[str, str, bool]:
    """The buyer is the one role with a predecessor.

    The August spike left a funded, USDC-opted-in account behind in
    AVM_BUYER_MNEMONIC, and TestNet USDC has no programmatic dispenser -- so
    inheriting it is what makes the captcha step unnecessary. Generating a
    fresh buyer over the top would strand the only scarce resource in the
    bootstrap.
    """
    inherited = values.get("AVM_BUYER_MNEMONIC")
    if inherited and not _is_set(values, "E2E_BUYER_MNEMONIC") and not force:
        return inherited, _address_of(inherited), False
    return _account_for(values, "E2E_BUYER_MNEMONIC", force=force)


def bootstrap(*, force: bool) -> dict[str, tuple[str, bool, int]]:
    """Generate what is missing, write it, and return {role: (address, new, algo)}."""
    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    ENV_PATH.touch(exist_ok=True)
    CONTRACTS_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACTS_ENV_PATH.touch(exist_ok=True)

    admin_mn, admin_addr, admin_new = _account_for(
        values, "ADMIN_MNEMONIC", force=force
    )
    verifier_mn, verifier_addr, verifier_new = _account_for(
        values, "E2E_VERIFIER_MNEMONIC", force=force
    )
    treasury_mn, treasury_addr, treasury_new = _account_for(
        values, "E2E_TREASURY_MNEMONIC", force=force
    )
    buyer_mn, buyer_addr, buyer_new = _buyer_for(values, force=force)

    for key, value in {
        # Read by api/config.py.
        "ADMIN_MNEMONIC": admin_mn,
        "TREASURY": treasury_addr,
        "VERIFIER": verifier_addr,
        "FEE_PAYER": FACILITATOR_FEE_SPONSOR,
        "NETWORK": TESTNET_CAIP2,
        "USDC_ASA_ID": str(USDC_TESTNET_ASA_ID),
        # Read by tests/e2e.
        "E2E_ENABLED": "1",
        "E2E_BUYER_MNEMONIC": buyer_mn,
        "E2E_VERIFIER_MNEMONIC": verifier_mn,
        "E2E_TREASURY_MNEMONIC": treasury_mn,
        # Short enough that the refund scenario's wait is bearable. There is no
        # timestamp offset on TestNet, so this is real elapsed time.
        "E2E_DEADLINE_SECONDS": "120",
    }.items():
        if _is_set(values, key) and not force:
            continue
        set_key(str(ENV_PATH), key, value, quote_mode="auto")

    # AlgoKit reads contracts/.env.testnet for `algokit project deploy testnet`.
    # DEPLOYER_MNEMONIC is deliberately the same account as ADMIN_MNEMONIC, and
    # VERIFIER_MNEMONIC the same account as E2E_VERIFIER_MNEMONIC:
    # `agents.release_edition` signs with it, and reads it from this file
    # rather than from .env, which the server loads too.
    contracts_values = dotenv_values(CONTRACTS_ENV_PATH)
    for key, value in {
        "ALGOD_SERVER": "https://testnet-api.algonode.cloud",
        "ALGOD_PORT": "443",
        "ALGOD_TOKEN": "",
        "INDEXER_SERVER": "https://testnet-idx.algonode.cloud",
        "INDEXER_PORT": "443",
        "INDEXER_TOKEN": "",
        "DEPLOYER_MNEMONIC": admin_mn,
        "VERIFIER_MNEMONIC": verifier_mn,
        "USDC_ASSET_ID": str(USDC_TESTNET_ASA_ID),
        "TREASURY_ADDRESS": treasury_addr,
    }.items():
        if _is_set(contracts_values, key) and not force:
            continue
        set_key(str(CONTRACTS_ENV_PATH), key, value, quote_mode="auto")

    return {
        "admin (also deployer)": (admin_addr, admin_new, ADMIN_ALGO),
        "verifier": (verifier_addr, verifier_new, VERIFIER_ALGO),
        "treasury": (treasury_addr, treasury_new, TREASURY_ALGO),
        "buyer": (buyer_addr, buyer_new, BUYER_ALGO),
    }


def report(roles: dict[str, tuple[str, bool, int]]) -> None:
    """Print addresses and what each still needs. Never prints a mnemonic."""
    algod = get_algod_client()
    print(f"\nWrote {ENV_PATH}\n  and {CONTRACTS_ENV_PATH}\nBoth are gitignored.\n")
    print("Fund from https://lora.algokit.io/testnet/fund\n")
    header = f"{'role':22} {'address':60} {'ALGO':>9} {'needs':>7}  state"
    print(header)
    print("-" * len(header))
    for role, (address, is_new, want_algo) in roles.items():
        algo = 0.0
        usdc = None
        try:
            info = algod.account_info(address)
            algo = info.get("amount", 0) / 1e6
            holdings = {a["asset-id"]: a for a in info.get("assets", [])}
            usdc = holdings.get(USDC_TESTNET_ASA_ID)
        except Exception as exc:  # unfunded accounts are not yet known to algod
            if "no accounts found" not in str(exc).lower():
                raise
        short = want_algo - algo
        need = "ok" if short <= 0.01 else f"+{short:.2f}"
        print(
            f"{role:22} {address:60} {algo:9.3f} {need:>7}  "
            f"{'new' if is_new else 'reused'}"
        )
        if usdc is not None:
            frozen = " FROZEN" if usdc.get("is-frozen") else ""
            print(f"{'':22} {'':60} {usdc['amount'] / 1e6:9.3f} USDC{frozen}")
    print(
        "\nThe buyer's USDC recirculates rather than draining: release sends it "
        "to the treasury and teardown sends it back, and a refund returns it "
        "directly. No Circle faucet visit is needed while it holds a balance."
    )
    print(
        "APP_ID and APP_ACCOUNT still point at the August spike's JoinSpike "
        "application. Deploying the escrow overwrites them."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="regenerate accounts even where .env already holds one",
    )
    args = parser.parse_args()
    if args.force and os.environ.get("E2E_FORCE_CONFIRM") != "yes":
        raise SystemExit(
            "--force replaces existing keys and strands whatever they hold. "
            "Re-run with E2E_FORCE_CONFIRM=yes if that is what you want."
        )
    report(bootstrap(force=args.force))


if __name__ == "__main__":
    main()
