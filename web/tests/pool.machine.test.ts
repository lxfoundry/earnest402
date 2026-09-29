import { describe, expect, it } from 'vitest'
import type { Agreement, Seat } from '../src/chain/decode'
import {
  PROMPT_REFRESH_INTERVAL_MS,
  REFRESH_INTERVAL_MS,
  initialState,
  isConfirmingSeat,
  poolPath,
  reduce,
  refreshInterval,
  shouldPoll,
  verdictOf,
  type Event,
  type State,
} from '../src/pool/machine'
import { verdictFor, type Circumstances, type SeatQuote } from '../src/pool/view'

const DEADLINE = 1789651281n
/** The wallet most of these tests connect. Every reading names the wallet it was made for. */
const P = 'P'.repeat(58)
const BEFORE = DEADLINE - 1n
const AFTER = DEADLINE

function agreement(overrides: Partial<Agreement> = {}): Agreement {
  return {
    condition: 0,
    state: 'OPEN',
    deadline: DEADLINE,
    sharePrice: 100000n,
    minSeats: 5,
    maxSeats: 5,
    seats: 1,
    refundCursor: 0,
    unclaimedSeats: 0,
    totalHeld: 100000n,
    commitSha256: 'fa'.repeat(32),
    beneficiary: 'B'.repeat(58),
    verifier: 'V'.repeat(58),
    creator: 'C'.repeat(58),
    ...overrides,
  }
}

const seatAt = (index: number, overrides: Partial<Seat> = {}): Seat => ({
  index,
  payer: 'P'.repeat(58),
  amount: 100000n,
  status: 'owed',
  ...overrides,
})

/** A released pool's edition link, as the release note records it. */
const LINK = { edition: '900', cid: 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i' }

const circumstances = (overrides: Partial<Circumstances> = {}): Circumstances => ({
  record: agreement(),
  seat: null,
  chainNow: BEFORE,
  canPayFee: true,
  ...overrides,
})

/**
 * One reading of the chain for each shape the verdict can take, so the
 * reducer is exercised against every action list `view.ts` can hand it rather
 * than against one convenient pool.
 */
const CHAIN = {
  openNoSeat: circumstances(),
  openWithSeat: circumstances({ seat: seatAt(0) }),
  // A running refund pass that skipped this buyer's seat: `expire` is gone
  // (already expired), `refund_next` and `claim_refund` are both live. Not an
  // EXPIRED record with the cursor moved -- the contract cannot produce one,
  // since the first `refund_next` is what moves the cursor and it leaves
  // EXPIRED in the same step.
  refundingSkipped: circumstances({
    record: agreement({ state: 'REFUNDING', seats: 5, refundCursor: 1, unclaimedSeats: 1 }),
    seat: seatAt(0),
    chainNow: AFTER,
  }),
  // The pass finished and nothing is outstanding, so `close` is the only
  // thing left to send.
  refundedClosable: circumstances({
    record: agreement({
      state: 'REFUNDED',
      seats: 5,
      refundCursor: 5,
      unclaimedSeats: 0,
      totalHeld: 0n,
    }),
    seat: seatAt(0, { amount: 0n, status: 'settled' }),
    chainNow: AFTER,
  }),
  // The pass finished, somebody else was skipped, and this buyer has their
  // money. Nothing left for them to do or to wait for.
  refundedSpent: circumstances({
    record: agreement({
      state: 'REFUNDED',
      seats: 5,
      refundCursor: 5,
      unclaimedSeats: 1,
      totalHeld: 100000n,
    }),
    seat: seatAt(0, { amount: 0n, status: 'settled' }),
    chainNow: AFTER,
  }),
  released: circumstances({ record: agreement({ state: 'RELEASED', seats: 5 }), edition: LINK }),
  gone: circumstances({ record: null, closedOutcome: 'released', edition: LINK }),
  // Holding USDC locks a minimum balance, so a buyer can arrive here unable
  // to pay a fee. The verdict withholds every action; the reducer must not
  // invent one back.
  refundingSkippedNoFee: circumstances({
    record: agreement({ state: 'REFUNDING', seats: 5, refundCursor: 1, unclaimedSeats: 1 }),
    seat: seatAt(0),
    chainNow: AFTER,
    canPayFee: false,
  }),
} as const

const quote = (overrides: Partial<SeatQuote> = {}): SeatQuote => ({
  agreementId: 25n,
  amount: 100000n,
  seatsTotal: 5,
  seatsLeft: 4,
  deadline: Number(DEADLINE),
  commitSha256: 'fa'.repeat(32),
  ...overrides,
})

/** Drive the machine through a list of events, returning the final state. */
function run(events: Event[], from: State = initialState()): State {
  return events.reduce((state, event) => reduce(state, event).state, from)
}

const effectsOf = (state: State, event: Event) =>
  reduce(state, event).effects.map((e) => e.type)

/** Connected, on a pool, with one quote in hand and the buy button live. */
const confirming = (): State =>
  run([
    { type: 'WALLET_CONNECTED', address: 'P'.repeat(58) },
    { type: 'QUOTE_REQUESTED' },
    { type: 'QUOTE_RECEIVED', quote: quote() },
  ])

describe('the resting shape', () => {
  it('starts idle, reading nothing, holding nothing', () => {
    const state = initialState()
    expect(state.purchase).toBe('IDLE')
    expect(state.reading).toBe(false)
    expect(state.agreementId).toBeUndefined()
    expect(state.circumstances).toBeUndefined()
  })

  it('has no verdict before the first read', () => {
    expect(verdictOf(initialState())).toBeNull()
  })

  it('reads a pool the moment one is opened', () => {
    expect(effectsOf(initialState(), { type: 'OPENED_POOL', agreementId: 25n })).toEqual([
      'READ_POOL',
    ])
  })
})

describe('never a second unpaid pass while one is in flight', () => {
  // A property of the reducer rather than of a disabled button: a double
  // click, a re-mount and a retry all arrive as the same event.
  const inFlight: State['purchase'][] = ['QUOTING', 'CHECKING', 'SETTLING']

  for (const purchase of inFlight) {
    it(`is inert while ${purchase}`, () => {
      const state: State = { ...initialState(), purchase }
      const result = reduce(state, { type: 'QUOTE_REQUESTED' })
      expect(result.effects).toEqual([])
      expect(result.state).toBe(state)
    })
  }

  it('quotes again from CONFIRM, which holds a quote and has nothing in flight', () => {
    // The confirm view's way out for a buyer with no wallet to buy with: the
    // pooled 402 parks nothing, so replacing the quote on request is free.
    const state = { ...confirming(), wallet: undefined }
    const result = reduce(state, { type: 'QUOTE_REQUESTED' })
    expect(result.effects.map((e) => e.type)).toEqual(['REQUEST_QUOTE'])
    expect(result.state.purchase).toBe('QUOTING')
    expect(result.state.quote).toBeUndefined()
  })

  it('quotes again after the route declined', () => {
    const declined = run(
      [
        { type: 'QUOTE_REQUESTED' },
        { type: 'QUOTE_REFUSED', reason: 'no_pool_open', message: 'nothing open' },
      ],
      initialState(),
    )
    expect(declined.purchase).toBe('UNAVAILABLE')
    expect(effectsOf(declined, { type: 'QUOTE_REQUESTED' })).toEqual(['REQUEST_QUOTE'])
  })

  it('quotes again after a settlement failed', () => {
    // The state a failed signature rests in has to be one a second attempt
    // can start from, or a declined wallet prompt is a dead end.
    const failed = run(
      [
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_VERIFIED' },
        { type: 'SETTLE_FAILED', error: 'the wallet declined' },
      ],
      confirming(),
    )
    expect(failed.purchase).toBe('IDLE')
    expect(failed.quote).toBeUndefined()
    expect(effectsOf(failed, { type: 'QUOTE_REQUESTED' })).toEqual(['REQUEST_QUOTE'])
  })

  it('quotes with no wallet connected, because a pooled 402 commits nothing', () => {
    // Pools are created out of band, so an unpaid pass parks no deposit and
    // names no buyer. Showing a price before a handshake costs nothing.
    expect(effectsOf(initialState(), { type: 'QUOTE_REQUESTED' })).toEqual([
      'REQUEST_QUOTE',
    ])
  })
})

describe('the link is minted before anything can fail', () => {
  it('navigates to the pool as soon as the 402 is read', () => {
    const { state, effects } = reduce(
      run([{ type: 'QUOTE_REQUESTED' }]),
      { type: 'QUOTE_RECEIVED', quote: quote({ agreementId: 26n }) },
    )
    // The URL first: it is the part that must survive whatever happens next.
    expect(effects).toEqual([
      { type: 'NAVIGATE', path: '/app/pool/26' },
      { type: 'READ_POOL', agreementId: 26n, address: null },
    ])
    expect(state.agreementId).toBe(26n)
    expect(state.purchase).toBe('CONFIRM')
  })

  it('does not read again for a quote on the pool already on screen', () => {
    const onPool = run([{ type: 'OPENED_POOL', agreementId: 25n }, { type: 'QUOTE_REQUESTED' }])
    expect(reduce(onPool, { type: 'QUOTE_RECEIVED', quote: quote() }).effects).toEqual([
      { type: 'NAVIGATE', path: '/app/pool/25' },
    ])
  })

  it('follows the quote when the open pool is not the one on screen', () => {
    // The pooled route sells whichever edition is open, so a buyer sitting on
    // a stale link may be quoted a different pool. That is not an error.
    const state = run([
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: null, circumstances: CHAIN.released },
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', quote: quote({ agreementId: 26n }) },
    ])
    expect(state.agreementId).toBe(26n)
    // And it takes nothing of pool 25 with it. The router's OPENED_POOL for
    // 26 arrives next and sees the id already set, so anything left here now
    // would stay on screen as pool 26's -- a released pool, in this case,
    // shown over one that is still selling seats.
    expect(state.circumstances).toBeUndefined()
    const routed = run([{ type: 'OPENED_POOL', agreementId: 26n }], state)
    expect(routed.circumstances).toBeUndefined()
  })

  it('still holds the link after the signature fails', () => {
    const abandoned = run(
      [
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_VERIFIED' },
        { type: 'SETTLE_FAILED', error: 'declined' },
      ],
      confirming(),
    )
    expect(abandoned.agreementId).toBe(25n)
    expect(poolPath(abandoned.agreementId!)).toBe('/app/pool/25')
  })

  it('ignores a quote that arrives after the buyer moved on', () => {
    const idle = initialState()
    expect(reduce(idle, { type: 'QUOTE_RECEIVED', quote: quote() }).state).toBe(idle)
  })
})

