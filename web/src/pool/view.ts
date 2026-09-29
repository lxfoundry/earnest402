import { REFUND_BATCH_CEILING, type Agreement, type PoolState, type Seat } from '../chain/decode'
import type { ClosedOutcome, EditionLink } from '../chain/read'

/**
 * What the chain says, turned into what a buyer is shown and what they may
 * do about it.
 *
 * Pure, and deliberately separate from the reducer: the buyer's situation is
 * *derived* from the agreement record on every read rather than stored, so
 * there is no second copy of it to drift. The reducer holds only what is in
 * flight.
 */

/**
 * Re-exported from `chain/decode.ts`, which is where every fact this client
 * mirrors from the contract lives. It moved there because `chain/calls.ts`
 * needs it too and `chain/` must not import from `pool/` -- this re-export is
 * what keeps that move invisible to `web/tests/view.test.ts` and to any UI
 * that already imports the name from here.
 */
export { REFUND_BATCH_CEILING }

/**
 * What the buyer can be told.
 *
 * `gone` is the case worth naming: `close` deletes both boxes, so a released
 * pool and a refunded one both read as an absent record. The box cannot say
 * which, and the difference matters to whoever paid.
 */
export type Situation =
  | 'waiting'
  | 'awaiting-delivery'
  | 'released'
  | 'refundable'
  | 'refunding'
  | 'refunded'
  | 'gone'

/**
 * A total map, so a state added to the contract cannot fall into a tail
 * branch and be rendered as whatever the last case happened to be.
 *
 * `FILLED` is unreachable for a pool — it is the `quorum` fill state, and a
 * pool is always a `hash` agreement — but the map still demands an entry, and
 * the honest one is the same thing `FUNDED` means to a buyer: the money is in,
 * delivery is pending.
 */
export const SITUATION_FOR: Record<PoolState, Situation> = {
  OPEN: 'waiting',
  FUNDED: 'awaiting-delivery',
  FILLED: 'awaiting-delivery',
  RELEASED: 'released',
  EXPIRED: 'refundable',
  REFUNDING: 'refunding',
  REFUNDED: 'refunded',
}

/** What a buyer can send. Every one is permissionless. */
export type Action = 'expire' | 'refund_next' | 'claim_refund' | 'close'

export interface Verdict {
  situation: Situation
  /**
   * The boxes are gone, so nothing further can be read from the chain about
   * this pool -- not the roster, not the commitment. The situation still says
   * how it ended; this says the record backing that answer no longer exists.
   */
  closed: boolean
  /** Ordered: the one that moves things along comes first. */
  actions: Action[]
  /**
   * True when the buyer's own seat is still owed money. Independent of the
   * situation, because `REFUNDED` does not mean everyone has been paid.
   */
  owed: boolean
  /**
   * True when that owed seat is one the refund pass has already gone past --
   * skipped, because its address could not receive USDC when the pass reached
   * it. The pass never returns to a seat behind its cursor, so nothing but a
   * claim pays this one, whether the pass is still running or has finished.
   *
   * Worth its own field because `owed` alone reads as "the pass will get to
   * it", and on a running pass that is true of the seats ahead of the cursor
   * and false of the seats behind it.
   */
  skipped: boolean
  /** The seat this wallet holds, when it holds one. */
  seat: Seat | null
}

export interface Circumstances {
  /** `null` means the box is gone — closed, or never created. */
  record: Agreement | null
  seat: Seat | null
  /**
   * What the application's history says became of a closed pool, when the
   * record is gone and the question has been asked.
   *
   * `close` deletes both boxes, so a delivered pool and a refunded one are
   * indistinguishable on algod. Telling those apart needs the indexer, which
   * is a separate read that may be unavailable — `undefined` means it was not
   * asked or did not answer, and `unknown` means it answered and the history
   * did not say. Both render as `gone`, which is not an error.
   */
  closedOutcome?: ClosedOutcome
  /**
   * Where a released pool's edition is, read from the note on its release.
   *
   * Asked only of a pool that was released, open record or closed. The same
   * two absences as `closedOutcome`, and they render the same way: `undefined`
   * means it was not asked or the indexer did not answer, `null` that the
   * history holds no release note for this pool -- or not yet, since an
   * indexer trails the node. Neither is ever filled in with a guess.
   */
  edition?: EditionLink | null
  /** The chain's clock, never the browser's. */
  chainNow: bigint
  /**
   * Whether this wallet can pay a transaction fee at all.
   *
   * A buyer needs no ALGO to *buy* — the settlement group loads the whole
   * pooled fee onto the facilitator's leg — but every call below is an
   * ordinary application call whose fee the sender pays. Holding USDC implies
   * a minimum balance that is held, not spendable, so a buyer can arrive here
   * unable to send anything.
   *
   * That is not a dead end, and the interface must not present it as one:
   * `refund_next` and `claim_refund` pay the roster address rather than the
   * sender, so anyone can pay the fee and the money still reaches the buyer.
   * When this is false the actions are withheld and the buyer is told the
   * refund is coming, not that they must act.
   */
  canPayFee: boolean
}

