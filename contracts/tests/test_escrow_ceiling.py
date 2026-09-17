"""The published `max_seats` ceiling, exercised at the ceiling.

Every parameter the specification publishes has to be reachable. The
duplicate-payer scan in `join` is O(occupied seats) and the group scan added
beside it spends from the same per-call opcode budget, so the largest pool the
contract will actually fill is a measured number, not an assumed one. These
tests fill one, release it, and refund two -- the paths whose cost grows with
the roster, refunded at the widest roster each condition can reach.

They are slow by nature: a ceiling-sized pool is one settlement group per seat.
"""

from algokit_utils import AlgorandClient

from smart_contracts.artifacts.escrow.escrow_client import EscrowClient
from smart_contracts.escrow.contract import MAX_SEATS_CEILING, REFUND_BATCH_CEILING
from tests.conftest import (
    SHARE_PRICE,
    STATE_FILLED,
    STATE_FUNDED,
    STATE_REFUNDED,
    STATE_RELEASED,
    call_refund_next,
    call_release_quorum,
    usdc_balance,
)


def test_a_pool_at_the_published_ceiling_fills(
    app_client: EscrowClient, make_pool
) -> None:
    """The test whose absence let the ceiling be wrong.

    `join` costs more with every seat already taken, so the last seat is the
    expensive one and the only one that proves the number.
    """
    agreement_id, payers = make_pool(min_seats=MAX_SEATS_CEILING)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.seats == MAX_SEATS_CEILING
    assert record.state == STATE_FILLED
    assert record.total_held == SHARE_PRICE * MAX_SEATS_CEILING
    assert len(payers) == MAX_SEATS_CEILING


def test_a_hash_pool_fills_every_seat_up_to_the_ceiling(
    app_client: EscrowClient, make_hash_pool
) -> None:
    """20 seats is a measured limit, and it is measured on `join`.

    Joins died at 28 before the group scan and at 26 after it, so the ceiling
    carries deliberate headroom. It is asserted on the `hash` path too because
    the duplicate-payer scan and the group scan, which are what consume the
    budget, cost the same whichever condition the agreement carries.
    """
    agreement_id, payers = make_hash_pool(min_seats=MAX_SEATS_CEILING)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.seats == MAX_SEATS_CEILING
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE * MAX_SEATS_CEILING
    assert len(payers) == MAX_SEATS_CEILING


def test_release_quorum_fits_its_budget_at_the_ceiling(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    beneficiary,
    make_pool,
) -> None:
    """Release pays one inner transfer, but it reads a full-sized roster box."""
    agreement_id, _ = make_pool(min_seats=MAX_SEATS_CEILING)
    before = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    call_release_quorum(app_client, agreement_id)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_RELEASED
    assert record.total_held == 0
    after = usdc_balance(algorand, beneficiary.address, usdc_asset_id)
    assert after - before == SHARE_PRICE * MAX_SEATS_CEILING


def test_refund_clears_the_largest_pool_that_can_expire(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_pool,
) -> None:
    """The widest roster a `quorum` refund ever walks, at one short of the ceiling.

    A `quorum` pool that reaches its quota lands in FILLED, which never
    expires on the clock, so `MAX_SEATS_CEILING - 1` seated payers is the most
    the paging arithmetic ever clears on this path. It is also the case that
    exercises the `last > seats` clamp: 19 pages as 4+4+4+4+3, where 20 is a
    clean multiple of the batch ceiling.

    A `hash` pool is not bounded this way -- see
    test_a_hash_pool_refunds_a_roster_at_the_full_ceiling below.
    """
    seated = MAX_SEATS_CEILING - 1
    agreement_id, payers = make_expired_pool(joins=seated)
    before = [usdc_balance(algorand, p.address, usdc_asset_id) for p in payers]

    while app_client.state.box.agreements.get_value(agreement_id).state != (
        STATE_REFUNDED
    ):
        call_refund_next(algorand, app_client, usdc_asset_id, agreement_id, 4)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.refund_cursor == seated
    assert record.unclaimed_seats == 0
    assert record.total_held == 0
    for payer, start in zip(payers, before, strict=True):
        got = usdc_balance(algorand, payer.address, usdc_asset_id) - start
        assert got == SHARE_PRICE, f"{payer.address} was refunded {got}"


def test_a_hash_pool_refunds_a_roster_at_the_full_ceiling(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_expired_hash_pool,
) -> None:
    """The widest roster any refund walks, and only the `hash` path reaches it.

    `quorum` stops one seat short: meeting its quota lands it in FILLED, which
    never expires on the clock. A `hash` pool meeting its quota lands in
    FUNDED, which `expire` accepts, so the published ceiling is reachable on
    the refund path too -- and at exactly the ceiling the walk is a whole
    number of full pages, where 19 seats ends on a short one.
    """
    agreement_id, payers = make_expired_hash_pool(
        min_seats=MAX_SEATS_CEILING, joins=MAX_SEATS_CEILING
    )
    before = [usdc_balance(algorand, p.address, usdc_asset_id) for p in payers]

    pages = 0
    while app_client.state.box.agreements.get_value(agreement_id).state != (
        STATE_REFUNDED
    ):
        call_refund_next(
            algorand, app_client, usdc_asset_id, agreement_id, REFUND_BATCH_CEILING
        )
        pages += 1

    assert pages == MAX_SEATS_CEILING // REFUND_BATCH_CEILING
    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.refund_cursor == MAX_SEATS_CEILING
    assert record.unclaimed_seats == 0
    assert record.total_held == 0
    for payer, start in zip(payers, before, strict=True):
        got = usdc_balance(algorand, payer.address, usdc_asset_id) - start
        assert got == SHARE_PRICE, f"{payer.address} was refunded {got}"
