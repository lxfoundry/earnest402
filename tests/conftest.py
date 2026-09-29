"""Shared test configuration for the repository-root surface.

Nothing here reads the process environment for its own configuration.
`api/config.py` takes its environment as an argument (`load_settings(env)`)
and no test imports `api/server.py`, so the suite asserts the same thing on
every machine without priming anything first: tests build a `Settings` from
`settings_factory` instead.

That is narrower than "no test loads a .env". `agents/common.py` calls
`load_dotenv()` at import time and several test modules import it, so a `.env`
on the machine running the suite *is* read into the process environment.
Nothing offline consults it -- every setting arrives explicitly -- but a
test that started relying on an environment variable would pass here and
fail on a machine without that file.

The value below is public: an application address, nothing secret. Nothing
in this file should ever be read by code outside a test.
"""

import asyncio
import base64
import json
import pathlib

import pytest
from algosdk import abi, encoding, transaction
from fastapi.testclient import TestClient
from x402.http.facilitator_client import HTTPFacilitatorClient
from x402.http.types import HTTPRequestContext, PaymentOption, RouteConfig
from x402.http.utils import encode_payment_signature_header
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2, SCHEME_EXACT
from x402.schemas import (
    X402_VERSION,
    AssetAmount,
    PaymentPayload,
    PaymentRequirements,
)
from x402.schemas.responses import (
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
)

from api.app import create_app
from api.config import Settings
from api.deps import Deps
from api.escrow import JOIN, required_deposit
from api.jobs import JobStore
from api.routes import build_routes

# The real-USDC TestNet application account the guards compare against.
# The application the offline suite pretends to be talking to. Shared by
# settings_factory and by the payment groups tests build, because a join
# call naming a different application than the one the handler checks
# against would be refused for a reason no test meant to exercise.
TEST_APP_ID = 769608941

# Two real, checksum-valid addresses for the buyer side of a built payment
# group. `PAYER` further down is a rate-limiter key, not an address, and
# does not survive `decode_address`. Derived once from fixed bytes rather
# than generated per run, so a failure names the same account twice.
BUYER = "AAAQEAYEAUDAOCAJBIFQYDIOB4IBCEQTCQKRMFYYDENBWHA5DYP7MUPJQE"
OTHER_BUYER = "EAQSEIZEEUTCOKBJFIVSYLJOF4YDCMRTGQ2TMNZYHE5DWPB5HY7WBK462E"

APP_ACCOUNT = "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY"
FEE_PAYER = "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M"


def await_price(price, ctx):
    """The price callable is async because creating an agreement is a network
    call; the library awaits it. Tests drive it synchronously."""
    return asyncio.run(price(ctx))


class FakeClock:
    """A clock the test drives, in whole seconds.

    Every deadline, funding window and retention window in this system is a
    comparison against "now", and a test that waited on the wall clock would
    either be slow or be flaky. Callable so it can stand in for `time.time`
    wherever one is injected.
    """

    def __init__(self, now: int = 1_000_000) -> None:
        self._now = now

    def __call__(self) -> int:
        return self._now

    def advance(self, seconds: int) -> int:
        self._now += seconds
        return self._now


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def settings_factory():
    """A configuration that passes every guard, with one property spoiled on
    request. Built positively rather than as a pile of overrides, so a test
    that spoils one thing is visibly testing that one thing."""

    def make(**overrides):
        defaults = {
            "network": ALGORAND_TESTNET_CAIP2,
            "algod_url": "http://localhost:4001",
            "algod_token": "a" * 64,
            "facilitator_url": "https://facilitator.goplausible.xyz",
            "usdc_asa_id": 10458941,
            "app_id": TEST_APP_ID,
            "app_account": APP_ACCOUNT,
            "fee_payer": FEE_PAYER,
            "treasury": FEE_PAYER,
            "verifier": FEE_PAYER,
            "admin_mnemonic": "",
            "public_host": "https://earnest.example",
            "pin_resource_url": "https://earnest.example/pin",
            "index_resource_url": "https://earnest.example/index",
            "index_share_price_micro": 5_000_000,
            "index_seats": 5,
            # 0 means unconfigured, matching the shipped default: most tests
            # never touch the pool route, and the ones that do set this
            # explicitly to the agreement id they seed.
            "index_agreement_id": 0,
            "price_micro_usdc": 100_000,
            "funding_window_seconds": 120,
            "delivery_deadline_seconds": 900,
            "max_file_bytes": 16 * 1024 * 1024,
            "max_leg2_micro_usdc": 500_000,
            "max_unfunded_agreements": 50,
            "quotes_per_source_per_minute": 20,
            "price_hook_timeout_seconds": 60,
            "worker_url": "https://api.algofile.io/api/algofile/upload",
            "gateway_url": "https://ipfs.io/ipfs",
            "job_db_path": ":memory:",
            "production_app_id": 0,
            # On, unlike the shipped default: most of this suite exercises
            # `/pin`. tests/test_pin_route_switch.py covers the route off.
            "pin_route_enabled": True,
            # A directory that never exists, so no test serves the client
            # unless it builds a bundle of its own -- the checkout's own
            # web/dist may or may not be there.
            "web_dist_dir": str(pathlib.Path(__file__).parent / "no-client-bundle"),
        }
        defaults.update(overrides)
        return Settings(**defaults)

    return make


