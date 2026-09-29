"""Dump the golden vectors the TypeScript builder is asserted against.

Deterministic by construction: fixed keys, fixed suggested params, fixed
notes, no network. Re-running it on any machine must produce a byte-identical
file, which is what `tests/test_golden_vectors.py` checks.

The keypairs below are derived from fixed seeds so signatures reproduce. They
hold nothing, on any network, ever, and no mnemonic for them exists.

    python -m agents.dump_golden_vectors
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import nacl.signing
from algosdk import encoding as algo_encoding
from algosdk import transaction

from agents.build_join_group import GroupNotes, build_join_group, decode_leg
from agents.common import AlgorandSigner

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "web" / "tests" / "golden" / "join-group.json"

GENESIS_HASH = base64.b64encode(bytes(range(32, 64))).decode()
GENESIS_ID = "testnet-v1.0"
FIRST_VALID = 41_000_000
LAST_VALID = 41_001_000


def _account_from_seed(seed: bytes) -> tuple[bytes, str]:
    """(64-byte private key, address) from a fixed 32-byte seed.

    algosdk derives an address from a 64-byte key rather than from a seed, so
    the expansion is done here with the same primitive algosdk uses.
    """
    signing_key = nacl.signing.SigningKey(seed)
    public_key = bytes(signing_key.verify_key)
    return seed + public_key, algo_encoding.encode_address(public_key)


BUYER_KEY, BUYER = _account_from_seed(bytes([1]) * 32)
_, FEE_PAYER = _account_from_seed(bytes([2]) * 32)
_, APP_ACCOUNT = _account_from_seed(bytes([3]) * 32)


class FixedParams:
    """The suggested-params interface `build_join_group` needs, with no
    network behind it."""

    def __init__(self, min_fee: int) -> None:
        self._min_fee = min_fee

    def suggested_params(self) -> transaction.SuggestedParams:
        return transaction.SuggestedParams(
            fee=0,
            flat_fee=True,
            first=FIRST_VALID,
            last=LAST_VALID,
            gh=GENESIS_HASH,
            gen=GENESIS_ID,
            min_fee=self._min_fee,
        )


CASES: list[dict[str, Any]] = [
    {
        "name": "baseline",
        "app_id": 741_000_001,
        "asset_id": 10_458_941,
        "amount": 10_000,
        "agreement_id": 42,
        "min_fee": 1000,
    },
    {
        # A different agreement id changes both box names and one ABI
        # argument -- the pair most likely to be got wrong together. The value
        # is the maximum uint64, which also pins the big-endian packing.
        "name": "max-agreement-id",
        "app_id": 741_000_001,
        "asset_id": 10_458_941,
        "amount": 250_000,
        "agreement_id": 18_446_744_073_709_551_615,
        "min_fee": 1000,
    },
    {
        # Proves the pooled fee is read rather than assumed.
        "name": "raised-min-fee",
        "app_id": 741_000_001,
        "asset_id": 31_566_704,
        "amount": 1,
        "agreement_id": 1,
        "min_fee": 4000,
    },
]


def _unsigned_legs(group: dict[str, Any]) -> list[transaction.Transaction]:
    """Strip signatures.

    What the two builders must agree on is the transaction; who signed it is
    the signer's business, and Task 3 asserts the signing indexes separately.
    Pinning a signature here would also mean committing a key to make it
    reproducible.
    """
    return [decode_leg(raw)[1] for raw in group["rawBytes"]]


def build_vectors() -> dict[str, Any]:
    buyer = AlgorandSigner(BUYER_KEY, BUYER)
    vectors = []

    for case in CASES:
        notes = GroupNotes(
            fee_payer=f"x402-fee-payer-{case['name']}".encode(),
            payment=f"x402-payment-{case['name']}".encode(),
        )
        group = build_join_group(
            algod_client=FixedParams(case["min_fee"]),
            buyer_signer=buyer,
            fee_payer_address=FEE_PAYER,
            app_id=case["app_id"],
            app_account=APP_ACCOUNT,
            asset_id=case["asset_id"],
            amount=case["amount"],
            agreement_id=case["agreement_id"],
            notes=notes,
        )
        vectors.append(
            {
                "name": case["name"],
                "input": {
                    "buyer": BUYER,
                    "feePayer": FEE_PAYER,
                    "appAccount": APP_ACCOUNT,
                    "appId": case["app_id"],
                    "assetId": case["asset_id"],
                    "amount": case["amount"],
                    # A string: the max-uint64 case is beyond
                    # Number.MAX_SAFE_INTEGER, and a JSON number would come
                    # back rounded on the TypeScript side -- which would build
                    # a different transaction while the vector looked right.
                    "agreementId": str(case["agreement_id"]),
                    "suggestedParams": {
                        "firstValid": FIRST_VALID,
                        "lastValid": LAST_VALID,
                        "genesisHash": GENESIS_HASH,
                        "genesisId": GENESIS_ID,
                        "minFee": case["min_fee"],
                    },
                    "notes": {
                        "feePayer": notes.fee_payer.decode(),
                        "payment": notes.payment.decode(),
                    },
                },
                "expected": {
                    "paymentIndex": group["paymentIndex"],
                    "indexesToSign": group["clientIndexes"],
                    "unsigned": [
                        algo_encoding.msgpack_encode(leg)
                        for leg in _unsigned_legs(group)
                    ],
                },
            }
        )

    return {
        "note": (
            "Generated by agents/dump_golden_vectors.py. Do not edit by hand: "
            "tests/test_golden_vectors.py re-runs the dumper and fails if this "
            "file no longer matches."
        ),
        "vectors": vectors,
    }


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(build_vectors(), indent=2) + "\n",
        encoding="utf-8",
        # Explicit, so a second run on Windows leaves git clean. The default
        # translates the newline to CRLF, and the idempotence check -- run it
        # twice, git status stays clean -- then fails on line endings alone.
        newline="\n",
    )
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()
