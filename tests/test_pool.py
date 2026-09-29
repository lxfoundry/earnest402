"""Finding the pool a buyer can actually join.

Exactly one pool is open at a time, never two -- not even at the seam between
editions -- so "the open pool" is a well-defined thing to look for. The route
itself trusts a *pinned* agreement id (`read_pinned_pool`) rather than
searching for one, because every unpaid `POST /pin` quote for a new
`(sha256, size)` also creates an agreement -- unauthenticated, free, and fast
enough that ordinary `/pin` traffic can push a real open pool outside any
bounded scan. `find_any_open_pool`, the unbounded cross-edition scan, is
operator tooling: the create-pool script is looking for *any* still-open
pool, not for this edition's.

The case that matters most, for both the pinned read and the scans, is the
empty one: when no pool is open the route must refuse rather than issue a
payable 402. A 402 naming no agreement would take a buyer's money against
nothing.
"""

from __future__ import annotations

import pytest
from algosdk.error import AlgodHTTPError

from api.app import INDEX_BODY_LIMIT_BYTES
from api.config import CHALLENGE_TAG
from api.escrow import (
    CONDITION_HASH,
    CONDITION_QUORUM,
    STATE_FUNDED,
    STATE_OPEN,
    Agreement,
)
from api.pool import (
    NoPoolOpen,
    OpenPool,
    PoolClosed,
    PoolStatusUnknown,
    find_any_open_pool,
    read_pinned_pool,
)
from tests.conftest import accepts_of, payment_header_for

SHARE_PRICE = 5_000_000
SEATS = 5
COMMIT = bytes(range(32))


def _agreement(
    *,
    state: int = STATE_OPEN,
    condition: int = CONDITION_HASH,
    share_price: int = SHARE_PRICE,
    min_seats: int = SEATS,
    seats: int = 0,
    deadline: int = 2_000_000_000,
) -> Agreement:
    return Agreement(
        condition=condition,
        state=state,
        deadline=deadline,
        share_price=share_price,
        min_seats=min_seats,
        max_seats=min_seats,
        seats=seats,
        refund_cursor=0,
        unclaimed_seats=0,
        total_held=share_price * seats,
        commit_hash=COMMIT,
        beneficiary="B" * 58,
        verifier="V" * 58,
        creator="C" * 58,
    )


class FakeAgreementReader:
    """Stands in for `EscrowClient`'s two read methods, nothing else.

    Not named `FakeEscrow`: `tests/conftest.py` already exports a class by
    that name, standing in for the whole write surface across the offline
    suite, and `tests/test_escrow_client.py` imports it by name. A second
    class with the same name in this module would shadow it only here, which
    is exactly the kind of confusion a distinct name avoids.
    """

    def __init__(self, agreements: dict[int, Agreement], next_id: int) -> None:
        self._agreements = agreements
        self._next = next_id
        self.reads: list[int] = []

    def next_agreement_id(self) -> int:
        return self._next

    def read_agreement(self, agreement_id: int) -> Agreement | None:
        self.reads.append(agreement_id)
        return self._agreements.get(agreement_id)


def _settings(settings_factory, **overrides):
    """A Settings object with the two pool fields pinned and nothing live.

    Pinned explicitly rather than left to settings_factory's own defaults --
    which happen to already match SHARE_PRICE/SEATS above -- so this helper
    keeps describing the pool tests actually rely on even if those defaults
    are retuned later for an unrelated reason.
    """
    return settings_factory(
        index_share_price_micro=SHARE_PRICE,
        index_seats=SEATS,
        **overrides,
    )


def _deps_without_chain(settings_factory):
    """Deps whose escrow client raises if touched.

    The boot guard calls `.sample()` on every price callable, and a cold start
    with the chain unreachable still has to boot -- so the sample path is
    asserted to need no chain read by giving it a client that cannot serve one.
    """

    class Unreachable:
        def next_agreement_id(self):
            raise AssertionError("the sample quote must not read the chain")

        def read_agreement(self, agreement_id):
            raise AssertionError("the sample quote must not read the chain")

    from api.deps import Deps

    return Deps(
        settings=_settings(settings_factory),
        escrow=Unreachable(),
        jobs=None,
        algod=None,
        http=None,
    )


