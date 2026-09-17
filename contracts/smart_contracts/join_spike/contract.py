from algopy import (
    Account,
    ARC4Contract,
    BoxMap,
    Global,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
)


class Joined(arc4.Struct):
    """ARC-28 event emitted when a payer successfully joins an agreement."""

    agreement_id: arc4.UInt64
    payer: arc4.Address
    payment_txn_id: arc4.DynamicBytes


class JoinSpike(ARC4Contract):
    """Spike stub proving that an atomic group carrying an extra buyer-signed
    application call, alongside a standard asset-transfer payment, is
    internally well-formed and acceptable to the AVM.

    Deliberately absent: roster management, quorum/release/refund logic, and
    `record_join`. See README.md for what this stub is and is not.
    """

    def __init__(self) -> None:
        self.admin = Account()
        self.usdc_asset_id = UInt64()
        self.share_price = UInt64()
        self.payers = BoxMap(UInt64, Account, key_prefix="p_")

    @arc4.abimethod(create="require")
    def create(self, usdc_asset_id: UInt64, share_price: UInt64) -> None:
        self.admin = Txn.sender
        self.usdc_asset_id = usdc_asset_id
        self.share_price = share_price

    @arc4.abimethod
    def opt_in_asset(self, asset_id: UInt64) -> None:
        assert Txn.sender == self.admin, "sender must be admin"
        itxn.AssetTransfer(
            xfer_asset=asset_id,
            asset_receiver=Global.current_application_address,
            asset_amount=0,
            fee=0,
        ).submit()

    @arc4.abimethod
    def join(self, agreement_id: UInt64, payment_index: UInt64) -> None:
        # Checked before dereferencing the referenced transaction so a
        # self-referencing index fails on this assertion specifically,
        # rather than on a field read from the wrong transaction type.
        assert payment_index != Txn.group_index, (
            "payment_index must not reference the app call itself"
        )

        payment = gtxn.AssetTransferTransaction(payment_index)
        assert payment.xfer_asset.id == self.usdc_asset_id, "wrong asset"
        assert payment.asset_receiver == Global.current_application_address, (
            "payment must be made to this app account"
        )
        assert payment.sender == Txn.sender, (
            "referenced payment sender must match the caller"
        )
        assert payment.asset_amount == self.share_price, "wrong amount"
        assert payment.asset_close_to == Global.zero_address, (
            "asset_close_to must be unset"
        )
        assert payment.rekey_to == Global.zero_address, "rekey_to must be unset"

        self.payers[agreement_id] = Txn.sender

        arc4.emit(
            Joined(
                agreement_id=arc4.UInt64(agreement_id),
                payer=arc4.Address(Txn.sender),
                payment_txn_id=arc4.DynamicBytes(payment.txn_id),
            )
        )
