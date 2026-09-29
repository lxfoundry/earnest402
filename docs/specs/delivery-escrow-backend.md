# Delivery-escrow backend

> **Status: specification.** Written 2026-08-24, superseding the 2026-08-22 draft of the same name.
> Restates the design facts it depends on rather than citing other documents, so it stands alone.
> Its sibling `docs/specs/discovery-and-attribution.md` governs how a paid route is catalogued and
> attributed; this document configures the routes and does not restate those rules.

---

## 1. Scope

The **single-payer `hash` delivery escrow** in full: one buyer, one deliverable, `max_seats == 1`.
§4c covers the second paid product this server serves alongside it: a **multi-seat `hash`** pool
seat, `POST /index`, sharing this server's process and this application's one `payTo` with the
delivery escrow. The two are covered in one document because they are one deployment -- the startup
guards, the discovery declaration and the challenge tag are checked across both together, and a
config change to one is never safe to reason about without the other.

A pool seat is still condition `hash`, never `quorum`: its release still depends on a delivered
byte-for-byte match against a commitment fixed at creation, exactly as the single-payer escrow's
does. What makes it a pool rather than a single-payer purchase is only `min_seats > 1`: several
buyers fund one shared agreement instead of one buyer funding their own. Unlike the single-payer
escrow, this server runs no leg 2 and no job lifecycle for a pool seat — §4c states what it does
run, and where it differs from the delivery escrow, precisely.

**`POST /pin` is served only while `PIN_ROUTE_ENABLED` is on, and never on MainNet.** Its delivery
half — the upload, leg 2 and the release this document specifies — is not built yet. A deployment
serving the route anyway would make the admin key sign a `create_agreement`, deposit and fee
included, for every unpaid quote, and would take payments that could only ever end in a refund. With
the switch off, which is the default, `POST /index` is the whole paid surface: `/pin` answers 404 under
every spelling the payment middleware folds onto it, and the server holds no signing key at all —
boot refuses while `ADMIN_MNEMONIC` is set, the escrow client is read-only, no job store is opened,
and `PRICE_MICRO_USDC` is not required. The boot guard refusing the switch on MainNet (§5) goes when
the delivery half is built.

A **`quorum`**-conditioned pool -- released permissionlessly once enough distinct payers have joined,
with no delivered bytes at all -- is a different release condition this server's contract also
supports, and is out of scope for this document entirely. It has no leg 2, no float and no job
lifecycle, so folding it in here would add branches without sharing much.

The worker for v1 is a **pay-per-use IPFS pinning endpoint** that is itself an x402 resource:
`POST https://api.algofile.io/api/algofile/upload`, Algorand MainNet, USDC (ASA 31566704),
`multipart/form-data` with a `file` field, returning `{success, cid}`. Its price is derived from the
request size and returned in the 402 (`priceUsd`, `fileSizeGb`), with an observed floor of `25000`
(0.025 USDC) for a 6-byte probe. Verified answering a valid 402 on 2026-08-22.

## 2. The sequence

The escrow contract fixes `commit_hash` at `create_agreement`, offers no method to bind or update it
afterwards, and `join` names an `agreement_id` — so **the agreement exists before the buyer pays, and
the deliverable's hash is known before that.** Any design in which the worker announces the hash
after being commissioned cannot be built.

```
browser        user picks a file; sha256 computed locally, the file stays put
  -> POST /pin?sha256=<64hex>&size=<bytes>                          (no payment header)
backend        create_agreement(commit_hash=sha256, share_price=p, deadline, ...)  [admin key]
  <- 402        price p, payTo = app account, extra.agreementId
browser
  -> POST /pin?sha256=…&size=…
     PAYMENT-SIGNATURE + [feePayer, axfer p, join(agreementId, payment_index)] LEG 1
backend        the facilitator verifies, cosigns and settles; the contract records the payer
  <- 200 {jobId, agreementId, deadline}
browser
  -> POST /pin/{jobId}/content                                       the bytes
backend        sha256(received) == commit_hash ?   no -> 400, nothing spent
               multipart upload + x402 payment to the pinning endpoint         LEG 2 (float)
               fetch the returned CID from a public gateway, re-hash
               release_hash(agreementId, sha256)                     [verifier key]
  <- 200 {cid, gatewayUrl}
```

Any miss, or the deadline passing: `expire` → `refund_next` → the buyer is whole → `close` reclaims
the deposit.

**The settlement group shape is confirmed on TestNet against the real facilitator.** It accepts,
cosigns and settles a group carrying an extra buyer-signed application call, in both
`[feePayer, axfer, appCall]` and `[axfer, feePayer, appCall]` orderings, with the absolute
`payment_index` resolving correctly.

## 3. What is committed: `sha256(bytes)`, never a CID

A CID is the root of a DAG whose value depends on chunk size, CID version, codec, raw-leaves and
layout. Two conforming implementations can produce different CIDs for byte-identical input, so a CID
predicted by our frontend and one produced by the pinning service can differ legitimately. Committing
to that would refuse to release and refund **every job** the moment the parameters diverged — silently
correct, uniformly useless.

`sha256` of the file has no parameters to disagree about, is exactly the 32 bytes the contract wants,
and is precisely what the `hash` condition means. **The CID is a locator resolved at verification
time, never a value predicted in advance.** Nothing in this system may derive, predict or assert a
CID, and the test fakes are built so that a test cannot accidentally start depending on one (§12).

Consequence worth stating plainly: the deliverable must be **deterministic and known before the job
runs**. Nondeterministic output — model inference, generated text — cannot satisfy this condition at
all, and needs a different release condition.

