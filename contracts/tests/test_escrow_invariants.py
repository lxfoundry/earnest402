"""Property tests for the invariants in the specification.

All agreements share one application account, so the invariant that matters
most is conservation: the sum of what the records claim to hold never exceeds
what the account actually holds. A bug in one agreement reaching another's
money would show up here first.
"""

import random

import algokit_utils
import pytest
from algokit_utils import (
    AlgoAmount,
    AlgorandClient,
    AssetOptInParams,
    LogicError,
)

from smart_contracts.artifacts.escrow.escrow_client import EscrowClient
from tests.conftest import (
    CONDITION_QUORUM,
    SHARE_PRICE,
    STATE_EXPIRED,
    STATE_FILLED,
    STATE_FUNDED,
    STATE_REFUNDED,
    STATE_REFUNDING,
    STATE_RELEASED,
    algo_balance,
    box_mbr,
    call_claim_refund,
    call_close,
    call_expire,
    call_refund_next,
    call_release_hash,
    call_release_quorum,
    chain_now,
    events_named,
    fee_reserve,
    one_event,
    opt_out_of_usdc,
    roster_seats,
    usdc_balance,
)

SEED = 20260821
REFUNDING_STATES = (STATE_EXPIRED, STATE_REFUNDING, STATE_REFUNDED)


def _live_agreements(app_client: EscrowClient) -> dict:
    """Every agreement box that still exists, keyed by id.

    The generated map accessor hands back its uint64 keys as strings.
    """
    return {
        int(key): record
        for key, record in app_client.state.box.agreements.get_map().items()
    }


def _inner_transactions_spent(
    algorand: AlgorandClient, app_client: EscrowClient, agreement_id: int, record
) -> int:
    """How much of this agreement's fee reserve has been drawn down.

    A release is one inner transfer. A refund is one per seat actually paid,
    and a paid seat is exactly a zeroed amount on the roster.
    """
    if record.state == STATE_RELEASED:
        return 1
    if record.state in REFUNDING_STATES:
        occupied = roster_seats(algorand, app_client, agreement_id)[: record.seats]
        return sum(1 for _payer, amount in occupied if amount == 0)
    return 0


def assert_conservation(
    algorand: AlgorandClient, app_client: EscrowClient, usdc_asset_id: int
) -> None:
    """USDC and ALGO conservation, both checked against the real account."""
    live = _live_agreements(app_client)

    held = sum(record.total_held for record in live.values())
    on_account = usdc_balance(algorand, app_client.app_address, usdc_asset_id)
    assert held <= on_account, (
        f"conservation violated: {held} held, {on_account} on account"
    )

    staked = 0
    for agreement_id, record in live.items():
        spent = _inner_transactions_spent(algorand, app_client, agreement_id, record)
        staked += (
            box_mbr(record.max_seats) + fee_reserve(record.max_seats) - spent * 1_000
        )
    balance = algo_balance(algorand, app_client.app_address)
    assert balance >= staked, (
        f"ALGO conservation violated: {staked} staked, {balance} held"
    )


def assert_structural_bounds(app_client: EscrowClient) -> None:
    for agreement_id, record in _live_agreements(app_client).items():
        assert record.seats <= record.max_seats, f"{agreement_id}: seats > max_seats"
        assert record.refund_cursor <= record.seats, f"{agreement_id}: cursor > seats"
        assert record.unclaimed_seats <= record.seats, (
            f"{agreement_id}: unclaimed > seats"
        )
        # Seat counts are fixed on every condition: a pool is sized at
        # creation and `max_seats == min_seats` (spec 4).
        assert record.min_seats == record.max_seats


def test_conservation_holds_across_interleaved_lifecycles(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """One agreement released while another is refunded, step by step."""
    delivery = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    ).abi_return
    pool = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=3,
        max_seats=3,
        deadline=chain_now(algorand) + 60,
    ).abi_return
    assert_conservation(algorand, app_client, usdc_asset_id)

    payers = [make_buyer() for _ in range(3)]

    steps = [
        lambda: join_agreement(delivery, payers[0]),
        lambda: join_agreement(pool, payers[1]),
        lambda: join_agreement(pool, payers[2]),
        lambda: call_release_hash(app_client, delivery, verifier.address),
        lambda: advance_chain_time(300),
        lambda: call_expire(app_client, pool, stranger.address),
        lambda: call_refund_next(
            algorand, app_client, usdc_asset_id, pool, 4, sender=stranger.address
        ),
        lambda: call_close(app_client, delivery, stranger.address),
        lambda: call_close(app_client, pool, stranger.address),
    ]
    for step in steps:
        step()
        assert_conservation(algorand, app_client, usdc_asset_id)
        assert_structural_bounds(app_client)

    # Both records are gone and nothing of theirs is left on the account.
    assert _live_agreements(app_client) == {}
    assert usdc_balance(algorand, app_client.app_address, usdc_asset_id) == 0


