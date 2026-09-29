"""Settlement must never happen for a request the router did not serve.

PaymentMiddlewareASGI settles whenever the wrapped app answers with anything
below 400 ("Don't settle on error responses"). That makes every non-error
response the router can produce *without running the handler* a place where a
buyer can be charged for nothing -- and x402HTTPServerBase._normalize_path
folds "/pin/", "/pin//" and "//pin" onto the paid pattern "POST /pin" (and,
identically, "/index/", "/index//" and "//index" onto "POST /index"), so both
paid routes are reachable under names FastAPI would rather redirect than
serve.

These tests are written against the real middleware over a stubbed
facilitator, so the thing under test is the actual settle decision rather than
a description of it.
"""

import pytest
from x402.http.x402_http_server_base import x402HTTPServerBase

from api.escrow import CONDITION_HASH, STATE_OPEN, Agreement
from tests.conftest import accepts_of, payment_header_for

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64

INDEX_SHARE_PRICE = 5_000_000
INDEX_SEATS = 5
INDEX_AGREEMENT_ID = 7


def _quote(client, sha256: str) -> dict:
    """Earn a real 402 and return the requirements it offered."""
    unpaid = client.post(f"/pin?sha256={sha256}&size=1024")
    assert unpaid.status_code == 402, unpaid.text
    return accepts_of(unpaid)[0]


def _open_pool_agreement() -> Agreement:
    """A minimal open, multi-seat `hash` agreement -- restated rather than
    imported from tests/test_pool.py's private `_agreement`, so this module
    does not reach into another test module's helpers for one record shape."""
    return Agreement(
        condition=CONDITION_HASH,
        state=STATE_OPEN,
        deadline=2_000_000_000,
        share_price=INDEX_SHARE_PRICE,
        min_seats=INDEX_SEATS,
        max_seats=INDEX_SEATS,
        seats=0,
        refund_cursor=0,
        unclaimed_seats=0,
        total_held=0,
        commit_hash=bytes(range(32)),
        beneficiary="B" * 58,
        verifier="V" * 58,
        creator="C" * 58,
    )


def _index_client(deps, client_for):
    """A client whose POST /index has a pinned, open pool to quote against.

    `client_for` is needed rather than the shared `client` fixture: the pool
    route refuses with a 503 until `Settings.index_agreement_id` names an
    open agreement, and that setting has to be baked in before
    `build_routes` closes over it.
    """
    deps.escrow.agreement_records[INDEX_AGREEMENT_ID] = _open_pool_agreement()
    deps.escrow.created = max(deps.escrow.created, INDEX_AGREEMENT_ID)
    return client_for(index_agreement_id=INDEX_AGREEMENT_ID)


def test_the_canonical_path_is_paid_and_settles(client, facilitator, deps):
    """The control for everything below: on the exact path, this all works.

    Without this, a test asserting "no settlement happened" could pass because
    the harness never settles anything at all.
    """
    requirements = _quote(client, SHA_A)
    paid = client.post(
        f"/pin?sha256={SHA_A}&size=1024",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )
    assert paid.status_code == 200, paid.text
    assert paid.json()["jobId"]
    assert len(facilitator.settled) == 1


@pytest.mark.parametrize("path", ["/pin/", "/pin//"])
def test_a_slash_variant_is_matched_for_payment_but_never_settles(
    client, facilitator, deps, path
):
    """The composed defect this file exists for.

    The variant is *matched for payment* -- proven below by the 402 it earns,
    not asserted from the outside -- and must still never reach settlement,
    because the router will not run the handler for it. With FastAPI's default
    redirect_slashes=True, "/pin/" answered 307, which is not >= 400, and the
    payment settled against a redirect: charged buyer, no jobId, and a row
    left QUOTED while the chain says FUNDED.
    """
    assert x402HTTPServerBase._normalize_path(path) == "/pin"

    # Matched for payment: the middleware demands money for this path.
    unpaid = client.post(f"{path}?sha256={SHA_B}&size=1024")
    assert unpaid.status_code == 402, (
        f"{path} is not matched for payment, so this test proves nothing"
    )

    # And the paid retry must be refused without settling. Redirects are not
    # followed here, so what is asserted is the response the *middleware* saw
    # and judged -- following it would report the redirect target's status and
    # hide the 307 that is the whole defect.
    requirements = accepts_of(unpaid)[0]
    paid = client.post(
        f"{path}?sha256={SHA_B}&size=1024",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
        follow_redirects=False,
    )
    assert paid.status_code >= 400, (
        f"{path} answered {paid.status_code}; anything below 400 is settled"
    )
    assert facilitator.settled == [], f"{path} settled a payment it never served"