## 4. The paid route is the whole purchase

Four properties of `x402-avm` 2.0.2 constrain the endpoint shape. All four were read from the
installed source, not inferred:

1. `PaymentOption.price` accepts a callable, `Callable[[HTTPRequestContext], Price | Awaitable[Price]]`,
   so price and `extra` can vary per request.
2. The FastAPI adapter's `get_body()` **returns `None`, hardcoded.** The callable can never read a
   request body. It can read path, method, **query parameters** and **headers**.
3. The callable runs on **both** passes — the unpaid request that earns the 402 and the paid retry —
   but it can tell them apart: `HTTPRequestContext.payment_header` is populated on the second.
4. `find_matching_requirements` compares scheme, network, amount, asset and `payTo`. It does **not**
   compare `extra`, so the paid pass need not reproduce a per-job `extra` — though it must still
   carry `feePayer` and `tag`, which ride into the settlement payload.

A fifth property is not in the library but in the discovery extension: `declare_discovery_extension`
describes **a body or query parameters, never a header.** A job identifier carried in a request
header is invisible to the catalogue row, so a buyer agent that discovers the route cannot learn that
it must call something else first.

Therefore **one paid route, taking its input in the query string, is the entire delivery-escrow
purchase** (§4c's pool seat is the server's other paid route, and is subject to the same four
properties):

```
POST /pin?sha256=<64 hex chars>&size=<bytes>
```

- **Unpaid pass** (`payment_header is None`) — the callable calls `create_agreement` with
  `commit_hash = sha256`, `share_price = p`, `max_seats = min_seats = 1`, and returns the price with
  `extra.agreementId`, which is what the buyer needs to build the `join` call.
- **Paid pass** — the callable looks the agreement up and returns the same requirements. It creates
  nothing.

**The creating side effect is idempotent on `(sha256, size)`, and reuses an existing agreement only
while that agreement is unfunded and inside its funding window.** A funded or elapsed agreement is
never handed to a second buyer. Two buyers quoting the same file in the same window can therefore
race for the one seat: the loser's settlement fails atomically, so no money moves. That costs a
request and nothing else — the same trade §6 already accepts for a settlement racing the sweeper.

The loser is then offered both outcomes rather than one, because they are not equivalent and the
difference is not the client's to hide (§4b). Re-quoting buys a second, independently escrowed pin
with its own deadline and its own refund guarantee. Attaching to the winner's job costs nothing and
guarantees nothing: the attached party has no agreement, no deadline and no claim, and if that job
ends in `ABANDONED` or `REFUNDED` they are left with nothing after being told the file was on its
way. Both are honest offers; only one of them is a purchase.

This server has exactly one other paid route, and it is a different product (§4c). Every remaining
route below is unpaid. The `/pin` routes exist only while `PIN_ROUTE_ENABLED` is on (§1):

| Route | Paid | Purpose |
|---|---|---|
| `POST /pin?sha256=&size=` | **yes** | the delivery-escrow purchase: agreement, price, leg 1 |
| `POST /index` | **yes** | the pool-seat purchase: names the configured open pool, creates nothing (§4c) |
| `POST /pin/{jobId}/content` | no | the bytes; rejects on a `commit_hash` mismatch before any float is spent |
| `GET /pin/{jobId}` | no | job status, the payer's view |
| `GET /pin/by-hash/{sha256}` | no | is there a live or recently released job for these bytes (§4b) |
| `GET /healthz` | no | liveness. Never completes a payment |
| `GET /`, `GET /app`, `GET /app/*` | no | the browser client (`docs/specs/pool-client.md` §3): `/` and `/app` redirect to `/app/`, everything under it is the built bundle. Answered before the payment middleware, so no path here is ever matched for payment |
| `/llms.txt`, `/.well-known/x402.json`, `/logo.png`, `/favicon.ico` | no | the listing-enrichment surface |

`jobId` is an opaque random token, not the agreement id, and it is issued **with the 200 that
confirms settlement — never in the 402.** Two buyers quoting the same file are handed the same
agreement, so anything carried in that 402 is shared with a stranger; the token is minted for whoever
actually paid. The `commit_hash` gate on the content route is what makes the route safe either way —
bytes that do not hash to the commitment are rejected — but an unguessable path costs nothing.

The token is **not** a confidentiality boundary, and nothing in this design may be built as though it
were. See §4b.

### 4b. The hash-addressed status query

A second status route answers one question — *is there a live or recently released job for these
bytes* — for a caller holding only the `sha256`. The caller who needs it is the one who never
received a 200: a buyer whose settlement failed, either because another buyer took the single seat
or because their own payment did not land. Their `sha256` is the only handle they have.

**Two routes rather than one, and the reason is cardinality, not secrecy.** `jobId` is a key:
exactly one job, forever, and its absence is a 404. `sha256` is not a key — the same bytes can be
pinned many times, by different buyers, on different days, after a refund — so a hash lookup has to
answer *which* job, and the answer is a policy: the live one if there is one, otherwise the most
recently released one inside a bounded window. That is a query, not a fetch, and giving it its own
route keeps the fetch a fetch.

**An unfunded quote counts as live only while it can still be funded.** Past the funding window it
stops being live here for the same reason it stops being reusable: nobody can pay it any more.
Without that bound the two lookups disagree about the same row — the reuse query calls it dead while
this one calls it the live job — and a quote the sweeper has not yet reclaimed outranks, indefinitely,
a job that released seconds ago with a real CID. That CID is the one thing this route exists to
disclose, so a stale quote must not shadow it. Between elapsing and being swept the row is neither
live nor terminal and reports as nothing, which is deliberate: its state still reads `QUOTED`, and
returning that would tell the buyer they may still pay it.

