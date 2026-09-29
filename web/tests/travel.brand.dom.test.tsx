// @vitest-environment jsdom
//
// The travel line: the same screens, the same machine and the same rules as
// the index, presented as a sample group trip. Rendered against hand-built
// states, as `pool.screens.dom.test.tsx` renders the index, inside the brand
// context `App` provides.
//
// Three things are pinned here beyond the words themselves. The demo notice is
// on every travel page that offers a seat, as its own band after the offer and
// before any pay button. A trip past its deadline reads as expired by the
// chain's clock, whether it was still selling seats or already full. And no
// travel page says "pool", "edition", or any word the travel copy was written
// to avoid.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, within } from '@testing-library/react'
import type { ReactElement } from 'react'
import type { Agreement, Seat } from '../src/chain/decode'
import { COPY } from '../src/brand'
import { resetBrandForTests, resetConfigForTests, type Brand, type Offer } from '../src/config'
import { initialState, reduce, type State } from '../src/pool/machine'
import { quoteDisagreements, type Circumstances, type SeatQuote } from '../src/pool/view'
import { BrandContext } from '../src/ui/BrandContext'
import { EntryScreen } from '../src/ui/EntryScreen'
import { PoolScreen } from '../src/ui/PoolScreen'
import { TestnetFunds } from '../src/ui/TestnetFunds'

const runMock = vi.hoisted(() => vi.fn())
const useWalletMock = vi.hoisted(() => vi.fn())

// Only `App` reaches these: the effect executor and the wallet, faked as
// `app.nav.dom.test.tsx` fakes them.
vi.mock('../src/pool/runner', () => ({ run: runMock }))
vi.mock('@txnlab/use-wallet-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@txnlab/use-wallet-react')>()
  return { ...actual, useWallet: useWalletMock }
})

/**
 * What the travel copy never says, anywhere a buyer can read it: the index's
 * nouns, the Route Index, and the words of a booking.
 */
const FORBIDDEN = /edition|Route Index|booking|voucher|redeem|pool/i

/** The synthetic offer, encoded at run time where a test needs it encoded. */
const OFFER: Offer = {
  v: 1,
  title: 'Example City beach week',
  city: 'Example City',
  iata: 'EXC',
  origin: 'Sample Town',
  originIata: 'SMP',
  depart: '2026-12-12',
  return: '2026-12-19',
  nights: 7,
  travellers: 3,
  fare: { amount: '612.40', currency: 'USD', basis: 'per traveller, round trip' },
  source: 'Example travel API, test environment. Indicative, not bookable.',
  fetchedAt: '2026-09-28T14:03:00Z',
}

const TAGLINE =
  '"If this, then pay" for x402. Earnest holds the payment on Algorand until the condition is ' +
  'met: enough buyers joined to fund a purchase together, the deliverable matches what was ' +
  'promised, or both.'

const WALLET = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'
const TRIP = 25n
const OTHER_TRIP = 26n
const DEADLINE = 1789651281n
const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'
const GATEWAY = 'https://ipfs.example'

function agreement(overrides: Partial<Agreement> = {}): Agreement {
  return {
    condition: 0,
    state: 'OPEN',
    deadline: DEADLINE,
    sharePrice: 5_000_000n,
    minSeats: 3,
    maxSeats: 3,
    seats: 2,
    refundCursor: 0,
    unclaimedSeats: 0,
    totalHeld: 10_000_000n,
    commitSha256: 'fa'.repeat(32),
    beneficiary: 'B'.repeat(58),
    verifier: 'V'.repeat(58),
    creator: 'C'.repeat(58),
    ...overrides,
  }
}

const seat = (overrides: Partial<Seat> = {}): Seat => ({
  index: 0,
  payer: WALLET,
  amount: 5_000_000n,
  status: 'owed',
  ...overrides,
})

const circumstances = (overrides: Partial<Circumstances> = {}): Circumstances => ({
  record: agreement(),
  seat: null,
  chainNow: DEADLINE - 7_200n,
  canPayFee: true,
  ...overrides,
})

