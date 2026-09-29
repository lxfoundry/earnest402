import { describe, expect, it } from 'vitest'
import { POOL_STATES, type Agreement, type PoolState, type Seat } from '../src/chain/decode'
import type { ClosedOutcome } from '../src/chain/read'
import {
  REFUND_BATCH_CEILING,
  SITUATION_FOR,
  editionFiles,
  quoteDisagreements,
  refundBatchSize,
  seatsLeft,
  verdictFor,
  type SeatQuote,
} from '../src/pool/view'
import { COPY } from '../src/brand'

const DEADLINE = 1789651281n
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

const seat = (overrides: Partial<Seat> = {}): Seat => ({
  index: 0,
  payer: 'P'.repeat(58),
  amount: 100000n,
  status: 'owed',
  ...overrides,
})

const verdict = (
  record: Agreement | null,
  mine: Seat | null = null,
  chainNow = BEFORE,
  canPayFee = true,
  closedOutcome?: ClosedOutcome,
) => verdictFor({ record, seat: mine, chainNow, canPayFee, closedOutcome })

describe('SITUATION_FOR', () => {
  it('prices every state the contract can store', () => {
    // A total Record will not compile with a member missing, so this asserts
    // the runtime half: no state falls through to whatever the last branch
    // happened to be.
    for (const state of POOL_STATES) {
      expect(SITUATION_FOR[state as PoolState]).toBeTruthy()
    }
  })

  it('tells a filled quorum pool the same thing as a funded one', () => {
    // FILLED is unreachable for a hash pool, but the map still needs it, and
    // what it means to a buyer is identical: the money is in, delivery is
    // pending.
    expect(SITUATION_FOR.FILLED).toBe(SITUATION_FOR.FUNDED)
  })
})

describe('a pool that is still open', () => {
  it('is waiting, with nothing to do', () => {
    const v = verdict(agreement())
    expect(v.situation).toBe('waiting')
    expect(v.actions).toEqual([])
  })

  it('offers no expire before the deadline, however close', () => {
    // The countdown is an estimate; the contract's clock is a block behind.
    // Offering this early asks for a signature the chain then refuses: no fee,
    // since a refused call is never committed, but a prompt signed for nothing.
    expect(verdict(agreement(), null, DEADLINE - 1n).actions).toEqual([])
  })

  it('offers expire once chain time has reached the deadline', () => {
    expect(verdict(agreement(), null, AFTER).actions).toEqual(['expire'])
  })
})

describe('a pool that filled', () => {
  it('is awaiting delivery', () => {
    expect(verdict(agreement({ state: 'FUNDED', seats: 5 })).situation).toBe(
      'awaiting-delivery',
    )
  })

  it('can still be expired once the deadline passes', () => {
    // The delivery window closing is exactly when a funded pool becomes
    // refundable, and nobody but the buyers may care to act.
    const v = verdict(agreement({ state: 'FUNDED', seats: 5 }), null, AFTER)
    expect(v.actions).toEqual(['expire'])
  })
})

describe('a pool that expired', () => {
  const expired = agreement({ state: 'EXPIRED', seats: 3, refundCursor: 0 })

  it('is refundable and offers the pass', () => {
    const v = verdict(expired, seat(), AFTER)
    expect(v.situation).toBe('refundable')
    expect(v.actions).toContain('refund_next')
  })

  it('does not offer expire again', () => {
    expect(verdict(expired, null, AFTER).actions).not.toContain('expire')
  })

  it('does not offer close, because EXPIRED is not terminal', () => {
    expect(verdict(expired, null, AFTER).actions).not.toContain('close')
  })
})

describe('a pool whose refund pass is running', () => {
  it('still offers refund_next', () => {
    // The second half of the pass's state test. Every other test here that
    // asks for the pass uses an EXPIRED pool, so a guard that dropped
    // REFUNDING would pass them all and strand a half-drained pool.
    const running = agreement({ state: 'REFUNDING', seats: 3, refundCursor: 1 })
    const v = verdict(running, null, AFTER)
    expect(v.situation).toBe('refunding')
    expect(v.actions).toContain('refund_next')
  })
})

