# Pool client

> **Status: specification.** Written 2026-09-16.
> The buyer side of an edition pool: a browser client that buys a seat, shows what became of the
> pool, and recovers the money when the pool misses its deadline.
> Its sibling `docs/specs/delivery-escrow-client.md` specifies the single-seat product that shares
> this bundle and its configuration. `docs/specs/escrow-contract.md` specifies the contract both
> products settle into. This document restates the facts it depends on rather than assuming either
> has been read.

---

## 1. Scope

The buyer side of an **edition pool**: a multi-seat `hash` agreement whose seats are sold through the
pooled paid route, released against a sha256 committed when the pool was created, and refunded on
chain when it is not released by its deadline. One browser client, served at `/app`.

Three things are in scope: **buying a seat**, **showing what became of the pool**, and **recovering
the money** when a pool misses.

Out of scope:

- the single-seat delivery escrow, specified in `docs/specs/delivery-escrow-client.md`;
- creating a pool, releasing one, and closing a released one, which are operator actions
  (`agents/create_pool.py`, `agents/pool_ops.py`, `docs/runbooks/pool-operations.md`);
- `quorum` agreements, which no paid route sells.

## 2. What the client depends on

**The route.** `POST /index` quotes a seat in the one pool the server is configured to sell. The
unpaid pass creates nothing on chain: pools are created out of band. It answers:

| Response | Meaning |
|---|---|
| `402` | a seat is for sale. The accepted requirement's `extra` carries `feePayer`, `agreementId`, `seatsTaken`, `seatsTotal`, `seatsLeft`, `deadline` (Unix seconds) and `commitSha256` |
| `503` with `error: no_pool_open` | the chain was read and no pool is open. An answer, not a fault |
| `503` with `error: pool_status_unavailable` | the server could not read the chain, so whether a pool is open is unknown |

The paid pass returns `{agreementId, seatsTotal, deadline, commitSha256}` and nothing else. **There
is no status route.** Everything the client shows after the purchase is read from the chain.

**The payment group** is the three-transaction group of `docs/specs/delivery-escrow-client.md` §6:
`[feePayer, axfer, join(agreementId, payment_index=1)]`, the buyer signing indexes 1 and 2 in one
prompt, and `payTo` asserted to be the configured application's account before anything is built.

**The agreement record** is fixed width, decoded at fixed offsets with its length asserted. The
roster is 40 bytes a seat, pre-sized to `max_seats`, and a seat is one of three things: **empty**
(zero address), **settled** (address set, amount zero — paid out), or **owed** (address set, amount
above zero). Occupancy is never inferred from the roster's length. Both decoders are pinned by
golden vectors generated from the compiled contract's own description of the record.

**The contract facts the refund path turns on:**

- `expire` moves `OPEN` or `FUNDED` to `EXPIRED` once chain time has reached the deadline — or
  earlier, sent by the creator, for a pool nobody joined — and leaves the refund cursor at zero.
- `refund_next(count)` pays or skips the next `count` seats (at most 4) from the cursor, and leaves
  `EXPIRED` in the same step: `REFUNDED` when the cursor reaches `seats`, `REFUNDING` otherwise. It
  **skips** a seat whose address cannot receive USDC, leaving it owed, and never returns to it.
- `claim_refund(seat)` pays one owed seat **behind the cursor**. Claiming a seat ahead of it is
  refused.
- `close` deletes both boxes, and is accepted from `RELEASED` or `REFUNDED` once no seat is
  unclaimed and nothing is held.
- All four are permissionless. `refund_next` and `claim_refund` pay the roster address, never the
  sender, and no refund path checks the pause switch.

## 3. Routes

| Path | Screen |
|---|---|
| `/app/` | the product, a quote for the open pool on request, and a search for this wallet's seats. Links only ever mint this spelling; a bare `/app` is redirected to it |
| `/app/pool/<id>` | one pool. `<id>` is canonical decimal within `uint64` |
| anything else | not found |

**The URL is the buyer's recovery handle.** It is minted when a quote arrives, before anything can
fail, and moves with the pool a payment went into.

