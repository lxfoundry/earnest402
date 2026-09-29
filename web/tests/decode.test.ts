import algosdk from 'algosdk'
import { describe, expect, it } from 'vitest'
import {
  AGREEMENT_BYTES,
  BoxDecodeError,
  SEAT_BYTES,
  POOL_STATES,
  ZERO_ADDRESS,
  agreementBoxName,
  decodeAgreement,
  decodeRoster,
  findSeat,
  rosterBoxName,
} from '../src/chain/decode'
import golden from './golden/escrow-boxes.json'

/**
 * The vectors come from `agents/dump_box_vectors.py`, which takes the layout
 * from the compiled contract's own ARC-56 description, places each field by
 * name, and reads the bytes back through the Python decoder before writing
 * them out. So this compares two independent implementations of the same
 * layout rather than one implementation with itself — which is the only
 * version of this check worth running, since a decoder validated against a
 * hand-written encoder at the same offsets agrees with its own mistakes.
 */

const b64 = (value: string) => algosdk.base64ToBytes(value)

describe('the committed vectors', () => {
  it('agree with the widths this decoder assumes', () => {
    expect(golden.agreementBytes).toBe(AGREEMENT_BYTES)
    expect(golden.seatBytes).toBe(SEAT_BYTES)
  })

  it('agree about the zero address', () => {
    expect(golden.zeroAddress).toBe(ZERO_ADDRESS)
  })

  it('covers every vector, so a Python-side addition cannot go unread', () => {
    expect(golden.vectors.length).toBeGreaterThanOrEqual(3)
  })

  it('name the states in the order this decoder indexes them', () => {
    // The one place a wrong *order* -- as opposed to a wrong offset -- becomes
    // a wrong answer about money. `decodeAgreement` reads one byte and indexes
    // this array with it, so swapping RELEASED and EXPIRED tells a buyer owed
    // a refund that the edition was delivered, and withholds `refund_next`.
    // Nothing about the record's width or its other fields would notice.
    expect([...POOL_STATES]).toEqual(golden.states)
  })
})

describe('decodeAgreement', () => {
  for (const vector of golden.vectors) {
    it(`reads ${vector.name} exactly as Python does`, () => {
      const record = decodeAgreement(b64(vector.agreement.raw))
      const want = vector.agreement.expected
      expect(record.condition).toBe(want.condition)
      // Against the Python-derived list, not `POOL_STATES[want.state]`:
      // indexing the same array on both sides compares it with itself.
      expect(record.state).toBe(golden.states[want.state])
      expect(record.deadline).toBe(BigInt(want.deadline))
      expect(record.sharePrice).toBe(BigInt(want.sharePrice))
      expect(record.minSeats).toBe(want.minSeats)
      expect(record.maxSeats).toBe(want.maxSeats)
      expect(record.seats).toBe(want.seats)
      expect(record.refundCursor).toBe(want.refundCursor)
      expect(record.unclaimedSeats).toBe(want.unclaimedSeats)
      expect(record.totalHeld).toBe(BigInt(want.totalHeld))
      expect(record.commitSha256).toBe(want.commitSha256)
      expect(record.beneficiary).toBe(want.beneficiary)
      expect(record.verifier).toBe(want.verifier)
      expect(record.creator).toBe(want.creator)
    })
  }

  it('refuses a record of the wrong size rather than decoding garbage', () => {
    // The expensive failure this guards: fields read at hardcoded offsets
    // produce plausible numbers from the wrong bytes, so a refunding pool
    // could read as open with a deadline taken from inside an address.
    expect(() => decodeAgreement(new Uint8Array(AGREEMENT_BYTES - 1))).toThrow(
      BoxDecodeError,
    )
    expect(() => decodeAgreement(new Uint8Array(AGREEMENT_BYTES + 1))).toThrow(
      /hardcoded offset/,
    )
  })

  it('refuses a state it does not know', () => {
    // A state added to the contract without being priced here must fail
    // loudly: the states decide whether money is owed.
    const raw = new Uint8Array(AGREEMENT_BYTES)
    raw[1] = 7
    expect(() => decodeAgreement(raw)).toThrow(/does not know/)
  })

  it('reads the maximum value of every fixed-width field', () => {
    // A width read one byte short or long shows up here as a wrong number
    // rather than as plausible data.
    const vector = golden.vectors.find((v) => v.name === 'maximum-widths')!
    const record = decodeAgreement(b64(vector.agreement.raw))
    expect(record.deadline).toBe(2n ** 64n - 1n)
    expect(record.totalHeld).toBe(2n ** 64n - 1n)
    expect(record.seats).toBe(65535)
    expect(record.commitSha256).toBe('ff'.repeat(32))
  })
})

