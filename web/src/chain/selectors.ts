import algosdk from 'algosdk'

/**
 * The escrow application's ARC-4 method selectors, spelled once.
 *
 * Before this module existed the same four bytes were spelled twice, in two
 * encodings that happened to agree: `payment/buildJoinGroup.ts` held `join`'s
 * selector as a raw byte array, and `chain/read.ts` held five selectors,
 * including that same one, as lowercase hex in a private object. Two spots
 * that must always agree and are never checked against each other are exactly
 * the kind of duplicate that drifts silently -- a method renamed or a
 * signature changed on one side leaves the other quietly wrong, and nothing
 * fails until a call is built against the mismatched selector and rejected by
 * the contract for a reason that looks unrelated.
 *
 * The table below holds the seven methods of an agreement's lifecycle: the
 * ones this client sends, or reads back off the indexer. The contract exposes
 * twelve. The other five (`bootstrap`, `opt_in_asset`, `create_agreement`,
 * `set_admin`, `set_paused`) are deployment and operator calls that no client
 * builds or matches, so they are left out on purpose rather than spelled here
 * for nothing.
 *
 * Selectors are the first four bytes of the SHA-512/256 hash of the method's
 * ARC-4 signature. They are hex here, not raw bytes, because that is the
 * encoding `chain/read.ts` compares against transaction arguments it reads
 * back off the indexer; `selectorBytes` below is what a caller building an
 * application call argument wants instead. The full signatures, for
 * provenance:
 *
 * | Method | Signature |
 * |---|---|
 * | `join` | `join(uint64,uint64)void` |
 * | `expire` | `expire(uint64)void` |
 * | `refundNext` | `refund_next(uint64,uint64)void` |
 * | `claimRefund` | `claim_refund(uint64,uint64)void` |
 * | `close` | `close(uint64)void` |
 * | `releaseHash` | `release_hash(uint64,byte[])void` |
 * | `releaseQuorum` | `release_quorum(uint64)void` |
 */
export const SELECTORS = {
  join: 'f76681b7',
  expire: '575088fd',
  refundNext: '9368b091',
  claimRefund: '263141b3',
  close: 'cb7c6df2',
  releaseHash: '81d6f306',
  releaseQuorum: 'db674e6a',
} as const

export type SelectorName = keyof typeof SELECTORS

/** The selector as the four raw bytes an application call argument wants. */
export function selectorBytes(name: SelectorName): Uint8Array {
  return algosdk.hexToBytes(SELECTORS[name])
}
