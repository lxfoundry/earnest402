# Earnest

**"If this, then pay" for x402.** Earnest holds the payment on Algorand until the condition is met:
enough buyers joined to fund a purchase together, or the deliverable matches what was promised.

**Nobody pays unless enough do.**

Its first MainNet pool uses both conditions. 5 buyers must join, and what was promised is a file
whose sha256 was written on chain when the pool was created, before anyone could pay:

```
7ff9c6cc101dc4c53d6638f802fb3cd3e976339164194e45c0d804136eb0ddd9
```

## The file: edition 1 of the Algorand x402 Route Index

On 16 Sep we called, unpaid, all 1,841 endpoints the facilitator's directory lists for USDC on
Algorand:

- 1,580 asked for payment, 148 answered without a 402, 113 were unreachable
- of 1,578 with both a listed and a live price, 450 differ, 211 by 10× or more

Free report, aggregates only:
**<https://earnest.lxfoundry.ai/app/editions/edition-1-sample.html>**

A seat buys the full edition, naming every endpoint and its payee, in 3 formats: HTML to read, CSV
to analyse, JSON for agents. The sha256 is the JSON's. **Condition: 5 distinct buyers must join
before the deadline.** If the pool doesn't fill, or we don't release by then, the edition is not
published and every seat is refunded on chain to the wallet that paid.

## How the pool works

- 5 USDC a seat. Closes **Thu 24 Sep 2026, 18:30 UTC**
- Your payment settles into the escrow app, not our wallet. It reaches us only through a release
  that presents that sha256, and that transaction carries the IPFS link, so you can hash what you
  get
- We run the refunds; anyone can send them from the pool page
- Once this pool is released or expires, the next opens on a freshly probed edition, funded by its
  own pool

**Objectively verifiable conditions only: no arbitration, no human in the loop.**

**Buy a seat: <https://earnest.lxfoundry.ai/app/>**

