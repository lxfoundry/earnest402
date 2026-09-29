"""The escrow application as seen from off chain.

Raw algosdk on purpose. The generated typed client sits next to the compiled
artifacts and declares `requires: algokit-utils@^3.0.0`, a dependency of the
*other* dependency surface; importing it here would merge two environments that
are deliberately separate. The ABI is therefore restated below as signature
strings, and `tests/test_escrow_client.py` pins that restatement against the
committed ARC-56 spec -- read as a file, which is a path dependency rather than
an import -- so it cannot drift unnoticed.

Every fund-moving call the backend makes goes through this module.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from algosdk import abi, encoding, transaction
from algosdk.atomic_transaction_composer import (
    AtomicTransactionComposer,
    TransactionSigner,
    TransactionWithSigner,
)
from algosdk.error import AlgodHTTPError

# Release conditions, mirroring the contract's discriminator.
CONDITION_HASH = 0
CONDITION_QUORUM = 1

# States. There is no CLOSED: `close` deletes the boxes.
STATE_OPEN = 0
STATE_FUNDED = 1
STATE_FILLED = 2
STATE_RELEASED = 3
STATE_EXPIRED = 4
STATE_REFUNDING = 5
STATE_REFUNDED = 6

# Box minimum-balance parameters, mirroring the contract's own constants.
BOX_FLAT_MBR = 2_500
BOX_BYTE_MBR = 400
BOX_KEY_BYTES = 9
AGREEMENT_BYTES = 164
SEAT_BYTES = 40

# The per-call refund ceiling. Every account an inner transfer pays into has to
# be in that call's accounts array, and that array holds four.
REFUND_BATCH_CEILING = 4

# How long a read may wait on algod before it is abandoned. The SDK's own
# default is 30 seconds, which is right for a CLI and far too long for a
# route: `POST /index` is unauthenticated and reads the chain on every
# request, so a stalled node holds one worker thread per request for that
# whole time, and enough concurrent requests leave the process with no
# threads for anything else -- `/healthz` included. A box read against a
# healthy node answers in well under a second, so this leaves several times
# the headroom it needs and is still short enough that a stall drains rather
# than accumulates. Applied to reads only; a write waits on confirmation and has
# its own horizon.
READ_TIMEOUT_SECONDS = 3

# The protocol's ceiling on a transaction's note field, in bytes. algosdk does
# not enforce it, so a longer note is found out only when the node refuses the
# signed transaction -- checked here instead, before anything is signed.
NOTE_MAX_BYTES = 1024


def _require_note_within_ceiling(note: bytes | None) -> None:
    if note is not None and len(note) > NOTE_MAX_BYTES:
        raise ValueError(
            f"note must be at most {NOTE_MAX_BYTES} bytes, got {len(note)}"
        )


CREATE_AGREEMENT = (
    "create_agreement(uint64,uint64,uint64,uint64,uint64,byte[],address,address,pay)"
    "uint64"
)
JOIN = "join(uint64,uint64)void"
RELEASE_HASH = "release_hash(uint64,byte[])void"
EXPIRE = "expire(uint64)void"
REFUND_NEXT = "refund_next(uint64,uint64)void"
CLAIM_REFUND = "claim_refund(uint64,uint64)void"
CLOSE = "close(uint64)void"

SIGNATURES = (
    CREATE_AGREEMENT,
    JOIN,
    RELEASE_HASH,
    EXPIRE,
    REFUND_NEXT,
    CLAIM_REFUND,
    CLOSE,
)


def box_mbr(max_seats: int) -> int:
    """Minimum balance locked by the agreement box plus the roster box."""
    agreement = BOX_FLAT_MBR + BOX_BYTE_MBR * (BOX_KEY_BYTES + AGREEMENT_BYTES)
    roster = BOX_FLAT_MBR + BOX_BYTE_MBR * (BOX_KEY_BYTES + SEAT_BYTES * max_seats)
    return agreement + roster


def fee_reserve(max_seats: int, min_fee: int) -> int:
    """(max_seats + 1) inner-transaction fees at the live protocol minimum.

    Release and refund are mutually exclusive and each seat is paid at most
    once, so the ceiling per agreement is one payment per seat plus one -- and
    that spare covers the `close` payment itself.

    Priced from the network rather than a constant because that is what the
    contract charges the application account. A backend that hardcodes the fee
    sends the wrong deposit after any protocol change, and `create_agreement`
    rejects a wrong deposit in either direction.
    """
    return (max_seats + 1) * min_fee


def required_deposit(max_seats: int, min_fee: int) -> int:
    """Both boxes' minimum balance plus the inner-transaction reserve.

    Exact, not a floor: the contract rejects an overpayment as firmly as a
    shortfall, because `close` returns a computed figure rather than whatever
    was sent, so anything extra would have no path out.
    """
    return box_mbr(max_seats) + fee_reserve(max_seats, min_fee)


def agreement_box_name(agreement_id: int) -> bytes:
    return b"a" + agreement_id.to_bytes(8, "big")


def roster_box_name(agreement_id: int) -> bytes:
    return b"r" + agreement_id.to_bytes(8, "big")


@dataclass(frozen=True)
class Agreement:
    """The agreement record: 164 bytes of fixed-width ARC-4 fields."""

    condition: int
    state: int
    deadline: int
    share_price: int
    min_seats: int
    max_seats: int
    seats: int
    refund_cursor: int
    unclaimed_seats: int
    total_held: int
    commit_hash: bytes
    beneficiary: str
    verifier: str
    creator: str


def decode_agreement(raw: bytes) -> Agreement:
    """Decode the record at fixed offsets, in declaration order.

    The length is asserted first: every field below is read at a hardcoded
    offset, so a record of the wrong size would decode as plausible garbage
    rather than fail.
    """
    if len(raw) != AGREEMENT_BYTES:
        raise ValueError(
            f"agreement record is {len(raw)} bytes, expected {AGREEMENT_BYTES}"
        )
    u = int.from_bytes
    return Agreement(
        condition=u(raw[0:1], "big"),
        state=u(raw[1:2], "big"),
        deadline=u(raw[2:10], "big"),
        share_price=u(raw[10:18], "big"),
        min_seats=u(raw[18:20], "big"),
        max_seats=u(raw[20:22], "big"),
        seats=u(raw[22:24], "big"),
        refund_cursor=u(raw[24:26], "big"),
        unclaimed_seats=u(raw[26:28], "big"),
        total_held=u(raw[28:36], "big"),
        commit_hash=raw[36:68],
        beneficiary=encoding.encode_address(raw[68:100]),
        verifier=encoding.encode_address(raw[100:132]),
        creator=encoding.encode_address(raw[132:164]),
    )


def decode_roster(raw: bytes) -> list[tuple[str, int]]:
    """One pre-sized seat per 40 bytes: 32 address bytes then an 8-byte amount.

    A zero amount on an *occupied* seat is the paid marker, not an empty seat.
    Unoccupied seats decode to the zero address, which is what distinguishes
    the two.
    """
    seats: list[tuple[str, int]] = []
    for offset in range(0, len(raw), SEAT_BYTES):
        chunk = raw[offset : offset + SEAT_BYTES]
        seats.append(
            (encoding.encode_address(chunk[0:32]), int.from_bytes(chunk[32:40], "big"))
        )
    return seats


class EscrowClient:
    """Reads and fund-moving calls against one deployed escrow application."""

    def __init__(
        self,
        algod: Any,
        *,
        app_id: int,
        app_account: str,
        usdc_asa_id: int,
        admin_address: str,
        admin_signer: TransactionSigner | None,
    ) -> None:
        self._algod = algod
        self._app_id = app_id
        self._app_account = app_account
        self._usdc = usdc_asa_id
        self._admin = admin_address
        self._signer = admin_signer

    # -- reads ------------------------------------------------------------

    def min_fee(self) -> int:
        return self._algod.suggested_params().min_fee

    def _global_state(self) -> dict[str, int | bytes]:
        app = self._algod.application_info(self._app_id, timeout=READ_TIMEOUT_SECONDS)
        state: dict[str, int | bytes] = {}
        for entry in app["params"].get("global-state", []):
            key = base64.b64decode(entry["key"]).decode()
            value = entry["value"]
            state[key] = (
                value["uint"]
                if value["type"] == 2
                else base64.b64decode(value["bytes"])
            )
        return state

    def is_paused(self) -> bool:
        """The contract's global pause flag.

        `create_agreement` and `join` both assert it is clear, so a paused
        contract accepts neither a new pool nor a new seat. Read by the
        operator's create-pool script, which can afford a global-state read
        and would otherwise discover the pause only when the node rejects
        the create group. Deliberately not read on the request path:
        that would be a second chain round trip on every unauthenticated
        request, to catch a lever only we can pull.
        """
        return bool(int(self._global_state().get("paused", 0)))

    def next_agreement_id(self) -> int:
        """The id `create_agreement` will assign: agreement_count + 1.

        Read before composing, because both box references have to be named in
        the call and a box reference needs the id. Two concurrent creates would
        name the same id and the second would fail on `roster box already
        exists`, which is why creation is serialised behind a single lock in
        the pricing layer rather than here.
        """
        return int(self._global_state().get("agreement_count", 0)) + 1

    def read_agreement(self, agreement_id: int) -> Agreement | None:
        """The agreement record, or None when there is no such box.

        None means exactly one thing: algod answered, and the box is not
        there. Every other HTTP failure -- a 429 from a shared node, a 5xx, a
        rejected token -- is raised rather than flattened into None, because
        a caller that cannot tell "no box" from "no answer" acts on the wrong
        one. The create-pool script's one-pool-at-a-time guard is the case
        that makes this load-bearing: it reads "no answer" as "nothing open",
        and a second pool opened beside a live one holds real seats against a
        second real `commit_hash`, which cannot be merged back together.
        """
        try:
            box = self._algod.application_box_by_name(
                self._app_id,
                agreement_box_name(agreement_id),
                timeout=READ_TIMEOUT_SECONDS,
            )
        except AlgodHTTPError as error:
            if error.code == 404:
                return None
            raise
        return decode_agreement(base64.b64decode(box["value"]))

    def read_roster(self, agreement_id: int) -> list[tuple[str, int]]:
        """The roster, or `[]` when there is no such box.

        `[]` carries the same discipline `read_agreement` documents: it is an
        answer from algod, not the absence of one. An empty roster is the
        normal state after `close` has deleted the box, and `claim_refund`
        bounds-checks a seat against it -- so a 429 flattened to `[]` would
        report every seat out of range on an agreement whose roster is
        intact.
        """
        try:
            box = self._algod.application_box_by_name(
                self._app_id,
                roster_box_name(agreement_id),
                timeout=READ_TIMEOUT_SECONDS,
            )
        except AlgodHTTPError as error:
            if error.code == 404:
                return []
            raise
        return decode_roster(base64.b64decode(box["value"]))

    # -- writes -----------------------------------------------------------

    def _require_signer(self) -> TransactionSigner:
        """Every write goes through here.

        `admin_signer` stays optional because a read-only client is a real
        thing -- the sweeper's reconciliation reads and the offline tests read,
        and neither should have to hold a key to do it. What must not happen is
        a write silently reaching algosdk with `None` and failing there, where
        the message names an internal signing step rather than the missing
        configuration.
        """
        if self._signer is None:
            raise ValueError(
                "admin_signer is required for write calls; this client was "
                "constructed read-only"
            )
        return self._signer

    def _execute(self, atc: AtomicTransactionComposer) -> Any:
        return atc.execute(self._algod, 4)

    def create_hash_agreement(
        self,
        *,
        commit_hash: bytes,
        share_price: int,
        deadline: int,
        beneficiary: str,
        verifier: str,
        seats: int = 1,
        note: bytes | None = None,
    ) -> int:
        """Create a `hash` agreement with `seats` seats and return its id.

        `seats` defaults to 1 because the delivery escrow is single-payer by
        design, not because the contract constrains a `hash` agreement to one
        seat -- it no longer does. `min_seats` and `max_seats` are always sent
        equal: a pool's size is fixed at creation, so the threshold and the
        ceiling are the same number.

        `commit_hash` is fixed here and the contract offers no way to bind or
        update it afterwards, so the deliverable's hash must already be known.
        With `seats > 1` that is not merely advisable but enforced: a hash
        cannot be committed for bytes that do not exist, so a multi-seat hash
        agreement can only be created after its deliverable is final.

        `note` rides on the create transaction. Putting the deliverable's
        `sha256` there makes the commitment timestamped *and* linked to the
        agreement that carries it, which a box read alone does not give.
        """
        signer = self._require_signer()
        if len(commit_hash) != 32:
            raise ValueError(f"commit_hash must be 32 bytes, got {len(commit_hash)}")
        if seats < 1:
            raise ValueError(f"seats must be at least 1, got {seats}")
        agreement_id = self.next_agreement_id()
        sp = self._algod.suggested_params()
        deposit = required_deposit(seats, sp.min_fee)

        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=self._app_id,
            method=abi.Method.from_signature(CREATE_AGREEMENT),
            sender=self._admin,
            sp=sp,
            signer=signer,
            method_args=[
                CONDITION_HASH,
                share_price,
                seats,  # min_seats
                seats,  # max_seats: equal by construction, a pool's size is fixed
                deadline,
                commit_hash,
                beneficiary,
                verifier,
                TransactionWithSigner(
                    transaction.PaymentTxn(
                        sender=self._admin,
                        sp=sp,
                        receiver=self._app_account,
                        amt=deposit,
                    ),
                    signer,
                ),
            ],
            # The beneficiary's holding is read by the contract's _can_receive
            # predicate, and both the account and the asset have to be
            # available in the same call for the AVM to resolve a holding.
            accounts=[beneficiary],
            foreign_assets=[self._usdc],
            boxes=[
                (0, agreement_box_name(agreement_id)),
                (0, roster_box_name(agreement_id)),
            ],
            note=note,
        )
        returned = int(self._execute(atc).abi_results[0].return_value)
        if returned != agreement_id:
            raise RuntimeError(
                f"created agreement {returned}, expected {agreement_id}; another "
                "creator raced this call"
            )
        return returned

    def _app_call(
        self,
        signature: str,
        args: list[Any],
        *,
        accounts: list[str] | None = None,
        boxes: list[tuple[int, bytes]] | None = None,
        sender: str | None = None,
        signer: TransactionSigner | None = None,
        note: bytes | None = None,
    ) -> str:
        _require_note_within_ceiling(note)
        signer = signer or self._require_signer()
        sp = self._algod.suggested_params()
        atc = AtomicTransactionComposer()
        atc.add_method_call(
            app_id=self._app_id,
            method=abi.Method.from_signature(signature),
            sender=sender or self._admin,
            sp=sp,
            signer=signer,
            method_args=args,
            accounts=accounts or [],
            foreign_assets=[self._usdc],
            boxes=boxes or [],
            note=note,
        )
        return self._execute(atc).tx_ids[0]

    def release_hash(
        self,
        agreement_id: int,
        delivered_bytes_hash: bytes,
        *,
        note: bytes | None = None,
    ) -> str:
        """Release to the beneficiary against a 32-byte sha256 of the delivery.

        The length is checked here rather than left to the chain: the contract
        compares this against `commit_hash` and rejects a mismatch, but that
        rejection costs a round trip to the node and reports itself as
        "hash mismatch" -- which reads as *the delivery was wrong* when the real
        problem is that the caller passed the wrong kind of value.

        `note` rides on the release transaction. An edition pool's release
        carries the edition's CID there (docs/specs/edition-release.md): the
        call that pays the pool out is the one that records what it paid for,
        and a transaction's note outlives `close`, which deletes only boxes.
        """
        if len(delivered_bytes_hash) != 32:
            raise ValueError(
                f"delivered_bytes_hash must be a 32-byte sha256, got "
                f"{len(delivered_bytes_hash)} bytes"
            )
        _require_note_within_ceiling(note)
        agreement = self.read_agreement(agreement_id)
        if agreement is None:
            raise ValueError(f"agreement {agreement_id} does not exist")
        return self._app_call(
            RELEASE_HASH,
            [agreement_id, delivered_bytes_hash],
            accounts=[agreement.beneficiary],
            boxes=[(0, agreement_box_name(agreement_id))],
            note=note,
        )

    def expire(self, agreement_id: int) -> str:
        agreement = self.read_agreement(agreement_id)
        if agreement is None:
            raise ValueError(f"agreement {agreement_id} does not exist")
        return self._app_call(
            EXPIRE,
            [agreement_id],
            accounts=[agreement.beneficiary],
            boxes=[(0, agreement_box_name(agreement_id))],
        )

    def refund_next(self, agreement_id: int, count: int) -> str:
        """Pay or skip the next `count` roster entries, FIFO by seat.

        The accounts array is built from the roster window this call will
        touch, because every payer an inner transfer pays into has to be
        available in the same transaction.
        """
        if not 0 < count <= REFUND_BATCH_CEILING:
            raise ValueError(f"count must be 1..{REFUND_BATCH_CEILING}, got {count}")
        agreement = self.read_agreement(agreement_id)
        if agreement is None:
            raise ValueError(f"agreement {agreement_id} does not exist")
        roster = self.read_roster(agreement_id)
        window = roster[agreement.refund_cursor : agreement.refund_cursor + count]
        return self._app_call(
            REFUND_NEXT,
            [agreement_id, count],
            accounts=[payer for payer, _amount in window],
            boxes=[
                (0, agreement_box_name(agreement_id)),
                (0, roster_box_name(agreement_id)),
            ],
        )

    def claim_refund(
        self,
        agreement_id: int,
        seat: int,
        *,
        sender: str,
        signer: TransactionSigner,
    ) -> str:
        """Permissionless: a payer the refund pass skipped claims their own.

        Sent by the payer rather than the admin, which is what keeps a refund
        reachable when the operator is gone.

        The seat is bounds-checked against the roster rather than indexed
        blindly. `read_roster` returns `[]` for a missing or unreadable box --
        which is the normal state after `close` has deleted it -- so an
        unchecked index would raise IndexError before the transaction was even
        composed, and say nothing about which of the two happened.
        """
        agreement = self.read_agreement(agreement_id)
        if agreement is None:
            raise ValueError(
                f"agreement {agreement_id} does not exist; it may already be closed"
            )
        roster = self.read_roster(agreement_id)
        if not 0 <= seat < len(roster):
            raise ValueError(
                f"seat {seat} is out of range for agreement {agreement_id}, "
                f"which has {len(roster)} seats"
            )
        return self._app_call(
            CLAIM_REFUND,
            [agreement_id, seat],
            accounts=[roster[seat][0]],
            boxes=[
                (0, agreement_box_name(agreement_id)),
                (0, roster_box_name(agreement_id)),
            ],
            sender=sender,
            signer=signer,
        )

    def close(self, agreement_id: int) -> str:
        """Delete both boxes and return the unspent deposit to the creator."""
        agreement = self.read_agreement(agreement_id)
        if agreement is None:
            raise ValueError(f"agreement {agreement_id} does not exist")
        return self._app_call(
            CLOSE,
            [agreement_id],
            accounts=[agreement.creator],
            boxes=[
                (0, agreement_box_name(agreement_id)),
                (0, roster_box_name(agreement_id)),
            ],
        )
