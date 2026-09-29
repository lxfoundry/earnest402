"""Golden vectors for the browser's agreement and roster box decoders.

The TypeScript client reads both boxes straight off algod and decodes them at
hardcoded offsets. Nothing else checks that those offsets agree with the
contract's, so this pins them the way `dump_golden_vectors.py` pins the join
group: one committed file, regenerated on demand, with a test that fails when
the committed copy goes stale.

**The encoder here is deliberately not `api/escrow.py`'s decoder run
backwards, and not the hand-written `_raw_agreement` helper in
`tests/test_escrow_client.py`.** Both lay bytes out by hand at the same
offsets the decoder reads, so a mistake in the layout would be invisible:
the check would agree with itself.

Instead the layout is read from `Escrow.arc56.json` -- the compiler's own
description of the struct it emits -- and encoded with algosdk's ARC-4 tuple
type. The fields are then placed by *name*, in the order the artifact declares,
and the bytes are read back through `decode_agreement` and compared against the
values that went in. That comparison is the whole point of this file: the
encoder knows the layout from the compiler and the decoder knows it from
hardcoded offsets, so a transposition of two same-width fields -- which no
length check can see -- fails here.

Offline and deterministic, like its sibling: fixed inputs, no network, no
clock. Addresses come from fixed byte patterns rather than generated keys,
because these vectors are a layout fixture and hold nothing.

    python -m agents.dump_box_vectors
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from algosdk import abi, encoding

from api import escrow
from api.escrow import (
    AGREEMENT_BYTES,
    SEAT_BYTES,
    agreement_box_name,
    decode_agreement,
    decode_roster,
    roster_box_name,
)

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "tests"
    / "golden"
    / "escrow-boxes.json"
)

ARC56 = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "smart_contracts"
    / "artifacts"
    / "escrow"
    / "Escrow.arc56.json"
)


def _agreement_members() -> list[tuple[str, str]]:
    """The `Agreement` struct as the compiler emitted it: names and ARC-4 types.

    Read from the artifact rather than restated here, so this encoder cannot
    inherit a mistake from the decoder it is meant to check. Every member is
    fixed width, which is what makes the offsets the client reads literal; a
    member that was not would break that and is refused.
    """
    spec = json.loads(ARC56.read_text(encoding="utf-8"))
    members = [(m["name"], m["type"]) for m in spec["structs"]["Agreement"]]
    for name, kind in members:
        assert abi.ABIType.from_string(kind).byte_len(), (
            f"{name} is {kind}, which is not fixed width"
        )
    return members


AGREEMENT_MEMBERS = _agreement_members()

# The same struct as an ARC-4 tuple. Fixed widths throughout, so the encoding
# is a plain concatenation and the offsets the client reads are literal.
AGREEMENT_TUPLE = abi.ABIType.from_string(
    "(" + ",".join(kind for _, kind in AGREEMENT_MEMBERS) + ")"
)

# One roster seat: the payer, then what they are still owed.
SEAT_TUPLE = abi.ABIType.from_string("(address,uint64)")

ZERO_ADDRESS = encoding.encode_address(bytes(32))


def _state_names() -> list[str]:
    """Every state, in the order its stored value puts it.

    Shipped with the vectors so the client's own list can be checked against
    it. The client indexes an array by the byte it read, which is the one
    place a wrong *order* -- not a wrong offset -- becomes a wrong answer
    about money: swap RELEASED and EXPIRED and a buyer owed a refund is told
    the edition was delivered.
    """
    states = sorted(
        (value, name[len("STATE_") :])
        for name, value in vars(escrow).items()
        if name.startswith("STATE_")
    )
    assert [value for value, _ in states] == list(range(len(states))), (
        f"the state values are not 0..n: {states}"
    )
    return [name for _, name in states]


def _address(fill: int) -> str:
    """A recognisable address that holds nothing and never will."""
    return encoding.encode_address(bytes([fill]) * 32)


def _agreement(**fields: Any) -> bytes:
    """One record, encoded from the compiler's layout and checked against it.

    The fields are placed by name in the artifact's declared order, so a
    reordering of the struct moves them here too rather than silently writing
    the old order into the new slots.
    """
    declared = [name for name, _ in AGREEMENT_MEMBERS]
    assert set(fields) == set(declared), (
        f"expected exactly {sorted(declared)}, got {sorted(fields)}"
    )

    def _encodable(value: Any) -> Any:
        # algosdk wants byte[32] as a list of ints; everything else goes as-is.
        return list(value) if isinstance(value, bytes) else value

    raw = AGREEMENT_TUPLE.encode([_encodable(fields[name]) for name in declared])
    assert len(raw) == AGREEMENT_BYTES, (
        f"the ARC-4 tuple encoded to {len(raw)} bytes, not {AGREEMENT_BYTES}. "
        "Either the struct changed or this tuple no longer describes it."
    )

    # The check this file exists for. The encoder placed these by name from the
    # compiler's layout; the decoder reads them at hardcoded offsets. Two
    # same-width fields swapped in the decoder -- beneficiary for verifier, say
    # -- pass every width check ever written and fail right here.
    record = decode_agreement(raw)
    for name in declared:
        assert getattr(record, name) == fields[name], (
            f"`decode_agreement` read {name} as {getattr(record, name)!r}, but "
            f"{fields[name]!r} was encoded into that member. The decoder's "
            "offsets and the compiled struct disagree."
        )
    return raw


def _roster(seats: list[tuple[str, int]]) -> bytes:
    raw = b"".join(SEAT_TUPLE.encode([payer, amount]) for payer, amount in seats)
    assert len(raw) == SEAT_BYTES * len(seats)
    # Same round trip as the agreement, for the same reason: the seat's two
    # members are the only thing separating a settled seat from an empty one.
    assert decode_roster(raw) == seats, (
        "`decode_roster` read back something other than the seats encoded"
    )
    return raw


def _decoded_agreement(raw: bytes) -> dict[str, Any]:
    """What `decode_agreement` reads back, in a JSON-safe shape.

    Written from the decoder's output rather than from the inputs above, so a
    vector cannot claim a field the decoder does not actually produce.
    """
    record = decode_agreement(raw)
    return {
        "condition": record.condition,
        "state": record.state,
        # Every uint64 is a string, without exception. JSON numbers are
        # doubles, and a uint64 at full width does not survive one: the
        # maximum-widths vector round-trips 18446744073709551615 as
        # ...616, which reads as a decoder that is off by one rather than as
        # a fixture that cannot hold the value. The uint8 and uint16 fields
        # below are exact as numbers and stay numbers.
        "deadline": str(record.deadline),
        "sharePrice": str(record.share_price),
        "minSeats": record.min_seats,
        "maxSeats": record.max_seats,
        "seats": record.seats,
        "refundCursor": record.refund_cursor,
        "unclaimedSeats": record.unclaimed_seats,
        "totalHeld": str(record.total_held),
        "commitSha256": record.commit_hash.hex(),
        "beneficiary": record.beneficiary,
        "verifier": record.verifier,
        "creator": record.creator,
    }


def build_vectors() -> dict[str, Any]:
    treasury = _address(0x11)
    verifier = _address(0x22)
    creator = _address(0x33)
    payer_one = _address(0x44)
    payer_two = _address(0x55)
    payer_three = _address(0x66)

    vectors: list[dict[str, Any]] = []

    def add(name: str, agreement_id: int, raw: bytes, roster: bytes) -> None:
        vectors.append(
            {
                "name": name,
                "agreementId": str(agreement_id),
                "boxNames": {
                    "agreement": base64.b64encode(
                        agreement_box_name(agreement_id)
                    ).decode(),
                    "roster": base64.b64encode(roster_box_name(agreement_id)).decode(),
                },
                "agreement": {
                    "raw": base64.b64encode(raw).decode(),
                    "expected": _decoded_agreement(raw),
                },
                "roster": {
                    "raw": base64.b64encode(roster).decode(),
                    # Same discipline: read back through the decoder, not
                    # restated from the inputs.
                    "expected": [
                        {"payer": payer, "amount": str(amount)}
                        for payer, amount in decode_roster(roster)
                    ],
                },
            }
        )

    # An open five-seat pool with one seat sold: the shape a buyer sees while
    # waiting, and the one the roster's three seat states are visible in.
    add(
        "open-pool-one-seat-sold",
        25,
        _agreement(
            condition=0,
            state=0,
            deadline=1789651281,
            share_price=100_000,
            min_seats=5,
            max_seats=5,
            seats=1,
            refund_cursor=0,
            unclaimed_seats=0,
            total_held=100_000,
            commit_hash=bytes.fromhex(
                "fa3c8f3bee9291e2bb7ce78070d74e6c8ce307aa4651df9d2d2a7cef3e5eb3c7"
            ),
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster([(payer_one, 100_000)] + [(ZERO_ADDRESS, 0)] * 4),
    )

    # Mid-refund, and the one that pins the seat states apart: seat 0 has been
    # paid (occupied, amount zeroed), seat 1 was skipped and is still owed,
    # seat 2 is owed and still ahead of the cursor, and the tail is empty. A
    # decoder that reads "amount 0" as "empty" gets this vector wrong.
    #
    # Three seats with the cursor at two, because that is a record the
    # contract can hold: `refund_next` moves to REFUNDED in the same step the
    # cursor reaches `seats`, so a REFUNDING record always has a seat left.
    add(
        "refunding-one-paid-one-skipped",
        26,
        _agreement(
            condition=0,
            state=5,
            deadline=1789651281,
            share_price=100_000,
            min_seats=5,
            max_seats=5,
            seats=3,
            refund_cursor=2,
            unclaimed_seats=1,
            total_held=200_000,
            commit_hash=bytes(range(32)),
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster(
            [(payer_one, 0), (payer_two, 100_000), (payer_three, 100_000)]
            + [(ZERO_ADDRESS, 0)] * 2
        ),
    )

    # Delivered. Together with the expired vector below it pins the two states
    # a buyer must never see confused: 3 and 4 sit next to each other, carry
    # opposite news, and are told apart by one byte.
    add(
        "released-pool",
        27,
        _agreement(
            condition=0,
            state=3,
            deadline=1789651281,
            share_price=100_000,
            min_seats=5,
            max_seats=5,
            seats=5,
            refund_cursor=0,
            unclaimed_seats=0,
            total_held=0,
            commit_hash=bytes.fromhex(
                "fa3c8f3bee9291e2bb7ce78070d74e6c8ce307aa4651df9d2d2a7cef3e5eb3c7"
            ),
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster([(payer_one, 0)] * 5),
    )

    # Expired, before the refund pass has started: the money is still held and
    # every seat is still owed.
    add(
        "expired-pool",
        28,
        _agreement(
            condition=0,
            state=4,
            deadline=1789651281,
            share_price=100_000,
            min_seats=5,
            max_seats=5,
            seats=2,
            refund_cursor=0,
            unclaimed_seats=0,
            total_held=200_000,
            commit_hash=bytes.fromhex(
                "fa3c8f3bee9291e2bb7ce78070d74e6c8ce307aa4651df9d2d2a7cef3e5eb3c7"
            ),
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster([(payer_one, 100_000), (payer_two, 100_000)] + [(ZERO_ADDRESS, 0)] * 3),
    )

    # The refund pass finished and paid every seat, so nothing is owed or held:
    # the one state in which `close` is open to anyone.
    add(
        "refunded-pool",
        29,
        _agreement(
            condition=0,
            state=6,
            deadline=1789651281,
            share_price=100_000,
            min_seats=5,
            max_seats=5,
            seats=2,
            refund_cursor=2,
            unclaimed_seats=0,
            total_held=0,
            commit_hash=bytes.fromhex(
                "fa3c8f3bee9291e2bb7ce78070d74e6c8ce307aa4651df9d2d2a7cef3e5eb3c7"
            ),
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster([(payer_one, 0), (payer_two, 0)] + [(ZERO_ADDRESS, 0)] * 3),
    )

    # The extremes every fixed-width field can hold, so a width read one byte
    # short or long shows up as a wrong number rather than as plausible data.
    add(
        "maximum-widths",
        18446744073709551615,
        _agreement(
            condition=1,
            state=6,
            deadline=(1 << 64) - 1,
            share_price=(1 << 64) - 1,
            min_seats=(1 << 16) - 1,
            max_seats=(1 << 16) - 1,
            seats=(1 << 16) - 1,
            refund_cursor=(1 << 16) - 1,
            unclaimed_seats=(1 << 16) - 1,
            total_held=(1 << 64) - 1,
            commit_hash=b"\xff" * 32,
            beneficiary=treasury,
            verifier=verifier,
            creator=creator,
        ),
        _roster([(payer_one, (1 << 64) - 1)]),
    )

    return {
        "agreementBytes": AGREEMENT_BYTES,
        "seatBytes": SEAT_BYTES,
        "zeroAddress": ZERO_ADDRESS,
        "states": _state_names(),
        "vectors": vectors,
    }


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    # Trailing newline, LF only, sorted nothing -- the ordering here is
    # meaningful and hand-chosen. `write_bytes` rather than `write_text` so
    # the platform's line ending never gets a say.
    payload = json.dumps(build_vectors(), indent=2) + "\n"
    OUTPUT.write_bytes(payload.encode("utf-8"))
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