@pytest.fixture
def route_factory():
    """A minimal correct paid route, with one property spoiled on request.

    `pay_to` defaults to the same application account `settings_factory` uses,
    because the guards compare the two. `pattern` defaults to the single route
    this backend used to serve; a test that needs two routes in one guard run
    calls this twice with different patterns and merges the two one-entry
    dicts, rather than the fixture growing a second, parallel shape.
    """

    def make(
        *,
        pattern="POST /pin",
        tag="x402-global-challenge",
        fee_payer=FEE_PAYER,
        pay_to=APP_ACCOUNT,
        max_timeout_seconds=120,
        resource="https://earnest.example/pin",
        price=None,
        hook_timeout_seconds=60,
    ):
        if price is None:
            extra = {"decimals": 6}
            if tag is not None:
                extra["tag"] = tag
            if fee_payer is not None:
                extra["feePayer"] = fee_payer
            price = AssetAmount(amount="100000", asset="10458941", extra=extra)
        return {
            pattern: RouteConfig(
                accepts=[
                    PaymentOption(
                        scheme="exact",
                        pay_to=pay_to,
                        price=price,
                        network=ALGORAND_TESTNET_CAIP2,
                        max_timeout_seconds=max_timeout_seconds,
                    )
                ],
                resource=resource,
                hook_timeout_seconds=hook_timeout_seconds,
            )
        }

    return make


class FakeAlgodForPricing:
    def __init__(self):
        self.min_fee = 1_000

    def suggested_params(self):
        class SP:
            pass

        sp = SP()
        sp.min_fee = self.min_fee
        return sp


class FakeEscrow:
    """Records what would have gone on chain. The real calls are exercised on
    LocalNet in Task 7; here the subject is the callable's decisions.

    Every method here is signature-pinned against EscrowClient by
    tests/test_escrow_client.py, because a fake that has drifted from the
    client it stands in for makes the whole offline suite agree with itself
    and with nothing else.
    """

    def __init__(self, algod):
        self._algod = algod
        self.created = 0
        self.last_deposit = None
        self.agreements = {}
        # A separate store from `agreements` above, which only ever holds
        # id -> commit_hash for the create-path tests. `read_agreement`
        # serves whatever a test seeds here directly, keyed by id.
        self.agreement_records = {}
        # Bumped by a test that needs the predicted id to be wrong, which is
        # what a second process creating concurrently does.
        self.id_skew = 0

    def min_fee(self):
        # Through suggested_params(), which is the call EscrowClient.min_fee
        # actually makes. Reading self._algod.min_fee directly would pass the
        # same assertions while bypassing the interface under test.
        return self._algod.suggested_params().min_fee

    def next_agreement_id(self):
        return self.created + 1 + self.id_skew

    def read_agreement(self, agreement_id):
        return self.agreement_records.get(agreement_id)

    def create_hash_agreement(
        self,
        *,
        commit_hash,
        share_price,
        deadline,
        beneficiary,
        verifier,
        seats=1,
        note=None,
    ):
        self.created += 1
        self.last_deposit = required_deposit(seats, self.min_fee())
        agreement_id = self.created
        self.agreements[agreement_id] = commit_hash
        return agreement_id


@pytest.fixture
def deps(settings_factory, clock):
    """One assembled Deps for the pricing layer, sharing the clock fixture so
    the store's own timestamps and deps.now() never disagree."""
    algod = FakeAlgodForPricing()
    return Deps(
        settings=settings_factory(),
        escrow=FakeEscrow(algod),
        jobs=JobStore(":memory:", clock=clock),
        algod=algod,
        http=None,
        clock=clock,
    )


