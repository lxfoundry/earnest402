"""Shared LocalNet fixtures for the escrow suite.

The escrow tests are split by lifecycle phase across five modules that all need
the same scaffolding: a USDC-like asset, a deployed and opted-in application,
and accounts in specific opt-in states. Those live here rather than being
copied five times.

Time. LocalNet is a dev-mode network: a block is produced per transaction and
its timestamp is `previous + offset`, where the offset is set through algod's
dev-mode endpoint. `advance_chain_time` sets a large offset, produces one
block, and puts the offset back, moving `Global.latest_timestamp` by an exact
number of seconds without waiting on a wall clock.
"""

import base64
import json
import os
from pathlib import Path

import algokit_utils
import pytest
from algokit_utils import (
    AlgoAmount,
    AlgorandClient,
    AssetCreateParams,
    AssetFreezeParams,
    AssetOptInParams,
    AssetOptOutParams,
    AssetTransferParams,
    CommonAppCallParams,
    PaymentParams,
    SendParams,
)
from algosdk import abi as algosdk_abi
from algosdk import encoding as algosdk_encoding
from algosdk.encoding import checksum
from algosdk.error import AlgodHTTPError

# Release conditions, mirroring the contract's discriminator.
CONDITION_HASH = 0
CONDITION_QUORUM = 1

# States. CLOSED is not stored -- `close` deletes the boxes.
STATE_OPEN = 0
STATE_FUNDED = 1
STATE_FILLED = 2
STATE_RELEASED = 3
STATE_EXPIRED = 4
STATE_REFUNDING = 5
STATE_REFUNDED = 6

SHARE_PRICE = 1_000_000
ZERO_HASH = bytes(32)
COMMIT_HASH = bytes(range(32))
ZERO_ADDRESS = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAY5HFKQ"

# Auto-discover and attach box/asset/account references via simulate rather
# than computing box names and reference arrays by hand.
POPULATE = SendParams(populate_app_call_resources=True)

# Seconds a block timestamp advances during normal test execution.
BLOCK_SECONDS = 1


def box_mbr(max_seats: int) -> int:
    """Minimum balance locked by the agreement box plus the roster box."""
    agreement = 2_500 + 400 * (9 + 164)
    roster = 2_500 + 400 * (9 + 40 * max_seats)
    return agreement + roster


def fee_reserve(max_seats: int) -> int:
    """Inner-transaction reserve: at most one payment per seat, plus one."""
    return (max_seats + 1) * 1_000


def deposit_microalgo(max_seats: int) -> int:
    """The creation deposit: both boxes' minimum balance plus the reserve."""
    return box_mbr(max_seats) + fee_reserve(max_seats)


def chain_now(algorand: AlgorandClient) -> int:
    """The timestamp the contract will read as `Global.latest_timestamp`."""
    last_round = algorand.client.algod.status()["last-round"]
    return int(algorand.client.algod.block_info(last_round)["block"]["ts"])


def usdc_balance(algorand: AlgorandClient, address: str, asset_id: int) -> int:
    """Balance in asset units, or 0 when the account is not opted in."""
    try:
        return algorand.asset.get_account_information(address, asset_id).balance
    except AlgodHTTPError:
        return 0


def algo_balance(algorand: AlgorandClient, address: str) -> int:
    return algorand.account.get_information(address).amount.micro_algo


def agreement_box_name(agreement_id: int) -> bytes:
    return b"a" + agreement_id.to_bytes(8, "big")


def roster_box_name(agreement_id: int) -> bytes:
    return b"r" + agreement_id.to_bytes(8, "big")


def roster_seats(
    algorand: AlgorandClient, app_client, agreement_id: int
) -> list[tuple[str, int]]:
    """The roster as (payer, amount) pairs, one per pre-sized seat.

    Read by slice, exactly as the contract does: 32 address bytes then an
    8-byte amount. An amount of zero on an occupied seat is the paid marker.
    """
    box = algorand.client.algod.application_box_by_name(
        app_client.app_id, roster_box_name(agreement_id)
    )
    raw = base64.b64decode(box["value"])
    return [
        (
            algosdk_encoding.encode_address(raw[offset : offset + 32]),
            int.from_bytes(raw[offset + 32 : offset + 40], "big"),
        )
        for offset in range(0, len(raw), 40)
    ]


