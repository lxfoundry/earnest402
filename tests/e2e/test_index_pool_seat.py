"""S4 -- the pool seat: POST /index, quoted, paid and recorded on chain.

The other scenarios all exercise the delivery escrow, whose agreement is
created by the price callable on the unpaid pass. A pool is not like that. It
is created out of band by the operator, one at a time, and the route's only
job is to name it in the 402 and refuse cleanly when there is none -- so what
this scenario proves, and no cheaper layer can, is that the real facilitator
accepts a settlement group whose `join` targets a *multi-seat* agreement the
server did not create in the same request.

It is deliberately the smallest pool that is still a pool: two seats, one
bought. One seat would be the delivery escrow wearing a different name, and
the route refuses it for exactly that reason.

The scenario cleans up after itself rather than leaving a partially filled
pool on chain. That is not only hygiene: the refund it walks is the guarantee
a multi-seat `hash` agreement actually offers -- refund on non-delivery -- and
it is the path a pool that misses its threshold has to take.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from functools import partial
from typing import Any

import httpx
import pytest
from x402.http import PAYMENT_REQUIRED_HEADER

from api.app import create_app
from api.config import CHALLENGE_TAG
from api.deps import Deps
from api.escrow import STATE_EXPIRED, STATE_REFUNDED, EscrowClient
from api.jobs import JobStore
from api.routes import build_routes
from tests.e2e import buyer as buyer_client
from tests.e2e.events import events_for, one_event
from tests.e2e.harness import (
    REJECTED,
    Env,
    build_settings,
    reclaim,
    usdc_balance,
    wait_past_deadline,
)
from tests.e2e.serve import serve

pytestmark = pytest.mark.testnet

# The smallest multi-seat pool. `min_seats == max_seats`, so this number is
# both the threshold and the ceiling.
POOL_SEATS = 2

# Priced like the delivery escrow rather than like a real edition: the buyer
# is funded from a faucet, and what is under test is the shape of the
# settlement, not the number on it.
POOL_SHARE_PRICE = 100_000


def _open_pool(as_admin: EscrowClient, *, deadline: int, treasury: str, verifier: str):
    """Create the pool this scenario sells a seat in, or skip saying why.

    A multi-seat `hash` agreement is what the whole scenario rests on, and the
    deployed application may predate it -- `create_agreement` rejected
    `min_seats > 1` on `CONDITION_HASH` until the contract was widened.

    Discriminating the two is not a matter of reading the error. An assert
    that fails on chain comes back as a program counter and the opcodes
    around it, never as the comment beside the assert in the source, so
    matching on the words "single-seat" matches nothing a real node says.
    The chain answers the question directly instead: create the same
    agreement with one seat, and if *that* is accepted then the seat count
    was the only difference. The probe agreement is reclaimed immediately --
    it has no payers, so it need not wait for its deadline -- and if the
    single-seat create is refused too, the original rejection is re-raised,
    because then the seat count was not the problem.
    """
    commit_hash = hashlib.sha256(secrets.token_bytes(64)).digest()
    create = partial(
        as_admin.create_hash_agreement,
        commit_hash=commit_hash,
        share_price=POOL_SHARE_PRICE,
        deadline=deadline,
        beneficiary=treasury,
        verifier=verifier,
        note=commit_hash.hex().encode(),
    )
    try:
        return create(seats=POOL_SEATS)
    except REJECTED:
        reclaim(as_admin, create(seats=1))
        pytest.skip(
            "the deployed escrow application refuses `min_seats > 1` on a "
            "`hash` agreement -- the same parameters were accepted with one "
            "seat -- so there is no pool to sell a seat in. Deploy the "
            "widened contract and re-run."
        )


def _pool_server(e2e_env: Env, algod: Any, admin, job_db_path: str, agreement_id: int):
    """A second server, configured to serve exactly this pool.

    The session-wide `server` fixture is built once with no pool configured,
    which is the shipped default and the right one for every other scenario.
    Rather than reconfigure it underneath them, this serves a second app on
    its own port -- through the same loader, so the route is assembled exactly
    as production assembles it.

    The overrides are folded into an `Env` before they reach `build_settings`,
    never after: a plain dict copies the mnemonics out of the redacting
    mapping, and a ConfigError raised downstream would then render every one
    of them into the traceback.
    """
    overrides = Env(
        {
            **e2e_env,
            "INDEX_AGREEMENT_ID": str(agreement_id),
            "INDEX_SEATS": str(POOL_SEATS),
            "INDEX_SHARE_PRICE_MICRO": str(POOL_SHARE_PRICE),
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


def test_a_pool_seat_is_quoted_paid_and_refunded(
    e2e_env,
    settings,
    algod,
    as_admin,
    as_buyer,
    admin,
    buyer,
    treasury,
    verifier,
    job_db_path,
):
    asset_id = settings.usdc_asa_id
    deadline = int(time.time()) + settings.delivery_deadline_seconds

    agreement_id = _open_pool(
        as_admin,
        deadline=deadline,
        treasury=treasury.address,
        verifier=verifier.address,
    )

    record = as_admin.read_agreement(agreement_id)
    assert record is not None and record.min_seats == POOL_SEATS, (
        "the pool has to exist with the seat count the route is configured "
        "for, or the route refuses to quote it at all"
    )

    buyer_usdc_before = usdc_balance(algod, buyer.address, asset_id)

    with _pool_server(e2e_env, algod, admin, job_db_path, agreement_id) as base_url:
        url = f"{base_url}/index"
        with httpx.Client(timeout=120.0) as http:
            unpaid = http.post(url)
            assert unpaid.status_code == 402, (
                f"a configured, open pool must be payable: {unpaid.text}"
            )
            assert unpaid.headers.get(PAYMENT_REQUIRED_HEADER), (
                "the requirements ride in the header; a body-only 402 offers "
                "a buyer nothing it can pay"
            )

            quoted = buyer_client.quote(http, url, settings.network)
            extra = quoted.requirements.extra or {}
            # The attribution facts, read off the wire rather than out of the
            # object the callable returned in-process. Both products share one
            # payTo, and the tag is stamped at the first settlement for good.
            assert extra["tag"] == CHALLENGE_TAG
            assert extra["feePayer"]
            assert int(extra["agreementId"]) == agreement_id
            assert int(extra["seatsTotal"]) == POOL_SEATS
            assert int(extra["seatsLeft"]) == POOL_SEATS
            assert quoted.amount == POOL_SHARE_PRICE

            paid = buyer_client.pay(
                http,
                url,
                quoted,
                algod=algod,
                buyer_signer=buyer.avm_signer,
                app_id=settings.app_id,
            )

    assert paid.status_code == 200, f"paid retry failed: {paid.text}"
    receipt = paid.json()
    assert receipt["agreementId"] == agreement_id
    assert receipt["seatsTotal"] == POOL_SEATS
    assert receipt["commitSha256"] == record.commit_hash.hex()

    # The seat exists on chain, credited to the account that signed the
    # transfer -- which is the only thing the receipt above is a claim about.
    joined = one_event(events_for(algod, paid.app_call_txid), "Joined")
    assert joined["agreement_id"] == agreement_id
    assert joined["payer"] == buyer.address, (
        f"the contract credited {joined['payer']}, not the buyer that signed"
    )
    assert joined["amount"] == POOL_SHARE_PRICE

    after_join = as_admin.read_agreement(agreement_id)
    assert after_join.seats == 1, "one seat of two taken"
    assert (
        usdc_balance(algod, buyer.address, asset_id)
        == buyer_usdc_before - POOL_SHARE_PRICE
    )

    # A pool that misses its threshold refunds, and every call below is sent
    # by the buyer: if any of them needed the operator, a pool whose operator
    # walked away would hold its payers' money forever.
    wait_past_deadline(algod, deadline)
    as_buyer.expire(agreement_id)
    assert as_buyer.read_agreement(agreement_id).state == STATE_EXPIRED

    as_buyer.refund_next(agreement_id, 1)
    assert as_buyer.read_agreement(agreement_id).state == STATE_REFUNDED
    assert usdc_balance(algod, buyer.address, asset_id) == buyer_usdc_before, (
        "a missed pool makes its payers whole"
    )

    as_buyer.close(agreement_id)
    assert as_buyer.read_agreement(agreement_id) is None
