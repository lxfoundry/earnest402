// @vitest-environment jsdom
//
// The whole client, routed: a link on one screen opening another.
//
// With React's user timing switched on. A development build of React 19.2 logs
// every re-render's changed props to the browser's performance timeline, and
// only where `console.timeStamp` and `performance.measure` both exist -- which
// is every desktop Chrome and never jsdom. That logger passes an array of
// primitives to `JSON.stringify`, which throws on a bigint, and it throws
// outside any error boundary: React processes no update after it, so a link
// moves the address bar and the screen never follows. Without the two stubs
// below this file passes against exactly that defect, which is how it went
// unnoticed until a wallet was connected in a real browser.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Before React is imported: it decides once, at module load, whether user
// timing exists.
vi.hoisted(() => {
  if (typeof console.timeStamp !== 'function') console.timeStamp = () => {}
  if (typeof performance.measure !== 'function') {
    performance.measure = (() => undefined) as unknown as Performance['measure']
  }
})

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { Agreement } from '../src/chain/decode'
import type { Effect, Event } from '../src/pool/machine'
import type { Circumstances } from '../src/pool/view'

const runMock = vi.hoisted(() => vi.fn())
const useWalletMock = vi.hoisted(() => vi.fn())

// The effect executor and the wallet are faked, for the reasons
// `pool.hook.dom.test.tsx` gives; the router, the reducer and every screen are
// the real ones.
vi.mock('../src/pool/runner', () => ({ run: runMock }))
vi.mock('@txnlab/use-wallet-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@txnlab/use-wallet-react')>()
  return { ...actual, useWallet: useWalletMock }
})

const { App } = await import('../src/ui/App')
const { resetBrandForTests, resetConfigForTests } = await import('../src/config')

const WALLET = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'
const POOL = 25n

const record: Agreement = {
  condition: 0,
  state: 'RELEASED',
  deadline: 1789651281n,
  sharePrice: 100_000n,
  minSeats: 5,
  maxSeats: 5,
  seats: 5,
  refundCursor: 0,
  unclaimedSeats: 0,
  totalHeld: 0n,
  commitSha256: 'fa'.repeat(32),
  beneficiary: 'B'.repeat(58),
  verifier: 'V'.repeat(58),
  creator: 'C'.repeat(58),
}

const chain: Circumstances = { record, seat: null, chainNow: 1789651000n, canPayFee: true }

/** Let the fake runner's promises settle and their events dispatch. */
const settle = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

beforeEach(() => {
  vi.stubEnv('VITE_NETWORK', 'algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI=')
  vi.stubEnv('VITE_ASSET_ID', '10458941')
  vi.stubEnv('VITE_APP_ID', '741000001')
  vi.stubEnv('VITE_FACILITATOR_URL', 'https://facilitator.example')
  vi.stubEnv('VITE_RESOURCE_HOST', 'https://earnest.example')
  vi.stubEnv('VITE_MAX_FILE_BYTES', '16777216')
  vi.stubEnv('VITE_ALGOD_URL', 'https://algod.example')
  vi.stubEnv('VITE_INDEXER_URL', 'https://indexer.example')
  vi.stubEnv('VITE_IPFS_GATEWAY', 'https://ipfs.example')
  // The index, whatever the machine's environment says.
  vi.stubEnv('VITE_BRAND', '')
  vi.stubEnv('VITE_OFFER_B64', '')
  resetConfigForTests()
  resetBrandForTests()
  runMock.mockReset()
  runMock.mockImplementation(async (effect: Effect): Promise<Event[]> => {
    if (effect.type === 'FIND_MY_POOLS') {
      return [{ type: 'MY_POOLS_FOUND', address: effect.address, agreementIds: [POOL] }]
    }
    if (effect.type === 'READ_POOL') {
      return [
        {
          type: 'READ_RECEIVED',
          agreementId: effect.agreementId,
          address: effect.address,
          circumstances: chain,
        },
      ]
    }
    return []
  })
  useWalletMock.mockReturnValue({
    activeAddress: WALLET,
    activeWallet: null,
    signTransactions: vi.fn(),
    wallets: [],
  })
  window.history.replaceState(null, '', '/app')
})

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  resetConfigForTests()
  resetBrandForTests()
  window.history.replaceState(null, '', '/')
})

describe('a pool found from the entry screen opens when its link is clicked', () => {
  it('shows the pool screen, not just a new address', async () => {
    render(<App />)
    await act(settle)

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Find my seats' }))
      await settle()
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Pool 25' }))
      await settle()
    })

    expect(window.location.pathname).toBe('/app/pool/25')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('Pool 25')
  })
})

describe('the way back to the entry screen', () => {
  it('lands on /app/, the spelling every server of the bundle answers', async () => {
    // Not `/app`: a server whose base is `/app/` serves only paths under it,
    // and Vite answers the bare spelling with a 404 page. The address a link
    // leaves in the bar is the one a reload, a bookmark or a copied link asks
    // for next.
    window.history.replaceState(null, '', '/app/pool/25')
    render(<App />)
    await act(settle)

    // The logo is the way home, named by its alt text.
    await act(async () => {
      fireEvent.click(screen.getByRole('link', { name: 'Earnest' }))
      await settle()
    })

    expect(window.location.pathname).toBe('/app/')
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(
      '"If this, then pay" for x402.',
    )
  })
})

describe('the source link in the footer', () => {
  it('points at the published repository and opens safely', async () => {
    render(<App />)
    await act(settle)

    const link = screen.getByRole('link', { name: 'Source on GitHub' })
    expect(link.getAttribute('href')).toBe('https://github.com/lxfoundry/earnest402')
    expect(link.getAttribute('target')).toBe('_blank')
    // `noopener` so the opened tab cannot reach back through `window.opener`.
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('does not collide with the logo, which is found by that exact name', async () => {
    // `getByRole` matches an accessible name exactly and throws on two hits,
    // so a second link named `Earnest` would break the way home above.
    render(<App />)
    await act(settle)

    expect(screen.getAllByRole('link', { name: 'Earnest' })).toHaveLength(1)
  })
})
