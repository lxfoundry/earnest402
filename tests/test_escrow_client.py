"""The off-chain view of the escrow application: arithmetic, encoding, the ABI.

All offline. The network round trips are exercised on LocalNet from Task 7
onward; what is testable without a node is precisely the part that is wrong
silently -- a deposit off by a fee, a struct decoded at the wrong offset, a
method signature that drifted from the contract.
"""

import inspect
import json
import pathlib

import pytest
from algosdk import encoding

from api import escrow

ROOT = pathlib.Path(__file__).resolve().parents[1]
ARC56 = (
    ROOT
    / "contracts"
    / "smart_contracts"
    / "artifacts"
    / "escrow"
    / "Escrow.arc56.json"
)

BENEFICIARY = "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY"
VERIFIER = "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M"


# --- the ABI, pinned against the contract ------------------------------


def test_every_signature_this_backend_uses_exists_in_the_contract():
    """The ABI is restated here rather than imported, because the generated
    typed client next to the compiled artifacts declares
    `requires: algokit-utils@^3.0.0` -- a dependency of the other surface.

    This test is what stops the restatement drifting. It reads the committed
    ARC-56 spec as a *file*, which is a path dependency rather than an import,
    and CI already proves that file matches the compiled contract.
    """
    spec = json.loads(ARC56.read_text(encoding="utf-8"))
    declared = {
        f"{m['name']}({','.join(a['type'] for a in m['args'])}){m['returns']['type']}"
        for m in spec["methods"]
    }
    assert declared, "no methods found in the ARC-56 spec"
    for signature in escrow.SIGNATURES:
        assert signature in declared, (
            f"{signature} is not a method of the contract; the restatement in "
            "api/escrow.py has drifted, and the contract is what is right"
        )


def test_the_agreement_struct_layout_matches_the_contract():
    """The decoder reads fixed offsets, so a field added or reordered in the
    contract would decode as silent garbage rather than as an error."""
    spec = json.loads(ARC56.read_text(encoding="utf-8"))
    fields = [(f["name"], f["type"]) for f in spec["structs"]["Agreement"]]
    assert fields == [
        ("condition", "uint8"),
        ("state", "uint8"),
        ("deadline", "uint64"),
        ("share_price", "uint64"),
        ("min_seats", "uint16"),
        ("max_seats", "uint16"),
        ("seats", "uint16"),
        ("refund_cursor", "uint16"),
        ("unclaimed_seats", "uint16"),
        ("total_held", "uint64"),
        ("commit_hash", "byte[32]"),
        ("beneficiary", "address"),
        ("verifier", "address"),
        ("creator", "address"),
    ]


# --- deposit arithmetic ------------------------------------------------


def test_the_deposit_is_computed_from_the_live_minimum_fee():
    """`create_agreement` rejects an overpayment as firmly as a shortfall, so
    this arithmetic is exact or the call fails. The fee reserve is priced at
    the live protocol minimum because that is what the contract charges the
    application account -- a hardcoded 1000 breaks on any protocol change."""
    # Both boxes' minimum balance: agreement (9-byte key, 164 bytes) plus
    # roster (9-byte key, 40 bytes per seat).
    assert escrow.box_mbr(1) == 93_800
    assert escrow.fee_reserve(1, 1_000) == 2_000
    assert escrow.required_deposit(1, 1_000) == 95_800

    # A protocol fee change moves the answer. A constant would not.
    assert escrow.fee_reserve(1, 2_000) == 4_000
    assert escrow.required_deposit(1, 2_000) == 97_800


def test_the_fee_reserve_is_one_transaction_per_seat_plus_one():
    """Release and refund are mutually exclusive and each seat is paid at most
    once, so the ceiling is one payment per seat plus one -- and that spare is
    what pays for `close` itself."""
    for seats in (1, 2, 20):
        assert escrow.fee_reserve(seats, 1_000) == (seats + 1) * 1_000


def test_the_deposit_scales_with_the_roster():
    """Only the roster half grows with seats; the agreement box is fixed."""
    one = escrow.box_mbr(1)
    two = escrow.box_mbr(2)
    assert two - one == 400 * escrow.SEAT_BYTES
    assert escrow.required_deposit(20, 1_000) == escrow.box_mbr(20) + 21_000


