"""`POST /pin` is served only while `PIN_ROUTE_ENABLED` is on, and never on
MainNet.

The delivery half of `/pin` -- the upload, leg 2 and the release -- is not
built. Served anyway, every unpaid quote makes the admin key sign a
`create_agreement` (a deposit and a fee each), and every paid one takes USDC
that can only ever be refunded. With the route off, the server has no reason
to hold a signing key at all, and the pool route is the whole deployment.
"""

import dataclasses

import pytest
from algosdk import account, mnemonic
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2

from api.config import ConfigError, load_settings, verify_startup
from api.deps import build_deps
from api.routes import build_routes
from tests.conftest import APP_ACCOUNT, payment_header_for

MAINNET_USDC = 31566704
TESTNET_USDC = 10458941


class FakeAlgod:
    """Only what the guards call: an app account that is funded and opted in
    to whichever asset the configuration under test names."""

    def __init__(self, asset_id: int = TESTNET_USDC) -> None:
        self._asset_id = asset_id

    def account_info(self, address):
        return {
            "amount": 1_000_000,
            "assets": [{"asset-id": self._asset_id, "amount": 0, "is-frozen": False}],
        }


def _with(deps, **overrides):
    return dataclasses.replace(
        deps, settings=dataclasses.replace(deps.settings, **overrides)
    )


# --- the switch --------------------------------------------------------------

_BASE_ENV = {"USDC_ASA_ID": str(TESTNET_USDC), "APP_ID": "1"}


@pytest.mark.parametrize("raw", [None, "", "false", "FALSE", "0"])
def test_the_pin_route_is_off_unless_switched_on(raw):
    env = dict(_BASE_ENV)
    if raw is not None:
        env["PIN_ROUTE_ENABLED"] = raw
    assert load_settings(env).pin_route_enabled is False


@pytest.mark.parametrize("raw", ["true", "True", "1"])
def test_the_pin_route_is_on_when_switched_on(raw):
    env = {**_BASE_ENV, "PIN_ROUTE_ENABLED": raw, "PRICE_MICRO_USDC": "100000"}
    assert load_settings(env).pin_route_enabled is True


@pytest.mark.parametrize("raw", ["yes", "on", "ture"])
def test_an_unrecognised_switch_value_refuses_rather_than_guessing(raw):
    """A typo must not quietly decide whether a paid route is served."""
    with pytest.raises(ConfigError, match="PIN_ROUTE_ENABLED"):
        load_settings({**_BASE_ENV, "PIN_ROUTE_ENABLED": raw})


def test_the_delivery_price_is_not_required_while_the_pin_route_is_off():
    """`PRICE_MICRO_USDC` prices `/pin` and nothing else, so a pool-only
    deployment does not have to invent one."""
    load_settings(_BASE_ENV)


# --- the route table and the app ----------------------------------------------


def test_the_route_table_has_only_the_pool_route_while_pin_is_off(deps):
    assert set(build_routes(_with(deps, pin_route_enabled=False))) == {"POST /index"}


@pytest.mark.parametrize(
    "path",
    [
        "/pin",
        # Both fold onto the paid pattern. "//pin" would too, but httpx reads
        # it as a host and never sends it (tests/test_payment_integrity.py).
        "/pin/",
        "/pin//",
        "/pin?sha256=" + "a" * 64 + "&size=1024",
        # Malformed: the query gate would answer 400 if it were still mounted.
        "/pin?sha256=not-hex&size=1",
    ],
)
def test_every_spelling_of_pin_is_a_404_while_it_is_off(
    client_for, facilitator, deps, path
):
    response = client_for(pin_route_enabled=False).post(path)

    assert response.status_code == 404, response.text
    assert "payment-required" not in response.headers
    assert deps.escrow.created == 0


