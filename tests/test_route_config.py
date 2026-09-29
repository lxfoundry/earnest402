"""Route configuration is asserted offline, because every failure here is
silent in production: verification succeeds, settlement succeeds, funds
arrive, and the catalogue row simply never appears."""

import dataclasses

import pytest
from x402.extensions.bazaar import (
    bazaar_resource_server_extension,
    validate_discovery_extension,
)
from x402.http.types import HTTPRequestContext

from api.config import _sample_price
from api.routes import build_routes


@pytest.fixture
def routes(deps):
    return build_routes(deps)


def test_every_route_declares_a_valid_discovery_extension(routes):
    """Validated *after* the same enrichment a live request goes through, not
    against the bare declaration build_routes() returns.

    bazaar_resource_server_extension.enrich_declaration injects the real HTTP
    method into the declaration and adds "method" to what its schema
    requires -- so a bare, unenriched declaration can look valid (it has no
    "method" key to fail on) while the enriched one a facilitator actually
    receives is self-contradictory. That gap is exactly how the POST /pin
    discovery extension shipped broken once already: declare_discovery_extension
    without body_type builds the *query* shape, whose schema hardcodes
    input.method to ["GET", "HEAD", "DELETE"], and enrichment then injects
    "POST". Validating post-enrichment is what makes this the highest-value
    test in the project, per docs/specs/discovery-and-attribution.md: this
    failure mode is otherwise silent all the way through settlement.
    """
    assert routes, "no routes are declared"
    for pattern, config in routes.items():
        extensions = config.extensions or {}
        assert "bazaar" in extensions, f"{pattern}: no discovery extension declared"
        # Both halves come from the pattern, so the context describes the route
        # being validated rather than the only route that exists today. A
        # hardcoded path would enrich a second route's declaration against
        # /pin's -- which is how this test would keep passing while the
        # declaration a facilitator receives is wrong, the exact failure mode it
        # exists to catch.
        method, path = pattern.split(" ", 1)
        ctx = HTTPRequestContext(
            adapter=None, path=path, method=method, payment_header=None
        )
        enriched = bazaar_resource_server_extension.enrich_declaration(
            extensions["bazaar"], ctx
        )
        result = validate_discovery_extension(enriched)
        assert result.valid, f"{pattern}: {result.errors}"


def test_every_post_route_declares_the_body_shape(routes):
    """A POST route declares `bodyType`/`body`, never `queryParams`.

    Validity is necessary and not sufficient: the crawler applies its own
    rules, not the ones a declaration carries, so a declaration can pass the
    test above and still never be catalogued. That is not hypothetical --
    POST /index settled real USDC on both networks under two earlier
    query-shape declarations, first with an empty example (leaving only
    `type` and `method`) and then with an `agreementId` example
    (`queryParams`), and appeared in the directory under neither, with
    nothing reported anywhere in the payment path.

    Measured against the live directory on 2026-09-29: all 1,001 catalogued
    POST resources are body-shaped, and none is query-shaped or declares an
    input of only `type` and `method`. The route's first settlement under
    the body shape was catalogued within seconds.

    The shape is metadata -- `get_body()` is never called anywhere in the
    library -- so nothing enforces it against the request. That POST /index
    really reads the body it declares is asserted in tests/test_pool.py.
    """
    for pattern, config in routes.items():
        method, path = pattern.split(" ", 1)
        ctx = HTTPRequestContext(
            adapter=None, path=path, method=method, payment_header=None
        )
        enriched = bazaar_resource_server_extension.enrich_declaration(
            (config.extensions or {})["bazaar"], ctx
        )
        declared = enriched["info"]["input"]
        assert "queryParams" not in declared, (
            f"{pattern}: declares the query shape; no catalogued POST resource does"
        )
        assert declared.get("bodyType") == "json" and "body" in declared, (
            f"{pattern}: info.input declares {sorted(declared)}; every catalogued "
            "POST resource declares bodyType (1,001 of 1,001 on 2026-09-29), and "
            "the handler reads only a JSON body"
        )


