"""Fixtures for the TestNet circuit test.

What this layer answers, and what it does not: the real facilitator on TestNet
answers *is this shape acceptable*; the offline suite and the LocalNet fakes
answer *does the server do the right thing on every branch, every time*. So
nothing here stubs the facilitator, and nothing here reaches for a branch a
cheaper layer already covers.

Everything is session-scoped. A run costs real settlements and a real wait for
a real deadline, so the server, the clients and the accounts are built once.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from algosdk.logic import get_application_address
from algosdk.v2client import algod as algod_v2
from dotenv import dotenv_values
from x402.mechanisms.avm import ALGORAND_TESTNET_CAIP2, USDC_TESTNET_ASA_ID

from api.app import create_app
from api.config import Settings
from api.deps import Deps
from api.escrow import EscrowClient
from api.jobs import JobStore
from api.routes import build_routes
from tests.e2e.harness import (
    ENV_PATH,
    MIN_ADMIN_MICROALGO,
    Account,
    Env,
    algo_balance,
    algod_url_from,
    asset_id_from,
    build_settings,
    send_usdc,
    usdc_balance,
)
from tests.e2e.serve import serve


def pytest_collection_modifyitems(config, items):
    """Gate 2: these tests run only when `testnet` is named on the command line.

    `E2E_ENABLED` is gate 1, but `agents.e2e_bootstrap` writes it on the first
    run and never removes it, so on a bootstrapped machine gate 1 is always
    open. Without this hook the marker would be a label rather than a gate, and
    every command that does not filter it out -- a bare `pytest`, or the
    `pytest -m "not localnet"` that AGENTS.md calls offline -- would settle real
    payments. An `-m` that names `testnet` at all, in either direction, is
    already deciding for itself and is left alone.
    """
    if "testnet" in (config.getoption("markexpr", default="") or ""):
        return
    skip = pytest.mark.skip(
        reason="TestNet circuit test: real settlements. Select it with `-m testnet`."
    )
    for item in items:
        if "testnet" in item.keywords:
            item.add_marker(skip)


REQUIRED = (
    "APP_ID",
    "APP_ACCOUNT",
    "TREASURY",
    "VERIFIER",
    "ADMIN_MNEMONIC",
    "E2E_BUYER_MNEMONIC",
    "E2E_VERIFIER_MNEMONIC",
    "E2E_TREASURY_MNEMONIC",
)


@pytest.fixture(scope="session")
def e2e_env() -> Env:
    """The `.env` values this suite signs with, or a skip explaining what to do.

    Skipping rather than failing is what keeps a plain `pytest` safe on a
    machine that never did the bootstrap -- including CI, which has no `.env`
    at all and must never reach a network.
    """
    values = Env((k, v) for k, v in dotenv_values(ENV_PATH).items() if v)
    if not values.get("E2E_ENABLED"):
        pytest.skip(
            "E2E_ENABLED is not set in .env. Run `python -m agents.e2e_bootstrap`, "
            "fund the addresses it prints, then deploy the escrow application."
        )
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        pytest.skip(
            f".env is missing {', '.join(missing)}; re-run agents.e2e_bootstrap"
        )
    return values


@pytest.fixture(scope="session")
def algod(e2e_env: dict[str, str]) -> algod_v2.AlgodClient:
    return algod_v2.AlgodClient(e2e_env.get("ALGOD_TOKEN", ""), algod_url_from(e2e_env))


@pytest.fixture(scope="session")
def admin(e2e_env: dict[str, str]) -> Account:
    return Account(e2e_env["ADMIN_MNEMONIC"])


@pytest.fixture(scope="session")
def verifier(e2e_env: dict[str, str]) -> Account:
    return Account(e2e_env["E2E_VERIFIER_MNEMONIC"])


@pytest.fixture(scope="session")
def treasury(e2e_env: dict[str, str]) -> Account:
    return Account(e2e_env["E2E_TREASURY_MNEMONIC"])


@pytest.fixture(scope="session")
def buyer(e2e_env: dict[str, str]) -> Account:
    return Account(e2e_env["E2E_BUYER_MNEMONIC"])


def _escrow_client(algod: Any, env: dict[str, str], account: Account) -> EscrowClient:
    """An escrow client that signs as `account`.

    `EscrowClient` names its signing identity `admin_*` because creating an
    agreement is the only call the backend makes today, and that one is admin
    only. Release is not -- it is the agreement's own immutable verifier -- and
    expire, refund and close are permissionless. So the suite builds one client
    per identity rather than one per application, which is also how it can
    assert that a path really is open to somebody other than the admin.
    """
    return EscrowClient(
        algod,
        app_id=int(env["APP_ID"]),
        app_account=env["APP_ACCOUNT"],
        usdc_asa_id=asset_id_from(env),
        admin_address=account.address,
        admin_signer=account.atc_signer,
    )


@pytest.fixture(scope="session")
def as_admin(algod: Any, e2e_env: dict[str, str], admin: Account) -> EscrowClient:
    return _escrow_client(algod, e2e_env, admin)


@pytest.fixture(scope="session")
def as_verifier(algod: Any, e2e_env: dict[str, str], verifier: Account) -> EscrowClient:
    return _escrow_client(algod, e2e_env, verifier)


@pytest.fixture(scope="session")
def as_buyer(algod: Any, e2e_env: dict[str, str], buyer: Account) -> EscrowClient:
    """Proves `expire`, `refund_next` and `close` really are permissionless."""
    return _escrow_client(algod, e2e_env, buyer)


@pytest.fixture(scope="session")
def job_db_path(tmp_path_factory) -> str:
    """A fresh store per run, so a rerun never inherits a stale quote."""
    return str(tmp_path_factory.mktemp("jobs") / "jobs.sqlite3")


@pytest.fixture(scope="session")
def server(
    e2e_env: dict[str, str], algod: Any, admin: Account, job_db_path: str
) -> Iterator[str]:
    """The resource server under test, on a loopback port.

    `verify_startup` is deliberately not run: `tests/test_config_guards.py`
    covers every guard offline and exhaustively, and the one thing it cannot
    cover -- whether the live application account can actually receive -- is
    what `preflight` checks instead.
    """

    def build_app(base_url: str):
        settings = build_settings(e2e_env, base_url, job_db_path)
        deps = Deps(
            settings=settings,
            escrow=EscrowClient(
                algod,
                app_id=settings.app_id,
                app_account=settings.app_account,
                usdc_asa_id=settings.usdc_asa_id,
                admin_address=admin.address,
                admin_signer=admin.atc_signer,
            ),
            jobs=JobStore(settings.job_db_path),
            algod=algod,
            http=None,
        )
        return create_app(deps, build_routes(deps))

    with serve(build_app) as base_url:
        yield base_url


@pytest.fixture(scope="session")
def settings(e2e_env: dict[str, str], server: str, job_db_path: str) -> Settings:
    """The same settings the running server was built from."""
    return build_settings(e2e_env, server, job_db_path)


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    # Generous: the unpaid pass creates an agreement on chain before it can
    # answer, and the paid pass settles a group through the facilitator.
    with httpx.Client(timeout=120.0) as client:
        yield client


@pytest.fixture(scope="session", autouse=True)
def preflight(
    e2e_env: dict[str, str],
    algod: Any,
    admin: Account,
    verifier: Account,
    treasury: Account,
    buyer: Account,
) -> None:
    """Everything that must already be true, checked once and named plainly.

    These are the live half of `verify_startup` plus the buyer-side facts it
    has no reason to know about. Each is a failure that would otherwise surface
    deep inside a settle, as somebody else's error message.
    """
    app_id = int(e2e_env["APP_ID"])
    app_account = e2e_env["APP_ACCOUNT"]
    asset_id = asset_id_from(e2e_env)
    price = int(e2e_env.get("PRICE_MICRO_USDC") or "100000")

    # Pinned first, and to the chain rather than to the .env that describes it.
    # `verify_startup` is skipped here (see `server`), and its
    # `_check_network_asset` only checks that the network and the asset agree --
    # a coherent MainNet .env passes it. Nothing else in this suite would refuse
    # to settle against the wrong chain, and a settlement is irreversible.
    genesis = algod.suggested_params().gen
    assert genesis == "testnet-v1.0", (
        f"algod at {algod_url_from(e2e_env)} is on {genesis!r}, not TestNet. "
        "This suite settles real payments; point ALGOD_URL at TestNet."
    )
    network = e2e_env.get("NETWORK", ALGORAND_TESTNET_CAIP2)
    assert network == ALGORAND_TESTNET_CAIP2, (
        f"NETWORK is {network!r}, not TestNet. The quote the buyer signs would "
        "name a chain the suite is not running against."
    )
    assert asset_id == USDC_TESTNET_ASA_ID, (
        f"USDC_ASA_ID is {asset_id}, not TestNet USDC ({USDC_TESTNET_ASA_ID})."
    )

    # The verifier holding a key of its own is what makes the release condition
    # a constraint rather than a promise; four roles that collapse into one
    # would pass every assertion in the happy path while proving nothing.
    addresses = {
        "admin": admin.address,
        "verifier": verifier.address,
        "treasury": treasury.address,
        "buyer": buyer.address,
    }
    assert len(set(addresses.values())) == len(addresses), (
        f"the four roles are not four distinct accounts: {addresses}. "
        "Re-run `python -m agents.e2e_bootstrap`."
    )

    # The server reads VERIFIER and TREASURY from .env; this suite signs with
    # E2E_*_MNEMONIC. `e2e_bootstrap` skips any key already set, so a .env that
    # predates it can carry an address from one and a key from the other -- and
    # the failure surfaces as a bare contract rejection inside `release_hash`.
    for role, account in (("VERIFIER", verifier), ("TREASURY", treasury)):
        configured = e2e_env[role]
        assert configured == account.address, (
            f"{role} is {configured} but E2E_{role}_MNEMONIC signs for "
            f"{account.address}. The server and this suite disagree about who "
            f"the {role.lower()} is; reconcile .env."
        )

    derived = get_application_address(app_id)
    assert derived == app_account, (
        f"APP_ACCOUNT {app_account} is not the address of APP_ID {app_id} "
        f"({derived}). Deploy the escrow and re-record both."
    )

    info = algod.account_info(app_account)
    assert int(info.get("amount", 0)) > 0, (
        f"application account {app_account} is unfunded; it cannot pay the "
        "inner-transaction fees a release or refund charges it"
    )
    holdings = {a["asset-id"]: a for a in info.get("assets", [])}
    assert asset_id in holdings, (
        f"application account is not opted in to {asset_id}; a settlement into "
        "it would fail at the facilitator layer"
    )
    assert not holdings[asset_id].get("is-frozen"), (
        "the application account's holding is frozen; opt-in is only half of it"
    )

    for role, account in (("treasury", treasury), ("buyer", buyer)):
        held = {
            a["asset-id"]: a
            for a in algod.account_info(account.address).get("assets", [])
        }
        assert asset_id in held, (
            f"{role} {account.address} is not opted in to {asset_id}. Run "
            f"`python -m agents.opt_in_asset --asset-id {asset_id} "
            f"--mnemonic-env E2E_{role.upper()}_MNEMONIC`"
        )
        assert not held[asset_id].get("is-frozen"), f"{role}'s holding is frozen"

    buyer_usdc = usdc_balance(algod, buyer.address, asset_id)
    assert buyer_usdc >= price, (
        f"buyer holds {buyer_usdc} of asset {asset_id} but a seat costs {price}. "
        "TestNet USDC has no programmatic dispenser; top up at faucet.circle.com"
    )

    admin_algo = algo_balance(algod, admin.address)
    assert admin_algo >= MIN_ADMIN_MICROALGO, (
        f"admin holds {admin_algo} microALGO; each agreement parks 95,800 of it "
        "until close. Top up at https://lora.algokit.io/testnet/fund"
    )
    assert algo_balance(algod, verifier.address) > 0, (
        f"verifier {verifier.address} is unfunded and cannot pay for a release"
    )
    # The refund scenario sends expire, refund_next and close as the buyer.
    assert algo_balance(algod, buyer.address) > 0, (
        f"buyer {buyer.address} is unfunded and cannot pay for the three calls "
        "the refund scenario sends as the buyer"
    )
    # `recirculate_float` sends the treasury's USDC back on teardown.
    assert algo_balance(algod, treasury.address) > 0, (
        f"treasury {treasury.address} is unfunded; the float would not return "
        "and the session would end in a teardown error"
    )


@pytest.fixture(scope="session")
def seat_buyers(
    e2e_env: dict[str, str], algod: Any, admin: Account, buyer: Account
) -> Iterator[Any]:
    """Extra funded payers for a multi-seat pool, topped up on demand.

    `join` refuses a payer already on the roster, so a five-seat pool needs
    five keys and this suite has one. These are the other four.

    A factory rather than a value, because the float does not survive a
    scenario: `release_hash` sends every seat's USDC to the treasury, and
    `recirculate_float` does not send it back until session teardown. So each
    scenario re-seeds before it fills, which `provision_seats` makes free
    whenever the money is already there.

    Requested explicitly and never autouse: the four original scenarios must
    keep passing on a machine that has never provisioned a seat account.
    """
    from agents.seat_accounts import provision_seats, return_seats

    if not e2e_env.get("E2E_REHEARSAL"):
        pytest.skip(
            "the multi-seat rehearsal spends real settlements and waits out "
            "real deadlines. Set E2E_REHEARSAL=1 in .env to run it."
        )

    asset_id = asset_id_from(e2e_env)

    def _provision(count: int, usdc_each: int) -> list[Account]:
        needed = count * usdc_each
        held = usdc_balance(algod, buyer.address, asset_id)
        assert held >= needed, (
            f"buyer holds {held} micro-USDC but seeding {count} seats at "
            f"{usdc_each} needs {needed}. TestNet USDC has no programmatic "
            "dispenser; top up at https://faucet.circle.com"
        )
        seats = provision_seats(
            algod,
            count=count,
            asset_id=asset_id,
            usdc_each=usdc_each,
            algo_funder_key=admin.private_key,
            usdc_funder_key=buyer.private_key,
        )
        return [Account(phrase) for phrase, _address in seats]

    yield _provision

    # The seats' own float goes back the same way the treasury's does, and for
    # the same reason: TestNet USDC is the one thing here a captcha guards.
    return_seats(algod, asset_id=asset_id, usdc_to=buyer.address, close=False)


@pytest.fixture(scope="session", autouse=True)
def recirculate_float(
    e2e_env: dict[str, str], algod: Any, treasury: Account, buyer: Account
) -> Iterator[None]:
    """Send whatever the treasury received back to the buyer, afterwards.

    A released agreement pays the treasury, so without this the buyer's float
    drains by one seat price per happy-path run and the suite eventually stops
    working -- with a captcha as the only way back. Teardown, never setup: a
    scenario that asserts the treasury's balance moved has to see it move.
    """
    yield
    asset_id = asset_id_from(e2e_env)
    held = usdc_balance(algod, treasury.address, asset_id)
    if held:
        send_usdc(algod, treasury, buyer.address, asset_id, held)