# --- box names ---------------------------------------------------------


def test_box_names_match_the_contract_key_scheme():
    assert escrow.agreement_box_name(1) == b"a" + (1).to_bytes(8, "big")
    assert escrow.roster_box_name(41) == b"r" + (41).to_bytes(8, "big")
    # Big-endian, 8 bytes, so ordering by name is ordering by id.
    assert escrow.agreement_box_name(256) == b"a\x00\x00\x00\x00\x00\x00\x01\x00"


# --- decoding ----------------------------------------------------------


def _raw_agreement(**overrides):
    fields = {
        "condition": (0).to_bytes(1, "big"),
        "state": (1).to_bytes(1, "big"),
        "deadline": (1_756_000_000).to_bytes(8, "big"),
        "share_price": (100_000).to_bytes(8, "big"),
        "min_seats": (1).to_bytes(2, "big"),
        "max_seats": (1).to_bytes(2, "big"),
        "seats": (1).to_bytes(2, "big"),
        "refund_cursor": (0).to_bytes(2, "big"),
        "unclaimed_seats": (0).to_bytes(2, "big"),
        "total_held": (100_000).to_bytes(8, "big"),
        "commit_hash": bytes(range(32)),
        "beneficiary": encoding.decode_address(BENEFICIARY),
        "verifier": encoding.decode_address(VERIFIER),
        "creator": encoding.decode_address(BENEFICIARY),
    }
    fields.update(overrides)
    return b"".join(fields.values())


def test_the_agreement_record_decodes_field_for_field():
    raw = _raw_agreement()
    assert len(raw) == escrow.AGREEMENT_BYTES == 164

    agreement = escrow.decode_agreement(raw)
    assert agreement.condition == escrow.CONDITION_HASH
    assert agreement.state == escrow.STATE_FUNDED
    assert agreement.deadline == 1_756_000_000
    assert agreement.share_price == 100_000
    assert agreement.min_seats == agreement.max_seats == agreement.seats == 1
    assert agreement.refund_cursor == 0
    assert agreement.unclaimed_seats == 0
    assert agreement.total_held == 100_000
    assert agreement.commit_hash == bytes(range(32))
    assert agreement.beneficiary == BENEFICIARY
    assert agreement.verifier == VERIFIER
    assert agreement.creator == BENEFICIARY


def test_decoding_a_record_of_the_wrong_length_raises():
    """Better a loud failure than fields silently read from wrong offsets."""
    with pytest.raises(ValueError, match="164"):
        escrow.decode_agreement(_raw_agreement()[:-1])


def test_the_roster_decodes_as_address_then_amount():
    raw = encoding.decode_address(VERIFIER) + (100_000).to_bytes(8, "big")
    assert escrow.decode_roster(raw) == [(VERIFIER, 100_000)]


def test_the_roster_decodes_every_pre_sized_seat():
    """The roster is pre-sized at creation, so unoccupied seats decode to the
    zero address. A zero *amount* on an occupied seat is the paid marker, which
    is a different thing and must not be confused with an empty seat."""
    occupied = encoding.decode_address(VERIFIER) + (100_000).to_bytes(8, "big")
    paid = encoding.decode_address(BENEFICIARY) + (0).to_bytes(8, "big")
    empty = bytes(escrow.SEAT_BYTES)

    seats = escrow.decode_roster(occupied + paid + empty)
    assert len(seats) == 3
    assert seats[0] == (VERIFIER, 100_000)
    assert seats[1] == (BENEFICIARY, 0)
    assert seats[2][1] == 0
    assert seats[2][0] == encoding.encode_address(bytes(32))


# --- states ------------------------------------------------------------


def test_the_state_discriminators_match_the_contract():
    """Mirrored from the contract rather than imported, for the same reason the
    ABI is. Wrong values here would read a FUNDED agreement as OPEN."""
    assert (
        escrow.STATE_OPEN,
        escrow.STATE_FUNDED,
        escrow.STATE_FILLED,
        escrow.STATE_RELEASED,
        escrow.STATE_EXPIRED,
        escrow.STATE_REFUNDING,
        escrow.STATE_REFUNDED,
    ) == (0, 1, 2, 3, 4, 5, 6)
    assert (escrow.CONDITION_HASH, escrow.CONDITION_QUORUM) == (0, 1)


