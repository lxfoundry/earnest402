import type { Situation } from '../pool/view'
import { useBrand } from './BrandContext'

/**
 * One word per situation, so a stamp can say only what is true of every pool
 * in that situation.
 *
 * That is why the index's three refund situations share "Missed" rather than
 * counting the pass down to "Refunded". A finished pass is not everyone paid
 * -- a seat the pass skipped is still owed -- and a stamp reading "refunded"
 * beside "your seat is still owed" would be opposite news. How far the refunds
 * have got is the heading's to say, and the seat line's. A trip says
 * "Expired" for all three, for the same reason.
 *
 * A total map, as the views are: a situation added to `view.ts` fails to
 * compile in `brand.ts` until it has a stamp or an explicit `null`. `gone` is
 * the `null`, because a closed pool whose history did not say how it ended
 * was either released or refunded, and either stamp would be a guess.
 *
 * `lapsed` is a trip past its deadline that nobody has expired yet: still
 * `waiting` or `awaiting-delivery` on chain, and already a trip that will be
 * refunded. The index draws it as it always has.
 */
export function Stamp({ situation, lapsed = false }: { situation: Situation; lapsed?: boolean }) {
  const { copy } = useBrand()
  const stamp = lapsed && copy.brand === 'travel' ? copy.lapsed.stamp : copy.stamps[situation]
  if (stamp === null) return null
  return (
    <span className="stamp" data-outcome={stamp.outcome}>
      {stamp.word}
    </span>
  )
}
