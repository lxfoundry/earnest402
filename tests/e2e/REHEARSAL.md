# The multi-seat rehearsal, 2026-09-16

What a full five-seat pool cost and how long it took, measured on TestNet application
**`771795120`** before any of it was done with real money. Day 3 runs the release path once, for
real; this is the record it is sized against.

Reproduce with:

```bash
python -m agents.seat_accounts provision --count 4 --usdc-each 100000
pytest -m testnet tests/e2e/test_multi_seat_rehearsal.py -v --durations=5
```

## What ran

Three scenarios, one per terminal state, at five seats and 0.10 USDC a seat.

| Scenario | Agreement | Seats sold | Ends at | Wall clock | Run |
|---|---|---|---|---|---|
| fills, verifier releases | 6 | 5 of 5 | `RELEASED` | 78.5 s | first |
| fills, verifier releases (with the opcode probe) | 15 | 5 of 5 | `RELEASED` | 90.0 s | second |
| partial fill expires | 7 | 3 of 5 | `REFUNDED` | 445.4 s | second |
| full fill, never released | 8 | 5 of 5 | `REFUNDED` | 455.4 s | second |
| | | | **second run, 3 passed** | **16 m 47 s** | |

Four rows, three scenarios: the release path ran twice, and the second run is the one the total is
taken from — 990.8 s of scenario plus fixture setup. The first row is the same scenario before the
opcode probe was added, kept because it is the only timing for the release path without it.

Thirteen settlements through the real GoPlausible facilitator. The two expiry scenarios are almost
entirely deadline wait — 420 s each by construction, so the useful figure is the release path at
about 90 s.

## Fees

**0.0500 ALGO total**, across the four operator roles, for the whole window — provisioning the four
seat accounts, three pools, thirteen settlements, and the recovery of one pool stranded by an
unrelated TLS failure.

The table below is the operator roles only. The four seat accounts pay their own fees too — one
opt-in each at provisioning, and one transfer each on every teardown — which is another 8,000
µALGO across a provision-and-return cycle, from ALGO the admin funded them with.

| Account | Transactions sent | Fees |
|---|---|---|
| admin | 21 | 21,000 µALGO |
| buyer | 33 | 25,000 µALGO |
| verifier | 2 | 2,000 µALGO |
| treasury | 2 | 2,000 µALGO |

The buyer sent 33 transactions and paid 25 fees: the axfer and app-call legs inside a settlement
group are priced at zero and the facilitator's sponsor covers the group. **We do not pay for the
settlements themselves** — only for the calls we send directly, which are `expire`, `refund_next`
and `close`.

Per pool, the ALGO that moves and comes back is the deposit: **163,800 µALGO** at `min_fee = 1000`
(157,800 box minimum balance for the two boxes, plus a six-transaction fee reserve).

## The finding: the fee reserve is exact

`close` returns a *computed* figure — the box minimum balance plus the **unspent** part of the fee
reserve — never whatever was deposited. All three figures were asserted, not eyeballed:

| Ends at | Inner transactions used | `close` returns |
|---|---|---|
| `RELEASED` | 2 (the release transfer, plus close's own payment) | 161,800 µALGO |
| `REFUNDED`, 3 seats | 4 (three refunds, plus close's own payment) | 159,800 µALGO |
| `REFUNDED`, 5 seats | 6 (five refunds, plus close's own payment) | **157,800 µALGO** |

The last row is what a two-seat pool cannot show. The reserve is sized `max_seats + 1`, and a
five-seat pool that refunds every seat is the one shape that consumes all of it — so what comes back
is the box minimum balance and nothing else. An under-funded reserve would make `close` draw on
another agreement's deposit and break ALGO conservation; an over-funded one would strand ALGO with
no path out. It is right.

A fourth figure, measured while recovering a stranded pool: an agreement nobody joined returns
**162,800 µALGO** — one inner transaction used, five reserve fees unspent.

## The second finding: how close five seats runs to the opcode ceiling

`join` scans the roster for a repeated payer, so it costs more with every seat already taken — which
is why the seat ceiling of 20 is a *measured* figure and not a chosen one. Measured here by
simulating each join before submitting it, so it costs nothing:

| Seat | Consumed | Of budget |
|---|---|---|
| 0 | 245 / 700 | 35 % |
| 1 | 263 / 700 | 38 % |
| 2 | 281 / 700 | 40 % |
| 3 | 299 / 700 | 43 % |
| 4 (fills the pool) | 337 / 700 | **48 %** |

**18 opcodes per seat scanned**, plus a one-off **+20** on the seat that fills the pool — that seat
takes a branch none of the others do, emitting `Filled` and moving the state. Averaging its step
into the slope overstates the cost at the ceiling by about 12 % — averaging all four steps gives 23
rather than 18, and `245 + 23×19 = 682` against the correct 607 — so the two are reported separately.

Answering the brief's question directly: **a five-seat pool peaks at 48 % of the per-call budget,
which is nowhere near the wall.**

The extrapolation is the more interesting part. `consumed(n) = 245 + 18n` first exceeds 700 at
**seat 26** — and `docs/plans/2026-09-15-multi-seat-hash-agreements.md` records that joins died at 26
on LocalNet once `join`'s group scan was added. A measurement taken on TestNet, by simulation,
against a deployed application reproduces a number measured independently on LocalNet against a
compiled one. The ceiling of 20 is sound, and the headroom it carries is about six seats — real, but
narrower than "deliberate headroom" might suggest. A 20-seat pool would peak at 607/700 (87 %).

Anything that adds work to `join` spends that margin. Re-measure before raising the ceiling, and
re-measure after adding a guard to `join` even if the ceiling does not move.

## Conservation, read from the chain

After all three scenarios and their teardown:

- the application holds **zero boxes**;
- the application account is back to **1,000,000 µALGO** and **0 USDC**, exactly where it started;
- the buyer holds **19,970,000 µUSDC** — the opening figure to the micro-unit. A release pays the
  treasury and teardown sends it back; a refund returns it directly. Nothing leaked.

## Handoff to track 1 — the pool left open

**Agreement `25` on application `771795120`.** Five seats at 0.10 USDC, deadline `1789651281` (two
days from creation). **No longer pristine: one of the five seats is sold.** Created with
`agents/create_pool.py` so the operator path was the one that opened it.

It replaces agreement `16`, which held the same parameters and was drained to free the application
for the one-session re-run below — `create_pool.py`'s one-pool guard refuses while any pool is open.
Same commit hash, same seats, same price; only the id and deadline moved.

Seat 0 went to `QWUKD4ZG…` for 100,000 micro-USDC — a real purchase from a browser wallet, which
is what the spike on that pool was for. Two things follow from it. The pool now holds someone
else's money, so draining it is a refund and not just a cleanup. And `expire`'s immediate
creator-reclaim path is gone: it applies only at zero seats, so the deadline now governs whoever
asks.

To serve it, set `INDEX_AGREEMENT_ID=25` and restart — `POST /index` answers 503 until that variable
names the agreement, however correctly it was created.

Its `commit_hash` is `fa3c8f3b…`, which is **not a frozen edition**. It is
`sha256(b"earnest-x402 track-1 browser-wallet spike pool, 2026-09-16")`, deterministic so anyone can
recompute it and see it for what it is. Nothing will be delivered against it; the pool is expected
to expire and refund every seat.

Three consequences worth stating plainly:

- **`pytest -m testnet` cannot pass in full while this pool is open.**
  `test_a_five_seat_pool_fills_releases_and_closes` asserts no pool is open before it runs, because
  `create_pool.py` refuses otherwise. Clearing agreement 25 used to be instant; with a seat sold it
  takes the deadline, so the full suite is effectively gated until then unless someone drains with
  `--wait`. The other six scenarios are unaffected — they create their agreements through the
  client and never consult the one-pool guard.
- **`agents/create_pool.py` will refuse on this application until agreement 25 is gone**, including
  under `--dry-run`, because its one-pool guard runs before the dry-run branch. The deadline is two
  days rather than the default seven so the block lifts on its own if nobody clears it sooner.
- **Somebody has to drain it afterwards**, or 163,800 µALGO of the admin's balance stays parked and
  the sold seat's 100,000 micro-USDC stays with the contract rather than going back to its payer:
  `python -m agents.pool_ops drain --agreement-id 25 --as admin --wait`. `--wait` is now required
  rather than optional — with a seat sold, the immediate path is unavailable and the command
  refuses without it, naming how long is left. Verified: run without `--wait` it declines and sends
  no transaction, leaving the admin balance unchanged.

  The seat's payer must still be able to receive USDC when the refund pass reaches it. It is opted
  in and unfrozen today; if that changes, `refund_next` skips the seat, `unclaimed_seats` rises and
  `close` is blocked until someone sends `claim_refund` for it.

## Verification

| Check | Result |
|---|---|
| `pytest -m testnet` — all seven, one session | **7 passed, 22 m 34 s** |
| `pytest -m testnet tests/e2e/test_multi_seat_rehearsal.py` | 3 passed, 16 m 47 s |
| the four pre-existing scenarios, re-run after the `conftest.py` and `harness.py` edits | 4 passed, 5 m 50 s |
| `pytest -m "not localnet and not testnet"` | 327 passed, 7 deselected — the offline pass count unchanged; 334 collected, up from 331 by this file's three scenarios |
| `ruff check api agents tests probe` | clean |
| `seat_accounts return --close`, on a throwaway account | closes; the pre-fix sequence is rejected `cannot close: 1 outstanding assets` |

The close-out row is checked outside the suite on purpose. `seat_buyers` tears down with
`close=False` so the four seat accounts survive between runs, which means nothing in `pytest -m
testnet` ever reaches the `--close` path -- and proving it against the real seats would destroy the
opt-ins it takes a captcha-guarded faucet to rebuild. A throwaway account settles it instead: the
close is refused while any holding is open, even at a zero balance, so the opt-out is a
precondition rather than a courtesy. Both figures `return_seats` reports matched the receivers'
actual deltas.

The first two rows were **two batches, not one `pytest -m testnet`** — a weaker check, because it
shows the new scenarios pass and the old ones still pass, but not that they pass in one session
sharing one set of fixtures. The top row closes that: all seven together, 7 passed in 22 m 34 s,
with the session-scoped float recirculating across the whole run rather than per batch. The
per-scenario timings held — 453.3 s, 445.9 s and 90.0 s against the 455.4, 445.4 and 90.0 measured
in the split run.

## What day 3 should take from this

**Size the deadline for filling plus delivering, never for delivering alone.** The agreement's one
`deadline` bounds both. Five settlements took roughly 65 seconds of wall clock back to back — but
that is five buyers who were already waiting. Real buyers arrive over hours or days, and the pool
cannot be released until the last of them has paid. A deadline sized for delivery alone will refund
a pool whose bytes existed.

**The refund pass needs two calls at five seats.** `REFUND_BATCH_CEILING` is 4, which is the whole
accounts array rather than a chosen number. `refund_next(id, 4)` then `refund_next(id, 1)`;
`RefundComplete` is the receipt for the agreement, not for the call, so it does not appear on the
first. `agents/pool_ops.py drain` does the arithmetic from the cursor.

**One pool at a time is enforced, including under `--dry-run`.** `create_pool.py`'s guard runs
before the dry-run branch, so a pool left open blocks every later create on the application.

## Three defects this found

All three were found by running it, not by reading it.

1. **The seat provisioner bled on every re-run.** A seat pays its own opt-in fee, so it settles
   1,000 µALGO below any exact funding target, and each later run closed that gap again at a fee
   each time — 8,000 µALGO per run that was supposed to be free. Fixed by topping up only below a
   floor.
2. **`drain` could not recover a zero-seat agreement.** `EXPIRED` is not terminal and `close`
   rejects it; the refund cursor has to finish a pass first, even where no seat was ever sold. Found
   for real, on agreement 5, left open when a TLS failure killed a run between create and fill.
3. **The opcode probe took down the scenario it was measuring.** Its docstring promised a failed
   measurement would never turn a run red; its `except` clause named three exception types and the
   SDK raised a fourth. One create, no settlements, and a pool left open. The guard is now
   deliberately broad, with the reason written beside it — a measurement taken alongside real money
   has no failure worth losing the run over.

The TLS failure itself was environmental and is already in the runbook: `pip install
pip-system-certs`, because httpx pins certifi while algosdk and curl read the OS store.
