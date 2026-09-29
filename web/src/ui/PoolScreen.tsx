import type { ReactElement, ReactNode } from 'react'
import { getConfig } from '../config'
import { isConfirmingSeat, verdictOf, type State } from '../pool/machine'
import type { PoolMachine } from '../pool/usePoolMachine'
import { editionFiles, type Circumstances, type Situation, type Verdict } from '../pool/view'
import { Actions } from './Actions'
import { useBrand } from './BrandContext'
import { DemoNotice } from './DemoNotice'
import { formatInstant, formatRemaining, formatUsdc } from './format'
import { Hex } from './Link'
import { OfferCard } from './OfferCard'
import { Purchase } from './Purchase'
import { Stamp } from './Stamp'

/**
 * `/app/pool/<id>`: one pool, as the chain last described it.
 *
 * Everything shown about the pool is `verdictOf(state)` and the circumstances
 * it was derived from. Nothing here decides what the buyer may do or what
 * situation they are in; the reducer and `verdictFor` do, and this renders
 * their answer.
 */
export function PoolScreen({ state, dispatch }: PoolMachine) {
  const { brand, copy } = useBrand()
  const id = state.agreementId
  // Between the address bar changing and the router's event landing. A pool
  // screen with no pool in the machine has nothing true to say yet.
  if (id === undefined) return <p className="muted">{copy.pool.opening}</p>

  const verdict = verdictOf(state)
  const { circumstances } = state
  return (
    <div className="pool">
      {brand === 'travel' ? (
        <>
          <div className="trip-banner">
            <h1>{copy.pool.title(String(id))}</h1>
          </div>
          {/* The offer, then the demo notice, both before any pay button: the
              confirm view below is where a seat is paid for. */}
          <TripOffer state={state} />
          <DemoNotice />
        </>
      ) : (
        <h1>{copy.pool.title(String(id))}</h1>
      )}
      <Purchase state={state} dispatch={dispatch} where="pool" />
      {verdict === null || circumstances === undefined ? (
        <FirstRead state={state} />
      ) : (
        <>
          {state.readError !== undefined && (
            // The reducer keeps the last good reading when a refresh fails,
            // so the screen says which one it is showing.
            <p role="status">
              {state.readError.kind === 'incompatible'
                ? copy.pool.readIncompatible
                : copy.pool.readFailed}{' '}
              ({state.readError.message})
            </p>
          )}
          <Status state={state} verdict={verdict} circumstances={circumstances} />
        </>
      )}
      {/* Outside the reading check: a call sent from another pool still holds
          the lock while this one is being read for the first time, and the
          buyer should be told why nothing is offered. */}
      <Actions state={state} actions={verdict?.actions ?? []} dispatch={dispatch} />
    </div>
  )
}

/**
 * The offer card on a trip's own page, with the seat price and the deadline
 * once something has said what they are: the chain's record, or a quote for
 * this trip. Never the offer's -- the offer is display text, and the price a
 * buyer pays is the agreement's.
 *
 * "Book by" only while the trip still takes seats: open, and short of its
 * deadline by the chain's clock. Past that it would be a date to act by that
 * has gone, or a trip that has filled. The seat price stays, since it is what
 * every seat paid in. Before the first read, a quote for this trip stands in,
 * as the quote is what a buyer is about to pay against.
 */
function TripOffer({ state }: { state: State }) {
  const { circumstances } = state
  const record = circumstances?.record
  const quote = state.quote?.agreementId === state.agreementId ? state.quote : undefined
  const takingSeats =
    circumstances === undefined
      ? quote !== undefined
      : record != null && record.state === 'OPEN' && circumstances.chainNow < record.deadline
  const deadline = record?.deadline ?? (quote === undefined ? undefined : BigInt(quote.deadline))
  return (
    <OfferCard
      price={record?.sharePrice ?? quote?.amount}
      deadline={takingSeats ? deadline : undefined}
    />
  )
}