def test_every_accepts_entry_carries_the_challenge_tag_and_fee_payer(routes):
    for pattern, config in routes.items():
        accepts = config.accepts
        options = accepts if isinstance(accepts, list) else [accepts]
        for option in options:
            assert option.pay_to, f"{pattern}: payTo is empty"
            price = option.price
            if callable(price):
                # Dynamic, so the tag and the fee payer are only observable
                # by running it -- the same sampling verify_startup does.
                price = _sample_price(price)
            assert not isinstance(price, str), (
                f"{pattern}: a string price discards extra, and with it feePayer "
                "and the challenge tag"
            )
            extra = getattr(price, "extra", None) or {}
            assert extra.get("tag") == "x402-global-challenge", (
                f"{pattern}: tag missing from price.extra"
            )
            assert extra.get("feePayer"), (
                f"{pattern}: feePayer missing from price.extra"
            )


def test_both_routes_are_built_and_pinned_to_their_own_resources(deps):
    routes = build_routes(deps)

    assert set(routes) == {"POST /pin", "POST /index"}
    assert routes["POST /pin"].resource.endswith("/pin")
    assert routes["POST /index"].resource.endswith("/index")


def test_every_route_pins_a_query_free_https_resource(deps, routes):
    """The catalogue identity is built from `resource or adapter.get_url()`, and
    get_url() includes the query string. Unpinned, a route taking its input in
    the query registers one near-duplicate directory entry per distinct query
    instead of one entry whose settlement count rises.

    Asserted offline because the failure is invisible until it is expensive: the
    route still answers 402 and still settles, and only the shape of the
    catalogue is wrong -- by which point the rows exist.
    """
    for pattern, config in routes.items():
        assert config.resource, (
            f"{pattern}: resource is not pinned, so the catalogue identity would "
            "be whatever URL the request happened to carry"
        )
        assert config.resource.startswith("https://"), (
            f"{pattern}: resource is {config.resource!r}; a resource that is not "
            "https is not catalogued at all"
        )
        assert "?" not in config.resource, (
            f"{pattern}: pinned resource {config.resource!r} carries a query "
            "string, which defeats the pinning"
        )
        assert config.resource in deps.settings.resource_urls, (
            f"{pattern}: resource {config.resource!r} is hardcoded rather than "
            "taken from settings, so it cannot follow the deployed host"
        )


def test_the_listing_copy_says_what_is_checked_and_that_a_machine_checks_it(routes):
    """`description` is the Bazaar listing for every route, not just /pin, and
    every route is subject to the same copy rules -- so this loops rather
    than naming one route.

    `"sha256"` is a stand-in for "the release condition is stated", not a
    literal requirement of the copy rule itself: if a future route's
    description states its condition without that word, this assertion
    should change to test the rule (the condition is named) rather than keep
    insisting on a word no description need contain.
    """
    for pattern, config in routes.items():
        description = config.description
        assert description, f"{pattern}: no description"
        assert "sha256" in description, f"{pattern}: does not name sha256"
        assert "Machine-verified" in description, (
            f"{pattern}: not marked machine-verified"
        )
        assert "refunded" in description, f"{pattern}: says nothing about refunds"


def test_the_advertised_window_equals_the_funding_window(deps, routes):
    for pattern, config in routes.items():
        accepts = config.accepts
        options = accepts if isinstance(accepts, list) else [accepts]
        for option in options:
            assert option.max_timeout_seconds == deps.settings.funding_window_seconds, (
                f"{pattern}: advertises a window it does not honour"
            )


def _with_settings(deps, **overrides):
    return dataclasses.replace(
        deps, settings=dataclasses.replace(deps.settings, **overrides)
    )


def test_the_index_route_lists_the_configured_description(deps):
    """The listing is a setting, not a constant: an override reaches the
    RouteConfig the catalogue reads. Whether the override is acceptable is
    the boot guard's question, so any distinct string will do here."""
    description = "An overriding listing for the pool route."
    routes = build_routes(_with_settings(deps, index_description=description))

    assert routes["POST /index"].description == description


def test_the_index_discovery_example_carries_the_configured_seat_count(deps):
    """A literal seat count in the example would advertise a pool size the
    deployment does not sell once INDEX_SEATS moves off the default. Checked
    post-enrichment as well, so the parameterised declaration is still one a
    facilitator accepts."""
    routes = build_routes(_with_settings(deps, index_seats=3))
    declaration = routes["POST /index"].extensions["bazaar"]

    assert declaration["info"]["output"]["example"]["seatsTotal"] == 3

    ctx = HTTPRequestContext(
        adapter=None, path="/index", method="POST", payment_header=None
    )
    enriched = bazaar_resource_server_extension.enrich_declaration(declaration, ctx)
    result = validate_discovery_extension(enriched)
    assert result.valid, result.errors
