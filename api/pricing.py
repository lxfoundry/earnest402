"""The price callable: the only place an agreement is created.

x402-avm 2.0.2 runs this on both passes -- the unpaid request that earns the
402 and the paid retry -- and the two are distinguished by
HTTPRequestContext.payment_header, which is populated only on the second. The
adapter's get_body() returns None, hardcoded, so through the adapter's
interface the input comes from the query string -- even though the route's
discovery declaration names a body, the only shape the catalogue lists a POST
route under (see `_declare_post_discovery` in api/routes.py).
"""

from __future__ import annotations

import asyncio
import re

from x402.http.types import HTTPRequestContext
from x402.mechanisms.avm import DEFAULT_DECIMALS
from x402.schemas import AssetAmount

from api.config import CHALLENGE_TAG
from api.deps import Deps

SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class BadQuoteRequest(ValueError):
    """The query does not describe a file this endpoint can quote for."""


class QuoteCeilingReached(RuntimeError):
    """Too many agreements are parked unfunded to open another."""


class RateLimited(RuntimeError):
    """One source is quoting faster than the policy allows."""


class SourceLimiter:
    """A rolling one-minute window per source.

    An ordinary 402 probe parks a deposit -- which is expected rather than
    abusive, since probing a paywalled route is what a discovering agent does
    and the catalogue row exists to invite it. The ceiling bounds the total;
    this bounds how fast any one caller can reach it.

    With the shipped defaults (quotes_per_source_per_minute=20,
    max_unfunded_agreements=50), a single source can never reach the ceiling
    on its own: the limiter always binds first, at its 21st request from that
    source. The ceiling only comes into play across three or more distinct
    sources (2 * 20 = 40 < 50 <= 3 * 20 = 60). That is by design -- the
    ceiling bounds the aggregate, the limiter bounds any one caller -- not an
    argument for retuning either number.

    **This counter lives in the process**, so the effective per-source rate is
    the configured number multiplied by the worker count. The ceiling does not
    share that property: it is a query against the store, which every worker
    shares, so the aggregate bound holds however many workers there are. That
    asymmetry is why the ceiling is the guarantee and the limiter is only a
    speed bump; a limiter that had to hold across workers would need the shared
    store, and it is not worth a write per unpaid probe to get it.
    """

    def __init__(self, per_minute: int) -> None:
        self._per_minute = per_minute
        # Keyed on client-supplied input (an X-Forwarded-For value), so this
        # dict has to stay bounded to sources active in roughly the last
        # minute, not grow by one entry per source ever seen -- otherwise a
        # caller that spoofs a fresh source on every request, or simply never
        # comes back, leaves a permanent key behind forever.
        self._seen: dict[str, list[int]] = {}

    def check(self, source: str, now: int) -> None:
        # Evict every source whose whole window has elapsed, not just the
        # caller's: check() is the only place this dict is touched, so a
        # source that has gone quiet is never looked at again on its own --
        # nothing would ever prune it if pruning were scoped to `source`
        # alone. Sweeping here keeps the dict sized to "sources seen in the
        # last minute" instead of "sources ever seen".
        # Timestamps are only ever appended here, in call order, so the
        # last one decides recency -- no need to scan a source's whole list.
        cutoff = now - 60
        for other, timestamps in list(self._seen.items()):
            if timestamps[-1] <= cutoff:
                del self._seen[other]
        recent = [t for t in self._seen.get(source, []) if t > cutoff]
        if len(recent) >= self._per_minute:
            raise RateLimited(f"{source} exceeded {self._per_minute} quotes per minute")
        recent.append(now)
        self._seen[source] = recent


def validate_quote(
    sha256: str | None, size: str | int | None, max_file_bytes: int
) -> tuple[str, int]:
    """The purchase's input rules, over raw values from any source.

    Split out from `parse_quote` so the same rules can be applied to a
    Starlette request's query parameters, outside the payment middleware,
    where a rejection can still become a real 400 (see api/app.py). Both
    callers must use *these* rules and not a paraphrase of them: a gate that
    disagrees with the price callable would either reject a purchase the
    callable would have honoured, or pass one it will refuse a moment later
    as an opaque 500.

    A duplicated query parameter needs no handling here. Starlette's
    QueryParams.get and the FastAPI adapter's get_query_param both return the
    last value for a repeated key -- never a list -- so `?size=1&size=2`
    arrives as a single string by the time it reaches either caller.
    """
    if not sha256 or not SHA256_HEX.match(sha256):
        raise BadQuoteRequest("sha256 must be 64 lowercase hex characters")
    try:
        size_int = int(size)
    except (TypeError, ValueError) as exc:
        raise BadQuoteRequest("size must be an integer number of bytes") from exc
    if size_int < 1:
        raise BadQuoteRequest("size must be positive")
    if size_int > max_file_bytes:
        raise BadQuoteRequest(f"size exceeds the maximum of {max_file_bytes} bytes")
    return sha256, size_int


