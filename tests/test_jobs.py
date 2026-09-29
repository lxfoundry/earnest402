"""The job lifecycle and its store.

Transitions are tested *exhaustively* -- every ordered pair of states, not a
walk through the happy path. What matters is that the illegal ones are refused,
and there are far more of those than there are legal ones: a transition that
silently succeeds where it should not is how a job gets delivered twice, or
refunded after it was released.
"""

import itertools

import pytest

from api.jobs import (
    ALLOWED,
    TERMINAL,
    IllegalTransition,
    JobState,
    JobStore,
    check_transition,
)

SHA_A = "ab" * 32
SHA_B = "cd" * 32


# --- the state machine -------------------------------------------------


def test_the_legal_moves_are_exactly_these_eight():
    """The table itself, written out independently of the source.

    Every other test in this section reads `ALLOWED` to decide what to expect,
    which proves `check_transition` agrees with the table but proves nothing
    about the table. An edge *added* to it -- FUNDED -> PINNED, say, marking a
    job delivered when no bytes ever arrived, and releasable from there -- was
    caught by none of them. This is the one assertion that fails when the
    lifecycle changes, and it is meant to: changing it is how the change gets
    reviewed.
    """
    assert {
        (current, target) for current in JobState for target in ALLOWED[current]
    } == {
        (JobState.QUOTED, JobState.FUNDED),
        (JobState.QUOTED, JobState.ABANDONED),
        (JobState.FUNDED, JobState.RECEIVED),
        (JobState.FUNDED, JobState.REFUNDED),
        (JobState.RECEIVED, JobState.PINNED),
        (JobState.RECEIVED, JobState.REFUNDED),
        (JobState.PINNED, JobState.RELEASED),
        (JobState.PINNED, JobState.REFUNDED),
    }


def test_every_ordered_pair_of_states_is_decided():
    """49 pairs, each either allowed or refused. No pair is undefined."""
    for current, target in itertools.product(JobState, JobState):
        if target in ALLOWED[current]:
            check_transition(current, target)
        else:
            with pytest.raises(IllegalTransition):
                check_transition(current, target)


def test_no_state_transitions_to_itself():
    """A re-entered state would mean a second `mint_job_id` or a second
    release, both of which must be refused rather than silently repeated."""
    for state in JobState:
        assert state not in ALLOWED[state]


def test_terminal_states_have_no_exit():
    for state in TERMINAL:
        assert ALLOWED[state] == frozenset()
    assert TERMINAL == {JobState.RELEASED, JobState.ABANDONED, JobState.REFUNDED}


def test_the_happy_path_is_the_only_route_to_released():
    """Nothing but a verified pin may release. A job that never delivered must
    not be releasable, whatever a caller passes."""
    assert JobState.FUNDED in ALLOWED[JobState.QUOTED]
    assert JobState.RECEIVED in ALLOWED[JobState.FUNDED]
    assert JobState.PINNED in ALLOWED[JobState.RECEIVED]
    assert JobState.RELEASED in ALLOWED[JobState.PINNED]
    reaching = [s for s in JobState if JobState.RELEASED in ALLOWED[s]]
    assert reaching == [JobState.PINNED]


def test_only_an_unfunded_quote_is_abandoned():
    """ABANDONED means "nobody ever paid". Once money is in escrow the exit is
    REFUNDED, which is a different thing: it moves funds."""
    reaching = [s for s in JobState if JobState.ABANDONED in ALLOWED[s]]
    assert reaching == [JobState.QUOTED]


def test_every_funded_state_can_still_refund():
    """The buyer walks away whole from every branch after they have paid."""
    for state in (JobState.FUNDED, JobState.RECEIVED, JobState.PINNED):
        assert JobState.REFUNDED in ALLOWED[state]


def test_a_quote_can_never_be_refunded():
    """Refund moves money out of escrow. A quote nobody paid into holds none,
    so a refund there would be a payment the contract has no source for."""
    assert JobState.REFUNDED not in ALLOWED[JobState.QUOTED]