# --- ARC-28 events --------------------------------------------------------
#
# Boxes are working state, deleted at `close`; the events are the permanent
# record, so the suite reads them rather than trusting the box alone. An event
# is logged as a 4-byte selector -- the sha512/256 prefix of its signature --
# followed by the ARC-4 encoding of its arguments.

_ARC56 = (
    Path(__file__).resolve().parent.parent
    / "smart_contracts"
    / "artifacts"
    / "escrow"
    / "Escrow.arc56.json"
)


def _event_catalogue() -> dict[bytes, tuple[str, list[str], object]]:
    spec = json.loads(_ARC56.read_text(encoding="utf-8"))
    catalogue: dict[bytes, tuple[str, list[str], object]] = {}
    for method in spec["methods"]:
        for event in method.get("events", []):
            types = ",".join(arg["type"] for arg in event["args"])
            selector = checksum(f"{event['name']}({types})".encode())[:4]
            catalogue[selector] = (
                event["name"],
                [arg["name"] for arg in event["args"]],
                algosdk_abi.ABIType.from_string(f"({types})"),
            )
    return catalogue


def decoded_events(result) -> list[tuple[str, dict]]:
    """Every ARC-28 event a send result carries, in order."""
    catalogue = _event_catalogue()
    confirmations = getattr(result, "confirmations", None)
    if confirmations is None:
        confirmation = getattr(result, "confirmation", None)
        confirmations = [confirmation] if confirmation else []

    events: list[tuple[str, dict]] = []
    for confirmation in confirmations:
        for raw in confirmation.get("logs") or []:
            log = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
            entry = catalogue.get(log[:4])
            if entry is None:
                continue
            name, arg_names, codec = entry
            events.append((name, dict(zip(arg_names, codec.decode(log[4:])))))
    return events


def events_named(result, name: str) -> list[dict]:
    return [payload for event, payload in decoded_events(result) if event == name]


def one_event(result, name: str) -> dict:
    """The single event of that name, asserting there is exactly one."""
    matches = events_named(result, name)
    assert len(matches) == 1, f"expected one {name}, got {len(matches)}"
    return matches[0]


# --- calling the contract -------------------------------------------------


def call_release_hash(app_client, agreement_id: int, sender: str, delivered=None):
    from smart_contracts.artifacts.escrow.escrow_client import ReleaseHashArgs

    return app_client.send.release_hash(
        args=ReleaseHashArgs(
            agreement_id=agreement_id,
            delivered_bytes_hash=COMMIT_HASH if delivered is None else delivered,
        ),
        params=CommonAppCallParams(sender=sender, extra_fee=AlgoAmount(micro_algo=0)),
        send_params=POPULATE,
    )


def call_release_quorum(app_client, agreement_id: int, sender: str | None = None):
    from smart_contracts.artifacts.escrow.escrow_client import ReleaseQuorumArgs

    return app_client.send.release_quorum(
        args=ReleaseQuorumArgs(agreement_id=agreement_id),
        params=CommonAppCallParams(sender=sender) if sender else None,
        send_params=POPULATE,
    )


def call_expire(app_client, agreement_id: int, sender: str | None = None):
    from smart_contracts.artifacts.escrow.escrow_client import ExpireArgs

    return app_client.send.expire(
        args=ExpireArgs(agreement_id=agreement_id),
        params=CommonAppCallParams(sender=sender) if sender else None,
        send_params=POPULATE,
    )


def call_refund_next(
    algorand: AlgorandClient,
    app_client,
    usdc_asset_id: int,
    agreement_id: int,
    count: int,
    sender: str | None = None,
):
    """Call `refund_next` with the reference arrays built by hand.

    Auto-population cannot express this call: it re-appends the asset for every
    asset-holding lookup, so four seats would cost four accounts *plus* four
    asset entries and overflow the eight-reference limit at three seats. The
    budget the design actually needs is 4 accounts + 1 asset + 2 boxes = 7,
    which is what a backend paging refunds would send.
    """
    from smart_contracts.artifacts.escrow.escrow_client import RefundNextArgs

    record = app_client.state.box.agreements.get_value(agreement_id)
    window = roster_seats(algorand, app_client, agreement_id)[
        record.refund_cursor : min(record.refund_cursor + count, record.seats)
    ]
    return app_client.send.refund_next(
        args=RefundNextArgs(agreement_id=agreement_id, count=count),
        params=CommonAppCallParams(
            sender=sender,
            account_references=[payer for payer, amount in window if amount > 0],
            asset_references=[usdc_asset_id],
            box_references=[
                agreement_box_name(agreement_id),
                roster_box_name(agreement_id),
            ],
        ),
    )


