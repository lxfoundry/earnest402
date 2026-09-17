"""Earnest escrow application.

One application, one record type. Every escrow is an agreement: an id, a
release condition, a deadline, a beneficiary and a roster of payers.
Seat count and condition are independent: single-payer delivery escrow is
`max_seats = 1` with condition `hash`, a pool funding unspecified work is
`min_seats > 1` with condition `quorum`, and a pool funding an
already-committed deliverable is `min_seats > 1` with condition `hash`.

See docs/specs/escrow-contract.md for the full specification.
"""

import typing

from algopy import (
    Account,
    ARC4Contract,
    Asset,
    BoxMap,
    Bytes,
    Global,
    GlobalState,
    TransactionType,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    op,
    subroutine,
    urange,
)

# Release conditions.
CONDITION_HASH = 0
CONDITION_QUORUM = 1

# States. CLOSED is not stored -- `close` deletes the boxes.
STATE_OPEN = 0
STATE_FUNDED = 1
STATE_FILLED = 2
STATE_RELEASED = 3
STATE_EXPIRED = 4
STATE_REFUNDING = 5
STATE_REFUNDED = 6

# v1 parameters. The seat ceiling is measured, not chosen: `join` costs more
# with every seat already taken, and past 26 it exceeds the per-call opcode
# budget. 20 is that wall with headroom for future guards in the same method.
MAX_SEATS_CEILING = 20
REFUND_BATCH_CEILING = 4

# Storage arithmetic. A roster seat is a 32-byte address plus an 8-byte
# amount; the box key is one prefix byte plus an 8-byte id.
SEAT_BYTES = 40
BOX_FLAT_MBR = 2_500
BOX_BYTE_MBR = 400
BOX_KEY_BYTES = 9
AGREEMENT_BYTES = 164
AGREEMENT_BOX_MBR = BOX_FLAT_MBR + BOX_BYTE_MBR * (BOX_KEY_BYTES + AGREEMENT_BYTES)

HASH_BYTES = 32

Hash32: typing.TypeAlias = arc4.StaticArray[arc4.Byte, typing.Literal[32]]


class Agreement(arc4.Struct):
    """The agreement record, 164 bytes of fixed-width ARC-4 fields."""

    condition: arc4.UInt8
    state: arc4.UInt8
    deadline: arc4.UInt64
    share_price: arc4.UInt64
    min_seats: arc4.UInt16
    max_seats: arc4.UInt16
    seats: arc4.UInt16
    refund_cursor: arc4.UInt16
    unclaimed_seats: arc4.UInt16
    total_held: arc4.UInt64
    commit_hash: Hash32
    beneficiary: arc4.Address
    verifier: arc4.Address
    creator: arc4.Address


class AgreementCreated(arc4.Struct):
    agreement_id: arc4.UInt64
    condition: arc4.UInt8
    deadline: arc4.UInt64
    share_price: arc4.UInt64
    min_seats: arc4.UInt16
    max_seats: arc4.UInt16
    beneficiary: arc4.Address
    verifier: arc4.Address


class Joined(arc4.Struct):
    agreement_id: arc4.UInt64
    payer: arc4.Address
    amount: arc4.UInt64
    seat: arc4.UInt16
    payment_txn_id: arc4.DynamicBytes
    provenance: arc4.UInt8  # 0 = atomic join


class Filled(arc4.Struct):
    agreement_id: arc4.UInt64
    seats: arc4.UInt16


class Released(arc4.Struct):
    agreement_id: arc4.UInt64
    beneficiary: arc4.Address
    amount: arc4.UInt64
    proof: arc4.DynamicBytes


class Expired(arc4.Struct):
    agreement_id: arc4.UInt64
    seats: arc4.UInt16
    reason: arc4.UInt8  # 0 = deadline, 1 = beneficiary stranded


class Refunded(arc4.Struct):
    agreement_id: arc4.UInt64
    payer: arc4.Address
    amount: arc4.UInt64
    seat: arc4.UInt16


class RefundSkipped(arc4.Struct):
    agreement_id: arc4.UInt64
    payer: arc4.Address
    amount: arc4.UInt64
    seat: arc4.UInt16


