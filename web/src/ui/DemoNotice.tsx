import { useBrand } from './BrandContext'

/**
 * What the travel page is, said beside the offer rather than inside it.
 *
 * Its own band, directly under the offer card and before any pay button, and
 * never collapsible: a buyer cannot reach the button without passing it, and
 * cannot fold it away. The offer's own sentences stay free of it, so the
 * offer reads as an offer and the notice reads as the notice.
 *
 * The source line is the offer's. With no offer, a neutral one stands in, and
 * no data source is named; with an offer that names its provider, the notice
 * also says this page is not that provider's product.
 */
export function DemoNotice() {
  const { brand, copy, offer } = useBrand()
  if (brand !== 'travel') return null
  const notice = copy.demoNotice
  return (
    <aside className="demo-notice" aria-label={notice.label}>
      <p>
        <strong>{notice.label}.</strong>{' '}
        {notice.body(offer?.source ?? notice.neutralSource, offer?.provider)}
      </p>
    </aside>
  )
}
