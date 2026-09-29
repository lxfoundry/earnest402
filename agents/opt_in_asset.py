"""Opt the buyer account into an Algorand Standard Asset.

An Algorand address cannot receive units of an ASA it has not opted into,
so this must run before a faucet send or a transfer can fund the buyer.
The opt-in is a 0-amount asset transfer from the account to itself.

Opting in raises the account's minimum balance by 0.1 ALGO, which stays
locked while the opt-in stands.

Usage:
    python -m agents.opt_in_asset --asset-id 10458941
    python -m agents.opt_in_asset --asset-id 10458941 --mnemonic-env E2E_TREASURY_MNEMONIC
"""

from __future__ import annotations

import argparse
import base64

from algosdk import encoding, transaction

from agents.common import (
    get_algod_client,
    get_buyer_signer,
    get_signer_from_mnemonic_env,
)

# Minimum balance increase per ASA opt-in, plus headroom for the fee.
OPT_IN_COST_MICROALGO = 101_000


def opt_in(asset_id: int, *, mnemonic_env: str | None = None) -> str | None:
    """Opt an account into `asset_id`. Returns the txid, or None if already in.

    Defaults to the buyer. `mnemonic_env` names an environment variable
    holding a mnemonic instead, which is how the other end-to-end roles --
    the treasury above all, since a beneficiary that cannot receive is
    rejected at `create_agreement` rather than at release -- are opted in.
    """
    algod = get_algod_client()
    signer = (
        get_signer_from_mnemonic_env(mnemonic_env)
        if mnemonic_env
        else get_buyer_signer()
    )
    address = signer.address

    info = algod.account_info(address)
    held = {asset["asset-id"] for asset in info.get("assets", [])}
    if asset_id in held:
        print(f"{address} is already opted into asset {asset_id}; nothing to do")
        return None

    balance = info.get("amount", 0)
    if balance < OPT_IN_COST_MICROALGO:
        raise SystemExit(
            f"{address} holds {balance} microALGO, which cannot cover the "
            f"{OPT_IN_COST_MICROALGO} microALGO an opt-in requires (0.1 ALGO "
            f"minimum-balance increase plus fee). Fund it before retrying."
        )

    params = algod.suggested_params()
    txn = transaction.AssetTransferTxn(
        sender=address,
        sp=params,
        receiver=address,
        amt=0,
        index=asset_id,
    )

    unsigned = base64.b64decode(encoding.msgpack_encode(txn))
    signed = signer.sign_transactions([unsigned], [0])[0]
    assert signed is not None, "signer returned no signature for index 0"

    txid = algod.send_raw_transaction(base64.b64encode(signed).decode())
    transaction.wait_for_confirmation(algod, txid, 4)
    print(f"{address} opted into asset {asset_id} in {txid}")
    return txid


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--asset-id",
        type=int,
        required=True,
        help="ASA id to opt into, e.g. 10458941 for TestNet USDC",
    )
    parser.add_argument(
        "--mnemonic-env",
        default=None,
        help="environment variable holding the mnemonic to sign with; defaults to the buyer's AVM_PRIVATE_KEY",
    )
    args = parser.parse_args()
    opt_in(args.asset_id, mnemonic_env=args.mnemonic_env)


if __name__ == "__main__":
    main()
