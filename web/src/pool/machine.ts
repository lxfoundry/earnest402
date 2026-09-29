import type { Action, Circumstances, SeatQuote, Verdict } from './view'
import { verdictFor } from './view'

/**
 * Buying and holding a seat, as a pure reducer.
 *
 * `(state, event) => (state, effects)`. Nothing here touches the network, the
 * wallet or the DOM: `runner.ts` executes the effects and feeds the results
 * back as events.
 *
 * The rule that decides the shape of everything below:
 *
 * > **Chain state is the source of truth; the reducer stores only what is in
 * > flight.**
 *
 * `view.ts`'s `verdictFor` already turns a chain read into the buyer's
 * situation, the actions available to them and the seat they hold. This
 * reducer never computes any of that and never stores it -- it keeps the last
 * `Circumstances` read off the chain and calls `verdictFor` when asked. A
 * stored copy of a derived answer is a copy that drifts from the chain, and a
 * buyer reads the drift as truth.
 *
 * So nothing derived is stored here. A seat has no file, no hash to compute
 * and no upload; the only things genuinely in flight are an unpaid pass, a
 * signature, a read and a refund call. The size of what follows is not in
 * those but in the races between them: readings tagged by the pool and the
 * wallet they were made for, one action lock held across every pool, the URL
 * moving with a paid pool, and purchase outcomes that carry their kind.
 */

/**
 * Where the purchase is, and nothing else.
 *
 * Note what is absent: there is no `SETTLED` resting state. Whether this
 * buyer holds a seat is a fact about the roster, so it is read off the chain
 * through `verdictFor(...).seat` rather than remembered here -- a machine
 * status saying "bought" would be a second, unfalsifiable answer to a
 * question the box already answers. A settlement lands back in `IDLE`, and
 * what marks it is `awaitingSeat` until the node catches up.
 */
export type Purchase =
  | 'IDLE'
  | 'QUOTING'
  | 'CONFIRM'
  | 'CHECKING'
  | 'SETTLING'
  | 'UNAVAILABLE'

/**
 * The one refund-path call in flight, and the pool it was sent against.
 *
 * Not a property of the screen, for the same reason `awaitingSeat` is not: a
 * call keeps travelling when the buyer opens another pool, and a lock cleared
 * by navigation is a lock that is gone when they come back -- a read landing
 * before the confirmation would offer the same call again. So it survives
 * every screen change, and a completion is matched on both fields, since an
 * action name alone cannot tell one pool's `refund_next` from another's.
 */
export interface Sending {
  action: Action
  agreementId: bigint
  /**
   * Set when the call was submitted but not seen to confirm. It may still
   * land, so the lock holds until a reading of this pool shows what became of
   * it.
   */
  unconfirmed?: UnconfirmedCall
}

/**
 * A call that was submitted and not seen to confirm, as a reading is asked to
 * check it.
 *
 * A reading of the pool alone cannot release the lock. It may have been taken
 * before the call was committed, and on a pass with seats still ahead of the
 * cursor the verdict offers `refund_next` again either way -- so the buyer
 * would be handed a second call while the first is still travelling. The
 * reading has to say whether it already reflects the call.
 */
export interface UnconfirmedCall {
  txId: string
  /**
   * The last round the call can be committed in. Past it the call can no
   * longer land, so a reading taken after it shows the call's outcome
   * whatever the node remembers about the transaction.
   */
  lastValid: bigint
}

/** Why the route declined to quote, both ways of saying it. */
export interface Unavailable {
  /**
   * The server's own code -- `no_pool_open` or `pool_status_unavailable`.
   * Kept because the two are opposite news and the screen has to tell them
   * apart: one says the edition is closed, the other says we could not ask.
   */
  reason: string
  /** The wrapped detail, for a reason no screen has copy for yet. */
  message: string
}

/**
 * Where a purchase message came from.
 *
 * A kind rather than a bare string, because the messages are not variations
 * on one theme: two of them are opposite news. `settle_failed` says a purchase
 * did not complete; `receipt_unreadable` says the money moved and must not be
 * sent again. A screen handed only the text has to guess which it is holding,
 * and every way of guessing -- matching the wording, or reading some other
 * field as a proxy for it -- is wrong in a case that costs a buyer money or
 * trust.
 */
export type PurchaseErrorKind =
  /** The unpaid pass failed. No purchase was attempted. */
  | 'quote_failed'
  /** The freshness check found the pool had moved. Nothing was signed. */
  | 'quote_stale'
  /** The freshness check could not be made. Nothing was signed; the quote stands. */
  | 'quote_unverifiable'
  /** The signature or the settlement did not go through. */
  | 'settle_failed'
  /** The payment settled and its answer could not be read. Never to be repeated. */
  | 'receipt_unreadable'

export interface PurchaseError {
  kind: PurchaseErrorKind
  message: string
}