def test_the_refund_batch_ceiling_is_four():
    """Every account an inner transfer pays into has to be in that call's
    accounts array, and that array holds four."""
    assert escrow.REFUND_BATCH_CEILING == 4


# --- client argument validation, which needs no network ----------------

# A stand-in for a real TransactionSigner. Nothing here composes a transaction,
# so its identity never matters -- only that the client sees a signer present.
DUMMY_SIGNER = object()


def _client(*, signer=DUMMY_SIGNER):
    return escrow.EscrowClient(
        algod=None,
        app_id=1,
        app_account=BENEFICIARY,
        usdc_asa_id=10458941,
        admin_address=BENEFICIARY,
        admin_signer=signer,
    )


def test_a_write_without_a_signer_is_refused_before_it_reaches_algosdk():
    """`admin_signer` stays optional because a read-only client is a real
    thing. What must not happen is a write reaching algosdk with None and
    failing there, where the message names an internal signing step rather
    than the missing configuration."""
    client = _client(signer=None)
    with pytest.raises(ValueError, match="admin_signer is required"):
        client.create_hash_agreement(
            commit_hash=bytes(32),
            share_price=1,
            deadline=1,
            beneficiary=BENEFICIARY,
            verifier=VERIFIER,
        )


def test_a_commit_hash_that_is_not_32_bytes_is_refused():
    client = _client()
    with pytest.raises(ValueError, match="32 bytes"):
        client.create_hash_agreement(
            commit_hash=b"short",
            share_price=1,
            deadline=1,
            beneficiary=BENEFICIARY,
            verifier=VERIFIER,
        )


class _FakeAlgodBoxes:
    """Serves the two box reads the client makes, and nothing else."""

    def __init__(self, *, agreement: bytes | None, roster: bytes):
        self._agreement = agreement
        self._roster = roster

    def application_box_by_name(self, app_id, name, **kwargs):
        import base64

        from algosdk.error import AlgodHTTPError

        if name.startswith(b"a"):
            if self._agreement is None:
                # 404 is what algod answers for a box that is not there, and
                # the client now reads the status rather than the exception
                # type -- so a fake that omitted it would be testing a
                # not-found path the real node never produces.
                raise AlgodHTTPError("box not found", 404)
            return {"value": base64.b64encode(self._agreement).decode()}
        return {"value": base64.b64encode(self._roster).decode()}


def test_claim_refund_refuses_an_agreement_that_does_not_exist():
    """`close` deletes both boxes, so a missing agreement is the normal state
    after settlement rather than a corrupt one -- and the caller deserves to be
    told that rather than to catch an IndexError from an empty roster."""
    client = escrow.EscrowClient(
        algod=_FakeAlgodBoxes(agreement=None, roster=b""),
        app_id=1,
        app_account=BENEFICIARY,
        usdc_asa_id=10458941,
        admin_address=BENEFICIARY,
        admin_signer=DUMMY_SIGNER,
    )
    with pytest.raises(ValueError, match="does not exist"):
        client.claim_refund(1, 0, sender=BENEFICIARY, signer=DUMMY_SIGNER)


@pytest.mark.parametrize("seat", [-1, 1, 99])
def test_claim_refund_refuses_a_seat_outside_the_roster(seat):
    one_seat = encoding.decode_address(VERIFIER) + (100_000).to_bytes(8, "big")
    client = escrow.EscrowClient(
        algod=_FakeAlgodBoxes(agreement=_raw_agreement(), roster=one_seat),
        app_id=1,
        app_account=BENEFICIARY,
        usdc_asa_id=10458941,
        admin_address=BENEFICIARY,
        admin_signer=DUMMY_SIGNER,
    )
    with pytest.raises(ValueError, match="out of range"):
        client.claim_refund(1, seat, sender=BENEFICIARY, signer=DUMMY_SIGNER)