@pytest.fixture
def ctx_factory():
    class Adapter:
        def __init__(self, params, headers):
            self._params = params
            self._headers = {name.lower(): value for name, value in headers.items()}

        def get_query_param(self, name):
            return self._params.get(name)

        def get_query_params(self):
            return dict(self._params)

        def get_header(self, name):
            return self._headers.get(name.lower())

        def get_method(self):
            return "POST"

        def get_path(self):
            return "/pin"

        def get_url(self):
            return "https://earnest.example/pin"

        def get_body(self):
            return None

    def make(
        *,
        sha256=None,
        size=None,
        payment_header=None,
        source="10.0.0.9",
        headers=None,
    ):
        """`source` is sugar for a single-hop X-Forwarded-For.

        `headers` overrides it outright, for a test that needs to say what a
        proxy set versus what the caller claimed -- the distinction the rate
        limiter's source key turns on.
        """
        params = {}
        if sha256 is not None:
            params["sha256"] = sha256
        if size is not None:
            params["size"] = str(size)
        if headers is None:
            headers = {"x-forwarded-for": source} if source else {}
        return HTTPRequestContext(
            adapter=Adapter(params, headers),
            path="/pin",
            method="POST",
            payment_header=payment_header,
        )

    return make


# --------------------------------------------------------------------------
# The offline leg-1 harness.
#
# Everything above fakes the *chain*. This fakes only the facilitator's three
# network calls -- get_supported, verify, settle -- and leaves every piece of
# x402-avm's own machinery running for real: route matching, the price
# callable, requirement building, the Bazaar enrichment, find_matching_
# requirements, and the middleware's decision about when to settle.
#
# Stubbing `x402HTTPServerBase.initialize` instead, which is the cheaper
# shortcut, does not work and is actively misleading: it leaves the resource
# server uninitialised, so *every* request -- including one that should have
# earned a clean 402 -- dies in build_payment_requirements with
# RuntimeError("Server not initialized") and is swallowed into the same
# generic 500 the price-callable failures produce. A test written on that
# stub cannot tell "the ceiling refused this quote" from "the harness is not
# wired up", which is the difference between a test and a tautology.
# --------------------------------------------------------------------------


class FacilitatorStub:
    """Records what the facilitator was asked to do, and answers offline."""

    def __init__(self, network: str) -> None:
        self._network = network
        self.verified: list[tuple] = []
        self.settled: list[tuple] = []
        # Flipped by a test that needs settlement to fail after the handler
        # has already returned 200 -- the mint-before-settlement window.
        self.settle_succeeds = True

    def get_supported(self, _self=None):
        return SupportedResponse(
            kinds=[
                SupportedKind(
                    x402Version=X402_VERSION,
                    scheme=SCHEME_EXACT,
                    network=self._network,
                )
            ],
            extensions=["bazaar"],
        )

    async def verify(self, payload, requirements):
        self.verified.append((payload, requirements))
        return VerifyResponse(isValid=True, payer=PAYER)

    async def settle(self, payload, requirements):
        self.settled.append((payload, requirements))
        if not self.settle_succeeds:
            return SettleResponse(
                success=False,
                errorReason="stubbed_failure",
                transaction="",
                network=self._network,
            )
        return SettleResponse(
            success=True,
            transaction="STUBTXID",
            network=self._network,
            payer=PAYER,
        )


PAYER = "3NKRPUYEGZ6QMRT4XFPFRPHVXA6WEMD6NHOKZVJPDLRDXNU2ARHDBHKJPU"


@pytest.fixture
def facilitator(deps, monkeypatch):
    """Patch the facilitator's network boundary and nothing else."""
    stub = FacilitatorStub(deps.settings.network)
    monkeypatch.setattr(
        HTTPFacilitatorClient, "get_supported", lambda _self: stub.get_supported()
    )
    monkeypatch.setattr(
        HTTPFacilitatorClient,
        "verify",
        lambda _self, payload, requirements: stub.verify(payload, requirements),
    )
    monkeypatch.setattr(
        HTTPFacilitatorClient,
        "settle",
        lambda _self, payload, requirements: stub.settle(payload, requirements),
    )
    return stub


