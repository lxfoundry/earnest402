import algosdk from 'algosdk'
import {
  RouteUnavailableError,
  SettlementUnreadableError,
  requestSeatQuote,
  settleSeat,
  type QuotedRoute,
} from '../api'
import {
  buildClaimRefund,
  buildClose,
  buildExpire,
  buildRefundNext,
} from '../chain/calls'
import { BoxDecodeError, findSeat } from '../chain/decode'
import {
  NetworkMismatchError,
  canPayFee,
  chainTimestamp,
  findJoinedAgreements,
  readAgreement,
  readClosedOutcome,
  readEditionLink,
  readPoolBoxes,
  suggestedParams,
  type ClosedOutcome,
  type EditionLink,
} from '../chain/read'
import { copyFor } from '../brand'
import { getBrand, getConfig } from '../config'
import { buildPayment, withBuyer, type Signer } from '../payment/schemeClient'
import type { Effect, Event, ReadFailureKind, UnconfirmedCall } from './machine'
import {
  quoteDisagreements,
  refundBatchSize,
  verdictFor,
  type Action,
  type SeatQuote,
} from './view'

/**
 * The impure half: one effect in, the events it produced out.
 *
 * **`run` never throws.** Every failure -- a refused route, an unreachable
 * node, a wallet that declined, a transaction the chain rejected -- comes back
 * as an event the reducer already prices. A runner that threw would leave the
 * machine in a state no event can leave, with a spinner and no way out, and
 * the paths where that happens are exactly the paths where money is moving.
 *
 * Every branch below therefore wraps everything it does, including reading
 * the configuration, in its own `try`.
 */

export interface Runtime {
  algod: algosdk.Algodv2
  indexer: algosdk.Indexer
  /** Null until a wallet is connected. */
  signer: Signer | null
  address: string | null
  navigate: (path: string) => void
}

const messageOf = (error: unknown): string =>
  error instanceof Error ? error.message : String(error)

/**
 * The 402 exactly as it arrived, kept beside the parsed quote.
 *
 * The facilitator compares the requirements it is sent against the ones it
 * issued, byte for byte, so anything re-serialised from a parsed struct -- a
 * dropped optional field, a number that became a string -- is a settlement
 * that fails for a reason no log points at. `api.ts` keeps `accepted` and
 * `envelope` untouched for that purpose, and the settlement needs them back.
 *
 * It lives here rather than in the reducer because it is opaque wire data,
 * not state: nothing about it is a decision, nothing reads a field of it, and
 * putting it in the machine would put an un-inspectable blob in the one place
 * that is meant to be inspectable and serialisable. Keyed by agreement id so
 * a settlement for an earlier quote still finds its own envelope rather than
 * whichever was most recent.
 */
const QUOTED_ROUTES = new Map<string, QuotedRoute>()

/** Test seam: drop remembered envelopes so one test cannot feed another. */
export function resetQuotedRoutesForTests(): void {
  QUOTED_ROUTES.clear()
}

/**
 * The pool half of the 402, which `api.ts` does not parse.
 *
 * `api.ts`'s `quote` keeps only what the *payment* needs, so the seat counts,
 * the deadline and the commitment are read from `accepted.extra` here. Read
 * strictly: a missing `seatsLeft` would become `NaN`, every freshness check
 * would then disagree, and the buyer would be refused a seat forever with a
 * message blaming the pool. A 402 this client cannot understand is a failed
 * quote, which the caller already has to handle.
 */
function seatQuoteFrom(quoted: QuotedRoute): SeatQuote {
  const extra = (quoted.accepted.extra ?? {}) as Record<string, unknown>
  return {
    // Both already parsed and validated by `api.ts`, from the same 402.
    agreementId: quoted.quote.agreementId,
    amount: quoted.quote.amount,
    seatsTotal: wholeNumber(extra.seatsTotal, 'seatsTotal'),
    seatsLeft: wholeNumber(extra.seatsLeft, 'seatsLeft'),
    deadline: wholeNumber(extra.deadline, 'deadline'),
    commitSha256: hex64(extra.commitSha256),
  }
}