describe('a pool that sold nothing before it expired', () => {
  // The case a cursor guard on `refund_next` would get wrong: `refundCursor`
  // (0) already equals `seats` (0) the moment this pool expires, so a guard
  // reading "offer the pass while the cursor is behind" would never offer it
  // -- yet this is exactly the one call `refundBatchSize` says is needed to
  // reach REFUNDED, and the only way this agreement's deposit is ever
  // reclaimed with `close`.
  const empty = agreement({ state: 'EXPIRED', seats: 0, refundCursor: 0 })

  it('still offers refund_next', () => {
    expect(verdict(empty, null, AFTER).actions).toContain('refund_next')
  })

  it('agrees with refundBatchSize that exactly one call is needed', () => {
    expect(refundBatchSize(empty)).toBe(1)
  })
})

describe('claiming a skipped seat', () => {
  const skipped = (cursor: number, state: PoolState = 'REFUNDING') =>
    agreement({ state, seats: 3, refundCursor: cursor, unclaimedSeats: 1 })

  it('is offered once the cursor has passed the seat', () => {
    const v = verdict(skipped(2), seat({ index: 1 }), AFTER)
    expect(v.actions).toContain('claim_refund')
  })

  it('is refused while the seat is still ahead of the cursor', () => {
    // The contract refuses it, and for a reason worth respecting: claiming a
    // seat the pass has not reached would decrement unclaimed_seats for a seat
    // that was never skipped, cancelling a real skip and stranding that payer
    // permanently.
    const v = verdict(skipped(1), seat({ index: 1 }), AFTER)
    expect(v.actions).not.toContain('claim_refund')
  })

  it('is refused for a seat that has already been paid', () => {
    const v = verdict(skipped(3), seat({ index: 1, amount: 0n, status: 'settled' }), AFTER)
    expect(v.actions).not.toContain('claim_refund')
  })

  it('is still offered after the pass has finished', () => {
    // REFUNDED means the cursor finished its pass, not that everyone has
    // their money.
    const v = verdict(skipped(3, 'REFUNDED'), seat({ index: 1 }), AFTER)
    expect(v.actions).toContain('claim_refund')
    expect(v.situation).toBe('refunded')
    expect(v.owed).toBe(true)
  })
})

describe('a seat the refund pass has gone past', () => {
  const running = (cursor: number) =>
    agreement({ state: 'REFUNDING', seats: 3, refundCursor: cursor, unclaimedSeats: 1 })

  it('is skipped on a running pass once the cursor is past it', () => {
    // The case `owed` alone cannot tell apart: the pass is still running, and
    // it will never come back to this seat.
    const v = verdict(running(2), seat({ index: 1 }), AFTER)
    expect(v.situation).toBe('refunding')
    expect(v.owed).toBe(true)
    expect(v.skipped).toBe(true)
  })

  it('is not skipped while the seat is still ahead of the cursor', () => {
    const v = verdict(running(1), seat({ index: 1 }), AFTER)
    expect(v.owed).toBe(true)
    expect(v.skipped).toBe(false)
  })

  it('is skipped on a finished pass', () => {
    const done = agreement({ state: 'REFUNDED', seats: 3, refundCursor: 3, unclaimedSeats: 1 })
    expect(verdict(done, seat({ index: 2 }), AFTER).skipped).toBe(true)
  })

  it('is not skipped once it has been paid', () => {
    const v = verdict(running(2), seat({ index: 1, amount: 0n, status: 'settled' }), AFTER)
    expect(v.skipped).toBe(false)
  })

  it('is never skipped on a pool the pass has not started', () => {
    // `expire` leaves the cursor at zero, so nothing is behind it.
    const expired = agreement({ state: 'EXPIRED', seats: 3, refundCursor: 0 })
    expect(verdict(expired, seat({ index: 0 }), AFTER).skipped).toBe(false)
  })

  it('says whether it is skipped whether or not the wallet can pay a fee', () => {
    // A fact about the seat, not about what may be sent.
    expect(verdict(running(2), seat({ index: 1 }), AFTER, false).skipped).toBe(true)
  })

  it('is never skipped on a pool whose boxes are gone', () => {
    expect(verdict(null, seat(), AFTER, true, 'refunded').skipped).toBe(false)
  })
})

