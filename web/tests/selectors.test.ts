import { readFileSync } from 'node:fs'
import algosdk from 'algosdk'
import { describe, expect, it } from 'vitest'
import { SELECTORS, type SelectorName, selectorBytes } from '../src/chain/selectors'

/**
 * The compiled contract's own description of its methods.
 *
 * Read from the file at test time rather than imported by `src/`: the bundle
 * is built from `web/` alone, and the contract's build output is not part of
 * it. The table is checked against the file instead, so a method whose
 * signature changes in the contract fails here rather than as a reader that
 * silently stops recognising a call.
 */
const ARC56 = JSON.parse(
  readFileSync(
    new URL('../../contracts/smart_contracts/artifacts/escrow/Escrow.arc56.json', import.meta.url),
    'utf8',
  ),
) as { methods: algosdk.ABIMethodParams[] }

const snakeCase = (name: string) => name.replace(/[A-Z]/g, (letter) => `_${letter.toLowerCase()}`)

/**
 * The signature each selector is derived from, kept here rather than in
 * `src/` because it exists only to check the shipped table against an
 * independent computation. Production code never needs the string that
 * produced a selector, only the four bytes.
 */
const SIGNATURES: Record<SelectorName, string> = {
  join: 'join(uint64,uint64)void',
  expire: 'expire(uint64)void',
  refundNext: 'refund_next(uint64,uint64)void',
  claimRefund: 'claim_refund(uint64,uint64)void',
  close: 'close(uint64)void',
  releaseHash: 'release_hash(uint64,byte[])void',
  releaseQuorum: 'release_quorum(uint64)void',
}

describe('the selector table', () => {
  it('holds four bytes per method', () => {
    for (const hex of Object.values(SELECTORS)) {
      expect(algosdk.hexToBytes(hex)).toHaveLength(4)
    }
  })

  it('never assigns the same selector to two methods', () => {
    const values = Object.values(SELECTORS)
    expect(new Set(values).size).toBe(values.length)
  })

  // algosdk 3.7.0 exposes `ABIMethod.fromSignature(...).getSelector()`, which
  // computes a selector from a method signature independently of this table.
  // That is what this test uses: two separate computations checked against
  // each other, rather than a hand-rolled ARC-4 hasher built to reproduce the
  // same hex it would then be judged against.
  it('matches what algosdk derives from each method signature', () => {
    for (const name of Object.keys(SIGNATURES) as SelectorName[]) {
      const derived = algosdk.ABIMethod.fromSignature(SIGNATURES[name]).getSelector()
      expect(algosdk.bytesToHex(derived)).toBe(SELECTORS[name])
    }
  })

  // The signatures above are typed by hand, so the check above cannot notice
  // the contract changing underneath them. This one derives every selector
  // from the compiled contract's ARC-56 description instead -- which is what
  // stands behind `release_hash` in particular, the one selector that decides
  // whether a note is shown to a buyer as their edition.
  it('matches the selector the compiled contract gives each method', () => {
    for (const name of Object.keys(SELECTORS) as SelectorName[]) {
      const method = ARC56.methods.find((m) => m.name === snakeCase(name))
      expect(method, `${snakeCase(name)} in Escrow.arc56.json`).toBeDefined()
      const derived = new algosdk.ABIMethod(method!).getSelector()
      expect(algosdk.bytesToHex(derived), name).toBe(SELECTORS[name])
    }
  })
})

describe('selectorBytes', () => {
  it('turns the hex table entry into the raw bytes an app call argument wants', () => {
    expect(selectorBytes('join')).toEqual(algosdk.hexToBytes('f76681b7'))
  })
})