/**
 * The buyer's situation, and what they may do about it.
 *
 * Every action listed here mirrors an assert the contract will apply. Offering
 * one the contract would refuse is not harmless, though it costs no money: a
 * call the chain refuses is never committed and pays no fee, but the buyer has
 * still signed a wallet prompt for nothing and is left with an error they
 * cannot act on.
 */
export function verdictFor(circumstances: Circumstances): Verdict {
  const { record, seat, chainNow, canPayFee } = circumstances

  if (record === null) {
    // A closed pool ended one of two ways, and they are opposite news. The
    // box cannot say which; the history can, when it has been read.
    const ended: Record<ClosedOutcome, Situation> = {
      released: 'released',
      refunded: 'refunded',
      unknown: 'gone',
    }
    const situation = circumstances.closedOutcome
      ? ended[circumstances.closedOutcome]
      : 'gone'
    return { situation, closed: true, actions: [], owed: false, skipped: false, seat }
  }

  const situation = SITUATION_FOR[record.state]
  const owed = seat !== null && seat.status === 'owed'
  const skipped = isSkipped(record, seat)
  const actions = canPayFee ? availableActions(record, seat, chainNow) : []
  return { situation, closed: false, actions, owed, skipped, seat }
}

/**
 * Whether `seat` is owed and the refund pass has already gone past it.
 *
 * The cursor test is not a formality. A seat ahead of the cursor still
 * belongs to `refund_next`; claiming it early would decrement
 * `unclaimed_seats` for a seat that was never skipped, cancelling out a real
 * skip and stranding that payer permanently. The contract asserts the same
 * thing (`seat < refund_cursor`) before it pays a claim.
 *
 * No state test, because none is needed: `expire` leaves the cursor at zero
 * and only `refund_next` moves it, and that call leaves `EXPIRED` for
 * `REFUNDING` or `REFUNDED` in the same step. So a seat behind the cursor
 * exists only on a pass that is running or finished.
 */
function isSkipped(record: Agreement, seat: Seat | null): boolean {
  return seat !== null && seat.status === 'owed' && seat.index < record.refundCursor
}

function availableActions(
  record: Agreement,
  seat: Seat | null,
  chainNow: bigint,
): Action[] {
  const actions: Action[] = []

  // `expire` is permissionless once the deadline has passed. Before it, only
  // the creator may reclaim, and only at zero seats -- a buyer never has that
  // path, so the deadline is the whole test here.
  if (
    (record.state === 'OPEN' || record.state === 'FUNDED') &&
    chainNow >= record.deadline
  ) {
    actions.push('expire')
  }

  // The refund pass, in either state that accepts it -- no cursor test.
  //
  // A cursor test here looks like it is guarding something, but it either
  // does nothing or does harm. In REFUNDING the cursor is always behind
  // `seats`: reaching it is exactly what flips the state to REFUNDED, so
  // `refundCursor < seats` holds for the whole time this branch could run and
  // the clause is dead weight. In EXPIRED it is actively wrong: a pool that
  // sold zero seats has `refundCursor === seats === 0` from the moment it
  // expires, and `refund_next` is still the one call that moves it out of
  // EXPIRED -- `refundBatchSize` below returns 1 for exactly this case. The
  // old guard withheld that call forever, leaving a zero-seat pool unable to
  // reach REFUNDED and therefore unable to `close`. The contract agrees:
  // `refund_next` asserts only `state == STATE_EXPIRED or state ==
  // STATE_REFUNDING`, and `agents/pool_ops.py`'s drain loop conditions on
  // exactly those two states.
  if (record.state === 'EXPIRED' || record.state === 'REFUNDING') {
    actions.push('refund_next')
  }

  // A seat the pass skipped, and only once the cursor has gone past it --
  // `isSkipped` says why that test matters. The state test mirrors the
  // contract's own assert on `claim_refund`, though `isSkipped` already
  // implies a pass has run.
  if (
    isSkipped(record, seat) &&
    (record.state === 'EXPIRED' ||
      record.state === 'REFUNDING' ||
      record.state === 'REFUNDED')
  ) {
    actions.push('claim_refund')
  }

  // `close` returns the creator's deposit and deletes the boxes. Terminal
  // state only -- EXPIRED is not terminal -- and only once nothing is owed.
  //
  // RELEASED is deliberately absent, though the contract accepts it there.
  // On a released pool the call does a buyer no good. Its one payment returns
  // the deposit to the pool's creator, never to whoever sends it, so the
  // sender pays a fee for the operator's benefit. And it deletes the record
  // this page reads the commitment from: the commitment itself stays in the
  // chain's history, as an argument of the calls that created and released
  // the pool, but every other buyer's page stops showing it. Closing a
  // released pool is the operator's call, not a buyer's button.
  if (
    record.state === 'REFUNDED' &&
    record.unclaimedSeats === 0 &&
    record.totalHeld === 0n
  ) {
    actions.push('close')
  }

  return actions
}