# --- the pinned read: what the route itself actually calls ----------------
#
# The route trusts a configured agreement id rather than any scan, because
# a flood of unpaid
# /pin quotes -- unauthenticated, free, and fast enough at the configured
# rate limit -- can push a real open pool outside any bounded scan within a
# couple of minutes. `read_pinned_pool` applies the same six conditions to
# exactly one box read.


def test_the_pinned_agreement_serves_when_it_meets_every_condition() -> None:
    escrow = FakeAgreementReader({7: _agreement()}, next_id=8)

    pool = read_pinned_pool(
        escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS
    )

    assert pool.agreement_id == 7
    assert pool.commit_hash == COMMIT


def test_the_pinned_read_touches_exactly_one_agreement_regardless_of_population() -> (
    None
):
    """The whole point of pinning: no scan, so a large population of
    unrelated agreements costs nothing and cannot hide the pinned one."""
    agreements = {i: _agreement(min_seats=1) for i in range(1, 500)}
    agreements[17] = _agreement()
    escrow = FakeAgreementReader(agreements, next_id=500)

    pool = read_pinned_pool(
        escrow, agreement_id=17, share_price=SHARE_PRICE, seats=SEATS
    )

    assert pool.agreement_id == 17
    assert escrow.reads == [17]


def test_an_unconfigured_pinned_id_refuses_without_reading_the_chain() -> None:
    """0 is the shipped default (INDEX_AGREEMENT_ID unset). It must refuse
    the same way an unset RESOURCE_URL would -- fail-safe by construction,
    never a payable 402 against a guessed agreement."""
    escrow = FakeAgreementReader({7: _agreement()}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=0, share_price=SHARE_PRICE, seats=SEATS)

    assert escrow.reads == [], "an unconfigured id must not touch the chain at all"


def test_a_negative_pinned_id_refuses_rather_than_reaching_the_box_read() -> None:
    """`_int` accepts a negative INDEX_AGREEMENT_ID, and agreement ids are
    encoded unsigned -- `agreement_box_name` packs eight big-endian bytes --
    so a negative reaching the chain raises OverflowError from inside the
    box read, which the gate surfaces as a 500 rather than the 503 that
    names the state. It has to refuse here, before the read."""
    escrow = FakeAgreementReader({7: _agreement()}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=-1, share_price=SHARE_PRICE, seats=SEATS)

    assert escrow.reads == [], "a negative id must not touch the chain at all"