function wholeNumber(raw: unknown, field: string): number {
  const value = Number(raw)
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(
      `the 402's extra.${field} is ${JSON.stringify(raw)}, which is not a ` +
        'whole number of seats or seconds. Refusing the quote rather than ' +
        `comparing the ${product().nouns.pool} against a value that cannot match it.`,
    )
  }
  return value
}

function hex64(raw: unknown): string {
  if (typeof raw !== 'string' || !/^[0-9a-fA-F]{64}$/.test(raw)) {
    throw new Error(
      `the 402's extra.commitSha256 is ${JSON.stringify(raw)}, which is not a ` +
        'sha256. That value is the whole of what a seat buys, so a quote ' +
        'that does not name one legibly is not payable.',
    )
  }
  // `SeatQuote` promises lowercase, and a screen that compares what it shows
  // against a published digest should not have to know the server's casing.
  return raw.toLowerCase()
}

export async function run(effect: Effect, runtime: Runtime): Promise<Event[]> {
  switch (effect.type) {
    case 'REQUEST_QUOTE':
      return await requestQuote()

    case 'VERIFY_QUOTE':
      return await verifyQuote(effect.quote, runtime)

    case 'BUILD_AND_SIGN':
      return await buildAndSign(effect.quote, runtime)

    case 'READ_POOL':
      return await readPool(effect.agreementId, effect.address, effect.call, runtime)

    case 'SEND_ACTION':
      return await sendAction(effect.action, effect.agreementId, runtime)

    case 'FIND_MY_POOLS':
      return await findMyPools(effect.address, runtime)

    case 'NAVIGATE':
      try {
        runtime.navigate(effect.path)
      } catch {
        // A URL that would not change is not worth an event: the reducer
        // already holds the agreement id, so the screen is correct either
        // way and only the address bar is behind. There is no event for
        // "the history API refused", and inventing one would give the
        // reducer a failure it can do nothing about.
      }
      return []

    default:
      return exhaustive(effect)
  }
}

/**
 * Compile-time exhaustiveness guard, matching the reducer's. An `Effect`
 * variant with no branch above makes this call fail to compile.
 */
function exhaustive(_effect: never): Event[] {
  return []
}

async function requestQuote(): Promise<Event[]> {
  try {
    const quoted = await requestSeatQuote()
    const quote = seatQuoteFrom(quoted)
    QUOTED_ROUTES.set(String(quote.agreementId), quoted)
    return [{ type: 'QUOTE_RECEIVED', quote }]
  } catch (error) {
    if (error instanceof RouteUnavailableError) {
      // A refusal the server explained. `no_pool_open` is an answer -- the
      // chain was read and no edition is open -- and `pool_status_unavailable`
      // is the transient twin. The reason travels intact so the screen can
      // tell them apart; collapsing them into a generic failure would report
      // a closed edition as a broken client.
      return [
        { type: 'QUOTE_REFUSED', reason: error.reason, message: error.message },
      ]
    }
    return [{ type: 'QUOTE_FAILED', error: messageOf(error) }]
  }
}

async function verifyQuote(quote: SeatQuote, runtime: Runtime): Promise<Event[]> {
  try {
    const record = await readAgreement(
      runtime.algod,
      getConfig().appId,
      quote.agreementId,
    )
    const { nouns } = product()
    const disagreements = quoteDisagreements(quote, record, nouns)
    return disagreements.length === 0
      ? [{ type: 'QUOTE_VERIFIED' }]
      : [{ type: 'QUOTE_STALE', disagreements, noun: nouns.pool }]
  } catch (error) {
    // Everything that lands here is "the check could not be made" -- an
    // unreachable node, a bundle pointed at the wrong chain, a record this
    // client cannot decode. None of them is evidence the quote went stale,
    // and reporting them as staleness would tell a buyer the pool moved when
    // nobody looked at the pool at all.
    return [{ type: 'QUOTE_UNVERIFIABLE', error: messageOf(error) }]
  }
}

