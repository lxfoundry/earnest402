# Delivery-escrow client

> **Status: specification.** Written 2026-08-25.
> The buyer side of the single-payer `hash` delivery escrow: a browser client and a headless agent
> client, both paying the same route.
> Its sibling `docs/specs/delivery-escrow-backend.md` specifies the server this talks to, and
> `docs/specs/discovery-and-attribution.md` governs how that route is catalogued. This document
> restates the facts it depends on rather than assuming either has been read, but does not
> re-specify server behaviour.

---

## 1. Scope

The buyer side of the **single-payer `hash` delivery escrow**: one buyer, one file, one seat.

Out of scope: the edition pool, a multi-seat `hash` agreement sold through its own paid route, with
no leg 2 and no job lifecycle. Its buyer interface is specified in `docs/specs/pool-client.md`. The
two share one bundle and the configuration of §7, and nothing else in this document applies to it.

Two clients are in scope, because they share the part that is hard:

| Client | Language | Signer | Purpose |
|---|---|---|---|
| **Browser** | TypeScript, React + Vite, served at `/app` | a wallet, via `@txnlab/use-wallet` | a person buys a pin |
| **Agent** | Python, in `agents/` | a mnemonic | a published, runnable example |

## 2. What the client is paying for, and what it must know

The server exposes **one paid route that is the entire purchase**:

```
POST /pin?sha256=<64 hex chars>&size=<bytes>
```

The inputs travel in the **query string** because the price callable can read path, method, query
parameters and headers but never a body, and because a discovery declaration describes a body or
query parameters and never a header — a header-borne input would be invisible in the catalogue row.

Four facts follow from the server design and constrain everything below:

1. **The unpaid pass creates the agreement.** The first request earns a 402 *and* commits an escrow
   agreement on chain, keyed idempotently on `(sha256, size)`. The clock starts there, not at
   payment.
2. **The commitment is `sha256` of the file bytes, never a CID.** A CID depends on chunker, codec,
   CID version and layout, so a predicted one and a produced one may legitimately differ. The client
   therefore computes `sha256` locally and **never derives, predicts or asserts a CID**.
3. **The 402 carries what the payment needs**: price, `payTo` (an application account), and in the
   price's `extra`, `feePayer` and `agreementId`.
4. **Settlement is a three-transaction group**, not the library's standard two.

## 3. The sequence

```
browser   user picks a file; sha256 computed locally; the file stays in the page
  -> POST /pin?sha256=…&size=…                                  (no payment header)
  <- 402  price, payTo = application account, extra.{feePayer, agreementId}
browser   build [feePayer, axfer, join(agreementId, 1)], sign indexes 1 and 2
  -> POST /pin?sha256=…&size=…   PAYMENT-SIGNATURE + the group          LEG 1
  <- 200  {jobId, agreementId, deadline}
browser
  -> POST /pin/{jobId}/content                                          the bytes
  <- 200  {cid, gatewayUrl}          (after the server's leg 2 and verification)
```

The query string **must be byte-identical on both passes**. The server distinguishes the passes by
the presence of the payment header and looks the agreement up on the second; a differing query
string is a different job.

## 4. Two clients, one transport

The browser and the agent must produce **byte-identical transaction groups** from identical inputs.
This is a testable property, not an aspiration: see §11.

The group builder is duplicated in TypeScript and Python rather than shared, because the Python
builder already exists and has settled real groups. The duplication is made safe by golden vectors
rather than by a shared package.

**Byte-identity across the two SDKs is confirmed, not assumed.** A three-leg group built from
identical inputs by `py-algorand-sdk` 2.12.0 and `algosdk` 3.7.0 encodes to identical msgpack on all
three legs, canonical field ordering included. The premise the duplication rests on therefore holds
at the encoding level, and what the golden vectors defend against is drift in the *inputs* — a
recomputed fee, a changed note, a dropped box reference — rather than a disagreement between the
libraries.

## 5. Module boundaries (browser)