def test_the_pinned_agreement_refuses_when_nothing_exists_at_that_id() -> None:
    escrow = FakeAgreementReader({}, next_id=1)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_pinned_agreement_refuses_once_filled() -> None:
    escrow = FakeAgreementReader(
        {7: _agreement(state=STATE_FUNDED, seats=SEATS)}, next_id=8
    )

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_pinned_agreement_refuses_when_it_is_a_single_seat_delivery_escrow() -> (
    None
):
    escrow = FakeAgreementReader({7: _agreement(min_seats=1)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_a_single_seat_agreement_is_refused_even_when_one_seat_is_configured() -> None:
    """The single-seat exclusion is unconditional, not a side effect of the
    configured seat count never being one in practice. A misconfigured
    `INDEX_SEATS=1` must still never serve a delivery escrow's agreement as a
    pool: that agreement belongs to one buyer's already-provisioned job, and
    quoting it here would let a stranger's payment join it."""
    escrow = FakeAgreementReader({7: _agreement(min_seats=1)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=1)


def test_the_pinned_agreement_refuses_when_it_is_a_quorum_agreement() -> None:
    escrow = FakeAgreementReader({7: _agreement(condition=CONDITION_QUORUM)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_pinned_agreement_refuses_at_a_different_share_price() -> None:
    escrow = FakeAgreementReader({7: _agreement(share_price=1_000_000)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_pinned_agreement_refuses_at_a_different_seat_count() -> None:
    escrow = FakeAgreementReader({7: _agreement(min_seats=SEATS + 1)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_pinned_agreement_refuses_past_its_deadline() -> None:
    escrow = FakeAgreementReader({7: _agreement(deadline=1)}, next_id=8)

    with pytest.raises(NoPoolOpen):
        read_pinned_pool(
            escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS, now=1_000
        )


# --- the operator's cross-edition scan -------------------------------------
#
# find_any_open_pool is what agents/create_pool.py's one-pool guard uses. It
# has to catch a still-open pool from a *previous* edition even when this
# edition's price or seat count has since moved on, which is the one respect
# in which it must NOT behave like the route's own pinned read above.


def test_find_any_open_pool_finds_a_pool_at_a_different_price_and_seat_count() -> None:
    """The whole reason this scan exists and the pinned read cannot substitute
    for it: a previous edition's still-open pool must not go unseen just
    because INDEX_SHARE_PRICE_MICRO or INDEX_SEATS has moved on."""
    escrow = FakeAgreementReader(
        {3: _agreement(share_price=1_000_000, min_seats=SEATS + 7)}, next_id=4
    )

    pool = find_any_open_pool(escrow)

    assert pool is not None
    assert pool.agreement_id == 3


def test_find_any_open_pool_still_excludes_single_seat_and_quorum_agreements() -> None:
    escrow = FakeAgreementReader(
        {
            1: _agreement(min_seats=1),
            2: _agreement(condition=CONDITION_QUORUM),
        },
        next_id=3,
    )

    assert find_any_open_pool(escrow) is None


def test_find_any_open_pool_returns_none_rather_than_raising() -> None:
    """Unlike the route's pinned read, this is operator tooling that
    checks a fact ("is anything open") rather than trying to serve a sale, so
    the empty case is a plain None the caller branches on."""
    escrow = FakeAgreementReader({}, next_id=1)

    assert find_any_open_pool(escrow) is None


def test_find_any_open_pool_is_bounded_only_by_next_agreement_id() -> None:
    """The deliberate difference from a depth-bounded scan: a false
    'nothing open' here is the unrecoverable direction, so this scans the
    whole population rather than a fixed recent window."""
    agreements = {i: _agreement(min_seats=1) for i in range(1, 200)}
    agreements[1] = _agreement()  # the one open pool, at the oldest id
    escrow = FakeAgreementReader(agreements, next_id=200)

    pool = find_any_open_pool(escrow)

    assert pool is not None
    assert pool.agreement_id == 1
    assert len(escrow.reads) > 25, "this scan must not stop at the bounded depth"


# --- the quote and the price callable -----------------------------------


def test_the_quote_names_the_open_pool_and_carries_the_challenge_tag(
    settings_factory,
) -> None:
    """`extra` belongs to the price and to nothing else.

    The scheme builds the settled PaymentRequirements.extra from here, so an
    accepts-level extra or a bare "$5.00" string drops feePayer and the tag,
    and the payment settles unattributed -- permanently, because attribution
    is stamped at settlement and never reclassified.
    """
    from api.pool import quote_for

    pool = OpenPool(
        agreement_id=7,
        share_price=SHARE_PRICE,
        seats_taken=2,
        seats_total=SEATS,
        deadline=2_000_000_000,
        commit_hash=COMMIT,
    )
    quote = quote_for(_settings(settings_factory), pool)

    assert quote.amount == str(SHARE_PRICE)
    assert quote.extra["tag"] == CHALLENGE_TAG
    assert quote.extra["agreementId"] == 7
    assert quote.extra["feePayer"]
    # seatsLeft is derived (seatsTotal - seatsTaken) rather than looked up,
    # but it is what a buying agent actually wants to know before paying --
    # seatsTaken alone makes it do the subtraction itself.
    assert quote.extra["seatsLeft"] == SEATS - 2


def test_the_sample_quote_needs_no_chain_and_creates_nothing(settings_factory) -> None:
    """The boot guard inspects `extra` without running the callable for real.

    For the pin route that mattered because running it creates an agreement.
    Here it creates nothing, but the guard calls `.sample` on every callable,
    so the attribute has to exist -- and it must not require a chain read
    either, or a cold start with the chain unreachable cannot boot.
    """
    from api.pool import make_pool_price_callable

    price = make_pool_price_callable(_deps_without_chain(settings_factory))
    sample = price.sample()

    assert sample.extra["tag"] == CHALLENGE_TAG
    assert sample.amount == str(SHARE_PRICE)


def test_the_price_callable_refuses_when_no_pool_is_open(settings_factory) -> None:
    """The in-band signal the price callable raises on its own read.

    The gate in api/app.py answers a closed pool with a 503 before this ever
    runs, but the callable still has to refuse on its own: the gate's read
    and this one are two separate chain reads (see the gate's comment for
    why), and a pool that closes in between must never fall through to a
    payable 402 naming an agreement the contract will refuse to join. A
    configured id whose agreement no longer exists (say, `close` ran between
    the two reads) is exactly that case.
    """
    from api.deps import Deps
    from api.pool import make_pool_price_callable
    from tests.conftest import await_price

    class EmptyEscrow:
        def next_agreement_id(self) -> int:
            return 1

        def read_agreement(self, agreement_id: int):
            return None

    deps = Deps(
        settings=_settings(settings_factory, index_agreement_id=7),
        escrow=EmptyEscrow(),
        jobs=None,
        algod=None,
        http=None,
    )
    price = make_pool_price_callable(deps)

    with pytest.raises(PoolClosed):
        await_price(price, None)


def test_the_price_callable_refuses_without_a_chain_read_when_unconfigured(
    settings_factory,
) -> None:
    """0 is the shipped default (INDEX_AGREEMENT_ID unset): nothing to sell,
    and -- unlike the above -- nothing to read either."""
    from api.deps import Deps
    from api.pool import make_pool_price_callable
    from tests.conftest import await_price

    class Unreachable:
        def next_agreement_id(self):
            raise AssertionError("an unconfigured pool must not read the chain")

        def read_agreement(self, agreement_id):
            raise AssertionError("an unconfigured pool must not read the chain")

    deps = Deps(
        settings=_settings(settings_factory, index_agreement_id=0),
        escrow=Unreachable(),
        jobs=None,
        algod=None,
        http=None,
    )
    price = make_pool_price_callable(deps)

    with pytest.raises(PoolClosed):
        await_price(price, None)


# --- POST /index, paid end-to-end ------------------------------------------
#
# Driven through the real app over the offline facilitator harness (the
# `client_for`/`facilitator`/`deps` fixtures in tests/conftest.py), the same
# way tests/test_pin_handler.py drives a paid POST /pin. The open pool is
# seeded directly onto FakeEscrow's agreement_records -- both
# `_gate_pool_availability` and the price callable read it by id, via
# `read_pinned_pool` -- with `created` raised to match, since FakeEscrow
# derives `next_agreement_id()` from that counter rather than from what has
# been seeded. `client_for` is used rather than the shared `client` fixture
# because the pinned id has to be baked into `deps.settings` *before*
# `build_routes` closes over it -- the shared fixture's app is already built
# by the time a test runs, against `index_agreement_id=0`.


def _seed_open_pool(deps, agreement_id: int, **overrides) -> Agreement:
    record = _agreement(**overrides)
    deps.escrow.agreement_records[agreement_id] = record
    deps.escrow.created = max(deps.escrow.created, agreement_id)
    return record


def _quote_index(client) -> dict:
    unpaid = client.post("/index")
    assert unpaid.status_code == 402, unpaid.text
    return accepts_of(unpaid)[0]


def test_a_paid_seat_returns_the_receipt_the_buyer_paid_for(
    deps, facilitator, client_for
):
    """The four fields the brief settles on, read from the verified payment
    rather than a second chain lookup -- and nothing else: no `seatsTaken`
    (this buyer's own `join` has not settled yet), no `cid`, no `edition`,
    and no store write (a pool seat has no leg 2).

    Also pins the branch's own definition of done: the 402 carries
    `extra.tag` and `extra.feePayer` on the wire, not merely in the object
    the callable returns in-process.
    """
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)

    requirements = _quote_index(client)
    assert requirements["extra"]["agreementId"] == agreement_id
    assert requirements["extra"]["tag"] == CHALLENGE_TAG
    assert requirements["extra"]["feePayer"]

    paid = client.post(
        "/index",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert paid.status_code == 200, paid.text
    assert paid.json() == {
        "agreementId": agreement_id,
        "seatsTotal": SEATS,
        "deadline": requirements["extra"]["deadline"],
        "commitSha256": COMMIT.hex(),
    }
    assert len(facilitator.settled) == 1


def test_naming_the_wrong_agreement_id_is_refused_before_a_receipt_is_minted(
    deps, facilitator, client_for
):
    """The 409: a declared `agreementId` that disagrees with the agreement
    the verified payment was actually matched to.

    Reachable in practice when the pool the caller quoted at 402 time has
    since closed and a different pool has opened before the paid retry --
    the price callable re-reads the pinned agreement on every request. This
    test goes straight to that disagreement rather than re-enacting a pool
    closing and reopening in between, since what the handler checks is the
    mismatch itself.
    """
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)

    requirements = _quote_index(client)
    assert requirements["extra"]["agreementId"] == agreement_id
    settled_before = len(facilitator.settled)

    paid = client.post(
        "/index?agreementId=999",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert paid.status_code == 409, paid.text
    detail = paid.json()["detail"]
    assert detail["requestedAgreementId"] == 999
    assert detail["fundedAgreementId"] == agreement_id
    # 409 is >= 400, so the middleware never settled: the buyer is not charged.
    assert len(facilitator.settled) == settled_before


def _paid_index(client, requirements, *, path="/index", body=None, **headers):
    return client.post(
        path,
        content=body,
        headers={
            "PAYMENT-SIGNATURE": payment_header_for(requirements),
            **headers,
        },
    )


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Content-Type": "application/json"}, id="json"),
        pytest.param({"Content-Type": "application/json; charset=utf-8"}, id="charset"),
        pytest.param({}, id="no-content-type"),
        pytest.param({"Content-Type": "text/plain"}, id="text-plain"),
    ],
)
def test_an_agreement_id_in_a_json_body_is_honoured_like_the_query_one(
    deps, facilitator, client_for, headers
):
    """The route is catalogued as taking a JSON body, so a caller that sends
    `agreementId` there gets the same 409 as one that puts it in the query --
    rather than having it ignored and a receipt for a pool it did not name.

    Whatever the Content-Type says: a caller that omits or mislabels it still
    meant the id it sent, and ignoring the id is the outcome being refused."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = _paid_index(client, requirements, body=b'{"agreementId": 999}', **headers)

    assert paid.status_code == 409, paid.text
    detail = paid.json()["detail"]
    assert detail["requestedAgreementId"] == 999
    assert detail["fundedAgreementId"] == agreement_id
    assert len(facilitator.settled) == settled_before


def test_a_buyer_copying_the_catalogued_example_can_buy_a_seat(
    deps, facilitator, client_for
):
    """A buyer agent builds its request from the Bazaar listing, and the
    handler honours what the body names. So the example the listing shows
    has to be one that buys a seat in whichever pool this deployment is
    serving -- sent verbatim on both passes, as an x402 client replays its
    request. An example naming a fixed agreement id would be refused with a
    409, after the buyer had signed, on every deployment whose pool has
    another id."""
    from api.routes import build_routes

    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    declared = build_routes(deps)["POST /index"].extensions["bazaar"]["info"]["input"]
    assert declared["bodyType"] == "json", "the handler reads only a JSON body"
    example = declared["body"]

    unpaid = client.post("/index", json=example)
    assert unpaid.status_code == 402, unpaid.text
    requirements = accepts_of(unpaid)[0]
    paid = client.post(
        "/index",
        json=example,
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert paid.status_code == 200, paid.text
    assert paid.json()["agreementId"] == agreement_id
    assert len(facilitator.settled) == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b'{"agreementId": 7}', id="naming-the-funded-pool"),
        pytest.param(b"{}", id="empty-object"),
        pytest.param(b'{"note": "hello"}', id="other-keys-only"),
        pytest.param(b"", id="empty"),
        pytest.param(b"  \n", id="whitespace"),
    ],
)
def test_a_body_that_names_no_other_pool_returns_the_receipt(
    deps, facilitator, client_for, body
):
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)

    paid = _paid_index(
        client, requirements, body=body, **{"Content-Type": "application/json"}
    )

    assert paid.status_code == 200, paid.text
    assert paid.json()["agreementId"] == agreement_id
    assert len(facilitator.settled) == 1


def test_a_body_naming_agreement_zero_is_refused_not_ignored(
    deps, facilitator, client_for
):
    """0 is falsy and never a pool. Named, it is still a named id: a
    truthiness test in place of `is None` would drop it and settle."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = _paid_index(
        client,
        requirements,
        body=b'{"agreementId": 0}',
        **{"Content-Type": "application/json"},
    )

    assert paid.status_code == 409, paid.text
    assert paid.json()["detail"]["requestedAgreementId"] == 0
    assert len(facilitator.settled) == settled_before


@pytest.mark.parametrize(
    "query, body",
    [
        pytest.param("7", b'{"agreementId": 999}', id="query-funded-body-other"),
        pytest.param("999", b'{"agreementId": 7}', id="query-other-body-funded"),
        pytest.param("0", b'{"agreementId": 7}', id="query-zero-body-funded"),
    ],
)
def test_query_and_body_naming_different_pools_is_refused_before_settlement(
    deps, facilitator, client_for, query, body
):
    """Two answers to one question. Neither is picked: either guess could be
    the one the caller meant, so the request is refused and nothing settles."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = _paid_index(
        client,
        requirements,
        path=f"/index?agreementId={query}",
        body=body,
        **{"Content-Type": "application/json"},
    )

    assert paid.status_code == 400, paid.text
    assert paid.json()["detail"]["error"] == "conflicting_agreement_id"
    assert len(facilitator.settled) == settled_before


def test_query_and_body_naming_the_same_pool_returns_the_receipt(
    deps, facilitator, client_for
):
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)

    paid = _paid_index(
        client,
        requirements,
        path=f"/index?agreementId={agreement_id}",
        body=b'{"agreementId": 7}',
        **{"Content-Type": "application/json"},
    )

    assert paid.status_code == 200, paid.text
    assert len(facilitator.settled) == 1


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"agreementId=7", id="not-json"),
        pytest.param(b"[7]", id="not-an-object"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"agreementId": "7"}', id="string-id"),
        pytest.param(b'{"agreementId": true}', id="boolean-id"),
        pytest.param(b'{"agreementId": 7.0}', id="float-id"),
        pytest.param(b'{"agreementId": null}', id="null-id"),
        pytest.param(b"\x80", id="not-utf8"),
        pytest.param(b'{"agreementId": 7, "n": "\xe9"}', id="latin1-byte-in-object"),
        pytest.param(b"\xff\xfe", id="utf16-bom-only"),
        pytest.param(b"[" * INDEX_BODY_LIMIT_BYTES, id="nested-to-the-limit"),
    ],
)
def test_a_body_that_cannot_be_read_is_refused_before_settlement(
    deps, facilitator, client_for, body
):
    """Every one of these might have been meant to name a pool. Settling
    while ignoring it would hand back a receipt for whichever pool is open,
    which is the outcome the 409 exists to prevent -- so they are refused,
    and 400 is >= 400, so the middleware never settles.

    `7.0` is refused although JSON Schema counts it an integer: the handler
    compares ids exactly, and refusing before settlement costs the caller a
    retry, never money."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = _paid_index(
        client, requirements, body=body, **{"Content-Type": "application/json"}
    )

    assert paid.status_code == 400, paid.text
    assert paid.json()["detail"]["error"] == "unreadable_body"
    assert len(facilitator.settled) == settled_before


def test_a_body_one_byte_over_the_limit_is_refused_before_settlement(
    deps, facilitator, client_for
):
    """The one input is a small integer. A body past the limit is refused with
    413, and nothing settles. `nested-to-the-limit` above is exactly at the
    limit and is not refused as too large, so the two pin the limit exactly."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)
    prefix = f'{{"agreementId": {agreement_id}}}'
    body = (prefix + " " * (INDEX_BODY_LIMIT_BYTES + 1 - len(prefix))).encode()
    assert len(body) == INDEX_BODY_LIMIT_BYTES + 1

    paid = _paid_index(
        client, requirements, body=body, **{"Content-Type": "application/json"}
    )

    assert paid.status_code == 413, paid.text
    assert paid.json()["detail"]["error"] == "body_too_large"
    assert len(facilitator.settled) == settled_before


def test_a_seat_payment_carrying_no_join_is_refused_before_settlement(
    deps, facilitator, client_for
):
    """The exposure that made this route not-yet-MainNet-ready.

    A transfer with no `join` in its group settles into the application
    account against no seat, no roster entry and no refund path, and the
    USDC cannot be recovered by anyone. It is the only malformed group with
    no on-chain consequence -- there is no application call left to fail --
    so the refusal has to happen here, in the handler, which runs before
    process_settlement() and skips it entirely for any response >= 400.
    """
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = client.post(
        "/index",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements, joins=0)},
    )

    assert paid.status_code == 400, paid.text
    assert paid.json()["detail"]["error"] == "payment_group_incomplete"
    assert len(facilitator.settled) == settled_before, (
        "the buyer must still hold their USDC"
    )


def test_a_seat_payment_joining_another_pool_is_refused(deps, facilitator, client_for):
    """`extra.agreementId` decides what the receipt says; the `join` call
    decides which pool the chain credits. A group that disagrees would buy a
    seat in one pool and be handed a receipt for another."""
    agreement_id = 7
    _seed_open_pool(deps, agreement_id)
    client = client_for(index_agreement_id=agreement_id)
    requirements = _quote_index(client)
    settled_before = len(facilitator.settled)

    paid = client.post(
        "/index",
        headers={
            "PAYMENT-SIGNATURE": payment_header_for(requirements, join_agreement_id=99)
        },
    )

    assert paid.status_code == 400, paid.text
    assert len(facilitator.settled) == settled_before


def test_a_configured_pool_that_has_filled_answers_503_at_the_route(deps, client_for):
    """The app-level half of the filled-pool case.

    `read_pinned_pool` refuses a filled agreement in isolation, but what a
    buyer meets is the route -- and a pool that filled stays configured and
    pinned for the rest of its life, so this is the state the route spends
    most of its time in, not an edge case.

    A full pool is `STATE_FUNDED` with every seat taken, not `STATE_OPEN`
    with every seat taken: the contract's `join` moves a multi-seat `hash`
    agreement to FUNDED as it takes the last seat, so open-and-full is a
    state the chain cannot produce. Seeding it that way would test the
    route against something that never happens.
    """
    agreement_id = 7
    _seed_open_pool(deps, agreement_id, state=STATE_FUNDED, seats=SEATS)
    client = client_for(index_agreement_id=agreement_id)

    response = client.post("/index")

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "no_pool_open"
    assert "payment-required" not in response.headers


def test_a_configured_pool_past_its_deadline_answers_503_at_the_route(deps, client_for):
    """The other way a live pool stops being sellable, reached the same way.
    A deadline that has passed is what `expire` acts on, and a 402 issued
    after it would name an agreement whose `join` asserts `now < deadline`.
    """
    agreement_id = 7
    _seed_open_pool(deps, agreement_id, deadline=1)
    client = client_for(index_agreement_id=agreement_id)

    response = client.post("/index")

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "no_pool_open"
    assert "payment-required" not in response.headers


# --- the chain did not answer ----------------------------------------------
#
# "No pool is open" is a fact the chain stated. A read that failed states
# nothing, and the two must not collapse into one another. `read_agreement`
# raises on any HTTP status but 404 (tests/test_escrow_client.py pins that);
# these are the three callers that have to do something sensible with it.


class RefusingAgreementReader:
    """A reader whose box read fails the way a shared node's does.

    Raising `AlgodHTTPError` rather than returning None is the whole subject:
    the fakes above cannot express the difference, because returning None is
    precisely the conflation being tested against.
    """

    def __init__(self, *, code: int = 429, next_id: int = 40) -> None:
        self._code = code
        self._next = next_id
        self.reads: list[int] = []

    def next_agreement_id(self) -> int:
        return self._next

    def read_agreement(self, agreement_id: int):
        self.reads.append(agreement_id)
        raise AlgodHTTPError("rate limited", self._code)


def test_a_read_that_failed_is_not_reported_as_no_pool_open() -> None:
    """The distinction the route's two 503s rest on. `NoPoolOpen` tells a
    buyer the edition is closed; during a funding window that is the one
    wrong answer that costs a seat, so a chain that did not answer has to
    raise something else."""
    escrow = RefusingAgreementReader()

    with pytest.raises(PoolStatusUnknown):
        read_pinned_pool(escrow, agreement_id=7, share_price=SHARE_PRICE, seats=SEATS)


def test_the_unknown_status_is_not_a_subclass_of_no_pool_open() -> None:
    """Pinned explicitly because the gate distinguishes them with two
    `except` clauses in order: if these ever became related by inheritance
    the first clause would swallow both and the distinction above would go
    silently missing."""
    assert not issubclass(PoolStatusUnknown, NoPoolOpen)
    assert not issubclass(NoPoolOpen, PoolStatusUnknown)


def test_the_operator_scan_propagates_rather_than_reporting_nothing_open() -> None:
    """The unrecoverable direction, and the reason `read_agreement` narrowed.

    `find_any_open_pool` returning None is what the create-pool script reads
    as "nothing open, safe to create". The scan walks every agreement id, one
    request each, against a population that grows with ordinary `/pin`
    traffic -- so a rate limit partway through is a live possibility. If that
    were flattened to None the script would open a second pool beside a live
    one: two lots of real seats against two real commit hashes, with no way
    to merge them back.
    """
    escrow = RefusingAgreementReader()

    with pytest.raises(AlgodHTTPError):
        find_any_open_pool(escrow)

    assert escrow.reads, "the failure must come from a real read, not a short circuit"


def test_a_404_still_reads_as_absent_during_the_scan() -> None:
    """The other half: narrowing must not have turned an ordinary gap in the
    id range into a hard failure. `read_agreement` answers None for a 404,
    and the scan steps over it exactly as before."""
    escrow = FakeAgreementReader({3: _agreement()}, next_id=6)

    pool = find_any_open_pool(escrow)

    assert pool is not None and pool.agreement_id == 3
    assert escrow.reads[:2] == [5, 4], "the absent ids were read and skipped"


def test_the_price_callable_refuses_when_the_chain_cannot_be_read(
    settings_factory,
) -> None:
    """Whatever the gate did, the callable must not quote a seat it could not
    confirm. It collapses both refusals into `PoolClosed` at this point --
    the distinction has already been made where it is answerable."""
    from api.deps import Deps
    from api.pool import make_pool_price_callable
    from tests.conftest import await_price

    deps = Deps(
        settings=_settings(settings_factory, index_agreement_id=7),
        escrow=RefusingAgreementReader(),
        jobs=None,
        algod=None,
        http=None,
    )
    price = make_pool_price_callable(deps)

    with pytest.raises(PoolClosed):
        await_price(price, None)