**What the hash-addressed view discloses, and why it is not a new disclosure.** Every agreement's
`commit_hash` is a field of a public application box, and every payer's address is a field of a
public roster box. Both are readable by anyone against any node, and box names are enumerable, so
the mapping *sha256 → this file is escrowed here, by this address, for this amount, until this
deadline* is already fully public and cannot be made otherwise: the contract needs `commit_hash` on
chain to check the release condition against it. A hash-addressed status route therefore adds
nothing on the chain-derived facts.

It does disclose one thing the chain does not: the **CID**. Content addressed by a CID is
retrievable by anyone holding that CID, so a party who holds the file learns a public locator for
content they already possess. That is the whole of the delta, and it is accepted — withholding it
would make the route useless while leaving the payer's address public.

**The hash-addressed view is still the narrower of the two**, on the principle that a view keyed by
something derivable from the bytes should carry only what a holder of the bytes needs:

| Field | `GET /pin/{jobId}` | `GET /pin/by-hash/{sha256}` |
|---|---|---|
| `state`, `sha256`, `size`, `deadline` | yes | yes |
| `agreementId` | yes | yes — public on chain, and it lets the caller verify the answer independently |
| `cid` | yes | yes |
| `error` | yes | **no** — the reason a job failed is operational detail, and the terminal state alone is what a re-quote decision needs |
| `payer` | never returned by either route | no |

**Bounded in time, so that it makes no durability claim.** A released job is answerable by hash only
while it is inside `RECENT_RELEASE_WINDOW_SECONDS` (default 3600); past that the route is a 404 and
the caller re-quotes. Telling someone that a pin from days ago still covers them would be a
durability claim, and no durability claim may be made until the worker's retention terms are read
(§13). This needs no historical index and no lookup over closed jobs.

**Discovery does not enter into it.** Both status routes are unpaid and neither is part of a paid
interaction, so neither is catalogued and neither is described by a discovery extension. The rule
that killed a header-carried job reference — a discovery extension can describe a body or query
parameters, never a header — binds routes an agent must call *before* paying. A buyer agent that
pays reads its `jobId` out of the 200, whose shape the paid route's `output` example advertises.

### 4c. The pool-seat route: `POST /index`

The second paid route this server serves, sharing this application's `payTo` with the delivery
escrow — and, while `PIN_ROUTE_ENABLED` is off, the only one. It sells one seat in a multi-seat `hash` agreement — several buyers funding one shared
agreement instead of one buyer funding their own — and, unlike `POST /pin`, **it creates nothing.**
Every agreement it can sell a seat in already exists on chain before the first buyer ever requests
a quote.

**Pools are created out of band, by an operator, one at a time.** `agents/create_pool.py` reads a
frozen edition manifest (an `{edition, sha256}` object) and calls the same `create_hash_agreement`
the delivery escrow's price callable uses, with `seats > 1` and `min_seats == max_seats == seats` —
a pool's seat count is fixed at creation and cannot change afterwards. Because a `hash` agreement's
`commit_hash` cannot be bound or updated after creation, the deliverable's bytes must already be
frozen and hashed before this call runs; a pool cannot exist for a deliverable that does not yet
exist. Three checks run before it creates anything, in the order that follows:

1. **The configured `INDEX_SEATS` is at or under the measured seat ceiling** (20 seats). Past it, the
   contract rejects the create group, so the script refuses locally first and names the ceiling.
2. **The configured `verifier` is a key distinct from the admin account this script signs with.**
   `release_hash` compares the delivered bytes' hash against the `commit_hash` already public on the
   agreement, so the check it performs is trivially satisfiable by anyone who can read the box — the
   real guarantee is that only the `verifier` key can submit it. A pool created with `verifier ==
   admin` would hold real seats behind a release gate that gates nothing.
3. **No other multi-seat `hash` agreement is currently open.** Exactly one pool is open at a time.
   This scan deliberately ignores the *current* `INDEX_SHARE_PRICE_MICRO` and `INDEX_SEATS` — it has
   to catch a still-open pool from a previous edition even when either has since changed for the next
   one, or the guard would look straight past exactly the pool it exists to catch. A slow, thorough
   scan is acceptable in an operator script; a false "nothing open" here is not, because it lets a
   second pool be created while the first is still joinable — real seats, in two agreements, with no
   way to merge them back into one afterwards.

`--dry-run` prints every parameter this call would submit — including the ALGO deposit it actually
spends, `box_mbr(seats) + (seats + 1) × min_fee` — and creates nothing. It is meant to be run first,
every time: the deposit scales with seat count and is not returned until the agreement is closed.

**Creating an agreement is not the same act as serving it.** A successful create only prints the new
agreement's id; the route keeps answering 503 until an operator sets `INDEX_AGREEMENT_ID` to that id.
On a deployed host it is a platform secret rather than a value in the deployment config, so setting it
restarts the server without a rebuild, and a later deploy cannot restore a stale id. This is deliberate: it is the one edit that names which agreement is live, and the
create call itself has no way to update the running server's configuration on its own.

**The settings this route reads**, beyond the ones the delivery escrow already needs:

| Setting | Meaning |
|---|---|
| `INDEX_RESOURCE_URL` | this route's pinned catalogue identity — same rules as `RESOURCE_URL` (§5) |
| `INDEX_SHARE_PRICE_MICRO` | the price of one seat, in micro-USDC |
| `INDEX_SEATS` | seats per pool, and therefore `min_seats == max_seats` on every agreement this creates |
| `INDEX_AGREEMENT_ID` | the agreement currently served; `0` or unset means no pool is configured |

**The route trusts a configured agreement id — it never searches for one.**
`Settings.index_agreement_id` names the agreement `POST /index` currently sells seats in. An unauthenticated,
free `POST /pin` quote for a new `(sha256, size)` also creates an agreement on this same application,
so a route that instead searched recent agreements for "the newest one that looks like an open pool"
could have that search hidden behind ordinary, unrelated `/pin` traffic within a couple of minutes at
this server's own rate limit — silently answering 503 for the rest of a multi-day funding window with
nothing actually wrong on chain. A configured id reads exactly one box instead, and is checked against
six of the seven conditions the contract itself applies to a `join`: state `OPEN`; condition `hash`;
`min_seats > 1` (a single-seat agreement is the delivery escrow, never a pool, whatever seat count is
configured); `min_seats` equal to the configured `INDEX_SEATS`; `share_price` equal to the configured
`INDEX_SHARE_PRICE_MICRO`; and the deadline not yet passed. `INDEX_AGREEMENT_ID` unset (or `0`) means
no pool is configured at all, checked before any chain read.

The seventh — that the contract is not paused — is deliberately not checked here. `paused` is a
global rather than a field of the agreement box, so reading it would add a second chain round trip to
every unauthenticated request, and what it guards is bounded: a buyer sent at a paused contract has
their group fail atomically, so nothing settles and no money moves. The operator tooling reads it
where it costs one read per pool and turns a create the contract would reject — opening an edition
into a paused contract — into a plain refusal before anything is signed, and taking the route dark
belongs in the same operator step as the pause itself.

**The 503 contract.** When no configured id passes every condition, this route answers **503** —
`{"error": "no_pool_open", "detail": "..."}` — and issues **no `payment-required` header** at all,
on both the unpaid probe and every slash variant this server's path normalisation folds onto the
same paid pattern. A payable 402 naming an agreement that cannot actually be joined would take a
buyer's signature and a facilitator round trip for a purchase the contract will refuse the moment
the buyer's own `join` call reaches it. The check runs twice — once in a gate mounted outside the
payment middleware, so it can answer 503 directly rather than falling into a generic 500, and once
again inside the price callable, because a pool can close in the narrow gap between the two reads.
Both reads happen off the event loop: this server's chain client is synchronous, and a blocking
chain call on the event loop would otherwise stall every other request in the process, `GET
/healthz` included, for as long as it took to answer. Off the loop is not the same as bounded,
so every chain read also carries its own short timeout: this route is unauthenticated and reads
the chain on each request, and the SDK's default 30-second horizon would let a stalled node
hold one worker thread per request until the pool of them is gone.

**A second 503, for a different fact.** A read that *failed* is not a pool that is *absent*, and
the two never collapse into one another. When the chain does not answer, the route answers
**503** `{"error": "pool_status_unavailable", ...}` with a `Retry-After`, not `no_pool_open`.
The refusal is the same — a seat that cannot be confirmed is never quoted — but the fact
reported is not: `no_pool_open` tells a buyer this edition is closed, which during a funding
window is the one wrong answer that costs a seat. The distinction starts lower down, where the
chain client reports a missing box as `None` only for a genuine 404 and raises every other
status, so that no caller can mistake *no answer* for *no box*.

**The one optional input.** A caller may name the agreement it quoted, as `agreementId` in a JSON
body — the shape the route is declared in — or in the query string. On the paid pass, a named id that
differs from the agreement the verified payment was matched to is refused with **409**: the open pool
can change between the 402 and the paid retry, and the buyer must not be handed a receipt for a pool
it did not ask to join. The route also refuses, with **400**, a non-empty body that is not JSON or not
a JSON object, or whose `agreementId` is present but not a JSON integer (`"7"`, `true`, `null` and
`7.0` are all refused), and a query string and body that name different agreements; and with
**413** a body over 1 KiB. Each of these might have named a pool, so none is ignored. All of them
are ≥ 400, so the payment middleware never settles them. The body is read whatever its
`Content-Type` says. An empty body, or an object without `agreementId`, names nothing, and a request
that names nothing is quoted and served against whichever pool is configured.

The declared example body is **`{}`**, on purpose: a buyer agent builds its request from the
listing, and any fixed `agreementId` in the example would name a pool other than the open one on
almost every deployment, turning every agent that copies it into a 409 after it had signed.
`agreementId` stays documented as an optional property of the declared input schema.

**The receipt.** A paid `POST /index` returns:

```json
{"agreementId": 42, "seatsTotal": 5, "deadline": 1758000000, "commitSha256": "…"}
```

read from the verified payment's requirements rather than a second chain lookup, so it is exactly
what the buyer's 402 promised. `seatsTaken` is deliberately absent from this response: the buyer's
own `join` call sits in the same settlement group as their payment and has not yet landed by the
time this response is built, so a seat count read now would already be stale by the time it is
printed. `cid` and `edition` are absent for the same underlying reason — the edition is not
released at seat-purchase time and neither value exists in chain state yet.

**One seat per address is a contract-level assertion**, not something this route checks before
minting a receipt: `join` refuses a sender that already holds a seat on the same agreement's roster.
A buyer who already holds a seat can still reach this route, pay, and receive the receipt above —
their settlement group then fails atomically on chain, so no money moves, but a receipt naming a
seat that will never exist has already been handed back. The route's own listing copy states the
rule so a buyer agent can avoid the case rather than discover it after paying.