/**
 * Whether reading again could help.
 *
 * A kind for the same reason a purchase message has one: the two are opposite
 * news. One says the node did not answer this time, and the next tick may
 * well succeed. The other says this build of the page cannot read this chain
 * at all -- its bundle names another network than the node it reads, or the
 * records it finds are not ones it can decode -- and nothing a buyer does, and
 * no amount of retrying, changes that.
 */
export type ReadFailureKind =
  /** The node did not answer, or answered with an error. Worth asking again. */
  | 'unreachable'
  /** This build does not match the chain it reads. Asking again cannot help. */
  | 'incompatible'

export interface ReadError {
  kind: ReadFailureKind
  message: string
}

export interface State {
  wallet?: string
  /** The pool on screen, from the URL. Absent on the entry screen. */
  agreementId?: bigint

  // --- buying ---
  purchase: Purchase
  quote?: SeatQuote
  /** Set when the route declined. Rendered as an answer, not as an error. */
  unavailable?: Unavailable
  purchaseError?: PurchaseError

  // --- reading ---
  reading: boolean
  circumstances?: Circumstances
  readError?: ReadError
  /**
   * A settlement whose seat has not yet appeared in a box read.
   *
   * The 200 is authoritative and the node lags behind it, so a read that
   * shows no seat right after a purchase is a stale read, never an answer.
   * Holding the agreement id rather than a boolean is what lets a read of
   * *some other* pool leave the flag alone.
   */
  awaitingSeat?: bigint

  // --- acting ---
  sending?: Sending
  actionError?: string
  lastTxId?: string

  // --- the fallback for a buyer who arrived without their link ---
  /**
   * Objects, never a bare `bigint[]`. The state is a prop of every screen, and
   * a development build of React 19.2 logs each re-render's changed props by
   * passing any array of primitives to `JSON.stringify`, which throws on a
   * bigint -- outside every error boundary, after which React processes no
   * further update: a link moves the address bar and the screen never follows.
   * An array of objects is walked field by field instead, and a lone bigint is
   * printed with `String`. The same holds for any array a future field adds.
   */
  myPools?: { agreementId: bigint }[]
  findingMyPools: boolean
  myPoolsError?: string
}

export type Event =
  | { type: 'WALLET_CONNECTED'; address: string }
  | { type: 'WALLET_DISCONNECTED' }
  | { type: 'OPENED_ENTRY' }
  | { type: 'OPENED_POOL'; agreementId: bigint }
  | { type: 'QUOTE_REQUESTED' }
  | { type: 'QUOTE_RECEIVED'; quote: SeatQuote }
  | { type: 'QUOTE_REFUSED'; reason: string; message: string }
  | { type: 'QUOTE_FAILED'; error: string }
  | { type: 'BUY_REQUESTED' }
  | { type: 'QUOTE_VERIFIED' }
  /**
   * `noun` is what the buyer is told moved -- "pool" on the index, "trip" on
   * the travel line -- and "pool" when it is not given.
   */
  | { type: 'QUOTE_STALE'; disagreements: string[]; noun?: string }
  | { type: 'QUOTE_UNVERIFIABLE'; error: string }
  | {
      type: 'SETTLED'
      agreementId: bigint
      seatsTotal: number
      deadline: number
      commitSha256: string
    }
  /**
   * The money moved and the receipt could not be read.
   *
   * Its own event rather than a `SETTLE_FAILED` carrying a gentler string,
   * because the two need opposite handling and a flag on a failure event is
   * exactly how the distinction gets dropped. This one sets `awaitingSeat`
   * like an ordinary settlement -- the seat is coming -- and says so; a buyer
   * told their payment failed when it succeeded may pay twice.
   */
  | { type: 'RECEIPT_UNREADABLE'; agreementId: bigint; message: string }
  | { type: 'SETTLE_FAILED'; error: string }
  | { type: 'READ_REQUESTED' }
  /**
   * Both carry the pool they were read for, because `Circumstances` does not
   * -- and a read is slow enough to outlive the screen that asked for it. A
   * buyer who moves from one pool to the next while a read is in flight would
   * otherwise have the first pool's answer stored as the second's, and if
   * that answer is terminal the refresh stops and it stays there.
   *
   * And the wallet they were read for, `null` for none, for the same reason
   * one step over. Two of a reading's answers -- the seat, and whether a fee
   * can be paid -- belong to a wallet, and a read is slow enough to outlive a
   * wallet change too. A read started before a wallet connected that lands
   * after the connected wallet's own read would say "no seat here" over a
   * seat that is owed, and on a finished refund pass that reading is
   * terminal: the refresh stops, and the claim is never offered.
   */
  | {
      type: 'READ_RECEIVED'
      agreementId: bigint
      address: string | null
      circumstances: Circumstances
      /**
       * The unconfirmed call this reading was asked to check, when the
       * reading already shows what became of it: the boxes were read at or
       * after the round it confirmed in, or after the last round it could
       * have, or the node said it was dropped. Absent when the reading was
       * not asked, or cannot tell yet.
       */
      finalFor?: string
    }
  | {
      type: 'READ_FAILED'
      agreementId: bigint
      address: string | null
      kind: ReadFailureKind
      error: string
    }
  | { type: 'ACTION_REQUESTED'; action: Action }
  | { type: 'ACTION_SENT'; action: Action; agreementId: bigint; txId: string }
  /**
   * Submitted, and not seen to confirm: the wait ran out, or the node stopped
   * answering after the transaction left. Its own event rather than a failure
   * because the call may still land, and releasing the lock on a failure
   * would let the buyer send it again while the first one is still travelling.
   */
  | {
      type: 'ACTION_UNCONFIRMED'
      action: Action
      agreementId: bigint
      txId: string
      lastValid: bigint
    }
  | { type: 'ACTION_FAILED'; action: Action; agreementId: bigint; error: string }
  | { type: 'MY_POOLS_REQUESTED' }
  /**
   * Tagged with the wallet searched, for the reason the reads are: a search
   * that outlives its wallet must not be listed as the next wallet's seats.
   */
  | { type: 'MY_POOLS_FOUND'; address: string; agreementIds: bigint[] }
  | { type: 'MY_POOLS_UNAVAILABLE'; address: string; error: string }

