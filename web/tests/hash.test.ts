import { describe, expect, it } from 'vitest'
import { sha256Hex } from '../src/hash'

describe('sha256Hex', () => {
  it('hashes the empty input to the known digest', async () => {
    expect(await sha256Hex(new Blob([]))).toBe(
      'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    )
  })

  it('hashes "abc" to the known digest', async () => {
    expect(await sha256Hex(new Blob(['abc']))).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    )
  })

  it('returns 64 lower-case hex characters', async () => {
    expect(await sha256Hex(new Blob(['anything']))).toMatch(/^[0-9a-f]{64}$/)
  })

  it('hashes a megabyte to the digest Python computes', async () => {
    // A known answer from an independent implementation (hashlib), not a
    // comparison against ourselves: 1024*1024+7 bytes of 0x61, a length
    // chosen to sit just past a block boundary.
    const big = new Uint8Array(1024 * 1024 + 7).fill(0x61)
    expect(await sha256Hex(new Blob([big]))).toBe(
      'd068b86fa9718c9ef56229139facc172e9698d68c6248bd86a04857a262ae79e',
    )
  })

  it('is the digest of the bytes, not of anything about the Blob', async () => {
    // Same bytes, different Blob construction: the commitment must depend on
    // content alone, since the server re-derives it from what it receives.
    const split = new Blob([new Uint8Array([0x61]), new Uint8Array([0x62, 0x63])])
    expect(await sha256Hex(split)).toBe(await sha256Hex(new Blob(['abc'])))
  })
})