describe('closing', () => {
  it('is offered once a refunded pool owes nothing', () => {
    const done = agreement({
      state: 'REFUNDED',
      seats: 3,
      refundCursor: 3,
      unclaimedSeats: 0,
      totalHeld: 0n,
    })
    expect(verdict(done, null, AFTER).actions).toEqual(['close'])
  })

  it('is withheld while a seat is unclaimed', () => {
    const stuck = agreement({
      state: 'REFUNDED',
      seats: 3,
      refundCursor: 3,
      unclaimedSeats: 1,
      totalHeld: 100000n,
    })
    expect(verdict(stuck, null, AFTER).actions).not.toContain('close')
  })

  it('is withheld while the contract still holds money', () => {
    const holding = agreement({
      state: 'REFUNDED',
      seats: 5,
      refundCursor: 5,
      unclaimedSeats: 0,
      totalHeld: 1n,
    })
    expect(verdict(holding, null, AFTER).actions).not.toContain('close')
  })

  it('is never offered on a released pool, though the contract allows it', () => {
    // The one action that costs other people something: closing deletes the
    // boxes, which are the only place the commitment is readable, and pays
    // the deposit to the creator rather than to whoever sent it. Every guard
    // the contract applies is satisfied here, and it is still withheld.
    const delivered = agreement({
      state: 'RELEASED',
      seats: 5,
      refundCursor: 0,
      unclaimedSeats: 0,
      totalHeld: 0n,
    })
    expect(verdict(delivered, null, AFTER).actions).toEqual([])
  })
})

describe('a pool whose boxes are gone', () => {
  it('is its own situation, never an error', () => {
    const v = verdict(null)
    expect(v.situation).toBe('gone')
    expect(v.actions).toEqual([])
    expect(v.closed).toBe(true)
  })

  it('tells a delivered pool from a refunded one, given the history', () => {
    // Both read as an absent box, and they are opposite news: one says the
    // edition was published, the other says the money went back.
    expect(verdict(null, null, AFTER, true, 'released').situation).toBe('released')
    expect(verdict(null, null, AFTER, true, 'refunded').situation).toBe('refunded')
  })

  it('says gone rather than guessing, when the history did not say', () => {
    // An indexer that has not caught up is not evidence of either outcome.
    expect(verdict(null, null, AFTER, true, 'unknown').situation).toBe('gone')
    expect(verdict(null).situation).toBe('gone')
  })

  it('offers nothing to send, whatever the outcome was', () => {
    // There is nothing left to call: the boxes every remaining method reads
    // are the ones close deleted.
    for (const outcome of ['released', 'refunded', 'unknown'] as const) {
      expect(verdict(null, seat(), AFTER, true, outcome).actions).toEqual([])
    }
  })

  it('still reports a live pool as open rather than closed', () => {
    expect(verdict(agreement()).closed).toBe(false)
  })
})

describe('editionFiles', () => {
  const CID = 'bafybeidahqbf2ym3bx3cbv7czf6t2ois53p6sv5pnj44fv34atm55fdr4i'

  it('resolves the edition and its committed JSON under the CID, on the configured gateway', () => {
    expect(editionFiles({ edition: '900', cid: CID }, 'https://gateway.example')).toEqual({
      html: { name: 'edition-900.html', url: `https://gateway.example/ipfs/${CID}/edition-900.html` },
      json: { name: 'edition-900.json', url: `https://gateway.example/ipfs/${CID}/edition-900.json` },
    })
  })

  it("names a trip's files by the trip stem", () => {
    expect(editionFiles({ edition: '1', cid: CID }, 'https://gateway.example', 'trip')).toEqual({
      html: { name: 'trip-1.html', url: `https://gateway.example/ipfs/${CID}/trip-1.html` },
      json: { name: 'trip-1.json', url: `https://gateway.example/ipfs/${CID}/trip-1.json` },
    })
  })
})

