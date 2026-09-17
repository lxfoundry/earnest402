"""LocalNet tests for Escrow.join: the settlement group, the roster, and the
seat transitions.

The group under test is the one the facilitator actually produces:
`[feePayer, axfer buyer -> app account, appCall join]`, with the payment read
by absolute index rather than as an ABI transaction argument.
"""

import algokit_utils
import pytest
from algokit_utils import (
    AlgoAmount,
    AlgorandClient,
    AppCallParams,
    AssetCreateParams,
    AssetOptInParams,
    AssetTransferParams,
    CommonAppCallParams,
    LogicError,
    PaymentParams,
)
from algosdk.abi import Method
from algosdk.transaction import OnComplete

from smart_contracts.artifacts.escrow.escrow_client import (
    EscrowClient,
    JoinArgs,
    OptInAssetArgs,
)
from tests.conftest import (
    CONDITION_QUORUM,
    POPULATE,
    SHARE_PRICE,
    STATE_FILLED,
    STATE_FUNDED,
    STATE_OPEN,
    chain_now,
    events_named,
    one_event,
)
from tests.test_escrow_invariants import assert_conservation


def join_group(
    algorand: AlgorandClient,
    buyer: algokit_utils.SigningAccount,
    app_client: EscrowClient,
    usdc_asset_id: int,
    *,
    agreement_id: int,
    amount: int = SHARE_PRICE,
    payment_index: int = 1,
    close_asset_to: str | None = None,
    rekey_to: str | None = None,
    payment_first: bool = False,
):
    """[feePayer self-payment, axfer buyer -> app account, appCall join].

    `payment_first` swaps the first two legs, the other ordering the
    facilitator was observed to produce.
    """
    fee_payer = PaymentParams(
        sender=buyer.address,
        receiver=buyer.address,
        amount=AlgoAmount(micro_algo=0),
    )
    transfer = AssetTransferParams(
        sender=buyer.address,
        receiver=app_client.app_address,
        asset_id=usdc_asset_id,
        amount=amount,
        close_asset_to=close_asset_to,
        rekey_to=rekey_to,
    )

    group = algorand.new_group()
    if payment_first:
        group = group.add_asset_transfer(transfer).add_payment(fee_payer)
    else:
        group = group.add_payment(fee_payer).add_asset_transfer(transfer)

    return group.add_app_call_method_call(
        app_client.params.join(
            args=JoinArgs(agreement_id=agreement_id, payment_index=payment_index),
            params=CommonAppCallParams(sender=buyer.address),
        )
    )


@pytest.fixture
def quorum_pool(create_agreement, beneficiary) -> int:
    """An OPEN pool of exactly 3 seats."""
    return create_agreement(
        beneficiary=beneficiary.address,
        condition=CONDITION_QUORUM,
        min_seats=3,
        max_seats=3,
    ).abi_return


@pytest.fixture
def other_asset_id(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_creator: algokit_utils.SigningAccount,
) -> int:
    """A second 6-decimal asset, to prove `join` checks which one it was paid in.

    The application is opted in to it as well, so a transfer of the wrong
    asset reaches the contract instead of being rejected by the AVM for a
    missing holding -- otherwise the `wrong asset` assert is never exercised.
    """
    asset_id = algorand.send.asset_create(
        AssetCreateParams(
            sender=usdc_creator.address,
            total=1_000_000_000,
            decimals=6,
            asset_name="Not USDC",
            unit_name="nUSD",
        )
    ).asset_id
    app_client.send.opt_in_asset(
        args=OptInAssetArgs(asset_id=asset_id),
        params=CommonAppCallParams(extra_fee=AlgoAmount(micro_algo=1_000)),
    )
    return asset_id


