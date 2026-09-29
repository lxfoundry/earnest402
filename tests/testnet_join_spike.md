# Runbook — TestNet join-path and discovery spike

The ordered steps that produced `tests/FINDINGS.md`, with the tool or command that performed each
one. Written so the run can be repeated from an empty environment; the values in
`tests/FINDINGS.md` are what one particular run produced, not inputs to the next.

Everything here targets **TestNet only**. No step in this runbook may be pointed at MainNet.

## 0. Prerequisites

- Python 3.12 virtualenv at `.venv` with `requirements.txt` installed (`x402-avm`, `fastapi`,
  `uvicorn`, `python-dotenv`, `py-algorand-sdk`). The contract's own Poetry environment under
  `contracts/` is not needed here — only its build output, the ARC-56 spec, is read.
- `.env` from `.env.example`. It is gitignored and holds the only signing material in the run.
- An Algorand MCP server reaching TestNet, for everything an admin key has to sign.

If TLS fails with a certificate error, install `pip-system-certs` into the venv so Python trusts
the OS certificate store. **It was needed on 2026-08-21**, where it was not on the run before, so
check rather than assume. The symptom is total, not partial: every HTTPS call from `httpx` fails
with `CERTIFICATE_VERIFY_FAILED` — facilitator, resource host and algod alike — while `curl` and
`algosdk` are unaffected, because they read the OS store and `httpx` pins `certifi`. The same
trust gap makes Fly.io's remote builder fail; `fly deploy --local-only` builds with the local
Docker daemon and sidesteps it.

## 1. Accounts

| Account | Created by | Purpose |
|---|---|---|
| `SPIKE_ADMIN` | MCP `create_account`, then `fund_account` | deploys and administers the apps |
| buyer | `algosdk.account.generate_account()`, locally | signs the axfer and the app call |
| dry-run fee payer | `algosdk.account.generate_account()`, locally | stands in for the facilitator in step 5 |

The buyer and the fee payer are generated **outside** MCP on purpose: their transactions have to be
signed and handed to an HTTP client as raw bytes, and the MCP account provider cannot export signing
material. Their keys go into `.env` as `AVM_PRIVATE_KEY` / `DRYRUN_FEE_PAYER_KEY`; both addresses are
funded with MCP `fund_account`, which accepts an arbitrary address.

Derive the buyer's address without printing its key:

```bash
python -c "from agents.common import get_buyer_signer; print(get_buyer_signer().address)"
```

## 2. Deploy

MCP `app_deploy` with `appSpecPath` pointing at
`contracts/smart_contracts/artifacts/join_spike/JoinSpike.arc56.json`, `method: create`,
`args: [<assetId>, <sharePrice>]`, `sender: SPIKE_ADMIN`. Record `appId` and `appAddress` into
`.env` as `APP_ID` / `APP_ACCOUNT`.

Deploy this twice — see step 5 for why the second instance exists.

## 3. Fund the application account

MCP `send_payment` from `SPIKE_ADMIN`, 500000 microALGO. Generous rather than computed: the account
needs its own minimum balance, one ASA opt-in, and box storage for each agreement it records.

## 4. Opt the application into its asset

MCP `app_call`, `method: opt_in_asset`, `args: [<assetId>]`, `sender: SPIKE_ADMIN`,
**`extraFee: 1000`** — the contract submits its opt-in as an inner transaction with `fee=0`, so the
outer call has to carry that leg's fee.

Confirm with MCP `get_account_info` on the application address: the asset is present with amount 0.
A settle against an application that has not opted in fails at the facilitator layer.

## 5. Sourcing a payment asset, and the dry run

**Read this before step 6.** At the time of the last run, every public source of TestNet USDC (ASA
`10458941`) was behind a captcha, and the programmatic dispenser hands out ALGO only. A buyer holding
no USDC cannot settle anything, so both the dry run and the settled facilitator attempts used a
**locally minted 6-decimal ASA** and a **second application instance** bound to it:

```
MCP create_asset   total=1000000000000 decimals=6 unitName=SPKUSD sender=SPIKE_ADMIN
MCP app_deploy     create(<newAssetId>, 10000)        # the second instance
MCP send_payment   500000 microALGO -> its app address
MCP app_call       opt_in_asset(<newAssetId>) extraFee=1000
MCP asset_transfer 5000000 units -> buyer
```

The buyer must opt into every asset it will pay in, signing locally (MCP cannot sign for it): a
0-amount `AssetTransferTxn` to itself per asset.

If a captcha-free USDC source exists when this is next run, skip the minted asset and use ASA
`10458941` throughout — it is the higher-fidelity run.

### The dry run itself

```bash
python -m tests.dry_run_join_group --agreement-id 1 --variant fee_payer_first
python -m tests.dry_run_join_group --agreement-id 2 --variant axfer_first
```

Reads `DRYRUN_APP_ID` / `DRYRUN_APP_ACCOUNT` / `DRYRUN_ASSET_ID` / `DRYRUN_FEE_PAYER_*` from `.env`.
It simulates and then submits *the exact bytes* `agents/build_join_group.py` produces, with the
locally held fee payer signing the leg the facilitator would otherwise countersign.

**Do not skip this.** It is what makes a later facilitator rejection interpretable: with the dry run
green, a rejection is facilitator policy; without it, a rejection could equally be a malformed group
or a contract bug. Run it before spending a facilitator attempt.

## 6. Stand up the route

```bash
python -m uvicorn api.server:app --host 127.0.0.1 --port 8000
```

`api/server.py` reads `APP_ACCOUNT` and `USDC_TESTNET_ASA_ID` from the environment, and
`python-dotenv` does not override variables already set, so a second route against a different
asset needs no code change:

```bash
USDC_TESTNET_ASA_ID=<mintedAssetId> APP_ACCOUNT=<secondAppAddress> \
  python -m uvicorn api.server:app --host 127.0.0.1 --port 8001
```

**The price is not overridable this way.** `APP_ID`, `APP_ACCOUNT` and `USDC_TESTNET_ASA_ID` come
from the environment, but the amount does not: `10000` base units is hardcoded independently in four
places that all have to agree, or the contract's `join` rejects the group with `wrong amount`:

| Where | What it sets |
|---|---|
| `api/server.py` — the route's `price.amount` | what the 402 asks the buyer for |
| the contract's `create(<assetId>, 10000)` deploy argument (steps 2 and 5) | `share_price`, the value `join` asserts against |
| `agents/fallback_direct_settle.py` — `DEFAULT_AMOUNT` | the amount that script builds, when bypassing the resource server |
| `tests/dry_run_join_group.py` — the `--amount` default | the amount the dry run builds |

Reproducing at a different price means changing all four. The first two are the ones that must match:
a mismatch between them fails every attempt at the contract's amount assertion, which reads as a
group-shape problem and is not. (`agents/build_join_group.py` also names `10000`, but only inside its
offline `_self_check()`, which never touches a deployed app.)

Smoke-test the discovery declaration:

```bash
python -m tests.verify_join_group --discovery http://localhost:8001
```

This replaces the spec's `curl -i <host>/<route> | grep -i bazaar`, which cannot match: under
protocol V2 the payment requirements ride in the base64-encoded `payment-required` header, so the
string `bazaar` is never literally present in the response. The script decodes the header first.

## 7. The facilitator attempt

```bash
RESOURCE_HOST=http://localhost:8001 APP_ID=<secondAppId> \
  python -m agents.join_client_manual --agreement-id 10 --variant fee_payer_first
```

`fee_payer_first` is the ordering to try first. Triage the result:

- **HTTP 200** — accepted. Go to step 8 and confirm it on chain before believing it.
- **Rejected with a facilitator-originated error** — record the literal text. Only try
  `--variant axfer_first` if that error points at ordering specifically; otherwise a second attempt
  proves nothing that the first did not.
- **Rejected before reaching the facilitator** (our own server's validation) — ambiguous. Run
  `python -m agents.fallback_direct_settle --agreement-id N --variant V`, which posts the same group
  straight to the facilitator's `/verify` and `/settle` and takes the resource server out of the
  path.

Record each variant as its own attempt, so a shape rejection and an ordering rejection are never
conflated.

A rejection here still needs a landed payment for step 9's discovery check; get one with
`RESOURCE_HOST=... python -m agents.plain_settle_fallback`, a standard 2-transaction settlement.
Worth running even after a success, as a control: it distinguishes "the catalogue ignores our
unusual group" from "the catalogue ignores this route".

## 8. Confirm the settlement on chain

An HTTP 200 is the facilitator's claim, not evidence. Take the `transaction` id out of the decoded
`payment-response` header and get the group id from it, then check the group and the contract state:

```bash
python -m tests.verify_join_group --group <groupId> --box <appId>:<agreementId>
```

Three signals have to agree before calling the attempt a genuine cosigned settlement:

1. the group holds all three legs — `pay`, `axfer`, `appl`;
2. the `pay` leg's sender **equals the sponsor address the facilitator advertises at `/supported`**,
   and it carries the pooled fee while the buyer's legs are `fee=0`. This is the cosigning evidence.
   The check is a positive identification of that specific address, not "signed by someone other than
   the buyer" — a second key the buyer controls would satisfy the weaker test and prove nothing. The
   script fetches the expected address itself and **fails** on any mismatch;
3. `payers[agreementId]` decodes to the buyer, and the ARC-28 `Joined` event's `payment_txn_id`
   equals the axfer id the facilitator reported.

A group from the step-5 dry run was never cosigned by the facilitator — its fee-payer leg carries the
local throwaway key — so it correctly **fails** the default check. Verify one by naming its own
signer:

```bash
python -m tests.verify_join_group --group <dryRunGroupId>   --expected-fee-payer $DRYRUN_FEE_PAYER_ADDRESS
```

The same facts can be read directly with MCP `lookup_transaction_group`, `read_box_state`
(`keyPrefix: "p_"`, `keyType: "uint64"`) and `lookup_application_logs`.

## 9. Discovery checks

```bash
python -m tests.verify_join_group --discovery http://localhost:8001
```

Runs all three checks from `docs/specs/discovery-and-attribution.md` section 6, and when the route
is absent from the catalogue also prints an attribution survey: the catalogued population broken down
by network, and how many catalogued resources are served on a loopback host. Those two numbers are
what separate "absent because TestNet is excluded" from "absent because nothing can reach the
resource", so they are computed rather than asserted.

Two things the hand-written `curl | jq` versions get wrong:

- both discovery endpoints report a `pagination.total` larger than the maximum `limit` they accept,
  so a single request misses most of the catalogue and can report a false negative. The script pages
  to exhaustion;
- check 1 has to decode the `payment-required` header, as above.

Attribute any check-2/3 failure using check 1: a route that never declared discovery is a
configuration bug, whereas a declared route that is absent from the catalogue is something else —
consider whether the resource URL is publicly reachable at all before concluding the declaration is
broken.

## 10. Record and commit

Write the outcome into `tests/FINDINGS.md`: every group shape and ordering attempted, the literal
HTTP status and error body for each, which layer rejected it, and the verdicts. Commit at each
natural boundary rather than as one change at the end.

Never commit `.env`, a mnemonic, or a private key — not in code, not in the findings, not in a commit
message. Application ids, addresses, group ids and transaction ids are public and fine to record.