class _FakeAlgodRefusing:
    """An algod that answers every box read with one HTTP status.

    404 is a real answer -- the box is not there. Anything else is the node
    declining to answer at all, and the two have to stay distinguishable all
    the way up: `agents/create_pool.py`'s one-pool-at-a-time guard reads
    "no answer" as "nothing open" if they do not, and opens a second pool
    beside a live one.
    """

    def __init__(self, code: int):
        self._code = code
        self.calls = 0

    def application_box_by_name(self, app_id, name, **kwargs):
        from algosdk.error import AlgodHTTPError

        self.calls += 1
        raise AlgodHTTPError(f"status {self._code}", self._code)


def _client_over(algod):
    return escrow.EscrowClient(
        algod=algod,
        app_id=1,
        app_account=BENEFICIARY,
        usdc_asa_id=10458941,
        admin_address=BENEFICIARY,
        admin_signer=DUMMY_SIGNER,
    )


@pytest.mark.parametrize("code", [400, 401, 429, 500, 502, 503])
def test_a_read_that_is_not_a_404_is_raised_rather_than_read_as_absent(code):
    """The narrowing this fix turns on. `None` from `read_agreement` means
    one thing -- algod answered and the box is not there -- so every other
    status has to surface. A 429 from a shared node flattened into `None`
    is the case that costs real money, because the create-pool guard cannot
    tell it from an empty chain."""
    from algosdk.error import AlgodHTTPError

    algod = _FakeAlgodRefusing(code)
    client = _client_over(algod)

    with pytest.raises(AlgodHTTPError):
        client.read_agreement(1)
    assert algod.calls == 1


@pytest.mark.parametrize("code", [429, 500])
def test_the_roster_read_carries_the_same_discipline(code):
    """`[]` is likewise an answer, not the absence of one: it is the normal
    state after `close` deletes the box, and `claim_refund` bounds-checks a
    seat against it -- so a flattened 429 would report every seat out of
    range on an agreement whose roster is intact."""
    from algosdk.error import AlgodHTTPError

    client = _client_over(_FakeAlgodRefusing(code))

    with pytest.raises(AlgodHTTPError):
        client.read_roster(1)


def test_a_404_is_still_the_absent_box_both_reads_report():
    """The other half of the narrowing: the ordinary missing-box path, which
    `close` produces on every settled agreement, must be untouched."""
    client = _client_over(_FakeAlgodRefusing(404))

    assert client.read_agreement(1) is None
    assert client.read_roster(1) == []


def test_every_box_read_carries_a_timeout():
    """Unbounded is not an option on a request path: `POST /index` is
    unauthenticated and reads the chain per request, and the SDK's own
    default horizon is 30 seconds -- long enough that a stalled node holds a
    worker thread per request until the process has none left."""
    seen = {}

    class _Recording:
        def application_box_by_name(self, app_id, name, **kwargs):
            seen.update(kwargs)
            from algosdk.error import AlgodHTTPError

            raise AlgodHTTPError("box not found", 404)

    _client_over(_Recording()).read_agreement(1)

    assert seen.get("timeout") == escrow.READ_TIMEOUT_SECONDS
    assert 0 < escrow.READ_TIMEOUT_SECONDS <= 10, (
        "a request-path read has to fail fast; anything near the SDK's own "
        "30-second default defeats the point"
    )


@pytest.mark.parametrize("digest", [b"", b"short", bytes(31), bytes(33), bytes(64)])
def test_release_refuses_a_digest_that_is_not_a_32_byte_sha256(digest):
    """The contract would reject this too, but only after a round trip to the
    node -- and it reports "hash mismatch", which reads as
    *the delivery was wrong* when the caller simply passed the wrong kind of
    value. A hex string instead of bytes is the likely mistake, and 64 bytes is
    exactly what that looks like."""
    client = _client()
    with pytest.raises(ValueError, match="32-byte sha256"):
        client.release_hash(1, digest)


class _FakeAlgodForRelease:
    """The agreement read and the suggested parameters a release composes on."""

    def application_box_by_name(self, app_id, name, **kwargs):
        import base64

        return {"value": base64.b64encode(_raw_agreement()).decode()}

    def suggested_params(self):
        from algosdk import transaction

        return transaction.SuggestedParams(
            fee=0,
            first=1000,
            last=2000,
            gh="SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
            gen="testnet-v1.0",
            flat_fee=False,
            min_fee=1000,
        )


