"""The buyer half of the circuit: quote, build, sign, pay.

Lifted from `agents/join_client_manual.py`, which is the reference that has
actually settled on TestNet. Two details it gets right are easy to get wrong,
and both fail only against a real server:

  - the 402's requirements ride in the `PAYMENT-REQUIRED` *header*. The body
    is `{}`; a client that reads `accepts` out of the body concludes the
    server offered nothing it can pay.
  - `PAYMENT-SIGNATURE` carries a whole `PaymentPayload` -- the inner
    `{paymentGroup, paymentIndex}` wrapped with the accepted requirements and
    the version. Sending the inner object alone fails to decode.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx
from algosdk import transaction
from x402.http import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.mechanisms.avm import SCHEME_EXACT
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements

from agents.build_join_group import (
    VARIANT_FEE_PAYER_FIRST,
    build_join_group,
    decode_leg,
)
from agents.common import AlgorandSigner


@dataclass(frozen=True)
class Quote:
    """What one unpaid pass came back with."""

    payment_required: PaymentRequired
    requirements: PaymentRequirements

    @property
    def agreement_id(self) -> int:
        extra = self.requirements.extra or {}
        agreement_id = extra.get("agreementId")
        assert agreement_id is not None, (
            "the 402 carried no extra.agreementId; `extra` belongs on the "
            "price, not on the PaymentOption, and a string price drops it"
        )
        return int(agreement_id)

    @property
    def fee_payer(self) -> str:
        fee_payer = (self.requirements.extra or {}).get("feePayer")
        assert fee_payer, "the 402 carried no extra.feePayer"
        return str(fee_payer)

    @property
    def amount(self) -> int:
        return int(self.requirements.amount)


@dataclass(frozen=True)
class Paid:
    """The paid pass, plus the identity of the payment the buyer signed.

    The txids are computed from the group this client built, before the
    facilitator ever saw it. That is what makes them worth asserting against:
    the contract's record of who paid and the facilitator's settlement receipt
    both have to name this same axfer, and neither of them is the source of it.
    """

    response: httpx.Response
    axfer_txid: str
    app_call_txid: str
    fee_payer_txid: str
    group_ids: frozenset[str]

    @property
    def status_code(self) -> int:
        return self.response.status_code

    @property
    def text(self) -> str:
        return self.response.text

    def json(self) -> Any:
        return self.response.json()

    @property
    def settlement(self) -> Any:
        """The facilitator's decoded PAYMENT-RESPONSE, when it attached one."""
        return settlement_of(self.response)

    @property
    def identities(self) -> frozenset[str]:
        """Every name this one settlement can legitimately go by.

        A server may report a settlement by any leg's id or by the group's,
        and the encoding of a group id is not settled either. Asserting
        membership says "this is our group" without pinning which convention
        was picked -- and a report naming something we never built is the
        failure worth catching, not the choice between two valid names.
        """
        return (
            frozenset({self.axfer_txid, self.app_call_txid, self.fee_payer_txid})
            | self.group_ids
        )


def pin_url(base_url: str, sha256: str, size: int) -> str:
    """The paid route's URL.

    The query is part of the purchase and must be byte-identical on both
    passes: the paid pass re-runs the price callable, which looks the quote up
    by `(sha256, size)`.
    """
    return f"{base_url}/pin?sha256={sha256}&size={size}"


def quote(http: httpx.Client, url: str, network: str) -> Quote:
    """Do the unpaid pass and return the requirements it advertised."""
    response = http.post(url)
    assert response.status_code == 402, (
        f"expected 402 from {url}, got {response.status_code}: {response.text}"
    )
    header = response.headers.get(PAYMENT_REQUIRED_HEADER)
    assert header, (
        f"the 402 carried no {PAYMENT_REQUIRED_HEADER} header. Under x402 V2 "
        "the requirements are in the header and the body is {}"
    )
    payment_required = decode_payment_required_header(header)
    for requirements in payment_required.accepts:
        if requirements.scheme == SCHEME_EXACT and requirements.network == network:
            return Quote(payment_required, requirements)
    raise AssertionError(
        f"no exact/{network} entry in the 402's accepts: {payment_required.accepts!r}"
    )


def pay(
    http: httpx.Client,
    url: str,
    quoted: Quote,
    *,
    algod: Any,
    buyer_signer: AlgorandSigner,
    app_id: int,
    agreement_id: int | None = None,
    variant: str = VARIANT_FEE_PAYER_FIRST,
) -> Paid:
    """Build the settlement group, sign the buyer's legs, and retry paid.

    `agreement_id` defaults to the one the 402 advertised. It is overridable
    so a test can point the join at a different agreement than the one that
    was quoted, which is the shape of the mis-crediting the contract's group
    scan exists to refuse.
    """
    group = build_join_group(
        algod_client=algod,
        buyer_signer=buyer_signer,
        fee_payer_address=quoted.fee_payer,
        app_id=app_id,
        app_account=quoted.requirements.pay_to,
        asset_id=int(quoted.requirements.asset),
        amount=quoted.amount,
        agreement_id=quoted.agreement_id if agreement_id is None else agreement_id,
        variant=variant,
    )
    payload = PaymentPayload(
        x402_version=quoted.payment_required.x402_version,
        payload={
            "paymentGroup": group["paymentGroup"],
            "paymentIndex": group["paymentIndex"],
        },
        accepted=quoted.requirements,
        resource=quoted.payment_required.resource,
        extensions=quoted.payment_required.extensions,
    )
    # Decoded from the bytes that are about to be sent, not from anything the
    # facilitator says afterwards. `decode_leg` is build_join_group's own
    # reverse, so this reads the group exactly as the submitter will.
    legs = [decode_leg(raw)[1] for raw in group["rawBytes"]]
    payment_index = group["paymentIndex"]
    app_calls = [
        i
        for i, leg in enumerate(legs)
        if isinstance(leg, transaction.ApplicationCallTxn)
    ]
    assert len(app_calls) == 1, (
        f"expected exactly one application call in the group, got {len(app_calls)}"
    )
    rest = [i for i in range(len(legs)) if i not in (payment_index, app_calls[0])]
    assert len(rest) == 1, f"expected one fee-payer leg, got {len(rest)}"
    axfer = legs[payment_index]
    group_id = axfer.group
    group_ids = (
        {
            base64.b64encode(group_id).decode(),
            base64.b32encode(group_id).decode().rstrip("="),
        }
        if group_id
        else set()
    )

    response = http.post(
        url,
        headers={PAYMENT_SIGNATURE_HEADER: encode_payment_signature_header(payload)},
    )
    return Paid(
        response=response,
        axfer_txid=axfer.get_txid(),
        app_call_txid=legs[app_calls[0]].get_txid(),
        fee_payer_txid=legs[rest[0]].get_txid(),
        group_ids=frozenset(group_ids),
    )


def settlement_of(response: httpx.Response) -> Any | None:
    """The decoded PAYMENT-RESPONSE, when the server attached one."""
    header = response.headers.get(PAYMENT_RESPONSE_HEADER)
    return decode_payment_response_header(header) if header else None


def purchase(
    http: httpx.Client,
    base_url: str,
    *,
    sha256: str,
    size: int,
    network: str,
    algod: Any,
    buyer_signer: AlgorandSigner,
    app_id: int,
) -> tuple[Quote, Paid]:
    """Quote and pay in one step, the way every scenario starts."""
    url = pin_url(base_url, sha256, size)
    quoted = quote(http, url, network)
    return quoted, pay(
        http,
        url,
        quoted,
        algod=algod,
        buyer_signer=buyer_signer,
        app_id=app_id,
    )
