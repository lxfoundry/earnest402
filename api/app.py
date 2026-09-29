"""Assembles the FastAPI application from already-built collaborators.

Kept separate from api/server.py -- the only module that reads the
environment or opens a network connection -- so the app can be constructed
in a test against fake collaborators (a Deps built from settings_factory and
FakeEscrow/FakeAlgodForPricing) with no network involved. api/server.py is
the thin entrypoint: it builds the real Deps and routes, runs the startup
guard, and calls create_app().
"""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from x402.extensions.bazaar import bazaar_resource_server_extension
from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.http.x402_http_server_base import x402HTTPServerBase
from x402.mechanisms.avm.exact import ExactAvmServerScheme
from x402.server import x402ResourceServer

from api.deps import Deps
from api.jobs import IllegalTransition
from api.payment_group import MalformedPaymentGroup, require_join_call
from api.pool import NoPoolOpen, PoolStatusUnknown, read_pinned_pool
from api.pricing import (
    BadQuoteRequest,
    QuoteCeilingReached,
    RateLimited,
    validate_quote,
)
from api.web import ClientBundle


def _race_lost(sha256: str, because: str) -> dict:
    """The loser's 409, carrying both outcomes rather than one.

    Spec §4: the two outcomes are not equivalent, and the difference is not
    the client's to hide. Re-quoting is a purchase -- a second, independently
    escrowed pin with its own deadline and its own refund guarantee. Attaching
    to the winner's job costs nothing and guarantees nothing: the attached
    party has no agreement, no deadline and no claim, and if that job ends
    abandoned or refunded they are left with nothing after being told the file
    was on its way. Both are honest offers; only one of them is a purchase.

    The attach option deliberately names no URL. The hash-addressed status
    query that serves it is §4b's route, which is not mounted here, and a body
    advertising a path that 404s would be worse than one that states the
    choice plainly. It gains the link when that route ships.
    """
    return {
        "reason": f"another payer holds this agreement's single seat ({because})",
        "charged": False,
        "sha256": sha256,
        "options": [
            {
                "outcome": "requote",
                "how": "POST /pin?sha256=<sha256>&size=<bytes>",
                "costs": "the full price again",
                "guarantees": (
                    "a second, independently escrowed pin, with its own "
                    "deadline and its own on-chain refund if it misses"
                ),
            },
            {
                "outcome": "attach",
                "how": "the hash-addressed status query for this sha256",
                "costs": "nothing",
                "guarantees": (
                    "nothing -- no agreement, no deadline and no refund "
                    "claim; if the winner's job ends abandoned or refunded, "
                    "nothing is owed to you"
                ),
            },
        ],
    }


def _pool_changed_underneath(
    requested_agreement_id: int, funded_agreement_id: int
) -> dict:
    """The 409 body for a buyer whose declared `agreementId` -- the one
    optional input in `INDEX_INPUT_SCHEMA` -- disagrees with the agreement
    the verified payment was actually matched to.

    Reachable because the price callable re-reads the open pool on every
    request, the paid retry included: between the 402 that named
    `requested_agreement_id` and this retry, that pool can have closed --
    filled, or run past its deadline -- and the next edition's pool can have
    opened in its place. The buyer's own signed `join` call, sitting in the
    same settlement group as this payment, still names the old agreement in
    that case; the group fails atomically on chain and no money moves. This
    check does not lean on that outcome -- it refuses to hand back a receipt
    naming a pool the caller did not ask to join, regardless of what the
    chain would have done with the group.
    """
    return {
        "reason": "the verified payment funded a different pool than requested",
        "requestedAgreementId": requested_agreement_id,
        "fundedAgreementId": funded_agreement_id,
    }


# `POST /index`'s whole body is one optional small integer. The body is read
# in the chunks the server delivers and refused once it passes this limit, so
# the handler holds at most the limit plus one chunk, never an arbitrarily
# large body.
INDEX_BODY_LIMIT_BYTES = 1024


def _unreadable_body(reason: str) -> HTTPException:
    return HTTPException(
        status_code=400, detail={"error": "unreadable_body", "reason": reason}
    )