def test_structural_bounds_hold_through_a_randomised_sequence(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """Three agreements of different shapes, their steps interleaved.

    The seed is fixed so a failure reproduces; what varies is the order in
    which three independent lifecycles are advanced against each other.
    """
    rng = random.Random(SEED)
    deadline = chain_now(algorand) + 120

    delivery = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=deadline,
    ).abi_return
    missed = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=4,
        max_seats=4,
        deadline=deadline,
    ).abi_return
    filled = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=2,
        max_seats=2,
    ).abi_return

    buyers = [make_buyer() for _ in range(6)]
    tracks = [
        [
            lambda: join_agreement(delivery, buyers[0]),
            lambda: call_release_hash(app_client, delivery, verifier.address),
        ],
        [
            lambda: join_agreement(missed, buyers[1]),
            lambda: join_agreement(missed, buyers[2]),
            lambda: join_agreement(missed, buyers[3]),
        ],
        [
            lambda: join_agreement(filled, buyers[4]),
            lambda: join_agreement(filled, buyers[5]),
            lambda: call_release_quorum(app_client, filled, stranger.address),
        ],
    ]

    while any(tracks):
        track = rng.choice([t for t in tracks if t])
        track.pop(0)()
        assert_structural_bounds(app_client)
        assert_conservation(algorand, app_client, usdc_asset_id)

    # The pool that never reached its quota now runs out the clock.
    advance_chain_time(600)
    for step in (
        lambda: call_expire(app_client, missed, stranger.address),
        lambda: call_refund_next(
            algorand, app_client, usdc_asset_id, missed, 4, sender=stranger.address
        ),
        lambda: call_close(app_client, missed, stranger.address),
        lambda: call_close(app_client, delivery, stranger.address),
        lambda: call_close(app_client, filled, stranger.address),
    ):
        step()
        assert_structural_bounds(app_client)
        assert_conservation(algorand, app_client, usdc_asset_id)


def test_a_payer_appears_on_a_roster_at_most_once(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
) -> None:
    """`quorum` claims N *distinct* payers; two seats for one address would
    make that claim false."""
    pool = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=4,
        max_seats=4,
    ).abi_return
    repeat_offender = make_buyer()

    join_agreement(pool, repeat_offender)
    for _ in range(2):
        # Attempted again after each further seat is taken, not just once.
        with pytest.raises(LogicError, match="payer already joined this agreement"):
            join_agreement(pool, repeat_offender)
        join_agreement(pool, make_buyer())

    record = app_client.state.box.agreements.get_value(pool)
    assert record.seats == 3
    occupied = roster_seats(algorand, app_client, pool)[: record.seats]
    addresses = [payer for payer, _amount in occupied]
    assert len(set(addresses)) == len(addresses)
    assert addresses.count(repeat_offender.address) == 1
    assert_conservation(algorand, app_client, usdc_asset_id)


def test_a_release_moves_total_held_and_nothing_else(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
    hash_agreement: int,
) -> None:
    """`hash_agreement` is an untouched agreement whose money must not move."""
    pool = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=3,
        max_seats=3,
    ).abi_return
    payers = [make_buyer() for _ in range(3)]
    for payer in payers:
        join_agreement(pool, payer)
    join_agreement(hash_agreement, make_buyer())

    held = app_client.state.box.agreements.get_value(pool).total_held
    beneficiary_before = usdc_balance(algorand, beneficiary.address, usdc_asset_id)
    app_before = usdc_balance(algorand, app_client.app_address, usdc_asset_id)

    result = call_release_quorum(app_client, pool, stranger.address)

    assert (
        usdc_balance(algorand, beneficiary.address, usdc_asset_id) - beneficiary_before
        == held
    )
    assert (
        app_before - usdc_balance(algorand, app_client.app_address, usdc_asset_id)
        == held
    )
    # The bystander agreement still holds exactly what it did.
    assert (
        app_client.state.box.agreements.get_value(hash_agreement).total_held
        == SHARE_PRICE
    )
    assert one_event(result, "Released")["amount"] == held
    assert_conservation(algorand, app_client, usdc_asset_id)


