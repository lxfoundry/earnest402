import { useBrand } from './BrandContext'
import { formatInstant, formatUsdc } from './format'

/**
 * The trip on offer, as a travel page would show it.
 *
 * Everything but the last two lines is the offer's: display text the build
 * was given, never read by anything that moves money. The seat price and the
 * deadline are the agreement's, so they are shown only once something that
 * knows them -- a quote, or the chain's record -- has been read, and never
 * guessed from the offer.
 *
 * Rendered only by a travel build with an offer. The demo notice follows it
 * directly, as its own band: the card reads as the offer, and the notice says
 * what the offer is.
 */
export function OfferCard({ price, deadline }: { price?: bigint; deadline?: bigint }) {
  const { brand, copy, offer } = useBrand()
  if (brand !== 'travel' || offer === undefined) return null
  const card = copy.offerCard
  return (
    <section className="offer-card" aria-label={card.label}>
      <p className="offer-route">{card.route(offer)}</p>
      <p className="offer-fare">{card.fare(offer)}</p>
      <p className="offer-caption">{card.caption(offer.provider)}</p>
      {(price !== undefined || deadline !== undefined) && (
        <ul className="offer-terms">
          {price !== undefined && <li>{card.seat(formatUsdc(price))}</li>}
          {deadline !== undefined && <li>{card.bookBy(formatInstant(deadline))}</li>}
        </ul>
      )}
    </section>
  )
}
