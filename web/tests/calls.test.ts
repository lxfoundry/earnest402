import algosdk from 'algosdk'
import { describe, expect, it } from 'vitest'
import {
  CALL_BYTES_CEILING,
  CallArgumentError,
  buildClaimRefund,
  buildClose,
  buildExpire,
  buildRefundNext,
} from '../src/chain/calls'
import { REFUND_BATCH_CEILING, agreementBoxName, rosterBoxName, type Agreement, type Seat } from '../src/chain/decode'
import { SELECTORS } from '../src/chain/selectors'

/**
 * Offline, no algosdk mocks: every test here builds a real `Transaction` and
 * reads the decoded result back, the same discipline `buildJoinGroup.test.ts`
 * uses. There is no facilitator and no group on this side of the module, so
 * there is nothing to fake -- these builders are pure functions from
 * arguments to a signed-later `Transaction`.
 */

const UINT64 = new algosdk.ABIUintType(64)

/**
 * Any 32-byte fill is a valid, checksummed Algorand address -- `encodeAddress`
 * computes the checksum itself, so it does not matter that nothing signs for
 * these. `chain/decode.ts`'s own fixtures use the same trick. Distinct fill
 * bytes are what let a test on the accounts array tell six roster seats
 * apart by identity rather than by position alone.
 */
const addr = (byte: number) => algosdk.encodeAddress(new Uint8Array(32).fill(byte))

const SENDER = addr(0x01)
const BENEFICIARY = addr(0x02)
const VERIFIER = addr(0x03)
const CREATOR = addr(0x04)

function agreement(overrides: Partial<Agreement> = {}): Agreement {
  return {
    condition: 0,
    state: 'EXPIRED',
    deadline: 1000n,
    sharePrice: 100000n,
    minSeats: 5,
    maxSeats: 5,
    seats: 5,
    refundCursor: 0,
    unclaimedSeats: 0,
    totalHeld: 500000n,
    commitSha256: 'fa'.repeat(32),
    beneficiary: BENEFICIARY,
    verifier: VERIFIER,
    creator: CREATOR,
    ...overrides,
  }
}

function seat(index: number, overrides: Partial<Seat> = {}): Seat {
  return {
    index,
    // 0x10 upward, clear of the fixed addresses above, so a seat's address
    // never accidentally collides with the beneficiary or creator fixture.
    payer: addr(0x10 + index),
    amount: 100000n,
    status: 'owed',
    ...overrides,
  }
}

function roster(count: number): Seat[] {
  return Array.from({ length: count }, (_, index) => seat(index))
}

const suggestedParams = (): algosdk.SuggestedParams =>
  ({
    fee: 0,
    flatFee: false,
    firstValid: 1000,
    lastValid: 2000,
    genesisHash: new Uint8Array(32),
    genesisID: 'testnet-v1.0',
    minFee: 1000,
  }) as algosdk.SuggestedParams

/** The fields every builder needs regardless of which call it is. */
const base = () => ({
  suggestedParams: suggestedParams(),
  sender: SENDER,
  appId: 741000001n,
  assetId: 10458941n,
  agreementId: 42n,
})

/** Decode a `UInt64` app arg back to a number, for asserting against a plain index or count. */
const argNumber = (arg: Uint8Array) => Number(UINT64.decode(arg) as bigint)

describe('the refund window, not the whole roster', () => {
  // The one thing in this module that is arithmetic rather than transcription:
  // `refund_next` must name exactly the seats between the cursor and
  // `cursor + count`, in arrival order, because every payer an inner transfer
  // pays into has to be in this call's accounts array. Naming seat zero, or
  // the whole roster, would fail on chain with a resource error that gives no
  // hint the cursor was the actual mistake.
  it('names seats 2, 3 and 4 for a cursor of 2 and a count of 3', () => {
    const six = roster(6)
    const txn = buildRefundNext({
      ...base(),
      agreement: agreement({ refundCursor: 2, seats: 6 }),
      roster: six,
      count: 3,
    })
    const named = txn.applicationCall!.accounts.map((a) => a.toString())
    expect(named).toEqual([six[2]!.payer, six[3]!.payer, six[4]!.payer])
  })

  it('starts the window at zero once the cursor has not moved', () => {
    const six = roster(6)
    const txn = buildRefundNext({
      ...base(),
      agreement: agreement({ refundCursor: 0, seats: 6 }),
      roster: six,
      count: 2,
    })
    const named = txn.applicationCall!.accounts.map((a) => a.toString())
    expect(named).toEqual([six[0]!.payer, six[1]!.payer])
  })
})