def call_claim_refund(app_client, agreement_id: int, seat: int, sender=None):
    from smart_contracts.artifacts.escrow.escrow_client import ClaimRefundArgs

    return app_client.send.claim_refund(
        args=ClaimRefundArgs(agreement_id=agreement_id, seat=seat),
        params=CommonAppCallParams(sender=sender) if sender else None,
        send_params=POPULATE,
    )


def call_close(app_client, agreement_id: int, sender: str | None = None):
    from smart_contracts.artifacts.escrow.escrow_client import CloseArgs

    return app_client.send.close(
        args=CloseArgs(agreement_id=agreement_id),
        params=CommonAppCallParams(sender=sender) if sender else None,
        send_params=POPULATE,
    )


def box_exists(algorand: AlgorandClient, app_client, name: bytes) -> bool:
    try:
        algorand.client.algod.application_box_by_name(app_client.app_id, name)
    except AlgodHTTPError:
        return False
    return True


def _fund(
    algorand: AlgorandClient,
    account: algokit_utils.SigningAccount,
    algo: int = 10,
) -> None:
    algorand.account.ensure_funded(
        account.address, algorand.account.localnet_dispenser(), AlgoAmount(algo=algo)
    )


@pytest.fixture(scope="session")
def algorand() -> AlgorandClient:
    client = AlgorandClient.default_localnet()
    # Deterministic, small block-time steps for the whole session.
    client.client.algod.set_timestamp_offset(BLOCK_SECONDS)
    # Never reuse cached suggested params. LocalNet advances a round per
    # transaction, so two identical calls sharing one validity window would
    # produce the same transaction id and the second would be rejected as
    # already in the ledger.
    client.set_suggested_params_cache_timeout(0)
    return client