# --- the store ---------------------------------------------------------


@pytest.fixture
def store(clock):
    return JobStore(":memory:", clock=clock)


def test_a_quote_is_created_unfunded_and_without_a_job_id(store):
    job = store.create_quote(
        agreement_id=1, sha256=SHA_A, size=1024, deadline=2_000, quoted_at=1_000
    )
    assert job.state is JobState.QUOTED
    assert job.job_id is None
    assert store.count_unfunded() == 1


def test_a_quote_is_reused_only_while_unfunded_and_in_window(store):
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=1024, deadline=2_000, quoted_at=1_000
    )
    assert store.find_reusable_quote(SHA_A, 1024, now=1_060, window=120) is not None

    # Out of the window: the sweeper is entitled to reclaim it.
    assert store.find_reusable_quote(SHA_A, 1024, now=1_200, window=120) is None
    # A different size is a different price, so a different agreement.
    assert store.find_reusable_quote(SHA_A, 2048, now=1_060, window=120) is None
    # A different file entirely.
    assert store.find_reusable_quote(SHA_B, 1024, now=1_060, window=120) is None


def test_a_funded_quote_is_never_handed_to_a_second_buyer(store):
    """The seat is sold. A second buyer handed this agreement would build a
    settlement group that fails atomically -- costing them a request for
    nothing."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=1024, deadline=2_000, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=1, job_id="tok")
    assert store.find_reusable_quote(SHA_A, 1024, now=1_060, window=120) is None


def test_minting_a_job_id_funds_the_row_and_is_how_it_is_looked_up(store):
    store.create_quote(
        agreement_id=7, sha256=SHA_B, size=1, deadline=2_000, quoted_at=1_000
    )
    job = store.mint_job_id(agreement_id=7, job_id="tok")
    assert job.state is JobState.FUNDED
    assert store.get_by_job_id("tok") == job
    assert store.count_unfunded() == 0


def test_an_unknown_job_id_or_agreement_is_none(store):
    assert store.get_by_job_id("nope") is None
    assert store.get_by_agreement(999) is None


def test_the_store_refuses_an_illegal_transition(store):
    store.create_quote(
        agreement_id=3, sha256=SHA_A, size=1, deadline=2_000, quoted_at=1_000
    )
    with pytest.raises(IllegalTransition):
        store.transition(3, JobState.RELEASED)
    # And the row is unchanged, not half-moved.
    assert store.get_by_agreement(3).state is JobState.QUOTED


def test_transitioning_a_job_that_does_not_exist_raises(store):
    with pytest.raises(KeyError):
        store.transition(999, JobState.FUNDED)


def test_transitions_persist_their_fields(store):
    store.create_quote(
        agreement_id=4, sha256=SHA_A, size=1, deadline=2_000, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=4, job_id="tok")
    store.transition(4, JobState.RECEIVED)
    store.transition(
        4, JobState.PINNED, cid="bafyrandom", leg2_txid="TX", leg2_last_valid=99
    )
    job = store.get_by_agreement(4)
    assert (job.cid, job.leg2_txid, job.leg2_last_valid) == ("bafyrandom", "TX", 99)
    assert store.in_state(JobState.PINNED) == [job]


def test_a_transition_cannot_set_an_arbitrary_column(store):
    """Only the fields a transition legitimately carries are settable. Letting
    a caller rewrite sha256 or deadline through this door would let a job
    quietly become a different job."""
    store.create_quote(
        agreement_id=5, sha256=SHA_A, size=1, deadline=2_000, quoted_at=1_000
    )
    for bad in ("sha256", "deadline", "size", "state", "quoted_at", "updated_at"):
        with pytest.raises(ValueError, match="not settable"):
            store.transition(5, JobState.FUNDED, **{bad: "x"})

    # `agreement_id` is not in that list because it cannot get that far: it is
    # a positional parameter of `transition`, so passing it as a keyword is a
    # TypeError from Python before any check runs. Different mechanism, same
    # outcome, and worth naming so its absence does not read as an oversight.
    with pytest.raises(TypeError):
        store.transition(5, JobState.FUNDED, agreement_id=6)


def test_count_unfunded_counts_only_quotes(store):
    for agreement_id in (1, 2, 3):
        store.create_quote(
            agreement_id=agreement_id,
            sha256=f"{agreement_id:064x}",
            size=1,
            deadline=2_000,
            quoted_at=1_000,
        )
    assert store.count_unfunded() == 3
    store.mint_job_id(agreement_id=2, job_id="tok")
    assert store.count_unfunded() == 2


# --- find_by_hash: a query, not a fetch --------------------------------


def test_find_by_hash_prefers_the_live_job(store, clock):
    """sha256 is not a key: the same bytes can be pinned many times. The query
    therefore has a policy in it -- the live job if there is one -- which is
    exactly why it is a separate method from get_by_job_id."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=1, job_id="first")
    store.transition(1, JobState.RECEIVED)

    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 1
    assert found.state is JobState.RECEIVED


