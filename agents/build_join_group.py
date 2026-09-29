"""Builds the non-standard 3-transaction atomic group for the join-path spike.

Extends the standard x402-avm fee-abstracted 2-txn payment group (an unsigned
fee-payer self-payment plus a buyer-signed ASA transfer -- see
`.claude/skills/algorand-x402-python/references/use-python-x402-core-avm-
examples.md`, "Fee-Abstracted Payment Group") with a third, buyer-signed leg:
an application call invoking the escrow stub's `join(agreement_id,
payment_index)` ABI method (selector `f76681b7`, ARC-4 signature
`join(uint64,uint64)void`). All three legs share one `calculate_group_id`.

Two transaction orderings are exposed via `variant`, since it is not yet
known which one the facilitator will accept for a group carrying an extra
application call:

- `fee_payer_first` (default): [feePayer, axfer, appCall]
- `axfer_first`: [axfer, feePayer, appCall] -- the ordering the reference
  2-txn example uses, extended with the appCall last

Whichever is chosen, `paymentIndex` is always the axfer's absolute position
in the resulting group, matching what the contract's `join` call is told to
read via its own `payment_index` argument.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

from algosdk import abi, encoding, transaction
from algosdk.v2client import algod

from agents.common import AlgorandSigner, priced_params
from api.escrow import agreement_box_name, roster_box_name

# method("join(uint64,uint64)void") -- confirmed against the contract's
# compiled ARC-56 spec (contracts/smart_contracts/artifacts/join_spike/
# JoinSpike.arc56.json) and its approval TEAL, which pushes this exact
# selector before matching the `join` branch.
JOIN_METHOD_SELECTOR = bytes.fromhex("f76681b7")

_UINT64 = abi.UintType(64)


@dataclass(frozen=True)
class GroupNotes:
    """The two note fields, supplied rather than generated.

    The reference client stamps `time.time_ns()` into both, so its output
    differs on every call. Harmless in production, fatal for a golden vector,
    so the timestamp moves to `default_notes()` and the vectors pass fixed
    bytes instead.
    """

    fee_payer: bytes
    payment: bytes


def default_notes() -> GroupNotes:
    """The reference client's format, which is what production sends."""
    stamp = time.time_ns()
    return GroupNotes(
        fee_payer=f"x402-fee-payer-{stamp}".encode(),
        payment=f"x402-payment-{stamp}".encode(),
    )


VARIANT_FEE_PAYER_FIRST = "fee_payer_first"
VARIANT_AXFER_FIRST = "axfer_first"

_VARIANT_ROLE_ORDER: dict[str, tuple[str, str, str]] = {
    VARIANT_FEE_PAYER_FIRST: ("fee_payer", "axfer", "app_call"),
    VARIANT_AXFER_FIRST: ("axfer", "fee_payer", "app_call"),
}


def box_name(agreement_id: int) -> bytes:
    """The join *spike* application's payer box: prefix "p_" plus the id.

    Kept for the scripts that read a box off the *spike* deployment --
    `tests/dry_run_join_group.py` and `tests/verify_join_group.py`.

    Note that `build_join_group` no longer emits this prefix for anyone: it
    targets the escrow application's `a`/`r` boxes unconditionally. So a spike
    script that builds a group and then reads `p_` is now internally
    inconsistent, and replaying the spike against its old deployment would
    fail on chain. Those scripts are kept as a record of the TestNet run, not
    as a working path.
    """
    return b"p_" + agreement_id.to_bytes(8, "big")


def decode_leg(raw: bytes) -> tuple[bool, transaction.Transaction]:
    """One leg of a built group, back to `(was it signed, the transaction)`.

    `rawBytes` is deliberately mixed -- signed where the buyer signed, unsigned
    for the fee-payer leg the facilitator cosigns -- so every reader has to
    decode and then unwrap. This module defines that shape, so it owns the one
    function that reverses it. Both facts come back from the single decode
    because the callers that verify a group need both, and deciding signedness
    by decoding a second time is how the two answers drift apart.
    """
    obj = encoding.msgpack_decode(base64.b64encode(raw).decode())
    if isinstance(obj, transaction.SignedTransaction):
        return True, obj.transaction
    return False, obj


def escrow_box_names(agreement_id: int) -> list[tuple[int, bytes]]:
    """The two boxes `join` touches on the escrow application.

    The agreement box (prefix "a") is read for state, deadline, share price
    and seat count. The roster box (prefix "r") is read to refuse a payer who
    already holds a seat, and written to record the new one. Both must appear
    in the call's box array; naming only one fails on the unnamed box.

    The names themselves come from `api.escrow`, which is on this same
    dependency surface, rather than being spelled out a second time here. A
    key scheme copied is a key scheme that can drift, and this copy was the
    only one nothing pinned to another: the TypeScript and contract copies
    exist because those surfaces cannot import Python, which is not true of
    this one.
    """
    return [(0, agreement_box_name(agreement_id)), (0, roster_box_name(agreement_id))]


