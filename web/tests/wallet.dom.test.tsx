// @vitest-environment jsdom
//
// The only file in the suite that renders a component, and therefore the only
// one that needs a DOM. It is opt-in per file rather than global because
// jsdom's TextEncoder returns a Uint8Array from another realm and algosdk
// rejects it outright, which would break every payment test in the suite.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { StrictMode } from 'react'
import { cleanup, render, screen } from '@testing-library/react'

const built = vi.hoisted(() => ({ count: 0 }))
const useWalletMock = vi.hoisted(() => vi.fn())

vi.mock('@txnlab/use-wallet-react', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@txnlab/use-wallet-react')>()
  // Counts constructions so the memo in WalletProvider can be observed. A
  // manager built in the render body is a new store every render.
  class CountingManager extends actual.WalletManager {
    constructor(...args: ConstructorParameters<typeof actual.WalletManager>) {
      built.count += 1
      super(...args)
    }
  }
  return { ...actual, WalletManager: CountingManager, useWallet: useWalletMock }
})

const { ConnectButton, WalletProvider } = await import('../src/wallet')
const { resetConfigForTests } = await import('../src/config')

const ADDRESS = 'IUHCGQZG5KF2T5BMWXZFCH64T6ZAEU5NHTEWRHMG2644WK55PZ7Z34FAKM'

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
  resetConfigForTests()
  built.count = 0
  useWalletMock.mockReset()
})

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  resetConfigForTests()
})

describe('ConnectButton', () => {
  it('offers one button per wallet when nothing is connected', () => {
    useWalletMock.mockReturnValue({
      wallets: [
        { id: 'pera', metadata: { name: 'Pera' }, connect: vi.fn() },
        { id: 'defly', metadata: { name: 'Defly' }, connect: vi.fn() },
      ],
      activeAddress: null,
      activeWallet: null,
    })
    render(<ConnectButton />)
    expect(screen.getByRole('button', { name: 'Connect Pera' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Connect Defly' })).toBeDefined()
  })

  it('shows a truncated address and a way out once connected', () => {
    const disconnect = vi.fn()
    useWalletMock.mockReturnValue({
      wallets: [],
      activeAddress: ADDRESS,
      activeWallet: { disconnect },
    })
    render(<ConnectButton />)
    // Truncated, never the whole 58 characters: the full address is not what
    // a buyer reads to confirm they are on the right account.
    expect(screen.getByText(/^IUHCGQ…FAKM$/)).toBeDefined()
    expect(screen.queryByText(ADDRESS)).toBeNull()
    screen.getByRole('button', { name: 'Disconnect' }).click()
    expect(disconnect).toHaveBeenCalledTimes(1)
  })

  it('offers no connect buttons while one is already active', () => {
    useWalletMock.mockReturnValue({
      wallets: [{ id: 'pera', metadata: { name: 'Pera' }, connect: vi.fn() }],
      activeAddress: ADDRESS,
      activeWallet: { disconnect: vi.fn() },
    })
    render(<ConnectButton />)
    expect(screen.queryByRole('button', { name: 'Connect Pera' })).toBeNull()
  })
})

describe('WalletProvider', () => {
  const tree = (strict: boolean) => {
    const inner = (
      <WalletProvider>
        <p>child</p>
      </WalletProvider>
    )
    return strict ? <StrictMode>{inner}</StrictMode> : inner
  }

  it('does not rebuild the manager on every render', () => {
    // The bug this pins: a manager constructed in the render body is a new
    // manager, and a new store, every render -- so a session is discarded as
    // soon as it is established. Re-rendering must not construct another.
    useWalletMock.mockReturnValue({ wallets: [], activeAddress: null, activeWallet: null })
    const { rerender } = render(tree(false))
    expect(screen.getByText('child')).toBeDefined()
    expect(built.count).toBe(1)
    rerender(tree(false))
    rerender(tree(false))
    expect(built.count).toBe(1)
  })

  it('survives StrictMode without accumulating managers', () => {
    // StrictMode deliberately double-invokes the useMemo callback on mount to
    // surface impure computations, so two constructions there are React
    // working as designed and only one result is kept. What would be a bug is
    // the count continuing to climb as the tree re-renders.
    useWalletMock.mockReturnValue({ wallets: [], activeAddress: null, activeWallet: null })
    const { rerender } = render(tree(true))
    const afterMount = built.count
    rerender(tree(true))
    rerender(tree(true))
    expect(built.count).toBe(afterMount)
  })
})