**The resource server serves the bundle itself**, on the same origin as the paid routes, so the
page's requests need no CORS and can read the `PAYMENT-REQUIRED` header. The deployment image builds
the bundle and the server answers under it: `/` and a bare `/app` redirect to `/app/`, keeping the
query string; a path under `/app/` with no file behind it and no extension in its last segment
answers with `index.html`, which is how a deep link to `/app/pool/<id>` reaches the client's router;
and a missing file with an extension is a 404, never the page. `index.html` is revalidated on every
load, and the content-hashed files under `/app/assets/` are cached as immutable. These paths are
answered before the payment middleware, so `/app/index` is a page and is never matched for payment.

The image also places each edition's published free tier — `edition-N-sample.html`,
`edition-N-sample.json` and `edition-N-manifest.json` — under `/app/editions/`, where it is answered
like any other file of the bundle and revalidated on every load. The files arrive byte for byte, so
the digests in the manifest hold for what a reader downloads. The sold files are never in the image,
even when the directory they are copied from holds one.

## 4. Where each answer comes from

**algod** answers everything that decides whether money is owed: the record, the roster, the chain's
clock (the last block's timestamp — never the browser's), whether a wallet can pay a fee, and what
became of a submitted transaction.

**The indexer** answers three questions, all from the application's history: which pools a wallet
joined, for a buyer who arrives without their link; how a closed pool ended, since `close` deletes
the boxes that would otherwise say; and where a released pool's edition is, since the note on its
release is the link's only record. Every query asks for application calls only and checks the
application id and the selector again client-side. An indexer that cannot be reached costs those
three answers and nothing else.

**Every read and every signature checks the network first.** The configured node's genesis hash must
match the configured network, checked once per client. A mismatch is remembered for the session; a
node that did not answer is asked again.

## 5. The rule that decides the shape

**Chain state is the source of truth; the client stores only what is in flight.** The buyer's
situation, their seat and the calls they may send are derived from the latest reading on every
render, never stored.

A pure reducer holds what is in flight — a quote, a signature, a read, a refund call — and a runner
executes its effects. The runner never throws: every failure comes back as an event. Every reading
is tagged with the pool and the wallet it was made for. A reading made for a wallet other than the
one connected now is dropped on arrival, and a reading of a pool other than the one on screen is
never stored.

## 6. Buying a seat

1. **Quote on request.** No wallet is needed, since the 402 names no buyer.
2. **Confirm.** The screen reads back the pool, the price, seats taken of total, the deadline and the
   committed sha256, and offers a fresh quote.
3. **Check once, on Buy, against the live record.** The pool must still be `OPEN`, the price must
   equal `share_price`, the seats left must match and the commitment must match (case-insensitively).
   A stale quote is dropped unsigned, with the reasons. A check that could not be made is not
   staleness: the quote stands and the buyer is told nothing was signed.
4. **Build, sign, settle.**

Outcomes are reported by kind, never by wording:

| Outcome | Shown |
|---|---|
| settled | the seat is awaited on the pool's page |
| settled, receipt unreadable | the payment went through; do not pay again. Also used when the receipt names a different pool from the one the signed group joined |
| not settled | the purchase did not complete, **or its answer was lost**: check the wallet's balance and the pool's page before paying again. Never an invitation to retry at once |

**A paid seat is never reported missing.** Until a reading shows it, or shows the boxes gone, the
screen says the seat is being confirmed.

## 7. What the buyer is shown

| Record | Situation |
|---|---|
| `OPEN` | waiting for seats, with an estimated countdown |
| `FUNDED`, `FILLED` | filled; the edition is owed against the commitment |
| `RELEASED` | delivered, against the commitment |
| `EXPIRED` | missed; the refund pass has not started |
| `REFUNDING` | missed; the refund pass is running |
| `REFUNDED` | missed; the refund pass has finished |
| no record | released or refunded, from the history — or, when the history cannot say, **ended, without naming which way** |

**The countdown is an estimate** and is labelled one. The contract compares the deadline with the
previous block's timestamp, so every decision about `expire` reads chain time.

