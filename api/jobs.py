"""The job lifecycle and its store.

A quote and a job are **one row at two ages**: the row is created at the 402 in
`QUOTED`, keyed by `agreement_id`, and its `job_id` is minted at the 200 that
confirms settlement. That is what makes the idempotency lookup and the
abandoned-quote sweep the same query, and it is why a `jobId` is never carried
in a 402 -- two buyers quoting the same file are handed the same agreement, so
anything in that 402 is shared with a stranger.

SQLite rather than memory, because losing this table on a restart loses the map
from `jobId` to agreement, and with it a funded buyer's ability to upload
against money that is already in escrow. The chain stays the source of truth for
the money; this is the index into it.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum


class JobState(str, Enum):
    QUOTED = "QUOTED"
    FUNDED = "FUNDED"
    RECEIVED = "RECEIVED"
    PINNED = "PINNED"
    RELEASED = "RELEASED"
    ABANDONED = "ABANDONED"
    REFUNDED = "REFUNDED"


# The contract state each corresponds to: QUOTED is OPEN with zero seats;
# FUNDED, RECEIVED and PINNED are all FUNDED on chain and differ only in what
# the backend is waiting for; the last three are terminal.
#
# Written as the whole relation rather than as a list of legal moves, so that
# "is this transition allowed" has an answer for all 49 pairs and the illegal
# ones are refused by construction rather than by omission.
ALLOWED: dict[JobState, frozenset[JobState]] = {
    # Nobody has paid: the quote is either taken up or reclaimed. It can never
    # be refunded -- there is nothing in escrow to refund.
    JobState.QUOTED: frozenset({JobState.FUNDED, JobState.ABANDONED}),
    # Paid, waiting on bytes. The deadline can still refund.
    JobState.FUNDED: frozenset({JobState.RECEIVED, JobState.REFUNDED}),
    # Bytes accepted, leg 2 outstanding. A rejected upload never enters
    # RECEIVED at all, because the commit_hash gate runs first.
    JobState.RECEIVED: frozenset({JobState.PINNED, JobState.REFUNDED}),
    # Pinned. Gateway verification can still fail, and then the buyer is
    # refunded and the leg-2 fee is the operator's priced risk.
    JobState.PINNED: frozenset({JobState.RELEASED, JobState.REFUNDED}),
    JobState.RELEASED: frozenset(),
    JobState.ABANDONED: frozenset(),
    JobState.REFUNDED: frozenset(),
}

TERMINAL = frozenset({JobState.RELEASED, JobState.ABANDONED, JobState.REFUNDED})

# The states in which a job is still in flight, **most advanced first**, and
# that order is load-bearing: `find_by_hash` builds its ORDER BY from this
# tuple, so the constant decides the preference rather than merely describing
# it.
#
# Most advanced wins because two live jobs for one hash is reachable and the
# caller wants the one closest to producing a CID. Once buyer A's agreement is
# funded it stops being reusable, so buyer B quoting the same file gets a new
# agreement in QUOTED -- with a *higher* id than A's. Ordering by id alone would
# answer "there is an unpaid quote" while A's job sits at PINNED, seconds from a
# CID.
LIVE = (JobState.PINNED, JobState.RECEIVED, JobState.FUNDED, JobState.QUOTED)


class IllegalTransition(RuntimeError):
    """A move the lifecycle does not have."""


def check_transition(current: JobState, target: JobState) -> None:
    if target not in ALLOWED[current]:
        raise IllegalTransition(
            f"{current.value} -> {target.value} is not a transition"
        )


@dataclass(frozen=True)
class Job:
    agreement_id: int
    sha256: str
    size: int
    state: JobState
    quoted_at: int
    updated_at: int
    deadline: int
    job_id: str | None = None
    leg2_txid: str | None = None
    leg2_last_valid: int | None = None
    cid: str | None = None
    error: str | None = None


_COLUMNS = (
    "agreement_id, sha256, size, state, quoted_at, updated_at, deadline, job_id, "
    "leg2_txid, leg2_last_valid, cid, error"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    agreement_id     INTEGER PRIMARY KEY,
    sha256           TEXT    NOT NULL,
    size             INTEGER NOT NULL,
    state            TEXT    NOT NULL,
    quoted_at        INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL,
    deadline         INTEGER NOT NULL,
    job_id           TEXT    UNIQUE,
    leg2_txid        TEXT,
    leg2_last_valid  INTEGER,
    cid              TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS jobs_quote ON jobs (sha256, size, state);
CREATE INDEX IF NOT EXISTS jobs_hash ON jobs (sha256, updated_at);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs (state);

-- An agreement is created on chain *before* its job row can name it, because
-- the id comes back from the create. Anything that interrupts the gap between
-- those two writes -- a cancelled request, a crash, a redeploy -- leaves an
-- agreement holding minimum balance that no row in `jobs` refers to, and
-- therefore one that nothing enumerating `jobs` can ever find or reclaim.
--
-- This table closes the gap by recording the *intent* first. A row here means
-- "an agreement with about this id may exist on chain and may not be in
-- `jobs` yet". It is written before the create and deleted once the job row
-- is safely written, so anything left behind is a reconciliation task rather
-- than a leak. Kept separate from `jobs` deliberately: a provisional row in
-- `jobs` would be visible to find_reusable_quote and could be handed to a
-- second buyer as an agreement that does not exist on chain yet.
--
-- `agreement_id` is the *predicted* id (agreement_count + 1 as read under the
-- creation lock), so it is a reliable hint rather than a guarantee: another
-- process creating concurrently can shift the real id. Reconciliation must
-- therefore treat it as a starting point and confirm against chain state.
CREATE TABLE IF NOT EXISTS create_intents (
    agreement_id INTEGER PRIMARY KEY,
    sha256       TEXT    NOT NULL,
    size         INTEGER NOT NULL,
    requested_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS create_intents_age ON create_intents (requested_at);
"""

