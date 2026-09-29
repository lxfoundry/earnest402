"""An agreement created on chain is always reachable from the store.

The create returns the id, so the job row naming it can only be written
*after* the money is committed. Everything here is about that gap: a request
cancelled or crashed inside it must leave a reconcilable record rather than an
agreement holding minimum balance that nothing can enumerate.
"""

import pytest

from api.pricing import make_price_callable
from tests.conftest import await_price

SHA = "a" * 64


def test_a_completed_quote_leaves_no_intent_behind(deps, ctx_factory):
    """The happy path must not accumulate reconciliation work."""
    price = make_price_callable(deps)
    await_price(price, ctx_factory(sha256=SHA, size=1024))

    assert deps.escrow.created == 1
    assert deps.jobs.unreconciled_create_intents(older_than=deps.now()) == []


def test_an_interrupted_create_leaves_the_agreement_findable(deps, ctx_factory):
    """The gap, reproduced: the create lands, the job row never does.

    Standing in for a cancelled task or a crash, because both land in exactly
    this place -- after the on-chain call has committed and before
    create_quote's INSERT.
    """
    price = make_price_callable(deps)

    def refuse(**_kwargs):
        raise RuntimeError("interrupted between the create and the row")

    deps.jobs.create_quote = refuse

    with pytest.raises(RuntimeError):
        await_price(price, ctx_factory(sha256=SHA, size=1024))

    # The agreement exists on chain and no job row names it ...
    assert deps.escrow.created == 1
    assert deps.jobs.get_by_agreement(1) is None
    # ... and it is nonetheless findable, with what is needed to act on it.
    outstanding = deps.jobs.unreconciled_create_intents(older_than=deps.now())
    assert [intent["agreement_id"] for intent in outstanding] == [1]
    assert outstanding[0]["sha256"] == SHA
    assert outstanding[0]["size"] == 1024


def test_an_intent_whose_job_row_exists_is_not_reconciliation_work(deps, ctx_factory):
    """A crash between the row and the clear needs no chain lookup.

    The row is what the intent was protecting, so an intent sitting beside one
    is bookkeeping to delete rather than an orphan to investigate -- and the
    query must not send a reconciler to chain for it.
    """
    price = make_price_callable(deps)
    await_price(price, ctx_factory(sha256=SHA, size=1024))
    # Re-add the intent the successful path just cleared.
    deps.jobs.record_create_intent(
        agreement_id=1, sha256=SHA, size=1024, requested_at=deps.now()
    )

    assert deps.jobs.unreconciled_create_intents(older_than=deps.now()) == []


def test_a_mispredicted_id_does_not_strand_an_intent(deps, ctx_factory):
    """Another process creating concurrently shifts the real id.

    The prediction is agreement_count + 1 read under this process's lock, so a
    second process creating in between makes it wrong. Both ids are cleared on
    success; leaving the predicted one would send a reconciler after an
    agreement that never existed.
    """
    price = make_price_callable(deps)
    deps.escrow.id_skew = 5  # next_agreement_id() will over-predict

    await_price(price, ctx_factory(sha256=SHA, size=1024))

    assert deps.escrow.created == 1
    assert deps.jobs.get_by_agreement(1) is not None
    assert deps.jobs.unreconciled_create_intents(older_than=deps.now()) == []


def test_a_fresh_intent_is_not_yet_reconciliation_work(deps):
    """`older_than` exists so a create in flight right now is left alone."""
    deps.jobs.record_create_intent(
        agreement_id=1, sha256=SHA, size=1024, requested_at=deps.now()
    )

    assert deps.jobs.unreconciled_create_intents(older_than=deps.now() - 1) == []
    assert len(deps.jobs.unreconciled_create_intents(older_than=deps.now())) == 1