def build_fee_payer_leg(
    sp: transaction.SuggestedParams,
    fee_payer_address: str,
    pooled_fee: int,
    note: bytes,
) -> transaction.PaymentTxn:
    """Build the unsigned fee-payer self-payment leg (amount 0).

    Shared with `plain_settle_fallback.py`'s 2-txn group, which needs the
    same leg with a different pooled fee (2 legs pooled there, 3 here).
    """
    fee_payer_sp = transaction.SuggestedParams(
        fee=pooled_fee,
        flat_fee=True,  # prevents algosdk from recalculating the fee
        first=sp.first,
        last=sp.last,
        gh=sp.gh,
        gen=sp.gen,
        min_fee=sp.min_fee,
    )
    return transaction.PaymentTxn(
        sender=fee_payer_address,
        sp=fee_payer_sp,
        receiver=fee_payer_address,
        amt=0,
        note=note,
    )


def _zero_fee_sp(sp: transaction.SuggestedParams) -> transaction.SuggestedParams:
    return transaction.SuggestedParams(
        fee=0,
        flat_fee=True,
        first=sp.first,
        last=sp.last,
        gh=sp.gh,
        gen=sp.gen,
        min_fee=sp.min_fee,
    )


def build_join_group(
    *,
    algod_client: algod.AlgodClient,
    buyer_signer: AlgorandSigner,
    fee_payer_address: str,
    app_id: int,
    app_account: str,
    asset_id: int,
    amount: int,
    agreement_id: int,
    variant: str = VARIANT_FEE_PAYER_FIRST,
    notes: GroupNotes | None = None,
) -> dict[str, Any]:
    """Build the signed/unsigned 3-txn join group.

    Args:
        algod_client: TestNet algod client, used only for suggested params.
        buyer_signer: the buyer's signer (see agents/common.py). Signs the
            axfer and the appCall; the fee-payer leg is left unsigned for
            the facilitator to countersign.
        fee_payer_address: address that will sign and pay the pooled fee
            (from the 402 response's `extra.feePayer`, or a facilitator
            sponsor address discovered via `/supported`).
        app_id: the deployed JoinSpike application id.
        app_account: the escrow application's account address (axfer
            receiver, and `payTo` in the x402 payment requirements).
        asset_id: the TestNet USDC ASA id.
        amount: payment amount in ASA base units; must equal the contract's
            `share_price`.
        agreement_id: the box key `join` records the payer under.
        variant: `VARIANT_FEE_PAYER_FIRST` (default) or `VARIANT_AXFER_FIRST`;
            see module docstring.

    Returns:
        A dict mirroring the reference 2-txn builder's return shape,
        extended to 3 legs:
        - "paymentGroup": list[str], base64-encoded msgpack transactions,
          signed where applicable and unsigned for the fee-payer leg -- the
          shape the x402-avm client itself sends as the wire payload.
        - "paymentIndex": int, the axfer's absolute index in the group.
        - "rawBytes": list[bytes], the same transactions as raw msgpack
          bytes (for callers that post directly to a facilitator, which
          take dicts rather than base64 strings).
    """
    if variant not in _VARIANT_ROLE_ORDER:
        raise ValueError(
            f"unknown variant {variant!r}; choose one of {sorted(_VARIANT_ROLE_ORDER)}"
        )
    role_order = _VARIANT_ROLE_ORDER[variant]
    payment_index = role_order.index("axfer")

    # `priced_params` is what refuses params with no usable min_fee, so the
    # three legs below can be priced from the live protocol minimum without a
    # second copy of that rule here.
    sp = priced_params(algod_client)
    notes = notes or default_notes()

    pooled_fee = sp.min_fee * 3
    zero_fee_sp = _zero_fee_sp(sp)

    fee_payer_txn = build_fee_payer_leg(
        sp, fee_payer_address, pooled_fee, notes.fee_payer
    )

    axfer_txn = transaction.AssetTransferTxn(
        sender=buyer_signer.address,
        sp=zero_fee_sp,
        receiver=app_account,
        amt=amount,
        index=asset_id,
        note=notes.payment,
    )

    app_args = [
        JOIN_METHOD_SELECTOR,
        _UINT64.encode(agreement_id),
        _UINT64.encode(payment_index),
    ]
    app_call_txn = transaction.ApplicationCallTxn(
        sender=buyer_signer.address,
        sp=zero_fee_sp,
        index=app_id,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=app_args,
        boxes=escrow_box_names(agreement_id),
    )

    role_to_txn = {
        "fee_payer": fee_payer_txn,
        "axfer": axfer_txn,
        "app_call": app_call_txn,
    }
    transactions = [role_to_txn[role] for role in role_order]

    # Mutates each txn's .group in place and returns them in the same order
    # (address=None means "all of them"), matching the pattern the
    # installed x402-avm client itself uses.
    transactions = transaction.assign_group_id(transactions)

    client_indexes = [
        i for i, txn in enumerate(transactions) if txn.sender == buyer_signer.address
    ]

    unsigned_bytes_list = [
        base64.b64decode(encoding.msgpack_encode(t)) for t in transactions
    ]
    signed_results = buyer_signer.sign_transactions(unsigned_bytes_list, client_indexes)

    raw_bytes: list[bytes] = []
    for i, unsigned in enumerate(unsigned_bytes_list):
        signed = signed_results[i]
        raw_bytes.append(signed if signed is not None else unsigned)

    return {
        "paymentGroup": [base64.b64encode(b).decode("utf-8") for b in raw_bytes],
        "paymentIndex": payment_index,
        # Returned rather than recomputed by the caller: the golden vectors
        # pin the TypeScript builder's signing indexes against this list, and
        # a vector that restated them as a literal could not catch this side
        # moving. Every other field in the vectors comes from the builder.
        "clientIndexes": client_indexes,
        "rawBytes": raw_bytes,
    }


