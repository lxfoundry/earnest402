"""LocalNet tests for the end of an agreement's life: expiry, the paged refund
pass, the claim path for seats it had to skip, and close.

The tests that matter most here are the ones about a payer who can no longer
receive USDC: without the skip, one dead wallet freezes every seat behind it.
"""

import algokit_utils
import pytest
from algokit_utils import (
    AlgorandClient,
    AssetOptInParams,
    AssetOptOutParams,
    CommonAppCallParams,
    LogicError,
)

from smart_contracts.artifacts.escrow.escrow_client import EscrowClient
from tests.conftest import (
    POPULATE,
    SHARE_PRICE,
    STATE_EXPIRED,
    STATE_FILLED,
    STATE_FUNDED,
    STATE_OPEN,
    STATE_REFUNDED,
    STATE_REFUNDING,
    agreement_box_name,
    algo_balance,
    box_exists,
    box_mbr,
    call_claim_refund,
    call_close,
    call_expire,
    call_refund_next,
    call_release_hash,
    call_release_quorum,
    chain_now,
    deposit_microalgo,
    events_named,
    fee_reserve,
    freeze_usdc,
    one_event,
    opt_out_of_usdc,
    roster_box_name,
    usdc_balance,
)

REASON_DEADLINE = 0
REASON_BENEFICIARY_STRANDED = 1


_expire = call_expire


_refund_next = call_refund_next


@pytest.fixture
def short_deadline_agreement(
    algorand: AlgorandClient, create_agreement, beneficiary, verifier
) -> int:
    """An OPEN `hash` agreement whose deadline is a minute away."""
    return create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=chain_now(algorand) + 60,
    ).abi_return


def test_expire_after_deadline_is_permissionless(
    app_client: EscrowClient,
    short_deadline_agreement: int,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    advance_chain_time(300)

    result = _expire(app_client, short_deadline_agreement, stranger.address)

    record = app_client.state.box.agreements.get_value(short_deadline_agreement)
    assert record.state == STATE_EXPIRED

    event = one_event(result, "Expired")
    assert event["agreement_id"] == short_deadline_agreement
    assert event["reason"] == REASON_DEADLINE
    assert event["seats"] == 0


def test_creator_expires_an_unfunded_agreement_before_the_deadline(
    app_client: EscrowClient,
    hash_agreement: int,
    creator: algokit_utils.SigningAccount,
) -> None:
    """An agreement nobody has paid into is the creator's to reclaim.

    `seats == 0` means there is no payer to protect, so the only party
    affected is the creator getting their own deposit back. This is what
    lets the funding window be backend policy rather than a second clock
    on the record.
    """
    result = _expire(app_client, hash_agreement, creator.address)

    assert app_client.state.box.agreements.get_value(hash_agreement).state == (
        STATE_EXPIRED
    )
    event = one_event(result, "Expired")
    assert event["seats"] == 0
    assert event["reason"] == REASON_DEADLINE


def test_creator_cannot_expire_early_once_a_payer_has_joined(
    app_client: EscrowClient,
    funded_agreement: int,
    creator: algokit_utils.SigningAccount,
) -> None:
    """One seat taken is enough to put the clock back in charge.

    The early path exists only because an empty roster has nobody to harm.
    """
    with pytest.raises(LogicError, match="deadline has not passed"):
        _expire(app_client, funded_agreement, creator.address)

    assert app_client.state.box.agreements.get_value(funded_agreement).state == (
        STATE_FUNDED
    )


def test_early_expiry_returns_the_whole_deposit_to_the_creator(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    beneficiary,
    verifier,
    creator: algokit_utils.SigningAccount,
) -> None:
    """The reclaim path end to end, which is the point of the early exit.

    An abandoned quote has to give the deposit back or the capacity to
    quote at all drains away.
    """
    agreement_id = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    ).abi_return
    before = algo_balance(algorand, creator.address)

    _expire(app_client, agreement_id, creator.address)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 1)
    assert app_client.state.box.agreements.get_value(agreement_id).state == (
        STATE_REFUNDED
    )
    call_close(app_client, agreement_id, creator.address)

    reclaimed = algo_balance(algorand, creator.address) - before
    # The deposit back, less the fees the creator paid to reclaim it.
    assert reclaimed > deposit_microalgo(1) - 10_000
    assert not box_exists(algorand, app_client, agreement_box_name(agreement_id))