def test_find_by_hash_falls_back_to_a_recent_terminal_job(store, clock):
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=1, job_id="first")
    for target in (JobState.RECEIVED, JobState.PINNED, JobState.RELEASED):
        store.transition(1, target)

    assert (
        store.find_by_hash(SHA_A, now=clock(), recent_window=3_600, funding_window=120)
        is not None
    )


def test_find_by_hash_forgets_past_the_window(store, clock):
    """Past the window it is a 404 for the caller. That is what stops the
    answer becoming a durability claim about a pin from days ago."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=1, job_id="first")
    for target in (JobState.RECEIVED, JobState.PINNED, JobState.RELEASED):
        store.transition(1, target)

    clock.advance(3_601)
    assert (
        store.find_by_hash(SHA_A, now=clock(), recent_window=3_600, funding_window=120)
        is None
    )


def test_a_live_job_outranks_a_recent_terminal_one(store, clock):
    """An ABANDONED job is still answerable while it is recent -- the caller
    needs to know the previous attempt ended so they can re-quote -- but it
    must never outrank a job that is currently in flight."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=1_000
    )
    store.transition(1, JobState.ABANDONED)
    assert store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    ).state is (JobState.ABANDONED)

    store.create_quote(
        agreement_id=2, sha256=SHA_A, size=8, deadline=2_000, quoted_at=clock()
    )
    store.mint_job_id(agreement_id=2, job_id="second")
    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 2 and found.state is JobState.FUNDED


def test_the_most_advanced_live_job_wins(store, clock):
    """Two live jobs for one hash is reachable, and the caller wants the one
    closest to producing a CID.

    Once buyer A's agreement is funded it stops being reusable, so buyer B
    quoting the same file gets a *new* agreement in QUOTED -- with a higher id
    than A's. Ordering by id alone would answer "there is an unpaid quote"
    while A's job sits at PINNED, seconds from a CID.
    """
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=9_999, quoted_at=1_000
    )
    store.mint_job_id(agreement_id=1, job_id="A")
    store.transition(1, JobState.RECEIVED)
    store.transition(1, JobState.PINNED, cid="bafyreal")

    store.create_quote(
        agreement_id=2, sha256=SHA_A, size=8, deadline=9_999, quoted_at=clock()
    )

    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 1
    assert found.state is JobState.PINNED
    assert found.cid == "bafyreal"


def test_within_one_state_the_newest_job_wins(store, clock):
    """The tiebreak, so the ranking is fully determined rather than left to
    whatever order SQLite happens to return rows in."""
    for agreement_id in (1, 2, 3):
        store.create_quote(
            agreement_id=agreement_id,
            sha256=SHA_A,
            size=8,
            deadline=9_999,
            quoted_at=clock(),
        )
    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 3


