"""LocalNet tests for the two release paths and the authorisation split.

`release_hash` is the one method with a named caller; everything else is
permissionless. These tests are as much about what the split refuses as about
what it allows.
"""

import algokit_utils
import pytest
from algokit_utils import AlgoAmount, AlgorandClient, CommonAppCallParams, LogicError

from smart_contracts.artifacts.escrow.escrow_client import (
    EscrowClient,
    ReleaseHashArgs,
    ReleaseQuorumArgs,
)
from tests.conftest import (
    COMMIT_HASH,
    POPULATE,
    SHARE_PRICE,
    STATE_FUNDED,
    STATE_RELEASED,
    algo_balance,
    chain_now,
    one_event,
    usdc_balance,
)

WRONG_HASH = bytes(range(1, 33))


def _release_hash(app_client, agreement_id, sender, *, delivered=COMMIT_HASH):
    return app_client.send.release_hash(
        args=ReleaseHashArgs(agreement_id=agreement_id, delivered_bytes_hash=delivered),
        params=CommonAppCallParams(sender=sender, extra_fee=AlgoAmount(micro_algo=0)),
        send_params=POPULATE,
    )


def test_release_hash_pays_beneficiary_and_zeroes_held(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    funded_agreement: int,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    before = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    _release_hash(app_client, funded_agreement, verifier.address)

    after = usdc_balance(algorand, beneficiary.address, usdc_asset_id)
    assert after - before == SHARE_PRICE

    record = app_client.state.box.agreements.get_value(funded_agreement)
    assert record.state == STATE_RELEASED
    assert record.total_held == 0


def test_release_fee_comes_from_the_application_account(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    funded_agreement: int,
    verifier: algokit_utils.SigningAccount,
) -> None:
    """The caller pays only its own fee; the inner fee comes from the reserve.

    `extra_fee` is zero above, so if the application balance did not fall by
    exactly one minimum fee the inner transaction is being paid for by
    whoever triggered it, and the permissionless paths stop being free.
    """
    app_before = algo_balance(algorand, app_client.app_address)
    verifier_before = algo_balance(algorand, verifier.address)

    _release_hash(app_client, funded_agreement, verifier.address)

    assert algo_balance(algorand, app_client.app_address) == app_before - 1_000
    # The verifier paid one transaction fee and nothing more.
    assert algo_balance(algorand, verifier.address) == verifier_before - 1_000


def test_release_hash_rejects_a_non_verifier(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    funded_agreement: int,
    creator: algokit_utils.SigningAccount,
    beneficiary: algokit_utils.SigningAccount,
) -> None:
    for sender in (creator.address, beneficiary.address):
        with pytest.raises(LogicError, match="sender must be the verifier"):
            _release_hash(app_client, funded_agreement, sender)

    record = app_client.state.box.agreements.get_value(funded_agreement)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0


def test_release_hash_rejects_a_wrong_hash(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    funded_agreement: int,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    with pytest.raises(LogicError, match="hash mismatch"):
        _release_hash(
            app_client, funded_agreement, verifier.address, delivered=WRONG_HASH
        )

    record = app_client.state.box.agreements.get_value(funded_agreement)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0


def test_release_hash_rejects_a_passed_deadline(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    create_agreement,
    join_agreement,
    buyer,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
    advance_chain_time,
) -> None:
    agreement_id = create_agreement(
        beneficiary=beneficiary.address,
        verifier=verifier.address,
        deadline=chain_now(algorand) + 60,
    ).abi_return
    join_agreement(agreement_id, buyer)
    advance_chain_time(300)

    with pytest.raises(LogicError, match="deadline passed"):
        _release_hash(app_client, agreement_id, verifier.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0


def test_release_hash_rejects_an_open_agreement(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    hash_agreement: int,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    with pytest.raises(LogicError, match="agreement is not funded"):
        _release_hash(app_client, hash_agreement, verifier.address)

    assert app_client.state.box.agreements.get_value(hash_agreement).total_held == 0
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0


def test_release_hash_cannot_run_twice(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    funded_agreement: int,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    _release_hash(app_client, funded_agreement, verifier.address)
    paid_once = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    with pytest.raises(LogicError, match="agreement is not funded"):
        _release_hash(app_client, funded_agreement, verifier.address)

    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == paid_once
    assert app_client.state.box.agreements.get_value(funded_agreement).total_held == 0


def _release_quorum(app_client, agreement_id, sender):
    return app_client.send.release_quorum(
        args=ReleaseQuorumArgs(agreement_id=agreement_id),
        params=CommonAppCallParams(sender=sender, extra_fee=AlgoAmount(micro_algo=0)),
        send_params=POPULATE,
    )


def test_release_quorum_is_permissionless(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    filled_pool,
    beneficiary: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = filled_pool
    before = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    _release_quorum(app_client, agreement_id, stranger.address)

    after = usdc_balance(algorand, beneficiary.address, usdc_asset_id)
    assert after - before == SHARE_PRICE * 3

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_RELEASED
    assert record.total_held == 0


def test_release_quorum_rejects_a_pool_below_quota(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_pool,
    beneficiary: algokit_utils.SigningAccount,
    creator: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = make_pool(min_seats=3, joins=2)

    with pytest.raises(LogicError, match="agreement is not filled"):
        _release_quorum(app_client, agreement_id, creator.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.total_held == SHARE_PRICE * 2
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0


def test_release_quorum_rejects_a_hash_agreement(
    app_client: EscrowClient,
    funded_agreement: int,
    creator: algokit_utils.SigningAccount,
) -> None:
    """A FUNDED agreement is not releasable by counting seats, whatever its
    seat count.

    `release_quorum` dispatches on state, and FUNDED is not FILLED -- so a
    filled `hash` pool cannot be drained permissionlessly either. See
    test_a_filled_hash_pool_cannot_be_released_permissionlessly.
    """
    with pytest.raises(LogicError, match="agreement is not filled"):
        _release_quorum(app_client, funded_agreement, creator.address)

    record = app_client.state.box.agreements.get_value(funded_agreement)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE


def test_release_hash_rejects_a_filled_pool(
    app_client: EscrowClient,
    filled_pool,
    verifier: algokit_utils.SigningAccount,
) -> None:
    """The pooled path has no verifier, so the hash path must not reach it."""
    agreement_id, _payers = filled_pool

    with pytest.raises(LogicError, match="agreement is not funded"):
        _release_hash(app_client, agreement_id, verifier.address)

    assert (
        app_client.state.box.agreements.get_value(agreement_id).total_held
        == SHARE_PRICE * 3
    )


def test_release_quorum_cannot_run_twice(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    filled_pool,
    beneficiary: algokit_utils.SigningAccount,
    creator: algokit_utils.SigningAccount,
) -> None:
    agreement_id, _payers = filled_pool
    _release_quorum(app_client, agreement_id, creator.address)
    paid_once = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    with pytest.raises(LogicError, match="agreement is not filled"):
        _release_quorum(app_client, agreement_id, creator.address)

    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == paid_once


# --- multi-seat `hash` pools (mode 3) -------------------------------------


def test_release_hash_pays_the_summed_total_in_one_transfer(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_hash_pool,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> None:
    """Five seats at one share price release as one payment of five shares.

    `release_hash` pays `total_held`, which `join` has been accumulating
    across seats since the contract was written. Nothing about the release
    path needed changing for multi-seat -- this test exists to hold that
    claim, so a future edit that starts paying per seat is caught.
    """
    agreement_id, payers = make_hash_pool(min_seats=5)
    before = usdc_balance(algorand, beneficiary.address, usdc_asset_id)

    result = _release_hash(app_client, agreement_id, verifier.address)

    after = usdc_balance(algorand, beneficiary.address, usdc_asset_id)
    assert after - before == SHARE_PRICE * 5
    released = one_event(result, "Released")
    assert released["amount"] == SHARE_PRICE * 5
    assert bytes(released["proof"]) == COMMIT_HASH
    assert len(payers) == 5

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_RELEASED
    assert record.total_held == 0


def test_a_filled_hash_pool_cannot_be_released_permissionlessly(
    algorand: AlgorandClient,
    app_client: EscrowClient,
    usdc_asset_id: int,
    make_hash_pool,
    beneficiary: algokit_utils.SigningAccount,
    stranger: algokit_utils.SigningAccount,
) -> None:
    """The whole reason mode 3 exists.

    On `quorum` the money moves the moment the seats fill, to anyone who
    calls. On `hash` it does not move until bytes matching the commitment are
    presented by the verifier -- so what the buyer is owed on non-delivery is
    a refund, not a promise.
    """
    agreement_id, _payers = make_hash_pool(min_seats=3)

    with pytest.raises(LogicError, match="agreement is not filled"):
        _release_quorum(app_client, agreement_id, stranger.address)

    record = app_client.state.box.agreements.get_value(agreement_id)
    assert record.state == STATE_FUNDED
    assert record.total_held == SHARE_PRICE * 3
    assert usdc_balance(algorand, beneficiary.address, usdc_asset_id) == 0