function FirstRead({ state }: { state: State }) {
  const { pool } = useBrand().copy
  const id = String(state.agreementId)
  const { readError } = state
  if (readError?.kind === 'incompatible') {
    // Not "trying again": the refresh has stopped, because this failure is a
    // property of the build and the next tick would meet it again.
    return (
      <p role="status">
        {pool.firstReadIncompatible(id)} ({readError.message})
      </p>
    )
  }
  if (readError !== undefined) {
    // Nothing has been read yet, so the refresh is still running and will
    // ask again on its next tick.
    return (
      <p role="status">
        {pool.firstReadFailed(id)} ({readError.message})
      </p>
    )
  }
  return <p className="muted">{pool.firstReading(id)}</p>
}

interface ViewProps {
  state: State
  verdict: Verdict
  circumstances: Circumstances
}

/**
 * One view per situation, as a total map: a situation added to `view.ts`
 * fails to compile here until it has a screen of its own, rather than falling
 * into whichever view a tail branch happened to name.
 */
const VIEWS: Record<Situation, (props: ViewProps) => ReactElement> = {
  waiting: Waiting,
  'awaiting-delivery': AwaitingDelivery,
  released: Released,
  refundable: Refundable,
  refunding: Refunding,
  refunded: Refunded,
  gone: Gone,
}

function Status(props: ViewProps) {
  const View = VIEWS[props.verdict.situation]
  return (
    <div className="status">
      <View {...props} />
    </div>
  )
}

/**
 * A situation's heading, with the stamp beside it rather than inside it: the
 * heading's text stays the one line that names the screen, and the stamp is
 * the pool's outcome drawn the way the brand draws it.
 */
function Heading({
  verdict,
  lapsed = false,
  children,
}: {
  verdict: Verdict
  lapsed?: boolean
  children: ReactNode
}) {
  return (
    <div className="status-head">
      <h2>{children}</h2>
      <Stamp situation={verdict.situation} lapsed={lapsed} />
    </div>
  )
}

/**
 * Whether a held trip is past its deadline, by the chain's clock.
 *
 * Nothing on chain has changed yet: the agreement stays OPEN or FUNDED until
 * someone sends `expire`, and `view.ts` reads it that way. But the trip can no
 * longer fill or release, so the travel line says what is true of it now --
 * it has expired -- rather than "waiting". The chain's clock and never the
 * browser's, the same test `availableActions` makes before it offers the
 * expire call the view points at. The index shows these pools as it always
 * has.
 */
function isLapsed(circumstances: Circumstances): boolean {
  const { record, chainNow } = circumstances
  return (
    record !== null &&
    (record.state === 'OPEN' || record.state === 'FUNDED') &&
    chainNow >= record.deadline
  )
}

function Waiting(props: ViewProps) {
  const { state, verdict, circumstances } = props
  const { brand, copy } = useBrand()
  if (brand === 'travel' && isLapsed(circumstances)) return <Lapsed {...props} />
  const { record, chainNow } = circumstances
  return (
    <section>
      <Heading verdict={verdict}>{copy.pool.waiting.heading}</Heading>
      {record && (
        <>
          <p>{copy.pool.waiting.seats(record.seats, record.maxSeats)}</p>
          <Countdown deadline={record.deadline} now={chainNow} />
          <p>{copy.pool.waiting.unfilled}</p>
          <Commitment sha256={record.commitSha256} />
        </>
      )}
      <SeatLine state={state} verdict={verdict} />
    </section>
  )
}

function AwaitingDelivery(props: ViewProps) {
  const { state, verdict, circumstances } = props
  const { brand, copy } = useBrand()
  if (brand === 'travel' && isLapsed(circumstances)) return <Lapsed {...props} />
  const { record } = circumstances
  const { awaitingDelivery } = copy.pool
  return (
    <section>
      <Heading verdict={verdict}>{awaitingDelivery.heading}</Heading>
      {record && (
        <>
          <p>{awaitingDelivery.filled(record.seats, record.maxSeats)}</p>
          <p>{awaitingDelivery.unreleased(formatInstant(record.deadline))}</p>
          <Commitment sha256={record.commitSha256} />
        </>
      )}
      <SeatLine state={state} verdict={verdict} />
    </section>
  )
}

/**
 * A trip past its deadline that nobody has expired yet: OPEN with seats
 * unsold, or FUNDED with the trip file never released.
 *
 * Its money is still in the escrow app, and the `expire` call the verdict
 * offers is what starts the refunds. A viewer whose wallet cannot pay that
 * call's fee is offered no button, so they are told that anyone can send it
 * and that the refunds go to each wallet that paid either way.
 */
