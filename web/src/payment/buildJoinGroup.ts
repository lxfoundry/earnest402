import algosdk from 'algosdk'
import { agreementBoxName, rosterBoxName } from '../chain/decode'
import { selectorBytes } from '../chain/selectors'

/**
 * The three-transaction settlement group.
 *
 * The standard fee-abstracted x402 group is two transactions: an unsigned
 * fee-payer self-payment the facilitator cosigns, and the buyer's asset
 * transfer. This adds a third, buyer-signed leg -- the escrow application's
 * `join` call -- which must be atomic with the transfer, because the seat and
 * the payment have to succeed or fail together.
 *
 * Pure by construction. Suggested params and note bytes arrive as arguments
 * rather than being fetched or generated, which is what lets this be compared
 * byte for byte against the Python builder in `web/tests/golden`.
 */

// ARC-4 selector for `join(uint64,uint64)void`, which is the signature on both
// the escrow application and the earlier join spike -- so a group aimed at the
// wrong application fails on a box reference, never on the selector. Derived
// from `chain/selectors.ts` rather than spelled here a second time: that
// module is the single source now, and this export stays only because the
// golden-vector test and the spike import it by this name.
export const JOIN_SELECTOR = selectorBytes('join')

const UINT64 = new algosdk.ABIUintType(64)

export interface GroupNotes {
  feePayer: Uint8Array
  payment: Uint8Array
}

export interface JoinGroupParams {
  suggestedParams: algosdk.SuggestedParams
  buyer: string
  feePayer: string
  appAccount: string
  appId: bigint
  assetId: bigint
  amount: bigint
  agreementId: bigint
  notes: GroupNotes
}

export interface JoinGroup {
  txns: algosdk.Transaction[]
  /** The transfer's absolute index, which is what `join` is told to read. */
  paymentIndex: number
  /** The buyer's legs. Index 0 is left for the facilitator. */
  indexesToSign: number[]
}

/**
 * The two boxes `join` touches, both on its own application.
 *
 * The agreement box (prefix `a`) is read for state, deadline, share price and
 * seat count. The roster box (prefix `r`) is read to refuse a payer who
 * already holds a seat, and written to record the new one. A call naming only
 * one of them fails on the unnamed box.
 *
 * Built on `chain/decode.ts`'s `agreementBoxName` / `rosterBoxName` rather
 * than packing the prefix byte and the big-endian id here a second time --
 * that packing has to match the contract's own box addressing exactly, and
 * two independent copies of it are two places for that match to quietly stop
 * holding.
 */
export function escrowBoxNames(agreementId: bigint): algosdk.BoxReference[] {
  return [
    { appIndex: 0, name: agreementBoxName(agreementId) },
    { appIndex: 0, name: rosterBoxName(agreementId) },
  ]
}

export function buildJoinGroup(params: JoinGroupParams): JoinGroup {
  const {
    suggestedParams,
    buyer,
    feePayer,
    appAccount,
    appId,
    assetId,
    amount,
    agreementId,
    notes,
  } = params

  // Three legs at the live protocol minimum. Never a constant: the network
  // charges what it charges, and a hardcoded fee breaks on any protocol
  // change -- silently, as an underpaid group the node rejects.
  //
  // `minFee` is typed `number | bigint`, so the arithmetic is done in bigint
  // rather than coercing to number: a fee is a microAlgo amount, and amounts
  // in this codebase are bigint everywhere else.
  //
  // Absent or zero means the params are not usable, not that the minimum is
  // 1000. Defaulting would quietly reintroduce the constant this builder
  // exists to have removed, and the Python builder refuses the same input.
  const minFee = BigInt(suggestedParams.minFee ?? 0)
  if (minFee <= 0n) {
    throw new Error('suggested params carry no minFee; cannot price the group')
  }
  const pooledFee = minFee * 3n

  // flatFee stops algosdk recalculating from the transaction size, which
  // would undo the pooling.
  const feePayerParams: algosdk.SuggestedParams = {
    ...suggestedParams,
    fee: pooledFee,
    flatFee: true,
  }
  const zeroFeeParams: algosdk.SuggestedParams = {
    ...suggestedParams,
    fee: 0,
    flatFee: true,
  }

  const feePayerTxn = algosdk.makePaymentTxnWithSuggestedParamsFromObject({
    sender: feePayer,
    receiver: feePayer,
    amount: 0,
    note: notes.feePayer,
    suggestedParams: feePayerParams,
  })

  const axferTxn = algosdk.makeAssetTransferTxnWithSuggestedParamsFromObject({
    sender: buyer,
    receiver: appAccount,
    amount,
    assetIndex: assetId,
    note: notes.payment,
    suggestedParams: zeroFeeParams,
  })

  // Fixed at 1 because the ordering below is fixed. The facilitator is known
  // to accept [axfer, feePayer, appCall] too; pinning one ordering is what
  // lets the browser and the agent client agree without negotiating.
  const paymentIndex = 1

  const appCallTxn = algosdk.makeApplicationNoOpTxnFromObject({
    sender: buyer,
    appIndex: appId,
    appArgs: [
      JOIN_SELECTOR,
      UINT64.encode(agreementId),
      UINT64.encode(BigInt(paymentIndex)),
    ],
    boxes: escrowBoxNames(agreementId),
    suggestedParams: zeroFeeParams,
  })

  const txns = algosdk.assignGroupID([feePayerTxn, axferTxn, appCallTxn])

  return { txns, paymentIndex, indexesToSign: [1, 2] }
}