async function buildAndSign(quote: SeatQuote, runtime: Runtime): Promise<Event[]> {
  const key = String(quote.agreementId)
  const quoted = QUOTED_ROUTES.get(key)
  if (!quoted) {
    return [
      {
        type: 'SETTLE_FAILED',
        error:
          'This quote was issued by a page that is no longer loaded, so the ' +
          'original 402 is gone and the facilitator would reject a rebuilt ' +
          'one. Ask for a new quote; it costs nothing.',
      },
    ]
  }
  if (!runtime.signer || !runtime.address) {
    return [{ type: 'SETTLE_FAILED', error: 'Connect a wallet before buying a seat.' }]
  }

  try {
    const payload = await buildPayment(
      // The 402 names no buyer -- putting an address in the request would
      // change the resource identity -- so the connected wallet is attached
      // here, immediately before the group is built.
      withBuyer(quoted.quote, runtime.address),
      runtime.signer,
      await suggestedParams(runtime.algod),
    )
    const { receipt } = await settleSeat(quoted, payload)
    // Spent: the seat is bought, and a second settlement against the same
    // envelope is a payment nobody asked for.
    QUOTED_ROUTES.delete(key)
    if (receipt.agreementId !== quote.agreementId) {
      // The group this wallet signed joins `quote.agreementId`; that is where
      // the money went, whatever the answer says. A receipt naming another
      // pool contradicts the signature, so it is not trusted -- taken at its
      // word, it would have the buyer waiting on a seat in a pool they never
      // joined, polling for one that can never appear.
      const { pool } = product().nouns
      return [
        {
          type: 'RECEIPT_UNREADABLE',
          agreementId: quote.agreementId,
          message:
            `The payment settled, but the receipt names ${pool} ` +
            `${receipt.agreementId} and the payment joined ${pool} ` +
            `${quote.agreementId}. The payment is on chain: do not pay again.`,
        },
      ]
    }
    return [
      {
        type: 'SETTLED',
        agreementId: quote.agreementId,
        seatsTotal: receipt.seatsTotal,
        deadline: receipt.deadline,
        commitSha256: receipt.commitSha256,
      },
    ]
  } catch (error) {
    if (error instanceof SettlementUnreadableError) {
      // Past this line in `api.ts` the money has already moved, so this is a
      // receipt that cannot be read and never a payment that did not happen.
      // The agreement is known regardless -- it is the one this quote named
      // and the one the group paid into -- so the seat can still be waited
      // for.
      QUOTED_ROUTES.delete(key)
      return [
        {
          type: 'RECEIPT_UNREADABLE',
          agreementId: quote.agreementId,
          message: messageOf(error),
        },
      ]
    }
    return [{ type: 'SETTLE_FAILED', error: messageOf(error) }]
  }
}

/**
 * One reading of a pool, for the wallet the reducer named.
 *
 * `address` comes from the effect, never from `runtime.address`. The reducer
 * drops a reading whose tag is not the wallet connected now, so the tag has to
 * be the address the seat and the fee were actually read for -- and reading
 * for the effect's address makes that true by construction. Reading for the
 * runtime's instead would let the two differ across a wallet change, and a
 * reading tagged for one wallet but made for another is exactly the stale
 * answer the tag exists to catch.
 */
