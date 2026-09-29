import type { PoolMachine } from '../pool/usePoolMachine'
import { poolPath, type PurchaseError, type State } from '../pool/machine'
import type { SeatQuote } from '../pool/view'
import { useBrand } from './BrandContext'
import { formatInstant, formatUsdc } from './format'
import { Hex, Link } from './Link'
import { SeatTerms } from './SeatTerms'

/**
 * The purchase, wherever it has got to.
 *
 * Rendered on both screens because a purchase is not a property of a screen:
 * a quote moves the address bar to its pool, and a buyer can press back in the
 * middle of one. What differs is where a new quote may be asked for -- only on
 * the entry screen, which is the page that quotes the open edition. The pool
 * screen is about one pool, and a button there quoting whichever edition is
 * open would sit beside "confirming your seat" inviting a second purchase.
 * The confirm view is the exception, on either screen: a quote is already
 * there, and replacing it invites no purchase the buyer had not started.
 */
export interface PurchaseProps extends PoolMachine {
  where: 'entry' | 'pool'
}

export function Purchase(props: PurchaseProps) {
  return (
    <>
      <AwaitingSeat state={props.state} />
      <PurchaseStep {...props} />
    </>
  )
}

function PurchaseStep({ state, dispatch, where }: PurchaseProps) {
  const requestQuote = () => dispatch({ type: 'QUOTE_REQUESTED' })
  const { purchase } = useBrand().copy

  switch (state.purchase) {
    case 'IDLE':
      return (
        <section>
          <PurchaseOutcome error={state.purchaseError} />
          {/* Offered only in the states the reducer's quote allowlist admits:
              `IDLE` here, and `UNAVAILABLE` and `CONFIRM` below. A button
              anywhere else would be a click the reducer ignores. */}
          {where === 'entry' && (
            <button type="button" className="primary" onClick={requestQuote}>
              {purchase.quote}
            </button>
          )}
        </section>
      )

    case 'QUOTING':
      return (
        <section>
          <p className="muted">{purchase.quoting}</p>
        </section>
      )

    case 'UNAVAILABLE':
      // An answer about the moment the entry screen asked. On a pool's own
      // page it would read as news about that pool, which it is not.
      if (where !== 'entry') return null
      return (
        <section>
          <Unavailable reason={state.unavailable?.reason} message={state.unavailable?.message} />
          <button type="button" onClick={requestQuote}>
            {purchase.checkAgain}
          </button>
        </section>
      )

    case 'CONFIRM':
      // Holds a quote by construction; checked so a state that broke that
      // rule renders nothing rather than a price of `undefined`.
      if (!state.quote) return null
      return (
        <Confirm quote={state.quote} state={state} where={where} dispatch={dispatch} />
      )

    case 'CHECKING':
      return (
        <section>
          <p className="muted">{purchase.checking}</p>
        </section>
      )

    case 'SETTLING':
      return (
        <section>
          <p className="muted">{purchase.settling}</p>
        </section>
      )

    default:
      return exhaustive(state.purchase)
  }
}

function exhaustive(_purchase: never): null {
  return null
}

/**
 * Why the route declined to quote. The two known reasons are opposite news,
 * and only one of them is a failure.
 */
function Unavailable({ reason, message }: { reason?: string; message?: string }) {
  const { purchase } = useBrand().copy
  if (reason === 'no_pool_open') {
    // The chain was read and no edition is open. That is an answer to the
    // buyer's question, so it is worded as one.
    return <p>{purchase.noneOpen}</p>
  }
  if (reason === 'pool_status_unavailable') {
    return <p>{purchase.chainUnreachable}</p>
  }
  return (
    <>
      <p>{purchase.notQuoting}</p>
      {message && <p>{message}</p>}
    </>
  )
}