**A payment must carry its own `join` call, and this is enforced before settlement.** An ordinary
`exact`-scheme transfer to this application's `payTo`, with no accompanying `join` application call,
verifies perfectly well: the transfer is valid and the facilitator has nothing to object to. What it
buys is nothing. The USDC lands in the application account holding no seat, no roster entry and no
refund path — `close` returns only ALGO to the creator, never USDC, and `refund_next` walks a roster
this payer is not on — so it is unrecoverable by anyone, including us.

It is the only malformed group with no on-chain consequence. Every other mistake a buyer can make
in building the group is caught by the contract and fails the whole group atomically, leaving the
buyer's money where it was; a missing `join` leaves no application call behind to fail.

So both paid handlers inspect the group before answering, and refuse with **400**
`{"error": "payment_group_incomplete", "reason": "…"}` unless it carries exactly one `join` call on
this application, naming the agreement the payment was verified against, sent by the account that
sent the transfer. The placement is the point: the payment middleware calls the handler *before*
settlement and skips settlement entirely for any response of 400 or above, so a refusal costs the
buyer nothing — their signed transactions are never submitted. A refusal one step later would be a
refund problem instead of an error message.

This is a presence check, not a second copy of the contract's rules. Amount, state, deadline,
one-seat-per-address and the payment-to-sender binding are all asserted on chain, where failure is
atomic and safe. Only the case the chain never sees is checked here.

## 5. Route and discovery configuration

`docs/specs/discovery-and-attribution.md` is authoritative for *why* each of these is required and
for the verification commands. This section fixes the values for the delivery-escrow route; the
pool-seat route's own resource, description and discovery declaration follow the same rules and are
covered in §4c. The startup guard table at the end of this section runs over every configured route,
both included, since a route missing the tag or the fee payer is exactly as unrepairable whichever
product it belongs to.

**Pin the advertised resource URL.** The server builds the catalogue identity from
`route_config.resource or adapter.get_url()`, and `get_url()` includes the query string. Left to the
default, every distinct `?sha256=…` would advertise a different resource, registering thousands of
near-duplicate directory entries instead of one entry whose settlement count rises. Setting

```python
resource = "https://<public-host>/pin"
```

pins the identity to a constant, independent of both the query string and the proxy headers.
`--proxy-headers --forwarded-allow-ips '*'` is still required for everything else the app builds from
the request URL.

**`extra` lives on the price, not beside it.** The scheme builds the settled
`PaymentRequirements.extra` from the price's `extra` and from nothing else; an `accepts`-level `extra`
and the `"$0.01"` string shorthand both silently discard `feePayer` and `tag`, and payments still
settle — untagged, and therefore unattributable.

**`max_timeout_seconds = 120`, not the default 300.** It must equal the funding window of §6.
Advertising 300 seconds to settle while sweeping at 120 promises a window we do not honour.

**The discovery declaration** uses the body shape, `body_type="json"`, because that is the only
shape the catalogue lists a `POST` route under (`discovery-and-attribution.md` §2):

```python
declare_discovery_extension(
    input={"sha256": "e3b0c442…", "size": 1048576},
    input_schema={
        "properties": {
            "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            "size": {"type": "integer", "minimum": 1},
        },
        "required": ["sha256", "size"],
    },
    body_type="json",
    output=OutputConfig(example={"jobId": "…", "agreementId": 41, "deadline": 1756000000}),
)
```

**Open before this route is enabled on a deployment:** the declaration names a JSON body, but the
route reads its input from the query string only. The price callable gets no body through its
adapter interface (property 2 above), and the callable is what quotes. Until `/pin` can take its input from the body it declares, a
buyer agent that follows the catalogue sends a request this route does not understand.

**The `description` is the Bazaar listing** — product copy that happens to live in a config file, and
subject to the project's copy rules: lead with the condition rather than the mechanism, define what
was promised as a sha256 committed in advance, and never omit that the conditions are machine-checked.

> Pin a file to IPFS and pay only if it matches. Your USDC is held by an Algorand escrow app until the
> pinned bytes hash to the sha256 you committed in advance — released when they match, refunded
> on-chain when they don't. Machine-verified only: no arbitration, no human in the loop.

**Development and production are different escrow applications**, therefore different `payTo`
addresses, therefore different merchant identities — the merchant id is derived from the address, so
the separation is automatic once the applications differ. Development runs on its own host: one root
domain per merchant account, and the production `payTo` never rotates.

**Startup guards. The server refuses to boot** rather than serve a misconfigured 402, because the
challenge tag is stamped at settlement and never reclassified — a first MainNet settlement served
from a wrong configuration is unrepairable:

| Guard | Refuses when |
|---|---|
| network ↔ asset | the CAIP-2 network and the ASA id are not a known-good pair (MainNet ⇒ 31566704) |
| network ↔ application | the configured app account is not the address derived from the configured app id, or a production app id appears in a development configuration |
| app-account readiness | the app account is not funded, or not opted in to the asset — it cannot receive a payment, and the endpoint must not answer a 402 it cannot honour |
| `/pin` on MainNet | `PIN_ROUTE_ENABLED` is on in a MainNet configuration — the route has no delivery path yet (§1) |
| signing key | `ADMIN_MNEMONIC` is set while `PIN_ROUTE_ENABLED` is off — nothing is signed, so the key must not be held (§1) |
| `tag` | any `accepts` entry lacks `tag = "x402-global-challenge"` |
| `payTo` / `feePayer` | either is absent from any `accepts` entry |
| `resource` | unset, not `https://`, or not the configured public host — or a route's resource is not one this configuration serves, which is how a `/pin` route present while `PIN_ROUTE_ENABLED` is off is refused |
| client bundle | `WEB_DIST_DIR` exists but holds no `index.html`, so every client page would answer 404 |