describe('decodeRoster', () => {
  for (const vector of golden.vectors) {
    it(`reads ${vector.name}'s roster exactly as Python does`, () => {
      const seats = decodeRoster(b64(vector.roster.raw))
      expect(seats.length).toBe(vector.roster.expected.length)
      seats.forEach((seat, i) => {
        expect(seat.payer).toBe(vector.roster.expected[i]!.payer)
        expect(seat.amount).toBe(BigInt(vector.roster.expected[i]!.amount))
        expect(seat.index).toBe(i)
      })
    })
  }

  it('tells a paid seat from an empty one', () => {
    // Both carry a zero amount; only the address separates them. Reading a
    // zeroed amount as "empty" would report a refunded buyer as never having
    // had a seat, and hide a seat still owed money behind a roster that looks
    // unsold.
    const vector = golden.vectors.find(
      (v) => v.name === 'refunding-one-paid-one-skipped',
    )!
    const seats = decodeRoster(b64(vector.roster.raw))
    expect(seats.map((s) => s.status)).toEqual([
      'settled',
      'owed',
      'owed',
      'empty',
      'empty',
    ])
  })

  it('refuses a roster that is not a whole number of seats', () => {
    expect(() => decodeRoster(new Uint8Array(SEAT_BYTES + 1))).toThrow(BoxDecodeError)
  })

  it('reads an empty roster as no seats, not as an error', () => {
    // `close` deletes the box; a caller that reached here with zero bytes has
    // a box that exists and is empty, which is a legal shape.
    expect(decodeRoster(new Uint8Array(0))).toEqual([])
  })
})

describe('findSeat', () => {
  const vector = golden.vectors.find((v) => v.name === 'open-pool-one-seat-sold')!
  const seats = decodeRoster(b64(vector.roster.raw))

  it('finds the wallet that holds a seat', () => {
    const mine = findSeat(seats, vector.roster.expected[0]!.payer)
    expect(mine?.index).toBe(0)
    expect(mine?.amount).toBe(100000n)
  })

  it('returns null for a wallet with no seat', () => {
    expect(findSeat(seats, algosdk.generateAccount().addr.toString())).toBeNull()
  })

  it('never matches the zero address, however many empty seats there are', () => {
    // Four seats in this roster carry it. Matching would hand a stranger the
    // first unsold seat and offer them a refund for it.
    expect(findSeat(seats, ZERO_ADDRESS)).toBeNull()
  })
})

describe('box names', () => {
  for (const vector of golden.vectors) {
    it(`builds ${vector.name}'s box names as Python does`, () => {
      const id = BigInt(vector.agreementId)
      expect(algosdk.bytesToBase64(agreementBoxName(id))).toBe(
        vector.boxNames.agreement,
      )
      expect(algosdk.bytesToBase64(rosterBoxName(id))).toBe(vector.boxNames.roster)
    })
  }

  it('orders by name the way it orders by id', () => {
    // Big-endian is what makes that true, and the operator's backwards scan
    // depends on it.
    const a = agreementBoxName(1n)
    const b = agreementBoxName(256n)
    expect(algosdk.bytesToHex(a) < algosdk.bytesToHex(b)).toBe(true)
  })
})
