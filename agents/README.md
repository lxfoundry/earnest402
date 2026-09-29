# agents/

Client-side scripts for the join-path spike: they construct Algorand transaction groups and drive
the x402 handshake against `api/server.py` (and, for one of them, directly against the facilitator).
`agents/common.py` holds the shared pieces (a TestNet algod client, the buyer's signer) that every
script below reuses rather than reimplementing.

## Scope

These are one-shot scripts for a specific experiment, not a reusable agent framework. Each script
prints what happened (status code, headers, body) and exits; none of them retry, poll, or manage a
job lifecycle. `build_join_group.py` only builds and signs a transaction group — it never submits
anything to a network.

## The scripts

- **`build_join_group.py`** — builds the non-standard 3-transaction atomic group at the center of
  this spike: an unsigned fee-payer self-payment, a buyer-signed ASA transfer, and a buyer-signed
  application call invoking the escrow stub's `join(agreement_id, payment_index)` method. Run
  directly (`python -m agents.build_join_group`) to execute a structural self-check with a throwaway
  keypair — no facilitator or deployed contract required, only a read-only TestNet algod call for
  suggested params.
- **`join_client_manual.py`** — the real attempt: unauthenticated request to the paid route, decode
  the 402, build the 3-txn group, attach it as a payment header, retry, print the result.
- **`fallback_direct_settle.py`** — the same 3-txn group, posted straight to the facilitator's own
  `/verify` and `/settle` endpoints, bypassing `api/server.py`. Use this to tell apart "our resource
  server rejected the shape" from "the facilitator itself rejected the shape."
- **`plain_settle_fallback.py`** — an ordinary 2-txn payment (no application call) against the same
  route, so there is still a landed payment to check discovery/attribution against even if the 3-txn
  group is rejected everywhere.

## The operator scripts

Later additions, and not spike scripts: these are run against a live deployment on purpose, and two
of them move money. They are listed here because the section above predates them.

- **`e2e_bootstrap.py`** — generates the four roles the TestNet suite signs with, writes them to
  `.env` and `contracts/.env.testnet`, and prints addresses only.
- **`opt_in_asset.py`** — opts one account into an ASA. A beneficiary that cannot receive is
  rejected at `create_agreement`, not at release.
- **`product_kinds.py`** — not a script: the table of deliverable kinds `create_pool.py` and
  `release_edition.py` share. A manifest's `file` names its kind — `edition-N.json` for the index,
  `trip-N.json` for a trip file — and each kind's notes carry its own prefix, `earnest:index:edition-`
  or `earnest:travel:trip-`.
- **`create_pool.py`** — opens one edition's pool from a frozen manifest, the one committed under
  `editions/`. Irreversible:
  `commit_hash` is fixed at creation and no method rebinds it. `--dry-run` first, every time, and
  note that it refuses while any open multi-seat `hash` pool exists — including under `--dry-run`,
  because the guard runs before the dry-run branch.
- **`seat_accounts.py`** — generates, funds and seeds the extra payers a multi-seat pool needs,
  because `join` refuses a payer already on the roster and the suite has one buyer. Every step is a
  top-up guarded by a floor, so re-running costs a read per seat and no money. TestNet only: it
  talks to a TestNet node and seeds TestNet USDC, for the test suite's multi-seat rehearsals.
- **`pool_ops.py`** — the lifecycle calls that have no other CLI: `release`, `expire`, `refund`,
  `claim`, `close`, and `drain`, which walks an agreement in any state through to deletion. Its
  `release` refuses a multi-seat pool unless `--no-edition-note` says it is a test pool; an edition
  pool is released by `release_edition.py`.
- **`release_edition.py`** — releases an edition pool with the edition's CID in the release note,
  after checking the agreement, the manifest, and every file the manifest inventories, fetched
  through public gateways and hashed. The gateways are `RECHECK_GATEWAYS` from the environment or
  `.env`, comma-separated and tried in order (default `https://ipfs.filebase.io`).
  `--dry-run` runs every check and sends nothing.
  `--skip-gateway-check` leaves out the gateway fetch. The verifier
  key is `VERIFIER_MNEMONIC` from `contracts/.env.<network>`. The specification is
  `docs/specs/edition-release.md`.

## Running

From the repository root, with dependencies installed (see `requirements.txt`) and a `.env` file
populated from `.env.example`:

```bash
python -m agents.build_join_group          # structural self-check only, no network config needed
python -m agents.join_client_manual        # requires api/server.py running and reachable at RESOURCE_HOST
python -m agents.fallback_direct_settle    # requires FACILITATOR_URL, APP_ID, APP_ACCOUNT
python -m agents.plain_settle_fallback     # requires api/server.py running and reachable at RESOURCE_HOST
```

`join_client_manual.py` and `fallback_direct_settle.py` accept `--agreement-id` and `--variant`
(`fee_payer_first`, the default, or `axfer_first` — see `build_join_group.py`'s module docstring for
what each ordering means). `join_client_manual.py` also takes `--route` and `--mnemonic-env NAME`,
which pays with the 25-word mnemonic held in that variable instead of `AVM_PRIVATE_KEY` — one of the
`E2E_SEAT_<k>_MNEMONIC` keys `seat_accounts.py` records, for a pool that needs a payer per seat.

## Key material — TestNet only

`AVM_PRIVATE_KEY` and `AVM_BUYER_MNEMONIC` in `.env` must be TestNet-only values with no MainNet
funds behind them. Never commit a populated `.env` — it is gitignored; only `.env.example` (with
empty values) belongs in the repository. Rotate a key immediately if it is ever pasted into a chat
log, an issue, or a commit.
