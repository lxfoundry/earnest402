import {
  NetworkId,
  WalletManager,
  WalletProvider as UseWalletProvider,
  useWallet,
} from '@txnlab/use-wallet-react'
import { pera } from '@txnlab/use-wallet-pera'
import { defly } from '@txnlab/use-wallet-defly'
import { kmd } from '@txnlab/use-wallet-kmd'
import { useMemo, type ReactNode } from 'react'
import { MAINNET_CAIP2, TESTNET_CAIP2, getConfig } from './config'
import type { Signer } from './payment/schemeClient'

/**
 * Wallet connection, kept deliberately thin: the library owns the protocols,
 * and this module only maps the configured network onto the library's own
 * identifier, adapts its signer to the shape the group encoder wants, and
 * renders one button.
 */

// CAIP-2 identifiers, which is what the 402 carries and what `config.network`
// holds. The library wants its own short name, so the mapping lives here
// rather than duplicating either vocabulary elsewhere.
export function networkIdFor(caip2: string): string {
  if (caip2 === MAINNET_CAIP2) return NetworkId.MAINNET
  if (caip2 === TESTNET_CAIP2) return NetworkId.TESTNET
  // Anything else is a local chain: LocalNet mints a fresh genesis hash on
  // every reset, so it cannot be matched by value.
  return NetworkId.LOCALNET
}

export function createManager(caip2: string): WalletManager {
  const network = networkIdFor(caip2)
  return new WalletManager({
    wallets: [
      // Two extensions rather than one, so a buyer without Pera installed is
      // not turned away and the hand-run TestNet circuit is not tied to a
      // single vendor. use-wallet 5 ships each adapter as its own package,
      // which is why adding one is a dependency and not a list entry.
      pera(),
      defly(),
      // KMD is what lets a LocalNet run sign without a browser extension,
      // which is what makes the end-to-end test automatable. It is offered
      // only there, because it talks to a local key daemon that does not
      // exist on TestNet or MainNet.
      ...(network === NetworkId.LOCALNET ? [kmd()] : []),
    ],
    defaultNetwork: network,
  })
}

export function WalletProvider({ children }: { children: ReactNode }) {
  // Built once. A manager constructed in the render body is a new manager,
  // and a new store, on every render -- including StrictMode's double render
  // on mount, which would discard a session as soon as it was established.
  // The memo is what keeps `getConfig()` lazy without paying that price.
  const manager = useMemo(() => createManager(getConfig().network), [])
  return <UseWalletProvider manager={manager}>{children}</UseWalletProvider>
}

type SignTransactions = (
  txnGroup: Uint8Array[],
  indexesToSign?: number[],
) => Promise<(Uint8Array | null)[]>

/**
 * Adapt a use-wallet signer to the group encoder's shape: one slot per
 * transaction, null where the facilitator will countersign.
 *
 * The adapters disagree about what they return, and the declared type
 * `(Uint8Array | null)[]` describes only one of them:
 *
 *   - Pera returns one slot per transaction in the group, null for the ones
 *     it was not asked to sign.
 *   - KMD returns only the signed transactions, compacted.
 *
 * Read from their sources, not inferred. Assuming either shape breaks
 * silently against the other: with `indexesToSign = [1, 2]` over a
 * three-transaction group, scattering Pera's `[null, sig1, sig2]` by index
 * puts null on the transfer and the transfer's signature on the application
 * call -- a group that is malformed in a way no type checker sees.
 *
 * So the shape is discriminated by length rather than assumed.
 */
export function toSigner(signTransactions: SignTransactions): Signer {
  return async (txns, indexes) => {
    const signed = await signTransactions(txns, indexes)

    // Aligned: one slot per transaction, already in the right places.
    if (signed.length === txns.length) return signed

    // Compacted: one entry per requested index, in that order.
    if (signed.length === indexes.length) {
      const out: (Uint8Array | null)[] = txns.map(() => null)
      indexes.forEach((groupIndex, i) => {
        out[groupIndex] = signed[i] ?? null
      })
      return out
    }

    throw new Error(
      `the wallet returned ${signed.length} transactions for a group of ` +
        `${txns.length} with ${indexes.length} to sign. Refusing to guess ` +
        'which is which: a misplaced signature settles as a malformed group.',
    )
  }
}

export function useSigner(): Signer | null {
  const { signTransactions, activeAddress } = useWallet()
  if (!activeAddress) return null
  return toSigner(signTransactions as SignTransactions)
}

export function useWalletAddress(): string | null {
  return useWallet().activeAddress ?? null
}

export function ConnectButton() {
  const { wallets, activeAddress, activeWallet } = useWallet()

  if (activeAddress) {
    return (
      <span className="wallet">
        <code className="address">
          {activeAddress.slice(0, 6)}…{activeAddress.slice(-4)}
        </code>
        <button type="button" onClick={() => void activeWallet?.disconnect()}>
          Disconnect
        </button>
      </span>
    )
  }

  return (
    <div className="connect">
      {wallets.map((wallet) => (
        <button key={wallet.id} type="button" onClick={() => void wallet.connect()}>
          Connect {wallet.metadata.name}
        </button>
      ))}
    </div>
  )
}
