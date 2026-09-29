import { describe, expect, it, vi } from 'vitest'
import algosdk from 'algosdk'
import {
  UnsignedLegError,
  buildPayment,
  encodePaymentPayload,
  withBuyer,
} from '../src/payment/schemeClient'
import { PayToMismatchError } from '../src/payment/assertPayTo'

const APP_ID = 741000001n
const payTo = algosdk.getApplicationAddress(APP_ID).toString()

const suggestedParams: algosdk.SuggestedParams = {
  fee: 0,
  flatFee: true,
  firstValid: 1000,
  lastValid: 2000,
  genesisHash: new Uint8Array(32),
  genesisID: 'testnet-v1.0',
  minFee: 1000,
}

const BUYER = 'KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M'

/** What `requestQuote` returns: everything the 402 carries, and no buyer. */
const unpayable = {
  agreementId: 42n,
  amount: 10000n,
  assetId: 10458941n,
  payTo,
  feePayer: 'KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M',
  appId: APP_ID,
  expiresAt: 0,
}

const quote = withBuyer(unpayable, BUYER)

/** Stands in for a wallet: marks the bytes rather than really signing. */
const fakeSigner = vi.fn(async (txns: Uint8Array[], indexes: number[]) =>
  txns.map((_, i) => (indexes.includes(i) ? Uint8Array.from([0xff, i]) : null)),
)

describe('buildPayment', () => {
  it('asks the wallet to sign exactly indexes 1 and 2', async () => {
    fakeSigner.mockClear()
    await buildPayment(quote, fakeSigner, suggestedParams)
    expect(fakeSigner.mock.calls[0]![1]).toEqual([1, 2])
  })

  it('prompts the wallet exactly once', async () => {
    // One prompt covers both buyer legs. Two would read to a buyer as two
    // payments.
    fakeSigner.mockClear()
    await buildPayment(quote, fakeSigner, suggestedParams)
    expect(fakeSigner).toHaveBeenCalledTimes(1)
  })

  it('refuses when payTo is not the configured application', async () => {
    // Structurally impossible to join one application while paying another.
    const wrong = { ...quote, payTo: algosdk.getApplicationAddress(999n).toString() }
    await expect(buildPayment(wrong, fakeSigner, suggestedParams)).rejects.toThrow(
      PayToMismatchError,
    )
  })

  it('never asks the wallet to sign when the payTo check fails', async () => {
    fakeSigner.mockClear()
    const wrong = { ...quote, payTo: algosdk.getApplicationAddress(999n).toString() }
    await expect(buildPayment(wrong, fakeSigner, suggestedParams)).rejects.toThrow()
    expect(fakeSigner).not.toHaveBeenCalled()
  })
})

describe('encodePaymentPayload', () => {
  it('carries three base64 members and the payment index', async () => {
    const payload = await buildPayment(quote, fakeSigner, suggestedParams)
    expect(payload.paymentGroup).toHaveLength(3)
    expect(payload.paymentIndex).toBe(1)
    for (const member of payload.paymentGroup) {
      expect(member).toMatch(/^[A-Za-z0-9+/]+=*$/)
    }
  })

  it('leaves index 0 unsigned and the buyer legs signed', async () => {
    const payload = await buildPayment(quote, fakeSigner, suggestedParams)
    const decoded = payload.paymentGroup.map((m) => Buffer.from(m, 'base64'))
    // The unsigned leg still decodes as a bare transaction; the two signed
    // slots carry whatever the signer returned.
    expect(() =>
      algosdk.decodeUnsignedTransaction(Uint8Array.from(decoded[0]!)),
    ).not.toThrow()
    expect(Uint8Array.from(decoded[1]!)).toEqual(Uint8Array.from([0xff, 1]))
    expect(Uint8Array.from(decoded[2]!)).toEqual(Uint8Array.from([0xff, 2]))
  })

  const threeLegs = () => [
    Uint8Array.from([1]),
    Uint8Array.from([2]),
    Uint8Array.from([3]),
  ]

  it('falls back to the unsigned bytes only for the fee-payer leg', () => {
    // A signed placeholder at index 0 would make the facilitator's own
    // signature impossible to add, so index 0 must pass through unsigned.
    const payload = encodePaymentPayload(
      threeLegs(),
      [null, Uint8Array.from([0xaa]), Uint8Array.from([0xbb])],
      1,
      [1, 2],
    )
    expect(payload.paymentGroup[0]).toBe(Buffer.from([1]).toString('base64'))
    expect(payload.paymentGroup[1]).toBe(Buffer.from([0xaa]).toString('base64'))
    expect(payload.paymentGroup[2]).toBe(Buffer.from([0xbb]).toString('base64'))
  })

  it('refuses to encode a buyer leg the wallet did not sign', () => {
    // Falling back here would produce a payload that looks well-formed and
    // fails at the facilitator with nothing pointing back at the wallet.
    expect(() =>
      encodePaymentPayload(threeLegs(), [null, Uint8Array.from([0xaa]), null], 1, [1, 2]),
    ).toThrow(UnsignedLegError)
  })

  it('refuses when the wallet declined everything', () => {
    expect(() =>
      encodePaymentPayload(threeLegs(), [null, null, null], 1, [1, 2]),
    ).toThrow(UnsignedLegError)
  })
})

describe('withBuyer', () => {
  // The 402 cannot name the buyer, so `requestQuote` returns a quote without
  // one and this is the only way to get a `PayableQuote`. There is no runtime
  // check for a missing buyer any more because there is no longer a value
  // that expresses one: `buildPayment` does not accept a bare `Quote402`, so
  // the omission is a compile error at the call site rather than a throw
  // inside it. What is left to test is that the seam carries the rest of the
  // quote through unchanged.
  it('attaches the buyer and changes nothing else', () => {
    const payable = withBuyer(unpayable, BUYER)
    expect(payable.buyer).toBe(BUYER)
    expect({ ...payable, buyer: undefined }).toEqual({ ...unpayable, buyer: undefined })
  })

  it('does not mutate the quote it was given', () => {
    withBuyer(unpayable, BUYER)
    expect(unpayable).not.toHaveProperty('buyer')
  })

  it('produces a quote buildPayment accepts', async () => {
    const payload = await buildPayment(
      withBuyer(unpayable, BUYER),
      fakeSigner,
      suggestedParams,
    )
    expect(payload.paymentIndex).toBe(1)
  })
})
