"""The standard, spec-compliant 2-txn settlement: buyer axfer + a
facilitator-signed fee-payer leg, no appCall. Same handshake as
`join_client_manual.py` (GET/POST -> 402 -> build -> encode -> retry)
against the same route, but the group itself follows the reference
"Fee-Abstracted Payment Group" example verbatim (`.claude/skills/
algorand-x402-python/references/use-python-x402-core-avm-examples.md`,
"Fee-Abstracted Payment Group"), reusing only the fee-payer leg builder
from `build_join_group.py` (the two scripts' fee-payer leg is otherwise
identical, just pooled over a different number of transactions).

This script exists purely so the discovery self-test (Objective B) still
has a landed payment to check against if Objective A's 3-txn group is
rejected: a normal payment to the same `payTo` proves the route's discovery
declaration and attribution tag work, independent of whether the join-path
group shape is ever accepted.
"""

from __future__ import annotations

import base64
import os

import httpx
from algosdk import encoding, transaction
from dotenv import load_dotenv
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.schemas import PaymentPayload

from agents.build_join_group import build_fee_payer_leg, default_notes
from agents.common import (
    AlgorandSigner,
    dump_response,
    get_algod_client,
    get_buyer_signer,
    priced_params,
)
from agents.join_client_manual import select_requirements

load_dotenv()


def build_plain_group(
    *,
    algod_client,
    buyer_signer: AlgorandSigner,
    fee_payer_address: str,
    app_account: str,
    asset_id: int,
    amount: int,
) -> dict:
    """The reference 2-txn fee-abstracted group, verbatim ordering
    ([axfer, feePayer], paymentIndex 0).
    """
    # Two legs at the live minimum. `priced_params` is what refuses params
    # with no usable min_fee, for the same reason build_join_group leans on it.
    sp = priced_params(algod_client)
    pooled_fee = sp.min_fee * 2

    payment_sp = transaction.SuggestedParams(
        fee=0,
        flat_fee=True,
        first=sp.first,
        last=sp.last,
        gh=sp.gh,
        gen=sp.gen,
        min_fee=sp.min_fee,
    )
    payment_txn = transaction.AssetTransferTxn(
        sender=buyer_signer.address,
        sp=payment_sp,
        receiver=app_account,
        amt=amount,
        index=asset_id,
    )
    fee_payer_txn = build_fee_payer_leg(
        sp, fee_payer_address, pooled_fee, default_notes().fee_payer
    )

    gid = transaction.calculate_group_id([payment_txn, fee_payer_txn])
    payment_txn.group = gid
    fee_payer_txn.group = gid

    payment_bytes = base64.b64decode(encoding.msgpack_encode(payment_txn))
    fee_payer_bytes = base64.b64decode(encoding.msgpack_encode(fee_payer_txn))

    signed = buyer_signer.sign_transactions([payment_bytes, fee_payer_bytes], [0])
    signed_payment_bytes = signed[0] or payment_bytes

    return {
        "paymentGroup": [
            base64.b64encode(signed_payment_bytes).decode("utf-8"),
            base64.b64encode(fee_payer_bytes).decode("utf-8"),
        ],
        "paymentIndex": 0,
        "rawBytes": [signed_payment_bytes, fee_payer_bytes],
    }


def main() -> None:
    resource_host = os.environ["RESOURCE_HOST"]
    # ROUTE_PATH lets this same control settle against a second route on the
    # same host, which is how the discovery experiment varies one field at a
    # time rather than standing up a whole second deployment. It is accepted
    # with or without a leading slash: without normalising, "escrow/join-spike"
    # would silently build "https://hostescrow/join-spike" and pay the wrong
    # place rather than fail.
    route_path = os.getenv("ROUTE_PATH", "/escrow/join-spike")
    if not route_path.startswith("/"):
        route_path = f"/{route_path}"
    url = f"{resource_host.rstrip('/')}{route_path}"

    with httpx.Client(timeout=30.0) as http:
        first = http.post(url)
        dump_response(f"POST {url} (unauthenticated)", first)

        if first.status_code != 402:
            print(f"expected 402, got {first.status_code} -- stopping here.")
            return

        header = first.headers.get(PAYMENT_REQUIRED_HEADER)
        if not header:
            raise RuntimeError(
                f"402 response carried no {PAYMENT_REQUIRED_HEADER} header"
            )
        payment_required = decode_payment_required_header(header)
        requirements = select_requirements(payment_required)
        print(f"selected requirements: {requirements!r}\n")

        fee_payer = (requirements.extra or {}).get("feePayer")
        if not fee_payer:
            raise RuntimeError(
                "selected requirements carry no extra.feePayer -- cannot "
                "build a fee-abstracted group"
            )

        algod_client = get_algod_client()
        buyer_signer = get_buyer_signer()

        group = build_plain_group(
            algod_client=algod_client,
            buyer_signer=buyer_signer,
            fee_payer_address=fee_payer,
            app_account=requirements.pay_to,
            asset_id=int(requirements.asset),
            amount=int(requirements.amount),
        )

        payload = PaymentPayload(
            x402_version=payment_required.x402_version,
            payload={
                "paymentGroup": group["paymentGroup"],
                "paymentIndex": group["paymentIndex"],
            },
            accepted=requirements,
            resource=payment_required.resource,
            extensions=payment_required.extensions,
        )
        encoded = encode_payment_signature_header(payload)

        retry = http.post(url, headers={PAYMENT_SIGNATURE_HEADER: encoded})
        dump_response(f"POST {url} (with {PAYMENT_SIGNATURE_HEADER})", retry)


if __name__ == "__main__":
    main()
