"""Objective A: the real join-path attempt against api/server.py.

Flow: GET/POST the paid route unauthenticated -> expect 402 -> decode the
payment requirements the resource server advertised -> build the 3-txn join
group (agents/build_join_group.py) -> wrap it into a payment payload and
encode it with the installed x402-avm package's own encoder -> retry the
request with that payload attached -> print the full response so the result
can be pasted into a findings log.

A note on the payload header, since the task that requested this script
named it "X-PAYMENT": inspecting the installed x402-avm 2.0.2 source
(x402/http/constants.py, x402/http/x402_http_server_base.py) shows that name
is protocol V1 legacy only. This SDK's V2 protocol -- what api/server.py
actually runs, and what the 402 response below will carry -- uses two
different header names instead:

  - 402 response: PAYMENT-REQUIRED (base64 JSON of PaymentRequired)
  - payment retry: PAYMENT-SIGNATURE (base64 JSON of PaymentPayload)

The server's `_extract_payment` (x402_http_server_base.py) only ever reads
PAYMENT-SIGNATURE; it does not fall back to X-PAYMENT. This script follows
what the installed package actually does, not the literal header name.
"""

from __future__ import annotations

import argparse
import os
from typing import TYPE_CHECKING

import httpx
from dotenv import load_dotenv
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_required_header,
    encode_payment_signature_header,
)
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements

# Nothing here loads .env at import time. This module's own load_dotenv() runs
# in `main`, and every import that reaches `agents.common` -- which calls
# load_dotenv() when it is imported -- is made inside a function, so a test
# importing this module does not copy the developer's .env into its process.
if TYPE_CHECKING:
    from agents.common import AlgorandSigner


def select_requirements(payment_required: PaymentRequired) -> PaymentRequirements:
    """Pick the exact/AVM-TestNet entry out of the 402 response's `accepts`."""
    for req in payment_required.accepts:
        if req.scheme == "exact" and req.network == ALGORAND_TESTNET_CAIP2:
            return req
    raise ValueError(
        "no exact/AVM-TestNet payment requirement in the 402 response's "
        f"accepts list: {payment_required.accepts!r}"
    )


def choose_buyer_signer(mnemonic_env: str | None) -> AlgorandSigner:
    """The key that pays and is recorded on the roster.

    `join` refuses a payer already on the roster, so filling a multi-seat pool
    by hand takes one key per seat -- `agents/seat_accounts.py` records them as
    E2E_SEAT_<n>_MNEMONIC. Naming a variable selects that key; naming none
    keeps the buyer's AVM_PRIVATE_KEY. An empty name is passed through rather
    than treated as absent, so it stops the run instead of quietly paying
    from the buyer the operator was trying not to use.
    """
    from agents.common import get_buyer_signer, get_signer_from_mnemonic_env

    if mnemonic_env is not None:
        return get_signer_from_mnemonic_env(mnemonic_env)
    return get_buyer_signer()


def main(argv: list[str] | None = None) -> None:
    from agents.build_join_group import VARIANT_FEE_PAYER_FIRST, build_join_group
    from agents.common import dump_response, get_algod_client

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agreement-id",
        type=int,
        default=1,
        help="box key `join` records the payer under (default: 1)",
    )
    parser.add_argument(
        "--variant",
        default=VARIANT_FEE_PAYER_FIRST,
        help="transaction ordering to try (default: fee_payer_first)",
    )
    parser.add_argument(
        "--route",
        default="/escrow/join-spike",
        help=(
            "route to pay, with or without a leading slash and with an "
            "optional query string (default: /escrow/join-spike). The query "
            "is sent on both the unpaid and the paid pass, which is what a "
            "route taking its input in the query requires"
        ),
    )
    parser.add_argument(
        "--mnemonic-env",
        metavar="NAME",
        help=(
            "pay with the 25-word mnemonic held in environment variable NAME "
            "instead of AVM_PRIVATE_KEY, e.g. E2E_SEAT_1_MNEMONIC"
        ),
    )
    args = parser.parse_args(argv)

    load_dotenv()
    # Resolved before the first request, so a missing or misnamed variable
    # stops the run with nothing sent.
    buyer_signer = choose_buyer_signer(args.mnemonic_env)

    resource_host = os.environ["RESOURCE_HOST"]
    app_id = int(os.environ["APP_ID"])
    url = f"{resource_host.rstrip('/')}/{args.route.lstrip('/')}"

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

        group = build_join_group(
            algod_client=algod_client,
            buyer_signer=buyer_signer,
            fee_payer_address=fee_payer,
            app_id=app_id,
            app_account=requirements.pay_to,
            asset_id=int(requirements.asset),
            amount=int(requirements.amount),
            agreement_id=args.agreement_id,
            variant=args.variant,
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
