// @vitest-environment jsdom
//
// The screens, rendered against hand-built states rather than a running
// machine. Every assertion here is a sentence that costs a buyer money or trust
// when it is wrong, and every one is checked against the state the reducer
// would actually hold -- where an event decides the shape, the state is made
// by `reduce` from a hand-built one, never by the runner.
//
// Rendering only. jsdom's TextEncoder returns a Uint8Array from another realm
// and algosdk rejects it, so nothing here may build a transaction.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import type { Agreement, Seat } from '../src/chain/decode'
import { initialState, reduce, type State } from '../src/pool/machine'
import { verdictFor, type Action, type Circumstances, type SeatQuote } from '../src/pool/view'
import { EntryScreen } from '../src/ui/EntryScreen'
import { PoolScreen } from '../src/ui/PoolScreen'
import { resetConfigForTests } from '../src/config'

const WALLET = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'
const POOL = 25n
const OTHER_POOL = 26n
const DEADLINE = 1789651281n
const TXID = 'NTSTR5XWNGOVBSQ7DRWTXQC3L7ZKXOZBJWRDGKOOQGFRL7LD4HMA'

function agreement(overrides: Partial<Agreement> = {}): Agreement {
  return {
    condition: 0,
    state: 'OPEN',
    deadline: DEADLINE,
    sharePrice: 5_000_000n,
    minSeats: 5,
    maxSeats: 5,
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
  waiting: circumstances(),
  awaitingDelivery: circumstances({ record: agreement({ state: 'FUNDED', seats: 5 }) }),
  released: circumstances({ record: agreement({ state: 'RELEASED', seats: 5, totalHeld: 0n }) }),
  // Expired, and the pass has not started. The cursor is at zero, because
  // `expire` never moves it and the first `refund_next` leaves EXPIRED, so no
  // seat can be behind it: this wallet's seat is still the pass's to pay.
  refundable: circumstances({
    record: agreement({ state: 'EXPIRED', seats: 5, refundCursor: 0 }),
    seat: seat(),
    chainNow: DEADLINE,
  }),
  refunding: circumstances({
    record: agreement({ state: 'REFUNDING', seats: 5, refundCursor: 3 }),
    chainNow: DEADLINE,
  }),
  // A running pass that skipped this wallet's seat: the verdict offers two
  // calls, `refund_next` for the seats still ahead and `claim_refund` for this
  // one.
  refundingSkipped: circumstances({
    record: agreement({ state: 'REFUNDING', seats: 5, refundCursor: 3, unclaimedSeats: 1 }),
    seat: seat({ index: 0 }),
    chainNow: DEADLINE,
  }),
  // The pass finished and skipped this wallet's seat.
  refundedOwed: circumstances({
    record: agreement({ state: 'REFUNDED', seats: 5, refundCursor: 5, unclaimedSeats: 1 }),
    seat: seat(),
    chainNow: DEADLINE,
  }),
  gone: circumstances({ record: null }),
}

const QUOTE: SeatQuote = {
  agreementId: POOL,
  amount: 5_000_000n,
  seatsTotal: 5,
  seatsLeft: 3,
  deadline: Number(DEADLINE),
  commitSha256: 'fa'.repeat(32),
}

/** A buyer with a wallet, looking at pool 25 as `chain` describes it. */
function onPool(chain: Circumstances, overrides: Partial<State> = {}): State {
  return {
    ...initialState(),
    wallet: WALLET,
    agreementId: POOL,
    circumstances: chain,
    ...overrides,
  }
}

const dispatch = vi.fn()

function renderPool(state: State) {
  return render(<PoolScreen state={state} dispatch={dispatch} />)
}

function renderEntry(state: State) {
  return render(<EntryScreen state={state} dispatch={dispatch} />)
}

const buttonNames = () =>
  screen.queryAllByRole('button').map((button) => button.textContent ?? '')

beforeEach(() => {
  dispatch.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('a closed edition is an answer, not an error', () => {
  it('renders no_pool_open as "no edition is open right now", with no error in it', () => {
    const state: State = {
      ...initialState(),
      purchase: 'UNAVAILABLE',
      unavailable: {
        reason: 'no_pool_open',
        message: 'the route is not quoting right now: no_pool_open',
      },
    }
    const { container } = renderEntry(state)
    expect(screen.getByText(/no edition is open right now/i)).toBeDefined()
    expect(container.textContent).not.toMatch(/error/i)
  })

  it('renders pool_status_unavailable as the transient failure it is', () => {
    // The control for the test above: the two reasons must not render alike.
    const state: State = {
      ...initialState(),
      purchase: 'UNAVAILABLE',
      unavailable: { reason: 'pool_status_unavailable', message: 'unused' },
    }
    renderEntry(state)
    expect(screen.getByText(/could not reach the chain/i)).toBeDefined()
    expect(screen.queryByText(/no edition is open/i)).toBeNull()
  })
})

describe('the product line is the headline, verbatim', () => {
  it('splits the exact line between headline and lede, with the commitment defining it', () => {
    const { container } = renderEntry(initialState())
    const headline = screen.getByRole('heading', { level: 1 })
    expect(headline.textContent).toBe('"If this, then pay" for x402.')
    const lede = screen.getByText(/^Earnest holds the payment on Algorand/)
    // Split for the type, never reworded: read together they are the line.
    expect(`${headline.textContent} ${lede.textContent}`).toBe(
      '"If this, then pay" for x402. Earnest holds the payment on Algorand until the ' +
        'condition is met: enough buyers joined to fund a purchase together, the ' +
        'deliverable matches what was promised, or both.',
    )
    // The pool line is second, never first: it qualifies the product line
    // and a view that led with it would be selling a threshold, not Earnest.
    const poolLine = screen.getByText('Nobody pays unless enough do.')
    expect(lede.compareDocumentPosition(poolLine) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // No seat count in it: this view renders before a quote arrives.
    expect(poolLine.textContent).not.toMatch(/\d/)
    // "What was promised" has to be defined in the same view.
    expect(container.textContent).toMatch(/sha256 was committed before you paid/)
    // The only admissible use of the word.
    const rest = (container.textContent ?? '').replaceAll('no arbitration', '')
    expect(rest).not.toMatch(/dispute|arbitrat/i)
  })
})

describe('the confirm view shows what is being bought, and a way to quote again', () => {
  const confirming: State = {
    ...initialState(),
    wallet: WALLET,
    agreementId: POOL,
    purchase: 'CONFIRM',
    quote: QUOTE,
  }

  it('offers a quote while nothing is on screen', () => {
    // Without this, the assertion below would pass on a screen that never
    // offered a quote anywhere.
    renderEntry({ ...initialState(), wallet: WALLET })
    expect(buttonNames()).toContain('Quote a seat in the open edition')
  })

  it.each([
    ['the entry screen', renderEntry],
    ['the pool screen', renderPool],
  ])('offers a fresh quote beside the buy button on %s', (_where, show) => {
    // The reducer admits a quote request from CONFIRM, so the control is a
    // click that does something -- and it is the only one here for a buyer
    // with no wallet, below.
    show(confirming)
    const names = buttonNames()
    expect(names).toContain('Buy a seat for 5.00 USDC')
    expect(names.filter((name) => /quote/i.test(name))).toEqual(['Get a fresh quote'])
    screen.getByRole('button', { name: 'Get a fresh quote' }).click()
    expect(dispatch).toHaveBeenCalledWith({ type: 'QUOTE_REQUESTED' })
  })

  it('offers a fresh quote to a buyer with no wallet, who cannot buy', () => {
    renderPool({ ...confirming, wallet: undefined })
    expect(screen.getByText(/connect a wallet to buy this seat/i)).toBeDefined()
    expect(buttonNames()).toEqual(['Get a fresh quote'])
  })

  it('reads back the price, the seats taken of total and the commitment', () => {
    renderPool(confirming)
    expect(screen.getByText('5.00 USDC')).toBeDefined()
    expect(screen.getByText('2 of 5')).toBeDefined()
    // The commitment is the whole product: a buyer who cannot read it back
    // cannot check what is delivered.
    expect(screen.getAllByText('fa'.repeat(32)).length).toBeGreaterThan(0)
  })

  it('formats the price from micro-units without passing through a float', () => {
    // 2^53 + 1 micro-units. As a float this rounds to ...740992.
    renderPool({ ...confirming, quote: { ...QUOTE, amount: 9_007_199_254_740_993n } })
    expect(screen.getByText('9007199254.740993 USDC')).toBeDefined()
  })
})

describe('a purchase outcome never tells a buyer the opposite of what happened', () => {
  const settling: State = { ...onPool(CHAIN.waiting), purchase: 'SETTLING', quote: QUOTE }

  it('says a failed settlement may have lost its answer, and to check before paying again', () => {
    // SETTLE_FAILED also carries a reply lost after the paid request left, when
    // the money may have moved. The route sells whichever edition is open, so
    // copy inviting a retry could have the buyer pay into the next pool too.
    const { state } = reduce(settling, {
      type: 'SETTLE_FAILED',
      error: 'settlement failed with 402',
    })
    const { container } = renderPool(state)
    expect(container.textContent).toMatch(/did not complete, or its answer was lost/i)
    expect(container.textContent).toMatch(
      /check this wallet's USDC balance and this pool's page before paying again/i,
    )
    expect(container.textContent).not.toMatch(/payment failed|try again/i)
  })

  it('says an unreadable receipt went through and must not be repeated', () => {
    const { state } = reduce(settling, {
      type: 'RECEIPT_UNREADABLE',
      agreementId: POOL,
      message: 'the payment settled but the answer is missing commitSha256',
    })
    const { container } = renderPool(state)
    expect(container.textContent).toMatch(/your payment went through/i)
    expect(container.textContent).toMatch(/do not pay again/i)
    expect(container.textContent).not.toMatch(/did not complete/i)
  })

  it('says a stale quote signed nothing, and gives its reasons', () => {
    const checking: State = { ...settling, purchase: 'CHECKING' }
    const { state } = reduce(checking, {
      type: 'QUOTE_STALE',
      disagreements: ['seats moved: this quote offered 3 of 5 still free, and 2 are free now.'],
    })
    const { container } = renderPool(state)
    expect(container.textContent).toMatch(/nothing was signed/i)
    expect(container.textContent).toMatch(/seats moved/i)
  })

  it('says a failed second attempt did not complete, and that the earlier payment went through', () => {
    // A payment into pool 25 is still awaiting its seat; the buyer quoted and
    // bought again, and the second attempt was refused. Both are true, and
    // each is its own line: one heading for the pair would drop one of them.
    const retrying: State = { ...settling, awaitingSeat: POOL }
    const { state } = reduce(retrying, { type: 'SETTLE_FAILED', error: 'one seat per address' })
    expect(state.awaitingSeat).toBe(POOL)
    const { container } = renderPool(state)
    expect(container.textContent).toMatch(/the purchase did not complete/i)
    expect(container.textContent).toMatch(/your payment for a seat in pool 25 went through/i)
  })

  it('still says an unreadable receipt went through once its pool has no record', () => {
    // The read that finds the boxes gone clears `awaitingSeat`, since no
    // reading can show the seat any more. The receipt's own kind is what keeps
    // the buyer from being told their payment did not complete.
    const paid = reduce(settling, {
      type: 'RECEIPT_UNREADABLE',
      agreementId: POOL,
      message: 'the payment settled but the answer is missing commitSha256',
    }).state
    const { state } = reduce(paid, {
      type: 'READ_RECEIVED',
      agreementId: POOL,
      address: WALLET,
      circumstances: CHAIN.gone,
    })
    expect(state.awaitingSeat).toBeUndefined()
    const { container } = renderPool(state)
    expect(container.textContent).toMatch(/your payment went through/i)
    expect(container.textContent).toMatch(/do not pay again/i)
    expect(container.textContent).not.toMatch(/did not complete/i)
  })

  it('does not call a failed quote a purchase that did not complete', () => {
    // Nothing was bought or attempted: the price could not be fetched.
    const quoting: State = { ...initialState(), purchase: 'QUOTING' }
    const { state } = reduce(quoting, { type: 'QUOTE_FAILED', error: 'expected a 402, got 500' })
    const { container } = renderEntry(state)
    expect(container.textContent).toMatch(/could not get a seat price/i)
    expect(container.textContent).not.toMatch(/did not complete|went through/i)
  })
})

describe('a buyer who has just paid is never told their seat is missing', () => {
  it('renders "confirming your seat", and no "no seat", while the node lags', () => {
    const { container } = renderPool(onPool(CHAIN.waiting, { awaitingSeat: POOL }))
    expect(container.textContent).toMatch(/confirming your seat/i)
    expect(container.textContent).not.toMatch(/no seat/i)
  })

  it('does say "no seat" when no payment is awaiting one', () => {
    // The control: the same read with nothing awaited must say it, or the
    // assertion above passes on a screen that never says it at all.
    const { container } = renderPool(onPool(CHAIN.waiting))
    expect(container.textContent).toMatch(/no seat/i)
  })
})

describe('each situation is its own screen', () => {
  it('renders each of the seven situations distinctly', () => {
    const expected = {
      waiting: CHAIN.waiting,
      'awaiting-delivery': CHAIN.awaitingDelivery,
      released: CHAIN.released,
      refundable: CHAIN.refundable,
      refunding: CHAIN.refunding,
      refunded: CHAIN.refundedOwed,
      gone: CHAIN.gone,
    }
    const headings = Object.entries(expected).map(([situation, chain]) => {
      // Pins the fixture: a record that stopped producing its situation would
      // otherwise let two screens compare equal for the wrong reason.
      expect(verdictFor(chain).situation).toBe(situation)
      const { unmount } = renderPool(onPool(chain))
      const heading = screen.getAllByRole('heading', { level: 2 })[0]?.textContent
      unmount()
      return heading
    })
    expect(headings.every((heading) => heading)).toBe(true)
    expect(new Set(headings).size).toBe(7)
  })
})

describe('the stamp says only what is true of the whole pool', () => {
  const stamp = () => document.querySelector<HTMLElement>('[data-outcome]')

  it.each([
    ['waiting', 'Held', 'held', CHAIN.waiting],
    ['awaiting-delivery', 'Held', 'held', CHAIN.awaitingDelivery],
    ['released', 'Released', 'release', CHAIN.released],
    ['refundable', 'Missed', 'refund', CHAIN.refundable],
    ['refunding', 'Missed', 'refund', CHAIN.refunding],
    ['refunded', 'Missed', 'refund', CHAIN.refundedOwed],
  ] as const)('stamps %s "%s", drawn as %s', (situation, word, outcome, chain) => {
    expect(verdictFor(chain).situation).toBe(situation)
    renderPool(onPool(chain))
    expect(stamp()?.textContent).toBe(word)
    expect(stamp()?.dataset.outcome).toBe(outcome)
  })

  it('never stamps a finished pass "refunded" while a seat is still owed', () => {
    // REFUNDED is the pass having finished, not everyone paid: this fixture's
    // seat was skipped and is owed 5.00 USDC, which the view says below.
    renderPool(onPool(CHAIN.refundedOwed))
    expect(verdictFor(CHAIN.refundedOwed).owed).toBe(true)
    expect(stamp()?.textContent).not.toMatch(/refunded/i)
  })

  it('stamps nothing when the chain could not say how a closed pool ended', () => {
    // Released and refunded are opposite news; a stamp here would be a guess.
    renderPool(onPool(CHAIN.gone))
    expect(verdictFor(CHAIN.gone).situation).toBe('gone')
    expect(stamp()).toBeNull()
  })
})

describe('the action buttons are the verdict, exactly', () => {
  const LABEL: Record<Action, string> = {
    expire: 'Expire the pool',
    refund_next: 'Send the next refunds',
    claim_refund: 'Claim my refund',
    close: 'Close the pool',
  }

  it('renders every offered action, in order, and nothing else', () => {
    const { actions } = verdictFor(CHAIN.refundingSkipped)
    expect(actions).toEqual(['refund_next', 'claim_refund'])
    renderPool(onPool(CHAIN.refundingSkipped))
    expect(buttonNames()).toEqual(actions.map((action) => LABEL[action]))
    screen.getByRole('button', { name: LABEL.claim_refund }).click()
    expect(dispatch).toHaveBeenCalledWith({ type: 'ACTION_REQUESTED', action: 'claim_refund' })
  })

  it('renders no button when the verdict offers none', () => {
    expect(verdictFor(CHAIN.waiting).actions).toEqual([])
    renderPool(onPool(CHAIN.waiting))
    expect(buttonNames()).toEqual([])
  })

  it('never offers close on a released pool', () => {
    // The contract would accept it; the verdict withholds it, and the screen
    // must not put it back.
    renderPool(onPool(CHAIN.released))
    expect(screen.queryByRole('button', { name: LABEL.close })).toBeNull()
  })
})

describe('the action lock is global, and a refresh holds it too', () => {
  const buttons = () => screen.getAllByRole('button') as HTMLButtonElement[]

  it('leaves the buttons enabled with nothing in flight', () => {
    // The control: without it, a screen that disabled every button always
    // would pass both tests below.
    renderPool(onPool(CHAIN.refundingSkipped))
    expect(buttons().length).toBe(2)
    expect(buttons().every((button) => !button.disabled)).toBe(true)
  })

  it('disables every button while a call on a different pool is in flight', () => {
    renderPool(
      onPool(CHAIN.refundingSkipped, {
        sending: { action: 'refund_next', agreementId: OTHER_POOL },
      }),
    )
    expect(buttons().length).toBe(2)
    expect(buttons().every((button) => button.disabled)).toBe(true)
  })

  it('disables every button while a read is out', () => {
    renderPool(onPool(CHAIN.refundingSkipped, { reading: true }))
    expect(buttons().length).toBe(2)
    expect(buttons().every((button) => button.disabled)).toBe(true)
  })
})

describe('a call that may still land reads as waiting', () => {
  it('renders an unconfirmed call as waiting for the chain, with its id, and never as failed', () => {
    const { container } = renderPool(
      onPool(CHAIN.refundable, {
        sending: {
          action: 'refund_next',
          agreementId: POOL,
          unconfirmed: { txId: TXID, lastValid: 1120n },
        },
        lastTxId: TXID,
      }),
    )
    expect(container.textContent).toMatch(/waiting for the chain/i)
    expect(screen.getAllByText(TXID).length).toBe(1)
    expect(container.textContent).not.toMatch(/fail|did not go through|error/i)
  })
})

describe('a buyer who cannot pay a fee is not at a dead end', () => {
  it('shows no button and says the refund is coming, without asking them to act', () => {
    const noFee = { ...CHAIN.refundable, canPayFee: false }
    expect(verdictFor(noFee).actions).toEqual([])
    const { container } = renderPool(onPool(noFee))
    expect(buttonNames()).toEqual([])
    expect(container.textContent).toMatch(/the refund is on its way/i)
    expect(container.textContent).toMatch(/anyone can/i)
    expect(container.textContent).toMatch(/never to whoever pays the fee/i)
    expect(container.textContent).not.toMatch(/you must/i)
  })

  it('does not tell a skipped seat on a finished pass that the refund is on its way', () => {
    // The pass is over. Nothing pays this seat until its address can receive
    // USDC again, and only this wallet can opt back in -- so "you do not need
    // to act" would leave the buyer waiting for money that is not coming.
    const noFee = { ...CHAIN.refundedOwed, canPayFee: false }
    expect(verdictFor(noFee).actions).toEqual([])
    expect(verdictFor(noFee).owed).toBe(true)
    const { container } = renderPool(onPool(noFee))
    expect(buttonNames()).toEqual([])
    expect(container.textContent).toMatch(/opt this wallet back in to USDC/i)
    expect(container.textContent).toMatch(
      /once this address can receive USDC again, anyone can send the claim/i,
    )
    expect(container.textContent).not.toMatch(/do not need to act/i)
    expect(container.textContent).not.toMatch(/on its way/i)
  })

  it('does not tell a skipped seat on a running pass that the refund is on its way', () => {
    // The pass is still running, but not for this seat: the cursor is past
    // it, and the pass never goes back. Only a claim pays it, so the notice
    // that is true for the seats ahead of the cursor is false for this one.
    const noFee = { ...CHAIN.refundingSkipped, canPayFee: false }
    expect(verdictFor(noFee).situation).toBe('refunding')
    expect(verdictFor(noFee).skipped).toBe(true)
    expect(verdictFor(noFee).actions).toEqual([])
    const { container } = renderPool(onPool(noFee))
    expect(buttonNames()).toEqual([])
    expect(container.textContent).toMatch(/still owed 5\.00 USDC/i)
    expect(container.textContent).toMatch(/opt this wallet back in to USDC/i)
    expect(container.textContent).toMatch(
      /once this address can receive USDC again, anyone can send the claim/i,
    )
    expect(container.textContent).not.toMatch(/do not need to act/i)
    expect(container.textContent).not.toMatch(/on its way/i)
    expect(container.textContent).not.toMatch(/still to be refunded/i)
  })
})

describe('a pool that missed says whether this seat was paid back', () => {
  // A refunded seat keeps its address and loses its amount, so it is still
  // this wallet's seat -- and "holds a seat" beside a refund reads as money
  // still held.
  const settled = seat({ amount: 0n, status: 'settled' })
  const onRefunding = (heldSeat: Seat) =>
    circumstances({
      record: agreement({ state: 'REFUNDING', seats: 5, refundCursor: 3, totalHeld: 5_000_000n }),
      seat: heldSeat,
      chainNow: DEADLINE,
    })

  it('says a settled seat on a running pass was refunded, from the share price', () => {
    const chain = onRefunding(settled)
    expect(verdictFor(chain).situation).toBe('refunding')
    const { container } = renderPool(onPool(chain))
    expect(container.textContent).toMatch(
      /this wallet's seat has been refunded: 5\.00 USDC was paid back to this address/i,
    )
    expect(container.textContent).not.toMatch(/holds a seat/i)
  })

  it('says a settled seat on a finished pass was refunded', () => {
    const chain = circumstances({
      record: agreement({ state: 'REFUNDED', seats: 5, refundCursor: 5, totalHeld: 0n }),
      seat: settled,
      chainNow: DEADLINE,
    })
    expect(verdictFor(chain).situation).toBe('refunded')
    const { container } = renderPool(onPool(chain))
    expect(container.textContent).toMatch(
      /this wallet's seat has been refunded: 5\.00 USDC was paid back to this address/i,
    )
    expect(container.textContent).not.toMatch(/holds a seat/i)
    expect(container.textContent).not.toMatch(/still owed/i)
  })

  it('says an owed seat on a running pass is still to be refunded, and where the pass is', () => {
    const { container } = renderPool(onPool(onRefunding(seat({ index: 4 }))))
    const text = container.textContent ?? ''
    expect(text).toMatch(
      /this wallet's seat is still to be refunded\. The refund pass has been through 3 of 5 seats/i,
    )
    // Said once: the view's own line about the pass gives way to this one.
    expect(text.match(/has been through 3 of 5 seats/g)?.length).toBe(1)
    expect(text).not.toMatch(/holds a seat|has been refunded/i)
  })

  it('says a seat behind the cursor of a running pass was skipped, not still to be refunded', () => {
    // The seat above sits ahead of the cursor; this one sits behind it. The
    // pass has been and gone, so "still to be refunded" would promise a pass
    // that is not coming back, and the claim is what pays it.
    const chain = CHAIN.refundingSkipped
    expect(verdictFor(chain).actions).toContain('claim_refund')
    const { container } = renderPool(onPool(chain))
    const text = container.textContent ?? ''
    expect(text).toMatch(/still owed 5\.00 USDC/i)
    expect(text).toMatch(/has gone past it without paying it/i)
    expect(text).not.toMatch(/still to be refunded/i)
    // The pass's position is still stated, and only once.
    expect(text.match(/has been through 3 of 5 seats/g)?.length).toBe(1)
    expect(buttonNames()).toContain('Claim my refund')
  })

  it('still states where the pass is when this wallet holds no seat', () => {
    // The control for the "said once" assertion above.
    const { container } = renderPool(onPool({ ...CHAIN.refunding, seat: null }))
    expect(container.textContent).toMatch(/has been through 3 of 5 seats/i)
    expect(container.textContent).toMatch(/holds no seat/i)
  })
})

describe('a finished refund pass is not everyone paid', () => {
  it('tells a skipped seat it is still owed, how to fix it, and that refunds cannot be paused', () => {
    const { container } = renderPool(onPool(CHAIN.refundedOwed))
    expect(container.textContent).toMatch(/still owed 5\.00 USDC/i)
    expect(container.textContent).toMatch(/opt this wallet back in to USDC/i)
    expect(container.textContent).toMatch(/do not opt out of USDC while a pool you joined is live/i)
    expect(container.textContent).toMatch(/refunds cannot be paused/i)
  })
})

describe('a read that failed says whether trying again can help', () => {
  const unread = (readError: State['readError']): State => ({
    ...initialState(),
    wallet: WALLET,
    agreementId: POOL,
    readError,
  })

  it('says it is trying again after a node that did not answer', () => {
    const { container } = renderPool(unread({ kind: 'unreachable', message: 'status 503' }))
    expect(container.textContent).toMatch(/trying again shortly/i)
  })

  it('never says it is trying again when the build cannot read this chain', () => {
    // The refresh has stopped, because every tick would meet the same wrong
    // network. "Trying again shortly" would promise a recovery that is not
    // coming, to a buyer who may be owed money.
    const { container } = renderPool(
      unread({
        kind: 'incompatible',
        message: 'this client is built for algorand:SGO1, but the node reports wGHE',
      }),
    )
    expect(container.textContent).toMatch(/trying again will not change that/i)
    expect(container.textContent).toMatch(/built for algorand:SGO1/)
    expect(container.textContent).not.toMatch(/trying again shortly/i)
  })

  it('says a stale reading will not be refreshed when the build cannot read this chain', () => {
    const { container } = renderPool(
      onPool(CHAIN.waiting, { readError: { kind: 'incompatible', message: 'undecodable' } }),
    )
    expect(container.textContent).toMatch(/will not try again/i)
  })
})

describe('a closed pool is one of two opposite outcomes, or an honest "gone"', () => {
  const closed = (closedOutcome: Circumstances['closedOutcome']) =>
    renderPool(onPool(circumstances({ record: null, closedOutcome }))).container.textContent ?? ''

  it('renders released and refunded differently, and undefined as neither', () => {
    const released = closed('released')
    cleanup()
    const refunded = closed('refunded')
    cleanup()
    const unknown = closed(undefined)

    expect(released).toMatch(/delivered/i)
    expect(released).not.toMatch(/refund/i)
    expect(refunded).toMatch(/refunded/i)
    expect(refunded).not.toMatch(/deliver/i)
    expect(unknown).not.toMatch(/deliver|refund/i)
    expect(new Set([released, refunded, unknown]).size).toBe(3)
  })
})

describe('a closed released pool is not said to have lost its commitment', () => {
  it('says the page cannot show the sha256, and that the chain still holds it', () => {
    // `close` deletes the record this page reads the sha256 from. The value
    // itself is an argument of the calls that created and released the pool,
    // and stays in the chain's history, so "no longer on chain" would be
    // false -- and would tell a buyer their proof of purchase is gone.
    const { container } = renderPool(onPool(circumstances({ record: null, closedOutcome: 'released' })))
    const text = container.textContent ?? ''
    expect(text).toMatch(/can no longer show the sha256/i)
    expect(text).toMatch(/stays in the chain's history/i)
    expect(text).not.toMatch(/can no longer be read from the chain/i)
  })
})

describe('a released pool links to its edition, or says it cannot yet', () => {
  const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'
  const LINK = { edition: '900', cid: CID }
  const GATEWAY = 'https://ipfs.example'

  beforeEach(() => {
    vi.stubEnv('VITE_NETWORK', 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=')
    vi.stubEnv('VITE_ASSET_ID', '10458941')
    vi.stubEnv('VITE_APP_ID', '771795120')
    vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
    vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
    vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
    vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
    vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
    vi.stubEnv('VITE_IPFS_GATEWAY', GATEWAY)
    resetConfigForTests()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    resetConfigForTests()
  })

  const open = (edition: Circumstances['edition']) => ({ ...CHAIN.released, edition })
  const closed = (edition: Circumstances['edition']) =>
    circumstances({ record: null, closedOutcome: 'released', edition })

  const hrefOf = (name: RegExp) => screen.getByRole('link', { name }).getAttribute('href')

  it('links the edition, and its committed JSON beside it, while the record still holds the sha256', () => {
    const { container } = renderPool(onPool(open(LINK)))
    expect(hrefOf(/edition-900\.html/)).toBe(`${GATEWAY}/ipfs/${CID}/edition-900.html`)
    expect(hrefOf(/edition-900\.json/)).toBe(`${GATEWAY}/ipfs/${CID}/edition-900.json`)
    const text = container.textContent ?? ''
    // The check the JSON is there for needs the value to check it against.
    expect(text).toContain('fa'.repeat(32))
    expect(text).toMatch(/sha256/i)
  })

  it('links them the same way once the record is closed', () => {
    // The note is history, which `close` does not touch.
    const { container } = renderPool(onPool(closed(LINK)))
    expect(hrefOf(/edition-900\.html/)).toBe(`${GATEWAY}/ipfs/${CID}/edition-900.html`)
    expect(hrefOf(/edition-900\.json/)).toBe(`${GATEWAY}/ipfs/${CID}/edition-900.json`)
    // Still one of two opposite outcomes, and this is not the other one.
    expect(container.textContent ?? '').not.toMatch(/refund/i)
  })

  it('says the link cannot be read right now, and shows none, when the note was not found', () => {
    for (const chain of [open(undefined), open(null), closed(undefined), closed(null)]) {
      const { container, unmount } = renderPool(onPool(chain))
      const text = container.textContent ?? ''
      expect(text).toMatch(/released/i)
      expect(text).toMatch(/link to the edition cannot be read right now/i)
      // Never an invented link: nothing on the page points at a gateway.
      const hrefs = screen.queryAllByRole('link').map((a) => a.getAttribute('href') ?? '')
      expect(hrefs.filter((href) => href.includes('/ipfs/'))).toEqual([])
      unmount()
    }
  })

  it('makes no claim about how long the edition stays available', () => {
    // Pinning is a service with its own terms, and nothing this page can read
    // says how long it lasts.
    for (const chain of [open(LINK), closed(LINK), open(null)]) {
      const { container, unmount } = renderPool(onPool(chain))
      expect(container.textContent ?? '').not.toMatch(/pinned|permanent|forever|always/i)
      unmount()
    }
  })
})

describe('recovery degrades to the purchase link', () => {
  it('lists found pools as links to their pages', () => {
    renderEntry({
      ...initialState(),
      wallet: WALLET,
      myPools: [{ agreementId: POOL }, { agreementId: OTHER_POOL }],
    })
    const links = within(screen.getByRole('list')).getAllByRole('link') as HTMLAnchorElement[]
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/app/pool/25',
      '/app/pool/26',
    ])
  })

  it('sends the buyer to their purchase link when the indexer is unavailable', () => {
    renderEntry({ ...initialState(), wallet: WALLET, myPoolsError: 'could not read history' })
    expect(screen.getByText(/open the link from your purchase/i)).toBeDefined()
  })
})

describe('a misconfigured build says so instead of rendering nothing', () => {
  beforeEach(() => {
    // Every key valid first, so the one broken below is the only thing wrong.
    // Without this the test reads whatever `.env` the machine happens to have
    // -- and where there is none, `loadConfig` stops at the first missing key
    // it checks, which is not the one this test names.
    vi.stubEnv('VITE_NETWORK', 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=')
    vi.stubEnv('VITE_ASSET_ID', '10458941')
    vi.stubEnv('VITE_APP_ID', '741000001')
    vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
    vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
    vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
    vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
    vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
    vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
    resetConfigForTests()
  })

  afterEach(() => {
    vi.unstubAllEnvs()
    resetConfigForTests()
  })

  it('renders the configuration error, naming the key', async () => {
    vi.stubEnv('VITE_APP_ID', '')
    const { App } = await import('../src/ui/App')
    render(<App />)
    expect(screen.getByRole('heading', { name: /configuration error/i })).toBeDefined()
    expect(screen.getByText(/VITE_APP_ID/)).toBeDefined()
  })
})
