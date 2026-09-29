"""What the 200 mints against, and what the loser of a race is told.

The invariant these tests protect is one sentence: **the agreement that gets
the jobId is always the agreement the verified payment was matched against.**

That is worth pinning because the obvious alternative -- have the handler ask
the store again which quote is open for these bytes -- is a *different*
question with a different answer. x402-avm builds the requirements by running
the price callable on the paid pass and hands the matched object to the handler
on request.state, so reading it is the one way for the two to agree by
construction rather than by coincidence.

Asserted through the real middleware over a stubbed facilitator, so
request.state carries whatever PaymentMiddlewareASGI actually put there.
"""

from api.jobs import JobState
from tests.conftest import accepts_of, payment_header_for

SHA = "a" * 64
SIZE = 1024


def _seed(deps, agreement_id: int, sha256: str = SHA, size: int = SIZE) -> None:
    deps.jobs.create_quote(
        agreement_id=agreement_id,
        sha256=sha256,
        size=size,
        deadline=deps.now() + deps.settings.delivery_deadline_seconds,
        quoted_at=deps.now(),
    )


def _quote(client, sha256: str = SHA, size: int = SIZE) -> dict:
    unpaid = client.post(f"/pin?sha256={sha256}&size={size}")
    assert unpaid.status_code == 402, unpaid.text
    return accepts_of(unpaid)[0]


def test_the_funded_agreement_is_the_one_the_payment_was_matched_to(
    client, facilitator, deps
):
    """The invariant, with two live quotes for one file so it can be wrong.

    find_reusable_quote orders by agreement_id, so with 1000 and 1001 both in
    window it answers 1000. Whatever it answers, the row that ends up FUNDED
    must be that same one -- and the other must be untouched.
    """
    _seed(deps, 1000)
    _seed(deps, 1001)

    requirements = _quote(client)
    matched = requirements["extra"]["agreementId"]
    assert matched in (1000, 1001)

    paid = client.post(
        f"/pin?sha256={SHA}&size={SIZE}",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert paid.status_code == 200, paid.text
    assert paid.json()["agreementId"] == matched
    assert deps.jobs.get_by_agreement(matched).state is JobState.FUNDED
    assert deps.jobs.get_by_agreement(matched).job_id

    other = 1001 if matched == 1000 else 1000
    assert deps.jobs.get_by_agreement(other).state is JobState.QUOTED
    assert deps.jobs.get_by_agreement(other).job_id is None


def test_a_buyer_cannot_choose_which_agreement_their_payment_funds(
    client, facilitator, deps
):
    """`extra` is server-authoritative, and this is what guarantees it.

    The buyer's payload carries its own `accepted` requirements, but the
    middleware does not trust them: it rebuilds the route's requirements by
    running the price callable and hands the handler the object *it* built
    (find_matching_requirements compares scheme, network, amount, asset and
    payTo -- never extra). So a payload naming somebody else's agreement is
    matched on the comparable fields and then ignored on extra.

    Without that, `extra.agreementId` would be client input on the one field
    that decides which escrow row gets credited.
    """
    _seed(deps, 2000, sha256="b" * 64)
    requirements = _quote(client)
    server_chose = requirements["extra"]["agreementId"]
    assert server_chose != 2000

    forged = dict(requirements)
    forged["extra"] = {**requirements["extra"], "agreementId": 2000}
    paid = client.post(
        f"/pin?sha256={SHA}&size={SIZE}",
        # The forgery is confined to `accepted.extra`, which is the claim
        # under test. The transaction group is the honest one the buyer was
        # quoted -- a group that *actually* joined 2000 is a different attack
        # with a different answer, and has its own test below.
        headers={
            "PAYMENT-SIGNATURE": payment_header_for(
                forged, join_agreement_id=server_chose
            )
        },
    )

    assert paid.status_code == 200, paid.text
    assert paid.json()["agreementId"] == server_chose
    # The agreement the payload pointed at belongs to different bytes and is
    # left exactly as it was.
    assert deps.jobs.get_by_agreement(2000).state is JobState.QUOTED
    assert deps.jobs.get_by_agreement(2000).job_id is None


def test_a_group_that_joins_another_agreement_is_refused_before_settlement(
    client, facilitator, deps
):
    """The stronger half of the claim above, and the one `extra` cannot make.

    `extra.agreementId` decides what the *receipt* says. The `join` call in
    the buyer's own transaction group decides which escrow row the chain
    credits -- the contract reads its argument, not our response body. So a
    group whose join names a different agreement really would fund that other
    agreement while this handler minted a job against the one the server
    chose: a seat bought here, credited there, with the two records
    permanently disagreeing.

    Nothing downstream can catch it. The group is internally consistent, so
    it simulates and settles; the contract has no idea another agreement was
    quoted. It has to be refused here, before settlement, which is the last
    moment the buyer still holds their USDC.
    """
    _seed(deps, 2000, sha256="b" * 64)
    requirements = _quote(client)
    server_chose = requirements["extra"]["agreementId"]
    assert server_chose != 2000
    settled_before = len(facilitator.settled)

    paid = client.post(
        f"/pin?sha256={SHA}&size={SIZE}",
        headers={
            "PAYMENT-SIGNATURE": payment_header_for(
                requirements, join_agreement_id=2000
            )
        },
    )

    assert paid.status_code == 400, paid.text
    assert paid.json()["detail"]["error"] == "payment_group_incomplete"
    # The whole point of refusing here rather than later: nothing settled, so
    # the buyer still has their money and neither agreement moved.
    assert len(facilitator.settled) == settled_before
    assert deps.jobs.get_by_agreement(2000).state is JobState.QUOTED
    assert deps.jobs.get_by_agreement(server_chose).job_id is None


def test_a_group_with_no_join_call_is_refused_before_settlement(
    client, facilitator, deps
):
    """The case with no on-chain consequence at all, and the worst outcome.

    A transfer with no join settles into the application account against no
    seat, no roster entry and no refund path. `close` returns the ALGO
    deposit to the creator and `refund_next` walks a roster this buyer is not
    on, so the USDC is recoverable by nobody -- not the buyer, not us. Unlike
    every other malformed group, this one does not fail on chain: there is no
    application call left to fail.
    """
    _seed(deps, 3000, sha256=SHA)
    requirements = _quote(client)
    settled_before = len(facilitator.settled)

    paid = client.post(
        f"/pin?sha256={SHA}&size={SIZE}",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements, joins=0)},
    )

    assert paid.status_code == 400, paid.text
    assert paid.json()["detail"]["error"] == "payment_group_incomplete"
    assert "join" in paid.json()["detail"]["reason"]
    assert len(facilitator.settled) == settled_before


