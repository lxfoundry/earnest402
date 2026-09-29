"""What a client is actually told when a quote is refused.

One of the three refusals now reads correctly and two do not, and the split is
structural rather than incidental:

* **A malformed query is a real 400**, produced by the query gate in
  api/app.py, which is mounted outside the payment middleware and so is still
  allowed to answer for itself.
* **The ceiling and the rate limiter are still a generic 500.** Both are
  stateful, so they cannot be hoisted out of the price callable without
  double-counting (a limiter that counts the gate's pass and the callable's
  pass charges one caller twice). Inside the callable, x402-avm 2.0.2 catches
  every exception in its resolve_options phase, converts it to a hardcoded
  500 {"error": "Failed to process request"}, and returns it as a plain
  JSONResponse that is never re-raised -- so ExceptionMiddleware, where
  @app.exception_handler lives, is never reached. The handlers registered in
  api/app.py are correct and currently inert.

Every test here runs against the real middleware over a stubbed facilitator,
so a refusal is distinguishable from a harness that never worked. That matters
more than it sounds: stubbing `initialize` instead leaves the resource server
uninitialised, and then a *successful* quote returns the same generic 500 as a
refused one -- making any assertion about the 500 vacuous.
"""

import pytest

from tests.conftest import accepts_of

GOOD_SHA = "a" * 64
_SEED_BASE = 1000


def test_a_control_quote_earns_a_real_402(client, deps):
    """The control the rest of this module depends on.

    If this fails, every "refused with 500" assertion below is meaningless,
    because it would be indistinguishable from the harness not working.
    """
    response = client.post(f"/pin?sha256={GOOD_SHA}&size=1024")
    assert response.status_code == 402, response.text
    assert accepts_of(response)[0]["extra"]["agreementId"] == 1
    assert deps.escrow.created == 1


def test_a_malformed_query_is_a_real_400(client, deps):
    """BadQuoteRequest means 400, and says which rule was broken."""
    response = client.post("/pin?size=1024")

    assert response.status_code == 400, response.text
    assert "sha256" in response.json()["detail"]
    # The gate is before the payment middleware, so nothing was created and
    # no quote was parked for a request that could never be honoured.
    assert deps.escrow.created == 0
    assert deps.jobs.count_unfunded() == 0


@pytest.mark.parametrize("path", ["/pin/", "/pin//"])
def test_a_malformed_query_is_a_real_400_under_every_slash_variant(client, deps, path):
    """The gate matches the path the way the payment middleware does.

    `_normalize_path` folds these onto the paid pattern, so they demand money
    -- which means a malformed query under one of them is a request the gate
    is responsible for. Matched by bare equality it would slip past, reach the
    price callable inside the payment middleware, and come back as the generic
    500 this gate exists to avoid.
    """
    response = client.post(f"{path}?size=1024")

    assert response.status_code == 400, response.text
    assert "sha256" in response.json()["detail"]
    assert deps.escrow.created == 0


def test_healthz_reports_which_pool_the_deployment_is_configured_for(client_for):
    """The one configured value nothing else can catch being wrong.

    The boot guard samples the pool price callable without touching the chain,
    on purpose -- a cold start with algod unreachable still has to boot -- so a
    mistyped INDEX_AGREEMENT_ID boots green and then answers 503 forever with
    nothing wrong on chain. Reporting it here is what makes that visible, and
    it is reported rather than checked: no chain read happens on a liveness
    probe.
    """
    assert client_for(index_agreement_id=0).get("/healthz").json() == {
        "status": "ok",
        "indexAgreementId": 0,
    }
    assert (
        client_for(index_agreement_id=41).get("/healthz").json()["indexAgreementId"]
        == 41
    )


def test_every_malformed_shape_is_refused_before_anything_is_created(client, deps):
    """The gate's rules are the price callable's rules, over the wire."""
    for query in (
        "size=1024",  # no sha256
        f"sha256={'A' * 64}&size=1024",  # uppercase hex
        f"sha256={'a' * 63}&size=1024",  # too short
        f"sha256={'a' * 65}&size=1024",  # too long
        f"sha256={'z' * 64}&size=1024",  # not hex
        f"sha256={GOOD_SHA}",  # no size
        f"sha256={GOOD_SHA}&size=0",  # not positive
        f"sha256={GOOD_SHA}&size=-1",
        f"sha256={GOOD_SHA}&size=notanumber",
        f"sha256={GOOD_SHA}&size=99999999999999",  # over max_file_bytes
    ):
        response = client.post(f"/pin?{query}")
        assert response.status_code == 400, f"{query} -> {response.status_code}"
    assert deps.escrow.created == 0
    assert deps.jobs.count_unfunded() == 0


def test_the_unfunded_ceiling_is_still_a_generic_500(client, deps):
    """Pins the limitation, and proves the ceiling is what produced it.

    The two assertions that matter are the last two: without them this test
    would pass with the ceiling check deleted, since a *successful* quote also
    reaches a 500 when the harness is stubbed at the wrong seam.
    """
    for n in range(deps.settings.max_unfunded_agreements):
        deps.jobs.create_quote(
            # Out of FakeEscrow's own id range, which counts up from 1:
            # a seeded row at id 1 would collide with the next real create
            # and surface as an opaque 500, masking what is under test.
            agreement_id=_SEED_BASE + n,
            sha256=f"{n:064x}",
            size=1024,
            deadline=deps.now() + deps.settings.delivery_deadline_seconds,
            quoted_at=deps.now(),
        )
    assert deps.jobs.count_unfunded() == deps.settings.max_unfunded_agreements

    response = client.post(f"/pin?sha256={'f' * 64}&size=1024")

    assert response.status_code == 500
    assert response.json() == {"error": "Failed to process request"}
    # The refusal came from the ceiling: nothing new was created, and the row
    # count is untouched. A ceiling check that had silently stopped working
    # would create agreement 51 here and still return some 500.
    assert deps.escrow.created == 0
    assert deps.jobs.count_unfunded() == deps.settings.max_unfunded_agreements


