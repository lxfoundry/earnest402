"""Plumbing the multi-seat scenarios share.

`tests/e2e/test_index_pool_seat.py` proved the shape with the smallest pool
that is still a pool: two seats, one bought. Everything here exists because a
*full* pool is a different exercise. Five seats mean five distinct payers --
`join` refuses a payer already on the roster -- five sequential settlements
inside one deadline, and a refund pass that does not fit in a single call.

Three things live here rather than in a scenario file because more than one
scenario needs them: the server pinned to a specific pool, the fill loop, and
the arithmetic `close` is asserted against.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx
from algosdk import encoding, transaction
from algosdk.v2client.models import (
    SimulateRequest,
    SimulateRequestTransactionGroup,
)

from agents.build_join_group import build_join_group
from api.app import create_app
from api.config import CHALLENGE_TAG
from api.deps import Deps
from api.escrow import EscrowClient, box_mbr, fee_reserve
from api.jobs import JobStore
from api.routes import build_routes
from tests.e2e import buyer as buyer_client
from tests.e2e.events import events_for, one_event, txid_str
from tests.e2e.harness import Account, Env, build_settings, latest_timestamp, wait_until
from tests.e2e.serve import serve

# Five seats is the point of the rehearsal: it is the first size at which the
# fee reserve is spent by more than one refund, and the first at which the
# refund pass needs two calls.
POOL_SEATS = 5

# Not the shipped INDEX_SHARE_PRICE_MICRO of 5_000_000. A full pool at that
# price is 25 USDC and the TestNet buyer holds about 20, with no programmatic
# dispenser to top it up. Nothing the rehearsal proves depends on the number:
# the fee-reserve arithmetic and the opcode budget are both functions of seat
# count alone.
POOL_SHARE_PRICE = 100_000

# Long enough for five sequential settlements through the real facilitator and
# still short enough to wait out twice in one session. The deadline is fixed
# at create and bounds *filling* as well as delivering, so the whole fill has
# to happen inside it -- the suite's own 120s E2E_DEADLINE_SECONDS is sized
# for one settlement and is not enough for five.
FILL_DEADLINE_SECONDS = 420

# Refuse to start a join that cannot plausibly land before the deadline, so a
# pool that ran out of clock says so instead of surfacing as the contract
# rejecting a join that was valid when it was built.
JOIN_HEADROOM_SECONDS = 45


def close_return(max_seats: int, min_fee: int, *, inner_used: int) -> int:
    """What `close` returns to the creator.

    The box minimum balance plus whatever is left of the fee reserve.
    `inner_used` counts every inner transaction the agreement paid for -- one
    per release or refund, plus `close`'s own payment. Spelled as one formula
    so the three figures the scenarios assert are one rule read three ways,
    and so a protocol minimum-fee change moves all three together instead of
    failing as three unrelated off-by-a-thousand assertions.
    """
    return box_mbr(max_seats) + fee_reserve(max_seats, min_fee) - inner_used * min_fee


def inner_transfers(algod: Any, txid: str) -> list[tuple[str, int]]:
    """`(receiver, amount)` for every inner asset transfer one call made.

    A balance delta says money arrived; this says how it arrived.
    `release_hash` pays `total_held` in exactly one transfer however many
    seats funded it, and that is a property of the contract rather than an
    accident of the arithmetic -- so it is worth asserting directly.
    """
    info = algod.pending_transaction_info(txid)
    transfers = []
    for inner in info.get("inner-txns", []):
        txn = inner.get("txn", {}).get("txn", {})
        if txn.get("type") == "axfer":
            transfers.append((txn["arcv"], int(txn.get("aamt", 0))))
    return transfers


def opcode_budget(algod: Any, raw_bytes: list[bytes]) -> tuple[int, int] | None:
    """`(consumed, budget)` for a join group, measured by simulate.

    The seat ceiling of 20 is a *measured* opcode-budget limit rather than a
    chosen number -- `join` scans the roster for a repeated payer, so it costs
    more with every seat already taken. Whether a five-seat pool is anywhere
    near that wall is a question only a real group can answer, and this is how
    to ask it without spending a settlement.

    Must run **before** the group is submitted: simulate refuses a transaction
    already in the ledger. `allow_empty_signatures` is what lets the fee-payer
    leg stay unsigned, which is how `build_join_group` leaves it for the
    facilitator to countersign.

    Returns None rather than raising if simulate refuses: an opcode
    measurement is a finding, not a precondition, and it must never be the
    reason a scenario goes red.
    """
    try:
        # `build_join_group` returns mixed bytes -- signed where the buyer
        # signed, unsigned for the leg the facilitator will countersign -- so
        # the unsigned one is wrapped before simulate, which takes signed
        # transactions and is told separately to tolerate an empty signature.
        txns = []
        for raw in raw_bytes:
            decoded = encoding.msgpack_decode(base64.b64encode(raw).decode())
            if isinstance(decoded, transaction.Transaction):
                decoded = transaction.SignedTransaction(decoded, None)
            txns.append(decoded)
        response = algod.simulate_transactions(
            SimulateRequest(
                txn_groups=[SimulateRequestTransactionGroup(txns=txns)],
                allow_empty_signatures=True,
            )
        )
        group = (response.get("txn-groups") or [{}])[0]
        return int(group["app-budget-consumed"]), int(group["app-budget-added"])
    except Exception:  # noqa: BLE001 - see below
        # Deliberately broad. This is a measurement taken alongside a scenario
        # that spends real money, and there is no failure of it worth losing
        # that scenario over -- a node that declines simulate, an SDK whose
        # request model moved, a response shape that changed. Narrowing this
        # once already turned an unavailable measurement into a red run that
        # had settled nothing and left a pool open.
        return None


def pool_server(
    e2e_env: Env,
    algod: Any,
    admin: Account,
    job_db_path: str,
    agreement_id: int,
    *,
    seats: int = POOL_SEATS,
    share_price: int = POOL_SHARE_PRICE,
):
    """A server configured to serve exactly this pool, on its own port.

    Lifted from `test_index_pool_seat._pool_server` and parameterised, because
    three scenarios now need it. The session-wide `server` fixture is built
    with no pool configured, which is the shipped default and right for every
    other scenario; rather than reconfigure it underneath them, this serves a
    second app through the same loader, so the route is assembled exactly as
    production assembles it.

    The overrides are folded into an `Env` before they reach `build_settings`,
    never after: a plain dict copies the mnemonics out of the redacting
    mapping, and a ConfigError raised downstream would then render every one
    of them into the traceback.
    """
    overrides = Env(
        {
            **e2e_env,
            "INDEX_AGREEMENT_ID": str(agreement_id),
            "INDEX_SEATS": str(seats),
            "INDEX_SHARE_PRICE_MICRO": str(share_price),
        }
    )

    def build_app(base_url: str):
        settings = build_settings(overrides, base_url, job_db_path)
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

    return serve(build_app)


@dataclass(frozen=True)
class Seat:
    """One bought seat, and the identity of the payment that bought it."""

    index: int
    payer: str
    axfer_txid: str
    app_call_txid: str
    elapsed: float
    # (consumed, budget) for a join at this roster depth, or None if simulate
    # declined. `join` scans the roster, so consumption rises with the seat.
    budget: tuple[int, int] | None


def fill(
    http: httpx.Client,
    url: str,
    *,
    payers: list[Account],
    agreement_id: int,
    commit_hash: bytes,
    settings: Any,
    algod: Any,
    reader: EscrowClient,
    seats_total: int = POOL_SEATS,
    share_price: int = POOL_SHARE_PRICE,
) -> list[Seat]:
    """Buy one seat per payer, in order, through the real facilitator.

    Each pass asserts the quote against the pool's live state before paying,
    so a 402 that has gone stale -- a seat count that did not move, a
    commitment that is not the one under test -- is caught before money moves
    rather than after.
    """
    bought: list[Seat] = []
    for index, payer in enumerate(payers):
        record = reader.read_agreement(agreement_id)
        assert record is not None, f"agreement {agreement_id} vanished mid-fill"
        remaining = record.deadline - latest_timestamp(algod)
        assert remaining >= JOIN_HEADROOM_SECONDS, (
            f"only {remaining}s of the funding window left before seat {index} "
            f"of {seats_total}. The fill is outrunning the deadline, which "
            "bounds filling and delivering together -- raise "
            "FILL_DEADLINE_SECONDS rather than reading the contract's refusal "
            "as a fault."
        )

        started = time.monotonic()
        quoted = buyer_client.quote(http, url, settings.network)

        extra = quoted.requirements.extra or {}
        assert extra["tag"] == CHALLENGE_TAG
        assert extra["feePayer"]
        assert int(extra["agreementId"]) == agreement_id
        assert int(extra["seatsTotal"]) == seats_total
        assert int(extra["seatsTaken"]) == index
        assert int(extra["seatsLeft"]) == seats_total - index
        assert extra["commitSha256"] == commit_hash.hex()
        assert quoted.amount == share_price

        # Measured before paying, on a group of the same shape at the same
        # roster depth. The cost `join` is asked to bear depends on how many
        # seats it has to scan, not on which transaction does the scanning, so
        # a throwaway group answers the ceiling question exactly -- and unlike
        # the one about to be submitted, it can still be simulated.
        probe = build_join_group(
            algod_client=algod,
            buyer_signer=payer.avm_signer,
            fee_payer_address=quoted.fee_payer,
            app_id=settings.app_id,
            app_account=quoted.requirements.pay_to,
            asset_id=int(quoted.requirements.asset),
            amount=quoted.amount,
            agreement_id=agreement_id,
        )
        budget = opcode_budget(algod, probe["rawBytes"])

        paid = buyer_client.pay(
            http,
            url,
            quoted,
            algod=algod,
            buyer_signer=payer.avm_signer,
            app_id=settings.app_id,
        )
        assert paid.status_code == 200, f"seat {index} failed: {paid.text}"

        events = events_for(algod, paid.app_call_txid)
        joined = one_event(events, "Joined")
        assert joined["agreement_id"] == agreement_id
        assert joined["payer"] == payer.address, (
            f"the contract credited seat {index} to {joined['payer']}, not to "
            f"{payer.address}, which is the account that signed the transfer"
        )
        assert joined["seat"] == index
        assert joined["amount"] == share_price
        assert txid_str(joined["payment_txn_id"]) == paid.axfer_txid, (
            "the seat has to be credited to the axfer this client built, not "
            "to some other transfer that happened to be in the group"
        )

        # `Filled` fires only on the seat that meets the quota, and only when
        # min_seats > 1. A two-seat scenario that buys one seat never reaches
        # this branch, so these are its first executions outside LocalNet.
        filled = [payload for name, payload in events if name == "Filled"]
        if index == seats_total - 1:
            assert filled == [{"agreement_id": agreement_id, "seats": seats_total}], (
                "the seat that fills the pool must emit Filled, or a consumer "
                "keying on that event misses the pool filling"
            )
        else:
            assert filled == [], (
                f"Filled was emitted on seat {index} of {seats_total}, before "
                "the quota was met"
            )

        wait_until(
            lambda i=index: (
                (r := reader.read_agreement(agreement_id)) and r.seats == i + 1
            ),
            timeout=90,
            what=f"seat {index} to land on agreement {agreement_id}",
        )
        bought.append(
            Seat(
                index=index,
                payer=payer.address,
                axfer_txid=paid.axfer_txid,
                app_call_txid=paid.app_call_txid,
                elapsed=time.monotonic() - started,
                budget=budget,
            )
        )
    return bought
