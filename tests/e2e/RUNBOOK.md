# The TestNet circuit test

Run this by hand before merging. It drives a purchase from the 402 through a real settlement to a
release or a refund, against the real facilitator and the real deployed escrow application, and
asserts on-chain state at every step.

```bash
pytest -m testnet -v          # about six minutes, four scenarios
```

It is the only layer that can answer *does the real facilitator accept what we build, and does the
contract record the payer who actually paid*. Everything a fake can decide belongs in the offline
suite instead, where it is exhaustive and free.

## What it covers

| File | Scenario |
|---|---|
| `test_hash_happy.py` | quote → pay → settle → `release_hash` → `close` |
| `test_hash_refund.py` | non-delivery: deadline → late release refused → `expire` → `refund_next` → `close`, every call sent by the **buyer** |
| `test_abandoned_quote.py` | a quote nobody paid: the creator's early `expire` at `seats == 0`, and the deposit coming back |
| `test_index_pool_seat.py` | a seat in a **multi-seat `hash`** pool: `POST /index` quotes it, the facilitator settles a `join` against an agreement the server did not create in the same request, then the pool is refunded and closed |
| `test_multi_seat_rehearsal.py` | a **full five-seat pool**, taken to each of its three ends. Gated again behind `E2E_REHEARSAL`; see below |
| `test_edition_release.py` | a **two-seat pool committed to a rehearsal bundle**, filled, refused by `pool_ops release`, then released by `agents.release_edition` with the CID in the release note, and closed. Gated behind `E2E_REHEARSAL`, `E2E_EDITION_MANIFEST` and `E2E_EDITION_CID` |

Not covered, and why:

- **`quorum`** — the pooled route sells seats in a `CONDITION_HASH` agreement, and `api/pool.py`
  refuses anything else, so no paid route reaches `CONDITION_QUORUM`. Its release path stays a
  LocalNet contract test.
- **One payment credited to two seats** — needs a four-transaction settlement group, which the
  facilitator is not confirmed to accept. It stays a LocalNet contract test.
- **Leg 2, the gateway, CID handling** — the worker settles in MainNet USDC, so this can never be
  real here. LocalNet fakes own it.
- **The slash variants of the paid path** — `/pin/` and friends are *supposed* to earn a 402; the
  property that matters is that the paid retry is refused without settling, and
  `tests/test_payment_integrity.py` pins that offline against the real middleware.

Redemption is driven by the test through `api/escrow.py`, not by the server: the upload route and
the delivery pipeline are not built yet. As those land, those calls become assertions about what the
server did on its own.

## One-time bootstrap

Four roles, four distinct accounts: **admin** (also the deployer — `bootstrap` sets
`admin = Txn.sender`, so they cannot be separated), **verifier** (a different key from admin, or the
release condition is a promise rather than a constraint), **treasury** (the beneficiary), **buyer**.

```bash
python -m agents.e2e_bootstrap
```

Writes `.env` and `contracts/.env.testnet`, both gitignored, and prints **addresses only** — never a
mnemonic, which is the whole reason it is a script. Re-running is safe: an existing account is
reused, not replaced. `--force` regenerates and needs `E2E_FORCE_CONFIRM=yes`, because replacing a
key strands whatever it holds.

Then, in order:

1. **Fund** the addresses it prints from <https://lora.algokit.io/testnet/fund>. The table it prints
   says how much each needs and what it already has. All four roles need ALGO: the buyer pays for
   its own `expire`, `refund_next` and `close` in the refund scenario, and the treasury pays the fee
   on the teardown that returns the float.
2. **Opt the buyer in to USDC**, before visiting the faucet — an account that has not opted in
   cannot receive the asset, so the faucet visit is wasted otherwise:
   `python -m agents.opt_in_asset --asset-id 10458941 --mnemonic-env E2E_BUYER_MNEMONIC`.
   (On a machine that inherited an already-opted-in buyer from an earlier spike, this is a no-op.)
3. **USDC for the buyer.** Only if it holds none: <https://faucet.circle.com>, which is
   reCAPTCHA-gated. There is no programmatic TestNet USDC dispenser — every documented one is dead,
   ALGO-only, or captcha-gated. This is a *standing balance*, not a per-run cost: a release pays the
   treasury and the teardown sends it back, and a refund returns it directly. At 0.1 USDC a seat, a
   single top-up lasts hundreds of runs.