def test_one_below_the_ceiling_the_same_request_earns_a_402(client, deps):
    """The discriminating other half: the ceiling, not the request, refused it."""
    for n in range(deps.settings.max_unfunded_agreements - 1):
        deps.jobs.create_quote(
            # Out of FakeEscrow's own id range, which counts up from 1:
            # a seeded row at id 1 would collide with the next real create
            # and surface as an opaque 500, masking what is under test.
            agreement_id=_SEED_BASE + n,
            sha256=f"{n:064x}",
            size=1024,
            deadline=deps.now() + deps.settings.delivery_deadline_seconds,
            quoted_at=deps.now(),
        )

    response = client.post(f"/pin?sha256={'f' * 64}&size=1024")

    assert response.status_code == 402, response.text
    assert deps.escrow.created == 1


def test_no_open_pool_is_a_real_503_not_a_generic_500(client):
    """POST /index is gated the same way /pin's malformed query is above:
    a middleware mounted outside PaymentMiddlewareASGI, still able to answer
    for itself, rather than PoolClosed falling into the price callable's
    generic-500 path the ceiling and the rate limiter are stuck with. The
    `deps` fixture seeds no agreements at all, so no pool is ever open here.
    """
    response = client.post("/index")

    assert response.status_code == 503, response.text
    assert response.json() == {
        "error": "no_pool_open",
        "detail": (
            "No pool is open. Pools open one at a time, and each funds its "
            "own committed file; try again later."
        ),
    }
    # The property this route exists to guarantee: a 503 must never carry the
    # header that tells a buyer's client a purchase is on offer. A leaked
    # header here would have x402-aware clients quoting a route that can
    # never be paid.
    assert "payment-required" not in response.headers


@pytest.mark.parametrize("path", ["/index", "/index/", "/index//"])
def test_no_open_pool_is_a_real_503_for_every_slash_variant(client, path):
    """The gate used to match `request.url.path == "/index"` exactly, so
    "/index/" and "/index//" bypassed it entirely and reached the price
    callable, which turns `PoolClosed` into the generic 500 this gate exists
    to avoid. Matching the path the way PaymentMiddlewareASGI itself
    normalises it (`x402HTTPServerBase._normalize_path`) closes that gap.
    """
    response = client.post(path)

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "no_pool_open"


def test_a_chain_that_did_not_answer_is_a_different_503(deps, client_for):
    """Both refusals are 503, and they must not carry the same code.

    `no_pool_open` tells a buyer this edition is closed. During a funding
    window that is the one wrong answer that costs a seat, and a shared
    algod returning 429 for a moment is not grounds for telling it. The
    503 stands -- a seat that cannot be confirmed is never quoted -- but it
    says only that the status could not be read.
    """
    from algosdk.error import AlgodHTTPError

    def refuse(agreement_id):
        raise AlgodHTTPError("rate limited", 429)

    deps.escrow.read_agreement = refuse
    client = client_for(index_agreement_id=7)

    response = client.post("/index")

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "pool_status_unavailable"
    # The same guarantee the closed-pool 503 carries: never a header that
    # tells an x402 client a purchase is on offer.
    assert "payment-required" not in response.headers
    # Unlike a closed pool, this resolves on its own, so say so.
    assert response.headers.get("Retry-After") == "5"


def test_neither_refusal_names_what_the_pool_funds(deps, client_for):
    """One server answers for every deployment, and each deployment's pools
    fund a different kind of file -- its listing says which. A refusal's
    detail is read on all of them, by a buyer's agent as much as by a person,
    so it speaks of the pool and never of the product behind it."""
    from algosdk.error import AlgodHTTPError

    closed = client_for().post("/index")

    def refuse(agreement_id):
        raise AlgodHTTPError("rate limited", 429)

    deps.escrow.read_agreement = refuse
    unreadable = client_for(index_agreement_id=7).post("/index")

    assert closed.json()["error"] == "no_pool_open"
    assert unreadable.json()["error"] == "pool_status_unavailable"
    for response in (closed, unreadable):
        detail = response.json()["detail"].lower()
        for kind_word in ("edition", "trip", "route index"):
            assert kind_word not in detail, (response.json()["error"], kind_word)


def test_the_rate_limiter_is_still_a_generic_500(client, deps):
    """Same limitation, reached by the limiter rather than the ceiling."""
    allowed = deps.settings.quotes_per_source_per_minute
    for n in range(allowed):
        response = client.post(f"/pin?sha256={n:064x}&size=1024")
        assert response.status_code == 402, f"quote {n} -> {response.status_code}"

    refused = client.post(f"/pin?sha256={'e' * 64}&size=1024")

    assert refused.status_code == 500
    assert refused.json() == {"error": "Failed to process request"}
    # The limiter refused it, so the (allowed + 1)th agreement was never made.
    assert deps.escrow.created == allowed
