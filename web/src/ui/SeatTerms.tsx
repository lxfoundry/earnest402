import { useBrand } from './BrandContext'

/**
 * What a seat buys, in the paid route's own words.
 *
 * Not new copy. The route's discovery description is the reviewed public
 * statement of this product, so the screen restates its substance rather than
 * paraphrasing it into claims nobody reviewed -- and a buyer who reads the
 * listing and then the screen reads the same promise twice.
 *
 * It is also where "what was promised" is defined: the sha256 committed before
 * the buyer paid. Any view that shows the product line has to show this with
 * it.
 *
 * A trip states the same terms as numbered steps, with its seat count: the
 * quote's, which is what a buyer pays against. Never the offer's travellers,
 * which describe the sample trip and not the pool on sale. Until a quote has
 * been read there is no count, and the steps say "enough".
 */
export function SeatTerms({ seats }: { seats: number | undefined }) {
  const { copy } = useBrand()
  const { heading, ordered, items } = copy.terms
  const lines = items(seats)
  return (
    <section className="terms">
      <h2>{heading}</h2>
      {ordered ? (
        <ol>
          {lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ol>
      ) : (
        lines.map((line) => <p key={line}>{line}</p>)
      )}
    </section>
  )
}