/** One file of a released edition's bundle, and where this page resolves it. */
export interface EditionFile {
  name: string
  url: string
}

/**
 * The two files a buyer is pointed at: the edition to read, and beside it the
 * JSON the pool committed to, whose sha256 is theirs to check. A trip's files
 * are the same pair under the `trip` stem.
 *
 * `gateway` is this page's configured origin, never anything from the chain,
 * and the CID and edition number are the note's, which `readEditionLink`
 * accepts only as letters and digits -- so nothing here can steer the link off
 * the gateway or out of the CID's directory.
 */
export function editionFiles(
  link: EditionLink,
  gateway: string,
  stem: 'edition' | 'trip' = 'edition',
): { html: EditionFile; json: EditionFile } {
  const file = (extension: string): EditionFile => {
    const name = `${stem}-${link.edition}.${extension}`
    return { name, url: `${gateway}/ipfs/${link.cid}/${name}` }
  }
  return { html: file('html'), json: file('json') }
}

/**
 * How many seats the next `refund_next` should cover.
 *
 * At least one even when the pool sold nothing: a zero-seat agreement still
 * needs one call to move from EXPIRED to REFUNDED before `close` will accept
 * it.
 */
export function refundBatchSize(record: Agreement): number {
  const outstanding = record.seats - record.refundCursor
  return Math.max(1, Math.min(REFUND_BATCH_CEILING, outstanding))
}

/**
 * Seats still to be sold. Never negative, even mid-read.
 *
 * Counted against `maxSeats`, which is the ceiling the contract's own `join`
 * enforces (`seats < agreement.max_seats.native`) -- not `minSeats`, which is
 * the fill target the pool's completion check reads. Every pool this project
 * creates has `minSeats == maxSeats`, so this changes no number anyone sees
 * today. Worth fixing anyway: this function reads as if it knew which
 * ceiling `join` will refuse against, and until now it named the wrong one.
 */
export function seatsLeft(record: Agreement): number {
  return Math.max(0, record.maxSeats - record.seats)
}

/**
 * The nouns a message built outside the screens names the product by: the
 * index's "pool" and "edition", or a trip's "trip" and "trip file".
 *
 * Here rather than in the brand's copy because the messages that use them are
 * built here and in the runner, which have no screen to read a brand from;
 * `brand.ts` holds each brand's set, and the index's is the default so every
 * caller that names none says what it always said.
 */
export interface KindNouns {
  /** What a seat is a seat in: "pool", "trip". */
  pool: string
  /** What is over when the record is gone: "edition", "trip". */
  product: string
  /** What the commitment is to: "edition", "trip file". */
  file: string
}

export const INDEX_NOUNS: KindNouns = { pool: 'pool', product: 'edition', file: 'edition' }

/**
 * A seat as the 402 described it: what the buyer was shown, and what they are
 * about to sign for.
 *
 * Deliberately not `payment/schemeClient.ts`'s `Quote402`, which carries what
 * the *payment* needs -- amount, asset, fee payer, escrow account. This
 * carries what the *pool* is: how big it is, how much of it is left, when its
 * window closes and which bytes the edition is committed to. The route puts
 * all of it in the 402's `extra`, and none of it reaches the facilitator.
 */
