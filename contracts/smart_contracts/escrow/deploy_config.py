"""Deploy driver for the escrow application.

Two things have to be true before the resource server answers a single 402:
the application account holds enough ALGO for its own minimum balance, and it
is opted in to the payment asset. A settlement into an account that is not
opted in fails at the facilitator layer, so the opt-in is a deploy-time step
and never a runtime path.

Per-agreement box minimum balance is *not* funded here -- it arrives with the
deposit attached to each `create_agreement`.
"""

import logging
import os

import algokit_utils

logger = logging.getLogger(__name__)

# 0.1 ALGO application base plus 0.1 for the asset opt-in, with working room
# above it. Agreement deposits are paid in per agreement.
APP_FUNDING = algokit_utils.AlgoAmount(algo=1)


def deploy() -> None:
    from smart_contracts.artifacts.escrow.escrow_client import (
        BootstrapArgs,
        EscrowFactory,
        EscrowMethodCallCreateParams,
        OptInAssetArgs,
    )

    algorand = algokit_utils.AlgorandClient.from_environment()
    deployer_ = algorand.account.from_environment("DEPLOYER")

    usdc_asset_id = int(os.environ.get("USDC_ASSET_ID", "0"))
    treasury = os.environ.get("TREASURY_ADDRESS", deployer_.address)

    factory = algorand.client.get_typed_app_factory(
        EscrowFactory, default_sender=deployer_.address
    )

    app_client, result = factory.deploy(
        on_update=algokit_utils.OnUpdate.AppendApp,
        on_schema_break=algokit_utils.OnSchemaBreak.AppendApp,
        create_params=EscrowMethodCallCreateParams(
            args=BootstrapArgs(usdc_asset_id=usdc_asset_id, treasury=treasury),
        ),
    )

    if result.operation_performed in [
        algokit_utils.OperationPerformed.Create,
        algokit_utils.OperationPerformed.Replace,
    ]:
        algorand.send.payment(
            algokit_utils.PaymentParams(
                amount=APP_FUNDING,
                sender=deployer_.address,
                receiver=app_client.app_address,
            )
        )
        logger.info(
            f"Deployed {app_client.app_name} ({app_client.app_id}) "
            f"and funded {app_client.app_address}"
        )

        if usdc_asset_id:
            # One inner transaction; cover its pooled fee from the caller.
            app_client.send.opt_in_asset(
                args=OptInAssetArgs(asset_id=usdc_asset_id),
                params=algokit_utils.CommonAppCallParams(
                    extra_fee=algokit_utils.AlgoAmount(micro_algo=1_000)
                ),
            )
            logger.info(f"Opted the application account in to asset {usdc_asset_id}")
        else:
            logger.warning(
                "USDC_ASSET_ID is unset, so the application account was not "
                "opted in. It cannot receive a settlement until it is."
            )
