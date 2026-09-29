"""S3 -- a quote nobody paid, and the capital it parked.

Every 402 creates an agreement and parks a deposit, so an ordinary probe costs
the operator working capital. That is expected rather than abusive, but it
means the capacity to quote -- not loss -- is the binding constraint, and the
way back is the creator's early exit rather than waiting out a deadline sized
for delivery.

The exception being exercised: `expire` is permissionless *after* the deadline,
and before it is open only to the creator and only while `seats == 0`. Both
halves matter. A permissionless early exit would be a free denial of service
against a buyer mid-settlement; a creator-only exit that still worked after
somebody paid would let the operator cancel a funded agreement.

No test anywhere has run this path against a real network.
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from api.escrow import (
    STATE_EXPIRED,
    STATE_OPEN,
    STATE_REFUNDED,
    required_deposit,
)
from tests.e2e import buyer as buyer_client
from tests.e2e.events import events_for, one_event
from tests.e2e.harness import REJECTED, algo_balance

pytestmark = pytest.mark.testnet

EXPIRED_BY_DEADLINE = 0


def test_the_creator_reclaims_an_unfunded_quote_without_waiting(
    server, settings, http, algod, as_admin, as_buyer, admin
):
    content = secrets.token_bytes(64)
    sha256 = hashlib.sha256(content).hexdigest()

    admin_algo_before = algo_balance(algod, admin.address)
    deposit = required_deposit(1, as_admin.min_fee())

    # -- quote, and never pay ----------------------------------------------
    url = buyer_client.pin_url(server, sha256, len(content))
    quote = buyer_client.quote(http, url, settings.network)
    agreement_id = quote.agreement_id

    agreement = as_admin.read_agreement(agreement_id)
    assert agreement is not None
    assert agreement.state == STATE_OPEN
    assert agreement.seats == 0
    assert agreement.total_held == 0

    parked = admin_algo_before - algo_balance(algod, admin.address)
    assert parked >= deposit, (
        f"the quote should have parked at least the {deposit} microALGO "
        f"deposit; admin only spent {parked}"
    )

    # -- the exception is the creator's alone ------------------------------
    with pytest.raises(REJECTED):
        as_buyer.expire(agreement_id)
    assert as_admin.read_agreement(agreement_id).state == STATE_OPEN, (
        "before the deadline, expire is the creator's alone -- a permissionless "
        "early exit would be a free denial of service against a buyer who is "
        "mid-settlement"
    )

    # -- and it does not need the deadline ---------------------------------
    expire_txid = as_admin.expire(agreement_id)
    expired = one_event(events_for(algod, expire_txid), "Expired")
    assert expired["seats"] == 0
    assert expired["reason"] == EXPIRED_BY_DEADLINE
    assert as_admin.read_agreement(agreement_id).state == STATE_EXPIRED

    # -- nothing to pay, so one pass finishes the sweep --------------------
    refund_txid = as_admin.refund_next(agreement_id, 1)
    complete = one_event(events_for(algod, refund_txid), "RefundComplete")
    assert (complete["paid"], complete["skipped"]) == (0, 0)
    assert as_admin.read_agreement(agreement_id).state == STATE_REFUNDED, (
        "an agreement with no payers needs no special case: the cursor has "
        "already reached seats"
    )

    # -- and the capital comes back ----------------------------------------
    close_txid = as_admin.close(agreement_id)
    closed = one_event(events_for(algod, close_txid), "Closed")
    assert closed["algo_returned"] >= deposit - as_admin.min_fee(), (
        f"an unfunded agreement moved no money, so close should return "
        f"substantially the whole {deposit} microALGO deposit; it returned "
        f"{closed['algo_returned']}"
    )
    assert as_admin.read_agreement(agreement_id) is None