def test_join_records_payer_and_holds_funds(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
    hash_agreement: int,
) -> None:
    join_group(
        algorand,
        buyer,
        app_client,
        usdc_asset_id,
        agreement_id=hash_agreement,
        amount=SHARE_PRICE,
    ).send(POPULATE)

    record = app_client.state.box.agreements.get_value(hash_agreement)
    assert record.seats == 1
    assert record.total_held == SHARE_PRICE
    assert record.state == STATE_FUNDED


def test_join_accepts_the_payment_leg_first_ordering(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
    hash_agreement: int,
) -> None:
    """[axfer, feePayer, appCall] -- the other ordering the facilitator produced."""
    join_group(
        algorand,
        buyer,
        app_client,
        usdc_asset_id,
        agreement_id=hash_agreement,
        payment_index=0,
        payment_first=True,
    ).send(POPULATE)

    record = app_client.state.box.agreements.get_value(hash_agreement)
    assert record.seats == 1
    assert record.total_held == SHARE_PRICE


def test_quorum_pool_fills_on_the_third_join(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    make_buyer,
    usdc_asset_id: int,
    quorum_pool: int,
) -> None:
    for expected_seats in (1, 2):
        join_group(
            algorand,
            make_buyer(),
            app_client,
            usdc_asset_id,
            agreement_id=quorum_pool,
        ).send(POPULATE)
        record = app_client.state.box.agreements.get_value(quorum_pool)
        assert record.seats == expected_seats
        assert record.state == STATE_OPEN

    join_group(
        algorand, make_buyer(), app_client, usdc_asset_id, agreement_id=quorum_pool
    ).send(POPULATE)

    record = app_client.state.box.agreements.get_value(quorum_pool)
    assert record.seats == 3
    assert record.state == STATE_FILLED
    assert record.total_held == SHARE_PRICE * 3


def test_a_single_seat_funding_join_emits_no_filled_event(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
    hash_agreement: int,
) -> None:
    """The original concern, kept: a funding join is not a quota being met.

    This fixture's agreement is single-seat, so its one join satisfies the
    same `seats >= min_seats` arithmetic a pool's last join does. Emitting on
    the arithmetic alone puts a pool-fill receipt on a delivery escrow's
    spine, which an indexer reads as a quota being met.

    The property is the seat count, not the condition. `condition == quorum`
    was a proxy for it that held only while `hash` was single-seat, which is
    why the guard reads `min_seats > 1` instead -- see
    test_filled_is_emitted_when_a_multi_seat_hash_pool_fills.
    """
    result = join_group(
        algorand,
        buyer,
        app_client,
        usdc_asset_id,
        agreement_id=hash_agreement,
    ).send(POPULATE)

    assert app_client.state.box.agreements.get_value(hash_agreement).state == (
        STATE_FUNDED
    )
    assert events_named(result, "Joined") != []
    assert events_named(result, "Filled") == []


def test_quorum_fill_still_emits_filled(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    make_buyer,
    usdc_asset_id: int,
    quorum_pool: int,
) -> None:
    """The other side of the guard: a pool reaching its quota still reports it."""
    for _ in range(2):
        result = join_group(
            algorand, make_buyer(), app_client, usdc_asset_id, agreement_id=quorum_pool
        ).send(POPULATE)
        assert events_named(result, "Filled") == []

    result = join_group(
        algorand, make_buyer(), app_client, usdc_asset_id, agreement_id=quorum_pool
    ).send(POPULATE)

    filled = one_event(result, "Filled")
    assert filled["agreement_id"] == quorum_pool
    assert filled["seats"] == 3


def test_a_multi_seat_hash_pool_lands_in_funded_not_filled(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_hash_pool,
) -> None:
    """Mode 3's fill goes to FUNDED, which is what `release_hash` demands.

    FILLED and FUNDED both mean "the seats are full"; they differ in how the
    money leaves. FILLED releases permissionlessly on seat count, FUNDED only
    against delivered bytes and only by the verifier -- so a pool protecting
    against non-delivery has to be the second one.

    `join`'s terminal-state branch needed no change for this: it already
    dispatches on the condition, which is the one place that still should.
    """
    agreement_id, payers = make_hash_pool(min_seats=5)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_FUNDED
    assert record.seats == 5
    assert record.total_held == SHARE_PRICE * 5
    assert len(payers) == 5

    # The invariant suite is what catches multi-seat deposit arithmetic being
    # wrong in a way the per-method assertions miss, and this is the only
    # place it sees mode 3.
    assert_conservation(algorand, app_client, usdc_asset_id)


