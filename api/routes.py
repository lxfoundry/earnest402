"""The route table and the Bazaar listing.

The description is product copy that happens to live in a configuration file:
it is what a discovering agent reads. It leads with the condition rather than
the mechanism, says what "as promised" means in machine terms, and never omits
that the check is machine-made.
"""

from __future__ import annotations

from typing import Any

from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.avm import SCHEME_EXACT

from api.config import SAMPLE_SHA256
from api.deps import Deps
from api.pool import make_pool_price_callable
from api.pricing import SHA256_HEX, make_price_callable

PIN_DESCRIPTION = (
    "Pin a file to IPFS and pay only if it matches. Your USDC is held by an "
    "Algorand escrow app until the pinned bytes hash to the sha256 you committed "
    "in advance — released when they match, refunded on-chain when they don't. "
    "Machine-verified only: no arbitration, no human in the loop."
)

PIN_INPUT_SCHEMA = {
    "properties": {
        "sha256": {"type": "string", "pattern": SHA256_HEX.pattern},
        "size": {"type": "integer", "minimum": 1},
    },
    "required": ["sha256", "size"],
}

# secrets.token_hex(16) -- what api/app.py actually mints -- is 16 bytes,
# i.e. 32 hex characters. The catalogue example has to match that length: a
# buyer agent sizing a field from it gets the wrong answer otherwise.
_EXAMPLE_JOB_ID = "9f2b7c1e5a8d4f609f2b7c1e5a8d4f60"


def _declare_post_discovery(
    *, input: dict[str, Any], input_schema: dict[str, Any], output: OutputConfig
) -> dict[str, Any]:
    """Bazaar discovery for a POST route, declared through the body shape.

    `body_type="json"` picks the body shape, whose method enum is natively
    ["POST", "PUT", "PATCH"]. The query shape's is ["GET", "HEAD", "DELETE"],
    the verbs x402-avm groups as `QueryParamMethods`.

    The catalogue lists POST routes in this shape and no other. Measured
    against the live directory on 2026-09-29: all 1,001 catalogued POST
    resources are body-shaped; none declares `queryParams`, and none
    declares an input of only `type` and `method`. POST /index was declared
    twice before this, both times in the query shape with its enum widened
    to admit POST, and was catalogued under neither. With an empty example,
    which leaves only `type` and `method`, it settled on both networks
    through public hosts. With an `agreementId` example, which adds
    `queryParams`, it settled on two deployments, once on one and three
    times from three payers on the other. Its first settlement under the
    body shape was catalogued within seconds. The facilitator reports
    nothing when it declines a declaration, so this is an observed rule, not
    a documented one: the crawler applies its own requirements, not the ones
    a declaration's own schema carries.

    A declaration should describe the request the handler reads, because a
    buyer agent builds its request from it, and nothing in the library
    checks the two against each other -- `get_body()` is defined on the
    adapters and never called. POST /index reads `agreementId` from a JSON
    body as well as from the query string (`api/app.py`). POST /pin does
    not yet: its input is read by the price callable, which gets no body
    through the adapter's interface (`get_body()` returns None), so its
    declared body describes a request /pin would not understand. /pin is
    off in every Fly config here, and that has to be resolved before it is
    turned on for a deployment that serves this listing.

    `tests/test_route_config.py` asserts the enriched form validates and
    that it is body-shaped, because this failure is silent end to end:
    verification succeeds, settlement succeeds, funds arrive, and only the
    catalogue row never appears.
    """
    return declare_discovery_extension(
        input=input, input_schema=input_schema, body_type="json", output=output
    )


def _pin_discovery_extension() -> dict[str, Any]:
    """Bazaar discovery for POST /pin. See `_declare_post_discovery` for why
    this is declared through the body shape, and why /pin does not yet read
    the body it declares."""
    return _declare_post_discovery(
        input={
            "sha256": SAMPLE_SHA256,
            "size": 1048576,
        },
        input_schema=PIN_INPUT_SCHEMA,
        output=OutputConfig(
            example={
                "jobId": _EXAMPLE_JOB_ID,
                "agreementId": 41,
                "deadline": 1756000000,
            }
        ),
    )


# POST /index's description is a setting, `Settings.index_description`: its
# default text and the clauses every override must keep live in api/config.py.

