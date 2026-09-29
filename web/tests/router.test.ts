import { describe, expect, it } from 'vitest'
import { parseRoute } from '../src/router'
import { poolPath } from '../src/pool/machine'

describe('the two screens are reached at the paths the reducer mints', () => {
  it('reads /app and /app/ as the entry screen', () => {
    // Links only mint `/app/`, but a buyer may type the bare spelling, and a
    // server of the bundle may answer it rather than redirect -- either way it
    // must land on the same screen rather than on "no such page".
    expect(parseRoute('/app')).toEqual({ kind: 'entry' })
    expect(parseRoute('/app/')).toEqual({ kind: 'entry' })
  })

  it('reads a pool path back as the id it was minted from', () => {
    // The round trip is the property that matters: `poolPath` is what the
    // reducer navigates to, so a link it mints must parse to the same pool.
    for (const id of [0n, 25n, 18446744073709551615n]) {
      expect(parseRoute(poolPath(id))).toEqual({ kind: 'pool', agreementId: id })
    }
  })

  it('accepts the literal pool paths', () => {
    expect(parseRoute('/app/pool/0')).toEqual({ kind: 'pool', agreementId: 0n })
    expect(parseRoute('/app/pool/25')).toEqual({ kind: 'pool', agreementId: 25n })
  })

  it('recognises the spike harness path, which App gates on DEV', () => {
    expect(parseRoute('/app/spike')).toEqual({ kind: 'spike' })
  })
})

describe('a path that is not a pool never reaches BigInt or the chain', () => {
  it('accepts the largest uint64 and refuses one past it', () => {
    // Above 2^64 - 1 the id cannot name a box, and algosdk would refuse to
    // encode it only once a read was already under way.
    expect(parseRoute('/app/pool/18446744073709551615')).toEqual({
      kind: 'pool',
      agreementId: 18446744073709551615n,
    })
    expect(parseRoute('/app/pool/18446744073709551616')).toEqual({ kind: 'not-found' })
  })

  it.each([
    // A sign: ids are unsigned.
    '/app/pool/-1',
    // `BigInt('1e3')` throws, so an unchecked segment would take the screen
    // down from a typo.
    '/app/pool/1e3',
    // `BigInt(' 7')` succeeds, so an unchecked segment would reach pool 7
    // from a spelling `poolPath` never produces.
    '/app/pool/ 7',
    // Likewise `BigInt('007')`: a second URL for pool 7.
    '/app/pool/007',
    // There is no sub-page of a pool.
    '/app/pool/25/extra',
    // Nothing after the prefix at all.
    '/app/pool/',
  ])('refuses %s', (pathname) => {
    expect(parseRoute(pathname)).toEqual({ kind: 'not-found' })
  })

  it('refuses a path outside the application base', () => {
    expect(parseRoute('/elsewhere')).toEqual({ kind: 'not-found' })
  })
})