4. **Deploy the escrow**: `cd contracts && algokit project run build` first, then
   `algokit project deploy testnet`. The deploy step compiles nothing — it sends whatever is in
   `smart_contracts/artifacts/`, so a stale artifact deploys silently. Building first and
   confirming `git diff` on that directory is empty proves the TEAL you are about to send is the
   TEAL in the commit. The deploy driver then funds the application account with 1 ALGO and opts
   it in to USDC. `on_update=AppendApp`, so a changed contract gets a **new app id** and the
   previous application is left untouched rather than updated or deleted.
5. **Record** the new `APP_ID` and `APP_ACCOUNT` in `.env`.
6. **Opt the treasury in**:
   `python -m agents.opt_in_asset --asset-id 10458941 --mnemonic-env E2E_TREASURY_MNEMONIC`.
   A beneficiary that cannot receive is rejected at `create_agreement`, not at release.

## The multi-seat rehearsal

Three more scenarios, gated a **third** time — `E2E_REHEARSAL=1` in `.env`, on top of `E2E_ENABLED`
and the marker — because they spend thirteen settlements and wait out two real deadlines.

```bash
python -m agents.seat_accounts provision --count 4 --usdc-each 100000
pytest -m testnet tests/e2e/test_multi_seat_rehearsal.py -v
```

| Scenario | Ends at | `close` returns |
|---|---|---|
| five seats fill, the verifier releases | `RELEASED` | 161,800 µALGO |
| three of five fill, the deadline passes | `REFUNDED` | 159,800 µALGO |
| five seats fill, nobody releases | `REFUNDED` | **157,800 µALGO** |

The third figure is the point. Five refunds plus `close`'s own payment is six inner transactions
against a six-transaction reserve, so what comes back is the box minimum balance and nothing else —
the arithmetic a two-seat pool cannot show. An under-funded reserve would make `close` draw on
another agreement's deposit; an over-funded one would strand ALGO with no path out.

**Seat accounts.** `join` refuses a payer already on the roster, so five seats need five keys and
this suite has one. `agents/seat_accounts.py` generates the other four, records them in `.env` as
`E2E_SEAT_<n>_MNEMONIC`, funds them with ALGO from the admin and seeds them with USDC from the
buyer. Every step is a top-up guarded by a floor, so re-running costs one `account_info` read per
seat and no money. Never hand-edit those keys; `status` prints addresses and balances only.

The float does not survive a scenario — `release_hash` sends every seat's USDC to the treasury and
it does not come back until session teardown — so each scenario re-seeds before it fills. That is
why `seat_buyers` is a factory rather than a value.

**One pool open at a time.** `agents/create_pool.py` refuses while any open multi-seat `hash` pool
exists, *including* under `--dry-run`, because the guard runs before the dry-run branch. The
rehearsal opens one only inside its own scenarios, but nobody on any track may run `create_pool.py`
while a rehearsal is mid-fill.