@pytest.fixture
def client(deps, facilitator):
    """The real app, over the offline facilitator."""
    app = create_app(deps, build_routes(deps))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def client_for(deps, facilitator):
    """A factory for a fresh TestClient whose `deps.settings` carries the
    given overrides -- for a test that needs settings the shared `client`
    fixture's app already baked in.

    `build_routes(deps)` captures `settings = deps.settings` once, when a
    route's price callable is built, so replacing `deps.settings` after the
    `client` fixture has already run never reaches it. This builds a new
    `Deps` with the override applied first and constructs the app from that,
    reusing the same `escrow`/`jobs`/`algod` -- and, through the `facilitator`
    dependency, the same monkeypatched facilitator -- as the shared fixtures.
    """
    import dataclasses

    def make(**overrides):
        patched = dataclasses.replace(
            deps, settings=dataclasses.replace(deps.settings, **overrides)
        )
        app = create_app(patched, build_routes(patched))
        return TestClient(app, raise_server_exceptions=False)

    return make


def accepts_of(response) -> list[dict]:
    """The `accepts` a 402 actually put on the wire.

    v2 carries the requirements in the PAYMENT-REQUIRED header rather than the
    body, so reading the body would find `{}` and conclude nothing was offered.
    """
    header = response.headers.get("payment-required")
    assert header, f"no PAYMENT-REQUIRED header on a {response.status_code}"
    return json.loads(base64.b64decode(header))["accepts"]


# Enough of a chain to build transactions that encode and decode. No round
# is ever reached and no signature is ever checked here: the facilitator is
# stubbed, and what the server inspects is the *shape* of the group.
_GENESIS_HASH = "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
JOIN_SELECTOR = abi.Method.from_signature(JOIN).get_selector()


def _suggested_params() -> transaction.SuggestedParams:
    return transaction.SuggestedParams(
        fee=0, first=1, last=1001, gh=_GENESIS_HASH, flat_fee=True
    )


def build_payment_group(
    requirements: dict,
    *,
    app_id: int = TEST_APP_ID,
    join_agreement_id: int | None = None,
    joins: int = 1,
    join_sender: str = BUYER,
    buyer: str = BUYER,
) -> list[str]:
    """The three-leg group a real buyer signs, as base64 msgpack.

    Unsigned, because nothing offline checks a signature -- but structurally
    the real thing, so the server's pre-settlement inspection of the group
    (api/payment_group.py) is exercised by every paid test rather than by a
    handful that remember to opt in. The defaults build a *correct* group for
    whatever requirements the server just issued; each keyword spoils exactly
    one property, so a test that spoils one thing is visibly testing that one
    thing.
    """
    if join_agreement_id is None:
        join_agreement_id = int((requirements.get("extra") or {}).get("agreementId", 0))
    sp = _suggested_params()

    legs = [
        transaction.PaymentTxn(sender=FEE_PAYER, sp=sp, receiver=FEE_PAYER, amt=0),
        transaction.AssetTransferTxn(
            sender=buyer,
            sp=sp,
            receiver=requirements["payTo"],
            amt=int(requirements["amount"]),
            index=int(requirements["asset"]),
        ),
    ]
    for _ in range(joins):
        legs.append(
            transaction.ApplicationCallTxn(
                sender=join_sender,
                sp=sp,
                index=app_id,
                on_complete=transaction.OnComplete.NoOpOC,
                app_args=[
                    JOIN_SELECTOR,
                    join_agreement_id.to_bytes(8, "big"),
                    (1).to_bytes(8, "big"),
                ],
            )
        )
    return [encoding.msgpack_encode(leg) for leg in legs]


def payment_header_for(requirements: dict, **group) -> str:
    """A payment header for requirements the server just issued.

    Signatures are stubbed, because the facilitator is the thing that checks
    them and it is stubbed too. Two things have to be real. `accepted`: the
    middleware runs find_matching_requirements against the live route, which
    compares scheme, network, amount, asset and payTo -- so the retry is
    matched by exactly the same code a real buyer's retry is. And
    `paymentGroup`: the paid handlers refuse before settlement if the group
    does not carry the `join` call that records the seat, so a stub group
    would make every paid test agree with a server that never sees one.

    Keyword arguments are passed through to `build_payment_group` for the
    tests that need a group spoiled in one specific way.
    """
    payload = PaymentPayload(
        payload={
            "paymentGroup": build_payment_group(requirements, **group),
            "paymentIndex": 1,
        },
        accepted=PaymentRequirements.model_validate(requirements),
    )
    return encode_payment_signature_header(payload)