describe('each call names only the boxes its table row lists', () => {
  // A call naming the wrong box set fails on chain with a resource error that
  // points nowhere near this module -- so this is worth pinning per call
  // rather than trusting one shared helper to be right for all four.
  const AGREEMENT_BOX = agreementBoxName(42n)
  const ROSTER_BOX = rosterBoxName(42n)

  it('expire names only the agreement box', () => {
    const txn = buildExpire({ ...base(), agreement: agreement() })
    const names = txn.applicationCall!.boxes.map((b) => b.name)
    expect(names).toEqual([AGREEMENT_BOX])
  })

  it('refund_next names the agreement box and the roster box', () => {
    const txn = buildRefundNext({
      ...base(),
      agreement: agreement(),
      roster: roster(5),
      count: 2,
    })
    const names = txn.applicationCall!.boxes.map((b) => b.name)
    expect(names).toEqual([AGREEMENT_BOX, ROSTER_BOX])
  })

  it('claim_refund names the agreement box and the roster box', () => {
    const txn = buildClaimRefund({ ...base(), roster: roster(5), seat: 1 })
    const names = txn.applicationCall!.boxes.map((b) => b.name)
    expect(names).toEqual([AGREEMENT_BOX, ROSTER_BOX])
  })

  it('close names the agreement box and the roster box', () => {
    const txn = buildClose({ ...base(), agreement: agreement() })
    const names = txn.applicationCall!.boxes.map((b) => b.name)
    expect(names).toEqual([AGREEMENT_BOX, ROSTER_BOX])
  })
})

describe('accounts carry the party the inner transfer actually pays', () => {
  it('expire names the beneficiary, not the sender or the creator', () => {
    const txn = buildExpire({ ...base(), agreement: agreement() })
    expect(txn.applicationCall!.accounts.map((a) => a.toString())).toEqual([BENEFICIARY])
  })

  it('claim_refund names the roster seat, not the sender', () => {
    const five = roster(5)
    const txn = buildClaimRefund({ ...base(), roster: five, seat: 3 })
    expect(txn.applicationCall!.accounts.map((a) => a.toString())).toEqual([five[3]!.payer])
  })

  it('close names the creator, not the beneficiary', () => {
    const txn = buildClose({ ...base(), agreement: agreement() })
    expect(txn.applicationCall!.accounts.map((a) => a.toString())).toEqual([CREATOR])
  })
})

describe('the selector and its encoded arguments', () => {
  // Decoded back through the same ABI type rather than compared to a
  // hand-written byte array -- a hand-written vector would only prove this
  // file agrees with itself.
  it('encodes expire(agreementId)', () => {
    const txn = buildExpire({ ...base(), agreement: agreement() })
    const args = txn.applicationCall!.appArgs
    expect(algosdk.bytesToHex(args[0]!)).toBe(SELECTORS.expire)
    expect(UINT64.decode(args[1]!)).toBe(42n)
    expect(args).toHaveLength(2)
  })

  it('encodes refund_next(agreementId, count)', () => {
    const txn = buildRefundNext({
      ...base(),
      agreement: agreement(),
      roster: roster(5),
      count: 3,
    })
    const args = txn.applicationCall!.appArgs
    expect(algosdk.bytesToHex(args[0]!)).toBe(SELECTORS.refundNext)
    expect(UINT64.decode(args[1]!)).toBe(42n)
    expect(argNumber(args[2]!)).toBe(3)
  })

  it('encodes claim_refund(agreementId, seat)', () => {
    const txn = buildClaimRefund({ ...base(), roster: roster(5), seat: 2 })
    const args = txn.applicationCall!.appArgs
    expect(algosdk.bytesToHex(args[0]!)).toBe(SELECTORS.claimRefund)
    expect(UINT64.decode(args[1]!)).toBe(42n)
    expect(argNumber(args[2]!)).toBe(2)
  })

  it('encodes close(agreementId)', () => {
    const txn = buildClose({ ...base(), agreement: agreement() })
    const args = txn.applicationCall!.appArgs
    expect(algosdk.bytesToHex(args[0]!)).toBe(SELECTORS.close)
    expect(UINT64.decode(args[1]!)).toBe(42n)
    expect(args).toHaveLength(2)
  })
})