# ============================================================================
# Standalone structural self-check
#
# No live facilitator or deployed contract involved. Uses a throwaway
# ephemeral keypair and a real TestNet algod client (read-only: suggested
# params only, no submission) to confirm the group-id/byte-encoding logic is
# correct: all 3 legs share one group id, the axfer sits at the reported
# paymentIndex, the appCall's ABI args decode back to what was passed in,
# and the fee-payer leg is left unsigned while the buyer's two legs are
# signed by the buyer.
# ============================================================================


def _self_check() -> None:
    from algosdk import account

    from agents.common import get_algod_client

    buyer_sk, buyer_address = account.generate_account()
    _fee_payer_sk, fee_payer_address = account.generate_account()
    _app_sk, app_account = account.generate_account()

    secret_key = base64.b64decode(buyer_sk)
    buyer_signer = AlgorandSigner(secret_key, buyer_address)

    algod_client = get_algod_client()

    app_id = 999999
    agreement_id = 42
    asset_id = 10458941
    amount = 10000

    for variant in (VARIANT_FEE_PAYER_FIRST, VARIANT_AXFER_FIRST):
        print(f"--- variant: {variant} ---")
        group = build_join_group(
            algod_client=algod_client,
            buyer_signer=buyer_signer,
            fee_payer_address=fee_payer_address,
            app_id=app_id,
            app_account=app_account,
            asset_id=asset_id,
            amount=amount,
            agreement_id=agreement_id,
            variant=variant,
        )

        raw = group["rawBytes"]
        payment_index = group["paymentIndex"]
        assert len(raw) == 3, f"expected 3 legs, got {len(raw)}"
        assert len(group["paymentGroup"]) == 3

        decoded = []
        group_ids = set()
        for i, b in enumerate(raw):
            is_signed, txn = decode_leg(b)
            decoded.append((is_signed, txn))
            group_ids.add(txn.group)
            print(
                f"  leg {i}: type={txn.type} sender={txn.sender} "
                f"signed={is_signed} group={base64.b64encode(txn.group).decode() if txn.group else None}"
            )

        assert len(group_ids) == 1 and next(iter(group_ids)) is not None, (
            "legs do not share exactly one non-null group id"
        )

        axfer_is_signed, axfer_txn = decoded[payment_index]
        assert axfer_txn.type == "axfer", (
            f"paymentIndex {payment_index} does not point at the axfer leg "
            f"(found type={axfer_txn.type!r})"
        )
        assert axfer_is_signed, "axfer leg must be signed by the buyer"
        assert axfer_txn.sender == buyer_address

        app_call_is_signed, app_call_txn = next(
            (s, t) for s, t in decoded if t.type == "appl"
        )
        assert app_call_is_signed, "appCall leg must be signed by the buyer"
        assert app_call_txn.sender == buyer_address
        assert app_call_txn.index == app_id
        args = app_call_txn.app_args
        assert args[0] == JOIN_METHOD_SELECTOR, (
            f"expected selector {JOIN_METHOD_SELECTOR.hex()}, got {args[0].hex()}"
        )
        assert _UINT64.decode(args[1]) == agreement_id
        assert _UINT64.decode(args[2]) == payment_index
        packed = agreement_id.to_bytes(8, "big")
        assert app_call_txn.boxes == [
            transaction.BoxReference(0, b"a" + packed),
            transaction.BoxReference(0, b"r" + packed),
        ]

        fee_payer_is_signed, fee_payer_txn = next(
            (s, t) for s, t in decoded if t.type == "pay"
        )
        assert not fee_payer_is_signed, "fee-payer leg must be left unsigned"
        assert fee_payer_txn.sender == fee_payer_address
        assert fee_payer_txn.receiver == fee_payer_address
        assert fee_payer_txn.amt == 0

        print(f"  paymentIndex={payment_index}, all checks passed")

    print("\nOK: build_join_group structural self-check passed for both variants")


if __name__ == "__main__":
    _self_check()