class RefundComplete(arc4.Struct):
    agreement_id: arc4.UInt64
    paid: arc4.UInt16
    skipped: arc4.UInt16


class RefundClaimed(arc4.Struct):
    agreement_id: arc4.UInt64
    payer: arc4.Address
    amount: arc4.UInt64
    seat: arc4.UInt16


class Closed(arc4.Struct):
    agreement_id: arc4.UInt64
    algo_returned: arc4.UInt64


class Escrow(ARC4Contract):
    def __init__(self) -> None:
        self.admin = GlobalState(Account())
        # Configuration only: read off-chain as the conventional beneficiary
        # for `hash` agreements. No fund-moving path in this contract reads it.
        self.treasury = GlobalState(Account())
        self.usdc_asset_id = GlobalState(UInt64(0))
        self.agreement_count = GlobalState(UInt64(0))
        self.paused = GlobalState(UInt64(0))
        self.agreements = BoxMap(UInt64, Agreement, key_prefix="a")

    @arc4.abimethod(create="require")
    def bootstrap(self, usdc_asset_id: UInt64, treasury: Account) -> None:
        """The constructor. Not named `deploy`: the typed-client generator
        already owns that name on the factory and renames the method to
        `deploy1`."""
        self.admin.value = Txn.sender
        self.treasury.value = treasury
        self.usdc_asset_id.value = usdc_asset_id

    @arc4.abimethod
    def opt_in_asset(self, asset_id: UInt64) -> None:
        assert Txn.sender == self.admin.value, "sender must be admin"
        itxn.AssetTransfer(
            xfer_asset=asset_id,
            asset_receiver=Global.current_application_address,
            asset_amount=0,
            fee=0,
        ).submit()

    @subroutine
    def _roster_key(self, agreement_id: UInt64) -> Bytes:
        """The roster box name: one prefix byte plus the big-endian id."""
        return Bytes(b"r") + op.itob(agreement_id)

    @subroutine
    def _box_mbr(self, max_seats: UInt64) -> UInt64:
        """Minimum balance locked by the agreement box plus the roster box."""
        roster_mbr = UInt64(BOX_FLAT_MBR) + BOX_BYTE_MBR * (
            BOX_KEY_BYTES + SEAT_BYTES * max_seats
        )
        return UInt64(AGREEMENT_BOX_MBR) + roster_mbr

    @subroutine
    def _fee_reserve(self, max_seats: UInt64) -> UInt64:
        """The inner-transaction reserve: (max_seats + 1) transactions.

        Release and refund are mutually exclusive and each seat is paid at
        most once, so the ceiling per agreement is one payment per seat plus
        one -- and that spare covers the `close` payment itself.

        Priced at the live protocol minimum (0.001 ALGO today) rather than a
        constant, because that is what `_pay` and `close` actually charge the
        application account. A hardcoded price would let the two drift apart
        if the protocol parameter ever moved, and the drift that matters is
        upward: an under-funded reserve makes `close` return more than the
        agreement has left, which draws on another agreement's deposit and
        breaks ALGO conservation (spec 7).
        """
        return (max_seats + UInt64(1)) * Global.min_txn_fee

    @subroutine
    def _required_deposit(self, max_seats: UInt64) -> UInt64:
        """Box minimum balance for both boxes plus the inner-transaction reserve."""
        return self._box_mbr(max_seats) + self._fee_reserve(max_seats)

    @subroutine
    def _can_receive(self, account: Account, asset: Asset) -> bool:
        """Whether an inner transfer of `asset` into `account` can succeed.

        Opt-in is only half of it: a frozen holding is an opted-in holding and
        still rejects every transfer. Both halves have to hold, because an
        inner-transaction failure aborts the entire group -- which is what
        turns one unreachable payer into a stuck refund cursor (spec 5.4) and
        one unreachable beneficiary into an agreement with no exit (spec 2.2).
        USDC on MainNet declares a freeze address, so the frozen half is
        reachable in production and not a theoretical case.
        """
        _balance, opted_in = op.AssetHoldingGet.asset_balance(account, asset)
        frozen, _exists = op.AssetHoldingGet.asset_frozen(account, asset)
        return opted_in and not frozen

    @arc4.abimethod
    def create_agreement(
        self,
        condition: UInt64,
        share_price: UInt64,
        min_seats: UInt64,
        max_seats: UInt64,
        deadline: UInt64,
        commit_hash: Bytes,
        beneficiary: Account,
        verifier: Account,
        mbr_payment: gtxn.PaymentTransaction,
    ) -> UInt64:
        assert Txn.sender == self.admin.value, "sender must be admin"
        assert self.paused.value == 0, "contract is paused"

        # Shape-independent validity.
        assert deadline > Global.latest_timestamp, "deadline must be in the future"
        assert share_price > 0, "share_price must be positive"
        assert beneficiary != Global.zero_address, "beneficiary must be set"
        assert self._can_receive(beneficiary, Asset(self.usdc_asset_id.value)), (
            "beneficiary must be able to receive USDC"
        )
        assert commit_hash.length == HASH_BYTES, "commit_hash must be 32 bytes"

        zero_hash = op.bzero(HASH_BYTES)

        # Per-condition validity.
        if condition == UInt64(CONDITION_HASH):
            # Seat count and condition are independent. A single-seat `hash`
            # agreement is the delivery escrow; a multi-seat one is a pool
            # whose release condition is the deliverable rather than the
            # payer count, which is the only shape that can hold N buyers'
            # money against a specific artifact.
            #
            # Fixed seat count, same reason as `quorum` (spec 4): a payer past
            # the quota would own `1/seats` of something they priced at
            # `1/min_seats`, and slack seats are unreachable in any case.
            #
            # The lower bound is explicit because nothing else supplies one:
            # `min_seats == 1` used to carry it, and `quorum`'s own floor is
            # in the other branch. A zero-seat agreement would be inert --
            # `join` asserts `seats < max_seats` -- and its creator could
            # reclaim it (spec 2.3), but it is still a row no payer can reach
            # parking a deposit, and the validity table (spec 5.1) excludes it.
            assert min_seats >= 1, "min_seats must be at least 1"
            assert max_seats == min_seats, "max_seats must equal min_seats"
            assert max_seats <= MAX_SEATS_CEILING, "max_seats exceeds the ceiling"
            assert commit_hash != zero_hash, "hash requires a commit_hash"
            assert verifier != Global.zero_address, "hash requires a verifier"
        else:
            assert condition == UInt64(CONDITION_QUORUM), "unknown condition"
            assert min_seats > 1, "quorum requires min_seats > 1"
            # Fixed deliverable, fixed price: the supplier is owed
            # `share_price * min_seats`, so a seat past the quota buys nothing.
            # Slack is unreachable in any case -- reaching `min_seats` leaves
            # OPEN and `join` only accepts OPEN -- so it would lock roster
            # minimum balance for storage no payer can ever use (spec 4).
            assert max_seats == min_seats, "max_seats must equal min_seats"
            assert max_seats <= MAX_SEATS_CEILING, "max_seats exceeds the ceiling"
            assert commit_hash == zero_hash, "quorum must not carry a commit_hash"
            assert verifier == Global.zero_address, "quorum release is permissionless"

        # Creation deposit: both boxes' minimum balance plus the fee reserve.
        assert mbr_payment.receiver == Global.current_application_address, (
            "deposit must pay this application"
        )
        assert mbr_payment.sender == Txn.sender, "deposit must come from the caller"
        # Exact, not a floor. The amount is deterministic from `max_seats`,
        # and `close` returns the box minimum balance plus the *unspent*
        # reserve -- a computed figure, not whatever was sent. Anything over
        # the required deposit would therefore have no path out: no method
        # sweeps the application account and the application has no delete
        # handler. Rejecting an overpayment costs the admin a retry; accepting
        # one locks the excess for good.
        assert mbr_payment.amount == self._required_deposit(max_seats), (
            "deposit must equal box MBR plus the fee reserve"
        )

        agreement_id = self.agreement_count.value + UInt64(1)
        self.agreement_count.value = agreement_id

        self.agreements[agreement_id] = Agreement(
            condition=arc4.UInt8(condition),
            state=arc4.UInt8(STATE_OPEN),
            deadline=arc4.UInt64(deadline),
            share_price=arc4.UInt64(share_price),
            min_seats=arc4.UInt16(min_seats),
            max_seats=arc4.UInt16(max_seats),
            seats=arc4.UInt16(0),
            refund_cursor=arc4.UInt16(0),
            unclaimed_seats=arc4.UInt16(0),
            total_held=arc4.UInt64(0),
            commit_hash=Hash32.from_bytes(commit_hash),
            beneficiary=arc4.Address(beneficiary),
            verifier=arc4.Address(verifier),
            creator=arc4.Address(Txn.sender),
        )

        # Pre-size the roster so no join can ever fail for want of minimum
        # balance. Every seat's storage exists before the first payment.
        assert op.Box.create(
            self._roster_key(agreement_id), UInt64(SEAT_BYTES) * max_seats
        ), "roster box already exists"

        arc4.emit(
            AgreementCreated(
                agreement_id=arc4.UInt64(agreement_id),
                condition=arc4.UInt8(condition),
                deadline=arc4.UInt64(deadline),
                share_price=arc4.UInt64(share_price),
                min_seats=arc4.UInt16(min_seats),
                max_seats=arc4.UInt16(max_seats),
                beneficiary=arc4.Address(beneficiary),
                verifier=arc4.Address(verifier),
            )
        )
        return agreement_id

    @subroutine
    def _seat_payer(self, agreement_id: UInt64, seat: UInt64) -> Account:
        raw = op.Box.extract(
            self._roster_key(agreement_id), seat * SEAT_BYTES, UInt64(32)
        )
        return Account.from_bytes(raw)

    @subroutine
    def _seat_amount(self, agreement_id: UInt64, seat: UInt64) -> UInt64:
        raw = op.Box.extract(
            self._roster_key(agreement_id), seat * SEAT_BYTES + UInt64(32), UInt64(8)
        )
        return op.btoi(raw)

    @subroutine
    def _write_seat(
        self, agreement_id: UInt64, seat: UInt64, payer: Account, amount: UInt64
    ) -> None:
        op.Box.replace(
            self._roster_key(agreement_id),
            seat * SEAT_BYTES,
            payer.bytes + op.itob(amount),
        )

    @subroutine
    def _zero_seat_amount(self, agreement_id: UInt64, seat: UInt64) -> None:
        """Zero an entry's amount. This is the paid marker (spec 5.4)."""
        op.Box.replace(
            self._roster_key(agreement_id),
            seat * SEAT_BYTES + UInt64(32),
            op.itob(UInt64(0)),
        )

    @arc4.abimethod
    def join(self, agreement_id: UInt64, payment_index: UInt64) -> None:
        assert self.paused.value == 0, "contract is paused"

        # Checked before dereferencing, so a self-referencing index fails on
        # this assertion rather than on a field read from the wrong txn type.
        assert payment_index != Txn.group_index, (
            "payment_index must not reference the app call itself"
        )

        agreement = self.agreements[agreement_id].copy()
        assert agreement.state == arc4.UInt8(STATE_OPEN), "agreement is not open"
        assert Global.latest_timestamp < agreement.deadline.native, "deadline passed"

        seats = agreement.seats.native
        assert seats < agreement.max_seats.native, "agreement is full"

        payment = gtxn.AssetTransferTransaction(payment_index)
        assert payment.xfer_asset.id == self.usdc_asset_id.value, "wrong asset"
        assert payment.asset_receiver == Global.current_application_address, (
            "payment must be made to this app account"
        )
        assert payment.sender == Txn.sender, (
            "referenced payment sender must match the caller"
        )
        assert payment.asset_amount == agreement.share_price.native, "wrong amount"
        assert payment.asset_close_to == Global.zero_address, (
            "asset_close_to must be unset"
        )
        assert payment.rekey_to == Global.zero_address, "rekey_to must be unset"

        # One transfer funds one seat. The sender check above pins the payment
        # to this caller, but one caller can sign two joins in the same group
        # and point both at the same transfer -- same sender, same amount
        # whenever the two agreements are priced alike, and neither roster
        # sees the other. Conservation (spec 7) needs the group checked too.
        #
        # Only earlier transactions are scanned. A duplicated reference always
        # has a later member, and rejecting that one rejects the whole group,
        # so scanning backwards is sufficient as well as cheaper: an ordinary
        # settlement group puts `join` last and walks two non-app-call legs.
        # It also means every call read here has already been evaluated, so
        # its arguments are known to have decoded as the ABI types.
        #
        # The argument count is a lower bound, not an equality. Nothing on
        # chain enforces an ARC-4 count: the router dispatches on the selector
        # and reads the declared arguments by index, so a call padded with a
        # fourth argument is an ordinary join that takes an ordinary seat.
        # Keying on an exact count would let eight bytes of padding hide such
        # a join from this scan. Three is what the two reads below need.
        for index in urange(Txn.group_index):
            if gtxn.Transaction(index).type == TransactionType.ApplicationCall:
                call = gtxn.ApplicationCallTransaction(index)
                if (
                    call.app_id.id == Global.current_application_id.id
                    and call.num_app_args >= 3
                    and call.app_args(0)
                    == arc4.arc4_signature("join(uint64,uint64)void")
                ):
                    assert op.btoi(call.app_args(2)) != payment_index, (
                        "payment already referenced by another join"
                    )

        # A payer appears on a roster at most once: `quorum`'s public claim is
        # N *distinct* payers. The scan walks occupied seats only.
        for seat in urange(seats):
            assert self._seat_payer(agreement_id, seat) != Txn.sender, (
                "payer already joined this agreement"
            )

        self._write_seat(agreement_id, seats, Txn.sender, payment.asset_amount)

        new_seats = seats + UInt64(1)
        agreement.seats = arc4.UInt16(new_seats)
        agreement.total_held = arc4.UInt64(
            agreement.total_held.native + payment.asset_amount
        )

        # `Filled` means a seat quota was met. It is guarded on the quota
        # itself rather than on the condition: a single-seat agreement's
        # funding join satisfies the same seat arithmetic while transitioning
        # to FUNDED, and emitting on the arithmetic alone would put a
        # pool-fill receipt on a delivery escrow's spine, which an indexer
        # reads as a quota being met (spec 8). Guarding on
        # `condition == quorum` was a proxy for that concern which held only
        # while `hash` was single-seat.
        #
        # The terminal state below still dispatches on the condition, which
        # is the one thing that genuinely differs: FILLED releases on the
        # seat count, FUNDED only against delivered bytes.
        is_quorum = agreement.condition == arc4.UInt8(CONDITION_QUORUM)
        filled = new_seats >= agreement.min_seats.native
        if filled:
            if is_quorum:
                agreement.state = arc4.UInt8(STATE_FILLED)
            else:
                agreement.state = arc4.UInt8(STATE_FUNDED)

        self.agreements[agreement_id] = agreement.copy()

        arc4.emit(
            Joined(
                agreement_id=arc4.UInt64(agreement_id),
                payer=arc4.Address(Txn.sender),
                amount=arc4.UInt64(payment.asset_amount),
                seat=arc4.UInt16(seats),
                payment_txn_id=arc4.DynamicBytes(payment.txn_id),
                provenance=arc4.UInt8(0),
            )
        )
        if filled and agreement.min_seats.native > UInt64(1):
            arc4.emit(
                Filled(
                    agreement_id=arc4.UInt64(agreement_id),
                    seats=arc4.UInt16(new_seats),
                )
            )

    @subroutine
    def _pay(self, receiver: Account, amount: UInt64) -> None:
        """Inner USDC transfer, fee paid from the reserve funded at creation.

        The fee is set explicitly rather than left at 0. A zero-fee inner
        transaction is covered by surplus fee credit from the caller's own
        transactions, which would make every release and refund cost whoever
        triggered it; naming the minimum fee charges it to the application
        account instead, which is what the deposit is for (spec 3.4).
        """
        itxn.AssetTransfer(
            xfer_asset=self.usdc_asset_id.value,
            asset_receiver=receiver,
            asset_amount=amount,
            fee=Global.min_txn_fee,
        ).submit()

    @arc4.abimethod
    def release_hash(self, agreement_id: UInt64, delivered_bytes_hash: Bytes) -> None:
        agreement = self.agreements[agreement_id].copy()
        assert agreement.state == arc4.UInt8(STATE_FUNDED), "agreement is not funded"
        assert Txn.sender == agreement.verifier.native, "sender must be the verifier"
        # Guards against a late release racing a refund.
        assert Global.latest_timestamp < agreement.deadline.native, "deadline passed"
        # A record, not a proof: the chain compares two 32-byte values and the
        # real verification happens off-chain. See spec 5.2.
        assert delivered_bytes_hash == agreement.commit_hash.bytes, "hash mismatch"

        amount = agreement.total_held.native
        self._pay(agreement.beneficiary.native, amount)

        agreement.total_held = arc4.UInt64(0)
        agreement.state = arc4.UInt8(STATE_RELEASED)
        self.agreements[agreement_id] = agreement.copy()

        arc4.emit(
            Released(
                agreement_id=arc4.UInt64(agreement_id),
                beneficiary=agreement.beneficiary,
                amount=arc4.UInt64(amount),
                proof=arc4.DynamicBytes(delivered_bytes_hash),
            )
        )

    @arc4.abimethod
    def release_quorum(self, agreement_id: UInt64) -> None:
        """Permissionless: the contract counted the payments itself.

        No deadline check. Once `min_seats` is reached the condition is
        permanently met -- there is no leave and no withdraw -- so the fact
        cannot un-become true and there is nothing for a clock to invalidate.
        """
        agreement = self.agreements[agreement_id].copy()
        assert agreement.state == arc4.UInt8(STATE_FILLED), "agreement is not filled"

        amount = agreement.total_held.native
        self._pay(agreement.beneficiary.native, amount)

        agreement.total_held = arc4.UInt64(0)
        agreement.state = arc4.UInt8(STATE_RELEASED)
        self.agreements[agreement_id] = agreement.copy()

        arc4.emit(
            Released(
                agreement_id=arc4.UInt64(agreement_id),
                beneficiary=agreement.beneficiary,
                amount=arc4.UInt64(amount),
                proof=arc4.DynamicBytes(b""),
            )
        )

    @arc4.abimethod
    def expire(self, agreement_id: UInt64) -> None:
        agreement = self.agreements[agreement_id].copy()

        # An agreement nobody has paid into is the creator's to reclaim at
        # any time. `seats == 0` means there is no payer to protect, so the
        # only party affected is the creator, taking back their own deposit.
        # This is what lets a funding window be operator policy instead of a
        # second clock on the record: an abandoned quote stops holding
        # minimum balance as soon as it is recognised as abandoned.
        #
        # Creator-only on purpose. A permissionless early exit would let a
        # stranger cancel a quote out from under a buyer who is already
        # building a settlement group. Past the deadline nothing changes:
        # `expire` stays permissionless, which is what keeps refunds
        # reachable when the operator is gone.
        unfunded_reclaim = (
            agreement.seats.native == UInt64(0)
            and Txn.sender == agreement.creator.native
        )
        if not unfunded_reclaim:
            assert Global.latest_timestamp >= agreement.deadline.native, (
                "deadline has not passed"
            )

        state = agreement.state.native
        reason = UInt64(0)

        if state == UInt64(STATE_FILLED):
            # The stranded-beneficiary rescue. A FILLED agreement never
            # expires on the clock alone -- otherwise `release_quorum` and
            # `expire` would both be callable and the outcome would depend on
            # transaction ordering. It expires only when the beneficiary can
            # no longer receive, which is mutually exclusive with a release
            # succeeding, so exactly one of the two can pass at any moment.
            assert not self._can_receive(
                agreement.beneficiary.native, Asset(self.usdc_asset_id.value)
            ), "beneficiary can still receive; release_quorum applies"
            reason = UInt64(1)
        else:
            assert state == UInt64(STATE_OPEN) or state == UInt64(STATE_FUNDED), (
                "agreement cannot expire from this state"
            )

        agreement.state = arc4.UInt8(STATE_EXPIRED)
        self.agreements[agreement_id] = agreement.copy()

        arc4.emit(
            Expired(
                agreement_id=arc4.UInt64(agreement_id),
                seats=agreement.seats,
                reason=arc4.UInt8(reason),
            )
        )

    @arc4.abimethod
    def refund_next(self, agreement_id: UInt64, count: UInt64) -> None:
        """Pay or skip the next `count` roster entries, FIFO by seat.

        The caller picks only how many, never which: seat index is arrival
        order and `refund_cursor` is a pointer into it. `count` caps at 4
        because every account an inner transfer pays into must be in this
        call's accounts array (max 4), and the account and asset must appear
        in the same transaction for the AVM to resolve the holding.
        """
        assert count > 0, "count must be positive"
        assert count <= REFUND_BATCH_CEILING, "count exceeds the per-call ceiling"

        agreement = self.agreements[agreement_id].copy()
        state = agreement.state.native
        assert state == UInt64(STATE_EXPIRED) or state == UInt64(STATE_REFUNDING), (
            "agreement is not refunding"
        )

        cursor = agreement.refund_cursor.native
        seats = agreement.seats.native
        last = cursor + count
        if last > seats:
            last = seats

        usdc = Asset(self.usdc_asset_id.value)
        total_held = agreement.total_held.native
        unclaimed = agreement.unclaimed_seats.native

        for seat in urange(cursor, last):
            amount = self._seat_amount(agreement_id, seat)
            if amount > 0:
                payer = self._seat_payer(agreement_id, seat)
                if self._can_receive(payer, usdc):
                    self._pay(payer, amount)
                    self._zero_seat_amount(agreement_id, seat)
                    total_held = total_held - amount
                    arc4.emit(
                        Refunded(
                            agreement_id=arc4.UInt64(agreement_id),
                            payer=arc4.Address(payer),
                            amount=arc4.UInt64(amount),
                            seat=arc4.UInt16(seat),
                        )
                    )
                else:
                    # Leave the amount intact so `claim_refund` can still pay
                    # it. Without this check one dead wallet would abort every
                    # call that reaches its seat, freezing every seat behind it.
                    unclaimed = unclaimed + UInt64(1)
                    arc4.emit(
                        RefundSkipped(
                            agreement_id=arc4.UInt64(agreement_id),
                            payer=arc4.Address(payer),
                            amount=arc4.UInt64(amount),
                            seat=arc4.UInt16(seat),
                        )
                    )

        agreement.refund_cursor = arc4.UInt16(last)
        agreement.total_held = arc4.UInt64(total_held)
        agreement.unclaimed_seats = arc4.UInt16(unclaimed)

        if last >= seats:
            agreement.state = arc4.UInt8(STATE_REFUNDED)
        else:
            agreement.state = arc4.UInt8(STATE_REFUNDING)

        self.agreements[agreement_id] = agreement.copy()

        if last >= seats:
            # Totals for the whole sweep, not this call's tally. A pool past
            # `REFUND_BATCH_CEILING` seats clears over several `refund_next`
            # calls, so per-call counters would report the last batch alone and
            # undercount every batch before it -- and `RefundComplete` is the
            # terminal receipt an indexer reads for the agreement, not for the
            # call (spec 8).
            #
            # Derived from the record rather than accumulated across calls,
            # because `unclaimed_seats` is already the exact count of seats
            # still owed at this instant: `share_price > 0` makes every seat
            # funded, paying one zeroes its amount, skipping one increments
            # `unclaimed_seats`, and `claim_refund` -- which may run between
            # batches, and would defeat any counter this method accumulated
            # itself -- reverses that increment as it settles the seat. So
            # `seats - unclaimed` counts the seats settled by either path and
            # `paid + skipped == seats` holds.
            arc4.emit(
                RefundComplete(
                    agreement_id=arc4.UInt64(agreement_id),
                    paid=arc4.UInt16(seats - unclaimed),
                    skipped=arc4.UInt16(unclaimed),
                )
            )

    @arc4.abimethod
    def claim_refund(self, agreement_id: UInt64, seat: UInt64) -> None:
        """Pay one seat that `refund_next` had to skip.

        Permissionless and callable in any post-expiry state including
        REFUNDED, which is why REFUNDED means "the cursor finished its pass",
        not "everyone has their money".
        """
        agreement = self.agreements[agreement_id].copy()
        state = agreement.state.native
        assert (
            state == UInt64(STATE_EXPIRED)
            or state == UInt64(STATE_REFUNDING)
            or state == UInt64(STATE_REFUNDED)
        ), "agreement is not in a refunding state"
        assert seat < agreement.seats.native, "seat is out of range"
        # A skipped seat is exactly one the cursor has already passed while
        # leaving its amount intact. Seats ahead of the cursor still belong to
        # `refund_next`, and accepting one here would decrement
        # `unclaimed_seats` for a seat that was never skipped -- cancelling out
        # a real skip and stranding that payer permanently (spec 5.4).
        assert seat < agreement.refund_cursor.native, "seat has not been skipped yet"

        amount = self._seat_amount(agreement_id, seat)
        assert amount > 0, "seat is already settled"

        payer = self._seat_payer(agreement_id, seat)
        self._pay(payer, amount)
        self._zero_seat_amount(agreement_id, seat)

        agreement.total_held = arc4.UInt64(agreement.total_held.native - amount)
        agreement.unclaimed_seats = arc4.UInt16(
            agreement.unclaimed_seats.native - UInt64(1)
        )
        self.agreements[agreement_id] = agreement.copy()

        arc4.emit(
            RefundClaimed(
                agreement_id=arc4.UInt64(agreement_id),
                payer=arc4.Address(payer),
                amount=arc4.UInt64(amount),
                seat=arc4.UInt16(seat),
            )
        )

    @arc4.abimethod
    def close(self, agreement_id: UInt64) -> None:
        """Delete the boxes and return the unspent deposit to `creator`.

        Guarded on `unclaimed_seats == 0` so a roster is never deleted while
        anyone is still owed money -- this is what makes "nobody has called
        refund_next yet" an inconvenience rather than a loss.

        What comes back is the box minimum balance plus the *unspent* part of
        the fee reserve, never the whole deposit: the inner transfers this
        agreement made were charged to it. A RELEASED agreement made exactly
        one, a REFUNDED one exactly `seats` -- every occupied seat is paid
        once, whether by `refund_next` or by `claim_refund`, and the
        `unclaimed_seats` and `total_held` guards above are what make that
        count exact. One more covers the payment this method is about to
        send, which is why the reserve is sized `max_seats + 1`.
        """
        agreement = self.agreements[agreement_id].copy()
        state = agreement.state.native
        assert state == UInt64(STATE_RELEASED) or state == UInt64(STATE_REFUNDED), (
            "agreement is not in a terminal state"
        )
        assert agreement.unclaimed_seats == arc4.UInt16(0), "seats are still unclaimed"
        assert agreement.total_held == arc4.UInt64(0), "agreement still holds USDC"

        max_seats = agreement.max_seats.native
        inner_used = UInt64(1)  # the payment this call is about to make
        if state == UInt64(STATE_RELEASED):
            inner_used = inner_used + UInt64(1)
        else:
            inner_used = inner_used + agreement.seats.native

        reserve = self._fee_reserve(max_seats)
        spent = inner_used * Global.min_txn_fee
        unspent = UInt64(0)
        if reserve > spent:
            unspent = reserve - spent
        refund = self._box_mbr(max_seats) + unspent

        del self.agreements[agreement_id]
        assert op.Box.delete(self._roster_key(agreement_id)), "roster box missing"

        itxn.Payment(
            receiver=agreement.creator.native,
            amount=refund,
            fee=Global.min_txn_fee,
        ).submit()

        arc4.emit(
            Closed(
                agreement_id=arc4.UInt64(agreement_id),
                algo_returned=arc4.UInt64(refund),
            )
        )

    @arc4.abimethod
    def set_admin(self, new_admin: Account) -> None:
        assert Txn.sender == self.admin.value, "sender must be admin"
        assert new_admin != Global.zero_address, "admin must be set"
        self.admin.value = new_admin

    @arc4.abimethod
    def set_paused(self, paused: UInt64) -> None:
        """Pause new agreements and joins. Never pauses a refund or a close.

        The brake is on taking new money, never on returning it: `expire`,
        `refund_next`, `claim_refund`, `close` and both release paths carry no
        pause guard, so no operator can strand a payer's refund.
        """
        assert Txn.sender == self.admin.value, "sender must be admin"
        self.paused.value = paused