function Lapsed({ state, verdict, circumstances }: ViewProps) {
  const { copy } = useBrand()
  const { record } = circumstances
  if (copy.brand !== 'travel' || record === null) return <></>
  const filled = record.state === 'FUNDED' || record.state === 'FILLED'
  return (
    <section>
      <Heading verdict={verdict} lapsed>
        {copy.lapsed.heading(record.minSeats, filled)}
      </Heading>
      <p>{copy.lapsed.body}</p>
      {!circumstances.canPayFee && <p>{copy.lapsed.noFee}</p>}
      <p>{copy.pool.waiting.seats(record.seats, record.maxSeats)}</p>
      <p>{copy.pool.countdown.passed(formatInstant(record.deadline))}</p>
      <Commitment sha256={record.commitSha256} />
      <SeatLine state={state} verdict={verdict} />
    </section>
  )
}

function Released({ state, verdict, circumstances }: ViewProps) {
  const { released } = useBrand().copy.pool
  const { record } = circumstances
  if (verdict.closed || record === null) {
    return (
      <section>
        <Heading verdict={verdict}>{released.heading}</Heading>
        {/* Not "gone from the chain": the commitment is an argument of the
            call that created the pool and of the call that released it, and
            both stay in the chain's history. What `close` removed is the
            record this page reads it from. */}
        <p>{released.closed}</p>
        <EditionLinks circumstances={circumstances} against={released.againstClosed} />
      </section>
    )
  }
  return (
    <section>
      <Heading verdict={verdict}>{released.heading}</Heading>
      <p>{released.open}</p>
      <Commitment sha256={record.commitSha256} />
      <EditionLinks circumstances={circumstances} against={released.againstOpen} />
      <SeatLine state={state} verdict={verdict} />
    </section>
  )
}

/**
 * Where the released edition is, as the note on its release recorded it.
 *
 * Two things this never does. It never builds a link the note did not give:
 * an indexer that did not answer, or a history with no release note in it,
 * says the link cannot be read, and nothing is guessed from the CID's usual
 * shape or the edition's number. And it says nothing about how long the file
 * stays reachable, which is the pinning service's to decide and nothing on
 * chain records.
 *
 * The JSON sits beside the edition because it is the file the pool committed
 * to. A gateway is somebody else's server, so the link is where to look, and
 * hashing that file is how a buyer knows what they were given.
 */
function EditionLinks({
  circumstances,
  against,
}: {
  circumstances: Circumstances
  /** What the JSON's sha256 is to be compared with, as this screen can name it. */
  against: string
}) {
  const { copy } = useBrand()
  const { released } = copy.pool
  if (!circumstances.edition) {
    return <p role="status">{released.noLink}</p>
  }
  const { html, json } = editionFiles(
    circumstances.edition,
    getConfig().ipfsGateway,
    copy.fileStem,
  )
  // A fragment, not a wrapper: the section spaces its direct children.
  return (
    <>
      <p>
        {released.lead}
        <ExternalLink href={html.url}>{html.name}</ExternalLink>
      </p>
      <p>
        {released.check.before}
        <ExternalLink href={json.url}>{json.name}</ExternalLink>
        {released.check.after(against)}
      </p>
    </>
  )
}

function ExternalLink({ href, children }: { href: string; children: string }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  )
}

function Refundable({ state, verdict, circumstances }: ViewProps) {
  const { refundable } = useBrand().copy.pool
  return (
    <section>
      <Heading verdict={verdict}>{refundable.heading}</Heading>
      <p>{refundable.missed}</p>
      {!awaitingPass(verdict) && <p>{refundable.notStarted}</p>}
      <SeatLine state={state} verdict={verdict} />
      <PassNotFinished state={state} verdict={verdict} circumstances={circumstances} />
      <PauseProof />
    </section>
  )
}

function Refunding({ state, verdict, circumstances }: ViewProps) {
  const { refunding } = useBrand().copy.pool
  const { record } = circumstances
  return (
    <section>
      <Heading verdict={verdict}>{refunding.heading}</Heading>
      <p>{refunding.missed}</p>
      {record && !awaitingPass(verdict) && (
        <p>{refunding.passAt(record.refundCursor, record.seats)}</p>
      )}
      <SeatLine state={state} verdict={verdict} />
      <PassNotFinished state={state} verdict={verdict} circumstances={circumstances} />
      <PauseProof />
    </section>
  )
}