def test_filled_is_emitted_when_a_multi_seat_hash_pool_fills(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    create_agreement,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
    make_buyer,
    usdc_asset_id: int,
) -> None:
    """`Filled` means a seat quota was met, whatever the release condition.

    The guard this test pins used to read `if filled and is_quorum`, whose
    stated reason was that "`hash` has `min_seats == 1`". A multi-seat `hash`
    pool falsifies that premise and does meet a quota, so a consumer keying
    on `Filled` would miss it.
    """
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        min_seats=2,
        max_seats=2,
    ).abi_return

    first = join_group(
        algorand, make_buyer(), app_client, usdc_asset_id, agreement_id=agreement_id
    ).send(POPULATE)
    assert events_named(first, "Filled") == []

    last = join_group(
        algorand, make_buyer(), app_client, usdc_asset_id, agreement_id=agreement_id
    ).send(POPULATE)

    filled = one_event(last, "Filled")
    assert filled["agreement_id"] == agreement_id
    assert filled["seats"] == 2
    # The state still separates the two modes: a quota met on `hash` is
    # FUNDED, not FILLED, so the event and the state say different things on
    # purpose.
    assert app_client.state.box.agreements.get_value(agreement_id).state == (
        STATE_FUNDED
    )


def test_join_rejects_overpayment(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    with pytest.raises(LogicError, match="wrong amount"):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            amount=SHARE_PRICE + 1,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_underpayment(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    with pytest.raises(LogicError, match="wrong amount"):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            amount=SHARE_PRICE - 1,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_the_wrong_asset(
    algorand,
    app_client,
    buyer,
    usdc_creator,
    other_asset_id,
    hash_agreement,
) -> None:
    algorand.send.asset_opt_in(
        AssetOptInParams(sender=buyer.address, asset_id=other_asset_id)
    )
    algorand.send.asset_transfer(
        AssetTransferParams(
            sender=usdc_creator.address,
            receiver=buyer.address,
            asset_id=other_asset_id,
            amount=SHARE_PRICE * 4,
        )
    )
    with pytest.raises(LogicError, match="wrong asset"):
        join_group(
            algorand,
            buyer,
            app_client,
            other_asset_id,
            agreement_id=hash_agreement,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_a_payment_to_another_account(
    algorand, app_client, buyer, usdc_asset_id, beneficiary, hash_agreement
) -> None:
    group = (
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
                receiver=beneficiary.address,
                asset_id=usdc_asset_id,
                amount=SHARE_PRICE,
            )
        )
        .add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=hash_agreement, payment_index=1),
                params=CommonAppCallParams(sender=buyer.address),
            )
        )
    )
    with pytest.raises(LogicError, match="payment must be made to this app account"):
        group.send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_a_payment_from_another_sender(
    algorand, app_client, buyer, make_buyer, usdc_asset_id, hash_agreement
) -> None:
    """A group carrying another buyer transfer cannot buy this caller a seat."""
    other = make_buyer()
    group = (
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
                sender=other.address,
                receiver=app_client.app_address,
                asset_id=usdc_asset_id,
                amount=SHARE_PRICE,
            )
        )
        .add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=hash_agreement, payment_index=1),
                params=CommonAppCallParams(sender=buyer.address),
            )
        )
    )
    with pytest.raises(
        LogicError, match="referenced payment sender must match the caller"
    ):
        group.send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_nonzero_asset_close_to(
    algorand, app_client, buyer, usdc_asset_id, usdc_creator, hash_agreement
) -> None:
    with pytest.raises(LogicError, match="asset_close_to must be unset"):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            close_asset_to=usdc_creator.address,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_nonzero_rekey_to(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    # Rekeying to the sender own address leaves the group signable while
    # still setting a non-zero rekey_to field for the contract to reject.
    with pytest.raises(LogicError, match="rekey_to must be unset"):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            rekey_to=buyer.address,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_self_referencing_payment_index(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    with pytest.raises(
        LogicError, match="payment_index must not reference the app call itself"
    ):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            payment_index=2,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_a_payment_index_pointing_at_the_fee_leg(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    """Index 0 is the fee payment, not an asset transfer: the type check fails."""
    with pytest.raises(LogicError):
        join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
            payment_index=0,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(hash_agreement).seats == 0


def test_join_rejects_a_duplicate_payer(
    algorand, app_client, buyer, usdc_asset_id, quorum_pool
) -> None:
    join_group(
        algorand, buyer, app_client, usdc_asset_id, agreement_id=quorum_pool
    ).send(POPULATE)

    with pytest.raises(LogicError, match="payer already joined this agreement"):
        join_group(
            algorand, buyer, app_client, usdc_asset_id, agreement_id=quorum_pool
        ).send(POPULATE)

    record = app_client.state.box.agreements.get_value(quorum_pool)
    assert record.seats == 1
    assert record.total_held == SHARE_PRICE


def test_join_rejects_a_passed_deadline(
    algorand,
    app_client,
    buyer,
    usdc_asset_id,
    create_agreement,
    beneficiary,
    verifier,
    advance_chain_time,
) -> None:
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=chain_now(algorand) + 60,
    ).abi_return

    advance_chain_time(300)

    with pytest.raises(LogicError, match="deadline passed"):
        join_group(
            algorand, buyer, app_client, usdc_asset_id, agreement_id=agreement_id
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(agreement_id).seats == 0


def test_join_rejects_an_agreement_that_is_no_longer_open(
    algorand, app_client, buyer, make_buyer, usdc_asset_id, hash_agreement
) -> None:
    join_group(
        algorand, buyer, app_client, usdc_asset_id, agreement_id=hash_agreement
    ).send(POPULATE)
    assert (
        app_client.state.box.agreements.get_value(hash_agreement).state == STATE_FUNDED
    )

    with pytest.raises(LogicError, match="agreement is not open"):
        join_group(
            algorand,
            make_buyer(),
            app_client,
            usdc_asset_id,
            agreement_id=hash_agreement,
        ).send(POPULATE)


def test_join_rejects_a_pool_that_has_already_filled(
    algorand, app_client, make_buyer, usdc_asset_id, quorum_pool
) -> None:
    """The state guard is what stops the fourth payer.

    A pool leaves OPEN the moment `seats` reaches `min_seats`, and `join` only
    accepts OPEN. Since `max_seats == min_seats` the two limits coincide, so
    the `seats < max_seats` assert never fires first -- it is belt-and-braces
    behind the state guard, not the thing doing the work.
    """
    for _ in range(3):
        join_group(
            algorand,
            make_buyer(),
            app_client,
            usdc_asset_id,
            agreement_id=quorum_pool,
        ).send(POPULATE)

    record = app_client.state.box.agreements.get_value(quorum_pool)
    assert record.seats == 3 and record.max_seats == 3
    assert record.state == STATE_FILLED

    with pytest.raises(LogicError, match="agreement is not open"):
        join_group(
            algorand,
            make_buyer(),
            app_client,
            usdc_asset_id,
            agreement_id=quorum_pool,
        ).send(POPULATE)
    assert app_client.state.box.agreements.get_value(quorum_pool).seats == 3


def double_credit_group(
    algorand: AlgorandClient,
    buyer: algokit_utils.SigningAccount,
    app_client: EscrowClient,
    usdc_asset_id: int,
    *,
    first_id: int,
    second_id: int,
    second_buyer: algokit_utils.SigningAccount | None = None,
):
    """[feePayer, axfer, join(first), join(second)] -- two joins, one transfer.

    The attack shape from spec 7: a single transfer referenced by both app
    calls. `second_buyer` signs the second join when the caller wants the two
    joins to have different senders.
    """
    second_buyer = second_buyer or buyer

    group = (
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
    )
    for agreement_id, sender in ((first_id, buyer), (second_id, second_buyer)):
        group = group.add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=agreement_id, payment_index=1),
                params=CommonAppCallParams(sender=sender.address),
            )
        )
    return group


