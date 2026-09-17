"""LocalNet tests for Escrow.create_agreement: the validity table and the deposit."""

import algokit_utils
import pytest
from algokit_utils import AlgoAmount, AlgorandClient, LogicError, PaymentParams

from smart_contracts.artifacts.escrow.escrow_client import (
    CreateAgreementArgs,
    EscrowClient,
    SetAdminArgs,
    SetPausedArgs,
)
from smart_contracts.escrow.contract import MAX_SEATS_CEILING
from tests.conftest import (
    COMMIT_HASH,
    CONDITION_HASH,
    CONDITION_QUORUM,
    POPULATE,
    SHARE_PRICE,
    STATE_OPEN,
    ZERO_ADDRESS,
    ZERO_HASH,
    chain_now,
    deposit_microalgo,
    freeze_usdc,
)


def test_create_hash_agreement_opens(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    creator: algokit_utils.SigningAccount,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
    future_deadline,
) -> None:
    """A `hash` create lands every field of the record where it belongs.

    Single-seat because that is what this call asks for, not because a `hash`
    agreement must be: see test_hash_accepts_multiple_seats.
    """
    deadline = future_deadline()

    payment = algorand.create_transaction.payment(
        PaymentParams(
            sender=creator.address,
            receiver=app_client.app_address,
            amount=AlgoAmount(micro_algo=deposit_microalgo(1)),
        )
    )

    result = app_client.send.create_agreement(
        args=CreateAgreementArgs(
            condition=CONDITION_HASH,
            share_price=SHARE_PRICE,
            min_seats=1,
            max_seats=1,
            deadline=deadline,
            commit_hash=COMMIT_HASH,
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            mbr_payment=payment,
        ),
        params=algokit_utils.CommonAppCallParams(sender=creator.address),
        send_params=POPULATE,
    )

    agreement_id = result.abi_return
    assert agreement_id is not None and agreement_id >= 1

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_OPEN
    assert record.condition == CONDITION_HASH
    assert record.min_seats == 1
    assert record.max_seats == 1
    assert record.seats == 0
    assert record.refund_cursor == 0
    assert record.unclaimed_seats == 0
    assert record.total_held == 0
    assert record.deadline == deadline
    assert record.share_price == SHARE_PRICE
    assert record.beneficiary == beneficiary.address
    assert record.verifier == verifier.address
    assert record.creator == creator.address


def test_create_quorum_pool_opens(
    app_client: EscrowClient,
    create_agreement,
    beneficiary: algokit_utils.SigningAccount,
) -> None:
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=5,
        max_seats=5,
    ).abi_return

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_OPEN
    assert record.condition == CONDITION_QUORUM
    assert record.min_seats == 5
    assert record.max_seats == 5
    assert record.verifier == ZERO_ADDRESS
    assert bytes(record.commit_hash) == ZERO_HASH


def test_agreement_ids_are_consecutive(
    create_agreement,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    first = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    ).abi_return
    second = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    ).abi_return
    assert second == first + 1


# --- the validity table, one rejected row per test -------------------------


def test_hash_accepts_multiple_seats(
    app_client: EscrowClient, create_agreement, beneficiary, verifier
) -> None:
    """A multi-seat `hash` agreement is the pool that holds until delivery.

    The assert this replaces declared `hash` agreements to be single-seat. It
    was a narrowing at the door of machinery that is seat-general: the
    deposit arithmetic, the roster, `join` and the refund cursor were all
    written against `max_seats` from the start.
    """
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        min_seats=5,
        max_seats=5,
    ).abi_return
    assert agreement_id is not None

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.condition == CONDITION_HASH
    assert record.state == STATE_OPEN
    assert record.min_seats == 5
    assert record.max_seats == 5
    assert record.seats == 0
    assert record.total_held == 0
    # The deliverable is committed before any buyer pays -- that is the whole
    # point of this mode, and it is why a hash cannot be bound later.
    assert bytes(record.commit_hash) == COMMIT_HASH
    assert record.verifier == verifier.address


def test_hash_rejects_slack_seats(create_agreement, beneficiary, verifier) -> None:
    """A pool's seat count is fixed, on either condition.

    A payer past the quota would own `1/seats` of something they priced at
    `1/min_seats`, so the price a buyer agreed to would depend on how many
    people joined after them.
    """
    with pytest.raises(LogicError, match="max_seats must equal min_seats"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            min_seats=3,
            max_seats=5,
        )


def test_hash_rejects_zero_seats(create_agreement, beneficiary, verifier) -> None:
    """Widening the branch had to keep a lower bound, because nothing else has one.

    `min_seats == 1` used to be what excluded zero here, and `quorum`'s own
    `min_seats > 1` floor is in the other branch. A zero-seat agreement is
    inert -- `join` asserts `seats < max_seats` -- but it would still consume
    an id and lock box minimum balance for a row no payer can reach.
    """
    with pytest.raises(LogicError, match="min_seats must be at least 1"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            min_seats=0,
            max_seats=0,
        )


