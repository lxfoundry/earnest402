import { poolPath } from '../pool/machine'
import type { PoolMachine } from '../pool/usePoolMachine'
import { useBrand } from './BrandContext'
import { DemoNotice } from './DemoNotice'
import { formatDay } from './format'
import { Link } from './Link'
import { OfferCard } from './OfferCard'
import { Purchase } from './Purchase'
import { SeatTerms } from './SeatTerms'

/**
 * `/app`: the product, the open edition's quote, and the way back to a seat
 * for a buyer who has lost their link.
 *
 * The quote is asked for with a button rather than on arrival. A quote that
 * arrives always moves the address bar to its pool, so quoting on arrival
 * would carry every visitor to `/app/pool/<id>` before they had read what a
 * seat buys. It would also clear whatever the last purchase left on screen,
 * and carry a buyer who has just paid straight to a confirm view for the pool
 * they are already in.
 */
export function EntryScreen({ state, dispatch }: PoolMachine) {
  const { brand } = useBrand()
  if (brand === 'travel') return <TravelEntry state={state} dispatch={dispatch} />
  return <IndexEntry state={state} dispatch={dispatch} />
}

function IndexEntry({ state, dispatch }: PoolMachine) {
  const { copy } = useBrand()
  if (copy.brand !== 'index') return null
  const { hero } = copy
  return (
    <>
      {/* The product line, verbatim, split between the headline and the lede
          at its own sentence break -- set in two sizes, never reworded. The
          name is in the header's logo. "What was promised" in it is defined
          by the terms below: the sha256 committed before the buyer paid.

          Under it, the pool line: the one sentence that says what a threshold
          is, for the one product this host sells. It is subordinate to the
          product line and never appears above it, it never replaces it, and
          it carries no seat count -- the count belongs to a quote, and this
          view renders before one arrives. The mechanics it compresses are
          stated in reviewed words by <SeatTerms /> directly below, which is
          why the compression is allowed to be this short. */}
      <section className="hero">
        <h1>
          {hero.headline}
          <br />
          {hero.x402}
          <span className="coin">.</span>
        </h1>
        <p className="lede">{hero.lede}</p>
        <p className="pool-line">{hero.poolLine}</p>
      </section>
      <div className="entry-grid">
        <SeatTerms seats={state.quote?.seatsTotal} />
        <div className="entry-side">
          <div className="panel">
            <Purchase state={state} dispatch={dispatch} where="entry" />
          </div>
          {state.wallet !== undefined && <MySeats state={state} dispatch={dispatch} />}
        </div>
      </div>
    </>
  )
}

/**
 * `/app` on the travel line: the trip as an offer, with the product line over
 * it and the demo notice under it.
 *
 * The product line comes first, whole and verbatim, as a strip under the
 * header, and the terms that define its "what was promised" are on the same
 * screen. The hero is the offer's; the card under it carries the route and
 * the reference fare, and the demo notice follows the card directly, before
 * the pay button, as its own band.
 *
 * The seat price, the deadline and the seat count are the quote's, so the
 * card, the hook and the terms state them only once a quote is on hand.
 */
function TravelEntry({ state, dispatch }: PoolMachine) {
  const { copy, offer } = useBrand()
  if (copy.brand !== 'travel') return null
  const quote = state.quote
  return (
    <>
      <p className="tagline">{copy.tagline}</p>
      <section className="travel-hero">
        <h1>{offer?.title ?? copy.hero.untitled}</h1>
        {offer && (
          <p className="travel-sub">
            {copy.hero.sub(offer, formatDay(offer.depart), formatDay(offer.return))}
          </p>
        )}
        <p className="travel-hook">{copy.hero.hook(quote?.seatsTotal)}</p>
      </section>
      <div className="entry-grid">
        <div className="entry-main">
          <OfferCard
            price={quote?.amount}
            deadline={quote === undefined ? undefined : BigInt(quote.deadline)}
          />
          <DemoNotice />
          <SeatTerms seats={quote?.seatsTotal} />
        </div>
        <div className="entry-side">
          <div className="panel">
            <Purchase state={state} dispatch={dispatch} where="entry" />
          </div>
          {state.wallet !== undefined && <MySeats state={state} dispatch={dispatch} />}
        </div>
      </div>
    </>
  )
}

/**
 * The fallback for a buyer who arrived without their link.
 *
 * Offered only with a wallet connected, because the reducer ignores the
 * request without one: the search is by the connected address.
 */
function MySeats({ state, dispatch }: PoolMachine) {
  const { mySeats } = useBrand().copy
  return (
    <section className="my-seats">
      <h2>{mySeats.heading}</h2>
      <button
        type="button"
        disabled={state.findingMyPools}
        onClick={() => dispatch({ type: 'MY_POOLS_REQUESTED' })}
      >
        {mySeats.find}
      </button>
      {state.findingMyPools && <p>{mySeats.searching}</p>}
      {state.myPoolsError !== undefined && (
        // A public indexer is a third-party host. Its being down costs this
        // search and nothing else, and the link from the purchase still
        // reaches the pool, so that is where the buyer is sent.
        <p>{mySeats.unavailable}</p>
      )}
      {state.myPools !== undefined &&
        (state.myPools.length === 0 ? (
          <p>{mySeats.none}</p>
        ) : (
          <ul>
            {state.myPools.map(({ agreementId }) => (
              <li key={String(agreementId)}>
                <Link to={poolPath(agreementId)}>{mySeats.item(String(agreementId))}</Link>
              </li>
            ))}
          </ul>
        ))}
    </section>
  )
}