```
web/src/
  payment/
    buildJoinGroup.ts   pure: no wallet, no network, no DOM
    schemeClient.ts     SchemeNetworkClient: buildJoinGroup + signer + encoding
  machine.ts            pure: (state, event) -> (state, effects)
  api.ts                the four calls, over the payment-wrapped fetch
  hash.ts               File -> sha256 hex, Web Crypto
  config.ts             network parameters; no network constants live anywhere else
  wallet.tsx            use-wallet provider and connect UI
  ui/                   screens
```

`buildJoinGroup` and `machine` carry essentially all the risk and are both pure, so both are
exhaustively testable in Node with no browser, no wallet and no network. Everything else is thin.

The client is built to static assets and served by the existing application at `/app`, same origin
as the paid route. `/` is a separate, hand-written static page: it is read by a crawler before it is
read by a person, and a script-rendered shell serves a crawler nothing.

## 6. The payment group

The standard fee-abstracted group is two transactions. This one is three, because the buyer's seat
is claimed by an application call that must be atomic with the payment.

```
index 0   feePayer   self-payment, amount 0, unsigned — the facilitator cosigns
index 1   axfer      buyer -> payTo, the 402's amount and asset, fee 0
index 2   appCall    join(agreementId, payment_index=1), fee 0
```

- **`paymentIndex` is 1** — the axfer's absolute position in the group.
- **Signed indexes are 1 and 2.** Index 0 is left unsigned. One wallet prompt covers both.
- **Fees are pooled onto index 0** at three times the live minimum fee, read from suggested
  params. Never a hardcoded constant: a client that hardcodes the fee breaks on any protocol change.
- **The application call carries two box references**, both against its own application: the
  agreement box, prefix `a`, and the roster box, prefix `r`, each followed by `agreementId` as eight
  big-endian bytes. Two, not one: `join` reads the agreement record to check state, deadline, price
  and seat count, and reads *and writes* the roster to refuse a payer who already holds a seat. A
  call naming only one of them fails on the unnamed box.
- **The note fields are part of the encoding**, so they are inputs rather than internals: the
  fee-payer leg carries `x402-fee-payer-<ns>` and the transfer carries `x402-payment-<ns>`, matching
  what the reference client writes. They are supplied to the builder rather than generated inside
  it — a builder that stamps its own timestamp cannot be compared byte for byte against anything,
  which would cost the golden vectors of §11 their whole purpose.
- **Ordering is `[feePayer, axfer, appCall]`.** The facilitator is known to accept both this and
  `[axfer, feePayer, appCall]`; this one is fixed so both clients agree.

The signature is `buildJoinGroup(params) -> { txns, paymentIndex, indexesToSign }`, taking suggested
params as an argument rather than fetching them, which is what keeps it pure.

The wire payload is `{ paymentGroup: string[], paymentIndex: number }`, where `paymentGroup` is a
mixed array of base64 msgpack transactions — one unsigned, two signed. That encoding is the most
likely place for the two implementations to diverge silently, which is why §11 asserts it directly.

## 7. Configuration, and one assertion

Network parameters are configuration and appear nowhere else in the source: CAIP-2 network, payment
asset id, **application id**, facilitator URL, resource host, the maximum accepted file size, an
algod URL and an indexer URL. This is what lets one build run against LocalNet, TestNet and MainNet.

The application id must be configured because it cannot be recovered from the 402. The 402 carries
`payTo`, the application *account*, and deriving an account from an id is one-way.

The algod and indexer URLs are here because the bundle is shared: the pool interface reads the
chain directly, and `docs/specs/pool-client.md` §4 specifies which of the two answers what. They are
configured separately because they answer different questions and one does not stand in for the
other.

**Before building any group, the client asserts `payTo === getApplicationAddress(config.appId)`.**
One local derivation, no network call. It makes it structurally impossible to build a `join` call
against one application while paying another — the failure mode of a client left pointed at a
superseded deployment, which would otherwise move money into the wrong contract.