## 6. The funding window is policy, not a timestamp

An agreement must exist before anyone can pay into it, so every quote that is never taken up parks a
deposit — 0.0958 ALGO at today's minimum fee, being the roster-box minimum balance plus a reserve of
`(max_seats + 1)` inner-transaction fees. That deposit is the working capital the next quote needs, so
on an endpoint that quotes for anyone the binding constraint is the **capacity to quote at all**, not
loss.

The contract's one `deadline` serves both "pay by" and "deliver by", and sized for delivery it is far
longer than a funding window needs to be. Rather than adding a second timestamp to the record,
`expire` skips the deadline check when `seats == 0` **and** the sender is the creator — exactly the
condition "nobody has paid into this". So:

- **Deadline**: sized for *delivery alone*. Long enough for upload, leg 2, pinning, gateway
  propagation and the release transaction.
- **Funding window**: backend policy. Start at **120 s**, tightened under load. Sweep untouched
  agreements when it elapses.
- Never sweep inside the window the buyer was promised. A settlement racing the sweep fails
  harmlessly — the group is atomic, so the payment never lands — but it costs that buyer a request.

Because the unpaid pass of a paid request is what creates an agreement, **an ordinary 402 probe parks
a deposit.** That is expected rather than abusive: probing a paywalled route is what a discovering
agent does, and the catalogue row exists to invite it. It is bounded by three controls, all required —
the idempotency of §4, a **ceiling on concurrently unfunded agreements**, and rate limiting per source.

**The creation deposit must be computed from the live minimum fee and sent exactly.**
`create_agreement` rejects an overpayment as firmly as a shortfall, and the fee reserve is priced at
the live protocol minimum rather than a constant. A backend that hardcodes the fee breaks on any
protocol change.

## 7. Job states

| Job state | Contract state | Waiting on |
|---|---|---|
| `QUOTED` | `OPEN`, seats 0 | the buyer's leg 1 |
| `FUNDED` | `FUNDED` | the buyer's bytes |
| `RECEIVED` | `FUNDED` | leg 2 |
| `PINNED` | `FUNDED` | gateway verification |
| `RELEASED` | `RELEASED`, then closed | terminal, success |
| `ABANDONED` | `EXPIRED` → `REFUNDED` → closed | terminal, never funded |
| `REFUNDED` | `EXPIRED` → `REFUNDED` → closed | terminal, buyer made whole |

## 8. Failure handling

The buyer walks away whole in every branch; the risk lands on the operator. Treasury money becomes
unrecoverable at exactly one moment: **when leg 2 settles.**

| Failure | Buyer | Treasury | Response |
|---|---|---|---|
| Quote abandoned | paid nothing | deposit, briefly | early reclaim (§6) |
| Bytes never uploaded | refunded | — | deadline, then `expire` and refund |
| Uploaded bytes do not match the commitment | refunded | — | reject at upload; retry allowed until the deadline |
| Worker quotes above budget | refunded | — | never pay; refund |
| Worker unreachable | refunded | — | bounded backoff, capped attempts, then refund |
| **Paid, no CID returned** | refunded | **loses the leg-2 fee** | priced risk |
| **CID returned, gateway will not serve in time** | refunded | **loses the leg-2 fee** | priced risk |
| **Gateway serves bytes that do not match** | refunded | **loses the leg-2 fee** | priced risk; flag the worker |

Exposure is bounded by a **maximum file size**, not a job count: the leg-2 price is size-derived, so a
count-based cap does not bound the money. Express the in-flight limit in USDC. The same maximum bounds
memory, because the leg-2 client must hold the whole body (§9).

## 9. Two risks that need care

### Leg-2 retry is decidable — and must never be guessed

An HTTP timeout does not say whether the payment settled, and the worker's API offers no idempotency
key. But the answer is available on chain, exactly, because of how the payment is built: the client
constructs **and signs** the asset transfer locally before the request is sent, and the facilitator
cosigns the fee-payer leg without modifying it. **The transfer's transaction id and its last-valid
round are therefore known before the request leaves us.**

Record both before sending. On any ambiguous outcome:

| Chain says | Verdict |
|---|---|
| confirmed | **paid** — do not retry; reconcile from the response, or treat as "paid, no CID" |
| absent, `current_round <= last_valid` | **undecided** — it can still land; wait and re-check |
| absent, `current_round > last_valid` | **never landed** — safe to retry with a freshly built payload |

Bounded attempts and backoff still apply, and no retry may be issued without this check. A blind
retry double-spends quietly and repeatedly — the one failure here that does not announce itself.

Two consequences for the client. It cannot stream the upload: the x402 transport rebuilds the paid
retry from `request.content`, so the multipart body must be materialised, which is why §8's maximum
file size also bounds memory. And it must not try to mark the payment with a job identifier — the note
field is written by the library as `x402-payment-<nanoseconds>` and is not ours to set. The txid check
replaces it entirely, so no note, no per-job payer account and no worker-side idempotency key is
needed.

### Gateway propagation is the long pole and it is not ours