export type Effect =
  | { type: 'REQUEST_QUOTE' }
  | { type: 'VERIFY_QUOTE'; quote: SeatQuote }
  | { type: 'BUILD_AND_SIGN'; quote: SeatQuote }
  /**
   * `address` is the wallet the reading is for: the one connected when the
   * reducer asked, or `null`. The runner reads the seat and the fee for this
   * address rather than for whichever wallet is connected by the time the
   * effect runs, so the question asked and the tag on its answer are the same
   * value by construction.
   */
  | {
      type: 'READ_POOL'
      agreementId: bigint
      address: string | null
      /** Present when this pool has a call that may still land. */
      call?: UnconfirmedCall
    }
  | { type: 'SEND_ACTION'; action: Action; agreementId: bigint }
  | { type: 'FIND_MY_POOLS'; address: string }
  | { type: 'NAVIGATE'; path: string }

interface Result {
  state: State
  effects: Effect[]
}

export function initialState(): State {
  return { purchase: 'IDLE', reading: false, findingMyPools: false }
}

/** The path a pool lives at. Spelled once, so the router and this agree. */
export function poolPath(agreementId: bigint): string {
  return `/app/pool/${agreementId}`
}

/**
 * How often the screen re-reads an open pool. Thirty seconds is ample: a pool
 * fills over hours, and every tick is several round trips to a public node.
 */
export const REFRESH_INTERVAL_MS = 30_000

/**
 * How often it re-reads while an answer is due within a block or two: a seat
 * this buyer has just paid for, or a call that may still land.
 *
 * About two blocks. The node usually shows a settled seat a round or so
 * behind the settlement, and at half a minute a buyer who has just paid would
 * watch "confirming your seat" for most of that for no reason. Both waits end
 * on their own -- the seat appears, the call's outcome shows -- so the faster
 * rate never outlives what it is waiting for.
 */
export const PROMPT_REFRESH_INTERVAL_MS = 5_000

/**
 * Which purchase states may open a new unpaid pass.
 *
 * An opt-in allowlist rather than a chain of `!==`, so a state added later
 * has to be admitted deliberately instead of inheriting permission. The three
 * states left out -- `QUOTING`, `CHECKING`, `SETTLING` -- each have something
 * in flight whose result would arrive describing a quote this one replaced,
 * and `SETTLING` in particular has a wallet prompt open over a group built
 * from the quote a second pass would drop.
 *
 * `CONFIRM` is admitted: it holds a quote and nothing in flight. Refusing a
 * pass there left a buyer with no wallet connected no way off the confirm view
 * but connecting one -- `BUY_REQUESTED` needs a wallet to reach the freshness
 * check -- and that quote followed them to every screen. A fresh pass replaces
 * the quote only when the buyer asks for one, so the price does not move under
 * a buyer who is merely reading it.
 *
 * Unlike the delivery escrow's 402, the pooled route's unpaid pass creates
 * nothing on chain: pools are made out of band by the operator, so quoting
 * parks no deposit, and a re-quote from `CONFIRM` is free. This allowlist is
 * therefore about not moving the ground under a buyer, not about money.
 */
const MAY_QUOTE: ReadonlySet<Purchase> = new Set<Purchase>(['IDLE', 'UNAVAILABLE', 'CONFIRM'])

const only = (state: State): Result => ({ state, effects: [] })

/**
 * Compile-time exhaustiveness guard.
 *
 * Reachable only if an `Event` variant has no branch below, in which case
 * `event` is not `never` and this call fails to compile -- which is the whole
 * point. It returns the state unchanged rather than throwing: a reducer that
 * threw on an event it does not price would strand the machine instead of
 * ignoring it.
 */