The client must also refuse a file larger than the configured maximum **before** the unpaid pass.
The server bounds its exposure by file size, and a file over that limit cannot complete; quoting it
first would commit an agreement, and its deposit, to a job that is already lost.

## 8. The state machine

A pure reducer, `(state, event) -> (state, effects)`, with effects executed by a runner. The branch
count and the cost of getting a branch wrong both justify testing it exhaustively offline.

**Two URL shapes, matching the two server routes, and standing follows from which one is open:**

- `/app/j/{jobId}` — the **owner** view. Holding the `jobId` is the proof of having paid; it is
  minted with the 200 and never appears in a 402.
- `/app/h/{sha256}` — the **watcher** view, reachable by anyone holding the bytes, carrying no claim.
  This route arrives with the watcher surface in R2 (§12); R1 resolves the same situation to a
  single action and needs no second page.

**Resting states:**

| State | Meaning |
|---|---|
| `CONNECT` | no wallet connected |
| `CHOOSE` | connected; awaiting a file |
| `QUOTING` | unpaid pass in flight |
| `CONFIRM` | 402 in hand; group built; wallet prompt open; funding window counting down |
| `UPLOADING` | owner; sending bytes |
| `WORKING` | job is `FUNDED`, `RECEIVED` or `PINNED` |
| `DONE` | `RELEASED`, CID available |
| `ENDED` | `ABANDONED` or `REFUNDED` |
| `DECIDE` | a settlement attempt failed and the hash query has resolved why |
| `NEED_FILE` | the page has a job but no bytes, and the next action requires them |

**Hash early, quote late.** Hashing is local and free; the unpaid pass commits an agreement and
starts the funding window. So the client hashes on file selection even with no wallet connected, and
holds at `CONNECT` with the digest ready. The unpaid pass fires only once a wallet is connected, so
the window is not spent on a wallet handshake.

**The funding window is short** — server policy, 120 seconds by default — and it elapses between the
unpaid pass and settlement, which is exactly `CONFIRM`. The client shows it counting down, because
it explains both the urgency and the failure if it lapses.

**No speculative unpaid passes.** Each one commits an agreement and parks its deposit. The client
issues one per user-initiated attempt: never on hover, never on a retry without backoff.

## 9. The resolve fork

**Every settlement failure routes through the hash query first, and never to a raw error.** The
client does not classify the failure; it asks what actually happened:

```
GET /pin/by-hash/{sha256}
```

| Response | Client state | Action offered |
|---|---|---|
| `QUOTED` | quote still open, nobody took the seat | **retry payment** — reuses the agreement, no new deposit |
| `FUNDED` / `RECEIVED` / `PINNED` | another buyer holds the seat; their job is live | buy your own pin (and, from R2, watch) |
| `RELEASED` + `cid` | these bytes were pinned recently | show the CID; buy your own pin |
| `ABANDONED` / `REFUNDED` | a previous attempt ended | re-quote |
| `404` | no recent job | re-quote |

Three properties make this the right shape:

- **It needs no error taxonomy from the server.** It stays correct whatever error shape the paid
  route returns, and requires no coordination to keep correct.
- **It covers the common case, not just the rare one.** The two-buyer race needs byte-identical
  files quoted inside the same window and is rare. The *same* buyer retrying after a wallet
  rejection, a dropped connection or a lapsed window is not, and `QUOTED` turns that into a retry
  rather than a second deposit.
- **It is bounded by the server's recent-release window** (default 3600 seconds), past which the
  route is a 404. The client therefore cannot make a claim about a file pinned long ago, which is
  correct: the worker's retention terms are unverified, and no durability claim may be made until
  they are read.

## 10. Standing, and what a watcher may be shown

A watcher has **no agreement, no deadline and no claim**. The server hands the hash view nearly the
same fields as the owner view, including the deadline — but that deadline belongs to the escrow
protecting the buyer. **Rendering it to a watcher would promise something they will never receive.**