def parse_quote(ctx: HTTPRequestContext, max_file_bytes: int) -> tuple[str, int]:
    """`validate_quote` over an x402 request context."""
    return validate_quote(
        ctx.adapter.get_query_param("sha256"),
        ctx.adapter.get_query_param("size"),
        max_file_bytes,
    )


def open_quote(deps: Deps, sha256: str, size: int):
    """The one definition of 'a quote still reusable'.

    Shared by the price callable's two passes and by the paid handler, so the
    clock and the funding window are read in one place rather than restated at
    each call site -- the reuse lookup and the hash-status lookup have to agree
    about which rows are still live.
    """
    return deps.jobs.find_reusable_quote(
        sha256,
        size,
        now=deps.now(),
        window=deps.settings.funding_window_seconds,
    )


def make_price_callable(deps: Deps):
    settings = deps.settings

    limiter = SourceLimiter(settings.quotes_per_source_per_minute)

    async def price(ctx: HTTPRequestContext) -> AssetAmount:
        sha256, size = parse_quote(ctx, settings.max_file_bytes)

        if ctx.payment_header is not None:
            # The paid retry. find_matching_requirements compares scheme,
            # network, amount, asset and payTo -- never extra -- so this pass
            # only has to reproduce the same requirements. It creates nothing,
            # and it is not rate limited: rejecting it here would reject a
            # payment the buyer has already signed.
            job = open_quote(deps, sha256, size)
            if job is None:
                raise BadQuoteRequest(
                    "no open agreement for this file; request a quote first"
                )
            return _quote(settings, job.agreement_id)

        limiter.check(_source_of(ctx), deps.now())

        # Not a plain await. Creating an agreement spends minimum balance and
        # is not undoable, so once it has started it must be allowed to finish
        # even if the caller goes away -- and callers do go away: BaseHTTPMiddleware
        # cancels this task when the client disconnects, and hook_timeout_seconds
        # cancels it on a slow chain. Abandoning the coroutine mid-create would
        # release create_lock while the worker thread is still submitting (a
        # thread cannot be cancelled), letting the next create read the same
        # agreement_count and fail on an existing roster box. Running it to
        # completion instead means the quote is simply finished without its
        # requester -- and the next buyer for the same file reuses it, because
        # the create is idempotent on (sha256, size).
        return await _run_to_completion(_open_or_create(deps, sha256, size))

    # What the startup guard inspects instead of calling `price` for real.
    #
    # The guard exists to check that the served price carries the challenge tag
    # and a fee payer, both of which are only observable by producing an
    # AssetAmount. Producing one by *running the callable* means creating an
    # agreement: every cold start more than one funding window after the last
    # parks a real deposit and writes a QUOTED row, and count_unfunded() has no
    # window filter -- so a restart loop walks the ceiling and eventually
    # refuses to boot at all, with no way out until the sweeper exists.
    #
    # This returns the same object the callable returns, from the same
    # `_quote`, with no chain call and no store write. It is an attribute on
    # the callable rather than a header the sampler could set, because a header
    # is client input: any caller could then ask for a priced response that
    # skipped the agreement.
    #
    # tests/test_pricing.py pins this against the real callable's output, so
    # the two cannot drift into disagreeing about `extra`.
    price.sample = lambda: _quote(settings, SAMPLE_AGREEMENT_ID)

    return price


# Stands in for a real agreement id in the boot sample. Never written to chain
# and never handed to a buyer: agreement ids start at 1, so this cannot collide
# with one.
SAMPLE_AGREEMENT_ID = 0


# The one header on this deployment that the caller cannot write. Fly's proxy
# sets it itself, single-valued, from the connection it terminated. It is
# platform-specific by nature: change the platform and this name changes with
# it, or the limiter silently falls back to the weaker source below.
TRUSTED_CLIENT_IP_HEADER = "fly-client-ip"


def _source_of(ctx: HTTPRequestContext) -> str:
    """The caller, as far as the request can honestly say.

    Behind a TLS terminator the peer address is the proxy, so a forwarded
    header is the only signal available -- which is why uvicorn runs with
    --proxy-headers. The question is *which* forwarded value, and the obvious
    answer is the wrong one.

    X-Forwarded-For is a list that each proxy **appends** to, so its leftmost
    entry is whatever the original caller sent and its rightmost is what the
    nearest trusted proxy observed. Reading the leftmost entry -- and this
    deployment runs with --forwarded-allow-ips '*' behind a proxy that
    appends -- makes the rate-limit key client-controlled: a caller that
    varies a spoofed first hop per request is never limited at all, and each
    request it gets through parks an ALGO deposit that only the sweeper
    reclaims. So the rightmost entry is taken, never the leftmost.

    Preferred over either is a single-valued header the proxy sets itself,
    which cannot be extended by the caller at all. If that header is absent
    the rightmost X-Forwarded-For entry is the fallback, and a direct request
    with no proxy in front of it degrades to one shared "unknown" bucket --
    strict rather than permissive, which is the right direction for a limiter.
    """
    trusted = ctx.adapter.get_header(TRUSTED_CLIENT_IP_HEADER)
    if trusted:
        return trusted.strip()
    forwarded = ctx.adapter.get_header("x-forwarded-for")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    return ctx.adapter.get_header("x-real-ip") or "unknown"