@pytest.fixture
def two_hash_agreements(create_agreement, beneficiary, verifier) -> tuple[int, int]:
    """Two OPEN single-seat agreements at the same share price.

    Same price is what makes the double-credit group pass every per-payment
    check -- and it is the ordinary case, not a contrived one.
    """
    return tuple(
        create_agreement(
            beneficiary=beneficiary.address, verifier=verifier.address
        ).abi_return
        for _ in range(2)
    )


def test_join_rejects_a_payment_already_referenced_by_another_join(
    algorand, app_client, buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """One transfer buys one seat, never two.

    Every per-payment guard passes for both calls: they sit at different
    group indices, one buyer signs both so the sender matches, the amount
    matches each identical share price, and neither roster has seen this
    payer. Only a check across the group catches it.
    """
    first, second = two_hash_agreements

    with pytest.raises(LogicError, match="payment already referenced by another join"):
        double_credit_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            first_id=first,
            second_id=second,
        ).send(POPULATE)

    assert app_client.state.box.agreements.get_value(first).seats == 0
    assert app_client.state.box.agreements.get_value(second).seats == 0


def test_one_payment_never_credits_two_agreements(
    algorand, app_client, buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """The same attack, asserted as conservation rather than as an error.

    Separate from the rejection test on purpose: if the guard is missing this
    reports what the records claim against what the account actually holds,
    which is the damage, rather than reporting an error that did not arrive.
    """
    first, second = two_hash_agreements

    try:
        double_credit_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            first_id=first,
            second_id=second,
        ).send(POPULATE)
    except LogicError:
        pass

    assert_conservation(algorand, app_client, usdc_asset_id)


