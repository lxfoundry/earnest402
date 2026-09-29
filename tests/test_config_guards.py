"""Each guard of the backend spec's startup table, one test per refusal.

A guard that does not refuse is worse than no guard: it reports a configuration
as checked. So every test here asserts a *rejection*, and one asserts that a
correct configuration is accepted -- without that last one, a `verify_startup`
that raised unconditionally would pass the whole module.

The stakes are why this is a boot-time refusal rather than a warning: the
challenge tag is stamped at settlement and never reclassified, so a first
MainNet settlement served from a wrong configuration cannot be repaired
afterwards.
"""

import dataclasses

import pytest
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2, ALGORAND_TESTNET_CAIP2

from api.config import (
    CHALLENGE_TAG,
    DEFAULT_INDEX_DESCRIPTION,
    INDEX_DESCRIPTION_CLAUSES,
    ConfigError,
    load_settings,
    verify_startup,
)

APP_ACCOUNT = "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY"
OTHER_ACCOUNT = "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M"
FEE_PAYER = OTHER_ACCOUNT


class FakeAlgod:
    """Only what the guards call. `account_info` is the whole surface."""

    def __init__(self, *, amount=1_000_000, opted_in=True, frozen=False):
        self._amount = amount
        self._opted_in = opted_in
        self._frozen = frozen

    def account_info(self, address):
        assets = []
        if self._opted_in:
            assets = [{"asset-id": 10458941, "amount": 0, "is-frozen": self._frozen}]
        return {"amount": self._amount, "assets": assets}


# --- network <-> asset -------------------------------------------------


def test_a_mainnet_network_with_a_testnet_asset_refuses(settings_factory):
    """The pairing that would price a MainNet route in play money."""
    settings = settings_factory(network=ALGORAND_MAINNET_CAIP2, usdc_asa_id=10458941)
    with pytest.raises(ConfigError, match="asset"):
        verify_startup(settings, {}, FakeAlgod())


def test_an_unknown_network_refuses(settings_factory):
    settings = settings_factory(network="algorand:not-a-real-genesis-hash")
    with pytest.raises(ConfigError, match="unknown network"):
        verify_startup(settings, {}, FakeAlgod())


# --- network <-> application -------------------------------------------


def test_app_account_must_be_derived_from_the_app_id(settings_factory):
    settings = settings_factory(app_account=OTHER_ACCOUNT)
    with pytest.raises(ConfigError, match="application address"):
        verify_startup(settings, {}, FakeAlgod())


def test_a_missing_app_id_refuses(settings_factory):
    with pytest.raises(ConfigError, match="APP_ID"):
        verify_startup(settings_factory(app_id=0), {}, FakeAlgod())


def test_a_production_app_id_outside_mainnet_refuses(settings_factory):
    """Development and production are different applications, therefore
    different payTo addresses, therefore different merchant identities."""
    settings = settings_factory(
        network=ALGORAND_TESTNET_CAIP2, production_app_id=769608941, app_id=769608941
    )
    with pytest.raises(ConfigError, match="production"):
        verify_startup(settings, {}, FakeAlgod())


# --- app-account readiness ---------------------------------------------


def test_an_unfunded_app_account_refuses(settings_factory):
    with pytest.raises(ConfigError, match="funded"):
        verify_startup(settings_factory(), {}, FakeAlgod(amount=0))


def test_an_app_account_not_opted_in_refuses(settings_factory):
    """A 402 the payTo cannot honour: a settle against a non-opted-in account
    fails at the facilitator layer, after the buyer has signed."""
    with pytest.raises(ConfigError, match="opted in"):
        verify_startup(settings_factory(), {}, FakeAlgod(opted_in=False))


def test_a_frozen_app_account_holding_refuses(settings_factory):
    """Opt-in is only half of it. A frozen holding is an opted-in holding that
    still rejects every transfer, and MainNet USDC declares a freeze address."""
    with pytest.raises(ConfigError, match="frozen"):
        verify_startup(settings_factory(), {}, FakeAlgod(frozen=True))


# --- resource ----------------------------------------------------------


def test_a_non_https_resource_refuses(settings_factory):
    settings = settings_factory(pin_resource_url="http://earnest.example/pin")
    with pytest.raises(ConfigError, match="https"):
        verify_startup(settings, {}, FakeAlgod())


def test_a_resource_off_the_configured_host_refuses(settings_factory):
    """One root domain per merchant account: endpoints differentiate resources,
    never domains."""
    settings = settings_factory(pin_resource_url="https://elsewhere.example/pin")
    with pytest.raises(ConfigError, match="public host"):
        verify_startup(settings, {}, FakeAlgod())