describe('the freshness check, which runs once and just before signing', () => {
  it('checks before it signs', () => {
    const state = confirming()
    const checking = reduce(state, { type: 'BUY_REQUESTED' })
    expect(checking.state.purchase).toBe('CHECKING')
    expect(checking.effects).toEqual([{ type: 'VERIFY_QUOTE', quote: quote() }])
    // And nothing is signed until the check comes back.
    expect(checking.effects.map((e) => e.type)).not.toContain('BUILD_AND_SIGN')
  })

  it('signs only after the check passed', () => {
    const checking = run([{ type: 'BUY_REQUESTED' }], confirming())
    const signing = reduce(checking, { type: 'QUOTE_VERIFIED' })
    expect(signing.state.purchase).toBe('SETTLING')
    expect(signing.effects).toEqual([{ type: 'BUILD_AND_SIGN', quote: quote() }])
  })

  it('refuses to sign a quote the pool has outgrown, and says why', () => {
    const stale = run(
      [
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_STALE', disagreements: ['seats moved: 4 offered, 0 free now.'] },
      ],
      confirming(),
    )
    expect(stale.purchase).toBe('IDLE')
    expect(stale.quote).toBeUndefined()
    expect(stale.purchaseError?.kind).toBe('quote_stale')
    expect(stale.purchaseError?.message).toContain('seats moved')
    expect(stale.purchaseError?.message).toContain('nothing was signed')
  })

  it('names what moved by the noun the check was made in', () => {
    const stale = (noun?: string) =>
      run(
        [
          { type: 'BUY_REQUESTED' },
          { type: 'QUOTE_STALE', disagreements: ['seats moved.'], ...(noun ? { noun } : {}) },
        ],
        confirming(),
      ).purchaseError?.message
    expect(stale()).toBe(
      'The pool moved while this quote was open, so nothing was signed. seats moved.',
    )
    expect(stale('pool')).toBe(stale())
    expect(stale('trip')).toBe(
      'The trip moved while this quote was open, so nothing was signed. seats moved.',
    )
  })

  it('does not report an unreachable node as a stale quote', () => {
    // Opposite meanings. "The pool moved" tells the buyer the edition is
    // gone; "we could not ask" tells them to try again, and the quote in hand
    // is still good as far as anyone knows.
    const unchecked = run(
      [
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_UNVERIFIABLE', error: 'could not read agreement 25' },
      ],
      confirming(),
    )
    expect(unchecked.purchase).toBe('CONFIRM')
    expect(unchecked.quote).toEqual(quote())
    expect(unchecked.purchaseError).toEqual({
      kind: 'quote_unverifiable',
      message: 'could not read agreement 25',
    })
  })

  it('will not start a check with no wallet to sign with', () => {
    const noWallet = run([
      { type: 'QUOTE_REQUESTED' },
      { type: 'QUOTE_RECEIVED', quote: quote() },
    ])
    expect(effectsOf(noWallet, { type: 'BUY_REQUESTED' })).toEqual([])
  })

  it('runs the check once, not once per attempt state', () => {
    // Two VERIFY_QUOTE effects for one purchase would mean two reads and two
    // chances for the answer to disagree with itself.
    const events: Event[] = [
      { type: 'BUY_REQUESTED' },
      { type: 'QUOTE_VERIFIED' },
    ]
    const emitted = events.reduce<{ state: State; seen: string[] }>(
      (acc, event) => {
        const { state, effects } = reduce(acc.state, event)
        return { state, seen: [...acc.seen, ...effects.map((e) => e.type)] }
      },
      { state: confirming(), seen: [] },
    )
    expect(emitted.seen.filter((type) => type === 'VERIFY_QUOTE')).toHaveLength(1)
  })
})

describe('a seat the node has not caught up to yet', () => {
  const settled = (): State =>
    run(
      [
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_VERIFIED' },
        {
          type: 'SETTLED',
          agreementId: 25n,
          seatsTotal: 5,
          deadline: Number(DEADLINE),
          commitSha256: 'fa'.repeat(32),
        },
      ],
      confirming(),
    )

  it('reads the pool the moment the 200 lands', () => {
    const { state, effects } = reduce(
      run([{ type: 'BUY_REQUESTED' }, { type: 'QUOTE_VERIFIED' }], confirming()),
      {
        type: 'SETTLED',
        agreementId: 25n,
        seatsTotal: 5,
        deadline: 1,
        commitSha256: 'fa'.repeat(32),
      },
    )
    expect(effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P }])
    expect(state.awaitingSeat).toBe(25n)
    expect(isConfirmingSeat(state)).toBe(true)
  })

  it('survives a read that shows no seat', () => {
    // The lie this prevents: "you hold no seat here", told to someone whose
    // USDC has already left their wallet.
    const stale = reduce(settled(), {
      type: 'READ_RECEIVED',
      agreementId: 25n,
      address: P,
      circumstances: CHAIN.openNoSeat,
    }).state
    expect(stale.awaitingSeat).toBe(25n)
    expect(isConfirmingSeat(stale)).toBe(true)
    expect(verdictFor(stale.circumstances!).seat).toBeNull()
  })

  it('clears once the seat appears', () => {
    const caught = run(
      [
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openNoSeat },
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openWithSeat },
      ],
      settled(),
    )
    expect(caught.awaitingSeat).toBeUndefined()
    expect(isConfirmingSeat(caught)).toBe(false)
  })

  it('is not cleared by a read of some other pool', () => {
    // The flag names an agreement rather than being a boolean precisely so a
    // buyer who navigates elsewhere mid-confirmation does not have it wiped
    // by a reading that says nothing about the pool they paid into.
    const elsewhere = run(
      [
        { type: 'OPENED_POOL', agreementId: 26n },
        { type: 'READ_RECEIVED', agreementId: 26n, address: P, circumstances: CHAIN.openWithSeat },
      ],
      settled(),
    )
    expect(elsewhere.awaitingSeat).toBe(25n)
    expect(isConfirmingSeat(elsewhere)).toBe(false)
  })

  describe('a buyer who left the pool while the wallet prompt was open', () => {
    // Brought back to what they paid for -- the screen and the address bar
    // together. A pool on screen that the URL does not name is undone by the
    // router's next OPENED_POOL for the URL, by a copied link, and by a reload
    // that forgets `awaitingSeat`.
    const signingThen = (elsewhere: Event[]): State =>
      run(
        [{ type: 'BUY_REQUESTED' }, { type: 'QUOTE_VERIFIED' }, ...elsewhere],
        confirming(),
      )
    const settledInto25: Event = {
      type: 'SETTLED',
      agreementId: 25n,
      seatsTotal: 5,
      deadline: 1,
      commitSha256: 'fa'.repeat(32),
    }
    const unreadableFor25: Event = {
      type: 'RECEIPT_UNREADABLE',
      agreementId: 25n,
      message: 'the payment settled but the response was not JSON',
    }

    const departures: [string, Event[]][] = [
      [
        'to another pool',
        [
          { type: 'OPENED_POOL', agreementId: 26n },
          { type: 'READ_RECEIVED', agreementId: 26n, address: P, circumstances: CHAIN.released },
        ],
      ],
      ['to the entry screen', [{ type: 'OPENED_ENTRY' }]],
    ]

    for (const [where, departure] of departures) {
      for (const outcome of [settledInto25, unreadableFor25]) {
        it(`moves the URL with the screen after ${outcome.type}, having gone ${where}`, () => {
          const { state, effects } = reduce(signingThen(departure), outcome)
          expect(state.agreementId).toBe(25n)
          expect(state.circumstances).toBeUndefined()
          expect(isConfirmingSeat(state)).toBe(true)
          // The URL first, as when the quote minted it.
          expect(effects).toEqual([
            { type: 'NAVIGATE', path: '/app/pool/25' },
            { type: 'READ_POOL', agreementId: 25n, address: P },
          ])
        })
      }
    }

    it('does not navigate when the paid pool is still the one on screen', () => {
      // QUOTE_RECEIVED already put this URL in the bar; navigating to it again
      // would only add a history entry that goes nowhere.
      expect(reduce(signingThen([]), settledInto25).effects).toEqual([
        { type: 'READ_POOL', agreementId: 25n, address: P },
      ])
    })
  })

  it('survives leaving the pool screen, because a payment is not a screen', () => {
    const away = run([{ type: 'OPENED_ENTRY' }], settled())
    expect(away.awaitingSeat).toBe(25n)
  })

  it('stops waiting once the boxes are gone, since no read can answer again', () => {
    // Holding it past this point polls forever against a question the chain
    // can no longer be asked -- the roster is deleted with the record.
    const closed = reduce(settled(), {
      type: 'READ_RECEIVED',
      agreementId: 25n,
      address: P,
      circumstances: CHAIN.gone,
    }).state
    expect(closed.awaitingSeat).toBeUndefined()
  })

  it('takes a 200 whatever the machine was doing when it arrived', () => {
    // The lesson the delivery machine records at its own SETTLED: money that
    // has moved cannot be made not to have moved by where the reducer is
    // standing. There is only one proof of payment and dropping it is fatal.
    const idle = initialState()
    const late = reduce(idle, {
      type: 'SETTLED',
      agreementId: 25n,
      seatsTotal: 5,
      deadline: 1,
      commitSha256: 'fa'.repeat(32),
    })
    expect(late.state.awaitingSeat).toBe(25n)
    // No pool was on screen, so the paid one arrives with its URL.
    expect(late.effects).toEqual([
      { type: 'NAVIGATE', path: '/app/pool/25' },
      { type: 'READ_POOL', agreementId: 25n, address: null },
    ])
  })

  it('treats an unreadable receipt as a payment, not as a failure', () => {
    const unreadable = reduce(
      run([{ type: 'BUY_REQUESTED' }, { type: 'QUOTE_VERIFIED' }], confirming()),
      {
        type: 'RECEIPT_UNREADABLE',
        agreementId: 25n,
        message: 'the payment settled but the response was not JSON',
      },
    )
    expect(unreadable.state.awaitingSeat).toBe(25n)
    expect(unreadable.state.purchaseError?.kind).toBe('receipt_unreadable')
    expect(unreadable.state.purchaseError?.message).toContain('settled')
    expect(unreadable.effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P }])
  })

  it('stops saying so once the chain shows the seat', () => {
    const resolved = run(
      [
        {
          type: 'RECEIPT_UNREADABLE',
          agreementId: 25n,
          message: 'the payment settled but the response was not JSON',
        },
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openWithSeat },
      ],
      confirming(),
    )
    expect(resolved.purchaseError).toBeUndefined()
  })
})