def test_two_joins_with_their_own_payments_share_a_group(
    algorand, app_client, buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """The case the group scan must not break: one transfer each.

    Distinct `payment_index` values, so both joins are legitimate and both
    agreements end up seated and funded.
    """
    first, second = two_hash_agreements

    group = algorand.new_group().add_payment(
        PaymentParams(
            sender=buyer.address,
            receiver=buyer.address,
            amount=AlgoAmount(micro_algo=0),
        )
    )
    for seq in range(2):
        group = group.add_asset_transfer(
            AssetTransferParams(
                sender=buyer.address,
                receiver=app_client.app_address,
                asset_id=usdc_asset_id,
                amount=SHARE_PRICE,
                # Two transfers with identical fields would share a txn id and
                # the second would be rejected as already in the ledger.
                note=str(seq).encode(),
            )
        )
    for index, agreement_id in enumerate((first, second), start=1):
        group = group.add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=agreement_id, payment_index=index),
                params=CommonAppCallParams(sender=buyer.address),
            )
        )

    group.send(POPULATE)

    for agreement_id in (first, second):
        record = app_client.state.box.agreements.get_value(agreement_id)
        assert record.seats == 1
        assert record.total_held == SHARE_PRICE
        assert record.state == STATE_FUNDED


def test_join_rejects_two_buyers_referencing_one_payment(
    algorand, app_client, buyer, make_buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """Pins the sender guard while the group scan lands beside it.

    Different senders is the case `sender == Txn.sender` really does cover,
    and it must keep failing on that check rather than on the new one.
    """
    first, second = two_hash_agreements

    with pytest.raises(
        LogicError, match="referenced payment sender must match the caller"
    ):
        double_credit_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            first_id=first,
            second_id=second,
            second_buyer=make_buyer(),
        ).send(POPULATE)

    assert app_client.state.box.agreements.get_value(first).seats == 0
    assert app_client.state.box.agreements.get_value(second).seats == 0