INDEX_INPUT_SCHEMA = {
    "properties": {
        "agreementId": {"type": "integer", "minimum": 1},
    },
    "required": [],
}


def _index_discovery_extension(seats: int) -> dict[str, Any]:
    """Bazaar discovery for POST /index. See `_declare_post_discovery` for
    why a POST route is declared through the body shape.

    Carries no *required* input -- the seat it quotes is whichever pool is
    currently open, not something the caller names. `agreementId` is
    optional, documented in INDEX_INPUT_SCHEMA's `properties` with
    `required` left empty.

    The example body is `{}`, on purpose. A buyer agent builds its request
    from the listing, and the handler honours an `agreementId` it names with
    a 409 when it is not the open pool -- after the buyer has signed. Any
    fixed id in the example would name a pool other than the open one on
    almost every deployment, so an agent copying the listing could never
    buy. `tests/test_pool.py` sends the example verbatim and expects a seat.

    An empty example still declares `body: {}` in the body shape, a form the
    catalogue lists POST resources under, this project's earlier spike
    routes among them. In the query shape the same empty example dropped the
    input key entirely, leaving only `type` and the injected method: a form
    no catalogued POST resource carries, and one this route was declared in,
    uncatalogued, before.

    The output example's `seatsTotal` is this deployment's seat count, so the
    catalogue shows the size of pool a buyer would actually be joining.
    """
    return _declare_post_discovery(
        input={},
        input_schema=INDEX_INPUT_SCHEMA,
        output=OutputConfig(
            example={
                "agreementId": 42,
                "seatsTotal": seats,
                "deadline": 1758000000,
                "commitSha256": SAMPLE_SHA256,
            }
        ),
    )


def build_routes(deps: Deps) -> dict[str, RouteConfig]:
    """The paid routes this deployment serves.

    `POST /index` always; `POST /pin` only while `PIN_ROUTE_ENABLED` is on.
    `api/app.py` mounts the `/pin` handler from this table rather than from
    the setting, so a handler and its payment requirement cannot disagree.
    """
    routes = {}
    if deps.settings.pin_route_enabled:
        routes["POST /pin"] = _pin_route(deps)
    routes["POST /index"] = _index_route(deps)
    return routes


def _pin_route(deps: Deps) -> RouteConfig:
    settings = deps.settings
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme=SCHEME_EXACT,
                pay_to=settings.app_account,
                price=make_price_callable(deps),
                network=settings.network,
                # Equal to the funding window by construction. Advertising
                # 300 seconds to settle while sweeping at 120 promises a
                # window we do not honour.
                max_timeout_seconds=settings.funding_window_seconds,
            ),
        ],
        # Without this x402-avm awaits the price callable with
        # asyncio.wait_for(timeout=None) -- no bound at all. The callable
        # creates an agreement, so its worst case is however long the chain
        # takes to answer, and the buyer holds an open request for all of
        # it. Bounded well inside the funding window, and guarded in
        # api/config.py so the two cannot drift apart.
        hook_timeout_seconds=settings.price_hook_timeout_seconds,
        # Pinned, because the catalogue identity is
        # `resource or adapter.get_url()` and get_url() includes the query
        # string: unpinned, every distinct sha256 would register its own
        # near-duplicate directory entry.
        resource=settings.resource_url,
        description=PIN_DESCRIPTION,
        mime_type="application/json",
        extensions=_pin_discovery_extension(),
    )


def _index_route(deps: Deps) -> RouteConfig:
    settings = deps.settings
    return RouteConfig(
        accepts=[
            PaymentOption(
                scheme=SCHEME_EXACT,
                pay_to=settings.app_account,
                price=make_pool_price_callable(deps),
                network=settings.network,
                # Same reasoning as /pin: equal to the funding window by
                # construction, so the advertised window is one this
                # deployment actually honours.
                max_timeout_seconds=settings.funding_window_seconds,
            ),
        ],
        hook_timeout_seconds=settings.price_hook_timeout_seconds,
        # Pinned for the same reason as /pin's: the catalogue identity is
        # `resource or adapter.get_url()`, and both products share one
        # payTo -- an unpinned resource here would collide with /pin's
        # directory entry as easily as with its own query string.
        resource=settings.index_resource_url,
        description=settings.index_description,
        mime_type="application/json",
        extensions=_index_discovery_extension(settings.index_seats),
    )