def test_hash_rejects_seats_above_the_ceiling(
    create_agreement, beneficiary, verifier
) -> None:
    """The ceiling is a measured opcode-budget limit, not a policy number."""
    over = MAX_SEATS_CEILING + 1
    with pytest.raises(LogicError, match="max_seats exceeds the ceiling"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            min_seats=over,
            max_seats=over,
        )


def test_hash_rejects_zero_commit_hash(create_agreement, beneficiary, verifier) -> None:
    with pytest.raises(LogicError, match="hash requires a commit_hash"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            commit_hash=ZERO_HASH,
        )


def test_hash_rejects_zero_verifier(create_agreement, beneficiary) -> None:
    with pytest.raises(LogicError, match="hash requires a verifier"):
        create_agreement(beneficiary=beneficiary.address, verifier=ZERO_ADDRESS)


def test_quorum_rejects_single_seat(create_agreement, beneficiary) -> None:
    with pytest.raises(LogicError, match="quorum requires min_seats > 1"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=1,
            max_seats=4,
        )


def test_quorum_rejects_commit_hash(create_agreement, beneficiary) -> None:
    """Also the guard that the `hash` branch's seat rules stayed in their branch.

    A permissionless release carrying a committed hash would be a fourth mode
    nobody designed, and it is what a widening that leaked across the
    condition split would produce.
    """
    with pytest.raises(LogicError, match="quorum must not carry a commit_hash"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=2,
            max_seats=2,
            commit_hash=COMMIT_HASH,
        )


def test_quorum_rejects_verifier(create_agreement, beneficiary, verifier) -> None:
    """The other half of the mode-2 guard: `quorum` never gains a named caller."""
    with pytest.raises(LogicError, match="quorum release is permissionless"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=2,
            max_seats=2,
            verifier=verifier.address,
        )


def test_quorum_rejects_max_seats_above_the_ceiling(
    create_agreement, beneficiary
) -> None:
    # One past the published ceiling, not an arbitrary large number: this is
    # the boundary, and reading the constant keeps it the boundary if the
    # ceiling moves again.
    over = MAX_SEATS_CEILING + 1
    with pytest.raises(LogicError, match="max_seats exceeds the ceiling"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=over,
            max_seats=over,
        )


def test_quorum_rejects_max_seats_below_min_seats(
    create_agreement, beneficiary
) -> None:
    with pytest.raises(LogicError, match="max_seats must equal min_seats"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=5,
            max_seats=4,
        )


def test_rejects_unknown_condition(create_agreement, beneficiary) -> None:
    with pytest.raises(LogicError, match="unknown condition"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=7,
            min_seats=2,
            max_seats=4,
        )


def test_rejects_past_deadline(
    algorand: AlgorandClient, create_agreement, beneficiary, verifier
) -> None:
    with pytest.raises(LogicError, match="deadline must be in the future"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            deadline=chain_now(algorand) - 100,
        )


def test_rejects_zero_share_price(create_agreement, beneficiary, verifier) -> None:
    with pytest.raises(LogicError, match="share_price must be positive"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            share_price=0,
        )


def test_rejects_zero_beneficiary(create_agreement, verifier) -> None:
    with pytest.raises(LogicError, match="beneficiary must be set"):
        create_agreement(beneficiary=ZERO_ADDRESS, verifier=verifier.address)


def test_rejects_beneficiary_not_opted_in(
    create_agreement, verifier, stranger: algokit_utils.SigningAccount
) -> None:
    with pytest.raises(LogicError, match="beneficiary must be able to receive USDC"):
        create_agreement(beneficiary=stranger.address, verifier=verifier.address)


def test_rejects_a_frozen_beneficiary(
    algorand: AlgorandClient,
    create_agreement,
    verifier,
    opted_in_account,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
) -> None:
    """Opted in but frozen is still unable to receive.

    Creation checks the beneficiary can be paid so an agreement is never born
    unreleasable. An opt-in check alone would pass a frozen holding and the
    release would then fail on the inner transfer, with the pot depending on
    the stranded-beneficiary rescue to get out.
    """
    frozen_beneficiary = opted_in_account()
    freeze_usdc(algorand, frozen_beneficiary, usdc_asset_id, usdc_creator)

    with pytest.raises(LogicError, match="beneficiary must be able to receive USDC"):
        create_agreement(
            beneficiary=frozen_beneficiary.address, verifier=verifier.address
        )