/**
 * What the buyer is about to pay for, read back from the quote before anything
 * is signed.
 *
 * A fresh quote can be asked for here, and only by asking: the price never
 * changes under a buyer who is just reading it. Without the control, a buyer
 * with no wallet to buy with could leave this view only by connecting one, and
 * this quote, however stale, followed them to every screen. The pooled 402
 * parks nothing, so asking again is free. Buying re-checks the quote against
 * the chain first, and a stale one is dropped, unsigned, with the reasons it
 * went stale.
 */
function Confirm({
  quote,
  state,
  where,
  dispatch,
}: {
  quote: SeatQuote
  state: State
  where: PurchaseProps['where']
  dispatch: PoolMachine['dispatch']
}) {
  const price = formatUsdc(quote.amount)
  const { confirm } = useBrand().copy.purchase
  return (
    <section className="receipt">
      <h2>{confirm.heading}</h2>
      <dl>
        {/* Named, because the quote follows whichever edition is open and the
            buyer may be reading it on another pool's page. */}
        <dt>{confirm.pool}</dt>
        <dd>{String(quote.agreementId)}</dd>
        <dt>{confirm.price}</dt>
        <dd>{price}</dd>
        <dt>{confirm.seatsTaken}</dt>
        <dd>
          {quote.seatsTotal - quote.seatsLeft} of {quote.seatsTotal}
        </dd>
        <dt>{confirm.deadline}</dt>
        <dd>{formatInstant(quote.deadline)}</dd>
        <dt>{confirm.commitSha256}</dt>
        <dd>
          <Hex>{quote.commitSha256}</Hex>
        </dd>
      </dl>
      <p>{confirm.committed}</p>
      {/* The entry screen shows the terms beside its own heading already. */}
      {where === 'pool' && <SeatTerms seats={quote.seatsTotal} />}
      {/* Here, a freshness check that could not be made -- which is not a
          stale quote, and has its own kind so it cannot read as one. */}
      <PurchaseOutcome error={state.purchaseError} />
      {state.wallet ? (
        <button
          type="button"
          className="primary"
          onClick={() => dispatch({ type: 'BUY_REQUESTED' })}
        >
          {confirm.buy(price)}
        </button>
      ) : (
        // Not a disabled buy button: the reducer ignores a purchase with no
        // wallet to sign it, and the connect buttons are in the header.
        <p>{confirm.connect}</p>
      )}
      <p className="muted">{confirm.recheck}</p>
      <button type="button" onClick={() => dispatch({ type: 'QUOTE_REQUESTED' })}>
        {confirm.fresh}
      </button>
    </section>
  )
}

/**
 * What the last purchase attempt left behind, headed by where it came from.
 *
 * A total map over the reducer's kinds, in `brand.ts`, so the heading is read
 * off the state rather than inferred from it. The two headings that must never
 * be swapped -- a purchase that did not complete, and a payment that went
 * through -- are different kinds, so no other field has to stand in for the
 * difference, and a kind added later fails to compile there until it has a
 * heading of its own.
 */
function PurchaseOutcome({ error }: { error: PurchaseError | undefined }) {
  const { outcome } = useBrand().copy.purchase
  if (error === undefined) return null
  return (
    <div role="status">
      <p>{outcome[error.kind]}</p>
      <p>{error.message}</p>
    </div>
  )
}

/**
 * A payment that went through and whose seat has not been read yet.
 *
 * Its own line, apart from the last attempt's outcome, because they are
 * separate facts that can both be true: a buyer whose first payment is still
 * confirming can try again, be refused, and be owed both "that attempt did not
 * complete" and "your earlier payment went through". Folding them into one
 * heading drops one of the two, and either one dropped misleads.
 */
function AwaitingSeat({ state }: { state: State }) {
  const { awaitingSeat } = useBrand().copy.purchase
  if (state.awaitingSeat === undefined) return null
  const paid = state.awaitingSeat
  return (
    <p role="status">
      {awaitingSeat.before}
      <Link to={poolPath(paid)}>{awaitingSeat.link(String(paid))}</Link>
      {awaitingSeat.after}
    </p>
  )
}