function exhaustive(_event: never, state: State): Result {
  return only(state)
}

/**
 * Whether there is anything left to learn by reading this pool again.
 *
 * Deliberately a predicate and not an effect. An effect is a thing with a
 * result; a repeating timer is a subscription, and modelling one as an effect
 * would put a lifecycle the reducer cannot own inside a pure function. The
 * React layer holds the `setInterval` and the `visibilitychange` listener and
 * asks this whether to keep them.
 *
 * Terminal means the chain has nothing further to say to *this* buyer: the
 * boxes are gone, or the edition was released, or the refund pass finished
 * with nothing owed here and nothing left to send. A `refunded` pool that
 * still owes this buyer is **not** terminal -- `REFUNDED` means the cursor
 * finished its pass, not that everyone has their money. Nor is a released
 * pool whose edition link has not been read: the indexer that holds the link
 * trails the node that shows the release, so the reading that first shows a
 * pool released is the one most likely to come without it.
 */
export function shouldPoll(state: State): boolean {
  // Nothing on screen to poll. The entry screen reads no pool.
  if (state.agreementId === undefined) return false
  // A call that may still land: the lock on it is released by a read, and
  // nothing about the pool on screen -- finished, or unreadable -- may stop
  // the read of the call's pool from happening.
  if (state.sending?.unconfirmed !== undefined) return true
  // A build that cannot read this chain will not read it on the next tick
  // either, and a timer asking again forever is only load on a public node.
  if (state.readError?.kind === 'incompatible') return false
  // A paid-for seat the node has not shown yet is the one question worth
  // asking repeatedly, whatever the record currently says.
  if (state.awaitingSeat !== undefined) return true
  // Nothing read yet, so everything is still to learn -- this is what makes
  // the mount read fall out of the same predicate as the interval.
  if (state.circumstances === undefined) return true
  return !isTerminal(verdictFor(state.circumstances), state.circumstances)
}

/**
 * How often to re-read, in milliseconds, or `null` when `shouldPoll` says
 * there is nothing left to learn.
 *
 * Faster only for the pool on screen's own awaited seat, since the refresh
 * reads the pool on screen and a faster one would not find a seat bought
 * elsewhere any sooner -- and for a call that may still land, whose pool every
 * refresh reads wherever the buyer is.
 */
export function refreshInterval(state: State): number | null {
  if (!shouldPoll(state)) return null
  if (isConfirmingSeat(state) || state.sending?.unconfirmed !== undefined) {
    return PROMPT_REFRESH_INTERVAL_MS
  }
  return REFRESH_INTERVAL_MS
}

function isTerminal(verdict: Verdict, circumstances: Circumstances): boolean {
  // Before `closed`: a released pool's link is history, so it can still be
  // read once the boxes are gone. A link found is the last thing to learn.
  if (verdict.situation === 'released') return Boolean(circumstances.edition)
  if (verdict.closed) return true
  if (verdict.situation === 'refunded') {
    return !verdict.owed && verdict.actions.length === 0
  }
  return false
}

/**
 * Whether the buyer has paid for a seat the chain has not shown yet.
 *
 * Exported because the difference between this and "you hold no seat here" is
 * the difference between a true sentence and a lie told to someone who has
 * just spent money. The 200 is authoritative; a box read that disagrees with
 * it is behind, not right.
 */
export function isConfirmingSeat(state: State): boolean {
  return state.awaitingSeat !== undefined && state.awaitingSeat === state.agreementId
}

/** The verdict for whatever has been read, or `null` before the first read. */
export function verdictOf(state: State): Verdict | null {
  return state.circumstances ? verdictFor(state.circumstances) : null
}

/**
 * Everything about the pool currently on screen, cleared together.
 *
 * Grouped into one helper because these fields all describe one pool, and
 * forgetting one of them is how a buyer ends up reading the previous pool's
 * transaction id under this pool's heading.
 *
 * `sending` is deliberately not among them. It describes a call, not a screen,
 * and clearing it here would let a buyer who left a pool and came straight
 * back be offered the call they had just sent.
 */
function withoutPool(state: State): State {
  return {
    ...state,
    circumstances: undefined,
    readError: undefined,
    reading: false,
    actionError: undefined,
    lastTxId: undefined,
  }
}

/**
 * Put the pool a payment went into on screen, and read it.
 *
 * When that is not the pool already on screen -- the buyer opened another one,
 * or went back to `/app`, while the wallet prompt was open -- the URL moves
 * with it. Leaving the address bar behind is the one split that undoes
 * everything a settlement sets up: the router's next `OPENED_POOL` for the
 * URL's pool takes the paid pool off screen, a copied link names the wrong
 * pool, and a reload loses `awaitingSeat`, which is the only thing standing
 * between the buyer and "you hold no seat here".
 *
 * Only on a change. On the ordinary path `QUOTE_RECEIVED` already minted this
 * URL, and navigating to the address already in the bar would add a history
 * entry that goes nowhere.
 */