def test_the_cross_process_race_loser_gets_409_and_both_outcomes(
    client, facilitator, deps, monkeypatch
):
    """The window the IllegalTransition handler exists for.

    One process's price callable sees the quote open; another process funds it
    before the first reaches its handler; the first then mints against a row
    that is already FUNDED. FUNDED -> FUNDED is not a transition, so the store
    raises IllegalTransition -- which, raised from inside the router, does
    reach an exception handler, unlike anything the price callable raises.

    Simulated by funding the row between the two, which is exactly what the
    other process would have done.
    """
    requirements = _quote(client)
    agreement_id = requirements["extra"]["agreementId"]
    settled_before = len(facilitator.settled)

    real_mint = deps.jobs.mint_job_id

    def other_process_wins_first(*, agreement_id, job_id):
        deps.jobs.transition(agreement_id, JobState.FUNDED, job_id="winner-elsewhere")
        return real_mint(agreement_id=agreement_id, job_id=job_id)

    monkeypatch.setattr(deps.jobs, "mint_job_id", other_process_wins_first)

    loser = client.post(
        f"/pin?sha256={SHA}&size={SIZE}",
        headers={"PAYMENT-SIGNATURE": payment_header_for(requirements)},
    )

    assert loser.status_code == 409, loser.text
    detail = loser.json()["detail"]
    assert {option["outcome"] for option in detail["options"]} == {"requote", "attach"}
    assert detail["charged"] is False
    requote = next(o for o in detail["options"] if o["outcome"] == "requote")
    attach = next(o for o in detail["options"] if o["outcome"] == "attach")
    # The two offers are not equivalent, and the body does not pretend they are.
    assert requote["costs"] != attach["costs"]
    assert "nothing" in attach["guarantees"]
    # 409 is >= 400, so the middleware never settled: the loser is not charged.
    assert len(facilitator.settled) == settled_before
    assert deps.jobs.get_by_agreement(agreement_id).job_id == "winner-elsewhere"


def test_paying_twice_for_one_seat_is_refused_by_the_price_callable(
    client, facilitator, deps
):
    """The single-process shape, which never reaches the handler at all.

    Once the agreement is funded there is no reusable quote, so the *price
    callable* refuses the second retry -- and that refusal is swallowed into
    the generic 500 (see tests/test_error_handling.py). Pinned here so the
    two paths are not confused: this one is the library's swallowing, and it
    carries none of the two outcomes the 409 above does.

    Money is still safe, which is the part that matters: 500 is >= 400, so no
    second settlement happens.
    """
    requirements = _quote(client)
    headers = {"PAYMENT-SIGNATURE": payment_header_for(requirements)}

    assert (
        client.post(f"/pin?sha256={SHA}&size={SIZE}", headers=headers).status_code
        == 200
    )
    settled_after_winner = len(facilitator.settled)

    again = client.post(f"/pin?sha256={SHA}&size={SIZE}", headers=headers)

    assert again.status_code == 500
    assert again.json() == {"error": "Failed to process request"}
    assert len(facilitator.settled) == settled_after_winner