describe('the reducer defers to the verdict and never second-guesses it', () => {
  // `view.ts` owns every guard mirroring a contract assert, and
  // `view.test.ts` asserts them. What is asserted here is only that the
  // reducer adds nothing of its own -- a machine that re-derived the rules
  // would be a second copy of them, free to drift.
  const onPool = (chain: Circumstances): State =>
    run([
      { type: 'WALLET_CONNECTED', address: 'P'.repeat(58) },
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: chain },
    ])

  it('sends an action the verdict offered', () => {
    const state = onPool(CHAIN.refundingSkipped)
    expect(verdictFor(CHAIN.refundingSkipped).actions).toContain('refund_next')
    expect(reduce(state, { type: 'ACTION_REQUESTED', action: 'refund_next' })).toEqual({
      state: {
        ...state,
        sending: { action: 'refund_next', agreementId: 25n },
        actionError: undefined,
      },
      effects: [{ type: 'SEND_ACTION', action: 'refund_next', agreementId: 25n }],
    })
  })

  it('is inert for an action the verdict withheld', () => {
    // `close` on a pool that is not REFUNDED. No error and no state change:
    // the button should not have existed.
    const state = onPool(CHAIN.refundingSkipped)
    expect(reduce(state, { type: 'ACTION_REQUESTED', action: 'close' }).state).toBe(state)
  })

  it('offers nothing to a buyer who cannot pay a fee', () => {
    const state = onPool(CHAIN.refundingSkippedNoFee)
    for (const action of ['expire', 'refund_next', 'claim_refund', 'close'] as const) {
      expect(effectsOf(state, { type: 'ACTION_REQUESTED', action })).toEqual([])
    }
  })

  it('refuses a second call while one is in flight', () => {
    const sending = run(
      [{ type: 'ACTION_REQUESTED', action: 'refund_next' }],
      onPool(CHAIN.refundingSkipped),
    )
    expect(sending.sending).toEqual({ action: 'refund_next', agreementId: 25n })
    expect(effectsOf(sending, { type: 'ACTION_REQUESTED', action: 'claim_refund' })).toEqual(
      [],
    )
  })

  it('re-reads the pool once a call confirms', () => {
    const sent = reduce(
      run([{ type: 'ACTION_REQUESTED', action: 'refund_next' }], onPool(CHAIN.refundingSkipped)),
      { type: 'ACTION_SENT', action: 'refund_next', agreementId: 25n, txId: 'TX1' },
    )
    expect(sent.state.sending).toBeUndefined()
    expect(sent.state.lastTxId).toBe('TX1')
    expect(sent.effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P }])
  })

  it('re-reads the pool after a refusal, rather than offer the refused call again', () => {
    // The verdict that produced a refused call may be exactly why it was
    // refused. Left on screen, it goes on offering the same button.
    const { state, effects } = reduce(
      run([{ type: 'ACTION_REQUESTED', action: 'refund_next' }], onPool(CHAIN.refundingSkipped)),
      { type: 'ACTION_FAILED', action: 'refund_next', agreementId: 25n, error: 'rejected' },
    )
    expect(state.sending).toBeUndefined()
    expect(state.actionError).toBe('rejected')
    expect(effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P }])
  })

  it('cannot act on a pool it has not read', () => {
    const unread = run([{ type: 'OPENED_POOL', agreementId: 25n }])
    expect(effectsOf(unread, { type: 'ACTION_REQUESTED', action: 'expire' })).toEqual([])
  })
})