JOIN_SELECTOR = Method.from_signature("join(uint64,uint64)void").get_selector()


def padded_join_args(agreement_id: int, payment_index: int) -> list[bytes]:
    """A `join` call carrying one argument the ABI does not declare.

    Nothing on chain enforces an ARC-4 argument count. The router dispatches
    on args[0] and reads args 1 and 2 by index, so the fourth argument is
    never read by the contract; the only thing it changes is `num_app_args`,
    which is what the group scan sees.
    """
    return [
        JOIN_SELECTOR,
        agreement_id.to_bytes(8, "big"),
        payment_index.to_bytes(8, "big"),
        b"\x00" * 8,
    ]


def padded_join_group(
    algorand: AlgorandClient,
    buyer: algokit_utils.SigningAccount,
    app_client: EscrowClient,
    usdc_asset_id: int,
    *,
    first_id: int,
    second_id: int,
):
    """`double_credit_group`, with the first join padded to four app args.

    The hidden call is the earlier one: the scan runs backwards, so padding
    the join that has already taken its seat is what a later join would fail
    to see.
    """
    group = (
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
        .add_app_call(
            AppCallParams(
                sender=buyer.address,
                app_id=app_client.app_id,
                on_complete=OnComplete.NoOpOC,
                args=padded_join_args(first_id, 1),
            )
        )
    )
    return group.add_app_call_method_call(
        app_client.params.join(
            args=JoinArgs(agreement_id=second_id, payment_index=1),
            params=CommonAppCallParams(sender=buyer.address),
        )
    )


def test_join_executes_with_an_undeclared_extra_app_arg(
    algorand, app_client, buyer, usdc_asset_id, hash_agreement
) -> None:
    """The premise the group scan has to survive.

    An extra argument does not make a call malformed, so the scan cannot
    treat an exact argument count as the mark of a join. If a future
    compiler starts rejecting undeclared arguments this test flips, and the
    scan's lower bound can be revisited on that evidence.
    """
    (
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
        .add_app_call(
            AppCallParams(
                sender=buyer.address,
                app_id=app_client.app_id,
                on_complete=OnComplete.NoOpOC,
                args=padded_join_args(hash_agreement, 1),
            )
        )
        .send(POPULATE)
    )

    record = app_client.state.box.agreements.get_value(hash_agreement)
    assert record.seats == 1
    assert record.total_held == SHARE_PRICE


def test_join_scan_sees_a_join_padded_with_extra_app_args(
    algorand, app_client, buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """Eight bytes of padding must not buy a second seat.

    The padded call takes its seat like any other join, so if the scan keys
    on an exact argument count it is invisible to the join behind it and one
    transfer credits two agreements again.
    """
    first, second = two_hash_agreements

    with pytest.raises(LogicError, match="payment already referenced by another join"):
        padded_join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            first_id=first,
            second_id=second,
        ).send(POPULATE)

    assert app_client.state.box.agreements.get_value(first).seats == 0
    assert app_client.state.box.agreements.get_value(second).seats == 0


def test_one_padded_payment_never_credits_two_agreements(
    algorand, app_client, buyer, usdc_asset_id, two_hash_agreements
) -> None:
    """The padded attack, asserted as conservation rather than as an error."""
    first, second = two_hash_agreements

    try:
        padded_join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            first_id=first,
            second_id=second,
        ).send(POPULATE)
    except LogicError:
        pass

    assert_conservation(algorand, app_client, usdc_asset_id)