const CHAIN = {
  waiting: circumstances({ seat: seat() }),
  lapsedOpen: circumstances({ seat: seat(), chainNow: DEADLINE }),
  filled: circumstances({ record: agreement({ state: 'FUNDED', seats: 3 }), seat: seat() }),
  lapsedFunded: circumstances({
    record: agreement({ state: 'FUNDED', seats: 3 }),
    seat: seat(),
    chainNow: DEADLINE + 60n,
  }),
  released: circumstances({
    record: agreement({ state: 'RELEASED', seats: 3, totalHeld: 0n }),
    seat: seat({ status: 'settled' }),
    edition: { edition: '901', cid: CID },
  }),
  releasedNoLink: circumstances({
    record: agreement({ state: 'RELEASED', seats: 3, totalHeld: 0n }),
    edition: null,
  }),
  releasedClosed: circumstances({
    record: null,
    closedOutcome: 'released',
    edition: { edition: '901', cid: CID },
  }),
  refundable: circumstances({
    record: agreement({ state: 'EXPIRED', refundCursor: 0 }),
    seat: seat(),
    chainNow: DEADLINE,
  }),
  refunding: circumstances({
    record: agreement({ state: 'REFUNDING', refundCursor: 1 }),
    seat: seat({ index: 1 }),
    chainNow: DEADLINE,
  }),
  refundingSkipped: circumstances({
    record: agreement({ state: 'REFUNDING', refundCursor: 1, unclaimedSeats: 1 }),
    seat: seat({ index: 0 }),
    chainNow: DEADLINE,
    canPayFee: false,
  }),
  refunded: circumstances({
    record: agreement({ state: 'REFUNDED', refundCursor: 2, totalHeld: 0n }),
    seat: seat({ status: 'settled' }),
    chainNow: DEADLINE,
  }),
  refundedClosed: circumstances({ record: null, closedOutcome: 'refunded' }),
  gone: circumstances({ record: null }),
}

const QUOTE: SeatQuote = {
  agreementId: TRIP,
  amount: 5_000_000n,
  seatsTotal: 3,
  seatsLeft: 1,
  deadline: Number(DEADLINE),
  commitSha256: 'fa'.repeat(32),
}

function onTrip(chain: Circumstances, overrides: Partial<State> = {}): State {
  return {
    ...initialState(),
    wallet: WALLET,
    agreementId: TRIP,
    circumstances: chain,
    ...overrides,
  }
}

const dispatch = vi.fn()

function inBrand(ui: ReactElement, brand: Brand = { brand: 'travel', offer: OFFER }) {
  return render(<BrandContext value={brand}>{ui}</BrandContext>)
}

const renderTrip = (state: State, brand?: Brand) =>
  inBrand(<PoolScreen state={state} dispatch={dispatch} />, brand)

const renderEntry = (state: State, brand?: Brand) =>
  inBrand(<EntryScreen state={state} dispatch={dispatch} />, brand)

const text = (container: HTMLElement) => container.textContent ?? ''

const NONE_OPEN: State = {
  ...initialState(),
  purchase: 'UNAVAILABLE',
  unavailable: { reason: 'no_pool_open', message: '' },
}

const SETTLE_FAILED = { kind: 'settle_failed', message: 'x' } as const

/** A node's position relative to another: true when `first` comes before `second`. */
const precedes = (first: Node, second: Node) =>
  Boolean(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING)

function stubNetwork() {
  vi.stubEnv('VITE_NETWORK', 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=')
  vi.stubEnv('VITE_ASSET_ID', '10458941')
  vi.stubEnv('VITE_APP_ID', '771795120')
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', GATEWAY)
  // No brand from the machine's environment: each test that renders `App`
  // sets the one it means.
  vi.stubEnv('VITE_BRAND', '')
  vi.stubEnv('VITE_OFFER_B64', '')
  resetConfigForTests()
  resetBrandForTests()
}

beforeEach(() => {
  dispatch.mockReset()
  stubNetwork()
})

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.useRealTimers()
  resetConfigForTests()
  resetBrandForTests()
})

