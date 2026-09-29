import algosdk from 'algosdk'

/**
 * Stage 1 of the browser-wallet spike: will a mobile wallet sign the
 * three-transaction join group at all, and does it hand the bytes back
 * unchanged?
 *
 * The facilitator only ever sees bytes. If the wallet returns the same
 * transaction bytes carrying a valid signature, then the group is
 * indistinguishable from one a local key produced -- and that case is already
 * settled by Verdict A. So the question this module answers is narrower than
 * "does the facilitator accept a wallet signature": it is "does the wallet
 * produce the same group a script would".
 *
 * Nothing here submits anything. It decodes what came back, compares it
 * against what was sent, and reports.
 *
 * Dev-only. `SpikePage` is mounted behind `import.meta.env.DEV`, so this
 * module is tree-shaken out of a production build.
 */

/**
 * An agreement id that cannot exist, so the group cannot settle even if the
 * signed bytes escaped this page: `join` reads box `a` + the id, finds
 * nothing, and the whole atomic group fails.
 *
 * Max uint64 rather than an arbitrary large number, because that value is
 * already a golden vector (`web/tests/golden/join-group.json`) and so is
 * known to round-trip through both builders.
 *
 * It has one cost, and the page says so: a wallet that simulates before
 * signing may refuse a group it can see will fail, which would look exactly
 * like a wallet refusing the *shape*. That is why the page lets a real
 * agreement id be typed in -- running both is what separates the two.
 */
export const UNSETTLEABLE_AGREEMENT_ID = 18446744073709551615n

/** The domain separator Algorand prefixes to a transaction before signing. */
const TX_PREFIX = Uint8Array.from([0x54, 0x58]) // "TX"

export type SignerShape = 'aligned' | 'compacted' | 'unrecognised'

export interface LegReport {
  index: number
  /** What the group builder asked the wallet to do with this leg. */
  wasAskedToSign: boolean
  /** Whether a signature came back for it. */
  signed: boolean
  /**
   * Whether the transaction inside the signed blob still encodes to the exact
   * bytes we handed over. `null` when nothing came back to compare.
   */
  bytesUnchanged: boolean | null
  /** Ed25519 check over "TX" || txnBytes; `null` when it could not be run. */
  signatureValid: boolean | null
  signatureBase64: string | null
  /** Set when the blob could not be decoded at all. */
  decodeError?: string
}

export interface ProbeReport {
  shape: SignerShape
  /** What the wallet returned, before any normalisation. */
  rawLength: number
  groupLength: number
  indexesToSign: number[]
  legs: LegReport[]
  /** True only if every leg the buyer owns came back signed and unmutated. */
  pass: boolean
  ed25519Available: boolean
}

/**
 * Classify what the wallet returned.
 *
 * `toSigner` in `web/src/wallet.tsx` discriminates the two adapter shapes by
 * array length and throws on anything else. Which shape Pera 5.0.0 actually
 * produces is asserted nowhere in the repository, so the probe records it
 * rather than relying on the comment that claims it.
 */
export function classifyShape(
  rawLength: number,
  groupLength: number,
  indexCount: number,
): SignerShape {
  // Checked in this order because a two-of-two group would satisfy both, and
  // aligned is the shape `toSigner` returns untouched.
  if (rawLength === groupLength) return 'aligned'
  if (rawLength === indexCount) return 'compacted'
  return 'unrecognised'
}

/**
 * Scatter a compacted return back onto absolute group indexes, so the rest of
 * the analysis does not care which shape arrived. Deliberately the same
 * mapping `toSigner` performs, reimplemented here rather than imported: the
 * probe's job includes telling us whether `toSigner`'s assumption holds, and a
 * probe that calls the code under test cannot report that it threw.
 */
export function normalise(
  raw: (Uint8Array | null)[],
  shape: SignerShape,
  groupLength: number,
  indexesToSign: number[],
): (Uint8Array | null)[] {
  if (shape === 'aligned') return raw
  if (shape === 'unrecognised') {
    throw new Error(
      `the wallet returned ${raw.length} entries for a group of ` +
        `${groupLength} with ${indexesToSign.length} to sign, which matches ` +
        'neither known adapter shape',
    )
  }
  const out: (Uint8Array | null)[] = Array.from({ length: groupLength }, () => null)
  indexesToSign.forEach((groupIndex, i) => {
    out[groupIndex] = raw[i] ?? null
  })
  return out
}

