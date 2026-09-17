"""LocalNet integration tests for the join_spike stub.

Proves that a 3-transaction group — an unrelated fee-covering self-payment,
an asset transfer paying the app account, and an app call reading that
payment by its absolute group index — is accepted or rejected exactly as
`join`'s assertions describe.
"""

import algokit_utils
import pytest
from algokit_utils import (
    AlgoAmount,
    AlgorandClient,
    AssetCreateParams,
    AssetOptInParams,
    AssetTransferParams,
    CommonAppCallParams,
    LogicError,
    PaymentParams,
    SendParams,
)

from smart_contracts.artifacts.join_spike.join_spike_client import (
    CreateArgs,
    JoinArgs,
    JoinSpikeClient,
    JoinSpikeFactory,
    JoinSpikeMethodCallCreateParams,
    OptInAssetArgs,
)

SHARE_PRICE = 2_000_000  # arbitrary asset-unit amount, matches the app's share_price

# Auto-discover and attach box/asset/account references via simulate,
# instead of computing the payers box name by hand.
_POPULATE_RESOURCES = SendParams(populate_app_call_resources=True)


def _fund(algorand: AlgorandClient, account: algokit_utils.SigningAccount) -> None:
    algorand.account.ensure_funded(
        account.address,
        algorand.account.localnet_dispenser(),
        AlgoAmount(algo=10),
    )


@pytest.fixture(scope="module")
def algorand() -> AlgorandClient:
    return AlgorandClient.default_localnet()


@pytest.fixture(scope="module")
def creator(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    account = algorand.account.random()
    _fund(algorand, account)
    return account


@pytest.fixture(scope="module")
def usdc_asset_id(
    algorand: AlgorandClient, creator: algokit_utils.SigningAccount
) -> int:
    result = algorand.send.asset_create(
        AssetCreateParams(
            sender=creator.address,
            total=1_000_000_000,
            decimals=6,
            asset_name="Test USDC",
            unit_name="tUSDC",
        )
    )
    return result.asset_id


@pytest.fixture(scope="module")
def app_client(
    algorand: AlgorandClient,
    creator: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> JoinSpikeClient:
    factory = algorand.client.get_typed_app_factory(
        JoinSpikeFactory,
        default_sender=creator.address,
    )
    client, _ = factory.deploy(
        on_update=algokit_utils.OnUpdate.AppendApp,
        on_schema_break=algokit_utils.OnSchemaBreak.AppendApp,
        create_params=JoinSpikeMethodCallCreateParams(
            args=CreateArgs(usdc_asset_id=usdc_asset_id, share_price=SHARE_PRICE),
        ),
    )

    # Fund the app account: min balance for the asset opt-in plus box MBR.
    algorand.send.payment(
        PaymentParams(
            sender=creator.address,
            receiver=client.app_address,
            amount=AlgoAmount(algo=1),
        )
    )

    # opt_in_asset issues one inner transaction; cover its pooled fee.
    client.send.opt_in_asset(
        args=OptInAssetArgs(asset_id=usdc_asset_id),
        params=CommonAppCallParams(extra_fee=AlgoAmount(micro_algo=1_000)),
    )

    return client


@pytest.fixture
def buyer(
    algorand: AlgorandClient,
    creator: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> algokit_utils.SigningAccount:
    account = algorand.account.random()
    _fund(algorand, account)
    algorand.send.asset_opt_in(
        AssetOptInParams(sender=account.address, asset_id=usdc_asset_id)
    )
    algorand.send.asset_transfer(
        AssetTransferParams(
            sender=creator.address,
            receiver=account.address,
            asset_id=usdc_asset_id,
            amount=SHARE_PRICE * 10,
        )
    )
    return account


def _join_group(
    algorand: AlgorandClient,
    buyer: algokit_utils.SigningAccount,
    app_client: JoinSpikeClient,
    usdc_asset_id: int,
    *,
    agreement_id: int,
    amount: int = SHARE_PRICE,
    close_asset_to: str | None = None,
    payment_index: int = 1,
):
    """Build the group under test: [feePayer self-payment amt=0, axfer
    buyer->app_account amount=..., appCall join(agreement_id, payment_index)].
    """
    return (
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
                amount=amount,
                close_asset_to=close_asset_to,
            )
        )
        .add_app_call_method_call(
            app_client.params.join(
                args=JoinArgs(agreement_id=agreement_id, payment_index=payment_index),
                params=CommonAppCallParams(sender=buyer.address),
            )
        )
    )


def test_join_success_records_payer(
    algorand: AlgorandClient,
    app_client: JoinSpikeClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> None:
    agreement_id = 1

    _join_group(
        algorand, buyer, app_client, usdc_asset_id, agreement_id=agreement_id
    ).send(_POPULATE_RESOURCES)

    recorded_payer = app_client.state.box.payers.get_value(agreement_id)
    assert recorded_payer == buyer.address


def test_join_rejects_wrong_amount(
    algorand: AlgorandClient,
    app_client: JoinSpikeClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> None:
    agreement_id = 2

    with pytest.raises(LogicError, match="wrong amount"):
        _join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=agreement_id,
            amount=SHARE_PRICE + 1,
        ).send()

    assert agreement_id not in app_client.state.box.payers.get_map()


def test_join_rejects_self_referencing_payment_index(
    algorand: AlgorandClient,
    app_client: JoinSpikeClient,
    buyer: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> None:
    agreement_id = 3

    with pytest.raises(
        LogicError, match="payment_index must not reference the app call itself"
    ):
        # The app call is the 3rd (index 2) transaction in this group;
        # pointing payment_index at it must be rejected before any field
        # is read from the wrong transaction type.
        _join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=agreement_id,
            payment_index=2,
        ).send()

    assert agreement_id not in app_client.state.box.payers.get_map()


def test_join_rejects_nonzero_asset_close_to(
    algorand: AlgorandClient,
    app_client: JoinSpikeClient,
    buyer: algokit_utils.SigningAccount,
    creator: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> None:
    agreement_id = 4

    with pytest.raises(LogicError, match="asset_close_to must be unset"):
        _join_group(
            algorand,
            buyer,
            app_client,
            usdc_asset_id,
            agreement_id=agreement_id,
            close_asset_to=creator.address,
        ).send()

    assert agreement_id not in app_client.state.box.payers.get_map()