# What a transition is allowed to carry. Everything else about a job -- the
# hash, the size, the deadline, the agreement it belongs to -- is decided when
# the row is created and must not be rewritable through this door, or a job
# could quietly become a different job.
_MUTABLE = frozenset({"job_id", "leg2_txid", "leg2_last_valid", "cid", "error"})


class JobStore:
    def __init__(self, path: str, *, clock=time.time) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Stamped on every transition, so "recently released" is a fact the
        # store holds rather than one every caller has to remember.
        self._clock = clock
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _row(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        data = dict(row)
        data["state"] = JobState(data["state"])
        return Job(**data)

    def _write(self, sql: str, parameters: tuple) -> None:
        """Run one DML statement and commit it, or roll back and re-raise.

        The rollback is the point. `sqlite3` opens a transaction implicitly
        before DML, and a statement that raises rolls back the *statement*
        while leaving that transaction open -- holding the write lock until
        some later call happens to commit. Within one process that self-heals
        and only costs a stale read snapshot; the moment a second connection
        touches the file it becomes `database is locked`, raised far from the
        UNIQUE violation that actually caused it.

        Call with the lock held.
        """
        try:
            self._conn.execute(sql, parameters)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _locked_get(self, agreement_id: int) -> Job | None:
        """Read a row with the lock already held.

        Writers return through this rather than through `get_by_agreement`, so
        the `Job` handed back is the row the call wrote and not whatever a
        writer that took the lock in between has since made of it.
        """
        row = self._conn.execute(
            f"SELECT {_COLUMNS} FROM jobs WHERE agreement_id = ?", (int(agreement_id),)
        ).fetchone()
        return self._row(row)

    def create_quote(
        self,
        *,
        agreement_id: int,
        sha256: str,
        size: int,
        deadline: int,
        quoted_at: int,
    ) -> Job:
        """Create the row in QUOTED. Timestamps are whole seconds.

        Coerced here rather than trusted, because SQLite stores whatever it is
        handed: a caller passing `time.time()` would put a float in an INTEGER
        column, and it would come back as a float in `Job` too. Every window in
        this system is a comparison against one of these, and a mix of floats
        and ints is the kind of thing that works until a boundary lands exactly
        on it.
        """
        quoted_at = int(quoted_at)
        with self._lock:
            self._write(
                "INSERT INTO jobs (agreement_id, sha256, size, state, quoted_at, "
                "updated_at, deadline) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(agreement_id),
                    sha256,
                    int(size),
                    JobState.QUOTED.value,
                    quoted_at,
                    quoted_at,
                    int(deadline),
                ),
            )
            return self._locked_get(agreement_id)  # type: ignore[return-value]

    def find_reusable_quote(
        self, sha256: str, size: int, *, now: int, window: int
    ) -> Job | None:
        """An agreement is reused only while it is unfunded and in window.

        Keyed on `(sha256, size)` because size is part of the price, so two
        quotes for the same bytes at different declared sizes are different
        purchases. A funded or elapsed agreement is never handed to a second
        buyer: the first bought the one seat, and past the window the sweeper
        is entitled to reclaim it.
        """
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE sha256 = ? AND size = ? "
                "AND state = ? AND quoted_at > ? ORDER BY agreement_id LIMIT 1",
                (sha256, size, JobState.QUOTED.value, now - window),
            ).fetchone()
        return self._row(row)

    def find_by_hash(
        self, sha256: str, *, now: int, recent_window: int, funding_window: int
    ) -> Job | None:
        """The one job these bytes should be reported as, or None.

        A query rather than a fetch, because `sha256` is not a key: the same
        bytes can be pinned many times, by different buyers, on different days.
        The policy is live first, then the most recently updated terminal job
        inside `recent_window`, and nothing at all past it -- so an old pin is
        never reported as still covering anyone.

        **An unfunded quote is live only while it could still be funded.** Past
        `funding_window` it stops counting as live, for the same reason
        `find_reusable_quote` stops handing it out: nobody can pay it any more.
        Without that bound the two lookups disagree about one row -- the reuse
        query calls it dead while this one calls it the live job -- and a quote
        the sweeper has not reached yet outranks, forever, a job that released
        seconds ago with a real CID. That is the one answer this route exists to
        give, so the stale quote must not shadow it.

        Between elapsing and being swept to ABANDONED such a row is neither
        live nor terminal, and reports as nothing. That is deliberate: its state
        still reads QUOTED, which would tell the buyer they may still pay it.

        Size is deliberately not part of this lookup. The caller asking holds
        the bytes, not the quote, and the question is "is anything happening to
        these bytes" rather than "may I reuse this agreement".
        """
        live = tuple(state.value for state in LIVE)
        # Rank by how far the job has progressed, built from LIVE so that the
        # constant and the behaviour cannot disagree. agreement_id breaks ties
        # within one state, newest first.
        ranking = " ".join(f"WHEN ? THEN {position}" for position in range(len(live)))
        # Sorted for a deterministic parameter order; the set itself is what
        # decides membership, so the two branches stay jointly exhaustive.
        terminal = tuple(sorted(state.value for state in TERMINAL))
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE sha256 = ? "
                f"AND state IN ({','.join('?' * len(live))}) "
                "AND (state != ? OR quoted_at > ?) "
                f"ORDER BY CASE state {ranking} END, agreement_id DESC LIMIT 1",
                (sha256, *live, JobState.QUOTED.value, now - funding_window, *live),
            ).fetchone()
            if row is None:
                # Filtered to TERMINAL rather than left open, so an elapsed
                # quote cannot arrive here and be reported as a finished job.
                row = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM jobs WHERE sha256 = ? "
                    f"AND state IN ({','.join('?' * len(terminal))}) "
                    "AND updated_at > ? "
                    "ORDER BY updated_at DESC, agreement_id DESC LIMIT 1",
                    (sha256, *terminal, now - recent_window),
                ).fetchone()
        return self._row(row)

    def get_by_agreement(self, agreement_id: int) -> Job | None:
        with self._lock:
            return self._locked_get(agreement_id)

    def get_by_job_id(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row(row)

    def mint_job_id(self, *, agreement_id: int, job_id: str) -> Job:
        """Fund the row and give it the token the buyer will upload against."""
        return self.transition(agreement_id, JobState.FUNDED, job_id=job_id)

    def transition(self, agreement_id: int, target: JobState, **fields) -> Job:
        unknown = set(fields) - _MUTABLE
        if unknown:
            raise ValueError(f"not settable on a transition: {sorted(unknown)}")
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM jobs WHERE agreement_id = ?", (agreement_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no job for agreement {agreement_id}")
            # Checked before the UPDATE, so a refused transition leaves the row
            # exactly as it was rather than half-moved.
            check_transition(JobState(row["state"]), target)
            assignments = "".join(f", {name} = ?" for name in fields)
            self._write(
                f"UPDATE jobs SET state = ?, updated_at = ?{assignments} "
                "WHERE agreement_id = ?",
                (target.value, int(self._clock()), *fields.values(), agreement_id),
            )
            return self._locked_get(agreement_id)  # type: ignore[return-value]

    def in_state(self, state: JobState) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE state = ? ORDER BY agreement_id",
                (state.value,),
            ).fetchall()
        return [self._row(row) for row in rows]  # type: ignore[misc]

    def count_unfunded(self) -> int:
        """How many agreements are parked holding an ALGO deposit.

        The ceiling this feeds is about the capacity to quote, not about loss:
        every quote locks minimum balance until it is funded or swept.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE state = ?",
                (JobState.QUOTED.value,),
            ).fetchone()
        return int(row["n"])

    # -- Creation intents ---------------------------------------------------
    #
    # The three calls that make an interrupted create reconcilable instead of
    # invisible. See the create_intents comment in _SCHEMA for why the record
    # is written before the on-chain call rather than after it.

    def record_create_intent(
        self, *, agreement_id: int, sha256: str, size: int, requested_at: int
    ) -> None:
        """Note that an agreement is about to be created. Call before creating.

        INSERT OR REPLACE rather than INSERT: a predicted id can repeat after
        an earlier intent for the same id was left behind unreconciled, and
        refusing the write here would refuse the *quote* over a bookkeeping
        row. The reconciliation action is identical either way, because it is
        keyed on the id.
        """
        with self._lock:
            self._write(
                "INSERT OR REPLACE INTO create_intents "
                "(agreement_id, sha256, size, requested_at) VALUES (?, ?, ?, ?)",
                (int(agreement_id), sha256, int(size), int(requested_at)),
            )

    def clear_create_intent(self, agreement_id: int) -> None:
        """Drop the intent once the job row that supersedes it is written."""
        with self._lock:
            self._write(
                "DELETE FROM create_intents WHERE agreement_id = ?",
                (int(agreement_id),),
            )

    def unreconciled_create_intents(self, *, older_than: int) -> list[dict]:
        """Intents with no job row, older than a timestamp: possible orphans.

        The LEFT JOIN is what makes this cheap to act on -- an intent whose
        job row exists was simply not cleared (a crash between the two writes)
        and needs no chain lookup, only deleting. What comes back here is the
        set that has to be settled against chain state.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT i.agreement_id, i.sha256, i.size, i.requested_at "
                "FROM create_intents i LEFT JOIN jobs j "
                "ON j.agreement_id = i.agreement_id "
                "WHERE j.agreement_id IS NULL AND i.requested_at <= ? "
                "ORDER BY i.agreement_id",
                (int(older_than),),
            ).fetchall()
        return [dict(row) for row in rows]