def test_every_configured_resource_must_be_https_on_the_public_host(settings_factory):
    """`_check_resource` used to compare a single `resource_url` against the
    host. It now loops over every configured resource, and this is the test
    that the loop actually reaches the second entry rather than stopping
    after the pin route passes -- the pin resource is pinned to the same host
    in both cases below, so only the pool resource is ever the bad one."""
    with pytest.raises(ConfigError, match="is not on the configured"):
        verify_startup(
            settings_factory(
                public_host="https://example.test",
                pin_resource_url="https://example.test/pin",
                index_resource_url="https://elsewhere.test/index",
            ),
            {},
            FakeAlgod(),
        )

    with pytest.raises(ConfigError, match="must be https"):
        verify_startup(
            settings_factory(
                public_host="https://example.test",
                pin_resource_url="https://example.test/pin",
                index_resource_url="http://example.test/index",
            ),
            {},
            FakeAlgod(),
        )


# --- per-route guards --------------------------------------------------


def test_two_routes_with_two_pinned_resources_both_pass_the_guard(
    settings_factory, route_factory
):
    """The guard used to compare every route against one configured URL.

    Two products share one payTo, so both have to boot from one settings
    object. This is the check that the single-route assumption is gone.
    """
    settings = settings_factory(
        public_host="https://example.test",
        pin_resource_url="https://example.test/pin",
        index_resource_url="https://example.test/index",
    )
    routes = {
        **route_factory(resource="https://example.test/pin"),
        **route_factory(pattern="POST /index", resource="https://example.test/index"),
    }

    verify_startup(settings, routes, FakeAlgod())  # must not raise


def test_a_route_pinned_to_an_unconfigured_resource_is_rejected(
    settings_factory, route_factory
):
    settings = settings_factory(
        public_host="https://example.test",
        pin_resource_url="https://example.test/pin",
        index_resource_url="https://example.test/index",
    )
    routes = route_factory(pattern="POST /other", resource="https://example.test/other")

    with pytest.raises(ConfigError, match="not one of the configured resources"):
        verify_startup(settings, routes, FakeAlgod())


def test_the_challenge_tag_is_required_on_every_route_not_just_the_first(
    settings_factory, route_factory
):
    """Attribution is stamped at settlement and never reclassified, so a
    second route missing the tag is permanent and silent. The guard loops, so
    this is really a test that nothing short-circuits after the first route."""
    settings = settings_factory(
        public_host="https://example.test",
        pin_resource_url="https://example.test/pin",
        index_resource_url="https://example.test/index",
    )
    routes = {
        **route_factory(resource="https://example.test/pin", tag=CHALLENGE_TAG),
        **route_factory(
            pattern="POST /index",
            resource="https://example.test/index",
            tag=None,
        ),
    }

    with pytest.raises(ConfigError, match="price.extra.tag"):
        verify_startup(settings, routes, FakeAlgod())


def test_a_route_without_the_challenge_tag_refuses(settings_factory, route_factory):
    """The unrepairable one. Attribution is stamped at settlement."""
    with pytest.raises(ConfigError, match="tag"):
        verify_startup(settings_factory(), route_factory(tag=None), FakeAlgod())


def test_a_route_with_the_wrong_tag_refuses(settings_factory, route_factory):
    with pytest.raises(ConfigError, match="tag"):
        verify_startup(
            settings_factory(), route_factory(tag="x402-challenge"), FakeAlgod()
        )


def test_a_route_without_a_fee_payer_refuses(settings_factory, route_factory):
    with pytest.raises(ConfigError, match="feePayer"):
        verify_startup(settings_factory(), route_factory(fee_payer=None), FakeAlgod())


def test_a_route_without_a_pay_to_refuses(settings_factory, route_factory):
    with pytest.raises(ConfigError, match="payTo"):
        verify_startup(settings_factory(), route_factory(pay_to=""), FakeAlgod())


def test_a_route_whose_resource_is_unpinned_refuses(settings_factory, route_factory):
    with pytest.raises(ConfigError, match="pinned"):
        verify_startup(settings_factory(), route_factory(resource=None), FakeAlgod())


def test_max_timeout_must_equal_the_funding_window(settings_factory, route_factory):
    """Advertising 300 seconds to settle while sweeping at 120 promises the
    buyer a window we do not honour."""
    with pytest.raises(ConfigError, match="max_timeout_seconds"):
        verify_startup(
            settings_factory(), route_factory(max_timeout_seconds=300), FakeAlgod()
        )


