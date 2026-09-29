import algosdk from 'algosdk'
import { buildJoinGroup } from './buildJoinGroup'
import { assertPayTo } from './assertPayTo'

/**
 * The group builder joined to a signer and to the wire encoding.
 *
 * `buildJoinGroup` stays pure; everything that needs a wallet or makes a
 * decision lives here.
 */

export interface Quote402 {
  agreementId: bigint
  amount: bigint
  assetId: bigint
  payTo: string
  feePayer: string
  appId: bigint
  expiresAt: number
}

/**
 * A quote with the payer attached, which is the only kind that can be built
 * into a group.
 *
 * The 402 cannot name the buyer -- putting the address in the query string
 * would change the resource identity and the server's idempotency key -- so
 * `requestQuote` returns a `Quote402` with no buyer at all, and the wallet
 * supplies one here. Making that a separate type rather than an empty string
 * is what stops an unpayable quote reaching `buildPayment`: there is no value
 * of `Quote402` that satisfies the parameter, so the omission is a compile
 * error at the call site instead of a runtime check inside it.
 */
export type PayableQuote = Quote402 & { buyer: string }

export function withBuyer(quote: Quote402, buyer: string): PayableQuote {
  return { ...quote, buyer }
}

export interface PaymentPayload {
  paymentGroup: string[]
  paymentIndex: number
}

/**
 * Returns one entry per transaction: signed bytes at the indexes it was asked
 * for, null elsewhere. This is the shape `use-wallet` produces and the shape
 * the Python signer produces, so neither side has to adapt.
 */
export type Signer = (
  txns: Uint8Array[],
  indexes: number[],
) => Promise<(Uint8Array | null)[]>

export class UnsignedLegError extends Error {}

export function encodePaymentPayload(
  unsigned: Uint8Array[],
  signed: (Uint8Array | null)[],
  paymentIndex: number,
  indexesToSign: number[],
): PaymentPayload {
  // A mixed array: signed where the buyer signed, unsigned for the fee-payer
  // leg the facilitator cosigns. Sending a signed placeholder for index 0
  // would make the facilitator's own signature impossible to add.
  //
  // Which is why the fallback needs `indexesToSign` to be safe. Falling back
  // is correct at index 0 and wrong at 1 and 2: a wallet that declined, or
  // returned a short array, would otherwise have its unsigned transfer
  // encoded into a payload that looks perfectly well-formed and fails at the
  // facilitator with nothing pointing back here.
  const paymentGroup = unsigned.map((bytes, i) => {
    const signature = signed[i]
    if (signature) return algosdk.bytesToBase64(signature)
    if (indexesToSign.includes(i)) {
      throw new UnsignedLegError(
        `the wallet returned no signature for transaction ${i}, which the ` +
          'buyer must sign. Refusing to send a group with an unsigned leg.',
      )
    }
    return algosdk.bytesToBase64(bytes)
  })
  return { paymentGroup, paymentIndex }
}

export async function buildPayment(
  quote: PayableQuote,
  signer: Signer,
  suggestedParams: algosdk.SuggestedParams,
): Promise<PaymentPayload> {
  // Before anything is built, and before the wallet is disturbed: the 402's
  // payTo must be the account of the application this client is configured
  // for. One local derivation, no network call.
  assertPayTo(quote.payTo, quote.appId)

  const { txns, paymentIndex, indexesToSign } = buildJoinGroup({
    suggestedParams,
    buyer: quote.buyer,
    feePayer: quote.feePayer,
    appAccount: quote.payTo,
    appId: quote.appId,
    assetId: quote.assetId,
    amount: quote.amount,
    agreementId: quote.agreementId,
    notes: defaultNotes(),
  })

  const unsigned = txns.map((txn) => algosdk.encodeUnsignedTransaction(txn))
  // One call, so the buyer sees one prompt covering both of their legs.
  const signed = await signer(unsigned, indexesToSign)
  return encodePaymentPayload(unsigned, signed, paymentIndex, indexesToSign)
}

/**
 * The reference client's note format. Production wants the timestamp; only
 * the golden vectors want it fixed, and they pass their own.
 *
 * Milliseconds scaled to nanoseconds rather than real nanosecond precision:
 * the field is a uniqueness marker the library writes, and nothing reads it
 * back.
 */
function defaultNotes() {
  const stamp = BigInt(Date.now()) * 1_000_000n
  const encoder = new TextEncoder()
  return {
    feePayer: encoder.encode(`x402-fee-payer-${stamp}`),
    payment: encoder.encode(`x402-payment-${stamp}`),
  }
}