def attribution_extra(settings) -> dict:
    """The three `extra` keys every priced route on this server must carry.

    `extra` belongs to the price and to nothing else: the scheme builds the
    settled `PaymentRequirements.extra` from here, so an accepts-level extra
    or a bare "$0.01" string price silently drops `feePayer` and the
    challenge tag, and the payment settles unattributed.

    Built here rather than written out per route because the failure is
    unrepairable in one direction: attribution is stamped at a route's first
    settlement and never reclassified, and both products settle into the same
    `payTo`. Two literals that agree today are two literals that can drift;
    one function cannot.
    """
    return {
        "decimals": DEFAULT_DECIMALS,
        "feePayer": settings.fee_payer,
        "tag": CHALLENGE_TAG,
    }


def _quote(settings, agreement_id: int) -> AssetAmount:
    return AssetAmount(
        amount=str(settings.price_micro_usdc),
        asset=str(settings.usdc_asa_id),
        extra={**attribution_extra(settings), "agreementId": agreement_id},
    )


def _run_to_completion(coro):
    """Await work that must not be abandoned part-way through.

    asyncio.shield detaches the *waiting* from the *doing*: a cancellation
    delivered here stops this coroutine from waiting, while the task carrying
    the work keeps running to completion on its own.
    """
    task = asyncio.ensure_future(coro)
    task.add_done_callback(_consume_orphaned_result)
    return asyncio.shield(task)


def _consume_orphaned_result(task: asyncio.Task) -> None:
    """Retrieve the result of a create nobody is waiting for any more.

    An abandoned task that raised and was never awaited makes asyncio log
    "Task exception was never retrieved" at some arbitrary later point, which
    reads like an unhandled crash in an unrelated request. Retrieving it here
    is the whole purpose; there is nowhere to report it, because the caller
    this belonged to is gone.
    """
    if not task.cancelled():
        task.exception()


async def _open_or_create(deps: Deps, sha256: str, size: int) -> AssetAmount:
    """Requirements for a live quote for these bytes, creating one if needed.

    Serialised by deps.create_lock, which is what makes the reuse check, the
    ceiling check and the create one decision rather than three racing ones.
    """
    settings = deps.settings
    async with deps.create_lock:
        job = open_quote(deps, sha256, size)
        if job is not None:
            return _quote(settings, job.agreement_id)

        if deps.jobs.count_unfunded() >= settings.max_unfunded_agreements:
            raise QuoteCeilingReached(
                "too many unfunded agreements; every quote parks a deposit "
                "and the capacity to quote is the binding constraint"
            )

        now = deps.now()
        deadline = now + settings.delivery_deadline_seconds

        # Write the intent before spending anything. The create is what
        # locks up minimum balance, and its id only becomes knowable *from*
        # the create -- so between the create returning and create_quote
        # committing there is a gap in which the agreement exists on chain
        # and nothing in the store refers to it. A cancellation lands in
        # exactly that gap (the request below is awaited, and a client
        # disconnect or a hook timeout cancels the task there), and so does
        # a crash or a redeploy. Without this row the result is an
        # agreement holding a deposit that nothing enumerating `jobs` can
        # find, reclaimable only by hand.
        intended_id = await asyncio.to_thread(deps.escrow.next_agreement_id)
        deps.jobs.record_create_intent(
            agreement_id=intended_id,
            sha256=sha256,
            size=size,
            requested_at=now,
        )

        # algosdk is synchronous throughout, and this call waits for
        # confirmation -- several seconds of blocking round trips. On the
        # event loop that stalls every other request in the process,
        # /healthz included, so it runs in a worker thread. create_lock
        # still serialises creation: the agreement id is read from chain
        # and reserved by this call.
        agreement_id = await asyncio.to_thread(
            deps.escrow.create_hash_agreement,
            commit_hash=bytes.fromhex(sha256),
            share_price=settings.price_micro_usdc,
            deadline=deadline,
            beneficiary=settings.treasury,
            verifier=settings.verifier,
        )
        deps.jobs.create_quote(
            agreement_id=agreement_id,
            sha256=sha256,
            size=size,
            deadline=deadline,
            quoted_at=now,
        )
        # The job row now names the agreement, so the intent has nothing
        # left to protect. Both ids are cleared because the prediction can
        # be wrong: another process creating concurrently shifts the real
        # id, and leaving the predicted one behind would send a reconciler
        # after an agreement that was never created.
        deps.jobs.clear_create_intent(intended_id)
        if agreement_id != intended_id:
            deps.jobs.clear_create_intent(agreement_id)
        return _quote(settings, agreement_id)