You need an Algorand wallet with 5 USDC (tested with Pera); the facilitator pays the network fee.
It's new, so if anything breaks or reads wrong, say so: [open an
issue](https://github.com/lxfoundry/earnest402/issues), or reply wherever you found this.

---

The rest of this page is the machinery, and the code that runs it.

Underneath is an Algorand escrow app holding a standard `exact` USDC payment against a deadline and
a release condition.

## The two release conditions

| Condition | Releases when | Refunds when |
|---|---|---|
| `quorum` | `min_seats` distinct payers have joined | the deadline passes with fewer |
| `hash` | the delivered bytes' sha256 equals a commitment fixed at creation, before anyone pays | the deadline passes without a match |

*"What was promised"* is never a judgment about quality. It is a 32-byte sha256 written into the
agreement when the agreement is created, and unchangeable afterwards — so a supplier has to commit
to the exact bytes before taking a payment, and the chain only ever compares two numbers.

Two further condition types, `schema` and `evaluator`, are *designed in* — the record carries a
condition field and the state machine has room for them — but neither is shipped. Two conditions
exist today.

## What is in this repository

| Path | What |
|---|---|
| `contracts/` | The escrow application: Algorand Python source, compiled TEAL and ARC-56 artifacts, and its 140-test suite |
| `docs/specs/escrow-contract.md` | The specification it was built from — states, storage layout, ABI, authorisation, invariants, failure modes |

Not published yet: the resource server that answers the x402 `402`, the web client, and the
operator tooling. This repository is the part that holds the money, which is the part worth
checking.

## Verify what is actually running

| Network | Application | Application account |
|---|---|---|
| MainNet | [`3710645149`](https://lora.algokit.io/mainnet/application/3710645149) | `2IZVDH36QFPIQFKOBKHAN4CREL4K77PXU6HQ5RMOF5E47DPE5XZ37Q772I` |
| TestNet | [`771795120`](https://lora.algokit.io/testnet/application/771795120) | `CVD7RDNAMB6KPAOS26UEGWOT4MVGFJXRDEIUMOEBPA57FZ7PVVXCKCU5OI` |

Both run the same program, byte for byte, and both match the artifacts committed here.

**1. Hash the deployed program:**

```bash
curl -s https://mainnet-api.4160.nodely.dev/v2/applications/3710645149 \
  | python -c "import sys,json,base64,hashlib; p=json.load(sys.stdin)['params']; \
print(hashlib.sha256(base64.b64decode(p['approval-program'])).hexdigest())"
```

**2. Hash the artifact in this repository:**

```bash
python -c "import json,base64,hashlib; \
b=json.load(open('contracts/smart_contracts/artifacts/escrow/Escrow.arc56.json'))['byteCode']['approval']; \
print(hashlib.sha256(base64.b64decode(b)).hexdigest())"
```

Both print the same digest:

```
9413f24811ab11b4ed0006da32f4f10741996e11f9019415c45e300087a0b647
```

Substituting `clear-state-program` and `['clear']` in the two commands confirms the clear-state
program the same way: `ed90f0d2da1f1d1abd773c45230651a292a90edbc12a7bf859a493a12a640ce7`.

**3. Rebuild from source and check the artifact is not just asserted.** Needs
[AlgoKit](https://github.com/algorandfoundation/algokit-cli) and Python 3.12; the compiler is
`puya` 5.10.0, pinned by `contracts/poetry.lock`.

```bash
cd contracts
algokit project bootstrap all
algokit project run build
```

## What the contract can and cannot do to you

Every claim below is checkable against the source in this repository and against the ABI in
`contracts/smart_contracts/artifacts/escrow/Escrow.arc56.json`.

**The program can never be changed or deleted.** `Escrow.arc56.json` declares
`bareActions: {"create": [], "call": []}`, and every one of the twelve methods is `NoOp`-only. There
is no `UpdateApplication` and no `DeleteApplication` path in the ABI, so the bytes you verified
above are the bytes that will run for the life of the application.

**Pausing cannot trap your money.** The contract has an admin, and the admin can set a `paused`
flag. That flag is read in exactly two places — `create_agreement` and `join`
(`contract.py:252` and `contract.py:392`). It does not appear in `expire`, `refund_next`,
`claim_refund`, `release_quorum`, `release_hash` or `close`. A paused contract stops taking new
money; it cannot stop money leaving.

**Refunds do not need us.** Past the deadline, `expire` is permissionless, and so are `refund_next`
and `claim_refund`. Anyone can push a stalled agreement into refund and walk the roster; a payer the
batch had to skip can pull their own seat back with `claim_refund`. If the operator disappears, the
refund path still works, and the admin has no method that interferes with it
(`tests/test_escrow_expire_refund.py`, 47 cases).

**`release_quorum` is permissionless**, because the contract counted the payments itself. Once
`min_seats` is reached the condition is permanently true — there is no leave and no withdraw — so
anyone may trigger the release (`tests/test_escrow_release.py::test_release_quorum_is_permissionless`).

**A late release cannot race a refund.** `release_hash` asserts
`Global.latest_timestamp < deadline`, so once the deadline passes the only reachable path is refund.

### The limit, stated plainly

`release_hash` requires the `verifier` named in the agreement, and for agreements we create, that
verifier is us. The contract checks that the submitted sha256 equals the committed one; it cannot
check whether we bothered to submit anything at all.

So for a `hash` agreement the chain guarantees **a refund if the deliverable does not arrive** — not
delivery itself. The deadline is what enforces it: if we never release, the money goes back, and
nothing in the contract lets us stop that. `quorum` has no equivalent gap, because
`release_quorum` needs no one's cooperation.

This is the residual trust in the design, and naming it is part of the design.

## Build and test

```bash
cd contracts
algokit project bootstrap all     # Poetry venv + dependencies
algokit localnet start            # Docker
poetry run pytest                 # the escrow test suite
algokit project run build         # recompile TEAL and ARC-56 artifacts
```

`contracts/README.md` has the longer AlgoKit walkthrough. `join_spike` alongside the escrow is a
deliberate stub — it exists only to prove that a payment group carrying an extra buyer-signed
application call is accepted, and has none of the roster, release or refund logic.

## Licence

[Apache-2.0](LICENSE).