**A released pool links to its edition**, whether its record is still there or has been closed. The
link is read from the note on the pool's `release_hash` transaction, which is history and survives
`close`:

1. Search `/v2/transactions` with the application id, `tx-type=appl` and `note-prefix` = the base64
   of `earnest:index:edition-`, following `next-token` up to the same page limit as the other
   history walks, and stopping at the page that holds the release.
2. Keep a transaction only if it is a call to this application whose first argument is the
   `release_hash` selector, whose second is the agreement id as eight big-endian bytes, and whose note
   is valid UTF-8 matching, in full,
   `^earnest:index:edition-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$` with the same
   agreement id. The prefix is not reserved and anyone can write a note that starts with it; only the
   agreement's verifier can commit `release_hash`, and only once. Anything else is ignored and never
   shown.
3. Show `<gateway>/ipfs/<cid>/edition-<N>.html`, and `edition-<N>.json` beside it: the file the pool
   committed to, whose sha256 the buyer compares with the commitment — shown beside it while the
   record exists, and named as held in the chain's history once it is closed.

The gateway is this bundle's configuration (§11), never anything on chain. When the indexer cannot be
reached, or its history holds no matching note, the screen says the pool was released and that the
link cannot be read right now. It never builds a link the note did not give, and **makes no claim
about how long the file stays pinned**.

**On a pool that missed, the buyer's seat is one of three things**, and they are different news:

| Seat | Shown |
|---|---|
| settled | refunded: the share price was paid back to this address |
| owed, and the pass has still to reach it | still to be refunded, and how far the pass has got |
| owed, and **behind the cursor** | **skipped**: still owed, the pass will not come back to it, and the fix is to opt this address back in to USDC, after which a claim pays it |

A skipped seat is skipped on a running pass as much as on a finished one. `REFUNDED` means the cursor
finished its pass, not that everyone has their money.

**A read that failed** keeps the last good reading on screen and says so. Two kinds are told apart:

- **unreachable** — the node did not answer. The refresh keeps asking.
- **incompatible** — the node is on another network, or a record could not be decoded. Asking again
  cannot help, so the refresh stops and the screen says the page itself has to be fixed.

## 8. Refund calls

| Call | Offered when |
|---|---|
| `expire` | `OPEN` or `FUNDED`, and chain time has reached the deadline |
| `refund_next` | `EXPIRED` or `REFUNDING`. `count = max(1, min(4, seats − refund_cursor))`: a pool that sold nothing still takes one call to reach `REFUNDED` |
| `claim_refund` | this wallet's seat is owed and behind the cursor |
| `close` | `REFUNDED`, with nothing unclaimed and nothing held. **Never on `RELEASED`**, though the contract accepts it: there its one payment returns the deposit to the pool's creator at the sender's expense, and it removes the record this page reads the commitment from |

**Offered only to a wallet that can pay the fee.** Spendable balance (balance above the account's
minimum) must cover the larger of the network's minimum fee and its per-byte rate times 512 bytes —
a bound every call is tested to fit under, signed, with every variable-width field at its maximum.
Erring high withholds a call a wallet might have afforded, which costs the buyer nothing.

**A buyer who cannot pay a fee is not at a dead end**, and is never told they must act. The refund
calls pay the roster address, so anyone may send them, and the operator runs every missed pool's
refund pass (§10). The exception is a skipped seat: nothing reaches it until its own address can
receive USDC again, so that buyer is told what to do rather than to wait.

**Before the wallet is asked, the call is checked again.** The record, the roster and the chain clock
are read afresh and the same derivation is run on them. A call no longer offered — someone else's
call moved the pool, or chain time has not reached the deadline — is refused unsigned.

**One call at a time, across every pool.** Every button is disabled while a call is in flight or a
read is out. The lock is released:

| The call | Released |
|---|---|
| confirmed | at once, and the pool is read again if it is on screen |
| refused, or declined in the wallet | at once, the pool is read again and the reason shown if it is on screen |
| submitted but not seen to confirm | only by a reading of its pool that already shows its outcome: the boxes were read at or after the round the node says it confirmed in, or at or after its last valid round; or the node says it was dropped; or the boxes are gone |

