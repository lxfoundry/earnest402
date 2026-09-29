import { getConfig } from '../config'
import { useBrand } from './BrandContext'

/**
 * How to get what a seat on a TestNet trip is paid with, for a buyer who has
 * never used TestNet.
 *
 * Travel only, and mounted in the shell rather than on one screen: a buyer can
 * arrive on a trip's own page from a shared link. The asset id is this
 * build's, so the step that adds USDC names the asset the escrow app actually
 * takes.
 */
export function TestnetFunds() {
  const { brand, copy } = useBrand()
  if (brand !== 'travel') return null
  const funds = copy.testnetFunds
  return (
    // Named by its heading, so it is a region of its own: it sits in the shell,
    // outside `main`. One per page, so a fixed id is safe.
    <section className="testnet-funds" aria-labelledby="testnet-funds-heading">
      <h2 id="testnet-funds-heading">{funds.heading}</h2>
      <ol aria-label={funds.listLabel}>
        {funds.steps(String(getConfig().assetId)).map((step) => (
          <li key={step.before + (step.link?.href ?? '')}>
            {step.before}
            {step.link && (
              <a href={step.link.href} target="_blank" rel="noopener noreferrer">
                {step.link.text}
              </a>
            )}
            {step.after}
          </li>
        ))}
      </ol>
    </section>
  )
}