def test_expire_before_the_deadline_is_rejected_when_open(
    app_client: EscrowClient,
    hash_agreement: int,
    stranger: algokit_utils.SigningAccount,
) -> None:
    with pytest.raises(LogicError, match="deadline has not passed"):
        _expire(app_client, hash_agreement, stranger.address)
    assert app_client.state.box.agreements.get_value(hash_agreement).state == STATE_OPEN


def test_expire_before_the_deadline_is_rejected_when_funded(
    app_client: EscrowClient,
    funded_agreement: int,
    stranger: algokit_utils.SigningAccount,
) -> None:
    with pytest.raises(LogicError, match="deadline has not passed"):
        _expire(app_client, funded_agreement, stranger.address)
    record = app_client.state.box.agreements.get_value(funded_agreement)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE


def test_funded_agreement_expires_after_the_deadline(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    create_agreement,
    join_agreement,
    buyer,
    beneficiary,
    verifier,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=chain_now(algorand) + 60,
    ).abi_return
    join_agreement(agreement_id, buyer)
    advance_chain_time(300)

    result = _expire(app_client, agreement_id, stranger.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_EXPIRED
    # The money is still held: expiry marks, refunding pays.
    assert record.total_held == SHARE_PRICE

    event = one_event(result, "Expired")
    assert event["seats"] == 1
    assert event["reason"] == REASON_DEADLINE


def test_filled_pool_does_not_expire_on_the_clock(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    make_pool,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """A reachable beneficiary means `release_quorum` applies, not `expire`."""
    agreement_id, _payers = make_pool(min_seats=3, deadline=chain_now(algorand) + 60)
    advance_chain_time(300)

    with pytest.raises(
        LogicError, match="beneficiary can still receive; release_quorum applies"
    ):
        _expire(app_client, agreement_id, stranger.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_FILLED
    assert record.total_held == SHARE_PRICE * 3


def test_stranded_beneficiary_rescue(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_pool,
    beneficiary: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = make_pool(min_seats=3, deadline=chain_now(algorand) + 60)
    advance_chain_time(300)

    # The beneficiary walks away from the asset after the pool filled.
    algorand.send.asset_opt_out(
        AssetOptOutParams(
            sender=beneficiary.address,
            asset_id=usdc_asset_id,
            creator=usdc_creator.address,
        )
    )

    result = _expire(app_client, agreement_id, stranger.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_EXPIRED
    assert record.total_held == SHARE_PRICE * 3

    event = one_event(result, "Expired")
    assert event["reason"] == REASON_BENEFICIARY_STRANDED
    assert event["seats"] == 3


def test_frozen_beneficiary_lets_a_filled_pool_expire(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_pool,
    beneficiary: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """Opting out is not the only way a beneficiary stops being able to receive.

    A frozen holding is still an opted-in holding, so a rescue guarded on
    opt-in alone leaves `FILLED` with no exit at all: `release_quorum` fails on
    the inner transfer and `expire` refuses because the beneficiary "can still
    receive". The pot would be locked permanently. MainNet USDC carries a
    freeze address, so this is reachable in production.
    """
    agreement_id, _payers = make_pool(min_seats=3, deadline=chain_now(algorand) + 60)
    advance_chain_time(300)

    freeze_usdc(algorand, beneficiary, usdc_asset_id, usdc_creator)

    # The exit that would otherwise be the only one, and is not available.
    with pytest.raises(LogicError):
        call_release_quorum(app_client, agreement_id)

    result = _expire(app_client, agreement_id, stranger.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_EXPIRED
    assert record.total_held == SHARE_PRICE * 3

    event = one_event(result, "Expired")
    assert event["reason"] == REASON_BENEFICIARY_STRANDED
    assert event["seats"] == 3


def test_release_quorum_fails_once_the_pool_is_stranded(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_pool,
    beneficiary: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """The two exits from FILLED are mutually exclusive, never both open."""
    from smart_contracts.artifacts.escrow.escrow_client import ReleaseQuorumArgs

    agreement_id, _payers = make_pool(min_seats=3)
    algorand.send.asset_opt_out(
        AssetOptOutParams(
            sender=beneficiary.address,
            asset_id=usdc_asset_id,
            creator=usdc_creator.address,
        )
    )

    with pytest.raises(LogicError):
        app_client.send.release_quorum(
            args=ReleaseQuorumArgs(agreement_id=agreement_id),
            params=CommonAppCallParams(sender=stranger.address),
            send_params=POPULATE,
        )

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_FILLED
    assert record.total_held == SHARE_PRICE * 3


def test_expire_rejects_a_released_agreement(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    create_agreement,
    join_agreement,
    buyer,
    beneficiary,
    verifier,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    from smart_contracts.artifacts.escrow.escrow_client import ReleaseHashArgs
    from tests.conftest import COMMIT_HASH, STATE_RELEASED

    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=chain_now(algorand) + 60,
    ).abi_return
    join_agreement(agreement_id, buyer)
    app_client.send.release_hash(
        args=ReleaseHashArgs(
            agreement_id=agreement_id, delivered_bytes_hash=COMMIT_HASH
        ),
        params=CommonAppCallParams(sender=verifier.address),
        send_params=POPULATE,
    )
    advance_chain_time(300)

    with pytest.raises(LogicError, match="agreement cannot expire from this state"):
        _expire(app_client, agreement_id, stranger.address)

    assert (
        app_client.state.box.agreements.get_value(agreement_id).state == STATE_RELEASED
    )


def test_expire_cannot_run_twice(
    app_client: EscrowClient,
    short_deadline_agreement: int,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    advance_chain_time(300)
    _expire(app_client, short_deadline_agreement, stranger.address)

    with pytest.raises(LogicError, match="agreement cannot expire from this state"):
        _expire(app_client, short_deadline_agreement, stranger.address)


# --- refund_next ----------------------------------------------------------


def test_dead_wallet_does_not_freeze_the_seats_behind_it(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
) -> None:
    """The regression this method exists for.

    Seat 1 can no longer receive USDC. Seats 0 and 2 must still be paid: an
    inner-transaction failure aborts the whole group, so without the skip the
    cursor sticks at seat 1 and everyone behind it is frozen out permanently.
    """
    agreement_id, payers = expired_pool_of_three
    opt_out_of_usdc(algorand, payers[1], usdc_asset_id, usdc_creator)

    before = [usdc_balance(algorand, payer.address, usdc_asset_id) for payer in payers]

    result = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == 3
    assert record.unclaimed_seats == 1

    # The regression: seats behind the dead wallet were paid.
    for index in (0, 2):
        after = usdc_balance(algorand, payers[index].address, usdc_asset_id)
        assert after - before[index] == SHARE_PRICE

    # The skipped seat still owes, and total_held still reflects it.
    assert record.total_held == SHARE_PRICE

    refunded = events_named(result, "Refunded")
    skipped = events_named(result, "RefundSkipped")
    assert [event["seat"] for event in refunded] == [0, 2]
    assert [event["seat"] for event in skipped] == [1]
    assert skipped[0]["payer"] == payers[1].address
    assert skipped[0]["amount"] == SHARE_PRICE

    complete = one_event(result, "RefundComplete")
    assert complete["paid"] == 2
    assert complete["skipped"] == 1


def test_frozen_payer_is_skipped_rather_than_freezing_the_cursor(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
) -> None:
    """The dead-wallet regression again, with the other kind of dead wallet.

    Seat 1 is opted in but frozen. An opt-in check passes and the inner
    transfer then fails, aborting the group -- so the cursor sticks at seat 1
    and every seat behind it is frozen out, which is exactly the failure the
    skip exists to prevent.
    """
    agreement_id, payers = expired_pool_of_three
    freeze_usdc(algorand, payers[1], usdc_asset_id, usdc_creator)

    before = [usdc_balance(algorand, payer.address, usdc_asset_id) for payer in payers]

    result = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == 3
    assert record.unclaimed_seats == 1
    assert record.total_held == SHARE_PRICE

    for index in (0, 2):
        after = usdc_balance(algorand, payers[index].address, usdc_asset_id)
        assert after - before[index] == SHARE_PRICE

    skipped = events_named(result, "RefundSkipped")
    assert [event["seat"] for event in skipped] == [1]

    # Thawed, the skipped payer recovers through the claim path.
    freeze_usdc(algorand, payers[1], usdc_asset_id, usdc_creator, frozen=False)
    _claim_refund(app_client, agreement_id, 1)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.unclaimed_seats == 0 and record.total_held == 0
    assert usdc_balance(algorand, payers[1].address, usdc_asset_id) - before[1] == (
        SHARE_PRICE
    )


def test_refund_next_pays_every_seat_and_clears_the_balance(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, payers = expired_pool_of_three
    before = [usdc_balance(algorand, payer.address, usdc_asset_id) for payer in payers]

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 3)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == 3
    assert record.unclaimed_seats == 0
    assert record.total_held == 0
    for index, payer in enumerate(payers):
        after = usdc_balance(algorand, payer.address, usdc_asset_id)
        assert after - before[index] == SHARE_PRICE


def test_refund_caller_pays_no_inner_transaction_fee(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """A stranger paging refunds spends one transaction fee, nothing more."""
    agreement_id, _payers = expired_pool_of_three
    app_before = algo_balance(algorand, app_client.app_address)
    caller_before = algo_balance(algorand, stranger.address)

    _refund_next(
        algorand, app_client, usdc_asset_id, agreement_id, 3, sender=stranger.address
    )

    assert algo_balance(algorand, stranger.address) == caller_before - 1_000
    # Three inner transfers, all charged to the reserve funded at creation.
    assert algo_balance(algorand, app_client.app_address) == app_before - 3_000


def test_eight_seat_pool_clears_through_two_calls(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_pool,
) -> None:
    agreement_id, payers = make_expired_pool(joins=8)
    before = [usdc_balance(algorand, payer.address, usdc_asset_id) for payer in payers]

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDING
    assert record.refund_cursor == 4
    assert record.total_held == SHARE_PRICE * 4

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == record.seats == 8
    assert record.total_held == 0
    assert record.unclaimed_seats == 0

    for index, payer in enumerate(payers):
        after = usdc_balance(algorand, payer.address, usdc_asset_id)
        assert after - before[index] == SHARE_PRICE, f"seat {index} unpaid"


def test_refund_complete_counts_the_whole_sweep_not_the_final_batch(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_pool,
) -> None:
    """Six seats, batches of four then two: the receipt must read 6, not 2.

    `RefundComplete` is the terminal receipt for the agreement, so counting
    only the call that finished the sweep undercounts every batch before it.
    """
    agreement_id, _payers = make_expired_pool(joins=6)

    first = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    assert events_named(first, "RefundComplete") == []
    assert len(events_named(first, "Refunded")) == 4

    second = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    assert len(events_named(second, "Refunded")) == 2

    complete = one_event(second, "RefundComplete")
    assert complete["paid"] == 6
    assert complete["skipped"] == 0


def test_refund_complete_counts_a_claim_landing_between_batches_as_paid(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_expired_pool,
) -> None:
    """A seat skipped in batch one and claimed before batch two counts once.

    This is the case a counter accumulated inside `refund_next` cannot get
    right: the skip happens in one call and the settlement in neither.
    """
    agreement_id, payers = make_expired_pool(joins=6)
    opt_out_of_usdc(algorand, payers[0], usdc_asset_id, usdc_creator)

    first = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    assert [event["seat"] for event in events_named(first, "RefundSkipped")] == [0]
    assert app_client.state.box.agreements.get_value(agreement_id).unclaimed_seats == 1

    algorand.send.asset_opt_in(
        AssetOptInParams(
            sender=payers[0].address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    _claim_refund(app_client, agreement_id, 0)

    second = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    complete = one_event(second, "RefundComplete")
    assert complete["paid"] == 6
    assert complete["skipped"] == 0

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.total_held == 0


def test_refund_complete_reports_a_seat_still_owed_as_skipped(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_expired_pool,
) -> None:
    """The skipped half of `paid + skipped == seats`, across two batches."""
    agreement_id, payers = make_expired_pool(joins=6)
    opt_out_of_usdc(algorand, payers[0], usdc_asset_id, usdc_creator)

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    second = _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    complete = one_event(second, "RefundComplete")
    assert complete["paid"] == 5
    assert complete["skipped"] == 1
    assert complete["paid"] + complete["skipped"] == 6


def test_refund_next_rejects_a_count_above_the_ceiling(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, _payers = expired_pool_of_three
    with pytest.raises(LogicError, match="count exceeds the per-call ceiling"):
        _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 5)
    assert (
        app_client.state.box.agreements.get_value(agreement_id).state == STATE_EXPIRED
    )


def test_refund_next_rejects_a_zero_count(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, _payers = expired_pool_of_three
    with pytest.raises(LogicError, match="count must be positive"):
        _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 0)


def test_refund_next_rejects_an_open_agreement(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    hash_agreement: int,
) -> None:
    with pytest.raises(LogicError, match="agreement is not refunding"):
        _refund_next(algorand, app_client, usdc_asset_id, hash_agreement, 1)
    assert app_client.state.box.agreements.get_value(hash_agreement).state == STATE_OPEN


def test_expired_agreement_with_no_joins_refunds_in_one_call(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    short_deadline_agreement: int,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """No special case in the code: the cursor starts at the end already."""
    advance_chain_time(300)
    _expire(app_client, short_deadline_agreement, stranger.address)

    result = _refund_next(
        algorand, app_client, usdc_asset_id, short_deadline_agreement, 4
    )

    record = app_client.state.box.agreements.get_value(short_deadline_agreement)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == 0
    assert record.total_held == 0
    assert events_named(result, "Refunded") == []
    complete = one_event(result, "RefundComplete")
    assert complete["paid"] == 0 and complete["skipped"] == 0


def test_refund_next_cannot_run_again_once_complete(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, _payers = expired_pool_of_three
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    with pytest.raises(LogicError, match="agreement is not refunding"):
        _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)


# --- claim_refund ---------------------------------------------------------


_claim_refund = call_claim_refund


@pytest.fixture
def pool_with_one_skipped_seat(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
):
    """A fully paged pool where seat 1 could not be paid and still owes."""
    agreement_id, payers = expired_pool_of_three
    opt_out_of_usdc(algorand, payers[1], usdc_asset_id, usdc_creator)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED and record.unclaimed_seats == 1
    return agreement_id, payers[1], 1


def test_claim_refund_pays_a_previously_skipped_seat(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    pool_with_one_skipped_seat,
) -> None:
    agreement_id, skipped_payer, seat = pool_with_one_skipped_seat

    algorand.send.asset_opt_in(
        # The note distinguishes this from the payer's original opt-in, which
        # would otherwise be the same transaction within one validity window.
        AssetOptInParams(
            sender=skipped_payer.address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    result = _claim_refund(app_client, agreement_id, seat)

    assert usdc_balance(algorand, skipped_payer.address, usdc_asset_id) == SHARE_PRICE

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.unclaimed_seats == 0
    assert record.total_held == 0
    # REFUNDED means the cursor finished its pass, not that everyone was paid.
    assert record.state == STATE_REFUNDED

    event = one_event(result, "RefundClaimed")
    assert event["seat"] == seat
    assert event["amount"] == SHARE_PRICE
    assert event["payer"] == skipped_payer.address


def test_claim_refund_rejects_an_already_settled_seat(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, _payers = expired_pool_of_three
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    with pytest.raises(LogicError, match="seat is already settled"):
        _claim_refund(app_client, agreement_id, 0)


def test_claim_refund_rejects_a_seat_out_of_range(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    agreement_id, payers = expired_pool_of_three

    with pytest.raises(LogicError, match="seat is out of range"):
        _claim_refund(app_client, agreement_id, len(payers))


def test_claim_refund_fails_while_the_payer_is_still_opted_out(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    pool_with_one_skipped_seat,
) -> None:
    """The inner transfer aborts the call, so nothing about the record moves."""
    agreement_id, _skipped_payer, seat = pool_with_one_skipped_seat

    with pytest.raises(LogicError):
        _claim_refund(app_client, agreement_id, seat)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.unclaimed_seats == 1
    assert record.total_held == SHARE_PRICE


def test_claim_refund_rejects_an_open_agreement(
    app_client: EscrowClient, funded_agreement: int
) -> None:
    with pytest.raises(LogicError, match="agreement is not in a refunding state"):
        _claim_refund(app_client, funded_agreement, 0)


def test_claim_refund_rejects_a_seat_the_cursor_has_not_reached(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
) -> None:
    """A seat ahead of the cursor is `refund_next`'s business, not a claim's.

    `claim_refund` exists for seats the refund pass had to skip, and a skipped
    seat is exactly one behind the cursor with a non-zero amount. Accepting a
    seat ahead of it both jumps the FIFO queue the refund order promises and
    decrements `unclaimed_seats` for a seat that was never skipped.
    """
    agreement_id, _payers = expired_pool_of_three

    with pytest.raises(LogicError, match="seat has not been skipped yet"):
        _claim_refund(app_client, agreement_id, 0)


def test_a_claim_ahead_of_the_cursor_cannot_strand_a_skipped_seat(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
) -> None:
    """The regression the cursor guard exists for.

    `unclaimed_seats` counts seats the refund pass skipped. If a claim on an
    unskipped seat may decrement it, one such claim cancels out a real skip:
    the counter reaches zero while a payer is still owed, that payer's own
    claim then underflows, and `close` is blocked by `total_held` forever --
    the agreement has no exit and the money stays in the app account.
    """
    agreement_id, payers = expired_pool_of_three
    opt_out_of_usdc(algorand, payers[0], usdc_asset_id, usdc_creator)

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 1)
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.refund_cursor == 1 and record.unclaimed_seats == 1

    # Seat 2 is unpaid but ahead of the cursor: not a skipped seat.
    with pytest.raises(LogicError, match="seat has not been skipped yet"):
        _claim_refund(app_client, agreement_id, 2)

    # The refund pass still owns seats 1 and 2, and seat 0 still owes.
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.unclaimed_seats == 1
    assert record.total_held == SHARE_PRICE

    algorand.send.asset_opt_in(
        AssetOptInParams(
            sender=payers[0].address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    _claim_refund(app_client, agreement_id, 0)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.unclaimed_seats == 0 and record.total_held == 0
    assert usdc_balance(algorand, payers[0].address, usdc_asset_id) == SHARE_PRICE

    # The exit the strand would have blocked.
    _close(app_client, agreement_id)
    assert not _box_exists(algorand, app_client, agreement_box_name(agreement_id))


# --- close ----------------------------------------------------------------


_close = call_close
_box_exists = box_exists


def test_close_refuses_while_a_seat_is_unclaimed(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    pool_with_one_skipped_seat,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _skipped_payer, _seat = pool_with_one_skipped_seat

    with pytest.raises(LogicError, match="seats are still unclaimed"):
        _close(app_client, agreement_id, stranger.address)

    # The minimum balance is still locked: the boxes must survive the refusal.
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.unclaimed_seats == 1
    assert _box_exists(algorand, app_client, roster_box_name(agreement_id))


def test_close_succeeds_once_the_last_seat_is_claimed(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    pool_with_one_skipped_seat,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, skipped_payer, seat = pool_with_one_skipped_seat
    algorand.send.asset_opt_in(
        AssetOptInParams(
            sender=skipped_payer.address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    _claim_refund(app_client, agreement_id, seat)

    _close(app_client, agreement_id, stranger.address)

    assert not _box_exists(algorand, app_client, agreement_box_name(agreement_id))
    assert not _box_exists(algorand, app_client, roster_box_name(agreement_id))


def test_close_returns_the_unspent_deposit_to_the_creator(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
    stranger: algokit_utils.SigningAccount,
    hash_agreement: int,
) -> None:
    """Not the whole deposit: the fees this agreement spent stay spent.

    A three-seat pool of four seats staked a five-transaction reserve and used
    four of it -- three refunds and the close payment itself -- so one
    transaction's worth comes back with the box minimum balance.
    `hash_agreement` is here to be the other live agreement whose own deposit
    must survive this close untouched.
    """
    agreement_id, _payers = expired_pool_of_three
    max_seats = app_client.state.box.agreements.get_value(agreement_id).max_seats
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    creator_before = algo_balance(algorand, creator.address)
    app_before = algo_balance(algorand, app_client.app_address)

    result = _close(app_client, agreement_id, stranger.address)

    expected = box_mbr(max_seats) + fee_reserve(max_seats) - 4 * 1_000
    assert algo_balance(algorand, creator.address) - creator_before == expected
    assert one_event(result, "Closed")["algo_returned"] == expected
    # The application paid the refund plus the fee for the payment itself.
    assert app_before - algo_balance(algorand, app_client.app_address) == (
        expected + 1_000
    )

    # ALGO conservation: what is left still covers the other live agreement.
    other = app_client.state.box.agreements.get_value(hash_agreement)
    assert algo_balance(algorand, app_client.app_address) >= deposit_microalgo(
        other.max_seats
    )


def test_close_returns_the_unspent_deposit_after_a_release(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    creator: algokit_utils.SigningAccount,
    funded_agreement: int,
    verifier: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """A single-seat release uses the reserve exactly: one release, one close."""
    from smart_contracts.artifacts.escrow.escrow_client import ReleaseHashArgs
    from tests.conftest import COMMIT_HASH

    app_client.send.release_hash(
        args=ReleaseHashArgs(
            agreement_id=funded_agreement, delivered_bytes_hash=COMMIT_HASH
        ),
        params=CommonAppCallParams(sender=verifier.address),
        send_params=POPULATE,
    )
    creator_before = algo_balance(algorand, creator.address)

    result = _close(app_client, funded_agreement, stranger.address)

    expected = box_mbr(1)  # the two-transaction reserve is fully spent
    assert fee_reserve(1) == 2 * 1_000
    assert algo_balance(algorand, creator.address) - creator_before == expected
    assert one_event(result, "Closed")["algo_returned"] == expected


@pytest.mark.parametrize("state_name", ["open", "funded", "filled", "expired"])
def test_close_rejects_non_terminal_states(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    state_name: str,
    hash_agreement: int,
    funded_agreement: int,
    make_pool,
    make_expired_pool,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id = {
        "open": lambda: hash_agreement,
        "funded": lambda: funded_agreement,
        "filled": lambda: make_pool(min_seats=3)[0],
        "expired": lambda: make_expired_pool(joins=2)[0],
    }[state_name]()

    with pytest.raises(LogicError, match="agreement is not in a terminal state"):
        _close(app_client, agreement_id, stranger.address)

    assert _box_exists(algorand, app_client, roster_box_name(agreement_id))


def test_close_rejects_a_partially_refunded_agreement(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_pool,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = make_expired_pool(joins=6)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    assert (
        app_client.state.box.agreements.get_value(agreement_id).state == STATE_REFUNDING
    )

    with pytest.raises(LogicError, match="agreement is not in a terminal state"):
        _close(app_client, agreement_id, stranger.address)


def test_close_cannot_run_twice(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    expired_pool_of_three,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = expired_pool_of_three
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    _close(app_client, agreement_id, stranger.address)

    with pytest.raises(LogicError):
        _close(app_client, agreement_id, stranger.address)


# --- the pause never reaches an exit --------------------------------------


def test_pause_does_not_block_the_way_out(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    creator: algokit_utils.SigningAccount,
    expired_pool_of_three,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """A pause that could strand a refund would hand the operator exactly the
    power this design refuses to take. Every exit runs while paused."""
    from smart_contracts.artifacts.escrow.escrow_client import SetPausedArgs

    agreement_id, payers = expired_pool_of_three
    opt_out_of_usdc(algorand, payers[1], usdc_asset_id, usdc_creator)

    app_client.send.set_paused(
        args=SetPausedArgs(paused=1),
        params=CommonAppCallParams(sender=creator.address),
    )
    try:
        _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
        record = app_client.state.box.agreements.get_value(agreement_id)
        assert record.state == STATE_REFUNDED and record.unclaimed_seats == 1

        algorand.send.asset_opt_in(
            AssetOptInParams(
                sender=payers[1].address,
                asset_id=usdc_asset_id,
                note=b"re-opt-in",
            )
        )
        _claim_refund(app_client, agreement_id, 1)
        assert usdc_balance(algorand, payers[1].address, usdc_asset_id) == SHARE_PRICE

        _close(app_client, agreement_id, stranger.address)
        assert not _box_exists(algorand, app_client, roster_box_name(agreement_id))
    finally:
        app_client.send.set_paused(
            args=SetPausedArgs(paused=0),
            params=CommonAppCallParams(sender=creator.address),
        )


def test_pause_does_not_block_expire(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    creator: algokit_utils.SigningAccount,
    short_deadline_agreement: int,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    from smart_contracts.artifacts.escrow.escrow_client import SetPausedArgs

    advance_chain_time(300)
    app_client.send.set_paused(
        args=SetPausedArgs(paused=1),
        params=CommonAppCallParams(sender=creator.address),
    )
    try:
        _expire(app_client, short_deadline_agreement, stranger.address)
        assert (
            app_client.state.box.agreements.get_value(short_deadline_agreement).state
            == STATE_EXPIRED
        )
    finally:
        app_client.send.set_paused(
            args=SetPausedArgs(paused=0),
            params=CommonAppCallParams(sender=creator.address),
        )


# --- multi-seat `hash` pools (mode 3) -------------------------------------


def test_a_partially_filled_hash_pool_refunds_exactly_the_seats_it_sold(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_hash_pool,
) -> None:
    """Three of five joined, the deadline passed: three refunds, not five.

    This is the ordinary outcome of a pool that did not attract its quota, and
    it is the buyer-facing half of the guarantee: the money comes back on
    chain, callable by anyone, without the operator's cooperation.
    """
    agreement_id, payers = make_expired_hash_pool(min_seats=5, joins=3)
    before = [usdc_balance(algorand, p.address, usdc_asset_id) for p in payers]

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    for payer, prior in zip(payers, before, strict=True):
        after = usdc_balance(algorand, payer.address, usdc_asset_id)
        assert after - prior == SHARE_PRICE

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.seats == 3
    assert record.max_seats == 5
    assert record.refund_cursor == 3
    assert record.total_held == 0


def test_a_filled_hash_pool_that_was_never_released_refunds_all_five(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_hash_pool,
) -> None:
    """Non-delivery, which is the failure this mode exists to price.

    Five seats sold, the deadline reached with no matching bytes presented, so
    every payer is refunded. The verifier is the operator, so this guarantees
    refund on non-delivery rather than delivery itself -- and that is what
    escrow has always been.

    A `quorum` pool cannot reach here: meeting its quota lands it in FILLED,
    which never expires on the clock. A `hash` pool lands in FUNDED, which
    does.
    """
    agreement_id, payers = make_expired_hash_pool(min_seats=5, joins=5)
    before = [usdc_balance(algorand, p.address, usdc_asset_id) for p in payers]

    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    for payer, prior in zip(payers, before, strict=True):
        after = usdc_balance(algorand, payer.address, usdc_asset_id)
        assert after - prior == SHARE_PRICE

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_REFUNDED
    assert record.refund_cursor == 5
    assert record.total_held == 0


def test_close_after_five_refunds_uses_the_reserve_exactly(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    creator: algokit_utils.SigningAccount,
    make_expired_hash_pool,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """The reserve is `(max_seats + 1)` transactions and no more.

    Five refunds plus the `close` payment is exactly six inner transactions
    for a five-seat agreement, so the reserve is fully spent and `close`
    returns the box minimum balance alone. An under-funded reserve would make
    `close` draw on another agreement's deposit; an over-funded one would
    strand ALGO with no path out.
    """
    agreement_id, _payers = make_expired_hash_pool(min_seats=5, joins=5)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    creator_before = algo_balance(algorand, creator.address)
    result = _close(app_client, agreement_id, stranger.address)

    assert fee_reserve(5) == 6 * 1_000
    expected = box_mbr(5)  # the six-transaction reserve is fully spent
    assert algo_balance(algorand, creator.address) - creator_before == expected
    assert one_event(result, "Closed")["algo_returned"] == expected


def test_close_after_a_partially_filled_pool_returns_the_unspent_reserve(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    creator: algokit_utils.SigningAccount,
    make_expired_hash_pool,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """Three seats sold out of a five-seat reserve leaves two fees unspent.

    `close` returns a computed figure -- box minimum balance plus the
    *unspent* reserve -- rather than whatever was deposited, so the arithmetic
    has to hold for every number of seats actually sold, not just for a full
    pool. The reserve is sized on `max_seats`; what is spent is sized on
    `seats`.
    """
    agreement_id, _payers = make_expired_hash_pool(min_seats=5, joins=3)
    _refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    creator_before = algo_balance(algorand, creator.address)
    result = _close(app_client, agreement_id, stranger.address)

    # Three refunds plus the close payment is four of the six reserved fees.
    expected = box_mbr(5) + (fee_reserve(5) - 4 * 1_000)
    assert algo_balance(algorand, creator.address) - creator_before == expected
    assert one_event(result, "Closed")["algo_returned"] == expected


def test_close_after_a_hash_pool_release_returns_the_unspent_reserve(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    creator: algokit_utils.SigningAccount,
    make_hash_pool,
    verifier: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """The delivery outcome, where the refund tests above are the other one.

    `release_hash` pays `total_held` in a single transfer however many seats
    were sold, so a five-seat agreement that releases spends two of its six
    reserved transactions -- the transfer and the `close` payment -- and the
    other four come back. The reserve is sized for the refund branch, which is
    the expensive one; releasing cannot overrun a reserve that pays for five
    refunds.
    """
    agreement_id, _payers = make_hash_pool(min_seats=5)
    call_release_hash(app_client, agreement_id, verifier.address)

    creator_before = algo_balance(algorand, creator.address)
    result = _close(app_client, agreement_id, stranger.address)

    # One release transfer plus the close payment: two of the six reserved.
    expected = box_mbr(5) + (fee_reserve(5) - 2 * 1_000)
    assert algo_balance(algorand, creator.address) - creator_before == expected
    assert one_event(result, "Closed")["algo_returned"] == expected
