"""Shared helpers for TestNet spike scripts.

Loads environment configuration once via python-dotenv and exposes an
Algorand TestNet algod client plus a buyer signer implementing the
x402-avm ClientAvmSigner protocol (address property + sign_transactions).
"""

from __future__ import annotations

import base64
import os

from algosdk import encoding, mnemonic, transaction
from algosdk.v2client import algod
from dotenv import load_dotenv

load_dotenv()

# Public AlgoNode TestNet endpoint, used when ALGOD_TESTNET_URL is unset.
DEFAULT_ALGOD_TESTNET_URL = "https://testnet-api.algonode.cloud"


def get_algod_client() -> algod.AlgodClient:
    """Build an algod client against Algorand TestNet.

    Uses ALGOD_TESTNET_URL if set, otherwise the public AlgoNode TestNet
    endpoint, which takes no token.
    """
    url = os.getenv("ALGOD_TESTNET_URL") or DEFAULT_ALGOD_TESTNET_URL
    return algod.AlgodClient("", url)


class AlgorandSigner:
    """Implements the x402-avm ClientAvmSigner protocol using algosdk.

    The x402 SDK passes raw msgpack transaction bytes; algosdk works with
    base64-encoded strings. The conversion happens here, at the boundary.
    """

    def __init__(self, secret_key: bytes, address: str):
        self._secret_key = secret_key
        self._address = address

    @property
    def address(self) -> str:
        return self._address

    def sign_transactions(
        self,
        unsigned_txns: list[bytes],
        indexes_to_sign: list[int],
    ) -> list[bytes | None]:
        sk_b64 = base64.b64encode(self._secret_key).decode()
        result: list[bytes | None] = []
        for i, txn_bytes in enumerate(unsigned_txns):
            if i in indexes_to_sign:
                txn = encoding.msgpack_decode(base64.b64encode(txn_bytes).decode())
                signed = txn.sign(sk_b64)
                result.append(base64.b64decode(encoding.msgpack_encode(signed)))
            else:
                result.append(None)
        return result


def priced_params(algod_client: algod.AlgodClient) -> transaction.SuggestedParams:
    """Suggested params that are known to carry a usable fee.

    Every group in this directory is priced from the live protocol minimum
    rather than a constant, because a hardcoded 1000 sends the wrong fee after
    any protocol change -- silently, as an underpaid group the node rejects.
    Absent or zero `min_fee` means the params are not usable, not that the
    minimum is 1000: defaulting would quietly reintroduce the constant the
    builders exist to have removed.

    Checked here, once, where the params enter the process, so a builder
    written later cannot forget to check and a second copy of the rule cannot
    drift from this one. The TypeScript port refuses the same input.
    """
    sp = algod_client.suggested_params()
    if not sp.min_fee:
        raise ValueError("suggested params carry no min_fee; cannot price the group")
    return sp


def dump_response(label: str, response: object) -> None:
    """Print an HTTP response (status, headers, body) for pasting into a
    findings log. Shared by the spike's client scripts so their console
    output has one consistent shape.

    `response` is expected to be an httpx.Response (duck-typed here so this
    module does not have to import httpx unconditionally).
    """
    print(f"--- {label} ---")
    print(f"status: {response.status_code}")
    print("headers:")
    for key, value in response.headers.items():
        print(f"  {key}: {value}")
    print("body:")
    try:
        import json

        print(json.dumps(response.json(), indent=2))
    except ValueError:
        print(response.text)
    print()


def get_buyer_signer() -> AlgorandSigner:
    """Build the buyer signer from AVM_PRIVATE_KEY.

    AVM_PRIVATE_KEY is a Base64-encoded 64-byte key: a 32-byte Ed25519
    seed followed by the 32-byte public key, matching the x402-avm client
    signer convention.
    """
    avm_private_key = os.environ["AVM_PRIVATE_KEY"]
    secret_key = base64.b64decode(avm_private_key)
    if len(secret_key) != 64:
        raise ValueError("AVM_PRIVATE_KEY must be a Base64-encoded 64-byte key")

    address = encoding.encode_address(secret_key[32:])
    return AlgorandSigner(secret_key, address)


def get_signer_from_mnemonic_env(variable: str) -> AlgorandSigner:
    """Build a signer from an environment variable holding a 25-word mnemonic.

    The buyer's key is stored as AVM_PRIVATE_KEY because that is the form the
    x402-avm client signer convention uses. Every other role in the end-to-end
    bootstrap is stored as a mnemonic, which is the form AlgoKit and the
    Algorand wallets speak, so both forms have to be loadable.
    """
    phrase = os.environ.get(variable)
    if not phrase:
        raise SystemExit(f"{variable} is not set; run `python -m agents.e2e_bootstrap`")
    secret_key = base64.b64decode(mnemonic.to_private_key(phrase))
    return AlgorandSigner(secret_key, encoding.encode_address(secret_key[32:]))