describe('the entry page reads as a trip on offer', () => {
  it('sets the product line whole and verbatim, in the strip under the header', () => {
    renderEntry(initialState())
    expect(screen.getByText(TAGLINE)).toBeDefined()
  })

  it("heads the page with the offer's title, its dates and the hook", () => {
    const { container } = renderEntry(initialState())
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Example City beach week')
    expect(text(container)).toMatch(/7 nights · .+ – .+ · a trip for 3 travellers/)
    // No count before a quote: how many must join is the agreement's, not the
    // offer's.
    expect(
      screen.getByText(
        'This trip only runs if enough travellers join. Nobody pays unless enough do.',
      ),
    ).toBeDefined()
  })

  it('shows the offer card: the route, the reference fare, and where the fare came from', () => {
    renderEntry(initialState())
    const card = screen.getByRole('region', { name: 'Trip offer' })
    expect(within(card).getByText('From Sample Town (SMP) to Example City (EXC)')).toBeDefined()
    expect(
      within(card).getByText('Reference fare: 612.40 USD, per traveller, round trip'),
    ).toBeDefined()
    expect(within(card).getByText('Fare data: see the demo notice')).toBeDefined()
  })

  it('credits the data source by name only when the offer names it', () => {
    const offer = { ...OFFER, provider: 'Sample Data Co' }
    renderEntry(initialState(), { brand: 'travel', offer })
    expect(screen.getByText('Fare data: Sample Data Co, see the demo notice')).toBeDefined()
    expect(screen.getByRole('complementary', { name: 'Demo notice' }).textContent).toContain(
      'Earnest Travel is an independent demo, not a Sample Data Co product.',
    )
  })

  it('shows no seat price or deadline before a quote has said what they are', () => {
    renderEntry(initialState())
    const card = screen.getByRole('region', { name: 'Trip offer' })
    expect(text(card)).not.toMatch(/Seat:|Book by:/)
  })

  it('shows the quoted seat price and deadline on the card once a quote is on hand', () => {
    renderEntry({ ...initialState(), purchase: 'CONFIRM', quote: QUOTE })
    const card = screen.getByRole('region', { name: 'Trip offer' })
    expect(within(card).getByText('Seat: 5.00 USDC')).toBeDefined()
    expect(text(card)).toMatch(/Book by: /)
  })

  it('states how the trip works, as numbered steps with no count before a quote', () => {
    renderEntry(initialState())
    const terms = screen.getByRole('heading', { name: 'How this trip works' }).parentElement!
    const steps = within(terms).getAllByRole('listitem').map((li) => li.textContent)
    expect(steps).toHaveLength(4)
    expect(steps[0]).toBe(
      'Each of the seats is paid in USDC and held by an Algorand escrow app until the trip fills.',
    )
    expect(steps[1]).toMatch(/^If enough different travellers take a seat before the deadline/)
  })

  it("counts the seats from the quote, not the offer's travellers", () => {
    // The offer says 3 travellers; the pool on sale has 5 seats. What a buyer
    // pays against is the pool.
    const quote = { ...QUOTE, seatsTotal: 5, seatsLeft: 3 }
    renderEntry({ ...initialState(), purchase: 'CONFIRM', quote })
    expect(
      screen.getByText('This trip only runs if 5 travellers join. Nobody pays unless enough do.'),
    ).toBeDefined()
    const terms = screen.getByRole('heading', { name: 'How this trip works' }).parentElement!
    const steps = within(terms).getAllByRole('listitem').map((li) => li.textContent)
    expect(steps[0]).toBe(
      'Each of the 5 seats is paid in USDC and held by an Algorand escrow app until the trip ' +
        'fills.',
    )
    expect(steps[1]).toMatch(/^If 5 different travellers take a seat before the deadline/)
  })

  it('asks for the seat price with the travel label', () => {
    renderEntry(initialState())
    expect(screen.getByRole('button', { name: 'See the seat price' })).toBeDefined()
  })

  it('says no trip is open when the route has none', () => {
    renderEntry(NONE_OPEN)
    expect(screen.getByText('No trip is open right now.')).toBeDefined()
  })

  it('lists found seats as trips', () => {
    renderEntry({ ...initialState(), wallet: WALLET, myPools: [{ agreementId: TRIP }] })
    expect(screen.getByRole('link', { name: 'Trip 25' }).getAttribute('href')).toBe('/app/pool/25')
  })
})