describe('the lock on a call in flight', () => {
  // A call is not a property of the screen. It keeps travelling when the
  // buyer opens another pool, and it may still land after the wait for it
  // has run out -- so the lock is released only by a completion naming the
  // same call on the same pool, or by a fresh reading of a pool whose call
  // could not be confirmed.
  const on = (agreementId: bigint, chain: Circumstances): Event[] => [
    { type: 'OPENED_POOL', agreementId },
    { type: 'READ_RECEIVED', agreementId, address: P, circumstances: chain },
  ]
  const sendingOn25 = (action: 'refund_next' | 'claim_refund' = 'refund_next'): State =>
    run([
      { type: 'WALLET_CONNECTED', address: P },
      ...on(25n, CHAIN.refundingSkipped),
      { type: 'ACTION_REQUESTED', action },
    ])

  describe('a completion is matched on the call and the pool together', () => {
    it("does not let one pool's confirmation release another pool's call", () => {
      // The review's case: pool 26's refund_next is in flight, and a
      // confirmation for pool 25's refund_next arrives. The action names are
      // identical; only the pool tells them apart.
      const sendingOn26 = run([
        { type: 'WALLET_CONNECTED', address: P },
        ...on(26n, CHAIN.refundingSkipped),
        { type: 'ACTION_REQUESTED', action: 'refund_next' },
      ])
      const late = reduce(sendingOn26, {
        type: 'ACTION_SENT',
        action: 'refund_next',
        agreementId: 25n,
        txId: 'TX25',
      })
      expect(late.state).toBe(sendingOn26)
      expect(late.effects).toEqual([])
      expect(late.state.sending).toEqual({ action: 'refund_next', agreementId: 26n })
      expect(late.state.lastTxId).toBeUndefined()
    })

    it('ignores a completion for a different call on the same pool', () => {
      const state = sendingOn25('claim_refund')
      for (const completion of [
        { type: 'ACTION_SENT', action: 'refund_next', agreementId: 25n, txId: 'X' },
        { type: 'ACTION_FAILED', action: 'refund_next', agreementId: 25n, error: 'x' },
        { type: 'ACTION_UNCONFIRMED', action: 'refund_next', agreementId: 25n, txId: 'X' },
      ] as Event[]) {
        expect(reduce(state, completion).state).toBe(state)
      }
    })

    it('ignores a completion when nothing is in flight', () => {
      const idle = run([{ type: 'WALLET_CONNECTED', address: P }, ...on(25n, CHAIN.refundingSkipped)])
      const stray = reduce(idle, {
        type: 'ACTION_SENT',
        action: 'refund_next',
        agreementId: 25n,
        txId: 'TX1',
      })
      expect(stray.state).toBe(idle)
      expect(stray.effects).toEqual([])
    })
  })

  describe('a buyer who leaves the pool and comes back', () => {
    it('is not offered the call they just sent', () => {
      // The review's case: send claim_refund on 25, open 26, reopen 25, and a
      // reading of 25 lands before the confirmation -- still showing the seat
      // owed, so the verdict offers claim_refund again.
      const back = run(
        [...on(26n, CHAIN.openNoSeat), ...on(25n, CHAIN.refundingSkipped)],
        sendingOn25('claim_refund'),
      )
      expect(verdictFor(back.circumstances!).actions).toContain('claim_refund')
      expect(back.sending).toEqual({ action: 'claim_refund', agreementId: 25n })
      expect(effectsOf(back, { type: 'ACTION_REQUESTED', action: 'claim_refund' })).toEqual([])
    })

    it('shows nothing of the call on the other pool, and releases it when it confirms', () => {
      const away = run(on(26n, CHAIN.openNoSeat), sendingOn25())
      const confirmed = reduce(away, {
        type: 'ACTION_SENT',
        action: 'refund_next',
        agreementId: 25n,
        txId: 'TX25',
      })
      expect(confirmed.state.sending).toBeUndefined()
      expect(confirmed.state.lastTxId).toBeUndefined()
      // A reading of 25 would be discarded on arrival; 25 is read when opened.
      expect(confirmed.effects).toEqual([])
    })

    it('shows nothing of a refusal on the other pool, and releases the call', () => {
      const away = run(on(26n, CHAIN.openNoSeat), sendingOn25())
      const refused = reduce(away, {
        type: 'ACTION_FAILED',
        action: 'refund_next',
        agreementId: 25n,
        error: 'rejected',
      })
      expect(refused.state.sending).toBeUndefined()
      expect(refused.state.actionError).toBeUndefined()
      expect(refused.effects).toEqual([])
    })
  })

  describe('a call whose confirmation never came', () => {
    const CALL = { txId: 'TX25', lastValid: 1120n }
    const timedOut: Event = {
      type: 'ACTION_UNCONFIRMED',
      action: 'refund_next',
      agreementId: 25n,
      ...CALL,
    }
    /** A reading of pool 25 that already shows what became of the call. */
    const final = (circumstances: Circumstances): Event => ({
      type: 'READ_RECEIVED',
      agreementId: 25n,
      address: P,
      circumstances,
      finalFor: 'TX25',
    })

    it('keeps the lock, shows the id, and reads the pool, asking it to check the call', () => {
      const { state, effects } = reduce(sendingOn25(), timedOut)
      expect(state.sending).toEqual({
        action: 'refund_next',
        agreementId: 25n,
        unconfirmed: CALL,
      })
      expect(state.lastTxId).toBe('TX25')
      expect(state.actionError).toBeUndefined()
      expect(effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P, call: CALL }])
      // The stale verdict still offers refund_next; the lock is what stops it.
      expect(effectsOf(state, { type: 'ACTION_REQUESTED', action: 'refund_next' })).toEqual([])
    })

    it('releases the lock on a reading that shows what became of the call', () => {
      const read = run([timedOut, final(CHAIN.refundingSkipped)], sendingOn25())
      expect(read.sending).toBeUndefined()
      expect(effectsOf(read, { type: 'ACTION_REQUESTED', action: 'refund_next' })).toEqual([
        'SEND_ACTION',
      ])
    })

    it('holds the lock through a reading of that pool that cannot yet tell', () => {
      // Read before the call was committed, the pool looks as it did, and the
      // verdict offers refund_next again. Releasing here is what put a second
      // call in front of a buyer whose first was still travelling.
      const early = run(
        [
          timedOut,
          { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.refundingSkipped },
        ],
        sendingOn25(),
      )
      expect(early.sending?.unconfirmed).toEqual(CALL)
      expect(early.circumstances).toBe(CHAIN.refundingSkipped)
      expect(effectsOf(early, { type: 'ACTION_REQUESTED', action: 'refund_next' })).toEqual([])
    })

    it('is not released by a reading that is final for some other call', () => {
      const other = run(
        [timedOut, { ...final(CHAIN.refundingSkipped), finalFor: 'TX-EARLIER' } as Event],
        sendingOn25(),
      )
      expect(other.sending?.unconfirmed).toEqual(CALL)
    })

    it('holds the lock through a reading that failed', () => {
      const failed = run(
        [timedOut, { type: 'READ_FAILED', agreementId: 25n, address: P, kind: 'unreachable', error: 'boom' }],
        sendingOn25(),
      )
      expect(failed.sending?.unconfirmed).toEqual(CALL)
    })

    it('is not released by a reading of some other pool', () => {
      const other = run(
        [
          timedOut,
          {
            type: 'READ_RECEIVED',
            agreementId: 26n,
            address: P,
            circumstances: CHAIN.openNoSeat,
            finalFor: 'TX25',
          },
        ],
        sendingOn25(),
      )
      expect(other.sending?.unconfirmed).toEqual(CALL)
    })

    it('keeps the screen refreshing, even over a pool that looks finished', () => {
      const onReleased = run(
        [timedOut, { type: 'READ_RECEIVED', agreementId: 26n, address: P, circumstances: CHAIN.released }],
        run(on(26n, CHAIN.released), sendingOn25()),
      )
      expect(onReleased.sending?.unconfirmed).toEqual(CALL)
      expect(shouldPoll(onReleased)).toBe(true)
    })

    describe('when the buyer has moved to another pool', () => {
      const away = (): State => run(on(26n, CHAIN.openNoSeat), sendingOn25())

      it('asks for the timed-out pool as well as the one on screen', () => {
        const { state, effects } = reduce(away(), timedOut)
        expect(effects).toEqual([
          { type: 'READ_POOL', agreementId: 26n, address: P },
          { type: 'READ_POOL', agreementId: 25n, address: P, call: CALL },
        ])
        // Nothing about the call lands on 26's screen.
        expect(state.lastTxId).toBeUndefined()
      })

      it('releases the lock when a final reading lands, and leaves the screen alone', () => {
        const before = run([timedOut], away())
        const after = reduce(before, final(CHAIN.released)).state
        expect(after.sending).toBeUndefined()
        expect(after.circumstances).toBe(before.circumstances)
        expect(after.agreementId).toBe(26n)
      })

      it('asks again on the next refresh if that reading failed', () => {
        // Otherwise the lock -- which covers every pool -- would last until
        // the buyer happened to reopen pool 25.
        const failed = run(
          [timedOut, { type: 'READ_FAILED', agreementId: 25n, address: P, kind: 'unreachable', error: 'boom' }],
          away(),
        )
        expect(reduce(failed, { type: 'READ_REQUESTED' }).effects).toEqual([
          { type: 'READ_POOL', agreementId: 26n, address: P },
          { type: 'READ_POOL', agreementId: 25n, address: P, call: CALL },
        ])
      })
    })
  })
})

describe('reads, and what a failed one must not do', () => {
  const read = (chain: Circumstances): State =>
    run([
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: null, circumstances: chain },
    ])

  it('keeps the last good reading when a read fails', () => {
    // A node that did not answer must not blank a screen that was correct a
    // moment ago -- above all not a screen saying money is owed.
    const failed = run(
      [{ type: 'READ_FAILED', agreementId: 25n, address: null, kind: 'unreachable', error: 'could not read agreement 25' }],
      read(CHAIN.refundingSkipped),
    )
    expect(failed.circumstances).toBe(CHAIN.refundingSkipped)
    expect(failed.readError).toEqual({
      kind: 'unreachable',
      message: 'could not read agreement 25',
    })
    expect(failed.reading).toBe(false)
  })

  it('keeps refreshing after a failure worth retrying, and stops after one that is not', () => {
    // A node that did not answer may answer next time. A build pointed at
    // another network will fail the same way on every tick, and a timer that
    // kept asking would be load on a public node and nothing else.
    const opened = run([{ type: 'OPENED_POOL', agreementId: 25n }])
    const transient = run(
      [{ type: 'READ_FAILED', agreementId: 25n, address: null, kind: 'unreachable', error: 'boom' }],
      opened,
    )
    expect(shouldPoll(transient)).toBe(true)
    const wrongChain = run(
      [
        {
          type: 'READ_FAILED',
          agreementId: 25n,
          address: null,
          kind: 'incompatible',
          error: 'this client is built for testnet',
        },
      ],
      opened,
    )
    expect(wrongChain.readError?.kind).toBe('incompatible')
    expect(shouldPoll(wrongChain)).toBe(false)
  })

  it('clears the error once a read answers', () => {
    const recovered = run(
      [
        { type: 'READ_FAILED', agreementId: 25n, address: null, kind: 'unreachable', error: 'boom' },
        { type: 'READ_RECEIVED', agreementId: 25n, address: null, circumstances: CHAIN.openNoSeat },
      ],
      read(CHAIN.openNoSeat),
    )
    expect(recovered.readError).toBeUndefined()
  })

  it('drops a reading that outlived the screen it was started for', () => {
    const away = reduce(run([{ type: 'OPENED_ENTRY' }], read(CHAIN.openNoSeat)), {
      type: 'READ_RECEIVED',
      agreementId: 25n,
      address: null,
      circumstances: CHAIN.released,
    }).state
    expect(away.circumstances).toBeUndefined()
    expect(away.reading).toBe(false)
  })

  it('drops a reading of a pool the buyer has moved away from', () => {
    // The race that makes the id on the event necessary. The read for 25 was
    // in flight when 26 was opened; it lands after. Stored, it would put a
    // released pool on 26's screen -- and a released pool is terminal, so the
    // refresh would stop and it would stay there.
    const moved = run([
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'OPENED_POOL', agreementId: 26n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: null, circumstances: CHAIN.released },
    ])
    expect(moved.circumstances).toBeUndefined()
    expect(moved.reading).toBe(true)
    expect(shouldPoll(moved)).toBe(true)
  })

  it('drops a failure reading a pool the buyer has moved away from', () => {
    const moved = run([
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'OPENED_POOL', agreementId: 26n },
      { type: 'READ_FAILED', agreementId: 25n, address: null, kind: 'unreachable', error: 'boom' },
    ])
    expect(moved.readError).toBeUndefined()
  })

  it('keeps the reading when the wallet layer repeats the same session', () => {
    const state = run([
      { type: 'WALLET_CONNECTED', address: 'P'.repeat(58) },
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openWithSeat },
    ])
    const repeated = reduce(state, { type: 'WALLET_CONNECTED', address: 'P'.repeat(58) })
    expect(repeated.state).toBe(state)
    expect(repeated.effects).toEqual([])
  })

  it('does nothing on a disconnect with no wallet connected', () => {
    const state = run([
      { type: 'OPENED_POOL', agreementId: 25n },
      { type: 'READ_RECEIVED', agreementId: 25n, address: null, circumstances: CHAIN.openNoSeat },
    ])
    expect(reduce(state, { type: 'WALLET_DISCONNECTED' }).state).toBe(state)
  })

  it('forgets the previous pool entirely when a different one is opened', () => {
    const sent = run(
      [
        { type: 'ACTION_REQUESTED', action: 'refund_next' },
        { type: 'ACTION_SENT', action: 'refund_next', agreementId: 25n, txId: 'TX1' },
      ],
      read(CHAIN.refundingSkipped),
    )
    expect(sent.lastTxId).toBe('TX1')
    const moved = run([{ type: 'OPENED_POOL', agreementId: 26n }], sent)
    expect(moved.circumstances).toBeUndefined()
    expect(moved.lastTxId).toBeUndefined()
    expect(moved.readError).toBeUndefined()
  })

  it('keeps what it has when the same pool is re-opened', () => {
    // A remount must not blink the screen back to "reading…".
    const remounted = run([{ type: 'OPENED_POOL', agreementId: 25n }], read(CHAIN.openNoSeat))
    expect(remounted.circumstances).toBe(CHAIN.openNoSeat)
    expect(remounted.reading).toBe(true)
  })

  it('re-reads when the wallet changes, because the seat belongs to a wallet', () => {
    // Two thirds of a reading are about a wallet: which seat is mine, and can
    // I pay a fee. Keeping them across a wallet change shows a stranger's
    // seat as this buyer's.
    const swapped = reduce(read(CHAIN.openWithSeat), {
      type: 'WALLET_CONNECTED',
      address: 'Q'.repeat(58),
    })
    expect(swapped.state.circumstances).toBeUndefined()
    expect(swapped.effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: 'Q'.repeat(58) }])
  })

  it('drops the seat, but not the quote, when the wallet disconnects', () => {
    // The pooled 402 never names a buyer, so the quote is still payable by
    // whichever wallet connects next.
    const disconnected = reduce(
      run([{ type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openWithSeat }], confirming()),
      { type: 'WALLET_DISCONNECTED' },
    )
    expect(disconnected.state.circumstances).toBeUndefined()
    expect(disconnected.state.quote).toEqual(quote())
    expect(disconnected.state.wallet).toBeUndefined()
  })
})

