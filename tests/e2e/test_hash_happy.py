"""S1 -- the happy path: quote, pay, release, close.

The one circuit nothing else in the repository exercises. The contract suite
proves the state machine on LocalNet and the offline suite proves the server's
branches against fakes; only this proves that a real facilitator accepts what
we build, settles it into the application account, and that the payer the
contract recorded is the one who paid.

Redemption is driven from the test rather than the server: the upload route,
leg 2 and the delivery pipeline are not built yet, and the worker settles in
MainNet USDC, so leg 2 can never be real here. As those land, the calls below
become assertions about what the server did on its own.
"""

from __future__ import annotations

import hashlib
import re
import secrets

import pytest

from api.escrow import (
    CONDITION_HASH,
    STATE_FUNDED,
    STATE_OPEN,
    STATE_RELEASED,
    required_deposit,
)
from tests.e2e import buyer as buyer_client
from tests.e2e.events import events_for, one_event, txid_str
from tests.e2e.harness import (
    REJECTED,
    algo_balance,
    latest_timestamp,
    usdc_balance,
    wait_until,
)

pytestmark = pytest.mark.testnet

JOB_ID = re.compile(r"^[0-9a-f]{32}$")

# Seconds of the delivery deadline this scenario keeps for itself: the
# verifier's `release_hash` has to be *confirmed* before the deadline, not
# merely submitted, so the budget has to cover a block.
RELEASE_HEADROOM_SECONDS = 20