async def _index_agreement_id_requested(
    request: Request, from_query: int | None
) -> int | None:
    """The agreement a `POST /index` caller named, from the query string or a
    JSON body -- or None if they named none.

    The Bazaar declaration describes this route's input as a JSON body,
    because that is the only shape the catalogue lists a POST route under,
    while the query string is what this route was built on and what its 402
    declared before, so a caller written against that earlier declaration
    may send it. Both are read so that the declaration is true and nothing
    that already works stops working. The body is read whatever its
    Content-Type says: a caller that omits or mislabels it still meant the
    id it sent.

    Anything that might have named a pool and cannot be read as one is
    refused rather than ignored: ignoring it would hand back a receipt for
    whichever pool is open, the outcome `_pool_changed_underneath`'s 409
    exists to prevent. Every refusal here is >= 400, so PaymentMiddlewareASGI
    never settles the payment.
    """
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > INDEX_BODY_LIMIT_BYTES:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": "body_too_large",
                    "reason": f"the body exceeds {INDEX_BODY_LIMIT_BYTES} bytes",
                },
            )

    from_body = None
    if raw.strip():
        try:
            # ValueError alone is enough while the limit above holds: json
            # raises RecursionError only at a nesting depth in the thousands,
            # which a body this short cannot reach. A RecursionError would
            # still be a 500, and nothing settles on a 500.
            parsed = json.loads(raw)
        except ValueError as error:
            raise _unreadable_body("the body is not JSON") from error
        if not isinstance(parsed, dict):
            raise _unreadable_body("the body is not a JSON object")
        if "agreementId" in parsed:
            from_body = parsed["agreementId"]
            # `type(...) is int`, not isinstance: a JSON `true` is a Python
            # bool, which isinstance would accept as the agreement id 1.
            if type(from_body) is not int:
                raise _unreadable_body("agreementId is not an integer")

    if from_query is not None and from_body is not None and from_query != from_body:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "conflicting_agreement_id",
                "reason": "the query string and the body name different pools",
                "queryAgreementId": from_query,
                "bodyAgreementId": from_body,
            },
        )
    return from_body if from_body is not None else from_query


def _verified_requirements(request: Request, route: str):
    """`request.state.payment_requirements`, or a named failure.

    Set by PaymentMiddlewareASGI on every verified request, so its absence
    here is unreachable through normal traffic. Named rather than left as an
    AttributeError so that mounting a paid route unpaid by accident -- a
    router change that bypasses the middleware -- says what is actually
    wrong. Shared between every paid handler so the check can never drift
    into disagreeing with itself between routes.
    """
    requirements = getattr(request.state, "payment_requirements", None)
    if requirements is None:
        raise RuntimeError(f"{route} was served without a verified payment")
    return requirements


def _require_seat_is_actually_bought(
    request: Request, *, app_id: int, agreement_id: int
):
    """Refuse a verified payment whose group would not record the seat.

    The one exposure neither the facilitator nor the contract closes: a group
    carrying the transfer and no `join` settles into the application account
    against nothing, and the USDC cannot be recovered afterwards by anyone.
    See api/payment_group.py for why, and why here is the only place it can
    be caught -- this handler runs before process_settlement(), and any
    response >= 400 stops the settlement from happening at all.

    400, not 409: nothing raced and nothing changed underneath. The group the
    client built does not buy what the client asked to buy, and the fix is on
    their side.
    """
    payload = getattr(request.state, "payment_payload", None)
    group = []
    if payload is not None:
        group = (getattr(payload, "payload", None) or {}).get("paymentGroup") or []
    try:
        require_join_call(group, app_id=app_id, agreement_id=agreement_id)
    except MalformedPaymentGroup as error:
        raise HTTPException(
            status_code=400,
            detail={"error": "payment_group_incomplete", "reason": str(error)},
        ) from error


