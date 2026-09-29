import { describe, expect, it, vi } from 'vitest'
import { createManager, networkIdFor, toSigner } from '../src/wallet'

const MAINNET = 'algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8='
const TESTNET = 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI='

const txns = [Uint8Array.from([0]), Uint8Array.from([1]), Uint8Array.from([2])]
const SIG_1 = Uint8Array.from([0xa1])
const SIG_2 = Uint8Array.from([0xa2])

describe('networkIdFor', () => {
  it('maps the two public CAIP-2 identifiers', () => {
    expect(networkIdFor(MAINNET)).toBe('mainnet')
    expect(networkIdFor(TESTNET)).toBe('testnet')
  })

  it('treats anything else as localnet', () => {
    // LocalNet mints a fresh genesis hash on every reset, so it cannot be
    // matched by value.
    expect(networkIdFor('algorand:somethingelse=')).toBe('localnet')
  })
})

describe('toSigner', () => {
  // The two shipped adapters return different shapes, and the declared type
  // describes only one of them. Both are covered because assuming either
  // breaks silently against the other.

  it('passes through an aligned result (the Pera shape)', async () => {
    // One slot per transaction, null where it was not asked to sign.
    const signTransactions = vi.fn(async () => [null, SIG_1, SIG_2])
    const result = await toSigner(signTransactions)(txns, [1, 2])
    expect(result).toEqual([null, SIG_1, SIG_2])
  })

  it('scatters a compacted result (the KMD shape)', async () => {
    // Only the signed ones, in the order requested.
    const signTransactions = vi.fn(async () => [SIG_1, SIG_2])
    const result = await toSigner(signTransactions)(txns, [1, 2])
    expect(result).toEqual([null, SIG_1, SIG_2])
  })

  it('puts the signatures on the same legs whichever shape it is handed', async () => {
    // The point of the whole function: scattering Pera's array by index would
    // put null on the transfer and the transfer's signature on the app call.
    const aligned = await toSigner(vi.fn(async () => [null, SIG_1, SIG_2]))(txns, [1, 2])
    const compacted = await toSigner(vi.fn(async () => [SIG_1, SIG_2]))(txns, [1, 2])
    expect(aligned).toEqual(compacted)
    expect(aligned[0]).toBeNull()
    expect(aligned[1]).toEqual(SIG_1)
    expect(aligned[2]).toEqual(SIG_2)
  })

  it('refuses a length it cannot interpret rather than guessing', async () => {
    // A misplaced signature settles as a malformed group, so guessing is the
    // one thing this must not do.
    const signTransactions = vi.fn(async () => [SIG_1])
    await expect(toSigner(signTransactions)(txns, [1, 2])).rejects.toThrow(
      /Refusing to guess/,
    )
  })

  it('passes the indexes through unchanged', async () => {
    // Explicit parameters so mock.calls is a two-element tuple rather than [].
    const signTransactions = vi.fn(
      async (_group: Uint8Array[], _indexes?: number[]) => [SIG_1, SIG_2],
    )
    await toSigner(signTransactions)(txns, [1, 2])
    expect(signTransactions.mock.calls[0]![1]).toEqual([1, 2])
  })

  it('asks the wallet exactly once', async () => {
    // One prompt covers both buyer legs; two would read as two payments.
    const signTransactions = vi.fn(async () => [SIG_1, SIG_2])
    await toSigner(signTransactions)(txns, [1, 2])
    expect(signTransactions).toHaveBeenCalledTimes(1)
  })
})

describe('createManager', () => {
  const idsFor = (caip2: string) => createManager(caip2).wallets.map((w) => w.id)

  it('offers both browser wallets on a public network', () => {
    // Two, so a buyer without one installed is not turned away and the
    // hand-run TestNet circuit is not tied to a single vendor.
    expect(idsFor(MAINNET)).toEqual(['pera', 'defly'])
    expect(idsFor(TESTNET)).toEqual(['pera', 'defly'])
  })

  it('adds KMD on a local chain and nowhere else', () => {
    // KMD is what lets a LocalNet run sign without a browser extension, which
    // is what makes the end-to-end test automatable. It talks to a local key
    // daemon that does not exist on TestNet or MainNet, so offering it there
    // would be a button that cannot work.
    expect(idsFor('algorand:afreshlocalgenesis=')).toContain('kmd')
    expect(idsFor(MAINNET)).not.toContain('kmd')
    expect(idsFor(TESTNET)).not.toContain('kmd')
  })
})