Verification re-fetches the CID from a public gateway, and freshly pinned content can take time to
become servable. That, not pinning, sets the delivery deadline. Instrument it from the first real
pinning run, and start with twice the observed p95, with a 120 s floor. Those runs are on **MainNet**:
the worker settles in MainNet USDC only, so no TestNet exercise can produce a propagation sample.

## 10. Capital

Two distinct pools:

- **USDC float** — fronts the leg-2 fee per in-flight job, reimbursed from escrow at release. Must
  cover peak concurrent in-flight fees.
- **ALGO working capital** — 0.0958 per live agreement at today's minimum fee, plus sweeper fees.
  This is what §6's early reclaim protects.

## 11. The sweeper does not pay on a schedule

The facilitator files repeating machine-driven payment loops as developer traffic, which counts toward
nothing. The sweeper issues **application calls** — `expire`, `refund_next`, `claim_refund`, `close` —
not x402 payments, so it produces no settlements and cannot be classified that way. Only leg 2 is an
x402 payment, and it is triggered by a buyer's upload, irregular by construction.

Stated explicitly so the sweeper is never "improved" into a batched payment job, and so that no
scheduled task anywhere in this system is ever allowed to settle.

## 12. Test strategy

Four layers, with a clean split of what answers what: **the real facilitator on TestNet answers "is
this shape acceptable"; the local fakes answer "does our server do the right thing on every branch,
every time".**

### 12.1 Contract — unchanged

The escrow application's own suite runs against LocalNet under `contracts/` and is already in CI. No
new contract behaviour is introduced here.

### 12.2 Offline — no network, no facilitator

The repository root is a second dependency surface (`requirements.txt`), and **CI must gain a job for
it**: `ruff` plus these tests on every pull request.

- **Route-config validation.** `validate_discovery_extension()` over every declared route, plus:
  `extra` on the price and not beside it; `tag == "x402-global-challenge"`; `feePayer` and `payTo`
  present; `resource` set, `https://`, matching the configured host; `max_timeout_seconds == 120`;
  the description non-empty and carrying both "sha256" and the machine-verified clause. This is the
  highest-value test here — every one of these failures is silent in production, and the tag is
  unrepairable after the first MainNet settlement.
- **Price-callable behaviour.** Two unpaid passes for the same `(sha256, size)` inside the window
  create **one** agreement; a pass carrying `payment_header` creates **none**; a pass whose matching
  agreement is funded or elapsed creates a **new** one.
- **Deposit arithmetic** from a mocked `min_fee` — exact, never a constant.
- **The job state machine** (§7) as its own module, transitions tested exhaustively.
- **The upload gate** — a `commit_hash` mismatch is a 400 and spends nothing.
- **The budget check** — a quote above budget is never paid, whatever the quote says.
- **The leg-2 retry verdict** as a pure function of `(txid status, last_valid, current_round)`
  returning paid / undecided / safe-to-retry. It is pure, so it is tested exhaustively offline; it is
  also the one function in this system that must never be wrong.
- **The startup guards** of §5 — each one refuses to boot.

### 12.3 LocalNet, with a stub facilitator

A local facilitator serving `/supported`, `/verify` and `/settle`, cosigning the fee-payer leg from a
LocalNet-funded sponsor and submitting to LocalNet algod. That puts the whole of leg 1 under test —
402, group construction, settlement, the contract recording the payer — plus the sweep, expire and
refund paths, with no external dependency and no real money.

### 12.4 The worker and gateway fakes

Both are **test-only**: never deployed, never served from the public host.

The worker fake is a **real x402 resource server** built on the same library and settling on LocalNet
in a LocalNet ASA, so the client under test meets a genuine protocol implementation rather than a
hand-written approximation of one:

```python
# tests/fakes/algofile.py

PRICE_FLOOR = 25_000          # 0.025 USDC — the one price point actually measured
RATE = 1                      # micro-units per byte above the floor. Invented: the
                              # real size→price curve has exactly one measured point,
                              # which is why nothing in the client may predict a price.

def quote(ctx: HTTPRequestContext) -> AssetAmount:
    # The worker prices by size, and a price callable cannot read the body.
    # Content-Length is the only size signal it can have -- which is also why
    # the client must send the real multipart body on the unpaid pass.
    size = int(ctx.adapter.get_header("content-length") or 0)
    return AssetAmount(
        amount=str(max(PRICE_FLOOR, size * RATE)),
        asset=str(LOCALNET_ASA),
        extra={"decimals": 6, "feePayer": FEE_PAYER, "tag": "x402-global-challenge",
               "priceUsd": ..., "fileSizeGb": ...},
    )

routes = {
    "POST /api/algofile/upload": RouteConfig(
        accepts=[PaymentOption(scheme="exact", pay_to=WORKER_PAY_TO,
                               price=quote, network=LOCALNET_CAIP2)],
        mime_type="application/json",
    ),
}

@app.post("/api/algofile/upload")
async def upload(file: UploadFile) -> dict:
    body = await file.read()
    cid = _random_cid()          # deliberately NOT derived from the bytes
    STORE[cid] = body
    return {"success": True, "cid": cid}
```

Two properties of the fake are load-bearing:

- **The CID is random, never derived from the bytes.** A fake that computed a predictable CID would
  let a test quietly acquire a dependency on CID predictability — the exact thing §3 forbids, and a
  dependency that would fail only on MainNet. Randomness makes it impossible to write.
- **Failure modes are driven out of band**, through a `POST /__control` route called by the test —
  never by a header the client sends. The client under test must not be able to tell that it is
  talking to a fake; the moment a test needs it to send something fake-specific, the thing under test
  is no longer the thing we ship.