A reading taken before the call could have been committed does not release it: on a running pass
that reading still offers the same call. Refund calls are built valid for 120 rounds, so a call the
node cannot say anything about stops holding the lock within minutes. A call signed after its window
closed is refused by the node, at no cost, and releases the lock.

## 9. Refresh

A pool is read when it is opened. After that it is read on an interval, and whenever the tab becomes
visible, for as long as the interval runs:

- **every 30 seconds** while there is anything left to learn;
- **every 5 seconds** while a seat paid for on this pool has not appeared, or while a call may still
  land;
- **not at all** once the pool is closed, released with its edition link read, or refunded with
  nothing owed to this wallet and nothing left to send — and after an incompatible read failure.

A call that may still land keeps the refresh running whatever the pool on screen looks like, since
every refresh reads the call's pool too. A seat still awaited does the same, unless the page cannot
read this chain at all.

A released pool whose edition link has not been read keeps the refresh running at the 30-second
rate, closed or not. The indexer trails the node, so the reading that first shows a pool released is
the one most likely to come without its link, and a buyer watching the release land would otherwise
be left with "cannot be read right now" until they reload. A pool released with no note at all — a
test pool — is read at that rate for as long as its page is open.

## 10. What the operator owes this client

The client's promises rest on two operator obligations, both carried out through
`docs/runbooks/pool-operations.md`:

1. **Every pool that misses its deadline has its refund pass run to completion** — the sweep
   `docs/specs/escrow-contract.md` §11 lists among the backend's obligations. The contract makes the
   refund calls possible for anyone and automatic for no one. The screen's promise to a buyer who
   cannot pay a fee — the refund is on its way, and you do not need to act — is true only because of
   this.
2. **An edition pool is released with its CID in the release transaction's note.** That note is the
   edition link's only on-chain record, and a release cannot be sent twice, so one sent without it
   leaves the link nowhere this page can find. See §7.

Closing a released pool is not among them. `close` deletes the boxes, which are current state, and
nothing in the chain's history: the release note and the committed sha256 — an argument of both the
creating call and the release — survive it. Its only cost is this page's, which reads the sha256
from the agreement box and cannot show it once the box is gone.

## 11. Configuration

The bundle's configuration is `docs/specs/delivery-escrow-client.md` §7, shared by both products:
network, asset id, application id, facilitator URL, resource host, algod URL and indexer URL,
compiled in, required, with no defaults. The maximum file size is required there and unused here.

This product adds one: **`VITE_IPFS_GATEWAY`**, the gateway a released pool's edition link is
resolved against — an origin, https except on loopback, with no path, query or fragment, since the
page appends `/ipfs/<cid>/…` itself. Required like the rest: the bundle refuses to start without it.

### The brand

Two optional keys choose which product line the bundle presents. Both are read by `getBrand()`, on
their own and never through the network configuration, so a build or a test with no network
configuration still reads the brand.

- **`VITE_BRAND`**: `index` or `travel`. Unset or empty is `index`, and any other value refuses to
  start.
  - **`index`** is this client as §1–§10 describe it, word for word.
  - **`travel`** is the same client, the same kind of pool and the same rules, presented as a
    sample group trip on a deployment of its own. It says "trip" where the index says "pool" and
    "trip file" where it says "edition", including in the messages the runner writes. It reads a
    released trip's link from notes prefixed `earnest:travel:trip-` and matching, in full,
    `^earnest:travel:trip-([1-9][0-9]*):agreement:(0|[1-9][0-9]*):cid:([A-Za-z0-9]+)$`, on
    `release_hash` calls only, under the same checks as §7. It links `trip-<N>.html` and
    `trip-<N>.json`, and never takes one kind's note for the other's.

    **The writer side is not built.** Nothing in this repository yet writes an
    `earnest:travel:trip-` note or a trip bundle, as `agents/release_edition.py` writes the index's.
    Until it does, a released trip reads as released with its link unreadable, as §7 describes.

    **`travel` runs on TestNet only.** Its badge, its demo notice and its funding steps all say the
    seat is paid in test USDC on TestNet, so a travel build whose `VITE_NETWORK` is any other
    network, LocalNet included, is refused with the configuration error page. This is the one check
    that needs the brand and the network together, and it is made once both have been read.