describe('a reading belongs to the wallet it was made for', () => {
  const Q = 'Q'.repeat(58)
  // A finished refund pass that skipped this wallet's seat: owed, and the
  // claim is the one thing left to send.
  const refundedOwed = circumstances({
    record: agreement({
      state: 'REFUNDED',
      seats: 5,
      refundCursor: 5,
      unclaimedSeats: 1,
      totalHeld: 100000n,
    }),
    seat: seatAt(0, { payer: P }),
    chainNow: AFTER,
  })
  // The same pool read with no wallet: no seat, no fee, nothing offered --
  // which on a finished pass is a terminal verdict.
  const refundedWalletless = circumstances({
    record: refundedOwed.record,
    seat: null,
    chainNow: AFTER,
    canPayFee: false,
  })

  /** Opened with no wallet, then connected: one reading asked for each. */
  const connectedAfterOpening = (): State => {
    const opened = reduce(initialState(), { type: 'OPENED_POOL', agreementId: 25n })
    expect(opened.effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: null }])
    const connected = reduce(opened.state, { type: 'WALLET_CONNECTED', address: P })
    expect(connected.effects).toEqual([{ type: 'READ_POOL', agreementId: 25n, address: P }])
    return connected.state
  }

  it("drops a walletless reading that lands after the connected wallet's own", () => {
    // The race a buyer returning from a wallet app hits: the tab becomes
    // visible and reads with no wallet, the wallet connects and reads again,
    // and the walletless reading is the slower one.
    const walletlessOnScreen: State = {
      ...initialState(),
      agreementId: 25n,
      circumstances: refundedWalletless,
    }
    expect(shouldPoll(walletlessOnScreen)).toBe(false)

    const state = run(
      [
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: refundedOwed },
        {
          type: 'READ_RECEIVED',
          agreementId: 25n,
          address: null,
          circumstances: refundedWalletless,
        },
      ],
      connectedAfterOpening(),
    )
    expect(state.circumstances).toBe(refundedOwed)
    expect(verdictFor(state.circumstances!).seat).not.toBeNull()
    // Stored, the walletless verdict is terminal: the refresh would stop and
    // the claim would not come back until a reload.
    expect(shouldPoll(state)).toBe(true)
    expect(effectsOf(state, { type: 'ACTION_REQUESTED', action: 'claim_refund' })).toEqual([
      'SEND_ACTION',
    ])
  })

  it("keeps reading until the connected wallet's own reading lands", () => {
    // Dropping the stale reading must not strand `reading`: the wallet change
    // asked for its own, and that one is what settles the flag.
    const connected = connectedAfterOpening()
    const stale = reduce(connected, {
      type: 'READ_RECEIVED',
      agreementId: 25n,
      address: null,
      circumstances: refundedWalletless,
    })
    expect(stale.state).toBe(connected)
    expect(stale.effects).toEqual([])
    expect(stale.state.reading).toBe(true)
    const fresh = run(
      [{ type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: refundedOwed }],
      stale.state,
    )
    expect(fresh.reading).toBe(false)
    expect(fresh.circumstances).toBe(refundedOwed)
  })

  it("does not put a previous wallet's failed reading on screen", () => {
    const connected = connectedAfterOpening()
    const failed = reduce(connected, {
      type: 'READ_FAILED',
      agreementId: 25n,
      address: null,
      kind: 'unreachable',
      error: 'boom',
    }).state
    expect(failed).toBe(connected)
    expect(failed.readError).toBeUndefined()
    expect(failed.reading).toBe(true)
  })

  describe('a call that may still land, when the wallet changes', () => {
    const unconfirmedOn25 = (): State =>
      run([
        { type: 'WALLET_CONNECTED', address: P },
        { type: 'OPENED_POOL', agreementId: 25n },
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.refundingSkipped },
        { type: 'ACTION_REQUESTED', action: 'refund_next' },
        {
          type: 'ACTION_UNCONFIRMED',
          action: 'refund_next',
          agreementId: 25n,
          txId: 'TX25',
          lastValid: 1120n,
        },
      ])
    const CALL = { txId: 'TX25', lastValid: 1120n }

    it("is released by the reading the change asked for, not the previous wallet's", () => {
      const swapped = reduce(unconfirmedOn25(), { type: 'WALLET_CONNECTED', address: Q })
      expect(swapped.effects).toEqual([
        { type: 'READ_POOL', agreementId: 25n, address: Q, call: CALL },
      ])

      const stale = run(
        [
          {
            type: 'READ_RECEIVED',
            agreementId: 25n,
            address: P,
            circumstances: CHAIN.refundingSkipped,
            finalFor: 'TX25',
          },
        ],
        swapped.state,
      )
      expect(stale.sending?.unconfirmed).toEqual(CALL)

      const fresh = run(
        [
          {
            type: 'READ_RECEIVED',
            agreementId: 25n,
            address: Q,
            circumstances: CHAIN.refundingSkipped,
            finalFor: 'TX25',
          },
        ],
        stale,
      )
      expect(fresh.sending).toBeUndefined()
    })

    it("asks again for the call's pool too, for the new wallet, when the buyer is elsewhere", () => {
      // Off screen, the reading that releases the lock is asked for only by
      // `readPool`. A wallet change that asked for the screen's pool alone
      // would leave the lock -- which covers every pool -- waiting on a
      // reading that is now dropped on arrival.
      const away = run(
        [
          { type: 'OPENED_POOL', agreementId: 26n },
          { type: 'READ_RECEIVED', agreementId: 26n, address: P, circumstances: CHAIN.openNoSeat },
        ],
        unconfirmedOn25(),
      )
      expect(away.sending?.unconfirmed).toEqual(CALL)
      expect(reduce(away, { type: 'WALLET_CONNECTED', address: Q }).effects).toEqual([
        { type: 'READ_POOL', agreementId: 26n, address: Q },
        { type: 'READ_POOL', agreementId: 25n, address: Q, call: CALL },
      ])
      expect(reduce(away, { type: 'WALLET_DISCONNECTED' }).effects).toEqual([
        { type: 'READ_POOL', agreementId: 26n, address: null },
        { type: 'READ_POOL', agreementId: 25n, address: null, call: CALL },
      ])
    })
  })

  describe('a search for seats that outlives its wallet', () => {
    const searching = (): State =>
      run([{ type: 'WALLET_CONNECTED', address: P }, { type: 'MY_POOLS_REQUESTED' }])

    it('stops counting as in flight when the wallet changes', () => {
      // Left set, "find my seats" would stay disabled: the answer it waits
      // for is dropped on arrival.
      expect(searching().findingMyPools).toBe(true)
      expect(run([{ type: 'WALLET_CONNECTED', address: Q }], searching()).findingMyPools).toBe(
        false,
      )
      expect(run([{ type: 'WALLET_DISCONNECTED' }], searching()).findingMyPools).toBe(false)
    })

    it("is not listed as the next wallet's seats when it lands", () => {
      const swapped = run([{ type: 'WALLET_CONNECTED', address: Q }], searching())
      const found = reduce(swapped, { type: 'MY_POOLS_FOUND', address: P, agreementIds: [25n] })
      expect(found.state).toBe(swapped)
      const failed = reduce(swapped, { type: 'MY_POOLS_UNAVAILABLE', address: P, error: 'boom' })
      expect(failed.state).toBe(swapped)
    })
  })
})