/**
 * What is said about getting the money back while the refund pass has still
 * to start or is still running.
 *
 * A running pass is not one situation for every seat. Ahead of the cursor the
 * pass will pay a seat on its own, and `NoFee` may say the refund is on its
 * way; behind the cursor it has already skipped the seat and will not come
 * back, so that sentence would have a buyer wait for money that only a claim
 * can release.
 */
function PassNotFinished({ state, verdict, circumstances }: ViewProps) {
  if (verdict.skipped && verdict.seat !== null) {
    return <SkippedSeat amount={verdict.seat.amount} canPayFee={circumstances.canPayFee} />
  }
  return <NoFee state={state} circumstances={circumstances} />
}

function Refunded({ state, verdict, circumstances }: ViewProps) {
  const { refunded } = useBrand().copy.pool
  if (verdict.closed) {
    return (
      <section>
        <Heading verdict={verdict}>{refunded.closedHeading}</Heading>
        <p>{refunded.closed}</p>
      </section>
    )
  }
  return (
    <section>
      <Heading verdict={verdict}>{refunded.heading}</Heading>
      <p>{refunded.passDone}</p>
      <SeatLine state={state} verdict={verdict} />
      {/* A finished pass is not everyone paid: a seat the pass could not pay
          is still owed, and says so. */}
      {verdict.skipped && verdict.seat !== null && (
        <SkippedSeat amount={verdict.seat.amount} canPayFee={circumstances.canPayFee} />
      )}
      <PauseProof />
    </section>
  )
}

/**
 * The boxes are gone and the history did not say how the pool ended.
 *
 * Not an error, and not a guess. A closed pool was either delivered or
 * refunded, which are opposite news, and naming either without the history's
 * word for it would tell some buyers the wrong one.
 */
function Gone({ state, verdict }: ViewProps) {
  const { gone } = useBrand().copy.pool
  return (
    <section>
      <Heading verdict={verdict}>{gone.heading}</Heading>
      <p>{gone.body(String(state.agreementId))}</p>
      <p>{gone.unknown}</p>
    </section>
  )
}

/**
 * Time left, labelled as the estimate it is.
 *
 * Only ever text. The contract compares the deadline against the previous
 * block's timestamp, so any clock this page holds runs ahead of it; which
 * calls are possible is the verdict's to say, and it reads chain time.
 */
function Countdown({ deadline, now }: { deadline: bigint; now: bigint }) {
  const { countdown } = useBrand().copy.pool
  const remaining = formatRemaining(deadline, now)
  const when = formatInstant(deadline)
  return (
    <p>
      {remaining === null ? countdown.passed(when) : countdown.left(remaining, when)}{' '}
      {countdown.estimate}
    </p>
  )
}

function Commitment({ sha256 }: { sha256: string }) {
  const { commitment } = useBrand().copy.pool
  return (
    <p className="commitment">
      {commitment}
      <Hex>{sha256}</Hex>
    </p>
  )
}

/**
 * Whether this wallet holds a seat here.
 *
 * `isConfirmingSeat` first, and it is the reason this is a component at all.
 * Right after a purchase the node lags the settlement, so the roster read can
 * show no seat for a buyer who has just paid. Telling them they hold none is a
 * lie told to someone who has just spent money.
 */
function SeatLine({ state, verdict }: { state: State; verdict: Verdict }) {
  const { seat: words } = useBrand().copy.pool
  if (isConfirmingSeat(state)) {
    return (
      // The payment itself, and "do not pay again", are said once, above, by
      // the purchase panel: that notice follows the payment to any screen,
      // where this line belongs to the pool's reading.
      <p role="status">{words.confirming}</p>
    )
  }
  if (state.wallet === undefined) {
    return <p>{words.noWallet}</p>
  }
  const { seat, situation } = verdict
  if (seat === null) return <p>{words.none}</p>

  // On a pool that missed, "holds a seat" is not the answer a buyer came for:
  // the question is whether this seat's money came back. The roster says, and
  // the two statuses are opposite news -- `settled` is a seat the refund paths
  // have already paid, `owed` one they have not.
  const record = state.circumstances?.record ?? null
  const missed = situation === 'refundable' || situation === 'refunding' || situation === 'refunded'
  if (record !== null && missed && seat.status === 'settled') {
    // From the record, not the roster: a settled seat's amount is zeroed when
    // it is paid, and `sharePrice` is what every seat paid in.
    return <p>{words.refunded(formatUsdc(record.sharePrice))}</p>
  }
  // A seat the pass has gone past is a skipped one, on a running pass as on a
  // finished one, and `SkippedSeat` says so; here the pass has still to reach
  // this seat.
  if (record !== null && awaitingPass(verdict)) {
    return <p>{words.toRefund(record.refundCursor, record.seats)}</p>
  }
  return <p>{words.holds}</p>
}

