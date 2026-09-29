"""Plumbing the scenarios and the fixtures both need.

Separate from `conftest.py` on purpose: a test that imports from a conftest
makes Python load that file a second time, under a second name, and two copies
of a module that defines fixtures is a debugging session nobody wants. So the
conftest holds fixtures and nothing else, and everything shared lives here.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from algosdk import account as algo_account
from algosdk import encoding, mnemonic, transaction
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from algosdk.error import AlgodHTTPError, AtomicTransactionComposerError

from api.config import Settings, load_settings

if TYPE_CHECKING:  # imported lazily in `avm_signer`; see its docstring
    from agents.common import AlgorandSigner

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ENV_PATH = os.path.join(REPO_ROOT, ".env")

DEFAULT_ALGOD_URL = "https://testnet-api.algonode.cloud"
DEFAULT_USDC_ASA_ID = "10458941"

# What an unoccupied roster seat decodes to. The roster box is pre-sized to
# `max_seats` at creation, so `read_roster` returns `max_seats` tuples however
# many seats are taken -- a scenario that compares the whole list against its
# payers is comparing against the tail as well, and the tail is this.
ZERO_ADDRESS = encoding.encode_address(bytes(32))

# One agreement's deposit is 95,800 microALGO at the current minimum fee, and a
# scenario may hold two open at once. Half an ALGO leaves room for both plus
# every call the suite sends.
MIN_ADMIN_MICROALGO = 500_000

# What a contract rejection looks like from here. The node refuses the
# submission, so it surfaces as an HTTP error from algod -- or, when the
# composer catches that first, wrapped in its own. A test that asserts a
# guard holds has to name both, or it passes on the wrong failure.
REJECTED = (AlgodHTTPError, AtomicTransactionComposerError)


class Env(dict):
    """The .env values, with a repr that does not print them.

    pytest renders the local variables of every frame in a traceback, and
    a fixture that takes this mapping would otherwise dump every mnemonic
    in it on the first unrelated failure. The bootstrap script goes to some
    trouble to keep key material out of a terminal; this keeps it out of
    the one place that would put it back.
    """

    def __repr__(self) -> str:
        return f"<env: {len(self)} keys, redacted>"


class Account:
    """One role: its address, and both signer shapes it may be asked for."""

    def __init__(self, phrase: str) -> None:
        # Hides this frame, and therefore `phrase`, from pytest tracebacks.
        __tracebackhide__ = True
        self.private_key = mnemonic.to_private_key(phrase)
        self.address = algo_account.address_from_private_key(self.private_key)

    def __repr__(self) -> str:
        return f"Account({self.address})"

    @property
    def atc_signer(self) -> AccountTransactionSigner:
        """For `EscrowClient`, which composes through the ATC."""
        return AccountTransactionSigner(self.private_key)

    @property
    def avm_signer(self) -> AlgorandSigner:
        """For `build_join_group`, which speaks the x402 client convention.

        Imported here rather than at module scope: `agents.common` calls
        `load_dotenv()` on import, and pytest imports this module's conftest
        during collection whatever `-m` filter is in force -- so a module-scope
        import puts .env into the environment of every offline run.

        This closes one path, not the hole. `tests/e2e/buyer.py` and the
        pre-existing `tests/test_build_join_group.py` both still import
        `agents.common` at module scope, so tests/conftest.py's header claim
        ("nothing here touches the process environment") is already false on
        main. Closing it properly means moving `load_dotenv()` out of
        `agents/common.py` import time, which is its own change.
        """
        __tracebackhide__ = True
        from agents.common import AlgorandSigner

        secret_key = base64.b64decode(self.private_key)
        return AlgorandSigner(secret_key, encoding.encode_address(secret_key[32:]))


def algod_url_from(env: dict[str, str]) -> str:
    return env.get("ALGOD_URL") or env.get("ALGOD_TESTNET_URL") or DEFAULT_ALGOD_URL


def asset_id_from(env: dict[str, str]) -> int:
    return int(env.get("USDC_ASA_ID") or DEFAULT_USDC_ASA_ID)


# --- balances -------------------------------------------------------------


def algo_balance(algod: Any, address: str) -> int:
    return int(algod.account_info(address).get("amount", 0))


def usdc_balance(algod: Any, address: str, asset_id: int) -> int:
    for holding in algod.account_info(address).get("assets", []):
        if holding["asset-id"] == asset_id:
            return int(holding["amount"])
    return 0


def send_usdc(
    algod: Any, sender: Account, receiver: str, asset_id: int, amount: int
) -> str:
    """A plain transfer, used only to recirculate the float after a release."""
    txn = transaction.AssetTransferTxn(
        sender=sender.address,
        sp=algod.suggested_params(),
        receiver=receiver,
        amt=amount,
        index=asset_id,
    )
    txid = algod.send_transaction(txn.sign(sender.private_key))
    transaction.wait_for_confirmation(algod, txid, 4)
    return txid


# --- waiting --------------------------------------------------------------


def latest_timestamp(algod: Any) -> int:
    """The timestamp the contract will see.

    `Global.latest_timestamp` is the *previous* block's, which biases against
    whoever is waiting. Reading the same value the contract reads is what stops
    a deadline scenario passing because the wall clock was optimistic about
    which block its call would land in.
    """
    return int(algod.block_info(algod.status()["last-round"])["block"]["ts"])


def wait_until(
    predicate: Callable[[], Any],
    *,
    timeout: float,
    interval: float = 3.0,
    what: str = "",
) -> Any:
    """Poll until `predicate()` returns something truthy, or fail saying what.

    Evaluated once before any clock check, so a caller that computes its
    timeout from a budget -- and arrives with none left -- still gets the
    answer rather than an assertion about time it never had.
    """
    expiry = time.monotonic() + timeout
    while True:
        result = predicate()
        if result:
            return result
        if time.monotonic() >= expiry:
            raise AssertionError(
                f"timed out after {timeout}s waiting for {what or predicate!r}"
            )
        time.sleep(interval)


def wait_past_deadline(algod: Any, deadline: int, *, slack: int = 5) -> int:
    """Block until the chain's own clock is past `deadline`.

    There is no timestamp offset on TestNet, so this is real elapsed time --
    which is why the suite runs with a short DELIVERY_DEADLINE_SECONDS.
    """
    return wait_until(
        lambda: ts if (ts := latest_timestamp(algod)) >= deadline + slack else None,
        timeout=deadline - latest_timestamp(algod) + 180,
        what=f"the chain clock to pass {deadline}",
    )


# --- settings -------------------------------------------------------------


def build_settings(env: dict[str, str], base_url: str, job_db_path: str) -> Settings:
    """Real `load_settings`, fed a mapping instead of the process environment.

    Going through the loader rather than constructing `Settings` by hand keeps
    the suite honest about defaults: a field added to the loader arrives here
    with its production default rather than silently missing.

    PUBLIC_HOST and RESOURCE_URL are the loopback address the server is about
    to answer on. That is the honest value, and the facilitator never
    catalogues a loopback resource -- so however often this runs it creates no
    directory row and attaches to no merchant identity.

    The merged mapping is re-wrapped in `Env` rather than left a plain dict:
    `{**env, ...}` copies the values out of the redacted mapping into one that
    prints them, and it is that copy which becomes `load_settings`'s local --
    so a ConfigError raised in there would render every mnemonic in `.env`.
    """
    return load_settings(
        Env(
            {
                **env,
                "ALGOD_URL": algod_url_from(env),
                "PUBLIC_HOST": base_url,
                # The circuit exercises `/pin`, which the loader leaves off by
                # default. TestNet only: the boot guard refuses it on MainNet.
                "PIN_ROUTE_ENABLED": "true",
                "RESOURCE_URL": f"{base_url}/pin",
                "PRICE_MICRO_USDC": env.get("PRICE_MICRO_USDC") or "100000",
                "DELIVERY_DEADLINE_SECONDS": env.get("E2E_DEADLINE_SECONDS") or "120",
                "JOB_DB_PATH": job_db_path,
            }
        )
    )


# --- cleanup --------------------------------------------------------------


def reclaim(client: Any, agreement_id: int) -> None:
    """Walk an unfunded agreement to deletion and give the deposit back.

    Every 402 parks a deposit, so a scenario that quotes without paying has
    borrowed the working capital the next quote needs. Leaving it behind would
    make the suite degrade the account it runs against -- slowly, and only
    visibly once quoting started failing for want of ALGO.

    Not a fixture: which agreements a scenario created is the scenario's own
    business, and a blanket sweep would hide a close that should have worked.
    """
    if client.read_agreement(agreement_id) is None:
        return
    client.expire(agreement_id)
    client.refund_next(agreement_id, 1)
    client.close(agreement_id)