def test_a_paid_pin_settles_into_escrow_and_releases_to_the_treasury(
    server, settings, http, algod, as_admin, as_verifier, buyer, treasury, admin
):
    asset_id = settings.usdc_asa_id
    price = settings.price_micro_usdc

    # A distinct payload per run: the quote is idempotent on (sha256, size),
    # so reusing one would attach to the previous run's agreement.
    content = secrets.token_bytes(64)
    sha256 = hashlib.sha256(content).hexdigest()
    size = len(content)

    app_usdc_before = usdc_balance(algod, settings.app_account, asset_id)
    buyer_usdc_before = usdc_balance(algod, buyer.address, asset_id)
    treasury_usdc_before = usdc_balance(algod, treasury.address, asset_id)
    admin_algo_before = algo_balance(algod, admin.address)
    next_id_before = as_admin.next_agreement_id()

    # -- the unpaid pass ---------------------------------------------------
    url = buyer_client.pin_url(server, sha256, size)
    quote = buyer_client.quote(http, url, settings.network)

    assert quote.requirements.pay_to == settings.app_account, (
        "payTo must be the escrow application account, not a wallet: it is "
        "what makes the payment land in escrow rather than with the seller"
    )
    assert quote.amount == price
    assert int(quote.requirements.asset) == asset_id
    assert quote.requirements.max_timeout_seconds == settings.funding_window_seconds, (
        "the advertised settlement window must equal the funding window, or "
        "the 402 promises time the sweeper takes back"
    )
    extra = quote.requirements.extra or {}
    assert extra.get("decimals") == 6
    assert extra.get("tag") == "x402-global-challenge"
    assert extra.get("feePayer")

    agreement_id = quote.agreement_id
    assert agreement_id == next_id_before, (
        f"the quote created agreement {agreement_id}, but the next id was "
        f"{next_id_before}"
    )

    # -- what the unpaid pass parked on chain ------------------------------
    agreement = as_admin.read_agreement(agreement_id)
    assert agreement is not None, f"agreement {agreement_id} was never created"
    assert agreement.state == STATE_OPEN
    assert agreement.condition == CONDITION_HASH
    assert agreement.commit_hash == bytes.fromhex(sha256), (
        "the agreement must commit to the hash the buyer supplied, fixed "
        "before payment -- there is no method to bind it afterwards"
    )
    assert agreement.min_seats == agreement.max_seats == 1
    assert agreement.seats == 0
    assert agreement.share_price == price
    assert agreement.beneficiary == settings.treasury
    assert agreement.verifier == settings.verifier
    assert agreement.creator == admin.address

    # -- the paid pass -----------------------------------------------------
    paid = buyer_client.pay(
        http,
        url,
        quote,
        algod=algod,
        buyer_signer=buyer.avm_signer,
        app_id=settings.app_id,
    )
    assert paid.status_code == 200, f"paid retry failed: {paid.text}"
    body = paid.json()
    assert JOB_ID.match(body["jobId"]), f"jobId is not 32 hex characters: {body}"
    assert body["agreementId"] == agreement_id
    assert body["deadline"] == agreement.deadline

    # Settlement happens after the handler returns, so the 200 is not yet
    # proof the money moved. The chain is.
    #
    # Bounded by the agreement's own deadline, not by a flat 90s. The deadline
    # is fixed at *quote* time and E2E_DEADLINE_SECONDS is deliberately short,
    # so the two scenarios pull opposite ways on one setting: this one has to
    # release before the deadline the refund scenario waits out. A wait that
    # can outlive the agreement turns a slow-but-correct settle into what
    # looks like the contract refusing a valid release.
    budget = agreement.deadline - latest_timestamp(algod)
    funded = wait_until(
        lambda: (
            a
            if (a := as_admin.read_agreement(agreement_id)) and a.state == STATE_FUNDED
            else None
        ),
        timeout=max(0, min(90, budget - RELEASE_HEADROOM_SECONDS)),
        what=(
            f"agreement {agreement_id} to reach FUNDED within its "
            f"{budget}s delivery budget"
        ),
    )
    assert funded.seats == 1
    assert funded.total_held == price
    assert funded.unclaimed_seats == 0

    roster = as_admin.read_roster(agreement_id)
    assert roster == [(buyer.address, price)], (
        "the contract records the payer itself at settlement, with nobody "
        f"trusted in between; got {roster}"
    )

    # The one fact only this layer can establish: the payment the buyer signed,
    # the settlement the facilitator reported, and the payer the contract
    # recorded are all the *same* payment. Box state alone would pass even if
    # the group had been assembled differently than believed -- a payment and
    # the record it buys can look joined from the outside while being two
    # unlinked transactions, so the linkage is asserted rather than assumed.
    joined = one_event(events_for(algod, paid.app_call_txid), "Joined")
    assert joined["agreement_id"] == agreement_id
    assert joined["payer"] == buyer.address, (
        f"the contract credited {joined['payer']}, not the buyer that signed"
    )
    assert txid_str(joined["payment_txn_id"]) == paid.axfer_txid, (
        "the contract recorded a different transfer than the one the buyer "
        f"signed: recorded {txid_str(joined['payment_txn_id'])}, signed "
        f"{paid.axfer_txid}. The seat and the payment are not the same event."
    )
    assert joined["amount"] == price
    assert joined["provenance"] == 0, "0 is the atomic join path"

    # Named here rather than left to fail inside `release_hash`, where the
    # contract's own "deadline passed" arrives wrapped in an AlgodHTTPError and
    # reads as a rejected valid release.
    remaining = funded.deadline - latest_timestamp(algod)
    assert remaining >= RELEASE_HEADROOM_SECONDS, (
        f"only {remaining}s left of the delivery deadline, which is not enough "
        f"to release. The settle took most of E2E_DEADLINE_SECONDS; raise it "
        f"(the refund scenario's wait grows with it) rather than reading this "
        f"as a contract or facilitator fault."
    )

    settlement = paid.settlement
    assert settlement is not None, (
        "the paid pass carried no PAYMENT-RESPONSE header, so the facilitator "
        "reported no settlement for a request the server answered 200"
    )
    assert settlement.success, f"facilitator reported failure: {settlement!r}"
    assert settlement.payer == buyer.address, (
        f"the facilitator reported {settlement.payer} as the payer, not the "
        f"buyer {buyer.address} that signed the axfer"
    )
    assert settlement.transaction in paid.identities, (
        "the facilitator's settlement names a transaction that is not in the "
        f"group the buyer built: reported {settlement.transaction!r}, built "
        f"{sorted(paid.identities)}"
    )
    assert (
        usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before + price
    )
    assert usdc_balance(algod, buyer.address, asset_id) == buyer_usdc_before - price

    # The paid pass re-runs the price callable. If it created a second
    # agreement the money would be sitting against the wrong one.
    assert as_admin.next_agreement_id() == next_id_before + 1, (
        "exactly one agreement should exist across both passes"
    )

    # -- release -----------------------------------------------------------
    with pytest.raises(REJECTED):
        as_verifier.release_hash(agreement_id, secrets.token_bytes(32))
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED, (
        "a rejected release must leave the agreement exactly as it was"
    )

    released_txid = as_verifier.release_hash(agreement_id, bytes.fromhex(sha256))
    release_event = one_event(events_for(algod, released_txid), "Released")
    assert release_event["proof"] == bytes.fromhex(sha256), (
        "the Released event carries the delivered hash as its proof"
    )

    after_release = as_admin.read_agreement(agreement_id)
    assert after_release.state == STATE_RELEASED
    assert after_release.total_held == 0
    assert (
        usdc_balance(algod, treasury.address, asset_id) == treasury_usdc_before + price
    )
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before, (
        "the application account holds nothing of its own once an agreement "
        "has released"
    )

    # -- close -------------------------------------------------------------
    close_txid = as_admin.close(agreement_id)
    one_event(events_for(algod, close_txid), "Closed")

    assert as_admin.read_agreement(agreement_id) is None, (
        "close deletes both boxes; the event log is the permanent record"
    )
    assert as_admin.read_roster(agreement_id) == []

    deposit = required_deposit(1, as_admin.min_fee())
    admin_algo_after = algo_balance(algod, admin.address)
    assert admin_algo_after > admin_algo_before - deposit, (
        f"close should return most of the {deposit} microALGO deposit; admin "
        f"went from {admin_algo_before} to {admin_algo_after}"
    )