| Mode | Reproduces |
|---|---|
| `ok` | happy path |
| `no_402` | worker unreachable; the liveness check must fail loudly |
| `quote_high` | worker quotes above budget → never pay |
| `settle_then_hang` | **paid, no response** — the ambiguous retry, and the only end-to-end test of §9's verdict |
| `settle_no_cid` | paid, no CID returned |

The gateway fake is separate — a public gateway is not the worker — and shares `STORE`:
`GET /ipfs/{cid}`, with modes `serve`, `cold_for(<n>s) then serve`, `wrong_bytes` and `never`.
`cold_for` is what calibrates the delivery deadline.

### 12.5 TestNet — the circuit test

Already answered, and not re-run: the facilitator accepts and settles the three-transaction group, and
a publicly reachable route paying real TestNet USDC into an **application account** is catalogued
within seconds and receives a merchant identity.

Open, and required by §4: **does a paid request carrying a query string catalogue as a single row when
`resource` is pinned?** One settlement of ~0.02 USDC against the existing public TestNet host answers
it — assert that **exactly one** row matches our `payTo`, that its id is the pinned URL, and that no
query-bearing variant appears anywhere in the paged results. Assert on our own rows, never on a delta
in `pagination.total`: the catalogue grows continuously on other merchants' traffic, so a global
count proves nothing. Fold the `max_timeout_seconds = 120` check into the same run.

Standing, and run by hand before a merge: **the `hash` circuit, end to end, against the real
facilitator.** `tests/e2e/`, selected with `pytest -m testnet`, covering the happy path
(quote → pay → settle → `release_hash` → `close`), non-delivery (deadline → `expire` → `refund_next`
→ `close`), the creator's early reclaim of an unfunded quote, and the unpaid quote surface. It is
never run in CI and never scheduled. Three gates, because the money is real and irreversible: it
skips itself unless `.env` carries `E2E_ENABLED`; `tests/e2e/conftest.py` skips it again unless
`testnet` is named in `-m`, so no broader filter can sweep it in; and the CI filter excludes the
marker as well. The second gate is the load-bearing one — the bootstrap writes `E2E_ENABLED` on its
first run and never removes it, so on a working machine the first gate is always open.

It is also **pinned to TestNet by the chain, not by the `.env` that describes it**: `preflight`
asserts algod's genesis is `testnet-v1.0` before anything else, and asserts `NETWORK` and
`USDC_ASA_ID` are the TestNet pair. There is one `.env` in the repo and the MainNet rehearsal
repoints it; without the pin that rehearsal would turn this suite into a MainNet settler, and
attribution is stamped at settlement and never reclassified.

Three properties keep it cheap enough to run often. The float **recirculates** rather than draining —
a release pays the treasury and the teardown returns it, a refund returns it directly — so the one
captcha-gated USDC top-up is a standing balance, not a per-run cost. The server is served on a
**loopback** address, so the resource is never catalogued and the suite attaches to no merchant
identity however often it runs. And the deadline is configurable (`E2E_DEADLINE_SECONDS`), because
TestNet has no timestamp offset and the refund scenario is real elapsed time.

What it must not absorb: anything a cheaper layer already answers. A branch the offline suite can
decide belongs there, where it is exhaustive and free — the slash-variant settle decision is the
worked example, and it stays in `tests/test_payment_integrity.py`; the quote surface (malformed
queries, and idempotency on `(sha256, size)`) is another, answered over the wire in
`tests/test_error_handling.py` and `tests/test_pricing.py`, and deliberately not repeated here where
each pass would park a real deposit. This layer exists for the questions only a real facilitator and
a real chain can answer.

The sharpest of those, and the reason the happy path reads ARC-28 events rather than box state
alone: **the payment the buyer signed, the settlement the facilitator reported, and the payer the
contract recorded must be one payment.** The client computes the group's transaction ids from the
bytes it is about to send, before the facilitator sees them; the `Joined` event's `payment_txn_id`
must be the axfer it signed, and the settlement receipt must name that same group. A payment and the
record it buys can look joined from the outside while being two unlinked transactions, and no
offline fake can be wrong in that particular way.

### 12.6 MainNet — the smallest possible set

The worker settles in MainNet USDC only, so leg 2 can only ever be exercised for real there. One
human-triggered end-to-end rehearsal — buyer wallet, our route, escrow, the real paid upload, gateway
verification, release — then the deploy checklist and the definition-of-done checks in
`docs/specs/discovery-and-attribution.md`.

**Irregular, human-triggered and few.** A repeating rehearsal is precisely the loop that gets filed as
developer traffic, so no MainNet exercise is ever scheduled, and no CI job may settle anything.

### 12.7 Deliberately not tested

- **Catalogue field names.** Match only on `resourceUrl`, `payTo` and `merchantId`. The discovery
  records are not a stable schema: between two observations three days apart a counter vanished and
  another was renamed in a way that also changed what it counted.
- **The worker's reliability**, which §8 prices as risk rather than asserting.

## 13. Open items

- **Which public gateway.** The worker returns `{success, cid}` and nothing else; whether it operates
  a gateway, or we resolve through a general one, is unanswered. Decided by the first MainNet
  rehearsal, whose propagation p95 also sets the deadline (§9).
- **The worker's real size→price curve.** One point is measured. The code must therefore never predict
  the price: the budget check reads the quote out of the 402 it just received.
- **The worker's retention and durability terms** are unverified. No durability claim may appear in
  any copy until they are read.
- **Query-string cataloguing** — the single TestNet run of §12.5.