def test_a_route_without_a_hook_timeout_refuses(settings_factory, route_factory):
    """x402-avm awaits the price callable with wait_for(timeout=None) when this
    is unset -- no bound at all on a call that goes to chain."""
    with pytest.raises(ConfigError, match="hook_timeout_seconds is unset"):
        verify_startup(
            settings_factory(), route_factory(hook_timeout_seconds=None), FakeAlgod()
        )


def test_a_hook_timeout_outside_the_funding_window_refuses(
    settings_factory, route_factory
):
    """A quote allowed to run for the whole window leaves no time to pay in."""
    with pytest.raises(ConfigError, match="funding window"):
        verify_startup(
            settings_factory(funding_window_seconds=120),
            route_factory(hook_timeout_seconds=120),
            FakeAlgod(),
        )


def test_a_string_price_refuses(settings_factory, route_factory):
    """The `"$0.01"` shorthand silently discards `extra`, and with it feePayer
    and the challenge tag. Payments still settle -- untagged."""
    with pytest.raises(ConfigError, match="string price"):
        verify_startup(settings_factory(), route_factory(price="$0.01"), FakeAlgod())


def test_a_price_callable_that_hits_a_capacity_limit_refuses_as_capacity_not_config(
    settings_factory, route_factory
):
    """A crash-restart loop resamples the same callable on every boot, walking
    the same unfunded-agreement ceiling and rate limiter a real caller would --
    so a RuntimeError from the callable is very likely restart pressure, not a
    bad environment variable, and the refusal message should say so rather
    than sending an operator hunting through config."""

    def price(ctx):
        raise RuntimeError("too many unfunded agreements")

    with pytest.raises(ConfigError, match="capacity limit"):
        verify_startup(settings_factory(), route_factory(price=price), FakeAlgod())


def test_a_price_callable_that_raises_anything_else_still_refuses_at_boot(
    settings_factory, route_factory
):
    """Not every failure while sampling is a capacity problem; whatever it is,
    it still refuses -- boot cannot serve a 402 it cannot sample."""

    def price(ctx):
        raise ValueError("malformed sample query")

    with pytest.raises(ConfigError, match="sampling the price callable raised"):
        verify_startup(settings_factory(), route_factory(price=price), FakeAlgod())


# --- the accepting case ------------------------------------------------


def test_a_correct_configuration_boots(settings_factory, route_factory):
    """Without this, a verify_startup that raised unconditionally would pass
    every other test in this module."""
    verify_startup(settings_factory(), route_factory(), FakeAlgod())


# --- loading -----------------------------------------------------------


def test_load_settings_reads_an_explicit_mapping_and_never_the_process_env():
    """Taking the environment as an argument is what makes the loader testable
    and keeps os.environ reachable from exactly one place."""
    settings = load_settings(
        {
            "NETWORK": ALGORAND_TESTNET_CAIP2,
            "USDC_ASA_ID": "10458941",
            "APP_ID": "769608941",
            "APP_ACCOUNT": APP_ACCOUNT,
            "PUBLIC_HOST": "https://earnest.example/",
            "PRICE_MICRO_USDC": "100000",
        }
    )
    assert settings.usdc_asa_id == 10458941
    assert settings.app_id == 769608941
    # A trailing slash on the host would produce `https://earnest.example//pin`.
    assert settings.public_host == "https://earnest.example"
    assert settings.resource_url == "https://earnest.example/pin"
    assert settings.funding_window_seconds == 120


def test_a_required_value_that_is_missing_refuses():
    with pytest.raises(ConfigError, match="PRICE_MICRO_USDC"):
        load_settings(
            {"USDC_ASA_ID": "10458941", "APP_ID": "1", "PIN_ROUTE_ENABLED": "true"}
        )


def test_an_empty_value_is_treated_as_absent_and_the_default_applies():
    """`.env.example` ships every optional key present and empty, and
    `load_dotenv()` copies those empty strings into the environment. A plain
    `env.get(name, default)` finds the key, returns "", and the documented
    default never applies -- so a fresh clone that followed the template got a
    server refusing to boot on `resource must be https://, got ''`, a message
    pointing at the wrong thing entirely.

    `_int` always treated empty as absent. This is the string reads agreeing
    with it.
    """
    base = {
        "USDC_ASA_ID": "10458941",
        "APP_ID": "769608941",
        "PUBLIC_HOST": "https://earnest.example",
        "PRICE_MICRO_USDC": "100000",
    }

    # Absent, and present-but-empty, must produce the same settings.
    absent = load_settings(base)
    empty = load_settings(
        {
            **base,
            "RESOURCE_URL": "",
            "NETWORK": "",
            "FACILITATOR_URL": "",
            "JOB_DB_PATH": "",
            "MAX_FILE_BYTES": "",
        }
    )
    assert empty == absent
    assert empty.resource_url == "https://earnest.example/pin"
    assert empty.network == ALGORAND_TESTNET_CAIP2
    assert empty.facilitator_url == "https://facilitator.goplausible.xyz"
    assert empty.job_db_path == "jobs.sqlite3"
    assert empty.max_file_bytes == 16 * 1024 * 1024


