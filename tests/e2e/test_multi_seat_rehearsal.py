"""S5-S7 -- a five-seat pool, filled, and taken to each of its three ends.

`test_index_pool_seat.py` buys one seat of two and refunds it. That proves a
multi-seat agreement can be quoted, paid and refunded at all. It cannot prove
the two things that only appear at size, and both of them are arithmetic the
first real pool will depend on:

  - **the fee reserve is exact.** It is sized `max_seats + 1` inner
    transactions. Only a pool that actually spends all of them shows whether
    that is right, and only `close` can say so -- it returns a *computed*
    figure, the box minimum balance plus the unspent remainder, never whatever
    was deposited. An under-funded reserve makes `close` draw on another
    agreement's deposit; an over-funded one strands ALGO with no path out.
  - **`release_hash` on a pool.** It pays `total_held` in one transfer however
    many seats funded it, and no multi-seat agreement has ever been released
    anywhere but LocalNet.

Three scenarios, one per terminal state, and each one closes what it opened:

  S5  five seats fill, the verifier releases      -> RELEASED, close returns 161_800
  S6  three of five fill, the deadline passes     -> REFUNDED, close returns 159_800
  S7  five seats fill, nobody releases            -> REFUNDED, close returns 157_800

S7 is the one that carries the finding. Five refunds plus `close`'s own
payment is six inner transactions against a six-transaction reserve, so what
comes back is the box minimum balance and nothing else.

Every exit call is sent **as a buyer**, never as the admin. A buyer whose
counterparty walked away must have a way out without the operator's help, and
that property is worth re-proving at each of the three terminal states rather
than once.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from api.escrow import (
    CONDITION_HASH,
    STATE_EXPIRED,
    STATE_FUNDED,
    STATE_OPEN,
    STATE_REFUNDED,
    STATE_REFUNDING,
    STATE_RELEASED,
    required_deposit,
)
from api.pool import find_any_open_pool
from tests.e2e.events import events_for, one_event
from tests.e2e.harness import (
    REJECTED,
    REPO_ROOT,
    ZERO_ADDRESS,
    algo_balance,
    algod_url_from,
    usdc_balance,
    wait_past_deadline,
)
from tests.e2e.pool import (
    FILL_DEADLINE_SECONDS,
    POOL_SEATS,
    POOL_SHARE_PRICE,
    close_return,
    fill,
    inner_transfers,
    pool_server,
)

pytestmark = pytest.mark.testnet

# One inner transaction per release or refund, plus the payment `close` itself
# sends. The reserve is sized max_seats + 1, so only the five-refund case
# spends it to zero.
INNER_USED_RELEASED = 2
INNER_USED_REFUNDED_THREE = 4
INNER_USED_REFUNDED_FIVE = 6

EXPIRED_BY_DEADLINE = 0


def _commitment() -> bytes:
    """A throwaway digest standing in for a frozen edition's sha256.

    The bytes behind it are never produced and never need to be:
    `release_hash` compares two 32-byte values, and both of them come from
    here. A real edition's manifest is the one committed under editions/;
    what is under test is the release path, not the digest.
    """
    return hashlib.sha256(secrets.token_bytes(64)).digest()


def _manifest(tmp_path: Path, commit_hash: bytes, *, edition: int = 1) -> Path:
    """The manifest `agents/create_pool.py` consumes.

    Written under tmp_path rather than editions/: the digest is random, and a
    manifest left there would read as a real frozen edition to whoever found
    it next.
    """
    path = tmp_path / f"edition-{edition}-manifest.json"
    path.write_text(
        json.dumps({"edition": edition, "sha256": commit_hash.hex()}),
        encoding="utf-8",
    )
    return path


def _run_create_pool(
    e2e_env, manifest: Path, *, deadline_days: int, dry_run: bool
) -> tuple[int | None, dict, str]:
    """Run the operator's own script as a subprocess and read its report.

    A subprocess rather than importing `main(argv)`: `agents/create_pool.py`
    calls `load_dotenv()` at import and reads `os.environ` through
    `load_settings`, so importing it here would fold .env into this process's
    environment for every scenario afterwards. It is also the only way to
    exercise the path the operator actually runs.

    ALGOD_URL is set explicitly because `load_settings` reads that name and
    only that name, while this suite's `algod_url_from` also accepts
    ALGOD_TESTNET_URL -- so a .env carrying only the second would hand
    create_pool an AlgodClient pointed at "".
    """
    argv = [
        sys.executable,
        "-m",
        "agents.create_pool",
        "--manifest",
        str(manifest),
        "--deadline-days",
        str(deadline_days),
    ]
    if dry_run:
        argv.append("--dry-run")
    result = subprocess.run(
        argv,
        check=False,  # the assertion below reports stdout and stderr
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "ALGOD_URL": algod_url_from(e2e_env),
            "INDEX_SEATS": str(POOL_SEATS),
            "INDEX_SHARE_PRICE_MICRO": str(POOL_SHARE_PRICE),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"create_pool exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    report = json.loads(
        result.stdout[result.stdout.index("{") : result.stdout.rindex("}") + 1]
    )
    agreement_id = None
    for line in result.stdout.splitlines():
        if line.startswith("created agreement "):
            agreement_id = int(line.rsplit(" ", 1)[1])
    return agreement_id, report, result.stdout


def _create_directly(
    as_admin, *, treasury, verifier, commit_hash, algod
) -> tuple[int, int]:
    """A pool with a deadline measured in minutes, not days.

    `create_pool.py --deadline-days` takes whole days and cannot express the
    window an expiry scenario has to wait out; `--deadline-days 0` would set
    the deadline to now, which is not joinable at all. So the two expiry
    scenarios go straight to the client, which is what
    `test_index_pool_seat._open_pool` does and for the same reason.
    """
    deadline = int(time.time()) + FILL_DEADLINE_SECONDS
    agreement_id = as_admin.create_hash_agreement(
        commit_hash=commit_hash,
        share_price=POOL_SHARE_PRICE,
        deadline=deadline,
        beneficiary=treasury.address,
        verifier=verifier.address,
        seats=POOL_SEATS,
        note=commit_hash.hex().encode(),
    )
    return agreement_id, deadline


def _payers(buyer, seat_buyers, count):
    """The buyer plus however many throwaway seats the scenario needs.

    The buyer stays on the roster so the permissionless-exit assertions are
    made on the same identity `test_hash_refund.py` already holds them on.
    """
    payers = [buyer, *seat_buyers(POOL_SEATS - 1, POOL_SHARE_PRICE)][:count]
    assert len({p.address for p in payers}) == count, (
        "`join` refuses a payer already on the roster, so every seat needs a "
        f"distinct key; got {[p.address for p in payers]}"
    )
    return payers


def test_a_five_seat_pool_fills_releases_and_closes(
    e2e_env,
    settings,
    algod,
    as_admin,
    as_verifier,
    as_buyer,
    admin,
    buyer,
    treasury,
    verifier,
    job_db_path,
    seat_buyers,
    tmp_path,
):
    """S5 -- the path day 3 runs for real, driven by the operator's script.

    Created through `agents/create_pool.py` rather than through the client,
    because the script is what an operator will actually type and its dry run
    is the last gate before a commitment that cannot be rebound.
    """
    asset_id = settings.usdc_asa_id
    min_fee = as_admin.min_fee()
    commit_hash = _commitment()
    manifest = _manifest(tmp_path, commit_hash)

    assert find_any_open_pool(as_admin) is None, (
        "a multi-seat pool is already open on this application, so "
        "create_pool.py will refuse. Drain it first: "
        "`python -m agents.pool_ops drain --agreement-id <id> --wait`"
    )

    app_algo_before = algo_balance(algod, settings.app_account)
    app_usdc_before = usdc_balance(algod, settings.app_account, asset_id)
    treasury_usdc_before = usdc_balance(algod, treasury.address, asset_id)
    next_id_before = as_admin.next_agreement_id()

    # -- the dry run creates nothing -------------------------------------
    dry_id, report, stdout = _run_create_pool(
        e2e_env, manifest, deadline_days=1, dry_run=True
    )
    assert dry_id is None
    assert "dry run: nothing created" in stdout
    deadline_reported = report.pop("deadline")
    assert report == {
        "edition": 1,
        "commit_sha256": commit_hash.hex(),
        "seats": POOL_SEATS,
        "share_price_micro": POOL_SHARE_PRICE,
        "pool_value_micro": POOL_SEATS * POOL_SHARE_PRICE,
        "deadline_days": 1,
        "beneficiary": treasury.address,
        "verifier": verifier.address,
        "required_deposit_micro_algo": required_deposit(POOL_SEATS, min_fee),
    }
    assert 86_000 < deadline_reported - int(time.time()) <= 86_400
    assert as_admin.next_agreement_id() == next_id_before, (
        "the dry run must create nothing; it reported a pool it did not open"
    )

    # -- the real create --------------------------------------------------
    agreement_id, _report, _stdout = _run_create_pool(
        e2e_env, manifest, deadline_days=1, dry_run=False
    )
    assert agreement_id == next_id_before, (
        f"create_pool reported agreement {agreement_id} but the next id was "
        f"{next_id_before}; another session raced this create"
    )

    record = as_admin.read_agreement(agreement_id)
    assert record is not None
    assert record.condition == CONDITION_HASH
    assert record.state == STATE_OPEN
    assert (record.min_seats, record.max_seats, record.seats) == (
        POOL_SEATS,
        POOL_SEATS,
        0,
    )
    assert record.share_price == POOL_SHARE_PRICE
    assert record.commit_hash == commit_hash
    assert record.beneficiary == treasury.address
    assert record.verifier == verifier.address
    assert record.creator == admin.address
    assert (record.total_held, record.refund_cursor, record.unclaimed_seats) == (
        0,
        0,
        0,
    )
    assert as_admin.read_roster(agreement_id) == [(ZERO_ADDRESS, 0)] * POOL_SEATS, (
        "the roster box is pre-sized to max_seats at creation, so it is five "
        "empty seats rather than an empty list"
    )
    assert algo_balance(
        algod, settings.app_account
    ) == app_algo_before + required_deposit(POOL_SEATS, min_fee), (
        "the deposit is exact in both directions -- an overpayment is rejected too"
    )

    payers = _payers(buyer, seat_buyers, POOL_SEATS)
    usdc_before = {p.address: usdc_balance(algod, p.address, asset_id) for p in payers}

    # -- five settlements through the real facilitator --------------------
    with pool_server(e2e_env, algod, admin, job_db_path, agreement_id) as base_url:
        url = f"{base_url}/index"
        with httpx.Client(timeout=120.0) as http:
            seats = fill(
                http,
                url,
                payers=payers,
                agreement_id=agreement_id,
                commit_hash=commit_hash,
                settings=settings,
                algod=algod,
                reader=as_admin,
            )
            # The route closes itself the moment the pool fills: the pinned
            # pool must be OPEN to be quoted, so a sixth buyer is refused by
            # the server rather than by the contract.
            assert http.post(url).status_code == 503

    assert [s.index for s in seats] == list(range(POOL_SEATS))

    # The seat ceiling of 20 is a measured opcode-budget limit, not a policy
    # number: `join` scans the roster for a repeated payer, so it costs more
    # with every seat already taken. Five samples give the per-seat slope and
    # an extrapolation to the ceiling. Reported rather than asserted -- a
    # budget figure is a finding, and a scenario that went red on it would be
    # asserting the compiler's output rather than the contract's behaviour.
    # Every join has to have been simulated for the slope to mean anything.
    # `opcode_budget` returns None rather than failing the scenario when
    # simulate declines, and a gap read as contiguous would double the
    # apparent slope and move the reported wall from seat 26 to seat 13 --
    # printed with exactly as much confidence as the right answer.
    measured = [(s.index, s.budget) for s in seats if s.budget]
    complete = [index for index, _ in measured] == list(range(len(seats)))
    if complete and len(measured) >= 3:
        available = measured[-1][1][1]
        print("\n  join opcode budget by roster depth (consumed/available):")
        for index, (consumed, _) in measured:
            print(f"    seat {index}: {consumed}/{available}")

        # The last seat fills the pool and takes a branch none of the others
        # do -- it emits `Filled` and moves the state -- so its step is not the
        # roster-scan slope and must not be averaged into it. Extrapolating on
        # the inflated figure overstates the cost at the ceiling by about 12%.
        steps = [
            measured[i + 1][1][0] - measured[i][1][0] for i in range(len(measured) - 2)
        ]
        slope = sum(steps) / len(steps)
        base = measured[0][1][0]
        surcharge = (measured[-1][1][0] - measured[-2][1][0]) - slope
        wall = next(n for n in range(1000) if base + slope * n > available)
        print(
            f"    {slope:.0f} per seat scanned, +{surcharge:.0f} once more on "
            f"the seat that fills the pool"
        )
        print(
            f"    a {POOL_SEATS}-seat pool peaks at {measured[-1][1][0]}/"
            f"{available} ({measured[-1][1][0] / available:.0%})"
        )
        print(
            f"    a 20-seat pool would peak at "
            f"{base + slope * 19 + surcharge:.0f}/{available}; the budget is "
            f"first exceeded at seat {wall}"
        )
    else:
        # Silence here would look identical to a healthy run that simply had
        # nothing to say, so a permanently broken probe would go unnoticed.
        print()
        print(
            f"  join opcode budget not reported: {len(measured)} of "
            f"{len(seats)} joins simulated"
            f"{'' if complete else ', and not consecutively'} -- too few to "
            "extrapolate a per-seat slope from"
        )

    funded = as_admin.read_agreement(agreement_id)
    assert funded.state == STATE_FUNDED, (
        "a filled `hash` pool lands in FUNDED, not FILLED: FILLED releases "
        "permissionlessly on seat count, and this one must not"
    )
    assert funded.seats == POOL_SEATS
    assert funded.total_held == POOL_SEATS * POOL_SHARE_PRICE
    assert funded.unclaimed_seats == 0
    assert as_admin.read_roster(agreement_id) == [
        (p.address, POOL_SHARE_PRICE) for p in payers
    ]
    assert usdc_balance(algod, settings.app_account, asset_id) == (
        app_usdc_before + POOL_SEATS * POOL_SHARE_PRICE
    )
    for p in payers:
        assert (
            usdc_balance(algod, p.address, asset_id)
            == usdc_before[p.address] - POOL_SHARE_PRICE
        )

    # -- release, and the two refusals that make it mean something --------
    with pytest.raises(REJECTED):
        as_verifier.release_hash(agreement_id, secrets.token_bytes(32))
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED, (
        "bytes that do not match the commitment must not release the pool"
    )
    with pytest.raises(REJECTED):
        as_buyer.release_hash(agreement_id, commit_hash)
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED, (
        "only the agreement's own verifier may release, even with the right "
        "digest -- otherwise the release condition is a promise"
    )

    released_txid = as_verifier.release_hash(agreement_id, commit_hash)
    released = one_event(events_for(algod, released_txid), "Released")
    assert released["beneficiary"] == treasury.address
    assert released["amount"] == POOL_SEATS * POOL_SHARE_PRICE
    assert bytes(released["proof"]) == commit_hash
    assert inner_transfers(algod, released_txid) == [
        (treasury.address, POOL_SEATS * POOL_SHARE_PRICE)
    ], "five seats leave in one transfer, not five"

    after = as_admin.read_agreement(agreement_id)
    assert after.state == STATE_RELEASED
    assert after.total_held == 0
    assert usdc_balance(algod, treasury.address, asset_id) == (
        treasury_usdc_before + POOL_SEATS * POOL_SHARE_PRICE
    )
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before

    # -- close, sent by a buyer -------------------------------------------
    admin_algo_before_close = algo_balance(algod, admin.address)
    close_txid = as_buyer.close(agreement_id)
    closed = one_event(events_for(algod, close_txid), "Closed")
    assert closed["algo_returned"] == close_return(
        POOL_SEATS, min_fee, inner_used=INNER_USED_RELEASED
    )
    assert closed["algo_returned"] == 161_800
    assert algo_balance(algod, admin.address) == (
        admin_algo_before_close + closed["algo_returned"]
    ), "the deposit goes back to the creator, not to whoever called close"

    assert as_admin.read_agreement(agreement_id) is None
    assert as_admin.read_roster(agreement_id) == []
    assert algo_balance(algod, settings.app_account) == app_algo_before, (
        "conservation: the deposit in equals the inner fees plus the close "
        "payment out, exactly"
    )
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before
    assert find_any_open_pool(as_admin) is None


def test_a_partly_filled_pool_refunds_only_the_seats_it_sold(
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
    seat_buyers,
):
    """S6 -- the ordinary outcome of a pool that did not attract its quota.

    Three of five, then the deadline. Not an edge case: an edition that misses
    its threshold refunds everyone, and that is correct behaviour rather than
    a failure.
    """
    asset_id = settings.usdc_asa_id
    min_fee = as_admin.min_fee()
    commit_hash = _commitment()
    sold = 3

    app_algo_before = algo_balance(algod, settings.app_account)
    app_usdc_before = usdc_balance(algod, settings.app_account, asset_id)
    treasury_usdc_before = usdc_balance(algod, treasury.address, asset_id)

    agreement_id, deadline = _create_directly(
        as_admin,
        treasury=treasury,
        verifier=verifier,
        commit_hash=commit_hash,
        algod=algod,
    )
    payers = _payers(buyer, seat_buyers, sold)
    usdc_before = {p.address: usdc_balance(algod, p.address, asset_id) for p in payers}

    with pool_server(e2e_env, algod, admin, job_db_path, agreement_id) as base_url:
        url = f"{base_url}/index"
        with httpx.Client(timeout=120.0) as http:
            fill(
                http,
                url,
                payers=payers,
                agreement_id=agreement_id,
                commit_hash=commit_hash,
                settings=settings,
                algod=algod,
                reader=as_admin,
            )

    partial = as_admin.read_agreement(agreement_id)
    assert partial.state == STATE_OPEN, "three of five is not a filled pool"
    assert partial.seats == sold
    assert partial.total_held == sold * POOL_SHARE_PRICE
    assert as_admin.read_roster(agreement_id) == [
        *[(p.address, POOL_SHARE_PRICE) for p in payers],
        *[(ZERO_ADDRESS, 0)] * (POOL_SEATS - sold),
    ], "the roster is pre-sized to max_seats; the tail is empty, not absent"

    # Before the deadline there is no exit: the creator's early path needs
    # seats == 0, and this pool has three.
    with pytest.raises(REJECTED):
        as_buyer.expire(agreement_id)
    assert as_admin.read_agreement(agreement_id).state == STATE_OPEN

    wait_past_deadline(algod, deadline)

    expire_txid = as_buyer.expire(agreement_id)
    expired = one_event(events_for(algod, expire_txid), "Expired")
    assert (expired["reason"], expired["seats"]) == (EXPIRED_BY_DEADLINE, sold)
    assert as_admin.read_agreement(agreement_id).state == STATE_EXPIRED

    refund_txid = as_buyer.refund_next(agreement_id, sold)
    events = events_for(algod, refund_txid)
    assert [
        (p["seat"], p["payer"], p["amount"]) for name, p in events if name == "Refunded"
    ] == [(i, payers[i].address, POOL_SHARE_PRICE) for i in range(sold)], (
        "refunds go in seat order: the caller picks how many, never which"
    )
    complete = one_event(events, "RefundComplete")
    assert (complete["paid"], complete["skipped"]) == (sold, 0)
    assert inner_transfers(algod, refund_txid) == [
        (p.address, POOL_SHARE_PRICE) for p in payers
    ]

    after = as_admin.read_agreement(agreement_id)
    assert after.state == STATE_REFUNDED
    assert after.refund_cursor == sold
    assert after.total_held == 0
    assert after.unclaimed_seats == 0, (
        "REFUNDED means the cursor finished its pass; only unclaimed_seats == 0 "
        "means everybody actually has their money"
    )
    for p in payers:
        assert usdc_balance(algod, p.address, asset_id) == usdc_before[p.address], (
            "a missed pool makes its payers whole"
        )
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before
    assert usdc_balance(algod, treasury.address, asset_id) == treasury_usdc_before, (
        "a pool that missed its quota pays the beneficiary nothing"
    )

    admin_algo_before_close = algo_balance(algod, admin.address)
    closed = one_event(events_for(algod, as_buyer.close(agreement_id)), "Closed")
    assert closed["algo_returned"] == close_return(
        POOL_SEATS, min_fee, inner_used=INNER_USED_REFUNDED_THREE
    )
    assert closed["algo_returned"] == 159_800, (
        "three refunds plus close's own payment is four of the six reserved "
        "fees, so two fees come back unspent with the box minimum balance"
    )
    assert algo_balance(algod, admin.address) == (
        admin_algo_before_close + closed["algo_returned"]
    )
    assert as_admin.read_agreement(agreement_id) is None
    assert algo_balance(algod, settings.app_account) == app_algo_before


def test_a_full_pool_that_is_never_released_refunds_all_five(
    e2e_env,
    settings,
    algod,
    as_admin,
    as_verifier,
    as_buyer,
    admin,
    buyer,
    treasury,
    verifier,
    job_db_path,
    seat_buyers,
):
    """S7 -- non-delivery, and the fee-reserve arithmetic it exposes.

    Five seats sold, the deadline reached with no matching bytes presented, so
    every payer is refunded. This is the failure the mode exists to price: the
    verifier is the operator, so a `hash` pool guarantees refund on
    non-delivery rather than delivery itself -- which is what escrow has
    always been.

    It is also the only shape that spends the fee reserve to zero, which is
    the arithmetic a two-seat pool cannot show.
    """
    asset_id = settings.usdc_asa_id
    min_fee = as_admin.min_fee()
    commit_hash = _commitment()

    app_algo_before = algo_balance(algod, settings.app_account)
    app_usdc_before = usdc_balance(algod, settings.app_account, asset_id)

    agreement_id, deadline = _create_directly(
        as_admin,
        treasury=treasury,
        verifier=verifier,
        commit_hash=commit_hash,
        algod=algod,
    )
    payers = _payers(buyer, seat_buyers, POOL_SEATS)
    usdc_before = {p.address: usdc_balance(algod, p.address, asset_id) for p in payers}

    with pool_server(e2e_env, algod, admin, job_db_path, agreement_id) as base_url:
        url = f"{base_url}/index"
        with httpx.Client(timeout=120.0) as http:
            fill(
                http,
                url,
                payers=payers,
                agreement_id=agreement_id,
                commit_hash=commit_hash,
                settings=settings,
                algod=algod,
                reader=as_admin,
            )

    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED

    wait_past_deadline(algod, deadline)

    with pytest.raises(REJECTED):
        as_verifier.release_hash(agreement_id, commit_hash)
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED, (
        "no release beats a refund once the deadline five buyers were "
        "promised has passed, and a correct digest does not change that"
    )

    with pytest.raises(ValueError):
        as_buyer.refund_next(agreement_id, POOL_SEATS)

    expire_txid = as_buyer.expire(agreement_id)
    expired = one_event(events_for(algod, expire_txid), "Expired")
    assert (expired["reason"], expired["seats"]) == (EXPIRED_BY_DEADLINE, POOL_SEATS)

    # Two passes, because the batch ceiling is four: four account references
    # is the whole accounts array, not a chosen number.
    first = events_for(algod, as_buyer.refund_next(agreement_id, 4))
    assert [(p["seat"], p["payer"]) for name, p in first if name == "Refunded"] == [
        (i, payers[i].address) for i in range(4)
    ]
    assert [name for name, _ in first if name == "RefundComplete"] == [], (
        "RefundComplete is the receipt for the agreement, not for the call; a "
        "five-seat roster is not finished after four"
    )

    mid = as_admin.read_agreement(agreement_id)
    assert mid.state == STATE_REFUNDING
    assert mid.refund_cursor == 4
    assert mid.total_held == POOL_SHARE_PRICE

    second = events_for(algod, as_buyer.refund_next(agreement_id, 1))
    assert [(p["seat"], p["payer"]) for name, p in second if name == "Refunded"] == [
        (4, payers[4].address)
    ]
    complete = one_event(second, "RefundComplete")
    assert (complete["paid"], complete["skipped"]) == (POOL_SEATS, 0), (
        "the counts are the whole sweep's, read off the record, not this batch's tally"
    )

    final = as_admin.read_agreement(agreement_id)
    assert final.state == STATE_REFUNDED
    assert (final.refund_cursor, final.total_held, final.unclaimed_seats) == (
        POOL_SEATS,
        0,
        0,
    )
    for p in payers:
        assert usdc_balance(algod, p.address, asset_id) == usdc_before[p.address]
    assert usdc_balance(algod, settings.app_account, asset_id) == app_usdc_before

    # -- the finding --------------------------------------------------------
    admin_algo_before_close = algo_balance(algod, admin.address)
    closed = one_event(events_for(algod, as_buyer.close(agreement_id)), "Closed")
    assert closed["algo_returned"] == 157_800
    assert closed["algo_returned"] == close_return(
        POOL_SEATS, min_fee, inner_used=INNER_USED_REFUNDED_FIVE
    )
    from api.escrow import box_mbr, fee_reserve

    assert closed["algo_returned"] == box_mbr(POOL_SEATS), (
        "five refunds plus close's own payment spend the fee reserve to the "
        "microALGO, so what comes back is the box minimum balance and nothing "
        "else. The reserve is sized max_seats + 1 and this is the one shape "
        "that consumes all of it -- the arithmetic a two-seat pool cannot show"
    )
    assert fee_reserve(POOL_SEATS, min_fee) == INNER_USED_REFUNDED_FIVE * min_fee
    assert algo_balance(algod, admin.address) == (
        admin_algo_before_close + closed["algo_returned"]
    )
    assert as_admin.read_agreement(agreement_id) is None
    assert algo_balance(algod, settings.app_account) == app_algo_before
