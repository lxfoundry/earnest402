"""What a buyer's payment group must contain before it is allowed to settle.

An x402 payment for either product is one atomic group: the facilitator's
fee-payer leg, the buyer's asset transfer into the application account, and
the buyer's own `join` call recording the seat that transfer bought. The
contract enforces everything about that join once it runs -- the amount, the
state, the deadline, one seat per address, and (since the group scan) that
the transfer it credits was sent by the same account. What nothing enforces
is that the join is in the group at all.

A group carrying the transfer and no join is not rejected by anything. It
verifies, it settles, and real USDC lands in the application account against
no seat, no roster entry and no refund path: `close` returns the unspent
ALGO deposit to the creator, not USDC to a payer the contract never
recorded, and `refund_next` walks a roster this buyer is not on. The money
is unrecoverable -- not by the buyer, not by us, not by anyone.

This module is the check for that, and where it runs is the whole point.
PaymentMiddlewareASGI calls the route handler *before* process_settlement(),
and skips settlement entirely for any response >= 400. So a refusal raised
from a handler costs the buyer nothing: their signed transactions are never
submitted and their USDC never leaves their account. A refusal raised one
step later would be a refund problem instead of an error message.

It is deliberately a *presence* check and not a second copy of the
contract's rules. Everything the contract asserts, it asserts on chain, and
a group that fails there fails atomically with nothing settled. The only
case that needs catching here is the one the chain never sees, because
without the join call there is no application call to fail.
"""

from __future__ import annotations

from algosdk import abi, encoding, transaction

from api.escrow import JOIN

# `join(uint64,uint64)void` -> the four selector bytes a group's app call
# carries as its first argument. Derived from the signature the client
# already holds rather than written out as a literal, so a signature change
# cannot leave a stale constant here silently matching nothing -- which
# would refuse every correct group.
JOIN_SELECTOR = abi.Method.from_signature(JOIN).get_selector()

# `join(agreement_id, payment_index)`: the agreement id is the first ABI
# argument, so the second app_arg once the selector is counted.
_AGREEMENT_ID_ARG = 1


class MalformedPaymentGroup(ValueError):
    """The verified payment cannot buy what it was quoted.

    A refusal, never a correction. Every case this names would either strand
    the buyer's USDC or hand back a receipt for a seat that does not exist,
    and neither is repairable after settlement.
    """


def _unwrap(encoded: str):
    """One group entry as a Transaction, or None if it will not decode.

    A group reaches this module only after the facilitator has decoded,
    simulated and verified it, so an undecodable entry should not occur.
    If one does, it is skipped rather than refused: skipping can only ever
    cause this check to *fail to find* a join it was looking for, which
    refuses the payment and leaves the buyer holding their money. Refusing
    outright on a decode failure has the same effect with less information.
    The unsafe direction -- concluding a join is present when it is not --
    is unreachable either way.
    """
    try:
        decoded = encoding.msgpack_decode(encoded)
    except Exception:  # noqa: BLE001 -- algosdk's decoder raises from three
        # different modules for four kinds of malformed input (ValueError,
        # binascii.Error, msgpack's ExtraData, KeyError), and the useful
        # behaviour is identical for all of them.
        return None
    # Signed legs arrive wrapped; the fee-payer leg is unsigned until the
    # facilitator countersigns it, and arrives bare.
    return getattr(decoded, "transaction", decoded)


def require_join_call(
    payment_group: list[str], *, app_id: int, agreement_id: int
) -> None:
    """Refuse unless this group carries exactly one `join` for `agreement_id`.

    Raises `MalformedPaymentGroup`; returns None when the group is sound.
    """
    if not payment_group:
        raise MalformedPaymentGroup(
            "the payment carries no transaction group, so it cannot contain "
            "the join call that records the seat it pays for"
        )

    decoded = [_unwrap(entry) for entry in payment_group]

    joins = [
        txn
        for txn in decoded
        if isinstance(txn, transaction.ApplicationCallTxn)
        and txn.index == app_id
        and txn.app_args
        and bytes(txn.app_args[0]) == JOIN_SELECTOR
    ]

    if not joins:
        raise MalformedPaymentGroup(
            "the payment group carries no `join` call for this escrow "
            "application. The transfer alone would settle into the "
            "application account against no seat and no refund path, and "
            "the funds could not be recovered by anyone -- so it is refused "
            "before settlement rather than accepted and stranded. Build the "
            "group as the route's description specifies: the transfer and "
            "the buyer's own join call, in one atomic group."
        )

    if len(joins) > 1:
        # One payment buys one seat. The contract's own group scan already
        # refuses to credit one transfer twice, so this would fail on chain
        # rather than over-credit -- but it fails *after* the buyer has
        # signed, and the refusal reads as an opaque contract error. Named
        # here instead.
        raise MalformedPaymentGroup(
            f"the payment group carries {len(joins)} `join` calls; one "
            "payment buys exactly one seat"
        )

    join = joins[0]
    if len(join.app_args) <= _AGREEMENT_ID_ARG:
        raise MalformedPaymentGroup("the `join` call names no agreement id")

    joined_id = int.from_bytes(bytes(join.app_args[_AGREEMENT_ID_ARG]), "big")
    if joined_id != agreement_id:
        raise MalformedPaymentGroup(
            f"the `join` call joins agreement {joined_id}, but this payment "
            f"was quoted and verified against agreement {agreement_id}. "
            "Settling it would pay for one thing and record another."
        )

    transfers = [
        txn for txn in decoded if isinstance(txn, transaction.AssetTransferTxn)
    ]
    if transfers and join.sender != transfers[0].sender:
        # The contract's group scan asserts this too, so the group would fail
        # atomically. Named here so the buyer is told which leg disagrees
        # rather than reading it out of a failed simulation -- and so the
        # receipt this handler would otherwise return never describes a seat
        # credited to somebody else.
        raise MalformedPaymentGroup(
            "the `join` call and the transfer have different senders, so the "
            "seat would not be credited to the account that paid for it"
        )