describe('the demo notice', () => {
  const NOTICE =
    'Demo notice. A technical demonstration on Algorand TestNet, paid with test USDC. Nothing is ' +
    'booked or sold, no travel takes place, and the trip file cannot be exchanged for travel or ' +
    'anything else. Example travel API, test environment. Indicative, not bookable. The seat ' +
    'price is not a share of the fare. A real group trip would be sold by a licensed organiser ' +
    'as merchant of record.'

  it("carries the offer's source, and names no data source the offer does not name", () => {
    renderEntry(initialState())
    expect(screen.getByRole('complementary', { name: 'Demo notice' }).textContent).toBe(NOTICE)
  })

  it('is its own band, directly after the offer card and outside it', () => {
    renderEntry(initialState())
    const card = screen.getByRole('region', { name: 'Trip offer' })
    const notice = screen.getByRole('complementary', { name: 'Demo notice' })
    expect(card.contains(notice)).toBe(false)
    expect(notice.contains(card)).toBe(false)
    expect(card.nextElementSibling).toBe(notice)
  })

  it('comes before the button that starts a purchase on the entry page', () => {
    renderEntry(initialState())
    const notice = screen.getByRole('complementary', { name: 'Demo notice' })
    expect(precedes(notice, screen.getByRole('button', { name: 'See the seat price' }))).toBe(true)
  })

  it("comes before the pay button on a trip's own page", () => {
    renderTrip(onTrip(CHAIN.waiting, { purchase: 'CONFIRM', quote: QUOTE }))
    const notice = screen.getByRole('complementary', { name: 'Demo notice' })
    const pay = screen.getByRole('button', { name: 'Take a seat for 5.00 USDC' })
    expect(precedes(notice, pay)).toBe(true)
    // And directly under the card there too.
    expect(screen.getByRole('region', { name: 'Trip offer' }).nextElementSibling).toBe(notice)
  })

  it('cannot be collapsed', () => {
    const { container } = renderEntry(initialState())
    expect(container.querySelector('details, summary, [aria-expanded]')).toBeNull()
  })

  it('stands in a neutral source, and no offer card, for a build with no offer', () => {
    renderEntry(initialState(), { brand: 'travel' })
    expect(screen.queryByRole('region', { name: 'Trip offer' })).toBeNull()
    const notice = screen.getByRole('complementary', { name: 'Demo notice' }).textContent
    expect(notice).toContain('Sample built from travel-API test data: indicative, not bookable.')
    expect(notice).not.toContain('independent demo')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('A sample group trip')
    expect(
      screen.getByText(
        'This trip only runs if enough travellers join. Nobody pays unless enough do.',
      ),
    ).toBeDefined()
  })

  it('is not on the index', () => {
    const { container } = render(<EntryScreen state={initialState()} dispatch={dispatch} />)
    expect(screen.queryByRole('complementary', { name: 'Demo notice' })).toBeNull()
    expect(text(container)).toContain('Route Index')
  })
})