/**
 * An owed seat the refund pass has still to reach: on a pool whose pass has
 * not started, or ahead of the cursor on one that is running -- the one case
 * where `SeatLine` states the pass's position itself, so the view around it
 * leaves that sentence out rather than saying it twice.
 *
 * Not every owed seat on a running pass. One behind the cursor was skipped,
 * and the pass is not still to reach it: it has been and gone.
 */
function awaitingPass(verdict: Verdict): boolean {
  return (
    verdict.owed &&
    !verdict.skipped &&
    (verdict.situation === 'refundable' || verdict.situation === 'refunding')
  )
}

/**
 * A seat the refund pass has gone past without paying, whether the pass has
 * finished or is still running further down the roster.
 *
 * The pass skips a payer who cannot receive USDC rather than stopping behind
 * them, so the one thing that strands a seat is the buyer's own opt-out -- and
 * the one thing that frees it is opting back in.
 *
 * **Not `NoFee`, when the wallet cannot pay a fee.** That notice says the
 * refund is on its way and the buyer need not act, which is true of a seat the
 * pass has still to reach and false here: the pass has been past this seat and
 * does not come back, nothing is on its way, and opting back in is something
 * only the seat's own wallet can do.
 * A buyer told they need not act would wait for money that is not coming. So
 * the no-fee case says the one thing that stays true -- once this address can
 * receive USDC again, the claim can be sent by anyone and still pays this
 * address.
 */
function SkippedSeat({ amount, canPayFee }: { amount: bigint; canPayFee: boolean }) {
  const { stayOptedIn } = useBrand().copy.pool
  return (
    <div role="status">
      <p>
        Your seat is still owed {formatUsdc(amount)}. The refund pass has gone past it without
        paying it, and does not come back to it. The pass skips a seat whose address cannot
        receive USDC: one that has opted out of USDC, or is frozen.
      </p>
      <p>
        The fix is to opt this wallet back in to USDC. After that, a refund claim pays the seat.
      </p>
      {!canPayFee && (
        <p>
          This wallet cannot pay a network fee right now, so no claim is offered here. Once this
          address can receive USDC again, anyone can send the claim, and the money is paid to
          this address, never to whoever pays the fee.
        </p>
      )}
      <p>{stayOptedIn}</p>
    </div>
  )
}

/**
 * Why no call is offered, when it is because the wallet cannot pay a fee --
 * on a pool whose refund pass has still to reach this wallet's seat, or which
 * this wallet holds no seat in. A seat the pass has gone past says something
 * else; see `SkippedSeat`.
 *
 * Never "you must act". The refund calls pay the roster address and not the
 * sender, so the buyer's money reaches them whoever sends the call -- and a
 * buyer with no spendable ALGO is not at a dead end, only not the one who
 * sends it.
 */
function NoFee({ state, circumstances }: { state: State; circumstances: Circumstances }) {
  if (circumstances.canPayFee) return null
  if (state.wallet === undefined) {
    return (
      <p>
        The refunds are on their way, and anyone can send the calls that pay them. The money is
        always paid to each seat's address, never to whoever pays the fee.
      </p>
    )
  }
  return (
    <p>
      This wallet cannot pay a network fee right now, so no call is offered here. You do not need
      to act: the refund is on its way, and anyone can send the call that pays it. The money is
      always paid to the seat's address, never to whoever pays the fee.
    </p>
  )
}

/** The safety claim, stated where it applies. */
function PauseProof() {
  const { pauseProof } = useBrand().copy.pool
  return <p>{pauseProof}</p>
}
