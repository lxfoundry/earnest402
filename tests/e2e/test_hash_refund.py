"""S2 -- non-delivery: the deadline passes and the buyer is made whole.

`docs`-level framing worth keeping in front of the assertions: non-delivery is
the one failure class the escrow handles trustlessly end to end. Nothing here
needs the operator's cooperation, which is why every call below is sent by the
*buyer* rather than by the admin. If any of them required the admin, a buyer
whose counterparty walked away would have no way out.

This is also the slowest scenario in the suite, and unavoidably so: there is no
timestamp offset on TestNet, so the deadline is real elapsed time.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from api.escrow import STATE_EXPIRED, STATE_FUNDED, STATE_REFUNDED
from tests.e2e import buyer as buyer_client
from tests.e2e.events import events_for, one_event
from tests.e2e.harness import (
    REJECTED,
    algo_balance,
    usdc_balance,
    wait_past_deadline,
    wait_until,
)

pytestmark = pytest.mark.testnet

EXPIRED_BY_DEADLINE = 0


def test_an_undelivered_job_expires_and_refunds_the_buyer(
    server, settings, http, algod, as_admin, as_verifier, as_buyer, buyer, admin
):
    asset_id = settings.usdc_asa_id
    price = settings.price_micro_usdc

    content = secrets.token_bytes(64)
    sha256 = hashlib.sha256(content).hexdigest()

    buyer_usdc_before = usdc_balance(algod, buyer.address, asset_id)
    app_usdc_before = usdc_balance(algod, settings.app_account, asset_id)

    quote, paid = buyer_client.purchase(
        http,
        server,
        sha256=sha256,
        size=len(content),
        network=settings.network,
        algod=algod,
        buyer_signer=buyer.avm_signer,
        app_id=settings.app_id,
    )
    assert paid.status_code == 200, f"paid retry failed: {paid.text}"
    agreement_id = quote.agreement_id

    funded = wait_until(
        lambda: (
            a
            if (a := as_admin.read_agreement(agreement_id)) and a.state == STATE_FUNDED
            else None
        ),
        timeout=90,
        what=f"agreement {agreement_id} to reach FUNDED",
    )
    assert usdc_balance(algod, buyer.address, asset_id) == buyer_usdc_before - price

    # -- the deadline passes -----------------------------------------------
    wait_past_deadline(algod, funded.deadline)

    with pytest.raises(REJECTED):
        as_verifier.release_hash(agreement_id, bytes.fromhex(sha256))
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED, (
        "a late release must be refused cleanly: no release beats a refund "
        "once the deadline the buyer was promised has passed"
    )

    # -- expire, sent by the buyer to prove it needs nobody's permission ----
    expire_txid = as_buyer.expire(agreement_id)
    expired_event = one_event(events_for(algod, expire_txid), "Expired")
    assert expired_event["reason"] == EXPIRED_BY_DEADLINE
    assert expired_event["seats"] == 1
    assert as_admin.read_agreement(agreement_id).state == STATE_EXPIRED

    # -- refund ------------------------------------------------------------
    admin_algo_before_close = algo_balance(algod, admin.address)
    refund_txid = as_buyer.refund_next(agreement_id, 1)
    refund_events = events_for(algod, refund_txid)

    refunded = one_event(refund_events, "Refunded")
    assert refunded["payer"] == buyer.address
    assert refunded["amount"] == price
    assert refunded["seat"] == 0

    complete = one_event(refund_events, "RefundComplete")
    assert (complete["paid"], complete["skipped"]) == (1, 0), (
        "the counts are read off the record, not off this batch, so they stay "
        "right when a claim lands between two passes"
    )

    after = as_admin.read_agreement(agreement_id)
    assert after.state == STATE_REFUNDED
    assert after.total_held == 0
    assert after.unclaimed_seats == 0, (
        "REFUNDED means the cursor finished its pass; only unclaimed_seats == 0 "
        "means everybody actually has their money"
    )
    assert usdc_balance(algod, buyer.address, asset_id) == buyer_usdc_before, (
        "the buyer walks away whole -- that is the whole promise of this path"
    )
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before

    # -- close, also permissionless ----------------------------------------
    close_txid = as_buyer.close(agreement_id)
    closed = one_event(events_for(algod, close_txid), "Closed")
    assert closed["algo_returned"] > 0

    assert as_admin.read_agreement(agreement_id) is None
    assert algo_balance(algod, admin.address) == (
        admin_algo_before_close + closed["algo_returned"]
    ), (
        "the deposit goes back to the agreement's creator, not to whoever "
        "happened to call close"
    )
