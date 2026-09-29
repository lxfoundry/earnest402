# TestNet join-path and discovery spike — findings

**Run date:** 2026-08-21 · **Network:** Algorand TestNet
(`algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=`) ·
**Facilitator:** `https://facilitator.goplausible.xyz`

Two independent questions, answered by one run. Objectives C and D were added by later runs
against later applications; each records its own run date and artefacts.

| | Question | Verdict |
|---|---|---|
| **A** | Will the facilitator accept and cosign an atomic group carrying an extra buyer-signed application call, beyond the standard payment? | **ACCEPT** |
| **B** | Does a route that declares the Bazaar discovery extension get catalogued after it settles a payment? | **PASS** (on a public host paying real USDC: catalogued in ~6s, with a merchant identity. The first, loopback run's negative was confounded — both runs recorded below) |
| **C** | Does a query-bearing request catalogue as one row, or as one row per query? | **PASS** — a pinned `resource` collapses them into one |
| **D** | Does the facilitator accept a payment group whose buyer legs were signed by a mobile wallet over WalletConnect, rather than by a local key? | **ACCEPT** (Pera, 2026-09-15, app `771795120`) |

---

## Deployed artefacts

| Thing | Value |
|---|---|
| Admin account | `WSX572LTD52Y2KT6XYADEQFXF5XBV2QAYKURB43KJOME26D6QC5E7IFNLI` |
| Buyer account | `KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M` |
| App 1 (real USDC) | id `769608941`, account `D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY` |
| App 2 (test asset) | id `769609080`, account `NKEHV45S6UUFELAGKVGO7OUR6B2BETBNUVBHVEQJOWMKV6B22PGXK2DFRA` |
| TestNet USDC ASA | `10458941` (6 decimals) |
| Locally minted test ASA | `769609078` "Spike Test USD" / SPKUSD (6 decimals) |
| Facilitator fee sponsor | `ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA` |
| `share_price` on both apps | `10000` base units (0.01 at 6 decimals) |

Both applications were created with `create(usdc_asset_id, share_price)`, funded with 0.5 ALGO, and
opted into their asset by `opt_in_asset` before any payment was attempted.

## A blocker, and the substitution it forced

**Every public source of TestNet USDC (ASA `10458941`) is captcha-gated.** Checked: the asset
dispenser named in the vendored TypeScript reference (`asset-dispenser.testnet.algorand.network`) no
longer resolves in DNS; `bank.testnet.algorand.network` and `dispenser.testnet.aws.algodev.network`
both redirect to `lora.algokit.io/testnet/fund`, which dispenses ALGO only; the programmatic AlgoKit
dispenser API is ALGO-only; Circle's faucet accepted the request shape and then refused:

```
{"errors":[{"message":"ReCAPTCHA verification failed","path":["requestToken"],
  "extensions":{"code":"RECAPTCHA_ERROR"}}],"data":null}
```

Defeating a captcha was out of scope, so **the buyer holds 0 USDC** and no group paying in ASA
`10458941` can settle. The substitution: a locally minted 6-decimal ASA (`769609078`) and a second
application instance (`769609080`) bound to it, with the buyer funded with 5.0 units.

**What this does and does not cost the result.** The question under test is the *shape of the
transaction group*, which is orthogonal to which ASA the payment leg carries — the facilitator's
`/supported` advertises no asset allowlist for the algorand family, only a scheme, a network and a
fee-payer. And the real-USDC attempt (A1 below) independently confirms that the shape passed
validation against ASA `10458941`, failing only on the buyer's balance. What remains untested is
whether the facilitator applies USDC-specific handling somewhere past the point A1 reached; nothing
observed suggests it does, but this run cannot exclude it.

---

## Objective A — the extra application call

### A0 — facilitator-independent dry run (contract accepts the shape)

Run before spending any facilitator attempt, to separate "the group is malformed or the contract
rejects it" from "the facilitator refuses to cosign it". `tests/dry_run_join_group.py` submits *the
exact bytes* `agents/build_join_group.py` produces, substituting a locally held throwaway signer for
the fee-payer leg. Both orderings simulated clean and landed:

| Variant | Agreement | Group legs | Result |
|---|---|---|---|
| `fee_payer_first` | 1 | `[pay, axfer, appl]` | confirmed round 66520764 |
| `axfer_first` | 2 | `[axfer, pay, appl]` | confirmed round 66520771 |
| `fee_payer_first` | 3 | `[pay, axfer, appl]` | confirmed round 66521142, re-run to re-verify the signer after a later cleanup |

```
--- simulate ---
simulate OK (no failure-message)
  leg 2 logs: ['38SbGgAAAAAAAAABU5TDwxIk3YZ6TWG87dQsT3J1/8XS/TfmXbPzXEqragYAKgAgQeVJK+RJGe9G9Gto/ELVr7gNjlNk5jwMPr5cw4SpxiE=']
--- box state ---
box name (b64)=cF8AAAAAAAAAAQ==
box value (b64)=U5TDwxIk3YZ6TWG87dQsT3J1/8XS/TfmXbPzXEqragY=
box value decoded as address=KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M
```

So the AVM and the contract accept both orderings. Anything that failed later would be facilitator
policy, not a malformed group.

### A1 — real USDC, `fee_payer_first`, buyer holds 0 USDC

`python -m agents.join_client_manual --agreement-id 1 --variant fee_payer_first`, against the route
bound to app 1 and ASA `10458941`. The 402 handshake succeeded; the retry returned:

```
--- POST http://localhost:8000/escrow/join-spike (with PAYMENT-SIGNATURE) ---
status: 402
headers:
  date: Fri, 21 Aug 2026 07:30:09 GMT
  server: uvicorn
  content-type: application/json
  payment-required: <base64, decoded below>
  content-length: 2
body:
{}
```

The response body is the literal two bytes `{}`; everything the server has to say is in the
`payment-required` header, which base64-decodes to:

```json
{
  "x402Version": 2,
  "error": "Transaction simulation failed: transaction HTBGZGSWU562AC6K2TEAXGFQA2AAOYZFSRHCPBYTQBQ6CG7E2AXA: underflow on subtracting 10000 from sender amount 0",
  "resource": {
    "url": "http://localhost:8000/escrow/join-spike",
    "description": "Join the escrow's payer pool with a single payment.",
    "mimeType": "application/json"
  },
  "accepts": [
    {
      "scheme": "exact",
      "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
      "asset": "10458941",
      "amount": "10000",
      "payTo": "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY",
      "maxTimeoutSeconds": 300,
      "extra": {
        "decimals": 6,
        "feePayer": "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA",
        "tag": "x402-global-challenge",
        "genesisHash": "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
        "genesisId": "testnet-v1.0"
      }
    }
  ],
  "extensions": {
    "bazaar": {
      "info": {
        "input": {"type": "http", "bodyType": "json", "body": {}, "method": "POST"},
        "output": {"type": "json", "example": {"status": "settled"}}
      },
      "schema": { ...elided for length: the JSON Schema the route declares for its (empty) input body... }
    }
  }
}
```

**What this does and does not establish.** The payload reached **simulation** with asset `10458941`
declared in `accepts`, and failed there on the payment leg's *balance* — the buyer holds 0 USDC. That
places the request past payload decoding, past asset/requirements matching, and into execution
against the chain. So A1 rules out the facilitator specially refusing this group when the asset is
real USDC, which is the one thing the minted-asset runs cannot speak to.

It does **not**, on its own, establish that the 3-transaction shape is accepted: whether any
shape-policy check runs before or after simulation was not observed, and no rejection was seen either
way. The shape-acceptance claim rests on A2/A3, where the facilitator actually cosigned and settled
such a group — not on inference from this error message.

A1's role in the argument is narrow and worth stating exactly: **A2/A3 prove the shape is accepted;
A1 shows real USDC is not a special case.**

### A2 — funded asset, `fee_payer_first` — settled

Command: `RESOURCE_HOST=http://localhost:8001 APP_ID=769609080 python -m agents.join_client_manual --agreement-id 10 --variant fee_payer_first`

```
--- POST http://localhost:8001/escrow/join-spike (with PAYMENT-SIGNATURE) ---
status: 200
body:
{
  "status": "settled"
}
```

`payment-response` header, decoded:

```json
{"success": true,
 "payer": "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M",
 "transaction": "UOKTIAE7WXQ5UML65HH76GL4JHXDWNT3DFCE42N7SZC227J4VBZQ",
 "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="}
```

### A3 — funded asset, `axfer_first` — settled

Run as a separate, clearly distinct attempt to establish whether the facilitator constrains leg
order. It does not.

`RESOURCE_HOST=http://localhost:8001 APP_ID=769609080 python -m agents.join_client_manual --agreement-id 11 --variant axfer_first`

```
--- POST http://localhost:8001/escrow/join-spike (with PAYMENT-SIGNATURE) ---
status: 200
body:
{
  "status": "settled"
}
```

`payment-response` header, decoded:

```json
{"success": true,
 "payer": "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M",
 "transaction": "DXFLXSMHSNLCEWBFAIQT5DUCHJSH5BPD4OKHFDQXBTSODYGH7COQ",
 "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="}
```

Cross-checked to the same depth as A2, since A3 is what carries the ordering conclusion
(`python -m tests.verify_join_group --group WPCZfx/LzGz1IjrBHEFQacAXM+v+lPnNEC5ZkQw4POI= --box 769609080:11`):

```
=== group WPCZfx/LzGz1IjrBHEFQacAXM+v+lPnNEC5ZkQw4POI= ===
  axfer DXFLXSMHSNLCEWBFAIQT5DUCHJSH5BPD4OKHFDQXBTSODYGH7COQ sender=KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M fee=0
  pay   DDUMQZSEHGAE5A3WEDGCMIGCGV5GFX2HF22WCVTYWFH4LT7REXCQ sender=ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA fee=3000
  appl  VRRAC4GHLHUOMV33FDJ465SBG3NBCWN5WTB5NBWSAABHKGGZUYHA sender=KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M fee=0
  fee-payer leg signed by the expected party: ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA
PASS
=== app 769609080 box payers[11] ===
  payer = KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M
  Joined event in VRRAC4GHLHUOMV33FDJ465SBG3NBCWN5WTB5NBWSAABHKGGZUYHA: {"agreement_id": 11, "payer": "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M", "payment_txn_id": "DXFLXSMHSNLCEWBFAIQT5DUCHJSH5BPD4OKHFDQXBTSODYGH7COQ"}
PASS
```

Confirmed round 66520867. Note the leg order: the `axfer` is at index 0 and the facilitator's
fee-payer leg at index 1, the reverse of A2, and the `payment_index` the buyer supplied located the
payment leg correctly in both.

### On-chain cross-check — the part that makes it a settlement and not a simulation

The HTTP 200 alone proves nothing; the group was pulled back from the indexer independently.
Group `b3vgmbpoTkIoYyOwNpzRiGhRrqmzDJgidyoFfyujNOY=` (A2), confirmed round 66520845:

```
  pay   5M4FNVDZSLJ4TKXVNKMYBLMPLQUEIGA5J3FENJ6IZNRDZRXOHSZQ sender=ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA fee=3000
  axfer UOKTIAE7WXQ5UML65HH76GL4JHXDWNT3DFCE42N7SZC227J4VBZQ sender=KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M fee=0
  appl  KEJ2MNN3GOAKGFPESYX54TRJVDSBBP7DDYTUJGXT7BB3E3XTZDIA sender=KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M fee=0
  fee-payer leg signed by the expected party: ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA
PASS
```

Group `WPCZfx/LzGz1IjrBHEFQacAXM+v+lPnNEC5ZkQw4POI=` (A3) is the same three legs in the
`axfer_first` order, with the same facilitator-signed fee-payer leg
(`DDUMQZSEHGAE5A3WEDGCMIGCGV5GFX2HF22WCVTYWFH4LT7REXCQ`); its full dump is in the A3 section above.

"expected party" in that output is not "anyone other than the buyer" — the check compares the
fee-payer leg's sender against the address the facilitator advertises at `/supported`, and fails
otherwise. Run against a dry-run group, which carries a locally generated fee payer, the same command
reports:

```
FAIL: fee-payer leg was signed by O45FMX5JPQAILZYZXSKZIKAN2I2CHP4LGFWIZRVL6FJGWMV262W52TAOXA, expected ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA
```

so the PASS above is a positive identification of the facilitator, not the absence of the buyer.

Three things in that dump matter:

1. **The `appl` leg is on chain, inside the settled group.** The application call was not stripped.
2. **Leg 0 is a `pay` sent by the facilitator's own sponsor address**, carrying the pooled 3000
   microALGO fee for all three legs while the buyer's two legs pay `fee=0`. That address is exactly
   the one the facilitator advertises at `/supported`, and only the facilitator holds its key. This
   is the direct evidence of cosigning, and the check asserts that specific address rather than
   merely a different one.
3. The buyer's `axfer` and `appl` share a sender, so the contract's `payment.sender == Txn.sender`
   assertion held.

Contract state, read back independently:

```
=== app 769609080 box payers[10] ===
  payer = KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M
  Joined event in KEJ2MNN3GOAKGFPESYX54TRJVDSBBP7DDYTUJGXT7BB3E3XTZDIA:
    {"agreement_id": 10, "payer": "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M",
     "payment_txn_id": "UOKTIAE7WXQ5UML65HH76GL4JHXDWNT3DFCE42N7SZC227J4VBZQ"}
```

The ARC-28 `Joined` event's `payment_txn_id` equals the axfer id the facilitator reported settling,
which closes the loop between the HTTP response, the on-chain group and the contract's own record.

### Verdict A: ACCEPT

The facilitator accepts, cosigns and settles an atomic group carrying an extra buyer-signed
application call alongside the standard payment leg. It does not constrain the leg ordering: both
`[feePayer, axfer, appCall]` and `[axfer, feePayer, appCall]` settled. The buyer-supplied absolute
`payment_index` correctly identified the payment leg in both orderings.

Caveat, restated: the two settled groups paid in a locally minted ASA rather than ASA `10458941`,
for the faucet reason above. A1 is what carries the real-USDC half of the claim.

---

## Objective B — discovery

### Check 1 — the route declares the extension

The spec's command for this check is `curl -i <host>/<route> | grep -i bazaar`. **That command
cannot succeed against this resource server, for a reason unrelated to whether the route is
configured correctly**: under x402 protocol V2 the payment requirements travel in the base64-encoded
`payment-required` *header*, not in the response body, so the literal string `bazaar` never appears
in the raw response. The declaration has to be checked after decoding the header:

```
=== check 1: discovery extension declared on http://localhost:8001/escrow/join-spike ===
  extensions: ['bazaar']
  payTo: NKEHV45S6UUFELAGKVGO7OUR6B2BETBNUVBHVEQJOWMKV6B22PGXK2DFRA
  PASS: bazaar declared
```

The decoded `accepts[0]` also confirms the attribution tag and that the SDK **replaced** the route's
configured fallback `feePayer` with the facilitator's real sponsor address:

```json
{"scheme": "exact", "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
 "asset": "10458941", "amount": "10000",
 "payTo": "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY",
 "maxTimeoutSeconds": 300,
 "extra": {"decimals": 6, "feePayer": "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA",
           "tag": "x402-global-challenge",
           "genesisHash": "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=", "genesisId": "testnet-v1.0"}}
```

### Checks 2 and 3 — catalogue lookups

The **resources** endpoint caps `limit` below its own reported `total` — a single `limit=1000` call
returns 1000 of 1456 — so it is paged to exhaustion rather than queried once; a one-shot query there
would produce a false negative. The merchants endpoint reported a `total` of 90, within the `limit`
of 500 used, so one call covered it; it is paged the same way regardless, since the total is a
property of the catalogue on the day and not a guarantee. Resources are searched three ways
(resourceUrl substring, payTo anywhere in the record, derived `merchantId`) and merchants two
(`addresses.avm`, payTo anywhere), so a miss is not an artefact of one field being keyed differently
than expected. Verbatim output of
`python -m tests.verify_join_group --discovery http://localhost:8001`:

```
=== check 2: resource catalogued (payTo NKEHV45S6UUFELAGKVGO7OUR6B2BETBNUVBHVEQJOWMKV6B22PGXK2DFRA) ===
  scanned 1456 catalogued resources
  by resourceUrl substring 'localhost:8001':
    no match
  by payTo address anywhere in the record: no match
  by derived merchantId 'TktFSFY0NVM2VVVGRUxBR0tWR083T1VS': no match

=== check 3: merchant identity for NKEHV45S6UUFELAGKVGO7OUR6B2BETBNUVBHVEQJOWMKV6B22PGXK2DFRA ===
  scanned 90 merchants
  by addresses.avm:
    no match
  by payTo address anywhere in the record: no match

FAIL: not catalogued -- route declares discovery, so this is 'not catalogued (yet)', not 'never declared'
```

App 1 (`D4SAMDS7…GK7WMGIKRY`, merchantId `RDRTQU1EUzdTRFZPVTVPVEw1R0VaWDRS`) is likewise absent from
both catalogues, confirmed by running the same three searches against it ad hoc rather than through
the committed script, which only ever queries the payTo of the route it is pointed at. Nothing rests
on that result: app 1 never settled a payment, so its absence is expected either way.

### Attributing the failure

Four candidate causes, three of them excluded by evidence collected in this run:

| Candidate cause | Excluded by |
|---|---|
| Route never declared discovery | Check 1 passed — the `bazaar` extension is present in the 402 and therefore in the payload the facilitator received |
| No payment ever settled against it | Three settlements landed on this exact route (A2, A3, and the control below) |
| TestNet settlements are not catalogued | The catalogue does carry TestNet — see the survey below |
| The 3-transaction group shape is not catalogable | A standard 2-transaction settlement on the same route (`agents/plain_settle_fallback.py`, txid `7HQ5COAJN6PV3XQ26W4HGO2JMPGZPBY5AMSKMGC637DYFWQ3SWWQ`, HTTP 200) is equally absent |

The last two exclusions rest on population statistics, so the script computes and prints them rather
than leaving them as assertions. It emits this survey whenever the route is missing:

```
=== attribution survey (why might it be absent?) ===
  accepts entries by network (1490 entries across 1456 resources):
      1382  algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=  <- algorand mainnet
        62  algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=  <- algorand TESTNET
        33  eip155:8453
        12  solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp
         1  eip155:84532
  => TestNet accepts entries are catalogued (62 of them), so 'TestNet is excluded' does not explain the absence.
  merchant network entries (90 entries across 90 merchants):
        65  algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8=  <- algorand mainnet
        16  algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=  <- algorand TESTNET
         6  eip155:8453
         2  solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp
         1  eip155:84532
  catalogued resources served on a loopback host: 0
  => this route is served on a loopback host (http://localhost:8001) and no catalogued resource anywhere is. The resource being unreachable from outside is the leading explanation.
```

What remains is the resource URL. Zero of the 1456 catalogued resources are served on a loopback
host; every one is publicly resolvable. The route under test advertises
`http://localhost:8001/escrow/join-spike`, which the facilitator cannot reach or meaningfully
publish.

### Verdict B, first run (loopback host): INCONCLUSIVE — not catalogued, but the test is confounded

The route did not appear in either catalogue. What that does **not** mean is that the declaration is
broken: check 1 passes, the `bazaar` extension is present in the 402 and therefore in the payload the
facilitator received, and three settlements landed against the route.

It is recorded as inconclusive rather than as a failure because the run cannot separate the two
remaining explanations:

- **the resource URL is unreachable** — the route advertises `http://localhost:8001/...`, and no
  catalogued resource anywhere in the 1456 is served on a loopback host. This is the strongly
  favoured reading;
- **not catalogued *yet*** — re-checked several minutes after the last settlement with the same
  result, but no upper bound on indexing latency was established, so this cannot be excluded outright.

Either way the confound is the same: **the route was never reachable from outside the machine it ran
on**, which is not a property the production deployment will share. Treating this as evidence that
the discovery declaration fails would be reading a defect into what is really an artefact of the test
environment.

**What closes it:** re-run checks 2 and 3 against a publicly reachable deployment. That re-run is
the next section, and it closes this green.

### The public-host re-run — 2026-08-21

Both confounds were removed together: the same resource server, unchanged, redeployed to a public
HTTPS host and settled against in real TestNet USDC. Nothing about the contract, the route config or
the group shape changed.

| | Value |
|---|---|
| Public host | `https://earnest-spike-discovery.fly.dev` (Fly.io, region `cdg`, one machine, idle sleep disabled) |
| Application | `769608941`, account `D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY` |
| Asset | `10458941` — real TestNet USDC, not the minted substitute |
| Advertised `resource.url` | `https://earnest-spike-discovery.fly.dev/escrow/join-spike` |

The advertised URL had to be right before anything else could be interpreted. Behind a TLS
terminator the application sees plain HTTP, so uvicorn runs with `--proxy-headers
--forwarded-allow-ips *`; without them the 402 advertises `http://`, and the catalogue row is built
from that payload. Verified in the decoded 402 alongside `asset` `10458941` and
`extra.tag` `x402-global-challenge`.

Two settlements landed, each paying `10000` base units of ASA `10458941` into the application
account:

| Settlement | txid | group id | round |
|---|---|---|---|
| 3-txn join (`--agreement-id 30`, `fee_payer_first`) | `FBXKDAXLT47PXWRPNOJGVUV6C3UK2BJNPSP5XEZ4VOWSE3COKWYA` | `YLhEcRgx1JwhmaUCpNKbBlZ+ZITPNqrmrzNNCEYieV4=` | 66528418 |
| 2-txn control (`plain_settle_fallback`) | `L7WL73XEPEM7XPQZPEV7FUSDYFDHEMDGPWQYV2IDQLTIMSNYSFIA` | `GR1gJNcQGXF9jxs5t+DfI3jXC/NjGlBGhoS7tilht4U=` | 66528434 |

The join group's application call is `TE2XJVIP7J5CEQUPX624DEATMIYC4MORL5KSFCZQFIPUGUCYJBEA`, its
fee-payer leg `DWAPMDFBGJ7RLSLZQK43NXLFQP6X5TLVIVVNUG7SYQBSPOY7FD6Q` sent by
`ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA`, the facilitator's advertised sponsor.
`payers[30]` decodes to the buyer and the `Joined` event's `payment_txn_id` matches the axfer.

**This was also the first real-USDC settlement of the join group.** A1 above reached validation and
failed only on the buyer's balance, leaving open whether the facilitator applies USDC-specific
handling past that point. It does not.

#### Four timestamped catalogue observations

Each is `python -m tests.verify_join_group --discovery https://earnest-spike-discovery.fly.dev`.

| # | When | Result |
|---|---|---|
| 0 | `2026-08-21T13:00:01Z`, before any settlement | absent from both catalogues — the baseline |
| 1 | `2026-08-21T13:11:30Z`, immediately after settling | **catalogued** — resource row *and* merchant identity |
| 2 | `2026-08-21T14:12:49Z`, ~1h after | unchanged: same counts, same `firstSeen`/`lastSeen` |
| 3 | `2026-08-24T08:13:47Z`, ~67h after | still catalogued, `firstSeen`/`lastSeen` unchanged |

The plan called for the last check at ~24h; the workstation was shut down in between, so it landed
at ~67h instead — a longer persistence interval, which answers the same question with more margin.
`fly status` confirmed the machine was `started` throughout and its `Updated` timestamp never moved
off `2026-08-21T12:56:36Z`, so it never restarted and no interval is invalidated by a sleeping host.

At observation 1 the catalogued population moved 1456 → 1457 resources and 90 → 91 merchants:
exactly one new row in each, ours. By observation 3 it had grown to 1563 resources and 109
merchants, and our row was still there and unchanged through that churn. The resource row, verbatim
from observation 1:

```json
{
  "id": "UE9TVDpodHRwczovL2Vhcm5lc3Qtc3Bpa2UtZGlzY292ZXJ5LmZseS5kZXYvZXNjcm93L2pvaW4tc3Bpa2U",
  "resourceUrl": "https://earnest-spike-discovery.fly.dev/escrow/join-spike",
  "method": "POST",
  "description": "Join the escrow's payer pool with a single payment.",
  "mimeType": "application/json",
  "merchantId": "RDRTQU1EUzdTRFZPVTVPVEw1R0VaWDRS",
  "accepts": [{"scheme": "exact", "network": "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
               "asset": "10458941", "amount": "10000",
               "payTo": "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY",
               "maxTimeoutSeconds": 300,
               "extra": {"decimals": 6, "feePayer": "ZMFK2OI7ZBD2U27ISERZC4S6LKM6WMFJPZQ4MYNJDZ2VNBNMBA67RA22AA",
                         "tag": "x402-global-challenge",
                         "genesisHash": "SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=",
                         "genesisId": "testnet-v1.0"}}],
  "discoveryInfo": {"input": {"type": "http", "bodyType": "json", "body": {}, "method": "POST"},
                    "output": {"type": "json", "example": {"status": "settled"}}},
  "verifyCount": 1, "settleCount": 2,
  "firstSeen": "2026-08-21T13:10:30.908Z", "lastSeen": "2026-08-21T13:11:13.860Z"
}
```

and the merchant identity, which is the half that was most in doubt for an application account:

```json
{
  "id": "RDRTQU1EUzdTRFZPVTVPVEw1R0VaWDRS",
  "addresses": {"avm": "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY"},
  "resourceCount": 1, "totalVerifications": 1,
  "networks": ["algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="],
  "firstSeen": "2026-08-21T13:10:30.891Z", "lastSeen": "2026-08-21T13:11:13.860Z"
}
```

`merchantId` matches the value derived locally from the `payTo`, so the derivation rule in
`docs/specs/discovery-and-attribution.md` §6 holds for an application address.

**Indexing is effectively immediate.** The first settlement's 402 was issued at `13:10:24Z` and the
catalogue's `firstSeen` is `13:10:30.908Z` — about six seconds. The "not catalogued *yet*"
explanation the first run could not exclude is now excluded outright rather than merely bounded.

**`settleCount: 2` counts both settlements**, so the 3-transaction join group is catalogable as well
as settleable: the extra buyer-signed application call does not disqualify a payment from the Bazaar.
That also retires the fourth candidate cause above on direct evidence rather than by comparison with
a control.

#### The facilitator's discovery schema changed between observations

Between observation 2 and observation 3, with nothing changed on this side, two fields were renamed
or dropped:

| Record | 2026-08-21 (obs 1 and 2) | 2026-08-24 (obs 3) |
|---|---|---|
| resource | `verifyCount: 1`, `settleCount: 2` | `settleCount: 2` — `verifyCount` is gone |
| merchant | `resourceCount: 1`, `totalVerifications: 1` | `resourceCount: 1`, `totalSettlements: 2` |

The checks kept passing because they match on `resourceUrl`, `payTo` and `merchantId`, none of which
moved. But **the discovery records are not a stable schema**: fields appear, disappear and get
renamed on the facilitator's own schedule. Anything that parses them has to tolerate that.

The merchant counter deserves particular care. It did not simply get renamed — it changed what it
counts, from verifications (`1`) to settlements (`2`). A reader treating the two names as synonyms
would silently start reporting a different quantity.

#### What this run can and cannot attribute

It removed **two** confounds at once, not one: the loopback resource URL **and** the substituted
asset. The four-candidate table above omits the asset entirely, because in the first run it was
fully confounded with the URL — every settlement that could have triggered cataloguing paid in the
minted ASA precisely because the buyer held no USDC.

The result of this run alone is therefore: **a publicly reachable route paying real USDC into an
application account is catalogued and does receive a merchant identity.** Which of the two confounds
caused the original negative it could not say. Population evidence favoured the URL — zero of 1457
catalogued resources are served on a loopback host — but that was an inference, not a measurement.

The A/B below was then run to settle it, and the inference turned out to be wrong.

#### The A/B: public reachability is necessary, but not sufficient

Run 2026-08-24 to separate the two confounds. A second paid route was added to the **same server
process on the same public host**, declaring discovery identically, priced identically, settled with
the same 2-transaction control by the same buyer. It differs in exactly two things — what it charges
in, and where it pays:

| | `/escrow/join-spike` | `/escrow/join-spike-alt` |
|---|---|---|
| `payTo` | `D4SAMDS7…GK7WMGIKRY` (app `769608941`) | `NKEHV45S…22PGXK2DFRA` (app `769609080`) |
| asset | `10458941` — real TestNet USDC | `769609078` — locally minted SPKUSD |
| settlements | 2 | 2 |
| catalogued | **yes**, ~6s after the first | **no** |

The alt route's payments are on chain and uncontested:
`GUTNIK2HCXCB55NNY4X4PHLE5H54OTFGGPJREPZU5ZQHBUYVPPLA` and
`JZBUIGTEINNANIVKO2SNW5AXVSOGRUTA4JQSJHJFHEV2MFHJA2NQ`, the first in group
`PNJZ71su3ujrAAe6bvtAjs505ZXkPw3llkUyW1gR83w=` at round 66618417, each moving `10000` base units
into the application account. The facilitator returned HTTP 200 and `{"status": "settled"}` both
times.

Three catalogue checks across the following ~100 seconds found nothing — no resource row, no
merchant identity, no match on `payTo` anywhere in either catalogue. The same command run against
the sibling route in the same minute returned `PASS: catalogued`. The route that did get catalogued
had needed six seconds.

**So the loopback URL was not the whole explanation, and the first run's "strongly favoured reading"
was wrong.** A publicly reachable route that declares discovery correctly and genuinely settles can
still be absent from the catalogue. Public reachability is necessary — every catalogued resource is
publicly resolvable — but it is not sufficient.

What this A/B did **not** isolate: `payTo` and asset still vary together. An application account can
only receive an ASA it has opted into, and neither instance is opted into the other's, so serving one
account under both assets needs an admin `opt_in_asset` call first. That is the experiment that would
finish the job.

The leading hypothesis at the time was that **cataloguing is gated on the asset being one the
facilitator recognises**, sitting beside the earlier incidental finding that `/supported` advertises
no asset allowlist.

> **Withdrawn 2026-08-25 (Objective C).** The `-alt` route is catalogued, so the hypothesis is
> unsupported and the practical rule drawn from it — that anything priced in a test or bespoke asset
> "will settle and stay invisible" — is false. What the A/B actually measured was a **difference in
> cataloguing latency**: one route listed in six seconds, the other not listed within the ~100-second
> observation window and listed by the time it was next looked at. The confound the A/B was built to
> remove is still removed; what it could not do, on a two-minute window, was tell absence from
> lateness.

The `-alt` route was scaffolding, not a feature, and was removed from `api/server.py` once this
result was recorded — `api/server.py` declares one paid route again. Reproducing the A/B means
re-adding a second `RouteConfig` bound to a different application account and asset; the commit that
first added it is in this branch's history.

### Verdict B: PASS — an application-account `payTo` is catalogued and gets a merchant identity

Both halves work, and they persist: the row and the merchant identity were still intact ~67h later,
unchanged, while the surrounding catalogue grew by a hundred resources. The design's assumption that
`payTo` can be an escrow application account and still be discoverable and attributable is confirmed
on TestNet against a real facilitator.

The scope is narrower than "public host ⇒ catalogued", though. What is demonstrated is that a
publicly reachable route **priced in real USDC** is catalogued, six seconds after its first
settlement. Read the verdict as being about the route we actually ship, not about public hosting in
general.

> **Amended 2026-08-25 (Objective C).** This paragraph previously continued: *"The sibling route in
> the A/B above, identical but for its asset and application account, is publicly reachable, settles
> cleanly and is not catalogued at all."* **That is no longer true and should not be relied on.**
> The `-alt` row is in the catalogue, with a `firstSeen` equal to its own first settlement. The
> difference between the two routes is one of latency, not eligibility, and the asset-recognition
> hypothesis below is unsupported. See Objective C for the evidence and for what a negative
> catalogue result actually requires.

---

## Objective C — does a query-bearing request catalogue as one row?

Run 2026-08-25. The delivery-escrow backend takes its input in the query string
(`POST /pin?sha256=&size=`), because the discovery extension can describe a body or query
parameters but never a header. The server builds the catalogue identity from
`route_config.resource or adapter.get_url()`, and `get_url()` includes the query string — so left to
the default, every distinct `?sha256=…` would advertise a different resource and register its own
near-duplicate directory entry instead of one entry whose settlement count rises.

The question: **does pinning `RouteConfig.resource` collapse them into one row?**

### The mechanism, offline

Confirmed in-process before spending anything, with a control:

| | `resource.url` in the 402 |
|---|---|
| pinned | `https://earnest-spike-discovery.fly.dev/escrow/join-spike` — constant across queries |
| unpinned | `http://testserver/escrow/join-spike?sha256=aaa&size=1`, then `…?sha256=bbb&size=2` |

So the risk is real and the pin is what removes it. What remained to be shown is only that the
catalogue follows the payload.

### The settlement

One payment of `10000` base units (0.01 TestNet USDC) against
`POST /escrow/join-spike?sha256=e3b0c442…b855&size=1048576`, agreement id 31.

- transaction `2BN5JIQVNRNVW5GLIZAXVBC3HUEVBUQDXUWY6IIH533PCO63E23Q`
- payer `KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M`
- HTTP 200, `{"status": "settled"}`, at 2026-08-25T12:09:04Z

The 402 that preceded it advertised `resource.url` **without** the query string, over `https`, with
`maxTimeoutSeconds: 120`, the challenge tag, and the `bazaar` extension.

### Catalogue, ~6 seconds later

| | before (2026-08-21) | after |
|---|---|---|
| rows whose `accepts` carry our `payTo` | 1 | **1** |
| that row's `resourceUrl` | `…/escrow/join-spike` | unchanged, no query string |
| `settleCount` | 2 | **3** |
| `lastSeen` | 2026-08-21T13:11:13Z | 2026-08-25T12:09:10Z |
| query-bearing `resourceUrl` anywhere in the catalogue | 0 | **0** |
| catalogue size | 1647 | 1647 |

### Verdict C: PASS — a pinned `resource` collapses query-bearing requests into one row

The settlement merged into the existing row rather than creating a second one, and no
query-bearing `resourceUrl` exists anywhere in the 1647-row catalogue. The endpoint shape the
backend spec builds on — one paid route taking its input in the query — is safe to catalogue.

Cataloguing latency was again about six seconds, matching Objective B.

### Two findings that came with it

**1. The `accepts` block of a catalogued row is stale, and a settlement does not refresh it.** The
live 402 advertises `maxTimeoutSeconds: 120`; six seconds after a settlement against it, the
catalogued row still reports `300`, the value captured from the 2026-08-21 settlements. Settlements
bump `settleCount` and `lastSeen` and nothing else. Whether the ~daily branding crawl eventually
refreshes it is **untested** — re-check this row after 24h to tell "captured once at first sight"
from "refreshed on a slower cycle". Practical exposure is limited, because an agent pays against the
live 402 rather than the directory row, but an agent that pre-filters on catalogue metadata would be
filtering on a stale value.

**2. Objective B's bespoke-asset conclusion is wrong.** Verdict B states that the `-alt` route,
priced in a locally minted ASA, "settles cleanly and is not catalogued at all", and derives from it
that anything priced in a test or bespoke asset "will settle and stay invisible". That row is in the
catalogue now:

```
resourceUrl : https://earnest-spike-discovery.fly.dev/escrow/join-spike-alt
merchantId  : TktFSFY0NVM2VVVGRUxBR0tWR083T1VS
settleCount : 2
firstSeen   : 2026-08-24T08:33:05.874Z
lastSeen    : 2026-08-24T08:33:56.987Z
accepts     : payTo NKEHV45S…22PGXK2DFRA, asset 769609078
```

Note `firstSeen` — it coincides with the alt route's **first settlement**, not with any later event.
So the row was not created days afterwards; it either existed from the settlement and was not yet
being returned by `/discovery/resources` when the three checks ran, or it was created later with a
backdated `firstSeen`. This run cannot distinguish those.

What it does settle is that **"priced in a bespoke ASA ⇒ never listed" is false**, and with it the
leading hypothesis that cataloguing is gated on the facilitator recognising the asset. The observed
difference between the two routes is one of **latency**, not of eligibility: one was listed in six
seconds, the other was not listed within ~100 seconds and is listed now.

The methodological lesson is the sharper half: **a negative catalogue result from a ~100-second
window is not a negative.** Objective B drew a rule from three checks inside two minutes, and the
rule did not hold. Any future absence claim needs a window measured in hours and a re-check before
it is written down.

This does not disturb Verdict B's positive half, which is what the design depends on: an
application-account `payTo` is catalogued and receives a merchant identity. It disturbs only the
scope paragraph beneath it and the derived guidance in
`docs/specs/discovery-and-attribution.md` §6, both corrected in the commit that adds this section.

---

## Objective D — does the facilitator accept a browser-wallet signature?

Run 2026-09-15. Every settlement recorded above was signed by a **local private key held in a
script**. The browser client is built on `@txnlab/use-wallet-react` with the Pera and Defly
adapters, where the buyer's signature is produced by a mobile wallet over WalletConnect and
returned to the page as bytes.

The question: **is a payment group whose buyer legs were signed that way accepted, cosigned and
settled by the real facilitator, on the same `[feePayer, axfer, join]` group a local key already
gets accepted for?**

It is the assumption the whole browser-first pool design rests on, and nothing had tested it.

### Why it is not self-evident

The facilitator sees bytes and cannot tell how they were produced, so the interesting failure is
upstream of it: a wallet may **refuse** a three-transaction group whose first leg belongs to a
stranger and is deliberately left unsigned, or may **return** something other than what it was
handed. Two shapes are already known — one slot per transaction, or only the signed ones,
compacted — and `web/src/wallet.tsx` discriminates them by array length, throwing rather than
guessing, because scattering one shape as though it were the other puts a transfer's signature on
an application call and settles as a malformed group.

### Stage 1 — the signature, before any money

Run first and separately, because it needs no pool, no server and no USDC, and because a failure
here would have been the whole verdict at no cost. The page built a real group with the production
builder, asked Pera to sign indexes `[1, 2]`, and submitted nothing; `web/src/spike/probe.ts`
decoded what came back and compared it against what was sent.

```json
{ "shape": "aligned", "rawLength": 3, "groupLength": 3, "indexesToSign": [1, 2],
  "legs": [
    { "index": 0, "wasAskedToSign": false, "signed": false,
      "bytesUnchanged": null, "signatureValid": null },
    { "index": 1, "wasAskedToSign": true,  "signed": true,
      "bytesUnchanged": true, "signatureValid": true },
    { "index": 2, "wasAskedToSign": true,  "signed": true,
      "bytesUnchanged": true, "signatureValid": true } ],
  "pass": true, "ed25519Available": true }
```

Three separable results, in increasing order of what they establish:

1. **Pera 5.0.0 returns the aligned shape** — one slot per transaction in the group, `null` for the
   one it was not asked to sign. `wallet.tsx` has always said so, but on the strength of reading the
   adapter's source; this measures it. Leg 0 came back untouched, which is what leaves room for the
   facilitator's own signature.
2. **The transaction bytes survived the round trip.** Each signed blob was decoded and its
   transaction re-encoded: byte-identical to what the page handed over. The wallet changed nothing —
   no fee, no note, no group id.
3. **The signatures verify**, checked in-page with WebCrypto Ed25519 over `"TX" || txnBytes`
   against the wallet's own public key.

Together those are the mechanical reason Objective D's settlement was always going to work: the
facilitator received bytes it could not distinguish from a locally signed group, and Objective A had
already settled that case.

One thing this record does not capture: the probe reports no agreement id, so the JSON above does
not say whether the group signed here was one that *could* have settled. Worth adding before the
probe is re-run against another wallet.

### The settlement

One seat in a five-seat `hash` pool, agreement id **25** on application `771795120`, share price
`100000` base units (0.10 TestNet USDC). The buyer was a **Pera mobile wallet**,
`QWUKD4ZGIAKIZSMB43IOHIW6MPARAU3BQFHRTSEPMAZXNKGIR4Q7NJL3YU`, connected over WalletConnect; the
page asked it to sign group indexes `[1, 2]` and left index 0 as bare msgpack for the facilitator.

The 402 that preceded it carried its requirements in the `PAYMENT-REQUIRED` header with an empty
body, and advertised `agreementId: 25`, `seatsLeft: 5`, `amount: "100000"`, `payTo` the application
account, the facilitator's sponsor as `feePayer`, the challenge tag, and
`commitSha256: fa3c8f3bee9291e2bb7ce78070d74e6c8ce307aa4651df9d2d2a7cef3e5eb3c7`.

### On-chain cross-check — read back from the indexer, not from our own logs

Group `34bsRe9Q4QDXjKrp57C/C3globwJLqxqVBFFaCgnuSw=`, confirmed round **67330143**.

```
[0] pay   LEHIYX6KPNGUX63QSDEG477Q6LBSO3I7ABVPACCCMGMQPAT3LN7Q sender=ZMFK2OI7…BA67RA22AA fee=3000
[1] axfer W64MUEMDDGJ6AL5KHNZDLD7E5XV7O4UACS7XMRYNQTYTEXXZZXZA sender=QWUKD4ZG…IR4Q7NJL3YU fee=0
[2] appl  IDTKZVY4BGZ7BXX7OXZNOZLILBACXRN2LJ7NLUZLVKXHOJPLGDTQ sender=QWUKD4ZG…IR4Q7NJL3YU fee=0
```

Leg 0 is the facilitator's own sponsor carrying the pooled fee — `PASS`, and a positive
identification rather than a mere absence of error. Legs 1 and 2 are the wallet's, at zero fee.

Contract state, read from the boxes:

```
agreement 25: seats 1/5, state OPEN, total_held 100000
  seat 0: QWUKD4ZGIAKIZSMB43IOHIW6MPARAU3BQFHRTSEPMAZXNKGIR4Q7NJL3YU paid=100000
  seats 1-4: empty

Joined: {agreement_id: 25, payer: QWUKD4ZG…IR4Q7NJL3YU, amount: 100000, seat: 0,
         payment_txn_id: W64MUEMDDGJ6AL5KHNZDLD7E5XV7O4UACS7XMRYNQTYTEXXZZXZA, provenance: 0}
```

The event's `payment_txn_id` is leg 1's transaction id, so the seat the contract credited is tied
to the transfer the wallet signed, and the buyer's USDC balance fell by exactly the share price.

### Verdict D: ACCEPT — a mobile-wallet signature settles exactly as a local key's does

The facilitator verified, cosigned and submitted a group whose two buyer legs were signed by Pera
over WalletConnect, and the contract credited the seat to the wallet's own address. The browser-first
design's blocking unknown is closed, and the pool front end is a browser screen rather than a
command-line join with a browser attached.

Stage 1 explains why: the wallet returned the same bytes with valid signatures, so there was
nothing left for the facilitator to object to.

Caveat, restated: this is one wallet (Pera) on one adapter version
(`@txnlab/use-wallet-*` 5.0.0, `@perawallet/connect` 1.6.0). Defly is configured and **untested**,
and `wallet.tsx`'s length-discrimination is what would absorb a different return shape from it —
now known to be needed for at least one shape, since Pera's is the aligned one and the compacted
branch remains exercised only by tests.

### Three findings that came with it

**1. The browser client's HTTP layer had never met a real server, and was wrong twice.** It read
the 402's requirements from the JSON body, where a V2 server puts them in the `PAYMENT-REQUIRED`
header and leaves the body literally `{}` — so every quote would have concluded the server offered
nothing it could pay. And it sent the bare `{paymentGroup, paymentIndex}` as the payment signature,
where the facilitator's decoder needs that object wrapped with `x402Version`, the accepted
requirements and the resource. Either alone would have produced a failed run that said nothing
about wallets. Both are fixed, and the accepted requirements are now echoed back as the raw decoded
object rather than re-spelled from a parsed struct: the facilitator compares what it is sent
against what it issued, so a dropped optional field is a settlement that fails for a reason no log
points at.

**2. A browser cannot reach the paid route at all without the two sharing an origin.** The server
sets no CORS headers, and a `POST` carrying `PAYMENT-SIGNATURE` is not a simple request — it
preflights, and the preflight is answered `405`. Even a success would be unreadable, because
`fetch` cannot see `PAYMENT-REQUIRED` without `expose_headers`. In production the client is served
under `/app` by the same application and the question does not arise; in development the dev server
now proxies the paid routes so it does not arise there either. Relaxing the server for a case
production does not have would have been the wrong repair.

**3. `api/server.py` cannot serve a loopback host**, because every route's advertised `resource`
must be `https` and must start with `PUBLIC_HOST`. That is correct for a deployment and impossible
for `127.0.0.1`, so anything local goes through the `api.app.create_app` seam, as the circuit test
always has.

---

## Incidental findings

1. **`X-PAYMENT` is not the header to send.** The installed SDK's V2 protocol reads
   `PAYMENT-SIGNATURE` on the request and writes `PAYMENT-REQUIRED` / `PAYMENT-RESPONSE` on
   responses; `X-PAYMENT` is V1 legacy and is never consulted.
2. **`feePayer` configured on a route is only a fallback.** The server replaced the value with the
   facilitator's sponsor address, discovered from `/supported`, before emitting the 402. A route can
   be stood up without knowing a valid sponsor address.
3. **The facilitator sponsors the whole group's fees, not just the payment leg.** It signed a
   fee-payer leg carrying 3000 microALGO — 1000 per leg for all three, including the application
   call — so the extra call cost the buyer no ALGO.
4. **`/supported` advertises no asset allowlist** for the algorand family; a route naming an
   arbitrary TestNet ASA settled without complaint.
5. Both discovery endpoints paginate and cap `limit` below `total`; any check that queries them once
   can report a false negative.
6. **`payTo` is not a top-level field on a catalogue row.** It lives inside each row's `accepts`
   entries. A filter written as `row["payTo"] == ours` matches nothing, and the result reads as
   "our resource was dropped from the catalogue" — a far more alarming conclusion than "the query
   was written against the wrong shape". Row keys as of 2026-08-25: `accepts`, `description`,
   `discoveryInfo`, `firstSeen`, `id`, `lastSeen`, `merchantId`, `method`, `mimeType`,
   `resourceUrl`, `settleCount`.
7. **The counter has now been renamed twice.** `verifyCount` (2026-08-21) → absent → `settleCount`
   (2026-08-25), and the merchant record's `totalVerifications` became `totalSettlements`. The
   standing rule holds: match on `resourceUrl`, `payTo` and `merchantId`, never on a counter.
8. **A catalogued row survives its host going away.** The Fly app was deleted at some point after
   2026-08-21 and the resource row was still present, with its counters intact, when the host had
   been unreachable for days. Cataloguing is not a liveness signal, and a row's presence is not
   evidence that the endpoint behind it answers.
9. **Operational, for redeploying this spike.** The Fly app had to be recreated
   (`fly apps create earnest-spike-discovery`) — it no longer existed. On a machine behind a
   TLS-intercepting proxy, `fly deploy` fails in the depot builder with
   `x509: certificate signed by unknown authority` after two `deadline_exceeded` retries; the
   working path is **`fly deploy --depot=false --remote-only`**, which uses the legacy remote
   builder. A locally-built deploy (`--local-only`) needs Docker Desktop running.
   The first `/healthz` probe after a deploy takes several attempts while DNS propagates.

## Reproducing

`tests/testnet_join_spike.md` is the ordered runbook. `tests/verify_join_group.py` re-runs the three
checks above:

```bash
# real settlements: the expected fee payer defaults to the facilitator's /supported sponsor
python -m tests.verify_join_group --group b3vgmbpoTkIoYyOwNpzRiGhRrqmzDJgidyoFfyujNOY= --box 769609080:10
python -m tests.verify_join_group --group WPCZfx/LzGz1IjrBHEFQacAXM+v+lPnNEC5ZkQw4POI= --box 769609080:11

# dry-run groups were not cosigned by the facilitator, so name their own signer
python -m tests.verify_join_group --group F/lGdGUs23nWyxy3DwbcOki7KcAz8aYRJRh11B12wps=   --expected-fee-payer O45FMX5JPQAILZYZXSKZIKAN2I2CHP4LGFWIZRVL6FJGWMV262W52TAOXA

# discovery, including the attribution survey when the route is absent
python -m tests.verify_join_group --discovery http://localhost:8001
```