@pytest.fixture(scope="session")
def dispenser(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    return algorand.account.localnet_dispenser()


@pytest.fixture(scope="session")
def usdc_creator(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    account = algorand.account.random()
    _fund(algorand, account, algo=100)
    return account


@pytest.fixture(scope="session")
def usdc_asset_id(
    algorand: AlgorandClient, usdc_creator: algokit_utils.SigningAccount
) -> int:
    """A 6-decimal asset standing in for USDC.

    `freeze` and `manager` are set because MainNet USDC sets them: Circle
    retains the freeze authority. An account can therefore be opted in and
    still unable to receive, which is a distinct failure from having opted
    out and is what `_can_receive` in the contract has to tell apart.
    `default_frozen` stays off, so nothing is frozen unless a test asks.
    """
    result = algorand.send.asset_create(
        AssetCreateParams(
            sender=usdc_creator.address,
            total=10_000_000_000_000,
            decimals=6,
            asset_name="Test USDC",
            unit_name="tUSDC",
            manager=usdc_creator.address,
            freeze=usdc_creator.address,
            default_frozen=False,
        )
    )
    return result.asset_id


@pytest.fixture(scope="module")
def creator(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    """Admin and deployer. Fresh per module, so each module gets its own app."""
    account = algorand.account.random()
    _fund(algorand, account, algo=100)
    return account


@pytest.fixture(scope="module")
def treasury(
    algorand: AlgorandClient,
    usdc_creator: algokit_utils.SigningAccount,
    usdc_asset_id: int,
) -> algokit_utils.SigningAccount:
    account = algorand.account.random()
    _fund(algorand, account)
    algorand.send.asset_opt_in(
        AssetOptInParams(sender=account.address, asset_id=usdc_asset_id)
    )
    return account


@pytest.fixture(scope="module")
def app_client(
    algorand: AlgorandClient,
    creator: algokit_utils.SigningAccount,
    treasury: algokit_utils.SigningAccount,
    usdc_asset_id: int,
):
    from smart_contracts.artifacts.escrow.escrow_client import (
        BootstrapArgs,
        EscrowFactory,
        EscrowMethodCallCreateParams,
        OptInAssetArgs,
    )

    factory = algorand.client.get_typed_app_factory(
        EscrowFactory, default_sender=creator.address
    )
    client, _ = factory.deploy(
        on_update=algokit_utils.OnUpdate.AppendApp,
        on_schema_break=algokit_utils.OnSchemaBreak.AppendApp,
        create_params=EscrowMethodCallCreateParams(
            args=BootstrapArgs(usdc_asset_id=usdc_asset_id, treasury=treasury.address),
        ),
    )

    # Application-level minimum balance: 0.1 base plus 0.1 for the USDC
    # opt-in. Per-agreement deposits arrive with each `create_agreement`.
    algorand.send.payment(
        PaymentParams(
            sender=creator.address,
            receiver=client.app_address,
            amount=AlgoAmount(algo=1),
        )
    )
    # opt_in_asset issues one inner transaction; cover its pooled fee.
    client.send.opt_in_asset(
        args=OptInAssetArgs(asset_id=usdc_asset_id),
        params=CommonAppCallParams(extra_fee=AlgoAmount(micro_algo=1_000)),
    )
    return client


@pytest.fixture
def create_agreement(
    algorand: AlgorandClient,
    app_client,
    creator: algokit_utils.SigningAccount,
):
    """Send one `create_agreement`, defaulting every argument to a valid row.

    Returns the send result so callers can read the new agreement id. Every
    argument is overridable so the validity table can be walked one rejected
    row at a time.
    """
    from smart_contracts.artifacts.escrow.escrow_client import CreateAgreementArgs

    def _create(
        *,
        beneficiary: str,
        condition: int = CONDITION_HASH,
        share_price: int = SHARE_PRICE,
        min_seats: int = 1,
        max_seats: int = 1,
        deadline: int | None = None,
        commit_hash: bytes | None = None,
        verifier: str = ZERO_ADDRESS,
        deposit: int | None = None,
        sender: str | None = None,
        send_params: SendParams | None = POPULATE,
    ):
        sender = sender or creator.address
        if deadline is None:
            deadline = chain_now(algorand) + 3_600
        if commit_hash is None:
            commit_hash = COMMIT_HASH if condition == CONDITION_HASH else ZERO_HASH
        if deposit is None:
            deposit = deposit_microalgo(max_seats)

        payment = algorand.create_transaction.payment(
            PaymentParams(
                sender=sender,
                receiver=app_client.app_address,
                amount=AlgoAmount(micro_algo=deposit),
            )
        )
        return app_client.send.create_agreement(
            args=CreateAgreementArgs(
                condition=condition,
                share_price=share_price,
                min_seats=min_seats,
                max_seats=max_seats,
                deadline=deadline,
                commit_hash=commit_hash,
                beneficiary=beneficiary,
                verifier=verifier,
                mbr_payment=payment,
            ),
            params=CommonAppCallParams(sender=sender),
            send_params=send_params,
        )

    return _create


@pytest.fixture
def hash_agreement(
    create_agreement,
    beneficiary: algokit_utils.SigningAccount,
    verifier: algokit_utils.SigningAccount,
) -> int:
    """An OPEN single-seat `hash` agreement, one hour from its deadline."""
    result = create_agreement(
        beneficiary=beneficiary.address, verifier=verifier.address
    )
    return result.abi_return


@pytest.fixture
def join_agreement(algorand: AlgorandClient, app_client, usdc_asset_id: int):
    """Send one settlement group: [feePayer, axfer -> app account, join]."""
    from smart_contracts.artifacts.escrow.escrow_client import JoinArgs

    def _join(agreement_id: int, payer: algokit_utils.SigningAccount) -> None:
        share_price = app_client.state.box.agreements.get_value(
            agreement_id
        ).share_price
        (
            algorand.new_group()
            .add_payment(
                PaymentParams(
                    sender=payer.address,
                    receiver=payer.address,
                    amount=AlgoAmount(micro_algo=0),
                )
            )
            .add_asset_transfer(
                AssetTransferParams(
                    sender=payer.address,
                    receiver=app_client.app_address,
                    asset_id=usdc_asset_id,
                    amount=share_price,
                )
            )
            .add_app_call_method_call(
                app_client.params.join(
                    args=JoinArgs(agreement_id=agreement_id, payment_index=1),
                    params=CommonAppCallParams(sender=payer.address),
                )
            )
        ).send(POPULATE)

    return _join


@pytest.fixture
def funded_agreement(hash_agreement: int, buyer, join_agreement) -> int:
    """A FUNDED single-seat `hash` agreement holding one share."""
    join_agreement(hash_agreement, buyer)
    return hash_agreement


@pytest.fixture
def make_pool(create_agreement, beneficiary, join_agreement, make_buyer):
    """Factory for a pool with `joins` payers already seated."""

    def _make(
        *,
        min_seats: int = 3,
        max_seats: int | None = None,
        joins: int | None = None,
        deadline: int | None = None,
    ) -> tuple[int, list]:
        max_seats = min_seats if max_seats is None else max_seats
        joins = min_seats if joins is None else joins
        agreement_id = create_agreement(
            beneficiary=beneficiary.address,
            condition=CONDITION_QUORUM,
            min_seats=min_seats,
            max_seats=max_seats,
            deadline=deadline,
        ).abi_return
        payers = []
        for _ in range(joins):
            payer = make_buyer()
            join_agreement(agreement_id, payer)
            payers.append(payer)
        return agreement_id, payers

    return _make


@pytest.fixture
def make_hash_pool(create_agreement, beneficiary, verifier, join_agreement, make_buyer):
    """Factory for a multi-seat `hash` pool with `joins` payers seated.

    Mode 3: the deliverable's hash is committed at creation, so this fixture
    can only exist in the produce-first order the contract enforces. There is
    no way to bind a hash later, which is exactly the property that makes the
    mode worth having.
    """

    def _make(
        *,
        min_seats: int = 5,
        joins: int | None = None,
        deadline: int | None = None,
    ) -> tuple[int, list]:
        joins = min_seats if joins is None else joins
        agreement_id = create_agreement(
            beneficiary=beneficiary.address,
            verifier=verifier.address,
            condition=CONDITION_HASH,
            min_seats=min_seats,
            max_seats=min_seats,
            deadline=deadline,
        ).abi_return
        payers = []
        for _ in range(joins):
            payer = make_buyer()
            join_agreement(agreement_id, payer)
            payers.append(payer)
        return agreement_id, payers

    return _make


@pytest.fixture
def filled_pool(make_pool) -> tuple[int, list]:
    """A FILLED pool: three payers, three seats."""
    return make_pool(min_seats=3)


@pytest.fixture
def make_expired_pool(algorand, app_client, make_pool, advance_chain_time):
    """A pool that missed its quota, past its deadline and marked EXPIRED.

    `joins` payers seated out of a `min_seats` quota that is deliberately out
    of reach, so the pool is still OPEN when the clock runs out -- the ordinary
    way a pool reaches the refund path.
    """
    from smart_contracts.artifacts.escrow.escrow_client import ExpireArgs

    def _make(*, joins: int = 3, deadline_in: int = 60) -> tuple[int, list]:
        # Every block advances the clock a second, and seating one payer costs
        # several, so a large pool needs a deadline that outlives its own fill.
        deadline_in = max(deadline_in, 10 * joins)
        agreement_id, payers = make_pool(
            min_seats=joins + 1,
            joins=joins,
            deadline=chain_now(algorand) + deadline_in,
        )
        advance_chain_time(deadline_in + 240)
        app_client.send.expire(
            args=ExpireArgs(agreement_id=agreement_id),
            send_params=POPULATE,
        )
        return agreement_id, payers

    return _make


@pytest.fixture
def make_expired_hash_pool(algorand, app_client, make_hash_pool, advance_chain_time):
    """A multi-seat `hash` pool past its deadline and marked EXPIRED.

    `joins` payers seated out of a `min_seats` quota, then the clock run past
    the deadline. Two shapes reach the refund path and both matter: a pool
    that never filled, and one that filled but was never released -- the
    second is non-delivery, which is the failure this mode exists to price.
    A filled `hash` pool is FUNDED, and `expire` accepts FUNDED, so unlike a
    `quorum` pool it can still reach the refund path after reaching quota.
    """
    from smart_contracts.artifacts.escrow.escrow_client import ExpireArgs

    def _make(
        *, min_seats: int = 5, joins: int = 3, deadline_in: int = 60
    ) -> tuple[int, list]:
        # Every block advances the clock a second, and seating one payer costs
        # several, so a large pool needs a deadline that outlives its own fill.
        deadline_in = max(deadline_in, 10 * joins)
        agreement_id, payers = make_hash_pool(
            min_seats=min_seats,
            joins=joins,
            deadline=chain_now(algorand) + deadline_in,
        )
        advance_chain_time(deadline_in + 240)
        app_client.send.expire(
            args=ExpireArgs(agreement_id=agreement_id),
            send_params=POPULATE,
        )
        return agreement_id, payers

    return _make


@pytest.fixture
def expired_pool_of_three(make_expired_pool) -> tuple[int, list]:
    return make_expired_pool(joins=3)


def freeze_usdc(
    algorand, account, usdc_asset_id: int, usdc_creator, frozen: bool = True
) -> None:
    """Freeze an account's USDC holding, leaving it opted in.

    The other half of "cannot receive": the holding exists, so an opt-in check
    passes, but any transfer into it fails. Only the asset's freeze address
    can do this, which is why the stand-in asset declares one.
    """
    algorand.send.asset_freeze(
        AssetFreezeParams(
            sender=usdc_creator.address,
            asset_id=usdc_asset_id,
            account=account.address,
            frozen=frozen,
        )
    )


def opt_out_of_usdc(algorand, account, usdc_asset_id: int, usdc_creator) -> None:
    """Make an account unable to receive USDC, closing any balance out.

    `ensure_zero_balance` is off because a payer mid-refund still holds the
    change from their other joins; closing the holding sends it to the asset
    creator, which is exactly what a wallet abandoning an asset does.
    """
    algorand.send.asset_opt_out(
        AssetOptOutParams(
            sender=account.address,
            asset_id=usdc_asset_id,
            creator=usdc_creator.address,
        ),
        ensure_zero_balance=False,
    )


@pytest.fixture
def advance_chain_time(
    algorand: AlgorandClient, dispenser: algokit_utils.SigningAccount
):
    """Move the chain clock forward by an exact number of seconds."""

    def _advance(seconds: int) -> int:
        algod = algorand.client.algod
        algod.set_timestamp_offset(int(seconds))
        algorand.send.payment(
            PaymentParams(
                sender=dispenser.address,
                receiver=dispenser.address,
                amount=AlgoAmount(micro_algo=0),
                note=os.urandom(8),
            )
        )
        algod.set_timestamp_offset(BLOCK_SECONDS)
        return chain_now(algorand)

    return _advance


@pytest.fixture
def future_deadline(algorand: AlgorandClient):
    """A deadline `seconds` ahead of the timestamp the contract will read."""

    def _deadline(seconds: int = 3_600) -> int:
        return chain_now(algorand) + seconds

    return _deadline


@pytest.fixture
def opted_in_account(algorand: AlgorandClient, usdc_asset_id: int):
    """Factory for a funded account opted in to the test USDC asset."""

    def _make() -> algokit_utils.SigningAccount:
        account = algorand.account.random()
        _fund(algorand, account)
        algorand.send.asset_opt_in(
            AssetOptInParams(sender=account.address, asset_id=usdc_asset_id)
        )
        return account

    return _make


@pytest.fixture
def beneficiary(opted_in_account) -> algokit_utils.SigningAccount:
    """A beneficiary opted in to USDC, as `create_agreement` requires."""
    return opted_in_account()


@pytest.fixture
def verifier(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    """The release authority for a `hash` agreement. Holds no USDC."""
    account = algorand.account.random()
    _fund(algorand, account)
    return account


@pytest.fixture
def stranger(algorand: AlgorandClient) -> algokit_utils.SigningAccount:
    """Someone with no stake in any agreement, to prove the open paths are open.

    Holds ALGO for fees and nothing else: no USDC opt-in, no seat, no role.
    Two things need that -- a caller proving a permissionless path is open to
    anyone, and an address the contract must refuse because it cannot receive
    USDC -- and both want an account no agreement already knows.
    """
    account = algorand.account.random()
    _fund(algorand, account, algo=5)
    return account


@pytest.fixture
def make_buyer(
    algorand: AlgorandClient,
    usdc_creator: algokit_utils.SigningAccount,
    usdc_asset_id: int,
):
    """Factory for a funded buyer holding enough USDC to join several times."""

    def _make(usdc: int = SHARE_PRICE * 10) -> algokit_utils.SigningAccount:
        account = algorand.account.random()
        _fund(algorand, account)
        algorand.send.asset_opt_in(
            AssetOptInParams(sender=account.address, asset_id=usdc_asset_id)
        )
        algorand.send.asset_transfer(
            AssetTransferParams(
                sender=usdc_creator.address,
                receiver=account.address,
                asset_id=usdc_asset_id,
                amount=usdc,
            )
        )
        return account

    return _make


@pytest.fixture
def buyer(make_buyer) -> algokit_utils.SigningAccount:
    return make_buyer()