- **`VITE_OFFER_B64`**: the trip a travel build shows, as the base64 of UTF-8 JSON. Unset or empty
  means no offer.

**The offer** is display text: title, city and origin with their codes, departure and return dates,
nights, travellers, a reference fare, a source line, and `fetchedAt`, when the fare was fetched,
which is required and not shown. It may also carry **`provider`**, the data source's display name:
optional, and when present a non-blank string of at most 120 characters. It is rendered in the fare's
caption ("Fare data: {provider}, see the demo notice") and in the demo notice's sentence saying the
page is an independent demo, not that provider's product. Without it the caption reads "Fare data:
see the demo notice" and that sentence is dropped. This repository names no data source: the name
reaches the page only through the offer. Nothing in it reaches the chain or the paid route: the
seat price, the seat count and the deadline a buyer pays against are the quote's and the
agreement's, and the page shows them only once one of those has been read. Until then the hook and
the terms say "enough travellers"; the offer's `travellers` describes the sample trip under its
heading and is never the count a buyer pays against. The page ignores keys it does not know, and
refuses an offer that:

- is not standard, padded base64 of UTF-8 JSON, or is not an object. Line breaks are ignored, since
  `base64` wraps its output; any other whitespace is refused;
- has `v` other than `1`;
- is missing a field, or has a blank one;
- has a string longer than 120 characters;
- has dates not written `YYYY-MM-DD`, or naming a day that does not exist;
- has `nights` outside 1–21, or `travellers` below 1;
- has a fare amount that is not a decimal string, or a currency that is not three letters.

An offer given to an `index` build is refused too, since the index never shows one.

**A refusal is the configuration error page**, naming the key and the field. The build then shows
no trip at all, never a trip with a field filled in by guesswork.

A travel page always carries its **demo notice**. It is a band of its own, directly under the offer
card and before any pay button, and it cannot be collapsed. It carries the offer's source line, or a
neutral one when the build has no offer, and names the data source only when the offer gives
`provider`.

**A trip past its deadline** that nobody has expired yet is still `OPEN` or `FUNDED` on chain. The
travel page shows it as **Expired**, judged by the chain's clock and never the browser's, and points
to the `expire` call that starts the refunds. The situation in §7 is unchanged; the index shows these
pools as it always has.

## 12. Test strategy

**Offline.**

- The decoders against golden vectors generated from the compiled contract's record description,
  including the state names' order and a vector for each seat status.
- The derivation of situation, seat and calls against the contract's asserts, one case per guard.
- The reducer exhaustively: every reachable state against every event, asserting that it never
  throws, never sends a call the derivation did not offer, never sends a second call over one in
  flight, never releases the lock on a reading that does not show the call's outcome, never stores a
  reading made for another pool or wallet, and polls exactly when there is something left to learn.
- The runner's failure mappings against hand-written algod and indexer fakes, with the decoders, the
  network guard and the group builder running for real.
- The screens: every sentence that costs a buyer money or trust when it is wrong, checked against the
  state the reducer would actually hold.

**TestNet, by hand.** A real wallet buying a seat through the real facilitator, and a missed pool's
refund path sent from the page: `expire`, `refund_next`, the USDC back with its payer, `close`. A
released pool's page, followed to its edition through the configured gateway, with the JSON's sha256
compared against the commitment.

## 13. Open items

- **A closed released pool shows no sha256.** The page reads the commitment only from the agreement
  box. It is still in the history — the creating call's argument and the release call's argument
  and `Released` event — and could be read from there by the same indexer walk that tells a closed
  pool's outcome.
- **A lost answer to the paid request is reported as not settled.** The money may have moved, and
  only the wording guards against a second payment: the failure is not yet resolved against the chain
  the way an unconfirmed refund call is.
- **The closed-pool history walk starts at the application's first transaction** and stops at the
  answer or at its page limit. A pool whose calls lie beyond that limit reads as ended without saying
  which way.