describe('a buyer who cannot pay a fee', () => {
  it('is offered nothing, rather than a button that fails', () => {
    // Buying is gasless; refunding is not. The money still reaches them --
    // the refund calls pay the roster address, not the sender -- so the
    // absence of an action here is not the absence of a refund.
    const expired = agreement({ state: 'EXPIRED', seats: 3 })
    expect(verdict(expired, seat(), AFTER, false).actions).toEqual([])
  })

  it('is still told the truth about their seat', () => {
    const expired = agreement({ state: 'EXPIRED', seats: 3 })
    const v = verdict(expired, seat(), AFTER, false)
    expect(v.situation).toBe('refundable')
    expect(v.owed).toBe(true)
  })
})

describe('refundBatchSize', () => {
  it('never exceeds the contract ceiling', () => {
    const big = agreement({ seats: 20, refundCursor: 0 })
    expect(refundBatchSize(big)).toBe(REFUND_BATCH_CEILING)
  })

  it('covers exactly what is left when fewer remain', () => {
    expect(refundBatchSize(agreement({ seats: 5, refundCursor: 3 }))).toBe(2)
  })

  it('is at least one even for a pool that sold nothing', () => {
    // A zero-seat agreement still needs one call to reach REFUNDED before
    // close will accept it.
    expect(refundBatchSize(agreement({ seats: 0, refundCursor: 0 }))).toBe(1)
  })
})

describe('seatsLeft', () => {
  it('counts what is still for sale', () => {
    expect(seatsLeft(agreement({ minSeats: 5, maxSeats: 5, seats: 1 }))).toBe(4)
  })

  it('never goes negative', () => {
    expect(seatsLeft(agreement({ minSeats: 5, maxSeats: 5, seats: 6 }))).toBe(0)
  })

  it('counts against the ceiling join enforces, not the fill target', () => {
    // minSeats and maxSeats are equal on every pool this project creates, so
    // this case cannot happen today -- it exists to pin which field the
    // function reads, since the two fixtures above cannot tell that apart
    // when both are 5.
    expect(seatsLeft(agreement({ minSeats: 5, maxSeats: 8, seats: 5 }))).toBe(3)
  })
})