describe('argument rejections no chain state could fix', () => {
  // These are wrong independently of what the pool's record says, which is
  // exactly the line the task brief draws: `availableActions` in
  // `pool/view.ts` owns every check that depends on chain state, so a
  // builder re-checking one of those would be a second copy free to drift.
  // What is left is argument shape -- and `pool/view.ts` never sees `count`
  // or a seat index, both chosen at the call site, so nothing upstream of
  // this module could have caught these.
  it('rejects a count of zero, naming the ceiling', () => {
    expect(() =>
      buildRefundNext({ ...base(), agreement: agreement(), roster: roster(5), count: 0 }),
    ).toThrow(CallArgumentError)
    expect(() =>
      buildRefundNext({ ...base(), agreement: agreement(), roster: roster(5), count: 0 }),
    ).toThrow(new RegExp(String(REFUND_BATCH_CEILING)))
  })

  it('rejects a count past the per-call ceiling', () => {
    expect(() =>
      buildRefundNext({
        ...base(),
        agreement: agreement(),
        roster: roster(5),
        count: REFUND_BATCH_CEILING + 1,
      }),
    ).toThrow(CallArgumentError)
  })

  it('accepts a count exactly at the ceiling', () => {
    // The boundary the rejection test above sits one past -- worth its own
    // case, because an off-by-one in the bound would reject the ceiling
    // itself rather than only what exceeds it.
    expect(() =>
      buildRefundNext({
        ...base(),
        agreement: agreement(),
        roster: roster(5),
        count: REFUND_BATCH_CEILING,
      }),
    ).not.toThrow()
  })

  it('rejects a seat past the end of the roster, naming its length', () => {
    const three = roster(3)
    expect(() => buildClaimRefund({ ...base(), roster: three, seat: 3 })).toThrow(
      CallArgumentError,
    )
    expect(() => buildClaimRefund({ ...base(), roster: three, seat: 3 })).toThrow(/3/)
  })

  it('rejects a seat below zero', () => {
    expect(() => buildClaimRefund({ ...base(), roster: roster(3), seat: -1 })).toThrow(
      CallArgumentError,
    )
  })

  it('rejects a seat against an empty roster the same way as a missing box', () => {
    // `chain/read.ts`'s readRoster returns [] for a box that no longer
    // exists -- the ordinary state once `close` has run -- so this is the
    // shape a caller actually hits, not a hypothetical.
    expect(() => buildClaimRefund({ ...base(), roster: [], seat: 0 })).toThrow(
      CallArgumentError,
    )
  })
})

describe('the size a fee check prices a call at', () => {
  it('holds every call under the ceiling with every variable-width field at its maximum', () => {
    // The fee check multiplies a congested per-byte rate by this ceiling. A
    // call that outgrew it would be priced too low, and offered to a wallet
    // that cannot pay for it.
    const MAX = (1n << 64n) - 1n
    const worst = {
      suggestedParams: {
        // A flat fee, so the fee field itself is encoded at its widest.
        fee: MAX,
        flatFee: true,
        minFee: MAX,
        firstValid: MAX - 1000n,
        lastValid: MAX,
        genesisHash: new Uint8Array(32).fill(0xff),
        genesisID: 'mainnet-v1.0',
      } as unknown as algosdk.SuggestedParams,
      sender: SENDER,
      appId: MAX,
      assetId: MAX,
      agreementId: MAX,
    }
    const record = agreement()
    const roster = Array.from({ length: 20 }, (_, index) => seat(index))
    const calls = [
      buildExpire({ ...worst, agreement: record }),
      buildRefundNext({ ...worst, agreement: record, roster, count: REFUND_BATCH_CEILING }),
      buildClaimRefund({ ...worst, roster, seat: 19 }),
      buildClose({ ...worst, agreement: record }),
    ]
    // Signed by a key that is not the sender's, so the signed bytes also carry
    // the authorising address a rekeyed wallet's signature adds: the largest
    // a signed call from any wallet can be.
    const signer = algosdk.generateAccount()
    for (const call of calls) {
      expect(call.signTxn(signer.sk).length).toBeLessThanOrEqual(CALL_BYTES_CEILING)
    }
  })
})

describe('the configured asset travels on every call', () => {
  it('carries foreignAssets on all four', () => {
    const five = roster(5)
    const calls = [
      buildExpire({ ...base(), agreement: agreement() }),
      buildRefundNext({ ...base(), agreement: agreement(), roster: five, count: 2 }),
      buildClaimRefund({ ...base(), roster: five, seat: 0 }),
      buildClose({ ...base(), agreement: agreement() }),
    ]
    for (const txn of calls) {
      expect(txn.applicationCall!.foreignAssets).toEqual([base().assetId])
    }
  })
})