@pytest.mark.parametrize("path", ["/pin/", "/pin//"])
def test_a_redirect_following_client_is_not_charged_twice(client, facilitator, path):
    """The realistic shape of the defect, and the reason it is Critical.

    An ordinary HTTP client follows redirects, and the payment header rides
    along to the redirect target. Under redirect_slashes=True that means the
    middleware settles once against the 307 and the retry settles *again*
    against the 200 -- measured, not predicted: two settlements for one
    purchase, one of which buys nothing. The buyer pays twice for one pin.
    """
    unpaid = client.post(f"{path}?sha256={SHA_D}&size=1024")
    assert unpaid.status_code == 402, unpaid.text
    requirements = accepts_of(unpaid)[0]

    client.post(
        f"{path}?sha256={SHA_D}&size=1024",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )
    assert len(facilitator.settled) <= 1, (
        f"{path} settled {len(facilitator.settled)} times for one purchase"
    )


@pytest.mark.parametrize("path", ["/index/", "/index//"])
def test_an_index_slash_variant_is_matched_for_payment_but_never_settles(
    deps, facilitator, client_for, path
):
    """POST /index's counterpart of the defect above. The pool route is
    protected by the same global `redirect_slashes=False` /pin's variants
    exercise, but nothing asserted it, so a future change here could silently
    re-open charge-for-nothing on /index while every /pin test above kept
    passing.
    """
    assert x402HTTPServerBase._normalize_path(path) == "/index"
    client = _index_client(deps, client_for)

    # Matched for payment: the middleware demands money for this path.
    unpaid = client.post(path)
    assert unpaid.status_code == 402, (
        f"{path} is not matched for payment, so this test proves nothing"
    )

    requirements = accepts_of(unpaid)[0]
    paid = client.post(
        path,
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
        follow_redirects=False,
    )
    assert paid.status_code >= 400, (
        f"{path} answered {paid.status_code}; anything below 400 is settled"
    )
    assert facilitator.settled == [], f"{path} settled a payment it never served"


@pytest.mark.parametrize("path", ["/index/", "/index//"])
def test_an_index_redirect_following_client_is_not_charged_twice(
    deps, facilitator, client_for, path
):
    client = _index_client(deps, client_for)
    unpaid = client.post(path)
    assert unpaid.status_code == 402, unpaid.text
    requirements = accepts_of(unpaid)[0]

    client.post(
        path,
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )
    assert len(facilitator.settled) <= 1, (
        f"{path} settled {len(facilitator.settled)} times for one purchase"
    )


def test_the_leading_double_slash_folds_onto_the_index_route_too(deps, client_for):
    """The /index counterpart of the same recorded-rather-than-exercised case
    below for /pin: "//index" collapses onto the paid pattern by
    `_normalize_path`, but httpx (and any conforming client) reads a
    leading "//" as protocol-relative and never sends that path, so it is
    not a route a buyer can reach. The router's 404 covers it if some client
    ever does -- asserted here so the collapsing behaviour is not
    rediscovered from scratch for this route either.
    """
    assert x402HTTPServerBase._normalize_path("//index") == "/index"
    client = _index_client(deps, client_for)
    assert client.post("//index").status_code == 404


def test_the_handler_is_what_earns_a_settlement(client, facilitator, deps):
    """Stated positively, so the rule survives a future route being added.

    Settlement is only ever correct for a response the pin handler produced.
    A jobId in the body is the only evidence of that, so it is what the
    settlement count is tied to here.
    """
    requirements = _quote(client, SHA_C)
    paid = client.post(
        f"/pin?sha256={SHA_C}&size=1024",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )
    served_by_handler = paid.status_code == 200 and "jobId" in paid.json()
    assert served_by_handler is (len(facilitator.settled) == 1)


def test_the_leading_double_slash_folds_onto_the_paid_route_too(client):
    """Recorded rather than exercised, because a client cannot express it.

    _normalize_path collapses runs of slashes, so "//pin" also matches the
    paid pattern -- but "//pin" in a URL is protocol-relative, so httpx (and
    any conforming client) reads "pin" as a *host* and never sends that path.
    It is therefore not a route a buyer can reach, and the router's 404 covers
    it if some client ever does. Asserted here so the collapsing behaviour
    itself does not get rediscovered from scratch.
    """
    assert x402HTTPServerBase._normalize_path("//pin") == "/pin"
    assert client.post(f"//pin?sha256={SHA_B}&size=1024").status_code == 404
