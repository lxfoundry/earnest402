import { createContext, useContext } from 'react'
import { COPY, type IndexCopy, type TravelCopy } from '../brand'
import type { Brand, Offer } from '../config'

/**
 * The build's brand, as the screens read it.
 *
 * Provided once, by `App`, from `getBrand()`. The default is the index with no
 * offer, so a screen rendered on its own -- as most screen tests render them --
 * shows the index exactly as it did before there was a second brand.
 */
export const BrandContext = createContext<Brand>({ brand: 'index' })

export type BrandView =
  | { brand: 'index'; offer: undefined; copy: IndexCopy }
  | { brand: 'travel'; offer: Offer | undefined; copy: TravelCopy }

/** The brand, its offer, and its words, narrowed together on `brand`. */
export function useBrand(): BrandView {
  const { brand, offer } = useContext(BrandContext)
  return brand === 'travel'
    ? { brand, offer, copy: COPY.travel }
    : { brand, offer: undefined, copy: COPY.index }
}