async function readPool(
  agreementId: bigint,
  address: string | null,
  call: UnconfirmedCall | undefined,
  runtime: Runtime,
): Promise<Event[]> {
  try {
    const { appId } = getConfig()
    // All at once. None of these answers depends on another, and this runs on
    // a refresh interval against a public node, where six round trips in a row
    // are six chances to be slow.
    const [{ record, roster, round }, chainNow, fee, fate] = await Promise.all([
      readPoolBoxes(runtime.algod, appId, agreementId),
      // Never `Date.now()`: the contract compares deadlines against the
      // previous block's timestamp, so a browser clock runs ahead of it and
      // would offer an `expire` the chain refuses. The refusal costs no fee,
      // since a refused call is never committed; it costs a wallet prompt
      // signed for nothing and an error the buyer cannot act on.
      chainTimestamp(runtime.algod),
      // A question about a wallet, so with no wallet connected there is
      // nothing to ask and no round trip to spend asking it. `false` makes
      // `verdictFor` withhold every action, which the buyer is to be told
      // means the refund is coming -- not that they are at a dead end.
      address ? canPayFee(runtime.algod, address) : Promise.resolve(false),
      call ? transactionFate(runtime.algod, call.txId) : Promise.resolve(undefined),
    ])
    const seat = address ? findSeat(roster, address) : null

    let closedOutcome: ClosedOutcome | undefined
    if (record === null) {
      // Only worth asking once the boxes are gone: `close` deletes them, so a
      // delivered pool and a refunded one are indistinguishable on algod and
      // the application's own history is the only thing that separates them.
      try {
        closedOutcome = await readClosedOutcome(runtime.indexer, appId, agreementId)
      } catch {
        // `undefined` is a documented value of this field -- "not asked or
        // did not answer" -- and it renders as `gone`, which is not an error.
        // A public indexer is a third-party host and must never be able to
        // take the algod-backed half of this read down with it.
        closedOutcome = undefined
      }
    }

    let edition: EditionLink | null | undefined
    const released = record === null ? closedOutcome === 'released' : record.state === 'RELEASED'
    if (released) {
      // The link's only record is the note on the release, which is history
      // and survives `close`, so it is asked for the same way either side of
      // it -- and on every reading until it is found, since the indexer can
      // trail the node that already shows the pool released.
      try {
        edition = await readEditionLink(runtime.indexer, appId, agreementId, product().kind)
      } catch {
        // Same reasoning as the closed outcome: the indexer is a third-party
        // host, and its failure costs the link, never the reading.
        edition = undefined
      }
    }

    const received: Event = {
      type: 'READ_RECEIVED',
      agreementId,
      address,
      circumstances: { record, seat, closedOutcome, edition, chainNow, canPayFee: fee },
    }
    if (call && fate !== undefined && showsOutcome(call, round, fate)) {
      received.finalFor = call.txId
    }
    return [received]
  } catch (error) {
    return [
      {
        type: 'READ_FAILED',
        agreementId,
        address,
        kind: readFailureKind(error),
        error: messageOf(error),
      },
    ]
  }
}

/**
 * Whether a failed read is worth repeating.
 *
 * Only the two failures that are properties of the build are `incompatible`:
 * a node on another network than the bundle names, and a record the decoders
 * refuse. Everything else -- including anything unrecognised -- is treated as
 * a moment, because stopping the refresh over a failure that would have passed
 * leaves a screen that never updates.
 */
function readFailureKind(error: unknown): ReadFailureKind {
  return error instanceof NetworkMismatchError || error instanceof BoxDecodeError
    ? 'incompatible'
    : 'unreachable'
}

/** What the node says about a submitted transaction, reduced to what matters here. */
type Fate = { confirmedRound: bigint } | { dropped: string } | 'pending' | 'unknown'

/**
 * Ask the node about a transaction, never throwing.
 *
 * `unknown` covers a node that cannot be asked and a node that does not know
 * the transaction -- one it never admitted, or one committed long enough ago
 * that it no longer remembers it. Neither is a statement about the
 * transaction, so neither may be read as one.
 */
async function transactionFate(algod: algosdk.Algodv2, txId: string): Promise<Fate> {
  try {
    const pending = await algod.pendingTransactionInformation(txId).do()
    if (pending.confirmedRound) return { confirmedRound: pending.confirmedRound }
    if (pending.poolError) return { dropped: pending.poolError }
    return 'pending'
  } catch {
    return 'unknown'
  }
}

/**
 * Whether boxes read at `round` already show what became of `call`.
 *
 * Only then may the lock on it go. A reading from before the call was
 * committed shows the pool as it was, and would put the same call back in
 * front of the buyer while the first is still travelling.
 *
 *   - The boxes are gone: nothing is left to send against, whatever the call
 *     did.
 *   - The boxes were read at or after the call's last valid round: it has
 *     either been committed by then or can never be.
 *   - The node says it confirmed: the boxes show it once they were read at or
 *     after that round -- not before, since two requests to a public node can
 *     be answered a round apart.
 *   - The node says it was dropped: it will not land.
 */
function showsOutcome(call: UnconfirmedCall, round: bigint | null, fate: Fate): boolean {
  if (round === null) return true
  if (round >= call.lastValid) return true
  if (typeof fate !== 'object') return false
  return 'confirmedRound' in fate ? round >= fate.confirmedRound : true
}

/**
 * How long to wait for a call to confirm. Four rounds is over ten seconds of
 * block time, and it is what this repository's agent scripts wait for.
 *
 * Running out of rounds is not a failure. The transaction was accepted into
 * the pool and may still be committed, so it comes back as
 * `ACTION_UNCONFIRMED`: the lock stays on, and the next reading of the pool
 * says what actually happened.
 */