def test_the_shipped_template_boots(tmp_path):
    """The end-to-end form of the above: fill in only the values `.env.example`
    leaves for the operator and check the result passes the guards. This is the
    path a fresh clone actually takes."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    template = (root / ".env.example").read_text(encoding="utf-8")

    env = dict(re.findall(r"^([A-Z0-9_]+)=(.*)$", template, re.MULTILINE))
    env.update(
        {
            "APP_ID": "769608941",
            "APP_ACCOUNT": APP_ACCOUNT,
            "FEE_PAYER": OTHER_ACCOUNT,
            "TREASURY": OTHER_ACCOUNT,
            "VERIFIER": OTHER_ACCOUNT,
            "PUBLIC_HOST": "https://earnest.example",
        }
    )

    settings = load_settings(env)
    verify_startup(settings, {}, FakeAlgod())


def test_a_non_numeric_value_refuses():
    with pytest.raises(ConfigError, match="must be an integer"):
        load_settings(
            {
                "USDC_ASA_ID": "ten million",
                "APP_ID": "1",
                "PRICE_MICRO_USDC": "1",
            }
        )


def test_settings_are_frozen(settings_factory):
    """Configuration is read once at boot and never mutated afterwards; a
    later task that reassigns a field should fail loudly rather than diverge
    from what the guards checked."""
    settings = settings_factory()
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.app_id = 1  # type: ignore[misc]


def test_env_example_documents_every_variable_the_loader_reads():
    """The drift this catches is the one that has already bitten once: a value
    added to the deployment and to the code, and to no document. `.env.example`
    is the only place a newcomer learns a variable exists.
    """
    import pathlib
    import re

    # Anchored to this file rather than the working directory: a relative path
    # would resolve against wherever pytest happened to be invoked from, and
    # silently pass by reading nothing.
    root = pathlib.Path(__file__).resolve().parents[1]

    # Discovered by *running* the loader against a mapping that records every
    # key it asks for, rather than by pattern-matching the source. A regex has
    # to know the name of every accessor -- it missed each `_str(...)` read the
    # moment that helper was introduced -- and a check that silently stops
    # covering half the variables is worse than no check.
    class RecordingEnv(dict):
        def __init__(self):
            super().__init__()
            self.seen: set[str] = set()

        def get(self, key, default=None):
            self.seen.add(key)
            return "1"  # parses as an int and is non-empty as a string

    recorder = RecordingEnv()
    load_settings(recorder)
    read = recorder.seen
    assert read, "the loader asked for no environment variables at all"

    documented = set(
        re.findall(
            r"^([A-Z0-9_]+)=",
            (root / ".env.example").read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    missing = sorted(read - documented)
    assert not missing, f".env.example does not document: {missing}"


# --- the real boot combination -----------------------------------------


def test_the_real_route_table_passes_the_guards_and_costs_nothing(deps):
    """The combination production actually boots with, which nothing else here
    exercises: real settings, `build_routes(deps)`, one guard run.

    Every other route test in this module uses `route_factory`, which builds a
    RouteConfig by hand. A defect in build_routes -- a missing hook timeout, a
    price that stopped carrying the tag -- would pass all of them.

    The second half is the cost. Boot must not park an agreement: `_sample_price`
    asks the callable for `.sample()` rather than running it, so a restart loop
    cannot walk the unfunded ceiling into a boot it can never complete.
    """
    from api.routes import build_routes

    verify_startup(deps.settings, build_routes(deps), FakeAlgod())

    assert deps.escrow.created == 0
    assert deps.jobs.count_unfunded() == 0


def test_a_callable_price_whose_sample_lacks_the_tag_refuses(
    settings_factory, route_factory
):
    """The sampled path needs its own tag guard, not just the static one.

    Every other tag test passes a plain AssetAmount, which reaches the check
    directly. This one goes through the sampling branch, which is the branch
    the real route uses.
    """
    from x402.schemas import AssetAmount

    def price(ctx):  # pragma: no cover - the sample is what gets called
        raise AssertionError("the guard must use .sample(), not call this")

    price.sample = lambda: AssetAmount(
        amount="100000", asset="10458941", extra={"decimals": 6, "feePayer": FEE_PAYER}
    )

    with pytest.raises(ConfigError, match="tag"):
        verify_startup(settings_factory(), route_factory(price=price), FakeAlgod())


def test_a_callable_price_is_sampled_through_its_sample_not_by_calling_it(
    settings_factory, route_factory
):
    """Pins the mechanism, so a revert to calling the callable fails loudly."""
    from x402.schemas import AssetAmount

    calls = []

    def price(ctx):
        calls.append(ctx)
        raise AssertionError("the guard called the callable instead of sampling it")

    price.sample = lambda: AssetAmount(
        amount="100000",
        asset="10458941",
        extra={
            "decimals": 6,
            "feePayer": FEE_PAYER,
            "tag": "x402-global-challenge",
        },
    )

    verify_startup(settings_factory(), route_factory(price=price), FakeAlgod())
    assert calls == []


def test_an_invalid_treasury_address_refuses(settings_factory):
    """Named at boot rather than surfacing as a price-callable failure."""
    settings = settings_factory(treasury="not-an-address")
    with pytest.raises(ConfigError, match="TREASURY is not a valid"):
        verify_startup(settings, {}, FakeAlgod())


def test_an_empty_verifier_refuses(settings_factory):
    """`_str` returns "" for an absent value, and the design requires a
    non-zero verifier -- so an unset VERIFIER would otherwise reach a create."""
    with pytest.raises(ConfigError, match="VERIFIER is required"):
        verify_startup(settings_factory(verifier=""), {}, FakeAlgod())


# --- the pool route's listing ------------------------------------------

# Enough for the loader with POST /pin off, which is the shipped default.
_MINIMAL_ENV = {"USDC_ASA_ID": "10458941", "APP_ID": "1"}

# A reworded listing that keeps every required clause: the shape of override
# a deployment selling something other than the default edition would set.
_REWORDED_INDEX_DESCRIPTION = (
    "Your payment group must include a join call naming extra.agreementId; a "
    "bare transfer is not a purchase. Join a pool that buys one sample "
    "dataset whose sha256 was committed before you paid, released against "
    "matching bytes and refunded on-chain otherwise. One seat per address. "
    "Machine-verified only: no arbitration."
)


def test_the_index_description_is_read_from_the_environment():
    settings = load_settings(
        {**_MINIMAL_ENV, "INDEX_DESCRIPTION": _REWORDED_INDEX_DESCRIPTION}
    )
    assert settings.index_description == _REWORDED_INDEX_DESCRIPTION


@pytest.mark.parametrize(
    "override", [{}, {"INDEX_DESCRIPTION": ""}], ids=["unset", "empty"]
)
def test_an_unset_or_empty_index_description_is_the_default(override):
    """`.env.example` ships the key present and empty, so empty has to mean
    the default exactly as absent does."""
    settings = load_settings({**_MINIMAL_ENV, **override})
    assert settings.index_description == DEFAULT_INDEX_DESCRIPTION


def test_the_default_index_description_passes_the_guard(settings_factory):
    settings = settings_factory()
    assert settings.index_description == DEFAULT_INDEX_DESCRIPTION
    verify_startup(settings, {}, FakeAlgod())


@pytest.mark.parametrize("clause", INDEX_DESCRIPTION_CLAUSES)
def test_an_index_description_missing_a_clause_refuses_naming_it(
    settings_factory, clause
):
    """One clause dropped from an otherwise complete listing. The refusal
    names that clause and no other, so an operator reads what to put back
    rather than the whole list."""
    description = DEFAULT_INDEX_DESCRIPTION.replace(clause, "")
    assert clause not in description

    with pytest.raises(ConfigError, match="INDEX_DESCRIPTION") as refused:
        verify_startup(settings_factory(index_description=description), {}, FakeAlgod())

    message = str(refused.value)
    assert repr(clause) in message
    assert not [
        c for c in INDEX_DESCRIPTION_CLAUSES if c != clause and repr(c) in message
    ]


def test_every_missing_clause_is_named_not_just_the_first(settings_factory):
    with pytest.raises(ConfigError) as refused:
        verify_startup(settings_factory(index_description="A pool."), {}, FakeAlgod())
    for clause in INDEX_DESCRIPTION_CLAUSES:
        assert repr(clause) in str(refused.value)


def test_an_index_description_override_carrying_every_clause_boots(
    settings_factory,
):
    """The accepting case: an override may reword and reorder freely."""
    settings = settings_factory(index_description=_REWORDED_INDEX_DESCRIPTION)
    verify_startup(settings, {}, FakeAlgod())