describe("a trip's own page", () => {
  it('is headed with the trip, the offer and the notice', () => {
    renderTrip(onTrip(CHAIN.waiting))
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Trip 25')
    const card = screen.getByRole('region', { name: 'Trip offer' })
    // The price and the deadline are the record's, once it has been read.
    expect(within(card).getByText('Seat: 5.00 USDC')).toBeDefined()
    expect(text(card)).toMatch(/Book by: /)
  })

  it('says when to book by only while the trip still takes seats', () => {
    const bookBy = () =>
      /Book by: /.test(text(screen.getByRole('region', { name: 'Trip offer' })))
    const shown: [string, State, boolean][] = [
      ['waiting', onTrip(CHAIN.waiting), true],
      ['lapsed while open', onTrip(CHAIN.lapsedOpen), false],
      ['filled', onTrip(CHAIN.filled), false],
      ['released', onTrip(CHAIN.released), false],
      ['refunding', onTrip(CHAIN.refunding), false],
      ['closed, with a quote for it', onTrip(CHAIN.gone, { quote: QUOTE }), false],
    ]
    for (const [name, state, expected] of shown) {
      const { unmount } = renderTrip(state)
      expect(bookBy(), name).toBe(expected)
      // The seat price stays: it is what every seat paid in.
      if (state.circumstances?.record) {
        expect(text(screen.getByRole('region', { name: 'Trip offer' })), name).toContain(
          'Seat: 5.00 USDC',
        )
      }
      unmount()
    }
  })

  it('confirms a seat in the travel words', () => {
    const { container } = renderTrip(onTrip(CHAIN.waiting, { purchase: 'CONFIRM', quote: QUOTE }))
    expect(screen.getByRole('heading', { name: 'Check your seat before paying' })).toBeDefined()
    expect(within(container.querySelector('dl')!).getByText('Trip')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Take a seat for 5.00 USDC' })).toBeDefined()
  })

  it("states the terms with the quote's seat count, not the offer's travellers", () => {
    const record = agreement({ minSeats: 5, maxSeats: 5 })
    const quote = { ...QUOTE, seatsTotal: 5, seatsLeft: 3 }
    renderTrip(
      onTrip(circumstances({ record, seat: null }), { purchase: 'CONFIRM', quote }),
    )
    const terms = screen.getByRole('heading', { name: 'How this trip works' }).parentElement!
    const steps = within(terms).getAllByRole('listitem').map((li) => li.textContent)
    expect(steps[0]).toMatch(/^Each of the 5 seats is paid in USDC/)
    expect(steps[1]).toMatch(/^If 5 different travellers take a seat before the deadline/)
  })

  it('waits for travellers, counting seats, stamped Held', () => {
    const { container } = renderTrip(onTrip(CHAIN.waiting))
    expect(screen.getByRole('heading', { name: 'Waiting for travellers' })).toBeDefined()
    expect(text(container)).toContain('2 of 3 seats taken.')
    expect(container.querySelector('.stamp')?.textContent).toBe('Held')
  })

  it('says a full trip is owed its trip file', () => {
    renderTrip(onTrip(CHAIN.filled))
    const heading = screen.getByRole('heading', { name: 'Trip filled: the trip file is owed' })
    expect(heading).toBeDefined()
  })

  it('links the released trip file, and the committed file beside it', () => {
    const { container } = renderTrip(onTrip(CHAIN.released))
    expect(screen.getByRole('heading', { name: 'Trip file released' })).toBeDefined()
    expect(screen.getByRole('link', { name: 'trip-901.html' }).getAttribute('href')).toBe(
      `${GATEWAY}/ipfs/${CID}/trip-901.html`,
    )
    expect(screen.getByRole('link', { name: 'trip-901.json' }).getAttribute('href')).toBe(
      `${GATEWAY}/ipfs/${CID}/trip-901.json`,
    )
    expect(text(container)).toContain('Your trip file: trip-901.html')
    expect(text(container)).toContain('trip-901.json (the committed file)')
    expect(container.querySelector('.stamp')?.textContent).toBe('Released')
  })

  it('stamps every refund situation Expired, in the refund colour', () => {
    for (const chain of [CHAIN.refundable, CHAIN.refunding, CHAIN.refunded]) {
      const { container, unmount } = renderTrip(onTrip(chain))
      const stamp = container.querySelector('.stamp')
      expect(stamp?.textContent).toBe('Expired')
      expect(stamp?.getAttribute('data-outcome')).toBe('refund')
      unmount()
    }
  })

  it('names the calls after the trip', () => {
    renderTrip(onTrip(CHAIN.refundable))
    expect(screen.getByRole('button', { name: 'Send the next refunds' })).toBeDefined()
    cleanup()
    renderTrip(onTrip(CHAIN.lapsedOpen))
    expect(screen.getByRole('button', { name: 'Expire the trip' })).toBeDefined()
  })
})

describe('a trip past its deadline reads as expired, by the chain clock', () => {
  it('says fewer travellers joined when the trip was still selling seats', () => {
    const { container } = renderTrip(onTrip(CHAIN.lapsedOpen))
    expect(
      screen.getByRole('heading', { name: 'Expired: fewer than 3 travellers joined in time' }),
    ).toBeDefined()
    expect(text(container)).toContain(
      'The trip does not go ahead. Your USDC stays in the escrow app until someone sends the ' +
        'expire call below. Anyone can, and the refunds then go on-chain to each wallet that paid.',
    )
    expect(screen.queryByRole('heading', { name: 'Waiting for travellers' })).toBeNull()
  })

  it('says the trip file was not released when the trip had filled', () => {
    // Not "fewer travellers joined": every seat was taken. What missed the
    // deadline is the release.
    const { container } = renderTrip(onTrip(CHAIN.lapsedFunded))
    expect(
      screen.getByRole('heading', { name: 'Expired: the trip file was not released in time' }),
    ).toBeDefined()
    expect(text(container)).not.toMatch(/fewer than/)
    expect(screen.queryByRole('heading', { name: 'Trip filled: the trip file is owed' })).toBeNull()
  })

  it('stamps both Expired, in the refund colour', () => {
    for (const chain of [CHAIN.lapsedOpen, CHAIN.lapsedFunded]) {
      const { container, unmount } = renderTrip(onTrip(chain))
      const stamp = container.querySelector('.stamp')
      expect(stamp?.textContent).toBe('Expired')
      expect(stamp?.getAttribute('data-outcome')).toBe('refund')
      unmount()
    }
  })

  it('offers the expire call the page points at', () => {
    renderTrip(onTrip(CHAIN.lapsedFunded))
    expect(screen.getByRole('button', { name: 'Expire the trip' })).toBeDefined()
  })

  it('tells a viewer with no wallet that can pay the fee that anyone can send it', () => {
    for (const state of [
      onTrip({ ...CHAIN.lapsedOpen, canPayFee: false }),
      onTrip({ ...CHAIN.lapsedOpen, canPayFee: false }, { wallet: undefined }),
    ]) {
      const { container, unmount } = renderTrip(state)
      expect(text(container)).toContain(COPY.travel.lapsed.noFee)
      expect(screen.queryByRole('button', { name: 'Expire the trip' })).toBeNull()
      unmount()
    }
  })

  it('does not tell a viewer who can send the call that nobody here can', () => {
    const { container } = renderTrip(onTrip(CHAIN.lapsedOpen))
    expect(text(container)).not.toContain(COPY.travel.lapsed.noFee)
  })

  it("is still waiting one second before the deadline, whatever this device's clock says", () => {
    // Far past the deadline by the browser's clock, one second short of it by
    // the chain's: the chain's is the one the escrow app checks.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(Number(DEADLINE + 86_400n) * 1000)
    renderTrip(onTrip({ ...CHAIN.waiting, chainNow: DEADLINE - 1n }))
    expect(screen.getByRole('heading', { name: 'Waiting for travellers' })).toBeDefined()
    expect(document.querySelector('.stamp')?.textContent).toBe('Held')
  })

  it('does not read a trip as expired where the expire call is refused', () => {
    // FILLED is past `expire`'s reach, deadline or not: the page must not
    // point at a call that is never offered.
    const filledLate = circumstances({
      record: agreement({ state: 'FILLED', seats: 3 }),
      seat: seat(),
      chainNow: DEADLINE + 60n,
    })
    const { container } = renderTrip(onTrip(filledLate))
    expect(screen.getByRole('heading', { name: 'Trip filled: the trip file is owed' })).toBeDefined()
    expect(text(container)).not.toContain(COPY.travel.lapsed.body)
  })

  it('leaves the index as it was', () => {
    render(<PoolScreen state={onTrip(CHAIN.lapsedOpen)} dispatch={dispatch} />)
    expect(screen.getByRole('heading', { name: 'Waiting for seats' })).toBeDefined()
    expect(document.querySelector('.stamp')?.textContent).toBe('Held')
  })
})

describe('a count of one', () => {
  it('is said in the singular, wherever the travel copy counts travellers or seats', () => {
    expect(COPY.travel.hero.hook(1)).toBe(
      'This trip only runs if 1 traveller joins. Nobody pays unless enough do.',
    )
    const [first, second] = COPY.travel.terms.items(1)
    expect(first).toBe(
      'The seat is paid in USDC and held by an Algorand escrow app until the trip fills.',
    )
    expect(second).toMatch(/^If 1 traveller takes a seat before the deadline, every traveller/)
    expect(COPY.travel.lapsed.heading(1, false)).toBe('Expired: no traveller joined in time')
  })
})

describe('TestnetFunds', () => {
  it('is a region named by its heading', () => {
    inBrand(<TestnetFunds />)
    expect(screen.getByRole('region', { name: 'Paying on TestNet' })).toBeDefined()
  })

  it('lists the four steps, adding the USDC asset this build takes', () => {
    inBrand(<TestnetFunds />)
    const list = screen.getByRole('list', { name: 'How to get TestNet funds for a seat' })
    const steps = within(list).getAllByRole('listitem').map((li) => li.textContent)
    expect(steps).toHaveLength(4)
    expect(steps[0]).toMatch(/^Pera \(tested; Defly untested\)/)
    expect(steps[1]).toContain('lora.algokit.io/testnet/fund')
    expect(steps[2]).toBe('Add asset 10458941 (USDC).')
    // No seat price: that is the quote's, and this band is on every page.
    expect(steps[3]).toBe(
      'faucet.circle.com → Algorand Testnet: 20 USDC every 2 h. The seat payment\'s network ' +
        'fee is currently paid by the facilitator.',
    )
    expect(within(list).getByRole('link', { name: 'faucet.circle.com' }).getAttribute('href')).toBe(
      'https://faucet.circle.com',
    )
  })

  it('is not on the index', () => {
    const { container } = render(<TestnetFunds />)
    expect(container.innerHTML).toBe('')
  })
})

describe('no travel page says what the travel copy avoids', () => {
  const CONFIRMING = onTrip(CHAIN.waiting, { purchase: 'CONFIRM', quote: QUOTE })
  const stale = reduce(
    { ...CONFIRMING, purchase: 'CHECKING' },
    {
      type: 'QUOTE_STALE',
      noun: COPY.travel.nouns.pool,
      disagreements: [
        ...quoteDisagreements(QUOTE, null, COPY.travel.nouns),
        // Every line the check can write: state, price, seats and commitment.
        ...quoteDisagreements(
          QUOTE,
          agreement({
            state: 'EXPIRED',
            sharePrice: 6_000_000n,
            seats: 3,
            commitSha256: 'ab'.repeat(32),
          }),
          COPY.travel.nouns,
        ),
      ],
    },
  ).state

  const entries: [string, State, Brand?][] = [
    ['the entry page', initialState()],
    ['the entry page with no offer', initialState(), { brand: 'travel' }],
    ['the entry page quoting', { ...initialState(), purchase: 'QUOTING' }],
    ['the entry page with no trip open', NONE_OPEN],
    ['found seats', { ...initialState(), wallet: WALLET, myPools: [{ agreementId: TRIP }] }],
    ['found no seats', { ...initialState(), wallet: WALLET, myPools: [] }],
    ['a stale quote', stale],
    ['a failed settlement', { ...initialState(), purchaseError: SETTLE_FAILED }],
    ['a payment awaiting its seat', { ...initialState(), awaitingSeat: TRIP }],
  ]

  it.each(entries)('%s', (_name, state, brand) => {
    const { container } = renderEntry(state, brand)
    expect(text(container)).not.toMatch(FORBIDDEN)
  })

  const unread = (overrides: Partial<State>) =>
    onTrip(CHAIN.waiting, { circumstances: undefined, ...overrides })
  const NO_WALLET = { wallet: undefined }
  const elsewhere = { action: 'expire', agreementId: OTHER_TRIP } as const
  const unconfirmed = {
    action: 'close',
    agreementId: TRIP,
    unconfirmed: { txId: 'T'.repeat(52), lastValid: 1n },
  } as const

  const trips: [string, State][] = [
    ['opening', { ...initialState(), agreementId: undefined }],
    ['reading', onTrip(CHAIN.waiting, { circumstances: undefined })],
    ['a first read that failed', unread({ readError: { kind: 'unreachable', message: 'x' } })],
    ['a first read the build cannot make', unread({ readError: { kind: 'incompatible', message: 'x' } })],
    ['a stale reading', onTrip(CHAIN.waiting, { readError: { kind: 'unreachable', message: 'x' } })],
    ['confirming', CONFIRMING],
    ['a stale quote', { ...stale, agreementId: TRIP, circumstances: CHAIN.waiting }],
    ['waiting', onTrip(CHAIN.waiting)],
    ['waiting, no wallet', onTrip(CHAIN.waiting, { wallet: undefined })],
    ['waiting, no seat', onTrip({ ...CHAIN.waiting, seat: null })],
    ['confirming a seat', onTrip({ ...CHAIN.waiting, seat: null }, { awaitingSeat: TRIP })],
    ['lapsed while open', onTrip(CHAIN.lapsedOpen)],
    ['lapsed while open, no fee', onTrip({ ...CHAIN.lapsedOpen, canPayFee: false })],
    ['filled', onTrip(CHAIN.filled)],
    ['lapsed while filled', onTrip(CHAIN.lapsedFunded)],
    ['released', onTrip(CHAIN.released)],
    ['released, no link', onTrip(CHAIN.releasedNoLink)],
    ['released and closed', onTrip(CHAIN.releasedClosed)],
    ['refundable', onTrip(CHAIN.refundable)],
    ['refundable, no wallet', onTrip({ ...CHAIN.refundable, canPayFee: false }, NO_WALLET)],
    ['refunding', onTrip(CHAIN.refunding)],
    ['refunding, skipped', onTrip(CHAIN.refundingSkipped)],
    ['refunded', onTrip(CHAIN.refunded)],
    ['refunded and closed', onTrip(CHAIN.refundedClosed)],
    ['gone', onTrip(CHAIN.gone)],
    ['a call on another trip', onTrip(CHAIN.refundable, { sending: elsewhere })],
    ['a call waiting for the chain', onTrip(CHAIN.refundable, { sending: unconfirmed })],
    ['a failed settlement', onTrip(CHAIN.waiting, { purchaseError: SETTLE_FAILED })],
  ]

  it.each(trips)('%s', (_name, state) => {
    const { container } = renderTrip(state)
    expect(text(container)).not.toMatch(FORBIDDEN)
  })

  it('TestnetFunds', () => {
    const { container } = inBrand(<TestnetFunds />)
    expect(text(container)).not.toMatch(FORBIDDEN)
  })
})

describe('the shell', () => {
  const encode = (value: unknown) => Buffer.from(JSON.stringify(value), 'utf8').toString('base64')
  const MAINNET = 'algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
  // LocalNet mints a fresh genesis hash on every reset: any other well-formed
  // identifier stands for it.
  const LOCALNET = `algorand:${'A'.repeat(43)}=`

  beforeEach(() => {
    runMock.mockReset()
    runMock.mockResolvedValue([])
    useWalletMock.mockReturnValue({
      activeAddress: null,
      activeWallet: null,
      signTransactions: vi.fn(),
      wallets: [],
    })
    window.history.replaceState(null, '', '/app/')
  })

  afterEach(() => {
    window.history.replaceState(null, '', '/')
  })

  const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

  it('shows the wordmark, the badge and TestnetFunds on a travel build', async () => {
    vi.stubEnv('VITE_BRAND', 'travel')
    vi.stubEnv('VITE_OFFER_B64', encode(OFFER))
    resetBrandForTests()
    const { App } = await import('../src/ui/App')
    const { container } = render(<App />)
    await act(settle)

    expect(screen.getByRole('link', { name: 'Earnest Travel' }).getAttribute('href')).toBe('/app/')
    expect(screen.getByText('TestNet demo')).toBeDefined()
    expect(screen.getByRole('list', { name: 'How to get TestNet funds for a seat' })).toBeDefined()
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Example City beach week')
    expect(text(container)).not.toMatch(FORBIDDEN)
  })

  it('renders the configuration error for an offer it cannot read, naming the key', async () => {
    vi.stubEnv('VITE_BRAND', 'travel')
    vi.stubEnv('VITE_OFFER_B64', encode({ ...OFFER, nights: 30 }))
    resetBrandForTests()
    const { App } = await import('../src/ui/App')
    render(<App />)
    expect(screen.getByRole('heading', { name: /configuration error/i })).toBeDefined()
    expect(screen.getByText(/VITE_OFFER_B64/)).toBeDefined()
    expect(screen.getByText(/nights/)).toBeDefined()
  })

  it('refuses a travel build on any network but TestNet', async () => {
    // Its badge, its demo notice and its funding steps all say test USDC on
    // TestNet, and none of that is true anywhere else.
    const { App } = await import('../src/ui/App')
    for (const network of [MAINNET, LOCALNET]) {
      vi.stubEnv('VITE_BRAND', 'travel')
      vi.stubEnv('VITE_OFFER_B64', encode(OFFER))
      vi.stubEnv('VITE_NETWORK', network)
      resetConfigForTests()
      resetBrandForTests()
      const { unmount } = render(<App />)
      expect(screen.getByRole('heading', { name: /configuration error/i }), network).toBeDefined()
      expect(screen.getByText(/VITE_NETWORK/), network).toBeDefined()
      expect(screen.queryByText('TestNet demo'), network).toBeNull()
      unmount()
    }
  })

  it('still runs an index build on MainNet', async () => {
    vi.stubEnv('VITE_NETWORK', MAINNET)
    resetConfigForTests()
    const { App } = await import('../src/ui/App')
    render(<App />)
    await act(settle)
    expect(screen.queryByRole('heading', { name: /configuration error/i })).toBeNull()
    expect(screen.getByRole('link', { name: 'Earnest' })).toBeDefined()
  })

  it('renders the configuration error for a brand it does not know', async () => {
    vi.stubEnv('VITE_BRAND', 'cruise')
    resetBrandForTests()
    const { App } = await import('../src/ui/App')
    render(<App />)
    expect(screen.getByText(/VITE_BRAND/)).toBeDefined()
  })

  it('keeps the index shell on an index build', async () => {
    vi.stubEnv('VITE_BRAND', '')
    resetBrandForTests()
    const { App } = await import('../src/ui/App')
    render(<App />)
    await act(settle)
    expect(screen.getByRole('link', { name: 'Earnest' })).toBeDefined()
    expect(screen.queryByText('TestNet demo')).toBeNull()
    expect(screen.queryByRole('list', { name: 'How to get TestNet funds for a seat' })).toBeNull()
  })
})