const WAIT_ROUNDS = 4

/**
 * Send one refund-path call, and say how far it got.
 *
 * Three phases, because where a failure happens decides what it means:
 *
 *   1. composing and signing -- nothing has left this client, so any failure
 *      is `ACTION_FAILED` and the call cannot land;
 *   2. submitting -- a node that answers with a refusal never admitted the
 *      transaction, but a submission whose answer never arrived may have been
 *      admitted before the connection dropped;
 *   3. waiting -- the transaction is in the pool, so only the node's own word
 *      that it was dropped is a failure. Anything else, a wait that ran out
 *      included, is `ACTION_UNCONFIRMED`.
 *
 * Reporting a call that may still land as failed would release the lock and
 * put the same button back in front of the buyer while the first call is
 * still travelling.
 */
async function sendAction(
  action: Action,
  agreementId: bigint,
  runtime: Runtime,
): Promise<Event[]> {
  const failed = (error: string): Event[] => [
    { type: 'ACTION_FAILED', action, agreementId, error },
  ]

  if (!runtime.signer || !runtime.address) {
    return failed('Connect a wallet before sending this call.')
  }

  let txId: string
  let lastValid: bigint
  let signature: Uint8Array
  try {
    const composed = await compose(action, agreementId, runtime.address, runtime.algod)
    if (typeof composed === 'string') return failed(composed)
    // Known before the transaction is sent, not read out of the response. A
    // submission whose answer is lost still has an id anyone can look up --
    // and the phases below depend on having it when nothing else came back.
    txId = composed.txID()
    lastValid = composed.lastValid
    const signed = await runtime.signer([algosdk.encodeUnsignedTransaction(composed)], [0])
    const bytes = signed[0]
    if (!bytes) {
      return failed('The wallet returned no signature, so nothing was sent.')
    }
    signature = bytes
  } catch (error) {
    return failed(messageOf(error))
  }

  const unconfirmed: Event[] = [
    { type: 'ACTION_UNCONFIRMED', action, agreementId, txId, lastValid },
  ]

  try {
    await runtime.algod.sendRawTransaction(signature).do()
  } catch (error) {
    // algosdk attaches the HTTP status to an error the node answered with,
    // and a node that answered with an error did not admit the transaction.
    // No status means no answer at all: a dropped connection, a timeout. The
    // transaction may have reached the pool before either happened.
    if (typeof (error as { status?: unknown } | null)?.status === 'number') {
      return failed(messageOf(error))
    }
    return unconfirmed
  }

  try {
    await algosdk.waitForConfirmation(runtime.algod, txId, WAIT_ROUNDS)
    return [{ type: 'ACTION_SENT', action, agreementId, txId }]
  } catch {
    // `waitForConfirmation` throws for a dropped transaction, for running out
    // of rounds and for a node that stopped answering, and says which only in
    // its message. Asked directly instead, rather than by matching wording
    // that belongs to another library.
    const fate = await transactionFate(runtime.algod, txId)
    if (typeof fate !== 'object') return unconfirmed
    if ('confirmedRound' in fate) return [{ type: 'ACTION_SENT', action, agreementId, txId }]
    return failed(fate.dropped)
  }
}

/**
 * How many rounds a refund-path call stays valid for: about five and a half
 * minutes at today's block time, where the network's default is closer to an
 * hour.
 *
 * This is how long a call that was never seen to confirm can hold the lock.
 * Most such calls release it sooner -- a reading asks the node about the
 * transaction, and one that confirmed or was dropped ends the wait -- but a
 * node that never admitted it, or has forgotten it, cannot answer, and then
 * only the window can: past its last round the call cannot land, so a reading
 * after it is final. An hour of "waiting for the chain" for a call that is
 * not coming would be a screen that is stuck.
 *
 * Not much shorter, because the window opens when the call is built, before
 * the wallet prompt: a buyer who takes longer than this to approve has signed
 * a transaction the node refuses. That refusal costs nothing -- a refused call
 * is never committed -- and releases the lock at once.
 */
const CALL_VALIDITY_ROUNDS = 120n

/** Each call, as the thing a buyer asked to do. */
const ASKED: Record<Action, string> = {
  expire: 'expiring it',
  refund_next: 'sending its next refunds',
  claim_refund: "claiming this seat's refund",
  close: 'closing it',
}