def create_app(deps: Deps, routes: dict[str, RouteConfig]) -> FastAPI:
    settings = deps.settings

    facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=settings.facilitator_url))
    server = x402ResourceServer(facilitator)
    server.register(settings.network, ExactAvmServerScheme())
    server.register_extension(bazaar_resource_server_extension)

    # redirect_slashes=False is a payment-integrity setting, not a style
    # preference. x402HTTPServerBase._normalize_path does
    # re.sub(r"/+", "/", path).rstrip("/"), so "/pin/", "/pin//" and "//pin"
    # all normalise onto the paid pattern "POST /pin" and are matched for
    # payment. With FastAPI's default redirect_slashes=True the router then
    # answers "/pin/" with a 307 to "/pin" -- and PaymentMiddlewareASGI only
    # declines to settle on a response >= 400 ("Don't settle on error
    # responses"). A 307 is not an error, so the payment settles against a
    # redirect: the buyer is charged, receives no jobId, and the row stays
    # QUOTED while the chain says FUNDED -- a divergence the sweeper cannot
    # repair, since it drives `expire` off QUOTED rows and `expire` fails on a
    # funded agreement. Turning the redirect off makes those paths 404, which
    # is >= 400, so settlement is skipped and nothing moves.
    app = FastAPI(title="Earnest delivery escrow", redirect_slashes=False)

    app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)

    # `/pin` is mounted from the route table, never from the setting that
    # built it: a handler with no payment requirement in front of it, or a
    # requirement with no handler behind it, is not a state this app can be
    # constructed in. Its query gate goes with it -- left mounted, it would
    # answer a malformed `/pin` with a 400 for a route that does not exist.
    pin_served = "POST /pin" in routes

    # -- The query gate, mounted deliberately *outside* the payment middleware.
    #
    # Starlette's add_middleware does user_middleware.insert(0, ...) and
    # build_middleware_stack wraps in reversed(user_middleware), so the
    # middleware added *last* ends up outermost. This one is therefore reached
    # before PaymentMiddlewareASGI, which is the only place a malformed query
    # can still be answered with a real 400: once the request is inside the
    # payment middleware, anything the price callable raises is swallowed into
    # a generic 500 that never reaches an exception handler (see the note
    # below).
    #
    # Scope is deliberately narrow. It re-uses validate_quote -- the price
    # callable's own rules, not a paraphrase -- and it is pure: no agreement,
    # no store write, no counter touched. The ceiling and the rate limiter are
    # *not* checked here even though they would also read better than a 500,
    # because both are stateful: the limiter would count this pass and the
    # callable's pass as two requests from one caller, and start refusing at
    # half the configured rate.
    async def _gate_quote_query(request: Request, call_next):
        # Matched through the same normalisation PaymentMiddlewareASGI itself
        # applies, not by bare equality: "/pin/" and "/pin//" are folded onto
        # the paid pattern and demand money, so a malformed query under those
        # names would otherwise slip past this gate and come back as the
        # generic 500 it exists to avoid.
        if (
            request.method == "POST"
            and x402HTTPServerBase._normalize_path(request.url.path) == "/pin"
        ):
            try:
                validate_quote(
                    request.query_params.get("sha256"),
                    request.query_params.get("size"),
                    settings.max_file_bytes,
                )
            except BadQuoteRequest as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
        return await call_next(request)

    if pin_served:
        app.middleware("http")(_gate_quote_query)

    # -- The pool gate, mounted for the same reason and the same way as the
    # query gate above: outside PaymentMiddlewareASGI, so it can still answer
    # for itself instead of falling into the price callable's generic-500
    # path (see the comment block below this one).
    #
    # Unlike the query gate, this one is not pure -- it reads the chain to
    # find the open pool, the same read make_pool_price_callable's `price`
    # does a moment later inside the payment middleware. That duplication is
    # deliberate rather than an oversight: the alternative is threading a
    # result from this middleware into the price callable, and nothing in
    # x402-avm's request context offers that hand-off. Two reads is the
    # price of a real 503 instead of the generic one. And the read itself
    # runs in a worker thread: algod is synchronous, and a blocking chain
    # call on the event loop stalls every other request in the process,
    # /healthz included, for as long as it takes to answer.
    #
    # A pool can close in the gap between this read and the price callable's
    # -- another buyer's join lands, or the deadline passes, in between the
    # two. When that happens this gate has already let the request through,
    # so the buyer gets a 402 naming an agreement whose `join` the contract
    # will then refuse. No money moves: the buyer's own `join` call is in the
    # same settlement group as their payment, so the group fails together
    # and the facilitator never settles either leg.
    #
    # Matched through x402HTTPServerBase._normalize_path rather than a bare
    # equality check, for the reason _gate_quote_query does the same and
    # tests/test_payment_integrity.py pins for /pin:
    # PaymentMiddlewareASGI folds "/index/", "/index//" and "//index" onto
    # the paid pattern "POST /index", so a caller can reach the paid route
    # under a name this gate would otherwise miss -- and with the gate
    # bypassed, a closed pool reaches the price callable and comes back as
    # the generic 500 this gate exists to avoid.
    @app.middleware("http")
    async def _gate_pool_availability(request: Request, call_next):
        if (
            request.method == "POST"
            and x402HTTPServerBase._normalize_path(request.url.path) == "/index"
        ):
            try:
                await asyncio.to_thread(
                    read_pinned_pool,
                    deps.escrow,
                    agreement_id=settings.index_agreement_id,
                    share_price=settings.index_share_price_micro,
                    seats=settings.index_seats,
                )
            except NoPoolOpen:
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "no_pool_open",
                        "detail": (
                            "No pool is open. Pools open one at a time, and "
                            "each funds its own committed file; try again "
                            "later."
                        ),
                    },
                )
            except PoolStatusUnknown:
                # Same refusal, different fact, and the difference is worth a
                # distinct code: `no_pool_open` tells a buyer the edition is
                # closed, which during a funding window is the one wrong
                # answer that costs a seat. This says only that we could not
                # ask. Retry-After because, unlike a closed pool, this one
                # resolves on its own.
                return JSONResponse(
                    status_code=503,
                    headers={"Retry-After": "5"},
                    content={
                        "error": "pool_status_unavailable",
                        "detail": (
                            "The pool's status could not be read from the "
                            "chain just now. This is transient and says "
                            "nothing about whether a pool is open; "
                            "retry shortly."
                        ),
                    },
                )
        return await call_next(request)

    # -- The browser client, added after every other middleware and therefore
    # outermost: a request under `/` or `/app` is answered there and reaches
    # neither the payment middleware nor either gate above. See api/web.py.
    if Path(settings.web_dist_dir).is_dir():
        app.add_middleware(ClientBundle, dist_dir=settings.web_dist_dir)

    # -- Known limitation, x402-avm 2.0.2: these three handlers cannot fire
    # for anything the price callable raises. --------------------------------
    #
    # PaymentMiddlewareASGI is mounted with app.add_middleware(), so it runs
    # *outside* ExceptionMiddleware, which is where @app.exception_handler
    # lives (closer to the router). Inside the middleware,
    # x402HTTPResourceServer.process_http_request drives a generator
    # (_process_request_core in x402's x402_http_server_base.py) through a
    # "resolve_options" phase that calls the price callable. That phase's
    # own try/except catches *any* exception the callable raises -- our
    # BadQuoteRequest, QuoteCeilingReached, RateLimited, anything -- converts
    # it to HTTPProcessResult(type=RESULT_PAYMENT_ERROR, response=
    # HTTPResponseInstructions(status=500, body={"error": "Failed to process
    # request"})), and PaymentMiddlewareASGI turns that into a plain
    # JSONResponse. Nothing is ever re-raised, so ExceptionMiddleware -- and
    # these three handlers -- are never reached. Confirmed by reading that
    # code path and by a standalone probe: a price callable that always
    # raises, run through this exact app shape, returns 500 with body
    # {"error": "Failed to process request"} regardless of the exception's
    # type, with the matching handler never invoked.
    #
    # Left registered rather than removed: they are correct for an exception
    # raised anywhere reachable through the router (a path operation, a
    # dependency), and they document the *intended* mapping even though nothing
    # here currently raises these types from inside a route. The gap is
    # pinned by tests/test_error_handling.py, which asserts the actual
    # (500, generic-body) behaviour rather than the mapping below -- so it
    # fails, on purpose, the day this gets fixed. Task 7 exercises leg 1 end
    # to end and is where that fix belongs.

    @app.exception_handler(BadQuoteRequest)
    async def _bad_quote(request: Request, exc: BadQuoteRequest) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(QuoteCeilingReached)
    async def _ceiling(request: Request, exc: QuoteCeilingReached) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(RateLimited)
    async def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    # This one is *not* inert, unlike the three above: IllegalTransition is
    # raised by the job store from inside the pin handler, which is reachable
    # through the router, so ExceptionMiddleware does see it. Without this a
    # buyer who lost the race across two worker processes would get a 500 --
    # money-safe, since the middleware skips settlement on anything >= 400,
    # but indistinguishable from a server fault, and carrying none of the two
    # outcomes the spec requires the loser be offered.
    @app.exception_handler(IllegalTransition)
    async def _illegal_transition(
        request: Request, exc: IllegalTransition
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={
                "detail": _race_lost(
                    request.query_params.get("sha256", ""),
                    "the seat is already funded",
                )
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        """Unpaid liveness probe. Never completes a payment.

        `indexAgreementId` is the one configured value a deployment cannot
        otherwise be caught getting wrong. The boot guard samples the pool
        price callable without reading the chain -- deliberately, so a cold
        start with algod unreachable still boots -- which means a mistyped or
        stale `INDEX_AGREEMENT_ID` boots green and then answers 503 forever
        with nothing wrong on chain. Reported, never checked: no chain read
        happens here, because this endpoint is polled and the pool route's
        own gate is where the chain gets consulted. 0 means no pool is
        configured, which is the shipped default and not an error.
        """
        return {
            "status": "ok",
            "indexAgreementId": settings.index_agreement_id,
        }

    async def pin(request: Request, sha256: str, size: int) -> dict:
        """Reached after the payment is *verified*, and before it is settled.

        The jobId is minted here and never in the 402: two buyers quoting the
        same file are handed the same agreement, so anything carried in that
        402 is shared with a stranger. The token is minted for whoever
        actually paid.

        The agreement is read from the verified payment rather than looked up
        again from the query. request.state.payment_requirements is what
        PaymentMiddlewareASGI matched this buyer's payment against, so its
        extra.agreementId is, by construction, the agreement the money is for.
        Re-deriving it here with a second store lookup would be a different
        question -- "which quote is open for these bytes now" -- and the two
        can disagree: with more than one worker process there can be two
        in-window QUOTED rows for one (sha256, size), and find_reusable_quote
        picks the lower id while the payment may have been matched to the
        other.

        Known window, not fixed here -- it needs a settlement the backend
        controls, which is Task 7's harness:

        PaymentMiddlewareASGI settles *after* this handler returns (it calls
        call_next(), gets our response, then calls process_settlement()), so
        mint_job_id below runs and the 200 goes out *before* settlement is
        known to have succeeded. If settlement then fails -- the facilitator
        rejects it, a network error, anything -- the row this call just wrote
        is stuck FUNDED, carrying a jobId, for an agreement nobody actually
        paid into. The sweeper will later find a FUNDED job with zero seats
        on chain and needs to know what that means.

        The reverse race costs the loser nothing, though not for the reason
        this comment used to give. Within one event loop there is no await
        between reading the agreement and minting against it, so two requests
        in one process cannot interleave here at all. Across processes sharing
        the store they can, and then the loser's mint is refused by the state
        machine (FUNDED -> FUNDED is not a transition) and becomes the 409
        below. Either way the loser is not charged: 409 is >= 400, and
        PaymentMiddlewareASGI skips process_settlement() on any response
        >= 400.
        """
        requirements = _verified_requirements(request, "POST /pin")
        agreement_id = (requirements.extra or {}).get("agreementId")
        if agreement_id is None:
            raise RuntimeError("the verified payment carries no agreementId")

        _require_seat_is_actually_bought(
            request, app_id=settings.app_id, agreement_id=int(agreement_id)
        )

        paid_for = deps.jobs.get_by_agreement(int(agreement_id))
        if paid_for is None:
            raise HTTPException(
                status_code=409, detail=_race_lost(sha256, "agreement is unknown here")
            )
        if paid_for.sha256 != sha256 or paid_for.size != size:
            # The payment was matched to an agreement for different bytes than
            # this request asks about. Not reachable while the price callable
            # is the only thing that builds requirements, and refused rather
            # than reconciled: minting against either one would be a guess.
            raise HTTPException(
                status_code=409,
                detail=_race_lost(sha256, "agreement is for a different file"),
            )

        job = deps.jobs.mint_job_id(
            agreement_id=paid_for.agreement_id, job_id=secrets.token_hex(16)
        )
        return {
            "jobId": job.job_id,
            "agreementId": job.agreement_id,
            "deadline": job.deadline,
        }

    if pin_served:
        app.post("/pin")(pin)

    @app.post("/index")
    async def index(request: Request, agreementId: int | None = None) -> dict:
        """Reached after the payment is *verified*, and before it is settled.

        Everything below comes from request.state.payment_requirements --
        what PaymentMiddlewareASGI matched this buyer's payment against on
        the paid pass, by re-running make_pool_price_callable's price().
        Its extra is, by construction, the pool this payment is for; there
        is no second chain read here to disagree with it.

        `seatsTaken` is not part of this response, on purpose. This buyer's
        own `join` call sits in the same settlement group as their payment,
        and PaymentMiddlewareASGI calls call_next() -- which runs this
        handler and returns its response -- *before* it calls
        process_settlement(). A seat count read now would be the count from
        before this join lands, stale the moment it is printed. It stays
        where it is a true statement about quote time: the 402's `extra`.

        No `cid`, no `edition`. Neither exists in chain state at
        seat-purchase time -- the CID exists only once the edition is
        released, and `edition` lives in the create transaction's note,
        which would need an indexer query this handler has no other reason
        to make. The route's description already says what a seat funds.

        No store write. The chain is the record of who holds a seat; a pool
        seat has no leg 2, so there is no job row for it to start.

        `agreementId` is INDEX_INPUT_SCHEMA's one optional input, taken from
        the query string or a JSON body (`_index_agreement_id_requested`),
        and it is honoured rather than decorative: a caller that names one
        gets `_pool_changed_underneath`'s 409 rather than a receipt if it
        disagrees with the agreement the verified payment was actually
        matched to -- see that function's docstring for why this is
        reachable and why the check does not lean on the settlement group
        failing atomically instead.

        A buyer who already holds a seat in this pool can still reach a
        receipt here. The contract's `join` asserts one seat per address, but
        that assert runs on chain, after this handler has already returned
        its 200 -- the group then fails atomically and no money moves, but
        the receipt already handed back names a seat that will never exist.
        Nothing here checks for it: the wallet-to-seat mapping lives in the
        roster box, and reading it would be a second chain read this route
        has no other reason to make. The route's description states the rule
        -- boot refuses an INDEX_DESCRIPTION override that drops it -- so a
        buyer agent can avoid the case rather than discover it from a
        receipt that turns out to be worthless.
        """
        requirements = _verified_requirements(request, "POST /index")
        extra = requirements.extra or {}
        funded_agreement_id = extra.get("agreementId")
        if funded_agreement_id is None:
            raise RuntimeError("the verified payment carries no agreementId")
        funded_agreement_id = int(funded_agreement_id)

        requested = await _index_agreement_id_requested(request, agreementId)
        if requested is not None and requested != funded_agreement_id:
            raise HTTPException(
                status_code=409,
                detail=_pool_changed_underneath(requested, funded_agreement_id),
            )

        _require_seat_is_actually_bought(
            request, app_id=settings.app_id, agreement_id=funded_agreement_id
        )

        return {
            "agreementId": funded_agreement_id,
            "seatsTotal": extra.get("seatsTotal"),
            "deadline": extra.get("deadline"),
            "commitSha256": extra.get("commitSha256"),
        }

    return app
