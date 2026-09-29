"""The pool a buyer can join right now, read from the chain.

The route trusts a **pinned** agreement id (`Settings.index_agreement_id`)
rather than searching for one. A search bounded to the newest few agreements
sounds safe next to "one pool at a time", but every unpaid `POST /pin` quote
for a new `(sha256, size)` creates an agreement too (`api/pricing.py`) --
unauthenticated, free, and at the configured rate limit fast enough that a
couple of minutes of ordinary `/pin` traffic can push the real open pool
outside any bounded window. A route that fell back to a scan in that case
would answer 503 with nothing wrong on chain, for the rest of a multi-day
funding window. Pinning turns that into a single box read that cannot go
stale for that reason.

There is one scan left, and it is not the route's. `find_any_open_pool`
backs the create-pool script's one-pool-at-a-time guard: unlike the route,
it must not miss a still-open pool just because the *current* edition's
price or seat count has moved on from the one that pool was created with,
and unlike the route it can afford to be slow about it.

Six conditions have to hold before a pool is servable, and each of them
mirrors an assert the contract will apply to the buyer's `join`, or excludes
an agreement that is not this product at all. Advertising a pool that fails
any of them means selling a seat that cannot be taken:

  state is OPEN       -- `join` accepts only OPEN
  condition is hash    -- the other condition releases permissionlessly
  min_seats > 1        -- a single-seat agreement is the delivery escrow,
                          the other product sharing this application; this
                          holds unconditionally, independent of what seat
                          count the caller is looking for
  min_seats == seats   -- the pool is the size this route advertises
  share_price matches  -- `join` asserts the transfer equals it exactly
  now < deadline        -- `join` asserts it

`join` asserts a seventh thing these do not check: that the contract is not
paused. It is left out on purpose. `paused` is a global, not a field of the
agreement box, so reading it costs a second chain round trip on every
unauthenticated request -- to catch a lever only we can pull, and whose
consequence is bounded: a buyer sent at a paused contract has their group
fail atomically, so nothing settles and no money moves. The operator's
create-pool script does read it (`EscrowClient.is_paused`), where it costs
one read per pool and turns a create the contract would reject -- opening an
edition into a paused contract -- into a plain refusal before anything is
signed. A pause is an operator action, and taking the route dark belongs in
the same runbook step.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from algosdk.error import AlgodHTTPError
from x402.schemas import AssetAmount

from api.deps import Deps
from api.escrow import CONDITION_HASH, STATE_OPEN
from api.pricing import SAMPLE_AGREEMENT_ID, attribution_extra


class NoPoolOpen(RuntimeError):
    """No agreement currently satisfies every condition for a joinable seat.

    Raised rather than returning None: the caller's next act is to issue a 402
    naming an agreement id, and a forgotten None check there would name
    agreement 0 and take money against nothing.
    """


class PoolStatusUnknown(RuntimeError):
    """algod did not answer, so whether a pool is open is not known.

    Separate from `NoPoolOpen`, which is a fact the chain stated. Both refuse
    the sale -- never quote a seat that cannot be confirmed -- but only this
    one is transient, and answering a buyer "no edition pool is open" when
    the truth is "we could not reach the chain" is a false negative told
    during the one window where it costs seats. `read_agreement` raises
    rather than returning None for exactly this reason; this is where that
    distinction is turned into something a route can answer with.
    """


class PoolClosed(RuntimeError):
    """The price callable's own refusal, distinct from `NoPoolOpen`.

    `NoPoolOpen` speaks in terms of the chain read; the price callable speaks
    in terms of what it can quote. Kept as a separate type so a caller of
    the price callable never has to know that its refusal is implemented as
    a chain read underneath -- and so that api/app.py's gate, which
    calls the pinned read directly, and the price callable, which wraps it,
    are never confused for the same event by an `except` clause upstream.
    """


@dataclass(frozen=True, slots=True)
class OpenPool:
    agreement_id: int
    share_price: int
    seats_taken: int
    seats_total: int
    deadline: int
    commit_hash: bytes

    @property
    def seats_left(self) -> int:
        return max(0, self.seats_total - self.seats_taken)


class _Reader(Protocol):
    def next_agreement_id(self) -> int: ...
    def read_agreement(self, agreement_id: int): ...


def _is_open_pool_shape(record, moment: int) -> bool:
    """The conditions that make `record` a joinable pool of *some* edition:
    OPEN, condition `hash`, more than one seat, and its deadline has not
    passed. Independent of any particular edition's price or seat count,
    which is what lets `find_any_open_pool` -- the operator's cross-edition
    guard -- share this with the edition-scoped checks below.
    """
    if record.state != STATE_OPEN:
        return False  # `join` accepts only OPEN
    if record.condition != CONDITION_HASH:
        return False  # the other condition releases permissionlessly
    # A single-seat agreement is the delivery escrow, the other product
    # sharing this application. Unconditional: true regardless of what seat
    # count the caller is looking for.
    if record.min_seats <= 1:
        return False
    return record.deadline > moment  # `join` asserts `now < deadline`


def _matches_this_edition(record, *, share_price: int, seats: int, moment: int) -> bool:
    """`_is_open_pool_shape` plus the two conditions specific to one edition:
    the pool must be the size and price this route currently advertises."""
    if not _is_open_pool_shape(record, moment):
        return False
    if record.min_seats != seats:
        return False  # not the pool size this route advertises
    return (
        record.share_price == share_price
    )  # `join` asserts the transfer equals it exactly


def _as_open_pool(agreement_id: int, record) -> OpenPool:
    return OpenPool(
        agreement_id=agreement_id,
        share_price=record.share_price,
        seats_taken=record.seats,
        seats_total=record.min_seats,
        deadline=record.deadline,
        commit_hash=bytes(record.commit_hash),
    )


def read_pinned_pool(
    escrow: _Reader,
    *,
    agreement_id: int,
    share_price: int,
    seats: int,
    now: int | None = None,
) -> OpenPool:
    """The pool this deployment is configured to serve -- one box read, no
    scan, and no way to answer with the wrong agreement.

    `agreement_id` is `Settings.index_agreement_id`. Anything that is not a
    real id -- 0, the shipped default, or a negative left by a mistyped
    setting -- means nothing is configured and refuses immediately without
    touching the chain, the same fail-safe shape as an unset `RESOURCE_URL`.
    Non-positive is rejected here rather than passed on because agreement ids
    are encoded unsigned (`agreement_box_name` packs eight big-endian bytes),
    so a negative would raise `OverflowError` from inside the box read and
    surface as a 500 instead of the 503 this refusal produces. Otherwise this
    reads exactly that one box and applies the six conditions this module's
    docstring lists, so a pinned id that has since filled, expired, or was
    simply mistyped refuses cleanly rather than serving a 402 the contract
    will refuse.
    """
    if agreement_id <= 0:
        raise NoPoolOpen(
            "no pool is configured (INDEX_AGREEMENT_ID is unset or not a "
            "real agreement id); the next edition opens when its agreement "
            "id is set and the deployment is redeployed"
        )
    moment = int(time.time()) if now is None else now
    try:
        record = escrow.read_agreement(agreement_id)
    except AlgodHTTPError as error:
        # Not "no pool": no answer. See `PoolStatusUnknown`.
        raise PoolStatusUnknown(
            f"could not read agreement {agreement_id} from the chain: {error}"
        ) from error
    if record is None:
        raise NoPoolOpen(f"configured agreement {agreement_id} does not exist")
    if not _matches_this_edition(
        record, share_price=share_price, seats=seats, moment=moment
    ):
        raise NoPoolOpen(
            f"configured agreement {agreement_id} no longer satisfies every "
            "condition for a joinable seat"
        )
    return _as_open_pool(agreement_id, record)


def find_any_open_pool(escrow: _Reader, *, now: int | None = None) -> OpenPool | None:
    """Every open multi-seat `hash` agreement, regardless of this edition's
    configured price or seat count. Operator-only.

    `agents/create_pool.py`'s one-pool-at-a-time guard exists to catch a
    still-open pool before a second one is created beside it -- the
    unrecoverable direction, since both would carry a real `commit_hash` and
    real seats. A guard scoped to the *current* `INDEX_SHARE_PRICE_MICRO` /
    `INDEX_SEATS` would look straight past a previous edition's pool the
    moment either setting changes between editions, and report nothing open
    when something still is. So this scans every condition but those two,
    bounded only by `next_agreement_id()` -- the true population size, not a
    cap chosen for a route's response time. A slow scan is fine in a CLI; a
    false "nothing open" here is not.
    """
    moment = int(time.time()) if now is None else now
    newest = escrow.next_agreement_id() - 1

    for agreement_id in range(newest, 0, -1):
        record = escrow.read_agreement(agreement_id)
        if record is None:
            continue
        if not _is_open_pool_shape(record, moment):
            continue
        return _as_open_pool(agreement_id, record)

    return None


def quote_for(settings, pool: OpenPool) -> AssetAmount:
    """The price of one seat in a named pool.

    The amount has to equal the agreement's `share_price` exactly -- the
    contract asserts it on `join` -- so it is taken from the pool record
    rather than from settings, even though the two agree for a correctly
    created pool. Where they disagree, the chain is right.

    `extra` carries the agreement id because the buyer's payment group
    contains their own `join` call, and the client cannot build that call
    without knowing which agreement it is joining.
    """
    return AssetAmount(
        amount=str(pool.share_price),
        asset=str(settings.usdc_asa_id),
        extra={
            # Shared with the delivery escrow's quote on purpose: both
            # products settle into one `payTo`, and a tag that is right on
            # only one of them is unrepairable once either settles.
            **attribution_extra(settings),
            "agreementId": pool.agreement_id,
            "seatsTaken": pool.seats_taken,
            "seatsTotal": pool.seats_total,
            "seatsLeft": pool.seats_left,
            "deadline": pool.deadline,
            "commitSha256": pool.commit_hash.hex(),
        },
    )


def make_pool_price_callable(deps: Deps):
    """The pool route's price: read the pinned pool, or refuse the sale.

    This callable creates nothing. Pools are created out of band by the
    operator, one at a time, so a buyer's request can only ever find one or
    not -- which is why there is no idempotency problem here and no deposit
    parked by a probing agent.

    `PoolClosed` is the in-band signal, not an HTTP status: x402-avm 2.0.2
    catches whatever this raises and turns it into a generic 500 (see
    api/app.py's long comment on that limitation), so the *real* refusal a
    buyer sees comes from api/app.py's gate, which reads the pinned pool
    itself before the payment middleware is ever reached. This still has to
    refuse on its own rather than assume the gate always catches it first: a
    pool can close in the gap between the gate's read and this one, and
    falling through in that gap would quote a 402 naming an agreement the
    contract's own `join` will then refuse.
    """
    settings = deps.settings

    async def price(ctx):
        try:
            # algod is synchronous throughout; off the event loop for the
            # same reason api/pricing.py's create path is -- a chain read
            # here would otherwise stall every other request in the process,
            # /healthz included, for as long as it took to answer.
            pool = await asyncio.to_thread(
                read_pinned_pool,
                deps.escrow,
                agreement_id=settings.index_agreement_id,
                share_price=settings.index_share_price_micro,
                seats=settings.index_seats,
            )
        except (NoPoolOpen, PoolStatusUnknown) as error:
            # Never a payable 402 with no agreement behind it -- and an
            # unreadable chain is no more a reason to quote one than a closed
            # pool is. The two are kept distinct up to here so the gate can
            # say which happened; at the point of quoting they refuse alike.
            raise PoolClosed(str(error)) from error
        return quote_for(settings, pool)

    # Inspectable without a chain read, so a cold start with the chain
    # unreachable still boots and the guard can still see `extra`. Mirrors
    # api/pricing.py's price.sample -- see its comment for why running the
    # real callable at boot is the wrong thing to do; here it costs no
    # deposit, but it would still cost a chain round trip on every restart.
    price.sample = lambda: quote_for(
        settings,
        OpenPool(
            agreement_id=SAMPLE_AGREEMENT_ID,
            share_price=settings.index_share_price_micro,
            seats_taken=0,
            seats_total=settings.index_seats,
            deadline=0,
            commit_hash=b"\x00" * 32,
        ),
    )
    return price