def test_an_elapsed_quote_does_not_shadow_a_released_job(store, clock):
    """Buyer A quotes and walks away; buyer B pays for the same bytes and gets
    a CID. The caller holding the bytes must be told about the CID.

    Without a bound on the QUOTED rank, A's row outranks every terminal job
    forever, and the two lookups disagree about it: `find_reusable_quote` calls
    it dead while `find_by_hash` calls it the live one. Task 14's sweeper
    normally abandons it within the window, but an agreement whose `expire`
    keeps failing stays QUOTED indefinitely.
    """
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=9_999, quoted_at=clock()
    )
    clock.advance(200)  # past the 120s funding window
    assert store.find_reusable_quote(SHA_A, 8, now=clock(), window=120) is None

    store.create_quote(
        agreement_id=2, sha256=SHA_A, size=8, deadline=9_999, quoted_at=clock()
    )
    store.mint_job_id(agreement_id=2, job_id="B")
    for target in (JobState.RECEIVED, JobState.PINNED):
        store.transition(2, target)
    store.transition(2, JobState.RELEASED, cid="bafyreal")

    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 2
    assert found.cid == "bafyreal"


def test_an_elapsed_quote_reports_as_nothing_until_it_is_swept(store, clock):
    """Between elapsing and being abandoned it is neither live nor terminal.

    Reporting it would hand the buyer a row still reading QUOTED, which says
    they may pay it. They may not: the funding window closed. Once the sweeper
    moves it to ABANDONED it becomes answerable again, and then the state says
    the true thing.
    """
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=9_999, quoted_at=clock()
    )
    clock.advance(200)
    assert (
        store.find_by_hash(SHA_A, now=clock(), recent_window=3_600, funding_window=120)
        is None
    )

    store.transition(1, JobState.ABANDONED)
    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.state is JobState.ABANDONED


def test_a_quote_is_live_while_it_can_still_be_funded(store, clock):
    """The other side of the bound: inside the window it is the live job, so a
    buyer mid-settlement is not reported as nothing."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=9_999, quoted_at=clock()
    )
    clock.advance(119)
    found = store.find_by_hash(
        SHA_A, now=clock(), recent_window=3_600, funding_window=120
    )
    assert found.agreement_id == 1 and found.state is JobState.QUOTED


def test_the_live_ranking_is_driven_by_the_constant(store, clock):
    """LIVE is ordered most-advanced-first and find_by_hash builds its ORDER BY
    from it, so the two cannot drift. Asserted because the previous version
    documented an order the query did not implement."""
    from api.jobs import LIVE

    assert LIVE == (
        JobState.PINNED,
        JobState.RECEIVED,
        JobState.FUNDED,
        JobState.QUOTED,
    )
    assert set(LIVE) | TERMINAL == set(JobState)
    assert not set(LIVE) & TERMINAL


def test_find_by_hash_does_not_match_a_different_file(store, clock):
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=clock()
    )
    assert (
        store.find_by_hash(SHA_B, now=clock(), recent_window=3_600, funding_window=120)
        is None
    )


def test_find_by_hash_ignores_size(store, clock):
    """Unlike the quote lookup, which is keyed on (sha256, size) because size
    is part of the price. The status query answers "is anything happening to
    these bytes", and the caller asking has the bytes, not the quote."""
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=8, deadline=2_000, quoted_at=clock()
    )
    assert (
        store.find_by_hash(SHA_A, now=clock(), recent_window=3_600, funding_window=120)
        is not None
    )