**The edition release** (`test_edition_release.py`) runs under the same gate and needs
`E2E_EDITION_MANIFEST` and `E2E_EDITION_CID` for a *rehearsal bundle*: a directory holding a small
`edition-<N>.json` that is not any real edition, optionally renderings of it, and
`edition-<N>-manifest.json` with `edition`, `file` (`edition-<N>.json`), `sha256` and `files` (every
file's sha256, the committed one included), pinned with the pinning provider.
`E2E_EDITION_MANIFEST` is that manifest's path, absolute or relative to the repository root, and
`E2E_EDITION_CID` is the CID the pinning provider reported for the directory. Give the bundle an
edition number no real edition will use. **Never a real edition**: the release note publishes the
CID before any seat holder of that edition has paid, and the scenario refuses a manifest under
`editions/` or one matching a committed edition's digest. It also needs `VERIFIER_MNEMONIC` in
`contracts/.env.testnet`, which `python -m agents.e2e_bootstrap` writes — re-run it on a machine
bootstrapped before that key existed. The scenario checks the key belongs to this suite's verifier
before it opens a pool. It is the only scenario that reaches public IPFS gateways, so it can wait
minutes on a cold one, and its pool's deadline is thirty minutes to allow for that.

## Recovering a run that died

`agents/pool_ops.py` drives the lifecycle calls that have no other CLI. `drain` is the recovery
primitive: it walks an agreement in any state through to deletion, resuming from the refund cursor
rather than counting from zero, so a pass that already paid some seats does not pay them twice.

This section is about a test run that died. A live pool outside the suite is operated by
`docs/runbooks/pool-operations.md`, which makes its refund pass a duty rather than a recovery.

```bash
python -m agents.pool_ops show  --agreement-id <N>
python -m agents.pool_ops drain --agreement-id <N> --as admin [--wait]
```

| Died after | On chain | Recovery |
|---|---|---|
| create, before any join | `OPEN`, `seats == 0` | `drain --as admin`. `expire` is creator-only at `seats == 0` and needs **no deadline wait**, so this is immediate. Returns 162,800 µALGO |
| mid-fill, k seats in | `OPEN`, k seats | `drain --wait`. Blocks until the deadline, then expires, refunds `ceil(k/4)` batches and closes |
| after fill, before release | `FUNDED`, still inside the deadline | `release --agreement-id N --sha256 <hex> --no-edition-note`, then `close`. The flag says this is a test pool with no edition to link; an edition pool is released only by `python -m agents.release_edition`. The digest is printed by the scenario at creation — without it, wait the deadline out and refund |
| after `expire`, mid-refund | `REFUNDING` | `drain` resumes from the cursor |
| a seat was skipped | `REFUNDED` but `unclaimed_seats > 0`; **`close` refuses** | re-opt that account in, then `claim --seat <k>` per seat. `claim_refund` has no sender check, so this needs no key belonging to the skipped payer |
| seats still hold USDC | float stranded on throwaways | `python -m agents.seat_accounts return` |

## Reading a failure

The `preflight` fixture checks the live preconditions once and names each one, so most setup
mistakes fail there with the remedy in the message rather than deep inside a settle.

| Symptom | Cause |
|---|---|
| the whole directory skips | `testnet` was not named in `-m` (the suite never runs without it), or `.env` has no `E2E_ENABLED`, or a required key is missing. The skip message names which. |
| `APP_ACCOUNT is not the address of APP_ID` | `.env` still points at a previous deployment. |
| `application account is not opted in` | the deploy ran without `USDC_ASSET_ID`, so it skipped the opt-in. |
| `buyer holds N but a seat costs M` | top up at the Circle faucet. |
| `timed out waiting for agreement N to reach FUNDED` | the facilitator accepted the group but it did not settle. Inspect it: `python -m tests.verify_join_group --group <base64 group id>`. |
| `httpx` raises `CERTIFICATE_VERIFY_FAILED` | `pip install pip-system-certs`. httpx pins certifi; algosdk and curl read the OS store. |

A run leaves nothing behind: every agreement it creates is closed, and the treasury's USDC goes back
to the buyer on teardown. If a run dies midway it may leave one agreement open, parking about
0.0958 ALGO of the admin's balance until something closes it — 0.1638 for a five-seat pool, whose
roster box is larger and whose fee reserve is six transactions rather than two.

One exception, and it is deliberate: the rehearsal's **seat accounts persist** between runs. They
are recorded in `.env`, and their USDC is returned on teardown while the accounts themselves are
left standing. Closing them out would recover 0.2 ALGO each and destroy the USDC opt-in — and an
account that cannot receive the asset is *skipped* by `refund_next`, which raises `unclaimed_seats`
and blocks `close` on the whole agreement. `seat_accounts return --close` exists for when the
rehearsals are over for good: it opts each seat out of USDC in the same transaction that returns
the balance, because an account still holding an asset cannot be closed at all.

## Rules this suite lives under

- **Never point it at MainNet.** Not a single step, not once. It pays repeatedly and
  machine-driven, which is exactly the traffic shape the facilitator files under a developer bucket
  at settlement time — and that classification cannot be undone.
- **Never schedule it, and never put it in CI.** It is human-triggered by design.
- The server is served on a **loopback** address, so its resource is never catalogued and the suite
  attaches to no merchant identity. Development and production are different applications, and
  therefore different `payTo` addresses and different merchant identities.