/**
 * Build the call `action` names against a fresh reading of the pool, or say
 * in the buyer's terms why there is nothing to build.
 */
async function compose(
  action: Action,
  agreementId: bigint,
  sender: string,
  algod: algosdk.Algodv2,
): Promise<algosdk.Transaction | string> {
  const { appId, assetId } = getConfig()

  // Read and build from the *same* read. The builders take the decoded record
  // and roster as arguments precisely so that the accounts array a call
  // carries is derived from the state it was composed against, rather than
  // from a second fetch that may already disagree with it.
  const [{ record, roster }, chainNow, params] = await Promise.all([
    readPoolBoxes(algod, appId, agreementId),
    chainTimestamp(algod),
    suggestedParams(algod),
  ])
  const { pool } = product().nouns
  if (record === null) {
    return (
      `${capitalised(pool)} ${agreementId} has no record on chain any more, so there is ` +
      `nothing left to send. Closing a ${pool} deletes its boxes.`
    )
  }

  // The verdict again, on this reading, before the wallet is asked anything.
  // The button was offered on a reading that may be half a minute old, and
  // someone else's call can move the pool in between -- the operator's refund
  // pass finishing is the ordinary case -- so a click is no evidence the call
  // is still one the contract accepts. Refused here, the buyer signs nothing;
  // refused on chain, they sign for nothing and are shown the refusal.
  //
  // `canPayFee: true` because that is not the question. Whether this wallet
  // can cover the fee was asked when the button was offered, and the node
  // answers it again when the call is submitted; this asks only whether the
  // chain still accepts the call.
  const seat = findSeat(roster, sender)
  const { actions } = verdictFor({ record, seat, chainNow, canPayFee: true })
  if (!actions.includes(action)) {
    return (
      `${capitalised(pool)} ${agreementId} changed since this page last read it: it is now ` +
      `${record.state}, and ${ASKED[action]} is not something the escrow app ` +
      'would accept any more. Nothing was sent to your wallet.'
    )
  }

  const base = {
    suggestedParams: {
      ...params,
      lastValid: BigInt(params.firstValid) + CALL_VALIDITY_ROUNDS,
    },
    sender,
    appId,
    assetId,
    agreementId,
  }

  switch (action) {
    case 'expire':
      return buildExpire({ ...base, agreement: record })
    case 'refund_next':
      return buildRefundNext({
        ...base,
        agreement: record,
        roster,
        count: refundBatchSize(record),
      })
    case 'claim_refund': {
      // This buyer's own seat, found on the roster rather than remembered:
      // the index is a fact about the box, and `claim_refund` on someone
      // else's seat would settle their refund and leave this one owed. The
      // verdict offers no claim without a seat, so this guard only narrows.
      if (seat === null) {
        return (
          `${sender} holds no seat on ${pool} ${agreementId}'s roster, so ` +
          'there is no refund to claim here.'
        )
      }
      return buildClaimRefund({ ...base, roster, seat: seat.index })
    }
    case 'close':
      return buildClose({ ...base, agreement: record })
    default:
      return exhaustiveAction(action)
  }
}

/**
 * Which product this build sells, as the messages written here name it and as
 * the release note is read: the index's pools and editions, or the travel
 * line's trips.
 *
 * Read from the brand, which `App` validated before anything could run an
 * effect, so this never throws in a running page.
 */
function product() {
  const { brand } = getBrand()
  return { kind: brand, nouns: copyFor(brand).nouns }
}

const capitalised = (word: string) => word.charAt(0).toUpperCase() + word.slice(1)

function exhaustiveAction(_action: never): string {
  return 'This client does not know how to send that call.'
}

async function findMyPools(address: string, runtime: Runtime): Promise<Event[]> {
  try {
    const agreementIds = await findJoinedAgreements(
      runtime.indexer,
      getConfig().appId,
      address,
    )
    // Tagged with the address searched, which is the effect's, for the same
    // reason a reading is.
    return [{ type: 'MY_POOLS_FOUND', address, agreementIds }]
  } catch (error) {
    return [{ type: 'MY_POOLS_UNAVAILABLE', address, error: messageOf(error) }]
  }
}