def test_timestamps_are_stored_as_whole_seconds(store, clock):
    """A caller passing `time.time()` would otherwise put a float in an INTEGER
    column, and get a float back in `Job`. Every window in this system is a
    comparison against one of these, and mixed types work right up until a
    boundary lands exactly on one."""
    job = store.create_quote(
        agreement_id=1,
        sha256=SHA_A,
        size=8.0,
        deadline=2_000.75,
        quoted_at=1_000.4,
    )
    assert isinstance(job.quoted_at, int) and job.quoted_at == 1_000
    assert isinstance(job.deadline, int) and job.deadline == 2_000
    assert isinstance(job.size, int) and job.size == 8
    assert isinstance(job.updated_at, int)

    # And after a round trip through SQLite, not just in the returned object.
    reloaded = store.get_by_agreement(1)
    assert isinstance(reloaded.quoted_at, int)
    assert isinstance(reloaded.deadline, int)


def test_updated_at_is_a_whole_second_even_from_a_float_clock(tmp_path):
    """The default clock is `time.time`, which returns a float."""
    store = JobStore(str(tmp_path / "j.sqlite3"), clock=lambda: 1_000.9)
    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=1, deadline=2_000, quoted_at=1_000
    )
    job = store.mint_job_id(agreement_id=1, job_id="tok")
    assert isinstance(job.updated_at, int) and job.updated_at == 1_000
    store.close()


# --- durability --------------------------------------------------------


def test_the_store_survives_a_reopen(tmp_path):
    """SQLite rather than memory precisely because of this: losing the row
    loses the map from jobId to agreement, and with it a funded buyer's ability
    to upload against money already in escrow."""
    path = str(tmp_path / "jobs.sqlite3")
    first = JobStore(path)
    first.create_quote(
        agreement_id=9, sha256=SHA_A, size=5, deadline=2_000, quoted_at=1_000
    )
    first.mint_job_id(agreement_id=9, job_id="tok")
    first.close()

    second = JobStore(path)
    job = second.get_by_job_id("tok")
    assert job is not None
    assert job.agreement_id == 9
    assert job.state is JobState.FUNDED
    second.close()


def test_two_quotes_cannot_share_an_agreement_id(store):
    """agreement_id is the primary key: it is the chain's identifier for the
    row, and two rows claiming one agreement would mean two jobs against one
    escrowed payment."""
    import sqlite3

    store.create_quote(
        agreement_id=1, sha256=SHA_A, size=1, deadline=2_000, quoted_at=1_000
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.create_quote(
            agreement_id=1, sha256=SHA_B, size=1, deadline=2_000, quoted_at=1_000
        )


def test_two_jobs_cannot_share_a_job_id(store):
    import sqlite3

    for agreement_id in (1, 2):
        store.create_quote(
            agreement_id=agreement_id,
            sha256=f"{agreement_id:064x}",
            size=1,
            deadline=2_000,
            quoted_at=1_000,
        )
    store.mint_job_id(agreement_id=1, job_id="tok")
    with pytest.raises(sqlite3.IntegrityError):
        store.mint_job_id(agreement_id=2, job_id="tok")


def test_a_failed_write_leaves_no_transaction_open(store):
    """sqlite3 opens a transaction implicitly before DML, and a statement that
    raises rolls back the statement but not the transaction.

    Left open, it holds the write lock until some later call happens to commit.
    Inside one process that only costs a stale read snapshot, so it hides; the
    moment a second connection touches the file it is `database is locked`,
    raised seconds later and nowhere near the UNIQUE violation that caused it.
    """
    import sqlite3

    for agreement_id in (1, 2):
        store.create_quote(
            agreement_id=agreement_id,
            sha256=f"{agreement_id:064x}",
            size=1,
            deadline=2_000,
            quoted_at=1_000,
        )
    store.mint_job_id(agreement_id=1, job_id="tok")
    with pytest.raises(sqlite3.IntegrityError):
        store.mint_job_id(agreement_id=2, job_id="tok")

    assert store._conn.in_transaction is False
    # And the refused write left the row alone, as a refused transition does.
    assert store.get_by_agreement(2).state is JobState.QUOTED
    assert store.get_by_agreement(2).job_id is None