export interface SeatQuote {
  agreementId: bigint
  /** Micro-units of the payment asset, matching the record's `sharePrice`. */
  amount: bigint
  seatsTotal: number
  seatsLeft: number
  /** Unix seconds. */
  deadline: number
  /** Lowercase hex, no `0x`. */
  commitSha256: string
}

/**
 * Every way the pool has moved since its 402 was issued, said in the buyer's
 * own terms. An empty array means the quote still describes the chain.
 *
 * A 402 is a photograph of a pool, and a wallet prompt can sit open for
 * minutes. In between, somebody else's seat can land. So this runs once, on
 * the live record, immediately before the group is built -- the last moment
 * the answer is worth anything, and the moment with the most information.
 *
 * **`record` must be the agreement read for `quote.agreementId`.** The record
 * does not carry its own id -- the box name does, and the box name is gone by
 * the time it is decoded -- so this cannot re-check the identity itself, and
 * `null` is what stands in for that check: the pool the quote names has no
 * box, which is either a closed edition or an agreement that never existed.
 * Either way there is nothing to pay into.
 *
 * The price, seat and commitment comparisons are the ones `tests/e2e/pool.py`
 * makes against live chain state before each of its own purchases, which is
 * where the list comes from; the record's state is added because a buyer's
 * wallet can stay open across the pool's deadline, which that test's fill
 * never does. `seatsTotal` and `deadline` are fixed at creation and cannot
 * drift, so checking them would only restate the identity question the `null`
 * branch already answers.
 */
export function quoteDisagreements(
  quote: SeatQuote,
  record: Agreement | null,
  nouns: KindNouns = INDEX_NOUNS,
): string[] {
  const { pool, product, file } = nouns
  if (record === null) {
    return [
      `${pool} ${quote.agreementId} has no record on chain. Closing a ${pool} ` +
        `deletes its boxes, so this ${product} is either over or was never ` +
        'created -- and nothing can be paid into it now.',
    ]
  }

  const disagreements: string[] = []

  // `join` asserts the agreement is `OPEN`, and leaving `OPEN` is the ordinary
  // way a seven-day pool's quote goes stale: a pool that expired with seats
  // unsold keeps the same `seatsLeft`, so no comparison below would notice.
  //
  // The state, and deliberately not the deadline. Nothing here holds the
  // chain's clock, and the browser's runs ahead of it -- the contract compares
  // against the previous block's timestamp -- so a wall-clock test would refuse
  // quotes the chain still accepts. A pool past its deadline that nobody has
  // expired is still `OPEN` on chain; its group is refused when the
  // facilitator verifies it, before anything settles, and no money moves.
  if (record.state !== 'OPEN') {
    disagreements.push(
      `the ${pool} is no longer open: it is ${record.state}, and only an open ${pool} ` +
        'sells seats.',
    )
  }

  // The contract asserts the transfer equals `share_price` exactly, so a
  // price that moved is a group that fails on chain rather than a buyer who
  // overpays. No fee is paid either way -- a group the chain refuses is never
  // committed -- but refusing here asks for no signature, where finding out
  // there asks the buyer to sign for nothing and hands them a refusal.
  if (quote.amount !== record.sharePrice) {
    disagreements.push(
      `the price moved: this quote asks for ${quote.amount} micro-units and ` +
        `the ${pool} now charges ${record.sharePrice}.`,
    )
  }

  // The one that actually moves. Somebody else buying the last seat while
  // this buyer's wallet was open is the ordinary case, not the exotic one.
  const left = seatsLeft(record)
  if (quote.seatsLeft !== left) {
    disagreements.push(
      `seats moved: this quote offered ${quote.seatsLeft} of ` +
        `${quote.seatsTotal} still free, and ${left} ` +
        `${left === 1 ? 'is' : 'are'} free now.`,
    )
  }

  // Compared case-insensitively on purpose, and not because either side is
  // currently wrong. `decodeAgreement` spells this with `algosdk.bytesToHex`
  // and the 402 spells it with Python's `bytes.hex()`; both are lowercase
  // today, and neither promises to stay that way. A case flip on either side
  // would otherwise refuse every purchase of a perfectly good edition, and
  // the message would blame the edition.
  if (quote.commitSha256.toLowerCase() !== record.commitSha256.toLowerCase()) {
    disagreements.push(
      `the ${file} changed: this quote is for ${quote.commitSha256} and the ` +
        `${pool} is committed to ${record.commitSha256}.`,
    )
  }

  return disagreements
}
