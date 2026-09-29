"""Objective A disambiguation: bypass api/server.py entirely and post the
same 3-txn join group directly to the GoPlausible facilitator's own
`/verify` and `/settle` HTTP endpoints.

If `join_client_manual.py`'s attempt is rejected before ever reaching the
facilitator -- i.e. our own server-side SDK usage (`x402ResourceServer` /
the FastAPI middleware) rejects the payload shape first -- this script tells
us whether the facilitator itself would have accepted it, by removing the
resource server from the path altogether.

There is no 402 response to parse here (there is no resource server in the
loop), so the payment requirements this script targets are built by hand
from environment configuration rather than read off a live response. The
fee-payer address is discovered from the facilitator's own `/supported`
endpoint (`HTTPFacilitatorClientSync.get_supported()`), which reports the
sponsor addresses it actually manages -- using a made-up address there would
fail for an unrelated reason (facilitator does not hold that key) before we
ever learn whether the 3-txn shape itself is accepted.

Uses `HTTPFacilitatorClientSync` from the installed x402-avm package for the
`/verify` and `/settle` calls rather than hand-rolling the request bodies,
for the same reason `join_client_manual.py` uses the package's own header
encoder: its actual JSON envelope (x402/http/facilitator_client_base.py,
`_build_request_body`) is the ground truth, not something to guess at.
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from x402.http import FacilitatorConfig, HTTPFacilitatorClientSync
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2, USDC_TESTNET_ASA_ID
from x402.schemas import PaymentPayload, PaymentRequirements

from agents.build_join_group import VARIANT_FEE_PAYER_FIRST, build_join_group
from agents.common import get_algod_client, get_buyer_signer

load_dotenv()

# Matches api/server.py's route price (10000 = 0.01 USDC, 6 decimals).
DEFAULT_AMOUNT = "10000"

# x402-avm's own default when a route leaves max_timeout_seconds unset
# (x402/server_base.py: `config.max_timeout_seconds or 300`).
DEFAULT_MAX_TIMEOUT_SECONDS = 300


def pick_fee_payer(signers: dict[str, list[str]]) -> str:
    """Pick a facilitator-managed signer address for the algorand family."""
    for family, addresses in signers.items():
        if family.startswith("algorand") and addresses:
            return addresses[0]
    raise RuntimeError(
        f"facilitator /supported carried no algorand signer address: {signers!r}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agreement-id", type=int, default=1)
    parser.add_argument("--variant", default=VARIANT_FEE_PAYER_FIRST)
    parser.add_argument("--amount", default=DEFAULT_AMOUNT)
    args = parser.parse_args()

    facilitator_url = os.environ.get(
        "FACILITATOR_URL", "https://facilitator.goplausible.xyz"
    )
    app_id = int(os.environ["APP_ID"])
    app_account = os.environ["APP_ACCOUNT"]
    asset_id = int(os.environ.get("USDC_TESTNET_ASA_ID") or USDC_TESTNET_ASA_ID)

    facilitator = HTTPFacilitatorClientSync(FacilitatorConfig(url=facilitator_url))

    print("--- GET /supported ---")
    supported = facilitator.get_supported()
    print(supported.model_dump_json(by_alias=True, exclude_none=True, indent=2))
    print()

    fee_payer_address = pick_fee_payer(supported.signers)
    print(f"using fee payer: {fee_payer_address}\n")

    algod_client = get_algod_client()
    buyer_signer = get_buyer_signer()

    group = build_join_group(
        algod_client=algod_client,
        buyer_signer=buyer_signer,
        fee_payer_address=fee_payer_address,
        app_id=app_id,
        app_account=app_account,
        asset_id=asset_id,
        amount=int(args.amount),
        agreement_id=args.agreement_id,
        variant=args.variant,
    )

    requirements = PaymentRequirements(
        scheme="exact",
        network=ALGORAND_TESTNET_CAIP2,
        asset=str(asset_id),
        amount=args.amount,
        pay_to=app_account,
        max_timeout_seconds=DEFAULT_MAX_TIMEOUT_SECONDS,
        extra={
            "decimals": 6,
            "feePayer": fee_payer_address,
            "tag": "x402-global-challenge",
        },
    )
    payload = PaymentPayload(
        x402_version=2,
        payload={
            "paymentGroup": group["paymentGroup"],
            "paymentIndex": group["paymentIndex"],
        },
        accepted=requirements,
    )

    print("--- POST /verify ---")
    try:
        verify_result = facilitator.verify(payload, requirements)
    except Exception as exc:  # noqa: BLE001 -- print and stop, this is a probe script
        print(f"verify raised: {exc}")
        return
    print(verify_result.model_dump_json(by_alias=True, exclude_none=True, indent=2))
    print()

    if not verify_result.is_valid:
        print("verify rejected the payload; not attempting settle.")
        return

    print("--- POST /settle ---")
    try:
        settle_result = facilitator.settle(payload, requirements)
    except Exception as exc:  # noqa: BLE001
        print(f"settle raised: {exc}")
        return
    print(settle_result.model_dump_json(by_alias=True, exclude_none=True, indent=2))


if __name__ == "__main__":
    main()
