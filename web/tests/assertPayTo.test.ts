import { describe, expect, it } from 'vitest'
import algosdk from 'algosdk'
import {
  FileTooLargeError,
  PayToMismatchError,
  assertFileSize,
  assertPayTo,
} from '../src/payment/assertPayTo'

describe('assertPayTo', () => {
  it('accepts the address derived from the configured application', () => {
    const derived = algosdk.getApplicationAddress(741000001n).toString()
    expect(() => assertPayTo(derived, 741000001n)).not.toThrow()
  })

  it('refuses an address belonging to a different application', () => {
    // The failure this exists for: a client left pointed at a superseded
    // deployment would otherwise move money into the wrong contract.
    const other = algosdk.getApplicationAddress(741000002n).toString()
    expect(() => assertPayTo(other, 741000001n)).toThrow(PayToMismatchError)
  })

  it('refuses an ordinary wallet address', () => {
    expect(() =>
      assertPayTo('KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M', 741000001n),
    ).toThrow(PayToMismatchError)
  })

  it('compares strings, not an Address object', () => {
    // getApplicationAddress returns an Address in algosdk 3.x. Comparing it
    // to a string with === is always false, which would turn this assertion
    // into an unconditional throw -- caught here rather than in production.
    const derived = algosdk.getApplicationAddress(741000001n)
    expect(typeof derived).toBe('object')
    expect(() => assertPayTo(derived.toString(), 741000001n)).not.toThrow()
  })
})

describe('assertFileSize', () => {
  it('accepts a file at the limit', () => {
    expect(() => assertFileSize(1000, 1000)).not.toThrow()
  })

  it('refuses a file over the limit', () => {
    // Refused before the unpaid pass: a file over the limit cannot complete,
    // and quoting it first would commit an agreement and park its deposit
    // against a job that is already lost.
    expect(() => assertFileSize(1001, 1000)).toThrow(FileTooLargeError)
  })

  it('refuses an empty file', () => {
    expect(() => assertFileSize(0, 1000)).toThrow(FileTooLargeError)
  })
})