describe('quoteDisagreements', () => {
  // A 402 is a photograph of a pool and a wallet prompt can sit open for
  // minutes. Everything here is about the interval in between, which is where
  // somebody else's seat lands.
  //
  // The fixture pool is five seats of which one is sold, at 0.10 USDC, so a
  // matching quote offers four of five left.
  const quote = (overrides: Partial<SeatQuote> = {}): SeatQuote => ({
    agreementId: 25n,
    amount: 100000n,
    seatsTotal: 5,
    seatsLeft: 4,
    deadline: Number(DEADLINE),
    commitSha256: 'fa'.repeat(32),
    ...overrides,
  })

  it('finds nothing wrong with a quote the pool still matches', () => {
    expect(quoteDisagreements(quote(), agreement())).toEqual([])
  })

  it('reports a pool whose boxes are gone', () => {
    // Not "the seat count moved" and not an error: closing deletes the boxes,
    // so there is no longer anything to pay into. It is also the only check
    // on the quote's own agreement id, since the record does not carry one.
    const found = quoteDisagreements(quote(), null)
    expect(found).toHaveLength(1)
    expect(found[0]).toMatch(/pool 25 has no record on chain/)
  })

  it.each(['EXPIRED', 'FUNDED', 'REFUNDED'] as const)(
    'reports a pool that is %s rather than open',
    (state) => {
      // Seats and price unchanged, so the state is the only thing that moved:
      // a pool that expires with seats unsold keeps the `seatsLeft` its quote
      // was issued with, and every other comparison agrees.
      const found = quoteDisagreements(quote(), agreement({ state }))
      expect(found).toHaveLength(1)
      expect(found[0]).toMatch(/no longer open/)
      expect(found[0]).toContain(state)
    },
  )

  it('does not report an open pool as closed to sale', () => {
    // The control for the three above, stated on its own so it cannot be
    // lost if the matching-quote test's fixture changes.
    expect(quoteDisagreements(quote(), agreement({ state: 'OPEN' }))).toEqual([])
  })

  it('reports a share price that moved', () => {
    const found = quoteDisagreements(quote({ amount: 90000n }), agreement())
    expect(found).toHaveLength(1)
    expect(found[0]).toMatch(/price moved/)
    expect(found[0]).toContain('90000')
    expect(found[0]).toContain('100000')
  })

  it('reports a seat sold since the quote was issued', () => {
    // The ordinary case, not the exotic one: the quote said four were free
    // and the record now says three.
    const found = quoteDisagreements(quote(), agreement({ seats: 2 }))
    expect(found).toHaveLength(1)
    expect(found[0]).toMatch(/seats moved/)
    expect(found[0]).toContain('3 are free now')
  })

  it('reports a different edition', () => {
    const found = quoteDisagreements(quote({ commitSha256: 'ab'.repeat(32) }), agreement())
    expect(found).toHaveLength(1)
    expect(found[0]).toMatch(/edition changed/)
  })

  it('does not call a case difference in the commitment a disagreement', () => {
    // `decodeAgreement` spells this with algosdk's hex encoder and the 402
    // spells it with Python's `bytes.hex()`. Both are lowercase today and
    // neither promises to stay that way; a case flip must not refuse every
    // purchase of a perfectly good edition.
    const upper = quote({ commitSha256: 'FA'.repeat(32) })
    expect(quoteDisagreements(upper, agreement())).toEqual([])
  })

  it('reports every disagreement at once, not just the first', () => {
    const found = quoteDisagreements(
      quote({ amount: 1n, seatsLeft: 1, commitSha256: 'ab'.repeat(32) }),
      agreement(),
    )
    expect(found).toHaveLength(3)
  })

  it('says "is" for a single remaining seat', () => {
    // The message is shown to a buyer who just lost a seat; "1 are free now"
    // is the kind of detail that makes a refusal look like a bug.
    const found = quoteDisagreements(quote({ seatsLeft: 2 }), agreement({ seats: 4 }))
    expect(found[0]).toContain('1 is free now')
  })

  it('counts seats against the ceiling join enforces', () => {
    // seatsLeft() reads maxSeats, and this is the one fixture where the fill
    // target and the ceiling differ, so it pins which of the two the
    // freshness check compares against.
    const record = agreement({ minSeats: 5, maxSeats: 8, seats: 5 })
    expect(quoteDisagreements(quote({ seatsLeft: 3 }), record)).toEqual([])
  })

  describe('in the words of a trip', () => {
    const TRIP = COPY.travel.nouns

    it('says a trip whose boxes are gone is over, naming neither pool nor edition', () => {
      const [found] = quoteDisagreements(quote(), null, TRIP)
      expect(found).toMatch(
        /^trip 25 has no record on chain\. Closing a trip deletes its boxes, so this trip is either over/,
      )
      expect(found).not.toMatch(/pool|edition/i)
    })

    it('says a trip that left OPEN is no longer open', () => {
      const [found] = quoteDisagreements(quote(), agreement({ state: 'EXPIRED' }), TRIP)
      expect(found).toBe(
        'the trip is no longer open: it is EXPIRED, and only an open trip sells seats.',
      )
    })

    it("says the trip's price moved", () => {
      const [found] = quoteDisagreements(quote({ amount: 90000n }), agreement(), TRIP)
      expect(found).toBe(
        'the price moved: this quote asks for 90000 micro-units and the trip now charges 100000.',
      )
    })

    it('says the trip file changed', () => {
      const changed = quote({ commitSha256: 'ab'.repeat(32) })
      const [found] = quoteDisagreements(changed, agreement(), TRIP)
      expect(found).toBe(
        `the trip file changed: this quote is for ${'ab'.repeat(32)} and the trip is committed ` +
          `to ${'fa'.repeat(32)}.`,
      )
    })
  })
})
