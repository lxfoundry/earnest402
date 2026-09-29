"""Which key the manual join client pays with.

A multi-seat pool takes one key per seat, because `join` refuses a payer
already on the roster, and the client used to know only the buyer's. These pin
that `--mnemonic-env` selects the named key, that leaving it off keeps
AVM_PRIVATE_KEY, and that a missing variable stops the run before any request
goes out. Offline throughout: every account is generated here and discarded.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Self

import dotenv
import pytest
from algosdk import account, mnemonic

from agents import join_client_manual
from agents.seat_accounts import seat_env_key

# A name no .env defines, for the paths that must find nothing.
UNSET = "JOIN_CLIENT_TEST_UNSET_MNEMONIC"


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """Keep .env out of this process.

    `main` calls load_dotenv() itself, and both functions under test import
    `agents.common`, which calls it on first import. Replacing it on the
    `dotenv` module means that first import, if it happens here, binds the
    no-op -- so running this file alone reads no developer's .env, and what
    each test sets is all the environment it sees.
    """
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        join_client_manual, "load_dotenv", lambda *args, **kwargs: False
    )


@pytest.fixture
def buyer(monkeypatch) -> str:
    """A buyer key in AVM_PRIVATE_KEY, returning its address.

    algosdk's private key is already the form that variable takes: base64 of
    the 32-byte seed followed by the 32-byte public key.
    """
    private_key, address = account.generate_account()
    monkeypatch.setenv("AVM_PRIVATE_KEY", private_key)
    return address


def test_a_named_variable_selects_that_key_over_the_buyer(monkeypatch, buyer):
    private_key, seat = account.generate_account()
    monkeypatch.setenv(seat_env_key(1), mnemonic.from_private_key(private_key))

    signer = join_client_manual.choose_buyer_signer(seat_env_key(1))

    assert signer.address == seat
    assert signer.address != buyer


def test_no_variable_keeps_the_buyer_key(buyer):
    assert join_client_manual.choose_buyer_signer(None).address == buyer


def test_a_missing_variable_exits_naming_it(monkeypatch, buyer):
    monkeypatch.delenv(UNSET, raising=False)
    with pytest.raises(SystemExit, match=UNSET):
        join_client_manual.choose_buyer_signer(UNSET)


def test_an_empty_name_does_not_fall_back_to_the_buyer(buyer):
    """The operator asked for a key other than the buyer's; paying with the
    buyer's anyway is the one wrong outcome that would not be noticed."""
    with pytest.raises(SystemExit):
        join_client_manual.choose_buyer_signer("")


def _http_recording(events: list) -> SimpleNamespace:
    """Stands in for the `httpx` module, recording each request. Every answer
    is a 200 rather than a 402, so `main` stops after the first request."""

    class Client:
        def __init__(self, **kwargs) -> None:
            pass

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *exc) -> bool:
            return False

        def post(self, url: str, **kwargs) -> SimpleNamespace:
            events.append(("post", url))
            # Just enough of an httpx.Response for `dump_response`.
            return SimpleNamespace(status_code=200, headers={}, json=dict)

    return SimpleNamespace(Client=Client)


@pytest.fixture
def resource(monkeypatch) -> str:
    monkeypatch.setenv("RESOURCE_HOST", "http://resource.test")
    monkeypatch.setenv("APP_ID", "1")
    return "http://resource.test/escrow/join-spike"


@pytest.mark.parametrize(
    ("argv", "name"),
    [([], None), (["--mnemonic-env", "E2E_SEAT_2_MNEMONIC"], "E2E_SEAT_2_MNEMONIC")],
)
def test_main_chooses_the_key_before_the_first_request(
    monkeypatch, resource, argv, name
):
    events: list = []
    monkeypatch.setattr(
        join_client_manual,
        "choose_buyer_signer",
        lambda mnemonic_env: events.append(("signer", mnemonic_env)),
    )
    monkeypatch.setattr(join_client_manual, "httpx", _http_recording(events))

    join_client_manual.main(argv)

    assert events == [("signer", name), ("post", resource)]


def test_main_sends_nothing_when_the_named_variable_is_missing(
    monkeypatch, resource, buyer
):
    monkeypatch.delenv(UNSET, raising=False)
    events: list = []
    monkeypatch.setattr(join_client_manual, "httpx", _http_recording(events))

    with pytest.raises(SystemExit, match=UNSET):
        join_client_manual.main(["--mnemonic-env", UNSET])

    assert events == []
