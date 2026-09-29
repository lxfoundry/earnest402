"""The three details that differ between the join spike and the real escrow
application. Each fails on chain rather than at build time, so each is
asserted against the built transaction rather than against the source."""

from __future__ import annotations

import base64

import pytest
from algosdk import account, transaction

from agents.build_join_group import (
    GroupNotes,
    build_join_group,
    decode_leg,
    escrow_box_names,
)
from agents.common import AlgorandSigner
from agents.dump_golden_vectors import FixedParams

# Fixed for the whole module. Generating these per call would make two
# builds differ in `snd` and `rcv`, so the determinism test below would fail
# on the harness rather than on the thing it is testing.
FEE_PAYER = account.generate_account()[1]
APP_ACCOUNT = account.generate_account()[1]


@pytest.fixture
def signer() -> AlgorandSigner:
    private_key, address = account.generate_account()
    return AlgorandSigner(base64.b64decode(private_key), address)


def _by_type(group: dict) -> dict[str, transaction.Transaction]:
    legs = [decode_leg(raw)[1] for raw in group["rawBytes"]]
    return {txn.type: txn for txn in legs}


def _build(signer: AlgorandSigner, *, min_fee: int = 1000, agreement_id: int = 42):
    return build_join_group(
        algod_client=FixedParams(min_fee),
        buyer_signer=signer,
        fee_payer_address=FEE_PAYER,
        app_id=999_999,
        app_account=APP_ACCOUNT,
        asset_id=10_458_941,
        amount=10_000,
        agreement_id=agreement_id,
        notes=GroupNotes(fee_payer=b"fp-note", payment=b"pay-note"),
    )


def test_box_names_are_the_agreement_and_roster_boxes():
    packed = (42).to_bytes(8, "big")
    assert escrow_box_names(42) == [(0, b"a" + packed), (0, b"r" + packed)]


def test_app_call_references_both_boxes(signer):
    packed = (42).to_bytes(8, "big")
    assert _by_type(_build(signer))["appl"].boxes == [
        transaction.BoxReference(0, b"a" + packed),
        transaction.BoxReference(0, b"r" + packed),
    ]


@pytest.mark.parametrize("min_fee", [1000, 2000, 3500])
def test_pooled_fee_is_three_times_the_live_minimum(signer, min_fee):
    assert _by_type(_build(signer, min_fee=min_fee))["pay"].fee == min_fee * 3


def test_notes_are_taken_from_the_caller(signer):
    by_type = _by_type(_build(signer))
    assert by_type["pay"].note == b"fp-note"
    assert by_type["axfer"].note == b"pay-note"


def test_identical_inputs_produce_identical_bytes(signer):
    """The property the golden vectors rest on. Without caller-supplied notes
    this fails: two calls a nanosecond apart differ."""
    assert _build(signer)["paymentGroup"] == _build(signer)["paymentGroup"]


def test_default_notes_match_the_reference_client_format(signer):
    """Omitting `notes` must still produce the library's shape. The agent
    client relies on the default; only the vectors need determinism."""
    group = build_join_group(
        algod_client=FixedParams(1000),
        buyer_signer=signer,
        fee_payer_address=FEE_PAYER,
        app_id=999_999,
        app_account=APP_ACCOUNT,
        asset_id=10_458_941,
        amount=10_000,
        agreement_id=42,
    )
    by_type = _by_type(group)
    assert by_type["pay"].note.startswith(b"x402-fee-payer-")
    assert by_type["axfer"].note.startswith(b"x402-payment-")


def test_the_axfer_sits_at_the_reported_payment_index(signer):
    group = _build(signer)
    index = group["paymentIndex"]
    assert index == 1
    assert decode_leg(group["rawBytes"][index])[1].type == "axfer"