function sameBytes(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false
  return a.every((byte, i) => byte === b[i])
}

/**
 * Is Ed25519 available in this browser's WebCrypto?
 *
 * Chrome 137+ and Firefox 130+ have it. Where it is missing the probe says so
 * and leaves signature validity to stage 2, which settles it for free: adding
 * a crypto dependency to answer a question the facilitator answers anyway
 * would be a worse trade.
 */
export async function ed25519Available(): Promise<boolean> {
  try {
    await crypto.subtle.importKey('raw', new Uint8Array(32), { name: 'Ed25519' }, false, [
      'verify',
    ])
    return true
  } catch {
    return false
  }
}

async function verifySignature(
  txnBytes: Uint8Array,
  signature: Uint8Array,
  signerAddress: string,
): Promise<boolean | null> {
  try {
    const publicKey = algosdk.decodeAddress(signerAddress).publicKey
    const key = await crypto.subtle.importKey(
      'raw',
      publicKey as BufferSource,
      { name: 'Ed25519' },
      false,
      ['verify'],
    )
    const signed = new Uint8Array(TX_PREFIX.length + txnBytes.length)
    signed.set(TX_PREFIX, 0)
    signed.set(txnBytes, TX_PREFIX.length)
    return await crypto.subtle.verify(
      { name: 'Ed25519' },
      key,
      signature as BufferSource,
      signed as BufferSource,
    )
  } catch {
    return null
  }
}

export interface AnalyseParams {
  unsigned: Uint8Array[]
  raw: (Uint8Array | null)[]
  indexesToSign: number[]
  /** The connected wallet's address: the account that should have signed. */
  signerAddress: string
  /** Skip the Ed25519 check when WebCrypto cannot do it. */
  canVerify: boolean
}

/**
 * Compare what came back against what was sent.
 *
 * Three separable findings, in increasing order of how much they tell us:
 * the shape of the array, whether the transaction bytes survived the round
 * trip, and whether the signature verifies. The second is the load-bearing
 * one -- a mutated transaction is a different transaction, and no signature
 * over it helps.
 */
export async function analyse(params: AnalyseParams): Promise<ProbeReport> {
  const { unsigned, raw, indexesToSign, signerAddress, canVerify } = params
  const shape = classifyShape(raw.length, unsigned.length, indexesToSign.length)
  const scattered =
    shape === 'unrecognised'
      ? unsigned.map(() => null)
      : normalise(raw, shape, unsigned.length, indexesToSign)

  const legs: LegReport[] = []
  for (let index = 0; index < unsigned.length; index += 1) {
    const sent = unsigned[index]
    const blob = scattered[index] ?? null
    const wasAskedToSign = indexesToSign.includes(index)

    if (!sent) throw new Error(`no unsigned bytes at index ${index}`)

    if (!blob) {
      legs.push({
        index,
        wasAskedToSign,
        signed: false,
        bytesUnchanged: null,
        signatureValid: null,
        signatureBase64: null,
      })
      continue
    }

    try {
      const decoded = algosdk.decodeSignedTransaction(blob)
      const reencoded = algosdk.encodeUnsignedTransaction(decoded.txn)
      const signature = decoded.sig ?? null
      legs.push({
        index,
        wasAskedToSign,
        signed: signature !== null,
        bytesUnchanged: sameBytes(reencoded, sent),
        signatureValid:
          canVerify && signature ? await verifySignature(sent, signature, signerAddress) : null,
        signatureBase64: signature ? algosdk.bytesToBase64(signature) : null,
      })
    } catch (error) {
      legs.push({
        index,
        wasAskedToSign,
        signed: false,
        bytesUnchanged: null,
        signatureValid: null,
        signatureBase64: null,
        decodeError: error instanceof Error ? error.message : String(error),
      })
    }
  }

  // A pass is about the buyer's legs only. Index 0 is the facilitator's and
  // must come back untouched -- which here means not coming back at all.
  const pass =
    shape !== 'unrecognised' &&
    legs
      .filter((leg) => leg.wasAskedToSign)
      .every(
        (leg) =>
          leg.signed && leg.bytesUnchanged === true && leg.signatureValid !== false,
      )

  return {
    shape,
    rawLength: raw.length,
    groupLength: unsigned.length,
    indexesToSign,
    legs,
    pass,
    ed25519Available: canVerify,
  }
}