function intoPaidPool(state: State, agreementId: bigint, patch: Partial<State>): Result {
  const read = readPool({ ...showing(state, agreementId), ...patch })
  if (state.agreementId === agreementId) return read
  return {
    state: read.state,
    effects: [{ type: 'NAVIGATE', path: poolPath(agreementId) }, ...read.effects],
  }
}

/** Whether a completion belongs to the call in flight -- same call, same pool. */
function completes(
  sending: Sending | undefined,
  event: { action: Action; agreementId: bigint },
): sending is Sending {
  return (
    sending !== undefined &&
    sending.action === event.action &&
    sending.agreementId === event.agreementId
  )
}

/**
 * Put `agreementId` on screen, dropping everything about the previous pool
 * when it is a different one.
 *
 * Three events move the screen besides the router -- a quote for another
 * edition, and a settlement or unreadable receipt naming one -- and each has
 * to leave the same state the router would. Otherwise the router's own
 * `OPENED_POOL`, arriving a moment later, finds the id already set, reads it
 * as a remount and keeps the previous pool's reading under the new heading.
 */
function showing(state: State, agreementId: bigint): State {
  if (state.agreementId === agreementId) return state
  return { ...withoutPool(state), agreementId }
}

export function reduce(state: State, event: Event): Result {
  switch (event.type) {
    case 'WALLET_CONNECTED': {
      // The wallet layer may report the same session more than once -- on
      // mount, on a re-render -- and nothing read so far has become wrong.
      if (state.wallet === event.address) return only(state)
      // Two of the things a read produces belong to a wallet -- the seat and
      // whether it can pay a fee -- so circumstances read for the previous
      // wallet are not merely stale, they are about someone else. Dropping
      // them shows "reading…" for a moment; keeping them would show a
      // stranger's seat as this buyer's.
      //
      // Readings already in flight were asked for the previous wallet, and are
      // dropped when they land. Nothing waits on them in vain: `readPool` below
      // asks again for this wallet -- for the pool on screen, and for the pool
      // of a call that may still land -- so whatever those readings would have
      // released, `reading` or a held lock, one of these releases instead.
      const next: State = {
        ...state,
        wallet: event.address,
        circumstances: undefined,
        // Found for the wallet that has just been replaced.
        myPools: undefined,
        myPoolsError: undefined,
        // A search in flight is for that wallet too, and its answer will be
        // dropped, so nothing is being searched for this one. Left set, the
        // "find my seats" button would stay disabled for the session.
        findingMyPools: false,
      }
      return readPool(next)
    }

    case 'WALLET_DISCONNECTED': {
      if (state.wallet === undefined) return only(state)
      // The quote survives: the pooled 402 never names a buyer, so it is
      // still payable by whichever wallet connects next. The seat and the
      // fee check do not, for the same reason as above.
      const next: State = {
        ...state,
        wallet: undefined,
        circumstances: undefined,
        myPools: undefined,
        myPoolsError: undefined,
        findingMyPools: false,
      }
      return readPool(next)
    }

    case 'OPENED_ENTRY':
      // The router is at `/app`, so no pool is on screen. `awaitingSeat`
      // deliberately survives: it is a fact about a payment that happened,
      // not about a screen, and a buyer who navigates away and back must not
      // be told their seat is missing on the strength of the first stale read
      // after they return.
      return only({ ...withoutPool(state), agreementId: undefined })

    case 'OPENED_POOL': {
      // Re-opening the same pool -- a remount, a refresh -- keeps what has
      // already been read so the screen does not blink; opening a different
      // one keeps nothing, since every field it would keep describes the pool
      // being left.
      return readPool(showing(state, event.agreementId))
    }

    case 'QUOTE_REQUESTED':
      if (!MAY_QUOTE.has(state.purchase)) return only(state)
      // No wallet needed. The 402 does not name the buyer and the pooled
      // route creates nothing when it answers one, so a price can be shown
      // before a handshake -- and `BUY_REQUESTED` is where a wallet becomes
      // necessary, because that is where something gets signed.
      return {
        state: {
          ...state,
          purchase: 'QUOTING',
          quote: undefined,
          unavailable: undefined,
          purchaseError: undefined,
        },
        effects: [{ type: 'REQUEST_QUOTE' }],
      }

    case 'QUOTE_RECEIVED': {
      // A quote that arrives after the buyer moved on describes a pass this
      // machine is no longer running.
      if (state.purchase !== 'QUOTING') return only(state)
      // The link is minted here, before anything can fail. The agreement id
      // is known the moment the 402 is read, so a buyer whose signature is
      // declined -- or whose tab closes mid-prompt -- still has a URL that
      // reaches the pool.
      //
      // The pooled route sells whichever edition is open, so a buyer sitting
      // on a stale link may be quoted a different pool than the one they were
      // looking at. That is not an error: the machine follows the quote.
      const id = event.quote.agreementId
      const next = showing(
        {
          ...state,
          purchase: 'CONFIRM',
          quote: event.quote,
          unavailable: undefined,
          purchaseError: undefined,
        },
        id,
      )
      const navigate: Effect = { type: 'NAVIGATE', path: poolPath(id) }
      if (state.agreementId === id) return { state: next, effects: [navigate] }
      // A different pool is now on screen with nothing read about it. The
      // router will ask too, once the URL changes; asking here as well means
      // the confirm screen does not depend on the router noticing.
      const read = readPool(next)
      return { state: read.state, effects: [navigate, ...read.effects] }
    }

    case 'QUOTE_REFUSED':
      if (state.purchase !== 'QUOTING') return only(state)
      // Not an error state. `no_pool_open` means the chain was read and no
      // edition is open, which is an answer to the buyer's question.
      return only({
        ...state,
        purchase: 'UNAVAILABLE',
        quote: undefined,
        unavailable: { reason: event.reason, message: event.message },
      })

    case 'QUOTE_FAILED':
      if (state.purchase !== 'QUOTING') return only(state)
      return only({
        ...state,
        purchase: 'IDLE',
        quote: undefined,
        purchaseError: { kind: 'quote_failed', message: event.error },
      })

    case 'BUY_REQUESTED': {
      if (state.purchase !== 'CONFIRM') return only(state)
      // `CONFIRM` always holds a quote, and a wallet is what will sign. Both
      // are read rather than asserted: a build with either missing would ask
      // the runner to sign nothing, or to sign for nobody.
      if (!state.quote || !state.wallet) return only(state)
      // One freshness check, here, at the last moment before the wallet is
      // disturbed. Checking at quote time as well would answer a question
      // this check answers better, with more of the interval elapsed.
      return {
        state: { ...state, purchase: 'CHECKING', purchaseError: undefined },
        effects: [{ type: 'VERIFY_QUOTE', quote: state.quote }],
      }
    }

    case 'QUOTE_VERIFIED':
      if (state.purchase !== 'CHECKING' || !state.quote) return only(state)
      return {
        state: { ...state, purchase: 'SETTLING' },
        effects: [{ type: 'BUILD_AND_SIGN', quote: state.quote }],
      }

    case 'QUOTE_STALE':
      if (state.purchase !== 'CHECKING') return only(state)
      // Nothing was signed and nothing was spent. The quote is dropped rather
      // than kept for a retry, because every reason it is here says the
      // photograph no longer matches the pool.
      return only({
        ...state,
        purchase: 'IDLE',
        quote: undefined,
        purchaseError: {
          kind: 'quote_stale',
          message:
            `The ${event.noun ?? 'pool'} moved while this quote was open, so nothing was signed. ` +
            event.disagreements.join(' '),
        },
      })

    case 'QUOTE_UNVERIFIABLE':
      if (state.purchase !== 'CHECKING') return only(state)
      // The node did not answer, which is not the same as the quote being
      // stale and must never be reported as it. The quote is still good as
      // far as anyone knows, so this goes back to the confirm screen with the
      // reason rather than throwing the pass away.
      return only({
        ...state,
        purchase: 'CONFIRM',
        purchaseError: { kind: 'quote_unverifiable', message: event.error },
      })

    case 'SETTLED':
      // Accepted from any purchase state, on purpose. A 200 means the money
      // has moved, and where the machine happens to be standing when the news
      // arrives cannot make that untrue -- this is the lesson `src/machine.ts`
      // records at its own `SETTLED`, where a status guard once dropped the
      // only proof of payment the client ever gets.
      //
      // None of the receipt's other fields are stored. `seatsTotal`,
      // `deadline` and `commitSha256` are all in the agreement box, and the
      // box is the thing that stays true; the one fact the box cannot yet
      // report is that this buyer paid, and that is what `awaitingSeat` is.
      return intoPaidPool(state, event.agreementId, {
        purchase: 'IDLE',
        quote: undefined,
        purchaseError: undefined,
        unavailable: undefined,
        awaitingSeat: event.agreementId,
      })

    case 'RECEIPT_UNREADABLE':
      // The payment settled; only the answer to it is unreadable. So this
      // does everything `SETTLED` does, and additionally says so -- the
      // message is the one that tells the buyer not to pay again, and it is
      // cleared by the read that finds their seat.
      return intoPaidPool(state, event.agreementId, {
        purchase: 'IDLE',
        quote: undefined,
        unavailable: undefined,
        purchaseError: { kind: 'receipt_unreadable', message: event.message },
        awaitingSeat: event.agreementId,
      })

    case 'SETTLE_FAILED':
      if (state.purchase !== 'SETTLING') return only(state)
      // Back to a resting state a new pass may start from, with the quote
      // dropped: a pass is free here, and a fresh one is worth more than a
      // photograph taken before a failed attempt. No read is asked for --
      // if the settlement in fact landed, the refresh predicate is already
      // true for this pool and the next tick shows the seat.
      return only({
        ...state,
        purchase: 'IDLE',
        quote: undefined,
        purchaseError: { kind: 'settle_failed', message: event.error },
      })

    case 'READ_REQUESTED':
      return readPool(state)

    case 'READ_RECEIVED': {
      // Read for a wallet that is no longer the one connected. Dropped whole,
      // before anything else looks at it: its seat and its fee check are
      // about somebody else, and the wallet change that made it stale has
      // already asked for the readings that replace it -- see
      // `WALLET_CONNECTED` -- so neither `reading` nor a held lock is left
      // waiting on this one.
      if (!forWallet(state, event)) return only(state)

      // A call that may still have landed holds its lock until a reading shows
      // what became of it -- whichever pool is on screen. Not merely the next
      // reading of its pool: one taken before the call was committed shows the
      // pool as it was, and still offers the call. Once a reading is final for
      // the call, its outcome is in the verdict: if it landed, the chain has
      // moved on; if it did not, offering it again is right.
      const pending = state.sending
      const base: State =
        pending?.unconfirmed !== undefined &&
        pending.agreementId === event.agreementId &&
        event.finalFor === pending.unconfirmed.txId
          ? { ...state, sending: undefined }
          : state

      // A read that outlived the screen it was started for. Its circumstances
      // describe a pool nobody is looking at, and storing them would render
      // that pool under this one's heading. `reading` is left alone: if a
      // read of the pool on screen is outstanding, it still is.
      if (event.agreementId !== state.agreementId) return only(base)

      const seatShowed = event.circumstances.seat !== null
      // The boxes are gone, so the roster is gone with them and no future
      // read can ever show the seat. Holding the flag past that point would
      // poll forever against a question the chain can no longer answer -- and
      // a closed pool is a screen of its own, not a seat that failed to
      // appear.
      const boxesGone = event.circumstances.record === null
      const resolved =
        event.agreementId === state.awaitingSeat && (seatShowed || boxesGone)

      // What an unreadable receipt said is answered once the seat it paid for
      // is on the roster, and only then. Two narrower cases keep the message:
      //
      //   - The boxes went instead. The roster went with them, so the payment
      //     was never confirmed on screen, and "your payment went through" is
      //     still the one true thing to say about it. Its kind is what says
      //     it, so it no longer depends on `awaitingSeat`, which clears here.
      //   - The message is about a later attempt -- a second purchase the
      //     contract refused while this seat was still awaited. The seat
      //     appearing answers the first payment, not that one.
      return only({
        ...base,
        reading: false,
        readError: undefined,
        circumstances: event.circumstances,
        awaitingSeat: resolved ? undefined : state.awaitingSeat,
        purchaseError:
          resolved && seatShowed && state.purchaseError?.kind === 'receipt_unreadable'
            ? undefined
            : state.purchaseError,
      })
    }

    case 'READ_FAILED':
      // Same reasoning as above: a failure reading some other pool, or for
      // some other wallet, says nothing about this screen and must not put an
      // error on it.
      if (!forWallet(state, event)) return only(state)
      if (event.agreementId !== state.agreementId) return only(state)
      // The last good circumstances stay. A node that did not answer must not
      // blank a screen that was correct a moment ago.
      return only({
        ...state,
        reading: false,
        readError: { kind: event.kind, message: event.error },
      })

    case 'ACTION_REQUESTED': {
      // One call at a time, across every pool. A second prompt while the
      // first call is still travelling asks the buyer to sign something whose
      // premise the first call may be about to change, and whatever the chain
      // then refuses comes back as an error they can do nothing with.
      if (state.sending !== undefined) return only(state)
      if (state.agreementId === undefined || !state.circumstances) return only(state)
      // The verdict decides, and it decides alone. Re-checking the contract's
      // asserts here would be a second copy of guards that already exist in
      // `view.ts`, and two copies of a rule are two places for it to drift.
      // An action the verdict did not offer means the button should not have
      // existed, so this is inert rather than an error.
      if (!verdictFor(state.circumstances).actions.includes(event.action)) {
        return only(state)
      }
      const agreementId = state.agreementId
      return {
        state: {
          ...state,
          sending: { action: event.action, agreementId },
          actionError: undefined,
        },
        effects: [{ type: 'SEND_ACTION', action: event.action, agreementId }],
      }
    }

    // The three ways a call ends. Each is taken only when it names the call in
    // flight -- same action, same pool -- and is otherwise inert: a completion
    // that matches nothing belongs to no call this machine is waiting on, and
    // letting it through would release somebody else's lock.
    //
    // What each one shows lands only when its pool is the one on screen. A
    // transaction id or an error under another pool's heading describes a
    // call this buyer never made there.

    case 'ACTION_SENT': {
      if (!completes(state.sending, event)) return only(state)
      const released: State = { ...state, sending: undefined }
      // Not on screen, so there is nothing to show and no read worth asking
      // for: a reading of another pool is discarded on arrival, and this one
      // is read afresh whenever it is opened.
      if (event.agreementId !== state.agreementId) return only(released)
      // The chain has moved, so the screen is showing the state before it did.
      return readPool({ ...released, actionError: undefined, lastTxId: event.txId })
    }

    case 'ACTION_UNCONFIRMED': {
      if (!completes(state.sending, event)) return only(state)
      // The lock holds. Only a reading that shows the call's outcome can say
      // whether it landed, and `readPool` asks for this pool's even when it is
      // not on screen, because that reading is what releases the lock.
      const held: State = {
        ...state,
        sending: {
          ...state.sending,
          unconfirmed: { txId: event.txId, lastValid: event.lastValid },
        },
      }
      if (event.agreementId !== state.agreementId) return readPool(held)
      // The id is shown because it is real and may yet confirm: it is the one
      // thing a buyer can look up to find out.
      return readPool({ ...held, actionError: undefined, lastTxId: event.txId })
    }

    case 'ACTION_FAILED': {
      if (!completes(state.sending, event)) return only(state)
      const released: State = { ...state, sending: undefined }
      if (event.agreementId !== state.agreementId) return only(released)
      // Re-read rather than leave the verdict that produced this call on
      // screen: it may be exactly why the call was refused, and it would go
      // on offering the same button.
      return readPool({ ...released, actionError: event.error })
    }

    case 'MY_POOLS_REQUESTED':
      if (!state.wallet) return only(state)
      return {
        state: { ...state, findingMyPools: true, myPoolsError: undefined },
        effects: [{ type: 'FIND_MY_POOLS', address: state.wallet }],
      }

    case 'MY_POOLS_FOUND':
      // Found for a wallet that has since been replaced: those are not this
      // wallet's seats, and listing them would send the buyer to pools they
      // never joined. The wallet change already stopped counting the search
      // as in flight.
      if (event.address !== state.wallet) return only(state)
      return only({
        ...state,
        findingMyPools: false,
        myPoolsError: undefined,
        myPools: event.agreementIds.map((agreementId) => ({ agreementId })),
      })

    case 'MY_POOLS_UNAVAILABLE':
      if (event.address !== state.wallet) return only(state)
      // Whatever was found before stays. This path exists so a buyer who lost
      // their link has somewhere to look; a search that fails costs that
      // fallback, and touches nothing else in the state.
      return only({ ...state, findingMyPools: false, myPoolsError: event.error })

    default:
      return exhaustive(event, state)
  }
}