def test_a_paid_request_to_pin_settles_nothing_while_it_is_off(
    client_for, facilitator, deps
):
    requirements = {
        "scheme": "exact",
        "network": deps.settings.network,
        "asset": str(TESTNET_USDC),
        "amount": "100000",
        "payTo": APP_ACCOUNT,
        "maxTimeoutSeconds": 120,
        "extra": {"agreementId": 1},
    }
    response = client_for(pin_route_enabled=False).post(
        "/pin?sha256=" + "a" * 64 + "&size=1024",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert response.status_code == 404, response.text
    assert facilitator.verified == []
    assert facilitator.settled == []


def test_the_pool_route_is_still_served_while_pin_is_off(client_for):
    response = client_for(pin_route_enabled=False, index_agreement_id=0).post("/index")

    assert response.status_code == 503, response.text
    assert response.json()["error"] == "no_pool_open"


# --- startup ----------------------------------------------------------------


def test_the_pool_route_alone_passes_the_startup_guards(deps):
    pool_only = _with(deps, pin_route_enabled=False)
    verify_startup(pool_only.settings, build_routes(pool_only), FakeAlgod())


def test_a_pin_route_served_while_the_switch_is_off_refuses(deps):
    """The switch and the route table must agree. A `/pin` route reaching the
    payment middleware under a configuration that says it is off is the one
    disagreement that takes money."""
    with_pin = build_routes(_with(deps, pin_route_enabled=True))
    settings_off = dataclasses.replace(deps.settings, pin_route_enabled=False)

    with pytest.raises(ConfigError, match="POST /pin"):
        verify_startup(settings_off, with_pin, FakeAlgod())


def test_mainnet_refuses_to_boot_with_the_pin_route_on(settings_factory):
    settings = settings_factory(
        network=ALGORAND_MAINNET_CAIP2,
        usdc_asa_id=MAINNET_USDC,
        pin_route_enabled=True,
    )
    with pytest.raises(ConfigError, match="PIN_ROUTE_ENABLED"):
        verify_startup(settings, {}, FakeAlgod(MAINNET_USDC))


def test_mainnet_boots_with_the_pin_route_off(settings_factory):
    """The counterpart: without it, a guard that refused every MainNet
    configuration would pass the test above."""
    settings = settings_factory(
        network=ALGORAND_MAINNET_CAIP2,
        usdc_asa_id=MAINNET_USDC,
        pin_route_enabled=False,
    )
    verify_startup(settings, {}, FakeAlgod(MAINNET_USDC))


def _fresh_mnemonic() -> str:
    """Generated per run and never kept. What these tests turn on is a key
    being present at all, and a fixed phrase would sit in the repository."""
    private_key, _ = account.generate_account()
    return mnemonic.from_private_key(private_key)


def test_a_signing_key_present_while_the_pin_route_is_off_refuses_to_boot(
    settings_factory,
):
    """With `/pin` off the server signs nothing, so a key in its environment --
    a platform secret left behind, a shared `.env` -- is one it must not hold."""
    settings = settings_factory(
        pin_route_enabled=False, admin_mnemonic=_fresh_mnemonic()
    )

    with pytest.raises(ConfigError, match="ADMIN_MNEMONIC"):
        verify_startup(settings, {}, FakeAlgod())


# --- assembly ------------------------------------------------------------------


def test_the_pool_only_assembly_holds_no_signer_and_opens_no_store(
    settings_factory, tmp_path
):
    store = tmp_path / "jobs.sqlite3"
    settings = settings_factory(pin_route_enabled=False, job_db_path=str(store))

    deps = build_deps(settings, FakeAlgod())

    assert deps.jobs is None
    assert not store.exists()
    with pytest.raises(ValueError, match="read-only"):
        deps.escrow.create_hash_agreement(
            commit_hash=bytes(32),
            share_price=100_000,
            deadline=2_000_000,
            beneficiary=APP_ACCOUNT,
            verifier=APP_ACCOUNT,
        )


def test_the_pin_route_assembly_opens_the_job_store(settings_factory, tmp_path):
    store = tmp_path / "jobs.sqlite3"
    settings = settings_factory(
        admin_mnemonic=_fresh_mnemonic(), job_db_path=str(store)
    )

    deps = build_deps(settings, FakeAlgod())

    assert deps.jobs is not None
    assert store.exists()
    deps.jobs.close()


def test_the_pool_only_assembly_refuses_a_signing_key(settings_factory, tmp_path):
    settings = settings_factory(
        pin_route_enabled=False, admin_mnemonic=_fresh_mnemonic()
    )

    with pytest.raises(ConfigError, match="ADMIN_MNEMONIC"):
        build_deps(settings, FakeAlgod())


def test_mainnet_refuses_the_pin_route_before_any_key_is_decoded(
    settings_factory, tmp_path
):
    """The operator reads the guard's reason, not a symptom of it: an
    unreadable key, or a store created for a server that then refuses to
    start."""
    store = tmp_path / "jobs.sqlite3"
    settings = settings_factory(
        network=ALGORAND_MAINNET_CAIP2,
        usdc_asa_id=MAINNET_USDC,
        pin_route_enabled=True,
        admin_mnemonic="not a mnemonic",
        job_db_path=str(store),
    )

    with pytest.raises(ConfigError, match="PIN_ROUTE_ENABLED"):
        build_deps(settings, FakeAlgod(MAINNET_USDC))
    assert not store.exists()