describe('a purchase message says where it came from', () => {
  // Two of these are opposite news, so the screen reads the kind rather than
  // guessing from the text or from some other field.
  const signing = (): State =>
    run([{ type: 'BUY_REQUESTED' }, { type: 'QUOTE_VERIFIED' }], confirming())

  it('names each origin', () => {
    expect(
      run([{ type: 'QUOTE_REQUESTED' }, { type: 'QUOTE_FAILED', error: 'boom' }]).purchaseError,
    ).toEqual({ kind: 'quote_failed', message: 'boom' })
    expect(run([{ type: 'SETTLE_FAILED', error: 'declined' }], signing()).purchaseError).toEqual({
      kind: 'settle_failed',
      message: 'declined',
    })
    expect(
      run([{ type: 'RECEIPT_UNREADABLE', agreementId: 25n, message: 'not JSON' }], signing())
        .purchaseError,
    ).toEqual({ kind: 'receipt_unreadable', message: 'not JSON' })
  })

  it("keeps an unreadable receipt's kind when the boxes go instead of the seat appearing", () => {
    // `awaitingSeat` clears here, since no reading can show the seat any more.
    // The message must still say the payment went through, and its kind is
    // what says so.
    const gone = run(
      [
        { type: 'RECEIPT_UNREADABLE', agreementId: 25n, message: 'not JSON' },
        { type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.gone },
      ],
      signing(),
    )
    expect(gone.awaitingSeat).toBeUndefined()
    expect(gone.purchaseError).toEqual({ kind: 'receipt_unreadable', message: 'not JSON' })
  })

  it("keeps a later attempt's failure when the earlier payment's seat appears", () => {
    // Paid into 25, then quoted and bought again while that seat was still
    // awaited, and the second attempt was refused. The seat appearing answers
    // the first payment and says nothing about the second attempt.
    const retried = run(
      [
        {
          type: 'SETTLED',
          agreementId: 25n,
          seatsTotal: 5,
          deadline: 1,
          commitSha256: 'fa'.repeat(32),
        },
        { type: 'QUOTE_REQUESTED' },
        { type: 'QUOTE_RECEIVED', quote: quote() },
        { type: 'BUY_REQUESTED' },
        { type: 'QUOTE_VERIFIED' },
        { type: 'SETTLE_FAILED', error: 'one seat per address' },
      ],
      signing(),
    )
    expect(retried.awaitingSeat).toBe(25n)
    expect(retried.purchaseError?.kind).toBe('settle_failed')
    const seen = run(
      [{ type: 'READ_RECEIVED', agreementId: 25n, address: P, circumstances: CHAIN.openWithSeat }],
      retried,
    )
    expect(seen.awaitingSeat).toBeUndefined()
    expect(seen.purchaseError).toEqual({ kind: 'settle_failed', message: 'one seat per address' })
  })
})

describe('the fallback for a buyer who arrived without their link', () => {
  const connected = (): State =>
    run([{ type: 'WALLET_CONNECTED', address: 'P'.repeat(58) }])

  it('searches the wallet that is connected', () => {
    expect(reduce(connected(), { type: 'MY_POOLS_REQUESTED' }).effects).toEqual([
      { type: 'FIND_MY_POOLS', address: 'P'.repeat(58) },
    ])
  })

  it('has nothing to search with no wallet', () => {
    expect(effectsOf(initialState(), { type: 'MY_POOLS_REQUESTED' })).toEqual([])
  })

  it('keeps what it found when the indexer stops answering', () => {
    // A public indexer is a third-party host. Losing it costs this fallback
    // and nothing else, and it must not erase an answer already given.
    const degraded = run(
      [
        { type: 'MY_POOLS_REQUESTED' },
        { type: 'MY_POOLS_FOUND', address: P, agreementIds: [25n] },
        { type: 'MY_POOLS_REQUESTED' },
        { type: 'MY_POOLS_UNAVAILABLE', address: P, error: 'indexer unreachable' },
      ],
      connected(),
    )
    expect(degraded.myPools).toEqual([{ agreementId: 25n }])
    expect(degraded.myPoolsError).toContain('unreachable')
    expect(degraded.findingMyPools).toBe(false)
  })

  it('forgets the previous wallet s pools when another connects', () => {
    const swapped = run(
      [
        { type: 'MY_POOLS_FOUND', address: P, agreementIds: [25n] },
        { type: 'WALLET_CONNECTED', address: 'Q'.repeat(58) },
      ],
      connected(),
    )
    expect(swapped.myPools).toBeUndefined()
  })
})

describe('the refresh predicate, which is not an effect', () => {
  it('waits half a minute, which is ample for a pool that fills over hours', () => {
    expect(REFRESH_INTERVAL_MS).toBe(30_000)
  })

  const on = (chain?: Circumstances): State => ({
    ...initialState(),
    agreementId: 25n,
    circumstances: chain,
  })

  it('polls nothing on the entry screen', () => {
    expect(shouldPoll({ ...initialState(), circumstances: CHAIN.openNoSeat })).toBe(false)
  })

  it('polls a pool it has not read yet, which is what covers the mount', () => {
    expect(shouldPoll(on())).toBe(true)
  })

  // Enumerated rather than computed from the implementation: a test that
  // re-derived "terminal" would agree with a wrong definition of it.
  const cases: [string, Circumstances, boolean][] = [
    ['an open pool', CHAIN.openNoSeat, true],
    ['an open pool this buyer holds a seat in', CHAIN.openWithSeat, true],
    ['a running refund pass that skipped this buyer', CHAIN.refundingSkipped, true],
    ['a refunded pool that can still be closed', CHAIN.refundedClosable, true],
    ['a refunded pool with nothing left for this buyer', CHAIN.refundedSpent, false],
    ['a released pool', CHAIN.released, false],
    ['a pool whose boxes are gone', CHAIN.gone, false],
    // The link is the one thing a released pool still has to say, and the
    // indexer that says it trails the node that shows the release. Stopping
    // on the first reading would leave a buyer who watched the release land
    // looking at "cannot be read right now" until they reload.
    [
      'a released pool whose edition link has not been read',
      { ...CHAIN.released, edition: undefined },
      true,
    ],
    ['a released pool with no release note yet', { ...CHAIN.released, edition: null }, true],
    [
      'a closed released pool whose edition link has not been read',
      { ...CHAIN.gone, edition: undefined },
      true,
    ],
    [
      'a closed pool the history could not place',
      circumstances({ record: null, closedOutcome: 'unknown' }),
      false,
    ],
    ['a closed pool that was refunded', circumstances({ record: null, closedOutcome: 'refunded' }), false],
  ]

  for (const [what, chain, expected] of cases) {
    it(`${expected ? 'keeps polling' : 'stops polling'} ${what}`, () => {
      expect(shouldPoll(on(chain))).toBe(expected)
    })
  }

  it('keeps polling a terminal-looking pool while a seat is still expected', () => {
    // The 200 is authoritative and the node is behind it. Stopping here would
    // freeze the screen on the stale reading that prompted the wait.
    expect(shouldPoll({ ...on(CHAIN.released), awaitingSeat: 25n })).toBe(true)
  })

  it('reads again within seconds while a seat paid for on this pool has not appeared', () => {
    // The node is usually a round or so behind the settlement. At the normal
    // rate a buyer who has just paid watches "confirming" for half a minute.
    expect(refreshInterval(on(CHAIN.openNoSeat))).toBe(REFRESH_INTERVAL_MS)
    expect(refreshInterval({ ...on(CHAIN.openNoSeat), awaitingSeat: 25n })).toBe(
      PROMPT_REFRESH_INTERVAL_MS,
    )
    expect(PROMPT_REFRESH_INTERVAL_MS).toBeLessThan(REFRESH_INTERVAL_MS)
  })

  it('does not hurry for a seat awaited on some other pool', () => {
    // The refresh reads the pool on screen, so reading it faster finds the
    // other pool's seat no sooner.
    expect(refreshInterval({ ...on(CHAIN.openNoSeat), awaitingSeat: 26n })).toBe(
      REFRESH_INTERVAL_MS,
    )
  })

  it('reads again within seconds while a call may still land', () => {
    const sending = {
      action: 'refund_next' as const,
      agreementId: 26n,
      unconfirmed: { txId: 'TX', lastValid: 1120n },
    }
    expect(refreshInterval({ ...on(CHAIN.released), sending })).toBe(PROMPT_REFRESH_INTERVAL_MS)
  })

  it('has no interval at all where there is nothing to poll', () => {
    expect(refreshInterval({ ...initialState(), circumstances: CHAIN.openNoSeat })).toBeNull()
    expect(refreshInterval(on(CHAIN.released))).toBeNull()
  })

  it('keeps polling a refunded pool that still owes this buyer', () => {
    // REFUNDED means the cursor finished its pass, not that everyone has
    // their money.
    const owed = circumstances({
      record: agreement({
        state: 'REFUNDED',
        seats: 5,
        refundCursor: 5,
        unclaimedSeats: 1,
        totalHeld: 100000n,
      }),
      seat: seatAt(0),
      chainNow: AFTER,
    })
    expect(verdictFor(owed).owed).toBe(true)
    expect(shouldPoll(on(owed))).toBe(true)
  })
})