def _composed_release(**kwargs):
    """The transaction `release_hash` composes, captured instead of sent."""
    from algosdk import account
    from algosdk.atomic_transaction_composer import AccountTransactionSigner

    private_key, address = account.generate_account()
    client = escrow.EscrowClient(
        algod=_FakeAlgodForRelease(),
        app_id=1,
        app_account=BENEFICIARY,
        usdc_asa_id=10458941,
        admin_address=address,
        admin_signer=AccountTransactionSigner(private_key),
    )
    composed = []

    class _Executed:
        tx_ids = ("TXID",)

    def _capture(atc):
        composed.append(atc)
        return _Executed()

    client._execute = _capture
    client.release_hash(7, bytes(range(32)), **kwargs)
    (only,) = composed[0].build_group()
    return only.txn


def test_release_carries_the_note_it_is_given():
    """An edition pool's link travels in this note, and a release cannot be
    sent twice: a note dropped between the caller and the transaction is a
    link lost for good."""
    note = b"earnest:index:edition-1:agreement:7:cid:bafybeiexample"
    assert _composed_release(note=note).note == note


def test_release_without_a_note_carries_none():
    assert not _composed_release().note


def test_release_refuses_a_note_longer_than_the_protocol_allows():
    with pytest.raises(ValueError, match="at most 1024 bytes"):
        _composed_release(note=bytes(escrow.NOTE_MAX_BYTES + 1))


def test_any_app_call_refuses_a_note_longer_than_the_protocol_allows():
    """Every application call composes through `_app_call`, so the ceiling is
    enforced there for whichever call supplies a note, before algod is asked
    for anything. This client has no algod at all."""
    client = _client()
    with pytest.raises(ValueError, match="at most 1024 bytes"):
        client._app_call(
            "close(uint64)void", [7], note=bytes(escrow.NOTE_MAX_BYTES + 1)
        )


@pytest.mark.parametrize("count", [0, -1, 5])
def test_refund_next_refuses_a_count_outside_the_contract_ceiling(count):
    client = _client()
    with pytest.raises(ValueError, match="1..4"):
        client.refund_next(1, count)


def test_the_fake_escrow_matches_the_client_it_stands_in_for():
    """Every method FakeEscrow defines has EscrowClient's exact signature.

    The offline suite runs almost entirely against FakeEscrow, so a rename or
    an added keyword on EscrowClient that the fake does not follow leaves the
    whole suite green and production broken -- the same silent-drift failure
    that ARC56 pinning above exists to catch, one layer out.

    Compared by name, order and kind rather than by annotation: the fake is
    not obliged to repeat the client's type hints, only to be callable exactly
    where the client is.
    """
    from tests.conftest import FakeEscrow

    def shape(function):
        return [
            (name, parameter.kind)
            for name, parameter in inspect.signature(function).parameters.items()
            if name != "self"
        ]

    faked = [
        name
        for name, value in vars(FakeEscrow).items()
        if callable(value) and not name.startswith("_")
    ]
    assert faked, "FakeEscrow defines no methods; this test would pass vacuously"

    for name in faked:
        assert hasattr(escrow.EscrowClient, name), (
            f"FakeEscrow.{name} has no counterpart on EscrowClient"
        )
        assert shape(getattr(FakeEscrow, name)) == shape(
            getattr(escrow.EscrowClient, name)
        ), f"FakeEscrow.{name} has drifted from EscrowClient.{name}"


def test_the_fake_covers_every_client_method_the_backend_calls():
    """The other direction: nothing the api package calls is left unfaked.

    Without this, a new EscrowClient method used by api/ would simply
    AttributeError the first time a test exercised the path -- or worse, never
    be exercised offline at all.
    """
    from tests.conftest import FakeEscrow

    called = set()
    for module in (ROOT / "api").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        for name in vars(escrow.EscrowClient):
            if not name.startswith("_") and f"escrow.{name}(" in source:
                called.add(name)

    missing = {name for name in called if not hasattr(FakeEscrow, name)}
    assert not missing, f"api/ calls EscrowClient methods FakeEscrow lacks: {missing}"
