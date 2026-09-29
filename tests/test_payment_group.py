"""The pre-settlement inspection of the buyer's transaction group.

Everything the contract asserts, it asserts on chain, and a group that fails
there fails atomically with nothing settled. This check exists for the one
shape the chain never sees: a transfer with no `join` call has no application
call left to fail, so it settles cleanly into the application account against
no seat and no refund path, and the USDC is recoverable by nobody.

The others here are not money-loss cases -- the contract would refuse them --
but each fails after the buyer has signed and reads as an opaque contract
error. Named before settlement instead.
"""

from __future__ import annotations

import pytest
from algosdk import encoding, transaction

from api.payment_group import (
    JOIN_SELECTOR,
    MalformedPaymentGroup,
    require_join_call,
)
from tests.conftest import (
    APP_ACCOUNT,
    BUYER,
    OTHER_BUYER,
    TEST_APP_ID,
    build_payment_group,
)

AGREEMENT_ID = 7

REQUIREMENTS = {
    "payTo": APP_ACCOUNT,
    "amount": "5000000",
    "asset": "10458941",
    "extra": {"agreementId": AGREEMENT_ID},
}


def _group(**overrides) -> list[str]:
    return build_payment_group(REQUIREMENTS, **overrides)


def _check(group: list[str], agreement_id: int = AGREEMENT_ID) -> None:
    require_join_call(group, app_id=TEST_APP_ID, agreement_id=agreement_id)


def test_the_correct_group_passes() -> None:
    """The discriminating baseline. Without it every assertion below would
    hold just as well against a check that refused everything."""
    _check(_group())


def test_a_group_with_no_join_is_refused() -> None:
    with pytest.raises(MalformedPaymentGroup, match="no `join` call"):
        _check(_group(joins=0))


def test_an_empty_group_is_refused() -> None:
    """Distinct from a group whose join is missing: nothing to inspect at
    all. Refused rather than passed over, because 'nothing to check' must
    never read as 'nothing wrong'."""
    with pytest.raises(MalformedPaymentGroup, match="no transaction group"):
        _check([])


def test_a_join_for_a_different_agreement_is_refused() -> None:
    with pytest.raises(MalformedPaymentGroup, match="joins agreement 999"):
        _check(_group(join_agreement_id=999))


def test_two_joins_against_one_payment_are_refused() -> None:
    """One payment buys one seat. The contract's own group scan already
    refuses to credit a single transfer twice -- but on chain, after the
    buyer has signed."""
    with pytest.raises(MalformedPaymentGroup, match="2 `join` calls"):
        _check(_group(joins=2))


def test_a_join_sent_by_someone_other_than_the_payer_is_refused() -> None:
    """The seat would be credited to an account that paid nothing, which the
    contract also refuses -- and which would have this server hand back a
    receipt describing somebody else's seat."""
    with pytest.raises(MalformedPaymentGroup, match="different senders"):
        _check(_group(join_sender=OTHER_BUYER))


def test_a_join_on_a_different_application_does_not_count() -> None:
    """Both products are served from one application. An app call to any
    other application is not a join into this escrow, however well formed --
    it would leave the transfer stranded exactly as a missing join does."""
    with pytest.raises(MalformedPaymentGroup, match="no `join` call"):
        _check(_group(app_id=TEST_APP_ID + 1))


def test_an_app_call_with_another_selector_does_not_count() -> None:
    """`close`, `expire` and `release_hash` are all app calls on this same
    application. Only `join` records a seat."""
    sp = transaction.SuggestedParams(
        fee=0,
        first=1,
        last=1001,
        gh="SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
        flat_fee=True,
    )
    not_a_join = transaction.ApplicationCallTxn(
        sender=BUYER,
        sp=sp,
        index=TEST_APP_ID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[b"zzzz", AGREEMENT_ID.to_bytes(8, "big")],
    )
    group = _group(joins=0) + [encoding.msgpack_encode(not_a_join)]

    with pytest.raises(MalformedPaymentGroup, match="no `join` call"):
        _check(group)


def test_an_undecodable_leg_refuses_rather_than_passing() -> None:
    """A group reaches this check only after the facilitator has decoded and
    simulated it, so this should not occur. If it does, the fail-safe
    direction is refusal: skipping an entry can only lose a join, never
    invent one."""
    with pytest.raises(MalformedPaymentGroup):
        _check(["not-msgpack-at-all"])


def test_a_signed_group_is_read_the_same_as_an_unsigned_one() -> None:
    """The shape that actually arrives on the wire.

    Every group built above is unsigned, because nothing offline checks a
    signature. A real buyer's legs arrive signed, and a signed transaction
    decodes to a wrapper rather than to the transaction itself -- so a check
    that only understood the unsigned form would find no join in any real
    payment and refuse all of them. Signed here on purpose, with a throwaway
    key that never holds anything.
    """
    from algosdk import account

    private_key, address = account.generate_account()
    sp = transaction.SuggestedParams(
        fee=0,
        first=1,
        last=1001,
        gh="SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
        flat_fee=True,
    )
    axfer = transaction.AssetTransferTxn(
        sender=address, sp=sp, receiver=APP_ACCOUNT, amt=5_000_000, index=10458941
    )
    appl = transaction.ApplicationCallTxn(
        sender=address,
        sp=sp,
        index=TEST_APP_ID,
        on_complete=transaction.OnComplete.NoOpOC,
        app_args=[
            JOIN_SELECTOR,
            AGREEMENT_ID.to_bytes(8, "big"),
            (1).to_bytes(8, "big"),
        ],
    )
    transaction.assign_group_id([axfer, appl])
    signed = [
        encoding.msgpack_encode(axfer.sign(private_key)),
        encoding.msgpack_encode(appl.sign(private_key)),
    ]

    _check(signed)

    # And the discriminating half: the same signed transfer with its join
    # removed is still refused.
    with pytest.raises(MalformedPaymentGroup, match="no `join` call"):
        _check(signed[:1])


def test_the_selector_matches_the_contract_signature() -> None:
    """Pinned against the value the client builder has hardcoded since the
    TestNet spike. If these ever disagree, every correct group is refused."""
    from agents.build_join_group import JOIN_METHOD_SELECTOR

    assert JOIN_SELECTOR == JOIN_METHOD_SELECTOR