def test_quorum_rejects_max_seats_above_min_seats(
    create_agreement, beneficiary
) -> None:
    """A pool's seat count is fixed: `max_seats` must equal `min_seats`.

    A pool is a fixed deliverable at a fixed price -- the supplier is owed
    `share_price * min_seats` -- so there is nothing for a seat past the quota
    to buy. Slack above `min_seats` is unreachable anyway, because reaching
    `min_seats` moves the agreement out of OPEN and `join` only accepts OPEN;
    it would cost roster minimum balance for storage no payer can ever use.
    """
    with pytest.raises(LogicError, match="max_seats must equal min_seats"):
        create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=3,
            max_seats=5,
        )


def test_rejects_short_commit_hash(create_agreement, beneficiary, verifier) -> None:
    with pytest.raises(LogicError, match="commit_hash must be 32 bytes"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            commit_hash=COMMIT_HASH[:31],
        )


def test_rejects_deposit_one_microalgo_short(
    create_agreement, beneficiary, verifier
) -> None:
    with pytest.raises(
        LogicError, match="deposit must equal box MBR plus the fee reserve"
    ):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            deposit=deposit_microalgo(1) - 1,
        )


def test_rejects_deposit_one_microalgo_over(
    create_agreement, beneficiary, verifier
) -> None:
    """An overpayment is rejected, not banked.

    `close` returns a computed figure -- box minimum balance plus the unspent
    reserve -- so anything above the required deposit would have no way back
    out: nothing sweeps the application account and the application has no
    delete handler.
    """
    with pytest.raises(
        LogicError, match="deposit must equal box MBR plus the fee reserve"
    ):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            deposit=deposit_microalgo(1) + 1,
        )


def test_rejects_non_admin_sender(
    create_agreement, beneficiary, verifier, stranger: algokit_utils.SigningAccount
) -> None:
    with pytest.raises(LogicError, match="sender must be admin"):
        create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            sender=stranger.address,
        )


# --- operations -----------------------------------------------------------


def test_set_admin_rejects_a_non_admin(
    app_client: EscrowClient, stranger: algokit_utils.SigningAccount
) -> None:
    with pytest.raises(LogicError, match="sender must be admin"):
        app_client.send.set_admin(
            args=SetAdminArgs(new_admin=stranger.address),
            params=algokit_utils.CommonAppCallParams(sender=stranger.address),
        )


def test_set_admin_rejects_the_zero_address(
    app_client: EscrowClient, creator: algokit_utils.SigningAccount
) -> None:
    with pytest.raises(LogicError, match="admin must be set"):
        app_client.send.set_admin(
            args=SetAdminArgs(new_admin=ZERO_ADDRESS),
            params=algokit_utils.CommonAppCallParams(sender=creator.address),
        )


def test_set_admin_moves_the_power_to_create(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    create_agreement,
    creator: algokit_utils.SigningAccount,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    new_admin = algorand.account.random()
    algorand.account.ensure_funded(
        new_admin.address, algorand.account.localnet_dispenser(), AlgoAmount(algo=10)
    )

    app_client.send.set_admin(
        args=SetAdminArgs(new_admin=new_admin.address),
        params=algokit_utils.CommonAppCallParams(sender=creator.address),
    )
    try:
        with pytest.raises(LogicError, match="sender must be admin"):
            create_agreement(
                beneficiary=beneficiary.address,
                verifier=verifier.address,
                sender=creator.address,
            )

        agreement_id = create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            sender=new_admin.address,
        ).abi_return
        record = app_client.state.box.agreements.get_value(agreement_id)
        assert record.creator == new_admin.address
    finally:
        app_client.send.set_admin(
            args=SetAdminArgs(new_admin=creator.address),
            params=algokit_utils.CommonAppCallParams(sender=new_admin.address),
        )


def test_set_paused_rejects_a_non_admin(
    app_client: EscrowClient, stranger: algokit_utils.SigningAccount
) -> None:
    with pytest.raises(LogicError, match="sender must be admin"):
        app_client.send.set_paused(
            args=SetPausedArgs(paused=1),
            params=algokit_utils.CommonAppCallParams(sender=stranger.address),
        )


def test_pause_stops_new_agreements_and_new_joins(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    hash_agreement: int,
    buyer,
    creator: algokit_utils.SigningAccount,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    _set_paused(app_client, creator.address, 1)
    try:
        with pytest.raises(LogicError, match="contract is paused"):
            create_agreement(beneficiary=beneficiary.address, verifier=verifier.address)
        with pytest.raises(LogicError, match="contract is paused"):
            join_agreement(hash_agreement, buyer)
    finally:
        _set_paused(app_client, creator.address, 0)

    # And the same join works again the moment the brake comes off.
    join_agreement(hash_agreement, buyer)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 1


def _set_paused(app_client: EscrowClient, sender: str, paused: int) -> None:
    app_client.send.set_paused(
        args=SetPausedArgs(paused=paused),
        params=algokit_utils.CommonAppCallParams(sender=sender),
    )