describe('exhaustively', () => {
  const everyEvent: Event[] = [
    { type: 'WALLET_CONNECTED', address: 'P'.repeat(58) },
    { type: 'WALLET_DISCONNECTED' },
    { type: 'OPENED_ENTRY' },
    { type: 'OPENED_POOL', agreementId: 25n },
    { type: 'OPENED_POOL', agreementId: 26n },
    { type: 'QUOTE_REQUESTED' },
    { type: 'QUOTE_RECEIVED', quote: quote() },
    { type: 'QUOTE_REFUSED', reason: 'no_pool_open', message: 'no edition is open' },
    { type: 'QUOTE_FAILED', error: 'boom' },
    { type: 'BUY_REQUESTED' },
    { type: 'QUOTE_VERIFIED' },
    { type: 'QUOTE_STALE', disagreements: ['seats moved.'] },
    { type: 'QUOTE_UNVERIFIABLE', error: 'boom' },
    {
      type: 'SETTLED',
      agreementId: 25n,
      seatsTotal: 5,
      deadline: 1,
      commitSha256: 'fa'.repeat(32),
    },
    { type: 'RECEIPT_UNREADABLE', agreementId: 25n, message: 'unreadable' },
    { type: 'SETTLE_FAILED', error: 'boom' },
    { type: 'READ_REQUESTED' },
    // Every reading twice: once for the wallet the walk connects and once for
    // no wallet. Each state therefore meets readings made for its own wallet,
    // which it stores, and readings made for the other, which it must drop.
    ...[P, null].flatMap((address) => [
      ...Object.values(CHAIN).map(
        (chain): Event => ({ type: 'READ_RECEIVED', agreementId: 25n, address, circumstances: chain }),
      ),
      // Reads that land after the screen moved on, including a terminal one.
      { type: 'READ_RECEIVED', agreementId: 26n, address, circumstances: CHAIN.refundingSkipped },
      { type: 'READ_RECEIVED', agreementId: 26n, address, circumstances: CHAIN.released },
      // Readings that show what became of the walk's unconfirmed call, for its
      // pool and for another, and one final for a call that is not the one
      // in flight.
      { type: 'READ_RECEIVED', agreementId: 25n, address, circumstances: CHAIN.refundingSkipped, finalFor: 'TX1' },
      { type: 'READ_RECEIVED', agreementId: 25n, address, circumstances: CHAIN.released, finalFor: 'TX1' },
      { type: 'READ_RECEIVED', agreementId: 26n, address, circumstances: CHAIN.released, finalFor: 'TX1' },
      { type: 'READ_RECEIVED', agreementId: 25n, address, circumstances: CHAIN.released, finalFor: 'TX0' },
      { type: 'READ_FAILED', agreementId: 25n, address, kind: 'unreachable', error: 'boom' },
      { type: 'READ_FAILED', agreementId: 25n, address, kind: 'incompatible', error: 'boom' },
      { type: 'READ_FAILED', agreementId: 26n, address, kind: 'unreachable', error: 'boom' },
    ] satisfies Event[]),
    { type: 'ACTION_REQUESTED', action: 'expire' },
    { type: 'ACTION_REQUESTED', action: 'refund_next' },
    { type: 'ACTION_REQUESTED', action: 'claim_refund' },
    { type: 'ACTION_REQUESTED', action: 'close' },
    // Completions for the pool a call was sent on, for another pool, and for
    // another call on the same pool -- the last two must be inert.
    { type: 'ACTION_SENT', action: 'refund_next', agreementId: 25n, txId: 'TX1' },
    { type: 'ACTION_SENT', action: 'refund_next', agreementId: 26n, txId: 'TX1' },
    { type: 'ACTION_UNCONFIRMED', action: 'refund_next', agreementId: 25n, txId: 'TX1', lastValid: 1120n },
    { type: 'ACTION_UNCONFIRMED', action: 'refund_next', agreementId: 26n, txId: 'TX1', lastValid: 1120n },
    { type: 'ACTION_FAILED', action: 'refund_next', agreementId: 25n, error: 'boom' },
    { type: 'ACTION_FAILED', action: 'claim_refund', agreementId: 25n, error: 'boom' },
    { type: 'MY_POOLS_REQUESTED' },
    // The walk connects only P, so Q's answers are always another wallet's.
    { type: 'MY_POOLS_FOUND', address: P, agreementIds: [25n] },
    { type: 'MY_POOLS_UNAVAILABLE', address: P, error: 'boom' },
    { type: 'MY_POOLS_FOUND', address: 'Q'.repeat(58), agreementIds: [26n] },
    { type: 'MY_POOLS_UNAVAILABLE', address: 'Q'.repeat(58), error: 'boom' },
  ]

  /**
   * Every reachable state, discovered by walking rather than listed by hand.
   *
   * Keyed by what actually gates a transition -- the purchase, which of the
   * guarded fields are present, and the *shape* of the verdict the
   * circumstances produce -- never by their contents. Keying on contents does
   * not terminate: the walk becomes combinatorial in the error strings and
   * the worker runs out of memory instead of failing.
   *
   * The fields left out of the key are left out because no guard reads them:
   * the error messages, `unavailable`, `lastTxId`, `reading` and the
   * whole my-pools lane. Each is orthogonal to every other, so keying on them
   * multiplies the space by two apiece -- measured, it turns a walk of
   * seconds into one that does not finish -- while adding no branch the
   * properties below could reach.
   *
   * `readError` is in the key by its kind alone, since the refresh predicate
   * reads the kind and nothing reads the message.
   *
   * The values in the key rather than the presence of them are the ones a
   * branch compares: whether `awaitingSeat` names the pool on screen, and
   * which call is in flight, whether it is unconfirmed, and whether its pool
   * is the one on screen.
   */
  function walk(): State[] {
    const verdictKey = (chain: Circumstances) => {
      const verdict = verdictFor(chain)
      return [
        verdict.situation,
        verdict.closed ? 'x' : '-',
        verdict.actions.join('+') || '-',
        verdict.owed ? 'o' : '-',
        verdict.seat ? 's' : '-',
      ].join(',')
    }

    const key = (s: State) =>
      [
        s.purchase,
        s.wallet ? 'w' : '-',
        s.agreementId === undefined ? '-' : 'a',
        s.quote ? 'q' : '-',
        s.circumstances ? verdictKey(s.circumstances) : '-',
        s.awaitingSeat === undefined
          ? '-'
          : s.awaitingSeat === s.agreementId
            ? 'mine'
            : 'other',
        // Not the message, which no guard reads -- only whether retrying can
        // help, which decides whether the pool is polled.
        s.readError?.kind ?? '-',
        s.sending === undefined
          ? '-'
          : [
              s.sending.action,
              s.sending.unconfirmed === undefined ? 'sent' : 'unconfirmed',
              s.sending.agreementId === s.agreementId ? 'here' : 'there',
            ].join('@'),
      ].join('|')

    const seen = new Map<string, State>()
    const queue: State[] = [initialState()]
    while (queue.length) {
      const state = queue.shift()!
      const k = key(state)
      if (seen.has(k)) continue
      seen.set(k, state)
      for (const event of everyEvent) queue.push(reduce(state, event).state)
    }
    return [...seen.values()]
  }

  // Computed once: every test below iterates it, and the walk is the
  // expensive part.
  const reachableStates = walk()
  const reachable = () => reachableStates

  it('finds a state space worth walking', () => {
    // A guard on the guard: a key that collapsed everything into one state
    // would make every property below pass vacuously.
    expect(reachableStates.length).toBeGreaterThan(50)
  })

  // The three properties below touch every (state, event) pair, so they
  // collect violations and assert once. An `expect` per pair is what made the
  // first version of this block time out: the matcher's overhead, multiplied
  // by the walk, dwarfs the reducer itself.

  it('never throws, for any event in any reachable state', () => {
    const threw: string[] = []
    for (const state of reachable()) {
      for (const event of everyEvent) {
        try {
          reduce(state, event)
        } catch (error) {
          threw.push(`${event.type} from ${state.purchase}: ${String(error)}`)
        }
      }
    }
    expect(threw).toEqual([])
  })

  it('stores nothing the verdict derives', () => {
    // The whole reason this state shape exists. A stored `situation`, a
    // stored `seatsLeft` or a stored action list is a second answer to a
    // question the chain already answers, free to drift -- and the buyer
    // reads the drift as truth.
    const allowed = new Set([
      'wallet',
      'agreementId',
      'purchase',
      'quote',
      'unavailable',
      'purchaseError',
      'reading',
      'circumstances',
      'readError',
      'awaitingSeat',
      'sending',
      'actionError',
      'lastTxId',
      'myPools',
      'findingMyPools',
      'myPoolsError',
    ])
    const unexpected = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        for (const field of Object.keys(reduce(state, event).state)) {
          if (!allowed.has(field)) unexpected.add(`${field} (after ${event.type})`)
        }
      }
    }
    expect([...unexpected]).toEqual([])
  })

  it('always rests in a purchase state the screens know', () => {
    const known = new Set(['IDLE', 'QUOTING', 'CONFIRM', 'CHECKING', 'SETTLING', 'UNAVAILABLE'])
    const unknown = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { purchase } = reduce(state, event).state
        if (!known.has(purchase)) unknown.add(`${purchase} (after ${event.type})`)
      }
    }
    expect([...unknown]).toEqual([])
  })

  it('opens an unpaid pass only on request, and never while one is in flight', () => {
    const opened = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { effects } = reduce(state, event)
        if (!effects.some((e) => e.type === 'REQUEST_QUOTE')) continue
        expect(event.type).toBe('QUOTE_REQUESTED')
        expect(['QUOTING', 'CHECKING', 'SETTLING']).not.toContain(state.purchase)
        expect(['IDLE', 'UNAVAILABLE', 'CONFIRM']).toContain(state.purchase)
        opened.add(state.purchase)
      }
    }
    // Not vacuous: the walk does open a pass from each state the allowlist
    // admits, CONFIRM included.
    expect([...opened].sort()).toEqual(['CONFIRM', 'IDLE', 'UNAVAILABLE'])
  })

  it('signs only what a freshness check has just passed', () => {
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { effects } = reduce(state, event)
        const signing = effects.find((e) => e.type === 'BUILD_AND_SIGN')
        if (!signing || signing.type !== 'BUILD_AND_SIGN') continue
        expect(event.type).toBe('QUOTE_VERIFIED')
        expect(state.purchase).toBe('CHECKING')
        expect(signing.quote).toBe(state.quote)
      }
    }
  })

  it('checks a quote only from the confirm screen, with a wallet to sign', () => {
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { effects } = reduce(state, event)
        const checking = effects.find((e) => e.type === 'VERIFY_QUOTE')
        if (!checking || checking.type !== 'VERIFY_QUOTE') continue
        expect(event.type).toBe('BUY_REQUESTED')
        expect(state.purchase).toBe('CONFIRM')
        expect(state.wallet).toBeDefined()
        expect(checking.quote).toBe(state.quote)
      }
    }
  })

  it('never emits an action the verdict did not offer', () => {
    // Over every state and verdict pairing the walk reaches, not one case.
    // The contract's own asserts are mirrored in `view.ts` and tested there;
    // what could go wrong here is the reducer routing around them.
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { effects } = reduce(state, event)
        const sending = effects.find((e) => e.type === 'SEND_ACTION')
        if (!sending || sending.type !== 'SEND_ACTION') continue
        expect(event.type).toBe('ACTION_REQUESTED')
        expect(state.sending, 'sent a second call over one in flight').toBeUndefined()
        expect(state.circumstances, 'sent a call against no reading').toBeDefined()
        expect(verdictFor(state.circumstances!).actions).toContain(sending.action)
        expect(sending.agreementId).toBe(state.agreementId)
      }
    }
  })

  it('reads only the pool on screen, or the pool of a call that may still land', () => {
    // The second is the one exception, and it is there for the lock: a
    // reading of that pool is what releases it, and nothing else would ask
    // for one once the buyer has moved on.
    const wrong = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { state: next, effects } = reduce(state, event)
        for (const effect of effects) {
          if (effect.type !== 'READ_POOL') continue
          const onScreen = effect.agreementId === next.agreementId && next.reading
          const forUnconfirmed =
            next.sending?.unconfirmed !== undefined &&
            effect.agreementId === next.sending.agreementId
          if (!onScreen && !forUnconfirmed) wrong.add(`${event.type} from ${state.purchase}`)
        }
      }
    }
    expect([...wrong]).toEqual([])
  })

  it('asks every reading of a pool with an unconfirmed call to check that call', () => {
    // Any one of them may be the reading that releases the lock. A reading not
    // asked cannot say it is final, and a lock waiting only on those would
    // hold until the buyer reloaded.
    const unasked = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { state: next, effects } = reduce(state, event)
        const unconfirmed = next.sending?.unconfirmed
        for (const effect of effects) {
          if (effect.type !== 'READ_POOL') continue
          const ofThatPool =
            unconfirmed !== undefined && effect.agreementId === next.sending!.agreementId
          if (ofThatPool ? effect.call !== unconfirmed : effect.call !== undefined) {
            unasked.add(`${event.type} from ${state.purchase}`)
          }
        }
      }
    }
    expect([...unasked]).toEqual([])
  })

  it('releases the lock only when the call it holds cannot still be travelling', () => {
    // Either a completion naming the same call on the same pool that says it
    // ended -- confirmed or refused -- or, for a call that could not be
    // confirmed, a reading of its pool that shows what became of it. Nothing
    // else: not a screen change, not a wallet change, not a completion for
    // another call, and not a reading from before the call could have landed.
    const early = new Set<string>()
    for (const state of reachable()) {
      const held = state.sending
      if (held === undefined) continue
      for (const event of everyEvent) {
        if (reduce(state, event).state.sending !== undefined) continue
        const ended =
          (event.type === 'ACTION_SENT' || event.type === 'ACTION_FAILED') &&
          event.action === held.action &&
          event.agreementId === held.agreementId
        const answered =
          event.type === 'READ_RECEIVED' &&
          held.unconfirmed !== undefined &&
          event.finalFor === held.unconfirmed.txId &&
          event.agreementId === held.agreementId &&
          event.address === (state.wallet ?? null)
        if (!ended && !answered) early.add(`${event.type} released ${held.action}`)
      }
    }
    expect([...early]).toEqual([])
  })

  it('ignores a completion that does not name the call in flight', () => {
    const moved = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        if (
          event.type !== 'ACTION_SENT' &&
          event.type !== 'ACTION_UNCONFIRMED' &&
          event.type !== 'ACTION_FAILED'
        ) {
          continue
        }
        const held = state.sending
        if (held?.action === event.action && held.agreementId === event.agreementId) continue
        const result = reduce(state, event)
        if (result.state !== state || result.effects.length > 0) {
          moved.add(`${event.type} for ${event.action}@${event.agreementId}`)
        }
      }
    }
    expect([...moved]).toEqual([])
  })

  it("never shows a call's id or error under another pool's heading", () => {
    const misplaced = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        if (
          event.type !== 'ACTION_SENT' &&
          event.type !== 'ACTION_UNCONFIRMED' &&
          event.type !== 'ACTION_FAILED'
        ) {
          continue
        }
        if (event.agreementId === state.agreementId) continue
        const next = reduce(state, event).state
        if (next.lastTxId !== state.lastTxId || next.actionError !== state.actionError) {
          misplaced.add(`${event.type} for ${event.agreementId} on ${state.agreementId}`)
        }
      }
    }
    expect([...misplaced]).toEqual([])
  })

  it('moves the URL whenever a quote or a payment moves the screen', () => {
    // And never otherwise. A pool on screen that the address bar does not
    // name is undone by the router's next OPENED_POOL, by a copied link and
    // by a reload.
    const wrong = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { state: next, effects } = reduce(state, event)
        const navigations = effects.filter((e) => e.type === 'NAVIGATE')
        const paid = event.type === 'SETTLED' || event.type === 'RECEIPT_UNREADABLE'
        const expected =
          (event.type === 'QUOTE_RECEIVED' && state.purchase === 'QUOTING') ||
          (paid && state.agreementId !== next.agreementId)
        if (navigations.length !== (expected ? 1 : 0)) {
          wrong.add(`${event.type} from ${state.agreementId === undefined ? 'entry' : 'a pool'}`)
          continue
        }
        if (expected) {
          const [first] = effects
          if (
            first?.type !== 'NAVIGATE' ||
            next.agreementId === undefined ||
            first.path !== poolPath(next.agreementId)
          ) {
            wrong.add(`${event.type} navigated somewhere else, or not first`)
          }
        }
      }
    }
    expect([...wrong]).toEqual([])
  })

  it('never stores a reading of a pool that is not on screen', () => {
    const wrong = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        if (event.type !== 'READ_RECEIVED' && event.type !== 'READ_FAILED') continue
        if (event.agreementId === state.agreementId) continue
        const next = reduce(state, event).state
        if (next.circumstances !== state.circumstances || next.readError !== state.readError) {
          wrong.add(`${event.type} for ${event.agreementId} on ${state.agreementId}`)
        }
      }
    }
    expect([...wrong]).toEqual([])
  })

  it('never hears a reading made for another wallet', () => {
    // Not stored, not an error, and not a release: dropped as though it never
    // arrived, so a walletless reading landing after the connected wallet's
    // cannot put "no seat here" over a seat that is owed.
    const heard = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        if (event.type !== 'READ_RECEIVED' && event.type !== 'READ_FAILED') continue
        if (event.address === (state.wallet ?? null)) continue
        const result = reduce(state, event)
        if (result.state !== state || result.effects.length > 0) {
          heard.add(`${event.type} for ${event.address ? 'a wallet' : 'no wallet'}`)
        }
      }
    }
    expect([...heard]).toEqual([])
  })

  it('asks for every reading on behalf of the wallet connected now', () => {
    // The other half of the rule above. A reading asked for the wrong wallet
    // would be dropped on arrival and never replaced.
    const wrong = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        const { state: next, effects } = reduce(state, event)
        for (const effect of effects) {
          if (effect.type === 'READ_POOL' && effect.address !== (next.wallet ?? null)) {
            wrong.add(`${event.type} asked for ${effect.address ?? 'no wallet'}`)
          }
        }
      }
    }
    expect([...wrong]).toEqual([])
  })

  it('never lists a search made for another wallet', () => {
    const listed = new Set<string>()
    for (const state of reachable()) {
      for (const event of everyEvent) {
        if (event.type !== 'MY_POOLS_FOUND' && event.type !== 'MY_POOLS_UNAVAILABLE') continue
        if (event.address === state.wallet) continue
        if (reduce(state, event).state !== state) listed.add(event.type)
      }
    }
    expect([...listed]).toEqual([])
  })

  it('polls exactly when the chain still has something to say', () => {
    for (const state of reachable()) {
      if (state.agreementId === undefined) {
        expect(shouldPoll(state)).toBe(false)
        continue
      }
      if (state.sending?.unconfirmed !== undefined) {
        expect(shouldPoll(state)).toBe(true)
        continue
      }
      if (state.readError?.kind === 'incompatible') {
        expect(shouldPoll(state)).toBe(false)
        continue
      }
      if (state.awaitingSeat !== undefined || state.circumstances === undefined) {
        expect(shouldPoll(state)).toBe(true)
        continue
      }
      const verdict = verdictFor(state.circumstances)
      const overForThisBuyer =
        verdict.closed ||
        verdict.situation === 'released' ||
        (verdict.situation === 'refunded' && !verdict.owed && verdict.actions.length === 0)
      expect(shouldPoll(state)).toBe(!overForThisBuyer)
    }
  })

  it('leaves the state identical for an event it does not price', () => {
    // The `exhaustive` idiom: an event the machine does not know is inert
    // rather than fatal, and the same object comes back.
    const unknown = { type: 'SOMETHING_ELSE' } as unknown as Event
    let changed = 0
    for (const state of reachable()) {
      const result = reduce(state, unknown)
      if (result.state !== state || result.effects.length > 0) changed += 1
    }
    expect(changed).toBe(0)
  })

  it('reaches every purchase state the flow names', () => {
    const reached = new Set(reachable().map((s) => s.purchase))
    for (const purchase of ['IDLE', 'QUOTING', 'CONFIRM', 'CHECKING', 'SETTLING', 'UNAVAILABLE']) {
      expect(reached).toContain(purchase)
    }
  })
})