/**
 * Ask for a read of whatever pool is on screen, or do nothing when none is.
 *
 * Every caller reaches here for the same reason -- something happened that
 * the chain now says something new about -- so the "is there a pool" test
 * lives once rather than at each of the call sites.
 *
 * Also the pool of a call that may still land, when that is a different pool.
 * Its lock is released only by a reading of that pool, and nothing else would
 * ask for one once the buyer has moved on: the refresh reads the pool on
 * screen, and a single read that failed would otherwise hold the lock -- which
 * covers every pool -- until they happened to open that pool again.
 *
 * Reads are never refused for being concurrent. They cost nothing, they move
 * nothing, and refusing one while another is outstanding would let a single
 * request that never answers stop the screen refreshing for the rest of the
 * session. `reading` is a display flag for the pool on screen, not a lock.
 */
function readPool(state: State): Result {
  const effects: Effect[] = []
  const address = state.wallet ?? null
  const pending = state.sending
  // Every reading of the pool a call may still land on is asked to check that
  // call, since any one of them may be the reading that releases its lock.
  const read = (agreementId: bigint): Effect =>
    pending?.unconfirmed !== undefined && pending.agreementId === agreementId
      ? { type: 'READ_POOL', agreementId, address, call: pending.unconfirmed }
      : { type: 'READ_POOL', agreementId, address }

  let next = state
  if (state.agreementId !== undefined) {
    next = { ...state, reading: true }
    effects.push(read(state.agreementId))
  }
  if (pending?.unconfirmed !== undefined && pending.agreementId !== state.agreementId) {
    effects.push(read(pending.agreementId))
  }
  return { state: next, effects }
}

/**
 * Whether a reading was made for the wallet connected now -- `null` matching
 * no wallet at all.
 *
 * Kept beside `readPool` because the two are one rule: every reading is asked
 * for the current wallet, and only a reading for the current wallet is heard.
 */
function forWallet(state: State, event: { address: string | null }): boolean {
  return event.address === (state.wallet ?? null)
}