So the watcher view replaces the escrow fields with a single statement of who is protected, and
keeps the buy-your-own action present throughout rather than only after a failure:

- while working — the bytes, the state, and that this job's escrow protects its buyer, not them;
- on a terminal failure — copy written for that case, stating that the job ended without a pin, that
  they were never charged, and that nothing was held for them.

That failure branch is the only moment a watcher learns what "no claim" meant, so it may not fall
through to a generic error.

**A watcher view must never render an escrow row.** This is a correctness property with a test
(§11), not a stylistic preference.

## 11. Test strategy

Layered the same way the server is: pure logic offline, real facilitator only for the question only
it can answer.

**Offline — Node, no browser, no wallet, no network.**

- **`buildJoinGroup` against golden vectors dumped from the Python builder, asserted byte for
  byte.** This is the test that makes two implementations safe, and it is the one to gate merges on.
  Identical inputs — suggested params, addresses, asset, amount, ids — must produce identical
  encoded groups.
- **The reducer, exhaustively** over states and events. One assertion named explicitly because a bug
  there costs money every time it fires: **never re-quote while the agreement is still open.**
- `hash.ts` against known vectors; the `payTo` assertion; the oversize-file refusal; and
  configuration guards that refuse to start on a missing or malformed network configuration.

**Component level.** Screens render correctly per state, including the property test that **a
watcher view renders no escrow row** — the kind of invariant a well-meaning refactor reintroduces.

**LocalNet integration.** The server's stub facilitator and its worker and gateway fakes put the
whole flow under test — 402, group construction, settlement, upload, CID — with no external
dependency and no real money. `use-wallet` provides KMD and mnemonic connectors, so a headless run
signs without a wallet application. Both the happy path and every branch of §9 are automatable here.

**TestNet, manual, once.** The real facilitator cosigning a real three-transaction group, signed
through a real wallet interface. Not automatable, and not automated.

**MainNet.** The client participates in the server's single human-triggered rehearsal rather than
running an exercise of its own. Irregular and human-triggered: nothing on a schedule, and no
continuous-integration job settles anything.

**Deliberately not tested:** wallet provider internals; the worker's reliability, which the server
prices as risk rather than asserting; and anything that would require a CID to be predictable.

## 12. Delivery phases

**R1 — an external buyer can complete a purchase.**

Wallet connect; file selection and local hashing; the unpaid pass, group construction, signing and
settlement; byte upload; the addressable owner job page surviving reload, including re-selection
when the page has a job but no bytes; the funding-window countdown; oversize refusal; the hash query
and the resolve fork of §9; configuration and the `payTo` assertion; golden-vector and reducer tests
in continuous integration. Plus the Python agent client, which is the smaller piece of work and the
more likely to be run by someone who already holds funds.

In R1 the race resolves to a single action — another buyer holds the seat, buy your own pin. That is
the server's existing behaviour, and it settles.

**R2 — the surface that explains itself.**

The watcher view of §10 and the *watch* option in the fork, with the cost-and-claim comparison that
makes the difference legible; per-job receipts and chain links; refund detail beyond the terminal
state.

**Neither phase includes** the edition pool's interface, which is specified in
`docs/specs/pool-client.md` and delivered on its own; a user-facing network switch (the
configuration in §7 supports one; whether to offer it is a product decision, not an engineering
one); or persisting file bytes across a reload.

## 13. Open items

- **Where the maximum file size is published.** The client must know it to refuse early (§7), and a
  buyer agent needs the same fact for the same reason. Mirroring a constant into client
  configuration works but will drift; the paid route's discovery input schema is the better home.
  Unresolved with the server side.
- **Whether a reload between settlement and upload is common enough** to justify persisting bytes in
  IndexedDB rather than re-prompting for the file. Re-prompting is specified; measure before
  changing it.
- **Whether the worker deduplicates a second pin of identical bytes.** A CID is content-derived so
  it plausibly returns the same one, but nothing here may depend on that, and the worker's
  size-to-price curve has one measured point.
