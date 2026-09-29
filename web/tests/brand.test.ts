import { describe, expect, it } from 'vitest'
import { COPY } from '../src/brand'

/**
 * Every string the travel table can produce, whether or not a screen test
 * happens to render it: each entry, and each function called both with a
 * stand-in for every argument and with every argument left undefined, so both
 * sides of each "when the offer names it" branch are read.
 */
function travelStrings(): string[] {
  // A value that reads as an offer, a count and a word at once: every travel
  // function takes some mix of the three.
  const anything = Object.assign(Object.create({ toString: () => '7', valueOf: () => 7 }), {
    v: 1,
    title: 'Title',
    city: 'City',
    iata: 'CTY',
    origin: 'Origin',
    originIata: 'ORG',
    depart: '2026-12-12',
    return: '2026-12-19',
    nights: 7,
    travellers: 3,
    fare: { amount: '1.00', currency: 'USD', basis: 'basis' },
    source: 'Source.',
    fetchedAt: '2026-09-28T00:00:00Z',
  })
  const found: string[] = []
  const walk = (value: unknown): void => {
    if (typeof value === 'string') found.push(value)
    else if (typeof value === 'function') {
      const arity = Math.max(value.length, 1)
      // Every argument set, none set, and only the first: a trailing flag or
      // name left unset is the other side of its branch.
      for (const set of [arity, 0, 1]) {
        try {
          walk(value(...Array.from({ length: arity }, (_, i) => (i < set ? anything : undefined))))
        } catch {
          // A function that needs the offer itself, which the first call read.
        }
      }
    } else if (Array.isArray(value)) value.forEach(walk)
    else if (typeof value === 'object' && value !== null) Object.values(value).forEach(walk)
  }
  walk(COPY.travel)
  return found
}

describe('the travel copy', () => {
  const strings = travelStrings()

  it('is read in full by the walk', () => {
    // A guard on the walk itself: a table it could not see into would pass the
    // test below with nothing checked.
    expect(strings.length).toBeGreaterThan(100)
    expect(strings).toContain('Waiting for travellers')
    expect(strings).toContain('Expired: fewer than 7 travellers joined in time')
  })

  it("never says the index's nouns, the Route Index, or a booking's words", () => {
    const offending = strings.filter((line) =>
      /edition|Route Index|booking|voucher|redeem|pool|ticket|PNR|discount/i.test(line),
    )
    expect(offending).toEqual([])
  })

  it('names no data source of its own: only the offer can', () => {
    const provider = COPY.travel.demoNotice.body('Source.', undefined)
    expect(provider).not.toMatch(/not a .* product/)
    expect(COPY.travel.offerCard.caption(undefined)).toBe('Fare data: see the demo notice')
  })
})

describe('the index copy', () => {
  it('still names what the index sells', () => {
    expect(COPY.index.terms.items(undefined)[0]).toContain('Algorand x402 Route Index')
    expect(COPY.index.nouns).toEqual({ pool: 'pool', product: 'edition', file: 'edition' })
    expect(COPY.index.fileStem).toBe('edition')
  })
})