def test_cursor_and_unclaimed_seats_mean_different_things(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    make_expired_pool,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """Each condition alone leaves money owed; only together do they mean paid."""
    # Cursor finished, one seat still owed.
    skipped_id, skipped_payers = make_expired_pool(joins=3)
    opt_out_of_usdc(algorand, skipped_payers[1], usdc_asset_id, usdc_creator)
    call_refund_next(algorand, app_client, usdc_asset_id, skipped_id, 4)
    record = app_client.state.box.agreements.get_value(skipped_id)
    assert record.refund_cursor == record.seats
    assert record.unclaimed_seats == 1
    assert record.total_held == SHARE_PRICE

    # Nothing skipped, but the cursor has not finished its pass.
    partial_id, _payers = make_expired_pool(joins=6)
    call_refund_next(algorand, app_client, usdc_asset_id, partial_id, 4)
    record = app_client.state.box.agreements.get_value(partial_id)
    assert record.unclaimed_seats == 0
    assert record.refund_cursor < record.seats
    assert record.total_held == SHARE_PRICE * 2

    # Both, and only then is the agreement settled.
    algorand.send.asset_opt_in(
        AssetOptInParams(
            sender=skipped_payers[1].address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    call_claim_refund(app_client, skipped_id, 1)
    call_refund_next(algorand, app_client, usdc_asset_id, partial_id, 2)
    for agreement_id in (skipped_id, partial_id):
        record = app_client.state.box.agreements.get_value(agreement_id)
        assert record.unclaimed_seats == 0
        assert record.refund_cursor == record.seats
        assert record.total_held == 0

    assert_conservation(algorand, app_client, usdc_asset_id)


def test_no_transition_leaves_a_terminal_state(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    join_agreement,
    make_buyer,
    make_expired_pool,
    funded_agreement: int,
    verifier: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    call_release_hash(app_client, funded_agreement, verifier.address)
    refunded_id, _payers = make_expired_pool(joins=3)
    call_refund_next(algorand, app_client, usdc_asset_id, refunded_id, 4)
    # Past the delivery agreement's own deadline too, so `expire` has to fail
    # on the state guard rather than on the clock.
    advance_chain_time(7_200)

    for agreement_id, state in (
        (funded_agreement, STATE_RELEASED),
        (refunded_id, STATE_REFUNDED),
    ):
        assert app_client.state.box.agreements.get_value(agreement_id).state == state

        with pytest.raises(LogicError, match="agreement is not open"):
            join_agreement(agreement_id, make_buyer())
        with pytest.raises(LogicError, match="agreement is not funded"):
            call_release_hash(app_client, agreement_id, verifier.address)
        with pytest.raises(LogicError, match="agreement is not filled"):
            call_release_quorum(app_client, agreement_id, stranger.address)
        with pytest.raises(LogicError, match="agreement cannot expire from this state"):
            call_expire(app_client, agreement_id, stranger.address)
        with pytest.raises(LogicError, match="agreement is not refunding"):
            call_refund_next(
                algorand,
                app_client,
                usdc_asset_id,
                agreement_id,
                1,
                sender=stranger.address,
            )

        assert app_client.state.box.agreements.get_value(agreement_id).state == state

    # `claim_refund` is the one method that stays reachable after REFUNDED --
    # by design, since REFUNDED means the cursor finished, not that everyone
    # was paid. It still refuses a seat that is already settled.
    with pytest.raises(LogicError, match="seat is already settled"):
        call_claim_refund(app_client, refunded_id, 0)
    with pytest.raises(LogicError, match="agreement is not in a refunding state"):
        call_claim_refund(app_client, funded_agreement, 0)

    assert_conservation(algorand, app_client, usdc_asset_id)

    # Only `close` ends them, and it does so by deleting the record.
    for agreement_id in (funded_agreement, refunded_id):
        call_close(app_client, agreement_id, stranger.address)
        assert agreement_id not in _live_agreements(app_client)


def test_every_usdc_moving_path_emits_exactly_one_event(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    usdc_creator: algokit_utils.SigningAccount,
    join_agreement,
    make_buyer,
    make_expired_pool,
    hash_agreement: int,
    verifier: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    buyer = make_buyer()
    join_agreement(hash_agreement, buyer)

    release = call_release_hash(app_client, hash_agreement, verifier.address)
    assert len(events_named(release, "Released")) == 1
    assert events_named(release, "Refunded") == []

    agreement_id, payers = make_expired_pool(joins=3)
    opt_out_of_usdc(algorand, payers[1], usdc_asset_id, usdc_creator)
    refund = call_refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    # Two seats paid, one skipped: three seats, three events, no more.
    assert len(events_named(refund, "Refunded")) == 2
    assert len(events_named(refund, "RefundSkipped")) == 1
    assert len(events_named(refund, "RefundComplete")) == 1

    algorand.send.asset_opt_in(
        AssetOptInParams(
            sender=payers[1].address,
            asset_id=usdc_asset_id,
            note=b"re-opt-in",
        )
    )
    claim = call_claim_refund(app_client, agreement_id, 1)
    assert len(events_named(claim, "RefundClaimed")) == 1
    assert events_named(claim, "Refunded") == []

    closed = call_close(app_client, agreement_id, stranger.address)
    assert len(events_named(closed, "Closed")) == 1
    assert_conservation(algorand, app_client, usdc_asset_id)


def test_a_join_emits_one_event_carrying_the_settlement_txid(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    """The audit spine is chain data: the contract records the leg-1 txid."""
    from algokit_utils import (
        AssetTransferParams,
        CommonAppCallParams,
        PaymentParams,
    )

    from smart_contracts.artifacts.escrow.escrow_client import JoinArgs
    from tests.conftest import POPULATE

    agreement_id = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    ).abi_return
    buyer = make_buyer()

    result = (
        algorand.new_group()
        .add_payment(
            PaymentParams(
                sender=buyer.address,
                receiver=buyer.address,
                amount=AlgoAmount(micro_algo=0),
            )
        )
        .add_asset_transfer(
            AssetTransferParams(
                sender=buyer.address,
                receiver=app_client.app_address,
                asset_id=usdc_asset_id,
                amount=SHARE_PRICE,
            )
        )
        .add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=agreement_id, payment_index=1),
                params=CommonAppCallParams(sender=buyer.address),
            )
        )
    ).send(POPULATE)

    event = one_event(result, "Joined")
    assert event["payer"] == buyer.address
    assert event["amount"] == SHARE_PRICE
    assert event["seat"] == 0
    assert event["provenance"] == 0
    # The settlement transaction is the asset transfer, group index 1.
    assert bytes(event["payment_txn_id"]).hex() != ""
    assert len(bytes(event["payment_txn_id"])) == 32
    assert_conservation(algorand, app_client, usdc_asset_id)


def test_states_reachable_by_a_pool_are_the_documented_ones(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary: algokit_utils.SigningAccount,
    advance_chain_time,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """Walk one pool through OPEN, FILLED, RELEASED and one through the
    refund side, checking the state after each step rather than only at the
    end."""
    filled = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=2,
        max_seats=2,
    ).abi_return
    join_agreement(filled, make_buyer())
    assert app_client.state.box.agreements.get_value(filled).state == 0
    join_agreement(filled, make_buyer())
    assert app_client.state.box.agreements.get_value(filled).state == STATE_FILLED
    call_release_quorum(app_client, filled, stranger.address)
    assert app_client.state.box.agreements.get_value(filled).state == STATE_RELEASED

    missed, _payers = make_expired_pool_states(
        algorand,
        app_client,
        usdc_asset_id,
        create_agreement,
        join_agreement,
        make_buyer,
        beneficiary,
        advance_chain_time,
        stranger,
    )
    assert app_client.state.box.agreements.get_value(missed).state == STATE_REFUNDED
    assert_conservation(algorand, app_client, usdc_asset_id)


def make_expired_pool_states(
    algorand,
    app_client,
    usdc_asset_id,
    create_agreement,
    join_agreement,
    make_buyer,
    beneficiary,
    advance_chain_time,
    stranger,
):
    """A pool taken OPEN -> EXPIRED -> REFUNDING -> REFUNDED, asserting each."""
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=8,
        max_seats=8,
        deadline=chain_now(algorand) + 60,
    ).abi_return
    payers = [make_buyer() for _ in range(6)]
    for payer in payers:
        join_agreement(agreement_id, payer)
    assert app_client.state.box.agreements.get_value(agreement_id).state == 0

    advance_chain_time(300)
    call_expire(app_client, agreement_id, stranger.address)
    assert (
        app_client.state.box.agreements.get_value(agreement_id).state == STATE_EXPIRED
    )

    call_refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    assert (
        app_client.state.box.agreements.get_value(agreement_id).state == STATE_REFUNDING
    )
    call_refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)
    return agreement_id, payers


def test_funded_state_is_reachable_only_by_a_hash_agreement(
    app_client: EscrowClient,
    funded_agreement: int,
    filled_pool,
) -> None:
    pool_id, _payers = filled_pool
    assert app_client.state.box.agreements.get_value(funded_agreement).state == (
        STATE_FUNDED
    )
    assert app_client.state.box.agreements.get_value(pool_id).state == STATE_FILLED
